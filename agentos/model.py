"""Chat model 工厂:OpenAI 兼容网关(Azure / LiteLLM / vLLM / Ollama)。"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def build(*, model: str | None, base_url: str | None, api_key: str | None) -> BaseChatModel:
    missing = [name for name, value in (("model", model), ("base_url", base_url)) if not value]
    if missing:
        raise ValueError(
            f"missing {' + '.join(missing)}: set assistant config.configurable or OPENAI_* env"
        )
    assert model is not None and base_url is not None  # 上面已校验非空,收窄类型给 ChatOpenAI
    return ChatOpenAI(model=model, base_url=base_url, api_key=SecretStr(api_key or "EMPTY"))
