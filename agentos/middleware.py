"""官方 LangChain / deepagents 中间件栈:重试 / 上限 / 回退 / 上下文裁剪 / 工具选择 / PII / 自审。

config 驱动。追加到 create_deep_agent 默认栈之后、tail 之前,不替换 todo/skills/filesystem/
subagents/summarization/memory。build() 与主模型无关;build_review() 需 grader 模型故单列。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NotRequired

from deepagents.middleware.rubric import RubricMiddleware
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
from langchain.agents.middleware.types import AgentState

from agentos import model

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain_core.language_models import BaseChatModel
    from langgraph.runtime import Runtime

    from agentos.config import ResolvedConfig, Settings

_PII_TYPES = ("email", "credit_card", "ip", "mac_address")


def _tool_name(tool: Any) -> str | None:
    return tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)


class ToolFilter(AgentMiddleware[Any, Any, Any]):
    """按名从每次模型请求剔除工具(官方 wrap_model_call hook):deny/禁用的工具对模型不可见。"""

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


class _RubricState(AgentState):
    rubric: NotRequired[str]


class RubricSeed(AgentMiddleware[_RubricState, Any, Any]):
    """把每助手配置的 rubric 注入 state,激活 RubricMiddleware 的自审迭代。"""

    state_schema = _RubricState

    def __init__(self, rubric: str) -> None:
        super().__init__()
        self._rubric = rubric

    def before_agent(self, state: _RubricState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return None if state.get("rubric") else {"rubric": self._rubric}

    async def abefore_agent(
        self, state: _RubricState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return None if state.get("rubric") else {"rubric": self._rubric}


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
            model=resolved.fallback_model, base_url=resolved.base_url, api_key=resolved.api_key
        )
        stack.append(ModelFallbackMiddleware(fallback))
    if settings.context_editing:
        stack.append(ContextEditingMiddleware())
    if settings.tool_selector_max is not None:
        stack.append(LLMToolSelectorMiddleware(max_tools=settings.tool_selector_max))
    strategy = resolved.pii_strategy
    if strategy != "off":
        for kind in _PII_TYPES:
            stack.append(
                PIIMiddleware(
                    kind, strategy=strategy, apply_to_input=True, apply_to_tool_results=True
                )
            )
    if resolved.excluded_tools:
        stack.append(ToolFilter(set(resolved.excluded_tools)))
    return stack


def build_review(resolved: ResolvedConfig, grader: BaseChatModel) -> list[AgentMiddleware[Any, Any, Any]]:
    """自审:配置了 review.rubric 才启用(RubricSeed 注入 + RubricMiddleware 迭代)。"""
    if not resolved.rubric:
        return []
    return [
        RubricSeed(resolved.rubric),
        RubricMiddleware(model=grader, max_iterations=resolved.review_max_iterations),
    ]
