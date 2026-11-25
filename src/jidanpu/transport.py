"""HTTP 传输层：统一请求头、重试、业务壳解包。真实 API 状态码恒 200、成败在业务壳 `{code,msg,count,data,total}`；解包规则见 `unwrap_response`（3xx=会话失效；468 / 2xx+SafeLine 自报=对端 WAF 拦截 `UpstreamBlockedError` 换出口且不读正文；≥400 按状态码报错；2xx 非 JSON=失败（自报拦截页除外）；业务壳判定=严格模式 `code==expect_code` > 壳 `success` 字段（`is True` 才成功、与 `success_rule` 无关）> `success_rule`（`lenient` 读接口宽松启发式 / `positive_code` 写接口 `code>0`））。
⛔ 不拿 `<html` 兜底：本仓实测 2xx 回 HTML 登录页是会话问题不是封禁，判成「换出口」会让上层做错事。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from jidanpu.config import JidanpuConfig
from jidanpu.errors import (
    ApiError,
    AuthError,
    ConfigError,
    JidanpuTimeoutError,
    RateLimitError,
    ServerError,
    SessionExpiredError,
    TransportError,
    UpstreamBlockedError,
)

logger = logging.getLogger(__name__)

#: 除 `data` 外能让 `code == 0` 被认成成功的载荷键。「有载荷」判据=**键在**不看值（空 `data`/`count`/`total` 也算）——读接口成功但结果为空回 `{"code":0,"msg":"success","data":[]}`（真实租户只读回归实测）。
#: 写接口的保护不在这里（`client.call()` 按 `risk=="write"` 取 `positive_code` 语义）；
#: 本启发式只在壳里**没有** `success` 字段时才轮到（`success` 字段优先于任何码规则）。
_PAYLOAD_KEYS = ("data", "count", "total")

#: 业务壳里表示「会话没了」的消息片段（命中即 `SessionExpiredError`）。
_SESSION_EXPIRED_HINTS = ("请先登录", "未登录", "登录失效", "请重新登录")

#: 对端 WAF 拦截页的识别片段（小写）：实测 SafeLine 会在页里自报名字。
#: ⚠️ **不拿 `<html` 兜底**（2xx 的 HTML 也可能只是登录页 ⇒ 会话问题，不是封禁）；
#: ⚠️ 只在**原本就判成非 JSON**的那条路径上看它。
_WAF_PAGE_HINTS = ("safeline",)

#: `response.json()` 失败的哨兵：与「合法 JSON 的 null」区分开。
_NON_JSON = object()

#: 业务壳里「没有 `success` 字段」的哨兵：与「`success: null`」区分开。
_MISSING = object()

#: 端点可声明的成功语义。`lenient` = 默认的宽松启发式（为读接口设计）；
#: `positive_code` = 写接口口径：`code` 是数字且 `> 0` 才算成功。
SUCCESS_RULES = ("lenient", "positive_code")

# 429/重试退避的睡眠上限（秒）：服务端可以要求等待，但不该让同步调用方阻塞一整天。
MAX_BACKOFF_SECONDS = 30.0

# ── ⭐ 重试按「请求是否已发出」分流 ─────────────────
# 旧行为对所有 HTTPError 无差别重试 ⇒ 非幂等写（下单/改价/改状态）在「请求已到对方、响应没回来」时被重发=同一张单下两次。
# 分流判据=异常语义：「请求确定没出门」的重试对任何端点安全；「可能已出门」的只在调用方声明 `retry_on_sent=True`（读端点）时重试。

#: 「请求**可能已完整发出**」的超时类：读超时 = 请求已发、等回包超时；写超时 =
#: 请求体可能已部分 / 全部送达。⛔ `ConnectTimeout` / `PoolTimeout` **不在此列**：
#: 连接都没建立（或没排到连接），请求没出门。
_WAS_SENT_TIMEOUTS = (httpx.ReadTimeout, httpx.WriteTimeout)
#: 「请求**可能已完整发出**」的其它传输错误：对端在读请求途中断开 / 响应途中断开 /
#: 协议层断裂。⛔ `ConnectError` / `ProxyError` / `UnsupportedProtocol` /
#: `LocalProtocolError` 不在此列（请求没出门 / 没发就坏）。
_WAS_SENT_ERRORS = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.CloseError,
    # ⭐ `DecodingError` 只在读响应体/解码阶段抛 ⇒ 请求一定已完整送达对端。
    # 漏掉它=写端点把「响应解不开」误判成「没出门」而**重发**一次下单/改价；
    # 它属 HTTPError 子类，落这个集合才会被 `except httpx.HTTPError` 按「可能已送达」处置。
    httpx.DecodingError,
)


def unwrap_response(
    response: httpx.Response,
    *,
    expect_code: int | None = None,
    success_rule: str = "lenient",
    unwrap: str = "auto",
    secrets: Sequence[str] = (),
) -> Any:
    """按模块 docstring 的确定性规则把响应转成返回值或抛异常（失败分类：468 / 2xx+SafeLine ⇒ `UpstreamBlockedError` 换出口且文案不含正文；401/403 ⇒ `AuthError`；429 ⇒ `RateLimitError`；≥500 ⇒ `ServerError`）。
    成功判定优先级见 `_envelope_succeeded`：严格模式（`expect_code` 给定，登录传 `LOGIN_SUCCESS_CODE`；业务接口⛔ 不得误用——宽松模式下 `{"code":2,"msg":"需要验证码","data":"N"}` 是成功）> 壳 `success` 字段 > `success_rule`（调用方按「显式入参→清单声明→`risk` 默认」解析好传入，传输层不自己按 `risk` 猜；取值不在 `SUCCESS_RULES` ⇒ `ConfigError` fail-closed）。
    `unwrap` 三模式（`auto` 完整规则 / `status` 只做 1-3 步供调试 / `envelope`）；成功返回脱敏壳的 `data` 键、无 `data` 键返回整个壳（载荷在 `msg` 里的接口由调用方自取）；`secrets`=异常文本里不许出现的明文，**判定看未脱敏 `raw`、渲染看脱敏后 `body`**（否则裸数字密码被脱敏成 `"***"` 后业务失败被静默当成功）。"""
    if success_rule not in SUCCESS_RULES:
        raise ConfigError(f"success_rule 必须是 {SUCCESS_RULES} 之一，当前为 {success_rule!r}")

    status = response.status_code
    request = response.request
    endpoint = f"{request.method} {_scrub_text(request.url.path, secrets)}" if request else None

    if 300 <= status < 400:
        # follow_redirects=False：302 到登录页就是会话失效信号，页面本身不可信、不引用。
        raise SessionExpiredError("登录状态已失效，请重新登录", endpoint=endpoint)

    if status == 468:
        # 对端 WAF/风控拦截（实测形态：468 + SafeLine 拦截页，按来源 IP 全站封禁）。
        # ⚠️ 刻意**不读正文**：拦截页可能带 HTML/JS、也可能回显提交过的内容。
        # 上层据此**换出口**，而⛔ 不是重登或当普通 4xx 重试。
        raise UpstreamBlockedError(
            "请求被上家 WAF/风控拦截",
            endpoint=endpoint,
            status_code=status,
            retry_after=_retry_after(response),
        )

    parsed = _parse_json(response)
    raw = parsed if parsed is not _NON_JSON else _text_of(response)
    body = _scrub(raw, secrets)

    if status >= 400:
        upstream_message = _scrub(_message_of(body), secrets)
        message = upstream_message or f"HTTP {status}"
        if status in (401, 403):
            raise AuthError(f"认证失败：{message}", endpoint=endpoint, upstream_message=upstream_message)
        if status == 429:
            raise RateLimitError(
                f"触发限流：{message}",
                endpoint=endpoint,
                retry_after=_retry_after(response),
                upstream_message=upstream_message,
            )
        if status >= 500:
            raise ServerError(
                f"服务端错误：{message}", endpoint=endpoint, status_code=status, body=body
            )
        raise ApiError(
            f"请求失败：{message}", endpoint=endpoint, status_code=status, body=body
        )

    if parsed is _NON_JSON:
        # 2xx 却回了一张拦截页（WAF 有时不用 468）：按封禁处理，别当普通非 JSON 故障。
        if _is_waf_page(body):
            raise UpstreamBlockedError(
                "上家返回了拦截页（WAF/风控）",
                endpoint=endpoint,
                status_code=status,
                retry_after=_retry_after(response),
            )
        raise ApiError(
            "服务端返回了非 JSON 响应", endpoint=endpoint, status_code=status, body=body
        )

    if unwrap == "status":
        return body

    if not (isinstance(raw, dict) and "code" in raw):
        raise ApiError(
            "响应缺少业务码", endpoint=endpoint, status_code=status, body=body
        )

    message = _message_of(body)
    session_expired = message is not None and any(
        hint in message for hint in _SESSION_EXPIRED_HINTS
    )
    login_rejection = message if session_expired and type(raw["code"]) is int and raw["code"] == 0 else None
    # 宽松模式「会话优先」：`code > 0` 本会把壳判成功，但会话没了的壳（实测
    # `{"code":0,"msg":"请先登录"}` 之外还可能有 code>0 的变体）必须先抛失效，
    # 否则上层会拿着一个失效会话继续重试原请求。
    if expect_code is None and session_expired:
        raise SessionExpiredError(
            f"登录状态已失效：{message}", endpoint=endpoint, upstream_message=login_rejection,
        )

    if not _envelope_succeeded(
        raw, expect_code=expect_code, success_rule=success_rule
    ):
        if session_expired:
            # 严格模式的失败路径同样要认出会话失效，不能当成普通业务失败重试。
            raise SessionExpiredError(
                f"登录状态已失效：{message}", endpoint=endpoint, upstream_message=login_rejection,
            )
        # 判定用原始 code；异常上存脱敏后的形态，别在 `code=` 上重开一条明文路径。
        scrubbed_code = _scrub(raw.get("code"), secrets)
        raise ApiError(
            f"业务失败：{message or scrubbed_code}",
            endpoint=endpoint,
            status_code=status,
            body=body,
            code=scrubbed_code,
        )
    return body.get("data", body)


def _envelope_succeeded(
    envelope: Mapping[str, Any],
    *,
    expect_code: int | None,
    success_rule: str = "lenient",
) -> bool:
    """业务壳的确定性成功判定（优先级从高到低）：① 严格模式成功 ⟺ `code == expect_code`（bool 显式排除——`True == 1` 会把畸形响应当成功码 fail-open）；② 壳里有 `success` 字段由它决定、**与 `success_rule` 无关**（`is True` 才成功；`false`/`null` 一律失败——`_MISSING` 哨兵区分缺席与 `null`，fail-closed）；
    ③ 缺席时才看 `success_rule`：`positive_code`（写接口）成功 ⟺ `code` 非 bool 数字且 `> 0`（`code == 0` 一律失败不看 msg/载荷——实测写失败壳带空 `data`，E-0100）；`lenient`（读接口）`code > 0` 成功、`< 0` 失败、`== 0` ⟺ msg 空或缺 **或** `data`/`count`/`total` 键在（空值也算；非数字 fail-closed；「空」只认 `None`/缺失与 `""`，非 str 的非空 msg 也算非空）。
    ⛔ `code == 0` 的附加条件保住实测限流信号 `{"code":0,"msg":"限流中"}` 仍失败，**不能**简化成 `code >= 0`；会话失效不在这里判（`unwrap_response` 的「会话优先」）；`success` 检查必须排在 `success_rule` 派发**之前**（否则写接口会把 `{"code":200,"success":false}` 按 `code>0` 翻成成功=静默成功形态，且写端点 `observed_envelope` 全为空、这条路径无实测兜底）。"""
    code = envelope.get("code")

    if expect_code is not None:
        # 严格模式：`bool` 同样不算数字（`True == 1` 会把畸形响应当成成功码）。
        return (
            not isinstance(code, bool)
            and isinstance(code, (int, float))
            and code == expect_code
        )

    # `success` 字段存在时由它决定、与 `success_rule` 无关：必须排在 `success_rule` 派发**之前**，
    # 否则写接口（`positive_code`）会把显式失败的壳 `{"code":200,"success":false}` 按 `code>0` 翻成成功（本线要消灭的静默成功形态）。
    # 写端点全是 `positive_code`（229 个）且 `observed_envelope` 全为空 ⇒ 这条路径无实测覆盖兜底；`_MISSING` 区分缺席与 `null`（`null` 同 `false` 判失败，fail-closed）。
    success = envelope.get("success", _MISSING)
    if success is not _MISSING:
        return success is True

    if success_rule == "positive_code":
        # 写接口口径：`code` 是**业务值**（新建成功时就是新记录 id），所以只认「数字且 > 0」。
        # `code == 0` 一律失败——**不看 `msg`、不看载荷**：实测写失败壳带空的 `data`，
        # 而「空 data 算不算载荷」那条启发式本来就答不了这个形状（E-0100）。
        return (
            not isinstance(code, bool)
            and isinstance(code, (int, float))
            and code > 0
        )

    if isinstance(code, bool) or not isinstance(code, (int, float)):
        return False
    if code > 0:
        return True
    if code < 0:
        return False
    message = envelope.get("msg")
    # 「空消息」只认 `None`/缺失与 `""`；**任何**其它值（含 dict/list 等非 str）都算非空，
    # 无载荷键时判失败。旧实现只在 `msg` 是 `str` 时才走载荷判定，
    # `{"code":0,"msg":{"error":"参数错误"}}` 会被 fail-open 判成功。
    if message is not None and message != "":
        # 「有载荷」判据=**键在**不看值：空 `data`/`count`/`total` 也算（读接口成功但结果为空回
        # `{"code":0,"msg":"success","data":[]}`；收紧成「值非空」会把 4 个这样的成功读接口判失败——实测）。
        # 写接口的保护不在这里（`client.call()` 按 `risk=="write"` 取 `positive_code`），传输层只认传进来的规则。
        return any(key in envelope for key in _PAYLOAD_KEYS)
    return True


def _scrub(value: Any, secrets: Sequence[str]) -> Any:
    """明文替换成 `***`，深度遍历 dict 键值与 list/tuple。是 `errors.redact` 的补充（redact 只按键名，管不住"密码回显在 msg/键里/非 JSON 错误页"）。
    非字符串叶子只在 `str(value)` **相等**时整块替换（用包含会让 `secrets=("1",)` 把业务码 `511` 改成 `"***"`，非数字一律判成功 ⇒ 业务失败被静默当成功）；`bool`/`None` 不是密文、原样保留。"""
    usable = _usable(secrets)
    if not usable:
        return value
    if isinstance(value, str):
        return _scrub_text(value, usable)
    if isinstance(value, Mapping):
        return {
            (_scrub_text(key, usable) if isinstance(key, str) else key): _scrub(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, secrets) for item in value]
    if value is None or isinstance(value, bool):
        return value
    return "***" if str(value) in usable else value


def _usable(secrets: Sequence[str] | None) -> list[str]:
    """可用的明文清单（过滤空值）。"""
    return [s for s in secrets or () if s]


def _scrub_text(value: str, secrets: Sequence[str] | None) -> str:
    """把文本里的明文逐一替换成 `***`（明文按子串匹配）。"""
    for secret in _usable(secrets):
        value = value.replace(secret, "***")
    return value


def _sent_secrets(password: str, token: str | None) -> tuple[str, ...]:
    """本次请求**实际发出**的明文：配置密码 + 当时注入的 token。
    token 必须取**发送时**的值——响应到达后再调 `token_provider()` 可能已轮换，真正发出的那个会被漏掉。"""
    return (password, token) if token else (password,)


def _jar_secrets(cookies: httpx.Cookies) -> tuple[str, ...]:
    """cookie jar 里当前所有 cookie 的**值**（空值除外）。认证完全靠 jar（spec §0.2 无 `x-token`）⇒ 任何 cookie 都可能是会话凭据。
    按**值**全量入列、不按 cookie 名挑：真实 API 的几个会话 cookie 都是凭据，将来新增的也必须自动被遮蔽 ⛔ 不漏一个。"""
    return tuple(value for _, value in cookies.items() if value)


def _parse_json(response: httpx.Response) -> Any:
    """解析 JSON；失败返回哨兵 `_NON_JSON`（与「合法 JSON 的 null」区分开）。"""
    try:
        return response.json()
    except Exception:
        return _NON_JSON


def _text_of(response: httpx.Response) -> Any:
    text = response.text
    return text if text else None


def _is_waf_page(body: Any) -> bool:
    """2xx 的**非 JSON** 响应体是不是对端自报的 WAF 拦截页（含 `SafeLine`）。
    ⚠️ 调用点只有「原本就判成非 JSON」那条路径：正常 JSON 解析走不到这里 ⇒ 这条判据不改变任何合法业务壳的成败。"""
    if not isinstance(body, str):
        return False
    lowered = body.lower()
    return any(hint in lowered for hint in _WAF_PAGE_HINTS)


def _envelope_of(response: httpx.Response, secrets: Sequence[str]) -> dict[str, Any] | None:
    """响应里业务壳（含 `code` 的 JSON 对象）的**脱敏**副本；不是业务壳 ⇒ `None`。
    列表调用方从 `Transport.last_envelope` 读 `count`/`total` 而成功时 `unwrap_response` 只返回 `data`；存**脱敏**副本（公开属性，响应回显的明文不能借它泄露）。"""
    raw = _parse_json(response)
    if not isinstance(raw, dict) or "code" not in raw:
        return None
    scrubbed = _scrub(raw, secrets)
    return scrubbed if isinstance(scrubbed, dict) else None


def _message_of(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("msg", "message", "error", "errorMsg", "errmsg"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Transport:
    """带 token 注入与重试的同步 HTTP 客户端。"""

    def __init__(
        self,
        config: JidanpuConfig,
        *,
        token_provider: Callable[[], str | None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=config.timeout,
            follow_redirects=False,
            headers={"User-Agent": config.user_agent},
        )
        self.on_unauthorized: Callable[[], None] | None = None
        #: 最近一次响应里的完整业务壳（脱敏后），供列表调用方读 `count`/`total`。
        #: 每次请求覆盖；不是业务壳（或没有响应）时为 `None`。
        self.last_envelope: dict[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        auth_retry: bool = True,
        inject_token: bool = True,
        expect_code: int | None = None,
        success_rule: str = "lenient",
        referer: str | None = None,
        retry_on_sent: bool = True,
        retry_rate_limit: bool = True,
    ) -> Any:
        """发一次请求并解包。`expect_code` 透传 `unwrap_response`（`None`=宽松模式业务接口用；登录传 `LOGIN_SUCCESS_CODE`=1 严格模式）；`success_rule` 同样透传（写接口清单声明 `positive_code`）；`referer` 非空注入 `Referer`、`X-Requested-With` 始终注入。
        ⭐ `retry_on_sent`：**请求已发出**型错误（读/写超时、读/写中断、协议断裂，见 `_WAS_SENT_TIMEOUTS`/`_WAS_SENT_ERRORS`）要不要重试——`True` 默认兼容读路径、`False` 首次失败立刻抛 ⛔ 不重发；**连接期**错误不受它管（请求确定没出门，重试对任何端点安全）。判据是**调用点幂等标记** ⛔ 不是 HTTP 方法（鸡蛋铺读接口走 POST、写接口也可能是 GET 形状 ⇒ 方法什么都证明不了）；`client.call()` 按清单 `risk` 解析（写端点 `False`、读端点 `True`）。
        `inject_token=False` 供**用来获取 token 的**请求（登录/验证码）：不调 token_provider、不发 x-token、不走 401/403 重试、明文快照不含 provider token——否则登录请求回调正在持锁登录的 auth / `on_unauthorized` 在登录失败时重入同一把锁 ⇒ 两者都在请求发出前**死锁**。"""
        url = f"{self._config.api_base}{path}"
        request_headers = dict(headers or {})
        request_headers.setdefault("User-Agent", self._config.user_agent)
        request_headers.setdefault("X-Requested-With", "XMLHttpRequest")
        if referer:
            request_headers.setdefault("Referer", referer)
        token = self._token_provider() if self._token_provider and inject_token else None
        if token:
            request_headers["x-token"] = token

        method_label = method.upper()

        # 上一次请求的业务壳不能泄漏到这一次：没有响应时也必须是 None。
        self.last_envelope = None

        for attempt in range(self._config.max_retries + 1):
            # 明文快照取「发送时」实际持有的凭据，用于所有进入异常/日志的文本。会话凭据是 cookie（spec §0.2 移除了 x-token）：
            # jar 里**所有** cookie 的值都算明文（服务端把 `Cookie` 回显进 msg/错误页时，明文会经异常或公开的 `last_envelope` 漏出去）。
            # 每次尝试都重新取：上一轮响应可能已通过 Set-Cookie 轮换了会话。
            secrets = _sent_secrets(self._config.password, token) + _jar_secrets(
                self._client.cookies
            )
            scrubbed_path = _scrub_text(path, secrets)
            try:
                response = self._client.request(
                    method_label,
                    url,
                    params=_clean(query),
                    json=body if body is not None else None,
                    data=data,
                    files=files,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                detail = _scrub_text(str(exc), secrets)
                # ⭐ 读 / 写超时可能已送达（`_WAS_SENT_TIMEOUTS`）⇒ 只在调用方
                # 声明可重发时重试；连接期 / 连接池超时没出门 ⇒ 一律照旧重试。
                may_have_arrived = isinstance(exc, _WAS_SENT_TIMEOUTS)
                if attempt < self._config.max_retries and (
                    retry_on_sent or not may_have_arrived
                ):
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise JidanpuTimeoutError(
                    f"请求超时：{method_label} {scrubbed_path}（{detail}）"
                ) from exc
            except httpx.HTTPError as exc:
                detail = _scrub_text(str(exc), secrets)
                # ⭐ 读 / 写中断 / 协议断裂可能已送达（`_WAS_SENT_ERRORS`）⇒
                # 只在调用方声明可重发时重试；连接类错误没出门 ⇒ 一律照旧重试。
                may_have_arrived = isinstance(exc, _WAS_SENT_ERRORS)
                if attempt < self._config.max_retries and (
                    retry_on_sent or not may_have_arrived
                ):
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise TransportError(
                    f"请求失败：{method_label} {scrubbed_path}（{detail}）"
                ) from exc
            else:
                self.last_envelope = _envelope_of(response, secrets)
                if (
                    response.status_code in (401, 403)
                    and auth_retry
                    and inject_token
                    and self.on_unauthorized
                ):
                    self.on_unauthorized()
                    return self.request(
                        method,
                        path,
                        query=query,
                        body=body,
                        data=data,
                        files=files,
                        headers=headers,
                        auth_retry=False,
                        inject_token=inject_token,
                        expect_code=expect_code,
                        success_rule=success_rule,
                        referer=referer,
                        retry_on_sent=retry_on_sent,
                        retry_rate_limit=retry_rate_limit,
                    )
                if response.status_code == 429 and retry_rate_limit and attempt < self._config.max_retries:
                    retry_after = _retry_after(response)
                    backoff = retry_after if retry_after is not None else 2**attempt
                    # 夹紧到 [0, MAX_BACKOFF_SECONDS]：畸形的负数头不能让 time.sleep 抛 ValueError
                    wait = max(0.0, min(backoff, MAX_BACKOFF_SECONDS))
                    logger.warning(
                        "被限流，%.1fs 后重试 %s %s", wait, method_label, scrubbed_path
                    )
                    time.sleep(wait)
                    continue
                return unwrap_response(
                    response,
                    expect_code=expect_code,
                    success_rule=success_rule,
                    unwrap=self._config.unwrap,
                    secrets=secrets,
                )

    def masks(self, secret: str) -> bool:
        """本 transport 脱敏时是否会遮蔽 `secret`。`Transport` 只按**自己** config 的密码脱敏；认证层发密码前拿它自检
        （确认「实际发出的明文」=「会被遮蔽的那份」；用另一份 config 的密码时，回显进 `msg` 的密码会带出明文）。
        刻意只回答「是否遮蔽」不交出明文（调用方不需要读到密码）；空串不是明文一律 `False`。"""
        return secret in _usable(_sent_secrets(self._config.password, None))

    def has_cookie(self, name: str) -> bool:
        """cookie jar 里是否存在**非空**值的 `name` cookie（空值不算持有会话）。"""
        return bool(self._client.cookies.get(name))

    def clear_cookie(self, name: str) -> None:
        """从 cookie jar 删掉一个 cookie（用于丢弃失效会话）。"""
        self._client.cookies.delete(name)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _clean(query: dict[str, Any] | None) -> dict[str, Any] | None:
    if not query:
        return None
    return {k: v for k, v in query.items() if v is not None}
