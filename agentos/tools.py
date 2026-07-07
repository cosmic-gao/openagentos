"""Tools available to OpenAgentOS agents (in addition to DeepAgents built-ins).

`internet_search` uses Tavily when TAVILY_API_KEY is set; otherwise it returns a
clear message so the agent degrades gracefully instead of crashing.
"""

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
        return (
            "Web search is not configured. Set TAVILY_API_KEY in the environment "
            "to enable internet_search."
        )

    from tavily import TavilyClient

    client = TavilyClient(api_key=api_key)
    response = client.search(query, max_results=max_results, topic=topic)
    results = response.get("results", []) if isinstance(response, dict) else []
    if not results:
        return f"No results for: {query}"

    formatted = []
    for i, result in enumerate(results, start=1):
        title = result.get("title", "(no title)")
        url = result.get("url", "")
        snippet = (result.get("content") or "").strip()
        formatted.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(formatted)


def default_tools() -> list:
    """Tools exposed to the main agent, on top of the DeepAgents built-ins."""
    return [internet_search]
