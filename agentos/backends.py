"""组装 agent 后端：每线程沙箱（default，含 execute）+ /assistant/skills/ 磁盘路由。

execute 只走 default；沙箱禁用/未装时回退 StateBackend。config.json、.mcp.json 不路由到 agent。
"""

from __future__ import annotations

from deepagents.backends import CompositeBackend, StateBackend

from agentos.sandbox import build_sandbox
from agentos.workspace import ASSISTANT_ROUTE, skills_backend


def build_backend(assistant_id: str) -> CompositeBackend:
    default = build_sandbox() or StateBackend()
    return CompositeBackend(default=default, routes={ASSISTANT_ROUTE: skills_backend(assistant_id)})
