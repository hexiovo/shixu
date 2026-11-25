from __future__ import annotations

import asyncio
import base64
import http.client
import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, Icon, ListToolsResult, TextContent, Tool

from app import APP_VERSION
from app.http.runtime import HOST, settings
from app.launch import ensure_service_ready
from app.services import TOOL_SCHEMAS


logger = logging.getLogger("shixu.mcp")
TOOL_NAMES = {schema["name"] for schema in TOOL_SCHEMAS}


async def _list_tools(_context: Any, _params: Any) -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(name=schema["name"], description=schema["description"], input_schema=schema["inputSchema"])
            for schema in TOOL_SCHEMAS
        ]
    )


def _call_local_action(endpoint: dict[str, Any], action: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = settings()
    body = json.dumps({"action": action, "payload": payload}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connection = http.client.HTTPConnection(HOST, config.port, timeout=30.0)
    try:
        connection.request(
            "POST",
            "/_internal/mcp",
            body=body,
            headers={
                "Host": config.host_header,
                "Authorization": f"Bearer {endpoint['token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        response_body = response.read()
    finally:
        connection.close()
    result = json.loads(response_body.decode("utf-8"))
    if response.status != 200:
        message = result.get("error", {}).get("message", f"HTTP {response.status}") if isinstance(result, dict) else f"HTTP {response.status}"
        raise RuntimeError(f"The local Shixu service rejected the action: {message}")
    return result


async def _call_tool(_context: Any, params: CallToolRequestParams) -> CallToolResult:
    if params.name not in TOOL_NAMES:
        result: dict[str, Any] = {"ok": False, "error": {"code": "unknown_action", "message": "The requested tool is not available"}}
    else:
        try:
            endpoint = _ENDPOINT
            result = await asyncio.to_thread(_call_local_action, endpoint, params.name, params.arguments or {})
        except Exception:
            logger.exception("MCP call failed: %s", params.name)
            result = {"ok": False, "error": {"code": "service_unavailable", "message": "The local Shixu service could not complete the action"}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))],
        structured_content=result,
        is_error=result.get("ok") is False,
    )


icon_path = Path(__file__).resolve().parents[1] / "ui" / "shixu-icon-64.png"
icon_data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
server = Server(
    "shixu",
    version=APP_VERSION,
    instructions=(
        "食序在本机提供用餐记录、候选与推荐业务动作。未知的时间、金额和食材信息应保留为未知。"
        "用户明确要记下想吃的菜或方案时，先用 candidate_list 或 recipe_list 找到对应 ID，"
        "再用 recommendation_preference_set 传 preference=want_to_try 加入待尝。"
        "没有对应记录且已确定菜名和用餐方式时，可先用 candidate_upsert 保存候选。"
        "要查看想吃清单，用 recommendation_preference_list 传 preference=want_to_try；"
        "明确要求移除时，用相同来源 ID 和偏好设置 enabled=false。"
        "下次选餐用 recommend 并传 ranking=score，或用 meal_options；待尝按已有喜好规则加分，仍按时间、预算等条件判断是否合适。"
        "用户确认买了食材时，有清单就用 shopping_purchase_record 记录实买数量，或用 shopping_list_mark_bought 确认已买的清单数量；"
        "没有清单就用 inventory_batch_upsert。拿到本批到期日时传 expires_on。"
        "没有到期日时，用 inventory_shelf_life_get 按食材和存放位置查询已保存的保质天数。"
        "查不到记录或收到 SHELF_LIFE_REQUIRED 时，向用户询问本批到期日或要记住的保质天数。"
        "只有用户确认的天数才能通过 shelf_life_days 写入，或使用 inventory_shelf_life_set 单独保存。"
        "用户要做饭时，用 recommend 或 meal_options 并指定 mode=home_cooking，使用库存时传 use_kitchen_stock=true。"
        "推荐、选配或缩放菜谱返回 kitchen_alerts 时，先主动告诉用户过期食材和剩菜的名称及各自的到期日或保留至日期，再给做饭建议。"
        "不要建议使用这些过期批次。"
    ),
    icons=[Icon(src=f"data:image/png;base64,{icon_data}", mime_type="image/png", sizes=["64x64"])],
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)
_ENDPOINT: dict[str, Any] = {}


async def main() -> None:
    global _ENDPOINT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    _ENDPOINT = await asyncio.to_thread(ensure_service_ready)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
