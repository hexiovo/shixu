from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from typing import Any

from .errors import BusinessError
from .tool_schema import _date, _integer, _nullable_string, _request_id, _string, _tool


_PROFILE_FIELDS = {
    "request_id": _request_id,
    "store_id": _string,
    "expected_version": _integer,
    "environment": _nullable_string,
    "occasions": {"type": "array", "items": _string, "maxItems": 24},
    "note": _nullable_string,
}
_DIET_FIELDS = {"date_from": _date, "date_to": _date, "meal_limit": {"type": "integer", "minimum": 1, "maximum": 50}}
TOOL_SCHEMAS = [
    _tool("store_profile_set", "Set a store's recorded environment, suitable occasions and notes. Check the current profile version before editing.", _PROFILE_FIELDS, ("request_id", "store_id")),
    _tool("store_profile_get", "Read a store's environment, occasions and notes by its canonical store ID.", {"store_id": _string}, ("store_id",)),
    _tool("meal_diet_insights", "Review confirmed and rule-derived tags from recent meals, with the matching records and general suggestions.", _DIET_FIELDS),
    _tool("meal_report", "Build one weekly or monthly report with meal frequency, spending, budget and dietary trends.", {"period": {"enum": ["week", "month"]}, "anchor": _date}, ("period",)),
]
HANDLERS = {
    "store_profile_set": "_store_profile_set",
    "store_profile_get": "_store_profile_get",
    "meal_diet_insights": "_meal_diet_insights",
    "meal_report": "_meal_report",
}
MIGRATION_SQL = [
    """CREATE TABLE store_profiles (
        store_id TEXT PRIMARY KEY REFERENCES stores(store_id),
        environment TEXT,
        occasions_json TEXT NOT NULL,
        note TEXT,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
]
_TAG_TERMS = {
    "火锅": ("火锅", "涮锅"),
    "烧烤": ("烧烤", "烤串", "烤肉"),
    "偏油": ("油炸", "炸鸡", "干锅", "红油"),
    "清淡": ("清蒸", "白灼", "清淡"),
    "奶茶": ("奶茶",),
}


class DaybookMixin:
    def _read_store_profile(self, connection, store_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM store_profiles WHERE store_id=?", (store_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["occasions"] = json.loads(result.pop("occasions_json"))
        return result

    def _store_profile_identity(self, connection, store_id: str):
        key = self._canonical_store_id(connection, store_id)
        store = connection.execute("SELECT store_id, name, branch FROM stores WHERE store_id=?", (key,)).fetchone()
        if store is None:
            raise BusinessError("NOT_FOUND", "Store not found", {"store_id": store_id})
        return dict(store)

    def _store_profile_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"store_id"})
        store_id = self._string(payload.get("store_id"), "store_id")
        with self.store.read() as connection:
            store = self._store_profile_identity(connection, store_id)
            profile = self._read_store_profile(connection, store["store_id"])
        return {"store": store, "profile": profile}

    def _store_profile_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_PROFILE_FIELDS))
        store_id = self._string(payload.get("store_id"), "store_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        changes = {key: payload[key] for key in ("environment", "occasions", "note") if key in payload}
        if not changes:
            raise BusinessError("INVALID_INPUT", "Supply an environment, occasion list or note")
        for key in ("environment", "note"):
            if key in changes:
                changes[key] = self._string(changes[key], key, nullable=True)
        if "occasions" in changes:
            values = changes["occasions"]
            if not isinstance(values, list) or len(values) > 24:
                raise BusinessError("INVALID_INPUT", "occasions must contain at most 24 names")
            changes["occasions"] = list(dict.fromkeys(self._string(value, "occasion") for value in values))

        def operation(connection):
            store = self._store_profile_identity(connection, store_id)
            key = store["store_id"]
            previous = self._read_store_profile(connection, key)
            if previous is not None:
                if expected is None:
                    raise BusinessError("INVALID_INPUT", "Editing a profile requires expected_version")
                self._check_version(previous, key, expected)
            elif expected is not None:
                raise BusinessError("VERSION_CONFLICT", "The store profile does not exist")
            profile = {**(previous or {"environment": None, "occasions": [], "note": None, "version": 0}), **changes}
            connection.execute(
                """INSERT INTO store_profiles(store_id, environment, occasions_json, note, version, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(store_id) DO UPDATE SET
                   environment=excluded.environment, occasions_json=excluded.occasions_json, note=excluded.note,
                   version=excluded.version, updated_at=excluded.updated_at""",
                (key, profile["environment"], self._json(profile["occasions"]), profile["note"], profile["version"] + 1, self._now()),
            )
            return {"store": store, "profile": self._read_store_profile(connection, key)}

        return self._write("store_profile_set", payload, operation)

    def _read_candidate_occasion(self, connection, record: dict[str, Any], requested: str | None) -> dict[str, Any]:
        details = record["details"]
        existing = None
        store_id = details.get("store_id")
        if isinstance(store_id, str) and record["mode"] != "home_cooking":
            existing = self._read_store_profile(connection, self._canonical_store_id(connection, store_id))
        own = details.get("occasions")
        options = list(dict.fromkeys(
            [value.strip() for value in own if isinstance(value, str) and value.strip()] if isinstance(own, list) else []
        ))
        if existing:
            options = list(dict.fromkeys([*options, *existing["occasions"]]))
        status = "not_requested"
        if requested:
            if not options:
                status = "needs_verification"
            elif requested.casefold() in {value.casefold() for value in options}:
                status = "matched"
            else:
                status = "not_listed"
        return {
            "requested": requested,
            "status": status,
            "recorded_occasions": options,
            "environment": details.get("environment") or (existing["environment"] if existing else None),
            "note": existing["note"] if existing else None,
        }

    @staticmethod
    def _daybook_tags(names: list[str], confirmed: list[str], milk_tea: bool) -> list[dict[str, str]]:
        tags = {tag: {"name": tag, "source": "confirmed", "basis": "已记录标签"} for tag in confirmed}
        for tag, terms in _TAG_TERMS.items():
            if tag in tags or (tag == "偏油" and "清淡" in tags) or (tag == "清淡" and "偏油" in tags):
                continue
            match = next((name for name in names if any(term in name for term in terms)), None)
            if match or (tag == "奶茶" and milk_tea):
                tags[tag] = {"name": tag, "source": "rule", "basis": match or "奶茶付款明细"}
        return list(tags.values())

    def _meal_diet_insights(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_DIET_FIELDS))
        last = date.fromisoformat(self._iso_date(payload["date_to"], "date_to")) if payload.get("date_to") else date.today()
        first = date.fromisoformat(self._iso_date(payload["date_from"], "date_from")) if payload.get("date_from") else last - timedelta(days=6)
        limit = self._integer(payload.get("meal_limit", 7), "meal_limit", minimum=1)
        if first > last or limit > 50:
            raise BusinessError("INVALID_INPUT", "Use an ordered date range and up to 50 meals")
        span = {"date_from": first.isoformat(), "date_to": last.isoformat()}
        result = []
        counts = Counter()
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT id, version, calendar_date, meal_type FROM active_entries WHERE record_kind='meal'
                   AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date DESC,
                   COALESCE(json_extract(timing_json, '$.ended_at'), created_at) DESC, id DESC LIMIT ?""",
                (span["date_from"], span["date_to"], limit),
            ).fetchall()
            for row in rows:
                names = [item["name"] for item in connection.execute("SELECT name FROM items WHERE entry_id=?", (row["id"],))]
                manual = [item["tag"] for item in connection.execute(
                    "SELECT tag FROM meal_tags WHERE entry_id=? AND meal_version=? ORDER BY tag", (row["id"], row["version"])
                )]
                tea = connection.execute(
                    "SELECT 1 FROM payments WHERE entry_id=? AND kind='milk_tea' LIMIT 1", (row["id"],)
                ).fetchone() is not None
                tags = self._daybook_tags(names, manual, tea)
                counts.update(tag["name"] for tag in tags)
                result.append({"meal_id": row["id"], "date": row["calendar_date"], "meal_type": row["meal_type"], "dishes": names, "tags": tags})
            tea_purchases = self._meal_stats({**span, "top_limit": 1})["milk_tea_purchases"]["count"]
        suggestions = []
        rich_meals = sum(any(tag["name"] in {"偏油", "火锅", "烧烤"} for tag in item["tags"]) for item in result)
        if rich_meals >= 2:
            suggestions.append({"message": "下一餐可以换一种相对清淡的做法。", "basis": f"最近查看的餐次中有 {rich_meals} 餐含偏油、火锅或烧烤标签"})
        if tea_purchases >= 3:
            suggestions.append({"message": "接下来一餐可以先不加奶茶。", "basis": f"所查时段记录了 {tea_purchases} 笔奶茶"})
        return {
            **span, "meals": result, "meals_reviewed": len(result), "tag_counts": dict(counts),
            "milk_tea_purchase_count": tea_purchases, "suggestions": suggestions,
        }

    def _meal_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, {"period", "anchor"})
        period = self._string(payload.get("period"), "period")
        if period not in {"week", "month"}:
            raise BusinessError("INVALID_INPUT", "period must be week or month")
        anchor = date.fromisoformat(self._iso_date(payload["anchor"], "anchor")) if payload.get("anchor") else date.today()
        if period == "week":
            start = anchor - timedelta(days=anchor.weekday())
            finish = start + timedelta(days=6)
        else:
            start = anchor.replace(day=1)
            finish = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        span = {"date_from": start.isoformat(), "date_to": finish.isoformat()}
        with self.store.read() as connection:
            frequency = self._meal_stats(span)
            rows = connection.execute(
                """SELECT id, calendar_date, meal_type, mode FROM active_entries WHERE record_kind='meal'
                   AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date DESC, created_at DESC, id DESC LIMIT 30""",
                (span["date_from"], span["date_to"]),
            ).fetchall()
            meals = []
            for row in rows:
                item = self._entry(connection, row["id"])
                meals.append({
                    "id": row["id"], "date": row["calendar_date"], "meal_type": row["meal_type"], "mode": row["mode"],
                    "dishes": [dish["name"] for dish in item["items"]], "store": item["store"],
                })
            return {
                "period": period, **span,
                "frequency": frequency,
                "spending": self._spending_summary(span),
                "budget": self._budget_status({"period_type": period, "period_start": span["date_from"]}),
                "diet": self._meal_diet_insights(span),
                "meals": meals,
                "total_meals": sum(frequency["mode_counts"].values()),
            }
