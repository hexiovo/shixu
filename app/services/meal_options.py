from __future__ import annotations

import itertools
import json
import uuid
from collections import Counter
from datetime import timedelta
from decimal import Decimal, DecimalException

from .errors import BusinessError
from .kitchen_recipe import _normalize_quantity
from .recommendation_score import recommendation_score, score_weights
from .tool_schema import _integer, _nullable_string, _object, _request_id, _string, _tool


_ENUM_MODE = {"enum": ["eat_out", "delivery", "home_cooking"]}
_ROLES = ("main", "side", "soup", "staple", "drink", "other")
_STRINGS = {"type": "array", "items": _string}
_CENTS = {"type": ["integer", "null"], "minimum": 0, "maximum": 100_000_000}
_RANGE = _object({"min": _integer, "max": _integer}, ("min", "max"))
_PICK = _object({"offer_id": _string, "quantity": _integer}, ("offer_id",))
_DISH = {"request_id": _request_id, "dish_id": _string, "expected_version": _integer, "name": _string, "aliases": _STRINGS}
_OFFER = {
    "request_id": _request_id, "offer_id": _string, "expected_version": _integer, "dish_id": _string,
    "mode": _ENUM_MODE, "store_id": _nullable_string, "role": {"enum": list(_ROLES)}, "portion": _nullable_string,
    "store": _object({"name": _string, "branch": _nullable_string}, ("name",)),
    "payment_cents": _CENTS, "consumption_cents": _CENTS, "ready_minutes": _RANGE, "finish_extra_minutes": _RANGE,
    "return_extra_minutes": _RANGE, "ingredients": _STRINGS, "allergens": _STRINGS,
    "ingredient_amounts": {"type": "array", "items": _object({
        "name": _string, "quantity": {"type": ["number", "string"]}, "unit": _string,
    }, ("name", "quantity", "unit"))},
    "utensils": _STRINGS, "shopping_included": {"type": "boolean"},
    "origin_location": _nullable_string, "return_location": _nullable_string,
    "ingredients_known": {"type": "boolean"}, "allergens_known": {"type": "boolean"},
    "active": {"type": "boolean"}, "source": _nullable_string, "order_note": _nullable_string,
}
_CONSTRAINTS = _object({
    "now": _string, "deadline": _string, "time_target": {"enum": ["meal_start", "finished", "returned"]},
    "budget_cents": _CENTS, "budget_basis": {"enum": ["payment", "consumption"]},
    "budget_scope": {"enum": ["total", "personal_share"]},
    "people_count": _integer, "mode": _ENUM_MODE, "meal_type": _string, "location": _string,
    "return_location": _string, "occasion": _string, "use_kitchen_stock": {"type": "boolean"},
    "repeat_lookback_days": _integer,
    "price_region": _string, "available_ingredients": _STRINGS, "available_utensils": _STRINGS,
    "score_weights": {"type": "object"},
})
_OPTION = {
    "kind": {"enum": ["whole", "single", "complement"]}, "query": _string, "store_id": _string,
    "locked": {"type": "array", "items": _PICK}, "selection": {"type": "array", "items": _PICK},
    "slots": {"type": "array", "items": _object({"role": {"enum": list(_ROLES)}, "count": _integer}, ("role",))},
    "addition_budget_cents": _CENTS, "constraints": _CONSTRAINTS, "limit": _integer,
}
TOOL_SCHEMAS = [
    _tool("dish_catalog_set", "Create or edit a canonical dish and aliases.", _DISH, ("request_id", "name")),
    _tool("dish_catalog_search", "Find canonical dishes, their aliases and recorded meal frequency.", {"query": _string, "limit": _integer, "offset": _integer}),
    _tool("dish_offer_set", "Save a separately orderable dish with source, venue, portion price, timing and confirmed ingredients.", _OFFER, ("request_id", "dish_id", "mode")),
    _tool("meal_options", "Compare whole meals, look up one dish, or add sides while keeping locked choices. Home-cooking results can include kitchen_alerts for expired kitchen stock.", _OPTION, ("kind",)),
    _tool("meal_options_compare", "Compare the same request under several explicit budgets, time limits or weights.", {
        "options": _object(_OPTION, ("kind",)), "scenarios": {"type": "array", "items": _CONSTRAINTS}
    }, ("options", "scenarios")),
    _tool("candidate_from_meal", "Preview or save a candidate from a recorded meal. Only confirmed actual amounts can be copied explicitly.", {
        "meal_id": _string, "expected_version": _integer, "name": _string, "include_actual_price": {"type": "boolean"},
        "request_id": _request_id, "confirm": {"type": "boolean"}
    }, ("meal_id", "expected_version")),
]
HANDLERS = {tool["name"]: "_" + tool["name"] for tool in TOOL_SCHEMAS}
MIGRATION_SQL = [
    """CREATE TABLE catalog_dishes(id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL)""",
    """CREATE TABLE catalog_names(name_key TEXT PRIMARY KEY, name TEXT NOT NULL,
       dish_id TEXT NOT NULL REFERENCES catalog_dishes(id) ON DELETE CASCADE)""",
    """CREATE TABLE dish_offers(id TEXT PRIMARY KEY, dish_id TEXT NOT NULL REFERENCES catalog_dishes(id),
       mode TEXT NOT NULL, store_id TEXT REFERENCES stores(store_id), details TEXT NOT NULL,
       version INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE INDEX dish_offers_dish ON dish_offers(dish_id, mode, store_id)""",
]


class MealOptionsMixin:
    def _catalog_dish(self, connection, dish_id):
        row = connection.execute("SELECT * FROM catalog_dishes WHERE id=?", (dish_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "Dish not found", {"dish_id": dish_id})
        aliases = connection.execute(
            "SELECT name FROM catalog_names WHERE dish_id=? AND name_key<>? ORDER BY name",
            (dish_id, self._ingredient_key(row["name"])),
        ).fetchall()
        return {**dict(row), "aliases": [alias["name"] for alias in aliases]}

    def _catalog_version(self, payload, identity, row):
        expected = self._integer(payload.get("expected_version"), "expected_version", nullable=True, minimum=1)
        if row is None and expected is not None:
            raise BusinessError("NOT_FOUND", "Catalog record not found", {"id": identity})
        if row is not None:
            if expected is None:
                raise BusinessError("INVALID_INPUT", "Editing requires expected_version")
            self._check_version(row, identity, expected)
        return 1 if row is None else row["version"] + 1

    def _dish_catalog_set(self, payload):
        self._reject_extra_fields(payload, set(_DISH))
        name = self._string(payload.get("name"), "name")
        dish_id = self._string(payload.get("dish_id"), "dish_id", nullable=True) or uuid.uuid4().hex
        aliases = self._list(payload.get("aliases"), "aliases", default=[])
        if len(aliases) > 30:
            raise BusinessError("INVALID_INPUT", "Use at most 30 aliases")
        names = {}
        for spelling in [name, *(self._string(alias, "alias") for alias in aliases)]:
            names.setdefault(self._ingredient_key(spelling), spelling)

        def operation(connection):
            row = connection.execute("SELECT * FROM catalog_dishes WHERE id=?", (dish_id,)).fetchone()
            version = self._catalog_version(payload, dish_id, row)
            if row and "aliases" not in payload:
                for old in connection.execute("SELECT name, name_key FROM catalog_names WHERE dish_id=?", (dish_id,)):
                    names.setdefault(old["name_key"], old["name"])
            for key in names:
                owner = connection.execute("SELECT dish_id FROM catalog_names WHERE name_key=?", (key,)).fetchone()
                if owner and owner["dish_id"] != dish_id:
                    raise BusinessError("DISH_NAME_CONFLICT", "This dish name or alias already belongs to another dish", {"dish_id": owner["dish_id"], "name": names[key]})
            connection.execute(
                "INSERT INTO catalog_dishes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, version=excluded.version",
                (dish_id, name, version),
            )
            connection.execute("DELETE FROM catalog_names WHERE dish_id=?", (dish_id,))
            connection.executemany("INSERT INTO catalog_names VALUES(?,?,?)", [(key, spelling, dish_id) for key, spelling in names.items()])
            return self._catalog_dish(connection, dish_id)

        return self._write("dish_catalog_set", payload, operation)

    def _dish_catalog_search(self, payload):
        self._reject_extra_fields(payload, {"query", "limit", "offset"})
        query = self._string(payload.get("query"), "query", nullable=True)
        limit, offset = self._page(payload)
        key = self._ingredient_key(query) if query else ""
        with self.store.read() as connection:
            ids = connection.execute(
                """SELECT d.id FROM catalog_dishes d JOIN catalog_names n ON n.dish_id=d.id
                   WHERE instr(n.name_key, ?) > 0 GROUP BY d.id
                   ORDER BY MAX(n.name_key=?) DESC, d.name, d.id LIMIT ? OFFSET ?""",
                (key, key, limit, offset),
            ).fetchall()
            meals = connection.execute(
                """SELECT i.name,i.entry_id,e.calendar_date,p.dish_id FROM items i
                   LEFT JOIN meal_item_profiles p USING(item_id)
                   JOIN active_entries e ON e.id=i.entry_id WHERE e.record_kind='meal'"""
            ).fetchall()
            by_name = {}
            by_id = {}
            for meal in meals:
                by_name.setdefault(self._ingredient_key(meal["name"]), {})[meal["entry_id"]] = meal["calendar_date"]
                if meal["dish_id"]:
                    by_id.setdefault(meal["dish_id"], {})[meal["entry_id"]] = meal["calendar_date"]
            items = []
            for row in ids:
                dish = self._catalog_dish(connection, row["id"])
                hits = dict(by_id.get(dish["id"], {}))
                for name in [dish["name"], *dish["aliases"]]:
                    hits.update(by_name.get(self._ingredient_key(name), {}))
                items.append({**dish, "meal_count": len(hits), "last_date": max(hits.values(), default=None)})
        return {"items": items, "limit": limit, "offset": offset}

    def _option_range(self, value, field):
        pair = self._dict(value, field)
        self._reject_extra_fields(pair, {"min", "max"}, field)
        low = self._integer(pair.get("min"), field + ".min", minimum=0)
        high = self._integer(pair.get("max"), field + ".max", minimum=low)
        if high > 10_080:
            raise BusinessError("INVALID_INPUT", "A duration cannot exceed seven days")
        return {"min": low, "max": high}

    def _dish_offer_set(self, payload):
        self._reject_extra_fields(payload, set(_OFFER))
        offer_id = self._string(payload.get("offer_id"), "offer_id", nullable=True) or uuid.uuid4().hex
        dish_id = self._string(payload.get("dish_id"), "dish_id")
        mode = self._recommendation_enum(payload.get("mode"), "mode", set(_ENUM_MODE["enum"]))
        store_id = self._string(payload.get("store_id"), "store_id", nullable=True)
        if "store" in payload and "store_id" in payload:
            raise BusinessError("INVALID_INPUT", "Use store or store_id")
        store = self._store_fields(payload["store"], "store") if "store" in payload else None
        changes = {key: payload[key] for key in set(_OFFER) - {"request_id", "offer_id", "expected_version", "dish_id", "mode", "store_id", "store"} if key in payload}
        for key, value in changes.items():
            if key in {"payment_cents", "consumption_cents"}:
                changes[key] = self._integer(value, key, nullable=True, minimum=0)
                if changes[key] is not None and changes[key] > 100_000_000:
                    raise BusinessError("INVALID_INPUT", "Amount is outside the supported range")
            elif key.endswith("_minutes"):
                changes[key] = self._option_range(value, key)
            elif key in {"ingredients", "allergens", "utensils"}:
                changes[key] = [self._string(entry, key) for entry in self._list(value, key)]
            elif key == "ingredient_amounts":
                rows = self._list(value, key)
                if len(rows) > 50:
                    raise BusinessError("INVALID_INPUT", "Provide at most 50 ingredient amounts")
                amounts = []
                for raw in rows:
                    item = self._dict(raw, "ingredient_amount")
                    self._reject_extra_fields(item, {"name", "quantity", "unit"}, "ingredient_amount")
                    name = self._string(item.get("name"), "name")
                    unit = self._string(item.get("unit"), "unit")
                    try:
                        normalized = _normalize_quantity(item.get("quantity"), unit)
                    except DecimalException:
                        normalized = None
                    if normalized is None or not 0 < normalized[0] <= 10**9:
                        raise BusinessError("INVALID_INPUT", "Ingredient amount must be a supported positive quantity")
                    amounts.append({"name": name, "quantity": str(normalized[0]), "unit": normalized[1]})
                changes[key] = amounts
            elif key in {"ingredients_known", "allergens_known", "active", "shopping_included"}:
                if type(value) is not bool:
                    raise BusinessError("INVALID_INPUT", f"{key} must be a boolean")
            elif key == "role":
                changes[key] = self._recommendation_enum(value, key, set(_ROLES))
            else:
                changes[key] = self._string(value, key, nullable=True)

        def operation(connection):
            self._catalog_dish(connection, dish_id)
            row = connection.execute("SELECT * FROM dish_offers WHERE id=?", (offer_id,)).fetchone()
            version = self._catalog_version(payload, offer_id, row)
            old_venue = self._canonical_store_id(connection, row["store_id"]) if row is not None and row["store_id"] else None
            venue = self._store_id_input(connection, store, store_id, "store") if {"store", "store_id"} & payload.keys() else old_venue
            details = {**(json.loads(row["details"]) if row else {"role": "other", "active": True}), **changes}
            if row and (row["dish_id"], row["mode"], old_venue) != (dish_id, mode, venue):
                raise BusinessError("INVALID_INPUT", "Create a new offer for a different dish, mode or venue")
            connection.execute(
                """INSERT INTO dish_offers VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   dish_id=excluded.dish_id,mode=excluded.mode,store_id=excluded.store_id,details=excluded.details,
                   version=excluded.version,updated_at=excluded.updated_at""",
                (offer_id, dish_id, mode, venue, self._json(details), version, self._now()),
            )
            return {"id": offer_id, "dish_id": dish_id, "store_id": venue, "mode": mode, "version": version, "details": details}

        return self._write("dish_offer_set", payload, operation)

    def _offer_picks(self, value, field, offers):
        picks = self._list(value, field, default=[])
        if len(picks) > 12:
            raise BusinessError("INVALID_INPUT", "Use at most 12 distinct picks")
        output = []
        for raw in picks:
            pick = self._dict(raw, field)
            self._reject_extra_fields(pick, {"offer_id", "quantity"}, field)
            offer_id = self._string(pick.get("offer_id"), "offer_id")
            count = self._integer(pick.get("quantity", 1), "quantity", minimum=1)
            if count > 100:
                raise BusinessError("INVALID_INPUT", "A pick supports up to 100 portions")
            if offer_id not in offers:
                raise BusinessError("NOT_FOUND", "Active offer not found", {"offer_id": offer_id})
            output.append({**offers[offer_id], "quantity": count})
        if len({item["id"] for item in output}) != len(output):
            raise BusinessError("INVALID_INPUT", "Use one pick per offer and set its quantity")
        return output

    @staticmethod
    def _offer_venue(pick):
        return pick["mode"], pick["store_id"] or (None if pick["mode"] == "home_cooking" else pick["id"])

    @staticmethod
    def _dish_sets(group, slots, search):
        if not slots:
            yield ()
            return
        by_role = {role: [] for role, _ in slots}
        for offer in group:
            role = offer["details"]["role"]
            if role in by_role:
                by_role[role].append(offer)
        if any(len(by_role[role]) < count for role, count in slots):
            return

        def extend(index, picks):
            if index == len(slots):
                yield tuple(picks)
                return
            role, count = slots[index]
            for selected in itertools.combinations(by_role[role], count):
                if search["visited"] == 4000:
                    search["bounded"] = True
                    return
                search["visited"] += 1
                yield from extend(index + 1, (*picks, *selected))
                if search["bounded"]:
                    return

        yield from extend(0, ())

    def _catalog_kitchen(self, connection, picks, settings):
        home = [pick for pick in picks if pick["mode"] == "home_cooking"]
        inventory = settings["inventory"]
        shopping_region = settings["price_region"]
        explicit = settings.get("available_ingredients")
        utensils = settings.get("available_utensils")
        if not home or not (inventory is not None or shopping_region or explicit is not None or utensils is not None):
            return [], [], {}

        findings = []
        needs = {}
        unknown = {}
        for pick in home:
            details = pick["details"]
            raw = details.get("ingredient_amounts", [])
            documented = {}
            for item in raw:
                key = self._ingredient_key(item["name"]), item["unit"]
                name, total = needs.get(key, (item["name"], Decimal(0)))
                needs[key] = (name, total + Decimal(item["quantity"]) * pick["quantity"])
                documented[key[0]] = item["name"]
            base = details.get("ingredients", [])
            ingredient_names = dict(documented)
            ingredient_names.update({self._ingredient_key(name): name for name in base})
            if explicit is not None:
                provided = {self._ingredient_key(name) for name in explicit}
                findings.extend(f"{pick['name']}: available ingredient: {name}" for key, name in ingredient_names.items() if key not in provided)
            if utensils is not None:
                findings.extend(f"{pick['name']}: required utensil: {name}"
                                for name in self._missing_list(details.get("utensils"), utensils))
            if not (details.get("ingredients_known") and ingredient_names):
                unknown[("dish", pick["id"])] = pick["name"]
            unknown.update({("ingredient", key): name for key, name in ingredient_names.items() if key not in documented})

        if inventory is None and not shopping_region:
            if explicit is not None:
                findings.extend(f"{pick['name']}: ingredient list needs verification" for pick in home if not pick["details"].get("ingredients_known"))
            return findings, [], {}
        if explicit is not None and inventory is not None:
            provided = {self._ingredient_key(name) for name in explicit}
            inventory = [item for item in inventory if self._ingredient_key(item["ingredient"]) in provided]
        estimates = [
            {"ingredient": name, "status": "unknown_quantity", "payment_cents": None, "consumption_cents": None} for name in unknown.values()
        ]
        totals = self._estimate_ingredient_prices(
            connection, needs, estimates, shopping_region, settings["now"].date().isoformat(), inventory
        )
        if inventory is not None:
            findings.extend(f"{name}: stock quantity or ingredients unknown" for name in unknown.values())
            if any(not pick["details"].get("shopping_included") for pick in home):
                findings.extend(f"{item['ingredient']}: shopping duration needs verification" for item in estimates
                                if item.get("to_buy") is not None and Decimal(item["to_buy"]) > 0)
        return findings, estimates, totals

    def _offer_patterns(self, connection, as_of, days):
        cutoff = (as_of - timedelta(days=min(days - 1, as_of.toordinal() - 1))).isoformat()
        rows = connection.execute(
            """SELECT i.name,i.entry_id,e.mode,e.calendar_date,m.store_id,p.dish_id,p.rating
               FROM items i JOIN active_entries e ON i.entry_id=e.id LEFT JOIN meal_item_profiles p USING(item_id)
               LEFT JOIN meal_store_identity m ON m.entry_id=e.id
               WHERE e.record_kind='meal' AND e.calendar_date<=? AND (e.calendar_date>=? OR p.rating IS NOT NULL)
               ORDER BY e.calendar_date DESC,e.updated_at DESC,i.rowid DESC""",
            (as_of.isoformat(), cutoff),
        ).fetchall()
        names = {row["name_key"]: row["dish_id"] for row in connection.execute("SELECT name_key,dish_id FROM catalog_names")}
        occurrences = {}
        ratings = {}
        for row in rows:
            dish_id = row["dish_id"] or names.get(self._ingredient_key(row["name"]))
            if dish_id is None:
                continue
            if row["calendar_date"] >= cutoff:
                occurrences.setdefault(dish_id, set()).add(row["entry_id"])
            if row["rating"] is not None:
                ratings.setdefault((dish_id, row["mode"], row["store_id"]), row["rating"])
        return occurrences, ratings

    def _offer_option(self, connection, picks, locked_ids, settings, add_budget):
        now = settings["now"]
        deadline = settings["deadline"]
        target = settings["time_target"]
        budget = settings["budget_cents"]
        basis = settings["budget_basis"]
        scope = settings["budget_scope"]
        requested_occasion = settings["occasion"]
        count = settings["people_count"]
        restrictions = settings["restrictions"]
        missing = []
        blocked = False
        route_gap = False
        for pick in picks:
            details = pick["details"].copy()
            ingredient_amounts = details.get("ingredient_amounts", [])
            if "ingredients" in details or ingredient_amounts:
                details["ingredients"] = [*details.get("ingredients", []),
                                           *(item["name"] for item in ingredient_amounts)]
            status, reasons = self._restriction_status(details, restrictions)
            blocked |= status == "excluded"
            missing.extend(reasons)
            for requested, key in (("location", "origin_location"), ("return_location", "return_location")):
                if settings.get(requested) and (requested == "location" or target == "returned") and details.get(key) != settings[requested]:
                    missing.append(f"{pick['name']}: timing has no matching {requested}")
                    route_gap = True
            if requested_occasion:
                occasion = self._read_candidate_occasion(connection, {"details": {"store_id": pick["store_id"]}, "mode": pick["mode"]}, requested_occasion)
                blocked |= occasion["status"] == "not_listed"
                if occasion["status"] != "matched":
                    missing.append(f"{pick['name']}: occasion {occasion['status']}")
        amounts = {}
        known_subtotal = {}
        kitchen_missing, kitchen_lines, kitchen_totals = self._catalog_kitchen(connection, picks, settings)
        missing.extend(kitchen_missing)
        for kind in ("payment", "consumption"):
            values = [pick["details"].get(f"{kind}_cents") for pick in picks]
            known_subtotal[kind] = sum(value * pick["quantity"] for value, pick in zip(values, picks) if value is not None)
            estimate = None
            if all(value is not None for value in values):
                estimate = known_subtotal[kind]
            elif all(value is None for value in values) and all(pick["mode"] == "home_cooking" for pick in picks):
                estimate = kitchen_totals.get(f"{kind}_cents")
                known_subtotal[kind] = kitchen_totals.get(f"known_{kind}_cents", 0)
            if estimate is not None:
                amounts[kind] = {"min_cents": estimate, "max_cents": estimate, "source": "estimate"}
                amounts[f"{kind}_coverage_people_count"] = count
        cost = self._money_result(amounts, budget, basis, scope, count, {})
        if budget is not None and scope == "total" and known_subtotal[basis] > budget:
            blocked = True
            if cost["cents"] is None:
                missing.append("known subtotal already exceeds the meal budget")
        additions = [pick for pick in picks if pick["id"] not in locked_ids]
        additional_costs = [pick["details"].get("payment_cents") for pick in additions]
        known_additional = sum(value * pick["quantity"] for value, pick in zip(additional_costs, additions) if value is not None)
        additional = known_additional if all(value is not None for value in additional_costs) else None
        if additional is None and len(additions) == len(picks) and cost["payment"]["cents"] is not None:
            additional = cost["payment"]["cents"]["max"]
            known_additional = additional
        elif additional is None and add_budget is not None and all(pick["details"].get("payment_cents") is None for pick in picks):
            locked = [pick for pick in picks if pick["id"] in locked_ids]
            if locked and kitchen_totals.get("payment_cents") is not None and all(pick["mode"] == "home_cooking" for pick in picks):
                _, _, base = self._catalog_kitchen(connection, locked, settings)
                if base.get("payment_cents") is not None:
                    additional = kitchen_totals["payment_cents"] - base["payment_cents"]
                    known_additional = additional
        if add_budget is not None:
            blocked |= known_additional > add_budget
            if additional is None or known_additional > add_budget:
                missing.append("addition budget exceeded" if known_additional > add_budget else "addition price unknown")
        time_ranges = {}
        starts = [pick["details"].get("ready_minutes") for pick in picks]
        extra = {"finished": "finish_extra_minutes", "returned": "return_extra_minutes"}
        # A shared route or parallel kitchen schedule is unverified for a combination.
        # For one portion use the exact recorded interval. For multiple preparations the
        # lower bound assumes full overlap and the upper bound allows sequential work.
        if all(starts) and not route_gap:
            time_ranges["meal_start"] = (max(part["min"] for part in starts), sum(
                part["max"] * pick["quantity"] for part, pick in zip(starts, picks)
            ))
        for phase, previous in (("finished", "meal_start"), ("returned", "finished")):
            intervals = [pick["details"].get(extra[phase]) for pick in picks]
            if previous in time_ranges and all(intervals):
                early, late = time_ranges[previous]
                time_ranges[phase] = (early + max(i["min"] for i in intervals), late + sum(i["max"] for i in intervals))

        def window(phase):
            value = time_ranges.get(phase)
            if value is None:
                return None
            return {"earliest": (now + timedelta(minutes=value[0])).isoformat(), "latest": (now + timedelta(minutes=value[1])).isoformat()}

        interval = time_ranges.get(target)
        cap = (deadline - now).total_seconds() // 60 if deadline else None
        timed = "needs_verification"
        if interval is None:
            missing.append(f"{target} duration unknown for at least one dish")
            if cap is not None and not route_gap and any(part and part["min"] > cap for part in starts):
                timed = "expected_exceeds"
                missing.append("at least one dish is already too late")
        elif cap is None:
            timed = "known"
        elif interval[1] <= cap:
            timed = "expected_meets"
        elif interval[0] > cap:
            timed = "expected_exceeds"
            missing.append("time constraint exceeded")
        else:
            missing.append("timing range overlaps deadline")
        if cost["status"] == "expected_exceeds":
            missing.append("budget exceeded")
        missing.extend(cost["missing"])
        time_result = {
            "status": timed, "deadline_minutes": cap,
            "minutes": {"min": interval[0], "max": interval[1]} if interval else None,
            "start_at": window("meal_start"), "finish_at": window("finished"), "return_at": window("returned"),
            "source": "catalog_estimate" if interval else "unknown", "missing": [target] if interval is None else [],
        }
        status = "excluded" if blocked or "expected_exceeds" in (timed, cost["status"]) else (
            "needs_verification" if missing or cost["status"] == "needs_verification" else "eligible"
        )
        result = {
            "name": "、".join(pick["name"] for pick in picks), "mode": picks[0]["mode"],
            "modes": list(dict.fromkeys(pick["mode"] for pick in picks)),
            "status": status, "cost": cost, "time": time_result,
            "dishes": [{**pick, "locked": pick["id"] in locked_ids} for pick in picks],
            "additional_payment_cents": additional, "missing": list(dict.fromkeys(missing)), "preference": "neutral",
            "known_subtotal_cents": known_subtotal, "known_additional_payment_cents": known_additional,
            "ingredient_estimates": kitchen_lines,
        }
        occurred, ratings = settings["history"]
        visits = set().union(*(occurred.get(pick["dish_id"], set()) for pick in picks))
        rated = [ratings[(pick["dish_id"], pick["mode"], pick["store_id"])] for pick in picks
                 if (pick["dish_id"], pick["mode"], pick["store_id"]) in ratings]
        history = {"recent_meal_count": len(visits), "rated_dishes": len(rated),
                   "catalog_rating": sum(rated) / len(rated) if rated else None}
        result["catalog_history"] = history
        result["recommendation_score"] = recommendation_score(
            result, budget, {"preference": {}, "recipe_feedback": None, "skips": [], **history}, settings["score_weights"]
        )
        return result

    def _meal_options(self, payload):
        self._reject_extra_fields(payload, set(_OPTION))
        kind = self._recommendation_enum(payload.get("kind"), "kind", {"whole", "single", "complement"})
        constraints = self._dict(payload.get("constraints"), "constraints", nullable=True)
        self._reject_extra_fields(constraints, set(_CONSTRAINTS["properties"]), "constraints")
        query = self._string(payload.get("query"), "query", nullable=True)
        store_id = self._string(payload.get("store_id"), "store_id", nullable=True)
        add_budget = self._integer(payload.get("addition_budget_cents"), "addition_budget_cents", nullable=True, minimum=0)
        limit, _ = self._page(payload)
        with self.store.read() as connection:
            settings, defaults_used = self._recommendation_defaults(connection, constraints)
            for key in ("now", "deadline"):
                value = self._string(settings.get(key), key, nullable=True)
                settings[key] = self._parse_now(value) if value is not None or key == "now" else None
            for key in ("available_ingredients", "available_utensils"):
                if key in settings:
                    settings[key] = [self._string(item, key) for item in self._list(settings[key], key)]
            use_stock = settings.get("use_kitchen_stock", False)
            self._analytics_meal_type(settings.get("meal_type"))
            if type(use_stock) is not bool:
                raise BusinessError("INVALID_INPUT", "use_kitchen_stock must be a boolean")
            as_of = settings["now"].date().isoformat()
            lookback = self._integer(settings.get("repeat_lookback_days", 30), "repeat_lookback_days", minimum=1)
            if lookback > 3650:
                raise BusinessError("INVALID_INPUT", "Use a repeat lookback of up to 3650 days")
            settings.update(
                time_target=self._recommendation_enum(settings.get("time_target", "meal_start"), "time_target", {"meal_start", "finished", "returned"}),
                budget_cents=self._integer(settings.get("budget_cents"), "budget_cents", nullable=True, minimum=0),
                budget_basis=self._recommendation_enum(settings.get("budget_basis", "payment"), "budget_basis", {"payment", "consumption"}),
                budget_scope=self._recommendation_enum(settings.get("budget_scope", "total"), "budget_scope", {"total", "personal_share"}),
                people_count=self._integer(settings.get("people_count", 1), "people_count", minimum=1),
                occasion=self._string(settings.get("occasion"), "occasion", nullable=True),
                price_region=self._string(settings.get("price_region"), "price_region", nullable=True),
                restrictions=self._active_restrictions(connection, as_of),
                inventory=self._available_kitchen_context(connection, as_of)["inventory"] if use_stock else None,
                history=self._offer_patterns(connection, settings["now"].date(), lookback),
                score_weights=score_weights(settings.get("score_weights")),
            )
            offer_rows = connection.execute(
                """SELECT o.*, d.name FROM dish_offers o JOIN catalog_dishes d ON d.id=o.dish_id
                   ORDER BY o.updated_at DESC, o.id"""
            ).fetchall()
            offers = {}
            for row in offer_rows:
                entry = dict(row)
                if entry["store_id"]:
                    entry["store_id"] = self._canonical_store_id(connection, entry["store_id"])
                entry["details"] = json.loads(entry["details"])
                if entry["details"].get("active", True):
                    offers[row["id"]] = entry
            locked = self._offer_picks(payload.get("locked"), "locked", offers)
            selection = self._offer_picks(payload.get("selection"), "selection", offers)
            locked_ids = {item["id"] for item in locked}
            if locked_ids.intersection(item["id"] for item in selection):
                raise BusinessError("INVALID_INPUT", "Locked dishes are already included in the meal")
            if kind == "complement" and not locked:
                raise BusinessError("INVALID_INPUT", "Complement requires at least one locked dish")
            if kind == "single" and (locked or len(selection) > 1 or payload.get("slots")):
                raise BusinessError("INVALID_INPUT", "Single lookup accepts one selected dish or a query")
            dish_ids = None
            if query:
                dish_ids = {row["dish_id"] for row in connection.execute(
                    "SELECT DISTINCT dish_id FROM catalog_names WHERE instr(name_key,?)>0", (self._ingredient_key(query),),
                )}
            canonical = self._store_profile_identity(connection, store_id)["store_id"] if store_id else None
            mode = self._recommendation_enum(settings.get("mode"), "mode", set(_ENUM_MODE["enum"]), nullable=True)
            available = [
                {**offer, "quantity": 1} for offer in offers.values()
                if offer["id"] not in locked_ids and (kind == "whole" or dish_ids is None or offer["dish_id"] in dish_ids)
                and (mode is None or offer["mode"] == mode)
                and (canonical is None or (offer["store_id"] and self._canonical_store_id(connection, offer["store_id"]) == canonical))
            ]
            if mode is not None and any(offer["mode"] != mode for offer in [*locked, *selection]):
                raise BusinessError("INVALID_INPUT", "Selected dishes conflict with the requested mode")
            if canonical is not None and any(
                not offer["store_id"] or self._canonical_store_id(connection, offer["store_id"]) != canonical
                for offer in [*locked, *selection]
            ):
                raise BusinessError("INVALID_INPUT", "Selected dishes conflict with the requested store")
            raw_slots = self._list(payload.get("slots"), "slots", default=[])
            slots = []
            for raw in raw_slots:
                slot = self._dict(raw, "slot")
                self._reject_extra_fields(slot, {"role", "count"}, "slot")
                role = self._recommendation_enum(slot.get("role"), "role", set(_ROLES))
                slot_count = self._integer(slot.get("count", 1), "count", minimum=1)
                if slot_count > 6 or len(slots) + slot_count > 6:
                    raise BusinessError("INVALID_INPUT", "Use up to six generated slots")
                slots.extend([role] * slot_count)
            results = []
            bounded = False
            generation_issue = None
            fixed = [*locked, *selection]
            if not slots and (selection or (locked and kind == "whole")):
                results.append(self._offer_option(connection, fixed, locked_ids, settings, add_budget))
            elif slots or kind != "whole":
                if fixed:
                    choices = {self._offer_venue(pick) for pick in fixed}
                    selected_ids = {pick["dish_id"] for pick in fixed}
                    if len(choices) > 1:
                        generation_issue = "fixed_dishes_span_venues"
                        available = []
                    elif fixed[0]["mode"] != "home_cooking" and not fixed[0]["store_id"]:
                        generation_issue = "locked_venue_unknown"
                        available = []
                    else:
                        available = [
                            offer for offer in available if self._offer_venue(offer) in choices and offer["dish_id"] not in selected_ids
                        ]
                groups = {}
                for offer in available:
                    identity = self._offer_venue(offer)
                    groups.setdefault(identity, []).append(offer)
                if not slots:
                    groups = {("all", None): available}
                search = {"visited": 0, "bounded": False}
                for group in groups.values():
                    combinations = self._dish_sets(group, list(Counter(slots).items()), search) if slots else ((offer,) for offer in group)
                    for picks in combinations:
                        if not slots:
                            if search["visited"] == 4000:
                                search["bounded"] = True
                                break
                            search["visited"] += 1
                        if len({pick["dish_id"] for pick in picks}) != len(picks):
                            continue
                        if kind == "whole" and dish_ids is not None and not any(pick["dish_id"] in dish_ids for pick in (*fixed, *picks)):
                            continue
                        results.append(self._offer_option(connection, [*fixed, *picks], locked_ids, settings, add_budget))
                    if search["bounded"]:
                        break
                bounded = search["bounded"]
            else:
                def in_scope(record):
                    details = record["details"]
                    venue = details.get("store_id")
                    if canonical and (not isinstance(venue, str) or self._canonical_store_id(connection, venue) != canonical):
                        return False
                    names = [record["name"], *self._record_dish_names(record)]
                    return not query or any(self._ingredient_key(query) in self._ingredient_key(name) for name in names)

                existing = self._recommend({**constraints, "ranking": "score", "limit": limit}, record_filter=in_scope)
                results = existing["items"]
                bounded = existing["total"] > 200
            results.sort(key=lambda option: (
                {"eligible": 0, "needs_verification": 1, "excluded": 2}[option["status"]],
                -option["recommendation_score"]["percent"],
                (option["cost"].get("cents") or {}).get("max", 10**15), option["name"],
            ))
            for result in results[:limit]:
                money = result["cost"].get("cents")
                elapsed = result["time"].get("minutes")
                deadline = settings["deadline"]
                budget = settings["budget_cents"]
                allowed_minutes = (deadline - settings["now"]).total_seconds() // 60 if deadline else None
                result["constraints_margin"] = {
                    "budget_cents": budget - money["max"] if budget is not None and money is not None else None,
                    "deadline_minutes": int(allowed_minutes - elapsed["max"]) if allowed_minutes is not None and elapsed is not None else None,
                }
            packaged_matches = []
            if kind == "single" and query and not selection:
                key = self._ingredient_key(query)
                alias_keys = {row["name_key"] for row in connection.execute(
                    """SELECT n.name_key FROM catalog_names n WHERE EXISTS (
                       SELECT 1 FROM catalog_names a WHERE a.dish_id=n.dish_id AND instr(a.name_key,?)>0
                    )""", (key,),
                )}
                candidates = connection.execute("SELECT candidate_id,name,mode,details_json FROM candidates ORDER BY name").fetchall()
                for row in candidates:
                    if mode is not None and row["mode"] != mode:
                        continue
                    details = json.loads(row["details_json"])
                    venue = details.get("store_id")
                    if canonical and (not isinstance(venue, str) or self._canonical_store_id(connection, venue) != canonical):
                        continue
                    names = details.get("dishes") or [details.get("dish")]
                    if not isinstance(names, list):
                        continue
                    for item in names:
                        name = item.get("name") if isinstance(item, dict) else item
                        if isinstance(name, str) and (key in self._ingredient_key(name) or self._ingredient_key(name) in alias_keys):
                            packaged_matches.append({"candidate_id": row["candidate_id"], "candidate_name": row["name"],
                                                     "dish": name, "mode": row["mode"], "individual_payment_cents": None})
                            if len(packaged_matches) == limit:
                                break
                    if len(packaged_matches) == limit:
                        break
                if not canonical and mode in (None, "home_cooking"):
                    recipes = connection.execute("SELECT recipe_id,name,version FROM recipes ORDER BY name").fetchall()
                    for row in recipes:
                        if len(packaged_matches) >= limit:
                            break
                        name_key = self._ingredient_key(row["name"])
                        if key in name_key or name_key in alias_keys:
                            packaged_matches.append({**dict(row), "dish": row["name"], "mode": "home_cooking", "individual_payment_cents": None})
            kitchen_alerts = {}
            if kind == "whole" and not (fixed or slots):
                if "kitchen_alerts" in existing:
                    kitchen_alerts = {"kitchen_alerts": existing["kitchen_alerts"]}
            elif mode == "home_cooking" or (mode is None and (use_stock or any(
                item["mode"] == "home_cooking" for item in (*fixed, *results, *packaged_matches)
            ))):
                kitchen_alerts = self._kitchen_alerts(connection, as_of)
            return {"kind": kind, "items": results[:limit], "total_evaluated": existing["total"] if kind == "whole" and not (fixed or slots) else len(results), "bounded": bounded,
                    "score_weights": settings["score_weights"],
                    "packaged_matches": packaged_matches, "generation_issue": generation_issue, "defaults_used": defaults_used,
                    **kitchen_alerts}

    def _meal_options_compare(self, payload):
        self._reject_extra_fields(payload, {"options", "scenarios"})
        options = self._dict(payload.get("options"), "options")
        scenarios = self._list(payload.get("scenarios"), "scenarios")
        if not 1 <= len(scenarios) <= 10:
            raise BusinessError("INVALID_INPUT", "Provide one to ten scenarios")
        base = self._dict(options.get("constraints"), "constraints", nullable=True)
        # Freeze the comparison clock and hold one consistent local data snapshot.
        base = {"now": self._now(), **base}
        with self.store.read():
            results = [self._meal_options({**options, "constraints": {**base, **self._dict(s, "scenario")}}) for s in scenarios]
        return {"scenarios": [{"index": index, **result} for index, result in enumerate(results)]}

    def _candidate_from_meal(self, payload):
        self._reject_extra_fields(payload, {"meal_id", "expected_version", "name", "include_actual_price", "confirm", "request_id"})
        meal_id = self._string(payload.get("meal_id"), "meal_id")
        expected = self._integer(payload.get("expected_version"), "expected_version", minimum=1)
        def prepare(connection):
            entry = self._active_meal_row(connection, meal_id)
            self._check_version(entry, meal_id, expected)
            meal = self._history_entry(connection, meal_id)
            names = [item["name"] for item in meal["items"]]
            name = self._string(payload.get("name"), "name", nullable=True) or "、".join(names)
            if not name:
                raise BusinessError("INVALID_INPUT", "Candidate name is required when the meal has no dishes")
            details = {"dishes": names, "source": {"meal_id": meal_id, "meal_version": expected}}
            if meal["store"]:
                details["store_id"] = meal["store"]["store_id"]
            if payload.get("include_actual_price") is True and meal["meal_amount_cents"] is not None and meal["meal_amount_cents"] >= 0:
                amount = meal["meal_amount_cents"]
                details["cost_profile"] = {
                    "payment": {"min_cents": amount, "max_cents": amount, "source": "actual"},
                    "payment_coverage_people_count": meal["people_count"] or 1,
                }
            return {"name": name, "mode": meal["mode"], "details": details}

        if payload.get("confirm") is True:
            return self._write("candidate_from_meal", payload, lambda connection: self._candidate_upsert({
                **prepare(connection), "request_id": self._subrequest(payload["request_id"], 0),
            }))
        with self.store.read() as connection:
            return {"would_write": False, "candidate": prepare(connection), "meal_id": meal_id, "expected_version": expected}
