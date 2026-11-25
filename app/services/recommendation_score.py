from __future__ import annotations

from .errors import BusinessError


DEFAULT_SCORE_WEIGHTS = {"time": 40, "cost": 30, "preference": 20, "variety": 10}


def score_weights(value):
    if value is None:
        return DEFAULT_SCORE_WEIGHTS
    if (
        not isinstance(value, dict) or value.keys() != DEFAULT_SCORE_WEIGHTS.keys()
        or any(type(weight) is not int or weight < 0 or weight > 100 for weight in value.values())
        or sum(value.values()) != 100
    ):
        raise BusinessError("INVALID_INPUT", "score_weights requires time, cost, preference and variety points totaling 100")
    return value


def recommendation_score(item: dict, budget_cents: int | None, history: dict, weights=DEFAULT_SCORE_WEIGHTS) -> dict:
    if item["status"] == "excluded":
        return {"percent": 0, "components": {}, "cap_reason": "excluded"}

    time = item["time"]
    cost = item["cost"]
    time_points = 0
    if time["status"] in {"known", "expected_meets"}:
        limit = time["deadline_minutes"]
        time_points = 40 if limit is None or limit == 0 else 28 + round(12 * (limit - time["minutes"]["max"]) / limit)
    cost_points = 0
    if cost["status"] in {"known", "expected_meets"}:
        cost_points = 30 if not budget_cents else 20 + round(10 * (budget_cents - cost["cents"]["max"]) / budget_cents)

    preferences = history["preference"]
    feedback = (history["recipe_feedback"] or {}).get("feedback_kind")
    preference_points = 12
    if "temporarily_tired" in preferences or feedback == "temporarily_tired":
        preference_points = 2
    elif item["preference"] == "like" or "liked" in preferences or feedback == "liked":
        preference_points = 20
    elif "want_to_try" in preferences or feedback == "want_to_try":
        preference_points = 16
    elif history.get("catalog_rating") is not None:
        preference_points = round(20 * history["catalog_rating"] / 5)
    variety_points = max(0, 10 - 2 * history["recent_meal_count"] - 2 * len(history["skips"]))
    components = {
        "time": time_points,
        "cost": cost_points,
        "preference": preference_points,
        "variety": variety_points,
    }
    components = {name: round(points * weights[name] / DEFAULT_SCORE_WEIGHTS[name]) for name, points in components.items()}
    needs_verification = item["status"] == "needs_verification"
    return {
        "percent": min(59 if needs_verification else 100, sum(components.values())),
        "components": components,
        "cap_reason": "needs_verification" if needs_verification else None,
    }
