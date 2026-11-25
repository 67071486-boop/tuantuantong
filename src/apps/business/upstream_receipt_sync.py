"""「上家收款同步」（D-0359，计划 `docs/plans/2026-11-01-upstream-receipt-sync.md`）。 ## 它做什么已完成 + 未收款的订单，逐单去问**上家**「这单结没结账」，结了就把我们的 `Order.payment_status` 翻成「已结款」。三条上家通道： - **闲管家（xgj）**：现成的 `run_xgj_receipt_sync`（**零 diff**，本模块只 import 转调）。 ⚠️ 只在**手动按钮**里跑（其候选口径不筛「已完成」，接进夜间违背拍板③——交业务方复核）。 - **鸡蛋铺①式（账号密码，`JidanpuUpstreamBinding.mode=PASSWORD`）**：拿绑定账号登录上家，逐单打 `/EmpAjax/GetMyEmp`（「我的订单收入」，接单视角）问行上的 `settingSta`。 - **团团通上家（peer，`PeerUpstreamBinding`）**：拿绑定子账号 + 查询密码登录**对端网页 API** （csrf → login → `GET /api/business/orders/?order_nos=<键>`），读对端行的 `payment_status`。 ⛔ 不动 peer 机器协议（业务方拍板②）。 - **②式（官方 API）**：回包无结账字段 ⇒ **结构性排除在候选外**（拍板①，永久）。 ## ①式「已结账」判据与取数口（换源轮 深夜实测定案，⛔ 不实测不接线的证据块） - **取数口 = `/EmpAjax/GetMyEmp`（「我的订单收入」，接单视角）**；⛔ 不再逐单打 GetMyOrderList（`jidanpu_upstream.fetch_my_orders`）——上线手动实跑 ①式 settled=0 + 业务方提供已知已结算单单题实证：**该接口（接单视角）行内没有结算字段**（已知已结算单实测 `isSettStatus=None`）。⇒ 旧判据 `isSettStatus=="已结算"` + `recSettCount>0` 是 **店铺端成立、接单视角实证不存在**（2026-11-01 已知已结算单实证；Task 0a 探针当时只见未结样本，正样本残留由此解释）。该原语本身仍服务上家同步等其它功能，原语文件不动。 - **判据 = `str(row.get("settingSta") or "").strip().upper() == "Y"`**（`settingSta` 是 **代码值**：`N` 未结账 / `Y` 已结账；N / 空 / 未知 ⇒ 未结——方向安全，不误翻状态）。 `settingTime`（形如 `"MM-DD HH:MM"`）= **上游结算时间**，随留痕落档；`netReceipts` 在本端点上语义存疑 ⇒ **只留档、⛔ 不判据**。 - **回包是裸 JSON 数组**（不是 `{code,data}` 信封）⇒ 用共享同一会话的 `unwrap="status"` 视图客户端调它（照 `_viewpic_client` 先例：换 config 一格 + 共享 auth/registry/http；默认 `unwrap="auto"` 会把裸数组判成「缺业务码」抛 `ApiError` ⇒ 每单都成 error）＋**形状守卫**：回包非 list ⇒ 结局 `error`
（⛔ 不当空数组——「形状不对」与「查无」是两档结局）。 - **必带全过滤参数**：`gameName="全部游戏" / stime / etime / dateType="10" / settingSta="" / orderSta="" / key / key2="" / page=1 / limit=50`——缺任一 ⇒ 对端 **静默 0 行**（鸡蛋铺老毛病同款）。测试网有「全参数断言」钉住这份键表。 - **stime / etime 是服务端先过滤后匹配**：窗口必须包含订单的**录单时间**（`created_at`），否则命中单也会 0 行 ⇒ `stime = 订单 created_at − 7 天`、`etime = 查询当刻 + 1 天` （上游认它自己墙钟 Asia/Shanghai，格式同 `jidanpu_upstream.my_orders_window` 口径）。 - **键域只有 `key=<orderId>`**：单查 rows=1 精确命中；`key2`（orderIdView 域）恒 0 行、部分店行内 `orderIdView` 干脆为空 ⇒ **source_order_no 兜底键域在该接口已证伪** ⇒ 无 link / link 缺 `upstream_order_id` 的单一律新结局 `skipped_no_link` 计数跳过（⛔ 不再烧放弃数——不是「查无」，是「查不了」）。命中行仍**逐字回校验** `orderId`（出处：jidanpu_upstream.py:617,626）（⛔ 不拿 count==1 当判据）。 - **旧「已退单二次问法」在本通道退役**：GetMyEmp 无此问法 ⇒ `not_found` 即 `not_found` （「查无」照旧烧放弃数——收敛语义不变）。 - **证据**（2026-11-01 深夜真机只读探针，在主检出执行；租户名 / 真实单号一律占位，原始记录只在 `.tools/recon/`，不进 git）：已知已结算单在 GetMyOrderList 行内 `isSettStatus=None`，同一单 GetMyEmp 行内 `settingSta="Y"` + `settingTime="10-31 14:04"` 清晰可用；`key` 传上家内部单号单查 rows=1 精确命中、`key2` 恒 0 行；缺任一过滤参数 ⇒ 静默 0 行；`settingSta=Y` 服务端过滤可用（窗口内 25 张已结账）。 - **残差（S1）——会话过期的第二形态**：会话过期主形态是 302 跳登录页，传输层原样抛 `SessionExpiredError`、`call` 内自动重登重试一次（vendored 既有路径）； **2xx JSON 壳「请先登录」形态**在 `unwrap="status"` 下**会被当数据**漏进形状守卫 ⇒ 守卫按 vendored 传输层的既有提示词表（`_SESSION_EXPIRED_HINTS`，⛔ 不自编文案）认出后**清共享会话**，下一发尝试经 `ensure_login` 走既有重登路径重问（≤3 次尝试内自愈，有专项用例钉住）；重登也不成 ⇒ 按普通网络失败烧完 ⇒ `error` 计数、摘要可见、下轮自愈。 ## 口径（判据全部钉死，逐条对应计划 §2.2/§2.3） - 候选基条件：`status=COMPLETED` 且 `payment_status=UNSETTLED`（「结算中」不扫）、未软删
（默认 manager）；**夜间**按 `completed_at ≥ now − 90d` 收；**手动按钮**按按钮的录单日期窗收（不做 90 天截断、**不看放弃计数**——显式动作优先）；老单优先（`completed_at` asc）。 - **enabled 开关只管订单拉取（业务方拍板：主铺绑定（真实店名不进 git，见 .tools/recon 探针记录）保持停用但其历史订单结算状态要同步）**：①式候选**不筛** `enabled=True`——收款同步是只读动作（GetMyEmp 只查结算状态，不拉单 / 不建卡 / 不认领）⇒ 停用的①式绑定（凭据完整）也参与。⛔ `jidanpu_upstream` 侧的订单同步 enabled 闸**零改动**；peer 通道候选条件不动（peer 绑定有自己的 enabled / 凭据 / 预检闸）。 - **单向闩**：只有 `SETTLED` 一个方向会写；上游未结 / 已退单 / 查无 / 出错一律不动状态。每单 fresh 重读后仍 COMPLETED+UNSETTLED 才在同一个 `transaction.atomic()` 里走唯一写口 `change_payment_status`（照 `xgj_receipt_sync.py:368-373` 形状）。 - **①式查无（换源轮）**：GetMyEmp 0 行 ⇒ 结局 `not_found`、照旧烧放弃数（①式没有静默闸）；旧「补 `order_sta=已退单` 第二查」的分流随换源**退役**（GetMyEmp 无此问法）。 - **peer「对端查无」⇒ `Order.receipt_sync_notfound_count` +1**；计数 ≥ `RECEIPT_SYNC_NOTFOUND_GIVEUP`（默认 3）⇒ **夜间**候选不再收该单（列不清零、无清零写入口，到期语义由手动按钮兜底）。⚠️ **放弃计数只在绑定通过 Task 0b 五项预检（`receipt_query_precheck_ok=True`）后启用**：对端读路径有游戏作用域 / `restrict_to_self_created` 两个**静默回空**闸（`order_views.py:868` / `order_flags.py:68`），未过预检的绑定查无可能是「看不见」而不是「没有」⇒ 只留痕不计数（评审 M3）。 - **peer 键域**：查询键 = `peer_order_no`，为空退 `origin_order_no`；两者皆空 ⇒ `skipped_no_key` 跳过留痕、不烧放弃计数。对端 `?order_nos=` 按「20 位纯数字 ⇒ `order_no__in`，其余 ⇒ `source_order_no__in`」分流（对端 `selectors.py:418-436`）； ⛔ 我方 `order.source_order_no` 对端库里没有，**不做兜底键**（S7）。 **回包多于 1 行（对端同来源号多单）⇒ 结局 ambiguous：不翻状态、留痕行数**（Task 0b 实测： 19 位键走对端 `source_order_no__in`，可能多单）。 - **peer 绑定失败闩**：登录失败 / 查单 401/403 ⇒ 结局 `bind_error`（detail 区分「凭据错」/ 「登录限流 1038」/「app 来源账号 1034」/「对端拒绝」）+ **该绑定本轮剩余候选全跳过** （只闩本轮，照查码线 401 整租户闩的降级形状）。 - **http 形态的 peer 绑定结构性排除**（生产 session/CSRF cookie 带 Secure， `settings.py:336`；http 上不回传 ⇒ 整链必然 bind_error）+ 摘要计数。 - **留痕（拍板⑦）**：每问一次写一条 `order.upstream_receipt_query` 机器行（detail = 通道 / 绑定 / 结局 / 上游原始字段 JSON）；翻状态由 `change_payment_status` 自己的动作码留痕，不重复记。 - **频控与预算（拍板⑨）**：①式 / peer 相邻两单垫 `RECEIPT_SYNC_INTERVAL_SECONDS` （env float，默认 1.5s；≤0 / NaN 回落 1.5 + warning，⛔ 不静默失效）；每通道每轮 `MAX_ORDERS_PER_RUN=5000` + `TIME_BUDGET=3600s`；网络类失败总尝试 ≤3 次后跳过并留痕。 - **锁**：编排器沿用 `xgj_receipt_sync.SYNC_LOCK_NAMESPACE`（0x58474A52）——与旧按钮 / 在跑实例互斥。**重入已核定案**：`_sync_guard` 用线程本地 `connection`，编排器同线程持锁后转调 `run_xgj_receipt_sync`（其内部再拿一次 = 同会话计数重入、其 finally 只放一层），安全。 - **租户闸（Task 7）**：`ReceiptSyncConfig.enabled`（无行 = get_or_create = 启用）只闸 **夜间命令**；手动按钮是显式动作、不受租户闸挡。 ## 触发面 - **夜间**：`manage.py sync_upstream_receipts`（自己枚举本机租户）+ timer `tuantuantong-receipt-sync.timer`（02:00±5min）。跑 **①式 + peer** 两通道。 - **手动**：订单页「同步收款」按钮（`order_views.py::xgj_receipt_sync` action）—— 线程体换成本模块，**手动含闲管家通道**（转调 `run_xgj_receipt_sync` 原样）。"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db import connection, connections, transaction
from django.db.models import F
from django.db.models.functions import Trim
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu, xgj_push, xgj_receipt_sync
from apps.business.models import (
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    PaymentStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    ReceiptSyncConfig,
)
from apps.business.order_writes import change_payment_status
from apps.core.error_codes import ErrorCode
from jidanpu.client import JidanpuClient
from jidanpu.errors import ApiError, ConfigError, JidanpuError, SessionExpiredError
from jidanpu.transport import _SESSION_EXPIRED_HINTS, _message_of

logger = logging.getLogger(__name__)

# ── 常量（频控 / 预算 / 判据）───────────────────────────────────────────────

#: 相邻两次上游查询的默认间隔（秒；拍板⑨：1–2 秒防鸡蛋铺限流，默认 1.5）。
#: 可被 `settings.RECEIPT_SYNC_INTERVAL_SECONDS`（env 同名）覆盖。
DEFAULT_INTERVAL_SECONDS = 1.5

#: 「查无」放弃阈值（夜间）：连续查无达到它 ⇒ 夜间候选不再收该单。
DEFAULT_NOTFOUND_GIVEUP = 3

#: 夜间完成时间窗（天）：只扫近 90 天完成的单（拍板⑤）。
DEFAULT_WINDOW_DAYS = 90

#: 每通道每轮最多处理多少单（照 `xgj_receipt_sync.MAX_ORDERS_PER_RUN`）。
MAX_ORDERS_PER_RUN = 5000

#: 每通道总时长预算（秒；到点停止扫描，已处理的都已落库，照 xgj 先例）。
TIME_BUDGET = 3600.0

#: 网络类失败的总尝试上限（含第一次；D-0358 同款「总尝试 3 次」语义）。
NETWORK_MAX_ATTEMPTS = 3

#: ①式第二查的 `orderSta` 筛选值常量已随换源退役（GetMyEmp 无「已退单」问法）。

#: ①式取数口（换源轮）：「我的订单收入」（接单视角）。⛔ 不再逐单打
#: GetMyOrderList（`jidanpu_upstream.fetch_my_orders`）——真机实证其行内无结算字段。
JIDANPU_MY_EMP_PATH = "/EmpAjax/GetMyEmp"

#: ①式「已结账」的 `settingSta` **代码值**（换源轮实探；前端词表：N 未结账 / Y 已结账）。
JIDANPU_SETTLED_SETTING_STA = "Y"

#: GetMyEmp 查询窗：`stime = 订单 created_at − 7 天`（服务端先按窗过滤再配 key ⇒
#: 窗必须罩住录单时间，否则命中单也静默 0 行）；`etime = 查询当刻 + 1 天`。
JIDANPU_INCOME_WINDOW_BACK_DAYS = 7
JIDANPU_INCOME_WINDOW_FORWARD_DAYS = 1

#: stime / etime 的格式（照 `jidanpu_upstream.my_orders_window` 同一条实测口径：上游认
#: 它自己墙钟 Asia/Shanghai 的 `%Y-%m-%d %H:%M:%S`）。
JIDANPU_INCOME_WINDOW_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 留痕动作码（计划 §2.4）：登记在 `apps/audit/actions.py` 三处 + 测试网计数钉。
QUERY_ACTION = "order.upstream_receipt_query"

#: 留痕 `target_kind`（⛔ ≤16 字符，超了会被 `record()` 静默截断）。
TARGET_KIND = "order"

# ── 结局枚举（留痕 detail 与摘要计数的统一词表）─────────────────────────────

OUTCOME_SETTLED = "settled"                  # 上游判已结 ⇒ 翻 SETTLED
OUTCOME_NOT_SETTLED = "not_settled"          # 上游有行、判未结 ⇒ 零写
OUTCOME_NOT_FOUND = "not_found"              # 查无（含 peer 对端查无）⇒ 计放弃数
OUTCOME_AMBIGUOUS = "ambiguous"              # peer 回包多于 1 行 ⇒ 不翻状态、留痕行数
OUTCOME_BIND_ERROR = "bind_error"            # 凭据 / 登录限流 / 401/403 ⇒ peer 闩本轮
OUTCOME_ERROR = "error"                      # 网络类失败重试烧完 ⇒ 零写
OUTCOME_SKIPPED_NO_KEY = "skipped_no_key"    # 无查询键 ⇒ 跳过、不烧放弃数
OUTCOME_SKIPPED_NO_LINK = "skipped_no_link"  # 无 link / 键域已证伪查不了 ⇒ 跳过、不烧放弃数
OUTCOME_PRECHECK_FAILED = "precheck_failed"  # 处理时绑定已不满足候选闸（竞态）⇒ 跳过

#: 时钟与 sleep 的模块级出口（照 `xgj_receipt_sync._monotonic/_sleep`：
#: 用例注入后断言间隔；⛔ 不在函数里直接 `time.sleep`）。
_monotonic = time.monotonic
_sleep = time.sleep
#: 查询窗 `etime` 用的「当前时刻」出口（用例注入后断言窗口）。
_now = timezone.now

#: peer 网页 API 的三条路径与 CSRF 键名（对端 = 同一套代码，`core/urls.py:32-35`、
#: `frontend/src/api/http.ts` 的 `X-CSRFToken` 同款）。
PEER_CSRF_PATH = "/api/auth/csrf"
PEER_LOGIN_PATH = "/api/auth/login"
PEER_ORDERS_PATH = "/api/business/orders/"
PEER_CSRF_COOKIE = "csrftoken"
PEER_CSRF_HEADER = "X-CSRFToken"

#: peer 单次 HTTP 超时（socket 级；总时长由 TIME_BUDGET 在每单之间兜）。
PEER_QUERY_TIMEOUT = 10.0


# ── 配置读取 ────────────────────────────────────────────────────────────────


def _interval_seconds() -> float:
    """限速间隔（秒）。≤0 / NaN 回落默认值 + warning（⛔ 节流不许静默失效，照
    `XGJ_SHIP_MIN_INTERVAL` 先例）。"""
    raw = getattr(settings, "RECEIPT_SYNC_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(DEFAULT_INTERVAL_SECONDS)
    # `not (value > 0)` 同时接住 ≤0 与 NaN（NaN 与任何数比较都是 False）。
    if not value > 0:
        logger.warning(
            "RECEIPT_SYNC_INTERVAL_SECONDS 配成 %r（≤0 或非数）⇒ 回落默认 %.1fs",
            raw, DEFAULT_INTERVAL_SECONDS,
        )
        return float(DEFAULT_INTERVAL_SECONDS)
    return value


def _notfound_giveup() -> int:
    """放弃阈值；非法配置回落默认（⛔ 不因配置写坏就把放弃机制关掉）。"""
    raw = getattr(settings, "RECEIPT_SYNC_NOTFOUND_GIVEUP", DEFAULT_NOTFOUND_GIVEUP)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NOTFOUND_GIVEUP
    return value if value > 0 else DEFAULT_NOTFOUND_GIVEUP


def _window_days() -> int:
    """夜间完成时间窗（天）；非法配置回落默认。"""
    raw = getattr(settings, "RECEIPT_SYNC_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS
    return value if value > 0 else DEFAULT_WINDOW_DAYS


def receipt_sync_config() -> ReceiptSyncConfig:
    """本租户的配置行（**读侧唯一口径**：无行 = get_or_create 补行 = 视为启用）。
    ⚠️ 只在**租户上下文**里调（`tenant_schema` 取自 `connection.schema_name`）；
    平台 public 平面没有租户语义，⛔ 不要在那里调它。"""
    config, _created = ReceiptSyncConfig.objects.get_or_create(
        tenant_schema=connection.schema_name
    )
    return config


def receipt_sync_enabled() -> bool:
    """租户闸（夜间命令用）：`enabled=False` ⇒ 整租户零上游调用。"""
    return bool(receipt_sync_config().enabled)


# ── 节流与运行态 ────────────────────────────────────────────────────────────


def _pace(next_call_at: float | None, interval: float) -> float:
    """限速：距上一次上游调用不足 `interval` 就补睡；返回下一次的允许时刻。"""
    now = _monotonic()
    if next_call_at is not None:
        delay = next_call_at - now
        if delay > 0:
            _sleep(delay)
    return _monotonic() + interval


class _RunState:
    """一个通道循环的运行态：摘要计数 + 截止时刻 + 限速游标。"""

    def __init__(self, summary: dict, *, deadline: float, interval: float) -> None:
        self.summary = summary
        self.deadline = deadline
        self.interval = interval
        self.next_call_at: float | None = None
        self.processed = 0

    def pace(self) -> None:
        """每次上游调用前垫间隔（重试也算调用，一样垫）。"""
        self.next_call_at = _pace(self.next_call_at, self.interval)

    def over_budget(self) -> bool:
        return _monotonic() >= self.deadline

    def over_cap(self) -> bool:
        return self.processed >= MAX_ORDERS_PER_RUN

    def bump(self, key: str, amount: int = 1) -> None:
        self.summary[key] = self.summary.get(key, 0) + amount


def _new_channel_summary() -> dict:
    """一个通道一轮的计数（两通道共用一份键表，只用于日志与用例断言）。"""
    return {
        "scanned": 0,
        "queried": 0,
        "settled": 0,
        "not_settled": 0,
        "not_found": 0,
        "not_found_uncounted": 0,
        "ambiguous": 0,
        "bind_error": 0,
        "error": 0,
        "skipped_no_key": 0,
        "skipped_no_link": 0,
        "precheck_failed": 0,
        "vanished": 0,
        "stale_skip": 0,
        "bind_latched_skip": 0,
        "deadline_hit": False,
        "cap_hit": False,
        "excluded_openapi_bindings": 0,
        "excluded_no_credentials": 0,
        "excluded_http_bindings": 0,
        "excluded_no_password": 0,
        "excluded_bad_base_url": 0,
        "unexpected_error": False,
    }


# ── 留痕（计划 §2.4）───────────────────────────────────────────────────────


def _binding_host(base_url: str) -> str:
    """审计句子里显示的绑定名 = 域名（去协议头）。⛔ 不用账号 / 密码（凭据红线）。"""
    host = base_url or ""
    for prefix in ("https://", "http://"):
        host = host.removeprefix(prefix)
    return host.rstrip("/")


def _write_query_log(
    order, *, channel: str, binding_label: str, outcome: str,
    upstream: dict | None = None, extra: str = "",
) -> None:
    """每问一次写一条机器行（照 `xgj_push._write_receipt_log` 先例；detail 进 `reason`）。"""
    detail = f"通道={channel} 绑定={binding_label} 结局={outcome}"
    if extra:
        detail += f" {extra}"
    if upstream:
        # 键排序 + 不转义中文：同一行的留痕稳定可 diff。⛔ 列宽 255 由 record() 截断兜底。
        detail += " 上游=" + json.dumps(upstream, ensure_ascii=False, sort_keys=True)
    record(
        log_type=ACTION_LOG_TYPES[QUERY_ACTION],
        action=QUERY_ACTION,
        actor=xgj_push.SYSTEM_ACTOR,
        system=True,
        target_kind=TARGET_KIND,
        target_id=order.order_no,
        order_no=order.order_no,
        reason=detail,
    )


# ── 通用小件（两通道共用）──────────────────────────────────────────────────


def _fresh_order(order_pk: int, *, lock: bool = False):
    """写前重读，已软删单回 None。"""
    orders = Order.objects.filter(pk=order_pk)
    if lock:
        orders = orders.select_for_update()
    return orders.first()


def _still_pending(fresh) -> bool:
    """fresh 重读后是否仍是「已完成 + 未收款」（单向闩的写前复核）。"""
    return (
        fresh is not None
        and fresh.status == OrderStatus.COMPLETED
        and fresh.payment_status == PaymentStatus.UNSETTLED
    )


def _bump_notfound(order_pk: int) -> None:
    """放弃计数 +1（原子 UPDATE；查到行 / 未结 / 出错都不动它）。"""
    Order.objects.filter(pk=order_pk).update(
        receipt_sync_notfound_count=F("receipt_sync_notfound_count") + 1
    )


def jidanpu_row_settled(row) -> bool:
    """①式行 → 是否「已结账」（判据唯一出处；换源轮实测证据见模块 docstring）。 `settingSta` 是**代码值**（`"N"` / `"Y"`；前端词表映射 未结账/已结账）：
    Y（含带空白的形态）⇒ 已结；N / 空 / 缺列 / 未知值 ⇒ 未结——方向安全（不误翻状态）。
    ⛔ `netReceipts` 在本端点上语义存疑，不是信号、不参与判定。"""
    if not isinstance(row, dict):
        return False
    return str(row.get("settingSta") or "").strip().upper() == JIDANPU_SETTLED_SETTING_STA


def _match_row(rows, *, key: str, key_field: str):
    """按键域选定的比较列**逐字**回校验（⛔ 不拿 count==1 当判据，见模块 docstring）。
    ⛔ 服务端 `key` 参数在 GetMyEmp 上的**精确**匹配语义未实测穷尽（可能同 key1 一样
    带模糊面）⇒ 命中集里可能有别的单，必须逐字回校验——比较列拿错 / 不回校验都会把别人的单当这一单。"""
    if not isinstance(rows, (list, tuple)):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get(key_field) or "") == key:
            return row
    return None


#: ①式留痕要存的**上游原始字段**（换源轮 实测的四件；⛔ 不存整行——行里有
#: 支付宝账号等无关隐私）。`settingTime` = 上游结算时间（白送的留档增强）；
#: `netReceipts` 语义存疑 ⇒ 只留档不判据。
_JIDANPU_UPSTREAM_KEYS = ("settingSta", "settingTime", "orderSta", "netReceipts")


def _jidanpu_upstream_payload(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    return {key: row.get(key) for key in _JIDANPU_UPSTREAM_KEYS if key in row}


# ── 基础候选条件（两通道共用；计划 §2.2）───────────────────────────────────


def _nightly_order_filters(*, now: datetime) -> dict:
    """夜间口径：已完成 + 未收款 + 近 N 天完成 + 放弃计数未烧穿。"""
    return {
        "status": OrderStatus.COMPLETED,
        "payment_status": PaymentStatus.UNSETTLED,
        "completed_at__gte": now - timedelta(days=_window_days()),
        "receipt_sync_notfound_count__lt": _notfound_giveup(),
    }


def _manual_order_filters(*, date_from: date, date_to: date) -> dict:
    """手动口径：按钮的录单日期窗（半开区间）；**不做** 90 天截断、**不看**放弃计数。"""
    start, end = xgj_receipt_sync.window_bounds(date_from, date_to)
    return {
        "status": OrderStatus.COMPLETED,
        "payment_status": PaymentStatus.UNSETTLED,
        "created_at__gte": start,
        "created_at__lt": end,
    }


def _settle_it(fresh, *, channel: str, label: str, outcome: str, upstream: dict | None) -> None:
    """翻 SETTLED + 本问的留痕行（同一个事务；照 `xgj_receipt_sync.py:368-373` 形状）。"""
    with transaction.atomic():
        change_payment_status(
            fresh,
            payment_status=PaymentStatus.SETTLED,
            ctx=xgj_push._MACHINE_CTX,
            dry_run=False,
        )
        _write_query_log(
            fresh, channel=channel, binding_label=label, outcome=outcome,
            upstream=upstream,
        )


# ── ①式通道（鸡蛋铺账号密码）──────────────────────────────────────────────


#: 「老单优先」全局排序里 `completed_at` 为 NULL 的单的哨兵（Python 元组排序里
#: `None` 不能与 aware datetime 比较 ⇒ 用 (is_none, 哨兵, pk) 让它排最后，同库
#: `NULLS LAST` 口径）。⚠️ 用标准库 `dt_timezone`——`django.utils.timezone.utc` 在 Django 5 已移除。
_TIME_SENTINEL = datetime.max.replace(tzinfo=dt_timezone.utc)


@dataclass(frozen=True)
class _JidanpuJob:
    """①式的一单：查询键 = `link.upstream_order_id`（GetMyEmp 键域只有 `key=<orderId>`，
    换源轮实测 `key2`/orderIdView 域恒 0 行 ⇒ 不再有「键域决定比较列」这一层）。"""

    order_pk: int
    link_pk: int
    binding_pk: int
    key: str                 # GetMyEmp 的 `key`（上家平台内部号域，行比较列恒 `orderId`）
    #: 全局排序键（S1）：主路径与兜底两段各自 asc 不等于归并后 asc ⇒ 收集完对整个
    #: jobs 列表按它一次性 sorted（None 的 completed_at 排最后）。
    sort_key: tuple


def _jidanpu_credential_bindings(summary: dict) -> list[JidanpuUpstreamBinding]:
    """本租户「①式 + 凭据完整」的绑定（候选主路径与兜底的共同前置）。 ⭐ **拍板修订（业务方：主铺绑定（真实店名不进 git，见 .tools/recon 探针记录）保持停用但其历史订单结算状态要同步）**： **`enabled` 开关只管订单拉取**（`jidanpu_upstream` 侧的闸原样，⛔ 本模块不碰）；
    **收款同步是只读动作**（GetMyEmp 只查结算状态，不拉单、不建卡、不认领） ⇒ **停用的①式绑定也参与**——本函数不再筛 `enabled=True`，只保留两闸： - `mode=OPENAPI` 一律不收（拍板①：回包无结账字段）⇒ 摘要给排除计数；
    - 凭据不全（地址 / 账号 / 密码缺一）⇒ 排除 + 计数（判据唯一出处 `jidanpu.credentials_of`）。 ⚠️ peer 通道的候选条件**不动**（peer 绑定有自己的 enabled / 凭据 / 预检闸，见 `_peer_ready_bindings`）。"""
    candidates = list(JidanpuUpstreamBinding.objects.order_by("pk"))
    summary["excluded_openapi_bindings"] = sum(
        1 for binding in candidates
        if binding.mode == JidanpuUpstreamBinding.Mode.OPENAPI
    )
    ready: list[JidanpuUpstreamBinding] = []
    for binding in candidates:
        if binding.mode != JidanpuUpstreamBinding.Mode.PASSWORD:
            continue
        if jidanpu.credentials_of(binding) is None:
            summary["excluded_no_credentials"] += 1
            continue
        ready.append(binding)
    return ready


def _jidanpu_link_jobs(
    summary: dict, *, binding_pks: list[int], order_filters: dict
) -> tuple[list[_JidanpuJob], set[int]]:
    """主路径：`JidanpuUpstreamOrderLink`（order × 启用①式绑定 × upstream_order_id）。
    键域 = `upstream_order_id` ⇒ 行比较列恒 `orderId`；同单多条 link 挑**第一条带键**的
    （S3：键空 link 不定罪，等后续 link 救回），键全空 ⇒ 该单计一次 `skipped_no_link` 跳过（换源轮起 `key2` 兜底已证伪）。"""
    links = (
        JidanpuUpstreamOrderLink.objects.filter(binding_id__in=binding_pks)
        .filter(order__status=order_filters["status"])
        .filter(order__payment_status=order_filters["payment_status"])
        # ⚠️ join 侧**没有**默认 manager 的软删过滤（那是 `Order.objects` 基集才有的）
        #    ⇒ 显式补一道（已软删的单不再是「待收款」，别让它烧扫描预算）。
        .filter(order__deleted_at__isnull=True)
    )
    if "completed_at__gte" in order_filters:
        links = links.filter(order__completed_at__gte=order_filters["completed_at__gte"])
        links = links.filter(
            order__receipt_sync_notfound_count__lt=order_filters[
                "receipt_sync_notfound_count__lt"
            ]
        )
    else:
        links = links.filter(order__created_at__gte=order_filters["created_at__gte"])
        links = links.filter(order__created_at__lt=order_filters["created_at__lt"])
    links = links.select_related("order").order_by("order__completed_at", "order__id")

    jobs: list[_JidanpuJob] = []
    linked_pks: set[int] = set()     # 见过 link 的单全集（键空的也算有 link ⇒ 兜底排除面照旧）
    has_key_pks: set[int] = set()    # 已收到带键 link 的单（同单只收第一条带键的，S2）
    no_key_pks: set[int] = set()     # 见过 link 但**至今**没有带键 link 的单
    for link in links:
        order = link.order
        key = (link.upstream_order_id or "").strip()
        linked_pks.add(order.pk)
        if not key:
            # ⭐ 换源轮：GetMyEmp 键域只有 `key=<orderId>`，`key2` （source_order_no 的 orderIdView 域）恒 0 行 ⇒ 缺 upstream_order_id 的
            # link 查不了（旧形状退 source_order_no 兜底查询，已证伪）。 ⭐ S3：**先别定罪**——同单后续 link 可能带键（罕见：先建了空键 link、又补了正确绑定）⇒ 等完整条 links 才对「键全空」的
            # 单计**一次** `skipped_no_link`（⛔ 旧形状首条一锤定音，会把可查的单误计并整单弃查）。⛔ 不烧放弃数——不是「查无」，是「查不了」。
            if order.pk not in has_key_pks and order.pk not in no_key_pks:
                no_key_pks.add(order.pk)
            continue
        if order.pk in has_key_pks:
            # ⭐ S2：同单挂多个启用绑定的 link（罕见）⇒ 只收第一条
            #    **带键**的，⛔ 不去重就是同一单被问两遍、留痕两行。
            continue
        has_key_pks.add(order.pk)
        no_key_pks.discard(order.pk)  # 键空单被后续带键 link 救回（S3）
        jobs.append(
            _JidanpuJob(
                order_pk=order.pk, link_pk=link.pk, binding_pk=link.binding_id, key=key,
                sort_key=(
                    order.completed_at is None,
                    order.completed_at or _TIME_SENTINEL,
                    order.pk,
                ),
            )
        )
    summary["skipped_no_link"] += len(no_key_pks)
    return jobs, linked_pks


def _jidanpu_fallback_binding_by_source(
    bindings: list[JidanpuUpstreamBinding],
) -> dict[int, JidanpuUpstreamBinding]:
    """兜底映射：`order_source_id → 绑定`，**仅当该来源恰好一个启用①式绑定**。 ⭐ 换源轮起它只圈定「**本该被兜底问**」的候选范围供
    `_jidanpu_fallback_jobs` 计数（该函数已不再产查询单）——兜底查询本身已证伪（GetMyEmp 键域只有 `key=<orderId>`）。唯一性口径不变：绑定 `order_source` 为
    NULL 的**不算数**；两个启用绑定配同一来源 ⇒ 无唯一 ⇒ 该来源不兜底（计划 §2.2 / S4）。"""
    by_source: dict[int, list[JidanpuUpstreamBinding]] = {}
    for binding in bindings:
        if binding.order_source_id is None:
            continue
        by_source.setdefault(binding.order_source_id, []).append(binding)
    return {
        source_id: group[0]
        for source_id, group in by_source.items()
        if len(group) == 1
    }


def _jidanpu_fallback_jobs(
    summary: dict,
    *,
    fallback_by_source: dict[int, JidanpuUpstreamBinding],
    order_filters: dict,
    exclude_pks: set[int],
) -> list[_JidanpuJob]:
    """兜底路径（换源轮起**不再产查询单**，恒回空列表）：无 link 的来源单逐张计数 `skipped_no_link` 后跳过。 ⭐ 真机证伪：GetMyEmp 键域只有 `key=<orderId>`，`key2`（source_order_no 的 orderIdView 域）恒 0 行、部分店行内该列干脆为空 ⇒ 兜底查询不可能命中。这里仍按
    原候选口径把「本该被问」的单**数出来**（夜间摘要的 `skipped_no_link`），让首夜读数解释得通「这些单为什么没被查」；⛔ 不烧放弃数（不是「查无」，是「查不了」）。主路径 link 单不受影响（`exclude_pks` 把它们排除，不双计）。
    ⚠️ 函数名与签名照旧（恒空列表）：它的 `Order.objects` 读点登记在 `test_order_read_path_net.ORDER_READ_SITES` 那一格（E-0175），改名=白红一张结构网；「恒空」由 `test_a_fallback_source_order_is_counted_as_skipped_no_link` 钉住。"""
    if not fallback_by_source:
        return []
    orders = Order.objects.filter(
        order_source_id__in=sorted(fallback_by_source.keys()), **order_filters
    )
    if exclude_pks:
        orders = orders.exclude(pk__in=exclude_pks)
    orders = orders.annotate(_fallback_source_no=Trim(F("source_order_no"))).exclude(
        _fallback_source_no=""
    )
    summary["skipped_no_link"] += orders.count()
    return []


def _income_view_client(client):
    """共享同一会话的「裸数组视图」客户端：只把 config 的 `unwrap` 换成 `"status"`。 ⭐ 换源轮：GetMyEmp 回**裸 JSON 数组**（不是 `{code,data}` 信封），默认 `unwrap="auto"` 会把裸数组判成「缺业务码」抛 `ApiError` ⇒ 每单都成 error—— 通道一个都翻不动。照 `_viewpic_client` 先例（`jidanpu_upstream_pics.py`）：
    `dataclasses.replace` 换一格 config、**共享 auth**（已登录时 `ensure_login` 不重登） + registry + 同一个 httpx 客户端（= 同一份 cookie jar；派生 Transport 不拥有它，关仍由建连处 `open_client_with_http` 的 finally 负责）。 ⚠️ `_tt_http` 是本仓建连处（`_open_client_pair`）自己挂的引用，SDK 没有公开访问器；
    拿不到 ⇒ `ConfigError` 响亮失败。**落点（S2，评审 改准）**：它在 `_client_for`（逐单循环体）里抛、不在 `_query_jidanpu_one` 的逐单异常收窄内 ⇒ 冒到编排器的逐通道边界 ⇒ `unexpected_error` 记档 + 该通道本轮剩余候选全跳（比逐单 error 更响亮更安全——缺 `_tt_http` 是我们的装配错、不是某一张单坏了； ⛔ 不静默退回 auto——退回就是把「我们的装配错了」说成「上家坏了」）。"""
    http = getattr(client, "_tt_http", None)
    if http is None:
        raise ConfigError("建连产物缺 _tt_http ⇒ 造不出 GetMyEmp 裸数组视图客户端")
    config = replace(client.config, unwrap="status")
    return JidanpuClient(config, registry=client.registry, auth=client.auth, http_client=http)


def _jidanpu_income_query(*, key: str, stime: str, etime: str) -> dict:
    """GetMyEmp 的**全过滤参数**（换源轮真机探针：缺任一 ⇒ 对端**静默 0 行**）。
    ⛔ 测试网有「全参数断言」钉住这份键表（缺/改任何一个键那条网当场红）——
    动它先过网，别靠对端「宽容」。"""
    return {
        "gameName": "全部游戏",
        "stime": stime,
        "etime": etime,
        "dateType": "10",
        "settingSta": "",
        "orderSta": "",
        "key": key,
        "key2": "",
        "page": 1,
        "limit": 50,
    }


def _is_session_shell(payload: dict) -> bool:
    """2xx 回包认「会话失效壳」（S1）：dict 带 `code` 且 msg 命中 vendored 传输层的既有提示词表（`_SESSION_EXPIRED_HINTS`，⛔ 不自编文案）。
    为什么在这里认：302 形态的会话过期传输层自己会抛 `SessionExpiredError`； **2xx JSON 壳形态**在 `unwrap="status"` 下没人判，会当数据漏进形状守卫
    ——漏进来的就是它（见模块 docstring 残差段）。"""
    if not (isinstance(payload, dict) and "code" in payload):
        return False
    message = _message_of(payload)
    return message is not None and any(hint in message for hint in _SESSION_EXPIRED_HINTS)


def _window_stamp(value: datetime) -> str:
    """窗口时刻 → 上游墙上时间字符串（Asia/Shanghai，照
    `jidanpu_upstream.my_orders_window` 同一条实测口径；naive 原样用）。"""
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return local.strftime(JIDANPU_INCOME_WINDOW_FORMAT)


def _query_jidanpu_one(client, job: _JidanpuJob, fresh, state: _RunState) -> tuple[str, dict | None]:
    """逐单问①式（换源轮：GetMyEmp，窗口必须罩住录单时间）→ 结局。 - 窗口：`stime = fresh.created_at − 7 天`、`etime = 查询当刻 + 1 天`（服务端**先按窗
    过滤再配 key** ⇒ 窗不罩录单时间，命中单也会 0 行；见模块 docstring 证据块）； - 网络类失败（`JidanpuError` 族，含「回包非裸数组」的形状失败）总尝试 ≤
    `NETWORK_MAX_ATTEMPTS` ⇒ `error`（零写）； - 命中行按 `settingSta` 判据分 `settled` / `not_settled`；0 行 ⇒ `not_found` （旧「已退单二次问法」已随换源退役——GetMyEmp 无此问法）。"""
    stime = _window_stamp(
        fresh.created_at - timedelta(days=JIDANPU_INCOME_WINDOW_BACK_DAYS)
    )
    etime = _window_stamp(_now() + timedelta(days=JIDANPU_INCOME_WINDOW_FORWARD_DAYS))
    for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
        state.pace()
        try:
            rows = client.call(
                "GET",
                JIDANPU_MY_EMP_PATH,
                query=_jidanpu_income_query(key=job.key, stime=stime, etime=etime),
            )
            # GetMyEmp 既有裸数组，也有信封。失败信封的 data 可能是空数组或旧行。
            if isinstance(rows, dict):
                if _is_session_shell(rows):
                    client.auth.invalidate()
                    raise SessionExpiredError(
                        f"{JIDANPU_MY_EMP_PATH} 回了会话失效壳，已清共享会话待重登",
                        endpoint=JIDANPU_MY_EMP_PATH,
                    )
                code = rows.get("code")
                if type(code) is not int or code != 0 or rows.get("success", True) is not True:
                    raise ApiError("上家收款查询失败", endpoint=JIDANPU_MY_EMP_PATH, code=code)
                rows = rows.get("data")
            if not isinstance(rows, list):
                raise ApiError(
                    f"{JIDANPU_MY_EMP_PATH} 回包不是裸数组（{type(rows).__name__}）"
                    "⇒ 形状漂移，判不了这单",
                    code=None,
                )
            row = _match_row(rows, key=job.key, key_field="orderId")
            if row is not None:
                if jidanpu_row_settled(row):
                    return OUTCOME_SETTLED, row
                return OUTCOME_NOT_SETTLED, row
            return OUTCOME_NOT_FOUND, None
        except JidanpuError:
            if attempt >= NETWORK_MAX_ATTEMPTS:
                return OUTCOME_ERROR, None
    return OUTCOME_ERROR, None  # pragma: no cover —— 循环必经 return


def _run_jidanpu_channel(
    summary: dict, *, order_filters: dict, deadline: float, interval: float
) -> None:
    """①式通道一轮：候选（主路径 + 兜底）→ 逐单问 → 结局处理。 ⚠️ 候选按 `completed_at asc` 全局排序（老单优先跨绑定）；绑定客户端**懒建**、
    同一绑定跨单复用同一个会话（⛔ 一单一登录正是 2026-10-20 对端 WAF 封 IP 的老路，
    `jidanpu.UpstreamClientPool` 的成立理由）。"""
    bindings = _jidanpu_credential_bindings(summary)
    if not bindings:
        return
    binding_pks = [binding.pk for binding in bindings]
    jobs, linked_pks = _jidanpu_link_jobs(
        summary, binding_pks=binding_pks, order_filters=order_filters
    )
    fallback_by_source = _jidanpu_fallback_binding_by_source(bindings)
    # ⭐ 换源轮：兜底路径不再产查询单（键域已证伪），`_jidanpu_fallback_jobs`
    #    只把「本该被问」的来源单计数进 skipped_no_link、恒回空列表（见该函数 docstring）。
    jobs += _jidanpu_fallback_jobs(
        summary,
        fallback_by_source=fallback_by_source,
        order_filters=order_filters,
        exclude_pks=linked_pks,
    )
    # ⭐ S1：「老单优先」是**跨路径全局**的——主路径整段排在兜底段
    #    前只是「段内 asc」⇒ 收集完对全部 jobs 一次性归并排序（None 的 completed_at
    #    排最后，同库 NULLS LAST 口径），docstring 的全局口径由此成立。
    jobs.sort(key=lambda job: job.sort_key)
    if not jobs:
        return

    state = _RunState(summary, deadline=deadline, interval=interval)
    with contextlib.ExitStack() as stack:
        clients: dict[int, object] = {}

        def _client_for(binding):
            if binding.pk not in clients:
                # ⭐ M1：`open_client_with_http` 产出的是 `(client, http)` 二元组——**解包后只存 SDK client 本身** （把元组整个传下去 = 生产首单 `AttributeError`，不是 `JidanpuError`，
                # 重试/异常收窄都接不住）。ExitStack 退出时 `open_client_with_http` 的 finally 仍把 pair 双双关掉（同款形状先例 `recheck_terminated_links.py:102` 的 `as (sdk, _http)`）。
                # ⭐ 换源轮：存的是 SDK client 的「裸数组视图」（`_income_view_client`，仍是 JidanpuClient、共享同一会话）—— GetMyEmp 回裸数组，auto 解包会把每一发都判成「缺业务码」。
                client, _http = stack.enter_context(
                    jidanpu.open_client_with_http(binding)
                )
                clients[binding.pk] = _income_view_client(client)
            return clients[binding.pk]

        for job in jobs:
            if state.over_cap():
                summary["cap_hit"] = True
                logger.warning("①式收款查询达到单轮 %d 单上限，停止扫描", MAX_ORDERS_PER_RUN)
                break
            if state.over_budget():
                summary["deadline_hit"] = True
                logger.warning("①式收款查询达到 %.0f 秒预算，停止扫描", TIME_BUDGET)
                break
            state.processed += 1
            summary["scanned"] += 1

            fresh = _fresh_order(job.order_pk)
            if fresh is None:
                summary["vanished"] += 1
                continue
            if not _still_pending(fresh):
                summary["stale_skip"] += 1
                continue

            # ⚠️ 处理时**重读绑定**：候选收集后它可能被删 / 改成②式 / 凭据被清（竞态）
            #    ⇒ 如实留痕 bind_error、跳过（该绑定其余候选逐单同样落这档）。
            binding = JidanpuUpstreamBinding.objects.filter(pk=job.binding_pk).first()
            if (
                binding is None
                or binding.mode != JidanpuUpstreamBinding.Mode.PASSWORD
                or jidanpu.credentials_of(binding) is None
            ):
                _write_query_log(
                    fresh, channel="jidanpu", binding_label=f"#{job.binding_pk}",
                    outcome=OUTCOME_BIND_ERROR, extra="绑定已不存在或不可用",
                )
                summary["bind_error"] += 1
                continue

            link = JidanpuUpstreamOrderLink.objects.filter(
                pk=job.link_pk, order_id=job.order_pk, binding_id=job.binding_pk,
            )
            if (link.values_list("upstream_order_id", flat=True).first() or "").strip() != job.key:
                summary["stale_skip"] += 1
                continue
            outcome, row = _query_jidanpu_one(_client_for(binding), job, fresh, state)
            summary["queried"] += 1
            label = _binding_host(binding.jidanpu_base_url)
            payload = _jidanpu_upstream_payload(row)

            with transaction.atomic():
                # Match the same binding and link after the network trip. Rebind takes
                # the binding lock before retiring links.
                if not binding.current_identity_rows().select_for_update().exists():
                    summary["stale_skip"] += 1
                    continue
                remote_id = link.select_for_update().values_list("upstream_order_id", flat=True).first()
                if (remote_id or "").strip() != job.key:
                    summary["stale_skip"] += 1
                    continue
                latest = _fresh_order(job.order_pk, lock=True)
                if not _still_pending(latest) or latest.completed_at != fresh.completed_at:
                    summary["stale_skip"] += 1
                    continue
                if outcome == OUTCOME_SETTLED:
                    _settle_it(
                        latest, channel="jidanpu", label=label,
                        outcome=outcome, upstream=payload,
                    )
                else:
                    _write_query_log(
                        latest, channel="jidanpu", binding_label=label,
                        outcome=outcome, upstream=payload,
                    )
                    if outcome == OUTCOME_NOT_FOUND:
                        _bump_notfound(latest.pk)
            summary[outcome] = summary.get(outcome, 0) + 1


# ── peer 通道（团团通上家：登录对端网页 API）───────────────────────────────


class PeerQueryError(Exception):
    """对端查询单的网络 / 形状失败（可重试；烧尝试次数 ⇒ `error` 结局）。"""


class PeerBindError(Exception):
    """对端明确拒绝（凭据 / 限流 / 401/403）：⇒ `bind_error` + 该绑定闩本轮。
    `reason` ∈ credentials / rate_limited / app_account / forbidden——留痕 detail 要能
    把这四档分开（别把限流误诊成前置缺失，D-0332 / D-0277 在案）。"""

    def __init__(self, msg: str, *, reason: str) -> None:
        super().__init__(msg)
        self.reason = reason


class PeerQueryClient:
    """对端网页查询小客户端（HTTP 库照 `peer_client.py` 现用 httpx）。流程：`GET /api/auth/csrf`（拿 `csrftoken` cookie）→ `POST /api/auth/login`
    （`X-CSRFToken` 头 + `username/password`）→ `GET /api/business/orders/?order_nos=<键>`。
    ⚠️ 判成败一律看**信封业务码**（E-0087：调用不抛 ≠ 成功；本仓的失败也是 200 + 业务码）。 ⚠️ 密码只进登录请求体，⛔ 不进任何异常文案 / 日志。"""

    def __init__(self, *, base_url: str, username: str, password: str,
                 timeout: float = PEER_QUERY_TIMEOUT) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._http: httpx.Client | None = None

    def __enter__(self) -> "PeerQueryClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            # ⭐ 缺省 Origin/Referer（生产对照实锤）：对端 Django 的 CSRF 中间件对 HTTPS POST 缺 `Origin` ⇒ 403「CSRF 校验失败」——浏览器永远带
            # Origin 所以网页端无感；curl 带 token+cookie 无 Origin 是 403、加 Origin 变 401（假密码正常进认证）⇒ 根因只有这一环。同源自身即合法 Origin，
            # ⛔ 不要求对端改 CSRF_TRUSTED_ORIGINS。
            parts = urlsplit(self._base_url)
            origin = f"{parts.scheme}://{parts.netloc}"
            self._http = httpx.Client(
                base_url=self._base_url, timeout=self._timeout,
                follow_redirects=False,
                headers={"Origin": origin, "Referer": origin + "/"},
            )
        return self._http

    @staticmethod
    def _envelope(resp: httpx.Response) -> dict:
        """响应 → 信封 dict；不是 JSON / 形状不对 ⇒ `PeerQueryError`（⛔ 不静默当空）。"""
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PeerQueryError(
                f"对端响应不是 JSON（HTTP {resp.status_code}）"
            ) from exc
        if not isinstance(payload, dict) or "code" not in payload:
            raise PeerQueryError(
                f"对端响应不是信封形状（HTTP {resp.status_code}）"
            )
        return payload

    def login(self) -> None:
        """登录对端；失败按四档翻 `PeerBindError` / `PeerQueryError`。"""
        client = self._client()
        try:
            resp = client.get(PEER_CSRF_PATH)
        except httpx.HTTPError as exc:
            raise PeerQueryError(f"对端 csrf 引导失败（{type(exc).__name__}）") from exc
        if resp.status_code != 200:
            raise PeerQueryError(f"对端 csrf 引导失败（HTTP {resp.status_code}）")
        token = client.cookies.get(PEER_CSRF_COOKIE)
        if not token:
            raise PeerQueryError("对端没回 csrftoken cookie")
        try:
            resp = client.post(
                PEER_LOGIN_PATH,
                json={"username": self._username, "password": self._password},
                headers={PEER_CSRF_HEADER: token},
            )
        except httpx.HTTPError as exc:
            raise PeerQueryError(f"对端登录请求失败（{type(exc).__name__}）") from exc
        envelope = self._envelope(resp)
        code = envelope.get("code")
        if resp.status_code == 200 and code == ErrorCode.OK:
            return
        # 401/403 + 业务码分档（⛔ 文案不含凭据；三档各有处置，见计划 §8）。
        if code == ErrorCode.LOGIN_RATE_LIMITED:
            raise PeerBindError(
                "对端登录被限流（1038：该账号失败过于频繁，等 15 分钟自愈，勿连点）",
                reason="rate_limited",
            )
        if code == ErrorCode.APP_ACCOUNT_WEB_LOGIN_FORBIDDEN:
            raise PeerBindError(
                "对端账号是 App 来源，禁止网页登录（1034：换 console 来源子账号）",
                reason="app_account",
            )
        if resp.status_code == 401:
            raise PeerBindError(
                "对端登录失败（凭据错 / 账号停用）", reason="credentials",
            )
        if resp.status_code == 403:
            raise PeerBindError("对端拒绝访问（403）", reason="forbidden")
        raise PeerQueryError(
            f"对端登录响应形状不对（HTTP {resp.status_code}，code={code}）"
        )

    def fetch_orders(self, *, order_no: str) -> list:
        """按单号查对端订单行（`data.items`）；信封业务码非 0 ⇒ `PeerQueryError`。"""
        client = self._client()
        try:
            resp = client.get(PEER_ORDERS_PATH, params={"order_nos": order_no})
        except httpx.HTTPError as exc:
            raise PeerQueryError(f"对端查单请求失败（{type(exc).__name__}）") from exc
        if resp.status_code in (401, 403):
            raise PeerBindError(
                f"对端查单被拒（HTTP {resp.status_code}：会话失效或无权限）",
                reason="forbidden",
            )
        envelope = self._envelope(resp)
        code = envelope.get("code")
        if code != ErrorCode.OK:
            raise PeerQueryError(f"对端查单业务失败（code={code}）")
        data = envelope.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise PeerQueryError("对端查单信封缺 data.items")
        return data["items"]


@dataclass(frozen=True)
class _PeerJob:
    """peer 的一单：查询键（对端订单号）。"""

    order_pk: int
    binding_pk: int
    key: str


def _peer_ready_bindings(summary: dict) -> list[PeerUpstreamBinding]:
    """候选闸：`enabled=True` + 未软删 + 查询密码已配 + **https 形态**（计划 §2.2）。
    http 形态与未配密码的绑定排除并计数（http 上生产 Secure cookie 不回传 ⇒ 整链必然 bind_error，`settings.py:336`）。⚠️ `receipt_query_precheck_ok` **不在**候选闸里：
    未过预检的绑定照问（留痕），只是查无不烧放弃数（M3）。"""
    bindings = list(
        PeerUpstreamBinding.objects.filter(enabled=True, deleted_at__isnull=True)
        .order_by("pk")
    )
    ready: list[PeerUpstreamBinding] = []
    for binding in bindings:
        base = (binding.peer_base_url or "").strip()
        if base.startswith("http://"):
            summary["excluded_http_bindings"] += 1
            continue
        if not base.startswith("https://"):
            # ⭐ S3：空串 / 裸域名 / 其它 scheme——拼起来 httpx 会响
            #    `InvalidURL`（⛔ 不在 `httpx.HTTPError` 族，异常收窄接不住）⇒ 在候选闸
            #    排除并计数，别让它走异常路径。
            summary["excluded_bad_base_url"] += 1
            continue
        if not binding.receipt_query_password_set:
            summary["excluded_no_password"] += 1
            continue
        ready.append(binding)
    return ready


def _peer_jobs(
    summary: dict, *, binding_pks: list[int], order_filters: dict
) -> list[_PeerJob]:
    """peer 候选：`PeerOrderLink.upstream_binding`（我方为下家一侧）× 基础条件。
    查询键 = `peer_order_no`（对端订单号），为空退 `origin_order_no`（链起点）；两者皆空
    ⇒ `skipped_no_key`（不烧放弃数）。⛔ 我方 `order.source_order_no` 对端库里没有，不做兜底键。"""
    links = (
        PeerOrderLink.objects.filter(upstream_binding_id__in=binding_pks)
        .filter(order__status=order_filters["status"])
        .filter(order__payment_status=order_filters["payment_status"])
        # ⚠️ join 侧**没有**默认 manager 的软删过滤（那是 `Order.objects` 基集才有的）
        #    ⇒ 显式补一道（已软删的单不再是「待收款」，别让它烧扫描预算）。
        .filter(order__deleted_at__isnull=True)
    )
    if "completed_at__gte" in order_filters:
        links = links.filter(order__completed_at__gte=order_filters["completed_at__gte"])
        links = links.filter(
            order__receipt_sync_notfound_count__lt=order_filters[
                "receipt_sync_notfound_count__lt"
            ]
        )
    else:
        links = links.filter(order__created_at__gte=order_filters["created_at__gte"])
        links = links.filter(order__created_at__lt=order_filters["created_at__lt"])
    links = links.select_related("order").order_by("order__completed_at", "order__id")

    jobs: list[_PeerJob] = []
    for link in links:
        key = (link.peer_order_no or "").strip() or (link.origin_order_no or "").strip()
        if not key:
            summary["skipped_no_key"] += 1
            continue
        jobs.append(
            _PeerJob(order_pk=link.order_id, binding_pk=link.upstream_binding_id, key=key)
        )
    return jobs


def _query_peer_one(client: PeerQueryClient, job: _PeerJob, state: _RunState) -> list | None:
    """逐单问对端；回**行列表**（空列表 = 对端查无，是答案不是失败）。 - 网络类失败（`PeerQueryError`）总尝试 ≤ `NETWORK_MAX_ATTEMPTS` ⇒ 回 `None`（error）；
    - 401/403 ⇒ `PeerBindError` 上抛（调用方闩本轮），⛔ 不重试（不是瞬态）。"""
    for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
        state.pace()
        try:
            return client.fetch_orders(order_no=job.key)
        except PeerBindError:
            raise
        except PeerQueryError:
            if attempt >= NETWORK_MAX_ATTEMPTS:
                return None
    return None  # pragma: no cover —— 循环必经 return


def _peer_row_payment(row) -> str:
    """对端行的 `payment_status` 码（形状不对回空串——由调用方判「非 SETTLED」）。"""
    if not isinstance(row, dict):
        return ""
    return str(row.get("payment_status") or "")


def _run_peer_channel(
    summary: dict, *, order_filters: dict, deadline: float, interval: float
) -> None:
    """peer 通道一轮：候选 → 逐单登录查 → 结局处理（含绑定级 bind_error 闩）。"""
    bindings = _peer_ready_bindings(summary)
    if not bindings:
        return
    jobs = _peer_jobs(
        summary, binding_pks=[binding.pk for binding in bindings],
        order_filters=order_filters,
    )
    if not jobs:
        return

    state = _RunState(summary, deadline=deadline, interval=interval)
    with contextlib.ExitStack() as stack:
        clients: dict[int, PeerQueryClient] = {}
        logged_in: set[int] = set()
        latched: set[int] = set()

        def _client_for(binding) -> PeerQueryClient:
            if binding.pk not in clients:
                clients[binding.pk] = stack.enter_context(
                    PeerQueryClient(
                        base_url=binding.peer_base_url,
                        username=binding.peer_account,
                        password=binding.receipt_query_password,
                    )
                )
            return clients[binding.pk]

        for job in jobs:
            if job.binding_pk in latched:
                # 只闩本轮：该绑定剩余候选全跳过（照查码线 401 整租户闩的降级形状）。
                summary["bind_latched_skip"] += 1
                continue
            if state.over_cap():
                summary["cap_hit"] = True
                logger.warning("peer 收款查询达到单轮 %d 单上限，停止扫描", MAX_ORDERS_PER_RUN)
                break
            if state.over_budget():
                summary["deadline_hit"] = True
                logger.warning("peer 收款查询达到 %.0f 秒预算，停止扫描", TIME_BUDGET)
                break
            state.processed += 1
            summary["scanned"] += 1

            fresh = _fresh_order(job.order_pk)
            if fresh is None:
                summary["vanished"] += 1
                continue
            if not _still_pending(fresh):
                summary["stale_skip"] += 1
                continue

            # ⚠️ 处理时**重读绑定**：候选收集后它可能被软删 / 关成 http / 清掉密码
            #    （竞态）⇒ precheck_failed 留痕、跳过、不烧放弃数。
            binding = PeerUpstreamBinding.objects.filter(pk=job.binding_pk).first()
            if (
                binding is None
                or not (binding.peer_base_url or "").startswith("https://")
                or not binding.receipt_query_password_set
            ):
                _write_query_log(
                    fresh, channel="peer", binding_label=f"#{job.binding_pk}",
                    outcome=OUTCOME_PRECHECK_FAILED, extra="绑定已不满足查询前置",
                )
                summary["precheck_failed"] += 1
                continue

            client = _client_for(binding)
            try:
                # 登录懒执行、同绑定一次（⛔ 一单一登录会把对端刷进限流，D-0332 同族）。
                if job.binding_pk not in logged_in:
                    state.pace()
                    client.login()
                    logged_in.add(job.binding_pk)
                rows = _query_peer_one(client, job, state)
            except PeerBindError as exc:
                _write_query_log(
                    fresh, channel="peer", binding_label=binding.peer_name,
                    outcome=OUTCOME_BIND_ERROR, extra=f"原因={exc.reason}（{exc}）",
                )
                summary["bind_error"] += 1
                latched.add(job.binding_pk)
                continue
            summary["queried"] += 1
            label = binding.peer_name

            if rows is None:
                _write_query_log(
                    fresh, channel="peer", binding_label=label, outcome=OUTCOME_ERROR,
                )
                summary["error"] += 1
                continue

            if len(rows) > 1:
                # 对端同来源号多单（Task 0b 实测：19 位键走对端 source_order_no__in 可能多行）
                # ⇒ 不翻状态、留痕行数。
                _write_query_log(
                    fresh, channel="peer", binding_label=label,
                    outcome=OUTCOME_AMBIGUOUS, extra=f"行数={len(rows)}",
                )
                summary["ambiguous"] += 1
                continue

            if len(rows) == 1:
                payment = _peer_row_payment(rows[0])
                upstream = {"payment_status": payment}
                if payment == str(PaymentStatus.SETTLED):
                    _settle_it(
                        fresh, channel="peer", label=label,
                        outcome=OUTCOME_SETTLED, upstream=upstream,
                    )
                    summary["settled"] += 1
                else:
                    _write_query_log(
                        fresh, channel="peer", binding_label=label,
                        outcome=OUTCOME_NOT_SETTLED, upstream=upstream,
                    )
                    summary["not_settled"] += 1
                continue

            # 对端查无（0 行 ≠ 不存在）：过预检才烧放弃数（M3）。
            if binding.receipt_query_precheck_ok:
                _write_query_log(
                    fresh, channel="peer", binding_label=label,
                    outcome=OUTCOME_NOT_FOUND,
                )
                _bump_notfound(fresh.pk)
                summary["not_found"] += 1
            else:
                _write_query_log(
                    fresh, channel="peer", binding_label=label,
                    outcome=OUTCOME_NOT_FOUND, extra="未过预检⇒不计数",
                )
                summary["not_found_uncounted"] += 1


# ── 编排器 ─────────────────────────────────────────────────────────────────


def run_upstream_receipt_sync(
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    schema_name: str | None = None,
    nightly: bool = False,
) -> dict:
    """本体的**可直调缝隙**：按需落 schema 后按顺序跑通道（①式 → peer）。 - 手动（`nightly=False`，`date_from/date_to` 必给）：**先**转调闲管家通道 `run_xgj_receipt_sync`（口径原样、文件零 diff），**再**跑①式 + peer（录单窗口径）。
    - 夜间（`nightly=True`）：只跑①式 + peer（90 天完成窗 + 放弃计数过滤）。 - `schema_name` 非空 ⇒ `connection.set_schema(...)`（线程用）；`None` ⇒ 不动 schema （`TenantTestCase` / `tenant_context` 已设好）。
    - 返回本轮计数（`ran=False` = 另一轮正在跑、本次一行未动）。 - ⚠️ **异常收窄**：可预期失败（上游失败 / 形状漂移 / 竞态）逐单计数继续； **未预期异常 ⛔ 不接**，抛给线程体 / 命令的逐租户边界响亮记录。 - ⚠️ 锁：本函数自己持 `xgj_receipt_sync` 的 advisory lock（同 namespace ⇒ 与旧按钮 / 在跑实例互斥）；手动模式转调 `run_xgj_receipt_sync` 是**同会话计数重入**、其 finally 只放一层（已核定案，见模块 docstring）。"""
    if schema_name:
        connection.set_schema(schema_name)

    started = _monotonic()
    summary: dict = {
        "ran": False,
        "mode": "nightly" if nightly else "manual",
        "elapsed": 0.0,
    }
    with xgj_receipt_sync._sync_guard() as acquired:
        if not acquired:
            logger.info("上家收款同步：另一轮正在运行，本次直接退出")
            summary["elapsed"] = _monotonic() - started
            return summary

        summary["ran"] = True
        interval = _interval_seconds()

        if not nightly:
            summary["xgj"] = xgj_receipt_sync.run_xgj_receipt_sync(
                date_from=date_from, date_to=date_to, schema_name=None,
            )
            order_filters = _manual_order_filters(date_from=date_from, date_to=date_to)
        else:
            order_filters = _nightly_order_filters(now=timezone.now())

        # ⭐ M1-3：**逐通道** try/except 边界——单通道未预期异常（真元组流到真取数口的 `AttributeError` 就是这一形态）⇒ 响亮记录、该通道
        # 记档（`unexpected_error`）、**另一通道照跑**（查码线「毒单中断整租户」同族教训，审计 M6）；⛔ 通道层不把异常吞成静默——命令侧读该标记，
        # 租户这一轮仍以未预期失败收口（退出码 1 语义不回退）。
        summary["jidanpu"] = _new_channel_summary()
        try:
            _run_jidanpu_channel(
                summary["jidanpu"],
                order_filters=order_filters,
                deadline=_monotonic() + TIME_BUDGET,
                interval=interval,
            )
        except Exception:  # noqa: BLE001 —— 边界只记档与继续，不冒泡（见上）
            logger.exception("①式收款查询通道未预期失败 ⇒ 本轮跳过该通道（另一通道照跑）")
            summary["jidanpu"]["unexpected_error"] = True
        summary["peer"] = _new_channel_summary()
        try:
            _run_peer_channel(
                summary["peer"],
                order_filters=order_filters,
                deadline=_monotonic() + TIME_BUDGET,
                interval=interval,
            )
        except Exception:  # noqa: BLE001 —— 同上
            logger.exception("peer 收款查询通道未预期失败 ⇒ 本轮跳过该通道（另一通道照跑）")
            summary["peer"]["unexpected_error"] = True

    summary["elapsed"] = _monotonic() - started
    logger.info(
        "上家收款同步完成 mode=%s elapsed=%.1fs jidanpu[%s] peer[%s]",
        summary["mode"], summary["elapsed"],
        _compact_channel(summary.get("jidanpu")), _compact_channel(summary.get("peer")),
    )
    return summary


def _compact_channel(channel) -> str:
    """摘要日志的一行压缩（候选/翻状态/查无/错误/排除——计划 §5 第 13 条）。"""
    if not isinstance(channel, dict):
        return "-"
    keys = (
        "scanned", "queried", "settled", "not_settled", "not_found",
        "not_found_uncounted", "ambiguous", "bind_error", "error",
        "skipped_no_key", "skipped_no_link", "vanished", "stale_skip",
        "bind_latched_skip",
        "excluded_openapi_bindings", "excluded_no_credentials",
        "excluded_http_bindings", "excluded_no_password",
    )
    return " ".join(f"{key}={channel.get(key, 0)}" for key in keys)


# ── 手动按钮的线程入口（body / 权限 / busy 码 / 预算预检不变，只换线程体）───

#: watchdog 预算 = 手动模式三个通道各自的 TIME_BUDGET 之和（watchdog 只告警、杀不掉线程）。
_THREAD_BUDGET = TIME_BUDGET * 3


def start_upstream_receipt_sync(*, date_from: date, date_to: date) -> None:
    """端点调它：把本体交给一个**短命 daemon 线程**（请求不等上游，照 xgj 先例）。
    ⚠️ schema 是连接级状态（django-tenants）：新线程拿到的是另一条连接 ⇒ 把
    `connection.schema_name` 当普通值随参数带过去。"""
    schema_name = connection.schema_name
    threading.Thread(
        target=_sync_in_thread,
        kwargs={"date_from": date_from, "date_to": date_to, "schema_name": schema_name},
        name="upstream-receipt-sync",
        daemon=True,
    ).start()


def _warn_over_budget() -> None:
    """watchdog 到点：只记 warning（⛔ 它杀不掉堵住的线程，照 xgj_ship 如实登记）。"""
    logger.warning(
        "上家收款同步超过 %.0f 秒仍未结束（疑似上游卡死；socket 超时封不住总时长，"
        "线程是 daemon、进程重启即丢）",
        _THREAD_BUDGET,
    )


def _sync_in_thread(*, date_from: date, date_to: date, schema_name: str) -> None:
    """daemon 线程体：看门狗 → 本体 → 关掉这条线程自己开的数据库连接。"""
    watchdog = threading.Timer(_THREAD_BUDGET, _warn_over_budget)
    watchdog.daemon = True
    watchdog.start()
    try:
        run_upstream_receipt_sync(
            date_from=date_from, date_to=date_to, schema_name=schema_name,
        )
    except Exception:  # noqa: BLE001 —— 能逃出本体的都不是业务失败（异常收窄见 run）
        logger.exception("上家收款同步线程内部错误")
    finally:
        watchdog.cancel()
        connections.close_all()
