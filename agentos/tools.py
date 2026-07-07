"""agent 工具（DeepAgents 内置工具之外）。"""

from __future__ import annotations

import os
from typing import Literal


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


def default_tools() -> list:
    return [internet_search]


def build_export_artifact(sandbox):
    """构造 `export_artifact` 工具，闭包捕获与 backend 同一个 SessionSandbox。

    工具把沙箱内文件字节 download 出来，写到 thread 作用域的产物目录，返回下载 URL。
    与 backend 共用同一沙箱实例，确保命中同一线程容器（否则会连到另一个沙箱）。
    """
    import posixpath

    from agentos import artifacts
    from agentos.config import current_thread_id

    async def export_artifact(path: str, filename: str | None = None) -> str:
        """Export a file from the sandbox so the user can download it.

        Use this for deliverable files the user should receive (reports,
        spreadsheets, images, archives). Returns a download URL to give the
        user. Do NOT export scratch or intermediate files — keep those in the
        filesystem.

        Args:
            path: Absolute path of the file inside the sandbox (e.g. "/work/report.xlsx").
            filename: Optional download name; defaults to the file's base name.
        """
        responses = await sandbox.adownload_files([path])
        if not responses or responses[0].content is None:
            detail = responses[0].error if responses else "no response"
            return f"Failed to export '{path}': {detail}"
        data = responses[0].content
        name = filename or posixpath.basename(path) or "artifact"
        rel = artifacts.store_bytes(current_thread_id(), name, data)
        return f"Exported '{path}' ({len(data)} bytes). Download URL to give the user: {artifacts.public_url(rel)}"

    return export_artifact
