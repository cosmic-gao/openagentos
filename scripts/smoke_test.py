"""冒烟测试：对运行中的 Aegra 流式跑一轮（先 `uv run aegra dev`）。"""

from __future__ import annotations

import asyncio
import os

from langgraph_sdk import get_client


async def main() -> None:
    client = get_client(url=os.environ.get("AEGRA_URL", "http://localhost:2026"))
    thread = await client.threads.create()
    print(f"thread: {thread['thread_id']}")
    async for chunk in client.runs.stream(
        thread_id=thread["thread_id"],
        assistant_id="agentos",
        input={"messages": [{"type": "human", "content": "Say hello and list your built-in tools."}]},
        stream_mode="messages",
    ):
        print(f"[{chunk.event}] {chunk.data}")


if __name__ == "__main__":
    asyncio.run(main())
