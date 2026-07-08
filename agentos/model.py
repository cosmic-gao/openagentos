"""Chat model 工厂:默认走 OpenAI 兼容网关;`anthropic:` 前缀改用原生 Anthropic 以启用 prompt caching。"""

from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(*, model: str | None, base_url: str | None, api_key: str | None) -> BaseChatModel:
    name = model or ""
    if name.startswith("anthropic:"):
        from langchain.chat_models import init_chat_model

        return init_chat_model(
            name.removeprefix("anthropic:"),
            model_provider="anthropic",
            base_url=base_url,
            api_key=api_key or "EMPTY",
        )
    if name.startswith("openai:"):
        model = name.removeprefix("openai:")
    return ChatOpenAI(model=cast(str, model), base_url=base_url, api_key=SecretStr(api_key or "EMPTY"))
