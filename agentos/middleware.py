"""按 config 组装官方中间件栈(重试/上限/回退/裁剪/工具选择/PII/密钥脱敏),追加到默认栈之后。

自审(rubric)子系统在 [agentos/review.py](review.py);builder 把两者拼接为最终 middleware 列表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ContextEditingMiddleware,
    LLMToolSelectorMiddleware,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)

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
    """输出侧脱敏中间件——仅为与同类型输入侧实例区分 name(PIIMiddleware.name 含类名,create_agent
    要求中间件 name 唯一,否则同 pii_type 的两个实例撞名);行为与父类完全一致。"""


def _redactors(pii_type: str, strategy: RedactionStrategy, **kwargs: Any) -> list[AgentMiddleware[Any, Any, Any]]:
    """一个 PII/密钥类型的脱敏中间件:输入/工具结果按 strategy(block 则拒收可疑入站内容),
    但**输出侧永不 block**——block 降级为 redact:出站内容只脱敏、绝不中断 run(输出侧 block 与
    redact 防泄漏效果相同,唯 block 会硬失败)。非 block 策略下三侧共用一个中间件。"""
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


def build(resolved: ResolvedConfig, settings: Settings) -> list[AgentMiddleware[Any, Any, Any]]:
    stack: list[AgentMiddleware[Any, Any, Any]] = []
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
    if resolved.fallback_model:
        fallback = model.build(
            model=resolved.fallback_model,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            context_window=resolved.context_window,
        )
        stack.append(ModelFallbackMiddleware(fallback))
    if settings.context_editing:
        stack.append(ContextEditingMiddleware())
    if settings.tool_selector_max is not None:
        stack.append(LLMToolSelectorMiddleware(max_tools=settings.tool_selector_max))
    strategy = resolved.pii_strategy
    if strategy != "off":
        for kind in _PII_TYPES:
            stack.extend(_redactors(kind, strategy))
    # 密钥兜底:独立于 pii_strategy,默认开;同样输出侧不硬失败。
    if settings.secret_redaction:
        stack.extend(_redactors("secret", settings.secret_redaction_strategy, detector=redaction.detect_secrets))
    if resolved.excluded_tools:
        stack.append(ToolFilter(set(resolved.excluded_tools)))
    return stack
