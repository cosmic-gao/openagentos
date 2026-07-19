"""每线程临时沙箱 + 一次性无状态执行。

- [session](session.py):按 (assistant, thread) 多路复用的持久会话沙箱(发现/恢复/续期/重建),挂共享卷。
- [run](run.py):新建临时沙箱执行单文件、完成即销毁——供 `/sandboxes/execute` 用。
- [client](client.py):两者共享的 OpenSandbox 连接层与沙箱规格(transport / manager / resource / volume)。
"""

from __future__ import annotations

from agentos.sandbox.run import run
from agentos.sandbox.session import SessionSandbox, session

__all__ = ["SessionSandbox", "run", "session"]
