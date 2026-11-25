from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from .errors import BusinessError
from .recommendation_score import score_weights
from .tool_schema import _object, _tool


_SOURCE_TYPE = {"type": "string", "enum": ["candidate", "recipe"]}
_PREFERENCE = {"type": "string", "enum": ["liked", "want_to_try", "temporarily_tired"]}
_MODE = {"type": "string", "enum": ["eat_out", "delivery", "home_cooking"]}
_MEAL_TYPE = {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "other"]}
_DATE_TIME = {"type": ["string", "null"], "format": "date-time"}
_PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "offset": {"type": "integer", "minimum": 0},
}
_RANGE = _object(
    {
        "min_minutes": {"type": "integer", "minimum": 0},
        "max_minutes": {"type": "integer", "minimum": 0},
    },
    ("min_minutes", "max_minutes"),
)
_TIME_STAGES = {
    "outbound": _RANGE,
    "queue": _RANGE,
    "wait": _RANGE,
    "order_to_ready": _RANGE,
    "shopping": _RANGE,
    "prep": _RANGE,
    "cook": _RANGE,
    "meal": _RANGE,
    "return": _RANGE,
}
_EVENT_KINDS = [
    "departed",
    "arrived",
    "ordered",
    "ready",
    "received",
    "meal_started",
    "meal_finished",
    "returned",
    "shopping_started",
    "shopping_finished",
    "prep_started",
    "cooking_started",
    "meal_ready",
]
_EVENT_ORDER = {
    "eat_out": ["departed", "arrived", "ordered", "ready", "meal_started", "meal_finished", "returned"],
    "delivery": ["ordered", "ready", "received", "meal_started", "meal_finished", "returned"],
    "home_cooking": [
        "shopping_started",
        "shopping_finished",
        "prep_started",
        "cooking_started",
        "meal_ready",
        "meal_started",
        "meal_finished",
        "returned",
    ],
}
_PROFILE_STAGES = {
    "eat_out": {"outbound", "queue", "wait", "meal", "return"},
    "delivery": {"order_to_ready", "meal", "return"},
    "home_cooking": {"shopping", "prep", "cook", "meal", "return"},
}
_EVENT_STAGES = {
    "eat_out": {
        "outbound": ("departed", "arrived"),
        "queue": ("arrived", "ordered"),
        "wait": ("ordered", "ready"),
        "meal": ("meal_started", "meal_finished"),
        "return": ("meal_finished", "returned"),
    },
    "delivery": {
        "meal": ("meal_started", "meal_finished"),
        "return": ("meal_finished", "returned"),
    },
    "home_cooking": {
        "shopping": ("shopping_started", "shopping_finished"),
        "prep": ("prep_started", "cooking_started"),
        "cook": ("cooking_started", "meal_ready"),
        "meal": ("meal_started", "meal_finished"),
        "return": ("meal_finished", "returned"),
    },
}


TOOL_SCHEMAS = [
    _tool("recommendation_defaults_get", "Read the current recommendation defaults.", {}),
    _tool(
        "recommendation_defaults_set",
        "Set or clear selected recommendation defaults. Explicit recommend inputs always take precedence.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "values": _object(
                {
                    "mode": _MODE,
                    "location": {"type": ["string", "null"]},
                    "return_location": {"type": ["string", "null"]},
                    "people_count": {"type": ["integer", "null"], "minimum": 1},
                    "budget_cents": {"type": ["integer", "null"], "minimum": 0},
                    "budget_basis": {"type": ["string", "null"], "enum": ["payment", "consumption", None]},
                    "budget_scope": {"type": ["string", "null"], "enum": ["total", "personal_share", None]},
                    "time_target": {"type": ["string", "null"], "enum": ["meal_start", "finished", "returned", None]},
                    "meal_type": {"type": ["string", "null"], "enum": ["breakfast", "lunch", "dinner", "snack", "other", None]},
                    "repeat_lookback_days": {"type": ["integer", "null"], "minimum": 1},
                    "weekday_only": {"type": ["boolean", "null"]},
                    "for_meal_type": {"type": ["string", "null"], "enum": ["breakfast", "lunch", "dinner", "snack", "other", None]},
                    "score_weights": {"type": ["object", "null"]},
                }
            ),
        },
        ("request_id", "values"),
    ),
    _tool(
        "recommendation_history_list",
        "List explicit meal links and user skip feedback for a candidate or recipe. Only active meal records are included.",
        {
            "source_type": _SOURCE_TYPE,
            "source_id": {"type": "string"},
            "record_type": {"type": "string", "enum": ["all", "meal", "skip"]},
            **_PAGE,
        },
    ),
    _tool(
        "recommendation_meal_link_set",
        "Explicitly link an active meal to the candidate or recipe that was actually eaten.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "meal_id": {"type": "string"},
            "source_type": _SOURCE_TYPE,
            "source_id": {"type": "string"},
            "note": {"type": ["string", "null"]},
        },
        ("request_id", "meal_id", "source_type", "source_id"),
    ),
    _tool(
        "recommendation_preference_set",
        "Set or clear one candidate or recipe preference. Use want_to_try for the shared 想吃清单 (待尝); set enabled=false to remove it.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "source_type": _SOURCE_TYPE,
            "source_id": {"type": "string"},
            "preference": _PREFERENCE,
            "enabled": {"type": "boolean"},
            "reason": {"type": ["string", "null"]},
            "expires_at": _DATE_TIME,
        },
        ("request_id", "source_type", "source_id", "preference"),
    ),
    _tool(
        "recommendation_preference_list",
        "List saved preferences with candidate or recipe names and meal modes. Use preference=want_to_try for the 想吃清单 (待尝). Includes expired temporary preferences when they match.",
        {"source_type": _SOURCE_TYPE, "source_id": {"type": "string"}, "preference": _PREFERENCE, **_PAGE},
    ),
    _tool(
        "recommendation_skip_record",
        "Record an explicit user skip reason for a candidate or recipe and its meal context.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "source_type": _SOURCE_TYPE,
            "source_id": {"type": "string"},
            "reason": {"type": "string"},
            "mode": _MODE,
            "meal_type": _MEAL_TYPE,
            "location": {"type": ["string", "null"]},
            "skipped_at": _DATE_TIME,
        },
        ("request_id", "source_type", "source_id", "reason"),
    ),
    _tool(
        "recommendation_skip_list",
        "List explicit skip feedback with its original time, meal type, mode, and location.",
        {"source_type": _SOURCE_TYPE, "source_id": {"type": "string"}, **_PAGE},
    ),
    _tool(
        "recommendation_time_profile_set",
        "Set user-provided duration ranges for one exact mode, route, store identity, and optional meal type.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "mode": _MODE,
            "origin_location": {"type": ["string", "null"]},
            "destination_location": {"type": ["string", "null"]},
            "store_id": {"type": ["string", "null"]},
            "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
            "meal_type": {"anyOf": [_MEAL_TYPE, {"type": "null"}]},
            "stages": _object(_TIME_STAGES),
        },
        ("request_id", "mode", "stages"),
    ),
    _tool(
        "recommendation_time_profile_list",
        "List exact user-provided and observed duration ranges for a route and meal type.",
        {
            "mode": _MODE,
            "origin_location": {"type": ["string", "null"]},
            "destination_location": {"type": ["string", "null"]},
            "store_id": {"type": ["string", "null"]},
            "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
            "meal_type": {"anyOf": [_MEAL_TYPE, {"type": "null"}]},
            **_PAGE,
        },
        ("mode",),
    ),
    _tool(
        "meal_event_set",
        "Set an actual or expected meal journey event. Times are manual or profile estimates; no delivery platform is contacted.",
        {
            "request_id": {"type": "string", "minLength": 1},
            "meal_id": {"type": "string"},
            "event_kind": {"type": "string", "enum": _EVENT_KINDS},
            "status": {"type": "string", "enum": ["planned", "in_progress", "completed", "skipped"]},
            "actual_at": _DATE_TIME,
            "expected_earliest_at": _DATE_TIME,
            "expected_latest_at": _DATE_TIME,
            "time_source": {"type": "string", "enum": ["manual", "estimate"]},
            "expected_source": {"type": ["string", "null"], "enum": ["manual", "estimate", None]},
            "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
            "note": {"type": ["string", "null"]},
            "origin_location": {"type": ["string", "null"]},
            "destination_location": {"type": ["string", "null"]},
            "store_id": {"type": ["string", "null"]},
        },
        ("request_id", "meal_id", "event_kind"),
    ),
    _tool(
        "meal_event_list",
        "Read a meal's actual and expected timeline plus current order progress when applicable.",
        {"meal_id": {"type": "string"}, "now": _DATE_TIME},
        ("meal_id",),
    ),
    _tool(
        "recommendation_countdown",
        "Work backward from a target meal time using exact route ranges; missing stages remain explicit.",
        {
            "mode": _MODE,
            "target_at": {"type": "string", "format": "date-time"},
            "target": {"type": "string", "enum": ["meal_start", "finished", "returned"]},
            "origin_location": {"type": ["string", "null"]},
            "destination_location": {"type": ["string", "null"]},
            "store_id": {"type": ["string", "null"]},
            "fulfillment": {"type": ["string", "null"], "enum": ["delivery", "pickup", None]},
            "meal_type": {"anyOf": [_MEAL_TYPE, {"type": "null"}]},
            "shopping_required": {"type": ["boolean", "null"]},
        },
        ("mode", "target_at"),
    ),
]


HANDLERS = {
    "recommendation_defaults_get": "_recommendation_defaults_get",
    "recommendation_defaults_set": "_recommendation_defaults_set",
    "recommendation_history_list": "_recommendation_history_list",
    "recommendation_meal_link_set": "_recommendation_meal_link_set",
    "recommendation_preference_set": "_recommendation_preference_set",
    "recommendation_preference_list": "_recommendation_preference_list",
    "recommendation_skip_record": "_recommendation_skip_record",
    "recommendation_skip_list": "_recommendation_skip_list",
    "recommendation_time_profile_set": "_recommendation_time_profile_set",
    "recommendation_time_profile_list": "_recommendation_time_profile_list",
    "meal_event_set": "_meal_event_set",
    "meal_event_list": "_meal_event_list",
    "recommendation_countdown": "_recommendation_countdown",
}


MIGRATION_SQL = [
    "CREATE TABLE IF NOT EXISTS recommendation_meal_links ("
    "meal_id TEXT PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE, "
    "source_type TEXT NOT NULL CHECK(source_type IN ('candidate', 'recipe')), "
    "source_id TEXT NOT NULL, note TEXT, linked_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS recommendation_meal_links_source "
    "ON recommendation_meal_links(source_type, source_id, linked_at DESC)",
    "CREATE TABLE IF NOT EXISTS recommendation_preferences ("
    "source_type TEXT NOT NULL CHECK(source_type IN ('candidate', 'recipe')), "
    "source_id TEXT NOT NULL, preference TEXT NOT NULL "
    "CHECK(preference IN ('liked', 'want_to_try', 'temporarily_tired')), "
    "reason TEXT, expires_at TEXT, updated_at TEXT NOT NULL, "
    "PRIMARY KEY(source_type, source_id, preference), "
    "CHECK((preference = 'temporarily_tired' AND expires_at IS NOT NULL) "
    "OR (preference <> 'temporarily_tired' AND expires_at IS NULL)))",
    "CREATE TABLE IF NOT EXISTS recommendation_skips ("
    "skip_id TEXT PRIMARY KEY, "
    "source_type TEXT NOT NULL CHECK(source_type IN ('candidate', 'recipe')), "
    "source_id TEXT NOT NULL, reason TEXT NOT NULL, mode TEXT "
    "CHECK(mode IN ('eat_out', 'delivery', 'home_cooking') OR mode IS NULL), "
    "meal_type TEXT CHECK(meal_type IN ('breakfast', 'lunch', 'dinner', 'snack', 'other') OR meal_type IS NULL), "
    "location TEXT, skipped_at TEXT NOT NULL, recorded_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS recommendation_skips_source_time "
    "ON recommendation_skips(source_type, source_id, skipped_at DESC)",
    "CREATE TABLE IF NOT EXISTS recommendation_time_profiles ("
    "profile_id TEXT PRIMARY KEY, "
    "mode TEXT NOT NULL CHECK(mode IN ('eat_out', 'delivery', 'home_cooking')), "
    "origin_location TEXT, destination_location TEXT, "
    "store_id TEXT REFERENCES stores(store_id), "
    "fulfillment TEXT CHECK(fulfillment IN ('delivery', 'pickup') OR fulfillment IS NULL), "
    "meal_type TEXT CHECK(meal_type IN ('breakfast', 'lunch', 'dinner', 'snack', 'other') OR meal_type IS NULL), "
    "stages_json TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS recommendation_time_profiles_route "
    "ON recommendation_time_profiles(mode, origin_location, destination_location, store_id, fulfillment, meal_type)",
    "CREATE TABLE IF NOT EXISTS meal_timing_events ("
    "event_id TEXT PRIMARY KEY, meal_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE, "
    "event_kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'completed', 'skipped')), "
    "actual_at TEXT, expected_earliest_at TEXT, expected_latest_at TEXT, "
    "time_source TEXT NOT NULL CHECK(time_source IN ('manual', 'estimate')), "
    "expected_source TEXT CHECK(expected_source IN ('manual', 'estimate') OR expected_source IS NULL), "
    "fulfillment TEXT CHECK(fulfillment IN ('delivery', 'pickup') OR fulfillment IS NULL), "
    "note TEXT, origin_location TEXT, destination_location TEXT, "
    "store_id TEXT REFERENCES stores(store_id), updated_at TEXT NOT NULL, "
    "UNIQUE(meal_id, event_kind), "
    "CHECK((expected_earliest_at IS NULL AND expected_latest_at IS NULL) "
    "OR (expected_earliest_at IS NOT NULL AND expected_latest_at IS NOT NULL)))",
    "CREATE INDEX IF NOT EXISTS meal_timing_events_meal "
    "ON meal_timing_events(meal_id, event_kind)",
]


class RecommendationHistoryMixin:
    _DEFAULT_FIELDS = {
        "mode",
        "location",
        "return_location",
        "people_count",
        "budget_cents",
        "budget_basis",
        "budget_scope",
        "time_target",
        "meal_type",
        "repeat_lookback_days",
        "weekday_only",
        "for_meal_type",
        "score_weights",
    }

    @staticmethod
    def _recommendation_reject_fields(payload: dict[str, Any], allowed: set[str]) -> None:
        extra = next((name for name in payload if name not in allowed), None)
        if extra is not None:
            raise BusinessError("INVALID_INPUT", "payload contains unsupported fields", {"field": extra})

    @staticmethod
    def _recommendation_datetime(value: Any, field: str, *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value.strip():
            raise BusinessError("INVALID_INPUT", f"{field} must be an ISO date-time", {"field": field})
        text = value.strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", f"{field} must be an ISO date-time", {"field": field}) from exc
        return text

    @staticmethod
    def _recommendation_datetime_value(value: str) -> datetime:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return result

    @staticmethod
    def _recommendation_enum(value: Any, field: str, values: set[str], *, nullable: bool = False) -> str | None:
        if value is None and nullable:
            return None
        if value not in values:
            raise BusinessError("INVALID_INPUT", f"{field} is unsupported", {"field": field})
        return value

    @staticmethod
    def _recommendation_page(payload: dict[str, Any]) -> tuple[int, int]:
        limit = payload.get("limit", 50)
        offset = payload.get("offset", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise BusinessError("INVALID_INPUT", "limit must be between 1 and 200", {"field": "limit"})
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise BusinessError("INVALID_INPUT", "offset must be non-negative", {"field": "offset"})
        return limit, offset

    @staticmethod
    def _recommendation_source(connection: sqlite3.Connection, source_type: str, source_id: str) -> None:
        table, key = ("candidates", "candidate_id") if source_type == "candidate" else ("recipes", "recipe_id")
        if connection.execute(f"SELECT 1 FROM {table} WHERE {key} = ?", (source_id,)).fetchone() is None:
            raise BusinessError("NOT_FOUND", f"{source_type} was not found", {f"{source_type}_id": source_id})

    @staticmethod
    def _recommendation_canonical_store(connection: sqlite3.Connection, store_id: str | None) -> str | None:
        if store_id is None:
            return None
        row = connection.execute("SELECT store_id FROM stores WHERE store_id = ?", (store_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "store was not found", {"store_id": store_id})
        alias = connection.execute(
            "SELECT canonical_store_id FROM meal_store_aliases WHERE alias_store_id = ?",
            (store_id,),
        ).fetchone()
        return alias["canonical_store_id"] if alias else store_id

    @staticmethod
    def _recommendation_store_from_details(
        connection: sqlite3.Connection, details: dict[str, Any]
    ) -> str | None:
        store_id = details.get("store_id")
        if isinstance(store_id, str):
            row = connection.execute("SELECT 1 FROM stores WHERE store_id = ?", (store_id,)).fetchone()
            return RecommendationHistoryMixin._recommendation_canonical_store(connection, store_id) if row else None
        store = details.get("store")
        if not isinstance(store, dict) or not isinstance(store.get("name"), str):
            return None
        row = connection.execute(
            "SELECT store_id FROM stores WHERE name = ? AND branch IS ?",
            (store["name"], store.get("branch")),
        ).fetchone()
        if row is None:
            return None
        return RecommendationHistoryMixin._recommendation_canonical_store(connection, row["store_id"])

    def _recommendation_defaults(
        self, connection: sqlite3.Connection, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        row = connection.execute(
            "SELECT value_json FROM preferences WHERE preference_key = 'recommendation_defaults'"
        ).fetchone()
        defaults = json.loads(row["value_json"]) if row else {}
        effective = dict(arguments)
        used = []
        weekday_only = defaults.pop("weekday_only", False)
        for_meal_type = defaults.pop("for_meal_type", None)
        if weekday_only:
            query_date = self._parse_now(arguments.get("now")).date()
            if query_date.weekday() >= 5:
                return effective, used
        if for_meal_type is not None and arguments.get("meal_type") != for_meal_type:
            return effective, used
        for field, value in defaults.items():
            if field not in arguments:
                effective[field] = value
                used.append(field)
        return effective, used

    def _recommendation_context(
        self,
        connection: sqlite3.Connection,
        effective_arguments: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        now = self._recommendation_now(effective_arguments.get("now"))
        lookback_days = (
            self._integer(effective_arguments["repeat_lookback_days"], "repeat_lookback_days", minimum=1)
            if "repeat_lookback_days" in effective_arguments
            else 30
        )
        cutoff = (now.date() - timedelta(days=lookback_days - 1)).isoformat()
        mode_filter = effective_arguments.get("mode")
        meal_type = effective_arguments.get("meal_type")
        location = effective_arguments.get("location")
        source_ids = {
            source_type: list(dict.fromkeys(candidate["id"] for candidate in candidates if candidate["source_type"] == source_type))
            for source_type in ("candidate", "recipe")
        }
        meals_by_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
        recent_counts: dict[tuple[str, str], int] = {}
        preferences_by_source: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        skips_by_source: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for source_type, ids in source_ids.items():
            for offset in range(0, len(ids), 500):
                id_chunk = ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in id_chunk)
                source_params = (source_type, *id_chunk)
                meal_rows = connection.execute(
                    "WITH ranked AS ("
                    "SELECT l.source_id, e.id AS meal_id, e.calendar_date, e.meal_type, e.mode, e.location, "
                    "COALESCE(msi.store_id, e.store_id) AS store_id, s.name AS store_name, s.branch AS store_branch, "
                    "l.linked_at, l.note, ROW_NUMBER() OVER (PARTITION BY l.source_id "
                    "ORDER BY e.calendar_date DESC, l.linked_at DESC) AS history_rank "
                    "FROM recommendation_meal_links l "
                    "JOIN active_entries e ON e.id = l.meal_id "
                    "LEFT JOIN meal_store_identity msi ON msi.entry_id = e.id "
                    "LEFT JOIN stores s ON s.store_id = msi.store_id "
                    f"WHERE l.source_type = ? AND l.source_id IN ({placeholders}) AND e.calendar_date <= ?"
                    ") SELECT source_id, meal_id, calendar_date, meal_type, mode, location, store_id, store_name, "
                    "store_branch, linked_at, note FROM ranked WHERE history_rank <= 10 "
                    "ORDER BY source_id, history_rank",
                    (*source_params, now.date().isoformat()),
                ).fetchall()
                for row in meal_rows:
                    meals_by_source.setdefault((source_type, row["source_id"]), []).append(
                        {field: row[field] for field in (
                            "meal_id", "calendar_date", "meal_type", "mode", "location", "store_id",
                            "store_name", "store_branch", "linked_at", "note",
                        )}
                    )
                count_rows = connection.execute(
                    "SELECT l.source_id, COUNT(*) AS recent_count FROM recommendation_meal_links l "
                    "JOIN active_entries e ON e.id = l.meal_id "
                    f"WHERE l.source_type = ? AND l.source_id IN ({placeholders}) "
                    "AND e.calendar_date >= ? AND e.calendar_date <= ? GROUP BY l.source_id",
                    (*source_params, cutoff, now.date().isoformat()),
                ).fetchall()
                for row in count_rows:
                    recent_counts[(source_type, row["source_id"])] = row["recent_count"]
                preference_rows = connection.execute(
                    "SELECT source_id, preference, reason, expires_at, updated_at FROM recommendation_preferences "
                    f"WHERE source_type = ? AND source_id IN ({placeholders}) ORDER BY source_id, updated_at DESC",
                    source_params,
                ).fetchall()
                for row in preference_rows:
                    if row["expires_at"] is not None and self._recommendation_datetime_value(row["expires_at"]) <= now:
                        continue
                    source_key = (source_type, row["source_id"])
                    preferences_by_source.setdefault(source_key, {})[row["preference"]] = {
                        field: row[field] for field in ("preference", "reason", "expires_at", "updated_at")
                    }
                skip_rows = connection.execute(
                    "WITH ranked AS ("
                    "SELECT source_id, reason, mode, meal_type, location, skipped_at, "
                    "ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY skipped_at DESC) AS skip_rank "
                    "FROM recommendation_skips "
                    f"WHERE source_type = ? AND source_id IN ({placeholders})"
                    ") SELECT source_id, reason, mode, meal_type, location, skipped_at FROM ranked "
                    "WHERE skip_rank <= 30 ORDER BY source_id, skip_rank",
                    source_params,
                ).fetchall()
                for row in skip_rows:
                    skips_by_source.setdefault((source_type, row["source_id"]), []).append(
                        {field: row[field] for field in ("reason", "mode", "meal_type", "location", "skipped_at")}
                    )

        result: dict[str, dict[str, Any]] = {}
        store_cache: dict[tuple[Any, ...], str | None] = {}
        profile_cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        for candidate in candidates:
            source_type = candidate["source_type"]
            source_id = candidate["id"]
            mode = candidate["mode"]
            key = f"{source_type}:{source_id}"
            source_key = (source_type, source_id)
            meals = meals_by_source.get(source_key, [])
            recent_count = recent_counts.get(source_key, 0)
            preferences = preferences_by_source.get(source_key, {})
            skips = skips_by_source.get(source_key, [])
            similar_skips = []
            for row in skips:
                skipped_at = self._recommendation_datetime_value(row["skipped_at"]).astimezone(now.tzinfo)
                slot_conflict = meal_type is not None and row["meal_type"] is not None and row["meal_type"] != meal_type
                same_context = (
                    (meal_type is not None and row["meal_type"] == meal_type)
                    or (location is not None and row["location"] == location)
                )
                if (
                    cutoff <= skipped_at.date().isoformat() <= now.date().isoformat()
                    and skipped_at <= now
                    and (row["mode"] is None or row["mode"] == (mode_filter or mode))
                    and not slot_conflict
                    and same_context
                ):
                    similar_skips.append(row)
            details = candidate["details"]
            route = self._recommendation_candidate_route(
                connection, candidate, effective_arguments, details, store_cache,
            )
            profile_key = tuple(route[field] for field in (
                "mode", "origin_location", "destination_location", "store_id", "fulfillment", "meal_type",
            ))
            if profile_key not in profile_cache:
                profile_cache[profile_key] = self._recommendation_time_profile(connection, route)
            profile = profile_cache[profile_key]
            reasons = []
            penalty = int(recent_count)
            if recent_count:
                reasons.append(f"Linked to {recent_count} active meal(s) in the last {lookback_days} days.")
            for kind in preferences:
                reasons.append(f"Current user preference: {kind}.")
                if preferences[kind]["reason"]:
                    reasons.append(f"User preference reason: {preferences[kind]['reason']}.")
                penalty += {"liked": -2, "want_to_try": -1, "temporarily_tired": 5}[kind]
            recipe_feedback = self._latest_recipe_feedback_context(connection, source_id, now) if source_type == "recipe" else None
            if recipe_feedback is not None:
                reasons.append(f"Latest recipe feedback: {recipe_feedback['feedback_kind']}.")
                if recipe_feedback["reason"]:
                    reasons.append(f"Recipe feedback reason: {recipe_feedback['reason']}.")
                if recipe_feedback["feedback_kind"] == "liked" and "liked" not in preferences:
                    penalty -= 2
                elif recipe_feedback["feedback_kind"] == "want_to_try" and "want_to_try" not in preferences:
                    penalty -= 1
                elif recipe_feedback["feedback_kind"] == "temporarily_tired" and "temporarily_tired" not in preferences:
                    penalty += 5
            for skip in similar_skips:
                reasons.append(f"Previously skipped in a similar meal context: {skip['reason']}.")
                penalty += 1
            if profile is not None and profile["sample_count"]:
                reasons.append(
                    f"Observed route timing uses {profile['sample_count']} active meal sample(s) for this exact route."
                )
            result[key] = {
                "reasons": reasons,
                "rank_penalty": penalty,
                "history": {
                    "meals": [dict(row) for row in meals],
                    "recent_meal_count": recent_count,
                    "preference": preferences,
                    "preferences": list(preferences.values()),
                    "recipe_feedback": recipe_feedback,
                    "skips": similar_skips,
                },
                "time_profile": profile,
            }
        return result

    def _recommendation_now(self, value: Any) -> datetime:
        return self._parse_now(value)

    def _recommendation_effective_preferences(
        self, connection: sqlite3.Connection, source_type: str, source_id: str, now: datetime
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            "SELECT preference, reason, expires_at, updated_at FROM recommendation_preferences "
            "WHERE source_type = ? AND source_id = ? ORDER BY updated_at DESC",
            (source_type, source_id),
        ).fetchall()
        preferences = {}
        for row in rows:
            preference = dict(row)
            if preference["expires_at"] is not None and self._recommendation_datetime_value(preference["expires_at"]) <= now:
                continue
            preferences[preference["preference"]] = preference
        return preferences

    def _latest_recipe_feedback_context(
        self, connection: sqlite3.Connection, recipe_id: str, now: datetime
    ) -> dict[str, Any] | None:
        feedback = self._latest_recipe_feedback(connection, recipe_id, now)
        if feedback is None:
            return None
        return feedback

    def _recommendation_candidate_route(
        self,
        connection: sqlite3.Connection,
        candidate: dict[str, Any],
        arguments: dict[str, Any],
        details: dict[str, Any],
        store_cache: dict[tuple[Any, ...], str | None] | None = None,
    ) -> dict[str, Any]:
        store_id = None
        if candidate["source_type"] == "candidate":
            input_store_id = details.get("store_id")
            store = details.get("store")
            if isinstance(input_store_id, str):
                store_key = ("id", input_store_id)
            elif isinstance(store, dict) and isinstance(store.get("name"), str) and (
                store.get("branch") is None or isinstance(store.get("branch"), str)
            ):
                store_key = ("name", store["name"], store.get("branch"))
            else:
                store_key = None
            if store_cache is not None and store_key is not None:
                if store_key not in store_cache:
                    store_cache[store_key] = self._recommendation_store_from_details(connection, details)
                store_id = store_cache[store_key]
            else:
                store_id = self._recommendation_store_from_details(connection, details)
        destination = details.get("destination_location")
        if destination is None:
            destination = details.get("location")
        if not isinstance(destination, str) or not destination:
            destination = None
        fulfillment = details.get("fulfillment")
        if not isinstance(fulfillment, str) or fulfillment not in {"delivery", "pickup"}:
            fulfillment = None
        return {
            "mode": candidate["mode"],
            "origin_location": arguments.get("location"),
            "destination_location": destination,
            "store_id": store_id,
            "fulfillment": fulfillment if candidate["mode"] == "delivery" else None,
            "meal_type": arguments.get("meal_type"),
        }

    @staticmethod
    def _recommendation_profile_dimensions(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "mode": payload["mode"],
            "origin_location": payload.get("origin_location"),
            "destination_location": payload.get("destination_location"),
            "store_id": payload.get("store_id"),
            "fulfillment": payload.get("fulfillment"),
            "meal_type": payload.get("meal_type"),
        }

    def _recommendation_time_profile(
        self, connection: sqlite3.Connection, dimensions: dict[str, Any]
    ) -> dict[str, Any] | None:
        store_id = dimensions["store_id"]
        if store_id is not None:
            store_id = self._recommendation_canonical_store(connection, store_id)
        row = connection.execute(
            "SELECT stages_json, updated_at FROM recommendation_time_profiles "
            "WHERE mode = ? AND origin_location IS ? AND destination_location IS ? "
            "AND store_id IS ? AND fulfillment IS ? AND meal_type IS ? ORDER BY updated_at DESC LIMIT 1",
            (
                dimensions["mode"],
                dimensions["origin_location"],
                dimensions["destination_location"],
                store_id,
                dimensions.get("fulfillment"),
                dimensions["meal_type"],
            ),
        ).fetchone()
        manual_stages = json.loads(row["stages_json"]) if row else {}
        observed = self._recommendation_observed_stages(connection, dimensions, store_id)
        stages: dict[str, dict[str, Any]] = {}
        for name, value in manual_stages.items():
            stages[name] = {**value, "source": "user_estimate", "sample_count": 0}
        for name, value in observed["stages"].items():
            stages[name] = {**value, "source": "observed"}
        if not stages and not row:
            return None
        return {
            **dimensions,
            "store_id": store_id,
            "source": "mixed" if observed["sample_count"] and manual_stages else (
                "observed" if observed["sample_count"] else "user_estimate"
            ),
            "sample_count": observed["sample_count"],
            "stages": stages,
            "observed_stages": observed["stages"],
            "updated_at": row["updated_at"] if row else None,
        }

    def _recommendation_observed_stages(
        self,
        connection: sqlite3.Connection,
        dimensions: dict[str, Any],
        store_id: str | None,
    ) -> dict[str, Any]:
        if (
            dimensions["origin_location"] is None
            and dimensions["destination_location"] is None
            and store_id is None
            and dimensions["mode"] != "home_cooking"
        ):
            return {"sample_count": 0, "stages": {}}
        clauses = [
            "e.mode = ?",
            "ev.origin_location IS ?",
            "ev.destination_location IS ?",
            "ev.store_id IS ?",
            "ev.fulfillment IS ?",
            "ev.actual_at IS NOT NULL",
            "ev.status = 'completed'",
        ]
        values: list[Any] = [
            dimensions["mode"],
            dimensions["origin_location"],
            dimensions["destination_location"],
            store_id,
            dimensions.get("fulfillment"),
        ]
        if dimensions["meal_type"] is not None:
            clauses.append("e.meal_type = ?")
            values.append(dimensions["meal_type"])
        else:
            clauses.append("e.meal_type IS NULL")
        rows = connection.execute(
            "SELECT e.id AS meal_id, ev.event_kind, ev.actual_at "
            "FROM active_entries e JOIN meal_timing_events ev ON ev.meal_id = e.id "
            f"WHERE {' AND '.join(clauses)} ORDER BY e.id",
            values,
        ).fetchall()
        by_meal: dict[str, dict[str, str]] = {}
        for row in rows:
            by_meal.setdefault(row["meal_id"], {})[row["event_kind"]] = row["actual_at"]
        durations: dict[str, list[int]] = {}
        sampled_meals = set()
        for meal_id, events in by_meal.items():
            if dimensions["mode"] == "delivery":
                end_kind = "ready" if dimensions.get("fulfillment") == "pickup" else "received"
                pairs = {"order_to_ready": ("ordered", end_kind), **_EVENT_STAGES["delivery"]}
            else:
                pairs = _EVENT_STAGES[dimensions["mode"]]
            for stage, (start_kind, end_kind) in pairs.items():
                if start_kind not in events or end_kind not in events:
                    continue
                start = self._recommendation_datetime_value(events[start_kind])
                end = self._recommendation_datetime_value(events[end_kind])
                if end >= start:
                    durations.setdefault(stage, []).append(round((end - start).total_seconds() / 60))
                    sampled_meals.add(meal_id)
        stage_ranges = {
            stage: {
                "min_minutes": min(values),
                "max_minutes": max(values),
                "sample_count": len(values),
            }
            for stage, values in durations.items()
        }
        return {"sample_count": len(sampled_meals), "stages": stage_ranges}

    def _recommendation_defaults_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT value_json, updated_at FROM preferences WHERE preference_key = 'recommendation_defaults'"
            ).fetchone()
        return {
            "values": json.loads(row["value_json"]) if row else {},
            "updated_at": row["updated_at"] if row else None,
        }

    def _recommendation_defaults_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"request_id", "values"})
        values = self._dict(payload.get("values"), "values")
        self._recommendation_reject_fields(values, self._DEFAULT_FIELDS)
        validated: dict[str, Any] = {}
        for field, value in values.items():
            if field == "mode":
                validated[field] = self._recommendation_enum(value, field, {"eat_out", "delivery", "home_cooking"}, nullable=True)
            elif field in {"location", "return_location"}:
                validated[field] = self._string(value, field, nullable=True)
            elif field == "people_count":
                validated[field] = self._integer(value, field, nullable=True, minimum=1)
            elif field == "budget_cents":
                validated[field] = self._integer(value, field, nullable=True, minimum=0)
            elif field == "budget_basis":
                validated[field] = self._recommendation_enum(value, field, {"payment", "consumption"}, nullable=True)
            elif field == "budget_scope":
                validated[field] = self._recommendation_enum(value, field, {"total", "personal_share"}, nullable=True)
            elif field == "time_target":
                validated[field] = self._recommendation_enum(value, field, {"meal_start", "finished", "returned"}, nullable=True)
            elif field == "meal_type":
                validated[field] = self._recommendation_enum(value, field, {"breakfast", "lunch", "dinner", "snack", "other"}, nullable=True)
            elif field == "for_meal_type":
                validated[field] = self._recommendation_enum(value, field, {"breakfast", "lunch", "dinner", "snack", "other"}, nullable=True)
            elif field == "weekday_only":
                if value is not None and not isinstance(value, bool):
                    raise BusinessError("INVALID_INPUT", "weekday_only must be a boolean or null", {"field": field})
                validated[field] = value
            elif field == "score_weights":
                validated[field] = score_weights(value) if value is not None else None
            else:
                validated[field] = self._integer(value, field, nullable=True, minimum=1)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            current = connection.execute(
                "SELECT value_json FROM preferences WHERE preference_key = 'recommendation_defaults'"
            ).fetchone()
            merged = json.loads(current["value_json"]) if current else {}
            for field, value in validated.items():
                if value is None:
                    merged.pop(field, None)
                else:
                    merged[field] = value
            updated_at = self._now()
            if merged:
                connection.execute(
                    "INSERT INTO preferences(preference_key, value_json, updated_at) VALUES ('recommendation_defaults', ?, ?) "
                    "ON CONFLICT(preference_key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                    (self._json(merged), updated_at),
                )
            else:
                connection.execute("DELETE FROM preferences WHERE preference_key = 'recommendation_defaults'")
            return {"values": merged, "updated_at": updated_at}

        return self._write("recommendation_defaults_set", payload, operation)

    def _recommendation_history_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"source_type", "source_id", "record_type", "limit", "offset"})
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"}, nullable=True)
        source_id = self._string(payload.get("source_id"), "source_id", nullable=True)
        record_type = payload.get("record_type", "all")
        self._recommendation_enum(record_type, "record_type", {"all", "meal", "skip"})
        limit, offset = self._recommendation_page(payload)
        clauses = []
        values: list[Any] = []
        if source_type is not None:
            clauses.append("source_type = ?")
            values.append(source_type)
        if source_id is not None:
            clauses.append("source_id = ?")
            values.append(source_id)
        filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
        sources = []
        if record_type in {"all", "meal"}:
            sources.append(
                "SELECT 'meal' AS record_type, l.linked_at AS occurred_at, l.source_type, l.source_id, "
                "l.meal_id, l.note AS reason, e.mode, e.meal_type, e.location, "
                "COALESCE(msi.store_id, e.store_id) AS store_id, s.name AS store_name, s.branch AS store_branch "
                "FROM recommendation_meal_links l JOIN active_entries e ON e.id = l.meal_id "
                "LEFT JOIN meal_store_identity msi ON msi.entry_id = e.id "
                "LEFT JOIN stores s ON s.store_id = msi.store_id "
                "WHERE 1 = 1" + filter_sql
            )
        if record_type in {"all", "skip"}:
            sources.append(
                "SELECT 'skip' AS record_type, s.skipped_at AS occurred_at, s.source_type, s.source_id, "
                "NULL AS meal_id, s.reason, s.mode, s.meal_type, s.location, "
                "NULL AS store_id, NULL AS store_name, NULL AS store_branch "
                "FROM recommendation_skips s WHERE 1 = 1" + filter_sql.replace("source_type", "s.source_type").replace("source_id", "s.source_id")
            )
        query = (
            "SELECT * FROM (" + " UNION ALL ".join(sources) + ") "
            "ORDER BY occurred_at DESC, source_type, source_id LIMIT ? OFFSET ?"
        )
        with self.store.read() as connection:
            rows = connection.execute(query, [*values, *values, limit, offset] if len(sources) == 2 else [*values, limit, offset]).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                if item["record_type"] == "meal":
                    item["items"] = [
                        dish["name"]
                        for dish in connection.execute(
                            "SELECT name FROM items WHERE entry_id = ? ORDER BY rowid",
                            (item["meal_id"],),
                        ).fetchall()
                    ]
                items.append(item)
        return {"items": items, "limit": limit, "offset": offset}

    def _recommendation_meal_link_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"request_id", "meal_id", "source_type", "source_id", "note"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"})
        source_id = self._string(payload.get("source_id"), "source_id")
        note = self._string(payload.get("note"), "note", nullable=True)

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            meal = connection.execute(
                "SELECT mode FROM active_entries WHERE id = ? AND record_kind = 'meal'",
                (meal_id,),
            ).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "active meal was not found", {"meal_id": meal_id})
            return self._link_meal_recommendation(connection, meal_id, source_type, source_id, meal["mode"], note)

        return self._write("recommendation_meal_link_set", payload, operation)

    def _link_meal_recommendation(self, connection, meal_id, source_type, source_id, mode, note):
        self._validate_recommendation_meal_source(connection, source_type, source_id, mode, meal_id=meal_id)
        linked_at = self._now()
        connection.execute(
            "INSERT INTO recommendation_meal_links(meal_id, source_type, source_id, note, linked_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(meal_id) DO UPDATE SET "
            "source_type = excluded.source_type, source_id = excluded.source_id, "
            "note = excluded.note, linked_at = excluded.linked_at",
            (meal_id, source_type, source_id, note, linked_at),
        )
        return {"meal_id": meal_id, "source_type": source_type, "source_id": source_id, "note": note, "linked_at": linked_at}

    def _validate_recommendation_meal_source(self, connection, source_type, source_id, mode, *, meal_id=None):
        self._recommendation_source(connection, source_type, source_id)
        source_mode = "home_cooking"
        if source_type == "candidate":
            source_mode = connection.execute(
                "SELECT mode FROM candidates WHERE candidate_id = ?", (source_id,),
            ).fetchone()["mode"]
        if mode != source_mode:
            details = {"field": "recommendation_source"}
            if meal_id is not None:
                details["meal_id"] = meal_id
            raise BusinessError("INVALID_INPUT", "linked source mode must match the meal mode", details)

    def _recommendation_preference_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {"request_id", "source_type", "source_id", "preference", "enabled", "reason", "expires_at"},
        )
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"})
        source_id = self._string(payload.get("source_id"), "source_id")
        preference = self._recommendation_enum(
            payload.get("preference"),
            "preference",
            set(_PREFERENCE["enum"]),
        )
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BusinessError("INVALID_INPUT", "enabled must be a boolean", {"field": "enabled"})
        reason = self._string(payload.get("reason"), "reason", nullable=True)
        expires_at = self._recommendation_datetime(payload.get("expires_at"), "expires_at", nullable=True)
        if enabled and preference == "temporarily_tired":
            if expires_at is None:
                raise BusinessError("INVALID_INPUT", "temporarily_tired requires expires_at", {"field": "expires_at"})
            if self._recommendation_datetime_value(expires_at) <= self._recommendation_datetime_value(self._now()):
                raise BusinessError("INVALID_INPUT", "expires_at must be in the future", {"field": "expires_at"})
        elif expires_at is not None:
            raise BusinessError("INVALID_INPUT", "expires_at is only valid when enabling temporarily_tired", {"field": "expires_at"})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._recommendation_source(connection, source_type, source_id)
            if not enabled:
                connection.execute(
                    "DELETE FROM recommendation_preferences WHERE source_type = ? AND source_id = ? AND preference = ?",
                    (source_type, source_id, preference),
                )
                return {"source_type": source_type, "source_id": source_id, "preference": preference, "active": False}
            updated_at = self._now()
            connection.execute(
                "INSERT INTO recommendation_preferences(source_type, source_id, preference, reason, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_type, source_id, preference) DO UPDATE SET "
                "reason = excluded.reason, expires_at = excluded.expires_at, "
                "updated_at = excluded.updated_at",
                (source_type, source_id, preference, reason, expires_at, updated_at),
            )
            return {
                "source_type": source_type,
                "source_id": source_id,
                "preference": preference,
                "active": True,
                "reason": reason,
                "expires_at": expires_at,
                "updated_at": updated_at,
            }

        return self._write("recommendation_preference_set", payload, operation)

    def _recommendation_preference_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"source_type", "source_id", "preference", "limit", "offset"})
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"}, nullable=True)
        source_id = self._string(payload.get("source_id"), "source_id", nullable=True)
        preference = self._recommendation_enum(payload.get("preference"), "preference", set(_PREFERENCE["enum"]), nullable=True)
        limit, offset = self._recommendation_page(payload)
        clauses, values = [], []
        if source_type is not None:
            clauses.append("p.source_type = ?")
            values.append(source_type)
        if source_id is not None:
            clauses.append("p.source_id = ?")
            values.append(source_id)
        if preference is not None:
            clauses.append("p.preference = ?")
            values.append(preference)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT p.source_type, p.source_id, p.preference, p.reason, p.expires_at, p.updated_at, "
                "COALESCE(c.name, r.name) AS source_name, "
                "CASE WHEN p.source_type = 'recipe' THEN 'home_cooking' ELSE c.mode END AS mode "
                "FROM recommendation_preferences p "
                "LEFT JOIN candidates c ON p.source_type = 'candidate' AND c.candidate_id = p.source_id "
                "LEFT JOIN recipes r ON p.source_type = 'recipe' AND r.recipe_id = p.source_id" + where
                + " ORDER BY p.updated_at DESC, p.source_type, p.source_id, p.preference LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        now = datetime.now().astimezone()
        items = []
        for row in rows:
            item = dict(row)
            item["active"] = item["expires_at"] is None or self._recommendation_datetime_value(item["expires_at"]) > now
            items.append(item)
        return {"items": items, "limit": limit, "offset": offset}

    def _recommendation_skip_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {"request_id", "source_type", "source_id", "reason", "mode", "meal_type", "location", "skipped_at"},
        )
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"})
        source_id = self._string(payload.get("source_id"), "source_id")
        reason = self._string(payload.get("reason"), "reason")
        mode = self._recommendation_enum(payload.get("mode"), "mode", {"eat_out", "delivery", "home_cooking"}, nullable=True)
        meal_type = self._recommendation_enum(payload.get("meal_type"), "meal_type", {"breakfast", "lunch", "dinner", "snack", "other"}, nullable=True)
        location = self._string(payload.get("location"), "location", nullable=True)
        skipped_at = self._recommendation_datetime(payload.get("skipped_at"), "skipped_at", nullable=True) or self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            self._recommendation_source(connection, source_type, source_id)
            skip_id = uuid.uuid4().hex
            recorded_at = self._now()
            connection.execute(
                "INSERT INTO recommendation_skips(skip_id, source_type, source_id, reason, mode, meal_type, location, skipped_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (skip_id, source_type, source_id, reason, mode, meal_type, location, skipped_at, recorded_at),
            )
            return {
                "skip_id": skip_id,
                "source_type": source_type,
                "source_id": source_id,
                "reason": reason,
                "mode": mode,
                "meal_type": meal_type,
                "location": location,
                "skipped_at": skipped_at,
                "recorded_at": recorded_at,
            }

        return self._write("recommendation_skip_record", payload, operation)

    def _recommendation_skip_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"source_type", "source_id", "limit", "offset"})
        source_type = self._recommendation_enum(payload.get("source_type"), "source_type", {"candidate", "recipe"}, nullable=True)
        source_id = self._string(payload.get("source_id"), "source_id", nullable=True)
        limit, offset = self._recommendation_page(payload)
        clauses, values = [], []
        if source_type is not None:
            clauses.append("source_type = ?")
            values.append(source_type)
        if source_id is not None:
            clauses.append("source_id = ?")
            values.append(source_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT skip_id, source_type, source_id, reason, mode, meal_type, location, skipped_at, recorded_at "
                "FROM recommendation_skips" + where
                + " ORDER BY skipped_at DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def _recommendation_time_profile_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {
                "request_id", "mode", "origin_location", "destination_location",
                "store_id", "fulfillment", "meal_type", "stages",
            },
        )
        mode = self._recommendation_enum(payload.get("mode"), "mode", {"eat_out", "delivery", "home_cooking"})
        origin = self._string(payload.get("origin_location"), "origin_location", nullable=True)
        destination = self._string(payload.get("destination_location"), "destination_location", nullable=True)
        store_id = self._string(payload.get("store_id"), "store_id", nullable=True)
        fulfillment = self._recommendation_enum(
            payload.get("fulfillment"),
            "fulfillment",
            {"delivery", "pickup"},
            nullable=True,
        )
        if mode != "delivery" and fulfillment is not None:
            raise BusinessError("INVALID_INPUT", "fulfillment is only valid for delivery mode", {"field": "fulfillment"})
        meal_type = self._recommendation_enum(
            payload.get("meal_type"),
            "meal_type",
            {"breakfast", "lunch", "dinner", "snack", "other"},
            nullable=True,
        )
        raw_stages = self._dict(payload.get("stages"), "stages")
        if not raw_stages:
            raise BusinessError("INVALID_INPUT", "stages must contain at least one duration range", {"field": "stages"})
        unsupported = next((name for name in raw_stages if name not in _PROFILE_STAGES[mode]), None)
        if unsupported is not None:
            raise BusinessError("INVALID_INPUT", "stage is not valid for this mode", {"field": f"stages.{unsupported}"})
        stages = {}
        for name, value in raw_stages.items():
            row = self._dict(value, f"stages.{name}")
            self._recommendation_reject_fields(row, {"min_minutes", "max_minutes"})
            minimum = self._integer(row.get("min_minutes"), f"stages.{name}.min_minutes", minimum=0)
            maximum = self._integer(row.get("max_minutes"), f"stages.{name}.max_minutes", minimum=0)
            if minimum > maximum:
                raise BusinessError(
                    "INVALID_INPUT",
                    "min_minutes cannot exceed max_minutes",
                    {"field": f"stages.{name}"},
                )
            stages[name] = {"min_minutes": minimum, "max_minutes": maximum}

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            canonical_store_id = self._recommendation_canonical_store(connection, store_id)
            row = connection.execute(
                "SELECT profile_id FROM recommendation_time_profiles WHERE mode = ? "
                "AND origin_location IS ? AND destination_location IS ? AND store_id IS ? "
                "AND fulfillment IS ? AND meal_type IS ?",
                (mode, origin, destination, canonical_store_id, fulfillment, meal_type),
            ).fetchone()
            updated_at = self._now()
            profile_id = row["profile_id"] if row else uuid.uuid4().hex
            if row:
                connection.execute(
                    "UPDATE recommendation_time_profiles SET stages_json = ?, updated_at = ? WHERE profile_id = ?",
                    (self._json(stages), updated_at, profile_id),
                )
            else:
                connection.execute(
                    "INSERT INTO recommendation_time_profiles("
                    "profile_id, mode, origin_location, destination_location, store_id, fulfillment, meal_type, stages_json, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        profile_id, mode, origin, destination, canonical_store_id, fulfillment,
                        meal_type, self._json(stages), updated_at,
                    ),
                )
            return {
                "profile_id": profile_id,
                "mode": mode,
                "origin_location": origin,
                "destination_location": destination,
                "store_id": canonical_store_id,
                "fulfillment": fulfillment,
                "meal_type": meal_type,
                "stages": stages,
                "source": "user_estimate",
                "sample_count": 0,
                "updated_at": updated_at,
            }

        return self._write("recommendation_time_profile_set", payload, operation)

    def _recommendation_time_profile_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {"mode", "origin_location", "destination_location", "store_id", "fulfillment", "meal_type", "limit", "offset"},
        )
        mode = self._recommendation_enum(payload.get("mode"), "mode", {"eat_out", "delivery", "home_cooking"})
        limit, offset = self._recommendation_page(payload)
        clauses, values = ["mode = ?"], [mode]
        for field in ("origin_location", "destination_location", "fulfillment", "meal_type"):
            if field in payload:
                if field == "fulfillment":
                    value = self._recommendation_enum(payload[field], field, {"delivery", "pickup"}, nullable=True)
                elif field == "meal_type":
                    value = self._recommendation_enum(
                        payload[field],
                        field,
                        {"breakfast", "lunch", "dinner", "snack", "other"},
                        nullable=True,
                    )
                else:
                    value = self._string(payload[field], field, nullable=True)
                clauses.append(f"{field} IS ?")
                values.append(value)
        store_id = None
        if "store_id" in payload:
            store_id = self._string(payload["store_id"], "store_id", nullable=True)
        with self.store.read() as connection:
            if "store_id" in payload:
                store_id = self._recommendation_canonical_store(connection, store_id)
                clauses.append("store_id IS ?")
                values.append(store_id)
            rows = connection.execute(
                "SELECT profile_id, mode, origin_location, destination_location, store_id, fulfillment, meal_type, "
                "stages_json, updated_at FROM recommendation_time_profiles WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            result = self._recommendation_profile_list_result(connection, rows, limit, offset)
            exact_fields = ["origin_location", "destination_location", "store_id", "meal_type"]
            if mode == "delivery":
                exact_fields.append("fulfillment")
            exact_route = all(field in payload for field in exact_fields) and offset == 0
            if exact_route:
                dimensions = {
                    "mode": mode,
                    "origin_location": self._string(payload["origin_location"], "origin_location", nullable=True),
                    "destination_location": self._string(payload["destination_location"], "destination_location", nullable=True),
                    "store_id": store_id,
                    "fulfillment": self._recommendation_enum(
                        payload["fulfillment"], "fulfillment", {"delivery", "pickup"}, nullable=True
                    ) if mode == "delivery" and "fulfillment" in payload else None,
                    "meal_type": self._recommendation_enum(
                        payload["meal_type"],
                        "meal_type",
                        {"breakfast", "lunch", "dinner", "snack", "other"},
                        nullable=True,
                    ),
                }
                observed = self._recommendation_time_profile(connection, dimensions)
                if observed is not None and not any(
                    item["origin_location"] == dimensions["origin_location"]
                    and item["destination_location"] == dimensions["destination_location"]
                    and item["store_id"] == dimensions["store_id"]
                    and item["fulfillment"] == dimensions["fulfillment"]
                    and item["meal_type"] == dimensions["meal_type"]
                    for item in result["items"]
                ):
                    result["items"].append({"profile_id": None, **observed})
            return result

    def _recommendation_profile_list_result(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        items = []
        for row in rows:
            dimensions = {
                "mode": row["mode"],
                "origin_location": row["origin_location"],
                "destination_location": row["destination_location"],
                "store_id": row["store_id"],
                "fulfillment": row["fulfillment"],
                "meal_type": row["meal_type"],
            }
            profile = self._recommendation_time_profile(connection, dimensions)
            items.append(
                {
                    "profile_id": row["profile_id"],
                    **profile,
                    "stages": profile["stages"],
                }
            )
        return {"items": items, "limit": limit, "offset": offset}

    def _recommendation_profile_for_dimensions(
        self, connection: sqlite3.Connection, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        store_id = self._string(payload.get("store_id"), "store_id", nullable=True)
        canonical_store_id = self._recommendation_canonical_store(connection, store_id)
        dimensions = {
            "mode": self._recommendation_enum(payload.get("mode"), "mode", {"eat_out", "delivery", "home_cooking"}),
            "origin_location": self._string(payload.get("origin_location"), "origin_location", nullable=True),
            "destination_location": self._string(payload.get("destination_location"), "destination_location", nullable=True),
            "store_id": canonical_store_id,
            "fulfillment": self._recommendation_enum(payload.get("fulfillment"), "fulfillment", {"delivery", "pickup"}, nullable=True),
            "meal_type": self._recommendation_enum(
                payload.get("meal_type"),
                "meal_type",
                {"breakfast", "lunch", "dinner", "snack", "other"},
                nullable=True,
            ),
        }
        if dimensions["mode"] != "delivery" and dimensions["fulfillment"] is not None:
            raise BusinessError("INVALID_INPUT", "fulfillment is only valid for delivery mode", {"field": "fulfillment"})
        return self._recommendation_time_profile(connection, dimensions)

    def _recommendation_countdown(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {
                "mode", "target_at", "target", "origin_location", "destination_location",
                "store_id", "fulfillment", "meal_type", "shopping_required",
            },
        )
        mode = self._recommendation_enum(payload.get("mode"), "mode", {"eat_out", "delivery", "home_cooking"})
        target_at = self._recommendation_datetime(payload.get("target_at"), "target_at")
        target = self._recommendation_enum(
            payload.get("target", "meal_start"),
            "target",
            {"meal_start", "finished", "returned"},
        )
        shopping_required = payload.get("shopping_required")
        if shopping_required is not None and not isinstance(shopping_required, bool):
            raise BusinessError("INVALID_INPUT", "shopping_required must be a boolean or null", {"field": "shopping_required"})
        dimensions_payload = {
            "mode": mode,
            "origin_location": payload.get("origin_location"),
            "destination_location": payload.get("destination_location"),
            "store_id": payload.get("store_id"),
            "fulfillment": payload.get("fulfillment"),
            "meal_type": payload.get("meal_type"),
        }
        with self.store.read() as connection:
            profile = self._recommendation_profile_for_dimensions(connection, dimensions_payload)
        stages = profile["stages"] if profile else {}
        if mode == "eat_out":
            required = ["outbound", "queue", "wait"]
        elif mode == "delivery":
            required = ["order_to_ready"]
        else:
            required = ([] if shopping_required is False else ["shopping"]) + ["prep", "cook"]
        if target in {"finished", "returned"}:
            required.append("meal")
        if target == "returned":
            required.append("return")
        missing = []
        total_min = 0
        total_max = 0
        stage_values = {}
        for stage in required:
            value = stages.get(stage)
            if value is None:
                missing.append(stage)
                continue
            minimum = value["min_minutes"]
            maximum = value["max_minutes"]
            total_min += minimum
            total_max += maximum
            stage_values[stage] = value
        window = None
        if not missing:
            target_value = self._recommendation_datetime_value(target_at)
            window = {
                "earliest": (target_value - timedelta(minutes=total_max)).isoformat(timespec="minutes"),
                "latest": (target_value - timedelta(minutes=total_min)).isoformat(timespec="minutes"),
            }
        return {
            "status": "known" if not missing else "needs_verification",
            "mode": mode,
            "target": target,
            "target_at": target_at,
            "start_window": window,
            "safe_latest_start": window["earliest"] if window else None,
            "fastest_start": window["latest"] if window else None,
            "duration_minutes": {"min": total_min, "max": total_max} if not missing else None,
            "stages": stage_values,
            "missing": missing,
            "time_profile": profile,
        }

    def _meal_event_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(
            payload,
            {
                "request_id", "meal_id", "event_kind", "status", "actual_at",
                "expected_earliest_at", "expected_latest_at", "time_source", "expected_source",
                "fulfillment", "note", "origin_location", "destination_location", "store_id",
            },
        )
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        event_kind = self._recommendation_enum(payload.get("event_kind"), "event_kind", set(_EVENT_KINDS))
        actual_input = self._recommendation_datetime(payload.get("actual_at"), "actual_at", nullable=True)
        earliest_input = self._recommendation_datetime(payload.get("expected_earliest_at"), "expected_earliest_at", nullable=True)
        latest_input = self._recommendation_datetime(payload.get("expected_latest_at"), "expected_latest_at", nullable=True)
        status_input = (
            self._recommendation_enum(payload["status"], "status", {"planned", "in_progress", "completed", "skipped"})
            if "status" in payload
            else None
        )
        time_source_input = (
            self._recommendation_enum(payload["time_source"], "time_source", {"manual", "estimate"})
            if "time_source" in payload
            else None
        )
        expected_source_input = (
            self._recommendation_enum(payload["expected_source"], "expected_source", {"manual", "estimate"}, nullable=True)
            if "expected_source" in payload
            else None
        )
        fulfillment_input = (
            self._recommendation_enum(payload["fulfillment"], "fulfillment", {"delivery", "pickup"}, nullable=True)
            if "fulfillment" in payload
            else None
        )
        note_input = self._string(payload.get("note"), "note", nullable=True) if "note" in payload else None

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            meal = connection.execute(
                "SELECT e.id, e.mode, e.meal_type, COALESCE(msi.store_id, e.store_id) AS store_id "
                "FROM active_entries e LEFT JOIN meal_store_identity msi ON msi.entry_id = e.id "
                "WHERE e.id = ? AND e.record_kind = 'meal'",
                (meal_id,),
            ).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "active meal was not found", {"meal_id": meal_id})
            mode = meal["mode"]
            if event_kind not in _EVENT_ORDER[mode]:
                raise BusinessError("INVALID_INPUT", "event_kind is not valid for this meal mode", {"field": "event_kind"})
            prior_route = connection.execute(
                "SELECT origin_location, destination_location, store_id, fulfillment FROM meal_timing_events "
                "WHERE meal_id = ? ORDER BY updated_at DESC LIMIT 1",
                (meal_id,),
            ).fetchone()
            old_event = connection.execute(
                "SELECT * FROM meal_timing_events WHERE meal_id = ? AND event_kind = ?",
                (meal_id, event_kind),
            ).fetchone()
            actual_at = actual_input if "actual_at" in payload else (old_event["actual_at"] if old_event else None)
            earliest = earliest_input if "expected_earliest_at" in payload else (old_event["expected_earliest_at"] if old_event else None)
            latest = latest_input if "expected_latest_at" in payload else (old_event["expected_latest_at"] if old_event else None)
            if (earliest is None) != (latest is None):
                raise BusinessError("INVALID_INPUT", "expected earliest and latest must both be provided", {"field": "expected_earliest_at"})
            if earliest is not None and self._recommendation_datetime_value(earliest) > self._recommendation_datetime_value(latest):
                raise BusinessError("INVALID_INPUT", "expected_earliest_at cannot exceed expected_latest_at", {"field": "expected_earliest_at"})
            if status_input is not None:
                status = status_input
            elif actual_at is not None:
                status = "completed"
            elif earliest is not None:
                status = "planned"
            else:
                status = old_event["status"] if old_event else "in_progress"
            if status == "completed" and actual_at is None:
                raise BusinessError("INVALID_INPUT", "completed events require actual_at", {"field": "actual_at"})
            if actual_at is not None and status in {"planned", "skipped"}:
                raise BusinessError("INVALID_INPUT", "actual_at requires completed or in_progress status", {"field": "status"})
            if "actual_at" in payload and actual_at is not None:
                time_source = time_source_input or "manual"
            elif actual_at is not None and old_event:
                time_source = time_source_input or old_event["time_source"]
            elif earliest is not None:
                time_source = time_source_input or expected_source_input or (
                    old_event["expected_source"] if old_event else None
                ) or "estimate"
            else:
                time_source = time_source_input or (old_event["time_source"] if old_event else "estimate")
            if actual_at is not None and time_source != "manual":
                raise BusinessError("INVALID_INPUT", "actual_at must have manual time_source", {"field": "time_source"})
            if "expected_source" in payload:
                expected_source = expected_source_input
            elif earliest is not None and ("expected_earliest_at" in payload or "expected_latest_at" in payload):
                expected_source = time_source_input or "estimate"
            elif earliest is not None and old_event:
                expected_source = old_event["expected_source"] or old_event["time_source"]
            else:
                expected_source = None
            if earliest is not None and expected_source is None:
                raise BusinessError("INVALID_INPUT", "expected times require expected_source", {"field": "expected_source"})
            if earliest is None and expected_source is not None:
                raise BusinessError("INVALID_INPUT", "expected_source requires expected times", {"field": "expected_source"})
            note = note_input if "note" in payload else (old_event["note"] if old_event else None)
            origin = (
                self._string(payload.get("origin_location"), "origin_location", nullable=True)
                if "origin_location" in payload
                else (prior_route["origin_location"] if prior_route else None)
            )
            destination = (
                self._string(payload.get("destination_location"), "destination_location", nullable=True)
                if "destination_location" in payload
                else (prior_route["destination_location"] if prior_route else None)
            )
            if "store_id" in payload:
                input_store_id = self._string(payload.get("store_id"), "store_id", nullable=True)
            else:
                input_store_id = prior_route["store_id"] if prior_route else meal["store_id"]
            canonical_store_id = self._recommendation_canonical_store(connection, input_store_id)
            fulfillment = fulfillment_input if "fulfillment" in payload else (prior_route["fulfillment"] if prior_route else None)
            if mode == "delivery" and fulfillment is None:
                raise BusinessError("INVALID_INPUT", "delivery events require fulfillment=delivery or pickup", {"field": "fulfillment"})
            if mode != "delivery" and fulfillment is not None:
                raise BusinessError("INVALID_INPUT", "fulfillment is only valid for delivery mode", {"field": "fulfillment"})
            if prior_route:
                connection.execute(
                    "UPDATE meal_timing_events SET origin_location = ?, destination_location = ?, store_id = ?, fulfillment = ? WHERE meal_id = ?",
                    (origin, destination, canonical_store_id, fulfillment, meal_id),
                )
            event_id = old_event["event_id"] if old_event else uuid.uuid4().hex
            updated_at = self._now()
            connection.execute(
                "INSERT INTO meal_timing_events("
                "event_id, meal_id, event_kind, status, actual_at, expected_earliest_at, "
                "expected_latest_at, time_source, expected_source, fulfillment, note, "
                "origin_location, destination_location, store_id, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(meal_id, event_kind) DO UPDATE SET "
                "status = excluded.status, actual_at = excluded.actual_at, "
                "expected_earliest_at = excluded.expected_earliest_at, "
                "expected_latest_at = excluded.expected_latest_at, time_source = excluded.time_source, "
                "expected_source = excluded.expected_source, fulfillment = excluded.fulfillment, "
                "note = excluded.note, origin_location = excluded.origin_location, "
                "destination_location = excluded.destination_location, store_id = excluded.store_id, "
                "updated_at = excluded.updated_at",
                (
                    event_id, meal_id, event_kind, status, actual_at, earliest, latest,
                    time_source, expected_source, fulfillment, note, origin, destination,
                    canonical_store_id, updated_at,
                ),
            )
            self._recommendation_validate_event_order(connection, meal_id, mode)
            return {
                "event_id": event_id,
                "meal_id": meal_id,
                "event_kind": event_kind,
                "status": status,
                "actual_at": actual_at,
                "expected_earliest_at": earliest,
                "expected_latest_at": latest,
                "time_source": time_source,
                "actual_source": "manual" if actual_at is not None else None,
                "expected_source": expected_source,
                "fulfillment": fulfillment,
                "note": note,
                "origin_location": origin,
                "destination_location": destination,
                "store_id": canonical_store_id,
                "updated_at": updated_at,
            }

        return self._write("meal_event_set", payload, operation)

    def _recommendation_validate_event_order(
        self, connection: sqlite3.Connection, meal_id: str, mode: str
    ) -> None:
        rows = connection.execute(
            "SELECT event_kind, actual_at FROM meal_timing_events "
            "WHERE meal_id = ? AND actual_at IS NOT NULL",
            (meal_id,),
        ).fetchall()
        positions = {name: index for index, name in enumerate(_EVENT_ORDER[mode])}
        actuals = sorted(rows, key=lambda row: positions[row["event_kind"]])
        previous = None
        for row in actuals:
            current = self._recommendation_datetime_value(row["actual_at"])
            if previous is not None and current < previous:
                raise BusinessError("INVALID_INPUT", "actual event times must follow meal event order", {"meal_id": meal_id})
            previous = current

    def _meal_event_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._recommendation_reject_fields(payload, {"meal_id", "now"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        now_text = self._recommendation_datetime(payload.get("now"), "now", nullable=True)
        now = self._recommendation_datetime_value(now_text) if now_text else datetime.now().astimezone()
        with self.store.read() as connection:
            meal = connection.execute(
                "SELECT id, mode, meal_type, calendar_date, location, store_id "
                "FROM active_entries WHERE id = ? AND record_kind = 'meal'",
                (meal_id,),
            ).fetchone()
            if meal is None:
                raise BusinessError("NOT_FOUND", "active meal was not found", {"meal_id": meal_id})
            rows = connection.execute(
                "SELECT event_id, event_kind, status, actual_at, expected_earliest_at, "
                "expected_latest_at, time_source, expected_source, fulfillment, note, "
                "origin_location, destination_location, store_id, updated_at "
                "FROM meal_timing_events WHERE meal_id = ?",
                (meal_id,),
            ).fetchall()
            events = [dict(row) for row in rows]
        for event in events:
            event["actual_source"] = "manual" if event["actual_at"] is not None else None
        positions = {name: index for index, name in enumerate(_EVENT_ORDER[meal["mode"]])}
        events.sort(key=lambda row: positions[row["event_kind"]])
        event_map = {row["event_kind"]: row for row in events}
        actual_durations = self._recommendation_event_durations(meal["mode"], event_map)
        progress = self._recommendation_meal_progress(meal["mode"], event_map, now)
        return {
            "meal_id": meal_id,
            "mode": meal["mode"],
            "meal_type": meal["meal_type"],
            "fulfillment": next((event["fulfillment"] for event in events if event["fulfillment"] is not None), None),
            "calendar_date": meal["calendar_date"],
            "events": events,
            "actual_durations": actual_durations,
            "progress": progress,
            "now": now.isoformat(timespec="minutes"),
        }

    def _recommendation_event_durations(
        self, mode: str, event_map: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        if mode == "delivery":
            fulfillment = next((row["fulfillment"] for row in event_map.values() if row["fulfillment"] is not None), None)
            end_kind = "ready" if fulfillment == "pickup" else "received"
            pairs = {"order_to_ready": ("ordered", end_kind), **_EVENT_STAGES[mode]}
        else:
            pairs = _EVENT_STAGES[mode]
        durations = {}
        for stage, (start_kind, end_kind) in pairs.items():
            start_row, end_row = event_map.get(start_kind), event_map.get(end_kind)
            if start_row is None or end_row is None or start_row["actual_at"] is None or end_row["actual_at"] is None:
                continue
            start = self._recommendation_datetime_value(start_row["actual_at"])
            end = self._recommendation_datetime_value(end_row["actual_at"])
            durations[stage] = {
                "minutes": round((end - start).total_seconds() / 60),
                "source": "actual",
                "start_event": start_kind,
                "end_event": end_kind,
            }
        return durations

    def _recommendation_meal_progress(
        self, mode: str, event_map: dict[str, dict[str, Any]], now: datetime
    ) -> dict[str, Any] | None:
        if mode != "delivery":
            return None
        order = event_map.get("ordered")
        finish = event_map.get("received")
        ready = event_map.get("ready")
        if order is None:
            return {"status": "not_started", "elapsed_minutes": None, "remaining_minutes": None}
        order_at = order["actual_at"]
        finish_at = finish["actual_at"] if finish else None
        elapsed = None
        if order_at is not None:
            elapsed = max(
                0,
                int((now - self._recommendation_datetime_value(order_at)).total_seconds() // 60),
            )
        if finish_at is not None:
            actual = (
                max(0, int((self._recommendation_datetime_value(finish_at) - self._recommendation_datetime_value(order_at)).total_seconds() // 60))
                if order_at is not None
                else None
            )
            return {
                "status": "completed",
                "elapsed_minutes": actual,
                "remaining_minutes": {"min": 0, "max": 0},
                "completed_at": finish_at,
                "time_source": finish["time_source"],
            }
        if finish is None:
            return {
                "status": "in_progress" if order_at is not None else order["status"],
                "elapsed_minutes": elapsed,
                "remaining_minutes": None,
                "merchant_ready_at": ready["actual_at"] if ready else None,
                "fulfillment": order["fulfillment"],
            }
        earliest = finish["expected_earliest_at"]
        latest = finish["expected_latest_at"]
        remaining = None
        overdue = False
        if earliest is not None:
            earliest_value = self._recommendation_datetime_value(earliest)
            latest_value = self._recommendation_datetime_value(latest)
            remaining = {
                "min": max(0, int((earliest_value - now).total_seconds() // 60)),
                "max": max(0, int((latest_value - now).total_seconds() // 60)),
            }
            overdue = now > latest_value
        return {
            "status": "overdue" if overdue else ("in_progress" if order_at is not None else finish["status"]),
            "elapsed_minutes": elapsed,
            "remaining_minutes": remaining,
            "expected_earliest_at": earliest,
            "expected_latest_at": latest,
            "time_source": finish["expected_source"] or finish["time_source"],
            "fulfillment": finish["fulfillment"],
        }


__all__ = ["HANDLERS", "MIGRATION_SQL", "RecommendationHistoryMixin", "TOOL_SCHEMAS"]


