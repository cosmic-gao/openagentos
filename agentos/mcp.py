"""MCP 工具载入(langchain-mcp-adapters):解析 .mcp.json,按 server 缓存载入;仅允许 http/sse transport。

网络出站的 SSRF/私网防护交由下游网关负责,本层不再对 url 做主机/IP 过滤。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, list] = {}

_LOAD_TIMEOUT_S = float(os.getenv("AGENTOS_MCP_LOAD_TIMEOUT", "20"))

_ALLOWED_TRANSPORTS = frozenset({"streamable_http", "sse"})


def _transport(conn: dict) -> str | None:
    if conn.get("transport"):
        return conn["transport"]
    if conn.get("command"):
        return "stdio"
    if conn.get("url"):
        return "streamable_http"
    return None


def _normalize(servers: dict) -> dict:
    """过滤:非 http/sse(如 stdio)或缺 url 的条目跳过并告警。SSRF/私网防护交下游网关。"""
    normalized: dict = {}
    rejected: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        conn = dict(spec)
        transport = _transport(conn)
        if transport not in _ALLOWED_TRANSPORTS:
            rejected.append(f"{name}({transport or 'unknown'})")
            continue
        if not conn.get("url"):
            rejected.append(f"{name}(no-url)")
            continue
        conn["transport"] = transport
        normalized[name] = conn
    if rejected:
        logger.warning(
            "Ignoring unusable MCP server(s) (need http/sse transport + url): %s", ", ".join(rejected)
        )
    return normalized


def parse(text: str | None) -> dict:
    """取出 .mcp.json 的 mcpServers 段(容错,失败返回空)。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


async def tools(servers: dict) -> list:
    """载入 mcpServers 为 tools。逐 server 并发 + 硬超时;单个失败降级 warning 跳过、不写缓存,仅成功的才缓存。"""
    if not servers:
        return []
    normalized = _normalize(servers)
    if not normalized:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient

    async def _load(name: str, conf: Any) -> list:
        key = json.dumps({name: conf}, sort_keys=True, default=str)
        cached = _cache.get(key)
        if cached is not None:
            return cached
        try:
            client = MultiServerMCPClient({name: conf})
            loaded = await asyncio.wait_for(client.get_tools(server_name=name), timeout=_LOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Skipping MCP server %r: not ready within %.0fs", name, _LOAD_TIMEOUT_S)
            return []
        except Exception as exc:  # noqa: BLE001 — 单个 server 失败不拖垮其余
            logger.warning("Skipping MCP server %r after load failure: %s", name, exc)
            return []
        _cache[key] = loaded
        return loaded

    loaded = await asyncio.gather(*(_load(name, conf) for name, conf in normalized.items()))
    return [tool for server_tools in loaded for tool in server_tools]
