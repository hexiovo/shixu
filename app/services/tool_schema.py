from __future__ import annotations


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _tool(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": _object(properties, required),
    }


_string = {"type": "string"}
_nullable_string = {"type": ["string", "null"]}
_integer = {"type": "integer"}
_money_cents = {"type": ["integer", "null"], "minimum": 0}
_mode = {"type": "string", "enum": ["eat_out", "delivery", "home_cooking"]}
_date = {"type": "string", "format": "date"}
_request_id = {"type": "string", "minLength": 1}

_store = _object(
    {"name": _string, "branch": _nullable_string},
    ("name",),
)
_item = _object(
    {
        "name": _string,
        "quantity": {"type": ["number", "string", "null"]},
        "unit": _nullable_string,
        "notes": _nullable_string,
    },
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
        "duration_min": {"type": ["integer", "null"], "minimum": 0},
        "duration_max": {"type": ["integer", "null"], "minimum": 0},
    }
)
_cost_range = _object(
    {
        "min_cents": _money_cents,
        "max_cents": _money_cents,
        "source": {"type": "string", "enum": ["actual", "estimate", "unknown"]},
    }
)

MEAL_CREATE_SCHEMA = _tool(
    "meal_create",
    "Create one meal record with dishes, a whole-meal amount, detailed payments, and timing facts. Amounts are integer cents.",
    {
        "request_id": _request_id,
        "date": _date,
        "meal_type": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack", "other"]},
        "mode": _mode,
        "meal_amount_cents": _money_cents,
        "items": {"type": "array", "items": _item},
        "payments": {"type": "array", "items": _payment},
        "timing": _timing,
        "consumption_cost": _cost_range,
        "location": _nullable_string,
        "note": _nullable_string,
        "people_count": {"type": ["integer", "null"], "minimum": 1},
        "store": {"anyOf": [_store, {"type": "null"}]},
        "store_id": _nullable_string,
        "recommendation_source": _object({
            "source_type": {"enum": ["candidate", "recipe"]}, "source_id": _string,
        }, ("source_type", "source_id")),
    },
    ("request_id", "date", "meal_type", "mode"),
)


__all__ = [
    "_tool", "_object", "_string", "_nullable_string", "_integer", "_date",
    "_mode", "_request_id", "_money_cents", "MEAL_CREATE_SCHEMA",
]
