"""配置:每助手 AgentConfig(config.configurable)+ 全局 Settings(env)兜底。

assistant schema:``{"configurable": {"model", "prompt", "api_key", "base_url", "assistant_id"}}``;
model/api_key/base_url 缺项回退 OPENAI_* 环境变量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langgraph.config import get_config
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

SYSTEM_PROMPT = """\
You are OpenAgentOS, a capable, methodical general-purpose agent.

Operating principles:
- Plan first. For any non-trivial or multi-step task, use `write_todos` to lay
  out the steps, then work through them and keep the list updated.
- Your working directory `/workspace` is persistent per conversation — files
  you create there survive across messages. Use it for notes, drafts, and
  deliverables instead of keeping everything in the conversation.
- Reusable skills live under `/workspace/skills`; consult them before solving
  a problem from scratch.
- Delegate deep, self-contained research to the `research-agent` subagent via
  the `task` tool. Give it a precise, standalone question and let it return a
  synthesized answer; don't micromanage its steps.
- When a file in `/workspace` is a deliverable the user should download
  (report, spreadsheet, image, archive, …), call `share_file` with its path
  and give the user the returned link. Do not share scratch files.
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

    workspace: str = "workspace"
    workspace_host: str | None = None
    workspace_claim: str | None = None
    public_url: str = ""

    sandbox_enabled: bool = True
    sandbox_image: str = "python:3.12"
    sandbox_ttl: int = 1800
    sandbox_timeout: int | None = None

    opensandbox_domain: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_DOMAIN")
    opensandbox_api_key: str | None = Field(default=None, validation_alias="OPEN_SANDBOX_API_KEY")
    protocol: str = "http"
    server_proxy: bool = True


def get_settings() -> Settings:
    return Settings()


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    prompt: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    assistant_id: str | None = None

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


def resolve(config: AgentConfig, settings: Settings) -> ResolvedConfig:
    return ResolvedConfig(
        model=config.model or settings.model,
        base_url=config.base_url or settings.base_url,
        api_key=config.api_key or settings.api_key,
        prompt=config.prompt,
    )


def configurable(config: dict[str, Any] | None) -> dict[str, Any]:
    return (config or {}).get("configurable") or {}


def current_thread_id() -> str:
    """当前运行的 thread id;不在图执行上下文时回退 default。"""
    try:
        cfg = get_config() or {}
    except Exception:
        return "default"
    conf = cfg.get("configurable") or {}
    meta = cfg.get("metadata") or {}
    return conf.get("thread_id") or meta.get("thread_id") or "default"


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_segment(value: str | None, fallback: str = "") -> str:
    """消毒单段路径名:非安全字符替换为下划线,剥离首尾点/下划线,空则回退。"""
    cleaned = _UNSAFE.sub("_", value or "").strip("._")
    return cleaned or fallback
