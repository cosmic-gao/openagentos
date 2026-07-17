"""配置:每助手 AgentConfig(来自 config.configurable)+ 全局 Settings(env)兜底。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from langgraph.config import get_config
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PIIStrategy = Literal["off", "block", "redact", "mask", "hash"]
RedactionStrategy = Literal["block", "redact", "mask", "hash"]  # PIIStrategy 去掉 off:实际会脱敏的策略
Permission = Literal["allow", "ask", "deny"]

TOOL_ALIASES = {"bash": "execute", "read": "read_file", "write": "write_file", "edit": "edit_file"}


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
    # 网关自定义模型名 langchain 认不出上下文窗口,summarization 会退回固定 170k 阈值、与真实窗口脱钩;
    # 显式设窗口(tokens)后改按"窗口 85%"触发,撞限前优雅压缩。缺省 None=沿用 langchain 按模型名推断。
    context_window: int | None = None
    stream_usage: bool = True

    model_max_retries: int = 2
    tool_max_retries: int = 2
    tool_call_limit: int | None = None
    fallback_model: str | None = None
    pii_strategy: PIIStrategy = "off"
    # 密钥/凭据兜底脱敏(纵深防御最后一道,独立于 pii_strategy,默认开)。
    secret_redaction: bool = True
    secret_redaction_strategy: RedactionStrategy = "redact"
    context_editing: bool = True
    tool_selector_max: int | None = None

    workspace: str = "workspace"
    workspace_host: str | None = None
    workspace_claim: str | None = None
    public_url: str = ""

    sandbox_image: str = "python:3.12"
    sandbox_ttl: int = 300
    sandbox_timeout: int | None = None
    sandbox_cpu: str = "1"
    sandbox_memory: str = "2Gi"
    # 会话目录回收:sandbox/<tid>/ 空闲(无文件改动)超 retention 秒后由后台 sweeper 删除;<=0 关闭。
    sandbox_retention: int = 604800
    sandbox_sweep_interval: int = 3600

    memory_enabled: bool = True

    # Langfuse score 上报凭据(复用 aegra 同一套 env;缺失则 score 静默禁用)。
    langfuse_public_key: str | None = Field(default=None, validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str | None = Field(default=None, validation_alias="LANGFUSE_BASE_URL")

    opensandbox_domain: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_DOMAIN")
    opensandbox_api_key: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_API_KEY")
    protocol: str = "http"
    server_proxy: bool = True


@lru_cache
def get_settings() -> Settings:
    """全局配置单例:env/.env 只读一次(pydantic-settings 每次实例化都会重读文件+重解析)。"""
    return Settings()


class ReviewRule(BaseModel):
    """一条审查规则:内容命中(triggers 正则 / LLM 路由归到 name)则用本 rubric 审。"""

    model_config = ConfigDict(extra="ignore")

    rubric: str
    name: str = "default"
    triggers: list[str] = Field(default_factory=list)  # 空=catch-all,总匹配
    description: str = ""  # 供 LLM 路由匹配


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rules: list[ReviewRule] = Field(default_factory=list)
    max_iterations: int = 3
    model: str | None = None  # grader 评分 + gate 路由共用;缺省复用主模型
    gate: bool = False
    gate_prompt: str | None = None

    @field_validator("max_iterations")
    @classmethod
    def _clamp_iterations(cls, v: int) -> int:
        # RubricMiddleware 硬性要求 [1,20],越界会 raise 使整图构建失败;夹取而非拒绝。
        return min(20, max(1, v))


class AgentConfig(BaseModel):
    """每助手配置(来自 run 的 configurable);tools 置 false 或 permission=deny 禁用工具、
    permission=ask 转 HITL 中断,interrupt_on 需 Aegra 注入的 checkpointer 才生效。"""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    prompt: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    assistant_id: str | None = None
    context_window: int | None = None
    stream_usage: bool | None = None
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
    context_window: int | None = None
    stream_usage: bool = True
    steps: int | None = None
    fallback_model: str | None = None
    pii_strategy: PIIStrategy = "off"
    review: ReviewConfig = field(default_factory=ReviewConfig)
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
        context_window=config.context_window or settings.context_window,
        stream_usage=settings.stream_usage if config.stream_usage is None else config.stream_usage,
        steps=config.steps,
        fallback_model=config.fallback_model or settings.fallback_model,
        pii_strategy=config.pii_strategy or settings.pii_strategy,
        review=config.review,
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
