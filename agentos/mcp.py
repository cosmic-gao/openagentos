"""MCP 工具载入（langchain-mcp-adapters）。

server 值遵循 langchain-mcp-adapters 的 Connection schema；兼容 Claude/Cursor 的 `type`
或省略（据 command/url 推断 transport）。`parse` 从 .mcp.json 文本解析；`tools` 从 server
配置载入工具，结果按内容缓存。
"""

from __future__ import annotations

import json

_cache: dict[str, list] = {}

# Claude/Cursor 的 `type` → langchain-mcp-adapters 的 `transport`
_TYPE_TO_TRANSPORT = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "websocket": "websocket",
}


def _normalize(servers: dict) -> dict:
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


def parse(text: str | None) -> dict:
    """解析 .mcp.json 文本为规整后的 mcpServers 配置（容错）。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return _normalize(servers) if isinstance(servers, dict) else {}


async def tools(servers: dict) -> list:
    """把 mcpServers 配置载入为 tools（无配置返回 []；按内容缓存）。"""
    if not servers:
        return []
    normalized = _normalize(servers)
    key = json.dumps(normalized, sort_keys=True, default=str)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    from langchain_mcp_adapters.client import MultiServerMCPClient

    result = await MultiServerMCPClient(normalized).get_tools()
    _cache[key] = result
    return result
