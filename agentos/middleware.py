"""按 config 组装官方中间件栈(重试/上限/回退/工具选择/PII/密钥脱敏 + 回复耗时),追加到默认栈之后。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import (
    AgentMiddleware,
    LLMToolSelectorMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage

from agentos import model, redaction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse

    from agentos.config import RedactionStrategy, ResolvedConfig, Settings

_PII_TYPES = ("email", "credit_card", "ip", "mac_address")


def _tool_name(tool: Any) -> str | None:
    return tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)


class ToolFilter(AgentMiddleware[Any, Any, Any]):
    """按名剔除 deny/禁用的工具,使其对模型不可见。"""

    def __init__(self, excluded: set[str]) -> None:
        super().__init__()
        self._excluded = excluded

    def _apply(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        return request.override(tools=[t for t in request.tools if _tool_name(t) not in self._excluded])

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._apply(request))


class _OutputPII(PIIMiddleware):
    """仅为与输入侧实例区分 name(create_agent 要求中间件 name 唯一);行为同父类。"""


def _redactors(pii_type: str, strategy: RedactionStrategy, **kwargs: Any) -> list[AgentMiddleware[Any, Any, Any]]:
    """一个 PII/密钥类型的脱敏中间件。输出侧永不 block(block 降级为 redact:防泄漏效果相同、但不硬失败)。"""
    if strategy != "block":
        return [
            PIIMiddleware(
                pii_type,
                strategy=strategy,
                apply_to_input=True,
                apply_to_output=True,
                apply_to_tool_results=True,
                **kwargs,
            )
        ]
    return [
        PIIMiddleware(pii_type, strategy="block", apply_to_input=True, apply_to_tool_results=True, **kwargs),
        _OutputPII(pii_type, strategy="redact", apply_to_output=True, **kwargs),
    ]


class _TimingProbe(AsyncCallbackHandler):
    """挂到本次模型调用上,记录起始/首 token/结束时刻与 chunk 数。首 token 与 chunk 仅在流式时(on_llm_new_token
    有触发)才有;非流式退化为仅总时长。为兼容 chat/llm 两类事件名,start/end 同时挂两组回调。"""

    def __init__(self) -> None:
        self.start: float | None = None
        self.first: float | None = None
        self.end: float | None = None
        self.chunks = 0

    async def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self.start = time.perf_counter()

    async def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        if self.start is None:
            self.start = time.perf_counter()

    async def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        if self.first is None:
            self.first = time.perf_counter()
        self.chunks += 1

    async def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
        self.end = time.perf_counter()


class ModelTiming(AgentMiddleware[Any, Any, Any]):
    """测量本轮回复的模型调用耗时,写进该回复 AIMessage 的 additional_kwargs['timing']:随 checkpoint 持久化,
    前端刷新后即可从 state 恢复展示(无需客户端本地存储)。字段名对齐前端 Timing(camelCase)。

    经 handler 调用(不自行 stream request.model),以保留内层 summarization/memory/prompt-caching 等中间件;
    callback 绑在真正执行的模型上,故耗时不受本中间件在栈中的位置影响。任何异常都不落 timing、绝不中断回复。
    """

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        probe = _TimingProbe()
        wall0 = time.perf_counter()
        response = await handler(request.override(model=request.model.with_config({"callbacks": [probe]})))
        wall1 = time.perf_counter()
        try:
            ai = next((m for m in response.result if isinstance(m, AIMessage)), None)
            if ai is not None:
                ai.additional_kwargs["timing"] = _timing(probe, wall0, wall1, ai)
        except Exception:  # noqa: BLE001 —— 计时是旁路,任何异常都不该影响回复
            pass
        return response


def _timing(probe: _TimingProbe, wall0: float, wall1: float, ai: AIMessage) -> dict[str, Any]:
    """按前端 Timing 形状产出 {totalStreamTime, totalChunks, firstTokenTime?, tokensPerSecond?}(毫秒/整数)。"""
    base = probe.start if probe.start is not None else wall0
    end = probe.end if probe.end is not None else wall1
    total_ms = round((end - base) * 1000, 1)
    timing: dict[str, Any] = {"totalStreamTime": total_ms, "totalChunks": probe.chunks}
    if probe.first is not None:
        timing["firstTokenTime"] = round((probe.first - base) * 1000, 1)
    out = (ai.usage_metadata or {}).get("output_tokens")
    if out and total_ms > 0:
        timing["tokensPerSecond"] = round(out / (total_ms / 1000.0), 1)
    return timing


def build(resolved: ResolvedConfig, settings: Settings) -> list[AgentMiddleware[Any, Any, Any]]:
    stack: list[AgentMiddleware[Any, Any, Any]] = []
    # wrap_model_call 洋葱嵌套(先注册者在外层):fallback 须包在 retry 外层——先把单模型重试打满、
    # 仍失败才回退备用模型(官方规则 retry inner / fallback outer;反之会连 fallback 一起重试)。
    if resolved.fallback_model:
        fallback = model.build(
            model=resolved.fallback_model,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            context_window=resolved.context_window,
            stream_usage=resolved.stream_usage,
        )
        stack.append(ModelFallbackMiddleware(fallback))
    if settings.model_max_retries > 0:
        stack.append(ModelRetryMiddleware(max_retries=settings.model_max_retries))
    if settings.tool_max_retries > 0:
        stack.append(ToolRetryMiddleware(max_retries=settings.tool_max_retries))
    if resolved.steps is not None:
        stack.append(ModelCallLimitMiddleware(run_limit=resolved.steps, exit_behavior="end"))
    if settings.tool_call_limit is not None:
        stack.append(
            ToolCallLimitMiddleware(run_limit=settings.tool_call_limit, exit_behavior="continue")
        )
    if settings.tool_selector_max is not None:
        stack.append(LLMToolSelectorMiddleware(max_tools=settings.tool_selector_max))
    strategy = resolved.pii_strategy
    if strategy != "off":
        for kind in _PII_TYPES:
            stack.extend(_redactors(kind, strategy))
    # 密钥兜底:独立于 pii_strategy,默认开。
    if settings.secret_redaction:
        stack.extend(_redactors("secret", settings.secret_redaction_strategy, detector=redaction.detect_secrets))
    if resolved.excluded_tools:
        stack.append(ToolFilter(set(resolved.excluded_tools)))
    # 末位注册=最内层:紧贴模型调用,callback 稳挂在真正执行的模型上,拿到首 token/chunk。
    stack.append(ModelTiming())
    return stack
