"""MCP 工具载入(langchain-mcp-adapters):parse 解析 .mcp.json,tools 按内容缓存载入。"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_cache: dict[str, list] = {}

# Claude/Cursor 的 `type` → langchain-mcp-adapters 的 `transport`
_TYPE_TO_TRANSPORT = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "websocket": "websocket",
}

# 仅远程 http 家族:stdio 会在 app 容器内起子进程(镜像无 node/uv),不符服务端场景;其余跳过并告警
_ALLOWED_TRANSPORTS = frozenset({"streamable_http", "sse"})


def _transport(spec: dict, conn: dict) -> str | None:
    """推断 transport:显式 > type > command/url;无从判断返回 None。"""
    if conn.get("transport"):
        return conn["transport"]
    claude_type = spec.get("type")
    if claude_type in _TYPE_TO_TRANSPORT:
        return _TYPE_TO_TRANSPORT[claude_type]
    if conn.get("command"):
        return "stdio"
    if conn.get("url"):
        return "streamable_http"
    return None


def _normalize(servers: dict) -> dict:
    """规整并过滤 mcpServers:非 http/sse 跳过并告警。"""
    normalized: dict = {}
    rejected: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        conn = {k: v for k, v in spec.items() if k != "type"}
        transport = _transport(spec, conn)
        if transport not in _ALLOWED_TRANSPORTS:
            rejected.append(f"{name}({transport or 'unknown'})")
            continue
        conn["transport"] = transport
        normalized[name] = conn
    if rejected:
        logger.warning(
            "忽略非 http/sse 的 MCP server(仅允许 %s): %s",
            "/".join(sorted(_ALLOWED_TRANSPORTS)),
            ", ".join(rejected),
        )
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
