"""Rubric 自审子系统:按用户意图逐 run 路由 rubric、迭代评分、终态发裁决 UI、上报 score。配了规则才启用。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, NotRequired

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.rubric import (
    RUBRIC_GRADER_MESSAGE_SOURCE,
    GraderResponse,
    RubricEvaluation,
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

_READONLY_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})


def _grader_tools(backend: Any) -> list[Any]:
    """只读文件系统工具子集,让 grader 实跑取证而非只凭 transcript;取不到降级为空,不炸构图。"""
    if backend is None:
        return []
    try:
        tools = FilesystemMiddleware(backend=backend).tools
    except Exception:
        logger.warning("could not build read-only grader tools; grader judges from transcript only", exc_info=True)
        return []
    return [t for t in tools if getattr(t, "name", None) in _READONLY_TOOLS]


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
            for block in (content or [])
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
    name: str
    patterns: list[re.Pattern[str]]
    rubric: str
    description: str
    catch_all: bool = False  # 仅「未配置 triggers」时为真=显式全命中;坏正则致空 patterns 不算(见 _compile_rules)


def _compile_triggers(patterns: list[str] | None) -> list[re.Pattern[str]]:
    """坏正则跳过,不因单条坏配置炸整图。"""
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            logger.warning("review trigger %r failed to compile; skipped", pattern)
    return compiled


def _compile_rules(rules: Any) -> list[_Rule]:
    """编译规则表并唯一化 name:重名会让 router 路由表({name: rubric})折叠、只剩最后一条,故消歧。
    catch_all 仅当「未配置 triggers」时为真——配了却全部编译失败,视为「不命中」而非退化成命中一切
    (否则一个 trigger 笔误就把规则静默翻转为对每个 run 都注入其 rubric)。"""
    out: list[_Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(rules or []):
        raw_triggers = getattr(r, "triggers", None) or []
        patterns = _compile_triggers(raw_triggers)
        if raw_triggers and not patterns:
            logger.warning("review rule #%d: all triggers failed to compile; rule disabled (won't match)", i + 1)
        name = (getattr(r, "name", "") or "").strip() or f"rule{i + 1}"
        if name in seen:
            logger.warning("review rule name %r duplicated; disambiguating to keep router routing intact", name)
            name = f"{name}-{i + 1}"
        seen.add(name)
        out.append(
            _Rule(
                name=name,
                patterns=patterns,
                rubric=getattr(r, "rubric", "") or "",
                description=getattr(r, "description", "") or "",
                catch_all=not raw_triggers,
            )
        )
    return out


class RouteDecision(BaseModel):
    label: str = Field(description="The single best-matching rule label, or 'none' if no review is needed.")
    reason: str = Field(default="", description="Brief justification, one short clause.")


def _label(out: Any) -> str:
    if isinstance(out, RouteDecision):
        return out.label
    if isinstance(out, dict):
        return str(out.get("label", "none"))
    return str(getattr(out, "label", "none"))


class _Router:
    """LLM 意图路由:从规则表选一条或 none;失败/无效降级为不审,绝不阻断主 run。"""

    def __init__(self, model: BaseChatModel, prompt: str, rules: list[_Rule]) -> None:
        # tags 含 "nostream"(langgraph TAG_NOSTREAM):这一步是路由前置分类,其结构化输出
        # {label, reason} 不应作为 AI 消息流进聊天(否则每条回复开头都露出一段路由 JSON)。
        self._model = model.with_structured_output(RouteDecision).with_config(
            run_name="rubric-gate", tags=["review", "gate", "nostream"]
        )
        catalog = "\n".join(f"- {rule.name}: {rule.description or '(no description)'}" for rule in rules)
        self._prompt = prompt.replace("{catalog}", catalog)
        self._rubrics = {rule.name: rule.rubric for rule in rules}

    def _messages(self, text: str) -> list[Any]:
        return [SystemMessage(self._prompt), HumanMessage(text or "(empty)")]

    def route(self, text: str) -> str:
        try:
            return self._rubrics.get(_label(self._model.invoke(self._messages(text))), "")
        except Exception:  # noqa: BLE001 — 判不了就保守不审
            return ""

    async def aroute(self, text: str) -> str:
        try:
            return self._rubrics.get(_label(await self._model.ainvoke(self._messages(text))), "")
        except Exception:  # noqa: BLE001
            return ""


class RubricSeed(AgentMiddleware[_RubricState, Any, Any]):
    """按用户意图逐 run 把匹配规则的 rubric 注入 state;不匹配注入空串(空 rubric 即 no-op)。

    正则命中优先(未配 triggers=catch-all;坏正则不算),否则交 router 或 none。每 run 无条件重写 rubric,防跨 run 残留。
    """

    state_schema = _RubricState

    def __init__(self, rules: list[_Rule], router: _Router | None = None) -> None:
        super().__init__()
        self._rules = rules
        self._router = router

    def _match(self, text: str) -> str | None:
        for rule in self._rules:
            if rule.catch_all or (rule.patterns and any(pattern.search(text) for pattern in rule.patterns)):
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
    # 公开裁决镜像:供刷新后经 getState 恢复自审结论。deepagents 的 _rubric_status/_rubric_evaluations
    # 标了 PrivateStateAttr(OmitFromSchema output=True),不进 get_state 输出;ui 走 push_ui_message
    # 侧信道也不够稳。故另写一个走正常 state 通道的公开键(LastValue,存最近一轮终态)。
    review: NotRequired[dict[str, Any]]


class RubricReviewUI(RubricMiddleware):
    """自审终态时发一条持久化 ui 裁决卡(挂到最终回复)。覆写须重挂 @hook_config,否则丢 can_jump_to 断修订循环。"""

    state_schema = _RubricUIState

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        update = super().after_agent(state, runtime)
        mirror = _push_verdict(update, state)
        return {**(update or {}), **mirror} if mirror else update

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        update = await super().aafter_agent(state, runtime)
        mirror = _push_verdict(update, state)
        return {**(update or {}), **mirror} if mirror else update

    def _ensure_grader(self) -> Any:
        # 强制 ProviderStrategy(json_schema):默认 tool 策略下 gemini 格式化不出复杂 criteria、反复重试。
        # 依赖 RubricMiddleware(@beta)私有属性,契约变则响亮失败(不再降级)。
        if self._grader is None:
            self._grader = create_agent(
                model=self._model,
                system_prompt=self._system_prompt,
                tools=self._tools,
                name=RUBRIC_GRADER_MESSAGE_SOURCE,
                response_format=ProviderStrategy(GraderResponse),
            )
        return self._grader


def _push_verdict(update: dict[str, Any] | None, state: Any) -> dict[str, Any] | None:
    """终态:推一条持久 UI 裁决卡,并返回公开裁决镜像 {"review": props}(由调用方并入 state,供刷新恢复);
    非终态/无裁决返回 None。"""
    if not update or update.get("jump_to"):  # 非终态不落持久卡
        return None
    status = update.get("_rubric_status")
    evals = update.get("_rubric_evaluations") or []
    if not status or not evals:
        return None
    ai = next((m for m in reversed(state.get("messages") or []) if isinstance(m, AIMessage)), None)
    if ai is None:
        return None
    last = evals[-1]
    props = {
        "verdict": status,
        "pass": (last.get("iteration") or 0) + 1,
        "explanation": last.get("explanation") or "",
        "criteria": last.get("criteria") or [],
    }
    try:
        push_ui_message("rubric_verdict", props, message=ai, state_key="ui")
    except Exception:  # noqa: BLE001 — UI 副作用失败不中断 run
        pass
    return {"review": props}


def _on_evaluation(evaluation: RubricEvaluation) -> None:
    """rubric 裁决:结构化日志 + 上报 Langfuse score。没过审却收尾(failed/max_iterations/grader_error)用 warning。"""
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
    resolved: ResolvedConfig,
    grader: BaseChatModel,
    gate: BaseChatModel | None = None,
    backend: Any = None,
) -> list[AgentMiddleware[Any, Any, Any]]:
    """自审:配了 review 规则才启用(RubricSeed 路由 + RubricReviewUI 迭代裁决);恒挂载、不匹配零成本。"""
    review = resolved.review
    rules = _compile_rules(review.rules)
    if not rules:
        return []
    router = _Router(gate, review.gate_prompt or ROUTE_PROMPT, rules) if gate is not None else None
    return [
        RubricSeed(rules, router),
        RubricReviewUI(
            model=grader,
            tools=_grader_tools(backend),
            max_iterations=review.max_iterations,
            on_evaluation=_on_evaluation,
        ),
    ]
