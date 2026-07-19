"""routes 各模块共享的领域异常 → HTTP 码映射。各路由模块用即时注解,勿加 from __future__ import annotations。"""

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

_STATUS: dict[type[Exception], int] = {
    UnicodeDecodeError: 415,  # ValueError 子类,须排 ValueError 前
    FileNotFoundError: 404,
    FileExistsError: 409,
    IsADirectoryError: 400,
    NotADirectoryError: 400,
    ValueError: 400,
}


def _status(exc: Exception) -> int:
    return next((code for kind, code in _STATUS.items() if isinstance(exc, kind)), 500)


def _message(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):  # str 常含宿主绝对路径,用通用文案不回显
        return "file not found"
    return str(exc) or type(exc).__name__


@contextmanager
def _http_errors() -> Iterator[None]:
    """领域异常(ValueError/OSError 家族)→ HTTPException。"""
    try:
        yield
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=_status(exc), detail=_message(exc)) from exc
