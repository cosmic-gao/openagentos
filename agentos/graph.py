"""aegra.json 入口：make_graph(config) 按 assistant 构图。

配置全部来自 Aegra assistant 的 config 字段（config.configurable）：model/base_url/api_key/
prompt/mcpServers/skills（缺项回退全局 env）。每线程沙箱（含 execute）与 export_artifact
工具共用同一实例。持久化由 Aegra 运行时注入（不传 checkpointer/store）。
"""

from __future__ import annotations

from agentos import builder, mcp
from agentos import config as cfg
from agentos import sandbox as sbx
from agentos import tools as tls


async def make_graph(config: dict):
    resolved = cfg.resolve(cfg.AgentConfig.parse(cfg.configurable(config)), cfg.get_settings())

    # 每线程沙箱（禁用时 None）；与 export_artifact 共用同一实例，确保命中同一线程容器。
    box = sbx.build_sandbox()
    agent_tools = tls.default_tools()
    if box is not None:
        agent_tools.append(tls.build_export_artifact(box))
    agent_tools += await mcp.tools(resolved.mcp_servers)

    backend, skill_sources = builder.build_backend(resolved.skills, default=box)
    return builder.build(
        resolved=resolved,
        backend=backend,
        tools=agent_tools,
        skill_sources=skill_sources,
    )
