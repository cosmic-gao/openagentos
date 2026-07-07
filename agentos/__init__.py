"""OpenAgentOS — DeepAgents 智能体,托管于 Aegra。导入时加载本地 .env。"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from agentos.config import (  # noqa: E402
    RESEARCH_PROMPT,
    SYSTEM_PROMPT,
    AgentConfig,
    ResolvedConfig,
    Settings,
    configurable,
    current_thread_id,
    get_settings,
    resolve,
    safe_segment,
)

__all__ = [
    "RESEARCH_PROMPT",
    "SYSTEM_PROMPT",
    "AgentConfig",
    "ResolvedConfig",
    "Settings",
    "configurable",
    "current_thread_id",
    "get_settings",
    "resolve",
    "safe_segment",
]
__version__ = "0.2.0"
