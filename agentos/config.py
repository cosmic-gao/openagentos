"""配置:每助手 AgentConfig(来自 config.configurable)+ 全局 Settings(env)兜底。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from langgraph.config import get_config
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PIIStrategy = Literal["off", "block", "redact", "mask", "hash"]
Permission = Literal["allow", "ask", "deny"]

TOOL_ALIASES = {"bash": "execute", "read": "read_file", "write": "write_file", "edit": "edit_file"}

SYSTEM_PROMPT = """\
You are OpenAgentOS, a capable general-purpose agent working in a real,
persistent environment. You plan, run code, edit files, use tools, and deliver
finished work end to end — like a skilled engineer who owns the task.

Operating principles:
- Plan before acting. For any multi-step task, use `write_todos` to lay out the
  steps, then work through them and keep the list current.
- `/workspace` is persistent for the whole conversation and shared with your
  sandbox — files you write there survive across messages and tool calls. Keep
  durable work there; put scratch and intermediate files under `/tmp`.
- You have a real shell via `execute`: run commands, install packages, and test
  your work instead of guessing.
- Reusable skills live under `/workspace/skills`; consult them before solving a
  problem from scratch.
- Delegate deep, self-contained research to the `research-agent` subagent via
  the `task` tool — give it one precise, standalone question and build on its
  synthesized answer; don't micromanage its steps.
- When a file in `/workspace` is a deliverable for the user (report,
  spreadsheet, image, archive, …), call `download_file` with its path and hand
  the user the returned link. Never expose scratch or intermediate files.
- Verify before claiming done: run it, read the output, confirm the result.
  State assumptions explicitly and cite sources when you rely on web results.
- Be concise and direct: lead with the answer or result, then the detail that
  matters.
"""

RESEARCH_PROMPT = """\
You are a meticulous research subagent.

- Decompose the question into concrete sub-questions.
- Use `internet_search` to gather several independent sources before concluding.
- Cross-check claims and prefer primary or official sources over aggregators.
- Save lengthy raw findings to the filesystem, then return a concise, well-
  organized synthesis with inline source URLs. Do not pad the answer.
"""

HARNESS_SUFFIX = """\
<use_parallel_tool_calls>
If you intend to call multiple tools with no dependencies between them, make all
the independent calls in parallel rather than sequentially — e.g. reading three
files is three tool calls in one turn. Only sequence calls when a later one
depends on an earlier result. Never use placeholders or guess missing parameters.
</use_parallel_tool_calls>

<investigate_before_answering>
Never speculate about code or state you have not observed. If the user references
a file, read it before answering; investigate relevant files, run the check, or
search before making claims. Give grounded, hallucination-free answers.
</investigate_before_answering>

<tool_result_reflection>
After receiving tool results, reflect on their quality and plan the best next
step before proceeding, rather than reflexively continuing.
</tool_result_reflection>
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")
    base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")

    model_max_retries: int = 2
    tool_max_retries: int = 2
    tool_call_limit: int | None = None
    fallback_model: str | None = None
    pii_strategy: PIIStrategy = "off"
    # 密钥/凭据兜底脱敏(纵深防御最后一道,独立于 pii_strategy,默认开)。
    secret_redaction: bool = True
    secret_redaction_strategy: Literal["redact", "mask", "hash", "block"] = "redact"
    context_editing: bool = True
    tool_selector_max: int | None = None

    workspace: str = "workspace"
    workspace_host: str | None = None
    workspace_claim: str | None = None
    public_url: str = ""

    sandbox_image: str = "python:3.12"
    sandbox_ttl: int = 1800
    sandbox_timeout: int | None = None
    sandbox_cpu: str = "1"
    sandbox_memory: str = "2Gi"

    memory_enabled: bool = True

    opensandbox_domain: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_DOMAIN")
    opensandbox_api_key: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_API_KEY")
    protocol: str = "http"
    server_proxy: bool = True


@lru_cache
def get_settings() -> Settings:
    """全局配置单例:env/.env 只读一次(pydantic-settings 每次实例化都会重读文件+重解析)。"""
    return Settings()


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rubric: str | None = None
    max_iterations: int = 3


class AgentConfig(BaseModel):
    """每助手配置(来自 run 的 configurable);tools 置 false 或 permission=deny 禁用工具、
    permission=ask 转 HITL 中断,interrupt_on 需 Aegra 注入的 checkpointer 才生效。"""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    prompt: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    assistant_id: str | None = None
    steps: int | None = None
    fallback_model: str | None = None
    pii_strategy: PIIStrategy | None = None
    tools: dict[str, bool] = Field(default_factory=dict)
    permission: dict[str, Permission] = Field(default_factory=dict)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    interrupt_on: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedConfig:
    model: str | None
    base_url: str | None
    api_key: str | None
    prompt: str | None
    steps: int | None = None
    fallback_model: str | None = None
    pii_strategy: PIIStrategy = "off"
    rubric: str | None = None
    review_max_iterations: int = 3
    excluded_tools: list[str] = field(default_factory=list)
    interrupt_on: dict[str, Any] = field(default_factory=dict)


def _tool_policy(config: AgentConfig) -> tuple[list[str], dict[str, Any]]:
    """tools/permission → (禁用工具名, ask→HITL)。"""

    def real(name: str) -> str:
        return TOOL_ALIASES.get(name, name)

    excluded: dict[str, None] = {}
    ask: dict[str, Any] = {}
    for name, on in config.tools.items():
        if on is False:
            excluded[real(name)] = None
    for name, perm in config.permission.items():
        if perm == "deny":
            excluded[real(name)] = None
        elif perm == "ask":
            ask[real(name)] = True
    return list(excluded), ask


def resolve(config: AgentConfig, settings: Settings) -> ResolvedConfig:
    excluded, ask = _tool_policy(config)
    return ResolvedConfig(
        model=config.model or settings.model,
        base_url=config.base_url or settings.base_url,
        api_key=config.api_key or settings.api_key,
        prompt=config.prompt,
        steps=config.steps,
        fallback_model=config.fallback_model or settings.fallback_model,
        pii_strategy=config.pii_strategy or settings.pii_strategy,
        rubric=config.review.rubric,
        review_max_iterations=config.review.max_iterations,
        excluded_tools=excluded,
        interrupt_on={**ask, **(config.interrupt_on or {})},
    )


def configurable(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get("configurable") or {}


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_segment(value: str | None, fallback: str = "default") -> str:
    """消毒单段路径名:非安全字符→_、剥离首尾点/下划线(挡 .. 与穿越),空则回退。"""
    return _UNSAFE.sub("_", value or "").strip("._") or fallback


def current_thread_id() -> str:
    """当前 run 的 thread id(已消毒);不在图执行上下文时回退 default。"""
    try:
        cfg = get_config() or {}
    except Exception:
        return "default"
    conf = cfg.get("configurable") or {}
    return safe_segment(conf.get("thread_id") or cfg.get("metadata", {}).get("thread_id"))
