"""Chat model 工厂：OpenAI 兼容网关。每助手 config.json 覆盖，缺项回退全局 env。"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

DEFAULT_MODEL = "gpt-4o"


def _first_env(*names: str) -> str | None:
    return next((v for name in names if (v := os.environ.get(name))), None)


def get_model(
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    model = model or os.environ.get("AGENTOS_MODEL", DEFAULT_MODEL)
    base_url = base_url or _first_env("OPENAI_BASE_URL", "AGENTOS_BASE_URL")
    api_key = api_key or _first_env("OPENAI_API_KEY", "AGENTOS_API_KEY")
    if temperature is None:
        raw = os.environ.get("AGENTOS_TEMPERATURE")
        temperature = float(raw) if raw else None

    if not base_url:
        raise RuntimeError("OPENAI_BASE_URL 未配置（全局 .env 或 .deepagent/<id>/config.json）。")

    kwargs: dict = {"model": model, "base_url": base_url, "api_key": api_key or "EMPTY"}
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatOpenAI(**kwargs)


def model_from_config(cfg: dict | None) -> BaseChatModel:
    """按助手 config.json（OPENAI_MODEL/BASE_URL/API_KEY）覆盖构造模型。"""
    cfg = cfg or {}
    return get_model(cfg.get("OPENAI_MODEL"), base_url=cfg.get("OPENAI_BASE_URL"), api_key=cfg.get("OPENAI_API_KEY"))
