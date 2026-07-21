"""agent 工具:把会话产物交给用户下载。

- download_file —— 会话交付物拷进 per-user Store,返回按用户隔离的下载直链(取回需鉴权身份)。
- download_skill —— skill 包拷进 assistant 共享盘(.deepagent/<aid>/skills/),返回免鉴权的资产直链
  (skill 是 assistant 级共享资产,非会话级交付物,故不进 per-user Store、不带 thread)。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import quote

from agentos import artifacts, assets, sandbox, workspace
from agentos.config import Settings, current_thread_id


def relative(path: str) -> str:
    """沙箱路径 → /workspace 内相对路径。"""
    parts = [p for p in PurePosixPath(path).parts if p not in ("/", ".")]
    if parts[:1] == ["workspace"]:
        parts = parts[1:]
    return "/".join(parts)


async def _fetch(settings: Settings, assistant_id: str, identity: str, path: str) -> bytes | None:
    """从沙箱取回单个文件字节;路径穿越/不存在/读失败一律 None。"""
    if not relative(path) or ".." in PurePosixPath(path).parts:  # 拒绝路径穿越
        return None
    results = await sandbox.session(settings, assistant_id, identity).adownload_files([path])
    result = results[0] if results else None
    if result is None or result.error or result.content is None:
        return None
    return result.content


def build_download(settings: Settings, assistant_id: str, identity: str) -> Callable[[str], Awaitable[str]]:
    async def download_file(path: str) -> str:
        """Give the user a download link for a file in /workspace.

        Use this for per-user deliverables the user should receive (reports,
        spreadsheets, images, archives). The file is copied from the sandbox
        into durable storage and a download link is returned to hand to the
        user. For a packaged skill use download_skill instead. Do NOT expose
        scratch or intermediate files.

        Args:
            path: Path of the file inside the sandbox (e.g. "/workspace/report.xlsx").
        """
        content = await _fetch(settings, assistant_id, identity, path)
        if content is None:
            return f"File not found: {path!r}"
        rel = relative(path)
        thread_id = current_thread_id()
        if not await artifacts.save(identity, assistant_id, thread_id, rel, content):
            return "Download unavailable: artifact store not configured."
        base = settings.public_url.rstrip("/")
        return f"Download link for the user: {base}/files/{quote(assistant_id)}/{quote(thread_id)}/{quote(rel)}"

    return download_file


def build_download_skill(settings: Settings, assistant_id: str, identity: str) -> Callable[[str], Awaitable[str]]:
    async def download_skill(path: str) -> str:
        """Give the user a download link for a skill package.

        A skill is an assistant-shared asset, not a per-user deliverable. Use
        this for a packaged skill (a `.skill` file or an archive of a skill
        directory): the package is copied into the assistant's shared skills
        area and a stable, assistant-scoped download link is returned that any
        recipient of the link can open. For regular per-user deliverables use
        download_file instead.

        Args:
            path: Path of the skill package inside the sandbox (e.g. "/workspace/foo.skill").
        """
        content = await _fetch(settings, assistant_id, identity, path)
        if content is None:
            return f"File not found: {path!r}"
        name = PurePosixPath(relative(path)).name
        base_dir = workspace.assistant(settings, assistant_id)
        # 拷进共享盘 skills/(磁盘 I/O 挪出事件循环);资产端点按 assistant 隔离、免 identity 即可取回。
        stored = await asyncio.to_thread(assets.save, base_dir, f"skills/{name}", content)
        base = settings.public_url.rstrip("/")
        return f"Download link for the user: {base}/assistants/{quote(assistant_id)}/files/{quote(stored)}"

    return download_skill
