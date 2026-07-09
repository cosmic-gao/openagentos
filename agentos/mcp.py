"""MCP 工具载入(langchain-mcp-adapters):解析 .mcp.json,按内容缓存载入;仅允许远程 http/sse。"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_cache: dict[str, list] = {}

_TYPE_TO_TRANSPORT = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable_http": "streamable_http",
    "websocket": "websocket",
}

_ALLOWED_TRANSPORTS = frozenset({"streamable_http", "sse"})


def _transport(spec: dict, conn: dict) -> str | None:
    """推断 transport:显式 transport > Claude/Cursor 的 type > command/url;无从判断返回 None。"""
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
    """规整 mcpServers 并过滤:非 http/sse(如 stdio,服务端镜像无 node/uv 起子进程)跳过并告警。"""
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
            "Ignoring non-http/sse MCP server(s) (only %s allowed): %s",
            "/".join(sorted(_ALLOWED_TRANSPORTS)),
            ", ".join(rejected),
        )
    return normalized


def parse(text: str | None) -> dict:
    """解析 .mcp.json 文本为规整后的 mcpServers 配置(容错,失败返回空)。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return _normalize(servers) if isinstance(servers, dict) else {}


async def tools(servers: dict) -> list:
    """把 mcpServers 配置载入为 tools(无配置返回 [];按规整后内容缓存,同配置只连一次)。

    逐个 server 并发载入;单个 server 连接/握手失败降级为 warning 并跳过,不影响其余 server
    (MultiServerMCPClient.get_tools() 内部用不带 return_exceptions 的 gather,一个坏 server 会整批失败)。
    """
    if not servers:
        return []
    normalized = _normalize(servers)
    key = json.dumps(normalized, sort_keys=True, default=str)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(normalized)

    async def _load(name: str) -> list:
        try:
            return await client.get_tools(server_name=name)
        except Exception as exc:  # noqa: BLE001 - 任何 server 的连接/握手失败都不应拖垮其余
            logger.warning("Skipping MCP server %r after load failure: %s", name, exc)
            return []

    loaded = await asyncio.gather(*(_load(name) for name in normalized))
    result = [tool for server_tools in loaded for tool in server_tools]
    _cache[key] = result
    return result
