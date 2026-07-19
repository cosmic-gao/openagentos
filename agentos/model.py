"""Chat model 工厂:默认走 OpenAI 兼容网关;`anthropic:` 前缀改用原生 Anthropic 以启用 prompt caching。"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.language_models import BaseChatModel, ModelProfile
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(
    *,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    timeout: float | None = None,
    max_retries: int | None = None,
    context_window: int | None = None,
    stream_usage: bool = True,
) -> BaseChatModel:
    name = model or ""
    extra: dict[str, Any] = {}
    if timeout is not None:
        extra["timeout"] = timeout
    if max_retries is not None:
        extra["max_retries"] = max_retries
    if name.startswith("anthropic:"):
        from langchain.chat_models import init_chat_model

        llm = init_chat_model(
            name.removeprefix("anthropic:"),
            model_provider="anthropic",
            base_url=base_url,
            api_key=api_key or "EMPTY",
            **extra,
        )
    else:
        if name.startswith("openai:"):
            model = name.removeprefix("openai:")
        # stream_usage=True 让流式回传 token usage(Langfuse 成本统计所需;网关须支持该参数)。
        llm = ChatOpenAI(
            model=cast(str, model),
            base_url=base_url,
            api_key=SecretStr(api_key or "EMPTY"),
            stream_usage=stream_usage,
            **extra,
        )
    if context_window:
        # 网关自定义模型名认不出窗口→summarization 退回固定 170k;注入 max_input_tokens 改按窗口 85% 触发。
        llm.profile = cast(ModelProfile, {**(llm.profile or {}), "max_input_tokens": context_window})
    return llm
