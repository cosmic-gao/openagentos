"""Langfuse score 上报:rubric 裁决、用户反馈等质量信号,按 OTEL trace_id 关联到 aegra 导出的 trace。

score 是 Langfuse 一等对象,走 SDK 直连(非 OTEL span attr):v4 须 tracing_enabled=True 才上报,故传一个
隔离 TracerProvider——满足开启前提又不劫持 aegra 追踪管道。缺凭据/SDK/出错一律静默降级,绝不阻断主 run。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from opentelemetry import trace

from agentos.config import get_settings

logger = logging.getLogger(__name__)

ScoreType = Literal["NUMERIC", "CATEGORICAL", "BOOLEAN"]


@lru_cache
def _client() -> Any | None:
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse
        from opentelemetry.sdk.trace import TracerProvider

        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host or None,
            tracing_enabled=True,  # v4 关掉 tracing 会静默丢弃 score;隔离 provider 避免劫持/二次导出
            tracer_provider=TracerProvider(),
        )
    except Exception:
        logger.warning("Langfuse score client init failed; scoring disabled", exc_info=True)
        return None


def current_trace_id() -> str | None:
    """当前 OTEL trace id(32-hex);无活动 span 则 None。"""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else None


def score(
    name: str,
    value: float | str,
    *,
    trace_id: str | None = None,
    data_type: ScoreType = "CATEGORICAL",
    comment: str | None = None,
) -> None:
    """上报一个 score,关联当前(或指定)trace;缺凭据/SDK/trace 或出错静默降级。"""
    client = _client()
    if client is None:
        return
    tid = trace_id or current_trace_id()
    if not tid:
        return
    try:
        client.create_score(name=name, value=value, trace_id=tid, data_type=data_type, comment=comment)
    except Exception:
        logger.warning("Langfuse score report failed", exc_info=True)


def flush() -> None:
    """冲刷缓冲的 score(进程退出前调,防丢)。"""
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            logger.warning("Langfuse score flush failed", exc_info=True)
