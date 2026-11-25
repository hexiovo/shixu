from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from .errors import BusinessError
from .recommendation_history import _EVENT_STAGES
from .tool_schema import _integer, _object, _request_id, _string, _tool


_STRINGS = {"type": "array", "items": _string}
_STEP = _object({"title": _string, "minutes": _integer, "resources": _STRINGS}, ("title", "minutes"))
_RECIPE_REF = _object({
    "recipe_id": _string, "version": _integer, "servings": _integer,
    "steps": {"type": "array", "items": _STEP},
}, ("recipe_id", "version"))
_CONFIRM = {
    "recipe_id": _string, "version": _integer, "request_id": _request_id, "ingredients": _STRINGS, "allergens": _STRINGS,
    "source": _string, "complete": {"type": "boolean"},
    "expected_updated_at": _string,
}
TOOL_SCHEMAS = [
    _tool("meal_journey_remaining", "Read the remaining time to start, finish or return using recorded actual or expected milestones.", {
        "meal_id": _string, "now": _string, "deadline": _string, "target": {"enum": ["meal_start", "finished", "returned"]},
    }, ("meal_id",)),
    _tool("recipe_versions_compare", "Compare two saved recipe versions including source and changed details.", {
        "recipe_id": _string, "from_version": _integer, "to_version": _integer,
    }, ("recipe_id", "from_version", "to_version")),
    _tool("recipe_adjust_preview", "Preview explicit ingredient substitutions and serving changes for a particular saved recipe version.", {
        "recipe_id": _string, "version": _integer, "servings": _integer, "target_expected_version": _integer,
        "substitutions": {"type": "array", "items": _object({
            "index": _integer, "name": _string, "quantity": {"type": ["number", "string", "null"]},
            "unit": {"type": ["string", "null"]},
        }, ("index", "name"))},
    }, ("recipe_id", "version")),
    _tool("recipe_work_plan", "Schedule multiple recipes using ordered steps and shared named resources. Unspecified durations remain missing.", {
        "recipes": {"type": "array", "items": _RECIPE_REF}
    }, ("recipes",)),
    _tool("recipe_ingredients_verify", "Read or record referenced ingredient and allergen evidence for exactly one saved recipe version.", _CONFIRM, ("recipe_id", "version")),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}
MIGRATION_SQL = [
    """CREATE TABLE recipe_ingredient_checks(
       recipe_id TEXT NOT NULL, version INTEGER NOT NULL, details TEXT NOT NULL, updated_at TEXT NOT NULL,
       PRIMARY KEY(recipe_id,version), FOREIGN KEY(recipe_id,version) REFERENCES recipe_versions(recipe_id,version))""",
]


class MealPlanningMixin:
    def _meal_journey_remaining(self, payload):
        self._reject_extra_fields(payload, {"meal_id", "now", "deadline", "target"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        now_text = self._recommendation_datetime(payload.get("now"), "now", nullable=True)
        now = self._recommendation_datetime_value(now_text or self._now())
        due = self._recommendation_datetime(payload.get("deadline"), "deadline", nullable=True)
        deadline = self._recommendation_datetime_value(due) if due else None
        target = self._recommendation_enum(payload.get("target", "meal_start"), "target", {"meal_start", "finished", "returned"})
        with self.store.read() as connection:
            timeline = self._meal_event_list({"meal_id": meal_id, "now": now.isoformat()})
            timing = self._entry(connection, meal_id)["timing"]
        events = {event["event_kind"]: event for event in timeline["events"]}
        start = {"eat_out": "ready", "delivery": "received", "home_cooking": "meal_ready"}
        target_event = {"meal_start": "meal_started", "finished": "meal_finished", "returned": "returned"}[target]
        event = events.get(target_event)
        if target == "meal_start" and not (event and (event["actual_at"] or event["expected_latest_at"])):
            event = events.get(start[timeline["mode"]]) or event
        completed = [event for event in events.values() if event["actual_at"]]
        current = max(completed, key=lambda event: self._recommendation_datetime_value(event["actual_at"]), default=None)
        earliest = latest = remaining = None
        status = "needs_verification"
        basis = None
        if event and event["actual_at"]:
            earliest = latest = self._recommendation_datetime_value(event["actual_at"])
            status = "completed" if event["event_kind"] == target_event else "ready"
            basis = "actual_event"
        elif event and event["expected_latest_at"]:
            earliest = self._recommendation_datetime_value(event["expected_earliest_at"])
            latest = self._recommendation_datetime_value(event["expected_latest_at"])
            status = "overdue" if now > latest else "estimated"
            basis = event["expected_source"] or "expected_event"
        if earliest is None and current:
            estimate = self._journey_from_profile(timeline, current, target, start[timeline["mode"]])
            if estimate:
                earliest, latest = estimate
                status = "overdue" if now > latest else "estimated"
                basis = "saved_route_profile"
        recorded_key = {"meal_start": "ready_at", "finished": "ended_at", "returned": "return_at"}[target]
        if basis not in {"actual_event"} and timing.get(recorded_key):
            earliest = latest = self._parse_now(timing[recorded_key])
            status = "ready" if target == "meal_start" else "completed"
            basis = "meal_timing"
        if earliest and status != "overdue":
            remaining = {"min": max(0, math.ceil((earliest-now).total_seconds()/60)),
                         "max": max(0, math.ceil((latest-now).total_seconds()/60))}
        fits = None
        if deadline:
            fits = "needs_verification"
            if earliest and status != "overdue":
                fits = "fits" if latest <= deadline else ("exceeds" if earliest > deadline else "needs_verification")
        return {
            "meal_id": meal_id, "target": target, "status": status, "fits_deadline": fits,
            "last_completed_event": current, "target_event": event, "remaining_minutes": remaining, "basis": basis,
            "missing": [] if earliest and status != "overdue" else [target_event],
        }

    def _journey_from_profile(self, timeline, current, target, readiness):
        mode = timeline["mode"]
        cursor = current["event_kind"]
        if target == "meal_start":
            pairs = dict(_EVENT_STAGES[mode])
            if mode == "delivery" and timeline["fulfillment"] == "delivery":
                pairs["order_to_ready"] = ("ordered", "received")
            end = readiness
        else:
            pairs = {"meal": ("meal_started", "meal_finished"), "return": ("meal_finished", "returned")}
            end = "meal_finished" if target == "finished" else "returned"
        needed = []
        while cursor != end:
            following = next(((name, finish) for name, (begin, finish) in pairs.items() if begin == cursor), None)
            if following is None:
                return None
            name, cursor = following
            needed.append(name)
        if not needed:
            return None
        with self.store.read() as connection:
            profile = self._recommendation_profile_for_dimensions(connection, {
                "mode": mode, "meal_type": timeline["meal_type"], "fulfillment": timeline["fulfillment"],
                **{key: current.get(key) for key in ("origin_location", "destination_location", "store_id")},
            })
        stages = (profile or {}).get("stages", {})
        if any(stages.get(stage) is None for stage in needed):
            return None
        anchor = self._recommendation_datetime_value(current["actual_at"])
        return tuple(anchor + timedelta(minutes=sum(stages[stage][key] for stage in needed))
                     for key in ("min_minutes", "max_minutes"))

    def _recipe_versions_compare(self, payload):
        self._reject_extra_fields(payload, {"recipe_id", "from_version", "to_version"})
        with self.store.read():
            left, right = [self._recipe_version_get({"recipe_id": payload.get("recipe_id"), "version": payload.get(key)})
                           for key in ("from_version", "to_version")]
        return {
            "recipe_id": left["recipe_id"], "from_version": left["version"], "to_version": right["version"],
            "fields": [{"field": key, "before": left.get(key), "after": right.get(key)}
                       for key in ("name", "source_url", "source_kind", "source_text") if left.get(key) != right.get(key)],
            "details": [{"field": key, "before": left["details"].get(key), "after": right["details"].get(key)}
                        for key in sorted(left["details"].keys() | right["details"].keys())
                        if left["details"].get(key) != right["details"].get(key)],
        }

    def _recipe_adjust_preview(self, payload):
        self._reject_extra_fields(payload, {"recipe_id", "version", "servings", "substitutions", "target_expected_version"})
        recipe = self._recipe_version_get({"recipe_id": payload.get("recipe_id"), "version": payload.get("version")})
        target_version = self._integer(payload.get("target_expected_version"), "target_expected_version", minimum=1, nullable=True)
        details = recipe["details"]
        base = details.get("servings")
        if type(base) is not int or base <= 0:
            base = None
        requested = self._integer(payload.get("servings"), "servings", minimum=1, nullable=True)
        servings = requested or base
        edits = {}
        for raw in self._list(payload.get("substitutions"), "substitutions", default=[]):
            replacement = self._dict(raw, "replacement")
            self._reject_extra_fields(replacement, {"index", "name", "quantity", "unit"})
            index = self._integer(replacement.get("index"), "index", minimum=0)
            if index in edits:
                raise BusinessError("INVALID_INPUT", "Use one substitution per ingredient index")
            edits[index] = {key: value for key, value in replacement.items() if key != "index"}
        names = details.get("ingredients") or []
        amounts = details.get("ingredient_amounts") or []
        if not edits.keys() <= set(range(len(names))):
            raise BusinessError("INVALID_INPUT", "An ingredient index is outside this recipe version")
        ingredients = []
        for index, name in enumerate(names):
            if index in edits:
                ingredients.append(edits[index])
                continue
            raw = amounts[index] if index < len(amounts) and isinstance(amounts[index], dict) else {}
            amount = raw.get("quantity")
            if requested and requested != base:
                known = self._parse_decimal(amount)
                amount = self._decimal_text(known * requested / base) if known is not None and base else None
            ingredients.append({"name": name, "quantity": amount, "unit": raw.get("unit")})
        draft = {"name": recipe["name"], "servings": servings, "ingredients": ingredients, "steps": details.get("steps") or []}
        source = {"source_kind": "structured_draft", "structured_draft": draft, "source_url": recipe["source_url"]}
        with self.store.read() as connection:
            live = connection.execute("SELECT version FROM recipes WHERE recipe_id=?", (recipe["recipe_id"],)).fetchone()
            current = live["version"] if live else None
            destination = target_version if target_version is not None else (current if current == recipe["version"] else None)
            if destination is not None:
                source.update(recipe_id=recipe["recipe_id"], expected_version=destination)
            preview = self._recipe_import_preview(source)
        return {"base_version": recipe["version"], "current_version": current, "save_as": "new_recipe" if destination is None else "new_version",
                "import_arguments": source, "preview": preview,
                "substituted_indices": sorted(edits)}

    def _recipe_work_plan(self, payload):
        self._reject_extra_fields(payload, {"recipes"})
        recipes = self._list(payload.get("recipes"), "recipes")
        if not 1 <= len(recipes) <= 12:
            raise BusinessError("INVALID_INPUT", "Provide one to twelve recipes")
        captured = []
        missing = []
        with self.store.read():
            for raw in recipes:
                spec = self._dict(raw, "recipe")
                self._reject_extra_fields(spec, set(_RECIPE_REF["properties"]))
                saved = self._recipe_version_get({"recipe_id": spec.get("recipe_id"), "version": spec.get("version")})
                servings = self._integer(spec.get("servings"), "servings", minimum=1, nullable=True)
                steps = spec.get("steps")
                if steps is None and (servings is None or servings == saved["details"].get("servings")):
                    steps = saved["details"].get("work_steps")
                if not steps:
                    missing.append({"recipe_id": saved["recipe_id"], "name": saved["name"], "field": "timed_steps"})
                    continue
                steps = self._list(steps, "steps")
                if len(steps) > 60:
                    raise BusinessError("INVALID_INPUT", "Provide at most 60 timed steps per recipe")
                validated = []
                for raw_step in steps:
                    step = self._dict(raw_step, "step")
                    self._reject_extra_fields(step, set(_STEP["properties"]))
                    validated.append((
                        self._string(step.get("title"), "title"),
                        self._integer(step.get("minutes"), "minutes", minimum=0),
                        sorted({self._ingredient_key(self._string(resource, "resource"))
                                for resource in self._list(step.get("resources"), "resources", default=[])}),
                    ))
                captured.append((saved, validated))
        resources = {}
        end_at = [0] * len(captured)
        next_step = [0] * len(captured)
        schedule = []
        while True:
            possible = []
            for index, (_, steps) in enumerate(captured):
                step_index = next_step[index]
                if step_index < len(steps):
                    title, duration, required = steps[step_index]
                    begin = max(end_at[index], *(resources.get(resource, 0) for resource in required), 0)
                    possible.append((begin, index, title, duration, required))
            if not possible:
                break
            begin, index, title, duration, required = min(possible)
            end = begin + duration
            end_at[index] = end
            next_step[index] += 1
            for resource in required:
                resources[resource] = end
            saved, _ = captured[index]
            schedule.append({"recipe_id": saved["recipe_id"], "version": saved["version"], "title": title,
                             "start_minute": begin, "end_minute": end, "resources": required})
        known = max(end_at, default=0)
        return {"steps": schedule, "total_minutes": None if missing else known, "known_schedule_minutes": known, "missing": missing}

    def _recipe_confirmed_evidence(self, connection, recipe_id, version):
        row = connection.execute(
            "SELECT details,updated_at FROM recipe_ingredient_checks WHERE recipe_id=? AND version=?", (recipe_id, version)
        ).fetchone()
        return {**json.loads(row["details"]), "updated_at": row["updated_at"]} if row else None

    def _recipe_ingredients_verify(self, payload):
        self._reject_extra_fields(payload, set(_CONFIRM))
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        version = self._integer(payload.get("version"), "version", minimum=1)
        key = {"recipe_id": recipe_id, "version": version}
        saved = self._recipe_version_get(key)
        if "request_id" not in payload:
            self._reject_extra_fields(payload, set(key))
            with self.store.read() as connection:
                return {**key, "recipe_ingredients": saved["details"].get("ingredients"),
                        "confirmation": self._recipe_confirmed_evidence(connection, recipe_id, version)}
        complete = payload.get("complete", False)
        if type(complete) is not bool:
            raise BusinessError("INVALID_INPUT", "complete must be a boolean")
        check = {
            name: [self._string(item, name) for item in self._list(payload.get(name), name)]
            for name in ("ingredients", "allergens")
        }
        check.update(source=self._string(payload.get("source"), "source"), ingredients_known=complete, allergens_known=complete)
        expected = self._string(payload.get("expected_updated_at"), "expected_updated_at", nullable=True)

        def operation(connection):
            existing = self._recipe_confirmed_evidence(connection, recipe_id, version)
            actual = existing["updated_at"] if existing else None
            if actual != expected:
                raise BusinessError("VERSION_CONFLICT", "Read the current confirmation before modifying it", {"actual_updated_at": actual})
            updated = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            connection.execute("""INSERT INTO recipe_ingredient_checks VALUES(?,?,?,?) ON CONFLICT(recipe_id,version)
                                   DO UPDATE SET details=excluded.details,updated_at=excluded.updated_at""",
                               (recipe_id, version, self._json(check), updated))
            return {**key, "confirmation": self._recipe_confirmed_evidence(connection, recipe_id, version)}

        return self._write("recipe_ingredients_verify", payload, operation)
