"""Chat model 工厂:OpenAI 兼容网关(Azure / LiteLLM / vLLM / Ollama)。"""

from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(*, model: str | None, base_url: str | None, api_key: str | None) -> BaseChatModel:
    # model 由上游(assistant config 或 OPENAI_* env)保证非空;此处仅收窄类型,不做校验。
    return ChatOpenAI(model=cast(str, model), base_url=base_url, api_key=SecretStr(api_key or "EMPTY"))
