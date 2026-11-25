"""SDK 异常体系。约定：所有异常继承 JidanpuError；任何异常都不得泄露 password / token（见 redact()）；
判定「什么算敏感字段」的唯一来源是 SENSITIVE_KEYS，脱敏（redact）与模型 repr 都用它。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = frozenset(
    {"password", "pwd", "newpwd", "renewpwd", "ypwd", "token", "x-token", "authorization", "secret"}
)
_MASK = "***"


def redact(data: Any) -> Any:
    """递归把敏感字段的值替换成 ***，用于安全地放进异常/日志。"""
    if isinstance(data, Mapping):
        return {
            key: (_MASK if str(key).lower() in SENSITIVE_KEYS else redact(value))
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact(item) for item in data]
    return data


class JidanpuError(Exception):
    """SDK 统一异常基类。"""


class ConfigError(JidanpuError):
    """配置缺失或非法。"""


class TransportError(JidanpuError):
    """连接、超时或重试耗尽。"""


class JidanpuTimeoutError(TransportError):
    """请求超时。"""


class AuthError(JidanpuError):
    """登录失败，或重登后仍无权限。"""

    def __init__(
        self, message: str, *, endpoint: str | None = None, upstream_message: str | None = None
    ) -> None:
        self.endpoint = endpoint
        self.upstream_message = upstream_message
        super().__init__(_describe(message, endpoint, None, None))


class SessionExpiredError(AuthError):
    """会话已失效：服务端 3xx 重定向到登录页，或业务壳提示未登录。
    与 `AuthError` 只差语义：调用方可据此「清掉本地会话并重登一次」（`AuthError`=登录本身失败）；
    不携带响应体——失效页可能回显提交过的明文。"""


class CaptchaRequiredError(AuthError):
    """服务端要求验证码。带 key 与 base64 图片，便于人工或 solver 处理。"""

    def __init__(
        self,
        message: str = "服务端要求验证码",
        *,
        key: str | None = None,
        image_b64: str | None = None,
    ) -> None:
        self.key = key
        self.image_b64 = image_b64
        hint = ""
        if key is not None:
            hint = " 若需自动处理，请为 JidanpuClient 配置 captcha_solver。"
        super().__init__(message + hint)


class RateLimitError(JidanpuError):
    """触发限流（HTTP 429）。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        retry_after: float | None = None,
        upstream_message: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.retry_after = retry_after
        self.upstream_message = upstream_message
        extra = f" retry_after={retry_after}" if retry_after is not None else ""
        super().__init__(_describe(message, endpoint, None, None) + extra)


class UpstreamBlockedError(JidanpuError):
    """对端 WAF/风控拦截（HTTP 468，或 2xx 却回了拦截页）。不是配额问题（那是 429/`RateLimitError`）、
    也不是凭据问题（401/403/`AuthError`，重登救不回来）：按来源 IP 拦，正确应对是换出口并隔离坏出口一段时间。
    拦截页正文可能带 HTML/JS 或回显提交过的内容 ⇒ 正文不进异常文案，只留状态码、endpoint 与 `retry_after`。"""

    def __init__(
        self,
        message: str = "请求被对端 WAF/风控拦截",
        *,
        endpoint: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.retry_after = retry_after
        extra = f" retry_after={retry_after}" if retry_after is not None else ""
        # body 一律传 None：正文可能带 HTML/JS/回显，绝不能进异常文本或日志。
        super().__init__(_describe(message, endpoint, status_code, None) + extra)


class ApiError(JidanpuError):
    """其他 4xx。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status_code: int | None = None,
        body: Any = None,
        code: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = redact(body)
        self.code = code
        super().__init__(_describe(message, endpoint, status_code, self.body))


class ServerError(JidanpuError):
    """5xx。"""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status_code: int | None = None,
        body: Any = None,
        code: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = redact(body)
        self.code = code
        super().__init__(_describe(message, endpoint, status_code, self.body))


def _describe(
    message: str,
    endpoint: str | None,
    status_code: int | None,
    body: Any,
) -> str:
    parts = [message]
    if endpoint:
        parts.append(f"endpoint={endpoint}")
    if status_code is not None:
        parts.append(f"status={status_code}")
    if body:
        parts.append(f"body={body}")
    return " | ".join(parts)
