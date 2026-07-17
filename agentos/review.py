"""Rubric 自审子系统:按用户意图逐 run 路由 rubric、迭代评分、并发裁决 UI、上报 score。

配了 review 规则才启用(恒挂载、不匹配零成本):RubricSeed 把命中规则的 rubric 注入 state 激活自审,
RubricReviewUI(继承 deepagents RubricMiddleware)迭代评分并在终态发一条持久化裁决 UI 卡。
规则选择两条路径:triggers 正则命中优先,否则(gate 开启时)交 LLM router 路由或 none。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, NotRequired

from deepagents.middleware.rubric import (
    GraderResponse,
    RUBRIC_GRADER_MESSAGE_SOURCE,
    RubricMiddleware,
    RubricState,
)
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, hook_config
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph.ui import AnyUIMessage, push_ui_message, ui_message_reducer
from pydantic import BaseModel, Field

from agentos import scoring

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from langgraph.runtime import Runtime

    from agentos.config import ResolvedConfig

logger = logging.getLogger(__name__)


class _RubricState(AgentState):
    rubric: NotRequired[str]


def _latest_user_text(messages: list[Any] | None) -> str:
    """最新一条真实用户消息的文本;跳过 grader 注入的修订消息,无则空串。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, HumanMessage):
            continue
        if msg.additional_kwargs.get("lc_source") == RUBRIC_GRADER_MESSAGE_SOURCE:
            continue
        content = msg.content
        if isinstance(content, str):
            return content
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


ROUTE_PROMPT = """You are a review routing classifier. Given the user's latest \
request, choose which review rubric (if any) applies to the agent's output.

Candidates (label: when it applies):
{catalog}

Choose the single best-matching label, or "none" if the request is conversational or \
exploratory with no objective acceptance criteria (explanations, Q&A, brainstorming, \
chit-chat). Be conservative: when unsure, answer "none" — a missed review just falls \
back to the no-review baseline, whereas a wrong rubric wastes revision cycles and can \
degrade the answer. Judge only the latest user request."""


@dataclass
class _Rule:
    """编译后的审查规则(triggers 已编译为正则)。"""

    name: str
    patterns: list[re.Pattern[str]]
    rubric: str
    description: str


def _compile_triggers(patterns: list[str] | None) -> list[re.Pattern[str]]:
    """坏正则跳过,不因单条坏配置炸整图。"""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def _compile_rules(rules: Any) -> list[_Rule]:
    return [
        _Rule(
            name=getattr(r, "name", "") or "default",
            patterns=_compile_triggers(getattr(r, "triggers", None)),
            rubric=getattr(r, "rubric", "") or "",
            description=getattr(r, "description", "") or "",
        )
        for r in rules or []
    ]


class RouteDecision(BaseModel):
    """LLM 路由的结构化判定:选中的规则 name,或 'none' 表示不审。"""

    label: str = Field(description="The single best-matching rule label, or 'none' if no review is needed.")
    reason: str = Field(default="", description="Brief justification, one short clause.")


def _label(out: Any) -> str:
    """从 with_structured_output 的返回(实例或 dict)取 label;取不到判 'none'。"""
    if isinstance(out, RouteDecision):
        return out.label
    if isinstance(out, dict):
        return str(out.get("label", "none"))
    return str(getattr(out, "label", "none"))


class _Router:
    """LLM 意图路由:从规则表选一条(命中返回其 rubric)或 none。薄、单次判定、失败/无效降级为不审(绝不阻断主 run)。"""

    def __init__(self, model: BaseChatModel, prompt: str, rules: list[_Rule]) -> None:
        # with_config 命名 + 打 tag:trace 里 gate 调用可辨识(否则混在主模型调用里)。
        self._model = model.with_structured_output(RouteDecision).with_config(
            run_name="rubric-gate", tags=["review", "gate"]
        )
        catalog = "\n".join(f"- {rule.name}: {rule.description or '(no description)'}" for rule in rules)
        self._prompt = prompt.replace("{catalog}", catalog)
        self._rubrics = {rule.name: rule.rubric for rule in rules}

    def _messages(self, text: str) -> list[Any]:
        return [SystemMessage(self._prompt), HumanMessage(text or "(empty)")]

    def route(self, text: str) -> str:
        try:
            return self._rubrics.get(_label(self._model.invoke(self._messages(text))), "")
        except Exception:  # noqa: BLE001 —— 判不了/选了无效 label 就保守不审,不阻断 run
            return ""

    async def aroute(self, text: str) -> str:
        try:
            return self._rubrics.get(_label(await self._model.ainvoke(self._messages(text))), "")
        except Exception:  # noqa: BLE001
            return ""


class RubricSeed(AgentMiddleware[_RubricState, Any, Any]):
    """按用户意图逐 run 把匹配规则的 rubric 注入 state 激活自审;不匹配注入空串(空 rubric 即 no-op)。

    取最新真实用户消息:正则按序命中优先短路(triggers 为空=catch-all),否则交 router 路由或 none。
    每 run 无条件重写 rubric 键:防跨 run 残留 stick、assistant 改规则老线程即时生效。
    """

    state_schema = _RubricState

    def __init__(self, rules: list[_Rule], router: _Router | None = None) -> None:
        super().__init__()
        self._rules = rules
        self._router = router

    def _match(self, text: str) -> str | None:
        for rule in self._rules:
            if not rule.patterns or any(pattern.search(text) for pattern in rule.patterns):
                return rule.rubric
        return None

    def before_agent(self, state: _RubricState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        text = _latest_user_text(state.get("messages"))
        hit = self._match(text)
        if hit is not None:
            return {"rubric": hit}
        return {"rubric": self._router.route(text) if self._router is not None else ""}

    async def abefore_agent(
        self, state: _RubricState, runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        text = _latest_user_text(state.get("messages"))
        hit = self._match(text)
        if hit is not None:
            return {"rubric": hit}
        return {"rubric": (await self._router.aroute(text)) if self._router is not None else ""}


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
        # 此覆写依赖 RubricMiddleware(@beta)的私有属性(_grader/_model/_system_prompt/_tools);deepagents
        # 升级若改私有契约,降级回父类默认 grader(丢 ProviderStrategy 但不崩)并告警,不静默失败。
        try:
            if self._grader is None:
                self._grader = create_agent(
                    model=self._model,
                    system_prompt=self._system_prompt,
                    tools=self._tools,
                    name=RUBRIC_GRADER_MESSAGE_SOURCE,
                    response_format=ProviderStrategy(GraderResponse),
                )
            return self._grader
        except AttributeError:
            logger.warning(
                "RubricMiddleware internals changed; falling back to default grader (lost ProviderStrategy)",
                exc_info=True,
            )
            return super()._ensure_grader()


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


def _on_evaluation(evaluation: dict[str, Any]) -> None:
    """rubric 裁决:落结构化日志(run_id/thread_id 由 aegra structlog 自动附带)+ 上报 Langfuse score。

    failed/max_iterations_reached/grader_error 这类"没过审却收尾"用 warning,便于在追踪后端过滤告警。
    """
    result = evaluation.get("result")
    level = logging.WARNING if result in {"failed", "max_iterations_reached", "grader_error"} else logging.INFO
    logger.log(
        level,
        "rubric evaluation: result=%s iteration=%s run=%s",
        result,
        evaluation.get("iteration"),
        evaluation.get("grading_run_id"),
    )
    scoring.score("rubric", result or "unknown", data_type="CATEGORICAL", comment=evaluation.get("explanation") or None)


def build(
    resolved: ResolvedConfig, grader: BaseChatModel, gate: BaseChatModel | None = None
) -> list[AgentMiddleware[Any, Any, Any]]:
    """自审:配了 review 规则才启用(RubricSeed 路由 + RubricReviewUI 迭代并发裁决 UI);恒挂载、不匹配零成本。"""
    review = resolved.review
    rules = _compile_rules(review.rules)
    if not rules:
        return []
    router = _Router(gate, review.gate_prompt or ROUTE_PROMPT, rules) if gate is not None else None
    return [
        RubricSeed(rules, router),
        RubricReviewUI(
            model=grader,
            max_iterations=review.max_iterations,
            on_evaluation=_on_evaluation,
        ),
    ]
