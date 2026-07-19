"""aegra.json 入口:make_graph(config, runtime) 按 assistant 构图。"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph_sdk.runtime import ServerRuntime

from agentos import builder, mcp, sandbox, tools, workspace
from agentos.config import AgentConfig, Settings, configurable, get_settings, resolve, safe_segment


def _servers(settings: Settings, assistant_id: str) -> dict[str, Any]:
    file = workspace.mcp(settings, assistant_id)
    return mcp.parse(file.read_text(encoding="utf-8") if file.is_file() else None)


def _identity(conf: dict[str, Any], runtime: ServerRuntime | None) -> str:
    """当前请求的 user identity(具体组成由 auth.py 决定;缺失回退 anonymous)——记忆按用户隔离用。"""
    user = getattr(runtime, "user", None)
    return getattr(user, "identity", None) or conf.get("user_id") or "anonymous"


def _backend(settings: Settings, assistant_id: str, identity: str) -> tuple[Any, list[str], list[str] | None]:
    """组合后端:default=沙箱(execute + /workspace 文件);/workspace/skills → 宿主盘直读(skills 枚举/读取
    不拉起沙箱,沙箱只在真正 execute/文件操作时才启);/memories → Store(按 user+assistant 隔离、跨线程持久)。skills 仍挂进沙箱供 execute。"""
    from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend

    routes: dict[str, Any] = {
        f"{workspace.SKILLS}/": FilesystemBackend(
            root_dir=str(workspace.skills(settings, assistant_id)), virtual_mode=True
        ),
    }
    memory: list[str] | None = None
    if settings.memory_enabled:
        # 每用户每助手一份记忆:按 aegra 官方 store 布局 ["users", <user_id>, …] 组织,与 REST /store 同一棵用户树。
        # 图运行时 store 不自动 scope(仅 REST 层加 ["users",id] 前缀),故此处显式带 "users"。identity 用 make_graph
        # 闭包捕获——LGP 惯用的 rt.server_info.* 在 aegra 恒 None(会 AttributeError),不能用。
        routes[f"{workspace.MEMORIES}/"] = StoreBackend(
            namespace=lambda _rt: ("users", identity, assistant_id, "memories")
        )
        memory = [workspace.MEMORY_FILE]
    backend = CompositeBackend(default=sandbox.session(settings, assistant_id, identity), routes=routes)
    return backend, [workspace.SKILLS], memory


async def make_graph(config: dict, runtime: ServerRuntime) -> Any:
    settings = get_settings()
    conf = configurable(config)
    assistant_id = safe_segment(conf.get("assistant_id"))
    identity = _identity(conf, runtime)
    parsed = AgentConfig.model_validate(conf)
    resolved = resolve(parsed, settings)

    # 仅真正执行(execution_runtime 非 None)才连 MCP、写盘;schema/画图等只读调用走轻量路径,图拓扑不变。
    executing = runtime is not None and runtime.execution_runtime is not None

    agent_tools = [tools.build_download(settings, assistant_id, identity)]
    if executing:
        workspace.ensure(settings, assistant_id)
        agent_tools += await mcp.tools(_servers(settings, assistant_id))

    backend, skills, memory = _backend(settings, assistant_id, identity)

    # create_deep_agent 建图 ~60ms 纯 CPU:挪出事件循环,避免高并发下各 run 建图串行阻塞。
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
