"""MCP 工具载入（langchain-mcp-adapters）。

server 值遵循 langchain-mcp-adapters 的 Connection schema；兼容 Claude/Cursor 的 `type`
或省略（据 command/url 推断 transport）。`parse` 从 .mcp.json 文本解析；`tools` 从 server
配置载入工具，结果按内容缓存。

**仅允许远程 http 家族 transport（streamable_http / sse）**：stdio 等会在 app 容器内起子进程、
不符合服务端场景，一律跳过并告警（见 `_ALLOWED_TRANSPORTS`）。
"""

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

# 仅允许远程 http 家族 transport。服务端不宜跑 stdio(会在 app 容器内起子进程,且镜像无 node/uv;
# langchain-mcp-adapters 亦明确劝退服务端用 stdio)。stdio / websocket / 无法推断者一律跳过并告警;
# 需放开某类,把它加进此集合即可。
_ALLOWED_TRANSPORTS = frozenset({"streamable_http", "sse"})


def _transport(spec: dict, conn: dict) -> str | None:
    """推断 server 的 transport:显式优先,否则据 type / command / url 推断,无从判断返回 None。"""
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
    """规整 mcpServers,并按 ``_ALLOWED_TRANSPORTS`` 过滤:非 http/sse 的条目跳过并告警。"""
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
