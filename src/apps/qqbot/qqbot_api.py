"""QQ 机器人开放 API 的同步客户端（httpx）。成败判据（E-0087 纪律，本项目踩过的坑）：QQ 业务错误走响应体 `code`、HTTP 仍是 200 ⇒ 判成败只认——① HTTP 非 2xx ⇒ 失败（code=`HTTP<状态码>`）；
② 响应体 `code` 存在且 != 0 ⇒ 失败（code 给那个业务码）；③ `code` 缺失时按该端点自己的关键字段判（发群消息看 `id`、网关看 `url`、token 看 `access_token`），缺关键字段 ⇒ 失败。⛔「没抛异常」永远不构成成功判据；每个函数都返回 `(ok: bool, data, code, message)`，调用方必须看 `ok`。
脱敏：secret/token 永不进日志与异常文案，所有错误路径先过 `_redact`（照 `src/jidanpu/transport.py` `_scrub_text` 同款：候选明文按子串匹配逐一替换成占位、过短（<6 字符）候选不参与——太短的替换只会误伤正常文案）；判据钉在 `tests/test_qqbot_client.py`。路径核实状态（E-0005/E-0007 纪律，逐条标注）：实测/任务书给定的（`/app/getAppAccessToken`、`/gateway`、`POST /v2/groups/{group_openid}/messages`、`/approval_join_request/{member_openid}`）可直接用；注释标「推断」的那几条接线前必须实测复核，⛔ 复核前不得把返回当事实用。"""
from __future__ import annotations

import logging
import threading
import time

import httpx

#: 单次请求超时（秒）。照 `apps/business/jidanpu_openapi.DEFAULT_TIMEOUT` 同款口径：
#: 本客户端的调用会进常驻循环，别让「上游卡住」把一轮拖穿。
DEFAULT_TIMEOUT = 15.0

API_BASE = "https://api.bot.qq.com"

#: token 提前续期余量（秒）：到期前这么久就换新；官方口径是旧 token 在窗口内仍有效。
#: ⚠️ 我们自定（对齐 jidanpu_openapi 的 TOKEN_EXPIRY_SKEW_SECONDS）。
TOKEN_EXPIRY_SKEW_SECONDS = 60.0

#: 脱敏占位（照 jidanpu_openapi.REDACTED：不许用空串，「抹掉了」≠「本来就没有」）。
REDACTED = "***"

logger = logging.getLogger(__name__)


def _usable(secrets) -> tuple[str, ...]:
    """非空、长度达标（≥6）的明文集合——太短的「密钥」做替换只会误伤正常文案。"""
    return tuple(s for s in secrets if isinstance(s, str) and len(s) >= 6)


def _redact(value: str, secrets) -> str:
    """把 `value` 里出现的任一候选明文**子串**替换成 `***`（评审 P3-2 改准：是**子串
    匹配**、逐一 `replace`，与 jidanpu `_scrub_text` 同款——不是「值相等才替换」）。
    过短（< 6 字符）的候选不参与（`_usable`，理由见模块 docstring）。"""
    for secret in _usable(secrets):
        if secret and secret in value:
            value = value.replace(secret, REDACTED)
    return value


class TokenManager:
    """一个机器人的 access_token 缓存（线程安全；token 永不进日志）。`get_token()` 过期前 `TOKEN_EXPIRY_SKEW_SECONDS` 就换新
    （官方：旧 token 在窗口内仍有效 ⇒ 平滑切换）；取 token 失败重试一次（`/app/getAppAccessToken` 本身不需要 Authorization）；持 `threading.Lock`（reader/sender 两线程共用一个实例）。"""

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        clock=time.monotonic,
        transport: httpx.BaseTransport | None = None,
    ):
        self.app_id = app_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._clock = clock
        self._transport = transport
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def transport(self) -> httpx.BaseTransport | None:
        """测试注入的传输层（评审 P3-7：公开只读访问器——调用方 ⛔ 别再直取 `_transport`）。"""
        return self._transport

    def get_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token and now < self._expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
                return self._token
            last_error = ""
            for _attempt in range(2):  # 失败重试一次
                ok, data, _code, message = self._fetch()
                if ok:
                    self._token = data["access_token"]
                    self._expires_at = self._clock() + float(data.get("expires_in") or 0)
                    return self._token
                last_error = message
            # ⛔ 异常文案里不得出现 secret：`message` 由 _fetch 的脱敏路径产出。
            raise TokenError(f"换取 access_token 失败：{last_error}")

    def _fetch(self):
        body = {"appId": self.app_id, "clientSecret": self.client_secret}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                response = client.post(f"{API_BASE}/app/getAppAccessToken", json=body)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning(
                "qqbot 取 token 网络失败：%s", type(exc).__name__
            )
            return False, None, "NETWORK", _redact(
                f"{type(exc).__name__}", _usable((self.client_secret,))
            )
        return _judge(
            response,
            ok_key="access_token",
            secrets=_usable((self.client_secret,)),
        )


class TokenError(Exception):
    """取不到 access_token（凭据被拒 / 上游不可达）。"""


def _judge(
    response: httpx.Response, *, ok_key: str | None, secrets: tuple[str, ...]
) -> tuple[bool, dict | None, str, str]:
    """按模块 docstring 的三条判据给一个响应定性。
    `ok_key`：`code` 缺失时判成功的该端点关键字段（`id`/`url`/`access_token`…）。"""
    if response.status_code < 200 or response.status_code >= 300:
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("msg") or "")
        except ValueError:
            message = (response.text or "")[:200]
        return False, None, f"HTTP{response.status_code}", _redact(message, secrets)
    try:
        body = response.json()
    except ValueError:
        return False, None, "BAD_JSON", "响应不是 JSON"
    if not isinstance(body, dict):
        return False, None, "BAD_JSON", "响应不是 JSON 对象"
    message = str(body.get("message") or body.get("msg") or "")
    if "code" in body:
        # ⭐ E-0087：HTTP 200 + code≠0 **必须判失败**（QQ 业务错误不走 HTTP 状态码）。
        code = str(body.get("code"))
        if code != "0":
            return False, body, code, _redact(message, secrets)
        # code==0 的信封把业务数据放 `data`（没有就整个 body 当 data）。
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        return True, data, "0", message
    # 无统一 code 字段的端点：按关键字段判（发群消息的 `{id, timestamp}` 形状）。
    if ok_key is not None and ok_key not in body:
        return (
            False,
            body,
            "MISSING_KEY",
            _redact(f"响应缺关键字段 {ok_key}", secrets),
        )
    return True, body, "0", message


def _request(
    token_manager: TokenManager,
    method: str,
    path: str,
    *,
    ok_key: str | None = None,
    json_body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, dict | None, str, str]:
    """带 Authorization 的一次 API 调用（脱敏后的 `(ok, data, code, message)`）。"""
    try:
        token = token_manager.get_token()
    except TokenError as exc:
        logger.warning("qqbot api %s %s 取 token 失败：%s", method, path, exc)
        return False, None, "TOKEN", str(exc)
    try:
        with httpx.Client(
            timeout=timeout,
            transport=transport if transport is not None else token_manager.transport,
        ) as client:
            response = client.request(
                method,
                f"{API_BASE}{path}",
                json=json_body,
                headers={"Authorization": f"QQBot {token}"},
            )
    except (httpx.HTTPError, OSError) as exc:
        logger.warning(
            "qqbot api %s %s 网络失败：%s", method, path, type(exc).__name__
        )
        return False, None, "NETWORK", type(exc).__name__
    result = _judge(
        response,
        ok_key=ok_key,
        secrets=_usable((token_manager.client_secret, token)),
    )
    if not result[0]:
        # `message` 已过 _redact（_judge 收了 secrets）⇒ 这条日志不含 secret/token。
        logger.warning(
            "qqbot api %s %s 失败 code=%s message=%s", method, path, result[2], result[3]
        )
    return result


# ── 端点（路径核实状态见模块 docstring）────────────────────────────────────


def fetch_gateway(token_manager, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """`GET /gateway` → `{"url": "wss://…"}`（WS 网关地址，带认证）。"""
    return _request(
        token_manager, "GET", "/gateway", ok_key="url", timeout=timeout, transport=transport
    )


def send_group_message(
    token_manager,
    group_openid: str,
    *,
    msg_type: int,
    content: str | None = None,
    markdown: str | None = None,
    msg_id: str | None = None,
    msg_seq: int | None = None,
    timeout=DEFAULT_TIMEOUT,
    transport=None,
):
    """`POST /v2/groups/{group_openid}/messages`。`msg_type`：`0`=文本（`content`）、`2`=markdown（`markdown.content`）；
    被动回复必须带 `msg_id`（收到的消息 id）、主动消息不带；成功响应 `{"id","timestamp"}` ⇒ `ok_key="id"`。"""
    body: dict = {"msg_type": msg_type}
    if msg_type == 2:
        body["markdown"] = {"content": markdown or ""}
    else:
        body["content"] = content or ""
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq or 1
    return _request(
        token_manager,
        "POST",
        f"/v2/groups/{group_openid}/messages",
        ok_key="id",
        json_body=body,
        timeout=timeout,
        transport=transport,
    )


def approve_join_request(
    token_manager,
    group_openid: str,
    member_openid: str,
    *,
    op: str,
    join_request_id: str | None = None,
    reject_reason: str | None = None,
    timeout=DEFAULT_TIMEOUT,
    transport=None,
):
    """`POST /v2/groups/{group_openid}/approval_join_request/{member_openid}`。
    `op`：`approve` | `decline`；拒绝时带 `reject_reason`。"""
    body: dict = {"op": op}
    if join_request_id:
        body["join_request_id"] = join_request_id
    if reject_reason:
        body["reject_reason"] = reject_reason
    return _request(
        token_manager,
        "POST",
        f"/v2/groups/{group_openid}/approval_join_request/{member_openid}",
        ok_key=None,
        json_body=body,
        timeout=timeout,
        transport=transport,
    )


def get_bot_state(token_manager, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """机器人自身信息。⚠️ 路径**推断**（`/users/@me`，频道 API 同形）——接线前实测复核。"""
    return _request(
        token_manager, "GET", "/users/@me", ok_key="id", timeout=timeout, transport=transport
    )


def get_group_info(token_manager, group_openid, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """群资料。⚠️ 路径**推断**——接线前实测复核（拿不到就用事件投影回填）。"""
    return _request(
        token_manager,
        "GET",
        f"/v2/groups/{group_openid}/info",
        ok_key=None,
        timeout=timeout,
        transport=transport,
    )


def get_mutes(token_manager, group_openid, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """群禁言名单。⚠️ 路径**推断**——接线前实测复核。"""
    return _request(
        token_manager,
        "GET",
        f"/v2/groups/{group_openid}/mutes",
        ok_key=None,
        timeout=timeout,
        transport=transport,
    )


def get_join_requests(token_manager, group_openid, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """入群申请列表。⚠️ 路径**推断**——本批事件流的镜像查询，排障页要用，接线前实测复核。"""
    return _request(
        token_manager,
        "GET",
        f"/v2/groups/{group_openid}/join_requests",
        ok_key=None,
        timeout=timeout,
        transport=transport,
    )


def get_menu(token_manager, group_openid, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """群菜单读取。⚠️ 路径**推断**——菜单管理接线前实测复核。"""
    return _request(
        token_manager,
        "GET",
        f"/v2/groups/{group_openid}/menu",
        ok_key=None,
        timeout=timeout,
        transport=transport,
    )


def put_menu(token_manager, group_openid, menu: dict, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """群菜单写入。⚠️ 路径**推断**——同上，接线前实测复核。"""
    return _request(
        token_manager,
        "PUT",
        f"/v2/groups/{group_openid}/menu",
        ok_key=None,
        json_body=menu,
        timeout=timeout,
        transport=transport,
    )


def generate_url_link(token_manager, group_openid, *, timeout=DEFAULT_TIMEOUT, transport=None):
    """生成群的 URL 链接。⚠️ 路径**推断**——接线前实测复核。"""
    return _request(
        token_manager,
        "POST",
        f"/v2/groups/{group_openid}/url_link",
        ok_key="url",
        timeout=timeout,
        transport=transport,
    )
