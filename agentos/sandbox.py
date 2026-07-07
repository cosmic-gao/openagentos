"""每线程 OpenSandbox：按 Aegra thread 复用沙箱，空闲到期由 reaper 销毁。

SessionSandbox 传给 create_deep_agent(backend=...)，每次操作按 thread_id 委派到该线程的
AsyncOpenSandboxBackend；SandboxManager 负责创建、复用、空闲回收。沙箱运行时由开源包
deepagents-opensandbox + OpenSandbox 服务提供（不自研）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    execute_accepts_timeout,
)
from deepagents.backends.sandbox import BaseSandbox

from agentos.runtime import current_thread_id

logger = logging.getLogger(__name__)

_SYNC_UNSUPPORTED = "SessionSandbox 仅支持异步执行（Aegra 使用 ainvoke）。"


def thread_key() -> str:
    return current_thread_id()


def _disabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}


@dataclass
class _Entry:
    backend: BaseSandbox
    stack: AsyncExitStack
    last_used: float


@dataclass
class SandboxManager:
    """按 key（thread_id）复用 OpenSandbox 沙箱，空闲到期销毁。"""

    image: str = "python:3.11"
    idle_ttl: float = 1800.0
    sweep_interval: float = 60.0
    command_timeout: int | None = None
    sandbox_lifetime: int | None = None
    create_kwargs: dict = field(default_factory=dict)

    _entries: dict[str, _Entry] = field(default_factory=dict, init=False)
    _pending: dict[str, asyncio.Future] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _reaper: asyncio.Task | None = field(default=None, init=False)

    @classmethod
    def from_env(cls) -> "SandboxManager":
        def _int(name: str) -> int | None:
            raw = os.environ.get(name)
            return int(raw) if raw else None

        return cls(
            image=os.environ.get("AGENTOS_SANDBOX_IMAGE", "python:3.11"),
            idle_ttl=float(os.environ.get("AGENTOS_SANDBOX_IDLE_TTL", "1800")),
            sweep_interval=float(os.environ.get("AGENTOS_SANDBOX_SWEEP", "60")),
            command_timeout=_int("AGENTOS_SANDBOX_TIMEOUT"),
            sandbox_lifetime=_int("AGENTOS_SANDBOX_LIFETIME"),
        )

    async def acquire(self, key: str) -> BaseSandbox:
        """取（或惰性新建）该 key 的沙箱并刷新空闲计时；同 key 并发只创建一次。"""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
                return entry.backend
            pending = self._pending.get(key)
            creator = pending is None
            if creator:
                pending = asyncio.get_running_loop().create_future()
                self._pending[key] = pending

        if not creator:
            return await pending

        try:
            entry = await self._create(key)
        except Exception as exc:
            async with self._lock:
                self._pending.pop(key, None)
            pending.set_exception(exc)
            raise
        async with self._lock:
            self._entries[key] = entry
            self._pending.pop(key, None)
            self._ensure_reaper()
        pending.set_result(entry.backend)
        return entry.backend

    async def evict(self, key: str) -> None:
        async with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None:
            await self._close(key, entry)

    async def aclose(self) -> None:
        async with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        for key, entry in entries:
            await self._close(key, entry)

    async def _create(self, key: str) -> _Entry:
        from deepagents_opensandbox import AsyncOpenSandboxBackend

        create_kwargs = {"image": self.image, **self.create_kwargs}
        if self.command_timeout is not None:
            create_kwargs["default_timeout"] = self.command_timeout
        # create() 的 timeout 是 timedelta（沙箱寿命，服务端兜底）；默认远大于 idle_ttl，让 reaper 作主控。
        lifetime = self.sandbox_lifetime if self.sandbox_lifetime is not None else max(int(self.idle_ttl) * 4, 3600)
        create_kwargs["timeout"] = timedelta(seconds=lifetime)
        # AsyncExitStack 复刻文档用法 `async with await create() as backend`，统一由 aclose 关闭。
        stack = AsyncExitStack()
        backend = await stack.enter_async_context(await AsyncOpenSandboxBackend.create(**create_kwargs))
        logger.info("opensandbox created (key=%s, image=%s)", key, self.image)
        return _Entry(backend=backend, stack=stack, last_used=time.monotonic())

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval)
            try:
                await self._reap_once()
            except Exception:  # noqa: BLE001
                logger.warning("opensandbox reaper sweep failed", exc_info=True)

    async def _reap_once(self) -> None:
        now = time.monotonic()
        async with self._lock:
            stale = [(k, e) for k, e in self._entries.items() if now - e.last_used > self.idle_ttl]
            for key, _ in stale:
                del self._entries[key]
        for key, entry in stale:
            await self._close(key, entry)

    async def _close(self, key: str, entry: _Entry) -> None:
        try:
            await entry.stack.aclose()
        except Exception:  # noqa: BLE001
            logger.warning("closing opensandbox failed (key=%s)", key, exc_info=True)
        else:
            logger.info("opensandbox reaped (key=%s)", key)


class SessionSandbox(BaseSandbox):
    """按 thread 多路复用的沙箱后端。

    只实现异步原语（aexecute/aupload_files/adownload_files）；BaseSandbox 由此派生全部
    异步文件操作。同步方法不支持（Aegra 走 ainvoke）。
    """

    def __init__(self, manager: SandboxManager, key_fn=thread_key) -> None:
        self._manager = manager
        self._key_fn = key_fn

    @property
    def id(self) -> str:
        return "opensandbox-session"

    async def _with_retry(self, make_coro):
        """在当前线程沙箱上执行；若沙箱已被回收则驱逐后用新沙箱重试一次。"""
        key = self._key_fn()
        backend = await self._manager.acquire(key)
        try:
            return await make_coro(backend)
        except Exception:  # noqa: BLE001
            await self._manager.evict(key)
            return await make_coro(await self._manager.acquire(key))

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        def call(backend: BaseSandbox):
            if timeout is not None and execute_accepts_timeout(type(backend)):
                return backend.aexecute(command, timeout=timeout)
            return backend.aexecute(command)

        return await self._with_retry(call)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await self._with_retry(lambda backend: backend.aupload_files(files))

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._with_retry(lambda backend: backend.adownload_files(paths))

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise NotImplementedError(_SYNC_UNSUPPORTED)


def build_sandbox() -> SessionSandbox | None:
    """按 AGENTOS_SANDBOX_ENABLED 构造多路复用沙箱；禁用时返回 None。"""
    if _disabled(os.environ.get("AGENTOS_SANDBOX_ENABLED", "true")):
        return None
    return SessionSandbox(SandboxManager.from_env())
