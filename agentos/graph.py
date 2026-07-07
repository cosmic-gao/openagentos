"""aegra.json 入口:make_graph(config) 按 assistant 构图。

配置来自 assistant 的 config.configurable(model/prompt/api_key/base_url/assistant_id,
缺项回退 OPENAI_* env);MCP servers 来自共享磁盘 ``.deepagent/<aid>/.mcp.json``;
skills 由沙箱挂载 ``/workspace/skills`` 提供。持久化由 Aegra 运行时注入。
"""

from __future__ import annotations

from agentos import builder, mcp, sandbox, tools, workspace
from agentos.config import AgentConfig, configurable, get_settings, resolve, safe_segment


def _servers(settings, assistant_id: str) -> dict:
    file = workspace.mcp(settings, assistant_id)
    return mcp.parse(file.read_text(encoding="utf-8") if file.is_file() else None)


async def make_graph(config: dict):
    settings = get_settings()
    conf = configurable(config)
    assistant_id = safe_segment(conf.get("assistant_id"), "default")
    resolved = resolve(AgentConfig.parse(conf), settings)

    workspace.ensure(settings, assistant_id)
    agent_tools = [tools.internet_search, tools.build_share(settings, assistant_id)]
    agent_tools += await mcp.tools(_servers(settings, assistant_id))

    box = sandbox.session(settings, assistant_id)
    if box is not None:
        return builder.build(
            resolved=resolved, backend=box, tools=agent_tools, skills=[workspace.SKILLS]
        )

    from deepagents.backends import StateBackend

    return builder.build(
        resolved=resolved, backend=StateBackend(), tools=agent_tools, skills=None
    )
