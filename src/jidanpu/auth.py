"""认证。真实 API（spec §0.2）用 cookie 会话而非 `x-token`：登录是 `POST /Login/Login` 表单（`code` 传空串=无验证码），
成功判定是严格模式 `code == 1`（宽松模式会把 `{"code":2,"msg":"需要验证码"}` 判成成功），凭据由 `Transport` 的 cookie jar 持有。
旧的 `x-token` 实现（`PasswordAuth`）与验证码机制已删除。安全：密码只作表单字段 `pwd`，绝不写日志、不进 `repr`、不进异常消息。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from jidanpu.config import JidanpuConfig
from jidanpu.errors import (
    ApiError,
    AuthError,
    ConfigError,
    SessionExpiredError,
)
from jidanpu.transport import Transport

logger = logging.getLogger(__name__)

# ── cookie 会话（真实 API）──────────────────────────────────
LOGIN_PATH: Final[str] = "/Login/Login"
LOGIN_SUCCESS_CODE: Final[int] = 1
#: 承载会话的 cookie 名（服务端还会下发 jdpsysder/jdpsysnst/jdpsysndy）。
SESSION_COOKIE_NAME: Final[str] = "sl-session"
#: `get_token()` 返回的不透明标记；它**不是**凭据，凭据在 cookie jar 里。
SESSION_MARKER: Final[str] = "cookie-session"
#: 登录表单里与凭据无关的固定字段。`code` 传空串表示不使用验证码。
#: ⚠️ `deviceInfo` 必须长得像真实网页：网页登录 JS 这格是 `'访问设备：' + (isPc?'PC':'非PC') + '，' + JSON.stringify(layui.device())`。
#: 之前写 SDK 自编的 `"bot"` 等于自报「我是机器人」、被对端 WAF 判整台机器异常流量（现场见 docs/ERRORS.md）。
_LOGIN_FORM_CONSTANTS: Final[dict[str, str]] = {
    "code": "",
    "deviceInfo": (
        '访问设备：PC，'
        '{"os":"windows","ie":false,"weixin":false,"android":false,"ios":false}'
    ),
    "isRemember": "1",
}


class _Secret:
    """明文的不透明包装：`repr`/`str` 只输出掩码，遍历实例 `__dict__` 也漏不出 token。
    裸 `str` 会让 `repr(vars(auth))` 打出明文；这里用小载体达到 `repr=False` 的效果，
    `__slots__` 让 `vars()` 无法再往下走一层。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<_Secret ***>"

    def __str__(self) -> str:
        return "<_Secret ***>"


@dataclass(frozen=True, slots=True)
class CachedToken:
    """本地缓存的会话记录。`token` 是不透明会话标记（不是凭据，凭据在 `Transport` 的 cookie jar 里），
    `expires_at` 仍是「登录成功时间 + TTL」即本地愿意信任该会话的截止时刻。
    `token` 不进 `repr`：与 `JidanpuConfig.password` 同一约定，避免日志/异常里泄露凭据。"""

    token: str = field(repr=False)
    expires_at: float


class TokenStore(Protocol):
    def load(self) -> CachedToken | None: ...
    def save(self, token: CachedToken) -> None: ...
    def clear(self) -> None: ...


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._token: CachedToken | None = None

    def load(self) -> CachedToken | None:
        return self._token

    def save(self, token: CachedToken) -> None:
        self._token = token

    def clear(self) -> None:
        self._token = None


class FileTokenStore:
    """把会话记录落盘缓存。文件权限 0600。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> CachedToken | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            token = raw["token"]
            # 只认非空 str：与登录响应解析同样严格，否则 null/123 会被 str() 成 "None"/"123"
            if not isinstance(token, str) or not token:
                return None
            return CachedToken(token=token, expires_at=float(raw["expires_at"]))
        except Exception:
            return None

    def save(self, token: CachedToken) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"token": token.token, "expires_at": token.expires_at})
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # mode 只在 O_CREAT 建新文件时生效；已存在的旧文件可能仍是 0644，
            # 必须在写入前收紧 fd，否则 token 会先落进宽权限文件。
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


class Authenticator(Protocol):
    def get_token(self, *, force: bool = False) -> str: ...
    def invalidate(self) -> None: ...


class TokenAuth:
    """直接使用既有 token 的逃生舱。"""

    def __init__(self, token: str) -> None:
        if not token:
            raise AuthError("token 不能为空")
        self._token = _Secret(token)

    def get_token(self, *, force: bool = False) -> str:
        return self._token.reveal()

    def invalidate(self) -> None:
        raise AuthError("TokenAuth 不支持失效重登，请改用 SessionAuth")


class SessionAuth:
    """cookie 会话认证：`POST /Login/Login`（表单，`code=""` 无验证码、`deviceInfo` 与真实网页同形）建立会话，成功判定是业务码 `code == 1`。
    没有 token 头——会话是 `Transport` cookie jar 里的 `sl-session`；`get_token()` 只返回不透明标记（`SESSION_MARKER`），认证靠 jar 不靠返回值。
    并发=双重检查锁只登录一次；缓存=只有 jar 里确实还持有会话 cookie 时才采纳，否则缓存是关于凭据的一句谎话。"""

    def __init__(
        self,
        config: JidanpuConfig,
        transport: Transport,
        *,
        store: TokenStore | None = None,
    ) -> None:
        _require_matching_password(config, transport)
        self._config = config
        self._transport = transport
        self._store = store
        self._marker: _Secret | None = None
        self._lock = threading.Lock()

    def get_token(self, *, force: bool = False) -> str:
        """确保会话已建立，返回不透明标记。`force=True` 无视缓存重新登录。
        真正的凭据（cookie）由 `Transport` 的 cookie jar 持有，返回值不能拿去认证，只表示「已登录」。"""
        if not force:
            if self._marker is not None:
                return self._marker.reveal()
            cached = self._load_cached()
            if cached is not None:
                self._marker = _Secret(cached)
                return cached

        with self._lock:
            # 双重检查：可能在等锁期间已有别的线程登录成功
            if not force and self._marker is not None:
                return self._marker.reveal()
            marker = self._login_once()
            self._marker = _Secret(marker)
            self._persist(marker)
            return marker

    def invalidate(self) -> None:
        """丢弃本地会话记录并清掉 jar 里的会话 cookie；下次 `get_token()` 重新登录。
        必须连 cookie 一起清：否则 `_has_session_cookie()` 仍为真，新登录的「jar 里必须有会话 cookie」判定会被残留 cookie 假通过。"""
        with self._lock:
            self._marker = None
            if self._store is not None:
                self._store.clear()
            self._transport.clear_cookie(SESSION_COOKIE_NAME)

    def login(self) -> str:
        """强制重新登录（`get_token(force=True)` 的别名）。"""
        return self.get_token(force=True)

    def _load_cached(self) -> str | None:
        if self._store is None:
            return None
        cached = self._store.load()
        if cached is None:
            return None
        if cached.expires_at <= time.monotonic():
            self._store.clear()
            return None
        if not _has_session_cookie(self._transport):
            self._store.clear()
            return None
        return cached.token

    def _persist(self, marker: str) -> None:
        if self._store is None:
            return
        ttl = self._config.token_ttl.total_seconds()
        self._store.save(CachedToken(token=marker, expires_at=time.monotonic() + ttl))

    def _login_once(self) -> str:
        # 正面证据的前提：先清掉 jar 里可能残留的旧会话 cookie，这样「必须有会话 cookie」判定
        # 只可能被本次登录响应重新下发的 cookie 满足。选「先清」而非「快照旧值+要求值变化」：
        # 真实服务端可能对同一次登录重新下发同一个 sl-session 值，快照方案会把合法登录误判为失败。
        self._transport.clear_cookie(SESSION_COOKIE_NAME)
        # 密码只进 data，不进日志/异常；`inject_token=False`：登录请求自身不参与
        # 会话重登，也不会回调 token_provider（client.py 已不再接线该 provider）。
        payload: dict[str, str] = {
            "acc": self._config.account,
            "pwd": self._config.password,
            **_LOGIN_FORM_CONSTANTS,
        }
        try:
            body = self._transport.request(
                "POST",
                LOGIN_PATH,
                data=payload,
                inject_token=False,
                # 登录的成功码是 1，且必须走严格模式：宽松模式对 `code > 0` 判成功，
                # 会把 `{"code":2,…,"data":"N"}` 这类被拒应答当成登录成功。
                expect_code=LOGIN_SUCCESS_CODE,
            )
        except SessionExpiredError as exc:
            # 登录 POST 被 302 到登录页 = 本次没有建立会话；对调用方就是登录失败。
            raise AuthError(f"登录失败：{exc}") from exc
        except ApiError as exc:
            # transport 已按「本次实际发出的明文」脱敏服务端回显，异常里不会带出密码
            raise AuthError(f"登录失败：{exc}") from exc

        # transport 在 auto 模式下已按 `expect_code=1` 判过成败；这里再判一次是为了
        # `unwrap="status"`（transport 不解包、不判业务码）时仍严格比对 `code == 1`。
        if isinstance(body, dict) and "code" in body:
            code = body.get("code")
            if code != LOGIN_SUCCESS_CODE:
                raise AuthError(f"登录失败：服务端返回 code={code!r}")

        # 正面证据：非业务壳的 2xx（HTML 登录页、空响应、{}、未跟随的 302……）会绕过上面的
        # 判定，所以最终以「jar 里真的拿到了会话 cookie」为准 —— 没有会话就没有登录成功。
        # 进入这里之前 cookie 已被清空，因此只有**本次**响应重新下发才能满足该判定。
        if not _has_session_cookie(self._transport):
            raise AuthError(f"登录失败：响应未建立会话（缺少 {SESSION_COOKIE_NAME} cookie）")
        return SESSION_MARKER


def _require_matching_password(config: JidanpuConfig, transport: Transport) -> None:
    """认证层要发的密码必须正好是传输层会脱敏的那一份：`Transport` 只按自己 config 的密码脱敏，
    若用另一份 config 的密码登录（`SessionAuth(cfgA, Transport(cfgB))` 可复现），服务端把收到的密码回显在
    `msg`/错误页时异常文本会带出明文 ⇒ 装配期直接拒绝（错误消息不含密码）。`password` 已保证非空，不遮蔽=两份 config 不一致。"""
    if not transport.masks(config.password):
        raise ConfigError(
            "SessionAuth 与 Transport 的密码不一致："
            "登录会发送认证层的密码，而传输层按自己的密码脱敏，"
            "两者不一致会让服务端回显的密码以明文进入异常。请用同一份配置构造二者。"
        )


def _has_session_cookie(transport: Transport) -> bool:
    """`Transport` 的 cookie jar 里是否持有**非空**的会话 cookie。"""
    return transport.has_cookie(SESSION_COOKIE_NAME)
