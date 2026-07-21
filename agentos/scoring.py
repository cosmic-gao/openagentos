"""质量 score 上报:rubric 裁决、用户反馈等信号,按 OTEL trace_id 关联到 aegra 导出的 trace。

直连 Langfuse Public API(POST /api/public/scores, HTTP Basic)——不引 Langfuse SDK、不碰 OTEL
TracerProvider。上报走后台线程池 fire-and-forget,不阻塞事件循环;缺凭据/无 trace/出错一律静默降级,
绝不阻断主 run。score 是 Langfuse 一等对象、无法走 OTEL span attribute,故单独上报而非并入 aegra 的 trace 管道。
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Literal

import httpx
from opentelemetry import trace

from agentos.config import get_settings

logger = logging.getLogger(__name__)

ScoreType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]

_DEFAULT_HOST = "https://cloud.langfuse.com"
_TIMEOUT = 10.0

_pool: ThreadPoolExecutor | None = None


@lru_cache
def _endpoint() -> tuple[str, str] | None:
    """(scores url, Basic auth 头);缺 public/secret key 则 None=禁用上报。"""
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    host = (settings.langfuse_host or _DEFAULT_HOST).rstrip("/")
    token = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode("ascii")
    return f"{host}/api/public/scores", f"Basic {token}"


def _submit(fn: Any, *args: Any) -> None:
    """丢进后台线程池 fire-and-forget;懒建池,池已关(flush 后)则放弃。"""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="langfuse-score")
    try:
        _pool.submit(fn, *args)
    except RuntimeError:
        pass


def current_trace_id() -> str | None:
    """当前 OTEL trace id(32-hex);无活动 span 则 None。"""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None


def _post(url: str, auth: str, payload: dict[str, Any]) -> None:
    try:
        resp = httpx.post(url, json=payload, headers={"Authorization": auth}, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Langfuse score rejected (%s): %s", resp.status_code, resp.text[:200])
    except Exception:
        logger.warning("Langfuse score report failed", exc_info=True)


def score(
    name: str,
    value: float | str,
    *,
    trace_id: str | None = None,
    data_type: ScoreType = "CATEGORICAL",
    comment: str | None = None,
) -> None:
    """上报一个 score 关联当前(或指定)trace;后台 fire-and-forget。缺凭据/无 trace 静默跳过。"""
    endpoint = _endpoint()
    if endpoint is None:
        return
    tid = trace_id or current_trace_id()
    if not tid:
        return
    url, auth = endpoint
    payload: dict[str, Any] = {"traceId": tid, "name": name, "value": value, "dataType": data_type}
    if comment:
        payload["comment"] = comment
    _submit(_post, url, auth, payload)


def flush() -> None:
    """等后台上报排空(进程退出前调,防丢)。关池后复位 _pool——否则 flush 后再来的 score 会命中
    已关闭池抛 RuntimeError 被 _submit 静默吞掉丢弃;复位后 _submit 会按需重建新池。"""
    global _pool
    if _pool is not None:
        try:
            _pool.shutdown(wait=True)
        except Exception:
            logger.warning("Langfuse score flush failed", exc_info=True)
        finally:
            _pool = None
