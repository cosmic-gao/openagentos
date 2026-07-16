"""每线程临时沙箱:按 metadata 发现(connect/resume)或新建,服务端 TTL 到期自动销毁。

进程级共享一个 httpx transport(全沙箱共用连接池)与一个长驻 SandboxManager(发现复用);
按 (assistant, thread) 多路复用后端句柄,失联时重新发现并重试一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    ReadResult,
)
from deepagents.backends.sandbox import BaseSandbox
from deepagents_opensandbox import AsyncOpenSandboxBackend

from agentos import workspace
from agentos.config import Settings, current_thread_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ASYNC_ONLY = "SessionSandbox is async-only (Aegra invokes graphs with ainvoke)."
_MAX_SLOTS = 256

_transport: Any = None
_manager: Any = None


@dataclass
class _Slot:
    """每 (assistant, thread) 一个槽:开箱锁 + 惰性句柄 + 上次续期的单调时刻。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    backend: AsyncOpenSandboxBackend | None = None
    renewed: float = 0.0


_slots: OrderedDict[tuple[str, str], _Slot] = OrderedDict()


def _connection(settings: Settings) -> Any:
    """连接配置;进程级惰性建共享 transport(须在事件循环内首建),SDK 视为用户所有故不关闭它。"""
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


def _volumes(settings: Settings, assistant_id: str, thread_id: str) -> list[Any]:
    """只挂持久卷:workspace(会话产物)+ skills(助手技能)。/tmp 不挂卷,落沙箱容器本地、随箱销毁。"""
    ws = workspace.under(settings, workspace.storage(settings, thread_id))
    sk = workspace.under(settings, workspace.skills(settings, assistant_id))
    return [
        _volume(settings, "workspace", workspace.WORKSPACE, ws),
        _volume(settings, "skills", workspace.SKILLS, sk),
    ]


async def _discover(settings: Settings, metadata: dict[str, str]) -> Any | None:
    """按 metadata 找 RUNNING/PAUSED 沙箱:PAUSED 恢复、RUNNING 连接,否则 None。"""
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


async def _open(settings: Settings, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    metadata = {"agentos.assistant": assistant_id, "agentos.thread": thread_id}
    workspace.storage(settings, thread_id).mkdir(parents=True, exist_ok=True)

    existing = await _discover(settings, metadata)
    if existing is not None:
        return AsyncOpenSandboxBackend(existing, default_timeout=settings.sandbox_timeout)

    backend = await AsyncOpenSandboxBackend.create(
        settings.sandbox_image,
        connection_config=_connection(settings),
        timeout=timedelta(seconds=settings.sandbox_ttl),
        default_timeout=settings.sandbox_timeout,
        resource=_resource(settings),
        volumes=_volumes(settings, assistant_id, thread_id),
        metadata=metadata,
    )
    logger.info("sandbox created (assistant=%s thread=%s)", assistant_id, thread_id)
    return backend


def _trim(keep: tuple[str, str]) -> None:
    """压回上限:LRU 丢弃最旧的空闲槽(未加锁);thread_id 随会话增长,必须限界。"""
    while len(_slots) > _MAX_SLOTS:
        victim = next(
            (key for key, slot in _slots.items() if key != keep and not slot.lock.locked()),
            None,
        )
        if victim is None:
            return
        del _slots[victim]


def _forget(key: tuple[str, str], backend: AsyncOpenSandboxBackend) -> None:
    """忘记失联句柄;仅当槽内仍是同一实例(避免误删并发新句柄)。"""
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


async def _acquire(settings: Settings, assistant_id: str, thread_id: str) -> AsyncOpenSandboxBackend:
    """取或建句柄;同 key 并发只开箱一次(双检锁),建成后压回上限并按需续期。"""
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
    """结果是否表征失败(后端从不抛异常,失败以结果对象回传)。"""
    if isinstance(result, ExecuteResponse):
        return result.exit_code != 0
    if isinstance(result, ReadResult):
        return result.error is not None
    if isinstance(result, list):
        return any(getattr(item, "error", None) for item in result)
    return False


class SessionSandbox(BaseSandbox):
    """按 (assistant, thread) 多路复用的沙箱后端;失联时重新发现并重试一次。"""

    def __init__(self, settings: Settings, assistant_id: str) -> None:
        self._settings = settings
        self._assistant = assistant_id

    @property
    def id(self) -> str:
        return f"agentos-{self._assistant}"

    async def _call(self, op: Callable[[AsyncOpenSandboxBackend], Awaitable[Any]]) -> Any:
        key = (self._assistant, current_thread_id())
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


def session(settings: Settings, assistant_id: str) -> SessionSandbox:
    """按 (assistant, thread) 多路复用的每线程沙箱后端。"""
    return SessionSandbox(settings, assistant_id)


_RUNNERS: dict[str, tuple[str, str]] = {
    "python": ("py", "python"),
    "bash": ("sh", "bash"),
    "sh": ("sh", "sh"),
}

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _command(
    interp: str,
    path: str,
    args: list[str],
    env: dict[str, str],
    stdin_path: str | None,
) -> str:
    prefix = ["env", *(f"{name}={value}" for name, value in env.items())] if env else []
    command = " ".join(shlex.quote(token) for token in (*prefix, interp, path, *args))
    if stdin_path is not None:
        command += f" < {shlex.quote(stdin_path)}"
    return command


async def run(
    settings: Settings,
    code: str,
    *,
    language: str = "python",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    params: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> ExecuteResponse:
    """新建临时沙箱执行 code、完成即销毁——单次、无状态、不挂持久卷。"""
    runner = _RUNNERS.get(language.lower())
    if runner is None:
        raise ValueError(f"unsupported language {language!r}; expected one of {sorted(_RUNNERS)}")
    env = env or {}
    for name in env:
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name {name!r}")
    ext, interp = runner

    if params:
        params_json = json.dumps(params)
        if interp == "python":
            code = f"params = __import__('json').loads({params_json!r})\n" + code
        else:
            env = {**env, "PARAMS": params_json}

    backend = await AsyncOpenSandboxBackend.create(
        settings.sandbox_image,
        connection_config=_connection(settings),
        timeout=timedelta(seconds=settings.sandbox_ttl),
        default_timeout=settings.sandbox_timeout,
        resource=_resource(settings),
    )
    try:
        path = f"/tmp/{uuid4().hex}.{ext}"
        blobs = [(path, code.encode())]
        stdin_path = None
        if stdin is not None:
            stdin_path = f"/tmp/{uuid4().hex}.stdin"
            blobs.append((stdin_path, stdin.encode()))
        staged = await backend.aupload_files(blobs)
        failed = next((s for s in staged if s.error), None)
        if failed is not None:
            raise OSError(f"failed to stage {failed.path} in sandbox: {failed.error}")
        command = _command(interp, path, args or [], env, stdin_path)
        return await backend.aexecute(command, timeout=timeout)
    finally:
        await backend.aclose()
