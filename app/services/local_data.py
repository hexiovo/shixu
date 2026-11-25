from __future__ import annotations

import csv
import io

from app import APP_VERSION

from .errors import BusinessError
from .tool_schema import _date, _integer, _string, _tool


_QUERY = {
    "date_from": _date, "date_to": _date, "query": _string, "mode": {"enum": ["eat_out", "delivery", "home_cooking"]},
    "meal_type": _string, "detail": {"enum": ["summary", "full"]}, "limit": _integer, "offset": _integer,
}
_EXPORT_KINDS = {"meals", "milk_tea", "inventory", "catalog"}
TOOL_SCHEMAS = [
    _tool("meal_history_query", "Search local meals by partial dish name, saved alias, store or note. Choose summary or full rows.", _QUERY),
    _tool("meal_import_inspect", "Validate structured import rows and report likely existing duplicates without writing.", {
        "rows": {"type": "array", "items": {"type": "object"}},
    }, ("rows",)),
    _tool("meal_data_export", "Return a bounded page of local records in JSON or spreadsheet-safe CSV, with no file path.", {
        **{key: value for key, value in _QUERY.items() if key != "detail"},
        "kind": {"enum": sorted(_EXPORT_KINDS)}, "format": {"enum": ["json", "csv"]},
    }),
    _tool("meal_data_check", "Inspect a bounded page of meals for missing fields, amount discrepancies, and likely duplicates.", {
        **{key: value for key, value in _QUERY.items() if key != "detail"},
    }),
    _tool("shixu_status", "Read local release, schema, tool count and optional SQLite integrity summary.", {"check_storage": {"type": "boolean"}}),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}


class LocalDataMixin:
    def _meal_history_query(self, payload):
        self._reject_extra_fields(payload, set(_QUERY))
        first, last = self._analytics_dates(payload.get("date_from"), payload.get("date_to"))
        mode = self._analytics_mode(payload.get("mode"), "mode")
        meal_type = self._analytics_meal_type(payload.get("meal_type"))
        query = self._string(payload.get("query"), "query", nullable=True)
        detail = self._recommendation_enum(payload.get("detail", "summary"), "detail", {"summary", "full"})
        limit, offset = self._page(payload)
        base, values = self._meal_where(first, last, mode=mode)
        where = [base]
        if meal_type:
            where.append("e.meal_type=?")
            values.append(meal_type)
        if query:
            where.append(
                """(instr(lower(COALESCE(e.note,'')),lower(?))>0
                   OR EXISTS(SELECT 1 FROM meal_store_identity m JOIN stores s ON s.store_id=m.store_id
                             WHERE m.entry_id=e.id AND instr(lower(s.name||' '||COALESCE(s.branch,'')),lower(?))>0)
                   OR EXISTS(SELECT 1 FROM items i LEFT JOIN meal_item_profiles p USING(item_id)
                             WHERE i.entry_id=e.id AND (instr(lower(i.name),lower(?))>0 OR EXISTS(
                                 SELECT 1 FROM catalog_names n JOIN catalog_names a ON n.dish_id=a.dish_id
                                 WHERE (lower(n.name)=lower(i.name) OR n.dish_id=p.dish_id) AND instr(a.name_key,?)>0))))"""
            )
            values.extend([query] * 3 + [self._ingredient_key(query)])
        sql_where = " AND ".join(where)
        with self.store.read() as connection:
            total = connection.execute("SELECT COUNT(*) FROM active_entries e WHERE " + sql_where, values).fetchone()[0]
            rows = connection.execute(
                "SELECT e.id FROM active_entries e WHERE " + sql_where +
                " ORDER BY e.calendar_date DESC,e.created_at DESC,e.id DESC LIMIT ? OFFSET ?", (*values, limit, offset),
            ).fetchall()
            items = [self._history_entry(connection, row["id"]) for row in rows]
        if detail == "summary":
            items = [{
                "id": meal["id"], "version": meal["version"], "date": meal["calendar_date"], "mode": meal["mode"],
                "meal_type": meal["meal_type"], "dishes": [item["name"] for item in meal["items"]],
                "store": meal["canonical_store"], "meal_amount_cents": meal["meal_amount_cents"],
            } for meal in items]
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    def _import_duplicate_hits(self, connection, normalized):
        meal = normalized
        wanted = sorted(self._ingredient_key(item["name"]) for item in meal["items"])
        if not wanted:
            return []
        matches = connection.execute(
            """SELECT id FROM active_entries WHERE record_kind='meal' AND calendar_date=? AND meal_type=? AND mode=?
               ORDER BY created_at DESC,id LIMIT 100""",
            (self._canonical_calendar_date(meal["date"]), meal["meal_type"], meal["mode"]),
        ).fetchall()
        hits = []
        for row in matches:
            existing = self._history_entry(connection, row["id"])
            if sorted(self._ingredient_key(item["name"]) for item in existing["items"]) != wanted:
                continue
            if meal["meal_amount"] is not None and existing["meal_amount_cents"] is not None and meal["meal_amount"] != existing["meal_amount_cents"]:
                continue
            store = existing["canonical_store"]
            if meal["store_id"] and (not store or store["store_id"] != self._canonical_store_id(connection, meal["store_id"])):
                continue
            if meal["store"] and store and (meal["store"]["name"], meal["store"]["branch"]) != (store["name"], store["branch"]):
                continue
            hits.append({"id": row["id"], "version": existing["version"], "amount_cents": existing["meal_amount_cents"]})
        return hits

    def _meal_import_inspect(self, payload):
        self._reject_extra_fields(payload, {"rows"})
        raw = self._list(payload.get("rows"), "rows")
        with self.store.read() as connection:
            preview = self._meal_batch_preview(payload)
            batch = {}
            for item in preview["results"]:
                if item["ok"]:
                    _, _, normalized = self._batch_row(raw[item["row_index"]], item["row_index"], validate=True)
                    item["possible_duplicates"] = self._import_duplicate_hits(connection, normalized)
                    names = tuple(sorted(self._ingredient_key(dish["name"]) for dish in normalized["items"]))
                    index = (self._canonical_calendar_date(normalized["date"]), normalized["meal_type"], normalized["mode"], names)
                    existing = batch.setdefault(index, [])
                    store = normalized["store"] or normalized["store_id"]
                    amount = normalized["meal_amount"]
                    item["possible_duplicates_in_batch"] = [
                        row["index"] for row in existing if names
                        and (store is None or row["store"] is None or store == row["store"])
                        and (amount is None or row["amount"] is None or amount == row["amount"])
                    ]
                    existing.append({"index": item["row_index"], "store": store, "amount": amount})
        return preview

    @staticmethod
    def _csv_safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    def _meal_data_export(self, payload):
        self._reject_extra_fields(payload, (set(_QUERY) - {"detail"}) | {"kind", "format"})
        kind = self._recommendation_enum(payload.get("kind", "meals"), "kind", _EXPORT_KINDS)
        fmt = self._recommendation_enum(payload.get("format", "json"), "format", {"json", "csv"})
        filters = {key: value for key, value in payload.items() if key not in {"kind", "format"}}
        columns = ["id", "version", "date", "meal_type", "mode", "store", "meal_amount_cents", "items", "payments", "note"]
        if kind == "inventory":
            self._reject_extra_fields(filters, {"date_from", "date_to", "query", "limit", "offset"})
            page = self._inventory_export(filters)
            columns = ["batch_id", "ingredient", "quantity", "unit", "location", "expires_on", "source", "version", "created_at", "updated_at"]
        elif kind == "catalog":
            self._reject_extra_fields(filters, {"query", "limit", "offset"})
            page = self._dish_catalog_search(filters)
            columns = ["id", "name", "aliases", "version", "meal_count", "last_date"]
        elif kind == "milk_tea":
            self._reject_extra_fields(filters, {"date_from", "date_to", "limit", "offset"})
            page = self._milk_tea_list(filters)
        else:
            page = self._meal_history_query({**filters, "detail": "full"})
        if fmt == "json":
            return {"format": fmt, "kind": kind, **page}
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        for record in page["items"]:
            values = [self._json(record[key]) if isinstance(record[key], (list, dict)) else record[key] for key in columns]
            writer.writerow([self._csv_safe(value) for value in values])
        return {"format": fmt, "kind": kind, "csv": output.getvalue(), "total": page.get("total"),
                "offset": page["offset"], "limit": page["limit"]}

    def _inventory_export(self, filters):
        first, last = self._analytics_dates(filters.get("date_from"), filters.get("date_to"))
        query = self._string(filters.get("query"), "query", nullable=True)
        limit, offset = self._page(filters)
        where = ["1"]
        params = []
        for value, clause in ((first, "substr(created_at,1,10)>=?"), (last, "substr(created_at,1,10)<=?"),
                              (query, "instr(lower(ingredient),lower(?))>0")):
            if value is not None:
                where.append(clause)
                params.append(value)
        condition = " AND ".join(where)
        with self.store.read() as connection:
            total = connection.execute("SELECT COUNT(*) FROM inventory_batches WHERE " + condition, params).fetchone()[0]
            items = connection.execute(
                "SELECT * FROM inventory_batches WHERE " + condition + " ORDER BY created_at DESC,batch_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"total": total, "offset": offset, "limit": limit, "items": [dict(item) for item in items]}

    def _meal_data_check(self, payload):
        self._reject_extra_fields(payload, set(_QUERY) - {"detail"})
        issues = []
        with self.store.read() as connection:
            page = self._meal_history_query({**payload, "detail": "full"})
            for meal in page["items"]:
                row = connection.execute("SELECT meal_amount_manual FROM entries WHERE id=?", (meal["id"],)).fetchone()
                missing = (["items"] if not meal["items"] else []) + (["meal_amount_cents"] if meal["meal_amount_cents"] is None else [])
                amount = self._derive_meal_amount(connection, meal["id"])
                keys = {"id": meal["id"], "version": meal["version"]}
                if missing:
                    issues.append({**keys, "code": "MISSING_FIELDS", "fields": missing})
                if row["meal_amount_manual"] and amount is not None and meal["meal_amount_cents"] is not None and amount != meal["meal_amount_cents"]:
                    issues.append({**keys, "code": "AMOUNT_DIFFERS_FROM_LINES", "whole_meal_cents": meal["meal_amount_cents"], "line_total_cents": amount})
                normalized = self._validate_meal_payload({
                    "date": meal["calendar_date"], "meal_type": meal["meal_type"], "mode": meal["mode"],
                    "items": [{key: item[key] for key in ("name", "quantity", "unit", "notes")} for item in meal["items"]],
                    "meal_amount_cents": meal["meal_amount_cents"] if meal["meal_amount_cents"] is None or meal["meal_amount_cents"] >= 0 else None,
                    "store_id": (meal["canonical_store"] or {}).get("store_id"),
                })
                matches = [hit for hit in self._import_duplicate_hits(connection, normalized) if hit["id"] != meal["id"]]
                if matches:
                    issues.append({**keys, "code": "POSSIBLE_DUPLICATE", "matches": matches})
        return {"items": issues, "inspected": len(page["items"]), "total": page["total"], "offset": page["offset"], "limit": page["limit"]}

    def _shixu_status(self, payload):
        self._reject_extra_fields(payload, {"check_storage"})
        check = payload.get("check_storage", False)
        if type(check) is not bool:
            raise BusinessError("INVALID_INPUT", "check_storage must be a boolean")
        with self.store.read() as connection:
            schema = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            integrity = [row[0] for row in connection.execute("PRAGMA quick_check(4)")] if check else None
        return {"version": APP_VERSION, "schema_version": schema, "tool_count": len(self._handlers),
                "storage_check": None if integrity is None else {"ok": integrity == ["ok"], "findings": integrity}}
