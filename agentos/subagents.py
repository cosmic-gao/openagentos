"""子代理定义（主 agent 通过内置 task 工具委派，沿用该助手模型）。"""

from __future__ import annotations

from deepagents import SubAgent
from langchain_core.language_models import BaseChatModel

from agentos.prompts import RESEARCH_PROMPT
from agentos.tools import internet_search


def build_subagents(model: BaseChatModel) -> list[SubAgent]:
    research_agent: SubAgent = {
        "name": "research-agent",
        "description": (
            "Delegate deep, self-contained web research and multi-source "
            "fact-finding here. Provide a precise, standalone question; it "
            "returns a synthesized, cited answer."
        ),
        "system_prompt": RESEARCH_PROMPT,
        "tools": [internet_search],
        "model": model,
    }
    return [research_agent]
