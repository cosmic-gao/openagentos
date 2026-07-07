"""Graph entrypoint — registered in aegra.json as `agentos`.

DeepAgents builds the agent; Aegra hosts it and injects PostgreSQL persistence
(the checkpointer) and the store at runtime. We therefore deliberately do NOT
pass a checkpointer/store here — Aegra owns durability.
"""

from __future__ import annotations

from deepagents import create_deep_agent

from agentos.backends import build_backend
from agentos.model import get_model
from agentos.prompts import SYSTEM_PROMPT
from agentos.subagents import build_subagents
from agentos.tools import default_tools


def build_graph():
    """Construct the compiled DeepAgents graph that Aegra serves."""
    # backend：每线程临时沙箱（execute + 草稿）+ /assistant/ 每助手磁盘目录。
    return create_deep_agent(
        model=get_model(),
        tools=default_tools(),
        system_prompt=SYSTEM_PROMPT,
        subagents=build_subagents(),
        backend=build_backend(),
    )


# Aegra loads this module-level compiled graph at startup.
graph = build_graph()


# ─────────────────────────────────────────────────────────────────────────────
# Enabling MCP tools (QuickBooks / ClickUp / your own servers)
# ─────────────────────────────────────────────────────────────────────────────
# MCP tools load asynchronously, so expose an async factory and point aegra.json
# at it instead of the `graph` above:
#
#     "graphs": { "agentos": "./agentos/graph.py:make_graph" }
#
# async def make_graph():
#     from agentos.mcp_tools import load_mcp_tools
#     tools = default_tools() + await load_mcp_tools()
#     return create_deep_agent(
#         model=get_model(),
#         tools=tools,
#         system_prompt=SYSTEM_PROMPT,
#         subagents=build_subagents(),
#     )
