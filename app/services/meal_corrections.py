from __future__ import annotations

import hashlib
import json
import uuid

from .errors import BusinessError
from .tool_schema import _date, _integer, _money_cents, _nullable_string, _object, _request_id, _string, _tool


AUDITED_ACTIONS = {
    "meal_create", "meal_update", "meal_set_status", "meal_item_update", "meal_review_set",
    "payment_add", "payment_remove", "milk_tea_create", "milk_tea_update", "meal_item_profile_set",
}
_ID_LIST = {"type": "array", "items": _string}
_OPERATIONS = {
    "batch_edit": _object({"updates": {"type": "array", "items": _object({
        "meal_id": _string, "expected_version": _integer, "scope": {"enum": ["meal", "payment"]},
        "payment_id": _string, "changes": {"type": "object"},
    }, ("meal_id", "expected_version", "scope", "changes"))}}, ("updates",)),
    "restore_fields": _object({"meal_id": _string, "from_version": _integer, "fields": _ID_LIST}, ("meal_id", "from_version", "fields")),
    "restore_payment": _object({"meal_id": _string, "from_version": _integer, "payment_id": _string, "fields": _ID_LIST},
                               ("meal_id", "from_version", "payment_id")),
    "merge": _object({"source_id": _string, "target_id": _string, "target_amount_cents": _money_cents,
                      "move_item_ids": _ID_LIST, "move_payment_ids": _ID_LIST}, ("source_id", "target_id")),
    "split": _object({
        "meal_id": _string, "item_ids": _ID_LIST, "payment_ids": _ID_LIST, "date": _date, "meal_type": _string,
        "source_amount_cents": _money_cents, "target_amount_cents": _money_cents,
    }, ("meal_id",)),
    "attach_tea": _object({"source_id": _string, "target_id": _string}, ("source_id", "target_id")),
    "detach_tea": _object({"meal_id": _string, "payment_id": _string}, ("meal_id", "payment_id")),
    "edit_items": _object({"meal_id": _string, "add": {"type": "array", "items": _object({
        "name": _string, "quantity": {"type": ["number", "string", "null"]}, "unit": _nullable_string, "notes": _nullable_string,
    }, ("name",))}, "remove": _ID_LIST}, ("meal_id",)),
}
_PREVIEW = {"operation": {"enum": list(_OPERATIONS)}, "args": {"anyOf": list(_OPERATIONS.values())}}
_ITEM_PROFILE = {
    "request_id": _request_id, "meal_id": _string, "item_id": _string, "expected_version": _integer,
    "dish_id": _nullable_string, "rating": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
    "order_note": _nullable_string,
}
TOOL_SCHEMAS = [
    _tool("meal_change_history", "Read consecutive revision differences and an optional version snapshot for a meal or standalone drink.", {
        "meal_id": _string, "version": _integer, "limit": _integer, "offset": _integer,
    }, ("meal_id",)),
    _tool("meal_change_preview", "Preview batch edits, selective meal/payment restoration, merge, split, tea moves or item edits. The response includes a version-bound fingerprint.", _PREVIEW, ("operation", "args")),
    _tool("meal_change_commit", "Confirm exactly the same operation and arguments returned by meal_change_preview.", {
        **_PREVIEW, "fingerprint": _string, "request_id": _request_id, "confirm": {"const": True},
    }, ("operation", "args", "fingerprint", "request_id", "confirm")),
    _tool("meal_item_profile_set", "Set a one-to-five dish rating, catalog link or dish-specific order note by stable meal item ID.", _ITEM_PROFILE, ("request_id", "meal_id", "item_id", "expected_version")),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}
MIGRATION_SQL = [
    "ALTER TABLE items ADD COLUMN display_order INTEGER",
    """CREATE TABLE meal_revisions(
       entry_id TEXT NOT NULL REFERENCES entries(id), version INTEGER NOT NULL,
       action TEXT NOT NULL, snapshot TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(entry_id,version))""",
    """CREATE TABLE meal_item_profiles(
       item_id TEXT PRIMARY KEY REFERENCES items(item_id) ON DELETE CASCADE,
       dish_id TEXT REFERENCES catalog_dishes(id), rating INTEGER, order_note TEXT)""",
    """CREATE TABLE meal_redirects(source_id TEXT PRIMARY KEY REFERENCES entries(id),
       target_id TEXT NOT NULL REFERENCES entries(id), operation TEXT NOT NULL)""",
]
_RESTORABLE = {
    "date", "meal_type", "mode", "meal_amount_cents", "location", "note", "timing", "people_count", "consumption_cost", "store",
}
_PAYMENT_FIELDS = {"kind", "scope", "amount_cents", "amount_min_cents", "amount_max_cents", "source", "note"}
_PAYMENT_AMOUNT = {"amount_cents", "amount_min_cents", "amount_max_cents", "source"}


class MealCorrectionsMixin:
    def _replace_meal_items(self, connection, entry_id, raw):
        fields = ("name", "quantity", "unit", "notes")
        incoming = [self._dict(item, "item") for item in self._list(raw, "items", default=[])]
        cleaned = self._validate_items([{key: value for key, value in item.items() if key != "item_id"} for item in incoming])
        existing = {
            row["item_id"]: dict(row) for row in connection.execute(
                "SELECT * FROM items WHERE entry_id=? ORDER BY COALESCE(display_order,rowid),rowid", (entry_id,),
            )
        }
        explicit = [self._string(item["item_id"], "item_id") for item in incoming if "item_id" in item]
        if len(explicit) != len(set(explicit)) or not set(explicit) <= existing.keys():
            raise BusinessError("INVALID_INPUT", "An item ID is repeated or does not belong to the meal")
        reusable = {key: item for key, item in existing.items() if key not in explicit}
        kept = set()
        for position, (raw_item, item) in enumerate(zip(incoming, cleaned), 1):
            item_id = raw_item["item_id"].strip() if "item_id" in raw_item else next((
                key for key, old in reusable.items() if all(old[field] == item[field] for field in fields)
            ), None)
            if item_id is not None:
                reusable.pop(item_id, None)
                kept.add(item_id)
                connection.execute("UPDATE items SET name=?,quantity=?,unit=?,notes=?,display_order=? WHERE item_id=?",
                                   (*(item[field] for field in fields), position, item_id))
            else:
                connection.execute("INSERT INTO items(item_id,entry_id,name,quantity,unit,notes,display_order) VALUES(?,?,?,?,?,?,?)",
                                   (uuid.uuid4().hex, entry_id, *(item[field] for field in fields), position))
        connection.executemany("DELETE FROM items WHERE item_id=?", [(key,) for key in existing.keys() - kept])

    def _active_meal_row(self, connection, entry_id, kind="meal"):
        row = connection.execute("SELECT * FROM active_entries WHERE id=? AND record_kind=?", (entry_id, kind)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "Active record not found", {"id": entry_id, "kind": kind})
        return row

    def _revision_state(self, connection, entry_id):
        row = connection.execute("SELECT meal_amount_manual FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            return None
        entry = self._history_entry(connection, entry_id)
        entry["meal_amount_manual"] = bool(row["meal_amount_manual"])
        return entry

    def _audit_meal(self, connection, entry_id, action):
        if entry_id is None:
            return
        state = self._revision_state(connection, entry_id)
        if state is not None:
            connection.execute(
                "INSERT OR IGNORE INTO meal_revisions VALUES(?,?,?,?,?)",
                (entry_id, state["version"], action, self._json(state), self._now()),
            )

    def _meal_change_history(self, payload):
        self._reject_extra_fields(payload, {"meal_id", "version", "limit", "offset"})
        entry_id = self._string(payload.get("meal_id"), "meal_id")
        wanted = self._integer(payload.get("version"), "version", nullable=True, minimum=1)
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            current = self._revision_state(connection, entry_id)
            if current is None:
                raise BusinessError("NOT_FOUND", "Record not found", {"id": entry_id})
            rows = connection.execute(
                "SELECT * FROM meal_revisions WHERE entry_id=? ORDER BY version DESC LIMIT ? OFFSET ?",
                (entry_id, limit + 1, offset),
            ).fetchall()
            history = []
            for index, row in enumerate(rows[:limit]):
                state = json.loads(row["snapshot"])
                earlier = json.loads(rows[index + 1]["snapshot"]) if index + 1 < len(rows) else {}
                changed = [key for key in state if key not in {"version", "expected_version", "updated_at"} and state[key] != earlier.get(key)]
                history.append({"version": row["version"], "action": row["action"], "recorded_at": row["recorded_at"], "changed_fields": changed})
            snapshot = self._revision_version(connection, entry_id, wanted, current) if wanted is not None else None
        return {"meal_id": entry_id, "version": current["version"], "items": history, "snapshot": snapshot, "has_more": len(rows) > limit}

    def _revision_version(self, connection, entry_id, version, current):
        if current["version"] == version:
            return current
        row = connection.execute("SELECT snapshot FROM meal_revisions WHERE entry_id=? AND version=?", (entry_id, version)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "Revision predates available change history or was not found", {"version": version})
        return json.loads(row["snapshot"])

    def _meal_item_profile_set(self, payload):
        self._reject_extra_fields(payload, set(_ITEM_PROFILE))
        entry_id = self._string(payload.get("meal_id"), "meal_id")
        item_id = self._string(payload.get("item_id"), "item_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        changes = {key: payload[key] for key in ("dish_id", "rating", "order_note") if key in payload}
        if not changes:
            raise BusinessError("INVALID_INPUT", "Provide a dish link, rating or order note")
        if "rating" in changes:
            rating = self._integer(changes["rating"], "rating", minimum=1, nullable=True)
            if rating is not None and rating > 5:
                raise BusinessError("INVALID_INPUT", "Rating must be between one and five")
        for key in {"dish_id", "order_note"} & changes.keys():
            changes[key] = self._string(changes[key], key, nullable=True)

        def operation(connection):
            entry = self._active_meal_row(connection, entry_id)
            self._check_version(entry, entry_id, expected)
            if not connection.execute("SELECT 1 FROM items WHERE item_id=? AND entry_id=?", (item_id, entry_id)).fetchone():
                raise BusinessError("NOT_FOUND", "Meal item not found", {"item_id": item_id})
            if changes.get("dish_id"):
                self._catalog_dish(connection, changes["dish_id"])
            saved = connection.execute("SELECT * FROM meal_item_profiles WHERE item_id=?", (item_id,)).fetchone()
            profile = {**(dict(saved) if saved else dict.fromkeys(("dish_id", "rating", "order_note"))), **changes}
            connection.execute("""INSERT INTO meal_item_profiles VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                dish_id=excluded.dish_id,rating=excluded.rating,order_note=excluded.order_note""",
                (item_id, profile["dish_id"], profile["rating"], profile["order_note"]))
            self._touch_meal(connection, entry_id)
            return {"meal_id": entry_id, "meal_version": expected + 1, "profile": {**profile, "item_id": item_id}}

        return self._write("meal_item_profile_set", payload, operation)

    def _merge_allocation_state(self, connection, source_id, target_id):
        return [dict(row) for row in connection.execute(
            """SELECT a.payment_id,p.entry_id AS purchase_meal_id,a.meal_id,a.amount_cents
               FROM purchase_allocations a JOIN payments p ON p.payment_id=a.payment_id
               WHERE a.meal_id IN (?,?) ORDER BY a.meal_id,a.payment_id""",
            (source_id, target_id),
        )]

    @staticmethod
    def _merge_allocation_transfers(ids, allocation_state):
        source_id, target_id = ids
        source_amounts = {}
        target_amounts = {}
        purchase_meals = {}
        for row in allocation_state:
            payment_id = row["payment_id"]
            purchase_meals[payment_id] = row["purchase_meal_id"]
            if row["meal_id"] == source_id:
                source_amounts[payment_id] = row["amount_cents"]
            else:
                target_amounts[payment_id] = row["amount_cents"]
        return [
            {
                "payment_id": payment_id,
                "purchase_meal_id": purchase_meals[payment_id],
                "from_meal_id": source_id,
                "to_meal_id": target_id,
                "source_amount_cents": amount,
                "target_amount_before_cents": target_amounts.get(payment_id, 0),
                "target_amount_after_cents": amount + target_amounts.get(payment_id, 0),
                "combined_amount_before_cents": amount + target_amounts.get(payment_id, 0),
                "combined_amount_after_cents": amount + target_amounts.get(payment_id, 0),
            }
            for payment_id, amount in sorted(source_amounts.items())
        ]

    def _move_purchase_allocations(self, connection, source_id, target_id):
        source_allocations = connection.execute(
            "SELECT payment_id,amount_cents FROM purchase_allocations WHERE meal_id=? ORDER BY payment_id",
            (source_id,),
        ).fetchall()
        for allocation in source_allocations:
            target_allocation = connection.execute(
                "SELECT amount_cents FROM purchase_allocations WHERE payment_id=? AND meal_id=?",
                (allocation["payment_id"], target_id),
            ).fetchone()
            if target_allocation:
                connection.execute(
                    "UPDATE purchase_allocations SET amount_cents=? WHERE payment_id=? AND meal_id=?",
                    (target_allocation["amount_cents"] + allocation["amount_cents"], allocation["payment_id"], target_id),
                )
                connection.execute(
                    "DELETE FROM purchase_allocations WHERE payment_id=? AND meal_id=?",
                    (allocation["payment_id"], source_id),
                )
            else:
                connection.execute(
                    "UPDATE purchase_allocations SET meal_id=? WHERE payment_id=? AND meal_id=?",
                    (target_id, allocation["payment_id"], source_id),
                )

    def _meal_change_inputs(self, connection, payload):
        action = self._recommendation_enum(payload.get("operation"), "operation", set(_OPERATIONS))
        args = self._dict(payload.get("args"), "args")
        self._reject_extra_fields(args, set(_OPERATIONS[action]["properties"]), "args")
        if action == "batch_edit":
            updates = self._list(args.get("updates"), "updates")
            if not 1 <= len(updates) <= 20:
                raise BusinessError("INVALID_INPUT", "Provide one to twenty updates")
            ids = [self._dict(update, "update").get("meal_id") for update in updates]
        elif action in {"merge", "attach_tea"}:
            ids = [args.get("source_id"), args.get("target_id")]
        else:
            ids = [args.get("meal_id")]
        ids = [self._string(entry_id, "record_id") for entry_id in ids]
        if len(ids) != len(set(ids)):
            raise BusinessError("INVALID_INPUT", "Each existing record must occur once per operation")
        states = [self._revision_state(connection, entry_id) for entry_id in ids]
        if None in states:
            raise BusinessError("NOT_FOUND", "A selected record was not found")
        allocation_state = self._merge_allocation_state(connection, *ids) if action == "merge" else []
        fingerprint_state = [action, args, states]
        if action == "merge":
            fingerprint_state.append(allocation_state)
        fingerprint = hashlib.sha256(self._canonical_json(fingerprint_state).encode()).hexdigest()
        return action, args, ids, states, fingerprint, allocation_state

    def _meal_change_preview(self, payload):
        self._reject_extra_fields(payload, set(_PREVIEW))
        with self.store.transaction() as connection:
            action, args, ids, before, fingerprint, allocation_state = self._meal_change_inputs(connection, payload)
            connection.execute("SAVEPOINT meal_preview")
            try:
                affected = self._apply_meal_change(connection, action, args, ids, "preview-" + uuid.uuid4().hex)
                after = [self._revision_state(connection, entry_id) for entry_id in affected]
            finally:
                connection.execute("ROLLBACK TO meal_preview")
                connection.execute("RELEASE meal_preview")
            result = {"operation": action, "fingerprint": fingerprint, "before": before, "after": after, "would_write": False}
            if action == "merge":
                result["allocation_transfers"] = self._merge_allocation_transfers(ids, allocation_state)
            return result

    def _meal_change_commit(self, payload):
        self._reject_extra_fields(payload, {*_PREVIEW, "request_id", "fingerprint", "confirm"})
        supplied = self._string(payload.get("fingerprint"), "fingerprint")
        if payload.get("confirm") is not True:
            raise BusinessError("INVALID_INPUT", "Explicit confirmation is required")

        def operation(connection):
            action, args, ids, before, fingerprint, allocation_state = self._meal_change_inputs(connection, payload)
            if fingerprint != supplied:
                raise BusinessError("STALE_PREVIEW", "Records or arguments differ from the preview", {
                    "current_versions": {row["id"]: row["version"] for row in before}, "retry_with": "meal_change_preview",
                })
            affected = self._apply_meal_change(connection, action, args, ids, payload["request_id"])
            result = {"operation": action, "items": [self._revision_state(connection, entry_id) for entry_id in affected]}
            if action == "merge":
                result["allocation_transfers"] = self._merge_allocation_transfers(ids, allocation_state)
            return result

        return self._write("meal_change_commit", payload, operation)

    def _touch_meal(self, connection, entry_id):
        connection.execute("UPDATE entries SET version=version+1, updated_at=? WHERE id=?", (self._now(), entry_id))

    def _reprice_meal(self, connection, entry_id, args, key):
        existing = self._active_meal_row(connection, entry_id)
        if key in args:
            amount = self._integer(args[key], key, minimum=0, nullable=True)
            manual = 1
        elif existing["meal_amount_manual"]:
            raise BusinessError("INVALID_INPUT", f"Provide {key} for a meal with a manually recorded total")
        else:
            amount = self._derive_meal_amount(connection, entry_id)
            manual = 0
        connection.execute("UPDATE entries SET meal_amount_cents=?, meal_amount_manual=? WHERE id=?", (amount, manual, entry_id))

    def _moved_entry(self, connection, source, *, kind, args):
        date = self._iso_date(args.get("date", source["entry_date"]), "date")
        meal_type = self._analytics_meal_type(args.get("meal_type", source["meal_type"])) if kind == "meal" else None
        return self._insert_entry(
            connection, record_kind=kind, entry_date=date, meal_type=meal_type, mode=source["mode"] if kind == "meal" else None,
            meal_amount_cents=None, meal_amount_manual=False, location=source["location"], note=None, timing={},
            people_count=source["people_count"] if kind == "meal" else None, consumption_cost=self._validate_consumption_cost(None),
            store_id=source["store_id"] if kind == "meal" else None,
        )

    def _move_meal_rows(self, connection, table, id_column, source, target, selected=None):
        order = " ORDER BY COALESCE(display_order,rowid),rowid" if table == "items" else " ORDER BY rowid"
        rows = connection.execute(f"SELECT {id_column} FROM {table} WHERE entry_id=?" + order, (source,)).fetchall()
        actual = {row[id_column] for row in rows}
        if selected is not None and not set(selected) <= actual:
            raise BusinessError("NOT_FOUND", "A selected item or payment does not belong to the record")
        if selected and table == "payments":
            selected_ids = set(selected)
            for link in connection.execute(
                """SELECT r.original_id,r.refund_id FROM payment_refunds r
                   JOIN payments p ON p.payment_id=r.original_id WHERE p.entry_id=?""", (source,),
            ):
                if (link["original_id"] in selected_ids) != (link["refund_id"] in selected_ids):
                    raise BusinessError("PAYMENT_LINKED", "Move the original payment and all linked refunds together")
        selected = actual if selected is None else selected
        if table == "items":
            highest = connection.execute("SELECT COALESCE(MAX(COALESCE(display_order,rowid)),0) FROM items WHERE entry_id=?", (target,)).fetchone()[0]
            selected_ids = set(selected)
            target_rows = [row[id_column] for row in rows if row[id_column] in selected_ids]
            connection.executemany("UPDATE items SET entry_id=?,display_order=? WHERE item_id=?",
                                   [(target, highest+index+1, item_id) for index, item_id in enumerate(target_rows)])
        else:
            connection.executemany(
                f"UPDATE {table} SET entry_id=? WHERE {id_column}=?", [(target, row_id) for row_id in selected]
            )

    def _row_ids(self, args, key):
        values = self._list(args.get(key), key, default=[])
        if len(values) > 200:
            raise BusinessError("INVALID_INPUT", "Provide at most 200 row IDs")
        return list(dict.fromkeys(self._string(value, key) for value in values))

    def _restore_payment(self, connection, entry_id, source, args, request):
        payment_id = self._string(args.get("payment_id"), "payment_id")
        version = self._integer(args.get("from_version"), "from_version", minimum=1)
        current = self._revision_state(connection, entry_id)
        saved = self._revision_version(connection, entry_id, version, current)
        old = next((row for row in saved["payments"] if row["payment_id"] == payment_id), None)
        if old is None:
            raise BusinessError("NOT_FOUND", "The selected revision has no such payment")
        fields = set(self._row_ids(args, "fields")) if "fields" in args else set(_PAYMENT_FIELDS)
        if not fields or not fields <= _PAYMENT_FIELDS:
            raise BusinessError("INVALID_INPUT", "Select fields from the historical payment", {"supported_fields": sorted(_PAYMENT_FIELDS)})
        if fields & _PAYMENT_AMOUNT:
            fields.update(_PAYMENT_AMOUNT)
        owner = connection.execute("SELECT entry_id FROM payments WHERE payment_id=?", (payment_id,)).fetchone()
        if owner is not None:
            if owner["entry_id"] != entry_id:
                raise BusinessError("PAYMENT_MOVED", "The payment now belongs to a different record", {"meal_id": owner["entry_id"]})
            self._meal_update({"scope": "payment", "meal_id": entry_id, "expected_version": source["version"],
                               "payment_id": payment_id, "changes": {field: old[field] for field in fields},
                               "request_id": self._subrequest(request, 0)})
            return
        if "fields" in args:
            raise BusinessError("INVALID_INPUT", "Restore the entire payment when it no longer exists")
        payment = self._validate_payment({key: old[key] for key in _PAYMENT_FIELDS}, "payment")
        connection.execute(
            "INSERT INTO payments(payment_id,entry_id,kind,scope,amount_cents,amount_min_cents,amount_max_cents,source,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (payment_id, entry_id, *(payment[key] for key in ("kind", "scope", "amount_cents", "amount_min_cents", "amount_max_cents", "source", "note"))),
        )
        if not source["meal_amount_manual"]:
            connection.execute("UPDATE entries SET meal_amount_cents=? WHERE id=?", (self._derive_meal_amount(connection, entry_id), entry_id))
        self._touch_meal(connection, entry_id)

    def _merge_selected_rows(self, connection, ids, args):
        source, target = (self._revision_state(connection, entry_id) for entry_id in ids)
        for table, identifiers, selector, comparable in (
            ("items", "item_id", "move_item_ids", ("name", "quantity", "unit")),
            ("payments", "payment_id", "move_payment_ids", ("kind", "scope", "amount_cents", "amount_min_cents", "amount_max_cents")),
        ):
            moving = self._row_ids(args, selector) if selector in args else None
            if moving is None:
                target_signatures = {tuple(row[key] for key in comparable) for row in target[table]}
                duplicates = [row[identifiers] for row in source[table] if tuple(row[key] for key in comparable) in target_signatures]
                if duplicates:
                    raise BusinessError("MERGE_SELECTION_REQUIRED", "Some rows overlap; specify the source rows to move", {
                        "selector": selector, "overlap_ids": duplicates,
                    })
            self._move_meal_rows(connection, table, identifiers, *ids, moving)

    def _apply_meal_change(self, connection, operation, args, ids, request):
        for entry_id in ids:
            self._audit_meal(connection, entry_id, "baseline")
        if operation == "batch_edit":
            for index, patch in enumerate(args["updates"]):
                self._meal_update({**patch, "request_id": self._subrequest(request, index)})
            return ids
        entry_id = ids[0]
        source = self._active_meal_row(connection, entry_id, "milk_tea" if operation == "attach_tea" else "meal")
        if operation == "restore_payment":
            self._restore_payment(connection, entry_id, source, args, request)
        elif operation == "restore_fields":
            fields = self._row_ids(args, "fields")
            if not fields or not set(fields) <= _RESTORABLE:
                raise BusinessError("INVALID_INPUT", "Select restorable meal fields", {"supported_fields": sorted(_RESTORABLE)})
            current = self._revision_state(connection, entry_id)
            version = self._integer(args.get("from_version"), "from_version", minimum=1)
            saved = self._revision_version(connection, entry_id, version, current)
            changes = {field: saved[field] for field in fields}
            if "store" in changes:
                changes["store_id"] = (changes.pop("store") or {}).get("store_id")
            restore_derived = "meal_amount_cents" in changes and not saved["meal_amount_manual"]
            if restore_derived:
                changes.pop("meal_amount_cents")
                connection.execute("UPDATE entries SET meal_amount_manual=0,meal_amount_cents=? WHERE id=?",
                                   (self._derive_meal_amount(connection, entry_id), entry_id))
            if changes:
                self._meal_update({"scope": "meal", "meal_id": entry_id, "expected_version": source["version"], "changes": changes,
                                   "request_id": self._subrequest(request, 0)})
            else:
                self._touch_meal(connection, entry_id)
        elif operation == "edit_items":
            additions = self._validate_items(args.get("add", []))
            remove = self._row_ids(args, "remove")
            if not additions and not remove:
                raise BusinessError("INVALID_INPUT", "Provide items to add or remove")
            for item_id in remove:
                if not connection.execute("DELETE FROM items WHERE item_id=? AND entry_id=?", (item_id, entry_id)).rowcount:
                    raise BusinessError("NOT_FOUND", "Meal item not found", {"item_id": item_id})
            self._insert_items(connection, entry_id, additions)
            self._touch_meal(connection, entry_id)
        elif operation in {"merge", "attach_tea"}:
            target = self._active_meal_row(connection, ids[1])
            if operation == "merge":
                if any(source[key] != target[key] for key in ("calendar_date", "meal_type", "mode")):
                    raise BusinessError("INVALID_INPUT", "Merge requires the same date, meal slot and mode")
                source_store, target_store = (self._history_entry(connection, key)["canonical_store"] for key in ids)
                if source_store and (not target_store or target_store["store_id"] != source_store["store_id"]):
                    raise BusinessError("INVALID_INPUT", "Resolve conflicting store identities before merging")
                if source["meal_amount_manual"] and "target_amount_cents" not in args:
                    raise BusinessError("INVALID_INPUT", "Provide the confirmed target total or null")
                self._merge_selected_rows(connection, ids, args)
                self._reprice_meal(connection, ids[1], args, "target_amount_cents")
                self._move_purchase_allocations(connection, *ids)
            else:
                if source["note"]:
                    connection.execute("UPDATE payments SET note=COALESCE(note,?) WHERE entry_id=?", (source["note"], entry_id))
                self._move_meal_rows(connection, "payments", "payment_id", entry_id, ids[1])
            connection.execute("UPDATE entries SET voided_at=?,void_reason=? WHERE id=?", (self._now(), operation, entry_id))
            connection.execute("INSERT INTO meal_redirects VALUES(?,?,?)", (*ids, operation))
            for record in ids:
                self._touch_meal(connection, record)
        elif operation in {"split", "detach_tea"}:
            if operation == "detach_tea":
                payment_id = self._string(args.get("payment_id"), "payment_id")
                if not connection.execute("SELECT 1 FROM payments WHERE entry_id=? AND payment_id=? AND kind='milk_tea' AND scope='extra'",
                                          (entry_id, payment_id)).fetchone():
                    raise BusinessError("NOT_FOUND", "Attached milk tea payment not found")
                target_id = self._moved_entry(connection, source, kind="milk_tea", args={})
                self._move_meal_rows(connection, "payments", "payment_id", entry_id, target_id, [payment_id])
            else:
                item_ids, payment_ids = self._row_ids(args, "item_ids"), self._row_ids(args, "payment_ids")
                if not item_ids and not payment_ids:
                    raise BusinessError("INVALID_INPUT", "Select at least one dish or payment for the new meal")
                if "meal_type" in args and args["meal_type"] is None:
                    raise BusinessError("INVALID_INPUT", "meal_type cannot be null")
                target_id = self._moved_entry(connection, source, kind="meal", args=args)
                self._move_meal_rows(connection, "items", "item_id", entry_id, target_id, item_ids)
                self._move_meal_rows(connection, "payments", "payment_id", entry_id, target_id, payment_ids)
                if source["meal_amount_manual"] and "target_amount_cents" not in args:
                    raise BusinessError("INVALID_INPUT", "Provide the new meal total or null when splitting a manually priced meal")
                self._reprice_meal(connection, entry_id, args, "source_amount_cents")
                self._reprice_meal(connection, target_id, args, "target_amount_cents")
            ids.append(target_id)
            self._touch_meal(connection, entry_id)
        for record in ids:
            self._audit_meal(connection, record, operation)
        return ids

    @staticmethod
    def _subrequest(parent, index):
        return "sub:" + hashlib.sha256(f"{parent}:{index}".encode()).hexdigest()
