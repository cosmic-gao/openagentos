"""组装 agent:model + tools + subagents → create_deep_agent。

经 OpenAI 兼容网关的模型自省出 `openai:<name>`,匹配不到 deepagents 内置 harness profile,
故在此注册一份 provider 级 `openai` profile,补回并行工具调用、先查证再答等模型级调优。
"""

from __future__ import annotations

from typing import Any, cast

from deepagents import HarnessProfile, SubAgent, create_deep_agent, register_harness_profile
from deepagents.middleware.skills import SkillsMiddleware, SkillsState

from agentos import middleware, model
from agentos.config import HARNESS_SUFFIX, RESEARCH_PROMPT, SYSTEM_PROMPT, ResolvedConfig, Settings
from agentos.tools import internet_search

register_harness_profile("openai", HarnessProfile(system_prompt_suffix=HARNESS_SUFFIX))


class _FreshSkills(SkillsMiddleware):
    """每次 run 重新枚举 skills:剥掉 checkpoint 里缓存的清单键再交父类加载。

    deepagents 默认把 skill 清单缓存进 thread state(before_agent 命中即跳过),导致已存在会话
    看不到 skill 增删。这里让每次 run 都重新扫描挂载目录,使增删对老会话的下一次 run 也即时生效。
    """

    _CACHED = ("skills_metadata", "skills_load_errors")

    def _fresh(self, state: SkillsState) -> SkillsState:
        return cast(SkillsState, {k: v for k, v in state.items() if k not in self._CACHED})

    def before_agent(self, state: SkillsState, runtime, config):
        return super().before_agent(self._fresh(state), runtime, config)

    async def abefore_agent(self, state: SkillsState, runtime, config):
        return await super().abefore_agent(self._fresh(state), runtime, config)


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
    memory: list[str] | None = None,
) -> Any:
    llm = model.build(model=resolved.model, base_url=resolved.base_url, api_key=resolved.api_key)
    grader = (
        model.build(model=resolved.review_model, base_url=resolved.base_url, api_key=resolved.api_key)
        if resolved.review_model
        else llm
    )
    skills_mw = [_FreshSkills(backend=backend, sources=skills)] if skills else []
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=resolved.prompt or SYSTEM_PROMPT,
        middleware=[*skills_mw, *middleware.build(resolved, settings), *middleware.build_review(resolved, grader)],
        subagents=[_research(llm)],
        backend=backend,
        memory=memory,
        interrupt_on=resolved.interrupt_on or None,
    )
