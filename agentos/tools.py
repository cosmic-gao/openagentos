"""agent 工具:download_file——把会话交付物从 ephemeral 沙箱拷进 Store 并给出下载直链。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import quote

from agentos import artifacts, sandbox
from agentos.config import Settings, current_thread_id


def relative(path: str) -> str:
    """沙箱路径 → /workspace 内相对路径。"""
    parts = [p for p in PurePosixPath(path).parts if p not in ("/", ".")]
    if parts[:1] == ["workspace"]:
        parts = parts[1:]
    return "/".join(parts)


def build_download(settings: Settings, assistant_id: str, identity: str) -> Callable[[str], Awaitable[str]]:
    async def download_file(path: str) -> str:
        """Give the user a download link for a file in /workspace.

        Use this for deliverables the user should receive (reports,
        spreadsheets, images, archives). The file is copied from the sandbox
        into durable storage and a download link is returned to hand to the
        user. Do NOT expose scratch or intermediate files.

        Args:
            path: Path of the file inside the sandbox (e.g. "/workspace/report.xlsx").
        """
        rel = relative(path)
        if not rel or ".." in PurePosixPath(path).parts:  # 拒绝路径穿越
            return f"File not found: {path!r}"
        thread_id = current_thread_id()
        results = await sandbox.session(settings, assistant_id, identity).adownload_files([path])
        result = results[0] if results else None
        if result is None or result.error or result.content is None:
            return f"File not found: {path!r}"
        if not await artifacts.save(identity, assistant_id, thread_id, rel, result.content):
            return "Download unavailable: artifact store not configured."
        base = settings.public_url.rstrip("/")
        return f"Download link for the user: {base}/files/{quote(assistant_id)}/{quote(thread_id)}/{quote(rel)}"

    return download_file
