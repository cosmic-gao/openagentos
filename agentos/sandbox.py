"""每线程沙箱：把 OpenSandbox 容器按 Aegra thread 复用，空闲到期销毁。

- `SandboxManager`：按 key（线程 id）维护一池 `AsyncOpenSandboxBackend`，惰性创建、
  访问刷新，后台 reaper 每 `sweep_interval` 秒关闭空闲超过 `idle_ttl` 的沙箱。
  OpenSandbox 自身的 `create(timeout=…)` 作进程崩溃/退出时的服务端兜底。
- `SessionSandbox`：传给 `create_deep_agent(backend=…)` 的单例多路复用后端。每次操作
  用 `thread_key()` 解析线程，委派该线程的沙箱。只实现异步原语
  （`aexecute`/`aupload_files`/`adownload_files`），deepagents 的 `BaseSandbox` 由此
  派生全部异步文件操作；同步方法不支持（Aegra 走 `ainvoke`）。

沙箱运行时不自研——由开源包 `deepagents-opensandbox` + OpenSandbox 服务提供。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

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
    """沙箱池的 key：当前 Aegra thread id。"""
    return current_thread_id()


def _is_disabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}


@dataclass
class _Entry:
    """池中一个沙箱的存活记录。"""

    backend: BaseSandbox
    stack: AsyncExitStack
    last_used: float


@dataclass
class SandboxManager:
    """按 key 复用 OpenSandbox 沙箱，空闲到期销毁。"""

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
        """从 `AGENTOS_SANDBOX_*` 环境变量构造。"""

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
        """取（或惰性创建）该 key 的沙箱后端，并刷新空闲计时。

        同 key 的并发请求只创建一次（用 pending future 去重）；不同 key 并行创建
        （只在极短临界区持锁）。
        """
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
        except Exception as exc:  # 创建失败：唤醒等待者并清理
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

    async def _create(self, key: str) -> _Entry:
        # 延迟导入：未装 deepagents-opensandbox 时也能导入本模块（沙箱可关闭）。
        from deepagents_opensandbox import AsyncOpenSandboxBackend

        create_kwargs = {"image": self.image, **self.create_kwargs}
        if self.command_timeout is not None:
            create_kwargs["default_timeout"] = self.command_timeout
        if self.sandbox_lifetime is not None:
            create_kwargs["timeout"] = self.sandbox_lifetime

        # 用 AsyncExitStack 严格复刻文档用法 `async with await create() as backend`。
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
            except Exception:  # noqa: BLE001 — reaper 不应因单次异常退出
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
        except Exception:  # noqa: BLE001 — 关闭失败不应影响其余
            logger.warning("closing opensandbox failed (key=%s)", key, exc_info=True)
        else:
            logger.info("opensandbox reaped (key=%s)", key)

    async def aclose(self) -> None:
        """关闭全部沙箱（用于优雅停机）。"""
        async with self._lock:
            entries = list(self._entries.items())
            self._entries.clear()
        for key, entry in entries:
            await self._close(key, entry)


class SessionSandbox(BaseSandbox):
    """按 thread 多路复用的沙箱后端（传给 create_deep_agent 的单例）。"""

    def __init__(self, manager: SandboxManager, key_fn=thread_key) -> None:
        self._manager = manager
        self._key_fn = key_fn

    @property
    def id(self) -> str:
        return "opensandbox-session"

    async def _backend(self) -> BaseSandbox:
        return await self._manager.acquire(self._key_fn())

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        backend = await self._backend()
        if timeout is not None and execute_accepts_timeout(type(backend)):
            return await backend.aexecute(command, timeout=timeout)
        return await backend.aexecute(command)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        backend = await self._backend()
        return await backend.aupload_files(files)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        backend = await self._backend()
        return await backend.adownload_files(paths)

    # -- 同步不支持：Aegra 走 ainvoke，只会命中 a* 方法 --
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        raise NotImplementedError(_SYNC_UNSUPPORTED)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        raise NotImplementedError(_SYNC_UNSUPPORTED)


def build_sandbox() -> SessionSandbox | None:
    """按环境构造多路复用沙箱后端；`AGENTOS_SANDBOX_ENABLED=false` 时返回 None。"""
    if _is_disabled(os.environ.get("AGENTOS_SANDBOX_ENABLED", "true")):
        return None
    return SessionSandbox(SandboxManager.from_env())
