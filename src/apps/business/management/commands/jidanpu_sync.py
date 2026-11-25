"""`jidanpu_sync`：鸡蛋铺同步的**唯一调度入口**（spec §9/旧 spec §5.5；下游线 spec §10）。用法：`manage.py jidanpu_sync --once`（跑一轮，手工/排障）/ `--tenant <schema>`（只跑一个租户）/ `--loop`（常驻，生产形态：systemd 单元拉起）。 **同步解耦成六台常驻进程**（计划 `docs/plans/2026-10-25-jidanpu-sync-decoupling.md`；⛔ 别只启其中一台，合起来才是一份完整同步）：`--outbox-only`=只跑下家①式绑定的 `run_outbox`（推单/指派/抛单/退单/改价/项目/状态），睡 `OUTBOX_LOOP_SLEEP_SECONDS`，单元 `tuantuantong-jidanpu-outbox`；`--callback-only`=只跑下家②式一轮的三段 ⓠ回调消费 → ⓠ'兜底对账 → ⓠ''出向发单，睡 `LOOP_SLEEP_SECONDS`，单元 `tuantuantong-jidanpu-callback`（⚠️⛔ ②式出向发单**不拆到 outbox 单元**：依赖「同轮先消费回调补上缺的远端系统单号」（`_run_member_openapi_binding` 表第 1 行）⇒ 拆开=拿空号去办）；`--fastpath-only`=只跑下家①式的 ⓪身份自愈+②日志快路（`poll_log`），睡 `LOOP_SLEEP_SECONDS`，单元 `tuantuantong-jidanpu-fastpath`（⚠️⛔ 身份自愈必须与快路**同进同出**（`--skip-fastpath` 一起跳过）：自愈在快路前把 `account_name` 对齐到登录账号 ⇒ 留主进程=两进程重复自愈）；`--reconcile-only`=只跑下家①式的 ③状态回拉（`pull_statuses`）+④对账排事件（`reconcile_and_queue`）+⑤接单兜底网（`claim_net`），单元 `tuantuantong-jidanpu-reconcile`（⚠️⛔ 三段必须**同单元**——闸门与锚都是进程内的，拆开=同一段慢路两台各跑一遍；但**各有各的闸门**：回拉 `status_pull_seconds` 默认 600 / 对账 `reconcile_seconds` 默认 180 / 兜底网 `CLAIM_NET_SECONDS`=180（常量），互不阻塞 ⛔ 不共用一个闸门——真机实测共用只能二选一：按回拉节奏 ⇒ 对账每轮排 450+ 条候选白打上游，按对账节奏 ⇒ 回拉变慢）；`--upstream-only`=只跑上家线**整行** `_run_upstream_binding`（①式=outbox+大厅逐游戏全量+我的订单/App 按需拉图；②式=回调消费+兜底对账），⛔ 不跑下家任何一段与模拟单，单元 `tuantuantong-jidanpu-upstream`（⚠️⛔ 上家线**只能整行拆、⛔ 不许按子段再拆**：健康合成判据（`last_ok_at` 只在「至少一条读路到点+各段都没未预期异常+大厅拉全+outbox 没记失败」时才盖）与整行连续失败退避（`_UPSTREAM_PENALTY_UNTIL` 连 outbox 一起跳）都是**行级**的 ⇒ 子段拆开=每台各算一次、判据互相打架；边界=`_run_upstream_binding` 这一个方法）；`--skip-outbox/--skip-callback/--skip-fastpath/--skip-reconcile/--skip-upstream`=原有一轮里分别跳过 ①outbox/②式那段/⓪自愈+②快路/③回拉+④对账+⑤兜底网/整行上家（生产形态=主单元 `ExecStart` 同时给这五个）；每对开关互斥（同给 ⇒ `CommandError`）、五个 `--*-only` 两两互斥（同给只能静默忽略一个）、都不给 ⇒ 行为**逐字不变**（回归判据）；⭐ 主单元只剩「租户级模拟单轮」（`hall_simulated.run_tenant_round`，D-0296）——五个 skip 全给的一轮只跑模拟单（模拟单是租户级不挂绑定 ⇒ 没有 `--*-only` 形态）。
⚠️ **cron 是「共用的闹钟」，数据是各自的**（spec §9）：遍历租户 → 各自在 `tenant_context` 里跑 → **逐租户独立异常边界**；**没有游标/断点**：方向一每轮全量重拉（D7）、方向二靠 outbox 行自己的状态。⚠️ **生产调度=`--loop`+systemd 单元**（业务方要 10 秒级，cron 最细 1 分钟；模板在 `deploy/systemd/`，发版/回滚脚本装它/重启它）：⛔ **只能在一台应用机 `systemctl enable`**（两台=并发同步同一租户：同一张单被录两次、抢单与撤单互踩）、⛔ **不要再叠 `--once` 的 cron**（与常驻真并发）；`--loop` 进程活着才轮询（靠 systemd `Restart=always` 拉起+绑定 `last_ok_at` 告警，运维口径 `docs/RUNBOOK.md`）；命令**不自行判上游成败**：每段有自己的异常边界并记进各绑定 `last_error`，本层只「换个租户继续」并把不完整的那轮写到 stderr（⛔ 不静默）；**退出码契约**（照 `fleet_status` 先例「findings 是数据不是崩溃」）：`--once`（含各 `--*-only` 单趟）在**有租户未预期失败**时抛 `CommandError`（退出码 1）否则 0 ⇒ cron 只在「我们这边坏了」时发信，预期内可重试失败（上游 `ApiError`、拉取不完整、退避等待）是正常噪音；本命令是**管理命令**而非 HTTP 端点：`--tenant` 是运维排障入口，⛔ 不接受任何请求来源的租户标识（`AGENTS.md` §4）。 **一个租户里跑两段**（下游线+上家线，spec §10；租户级 `JidanpuBinding` 线已拆）：1. **代练员个人绑定**（`JidanpuMemberBinding`，逐个）：①式一轮顺序固定 `run_outbox` → `poll_log` → `pull_statuses` → `reconcile_and_queue` → `claim_net`（⑤兜底网排最后：先收敛对方事实再兜底），**逐绑定且逐条路**各有异常边界（一个代练员/一条路坏了其余照跑）；各条路**各自**按该绑定自己的间隔判到点：快路 `fast_poll_seconds`、回拉 `status_pull_seconds`（默认 600）、对账 `reconcile_seconds`（默认 180）、兜底网 `CLAIM_NET_SECONDS`=180（常量），慢路各有锚、互不阻塞（锚是**进程内**的，库里唯一 `last_run_at` 分不开这几条路）；⭐ ②式走另一条支（`_run_member_openapi_binding`）：三段顺序固定=ⓠ回调消费（无间隔闸）→ ⓠ'兜底对账（默认 180）→ ⓠ''出向发单（无闸），承重口径「先把对方推来的事实收敛掉，再决定要不要往外推」（spec §5.5），且回调消费能把链路上缺的远端系统单号补上（`_heal_missing_remote_id`），发单六档每一档都要那一格 ⇒ 反过来=拿空号去办（逐段理由见其 docstring）；⚠️ **出向发单开关默认关着**（`JIDANPU_MEMBER_OPENAPI_OUTBOX`）：接线 ⛔ 不等于开闸——关着时一次上游都不打、一个字节都不写；⚠️ ①式那四段在②式行**一段都不跑**（都要①式登录凭据，②式账号密码为空 ⇒ 只有建连就抛 `ConfigError`；⛔ 不留「跑了但必然失败」的段）；**入口闸也按 `mode` 分**（`_member_credentials_missing`；沿用①式判据会让②式整行静默跳过，上家线踩过 E-0202）。2. **上家本线绑定**（`JidanpuUpstreamBinding`，逐个）：**一轮跑哪几段由行上的 `mode` 决定**（spec §2.1）——①式：`run_outbox` → `sync_hall` → `sync_my_orders`；②式：回调消费 → 兜底对账（⛔ 不跑 outbox/大厅/我的订单；按方式分是实测缺陷：②式账号密码为空 ⇒ ①式凭据判据=整行静默跳过、让那三段照跑=建连处每轮抛 `ConfigError` 把「这条路没有这一段」报成「同步坏了」）；**逐绑定且逐段**各有异常边界；⚠️ **ⓠ回调消费排在 outbox 之前**（spec §5.5 明写，顺序承重：outbox 会把我方状态推给上家 ⇒ 先收敛对方推来的事实再外推），**ⓠ'兜底对账紧随其后**（spec §8.3 ②：先回调后对账，两条路算同一个目标态 ⇒ 同轮先后写同单落同一个值）；回调消费**不设间隔闸**（平时零网络请求），留痕出口=台账+整轮末尾盖 `last_error`；⚠️ 对账有闸（`reconcile_seconds` 默认 600，锚 `_UPSTREAM_OPENAPI_RECONCILE_AT`——没有它一张在途单每拍都被查）；大厅 `hall_poll_seconds` 默认 **30**、我的订单 `my_orders_poll_seconds` 默认 **60**（锚都在进程内）；**outbox 不设闸**（排出去的台账要尽快变成动作）；⛔ ②式**不跑 outbox**（写接口第一期不做，spec §15-N3）：跳过**不静默**——`jidanpu_openapi.note_openapi_outbox_not_supported` 留幂等台账+把那句话盖进 `last_error`；⚠️ 停用/凭据不全 ⇒ 整行跳过（spec §8.4；⛔ 一次上游都不打、连建连都不建），凭据判据按方式分（`_credentials_missing`）。⚠️ **健康列**：①式=至少一条读路到点+三段无未预期异常+到点的大厅拉全+outbox 没记失败 ⇒ **整轮末尾**盖 `last_ok_at` 并清旧错（`_stamp_binding_ok`；⛔ 部分成功不许盖；逐档依据见 `_run_upstream_binding` 末尾注释，用例 `test_jidanpu_upstream_schedule.py::UpstreamHealthStampTests`）；②式那四档有三档不适用 ⇒ 判据换成「兜底对账真查过单或回调真处理过事件+无未预期异常」（不补这支，②式的 `last_ok_at` 永远为空、恢复正常后永远挂旧错=说假话）；⚠️ **间隔列存 `0` 按模型列自己的默认值兜底**（`_poll_seconds`，spec §10/R22）。"""
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context
from jidanpu.errors import AuthError

from apps.business import (
    hall_simulated,
    jidanpu,
    jidanpu_direct_queue,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_member_openapi_outbox,
    jidanpu_openapi,
    jidanpu_openapi_consume,
    jidanpu_openapi_reconcile,
    jidanpu_proxy,
    jidanpu_upstream,
)
from apps.business.models import (
    MEMBER_FAST_POLL_SECONDS,
    MEMBER_RECONCILE_SECONDS,
    MEMBER_STATUS_PULL_SECONDS,
    JidanpuUpstreamBinding,
)
from apps.platform.models import Tenant

logger = logging.getLogger(__name__)

#: `--loop` 的睡眠间隔（秒）＝本线最快闸门（日志快路 `MEMBER_FAST_POLL_SECONDS`）＝3 秒：
#: 常驻一拍最多跑一次，本值必须 ≤ 快路闸门（⛔ 值一个字不改；人话口径见 `docs/RUNBOOK.md`）；
#: 闸门配得比它更小也不会更快（真上限是这个常量）。实际周期 ≈ 本值 + 一轮自身耗时（先跑完再睡）。
LOOP_SLEEP_SECONDS = 3

# ── ⭐ 优雅停机（照 `peer_sync` 的现成结构）──────────────────────────────
#: `SIGTERM` / `SIGINT` 只置位、⛔ 不直接退出：跑完当前租户当前轮再收口，睡眠走 `Event.wait` （信号一到立刻醒；systemd `TimeoutStopSec=30` 只覆盖一轮，不再 SIGKILL 打断在途上游写）。
#: ⚠️ 六条循环共用这一个停止位（停的是整个进程，退避等待也走它）；测试可对它 `set()` / `clear()`。
_STOP = threading.Event()


def _request_stop(signum, _frame) -> None:
    """信号处理器：只置位。⛔ 在 handler 里做任何重活（信号上下文不可重入）。"""
    _STOP.set()

#: `--outbox-only`（独立出向进程）每轮之间的睡眠间隔（秒）。出向**没有间隔闸**（排出去的台账要
#: 尽快变成真实动作）⇒ 节奏就是这个值；取 2 秒（比轮询快一拍：这一台只查台账 + 打写接口，不建连读数）。
#: 实际周期 ≈ 本值 + 一趟自身耗时；⛔ 不要再叠 `--once` 的 cron（与常驻真并发）。
OUTBOX_LOOP_SLEEP_SECONDS = 2
DIRECT_ASSIGNMENT_SLEEP_SECONDS = 1

# ── 个人绑定的**逐条路**间隔锚（进程内）────────────────────────────────────────────
#: 「这条路上次跑到点是什么时候」，键 = `(租户 schema, 绑定主键)`。不落库：库里唯一 `last_run_at` 被 outbox / 快路 / 回拉三处每轮写 ⇒ 分不开各自节奏；代价 = 多进程退化成「每进程一窗」
#: （本仓一租户一套部署、常驻只在一台 ⇒ 够用）；重启 / 刚启用 ⇒ 锚空 ⇒ 到点（⛔ 不是永不到点）。
_MEMBER_FAST_AT: dict[tuple[str, int], datetime] = {}
#: ⭐ **③ 状态回拉**的间隔锚：只闸 `pull_statuses`。⚠️ 与库列 `status_pull_at` 不是一回事：
#: 那是「上次成功拉完」的水位（增量取数窗），这是「上次跑到点」的调度锚（判间隔）——名字像，⛔ 别混。
#: 失败也打锚（照快路口径），否则持续失败的回拉会被每一拍（3 秒）重打上游。
_MEMBER_STATUS_PULL_AT: dict[tuple[str, int], datetime] = {}
#: **④ 补漏对账**的间隔锚：只闸 `reconcile_and_queue`（与上面的回拉锚各管各的）。
_MEMBER_RECONCILE_AT: dict[tuple[str, int], datetime] = {}
#: **⑤ 接单兜底网**的间隔锚：只闸 `claim_net`（照上面两把锚的形状，键同形 ⛔ 不共用：
#: 三条慢路各判各的到点，共用一根锚会让先跑的那条把别的节奏吃掉）。节奏是**常量**（零迁移）：
#: 兜底网收口的是「快路护栏窗外的漏行」，比快路慢两个量级即可。
CLAIM_NET_SECONDS = 180
_MEMBER_CLAIM_NET_AT: dict[tuple[str, int], datetime] = {}


def _member_interval(value, *, default: int) -> int:
    """间隔列存了 `0`（配置写坏）⇒ 按**模型列自己的默认值**算（照 `interval_elapsed` 的 `or 60`）。
    ⚠️ 0 是有意义的边界（E-0021 同族），这里的意义是「配置写坏了、按默认走」， ⛔ 不是「每轮都跑」——那会把一条常驻循环变成对上游的紧循环。
    ⚠️ 默认值只从 `apps/business/models.py` 的两个常量取（那里是**唯一出处**），⛔ 不写 3 / 180。"""
    return value or default


def _member_due(anchor, seconds, *, now) -> bool:
    """这一条路到点了吗（判据形状照 `jidanpu_hall.interval_elapsed`；⚠️ 那个模块已随
    租户级那条线删除，本行只指**历史的**参照物，形状一字未改）。"""
    if anchor is None:
        return True
    return anchor + timedelta(seconds=seconds) <= now


# ── 上家鸡蛋铺本线的两条读路各自的间隔锚（进程内）────────────────────────
#: 「这条读路上次跑到点是什么时候」，键 = `(租户 schema, 绑定主键)`；与个人绑定那层同一理由与代价（库里 `last_run_at` 被 outbox 每轮写 ⇒ 分不开大厅 / 我的订单各自节奏；多进程 = 「每进程一窗」，
#: 本仓常驻只在一台 ⇒ 够用）；重启 / 刚启用 ⇒ 锚空 ⇒ 到点。
_UPSTREAM_HALL_AT: dict[tuple[str, int], datetime] = {}
_UPSTREAM_MY_ORDERS_AT: dict[tuple[str, int], datetime] = {}
#: ⭐ **官方 API 兜底对账**（spec §8.3）的锚：间隔取该绑定自己的 `reconcile_seconds`
#: （默认 `UPSTREAM_OPENAPI_RECONCILE_SECONDS` = **600**，由 `_poll_seconds` 兜底）。
#: 进程内（同上代价；重启 ⇒ 到点）；失败也打锚（照两条全量读路口径，保护上游容忍度）。
_UPSTREAM_OPENAPI_RECONCILE_AT: dict[tuple[str, int], datetime] = {}

# ── ⭐ 大厅模拟单的轮次锚（D-0296；进程内）──────────────────────────────
#: 「这个租户的模拟单轮次上次跑到点是什么时候」，键 = **租户 schema**（模拟单是租户级数据、不挂任何绑定 ⇒ 键少一元）；间隔取 `hall_simulated.ROUND_PERIOD_SECONDS`（**7200** 秒）；
#: 进程内（同上代价）；重启 ⇒ 锚空 ⇒ **到点**。
_HALL_SIM_AT: dict[str, datetime] = {}


def _sim_due(anchor, seconds, *, now) -> bool:
    """模拟单轮次到点了吗（判据形状照 `_upstream_due`，一个字不改地共用语义）。"""
    if anchor is None:
        return True
    return anchor + timedelta(seconds=seconds) <= now

# ── ⭐ 读路连续失败的**指数退避**（上家线与下家①式读路）──────────────────
#: 基数 30 / 翻倍 / 封顶 300。连续失败必须**整轮**退避（连 outbox 一起）：对端封的是 IP，同 IP 任何一发都是同一来源，没有「读停写照打」的余地（实测现场见外移登记）。 ⚠️ 判据只认「读路段抛了」（outbox / 大厅 / 我的订单 / 读路建连；⭐ 大厅尾部快路已随 D-0366 退役）；
#: 进程内 ⇒ 重启清零是**期望**行为（⛔ 不是漏洞）；⭐ 下家快路 `_poll_member_log` 失败也吃同一张表、同一套阶梯（⛔ 不抄第二套数字）；上家 / 下家主键独立计数 ⇒ 同键两行一起退避 / 清零（方向安全，⛔ 不另起键）。
UPSTREAM_FAILURE_BACKOFF_BASE_SECONDS = 30
UPSTREAM_FAILURE_BACKOFF_MAX_SECONDS = 300
#: 「这一行的连续失败次数」，键 = `(租户 schema, 绑定主键)`（与上面那几个锚同一把键）。
_UPSTREAM_FAILURE_COUNT: dict[tuple[str, int], int] = {}
#: 「这一行退避到什么时候」，键同上；到点之后才允许再打上游。
_UPSTREAM_PENALTY_UNTIL: dict[tuple[str, int], datetime] = {}


def _upstream_backoff_seconds(count: int) -> int:
    """连续第 `count` 次失败要停多少秒：`min(基数 × 2^(count-1), 上限)`；`count` 从 1 起。
    ⚠️ 纯函数（只读常量、不碰字典）⇒ 用例直接打它就能验 30 / 60 / 120 / 300（spec §3 第 3 条），
    ⛔ 不必驱动整个命令循环；传 0 / 负数不在契约内（只会得到更短的窗口，不抛也不长睡）。"""
    return min(
        UPSTREAM_FAILURE_BACKOFF_BASE_SECONDS * 2 ** (count - 1),
        UPSTREAM_FAILURE_BACKOFF_MAX_SECONDS,
    )


def _note_upstream_failure(key, *, now) -> datetime:
    """记一次「这一轮有读路失败」：计数 +1 ⇒ 算出并写下退避窗口，回窗口终点。 ⚠️ 窗口从**本轮起点**算（`now` 是整轮共用的注入时钟，⛔ 不另读墙钟）⇒ 实际剩余 =
    窗口 − 本轮已耗时间；一轮比窗口还慢时轮末写的窗口可能已过期 ⇒ 下一拍照样打一次（方向安全；⛔ 别把「写了窗口」读成「下 30 秒保证零请求」）。"""
    count = _UPSTREAM_FAILURE_COUNT.get(key, 0) + 1
    _UPSTREAM_FAILURE_COUNT[key] = count
    until = now + timedelta(seconds=_upstream_backoff_seconds(count))
    _UPSTREAM_PENALTY_UNTIL[key] = until
    return until


def _clear_upstream_failure(key) -> None:
    """记一次「这一轮干净」：连续失败计数清零 + 清掉退避窗口（⛔ 不留下一个过期的窗口）。"""
    _UPSTREAM_FAILURE_COUNT.pop(key, None)
    _UPSTREAM_PENALTY_UNTIL.pop(key, None)


# ── ⭐ 「这一轮的取数整体不打了」的两句人话（spec §9.5 ② / T12）──────────────────
#: 消费那一段（⓪）被挡下时的话。⚠️ **必须是常量**：它进 `findings` ⇒ 绑定 `last_error` （每轮覆盖一次），而台账的幂等键是「四元组含 `reason`」⇒ 带着会变的剩余秒数就会**每轮灌一条**。
#: ⚠️ 真正的「为什么被拒 + 对方原文」由**上一轮**那一档写在 `last_error` 上（这里不抢那一格）。
TOKEN_BLOCKED_CONSUME_REASON = (
    "这一轮的取数**整段跳过了**：上一步刚被鸡蛋铺拒过（本轮的等待窗口还没过）⇒ "
    "⛔ 不再反复打那个必被拒的接口（到点会自动重试）"
)
#: 兜底对账那一段（⓪'）被同一件事挡下时的话（⛔ 不复用上面那句：两段是两件事，同一句话
#: 会让「到底是哪一段没跑」查不出来）。
TOKEN_BLOCKED_RECONCILE_REASON = (
    "这一轮的兜底对账**跳过了**：同轮的取 token 那一跳刚被拒（等待窗口内）⇒ "
    "⛔ 不为同一个必被拒的接口再打一次（到点会自动重试）"
)


def _poll_seconds(model, field: str, value) -> int:
    """间隔列存了 `0`（配置写坏）⇒ 按**本表这一列自己的默认值**算（spec §10 / R22）。
    ⚠️ 0 是有意义的边界（E-0021 同族）＝「配置写坏了按默认走」，⛔ 不是「每轮都跑」。 ⚠️ 独立实现、吃「模型 + 列名」两个参数，⛔ 不复用写死单一模型的实现（实测读数见外移登记）；
    默认值的唯一出处是模型列本身（⛔ 不在这里再写一遍 30 / 60）。"""
    return value or model._meta.get_field(field).default


def _upstream_due(anchor, seconds, *, now) -> bool:
    """本线某条读路到点了吗（判据形状照 `_member_due` / `jidanpu_hall._segment_due`；
    ⚠️ `jidanpu_hall` 已随租户级那条线删除，这里同样只指**历史的**参照物）。"""
    if anchor is None:
        return True
    return anchor + timedelta(seconds=seconds) <= now


class Command(BaseCommand):
    # ⛔ **本命令只跑两段**：下家（代练员）**个人绑定**、上家**个人绑定**——
    # 租户级第 1 段（`jidanpu_hall`）已随租户级鸡蛋铺线整条拆除
    # （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。
    help = "鸡蛋铺同步一轮（下家个人绑定 + 上家个人绑定）"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fastpath_starts: dict[tuple[str, int], float] = {}

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="跑一轮（手工 / 排障）")
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="常驻（生产调度形态；systemd 单元拉起，⛔ 只在一台应用机上启用）",
        )
        # ⭐ 出向事件消费拆成独立进程的两个开关（互斥，见 `handle`）。
        parser.add_argument(
            "--outbox-only", action="store_true",
            help=(
                "只跑下家①式代练员绑定的出向事件消费（run_outbox），"
                "不跑日志快路 / 状态回拉 / 对账（独立单元 tuantuantong-jidanpu-outbox 用这个形态）"
            ),
        )
        parser.add_argument(
            "--skip-outbox", action="store_true",
            help=(
                "原有的一轮里跳过① outbox（其余三段照旧；出向由独立进程消费）。"
                "⛔ 上家线与②式绑定不受影响"
            ),
        )
        # ⭐ ②式（官方 API）回调消费再拆一个进程（互斥，见 `handle`）。
        parser.add_argument(
            "--callback-only", action="store_true",
            help=(
                "只跑下家②式（官方API）绑定的回调消费 + 兜底对账 + 出向发单，"
                "不跑①式任何一段、不跑上家线"
                "（独立单元 tuantuantong-jidanpu-callback 用这个形态）"
            ),
        )
        parser.add_argument(
            "--skip-callback", action="store_true",
            help=(
                "原有的一轮里只跳过②式（官方API）那一段（其余照旧；②式由独立进程消费）。"
                "⛔ ①式与上家线不受影响"
            ),
        )
        # ⭐ ①式「快路」再拆一个进程（含身份自愈，互斥，见 `handle`）。
        parser.add_argument(
            "--fastpath-only", action="store_true",
            help=(
                "只跑下家①式（代练员）绑定的身份自愈 + 日志快路（poll_log），"
                "不跑状态回拉 / 对账 / 出向 / ②式 / 上家线"
                "（独立单元 tuantuantong-jidanpu-fastpath 用这个形态）"
            ),
        )
        parser.add_argument(
            "--skip-fastpath", action="store_true",
            help=(
                "原有的一轮里跳过①式的身份自愈 + 日志快路（两者同进同出；快路由独立进程跑）。"
                "⛔ 回拉 / 对账 / ②式 / 上家线不受影响"
            ),
        )
        # ⭐ ①式「状态回拉 + 对账」再拆一个进程。
        # ⭐ 两段同在**这个**进程里，但**各有各的闸门**（回拉 status_pull_seconds、
        # 对账 reconcile_seconds）⇒ ⛔ 不许拆成两台（闸门与锚都是进程内的）。
        parser.add_argument(
            "--reconcile-only", action="store_true",
            help=(
                "只跑下家①式（代练员）绑定的状态回拉（pull_statuses）+ 对账（reconcile_and_queue）"
                "+ 接单兜底网（claim_net），回拉按 status_pull_seconds（默认 600）、"
                "对账按 reconcile_seconds（默认 180）、兜底网 180 秒一轮，"
                "各有各的闸门；不跑 heal / 快路 / 出向 / ②式 / 上家线"
                "（独立单元 tuantuantong-jidanpu-reconcile 用这个形态）"
            ),
        )
        parser.add_argument(
            "--skip-reconcile", action="store_true",
            help=(
                "原有的一轮里跳过①式的状态回拉 + 对账 + 接单兜底网（三段同进同出；由独立进程跑）。"
                "⛔ heal / 快路 / 出向 / ②式 / 上家线不受影响"
            ),
        )
        # ⭐ 上家线（JidanpuUpstreamBinding）**整行**再拆一个进程。
        # ⛔ 不许按「大厅 / 回拉 / 对账」子段拆（行级健康合成 + 整行退避，见模块 docstring）。
        parser.add_argument(
            "--upstream-only", action="store_true",
            help=(
                "只跑上家线（JidanpuUpstreamBinding）的整行绑定（①式 outbox + 大厅 + 我的订单；"
                "②式回调消费 + 兜底对账），不跑下家任何一段、不跑模拟单；"
                "⛔ 只能整行、不可再按子段拆（行级健康合成与整行退避）"
                "（独立单元 tuantuantong-jidanpu-upstream 用这个形态）"
            ),
        )
        parser.add_argument(
            "--skip-upstream", action="store_true",
            help=(
                "原有的一轮里跳过整行上家绑定（由上家独立进程跑）；"
                "⛔ 只跳上家线，下家四段与模拟单不受影响"
            ),
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        # ⛔ `--once` 与 `--loop` 不能同时给：`--once` 是跑一趟就退、`--loop` 是常驻，
        # 同时给会变成「静默常驻」（`--once` 被忽略）——宁可响亮报错。
        if options.get("once") and options.get("loop"):
            raise CommandError("--once 与 --loop 不能同时给（一个跑一趟、一个常驻）")
        # ⛔ 出向的两个开关也互斥：一个「只跑出向」、一个「跳过出向」，同时给只能挑一个执行
        # ⇒ 那就是静默忽略另一个（宁可响亮报错）。
        outbox_only = bool(options.get("outbox_only"))
        skip_outbox = bool(options.get("skip_outbox"))
        if outbox_only and skip_outbox:
            raise CommandError(
                "--outbox-only 与 --skip-outbox 不能同时给"
                "（一个只跑出向、一个跳过出向）"
            )
        # ⛔ ②式那几个开关同一形状、同一校验：一个「只跑回调」、一个「跳过回调」，同给只能挑一个
        # 执行 ⇒ 那就是静默忽略另一个（宁可响亮报错）。
        callback_only = bool(options.get("callback_only"))
        skip_callback = bool(options.get("skip_callback"))
        if callback_only and skip_callback:
            raise CommandError(
                "--callback-only 与 --skip-callback 不能同时给"
                "（一个只跑②式回调、一个跳过②式回调）"
            )
        # ⛔ 快路那一对同一形状、同一校验：一个「只跑快路」、一个「跳过快路」，
        # 同给只能挑一个执行 ⇒ 那就是静默忽略另一个（宁可响亮报错）。
        fastpath_only = bool(options.get("fastpath_only"))
        skip_fastpath = bool(options.get("skip_fastpath"))
        if fastpath_only and skip_fastpath:
            raise CommandError(
                "--fastpath-only 与 --skip-fastpath 不能同时给"
                "（一个只跑快路、一个跳过快路）"
            )
        # ⛔ 回拉 + 对账那一对同一形状、同一校验：一个「只跑对账」、
        # 一个「跳过对账」，同给只能挑一个执行 ⇒ 那就是静默忽略另一个（宁可响亮报错）。
        reconcile_only = bool(options.get("reconcile_only"))
        skip_reconcile = bool(options.get("skip_reconcile"))
        if reconcile_only and skip_reconcile:
            raise CommandError(
                "--reconcile-only 与 --skip-reconcile 不能同时给"
                "（一个只跑①式回拉 + 对账、一个跳过回拉 + 对账）"
            )
        # ⛔ 上家线那一对同一形状、同一校验：一个「只跑上家整行」、
        # 一个「跳过上家整行」，同给只能挑一个执行 ⇒ 那就是静默忽略另一个（宁可响亮报错）。
        upstream_only = bool(options.get("upstream_only"))
        skip_upstream = bool(options.get("skip_upstream"))
        if upstream_only and skip_upstream:
            raise CommandError(
                "--upstream-only 与 --skip-upstream 不能同时给"
                "（一个只跑上家线整行、一个跳过上家线整行）"
            )
        # ⛔ 两个「**只**跑某一段」的开关也互斥：同时给时命令只能进其中一个分支并 `return`
        # ⇒ 另一段被**静默**跳掉（无输出、无报错、无台账）。宁可响亮报错。
        if outbox_only and callback_only:
            raise CommandError(
                "--outbox-only 与 --callback-only 不能同时给"
                "（一个只跑①式出向、一个只跑②式回调；两段要都跑就别加 --*-only）"
            )
        # ⭐ 快路那台与上面两台同一口径：两个 only 同给 ⇒ 静默落一段。
        if fastpath_only and outbox_only:
            raise CommandError(
                "--fastpath-only 与 --outbox-only 不能同时给"
                "（一个只跑①式快路、一个只跑①式出向；两段要都跑就别加 --*-only）"
            )
        if fastpath_only and callback_only:
            raise CommandError(
                "--fastpath-only 与 --callback-only 不能同时给"
                "（一个只跑①式快路、一个只跑②式回调；两段要都跑就别加 --*-only）"
            )
        # ⭐ 回拉 + 对账那台与上面三台同一口径：两个 only 同给 ⇒ 静默落一段。
        if reconcile_only and outbox_only:
            raise CommandError(
                "--reconcile-only 与 --outbox-only 不能同时给"
                "（一个只跑①式回拉 + 对账、一个只跑①式出向；两段要都跑就别加 --*-only）"
            )
        if reconcile_only and callback_only:
            raise CommandError(
                "--reconcile-only 与 --callback-only 不能同时给"
                "（一个只跑①式回拉 + 对账、一个只跑②式回调；两段要都跑就别加 --*-only）"
            )
        if reconcile_only and fastpath_only:
            raise CommandError(
                "--reconcile-only 与 --fastpath-only 不能同时给"
                "（一个只跑①式回拉 + 对账、一个只跑①式快路；两段要都跑就别加 --*-only）"
            )
        # ⭐ 上家那台与上面四台同一口径：两个 only 同给 ⇒ 静默落一段。
        if upstream_only and outbox_only:
            raise CommandError(
                "--upstream-only 与 --outbox-only 不能同时给"
                "（一个只跑上家线整行、一个只跑下家①式出向；两段要都跑就别加 --*-only）"
            )
        if upstream_only and callback_only:
            raise CommandError(
                "--upstream-only 与 --callback-only 不能同时给"
                "（一个只跑上家线整行、一个只跑下家②式回调；两段要都跑就别加 --*-only）"
            )
        if upstream_only and fastpath_only:
            raise CommandError(
                "--upstream-only 与 --fastpath-only 不能同时给"
                "（一个只跑上家线整行、一个只跑下家①式快路；两段要都跑就别加 --*-only）"
            )
        if upstream_only and reconcile_only:
            raise CommandError(
                "--upstream-only 与 --reconcile-only 不能同时给"
                "（一个只跑上家线整行、一个只跑下家①式回拉 + 对账；两段要都跑就别加 --*-only）"
            )
        # ⚠️ 上游客户端池是**进程级**的（spec §2.1）：`--loop` 下跨轮复用 ⇒ 同一绑定只登录一次； ⛔ `release_all()` 不放 `atexit`（要跟**这一次** `handle` 的生命周期走；测试一个进程跑很多趟
        # `call_command`），`finally` 同时兜住 `--once` 跑完 / `CommandError` / `Ctrl-C` 三种收尾。 ⭐ 优雅停机：信号是进程级的 ⇒ `--loop` 在这里注册一次 SIGTERM / SIGINT（六条循环共用 `_STOP`，
        # 重复注册只是覆盖 handler）；`--once` 不注册（跑一趟就退，没有「当前轮」可言）。
        if options.get("loop"):
            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _request_stop)
        try:
            if outbox_only:
                # ⭐ 独立出向进程（`--loop --outbox-only`）；`--once --outbox-only` 是手工排障形态：
                # 跑一趟就退（与下面那条 `--once` 的退出码契约一样：出事 ⇒ 非零）。
                # ⭐ 睡眠走 `Event.wait`（D2 优雅停机）：停止位置位 ⇒ 当拍醒来退出。
                if options.get("loop"):
                    self._loop_outbox_with_direct_assignment(schema)
                    return
                direct_failed = self._run_direct_assignment_once(schema)
                if self._run_outbox_once(schema) or direct_failed:
                    raise CommandError(
                        "有租户的本轮出向消费出现未预期失败，"
                        "详见上方汇总与各绑定上的 last_error",
                        returncode=1,
                    )
                return
            if callback_only:
                # ⭐ 独立②式回调进程（`--loop --callback-only`）；`--once` 形态跑一趟就退（出事 ⇒ 非零）。
                # ⚠️ 间隔取 `LOOP_SLEEP_SECONDS`：回调消费不设间隔闸（跟每一拍跑）、兜底对账有自己的
                # `reconcile_seconds` 闸 ⇒ 拍子再快只是空转读一遍回调文件。
                if options.get("loop"):
                    while not _STOP.is_set():
                        self._run_callback_once(schema)
                        _STOP.wait(LOOP_SLEEP_SECONDS)
                if self._run_callback_once(schema):
                    raise CommandError(
                        "有租户的本轮②式回调消费出现未预期失败，"
                        "详见上方汇总与各绑定上的 last_error",
                        returncode=1,
                    )
                return
            if fastpath_only:
                # ⭐ 独立①式快路进程（`--loop --fastpath-only`）；`--once` 形态跑一趟就退（出事 ⇒ 非零）。
                # ⚠️ 含身份自愈（⛔ 不能留主进程：两个进程会重复自愈）；节奏取 `LOOP_SLEEP_SECONDS`
                # （快路自己的闸门 `MEMBER_FAST_POLL_SECONDS` 默认就是 3 秒，更快只会空转）。
                if options.get("loop"):
                    while not _STOP.is_set():
                        started = time.monotonic()
                        self._run_fastpath_once(schema, round_started=started)
                        remaining = started + LOOP_SLEEP_SECONDS - time.monotonic()
                        if remaining > 0 and not _STOP.is_set():
                            _STOP.wait(remaining)
                    return
                if self._run_fastpath_once(schema):
                    raise CommandError(
                        "有租户的本轮快路（含身份自愈）出现未预期失败，"
                        "详见上方汇总与各绑定上的 last_error",
                        returncode=1,
                    )
                return
            if reconcile_only:
                # ⭐ 独立①式回拉 + 对账进程（`--loop --reconcile-only`）；`--once` 形态跑一趟就退（出事 ⇒ 非零）。
                # ⚠️ 两段**必须同一个单元**（闸门与锚都是进程内的，拆两台 = 各算一次闸门、同一段慢路跑两遍），但**各有各的闸门**（回拉 `status_pull_seconds` 默认 600 / 对账 `MEMBER_RECONCILE_SECONDS`
                # 默认 180）⇒ 互不阻塞；节奏取 `LOOP_SLEEP_SECONDS`（两条慢路 ≥180 秒，拍快只是空转）。
                if options.get("loop"):
                    while not _STOP.is_set():
                        self._run_reconcile_once(schema)
                        _STOP.wait(LOOP_SLEEP_SECONDS)
                if self._run_reconcile_once(schema):
                    raise CommandError(
                        "有租户的本轮状态回拉 + 对账出现未预期失败，"
                        "详见上方汇总与各绑定上的 last_error",
                        returncode=1,
                    )
                return
            if upstream_only:
                # ⭐ 独立上家线进程（`--loop --upstream-only`）；`--once` 形态跑一趟就退（出事 ⇒ 非零）。
                # ⚠️ **整行**（`_run_upstream_binding`）：①式 / ②式各段全部同一台，⛔ 不可再按子段拆（行级健康合成 + 整行退避）；节奏取 `LOOP_SLEEP_SECONDS`（动作频率由各绑定自己的
                # 闸门决定——大厅 300 / 我的订单 60 / 对账 600，outbox 无闸，拍快只是空转）。
                if options.get("loop"):
                    while not _STOP.is_set():
                        self._run_upstream_once(schema)
                        _STOP.wait(LOOP_SLEEP_SECONDS)
                if self._run_upstream_once(schema):
                    raise CommandError(
                        "有租户的本轮上家线出现未预期失败，"
                        "详见上方汇总与各绑定上的 last_error",
                        returncode=1,
                    )
                return
            if options.get("loop"):
                # ⭐ 优雅停机（D2）：停止位置位 ⇒ 当轮跑完就 `return`（⛔ 不落到下面那条
                # `--once` 的退出码检查——那会**再多跑一趟**、并按它的契约误判非零；
                # 常驻形态的告警判据是 `last_ok_at`，不是退出码）。
                while not _STOP.is_set():
                    self._run_once(
                        schema, skip_outbox=skip_outbox, skip_callback=skip_callback,
                        skip_fastpath=skip_fastpath, skip_reconcile=skip_reconcile,
                        skip_upstream=skip_upstream,
                    )
                    _STOP.wait(LOOP_SLEEP_SECONDS)
                return
            if self._run_once(
                schema, skip_outbox=skip_outbox, skip_callback=skip_callback,
                skip_fastpath=skip_fastpath, skip_reconcile=skip_reconcile,
                skip_upstream=skip_upstream,
            ):
                # ⚠️ 退出码契约：未预期失败 ⇒ 非零，cron 才会发信；
                # 预期内的可重试失败**不**走这里（那些是每分钟都可能有的噪音）。
                raise CommandError(
                    "有租户的本轮出现未预期失败，详见上方汇总与各租户绑定上的 last_error",
                    returncode=1,
                )
        finally:
            self._release_upstream_pool()

    #: ⭐ 上游 SDK 客户端池（`--loop` 跨轮复用 ⇒ 同一绑定只登录一次；spec §2.1）。
    #: ⚠️ **懒建**（`_upstream_pool()`）：① `Command` 可能被直接实例化去调 `_run_upstream_binding`
    #: （用例里有）；② ②式一整趟不碰它。⛔ 不做模块级全局单例（测试之间会串味）。
    _upstream_pool_instance: "jidanpu.UpstreamClientPool | None" = None

    def _upstream_pool(self) -> "jidanpu.UpstreamClientPool":
        """本进程的上游客户端池（懒建；⛔ 不是模块级全局单例）。"""
        if self._upstream_pool_instance is None:
            self._upstream_pool_instance = jidanpu.UpstreamClientPool()
        return self._upstream_pool_instance

    def _release_upstream_pool(self) -> None:
        """收尾：关掉池里所有客户端（`--once` 跑完 / `--loop` 退出 / 被打断时各调一次）。"""
        if self._upstream_pool_instance is not None:
            self._upstream_pool_instance.release_all()

    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`，spec §9）。
        ⚠️ `--tenant` 是排障闸门（spec §9 命令签名），⛔ 不提供 `--schema` 别名（会变成没有文档的别名）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_once(
        self, schema, *, skip_outbox: bool = False, skip_callback: bool = False,
        skip_fastpath: bool = False, skip_reconcile: bool = False,
        skip_upstream: bool = False,
    ) -> bool:
        """跑一趟所有（或指定的）租户，返回「是否出现过未预期失败」（退出码契约用）。 ⭐ 五个 `skip_*` 各只影响自己那一段（分别交给对应独立单元，语义见各调用方 docstring）；
        ⛔ 前四个都不影响上家线（它的 outbox / 回调有各自的顺序依赖，见模块 docstring）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            # ⚠️ 逐租户独立异常边界（spec §9）：一个租户失败**不影响**别的租户。
            # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
            try:
                with tenant_context(tenant):
                    if self._run_tenant(
                        tenant, skip_outbox=skip_outbox, skip_callback=skip_callback,
                        skip_fastpath=skip_fastpath, skip_reconcile=skip_reconcile,
                        skip_upstream=skip_upstream,
                    ):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # 能逃到这一层的是未预期失败（退出码契约）。
                # 本命令只跑下家个人绑定和上家个人绑定，两段各自的异常边界在
                # `_run_member_bindings` / `_run_upstream_bindings`（各自再逐绑定一层）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        return unexpected

    # ── ⭐ 独立出向进程（`--outbox-only`）───────────────────────────────────────
    #: 与 `_run_once` 同形（逐租户异常边界 + 返回「有没有未预期失败」），但**只**跑下家①式的
    #: 出向台账：⛔ 不跑轮询 / 回拉 / 对账、⛔ 不碰上家线与模拟单。

    def _run_outbox_once(self, schema) -> bool:
        """跑一趟**只消费出向事件**；返回「是否出现过未预期失败」（`--once` 的退出码契约用）。
        ⚠️ 逐租户异常边界与 `_run_once` 同口径；**进程级**异常不在这里接——响亮退出非 0，
        由 systemd 的 `Restart=always` 拉起。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant_outbox(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 出向消费本轮失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        return unexpected

    def _loop_outbox_with_direct_assignment(self, schema):
        stop = threading.Event()
        direct_worker = threading.Thread(
            name="jidanpu-direct-assignment", target=self._loop_direct_assignment, args=(schema, stop), daemon=True,
        )
        direct_worker.start()
        try:
            while not _STOP.is_set():
                self._run_outbox_once(schema)
                if not direct_worker.is_alive():
                    raise CommandError("鸡蛋铺直派线程已退出，重启出向进程后恢复。")
                _STOP.wait(OUTBOX_LOOP_SLEEP_SECONDS)
        finally:
            stop.set()
            direct_worker.join(timeout=30)

    def _loop_direct_assignment(self, schema, stop):
        pool = jidanpu.UpstreamClientPool()
        unset = object()
        last_outlet = unset

        @contextmanager
        def session(binding):
            nonlocal last_outlet
            outlet = jidanpu_proxy.proxy_url()
            if last_outlet is not unset and outlet != last_outlet:
                pool.mark_all_stale()
            last_outlet = outlet
            try:
                with pool.session(binding) as pair:
                    outlet = self._egress_outlet_of(pair[0]) or outlet
                    yield pair
            except AuthError:
                pool.invalidate(binding)
                raise
            except jidanpu.UpstreamBlockedError:
                self._note_egress_blocked(stage="下家/直派", outlet=outlet, force=True)
                raise

        try:
            while not stop.is_set():
                close_old_connections()
                self._run_direct_assignment_once(schema, session=session, stop=stop)
                stop.wait(DIRECT_ASSIGNMENT_SLEEP_SECONDS)
        finally:
            pool.release_all()
            connections.close_all()

    def _run_direct_assignment_once(self, schema, *, session=None, stop=None):
        unexpected = False
        try:
            tenants = self._iter_tenants(schema)
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"鸡蛋铺直派本轮无法读取租户（{type(exc).__name__}）")
            return True
        for tenant in tenants:
            if stop is not None and stop.is_set():
                break
            try:
                with tenant_context(tenant):
                    jidanpu_direct_queue.run_pending(session=session, stop=stop)
            except Exception as exc:  # noqa: BLE001
                unexpected = True
                self.stderr.write(f"{tenant.schema_name}: 鸡蛋铺直派本轮失败（{type(exc).__name__}）")
        return unexpected

    def _run_tenant_outbox(self, tenant) -> bool:
        """在一个租户里只跑**启用中的①式代练员绑定**的 `run_outbox`；回「有没有未预期失败」。 ⚠️⛔ **只跑①式**：②式的出向发单必须排在同轮回调消费之后（回调消费补上缺的远端系统单号，
        发单每一档都要它 ⇒ 拆到本进程 = 拿空号去办），②式整段归 `--callback-only` 单元（⛔ `--skip-outbox` 与本进程都不动它）；判据只看 `mode`（`is_openapi_binding` 唯一出处，⛔ 不拿 `client_id` 真值反推）。
        ⚠️ 停用 / 凭据不全 / 逐绑定异常边界照 `_run_member_bindings` 同口径（凭据不全不筛 = 每 2 秒在建连处抛 `ConfigError`，把「还没配好」报成「同步坏了」）。"""
        now = timezone.now()
        unexpected = False
        for binding in jidanpu_member.enabled_bindings():
            if jidanpu_openapi.is_openapi_binding(binding):
                continue
            if self._member_credentials_missing(binding):
                continue
            stats = None
            try:
                stats = jidanpu_member.run_outbox(binding, now=now)
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §8.5-S1）。
                unexpected = True
                stats = getattr(exc, "stats", None)
                if isinstance(exc, AuthError):
                    jidanpu.member_pool().invalidate(binding)
                self._note_member_blocked_stage(exc, "outbox", binding)
                self._member_failure(tenant, binding, "outbox", exc)
            else:
                # ⭐ 出口池：独立单元也必须**成功即复位**，否则只有主一轮在复位 ⇒ 共享状态下自动换出口失效。⛔ **但只有「真跑过」才复位**：台账空的一轮 `run_outbox` 照样回
                # `OutboxStats()`（done=0、一次上游都没打）⇒ 不看 `done` 的话，空闲轮每 3 秒就把
                # 连续拦截计数清零，上家永远攒不到 3 次。
                if stats is not None and getattr(stats, "done", 0) > 0:
                    jidanpu_proxy.note_ok()
            self.stdout.write(
                f"{tenant.schema_name}: 代练员绑定 #{binding.pk}（出向） · "
                f"{self._member_outbox_text(stats)}"
            )
        return unexpected

    # ── ⭐ 独立②式回调进程（`--callback-only`）──────────────────────────────
    #: 与 `_run_outbox_once` 同形（逐租户异常边界 + 返回「有没有未预期失败」），但**只**跑下家
    #: ②式那一条支（回调消费 → 兜底对账 → 出向发单）：⛔ 不跑①式任何一段、⛔ 不碰上家线与模拟单。

    def _run_callback_once(self, schema) -> bool:
        """跑一趟**只消费②式回调 + 兜底对账 + 出向发单**；回「是否出现过未预期失败」。
        ⚠️ 逐租户异常边界与 `_run_once` 同口径；**进程级**异常不在这里接——响亮退出非 0，
        由 systemd 的 `Restart=always` 拉起（与出向那个进程同一口径）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant_callback(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: ②式回调消费本轮失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        return unexpected

    def _run_tenant_callback(self, tenant) -> bool:
        """在一个租户里只跑**启用中的②式代练员绑定**那一轮；回「有没有未预期失败」。 ⚠️ **只跑②式**：①式各段要①式登录凭据与池里的会话，留在主单元；判据只看 `mode`
        （`is_openapi_binding` 唯一出处，⛔ 不拿 `client_id` 真值反推）。停用 / 凭据不全 / 逐绑定异常边界照 `_run_member_bindings` 同口径；⛔ 不 invalidate ①式登录池
        （②式三段自建官方 API 客户端，一个字节都不碰 `jidanpu.member_pool()`）。"""
        now = timezone.now()
        unexpected = False
        for binding in jidanpu_member.enabled_bindings():
            if not jidanpu_openapi.is_openapi_binding(binding):
                continue
            if self._member_credentials_missing(binding):
                continue
            try:
                if self._run_member_openapi_binding(tenant, binding, now=now):
                    unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # 能逃到这一层的异常**按定义**是未预期的（三段各自已接住自己的）。
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §8.5-S1）。
                unexpected = True
                self._member_failure(tenant, binding, "本轮", exc)
        return unexpected

    # ── ⭐ 独立①式快路进程（`--fastpath-only`）──────────────────────────────
    #: 与 `_run_outbox_once` / `_run_callback_once` 同形（逐租户异常边界 + 返回「有没有未预期
    #: 失败」），但**只**跑下家①式的 **⓪ 身份自愈 + ② 日志快路**：⛔ 不跑回拉 / 对账 / 出向、 ⛔ 不跑②式、⛔ 不碰上家线与模拟单。

    def _run_fastpath_once(self, schema, *, round_started=None) -> bool:
        """跑一趟**只跑①式身份自愈 + 日志快路**；回「是否出现过未预期失败」。
        ⚠️ 逐租户异常边界与 `_run_once` 同口径；**进程级**异常不在这里接——响亮退出非 0，
        由 systemd 的 `Restart=always` 拉起（与另外两个独立进程同一口径）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant_fastpath(tenant, round_started=round_started):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 快路（含身份自愈）本轮失败"
                    f"（{type(exc).__name__}），下一个租户继续"
                )
        return unexpected

    def _run_tenant_fastpath(self, tenant, *, round_started=None) -> bool:
        """在一个租户里只跑**启用中的①式代练员绑定**的 ⓪ 自愈 + ② 快路；回「有没有未预期失败」。
        ⚠️ 只走①式（②式的自愈与快路都要①式登录凭据，`_run_member_openapi_binding` 表里逐段写明；
        ⛔ 不拿官方 API 凭据去跑一条登录式读路）；停用 / 凭据不全 / 逐绑定异常边界照 `_run_member_bindings` 同口径。"""
        unexpected = False
        for binding in jidanpu_member.enabled_bindings():
            if jidanpu_openapi.is_openapi_binding(binding):
                continue
            if self._member_credentials_missing(binding):
                continue
            now = timezone.now()
            fast_seconds = _member_interval(
                binding.fast_poll_seconds, default=MEMBER_FAST_POLL_SECONDS
            )
            # ⭐ **行级退避**：独立快路单元同样要守——否则主一轮在退避、这台却照打（审计 M-10 的
            # 「每 3 秒自证机器人」正是这台）。跳过时的人话由 `_member_penalty_skip` 自己打 ⇒ 直接 `continue`。
            if self._member_penalty_skip(tenant, binding, now=now):
                continue
            jidanpu_member.LAST_CALL.reset()
            try:
                fast_unexpected, lines, fast_due = self._run_member_fastpath(
                    tenant, binding, now=now, fast_seconds=fast_seconds, round_started=round_started,
                )
                if fast_unexpected:
                    unexpected = True
                else:
                    # ⭐ 独立快路单元成功即复位连续拦截计数；⛔ **但只有「真跑过」才复位**—— 没到点的一轮一次上游都没打（`fast_due=False`），无条件 `note_ok()` 会把共享状态
                    # 里的计数每 3 秒清零 ⇒ 自动换出口失效。判据同主一轮（`ran_a_stage`）：快路自报「真的建连并调通」；撞过被吞的 468（`LAST_CALL.blocked`）⛔ 不算干净轮。
                    upstream_called, swallowed_block = jidanpu_member.LAST_CALL.take()
                    if fast_due and upstream_called and not swallowed_block:
                        jidanpu_proxy.note_ok()
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # 能逃到这一层的异常**按定义**是未预期的（自愈与快路各自已接住自己的）。
                unexpected = True
                self._note_member_blocked_stage(
                    exc, "快路", binding, outlet=jidanpu_member.LAST_CALL.outlet
                )
                self._member_failure(tenant, binding, "快路", exc)
                lines = None
                fast_due = True
            self.stdout.write(
                f"{tenant.schema_name}: 代练员绑定 #{binding.pk}（身份自愈 + 快路） · "
                f"{self._member_fast_text(lines, due=fast_due, seconds=fast_seconds)}"
            )
        return unexpected

    # ── ⭐ 独立①式回拉 + 对账进程（`--reconcile-only`）─────────────────────────
    #: 与 `_run_outbox_once` / `_run_callback_once` / `_run_fastpath_once` 同形（逐租户异常边界 + 返回「有没有未预期失败」），但**只**跑下家①式的 **③ 状态回拉 + ④ 对账排事件 + ⑤ 接单兜底网**
    #: （三段**各有各的闸门**：回拉 `status_pull_seconds` / 对账 `reconcile_seconds` / 兜底网 `CLAIM_NET_SECONDS`，见 `_run_member_reconcile`）：⛔ 不跑 heal / 快路 / 出向 / ②式、 ⛔ 不碰上家线与模拟单。

    def _run_reconcile_once(self, schema) -> bool:
        """跑一趟**只跑①式状态回拉 + 对账 + 接单兜底网**；回「是否出现过未预期失败」。 ⚠️ 逐租户异常边界与 `_run_once` 同口径；**进程级**异常不在这里接——响亮退出非 0，
        由 systemd 的 `Restart=always` 拉起（与另外三个独立进程同一口径）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant_reconcile(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 状态回拉 + 对账本轮失败"
                    f"（{type(exc).__name__}），下一个租户继续"
                )
        return unexpected

    def _run_tenant_reconcile(self, tenant) -> bool:
        """在一个租户里只跑**启用中的①式代练员绑定**的 ③ 回拉 + ④ 对账 + ⑤ 兜底网；回「有没有未预期失败」。 ⚠️ 只走①式（②式的 ③/④/⑤ 按设计不跑：都要①式登录凭据，见 `_run_member_openapi_binding` 表）；
        停用 / 凭据不全 / 逐绑定异常边界照 `_run_member_bindings` 同口径。 ⭐ 各段间隔**各自算好传下去**、人话汇总**各报各的**——否则真机上看不出「回拉没跑」
        是没到点还是没跑成。"""
        now = timezone.now()
        unexpected = False
        for binding in jidanpu_member.enabled_bindings():
            if jidanpu_openapi.is_openapi_binding(binding):
                continue
            if self._member_credentials_missing(binding):
                continue
            status_pull_seconds = _member_interval(
                binding.status_pull_seconds, default=MEMBER_STATUS_PULL_SECONDS
            )
            reconcile_seconds = _member_interval(
                binding.reconcile_seconds, default=MEMBER_RECONCILE_SECONDS
            )
            jidanpu_member.LAST_CALL.reset()
            try:
                (rec_unexpected, applied, queued, claimed,
                 status_pull_due, reconcile_due, claim_net_due) = self._run_member_reconcile(
                    tenant, binding, now=now,
                    status_pull_seconds=status_pull_seconds,
                    reconcile_seconds=reconcile_seconds,
                )
                if rec_unexpected:
                    unexpected = True
                else:
                    # ⭐ 独立回拉 / 对账单元成功即复位；⛔ **但只有「真跑过」才复位**（三道闸都没到点的
                    # 一轮一次上游都没打）。⚠️ 与主一轮同一条 `ran_a_stage` 判据：任一条到点的读路自报
                    # 「真的建连并调通」（⑤ 的上游证据由 `_claim_net` 自己写进 `LAST_CALL.upstream`）；
                    # 撞过被吞的 468（`LAST_CALL.blocked`）⛔ 不算干净轮。
                    upstream_called, swallowed_block = jidanpu_member.LAST_CALL.take()
                    ran_a_stage = (
                        status_pull_due or reconcile_due or claim_net_due
                    ) and upstream_called
                    if ran_a_stage and not swallowed_block:
                        jidanpu_proxy.note_ok()
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # 能逃到这一层的异常**按定义**是未预期的（回拉与对账各自已接住自己的）。
                unexpected = True
                self._member_failure(tenant, binding, "回拉 + 对账", exc)
                applied = None
                queued = None
                claimed = None
                status_pull_due = True
                reconcile_due = True
                claim_net_due = True
            self.stdout.write(
                f"{tenant.schema_name}: 代练员绑定 #{binding.pk}（回拉 + 对账） · "
                f"{self._member_pull_text(applied, due=status_pull_due, seconds=status_pull_seconds)} · "
                f"{self._member_reconcile_text(queued, due=reconcile_due, seconds=reconcile_seconds)} · "
                f"{self._member_claim_net_text(claimed, due=claim_net_due, seconds=CLAIM_NET_SECONDS)}"
            )
        return unexpected

    # ── ⭐ 独立上家线进程（`--upstream-only`）────────────────────────────────
    #: 与 `_run_outbox_once` / `_run_callback_once` / `_run_fastpath_once` / `_run_reconcile_once` 同形（逐租户异常边界 + 返回「有没有未预期失败」），但**只**跑上家线
    #: 绑定的**整行**（`_run_upstream_binding`）：⛔ 不跑下家任何一段、⛔ 不跑模拟单。 ⚠️⛔ **整行不可再拆**：健康合成判据与连续失败退避都是**行级**的（见模块 docstring）。

    def _run_upstream_once(self, schema) -> bool:
        """跑一趟**只跑上家线整行**；回「是否出现过未预期失败」。 ⚠️ 逐租户异常边界与 `_run_once` 同口径；**进程级**异常不在这里接——响亮退出非 0，
        由 systemd 的 `Restart=always` 拉起（与另外四个独立进程同一口径）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant_upstream(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（spec §9）
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 上家线本轮失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        return unexpected

    def _run_tenant_upstream(self, tenant) -> bool:
        """在一个租户里只跑**启用中的上家线绑定**（整行）；回「有没有未预期失败」。 ⚠️ 就是 `_run_upstream_bindings` 那一层：停用 / 凭据不全 / 凭据退避的跳过判据、逐绑定异常边界、
        健康合成与整行退避**全部原样复用**——本方法只补「只跑这一层」这个入口，⛔ 不复制任何一段实现
        （复制就会与主一轮各算一次行级判据）。"""
        return self._run_upstream_bindings(tenant, now=timezone.now())

    def _run_tenant(self, tenant, *, skip_outbox: bool = False,
                    skip_callback: bool = False, skip_fastpath: bool = False,
                    skip_reconcile: bool = False, skip_upstream: bool = False) -> bool:
        """在一个租户里跑一轮：**先**逐个代练员个人绑定，**再**逐个上家本线绑定（spec §10 入口表）。返回这一轮是否出现未预期失败（跳过 ⇒ `False`）。两段各有自己的「逐 X 独立异常边界」：
        一个绑定坏了 ⛔ 不影响同租户其他绑定与另一条线；前一段抛未预期异常走它自己的边界（下一段下一轮再跑——接本线之前就有的行为，⛔ 不在这里改）。 ⭐ `skip_callback` / `skip_fastpath` / `skip_reconcile` 只影响个人绑定段里对应的行 / 段、
        `skip_upstream` 整行跳过上家线（⛔ 互不越界）；五个 skip 全给 ⇒ 一轮只剩租户级模拟单轮（主单元今天的形态）。"""
        now = timezone.now()
        unexpected = False
        # ⭐ 大厅模拟单轮（D-0296）：**租户级**的一段（不挂任何绑定），排在两条绑定线之前。 ⚠️ 自己的 try/except + logger：这一段炸了**不许**炸掉后面的绑定线、也不许把本租户记成
        # 未预期失败（模拟卡是演示数据）。⚠️ 开关只关「生成新单」段；回收段（含快照 7 天清理）在开关门外**恒跑**（它是全租户唯一的快照回收点）。
        sim_key = tenant.schema_name
        if _sim_due(_HALL_SIM_AT.get(sim_key), hall_simulated.ROUND_PERIOD_SECONDS, now=now):
            try:
                hall_simulated.run_tenant_round(now=now)
            except Exception as exc:  # noqa: BLE001 —— 逐段异常边界（演示数据不许炸业务）
                logger.warning(
                    "模拟单轮次失败（%s：%s），下一个租户继续",
                    tenant.schema_name, type(exc).__name__,
                )
            finally:
                # ⚠️ 失败也打锚（照两条读路的同一口径：别让一轮失败被每一拍重放）。
                _HALL_SIM_AT[sim_key] = now
        if self._run_member_bindings(
            tenant, now=now, skip_outbox=skip_outbox, skip_callback=skip_callback,
            skip_fastpath=skip_fastpath, skip_reconcile=skip_reconcile,
        ):
            unexpected = True
        if skip_upstream:
            # ⛔ 不静默：让运维一眼看出上家线这一段为什么没跑（与 skip-* 系列同一口径）。
            self.stdout.write(
                f"{tenant.schema_name}: 上家线跳过"
                "（--skip-upstream：由独立进程 tuantuantong-jidanpu-upstream 跑）"
            )
        elif self._run_upstream_bindings(tenant, now=now):
            unexpected = True
        return unexpected

    def _mark_egress_pools_stale(self) -> None:
        """出口变了 ⇒ **两个池**里的所有客户端都作废（下一次 `acquire` 换新）。 ⚠️ **整池而不是一条**：出口是进程级的（`proxy_url()` 建连时烙进客户端），只丢一条 ⇒ 其余绑定
        继续从旧出口发请求、468 记到**新**出口账上 = 误隔离健康出口。⚠️ **只打标记、⛔ 不当场关**：切换常发生在段中间，当场关会让后续段撞 `RuntimeError: client has been closed`（实测假故障：
        被记成「我的订单未完成」+ 整轮退避）；真正关闭 / 重建留给下一次 `acquire`。"""
        self._upstream_pool().mark_all_stale()
        jidanpu.member_pool().mark_all_stale()

    def _note_egress_blocked(self, *, stage: str, outlet: str | None = None, force: bool = False) -> None:
        """对端 WAF / 风控拦截 ⇒ 记出口池；**出口真的变了**才整池作废 + 告警（三动作各有条件，⛔ 别合并判据）。 1. 计数：每次拦截都记（阈值由 `jidanpu_proxy.note_blocked()` 自己判）；2. 整池作废（只打标记，
        ⛔ 不当场关）：只在 `switched` / `exhausted` 时——客户端是建连时按当时出口烙的，只推进下标不作废 = 「换出口」只是日志一句话；`counted` 不标记（468 是 IP 被封不是会话坏，重登是白送）；3. 告警：
        同样只在出口变了时、两句文案分开（`exhausted` 没有「下一个出口」，写「已切换」是假话）。 ⛔ 文案只许带 `<下标>:<host>`：代理账号 / 密码绝不能进 journald 与运维终端（全局约束）。"""
        if force:
            outcome = jidanpu_proxy.note_blocked(outlet, force=True)
        else:
            outcome = jidanpu_proxy.note_blocked(outlet)
        if outcome == jidanpu_proxy.BLOCK_COUNTED:
            return
        # 出口变了（换下一个 / 第一次回落到直连）⇒ 整池作废；⚠️ 只打标记，⛔ 不当场关。
        self._mark_egress_pools_stale()
        label = jidanpu_proxy.current_label()
        if outcome == jidanpu_proxy.BLOCK_SWITCHED:
            logger.warning(
                "出站被拦截（%s），已切换到下一个出口：outlet=%s", stage, label,
            )
        else:
            logger.warning(
                "出站被拦截（%s），全部出口都在隔离期，已回落直连：outlet=%s", stage, label,
            )

    def _note_upstream_blocked_stage(self, exc, stage: str, binding, client=None) -> None:
        """上家某段（或整行）抛了 468 / WAF 拦截 ⇒ 记出口池；其它异常一律不记。 ⚠️ **承重**：每一段都有自己的 `except Exception` 且 ⛔ 不 re-raise ⇒ 真实路径上 468 到不了
        逐绑定那层的 `except`，不在这里记「连续 3 次才换出口」生产上**永远不触发**（改前就是这个洞）。
        同一次 468 只被**最内层**边界接住 ⇒ ⛔ 不会重复计数。"""
        if not isinstance(exc, jidanpu.UpstreamBlockedError):
            return
        # ⚠️ 池**不在这里取**：`_mark_egress_pools_stale()` 自己取两个池（那是唯一出处）。
        # ⭐ **468 归属按 `client` 建连时用的出口**：给了客户端就按它记，
        # ⛔ 不是「记账那一刻的当前出口」——同一轮里旧出口客户端迟到的 468 不许记到新出口头上。
        self._note_egress_blocked(
            stage=f"上家/{stage}", outlet=self._egress_outlet_of(client)
        )

    @staticmethod
    def _egress_outlet_of(client) -> str | None:
        """这个客户端是**哪个出口**建出来的（`_open_client_pair` 烙的 `_tt_egress_outlet`）；取不到 ⇒ `None`（按「当前出口」记的老口径）。
        ⚠️ 必须判 `isinstance(..., str)`：替身客户端（用例里的 `mock.Mock()`）对任何属性都回 Mock
        ⇒ 不判类型会把 Mock 当出口标签，`note_blocked` 找不到它 ⇒ 永远只计数、永不换出口（假绿）。"""
        outlet = getattr(client, "_tt_egress_outlet", None)
        return outlet if isinstance(outlet, str) and outlet else None

    def _note_member_blocked_stage(self, exc, stage: str, binding, *, outlet=None) -> None:
        """下家某段（或整行）抛了 468 / WAF 拦截 ⇒ 记出口池（与上家同一口径）；②式同样走到这里。 ⚠️ ②式不登录（池里没有它那一份），但**出口是进程级 / 共享状态级** ⇒ 该作废的两个池一个都不少
        （出口变化统一走 `_mark_egress_pools_stale()`，⛔ 不按 mode 分叉）。⭐ 468 归属按下家最近一次真实建连用的出口（`jidanpu_member.LAST_CALL.outlet` 带出），⛔ 不传就按「记账那一刻的当前出口」
        记 ⇒ 同一轮里旧客户端的 468 会把**新出口**误隔离。"""
        if not isinstance(exc, jidanpu.UpstreamBlockedError):
            return
        # ⚠️ ②式那一份没有池内会话，但**出口照样是进程级的** ⇒ 该作废的两个池一个都不少（不是「②式不丢会话」：那是「没有它那一份」，⛔ 不是「别人的也不用换」）。
        # ⭐ **468 归属按下家那条线最近一次真实建连用的出口**（与上家同一口径）：下家各段自己建连（`member_session`），调度层手上没有 client ⇒ 出口由
        # `jidanpu_member.LAST_CALL.outlet` 带出来（`_poll_log` / `_pull_statuses` 建连时记的）。 ⛔ 不传就会按「记账那一刻的当前出口」记：同一轮里旧客户端的 468 会把**新出口**误隔离。
        self._note_egress_blocked(stage=f"下家/{stage}", outlet=outlet)

    def _run_member_bindings(self, tenant, *, now, skip_outbox: bool = False,
                             skip_callback: bool = False,
                             skip_fastpath: bool = False,
                             skip_reconcile: bool = False) -> bool:
        """本租户里**逐个启用中的个人绑定**跑一轮（下游线 spec §10）；回「是否出现未预期失败」。 ⚠️ 跳过停用行（`enabled_bindings` 已在查询里筛掉，spec §8.4）与凭据不全行，两种跳过都**不打印**
        （页面本有 `checked_at` / `last_run_at` / `last_error` 可看）。⭐ 凭据闸按 `mode` 分两支—— 承重，不是洁癖：②式界面形态就是账号密码为空，沿用①式判据 = 整行**静默**跳过
        （上家线踩过，见 `docs/ERRORS.md` 的 E-0202）。`skip_*` 各自的语义见 `_run_member_binding`。"""
        unexpected = False
        for binding in jidanpu_member.enabled_bindings():
            if self._member_credentials_missing(binding):
                continue
            if skip_callback and jidanpu_openapi.is_openapi_binding(binding):
                self.stdout.write(
                    f"{tenant.schema_name}: 代练员绑定 #{binding.pk}（官方API） · "
                    "回调消费/兜底对账/出向发单 跳过"
                    "（--skip-callback：由独立进程 tuantuantong-jidanpu-callback 消费）"
                )
                continue
            try:
                if self._run_member_binding(
                    tenant, binding, now=now, skip_outbox=skip_outbox,
                    skip_fastpath=skip_fastpath, skip_reconcile=skip_reconcile,
                ):
                    unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # 能逃到这一层的异常**按定义**是未预期的（四条路各自已接住自己的）； ⛔ 只写异常类型名（`str(exc)` 可能带上游回包 / 凭据片段，spec §8.5-S1）。 ⭐ AuthError / 468 ⇒ 丢这一行池里的会话，下一轮换出口重建（②式不登录，不走这条；
                # 468 原先落在 `ApiError` 上 ⇒ 会话一直复用一直 468，正是本线要堵的洞）。 ⚠️ 如实登记：这条判据在**真实路径**上不可达（各段边界先接住且不 re-raise），
                # 468 由段边界记账；`AuthError` 那一半只登记不修（见 `test_an_auth_error_from_a_stage_never_reaches_the_per_binding_boundary`）。
                if isinstance(
                    exc, (AuthError, jidanpu.UpstreamBlockedError)
                ) and not jidanpu_openapi.is_openapi_binding(binding):
                    jidanpu.member_pool().invalidate(binding)
                # ⚠️ 同一次 468 只会被**最内层**边界接住 ⇒ 与段边界那几处不会重复计数。
                # ⚠️ 阶段名按**线 + 方式**取（⛔ 别让两条线的告警看起来像同一件事）。
                self._note_member_blocked_stage(
                    exc,
                    "②式" if jidanpu_openapi.is_openapi_binding(binding) else "①式",
                    binding,
                )
                unexpected = True
                self._member_failure(tenant, binding, "本轮", exc)
        return unexpected

    @staticmethod
    def _member_credentials_missing(binding) -> bool:
        """这一行**按它自己那条路**的凭据判据是不是还不全（⛔ 不沿用另一条路的判据）。唯一出处 = ①式 `jidanpu_member.credentials_missing` / ②式 `jidanpu_openapi.openapi_credentials_of`
        （各自还会 `strip`，覆盖历史数据 / shell 直写）。与 `api/jidanpu_member_views.py::_credentials_missing` 同一口径（那里是「启用」门、这里是「同步」门）；⛔ 不 import 视图那份（管理命令不依赖 api 包）、
        也不在这里重写判空；判「走哪条路」只看 `mode`（⛔ 不拿 `client_id` 真值反推，E-0021 家族）。"""
        if jidanpu_openapi.is_openapi_binding(binding):
            return jidanpu_openapi.openapi_credentials_of(binding) is None
        return jidanpu_member.credentials_missing(binding)

    def _run_member_binding(
        self, tenant, binding, *, now, skip_outbox: bool = False,
        skip_fastpath: bool = False, skip_reconcile: bool = False,
    ) -> bool:
        """一个个人绑定的一轮：⓪ 自愈 → ① outbox → ② poll_log → ③ pull_statuses → ④ reconcile_and_queue → ⑤ claim_net（spec §10）。 ⭐ `skip_outbox` 只跳① outbox；`skip_fastpath` 跳 ⓪自愈+②快路（⛔ 同进同出，理由见
        `_heal_member_identity`）；`skip_reconcile` 跳 ③+④+⑤（同一开关，但三段各有间隔与进程内锚 ⇒ 互不阻塞；只跳一段 = 另一段每轮空转 + 与独立进程各算一次闸门）；`--skip-callback` 不经过本方法（②式行在调用方整行交给独立进程）。 ⭐ ⓪/② 抽成 `_heal_member_identity` / `_poll_member_log`，主一轮与独立快路单元共用同一份实现
        （⛔ 不复制、⛔ 不改 outbox → 快路的段序）；自愈失败只跳过自愈、绝不打断本轮（对齐口径见 D-0170）。本线没有公共连（各条路各自建连，自愈自建一次）。⚠️ 各条路各有异常边界、失败也打锚（否则持续失败的路每拍重打上游）；⭐ ②式行走 `_run_member_openapi_binding` （那几段都要①式登录凭据，跑了只有建连就抛 `ConfigError`）。"""
        if jidanpu_openapi.is_openapi_binding(binding):
            return self._run_member_openapi_binding(tenant, binding, now=now)
        key = (tenant.schema_name, binding.pk)
        # ⚠️ 每轮开头复位「这一拍的快路 / 回拉实况」：⛔ 不吃上一轮 /
        # web 端点留下来的标记。
        jidanpu_member.LAST_CALL.reset()
        # ⭐ **行级退避**：快路连续失败的窗口还没过 ⇒ 整行跳过（⓪/①/②/③④⑤ 全不跑）——
        # 被 WAF 挡住时这一行的任何一发都是同一个 IP 出去的，一起停才对（与上家线整行退避同口径）。
        # ⚠️ ②式行不进这道闸（分派在它之前 return；退避判据扩在①式读路上）。
        if self._member_penalty_skip(tenant, binding, now=now):
            return False
        fast_seconds = _member_interval(
            binding.fast_poll_seconds, default=MEMBER_FAST_POLL_SECONDS
        )
        #: ⭐ 回拉与对账**各算各的到点**，互不阻塞。
        status_pull_seconds = _member_interval(
            binding.status_pull_seconds, default=MEMBER_STATUS_PULL_SECONDS
        )
        status_pull_due = _member_due(
            _MEMBER_STATUS_PULL_AT.get(key), status_pull_seconds, now=now
        )
        reconcile_seconds = _member_interval(
            binding.reconcile_seconds, default=MEMBER_RECONCILE_SECONDS
        )
        reconcile_due = _member_due(
            _MEMBER_RECONCILE_AT.get(key), reconcile_seconds, now=now
        )
        # ⑤ 兜底网的节奏是常量（零迁移），闸门与锚照两条慢路的形状。
        claim_net_due = _member_due(
            _MEMBER_CLAIM_NET_AT.get(key), CLAIM_NET_SECONDS, now=now
        )
        unexpected = False

        # ⓪ 自愈（与 `_poll_member_log` 共用同一份实现；独立快路单元走 `_run_member_fastpath` 串联）。
        # ⚠️ 必须排在各路之前：这一轮快路的回声抑制与对账接手人比对要按**正确**显示名比，否则自己的日志行不被抑制 ⇒ 多走一次「回查远端 ⇒ 可能误接手」。
        # ⭐ 与②快路用同一个 `skip_fastpath` 开关 ⇒ 两段只能同进同出（⛔ 不会重复自愈）。
        if not skip_fastpath:
            self._heal_member_identity(binding)

        # ① outbox：**不设间隔闸**——已经排出去的台账要尽快变成真实动作。
        # ⭐ `--skip-outbox` 时**这一段整段不跑**（出向已拆到独立进程
        # `tuantuantong-jidanpu-outbox`）⇒ ⛔ 不调 `run_outbox`、也不改 `unexpected`。
        stats = None
        if not skip_outbox:
            try:
                stats = jidanpu_member.run_outbox(binding, now=now)
            except Exception as exc:  # noqa: BLE001 —— 逐路异常边界
                unexpected = True
                # `MemberOutboxUnexpectedFailures` 自带一份计数（它是逐事件边界跑完才抛的）
                # ⇒ 顺手报出来；别的异常没有这份计数 ⇒ 回 `None`，汇总说「未完成」。
                stats = getattr(exc, "stats", None)
                self._note_member_blocked_stage(exc, "outbox", binding)
                self._member_failure(tenant, binding, "outbox", exc)

        # ② 日志快路（按绑定自己的 `fast_poll_seconds`；⚠️ 顺序仍是 outbox → 快路，
        # 由 `test_jidanpu_member_schedule` 的段序网钉住）。
        # ⭐ `--skip-fastpath` 时**这一段不跑**（由 `tuantuantong-jidanpu-fastpath` 跑）。
        lines = None
        if skip_fastpath:
            fast_due = _member_due(_MEMBER_FAST_AT.get(key), fast_seconds, now=now)
        else:
            fast_unexpected, lines, fast_due = self._poll_member_log(
                tenant, binding, now=now, fast_seconds=fast_seconds,
            )
            if fast_unexpected:
                unexpected = True

        # ③ 状态回拉 + ④ 对账排事件 + ⑤ 接单兜底网：⭐ **三个独立的闸门**。⭐ `--skip-reconcile`
        # 时三段都不跑（独立进程与本方法共用 `_pull_member_statuses` / `_reconcile_member` /
        # `_claim_net_member` 这三份实现）⇒ ⛔ 不碰锚（锚由跑的那一方推进）。
        # ⑤ 排最后：先把对方的事实收敛完（③④），再用兜底网收口快路漏掉的「已被接走」。
        applied = None
        queued = None
        claimed = None
        if not skip_reconcile and status_pull_due:
            pull_unexpected, applied = self._pull_member_statuses(
                tenant, binding, now=now,
            )
            if pull_unexpected:
                unexpected = True
        if not skip_reconcile and reconcile_due:
            rec_unexpected, queued = self._reconcile_member(
                tenant, binding, now=now,
            )
            if rec_unexpected:
                unexpected = True
        if not skip_reconcile and claim_net_due:
            net_unexpected, claimed = self._claim_net_member(
                tenant, binding, now=now,
            )
            if net_unexpected:
                unexpected = True

        self.stdout.write(
            f"{tenant.schema_name}: 代练员绑定 #{binding.pk} · "
            f"{self._member_outbox_text(stats, skipped=skip_outbox)} · "
            f"{self._member_fast_text(lines, due=fast_due, seconds=fast_seconds, skipped=skip_fastpath)} · "
            f"{self._member_pull_text(applied, due=status_pull_due, seconds=status_pull_seconds, skipped=skip_reconcile)} · "
            f"{self._member_reconcile_text(queued, due=reconcile_due, seconds=reconcile_seconds, skipped=skip_reconcile)} · "
            f"{self._member_claim_net_text(claimed, due=claim_net_due, seconds=CLAIM_NET_SECONDS, skipped=skip_reconcile)}"
            # ⭐ 出口池线：逐绑定那一行带出口标签
            # （`<下标>:<host>`，⛔ 不含账号 / 密码 / 查询串）。
            f" · outlet={jidanpu_proxy.current_label()}"
        )
        # ⭐ **出口池**：这一轮**真跑过且干净**才清零。
        # 「真跑过」只认**真的打过上游**的证据（快路 / 回拉自报的 `LAST_CALL.upstream`）；
        # 撞过被吞的拦截（`LAST_CALL.blocked`）⇒ ⛔ 不许当干净轮。
        upstream_called, swallowed_block = jidanpu_member.LAST_CALL.take()
        done = getattr(stats, "done", 0) if stats is not None else 0
        ran_a_stage = upstream_called or (isinstance(done, int) and done > 0)
        if ran_a_stage and not unexpected and not swallowed_block:
            jidanpu_proxy.note_ok()
        return unexpected

    def _pull_member_statuses(self, tenant, binding, *, now):
        """①式的 **③ 状态回拉**（`pull_statuses`）；回 `(unexpected, applied)`。
        ⭐ 主一轮与独立单元共用同一份实现（自带闸门常量与锚；**闸门不在这里**——调用方先判到点，
        这里只负责跑 + 打锚 + 收异常）。⚠️ 失败也打锚（否则持续失败的回拉每拍重打上游）。"""
        applied = None
        unexpected = False
        try:
            applied = jidanpu_member.pull_statuses(binding, now=now)
        except Exception as exc:  # noqa: BLE001 —— 逐路异常边界
            unexpected = True
            self._note_member_blocked_stage(
                exc, "回拉", binding, outlet=jidanpu_member.LAST_CALL.outlet
            )
            self._member_failure(tenant, binding, "回拉", exc)
        finally:
            # ⚠️ 与快路同一条口径：失败也打锚。
            _MEMBER_STATUS_PULL_AT[(tenant.schema_name, binding.pk)] = now
        return unexpected, applied

    def _reconcile_member(self, tenant, binding, *, now):
        """①式的 **④ 补漏对账排事件**（`reconcile_and_queue`）；回 `(unexpected, queued)`。
        ⭐ 自带闸门与锚，与 ③ 互不影响；⚠️ 失败也打锚（同上，否则持续失败的对账每拍重打上游）。"""
        queued = None
        unexpected = False
        observe = os.environ.get("JIDANPU_RECONCILE_LOOKUP_LOG") == "1"
        started_at = timezone.now() if observe else None
        lookup_stats = None
        try:
            with jidanpu_member.track_reconcile_lookups() if observe else nullcontext() as lookup_stats:
                queued = jidanpu_member.reconcile_and_queue(binding, now=now)
        except Exception as exc:  # noqa: BLE001 —— 逐路异常边界
            unexpected = True
            # ⭐ 对账与回拉共用同一个会话（`member_session`）⇒ 归属同样按建连出口记。
            self._note_member_blocked_stage(
                exc, "对账", binding, outlet=jidanpu_member.LAST_CALL.outlet
            )
            self._member_failure(tenant, binding, "对账", exc)
        finally:
            _MEMBER_RECONCILE_AT[(tenant.schema_name, binding.pk)] = now
            if lookup_stats is not None:
                self.stdout.write(
                    "jidanpu_reconcile_lookup "
                    f"started_at={started_at.isoformat()} finished_at={timezone.now().isoformat()} "
                    f"lookups={lookup_stats.lookups} hits={lookup_stats.hits} "
                    f"lookup_ms={lookup_stats.elapsed_ns / 1_000_000:.3f} failed={int(unexpected)}"
                )
        return unexpected, queued

    def _claim_net_member(self, tenant, binding, *, now):
        """①式的 **⑤ 接单兜底网**（`claim_net`）；回 `(unexpected, claimed)`。
        ⭐ 自带锚（`_MEMBER_CLAIM_NET_AT`，节奏 `CLAIM_NET_SECONDS` 常量）；⚠️ 失败也打锚
        （与 ③④ 同一条口径，否则持续失败的网每拍重打上游）。"""
        claimed = None
        unexpected = False
        try:
            claimed = jidanpu_member.claim_net(binding, now=now)
        except Exception as exc:  # noqa: BLE001 —— 逐路异常边界
            unexpected = True
            # ⭐ 兜底网与回拉/对账共用同一个会话（`member_session`）⇒ 归属同样按建连出口记。
            self._note_member_blocked_stage(
                exc, "兜底网", binding, outlet=jidanpu_member.LAST_CALL.outlet
            )
            self._member_failure(tenant, binding, "兜底网", exc)
        finally:
            _MEMBER_CLAIM_NET_AT[(tenant.schema_name, binding.pk)] = now
        return unexpected, claimed

    def _run_member_reconcile(self, tenant, binding, *, now, status_pull_seconds=None,
                              reconcile_seconds=None):
        """一个①式绑定的 **③ 状态回拉 + ④ 对账 + ⑤ 接单兜底网**；回 `(unexpected, applied, queued, claimed, status_pull_due, reconcile_due, claim_net_due)`。
        独立回拉 + 对账单元（`--reconcile-only`）的入口：三段各判各的闸门后交给共用实现（主一轮同一份）。 ⭐ 三道闸门互相独立（回拉 600 / 对账 180 / 兜底网 180，各有进程内锚）；真机实测共用一个闸门只能二选一
        （调成 600 = 每 10 分钟排 450+ 条候选事件白打上游）。⚠️⛔ 三段必须留在同一个单元（拆开 = 同一段慢路被两台各跑一遍：重复回拉 + 重复排事件 + 重复兜底）。"""
        key = (tenant.schema_name, binding.pk)
        if status_pull_seconds is None:
            status_pull_seconds = _member_interval(
                binding.status_pull_seconds, default=MEMBER_STATUS_PULL_SECONDS
            )
        if reconcile_seconds is None:
            reconcile_seconds = _member_interval(
                binding.reconcile_seconds, default=MEMBER_RECONCILE_SECONDS
            )
        status_pull_due = _member_due(
            _MEMBER_STATUS_PULL_AT.get(key), status_pull_seconds, now=now
        )
        reconcile_due = _member_due(
            _MEMBER_RECONCILE_AT.get(key), reconcile_seconds, now=now
        )
        claim_net_due = _member_due(
            _MEMBER_CLAIM_NET_AT.get(key), CLAIM_NET_SECONDS, now=now
        )
        unexpected = False
        applied = None
        queued = None
        claimed = None
        if status_pull_due:
            pull_unexpected, applied = self._pull_member_statuses(
                tenant, binding, now=now,
            )
            if pull_unexpected:
                unexpected = True
        if reconcile_due:
            rec_unexpected, queued = self._reconcile_member(
                tenant, binding, now=now,
            )
            if rec_unexpected:
                unexpected = True
        if claim_net_due:
            net_unexpected, claimed = self._claim_net_member(
                tenant, binding, now=now,
            )
            if net_unexpected:
                unexpected = True
        return (
            unexpected, applied, queued, claimed,
            status_pull_due, reconcile_due, claim_net_due,
        )

    def _run_member_fastpath(self, tenant, binding, *, now, fast_seconds, round_started):
        return self._poll_member_log(
            tenant, binding, now=now, fast_seconds=fast_seconds,
            verify_identity=True, round_started=round_started,
        )

    def _heal_member_identity(self, binding) -> None:
        """⓪ 身份自愈：把绑定的同步账号显示名对齐到**登录账号**（尽力而为的旁路；业务方裁定「同步账号 = 登录的那个账号」，同 D-0170）。 ⚠️ **绝不因它打断这一轮**：`heal_binding_identity` 自己已接住上游失败与本轮节流，这里再兜一层
        broad-except——可选旁路不许打挂整轮，也**不许把本轮记成失败**。⚠️⛔ **与快路同进同出**：自愈产出服务快路的回声抑制与对账接手人比对 ⇒ 两处调用点由同一个 `skip_fastpath` 控制
        （留主进程 = 与快路进程重复自愈，多建一次连、多写一次库）。"""
        try:
            jidanpu_member.heal_binding_identity(binding)
        except Exception as exc:  # noqa: BLE001 —— 见上（自愈是尽力而为的旁路）
            self._note_member_blocked_stage(exc, "自愈", binding)
            logger.warning(
                "下家绑定同步账号身份自愈失败，已跳过（本轮照跑）：%s", type(exc).__name__
            )

    def _poll_member_log(
        self, tenant, binding, *, now, fast_seconds, verify_identity=False, round_started=None,
    ):
        """② 日志快路（按绑定自己的 `fast_poll_seconds`）；回 `(unexpected, lines, fast_due)`。 ⚠️ 没到点 ⇒ 一次上游都不打（`lines=None`）；失败**也打锚**（否则持续失败的路每拍重打上游）；
        `fast_seconds` 由调用方算好传进来（它还要写 stdout 的「未到点（间隔 N 秒）」）。 ⭐ **行级退避**：这一路**抛了** ⇒ `_note_upstream_failure`（与上家线同一张表、同一套指数阶梯、
        封顶同 300 秒），窗口内整行被调用方的行级闸跳过；这一拍干净 ⇒ 清零。记 / 清都打在**快路自己的边界**里（主一轮与独立快路单元同一份实现，两处自然同算，⛔ 不复制第二份判据）；清零只看**快路**这一拍干净（同轮 outbox / 回拉失败不拦着清——退避判据按计划就扩在这条读路上，没有上提整轮）。"""
        key = (tenant.schema_name, binding.pk)
        if round_started is None:
            fast_due = _member_due(_MEMBER_FAST_AT.get(key), fast_seconds, now=now)
        else:
            last_started = self._fastpath_starts.get(key)
            fast_due = last_started is None or round_started >= last_started + fast_seconds
        unexpected = False
        lines = None
        if fast_due:
            try:
                if verify_identity:
                    lines = jidanpu_member.poll_log(binding, now=now, verify_identity=True)
                else:
                    lines = jidanpu_member.poll_log(binding, now=now)
            except Exception as exc:  # noqa: BLE001 —— 逐路异常边界
                expected_session_failure = verify_identity and isinstance(
                    exc, (AuthError, jidanpu.UpstreamBlockedError)
                )
                unexpected = not expected_session_failure
                if expected_session_failure:
                    jidanpu_member.LAST_CALL.upstream = False
                else:
                    self._member_failure(tenant, binding, "快路", exc)
                self._note_member_blocked_stage(
                    exc, "快路", binding, outlet=jidanpu_member.LAST_CALL.outlet
                )
                # ⭐ D2（M-10）：这一发也白撞了上游 ⇒ 记行级退避（指数阶梯同上家线）。
                # ⛔ 不静默：从下一拍起这一行要被跳过，运维只看输出也得看得见原因。
                until = _note_upstream_failure(key, now=now)
                self.stderr.write(
                    f"{tenant.schema_name}: 代练员绑定 #{binding.pk} · 快路失败，"
                    f"接下来 {int((until - now).total_seconds())} 秒**整行退避**（到点自动重试）"
                )
            else:
                # ⭐ 这一拍干净 ⇒ 清零（⛔ 不留下一个过期的窗口与残留计数）。
                _clear_upstream_failure(key)
            finally:
                if round_started is None:
                    _MEMBER_FAST_AT[key] = now
                else:
                    self._fastpath_starts[key] = round_started
        return unexpected, lines, fast_due

    def _member_penalty_skip(self, tenant, binding, *, now) -> bool:
        """下家一行的**行级退避**还没过 ⇒ 整行跳过（`True` = 跳过了，调用方直接 `continue`）。 ⭐ 与上家线整行退避同一条口径：只判「到点了没」、⛔ 不清窗口（由 `_poll_member_log` 跑干净时清）；
        `last_run_at` 照样前进（退避 = 这一轮**主动没打**，不是进程死了；⛔ 只动这一列， `last_ok_at` / `last_error` 一个字不碰）；⛔ 跳过必打一行（不静默）。两个调用点
        （主一轮 / 独立快路单元）各判一次 ⇒ 两台进程各有自己的一份进程内退避状态。"""
        key = (tenant.schema_name, binding.pk)
        penalty_until = _UPSTREAM_PENALTY_UNTIL.get(key)
        if penalty_until is None or now >= penalty_until:
            return False
        remaining = (penalty_until - now).total_seconds()
        jidanpu_member._stamp_binding_run(binding, now=now)
        self.stdout.write(
            f"{tenant.schema_name}: 代练员绑定 #{binding.pk} · 连续 "
            f"{_UPSTREAM_FAILURE_COUNT.get(key, 0)} 轮失败，退避中还剩 "
            f"{remaining:.0f} 秒 ⇒ **这一行整行跳过**（快路 / outbox / 回拉 / 对账都不跑，"
            f"到点自动重试）"
        )
        return True

    def _run_member_openapi_binding(self, tenant, binding, *, now) -> bool:
        """**下家②式**（`JidanpuMemberBinding` + `mode="openapi"`）的一轮：**三段、顺序固定**（⛔ 别调换；逐段理由见外移登记的段表，顺序由 `tests/test_jidanpu_member_openapi_consume.py::WiringTests` 钉住）： ⓠ 回调消费（无间隔闸，跟每一拍跑；平时零网络请求，只有新事件才查一次权威状态、按单号去重）
        → ⓠ' 兜底对账（按本表自己的 `reconcile_seconds` 默认 180；**紧跟回调、⛔ 不把发单插进它们之间**） → ⓠ'' 出向发单（无闸）。承重口径「先把对方推来的事实收敛掉，再决定要不要往外推」（spec §5.5），与上家线段序同一形状；三段间**唯一承重依赖** = 回调 ⇒ 发单（回调补上缺的远端系统单号，发单六档每一档都要它，反过来 = 拿空号去办）；对账与发单**没有写入依赖**（照上家线同形状的选择）。
        ⚠️⛔ ①式那五段（自愈 / outbox / 快路 / 回拉 / 对账）在②式下**一段都不跑**（都要①式登录凭据或排①式的待发动作；⛔ 不留「跑了但必然失败」的段）。⚠️ 健康列复用 `_stamp_poll_health` 既有四列（⛔ 不为②式新造）：真的有结论 + 无未预期异常 + 发单没记 `failed`/`abandoned`/`paused` ⇒ 盖 `last_ok_at` 并清旧错；否则只推进 `last_run_at`（⛔ 不说假话）； `blocked_reason`（开关关着）= 按设计没跑，**不算失败**。"""
        key = (tenant.schema_name, binding.pk)
        reconcile_seconds = _member_interval(
            binding.reconcile_seconds, default=MEMBER_RECONCILE_SECONDS
        )
        reconcile_due = _member_due(_MEMBER_RECONCILE_AT.get(key), reconcile_seconds, now=now)
        unexpected = False

        # ⓠ 回调消费：**没有间隔闸**，而且排在这一轮的最前面（spec §5.5 的顺序）。
        consume = None
        try:
            consume = jidanpu_member_openapi.consume_member_callbacks(binding, now=now)
        except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
            unexpected = True
            self._note_member_blocked_stage(exc, "②式回调消费", binding)
            self._member_failure(tenant, binding, "回调消费", exc)

        # ⓠ' 兜底对账：**回调之后**（spec §8.3 第 ② 条），按本表自己的 `reconcile_seconds` 判到点。
        reconcile = None
        if reconcile_due:
            try:
                reconcile = jidanpu_member_openapi.reconcile_member_statuses(binding, now=now)
            except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                unexpected = True
                self._note_member_blocked_stage(exc, "②式兜底对账", binding)
                self._member_failure(tenant, binding, "兜底对账", exc)
            finally:
                # ⚠️ 与两条读路同一条口径：**失败也打锚**（保护上游容忍度）。
                _MEMBER_RECONCILE_AT[key] = now

        consume_alert = consume.alert if consume is not None else ""
        reconcile_alert = reconcile.alert if reconcile is not None else ""
        first_alert = next((text for text in (consume_alert, reconcile_alert) if text), "")

        # ⓠ'' 出向发单：②式**自己的**那一份分派层（`run_member_status_outbox`），**不设间隔闸** （排出去的台账要尽快变成真实动作）。⚠️ 开关默认关着（`JIDANPU_MEMBER_OPENAPI_OUTBOX`）：
        # 那一段自己提前返回（一次上游都不打、一个字节都不写）⇒ 这里照旧调它、由它说「为什么没跑」（⛔ 调度层不自己判那个开关：两处判据 = 迟早漂）。⚠️ 逐段异常边界：抛了只记这一段，
        # ⛔ 不影响同轮别的绑定与①式绑定（外层逐绑定边界兜着）。
        outbox = None
        try:
            outbox = jidanpu_member_openapi_outbox.run_member_status_outbox(binding, now=now)
        except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
            unexpected = True
            # `MemberOutboxUnexpectedFailures` 自带一份计数（它是逐事件边界跑完才抛的）
            # ⇒ 顺手报出来；别的异常没有这份计数 ⇒ 回 `None`，汇总说「未完成」。
            outbox = getattr(exc, "stats", None)
            self._note_member_blocked_stage(exc, "②式出向发单", binding)
            self._member_failure(tenant, binding, "出向发单", exc)

        # ⭐ 「这一轮**真的做了事**」只认**有结论**的那两档计数（`applied` 写了 / `skipped` 有结论），
        # ⛔ 不认「有待处理的事件」（`mine > 0`）与「真打出去了」（`queried > 0`）：那两种在
        # 「第一发就失败」时同样成立 ⇒ 会把「什么都没做成」记成健康（裁定 1 明禁的形态）。
        did_something = (
            (consume is not None and (consume.applied + consume.skipped) > 0)
            or (reconcile is not None and (reconcile.applied + reconcile.skipped) > 0)
        )
        # ⭐ 出向发单记下的失败档（`failed` / `abandoned` / `paused`）也参与「这一轮算不算成功」
        # （照①式既有口径）：⛔ 少了这一格，下面那次 `ok=True` 会把 `run_outbox` 刚写下的 `last_error` 清掉 ⇒「发单失败了」在页面上静默消失。⚠️ `blocked_reason`（开关关着）
        # **不算失败**：那是「按设计没跑」，算进来会让②式在开闸前永远盖不上 `last_ok_at`。
        outbox_failed = outbox is not None and bool(
            outbox.failed or outbox.abandoned or outbox.paused
        )
        if did_something and not unexpected and not outbox_failed:
            # 有结论 + 没未预期异常 + 发单没记失败 ⇒ 盖成功（清掉上一次的旧错）；
            # ⚠️ 顺序与上家线②式那一支相同 ⇒「要人看见的事」在下面**之后**盖
            # （否则刚清掉又被它写回来，那一格就白盖了）。
            jidanpu_member._stamp_poll_health(binding, now=now, ok=True)
        else:
            # ⚠️ 什么都没做成 / 有未预期异常 / 发单记了失败 ⇒ ⛔ 不盖成功（裁定 1），
            # 只推进 `last_run_at`：发单那一档正是**要**让 `run_outbox` 写下的 `last_error`
            # 留在页面上（plan §3 步骤 5 判据：「②式行有 pending 事件时页面看得见」）。
            jidanpu_member._stamp_binding_run(binding, now=now)
        # ⭐ 要人看见的那句话（台账另有行）：只在**这一轮没有未预期异常**时盖
        # —— 有异常时 `last_error` 该留给更严重的原因（那段异常边界已经记下它自己的话）。
        if first_alert and not unexpected:
            jidanpu_member._stamp_poll_health(binding, now=now, ok=False, reason=first_alert)

        self.stdout.write(
            f"{tenant.schema_name}: 代练员绑定 #{binding.pk}（官方API） · "
            f"{self._member_openapi_consume_text(consume)} · "
            f"{self._member_openapi_reconcile_text(reconcile, due=reconcile_due, seconds=reconcile_seconds)} · "
            f"{self._member_openapi_outbox_text(outbox)}"
            " · 快路/回拉：本行是官方API（不走①式那几段登录式读路）"
            f" · outlet={jidanpu_proxy.current_label()}"
        )
        # ⭐ 出口池：②式那一轮同样「真跑过且干净」才复位。
        def _n(value) -> int:
            """只认**真整数**：用例里的替身对象对任何属性都回一个 Mock（⛔ 别让它参与比较）。"""
            return value if isinstance(value, int) else 0

        ran_a_stage = (
            (consume is not None and (_n(consume.applied) + _n(consume.skipped)) > 0)
            or (
                reconcile is not None
                and (
                    _n(reconcile.queried) > 0
                    or (_n(reconcile.applied) + _n(reconcile.skipped)) > 0
                )
            )
            or (outbox is not None and _n(outbox.done) > 0)
        )
        if ran_a_stage and not unexpected:
            jidanpu_proxy.note_ok()
        return unexpected

    @staticmethod
    def _member_openapi_consume_text(consume) -> str:
        """②式汇总里的回调消费那一段（`None` = 这一段抛了未预期异常）。"""
        if consume is None:
            return "回调 未完成"
        return consume.text or "回调（无新事件）"

    @staticmethod
    def _member_openapi_reconcile_text(reconcile, *, due, seconds) -> str:
        """②式汇总里的兜底对账那一段（没到点也要说清「为什么什么都没干」）。"""
        if not due:
            return f"对账未到点（间隔 {seconds} 秒）"
        if reconcile is None:
            return "对账未完成"
        return reconcile.text or "对账（无在途单）"

    @staticmethod
    def _member_openapi_outbox_text(stats) -> str:
        """②式汇总里的**出向发单**那一段（`None` = 这一段抛了未预期异常）。 ⚠️ 三种「没办事」要**分得开**（⛔ 别并成一句「没跑」）：`blocked_reason`（开关关着，人话由那条线
        自己给）/ 计数（跑了但没待发动作）/ `None`（抛了）。开关关着时**照旧把那句人话打出来**——
        那是运维判「为什么这一行一个字节都没发」的唯一出口，⛔ 静默 = 把开闸这件事藏起来。"""
        if stats is None:
            return "出向发单 未完成"
        if stats.blocked_reason:
            return f"出向发单 {stats.blocked_reason}"
        return (
            f"出向发单 完成 {stats.done}/待重试 {stats.failed + stats.deferred}"
            f"/跳过 {stats.skipped}/放弃 {stats.abandoned}"
        )

    @staticmethod
    def _member_outbox_text(stats, *, skipped: bool = False) -> str:
        """人话汇总的 outbox 那一段（`None` = 这条路抛了未预期异常）。 ⭐ `skipped=True` ⇒ 说清「这一段**没跑**、为什么」，⛔ 不许落成「outbox 未完成」
        （那是「跑了但抛了」——两种状态必须分得开，照 `_member_openapi_outbox_text` 同一口径）。"""
        if skipped:
            return "outbox 跳过（--skip-outbox：由独立进程 tuantuantong-jidanpu-outbox 消费）"
        if stats is None:
            return "outbox 未完成"
        extra = ""
        if stats.capped:
            extra += f"/留到下轮 {stats.capped}"
        if stats.cooldown_until is not None:
            extra += f"/出口冷却至 {stats.cooldown_until.isoformat()}"
        if stats.suspended:
            # ⭐ D-0359 D1：launch 读回熔断挂起条数进统计行（与完成/待重试
            # 同一行），运维不翻台账也能看出「这家店有 N 条发单在熔断期排队没投」。
            # 挂起期不打逐轮日志（⛔ 刷屏），这一格就是稳态可见性。
            extra += f"/熔断挂起 {stats.suspended}"
        return (
            f"outbox 完成 {stats.done}/待重试 {stats.failed + stats.deferred}"
            f"/跳过 {stats.skipped}{extra}"
        )

    @staticmethod
    def _member_fast_text(lines, *, due, seconds, skipped: bool = False) -> str:
        """人话汇总的快路那一段（没到点也要说清「为什么什么都没干」）。 ⭐ `skipped=True` ⇒ 说清「⓪ 自愈 + ② 快路这一段没跑、为什么」，⛔ 不许落成「快路未完成」
        （那是「跑了但抛了」——照 `_member_outbox_text(skipped=True)` 同一条口径）。"""
        if skipped:
            return (
                "快路（含身份自愈）跳过（--skip-fastpath："
                "由独立进程 tuantuantong-jidanpu-fastpath 跑）"
            )
        if not due:
            return f"快路未到点（间隔 {seconds} 秒）"
        if lines is None:
            return "快路未完成"
        return f"快路 {lines} 行"

    @staticmethod
    def _member_pull_text(applied, *, due, seconds, skipped: bool = False) -> str:
        """人话汇总的**③ 状态回拉**那一段（⭐ 与对账各报各的间隔）。
        ⭐ `skipped=True` ⇒ 说清「没跑、为什么」，⛔ 不许落成「回拉未完成」（那是「跑了但抛了」）；
        真机上「回拉没跑」到底是没到点还是没跑成，全靠这两句分得开。"""
        if skipped:
            return "回拉跳过（--skip-reconcile：由独立进程跑）"
        if not due:
            return f"回拉未到点（间隔 {seconds} 秒）"
        return "回拉未完成" if applied is None else f"回拉 {applied} 条"

    @staticmethod
    def _member_reconcile_text(queued, *, due, seconds, skipped: bool = False) -> str:
        """人话汇总的**④ 补漏对账**那一段（排事件计数 + 跳过计数与人话原因）。 ⭐ `skipped=True` ⇒ 说清「没跑、为什么」，⛔ 不许落成「排事件未完成」（那是「跑了但抛了」）。
        ⚠️ 跳过那段必须是「**计数 + 人话原因**」（spec §5.4）：只报「跳过 3」看不出跳过了什么、
        更看不出「为什么」——那等于又静默了一次。"""
        if skipped:
            return (
                "对账跳过（--skip-reconcile："
                "由独立进程 tuantuantong-jidanpu-reconcile 跑）"
            )
        if not due:
            return f"对账未到点（间隔 {seconds} 秒）"
        if queued is None:
            return "排事件未完成"
        queue_text = (
            f"排事件 {queued.launch + queued.assign + queued.withdraw}"
            f"（跳过 {queued.skipped_count}"
        )
        if queued.skipped:
            queue_text += f"：{queued.skipped[0]}"
        queue_text += "）"
        return queue_text

    @staticmethod
    def _member_claim_net_text(claimed, *, due, seconds, skipped: bool = False) -> str:
        """人话汇总的**⑤ 接单兜底网**那一段（照 `_member_fast_text` 的同一族口径）。 ⭐ `skipped=True` ⇒ 说清「没跑、为什么」，⛔ 不许落成「兜底网未完成」（那是「跑了但抛了」）；
        `None`（到点但抛了）/ `N 行`（到点、处理了 N 张在途单）/ 未到点（间隔多少秒）三档分得开。"""
        if skipped:
            return (
                "兜底网跳过（--skip-reconcile："
                "由独立进程 tuantuantong-jidanpu-reconcile 跑）"
            )
        if not due:
            return f"兜底网未到点（间隔 {seconds} 秒）"
        if claimed is None:
            return "兜底网未完成"
        return f"兜底网 {claimed} 行"

    def _member_failure(self, tenant, binding, stage, exc) -> None:
        """某条路（或整行）抛未预期异常 ⇒ 响亮，但**只写异常类型名**（⛔ 不带正文）。
        ⚠️ 正文可能带上游回包 / 凭据片段（spec §8.5-S1）⇒ 照租户级那一段的同一条口径。"""
        self.stderr.write(
            f"{tenant.schema_name}: 代练员绑定 #{binding.pk} 的{stage}"
            f"出现未预期失败（{type(exc).__name__}），继续下一个"
        )

    # ── 第三层：上家鸡蛋铺本线绑定（上家线 ；spec §10）────────────────────

    def _run_upstream_bindings(self, tenant, *, now) -> bool:
        """本租户里**逐个启用中的上家线绑定**跑一轮（spec §10）；回「是否出现未预期失败」。 ⚠️ 跳过停用行（`jidanpu_upstream.enabled_bindings()` 已筛，spec §8.4）与凭据不全行，都不打印、
        也**不盖 `last_ok_at`**（跳过的行根本不进 `_run_upstream_binding`）。⭐ 凭据闸按 `mode` 分两支 ——承重（②式账号密码为空，沿用①式判据 = 整行静默跳过）；凭据刚被拒 ⇒ 整行跳过
        （见 `_credential_backoff`）。逐绑定边界：登录被拒 / 468 ⇒ 丢池里客户端（那条判据真实路径不可达——各段边界先接住；468 由段边界记账）。"""
        unexpected = False
        for binding in jidanpu_upstream.enabled_bindings():
            if self._credentials_missing(binding):
                continue
            # ⭐ **凭据刚被①档拒过 ⇒ 这一轮整行跳过**（spec §9.5 ②，T12）：⛔ 不每轮反复打必拒的
            # 取 token 接口（不挡会每 3 秒打一次，直到人把权限开回来）。在这一层挡、⛔ 不在消费 / 对账各挡一遍（跳过的是整行的几段，判据只有一处 ⇒ 两段不可能给出两种结论）；
            # 人话已在 `last_error`（上一轮写的，含对方原文），⛔ 不再盖（盖了 = 每轮刷新「上次尝试时刻」）。
            if self._credential_backoff(binding):
                continue
            try:
                if self._run_upstream_binding(tenant, binding, now=now):
                    unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐绑定异常边界（spec §10）
                # 能逃到这一层的异常**按定义**是未预期的（各段已接住自己的）； ⛔ 只写异常类型名（`str(exc)` 可能带上游回包 / 凭据片段）。
                # ⭐ 登录被拒 / 会话失效 / 468 ⇒ 丢这一行当前的客户端；⚠️ 这条判据在真实路径上不可达（各段边界先接住且不 re-raise），468 由段边界记账与丢会话；
                # `AuthError` 那一半是另一件事（只登记不在此修）。
                if isinstance(exc, (AuthError, jidanpu.UpstreamBlockedError)):
                    self._upstream_pool().invalidate(binding)
                # ⚠️ 同一次 468 只会被**最内层**边界接住 ⇒ 与段边界那几处不会重复计数。
                # ⚠️ 阶段名按**线 + 方式**取（⛔ 别让两条线的告警看起来像同一件事）。
                self._note_upstream_blocked_stage(
                    exc,
                    "②式" if jidanpu_openapi.is_openapi_binding(binding) else "①式",
                    binding,
                )
                unexpected = True
                self._upstream_failure(tenant, binding, "本轮", exc)
        return unexpected

    @staticmethod
    def _credential_backoff(binding) -> bool:
        """这一行的凭据**此刻在退避窗口**里吗（spec §9.5 ②；T12）。 ⚠️ 判据唯一出处是 `jidanpu_openapi.credential_backoff_reason`（它自己加锁、自己看窗口）：
        ⛔ 不在这里重算秒数、⛔ 不自己拼缓存键（键由 `openapi_cache_key` 给出，少一维 = 把 A 租户的结论套到 B 租户头上且静默）。①式没有这个概念（不取 token）⇒ 直接 `False`；
        这里**不建 HTTP 客户端**（只为读一个进程内标记开连接池是白给）。"""
        if not jidanpu_openapi.is_openapi_binding(binding):
            return False
        key = jidanpu_openapi.openapi_cache_key(binding)
        if key is None:  # 凭据不全 ⇒ 交给上面那道既有判据（它会给一句准确的话）
            return False
        return bool(jidanpu_openapi.credential_backoff_reason(key))

    @staticmethod
    def _credentials_missing(binding) -> bool:
        """这一行**按它自己那条路**的凭据判据是不是还不全（⛔ 不沿用另一条路的判据）。唯一出处 = ①式 `jidanpu.credentials_of` / ②式 `jidanpu_openapi.openapi_credentials_of`
        （各自还会 `strip`，覆盖历史数据 / shell 直写）。与 `api/jidanpu_upstream_views.py::_credentials_missing`
        同一口径（那里是「启用」门、这里是「同步」门）；⛔ 不 import 视图那份（管理命令不依赖 api 包）、也不在这里重写判空；判「走哪条路」只看 `mode`（⛔ 不拿 `client_id` 真值反推，E-0021 家族）。"""
        if jidanpu_openapi.is_openapi_binding(binding):
            return jidanpu_openapi.openapi_credentials_of(binding) is None
        return jidanpu.credentials_of(binding) is None

    def _run_upstream_binding(self, tenant, binding, *, now) -> bool:
        """一个上家线绑定的一轮；**跑哪几段由这一行的 `mode` 决定**（spec §2.1）： ①式 = `run_outbox` → `sync_hall` → `sync_my_orders`（⛔ 不跑 ⓪ / ⓪'）； ②式 = **⓪ 回调消费** → **⓪' 兜底对账**（⛔ 不跑 outbox / 大厅 / 我的订单：都要①式登录凭据，本行没有）。 ⚠️ 各段各有异常边界；两条读路 / 对账按绑定自己的间隔判到点、锚在进程内、失败也打锚。 ⭐ 两条读路**共用池里那一个客户端**（跨轮只登录一次；「一轮一建一关」把 cookie jar 一起丢掉
        ⇒ 每轮重登 = 对端 WAF 封 IP 的主因，spec §2.1；池建对子是**零网络**的 ⇒ 什么都没到点也一次上游都不打）。⭐ 读路段只要抛了 ⇒ 整轮退避（连 outbox 一起、⛔ 不静默），判据见 `_note_upstream_failure` / `_upstream_backoff_seconds`。 ⛔ **本行没有「操作日志快路」**（✅ 已枚举确认）：打手权限读不到对方的系统操作日志（spec `docs/specs/2026-09-28-upstream-jidanpu-sync-design.md` §2.5 **实测**：`GetSysmteLogList`
        对打手账号回**空列表 + `code=0` 静默空**），下家线那条 `poll_log` 在本行不成立（⛔ 别挪进来）；旧「大厅尾部快路」已随 D-0366 退役 ⇒ 本行没有任何快路。判据钉在 `tests/test_jidanpu_upstream_schedule.NoOperationLogFastPathTests`（行为 + 结构两维）。 ⭐ ⓪ 回调消费（spec §5.5）：**零网络**（只有新事件才查权威状态、按单号去重）⇒ 不设间隔闸；留痕出口 = 台账 `JidanpuSyncEvent` + **整轮末尾**盖 `last_error`（⛔ 不在这里盖：健康列会清掉）。 ⭐ ⓪' 兜底对账（spec §8）：低频（默认 600 秒）逐单查在途单；⛔ 不自己判「该不该写」，走与 ⓪ **同一个** `apply_authoritative_status`（spec §8.3 第 ③ 条）。⓪/⓪' 各自自建连接（T2 的官方 API 客户端）。返回这一轮**是否出现未预期失败**。"""
        key = (tenant.schema_name, binding.pk)
        # ⭐ **退避窗口内 ⇒ 整轮跳过**（spec §2.3）：在最前面判（被 WAF 挡住时连 outbox 那一发也是
        # 同一个 IP 发的，一起停才对）；判据只看「到点了没」，⛔ 不清窗口（整轮干净时由
        # `_clear_upstream_failure` 清）；⛔ 不静默：跳过必打一行。
        penalty_until = _UPSTREAM_PENALTY_UNTIL.get(key)
        if penalty_until is not None and now < penalty_until:
            remaining = (penalty_until - now).total_seconds()
            # ⭐ **`last_run_at` 照样前进**：退避是「这一轮主动没打」，不是进程死了——不让它前进，
            # 运维第一反应就是去重启一个没坏的服务。⛔ 只动这一列（`last_ok_at` / `last_error` 不碰）。
            jidanpu_upstream._stamp_binding_run(binding, now=now)
            self.stdout.write(
                f"{tenant.schema_name}: 上家绑定 #{binding.pk} · 连续 "
                f"{_UPSTREAM_FAILURE_COUNT.get(key, 0)} 轮失败，退避中还剩 "
                f"{remaining:.0f} 秒 ⇒ **这一轮整轮跳过**（连 outbox 一起，到点自动重试）"
            )
            return False
        hall_seconds = _poll_seconds(
            JidanpuUpstreamBinding, "hall_poll_seconds", binding.hall_poll_seconds
        )
        orders_seconds = _poll_seconds(
            JidanpuUpstreamBinding, "my_orders_poll_seconds", binding.my_orders_poll_seconds
        )
        reconcile_seconds = _poll_seconds(
            JidanpuUpstreamBinding, "reconcile_seconds", binding.reconcile_seconds
        )
        # ⭐ **这一行走哪条路**（spec §2.1：只看 `mode`）——它决定**哪几段该跑**： ①式 ⇒ ①outbox + ②大厅 + ③我的订单（都要①式的登录凭据）；
        # ②式 ⇒ ⛔ 那三段**一段都不跑**（本行没有账号 / 密码，跑它们只会在建连处每轮抛 `ConfigError`，把「这条路没有这一段」报成「同步坏了」），只跑 ⓪ 回调消费 + ⓪' 兜底对账。
        is_openapi = jidanpu_openapi.is_openapi_binding(binding)
        # ⚠️ ②式下那两段的闸门直接判 **False**（= 「这一段本行不适用」的机械落实）：
        # 这样连**建连那一步**（`jidanpu.open_client_with_http`）都不会进。
        hall_due = (not is_openapi) and _upstream_due(
            _UPSTREAM_HALL_AT.get(key), hall_seconds, now=now
        )
        orders_due = (not is_openapi) and _upstream_due(
            _UPSTREAM_MY_ORDERS_AT.get(key), orders_seconds, now=now
        )
        reconcile_due = _upstream_due(
            _UPSTREAM_OPENAPI_RECONCILE_AT.get(key), reconcile_seconds, now=now
        )
        unexpected = False
        #: ⭐ **这一轮的读路段有没有抛**（spec §2.3 的退避计数只看它，⛔ 不看 `unexpected`）：
        #: outbox / 大厅 / 我的订单 / 读路建连，四个点各置一次。
        read_failed = False

        # ⭐ **这一份凭据刚被拒过吗**（spec §9.5 ②，T12）：判**一次**、把结论传给下面两段——
        # 取 token 是②式每一段的第一跳，消费与兜底对账各打一次必拒接口正是 §9.5 ③ 明禁的形态；
        # 判据与取 token 那一跳共用同一个函数（⛔ 不在这里重算窗口、不自己拼键）；①式恒为空串。
        cache_key = None
        consume_blocked = ""
        if is_openapi:
            cache_key = jidanpu_openapi.openapi_cache_key(binding)
            if cache_key is not None and jidanpu_openapi.credential_backoff_reason(cache_key):
                consume_blocked = TOKEN_BLOCKED_CONSUME_REASON

        # ⓪ 回调消费（T6）：**①之前**（spec §5.5 的顺序）。失败按本线既有的逐段边界处理。
        consume = None
        try:
            consume = jidanpu_openapi_consume.consume_callbacks(
                binding, now=now, token_blocked=consume_blocked,
            )
        except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
            unexpected = True
            self._note_upstream_blocked_stage(exc, "回调消费", binding)
            self._upstream_failure(tenant, binding, "回调消费", exc)
        # ⭐ 这一段的取 token 刚被拒 ⇒ **同一轮的后一段也不再打它**（spec §9.5 ③）。
        # ⚠️ 与上面那个「退避窗口」不是一回事：窗口**只对①档**生效，而「一轮里连打两次」
        # 对③档（网络抖动 / 上游 5xx）同样是白打 —— 这一条管的是**同一轮之内**。
        reconcile_blocked = consume_blocked
        if consume is not None and consume.token_rejected and not reconcile_blocked:
            reconcile_blocked = TOKEN_BLOCKED_RECONCILE_REASON

        # ⭐ ⓪' 兜底对账（T8，spec §8）：**回调之后**（spec §8.3 第 ② 条：先回调后对账）；
        # 两条路算**同一个目标态** ⇒ 同轮先后写同一张单只会落到同一个值。 ⚠️ **有闸**（按绑定自己的 `reconcile_seconds` 默认 600 判到点）：
        # 没有它，一张在途单会被每一拍（3 秒）查一次 = 对上游的紧循环。
        reconcile = None
        if reconcile_due:
            try:
                reconcile = jidanpu_openapi_reconcile.reconcile_openapi_statuses(
                    binding, now=now, token_blocked=reconcile_blocked,
                )
            except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                unexpected = True
                self._note_upstream_blocked_stage(exc, "兜底对账", binding)
                self._upstream_failure(tenant, binding, "兜底对账", exc)
            finally:
                # ⚠️ 与两条全量读路同一条口径：**失败也打锚**（保护上游容忍度）。
                _UPSTREAM_OPENAPI_RECONCILE_AT[key] = now

        # ① outbox：**不设间隔闸**（排出去的台账要尽快变成真实动作）。⛔ **②式不跑这一段** （T8 收口裁定，写接口第一期不做 spec §15-N3）：handler 都要经①式登录客户端打写接口，
        # 跑了只有「建连就抛 + 事件永远 pending + 每 3 秒重来」。⇒ **跳过但 ⛔ 绝不静默**： `note_openapi_outbox_not_supported` 留幂等台账 + 把那句话盖进 `last_error`；
        # 顺手记 `last_run_at`（②式没有别的段写这一列，不补就永远显示「从未运行」）。
        stats = None
        openapi_outbox_note = ""
        if is_openapi:
            openapi_outbox_note = jidanpu_openapi.note_openapi_outbox_not_supported(binding)
            jidanpu_upstream._stamp_binding_run(binding, now=now)
        else:
            # ⚠️ **先给 `client = None`**：下面的 `except` 拿它记账（468 归属按建连时的出口），
            # 而 `acquire()` 自己就可能抛 ⇒ 不预置会变成 `UnboundLocalError`，
            # 把真正的异常类型与 outbox 失败明细一起吞掉（现场实测过）。
            client = None
            try:
                # ⭐ 与下面两条读路**共用池里的那一个客户端**（spec §2.1）：会话因此跨轮复用。
                client, http = self._upstream_pool().acquire(binding)
                stats = jidanpu_upstream.run_outbox(
                    binding, now=now, client=client, http=http,
                )
            except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                unexpected = True
                read_failed = True
                # `UpstreamOutboxUnexpectedFailures` 自带一份计数（它是逐事件边界跑完才抛的）
                # ⇒ 顺手报出来；别的异常没有这份计数 ⇒ 回 `None`，汇总说「未完成」。
                stats = getattr(exc, "stats", None)
                self._note_upstream_blocked_stage(exc, "outbox", binding, client)
                self._upstream_failure(tenant, binding, "outbox", exc)

        # ⭐ 打手 App 排的**手工拉图请求**：不设间隔闸（有人在页面上等着）；零请求那一拍一次上游
        # 都不打（先查一行台账）；②式不跑这一段（拉图要走①式的 `ViewPic`，端点侧也挡成
        # `triggered=false` 排不出请求）。
        pull_pics_requests = 0
        if not is_openapi and jidanpu_upstream.has_pending_pull_pics_requests(binding):
            try:
                pull_client, _pull_http = self._upstream_pool().acquire(binding)
                pull_pics_requests = jidanpu_upstream.run_pull_pics_requests(
                    binding, now=now, client=pull_client,
                )
            except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                unexpected = True
                read_failed = True
                self._note_upstream_blocked_stage(exc, "App按需拉图", binding)
                self._upstream_failure(tenant, binding, "App按需拉图", exc)
            if pull_pics_requests:
                self.stdout.write(
                    f"{tenant.schema_name}: 上家绑定 #{binding.pk} · "
                    f"App 按需拉图 {pull_pics_requests} 条"
                )

        # ② 大厅 + ③「我的订单」：两条读路共用**同一个**客户端（池里的那一个），各按自己的间隔判到点。
        hall = None
        orders = None
        if hall_due or orders_due:
            try:
                # ⚠️ 池里拿，⛔ 不再 `with jidanpu.open_client_with_http(...)`：那种「一轮一建一关」
                # 会把 cookie jar 一起丢掉 ⇒ 每轮重登一次（= 对端 WAF 封 IP 的主因，spec §2.1）。
                client, _http = self._upstream_pool().acquire(binding)
                if hall_due:
                    try:
                        hall = jidanpu_upstream.sync_hall(
                            binding=binding, client=client, now=now
                        )
                    except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                        unexpected = True
                        read_failed = True
                        self._note_upstream_blocked_stage(exc, "大厅", binding, client)
                        self._upstream_failure(tenant, binding, "大厅", exc)
                if orders_due:
                    try:
                        orders = jidanpu_upstream.sync_my_orders(
                            binding=binding, client=client, now=timezone.now(), probe_clock=timezone.now,
                        )
                    except Exception as exc:  # noqa: BLE001 —— 逐段异常边界
                        unexpected = True
                        read_failed = True
                        self._note_upstream_blocked_stage(exc, "我的订单", binding, client)
                        self._upstream_failure(tenant, binding, "我的订单", exc)
            except Exception as exc:  # noqa: BLE001 —— 建连（池建对子）本身就是一条会失败的段
                unexpected = True
                read_failed = True
                self._note_upstream_blocked_stage(exc, "读路建连", binding)
                self._upstream_failure(tenant, binding, "读路建连", exc)
            finally:
                # ⚠️ 与前两层同一条口径：失败也打锚（保护上游容忍度）。
                if hall_due:
                    _UPSTREAM_HALL_AT[key] = now
                if orders_due:
                    _UPSTREAM_MY_ORDERS_AT[key] = now
            if hall is not None and not hall.complete:
                # ⚠️ 拉不全的**人话原因**必须写进绑定 `last_error` 并打到 stdout（⛔ 不许静默：
                # 拉不全就判「消失」会误撤在挂的单）；空 `reason` 由 `_stamp_binding_error` 顶替成固定人话。
                jidanpu_upstream._stamp_binding_error(binding, reason=hall.reason, now=now)

        # ── 健康列：一轮**读路完整成功** ⇒ 盖 `last_ok_at` + 清旧错（收口裁定 1 / 2）──
        # ⚠️ 必须留在整轮末尾、拿到三段全部结论之后（先成功的段会把后失败那段刚写下的 `last_error` 清掉）。四档**缺一不可**：① 至少一条读路到点且真的打了上游（非 owner 大厅 = 一次上游没打，
        # 不算）；② 三段都没有未预期异常；③ 到点的大厅拉全（拉不全刚写过 `last_error`，再盖成功 = 把「没拉全」说成健康）；④ 这一轮没有落在绑定上的失败（outbox 记了 `failed` / `abandoned`，或回 `paused` ⇒ 都不算成功）。
        read_ran = (hall_due and hall is not None and hall.is_owner) or (
            orders_due and orders is not None and orders.read
        )
        outbox_clean = (
            stats is not None
            and not stats.paused
            and not stats.failed
            and not stats.abandoned
        )
        hall_clean = not hall_due or (hall is not None and hall.complete)
        healthy = read_ran and outbox_clean and hall_clean and not unexpected
        consume_alert = consume.alert if consume is not None else ""
        reconcile_alert = reconcile.alert if reconcile is not None else ""
        # ⭐ **「要人看见的事」这一格按方式分两支**：②式不跑读路、不跑 outbox ⇒ `healthy` 结构性为假，
        # 照①式盖 `last_error` 会让②式的话一句都到不了页面（= 静默）⇒ ②式换成「三段都没未预期异常」；
        # ①式一个字都不变（下面 `if not is_openapi` 那一支）。
        if is_openapi:
            alert_ok = not unexpected
            # ⭐ ②式的「这一轮真的做了事」判据（照裁定 1「什么都没做不许盖成功」）：只认**有结论**的两档计数（`applied` 写了 + `skipped` 有结论）——⛔ 不认 `mine > 0`（只说明有待处理事件，
            # 第一条就查不动时也成立）、`queried > 0`（只说明真打出去了，第一发就失败时同样成立）。不盖的话，一条恢复正常的②式绑定会永远挂着上一次的 `last_error`（说假话）。
            did_something = (
                (reconcile is not None and (reconcile.applied + reconcile.skipped) > 0)
                or (consume is not None and (consume.applied + consume.skipped) > 0)
            )
            if did_something and not unexpected:
                jidanpu_upstream._stamp_binding_ok(binding, now=now)
        else:
            alert_ok = healthy
            if healthy:
                jidanpu_upstream._stamp_binding_ok(binding, now=now)
        # ── ⓪ 回调消费留下的、要人看见的那句话（本线 T6）────────────────────────────
        # ⚠️ 必须留在健康列之后（`_stamp_binding_ok` 会把刚写下的 `last_error` 清掉，提前盖 = 没盖）；
        # 只在这一轮干净时盖（不干净时 `last_error` 已被更严重的原因占着，⛔ 不覆盖）；三句话只放得下一句 ⇒ 优先级固定为 回调 → 对账 → ②式待发动作；台账才是承重出口。
        first_alert = next(
            (text for text in (consume_alert, reconcile_alert, openapi_outbox_note) if text), ""
        )
        if first_alert and alert_ok:
            jidanpu_upstream._stamp_binding_error(binding, reason=first_alert, now=now)

        self.stdout.write(self._upstream_round_line(
            tenant=tenant, binding=binding, is_openapi=is_openapi, consume=consume, stats=stats,
            hall=hall, orders=orders, reconcile=reconcile,
            hall_due=hall_due, orders_due=orders_due,
            reconcile_due=reconcile_due, hall_seconds=hall_seconds,
            orders_seconds=orders_seconds, reconcile_seconds=reconcile_seconds,
            openapi_outbox_note=openapi_outbox_note,
        ))
        #: ⭐ **这一轮真的动过上游吗**（⛔ 不是「空拍」）——出口池的连续拦截计数只许被
        #: **真跑过**的干净轮清零。判据只认「到点且真的跑了」的段 + 两段零网络闸门的**实际产出**
        #: （⛔ 不认「被调用过」：大厅只认 owner；⓪/① 只认「真有结论」的计数，与健康列同口径）。
        ran_a_stage = (
            (hall_due and hall is not None and hall.is_owner)
            or (orders_due and orders is not None and orders.read)
            #: ⚠️ 对账只认**真的产生过工作**：①式下它每 `reconcile_seconds`
            #: （默认 600 秒）就回一次 `ReconcileOutcome(enabled=False)`（一次库 / 上游都不打）
            #: ——「非 None」太宽，拿它当「真跑过」等于每 10 分钟白清一次连续拦截计数。
            or (
                reconcile_due
                and reconcile is not None
                and (reconcile.queried > 0 or (reconcile.applied + reconcile.skipped) > 0)
            )
            or (consume is not None and (consume.applied + consume.skipped) > 0)
            or (stats is not None and stats.done > 0)
        )
        # ── 连续失败的退避（spec §2.3）──────────────────────────────────────────────
        # ⚠️ 放在整轮末尾、拿到全部结论之后；判据只看 `read_failed`（四个读路点）：有一处抛了 ⇒ 计数 +1、写退避窗口；一处都没抛且无未预期失败 ⇒ 清零 + 清窗口。
        # ⛔ 不许并进 `unexpected`：退避记的是「对上游白撞一发」这个事实，与健康列是两件事。
        if read_failed:
            until = _note_upstream_failure(key, now=now)
            # ⛔ 不静默：从下一轮起会整轮跳过（运维只看 stdout 也得看得见「为什么这台机器不干活」）。
            self.stderr.write(
                f"{tenant.schema_name}: 上家绑定 #{binding.pk} · 本轮有读路失败，"
                f"接下来 {int((until - now).total_seconds())} 秒**整轮退避**（到点自动重试）"
            )
        elif not unexpected:
            _clear_upstream_failure(key)
            # ⭐ **出口池（T3）**：这一轮真的动过上游且整轮干净 ⇒ 当前出口连续拦截计数清零
            # （成功一次即复位）。⛔ **空轮不许清零**：空轮只是「没到点」，不是「上游通了」——
            # 拿它清零 = 被拦一次之后永远攒不到阈值。判据见上面 `ran_a_stage`。
            if ran_a_stage:
                jidanpu_proxy.note_ok()
        return unexpected

    def _upstream_round_line(self, *, tenant, binding, is_openapi, consume, stats, hall,
                             orders, reconcile, hall_due, orders_due,
                             reconcile_due, hall_seconds, orders_seconds, reconcile_seconds,
                             openapi_outbox_note) -> str:
        """一轮的人话汇总（**按方式分两支**：⛔ 不打印本行根本不跑的那几段）。
        ⚠️ ①式下打印「对账未到点」、②式下打印「大厅未到点」都是**假话**（那一段根本不存在）；
        ①式那一支的输出**逐字保持原样**（⛔ 连「对账」字样都不加）。"""
        if is_openapi:
            reconcile_text = self._upstream_reconcile_text(
                reconcile, due=reconcile_due, seconds=reconcile_seconds
            )
            outbox_text = "outbox：第一期不做写接口（本行跳过）"
            if openapi_outbox_note:
                outbox_text += "·有发不出去的待发动作（见 last_error）"
            detail = (
                f"{self._upstream_consume_text(consume)}{reconcile_text} · {outbox_text} · "
                "大厅与我的订单：本行是官方API（不走登录式那两条读路）"
            )
        else:
            detail = (
                f"{self._upstream_consume_text(consume)}"
                f"{self._upstream_outbox_text(stats)} · "
                f"{self._upstream_hall_text(hall, due=hall_due, seconds=hall_seconds)} · "
                f"{self._upstream_orders_text(orders, due=orders_due, seconds=orders_seconds)}"
            )
        return (
            f"{tenant.schema_name}: 上家绑定 #{binding.pk} · {detail}"
            # ⭐ **出口标签**：运维在 journald 里就能看出这一轮走的哪个出口
            # （`<下标>:<host>`，⛔ 不含账号 / 密码 / 查询串）。
            f" · outlet={jidanpu_proxy.current_label()}"
        )

    @staticmethod
    def _upstream_consume_text(consume) -> str:
        """人话汇总的**回调消费**那一段（本线 T6）。 ⚠️ `None` = 这一段抛了未预期异常；这一段**必须有话**：它是「回调到底有没有人在消费」
        在终端上的唯一读数（⛔ 空串会让人以为这一段没接上）。"""
        if consume is None:
            return "回调 未完成 · "
        text = consume.text
        return f"{text} · " if text else ""

    @staticmethod
    def _upstream_outbox_text(stats) -> str:
        """人话汇总的 outbox 那一段（`None` = 这一段抛了未预期异常）。
        ⚠️ `abandoned`（烧完额度、不再自动重试）**必须报出来**：它是「要人工核对」的那一档，
        ⛔ 混进「待重试」会让运维以为还会自己好（spec §10 的失败退避口径）。"""
        if stats is None:
            return "outbox 未完成"
        text = (
            f"outbox 完成 {stats.done}/待重试 {stats.failed + stats.deferred}"
            f"/跳过 {stats.skipped}"
        )
        if stats.abandoned:
            text += f"/已放弃 {stats.abandoned}"
        return text

    @staticmethod
    def _upstream_hall_text(hall, *, due, seconds) -> str:
        """人话汇总的大厅那一段：镜像 N + **拉不全的人话原因**（⛔ 不许静默）。 ⚠️ 空 `hall.reason` 与 `last_error` **同一句兜底**（`ROUND_UNKNOWN_FAILURE_REASON`）：
        `complete=False` 而 `reason` 为空 ⛔ 不许在终端上打成「（不完整：）」——那是把人话留痕变成空括号。"""
        if not due:
            return f"大厅未到点（间隔 {seconds} 秒）"
        if hall is None:
            return "大厅未完成"
        text = (
            f"大厅镜像 {hall.listed}/拉取 {hall.pulled}"
            f"/消失 {hall.gone}/跳过 {hall.skipped}"
        )
        if not hall.complete:
            text += (
                "（不完整："
                f"{hall.reason or jidanpu_upstream.ROUND_UNKNOWN_FAILURE_REASON}）"
            )
        return text

    @staticmethod
    def _upstream_orders_text(orders, *, due, seconds) -> str:
        """人话汇总的「我的订单」那一段（记录 M / 跳过 K / 跟退 J）。"""
        if not due:
            return f"我的订单未到点（间隔 {seconds} 秒）"
        if orders is None:
            return "我的订单未完成"
        if not orders.read:
            return "我的订单未读取"
        summary = f"记录 {orders.recorded}/跳过 {orders.skipped}/跟退 {orders.followed}"
        return f"{summary} · 指派单下轮续拉" if orders.backfill_pending else summary

    @staticmethod
    def _upstream_reconcile_text(reconcile, *, due, seconds) -> str:
        """人话汇总的**兜底对账**那一段（本线 T8，spec §8）。 ⚠️ `None` + 到点 = 这一段抛了未预期异常；空串 = 本行不是②式 ⇒ 那一段整个不打
        （照 `_upstream_consume_text` 同一形状：⛔ 不给①式绑定打噪音）。"""
        if not due:
            return f"对账未到点（间隔 {seconds} 秒）"
        if reconcile is None:
            return "对账未完成"
        return reconcile.text

    def _upstream_failure(self, tenant, binding, stage, exc) -> None:
        """本线某一段（或整行）抛未预期异常 ⇒ 响亮，但**只写异常类型名**（⛔ 不带正文）。"""
        self.stderr.write(
            f"{tenant.schema_name}: 上家绑定 #{binding.pk} 的{stage}"
            f"出现未预期失败（{type(exc).__name__}），继续下一个"
        )
