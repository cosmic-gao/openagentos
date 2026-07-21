"""按 (identity, assistant, thread) 多路复用的会话沙箱:按 metadata 发现/新建,失联时重发现重试一次;/workspace ephemeral。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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

from agentos.config import Settings, current_thread_id
from agentos.sandbox.client import _connection, _get_manager, _resource, _volumes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ASYNC_ONLY = "SessionSandbox is async-only (Aegra invokes graphs with ainvoke)."
_MAX_SLOTS = 256


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    backend: AsyncOpenSandboxBackend | None = None
    renewed: float = 0.0


_slots: OrderedDict[tuple[str, str, str], _Slot] = OrderedDict()


async def _discover(settings: Settings, metadata: dict[str, str]) -> Any | None:
    from opensandbox.models.sandboxes import SandboxFilter, SandboxState
    from opensandbox.sandbox import Sandbox

    manager = await _get_manager(settings)
    listing = await manager.list_sandbox_infos(
        SandboxFilter(
            metadata=metadata,
            states=[SandboxState.RUNNING, SandboxState.PAUSED],
            page_size=10,
        )
    )
    connection = _connection(settings)
    for info in listing.sandbox_infos:
        if info.status.state == SandboxState.PAUSED:
            return await Sandbox.resume(sandbox_id=info.id, connection_config=connection)
        if info.status.state == SandboxState.RUNNING:
            return await Sandbox.connect(sandbox_id=info.id, connection_config=connection)
    return None


async def _open(settings: Settings, identity: str, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    metadata = {"agentos.identity": identity, "agentos.assistant": assistant_id, "agentos.thread": thread_id}

    existing = await _discover(settings, metadata)
    if existing is not None:
        return AsyncOpenSandboxBackend(existing, default_timeout=settings.sandbox_timeout)

    created = await AsyncOpenSandboxBackend.create(
        settings.sandbox_image,
        connection_config=_connection(settings),
        timeout=timedelta(seconds=settings.sandbox_ttl),
        default_timeout=settings.sandbox_timeout,
        resource=_resource(settings),
        volumes=_volumes(settings, assistant_id),
        metadata=metadata,
    )
    logger.info("sandbox created (identity=%s assistant=%s thread=%s)", identity, assistant_id, thread_id)
    # 会话箱统一交服务端 TTL(renew_intent)回收:本层句柄一律 owns_sandbox=False,与 _discover 返回的句柄语义一致。
    # 驱逐(_trim)/失联(_forget)只丢本地引用、绝不 kill 真箱——否则高并发下 _trim 可能误杀正在执行的首建箱
    # (命令中断 + ephemeral /workspace 丢失);同一会话在 TTL 内重来经 _discover 重连同一箱、数据不丢。
    return AsyncOpenSandboxBackend(created.sandbox, owns_sandbox=False, default_timeout=settings.sandbox_timeout)


def _trim(keep: tuple[str, str, str]) -> None:
    """压回上限:LRU 丢弃最旧的空闲槽(thread_id 随会话增长,必须限界)。
    句柄 owns_sandbox=False(见 _open),丢弃即可——真箱交服务端 TTL 回收,无需 aclose。"""
    while len(_slots) > _MAX_SLOTS:
        victim = next(
            (key for key, slot in _slots.items() if key != keep and not slot.lock.locked()),
            None,
        )
        if victim is None:
            return
        _slots.pop(victim)


def _forget(key: tuple[str, str, str], backend: AsyncOpenSandboxBackend) -> None:
    """忘记失联句柄;仅当槽内仍是同一实例(避免误删并发新句柄)。句柄 owns=False,丢弃即可(真箱由 TTL 回收)。"""
    slot = _slots.get(key)
    if slot is not None and slot.backend is backend:
        slot.backend = None


async def _renew(settings: Settings, slot: _Slot) -> None:
    """按半个 TTL 节流续期,防长会话被服务端中途回收;失败留给失联重建兜底。"""
    ttl = settings.sandbox_ttl
    backend = slot.backend
    if not ttl or backend is None:
        return
    now = time.monotonic()
    if now - slot.renewed < ttl / 2:
        return
    slot.renewed = now
    try:
        await backend.sandbox.renew(timedelta(seconds=ttl))
    except Exception as exc:
        logger.debug("sandbox renew failed: %s", exc)


async def _acquire(settings: Settings, identity: str, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    """同 key 并发只开箱一次(双检锁)。"""
    key = (identity, assistant_id, thread_id)
    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = _Slot()
    else:
        _slots.move_to_end(key)

    if slot.backend is None:
        async with slot.lock:
            if slot.backend is None:
                slot.backend = await _open(settings, *key)
                slot.renewed = time.monotonic()
                _trim(keep=key)
    backend = slot.backend
    await _renew(settings, slot)
    return backend


async def _healthy(backend: AsyncOpenSandboxBackend) -> bool:
    try:
        return await backend.sandbox.is_healthy()
    except Exception:
        return False


def _failed(result: Any) -> bool:
    """结果是否为失败:后端从不抛异常,以结果对象回传失败。"""
    if isinstance(result, ExecuteResponse):
        return result.exit_code != 0
    if isinstance(result, ReadResult):
        return result.error is not None
    if isinstance(result, list):
        return any(getattr(item, "error", None) for item in result)
    return False


class SessionSandbox(BaseSandbox):
    """按 (identity, assistant, thread) 多路复用的沙箱后端;失联时重新发现并重试一次。"""

    def __init__(self, settings: Settings, assistant_id: str, identity: str) -> None:
        self._settings = settings
        self._assistant = assistant_id
        self._identity = identity

    @property
    def id(self) -> str:
        return f"agentos-{self._assistant}"

    async def _call(self, op: Callable[[AsyncOpenSandboxBackend], Awaitable[Any]]) -> Any:
        key = (self._identity, self._assistant, current_thread_id())
        backend = await _acquire(self._settings, *key)
        result = await op(backend)
        if _failed(result) and not await _healthy(backend):
            logger.info("sandbox unreachable, rebuilding (identity=%s assistant=%s thread=%s)", *key)
            _forget(key, backend)
            result = await op(await _acquire(self._settings, *key))
        return result

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


def session(settings: Settings, assistant_id: str, identity: str) -> SessionSandbox:
    return SessionSandbox(settings, assistant_id, identity)
