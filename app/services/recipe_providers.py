from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import BusinessError
from .tool_schema import _integer, _string, _tool


BAIDU_URL = "https://qianfan.baidubce.com/v2/ai_search/web_search"
MAX_REPLY_BYTES = 1_048_576
TOOL_SCHEMAS = [
    _tool("recipe_provider_status", "Check which optional recipe providers are enabled and configured locally.", {}),
    _tool("recipe_baidu_search", "Send only the explicit recipe query to the enabled Baidu provider and return source links.", {"query": _string, "limit": _integer}, ("query",)),
    _tool("recipe_model_preview", "Send only the explicit prompt and serving count to the configured model. The recipe is returned as a preview and saved only after a separate confirmed import.", {"prompt": _string, "servings": _integer}, ("prompt",)),
]
HANDLERS = {
    "recipe_provider_status": "_recipe_provider_status",
    "recipe_baidu_search": "_recipe_baidu_search",
    "recipe_model_preview": "_recipe_model_preview",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_url(url: str, *, local_http_only: bool) -> bool:
    try:
        address = urlsplit(url)
        _ = address.port
    except ValueError:
        return False
    return bool(
        address.hostname
        and address.username is None
        and address.password is None
        and (
            address.scheme == "https"
            or (address.scheme == "http" and (not local_http_only or address.hostname in {"localhost", "127.0.0.1", "::1"}))
        )
    )


def _post_json(url: str, body: dict, token: str | None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            value = response.read(MAX_REPLY_BYTES + 1)
        if len(value) > MAX_REPLY_BYTES:
            raise ValueError("payload too large")
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError("expected response object")
        return result
    except (URLError, OSError, ValueError) as exc:
        raise BusinessError("PROVIDER_UNAVAILABLE", "The recipe provider did not return a readable response") from exc


class RecipeProvidersMixin:
    def _recipe_provider_status(self, payload: dict) -> dict:
        self._reject_extra_fields(payload, set())
        baidu_enabled = os.getenv("SHIXU_ENABLE_BAIDU_RECIPE") == "1"
        model_enabled = os.getenv("SHIXU_ENABLE_RECIPE_MODEL") == "1"
        model_url = os.getenv("SHIXU_RECIPE_MODEL_URL", "")
        model_name = os.getenv("SHIXU_RECIPE_MODEL", "")
        return {
            "baidu": {"enabled": baidu_enabled, "ready": baidu_enabled and bool(os.getenv("SHIXU_BAIDU_API_KEY"))},
            "model": {"enabled": model_enabled, "ready": model_enabled and bool(model_name) and _read_url(model_url, local_http_only=True)},
        }

    def _recipe_baidu_search(self, payload: dict) -> dict:
        self._reject_extra_fields(payload, {"query", "limit"})
        query = self._string(payload.get("query"), "query")
        limit = self._integer(payload.get("limit", 5), "limit", minimum=1)
        if len(query) > 300 or limit > 10:
            raise BusinessError("INVALID_INPUT", "Supply up to 300 query characters and 10 results")
        if not self._recipe_provider_status({})["baidu"]["ready"]:
            raise BusinessError("PROVIDER_DISABLED", "Baidu recipe search is disabled or unconfigured")
        response = _post_json(BAIDU_URL, {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "edition": "standard",
            "resource_type_filter": [{"type": "web", "top_k": limit}],
        }, os.getenv("SHIXU_BAIDU_API_KEY"))
        references = response.get("references")
        if not isinstance(references, list):
            raise BusinessError("PROVIDER_UNAVAILABLE", "Baidu did not return search references")
        items = []
        for item in references:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and len(url) <= 2048 and _read_url(url, local_http_only=False):
                entry = {key: item[key][:1000] for key in ("title", "snippet", "website", "date") if isinstance(item.get(key), str)}
                items.append({"url": url, **entry})
                if len(items) == limit:
                    break
        return {"provider": "baidu", "items": items}

    def _recipe_model_preview(self, payload: dict) -> dict:
        self._reject_extra_fields(payload, {"prompt", "servings"})
        prompt = self._string(payload.get("prompt"), "prompt")
        servings = self._integer(payload.get("servings", 2), "servings", minimum=1)
        if len(prompt) > 2000 or servings > 100:
            raise BusinessError("INVALID_INPUT", "Supply up to 2000 prompt characters and 100 servings")
        if not self._recipe_provider_status({})["model"]["ready"]:
            raise BusinessError("PROVIDER_DISABLED", "The recipe model is disabled or unconfigured")
        response = _post_json(os.environ["SHIXU_RECIPE_MODEL_URL"], {
            "model": os.environ["SHIXU_RECIPE_MODEL"],
            "messages": [
                {"role": "system", "content": (
                    "用中文提供菜谱。只返回 JSON 对象，包含 name 字符串、servings 正整数、"
                    "ingredients 数组（每项包含 name、quantity、unit，未知用 null）、"
                    "steps 数组（每项包含 title 和 instruction）。"
                )},
                {"role": "user", "content": f"{servings} 人份。{prompt}"},
            ],
        }, os.getenv("SHIXU_RECIPE_MODEL_KEY"))
        try:
            content = response["choices"][0]["message"]["content"].strip()
            if content.startswith("```") and content.endswith("```"):
                content = content.partition("\n")[2].removesuffix("```").strip()
            draft = json.loads(content)
            arguments = {"source_kind": "ai_generated", "structured_draft": draft}
            preview = self._recipe_import_preview(arguments)
        except (AttributeError, BusinessError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise BusinessError("INVALID_PROVIDER_RESULT", "The model did not return a valid recipe draft") from exc
        return {"provider": "model", "preview": preview, "import_arguments": arguments}
