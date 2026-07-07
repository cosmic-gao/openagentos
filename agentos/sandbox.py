"""每线程临时沙箱:按 metadata 发现(connect/resume)或新建,服务端 TTL 到期自动销毁。

生命周期归 OpenSandbox 服务端(创建时带 ``timeout``),进程内只缓存句柄:app 重启或沙箱
到期后,下一次操作按 metadata 重新发现或重建,同一 subPath 重挂共享磁盘,线程文件无损——
持久化在卷上,沙箱本身可弃。

句柄按 (assistant, thread) 缓存,已建成的句柄数受 ``_MAX_HANDLES`` 约束:超额时 LRU 丢弃
空闲句柄(只忘记本地句柄、不 kill,服务端 TTL 负责回收)。后端从不抛异常、失败以结果对象
回传,故按结果判失败;若沙箱确已失联(``is_healthy`` 为假),忘记该句柄并重新发现/重建后
重试一次。
"""

from __future__ import annotations

import asyncio
import logging
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

from agentos import workspace
from agentos.config import Settings, current_thread_id, safe_segment

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ASYNC_ONLY = "SessionSandbox is async-only (Aegra invokes graphs with ainvoke)."
_MAX_HANDLES = 256


@dataclass
class _Slot:
    """每 (assistant, thread) 一个缓存槽:一把开箱锁 + 惰性句柄。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    backend: AsyncOpenSandboxBackend | None = None


_slots: OrderedDict[tuple[str, str], _Slot] = OrderedDict()


def _connection(settings: Settings) -> Any:
    from opensandbox.config import ConnectionConfig

    return ConnectionConfig(
        domain=settings.opensandbox_domain,
        api_key=settings.opensandbox_api_key,
        protocol=settings.protocol,
        use_server_proxy=settings.server_proxy,
    )


def _volume(settings: Settings, name: str, mount: str, sub: str) -> Any:
    # 用 SDK 模型的 alias 名(camelCase):这些字段必填且带 alias,snake_case 虽能运行但过不了类型检查。
    from opensandbox.models.sandboxes import PVC, Host, Volume

    if settings.workspace_claim:
        return Volume(
            name=name,
            pvc=PVC(claimName=settings.workspace_claim, createIfNotExists=True),
            mountPath=mount,
            subPath=sub,
        )
    return Volume(
        name=name,
        host=Host(path=workspace.host_root(settings)),
        mountPath=mount,
        subPath=sub,
    )


def _volumes(settings: Settings, assistant_id: str, thread_id: str) -> list[Any]:
    return [
        _volume(settings, "workspace", workspace.WORKSPACE, f"{assistant_id}/{thread_id}"),
        _volume(settings, "skills", workspace.SKILLS, f"{workspace.DEEPAGENT}/{assistant_id}/skills"),
    ]


async def _discover(settings: Settings, metadata: dict[str, str]) -> Any | None:
    """按 metadata 找已有沙箱:PAUSED 恢复,RUNNING 连接,否则 None。"""
    from opensandbox.manager import SandboxManager
    from opensandbox.models.sandboxes import SandboxFilter, SandboxState
    from opensandbox.sandbox import Sandbox

    connection = _connection(settings)
    async with await SandboxManager.create(connection_config=connection) as manager:
        listing = await manager.list_sandbox_infos(SandboxFilter(metadata=metadata, page_size=10))
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


def _trim(keep: tuple[str, str]) -> None:
    """把已建成的句柄数压回上限:LRU 丢弃最旧的空闲句柄(纯同步,无 await 竞态)。"""
    while sum(slot.backend is not None for slot in _slots.values()) > _MAX_HANDLES:
        victim = next(
            (
                key
                for key, slot in _slots.items()
                if key != keep and slot.backend is not None and not slot.lock.locked()
            ),
            None,
        )
        if victim is None:
            return
        del _slots[victim]


def _forget(key: tuple[str, str], backend: AsyncOpenSandboxBackend) -> None:
    """忘记一个失联句柄,仅当槽内仍是同一实例(避免误删并发刚刷新出的新句柄)。"""
    slot = _slots.get(key)
    if slot is not None and slot.backend is backend:
        slot.backend = None


async def _acquire(settings: Settings, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    """取或建句柄;同 key 并发只开箱一次(双检锁),建成后压回上限。"""
    key = (assistant_id, thread_id)
    slot = _slots.get(key)
    if slot is None:
        slot = _slots[key] = _Slot()
    else:
        _slots.move_to_end(key)

    if slot.backend is None:
        async with slot.lock:
            if slot.backend is None:
                slot.backend = await _open(settings, *key)
                _trim(keep=key)
    return slot.backend


async def _healthy(backend: AsyncOpenSandboxBackend) -> bool:
    """沙箱是否仍可达(``is_healthy`` 已吞异常返回 False,这里再兜一层)。"""
    try:
        return await backend.sandbox.is_healthy()
    except Exception:
        return False


def _failed(result: Any) -> bool:
    """结果是否表征失败(后端从不抛异常,失败以结果对象回传)。"""
    if isinstance(result, ExecuteResponse):
        return result.exit_code != 0
    if isinstance(result, ReadResult):
        return result.error is not None
    if isinstance(result, list):
        return any(getattr(item, "error", None) for item in result)
    return False


class SessionSandbox(BaseSandbox):
    """按 (assistant, thread) 多路复用的沙箱后端;沙箱失联时重新发现并重试一次。"""

    def __init__(self, settings: Settings, assistant_id: str) -> None:
        self._settings = settings
        self._assistant_id = safe_segment(assistant_id, "default")

    @property
    def id(self) -> str:
        return f"agentos-{self._assistant_id}"

    async def _call(self, op: Callable[[AsyncOpenSandboxBackend], Awaitable[Any]]) -> Any:
        key = (self._assistant_id, safe_segment(current_thread_id(), "default"))
        backend = await _acquire(self._settings, *key)
        result = await op(backend)
        if _failed(result) and not await _healthy(backend):
            logger.info("sandbox unreachable, rebuilding (assistant=%s thread=%s)", *key)
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


def session(settings: Settings, assistant_id: str) -> SessionSandbox | None:
    """沙箱后端;``AGENTOS_SANDBOX_ENABLED=false`` 时返回 None(开发态)。"""
    if not settings.sandbox_enabled:
        return None
    return SessionSandbox(settings, assistant_id)
