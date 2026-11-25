from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from .errors import BusinessError
from .kitchen import STOCK_EXPIRY_FIELDS, _quantity
from .tool_schema import _date, _integer, _nullable_string, _object, _request_id, _string, _tool


_QTY = {"type": ["number", "string"]}
_MOVE = _object({
    "batch_id": _string, "expected_version": _integer, "quantity": _QTY, "unit": _string,
    "reservation_id": _string,
}, ("batch_id", "expected_version", "quantity", "unit"))
_STOCK = {
    "operation": {"enum": ["reserve", "consume", "release", "count"]}, "items": {"type": "array", "items": _MOVE},
    "as_of": _date, "plan_id": _string, "reserve_until": _date, "source": _string,
}
_PURCHASE = {
    "request_id": _request_id, "shopping_list_id": _string, "item_id": _string, "expected_version": _integer,
    "quantity": _QTY, "unit": _string, "source": _string, **STOCK_EXPIRY_FIELDS,
    "price_cents": _integer, "region": _string,
}
TOOL_SCHEMAS = [
    _tool("stock_change_preview", "Preview an all-or-nothing inventory reservation, consumption, release or count by batch.", _STOCK, ("operation", "items", "source")),
    _tool("stock_change_commit", "Commit a stock_change_preview using its fingerprint and explicit confirmation.", {
        **_STOCK, "request_id": _request_id, "fingerprint": _string, "confirm": {"const": True},
    }, ("operation", "items", "source", "request_id", "fingerprint", "confirm")),
    _tool("stock_reservation_list", "Read active inventory reservations for a plan or batch.", {"plan_id": _string, "batch_id": _string, "as_of": _date, "limit": _integer, "offset": _integer}),
    _tool("leftover_take", "Take a whole number of portions from an available leftover, preserving the rest and its history.", {
        "request_id": _request_id, "leftover_id": _string, "expected_version": _integer, "servings": _integer, "as_of": _date,
        "used_in_meal_id": _string, "expected_meal_version": _integer, "consumption_cost_cents": _integer,
    }, ("request_id", "leftover_id", "expected_version", "servings")),
    _tool("menu_plan_reschedule", "Reschedule one current menu plan and rebuild a shopping list. Explicitly release its old reservations if needed.", {
        "request_id": _request_id, "plan_id": _string, "expected_version": _integer, "planned_date": _date,
        "meal_type": _string, "release_reservations": {"type": "boolean"},
    }, ("request_id", "plan_id", "expected_version", "planned_date")),
    _tool("shopping_list_list", "List saved shopping lists, optionally by plan or creation date.", {"plan_id": _string, "date_from": _date, "date_to": _date, "limit": _integer, "offset": _integer}),
    _tool("shopping_purchase_record", "Record the actual bought quantity, update the list and add a batch using a manual expiry date or a shelf life saved for its storage location.", _PURCHASE, (
        "request_id", "shopping_list_id", "item_id", "expected_version", "quantity", "unit", "source",
    )),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}
MIGRATION_SQL = [
    """CREATE TABLE stock_reservations(
       id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES inventory_batches(batch_id),
       plan_id TEXT REFERENCES menu_plans(plan_id), quantity TEXT NOT NULL, valid_until TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE INDEX stock_reservations_batch ON stock_reservations(batch_id,valid_until)""",
    """CREATE TABLE shopping_purchases(
       id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES shopping_list_items(item_id),
       batch_id TEXT NOT NULL REFERENCES inventory_batches(batch_id), quantity TEXT NOT NULL, unit TEXT NOT NULL,
       price_cents INTEGER, created_at TEXT NOT NULL)""",
    """CREATE TABLE leftover_cost_uses(
       request_id TEXT PRIMARY KEY, leftover_id TEXT NOT NULL REFERENCES leftovers(leftover_id),
       meal_id TEXT NOT NULL REFERENCES entries(id), servings INTEGER NOT NULL, cost_cents INTEGER NOT NULL)""",
]


class KitchenOperationsMixin:
    def _reserved_quantity(self, connection, batch_id, as_of, *, except_ids=(), except_plans=()):
        rows = connection.execute(
            "SELECT * FROM stock_reservations WHERE batch_id=? AND valid_until>=?", (batch_id, as_of),
        ).fetchall()
        return sum((Decimal(row["quantity"]) for row in rows if row["id"] not in except_ids and row["plan_id"] not in except_plans), Decimal(0))

    def _stock_change_data(self, connection, payload):
        action = self._recommendation_enum(payload.get("operation"), "operation", set(_STOCK["operation"]["enum"]))
        source = self._string(payload.get("source"), "source")
        as_of = self._date_value(payload["as_of"], "as_of") if "as_of" in payload else date.today().isoformat()
        plan_id = self._string(payload.get("plan_id"), "plan_id", nullable=True)
        plan = connection.execute("SELECT * FROM menu_plans WHERE plan_id=?", (plan_id,)).fetchone() if plan_id else None
        if plan_id and plan is None:
            raise BusinessError("NOT_FOUND", "Menu plan not found")
        end = self._date_value(payload["reserve_until"], "reserve_until") if "reserve_until" in payload else (
            plan["planned_date"] if plan else as_of
        )
        if action == "reserve" and (end < as_of or (plan and end < plan["planned_date"])):
            raise BusinessError("INVALID_INPUT", "Reservation must cover its use date")
        rows = self._list(payload.get("items"), "items")
        if not 1 <= len(rows) <= 30:
            raise BusinessError("INVALID_INPUT", "Provide one to thirty batches")
        result = []
        seen = set()
        for item in rows:
            item = self._dict(item, "item")
            self._reject_extra_fields(item, set(_MOVE["properties"]), "item")
            batch_id = self._string(item.get("batch_id"), "batch_id")
            if batch_id in seen:
                raise BusinessError("INVALID_INPUT", "Provide each batch once")
            seen.add(batch_id)
            row = connection.execute("SELECT * FROM inventory_batches WHERE batch_id=?", (batch_id,)).fetchone()
            expected = self._integer(item.get("expected_version"), "expected_version", minimum=1)
            self._expect_version(row, expected, "inventory batch", batch_id)
            quantity, unit = _quantity(item.get("quantity"), self._string(item.get("unit"), "unit"), "quantity")
            if unit.casefold() != row["unit"].casefold() or quantity < 0 or (quantity == 0 and action != "count"):
                raise BusinessError("INVALID_INPUT", "Quantity must be valid and match the batch unit")
            reservation_id = self._string(item.get("reservation_id"), "reservation_id", nullable=True)
            included = connection.execute("SELECT * FROM stock_reservations WHERE id=? AND batch_id=?",
                                          (reservation_id, batch_id)).fetchone() if reservation_id else None
            if (reservation_id and included is None) or (action == "release" and not reservation_id):
                raise BusinessError("NOT_FOUND", "Select a reservation of this batch")
            if included and (action not in {"release", "consume"} or quantity > Decimal(included["quantity"])):
                raise BusinessError("INVALID_INPUT", "Use no more than the reserved quantity")
            if included and plan_id and included["plan_id"] != plan_id:
                raise BusinessError("INVALID_INPUT", "Reservation belongs to another plan")
            if action in {"reserve", "consume"} and row["expires_on"] and row["expires_on"] < (end if action == "reserve" else as_of):
                raise BusinessError("INVENTORY_EXPIRED", "食材超过本次用料日期适用的登记期限。", {
                    "batch_id": batch_id, "ingredient": row["ingredient"], "expires_on": row["expires_on"],
                })
            reservation_as_of = date.today().isoformat() if action == "reserve" else as_of
            reserved = self._reserved_quantity(connection, batch_id, reservation_as_of)
            consumed_reserved = quantity if included and included["valid_until"] >= as_of and action == "consume" else Decimal(0)
            physical = Decimal(row["quantity"])
            after = quantity if action == "count" else (physical - quantity if action == "consume" else physical)
            held_after = reserved + (quantity if action == "reserve" else -consumed_reserved)
            if action != "release" and (after < held_after or after < 0):
                raise BusinessError("INSUFFICIENT_INVENTORY", "Requested quantity includes unavailable or reserved stock", {
                    "batch_id": batch_id, "physical_quantity": str(physical), "reserved_quantity": str(reserved),
                })
            result.append({"batch": dict(row), "quantity": str(quantity), "unit": row["unit"], "after": str(after),
                           "reserved_before": str(reserved), "reservation": dict(included) if included else None})
        data = {"operation": action, "source": source, "as_of": as_of, "plan_id": plan_id,
                "plan_version": plan["version"] if plan else None, "reserve_until": end, "items": result}
        fingerprint = hashlib.sha256(self._canonical_json(data).encode()).hexdigest()
        return data, fingerprint

    def _stock_change_preview(self, payload):
        self._reject_extra_fields(payload, set(_STOCK))
        with self.store.read() as connection:
            data, fingerprint = self._stock_change_data(connection, payload)
        return {**data, "fingerprint": fingerprint, "would_write": False}

    def _stock_change_commit(self, payload):
        self._reject_extra_fields(payload, {*_STOCK, "request_id", "confirm", "fingerprint"})
        if payload.get("confirm") is not True:
            raise BusinessError("INVALID_INPUT", "Explicit confirmation is required")
        supplied = self._string(payload.get("fingerprint"), "fingerprint")

        def operation(connection):
            data, fingerprint = self._stock_change_data(connection, payload)
            if fingerprint != supplied:
                raise BusinessError("STALE_PREVIEW", "Inventory or reservations changed since preview")
            action = data["operation"]
            items = []
            for index, item in enumerate(data["items"]):
                batch = item["batch"]
                batch_id = batch["batch_id"]
                quantity = Decimal(item["quantity"])
                reservation = item["reservation"]
                reservation_id = reservation["id"] if reservation else None
                if action == "reserve":
                    reservation_id = uuid.uuid4().hex
                    connection.execute("INSERT INTO stock_reservations VALUES(?,?,?,?,?,?)",
                                       (reservation_id, batch_id, data["plan_id"], item["quantity"], data["reserve_until"], self._now()))
                elif reservation:
                    remaining = Decimal(reservation["quantity"]) - quantity
                    if remaining:
                        connection.execute("UPDATE stock_reservations SET quantity=? WHERE id=?", (str(remaining), reservation_id))
                    else:
                        connection.execute("DELETE FROM stock_reservations WHERE id=?", (reservation_id,))
                delta = Decimal(item["after"]) - Decimal(batch["quantity"])
                if delta:
                    self._inventory_batch_adjust({"batch_id": batch_id, "expected_version": batch["version"], "unit": item["unit"],
                                                  "source": data["source"], "delta_quantity": str(delta),
                                                  "request_id": self._subrequest(payload["request_id"], index)})
                else:
                    connection.execute("UPDATE inventory_batches SET version=version+1,updated_at=? WHERE batch_id=?",
                                       (self._now(), batch_id))
                items.append({"batch_id": batch_id, "version": batch["version"] + 1, "quantity": item["after"],
                              "unit": item["unit"], "reservation_id": reservation_id})
            return {"operation": action, "items": items}

        return self._write("stock_change_commit", payload, operation)

    def _stock_reservation_list(self, payload):
        self._reject_extra_fields(payload, {"plan_id", "batch_id", "as_of", "limit", "offset"})
        date_key = self._date_value(payload.get("as_of", date.today().isoformat()), "as_of")
        where = ["valid_until>=?"]
        values = [date_key]
        for key in ("plan_id", "batch_id"):
            if key in payload:
                where.append(f"{key}=?")
                values.append(self._string(payload[key], key))
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            rows = connection.execute(
                f"SELECT * FROM stock_reservations WHERE {' AND '.join(where)} ORDER BY valid_until,id LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def _leftover_take(self, payload):
        self._reject_extra_fields(payload, {"request_id", "leftover_id", "expected_version", "servings", "as_of",
                                           "used_in_meal_id", "expected_meal_version", "consumption_cost_cents"})
        leftover_id = self._string(payload.get("leftover_id"), "leftover_id")
        as_of = self._date_value(payload.get("as_of", date.today().isoformat()), "as_of")
        servings = self._integer(payload.get("servings"), "servings", minimum=1)
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        meal_id = self._string(payload.get("used_in_meal_id"), "used_in_meal_id", nullable=True)
        cost_fields = {"used_in_meal_id", "expected_meal_version", "consumption_cost_cents"}
        if cost_fields.intersection(payload) and not cost_fields <= payload.keys():
            raise BusinessError("INVALID_INPUT", "An allocated leftover cost needs the meal, its version and confirmed cost")
        if cost_fields <= payload.keys():
            if meal_id is None:
                raise BusinessError("INVALID_INPUT", "used_in_meal_id is required")
            meal_version = self._integer(payload["expected_meal_version"], "expected_meal_version", minimum=1)
            cost = self._bounded_amount(payload["consumption_cost_cents"], "consumption_cost_cents")

        def operation(connection):
            row = connection.execute("SELECT * FROM leftovers WHERE leftover_id=?", (leftover_id,)).fetchone()
            self._expect_version(row, expected, "leftover", leftover_id)
            if self._leftover_record(row, as_of)["expired"]:
                raise BusinessError("INSUFFICIENT_LEFTOVER", "剩菜已超过登记的使用期限。", {
                    "leftover_id": leftover_id, "name": row["name"], "use_by": row["use_by"], "expired": True,
                })
            if row["status"] != "available" or row["prepared_on"] > as_of or row["servings"] < servings:
                raise BusinessError("INSUFFICIENT_LEFTOVER", "Requested servings are not available")
            left = row["servings"] - servings
            fields = {key: row[key] for key in ("leftover_id", "name", "prepared_on", "use_by", "source_meal_id", "note")}
            result = self._leftover_upsert({**fields, "expected_version": expected, "servings": left,
                                           "status": "available" if left else "used_up",
                                           "request_id": self._subrequest(payload["request_id"], 0)})
            if meal_id:
                if row["source_meal_id"] == meal_id:
                    raise BusinessError("INVALID_INPUT", "Select the later meal that consumed this leftover")
                entry = self._active_meal_row(connection, meal_id)
                self._check_version(entry, meal_id, meal_version)
                existing = json.loads(entry["consumption_cost_json"])
                if existing["source"] == "estimate":
                    raise BusinessError("NEEDS_ACTUAL_COST", "Confirm or clear the prior estimate before adding an actual cost")
                # An explicit component is recorded separately when the whole meal
                # cost remains unknown. An existing verified total can include it.
                version = meal_version
                if existing["source"] == "actual":
                    total = existing["min_cents"] + cost
                    changes = {"consumption_cost": {"min_cents": total, "max_cents": total, "source": "actual"}}
                    saved = self._meal_update({"request_id": self._subrequest(payload["request_id"], 1), "meal_id": meal_id,
                                               "expected_version": meal_version, "scope": "meal", "changes": changes})
                    version = saved["version"]
                else:
                    self._audit_meal(connection, meal_id, "baseline")
                    self._touch_meal(connection, meal_id)
                    version += 1
                connection.execute("INSERT INTO leftover_cost_uses VALUES(?,?,?,?,?)",
                                   (payload["request_id"], leftover_id, meal_id, servings, cost))
                self._audit_meal(connection, meal_id, "leftover_take")
                result.update(used_in_meal_id=meal_id, meal_version=version, consumption_cost_cents=cost)
            return result

        return self._write("leftover_take", payload, operation)

    def _menu_plan_reschedule(self, payload):
        self._reject_extra_fields(payload, {"request_id", "plan_id", "expected_version", "planned_date", "meal_type", "release_reservations"})
        plan_id = self._string(payload.get("plan_id"), "plan_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        planned = self._date_value(payload.get("planned_date"), "planned_date")

        def operation(connection):
            row = connection.execute("SELECT * FROM menu_plans WHERE plan_id=?", (plan_id,)).fetchone()
            self._expect_version(row, expected, "menu plan", plan_id)
            reservations = connection.execute("SELECT id,batch_id FROM stock_reservations WHERE plan_id=?", (plan_id,)).fetchall()
            if reservations and payload.get("release_reservations") is not True:
                raise BusinessError("PLAN_HAS_STOCK_RESERVATIONS", "Confirm release of this plan's old reservations first",
                                    {"reservation_ids": [item["id"] for item in reservations]})
            connection.execute("DELETE FROM stock_reservations WHERE plan_id=?", (plan_id,))
            for batch_id in {item["batch_id"] for item in reservations}:
                connection.execute("UPDATE inventory_batches SET version=version+1,updated_at=? WHERE batch_id=?",
                                   (self._now(), batch_id))
            meal_type = self._analytics_meal_type(payload.get("meal_type", row["meal_type"]))
            if row["meal_id"]:
                raise BusinessError("MEAL_ALREADY_LINKED", "Reschedule an unrealized plan before linking a meal")
            connection.execute("UPDATE menu_plans SET planned_date=?,meal_type=?,version=version+1,updated_at=? WHERE plan_id=?",
                               (planned, meal_type, self._now(), plan_id))
            saved = connection.execute("SELECT * FROM menu_plans WHERE plan_id=?", (plan_id,)).fetchone()
            shopping = self._shopping_list_build({"plan_ids": [plan_id], "request_id": self._subrequest(payload["request_id"], 0)})
            return {"plan": self._menu_plan_record(connection, saved), "shopping_list": shopping,
                    "released_reservation_ids": [item["id"] for item in reservations]}

        return self._write("menu_plan_reschedule", payload, operation)

    def _shopping_list_list(self, payload):
        self._reject_extra_fields(payload, {"plan_id", "date_from", "date_to", "limit", "offset"})
        first, last = self._analytics_dates(payload.get("date_from"), payload.get("date_to"))
        where = ["1"]
        values = []
        for value, clause in ((first, "substr(created_at,1,10)>=?"), (last, "substr(created_at,1,10)<=?")):
            if value:
                where.append(clause)
                values.append(value)
        if "plan_id" in payload:
            where.append("EXISTS(SELECT 1 FROM json_each(plan_ids_json) WHERE value=?)")
            values.append(self._string(payload["plan_id"], "plan_id"))
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            rows = connection.execute(f"SELECT * FROM shopping_lists WHERE {' AND '.join(where)} ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                                      (*values, limit, offset)).fetchall()
        return {"items": [{"shopping_list_id": row["shopping_list_id"], "plan_ids": json.loads(row["plan_ids_json"]),
                            "created_at": row["created_at"]} for row in rows], "offset": offset, "limit": limit}

    def _shopping_purchase_record(self, payload, *, complete_unknown_quantity=False):
        self._reject_extra_fields(payload, set(_PURCHASE))
        list_id = self._string(payload.get("shopping_list_id"), "shopping_list_id")
        item_id = self._string(payload.get("item_id"), "item_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        unit_text = self._string(payload.get("unit"), "unit")
        quantity, unit = _quantity(payload.get("quantity"), unit_text, "quantity")
        if quantity <= 0:
            raise BusinessError("INVALID_INPUT", "Purchase quantity must be positive")
        source = self._string(payload.get("source"), "source")
        price = self._integer(payload.get("price_cents"), "price_cents", minimum=0, nullable=True)

        def operation(connection):
            row = connection.execute("SELECT * FROM shopping_list_items WHERE shopping_list_id=? AND item_id=?", (list_id, item_id)).fetchone()
            self._expect_version(row, expected, "shopping-list item", item_id)
            if row["unit"] and row["unit"].casefold() != unit.casefold():
                raise BusinessError("INVALID_INPUT", "Purchase unit is incompatible with the required quantity")
            request = payload["request_id"]
            batch = self._inventory_batch_upsert({
                "request_id": self._subrequest(request, 0), "ingredient": row["ingredient"], "quantity": str(quantity), "unit": unit,
                "source": source, **{field: payload[field] for field in STOCK_EXPIRY_FIELDS if field in payload},
            })
            price_snapshot = None
            if price is not None:
                price_snapshot = self._price_snapshot_add({
                    "request_id": self._subrequest(request, 1), "ingredient": row["ingredient"], "package_quantity": payload["quantity"],
                    "unit": unit_text, "price_cents": price, "region": payload.get("region"),
                    "recorded_on": payload.get("recorded_on", date.today().isoformat()), "source": source,
                })
            purchase_id = uuid.uuid4().hex
            connection.execute("INSERT INTO shopping_purchases VALUES(?,?,?,?,?,?,?)",
                               (purchase_id, item_id, batch["batch_id"], str(quantity), unit, price, self._now()))
            purchases = connection.execute("SELECT quantity,unit FROM shopping_purchases WHERE item_id=?", (item_id,)).fetchall()
            total, remaining = self._shopping_progress(row, purchases, unit)
            status = "bought" if remaining == 0 or (complete_unknown_quantity and remaining is None) else "needed"
            connection.execute("UPDATE shopping_list_items SET status=?,version=version+1,updated_at=? WHERE item_id=?",
                               (status, self._now(), item_id))
            return {"purchase_id": purchase_id, "item_id": item_id, "version": expected+1, "status": status,
                    "quantity_purchased": str(total), "remaining_quantity": str(remaining) if remaining is not None else None,
                    "inventory_batch": batch, "price_snapshot": price_snapshot}

        return self._write("shopping_purchase_record", payload, operation)

    @staticmethod
    def _shopping_progress(item, purchases, unit=None):
        unit = unit or item["unit"]
        total = sum((Decimal(purchase["quantity"]) for purchase in purchases
                     if unit is not None and purchase["unit"].casefold() == unit.casefold()), Decimal(0))
        remaining = max(Decimal(0), Decimal(item["known_quantity_to_buy"]) - total) if item["quantity_known"] else None
        return total, remaining
