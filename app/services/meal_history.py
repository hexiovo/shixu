from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from .errors import BusinessError
from .tool_schema import (
    MEAL_CREATE_SCHEMA,
    _date,
    _integer,
    _mode,
    _money_cents,
    _nullable_string,
    _object,
    _request_id,
    _string,
    _tool,
)


_MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack", "other"}
_STORE = _object({"name": _string, "branch": _nullable_string}, ("name",))
_STORE_FILTER = {"anyOf": [_STORE, {"type": "null"}]}
_PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "offset": {"type": "integer", "minimum": 0},
}
_REVIEW_RATING = {"type": ["integer", "null"], "minimum": 1, "maximum": 5}


TOOL_SCHEMAS = [
    _tool(
        "meal_search",
        "Search active meal history by exact date, mode, meal type, store, dish, or note filters. Filters combine before pagination.",
        {
            "date_from": _date,
            "date_to": _date,
            "mode": _mode,
            "meal_type": {"type": "string", "enum": sorted(_MEAL_TYPES)},
            "store": _STORE_FILTER,
            "store_id": _string,
            "dish": _string,
            "note": _string,
            **_PAGE,
        },
    ),
    _tool("meal_get", "Read one meal or standalone milk-tea record by ID, including its current version and void status.", {"meal_id": _string}, ("meal_id",)),
    _tool(
        "meal_set_status",
        "Soft-void or restore one meal or standalone milk-tea record. Requires its current version.",
        {
            "request_id": _request_id,
            "meal_id": _string,
            "expected_version": {"type": "integer", "minimum": 1},
            "status": {"type": "string", "enum": ["void", "restore"]},
            "reason": _nullable_string,
        },
        ("request_id", "meal_id", "expected_version", "status"),
    ),
    _tool(
        "meal_item_update",
        "Correct selected fields on one meal item by stable item ID without replacing sibling items. Requires the current meal version.",
        {
            "request_id": _request_id,
            "meal_id": _string,
            "expected_version": {"type": "integer", "minimum": 1},
            "item_id": _string,
            "changes": _object(
                {
                    "name": _string,
                    "quantity": {"type": ["number", "string", "null"]},
                    "unit": _nullable_string,
                    "notes": _nullable_string,
                }
            ),
        },
        ("request_id", "meal_id", "expected_version", "item_id", "changes"),
    ),
    _tool(
        "meal_batch_preview",
        "Validate a bounded meal import batch without writing records or request receipts. Each row uses the meal_create schema.",
        {"rows": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object"}}},
        ("rows",),
    ),
    _tool(
        "meal_batch_create",
        "Create a bounded meal import batch. Each row has an independent request ID, receipt, and error result.",
        {"rows": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object"}}},
        ("rows",),
    ),
    _tool(
        "store_search",
        "Find exact store identities by name and optional exact branch. Results distinguish aliases from canonical stores.",
        {"name": _string, "branch": _nullable_string, **_PAGE},
        ("name",),
    ),
    _tool(
        "store_alias_set",
        "Explicitly map a new or unused store name and branch as an alias of a canonical store. Stores with history require merge preview and confirmation.",
        {"request_id": _request_id, "alias": _STORE, "canonical_store_id": _string},
        ("request_id", "alias", "canonical_store_id"),
    ),
    _tool(
        "store_merge_preview",
        "Preview the exact meal records and versions affected by merging one canonical store into another. This tool does not write.",
        {"source_store_id": _string, "target_store_id": _string},
        ("source_store_id", "target_store_id"),
    ),
    _tool(
        "store_merge_commit",
        "Commit a previously previewed store merge after explicit confirmation. The preview signature prevents stale commits.",
        {
            "request_id": _request_id,
            "source_store_id": _string,
            "target_store_id": _string,
            "preview_signature": _string,
            "confirm": {"type": "boolean", "const": True},
        },
        ("request_id", "source_store_id", "target_store_id", "preview_signature", "confirm"),
    ),
    _tool(
        "meal_review_set",
        "Set or clear a meal rating and order note. Requires the current meal version.",
        {
            "request_id": _request_id,
            "meal_id": _string,
            "expected_version": {"type": "integer", "minimum": 1},
            "rating": _REVIEW_RATING,
            "order_note": _nullable_string,
        },
        ("request_id", "meal_id", "expected_version"),
    ),
    _tool("meal_review_get", "Read a meal rating and order note together with the current parent meal version.", {"meal_id": _string}, ("meal_id",)),
]


HANDLERS = {
    "meal_search": "_meal_search",
    "meal_get": "_meal_get",
    "meal_set_status": "_meal_set_status",
    "meal_item_update": "_meal_item_update",
    "meal_batch_preview": "_meal_batch_preview",
    "meal_batch_create": "_meal_batch_create",
    "store_search": "_store_search",
    "store_alias_set": "_store_alias_set",
    "store_merge_preview": "_store_merge_preview",
    "store_merge_commit": "_store_merge_commit",
    "meal_review_set": "_meal_review_set",
    "meal_review_get": "_meal_review_get",
}


MIGRATION_SQL = [
    "CREATE TABLE IF NOT EXISTS meal_store_identity ("
    "entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE, "
    "store_id TEXT NOT NULL REFERENCES stores(store_id))",
    "CREATE INDEX IF NOT EXISTS meal_store_identity_store_id ON meal_store_identity(store_id)",
    "INSERT OR IGNORE INTO meal_store_identity(entry_id, store_id) "
    "SELECT id, store_id FROM entries WHERE store_id IS NOT NULL",
    "CREATE TABLE IF NOT EXISTS meal_store_aliases ("
    "alias_store_id TEXT PRIMARY KEY REFERENCES stores(store_id), "
    "canonical_store_id TEXT NOT NULL REFERENCES stores(store_id), "
    "created_at TEXT NOT NULL, CHECK(alias_store_id <> canonical_store_id))",
    "CREATE INDEX IF NOT EXISTS meal_store_aliases_canonical_store_id "
    "ON meal_store_aliases(canonical_store_id)",
    "CREATE TABLE IF NOT EXISTS meal_reviews ("
    "entry_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE, "
    "rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5), "
    "order_note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "CHECK(rating IS NOT NULL OR order_note IS NOT NULL))",
]


class MealHistoryMixin:
    @staticmethod
    def _reject_extra_fields(payload: dict[str, Any], allowed: set[str], field: str = "payload") -> None:
        extra = next((name for name in payload if name not in allowed), None)
        if extra is not None:
            raise BusinessError("INVALID_INPUT", f"{field} contains unsupported fields", {"field": f"{field}.{extra}" if field != "payload" else extra})

    @staticmethod
    def _row_error(index: int, exc: BusinessError) -> dict[str, Any]:
        details = dict(exc.details)
        details["row_index"] = index
        field = details.get("field")
        prefix = f"rows[{index}]"
        if field and not str(field).startswith(prefix):
            details["field"] = f"{prefix}.{field}"
        return {
            "row_index": index,
            "request_id": details.get("request_id"),
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "details": details},
        }

    def _canonical_store_id(self, connection: sqlite3.Connection, store_id: str) -> str:
        row = connection.execute(
            "SELECT canonical_store_id FROM meal_store_aliases WHERE alias_store_id = ?",
            (store_id,),
        ).fetchone()
        return row["canonical_store_id"] if row else store_id

    @staticmethod
    def _store_record(connection: sqlite3.Connection, store_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT store_id, name, branch FROM stores WHERE store_id = ?",
            (store_id,),
        ).fetchone()
        return dict(row) if row else None

    def _history_entry(self, connection: sqlite3.Connection, entry_id: str) -> dict[str, Any]:
        record = self._entry(connection, entry_id)
        row = connection.execute(
            "SELECT calendar_date, voided_at, void_reason, version, record_kind "
            "FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        record["calendar_date"] = row["calendar_date"]
        record["voided_at"] = row["voided_at"]
        record["void_reason"] = row["void_reason"]
        store = record.get("store")
        record["canonical_store"] = None
        if store is not None:
            canonical_id = self._canonical_store_id(connection, store["store_id"])
            record["canonical_store"] = self._store_record(connection, canonical_id)
        if row["record_kind"] == "meal":
            record["review"] = self._review_payload(connection, entry_id, row["version"])
        return record

    def _review_payload(self, connection: sqlite3.Connection, meal_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT rating, order_note, updated_at FROM meal_reviews WHERE entry_id = ?",
            (meal_id,),
        ).fetchone()
        return {
            "meal_id": meal_id,
            "rating": row["rating"] if row else None,
            "order_note": row["order_note"] if row else None,
            "updated_at": row["updated_at"] if row else None,
            "meal_version": version,
        }

    def _store_filter_ids(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> list[str] | None:
        if "store" in payload and "store_id" in payload:
            raise BusinessError("INVALID_INPUT", "store and store_id are mutually exclusive", {"field": "store"})
        if payload.get("store_id") is not None:
            store_id = self._string(payload["store_id"], "store_id")
            if self._store_record(connection, store_id) is None:
                return []
            return [self._canonical_store_id(connection, store_id)]
        if payload.get("store") is None:
            return None
        store = self._dict(payload["store"], "store")
        self._reject_extra_fields(store, {"name", "branch"}, "store")
        name = self._string(store.get("name"), "store.name")
        if "branch" in store:
            branch = self._string(store["branch"], "store.branch", nullable=True)
            rows = connection.execute(
                "SELECT store_id FROM stores WHERE name = ? AND branch IS ? ORDER BY store_id",
                (name, branch),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT store_id FROM stores WHERE name = ? ORDER BY branch, store_id",
                (name,),
            ).fetchall()
        return sorted({self._canonical_store_id(connection, row["store_id"]) for row in rows})

    @staticmethod
    def _active_store_entries(connection: sqlite3.Connection, store_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT e.id, e.calendar_date, e.version, e.store_id "
            "FROM entries AS e LEFT JOIN meal_store_identity AS i ON i.entry_id = e.id "
            "WHERE e.record_kind = 'meal' AND COALESCE(i.store_id, e.store_id) = ? "
            "ORDER BY e.calendar_date, e.created_at, e.id",
            (store_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _meal_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(
            payload,
            {"date_from", "date_to", "mode", "meal_type", "store", "store_id", "dish", "note", "limit", "offset"},
        )
        date_from = self._iso_date(payload["date_from"], "date_from") if payload.get("date_from") is not None else None
        date_to = self._iso_date(payload["date_to"], "date_to") if payload.get("date_to") is not None else None
        if date_from is not None:
            date_from = self._canonical_calendar_date(date_from)
        if date_to is not None:
            date_to = self._canonical_calendar_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise BusinessError("INVALID_INPUT", "date_from cannot be after date_to", {"field": "date_from"})
        mode = payload.get("mode")
        if mode is not None:
            mode = self._string(mode, "mode")
            if mode not in {"eat_out", "delivery", "home_cooking"}:
                raise BusinessError("INVALID_INPUT", "mode is unsupported", {"field": "mode"})
        meal_type = payload.get("meal_type")
        if meal_type is not None:
            meal_type = self._string(meal_type, "meal_type")
            if meal_type not in _MEAL_TYPES:
                raise BusinessError("INVALID_INPUT", "meal_type is unsupported", {"field": "meal_type"})
        dish = self._string(payload["dish"], "dish") if payload.get("dish") is not None else None
        note = self._string(payload["note"], "note") if payload.get("note") is not None else None
        limit, offset = self._page(payload)
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
        if dish is not None:
            clauses.append("EXISTS (SELECT 1 FROM items AS i WHERE i.entry_id = e.id AND i.name = ?)")
            values.append(dish)
        if note is not None:
            clauses.append(
                "(e.note = ? OR EXISTS (SELECT 1 FROM meal_reviews AS r WHERE r.entry_id = e.id AND r.order_note = ?) "
                "OR EXISTS (SELECT 1 FROM items AS i WHERE i.entry_id = e.id AND i.notes = ?) "
                "OR EXISTS (SELECT 1 FROM payments AS p WHERE p.entry_id = e.id AND p.note = ?))"
            )
            values.extend([note, note, note, note])
        with self.store.read() as connection:
            store_ids = self._store_filter_ids(connection, payload)
            if store_ids is not None:
                if not store_ids:
                    return {"items": [], "total": 0, "limit": limit, "offset": offset}
                placeholders = ", ".join("?" for _ in store_ids)
                clauses.append(f"COALESCE(msi.store_id, e.store_id) IN ({placeholders})")
                values.extend(store_ids)
            where = " AND ".join(clauses)
            join = " FROM active_entries AS e LEFT JOIN meal_store_identity AS msi ON msi.entry_id = e.id"
            total = connection.execute(f"SELECT COUNT(*){join} WHERE {where}", values).fetchone()[0]
            ids = connection.execute(
                f"SELECT e.id{join} WHERE {where} "
                "ORDER BY e.calendar_date DESC, e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            items = [self._history_entry(connection, row["id"]) for row in ids]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _meal_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"meal_id"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        with self.store.read() as connection:
            row = connection.execute("SELECT id FROM entries WHERE id = ?", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "record was not found", {"id": meal_id})
            return self._history_entry(connection, meal_id)

    def _meal_set_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "meal_id", "expected_version", "status", "reason"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        status = self._string(payload.get("status"), "status")
        if status not in {"void", "restore"}:
            raise BusinessError("INVALID_INPUT", "status must be void or restore", {"field": "status"})
        reason = self._string(payload.get("reason"), "reason", nullable=True)
        if status == "restore" and reason is not None:
            raise BusinessError("INVALID_INPUT", "reason is only valid when voiding a record", {"field": "reason"})
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM entries WHERE id = ?", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "record was not found", {"id": meal_id})
            self._check_version(row, meal_id, expected_version)
            if status == "restore" and connection.execute("SELECT 1 FROM meal_redirects WHERE source_id=?", (meal_id,)).fetchone():
                raise BusinessError("RECORD_MOVED", "This record's contents were moved by a confirmed correction")
            if status == "void" and row["voided_at"] is None:
                connection.execute(
                    "UPDATE entries SET voided_at = ?, void_reason = ?, version = ?, updated_at = ? WHERE id = ?",
                    (now, reason, expected_version + 1, now, meal_id),
                )
            elif status == "void" and row["void_reason"] != reason:
                connection.execute(
                    "UPDATE entries SET void_reason = ?, version = ?, updated_at = ? WHERE id = ?",
                    (reason, expected_version + 1, now, meal_id),
                )
            elif status == "restore" and row["voided_at"] is not None:
                connection.execute(
                    "UPDATE entries SET voided_at = NULL, void_reason = NULL, version = ?, updated_at = ? WHERE id = ?",
                    (expected_version + 1, now, meal_id),
                )
            return self._history_entry(connection, meal_id)

        return self._write("meal_set_status", payload, operation)

    def _meal_item_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "meal_id", "expected_version", "item_id", "changes"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        item_id = self._string(payload.get("item_id"), "item_id")
        changes = self._dict(payload.get("changes"), "changes")
        self._reject_extra_fields(changes, {"name", "quantity", "unit", "notes"}, "changes")
        if not changes:
            raise BusinessError("INVALID_INPUT", "changes cannot be empty", {"field": "changes"})
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute("SELECT * FROM entries WHERE id = ? AND record_kind = 'meal'", (meal_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(row, meal_id, expected_version)
            if row["voided_at"] is not None:
                raise BusinessError("MEAL_VOIDED", "voided meals must be restored before editing", {"id": meal_id})
            item = connection.execute(
                "SELECT item_id, name, quantity, unit, notes FROM items WHERE item_id = ? AND entry_id = ?",
                (item_id, meal_id),
            ).fetchone()
            if item is None:
                raise BusinessError("NOT_FOUND", "meal item was not found", {"item_id": item_id, "meal_id": meal_id})
            candidate = {field: item[field] for field in ("name", "quantity", "unit", "notes")}
            candidate.update(changes)
            validated = self._validate_items([candidate])[0]
            setters = ", ".join(f"{field} = ?" for field in changes)
            connection.execute(
                f"UPDATE items SET {setters} WHERE item_id = ? AND entry_id = ?",
                [*(validated[field] for field in changes), item_id, meal_id],
            )
            connection.execute(
                "UPDATE entries SET version = ?, updated_at = ? WHERE id = ?",
                (expected_version + 1, now, meal_id),
            )
            return self._history_entry(connection, meal_id)

        return self._write("meal_item_update", payload, operation)

    def _batch_rows(self, payload: dict[str, Any]) -> list[Any]:
        self._reject_extra_fields(payload, {"rows"})
        rows = self._list(payload.get("rows"), "rows")
        if not rows:
            raise BusinessError("INVALID_INPUT", "rows cannot be empty", {"field": "rows"})
        if len(rows) > 50:
            raise BusinessError("INVALID_INPUT", "a batch cannot exceed 50 rows", {"field": "rows"})
        return rows

    def _batch_row(self, value: Any, index: int, *, validate: bool) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
        prefix = f"rows[{index}]"
        row = self._dict(value, prefix)
        meal_schema = MEAL_CREATE_SCHEMA["inputSchema"]
        properties = set(meal_schema["properties"])
        self._reject_extra_fields(row, properties, prefix)
        missing = next((field for field in meal_schema["required"] if field not in row), None)
        if missing is not None:
            raise BusinessError("INVALID_INPUT", f"{prefix}.{missing} is required", {"field": f"{prefix}.{missing}"})
        if "store" in row and "store_id" in row:
            raise BusinessError("INVALID_INPUT", "store and store_id are mutually exclusive", {"field": f"{prefix}.store"})
        request_id = self._string(row.get("request_id"), f"{prefix}.request_id") if validate else row.get("request_id")
        normalized = self._validate_meal_payload(row) if validate else None
        return row, request_id, normalized

    @staticmethod
    def _batch_validation_view(normalized: dict[str, Any], calendar_date: str) -> dict[str, Any]:
        return {
            "date": normalized["date"],
            "calendar_date": calendar_date,
            "meal_type": normalized["meal_type"],
            "mode": normalized["mode"],
            "meal_amount_cents": normalized["meal_amount"],
            "meal_amount_manual": normalized["manual_amount"],
            "items": normalized["items"],
            "payments": normalized["payments"],
            "timing": normalized["timing"],
            "consumption_cost": normalized["consumption_cost"],
            "location": normalized["location"],
            "note": normalized["note"],
            "people_count": normalized["people_count"],
            "store": normalized["store"],
            "store_id": normalized["store_id"],
        }

    def _meal_batch_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._batch_rows(payload)
        results = []
        with self.store.read() as connection:
            for index, row_value in enumerate(rows):
                try:
                    row, request_id, normalized = self._batch_row(row_value, index, validate=True)
                    store_id = normalized["store_id"]
                    if store_id is not None and self._store_record(connection, store_id) is None:
                        raise BusinessError("NOT_FOUND", "store_id was not found", {"field": "store_id", "store_id": store_id})
                    source = self._meal_recommendation_source(row)
                    if source is not None:
                        self._validate_recommendation_meal_source(connection, *source, normalized["mode"])
                    results.append(
                        {
                            "row_index": index,
                            "request_id": request_id,
                            "ok": True,
                            "meal": self._batch_validation_view(
                                normalized,
                                self._canonical_calendar_date(normalized["date"]),
                            ),
                        }
                    )
                except BusinessError as exc:
                    results.append(self._row_error(index, exc))
        return {
            "results": results,
            "valid_count": sum(row["ok"] for row in results),
            "invalid_count": sum(not row["ok"] for row in results),
            "would_write": False,
        }

    def _meal_batch_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._batch_rows(payload)
        results = []
        for index, row_value in enumerate(rows):
            try:
                row, request_id, _ = self._batch_row(row_value, index, validate=False)
                result = self._meal_create(row)
                results.append({"row_index": index, "request_id": request_id, "ok": True, "receipt": result})
            except BusinessError as exc:
                results.append(self._row_error(index, exc))
        return {
            "results": results,
            "committed_count": sum(row["ok"] for row in results),
            "error_count": sum(not row["ok"] for row in results),
        }

    def _store_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"name", "branch", "limit", "offset"})
        name = self._string(payload.get("name"), "name")
        limit, offset = self._page(payload)
        clauses = ["name = ?"]
        values: list[Any] = [name]
        if "branch" in payload:
            branch = self._string(payload["branch"], "branch", nullable=True)
            clauses.append("branch IS ?")
            values.append(branch)
        where = " AND ".join(clauses)
        with self.store.read() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM stores WHERE {where}", values).fetchone()[0]
            rows = connection.execute(
                f"SELECT store_id, name, branch FROM stores WHERE {where} "
                "ORDER BY branch, store_id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                store = dict(row)
                canonical_id = self._canonical_store_id(connection, store["store_id"])
                store["canonical_store"] = self._store_record(connection, canonical_id)
                aliases = connection.execute(
                    "SELECT s.store_id, s.name, s.branch FROM meal_store_aliases AS a "
                    "JOIN stores AS s ON s.store_id = a.alias_store_id "
                    "WHERE a.canonical_store_id = ? ORDER BY s.name, s.branch, s.store_id",
                    (canonical_id,),
                ).fetchall()
                store["aliases"] = [dict(alias) for alias in aliases]
                items.append(store)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _store_alias_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "alias", "canonical_store_id"})
        alias = self._store_fields(payload.get("alias"), "alias")
        canonical_store_id = self._string(payload.get("canonical_store_id"), "canonical_store_id")
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            canonical_store = self._store_record(connection, canonical_store_id)
            if canonical_store is None:
                raise BusinessError("NOT_FOUND", "canonical store was not found", {"store_id": canonical_store_id})
            canonical_store_id_resolved = self._canonical_store_id(connection, canonical_store_id)
            canonical_store = self._store_record(connection, canonical_store_id_resolved)
            row = connection.execute(
                "SELECT store_id, name, branch FROM stores WHERE name = ? AND branch IS ?",
                (alias["name"], alias["branch"]),
            ).fetchone()
            created = row is None
            if row is None:
                alias_store_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO stores(store_id, name, branch) VALUES (?, ?, ?)",
                    (alias_store_id, alias["name"], alias["branch"]),
                )
                alias_store = {"store_id": alias_store_id, **alias}
            else:
                alias_store = dict(row)
                alias_store_id = alias_store["store_id"]
            if alias_store_id == canonical_store_id_resolved:
                raise BusinessError("STORE_ALIAS_CONFLICT", "an identity cannot be its own alias", {"store_id": alias_store_id})
            existing = connection.execute(
                "SELECT canonical_store_id FROM meal_store_aliases WHERE alias_store_id = ?",
                (alias_store_id,),
            ).fetchone()
            if existing is not None:
                current_canonical = self._canonical_store_id(connection, existing["canonical_store_id"])
                if current_canonical != canonical_store_id_resolved:
                    raise BusinessError(
                        "STORE_ALIAS_CONFLICT",
                        "this store alias already points to another canonical store",
                        {"alias_store_id": alias_store_id, "canonical_store_id": current_canonical},
                    )
                return {"alias": alias_store, "canonical_store": canonical_store, "created": False}
            affected = self._active_store_entries(connection, alias_store_id)
            if affected:
                raise BusinessError(
                    "STORE_HAS_HISTORY",
                    "a store with meal history must be merged through preview and confirmation",
                    {"store_id": alias_store_id, "affected_count": len(affected), "affected_entries": affected},
                )
            incoming = connection.execute(
                "SELECT 1 FROM meal_store_aliases WHERE canonical_store_id = ? LIMIT 1",
                (alias_store_id,),
            ).fetchone()
            if incoming is not None:
                raise BusinessError(
                    "STORE_HAS_ALIASES",
                    "a canonical store with aliases must be merged through preview and confirmation",
                    {"store_id": alias_store_id},
                )
            connection.execute(
                "INSERT INTO meal_store_aliases(alias_store_id, canonical_store_id, created_at) VALUES (?, ?, ?)",
                (alias_store_id, canonical_store_id_resolved, now),
            )
            return {"alias": alias_store, "canonical_store": canonical_store, "created": created}

        return self._write("store_alias_set", payload, operation)

    def _store_merge_snapshot(
        self,
        connection: sqlite3.Connection,
        source_store_id: str,
        target_store_id: str,
    ) -> dict[str, Any]:
        source_store = self._store_record(connection, source_store_id)
        if source_store is None:
            raise BusinessError("NOT_FOUND", "source store was not found", {"store_id": source_store_id})
        target_store = self._store_record(connection, target_store_id)
        if target_store is None:
            raise BusinessError("NOT_FOUND", "target store was not found", {"store_id": target_store_id})
        source_canonical = self._canonical_store_id(connection, source_store_id)
        if source_canonical != source_store_id:
            raise BusinessError(
                "STORE_ALREADY_ALIAS",
                "merge source must be a canonical store",
                {"store_id": source_store_id, "canonical_store_id": source_canonical},
            )
        target_canonical = self._canonical_store_id(connection, target_store_id)
        target_store = self._store_record(connection, target_canonical)
        if source_store_id == target_canonical:
            raise BusinessError("INVALID_INPUT", "source and target stores must differ", {"field": "target_store_id"})
        affected = self._active_store_entries(connection, source_store_id)
        aliases = connection.execute(
            "SELECT alias_store_id, canonical_store_id FROM meal_store_aliases "
            "WHERE canonical_store_id IN (?, ?) OR alias_store_id IN (?, ?) "
            "ORDER BY alias_store_id",
            (source_store_id, target_canonical, source_store_id, target_canonical),
        ).fetchall()
        signed = {
            "source": source_store,
            "target": target_store,
            "source_store_id": source_store_id,
            "target_store_id": target_canonical,
            "aliases": [dict(row) for row in aliases],
            "entries": [
                {
                    "id": row["id"],
                    "calendar_date": row["calendar_date"],
                    "version": row["version"],
                    "store_id": row["store_id"],
                }
                for row in affected
            ],
        }
        source_profile = self._read_store_profile(connection, source_store_id)
        target_profile = self._read_store_profile(connection, target_canonical)
        if source_profile is not None or target_profile is not None:
            signed["source_profile"] = source_profile
            signed["target_profile"] = target_profile
        signature = hashlib.sha256(
            json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "source_store": source_store,
            "target_store": target_store,
            "source_store_id": source_store_id,
            "target_store_id": target_canonical,
            "source_profile": source_profile,
            "target_profile": target_profile,
            "affected_entries": [
                {
                    "entry_id": row["id"],
                    "date": row["calendar_date"],
                    "version": row["version"],
                    "original_store_id": row["store_id"],
                }
                for row in affected
            ],
            "affected_count": len(affected),
            "aliases_to_redirect": [row["alias_store_id"] for row in aliases if row["canonical_store_id"] == source_store_id],
            "preview_signature": signature,
        }

    def _store_merge_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"source_store_id", "target_store_id"})
        source_store_id = self._string(payload.get("source_store_id"), "source_store_id")
        target_store_id = self._string(payload.get("target_store_id"), "target_store_id")
        with self.store.read() as connection:
            return self._store_merge_snapshot(connection, source_store_id, target_store_id)

    def _store_merge_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "source_store_id", "target_store_id", "preview_signature", "confirm"})
        source_store_id = self._string(payload.get("source_store_id"), "source_store_id")
        target_store_id = self._string(payload.get("target_store_id"), "target_store_id")
        preview_signature = self._string(payload.get("preview_signature"), "preview_signature")
        if payload.get("confirm") is not True:
            raise BusinessError("INVALID_INPUT", "confirm must be true", {"field": "confirm"})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            snapshot = self._store_merge_snapshot(connection, source_store_id, target_store_id)
            if snapshot["preview_signature"] != preview_signature:
                raise BusinessError(
                    "STORE_MERGE_STALE_PREVIEW",
                    "store history or aliases changed after preview; preview the merge again",
                    {"source_store_id": source_store_id, "target_store_id": snapshot["target_store_id"]},
                )
            target_id = snapshot["target_store_id"]
            connection.execute(
                "UPDATE meal_store_aliases SET canonical_store_id = ? WHERE canonical_store_id = ?",
                (target_id, source_store_id),
            )
            connection.execute(
                "INSERT INTO meal_store_aliases(alias_store_id, canonical_store_id, created_at) VALUES (?, ?, ?)",
                (source_store_id, target_id, self._now()),
            )
            entries = self._active_store_entries(connection, source_store_id)
            connection.executemany(
                "INSERT INTO meal_store_identity(entry_id, store_id) VALUES (?, ?) "
                "ON CONFLICT(entry_id) DO UPDATE SET store_id = excluded.store_id",
                [(entry["id"], target_id) for entry in entries],
            )
            source_profile = snapshot["source_profile"]
            target_profile = snapshot["target_profile"]
            if source_profile is not None and target_profile is None:
                connection.execute(
                    "UPDATE store_profiles SET store_id=?, version=version+1, updated_at=? WHERE store_id=?",
                    (target_id, self._now(), source_store_id),
                )
            elif source_profile is not None and target_profile is not None:
                occasions = list(dict.fromkeys([*target_profile["occasions"], *source_profile["occasions"]]))
                environment = target_profile["environment"] or source_profile["environment"]
                note = target_profile["note"] or source_profile["note"]
                connection.execute(
                    "UPDATE store_profiles SET environment=?, occasions_json=?, note=?, version=version+1, updated_at=? WHERE store_id=?",
                    (environment, self._json(occasions), note, self._now(), target_id),
                )
            return {
                "source_store_id": source_store_id,
                "canonical_store_id": target_id,
                "merged_entry_ids": [entry["id"] for entry in entries],
                "merged_count": len(entries),
                "record_versions_changed": False,
            }

        return self._write("store_merge_commit", payload, operation)

    def _meal_review_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"request_id", "meal_id", "expected_version", "rating", "order_note"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected_version = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        fields = {"rating", "order_note"} & set(payload)
        if not fields:
            raise BusinessError("INVALID_INPUT", "rating or order_note must be provided", {"field": "payload"})
        rating = payload.get("rating")
        if "rating" in payload and rating is not None:
            rating = self._integer(rating, "rating", minimum=1)
            if rating > 5:
                raise BusinessError("INVALID_INPUT", "rating cannot exceed 5", {"field": "rating"})
        order_note = self._string(payload.get("order_note"), "order_note", nullable=True) if "order_note" in payload else None
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            meal = connection.execute("SELECT * FROM entries WHERE id = ? AND record_kind = 'meal'", (meal_id,)).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            self._check_version(meal, meal_id, expected_version)
            if meal["voided_at"] is not None:
                raise BusinessError("MEAL_VOIDED", "voided meals must be restored before editing", {"id": meal_id})
            previous = connection.execute(
                "SELECT rating, order_note, created_at FROM meal_reviews WHERE entry_id = ?",
                (meal_id,),
            ).fetchone()
            new_rating = rating if "rating" in fields else (previous["rating"] if previous else None)
            new_note = order_note if "order_note" in fields else (previous["order_note"] if previous else None)
            changed = (
                previous is None and (new_rating is not None or new_note is not None)
            ) or (
                previous is not None and (previous["rating"] != new_rating or previous["order_note"] != new_note)
            )
            if changed:
                if new_rating is None and new_note is None:
                    connection.execute("DELETE FROM meal_reviews WHERE entry_id = ?", (meal_id,))
                elif previous is None:
                    connection.execute(
                        "INSERT INTO meal_reviews(entry_id, rating, order_note, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (meal_id, new_rating, new_note, now, now),
                    )
                else:
                    connection.execute(
                        "UPDATE meal_reviews SET rating = ?, order_note = ?, updated_at = ? WHERE entry_id = ?",
                        (new_rating, new_note, now, meal_id),
                    )
                connection.execute(
                    "UPDATE entries SET version = ?, updated_at = ? WHERE id = ?",
                    (expected_version + 1, now, meal_id),
                )
            version = expected_version + 1 if changed else expected_version
            return {"meal_id": meal_id, "meal_version": version, "review": self._review_payload(connection, meal_id, version)}

        return self._write("meal_review_set", payload, operation)

    def _meal_review_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"meal_id"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        with self.store.read() as connection:
            meal = connection.execute(
                "SELECT id, record_kind, version FROM entries WHERE id = ?",
                (meal_id,),
            ).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "meal was not found", {"id": meal_id})
            if meal["record_kind"] != "meal":
                raise BusinessError("INVALID_INPUT", "reviews are only available for meal records", {"id": meal_id})
            return self._review_payload(connection, meal_id, meal["version"])


__all__ = ["HANDLERS", "MIGRATION_SQL", "MealHistoryMixin", "TOOL_SCHEMAS"]
