"""组装 agent:model + tools + subagents → create_deep_agent。"""

from __future__ import annotations

from typing import Any

from deepagents import SubAgent, create_deep_agent

from agentos import middleware, model
from agentos.config import RESEARCH_PROMPT, SYSTEM_PROMPT, ResolvedConfig, Settings
from agentos.tools import internet_search


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
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=resolved.prompt or SYSTEM_PROMPT,
        middleware=[*middleware.build(resolved, settings), *middleware.build_review(resolved, llm)],
        subagents=[_research(llm)],
        backend=backend,
        skills=skills,
        memory=memory,
        interrupt_on=resolved.interrupt_on or None,
    )
