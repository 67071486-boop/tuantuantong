"""订单群码自动拉取（对接「马上来 self-app」查码接口；D-0348）。 ## 一件事：订单没有群二维码时，在自然触发点异步拉一次对端、转存落库对端接口（文档：马上来 self-app 仓 `docs/tt-order-qr-api.md`，**仓外只读依据**）： `GET {base_url}/api/tt/order-qr?order_no=<团团通订单号>&source_order_no=<鸡蛋铺单号可选>`， `Authorization: Bearer <api_key>`；HTTP 200 信封 `code==0` 里 `data.status` 三态： | 态 | 含义 | 我方处置 | |---|---|---| | `ok` | 群码就绪，`qr_url` 是企微图床**临时地址（会过期）** | 下载**转存字节**（⛔ 不存 URL）→ 走既有截图签发+认领管道落 `img_type=1` | | `group_not_found` | 群没建 | **静默**（等下次自然触发点，或等查码队列的下一轮——每轮全量逐个问，失败不退避） | | `generating` | 建码中 | **静默**（同上；对端建议 30-60s 重试——web 请求路径刻意不轮询，定时补拉由查码队列每分钟一轮做） | 错误：401（密钥无效 ⇒ **闩**停触发）/ 429（按 `Retry-After` 冷却）/ 503（对端未启用，静默）/ **404**（⭐ D-0364，2026-11-02：对端对**没有的单回 404**——文档外实测形态，生产实探确认；按「对端查无此单」折成 `not_found` 静默档，**不写台账**）/ 200+1000（参数，我方契约 bug，logger）/ 200+500（对端内部错误，可重试档）。对端限速按我方 IP：查询 ≤120/min、**触发建码 ≤30/min 独立预算**（2026-10-31 对端新口径，D-0356 取代旧的 ≤2/min）。 ## 触发点（三个，都幂等防抖） 1. **查看触发**：打手 H5 订单详情的群码区块取数口 `my-orders/<no>/detail/` （`MyOrderDetailSerializer.get_group_qr_url` 回 `None` = 区块是「暂无群码」）与客服端截图列表读口 `orders/<no>/screenshots` 的 `list_items`（列表里没有 `img_type=1` 行）；
2. **接单触发**：`claim.py::claim_order` 尾部（web `/claim`、takerapi、机器、peer 四路收敛点）； 3. **查码队列（定时，D-0356，2026-10-31 业务方拍板）**：管理命令 `fetch_order_qr_queue`（timer 每分钟触发）扫最近 24 小时录入、缺群码、不在退避期的单，老单优先、按**单龄分层间隔**（<1h 每 1 分钟、1~6h 每 5 分钟、6~24h 每 15 分钟—— ⭐ 2026-11-03 CPU 底噪治理批，出站频次随单龄衰减；判定纯函数、无跨进程状态），逐单调 `_run_fetch_careful(record_failures=False)` 补拉，且命令把**已批量核过的** `(order, config)` 经 `preverified` 传下来折掉每单的复核查询（每问一张的额外库往返趋近于零）。timer 通道的失败**不进操作日志页**（只 logger）；落库失败折成 `land_failed` 结局（H1，2026-11-01：命令摘要**不**计 ok）、单笔意外异常记 `unexpected` 后**继续本轮**（毒单不中断整租户；本轮出现过 ⇒ 退出码 1）；持久列 `Order.qr_fetch_suppress_until` **仅对端 429 写**（⭐ 2026-11-01 撤退避：普通失败下一轮自然重试；cache 冷却每进程一份，对每分钟新起的 timer 进程无效）。调用方只调 `maybe_enqueue_fetch(order)`：五道闸（配置 / 未软删 / 已有群码短路 / 冷却 / 护栏）全过才在 `transaction.on_commit` 后 spawn 一个 daemon 线程。请求路径**零出站**（E-0022/PROD-02 先例：出站 HTTP 不进请求事务、不阻塞响应，形状照 `order_push`）。 ## ⚠️ 线程里的租户上下文（与 order_push 的关键差异） `order_push` 的线程**禁查库**（新连接没有租户 schema）。本模块的线程**必须**查库（读配置、复核、落库）⇒ 线程自己重建租户上下文：`Tenant.objects.get(schema_name=…)`（public 默认可见） → `tenant_context(tenant)` → 业务读写 → finally `connection.close()`（线程本地连接用完即关，
防泄漏）。先例：`peer_sync` 命令的逐租户 `tenant_context`；`docs/WORKFLOW.md` §E「请求路径以外要访问租户业务面需自己建立租户上下文」。 ## 落库（ok 态）——逐字复用既有管道，⛔ 不绕行 `issue_screenshot_ticket`（共享前缀 key `kind="qr"` + 订单行锁 + 落 pending 行）→ `put_bytes`（机器侧的"直传"那跳）→ `claim_screenshot`（同一把行锁 + head 真实大小 + 魔数 + `_evict_previous_group_qr` 淘汰 + ready + D-0316 的 peer 广播 + member 补推）。签发返回的预签名 `upload_url` 本流程用不上（`presign_put` 两个后端都是纯本地签名、零远端写）。 ## 防抖与护栏 - **同单冷却**：cache 键 `order-qr-fetch:cool:{schema}:{order_no}`，入队时铺 `COOLDOWN_SECONDS`（60s，2026-11-01 业务方「统一 1 分钟」）；429 的 `Retry-After` 更长时改写（封顶 `RATE_LIMIT_COOLDOWN_CAP`）。 ⚠️ **跨进程残差（如实登记）**：默认 LocMem cache 每进程一份 ⇒ 多 worker 间冷却不共享，同单可能被两个 worker 各拉一次；后果自愈（订单行锁串行 + 淘汰 + `uniq_order_ready_group_qr` 仍只留一张 ready 群码、peer 接收侧幂等）。 ⭐ D-0356 起另有**持久退避列** `Order.qr_fetch_suppress_until`——⭐ 2026-11-01 业务方拍板收窄成**仅对端 429 写**（`max(60s, retry_after)`）：普通失败一律不写、每分钟一轮的扫描本身就是节流，下一轮自然重试。web 的 cache 冷却与它**并存、互不替代** ——cache 管请求路径的快连击，列管定时队列里对端明确要求的等待（timer 每分钟是新进程，看不见 cache）。 - **成功后永不重触发**：不靠冷却，靠「已有 ready 群码 ⇒ 短路」这道闸（入队与 worker 里各判一次）。 - **并发上限**：`BoundedSemaphore(MAX_CONCURRENT_FETCHES)`，槽在 **on_commit 回调里**非阻塞拿（事务回滚不会漏放）；拿不到就丢弃（`_spawn` 返回 False）。槽跟着**获取方**走：web 的线程获取/归还成对；查码队列逐单同步跑、本就不占槽。 - **速率护栏（留死）**：滑窗 60s 内 ≥ `MAX_STARTS_PER_MINUTE` ⇒ 丢弃 + logger.warning。点开详情是稀疏人工动作，正常到不了；真到了说明有异常（例如有人脚本刷详情），宁可少拉。 ## 失败与安全 - **密钥安全**：api_key 只存 `OrderQrFetchConfig` 行；client 的错误消息只含 host（照 `order_push._post_json` 口径）；台账与日志 ⛔ 不含密钥、⛔ 不含 qr_url 全文。 - **台账**：成功 / 401 闩沿 / 网络异常 / 对端 500 ⇒ `order.malailai_qr_fetch` 机器行（`system=True`；actor=「马上来查码」）。`group_not_found` / `generating` / 对端 503 / **对端 404（对端查无此单，D-0364）** / 我方参数错 ⇒ 不记（静默态与契约 bug 不刷台账）。 ⭐ D-0356 起 `record_failures` 按通道分口：web（默认 True）照旧；**查码队列传 False**—— 失败只 logger（journald 可查），只有 `ok` 与 401 闩沿照记台账（否则对端抖十分钟 = 每台几十行机器失败行淹没操作日志页）。 - **线程兜底**：worker 顶层 catch + logger——后台线程的异常没人接，让它响亮地进日志而不是炸进程；未预期异常不写退避（2026-11-01：异常单仍在候选里，下一轮自然重试）。"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.core.cache import cache
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from apps.audit.actions import QR_FETCH_SUCCESS_REASON
from apps.business.models import OrderScreenshot

logger = logging.getLogger(__name__)

#: 一次查码的 HTTP 超时（秒）。对端是查库 + 可能当场建码，比 webhook 宽一点，但不宽很多：
#: 这是后台线程，挂久了只是占并发槽。
FETCH_TIMEOUT_SECONDS = 10.0
#: 下载群码图片的超时（秒）。
DOWNLOAD_TIMEOUT_SECONDS = 15.0

#: 同单冷却（秒）：失败 / 未就绪后这个窗口内不再触发。 ⭐ 业务方拍板「统一改成 1 分钟」：web 触发的 cache 冷却与查码队列的
#: 持久退避列（**仅 429 写**）取大者的下限；429 仍按 `Retry-After` 取大者，封顶不变。
COOLDOWN_SECONDS = 60
#: 429 时冷却改写为对端 `Retry-After`，但不超过这个封顶（秒）。
RATE_LIMIT_COOLDOWN_CAP = 3600
#: 同时在跑的拉取线程上限（全局，跨单）。
MAX_CONCURRENT_FETCHES = 2
#: 速率护栏（留死）：滑动 60 秒窗口内的拉取启动数上限。对端查询预算 120/min，
#: 留出其它调用方余量后取 100；正常人工触发到不了。
MAX_STARTS_PER_MINUTE = 100
_RATE_WINDOW_SECONDS = 60.0

#: 对端三态里「就绪」的值（信封 `data.status`）。
STATUS_OK = "ok"

USER_AGENT = "tuantuantong-order-qr-fetch/1.0"

#: 台账动作码（`apps/audit/actions.py` 同批登记）。actor = 机器名「马上来查码」。
LEDGER_ACTION = "order.malailai_qr_fetch"
LEDGER_ACTOR = "马上来查码"


class QrFetchError(RuntimeError):
    """查码 / 下载失败（人话；**不含 api_key**、⛔ 不含 qr_url 全文——只留 host）。"""


@dataclass(frozen=True)
class QrFetchResult:
    """一次查码的类型化结果（`kind` 是**封闭**枚举，⛔ 不拿字符串裸传）。"""

    kind: str  # ok / not_found / generating / rate_limited / unauthorized / peer_disabled / peer_error / network / bad_param
    qr_url: str = ""
    retry_after_seconds: int = 0
    detail: str = ""


# ── 出站 client ─────────────────────────────────────────────────────────────


def _build_transport() -> httpx.BaseTransport | None:
    """httpx 传输工厂（测试注入口）：默认 `None` = 真实网络。 ⚠️ 测试 patch **本模块属性**（E-0029：桩打在被查找的模块上）返回
    `httpx.MockTransport(handler)`——⛔ 不打真接口 malailai.suqing.chat。"""
    return None


def _safe_host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or "（未知地址）"


def fetch_order_qr(
    *, base_url: str, api_key: str, order_no: str, source_order_no: str = ""
) -> QrFetchResult:
    """调一次对端查码接口。**不抛预期失败**——所有失败都折进 `kind`。
    ⚠️ `detail`（人话）只含 host；`api_key` 与 `qr_url` 全文**不进**本返回值（那会让它顺着
    调用方流进日志 / 台账）。"""
    url = f"{base_url.rstrip('/')}/api/tt/order-qr"
    params: dict[str, str] = {"order_no": order_no}
    if source_order_no:
        params["source_order_no"] = source_order_no
    try:
        # ⛔ 不跟随重定向（SSRF 纪律，与 order_push / dailiantong 同一条）。
        # ⚠️ 走 `httpx.Client` 而不是顶层 `httpx.get`：只有 Client 收 `transport`
        #（测试注入口，MockTransport 从这里进来）。
        with httpx.Client(
            transport=_build_transport(), follow_redirects=False
        ) as client:
            response = client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": USER_AGENT,
                },
                timeout=FETCH_TIMEOUT_SECONDS,
            )
    except httpx.HTTPError as exc:
        return QrFetchResult(
            kind="network",
            detail=f"连接查码接口失败（{_safe_host(url)}）：{type(exc).__name__}",
        )

    if response.status_code == 401:
        return QrFetchResult(
            kind="unauthorized",
            detail=f"查码接口凭据无效（{_safe_host(url)}）",
        )
    if response.status_code == 429:
        try:
            retry_after = int(response.headers.get("Retry-After", "0"))
        except (TypeError, ValueError):
            retry_after = 0
        return QrFetchResult(
            kind="rate_limited",
            retry_after_seconds=max(retry_after, 0),
            detail=f"查码接口限速（{_safe_host(url)}）",
        )
    if response.status_code == 503:
        return QrFetchResult(
            kind="peer_disabled",
            detail=f"查码接口未启用（{_safe_host(url)}）",
        )
    if response.status_code == 404:
        # ⭐ 案 C（D-0364，生产实探）：对端对**没有的单回 HTTP 404** （`tt-order-qr-api` 文档外的实测形态）＝「对端查无此单」——
        # 与 200 + `group_not_found` 同档**静默态**（⛔ 不刷台账；现场根因：旧折成 `peer_error` 时恰好撞上「已有 ready 群码就短路」闸 ⇒ 全租户
        # 台账只留下这条 404 失败行）。`detail` 带 host（照全模块口径：⛔ 不含密钥 / 单号以外的业务值），队列通道据此在 journald 里可辨可数。
        return QrFetchResult(
            kind="not_found",
            detail=f"对端系统里没有这张单（{_safe_host(url)}）",
        )
    if response.status_code != 200:
        return QrFetchResult(
            kind="peer_error",
            detail=f"查码接口返回 HTTP {response.status_code}（{_safe_host(url)}）",
        )

    try:
        body = response.json()
    except ValueError:
        return QrFetchResult(
            kind="peer_error", detail=f"查码接口返回非 JSON（{_safe_host(url)}）"
        )
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "?"
        if code == 1000:
            # 我方契约 bug（order_no 缺失/非法不该发生）——响亮但不刷台账。
            return QrFetchResult(
                kind="bad_param", detail=f"查码接口判参数非法（code=1000）：{body.get('msg') or ''}"
            )
        return QrFetchResult(
            kind="peer_error",
            detail=f"查码接口返回业务码 {code}（{_safe_host(url)}）",
        )

    data = body.get("data") or {}
    if not isinstance(data, dict):
        return QrFetchResult(kind="peer_error", detail="查码接口 data 形状不对")
    status = str(data.get("status") or "")
    if status == "not_found" or status == "group_not_found":
        return QrFetchResult(kind="not_found")
    if status == "generating":
        return QrFetchResult(kind="generating")
    if status == STATUS_OK:
        qr_url = str(data.get("qr_url") or "").strip()
        if not qr_url:
            return QrFetchResult(kind="peer_error", detail="查码接口 ok 态缺 qr_url")
        return QrFetchResult(kind="ok", qr_url=qr_url)
    return QrFetchResult(kind="peer_error", detail=f"查码接口未知 status：{status}")


def _download_image(url: str) -> bytes:
    """下载 `qr_url` 的图片字节（**转存**，临时 URL 会过期 ⛔ 不落库）。 SSRF 口径（照 `order_push.validate_webhook_url`）：http(s)、挡回环 / 未指定 / 链路本地
    （云元数据）；不跟随重定向；上限 `MAX_IMAGE_BYTES`。 ⚠️ 测试不打真网络：落库用例 patch 本函数返回真字节（dailiantong `_fetch_image` 先例）；
    本函数的守卫逻辑由用例**直调坏 URL** 钉住。"""
    from apps.core.storage import MAX_IMAGE_BYTES

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().strip("[]").rstrip(".")
    if parsed.scheme not in ("http", "https") or not host:
        raise QrFetchError("群码地址不是 http(s) 直链，拒绝下载")
    if host in {"localhost", "0.0.0.0", "::1"} or host.endswith(".localhost"):
        raise QrFetchError("群码地址指向本机回环地址，拒绝下载")
    import ipaddress

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_loopback or (mapped is not None and mapped.is_loopback):
            raise QrFetchError("群码地址指向本机回环地址，拒绝下载")
        if address.is_unspecified:
            raise QrFetchError("群码地址是未指定地址，拒绝下载")
        if address.is_link_local:
            raise QrFetchError("群码地址指向链路本地地址，拒绝下载")

    try:
        with httpx.Client(
            transport=_build_transport(), follow_redirects=False
        ) as client:
            with client.stream(
                "GET",
                url,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.status_code != 200:
                    raise QrFetchError(f"下载群码失败（HTTP {response.status_code}）")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise QrFetchError(f"群码超过本仓上限（{MAX_IMAGE_BYTES} 字节）")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise QrFetchError(f"下载群码失败（{type(exc).__name__}）") from exc
    data = b"".join(chunks)
    if not data:
        raise QrFetchError("群码是空文件")
    return data


# ── 冷却（cache；键带租户 schema，⛔ 不同租户互相串）─────────────────────────


def _cooldown_key(schema: str, order_no: str) -> str:
    return f"order-qr-fetch:cool:{schema}:{order_no}"


def cooldown_remaining(schema: str, order_no: str) -> int:
    """本单冷却剩余秒数（0 = 不在冷却）。测试与排障用。"""
    return int(cache.get(_cooldown_key(schema, order_no)) or 0)


def _set_cooldown(schema: str, order_no: str, seconds: int) -> None:
    cache.set(_cooldown_key(schema, order_no), int(seconds), max(int(seconds), 1))


# ── 持久退避列（D-0356；跨进程可见，web / timer 两通道统一写）──────────────────


def _suppress_fetching(order, *, retry_after_seconds: int = 0) -> None:
    """**仅 429** 的持久退避：写 `Order.qr_fetch_suppress_until`（⭐ 2026-11-01 业务方拍板「不需要失败退避，每分钟扫描本身就是节流」⇒ 普通失败一律**不写**、下一轮自然
    重试；唯一例外是对端回 429 明确给了「过 N 秒再来」——听对端的）。 - 公式 `max(COOLDOWN_SECONDS, retry_after)`：对端没带头时至少等 1 分钟；
    ⛔ **不把对端给的窗口压小**（cache 那层保留自己的 3600 封顶，两层语义取并集）。 - 用 queryset 条件更新而非 `save()`：worker 手上的 order 对象可能已落后于并发写入（行锁语义在那两跳落库管道里），退避列自己的写入不该顺带覆盖别的列。"""
    from apps.business.models import Order

    suppress_until = max(
        timezone.now() + timedelta(seconds=COOLDOWN_SECONDS),
        timezone.now() + timedelta(seconds=max(retry_after_seconds, 0)),
    )
    Order.objects.filter(pk=order.pk).update(qr_fetch_suppress_until=suppress_until)


# ── 入队（请求线程内；快、零出站）────────────────────────────────────────────


def _config_gate_passing(config) -> bool:
    """配置行层面的闸（**一份判据三处共用**）：无行 / 关 / 无密钥 / 无基址 / 401 闩 ⇒ False。
    使用方：`_fetch_gate_passing`（web 入队）、`_run_fetch`（worker 复核）、
    查码队列命令的**整租户**预判（`fetch_order_qr_queue`——一次判整租户，判不过零出站）。 ⛔ 别在调用方再抄一份条件——两处判据必然漂移（本仓 E 系教训的形状）。"""
    return not (
        config is None
        or not config.enabled
        or not (config.api_key or "").strip()
        # ⭐ F-1：base_url 空 = 未配置（模型列注释 / RUNBOOK 同一口径），
        # ⛔ 不判它就会放 worker 拿空基址拼相对 URL 必败，每单每 10 分钟刷一行失败台账。
        or not (config.base_url or "").strip()
        or config.auth_failed_at is not None
    )


def _fetch_gate_passing(order) -> bool:
    """配置闸 + 订单闸 + 短路闸（入队与 worker **共用**的同一份判据）。"""
    from apps.business.models import OrderQrFetchConfig

    config = OrderQrFetchConfig.objects.filter(
        tenant_schema=connection.schema_name
    ).first()
    if not _config_gate_passing(config):
        return False
    if order.deleted_at is not None:
        return False
    if OrderScreenshot.objects.filter(
        order_id=order.pk,
        img_type=OrderScreenshot.IMG_GROUP_QR,
        status=OrderScreenshot.STATUS_READY,
    ).exists():
        return False
    return True


def _rate_guard_allows() -> bool:
    """滑窗速率护栏（留死）：60s 内启动数 ≥ `MAX_STARTS_PER_MINUTE` ⇒ False。"""
    now = time.monotonic()
    with _rate_lock:
        while _rate_window and now - _rate_window[0] > _RATE_WINDOW_SECONDS:
            _rate_window.popleft()
        if len(_rate_window) >= MAX_STARTS_PER_MINUTE:
            return False
        _rate_window.append(now)
        return True


_rate_lock = threading.Lock()
_rate_window: deque[float] = deque()
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)


def maybe_enqueue_fetch(order) -> bool:
    """触发点入口：五道闸全过 ⇒ 提交后 spawn 拉取线程。回 True = 排上了。 ⚠️ 必须在**能提交**的上下文里调（业务事务内 / 请求内都行）：`transaction.on_commit`
    在事务内 = 提交后 spawn（回滚不 spawn）；在事务外 = 立即 spawn（detail/list_items 的读路径）。 ⚠️ 冷却在**入队时**就铺（防触发点①②同单连击与并发重复触发）；事务回滚会白铺一次，
    代价是这单 1 分钟内不再自动拉（起冷却 60s）——稀疏人工动作下可接受（如实登记）。"""
    schema = (getattr(connection, "schema_name", "") or "").strip() or "public"
    if not _fetch_gate_passing(order):
        return False
    if cooldown_remaining(schema, order.order_no) > 0:
        return False
    _set_cooldown(schema, order.order_no, COOLDOWN_SECONDS)

    payload = {
        "schema": schema,
        "order_no": order.order_no,
        "source_order_no": order.source_order_no or "",
        "order_pk": order.pk,
    }

    def _start() -> None:
        _spawn(**payload)

    transaction.on_commit(_start)
    return True


def _spawn(*, schema: str, order_no: str, source_order_no: str, order_pk: int) -> bool:
    """护栏（并发槽 + 速率）通过才起 daemon 线程；回 False = 被护栏丢弃。
    ⚠️ 槽在**这里**拿（on_commit 回调时机），不是入队时——事务回滚不会漏放槽位。
    ⚠️ 测试同步点：patch 本模块的 `_spawn` 为直接调 `_run_fetch(...)`（E-0029）。"""
    if not _rate_guard_allows():
        logger.warning("订单群码拉取被速率护栏丢弃 order=%s", order_no)
        return False
    if not _slots.acquire(blocking=False):
        logger.warning("订单群码拉取并发槽已满，丢弃 order=%s", order_no)
        return False
    threading.Thread(
        target=_thread_main,
        kwargs={
            "schema": schema,
            "order_no": order_no,
            "source_order_no": source_order_no,
            "order_pk": order_pk,
        },
        name=f"order-qr-fetch-{order_no}",
        daemon=True,
    ).start()
    return True


def _thread_main(**kwargs) -> None:
    """web 通道的线程目标：槽位在这里归还（它跟**获取方**成对）。 ⚠️ 查码队列（timer）直接调 `_run_fetch_careful`、不走本函数——它逐单同步跑、
    从不占槽，也就没有槽可还（把归还留在 careful 里会让 timer 每单多还一次、必炸）。"""
    try:
        _run_fetch_careful(**kwargs)
    finally:
        _slots.release()


#: `_run_fetch_careful` 的回值哨兵：未预期异常（已记日志；⭐ 起不写退避，
#: 异常单仍在候选里、下一轮自然重试）。查码队列命令据它把该单记入失败明细并**继续本轮**（⭐ 毒单不再中断：
#: 本轮只要出现过就退出码 1）；web 线程忽略回值。
OUTCOME_UNEXPECTED = "unexpected"

#: `_run_fetch` 的回值结局：对端回了 ok 但**落库失败**（下载 / 识别 / 签发 / 直传 /
#: 认领五档之一；H1）。不是异常——不写退避、下一轮自然重试；查码队列
#: 把它记入失败明细、⛔ 不计入 ok；web 线程忽略回值。
OUTCOME_LAND_FAILED = "land_failed"


def _run_fetch_careful(**kwargs) -> str:
    """查码入口的机械兜底：顶层 catch + 连接关闭，异常不出调用方（web 线程 / timer 同用）。未预期异常响亮进日志；⭐ 起不再写持久退避（业务方：失败下一轮自然重试，
    每分钟扫描本身就是节流）——异常单仍在本租户候选里，下一轮重试。回值 = `_run_fetch` 的结局；未预期异常回 `OUTCOME_UNEXPECTED`（不向上抛——
    web 的线程目标没人接异常；timer 命令按回值记账：unexpected ⇒ 该单记失败明细并 **继续本轮**，本轮只要出现过 ⇒ 退出码 1）。"""
    try:
        return _run_fetch(**kwargs)
    except Exception:  # noqa: BLE001 —— 后台线程异常没人接；响亮进日志，⛔ 不炸进程
        logger.exception("订单群码拉取未预期失败 order=%s", kwargs.get("order_no"))
        return OUTCOME_UNEXPECTED
    finally:
        connection.close()


# ── worker（daemon 线程内）──────────────────────────────────────────────────


def _run_fetch(
    *,
    schema: str,
    order_no: str,
    source_order_no: str,
    order_pk: int,
    record_failures: bool = True,
    preverified: tuple | None = None,
) -> str:
    """一次完整的拉取：重建租户上下文 → 复核 → 查码 → 分派落库 / 记账。 ⚠️ 复核（配置三样 + 订单在 + **已有 ready 群码即放弃**）收窄了入队→执行的窗口；复核与落库之间仍有窗口（拉取途中群码被人工传上），落库那两跳各自的既有闸（行锁 / 淘汰 / 唯一部分索引）兜底，最坏是多一次无害覆盖。 ⚠️ `record_failures`（D-0356 两通道分口）：web 默认 True 照旧；**查码队列传 False** ——失败只 logger、不进操作日志页（`ok` 与 401 闩沿不受它影响，照记台账）。
    回值 = 本次结局（`"skipped"` = 闸没过、没碰对端；其余 = `_dispatch` 的结局—— 对端 `kind`，其中 ok 落库失败折成 `OUTCOME_LAND_FAILED`）。查码队列用它拼 journald 摘要行；web 线程忽略回值。 ⭐ **`preverified = (order, config)`（2026-11-03 查码省库批）**：**只有查码队列命令** 传它——候选主查询（未软删 + 24h 窗 + 不在 429 退避期 + 无群码截图行）与整租户
    配置闸**在本轮一开始已批量核过** ⇒ 折掉每单重复的 4 处检查查询（Tenant / 配置 / 订单 / 截图 exists），每问一张的额外库往返趋近于零。行为等价性边界（如实登记）：本轮开始**之后**才发生的变化（人工传码 / 软删 / 配置被关）本轮看不见——软删由落库签发那跳的 `get_object_or_404` 兜成 `skipped`（与复核挡下同结局）、人工传码由落库管道的淘汰 + 唯一部分索引兜底（最坏多一次无害覆盖）、其余下一轮候选查询自然收口。⭐ 租户 schema 由调用方的 `tenant_context` 承载（命令在轮内、连接关闭后重连也会按 wrapper 上的 schema 重设 search_path），⛔ 不再逐单重查 `Tenant`。 web 线程**永远不传**（它没有批量前置，逐项复核是承重的）。"""
    result = None
    if preverified is not None:
        order, config = preverified
        # 复核走**内存对象**（零查询）：本轮开始后的变化下一轮收口（边界见 docstring）。
        if (
            order is None
            or order.deleted_at is not None
            or not _config_gate_passing(config)
        ):
            return "skipped"
        result = fetch_order_qr(
            base_url=config.base_url,
            api_key=config.api_key,
            order_no=order_no,
            source_order_no=source_order_no,
        )
        try:
            return _dispatch(order, config, result, record_failures=record_failures)
        except Http404:
            # 复核与落库之间单被软删（签发那跳 `get_object_or_404`）⇒ 与「复核挡下」
            # 同一结局：静默跳过，不冒充未预期（毒单口径不为本轮的竞态买单）。
            logger.info("订单群码拉取时单已删 order=%s（本轮跳过，下一轮自然出候选）", order_no)
            return "skipped"

    from django_tenants.utils import tenant_context

    from apps.business.models import Order, OrderQrFetchConfig

    tenant = None
    from apps.platform.models import Tenant

    try:
        tenant = Tenant.objects.get(schema_name=schema)
    except Tenant.DoesNotExist:
        logger.warning("订单群码拉取找不到租户 schema=%s（已下线？），放弃", schema)
        return "skipped"
    with tenant_context(tenant):
        config = OrderQrFetchConfig.objects.filter(tenant_schema=schema).first()
        # ⭐ F-1：与 `_fetch_gate_passing` 同一句判据（`_config_gate_passing`
        #    一处实现）——入队到执行有窗口，配置可能被改成「开着开关却无基址」。
        if not _config_gate_passing(config):
            return "skipped"
        order = Order.objects.filter(pk=order_pk, order_no=order_no).first()
        if order is None or order.deleted_at is not None:
            return "skipped"
        # ⭐ S-1：计划 §3.2 承诺的第三道复核——入队→执行窗口内
        # 群码可能被人工传上；已有 ready 群码即放弃，⛔ 不覆盖窗口内的人工上传。
        if OrderScreenshot.objects.filter(
            order_id=order.pk,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        ).exists():
            return "skipped"
        result = fetch_order_qr(
            base_url=config.base_url,
            api_key=config.api_key,
            order_no=order_no,
            source_order_no=source_order_no,
        )
        return _dispatch(order, config, result, record_failures=record_failures)


def _dispatch(
    order, config, result: QrFetchResult, *, record_failures: bool = True
) -> str:
    """按查码结果分派（库内收尾）。回值 = 本次结局（对端 `result.kind`；ok 分支按落库成败折成 `"ok"` / `OUTCOME_LAND_FAILED`——H1：落库失败不再冒充 ok）。
    ⭐ 2026-11-01 业务方口径：**普通失败一律不写持久退避**（每分钟一轮扫描本身就是节流，下一轮自然重试）——唯一例外是对端 429（对端明确给「过 N 秒再来」，听对端的）。
    `ok` 也不写（ready 行已让候选查询自然排除）；401 不写（整租户闩比单单退避更强，清闩后单子立刻回队）。"""
    if result.kind == "ok":
        if _land_group_qr(order, result.qr_url, record_failures=record_failures):
            return "ok"
        return OUTCOME_LAND_FAILED
    if result.kind == "not_found" or result.kind == "generating":
        # 静默态（台账不记，D-0348 同口径）；⭐ 起**不写退避**——每分钟一轮的扫描本身就是节流，下一轮自然重试。timer 通道留一行 journald 摘要
        # （web 通道维持全静默——record_failures=True 不打这行）。 ⭐ 案 C（D-0364）：日志打 **detail**（404 折成 not_found 后 detail 是
        # 「对端系统里没有这张单（host）」，journald 按这句话可辨可数）； 200 态的 not_found / generating 没有 detail ⇒ 回落 kind，旧行不丢形。
        if not record_failures:
            logger.info(
                "订单群码未就绪 order=%s：%s", order.order_no, result.detail or result.kind
            )
        return result.kind
    if result.kind == "rate_limited":
        _set_cooldown(
            connection.schema_name,
            order.order_no,
            min(max(result.retry_after_seconds, COOLDOWN_SECONDS), RATE_LIMIT_COOLDOWN_CAP),
        )
        # 列语义与 cache 那层**取并集**：max(60, retry_after)，不套 cache 的 3600 封顶
        # ——对端给多长就退多久（唯一保留的持久退避；429 是对端的明确指令）。
        _suppress_fetching(order, retry_after_seconds=result.retry_after_seconds)
        return result.kind
    if result.kind == "unauthorized":
        _latch_auth_failed(config, order, result.detail)
        return result.kind
    if result.kind in ("peer_disabled", "bad_param"):
        # 对端没开 / 我方契约 bug：不刷台账，logger 留痕即可；⭐ 不写退避（下一轮自然重试）。
        logger.warning("订单群码拉取未执行 order=%s：%s", order.order_no, result.detail)
        return result.kind
    # peer_error / network：预期失败 ⇒ 台账（按通道口径）+ 冷却（入队已铺）；
    # ⭐ 不写持久退避（每分钟扫描本身就是节流，下一轮自然重试）。
    _record_ledger(
        order,
        reason=f"自动拉取群码失败：{result.detail}",
        ok=False,
        record_failures=record_failures,
    )
    return result.kind


def _latch_auth_failed(config, order, detail: str) -> None:
    """401 闩：置位（沿上唯一）+ 台账恰一次。 ⚠️ 条件更新（`auth_failed_at__isnull=True`）+ `rowcount==1` 才记台账：并发两个 worker
    同时吃 401 时只有一行台账（「告警一次」的落地）。清闩在配置端点的 PATCH 里。"""
    from apps.business.models import OrderQrFetchConfig

    now = timezone.now()
    updated = OrderQrFetchConfig.objects.filter(
        pk=config.pk, auth_failed_at__isnull=True
    ).update(auth_failed_at=now)
    if updated:
        _record_ledger(
            order,
            reason=f"自动拉取群码失败：{detail}；已停止自动拉取，保存任意配置后恢复",
            ok=False,
        )


# ── 落库（ok 态；逐字复用既有截图管道）───────────────────────────────────────


def _qr_fetch_uploader(order):
    """同步行的 `uploaded_by`（NOT NULL + PROTECT）：优先本单客服，否则占位账号「马上来」。
    ⚠️ 占位账号照 `dailiantong_push.placeholder_taker_user` 的守卫：固定 username、
    不可登录（`is_active=False` + unusable password）；本租户若存在**同名且可登录**的真人账号 ⇒ 响亮失败，⛔ 绝不把真人账号停用。"""
    if order.sales_user_id:
        return order.sales_user
    return placeholder_malailai_user()


#: 占位账号 username（固定判据；显示名「马上来」）。
VIRTUAL_MALAILAI_USERNAME = "malailai_qr"
VIRTUAL_MALAILAI_DISPLAY_NAME = "马上来"


def placeholder_malailai_user():
    """本租户的占位账号「马上来」（不可登录、自动创建 / 复用）——逐字照 dailiantong 守卫。"""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=VIRTUAL_MALAILAI_USERNAME,
        defaults={
            "first_name": VIRTUAL_MALAILAI_DISPLAY_NAME,
            "is_active": False,
        },
    )
    if created:
        user.set_unusable_password()
        user.is_active = False
        user.save(update_fields=["password", "is_active"])
        return user
    if user.is_active or user.has_usable_password():
        raise RuntimeError(
            f"本租户已有一个可登录的账号叫「{VIRTUAL_MALAILAI_USERNAME}」，"
            "它不是马上来的占位账号，本系统不能复用它、也不能停用它。"
            "请管理员给那个账号改名或停用，再让拉取重试。"
        )
    # 旧账号显示名不同时就地改准复用（判据只认 username）。
    if user.first_name != VIRTUAL_MALAILAI_DISPLAY_NAME:
        user.first_name = VIRTUAL_MALAILAI_DISPLAY_NAME
        user.save(update_fields=["first_name"])
    return user


def _land_group_qr(order, qr_url: str, *, record_failures: bool = True) -> bool:
    """`ok` 态收尾：下载转存 → 签发 + 直传 + 认领（既有管道，⛔ 不绕行）。回 `True` = ready 群码行落成；五档失败各自记账后回 `False`（H1：调用方据此折成
    `land_failed` 结局——落库失败不再冒充 ok）。台账与 logger 分口照旧：失败进不进操作日志页仍由 `record_failures` 决定。
    ⚠️ 落库失败的每一支都**只记账、不写持久退避**（⭐ 2026-11-01 业务方「撤退避」口径）：没落成 ready 行 = 这单仍在队列里，下一轮自然重试——每轮全量逐个问本身就是节流。"""
    from apps.core.storage import (
        IMAGE_EXT_BY_CONTENT_TYPE,
        get_storage,
        sniff_image_bytes,
    )
    from apps.business.api.screenshot_views import (
        claim_screenshot,
        issue_screenshot_ticket,
    )

    try:
        data = _download_image(qr_url)
    except QrFetchError as exc:
        _record_ledger(order, reason=f"自动拉取群码失败：{exc}", ok=False, record_failures=record_failures)
        return False
    content_type = sniff_image_bytes(data)
    if content_type is None:
        _record_ledger(order, reason="自动拉取群码失败：图片类型无法识别（只支持 jpg / png）", ok=False, record_failures=record_failures)
        return False
    ext = IMAGE_EXT_BY_CONTENT_TYPE[content_type]
    uploader = _qr_fetch_uploader(order)

    ticket = issue_screenshot_ticket(
        order_no=order.order_no,
        ext=ext,
        img_type=OrderScreenshot.IMG_GROUP_QR,
        uploaded_by=uploader,
    )
    if not ticket.ok or ticket.data is None or ticket.row is None:
        _record_ledger(
            order,
            reason=f"自动拉取群码失败：签发上传凭证被拒（{ticket.msg or ticket.code}）",
            ok=False,
            record_failures=record_failures,
        )
        return False
    object_key = str(ticket.data.get("key") or "")
    try:
        get_storage().put_bytes(object_key, data, content_type=content_type)
    except Exception as exc:  # noqa: BLE001 —— 字节没传上去：删掉 pending 行，对象留给 24h 清理
        ticket.row.delete()
        logger.warning(
            "订单群码直传失败 order=%s key=%s（%s）", order.order_no, object_key, type(exc).__name__
        )
        _record_ledger(order, reason=f"自动拉取群码失败：图片上传存储失败（{type(exc).__name__}）", ok=False, record_failures=record_failures)
        return False
    outcome = claim_screenshot(
        order_no=order.order_no,
        uploaded_by=uploader,
        screenshot_id=ticket.row.pk,
        original_name=f"malailai-qr-{order.order_no}.{ext}",
        remark="马上来自动拉取群码"[: OrderScreenshot._meta.get_field("remark").max_length],
    )
    if not outcome.ok:
        # 认领的四档拒收自己删行删对象（既有语义）；这里只记账。
        _record_ledger(order, reason=f"自动拉取群码失败：认领被拒（{outcome.msg or outcome.code}）", ok=False, record_failures=record_failures)
        return False
    _record_ledger(order, reason=LEDGER_OK_REASON, ok=True)
    return True


def _record_ledger(order, *, reason: str, ok: bool, record_failures: bool = True) -> None:
    """写一条机器台账（`order.malailai_qr_fetch`）。 ⚠️ `record_failures=False`（查码队列通道）：失败**不进操作日志页**、只 logger
    （journald 可查）；成功与 401 闩沿不受它影响、照记。 ⚠️ 失败走 `logger.warning`（H1）：本项目没配 LOGGING，INFO 到不了
    journald ⇒ 落库系统性坏掉时每轮「ok N」全绿零信号；WARNING 起才可见。 ⛔ reason 不含密钥 / qr_url 全文。"""
    if not ok and not record_failures:
        logger.warning("订单群码拉取失败（队列通道不记台账）order=%s：%s", order.order_no, reason)
        return
    from apps.audit.actions import ACTION_LOG_TYPES
    from apps.audit.log import record

    record(
        log_type=ACTION_LOG_TYPES[LEDGER_ACTION],
        action=LEDGER_ACTION,
        actor=LEDGER_ACTOR,
        actor_id=None,
        system=True,
        target_kind="order",
        target_id=str(order.pk),
        target_name=order.order_no,
        order_no=order.order_no,
        source_order_no=order.source_order_no or "",
        reason=reason,
    )
    if not ok:
        logger.warning("订单群码拉取失败 order=%s：%s", order.order_no, reason)


# ── 台账口径：**只写「查到」那一行** ────────────────────
# D-0361 曾在关键时刻落 `qr.fetch_started` / `qr.fetch_gaveup` 两个台账 kind（首查 / 24h 关窗）；每分钟全量轮询的形态下它们是纯噪声（一轮几百条、页面上只能显示提示语）⇒ 业务方拍板停写：查码在台账只留**查到**时的既有成功行
# （`_land_group_qr` 成功臂的 `order.malailai_qr_fetch` ok 行，本来就有）；失败维持只进 journald（`record_failures=False` 的既有口径），排障走队列命令的逐轮摘要与失败明细行（stdout 必达）。消费面（audit/timeline 查码节）只显示查到行。

#: 查码成功行的人话（写面与读面共用这一个常量，⛔ 不许两处各写一份）。
LEDGER_OK_REASON = QR_FETCH_SUCCESS_REASON
