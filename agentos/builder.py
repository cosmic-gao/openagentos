"""组装 agent:model + tools + subagents → create_deep_agent。

经 OpenAI 兼容网关的模型自省出 `openai:<name>`,匹配不到 deepagents 内置 harness profile,
故在此注册一份 provider 级 `openai` profile,补回并行工具调用、先查证再答等模型级调优。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, NotRequired, cast

from deepagents import HarnessProfile, SubAgent, create_deep_agent, register_harness_profile
from deepagents.middleware.skills import SkillsMiddleware, SkillsState

from agentos import middleware, model, review
from agentos.config import ResolvedConfig, Settings
from agentos.prompts import HARNESS_SUFFIX, RESEARCH_PROMPT, SYSTEM_PROMPT
from agentos.tools import internet_search

register_harness_profile("openai", HarnessProfile(system_prompt_suffix=HARNESS_SUFFIX))


class _FreshSkillsState(SkillsState):
    skills_mtime: NotRequired[float]  # 上次枚举时 host skills 目录树的最新 mtime,用于门控重扫


class _FreshSkills(SkillsMiddleware):
    """按 host skills 目录 mtime 门控重扫:目录变了才清缓存、经沙箱重枚举;未变则复用 thread state 缓存、
    跳过沙箱。

    deepagents 默认把 skill 清单永久缓存进 thread state(增删对老会话不可见);若每 run 无条件重扫,则
    每 run 都要经沙箱后端 `ls`+下载 SKILL.md(网络),还会在 run 开头就强制拉起沙箱(连纯聊天 run 也是)。
    这里用**本地** stat 目录树 mtime 判断是否真变:未变直接复用,既即时反映增删改、又免掉每 run 的沙箱开销。
    """

    state_schema = _FreshSkillsState
    _CACHED = ("skills_metadata", "skills_load_errors")

    def __init__(self, *, backend: Any, sources: list[str], skills_dir: Path | None) -> None:
        super().__init__(backend=backend, sources=sources)
        self._skills_dir = skills_dir

    def _mtime(self) -> float:
        """host skills 目录树最新 mtime(捕获增/删/改);不存在或未提供返回 0。纯本地 stat,远快于沙箱枚举。"""
        root = self._skills_dir
        if root is None or not root.is_dir():
            return 0.0
        latest = root.stat().st_mtime
        for child in root.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
        return latest

    def _fresh(self, state: SkillsState) -> SkillsState:
        return cast(SkillsState, {k: v for k, v in state.items() if k not in self._CACHED})

    def before_agent(self, state: SkillsState, runtime, config):
        mtime = self._mtime()
        if "skills_metadata" in state and state.get("skills_mtime") == mtime:
            return None
        update = super().before_agent(self._fresh(state), runtime, config)
        return {**(update or {}), "skills_mtime": mtime}

    async def abefore_agent(self, state: SkillsState, runtime, config):
        mtime = await asyncio.to_thread(self._mtime)
        if "skills_metadata" in state and state.get("skills_mtime") == mtime:
            return None
        update = await super().abefore_agent(self._fresh(state), runtime, config)
        return {**(update or {}), "skills_mtime": mtime}


def _research(llm: Any) -> SubAgent:
    return {
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


def build(
    *,
    resolved: ResolvedConfig,
    settings: Settings,
    backend: Any,
    tools: list,
    skills: list[str] | None,
    skills_dir: Path | None = None,
    memory: list[str] | None = None,
) -> Any:
    llm = model.build(
        model=resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        context_window=resolved.context_window,
    )
    # grader 独立构建并设超时/限重试:自审模型调用挂起或不可达时快速降级为 grader_error,不拖死主 run。
    grader = model.build(
        model=resolved.review.model or resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        context_window=resolved.context_window,
        timeout=60,
        max_retries=1,
    )
    # LLM 意图路由:启用且有规则时复用 grader 模型(review.model,缺省主模型),不另开模型字段。
    gate = grader if (resolved.review.rules and resolved.review.gate) else None
    skills_mw = [_FreshSkills(backend=backend, sources=skills, skills_dir=skills_dir)] if skills else []
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=resolved.prompt or SYSTEM_PROMPT,
        middleware=[*skills_mw, *middleware.build(resolved, settings), *review.build(resolved, grader, gate)],
        subagents=[_research(llm)],
        backend=backend,
        memory=memory,
        interrupt_on=resolved.interrupt_on or None,
    )
