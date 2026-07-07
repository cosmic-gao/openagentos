"""组装 agent：backend 组合 + 子代理 + create_deep_agent。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from deepagents import SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.filesystem import FilesystemBackend

from agentos import model
from agentos.config import RESEARCH_PROMPT, SYSTEM_PROMPT, ResolvedConfig
from agentos.tools import internet_search

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def build_backend(skill_paths: list[str], *, default: Any = None):
    """返回 (backend, skills 源虚拟路径)。

    default 由调用方注入（graph 传入每线程沙箱，好让 export 工具与后端共用同一实例）；
    缺省回退 StateBackend（沙箱禁用时的无 execute 开发态）。每个 skills 源目录挂到
    `/skills/<name>/` 供 agent 读取与 SkillsMiddleware 加载。
    """
    if default is None:
        default = StateBackend()
    routes: dict = {}
    sources: list[str] = []
    used: set[str] = set()
    for path in skill_paths:
        name = _UNSAFE.sub("_", Path(path).name) or "skills"
        unique, i = name, 1
        while unique in used:
            unique, i = f"{name}-{i}", i + 1
        used.add(unique)
        routes[f"/skills/{unique}/"] = FilesystemBackend(root_dir=path, virtual_mode=True)
        sources.append(f"/skills/{unique}")
    if not routes:
        return default, []
    return CompositeBackend(default=default, routes=routes), sources


def _subagents(llm: Any) -> list[SubAgent]:
    research: SubAgent = {
        "name": "research-agent",
        "description": (
            "Delegate deep, self-contained web research and multi-source "
            "fact-finding here. Provide a precise, standalone question; it "
            "returns a synthesized, cited answer."
        ),
        "system_prompt": RESEARCH_PROMPT,
        "tools": [internet_search],
        "model": llm,
    }
    return [research]


def build(*, resolved: ResolvedConfig, backend: Any, tools: list, skill_sources: list[str]) -> Any:
    """按 resolved 配置构造并返回已编译的 DeepAgents 图。"""
    llm = model.build(
        model=resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        temperature=resolved.temperature,
    )
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=resolved.prompt or SYSTEM_PROMPT,
        subagents=_subagents(llm),
        backend=backend,
        skills=skill_sources or None,
    )
