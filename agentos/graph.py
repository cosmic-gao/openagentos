"""aegra.json 入口:make_graph(config, runtime) 按 assistant 构图。"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph_sdk.runtime import ServerRuntime

from agentos import builder, mcp, sandbox, tools, workspace
from agentos.config import AgentConfig, configurable, get_settings, resolve, safe_segment


def _servers(settings, assistant_id: str) -> dict:
    file = workspace.mcp(settings, assistant_id)
    return mcp.parse(file.read_text(encoding="utf-8") if file.is_file() else None)


def _backend(settings, assistant_id: str) -> tuple[Any, list[str]]:
    return sandbox.session(settings, assistant_id), [workspace.SKILLS]


def _memory(settings, assistant_id: str, base: Any) -> tuple[Any, list[str] | None]:
    """启用记忆时把 base 包进 CompositeBackend,/memories/ 路由到按 assistant 隔离的 StoreBackend。"""
    if not settings.memory_enabled:
        return base, None

    from deepagents.backends import CompositeBackend, StoreBackend

    store = StoreBackend(namespace=lambda _rt: (assistant_id, "memories"))
    backend = CompositeBackend(default=base, routes={f"{workspace.MEMORIES}/": store})
    return backend, [workspace.MEMORY_FILE]


async def make_graph(config: dict, runtime: ServerRuntime) -> Any:
    settings = get_settings()
    conf = configurable(config)
    assistant_id = safe_segment(conf.get("assistant_id"))
    parsed = AgentConfig.model_validate(conf)
    resolved = resolve(parsed, settings)

    executing = runtime is not None and runtime.execution_runtime is not None

    agent_tools = [tools.internet_search, tools.build_download(settings)]
    if executing:
        workspace.ensure(settings, assistant_id)
        agent_tools += await mcp.tools(_servers(settings, assistant_id))

    base, skills = _backend(settings, assistant_id)
    backend, memory = _memory(settings, assistant_id, base)

    # create_deep_agent 全图编译是 ~60ms 纯 CPU:挪出事件循环,避免高并发下各 run 建图相互串行阻塞。
    return await asyncio.to_thread(
        builder.build,
        resolved=resolved,
        settings=settings,
        backend=backend,
        tools=agent_tools,
        skills=skills,
        skills_dir=workspace.skills(settings, assistant_id),
        memory=memory,
    )
