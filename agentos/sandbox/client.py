"""OpenSandbox 连接层与沙箱规格:进程级共享 httpx transport + 长驻 SandboxManager;session 与 run 共享。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from agentos import workspace
from agentos.config import Settings

_transport: Any = None
_manager: Any = None
_manager_lock = asyncio.Lock()


def _connection(settings: Settings) -> Any:
    """进程级惰性建共享 transport(须在事件循环内首建);SDK 视为用户所有故不关闭它。"""
    global _transport
    from opensandbox.config import ConnectionConfig

    if _transport is None:
        _transport = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0
            )
        )
    return ConnectionConfig(
        domain=settings.opensandbox_domain,
        api_key=settings.opensandbox_api_key,
        protocol=settings.protocol,
        use_server_proxy=settings.server_proxy,
        transport=_transport,
    )


async def _get_manager(settings: Settings) -> Any:
    global _manager
    if _manager is None:
        async with _manager_lock:  # 双检锁:并发冷启动只建一次 manager
            if _manager is None:
                from opensandbox.manager import SandboxManager

                _manager = await SandboxManager.create(connection_config=_connection(settings))
    return _manager


def _resource(settings: Settings) -> dict[str, str]:
    return {"cpu": settings.sandbox_cpu, "memory": settings.sandbox_memory}


def _volume(settings: Settings, name: str, mount: str, sub: str) -> Any:
    from opensandbox.models.sandboxes import PVC, Host, Volume

    if settings.workspace_claim:
        return Volume(
            name=name,
            pvc=PVC(claimName=settings.workspace_claim, createIfNotExists=True),
            mountPath=mount,
            subPath=sub,
        )
    return Volume(name=name, host=Host(path=workspace.host_root(settings)), mountPath=mount, subPath=sub)


def _volumes(settings: Settings, assistant_id: str) -> list[Any]:
    """只挂 skills 卷(助手级配置);会话 /workspace 不挂持久卷、随箱销毁(ephemeral,LGP 语义)。"""
    skills = workspace.under(settings, workspace.skills(settings, assistant_id))
    return [_volume(settings, "skills", workspace.SKILLS, skills)]
