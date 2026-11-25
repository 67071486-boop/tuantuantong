"""**下家调上家**的 httpx 封装（spec §5.1 / §5.2 / §7.2）。 ⛔ 本模块是**唯一**出站点：⛔ 别在视图里直接 `httpx.post`（那样闸门与超时就散开了）。 ⚠️ 判成败一律看**业务码**，⛔ 不看 HTTP 状态码（本仓的失败也是 200 + 业务码，spec §9）。
## 两种失败分得很开（调用方按它们翻业务码） | 异常 | 什么时候抛 | 页面端点翻成 | |---|---|---| | `PeerUnreachable` | 连不上 / 超时 / 响应不是 JSON / 形状不对 | `1021` |
| `PeerRejected` | 收到了信封、但**业务码非 0** | `1020` | ⚠️ 「形状不对」归 `PeerUnreachable` 是**刻意**的：对端回了个裸列表或少了 `data` 是 **未预期的对端变更**，不许静默当成空值（`AGENTS.md` §5-E / E-0010）。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

#: 握手是**人在页面上等**的动作（spec §5.1）⇒ 超时短一点，5 秒。
BIND_TIMEOUT_SECONDS = 5.0
#: 事件投递是后台动作（spec §7.2）⇒ 可以宽一点，但本期它是同步投递、仍要短。
PUSH_TIMEOUT_SECONDS = 5.0
#: 大厅补拉每个网络阶段的默认等待上限。共享进程给整页两倍时间（默认 3 秒）。
HALL_PULL_PAGE_TIMEOUT_SECONDS = 1.5
HALL_PULL_REQUEST_ID_HEADER = "X-Peer-Hall-Request-ID"


class PeerUnreachable(Exception):
    """网络层失败（超时 / DNS / 连接被拒 / 非 JSON / 形状不对）⇒ 翻 `1021`（spec §8）。"""


class PeerHallBodyIncomplete(PeerUnreachable):
    """共享大厅响应体读超时或中途断开，可短退避补试。"""


class PeerUnsupported(PeerUnreachable):
    """对端**没有这个端点**（HTTP 404/405）——只用于**路由形状**分类。 ⭐ 计划 `2026-10-25-peer-hall-pull` §6：`PeerUnreachable` 原来把 404 / 非 JSON / 超时 /
    连不上吞成同一个异常 ⇒ 下家无法区分「老上家还没有拉取端点」与「这一秒网络抖了」，一次超时就会把该绑定的拉取永久停掉。 - 继承 `PeerUnreachable`：所有既有调用方（解绑 / 投递 / 对账）的 `except PeerUnreachable`
    行为**一字不变**（404 仍然算不可达），只是拉取侧可以**先**接住它、按「对端不支持」处理； - ⛔ 判成败仍看**业务码**：这个异常只由 HTTP 状态码 404/405 触发，⛔ 不按状态码判成功/失败（本仓的失败也是 200 + 业务码，spec §9）。"""


class PeerRejected(Exception):
    """对端明确拒收（业务码非 0）⇒ 翻 `1020`。"""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code

class PeerBusy(PeerRejected):
    """对端临时锁冲突，可以稍后重试。"""


#: ⭐ C5（L-3，审计批）：出站应答的**流式读上限**（字节）。正常应答（信封 JSON）远小于它——事件批 ≤100 条、对账映射 ≤200 个号、大厅页 ≤200 张卡。
#: 恶意 / 失常的对端回一个数 GB 的「JSON」时，无上限的 `resp.json()` 会把 worker 内存打爆；
#: 有了这个闸，超限按 `PeerUnreachable` 处理（= 这一轮投递 / 拉取按「对端没回话」走退避）。
PEER_RESPONSE_MAX_BYTES = 10 * 1024 * 1024


def _endpoint(base_url: str, path: str) -> str:
    """把同步端点拼到上家基址后面。 ⚠️ 基址是我们自己存的（`normalize_base_url` 的产物，已去掉尾斜杠）——这里再
    `rstrip("/")` 一次只是不让"将来某个入口多打了一个斜杠"变成 `//api/...` 这种
    诡异路径（服务器可能 404，而症状看起来像对端坏了）。"""
    return f"{base_url.rstrip('/')}/api/business/{path.lstrip('/')}"


def _read_body_capped(resp: httpx.Response, *, cap: int) -> bytes:
    """流式读响应体，累计超过 `cap` 立刻断掉（⛔ 不把超大应答整个读进内存）。 ⚠️ 必须在 `iter_bytes()` **逐块**累计里判：先 `resp.content` 再比长度 = 已经读完了，
    闸就是摆设。超限抛 `PeerUnreachable`（调用方按「对端没回话」分流，`AGENTS.md` §5-E）。"""
    total = 0
    parts: list[bytes] = []
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > cap:
            raise PeerUnreachable(
                f"对端应答超过 {cap} 字节上限，按失联处理"
            )
        parts.append(chunk)
    return b"".join(parts)


_deadline_loop = ContextVar("peer_deadline_loop", default=None)


def new_peer_deadline_loop():
    loop = asyncio.new_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1, thread_name_prefix="peer-dns"))
    return loop


@contextmanager
def enforce_total_peer_timeout(loop):
    token = _deadline_loop.set(loop)
    try:
        yield
    finally:
        _deadline_loop.reset(token)


def _check_route_support(response):
    if response.status_code in (404, 405):
        raise PeerUnsupported(f"对端没有这个端点（HTTP {response.status_code}）")


def valid_hall_pull_request_id(value: str | None) -> bool:
    return value is not None and re.fullmatch(r"[0-9a-f]{32}", value) is not None


@dataclass
class _HallPullTrace:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.monotonic)
    phase: str = "response_headers"
    response: httpx.Response | None = None

    def log_failure(self, exc: Exception) -> None:
        cause = exc.__cause__ or exc
        phase = self.phase
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)):
            phase = "connect"
        elif isinstance(cause, (httpx.WriteError, httpx.WriteTimeout)):
            phase = "request_body"
        expected, received = None, None
        if self.response is not None:
            length = self.response.headers.get("Content-Length", "")
            if "Transfer-Encoding" not in self.response.headers and re.fullmatch(r"[0-9]{1,18}", length):
                expected = int(length)
            received = self.response.num_bytes_downloaded
        logger.warning(
            "大厅拉取请求失败 request_id=%s phase=%s error=%s elapsed_ms=%d expected_bytes=%s received_bytes=%s",
            self.request_id, phase, type(cause).__name__, (time.monotonic() - self.started_at) * 1000,
            expected, received,
        )


async def _read_before_deadline(url, *, payload, headers, timeout, total_timeout, hall_trace=None):
    async with asyncio.timeout(total_timeout):
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers, timeout=timeout,
            ) as response:
                if hall_trace is not None:
                    hall_trace.response = response
                _check_route_support(response)
                if hall_trace is not None:
                    hall_trace.phase = "response_body"
                try:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > PEER_RESPONSE_MAX_BYTES:
                            raise PeerUnreachable(f"对端应答超过 {PEER_RESPONSE_MAX_BYTES} 字节上限，按失联处理")
                        body.extend(chunk)
                except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                    if hall_trace is None:
                        raise
                    raise PeerHallBodyIncomplete(f"大厅回包中断（{type(exc).__name__}）") from exc
                return body


def _post_envelope(
    url, *, payload, headers, timeout, rejected_default, total_timeout_multiplier=1, hall_trace=None,
) -> dict:
    """POST 一次并解出信封；网络层失败 ⇒ `PeerUnreachable`，业务码非 0 ⇒ `PeerRejected`。 ⛔ **不读 HTTP 状态码**（本仓的失败也是 200 + 业务码，spec §9）。
    ⚠️ 非 `dict` 的 JSON 也算不可达：不接这一支的话 `envelope.get` 会抛 `AttributeError`，页面端点会把它变成 500 —— 未预期的对端变更伪装成"我们的接口坏了"（E-0010）。
    ⭐ C5（L-3）：应答走**流式读 + 10MB 上限**（`PEER_RESPONSE_MAX_BYTES`），超限按 `PeerUnreachable` 处理（见那条常量的注释）。"""
    try:
        loop = _deadline_loop.get()
        if loop is None:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=timeout) as resp:
                if hall_trace is not None:
                    hall_trace.response = resp
                _check_route_support(resp)
                if hall_trace is not None:
                    hall_trace.phase = "response_body"
                body = _read_body_capped(resp, cap=PEER_RESPONSE_MAX_BYTES)
        else:
            total_timeout = timeout
            if isinstance(timeout, (int, float)):
                total_timeout *= total_timeout_multiplier
            body = loop.run_until_complete(
                _read_before_deadline(
                    url, payload=payload, headers=headers, timeout=timeout, total_timeout=total_timeout,
                    hall_trace=hall_trace,
                ),
            )
        if hall_trace is not None:
            hall_trace.phase = "response_parse"
        envelope = json.loads(body)
    except TimeoutError as exc:
        if loop is None:
            raise
        raise PeerUnreachable("对端请求总耗时超过上限") from exc
    except (httpx.HTTPError, ValueError) as exc:
        if hall_trace is not None:
            raise PeerUnreachable(f"大厅拉取请求失败（{type(exc).__name__}）") from exc
        raise PeerUnreachable(str(exc)) from exc
    if not isinstance(envelope, dict):
        raise PeerUnreachable("对端回的不是一个信封（JSON 对象）")
    if envelope.get("code") != 0:
        if hall_trace is not None:
            hall_trace.phase = "response_business"
        data = envelope.get('data')
        if envelope.get("code") == 409 and isinstance(data, dict) and data.get("retry_kind") == "split_group_busy":
            raise PeerBusy(envelope['msg'])
        raise PeerRejected(envelope.get("msg") or rejected_default, code=envelope.get("code"))
    return envelope


def bind_upstream(base_url, *, account, password, token_out, local_base_url,
                  timeout=BIND_TIMEOUT_SECONDS) -> str:
    """调上家的 `peer-sync/bind/`，回它给我的令牌（spec §5.1 第 3–4 步）。"""
    envelope = _post_envelope(
        _endpoint(base_url, "peer-sync/bind/"),
        payload={
            "account": account,
            "password": password,
            "peer_base_url": local_base_url,
            "token_out": token_out,
        },
        # 握手是**唯一**一次不带令牌的调用：此刻还没有令牌可带。
        headers=None,
        timeout=timeout,
        rejected_default="对端拒绝了这个绑定",
    )
    data = envelope.get("data")
    token = data.get("token_out") if isinstance(data, dict) else None
    if not token:
        # 形状不对是**未预期的对端变更** ⇒ 响亮地按不可达报，⛔ 不静默当成空
        raise PeerUnreachable("对端没有回令牌")
    return token


def push_events(base_url, token, events, *, timeout=PUSH_TIMEOUT_SECONDS) -> dict:
    """把一批事件投给对端（spec §7.2），回信封里的业务数据 `data`。本期只有上家 → 下家一个方向。⚠️ 调用方要的是 `data` 里的 `skipped_details`
    （spec §16.5 的部分失败契约）⇒ 这里**如实回**它：`data` 不是 dict（缺失 / null / 对端形状漂了）⇒ 回 `{}`，⛔ 不抛——**真失败**（网络层 / 业务码非 0）已经由
    `_post_envelope` 抛成 `PeerUnreachable` / `PeerRejected`，调用方按那两种异常分流。"""
    envelope = _post_envelope(
        _endpoint(base_url, "peer-sync/inbound/events/"),
        payload={"events": events},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        rejected_default="对端拒收了事件",
    )
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def fetch_applied_watermarks(
    base_url, token, orders, *, order_nos=None, timeout=PUSH_TIMEOUT_SECONDS
) -> dict:
    """问对端「这些单号你应用到哪了」（P3 Task 8 对账 / spec §16.8）。形状与 `push_events` 完全同形（同一个 `_post_envelope`、同一个 Bearer 头、同一个超时）， ⛔ 不另开一条出站路径（那样超时与闸门就散开了，见模块 docstring）。 `orders` = 要问的**同步身份键**（起 = 来源单号）；`order_nos` = 可选的
    **第二个键** `{<来源单号>: <发送方内置号>}`——老链路那一列 `peer_order_no` 记的是当年那个内置号，收方**任一对上即命中**（计划 §4 的过渡期兼容）。 ⚠️ `order_nos` **空的就不发这个键**：老对端不认识它，多一个空对象只是噪声； ⛔ 也不要把它写成 `None`（JSON 里是 `null`，语义是"我给了一个空值"而不是"我没这个键"）。
    回信封里的 `data["applied"]`（`{"<对端单号>": "<ISO>" | None}`）；`data` 不是 dict、或 `applied` 不是 dict（缺失 / null / 对端形状漂了）⇒ 回 `{}`，⛔ **不抛**——**真失败** （网络层 / 业务码非 0）已经由 `_post_envelope` 抛成 `PeerUnreachable` / `PeerRejected`。 ⚠️ `{}` 的语义是"问不出来"，调用方（`run_reconcile`）把它当"全部落后"处理：宁可多投一次（接收侧幂等），也不漏一单。"""
    payload = {"orders": orders}
    if order_nos:
        payload["order_nos"] = order_nos
    envelope = _post_envelope(
        _endpoint(base_url, "peer-sync/reconcile/"),
        payload=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        rejected_default="对端拒绝了对账请求",
    )
    data = envelope.get("data")
    if not isinstance(data, dict):
        return {}
    applied = data.get("applied")
    return applied if isinstance(applied, dict) else {}


def fetch_hall_page(
    base_url, token, *, cursor=None, limit=200,
    timeout=HALL_PULL_PAGE_TIMEOUT_SECONDS,
) -> dict:
    """拉一页上家共享给**这条绑定**的在厅卡（计划 `2026-10-25-peer-hall-pull` §1）。与 `push_events` / `fetch_applied_watermarks` 同一个 `_post_envelope`（同一套 Bearer 头、同一条出站路径、同一个业务码判据），只是多一层**响应形状闸**：拉取的应答会被下家直接拿去改镜像行，形状漂了必须**响亮不可达**（于是这一轮作废、不判消失），
    ⛔ 不许静默当成空页（空页会被当成「真的一张开厅卡都没有」，那是安全错误的来源）。返回 `{"shared", "count", "rows", "next_cursor", "complete"}`（已校验形状）： - `shared=False` = 对端**不对这条绑定共享**（开关关着 / 没有共享关系）⇒ 调用方 **不判 gone**（三态见计划 §1.2；`shared=True, rows=[]` 才是真空厅）；
    - `rows` = `[{"target_ref": <卡号>, "payload": <订单白名单载荷>}]`； - `next_cursor` = 不透明串（⛔ 不当整数解析）； - `complete` = 对端判的本页是否走到尽头（⛔ 不拿 `count` 判）。 ⚠️ 老上家没有这个端点 ⇒ `_post_envelope` 抛 `PeerUnsupported`（404/405），调用方按「对端不支持」加长 TTL，⛔ 不计入绑定连续失败、⛔ 不自动停绑（计划 §6）。"""
    trace = _HallPullTrace()
    try:
        envelope = _post_envelope(
            _endpoint(base_url, "peer-sync/hall/list/"),
            payload={"cursor": cursor, "limit": limit},
            headers={
                "Authorization": f"Bearer {token}",
                HALL_PULL_REQUEST_ID_HEADER: trace.request_id,
            },
            timeout=timeout,
            total_timeout_multiplier=2,
            rejected_default="对端拒绝了这次大厅拉取",
            hall_trace=trace,
        )
        return _parse_hall_page(envelope)
    except (PeerUnreachable, PeerRejected) as exc:
        trace.log_failure(exc)
        raise


def _parse_hall_page(envelope) -> dict:
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise PeerUnreachable("大厅拉取的应答里没有 data 对象")
    shared = data.get("shared")
    rows = data.get("rows")
    count = data.get("count")
    next_cursor = data.get("next_cursor")
    complete = data.get("complete")
    if (
        not isinstance(shared, bool)
        or not isinstance(complete, bool)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(rows, list)
        or not (next_cursor is None or isinstance(next_cursor, str))
    ):
        raise PeerUnreachable("大厅拉取的应答形状不对（shared/count/rows/next_cursor/complete）")
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("target_ref"), str)
            or not row["target_ref"]
            or not isinstance(row.get("payload"), dict)
        ):
            raise PeerUnreachable("大厅拉取的行形状不对（缺 target_ref / payload）")
    return data


def unbind_upstream(base_url, token, *, timeout=PUSH_TIMEOUT_SECONDS) -> None:
    """通知对端解除绑定（spec §5.2 第 2 步）。 ⚠️ **调用方（页面端点）要吞掉这两个异常**：spec §5.2 第 5 条写明「通知失败不能让
    解绑失败」，否则用户会卡在一个删不掉的绑定上。本函数仍如实抛，分级在这里、取舍在调用方。"""
    _post_envelope(
        _endpoint(base_url, "peer-sync/unbind/"),
        payload={},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
        rejected_default="对端拒绝了这次解绑",
    )


def direct_taker_request(binding, action, **params):
    """直派只走现有工作室绑定的 HTTPS 通道。"""
    if not binding.peer_base_url.startswith("https://"):
        raise PeerUnsupported("该工作室的绑定尚未使用 HTTPS")
    envelope = _post_envelope(
        _endpoint(binding.peer_base_url, "peer-sync/direct-taker/"),
        payload={"action": action, **params},
        headers={"Authorization": f"Bearer {binding.token_out}"},
        timeout=3,
        rejected_default="下家暂时无法处理直派请求",
    )
    result = envelope.get("data")
    if result is not None and not isinstance(result, dict):
        raise PeerUnreachable("直派应答格式不正确")
    return result
