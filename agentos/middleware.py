"""按 config 组装官方中间件栈(重试/上限/回退/裁剪/工具选择/PII/自审),追加到默认栈之后。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, NotRequired

from deepagents.middleware.rubric import (
    GraderResponse,
    RUBRIC_GRADER_MESSAGE_SOURCE,
    RubricMiddleware,
    RubricState,
)
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
from langchain.agents import create_agent
from langchain.agents.middleware.types import AgentState, hook_config
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import AIMessage
from langgraph.graph.ui import AnyUIMessage, push_ui_message, ui_message_reducer

from agentos import model, redaction

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
    """每次模型请求按名剔除工具:deny/禁用的工具对模型不可见。"""

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
        # 每 run 无条件重写:assistant 改了 rubric 后老线程也即时生效(同 _FreshSkills 思路);
        # 否则守卫会锁死首次写入的旧 rubric。
        return {"rubric": self._rubric}

    async def abefore_agent(
        self, state: _RubricState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return {"rubric": self._rubric}


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
        stack.extend(
            PIIMiddleware(kind, strategy=strategy, apply_to_input=True, apply_to_tool_results=True)
            for kind in _PII_TYPES
        )
    # 密钥兜底:独立于 pii_strategy,默认开;输入/输出/工具结果全覆盖。
    if settings.secret_redaction:
        stack.append(redaction.secret_middleware(settings.secret_redaction_strategy))
    if resolved.excluded_tools:
        stack.append(ToolFilter(set(resolved.excluded_tools)))
    return stack


class _RubricUIState(RubricState):
    ui: NotRequired[Annotated[list[AnyUIMessage], ui_message_reducer]]


class RubricReviewUI(RubricMiddleware):
    """自审终态时发一条持久化 ui 消息(挂到最终回复),供前端 Generative UI 逐消息渲染裁决卡。

    评审逻辑全走父类,仅在收尾钩子追加副作用;needs_revision(jump 回模型)不落卡,实时进度靠
    rubric_evaluation_* 事件。声明 ui 通道使 push_ui_message 的 state 写入有处落、随 checkpoint 持久化。
    覆写须重挂 @hook_config,否则丢失 can_jump_to 会断掉修订循环。
    """

    state_schema = _RubricUIState

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        update = super().after_agent(state, runtime)
        _push_verdict(update, state)
        return update

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        update = await super().aafter_agent(state, runtime)
        _push_verdict(update, state)
        return update

    def _ensure_grader(self) -> Any:
        # 强制 json_schema(ProviderStrategy):默认 tool 策略下 gemini 格式化不出复杂的 criteria,
        # 会反复重试十几轮(实测 41s 且判 failed);ProviderStrategy 下约 3s 出合法裁决。
        if self._grader is None:
            self._grader = create_agent(
                model=self._model,
                system_prompt=self._system_prompt,
                tools=self._tools,
                name=RUBRIC_GRADER_MESSAGE_SOURCE,
                response_format=ProviderStrategy(GraderResponse),
            )
        return self._grader


def _push_verdict(update: dict[str, Any] | None, state: Any) -> None:
    if not update or update.get("jump_to"):  # 非终态(还要回模型改)不落持久卡
        return
    status = update.get("_rubric_status")
    evals = update.get("_rubric_evaluations") or []
    if not status or not evals:
        return
    ai = next((m for m in reversed(state.get("messages") or []) if isinstance(m, AIMessage)), None)
    if ai is None:
        return
    last = evals[-1]
    try:
        push_ui_message(
            "rubric_verdict",
            {
                "verdict": status,
                "pass": (last.get("iteration") or 0) + 1,
                "explanation": last.get("explanation") or "",
                "criteria": last.get("criteria") or [],
            },
            message=ai,
            state_key="ui",
        )
    except Exception:  # noqa: BLE001 —— UI 副作用失败不该中断 run
        pass


def build_review(resolved: ResolvedConfig, grader: BaseChatModel) -> list[AgentMiddleware[Any, Any, Any]]:
    """自审:配置了 review.rubric 才启用(RubricSeed 注入 + RubricReviewUI 迭代并发裁决 UI)。"""
    if not resolved.rubric:
        return []
    return [
        RubricSeed(resolved.rubric),
        RubricReviewUI(model=grader, max_iterations=resolved.review_max_iterations),
    ]
