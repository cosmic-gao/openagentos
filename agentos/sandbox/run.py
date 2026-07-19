"""一次性无状态执行:新建临时沙箱执行单文件、完成即销毁——供 /sandboxes/execute 用。"""

from __future__ import annotations

import json
import re
import shlex
from datetime import timedelta
from typing import Any
from uuid import uuid4

from deepagents.backends.protocol import ExecuteResponse
from deepagents_opensandbox import AsyncOpenSandboxBackend

from agentos.config import Settings
from agentos.sandbox.client import _connection, _resource

_RUNNERS: dict[str, tuple[str, str]] = {
    "python": ("py", "python"),
    "bash": ("sh", "bash"),
    "sh": ("sh", "sh"),
}

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _command(
    interp: str,
    path: str,
    args: list[str],
    env: dict[str, str],
    stdin_path: str | None,
) -> str:
    prefix = ["env", *(f"{name}={value}" for name, value in env.items())] if env else []
    # shlex.quote 转义每个 token 防 shell 注入(env/args/路径均不可信)
    command = " ".join(shlex.quote(token) for token in (*prefix, interp, path, *args))
    if stdin_path is not None:
        command += f" < {shlex.quote(stdin_path)}"
    return command


async def run(
    settings: Settings,
    code: str,
    *,
    language: str = "python",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    params: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> ExecuteResponse:
    """新建临时沙箱执行 code、完成即销毁——单次、无状态、不挂持久卷。"""
    runner = _RUNNERS.get(language.lower())
    if runner is None:
        raise ValueError(f"unsupported language {language!r}; expected one of {sorted(_RUNNERS)}")
    env = env or {}
    for name in env:  # env 名拼进 `env NAME=value` 前缀,须为合法 shell 变量名
        if not _ENV_NAME.match(name):
            raise ValueError(f"invalid environment variable name {name!r}")
    ext, interp = runner

    if params:
        params_json = json.dumps(params)
        if interp == "python":
            code = f"params = __import__('json').loads({params_json!r})\n" + code
        else:
            env = {**env, "PARAMS": params_json}

    backend = await AsyncOpenSandboxBackend.create(
        settings.sandbox_image,
        connection_config=_connection(settings),
        timeout=timedelta(seconds=settings.sandbox_ttl),
        default_timeout=settings.sandbox_timeout,
        resource=_resource(settings),
    )
    try:
        path = f"/tmp/{uuid4().hex}.{ext}"
        blobs = [(path, code.encode())]
        stdin_path = None
        if stdin is not None:
            stdin_path = f"/tmp/{uuid4().hex}.stdin"
            blobs.append((stdin_path, stdin.encode()))
        staged = await backend.aupload_files(blobs)
        failed = next((s for s in staged if s.error), None)
        if failed is not None:
            raise OSError(f"failed to stage {failed.path} in sandbox: {failed.error}")
        command = _command(interp, path, args or [], env, stdin_path)
        return await backend.aexecute(command, timeout=timeout)
    finally:
        await backend.aclose()
