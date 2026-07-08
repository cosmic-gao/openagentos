"""Chat model 工厂:默认 OpenAI 兼容网关;`anthropic:` 前缀走原生 Anthropic(拿 prompt caching)。"""

from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(*, model: str | None, base_url: str | None, api_key: str | None) -> BaseChatModel:
    # 由上游保证,此处只收窄类型
    name = model or ""
    if name.startswith("anthropic:"):
        # 原生 Anthropic:激活默认栈的 prompt caching;init_chat_model 避开 ChatAnthropic 的类型检查误报
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
