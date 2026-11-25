"""订单管理页「同步收款」的**领域模块**（计划 `docs/plans/2026-10-26-receipt-sync-button.md`；业务方拍板）。 ## 它做什么按**录单时间**窗口取一批「未收款」单，逐笔用**该单店铺所属的闲管家应用**查上游 `order/detail`；`order_status` 命中「交易成功」（`xgj_push.RECEIPT_PUSH_STATUS`）⇒ 走**既有** 写入口 `order_writes.change_payment_status` 改成「已结款」，并复用推送线的机器 ctx / 留痕。其它状态跳过；上游失败只计数不中断。 ## 落点与口径（逐条 `file:line` 见计划 §1） - **取单唯一口径 = `candidates_qs()`**：端点的 `count()` 与执行循环**必须同源**（计划 B2）。 - **凭据复用** `platform_order_views.ReadPlatformOrderView._binding_for_source`（**不试错、
不宽回落**）：`order_source` 非空 ⇒ 只认它；为空 ⇒ **仅当本租户恰好一个完整 binding** 才兜底。 - **成功判据只有一处**：`xgj_push.RECEIPT_PUSH_STATUS`；比较前归一化（`str(...).strip()`，上游可能给数字）。 - **写收款只走** `order_writes.change_payment_status`（唯一写入口），⛔ 不直接 `.update()`；每单一个 `atomic()`，一张拒不连坐（照 `xgj_push` 的 22 分支）。 - **后台执行** = 短命 daemon 线程 + watchdog `Timer` + `finally: connections.close_all()` （照 `xgj_ship.dispatch_auto_ship` / `_auto_ship_in_thread`）。 - **单例 = 会话级 advisory lock**（照 `jidanpu_member._outbox_guard`，但**独立 namespace**）：
**线程自己的连接**上取 / 放（`try/finally`）；拿不到就静默退出。请求线程先用 `sync_is_running()` 做一次「试拿即放」探测，抢到才起线程（计划 §2-3/§2-4）。 - **限速** = 每次上游调用之间 `sleep(max(0, next_at - monotonic()))`，`XGJ_RECEIPT_SYNC_QPS` （默认 2）。时钟 / sleep 走模块级 `_monotonic` / `_sleep`，用例可注入断言间隔。 - **预算**：单次 ≤ `MAX_ORDERS_PER_RUN`（**起 5000**；端点在起线程前判，超限**零上游**）、总时长 `TIME_BUDGET`（每笔之间检查，到点停止；watchdog 只告警、**杀不掉**线程）。 ## 如实登记的残差（⛔ 不是缺陷） - **不做负结果记忆**：不建表记「查过了不成功」⇒ 重复点同一范围会**重复查同一批**，而闲管家按次计费（计划 §3-1 的 B2 附加条；业务要的是「手动一次性」，重复查结果幂等）。 - **手动同步改出来的「已收款」不进「闲管家收款明细」页**：那一页只读推送记账行 `XgjReceiptRecord`（`xgj_receipt_stats._window_rows`），不读订单 ⇒ 用户会看到「订单列表已收款、收款明细没有这笔」。这是业务方确认的**已知行为**（计划 §0 的 I6）。 - **手动 / 自动留痕分不出**：留痕动作码 `order.xgj_push_receipt` 的短名是「自动收款」（`apps/audit/actions.py`）⇒ 日志里区分不出手动还是自动（计划 §1.2 的 M6，本批不改文案）。 - **极小竞态**：两次点击几乎同时到达时可能都过探测，只有线程侧真锁赢家跑；业务不需要页面反馈，无害（计划 §2-4）。"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime

from django.conf import settings
from django.db import connection, connections, transaction
from django.db.models import F, Q
from django.db.models.functions import Trim

from apps.business import xgj_push
from apps.business.models import (
    Order,
    OrderStatus,
    PaymentStatus,
    XgjShop,
    XgjShopBinding,
)
from apps.business.order_writes import change_payment_status
from apps.business.xgj import DEFAULT_BASE_URL, XgjError, order_detail

logger = logging.getLogger(__name__)

#: 单次最多处理多少笔（端点在起线程前用**同一个** `candidates_qs()` 判；超限零上游）。 ⭐ **业务方拍板：500 → 5000**（原值 500，一天的单量经常超，被迫切成几段跑）。
#: ⚠️ 真正的「一轮能跑完多少」还受 `TIME_BUDGET`（**起 1 小时**，原 900 秒）与 `DEFAULT_QPS`（2 笔/秒）封着：5000 笔按 2 笔/秒要 ~42 分钟 ⇒ 现在一轮能跑完。
MAX_ORDERS_PER_RUN = 5000

#: 整轮总时长预算（秒）：超过就停止扫描（已处理的都落库了，再点一次可继续）。 ⭐ **业务方拍板：15 分钟 → 1 小时**（原 900 秒）。
#: 为什么一起改：上限提到 5000 笔后，按 `DEFAULT_QPS`（2 笔/秒）跑完要 ~42 分钟 ⇒ 预算仍留在 900 秒的话，5000 的上限等于没用（跑到 450 笔就被截断）。
#: ⚠️ 到点只是**停止扫描**（已处理的全部已落库，再点一次接着跑），线程不占锁、不占页面。
TIME_BUDGET = 3600.0

#: 单笔上游调用的超时（秒）。⚠️ 这是 **socket 单次读写**级（PROD-02），封不住总时长
#: ⇒ 总时长由 `TIME_BUDGET` 在每笔之间兜。
UPSTREAM_TIMEOUT = 10.0

#: 限速默认值（笔/秒）。业务上限 3；生产可经 `XGJ_RECEIPT_SYNC_QPS` 覆盖。
DEFAULT_QPS = 2

#: advisory lock 的**命名空间**：与 `jidanpu_member.OUTBOX_LOCK_NAMESPACE`（0x4F555442）
#: 必须不同——命名空间是锁的身份，撞了等于两件事共用一把锁。
SYNC_LOCK_NAMESPACE = 0x58474A52  # "XGJR"

#: 同一命名空间下的键（本线只需要一把全局锁：一个租户同时只跑一轮同步收款）。
SYNC_LOCK_KEY = 0

#: 时钟与 sleep 的**模块级出口**（用例注入后断言间隔；⛔ 不在函数里直接 `time.sleep`，
#: 那样只能去 patch 全局 `time` 模块，E-0029 的「打在被查找的模块上」不成立）。
_monotonic = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class BindingAvailability:
    """本租户「可用凭据」的视图（`candidates_qs` 与逐行兜底共用同一份判据）。 - `source_ids`：有**至少一条凭据完整的店**的来源主键集合（按来源认店认应用）。
    - `fallback_binding` / `fallback_credentials`：**仅当本租户恰好一个完整 binding** 时非空；
    它只给「订单没填 `order_source`」那一档兜底（零个或多个 ⇒ ⛔ 不猜）。"""

    source_ids: frozenset[int]
    fallback_binding: XgjShopBinding | None
    fallback_credentials: tuple[str, str] | None


def window_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """本地日窗口 → **aware** 的 `[start, end)` 半开区间（`[当日 00:00, 次日 00:00)`）。
    ⚠️ 判据与 `xgj_receipt_stats.window_bounds` **同一条理由**（复用同一份实现，⛔ 不另抄）：
    `USE_TZ=True` 下必须用本地日 00:00 的 aware 值进 `__gte/__lt`，⛔ 不用 `__date` 后缀（那会按**数据库会话时区**截日，与「按本地日」是两套判据）。"""
    # 函数内 import：避免领域模块之间在 import 期互相牵连（同 `xgj_push._credentials_of` 的形状）。
    from apps.business.xgj_receipt_stats import window_bounds as stats_window_bounds

    return stats_window_bounds(date_from, date_to)


def _credentials_of(binding):
    """凭据完整性判据的唯一出处（`apps.business.api.xgj_views.credentials_of`）。
    ⚠️ 函数内 import：那个判据当初落在 api 层（同款先例 = `xgj_push._credentials_of` /
    `xgj_ship._credentials_of`）⇒ 复用，⛔ 不在这里抄一份 `strip` 判据（E-0021）。"""
    from apps.business.api.xgj_views import credentials_of

    return credentials_of(binding)


def binding_availability() -> BindingAvailability:
    """本租户可用的闲管家凭据视图（`candidates_qs` 与逐行兜底**同一份判据**）。"""
    complete: list[tuple[XgjShopBinding, tuple[str, str]]] = []
    for binding in XgjShopBinding.objects.order_by("id"):
        found = _credentials_of(binding)
        if found is not None:
            complete.append((binding, found))

    source_ids = set(
        XgjShop.objects.filter(
            binding_id__in=[binding.pk for binding, _ in complete],
            order_source__isnull=False,
        ).values_list("order_source_id", flat=True)
    )

    fallback_binding = None
    fallback_credentials = None
    if len(complete) == 1:
        fallback_binding, fallback_credentials = complete[0]
    return BindingAvailability(
        source_ids=frozenset(source_ids),
        fallback_binding=fallback_binding,
        fallback_credentials=fallback_credentials,
    )


def candidates_qs(*, date_from: date, date_to: date):
    """**唯一取单口径**（端点的 `count()` 与执行循环必须同源；计划 §3-1 的 B2）。判据（全部落在库里，⛔ 不在 Python 侧再筛一遍）： - `payment_status == UNSETTLED`（只查未收款）； - 排除 `OrderStatus.REFUNDED`（退款 / 退单由 23/24 线负责，两条线互不掺和）；
    - `created_at ∈ [当日 00:00, 次日 00:00)`（**录单时间**，半开区间）； - `source_order_no` **TRIM 后非空**（`"   "` 不许真发上游）； - `order_source` 命中「有可用 binding 的来源」；`order_source is None` 仅在
    本租户恰好一个完整 binding 时放行； - 排除**隐藏单**（`hall_card` 非空 —— 镜像卡派生的单不进面向人 / 任务读面，同 `selectors.search_orders`）。 ⚠️ 默认 manager 已带软删排除 ⇒ 已软删单天然进不了候选。"""
    start, end = window_bounds(date_from, date_to)
    availability = binding_availability()

    source_filter = Q(order_source_id__in=sorted(availability.source_ids))
    if availability.fallback_credentials is not None:
        source_filter |= Q(order_source__isnull=True)

    return (
        Order.objects.filter(
            source_filter,
            payment_status=PaymentStatus.UNSETTLED,
            created_at__gte=start,
            created_at__lt=end,
        )
        .exclude(status=OrderStatus.REFUNDED)
        .annotate(_receipt_source_order_no=Trim(F("source_order_no")))
        .exclude(_receipt_source_order_no="")
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
    )


def count_candidates(date_from: date, date_to: date) -> int:
    """候选笔数（端点的 `MAX_ORDERS_PER_RUN` 上限判据；与执行循环**同一个** `candidates_qs()`）。"""
    return candidates_qs(date_from=date_from, date_to=date_to).count()


def receipt_success(payload) -> bool:
    """上游 `order/detail` 的订单对象 → 是不是「交易成功」。 ⚠️ 判据常量**唯一出处** = `xgj_push.RECEIPT_PUSH_STATUS`（⛔ 不在这里抄 "22" 字面量）；
    比较前 `str(...).strip()` 归一（上游可能给数字 / 带空白 / 不给这个键）。"""
    if not isinstance(payload, dict):
        return False
    raw = payload.get("order_status")
    return str(raw).strip() == str(xgj_push.RECEIPT_PUSH_STATUS).strip()


def _qps() -> float:
    """本次运行的限速（笔/秒）；非法配置回落默认值（⛔ 不因配置写成 0 就除以零）。"""
    value = getattr(settings, "XGJ_RECEIPT_SYNC_QPS", DEFAULT_QPS)
    try:
        qps = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_QPS)
    return qps if qps > 0 else float(DEFAULT_QPS)


def _base_url() -> str:
    """上游基址：只由平台配置提供，空值回落实测基址（同 `xgj_views` 的 `or DEFAULT_BASE_URL`）。"""
    return getattr(settings, "XGJ_API_BASE", "") or DEFAULT_BASE_URL


@contextmanager
def _sync_guard():
    """会话级 advisory lock：拿到 ⇒ yield `True`，拿不到 ⇒ yield `False`（⛔ 不排队等）。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [SYNC_LOCK_NAMESPACE, SYNC_LOCK_KEY],
        )
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                [SYNC_LOCK_NAMESPACE, SYNC_LOCK_KEY],
            )


def sync_is_running() -> bool:
    """请求线程的重入探测：能拿到锁 ⇒ 没在跑（**拿到后立刻放掉**）；拿不到 ⇒ 正在跑。
    ⚠️ 「同一连接上重复 `pg_try_advisory_lock` 可重入」对**这条探测**不构成假绿：
    探测跑在**请求连接**上、真锁跑在**线程自己的连接**上，两把锁跨会话可见。"""
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)",
                [SYNC_LOCK_NAMESPACE, SYNC_LOCK_KEY],
            )
            acquired = bool(cursor.fetchone()[0])
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    [SYNC_LOCK_NAMESPACE, SYNC_LOCK_KEY],
                )
    return not acquired


def start_xgj_receipt_sync(*, date_from: date, date_to: date) -> None:
    """端点调它：把本体交给一个**短命 daemon 线程**（请求不等上游，PROD-02）。 ⚠️ schema 是**连接级**状态（django-tenants）：新线程拿到的是另一条连接 ⇒ 把
    `connection.schema_name` 当**普通值**随参数带过去（同 `xgj_ship.dispatch_auto_ship`）。"""
    schema_name = connection.schema_name
    threading.Thread(
        target=_sync_in_thread,
        kwargs={"date_from": date_from, "date_to": date_to, "schema_name": schema_name},
        name="xgj-receipt-sync",
        daemon=True,
    ).start()


def _warn_over_budget() -> None:
    """watchdog 到点：只记 warning（⛔ 它杀不掉堵住的线程，照 `xgj_ship` 如实登记）。"""
    logger.warning(
        "闲管家同步收款超过 %.0f 秒仍未结束（疑似上游卡死；socket 超时封不住总时长，"
        "线程是 daemon、进程重启即丢）",
        TIME_BUDGET,
    )


def _sync_in_thread(*, date_from: date, date_to: date, schema_name: str) -> None:
    """daemon 线程体：看门狗 → 本体 → 关掉这条线程自己开的数据库连接。"""
    watchdog = threading.Timer(TIME_BUDGET, _warn_over_budget)
    watchdog.daemon = True
    watchdog.start()
    try:
        run_xgj_receipt_sync(
            date_from=date_from, date_to=date_to, schema_name=schema_name
        )
    except Exception:  # noqa: BLE001 —— 能逃出本体的都不是业务失败（见 run 的收窄）
        logger.exception("闲管家同步收款线程内部错误")
    finally:
        watchdog.cancel()
        connections.close_all()


def _new_summary() -> dict:
    """一轮的计数（只用于日志与用例断言；⛔ 不建任务状态表）。"""
    return {
        "ran": False,
        "scanned": 0,
        "updated": 0,
        "not_success": 0,
        "upstream_error": 0,
        "skipped_no_binding": 0,
        "settled_skip": 0,
        "refunded_skip": 0,
        "no_source_no": 0,
        "vanished": 0,
        "deadline_hit": False,
        "elapsed": 0.0,
    }


def _credentials_for_order(order):
    """按单解析上游凭据：`来源 → XgjShop → binding`（复用 `_binding_for_source`，不试错）。
    返回 `(binding, credentials)`；认不到 / 凭据不全 ⇒ `(None, None)`（调用方跳过计数）。"""
    if order.order_source_id is not None:
        from apps.business.api.platform_order_views import ReadPlatformOrderView

        return ReadPlatformOrderView._binding_for_source(order.order_source_id)
    availability = binding_availability()
    return availability.fallback_binding, availability.fallback_credentials


def _pace(next_call_at: float | None, interval: float) -> float:
    """限速：距上一次上游调用不足 `interval` 就补睡；返回下一次的允许时刻。"""
    now = _monotonic()
    if next_call_at is not None:
        delay = next_call_at - now
        if delay > 0:
            _sleep(delay)
    return _monotonic() + interval


def run_xgj_receipt_sync(
    *, date_from: date, date_to: date, schema_name: str | None = None
) -> dict:
    """本体的**可直调缝隙**：按需落 schema 后跑一轮同步收款。 - `schema_name` 非空 ⇒ `connection.set_schema(...)`（线程用）；`None` ⇒ 不动 schema
    （测试线程的 schema 已由 `TenantTestCase` 设好）。 - 返回本轮计数（`ran=False` = 另一轮正在跑、本次一行未动）。 ⚠️ **异常收窄（计划 I7）**：`XgjError` 与「重读为 `None`（扫单期间被软删）」是可预期
    失败 ⇒ 计数后继续；**未预期异常 ⛔ 不接**，让它抛到线程体由 `logger.exception` 记完整栈。"""
    if schema_name:
        connection.set_schema(schema_name)

    started = _monotonic()
    summary = _new_summary()
    with _sync_guard() as acquired:
        if not acquired:
            logger.info("闲管家同步收款：另一轮正在运行，本次直接退出")
            summary["elapsed"] = _monotonic() - started
            return summary

        summary["ran"] = True
        deadline = started + TIME_BUDGET
        interval = 1.0 / _qps()
        next_call_at = None
        base_url = _base_url()

        queryset = candidates_qs(date_from=date_from, date_to=date_to)
        for order in queryset.order_by("id").iterator(chunk_size=200):
            if _monotonic() >= deadline:
                summary["deadline_hit"] = True
                logger.warning(
                    "闲管家同步收款达到 %.0f 秒预算，停止扫描（已处理的都已落库，"
                    "再点一次可继续）",
                    TIME_BUDGET,
                )
                break
            summary["scanned"] += 1

            # ⚠️ 每笔**重读**：扫描开始到这一刻之间，推送线可能已经改过状态 / 把单软删。
            fresh = Order.objects.filter(pk=order.pk).first()
            if fresh is None:
                summary["vanished"] += 1
                continue
            if str(fresh.payment_status) == str(PaymentStatus.SETTLED):
                summary["settled_skip"] += 1
                continue
            if str(fresh.status) == str(OrderStatus.REFUNDED):
                summary["refunded_skip"] += 1
                continue
            channel_no = (fresh.source_order_no or "").strip()
            if not channel_no:
                summary["no_source_no"] += 1
                continue

            _, credentials = _credentials_for_order(fresh)
            if credentials is None:
                summary["skipped_no_binding"] += 1
                continue
            app_key, app_secret = credentials

            next_call_at = _pace(next_call_at, interval)
            try:
                payload = order_detail(
                    channel_no,
                    app_key=app_key,
                    app_secret=app_secret,
                    base_url=base_url,
                    timeout=UPSTREAM_TIMEOUT,
                )
            except XgjError:
                # 可预期（上游业务失败 / 网络 / 形状）：这一笔白跑，继续下一笔。
                summary["upstream_error"] += 1
                continue

            if not receipt_success(payload):
                summary["not_success"] += 1
                continue

            # 每单一个事务：一张拒不连坐（照 `xgj_push` 的 22 分支）。
            with transaction.atomic():
                change_payment_status(
                    fresh,
                    payment_status=PaymentStatus.SETTLED,
                    ctx=xgj_push._MACHINE_CTX,
                    dry_run=False,
                )
                xgj_push._write_receipt_log(fresh)
            summary["updated"] += 1

    summary["elapsed"] = _monotonic() - started
    logger.info(
        "闲管家同步收款完成 elapsed=%.1fs scanned=%d updated=%d not_success=%d "
        "upstream_error=%d skipped_no_binding=%d settled_skip=%d refunded_skip=%d "
        "no_source_no=%d vanished=%d deadline_hit=%s",
        summary["elapsed"],
        summary["scanned"],
        summary["updated"],
        summary["not_success"],
        summary["upstream_error"],
        summary["skipped_no_binding"],
        summary["settled_skip"],
        summary["refunded_skip"],
        summary["no_source_no"],
        summary["vanished"],
        summary["deadline_hit"],
    )
    return summary
