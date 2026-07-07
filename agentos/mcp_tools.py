"""按 assistant 载入 .deepagent/<id>/.mcp.json 的 MCP 服务器为 tools（按 mtime 缓存）。"""

from __future__ import annotations

import json
import os

from agentos.workspace import load_mcp_servers, mcp_file

_cache: dict[str, tuple[float, list]] = {}


# Claude/Cursor 的 `type` → langchain-mcp-adapters 的 `transport`
_TYPE_TO_TRANSPORT = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "websocket": "websocket",
}


def _normalize(servers: dict) -> dict:
    """把 .mcp.json 的 server 定义补成 langchain-mcp-adapters 的 Connection。

    对齐标准：显式 `transport` 原样保留；兼容 Claude/Cursor 的 `type`；否则据 command/url 推断。
    """
    normalized: dict = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        conn = {k: v for k, v in spec.items() if k != "type"}
        if "transport" not in conn:
            claude_type = spec.get("type")
            if claude_type in _TYPE_TO_TRANSPORT:
                conn["transport"] = _TYPE_TO_TRANSPORT[claude_type]
            elif conn.get("command"):
                conn["transport"] = "stdio"
            elif conn.get("url"):
                conn["transport"] = "streamable_http"
        normalized[name] = conn
    return normalized


async def load_mcp_tools(assistant_id: str) -> list:
    servers = dict(load_mcp_servers(assistant_id))
    raw = os.environ.get("AGENTOS_MCP_SERVERS")  # 可选全局叠加；助手级同名项覆盖
    if raw:
        try:
            servers = {**json.loads(raw), **servers}
        except ValueError:
            pass
    if not servers:
        return []

    path = mcp_file(assistant_id)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    cached = _cache.get(assistant_id)
    if cached and cached[0] == mtime:
        return cached[1]

    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools = await MultiServerMCPClient(_normalize(servers)).get_tools()
    _cache[assistant_id] = (mtime, tools)
    return tools
