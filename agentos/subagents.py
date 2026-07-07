"""OpenAgentOS 的子代理定义。

子代理在隔离上下文中运行，主 agent 通过内置 `task` 工具调用。每个子代理是一个
deepagents 的 `SubAgent` TypedDict：name / description / system_prompt（必填），外加
可选的 tools / model / middleware / skills 等。
"""

from __future__ import annotations

from deepagents import SubAgent

from agentos.model import get_subagent_model
from agentos.prompts import RESEARCH_PROMPT
from agentos.tools import internet_search


def build_subagents() -> list[SubAgent]:
    """返回主 agent 可通过 `task` 委派的子代理列表。"""
    research_agent: SubAgent = {
        "name": "research-agent",
        "description": (
            "Delegate deep, self-contained web research and multi-source "
            "fact-finding here. Provide a precise, standalone question; it "
            "returns a synthesized, cited answer."
        ),
        "system_prompt": RESEARCH_PROMPT,
        "tools": [internet_search],
        "model": get_subagent_model(),
    }
    return [research_agent]
