"""密钥/凭据兜底脱敏:从 agent 的输入/输出/工具结果里 redact 掉 API key、token、私钥等。

**纵深防御的最后一道**——第一道仍是别把密钥放到 agent 读得到的位置(工作区 / 提示词 /
技能目录 / .mcp.json)。基于 LangChain `PIIMiddleware` 的 callable detector 扩展点落地,
规则取自 gitleaks / detect-secrets / trufflehog 的高可信、低误报模式;正则 best-effort,挡不住所有形态。

官方 detector 传正则串时只能 redact 整个 match,故用 callable 返回精确 span:对「整段即密钥」
取 group 0,对「只 redact 值/凭据段」的规则(URL 密码、key=value 右值)取捕获组、保留上下文。
callable 的每个 match 自带 type,故一个中间件一次扫描即覆盖多种密钥类型。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from langchain.agents.middleware import PIIMiddleware

# 与 PIIMiddleware 的 strategy 取值对齐:redact 占位 / mask 留尾 / hash 摘要 / block 检到即抛错中断。
Strategy = Literal["block", "redact", "mask", "hash"]

if TYPE_CHECKING:
    # 仅类型引用:官方 detector 契约用的 PIIMatch(TypedDict);运行时返回等价 dict 字面量,无需导入。
    from langchain.agents.middleware._redaction import PIIMatch

# (type, 正则, 取作 redact span 的捕获组)。group=0 整段即密钥;group>0 只 redact 该组、保留上下文。
_Rule = tuple[str, re.Pattern[str], int]

_RULES: list[_Rule] = [
    # ── 云厂商 / 平台密钥(整段即密钥)──
    ("aws_access_key_id", re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}\b"), 0),
    ("gcp_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_\-]+"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"), 0),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), 0),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), 0),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), 0),
    ("twilio_key", re.compile(r"\bSK[0-9a-fA-F]{32}\b"), 0),
    # ── 结构化凭据 ──
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), 0),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{20,}={0,2}"), 0),
    # ── 只 redact 凭据段、保留上下文(group 1)──
    ("basic_auth_url", re.compile(r"(?i)\b(?:https?|ftp|postgres(?:ql)?|redis|mongodb(?:\+srv)?|amqp)://[^\s:/@]+:([^\s:/@]+)@"), 1),
    ("assigned_secret", re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b\s*[:=]\s*['"]?([A-Za-z0-9._\-/+=]{8,})['"]?"""), 1),
]


def _dedupe(matches: list[PIIMatch]) -> list[PIIMatch]:
    """丢弃重叠 span(同一密钥被多条规则命中时),保留起点靠前、更长的那条,避免重复 redact 串位。"""
    matches.sort(key=lambda m: (m["start"], -(m["end"] - m["start"])))
    kept: list[PIIMatch] = []
    last_end = -1
    for m in matches:
        if m["start"] >= last_end:
            kept.append(m)
            last_end = m["end"]
    return kept


def _detect_secrets(content: str) -> list[PIIMatch]:
    """扫描文本返回命中的密钥 span(PIIMiddleware 的 callable detector 契约:dict 需含 start/end/value/type)。"""
    found: list[PIIMatch] = []
    for pii_type, pattern, group in _RULES:
        for m in pattern.finditer(content):
            start, end = m.span(group)
            if start < 0:  # 该捕获组未参与匹配
                continue
            found.append({"type": pii_type, "value": m.group(group), "start": start, "end": end})
    return _dedupe(found)


def secret_middleware(strategy: Strategy = "redact") -> PIIMiddleware:
    """覆盖输入/输出/工具结果的密钥脱敏中间件(单次扫描、多类型占位)。

    apply_to_output=True 会额外装流式 transformer,在途 redact 模型回复,不必等整条消息落定。
    """
    return PIIMiddleware(
        "secret",
        detector=_detect_secrets,
        strategy=strategy,
        apply_to_input=True,
        apply_to_output=True,
        apply_to_tool_results=True,
    )
