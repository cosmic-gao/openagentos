"""Langfuse score 上报:rubric 裁决、用户反馈等质量信号关联到 aegra 经 OTEL 导出的 trace。

score 是 Langfuse 一等对象,无法走 OTEL span attribute,必须用 SDK/API。故这里直连 Langfuse:
但 v4 起 `create_score`/`flush` 在 `tracing_enabled=False` 时直接 early-return(v3 不会),关掉即
静默丢弃全部 score。故必须开 `tracing_enabled=True`,同时传一个**隔离的 TracerProvider** 承接其
span processor:既满足 score ingestion 的开启前提,又不注册为全局 provider —— 既不劫持 aegra 的
追踪管道,也不会把 aegra 的 span 二次导出到 Langfuse。score 仍按当前 OTEL trace_id 关联到 aegra
导出的同一条 trace。缺凭据/SDK 或出错一律静默降级,绝不阻断主 run。
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
            # 见模块 docstring:v4 关掉 tracing 会静默丢弃 score,故开启;隔离 provider 避免劫持/二次导出。
            tracing_enabled=True,
            tracer_provider=TracerProvider(),
        )
    except Exception:
        logger.warning("Langfuse score client init failed; scoring disabled", exc_info=True)
        return None


def current_trace_id() -> str | None:
    """当前 OTEL trace id(32-hex);Langfuse 用它关联 aegra 经 OTLP 导出的 trace。无活动 span 则 None。"""
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
    """冲刷缓冲的 score(进程退出前调,防丢);score 走后台 batch,平时无需手动调。"""
    client = _client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass
