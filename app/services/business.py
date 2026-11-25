from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from app.storage import SQLiteStore
from .errors import BusinessError
from .meal_history import HANDLERS as HISTORY_HANDLERS, MealHistoryMixin
from .meal_analytics import HANDLERS as ANALYTICS_HANDLERS, MealAnalyticsMixin
from .recommendation_history import HANDLERS as RECOMMENDATION_HANDLERS, RecommendationHistoryMixin
from .kitchen_recipe import HANDLERS as RECIPE_HANDLERS, KitchenRecipeMixin, _normalize_quantity
from .kitchen import HANDLERS as KITCHEN_HANDLERS, KitchenMixin
from .daybook import HANDLERS as DAYBOOK_HANDLERS, DaybookMixin
from .pricebook import HANDLERS as PRICEBOOK_HANDLERS, PricebookMixin
from .reminders import HANDLERS as REMINDER_HANDLERS, RemindersMixin
from .recipe_providers import HANDLERS as RECIPE_PROVIDER_HANDLERS, RecipeProvidersMixin
from .recommendation_score import recommendation_score, score_weights
from .meal_options import HANDLERS as OPTION_HANDLERS, MealOptionsMixin
from .meal_corrections import AUDITED_ACTIONS, HANDLERS as CORRECTION_HANDLERS, MealCorrectionsMixin
from .meal_planning import HANDLERS as PLANNING_HANDLERS, MealPlanningMixin
from .kitchen_operations import HANDLERS as KITCHEN_OPERATION_HANDLERS, KitchenOperationsMixin
from .meal_finance import HANDLERS as FINANCE_HANDLERS, MealFinanceMixin
from .local_data import HANDLERS as DATA_HANDLERS, LocalDataMixin


MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack", "other"}
MODES = {"eat_out", "delivery", "home_cooking"}
PAYMENT_KINDS = {"meal", "delivery_fee", "packaging_fee", "discount", "milk_tea", "purchase", "other"}
PAYMENT_SCOPES = {"meal", "extra"}


class BusinessService(
    MealHistoryMixin,
    MealAnalyticsMixin,
    RecommendationHistoryMixin,
    KitchenRecipeMixin,
    KitchenMixin,
    DaybookMixin,
    PricebookMixin,
    RemindersMixin,
    RecipeProvidersMixin,
    MealOptionsMixin,
    MealCorrectionsMixin,
    MealPlanningMixin,
    KitchenOperationsMixin,
    MealFinanceMixin,
    LocalDataMixin,
):
    def __init__(self, db_path: str | Path) -> None:
        self.store = SQLiteStore(db_path)
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "meal_create": self._meal_create,
            "meal_list": self._meal_list,
            "meal_stats": self._meal_stats,
            "meal_update": self._meal_update,
            "payment_add": self._payment_add,
            "payment_remove": self._payment_remove,
            "milk_tea_create": self._milk_tea_create,
            "milk_tea_list": self._milk_tea_list,
            "milk_tea_update": self._milk_tea_update,
            "request_status": self._request_status,
            "candidate_upsert": self._candidate_upsert,
            "candidate_list": self._candidate_list,
            "recipe_upsert": self._recipe_upsert,
            "recipe_list": self._recipe_list,
            "evidence_create": self._evidence_create,
            "evidence_list": self._evidence_list,
            "preference_set": self._preference_set,
            "restriction_set": self._restriction_set,
            "restriction_list": self._restriction_list,
            "recommend": self._recommend,
        }
        for handlers in (
            HISTORY_HANDLERS,
            ANALYTICS_HANDLERS,
            RECOMMENDATION_HANDLERS,
            RECIPE_HANDLERS,
            KITCHEN_HANDLERS,
            DAYBOOK_HANDLERS,
            PRICEBOOK_HANDLERS,
            REMINDER_HANDLERS,
            RECIPE_PROVIDER_HANDLERS,
            OPTION_HANDLERS,
            CORRECTION_HANDLERS,
            PLANNING_HANDLERS,
            KITCHEN_OPERATION_HANDLERS,
            FINANCE_HANDLERS,
            DATA_HANDLERS,
        ):
            self._handlers.update({name: getattr(self, method_name) for name, method_name in handlers.items()})

    def dispatch(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action, str) or not action:
            return self._error("INVALID_ACTION", "action must be a non-empty string")
        if not isinstance(payload, dict):
            return self._error("INVALID_INPUT", "payload must be an object", {"field": "payload"})
        handler = self._handlers.get(action)
        if handler is None:
            return self._error("INVALID_ACTION", f"Unsupported action: {action}", {"action": action})
        try:
            return {"ok": True, "result": handler(payload)}
        except BusinessError as exc:
            return self._error(exc.code, exc.message, exc.details)
        except sqlite3.Error as exc:
            return self._error("STORAGE_ERROR", str(exc))

    @staticmethod
    def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}

    @staticmethod
    def _json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BusinessError("INVALID_INPUT", "payload must contain JSON-compatible values") from exc

    @staticmethod
    def _string(value: Any, field: str, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value.strip():
            raise BusinessError("INVALID_INPUT", f"{field} must be a non-empty string", {"field": field})
        return value.strip()

    @staticmethod
    def _integer(value: Any, field: str, *, nullable: bool = False, minimum: int | None = None) -> int | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessError("INVALID_INPUT", f"{field} must be an integer", {"field": field})
        if minimum is not None and value < minimum:
            raise BusinessError("INVALID_INPUT", f"{field} must be at least {minimum}", {"field": field})
        return value

    @staticmethod
    def _iso_date(value: Any, field: str) -> str:
        text = BusinessService._string(value, field)
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", f"{field} must be an ISO date", {"field": field}) from exc
        return text

    @staticmethod
    def _canonical_calendar_date(value: str) -> str:
        return date.fromisoformat(value).isoformat()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _dict(value: Any, field: str, *, nullable: bool = False) -> dict[str, Any]:
        if value is None and nullable:
            return {}
        if not isinstance(value, dict):
            raise BusinessError("INVALID_INPUT", f"{field} must be an object", {"field": field})
        return value

    @staticmethod
    def _list(value: Any, field: str, *, default: list | None = None) -> list:
        if value is None and default is not None:
            return default
        if not isinstance(value, list):
            raise BusinessError("INVALID_INPUT", f"{field} must be an array", {"field": field})
        return value

    def _write(self, action: str, payload: dict[str, Any], operation: Callable[[sqlite3.Connection], Any]) -> Any:
        request_id = self._string(payload.get("request_id"), "request_id")
        payload_text = self._json(payload)
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        with self.store.transaction() as connection:
            previous = connection.execute(
                "SELECT action, payload_sha256, result_json FROM request_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if previous:
                if previous["action"] != action or previous["payload_sha256"] != payload_hash:
                    raise BusinessError(
                        "IDEMPOTENCY_CONFLICT",
                        "request_id was already used for different content",
                        {"request_id": request_id},
                    )
                return json.loads(previous["result_json"])
            audited_id = None
            if action in AUDITED_ACTIONS:
                audited_id = payload.get("meal_id") or payload.get("drink_id")
                if isinstance(audited_id, str):
                    self._audit_meal(connection, audited_id, "baseline")
            result = operation(connection)
            if action in AUDITED_ACTIONS:
                self._audit_meal(connection, audited_id or result.get("id"), action)
            result_text = self._json(result)
            connection.execute(
                "INSERT INTO request_receipts(request_id, action, payload_sha256, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_id, action, payload_hash, result_text, self._now()),
            )
            return result

    def _validate_timing(self, value: Any) -> dict[str, Any]:
        timing = self._dict(value, "timing", nullable=True)
        allowed = {"start_at", "ready_at", "ended_at", "return_at", "duration_min", "duration_max"}
        if set(timing) - allowed:
            raise BusinessError("INVALID_INPUT", "timing contains unsupported fields", {"field": "timing"})
        result: dict[str, Any] = {}
        for key in ("start_at", "ready_at", "ended_at", "return_at"):
            if key in timing:
                text = self._string(timing[key], f"timing.{key}", nullable=True)
                if text is not None:
                    try:
                        datetime.fromisoformat(text.replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            time.fromisoformat(text)
                        except ValueError as exc:
                            raise BusinessError("INVALID_INPUT", f"timing.{key} must be an ISO date-time or time", {"field": f"timing.{key}"}) from exc
                result[key] = text
        for key in ("duration_min", "duration_max"):
            if key in timing:
                result[key] = self._integer(timing[key], f"timing.{key}", nullable=True, minimum=0)
        low, high = result.get("duration_min"), result.get("duration_max")
        if (low is None) != (high is None):
            raise BusinessError("INVALID_INPUT", "duration_min and duration_max must both be provided or both be null", {"field": "timing"})
        if low is not None and high is not None and low > high:
            raise BusinessError("INVALID_INPUT", "duration_min cannot exceed duration_max", {"field": "timing"})
        return result

    def _validate_consumption_cost(self, value: Any, field: str = "consumption_cost") -> dict[str, Any]:
        if value is None:
            return {"min_cents": None, "max_cents": None, "source": "unknown"}
        cost = self._dict(value, field)
        if set(cost) - {"min_cents", "max_cents", "source"}:
            raise BusinessError("INVALID_INPUT", f"{field} contains unsupported fields", {"field": field})
        minimum = self._integer(cost.get("min_cents"), f"{field}.min_cents", nullable=True, minimum=0)
        maximum = self._integer(cost.get("max_cents"), f"{field}.max_cents", nullable=True, minimum=0)
        if (minimum is None) != (maximum is None) or (minimum is not None and minimum > maximum):
            raise BusinessError("INVALID_INPUT", f"{field} must be unknown or a complete non-negative range", {"field": field})
        source = cost.get("source", "unknown" if minimum is None else None)
        if source not in {"actual", "estimate", "unknown"}:
            raise BusinessError("INVALID_INPUT", f"{field}.source is unsupported", {"field": f"{field}.source"})
        if (source == "actual" and (minimum is None or minimum != maximum)) or (source == "estimate" and minimum is None) or (source == "unknown" and minimum is not None):
            raise BusinessError("INVALID_INPUT", f"{field}.source does not match its range", {"field": f"{field}.source"})
        return {"min_cents": minimum, "max_cents": maximum, "source": source}

    def _validate_items(self, value: Any) -> list[dict[str, Any]]:
        items = self._list(value, "items", default=[])
        result = []
        for index, item in enumerate(items):
            item = self._dict(item, f"items[{index}]")
            if set(item) - {"name", "quantity", "unit", "notes"}:
                raise BusinessError("INVALID_INPUT", "item contains unsupported fields", {"field": f"items[{index}]"})
            name = self._string(item.get("name"), f"items[{index}].name")
            quantity = item.get("quantity")
            if quantity is not None and not isinstance(quantity, (str, int, float)):
                raise BusinessError("INVALID_INPUT", "item quantity must be a number, string, or null", {"field": f"items[{index}].quantity"})
            if isinstance(quantity, float) and not math.isfinite(quantity):
                raise BusinessError("INVALID_INPUT", "item quantity must be finite", {"field": f"items[{index}].quantity"})
            unit = self._string(item.get("unit"), f"items[{index}].unit", nullable=True)
            notes = self._string(item.get("notes"), f"items[{index}].notes", nullable=True)
            result.append({"name": name, "quantity": None if quantity is None else str(quantity), "unit": unit, "notes": notes})
        return result

    def _validate_payment(self, value: Any, field: str = "payment") -> dict[str, Any]:
        payment = self._dict(value, field)
        if set(payment) - {"kind", "scope", "amount_cents", "amount_min_cents", "amount_max_cents", "source", "note"}:
            raise BusinessError("INVALID_INPUT", f"{field} contains unsupported fields", {"field": field})
        kind = self._string(payment.get("kind"), f"{field}.kind")
        scope = self._string(payment.get("scope"), f"{field}.scope")
        if kind not in PAYMENT_KINDS:
            raise BusinessError("INVALID_INPUT", f"{field}.kind is unsupported", {"field": f"{field}.kind"})
        if scope not in PAYMENT_SCOPES:
            raise BusinessError("INVALID_INPUT", f"{field}.scope is unsupported", {"field": f"{field}.scope"})
        exact = self._integer(payment.get("amount_cents"), f"{field}.amount_cents", nullable=True, minimum=0)
        minimum = self._integer(payment.get("amount_min_cents"), f"{field}.amount_min_cents", nullable=True, minimum=0)
        maximum = self._integer(payment.get("amount_max_cents"), f"{field}.amount_max_cents", nullable=True, minimum=0)
        if (minimum is None) != (maximum is None) or (exact is not None and minimum is not None):
            raise BusinessError("INVALID_INPUT", f"{field} must use one exact amount, one complete range, or unknown", {"field": field})
        if minimum is not None and minimum > maximum:
            raise BusinessError("INVALID_INPUT", f"{field}.amount_min_cents cannot exceed amount_max_cents", {"field": field})
        source = payment.get("source")
        if source is None:
            source = "actual" if exact is not None else ("estimate" if minimum is not None else "unknown")
        if source not in {"actual", "estimate", "unknown"}:
            raise BusinessError("INVALID_INPUT", f"{field}.source is unsupported", {"field": f"{field}.source"})
        if (source == "actual" and exact is None) or (source == "estimate" and exact is None and minimum is None) or (source == "unknown" and (exact is not None or minimum is not None)):
            raise BusinessError("INVALID_INPUT", f"{field}.source does not match its amount fields", {"field": f"{field}.source"})
        note = self._string(payment.get("note"), f"{field}.note", nullable=True)
        return {
            "kind": kind,
            "scope": scope,
            "amount_cents": exact,
            "amount_min_cents": minimum,
            "amount_max_cents": maximum,
            "source": source,
            "note": note,
        }

    def _insert_items(self, connection: sqlite3.Connection, entry_id: str, items: list[dict[str, Any]]) -> None:
        start = connection.execute(
            "SELECT COALESCE(MAX(COALESCE(display_order,rowid)),0) FROM items WHERE entry_id=?", (entry_id,),
        ).fetchone()[0]
        connection.executemany(
            "INSERT INTO items(item_id, entry_id, name, quantity, unit, notes, display_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(uuid.uuid4().hex, entry_id, row["name"], row["quantity"], row["unit"], row["notes"], start + index + 1)
             for index, row in enumerate(items)],
        )

    def _insert_payments(self, connection: sqlite3.Connection, entry_id: str, payments: list[dict[str, Any]]) -> None:
        connection.executemany(
            "INSERT INTO payments(payment_id, entry_id, kind, scope, amount_cents, amount_min_cents, amount_max_cents, source, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    uuid.uuid4().hex, entry_id, payment["kind"], payment["scope"], payment["amount_cents"],
                    payment["amount_min_cents"], payment["amount_max_cents"], payment["source"], payment["note"],
                )
                for payment in payments
            ],
        )

    @staticmethod
    def _derive_meal_amount(connection: sqlite3.Connection, entry_id: str) -> int | None:
        rows = connection.execute(
            "SELECT kind, amount_cents, amount_min_cents FROM payments WHERE entry_id = ? AND scope = 'meal'",
            (entry_id,),
        ).fetchall()
        if not rows or any(row["amount_cents"] is None or row["amount_min_cents"] is not None for row in rows):
            return None
        return sum(-row["amount_cents"] if row["kind"] == "discount" else row["amount_cents"] for row in rows)

    def _insert_entry(
        self,
        connection: sqlite3.Connection,
        *,
        record_kind: str,
        entry_date: str,
        meal_type: str | None,
        mode: str | None,
        meal_amount_cents: int | None,
        meal_amount_manual: bool,
        location: str | None,
        note: str | None,
        timing: dict[str, Any],
        people_count: int | None,
        consumption_cost: dict[str, Any],
        store_id: str | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex
        now = self._now()
        calendar_date = self._canonical_calendar_date(entry_date)
        connection.execute(
            "INSERT INTO entries(id, record_kind, entry_date, calendar_date, meal_type, mode, meal_amount_cents, meal_amount_manual, "
            "location, note, timing_json, people_count, consumption_cost_json, store_id, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                entry_id, record_kind, entry_date, calendar_date, meal_type, mode, meal_amount_cents, int(meal_amount_manual),
                location, note, self._json(timing), people_count, self._json(consumption_cost), store_id, now, now,
            ),
        )
        if record_kind == "meal" and store_id is not None:
            connection.execute(
                "INSERT INTO meal_store_identity(entry_id, store_id) VALUES (?, ?)",
                (entry_id, store_id),
            )
        return entry_id

    def _store_fields(self, value: Any, field: str) -> dict[str, str | None] | None:
        if value is None:
            return None
        store = self._dict(value, field)
        if set(store) - {"name", "branch"}:
            raise BusinessError("INVALID_INPUT", f"{field} contains unsupported fields", {"field": field})
        return {
            "name": self._string(store.get("name"), f"{field}.name"),
            "branch": self._string(store.get("branch"), f"{field}.branch", nullable=True),
        }

    def _store_id(self, connection: sqlite3.Connection, value: Any, field: str) -> str | None:
        store = self._store_fields(value, field)
        if store is None:
            return None
        row = connection.execute(
            "SELECT store_id FROM stores WHERE name = ? AND branch IS ?",
            (store["name"], store["branch"]),
        ).fetchone()
        if row:
            return self._canonical_store_id(connection, row["store_id"])
        store_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO stores(store_id, name, branch) VALUES (?, ?, ?)",
            (store_id, store["name"], store["branch"]),
        )
        return self._canonical_store_id(connection, store_id)

    def _store_id_input(
        self,
        connection: sqlite3.Connection,
        store_value: Any,
        stable_store_id: Any,
        field: str,
    ) -> str | None:
        if stable_store_id is not None:
            requested_id = self._string(stable_store_id, f"{field}.store_id")
            row = connection.execute("SELECT store_id FROM stores WHERE store_id = ?", (requested_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "store was not found", {"store_id": requested_id})
            return self._canonical_store_id(connection, row["store_id"])
        return self._store_id(connection, store_value, f"{field}.store")

    def _entry(self, connection: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "record was not found", {"id": entry_id})
        items = connection.execute(
            """SELECT i.item_id,i.name,i.quantity,i.unit,i.notes,p.dish_id,p.rating,p.order_note
               FROM items i LEFT JOIN meal_item_profiles p USING(item_id) WHERE i.entry_id=?
               ORDER BY COALESCE(i.display_order,i.rowid),i.rowid""", (entry_id,),
        ).fetchall()
        payments = connection.execute(
            "SELECT payment_id, kind, scope, amount_cents, amount_min_cents, amount_max_cents, source, note "
            "FROM payments WHERE entry_id = ? ORDER BY rowid",
            (entry_id,),
        ).fetchall()
        store = None
        if row["store_id"] is not None:
            store = dict(connection.execute(
                "SELECT store_id, name, branch FROM stores WHERE store_id = ?",
                (row["store_id"],),
            ).fetchone())
        return {
            "id": row["id"],
            "version": row["version"],
            "expected_version": row["version"],
            "record_kind": row["record_kind"],
            "date": row["entry_date"],
            "meal_type": row["meal_type"],
            "mode": row["mode"],
            "meal_amount_cents": row["meal_amount_cents"],
            "items": [dict(item) for item in items],
            "payments": [dict(payment) for payment in payments],
            "timing": json.loads(row["timing_json"]),
            "consumption_cost": json.loads(row["consumption_cost_json"]),
            "location": row["location"],
            "store": store,
            "note": row["note"],
            "people_count": row["people_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _validate_meal_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry_date = self._iso_date(payload.get("date"), "date")
        meal_type = self._string(payload.get("meal_type"), "meal_type")
        if meal_type not in MEAL_TYPES:
            raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
        mode = self._string(payload.get("mode"), "mode")
        if mode not in MODES:
            raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
        items = self._validate_items(payload.get("items", []))
        payments = [self._validate_payment(row, f"payments[{index}]") for index, row in enumerate(self._list(payload.get("payments", []), "payments"))]
        timing = self._validate_timing(payload.get("timing", {}))
        consumption_cost = self._validate_consumption_cost(payload.get("consumption_cost"))
        location = self._string(payload.get("location"), "location", nullable=True)
        note = self._string(payload.get("note"), "note", nullable=True)
        people_count = self._integer(payload.get("people_count"), "people_count", nullable=True, minimum=1)
        manual_amount = "meal_amount_cents" in payload
        meal_amount = self._integer(payload.get("meal_amount_cents"), "meal_amount_cents", nullable=True, minimum=0)
        if "store" in payload and "store_id" in payload:
            raise BusinessError("INVALID_INPUT", "provide store or store_id, not both", {"field": "store"})
        store = self._store_fields(payload.get("store"), "store") if "store" in payload else None
        store_id = self._string(payload.get("store_id"), "store_id", nullable=True) if "store_id" in payload else None
        return {
            "date": entry_date,
            "meal_type": meal_type,
            "mode": mode,
            "items": items,
            "payments": payments,
            "timing": timing,
            "consumption_cost": consumption_cost,
            "location": location,
            "note": note,
            "people_count": people_count,
            "manual_amount": manual_amount,
            "meal_amount": meal_amount,
            "store": store,
            "store_id": store_id,
        }

    def _meal_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        meal = self._validate_meal_payload(payload)
        source = self._meal_recommendation_source(payload)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            entry_id = self._insert_entry(
                connection,
                record_kind="meal",
                entry_date=meal["date"],
                meal_type=meal["meal_type"],
                mode=meal["mode"],
                meal_amount_cents=meal["meal_amount"],
                meal_amount_manual=meal["manual_amount"],
                location=meal["location"],
                note=meal["note"],
                timing=meal["timing"],
                people_count=meal["people_count"],
                consumption_cost=meal["consumption_cost"],
                store_id=self._store_id_input(connection, meal["store"], meal["store_id"], "store"),
            )
            self._insert_items(connection, entry_id, meal["items"])
            self._insert_payments(connection, entry_id, meal["payments"])
            if not meal["manual_amount"]:
                derived = self._derive_meal_amount(connection, entry_id)
                connection.execute("UPDATE entries SET meal_amount_cents = ? WHERE id = ?", (derived, entry_id))
            if source is not None:
                source_type, source_id = source
                self._link_meal_recommendation(connection, entry_id, source_type, source_id, meal["mode"], None)
            return self._entry(connection, entry_id)

        return self._write("meal_create", payload, operation)

    def _meal_recommendation_source(self, payload: dict[str, Any]) -> tuple[str, str] | None:
        if "recommendation_source" not in payload:
            return None
        source = self._dict(payload["recommendation_source"], "recommendation_source")
        self._reject_extra_fields(source, {"source_type", "source_id"})
        source_type = self._recommendation_enum(source.get("source_type"), "source_type", {"candidate", "recipe"})
        source_id = self._string(source.get("source_id"), "source_id")
        return source_type, source_id

    def _page(self, payload: dict[str, Any]) -> tuple[int, int]:
        limit = self._integer(payload.get("limit", 50), "limit", minimum=1)
        offset = self._integer(payload.get("offset", 0), "offset", minimum=0)
        if limit > 200:
            raise BusinessError("INVALID_INPUT", "limit cannot exceed 200", {"field": "limit"})
        return limit, offset

    def _list_entries(self, payload: dict[str, Any], record_kind: str) -> dict[str, Any]:
        date_from = self._canonical_calendar_date(self._iso_date(payload["date_from"], "date_from")) if payload.get("date_from") is not None else None
        date_to = self._canonical_calendar_date(self._iso_date(payload["date_to"], "date_to")) if payload.get("date_to") is not None else None
        if date_from and date_to and date_from > date_to:
            raise BusinessError("INVALID_INPUT", "date_from cannot be after date_to", {"field": "date_from"})
        limit, offset = self._page(payload)
        clauses = ["record_kind = ?"]
        values: list[Any] = [record_kind]
        if date_from:
            clauses.append("calendar_date >= ?")
            values.append(date_from)
        if date_to:
            clauses.append("calendar_date <= ?")
            values.append(date_to)
        if record_kind == "meal":
            mode = payload.get("mode")
            meal_type = payload.get("meal_type")
            if mode is not None:
                if mode not in MODES:
                    raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
                clauses.append("mode = ?")
                values.append(mode)
            if meal_type is not None:
                if meal_type not in MEAL_TYPES:
                    raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
                clauses.append("meal_type = ?")
                values.append(meal_type)
        where = " AND ".join(clauses)
        with self.store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM active_entries WHERE {where}", values).fetchone()[0]
            ids = connection.execute(
                f"SELECT id FROM active_entries WHERE {where} ORDER BY calendar_date DESC, created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            items = [self._entry(connection, row["id"]) for row in ids]
        for item in items:
            item["date"] = self._canonical_calendar_date(item["date"])
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _meal_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._list_entries(payload, "meal")

    def _milk_tea_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._list_entries(payload, "milk_tea")

    @staticmethod
    def _meal_where(
        date_from: str | None,
        date_to: str | None,
        *,
        mode: str | None = None,
        dish: str | None = None,
        store_id: str | None = None,
    ) -> tuple[str, list[Any]]:
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
        if dish is not None:
            clauses.append("EXISTS (SELECT 1 FROM items i WHERE i.entry_id = e.id AND i.name = ?)")
            values.append(dish)
        if store_id is not None:
            clauses.append("EXISTS (SELECT 1 FROM meal_store_identity si WHERE si.entry_id = e.id AND si.store_id = ?)")
            values.append(store_id)
        return " AND ".join(clauses), values

    @staticmethod
    def _latest_meal(
        connection: sqlite3.Connection,
        date_from: str | None,
        date_to: str | None,
        *,
        mode: str | None = None,
        dish: str | None = None,
        store_id: str | None = None,
    ) -> dict[str, str | None] | None:
        where, values = BusinessService._meal_where(
            date_from, date_to, mode=mode, dish=dish, store_id=store_id,
        )
        latest_date = connection.execute(
            f"SELECT MAX(e.calendar_date) FROM active_entries e WHERE {where}", values,
        ).fetchone()[0]
        if latest_date is None:
            return None
        rows = connection.execute(
            f"SELECT e.timing_json FROM active_entries e WHERE {where} AND e.calendar_date = ? "
            "ORDER BY e.created_at DESC, e.id DESC",
            [*values, latest_date],
        ).fetchall()
        raw_values = [json.loads(row["timing_json"]).get("ended_at") for row in rows]
        ended_at = None
        if raw_values and all(isinstance(value, str) and value for value in raw_values):
            parsed: list[tuple[str, datetime | time]] = []
            for value in raw_values:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    pass
                else:
                    parsed = []
                    break
                try:
                    parsed.append(("datetime", datetime.fromisoformat(value.replace("Z", "+00:00"))))
                except ValueError:
                    try:
                        parsed.append(("time", time.fromisoformat(value)))
                    except ValueError:
                        parsed = []
                        break
            kinds = {kind for kind, _ in parsed}
            if len(parsed) == len(raw_values) and len(kinds) == 1:
                kind = parsed[0][0]
                values_to_compare = [value for _, value in parsed]
                if kind == "datetime":
                    aware = [value.tzinfo is not None for value in values_to_compare]
                    if all(aware):
                        values_to_compare = [value.astimezone(timezone.utc) for value in values_to_compare]
                    elif any(aware):
                        values_to_compare = []
                else:
                    offsets = [value.utcoffset() for value in values_to_compare]
                    if len(set(offsets)) != 1:
                        values_to_compare = []
                    else:
                        values_to_compare = [
                            (value.hour, value.minute, value.second, value.microsecond)
                            for value in values_to_compare
                        ]
                if values_to_compare:
                    latest_index = max(range(len(values_to_compare)), key=values_to_compare.__getitem__)
                    ended_at = raw_values[latest_index]
        return {"date": latest_date, "ended_at": ended_at}

    def _meal_stats(self, payload: dict[str, Any]) -> dict[str, Any]:
        date_from = date.fromisoformat(self._iso_date(payload["date_from"], "date_from")).isoformat() if payload.get("date_from") is not None else None
        date_to = date.fromisoformat(self._iso_date(payload["date_to"], "date_to")).isoformat() if payload.get("date_to") is not None else None
        if date_from is not None and date_to is not None and date_from > date_to:
            raise BusinessError("INVALID_INPUT", "date_from cannot be after date_to", {"field": "date_from"})
        dish_filter = self._string(payload.get("dish"), "dish") if "dish" in payload else None
        store_filter = self._store_fields(payload.get("store"), "store") if "store" in payload else None
        top_limit = self._integer(payload.get("top_limit", 10), "top_limit", minimum=1)
        if top_limit > 200:
            raise BusinessError("INVALID_INPUT", "top_limit cannot exceed 200", {"field": "top_limit"})

        where, values = self._meal_where(date_from, date_to)
        with self.store.read() as connection:
            meal_count = connection.execute(
                f"SELECT COUNT(*) FROM active_entries e WHERE {where}", values,
            ).fetchone()[0]
            mode_counts = {mode: 0 for mode in ("eat_out", "delivery", "home_cooking")}
            for row in connection.execute(
                f"SELECT e.mode, COUNT(*) AS meal_count FROM active_entries e WHERE {where} GROUP BY e.mode",
                values,
            ):
                mode_counts[row["mode"]] = row["meal_count"]

            dishes = connection.execute(
                "SELECT i.name, COUNT(DISTINCT e.id) AS meal_count "
                f"FROM active_entries e JOIN items i ON i.entry_id = e.id WHERE {where} "
                "GROUP BY i.name ORDER BY meal_count DESC, i.name COLLATE NOCASE, i.name LIMIT ?",
                [*values, top_limit],
            ).fetchall()
            common_dishes = [
                {
                    "name": row["name"],
                    "meal_count": row["meal_count"],
                    "last_meal": self._latest_meal(connection, date_from, date_to, dish=row["name"]),
                }
                for row in dishes
            ]

            stores = connection.execute(
                "SELECT s.store_id, s.name, s.branch, COUNT(DISTINCT e.id) AS meal_count "
                f"FROM active_entries e JOIN meal_store_identity si ON si.entry_id = e.id "
                "JOIN stores s ON s.store_id = si.store_id WHERE "
                + where + " "
                "GROUP BY s.store_id, s.name, s.branch "
                "ORDER BY meal_count DESC, s.name COLLATE NOCASE, s.name, "
                "COALESCE(s.branch, '') COLLATE NOCASE, COALESCE(s.branch, '') LIMIT ?",
                [*values, top_limit],
            ).fetchall()
            common_stores = [
                {
                    "store_id": row["store_id"],
                    "name": row["name"],
                    "branch": row["branch"],
                    "meal_count": row["meal_count"],
                    "last_meal": self._latest_meal(connection, date_from, date_to, store_id=row["store_id"]),
                }
                for row in stores
            ]

            tea_clauses = ["record_kind = 'milk_tea'"]
            tea_values: list[Any] = []
            payment_clauses = ["e.record_kind = 'meal'", "p.kind = 'milk_tea'"]
            payment_values: list[Any] = []
            if date_from is not None:
                tea_clauses.append("calendar_date >= ?")
                tea_values.append(date_from)
                payment_clauses.append("e.calendar_date >= ?")
                payment_values.append(date_from)
            if date_to is not None:
                tea_clauses.append("calendar_date <= ?")
                tea_values.append(date_to)
                payment_clauses.append("e.calendar_date <= ?")
                payment_values.append(date_to)
            standalone_milk_tea = connection.execute(
                f"SELECT COUNT(*) FROM active_entries WHERE {' AND '.join(tea_clauses)}", tea_values,
            ).fetchone()[0]
            attached_milk_tea = connection.execute(
                "SELECT COUNT(*) FROM payments p JOIN active_entries e ON e.id = p.entry_id WHERE "
                + " AND ".join(payment_clauses),
                payment_values,
            ).fetchone()[0]

            result: dict[str, Any] = {
                "date_from": date_from,
                "date_to": date_to,
                "meal_count": meal_count,
                "mode_counts": mode_counts,
                "common_dishes": common_dishes,
                "common_stores": common_stores,
                "last_meal": self._latest_meal(connection, date_from, date_to),
                "milk_tea_purchases": {
                    "count": standalone_milk_tea + attached_milk_tea,
                    "standalone_records": standalone_milk_tea,
                    "meal_payment_lines": attached_milk_tea,
                },
                "top_limit": top_limit,
            }
            if dish_filter is not None:
                dish_where, dish_values = self._meal_where(date_from, date_to, dish=dish_filter)
                result["selected_dish"] = {
                    "name": dish_filter,
                    "meal_count": connection.execute(
                        f"SELECT COUNT(*) FROM active_entries e WHERE {dish_where}", dish_values,
                    ).fetchone()[0],
                    "last_meal": self._latest_meal(connection, date_from, date_to, dish=dish_filter),
                }
            if store_filter is not None:
                store_row = connection.execute(
                    "SELECT store_id FROM stores WHERE name = ? AND branch IS ?",
                    (store_filter["name"], store_filter["branch"]),
                ).fetchone()
                store_id = self._canonical_store_id(connection, store_row["store_id"]) if store_row else None
                if store_id is None:
                    selected_count = 0
                    selected_latest = None
                else:
                    store_where, store_values = self._meal_where(date_from, date_to, store_id=store_id)
                    selected_count = connection.execute(
                        f"SELECT COUNT(*) FROM active_entries e WHERE {store_where}", store_values,
                    ).fetchone()[0]
                    selected_latest = self._latest_meal(connection, date_from, date_to, store_id=store_id)
                result["selected_store"] = {
                    "store_id": store_id,
                    "name": store_filter["name"],
                    "branch": store_filter["branch"],
                    "meal_count": selected_count,
                    "last_meal": selected_latest,
                }
        return result

    def _meal_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        scope = self._string(payload.get("scope"), "scope")
        changes = self._dict(payload.get("changes"), "changes")
        if not changes:
            raise BusinessError("INVALID_INPUT", "changes cannot be empty", {"field": "changes"})
        now = self._now()
        if scope == "meal" and set(payload) - {"request_id", "meal_id", "expected_version", "scope", "changes"}:
            raise BusinessError("INVALID_INPUT", "meal update contains unsupported fields", {"field": "payload"})
        if scope == "payment" and set(payload) - {"request_id", "meal_id", "expected_version", "scope", "payment_id", "changes"}:
            raise BusinessError("INVALID_INPUT", "payment update contains unsupported fields", {"field": "payload"})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM active_entries WHERE id = ? AND record_kind = 'meal'", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(row, meal_id, expected_version)
            if scope == "meal":
                allowed = {"date", "meal_type", "mode", "meal_amount_cents", "items", "location", "note", "timing", "people_count", "consumption_cost", "store", "store_id"}
                if set(changes) - allowed:
                    raise BusinessError("INVALID_INPUT", "meal changes contain unsupported fields", {"field": "changes"})
                if "store" in changes and "store_id" in changes:
                    raise BusinessError("INVALID_INPUT", "provide store or store_id, not both", {"field": "changes.store"})
                columns: dict[str, Any] = {}
                if "date" in changes:
                    entry_date = self._iso_date(changes["date"], "changes.date")
                    columns["entry_date"] = entry_date
                    columns["calendar_date"] = self._canonical_calendar_date(entry_date)
                if "meal_type" in changes:
                    if changes["meal_type"] not in MEAL_TYPES:
                        raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "changes.meal_type"})
                    columns["meal_type"] = changes["meal_type"]
                if "mode" in changes:
                    if changes["mode"] not in MODES:
                        raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "changes.mode"})
                    columns["mode"] = changes["mode"]
                if "meal_amount_cents" in changes:
                    columns["meal_amount_cents"] = self._integer(changes["meal_amount_cents"], "changes.meal_amount_cents", nullable=True, minimum=0)
                    columns["meal_amount_manual"] = 1
                if "location" in changes:
                    columns["location"] = self._string(changes["location"], "changes.location", nullable=True)
                if "note" in changes:
                    columns["note"] = self._string(changes["note"], "changes.note", nullable=True)
                if "timing" in changes:
                    columns["timing_json"] = self._json(self._validate_timing(changes["timing"]))
                if "people_count" in changes:
                    columns["people_count"] = self._integer(changes["people_count"], "changes.people_count", nullable=True, minimum=1)
                if "consumption_cost" in changes:
                    columns["consumption_cost_json"] = self._json(self._validate_consumption_cost(changes["consumption_cost"], "changes.consumption_cost"))
                if "store" in changes or "store_id" in changes:
                    columns["store_id"] = self._store_id_input(
                        connection, changes.get("store"), changes.get("store_id"), "changes",
                    )
                if "items" in changes:
                    self._replace_meal_items(connection, meal_id, changes["items"])
                columns["version"] = expected_version + 1
                columns["updated_at"] = now
                setters = ", ".join(f"{column} = ?" for column in columns)
                connection.execute(f"UPDATE entries SET {setters} WHERE id = ?", [*columns.values(), meal_id])
                if "store_id" in columns:
                    if columns["store_id"] is None:
                        connection.execute("DELETE FROM meal_store_identity WHERE entry_id = ?", (meal_id,))
                    else:
                        connection.execute(
                            "INSERT INTO meal_store_identity(entry_id, store_id) VALUES (?, ?) "
                            "ON CONFLICT(entry_id) DO UPDATE SET store_id = excluded.store_id",
                            (meal_id, columns["store_id"]),
                        )
            elif scope == "payment":
                payment_id = self._string(payload.get("payment_id"), "payment_id")
                allowed = {"kind", "scope", "amount_cents", "amount_min_cents", "amount_max_cents", "source", "note"}
                if set(changes) - allowed:
                    raise BusinessError("INVALID_INPUT", "payment changes contain unsupported fields", {"field": "changes"})
                payment_row = connection.execute("SELECT * FROM payments WHERE payment_id = ? AND entry_id = ?", (payment_id, meal_id)).fetchone()
                if payment_row is None:
                    raise BusinessError("NOT_FOUND", "payment line was not found", {"id": payment_id, "meal_id": meal_id})
                if set(changes) - {"note"} and self._payment_is_linked(connection, payment_id):
                    raise BusinessError("PAYMENT_LINKED", "This payment has linked refunds or allocations", {"payment_id": payment_id})
                amount_fields = {"amount_cents", "amount_min_cents", "amount_max_cents"}
                if amount_fields & set(changes):
                    amount_cents = changes.get("amount_cents") if "amount_cents" in changes else None
                    amount_min_cents = changes.get("amount_min_cents") if "amount_min_cents" in changes else None
                    amount_max_cents = changes.get("amount_max_cents") if "amount_max_cents" in changes else None
                    source = changes.get("source")
                else:
                    amount_cents = payment_row["amount_cents"]
                    amount_min_cents = payment_row["amount_min_cents"]
                    amount_max_cents = payment_row["amount_max_cents"]
                    source = changes.get("source", payment_row["source"])
                payment = {
                    "kind": changes.get("kind", payment_row["kind"]),
                    "scope": changes.get("scope", payment_row["scope"]),
                    "amount_cents": amount_cents,
                    "amount_min_cents": amount_min_cents,
                    "amount_max_cents": amount_max_cents,
                    "source": source,
                    "note": changes.get("note", payment_row["note"]),
                }
                payment = self._validate_payment(payment, "changes")
                connection.execute(
                    "UPDATE payments SET kind = ?, scope = ?, amount_cents = ?, amount_min_cents = ?, "
                    "amount_max_cents = ?, source = ?, note = ? WHERE payment_id = ?",
                    (payment["kind"], payment["scope"], payment["amount_cents"], payment["amount_min_cents"], payment["amount_max_cents"], payment["source"], payment["note"], payment_id),
                )
                if not row["meal_amount_manual"]:
                    connection.execute("UPDATE entries SET meal_amount_cents = ? WHERE id = ?", (self._derive_meal_amount(connection, meal_id), meal_id))
                connection.execute("UPDATE entries SET version = ?, updated_at = ? WHERE id = ?", (expected_version + 1, now, meal_id))
            else:
                raise BusinessError("INVALID_INPUT", "scope must be meal or payment", {"field": "scope"})
            return self._entry(connection, meal_id)

        return self._write("meal_update", payload, operation)

    @staticmethod
    def _check_version(row: sqlite3.Row, record_id: str, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise BusinessError(
                "VERSION_CONFLICT",
                "record changed since it was read",
                {"id": record_id, "expected_version": expected_version, "actual_version": row["version"]},
            )

    def _payment_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        payment = self._validate_payment(payload.get("payment"))
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM active_entries WHERE id = ? AND record_kind = 'meal'", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(row, meal_id, expected_version)
            self._insert_payments(connection, meal_id, [payment])
            if not row["meal_amount_manual"]:
                connection.execute("UPDATE entries SET meal_amount_cents = ? WHERE id = ?", (self._derive_meal_amount(connection, meal_id), meal_id))
            connection.execute("UPDATE entries SET version = ?, updated_at = ? WHERE id = ?", (expected_version + 1, now, meal_id))
            return self._entry(connection, meal_id)

        return self._write("payment_add", payload, operation)

    def _payment_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        payment_id = self._string(payload.get("payment_id"), "payment_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM active_entries WHERE id = ? AND record_kind = 'meal'", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(row, meal_id, expected_version)
            if self._payment_is_linked(connection, payment_id):
                raise BusinessError("PAYMENT_LINKED", "This payment has linked refunds or allocations", {"payment_id": payment_id})
            deleted = connection.execute("DELETE FROM payments WHERE payment_id = ? AND entry_id = ?", (payment_id, meal_id)).rowcount
            if not deleted:
                raise BusinessError("NOT_FOUND", "payment line was not found", {"id": payment_id, "meal_id": meal_id})
            if not row["meal_amount_manual"]:
                connection.execute("UPDATE entries SET meal_amount_cents = ? WHERE id = ?", (self._derive_meal_amount(connection, meal_id), meal_id))
            connection.execute("UPDATE entries SET version = ?, updated_at = ? WHERE id = ?", (expected_version + 1, now, meal_id))
            return self._entry(connection, meal_id)

        return self._write("payment_remove", payload, operation)

    def _milk_tea_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry_date = self._iso_date(payload.get("date"), "date")
        amount = self._integer(payload.get("amount_cents"), "amount_cents", nullable=True, minimum=0)
        note = self._string(payload.get("note"), "note", nullable=True)
        location = self._string(payload.get("location"), "location", nullable=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            entry_id = self._insert_entry(
                connection, record_kind="milk_tea", entry_date=entry_date, meal_type=None, mode=None,
                meal_amount_cents=None, meal_amount_manual=False, location=location, note=note,
                timing={}, people_count=None,
                consumption_cost={"min_cents": None, "max_cents": None, "source": "unknown"},
            )
            self._insert_payments(connection, entry_id, [{
                "kind": "milk_tea", "scope": "extra", "amount_cents": amount, "amount_min_cents": None,
                "amount_max_cents": None, "source": "actual" if amount is not None else "unknown", "note": None,
            }])
            return self._entry(connection, entry_id)

        return self._write("milk_tea_create", payload, operation)

    def _milk_tea_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        drink_id = self._string(payload.get("drink_id"), "drink_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        changes = self._dict(payload.get("changes"), "changes")
        allowed = {"date", "amount_cents", "note", "location"}
        if not changes or set(changes) - allowed:
            raise BusinessError("INVALID_INPUT", "changes must contain supported milk_tea fields", {"field": "changes"})
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM active_entries WHERE id = ? AND record_kind = 'milk_tea'", (drink_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "milk-tea purchase was not found", {"id": drink_id})
            self._check_version(row, drink_id, expected_version)
            columns: dict[str, Any] = {"version": expected_version + 1, "updated_at": now}
            if "date" in changes:
                entry_date = self._iso_date(changes["date"], "changes.date")
                columns["entry_date"] = entry_date
                columns["calendar_date"] = self._canonical_calendar_date(entry_date)
            if "note" in changes:
                columns["note"] = self._string(changes["note"], "changes.note", nullable=True)
            if "location" in changes:
                columns["location"] = self._string(changes["location"], "changes.location", nullable=True)
            if "amount_cents" in changes:
                amount = self._integer(changes["amount_cents"], "changes.amount_cents", nullable=True, minimum=0)
                connection.execute(
                    "UPDATE payments SET amount_cents = ?, amount_min_cents = NULL, amount_max_cents = NULL, source = ? "
                    "WHERE entry_id = ? AND kind = 'milk_tea'",
                    (amount, "actual" if amount is not None else "unknown", drink_id),
                )
            setters = ", ".join(f"{column} = ?" for column in columns)
            connection.execute(f"UPDATE entries SET {setters} WHERE id = ?", [*columns.values(), drink_id])
            return self._entry(connection, drink_id)

        return self._write("milk_tea_update", payload, operation)

    def _request_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = self._string(payload.get("request_id"), "request_id")
        with self.store.read() as connection:
            row = connection.execute("SELECT action, result_json, created_at FROM request_receipts WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            return {"status": "not_found", "request_id": request_id}
        return {"status": "committed", "request_id": request_id, "action": row["action"], "result": json.loads(row["result_json"]), "created_at": row["created_at"]}

    @staticmethod
    def _json_value(value: Any, field: str) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BusinessError("INVALID_INPUT", f"{field} must be JSON-compatible", {"field": field}) from exc

    def _candidate(self, connection: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "candidate was not found", {"id": candidate_id})
        return {"candidate_id": row["candidate_id"], "version": row["version"], "expected_version": row["version"], "mode": row["mode"], "name": row["name"], "details": json.loads(row["details_json"]), "preference": row["preference"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _candidate_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = self._string(payload.get("candidate_id"), "candidate_id", nullable=True) or uuid.uuid4().hex
        mode = self._string(payload.get("mode"), "mode")
        if mode not in MODES:
            raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
        name = self._string(payload.get("name"), "name")
        details = self._dict(payload.get("details", {}), "details")
        details_json = self._json_value(details, "details")
        preference = payload.get("preference", "neutral")
        if preference not in {"like", "neutral", "avoid"}:
            raise BusinessError("INVALID_INPUT", "preference is unsupported", {"field": "preference"})
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
            if row is None:
                if expected is not None:
                    raise BusinessError("NOT_FOUND", "candidate was not found", {"id": candidate_id})
                connection.execute("INSERT INTO candidates(candidate_id, mode, name, details_json, preference, version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)", (candidate_id, mode, name, details_json, preference, now, now))
            else:
                if expected is None:
                    raise BusinessError("INVALID_INPUT", "expected_version is required when updating a candidate", {"field": "expected_version"})
                self._check_version(row, candidate_id, expected)
                connection.execute("UPDATE candidates SET mode = ?, name = ?, details_json = ?, preference = ?, version = ?, updated_at = ? WHERE candidate_id = ?", (mode, name, details_json, preference, expected + 1, now, candidate_id))
            return self._candidate(connection, candidate_id)

        return self._write("candidate_upsert", payload, operation)

    def _candidate_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit, offset = self._page(payload)
        mode = payload.get("mode")
        if mode is not None and mode not in MODES:
            raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
        with self.store.read() as connection:
            if mode:
                total = connection.execute("SELECT COUNT(*) FROM candidates WHERE mode = ?", (mode,)).fetchone()[0]
                rows = connection.execute("SELECT candidate_id FROM candidates WHERE mode = ? ORDER BY name LIMIT ? OFFSET ?", (mode, limit, offset)).fetchall()
            else:
                total = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                rows = connection.execute("SELECT candidate_id FROM candidates ORDER BY name LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            items = [self._candidate(connection, row["candidate_id"]) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _recipe(self, connection: sqlite3.Connection, recipe_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
        recipe = {"recipe_id": row["recipe_id"], "version": row["version"], "expected_version": row["version"], "name": row["name"], "details": json.loads(row["details_json"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}
        recipe["latest_feedback"] = self._latest_recipe_feedback(connection, recipe_id)
        return recipe

    def _recipe_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id", nullable=True) or uuid.uuid4().hex
        name = self._string(payload.get("name"), "name")
        details_json = self._json_value(self._dict(payload.get("details", {}), "details"), "details")
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
            if row is None:
                if expected is not None:
                    raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
                version = 1
                connection.execute("INSERT INTO recipes(recipe_id, name, details_json, version, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)", (recipe_id, name, details_json, now, now))
            else:
                if expected is None:
                    raise BusinessError("INVALID_INPUT", "expected_version is required when updating a recipe", {"field": "expected_version"})
                self._check_version(row, recipe_id, expected)
                version = expected + 1
                connection.execute("UPDATE recipes SET name = ?, details_json = ?, version = ?, updated_at = ? WHERE recipe_id = ?", (name, details_json, version, now, recipe_id))
            self._record_recipe_version(connection, recipe_id, version, name, details_json)
            return self._recipe(connection, recipe_id)

        return self._write("recipe_upsert", payload, operation)

    def _recipe_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            total = connection.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            rows = connection.execute("SELECT recipe_id FROM recipes ORDER BY name LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            items = [self._recipe(connection, row["recipe_id"]) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _evidence(self, row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["value_json"])
        return {"evidence_id": row["evidence_id"], "subject_type": row["subject_type"], "subject_id": row["subject_id"], "kind": row["kind"], "mode": row["mode"], "location": row["location"], "origin_location": value.get("origin_location", row["location"]), "return_location": value.get("return_location"), "value": value, "source": row["source"], "observed_at": row["observed_at"]}

    def _evidence_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        subject_type = self._string(payload.get("subject_type"), "subject_type")
        kind = self._string(payload.get("kind"), "kind")
        if subject_type not in {"candidate", "recipe", "location", "meal"} or kind not in {"time", "cost"}:
            raise BusinessError("INVALID_INPUT", "subject_type or kind is unsupported", {"field": "subject_type/kind"})
        subject_id = self._string(payload.get("subject_id"), "subject_id", nullable=True)
        mode = payload.get("mode")
        if mode is not None and mode not in MODES:
            raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
        location = self._string(payload.get("location"), "location", nullable=True)
        value = self._dict(payload.get("value"), "value")
        origin_location = self._string(payload.get("origin_location"), "origin_location", nullable=True)
        return_location = self._string(payload.get("return_location"), "return_location", nullable=True)
        if kind == "time":
            origin_location = origin_location or value.get("origin_location") or location
            return_location = return_location or value.get("return_location")
            if origin_location is not None:
                value["origin_location"] = origin_location
            if return_location is not None:
                value["return_location"] = return_location
            location = origin_location
        value_json = self._json_value(value, "value")
        source = self._string(payload.get("source"), "source", nullable=True)
        evidence_id = uuid.uuid4().hex
        observed_at = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute("INSERT INTO evidence(evidence_id, subject_type, subject_id, kind, mode, location, value_json, source, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (evidence_id, subject_type, subject_id, kind, mode, location, value_json, source, observed_at))
            row = connection.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
            return self._evidence(row)

        return self._write("evidence_create", payload, operation)

    def _evidence_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        for key, column in (("subject_type", "subject_type"), ("subject_id", "subject_id"), ("kind", "kind"), ("mode", "mode")):
            value = payload.get(key)
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            rows = connection.execute(f"SELECT * FROM evidence{where} ORDER BY observed_at DESC", values).fetchall()
        return {"items": [self._evidence(row) for row in rows], "total": len(rows)}

    def _preference_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._string(payload.get("key"), "key")
        value_json = self._json_value(payload.get("value"), "value")
        updated_at = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                "INSERT INTO preferences(preference_key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(preference_key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, value_json, updated_at),
            )
            return {"key": key, "value": json.loads(value_json), "updated_at": updated_at}

        return self._write("preference_set", payload, operation)

    def _restriction(self, connection: sqlite3.Connection, restriction_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM restrictions WHERE restriction_id = ?", (restriction_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "restriction was not found", {"id": restriction_id})
        return {"restriction_id": row["restriction_id"], "version": row["version"], "expected_version": row["version"], "kind": row["kind"], "term": row["term"], "active_from": row["active_from"], "active_until": row["active_until"], "enabled": bool(row["enabled"]), "source": row["source"], "updated_at": row["updated_at"]}

    def _restriction_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        restriction_id = self._string(payload.get("restriction_id"), "restriction_id", nullable=True) or uuid.uuid4().hex
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        kind = self._string(payload.get("kind"), "kind")
        if kind not in {"allergy", "temporary_avoidance"}:
            raise BusinessError("INVALID_INPUT", "kind is unsupported", {"field": "kind"})
        term = self._string(payload.get("term"), "term")
        active_from = self._canonical_calendar_date(self._iso_date(payload["active_from"], "active_from")) if payload.get("active_from") is not None else None
        active_until = self._canonical_calendar_date(self._iso_date(payload["active_until"], "active_until")) if payload.get("active_until") is not None else None
        if active_from and active_until and active_from > active_until:
            raise BusinessError("INVALID_INPUT", "active_from cannot be after active_until", {"field": "active_from"})
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BusinessError("INVALID_INPUT", "enabled must be boolean", {"field": "enabled"})
        source = self._string(payload.get("source"), "source", nullable=True)
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM restrictions WHERE restriction_id = ?", (restriction_id,)).fetchone()
            if row is None:
                if expected is not None:
                    raise BusinessError("NOT_FOUND", "restriction was not found", {"id": restriction_id})
                connection.execute("INSERT INTO restrictions(restriction_id, kind, term, active_from, active_until, enabled, source, version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)", (restriction_id, kind, term, active_from, active_until, int(enabled), source, now))
            else:
                if expected is None:
                    raise BusinessError("INVALID_INPUT", "expected_version is required when updating a restriction", {"field": "expected_version"})
                self._check_version(row, restriction_id, expected)
                stored_active_from = self._canonical_calendar_date(row["active_from"]) if row["active_from"] is not None else None
                stored_active_until = self._canonical_calendar_date(row["active_until"]) if row["active_until"] is not None else None
                weakens_enabled_allergy = row["kind"] == "allergy" and row["enabled"] and (
                    kind != "allergy"
                    or term.casefold() != row["term"].casefold()
                    or not enabled
                    or (stored_active_from is None and active_from is not None)
                    or (stored_active_from is not None and active_from is not None and active_from > stored_active_from)
                    or (stored_active_until is None and active_until is not None)
                    or (stored_active_until is not None and active_until is not None and active_until < stored_active_until)
                )
                if weakens_enabled_allergy:
                    if payload.get("explicit_user_action") is not True:
                        raise BusinessError("INVALID_INPUT", "weakening an enabled allergy requires explicit_user_action=true", {"field": "explicit_user_action"})
                connection.execute("UPDATE restrictions SET kind = ?, term = ?, active_from = ?, active_until = ?, enabled = ?, source = ?, version = ?, updated_at = ? WHERE restriction_id = ?", (kind, term, active_from, active_until, int(enabled), source, expected + 1, now, restriction_id))
            return self._restriction(connection, restriction_id)

        return self._write("restriction_set", payload, operation)

    def _restriction_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        as_of = self._canonical_calendar_date(self._iso_date(payload.get("as_of", date.today().isoformat()), "as_of"))
        with self.store.read() as connection:
            rows = connection.execute("SELECT * FROM restrictions ORDER BY updated_at DESC").fetchall()
            items = [
                {
                    "restriction_id": row["restriction_id"], "version": row["version"], "expected_version": row["version"],
                    "kind": row["kind"], "term": row["term"], "active_from": row["active_from"],
                    "active_until": row["active_until"], "enabled": bool(row["enabled"]),
                    "source": row["source"], "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        active = [
            item for item in items
            if item["enabled"]
            and (item["active_from"] is None or self._canonical_calendar_date(item["active_from"]) <= as_of)
            and (item["active_until"] is None or self._canonical_calendar_date(item["active_until"]) >= as_of)
        ]
        return {"items": items, "active": active, "as_of": as_of}

    @staticmethod
    def _parse_now(value: str | None) -> datetime:
        if value is None:
            return datetime.now().astimezone()
        text = value.strip()
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", "now/deadline must be ISO date-time", {"field": "now/deadline"}) from exc
        return result.astimezone() if result.tzinfo is not None else result.replace(tzinfo=datetime.now().astimezone().tzinfo)

    @staticmethod
    def _stage_range(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, dict):
            return None
        steps = value.get("steps")
        if isinstance(steps, list):
            groups: dict[str, list[tuple[int, int]]] = {}
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    return None
                low, high = step.get("min_minutes"), step.get("max_minutes")
                if not isinstance(low, int) or isinstance(low, bool) or not isinstance(high, int) or isinstance(high, bool) or low < 0 or high < low:
                    return None
                group = str(step.get("concurrent_group", f"step-{index}"))
                groups.setdefault(group, []).append((low, high))
            return (sum(max(pair[0] for pair in group) for group in groups.values()), sum(max(pair[1] for pair in group) for group in groups.values()))
        low, high = value.get("min_minutes"), value.get("max_minutes")
        if not isinstance(low, int) or isinstance(low, bool) or not isinstance(high, int) or isinstance(high, bool) or low < 0 or high < low:
            return None
        return low, high

    @staticmethod
    def _cost_range(value: Any) -> tuple[int, int, str] | None:
        if not isinstance(value, dict):
            return None
        low, high = value.get("min_cents"), value.get("max_cents")
        if low is None or high is None or isinstance(low, bool) or isinstance(high, bool) or not isinstance(low, int) or not isinstance(high, int) or low < 0 or high < low:
            return None
        return low, high, value.get("source", "unknown")

    def _active_restrictions(self, connection: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
        as_of = self._canonical_calendar_date(self._iso_date(as_of, "as_of"))
        rows = connection.execute("SELECT * FROM restrictions WHERE enabled = 1").fetchall()
        return [
            self._restriction(connection, row["restriction_id"])
            for row in rows
            if (row["active_from"] is None or self._canonical_calendar_date(row["active_from"]) <= as_of)
            and (row["active_until"] is None or self._canonical_calendar_date(row["active_until"]) >= as_of)
        ]

    def _recommendation_kitchen_match(
        self,
        connection: sqlite3.Connection,
        record: dict[str, Any],
        target_servings: int,
        kitchen_context: dict[str, Any],
    ) -> dict[str, Any]:
        available: dict[str, dict[str, dict[str, Any]]] = {}

        def add_source(name: str, quantity: Any, unit: str, source: dict[str, Any]) -> None:
            normalized = _normalize_quantity(quantity, unit)
            if normalized is None:
                return
            amount, normalized_unit = normalized
            name_key = self._ingredient_key(name)
            unit_key = normalized_unit.casefold()
            units = available.setdefault(name_key, {})
            item = units.setdefault(
                unit_key,
                {"unit": normalized_unit, "quantity": Decimal(0), "sources": []},
            )
            item["quantity"] += amount
            item["sources"].append(source)

        for item in kitchen_context["inventory"]:
            normalized = _normalize_quantity(item["quantity"], item["unit"])
            if normalized is None:
                continue
            quantity, unit = normalized
            add_source(
                item["ingredient"],
                quantity,
                unit,
                {
                    "source_type": "pantry_inventory",
                    "quantity": self._decimal_text(quantity),
                    "unit": unit,
                    "batches": item["batches"],
                },
            )
        for item in kitchen_context["leftovers"]:
            quantity, unit = _normalize_quantity(item["servings"], "份")
            if quantity <= 0:
                continue
            add_source(
                item["name"],
                quantity,
                unit,
                {
                    "source_type": "leftover",
                    "quantity": self._decimal_text(quantity),
                    "unit": unit,
                    "leftover_id": item["leftover_id"],
                    "prepared_on": item["prepared_on"],
                    "use_by": item["use_by"],
                    "source_meal_id": item["source_meal_id"],
                    "source_meal_status": item["source_meal_status"],
                },
            )

        details = record["details"]
        if record["source_type"] == "recipe":
            requirements = self._scaled_recipe(connection, record["id"], target_servings)["ingredients"]
        else:
            requirements = []
            names = details.get("ingredients", [])
            amounts = details.get("ingredient_amounts", [])
            base_servings = details.get("servings")
            for index, name in enumerate(names):
                amount = amounts[index] if isinstance(amounts, list) and index < len(amounts) and isinstance(amounts[index], dict) else {}
                quantity = amount.get("quantity")
                unit = amount.get("unit")
                normalized = _normalize_quantity(quantity, unit)
                known = bool(
                    normalized
                    and amount.get("quantity_known", True)
                    and isinstance(base_servings, int)
                    and not isinstance(base_servings, bool)
                    and base_servings > 0
                )
                requirements.append(
                    {
                        "name": name,
                        "scaled_quantity": self._decimal_text(
                            normalized[0] * Decimal(target_servings) / Decimal(base_servings)
                        ) if known else None,
                        "unit": normalized[1] if normalized else unit,
                        "quantity_known": known,
                    }
                )

        matches = []
        for requirement in requirements:
            name = requirement["name"]
            same_name = available.get(self._ingredient_key(name), {})
            sources = [source for item in same_name.values() for source in item["sources"]]
            quantity = requirement.get("scaled_quantity")
            unit = requirement.get("unit")
            normalized = _normalize_quantity(quantity, unit) if requirement.get("quantity_known") else None
            if normalized is None:
                matches.append(
                    {
                        "name": name,
                        "required_quantity": None,
                        "available_quantity": None,
                        "unit": unit,
                        "status": "needs_confirmation",
                        "sources": sources,
                    }
                )
                continue

            required_quantity, required_unit = normalized
            stock = same_name.get(required_unit.casefold())
            if stock is None:
                incompatible_sources = bool(sources)
                matches.append(
                    {
                        "name": name,
                        "required_quantity": self._decimal_text(required_quantity),
                        "available_quantity": None if incompatible_sources else "0",
                        "unit": required_unit,
                        "status": "needs_confirmation" if incompatible_sources else "insufficient",
                        "sources": sources,
                    }
                )
                continue

            available_quantity = stock["quantity"]
            matches.append(
                {
                    "name": name,
                    "required_quantity": self._decimal_text(required_quantity),
                    "available_quantity": self._decimal_text(available_quantity),
                    "unit": required_unit,
                    "status": "matched" if available_quantity >= required_quantity else "insufficient",
                    "sources": stock["sources"],
                }
            )
        return {"ingredients": matches}

    @staticmethod
    def _record_dish_names(record):
        details = record["details"]
        values = details.get("dishes")
        if not isinstance(values, list):
            values = [record["name"]] if record["source_type"] == "recipe" else [details.get("dish")]
        return [dish.strip() for value in values for dish in [value.get("name") if isinstance(value, dict) else value]
                if isinstance(dish, str) and dish.strip()]

    def _recommend(self, payload: dict[str, Any], *, record_filter=None) -> dict[str, Any]:
        with self.store.read() as connection:
            effective_arguments, defaults_used = self._recommendation_defaults(connection, payload)
            now = self._parse_now(effective_arguments.get("now"))
            deadline = self._parse_now(effective_arguments["deadline"]) if effective_arguments.get("deadline") else None
            target = effective_arguments.get("time_target", "meal_start")
            if target not in {"meal_start", "finished", "returned"}:
                raise BusinessError("INVALID_INPUT", "time_target is unsupported", {"field": "time_target"})
            budget = self._integer(effective_arguments.get("budget_cents"), "budget_cents", nullable=True, minimum=0)
            basis = effective_arguments.get("budget_basis", "payment")
            if basis not in {"payment", "consumption"}:
                raise BusinessError("INVALID_INPUT", "budget_basis is unsupported", {"field": "budget_basis"})
            budget_scope = effective_arguments.get("budget_scope", "total")
            if budget_scope not in {"total", "personal_share"}:
                raise BusinessError("INVALID_INPUT", "budget_scope is unsupported", {"field": "budget_scope"})
            location = self._string(effective_arguments.get("location"), "location", nullable=True)
            return_location = self._string(effective_arguments.get("return_location"), "return_location", nullable=True)
            if "return_location" not in effective_arguments:
                return_location = location
            meal_type = effective_arguments.get("meal_type")
            if meal_type is not None and meal_type not in MEAL_TYPES:
                raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
            lookback_days = self._integer(effective_arguments.get("repeat_lookback_days"), "repeat_lookback_days", nullable=True, minimum=1)
            requested_people = self._integer(effective_arguments.get("people_count"), "people_count", nullable=True, minimum=1) or 1
            mode = effective_arguments.get("mode")
            if mode is not None and mode not in MODES:
                raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
            limit, _ = self._page({"limit": effective_arguments.get("limit", 100), "offset": 0})
            available_ingredients = effective_arguments.get("available_ingredients")
            if available_ingredients is not None and not isinstance(available_ingredients, list):
                raise BusinessError("INVALID_INPUT", "available_ingredients must be an array", {"field": "available_ingredients"})
            available_utensils = effective_arguments.get("available_utensils")
            if available_utensils is not None and not isinstance(available_utensils, list):
                raise BusinessError("INVALID_INPUT", "available_utensils must be an array", {"field": "available_utensils"})
            use_kitchen_stock = effective_arguments.get("use_kitchen_stock", False)
            kitchen_location = self._string(effective_arguments.get("kitchen_location"), "kitchen_location", nullable=True)
            occasion = self._string(effective_arguments.get("occasion"), "occasion", nullable=True)
            price_region = self._string(effective_arguments.get("price_region"), "price_region", nullable=True)
            ranking = effective_arguments.get("ranking", "existing")
            weights = score_weights(effective_arguments.get("score_weights"))
            if ranking not in ("existing", "score"):
                raise BusinessError("INVALID_INPUT", "ranking must be existing or score")
            as_of = now.date().isoformat()
            restrictions = self._active_restrictions(connection, as_of)
            candidates = connection.execute("SELECT * FROM candidates ORDER BY name").fetchall()
            recipes = connection.execute("SELECT * FROM recipes ORDER BY name").fetchall()
            evidence_rows = connection.execute("SELECT * FROM evidence ORDER BY observed_at DESC").fetchall()
            evidence = [self._evidence(row) for row in evidence_rows]
            evidence_by_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for item in evidence:
                if item["subject_type"] in {"candidate", "recipe"} and item["subject_id"] is not None:
                    evidence_by_source.setdefault((item["subject_type"], item["subject_id"]), []).append(item)
            records: list[dict[str, Any]] = []
            for row in candidates:
                if mode and row["mode"] != mode:
                    continue
                details = json.loads(row["details_json"])
                records.append({"id": row["candidate_id"], "name": row["name"], "mode": row["mode"], "preference": row["preference"], "details": details, "source_type": "candidate"})
            for row in recipes:
                if mode and mode != "home_cooking":
                    continue
                details = json.loads(row["details_json"])
                confirmed = self._recipe_confirmed_evidence(connection, row["recipe_id"], row["version"])
                if confirmed:
                    for key in ("ingredients", "allergens"):
                        original = details.get(key) or []
                        details[key] = list(dict.fromkeys([*(original if isinstance(original, list) else []), *confirmed[key]]))
                        details[key + "_known"] = confirmed[key + "_known"]
                records.append({"id": row["recipe_id"], "name": row["name"], "mode": "home_cooking", "preference": "neutral", "details": details, "source_type": "recipe"})
            if record_filter is not None:
                records = [record for record in records if record_filter(record)]

            recommendation_context = self._recommendation_context(connection, effective_arguments, records)
            occasions = {
                f"{record['source_type']}:{record['id']}": self._read_candidate_occasion(connection, record, occasion)
                for record in records
            }
            canonical_stores = {
                f"{record['source_type']}:{record['id']}": self._canonical_store_id(connection, record["details"]["store_id"])
                if isinstance(record["details"].get("store_id"), str) else None
                for record in records
            }
            kitchen_context = None
            if use_kitchen_stock and mode in {None, "home_cooking"}:
                kitchen_context = self._available_kitchen_context(connection, as_of, kitchen_location)
            kitchen_alerts = self._kitchen_alerts(connection, as_of, kitchen_location) if (
                mode == "home_cooking" or (mode is None and (
                    use_kitchen_stock or any(record["mode"] == "home_cooking" for record in records)
                ))
            ) else {}
            kitchen_matches = {}
            if kitchen_context is not None:
                kitchen_matches = {
                    f"{record['source_type']}:{record['id']}": self._recommendation_kitchen_match(
                        connection, record, requested_people, kitchen_context
                    )
                    for record in records
                    if record["mode"] == "home_cooking"
                }
            price_estimates = {}
            if price_region is not None:
                inventory = kitchen_context["inventory"] if kitchen_context is not None else None
                if inventory is not None and available_ingredients is not None:
                    available_keys = {self._ingredient_key(name) for name in available_ingredients if isinstance(name, str)}
                    inventory = [item for item in inventory if self._ingredient_key(item["ingredient"]) in available_keys]
                price_estimates = {
                    record["id"]: self._estimate_recipe_prices(
                        connection, record["id"], requested_people, price_region, as_of, inventory, kitchen_location
                    )
                    for record in records if record["source_type"] == "recipe"
                }

        results = []
        for record in records:
            details = record["details"]
            context_key = f"{record['source_type']}:{record['id']}"
            recommendation = recommendation_context[context_key]
            evidence_for = evidence_by_source.get((record["source_type"], record["id"]), [])
            time_evidence = [item for item in evidence_for if item["kind"] == "time"]
            matching_time_evidence = [item for item in time_evidence if item["origin_location"] == location]
            time_profile = details.get("time_profile")
            historical_profile = recommendation["time_profile"]
            if historical_profile is not None and (
                time_profile is None
                or (historical_profile["source"] == "observed" and historical_profile["sample_count"] > 0)
            ):
                time_profile = historical_profile
            cost_profile = details.get("cost_profile")
            if time_profile is None:
                time_profile = next((item["value"] for item in matching_time_evidence), None)
            if cost_profile is None:
                cost_profile = next((item["value"] for item in evidence_for if item["kind"] == "cost"), None)
            time_missing = []
            if time_profile is None and time_evidence and not matching_time_evidence:
                time_missing.append("time evidence origin does not match requested location")
            elif isinstance(time_profile, dict):
                origin = time_profile.get("origin_location")
                if origin is not None and origin != location:
                    time_profile = None
                    time_missing.append("time profile origin does not match requested location")
                else:
                    expected_return = return_location
                    profile_return = time_profile.get("return_location") or origin
                    if target == "returned" and expected_return is not None and profile_return is None:
                        time_profile = {**time_profile, "stages": {**time_profile.get("stages", {}), "return": None}}
                    elif expected_return is not None and profile_return is not None and profile_return != expected_return:
                        time_profile = {**time_profile, "stages": {**time_profile.get("stages", {}), "return": None}}
                        if target == "returned":
                            time_missing.append("time profile return location does not match requested location")
            restriction_status, restriction_reasons = self._restriction_status(details, restrictions)
            utensil_missing = self._missing_list(details.get("utensils"), available_utensils)
            kitchen_match = kitchen_matches.get(context_key)
            record_available_ingredients = available_ingredients
            if kitchen_match is not None:
                if record_available_ingredients is None:
                    record_available_ingredients = [
                        item["name"] for item in kitchen_match["ingredients"] if item["status"] == "matched"
                    ]
            time_result = self._time_result(record["mode"], time_profile, now, deadline, target, details, record_available_ingredients, time_missing, bool(utensil_missing))
            payment_zero_confirmed = (
                record["mode"] == "home_cooking"
                and details.get("shopping_required") is False
                and details.get("ingredients_known") is True
                and isinstance(details.get("ingredients"), list)
                and isinstance(details.get("utensils"), list)
                and record_available_ingredients is not None
                and available_utensils is not None
                and not self._missing_list(details.get("ingredients"), record_available_ingredients)
                and not self._missing_list(details.get("utensils"), available_utensils)
            )
            if payment_zero_confirmed and (cost_profile is None or isinstance(cost_profile, dict)) and (cost_profile or {}).get("payment") is None:
                cost_profile = {**(cost_profile or {}), "payment_coverage_people_count": requested_people, "payment": {"min_cents": 0, "max_cents": 0, "source": "estimate"}}
            estimate = price_estimates.get(record["id"]) if record["source_type"] == "recipe" else None
            if estimate is not None:
                cost_profile = dict(cost_profile) if isinstance(cost_profile, dict) else {}
                for key in ("payment", "consumption"):
                    amount = estimate[f"{key}_cents"]
                    if amount is not None and cost_profile.get(key) is None:
                        cost_profile[key] = {"min_cents": amount, "max_cents": amount, "source": "estimate"}
                        cost_profile[f"{key}_coverage_people_count"] = requested_people
            cost_result = self._money_result(cost_profile, budget, basis, budget_scope, requested_people, details)
            if utensil_missing:
                restriction_reasons.extend(f"missing utensil: {value}" for value in utensil_missing)
            status = "eligible"
            if record["preference"] == "avoid" or restriction_status == "excluded":
                status = "excluded"
                if record["preference"] == "avoid":
                    restriction_reasons.append("user marked as avoid")
            elif time_result["status"] == "expected_exceeds" or cost_result["status"] == "expected_exceeds":
                status = "excluded"
                if time_result["status"] == "expected_exceeds":
                    restriction_reasons.append("time constraint exceeded")
                if cost_result["status"] == "expected_exceeds":
                    restriction_reasons.append(f"{basis} budget exceeded")
            elif restriction_status == "needs_verification" or time_result["status"] == "needs_verification" or cost_result["status"] == "needs_verification" or utensil_missing:
                status = "needs_verification"
            occasion_result = occasions[context_key]
            if occasion_result["status"] == "not_listed":
                status = "excluded"
                restriction_reasons.append(f"occasion not among recorded options: {occasion}")
            elif occasion_result["status"] == "needs_verification":
                if status == "eligible":
                    status = "needs_verification"
                restriction_reasons.append(f"occasion suitability unrecorded: {occasion}")
            shown_evidence = [item for item in evidence_for if item["kind"] != "time" or item in matching_time_evidence]
            dishes = self._record_dish_names(record)
            result = {
                "id": record["id"], "name": record["name"], "mode": record["mode"],
                "source_type": record["source_type"], "preference": record["preference"],
                "store_id": canonical_stores[context_key],
                "dishes": dishes,
                "status": status, "time": time_result, "cost": cost_result, "occasion": occasion_result,
                "missing": restriction_reasons + time_result["missing"] + cost_result["missing"] + utensil_missing,
                "evidence": [{"source_type": item["subject_type"], "source": item["source"], "kind": item["kind"]} for item in shown_evidence],
                "recommendation_context": {
                    "reasons": recommendation["reasons"],
                    "history": recommendation["history"],
                    "time_profile": recommendation["time_profile"],
                },
            }
            if kitchen_match is not None:
                result["kitchen_match"] = kitchen_match
            if estimate is not None:
                result["recipe_price_estimate"] = estimate
            result["recommendation_score"] = recommendation_score(result, budget, recommendation["history"], weights)
            results.append(result)
        results.sort(key=lambda row: (
            {"excluded": 2, "needs_verification": 1, "eligible": 0}[row["status"]],
            -row["recommendation_score"]["percent"] if ranking == "score" else 0,
            0 if row["preference"] == "like" else 1,
            recommendation_context[f"{row['source_type']}:{row['id']}"]["rank_penalty"],
            row["name"],
        ))
        result = {
            "items": results[:limit], "total": len(results), "now": now.isoformat(timespec="minutes"),
            "deadline": deadline.isoformat(timespec="minutes") if deadline else None,
            "time_target": target, "budget_cents": budget, "budget_basis": basis,
            "budget_scope": budget_scope, "people_count": requested_people,
            "location": location, "return_location": return_location, "meal_type": meal_type,
            "occasion": occasion, "price_region": price_region,
            "ranking": ranking,
            "score_weights": weights,
            "repeat_lookback_days": lookback_days, "defaults_used": defaults_used,
            **kitchen_alerts,
        }
        if kitchen_context is not None:
            result["kitchen_context"] = {
                "as_of": kitchen_context["as_of"],
                "location_scope": kitchen_context["location"],
                "inventory": kitchen_context["inventory"],
                "leftovers": kitchen_context["leftovers"],
            }
        return result

    @staticmethod
    def _missing_list(required: Any, available: list | None) -> list[str]:
        if not required:
            return []
        if not isinstance(required, list):
            return ["required supplies are not structured"]
        if available is None:
            return [f"availability unknown: {item}" for item in required]
        available_folded = {str(item).casefold() for item in available}
        return [str(item) for item in required if str(item).casefold() not in available_folded]

    @staticmethod
    def _restriction_status(details: dict[str, Any], restrictions: list[dict[str, Any]]) -> tuple[str, list[str]]:
        if not restrictions:
            return "eligible", []
        ingredients = details.get("ingredients")
        allergens = details.get("allergens")
        ingredients_known = details.get("ingredients_known") is True and isinstance(ingredients, list)
        allergens_known = details.get("allergens_known") is True and isinstance(allergens, list)
        known_values = [str(item).casefold() for item in ingredients] if isinstance(ingredients, list) else []
        known_allergens = [str(item).casefold() for item in allergens] if isinstance(allergens, list) else []
        reasons = []
        pending = False
        for restriction in restrictions:
            term = restriction["term"].casefold()
            if restriction["kind"] == "allergy":
                if any(term in item for item in known_values + known_allergens):
                    return "excluded", [f"contains restricted allergen: {restriction['term']}"]
                if not (ingredients_known and allergens_known):
                    pending = True
                    reasons.append(f"allergen needs verification: {restriction['term']}")
            else:
                if any(term in item for item in known_values):
                    return "excluded", [f"contains temporary avoidance: {restriction['term']}"]
                if not ingredients_known:
                    pending = True
                    reasons.append(f"ingredients need verification: {restriction['term']}")
        return ("needs_verification" if pending else "eligible"), reasons

    @staticmethod
    def _mode_stages(mode: str, time_target: str, details: dict[str, Any]) -> tuple[list[str], list[str]]:
        if mode == "eat_out":
            pre = ["outbound", "queue", "wait"]
        elif mode == "delivery":
            pre = ["order_to_ready"]
        else:
            shopping_required = details.get("shopping_required")
            pre = ([] if shopping_required is False else ["shopping"]) + ["prep", "cook"]
        return pre, pre + (["meal"] if time_target in {"finished", "returned"} else []) + (["return"] if time_target == "returned" else [])

    def _time_result(
        self,
        mode: str,
        profile: Any,
        now: datetime,
        deadline: datetime | None,
        target: str,
        details: dict[str, Any],
        available_ingredients: list | None,
        context_missing: list[str],
        prerequisite_missing: bool,
    ) -> dict[str, Any]:
        stages = profile.get("stages") if isinstance(profile, dict) else None
        pre_stages, target_stages = self._mode_stages(mode, target, details)
        missing = list(context_missing)
        ranges: dict[str, tuple[int, int]] = {}
        display_stages = list(dict.fromkeys(pre_stages + ["meal", "return"]))
        if not isinstance(stages, dict):
            missing.extend(target_stages)
        else:
            for stage in display_stages:
                duration = self._stage_range(stages.get(stage))
                if duration is not None:
                    ranges[stage] = duration
                elif stage in target_stages:
                    missing.append(stage)
        if mode == "home_cooking" and details.get("ingredients"):
            ingredient_missing = self._missing_list(details.get("ingredients"), available_ingredients)
            if ingredient_missing:
                if details.get("shopping_required") is False:
                    missing.extend(f"available ingredient: {item}" for item in ingredient_missing)
                elif "shopping" not in missing and "shopping" not in ranges:
                    missing.append("shopping")
        finish_stages = pre_stages + ["meal"]
        start_range = self._sum_ranges(ranges[stage] for stage in pre_stages) if all(stage in ranges for stage in pre_stages) else None
        start_at = None if start_range is None else {"earliest": (now + timedelta(minutes=start_range[0])).isoformat(timespec="minutes"), "latest": (now + timedelta(minutes=start_range[1])).isoformat(timespec="minutes")}
        finish_at = None
        finish_range = None
        if all(stage in ranges for stage in finish_stages):
            finish_range = self._sum_ranges(ranges[stage] for stage in finish_stages)
            finish_at = {"earliest": (now + timedelta(minutes=finish_range[0])).isoformat(timespec="minutes"), "latest": (now + timedelta(minutes=finish_range[1])).isoformat(timespec="minutes")}
        return_at = None
        return_range = None
        if finish_range is not None and "return" in ranges:
            return_range = self._sum_ranges([finish_range, ranges["return"]])
            return_at = {"earliest": (now + timedelta(minutes=return_range[0])).isoformat(timespec="minutes"), "latest": (now + timedelta(minutes=return_range[1])).isoformat(timespec="minutes")}
        comparison_range = start_range if target == "meal_start" else (finish_range if target == "finished" else return_range)
        total_range = comparison_range
        status = "known" if comparison_range is not None and not missing and not prerequisite_missing else "needs_verification"
        limit_minutes = None
        if deadline is not None:
            if comparison_range is None or missing or prerequisite_missing:
                status = "needs_verification"
            else:
                limit_minutes = max(0, math.floor((deadline - now).total_seconds() / 60))
                if comparison_range[1] <= limit_minutes:
                    status = "expected_meets"
                elif comparison_range[0] > limit_minutes:
                    status = "expected_exceeds"
                else:
                    status = "needs_verification"
        source = profile.get("source", "unknown") if isinstance(profile, dict) else "unknown"
        return {"status": status, "source": source, "minutes": None if total_range is None else {"min": total_range[0], "max": total_range[1]}, "start_at": start_at, "finish_at": finish_at, "return_at": return_at, "deadline_minutes": limit_minutes, "missing": sorted(set(missing))}

    @staticmethod
    def _sum_ranges(values: Any) -> tuple[int, int]:
        pairs = list(values)
        return sum(value[0] for value in pairs), sum(value[1] for value in pairs)

    def _money_result(self, profile: Any, budget: int | None, basis: str, scope: str, people_count: int, details: dict[str, Any]) -> dict[str, Any]:
        profile = profile if isinstance(profile, dict) else {}
        views = {}
        for key in ("payment", "consumption"):
            covered_people = profile.get(f"{key}_coverage_people_count", profile.get("people_count", details.get("servings", 1))) or 1
            coverage_matches = covered_people == people_count
            amount = self._cost_range(profile.get(key))
            if scope == "personal_share":
                amount = self._cost_range(profile.get("personal_share", {}).get(key)) if isinstance(profile.get("personal_share"), dict) else None
            if amount is None:
                views[key] = {"status": "needs_verification", "basis": key, "scope": scope, "cents": None, "source": None, "people_count": people_count, "missing": [f"{scope} {key} cost"]}
                continue
            low, high, source = amount
            status = "known"
            missing = []
            if scope == "total" and not coverage_matches:
                status = "needs_verification"
                missing.append(f"cost profile covers {covered_people} people, request is {people_count}")
            elif budget is not None and key == basis:
                if high <= budget:
                    status = "expected_meets"
                elif low > budget:
                    status = "expected_exceeds"
                else:
                    status = "needs_verification"
            views[key] = {"status": status, "basis": key, "scope": scope, "cents": {"min": low, "max": high}, "source": source, "people_count": covered_people if scope == "total" else people_count, "missing": missing}
        selected = views[basis]
        return {**selected, "payment": views["payment"], "consumption": views["consumption"]}

    def close(self) -> None:
        self.store.close()


__all__ = ["BusinessService", "BusinessError", "MEAL_TYPES", "MODES", "PAYMENT_KINDS"]
