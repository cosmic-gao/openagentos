"""Chat model 工厂:默认走 OpenAI 兼容网关;`anthropic:` 前缀改用原生 Anthropic 以启用 prompt caching。"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.language_models import BaseChatModel
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
        # stream_usage=True 强制流式带 stream_options.include_usage → 流式响应回传 token usage;
        # 否则自定义网关 base_url 下 langchain 默认不请求,Langfuse 拿不到 token(网关须支持该参数)。
        llm = ChatOpenAI(
            model=cast(str, model),
            base_url=base_url,
            api_key=SecretStr(api_key or "EMPTY"),
            stream_usage=stream_usage,
            **extra,
        )
    if context_window:
        # 网关自定义模型名 langchain 认不出窗口(profile=None)→ summarization 退回固定 170k、与真实窗口脱钩。
        # 注入 max_input_tokens 使其改按"窗口 85%"触发,撞限前优雅压缩(合并进已推断的 profile,不覆盖其余字段)。
        llm.profile = {**(llm.profile or {}), "max_input_tokens": context_window}
    return llm
