"""agent 工具:internet_search 与 share_file(共享磁盘直链,无需搬运字节)。"""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from agentos import workspace
from agentos.config import Settings, current_thread_id


def internet_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
) -> str:
    """Search the public web for current information.

    Args:
        query: The search query.
        max_results: Maximum number of results to return (default 5).
        topic: Search category — "general", "news", or "finance".
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return "Web search is not configured. Set TAVILY_API_KEY to enable internet_search."

    from tavily import TavilyClient

    response = TavilyClient(api_key=api_key).search(query, max_results=max_results, topic=topic)
    results = response.get("results", []) if isinstance(response, dict) else []
    if not results:
        return f"No results for: {query}"
    return "\n\n".join(
        f"{i}. {r.get('title', '(no title)')}\n   {r.get('url', '')}\n   {(r.get('content') or '').strip()}"
        for i, r in enumerate(results, start=1)
    )


def relative(path: str) -> str:
    """沙箱路径 → /workspace 内相对路径。"""
    parts = [p for p in PurePosixPath(path).parts if p not in ("/", ".")]
    if parts[:1] == ["workspace"]:
        parts = parts[1:]
    return "/".join(parts)


def build_share(settings: Settings, assistant_id: str):
    """构造 share_file 工具(文件已在共享磁盘,直链下载)。"""

    def share_file(path: str) -> str:
        """Share a file from /workspace so the user can download it.

        Use this for deliverables the user should receive (reports,
        spreadsheets, images, archives). Returns a download link to give the
        user. Do NOT share scratch or intermediate files.

        Args:
            path: Path of the file inside the sandbox (e.g. "/workspace/report.xlsx").
        """
        rel = relative(path)
        thread_id = current_thread_id()
        try:
            target = workspace.contained(workspace.thread(settings, assistant_id, thread_id), rel)
        except ValueError:
            return f"File not found: {path!r}"
        if not target.is_file():
            return f"File not found: {path!r}"
        base = settings.public_url.rstrip("/")
        return f"Download link for the user: {base}/files/{assistant_id}/{thread_id}/{quote(rel)}"

    return share_file
