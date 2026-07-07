"""Chat model 工厂：OpenAI 兼容网关（MSPbots / Azure / LiteLLM / vLLM / Ollama）。"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from agentos.config import DEFAULT_MODEL


def build(
    *,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    temperature: float | None = None,
    model_params: dict[str, Any] | None = None,
) -> BaseChatModel:
    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL 未配置（全局 .env 或 assistant config.configurable）。")
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_MODEL,
        "base_url": base_url,
        # 无鉴权本地网关（vLLM/Ollama）可用任意 key
        "api_key": SecretStr(api_key or "EMPTY"),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if model_params:
        kwargs.update(model_params)
    return ChatOpenAI(**kwargs)
