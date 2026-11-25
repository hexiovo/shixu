from __future__ import annotations

import json
from datetime import date, timedelta

from .errors import BusinessError
from .tool_schema import _date, _integer, _nullable_string, _object, _request_id, _string, _tool


_AMOUNT = {"type": "integer", "minimum": 0, "maximum": 100_000_000}
_FIELDS = {
    "request_id": _request_id, "meal_id": _string, "expected_version": _integer, "operation": {"enum": ["refund", "shares", "allocate"]},
    "payment_id": _string, "amount_cents": _AMOUNT, "reason": _nullable_string,
    "shares": {"type": "array", "items": _object({"person": _string, "amount_cents": _AMOUNT}, ("person", "amount_cents"))},
    "allocations": {"type": "array", "items": _object({"meal_id": _string, "amount_cents": _AMOUNT}, ("meal_id", "amount_cents"))},
}
TOOL_SCHEMAS = [
    _tool("meal_money_record", "Write a linked refund, confirmed shares, or an explicit allocation of one purchase across meals.", _FIELDS, (
        "request_id", "meal_id", "expected_version", "operation",
    )),
    _tool("meal_money_details", "Read actual payout, refund links, personal shares, ingredient consumption cost and allocations. Optionally include a saved budget.", {
        "meal_id": _string, "person": _string, "budget_period": {"enum": ["week", "month"]}, "period_start": _date,
    }, ("meal_id",)),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}
MIGRATION_SQL = [
    """CREATE TABLE payment_refunds(
       original_id TEXT NOT NULL REFERENCES payments(payment_id), refund_id TEXT PRIMARY KEY REFERENCES payments(payment_id),
       recorded_at TEXT NOT NULL)""",
    """CREATE TABLE meal_shares(entry_id TEXT PRIMARY KEY REFERENCES entries(id), shares TEXT NOT NULL)""",
    """CREATE TABLE purchase_allocations(
       payment_id TEXT NOT NULL REFERENCES payments(payment_id), meal_id TEXT NOT NULL REFERENCES entries(id),
       amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), PRIMARY KEY(payment_id,meal_id))""",
]


class MealFinanceMixin:
    @staticmethod
    def _payment_is_linked(connection, payment_id):
        return connection.execute(
            """SELECT 1 FROM payment_refunds WHERE original_id=? OR refund_id=?
               UNION ALL SELECT 1 FROM purchase_allocations WHERE payment_id=? LIMIT 1""",
            (payment_id, payment_id, payment_id),
        ).fetchone() is not None

    @staticmethod
    def _payment_refunded(connection, source_id):
        return connection.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) FROM payment_refunds r JOIN payments p ON p.payment_id=r.refund_id WHERE r.original_id=?",
            (source_id,),
        ).fetchone()[0]

    @staticmethod
    def _net_paid(meal, row):
        meal_lines = [line for line in meal["payments"] if line["scope"] == "meal"]
        extra = [line for line in meal["payments"] if line["scope"] == "extra"]
        lines = extra if row["meal_amount_manual"] else meal_lines + extra
        whole = [row["meal_amount_cents"]] if row["meal_amount_manual"] or not meal_lines else []
        amounts = whole + [
            (line["amount_cents"] * (-1 if line["kind"] == "discount" else 1))
            if line["amount_cents"] is not None and line["source"] == "actual" else None for line in lines
        ]
        return sum(amounts) if all(amount is not None for amount in amounts) else None

    def _bounded_amount(self, value, field, minimum=0):
        amount = self._integer(value, field, minimum=minimum)
        if amount > 100_000_000:
            raise BusinessError("INVALID_INPUT", f"{field} is outside the supported range")
        return amount

    def _finance_rows(self, payload, key, identity):
        rows = self._list(payload.get(key), key)
        if len(rows) > 50:
            raise BusinessError("INVALID_INPUT", "Provide at most 50 rows")
        found = {}
        for raw in rows:
            item = self._dict(raw, key)
            self._reject_extra_fields(item, {identity, "amount_cents"})
            label = self._string(item.get(identity), identity)
            normalized = label.casefold() if key == "shares" else label
            if normalized in found:
                raise BusinessError("INVALID_INPUT", "Use one row per participant or meal")
            found[normalized] = {identity: label, "amount_cents": self._bounded_amount(item.get("amount_cents"), "amount_cents")}
        return list(found.values())

    def _meal_money_record(self, payload):
        action = self._recommendation_enum(payload.get("operation"), "operation", {"refund", "shares", "allocate"})
        required = {"refund": {"payment_id", "amount_cents", "reason"}, "shares": {"shares"}, "allocate": {"payment_id", "allocations"}}
        self._reject_extra_fields(payload, {"request_id", "meal_id", "expected_version", "operation", *required[action]})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)

        def operation(connection):
            row = self._active_meal_row(connection, meal_id)
            self._check_version(row, meal_id, expected)
            self._audit_meal(connection, meal_id, "baseline")
            result = {"meal_id": meal_id, "version": expected+1, "operation": action}
            if action == "shares":
                shares = self._finance_rows(payload, "shares", "person")
                total = self._net_paid(self._entry(connection, meal_id), row)
                if total is not None and sum(share["amount_cents"] for share in shares) > max(0, total):
                    raise BusinessError("OVERALLOCATED", "Personal shares exceed the verified net payment", {"paid_cents": total})
                connection.execute("INSERT INTO meal_shares VALUES(?,?) ON CONFLICT(entry_id) DO UPDATE SET shares=excluded.shares",
                                   (meal_id, self._json(shares)))
                result["shares"] = shares
            else:
                payment_id = self._string(payload.get("payment_id"), "payment_id")
                payment = connection.execute("SELECT * FROM payments WHERE payment_id=? AND entry_id=?", (payment_id, meal_id)).fetchone()
                if not payment or payment["source"] != "actual" or payment["amount_cents"] is None or payment["kind"] == "discount":
                    raise BusinessError("NEEDS_ACTUAL_PAYMENT", "Select a known actual charge belonging to this meal")
                available = payment["amount_cents"] - self._payment_refunded(connection, payment_id)
                if action == "refund":
                    amount = self._bounded_amount(payload.get("amount_cents"), "amount_cents", 1)
                    allocated = connection.execute(
                        "SELECT COALESCE(SUM(amount_cents),0) FROM purchase_allocations WHERE payment_id=?", (payment_id,),
                    ).fetchone()[0]
                    if amount + allocated > available:
                        raise BusinessError("OVERALLOCATED", "Refund would exceed the unallocated, unrefunded charge",
                                            {"refundable_cents": max(0, available-allocated)})
                    reason = self._string(payload.get("reason"), "reason", nullable=True)
                    refund_id = self._new_refund_line(connection, meal_id, amount, reason)
                    connection.execute("INSERT INTO payment_refunds VALUES(?,?,?)", (payment_id, refund_id, self._now()))
                    result.update(refund_id=refund_id, original_payment_id=payment_id, amount_cents=amount)
                else:
                    allocations = self._finance_rows(payload, "allocations", "meal_id")
                    if sum(item["amount_cents"] for item in allocations) > available:
                        raise BusinessError("OVERALLOCATED", "Allocation exceeds the net purchase price", {"available_cents": available})
                    for item in allocations:
                        self._active_meal_row(connection, item["meal_id"])
                    connection.execute("DELETE FROM purchase_allocations WHERE payment_id=?", (payment_id,))
                    connection.executemany(
                        "INSERT INTO purchase_allocations VALUES(?,?,?)",
                        [(payment_id, item["meal_id"], item["amount_cents"]) for item in allocations],
                    )
                    result.update(payment_id=payment_id, allocations=allocations)
            self._touch_meal(connection, meal_id)
            self._audit_meal(connection, meal_id, "meal_money_record")
            return result

        return self._write("meal_money_record", payload, operation)

    def _new_refund_line(self, connection, meal_id, amount, reason):
        from uuid import uuid4

        refund_id = uuid4().hex
        connection.execute(
            "INSERT INTO payments(payment_id,entry_id,kind,scope,amount_cents,source,note) VALUES(?,?,'discount','extra',?,'actual',?)",
            (refund_id, meal_id, amount, reason),
        )
        return refund_id

    def _meal_money_details(self, payload):
        self._reject_extra_fields(payload, {"meal_id", "person", "budget_period", "period_start"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        person = self._string(payload.get("person"), "person", nullable=True)
        with self.store.read() as connection:
            row = self._active_meal_row(connection, meal_id)
            meal = self._entry(connection, meal_id)
            stored_shares = connection.execute("SELECT shares FROM meal_shares WHERE entry_id=?", (meal_id,)).fetchone()
            shares = json.loads(stored_shares["shares"]) if stored_shares else []
            refunds = connection.execute(
                """SELECT r.original_id,r.refund_id,r.recorded_at,p.amount_cents,p.note FROM payment_refunds r
                   JOIN payments p ON p.payment_id=r.refund_id WHERE p.entry_id=? ORDER BY r.recorded_at,r.refund_id""", (meal_id,),
            ).fetchall()
            incoming = connection.execute(
                """SELECT a.payment_id,p.entry_id AS purchase_meal_id,a.amount_cents FROM purchase_allocations a
                   JOIN payments p ON p.payment_id=a.payment_id JOIN active_entries e ON e.id=p.entry_id
                   WHERE a.meal_id=? ORDER BY a.payment_id""", (meal_id,),
            ).fetchall()
            outgoing = connection.execute(
                """SELECT a.payment_id,a.meal_id,a.amount_cents FROM purchase_allocations a
                   JOIN payments p ON p.payment_id=a.payment_id WHERE p.entry_id=? ORDER BY a.payment_id,a.meal_id""", (meal_id,),
            ).fetchall()
            leftovers = connection.execute(
                "SELECT leftover_id,servings,cost_cents FROM leftover_cost_uses WHERE meal_id=? ORDER BY rowid", (meal_id,),
            ).fetchall()
            budget = None
            if "period_start" in payload and "budget_period" not in payload:
                raise BusinessError("INVALID_INPUT", "period_start requires budget_period")
            if "budget_period" in payload:
                period = self._recommendation_enum(payload["budget_period"], "budget_period", {"week", "month"})
                period_start = payload.get("period_start")
                if period_start is None:
                    day = date.fromisoformat(row["calendar_date"])
                    period_start = (day - timedelta(days=day.weekday()) if period == "week" else day.replace(day=1)).isoformat()
                budget = self._budget_status({"period_type": period, "period_start": period_start})
            actual = self._net_paid(meal, row)
        return {"meal_id": meal_id, "version": row["version"], "net_paid_cents": actual, "consumption_cost": meal["consumption_cost"],
                "shares": shares, "personal_share_cents": next((share["amount_cents"] for share in shares if person and share["person"].casefold() == person.casefold()), None),
                "share_coverage": (None if actual is None else {
                    "allocated_cents": sum(share["amount_cents"] for share in shares),
                    "difference_cents": actual - sum(share["amount_cents"] for share in shares),
                }),
                "attributed_consumption_components_cents": sum(line["amount_cents"] for line in incoming) +
                                                           sum(line["cost_cents"] for line in leftovers),
                "leftover_costs": [dict(line) for line in leftovers],
                "refunds": [dict(line) for line in refunds], "allocated_in": [dict(line) for line in incoming],
                "allocated_out": [dict(line) for line in outgoing], "budget": budget}
