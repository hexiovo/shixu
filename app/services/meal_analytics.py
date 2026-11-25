from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .errors import BusinessError
from .tool_schema import (
    _date,
    _mode,
    _request_id,
    _string,
    _tool,
)


_MODES = ("eat_out", "delivery", "home_cooking")
_MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack", "other")
_MODE_SCOPE = {"type": "string", "enum": ["all", "out_of_home", *_MODES]}


TOOL_SCHEMAS = [
    _tool(
        "spending_summary",
        "Summarize active meal payments and standalone milk-tea purchases, separating actual amounts, estimate ranges, unique records with unknown amounts, and persisted unknown payment lines. A mode filter includes only meals of that mode; without it, standalone milk tea is included.",
        {"date_from": _date, "date_to": _date, "mode": _mode},
    ),
    _tool(
        "period_compare",
        "Compare active meal counts and payment totals across two inclusive date ranges. Exact payment differences are returned only when both ranges have complete actual amounts.",
        {
            "first_date_from": _date,
            "first_date_to": _date,
            "second_date_from": _date,
            "second_date_to": _date,
            "mode": _mode,
        },
        ("first_date_from", "first_date_to", "second_date_from", "second_date_to"),
    ),
    _tool(
        "meal_frequency",
        "Count active meals under combined date, meal type, mode, dish, canonical store, ISO weekday, and current meal-tag filters. top_limit truncates ranking lists only, never the total count.",
        {
            "date_from": _date,
            "date_to": _date,
            "meal_type": {"type": "string", "enum": list(_MEAL_TYPES)},
            "mode": _mode,
            "dish": _string,
            "store_id": _string,
            "weekdays": {"type": "array", "minItems": 1, "maxItems": 7, "items": {"type": "integer", "minimum": 1, "maximum": 7}},
            "tag": {"type": "string", "minLength": 1, "maxLength": 100},
            "top_limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    _tool(
        "budget_set",
        "Set a weekly or monthly budget for a mode scope. period_start must be Monday for a week or the first day of a month; updates require expected_version.",
        {
            "request_id": _request_id,
            "period_type": {"type": "string", "enum": ["week", "month"]},
            "period_start": _date,
            "mode_scope": _MODE_SCOPE,
            "budget_cents": {"type": "integer", "minimum": 0},
            "expected_version": {"type": ["integer", "null"], "minimum": 1},
        },
        ("request_id", "period_type", "period_start", "budget_cents"),
    ),
    _tool(
        "budget_status",
        "Read the saved budget and actual payment coverage for one weekly or monthly period. Remaining amount is withheld when the budget is missing or payment data is incomplete.",
        {
            "period_type": {"type": "string", "enum": ["week", "month"]},
            "period_start": _date,
            "mode_scope": _MODE_SCOPE,
        },
        ("period_type", "period_start"),
    ),
    _tool(
        "meal_tags_set",
        "Replace a meal's explicit user-confirmed tags. The tag assignment is linked to the resulting meal version; edits to the meal make the old assignment stale until tags are confirmed again.",
        {
            "request_id": _request_id,
            "meal_id": _string,
            "expected_version": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
        },
        ("request_id", "meal_id", "expected_version", "tags"),
    ),
    _tool(
        "diet_review",
        "Review current user-confirmed meal tags among active meals, with counts by tag, mode, and meal type. Tags attached to an older meal version are reported as stale and excluded from current counts.",
        {
            "date_from": _date,
            "date_to": _date,
            "tag": {"type": "string", "minLength": 1, "maxLength": 100},
            "mode": _mode,
            "meal_type": {"type": "string", "enum": list(_MEAL_TYPES)},
        },
    ),
]


HANDLERS = {
    "spending_summary": "_spending_summary",
    "period_compare": "_period_compare",
    "meal_frequency": "_meal_frequency",
    "budget_set": "_budget_set",
    "budget_status": "_budget_status",
    "meal_tags_set": "_meal_tags_set",
    "diet_review": "_diet_review",
}


MIGRATION_SQL = [
    "CREATE TABLE IF NOT EXISTS period_budgets ("
    "period_type TEXT NOT NULL CHECK (period_type IN ('week', 'month')), "
    "period_start TEXT NOT NULL, "
    "mode_scope TEXT NOT NULL CHECK (mode_scope IN ('all', 'out_of_home', 'eat_out', 'delivery', 'home_cooking')), "
    "budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0), "
    "version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "PRIMARY KEY (period_type, period_start, mode_scope))",
    "CREATE TABLE IF NOT EXISTS meal_tags ("
    "entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE, "
    "tag TEXT NOT NULL CHECK (length(trim(tag)) BETWEEN 1 AND 100), "
    "meal_version INTEGER NOT NULL CHECK (meal_version > 0), "
    "created_at TEXT NOT NULL, PRIMARY KEY (entry_id, tag))",
    "CREATE INDEX IF NOT EXISTS meal_tags_tag_entry ON meal_tags(tag, entry_id)",
]


class MealAnalyticsMixin:
    def _analytics_dates(
        self,
        date_from_value: Any,
        date_to_value: Any,
        *,
        from_field: str = "date_from",
        to_field: str = "date_to",
        required: bool = False,
    ) -> tuple[str | None, str | None]:
        if required and date_from_value is None:
            self._iso_date(date_from_value, from_field)
        if required and date_to_value is None:
            self._iso_date(date_to_value, to_field)
        date_from = (
            date.fromisoformat(self._iso_date(date_from_value, from_field)).isoformat()
            if date_from_value is not None else None
        )
        date_to = (
            date.fromisoformat(self._iso_date(date_to_value, to_field)).isoformat()
            if date_to_value is not None else None
        )
        if date_from is not None and date_to is not None and date_from > date_to:
            raise BusinessError("INVALID_INPUT", f"{from_field} cannot be after {to_field}", {"field": from_field})
        return date_from, date_to

    def _period_window(self, period_type: str, period_start_value: Any) -> tuple[str, str]:
        period_start = date.fromisoformat(self._iso_date(period_start_value, "period_start"))
        if period_type == "week":
            if period_start.weekday() != 0:
                raise BusinessError("INVALID_INPUT", "period_start must be a Monday for a weekly budget", {"field": "period_start"})
            period_end = period_start + timedelta(days=6)
        else:
            if period_start.day != 1:
                raise BusinessError("INVALID_INPUT", "period_start must be the first day of a month", {"field": "period_start"})
            period_end = date(period_start.year, period_start.month, calendar.monthrange(period_start.year, period_start.month)[1])
        return period_start.isoformat(), period_end.isoformat()

    def _analytics_mode(self, value: Any, field: str = "mode") -> str | None:
        if value is None:
            return None
        mode = self._string(value, field)
        if mode not in _MODES:
            raise BusinessError("INVALID_INPUT", f"{field} is unsupported", {"field": field})
        return mode

    def _analytics_meal_type(self, value: Any) -> str | None:
        if value is None:
            return None
        meal_type = self._string(value, "meal_type")
        if meal_type not in _MEAL_TYPES:
            raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
        return meal_type

    @staticmethod
    def _spending_summary_result(
        connection: sqlite3.Connection,
        date_from: str | None,
        date_to: str | None,
        modes: tuple[str, ...] | None = None,
        *,
        include_standalone: bool = True,
    ) -> dict[str, Any]:
        clauses = ["e.record_kind IN ('meal', 'milk_tea')"] if include_standalone and modes is None else ["e.record_kind = 'meal'"]
        values: list[Any] = []
        if date_from is not None:
            clauses.append("e.calendar_date >= ?")
            values.append(date_from)
        if date_to is not None:
            clauses.append("e.calendar_date <= ?")
            values.append(date_to)
        if modes is not None:
            if len(modes) == 1:
                clauses.append("e.mode = ?")
                values.append(modes[0])
            else:
                clauses.append("e.mode IN (?, ?)")
                values.extend(modes)
        where = " AND ".join(clauses)
        entries = connection.execute(
            "SELECT e.id, e.record_kind, e.mode, e.meal_amount_cents, e.meal_amount_manual, e.consumption_cost_json "
            f"FROM active_entries e WHERE {where}",
            values,
        ).fetchall()
        payments: dict[str, list[sqlite3.Row]] = {}
        for row in connection.execute(
            "SELECT p.entry_id, p.kind, p.scope, p.amount_cents, p.amount_min_cents, p.amount_max_cents, p.source "
            "FROM payments p JOIN active_entries e ON e.id = p.entry_id WHERE " + where,
            values,
        ):
            payments.setdefault(row["entry_id"], []).append(row)

        category_names = ("whole_meal", "extra", "standalone_milk_tea")
        payment_totals = {
            name: {"actual_cents": 0, "estimate_min_cents": 0, "estimate_max_cents": 0,
                   "unknown_line_count": 0, "unknown_entry_ids": set(), "estimated_entry_ids": set()}
            for name in category_names
        }
        total_unknown_entry_ids: set[str] = set()
        total_estimated_entry_ids: set[str] = set()

        def add_payment(category: str, entry_id: str, payment: Any, *, persisted_line: bool = True) -> None:
            item = payment_totals[category]
            sign = -1 if payment["kind"] == "discount" else 1
            if payment["source"] == "actual":
                item["actual_cents"] += sign * payment["amount_cents"]
            elif payment["source"] == "estimate":
                low = payment["amount_cents"] if payment["amount_cents"] is not None else payment["amount_min_cents"]
                high = payment["amount_cents"] if payment["amount_cents"] is not None else payment["amount_max_cents"]
                item["estimate_min_cents"] += sign * (high if sign < 0 else low)
                item["estimate_max_cents"] += sign * (low if sign < 0 else high)
                item["estimated_entry_ids"].add(entry_id)
                total_estimated_entry_ids.add(entry_id)
            else:
                item["unknown_line_count"] += int(persisted_line)
                item["unknown_entry_ids"].add(entry_id)
                total_unknown_entry_ids.add(entry_id)

        consumption_actual_cents = 0
        consumption_estimate_min_cents = 0
        consumption_estimate_max_cents = 0
        consumption_unknown_entry_ids: set[str] = set()
        consumption_estimated_entry_ids: set[str] = set()
        consumption_meal_count = 0
        for entry in entries:
            entry_id = entry["id"]
            lines = payments.get(entry_id, [])
            if entry["record_kind"] == "milk_tea":
                if lines:
                    for line in lines:
                        add_payment("standalone_milk_tea", entry_id, line)
                else:
                    add_payment("standalone_milk_tea", entry_id, {
                        "kind": "milk_tea", "source": "unknown", "amount_cents": None,
                        "amount_min_cents": None, "amount_max_cents": None,
                    }, persisted_line=False)
            else:
                consumption_meal_count += 1
                meal_lines = [line for line in lines if line["scope"] == "meal"]
                if entry["meal_amount_manual"]:
                    add_payment("whole_meal", entry_id, {
                        "kind": "meal", "source": "actual" if entry["meal_amount_cents"] is not None else "unknown",
                        "amount_cents": entry["meal_amount_cents"], "amount_min_cents": None, "amount_max_cents": None,
                    }, persisted_line=False)
                elif meal_lines:
                    for line in meal_lines:
                        add_payment("whole_meal", entry_id, line)
                else:
                    add_payment("whole_meal", entry_id, {
                        "kind": "meal", "source": "unknown", "amount_cents": None,
                        "amount_min_cents": None, "amount_max_cents": None,
                    }, persisted_line=False)
                for line in lines:
                    if line["scope"] == "extra":
                        add_payment("extra", entry_id, line)

                cost = json.loads(entry["consumption_cost_json"])
                if cost["source"] == "actual":
                    consumption_actual_cents += cost["min_cents"]
                elif cost["source"] == "estimate":
                    consumption_estimate_min_cents += cost["min_cents"]
                    consumption_estimate_max_cents += cost["max_cents"]
                    consumption_estimated_entry_ids.add(entry_id)
                else:
                    consumption_unknown_entry_ids.add(entry_id)

        all_categories = list(payment_totals.values())
        actual_by_category = {name: payment_totals[name]["actual_cents"] for name in category_names}
        actual_by_category["total"] = sum(actual_by_category.values())
        estimate_by_category = {
            name: {"min_cents": payment_totals[name]["estimate_min_cents"], "max_cents": payment_totals[name]["estimate_max_cents"]}
            for name in category_names
        }
        estimate_by_category["total"] = {
            "min_cents": sum(item["estimate_min_cents"] for item in all_categories),
            "max_cents": sum(item["estimate_max_cents"] for item in all_categories),
        }
        unknown_records = {name: len(payment_totals[name]["unknown_entry_ids"]) for name in category_names}
        unknown_records["total"] = len(total_unknown_entry_ids)
        unknown_lines = {name: payment_totals[name]["unknown_line_count"] for name in category_names}
        unknown_lines["total"] = sum(unknown_lines.values())
        estimated_records = {name: len(payment_totals[name]["estimated_entry_ids"]) for name in category_names}
        estimated_records["total"] = len(total_estimated_entry_ids)
        total_complete = unknown_records["total"] == 0
        total_actual = actual_by_category["total"]
        total_estimate = estimate_by_category["total"]
        payment_total_status = (
            "no_data" if not entries else
            "incomplete" if not total_complete else
            "estimated" if estimated_records["total"] else "actual"
        )
        payment_total_range = (
            {"min_cents": total_actual + total_estimate["min_cents"], "max_cents": total_actual + total_estimate["max_cents"]}
            if payment_total_status in {"actual", "estimated"} else None
        )
        exact_actual = payment_total_status == "actual"
        consumption_total_status = (
            "no_data" if not consumption_meal_count else
            "incomplete" if consumption_unknown_entry_ids else
            "estimated" if consumption_estimated_entry_ids else "actual"
        )

        standalone_excluded = 0
        if modes is not None:
            tea_clauses = ["record_kind = 'milk_tea'"]
            tea_values: list[Any] = []
            if date_from is not None:
                tea_clauses.append("calendar_date >= ?")
                tea_values.append(date_from)
            if date_to is not None:
                tea_clauses.append("calendar_date <= ?")
                tea_values.append(date_to)
            standalone_excluded = connection.execute(
                "SELECT COUNT(*) FROM active_entries WHERE " + " AND ".join(tea_clauses), tea_values,
            ).fetchone()[0]

        return {
            "record_count": len(entries),
            "meal_count": sum(entry["record_kind"] == "meal" for entry in entries),
            "standalone_milk_tea_count": sum(entry["record_kind"] == "milk_tea" for entry in entries),
            "excluded_standalone_milk_tea_count": standalone_excluded,
            "payment": {
                "actual_cents": actual_by_category,
                "estimated_cents": estimate_by_category,
                "unknown_records": unknown_records,
                "unknown_lines": unknown_lines,
                "estimated_records": estimated_records,
                "total_cents": total_actual if exact_actual else None,
                "total_status": payment_total_status,
                "total_range_cents": payment_total_range,
            },
            "consumption_cost": {
                "actual_cents": consumption_actual_cents,
                "estimated_cents": {"min_cents": consumption_estimate_min_cents, "max_cents": consumption_estimate_max_cents},
                "unknown_records": len(consumption_unknown_entry_ids),
                "estimated_records": len(consumption_estimated_entry_ids),
                "total_cents": consumption_actual_cents if consumption_total_status == "actual" else None,
                "total_status": consumption_total_status,
                "total_range_cents": (
                    {"min_cents": consumption_actual_cents + consumption_estimate_min_cents,
                     "max_cents": consumption_actual_cents + consumption_estimate_max_cents}
                    if consumption_total_status in {"actual", "estimated"} else None
                ),
            },
        }

    def _spending_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"date_from", "date_to", "mode"})
        date_from, date_to = self._analytics_dates(payload.get("date_from"), payload.get("date_to"))
        mode = self._analytics_mode(payload.get("mode"))
        modes = (mode,) if mode else None
        with self.store.read() as connection:
            result = self._spending_summary_result(connection, date_from, date_to, modes)
        return {"date_from": date_from, "date_to": date_to, "mode": mode, **result}

    def _period_compare(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"first_date_from", "first_date_to", "second_date_from", "second_date_to", "mode"}
        self._reject_extra_fields(payload, allowed)
        first_from, first_to = self._analytics_dates(
            payload.get("first_date_from"), payload.get("first_date_to"),
            from_field="first_date_from", to_field="first_date_to", required=True,
        )
        second_from, second_to = self._analytics_dates(
            payload.get("second_date_from"), payload.get("second_date_to"),
            from_field="second_date_from", to_field="second_date_to", required=True,
        )
        mode = self._analytics_mode(payload.get("mode"))
        modes = (mode,) if mode else None
        with self.store.read() as connection:
            first = self._spending_summary_result(connection, first_from, first_to, modes)
            second = self._spending_summary_result(connection, second_from, second_to, modes)
        first_payment = first["payment"]
        second_payment = second["payment"]
        payment_comparable = first_payment["total_status"] == "actual" and second_payment["total_status"] == "actual"
        delta_range = None
        if first_payment["total_status"] in {"actual", "estimated"} and second_payment["total_status"] in {"actual", "estimated"}:
            delta_range = {
                "min_cents": second_payment["total_range_cents"]["min_cents"] - first_payment["total_range_cents"]["max_cents"],
                "max_cents": second_payment["total_range_cents"]["max_cents"] - first_payment["total_range_cents"]["min_cents"],
            }
        return {
            "mode": mode,
            "first": {"date_from": first_from, "date_to": first_to, **first},
            "second": {"date_from": second_from, "date_to": second_to, **second},
            "meal_count_delta": second["meal_count"] - first["meal_count"],
            "standalone_milk_tea_count_delta": second["standalone_milk_tea_count"] - first["standalone_milk_tea_count"],
            "payment_total_delta_cents": (
                second_payment["total_cents"] - first_payment["total_cents"] if payment_comparable else None
            ),
            "payment_delta_range_cents": delta_range,
            "payment_comparable": payment_comparable,
            "payment_comparison_status": (
                "actual" if payment_comparable else
                "incomplete" if first_payment["total_status"] == "incomplete" or second_payment["total_status"] == "incomplete" else
                "no_data" if first_payment["total_status"] == "no_data" or second_payment["total_status"] == "no_data" else "estimated"
            ),
        }

    @staticmethod
    def _spending_mode_scope(mode_scope: str) -> tuple[str, ...] | None:
        if mode_scope == "all":
            return None
        if mode_scope == "out_of_home":
            return ("eat_out", "delivery")
        return (mode_scope,)

    def _budget_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "period_type", "period_start", "mode_scope", "budget_cents", "expected_version"})
        request_id = self._string(payload.get("request_id"), "request_id")
        period_type = self._string(payload.get("period_type"), "period_type")
        if period_type not in {"week", "month"}:
            raise BusinessError("INVALID_INPUT", "period_type is unsupported", {"field": "period_type"})
        period_start, period_end = self._period_window(period_type, payload.get("period_start"))
        mode_scope = self._string(payload.get("mode_scope", "all"), "mode_scope")
        if mode_scope not in {"all", "out_of_home", *_MODES}:
            raise BusinessError("INVALID_INPUT", "mode_scope is unsupported", {"field": "mode_scope"})
        budget_cents = self._integer(payload.get("budget_cents"), "budget_cents", minimum=0)
        expected_version = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        now = self._now()
        budget_key = f"{period_type}:{period_start}:{mode_scope}"

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM period_budgets WHERE period_type = ? AND period_start = ? AND mode_scope = ?",
                (period_type, period_start, mode_scope),
            ).fetchone()
            if row is None:
                if expected_version is not None:
                    raise BusinessError("NOT_FOUND", "budget was not found for expected_version", {"budget": budget_key})
                connection.execute(
                    "INSERT INTO period_budgets(period_type, period_start, mode_scope, budget_cents, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (period_type, period_start, mode_scope, budget_cents, now, now),
                )
                return {"budget": {"period_type": period_type, "period_start": period_start, "period_end": period_end,
                                    "mode_scope": mode_scope, "budget_cents": budget_cents, "version": 1,
                                    "created_at": now, "updated_at": now}, "created": True, "changed": True}
            if expected_version is None:
                raise BusinessError("INVALID_INPUT", "expected_version is required when updating a budget", {"field": "expected_version"})
            self._check_version(row, budget_key, expected_version)
            changed = row["budget_cents"] != budget_cents
            version = expected_version + 1 if changed else expected_version
            updated_at = now if changed else row["updated_at"]
            if changed:
                connection.execute(
                    "UPDATE period_budgets SET budget_cents = ?, version = ?, updated_at = ? "
                    "WHERE period_type = ? AND period_start = ? AND mode_scope = ?",
                    (budget_cents, version, updated_at, period_type, period_start, mode_scope),
                )
            return {"budget": {"period_type": period_type, "period_start": period_start, "period_end": period_end,
                                "mode_scope": mode_scope, "budget_cents": budget_cents, "version": version,
                                "created_at": row["created_at"], "updated_at": updated_at}, "created": False, "changed": changed}

        return self._write("budget_set", payload, operation)

    def _budget_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"period_type", "period_start", "mode_scope"})
        period_type = self._string(payload.get("period_type"), "period_type")
        if period_type not in {"week", "month"}:
            raise BusinessError("INVALID_INPUT", "period_type is unsupported", {"field": "period_type"})
        period_start, period_end = self._period_window(period_type, payload.get("period_start"))
        mode_scope = self._string(payload.get("mode_scope", "all"), "mode_scope")
        if mode_scope not in {"all", "out_of_home", *_MODES}:
            raise BusinessError("INVALID_INPUT", "mode_scope is unsupported", {"field": "mode_scope"})
        modes = self._spending_mode_scope(mode_scope)
        with self.store.read() as connection:
            budget_row = connection.execute(
                "SELECT * FROM period_budgets WHERE period_type = ? AND period_start = ? AND mode_scope = ?",
                (period_type, period_start, mode_scope),
            ).fetchone()
            spending = self._spending_summary_result(connection, period_start, period_end, modes, include_standalone=modes is None)
        budget = dict(budget_row) if budget_row else None
        payment = spending["payment"]
        balance_status = "not_set"
        remaining_cents = None
        remaining_range_cents = None
        if budget is not None:
            if payment["total_status"] == "no_data":
                balance_status = "no_data"
            elif payment["total_status"] == "incomplete":
                balance_status = "incomplete"
            elif payment["total_status"] == "estimated":
                balance_status = "estimated"
                remaining_range_cents = {
                    "min_cents": budget["budget_cents"] - payment["total_range_cents"]["max_cents"],
                    "max_cents": budget["budget_cents"] - payment["total_range_cents"]["min_cents"],
                }
            else:
                balance_status = "known"
                remaining_cents = budget["budget_cents"] - payment["total_cents"]
                remaining_range_cents = {"min_cents": remaining_cents, "max_cents": remaining_cents}
        return {
            "period": {"period_type": period_type, "period_start": period_start, "period_end": period_end, "mode_scope": mode_scope},
            "budget": budget,
            "spending": spending,
            "balance": {"status": balance_status, "remaining_cents": remaining_cents, "remaining_range_cents": remaining_range_cents},
        }

    def _meal_tags_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "meal_id", "expected_version", "tags"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        raw_tags = self._list(payload.get("tags"), "tags")
        if len(raw_tags) > 50:
            raise BusinessError("INVALID_INPUT", "tags cannot contain more than 50 items", {"field": "tags"})
        tags: list[str] = []
        for index, raw in enumerate(raw_tags):
            tag = self._string(raw, f"tags[{index}]")
            if len(tag) > 100:
                raise BusinessError("INVALID_INPUT", "tag cannot exceed 100 characters", {"field": f"tags[{index}]"})
            if tag not in tags:
                tags.append(tag)
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            meal = connection.execute(
                "SELECT id, version FROM active_entries WHERE id = ? AND record_kind = 'meal'",
                (meal_id,),
            ).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(meal, meal_id, expected_version)
            current_rows = connection.execute(
                "SELECT tag, meal_version FROM meal_tags WHERE entry_id = ? ORDER BY tag COLLATE NOCASE, tag",
                (meal_id,),
            ).fetchall()
            current_tags = [row["tag"] for row in current_rows]
            unchanged = set(current_tags) == set(tags) and all(row["meal_version"] == expected_version for row in current_rows)
            if unchanged:
                version = expected_version
            else:
                version = expected_version + 1
                connection.execute("DELETE FROM meal_tags WHERE entry_id = ?", (meal_id,))
                connection.executemany(
                    "INSERT INTO meal_tags(entry_id, tag, meal_version, created_at) VALUES (?, ?, ?, ?)",
                    [(meal_id, tag, version, now) for tag in tags],
                )
                connection.execute("UPDATE entries SET version = ?, updated_at = ? WHERE id = ?", (version, now, meal_id))
            return {"meal_id": meal_id, "meal_version": version, "tags": tags, "changed": not unchanged}

        return self._write("meal_tags_set", payload, operation)

    def _meal_frequency(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"date_from", "date_to", "meal_type", "mode", "dish", "store_id", "weekdays", "tag", "top_limit"}
        self._reject_extra_fields(payload, allowed)
        date_from, date_to = self._analytics_dates(payload.get("date_from"), payload.get("date_to"))
        meal_type = self._analytics_meal_type(payload.get("meal_type"))
        mode = self._analytics_mode(payload.get("mode"))
        dish = self._string(payload.get("dish"), "dish") if payload.get("dish") is not None else None
        store_id = self._string(payload.get("store_id"), "store_id") if payload.get("store_id") is not None else None
        tag = self._string(payload.get("tag"), "tag") if payload.get("tag") is not None else None
        if tag is not None and len(tag) > 100:
            raise BusinessError("INVALID_INPUT", "tag cannot exceed 100 characters", {"field": "tag"})
        top_limit = self._integer(payload.get("top_limit", 10), "top_limit", minimum=1)
        if top_limit > 200:
            raise BusinessError("INVALID_INPUT", "top_limit cannot exceed 200", {"field": "top_limit"})
        weekdays: list[int] | None = None
        if payload.get("weekdays") is not None:
            raw_weekdays = self._list(payload["weekdays"], "weekdays")
            weekdays = sorted({self._integer(value, f"weekdays[{index}]", minimum=1) for index, value in enumerate(raw_weekdays)})
            if not weekdays or any(value > 7 for value in weekdays):
                raise BusinessError("INVALID_INPUT", "weekdays must contain ISO weekday numbers from 1 through 7", {"field": "weekdays"})

        clauses = ["e.record_kind = 'meal'"]
        values: list[Any] = []
        if date_from is not None:
            clauses.append("e.calendar_date >= ?")
            values.append(date_from)
        if date_to is not None:
            clauses.append("e.calendar_date <= ?")
            values.append(date_to)
        if meal_type is not None:
            clauses.append("e.meal_type = ?")
            values.append(meal_type)
        if mode is not None:
            clauses.append("e.mode = ?")
            values.append(mode)
        if dish is not None:
            clauses.append("EXISTS (SELECT 1 FROM items i WHERE i.entry_id = e.id AND i.name = ?)")
            values.append(dish)
        if tag is not None:
            clauses.append("EXISTS (SELECT 1 FROM meal_tags mt WHERE mt.entry_id = e.id AND mt.tag = ? AND mt.meal_version = e.version)")
            values.append(tag)
        if weekdays is not None:
            placeholders = ", ".join("?" for _ in weekdays)
            clauses.append(f"(((CAST(strftime('%w', e.calendar_date) AS INTEGER) + 6) % 7) + 1) IN ({placeholders})")
            values.extend(weekdays)
        where = " AND ".join(clauses)

        with self.store.read() as connection:
            if store_id is not None:
                requested = connection.execute("SELECT store_id FROM stores WHERE store_id = ?", (store_id,)).fetchone()
                if requested is None:
                    clauses.append("0")
                else:
                    canonical_id = self._canonical_store_id(connection, store_id)
                    clauses.append(
                        "EXISTS (SELECT 1 FROM active_entries e2 "
                        "LEFT JOIN meal_store_identity msi ON msi.entry_id = e2.id "
                        "LEFT JOIN meal_store_aliases msa ON msa.alias_store_id = COALESCE(msi.store_id, e2.store_id) "
                        "WHERE e2.id = e.id AND COALESCE(msa.canonical_store_id, msi.store_id, e2.store_id) = ?)"
                    )
                    values.append(canonical_id)
                where = " AND ".join(clauses)

            meal_count = connection.execute(f"SELECT COUNT(*) FROM active_entries e WHERE {where}", values).fetchone()[0]
            mode_counts = {key: 0 for key in _MODES}
            for row in connection.execute(
                f"SELECT e.mode, COUNT(*) AS meal_count FROM active_entries e WHERE {where} GROUP BY e.mode", values,
            ):
                mode_counts[row["mode"]] = row["meal_count"]
            meal_type_counts = {key: 0 for key in _MEAL_TYPES}
            for row in connection.execute(
                f"SELECT e.meal_type, COUNT(*) AS meal_count FROM active_entries e WHERE {where} GROUP BY e.meal_type", values,
            ):
                meal_type_counts[row["meal_type"]] = row["meal_count"]
            weekday_counts = {weekday: 0 for weekday in range(1, 8)}
            weekday_expr = "(((CAST(strftime('%w', e.calendar_date) AS INTEGER) + 6) % 7) + 1)"
            for row in connection.execute(
                f"SELECT {weekday_expr} AS weekday, COUNT(*) AS meal_count FROM active_entries e WHERE {where} GROUP BY weekday", values,
            ):
                weekday_counts[row["weekday"]] = row["meal_count"]

            dishes = connection.execute(
                "SELECT i.name, COUNT(DISTINCT e.id) AS meal_count FROM active_entries e "
                "JOIN items i ON i.entry_id = e.id WHERE " + where +
                " GROUP BY i.name ORDER BY meal_count DESC, i.name COLLATE NOCASE, i.name LIMIT ?",
                [*values, top_limit],
            ).fetchall()
            store_join = (
                " FROM active_entries e LEFT JOIN meal_store_identity msi ON msi.entry_id = e.id "
                "LEFT JOIN meal_store_aliases msa ON msa.alias_store_id = COALESCE(msi.store_id, e.store_id) "
                "LEFT JOIN stores s ON s.store_id = COALESCE(msa.canonical_store_id, msi.store_id, e.store_id) "
            )
            canonical_expr = "COALESCE(msa.canonical_store_id, msi.store_id, e.store_id)"
            stores = connection.execute(
                "SELECT " + canonical_expr + " AS store_id, s.name, s.branch, COUNT(DISTINCT e.id) AS meal_count " +
                store_join + " WHERE " + where + f" AND {canonical_expr} IS NOT NULL " +
                "GROUP BY 1, s.name, s.branch ORDER BY meal_count DESC, s.name COLLATE NOCASE, s.name, "
                "COALESCE(s.branch, '') COLLATE NOCASE, COALESCE(s.branch, '') LIMIT ?",
                [*values, top_limit],
            ).fetchall()
            unidentified_store_meal_count = connection.execute(
                "SELECT COUNT(DISTINCT e.id) " + store_join + " WHERE " + where +
                " AND COALESCE(msi.store_id, e.store_id) IS NULL", values,
            ).fetchone()[0]
            tags = connection.execute(
                "SELECT mt.tag, COUNT(DISTINCT e.id) AS meal_count FROM active_entries e "
                "JOIN meal_tags mt ON mt.entry_id = e.id AND mt.meal_version = e.version WHERE " + where +
                " GROUP BY mt.tag ORDER BY meal_count DESC, mt.tag COLLATE NOCASE, mt.tag LIMIT ?",
                [*values, top_limit],
            ).fetchall()

        return {
            "date_from": date_from,
            "date_to": date_to,
            "filters": {"meal_type": meal_type, "mode": mode, "dish": dish, "store_id": store_id, "weekdays": weekdays, "tag": tag},
            "meal_count": meal_count,
            "mode_counts": mode_counts,
            "meal_type_counts": meal_type_counts,
            "weekday_counts": [{"weekday": weekday, "meal_count": count} for weekday, count in weekday_counts.items()],
            "common_dishes": [{"name": row["name"], "meal_count": row["meal_count"]} for row in dishes],
            "common_stores": [{"store_id": row["store_id"], "name": row["name"], "branch": row["branch"], "meal_count": row["meal_count"]} for row in stores],
            "unidentified_store_meal_count": unidentified_store_meal_count,
            "tag_counts": [{"tag": row["tag"], "meal_count": row["meal_count"]} for row in tags],
            "top_limit": top_limit,
        }

    def _diet_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"date_from", "date_to", "tag", "mode", "meal_type"})
        date_from, date_to = self._analytics_dates(payload.get("date_from"), payload.get("date_to"))
        tag = self._string(payload.get("tag"), "tag") if payload.get("tag") is not None else None
        if tag is not None and len(tag) > 100:
            raise BusinessError("INVALID_INPUT", "tag cannot exceed 100 characters", {"field": "tag"})
        mode = self._analytics_mode(payload.get("mode"))
        meal_type = self._analytics_meal_type(payload.get("meal_type"))
        clauses = ["e.record_kind = 'meal'"]
        values: list[Any] = []
        if date_from is not None:
            clauses.append("e.calendar_date >= ?")
            values.append(date_from)
        if date_to is not None:
            clauses.append("e.calendar_date <= ?")
            values.append(date_to)
        if mode is not None:
            clauses.append("e.mode = ?")
            values.append(mode)
        if meal_type is not None:
            clauses.append("e.meal_type = ?")
            values.append(meal_type)
        where = " AND ".join(clauses)
        tag_clause = " AND mt.tag = ?" if tag is not None else ""
        tag_values = [tag] if tag is not None else []

        with self.store.read() as connection:
            total_meal_count = connection.execute(
                f"SELECT COUNT(*) FROM active_entries e WHERE {where}", values,
            ).fetchone()[0]
            tagged_meal_count = connection.execute(
                "SELECT COUNT(DISTINCT e.id) FROM active_entries e JOIN meal_tags mt "
                "ON mt.entry_id = e.id AND mt.meal_version = e.version WHERE " + where,
                values,
            ).fetchone()[0]
            matching_meal_count = connection.execute(
                "SELECT COUNT(DISTINCT e.id) FROM active_entries e JOIN meal_tags mt "
                "ON mt.entry_id = e.id AND mt.meal_version = e.version WHERE " + where + tag_clause,
                [*values, *tag_values],
            ).fetchone()[0]
            tag_rows = connection.execute(
                "SELECT mt.tag, COUNT(DISTINCT e.id) AS meal_count FROM active_entries e JOIN meal_tags mt "
                "ON mt.entry_id = e.id AND mt.meal_version = e.version WHERE " + where + tag_clause +
                " GROUP BY mt.tag ORDER BY meal_count DESC, mt.tag COLLATE NOCASE, mt.tag",
                [*values, *tag_values],
            ).fetchall()
            stale_tagged_meal_count = connection.execute(
                "SELECT COUNT(DISTINCT e.id) FROM active_entries e JOIN meal_tags mt ON mt.entry_id = e.id "
                "WHERE " + where + " AND mt.meal_version <> e.version" + (" AND mt.tag = ?" if tag is not None else ""),
                [*values, *tag_values],
            ).fetchone()[0]
            matched_where = where + " AND EXISTS (SELECT 1 FROM meal_tags mt WHERE mt.entry_id = e.id AND mt.meal_version = e.version" + (" AND mt.tag = ?" if tag is not None else "") + ")"
            matched_values = [*values, *tag_values]
            mode_counts = {key: 0 for key in _MODES}
            for row in connection.execute(
                f"SELECT e.mode, COUNT(*) AS meal_count FROM active_entries e WHERE {matched_where} GROUP BY e.mode",
                matched_values,
            ):
                mode_counts[row["mode"]] = row["meal_count"]
            meal_type_counts = {key: 0 for key in _MEAL_TYPES}
            for row in connection.execute(
                f"SELECT e.meal_type, COUNT(*) AS meal_count FROM active_entries e WHERE {matched_where} GROUP BY e.meal_type",
                matched_values,
            ):
                meal_type_counts[row["meal_type"]] = row["meal_count"]

        return {
            "date_from": date_from,
            "date_to": date_to,
            "filters": {"tag": tag, "mode": mode, "meal_type": meal_type},
            "total_meal_count": total_meal_count,
            "tagged_meal_count": tagged_meal_count,
            "meal_count": matching_meal_count,
            "untagged_meal_count": total_meal_count - tagged_meal_count,
            "stale_tagged_meal_count": stale_tagged_meal_count,
            "tag_counts": [{"tag": row["tag"], "meal_count": row["meal_count"]} for row in tag_rows],
            "mode_counts": mode_counts,
            "meal_type_counts": meal_type_counts,
        }


__all__ = ["HANDLERS", "MealAnalyticsMixin", "MIGRATION_SQL", "TOOL_SCHEMAS"]
