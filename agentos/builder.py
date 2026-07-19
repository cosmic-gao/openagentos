"""组装 agent:model + tools + subagents + 中间件 → create_deep_agent。

网关模型自省为 `openai:<name>`,匹配不到内置 harness profile,故注册一份 provider 级 openai profile。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, cast

from deepagents import HarnessProfile, create_deep_agent, register_harness_profile
from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from deepagents.middleware.summarization import create_summarization_tool_middleware

from agentos import middleware, model, review
from agentos.config import ResolvedConfig, Settings
from agentos.prompts import HARNESS_SUFFIX, SYSTEM_PROMPT

if TYPE_CHECKING:
    # before_agent/abefore_agent 的参数注解须为真实名:langgraph RunnableCallable 按注解决定是否注入 config
    # (config 必须注解成 RunnableConfig,否则不注入 → 缺参报错);runtime 任意注解均注入。
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

register_harness_profile("openai", HarnessProfile(system_prompt_suffix=HARNESS_SUFFIX))


class _FreshSkillsState(SkillsState):
    skills_mtime: NotRequired[float]


class _FreshSkills(SkillsMiddleware):
    """按 host skills 目录 mtime 门控重扫:目录变了才经沙箱重枚举,未变复用 thread state 缓存、跳过沙箱。

    默认 SkillsMiddleware 把清单永久缓存进 thread state(增删对老会话不可见)、且每 run 无条件重扫会强制
    拉起沙箱;本地 stat mtime 门控既即时反映增删改、又免掉每 run 的沙箱开销。
    """

    state_schema = _FreshSkillsState
    _CACHED = ("skills_metadata", "skills_load_errors")

    def __init__(self, *, backend: Any, sources: list[str], skills_dir: Path | None) -> None:
        super().__init__(backend=backend, sources=sources)
        self._skills_dir = skills_dir

    def _mtime(self) -> float:
        root = self._skills_dir
        if root is None or not root.is_dir():
            return 0.0
        try:
            latest = root.stat().st_mtime
        except OSError:
            return 0.0
        for child in root.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
        return latest

    def _fresh(self, state: SkillsState) -> SkillsState:
        return cast(SkillsState, {k: v for k, v in state.items() if k not in self._CACHED})

    def before_agent(self, state: SkillsState, runtime: Runtime, config: RunnableConfig) -> dict[str, Any] | None:
        mtime = self._mtime()
        if "skills_metadata" in state and state.get("skills_mtime") == mtime:
            return None
        update = super().before_agent(self._fresh(state), runtime, config)
        return {**(update or {}), "skills_mtime": mtime}

    async def abefore_agent(self, state: SkillsState, runtime: Runtime, config: RunnableConfig) -> dict[str, Any] | None:
        mtime = await asyncio.to_thread(self._mtime)
        if "skills_metadata" in state and state.get("skills_mtime") == mtime:
            return None
        update = await super().abefore_agent(self._fresh(state), runtime, config)
        return {**(update or {}), "skills_mtime": mtime}


def build(
    *,
    resolved: ResolvedConfig,
    settings: Settings,
    backend: Any,
    tools: list[Any],
    skills: list[str] | None,
    skills_dir: Path | None = None,
    memory: list[str] | None = None,
) -> Any:
    llm = model.build(
        model=resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        context_window=resolved.context_window,
        stream_usage=resolved.stream_usage,
    )
    # grader 独立构建、设超时/限重试:自审模型挂起/不可达时快速降级 grader_error,不拖死主 run。
    grader = model.build(
        model=resolved.review.model or resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        context_window=resolved.context_window,
        stream_usage=resolved.stream_usage,
        timeout=60,
        max_retries=1,
    )
    gate = grader if (resolved.review.rules and resolved.review.gate) else None
    # skills 经 _FreshSkills 挂 middleware=(非 skills= 参数)替换默认缓存;代价:子代理拿不到 skills(有意取舍)。
    skills_mw = [_FreshSkills(backend=backend, sources=skills, skills_dir=skills_dir)] if skills else []
    compact = create_summarization_tool_middleware(model=llm, backend=backend)
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=resolved.prompt or SYSTEM_PROMPT,
        middleware=[
            *skills_mw,
            compact,
            *middleware.build(resolved, settings),
            *review.build(resolved, grader, gate, backend),
        ],
        backend=backend,
        memory=memory,
        interrupt_on=resolved.interrupt_on or None,
    )
