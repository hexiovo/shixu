from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_HALF_UP
from typing import Any

from .errors import BusinessError
from .kitchen_recipe import _normalize_quantity
from .tool_schema import _date, _integer, _money_cents, _nullable_string, _request_id, _string, _tool


_ADD = {
    "request_id": _request_id, "ingredient": _string, "package_quantity": {"type": ["string", "number"]},
    "unit": _string, "price_cents": _money_cents, "region": _string, "recorded_on": _date, "source": _string,
}
_LIST = {"ingredient": _string, "region": _string, "as_of": _date, "limit": _integer, "offset": _integer}
_ESTIMATE = {
    "recipe_id": _string, "servings": {"type": "integer", "minimum": 1}, "region": _string, "as_of": _date,
    "use_stock": {"type": "boolean"}, "location": _nullable_string,
}
TOOL_SCHEMAS = [
    _tool("price_snapshot_add", "Save a dated ingredient package price with region and source. Quantity and price are for the whole package.", _ADD, tuple(_ADD)),
    _tool("price_snapshot_list", "List locally recorded grocery price snapshots by ingredient, region or cutoff date.", _LIST),
    _tool("recipe_price_estimate", "Estimate recipe consumption cost and cash needed for whole packages using dated local prices. Optionally deduct available stock.", _ESTIMATE, ("recipe_id", "servings", "region")),
]
HANDLERS = {
    "price_snapshot_add": "_price_snapshot_add",
    "price_snapshot_list": "_price_snapshot_list",
    "recipe_price_estimate": "_recipe_price_estimate",
}
MIGRATION_SQL = [
    """CREATE TABLE ingredient_price_snapshots (
        price_id TEXT PRIMARY KEY,
        ingredient TEXT NOT NULL,
        ingredient_key TEXT NOT NULL,
        package_quantity TEXT NOT NULL,
        unit TEXT NOT NULL,
        package_base TEXT NOT NULL,
        base_unit TEXT NOT NULL,
        price_cents INTEGER NOT NULL CHECK(price_cents>=0),
        region TEXT NOT NULL,
        recorded_on TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX ingredient_price_lookup
       ON ingredient_price_snapshots(ingredient_key, region, base_unit, recorded_on DESC, created_at DESC)""",
]


def _price_quantity(value: Any, unit: str) -> tuple[Decimal, str]:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError
        normalized = _normalize_quantity(number, unit)
        if normalized is None:
            raise ValueError
        return normalized
    except (DecimalException, ValueError) as exc:
        raise BusinessError("INVALID_INPUT", "A positive package quantity and unit are required") from exc


class PricebookMixin:
    def _price_snapshot_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_ADD))
        ingredient = self._string(payload.get("ingredient"), "ingredient")
        unit = self._string(payload.get("unit"), "unit")
        package_base, base_unit = _price_quantity(payload.get("package_quantity"), unit)
        price_cents = self._integer(payload.get("price_cents"), "price_cents", minimum=0)
        region = self._string(payload.get("region"), "region")
        recorded = date.fromisoformat(self._iso_date(payload.get("recorded_on"), "recorded_on")).isoformat()
        source = self._string(payload.get("source"), "source")

        def operation(connection):
            key = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO ingredient_price_snapshots(price_id, ingredient, ingredient_key, package_quantity,
                   unit, package_base, base_unit, price_cents, region, recorded_on, source, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, ingredient, self._ingredient_key(ingredient), str(payload["package_quantity"]), unit, str(package_base),
                 base_unit, price_cents, region, recorded, source, self._now()),
            )
            return dict(connection.execute("SELECT * FROM ingredient_price_snapshots WHERE price_id=?", (key,)).fetchone())

        return self._write("price_snapshot_add", payload, operation)

    def _price_snapshot_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_LIST))
        clauses = []
        params = []
        for key, expression in (("ingredient", "ingredient_key=?"), ("region", "region=?"), ("as_of", "recorded_on<=?")):
            if payload.get(key):
                value = self._string(payload[key], key)
                if key == "ingredient":
                    value = self._ingredient_key(value)
                elif key == "as_of":
                    value = date.fromisoformat(self._iso_date(value, key)).isoformat()
                clauses.append(expression)
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit, offset = self._page(payload)
        with self.store.read() as connection:
            rows = connection.execute(
                f"SELECT * FROM ingredient_price_snapshots{where} ORDER BY recorded_on DESC, created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def _recipe_price_estimate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._reject_extra_fields(payload, set(_ESTIMATE))
        recipe_id = self._string(payload.get("recipe_id"), "recipe_id")
        servings = self._integer(payload.get("servings"), "servings", minimum=1)
        region = self._string(payload.get("region"), "region")
        as_of = date.fromisoformat(self._iso_date(payload["as_of"], "as_of")).isoformat() if payload.get("as_of") else date.today().isoformat()
        location = self._string(payload.get("location"), "location", nullable=True)
        use_stock = payload.get("use_stock", False)
        if type(use_stock) is not bool:
            raise BusinessError("INVALID_INPUT", "use_stock must be a boolean")
        with self.store.read() as connection:
            inventory = self._available_kitchen_context(connection, as_of, location)["inventory"] if use_stock else None
            return self._estimate_recipe_prices(connection, recipe_id, servings, region, as_of, inventory, location)

    def _estimate_recipe_prices(self, connection, recipe_id, servings, region, as_of, inventory, location):
        estimates = []
        needs: dict[tuple[str, str], tuple[str, Decimal]] = {}
        recipe = self._scaled_recipe(connection, recipe_id, servings)
        for item in recipe["ingredients"]:
            normalized = _normalize_quantity(item["scaled_quantity"], item["unit"]) if item["quantity_known"] else None
            if normalized is None or normalized[0] < 0:
                estimates.append({"ingredient": item["name"], "status": "unknown_quantity", "payment_cents": None, "consumption_cents": None})
                continue
            quantity, unit = normalized
            key = (self._ingredient_key(item["name"]), unit)
            title, previous = needs.get(key, (item["name"], Decimal(0)))
            needs[key] = (title, previous + quantity)
        totals = self._estimate_ingredient_prices(connection, needs, estimates, region, as_of, inventory)
        return {
            "recipe_id": recipe_id, "recipe_version": recipe["recipe_version"], "servings": servings,
            "region": region, "as_of": as_of, "use_stock": inventory is not None, "location": location, "items": estimates, **totals,
        }

    def _estimate_ingredient_prices(self, connection, needs, estimates, region, as_of, inventory):
        stocks = {
            (self._ingredient_key(item["ingredient"]), item["unit"]): Decimal(str(item["quantity"]))
            for item in (inventory or [])
        }
        for (ingredient_key, unit), (name, quantity) in needs.items():
            price = connection.execute(
                """SELECT * FROM ingredient_price_snapshots WHERE ingredient_key=? AND region=? AND base_unit=? AND recorded_on<=?
                   ORDER BY recorded_on DESC, created_at DESC, rowid DESC LIMIT 1""",
                (ingredient_key, region, unit, as_of),
            ).fetchone()
            on_hand = min(quantity, stocks.get((ingredient_key, unit), Decimal(0)))
            remaining = quantity - on_hand
            estimate = {
                "ingredient": name, "required": str(quantity), "unit": unit, "stock_used": str(on_hand),
                "to_buy": str(remaining), "status": "estimated" if price else "unknown_price",
                "price": dict(price) if price else None, "packages_to_buy": None,
                "payment_cents": 0 if remaining == 0 else None, "consumption_cents": None,
            }
            if price:
                package = Decimal(price["package_base"])
                packs = int((remaining / package).to_integral_value(rounding=ROUND_CEILING))
                estimate["packages_to_buy"] = packs
                estimate["payment_cents"] = packs * price["price_cents"]
                estimate["consumption_cents"] = int(
                    (quantity * price["price_cents"] / package).to_integral_value(rounding=ROUND_HALF_UP)
                )
            estimates.append(estimate)
        totals = {}
        for field in ("payment_cents", "consumption_cents"):
            known = [line[field] for line in estimates if line[field] is not None]
            totals[field] = sum(known) if estimates and len(known) == len(estimates) else None
            totals[f"known_{field}"] = sum(known)
        return totals
