"""OpenAgentOS — DeepAgents 智能体，托管于 Aegra。导入时加载本地 .env。"""

from __future__ import annotations

from dotenv import load_dotenv

# 先加载 .env，再导出配置 API：get_settings() 运行时读环境变量，此序保证其可见。
load_dotenv()

from agentos.config import (  # noqa: E402
    DEFAULT_MODEL,
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
    "DEFAULT_MODEL",
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
__version__ = "0.1.0"
