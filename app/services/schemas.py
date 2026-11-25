from __future__ import annotations

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
from .meal_history import TOOL_SCHEMAS as HISTORY_TOOL_SCHEMAS
from .meal_analytics import TOOL_SCHEMAS as MEAL_ANALYTICS_TOOL_SCHEMAS
from .recommendation_history import TOOL_SCHEMAS as RECOMMENDATION_TOOL_SCHEMAS
from .kitchen_recipe import TOOL_SCHEMAS as KITCHEN_RECIPE_TOOL_SCHEMAS
from .kitchen import TOOL_SCHEMAS as KITCHEN_TOOL_SCHEMAS
from .daybook import TOOL_SCHEMAS as DAYBOOK_TOOL_SCHEMAS
from .pricebook import TOOL_SCHEMAS as PRICEBOOK_TOOL_SCHEMAS
from .reminders import TOOL_SCHEMAS as REMINDER_TOOL_SCHEMAS
from .recipe_providers import TOOL_SCHEMAS as RECIPE_PROVIDER_TOOL_SCHEMAS
from .meal_options import TOOL_SCHEMAS as OPTION_TOOL_SCHEMAS
from .meal_corrections import TOOL_SCHEMAS as CORRECTION_TOOL_SCHEMAS
from .meal_planning import TOOL_SCHEMAS as PLANNING_TOOL_SCHEMAS
from .kitchen_operations import TOOL_SCHEMAS as KITCHEN_OPERATION_TOOL_SCHEMAS
from .meal_finance import TOOL_SCHEMAS as FINANCE_TOOL_SCHEMAS
from .local_data import TOOL_SCHEMAS as DATA_TOOL_SCHEMAS


_nullable_integer = {"type": ["integer", "null"]}
_minutes = {"type": ["integer", "null"], "minimum": 0}

_item = _object(
    {
        "name": _string,
        "quantity": {"type": ["number", "string", "null"]},
        "unit": _nullable_string,
        "notes": _nullable_string,
    },
    ("name",),
)

_store = _object(
    {"name": _string, "branch": _nullable_string},
    ("name",),
)

_payment = _object(
    {
        "kind": {
            "type": "string",
            "enum": ["meal", "delivery_fee", "packaging_fee", "discount", "milk_tea", "purchase", "other"],
        },
        "scope": {"type": "string", "enum": ["meal", "extra"]},
        "amount_cents": _money_cents,
        "amount_min_cents": _money_cents,
        "amount_max_cents": _money_cents,
        "source": {"type": "string", "enum": ["actual", "estimate", "unknown"]},
        "note": _nullable_string,
    },
    ("kind", "scope"),
)

_timing = _object(
    {
        "start_at": _nullable_string,
        "ready_at": _nullable_string,
        "ended_at": _nullable_string,
        "return_at": _nullable_string,
        "duration_min": _minutes,
        "duration_max": _minutes,
    }
)

_stage = {
    "type": "object",
    "properties": {
        "min_minutes": _minutes,
        "max_minutes": _minutes,
        "steps": {
            "type": "array",
            "items": _object({
                "min_minutes": {"type": "integer", "minimum": 0},
                "max_minutes": {"type": "integer", "minimum": 0},
                "concurrent_group": _string,
            }, ("min_minutes", "max_minutes")),
        },
    },
    "additionalProperties": False,
}
_cost_range = _object(
    {
        "min_cents": _money_cents,
        "max_cents": _money_cents,
        "source": {"type": "string", "enum": ["actual", "estimate", "unknown"]},
    }
)

_details = {
    "type": "object",
    "properties": {
        "location": _nullable_string,
        "ingredients": {"type": "array", "items": _string},
        "ingredients_known": {"type": "boolean"},
        "allergens": {"type": "array", "items": _string},
        "allergens_known": {"type": "boolean"},
        "utensils": {"type": "array", "items": _string},
        "shopping_required": {"type": "boolean"},
        "instructions": _nullable_string,
        "servings": _nullable_integer,
        "source": {"type": ["object", "string", "null"]},
        "time_profile": _object({
            "source": {"type": "string", "enum": ["actual", "estimate", "unknown"]},
            "origin_location": _nullable_string,
            "return_location": _nullable_string,
            "stages": {
                "type": "object",
                "properties": {name: _stage for name in ("outbound", "queue", "wait", "order_to_ready", "meal", "return", "shopping", "prep", "cook")},
                "additionalProperties": False,
            },
        }),
        "cost_profile": _object({
            "payment": _cost_range,
            "consumption": _cost_range,
            "people_count": {"type": "integer", "minimum": 1},
            "payment_coverage_people_count": {"type": "integer", "minimum": 1},
            "consumption_coverage_people_count": {"type": "integer", "minimum": 1},
            "personal_share": _object({"payment": _cost_range, "consumption": _cost_range}),
        }),
    },
    "additionalProperties": True,
}

TOOL_SCHEMAS = [
    MEAL_CREATE_SCHEMA,
    _tool(
        "meal_list",
        "List meal records only. Standalone milk tea is listed with milk_tea_list.",
        {
            "date_from": _date,
            "date_to": _date,
            "mode": _mode,
            "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "other"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0},
        },
    ),
    _tool(
        "meal_stats",
        "Read meal counts, common dishes and stores, last recorded meal times, and milk-tea purchase counts. date_from/date_to are optional inclusive bounds; omit both to query all history. dish and store add exact-item counts; top_limit limits only the ranking lists, not totals.",
        {
            "date_from": _date,
            "date_to": _date,
            "dish": _string,
            "store": _store,
            "top_limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
    ),
    _tool(
        "meal_update",
        "Update meal fields at scope meal, or one payment line at scope payment. Requires the current version.",
        {
            "request_id": _request_id,
            "meal_id": _string,
            "expected_version": {"type": "integer", "minimum": 1},
            "scope": {"type": "string", "enum": ["meal", "payment"]},
            "payment_id": _string,
            "changes": _object(
                {
                    "date": _date,
                    "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "other"]},
                    "mode": _mode,
                    "meal_amount_cents": _money_cents,
                    "items": {"type": "array", "items": _object({**_item["properties"], "item_id": _string}, ("name",))},
                    "location": _nullable_string,
                    "note": _nullable_string,
                    "timing": _timing,
                    "consumption_cost": _cost_range,
                    "people_count": _nullable_integer,
                    "store": {"anyOf": [_store, {"type": "null"}]},
                    "store_id": _nullable_string,
                    "kind": {"type": "string", "enum": ["meal", "delivery_fee", "packaging_fee", "discount", "milk_tea", "purchase", "other"]},
                    "scope": {"type": "string", "enum": ["meal", "extra"]},
                    "amount_cents": _money_cents,
                    "amount_min_cents": _money_cents,
                    "amount_max_cents": _money_cents,
                    "source": {"type": "string", "enum": ["actual", "estimate", "unknown"]},
                }
            ),
        },
        ("request_id", "meal_id", "expected_version", "scope", "changes"),
    ),
    _tool(
        "payment_add",
        "Add one payment line to a meal and increment its version.",
        {"request_id": _request_id, "meal_id": _string, "expected_version": {"type": "integer", "minimum": 1}, "payment": _payment},
        ("request_id", "meal_id", "expected_version", "payment"),
    ),
    _tool(
        "payment_remove",
        "Remove one payment line from a meal and increment its version.",
        {"request_id": _request_id, "meal_id": _string, "expected_version": {"type": "integer", "minimum": 1}, "payment_id": _string},
        ("request_id", "meal_id", "expected_version", "payment_id"),
    ),
    _tool(
        "milk_tea_create",
        "Create a standalone milk-tea purchase; it is not counted as a meal.",
        {"request_id": _request_id, "date": _date, "amount_cents": _money_cents, "note": _nullable_string, "location": _nullable_string},
        ("request_id", "date"),
    ),
    _tool(
        "milk_tea_list",
        "List standalone milk-tea purchases; these are not meal records.",
        {"date_from": _date, "date_to": _date, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}},
    ),
    _tool(
        "milk_tea_update",
        "Update one standalone milk-tea purchase by id and current version.",
        {"request_id": _request_id, "drink_id": _string, "expected_version": {"type": "integer", "minimum": 1}, "changes": _object({"date": _date, "amount_cents": _money_cents, "note": _nullable_string, "location": _nullable_string})},
        ("request_id", "drink_id", "expected_version", "changes"),
    ),
    _tool("request_status", "Look up the committed result for a request id before retrying an uncertain write.", {"request_id": _request_id}, ("request_id",)),
    _tool(
        "candidate_upsert",
        "Create or update a restaurant, delivery, or home-cooking candidate. Use details.dishes to store each dish of a multi-dish option. Details may also contain ingredients, allergens, time_profile, and cost_profile.",
        {"request_id": _request_id, "candidate_id": _nullable_string, "expected_version": _nullable_integer, "mode": _mode, "name": _string, "details": _details, "preference": {"type": "string", "enum": ["like", "neutral", "avoid"]}},
        ("request_id", "mode", "name"),
    ),
    _tool("candidate_list", "List saved candidates and their structured evidence.", {"mode": _mode, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    _tool(
        "recipe_upsert",
        "Create or update a manual recipe, preserving source, ingredients, utensils, and time/cost profiles.",
        {"request_id": _request_id, "recipe_id": _nullable_string, "expected_version": _nullable_integer, "name": _string, "details": _details},
        ("request_id", "name"),
    ),
    _tool("recipe_list", "List saved manual recipes.", {"limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}),
    _tool(
        "evidence_create",
        "Save a time or cost evidence record with its source. Time values use stage ranges; cost values use payment and consumption ranges.",
        {"request_id": _request_id, "subject_type": {"type": "string", "enum": ["candidate", "recipe", "location", "meal"]}, "subject_id": _nullable_string, "kind": {"type": "string", "enum": ["time", "cost"]}, "mode": _mode, "location": _nullable_string, "origin_location": _nullable_string, "return_location": _nullable_string, "value": {"type": "object"}, "source": _nullable_string},
        ("request_id", "subject_type", "kind", "value"),
    ),
    _tool("evidence_list", "List time and cost evidence by subject or mode.", {"subject_type": {"type": "string", "enum": ["candidate", "recipe", "location", "meal"]}, "subject_id": _string, "kind": {"type": "string", "enum": ["time", "cost"]}, "mode": _mode}),
    _tool(
        "preference_set",
        "Set a user preference as structured JSON.",
        {"request_id": _request_id, "key": _string, "value": {}},
        ("request_id", "key", "value"),
    ),
    _tool(
        "restriction_set",
        "Create or update a confirmed allergy or temporary food avoidance. Use enabled=false only for an explicit user action.",
        {"request_id": _request_id, "restriction_id": _nullable_string, "expected_version": _nullable_integer, "kind": {"type": "string", "enum": ["allergy", "temporary_avoidance"]}, "term": _string, "active_from": _nullable_string, "active_until": _nullable_string, "enabled": {"type": "boolean"}, "explicit_user_action": {"type": "boolean"}, "source": _nullable_string},
        ("request_id", "kind", "term"),
    ),
    _tool("restriction_list", "List confirmed restrictions and their active periods.", {"as_of": _date}),
    _tool(
        "recommend",
        "Compare candidates and recipes using time, cost, restrictions, history, and kitchen stock. Home-cooking results can include kitchen_alerts with expired batches and leftovers. Use ranking score to sort by the score.",
        {"now": {"type": "string", "format": "date-time"}, "deadline": {"type": "string", "format": "date-time"}, "time_target": {"type": "string", "enum": ["meal_start", "finished", "returned"]}, "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "other"]}, "repeat_lookback_days": {"type": "integer", "minimum": 1}, "location": _nullable_string, "return_location": _nullable_string, "budget_cents": _money_cents, "budget_basis": {"type": "string", "enum": ["payment", "consumption"]}, "budget_scope": {"type": "string", "enum": ["total", "personal_share"]}, "people_count": {"type": ["integer", "null"], "minimum": 1}, "available_ingredients": {"type": "array", "items": _string}, "available_utensils": {"type": "array", "items": _string}, "use_kitchen_stock": {"type": "boolean"}, "kitchen_location": _nullable_string, "occasion": _nullable_string, "price_region": _nullable_string, "ranking": {"enum": ["existing", "score"]}, "score_weights": {"type": "object"}, "mode": _mode, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
    ),
]

TOOL_SCHEMAS.extend(HISTORY_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(MEAL_ANALYTICS_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(RECOMMENDATION_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(KITCHEN_RECIPE_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(KITCHEN_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(DAYBOOK_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(PRICEBOOK_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(REMINDER_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(RECIPE_PROVIDER_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(OPTION_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(CORRECTION_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(PLANNING_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(KITCHEN_OPERATION_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(FINANCE_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(DATA_TOOL_SCHEMAS)

__all__ = ["TOOL_SCHEMAS"]
