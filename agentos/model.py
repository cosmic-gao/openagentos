"""Chat model factory.

OpenAgentOS talks to an OpenAI-compatible gateway (the MSPbots gateway, Azure
OpenAI, LiteLLM, vLLM, Ollama, ...). The endpoint, key and model are all driven
by environment variables, so the same code runs against any backend with no edits.

Environment variables (aliases in parentheses):
    OPENAI_BASE_URL  (AGENTOS_BASE_URL)   Base URL of the OpenAI-compatible gateway.
    OPENAI_API_KEY   (AGENTOS_API_KEY)    API key for the gateway.
    AGENTOS_MODEL                         Default model name (default: "gpt-4o").
    AGENTOS_SUBAGENT_MODEL                Model for subagents (default: AGENTOS_MODEL).
    AGENTOS_TEMPERATURE                   Optional sampling temperature (float).
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "gpt-4o"


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value among the given environment variables."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def get_model(model: str | None = None, *, temperature: float | None = None) -> BaseChatModel:
    """Build a chat model pointed at the configured OpenAI-compatible gateway."""
    model = model or os.environ.get("AGENTOS_MODEL", DEFAULT_MODEL)
    base_url = _first_env("OPENAI_BASE_URL", "AGENTOS_BASE_URL")
    api_key = _first_env("OPENAI_API_KEY", "AGENTOS_API_KEY")

    if temperature is None:
        raw = os.environ.get("AGENTOS_TEMPERATURE")
        temperature = float(raw) if raw else None

    if not base_url:
        raise RuntimeError(
            "OPENAI_BASE_URL (or AGENTOS_BASE_URL) is not set. Copy .env.example "
            "to .env and point it at your OpenAI-compatible gateway."
        )

    kwargs: dict = {
        "model": model,
        "base_url": base_url,
        # Many gateways require a key; keyless local gateways (vLLM/Ollama) accept any.
        "api_key": api_key or "EMPTY",
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    return ChatOpenAI(**kwargs)


def get_subagent_model() -> BaseChatModel:
    """Model used by subagents; falls back to the main model when unset."""
    return get_model(os.environ.get("AGENTOS_SUBAGENT_MODEL"))
