"""Chat model 工厂:默认 OpenAI 兼容网关;`anthropic:` 前缀走原生 Anthropic(拿 prompt caching)。"""

from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(*, model: str | None, base_url: str | None, api_key: str | None) -> BaseChatModel:
    # model/base_url 由上游保证;此处仅收窄类型,不做校验。
    name = model or ""
    if name.startswith("anthropic:"):
        # 原生 Anthropic:激活默认栈的 AnthropicPromptCachingMiddleware;base_url 须指向 Anthropic 协议端点。
        # 用官方 init_chat_model 工厂(避开 ChatAnthropic 的 alias 构造在类型检查下的误报)。
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
