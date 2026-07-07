"""配置层：全局 Settings(env) + 每助手 AgentConfig(configurable) + resolve → ResolvedConfig。

每助手的 model/base_url/api_key/prompt/mcpServers/skills 来自 Aegra assistant 的 config
字段（config.configurable）；model/base_url/api_key/temperature 缺项回退全局 env。系统提示
与运行时上下文小工具（configurable、current_thread_id）也集中于此。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from langgraph.config import get_config
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "gpt-4o"

SYSTEM_PROMPT = """\
You are OpenAgentOS, a capable, methodical general-purpose agent.

Operating principles:
- Plan first. For any non-trivial or multi-step task, use `write_todos` to lay
  out the steps, then work through them and keep the list updated.
- Use the virtual filesystem (`write_file`, `read_file`, `edit_file`, `ls`,
  `glob`, `grep`) to hold notes, drafts, and intermediate artifacts instead of
  keeping everything in the conversation.
- Delegate deep, self-contained research to the `research-agent` subagent via
  the `task` tool. Give it a precise, standalone question and let it return a
  synthesized answer; don't micromanage its steps.
- When you produce a file the user should download (report, spreadsheet, image,
  archive, …), call `export_artifact` with its sandbox path and give the user
  the returned download link. Keep scratch/intermediate files in the filesystem;
  export only deliverables. (Available only when the sandbox is enabled.)
- State assumptions explicitly, cite sources when you rely on web results, and
  finish with a clear, well-structured answer.
"""

RESEARCH_PROMPT = """\
You are a meticulous research subagent.

- Decompose the question into concrete sub-questions.
- Use `internet_search` to gather several independent sources before concluding.
- Cross-check claims and prefer primary or official sources over aggregators.
- Save lengthy raw findings to the filesystem, then return a concise, well-
  organized synthesis with inline source URLs. Do not pad the answer.
"""


class Settings(BaseSettings):
    """全局兜底（env）。每助手可在 assistant config 覆盖 model/base_url/api_key/temperature。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", protected_namespaces=()
    )

    model: str = Field(default=DEFAULT_MODEL, validation_alias="AGENTOS_MODEL")
    base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    temperature: float | None = Field(default=None, validation_alias="AGENTOS_TEMPERATURE")


def get_settings() -> Settings:
    return Settings()


class AgentConfig(BaseModel):
    """每助手配置，从 assistant 的 config.configurable 解析（逐字段容错）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    model: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_MODEL", "model"))
    base_url: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_BASE_URL", "base_url"))
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "api_key"))
    prompt: str | None = Field(default=None, validation_alias=AliasChoices("prompt", "system_prompt"))
    temperature: float | None = None
    mcp_servers: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("mcpServers", "mcp_servers"))
    skills: list[str] = Field(default_factory=list)

    @classmethod
    def parse(cls, configurable: dict[str, Any] | None) -> AgentConfig:
        try:
            return cls.model_validate(configurable or {})
        except ValidationError:
            return cls()


@dataclass(frozen=True)
class ResolvedConfig:
    model: str | None
    base_url: str | None
    api_key: str | None
    prompt: str | None
    temperature: float | None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)


def resolve(config: AgentConfig, settings: Settings) -> ResolvedConfig:
    """合并每助手 config 与全局 settings（config 优先，缺项回退 env）。"""
    return ResolvedConfig(
        model=config.model or settings.model,
        base_url=config.base_url or settings.base_url,
        api_key=config.api_key or settings.api_key,
        prompt=config.prompt,
        temperature=config.temperature if config.temperature is not None else settings.temperature,
        mcp_servers=dict(config.mcp_servers),
        skills=list(config.skills),
    )


def configurable(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get("configurable") or {}


def current_thread_id() -> str:
    """当前会话线程 id（沙箱按线程隔离）；不在图执行上下文时回退 default。"""
    try:
        cfg = get_config() or {}
    except Exception:  # noqa: BLE001
        return "default"
    conf = cfg.get("configurable") or {}
    meta = cfg.get("metadata") or {}
    return conf.get("thread_id") or meta.get("thread_id") or "default"


_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


def safe_segment(value: str, fallback: str = "", *, strip_dots: bool = True) -> str:
    """消毒单段路径名：非安全字符替换为下划线；strip_dots 时再剥离首尾点/下划线；空则回退。

    集中于此供 artifacts / builder 复用（参考项目把这类路径安全放在 config.segment）。
    """
    cleaned = _UNSAFE_SEGMENT.sub("_", value or "")
    if strip_dots:
        cleaned = cleaned.strip("._")
    return cleaned or fallback
