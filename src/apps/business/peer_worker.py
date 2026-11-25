"""上家侧**投递内核**（spec §7.2/§16.2/§16.5/§16.6）。本模块是 `apps/business/api/peer_views.py::deliver_event` 的**迁移落点**（spec §16.6 第 1 条：投递移出请求路径）⇒ 请求侧的入队（`peer_dispatch`）只写 `pending` 行，真投递在这里由**管理命令+cron**（`apps/business/management/commands/peer_sync.py`）驱动。**一次投递要过的五道关（⛔ 顺序承重）**：①**取活**（`claim_due_events`）：一个事务里 `select_for_update(skip_locked=True)` 取 `pending` 出站事件、打**租约**（`next_attempt_at = now + LEASE_SECONDS`）返回 pk，⛔ **事务里不发 HTTP**（HTTP 在租约打出之后、事务**外**做）；②**事件/绑定还在不在**：事件不存在或不是 `pending` ⇒ `noop`；绑定不存在或**按方向**失效（下行=非 `ACTIVE`；上行=已解绑或未启用，`_binding_for_event`）⇒ 事件转**终态 `failed`**（结果名 `dropped`，停绑后不再投）——⛔ **不是**留在 `pending`：那会让取活每 5 分钟重新认领（台账永远显示「待投递」）且最老 50 条吃光租户名额 ⇒ 其它绑定被**静默饿死**；③**撤单投递前复核**（`_recheck_withdraw`，spec §16.2，**承重不是防御性写法**）：投递那一刻重判「当前接手人还是不是这个绑定账号」——仍是 ⇒ 作废（`done`+人话，⛔ 不发）；已软删 ⇒ 发（单已删正是要通知下家）；单不存在 ⇒ 作废；其余 ⇒ 发；④**投递**：`attempts += 1` ⇒ 恰发一条事件（事件级 `meta` 展开在**顶层、`payload` 之前**）；⑤**成败记账**：成功（含对端良性 `stale` 跳过）⇒ 事件 `done`+`processed_at`、绑定 `last_ok_at`/`consecutive_failures = 0`（两方向同形）；失败 ⇒ 退避重排（四档全部可达），**第 5 次** ⇒ 终态 `failed`，两条失败分支都写绑定失败台账。⭐ **自动停绑是「按轮」的**：一轮**整轮连接类失败** ⇒ `consecutive_failures` +1 **恰好一次**、轮内任一成功即清零（收口在 `_close_round` 轮末统一做）；**业务类**拒绝（`PeerRejected`/对端硬跳非 stale 码）⛔ 不计入——对端能回话=链路是通的（旧口径按尝试累加，对端短暂失联+10 条 pending 第一轮就停绑；停绑是双向 kill，恢复只能重走握手）；计数达 `AUTO_REVOKE_AFTER_FAILURES` ⇒ 自动停绑+写一条操作日志——⚠️ 停绑**只对下行**（上行绑定没有 `state`/`revoked_at` 两列，且「上行连败要不要停推」业务方没有裁定过）。
⭐ **为什么 `meta` 展开在 `payload` 之前（实测的洞，⛔ 不要挪）**：接收侧从事件对象**顶层**读 `occurred_at`/`withdraw_reason`/`actor_name`（`apps/business/peer_inbound.py::apply_events` 的 `raw.get(...)`），⛔ 不读 `payload`（`payload` 只放 §10.1 白名单的订单字段）——少了顶层这几个键，下家的同单更新与撤单落库**永远走不到**而两侧各自的单元测试**全绿**（正是「接线断了没人看得见」的洞）；展开排在 `payload` 之前：即便将来 `meta` 里混进一个 `payload` 键，也顶不掉真正的订单载荷。
**失败姿势**：`PeerUnreachable`/`PeerRejected` 是**预期**失败 ⇒ 落库+退避，⛔ 不冒泡；其余异常（模型列宽、编码问题等）是**未预期**的 ⇒ 由 `run_round` 的逐事件边界接住并记进 `errors`（监控不该失明，E-0010），⛔ 不在这儿伪装成业务结果。⚠️ 本模块是**租户 schema** 内的写路径（`PeerSyncEvent`/`PeerDownstreamBinding`/`PeerUpstreamBinding`/`Order` 都按租户隔离）⇒ 租户上下文来自**调用方**（命令的 `tenant_context`），⛔ 本模块不接受任何租户参数（`AGENTS.md` §4）。"""
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import DatabaseError, connection, models, transaction
from django.db.models.functions import Cast
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import peer_wakeup
from apps.business.models import (
    EmpType,
    PeerDownstreamBinding,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import (
    HALL_PULL_PAGE_TIMEOUT_SECONDS,
    PeerBusy,
    PeerHallBodyIncomplete,
    PeerRejected,
    PeerUnreachable,
    PeerUnsupported,
    fetch_applied_watermarks,
    fetch_hall_page,
    push_events,
)
from apps.business.peer_inbound import (
    SKIP_STALE,
    _local_order_for_peer_ref,
    _parse_event_time,
    _peer_counterparty_name,
    _peer_status_label,
    apply_hall_pull_rows,
    mark_missing_hall_rows_gone,
)
from apps.business.peer_sync import (
    CORE_CHAT_TARGET, DIRECT_TAKER_ASSIGN_KIND, HALL_PULL_MAX_LIMIT, HALL_TAKE_KIND, RECONCILE_MAX_ORDERS,
)

logger = logging.getLogger(__name__)

#: 失败后的重排间隔（秒）：`1m / 5m / 15m / 1h`（spec §16.6 第 3 条）。 ⚠️ 这**四档全部可达**：第 1–4 次失败各取一档，**第 5 次**失败才转 `failed` 终态
#: （共 5 次投递尝试 = 1 次首发 + 4 次重投；若按 `>= len(...)` 判终态，`3600` 那档永远用不上）。
#: ⛔ 不要按"第 n 次退避"的直觉读它——`attempts` 是**已投递次数**，取 `[attempts - 1]`。
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 3600)

#: 连续失败到这个数就**自动停绑**（spec §16.6 第 4 条 / R17：阈值写死、无界面可调）。
AUTO_REVOKE_AFTER_FAILURES: int = 10

#: 取活时打出的**租约**（秒）：投递中被占用，进程中途死掉也能在对端重投前被下一轮接管。
LEASE_SECONDS = 300

#: 一轮最多取多少条（一条事件一次 HTTP；分批避免一轮拖太长）。
BATCH_SIZE = 50
ORDER_SYNC_TARGETS = ('order', 'hall')

#: 自动停绑要写的那条审计动作（见 `apps/audit/actions.py`）。
AUTO_REVOKE_ACTION = "system.peer_binding_auto_revoke"

#: ⭐ 案 B2（D-0364）：出站同步「已同步给谁」机器行的动作码与来源名（登记见 `apps/audit/actions.py::PEER_OUTBOUND_SYNC`）。机器行照 fail-closed：
#: `actor_id=None` + 字面 `system=True`；`actor` 用「系统」（与 dailiantong / xgj_push
#: 等模块的 `SYSTEM_ACTOR` 同一口径，本模块就这一处用 ⇒ 直接字面，不另设常量）。
OUTBOUND_SYNC_LOG_ACTION = "peer.outbound_sync"

#: ⭐ 案 B2：出站同步**不写**「已同步给谁」行的 kind（噪声闸）：截图增删**双向**广播（`enqueue_screenshot_event` 沿链两个方向各一行）、高频
#: （接手类事件还成批补发既有图）、且没有业务悬念 ⇒ 写行只刷屏。⛔ 表上的 kind **不在**这个集合里（它们照写）；**表外** kind（两机版本偏斜）也不在这个集合里
#: ⇒ 走下面的 warning 分支（fail-closed，不静默）。
OUTBOUND_SYNC_SILENT_KINDS: frozenset[str] = frozenset({
    "order.screenshot.add",
    "order.screenshot.delete",
})

#: ⭐ 案 B2：出站同步事件短句的**封口小表**（本仓唯一出处；⛔ 表外 kind 不许编句子——不写行 + `logger.warning`，两机版本偏斜时不刷屏也不说假话）。
#: 短句进 summary 的尾段（「已同步给下家「X」：<短句>」），读者是本租户的人，一律从我方视角写： - `order.update` **不在表里**：它的短句带状态 label（「订单更新：代练中」），
#: 从事件载荷现取（见 `_outbound_sync_sentence`）； - `hall.claim`：我方（或我方转发的下一跳）受理**对端大厅**里的卡（收件人是上家）； - `hall.claim_ok` / `hall.claim_refused`：我方作为上家对下家抢单的**应答**。
_OUTBOUND_SYNC_SENTENCES: dict[str, str] = {
    "order.withdraw": "撤单",
    "order.assign": "指派订单",
    "order.grab": "接单",
    "hall.throw": "大厅抛卡",
    "hall.take": "大厅摘牌",
    "hall.reprice": "大厅改价",
    "hall.claim": "受理对端大厅卡",
    "hall.claim_ok": "受理下家抢单",
    "hall.claim_refused": "拒绝下家抢单",
}

#: `done` + `reason` 的两句人话：撤单被复核作废的两种情形（spec §16.2）。
STALE_VOID_STILL_TAKER_REASON = "撤单已作废：该账号仍是这张单的当前接手人"
STALE_VOID_ORDER_MISSING_REASON = "撤单已作废：本地上游台账里没有这张单"

#: 对端跳过了这条事件、却没给出可用原因时的兜底文案（`reason` 列非空，空文案在台账上排不了障）。
_SKIP_REASON_FALLBACK = "对端跳过了这条事件但没有给出原因"

#: 绑定缺失 / 非 `ACTIVE` 时把事件转终态的原因（人话；⛔ 不刷绑定健康度）。
DROPPED_BINDING_REASON = "绑定已解除/失效，不再投递"

#: ⭐ M3（D-0303）：`hall.claim` 烧完退避梯子终态 `failed` 时，写进镜像行
#: `last_error` 的人话（打手 / 客服在行上直接可见；行停在 `claiming`，由人工处置或
#: 打手重新抢单）。单独常量让用例钉字面值（E-0024）。
DEAD_CLAIM_NOTE = "接单请求未能送达上家，已停止重试；请打手重新抢单或联系客服"
#: M3 同族：claim 因**绑定失效**被丢弃（`dropped`）时的那一句（不会随重试自愈）。
DROPPED_CLAIM_NOTE = "与上家的同步通道已断开，接单请求不再投递；请打手重新抢单或联系客服"

#: 对账只看**近多少小时**内建的事件（P3 Task 8 / spec §16.8）。
#: ⚠️ 窗口是**建事件时刻**（`created_at`）而不是事件时刻（`meta.occurred_at`）：越老的台账
#: 越可能是"当时就没投出去"的历史垃圾，对账补它等于把陈年积压重新灌给下家。
RECONCILE_WINDOW_HOURS = 24

#: ⭐ B1 / M-4：`hall.*` 出站事件烧完退避梯子**终态化**之后，
#: 克隆重投的**救援事件**推迟多久再投（秒）。梯子全程约 81 分钟，救援再等 10 分钟：
#: 给「对端短暂抖动刚恢复」留一点喘息，又不至于让一张该撤的卡多挂太久。
HALL_RESCUE_DELAY_SECONDS = 600

#: 对账重排时写进 `reason` 的**人话**（台账排障直接可见）。
#: ⚠️ 单独一个常量是为了让用例能**钉住字面值**（⛔ 不靠读代码口头保证）。
RECONCILE_REQUEUE_REASON = "对账发现缺口，重投"

# ── 大厅**慢路拉取**（计划 `2026-10-25-peer-hall-pull` §5）────────────────────
#: 每绑定一次完整全量的节拍（秒）：**5 分钟**。⚠️ 判到期只认 `PeerUpstreamBinding.hall_pull_next_at`，⛔ 禁用 `now.minute % 5` 那种写法
#: （它把「每 5 分钟一次」绑在墙钟整点上，慢一轮就永远补不回来）。
HALL_PULL_PERIOD_SECONDS = 300
#: 失败退避（秒）：10 分钟后再试（⛔ 不退化成每 10 秒重试；那一档会把一个死对端
#: 变成常驻流量源）。**不计 `consecutive_failures`**：那是**投递**的自动停绑账，
#: 拉取失败不该停绑（计划 §6）。
HALL_PULL_BACKOFF_SECONDS = 600
HALL_PULL_BODY_RETRY_SECONDS = 5
# 兼容先锁镜像卡、再写绑定健康的既有 Web 投递。
HALL_PULL_RETRY_LOCK_TIMEOUT_MS = 200
#: 老对端没有 `peer-sync/hall/list/`（404/405）⇒ 停用拉取 **45 分钟**再探一次。
#: 计划 §6 的两个选项这里都落：① `PeerUnsupported` 把「路由不存在」从网络抖动里分出来；
#: ② 再给一个 TTL，到期自动重探（⛔ 不「本次进程内永久停用」——先发一台再发另一台时，前一台不会自己恢复，计划 §6 的原话）。
HALL_PULL_UNSUPPORTED_TTL_SECONDS = 45 * 60
#: 一轮最多翻多少页（计划 §5 写死 50）：撞上限 ⇒ **incomplete**，⛔ 不清标记、下一轮续传。
HALL_PULL_MAX_PAGES = 50
#: 每页条数（端点上限 200）：655 张厅 ≈ 4 页；页数上限 50 是防「对端永远给 next_cursor」。
HALL_PULL_PAGE_LIMIT = HALL_PULL_MAX_LIMIT
#: 开新页的窗口。已开始的页每个网络阶段最多等 1.5 秒，共享进程还限整页请求 3 秒。
HALL_PULL_ROUND_BUDGET_SECONDS = 2.0
#: ⭐ B2 / M-7：**连续**拉取失败到这个轮数 ⇒ 这份绑定名下 `listed` 的镜像行判 `gone`（`peer_hall.unlist_binding_mirrors`，可逆——上家恢复后
#: 「重见即复活」的既有机制照旧生效）。30 轮 × 退避 10 分钟 ≈ **5 小时**连不上：上家「变死 / 换址不删旧绑定」之后，死域名上的卡不再永久「可见 → 点了失败 → 又可见」。
#: ⛔ **不解绑**（停绑是双向 kill，恢复要重走握手；M-7 的验收口径：行 gone、绑定保留）； ⛔ **不计** `consecutive_failures`（那是**投递**的自动停绑账，拉取失败不该停绑，计划 §6）。
HALL_PULL_DEAD_AFTER_FAILURES = 30
#: ⭐ L-3（审计批）：判死收口当轮把 `hall_pull_next_at` 推远到这个秒数之后（**6 小时**），消除「拉取判 gone → 对端推送又把卡挂回来 → 下一轮拉取再判 gone」的
#: **10 分钟拍动**。判死的前提是「拉取这条路已经坏了」⇒ 在这一档里**以推送为准**，拉取先停手；6 小时后再试一次（仍坏 ⇒ 重新判死并再推 6 小时，频率从 10 分钟降到 6 小时）。
#: ⛔ 不写死成「永久停」，也⛔ 不动 `enabled` / `hall_sync_enabled`（那会让恢复要人工重开）。
HALL_PULL_DEAD_HOLD_SECONDS = 6 * 3600
#: 判死下架时写进镜像行 `last_error` 的**人话**（打手 / 客服在行上直接可见）。单独常量
#: 让用例钉字面值（E-0024）；`pull_dead` 这个机器词留在文案里，巡检 / 台账 grep 得到。
PULL_DEAD_NOTE = "上家大厅连续拉取失败已自动下架（pull_dead）；上家恢复后会自动重新上架"
#: 游标 + 本轮已见卡号的**进程内存**状态，键 = `(schema, 绑定 pk)`。
#: 跨 worker 轮续传（重启丢失可接受：下一轮从头发车，只是多花几页）。
_HALL_PULL_STATE: dict[tuple[str, int], dict] = {}


def reset_hall_pull_state() -> None:
    """清空进程内的拉取续传状态（用例夹具用；生产不需要手工调用）。"""
    _HALL_PULL_STATE.clear()


def clear_hall_pull_marker(binding, *, expected, now) -> bool:
    """**条件更新**清开关标记，并把下一次节拍推到 5 分钟后（防 ABA，计划 §5）。 `expected` = 本轮开头读到的 `hall_pull_requested_at`：
    - 非空 ⇒ `WHERE hall_pull_requested_at = expected`； - 空 ⇒ `WHERE hall_pull_requested_at IS NULL`（⛔ 不许无条件清——本轮跑的时候
    开关又被翻了一次、标记换了新值，那是**另一次**欠账，不能被这一次顺手抹掉）。回 `True` = 这一行真的被更新（标记归本次所有）；`False` = 标记已被并发换掉，尊重那一次。"""
    qs = PeerUpstreamBinding.objects.filter(pk=binding.pk)
    if expected is None:
        qs = qs.filter(hall_pull_requested_at__isnull=True)
    else:
        qs = qs.filter(hall_pull_requested_at=expected)
    return bool(
        qs.update(
            hall_pull_requested_at=None,
            hall_pull_next_at=now + timedelta(seconds=HALL_PULL_PERIOD_SECONDS),
        )
    )


def _due_hall_pull_binding(now, *, binding_pk=None):
    """本轮该拉哪一条绑定：启用 + 未解绑 + 接收开关开着 + 到期（`NULL` = 立即到期）。"""
    due = (
        PeerUpstreamBinding.objects.filter(
            deleted_at__isnull=True,
            enabled=True,
            hall_sync_enabled=True,
        )
        .filter(
            models.Q(hall_pull_next_at__isnull=True)
            | models.Q(hall_pull_next_at__lte=now)
        )
        .order_by("pk")
    )
    if binding_pk is not None:
        due = due.filter(pk=binding_pk)
    return due.first()


def _schedule_hall_pull(binding, *, now, seconds: int) -> None:
    """把这条绑定的下一次拉取推到 `now + seconds`（⛔ 不动开关标记那一列）。"""
    PeerUpstreamBinding.objects.filter(pk=binding.pk).update(
        hall_pull_next_at=now + timedelta(seconds=seconds)
    )


def _hall_retry_target(binding):
    return PeerUpstreamBinding.objects.filter(
        pk=binding.pk, deleted_at__isnull=True, enabled=True, hall_sync_enabled=True,
        hall_pull_requested_at=binding.hall_pull_requested_at,
        hall_pull_next_at=binding.hall_pull_next_at,
    )


class _HallPullLockContended(Exception):
    """本地写入让位给已获锁的请求，等下一轮重新检查。"""


@contextmanager
def _guard_hall_retry_writes(binding, *, guard):
    if not guard:
        yield True
        return
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT setting::integer FROM pg_settings WHERE name = 'lock_timeout'")
                previous_ms = cursor.fetchone()[0]
                limit_ms = min(previous_ms or HALL_PULL_RETRY_LOCK_TIMEOUT_MS, HALL_PULL_RETRY_LOCK_TIMEOUT_MS)
                cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{limit_ms}ms"])
            yield _hall_retry_target(binding).select_for_update().exists()
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout', %s, true)", [f"{previous_ms}ms"])
    except DatabaseError as exc:
        if getattr(exc.__cause__, "sqlstate", None) in ("55P03", "40P01"):
            raise _HallPullLockContended from exc
        raise


def _stamp_hall_pull_ok(binding, *, now) -> None:
    """⭐ B2 / M-7+L-11：**完整走完一轮拉取** ⇒ 盖章 + 连败清零（成功就是这条判据）。"""
    PeerUpstreamBinding.objects.filter(pk=binding.pk).update(
        hall_pull_last_ok_at=now, hall_pull_fail_count=0,
    )


def _bump_hall_pull_failure(binding, *, now) -> int:
    """⭐ B2 / M-7：拉取失败 ⇒ 连败 +1（`F()` 表达式防两台机器并发丢计数），回新计数。 ⛔ 刻意不写 `last_error` / `last_error_at` / `consecutive_failures`：那是**入站健康**
    与**投递**的账（`_record_inbound_health` / `_record_failure` 写），拉取再记一本会把三本账搅成一团；拉取失败的可见性 = 本计数 + 巡检的 `hall_pull_last_ok_at` +
    每次失败已有的 `logger.warning`。"""
    PeerUpstreamBinding.objects.filter(pk=binding.pk).update(
        hall_pull_fail_count=models.F("hall_pull_fail_count") + 1,
    )
    return (
        PeerUpstreamBinding.objects.filter(pk=binding.pk)
        .values_list("hall_pull_fail_count", flat=True)
        .first()
        or 0
    )


def _maybe_pull_dead(binding, *, fail_count: int, now) -> None:
    """连败到阈值 ⇒ 该绑定名下 `listed` 的镜像行判 `gone`（复用页面下架的 mark 路径）。 ⚠️ 复用 `peer_hall.unlist_binding_mirrors`（`note=PULL_DEAD_NOTE`）：它只翻 `listed`
    （`gone` / `claiming` / `promoted` 不碰）⇒ 阈值之后每一轮失败再进来也是幂等的（第一次翻完就没有 `listed` 行了）；每张**真的**翻成 `gone` 的行照旧给下家补发 `hall.take`（多跳状态跟随，与关开关 / 解绑同一份口径）。
    ⚠️ 上家恢复后「重见即复活」：`apply_hall_pull_rows` 的复活闸是 `gone_at <= round_started_at`，判死下架的 `gone_at`（失败时刻）必然早于恢复后那轮的起点 ⇒ 复活照常生效，连败计数由 `_stamp_hall_pull_ok` 清零。"""
    if fail_count < HALL_PULL_DEAD_AFTER_FAILURES:
        return
    from apps.business import peer_hall

    unlisted = peer_hall.unlist_binding_mirrors(
        binding, note=PULL_DEAD_NOTE, now=now
    )
    # ⭐ 判死的那一刻把进程内的**续传状态**丢掉：复活闸（`apply_hall_pull_rows` 的 `gone_at <= round_started_at`）比的是「这一轮发请求前的起点」，而判死写下的
    # `gone_at` 晚于老状态的起点（状态从连败第一轮就一直在攒）——不丢的话，上家恢复后「重见即复活」会被这个**过期起点**挡住、卡永远救不回来。丢掉 ⇒ 下一轮从头发车、
    # 起点新鲜（晚于判死时刻）⇒ 复活照常生效。
    _HALL_PULL_STATE.pop((connection.schema_name, binding.pk), None)
    # ⭐ L-3（审计批）：判死当轮就把下一次拉取**推远**（`run_hall_pull` 失败支
    #    本来只排 10 分钟后——那会让「推送复活 / 拉取再判死」每 10 分钟拍一次）。
    #    这一档以**推送**为准：拉取停手 6 小时，别再拿一条坏掉的路去盖掉推送带来的事实。
    _schedule_hall_pull(binding, now=now, seconds=HALL_PULL_DEAD_HOLD_SECONDS)
    logger.warning(
        "大厅拉取连续失败 %s 轮判死：binding=%s 下架在挂镜像卡 %s 张（绑定保留，恢复后重见即复活）",
        fail_count, binding.pk, unlisted,
    )


def run_hall_pull(*, now=None, monotonic=None, retry_binding_pk=None) -> dict:
    """按游标处理本租户的一条到期绑定，短退避时返回补试时间和绑定。
    只有完整且无落地失败的补拉才确认缺失卡片、清标记和清零连败。"""
    import time as _time

    use_wall_clock = now is None
    now = now if now is not None else timezone.now()
    monotonic = monotonic if monotonic is not None else _time.monotonic
    summary = {
        "bindings": 0, "pages": 0, "rows": 0, "gone": 0,
        "complete": False, "unsupported": False, "errors": 0,
    }
    binding = _due_hall_pull_binding(now, binding_pk=retry_binding_pk)
    if binding is None:
        return summary
    summary["bindings"] = 1
    try:
        return _run_hall_pull_pages(
            binding, summary, now=now, monotonic=monotonic,
            retry_clock=timezone.now if use_wall_clock else lambda: now,
        )
    except _HallPullLockContended:
        _HALL_PULL_STATE.pop((connection.schema_name, binding.pk), None)
        summary["errors"] += 1
        logger.warning("大厅补试落库遇到并发修改：binding=%s（下一轮从头拉取）", binding.pk)
        return summary


def _run_hall_pull_pages(binding, summary, *, now, monotonic, retry_clock):
    marker_seen = binding.hall_pull_requested_at
    key = (connection.schema_name, binding.pk)
    state = _HALL_PULL_STATE.get(key)
    if state is not None and state["marker_at"] != marker_seen:
        # 本轮跑的时候开关又被翻了一次 ⇒ 那是**另一次**全量欠账，从头开始（⛔ 复用旧 seen）。
        state = None
    if state is None:
        state = {
            "cursor": None,
            "seen": set(),
            "started_at": now,       # ⭐ 复活闸的整轮起点：**发请求前**一次，所有页共用
            "marker_at": marker_seen,
            "body_retry_pending": False,
            "guard_writes": False,
            "row_failure_seen": False,
        }
        _HALL_PULL_STATE[key] = state
    budget_started = monotonic()
    pages = 0
    failures = 0
    reached_end = False

    while pages < HALL_PULL_MAX_PAGES:
        if monotonic() - budget_started >= HALL_PULL_ROUND_BUDGET_SECONDS:
            break
        try:
            page = fetch_hall_page(
                binding.peer_base_url,
                binding.token_out,
                cursor=state["cursor"],
                limit=HALL_PULL_PAGE_LIMIT,
                timeout=HALL_PULL_PAGE_TIMEOUT_SECONDS,
            )
        except PeerUnsupported:
            summary["unsupported"] = True
            state["body_retry_pending"] = False
            with _guard_hall_retry_writes(binding, guard=state["guard_writes"]) as current:
                if not current:
                    return summary
                _schedule_hall_pull(binding, now=now, seconds=HALL_PULL_UNSUPPORTED_TTL_SECONDS)
            logger.warning(
                "大厅拉取：对端没有拉取端点，按老上家处理 binding=%s 下次=%s",
                binding.pk, now + timedelta(seconds=HALL_PULL_UNSUPPORTED_TTL_SECONDS),
            )
            return summary
        except PeerRejected:
            if not state["body_retry_pending"]:
                raise
            state["body_retry_pending"] = False
            summary["errors"] += 1
            with _guard_hall_retry_writes(binding, guard=True) as current:
                if current:
                    _schedule_hall_pull(binding, now=now, seconds=HALL_PULL_BACKOFF_SECONDS)
                    logger.warning("大厅补试被对端拒绝：binding=%s（%s 秒后再试）", binding.pk, HALL_PULL_BACKOFF_SECONDS)
            return summary
        except PeerUnreachable as exc:
            summary["errors"] += 1
            if isinstance(exc, PeerHallBodyIncomplete) and not state["body_retry_pending"]:
                summary["pages"] = pages
                with _guard_hall_retry_writes(binding, guard=True) as current:
                    if current:
                        retry_at = retry_clock() + timedelta(seconds=HALL_PULL_BODY_RETRY_SECONDS)
                        PeerUpstreamBinding.objects.filter(pk=binding.pk).update(hall_pull_next_at=retry_at)
                if current:
                    state["body_retry_pending"] = True
                    state["guard_writes"] = True
                    summary["retry_at"] = retry_at
                    summary["retry_binding_pk"] = binding.pk
                    logger.warning("大厅回包中断：binding=%s（%s 秒后补试一次）", binding.pk, HALL_PULL_BODY_RETRY_SECONDS)
                return summary
            state["body_retry_pending"] = False
            with _guard_hall_retry_writes(binding, guard=state["guard_writes"]) as current:
                if not current:
                    return summary
                _schedule_hall_pull(binding, now=now, seconds=HALL_PULL_BACKOFF_SECONDS)
                _maybe_pull_dead(binding, fail_count=_bump_hall_pull_failure(binding, now=now), now=now)
            logger.warning(
                "大厅拉取失败：binding=%s %s（%s 秒后再试）",
                binding.pk, exc, HALL_PULL_BACKOFF_SECONDS,
            )
            return summary
        state["body_retry_pending"] = False
        pages += 1
        try:
            with _guard_hall_retry_writes(binding, guard=state["guard_writes"]) as current:
                if not current:
                    summary["pages"] = pages - 1
                    return summary
                # 拉取中关开关或停绑，不能把刚下架的卡拉回来。
                if not state["guard_writes"] and not PeerUpstreamBinding.objects.filter(
                    pk=binding.pk, deleted_at__isnull=True, enabled=True, hall_sync_enabled=True,
                ).exists():
                    break
                if not page["shared"]:
                    summary["pages"] = pages
                    _schedule_hall_pull(binding, now=now, seconds=HALL_PULL_PERIOD_SECONDS)
                    return summary
                outcome = apply_hall_pull_rows(
                    binding, page["rows"], now=now, round_started_at=state["started_at"],
                )
        except _HallPullLockContended:
            raise
        except Exception:  # noqa: BLE001 - 兜底面（M-5）：整轮按失败退避，⛔ 不裸逃
            logger.exception(
                "大厅拉取落地失败（本轮按有失败处理）：binding=%s page=%s",
                binding.pk, pages,
            )
            failures += 1
            state["row_failure_seen"] = True
            summary["errors"] += 1
            break
        summary["rows"] += outcome["applied"]
        failures += outcome["failures"]
        state["row_failure_seen"] |= bool(outcome["failures"])
        state["seen"].update(outcome["seen"])
        state["cursor"] = page["next_cursor"]
        if page["next_cursor"] is None:
            reached_end = True
            break

    summary["pages"] = pages
    rescan = reached_end and state["guard_writes"] and state["row_failure_seen"]
    complete = reached_end and failures == 0 and not rescan
    if not complete:
        # ⭐ 撞预算 / 页数上限 / 有失败：**不清标记**，下一轮接着走（计划 §5 / 三审 M4）。
        with _guard_hall_retry_writes(binding, guard=state["guard_writes"]) as current:
            if current:
                _schedule_hall_pull(binding, now=now, seconds=0)
        if rescan:
            _HALL_PULL_STATE.pop(key, None)
        return summary
    with _guard_hall_retry_writes(binding, guard=state["guard_writes"]) as current:
        if not current:
            return summary
        summary["gone"] = mark_missing_hall_rows_gone(binding, seen=state["seen"], now=now)
        clear_hall_pull_marker(binding, expected=marker_seen, now=now)
        _stamp_hall_pull_ok(binding, now=now)
    _HALL_PULL_STATE.pop(key, None)
    summary["complete"] = True
    return summary




#: 投递失败的两大类：自动停绑的计数**只认连接类**。 - `FAILURE_CONNECTION` = 对端**没回话**（`PeerUnreachable`：超时 / DNS / 连接被拒 /
#: 回包不是信封）——这才是计数要抓的「失联」； - `FAILURE_BUSINESS` = 对端**收到了并明确表态**（`PeerRejected` 信封业务码非 0；
#: 或按**本事件**硬跳、`skipped_details` 里非 `stale` 码，如 `not_your_order`） ⇒ 链路是通的，⛔ 不计入停绑计数（否则一场数据层面的分歧也能把绑定双向 kill 掉、逼人重走握手）。
FAILURE_CONNECTION = "connection"
FAILURE_BUSINESS = "business"
LOCK_BUSY_REASON = "对端订单正在处理，稍后自动重试"

#: 上行回传的**超龄重排**阈值（小时）：终态 `failed` 的上行行，
#: 创建超过这么久才兜底重排（正常梯子全程约 81 分钟，24 小时给足了余量）。
UPSTREAM_RESCUE_MIN_AGE_HOURS = 24

#: 上行重排写进事件 `reason` 的**人话**（单独常量：用例钉字面值，⛔ 不靠读代码口头保证）。
UPSTREAM_RESCUE_REASON = "上行回传超龄，自动重排一次"

#: 上行重排写进绑定失败台账（`last_error`）的**人话**（同上）。
UPSTREAM_RESCUE_LEDGER = "上行回传超龄未送达，已自动重排一次"


def _auto_revoke_reason(consecutive_failures: int) -> str:
    """自动停绑写进 `last_error` / 操作日志的**人话**（spec §16.6 第 4 条）。"""
    return f"连续失败 {consecutive_failures} 次已自动停止推送"


def _earlier_chat_events(*, role, binding_id, target_ref, event_id):
    return PeerSyncEvent.objects.filter(
        direction="out", status=PeerSyncEvent.Status.PENDING, target_type=CORE_CHAT_TARGET,
        binding_role=role, binding_id=binding_id, target_ref=target_ref, pk__lt=event_id,
    )


def claim_due_events(*, now, previous_deadlines=None, batch_size=BATCH_SIZE, orders_only=False) -> list[int]:
    """取一批到点的出站事件并打租约，回 pk 列表（spec §16.6 第 2 条 / §7.7）。 ⛔ **事务里不发 HTTP**：调用方（`run_round`）拿到 pk 之后、事务**外**才逐条投递
    （经 `_attempt`，见下）。 ⚠️ `skip_locked=True` 是承重的（spec §7.7）：多台应用服务器共跑一条 cron 时，
    被别的进程锁住的行**跳过**而不是排队 —— 否则两台机器会在同一行上串行等待。"""
    due = PeerSyncEvent.objects.filter(
        direction="out", status=PeerSyncEvent.Status.PENDING,
    ).filter(models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now))
    if orders_only:
        due = due.filter(target_type__in=ORDER_SYNC_TARGETS)
    else:
        previous = _earlier_chat_events(
            role=models.OuterRef("binding_role"), binding_id=models.OuterRef("binding_id"),
            target_ref=models.OuterRef("target_ref"), event_id=models.OuterRef("pk"),
        )
        due = due.filter(~models.Q(target_type=CORE_CHAT_TARGET) | ~models.Exists(previous))
    with transaction.atomic():
        rows = list(
            due.select_for_update(skip_locked=True)
            .annotate(_ready_since=models.Case(
                models.When(reason=LOCK_BUSY_REASON, then=models.F('next_attempt_at')),
                default=models.F('created_at'), output_field=models.DateTimeField(),
            ))
            .order_by("_ready_since", "created_at", "id")[:batch_size]
        )
        if previous_deadlines is not None:
            previous_deadlines.update((row.pk, row.next_attempt_at) for row in rows)
        for row in rows:
            row.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        if rows:
            PeerSyncEvent.objects.bulk_update(rows, ["next_attempt_at"])
    return [row.pk for row in rows]


def _hard_skip_reason(data, target_ref):
    """取**本事件**在对端被硬跳过的原因（spec §16.5）；回 `None` = 这次投递算成功。 ⛔ **按 `code` 分流、⛔ 不按 `reason` 文本**：`reason` 是对端给人读的中文（随时会改），
    `code` 才是双方约定的机器契约（`SKIP_*` 常量在 `apps/business/peer_inbound.py`）。四类"算成功"里有两类是**兼容旧对端**：响应不是 dict、没有 `skipped_details`
    （或它不是 list）——P1 的对端不回这些键；而既有投递用例把 `push_events` 打成裸 `MagicMock`，那是"没定义返回值"，不是"对端报了失败"。"""
    if not isinstance(data, dict):
        return None
    details = data.get("skipped_details")
    if not isinstance(details, list):
        return None
    for entry in details:
        # 一批里可能混着别的对端单号的明细 ⇒ 只认本事件那一条（一次只投一条，P1 现状）。
        if not isinstance(entry, dict) or entry.get("target_ref") != target_ref:
            continue
        if entry.get("code") == SKIP_STALE:
            # 对端已有更新的 ⇒ 良性（spec §16.5）：算成功，⛔ 不在台账上留失败原因。
            return None
        reason = entry.get("reason")
        # `reason` 列非空 ⇒ 对端给了不可用形状时兜一句，⛔ 不把 `None` 写进列。
        return reason if isinstance(reason, str) and reason else _SKIP_REASON_FALLBACK
    return None


def _recheck_withdraw(target_ref: str, *, account_id) -> tuple[bool, str]:
    """撤单**投递前复核**（spec §16.2）。回 `(是否该发, 作废时的人话原因)`。 ⭐ 承重不是防御性写法：撤单事件可能与"先清接手人、再把同一个账号派回来"在**同一个 pending 窗口**里先后落库 ⇒ 投递那一刻若不复核，下家会收到一条**假的**"这单不再属于你"。 - 单**不存在** ⇒ 作废（没有可作废的对象，撤单无从谈起）； - 单**已软删** ⇒ **发**（删单正是要通知下家的那件事）；
    - 当前 MAIN 接手人**仍是**这个绑定账号 ⇒ 作废（⛔ 不发）； - 其余（换人 / 无 MAIN 绩效行 / 接手人为空）⇒ 发。 ⚠️ 用 `Order.all_objects`（包含已软删，spec §4.3-C4）；MAIN 绩效行用**容错查询** ——缺行是真实数据形态（不经录单端点落库的旧单 / ORM 夹具）， `main_performance_of` 缺行时**响亮抛**，会让一轮 cron 整个失败
    （与 `order_views` 删单取原接单人时同一口径）。 ⭐：事件的身份键 `target_ref` 改成了**来源单号**（新入队的事件），而库里还躺着旧口径（内置号）的在飞行 ⇒ 本复核必须两把键都认（唯一解析口 `peer_inbound._local_order_for_peer_ref`）。⛔ 只按 `Order.order_no` 查的后果是 **所有带来源号的撤单被静默作废**（`STALE_VOID_ORDER_MISSING_REASON`），下家永远收不到「这单不再属于你」。"""
    order = _local_order_for_peer_ref(target_ref, include_deleted=True)
    if order is None:
        return False, STALE_VOID_ORDER_MISSING_REASON
    if order.deleted_at is not None:
        return True, ""
    perf = (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .select_related("user")
        .first()
    )
    if perf is not None and perf.user_id == account_id:
        return False, STALE_VOID_STILL_TAKER_REASON
    return True, ""


def _pending_event_match(event):
    return dict(
        pk=event.pk, status=PeerSyncEvent.Status.PENDING, next_attempt_at=event.next_attempt_at,
        idempotency_key=event.idempotency_key,
    )


def _mark_event_done(event, *, now, reason="") -> bool:
    """Keep an already-voided row or a renewed lease intact."""
    return bool(PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(
        status=PeerSyncEvent.Status.DONE, attempts=event.attempts, reason=reason[:255],
        processed_at=now, next_attempt_at=None,
    ))


def _mark_done(event, binding, *, now) -> bool:
    if not _mark_event_done(event, now=now):
        return False
    binding.last_ok_at = now
    binding.consecutive_failures = 0
    binding.save(update_fields=["last_ok_at", "consecutive_failures"])
    return True


def _outbound_sync_sentence(event) -> str | None:
    """出站事件的**短句**（summary 尾段）：封口小表 + `order.update` 的状态 label。 ⚠️ `order.update` 不进 `_OUTBOUND_SYNC_SENTENCES`（它的短句带状态），在这里
    从载荷现取；状态 label 唯一出处 `_peer_status_label`（= `OrderStatus.label`）。回 `None` = 表外 kind（fail-closed：调用方不写行、打 warning）；
    回 `""` 不会发生（截图片种在 `OUTBOUND_SYNC_SILENT_KINDS`、先被调用方挡掉）。"""
    kind = str(event.kind)
    if kind == "order.update":
        payload = event.payload
        status = payload.get("status") if isinstance(payload, dict) else None
        return f"订单更新：{_peer_status_label(status)}"
    return _OUTBOUND_SYNC_SENTENCES.get(kind)


def _record_outbound_sync_log(event, binding, role) -> None:
    """真投递成功 ⇒ 「**已同步给谁**」机器行（案 B2 / D-0364；动作码 `peer.outbound_sync`，模板见 `apps/audit/actions.py::PEER_OUTBOUND_SYNC`）。 ## 判据与口径（写点在 `_attempt` 落 done 处，只有这一处） - **仅 `done` 且 `reason==""`**：有意作废（`stale-void` 的 `done` + 人话原因）、 `failed` / 重试中间态都不写——防 81 分钟退避梯子把时间线刷成瀑布。（调用点在 `_mark_done` 之后；这里再判一次 `reason` 是把「什么行不写」
    钉在本函数自己身上，⛔ 不依赖调用顺序。） - **噪声闸**：截图增删（`OUTBOUND_SYNC_SILENT_KINDS`）不写、**不警告**（它们是已知的低信息量 kind，警告才是噪声）；**表外** kind 不写 + `logger.warning` （fail-closed：两机版本偏斜时对端新 kind 在 journald 里可查，⛔ 不静默吞）。 - **双列口径**：`order_no` 与 `source_order_no` **两列都写** （照 `peer_inbound._record_peer_sync` 同款）——时间线两臂按
    `order_no=本地单号` **或** `source_order_no=来源号` 查，⛔ 只写 `target_ref` （那是来源号 / 对端号）两臂都命中不了。解析走全仓唯一口 `_local_order_for_peer_ref`；解析不到（对端指认的不是本租户任何一张本地单，如纯中间层的转发卡）⇒ 没有可挂的时间线锚点，不写行。 - **方向**按 `role`（`_binding_for_event` 的回值）：`downstream` = 我方是上家、收件人是**下家**；`upstream` = 反之。对方可读名复用 `_peer_counterparty_name` （⛔ 不新造第二套名字）。 - ⛔ **本行与 done 记账不同事务**：`_attempt` 本就没有 `atomic` 包裹， `_mark_done` 的两表写也各自 `save` ⇒ 极端窗口下可能「事件已 done、本行缺失」。这是审计线、不是业务写入，照旧可（⭐ 与 `_mark_done` 的既有口径一致，如实声明）。"""
    if event.reason or event.target_type in ("chat", CORE_CHAT_TARGET):
        return
    sentence = _outbound_sync_sentence(event)
    if sentence is None:
        kind = str(event.kind)
        if kind not in OUTBOUND_SYNC_SILENT_KINDS:
            logger.warning(
                "出站同步事件没有登记短句，不写「已同步给谁」行 kind=%s ref=%s 绑定=%s",
                kind, event.target_ref, event.binding_id,
            )
        return
    order = _local_order_for_peer_ref(str(event.target_ref), include_deleted=True)
    if order is None:
        return
    record(
        log_type=ACTION_LOG_TYPES[OUTBOUND_SYNC_LOG_ACTION],
        action=OUTBOUND_SYNC_LOG_ACTION,
        # 机器来源（常驻同步进程投递）⇒ `actor_id` 留空 + 字面 `system=True`（fail-closed）。
        actor="系统",
        system=True,
        target_kind="order",
        # 对方可读名（模板的 `{target_name}`）：与入站行同一个出处，⛔ 不新造第二套。
        target_name=_peer_counterparty_name(binding),
        order_no=order.order_no,
        # 「来源单号」按订单填（同步来的单在建单那一刻就写好了这一列）。
        source_order_no=order.source_order_no or "",
        game_name=(order.game.name if order.game_id else "") or "",
        game_role_name=order.game_role_name or "",
        # `{to_value}` 装**方向词**（本动作的 to_value 不是状态；见模板常量处的说明）。
        to_value="下家" if role == "downstream" else "上家",
        reason=sentence,
    )


def _record_failure(event, binding, failure: str, *, now) -> bool:
    """失败的**事件侧 + 台账侧**记账：原因写回事件、失败台账写绑定两列。回 `True` = 事件仍在 `pending`、失败已记；`False` = 事件已被别处**作废**、一个字都没写。 ⭐ Z6（审计批次二）**「作废不复活」**：事件侧从 `event.save(...)` 改成 **条件更新**（`pk` ＋ `status=pending`）。投递是「读事件 → 发 HTTP → 记账」三步，中间那一拍可能很长（对端超时）⇒ 期间别处（人工处置 / 对账终态化 / admin 作废）把这条事件置成 `done` 是完全可能的。旧写法把内存里那份 `pending` 的 `status` 原样写回，
    等于**把一条已作废的事件复活成待投递**（还会把作废原因擦成一句失败原因）。条件更新命中 0 行 ⇒ 事件已被作废 ⇒ 记账整体放弃（连绑定台账都不写：这次投递的结果已经被别处的判决覆盖，写 `last_error` 等于替一次没发生的失败记账，同 `stale-void` 不刷健康度那条纪律）。 ⭐ PW-01（修复线）：`consecutive_failures` 的累加与自动停绑**不在本函数**—— 旧口径按「每次 HTTP 尝试」+1，对端短暂失联 + 一批 10 条 pending ⇒ 第一轮（约 10 秒）就把绑定打成 REVOKED（停绑是**双向 kill**：认证 filter 只认 ACTIVE，下家回传同样 401，
    恢复只能重走握手）。新口径是**按轮**的：收口见 `_close_round`（入站侧先例 = 「一批硬跳只 +1」，`test_peer_binding_api.py::…:: test_the_error_names_the_hard_skip_count_and_the_first_reason`）。 ⚠️ `reason` 是 `CharField(255)` ⇒ **必须**截断，否则 PostgreSQL 直接报 `value too long for type character varying(255)`（那会变成一次未预期异常）。 `last_error` 是 `TextField` ⇒ 不截断。 ⚠️ 只写台账两列：计数是**轮级**的（`_close_round` 从库里重取再写），而每个 `_attempt` 各持有自己那份绑定实例——在这里也写计数会把它刷回实例被取出来时的旧值。"""
    event.reason = failure[:255]
    updated = PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(reason=event.reason)
    if not updated:
        logger.info("事件已被作废，跳过失败记账 pk=%s kind=%s", event.pk, event.kind)
        return False
    binding.last_error = failure
    binding.last_error_at = now
    binding.save(update_fields=["last_error", "last_error_at"])
    return True


@transaction.atomic
def _close_round(role: str, binding_pk: str, *, now, since=None, nowait=False) -> bool:
    """一轮的**计数收口**（PW-01）：整轮连接类失败 ⇒ 计数 +1 **恰好一次**；达阈值 ⇒ 停绑。调用契约（`run_round` 与 `deliver_one` 都走这里；「一轮」= 一个绑定把当批事件处理完）： - 轮内该绑定**有过任一成功** ⇒ 调用方不进来（计数已由 `_mark_done` 在事件时点清零）； - 轮内**只有业务类失败**（或 noop / dropped / stale-void——后三类按既有纪律
    ⛔ 一律不碰健康列）⇒ 也不进来：业务类说明对端活着，计数只抓「失联」； - 轮内**有连接类失败且无成功** ⇒ +1 **一次**（⛔ 不是按尝试 +N）；达到 `AUTO_REVOKE_AFTER_FAILURES` ⇒ 停绑 + 一条操作日志。 ⚠️ 停绑**只对 `role == "downstream"`**（理由同旧口径）：`PeerUpstreamBinding` 没有
    `state` / `revoked_at` 两列（写它就是 `AttributeError`），且"上行连续失败要不要停推" 业务方从没裁定过（Task 9 的残差）⇒ ⛔ 不擅自给上行加停绑。 ⚠️ 绑定行从库里**重取**：`_attempt` 每个事件各取一份实例、各写各的 `update_fields`；收口只写自己管的列（计数 + 停绑时的人话台账三列），⛔ 不把实例上别的列刷回去。 ⚠️ 由此停绑发生在**轮末**：同一轮里同绑定的后续事件**不再中途 drop**（旧口径会在轮中停绑、把同批后面的事件打成 dropped）——本批事件按停绑前的状态投完，下一轮才掉。"""
    if role == "downstream":
        binding = PeerDownstreamBinding.objects.select_for_update(nowait=nowait).filter(pk=int(binding_pk)).first()
    elif role == "upstream":
        binding = PeerUpstreamBinding.objects.select_for_update(nowait=nowait).filter(pk=int(binding_pk)).first()
    else:
        raise ValueError(f"未知的绑定角色：{role!r}")
    if _binding_is_invalid(binding, role):
        return False
    if since is not None and binding.last_ok_at is not None and binding.last_ok_at >= since:
        return False
    binding.consecutive_failures += 1
    fields = ["consecutive_failures"]
    revoked = False
    if role == "downstream" and binding.consecutive_failures >= AUTO_REVOKE_AFTER_FAILURES:
        revoked = True
        binding.state = PeerDownstreamBinding.State.REVOKED
        binding.revoked_at = now
        # 台账留**人话**（spec §16.6 第 4 条）：把对端原文换掉，否则运维只看到一条旧报错。
        binding.last_error = _auto_revoke_reason(binding.consecutive_failures)
        fields += ["state", "revoked_at", "last_error"]
    binding.save(update_fields=fields)
    if revoked:
        # 一条**操作日志**（spec §16.6 第 4 条）：机器来源 ⇒ `actor` 与 `actor_id` 留空，
        # 文案由 `apps/audit/actions.py` 的模板渲染，⛔ 调用方不拼串（`record()` 没有
        # `summary` 形参）。⛔ 两个令牌与握手密码一律不进模板。
        record(
            log_type=ACTION_LOG_TYPES[AUTO_REVOKE_ACTION],
            action=AUTO_REVOKE_ACTION,
            actor="",
            # ⭐ 常驻同步来源（自动停绑）⇒ **显式**声明机器来源。
            system=True,
            target_kind="peer_binding",
            target_id=str(binding.pk),
            # 下家绑定表里没有"名字"，只有域名 ⇒ 就写域名（页面那条绑定动作同形）。
            target_name=binding.peer_base_url,
            reason=_auto_revoke_reason(binding.consecutive_failures),
        )
    return revoked


def _mark_event_dropped(event, *, reason=DROPPED_BINDING_REASON) -> bool:
    return bool(PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(
        status=PeerSyncEvent.Status.FAILED, reason=reason[:255], next_attempt_at=None,
    ))


def _note_dead_claim(event, *, note: str) -> None:
    """`hall.claim` 走到**终态**（烧完重试 / 绑定失效被丢弃）⇒ 把线索写回镜像行（M3）。修前病根：claim 事件终态 `failed` 后镜像行**永久停在 `claiming`**，行上一个字的线索都没有——打手看「接单中」、客服查台账才知道死了。写 `last_error`（行上可见）
    ⛔ **不改状态**：claimed 的事实还在（上家可能已收到并指派——终态 failed 只说明「我方不再重试」，不说明「对方没收到」）⇒ 状态收口仍归 `order.assign` / take / refused；若上家真没收到，行由人工按这条线索处置。
    ⚠️ 只认 `binding_role="upstream"` + `kind="hall.claim"` 且行仍在 `claiming` （条件更新；已被 assign / refused / take 收口的行一个字不覆盖）。 ⚠️ `binding_id` 是**字符串**列（两张表共用），解析失败按「没有这样的行」处理 ——这一步是纯辅助留痕，⛔ 不让它把投递循环打挂。"""
    if event.binding_role != "upstream" or event.kind != "hall.claim":
        return
    from apps.business.models import PeerHallMirror

    try:
        binding_pk = int(event.binding_id)
    except (TypeError, ValueError):
        return
    PeerHallMirror.objects.filter(
        peer_upstream_binding_id=binding_pk,
        peer_order_no=event.target_ref,
        status=PeerHallMirror.Status.CLAIMING,
    ).update(last_error=note)


def _rescue_hall_event(event, *, now) -> bool:
    """⭐ B1 / M-4：`hall.*` 出站事件烧完退避梯子终态化时， **克隆重投一条救援事件**（每条至多救一次），回是否真的排了一条。修前病根：`hall.throw` / `hall.take` / `hall.reprice` / `hall.claim*` 不参与对账（`run_reconcile` 显式 exclude `hall.`——它们不落订单与水位，水位查询必回 null）⇒ 烧完梯子终态 `failed` 之后**没有任何收口**：漏一条 `hall.take` = 下家那张卡永久挂错（纯推送对端连拉取兜底都没有）。本函数在终态化的那一刻补一条 `pending` 的事件行，
    走**同一条投递路**（`claim_due_events` 正常取活 → `_attempt` 正常投；⛔ 不开特殊通道、 ⛔ 不在投递循环里直接发 HTTP）。判据（每条都有代价，⛔ 不要"顺手放宽"）： 1. 只认 `kind` 以 `hall.` 开头的出站事件：订单类事件的对账 / 上行超龄救援（`_rescue_stale_upstream_events`）已经各管各的，⛔ 不重复兜底； 2. `meta.rescued` 非**真**才救：救援事件自己带着 `rescued=true` ⇒ 它再烧完梯子就是**真终态**（台账上是一条 `failed`，等人工），⛔ 不变成无限重试。
    `attempts=0` 起步（与下行对账重排同一条理由：不清零的话下一投立刻又终态）； 3. `meta` 原样继承（含 `occurred_at`）再盖 `rescued=true`——`occurred_at` 是接收侧幂等 / 迟到闸（A3 的 `gone_at` 比对、`hall.claim` 重放应答）的判据，换新时刻等于把「旧事实」伪装成「新事实」，⛔ 不动它； 4. 只挂在「烧完梯子」的终态支（`_attempt` 里 `attempts > len(RETRY_BACKOFF_SECONDS)` 那一支）：绑定失效的 `dropped` **不救**——重排了也会再被 drop（与 `_rescue_stale_upstream_events` 第 2 条同一条纪律，⛔ 不开「救援 → drop → 再救」的死循环）。 ⚠️ 幂等键用随机值（接收侧从不读它；真正的去重是接收侧按 `occurred_at` / 镜像行状态裁决，at-least-once 是良性代价）。 ⚠️ 本函数只 `create` 一行，不改原事件：原事件留在 `failed` 终态（台账事实），救援事件是**另一行**（`meta.rescued=true` 可辨）。"""
    if not str(event.kind).startswith("hall."):
        return False
    meta = dict(event.meta) if isinstance(event.meta, dict) else {}
    if meta.get("rescued"):
        return False
    meta["rescued"] = True
    PeerSyncEvent.objects.create(
        binding_role=event.binding_role,
        binding_id=event.binding_id,
        direction="out",
        kind=event.kind,
        target_type=event.target_type,
        target_ref=event.target_ref,
        status=PeerSyncEvent.Status.PENDING,
        attempts=0,
        payload=event.payload,
        meta=meta,
        next_attempt_at=now + timedelta(seconds=HALL_RESCUE_DELAY_SECONDS),
        idempotency_key=uuid.uuid4().hex,
    )
    logger.warning(
        "hall.* 事件烧完退避已终态，排一条救援重投 kind=%s ref=%s 绑定=%s(%s) 下次=+%s 秒",
        event.kind, event.target_ref, event.binding_role, event.binding_id,
        HALL_RESCUE_DELAY_SECONDS,
    )
    return True


def _binding_for_event(event):
    """按 `event.binding_role` 取绑定行（P3：出站事件有两个方向）。 ⛔ 用**显式** `binding_role` 分支，⛔ 不写"先假定下游、找不到再当上游"的排他写法：
    将来加第三种绑定会被静默当上游（且两张表的 pk **各自从 1 开始**， `int(event.binding_id)` 在另一张表里会命中**无关的那一行**）。
    回 `(binding, role)`；认不出的 role ⇒ 抛 `ValueError`（未预期 = 响亮失败）。"""
    if event.binding_role == "downstream":
        return PeerDownstreamBinding.objects.filter(pk=int(event.binding_id)).first(), "downstream"
    if event.binding_role == "upstream":
        return PeerUpstreamBinding.objects.filter(pk=int(event.binding_id)).first(), "upstream"
    raise ValueError(f"未知的绑定角色：{event.binding_role!r}")


def _binding_is_invalid(binding, role: str) -> bool:
    """绑定缺失 / **按方向**失效（P3）：下行看 `state`，上行看 `deleted_at` + `enabled`。
    ⚠️ `PeerUpstreamBinding` **没有** `state` 列 ⇒ 判据必须按方向分叉，⛔ 不能共用一个表达式。"""
    if binding is None:
        return True
    if role == "downstream":
        return binding.state != PeerDownstreamBinding.State.ACTIVE
    return binding.deleted_at is not None or not binding.enabled


def _attempt(event_pk: int, *, now, lease=None, previous_deadline=None) -> tuple[str, str | None, tuple[str, str] | None]:
    """投递一行，回 `(结果, 失败类别, 轮键)`（PW-01：计数归 `_close_round`，本函数只分类）。 - **结果**：`"noop" | "done" | "retry" | "failed" | "dropped" | "stale-void"`（不变）；
    ⭐ Z6：`noop` 现在还多一档——取活时是 `pending`、投递期间被别处 **作废**（`done`）⇒ 失败记账整体放弃，见 `_record_failure`； - **失败类别**：只有 `retry` / `failed` 非 `None`——`FAILURE_CONNECTION`
    （计入按轮的停绑计数）或 `FAILURE_BUSINESS`（⛔ 不计入）； - **轮键**：`(binding_role, binding_id)`，给 `run_round` / `deliver_one` 按**绑定**收口计数用（两角色各查一张表，pk 空间不相干 ⇒ 必须连角色一起做键）；`noop` 时是 `None`。"""
    event = PeerSyncEvent.objects.filter(pk=event_pk).first()
    if (
        event is None or event.status != PeerSyncEvent.Status.PENDING
        or (lease is not None and event.next_attempt_at != lease)
    ):
        return "noop", None, None
    result = _send_pending_event(event, now=now)
    if (
        result[0] == "noop" and event.kind == HALL_TAKE_KIND
        and _release_refreshed_take(
            event, event.next_attempt_at,
            previous_deadline if lease is not None else event.next_attempt_at,
        )
    ):
        return ("rescan" if lease is not None else "noop"), None, None
    return result


@transaction.atomic
def _release_refreshed_take(event, lease, previous_deadline):
    refreshed = PeerSyncEvent.objects.filter(
        pk=event.pk, status=PeerSyncEvent.Status.PENDING, next_attempt_at=lease,
    ).exclude(idempotency_key=event.idempotency_key).update(next_attempt_at=previous_deadline)
    if refreshed:
        peer_wakeup.notify_hall_take()
    return bool(refreshed)


def _send_pending_event(event, *, now):
    event_pk = event.pk
    ordered_chat = event.target_type == CORE_CHAT_TARGET
    if ordered_chat and _earlier_chat_events(
        role=event.binding_role, binding_id=event.binding_id, target_ref=event.target_ref, event_id=event.pk,
    ).exists():
        return "noop", None, None
    binding, role = _binding_for_event(event)
    if _binding_is_invalid(binding, role):
        # 停绑 / 解绑 / 停用后不再投（spec §16.6 第 4 条末句）。绑定缺失 / 失效 ⇒ 事件**终态化**
        # `failed`（结果名 `dropped`）：⛔ 不留 `pending`（会被反复认领、并饿死其它绑定），
        # ⛔ 也不刷绑定健康度（这一投一条都没发出去）。见 `_mark_event_dropped`。
        if not _mark_event_dropped(event):
            return "noop", None, None
        # ⭐ M3：claim 被丢弃同样要在镜像行上留线索（绑定失效不会自愈，行上无声
        # 就没人知道该重新抢单）。
        _note_dead_claim(event, note=DROPPED_CLAIM_NOTE)
        return "dropped", None, None
    payload = event.payload
    if event.target_type in ("chat", CORE_CHAT_TARGET):
        from apps.internal_chat import peer_chat
        from apps.internal_chat.order_groups import can_deliver, materialize_outbound_payload

        if ordered_chat:
            payload = event.payload if peer_chat.can_deliver(event, binding) else None
        else:
            payload = materialize_outbound_payload(event, binding) if can_deliver(event, binding) else None
        if payload is None:
            if _mark_event_done(event, now=now, reason="该绑定已退出订单群"):
                return "stale-void", None, None
            return "noop", None, None
    # ⚠️ 复核**只对下行**：它要读 `binding.account_id`（上行绑定没有这一列），
    # 且上行本期没有撤单事件（spec §7.1）。
    if role == "downstream" and event.kind == "order.withdraw":
        send, stale_reason = _recheck_withdraw(
            event.target_ref, account_id=binding.account_id
        )
        if not send:
            # ⭐ （Minor 2）：作废**只**记事件，⛔ 不碰绑定的健康列——
            # 这次**一条都没发出去**，刷新 `last_ok_at` / 清零失败计数等于替一个不通的对端
            # 刷健康度、并推迟自动停绑（台账上也会记一次没发生过的成功）。
            if _mark_event_done(event, now=now, reason=stale_reason):
                return "stale-void", None, None
            return "noop", None, None
    event.attempts += 1
    if ordered_chat:
        event.attempts = min(event.attempts, len(RETRY_BACKOFF_SECONDS) + 1)
    try:
        data = push_events(binding.peer_base_url, binding.token_out, [{
            "idempotency_key": event.idempotency_key,
            "kind": event.kind,
            "target_ref": event.target_ref,
            # ⭐ 事件级元数据展开在**顶层、`payload` 之前**（见模块 docstring）。
            **(event.meta or {}),
            # ⚠️ 载荷已经是 JSON 安全的（裁决 R14）⇒ 原样发，⛔ 不再转一次。
            "payload": payload,
        }])
    except PeerBusy as exc:
        updated = PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(
            reason=LOCK_BUSY_REASON, next_attempt_at=now + timedelta(seconds=60),
        )
        return ('retry', FAILURE_BUSINESS, (role, event.binding_id)) if updated else ('noop', None, None)
    except PeerUnreachable as exc:
        # 连接类：对端**没回话**（超时 / 连不上 / 回包不是信封）⇒ 计入停绑计数
        # （按轮，收口在 `_close_round`；PW-01）。
        failure, failure_class = str(exc), FAILURE_CONNECTION
    except PeerRejected as exc:
        # 业务类：对端**收到了信封**、业务码非 0 ⇒ 明确表态 = 链路通，⛔ 不计入停绑计数。
        failure, failure_class = str(exc), FAILURE_BUSINESS
    else:
        # ⭐ 推送**没抛异常**不等于成功——对端可能收下了信封、却把这条事件硬跳过
        # （spec §16.5）⇒ 再按业务数据里的 `code` 分流；`None` = 真成功。
        failure = _hard_skip_reason(data, event.target_ref)
        # 对端按**本事件**给的硬跳（如 `not_your_order`）同样是业务表态 ⇒ 业务类（PW-01）。
        failure_class = None if failure is None else FAILURE_BUSINESS
    if failure is None:
        if not _mark_done(event, binding, now=now):
            return "noop", None, None
        # ⭐ 案 B2（D-0364）：真投递成功 ⇒ 「已同步给谁」机器行（仅 done 且 reason==""；作废 / 失败 / 重试都不写——判据与口径见本函数 docstring）。
        # ⛔ 与上面 `_mark_done` 的双表写**不同事务**（本函数无 atomic 包裹），
        # 日志行与 done 标记非同生共死，照旧可（审计线，如实声明）。
        _record_outbound_sync_log(event, binding, role)
        return "done", None, (role, event.binding_id)
    if not _record_failure(event, binding, failure, now=now):
        # ⭐ Z6：事件在投递期间被别处作废 ⇒ 失败记账整体放弃（⛔ 不复活成 pending、
        #    ⛔ 不计入按轮停绑计数、⛔ 不写绑定台账）。结果名取 `noop`：这次投递的结果
        #    已经被别处的判决覆盖，与「取活时事件已不是 pending」同一档。
        return "noop", None, None
    # ⚠️ 终态判据是 **`>`**：第 1–4 次失败各取一档退避（含 `3600`），**第 5 次**才终态
    # （改成 `>=` 会让 `1h` 那档永远不可达 —— spec §16.6 第 3 条的四档就少了一档）。
    if event.attempts > len(RETRY_BACKOFF_SECONDS) and not ordered_chat:
        # ⭐ Z6：**条件更新**（`pk` ＋ `status=pending`）。读到落写之间被别处置成 `done`
        #    的事件不许被这次终态化改回 `failed`（作废是终态，见 `_record_failure`）。
        updated = PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(
            status=PeerSyncEvent.Status.FAILED,
            attempts=event.attempts,
            reason=event.reason,
            processed_at=event.processed_at,
            next_attempt_at=None,
        )
        if not updated:
            logger.info(
                "事件已被作废，跳过失败记账 pk=%s kind=%s", event.pk, event.kind
            )
            return "noop", None, None
        # ⭐ M3：claim 烧完梯子 ⇒ 镜像行上留一条人话线索（⛔ 不改它的状态）。
        _note_dead_claim(event, note=DEAD_CLAIM_NOTE)
        # ⭐ B1 / M-4：`hall.*` 事件没有对账兜底 ⇒ 终态化那一刻克隆重投**一条**救援事件
        #    （`meta.rescued=true` ⇒ 它再败就是真终态；⛔ 不是无限重试）。
        _rescue_hall_event(event, now=now)
        return "failed", failure_class, (role, event.binding_id)
    # ⚠️ `attempts` 是**已投递次数** ⇒ 第 1 次失败的退避是 `RETRY_BACKOFF_SECONDS[0]`。
    retry_index = min(event.attempts, len(RETRY_BACKOFF_SECONDS)) - 1
    retry_at = now + timedelta(seconds=RETRY_BACKOFF_SECONDS[retry_index])
    # ⭐ Z6：退避重排也用**条件更新**——旧写法把内存里那份 `pending` 的 `status` 一起写回，
    # 同样会把一条已作废的事件复活（本支才是多数失败走的那条路）。`status` 刻意不进 `update(...)`：`WHERE status=pending` 已经保证它不变，多写一列只会让「状态写入点」
    # 多出一处假落点。
    updated = PeerSyncEvent.objects.filter(**_pending_event_match(event)).update(
        attempts=event.attempts,
        reason=event.reason,
        processed_at=event.processed_at,
        next_attempt_at=retry_at,
    )
    if not updated:
        logger.info("事件已被作废，跳过失败记账 pk=%s kind=%s", event.pk, event.kind)
        return "noop", None, None
    # An unavailable chat receiver must leave that binding available to order delivery.
    return "retry", failure_class, None if ordered_chat else (role, event.binding_id)


def deliver_one(event_pk: int, *, now) -> str:
    """投递一行事件，回 `"noop" | "done" | "retry" | "failed" | "dropped" | "stale-void"`。 ⚠️ **生产调用关系**：真正跑一轮的是 `run_round`，它逐条走**内部**的 `_attempt`
    （因为计数收口要**按绑定**在轮末统一做）。本函数是 `_attempt` 的**单条包装**——给测试 / 排障用，⛔ 不是 `run_round` 的调用目标。
    （修复波 C3：原话说"调用方逐条调它"，与实现不符，按实现改准。） ⭐ PW-01：一条事件也构成**一轮** ⇒ 收口在这里也走一次（既有用例的「单事件 +1」「到阈值停绑」因此口径不变）。"""
    outcome, failure_class, round_key = _attempt(event_pk, now=now)
    if round_key is not None and failure_class == FAILURE_CONNECTION:
        _close_round(round_key[0], round_key[1], now=now)
    return outcome


@dataclass
class DeliveryWindow:
    """Accumulate connection results for one tenant and one counting interval."""

    conn_failed: set[tuple[str, str]] = field(default_factory=set)
    succeeded: set[tuple[str, str]] = field(default_factory=set)

    def close(self, *, now, since=None, on_error=None) -> int:
        revoked = 0
        for role, binding_id in sorted(self.conn_failed - self.succeeded):
            try:
                if on_error is None:
                    revoked += _close_round(role, binding_id, now=now, since=since)
                else:
                    revoked += _close_round(role, binding_id, now=now, since=since, nowait=True)
            except Exception as exc:
                if on_error is None:
                    raise
                on_error(role, binding_id, exc)
            self.conn_failed.remove((role, binding_id))
        return revoked


def run_delivery_batch(*, now, window, should_stop=None, batch_size=BATCH_SIZE, orders_only=False) -> dict:
    """Claim and deliver one batch through the shared worker."""
    summary = {
        "claimed": 0, "delivered": 0, "retried": 0, "failed": 0,
        "voided": 0, "dropped": 0, "errors": [],
    }
    previous_deadlines = {}
    event_ids = claim_due_events(
        now=now, previous_deadlines=previous_deadlines, batch_size=batch_size, orders_only=orders_only,
    )
    summary["claimed"] = len(event_ids)
    lease = now + timedelta(seconds=LEASE_SECONDS)
    for index, event_pk in enumerate(event_ids):
        if should_stop is not None and should_stop():
            _release_unsent_events(event_ids[index:], previous_deadlines, now=now)
            break
        try:
            outcome, failure_class, round_key = _attempt(
                event_pk, now=now, lease=lease, previous_deadline=previous_deadlines[event_pk],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("上下游投递事件 %s 出现未预期失败", event_pk)
            summary["errors"].append(f"事件 {event_pk}: {type(exc).__name__}")
            continue
        if outcome == "rescan":
            _release_unsent_events(event_ids[index + 1:], previous_deadlines, now=now)
            break
        if outcome == "done":
            summary["delivered"] += 1
            if round_key is not None:
                window.succeeded.add(round_key)
        elif outcome == "retry":
            summary["retried"] += 1
        elif outcome == "failed":
            summary["failed"] += 1
        elif outcome == "stale-void":
            summary["voided"] += 1
        elif outcome == "dropped":
            summary["dropped"] += 1
        if failure_class == FAILURE_CONNECTION and round_key is not None:
            window.conn_failed.add(round_key)
    return summary


def _release_unsent_events(event_ids, previous_deadlines, *, now):
    previous = {}
    for pk in event_ids:
        previous.setdefault(previous_deadlines[pk], []).append(pk)
    for deadline, pks in previous.items():
        PeerSyncEvent.objects.filter(
            pk__in=pks, status=PeerSyncEvent.Status.PENDING,
            next_attempt_at=now + timedelta(seconds=LEASE_SECONDS),
        ).update(next_attempt_at=deadline)


def run_round(*, now=None) -> dict:
    """Run a single batch and close its connection-failure counters."""
    now = now if now is not None else timezone.now()
    window = DeliveryWindow()
    summary = run_delivery_batch(now=now, window=window)
    summary.pop("claimed")
    summary["revoked"] = window.close(now=now)
    return summary


@dataclass
class ReconcileCursor:
    last_binding_id: int | None = None


def run_reconcile(*, now=None, should_stop=None, cursor=None, orders_only=False) -> dict:
    """对账 + 自动补：问下家"我推给你的单你应用到哪了"，把落后的那些重投一遍。业务方原话：「你要是能查到丢单 那直接自动补不就行了」⇒ 本函数就是那句"直接补"。回 `{"requeued": int, "errors": int}`。判据顺序（每一条都有代价，⛔ 不要"顺手放宽"）： 1. **只看出站下行**（`direction="out"` + `binding_role="downstream"`），候选判据是 `status="failed"` **或**（`status="done"` **且** `reason=""`）： - `done` 也要查：**"收到 ack"与"下家真应用了"是两回事**——`done` 只说明**发送方收到了 ack** （对端真的应用了，或对端把这条**良性**判成 `stale` 跳过，见 `_hard_skip_reason`）； ⛔ **硬跳过与落库失败都不会是 `done`**（`_hard_skip_reason` 只在 `code == "stale"` 时回 `None`，其余硬跳过回原因 ⇒ 落 `retry` / `failed`；接收侧落库失败以 HTTP / 业务码失败面回来 ⇒ 同样落 `failed`）。查 `done` 是为了抓**分歧**：ack 收到了、对端回报的水位却落后于这条事件的 `occurred_at`。 - ⛔ **`done` + 非空 `reason` 一律不是候选**：那是**有意作废**（撤单投递前复核判定
    "仍是当前接手人"，`_mark_event_done(reason=…)`）⇒ 对端永远不会应用它、水位也就永远追不上 `occurred_at`，不排除就会每轮重排一次、持续 24 小时。 - `failed` + 非空 `reason` **仍是**候选（那是对端给的失败原因，不是"有意作废"）。 2. **窗口**：只查 `created_at >= now - RECONCILE_WINDOW_HOURS`。更老的台账是历史积压，重投它等于把陈年事件灌给下家。 3. ⛔ **不碰 `pending`**：那些归正常的退避机制管（`_attempt`），对账抢过来只会打乱退避。 4. **没有候选就不发 HTTP**（`continue` 在 `fetch` 之前）：空的绑定不该产生出站流量。 5. 每个**生效**（`ACTIVE`）绑定一次调用，单号去重后按 `RECONCILE_MAX_ORDERS` 切片。 ⭐ 起请求里还带**第二个键** `order_nos = {来源单号: 本单内置号}`：老链路那一列记的是当年那个内置号，收方任一对上即命中（计划 §4 的过渡期兼容）。 6. 对端不可达 / 拒收（`PeerUnreachable` / `PeerRejected`）⇒ `errors += 1`、跳过这个绑定， ⛔ **不让整轮炸掉**（对账是尽力而为；命令把它当**预期内**噪音，不设退出码）。其余异常照旧冒泡（未预期 = 响亮失败，E-0010）。
    7. 水位判据 **`>=`**：对端回报的已应用时刻**不早于**这条事件的 `occurred_at` ⇒ 它已经有了，留给它；否则（含对端回 `null` / 集合里没有这个单号 / 水位解析不出）⇒ **重排**。 ⚠️ `occurred_at` 解析不出的**跳过**：没有时刻就没有比较基准，⛔ 不许当成"很旧"静默重投。 8. 重排写四个字段：`status=pending` / **`attempts=0`** / `next_attempt_at=None` / `reason=RECONCILE_REQUEUE_REASON`，外加把终态时刻 `processed_at` 摘掉（`pending` 行带上一个"处理时刻"是自相矛盾的台账）。⭐ `attempts` 清零**承重**：`_attempt` 的终态判据是 `attempts > len(RETRY_BACKOFF_SECONDS)`，一条 `attempts=5` 的 `failed` 若不清零，下一投**立刻又终态**——补了等于没补。 ⚠️ **被上限截掉的单号这一轮不对账**（它们的事件既不在 `queried` 里、也就不参与重排）：拿"我没问过"当成"对端没有"会对着一个**健康**的对端每轮重复投递（reconcile 是每轮都跑的）。代价如实登记：一个绑定 24 小时内超过 `RECONCILE_MAX_ORDERS` 个不同单号时，排在后面的那些这一轮查不到（Task 9 残差；真要覆盖得做轮转或分页）。 ⚠️ **接收侧幂等**（`peer_inbound` 按 `occurred_at` 与 `last_applied_at` 裁决新旧）⇒ 重投是 at-least-once 的良性代价：宁可多投一次，也不漏一单。 ⭐ **上行回传的超龄兜底**：本函数开头先跑一次 `_rescue_stale_upstream_events`——纯本地重排（无 HTTP、无对端依赖），把「走完 81 分钟梯子后终态 `failed`」的上行回传行捞回来（那一方向原本**没有任何兜底**，终态即永久丢失）；计数并进同一个 `requeued`（命令行那句「对账补 N」两种来源都算）。两条判据**刻意不同**：下行按水位比对、重排清零 `attempts`（有对账把关，给一整副新梯子）；上行按超龄 + `attempts` 封顶（没有对账，只再试**一次**，⛔ 不能变成无限重试）。 ⚠️ 本模块是**租户 schema** 内的写路径 ⇒ 租户上下文来自调用方，本函数不接受租户参数。"""
    # ⚠️ 判据用 `is not None`（E-0021 同口径）：不拿真值判这个形参。
    now = now if now is not None else timezone.now()
    summary = {"requeued": 0, "errors": 0}
    # ⭐ PW-02：上行兜底先跑（纯本地重排；放最前是因为它与下面的逐绑定 HTTP 无依赖，
    # 也让「对账补」的计数把两种来源都算进去）。
    summary["requeued"] += _rescue_stale_upstream_events(
        now=now, batch_size=50 if cursor is not None else None, orders_only=orders_only,
    )
    window_start = now - timedelta(hours=RECONCILE_WINDOW_HOURS)
    bindings = PeerDownstreamBinding.objects.filter(
        state=PeerDownstreamBinding.State.ACTIVE
    ).order_by("pk")
    if cursor is not None and cursor.last_binding_id is not None:
        bindings = bindings.filter(pk__gt=cursor.last_binding_id)
    for binding in bindings.iterator(chunk_size=64) if cursor is not None else bindings:
        if should_stop is not None and should_stop():
            break
        if cursor is not None:
            cursor.last_binding_id = binding.pk
        candidates = (
            PeerSyncEvent.objects.filter(
                # ⚠️ 候选 = `failed`（真失败）或 `done` **且 `reason` 为空**（真成功）；
                # `done` + 非空 `reason` 是有意作废（撤单复核），对端永不应用 ⇒ ⛔ 不重投。
                # 详见 docstring 第 1 条。
                models.Q(status=PeerSyncEvent.Status.FAILED)
                | models.Q(status=PeerSyncEvent.Status.DONE, reason=""),
                direction="out",
                binding_role="downstream",
                binding_id=str(binding.pk),
                created_at__gte=window_start,
            )
            # ⭐ 大厅镜像事件（hall.throw/take/reprice，D-0303）**不参与对账重排**：它们的接收侧只落镜像行、**不建订单与水位** ⇒ 水位查询对它们**必回 null**
            # ⇒ 按下面第 7 条判据会「永久重排」——生产实锤：铺底 655 条投完后每轮「投递 50 · 对账补 50」无限空转。错过一拍没有危险：镜像
            # 行是幂等 upsert，漏的 throw 由铺底命令/后续 take 收口，接收侧天然 at-least-once 良性。
            .exclude(kind__startswith="hall.")
            .exclude(kind=DIRECT_TAKER_ASSIGN_KIND)
            .exclude(target_type=CORE_CHAT_TARGET)
            .order_by("created_at", "id")
        )
        if orders_only:
            candidates = candidates.filter(target_type__in=ORDER_SYNC_TARGETS)
        candidates = list(candidates)
        if not candidates:
            # ⛔ 没有候选 ⇒ 一个 HTTP 都不发（空的绑定不该产生出站流量）。
            continue
        # 去重但**保持顺序**（`dict.fromkeys`）：同一单的多条事件只问一次单号。
        refs = list(dict.fromkeys(event.target_ref for event in candidates))
        refs = refs[:RECONCILE_MAX_ORDERS]
        queried = set(refs)
        # ⭐（计划 `2026-10-25-peer-source-identity` §4）：对账的**第二个键**。新口径下 `target_ref` 是**来源单号**，而老链路的 `peer_order_no` 记的是当年那个
        # **内置号** ⇒ 把每条候选载荷里的内置号一起带上（`{来源号: 内置号}`），收方**任一对上即命中**。⛔ 不做存量反推（老行不改写，见 RUNBOOK §3.2f）。
        # ⚠️ 只带**这一轮真问到的** refs（被上限截掉的不带，免得白扩请求体）；同一 ref 取**第一条**候选的内置号（同一张单的事件载荷里它是同一个值）。
        order_nos: dict[str, str] = {}
        for event in candidates:
            payload = event.payload if isinstance(event.payload, dict) else {}
            builtin = payload.get("order_no")
            if (
                isinstance(builtin, str)
                and builtin
                and event.target_ref in queried
                and event.target_ref not in order_nos
            ):
                order_nos[event.target_ref] = builtin
        try:
            applied = fetch_applied_watermarks(
                binding.peer_base_url, binding.token_out, refs, order_nos=order_nos
            )
        except (PeerUnreachable, PeerRejected):
            # ⚠️ 对端不给水位 / 明确拒收都是**预期**失败：记一笔、跳过这个绑定。
            # 这里有意不 logger.exception（那不是"我们坏了"），命令侧把它当预期内噪音。
            summary["errors"] += 1
            continue
        for event in candidates:
            if event.target_ref not in queried:
                # 被上限截掉的单号这一轮不问也不判（见 docstring 的代价说明）。
                continue
            occurred_at = _parse_event_time((event.meta or {}).get("occurred_at"))
            if occurred_at is None:
                # 没有可比的时刻 ⇒ 跳过（⛔ 不许当成"很旧"静默重投）。
                continue
            watermark = _parse_event_time(applied.get(event.target_ref))
            if watermark is not None and watermark >= occurred_at:
                # 对端已有这条（相等 = 同一时刻，也是"已有"）⇒ 留给它。
                continue
            summary["requeued"] += PeerSyncEvent.objects.filter(
                pk=event.pk, status=event.status, attempts=event.attempts,
                reason=event.reason, processed_at=event.processed_at,
                next_attempt_at=event.next_attempt_at,
            ).update(
                status=PeerSyncEvent.Status.PENDING, attempts=0,
                next_attempt_at=None, reason=RECONCILE_REQUEUE_REASON, processed_at=None,
            )
    else:
        if cursor is not None:
            cursor.last_binding_id = None
    return summary


def _rescue_stale_upstream_events(*, now, batch_size=None, orders_only=False) -> int:
    """PW-02（修复线）：把**超龄终态失败**的上行回传事件重排**一次**。回重排条数。背景：下家→上家的状态回传走 81 分钟退避梯子（1m/5m/15m/1h，第 5 次终态 `failed`），终态后**没人再碰它**——全仓唯一的 PENDING 重排写点原本在下行对账（水位比对），上行方向没有兜底 ⇒ 一场上家侧的抖动就能让状态回传**永久丢失**、无告警。判据（每条都有代价，⛔ 不要"顺手放宽"）： 1. 只认 `binding_role="upstream"` + `direction="out"` + `status="failed"`： `pending` 归正常退避管（租约 300 秒，到期下一轮再认领，**不存在**卡死的 pending），
    ⛔ 不抢；`done` 是已送达，没有"丢"可言；下行的 `failed` 归水位对账管（判据完全不同）。 2. `attempts == len(RETRY_BACKOFF_SECONDS) + 1`（= 5，梯子终态的形状）： ⭐ 这一条**就是**「最多重排 1 次」的封顶——重排**不清零** `attempts`（⛔ 与下行对账的清零**刻意相反**），重排后那一投再失败会落 `attempts=6` 再次终态，而 6 不在本判据内 ⇒ 结构上不可能二次重排，`attempts` 本身就是重排计数（⛔ 不建新列 / 新表）；重排也**只多一次**投递机会（不是新梯子）——这是兜底，⛔ 不是无限重试。 `dropped`（绑定失效终态化，`attempts` 不动）不在内：那类行重排了也会再被 drop
    ⇒ ⛔ 不给它开「重排 → drop → 再重排」的死循环。 3. `created_at < now - UPSTREAM_RESCUE_MIN_AGE_HOURS`：正常梯子全程约 81 分钟； 24 小时前就终态的行，对端侧大概率已恢复 ⇒ 兜底才有意义。 4. 只动**启用且未解绑**的上游绑定的行（判据与 `_binding_is_invalid` 的上行支同形）：绑定失效的行见第 2 条的 `dropped` 说明。 5. 台账留痕：写绑定 `last_error` / `last_error_at`（人话，先例 = `_auto_revoke_reason` 的「把对端原文换掉」）；⛔ 不碰 `consecutive_failures` （重排不是失败也不是成功，健康计数另有主人）。 ⚠️ 接收侧幂等（`peer_inbound` 按 `occurred_at` 与 `last_applied_at` 裁决新旧）⇒ 重投是 at-least-once 的良性代价；若这张单后来又有新事件，旧状态回传会被对端判 `stale`。 ⚠️ 不设每轮条数上限：封顶在第 2 条（每行一生一次），积压由 `claim_due_events` 的每轮 50 条、最老优先自然消化。"""
    threshold = now - timedelta(hours=UPSTREAM_RESCUE_MIN_AGE_HOURS)
    eligible = PeerSyncEvent.objects.filter(
        binding_role="upstream",
        direction="out",
        status=PeerSyncEvent.Status.FAILED,
        attempts=len(RETRY_BACKOFF_SECONDS) + 1,
        created_at__lt=threshold,
    ).exclude(target_type=CORE_CHAT_TARGET).filter(
        # ⭐ L-1（审计批，并发实测）：**救援事件自己不再被救援**。 B1 的 hall 救援克隆行带着 `meta.rescued=true`、`attempts` 从 0 重走梯子；它再次烧到终态（`attempts=5`）时形状与原事件**逐字相同** ⇒ 不加这条
        # 「PW-02 重排 × B1 救援」会把同一条事实重投第二次（第二轮探针已证）。 ⚠️ **不能写 `.exclude(meta__rescued=True)`**：JSONB 缺键时 `meta -> 'rescued'`
        # 是 SQL NULL，`NOT (NULL = true)` 仍是 NULL ⇒ 那种写法会把**没有这个键** 的普通行一起排除（实测：普通行一条都不再被救 = 这条兜底整个失效）。判据必须把「缺键 / `null`」显式算进**可救**那一侧。代价：救援行一生只救一次（这本来就是 B1 的口径：救援是兜底、⛔ 不是无限重试）。
        models.Q(meta__rescued__isnull=True) | models.Q(meta__rescued=False)
    ).order_by("created_at", "id")
    if orders_only:
        eligible = eligible.filter(target_type__in=ORDER_SYNC_TARGETS)
    if batch_size is not None:
        enabled = PeerUpstreamBinding.objects.filter(deleted_at__isnull=True, enabled=True)
        binding_ids = enabled.annotate(
            _event_binding_id=Cast("pk", output_field=models.CharField()),
        ).values("_event_binding_id")
        eligible = eligible.filter(binding_id__in=binding_ids)
    stale = list(eligible[:batch_size])
    if not stale:
        return 0
    # 第 4 条：只救「启用且未解绑」的绑定（`binding_id` 存的是字符串、⛔ 不建 FK
    # ⇒ 只能在 Python 侧按存活绑定集合过滤）。
    alive_ids = {
        str(pk)
        for pk in PeerUpstreamBinding.objects.filter(
            deleted_at__isnull=True, enabled=True,
            pk__in=[int(event.binding_id) for event in stale],
        ).values_list("pk", flat=True)
    }
    requeued = 0
    touched: list[str] = []
    for event in stale:
        if event.binding_id not in alive_ids:
            continue
        updated = PeerSyncEvent.objects.filter(
            pk=event.pk, status=event.status, attempts=event.attempts,
            reason=event.reason, processed_at=event.processed_at,
            next_attempt_at=event.next_attempt_at,
        ).update(
            status=PeerSyncEvent.Status.PENDING, next_attempt_at=None,
            reason=UPSTREAM_RESCUE_REASON, processed_at=None,
        )
        if not updated:
            continue
        requeued += updated
        if event.binding_id not in touched:
            touched.append(event.binding_id)
    for binding_id in touched:
        # 第 5 条：台账留**人话**；⛔ 不碰 `consecutive_failures`（见 docstring）。
        PeerUpstreamBinding.objects.filter(pk=int(binding_id)).update(
            last_error=UPSTREAM_RESCUE_LEDGER, last_error_at=now,
        )
    return requeued
