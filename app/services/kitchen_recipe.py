from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
import re
import sqlite3
import uuid
from typing import Any

from .errors import BusinessError
from .tool_schema import _integer, _nullable_string, _object, _request_id, _string, _tool


_FEEDBACK_KINDS = {"liked", "want_to_try", "temporarily_tired"}
_PAGE = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "offset": {"type": "integer", "minimum": 0},
}
_EXPECTED_VERSION = {"type": ["integer", "null"], "minimum": 1}
_STRUCTURED_INGREDIENT = _object(
    {
        "name": _string,
        "quantity": {"type": ["string", "number", "null"]},
        "unit": _nullable_string,
        "raw_text": _nullable_string,
    },
    ("name",),
)
_STRUCTURED_STEP = _object(
    {"title": _string, "instruction": _nullable_string},
    ("title",),
)
_STRUCTURED_DRAFT = _object(
    {
        "name": _string,
        "servings": _EXPECTED_VERSION,
        "ingredients": {"type": "array", "items": {"anyOf": [_string, _STRUCTURED_INGREDIENT]}},
        "steps": {"type": "array", "items": {"anyOf": [_string, _STRUCTURED_STEP]}},
    },
    ("name", "ingredients"),
)
_IMPORT_PROPERTIES = {
    "source_kind": {"type": "string", "enum": ["pasted_text", "subtitle", "structured_draft", "ai_generated"]},
    "source_text": _nullable_string,
    "structured_draft": {"anyOf": [_STRUCTURED_DRAFT, {"type": "null"}]},
    "source_url": _nullable_string,
    "name": _nullable_string,
    "recipe_id": _nullable_string,
    "expected_version": _EXPECTED_VERSION,
}
_SEASONING_CHANGE = _object(
    {
        "ingredient": _string,
        "planned_quantity": _nullable_string,
        "actual_quantity": _string,
        "unit": _nullable_string,
    },
    ("ingredient", "actual_quantity"),
)
_SOURCE_VERSION = {"type": "integer", "minimum": 1}
_RECIPE_SCALE_UNITS = ("g", "kg", "ml", "L", "克", "千克", "公斤", "毫升", "升")
_TARGET_RECIPE_UNIT = {"type": ["string", "null"], "enum": [None, *_RECIPE_SCALE_UNITS]}


def _normalize_quantity(value: Any, unit: str | None) -> tuple[Decimal, str] | None:
    if value is None or unit is None or not unit.strip():
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not quantity.is_finite():
        return None
    source_unit = unit.strip()
    unit_key = source_unit.casefold()
    normalized_unit = source_unit
    if unit_key in {"kg", "千克", "公斤"}:
        quantity *= Decimal("1000")
        normalized_unit = "g"
    elif unit_key in {"l", "升"}:
        quantity *= Decimal("1000")
        normalized_unit = "ml"
    elif unit_key in {"g", "克"}:
        normalized_unit = "g"
    elif unit_key in {"ml", "毫升"}:
        normalized_unit = "ml"
    return quantity, normalized_unit


TOOL_SCHEMAS = [
    _tool(
        "recipe_import_preview",
        "Parse user-provided recipe text, subtitles, or a structured draft for review. No network access or writes occur.",
        {**_IMPORT_PROPERTIES},
        ("source_kind",),
    ),
    _tool(
        "recipe_import_commit",
        "Persist a recipe only after its preview signature is confirmed. Each commit creates an immutable recipe version.",
        {
            **_IMPORT_PROPERTIES,
            "request_id": _request_id,
            "preview_signature": _string,
            "confirm": {"type": "boolean", "const": True},
        },
        ("source_kind", "request_id", "preview_signature", "confirm"),
    ),
    _tool(
        "recipe_scale",
        "Scale a saved recipe to a target serving count, optionally convert compatible metric units and report expired kitchen stock as of today.",
        {
            "recipe_id": _string,
            "target_servings": {"type": "integer", "minimum": 1},
            "target_unit": _TARGET_RECIPE_UNIT,
        },
        ("recipe_id", "target_servings"),
    ),
    _tool(
        "recipe_version_list",
        "List saved immutable recipe versions, including the migration baseline for recipes that existed before version history.",
        {"recipe_id": _string, **_PAGE},
        ("recipe_id",),
    ),
    _tool(
        "recipe_version_get",
        "Read one immutable recipe version together with its original source text and source metadata.",
        {"recipe_id": _string, "version": _SOURCE_VERSION},
        ("recipe_id", "version"),
    ),
    _tool(
        "recipe_feedback_add",
        "Save recipe feedback, optionally linked to a real meal, including purpose and actual seasoning quantity changes.",
        {
            "request_id": _request_id,
            "recipe_id": _string,
            "meal_id": _nullable_string,
            "purpose": _string,
            "feedback_kind": {"type": "string", "enum": sorted(_FEEDBACK_KINDS)},
            "reason": _nullable_string,
            "seasoning_changes": {"type": "array", "maxItems": 50, "items": _SEASONING_CHANGE},
            "expires_at": {"type": ["string", "null"], "format": "date-time"},
        },
        ("request_id", "recipe_id", "purpose", "feedback_kind"),
    ),
    _tool(
        "recipe_feedback_list",
        "List recipe feedback newest first, including its meal link and seasoning changes.",
        {"recipe_id": _string, **_PAGE},
        ("recipe_id",),
    ),
]


HANDLERS = {
    "recipe_import_preview": "_recipe_import_preview",
    "recipe_import_commit": "_recipe_import_commit",
    "recipe_scale": "_recipe_scale",
    "recipe_version_list": "_recipe_version_list",
    "recipe_version_get": "_recipe_version_get",
    "recipe_feedback_add": "_recipe_feedback_add",
    "recipe_feedback_list": "_recipe_feedback_list",
}


MIGRATION_SQL = [
    "CREATE TABLE IF NOT EXISTS recipe_versions ("
    "recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE, "
    "version INTEGER NOT NULL CHECK(version > 0), "
    "name TEXT NOT NULL, details_json TEXT NOT NULL, source_text TEXT, source_url TEXT, "
    "source_kind TEXT, created_at TEXT NOT NULL, "
    "migration_baseline INTEGER NOT NULL DEFAULT 0 CHECK(migration_baseline IN (0, 1)), "
    "PRIMARY KEY(recipe_id, version))",
    "INSERT OR IGNORE INTO recipe_versions "
    "(recipe_id, version, name, details_json, source_text, source_url, source_kind, created_at, migration_baseline) "
    "SELECT recipe_id, version, name, details_json, NULL, NULL, NULL, updated_at, 1 FROM recipes",
    "CREATE INDEX IF NOT EXISTS recipe_versions_created_at "
    "ON recipe_versions(recipe_id, version DESC)",
    "CREATE TABLE IF NOT EXISTS recipe_feedback ("
    "feedback_id TEXT PRIMARY KEY, "
    "recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id) ON DELETE CASCADE, "
    "recipe_version INTEGER NOT NULL, meal_id TEXT REFERENCES entries(id), "
    "purpose TEXT NOT NULL, "
    "feedback_kind TEXT NOT NULL CHECK(feedback_kind IN ('liked', 'want_to_try', 'temporarily_tired')), "
    "reason TEXT, seasoning_changes_json TEXT NOT NULL, recorded_at TEXT NOT NULL, expires_at TEXT, "
    "FOREIGN KEY(recipe_id, recipe_version) REFERENCES recipe_versions(recipe_id, version))",
    "CREATE INDEX IF NOT EXISTS recipe_feedback_latest "
    "ON recipe_feedback(recipe_id, recorded_at DESC, feedback_id DESC)",
    "CREATE VIEW IF NOT EXISTS latest_recipe_feedback AS "
    "SELECT f.feedback_id, f.recipe_id, f.meal_id, f.purpose, f.feedback_kind, f.reason, "
    "f.seasoning_changes_json, f.recorded_at, f.expires_at "
    "FROM recipe_feedback AS f WHERE f.feedback_id = ("
    "SELECT newest.feedback_id FROM recipe_feedback AS newest "
    "WHERE newest.recipe_id = f.recipe_id "
    "ORDER BY newest.recorded_at DESC, newest.feedback_id DESC LIMIT 1)",
]


class KitchenRecipeMixin:
    @staticmethod
    def _reject_fields(payload: dict[str, Any], allowed: set[str], field: str = "payload") -> None:
        extra = next((name for name in payload if name not in allowed), None)
        if extra is not None:
            detail = extra if field == "payload" else f"{field}.{extra}"
            raise BusinessError("INVALID_INPUT", f"{field} contains unsupported fields", {"field": detail})

    @staticmethod
    def _canonical_json(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BusinessError("INVALID_INPUT", "payload must contain JSON-compatible values") from exc

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _parse_decimal(value: Any) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return number if number.is_finite() else None

    @staticmethod
    def _parse_datetime(value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BusinessError("INVALID_INPUT", f"{field} must be an ISO date-time", {"field": field}) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _source_lines(source_text: str, source_kind: str) -> list[str]:
        result = []
        for raw_line in source_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if source_kind == "subtitle":
                if line.upper() == "WEBVTT" or line.isdigit() or line.startswith("NOTE "):
                    continue
                if re.match(r"^(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}\s+-->\s+", line):
                    continue
                line = html.unescape(re.sub(r"<[^>]+>", "", line))
                line = re.sub(r"\b(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3}\b", "", line).strip()
            else:
                line = re.sub(r"^\s*#{1,6}\s*", "", line)
            if line:
                result.append(line)
        return result

    @staticmethod
    def _section(line: str) -> str | None:
        normalized = re.sub(r"[\s:：]+$", "", line).strip().casefold()
        if normalized in {"食材", "配料", "材料", "所需食材", "用料", "ingredients"}:
            return "ingredients"
        if normalized in {"步骤", "做法", "制作方法", "烹饪步骤", "steps", "method", "directions"}:
            return "steps"
        return None

    @classmethod
    def _ingredient_line(cls, line: str) -> dict[str, Any]:
        text = re.sub(r"^(?:[-*•·]|\d+[.)、）])\s*", "", line).strip()
        unit_pattern = r"([A-Za-zµμ]+|克|千克|公斤|毫升|升|个|只|枚|片|勺|茶匙|汤匙)?"
        number_pattern = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
        prefix = re.match(rf"^{number_pattern}\s*{unit_pattern}\s*(.+)$", text)
        if prefix:
            quantity, unit, name = prefix.groups()
            name = name.strip()
            if name:
                return {
                    "name": name,
                    "quantity": quantity,
                    "unit": unit or None,
                    "quantity_known": cls._parse_decimal(quantity) is not None and bool(unit),
                    "raw_text": line,
                }
        suffix = re.match(rf"^(.+?)\s*{number_pattern}\s*{unit_pattern}$", text)
        if suffix:
            name, quantity, unit = suffix.groups()
            name = name.strip()
            if name:
                return {
                    "name": name,
                    "quantity": quantity,
                    "unit": unit or None,
                    "quantity_known": cls._parse_decimal(quantity) is not None and bool(unit),
                    "raw_text": line,
                }
        return {"name": text, "quantity": None, "unit": None, "quantity_known": False, "raw_text": line}

    @staticmethod
    def _step_line(line: str) -> dict[str, Any]:
        text = re.sub(r"^(?:[-*•·]|\d+[.)、）])\s*", "", line).strip()
        match = re.match(r"^([^:：]+)[:：](.*)$", text)
        if match and match.group(1).strip():
            return {"title": match.group(1).strip(), "instruction": match.group(2).strip() or None}
        return {"title": text, "instruction": None}

    def _structured_draft(self, value: Any, name_override: str | None) -> dict[str, Any]:
        draft = self._dict(value, "structured_draft")
        self._reject_fields(draft, {"name", "servings", "ingredients", "steps"}, "structured_draft")
        name = name_override or self._string(draft.get("name"), "structured_draft.name")
        servings_value = draft.get("servings")
        servings = self._integer(servings_value, "structured_draft.servings", nullable=True, minimum=1)
        ingredients_value = self._list(draft.get("ingredients"), "structured_draft.ingredients")
        ingredients = []
        for index, value in enumerate(ingredients_value):
            field = f"structured_draft.ingredients[{index}]"
            if isinstance(value, str):
                ingredient = {"name": self._string(value, field), "quantity": None, "unit": None, "quantity_known": False, "raw_text": value}
            else:
                item = self._dict(value, field)
                self._reject_fields(item, {"name", "quantity", "unit", "raw_text"}, field)
                ingredient_name = self._string(item.get("name"), f"{field}.name")
                raw_quantity = item.get("quantity")
                if raw_quantity is not None and (isinstance(raw_quantity, bool) or not isinstance(raw_quantity, (str, int, float))):
                    raise BusinessError("INVALID_INPUT", "quantity must be a string, number, or null", {"field": f"{field}.quantity"})
                quantity = None if raw_quantity is None else str(raw_quantity)
                unit = self._string(item.get("unit"), f"{field}.unit", nullable=True)
                raw_text = self._string(item.get("raw_text"), f"{field}.raw_text", nullable=True)
                ingredient = {
                    "name": ingredient_name,
                    "quantity": quantity,
                    "unit": unit,
                    "quantity_known": self._parse_decimal(quantity) is not None and bool(unit and unit.strip()),
                    "raw_text": raw_text,
                }
            ingredients.append(ingredient)
        steps_value = self._list(draft.get("steps", []), "structured_draft.steps")
        steps = []
        for index, value in enumerate(steps_value):
            field = f"structured_draft.steps[{index}]"
            if isinstance(value, str):
                steps.append({"title": self._string(value, field), "instruction": None})
            else:
                step = self._dict(value, field)
                self._reject_fields(step, {"title", "instruction"}, field)
                steps.append(
                    {
                        "title": self._string(step.get("title"), f"{field}.title"),
                        "instruction": self._string(step.get("instruction"), f"{field}.instruction", nullable=True),
                    }
                )
        return {"name": name, "servings": servings, "ingredients": ingredients, "steps": steps, "cleaned_text": None}

    def _text_draft(self, source_text: str, source_kind: str, name_override: str | None) -> dict[str, Any]:
        lines = self._source_lines(source_text, source_kind)
        name = name_override
        servings = None
        section = None
        ingredients = []
        steps = []
        for line in lines:
            servings_match = re.fullmatch(
                r"(?:(?:份量|人数|servings?)\s*[:：]?\s*(\d+)\s*(?:人份|人|份)?|(\d+)\s*(?:人份|人|份))",
                line,
                re.IGNORECASE,
            )
            if servings_match:
                servings = self._integer(int(servings_match.group(1) or servings_match.group(2)), "servings", minimum=1)
                continue
            section_header = self._section(line)
            if section_header is not None:
                section = section_header
                continue
            name_header = re.match(r"^(?:菜名|食谱名|recipe\s*name)\s*[:：]\s*(.+)$", line, re.IGNORECASE)
            if name_header and name is None:
                name = self._string(name_header.group(1), "name")
                continue
            if section == "ingredients":
                ingredients.append(self._ingredient_line(line))
            elif section == "steps" or source_kind == "subtitle":
                steps.append(self._step_line(line))
            elif name is None:
                name = line.strip()
        if name is None:
            raise BusinessError("INVALID_INPUT", "recipe name could not be determined; provide name", {"field": "name"})
        return {
            "name": self._string(name, "name"),
            "servings": servings,
            "ingredients": ingredients,
            "steps": steps,
            "cleaned_text": "\n".join(lines),
        }

    @staticmethod
    def _draft_details(draft: dict[str, Any], source_kind: str, source_url: str | None) -> dict[str, Any]:
        return {
            "servings": draft["servings"],
            "ingredients": [item["name"] for item in draft["ingredients"]],
            "ingredient_amounts": draft["ingredients"],
            "steps": draft["steps"],
            "source": {"kind": source_kind, "url": source_url},
        }

    def _import_candidate(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
        source_kind = self._string(payload.get("source_kind"), "source_kind")
        if source_kind not in {"pasted_text", "subtitle", "structured_draft", "ai_generated"}:
            raise BusinessError("INVALID_INPUT", "source_kind is unsupported", {"field": "source_kind"})
        name_override = self._string(payload.get("name"), "name", nullable=True)
        source_url = self._string(payload.get("source_url"), "source_url", nullable=True)
        structured_value = payload.get("structured_draft")
        source_text_value = payload.get("source_text")
        if source_kind in {"structured_draft", "ai_generated"}:
            if structured_value is None or source_text_value is not None:
                raise BusinessError("INVALID_INPUT", "structured_draft requires structured_draft only", {"field": "structured_draft"})
            draft = self._structured_draft(structured_value, name_override)
            raw_source_text = self._canonical_json(structured_value)
        else:
            if source_text_value is None or structured_value is not None:
                raise BusinessError("INVALID_INPUT", "pasted_text and subtitle require source_text only", {"field": "source_text"})
            if not isinstance(source_text_value, str) or not source_text_value.strip():
                raise BusinessError("INVALID_INPUT", "source_text must be a non-empty string", {"field": "source_text"})
            raw_source_text = source_text_value
            draft = self._text_draft(source_text_value, source_kind, name_override)
        if not draft["ingredients"]:
            draft["warnings"] = [{"code": "NO_INGREDIENTS", "message": "No ingredient lines were identified."}]
        else:
            draft["warnings"] = [
                {"code": "UNKNOWN_QUANTITY", "ingredient": item["name"], "index": index}
                for index, item in enumerate(draft["ingredients"])
                if not item["quantity_known"]
            ]
        if draft["servings"] is None:
            draft["warnings"].append({"code": "UNKNOWN_SERVINGS", "message": "Serving count is unknown; scaling will be unavailable."})
        if not draft["steps"]:
            draft["warnings"].append({"code": "NO_STEP_TITLES", "message": "No step titles were identified."})
        details = self._draft_details(draft, source_kind, source_url)
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id", nullable=True)
        expected_version = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        if (recipe_id is None) != (expected_version is None):
            raise BusinessError("INVALID_INPUT", "recipe_id and expected_version must be provided together", {"field": "recipe_id"})
        current_version = None
        if recipe_id is not None:
            row = connection.execute("SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
            self._check_version(row, recipe_id, expected_version)
            current_version = row["version"]
        name = draft["name"]
        signed = {
            "source_kind": source_kind,
            "source_text": raw_source_text,
            "source_url": source_url,
            "name": name,
            "recipe_id": recipe_id,
            "current_version": current_version,
            "details": details,
        }
        signature = hashlib.sha256(self._canonical_json(signed).encode("utf-8")).hexdigest()
        return {
            "source_kind": source_kind,
            "source_text": raw_source_text,
            "source_url": source_url,
            "recipe_id": recipe_id,
            "current_version": current_version,
            "name": name,
            "details": details,
            "warnings": draft["warnings"],
            "cleaned_text": draft["cleaned_text"],
            "preview_signature": signature,
        }

    @staticmethod
    def _import_fields(commit: bool) -> set[str]:
        fields = set(_IMPORT_PROPERTIES)
        if commit:
            fields.update({"request_id", "preview_signature", "confirm"})
        return fields

    def _recipe_import_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, self._import_fields(False))
        with self.store.read() as connection:
            candidate = self._import_candidate(connection, payload)
        return {
            "recipe_id": candidate["recipe_id"],
            "current_version": candidate["current_version"],
            "name": candidate["name"],
            "details": candidate["details"],
            "source_kind": candidate["source_kind"],
            "source_url": candidate["source_url"],
            "cleaned_text": candidate["cleaned_text"],
            "warnings": candidate["warnings"],
            "preview_signature": candidate["preview_signature"],
            "would_write": False,
        }

    def _recipe_import_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, self._import_fields(True))
        preview_signature = self._string(payload.get("preview_signature"), "preview_signature")
        if payload.get("confirm") is not True:
            raise BusinessError("INVALID_INPUT", "confirm must be true", {"field": "confirm"})
        now = self._now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            candidate = self._import_candidate(connection, payload)
            if candidate["preview_signature"] != preview_signature:
                raise BusinessError("STALE_RECIPE_PREVIEW", "recipe data changed after preview; preview again", {"recipe_id": candidate["recipe_id"]})
            recipe_id = candidate["recipe_id"] or uuid.uuid4().hex
            details_json = self._canonical_json(candidate["details"])
            if candidate["current_version"] is None:
                version = 1
                connection.execute(
                    "INSERT INTO recipes(recipe_id, name, details_json, version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (recipe_id, candidate["name"], details_json, version, now, now),
                )
            else:
                version = candidate["current_version"] + 1
                connection.execute(
                    "UPDATE recipes SET name = ?, details_json = ?, version = ?, updated_at = ? WHERE recipe_id = ?",
                    (candidate["name"], details_json, version, now, recipe_id),
                )
            self._record_recipe_version(
                connection,
                recipe_id,
                version,
                candidate["name"],
                details_json,
                candidate["source_text"],
                candidate["source_url"],
                candidate["source_kind"],
            )
            recipe = self._recipe(connection, recipe_id)
            recipe["source_text"] = candidate["source_text"]
            recipe["source_url"] = candidate["source_url"]
            recipe["source_kind"] = candidate["source_kind"]
            recipe["migration_baseline"] = False
            recipe["warnings"] = candidate["warnings"]
            return recipe

        return self._write("recipe_import_commit", payload, operation)

    def _record_recipe_version(
        self,
        connection: sqlite3.Connection,
        recipe_id: str,
        version: int,
        name: str,
        details_json: str,
        source_text: str | None = None,
        source_url: str | None = None,
        source_kind: str | None = None,
    ) -> dict[str, Any]:
        connection.execute(
            "INSERT INTO recipe_versions "
            "(recipe_id, version, name, details_json, source_text, source_url, source_kind, created_at, migration_baseline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (recipe_id, version, name, details_json, source_text, source_url, source_kind, self._now()),
        )
        return {"recipe_id": recipe_id, "version": version}

    def _recipe_version_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, {"recipe_id", "limit", "offset"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            self._recipe(connection, recipe_id)
            total = connection.execute("SELECT COUNT(*) FROM recipe_versions WHERE recipe_id = ?", (recipe_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT recipe_id, version, name, source_url, source_kind, created_at, migration_baseline "
                "FROM recipe_versions WHERE recipe_id = ? ORDER BY version DESC LIMIT ? OFFSET ?",
                (recipe_id, limit, offset),
            ).fetchall()
            items = [
                {**dict(row), "migration_baseline": bool(row["migration_baseline"])}
                for row in rows
            ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def _recipe_version_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, {"recipe_id", "version"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        version = self._integer(payload.get("version"), "version", minimum=1)
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT * FROM recipe_versions WHERE recipe_id = ? AND version = ?",
                (recipe_id, version),
            ).fetchone()
            if row is None:
                raise BusinessError("NOT_FOUND", "recipe version was not found", {"recipe_id": recipe_id, "version": version})
            return {
                "recipe_id": row["recipe_id"],
                "version": row["version"],
                "name": row["name"],
                "details": json.loads(row["details_json"]),
                "source_text": row["source_text"],
                "source_url": row["source_url"],
                "source_kind": row["source_kind"],
                "created_at": row["created_at"],
                "migration_baseline": bool(row["migration_baseline"]),
            }

    def _scaled_recipe(self, connection: sqlite3.Connection, recipe_id: str, target_servings: int) -> dict[str, Any]:
        target = self._integer(target_servings, "target_servings", minimum=1)
        row = connection.execute("SELECT version, details_json FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
        details = json.loads(row["details_json"])
        base_value = details.get("servings")
        base_servings = base_value if isinstance(base_value, int) and not isinstance(base_value, bool) and base_value > 0 else None
        names = details.get("ingredients", [])
        amounts = details.get("ingredient_amounts", [])
        ingredients = []
        if isinstance(names, list):
            for index, name_value in enumerate(names):
                if not isinstance(name_value, str):
                    continue
                amount = amounts[index] if isinstance(amounts, list) and index < len(amounts) and isinstance(amounts[index], dict) else None
                if amount is not None and amount.get("name") not in {None, name_value}:
                    amount = None
                raw_quantity = amount.get("quantity") if amount is not None else None
                original_quantity = None if raw_quantity is None else str(raw_quantity)
                unit_value = amount.get("unit") if amount is not None else None
                unit = unit_value if isinstance(unit_value, str) else None
                parsed_quantity = self._parse_decimal(raw_quantity)
                quantity_known = (
                    bool(amount and amount.get("quantity_known", parsed_quantity is not None))
                    and parsed_quantity is not None
                    and bool(unit and unit.strip())
                )
                scaled_quantity = None
                if quantity_known and base_servings is not None:
                    scaled_quantity = self._decimal_text(parsed_quantity * Decimal(target) / Decimal(base_servings))
                ingredients.append(
                    {
                        "name": name_value,
                        "original_quantity": original_quantity,
                        "scaled_quantity": scaled_quantity,
                        "unit": unit,
                        "quantity_known": quantity_known,
                    }
                )
        return {
            "recipe_id": recipe_id,
            "recipe_version": row["version"],
            "base_servings": base_servings,
            "target_servings": target,
            "ingredients": ingredients,
        }

    def _recipe_scale(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, {"recipe_id", "target_servings", "target_unit"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        target_servings = self._integer(payload.get("target_servings"), "target_servings", minimum=1)
        target_unit = self._string(payload.get("target_unit"), "target_unit", nullable=True)
        if target_unit is not None and target_unit not in _RECIPE_SCALE_UNITS:
            raise BusinessError("INVALID_INPUT", "target_unit must be a supported metric unit", {"field": "target_unit"})

        with self.store.read() as connection:
            result = self._scaled_recipe(connection, recipe_id, target_servings)
            result.update(self._kitchen_alerts(connection, self._status_date(None, "as_of")))
        result["target_unit"] = target_unit
        if target_unit is None:
            return result

        target_basis = _normalize_quantity("1", target_unit)
        for ingredient in result["ingredients"]:
            source_unit = ingredient["unit"]
            scaled_quantity = ingredient["scaled_quantity"]
            ingredient["converted_quantity"] = None
            ingredient["converted_unit"] = None
            ingredient["unit_conversion"] = {
                "requested_unit": target_unit,
                "source_unit": source_unit,
                "status": "needs_confirmation",
            }
            if scaled_quantity is None or source_unit is None or not source_unit.strip():
                continue

            source_quantity = _normalize_quantity(scaled_quantity, source_unit)
            source_basis = _normalize_quantity("1", source_unit)
            if source_quantity[1] not in {"g", "ml"}:
                ingredient["unit_conversion"]["status"] = "unit_conversion_unknown"
                continue
            if source_quantity[1] != target_basis[1]:
                ingredient["unit_conversion"]["status"] = "incompatible_dimension"
                continue

            ingredient["converted_quantity"] = self._decimal_text(source_quantity[0] / target_basis[0])
            ingredient["converted_unit"] = target_unit
            ingredient["unit_conversion"] = {
                "requested_unit": target_unit,
                "source_unit": source_unit,
                "status": "converted",
                "base_unit": source_quantity[1],
                "source_to_base_factor": self._decimal_text(source_basis[0]),
                "target_to_base_factor": self._decimal_text(target_basis[0]),
            }
        return result

    @staticmethod
    def _feedback_row(row: sqlite3.Row) -> dict[str, Any]:
        feedback = dict(row)
        feedback["seasoning_changes"] = json.loads(feedback.pop("seasoning_changes_json"))
        return feedback

    def _latest_recipe_feedback(
        self, connection: sqlite3.Connection, recipe_id: str, as_of: datetime | None = None
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT f.feedback_id, f.recipe_id, f.meal_id, f.purpose, f.feedback_kind, f.reason, "
            "f.seasoning_changes_json, f.recorded_at, f.expires_at "
            "FROM recipe_feedback AS f LEFT JOIN active_entries AS e ON e.id = f.meal_id "
            "WHERE f.recipe_id = ? AND (f.meal_id IS NULL OR e.id IS NOT NULL) "
            "ORDER BY f.recorded_at DESC, f.feedback_id DESC LIMIT 1",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        feedback = self._feedback_row(row)
        if feedback["feedback_kind"] == "temporarily_tired" and feedback["expires_at"] is not None:
            now = as_of.astimezone(timezone.utc) if as_of is not None else datetime.now(timezone.utc)
            if self._parse_datetime(feedback["expires_at"], "expires_at") <= now:
                return None
        feedback["seasoning_changes_json"] = self._canonical_json(feedback["seasoning_changes"])
        return feedback

    def _recipe_feedback_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, {"request_id", "recipe_id", "meal_id", "purpose", "feedback_kind", "reason", "seasoning_changes", "expires_at"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        meal_id = self._string(payload.get("meal_id"), "meal_id", nullable=True)
        purpose = self._string(payload.get("purpose"), "purpose")
        feedback_kind = self._string(payload.get("feedback_kind"), "feedback_kind")
        if feedback_kind not in _FEEDBACK_KINDS:
            raise BusinessError("INVALID_INPUT", "feedback_kind is unsupported", {"field": "feedback_kind"})
        reason = self._string(payload.get("reason"), "reason", nullable=True)
        changes_value = self._list(payload.get("seasoning_changes", []), "seasoning_changes")
        if len(changes_value) > 50:
            raise BusinessError("INVALID_INPUT", "seasoning_changes cannot exceed 50 items", {"field": "seasoning_changes"})
        changes = []
        for index, value in enumerate(changes_value):
            field = f"seasoning_changes[{index}]"
            change = self._dict(value, field)
            self._reject_fields(change, {"ingredient", "planned_quantity", "actual_quantity", "unit"}, field)
            actual_quantity = self._string(change.get("actual_quantity"), f"{field}.actual_quantity")
            changes.append(
                {
                    "ingredient": self._string(change.get("ingredient"), f"{field}.ingredient"),
                    "planned_quantity": self._string(change.get("planned_quantity"), f"{field}.planned_quantity", nullable=True),
                    "actual_quantity": actual_quantity,
                    "unit": self._string(change.get("unit"), f"{field}.unit", nullable=True),
                }
            )
        expires_at = self._string(payload.get("expires_at"), "expires_at", nullable=True)
        if expires_at is not None:
            expires_at = self._parse_datetime(expires_at, "expires_at").isoformat(timespec="seconds")
        if feedback_kind == "temporarily_tired" and expires_at is None:
            raise BusinessError("INVALID_INPUT", "temporarily_tired feedback requires expires_at", {"field": "expires_at"})

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            recipe = connection.execute("SELECT version FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
            if recipe is None:
                raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
            if meal_id is not None:
                meal = connection.execute("SELECT id, record_kind FROM entries WHERE id = ?", (meal_id,)).fetchone()
                if meal is None or meal["record_kind"] != "meal":
                    raise BusinessError("NOT_FOUND", "meal record was not found", {"id": meal_id})
            feedback_id = uuid.uuid4().hex
            recorded_at = self._now()
            changes_json = self._canonical_json(changes)
            connection.execute(
                "INSERT INTO recipe_feedback "
                "(feedback_id, recipe_id, recipe_version, meal_id, purpose, feedback_kind, reason, "
                "seasoning_changes_json, recorded_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feedback_id, recipe_id, recipe["version"], meal_id, purpose, feedback_kind, reason, changes_json, recorded_at, expires_at),
            )
            row = connection.execute("SELECT * FROM recipe_feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
            return self._feedback_row(row)

        return self._write("recipe_feedback_add", payload, operation)

    def _recipe_feedback_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_fields(payload, {"recipe_id", "limit", "offset"})
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            if connection.execute("SELECT 1 FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone() is None:
                raise BusinessError("NOT_FOUND", "recipe was not found", {"id": recipe_id})
            total = connection.execute("SELECT COUNT(*) FROM recipe_feedback WHERE recipe_id = ?", (recipe_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM recipe_feedback WHERE recipe_id = ? "
                "ORDER BY recorded_at DESC, feedback_id DESC LIMIT ? OFFSET ?",
                (recipe_id, limit, offset),
            ).fetchall()
            items = [self._feedback_row(row) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}


__all__ = ["HANDLERS", "KitchenRecipeMixin", "MIGRATION_SQL", "TOOL_SCHEMAS", "_normalize_quantity"]
