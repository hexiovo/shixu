from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import uuid
from typing import Any

from .errors import BusinessError
from .kitchen_recipe import _normalize_quantity
from .tool_schema import _date, _integer, _nullable_string, _object, _request_id, _string, _tool


_PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "offset": {"type": "integer", "minimum": 0},
}
_EXPECTED_VERSION = {"type": ["integer", "null"], "minimum": 1}
_QUANTITY = {"type": ["number", "string"]}
_MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack", "other"]
_MAX_SHELF_LIFE = date.max.toordinal() - 1
STOCK_EXPIRY_FIELDS = {
    "location": _nullable_string,
    "expires_on": {**_date, "type": ["string", "null"]},
    "recorded_on": _date,
    "shelf_life_days": {"type": "integer", "minimum": 0, "maximum": _MAX_SHELF_LIFE},
}


TOOL_SCHEMAS = [
    _tool(
        "inventory_batch_upsert",
        "Record a purchased ingredient or update a stock batch. New stock needs expires_on or a saved shelf life for that ingredient and location. Explicit shelf_life_days saves the duration for future batches.",
        {
            "request_id": _request_id,
            "batch_id": _nullable_string,
            "expected_version": _EXPECTED_VERSION,
            "ingredient": _string,
            "quantity": _QUANTITY,
            "unit": _string,
            **STOCK_EXPIRY_FIELDS,
            "source": _string,
        },
        ("request_id", "ingredient", "quantity", "unit", "source"),
    ),
    _tool(
        "inventory_shelf_life_get",
        "Look up the shelf-life duration saved for an ingredient and its exact storage location, and calculate the date from recorded_on or today. Missing durations return null.",
        {"ingredient": _string, "location": _nullable_string, "recorded_on": _date},
        ("ingredient",),
    ),
    _tool(
        "inventory_shelf_life_set",
        "Save a user-confirmed number of shelf-life days for an ingredient and storage location, or clear it with null. Existing batch dates are unaffected.",
        {"request_id": _request_id, "ingredient": _string, "location": _nullable_string,
         "shelf_life_days": {**STOCK_EXPIRY_FIELDS["shelf_life_days"], "type": ["integer", "null"]}},
        ("request_id", "ingredient", "shelf_life_days"),
    ),
    _tool(
        "inventory_batch_list",
        "List pantry batches, including expired and empty batches, with expiry status for the requested date.",
        {"ingredient": _string, "location": _string, "as_of": _date, **_PAGE},
    ),
    _tool(
        "inventory_batch_adjust",
        "Adjust one pantry batch by a signed quantity and explicit source. The expected version is required and stock cannot fall below zero.",
        {"request_id": _request_id, "batch_id": _string, "expected_version": {"type": "integer", "minimum": 1}, "delta_quantity": _QUANTITY, "unit": _string, "source": _string},
        ("request_id", "batch_id", "expected_version", "delta_quantity", "unit", "source"),
    ),
    _tool(
        "leftover_upsert",
        "Record a leftover and its servings and preparation date. A new available leftover defaults to the day after preparation when use_by is omitted. No meal is created.",
        {
            "request_id": _request_id,
            "leftover_id": _nullable_string,
            "expected_version": _EXPECTED_VERSION,
            "name": _string,
            "servings": {"type": "integer", "minimum": 0},
            "prepared_on": _date,
            "use_by": {"type": ["string", "null"], "format": "date"},
            "source_meal_id": _nullable_string,
            "status": {"type": "string", "enum": ["available", "used_up", "discarded"]},
            "note": _nullable_string,
        },
        ("request_id", "name", "servings", "prepared_on", "status"),
    ),
    _tool(
        "leftover_list",
        "List leftovers with their current source-meal status and use-by status.",
        {"status": {"type": "string", "enum": ["available", "used_up", "discarded"]}, "source_meal_id": _string, "as_of": _date, **_PAGE},
    ),
    _tool(
        "leftover_history_list",
        "Read the immutable change history for one leftover.",
        {"leftover_id": _string, **_PAGE},
        ("leftover_id",),
    ),
    _tool(
        "menu_plan_upsert",
        "Create or update one planned meal with a recipe revision and serving count. An existing active meal may be linked explicitly; linking never edits the meal or consumes stock.",
        {
            "request_id": _request_id,
            "plan_id": _nullable_string,
            "expected_version": _EXPECTED_VERSION,
            "planned_date": _date,
            "meal_type": {"type": "string", "enum": _MEAL_TYPES},
            "recipe_id": _string,
            "servings": {"type": "integer", "minimum": 1},
            "meal_id": _nullable_string,
        },
        ("request_id", "planned_date", "meal_type", "recipe_id", "servings"),
    ),
    _tool(
        "menu_plan_list",
        "List planned meals and show the linked meal's current active or void status without rewriting either record.",
        {"date_from": _date, "date_to": _date, "meal_type": {"type": "string", "enum": _MEAL_TYPES}, **_PAGE},
    ),
    _tool(
        "shopping_list_build",
        "Build a consolidated shopping list from explicitly selected menu plans and scaled recipe ingredients. Compatible quantities are normalized, available non-expired stock is subtracted for each plan date, and unknown amounts remain unknown.",
        {"request_id": _request_id, "plan_ids": {"type": "array", "minItems": 1, "maxItems": 100, "items": _string}},
        ("request_id", "plan_ids"),
    ),
    _tool(
        "shopping_list_get",
        "Read a saved consolidated shopping-list snapshot and its item versions.",
        {"shopping_list_id": _string},
        ("shopping_list_id",),
    ),
    _tool(
        "shopping_list_mark_bought",
        "Mark an item bought and add its known outstanding quantity to stock with a manual date or saved shelf life. Supply quantity and unit when the amount differs or is unknown. Marking needed only reopens the checklist item.",
        {"request_id": _request_id, "shopping_list_id": _string, "item_id": _string, "expected_version": {"type": "integer", "minimum": 1}, "bought": {"type": "boolean"}, "quantity": _QUANTITY, "unit": _string, **STOCK_EXPIRY_FIELDS},
        ("request_id", "shopping_list_id", "item_id", "expected_version", "bought"),
    ),
]


HANDLERS = {
    "inventory_batch_upsert": "_inventory_batch_upsert",
    "inventory_shelf_life_get": "_inventory_shelf_life_get",
    "inventory_shelf_life_set": "_inventory_shelf_life_set",
    "inventory_batch_list": "_inventory_batch_list",
    "inventory_batch_adjust": "_inventory_batch_adjust",
    "leftover_upsert": "_leftover_upsert",
    "leftover_list": "_leftover_list",
    "leftover_history_list": "_leftover_history_list",
    "menu_plan_upsert": "_menu_plan_upsert",
    "menu_plan_list": "_menu_plan_list",
    "shopping_list_build": "_shopping_list_build",
    "shopping_list_get": "_shopping_list_get",
    "shopping_list_mark_bought": "_shopping_list_mark_bought",
}


MIGRATION_SQL = [
    "CREATE TABLE IF NOT EXISTS inventory_batches ("
    "batch_id TEXT PRIMARY KEY, ingredient TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL, "
    "location TEXT, expires_on TEXT, source TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS inventory_batches_ingredient_unit "
    "ON inventory_batches(ingredient COLLATE NOCASE, unit COLLATE NOCASE, expires_on)",
    "CREATE TABLE IF NOT EXISTS inventory_batch_events ("
    "event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES inventory_batches(batch_id), "
    "version INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('created', 'updated', 'adjusted')), "
    "quantity_before TEXT, quantity_after TEXT NOT NULL, source TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, "
    "recorded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS leftovers ("
    "leftover_id TEXT PRIMARY KEY, name TEXT NOT NULL, servings INTEGER NOT NULL CHECK(servings >= 0), "
    "prepared_on TEXT NOT NULL, use_by TEXT, source_meal_id TEXT REFERENCES entries(id), "
    "status TEXT NOT NULL CHECK(status IN ('available', 'used_up', 'discarded')), note TEXT, "
    "version INTEGER NOT NULL CHECK(version > 0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS leftovers_status_use_by ON leftovers(status, use_by, prepared_on)",
    "CREATE TABLE IF NOT EXISTS leftover_events ("
    "event_id TEXT PRIMARY KEY, leftover_id TEXT NOT NULL REFERENCES leftovers(leftover_id), version INTEGER NOT NULL, "
    "before_json TEXT, after_json TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, recorded_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS menu_plans ("
    "plan_id TEXT PRIMARY KEY, planned_date TEXT NOT NULL, meal_type TEXT NOT NULL "
    "CHECK(meal_type IN ('breakfast', 'lunch', 'dinner', 'snack', 'other')), "
    "recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id), recipe_version INTEGER NOT NULL, "
    "servings INTEGER NOT NULL CHECK(servings > 0), meal_id TEXT REFERENCES entries(id), "
    "version INTEGER NOT NULL CHECK(version > 0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "FOREIGN KEY(recipe_id, recipe_version) REFERENCES recipe_versions(recipe_id, version))",
    "CREATE UNIQUE INDEX IF NOT EXISTS menu_plans_meal_id ON menu_plans(meal_id) WHERE meal_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS menu_plans_date_meal ON menu_plans(planned_date, meal_type)",
    "CREATE TABLE IF NOT EXISTS shopping_lists ("
    "shopping_list_id TEXT PRIMARY KEY, plan_ids_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS shopping_list_items ("
    "item_id TEXT PRIMARY KEY, shopping_list_id TEXT NOT NULL REFERENCES shopping_lists(shopping_list_id) ON DELETE CASCADE, "
    "ingredient TEXT NOT NULL, ingredient_key TEXT NOT NULL, unit TEXT, unit_key TEXT NOT NULL, "
    "required_quantity TEXT, known_required_quantity TEXT NOT NULL, available_quantity TEXT NOT NULL, "
    "known_quantity_to_buy TEXT NOT NULL, quantity_known INTEGER NOT NULL CHECK(quantity_known IN (0, 1)), "
    "unknown_source_count INTEGER NOT NULL CHECK(unknown_source_count >= 0), sources_json TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('needed', 'bought')), version INTEGER NOT NULL CHECK(version > 0), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS shopping_list_items_list ON shopping_list_items(shopping_list_id, ingredient_key, unit_key)",
]


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quantity(value: Any, unit: str, field: str, *, allow_negative: bool = False) -> tuple[Decimal, str]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise BusinessError("INVALID_INPUT", f"{field} must be a finite decimal number", {"field": field})
    try:
        normalized = _normalize_quantity(str(value), unit)
    except (InvalidOperation, TypeError, ValueError):
        normalized = None
    if normalized is None:
        raise BusinessError("INVALID_INPUT", f"{field} and unit must describe a finite quantity", {"field": field})
    quantity, normalized_unit = normalized
    if not allow_negative and quantity < 0:
        raise BusinessError("INVALID_INPUT", f"{field} cannot be negative", {"field": field})
    return quantity, normalized_unit


class KitchenMixin:
    @staticmethod
    def _kitchen_reject_fields(payload: dict[str, Any], allowed: set[str]) -> None:
        extra = next((name for name in payload if name not in allowed), None)
        if extra is not None:
            raise BusinessError("INVALID_INPUT", "payload contains unsupported fields", {"field": extra})

    @staticmethod
    def _kitchen_decimal(value: Decimal) -> str:
        return _decimal_text(value)

    @staticmethod
    def _status_date(value: Any, field: str) -> str:
        return date.today().isoformat() if value is None else KitchenMixin._date_value(value, field)

    @staticmethod
    def _expiry_after(start: str, days: int) -> str:
        end = min(date.fromisoformat(start).toordinal() + days, date.max.toordinal())
        return date.fromordinal(end).isoformat()

    @staticmethod
    def _date_value(value: Any, field: str) -> str:
        text = value.strip() if isinstance(value, str) else ""
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", f"{field} must be an ISO date", {"field": field}) from exc

    @staticmethod
    def _page_values(payload: dict[str, Any]) -> tuple[int, int]:
        limit = payload.get("limit", 50)
        offset = payload.get("offset", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise BusinessError("INVALID_INPUT", "limit must be an integer from 1 to 200", {"field": "limit"})
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise BusinessError("INVALID_INPUT", "offset must be a non-negative integer", {"field": "offset"})
        return limit, offset

    @staticmethod
    def _expect_version(row: sqlite3.Row | None, expected: int, kind: str, record_id: str) -> None:
        if row is None:
            raise BusinessError("NOT_FOUND", f"{kind} was not found", {"id": record_id})
        if row["version"] != expected:
            raise BusinessError("VERSION_CONFLICT", f"{kind} has changed", {"id": record_id, "current_version": row["version"]})

    @staticmethod
    def _inventory_record(row: sqlite3.Row, as_of: str) -> dict[str, Any]:
        item = dict(row)
        item["expired"] = item["expires_on"] is not None and item["expires_on"] < as_of
        return item

    def _shelf_life_key(self, ingredient: str, location: str | None) -> tuple[str, str]:
        return self._ingredient_key(ingredient), self._ingredient_key(location or "")

    def _shelf_life_days(self, value: Any) -> int | None:
        days = self._integer(value, "shelf_life_days", nullable=True, minimum=0)
        if days is not None and days > _MAX_SHELF_LIFE:
            raise BusinessError("INVALID_INPUT", "保质天数超出日期范围。", {"field": "shelf_life_days"})
        return days

    @staticmethod
    def _saved_shelf_life(connection: sqlite3.Connection, key: tuple[str, str]) -> int | None:
        row = connection.execute("SELECT days FROM inventory_shelf_lives WHERE ingredient_key = ? AND location_key = ?", key).fetchone()
        return row["days"] if row is not None else None

    def _inventory_shelf_life_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"ingredient", "location", "recorded_on"})
        ingredient = self._string(payload.get("ingredient"), "ingredient")
        location = self._string(payload.get("location"), "location", nullable=True)
        recorded_on = self._status_date(payload.get("recorded_on"), "recorded_on")
        with self.store.read() as connection:
            days = self._saved_shelf_life(connection, self._shelf_life_key(ingredient, location))
        return {"ingredient": ingredient, "location": location, "recorded_on": recorded_on,
                "shelf_life_days": days, "expires_on": self._expiry_after(recorded_on, days) if days is not None else None}

    def _inventory_shelf_life_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "ingredient", "location", "shelf_life_days"})
        if "shelf_life_days" not in payload:
            raise BusinessError("INVALID_INPUT", "需要提供保质天数；传 null 可清除已保存的记录。")
        ingredient = self._string(payload.get("ingredient"), "ingredient")
        location = self._string(payload.get("location"), "location", nullable=True)
        days = self._shelf_life_days(payload["shelf_life_days"])
        key = self._shelf_life_key(ingredient, location)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            if days is None:
                connection.execute("DELETE FROM inventory_shelf_lives WHERE ingredient_key = ? AND location_key = ?", key)
            else:
                self._save_shelf_life(connection, key, days)
            return {"ingredient": ingredient, "location": location, "shelf_life_days": days}

        return self._write("inventory_shelf_life_set", payload, operation)

    @staticmethod
    def _save_shelf_life(connection: sqlite3.Connection, key: tuple[str, str], days: int) -> None:
        connection.execute(
            "INSERT INTO inventory_shelf_lives(ingredient_key, location_key, days) VALUES (?, ?, ?) "
            "ON CONFLICT(ingredient_key, location_key) DO UPDATE SET days = excluded.days",
            (*key, days),
        )

    def _inventory_batch_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "batch_id", "expected_version", "ingredient", "quantity", "unit", "source", *STOCK_EXPIRY_FIELDS})
        batch_id = self._string(payload.get("batch_id"), "batch_id", nullable=True)
        expected_version = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        if batch_id is None and expected_version is not None:
            raise BusinessError("INVALID_INPUT", "expected_version requires batch_id", {"field": "expected_version"})
        ingredient = " ".join(self._string(payload.get("ingredient"), "ingredient").split())
        quantity, unit = _quantity(payload.get("quantity"), self._string(payload.get("unit"), "unit"), "quantity")
        location = self._string(payload.get("location"), "location", nullable=True)
        supplied_expiry = payload.get("expires_on")
        if supplied_expiry is not None:
            supplied_expiry = self._date_value(supplied_expiry, "expires_on")
        recorded_on = self._status_date(payload.get("recorded_on"), "recorded_on")
        days = self._shelf_life_days(payload.get("shelf_life_days"))
        shelf_life_key = self._shelf_life_key(ingredient, location)
        source = self._string(payload.get("source"), "source")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_id = batch_id or str(uuid.uuid4())
            row = connection.execute("SELECT * FROM inventory_batches WHERE batch_id = ?", (current_id,)).fetchone()
            now = self._now()
            quantity_text = _decimal_text(quantity)
            expires_on = supplied_expiry
            if row is None:
                if expected_version is not None:
                    raise BusinessError("NOT_FOUND", "inventory batch was not found", {"id": current_id})
                if expires_on is None:
                    duration = days if days is not None else self._saved_shelf_life(connection, shelf_life_key)
                    if duration is not None:
                        expires_on = self._expiry_after(recorded_on, duration)
                    elif quantity > 0:
                        raise BusinessError("SHELF_LIFE_REQUIRED", "请填写到期日，或填写保质天数供这次和以后入库使用。", {
                            "ingredient": ingredient, "location": location, "recorded_on": recorded_on,
                        })
                connection.execute(
                    "INSERT INTO inventory_batches(batch_id, ingredient, quantity, unit, location, expires_on, source, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (current_id, ingredient, quantity_text, unit, location, expires_on, source, now, now),
                )
                action, version, before = "created", 1, None
            else:
                self._expect_version(row, expected_version, "inventory batch", current_id)
                if "expires_on" not in payload:
                    expires_on = row["expires_on"]
                held = self._reserved_quantity(connection, current_id, date.today().isoformat())
                if held and (quantity < held or (ingredient, unit, location, expires_on) != (
                    row["ingredient"], row["unit"], row["location"], row["expires_on"],
                )):
                    raise BusinessError("STOCK_RESERVED", "Release the batch reservations before changing reserved stock")
                version = row["version"] + 1
                before = row["quantity"]
                action = "updated"
                connection.execute(
                    "UPDATE inventory_batches SET ingredient = ?, quantity = ?, unit = ?, location = ?, expires_on = ?, source = ?, version = ?, updated_at = ? "
                    "WHERE batch_id = ?",
                    (ingredient, quantity_text, unit, location, expires_on, source, version, now, current_id),
                )
            connection.execute(
                "INSERT INTO inventory_batch_events(event_id, batch_id, version, action, quantity_before, quantity_after, source, request_id, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), current_id, version, action, before, quantity_text, source, payload["request_id"], now),
            )
            if days is not None:
                self._save_shelf_life(connection, shelf_life_key, days)
            return {"batch_id": current_id, "version": version, "quantity": quantity_text, "unit": unit,
                    "action": action, "expires_on": expires_on}

        return self._write("inventory_batch_upsert", payload, operation)

    def _inventory_batch_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"ingredient", "location", "as_of", "limit", "offset"})
        ingredient = self._string(payload.get("ingredient"), "ingredient", nullable=True)
        location = self._string(payload.get("location"), "location", nullable=True)
        as_of = self._status_date(payload.get("as_of"), "as_of")
        limit, offset = self._page_values(payload)
        clauses, values = [], []
        if ingredient is not None:
            clauses.append("ingredient = ? COLLATE NOCASE")
            values.append(ingredient)
        if location is not None:
            clauses.append("location = ? COLLATE NOCASE")
            values.append(location)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM inventory_batches{where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM inventory_batches{where} ORDER BY expires_on IS NULL, expires_on, created_at, batch_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = self._inventory_record(row, as_of)
                held = self._reserved_quantity(connection, row["batch_id"], as_of)
                available = Decimal(0) if item["expired"] else max(Decimal(0), Decimal(row["quantity"]) - held)
                items.append({**item, "reserved_quantity": _decimal_text(held), "available_quantity": _decimal_text(available)})
        return {"items": items, "total": total, "as_of": as_of, "limit": limit, "offset": offset}

    def _inventory_batch_adjust(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "batch_id", "expected_version", "delta_quantity", "unit", "source"})
        batch_id = self._string(payload.get("batch_id"), "batch_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        delta, unit = _quantity(payload.get("delta_quantity"), self._string(payload.get("unit"), "unit"), "delta_quantity", allow_negative=True)
        if delta == 0:
            raise BusinessError("INVALID_INPUT", "delta_quantity must not be zero", {"field": "delta_quantity"})
        source = self._string(payload.get("source"), "source")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM inventory_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            self._expect_version(row, expected_version, "inventory batch", batch_id)
            if unit.casefold() != row["unit"].casefold():
                raise BusinessError("INVALID_INPUT", "unit is incompatible with this inventory batch", {"field": "unit", "batch_unit": row["unit"], "adjustment_unit": unit})
            before = Decimal(row["quantity"])
            after = before + delta
            if after < self._reserved_quantity(connection, batch_id, date.today().isoformat()):
                raise BusinessError("INSUFFICIENT_INVENTORY", "inventory batch quantity cannot fall below zero", {"batch_id": batch_id, "available_quantity": row["quantity"], "unit": row["unit"]})
            version = row["version"] + 1
            now = self._now()
            after_text = _decimal_text(after)
            connection.execute(
                "UPDATE inventory_batches SET quantity = ?, source = ?, version = ?, updated_at = ? WHERE batch_id = ?",
                (after_text, source, version, now, batch_id),
            )
            connection.execute(
                "INSERT INTO inventory_batch_events(event_id, batch_id, version, action, quantity_before, quantity_after, source, request_id, recorded_at) "
                "VALUES (?, ?, ?, 'adjusted', ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), batch_id, version, _decimal_text(before), after_text, source, payload["request_id"], now),
            )
            return {"batch_id": batch_id, "version": version, "quantity": after_text, "unit": row["unit"], "source": source}

        return self._write("inventory_batch_adjust", payload, operation)

    @staticmethod
    def _leftover_record(row: sqlite3.Row, as_of: str) -> dict[str, Any]:
        item = dict(row)
        item["expired"] = item["use_by"] is not None and item["use_by"] < as_of and item["status"] == "available"
        return item

    def _leftover_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "leftover_id", "expected_version", "name", "servings", "prepared_on", "use_by", "source_meal_id", "status", "note"})
        leftover_id = self._string(payload.get("leftover_id"), "leftover_id", nullable=True)
        expected_version = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        if leftover_id is None and expected_version is not None:
            raise BusinessError("INVALID_INPUT", "expected_version requires leftover_id", {"field": "expected_version"})
        name = self._string(payload.get("name"), "name")
        servings = self._integer(payload.get("servings"), "servings", minimum=0)
        prepared_on = self._date_value(payload.get("prepared_on"), "prepared_on")
        supplied_use_by = payload.get("use_by")
        if supplied_use_by is not None:
            supplied_use_by = self._date_value(supplied_use_by, "use_by")
        source_meal_id = self._string(payload.get("source_meal_id"), "source_meal_id", nullable=True)
        status = self._string(payload.get("status"), "status")
        if status not in {"available", "used_up", "discarded"}:
            raise BusinessError("INVALID_INPUT", "status is unsupported", {"field": "status"})
        if (status == "available" and servings == 0) or (status == "used_up" and servings != 0):
            raise BusinessError("INVALID_INPUT", "available leftovers need servings; used_up leftovers must have zero servings", {"field": "servings"})
        note = self._string(payload.get("note"), "note", nullable=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_id = leftover_id or str(uuid.uuid4())
            if source_meal_id is not None:
                meal = connection.execute("SELECT record_kind FROM entries WHERE id = ?", (source_meal_id,)).fetchone()
                if meal is None or meal["record_kind"] != "meal":
                    raise BusinessError("NOT_FOUND", "source meal was not found", {"meal_id": source_meal_id})
            row = connection.execute("SELECT * FROM leftovers WHERE leftover_id = ?", (current_id,)).fetchone()
            now = self._now()
            use_by = supplied_use_by
            if row is None and use_by is None and status == "available":
                use_by = self._expiry_after(prepared_on, 1)
            elif row is not None and "use_by" not in payload:
                use_by = row["use_by"]
            if use_by is not None and use_by < prepared_on:
                raise BusinessError("INVALID_INPUT", "use_by cannot precede prepared_on", {"field": "use_by"})
            after = {"name": name, "servings": servings, "prepared_on": prepared_on, "use_by": use_by,
                     "source_meal_id": source_meal_id, "status": status, "note": note}
            if row is None:
                if expected_version is not None:
                    raise BusinessError("NOT_FOUND", "leftover was not found", {"id": current_id})
                version, before = 1, None
                connection.execute(
                    "INSERT INTO leftovers(leftover_id, name, servings, prepared_on, use_by, source_meal_id, status, note, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (current_id, name, servings, prepared_on, use_by, source_meal_id, status, note, now, now),
                )
            else:
                self._expect_version(row, expected_version, "leftover", current_id)
                version = row["version"] + 1
                before = {key: row[key] for key in after}
                connection.execute(
                    "UPDATE leftovers SET name = ?, servings = ?, prepared_on = ?, use_by = ?, source_meal_id = ?, status = ?, note = ?, version = ?, updated_at = ? "
                    "WHERE leftover_id = ?",
                    (name, servings, prepared_on, use_by, source_meal_id, status, note, version, now, current_id),
                )
            connection.execute(
                "INSERT INTO leftover_events(event_id, leftover_id, version, before_json, after_json, request_id, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), current_id, version, None if before is None else self._json(before), self._json(after), payload["request_id"], now),
            )
            return {"leftover_id": current_id, "version": version, **after}

        return self._write("leftover_upsert", payload, operation)

    def _leftover_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"status", "source_meal_id", "as_of", "limit", "offset"})
        status = self._string(payload.get("status"), "status", nullable=True)
        if status is not None and status not in {"available", "used_up", "discarded"}:
            raise BusinessError("INVALID_INPUT", "status is unsupported", {"field": "status"})
        source_meal_id = self._string(payload.get("source_meal_id"), "source_meal_id", nullable=True)
        as_of = self._status_date(payload.get("as_of"), "as_of")
        limit, offset = self._page_values(payload)
        clauses, values = [], []
        if status is not None:
            clauses.append("l.status = ?")
            values.append(status)
        if source_meal_id is not None:
            clauses.append("l.source_meal_id = ?")
            values.append(source_meal_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM leftovers l{where}", values).fetchone()[0]
            rows = connection.execute(
                "SELECT l.*, CASE WHEN e.id IS NULL THEN NULL WHEN e.voided_at IS NULL THEN 'active' ELSE 'void' END AS source_meal_status, "
                f"e.version AS source_meal_version FROM leftovers l LEFT JOIN entries e ON e.id = l.source_meal_id{where} "
                "ORDER BY l.prepared_on DESC, l.created_at DESC, l.leftover_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {"items": [self._leftover_record(row, as_of) for row in rows], "total": total, "as_of": as_of, "limit": limit, "offset": offset}

    def _leftover_history_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"leftover_id", "limit", "offset"})
        leftover_id = self._string(payload.get("leftover_id"), "leftover_id")
        limit, offset = self._page_values(payload)
        with self.store.read() as connection:
            if connection.execute("SELECT 1 FROM leftovers WHERE leftover_id = ?", (leftover_id,)).fetchone() is None:
                raise BusinessError("NOT_FOUND", "leftover was not found", {"id": leftover_id})
            total = connection.execute("SELECT COUNT(*) FROM leftover_events WHERE leftover_id = ?", (leftover_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM leftover_events WHERE leftover_id = ? ORDER BY version DESC LIMIT ? OFFSET ?",
                (leftover_id, limit, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["before"] = None if item.pop("before_json") is None else json.loads(row["before_json"])
            item["after"] = json.loads(item.pop("after_json"))
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _menu_plan_record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        meal = connection.execute("SELECT record_kind, voided_at, version FROM entries WHERE id = ?", (item["meal_id"],)).fetchone() if item["meal_id"] else None
        recipe = connection.execute("SELECT version FROM recipes WHERE recipe_id = ?", (item["recipe_id"],)).fetchone()
        item["recipe_current_version"] = recipe["version"] if recipe else None
        item["recipe_revision_current"] = bool(recipe and recipe["version"] == item["recipe_version"])
        item["linked_meal_status"] = None if meal is None else ("void" if meal["voided_at"] is not None else "active")
        item["linked_meal_version"] = None if meal is None else meal["version"]
        return item

    def _menu_plan_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "plan_id", "expected_version", "planned_date", "meal_type", "recipe_id", "servings", "meal_id"})
        plan_id = self._string(payload.get("plan_id"), "plan_id", nullable=True)
        expected_version = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        if plan_id is None and expected_version is not None:
            raise BusinessError("INVALID_INPUT", "expected_version requires plan_id", {"field": "expected_version"})
        planned_date = self._date_value(payload.get("planned_date"), "planned_date")
        meal_type = self._string(payload.get("meal_type"), "meal_type")
        if meal_type not in _MEAL_TYPES:
            raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        servings = self._integer(payload.get("servings"), "servings", minimum=1)
        meal_id = self._string(payload.get("meal_id"), "meal_id", nullable=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current_id = plan_id or str(uuid.uuid4())
            row = connection.execute("SELECT * FROM menu_plans WHERE plan_id = ?", (current_id,)).fetchone()
            if row is None and expected_version is not None:
                raise BusinessError("NOT_FOUND", "menu plan was not found", {"id": current_id})
            if row is not None:
                self._expect_version(row, expected_version, "menu plan", current_id)
            recipe = connection.execute("SELECT version FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
            if recipe is None:
                raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
            recipe_version = recipe["version"]
            if connection.execute("SELECT 1 FROM recipe_versions WHERE recipe_id = ? AND version = ?", (recipe_id, recipe_version)).fetchone() is None:
                raise BusinessError("NOT_FOUND", "current recipe revision was not found", {"recipe_id": recipe_id, "version": recipe_version})
            if meal_id is not None:
                meal = connection.execute("SELECT record_kind, voided_at FROM entries WHERE id = ?", (meal_id,)).fetchone()
                if meal is None or meal["record_kind"] != "meal":
                    raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
                if meal["voided_at"] is not None:
                    raise BusinessError("INVALID_MEAL_LINK", "only an active meal can be linked", {"id": meal_id})
                other = connection.execute("SELECT plan_id FROM menu_plans WHERE meal_id = ? AND plan_id <> ?", (meal_id, current_id)).fetchone()
                if other is not None:
                    raise BusinessError("MEAL_ALREADY_LINKED", "meal is already linked to another menu plan", {"id": meal_id, "plan_id": other["plan_id"]})
            now = self._now()
            if row is None:
                version = 1
                connection.execute(
                    "INSERT INTO menu_plans(plan_id, planned_date, meal_type, recipe_id, recipe_version, servings, meal_id, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (current_id, planned_date, meal_type, recipe_id, recipe_version, servings, meal_id, now, now),
                )
                action = "created"
            else:
                version = row["version"] + 1
                if planned_date != row["planned_date"] and connection.execute(
                    "SELECT 1 FROM stock_reservations WHERE plan_id=? AND valid_until>=?", (current_id, date.today().isoformat()),
                ).fetchone():
                    raise BusinessError("PLAN_HAS_STOCK_RESERVATIONS", "Use menu_plan_reschedule to release the old reservations")
                connection.execute(
                    "UPDATE menu_plans SET planned_date = ?, meal_type = ?, recipe_id = ?, recipe_version = ?, servings = ?, meal_id = ?, version = ?, updated_at = ? "
                    "WHERE plan_id = ?",
                    (planned_date, meal_type, recipe_id, recipe_version, servings, meal_id, version, now, current_id),
                )
                action = "updated"
            saved = connection.execute("SELECT * FROM menu_plans WHERE plan_id = ?", (current_id,)).fetchone()
            return {**self._menu_plan_record(connection, saved), "action": action}

        return self._write("menu_plan_upsert", payload, operation)

    def _menu_plan_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"date_from", "date_to", "meal_type", "limit", "offset"})
        date_from = self._string(payload.get("date_from"), "date_from", nullable=True)
        date_to = self._string(payload.get("date_to"), "date_to", nullable=True)
        if date_from is not None:
            date_from = self._date_value(date_from, "date_from")
        if date_to is not None:
            date_to = self._date_value(date_to, "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise BusinessError("INVALID_INPUT", "date_from cannot be after date_to", {"field": "date_from"})
        meal_type = self._string(payload.get("meal_type"), "meal_type", nullable=True)
        if meal_type is not None and meal_type not in _MEAL_TYPES:
            raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
        limit, offset = self._page_values(payload)
        clauses, values = [], []
        if date_from is not None:
            clauses.append("planned_date >= ?")
            values.append(date_from)
        if date_to is not None:
            clauses.append("planned_date <= ?")
            values.append(date_to)
        if meal_type is not None:
            clauses.append("meal_type = ?")
            values.append(meal_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM menu_plans{where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM menu_plans{where} ORDER BY planned_date, CASE meal_type WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 WHEN 'snack' THEN 3 ELSE 4 END, plan_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            items = [self._menu_plan_record(connection, row) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _ingredient_key(name: str) -> str:
        return " ".join(name.split()).casefold()

    def _shopping_list_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "plan_ids"})
        plan_ids = self._list(payload.get("plan_ids"), "plan_ids")
        if not 1 <= len(plan_ids) <= 100:
            raise BusinessError("INVALID_INPUT", "plan_ids must contain 1 to 100 items", {"field": "plan_ids"})
        plan_ids = [self._string(value, f"plan_ids[{index}]") for index, value in enumerate(plan_ids)]
        if len(set(plan_ids)) != len(plan_ids):
            raise BusinessError("INVALID_INPUT", "plan_ids must not contain duplicates", {"field": "plan_ids"})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            plans = []
            for plan_id in plan_ids:
                row = connection.execute("SELECT * FROM menu_plans WHERE plan_id = ?", (plan_id,)).fetchone()
                if row is None:
                    raise BusinessError("NOT_FOUND", "menu plan was not found", {"id": plan_id})
                plan = dict(row)
                scaled = self._scaled_recipe(connection, plan["recipe_id"], plan["servings"])
                plan["current_recipe_version"] = scaled["recipe_version"]
                plan["recipe_revision_current"] = scaled["recipe_version"] == plan["recipe_version"]
                plans.append((plan, scaled))

            groups: dict[tuple[str, str], dict[str, Any]] = {}
            for plan, scaled in plans:
                stale_revision = not plan["recipe_revision_current"]
                for ingredient in scaled["ingredients"]:
                    name = ingredient["name"].strip()
                    raw_quantity = ingredient["scaled_quantity"]
                    normalized = None if stale_revision or raw_quantity is None else _normalize_quantity(raw_quantity, ingredient["unit"])
                    known = bool(ingredient["quantity_known"] and normalized is not None)
                    quantity, unit = normalized if normalized is not None else (None, ingredient["unit"])
                    ingredient_key = self._ingredient_key(name)
                    unit_key = "" if unit is None else unit.casefold()
                    key = ingredient_key, unit_key
                    group = groups.setdefault(
                        key,
                        {"ingredient": name, "ingredient_key": ingredient_key, "unit": unit, "unit_key": unit_key, "required": Decimal(0), "unknown_source_count": 0, "demands": [], "sources": []},
                    )
                    group["sources"].append(
                        {
                            "plan_id": plan["plan_id"],
                            "planned_date": plan["planned_date"],
                            "recipe_id": plan["recipe_id"],
                            "planned_recipe_version": plan["recipe_version"],
                            "current_recipe_version": scaled["recipe_version"],
                            "target_servings": plan["servings"],
                            "original_quantity": ingredient["original_quantity"],
                            "scaled_quantity": ingredient["scaled_quantity"] if not stale_revision else None,
                            "unit": ingredient["unit"],
                            "quantity_known": known,
                            "needs_recipe_refresh": stale_revision,
                        }
                    )
                    if known:
                        group["required"] += quantity
                        group["demands"].append((max(plan["planned_date"], date.today().isoformat()), plan["plan_id"], quantity))
                    else:
                        group["unknown_source_count"] += 1

            shopping_list_id = str(uuid.uuid4())
            created_at = self._now()
            connection.execute(
                "INSERT INTO shopping_lists(shopping_list_id, plan_ids_json, created_at) VALUES (?, ?, ?)",
                (shopping_list_id, self._json(plan_ids), created_at),
            )
            items = []
            for group in groups.values():
                batches = connection.execute(
                    "SELECT batch_id, quantity, unit, expires_on FROM inventory_batches WHERE ingredient = ? COLLATE NOCASE "
                    "AND unit = ? COLLATE NOCASE AND CAST(quantity AS REAL) > 0 "
                    "ORDER BY expires_on IS NULL, expires_on, created_at, batch_id",
                    (group["ingredient"], group["unit"] or ""),
                ).fetchall()
                stock = []
                for batch in batches:
                    reserved = [dict(row) for row in connection.execute(
                        "SELECT plan_id,quantity,valid_until FROM stock_reservations WHERE batch_id=? AND valid_until>=?",
                        (batch["batch_id"], date.today().isoformat()),
                    )]
                    for hold in reserved:
                        hold["quantity"] = Decimal(hold["quantity"])
                    stock.append({"quantity": max(0, Decimal(batch["quantity"]) - sum(hold["quantity"] for hold in reserved)),
                                  "expires_on": batch["expires_on"], "reserved": reserved})
                demands = sorted(group["demands"], key=lambda item: item[0])
                covered = Decimal(0)
                for planned_date, plan_id, required in demands:
                    remaining = required
                    for batch in stock:
                        if batch["expires_on"] is not None and batch["expires_on"] < planned_date:
                            continue
                        for hold in batch["reserved"]:
                            if hold["plan_id"] == plan_id and hold["valid_until"] >= planned_date:
                                used = min(remaining, hold["quantity"])
                                hold["quantity"] -= used
                                remaining -= used
                                covered += used
                    for batch in stock:
                        if remaining == 0:
                            break
                        if batch["quantity"] <= 0 or (batch["expires_on"] is not None and batch["expires_on"] < planned_date):
                            continue
                        used = min(remaining, batch["quantity"])
                        batch["quantity"] -= used
                        remaining -= used
                        covered += used
                        if remaining == 0:
                            break
                known_quantity_to_buy = max(Decimal(0), group["required"] - covered)
                quantity_known = group["unknown_source_count"] == 0
                item_id = str(uuid.uuid4())
                required_text = _decimal_text(group["required"]) if quantity_known else None
                item = {
                    "item_id": item_id,
                    "ingredient": group["ingredient"],
                    "unit": group["unit"],
                    "required_quantity": required_text,
                    "known_required_quantity": _decimal_text(group["required"]),
                    "available_quantity": _decimal_text(covered),
                    "quantity_to_buy": _decimal_text(known_quantity_to_buy) if quantity_known else None,
                    "known_quantity_to_buy": _decimal_text(known_quantity_to_buy),
                    "quantity_known": quantity_known,
                    "unknown_source_count": group["unknown_source_count"],
                    "sources": group["sources"],
                    "status": "needed",
                    "version": 1,
                }
                connection.execute(
                    "INSERT INTO shopping_list_items(item_id, shopping_list_id, ingredient, ingredient_key, unit, unit_key, required_quantity, known_required_quantity, available_quantity, known_quantity_to_buy, quantity_known, unknown_source_count, sources_json, status, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'needed', 1, ?, ?)",
                    (
                        item_id, shopping_list_id, group["ingredient"], group["ingredient_key"], group["unit"], group["unit_key"],
                        required_text, _decimal_text(group["required"]), _decimal_text(covered), _decimal_text(known_quantity_to_buy),
                        int(quantity_known), group["unknown_source_count"], self._json(group["sources"]), created_at, created_at,
                    ),
                )
                items.append(item)
            return {"shopping_list_id": shopping_list_id, "plan_ids": plan_ids, "created_at": created_at, "items": items}

        return self._write("shopping_list_build", payload, operation)

    def _shopping_list_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"shopping_list_id"})
        shopping_list_id = self._string(payload.get("shopping_list_id"), "shopping_list_id")
        with self.store.read() as connection:
            row = connection.execute("SELECT * FROM shopping_lists WHERE shopping_list_id = ?", (shopping_list_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "shopping list was not found", {"id": shopping_list_id})
            items = connection.execute(
                "SELECT * FROM shopping_list_items WHERE shopping_list_id = ? ORDER BY ingredient_key, unit_key, item_id",
                (shopping_list_id,),
            ).fetchall()
            purchases = {}
            for purchase in connection.execute(
                "SELECT p.* FROM shopping_purchases p JOIN shopping_list_items i ON i.item_id=p.item_id WHERE i.shopping_list_id=? ORDER BY p.created_at,p.id",
                (shopping_list_id,),
            ):
                purchases.setdefault(purchase["item_id"], []).append(dict(purchase))
        progress = {item["item_id"]: self._shopping_progress(item, purchases.get(item["item_id"], [])) for item in items}
        return {
            "shopping_list_id": row["shopping_list_id"],
            "plan_ids": json.loads(row["plan_ids_json"]),
            "created_at": row["created_at"],
            "items": [
                {
                    "item_id": item["item_id"],
                    "ingredient": item["ingredient"],
                    "unit": item["unit"],
                    "required_quantity": item["required_quantity"],
                    "known_required_quantity": item["known_required_quantity"],
                    "available_quantity": item["available_quantity"],
                    "quantity_to_buy": item["known_quantity_to_buy"] if item["quantity_known"] else None,
                    "known_quantity_to_buy": item["known_quantity_to_buy"],
                    "quantity_known": bool(item["quantity_known"]),
                    "unknown_source_count": item["unknown_source_count"],
                    "sources": json.loads(item["sources_json"]),
                    "status": item["status"],
                    "purchases": purchases.get(item["item_id"], []),
                    "quantity_purchased": _decimal_text(progress[item["item_id"]][0]),
                    "remaining_quantity": (_decimal_text(progress[item["item_id"]][1])
                                           if progress[item["item_id"]][1] is not None else None),
                    "version": item["version"],
                    "updated_at": item["updated_at"],
                }
                for item in items
            ],
        }

    def _shopping_list_mark_bought(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._kitchen_reject_fields(payload, {"request_id", "shopping_list_id", "item_id", "expected_version", "bought", "quantity", "unit", *STOCK_EXPIRY_FIELDS})
        shopping_list_id = self._string(payload.get("shopping_list_id"), "shopping_list_id")
        item_id = self._string(payload.get("item_id"), "item_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        bought = payload.get("bought")
        if not isinstance(bought, bool):
            raise BusinessError("INVALID_INPUT", "bought must be a boolean", {"field": "bought"})
        explicit_amount = "quantity" in payload or "unit" in payload
        if explicit_amount and (not bought or not {"quantity", "unit"} <= payload.keys()):
            raise BusinessError("INVALID_INPUT", "A bought quantity and unit must be provided together")
        status = "bought" if bought else "needed"

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM shopping_list_items WHERE shopping_list_id = ? AND item_id = ?",
                (shopping_list_id, item_id),
            ).fetchone()
            self._expect_version(row, expected_version, "shopping-list item", item_id)
            if row["status"] == status:
                return {"shopping_list_id": shopping_list_id, "item_id": item_id, "status": status, "version": row["version"], "changed": False, "inventory_updated": False}
            if bought:
                purchases = connection.execute("SELECT quantity, unit FROM shopping_purchases WHERE item_id = ?", (item_id,)).fetchall()
                _, remaining = self._shopping_progress(row, purchases)
                if explicit_amount or (remaining is not None and remaining > 0):
                    purchase = self._shopping_purchase_record({
                        "request_id": self._subrequest(payload["request_id"], 0), "shopping_list_id": shopping_list_id,
                        "item_id": item_id, "expected_version": expected_version, "source": "购物清单",
                        "quantity": payload["quantity"] if explicit_amount else _decimal_text(remaining),
                        "unit": payload["unit"] if explicit_amount else row["unit"],
                        **{field: payload[field] for field in STOCK_EXPIRY_FIELDS if field in payload},
                    }, complete_unknown_quantity=True)
                    return {"shopping_list_id": shopping_list_id, "changed": True, "inventory_updated": True, **purchase}
                if remaining is None and not purchases:
                    raise BusinessError("PURCHASE_QUANTITY_REQUIRED", "请填写实买数量和单位后再入库。")
            version = row["version"] + 1
            now = self._now()
            connection.execute(
                "UPDATE shopping_list_items SET status = ?, version = ?, updated_at = ? WHERE item_id = ?",
                (status, version, now, item_id),
            )
            return {"shopping_list_id": shopping_list_id, "item_id": item_id, "status": status, "version": version, "changed": True, "inventory_updated": False}

        return self._write("shopping_list_mark_bought", payload, operation)

    @staticmethod
    def _kitchen_alerts(connection: sqlite3.Connection, as_of: str, location: str | None = None) -> dict:
        location_clause = " AND location = ? COLLATE NOCASE" if location is not None else ""
        stock = connection.execute(
            "SELECT batch_id, ingredient, quantity, unit, location, expires_on FROM inventory_batches "
            f"WHERE expires_on < ? AND CAST(quantity AS REAL) > 0{location_clause} ORDER BY expires_on, batch_id",
            (as_of, location) if location is not None else (as_of,),
        ).fetchall()
        leftovers = connection.execute(
            "SELECT leftover_id, name, servings, use_by FROM leftovers "
            "WHERE status = 'available' AND servings > 0 AND use_by < ? ORDER BY use_by, leftover_id",
            (as_of,),
        ).fetchall()
        if not stock and not leftovers:
            return {}
        return {"kitchen_alerts": {
            "code": "KITCHEN_FOOD_EXPIRED", "as_of": as_of,
            "message": f"有 {len(stock)} 批食材和 {len(leftovers)} 条剩菜超过登记的使用期限，已从可用库存中排除。",
            "expired_inventory": [dict(row) for row in stock],
            "expired_leftovers": [dict(row) for row in leftovers],
        }}

    def _available_kitchen_context(
        self, connection: sqlite3.Connection, as_of: str, location: str | None = None
    ) -> dict[str, Any]:
        location_clause = " AND location = ? COLLATE NOCASE" if location is not None else ""
        values = (as_of, location) if location is not None else (as_of,)
        stock_rows = connection.execute(
            "SELECT ingredient, quantity, unit, location, expires_on, source, batch_id FROM inventory_batches "
            f"WHERE CAST(quantity AS REAL) > 0 AND (expires_on IS NULL OR expires_on >= ?){location_clause} "
            "ORDER BY ingredient COLLATE NOCASE, unit COLLATE NOCASE, expires_on IS NULL, expires_on, created_at, batch_id",
            values,
        ).fetchall()
        stock: dict[tuple[str, str], dict[str, Any]] = {}
        for row in stock_rows:
            ingredient = row["ingredient"]
            key = self._ingredient_key(ingredient), row["unit"].casefold()
            available = max(0, Decimal(row["quantity"]) - self._reserved_quantity(connection, row["batch_id"], as_of))
            if not available:
                continue
            item = stock.setdefault(key, {"ingredient": ingredient, "quantity": Decimal(0), "unit": row["unit"], "batches": []})
            item["quantity"] += available
            item["batches"].append({"batch_id": row["batch_id"], "location": row["location"], "expires_on": row["expires_on"], "source": row["source"],
                                    "available_quantity": _decimal_text(available)})
        leftovers = connection.execute(
            "SELECT l.leftover_id, l.name, l.servings, l.prepared_on, l.use_by, l.source_meal_id, l.status, "
            "CASE WHEN e.id IS NULL THEN NULL WHEN e.voided_at IS NULL THEN 'active' ELSE 'void' END AS source_meal_status "
            "FROM leftovers l LEFT JOIN entries e ON e.id = l.source_meal_id "
            "WHERE l.status = 'available' AND l.servings > 0 AND l.prepared_on <= ? AND (l.use_by IS NULL OR l.use_by >= ?) "
            "ORDER BY l.use_by IS NULL, l.use_by, l.prepared_on, l.leftover_id",
            (as_of, as_of),
        ).fetchall()
        return {
            "as_of": as_of,
            "location": location,
            "inventory": [
                {**{key: value for key, value in item.items() if key != "quantity"}, "quantity": _decimal_text(item["quantity"])}
                for item in stock.values()
            ],
            "leftovers": [dict(row) for row in leftovers],
        }


__all__ = ["HANDLERS", "KitchenMixin", "MIGRATION_SQL", "TOOL_SCHEMAS"]
