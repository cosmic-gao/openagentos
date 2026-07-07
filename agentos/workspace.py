"""每助手磁盘隔离：按 assistantId 在磁盘建目录，skills/、mcp.json 按目录隔离。

布局（`AGENTOS_DATA_DIR`，默认 `./data`）：

    data/assistants/<assistantId>/
        skills/        # 该助手的 skills
        mcp.json       # 该助手的 MCP 服务器配置

`AssistantBackend` 挂在 CompositeBackend 的 `/assistant/` 路由下：agent 看到的
`/assistant/skills/x.md` 会落到 `data/assistants/<id>/skills/x.md`。真实磁盘读写复用
deepagents 的 `FilesystemBackend`（`virtual_mode=True` 锚定到助手根目录并阻断路径穿越），
不自研文件操作。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from agentos.runtime import current_assistant_id

# CompositeBackend 路由前缀（会被剥离后转发给本后端）。
ASSISTANT_ROUTE = "/assistant/"

# 仅允许安全字符，防止 assistantId 越权造成路径穿越。
_UNSAFE = re.compile(r"[^A-Za-z0-9._@+:~-]")


def data_root() -> Path:
    """数据根目录（`AGENTOS_DATA_DIR`，默认 `./data`）。"""
    return Path(os.environ.get("AGENTOS_DATA_DIR", "./data")).resolve()


def _safe_id(assistant_id: str) -> str:
    cleaned = _UNSAFE.sub("_", assistant_id or "").strip("._") or "default"
    return cleaned


def assistant_dir(assistant_id: str) -> Path:
    """该助手的磁盘目录（不保证已存在，见 `ensure_assistant`）。"""
    return data_root() / "assistants" / _safe_id(assistant_id)


def ensure_assistant(assistant_id: str) -> Path:
    """确保助手目录及其 `skills/`、默认 `mcp.json` 存在，返回该目录。"""
    root = assistant_dir(assistant_id)
    (root / "skills").mkdir(parents=True, exist_ok=True)
    mcp = root / "mcp.json"
    if not mcp.exists():
        mcp.write_text(json.dumps({"mcpServers": {}}, indent=2, ensure_ascii=False), encoding="utf-8")
    return root


class AssistantBackend(BackendProtocol):
    """按 assistantId 隔离的磁盘后端，转发到对应目录的 FilesystemBackend。"""

    def __init__(self) -> None:
        self._cache: dict[str, FilesystemBackend] = {}

    def _fs(self) -> FilesystemBackend:
        assistant_id = current_assistant_id()
        backend = self._cache.get(assistant_id)
        if backend is None:
            root = ensure_assistant(assistant_id)
            backend = FilesystemBackend(root_dir=root, virtual_mode=True)
            self._cache[assistant_id] = backend
        return backend

    # -- 同步 --
    def ls(self, path: str) -> LsResult:
        return self._fs().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._fs().read(file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._fs().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self._fs().edit(file_path, old_string, new_string, replace_all)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._fs().glob(pattern, path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self._fs().grep(pattern, path, glob)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._fs().upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._fs().download_files(paths)

    # -- 异步（FilesystemBackend 由 BackendProtocol 提供 to_thread 版本）--
    async def als(self, path: str) -> LsResult:
        return await self._fs().als(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await self._fs().aread(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await self._fs().awrite(file_path, content)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return await self._fs().aedit(file_path, old_string, new_string, replace_all)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await self._fs().aglob(pattern, path)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return await self._fs().agrep(pattern, path, glob)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return await self._fs().aupload_files(files)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return await self._fs().adownload_files(paths)


def assistant_mcp_config(assistant_id: str | None = None) -> dict:
    """读取该助手 `mcp.json` 中的 MCP 服务器配置（隔离配置）。

    返回形如 `{"server": {...}}` 的 dict；文件不存在或非法时返回 `{}`。
    """
    assistant_id = assistant_id or current_assistant_id()
    path = assistant_dir(assistant_id) / "mcp.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    return servers if isinstance(servers, dict) else {}
