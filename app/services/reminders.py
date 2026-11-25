from __future__ import annotations

import json
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from .errors import BusinessError
from .tool_schema import _integer, _nullable_string, _request_id, _string, _tool


_RULE_FIELDS = {
    "request_id": _request_id,
    "rule_id": _string,
    "expected_version": _integer,
    "meal_type": {"enum": ["breakfast", "lunch", "dinner", "snack", "other"]},
    "at": {"type": "string", "pattern": r"^[0-9]{2}:[0-9]{2}$"},
    "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}, "minItems": 1},
    "note": _nullable_string,
    "enabled": {"type": "boolean"},
}
_EVENT_FIELDS = {"request_id": _request_id, "event_id": _string}
TOOL_SCHEMAS = [
    _tool("meal_reminder_set", "Create or edit a local recurring mealtime reminder. Weekdays run from Monday (0) to Sunday (6).", _RULE_FIELDS, ("request_id", "meal_type", "at")),
    _tool("meal_reminder_list", "List local meal reminder rules and their current versions.", {}),
    _tool("meal_reminder_pending", "Read pending local reminders. On restart, only the latest missed occurrence per rule is resumed.", {}),
    _tool("meal_reminder_claim", "Claim one optional browser notification across local tabs. This does not acknowledge the reminder.", _EVENT_FIELDS, ("request_id", "event_id")),
    _tool("meal_reminder_ack", "Acknowledge a pending local reminder after displaying or handling it.", _EVENT_FIELDS, ("request_id", "event_id")),
]
HANDLERS = {
    "meal_reminder_set": "_meal_reminder_set",
    "meal_reminder_list": "_meal_reminder_list",
    "meal_reminder_pending": "_meal_reminder_pending",
    "meal_reminder_claim": "_meal_reminder_claim",
    "meal_reminder_ack": "_meal_reminder_ack",
}
MIGRATION_SQL = [
    """CREATE TABLE meal_reminders (
        rule_id TEXT PRIMARY KEY,
        meal_type TEXT NOT NULL,
        at TEXT NOT NULL,
        weekdays_json TEXT NOT NULL,
        note TEXT,
        enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE meal_reminder_events (
        event_id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL REFERENCES meal_reminders(rule_id),
        rule_version INTEGER NOT NULL,
        scheduled_at TEXT NOT NULL,
        claimed_at TEXT,
        acknowledged_at TEXT,
        UNIQUE(rule_id, rule_version, scheduled_at)
    )""",
]


class RemindersMixin:
    @staticmethod
    def _reminder_rule(row) -> dict[str, Any]:
        result = dict(row)
        result["weekdays"] = json.loads(result.pop("weekdays_json"))
        result["enabled"] = bool(result["enabled"])
        return result

    def _meal_reminder_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_RULE_FIELDS))
        rule_id = self._string(payload.get("rule_id"), "rule_id", nullable=True)
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        meal_type = self._string(payload.get("meal_type"), "meal_type")
        at = self._string(payload.get("at"), "at")
        try:
            scheduled = time.fromisoformat(at)
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", "Use a reminder time in HH:MM") from exc
        if at != scheduled.isoformat(timespec="minutes") or scheduled.tzinfo is not None:
            raise BusinessError("INVALID_INPUT", "Use a reminder time in HH:MM")
        weekdays = payload.get("weekdays", list(range(7)))
        enabled = payload.get("enabled", True)
        note = self._string(payload.get("note"), "note", nullable=True)
        if meal_type not in {"breakfast", "lunch", "dinner", "snack", "other"}:
            raise BusinessError("INVALID_INPUT", "Unsupported meal type")
        if not isinstance(weekdays, list) or not weekdays or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays):
            raise BusinessError("INVALID_INPUT", "Choose at least one weekday from 0 to 6")
        if type(enabled) is not bool:
            raise BusinessError("INVALID_INPUT", "enabled must be a boolean")
        if rule_id and expected is None:
            raise BusinessError("INVALID_INPUT", "Editing a reminder requires expected_version")
        if rule_id is None and expected is not None:
            raise BusinessError("INVALID_INPUT", "expected_version requires rule_id")
        days = self._json(sorted(set(weekdays)))

        def operation(connection):
            key = rule_id or uuid.uuid4().hex
            if rule_id:
                row = connection.execute("SELECT * FROM meal_reminders WHERE rule_id = ?", (key,)).fetchone()
                if row is None:
                    raise BusinessError("NOT_FOUND", "Reminder not found")
                self._check_version(row, key, expected)
                connection.execute(
                    """UPDATE meal_reminders SET meal_type=?, at=?, weekdays_json=?, note=?,
                       enabled=?, version=version+1, updated_at=? WHERE rule_id=?""",
                    (meal_type, at, days, note, enabled, self._now(), key),
                )
            else:
                connection.execute(
                    """INSERT INTO meal_reminders(rule_id, meal_type, at, weekdays_json, note,
                       enabled, version, updated_at) VALUES(?, ?, ?, ?, ?, ?, 1, ?)""",
                    (key, meal_type, at, days, note, enabled, self._now()),
                )
            return self._reminder_rule(connection.execute("SELECT * FROM meal_reminders WHERE rule_id=?", (key,)).fetchone())

        return self._write("meal_reminder_set", payload, operation)

    def _meal_reminder_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set())
        with self.store.read() as connection:
            rows = connection.execute("SELECT * FROM meal_reminders ORDER BY at, rule_id").fetchall()
        return {"items": [self._reminder_rule(row) for row in rows]}

    def tick_meal_reminders(self, now: datetime | None = None) -> None:
        moment = now or datetime.now().astimezone()
        with self.store.transaction() as connection:
            for rule in connection.execute("SELECT * FROM meal_reminders WHERE enabled=1").fetchall():
                weekdays = json.loads(rule["weekdays_json"])
                since = datetime.fromisoformat(rule["updated_at"])
                for days_ago in range(8):
                    day = moment.date() - timedelta(days=days_ago)
                    if day.weekday() not in weekdays:
                        continue
                    scheduled = datetime.combine(day, time.fromisoformat(rule["at"]), moment.tzinfo)
                    if scheduled > moment:
                        continue
                    if scheduled >= since:
                        connection.execute(
                            "INSERT OR IGNORE INTO meal_reminder_events(event_id, rule_id, rule_version, scheduled_at) VALUES(?, ?, ?, ?)",
                            (uuid.uuid4().hex, rule["rule_id"], rule["version"], scheduled.isoformat(timespec="minutes")),
                        )
                    break

    def _meal_reminder_pending(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set())
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT e.event_id, e.rule_id, e.scheduled_at, e.claimed_at, r.meal_type, r.note
                   FROM meal_reminder_events e JOIN meal_reminders r ON r.rule_id=e.rule_id
                   WHERE r.enabled=1 AND r.version=e.rule_version AND e.acknowledged_at IS NULL
                   AND e.scheduled_at=(SELECT MAX(newest.scheduled_at) FROM meal_reminder_events newest
                       WHERE newest.rule_id=e.rule_id AND newest.rule_version=e.rule_version)
                   ORDER BY e.scheduled_at, e.rule_id"""
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def _update_reminder_event(self, payload: dict[str, Any], *, acknowledge: bool) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_EVENT_FIELDS))
        event_id = self._string(payload.get("event_id"), "event_id")
        action = "meal_reminder_ack" if acknowledge else "meal_reminder_claim"

        def operation(connection):
            row = connection.execute(
                """SELECT e.*, r.enabled, r.version, NOT EXISTS(
                       SELECT 1 FROM meal_reminder_events newer WHERE newer.rule_id=e.rule_id
                       AND newer.rule_version=e.rule_version AND newer.scheduled_at>e.scheduled_at
                   ) AS is_latest FROM meal_reminder_events e
                   JOIN meal_reminders r ON r.rule_id=e.rule_id WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "Reminder event not found")
            if acknowledge:
                connection.execute(
                    "UPDATE meal_reminder_events SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE event_id=?",
                    (self._now(), event_id),
                )
                return {"event_id": event_id, "acknowledged": True}
            current = row["enabled"] and row["version"] == row["rule_version"] and row["is_latest"]
            claimed = bool(current and row["acknowledged_at"] is None and row["claimed_at"] is None)
            if claimed:
                connection.execute("UPDATE meal_reminder_events SET claimed_at=? WHERE event_id=?", (self._now(), event_id))
            return {"event_id": event_id, "claimed": claimed}

        return self._write(action, payload, operation)

    def _meal_reminder_claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_reminder_event(payload, acknowledge=False)

    def _meal_reminder_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_reminder_event(payload, acknowledge=True)
