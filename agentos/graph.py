"""aegra.json 入口:make_graph(config, runtime) 按 assistant 构图,并区分 introspection/执行。

配置来自 assistant 的 config.configurable(model/prompt/api_key/base_url/assistant_id/
interrupt_on,缺项回退 OPENAI_* env);MCP servers 来自共享磁盘 ``.deepagent/<aid>/.mcp.json``;
skills 由沙箱挂载 ``/workspace/skills`` 提供;长期记忆经 CompositeBackend 把 ``/memories/``
路由到跨线程持久的 StoreBackend。持久化(checkpointer/store)由 Aegra 运行时注入。

runtime 感知:仅在真正执行(``runtime.execution_runtime`` 非 None)时才做 MCP 载入与磁盘
初始化;schema/画图等 introspection 调用走轻量路径(不连 MCP、不写盘)。图拓扑保持一致
(backend/skills/memory/interrupt_on 结构不变,仅工具集在 introspection 时省去 MCP 工具,
工具集不影响 state schema 与节点结构)。
"""

from __future__ import annotations

from typing import Any

from langgraph_sdk.runtime import ServerRuntime

from agentos import builder, mcp, sandbox, tools, workspace
from agentos.config import AgentConfig, configurable, get_settings, resolve, safe_segment


def _servers(settings, assistant_id: str) -> dict:
    file = workspace.mcp(settings, assistant_id)
    return mcp.parse(file.read_text(encoding="utf-8") if file.is_file() else None)


def _base_backend(settings, assistant_id: str) -> tuple[Any, list[str] | None]:
    """基础 backend:沙箱可用则每线程沙箱(带 skills),否则 StateBackend(无 skills)。"""
    box = sandbox.session(settings, assistant_id)
    if box is not None:
        return box, [workspace.SKILLS]

    from deepagents.backends import StateBackend

    return StateBackend(), None


def _with_memory(settings, assistant_id: str, base: Any) -> tuple[Any, list[str] | None]:
    """启用记忆时把 base 包进 CompositeBackend,并把 /memories/ 路由到 StoreBackend。

    namespace 按 assistant 隔离(跨该 assistant 所有线程共享);store 由 Aegra 注入,构图期
    不触碰(introspection 安全)。deepagents 对 CompositeBackend 会解包按 ``default`` 判定
    execute 工具,故此包裹不改变 execute 的有无。返回 (backend, memory_sources)。
    """
    if not settings.memory_enabled:
        return base, None

    from deepagents.backends import CompositeBackend, StoreBackend

    store = StoreBackend(namespace=lambda _rt: (assistant_id, "memories"))
    backend = CompositeBackend(default=base, routes={f"{workspace.MEMORIES}/": store})
    return backend, [workspace.MEMORY_FILE]


async def make_graph(config: dict, runtime: ServerRuntime) -> Any:
    settings = get_settings()
    conf = configurable(config)
    assistant_id = safe_segment(conf.get("assistant_id"), "default")
    parsed = AgentConfig.parse(conf)
    resolved = resolve(parsed, settings)

    executing = runtime is None or runtime.execution_runtime is not None

    agent_tools = [tools.internet_search, tools.build_share(settings, assistant_id)]
    if executing:
        # 仅执行态做副作用:确保助手目录/模板存在 + 载入 MCP 工具(网络/子进程,可能失败)。
        workspace.ensure(settings, assistant_id)
        agent_tools += await mcp.tools(_servers(settings, assistant_id))

    base, skills = _base_backend(settings, assistant_id)
    backend, memory = _with_memory(settings, assistant_id, base)

    return builder.build(
        resolved=resolved,
        backend=backend,
        tools=agent_tools,
        skills=skills,
        memory=memory,
        interrupt_on=parsed.interrupt_on,
    )
