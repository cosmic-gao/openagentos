"""Minimal smoke test: stream one turn against a running Aegra server.

Usage (two terminals):
    uv run aegra dev                     # terminal 1: start server on :2026
    uv run python scripts/smoke_test.py  # terminal 2: send a message
"""

from __future__ import annotations

import asyncio
import os

from langgraph_sdk import get_client


async def main() -> None:
    url = os.environ.get("AEGRA_URL", "http://localhost:2026")
    client = get_client(url=url)

    thread = await client.threads.create()
    print(f"thread: {thread['thread_id']}")

    async for chunk in client.runs.stream(
        thread_id=thread["thread_id"],
        assistant_id="agentos",
        input={
            "messages": [
                {"type": "human", "content": "Say hello and list your built-in tools."}
            ]
        },
        stream_mode="messages",
    ):
        print(f"[{chunk.event}] {chunk.data}")


if __name__ == "__main__":
    asyncio.run(main())
