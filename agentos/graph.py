"""aegra.json 入口：make_graph(config) 按 assistant 构图（Aegra 每请求以该助手 config 调用）。

按助手装配 model（config.json）、skills（/assistant/skills）、MCP tools（.mcp.json）、backend。
持久化由 Aegra 运行时注入（不传 checkpointer/store）。
"""

from __future__ import annotations

from deepagents import create_deep_agent

from agentos import workspace
from agentos.backends import build_backend
from agentos.mcp_tools import load_mcp_tools
from agentos.model import model_from_config
from agentos.prompts import SYSTEM_PROMPT
from agentos.runtime import assistant_id_from_config
from agentos.subagents import build_subagents
from agentos.tools import default_tools


async def make_graph(config: dict):
    assistant_id = assistant_id_from_config(config)
    workspace.ensure_assistant(assistant_id)
    model = model_from_config(workspace.load_config(assistant_id))
    tools = default_tools() + await load_mcp_tools(assistant_id)
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        subagents=build_subagents(model),
        backend=build_backend(assistant_id),
        skills=["/assistant/skills"],
    )
