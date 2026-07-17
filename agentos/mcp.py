"""MCP 工具载入(langchain-mcp-adapters):解析 .mcp.json,按内容缓存载入;仅允许远程 http/sse。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, list] = {}

# 单个 MCP server 载入(连接 + 列 tools)的超时秒数。配错的 server(如把 url 指向普通网站)
# 会在 MCP 协议层一直等 initialize 响应而卡死,HTTP 超时救不了;这里用硬超时兜底,可用环境变量覆盖。
_LOAD_TIMEOUT_S = float(os.getenv("AGENTOS_MCP_LOAD_TIMEOUT", "20"))

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
    """取出 .mcp.json 的 mcpServers 段(容错,失败返回空);规整与过滤留给 tools(),避免重复。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


async def tools(servers: dict) -> list:
    """把 mcpServers 配置载入为 tools(无配置返回 [])。**按 server 粒度缓存、仅成功载入的才缓存**。

    逐个 server 并发载入,各套 _LOAD_TIMEOUT_S 硬超时;单个 server 超时/连接/握手失败降级为 warning
    并跳过,**且不写缓存**——故某 server 短暂不可达不会被永久负缓存,恢复后下次自动重试;好 server 命中
    缓存不受坏 server 拖累(否则:一个坏 server 卡死 initialize 会拖垮整批,或把缺它的结果永久缓存)。
    """
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
            loaded = await asyncio.wait_for(
                client.get_tools(server_name=name), timeout=_LOAD_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Skipping MCP server %r: not ready within %.0fs "
                "(unreachable, or the URL is not an MCP endpoint)",
                name,
                _LOAD_TIMEOUT_S,
            )
            return []  # 不写缓存:恢复后下次重试
        except Exception as exc:  # noqa: BLE001 - 任何 server 的连接/握手失败都不应拖垮其余
            logger.warning("Skipping MCP server %r after load failure: %s", name, exc)
            return []
        _cache[key] = loaded  # 仅成功才缓存
        return loaded

    loaded = await asyncio.gather(*(_load(name, conf) for name, conf in normalized.items()))
    return [tool for server_tools in loaded for tool in server_tools]
