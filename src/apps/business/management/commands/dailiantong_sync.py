"""`dailiantong_sync`：代练通状态同步（**回调为主 + 1 小时兜底轮询**，T9c / h13 2026-10-28）。 python manage.py dailiantong_sync --once                 # 跑一轮（排障 / 手工；含兜底轮询） python manage.py dailiantong_sync --loop                 # 常驻（生产形态：回调即时 + 每 1 小时兜底） python manage.py dailiantong_sync --loop --interval 30   # 临时把兜底轮询打快（排障；秒） python manage.py dailiantong_sync --tenant <schema>      # 只跑一个租户（排障） ## ⭐（业务方）：**回调为主，轮询改 1 小时兜底** 状态改动**以回调为主**：回调接收器把事件落盘后，本命令在 `CALLBACK_WATCH_SECONDS` （2 秒）的 tick 里 stat 事件文件，一有增长就**立刻**消费并回读（这一轮 `poll_upstream=False`：只消费回调 + 跑定时任务段，**不**扫其它推送行）。 `--interval` 只管**兜底轮询**：每满 `LOOP_INTERVAL_SECONDS`（默认 **3600** 秒）才把全部 `sent` / `unknown` 行扫一遍。⚠️ 回调覆盖率见本文件末尾「回调覆盖率」一节。 ## 每轮做这几件事（顺序固定） 1. **消费回调队列**：接收器（`deploy/callback/dailiantong_receiver.py`）把验签通过的原始报文追加到 `shared/dailiantong-callback.jsonl`；本命令按 `SerialNo` 找 `DailiantongPush`，再调 `refresh_push_status()` 回读远端详情。 2. **兜底轮询**（**只在满 1 小时的那一轮做**）：所有 `state in (sent, unknown)` 的推送行，逐条 `refresh_push_status()`。 ⭐ 第 1 / 2 段都经 `refresh_push_status` ⇒ 它同时把**远端字段**（接单人 UID·昵称、接单时间、撤销原因）与**本地订单状态**（计划 Rev.9 §2 的映射表）写回；回调只是闹钟，状态仍以回读为准（见下一节）。 ⭐ **H3-2**：远端 `Status=17`（已结算）时，除了把本地订单写成 `COMPLETED`，还把本单**打单绩效**置为已结账（`settle_status=已结账` + `settled_at`，实现见 `dailiantong_push.settle_performances_on_remote_settled`）。⚠️ **客服提成不动**。 2.5 **并发抢单收口**（A4，计划 Rev.9 §4 第 2 条）：**内部打手先接**的推送行（本地 MAIN 行有真实接手人、远端还没被接）⇒ 及时把代练通订单撤掉（未接手删除 / 已接手申请协商撤销；实现见 `dailiantong_push.reconcile_internal_claims`，本命令只负责每轮触发一次）。反向那一档（**两边同时接**）在状态回写里收口：远端已接手 ⇒ 本地接手人改成占位账号
「代练通」、原打手的七列绩效回退。 2.6 **协商撤销满 24 小时 ⇒ 自动申请客服介入**：本轮回读之后， `remote_cancel_status=11`（协商撤销中）、`auto_arbitrate=True` 且 `cancel_requested_at + 24h <= now` 的行调一次 `LevelOrderRequestArbitration` （实现见 `dailiantong_extra_actions.auto_arbitrate_pending_pushes`）。 - 满 1 小时之前⛔ 不调（上游会拒，白打）；`cancel_requested_at + 24h` 之后才自动进入仲裁。 - ⚠️ **只在客服勾选了「自动申请仲裁」（`auto_arbitrate=True`）时才动手**（业务方改准，开关**默认关**）：不勾选时**代练通侧 24 小时自己会转客服介入**，我们再去申请就是重复申请 ⇒ 不勾选的行本段一律不碰。 - 成功 / 无需申请 ⇒ 记 `auto_arbitrated_at`，**只做一次**； - 失败 ⇒ 每 6 小时退避重试，最多 5 次后停手并把可读原因留在 `auto_arbitrate_note`。 2.7 **撞单 0 撤**（H3-1）：本轮回读之后，**同一订单下 ≥ 2 条**已接手的推送（`remote_status in (12, 13)`）⇒ 保留**最早接单**那条（判据 = `remote_accepted_at`，缺失退化到推送创建时间），其余各条调 `LevelOrderCancel(Flag=0, PayLevelBal=0, RepEnsureBal=0, Comment=系统自动：撞单保留最早接单，其余 0 撤)` （实现见 `dailiantong_push.zero_refund_conflicting_pushes`；写前回读 + 写后回读核验）。 ⚠️ 今天**只有代练通**能撤：其它平台的推送（撞单的另一半）只写可读原因、零上游调用（它们的适配器还没接入撤销能力）。 - ⚠️ **总开关是 `PlatformPushSettings.auto_zero_refund_on_conflict`：关 ⇒ 这一段零上游调用**； - 同一条推送**只发起一次**（`conflict_zero_refund_at`），失败每 6 小时退避、最多 5 次。 3. **接手后发送**（T12，2026-10-27 由 B1 合并成唯一入口）：本轮回读/回调之后， `remote_status in (12, 13)` 且 `qr_sent_at` 为空的行调一次 `dailiantong_qr.send_accept_qr`（一次接手最多一条话术 + 一张图、共用一个已发标记；每轮每行至多一次，成败判据是「进度回读里出现该对象名」+「留言回读里出现系统标记」）。 ⚠️ 第 6 条开关（`auto_send_group_qr_after_accept`）**只管图**：关着也跑这一段，规则话术照发、图不发（业务方口径）。 4. **申请验收后自动回复**（T14，2026-10-27 收窄）：本轮回读之后，`is_active` 且 `remote_status=13` 的推送行逐条调 `dailiantong_auto_reply.dispatch_for_push`
（`13` ⇒「申请验收后」；`12`「接单后」已并入第 3 段）。同一条推送对同一条规则只发一次，台账是 `DailiantongAutoReplySent` 的 `(push, rule)` 唯一约束。 ⚠️ **3 / 4 两段互不阻塞**：接手后发送抛异常不影响自动回复，反之亦然（两段各自有异常边界；第 2.5 段同样独立）。 ## ⛔ 回调只当闹钟，**状态一律回读** 回调报文里的 `Status` / `CancelStatus` **一个字都不许直接落库**： - 回调可能是重复的、迟到的、甚至乱序的（厂商文档没承诺重试 / 去重 / 顺序）； - 实测回调只覆盖「状态真的变了」的动作（`docs/DAILIANTONG-API.md` §6.2）； - 同一个字段的真相在 `LevelOrderDetail`，回调只是「现在值得去看一眼」的提醒。 ⇒ 本命令从回调里只取 `SerialNo`（用于定位推送行），`Status` 一律由 `refresh_push_status()` 重新回读后写入。⚠️ 后续维护者**不要**为了省一次请求，把回调里的 `Status` 直接 `push.remote_status = …` —— 那正是本任务点名要避免的形态。 ## 为什么还要轮询（回调不稳） - 厂商文档**没有承诺**回调失败会重试（§7 / §9 R4、R11）； - 补时 / 改密码 / 补款 / 发进度**根本不推回调**（§6.2 第 1 条）； - 回调来源 IP 是多个（§6.2 第 3 条）⇒ 不能做 IP 白名单，只能验签。 ⇒ 回调与轮询是**双路**：回调让状态变化尽快被看到，轮询兜底「回调丢了 / 没推」。 ## ⭐ 回调覆盖率（决定 1 小时兜底够不够用）回调（`OperationStatus`，`docs/DAILIANTONG-API.md` §7）**覆盖**： `接手订单` / `申请撤销` / `取消撤销` / `同意撤销` / `申请完单` / `确认完单` / `取消完单` / `订单结算` / `订单锁定` / `订单解锁` / `订单删除` / `留言` / `截图` / `提交异常` / `取消异常` / `申请客服介入` / `客服强制撤销`。 **不覆盖**（实测「根本不推」）：`补时` / `补款` / `改密码` / `发进度`。这四件事**我们发起**（客服在页面上点）或**不关心**（打手侧的进度文案不影响状态）， ⇒ 它们不需要靠轮询发现；页面自己的动作会当场回读 / 写本地行。 ⇒ **1 小时兜底够用**：所有会改变「交易状态」的动作都在回调覆盖里；兜底只负责「回调漏推 / 接收器停机」期间把状态补回来。⚠️ 回调地址要人工登记（§7），登记前所有变化只能靠这 1 小时兜底。 ## 回调即时唤醒（h13） `--loop` 不按 `interval` 一口气睡满：它每 `CALLBACK_WATCH_SECONDS`（2 秒）stat 一次回调事件文件（**不查业务表、不打上游**；长度没变时还会比对各租户偏移、只读一个 64 KB 窗口，仅用于把「一轮 500 条上限」的剩余积压排空）。文件一增长就立刻跑一轮 `_run_once(..., poll_upstream=False)`：只消费回调 + 跑 2.5–4 段（定时任务与接手后发送）， ⛔ **不扫**其它 `sent` / `unknown` 行。满 `interval` 的那一轮才是完整轮（含兜底轮询）。 ⚠️ 事件文件与同步进程必须在**同一台机器 / 同一份共享部署目录**上（接收器写 `$R/shared/dailiantong-callback.jsonl`）；分机部署时回调事件本机读不到，兜底轮询就是唯一状态来源（如实登记，见 `docs/DAILIANTONG-API.md` §7.2 的跨机锁缺口）。 ## ⭐ 多平台分派（dl-F）本命令的每一条推送行都**按它自己的 `platform`** 取适配器（`platform_adapters.get_adapter`）再调回读 / 接手后发送 / 自动回复： - 已接入平台（今天 = 代练通）⇒ 转调既有实现，行为与改前逐字一致； - **未接入平台** ⇒ 该行**跳过**（不计失败、不打上游）；轮询段只在汇总行报「未接入平台跳过 N」，两段发送把可读原因写进 `qr_last_error` / `auto_reply_last_error`； - 一个平台失败不影响别的平台的行（逐行/逐段各自的异常边界不变）。 ## 退出码契约（照 `jidanpu_sync` / `peer_sync`） - `--once`：**单条回读失败是预期内噪音**（对端超时 / 业务错误 / 某张单被删），只计入汇总行的「失败 N」并写日志，**不**让退出码非零； - 只有**租户级**的未预期失败（数据库错、读文件权限错之外的编程错误等）才 `CommandError(returncode=1)` ⇒ 手工跑这一趟时非零就是「我们这边坏了」； - `--loop` 崩了由 systemd `Restart=always` 拉起；常驻形态的告警看 `last_synced_at`。 ## 部署（`AGENTS.md` §5-A）生产形态 = systemd 单元 `deploy/systemd/tuantuantong-dailiantong-sync.service` （`--loop`），随发版安装 / 重启。⛔ **不要**再叠 `--once` 的 cron（会与常驻真并发）。 ## 边界 - 本命令是**管理命令**而非 HTTP 端点：`--tenant` 是运维排障闸门， ⛔ 不接受任何请求来源的租户标识（`AGENTS.md` §4）。 - 回调文件与偏移文件在 `shared/`（**不进 git**）。接收器只写事件文件；本命令只写**自己的偏移文件**（每个租户一份，后缀是 schema）。 - ⛔ 本命令不读回调密钥：验签在接收器里已经做完；这里拿到的都是「已验签事件」。"""

from __future__ import annotations

import fcntl
import json
import fcntl
import logging
import os
import signal
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business.dailiantong_auto_reply import TRIGGER_BY_REMOTE_STATUS
from apps.business.dailiantong_extra_actions import auto_arbitrate_pending_pushes
from apps.business.dailiantong_progress_sync import (
    sync_active_progress_screenshots,
)
from apps.business.dailiantong_push import (
    LAST_ERROR_MAX_LENGTH,
    MAX_SYNC_FAILURES,
    SYNC_BACKOFF_MINUTES,
    _clip_error,
    reclaim_stale_pending_pushes,
    zero_refund_conflicting_pushes,
)
from apps.business.dailiantong_qr import ACCEPT_STAGE_REMOTE_STATUSES
from apps.business.models import DailiantongPush
from apps.business.process_lock import ProcessLock
from apps.business.platform_adapters import (
    PlatformNotConnected,
    adapter_or_none,
    get_adapter,
)
from apps.platform.models import Tenant

logger = logging.getLogger(__name__)

#: `--loop` 的**兜底轮询**间隔（秒）。⭐（业务方）：回调为主 ⇒ 轮询改
#: **1 小时一次兜底**（原 30 秒）；回调自身由下面的 `CALLBACK_WATCH_SECONDS` 即时唤醒。
#: 做成参数是为了排障时临时打快一点（`--loop --interval 30`），而**不改单元文件**。
LOOP_INTERVAL_SECONDS = 3600

#: `--loop` 检查**回调事件文件**的间隔（秒）。回调是主路径：事件一落盘就要尽快消费并回读。
#: ⚠️ 这个 tick 只 `stat()` 一次文件：不查业务表、不打上游 ⇒ 2 秒一次的成本可忽略。
#: 与 `LOOP_INTERVAL_SECONDS` 是两件事：这个管「回调多快被看到」，那个管「多久兜底扫一遍」。
CALLBACK_WATCH_SECONDS = 2.0

#: 停止位（与 `peer_sync` / `jidanpu_sync` 同形）：`SIGTERM` / `SIGINT` 只置位，
#: 当轮跑完再退；睡眠用 `Event.wait`，信号一到立刻醒（不陪睡满一个 tick）。
_STOP = threading.Event()

#: 回调事件文件所在部署根。生产 = `/home/ubuntu/projects/tuantuantong`；
#: 测试 / 排障用 `DAILIANTONG_CALLBACK_ROOT` 指到临时目录。
DEFAULT_CALLBACK_ROOT = "/home/ubuntu/projects/tuantuantong"
ENV_CALLBACK_ROOT = "DAILIANTONG_CALLBACK_ROOT"

#: 事件文件与偏移文件的**环境变量覆盖**（测试用；生产不需要设）。
#: ⚠️ 接收器（`deploy/callback/dailiantong_receiver.py`）也认同一个 `…_EVENTS_PATH` 名字，
#: 两处口径必须一致，否则「接收器写的」与「命令读的」不是同一个文件（最典型的静默停摆）。
ENV_CALLBACK_EVENTS_PATH = "DAILIANTONG_CALLBACK_EVENTS_PATH"
ENV_CALLBACK_OFFSET_PATH = "DAILIANTONG_CALLBACK_OFFSET_PATH"

#: 默认相对路径。⚠️ 文件名是接收器与消费侧的**共同契约**。
CALLBACK_EVENTS_RELATIVE = ("shared", "dailiantong-callback.jsonl")
CALLBACK_OFFSET_RELATIVE = ("shared", "dailiantong-callback.offset")

#: 回调事件文件的**轮转阈值**（字节）。默认 64 MiB（与鸡蛋铺那条线的 `ROTATE_BYTES`
#: 同值）：接收器只追加、永不删，磁盘会被无限放大（H-1 的同一课）。
#: 可用 `DAILIANTONG_CALLBACK_ROTATE_BYTES` 覆盖（测试用小值；非法值回落默认）。
ENV_ROTATE_BYTES = "DAILIANTONG_CALLBACK_ROTATE_BYTES"
ROTATE_BYTES = 64 * 1024 * 1024

#: 归档段的**保留期**（秒）。默认 24 小时（业务方定的同一口径）。
#: 可用 `DAILIANTONG_CALLBACK_RETENTION_SECONDS` 覆盖。
ENV_RETENTION_SECONDS = "DAILIANTONG_CALLBACK_RETENTION_SECONDS"
RETENTION_SECONDS = 24 * 3600

#: 与接收器共用的**锁文件后缀**：轮转与 append 必须在同一把锁下（否则可能把一条
#: 正在写的记录劈成两半，或让接收器继续往已改名的旧文件里写）。
CALLBACK_LOCK_SUFFIX = ".lock"

#: 每轮每个租户最多消费多少条回调事件（防止积压时一轮跑成分钟级）。
#: 到达上限时偏移只推进到**最后一条完整行**，剩下的下一轮继续。
MAX_CALLBACK_EVENTS_PER_ROUND = 500

#: ⭐ 进程级锁（审计修复 P1-3）：同一台机器只许一个 `dailiantong_sync`。
#: 两个进程 / 两台机（共享部署目录）并发跑会在同一个租户里重复发群码 / 自动回复 ⇒
#: 第二个进程拿到锁失败就**直接退出并打印明确原因**。
ENV_SYNC_LOCK_PATH = "DAILIANTONG_SYNC_LOCK_PATH"
LOCK_RELATIVE = ("shared", "dailiantong-sync.lock")


# ── 按行分派的两个测试替换点（⭐ dl-F）──────────────────────────
# 名字与既有测试的 patch 目标逐字一致（`apps.business.management.commands.dailiantong_sync. send_accept_qr` / `...dispatch_for_push`，见 `tests/test_dailiantong_qr.py` 与
# `tests/test_dailiantong_auto_reply_engine.py`）。本命令仍然调这两个**模块级名字**，桩才接得上（`AGENTS.md` §5-E：`mock.patch` 打在「被查找的那个模块」上）。它们内部按 `push.platform` 取适配器：代练通 ⇒ 转调既有引擎；未接入平台 ⇒ 跳过 + 可读原因。


def _note_unconnected_platform(push, field: str, action_label: str) -> None:
    """未接入平台的行：跳过，并把可读原因写进该段自己的错误列（同值不重复写）。 ⭐ dl-F（多平台骨架）：这不是「发送失败」，所以：
    - ⛔ 不改 `state` / `qr_sent_at` / 自动回复台账 ———— 接入该平台后下一轮还会照常尝试；
    - 只写 `qr_last_error` / `auto_reply_last_error`（页面 tooltip 与人工排查靠它），且**只在原因变化时写**（否则每轮刷一次同文案）。"""
    reason = _clip_error(
        f"该交易平台尚未接入（平台 id={push.platform}），已跳过{action_label}"
    )
    if getattr(push, field, "") == reason:
        return
    setattr(push, field, reason)
    push.save(update_fields=[field, "updated_at"])
    logger.warning(
        "dailiantong_sync: 推送行平台未接入，跳过%s push=%s platform=%s",
        action_label,
        push.pk,
        push.platform,
    )


def send_accept_qr(push):
    """接手后发送的**按行分派**入口（返回 `QrSendOutcome`；未接入平台返回 `None`）。"""
    adapter = adapter_or_none(push.platform)
    if adapter is None or not adapter.supports_accept_send:
        _note_unconnected_platform(push, "qr_last_error", "接手后发送")
        return None
    return adapter.send_accept_qr(push)


def dispatch_for_push(push):
    """自动回复的**按行分派**入口（返回 `AutoReplyOutcome`；未接入平台返回 `None`）。"""
    adapter = adapter_or_none(push.platform)
    if adapter is None or not adapter.supports_auto_reply:
        _note_unconnected_platform(push, "auto_reply_last_error", "自动回复")
        return None
    return adapter.dispatch_for_push(push)


def sync_lock_path() -> Path:
    """进程锁文件路径（默认在部署根 `shared/` 下；测试可用环境变量指到临时目录）。"""
    override = os.environ.get(ENV_SYNC_LOCK_PATH)
    if override:
        return Path(override)
    return _callback_root().joinpath(*LOCK_RELATIVE)


#: 进程锁实现搬去 `apps.business.process_lock`（M7）：手工补数据命令
#: `dailiantong_sync_progress` 也要用同一份实现（⛔ 不在两个命令里各写一份 flock 代码）。
_ProcessLock = ProcessLock



def _request_stop(signum, _frame) -> None:
    """信号处理器：只置位（信号上下文不可重入，⛔ 不做任何重活）。"""
    _STOP.set()


def _callback_root() -> Path:
    return Path(os.environ.get(ENV_CALLBACK_ROOT) or DEFAULT_CALLBACK_ROOT)


def callback_events_path() -> Path:
    """回调事件文件（一行一条 JSON，接收器追加写）。"""
    override = os.environ.get(ENV_CALLBACK_EVENTS_PATH)
    if override:
        return Path(override)
    return _callback_root().joinpath(*CALLBACK_EVENTS_RELATIVE)


def callback_offset_path(schema: str) -> Path:
    """某个租户的回调消费偏移文件。 ⚠️ **每个租户一份**：事件文件是**机器级**的（接收器不认识租户），而本命令按租户
    起数据库上下文 ⇒ 不能共用一份偏移，否则先跑的租户会把别的租户的事件也「消费掉」。
    每个租户各读一遍文件、只认自己能找到的 `SerialNo`，代价是重复读几行文本。"""
    override = os.environ.get(ENV_CALLBACK_OFFSET_PATH)
    base = Path(override) if override else _callback_root().joinpath(*CALLBACK_OFFSET_RELATIVE)
    return base.with_name(f"{base.name}.{schema}")


def callback_lock_path() -> Path:
    """接收器与同步命令**共用**的回调事件文件锁（轮转 / append 的临界区）。"""
    path = callback_events_path()
    return path.with_name(path.name + CALLBACK_LOCK_SUFFIX)


def _callback_events_size() -> int | None:
    """回调事件文件当前长度（字节）；文件还不存在 / stat 失败 ⇒ `None`。 ⭐ h13：`--loop` 用它在 `CALLBACK_WATCH_SECONDS` 的 tick 里判断
    「事件文件有没有增长」⇒ 有增长就立刻跑一轮**只消费回调**的轮。⛔ 只 stat，不读内容、不查库、不打上游（回调消费本身在 `_consume_callbacks` 里做）。"""
    try:
        return callback_events_path().stat().st_size
    except OSError:
        # 文件还没被创建（接收器没启用 / 还没有任何回调）⇒ 当作「没变化」。
        return None


#: 判断「还有没有未消费的**完整行**」时最多读多少字节（回调一行通常 < 1 KB）。
#: 只读一个窗口就够：有完整行必然在窗口里出现换行；窗口外还有内容时，下一 tick 还能再判。
CALLBACK_BACKLOG_PROBE_BYTES = 64 * 1024


def _callbacks_pending(size: int | None = None) -> bool:
    """回调事件文件里是否还有**完整但尚未消费**的行（`_loop` 的排空判据）。为什么需要它：`MAX_CALLBACK_EVENTS_PER_ROUND`（500）限制一轮最多消费 500 条。若某轮之后文件**长度没再增长**、但仍有剩余完整行，只看「长度变了」就不会再唤醒
    ⇒ 剩下的要等 1 小时兜底（积压场景）。本函数补上这一档： - 取**现役租户**偏移的**最小值**（= 最落后的那个还没读到哪；⭐ M6：失效租户的遗留偏移文件被忽略，否则 `min` 永远停在一个不再推进的值上 ⇒ 每 tick 空转）；
    - 从那里读一个窗口，只要窗口里有换行 ⇒ 至少有一条完整行待消费 ⇒ 再跑一轮； - 只剩半行（接收器正在写）⇒ `False`，等文件增长（下一个 tick 自然看到）。 ⚠️ 只读**一个 64 KB 窗口**：正常回调行 < 1 KB，够用；⛔ 不把整个文件读进内存。 ⚠️ 还没有任何偏移文件（进程第一次跑）⇒ `False`：第一次轮询由完整轮负责，不走这条。"""
    path = callback_events_path()
    if size is None:
        size = _callback_events_size()
    if not size:
        return False
    offsets = [_read_offset(offset_path) for offset_path in _offset_files()]
    if not offsets:
        return False
    start = min(offsets)
    if start >= size:
        return False
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            window = stream.read(min(size - start, CALLBACK_BACKLOG_PROBE_BYTES))
    except OSError:
        return False
    return b"\n" in window


def _rotate_bytes() -> int:
    """轮转阈值（字节）：环境变量优先；非法值回落 `ROTATE_BYTES` 并记 warning。"""
    raw = (os.environ.get(ENV_ROTATE_BYTES) or "").strip()
    if not raw:
        return ROTATE_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s 不是整数（%r）⇒ 回落 %s", ENV_ROTATE_BYTES, raw, ROTATE_BYTES)
        return ROTATE_BYTES
    if value <= 0:
        logger.warning("%s 必须 > 0（%r）⇒ 回落 %s", ENV_ROTATE_BYTES, raw, ROTATE_BYTES)
        return ROTATE_BYTES
    return value


def _retention_seconds() -> int:
    """归档段保留期（秒）：环境变量优先；非法值回落 `RETENTION_SECONDS`。"""
    raw = (os.environ.get(ENV_RETENTION_SECONDS) or "").strip()
    if not raw:
        return RETENTION_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s 不是整数（%r）⇒ 回落 %s", ENV_RETENTION_SECONDS, raw, RETENTION_SECONDS
        )
        return RETENTION_SECONDS
    if value <= 0:
        logger.warning(
            "%s 必须 > 0（%r）⇒ 回落 %s", ENV_RETENTION_SECONDS, raw, RETENTION_SECONDS
        )
        return RETENTION_SECONDS
    return value


def _live_tenant_schemas() -> set[str] | None:
    """平台面 `Tenant` 表里**现役**的 schema 名集合；查不到（库异常）⇒ `None`。 ⭐ M6：回调偏移文件是**按 schema 命名**的（`<base>.<schema>`），而租户会下线 / 改名。遗留的偏移文件会让： - `_callbacks_pending()` 的 `min(offsets)` 永远停在那个**再也不会推进**的值上 ⇒
    每 2 秒 tick 都判「还有积压」⇒ 空转一整轮维护段（DB churn）； - `rotate_callback_events()` 的 `min(offsets) >= size` 永远为假 ⇒ 64 MiB 轮转阈值永久失效、现役文件无限增长。 ⇒ 所有「按偏移文件判全局」的地方都只认**现役租户**的偏移（`_offset_files()` 过滤），
    并在维护段把失效租户的偏移文件**物理删掉**（`_prune_stale_offset_files()`）。 ⚠️ 用 `schema_context(public)` 显式切公共 schema：本函数可能在某个租户上下文里被调（直接调用 `rotate_callback_events()` 的测试 / 排障会在）。 ⚠️ 查不到（连接问题 / 表不存在）⇒ **不筛**（`None` = 退回旧行为）：过滤是可靠性修复， ⛔ 不能因为一次 DB 抖动把正常消费也停掉。"""
    from django_tenants.utils import schema_context

    try:
        with schema_context(get_public_schema_name()):
            return set(
                Tenant.objects.exclude(schema_name=get_public_schema_name())
                .values_list("schema_name", flat=True)
            )
    except Exception as exc:  # noqa: BLE001 —— 过滤是旁路，查不到就退回旧行为
        logger.warning("读租户表失败（%s）：本轮不按租户过滤回调偏移文件", type(exc).__name__)
        return None


def _offset_files(schemas: set[str] | None = None) -> list[Path]:
    """**现役租户**的偏移文件（`dailiantong-callback.offset.<schema>`）。
    ⭐ M6：失效租户遗留的偏移文件**不出现在这里**（见 `_live_tenant_schemas`）；
    `schemas` 可由调用方传入（同一轮里复用一次查询结果），不传 ⇒ 现查一次。"""
    override = os.environ.get(ENV_CALLBACK_OFFSET_PATH)
    base = Path(override) if override else _callback_root().joinpath(*CALLBACK_OFFSET_RELATIVE)
    live = _live_tenant_schemas() if schemas is None else schemas
    prefix = base.name + "."
    result: list[Path] = []
    try:
        for path in base.parent.glob(base.name + ".*"):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            schema = path.name[len(prefix):] if path.name.startswith(prefix) else ""
            if live is not None and schema not in live:
                # 失效租户（已下线 / 改名）的遗留偏移：忽略（物理清理见 prune）。
                continue
            result.append(path)
    except OSError:  # pragma: no cover - 目录不可读
        return []
    return sorted(result)


def _prune_stale_offset_files(schemas: set[str] | None = None) -> list[Path]:
    """删掉**失效租户**的偏移文件，返回删掉的路径（M6：清，而不是只忽略）。
    ⚠️ 只在完整轮（全租户）的维护段调用：`--tenant` 排障跑法拿不到完整租户表时
    ⛔ 不要凭一个子集去删别人的偏移。"""
    override = os.environ.get(ENV_CALLBACK_OFFSET_PATH)
    base = Path(override) if override else _callback_root().joinpath(*CALLBACK_OFFSET_RELATIVE)
    live = _live_tenant_schemas() if schemas is None else schemas
    if live is None:
        return []
    prefix = base.name + "."
    removed: list[Path] = []
    try:
        candidates = list(base.parent.glob(base.name + ".*"))
    except OSError:  # pragma: no cover - 目录不可读
        return []
    for path in candidates:
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        if not path.name.startswith(prefix):
            continue
        schema = path.name[len(prefix):]
        if schema in live:
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:  # 删失败只提醒（清理是旁路，不许把这一轮弄挂）
            logger.warning("删失效租户偏移文件失败（%s）：%s", path.name, type(exc).__name__)
    if removed:
        logger.info("已清理 %s 个失效租户的回调偏移文件", len(removed))
    return removed


def rotate_callback_events() -> Path | None:
    """把「**已被所有租户读完**」的回调事件文件轮转成归档段，返回归档路径（没轮转 ⇒ `None`）。 ## 为什么轮转放在消费侧、而不是接收器的写热路径上本命令掌握**每一张单的消费位点**；接收器不认识租户、只认偏移文件。轮转的前置条件因此只能在这里判：**现役文件的大小 ≤ 所有租户偏移的最小值** （= 每一行都至少被每个租户「看过一遍」；找不到自己的 `SerialNo` 的行也算消费过）。
    ## 安全三件 1. **与接收器共用一把锁**（`dailiantong-callback.jsonl.lock`）：接收器 append 也拿它 ⇒ 改名不会把一条正在写的记录劈开，也不会出现「接收器还握着旧文件的 fd 继续写」； 2. **拿锁后复检大小**：从「判大小」到「拿到锁」之间可能又来了几条 ⇒ 大小变了就放弃这一轮；
    3. **轮转后把所有偏移文件归零**：归档段里的字节已全部消费，新现役文件从 0 开始 ⇒ 归零后没有任何租户会「从旧偏移跳着读新文件」。⚠️ 若不归零，某租户的旧偏移恰好小于新文件长度时就会**跳过新文件开头**那几条（静默丢事件）。 ⚠️ 消费侧**只读现役文件**（不读归档段）⇒ 轮转不改变读取实现；归档段只作事后取证。与鸡蛋铺线（`apps.callback.store` 的「偏移全文件连续」设计）的差异如实登记在这。"""
    path = callback_events_path()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("读回调事件文件失败（%s）：%s", path, type(exc).__name__)
        return None
    if size < _rotate_bytes():
        return None

    offsets = []
    for offset_path in _offset_files():
        offsets.append(_read_offset(offset_path))
    if not offsets:
        # 还没有任何租户读过 ⇒ ⛔ 不清（否则这一整段会被静默跳过）。
        return None
    if min(offsets) < size:
        return None

    lock_path = callback_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning("打不开回调锁文件（%s）：%s；本轮不轮转", lock_path, type(exc).__name__)
        return None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                if path.stat().st_size != size:
                    return None  # 窗口内又写了 ⇒ 下一轮
            except OSError:
                return None
            archive = path.with_name(f"{path.name}.{int(time.time())}")
            if archive.exists():
                # 同一秒内连续轮转：加序号，⛔ 不覆盖已有归档。
                archive = path.with_name(f"{path.name}.{int(time.time() * 1000)}")
                if archive.exists():
                    return None
            os.rename(path, archive)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
            # 归档段已全部消费 ⇒ 所有消费者回到新文件的 0（见 docstring 第 3 条）。
            for offset_path in _offset_files():
                try:
                    _write_offset(offset_path, 0)
                except OSError as exc:  # 写不进去只影响下一轮（会重复消费 / 跳过风险）
                    logger.warning(
                        "轮转后写偏移失败（%s）：%s；下一轮可能重复消费",
                        offset_path, type(exc).__name__,
                    )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)

    logger.info(
        "回调事件文件已轮转：%s（%s 字节）⇒ %s；新现役文件为空，消费偏移已归零",
        path.name, size, archive.name,
    )
    return archive


def prune_callback_archives(*, now: float | None = None) -> list[Path]:
    """删掉**超过保留期**的归档段，返回删掉的路径。 ⚠️ 归档段只作事后取证：消费侧从不读它 ⇒ 保留期到了就删（磁盘不能无限长）。
    ⚠️ 段龄 = `mtime`（这一段的最后一笔写入时刻）；⛔ 不用文件名里的时间戳
    （手工搬过文件之后那个数字会骗人）。"""
    path = callback_events_path()
    try:
        candidates = [
            item
            for item in path.parent.glob(path.name + ".*")
            if item.is_file()
            and not item.name.endswith(CALLBACK_LOCK_SUFFIX)
            and not item.name.endswith(".tmp")
        ]
    except OSError:  # pragma: no cover - 目录不可读
        return []
    cutoff = (time.time() if now is None else now) - _retention_seconds()
    removed: list[Path] = []
    for item in candidates:
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
                removed.append(item)
        except OSError as exc:  # 删失败只提醒（清理是旁路，不许把这一轮弄挂）
            logger.warning(
                "删回调归档段失败（%s）：%s", item.name, type(exc).__name__
            )
    if removed:
        logger.info("已删除 %s 个超过保留期的回调归档段", len(removed))
    return removed


def _read_offset(path: Path) -> int:
    """读偏移文件；不存在 / 内容坏了 ⇒ 0（从文件头读），并且**响亮**记一条日志。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning("读回调偏移文件失败（%s）：%s；本轮从文件头开始", path, type(exc).__name__)
        return 0
    if not text:
        return 0
    try:
        offset = int(text)
    except ValueError:
        logger.warning("回调偏移文件 %s 内容不是整数，本轮从文件头开始", path)
        return 0
    return max(offset, 0)


def _write_offset(path: Path, offset: int) -> None:
    """原子写偏移（临时文件 + `os.replace`），避免读到半截数字。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    os.replace(tmp, path)


def _serial_from_body(body) -> str:
    """从回调 `body` 里取 `SerialNo`。 `body` 是接收器原样落盘的**原始报文**（可能是 form-urlencoded，也可能是 JSON）。
    ⚠️ 只取 `SerialNo`；`Status` / `CancelStatus` **绝不**从这里落脚（见模块 docstring）。"""
    if isinstance(body, dict):
        for key, value in body.items():
            if str(key).lower() == "serialno":
                return str(value or "").strip()
        return ""

    if not isinstance(body, str):
        return ""

    text = body.strip()
    pairs: list[tuple[object, object]]
    if text.startswith("{"):
        try:
            parsed = json.loads(text, object_pairs_hook=list)
        except (TypeError, ValueError):
            return ""
        if not isinstance(parsed, list):
            return ""
        pairs = list(parsed)
    else:
        pairs = list(parse_qsl(text, keep_blank_values=True))

    for key, value in pairs:
        if str(key).lower() == "serialno":
            return str(value or "").strip()
    return ""


def _extract_serial_no(record) -> str:
    """事件的 `SerialNo`：优先顶层（兼容将来接收器直接记结构化字段），否则解析 `body` 原文。"""
    if not isinstance(record, dict):
        return ""
    for key in ("SerialNo", "serial_no", "serialNo"):
        value = record.get(key)
        if value:
            return str(value).strip()
    return _serial_from_body(record.get("body"))


class Command(BaseCommand):
    help = "代练通状态同步（消费回调队列 + 轮询 sent/unknown 推送行并回读远端状态）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="跑一轮就退（排障 / 手工；⛔ 不是生产调度——生产是下面的 --loop 常驻单元）",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="常驻轮询（生产形态：由发版装的 systemd 单元 tuantuantong-dailiantong-sync 跑它）",
        )
        parser.add_argument(
            "--interval", type=int, default=LOOP_INTERVAL_SECONDS,
            help=f"--loop 每轮之间的间隔秒数（默认 {LOOP_INTERVAL_SECONDS}；仅 --loop 使用，--once 时忽略）",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        interval = options.get("interval", LOOP_INTERVAL_SECONDS)
        if options.get("once") and options.get("loop"):
            # ⛔ 同时给会变成「静默常驻」（--once 被忽略）——宁可响亮报错。
            raise CommandError("--once 与 --loop 不能同时给（一个跑一趟、一个常驻）")

        # ⭐ 进程级锁：第二台机 / 第二个进程直接退出（⛔ 不并发发同一批单）。
        lock = _ProcessLock(sync_lock_path())
        if not lock.acquire():
            raise CommandError(
                "另一个 dailiantong_sync 进程正在运行"
                f"（锁文件：{lock.path}）。为避免重复发群码 / 自动回复，本进程退出。"
                "若是上一次异常退出留下的锁，等它释放或确认没有进程在跑后重试。",
                returncode=1,
            )
        try:
            if options.get("loop"):
                # `--interval` 只对 `--loop` 有意义：`--once` 跑一趟就退，传 0 / 负数也**不报错**
                # （help 里就是这么写的；⛔ 别让一个被忽略的参数把排障命令挡下来）。
                if interval is None or interval < 1:
                    raise CommandError("--interval 必须是 ≥ 1 的整数秒")
                # 与 peer_sync 同形：只置停止位，当前轮跑完再退。
                signal.signal(signal.SIGTERM, _request_stop)
                signal.signal(signal.SIGINT, _request_stop)
                self._loop(schema, interval)
                return

            if self._run_once(schema):
                # 退出码契约：只有**租户级**未预期失败才非零；单条回读失败已在汇总行里。
                raise CommandError(
                    "有租户的本轮出现未预期失败，详见上方汇总",
                    returncode=1,
                )
        finally:
            lock.release()

    def _loop(self, schema, interval: int) -> None:
        """常驻：**回调即时唤醒** + 每 `interval` 秒一轮**兜底轮询**（h13）。为什么不干脆 `_STOP.wait(interval)`：回调事件由**另一个进程**（接收器）追加到事件文件，本进程没有别的唤醒源；若一口气睡 1 小时，回调要等下一个整点才被消费 —— 那会把「回调为主」变成「1 小时一次」（接手后的群码 / 自动回复跟着晚一小时）。
        ⇒ 每 `CALLBACK_WATCH_SECONDS`： - `stat` 事件文件（不读内容、不打上游）；需要判「还有没有完整未消费行」时才读一个 64 KB 窗口，并查一次现役租户表（`_live_tenant_schemas` 的小 SELECT）； - 文件长度变了（增长 / 轮转后变小 / 从无到有）⇒ 立刻跑一轮
        `poll_upstream=False`（只消费回调 + 定时任务段，⛔ 不扫其它行）； - 长度没变但还有「完整未消费行」（一轮 500 条上限的剩余积压）⇒ 同样再跑一轮（判据 `_callbacks_pending`，只读一个 64 KB 窗口）； - 满 `interval` ⇒ 跑一轮完整的（含兜底轮询），并从这一刻重新计时。 ⚠️ 记的是**本轮开始前**的文件长度：轮次运行时又有回调落盘时，下一个 tick 仍会看到「长度变了」⇒ 不会丢唤醒（轮转把文件变小同样算「变了」，多跑一轮无害）。"""
        next_full_at = 0.0
        last_size = _callback_events_size()
        while not _STOP.is_set():
            now = time.monotonic()
            full_round = now >= next_full_at
            size = _callback_events_size()
            # ⚠️ `None` = 文件还不存在 / stat 失败 ⇒ 不当作「有变化」；只由满 interval 触发。
            changed = size is not None and size != last_size
            # ⚠️ 长度没变也可能有**上一轮没消费完**的完整行（一轮最多 500 条）⇒ 再唤一次。
            # 只在非完整轮时判：完整轮本来就要跑，省掉这次窗口读取。
            if not full_round and not changed:
                changed = _callbacks_pending(size)
            if full_round or changed:
                last_size = size
                self._run_once(schema, poll_upstream=full_round)
                if full_round:
                    next_full_at = time.monotonic() + interval
            # 下一觉：固定 tick；完整轮快到点时提前醒（不让 tick 把整点往后拖）。
            wait = CALLBACK_WATCH_SECONDS
            if not full_round:
                wait = min(wait, max(0.0, next_full_at - time.monotonic()))
            _STOP.wait(wait)

    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_once(self, schema, *, poll_upstream: bool = True) -> bool:
        """跑一趟所有（或指定的）租户，返回「是否出现过未预期失败」（退出码契约用）。 `poll_upstream=False` = **回调触发的轮次**：只消费回调 + 跑定时任务 / 发送段，
        ⛔ 不扫其它 `sent` / `unknown` 行（1 小时兜底由 `poll_upstream=True` 的那一轮做）。
        `--once`（排障 / 手工）**永远**是完整轮（默认 `True`）。"""
        unexpected = False
        # ⭐ 回调事件文件轮转 / 保留期清理：只在「全租户」的**完整轮**里做。 `--tenant` 是排障命令（只看一个租户），拿不全其它租户的偏移 ⇒ 判不了
        # 「每一行都消费过」，所以那种跑法**不轮转**（宁可继续追加）。回调触发的轮次也**不轮转**：轮转要求「各租户偏移都读到文件尾」，而这一轮
        # 只消费回调、正好会把偏移推到最后一行——同轮里判断/改名容易把刚写的行夹在中间。
        if schema is None and poll_upstream:
            try:
                # ⭐ M6：先把失效租户的偏移文件清掉，再轮转（否则 min(offsets) 永远为假）。
                _prune_stale_offset_files()
                rotate_callback_events()
                prune_callback_archives()
            except Exception as exc:  # noqa: BLE001 —— 运维旁路，不许把同步弄挂
                logger.warning(
                    "回调事件文件轮转 / 清理失败（%s）", type(exc).__name__
                )

        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if self._run_tenant(tenant, poll_upstream=poll_upstream):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户独立异常边界
                unexpected = True
                # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包片段（spec §11-S1）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}），下一个租户继续"
                )
        return unexpected

    def _run_tenant(self, tenant, *, poll_upstream: bool = True) -> bool:
        """在一个租户里跑一轮：⓪ pending 回收 → ① 消费回调 → ② 轮询 → ②.5 内部先接收口 → ②.6 自动仲裁 → ②.7 撞单 0 撤 → ③ 群二维码 → ④ 自动回复。
        ⭐ h13：`poll_upstream=False` = 回调触发的轮次 ⇒ **跳过 ②**（只看回调命中的行），但 ②.5–④ 照跑（定时任务与「接手后发送」都靠这一轮尽快发生）。
        每一段都**逐条独立异常边界**：一条失败只记日志、计入「失败 N」，不影响同租户的其它行（也⛔ 不影响退出码——那是预期内噪音）。返回「本租户是否出现过**段级**未预期失败」（由 `_guard_section` 计入，P3-14）。"""
        unexpected = False
        callback_hits: set[int] = set()

        # ⭐ P1-4：pending 超时回收（先做：回收出来的 unknown 行本轮机可参与轮询）。
        reclaimed = reclaim_stale_pending_pushes()
        callbacks, callback_failed = self._consume_callbacks(tenant, callback_hits)

        polled = 0
        failed = callback_failed
        skipped_without_serial = 0
        skipped_backoff = 0
        #: ⭐ dl-F：平台未接入的推送行本机跳过（不计失败，⛔ 也不打上游）。
        unconnected_platforms = 0
        if poll_upstream:
            now = timezone.now()
            pushes = DailiantongPush.objects.filter(
                state__in=(DailiantongPush.State.SENT, DailiantongPush.State.UNKNOWN),
                # 终态闸：`refresh_push_status` 在远端 `Status=17` / `CancelStatus` 12/15/16 后把
                # `is_active` 置 False ⇒ 历史终态单不再每轮白打上游。
                is_active=True,
            ).filter(Q(next_sync_at__isnull=True) | Q(next_sync_at__lte=now)).order_by("id")
            if callback_hits:
                # 本轮已经被回调「闹钟」回读过的行不重复打上游（回调优先，轮询补其它行）。
                pushes = pushes.exclude(pk__in=callback_hits)
            for push in pushes.iterator():
                if not push.serial_no:
                    # `unknown` 允许没有上游单号（超时 / 没拿到号，需要人工核对）。
                    # 这种行再回读也只会得到「没有上游订单号」⇒ 跳过并汇总，⛔ 不每轮刷失败。
                    skipped_without_serial += 1
                    continue
                # ⭐ dl-F（多平台骨架）：按**这一行自己的 platform** 取适配器再回读。
                # 未接入平台 ⇒ 跳过（不计失败、不打上游）；某一平台失败不影响别的平台的行。
                try:
                    adapter = get_adapter(push.platform)
                except PlatformNotConnected:
                    unconnected_platforms += 1
                    logger.warning(
                        "dailiantong_sync: 推送行平台未接入，跳过回读 tenant=%s push=%s platform=%s",
                        tenant.schema_name, push.pk, push.platform,
                    )
                    continue
                try:
                    adapter.refresh_push_status(push)
                    polled += 1
                except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
                    failed += 1
                    self._note_sync_failure(push)
                    logger.warning(
                        "dailiantong_sync: 回读推送行失败 tenant=%s push=%s serial=%s（%s）",
                        tenant.schema_name, push.pk, push.serial_no, type(exc).__name__,
                    )

            # 退避窗口里的行：只在汇总里报个数，⛔ 不打上游（P3-12）。
            skipped_backoff = DailiantongPush.objects.filter(
                state__in=(DailiantongPush.State.SENT, DailiantongPush.State.UNKNOWN),
                is_active=True,
                next_sync_at__gt=now,
            ).count()

        if poll_upstream:
            summary = (
                f"{tenant.schema_name}: 轮询 {polled} · 回调 {callbacks} · 失败 {failed}"
            )
        else:
            # 回调触发的轮次：这里写「兜底轮询 未到期」而不是「轮询 0」——免得排障时
            # 误以为「该扫的行没扫」（那是设计如此，1 小时兜底会扫）。
            summary = (
                f"{tenant.schema_name}: 回调 {callbacks} · 失败 {failed}"
                "（回调触发，本轮不扫其它行）"
            )
        if reclaimed:
            summary += f" · pending 回收 {reclaimed}"
        if skipped_without_serial:
            summary += f" · 无单号 {skipped_without_serial}"
        if skipped_backoff:
            summary += f" · 退避中 {skipped_backoff}"
        if unconnected_platforms:
            # ⭐ dl-F：未接入平台的行跳过（页面 / 排障要能看到「不是失败、是还没接入」）。
            summary += f" · 未接入平台跳过 {unconnected_platforms}"

        # ⭐⭐ A4（计划 Rev.9 §4 第 2 条）：**内部打手先接** ⇒ 及时把代练通订单撤掉。
        # 位置在**本轮回读之后**：判据用刚刷新的 `remote_status`（远端已接走的那一档
        # 由状态回写把本地打手撤销，⛔ 不在这里撤代练通——那是「两边同时接」，保代练通）。 ⚠️ 与群码 / 自动回复同一条纪律：整段异常边界，⛔ 不让它拖垮别的段。
        withdraw_done, withdraw_failed, withdraw_unexpected = self._guard_section(
            tenant, "内部先接收口", self._reconcile_internal_claims
        )
        unexpected = unexpected or withdraw_unexpected
        if withdraw_done:
            summary += f" · 内部先接已撤代练通 {withdraw_done}"
        if withdraw_failed:
            summary += f" · 内部先接撤回失败 {withdraw_failed}"

        # ⭐：协商撤销满 24 小时 ⇒ 自动申请客服介入。
        #    位置在**本轮回读之后**：候选集用的 `remote_cancel_status=11` 是本轮刚刷新的值。
        #    ⚠️ 与其它段同一条纪律：整段异常边界（`_guard_section`），⛔ 不拖垮群码 / 自动回复。
        arbitrated, arbitrate_failed, arbitrate_unexpected = self._guard_section(
            tenant, "自动仲裁", self._auto_arbitrate
        )
        unexpected = unexpected or arbitrate_unexpected
        if arbitrated:
            summary += f" · 自动仲裁 {arbitrated}"
        if arbitrate_failed:
            summary += f" · 自动仲裁失败 {arbitrate_failed}"

        # ⭐⭐ H3-1：**撞单 ⇒ 保留最早接单，其余 0 撤**。位置在**本轮回读之后**：候选集用刚刷新的 `remote_status`（12 / 13 = 已接手）。
        # 开关是 `PlatformPushSettings.auto_zero_refund_on_conflict`：关 ⇒ 这一段只有
        # 读开关这一下，**零上游调用、零写入**（实现里的第一条判据）。 ⚠️ 与其它段同一条纪律：整段异常边界，⛔ 不拖垮群码 / 自动回复。
        zero_refunded, zero_refund_failed, zero_refund_unexpected = self._guard_section(
            tenant, "撞单0撤", self._zero_refund_conflicts
        )
        unexpected = unexpected or zero_refund_unexpected
        if zero_refunded:
            summary += f" · 撞单0撤 {zero_refunded}"
        if zero_refund_failed:
            summary += f" · 撞单0撤失败 {zero_refund_failed}"

        # ⭐（业务方）：把打手上传的**进度图**同步进本仓「订单截图」。 ⭐ I3：只在**完整轮询**（poll_upstream=True）跑——回调唤醒轮次
        # 「不扫其它行」，若每回调都逐条打 progress_list，请求量会变成
        # 回调数 × 活跃单数，与回调优先的设计自相矛盾。
        if poll_upstream:
            progress_added, progress_failed, progress_unexpected = self._guard_section(
                tenant, "进度图同步", self._sync_progress_screenshots
            )
            unexpected = unexpected or progress_unexpected
            if progress_added:
                summary += f" · 进度图 {progress_added}"
            if progress_failed:
                summary += f" · 进度图失败 {progress_failed}"

        # ⭐ T12：接手后自动发群二维码（在**本轮回读之后**跑：`remote_status=12` 是本轮的
        # 回读结果；回调路径上的行也已落库，同样会被这一步看到）。
        # ⚠️ 两段（群码 / 自动回复）各自兜整段的未预期异常：**互不阻塞**是本任务的硬要求。
        qr_sent, qr_failed, qr_unexpected = self._guard_section(
            tenant, "接手后发送", self._send_group_qr
        )
        unexpected = unexpected or qr_unexpected
        if qr_sent:
            summary += f" · 接手后 {qr_sent}"
        if qr_failed:
            summary += f" · 接手后失败 {qr_failed}"

        # ⭐ T14：按规则自动回复（同样在**本轮回读之后**；只剩 13 ⇒ 申请验收后）。
        auto_sent, auto_failed, auto_unexpected = self._guard_section(
            tenant, "自动回复", self._dispatch_auto_replies
        )
        unexpected = unexpected or auto_unexpected
        if auto_sent:
            summary += f" · 自动回复 {auto_sent}"
        if auto_failed:
            summary += f" · 自动回复失败 {auto_failed}"
        self.stdout.write(summary)
        return unexpected

    def _note_sync_failure(self, push) -> None:
        """记一次回读失败：累加退避（1/5/15/60 分钟），到上限停止轮询并写可读原因。"""
        attempts = (push.sync_attempts or 0) + 1
        now = timezone.now()
        update_fields = ["sync_attempts", "next_sync_at", "is_active", "last_error", "updated_at"]
        push.sync_attempts = attempts
        if attempts >= MAX_SYNC_FAILURES:
            push.is_active = False
            push.next_sync_at = None
            reason = (
                f"连续 {attempts} 次回读失败，已停止自动轮询："
                f"{push.last_error or '上游回读失败'}"
                "。请人工核对后在代练通后台处理。"
            )
            push.last_error = reason[:LAST_ERROR_MAX_LENGTH]
        else:
            minutes = SYNC_BACKOFF_MINUTES[min(attempts - 1, len(SYNC_BACKOFF_MINUTES) - 1)]
            push.next_sync_at = now + timedelta(minutes=minutes)
        push.save(update_fields=update_fields)

    def _guard_section(self, tenant, label: str, section) -> tuple[int, int, bool]:
        """跑一个「批量发送」段，把**整段**的未预期异常挡在这一段里。返回 `(成功, 失败, 是否出现段级意外)`。⭐ P3-14：段级意外不再被吞成 `(0, 0)`
        —— 它会被计进 `unexpected`，`--once` 因此**非零退出**（不再「看起来全绿」）。
        段内单条失败已由各段自己收口；群二维码段炸了⛔ 不能让自动回复跟着不跑。"""
        try:
            sent, failed = section(tenant)
            return sent, failed, False
        except Exception as exc:  # noqa: BLE001 —— 段级异常边界
            logger.warning(
                "dailiantong_sync: %s段未预期失败 tenant=%s（%s）",
                label,
                tenant.schema_name,
                type(exc).__name__,
            )
            return 0, 0, True

    def _reconcile_internal_claims(self, tenant) -> tuple[int, int]:
        """A4（计划 Rev.9 §4 第 2 条）：**内部打手先接** ⇒ 把代练通订单撤掉。判据 / 做法 / 有界重试全在 `dailiantong_push.reconcile_internal_claims`
        （它才是唯一实现，本命令只负责每轮触发一次）；返回 `(成功, 失败)`。 ⚠️ 为什么放在同步命令里而不是抢单事务里：撤回是**上游写动作**（不重试、不该让
        客服等一个 HTTP），而「及时」的口径是「下一轮同步内」——有回调时即时唤醒，最迟 1 小时的兜底轮。 ⚠️ 段级异常由 `_guard_section` 收口：它炸了不许拖垮群码 / 自动回复两段。"""
        from apps.business.dailiantong_push import reconcile_internal_claims

        return reconcile_internal_claims()

    def _auto_arbitrate(self, tenant) -> tuple[int, int]:
        """⭐：协商撤销满 24 小时 ⇒ 自动申请客服介入；返回 `(已收口, 失败)`。判据 / 时间门槛 / 退避 / 只做一次全在 `dailiantong_extra_actions.auto_arbitrate_pending_pushes`（唯一实现，本命令只负责
        每轮触发一次）。⛔ **不另起 systemd 单元**：它与轮询共用同一个轮次节奏（回调即时唤醒 + 1 小时兜底）。 ⚠️ 段级异常由 `_guard_section` 收口：它炸了不许拖垮群码 / 自动回复两段。
        ⚠️ 单行未预期失败由实现内部逐行收口（不计段级意外）。"""
        return auto_arbitrate_pending_pushes()

    def _zero_refund_conflicts(self, tenant) -> tuple[int, int]:
        """⭐ H3-1：撞单 ⇒ 保留最早接单，其余 0 撤；返回 `(已发起, 失败)`。判据 / 「最早接单」的排序 / 开关 / 幂等 / 退避全在 `dailiantong_push.zero_refund_conflicting_pushes`（唯一实现，本命令只负责每轮
        触发一次）。⛔ **不另起 systemd 单元**：它与轮询共用同一个轮次节奏（回调即时唤醒 + 1 小时兜底）。 ⚠️ 开关 `PlatformPushSettings.auto_zero_refund_on_conflict` 关 ⇒ 返回 `(0, 0)`
        且**零上游调用**（判据在实现里，⛔ 不在本命令另判一份——那会变成两处真相）。 ⚠️ 段级异常由 `_guard_section` 收口；单行失败由实现内部逐行收口（不计段级意外）。"""
        return zero_refund_conflicting_pushes()

    def _sync_progress_screenshots(self, tenant) -> tuple[int, int]:
        """本租户所有有效推送同步一轮打手进度图；返回 `(新增, 失败)`。"""
        return sync_active_progress_screenshots()

    def _send_group_qr(self, tenant) -> tuple[int, int]:
        """对「已接管（`Status=12/13`）且还没发过接手后内容」的推送发一次，返回 `(成功, 失败)`。 ⭐（B1）：这一段现在是**接手后发送的唯一入口**（一张图 + 一条规则话术，共用一个 `qr_sent_at` 标记）；`dailiantong_auto_reply` 只负责「申请验收后」。
        ⭐ 「首次变成 12」的落地 = **持久化条件** `remote_status=12 且 qr_sent_at is null`， ⛔ 不是进程内的状态跃迁检测： - 进程重启 / 上线前已经被接手的单也不会漏发（业务口径是「接手后就发」）； - 幂等仍由 `qr_sent_at` 唯一承担（`send_accept_qr` 自己先判一次，这里再收窄）；
        - 失败的行**不置** `qr_sent_at` ⇒ 下一轮自然重试，但**每轮至多一次**（⛔ 不刷屏）。 ⚠️ **候选集不按第 6 条开关收窄**（业务方口径）：那个开关只管图，规则话术与它无关 ⇒ 关着开关也要逐行跑（由 `send_accept_qr` 决定「只发文字 / 什么都不发」）。单条失败只记日志（预期内噪音：对端超时 / 没图 / 上传失败），⛔ 不影响整轮退出码。"""
        candidates = (
            DailiantongPush.objects.filter(
                state__in=DailiantongPush.ACTIVE_STATES,
                # ⭐ P3-11：判据是「曾经到过 12」而不是「本轮正好是 12」。
                # 12→13 可能落在两次轮询之间；只认 12 会让那张单**永远不发**。
                # `13`（等待验收）且还没发过 ⇒ 同样补发（12 一定在它之前发生过）。
                remote_status__in=ACCEPT_STAGE_REMOTE_STATUSES,
                qr_sent_at__isnull=True,
            )
            .exclude(serial_no__isnull=True)
            .exclude(serial_no="")
            .order_by("id")
        )
        sent = 0
        failed = 0
        for push in candidates.iterator():
            # ⭐ dl-F：`send_accept_qr` 是**模块级分派入口**（按这一行的 platform 取适配器；
            # 未接入平台 ⇒ 跳过并把可读原因写进 `qr_last_error`，返回 None、零上游请求）。
            try:
                outcome = send_accept_qr(push)
            except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
                failed += 1
                logger.warning(
                    "dailiantong_sync: 群二维码发送失败 tenant=%s push=%s（%s）",
                    tenant.schema_name,
                    push.pk,
                    type(exc).__name__,
                )
                continue
            if outcome is None:
                # ⭐ dl-F：未接入平台 ⇒ 已写可读原因，不计成功也不计失败。
                continue
            if outcome.sent:
                sent += 1
            elif outcome.failed:
                failed += 1
        return sent, failed

    def _dispatch_auto_replies(self, tenant) -> tuple[int, int]:
        """对 `remote_status=13` 的活跃推送逐条跑一次自动回复，返回 `(成功, 失败)`。 ⭐ 触发判据在引擎里（`dailiantong_auto_reply.TRIGGER_BY_REMOTE_STATUS`），这里只收窄
        候选集：**本轮已回读、还在轮询窗口内**的行。每轮每行**至多调一次** `dispatch_for_push`，失败不写台账 ⇒ 下一轮自然重试（⛔ 不刷屏）。 ⚠️ `12`（接单后）不在这里：那条由 `_send_group_qr` 的合并入口负责。
        ⚠️ 这里**不预筛「已经发过某条规则」**：「发没发过」的判据是 `(push, rule)` 台账，只有引擎知道该选哪条规则。单条失败只记日志（预期内噪音：配置 / 存储 / 上游 / 上传），⛔ 不影响整轮退出码， ⛔ 也不影响同轮的群二维码（两段由 `_guard_section` 各自兜底）。"""
        candidates = (
            DailiantongPush.objects.filter(
                state__in=DailiantongPush.ACTIVE_STATES,
                is_active=True,
                # ⚠️ 与引擎共用同一张映射表：⛔ 不再手抄 13（那是第二处真相，
                # 迟早与 `TRIGGER_BY_REMOTE_STATUS` 漂移）。
                remote_status__in=tuple(TRIGGER_BY_REMOTE_STATUS),
            )
            .exclude(serial_no__isnull=True)
            .exclude(serial_no="")
            .order_by("id")
        )
        sent = 0
        failed = 0
        for push in candidates.iterator():
            # ⭐ dl-F：`dispatch_for_push` 是**模块级分派入口**（同群码段的同一条口径：
            # 未接入平台 ⇒ 跳过 + 写 `auto_reply_last_error`，返回 None、零上游请求）。
            try:
                outcome = dispatch_for_push(push)
            except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
                failed += 1
                logger.warning(
                    "dailiantong_sync: 自动回复失败 tenant=%s push=%s（%s）",
                    tenant.schema_name,
                    push.pk,
                    type(exc).__name__,
                )
                continue
            if outcome is None:
                # ⭐ dl-F：未接入平台 ⇒ 已写可读原因，不计成功也不计失败。
                continue
            if outcome.sent:
                sent += 1
            elif outcome.failed:
                failed += 1
        return sent, failed

    def _consume_callbacks(self, tenant, handled: set[int]) -> tuple[int, int]:
        """消费该租户尚未读过的回调事件，返回 `(触发回读的事件数, 失败数)`。 - 只读**完整行**（最后一行没有换行 ⇒ 本轮不碰，等接收器写完下一轮再读）；
        - 消费位点写在**每个租户自己的偏移文件**里（见 `callback_offset_path`）； - 事件里的 `Status` **绝不**落库：只取 `SerialNo`，再调 `refresh_push_status` 回读。"""
        path = callback_events_path()
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0, 0
        except OSError as exc:
            logger.warning("读回调事件文件失败（%s）：%s", path, type(exc).__name__)
            return 0, 0

        offset_path = callback_offset_path(tenant.schema_name)
        offset = _read_offset(offset_path)
        if offset > size:
            # 文件被截断 / 轮转过 ⇒ 旧偏移没有意义，从文件头开始（宁可重复回读一次）。
            logger.warning(
                "回调偏移 %s 大于事件文件长度（%s > %s）⇒ 从文件头重新读",
                offset_path, offset, size,
            )
            offset = 0

        callbacks = 0
        failures = 0
        consumed = offset
        seen_serials: set[str] = set()
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(MAX_CALLBACK_EVENTS_PER_ROUND):
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        # 接收器正在写 / 只写了一半：不消费这一行，也不推进偏移。
                        stream.seek(line_start)
                        break
                    consumed = stream.tell()
                    outcome = self._handle_callback_line(line, tenant, handled, seen_serials)
                    if outcome is True:
                        callbacks += 1
                    elif outcome is False:
                        failures += 1
        except OSError as exc:
            logger.warning("读回调事件文件失败（%s）：%s", path, type(exc).__name__)
            return callbacks, failures

        if consumed != offset:
            try:
                _write_offset(offset_path, consumed)
            except OSError as exc:
                # 偏移写不进去只意味着下一轮会重复消费同几条（回读是幂等的），
                # ⛔ 不该让整个租户的轮询跟着失败。
                logger.warning(
                    "写回调偏移文件失败（%s）：%s；下一轮会重复消费这几条",
                    offset_path, type(exc).__name__,
                )
        return callbacks, failures

    def _handle_callback_line(
        self,
        line: bytes,
        tenant,
        handled: set[int],
        seen_serials: set[str],
    ) -> bool | None:
        """处理一行回调。返回 `True`=触发回读、`False`=回读失败、`None`=不是可用事件。"""
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("回调事件文件里有一行不是合法 JSON，跳过（tenant=%s）", tenant.schema_name)
            return None

        serial = _extract_serial_no(record)
        if not serial:
            logger.warning("回调事件没有 SerialNo，跳过（tenant=%s）", tenant.schema_name)
            return None
        if serial in seen_serials:
            return None
        seen_serials.add(serial)

        push = (
            DailiantongPush.objects.filter(
                serial_no=serial,
                state__in=(DailiantongPush.State.SENT, DailiantongPush.State.UNKNOWN),
                # ⚠️ **故意不加** `is_active=True`：回调是闹钟，终态行也要**强制回读一次**
                # （终态判定以这次回读结果为准；`is_active` 只是「轮询不再碰它」）。
            )
            .order_by("id")
            .first()
        )
        if push is None:
            # 可能是别的租户的推送行（事件文件是机器级的），也可能这条行已被删除。
            logger.debug(
                "回调事件找不到对应推送行（tenant=%s serial=%s）", tenant.schema_name, serial
            )
            return None

        # ⭐ dl-F：回调触发的回读同样按**行上的 platform** 分派（回调只认代练通签名，
        # 但行本身可能不是代练通的；未接入平台跳过，⛔ 不拿代练通客户端去打它）。
        adapter = adapter_or_none(push.platform)
        if adapter is None:
            logger.warning(
                "dailiantong_sync: 回调命中未接入平台的推送行，跳过回读 tenant=%s push=%s platform=%s",
                tenant.schema_name, push.pk, push.platform,
            )
            return None
        try:
            adapter.refresh_push_status(push)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
            logger.warning(
                "dailiantong_sync: 回调触发回读失败 tenant=%s push=%s serial=%s（%s）",
                tenant.schema_name, push.pk, serial, type(exc).__name__,
            )
            return False

        handled.add(push.pk)
        return True
