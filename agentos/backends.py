"""组装 agent 的存储/执行后端。

用 deepagents 的 `CompositeBackend` 把两种隔离组合起来：

- **default** → 每线程临时沙箱（`SessionSandbox`，含 `execute`）；沙箱禁用或未装
  opensandbox 时回退 `StateBackend`（线程内存储、无 `execute`），便于无服务器开发。
- **`/assistant/`** → 每助手磁盘目录（`AssistantBackend`，skills/、mcp.json 按目录隔离）。

`execute` 只走 default（不可路由），文件操作按前缀路由；均在调用时按 thread/assistant 解析。
"""

from __future__ import annotations

import logging

from deepagents.backends import CompositeBackend, StateBackend

from agentos.sandbox import build_sandbox
from agentos.workspace import ASSISTANT_ROUTE, AssistantBackend

logger = logging.getLogger(__name__)


def build_backend() -> CompositeBackend:
    """构造组合后端：每线程沙箱 + 每助手磁盘目录。"""
    sandbox = build_sandbox()
    if sandbox is None:
        logger.info("sandbox disabled; default=StateBackend (no execute tool)")
        default = StateBackend()
    else:
        default = sandbox
    return CompositeBackend(default=default, routes={ASSISTANT_ROUTE: AssistantBackend()})
