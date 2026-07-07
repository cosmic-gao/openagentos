"""每线程临时沙箱:metadata 发现 + connect/resume/create,服务端 TTL 到期自动销毁。

生命周期归 OpenSandbox 服务端(创建时带 ``timeout``),进程内只缓存句柄——app 重启
或沙箱到期后,下一次操作按 metadata 重新发现或重建,同一 subPath 重挂共享磁盘,
线程文件无损。持久化在卷上,沙箱本身可弃。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
)
from deepagents.backends.sandbox import BaseSandbox
from deepagents_opensandbox import AsyncOpenSandboxBackend

from agentos import workspace
from agentos.config import Settings, current_thread_id, safe_segment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ASYNC_ONLY = "SessionSandbox is async-only (Aegra invokes graphs with ainvoke)."

_handles: dict[tuple[str, str], AsyncOpenSandboxBackend] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _connection(settings: Settings) -> Any:
    from opensandbox.config import ConnectionConfig

    return ConnectionConfig(
        domain=settings.opensandbox_domain,
        api_key=settings.opensandbox_api_key,
        protocol=settings.protocol,
        use_server_proxy=settings.server_proxy,
    )


def _volume(settings: Settings, name: str, mount: str, sub: str) -> Any:
    from opensandbox.models.sandboxes import PVC, Host, Volume

    if settings.workspace_claim:
        return Volume(
            name=name,
            pvc=PVC(claim_name=settings.workspace_claim, create_if_not_exists=True),
            mount_path=mount,
            sub_path=sub,
        )
    return Volume(
        name=name,
        host=Host(path=workspace.host_root(settings)),
        mount_path=mount,
        sub_path=sub,
    )


def _volumes(settings: Settings, assistant_id: str, thread_id: str) -> list[Any]:
    return [
        _volume(settings, "workspace", workspace.WORKSPACE, f"{assistant_id}/{thread_id}"),
        _volume(
            settings,
            "skills",
            workspace.SKILLS,
            f"{workspace.DEEPAGENT}/{assistant_id}/skills",
        ),
    ]


async def _discover(settings: Settings, metadata: dict[str, str]) -> Any | None:
    """按 metadata 找已有沙箱:RUNNING 连接,PAUSED 恢复,否则 None。"""
    from opensandbox.manager import SandboxManager
    from opensandbox.models.sandboxes import SandboxFilter, SandboxState
    from opensandbox.sandbox import Sandbox

    connection = _connection(settings)
    async with await SandboxManager.create(connection_config=connection) as manager:
        listing = await manager.list_sandbox_infos(
            SandboxFilter(metadata=metadata, page_size=10)
        )
        for info in listing.sandbox_infos:
            if info.status.state == SandboxState.PAUSED:
                return await Sandbox.resume(sandbox_id=info.id, connection_config=connection)
            if info.status.state == SandboxState.RUNNING:
                return await Sandbox.connect(sandbox_id=info.id, connection_config=connection)
    return None


async def _open(settings: Settings, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    metadata = {"agentos.assistant": assistant_id, "agentos.thread": thread_id}
    workspace.thread(settings, assistant_id, thread_id).mkdir(parents=True, exist_ok=True)

    existing = await _discover(settings, metadata)
    if existing is not None:
        return AsyncOpenSandboxBackend(existing, default_timeout=settings.sandbox_timeout)

    backend = await AsyncOpenSandboxBackend.create(
        settings.sandbox_image,
        connection_config=_connection(settings),
        timeout=timedelta(seconds=settings.sandbox_ttl),
        default_timeout=settings.sandbox_timeout,
        volumes=_volumes(settings, assistant_id, thread_id),
        metadata=metadata,
    )
    logger.info("sandbox created (assistant=%s thread=%s)", assistant_id, thread_id)
    return backend


async def _acquire(settings: Settings, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    key = (assistant_id, thread_id)
    if (cached := _handles.get(key)) is not None:
        return cached
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if (cached := _handles.get(key)) is not None:
            return cached
        backend = await _open(settings, assistant_id, thread_id)
        _handles[key] = backend
        return backend


class SessionSandbox(BaseSandbox):
    """按 (assistant, thread) 多路复用的沙箱后端;沙箱失联时重新发现并重试一次。"""

    def __init__(self, settings: Settings, assistant_id: str) -> None:
        self._settings = settings
        self._assistant_id = safe_segment(assistant_id, "default")

    @property
    def id(self) -> str:
        return f"agentos-{self._assistant_id}"

    async def _call(
        self, op: Callable[[AsyncOpenSandboxBackend], Awaitable[Any]]
    ) -> Any:
        key = (self._assistant_id, safe_segment(current_thread_id(), "default"))
        backend = await _acquire(self._settings, *key)
        try:
            return await op(backend)
        except Exception:
            _handles.pop(key, None)
            return await op(await _acquire(self._settings, *key))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await self._call(lambda b: b.aexecute(command, timeout=timeout))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._call(lambda b: b.aread(file_path, offset, limit))

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await self._call(lambda b: b.aupload_files(files))

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._call(lambda b: b.adownload_files(paths))

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise NotImplementedError(_ASYNC_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise NotImplementedError(_ASYNC_ONLY)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise NotImplementedError(_ASYNC_ONLY)


def session(settings: Settings, assistant_id: str) -> SessionSandbox | None:
    """沙箱后端;``AGENTOS_SANDBOX_ENABLED=false`` 时返回 None(开发态)。"""
    if not settings.sandbox_enabled:
        return None
    return SessionSandbox(settings, assistant_id)
