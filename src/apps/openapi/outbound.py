"""开放平台的**出站投递**（HTTPS POST + 签名 + SSRF 防护；T8，设计文档 §7.3 / §7.4 / §4-V6 / §9）。

## 本模块的边界：**只有网络与字节**，一行 ORM 都没有

取件 / 记账 / 退避在 `apps/openapi/dispatch.py`，常驻调度在
`apps/openapi/management/commands/openapi_dispatch.py`。本模块对外只有三个入口：
`validate_callback_url()`（地址判据）、`build_signed_request()`（报文与签名）、
`deliver()`（一次投递）。

## ⛔ 为什么不复用 `apps/business/peer_client.py`（设计文档 §7.4 点名的原因）

`peer_client` 的 docstring 自己写着「本模块是**唯一**出站点」，但它的语义是**点对点绑定**：
它只发**我们自己的信封**到**绑定的对端租户**（`_endpoint()` 拼 `/api/business/<path>`）。
这里要发的是「POST 到一个**由租户任意填写**的 URL」——两者的**信任模型相反**
（绑定对端是我们自己配的；回调地址是租户自己填的、要按 SSRF 防）⇒ 本模块独立存在，
⛔ 也**不去改** `peer_client`（它的绑定语义正被上下游线依赖，稀释它会让那条线静默变松）。

## 报文与签名（§7.3 逐字）

```
body = {**payload, "timestamp": <UTC Unix 秒，整数>, "sign": <hex>}
sign = HMAC-SHA256(app_secret, f"{timestamp}\n{delivery_id}\n{sha256(body_without_sign)}") 的 hex
请求头：X-TT-Timestamp / X-TT-Delivery / X-TT-Signature
```

⭐ **`timestamp` 与 `sign` 都在投递那一刻现算，⛔ 都不进 outbox 的 `payload` 列**，理由两条
（第二条是本模块自己加的，登记在此）：

1. **密钥可轮换**（模型 `OpenApiDelivery` 的 docstring 已写明）：把 `sign` 存进 outbox 就等于把
   签名钉死在写行那一刻的密钥上 ⇒ 轮换后待投递的行会带着一把**已作废密钥**算出的签名送出去，
   对端一律验不过，而失败原因看起来像"对方不认我们的签名"；
2. **`timestamp` 是签名覆盖的一部分**，且它是**对端判新鲜度**的依据：把它钉在入队时刻，
   一次 6 小时后的重投就会带着一个 6 小时前的时刻 + 与它匹配的签名 ⇒ 对端的重放窗口会把它拒掉，
   而**签名本身是对的**（更难排查）。⇒ 两者同生同灭，一起在投递那一刻算。

### `sha256(body_without_sign)` 到底对什么字节算（我们自己的规范化，必须写破）

`body_without_sign` = **不含 `sign` 的报文**（`timestamp` **在**里面，因为签名覆盖它），
规范化 = `json.dumps(…, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 **UTF-8 字节**。
⇒ 对端复算的方式：解析收到的 JSON → 去掉 `sign` → 用**同一套**规范化重新序列化 → `sha256(...).hexdigest()`。
⚠️ 这条规范化是**契约的一部分**（§7.3 只写了 `sha256(body_without_sign)`，没写序列化口径）
⇒ T11 的接入文档必须逐字写明它，并给出可跑的复算代码（§12 那条「文档代码真跑」的判据）。

## SSRF 防护（§4-V6 / §9）：**保存时**与**每次投递前**各校验一次

⚠️ **为什么两处都要**（这是设计文档 §7.1 的要求，代价与理由都写在这里）：

- **保存时**（应用管理端点）：租户填的是 `http://…` / `http://127.0.0.1:8001` / 云元数据地址
  `http://169.254.169.254/…` 时**当场拒**，用户看得见原因；
  只在投递时拒 ⇒ 用户以为配好了，而推送**一条都发不出去**（静默失败）。
- **每次投递前**：地址是**租户填的域名**，DNS 是**可变的**（今天解析到公网、明天解析到
  `10.0.0.1`；或用一个我们校验过、随后 302 到内网的域名）⇒ 只在保存时校验一次是**假保证**。

判据（本模块只做这四条，⛔ 不多不少）：

| # | 判据 | 拒掉的形态 |
|---|---|---|
| 1 | 协议**只能是 `https`** | `http://…`、`ftp://…`、`file://…` |
| 2 | 不许带**用户信息**（`user:pass@`） | `https://user:pass@host/…`（凭据进日志 / 被转发） |
| 3 | 域名**必须解析得出**地址（fail-closed） | 解析失败 ❌ 放行（放行 = 把"解析不出来"当成"安全"） |
| 4 | **每一个**解析出来的 IP 都必须是**公网**（`is_global`） | 回环 `127.0.0.0/8`、私网 `10/172.16/192.168`、链路本地 `169.254.0.0/16`、CGNAT `100.64/10`、保留 / 组播 / 未指定 … |

外加投递侧的 **禁跳转**（`follow_redirects=False`）+ **超时 10 秒** + **只读少量响应体**
（`MAX_RESPONSE_READ_BYTES`，读完即弃）——这三条与上面四条一起构成 §9 那一格。

⚠️ **如实登记的残量风险**：判据 4 校验的是**我们这一次解析**的结果，而 `httpx` 会**自己再解析一次**
（连接那一刻）⇒ 「校验到连接」之间存在一个 **DNS 重绑定窗口**（本轮不消除：消除它要把已解析的 IP
钉进连接（`httpx` 没有直接支持「IP 固定 + SNI 保持域名」的写法），那属于另一条线的改动面）。
本模块**不写**"已完全消除 SSRF"这类绝对句。

## 失败姿势

`deliver()` **不抛异常**：一切失败（含上面的 SSRF 拒绝、超时、非 2xx）都压成
`DeliveryResult(ok=False, …)` —— 由 `dispatch` 记进台账并按退避表重排。
⛔ 只有**编程错误**（未预期）才抛，由常驻进程的逐行异常边界接住（`AGENTS.md` §5-E / E-0010）。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

#: 回调地址**唯一**允许的协议（§4-V6：http 一律拒；内网自建回调本批明确不支持）。
CALLBACK_SCHEME = "https"

#: 一次投递的总超时（秒）。§9 逐字「超时 10s」。
CALLBACK_TIMEOUT_SECONDS = 10.0

#: 一次投递最多读回多少**响应体字节**（读满即断，剩下的丢弃）。§9 逐字「只读少量响应体」。
#: ⚠️ 它不是"响应体上限"（我们**不**校验 `Content-Length`、也不因为对方回得多就判失败）：
#: 它只是"我们读多少"，读完就关连接。取值 2048 的理由：够放下一句人话的报错（排障要它），
#: 又远小于任何"把对方响应体灌进台账"的量级（`reason` 列是 `TextField`，灌满会变成几 MB 的行）。
MAX_RESPONSE_READ_BYTES = 2048

#: 签名 / 报文用到的三个请求头（§7.3 逐字；⛔ 不照抄对标的「参数排序 md5」）。
HEADER_TIMESTAMP = "X-TT-Timestamp"
HEADER_DELIVERY = "X-TT-Delivery"
HEADER_SIGNATURE = "X-TT-Signature"

#: 保存时 / 投递时共用的拒收文案（人话；两处**同一句**——⛔ 不许两处各写一份文案）。
NOT_HTTPS_MSG = "回调地址必须是 https（本批不支持 http / 其它协议）"
CREDENTIALS_IN_URL_MSG = "回调地址里不许带用户名 / 密码"
NO_HOST_MSG = "回调地址里没有域名"
RESOLVE_FAILED_MSG = "回调地址的域名解析不出地址"
NON_PUBLIC_IP_MSG = "回调地址解析到非公网地址（内网 / 回环 / 链路本地 / 保留段一律拒）"


class CallbackUrlError(ValueError):
    """回调地址不合法（保存时 / 投递前共用同一个异常类型与同一句文案）。"""


@dataclass(frozen=True)
class DeliveryResult:
    """一次投递的结果（`dispatch` 按它记账）。

    - `ok`：是否算成功（**只有 HTTP 2xx** 算成功，§7.3：3xx / 4xx / 5xx / 超时全算失败）；
    - `status_code`：对方回执的 HTTP 码（没连上 / 没回 ⇒ `None`）；
    - `reason`：失败原因（人话，进台账的 `reason` 列；成功时是空串）。
    """
    ok: bool
    status_code: int | None
    reason: str


def _resolve_ips(host: str) -> list[str]:
    """把域名解析成**全部**地址（去重、保序）。

    ⚠️ 用 `getaddrinfo`（照 `SOCK_STREAM` 问，与 `httpx` 实际建连的形态一致），
    ⛔ 不用 `gethostbyname`：后者**只回一条 A 记录**（IPv4），双栈域名里那条
    AAAA 记录就逃出校验了——而"逃出校验的那一条"正是被连上的那一条。
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise CallbackUrlError(f"{RESOLVE_FAILED_MSG}：{type(exc).__name__}") from exc
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    if not seen:
        raise CallbackUrlError(RESOLVE_FAILED_MSG)
    return seen


def _is_public_ip(raw: str) -> bool:
    """一个地址是不是**公网**地址。

    ⚠️ 判据只有 `ipaddress.is_global` 一条（它一次覆盖回环 / 私网 / 链路本地 / CGNAT /
    测试段 / 组播 / 未指定 / 保留段）——⛔ 不自己拼一张网段表：手抄的网段表会**过期**
    （新的保留段进了标准，表不会自己长），而 `is_global` 跟着标准库走。
    """
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        # 解析出来的东西不是 IP（理论上不该发生）⇒ fail-closed。
        return False
    # ⚠️ `is_global` 对 IPv4-mapped IPv6（`::ffff:127.0.0.1`）的读数与它映射的那个 IPv4
    #    可能不同 ⇒ 先按映射关系取回真正的 v4 地址再判（这是"用一条判据覆盖两种写法"）。
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global)


def validate_callback_url(url: str) -> str:
    """校验回调地址；合法 ⇒ 原样回（去掉首尾空白），不合法 ⇒ 抛 `CallbackUrlError`。

    ⚠️ **空地址是合法的**（= 没配回调，§7.1「地址为空 ⇒ 不产生投递」）⇒ 回空串、不解析。
    ⛔ **不要**把它读成"空地址通过了 SSRF 校验"：空地址根本不会走到 `deliver()`。

    ⚠️ **保存时与每次投递前都调它**（同一个函数、同一句文案，设计文档 §7.1）——
    ⛔ 不许两处各写一份判据：那一定会漂移，而漂移的表现是「保存时拒了、投递时照发」
    或反过来（后者是**真的 SSRF 通道**）。
    """
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme != CALLBACK_SCHEME:
        raise CallbackUrlError(NOT_HTTPS_MSG)
    if parsed.username or parsed.password:
        raise CallbackUrlError(CREDENTIALS_IN_URL_MSG)
    host = parsed.hostname
    if not host:
        raise CallbackUrlError(NO_HOST_MSG)
    for address in _resolve_ips(host):
        if not _is_public_ip(address):
            raise CallbackUrlError(f"{NON_PUBLIC_IP_MSG}：{host} → {address}")
    return url


def canonical_json(body: dict) -> bytes:
    """报文的**规范化** UTF-8 字节（签名与请求体用的是**同一份**字节，见模块 docstring）。

    ⚠️ `sort_keys=True` + 固定 `separators` + `ensure_ascii=False` 三条**缺一不可**：
    少了任何一条，对端按它自己的 `json.dumps` 复算出的摘要就与我们不同
    ——而症状是**所有推送都被对端判签名不通过**（且我们这边看不出任何异常）。
    """
    return json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compute_signature(
    *, secret: str, timestamp: int, delivery_id: str, body_without_sign: dict
) -> str:
    """`sign = HMAC-SHA256(secret, f"{timestamp}\\n{delivery_id}\\n{sha256(body_without_sign)}")` 的 hex。

    ⚠️ `timestamp` / `delivery_id` 是**我们自己**的值（不是对方的），所以两者都用我们的形态：
    `timestamp` = UTC Unix 秒的**整数**（§7.3 逐字）；`delivery_id` = 那一行的去重身份。
    """
    digest = hashlib.sha256(canonical_json(body_without_sign)).hexdigest()
    message = f"{timestamp}\n{delivery_id}\n{digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_signed_request(
    payload: dict, *, secret: str, timestamp: int | None = None
) -> tuple[bytes, dict[str, str]]:
    """把 outbox 里那行 `payload` 装成**要发出去的字节与请求头**（回 `(body, headers)`）。

    ⚠️ 回的是**规范化之后的字节**（不是 dict）：请求体必须**逐字节**等于我们签名时序列化的那份，
    否则对端复算出的摘要与我们不同（`canonical_json` 的三条理由）。
    ⚠️ 判据用 `is not None`（E-0021 同口径）：`timestamp=0` 是一个**合法**的整数时刻
    （1970-01-01），拿真值判会把它当成"没传"而换成当前时刻 —— 那会让"签名覆盖的时刻"
    与调用方以为的不是同一个。
    """
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    delivery_id = str(payload.get("delivery_id") or "")
    body_without_sign = {**payload, "timestamp": stamp}
    body = {
        **body_without_sign,
        "sign": compute_signature(
            secret=secret,
            timestamp=stamp,
            delivery_id=delivery_id,
            body_without_sign=body_without_sign,
        ),
    }
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        HEADER_TIMESTAMP: str(stamp),
        HEADER_DELIVERY: delivery_id,
        HEADER_SIGNATURE: body["sign"],
    }
    return canonical_json(body), headers


def _read_prefix(response: httpx.Response) -> str:
    """最多读 `MAX_RESPONSE_READ_BYTES` 字节的响应体，回 UTF-8 文本（读不满就有什么读什么）。"""
    buffered = bytearray()
    for chunk in response.iter_bytes():
        buffered += chunk
        if len(buffered) >= MAX_RESPONSE_READ_BYTES:
            break
    return bytes(buffered[:MAX_RESPONSE_READ_BYTES]).decode("utf-8", "replace")


def deliver(
    url: str, *, payload: dict, secret: str, timestamp: int | None = None
) -> DeliveryResult:
    """往 `url` POST 一次签好名的报文，回 `DeliveryResult`（**不抛**预期失败）。

    `timestamp` 只给用例 / 排障用（复算签名）；生产调用方**不传** ⇒ 取"这一刻"的 UTC Unix 秒。
    """
    try:
        validate_callback_url(url)
    except CallbackUrlError as exc:
        # ⚠️ 投递前的 SSRF 拒绝**也算一次失败**（记台账 + 退避），⛔ 不是"跳过不记"：
        #    地址被 DNS 指到内网是一件必须**看得见**的事（页面上那条 `reason` 就是告警面）。
        return DeliveryResult(ok=False, status_code=None, reason=str(exc))
    if not url.strip():
        # 空地址 = 没配回调（§7.1）。调用方（`dispatch`）本来就会先把这种行终态化，
        # 这里再兜一句是为了让本函数**自己**也不会拿着空地址去发一个非法请求。
        return DeliveryResult(ok=False, status_code=None, reason="没有配置回调地址")

    body, headers = build_signed_request(payload, secret=secret, timestamp=timestamp)
    try:
        # ⚠️ `follow_redirects=False` 是**判据**（§9「禁跳转」）：3xx 一律当失败，
        #    ⛔ 不跟着跳到另一个地址去（那正好绕过上面那次 SSRF 校验）。
        # ⚠️ 每次调用建一个 `Client`：投递是长间隔的（一轮最多几十条），
        #    连接池的收益 < "不把状态藏在模块级全局里"的收益。
        with httpx.Client(
            timeout=CALLBACK_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            with client.stream("POST", url, content=body, headers=headers) as response:
                status_code = response.status_code
                snippet = _read_prefix(response)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # 网络层失败（连不上 / TLS / 超时 / DNS 在 httpx 那侧失败）⇒ 预期失败，压成结果。
        # ⛔ 异常文本里**没有** `app_secret`（签名只进请求头，不进异常消息）。
        detail = str(exc)
        return DeliveryResult(
            ok=False,
            status_code=None,
            reason=f"投递失败（{type(exc).__name__}）：{detail}" if detail
            else f"投递失败（{type(exc).__name__}）",
        )
    if 200 <= status_code < 300:
        return DeliveryResult(ok=True, status_code=status_code, reason="")
    snippet = snippet.strip()
    reason = f"对方回了 HTTP {status_code}"
    if snippet:
        reason = f"{reason}：{snippet}"
    return DeliveryResult(ok=False, status_code=status_code, reason=reason)
