"""MS Teams 通道:Bot Framework webhook ⇄ 本地 Agent Protocol 桥接(纯 HTTP,无 Bot SDK)。

多租户 / 多 agent / 多 bot:每个 agent 一个独立 Azure Bot,webhook 路径带该**平台 agent id**(稳定,
assistant 删除重建也不变;routes/webhooks.py 挂 POST /webhooks/msteams/{agent_id}),凭据按 agent 存于其
assistant 配置 config.configurable.msteams(由平台 UI 写入),运行时按 agent_id 反查 assistant 读取——
不再用全局 env 单 bot。

流程(webhook(agent_id, body, authorization)):
1. 按 agent_id 反查 assistant(metadata.agent_id)并读其 bot 凭据(system 身份跨租户;enabled/缺凭据→404);
2. 入站 JWT 验证(Bot Framework OpenID → JWKS,校验 aud=该 bot app_id、iss、serviceUrl claim);
3. 解析 activity(仅纯文本 DM;群聊/非 message/异租户/自发消息丢弃);
4. 立即 200 ACK(Teams 15 秒硬超时),agent run 放后台任务;
5. 后台:thread_id = uuid5(conversation_id) 确定性映射(零存储,同一 Teams 会话续同一线程)
   → 以该 agent 的 assistant 跑 runs.wait → 取最终 AI 文本,用该 bot 令牌主动回复。

MVP 边界:不做附件 / Adaptive Card / 群聊@ / 流式 / SSO。仅 MSTEAMS_LOCAL_URL 一个 env(本地 AP 地址)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt as pyjwt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

BOTFRAMEWORK_ISSUER = "https://api.botframework.com"
OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOTFRAMEWORK_SCOPE = "https://api.botframework.com/.default"
# 读 assistant 配置 / 跑 run 用的内部身份:agentos/auth.py 中 x-user-id=system → 全局命名空间,
# 可跨租户读取任意 agent 的 assistant 配置(Bot Framework 回调无平台用户身份)。
SYSTEM_USER_ID = "system"

# 入站体积上限:Bot Framework activity 很小,256KB 足够;超限直接拒(未鉴权即拒,DoS 面收敛)。
_MAX_BODY_BYTES = 256 * 1024
# 后台 agent run 的硬超时(秒),防卡死的 run 永挂后台任务、泄漏连接。
_RUN_TIMEOUT_SECONDS = 300
# 入站 JWT 时钟偏移容差(秒):挡服务器时钟略滞后时新令牌 nbf 落在"未来"的误拒;须 < 测试用例 exp=now-60 的余量。
_JWT_LEEWAY_SECONDS = 30
# 关停时排空在飞后台回复的最长等待(秒);超时则取消余下任务再关客户端,避免在飞 _process 撞上已关闭的 client。
_SHUTDOWN_DRAIN_SECONDS = 10
# 出站回复只允许把 bot 令牌发往可信的 Bot Connector 主机(纵深防御;serviceUrl 已经 JWT claim 绑定)。
_TRUSTED_SERVICE_HOST_SUFFIXES = (".botframework.com", ".trafficmanager.net")

# uuid5 固定命名空间:同一 conversation.id 永远映射到同一 Aegra thread(升级/重启不变)。
_THREAD_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "openagentos:msteams:conversation")

_MENTION = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


class MSTeamsSettings(BaseSettings):
    """MS Teams 通道的进程级设置。bot 凭据不再放 env(改为按 agent 存于其 assistant 配置,
    支持多租户/多 agent/多 bot),这里只保留连接本地 Agent Protocol 的地址。"""

    model_config = SettingsConfigDict(
        env_prefix="MSTEAMS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 本地 Agent Protocol 自连地址。用 127.0.0.1(不用 localhost):容器里 localhost 可能先解析到
    # IPv6 ::1,而 uvicorn 只绑 IPv4 → ConnectError;端口跟随 PORT(aegra serve 的实际监听端口)。
    # 可用 MSTEAMS_LOCAL_URL 覆盖。
    local_url: str = Field(default_factory=lambda: f"http://127.0.0.1:{os.environ.get('PORT') or '2026'}")


@lru_cache
def get_msteams_settings() -> MSTeamsSettings:
    """进程级单例(env/.env 只读一次;lru_cache 便于测试 cache_clear)。"""
    return MSTeamsSettings()


@dataclass(frozen=True)
class BotCreds:
    """某个 agent(assistant)的 Teams bot 凭据,来自其 assistant 配置 config.configurable.msteams。"""

    assistant_id: str
    app_id: str
    app_password: str = field(repr=False)  # 不进默认 repr,避免明文密钥随对象被 log/入异常
    tenant_id: str
    allow_from: frozenset[str]


def _disabled(raw: Any) -> bool:
    """enabled 归一化:显式假值(False / 0 / "false" / "no" / "off" / "0")=关闭;缺省或其他=启用。
    仅用 `is False` 会漏掉平台把开关写成字符串/数字的情形(静默关不掉)。"""
    if isinstance(raw, str):
        return raw.strip().lower() in {"false", "0", "no", "off"}
    return raw is False or raw == 0


def bot_creds_from_config(assistant_id: str, configurable: dict[str, Any] | None) -> BotCreds | None:
    """从 assistant 的 configurable.msteams 解析 bot 凭据。返回 None(=不启用)当:
    非法结构 / enabled 归一化为关闭 / 缺 app_id 或 app_password。enabled 缺省视为启用(向后兼容)。"""
    msteams = (configurable or {}).get("msteams")
    if not isinstance(msteams, dict):
        return None
    if _disabled(msteams.get("enabled", True)):
        return None
    app_id = str(msteams.get("app_id") or "").strip()
    app_password = str(msteams.get("app_password") or "").strip()
    if not app_id or not app_password:
        return None
    allow_from = frozenset(
        part.strip() for part in str(msteams.get("allow_from") or "").split(",") if part.strip()
    )
    return BotCreds(
        assistant_id=assistant_id,
        app_id=app_id,
        app_password=app_password,
        tenant_id=str(msteams.get("tenant_id") or "").strip(),
        allow_from=allow_from,
    )


# ── 纯函数面(单元测试覆盖)───────────────────────────────────────────────────


def thread_id_for(conversation_id: str) -> str:
    """Teams conversation.id → 确定性 Aegra thread_id(uuid5,零映射存储)。"""
    return str(uuid.uuid5(_THREAD_NAMESPACE, conversation_id))


def _clean_agent_id(raw: str) -> str:
    """从 webhook 路径段取干净的 agent id:去掉误串进来的网关杂串(如租户路由 "<id>&X_Tenant_ID=...")。
    只保留首个 token(遇 & ? / ; 或空白即止)。"""
    return re.split(r"[&?/;\s]", (raw or "").strip(), maxsplit=1)[0]


def reply_payload(text: str) -> dict[str, str]:
    """Bot Framework 回复体;text 为空时退化为单空格(空 text 会被拒)。"""
    return {"type": "message", "text": text or " "}


@dataclass(frozen=True)
class InboundMessage:
    sender_id: str
    conversation_id: str
    service_url: str
    text: str


def parse_activity(
    activity: dict[str, Any],
    *,
    config_tenant: str | None = None,
    allow_from: frozenset[str] | None = None,
) -> InboundMessage | None:
    """提取纯文本 DM;不符合 MVP 边界的 activity 一律返回 None(调用方直接 200 吞掉)。"""
    if activity.get("type") != "message":
        return None

    conversation = activity.get("conversation") or {}
    from_user = activity.get("from") or {}
    recipient = activity.get("recipient") or {}
    channel_data = activity.get("channelData") or {}

    sender_id = str(from_user.get("aadObjectId") or from_user.get("id") or "").strip()
    conversation_id = str(conversation.get("id") or "").strip()
    service_url = str(activity.get("serviceUrl") or "").strip()
    if not sender_id or not conversation_id or not service_url:
        return None

    # bot 自己的回执(from == recipient)不再进 agent,防回环。
    if recipient.get("id") and from_user.get("id") == recipient.get("id"):
        return None

    # DM-only:群聊/频道流量不在 MVP 范围。
    conversation_type = str(conversation.get("conversationType") or "").strip()
    if conversation_type and conversation_type != "personal":
        return None

    # 租户边界:配置了 tenant_id 即视为显式的单租户隔离,fail-closed——异租户 *以及缺失租户信息*
    # 的 activity 一律丢弃(入站 JWT 只证明 aud=app_id,不证明来源租户,不能据此放行缺租户请求)。
    activity_tenant = str((channel_data.get("tenant") or {}).get("id") or "").strip()
    config_tenant = (config_tenant or "").strip()
    if config_tenant and activity_tenant.lower() != config_tenant.lower():
        return None

    if allow_from and sender_id not in allow_from:
        return None

    text = _MENTION.sub(" ", str(activity.get("text") or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    return InboundMessage(
        sender_id=sender_id,
        conversation_id=conversation_id,
        service_url=service_url,
        text=text,
    )


class UnknownKid(ValueError):
    """令牌 kid 不在注入的 JWKS 中——多为签名密钥轮换;调用方据此强制刷新 JWKS 重试一次。
    仍是 ValueError 子类,故不刷新的调用方与既有"验签失败即 401"语义不变。"""


def validate_inbound_jwt(
    token: str, *, jwks: dict[str, Any], app_id: str, activity_service_url: str
) -> dict[str, Any]:
    """校验 Bot Framework 签名 JWT(aud=app_id、iss、exp/nbf、serviceUrl 与 activity 一致)。

    JWKS 由调用方注入(get_jwks 负责抓取与缓存),本函数不发网络,任何失败抛 ValueError(未知 kid 抛 UnknownKid)。
    """
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.PyJWTError as exc:
        raise ValueError(f"malformed token: {exc}") from exc

    kid = str(header.get("kid") or "").strip()
    if not kid:
        raise ValueError("missing token kid")
    jwk = next((k for k in jwks.get("keys") or [] if k.get("kid") == kid), None)
    if jwk is None:
        raise UnknownKid(f"signing key not found for kid={kid}")

    try:
        public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = pyjwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=BOTFRAMEWORK_ISSUER,
            leeway=_JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "nbf", "iss", "aud"]},
        )
    except pyjwt.PyJWTError as exc:
        raise ValueError(f"token validation failed: {exc}") from exc

    # serviceUrl 绑定:必须携带 serviceurl claim 且与 activity 一致(fail-closed)。缺 claim 就放行会让
    # 攻击者用任意 activity.serviceUrl 把出站的 bot 令牌导向任意主机(SSRF/令牌外泄),故强制要求。
    claim_service_url = str(claims.get("serviceurl") or claims.get("serviceUrl") or "").strip()
    activity_service_url = (activity_service_url or "").strip()
    if not claim_service_url:
        raise ValueError("token missing serviceurl claim")
    if claim_service_url.rstrip("/") != activity_service_url.rstrip("/"):
        raise ValueError("serviceUrl claim mismatch")
    return claims


def extract_reply_text(state: dict[str, Any]) -> str | None:
    """从 runs.wait 的最终状态取最后一条 AI 消息的文本(内容块只取 text 块)。"""
    for message in reversed(state.get("messages") or []):
        if message.get("type") != "ai":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(part for part in parts if part)
            return text or None
    return None


# ── 出站 token / JWKS 缓存(模块级,进程内共享)───────────────────────────────


class _Expiring:
    """带过期时间的单值缓存 + 并发去重锁。"""

    def __init__(self) -> None:
        self.value: Any = None
        self.expires_at: float = 0.0
        self.lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self.value is not None and time.time() < self.expires_at


# 出站 token 按 app_id 分桶(多 bot 各自的凭据);JWKS 全局共享(Bot Framework 公钥)。
_token_caches: dict[str, _Expiring] = {}
_jwks_cache = _Expiring()


async def get_access_token(http: httpx.AsyncClient, creds: BotCreds) -> str:
    """client_credentials 换某 bot 的 Bot Framework 出站 token,按 app_id 缓存(留 60s 余量)。"""
    cache = _token_caches.get(creds.app_id)
    if cache is None:
        cache = _token_caches.setdefault(creds.app_id, _Expiring())
    if cache.fresh():
        return cache.value
    async with cache.lock:
        if cache.fresh():
            return cache.value
        tenant = creds.tenant_id.strip() or "botframework.com"
        resp = await http.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": creds.app_id,
                "client_secret": creds.app_password,
                "scope": BOTFRAMEWORK_SCOPE,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("token response missing access_token")
        cache.value = token
        # max(0, …):expires_in 极小时不至于算出过去时刻导致每次都刷新。
        cache.expires_at = time.time() + max(0, int(payload.get("expires_in", 3600)) - 60)
        return token


def _json(resp: httpx.Response) -> dict[str, Any]:
    """解析响应 JSON;坏 JSON 抛 RuntimeError(而非 ValueError 子类 JSONDecodeError),
    避免 200+坏体被 webhook 的"验签失败→401"分支误捕(应归 503)。"""
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from {resp.request.url}: {exc}") from exc


async def get_jwks(http: httpx.AsyncClient, *, force: bool = False) -> dict[str, Any]:
    """经 OpenID discovery 抓 Bot Framework JWKS,缓存 1 小时。force=True 跳过缓存强制回源
    (签名密钥轮换、遇未知 kid 时用,否则轮换后最长一小时拒真令牌)。"""
    if not force and _jwks_cache.fresh():
        return _jwks_cache.value
    async with _jwks_cache.lock:
        if not force and _jwks_cache.fresh():
            return _jwks_cache.value
        resp = await http.get(OPENID_CONFIG_URL)
        resp.raise_for_status()
        jwks_uri = str(_json(resp).get("jwks_uri") or "").strip()
        if not jwks_uri:
            raise RuntimeError("Bot Framework OpenID config missing jwks_uri")
        resp = await http.get(jwks_uri)
        resp.raise_for_status()
        _jwks_cache.value = _json(resp)
        _jwks_cache.expires_at = time.time() + 3600
        return _jwks_cache.value


async def _authenticate_inbound(token: str, app_id: str, service_url: str) -> None:
    """验入站 JWT;遇未知 kid(签名密钥轮换)强制刷新一次 JWKS 再验。失败抛 ValueError(→401);
    JWKS 抓取/解析失败抛其他异常(→503),由 webhook 分流。"""
    jwks = await get_jwks(_http())
    try:
        validate_inbound_jwt(token, jwks=jwks, app_id=app_id, activity_service_url=service_url)
    except UnknownKid:
        jwks = await get_jwks(_http(), force=True)
        validate_inbound_jwt(token, jwks=jwks, app_id=app_id, activity_service_url=service_url)


# ── 出站回复 ──────────────────────────────────────────────────────────────────


def _is_trusted_service_url(service_url: str) -> bool:
    """出站只把 bot 令牌发往 https 的可信 Bot Connector 主机(纵深防御)。"""
    parts = urlsplit(service_url)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in _TRUSTED_SERVICE_HOST_SUFFIXES)


async def _post_activity(
    http: httpx.AsyncClient, creds: BotCreds, msg: InboundMessage, payload: dict[str, Any]
) -> None:
    if not _is_trusted_service_url(msg.service_url):
        raise ValueError(f"refusing to send to untrusted serviceUrl host: {msg.service_url}")
    token = await get_access_token(http, creds)
    # conversation_id 来自 activity(未被 JWT 绑定),须 URL 编码防路径/查询注入;serviceUrl 已 claim 绑定。
    url = f"{msg.service_url.rstrip('/')}/v3/conversations/{quote(msg.conversation_id, safe='')}/activities"
    resp = await http.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()


async def send_reply(http: httpx.AsyncClient, creds: BotCreds, msg: InboundMessage, text: str) -> None:
    await _post_activity(http, creds, msg, reply_payload(text))


# ── 后台 run 桥接 ─────────────────────────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None
_lg_client: Any = None
_background_tasks: set[asyncio.Task] = set()


def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


def _langgraph_client() -> Any:
    """复用进程级 langgraph client(每条消息新建会泄漏连接池/FD);以 system 身份读配置/跑 run。"""
    global _lg_client
    if _lg_client is None:
        from langgraph_sdk import get_client  # 延迟导入,避免拉高 routes 模块加载成本

        _lg_client = get_client(
            url=get_msteams_settings().local_url, headers={"x-user-id": SYSTEM_USER_ID}
        )
    return _lg_client


def _pick_assistant_for_agent(found: Any, agent_id: str) -> dict[str, Any] | None:
    """从 assistants.search 结果里挑出 metadata.agent_id 精确匹配的那个(服务端过滤可能宽松,客户端再核一遍)。"""
    items = found if isinstance(found, list) else (
        found.get("assistants") if isinstance(found, dict) else None
    )
    if not isinstance(items, list):
        return None
    for assistant in items:
        if not isinstance(assistant, dict):
            continue
        meta = assistant.get("metadata")
        if isinstance(meta, dict) and str(meta.get("agent_id") or "") == agent_id:
            return assistant
    return None


async def load_bot_creds(agent_id: str) -> BotCreds | None:
    """按平台 agent_id 反查其 assistant(metadata.agent_id),读 config.configurable.msteams 解析凭据。
    用稳定的 agent_id 而非 assistant_id 作 webhook key:assistant 删除重建后 webhook URL 不变。
    system 身份跨租户可读;凭据里带回解析出的 assistant_id 供后续 runs.wait 使用。
    反查的瞬时故障(网络/后端)向上抛,由 webhook 映射 503 让 Teams 重投;查无 assistant/缺凭据才返回 None(→404)。"""
    client = _langgraph_client()
    found = await client.assistants.search(metadata={"agent_id": agent_id}, limit=10)
    assistant = _pick_assistant_for_agent(found, agent_id)
    if assistant is None:
        return None
    assistant_id = str(assistant.get("assistant_id") or assistant.get("id") or "").strip()
    if not assistant_id:
        return None
    config = assistant.get("config")
    configurable = config.get("configurable") if isinstance(config, dict) else None
    return bot_creds_from_config(assistant_id, configurable)


async def _run_agent(creds: BotCreds, msg: InboundMessage) -> str | None:
    """同一 Teams 会话续同一 Aegra 线程,以该 agent 的 assistant 跑 run,取最终 AI 文本(带硬超时)。"""
    client = _langgraph_client()
    tid = thread_id_for(msg.conversation_id)
    await client.threads.create(thread_id=tid, if_exists="do_nothing")
    state = await asyncio.wait_for(
        client.runs.wait(
            tid,
            creds.assistant_id,
            input={"messages": [{"role": "user", "content": msg.text}]},
        ),
        timeout=_RUN_TIMEOUT_SECONDS,
    )
    return extract_reply_text(state if isinstance(state, dict) else {})


async def _process(creds: BotCreds, msg: InboundMessage) -> None:
    """webhook ACK 之后的全部慢路径:typing 提示 → agent run → 主动回复。"""
    http = _http()
    try:
        await _post_activity(http, creds, msg, {"type": "typing"})
    except Exception:
        logger.debug("msteams: typing indicator failed", exc_info=True)

    try:
        reply = await _run_agent(creds, msg)
    except Exception:
        logger.exception("msteams: agent run failed for conversation %s", msg.conversation_id)
        reply = "抱歉,处理你的消息时出错了,请稍后重试。"
    if not reply:
        reply = "(本轮没有产生文本回复)"

    try:
        await send_reply(http, creds, msg, reply)
    except Exception:
        logger.exception("msteams: reply failed for conversation %s", msg.conversation_id)


def _spawn(coro) -> None:
    """挂后台任务并持强引用防 GC;webhook 已 ACK,失败只记日志。"""
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ── webhook 入口(routes/webhooks.py 挂 POST /webhooks/msteams/{agent_id})──────


async def webhook(agent_id: str, body: bytes, authorization: str) -> tuple[int, dict[str, Any]]:
    """路径 /webhooks/msteams/{agent_id}(平台 agent id,稳定):按该 agent 反查 assistant 取 bot 凭据
    → 验 JWT → 解析 activity → 立即 ACK,agent run 交后台。返回 (status, body)。"""
    agent_id = _clean_agent_id(agent_id)
    if not agent_id:
        return 404, {"error": "unknown bot"}

    # 先做廉价的鉴权头与体积检查,再解析——避免未鉴权请求触发 JSON 解析/内存分配(DoS 面收敛)。
    if not authorization.lower().startswith("bearer "):
        return 401, {"error": "unauthorized"}
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return 401, {"error": "unauthorized"}
    if len(body) > _MAX_BODY_BYTES:
        return 413, {"error": "payload too large"}

    try:
        activity = json.loads(body.decode("utf-8"))
        if not isinstance(activity, dict):
            raise ValueError("activity must be a JSON object")
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError 来自深层嵌套 JSON,不是 ValueError 子类,须显式捕获,否则逃逸成 500。
        return 400, {"error": "invalid request body"}

    try:
        creds = await load_bot_creds(agent_id)
    except Exception:
        # 反查 assistant 的瞬时故障(网络/后端)≠ 未配置:返回 503 让 Teams 重投,不当永久失败丢消息。
        logger.exception("msteams: bot creds lookup failed for agent %s", agent_id)
        return 503, {"error": "backend unavailable"}
    if creds is None:
        # 该 agent 未配置 Teams、被关闭、缺凭据、或查不到对应 assistant。
        return 404, {"error": "bot not configured"}

    try:
        await _authenticate_inbound(token, creds.app_id, str(activity.get("serviceUrl") or ""))
    except ValueError as exc:
        logger.warning("msteams: inbound JWT rejected for agent %s: %s", agent_id, exc)
        return 401, {"error": "unauthorized"}
    except Exception:
        logger.exception("msteams: JWKS fetch failed")
        return 503, {"error": "jwks unavailable"}

    msg = parse_activity(
        activity,
        config_tenant=creds.tenant_id,
        allow_from=creds.allow_from or None,
    )
    if msg is None:
        return 200, {}

    _spawn(_process(creds, msg))
    return 200, {}


# ── 生命周期(由 routes/__init__.py 的 lifespan 调用)──────────────────────────


async def prewarm() -> None:
    """启动时预热 JWKS 缓存,避免首个入站请求在 ACK 前做冷缓存的 OpenID+JWKS 两次串行 GET
    而超过 Teams 15 秒 webhook 超时(→ 重投 → 重复 run)。JWKS 全局共享(与具体 bot 无关),
    是否有 agent 启用了 Teams 在启动时未知,故无条件预热;失败仅告警,首个请求会自行重试。"""
    try:
        await get_jwks(_http())
    except Exception:
        logger.warning("msteams: JWKS prewarm failed; will retry on first request", exc_info=True)


async def aclose() -> None:
    """关停时先排空在飞后台回复,再释放模块级 httpx/langgraph 客户端,避免连接池泄漏与 'Unclosed client session' 告警。"""
    global _http_client, _lg_client
    pending = list(_background_tasks)
    if pending:
        # 给在飞 _process 收尾时间,避免其撞上随后被关闭的 client 丢回复;超时则取消余下任务,继续关停。
        _, still_running = await asyncio.wait(pending, timeout=_SHUTDOWN_DRAIN_SECONDS)
        for task in still_running:
            task.cancel()
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception:
            logger.debug("msteams: http client aclose failed", exc_info=True)
        _http_client = None
    if _lg_client is not None:
        aclose_fn = getattr(_lg_client, "aclose", None)
        if callable(aclose_fn):
            try:
                await aclose_fn()
            except Exception:
                logger.debug("msteams: langgraph client aclose failed", exc_info=True)
        _lg_client = None
