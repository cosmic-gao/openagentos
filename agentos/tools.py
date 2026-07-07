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
