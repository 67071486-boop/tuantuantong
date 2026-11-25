"""上下游**接收侧**的事件落地内核（spec §16.2 / §16.3 / §16.5）。自 `api/peer_views.py` 搬出：视图只组装响应信封，落库规则收在这里；⛔ 本模块不许 import `apps.business.api.peer_views`（循环）。
下行 `apply_events`：事件级字段只读**事件对象顶层**（⛔ 不读 `payload` 同名键——那里只放 spec §10.1 白名单订单字段）；时刻裁决按链行 `last_applied_at` 与并列的 `local_action_at` 双闸（都 `<=` 即旧 ⇒ 良性 `stale`，相等 = 重投）；未知状态硬跳过 ⛔ 不回落；形状闸（`_peer_value_shape_error` / `_has_unstorable_text`）与一切校验都在碰任何 DB **之前**、`atomic()` 块内只有写入（E-0022）；截图与 `hall.*` 大厅事件排在状态闸之前、不参与新旧裁决；本地没单 `assign`/`grab` 照常落单、`withdraw` ⇒ `unknown_order`；接手类落「未分配」，唯一例外 D-0303：完成抢单转正 ⇒ 「代练中」。
上行 `apply_upstream_events` 只改状态，承重安全判据 = 当前 MAIN 接手人 = 这条绑定账号（否则 `not_your_order`）；撤单落库跑 `refunds.on_order_refunded`（传**赋值前**状态，失败 `set_rollback` + skip 可重投）；订单日志唯一写出口 `_record_peer_sync`（跳过 / 幂等不写，撤单走既有 `order.peer_withdraw` ⛔ 不写第二行；`hall.*` 只改镜像卡、有意不写）；入站限流未做（登记残差：无计数来源、进程内计数对多 worker 不成立），滥用面靠令牌只对一家下家 + Z20 来源校验收窄。多跳转发落点表见 `docs/code-comment-notes.md`；细节见 `_apply_hall_event` / `_forward_mirror_claim` / `_promote_claiming_mirror` 的 docstring。"""
from __future__ import annotations

import logging
import uuid
from collections import deque
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import NamedTuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import group_qr, hall_shadow, jidanpu_assignment_lock
from apps.business.media_access import is_shared_object_key
from apps.business.order_status_persistence import persist_order_status
from apps.business.wxpusher_events import queue_order_status_notice
from apps.business.peer_sync import (
    PEER_DIRECT_PENDING_MESSAGE,
    DOWNSTREAM_ACCEPTED_STATUSES,
    HALL_CLAIM_KIND,
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
    HALL_INBOUND_KINDS,
    HALL_PULL_KIND,
    HALL_REPRICE_KIND,
    HALL_TAKE_KIND,
    HALL_TAKE_LEAVE_ASSIGNED,
    HALL_THROW_KIND,
    WITHDRAW_REASON_TEXT,
    effective_peer_ref,
    is_negative_amount,
    source_name_for,
)

from apps.business.upstream_game_rules import allowed_upstream_game, upstream_game_price_percent

logger = logging.getLogger(__name__)

#: 良性的跳过（本地更新或结账已优先）：发送侧据此把事件记成 `done`（spec §16.5）。
SKIP_STALE = "stale"
#: 形状不对：事件不是对象 / 缺 `target_ref` / `payload` 不是对象 / 已有单时时刻不可用 / 撤单原因不在枚举 /
#: 游戏名缺失、空串或不是字符串（"名字对不上"走自动建游戏，⛔ 不落这里）/ 对端值形状不对（非字符串、空金额、非数值金额、
#: 装不进 `_MODE_TEXT` 列宽、查找键超列宽——整类闸见 `_peer_value_shape_error`）；⚠️ 超宽的 `project_text` 不在此列——它按 R19 口径**截断**（`_MODE_CLIP`），不是形状错。发送侧据此记 `failed`（spec §16.5）。
SKIP_BAD_SHAPE = "bad_shape"
#: 状态值不在 `OrderStatus.values` 里（spec §10.2 的硬跳过，⛔ 不回落默认态）。
SKIP_UNKNOWN_STATUS = "unknown_status"
#: `order.withdraw` 指向一张本地没有的单（spec §16.3 末表）：发送侧据此记 `failed`（可见）。
SKIP_UNKNOWN_ORDER = "unknown_order"
#: **上行专有**：回传指认的那张单，当前 MAIN 接手人**不是**这条下游绑定的账号
#: （承重安全判据，见 `apply_upstream_events`）。发送侧据此记 `failed`（可见）。
SKIP_NOT_YOUR_ORDER = "not_your_order"
#: **下行专有**（结算 P0-5）：上家改价落到一张「MAIN 行**已结账**」的单上 ⇒ 发单价**照写**（下家那一格必须跟），
#: 但金额**不重算**（钱已按旧价发过，⛔ 不许改已发钱行的 `bonus_amount`）⇒ 留一条原因给人工调账；本事件仍算 `applied`（价 / 状态 / 下游排队都落库）。
#: ⚠️ **已退役（D-0307）**：`order.update` 改价支不再重算打手金额，既不会有「已结账未重算」也不会有「重算失败回滚」；两个码保留**只为**老事件台账 / 历史记录（读侧按字符串匹配，⛔ 不必 import）。
SKIP_SETTLED_NO_RECOMPUTE = "settled_no_recompute"
#: **下行专有**（结算 P0-5）：上家改价后按新价**重算提成算不出**（`CommissionError` / `AmountError`）⇒ 整条事件**回滚**（价也不改），事件记 skip、不推进 `last_applied_at`。
#: ⚠️ **发送侧对未知码的处置（`peer_worker._hard_skip_reason`，读码核对）**：只把 `stale` 当良性（记 `done`）；其余任何 code（含本仓新加的这个）⇒ 业务类失败（`FAILURE_BUSINESS`，按退避重投、第 5 次终态 `failed` 并把原因写进事件台账），**不计入自动停绑计数**（那只抓连接类失败）⇒ 新增码**不会**把绑定打死。
#: ⚠️ **已退役（D-0307）**：见 `SKIP_SETTLED_NO_RECOMPUTE` 上面那段。
SKIP_RECOMPUTE_FAILED = "recompute_failed"
#: **下行专有**（结算 P0-7，`D-0287`）：撤单落库后跑 `refunds.on_order_refunded` **失败**（金额 / 提成类领域错误）⇒ 整条事件**回滚**（状态、订单日志、钱、绩效**一起消失**），事件记 skip、**不推进** `last_applied_at`。
#: ⚠️ 为什么不吞掉这一档继续「只改状态」：钱已经自动发出去过，半笔比不落地更坏——「上家撤了、我方还把单标成已退单、钱却留在打手钱包里」正是本条要堵的形状；**不可预期的异常**（数据脏、DB 故障）**不**在这里接，照旧响亮 500（E-0022）。
#: ⚠️ **发送侧处置与 `SKIP_RECOMPUTE_FAILED` 同族**（只把 `stale` 当良性，其余 code 一律业务类失败 + 退避重投，不计入自动停绑）⇒ 本码不会把绑定打死。
SKIP_WITHDRAW_REFUND_FAILED = "withdraw_refund_failed"
#: **下行专有**（⭐ A2 / M-2）：事件 kind 没有显式分支、要走「状态照抄」路（建单支的首条 `order.update` / 未知 kind；已建链 else 支的未知 kind）时，那个状态必须在下家接受集（`peer_sync.DOWNSTREAM_ACCEPTED_STATUSES`，业务方裁定的 4 种）里；集外 ⇒ 整条硬跳过——盖住 M-2 的现场：对端发一个未知 kind + 任意合法状态就能绕过接受集直写状态列。
#: ⚠️ 已知 kind 的显式分支（`assign` / `grab` / `withdraw` / 已建链 `order.update` 的 R3 口径）**不受本闸管**——它们从不照抄 `raw_status`。
#: ⚠️ 发送侧对非 `stale` 码按**业务类失败**退避重投（⛔ 不计自动停绑）——两机版本偏斜时对端能看到明确的「状态不被接受」，比静默照抄安全。
SKIP_STATUS_NOT_ACCEPTED_FOR_KIND = "status_not_accepted_for_kind"
#: **下行专有**（⭐ A7）：`payload.hall_price` 是**负数** ⇒ 整条硬跳过。口径（业务方裁定）：形状（落不落得进金额列）归 `_peer_value_shape_error`；
#: **值域**只加这一条下限——0 与正数照常（D-0015：0 是合法边界）；**不设上限**是登记的残差（落得进 `numeric(12,2)` 的天文数字仍会落库，等口径再收）。
#: ⚠️ 大厅拉取行的同一闸在 `apply_hall_pull_rows`（坏行计 `failures`，无台账面）。
SKIP_NEGATIVE_PRICE = "negative_price_rejected"
#: **下行专有**（⭐ A5）：抢单转正落库撞上**业务类**失败（提成配置坏 `CommissionError` / 金额越界 `AmountError` / 归属说不出 `ValueError`）
#: ⇒ 整条事件回滚、记 skip、**可重投**；⛔ 不再裸 500——那会被发送侧归「连接类失败」、~5 轮后自动停绑（毒循环，M-8 现场：提成配置坏 ⇒ assign 每次投递整批 500 ⇒ 双向 kill）。
#: ⚠️ 自动重投窗口 = 退避梯 ~81 分钟（60s/5m/15m/1h），超窗终态 `failed` 靠 web 手推 / 人工（登记残差）；配置修复后的重投按既有用例 `test_the_event_can_be_applied_once_the_commission_is_fixed` 同族语义成功。
SKIP_PROMOTION_FAILED = "promotion_failed_business"

#: 截图广播的两个 kind。**两个接收支**（下行 `apply_events` / 上行
#: `apply_upstream_events`）都要认它们，且都必须排在**状态闸之前**——截图载荷里根本没有
#: `status` 字段，排在后面会被静默判成 `unknown_status`。
SCREENSHOT_ADD_KIND = "order.screenshot.add"
SCREENSHOT_DELETE_KIND = "order.screenshot.delete"
SCREENSHOT_KINDS = (SCREENSHOT_ADD_KIND, SCREENSHOT_DELETE_KIND)

#: 更新一支要写的订单列（spec §10.1 白名单里属于订单自己的那些）。⛔ 标识与归属列（`order_no` / `source_order_no` /
#: `order_source` / `source` / `sales_user` / `sales_agent` / `created_by` / `created_at`）不在内：来源与提成归属是**首单**的事实（spec §10.4 末段 / E-0027 同族）。
_UPDATE_FIELDS = (
    "status", "total_price", "hall_price", "remark", "project_text",
    "customer_phone", "customer_qq", "content", "game",
)

# ── 有界列 / 精确查找键的**整类**形状闸（P4 挂账⑨）────────────────────────────
#: 口径一：必须是 `str` 且装得进目标列宽（空串合法）；超宽 / 非字符串 ⇒ 形状错。
_MODE_TEXT = "text"
#: 口径二：必须是 `str`；**超宽按已登记的列宽截断口径**处理（R19，指 `project_text`）。
_MODE_CLIP = "clip"
#: 口径三：必须落得进 `numeric(max_digits, decimal_places)`（金额列）。
_MODE_AMOUNT = "amount"

#: 下行接收侧**对端可控、且会落进有界列**的 `payload` 字段。元组 = (`payload` 键, `Order` 的列名, 口径)；列宽 / 精度一律从**模型元数据**取（⛔ 不抄数字）。
#: ⚠️ 这里**只列落进有界列的**那些：`remark` / `content` 是 `TextField`（无列宽，超长本来就存得下），`status` / `withdraw_reason` / `occurred_at` / `source_order_no` / `game_name` 各自已有出口（枚举闸 / 时刻解析 / 非字符串退回+截断 / 非空字符串+截断）⇒ 不重复登记。
_DOWNSTREAM_BOUNDED_FIELDS = (
    # ⛔ 业务方裁定：下家的「客户价（= 接单价）」一律取**发单价**——
    #    上家的客户价不随载荷下发（`PEER_ORDER_FIELDS` 已删 `total_price`）⇒
    #    两列同源（`hall_price`），校验也只做这一处。
    ("hall_price", "hall_price", _MODE_AMOUNT),
    ("customer_phone", "customer_phone", _MODE_TEXT),
    ("customer_qq", "customer_qq", _MODE_TEXT),
    ("project_text", "project_text", _MODE_CLIP),
)


class Outcome(NamedTuple):
    """一批事件的结果：应用数、跳过数、逐条跳过明细（spec §16.5）。⭐ **D-0347 起多第 4 格 `claim_replies`**（带默认，既有构造点逐字不变）：上行 `hall.claim` 的**判决搭响应车**——原生支受理 / 回绝时把刚排好的应答事件（含受理时的 `order.assign`）序列化进响应，让下家的**同步抢单**（`peer_hall.claim_peer_mirror_sync`）在同一个请求窗内拿到终局；⛔ 对端 outbox **照常**排这些行、照常由 worker 重发（响应车只是抄近路，响应丢失时异步重发就是兜底；两边幂等：`order.assign` 重放按 `link.last_applied_at` 判 `stale`、`claim_ok` 只记账、`claim_refused` 的条件更新只认 `claiming` 行）。多跳转发支给不出判决 ⇒ `outcome="forwarded"`（下家按「未响应」当场放回重试）。"""

    applied: int
    skipped: int
    skipped_details: list[dict]   # [{"target_ref": str, "code": str, "reason": str}]
    claim_replies: tuple = ()     # [{"target_ref", "outcome", "reply_events": [事件线形状]}]


def _parse_event_time(value):
    """事件级 `occurred_at` → **带时区**的 `datetime`；不可用 ⇒ `None`。四类都归 `None`（= 调用方按 `bad_shape` / 「时间未知」处理）：缺失、不是字符串、`parse_datetime` 解析不出、解析出来没有时区。
    ⛔ **无时区的那一类必须也归 `None`**：本仓 `USE_TZ=True`，库里那两个时刻都是 aware，拿 naive 去比较会抛 `TypeError` ⇒ 一个畸形外部串会变成 500（E-0010）；⛔ 不做「按本地时区补一个」的猜测：那是替对端改口径（判错的代价是静默错判新旧）。"""
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed


#: ⭐ B5 / L-10：水位**自检**的「跳变」阈值（秒）。对账水位（`PeerOrderLink.last_applied_at`）记的是**对端的事件时刻**——它建立在「两边时钟大体一致」的跨机假设上：
#: 对端钟**超前** ⇒ 它的事件时刻落在未来 ⇒ 水位超前 ⇒ 「我还没应用」的事件被水位判成「已应用」，对账自愈**静默失灵**（L-10 的现场）。超 1 小时（对正常延迟来说已经离谱）就 `logger.warning` 一条，让这种偏斜**有信号**。
WATERMARK_SKEW_WARN_SECONDS = 3600


def _warn_watermark_anomaly(old, new, *, now, binding, peer_order_no) -> None:
    """写水位**之前**的自检：倒退 / 比本机时钟超前超 1 小时 ⇒ `logger.warning`（只报不拦）。**倒退**（`old` 非空且 `new < old`）今天被 `apply_events` 的 stale 闸结构性挡死 ⇒ 这条腿是给**将来新增的水位写点**站岗的自检（真走到就是有人绕过了闸）；**超前**（`new > now + WATERMARK_SKEW_WARN_SECONDS`）= 对端钟超前 / `occurred_at` 被写歪 ⇒ 水位会跑到未来、对账自此失灵（L-10 要抓的那一档）。⛔ 不是承重逻辑、也不抛错——自检的职责是让不变量破坏**有信号**，处置仍归人。
    ⚠️ **只 log、不拒**：拒掉会把「对端钟快」变成「事件全被跳过」，比失灵的对账更糟（业务方口径没裁过拒收）；本函数是**观察点**，⛔ 不要在这里加行为。"""
    if new is None:
        return
    if old is not None and new < old:
        logger.warning(
            "同步水位自检：绑定 %s 的单 %s 水位将倒退（现值 %s → 新值 %s）；"
            "正常路径走不到这里（stale 闸在前），请查这条事件的来源",
            binding.pk, peer_order_no, old.isoformat(), new.isoformat(),
        )
        return
    if new > now + timedelta(seconds=WATERMARK_SKEW_WARN_SECONDS):
        logger.warning(
            "同步水位自检：绑定 %s 的单 %s 新水位比本机时钟超前超过 %s 秒"
            "（水位 %s / 本机 %s）——对端时钟可能超前，对账水位将落在未来、"
            "落后事件会被误判为已应用",
            binding.pk, peer_order_no, WATERMARK_SKEW_WARN_SECONDS,
            new.isoformat(), now.isoformat(),
        )


def resolve_game(game_name, *, binding, new_sync=False):
    """按**名字**对齐本地 `Game`（裁决 R21）；名字对不上就**自动建**（spec §16.4 / A14+A15，宁可建配置、不能漏单）。回一个 `Game`，或 `None`——**`None` 只在名字缺失 / 空串 / 不是字符串时**返回（"建不出无名游戏"，这三种形状调用方仍按 `bad_shape` 硬跳过），⛔ 别拿别的理由回 `None`。
    ⚠️ **名字对不上 = 建一款同名的**：新行 `is_active=True`、其余开关取模型默认；⛔ 不给 `Game` 加「同步来源」标记列（对端是谁已记在 `Order` 来源快照与 `PeerOrderLink` 上）。名字先按 `Game.name` 的**列宽截断**（出题方裁定 1，先例 `project_text` 的 **R19**）——超长的对端值不许把整批落单一起回滚，代价可见：截断后的名字可能与对端不同名。
    ⚠️ **同名但已软删 ⇒ 复活，⛔ 不许再建一行**：`Game.name` 的 `uniq_game_name` **把软删行也算在内**（软删是保历史订单 FK 的手段）⇒ 再 `create` 撞唯一约束、整批响亮 500；**授权给建这条绑定的那个客服**（A15：`binding.bound_by.staff_profile` 在 ⇒ `profile.games.add(game)`），**没有档案**（供给出来的租户管理员按构造就没有）⇒ 跳过授权、⛔ 不建档案——`games_of` 对管理员已短路成"全部游戏"，凭空造档案是多一行假数据。**本函数会写库** ⇒ 调用方必须把注定跳过的形状校验放在它**之前**、把它调用在每条事件的 `transaction.atomic()` **之内**（被拒事件不在配置表留游戏；这几笔写与订单写入同生共死，出题方裁定 2 / E-0022 同族）。"""
    from apps.business.models import Game

    if not isinstance(game_name, str) or not game_name:
        return None

    # ⚠️ **按列宽截断**（出题方裁定 1；先例是本仓 `project_text` 的 **R19**）：超长的对端值不许把整批落单一起回滚（PostgreSQL 会抛 `value too long`），A14 也不许漏单；列宽从**模型元数据**取（走 `_clip_to_column`，⛔ 不在这里抄 `64` 这个数字）。
    #    ⚠️ **代价可见、不是静默**：截断后的名字可能与对端**不同名**；将来要精确同名，得先把 `Game.name` 的列宽问题单独裁定（⛔ 不在这里动模型）。
    game_name = _clip_to_column(game_name, Game, "name")

    # ⛔ 不按 `deleted_at` 过滤：名字仍唯一、软删的那一行照样是"同名的那一款"（复活而非新建）。
    game = Game.objects.filter(name=game_name).first()
    if new_sync and not allowed_upstream_game(binding, game.pk if game is not None else None):
        return None
    if game is None:
        game = Game.objects.create(name=game_name, is_active=True)
    elif game.deleted_at is not None:
        game.deleted_at = None
        game.save(update_fields=["deleted_at"])

    # ⚠️ `getattr`（⛔ 不是 `binding.bound_by.staff_profile`）：反向 OneToOne 缺行时抛的是
    #     `RelatedObjectDoesNotExist`，它是 `AttributeError` 的子类 ⇒ `None` 默认值能兜住，
    #     不需要 `try/except`（口径与 `games_of` 那一处同源）。
    profile = getattr(binding.bound_by, "staff_profile", None)
    if profile is not None:
        profile.games.add(game)
    return game


def _peer_text_or_empty(payload, key) -> str:
    """`remark` / `content` 这类 **TextField 展示字段**的类型闸（⭐ A1）：对端给的值不是字符串（dict / list / 数字 / `True`）⇒ **按「没带该字段」处理**——回空串、`logger.info` 记一条，事件**其余字段照常应用**。
    ⚠️ **为什么不是 `bad_shape` 硬跳过**：那会进 `skipped_details`，发送侧按**业务类失败**退避重投 5 次、终态 `failed`、事件永久丢失——而展示字段本就装得下任何 JSON 值（`TextField` 无列宽），一个畸形值没必要把整条事件顶掉（H-1 的现场：psycopg 类型错 ⇒ 500 ⇒ 发送侧归「连接类失败」⇒ ~5 轮后自动停绑，双向 kill）；也**不进** `skipped_details`（发送侧对非 `stale` 码的处置就是退避重投，一条「注定能应用的事件」不该背这个账）。
    ⚠️ 缺键 / `None` 本来就按空串落库（两支既有的 `or ""` 口径），本函数行为与它**逐字一致**；非 `None` 的畸形值多一条 `logger.info`（排障时能看见对端发过什么），⛔ 不进任何台账表。截图支（`_apply_screenshot`）同口径的 `isinstance` 判是它自己写的——那支不改。"""
    value = payload.get(key)
    if isinstance(value, str):
        return value
    if value is not None:
        logger.info(
            "peer 事件载荷里的 %s 不是字符串（%r），按「没带该字段」处理："
            "该字段不落库，事件其余字段照常应用",
            key, str(value)[:60],
        )
    return ""


def _clip_to_column(value: str, model, field_name: str) -> str:
    """把一段展示用文本裁进 `model` 里 `field_name` 那一列的宽度（裁决 R19；先例 `peer_sync.source_name_for`）。**有意截断、有意不报错**（业务方裁定 R19）：`Order.sales_agent` 是 `CharField(max_length=64)`、装的却是操作人**显示名**（`User.first_name` 上限 150）⇒ 超宽时 PostgreSQL 抛 `value too long`，而整批落单共用一个事务 ⇒ **一个人的长名字会把同批里其它正常的单一起回滚**、上家侧只看到 500 / failed。
    ⚠️ **代价**（有意接受，⛔ 不要说成没有）：显示名长于列宽时，**订单行里的快照是截断后的名字**，而「绑定上家系统」页的 `bound_by_name` 显示**完整**名字 ⇒ 同一个人在页面与订单行里可能长得不一样；另两条路（把列加宽到 150 / 显式拒绝整批）都要动已上线列或让「收到一单」变成业务失败，故取截断。
    ⚠️ 列宽从**模型元数据**取（`_meta.get_field(...).max_length`），⛔ **不抄数字**：抄一份就与模型漂移。口径与 `apps/audit/log.py::_clip` 同一先例。"""
    width = model._meta.get_field(field_name).max_length
    return value if width is None else value[:width]


def _amount_fits_column(value, model, field_name: str) -> bool:
    """`value` 是不是一个**落得进** `model.field_name` 那一列金额列的值（形状判据）。三条缺一条都会漏掉一类 500 或静默改写：① `bool` 先排除——Python 里 `Decimal(True) == 1`，不排除就是把 JSON 的 `true` **静默记成 1**；② `Decimal(value)` 要成功：非数值串抛 `InvalidOperation`、容器抛 `TypeError` / `ValueError` ⇒ 以前都是写库那一步才炸、整批 500；③ **量化到列的 scale 之后**绝对值仍小于 `10 ** (max_digits - decimal_places)`——否则 PostgreSQL 抛 `numeric field overflow`（同样整批 500）。量化这一步是承重的：`9999999999.995` 不量化时"看起来"小于 `10^10`，进位后是 `10^11` 级的值。
    ⚠️ 列宽 / 精度 / scale 全从**模型元数据**取（⛔ 不抄 `12` / `2`）：列改了这条闸跟着改；**只管形状**——金额的 0 与负数都是合法边界（D-0015），⛔ 不在这里做值域判断。⚠️ `ROUND_HALF_UP` 量化是**刻意的保守超集**（只用来判「量化后会不会撑爆 `max_digits`」）：它与 Django `DecimalField` 的 half-even（而 PostgreSQL `numeric` 是**半数远离零**）都可能在恰好半值给出相邻的一分，但两者都落得进这一列 ⇒ 放行的集合仍是「列存得下」的子集、**不会漏放**；真正落库那个值怎么舍入是 Django / DB 的事，⛔ 不是这条检查要复刻的口径（改量化模式会把它变成易漂移的承重逻辑）。"""
    if value is None or isinstance(value, bool):
        return False
    field = model._meta.get_field(field_name)
    try:
        amount = Decimal(value)
    except (ArithmeticError, TypeError, ValueError):
        return False
    if not amount.is_finite():
        return False
    try:
        amount = amount.quantize(
            Decimal(1).scaleb(-field.decimal_places), rounding=ROUND_HALF_UP
        )
    except ArithmeticError:
        return False
    return abs(amount) < Decimal(10) ** (field.max_digits - field.decimal_places)


def _text_fits_column(value, model, field_name: str) -> bool:
    """`value` 是不是一个装得进 `model.field_name` 那一列宽度的**字符串**（⛔ 不截断）。"""
    width = model._meta.get_field(field_name).max_length
    return isinstance(value, str) and (width is None or len(value) <= width)


def _reference_key_shape_error(value, model, field_name: str) -> str | None:
    """**精确查找键**（`target_ref`）的形状判据：非空 `str`、装得进那一列。⚠️ **⛔ 绝不截断查找键**（本闸与 `_clip_to_column` 的分界）：`target_ref` 是 `PeerOrderLink.objects.filter(peer_order_no=…)` / `Order.objects.filter(order_no=…)` 的键，截断后的键可能命中**另一张单**——比 500 更坏的**静默错单**（500 至少看得见、会重投；错单没人看得见，还会把两张单的数据搅在一起）⇒ 超宽一律 `bad_shape` 硬跳过，⛔ 不走「截断」这条路。"""
    if value is None or value == "":
        return "事件缺少 target_ref"
    if not isinstance(value, str):
        return f"target_ref 不是字符串：{str(value)[:60]!r}"
    width = model._meta.get_field(field_name).max_length
    if width is not None and len(value) > width:
        return (
            f"target_ref 超过 {model.__name__}.{field_name} 的列宽（{width}）："
            f"长度 {len(value)}"
        )
    return None


def _local_order_for_peer_ref(ref: str, *, include_deleted: bool = False):
    """把外部给来的**同步身份键**解析成本地订单（全仓唯一的解析口，⛔ 不各写一份）。身份键的语义（业务方裁定）= `jidanpu_member.effective_source_order_no(order)`——**来源单号优先，空 / 纯空白才回落内置号**，所以同时接受两种写法：新口径（`ref` == 本单**来源单号**）与老口径（`ref` == 内置单号 `Order.order_no`，旧版对端仍在发它——上行回传 / `hall.claim` 的兼容面）。
    ⚠️ **两档的优先级是承重的**：`order_no` 精确命中优先，其次才是来源号命中——对端发来一个**可能与本地某单内置号撞值**的来源号时，「老协议按内置号指认」这条既有语义不许被改掉（改掉等于把一张单的回传路由到另一张单上）；**来源号撞键**是计划 §7 已登记的残差，本函数按顺序取**第一张**，⛔ 不发明去重 / 合并规则。预筛只查两列、再用 `effective_source_order_no` 精判（照 `jidanpu_member_openapi` 既有手法）：直接把「来源号 or 内置号」写成 SQL 表达式就是第二份口径，迟早漂移。
    ⚠️ `ref` 已过精确查找键形状闸（非空 `str`），本函数不再重复判形状；`include_deleted=True` 只给**撤单投递前复核**用（`peer_worker._recheck_withdraw`：「单已软删 ⇒ 照发撤单」是 spec §16.2 的一条分支）；接收侧一律走默认 manager（已软删单取不到 ⇒ 按 `unknown_order` 跳过，fail-closed）。"""
    from django.db.models import Q

    from apps.business import jidanpu_member
    from apps.business.models import Order

    # 预筛：覆盖「原样」「两端 strip 后」两种写法（`effective_source_order_no` 只 strip 两端，
    # 所以对端发来的一定是 strip 过的那个值；本地这一列可能是复制粘贴带空白的）。
    candidates = {ref, ref.strip()} if ref.strip() else {ref}
    manager = Order.all_objects if include_deleted else Order.objects
    # ⭐ （业务方拍板）：**镜像卡派生的隐藏单不参与同步身份解析**（两道 exclude 同一把闸）。
    #    为什么必须挡（实测形状，多跳链直接红）：隐藏单的 `source_order_no` 就是那张卡的 `peer_order_no`（业务方口径），而本函数同时接受「来源号」⇒ 中间层会把自己派生的隐藏单当成「下游 claim 指认的原生单」，抢单请求于是不再向上家转发（`test_peer_hall_chain` 的 B 卡停在 `listed`）；⭐ 同一道闸罩到**上家镜像**隐藏单（`upstream_hall_card`）——它的 `source_order_no` 是鸡蛋铺内部数字单号、与 peer 线解析键对拍时可被撞值误解析，而上家镜像隐藏单**永远不是** peer 事件的合法目标（由 upstream 线承载），排它是纯收紧、无误伤面（转正后同样清空 ⇒ 照旧可解析）。
    #    语义上也该挡：外部事件说的是**上家那张单**，隐藏单只是本层为了同步下家大厅派生的影子（它的身份由 `hall_card` 指回的那张卡承载）；转正后 `hall_card` 已清空 ⇒ 它就是一张正常订单，照旧能被解析到。
    rows = list(
        manager.filter(
            Q(order_no__in=candidates) | Q(source_order_no__in=candidates)
        )
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
        .order_by("pk")
    )
    for order in rows:
        if order.order_no == ref:
            return order
    for order in rows:
        if jidanpu_member.effective_source_order_no(order) == ref:
            return order
    return None


def _peer_value_shape_error(raw, payload, *, checked_fields) -> str | None:
    """对端可控、且会落进**有界列**或做**精确查找键**的值的**整类**形状闸（P4 挂账⑨）。回一句人读原因（没问题时回 `None`）——调用方一律走既有的 `note_skip(..., SKIP_BAD_SHAPE, ...)` 出口，⛔ 不另造 code / 不另造跳过路径（E-0010：判据是「**这类**请求还会不会 500」，不是「被点名的那一个字段」）。
    为什么要按**类**收而不是逐字段补：接收端点把**整批**包在一个事务里（每条事件再包一个）⇒ 任何一个对端可控的值让 PostgreSQL 抛非 DRF 异常（`value too long` / `numeric field overflow` / `ValidationError`）都是**整批 500 + 全回滚**，发送侧会一直重投这条毒事件 ⇒ 判据必须落在「值能不能落进它要去的那一列 / 能不能当查找键」这个类上。口径按列分：**精确查找键**（`target_ref`）单独走 `_reference_key_shape_error`（⛔ 绝不截断）；`_MODE_TEXT` = `str` 且装得进列宽（空串合法）；`_MODE_CLIP`（`project_text`）= 已登记的 R19 截断口径（只判 `str`、超宽照旧截断）；`_MODE_AMOUNT` = 落得进 `numeric(max_digits, decimal_places)`。
    ⚠️ 两个边界：**缺键 / `None` = 对端没给这个字段**（建单支的回落与可空列语义都在写路径里）⇒ 不算形状错，其余任何「给了值」的形状都必须对得上——**包括 `""`**（空串不是合法金额）与 `True`（`Decimal(True) == 1`，不拦住就是静默改写）；**`kind` 决定检查哪一组字段**——截图事件的载荷里根本没有订单字段（那些值不落任何列）⇒ 调用方给它传空集，⛔ 不拿一条截图事件不读的字段把它顶掉（上行支同理只查 `target_ref`）；**`order.update` 与建单支共用同一份字段表**——本闸按纪律跑在**读库之前**（读不到链行就分不出支）⇒ 同一批字段两种支都查：宁可让一条「多发了一个畸形字段」的更新事件可见地失败，也不放它过去（它只要换个时刻成为那张单的**首条**事件，同一个值就会真的写进列里）。⚠️ **必须跑在碰任何 DB 之前**：本函数只看 `raw` / `payload` 两个纯数据，既不查库也不建游戏行 ⇒ 被拒的事件**零写入**（与 `_has_unstorable_text` 同一处位置口径）。"""
    from apps.business.models import Order, PeerOrderLink

    ref_error = _reference_key_shape_error(
        raw.get("target_ref"), PeerOrderLink, "peer_order_no"
    )
    if ref_error is not None:
        return ref_error
    if not isinstance(payload, dict):
        return None
    for key, column, mode in checked_fields:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            continue
        if mode == _MODE_CLIP:
            if not isinstance(value, str):
                return f"payload.{key} 不是字符串：{str(value)[:60]!r}"
            continue
        if mode == _MODE_TEXT:
            if not _text_fits_column(value, Order, column):
                width = Order._meta.get_field(column).max_length
                return (
                    f"payload.{key} 不是装得进列宽（{width}）的字符串："
                    f"{str(value)[:60]!r}"
                )
            continue
        if not _amount_fits_column(value, Order, column):
            return f"payload.{key} 不是可落库的金额：{str(value)[:60]!r}"
    return None


# ── 截图广播：两个接收支**共用**的一支 ──────────────────────────
def _negative_hall_price_error(payload) -> str | None:
    """**大厅路** `payload.hall_price` 的值域闸（A7 / L-5 → D-0306 收窄）。负数 ⇒ 回一句人读原因（调用方按 `negative_price_rejected` 硬跳过）；其余 ⇒ `None`。
    ⭐ **D-0306（业务方终裁）把这条闸收窄到大厅路**：**订单流放行负价**（建单 / `order.update` / 接手类 / 撤单）——负价照落 `total_price`（显式 0 落 0、负数照落，D-0015 合法边界），⛔ 不再整条跳过（旧口径对订单事件已下线）；**大厅负价不同步**——大厅镜像行的展示价 / 上家价永不为负 ⇒ 本闸继续用在 ①`apply_events` 的大厅事件分派处、②慢路拉取行、③入向 throw/reprice 的应用兜底；推送路另有入队前闸（`peer_dispatch._enqueue_hall_row`，负价卡不抛 / 负价 reprice 不传播）。
    ⚠️ **只管值域，不管形状**：能不能落进金额列已由 `_peer_value_shape_error` 在更早处判过，这里解析失败（理论上到不了）一律回 `None`——⛔ 不把形状错伪装成值域错（两类跳过的机器码与发送侧处置不同）；**0 不是负数**（D-0015 合法边界照常落库），**不设上限**是登记的残差（A7）。"""
    if not isinstance(payload, dict):
        return None
    value = payload.get("hall_price")
    if not is_negative_amount(value):
        return None
    return f"payload.hall_price 是负数：{str(value)[:60]!r}（大厅镜像行不收负发单价）"


def _has_unstorable_text(value) -> bool:
    """这棵（JSON 解出来的）值里有没有 PostgreSQL **存不下**的字符串（裁决 R-P4-23）。两类，都会让写库的 SQL 抛**非 DRF 异常** ⇒ HTTP 500 + **整批回滚**：含 NUL（`"\\x00"`）抛 `DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`；**孤立代理项**（如 `json.loads('"\\ud800"')` 得到的那个字符）让 psycopg 编码时抛 `UnicodeEncodeError: surrogates not allowed`。
    ⚠️ 判据是 `encode("utf-8", "strict")` **成功**且不含 NUL——前者一句就覆盖整类「Python 能存、utf-8 装不下」的字符（⛔ 别逐字符补白名单）；**递归**走 dict / list：`payload` 里的每一个字符串与顶层字段一视同仁；它**不可能误拒合法值**（PostgreSQL 本来就存不下这些字节，没有「合法但被拒」这一档）。"""
    if isinstance(value, str):
        if "\x00" in value:
            return True
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(
            _has_unstorable_text(k) or _has_unstorable_text(v) for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_unstorable_text(item) for item in value)
    return False


def _reported_ref(value) -> str:
    """跳过明细里回给对端的 `target_ref` 快照：⛔ 不回**存不下**的串。这条不是洁癖：本仓 DRF 的 `JSONRenderer` 是 `ensure_ascii=False`（`UNICODE_JSON=True`），一个孤立代理项进了响应体，编码那一步就会抛 `UnicodeEncodeError` ⇒ 「一条坏事件被跳过」反而变成整批 500。回空串，原因文本已说明问题出在这一条事件上。"""
    return value if isinstance(value, str) and not _has_unstorable_text(value) else ""


def _is_shared_screenshot_key(value) -> bool:
    """`object_key` 的形状闸（裁决 R-P4-16）：非空字符串、归一化后首段 == 共享前缀。⭐ D-0316：形状那一半已统一到 `apps.business.media_access.is_shared_object_key`（**全仓一份**，与发送侧补发、群码字节保留判据同源）；本函数只再加**接收侧自有**的两条：要求**至少两段**（`shared-orders` 这个前缀**本身**不是任何对象名——`build_shared_order_key` 恒产出 `<前缀>/<origin>/<随机>.<ext>`），以及**装得进 `object_key` 那一列**（列宽从模型元数据取，⛔ 不抄 255；超宽的 key 会让 PostgreSQL 抛 `value too long` ⇒ 整批事件一起回滚 + 500，E-0010）。
    ⚠️ 这个值来自对端 JSON ⇒ 必须挡住 `..` 逃逸（由委托的判据负责）；持有判据（`order_screenshot_key_is_held`）就是「本租户有没有一行指着这个对象名」⇒ 放对端随便插 key 等于让它**伪造持有关系**（跨租户读泄漏）。"""
    if not is_shared_object_key(value):
        return False
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if len(parts) < 2:
        return False
    from apps.business.models import OrderScreenshot

    width = OrderScreenshot._meta.get_field("object_key").max_length
    return width is None or len(value) <= width


def _screenshot_uploader(order, binding):
    """这条同步进来的截图行记在**谁**名下（`uploaded_by` NOT NULL + PROTECT）。优先本单的 `sales_user`；为空时回落**这条绑定上的本地用户**（裁决 R-P4-17）——两个绑定模型各有一个**非空**的本地用户列：下家侧 `PeerUpstreamBinding.bound_by`（建这条绑定的客服）、上家侧 `PeerDownstreamBinding.account`（下家拿来绑我的那个账号）⇒ 本函数**绝不回 `None`**（写 `None` 会 `IntegrityError` ⇒ 500）。
    ⚠️ 回落**逐级 `getattr`**、⛔ 不写成「两边同形」的对称式（`binding.account`）：上游绑定**没有** `account` 那一列，写死它一旦被复用就是 `AttributeError`（Minor 4）。"""
    if order.sales_user is not None:
        return order.sales_user
    return getattr(binding, "bound_by", None) or getattr(binding, "account", None)


def _apply_screenshot(
    order, *, kind, payload, binding, target_ref, note_skip, direction
) -> bool:
    """两个接收支**共用**的截图落地：形状闸 → 幂等建行 / 删行。回 `True` = 算 applied。`add`：**本单**已有同 key 行 ⇒ **不建第二行**（幂等那一跳也算 applied）；`delete`：**本单**没有同 key 行 ⇒ **良性 no-op**，⛔ 不记 `skipped_details`；key 被**本租户另一张单**持有 ⇒ 事件与本地状态不一致 ⇒ `bad_shape` 响亮跳过（Minor 2：⛔ 既不静默 no-op，也⛔ 不误删另一张单的行——R-P4-16 的动机延伸）；形状不对 ⇒ `bad_shape` 硬跳过，⛔ 一行不建。
    ⭐ **D-0316**：`img_type` 收 `0` / `1` 两类。`1`（群聊二维码）落行**之前**先淘汰本单旧的 ready 群码（覆盖语义，**到达顺序**裁决——旧事件晚到会盖掉新图，业务方已确认）；淘汰与建行**同处调用方的批事务**（`peer_views` 的 `transaction.atomic()`）⇒ 建行失败时淘汰一起回滚、旧行仍在。
    ⚠️ **并发双投递**（Minor 3）：`add` 的「查了没有 → 建」之间可能被另一条同 key 投递插进来 ⇒ 那笔 `INSERT` 包在**内层** `transaction.atomic()`（savepoint）里，唯一约束冲突只回滚这一笔、⛔ 不污染调用方整批事务；冲突后同 key 真的有一行 ⇒ 当**良性 already-applied**（⛔ 不记跳过：这不是失败，发送侧不该为它退避重投）。⭐ `direction` ∈ `"inbound"`（下行）/ `"upstream"`（上行）只用于挑动作码与文案方向；**只有真的建了 / 删了行**才写一条日志——幂等的 `add`（同 key 已有）与 no-op 的 `delete`（没有那一行）⛔ 不写：它们正是「同一事件重投」的形状，写第二行日志就是重复记账。"""
    from apps.business.models import OrderScreenshot

    screenshot_action = f"peer.{direction}_screenshot"

    key = payload.get("object_key")
    img_type = payload.get("img_type")
    # ⚠️ `img_type` 必须是**整数 0 或 1**：`isinstance(..., bool)` 那半句是承重的——Python 里 `False == 0`、`True == 1`，只写 `img_type not in (0, 1)` 会把 JSON 的 `false`/`true` 当合法值收下。
    # ⭐ **D-0316 改准**：`img_type=1`（群聊二维码）**放行**——群码随订单同步、对端 add 会覆盖本租户自己那张（业务方明确接受的反向口径，取代旧理由 R-P4-7「放行会让对端把本租户那张删掉」）。
    if (
        not _is_shared_screenshot_key(key)
        or not isinstance(img_type, int)
        or isinstance(img_type, bool)
        or img_type not in (OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_GROUP_QR)
    ):
        note_skip(
            target_ref, SKIP_BAD_SHAPE,
            "截图事件的形状不对（object_key 必须是共享前缀下的非空字符串、"
            "img_type 必须是 0 或 1）",
        )
        return False

    if kind == SCREENSHOT_ADD_KIND:
        # ⚠️ 按 **key**（不是 `order+key`）先查一次，才能把三种情形分开：幂等 / 被别的单占 / 空闲。
        held = OrderScreenshot.objects.filter(object_key=key).first()
        if held is not None:
            if held.order_id == order.pk:
                return True
            note_skip(
                target_ref, SKIP_BAD_SHAPE,
                "这张截图的 object_key 已经被本租户另一张单持有",
            )
            return False
        remark = payload.get("remark")
        # ⭐ D-0316：群码**覆盖语义** —— 淘汰本单旧的 ready 群码（**先淘汰、再建行**）。
        #    ⚠️ 必须在 `create` 之前：`uniq_order_ready_group_qr`（每单最多一张 ready 群码）是**部分唯一索引**，先建行必撞 `IntegrityError`（落到下面的分支 ⇒ 500）；位置在 `create` 的 **savepoint 之外**、调用方的批事务之内 ⇒ 建行失败时淘汰跟着一起回滚（`peer_views` 的 `transaction.atomic()` / T5 用例⑥）。
        #    ⚠️ 共享前缀下的旧字节**不删**（`group_qr.should_delete_object_bytes`）。
        if img_type == OrderScreenshot.IMG_GROUP_QR:
            group_qr.evict_previous_ready_group_qr(order.pk)
        try:
            with transaction.atomic():          # savepoint：并发冲突只回滚这一笔 INSERT
                OrderScreenshot.objects.create(
                    order=order,
                    object_key=key,
                    # ⚠️ 字节已经在共享存储里（对端传上去的）⇒ 本侧直接 `ready`，⛔ 不再传一次字节。
                    #    代价（如实登记）：`size_bytes` / `content_type` / `original_name` 本侧**不知道**
                    #    （载荷只有 key / img_type / remark）⇒ 落默认值（登记的残差）。
                    img_type=img_type,
                    uploaded_by=_screenshot_uploader(order, binding),
                    # ⚠️ 备注走 `_clip_to_column`（列宽 50，对端可控）：超长直写会让整批 500。
                    #    非字符串一律当"没有备注"——它是展示字段，不该让一个畸形值把整条事件顶掉。
                    remark=_clip_to_column(
                        remark if isinstance(remark, str) else "", OrderScreenshot, "remark"
                    ),
                    status=OrderScreenshot.STATUS_READY,
                    ready_at=timezone.now(),
                )
                # ⭐ 开放平台 v2（T8，设计文档 §7.2.1 裁决 3）：进度图变 `ready` 的**三个**写入点之一（同步接收侧）——⚠️ 这一处今天**不走**既有的入队函数（`peer_dispatch.enqueue_screenshot_event` 是 peer 链路的广播）⇒ 开放平台的推送必须在这里**显式**接一句；位置在 `with transaction.atomic():`（savepoint）块**内**、`create()` 之后：库里真有这一行 ready 与推送行同生共死，而上面那条并发 `IntegrityError` 分支**不经过这里**（它 `return` 了）⇒ 幂等的重复事件不会重复推。
                #    ⚠️⭐ `spec=OPENAPI_SPEC` 是显式的（回调线 T1b）：投递引擎的公开入口从 T1b 起**不再有**「默认回上家侧」那一格（漏传 = 当场 `TypeError`）⇒ 本处（上家侧那一族的写入点）显式声明自己是哪一族。
                #    ⚠️⭐ D-0316：下面两句（开放平台 / 打手回调）**只对 `img_type=0`**——它们的语义是「订单进度图」，群码没有那个语义（群码的第三方入口是锁定应用那条独立路径）；⛔ 别因为「群码现在也同步」就把这两句也放开。
                if img_type == OrderScreenshot.IMG_ORDER:
                    from apps.openapi import dispatch as openapi_dispatch
                    from apps.openapi.credentials import OPENAPI_SPEC
                    from apps.takerapi.api import callback_recipients as taker_callback

                    # ⚠️ 两族共用**同一个**时刻对象（同一次动作、两个当事人）。
                    moment = timezone.now()
                    openapi_dispatch.notify_order_image_added(
                        order,
                        spec=OPENAPI_SPEC,
                        object_key=key,
                        img_type=img_type,
                        remark=remark if isinstance(remark, str) else "",
                        occurred_at=moment,
                    )
                    # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 5 行前半）：**紧挨着**上面那句，同一个 savepoint、同一个 `moment`。⛔ 不能漏——这张单的当前接手人（我们这儿的打手）与上家应用是**两个当事人**，各接一句；位置同样在 `create()` 之后、并发 `IntegrityError` 分支**之外** ⇒ 幂等的重复事件不会重复推（与上一句同一条纪律）。
                    taker_callback.notify_order_image_added_for_taker(
                        order,
                        object_key=key,
                        img_type=img_type,
                        remark=remark if isinstance(remark, str) else "",
                        occurred_at=moment,
                    )
        except IntegrityError:
            # ⚠️ 走到这里只可能是同 key 的并发 INSERT（其余列都已过形状闸 / 非空）：
            #    key 现在真的有一行 ⇒ 良性 already-applied。⛔ 一行都没有 = 别的完整性错误
            #    （已预期之外的 ⇒ 让它响亮地 500，⛔ 别伪装成业务跳过）。
            if OrderScreenshot.objects.filter(object_key=key).exists():
                return True
            raise
        # ⭐ E-0282 / B2：peer 回传的截图入库（**savepoint 之后**）⇒ 给该单已建链的①式 member 绑定排一条 `member.assign` 补推（消费侧只补缺图）。
        #    ⚠️ 位置在 savepoint 块**外**：上面「同 key 已有行」的幂等支与并发 `IntegrityError` 支都**不经过这里**（没有新行 ⇒ 边沿不触发），真正新落的那一笔才排；外层整批事务还在 ⇒ 补推行与图行同生共死。函数内 import：照本文件 `queue_price_update` 那三处的同一处置（本模块与 `jidanpu_member` 一族的模块级 import 顺序上互为环）。
        from apps.business import jidanpu_member
        jidanpu_member.queue_pic_repush_for_ready_screenshots(order)
        # ⭐ **真的建了一行**才写日志（上面两条幂等支早已 return ⇒ 重投不重复写）。
        _record_peer_sync(
            binding, action=screenshot_action, order=order, to_value="新增"
        )
        return True

    # `order.screenshot.delete`：只删**本单**的同 key 行。⚠️ 不删对象字节：那是**链上共享**的那一份（删了别人家的行就成了断链）。
    # ⚠️ **已知残差**（本期不修）：本层不存 per-key 时间戳 ⇒ 乱序投递时 `delete` 先到、`add` 后到会把这张图**复活**（各家又建回一行）。
    deleted, _ = OrderScreenshot.objects.filter(order=order, object_key=key).delete()
    if deleted:
        # ⭐ **真的删掉了一行**才写日志（没有那一行 = 良性 no-op ⇒ 不写）。
        _record_peer_sync(
            binding, action=screenshot_action, order=order, to_value="删除"
        )
        return True
    if OrderScreenshot.objects.filter(object_key=key).exists():
        # key 在、但不属于这条事件指认的那张单 ⇒ 不一致，⛔ 不许按它删别人的行。
        note_skip(
            target_ref, SKIP_BAD_SHAPE,
            "这张截图的 object_key 属于本租户另外一张单，不按它删除",
        )
        return False
    return True


# ── 上家改价留痕（spec `2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D2）──
#: 发单价在 `Order.hall_price` / `UpstreamPriceChangeLog` 两处的列精度（`numeric(12,2)`）：从模型元数据取的代价不值得——两处都是本仓硬编码的 `12,2`（spec §D1），而这里是「比较之前先把对端原值折到这个精度」的算法参数，不是列宽声明。
_AMOUNT_QUANT = Decimal("0.01")


def _normalized_amount(value):
    """把一笔对端给的发单价折到**列精度（两位小数，四舍五入）**；无法解析 ⇒ `None`。必须折：`Order.hall_price` 是 `numeric(12,2)`，PostgreSQL 落库时会把 `12.001` 舍成 `12.00`，而载荷里是**原值**——不折就比，会出现「库里没变、留痕却写一行 `12.00 → 12.00`」的噪声（实测的边界；合法对端一般不会发三位小数，但形状闸只判「落得进列」，不判小数位）。
    四舍五入用 `ROUND_HALF_UP`，与 PostgreSQL `numeric` 的舍入口径一致（⛔ 不用 Python 默认的 `ROUND_HALF_EVEN`，那会把 `12.005` 舍成 `12.00`，与库里的 `12.01` 不一样）；解析失败回 `None`（由调用方按「没有这个值」处理）——形状闸在更早的位置已经拒过不可落库的金额，这里只是兜底，⛔ 不把坏值当 0。"""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(_AMOUNT_QUANT, rounding=ROUND_HALF_UP)
    except (ArithmeticError, TypeError, ValueError):
        return None


def _amounts_equal(before, after) -> bool:
    """两笔发单价在**列精度**上是不是同一个值（「同价不写」那条判据的比较口径）。必须**先折到两位小数再比**：`before` 是从库里读出来的 `Decimal("10.00")`，而 `after` 是**对端 JSON 里的原值**（通常是字符串 `"10.00"`，也可能是 int / float / `12.001` 这种多位小数）——直接写 `before == after` 时 `Decimal("10.00") == "10.00"` 恒为 `False` ⇒ 对端每条带价事件（`PEER_ORDER_FIELDS` **恒带** `hall_price`）都会写一行；直接折 `Decimal` 而不折精度，又会把 `12.001` 当成变了（库里舍成 `12.00`，没变）。
    ⚠️ 比较失败（两边折算后有一个 `None`）⇒ **回 `False`（= 变了）**：形状闸已保证落得进金额列，这条只是「不把比较失败当相等」——宁可多留一条痕，也不静默丢掉一次真实变动；两边都为空 ⇒ `True`（都没有价，不算变动），只有一边为空 ⇒ `False`（「首次从无价变有价」要留一行，`before_amount` 为 `null`，spec §6-4）。"""
    before_amount = _normalized_amount(before)
    after_amount = _normalized_amount(after)
    if before_amount is None or after_amount is None:
        return before_amount is None and after_amount is None
    return before_amount == after_amount


def _record_upstream_price_change(
    order, *, binding, before, after, occurred_at, column: str = "hall_price"
) -> None:
    """给一次**团团通↔团团通**的上家改价留一行痕（spec §3-D2；只在本模块被调用）。⭐ **`column`（D-0307）**记的是**这次上家改价动了哪一列**——`order.update` 改价支 ⇒ `"total_price"`（上家改价 = 改「接单客户价」（进账侧），这一支不再动 `hall_price`（打手价由下家自己决定）⇒ 台账记客户价旧→新）；接手类支（`order.assign` / `order.grab` / `order.withdraw`）⇒ 默认 `"hall_price"`（那一支仍按 P3/R10 照抄载荷里的发单价）。⛔ 不按「哪一列更值钱」猜：调用方**显式**传，⛔ 让默认值去兜两种语义；⛔ 鸡蛋铺 / 开放平台 / 闲管家 / 本地上改价**都不写**——本函数只从 `apply_events` 的两个入向分支被调用，判据 14 用源码扫描钉住这条边界。
    ⚠️ **调用点必须在同一个 `transaction.atomic()` 块内、`order.save()` 之后**：状态 / 价格与这条留痕同生共死（⛔ 不留「单改了、没留痕」或反向的半截状态）；所有文本列按**模型元数据**的列宽截断（走既有 `_clip_to_column`，⛔ 不手抄数字）——一条留痕不该因为超宽把整批落单一起回滚；`occurred_at` 取**事件的业务时刻**（外层已解析好的 `occurred_at`；缺失回落 `now()`），⛔ 不用 `order.updated_at`（那一列在 `update_fields` 缺席时根本不刷新，读到的是更早的时刻）。"""
    from apps.business.models import UpstreamPriceChangeLog

    if column not in ("hall_price", "total_price"):
        # 内部调用面的 fail-fast：写错列名等于让「改价留痕」说一句假话。
        raise ValueError(f"未知的改价留痕列：{column!r}")
    before_amount = _normalized_amount(before)
    after_amount = _normalized_amount(after)
    # `after` 原值为空 / 折不成金额 ⇒ 不写（⛔ 不把「这条事件没提价」记成「改成空」）；
    # 同价（在列精度上）⇒ 不写（载荷恒带价，不比较会刷屏）。
    if after is None or after_amount is None or _amounts_equal(before, after):
        return
    game = order.game
    UpstreamPriceChangeLog.objects.create(
        order_no=_clip_to_column(order.order_no, UpstreamPriceChangeLog, "order_no"),
        game_name=_clip_to_column(
            (game.name if game is not None else "") or "",
            UpstreamPriceChangeLog,
            "game_name",
        ),
        game_role_name=_clip_to_column(
            order.game_role_name or "", UpstreamPriceChangeLog, "game_role_name"
        ),
        before_amount=before_amount,
        after_amount=after_amount,
        source_name=_clip_to_column(
            binding.peer_name or "", UpstreamPriceChangeLog, "source_name"
        ),
        note=_clip_to_column(
            "上家改价" if column == "hall_price" else "上家改价（客户价）",
            UpstreamPriceChangeLog,
            "note",
        ),
        occurred_at=occurred_at or timezone.now(),
    )


# ── peer 同步的订单日志（业务方要求补的缺失）───────────────────────
#: 补之前只有撤单一条（`order.peer_withdraw`）有痕，其余同步事件落地后订单详情里什么都看不到。本组落点与纪律：
#: **唯一写出口** = 下面的 `_record_peer_sync()`（一个 `record(...)` 调用点：文案只能由 `apps/audit/actions.py` 的模板渲染，⛔ 调用方不拼串——`record()` 没有 `summary` 形参；`actor_id=None` + 字面 `system=True`，机器来源的 fail-closed）；**只在成功且真的落了本地数据的分支写**（`stale` / `duplicate` / `unknown_order` / `bad_shape` 等跳过不写，幂等重投不写——判据在各自调用点，⛔ 不在这里按「有没有调用过」猜）；撤单**不经过这里**（沿用既有 `order.peer_withdraw`，⛔ 不写第二行——同一件事两行日志会把订单详情读成两笔撤单）；`hall.*` 五条只改大厅镜像卡片（`PeerHallMirror`，不是订单）⇒ 有意不写（没有 `order_no` 可挂，落地事实由镜像行自身 + `PeerSyncEvent` 台账承载，残差）。方向：下行（我方是下家）用 `peer.inbound_*`、文案「上家「X」…」；上行（我方是上家）用 `peer.upstream_*` / `peer.hall_claim`、文案「下家「X」…」；对方可读名从绑定取（上家绑定 = `peer_name`，下家绑定 = 账号显示名、空则域名——⛔ 不新造第二套名字口径，见 `_peer_counterparty_name`）。

#: 已建链的后续事件 kind → 动作码（建单支单独用 `peer.inbound_order`）。
#: ⚠️ 未列出的 kind（两机版本偏斜、但 `raw_status` 过了接受集闸）按 `order.update` 处理：
#: 它落的也是「状态照抄」那一条路（`apply_events` 的 `else` 支），不是第二件事。
_PEER_INBOUND_ACTION_BY_KIND = {
    "order.assign": "peer.inbound_assign",
    "order.grab": "peer.inbound_grab",
    "order.update": "peer.inbound_update",
}

#: ⭐ 案 A（D-0364）：下家的两个「**池内态**」——上家**不可能知道**的本地状态（未分配 / 待抢单都只由下家自己的池子动作产生；协议里没有「把单改回池内」的上行事件）。池内豁免判据的唯一出处（`apply_events` 的两处「状态照抄」写点共用，⛔ 别再各写一份）。
#: ⚠️ 取值是**字符串字面**（与 `peer_sync.DOWNSTREAM_ACCEPTED_STATUSES` 同一口径：`OrderStatus.values` 就是这些串）；不从模型 import 是刻意的——本模块在同步进程里被导入，模型走函数内懒导入（全文件既有纪律）。
PEER_POOL_LOCAL_STATUSES: frozenset[str] = frozenset({"UNASSIGNED", "WAITING_GRAB"})

#: ⭐ 案 A（D-0364）：被池内豁免保住的场合，写进 `peer.inbound_update` 审计行 **reason 列**
#: 的人话（渲染成「（原因：…）」；**本线唯一出处**，用例钉字面值——E-0024）。
#: 入站恒为 `IN_PROGRESS`（豁免只对它开）⇒ 文案里的「代练中」是**定的**，不是拼接。
PEER_INBOUND_STATUS_HELD_NOTE = "状态保持：本单已回池，状态不跟代练中"


def _peer_counterparty_name(binding) -> str:
    """对方（上家 / 下家）的**可读名**（日志正文里的 `{actor_name}`）。`PeerUpstreamBinding`（我方是下家）⇒ `peer_name`（页面上那列「上家系统名称」）；`PeerDownstreamBinding`（我方是上家）⇒ 绑定账号的显示名（`user_display_name`，空串则回落 `peer_base_url`——那两张表没有「名字」列，域名是唯一稳定标识）。
    极少数空值（历史脏数据）⇒ 空串：模板会渲染成「上家「」…」，比抛异常诚实（⛔ 日志不能把业务落库顶掉）。"""
    from apps.business.models import PeerDownstreamBinding

    if isinstance(binding, PeerDownstreamBinding):
        name = user_display_name(binding.account) if binding.account_id else ""
        return name or (binding.peer_base_url or "")
    return (binding.peer_name or "").strip() or (binding.peer_base_url or "")


def _peer_status_label(value) -> str:
    """订单状态码 → 中文 label（唯一出处 `OrderStatus(value).label`）；认不出 ⇒ 原值。⚠️ 用 `OrderStatus` 而不是另抄一份中文：状态文案只有一个出处（模型 `choices`）；查不到（历史脏值 / 空）⇒ 原样回字符串，⛔ 不让一行日志因为标签查不到而 500。"""
    from apps.business.models import OrderStatus

    try:
        return OrderStatus(str(value)).label
    except (KeyError, ValueError):
        return "" if value is None else str(value)


def _record_peer_sync(binding, *, action, order, to_value="", from_value="", reason=""):
    """peer 同步落库成功后写**一条**订单日志（与业务写入同一个事务）。⚠️ 放在 `with transaction.atomic()` 块**内**、所有可能 `set_rollback(True)` / `continue` 的补偿路径**之后**：业务写入回滚时日志一起消失（⛔ 不留「单没落、日志说有」的半截）；⛔ 本函数不做幂等判断——那是调用点的事（见上方纪律）。
    ⭐ 案 B1（D-0364）：`from_value` 装事件**落库前**的本地状态中文 label（只有 `peer.inbound_update` 的模板带状态段）；事件没写状态（R3 / 池内豁免 / 接手类不传）时调用方传空 ⇒ 渲染器把状态段整段剔除（`audit/log.py`），不留空壳。"""
    if binding is None:
        # ⚠️ `_apply_screenshot` 的调用方在「订单已有 `sales_user`」时传 `binding=None`
        #    （见它的 docstring：那种形态用不到绑定）⇒ 拿不到对方可读名，**不写**这条日志，
        #    ⛔ 不许凭空编一个来源名。
        return
    actor = _peer_counterparty_name(binding)
    record(
        log_type=ACTION_LOG_TYPES[action],
        action=action,
        # 机器来源（常驻同步进程落地对端事件）⇒ `actor_id` 留空 + 显式 `system=True`。
        # `actor` 就是对方可读名，模板自带 `{actor_name}` ⇒ 不会再加 `【】` 前缀。
        actor=actor,
        system=True,
        target_kind="order",
        target_name=actor,
        order_no=order.order_no,
        # 「来源单号」按订单填（同步来的单在建单那一刻就写好了这一列）。
        source_order_no=order.source_order_no or "",
        game_name=(order.game.name if order.game_id else "") or "",
        game_role_name=order.game_role_name or "",
        from_value=from_value,
        to_value=to_value,
        reason=reason,
    )


# ── 团团通之间的大厅镜像（D-0303）：下家接收侧 ────────────────────────────────
#: `hall.claim` 被上家拒绝、原因是**订单已不存在于大厅**时写进镜像行 `last_error` 的**人话**（打手/客服在行上直接可见）。⭐ 业务方：这一类是**永久**拒绝 ⇒ 卡**彻底下架**（`gone`），⛔ 不再回 `listed`（回大厅也永远抢不到）。单独常量让用例钉字面值（E-0024）。
CLAIM_REFUSED_NOTE = "上家拒绝：这张单已不在对方大厅（可能已被别人接走或撤下）"
#: M2：claiming 期间上家已摘牌（`gone_at` 有证据）⇒ refused 收口成
#: `gone` 而非 `listed` 时写的原因（这张单**回不了大厅**，别让打手再试）。
CLAIM_REFUSED_AFTER_TAKE_NOTE = (
    "上家拒绝：这张单在接单确认期间已从对方大厅撤下，无法再抢"
)
#: ⭐ Z20：下家拿一个**从未通过 `hall.throw` 送达过它**的单号
#: 来要单 ⇒ 回绝时写进应答行 `reason` 的**人话**（我们自己的台账列；协议载荷仍是空对象）。
#: 单独常量让用例钉字面值（E-0024）。
CLAIM_REFUSED_NOT_SHARED_NOTE = "这张单从未通过大厅投递到该绑定，已回绝这次抢单请求"
#: ⭐ Z8：机器接手撞上数据库完整性约束（同单并发写入 / 唯一约束）
#: ⇒ 回绝时写进应答行 `reason` 的人话（照 `jidanpu_member.machine_claim_order` 的先例：
#: `IntegrityError` 属**可预期的拒绝**，⛔ 不许裸逃成 500）。
CLAIM_REFUSED_CONFLICT_NOTE = "接单时撞上数据冲突（同单并发写入），已回绝这次抢单请求"

#: ⭐ 业务方：`hall.claim_refused` **按原因分因**的判据表（本仓这一族的**唯一**出处）：**永久**（订单已不存在 / 从没共享给这家）⇒ 卡**彻底下架**（`gone`），⛔ 不再回大厅；**可重试**（数据冲突 / 认不出的原因 / 真正的一直没回话）⇒ 卡回 `listed` 等重试。
#: ⚠️ **取值复用上面那几句既有台账人话**（⛔ 不新造一套 reason 词表）：它们随应答载荷的 `reason` 键过线，接收侧拿同一个字符串做判据（发送侧与接收侧只有这一份取值面）。
#: ⚠️ 「网络超时 / 上家没响应」今天**不会**产生 `hall.claim_refused`——那条路由 `peer_hall.reclaim_stuck_claims` 的回收兜底（⭐ D-0335 起 60 秒，原 300；它回的也是 `listed`）⇒ 接收侧对**不带原因**的拒绝一律按**可重试**处理（fail-open 到「还能再抢」，不会误下架真单）。
CLAIM_REFUSED_PERMANENT_REASONS = frozenset({
    CLAIM_REFUSED_NOTE,
    CLAIM_REFUSED_NOT_SHARED_NOTE,
    CLAIM_REFUSED_AFTER_TAKE_NOTE,
})
#: `hall.claim_refused` 应答载荷里**拒绝原因**的键名（发送侧
#: `peer_dispatch.enqueue_hall_claim_reply` 写、接收侧本模块读；判据见上面那张表）。
CLAIM_REFUSED_REASON_KEY = "reason"


# ── P5：转发跳数上限（防互绑成环，share-design §3「环与深度」） ────────────────
#: 转发链允许的最大跳数（源头 = 0，每转发一次 +1）。`hop > 5` 的 `hall.throw` **不建卡、不转发**，只记一条台账。⚠️ 数值是**承重**的：没有它，两个租户互绑一次就能把两家的大厅无限互相镜像、撑爆（share-design §3 原话「这条是承重，不是防御」）。
PEER_HALL_HOP_LIMIT = 5
#: 超限丢弃时的**台账 kind**（`PeerSyncEvent`：`direction="in"` / `status="done"`）。⚠️ 与 `hall.*` 那六个**协议事件**分开命名：本行只是我们自己的决策留痕，⛔ 从不外发（`direction="in"` 不在 `peer_worker.claim_due_events` 的取活面里），也⛔ 不进 `HALL_INBOUND_KINDS`（对端永远不该收到它）。
HOP_LIMIT_LEDGER_KIND = "peer.hall_hop_limit"

# ── 大厅**慢路拉取**（计划 `2026-10-25-peer-hall-pull` §3 / §4）─────────────────
#: 一张卡连续这么久没在本轮完整拉取里出现 ⇒ 判 `gone`（计划 §3 的 10 分钟）。⚠️ 判据是 **`last_seen_at < now - 600s` + 本轮 complete + 本轮没见** 三条同时成立；`last_seen_at IS NULL` 的 legacy 行按「足够陈旧」同档（计划 §11.1，⛔ 不单独豁免）。
HALL_PULL_GONE_AFTER_SECONDS = 600
#: `last_seen_at` 的刷新节流（秒）：拉取每 5 分钟一轮、但别的轮次（重试 / 续传）可能更密；
#: 值没变不写。⚠️ 只节流「仍在大厅」的刷新，**新建 / 复活**照写 `now`。
HALL_PULL_LAST_SEEN_THROTTLE_SECONDS = 90
#: 拉取时一行的四种落地结果（`_hall_mirror_upsert_throw` 的返回值）。⭐ 拉取侧**只在 `created` / `resurrected` 时转发下游**；`refreshed` 不转发（否则每 5 分钟一次事件风暴）；`terminal` = 没改任何状态（promoted / claiming / 复活闸没过 / 并发改走）。⚠️ 推送侧逐字不变：仍按「读回 `listed`」转发。
HALL_UPSERT_CREATED = "created"
HALL_UPSERT_RESURRECTED = "resurrected"
HALL_UPSERT_REFRESHED = "refreshed"
HALL_UPSERT_TERMINAL = "terminal"
HALL_UPSERT_OUTCOMES = (
    HALL_UPSERT_CREATED,
    HALL_UPSERT_RESURRECTED,
    HALL_UPSERT_REFRESHED,
    HALL_UPSERT_TERMINAL,
)


def hop_limit_reason(hop: int) -> str:
    """超限丢弃时写进台账 `reason` 的**人话**（排障的人直接看得懂发生了什么）。⚠️ 上限数字从常量取（⛔ 不把「5」写死在两句文案里：改上限时文案会变成假话）。"""
    return (
        f"这张大厅卡在转发链上已经到了第 {hop} 跳（上限 {PEER_HALL_HOP_LIMIT} 跳，"
        "可能是两家互相绑定成环），已丢弃：不建卡、也不再往下转发"
    )


def _incoming_hop(payload) -> int:
    """上游 `hall.throw` 载荷里的**跳数**（纯控制字段，不是 PII）；缺失 / 坏形状 ⇒ 0。键不存在（单跳 / 老版本对端 / 本租户原生单）⇒ **0**（「源头」的旧口径，行为**逐字不变**）；`bool` 不算整数（`True` 是 `int` 的子类，判成 1 会把一次坏载荷当成第 1 跳）；负数 / 字符串 / `None` / 其它类型 ⇒ **0**，⛔ 不抛异常（`hall.throw` 的毒载荷不许把整批打成 500，同 `_peer_value_shape_error` 那条「这类请求还会不会 500」的纪律）。
    ⚠️ 按 0 兜底**不会**让环检测失效：每个接收方落卡时都会把跳数收成整数、转发时再 +1 ⇒ 链上的计数从这里往后照旧单调递增。"""
    value = payload.get("hop") if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def hop_limit_reprice_reason(hop: int) -> str:
    """⭐ B1：`hall.reprice` 到上限**不再转发**时写进台账的人话。与 `hop_limit_reason`（throw 的「丢弃」）分开：改价**不丢卡、不改状态**——本机的价照常刷新，只是不再往下游转发。⚠️ 上限数字同样从常量取（改上限时文案不许变成假话）。"""
    return (
        f"这张大厅卡的价格改动已经到了第 {hop} 跳（再转发就是第 {hop + 1} 跳，"
        f"上限 {PEER_HALL_HOP_LIMIT} 跳，可能是两家互相绑定成环）："
        "价格已在本机刷新，但没有再往下游转发"
    )


def _record_hop_limit(*, binding, target_ref: str, hop: int, reason: str | None = None) -> None:
    """记一条**跳数到上限**台账（形状照 `peer_hall._record_reclaim`：一张表兼 outbox 与台账）。throw 那一档的语义是**丢弃**（不建卡、不转发）；⭐ B1 的 `hall.reprice` 档是**只刷新本机、不再转发**（卡还在）——两档共用这张表，`reason` 分开写（人话里说清是哪一档）。
    `direction="in"` / `status="done"`：这是我们登记的**已知事实**（截断是决定，不是待办），`peer_worker` 只取 `direction="out"` + `pending` ⇒ 永远不会把它投出去；⛔ 不记 `failed`：截断不是投递失败，不该进失败计数 / 对账重排。`target_ref` = **源头单号**（大厅协议里 `target_ref` 就是卡号，沿链不变）；`payload` 里带 hop 与上限便于排障。"""
    from apps.business.models import PeerSyncEvent

    PeerSyncEvent.objects.create(
        binding_role="upstream",
        binding_id=str(binding.pk),
        direction="in",
        kind=HOP_LIMIT_LEDGER_KIND,
        target_type="hall",
        target_ref=target_ref,
        status=PeerSyncEvent.Status.DONE,
        reason=(reason if reason is not None else hop_limit_reason(hop))[:255],
        payload={"hop": hop, "hop_limit": PEER_HALL_HOP_LIMIT},
        meta={"occurred_at": timezone.now().isoformat()},
        idempotency_key=uuid.uuid4().hex,
    )
    logger.warning(
        "大厅镜像：转发跳数超上限，已终止转发 hop=%s limit=%s ref=%s binding=%s",
        hop, PEER_HALL_HOP_LIMIT, target_ref, binding.pk,
    )


def forward_hall_reprice(row, *, hop, now):
    """把「本机这一跳的改价」转发给 peer 下家（⛔ 不含本地落价 / 落库 / 负价判据）。**抽取范围含跳数上限 / 台账支**（N5 复审阻塞 1）：只抽 `enqueue_hall_mirror_event` 那三行会让百分比入口**没有上限**，互绑环重新无限转发（B1 的 bug 原样复活）。
    **签名必须带 `hop`**，且语义是「**这次改价事件**的跳数」：入向支传 `_incoming_hop(payload)`，百分比入口传**那张卡自己的深度**（`card.hop`）；⛔ 不在函数内回落 `row.hop + 1`——行上的 `hop` 是**卡的深度**（只被 throw 写），环上改价事件可能从很小的跳数重新开始，拿它转发永远到不了上限（B1 现场）。
    `hop + 1 > PEER_HALL_HOP_LIMIT` ⇒ 记一条 `peer.hall_hop_limit` 台账（人话说明「价已在本机刷新、只是不再往下游转发」），⛔ 不外发一条注定被弃的事件；`row is None` ⇒ 良性 no-op；台账的 `binding` / `target_ref` 从 `row` 取：镜像行就是按 `peer_base_url` 建的、与入站 `binding` 同一行，⛔ 不再从别处传一份可能漂移的值。"""
    from apps.business import peer_dispatch

    if row is None:
        return
    if hop + 1 > PEER_HALL_HOP_LIMIT:
        _record_hop_limit(
            binding=row.peer_upstream_binding,
            target_ref=row.peer_order_no,
            hop=hop,
            reason=hop_limit_reprice_reason(hop),
        )
        return
    peer_dispatch.enqueue_hall_mirror_event(
        row, kind=HALL_REPRICE_KIND, occurred_at=now, hop=hop + 1
    )


# ── P4 二次转发（多跳）：认单键 / 中间层转发 / 应答回传 / 交单 ────────────────────
def _hall_card_no(payload, peer_order_no) -> str:
    """本事件对应的那张大厅卡的**认单键**（P4 多跳链身份，share-design §5.3.1 第 6 条）。优先用载荷里的 `origin_order_no`（发送方在「这张单挂着上游链路」时带上它，见 `peer_sync.order_payload` 的 docstring）；缺失 / 空串 / 非字符串 ⇒ **对端单号本身**（= 单跳与原生单的旧口径，**逐字不变**）。多跳必须换键的原因：二次转发沿链**不换卡上的单号**（`peer_dispatch.mirror_hall_payload` 送的是镜像行的 `peer_order_no` = 源头单号），而真单的 `order_no` 是**每一跳自己生成**的 ⇒ A→B→C 时 C 收到 B 推来的 `order.assign`，它的 `target_ref` 是 **B 的单号**，而 C 大厅里那张卡记的是 **A 的号**——拿 `target_ref` 去认卡从第三层起永远认不回来（转正落空 ⇒ 真单建成、卡停在 `claiming`，5 分钟后被回收，打手白等）。
    ⚠️ 形状判据照 `peer_p4_chain` 的裁决 R-P4-9：**非空字符串才用**，其余一律退回对端单号（⛔ 不截断、⛔ 不猜；写库前还有列宽闸，本函数只产出一个**查找键**）。"""
    origin = payload.get("origin_order_no") if isinstance(payload, dict) else None
    if isinstance(origin, str) and origin:
        return origin
    return str(peer_order_no)


def _claimable_mirror_row(target_ref: str):
    """本租户有没有一张**现在还能被要**的镜像行与这个单号对应（多跳中间层那一档）；回**那一行**。判据与大厅读口 / 接单口**同一族**（本模块是上家侧、调用方是下游的令牌）：`peer_order_no == target_ref`（注意这是**卡的源头单号**，`_hall_card_no` 的口径：下游发 claim 时用的就是它在大厅里看到的那个号）；状态只认 `listed` / `claiming`（`gone` / `promoted` 的行**不是**「现在能接的卡」——`promoted` 行早已有了我方订单，它的单号是我方生成的、与这个上游单号无关）；归属绑定**未解绑且启用**（僵尸行不可接，与 `claim_peer_mirror` 的 `NOT_CONFIGURED_MESSAGE` 那一档同源）。
    ⚠️ **有意不判 `hall_sync_enabled`**：那是**接收**开关（关掉只让读口不投影），而这张卡已经在下游手里了——按它拒会让下游那张卡**永远接不了也回不来**（它等的答复永远不会到）；关开关时下架下游的卡是 P5（关开关 / 解绑下架）的事，⛔ 不在这一跳假装做了。同号多行（两家上家的单号撞值）⇒ 取「最近仍在厅」的那一张（`last_seen_at` 倒序 + 主键兜底，与大厅读口的排序口径同源）。"""
    from apps.business.models import PeerHallMirror

    return (
        PeerHallMirror.objects.filter(
            peer_order_no=target_ref,
            status__in=[PeerHallMirror.Status.LISTED, PeerHallMirror.Status.CLAIMING],
            peer_upstream_binding__deleted_at__isnull=True,
            peer_upstream_binding__enabled=True,
        )
        .select_related("peer_upstream_binding", "game")
        .order_by("-last_seen_at", "-pk")
        .first()
    )


def _withdraw_mirror_from_downstreams(
    row, *, except_binding_id=None, occurred_at=None, leave_reason=None
) -> None:
    """把这张卡从**本租户各位下家**的大厅里撤下来（发 `hall.take`，P4 二次转发）。什么时候调（三处，缺一处下游就会留一张**永远接不了**的僵尸卡）：① 本行的卡在上游被摘牌（`hall.take` 的 `listed → gone`）；② 本行开始 `claiming`（本租户打手点了接单**或**另一个下家在要——卡不再在厅）；③ 本行 `→ promoted`（真单已落本租户）。
    ⚠️ `except_binding_id` 是**承重**的：被**这一家**下家要走的卡不能撤给它自己——它那张卡此刻正 `claiming`，收到 take 会写下 `gone_at` 证据，于是随后的 `hall.claim_refused` 会把它收口成 `gone`（而「手慢了」那一档应该是回 `listed` 可再抢，见 `_apply_hall_event` 里 M2 那一段）。"""
    from apps.business import peer_dispatch
    from apps.business.models import PeerDownstreamBinding

    bindings = PeerDownstreamBinding.objects.all()
    if except_binding_id is not None:
        bindings = bindings.exclude(pk=except_binding_id)
    peer_dispatch.enqueue_hall_mirror_event(
        row,
        kind=HALL_TAKE_KIND,
        bindings=bindings,
        occurred_at=occurred_at,
        # 上游带因 take 沿转发链透传（多跳链式收单）；裸 take 照旧不带。
        meta_extra={"leave_reason": leave_reason} if leave_reason else None,
    )


def _forward_mirror_claim(binding, *, row, target_ref) -> bool:
    """**多跳中间层**：把下一跳的 `hall.claim` 逐跳转发给本行自己的上家（share-design §5.3.1 第 1/2 条）。`binding` 是**来要的那一家下家**（命中令牌的 `PeerDownstreamBinding`），`row` 是本租户那张**镜像行**（从它自己的上家镜像来的卡）。一次转发做三件事，**同一个事务**（与 `_apply_upstream_hall_claim` 的原生那一半同一条「事件与业务动作同生共死」纪律）：① `listed → claiming` **条件更新**（`rowcount` 是唯一判据：读到落写之间可能被本租户打手抢走 / 被上游推成 `gone` / 被另一个下家要）——写 `claim_downstream`（是谁在要，交单时要交给它的 `account`）、`claim_id`（本层转发的标识）、`claiming_at`（卡死回收按它判年龄）；⛔ **不写 `claiming_by`**：那一列是「本租户大厅里点单的人」，多跳这一档点单的人在**下游租户**里，本租户根本没有这个人（写下游账号会把「过账单该交给下一跳绑定账号」搅成两套）；② 用**这一行自己的**上家绑定向它的上家排一条 `hall.claim`（复用 `peer_dispatch.enqueue_hall_claim`，⭐ 同一条 outbox、同一个事件形状，⛔ 不新造投递）；③ 把这张卡从**其它**下家的大厅里撤下来（`_withdraw_mirror_from_downstreams`，`except` 本次发起方）。应答**不在这里回**（share-design §5.3：等上游判决）——上游的 `hall.claim_ok` / `hall.claim_refused` 会作为**下行事件**回到本层，由 `_apply_hall_event` 转给 `claim_downstream`（那就是「已受理 / 被拒绝」到达下游的时刻）。
    ⭐ **Z20**：进门第一件事也核**来源**——这张卡必须**真的通过 `hall.throw` 投递到过来要的这条下家绑定**（`_hall_throw_delivered`：`status=done` ＋ 空 `reason`；镜像卡往下转发同样是 `kind="hall.throw"` / `direction="out"` / `binding_role="downstream"`、`target_ref` = 卡的源头单号 ⇒ 与原生那一半**同一个判据**）。没有 ⇒ 回 `hall.claim_refused` + 同一条人话、`return True`；⛔ **不建 claiming、⛔ 不向上游转发、⛔ 不撤别家的卡**——没有这道判据时，任何持有绑定令牌的下家都能**猜一张卡号**、把一张从没推给它的卡要走（它照样会被逐跳转发到源头，源头只认「这个号在不在厅」，不认「有没有共享给这一家」）。
    占不到这一行时（`rowcount == 0`）的两个答复：**就是这一家**在要（`claim_downstream` 指着它）⇒ 上一次请求还没收口（它那边超时重投了 / 上一批事件还没投完）⇒ 回 `hall.claim_ok`（**这张卡正为你在受理**），⛔ 不往上游再打一次——源头会以「已不在厅」答复，把这一跳的判决搞乱；其余（本租户打手刚抢走 / 另一个下家在要 / 上游刚把它摘牌）⇒ 回 `hall.claim_refused`（⛔ 不泄漏是被谁拿走的）。⚠️ 回 `True`（= applied）的口径与原生那一半一致：请求被**处理**了（要么已转发、要么已回绝），⛔ 不把「没抢到」记成 skip——那会让下游按失败重投一条注定失败的请求。"""
    from django.utils import timezone

    from apps.accounts.display import user_display_name
    from apps.business import peer_dispatch
    from apps.business.models import PeerHallMirror
    from apps.business.peer_sync import HALL_CLAIM_OK_KIND, HALL_CLAIM_REFUSED_KIND

    # ⭐ Z20：来源校验（与原生那一半同一判据、同一句人话）。位置在**任何写入之前**：
    #    没共享给这一家的请求，一个 claiming / 一条转发出站都不许留下。
    if not _hall_throw_delivered(binding, target_ref=target_ref):
        _refuse_hall_claim(
            binding, target_ref=target_ref, note=CLAIM_REFUSED_NOT_SHARED_NOTE
        )
        return True

    now = timezone.now()
    claim_id = uuid.uuid4().hex
    updated = PeerHallMirror.objects.filter(
        pk=row.pk, status=PeerHallMirror.Status.LISTED
    ).update(
        status=PeerHallMirror.Status.CLAIMING,
        claim_downstream=binding,
        claim_id=claim_id,
        claiming_at=now,
        last_error="",
    )
    if not updated:
        row.refresh_from_db(fields=["status", "claim_downstream"])
        if (
            row.status == PeerHallMirror.Status.CLAIMING
            and row.claim_downstream_id == binding.pk
        ):
            peer_dispatch.enqueue_hall_claim_reply(
                binding, kind=HALL_CLAIM_OK_KIND, target_ref=target_ref
            )
        else:
            # ⭐ 这张卡已经被别人拿走了（本租户打手 / 另一个下家 / 上游刚摘牌）
            #    = 「订单已不存在于大厅」⇒ **永久**拒绝（下游那张卡彻底下架）。
            _refuse_hall_claim(
                binding, target_ref=target_ref, note=CLAIM_REFUSED_NOTE
            )
        return True

    with transaction.atomic():
        # 这一行自己的上家绑定（⛔ 不是本次请求的 `binding`：那是**下家**那一侧，
        # 拿它去找上家会带着错的令牌去错的地址）。
        peer_dispatch.enqueue_hall_claim(
            row, claim_by=user_display_name(binding.account)
        )
        _withdraw_mirror_from_downstreams(
            row, except_binding_id=binding.pk, occurred_at=now
        )
    return True


def _landing_hall_price(order, *, incoming, percent_at_sync=None):
    """事件载荷里的「上家发单价」→ 本地下家这张单该**落**的价（P1 百分比 / P4 多跳的唯一落点）。**普通 peer 单**（这张单不是任何镜像行的 `promoted_order`）⇒ **原样返回 `incoming`**（行为逐字不变，`PEER_FIELDS` 的白名单价是什么就落什么）；**已转正的镜像单**（`promoted_order` 指向这张单）⇒ 上家价先写回镜像行的 `remote_hall_price`（它是那张卡的价格事实），再按 `peer_hall.display_price` 算**本层展示价**（上家价 × 本绑定百分比 / 100）。
    为什么必须有这一支（实测形状）：转正那一刻 `_promote_claiming_mirror` 已经把价改写成展示价（90% ⇒ 45），而**随后**投递的那条 `order.update` / `order.assign` 载荷里带的是**上家原价**（45 的上游是 50）⇒ 照旧口径会把 45 悄悄改回 50：打手按 45 的卡点进来，正式单却按 50 结算，百分比白设。⚠️ 这里**不新算一份公式**：展示价的唯一算法出处仍是 `peer_hall.display_price`（读口 / 抢单闸 / 转正 / 本处同源）。
    ⚠️ 上家**在指派到达前改价**时（spec §6 待拍板①「claim→assign 窗口不锁价」）：新价随这条事件的载荷到、`remote_hall_price` 跟着更新，展示价按**新价 × 本层百分比**重算 ⇒ 与本线「不锁价」的既有口径一致。纯读一次 + 可能一次 `UPDATE`（都在这条事件自己的事务里）；`incoming is None`（载荷没带价）⇒ 回 `None`（调用方据此**不动**这一列，E-0021）。"""
    from apps.business import peer_hall
    from apps.business.models import PeerHallMirror

    if incoming is None:
        return None
    row = (
        PeerHallMirror.objects.filter(
            promoted_order_id=order.pk, status=PeerHallMirror.Status.PROMOTED
        )
        .select_related("peer_upstream_binding")
        .first()
    )
    if row is None:
        return incoming if percent_at_sync is None else peer_hall.price_at_percent(incoming, percent_at_sync)
    if not _amounts_equal(row.remote_hall_price, incoming):
        # 镜像行 `promoted` 是终态、但这**不是**改它的状态：只是把「上家这一刻的价」记
        # 回价格事实那一列（与 `hall.reprice` 对 `listed` 行的写法同一条语义）。
        PeerHallMirror.objects.filter(pk=row.pk).update(remote_hall_price=incoming)
        row.remote_hall_price = incoming
    price = peer_hall.display_price(row)
    return price if price is not None else incoming


def _apply_peer_order_fields(order, *, status, fields, event_at=None):
    """把「peer 落单」的字段应用到一张**已存在**的订单上（⭐ 隐藏单转正复用）。镜像卡派生的**隐藏单**（`Order.hall_card`，业务方拍板）在本租户打手抢走那张卡、卡转正时，**同一张单**要变成我方正式订单——不能新建第二张（会重复建单）；建单支的字段与状态必须与「新建一张」**逐字同形**，所以那份 `fields` dict 与 `status` 表达式由调用方（`apply_events` 建单支）**同一个**变量传进来，⛔ 本函数不另算一份。
    ⚠️ **状态写在这里**（`order.status = status`）：`status` 是 `apply_events` 的 `new_status`，能写出什么状态由那条事件自己的 `DOWNSTREAM_ACCEPTED_STATUSES` 闸回答（结构性网 `tests/test_order_time_fields.py` 把本落点登记成 `gated`）；`updated_at` 是 `auto_now` ⇒ 必须显式进 `update_fields`，否则这一次改动的「最后修改时间」不动。"""
    from apps.business.models import Order
    order = Order.all_objects.select_for_update().get(pk=order.pk)
    previous_status = order.status
    for name, value in fields.items():
        setattr(order, name, value)
    order.status = status
    from apps.wecom.notifications import prepare_order_status
    update_fields = [*fields.keys(), "status", "updated_at"]
    if prepare_order_status(order, previous_status, event_at=event_at, sales_changed=True):
        update_fields.extend(['wecom_notice_journal', 'wecom_notice_pending'])
    order.save(update_fields=update_fields)
    return order


def _confirm_promoted_order_chat(binding, card_no, order, chat_invite) -> None:
    if chat_invite is None:
        return
    from apps.business.models import PeerHallMirror
    from apps.internal_chat.order_groups import confirm_claim

    promoted = PeerHallMirror.objects.filter(
        peer_upstream_binding=binding, peer_order_no=card_no,
        status=PeerHallMirror.Status.PROMOTED, promoted_order=order,
        claiming_by__isnull=False,
    ).select_related("claiming_by").first()
    if promoted is not None:
        confirm_claim(order, binding, promoted.claiming_by, chat_invite)


def _promote_claiming_mirror(binding, card_no, order, *, chat_invite=None) -> None:
    """抢单确认制的**转正收口**（spec §4.3）：`claiming → promoted` + 回填 `promoted_order`。⭐ **转正单必须挂抢单打手，并落「代练中」**（D-0303 的放大器，业务方口径、登记进 spec §4.3 与 DECISIONS D-0303 行）：`claiming_by` 是「谁抢的」这个事实的唯一持有人，而订单落库支（peer 既有路径）建的是**无主**单 ⇒ 不挂的话打手在他自己的「我的单」里看不到它、解冻闸（`wallet._has_active_work`）也看不到这笔占用；做法照鸡蛋铺线 promote 的既有语义（`create_performance_for_order` 补 MAIN 绩效行连带客服行 + `set_taker` 归属写成 `claiming_by`）；**状态落 `IN_PROGRESS`** 由两个调用点所在的支负责（建单支 / 更新支各自带 `promoting_claim` / `promoted_hall` 判据，见 `apply_events` 里两处注释——单在本函数里再改一次状态会与 P4 的落点打架，⛔ 不在这里写第二份）。⭐ **转正时把订单价改写成本层展示价**（P1 的价格百分比，`peer_hall.display_price`）：打手拿到的钱必须等于它在**大厅里看到的那个价**（上家发单价 × 本绑定百分比 / 100）；⛔ 不是顺着 `order.assign` 载荷里的上家原价落库——那会让 90% 那一档的大厅价与正式单的价**差一成**（打手照大厅价点进来、结算按上家价算）；⚠️ 写价排在建绩效行**之前**（`create_performance_for_order` 的 `temp_amount` 取的就是 `order.hall_price`，顺序反了会先按上家价建一行再改）；判据同读口/抢单闸：**同一个 `display_price`**；无价（上家没给价）⇒ 不动 `order.hall_price`（`display_price` 回 `None`，⛔ 不补 0）。
    ⭐ **本租户打手那一档还要录到他自己绑定的下家鸡蛋铺**（生产缺口，真机实测）：抢单升格走的是本函数（⛔ 不是后台指派的 `order_writes.assign_order`，那里本来就排了 `member.assign`），所以这里必须**同事务**补排一条（`jidanpu_member.queue_assign`，收件人 = `claiming_by`）；没有 / 停用绑定时它自己回 0（既有口径，⛔ 不报错）；**多跳那一档（`claim_downstream` 非空）⛔ 不排**：那张单在本层只是过账单、最终做单在下游（接单人 = 下一跳绑定账号），给本层打手录单是错的（见下面的交单对象表）。⚠️ **只吞 `MissingPriceError`**（照同一条 F2 窄口径）：两边都没价（上家发单价空）⇒ 归属与 `claimed_at` 已经写下去（`set_taker` 先落人后算钱）、`taker_price` 保持 NULL，事后填价照常重算；**别的金额 / 提成错原样上抛** ⇒ 整条事件回滚重投。⚠️ **两个调用点**（建单支与更新支的各自末尾，都还在事件自己的 `atomic()` 里）：同一批投递里 `order.update`（建单支，状态照抄）可能先于 `order.assign` 到达——只挂在建单支会漏掉「update 先建单」那一条路，只挂更新支则漏掉最普通的 assign。⚠️ **条件更新只认 `claiming`**：普通 peer 单（没有镜像行 / 行已终态）命中 0 行、零副作用；`promoted` 永不二次回填（复活 = 重复接单的口子，D-0300 的另一半）。
    ⭐ **P4：`card_no`（认单键）**是**认单用的单号**（= 大厅里那张卡的号）：调用方传 `_hall_card_no(payload, target_ref)`——多跳时是**源头单号**，单跳时就是 `target_ref`（行为逐字不变）；⛔ 不在这里自己从 `payload` 取：本函数的两个调用点用的是**同一份**认单键，分头取就是两份口径（价格 / 状态那几处也会跟着漂）。交单对象按**哪一列非空**分两档（两列写点不同 ⇒ 不会同时非空）：本租户打手在大厅里点的（`claiming_by` 非空）⇒ 交给那个打手；**本租户的下家**要的（`claim_downstream` 非空，多跳中间层）⇒ 交给 `claim_downstream.account` **＋必须补排一条 `order.assign`** 给下一跳（`peer_dispatch.enqueue_order_assigned`）——本函数只把本租户这张单落人、落价、落状态，**不会**让下一跳转正：下一跳的钩子只认接手类事件（`order.assign` / `order.grab`），状态漏斗排的是 `order.update` ⇒ 不排的话下游会一直停在「正在受理中」，直到 5 分钟卡死回收；这一对调用点与源头那一跳（`_apply_upstream_hall_claim` 的原生支：`claim_order` + `enqueue_order_assigned`）**逐字同形**，⛔ 不另造一条「专门给链路的推送」。⚠️ `occurred_at` **必须显式给**（默认取 `order.updated_at`，而那条与状态漏斗里那条 `order.update` 同刻 ⇒ 下一跳按「相等即重复」把更全的这条静默丢掉；理由同 `peer_dispatch.enqueue_order_event` 的 docstring）；交完单还要把这张卡从**其它**下家的大厅里撤下来（`_withdraw_mirror_from_downstreams` 的 `except` = 正在要的那一家）：本租户这张卡的转正意味着它已经不在厅里了。"""
    from django.contrib.auth import get_user_model

    from apps.business import jidanpu_member, peer_dispatch, peer_hall
    from apps.business.models import PeerHallMirror, PeerOrderLink
    from apps.business.performance import (
        create_performance_for_order,
        set_taker,
    )
    from apps.business.performance_amount import MissingPriceError

    # ⚠️ 先取行（价格要读它自己的 `remote_hall_price` 与归属绑定的百分比）再条件更新：
    #    更新成功才是“这次转正由我收口”，价格也只在这一支写（⛔ 输家不碰订单价）。
    row = (
        PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url,
            peer_order_no=card_no,
            status=PeerHallMirror.Status.CLAIMING,
        )
        .select_related("peer_upstream_binding", "claim_downstream")
        .order_by("-pk")
        .first()
    )
    if row is None:
        _confirm_promoted_order_chat(binding, card_no, order, chat_invite)
        return
    updated = PeerHallMirror.objects.filter(
        pk=row.pk, status=PeerHallMirror.Status.CLAIMING
    ).update(status=PeerHallMirror.Status.PROMOTED, promoted_order=order)
    if not updated:
        return
    # ⭐ （业务方拍板）：这次转正是不是**用掉了那张隐藏单**？
    #    判据 = 订单的 `hall_card` 就指着这张卡（`apply_events` 建单支复用隐藏单时如此；
    #    手工建的 / 历史镜像行没有隐藏单 ⇒ False，行为逐字照旧）。
    if row.hall_price_percent_at_sync is not None:
        PeerOrderLink.objects.filter(order=order, upstream_binding=binding).update(
            hall_price_percent_at_sync=row.hall_price_percent_at_sync,
        )
    shadowed = order.hall_card_id == row.pk
    # ⭐ 展示价：与大厅读口 / 抢单闸**同一个** `peer_hall.display_price`（唯一算法出处）。
    price = peer_hall.display_price(row)
    if price is not None and order.hall_price != price:
        # ⭐ A6 / M-6：差**大于 0.01** ⇒ 落一行审计（操作日志）。覆盖展示价是**有意**的保护（打手照大厅价结算），但「上家价与本层展示价明显不一致」也可能是钓鱼价差 / claim→assign 窗口改价——那一半必须留痕，⛔ 不许静默吸收（Z18 修复只落了「覆盖」这一半，台账半边是 M-6 的欠账）。
        #    ⚠️ 口径照 `_record_upstream_price_change`：旧值**在赋值之前**拍、`record` 与订单写入同一个事务（同生共死）、`record()` 没有 `summary` 形参 ⇒ 文案由 `apps/audit/actions.py` 的模板渲染（⛔ 调用方不拼串）；差值判据用两位小数金额的**显式比较**（D-0015：不用真值），恰好 1 分的差不记（计划口径「差 > 0.01」逐字）。
        previous_price = order.hall_price
        order.hall_price = price
        # `updated_at` 是 `auto_now` ⇒ 必须进 `update_fields`，否则价改了它不动。
        order.save(update_fields=["hall_price", "updated_at"])
        # ⚠️ 差值比较走 `_normalized_amount`（本文件 `_amounts_equal` 的现成口径）：
        #    建单支的 `order.hall_price` 此刻可能还是**载荷原串**（"50.00"），
        #    直接相减是 `str - Decimal` 的 `TypeError`——折到列精度再比。
        before_amount = _normalized_amount(previous_price)
        after_amount = _normalized_amount(price)
        if (
            before_amount is not None
            and after_amount is not None
            and abs(before_amount - after_amount) > Decimal("0.01")
        ):
            record(
                log_type=ACTION_LOG_TYPES["order.peer_promote_price_override"],
                action="order.peer_promote_price_override",
                # 机器来源（本地没有人在操作）⇒ `actor_id` 留空 + 显式 `system=True`
                # （`record()` 的 fail-closed 判据，与撤单支那条同一条纪律）。
                actor=binding.peer_account,
                system=True,
                target_kind="order",
                order_no=order.order_no,
                field_name="hall_price",
                from_value=str(previous_price),
                to_value=str(price),
                reason="抢单转正覆盖展示价（上家价与本层展示价不一致）",
            )
    if shadowed:
        # ⭐ （业务方拍板）：转正 ⇒ 它**不再是一张隐藏单**，清空 `hall_card`
        #    （此后它出现在订单管理列表 / 统计里）。卡 ↔ 订单的关系改由
        #    `PeerHallMirror.promoted_order` 承载 —— 那一列是这条线既有的方向。
        order.hall_card = None
        order.save(update_fields=["hall_card", "updated_at"])
    claiming_by_id = row.claiming_by_id
    downstream_id = row.claim_downstream_id
    if claiming_by_id is None and downstream_id is None:
        # 两列都空（本列上线**之前**写下的 `claiming` 行，或数据被手改）⇒ 说不出该交给谁。
        # 响亮失败让整条事件重投，⛔ 不静默留一张无主转正单。
        raise ValueError(
            f"镜像行转正后找不到抢单人：{binding.peer_base_url}/{card_no}"
        )
    if claiming_by_id is not None:
        taker = get_user_model().objects.get(pk=claiming_by_id)
    else:
        # ⭐ P4：下一跳在要的那张卡 ⇒ 本租户这张单是**过账单**，接单人 = 下一跳绑定账号
        #    （share-design §5.3.1 第 5 条：按订单价结算、没有提成）。
        taker = row.claim_downstream.account
    main = create_performance_for_order(order)
    if main is None:
        # `create_performance_for_order` 恒建 MAIN 行（它的 None 支只管客服行）；
        # 走到这里说明行没建成（约束被并发破坏）⇒ 响亮失败同上。
        raise ValueError(
            f"转正单建不出 MAIN 绩效行：{binding.peer_base_url}/{card_no}"
        )
    try:
        set_taker(main, taker)
    except MissingPriceError:
        # 窄口径（照 jidanpu 转正支的 F2）：只吞「两边都没价」。
        pass
    if claiming_by_id is not None:
        from apps.internal_chat.order_groups import confirm_claim
        from apps.internal_chat.order_grab_qr_notices import enqueue

        confirm_claim(order, binding, taker, chat_invite)
        enqueue(order, user_id=claiming_by_id, claimed_at=main.claimed_at)
        # ⭐ 本租户打手在**自己大厅**里接了大厅卡、单在这里升格成正式单并挂上他 ⇒ **同事务**给他自己绑定的下家鸡蛋铺录一张单（`queue_assign` ⇒ `member.assign`）。
        #    后台指派走 `order_writes.assign_order`，那里排的正是同一条事件；转正这条路也要排 ⇒ 否则打手自己的鸡蛋铺永远收不到这张单。⚠️ 位置在写价 / `set_taker` / 建绩效行**之后**：`queue_assign` 按订单**此刻**的发单价与接手人录单，先排会录到旧价或空归属。
        #    ⚠️ **多跳那一档（`claim_downstream` 非空）⛔ 不排**：那张单在本层只是过账单、最终做单在下游（接单人 = 下一跳绑定账号）⇒ 给本层打手录单是错的。⚠️ 幂等：本函数只认 `claiming` 行（转正后命中 0 行 ⇒ 不会二次走到这里），即便并发 / 重复投递真的走到底，`queue_member_event` 也会把**在飞**同键（同 kind + 同单 + 同绑定）归并成一条 ⇒ 同一张卡只会有一条 `member.assign`；没有 / 停用绑定时 `queue_assign` 自己回 0（照它的既有口径），⛔ 不报错。
        from apps.business import jidanpu_member

        jidanpu_member.queue_assign(order, taker_id=claiming_by_id)
        if shadowed:
            jidanpu_member.record_promoted_member_claim(
                order, taker, row,
                main.taker_price if main.taker_price is not None else order.hall_price,
            )
            # ⭐ **隐藏单**派生的单转正后，还要把**其他**收到过这张单的店退掉（「撤其他家大厅」；`queue_withdraw` 按 `_push_evidence_ids` 找绑定、并排除接手人自己的店）。普通镜像卡那一档不排（它没有隐藏单期的多店 launch 面）；过滤 / 去重 / 投递判据全部复用既有入口，⛔ 不在这里复制。
            jidanpu_member.queue_withdraw(order, summarize=True)
    # ⚠️ 价已经在上面的 `save()` 里落成展示价 ⇒ 这条 `order.assign` 的载荷带的就是它
    #    （`peer_sync.order_payload` 现取 `order.hall_price`，逐跳相乘少乘一层就出在这里）。
    moment = timezone.now()
    if downstream_id is not None:
        peer_dispatch.enqueue_order_assigned(order, taker, occurred_at=moment)
    # 本租户这张卡离厅（→ promoted）⇒ 其它下家手上的同款卡撤下来（正在要的那一家除外）。
    _withdraw_mirror_from_downstreams(
        row, except_binding_id=downstream_id, occurred_at=moment
    )


def _hall_mirror_upsert_throw(
    *, binding, payload, game, price, target_ref, now, hop,
    pull_mode=False, round_started_at=None, event_occurred_at=None,
) -> str:
    """`hall.throw`：按 `(peer_base_url, peer_order_no)` upsert，终态口径对齐 D-0300。**没有行** ⇒ 新建 `listed`（`game` 由调用方经 `resolve_game` 解析好）；**`gone` 重新见到 ⇒ 自动复活**（条件更新；语义照 `jidanpu_upstream.sync_hall` 行循环那一支：「看见了」就是直接证据，不要求任何轮次确认；复活顺手清 `last_error`）；**`promoted` 永不复活**（已转成我方正式订单，复活 = 重复接单）、**`claiming` 不动**（它的收口在 claim 应答链上，spec §3.2）——两支只碰一次 `last_seen_at`，状态一个字不改；**`listed`** ⇒ 刷新投影（payload / 价 / 游戏 / `last_seen_at`）。
    ⭐ **复活 / 刷新两支把 `peer_upstream_binding` 重挂到当前收事件的这份绑定**（M5）：幂等键是 `(peer_base_url, peer_order_no)`，**解绑重绑同一个域名**会生成一条新绑定行——旧行还挂在已解绑的那一份上 ⇒ 可见与可接的「绑定启用」判据（`peer_hall_queryset` / `claim_peer_mirror`）会让它静默消失；归属锚 =「**当前还在推这份镜像的绑定**」（与鸡蛋铺线 F1「归属锚在事件」同一裁决）；`promoted` / `claiming` 两支**不重挂**（转正单的归属锚是历史事实、claiming 在事件归属那份绑定上收口）。
    ⚠️ `skipped` 在本线没有写入点（模型 docstring 有登记），这里不为它写分支；全部走**条件更新**：读到落写之间被并发改走（例如我方打手刚好发起接单）⇒ 0 行、静默跳过——不报错也不覆盖别人的状态（幂等；重复投递天然无害，spec §3.2）。⭐ **P5 `hop`** = 上游载荷带来的跳数（`_incoming_hop`；缺失 / 坏形状 = 0）：新建、复活、刷新三支都写它（行上的跳数必须是真话——它就是下一次转发时的 +1 基数）；`promoted` / `claiming` 两支**不写**（那两行不往外推，跳数不再被消费）。"""
    from django.db.models import Q

    from apps.business.models import PeerHallMirror

    percent_at_sync = (
        upstream_game_price_percent(binding, game.pk)
        if binding.game_rules is not None else None
    )
    if pull_mode:
        # ⚠️ 拉取与推送可能同时落地同一张卡（计划用例 §9-6「拉取×推送一行」）：
        #    `get_or_create` 自己接住唯一约束竞态（冲突后重查那一行），⛔ 不裸 `create`。
        existing, created = PeerHallMirror.objects.get_or_create(
            peer_base_url=binding.peer_base_url,
            peer_order_no=target_ref,
            defaults={
                "peer_upstream_binding": binding,
                "status": PeerHallMirror.Status.LISTED,
                "game": game,
                "payload": payload,
                "remote_hall_price": price,
                "hall_price_percent_at_sync": percent_at_sync,
                "hop": hop,
                "first_seen_at": now,
                "last_seen_at": now,
            },
        )
        if created:
            return HALL_UPSERT_CREATED
    else:
        existing = PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url, peer_order_no=target_ref
        ).first()
        if existing is None:
            PeerHallMirror.objects.create(
                peer_upstream_binding=binding,
                peer_base_url=binding.peer_base_url,
                peer_order_no=target_ref,
                status=PeerHallMirror.Status.LISTED,
                game=game,
                payload=payload,
                remote_hall_price=price,
                hall_price_percent_at_sync=percent_at_sync,
                # ⭐ P5：把「这张卡是第几跳」落到行上（源头 = 0）。下次往外转发时用它 +1。
                hop=hop,
                first_seen_at=now,
                last_seen_at=now,
            )
            return HALL_UPSERT_CREATED
    # ⭐ 计划 §7：拉取侧写 hop 用 `max(existing, incoming)`（互绑环上 hop 单调不减、
    # 不被一次较浅的拉取重置）；推送侧仍按原值写（语义逐字不变）。
    incoming_hop = max(existing.hop or 0, hop) if pull_mode else hop
    fields = {
        "payload": payload,
        "remote_hall_price": price,
        # ⭐ P5：跳数跟着事件走（同一条卡号被更远的一跳重新抛来时，行上的跳数也要是真话）。
        "hop": incoming_hop,
        # ⭐ M5：归属重挂到当前收事件的绑定（解绑重绑同域名那族）。
        "peer_upstream_binding": binding,
    }
    if pull_mode and (
        existing.last_seen_at is not None
        and (now - existing.last_seen_at).total_seconds()
        < HALL_PULL_LAST_SEEN_THROTTLE_SECONDS
    ):
        # 拉取刷新节流（计划 §3）：同一窗口内重复拉取不重复写 `last_seen_at`。
        # ⚠️ 只节流这一列；载荷 / 价 / 跳数照写（它们本来就是「对端此刻的事实」）。
        fields["last_seen_at"] = existing.last_seen_at
    else:
        fields["last_seen_at"] = now
    if existing.status == PeerHallMirror.Status.GONE:
        if pull_mode:
            # ⭐ 计划 §4 复活闸：`gone_at <= round_started_at` 才允许复活
            # （防「刚被接走 / 刚被撤下、take 已落地，而这一轮是发请求前开始的」把它拉回来）；
            # `gone_at IS NULL` 的 legacy gone 行允许复活（D-0300 同口径）。
            updated = PeerHallMirror.objects.filter(
                pk=existing.pk, status=PeerHallMirror.Status.GONE
            ).filter(
                Q(gone_at__isnull=True) | Q(gone_at__lte=round_started_at)
            ).update(
                status=PeerHallMirror.Status.LISTED,
                gone_at=None,
                game=game,
                last_error="",
                **fields,
            )
            return HALL_UPSERT_RESURRECTED if updated else HALL_UPSERT_TERMINAL
        # ⭐ A3 / M-3：推送支复活**同样要过时效闸**（与拉取支对齐）。事件时刻（`meta.occurred_at`，推送侧 `_enqueue_hall_row` 恒带）**不晚于** `gone_at` ⇒ 这是一次「迟到」的 throw——卡离开大厅之后才投到的旧话，⛔ 不复活（复活会产出一张 `listed` 幽灵卡：抢 → refused → 回 listed → `hall.*` 不参与对账 ⇒ 永久无收口，M-3 现场），只刷 `last_seen_at`（与 `promoted` / `claiming` 两支的「只记还见到过」同一条语义）。
        #    ⚠️ **缺事件时刻 ⇒ fail-open 照旧复活**：老对端不带 `occurred_at`，拿不到时刻就不判新旧（与 D-0300 的「重见即复活」口径一致，兼容不破）。
        event_at = _parse_event_time(event_occurred_at)
        if (
            event_at is not None
            and existing.gone_at is not None
            and event_at <= existing.gone_at
        ):
            PeerHallMirror.objects.filter(pk=existing.pk).update(
                last_seen_at=fields["last_seen_at"]
            )
            return HALL_UPSERT_TERMINAL
        # ⭐ D-0300 口径：`gone` 重新见到即复活——条件更新保住「复活不覆盖并发转正」。
        PeerHallMirror.objects.filter(
            pk=existing.pk, status=PeerHallMirror.Status.GONE
        ).update(
            status=PeerHallMirror.Status.LISTED,
            gone_at=None,
            game=game,
            last_error="",
            **fields,
        )
        return HALL_UPSERT_RESURRECTED
    if existing.status in (
        PeerHallMirror.Status.PROMOTED,
        PeerHallMirror.Status.CLAIMING,
    ):
        # 终态不复活（promoted）/ 抢单流程上不动（claiming）：只记「还见到过」。
        PeerHallMirror.objects.filter(pk=existing.pk).update(
            last_seen_at=fields["last_seen_at"]
        )
        return HALL_UPSERT_TERMINAL
    # `listed`（`skipped` 无写点，到不了这里）：正常刷新投影。
    updated = PeerHallMirror.objects.filter(
        pk=existing.pk, status=PeerHallMirror.Status.LISTED
    ).update(game=game, **fields)
    return HALL_UPSERT_REFRESHED if updated else HALL_UPSERT_TERMINAL


def _land_hall_throw(
    *, binding, payload, target_ref, note_skip, now,
    pull_mode=False, round_started_at=None, event_occurred_at=None,
):
    """`hall.throw` 的门 + 落地（推送与**慢路拉取**共用，⛔ 不各抄一份）。计划 §7 的「拉取侧复用 `_apply_hall_event` 的 `resolve_game` 与 `hop > 5` 闸」就落在这里：两条路走**同一份**跳数闸、同一份游戏解析、同一个 upsert。回 `(applied, outcome, row)`：`applied=False` ⇒ 被形状闸拒（调用方决定要不要记 skip）；`outcome` ∈ `created / resurrected / refreshed / terminal`（`hop` 超限时为 `None`——那是「处理完了、但什么也没落地」）；`row` 只在 `created / resurrected / refreshed` 时回读（`listed` 的那一行）。"""
    from apps.business.models import PeerHallMirror, PeerUpstreamBinding

    # ⭐ P5：**先判跳数**——超限的卡不建、不改、不转发，也不 `resolve_game`（那是写库：被丢弃的事件必须在配置表里零写入，同 bad_shape 的纪律）。
    #    ⚠️ 位置在 `game_name` 形状闸**之前**：跳数超限是「这条链不该再长了」，与载荷里游戏名填得好不好无关；先判它才不会为一条注定丢弃的事件报 bad_shape、让发送侧退避重投一条永远不可能成功的事件。
    hop = _incoming_hop(payload)
    if hop > PEER_HALL_HOP_LIMIT:
        _record_hop_limit(binding=binding, target_ref=target_ref, hop=hop)
        return True, None, None         # 「处理完了」：对端记 done，⛔ 不重投
    game_name = payload.get("game_name")
    if not isinstance(game_name, str) or not game_name:
        note_skip(
            target_ref, SKIP_BAD_SHAPE,
            "大厅事件没有可用的游戏名（缺失 / 空串 / 不是字符串）："
            f"{str(game_name)[:60]!r}",
        )
        return False, None, None
    # ⚠️ `resolve_game` 会写库（建 / 复活游戏 + 授权）⇒ 必须在形状闸**之后**
    #    （被拒的事件不在配置表里留游戏行，与订单落单支同一条「被拒事件零写入」纪律）。
    # ⭐ D-0306 入向兜底闸（防版本偏斜）：**负价载荷按「没带价」处理**——大厅镜像行永不负价。本闸与上面的整条硬跳过**并存**（正常版本根本走不到这里，但两机版本偏斜 / 手工重放仍可能把负价送到这里）；
    #    把它当 `None` 落地 ⇒ 卡片「无价」而不是负价（无价卡在抢单闸走「NULL 接单价不可抢」，与原生无价单同一档）。
    incoming_price = payload.get("hall_price")
    if is_negative_amount(incoming_price):
        incoming_price = None
    # ⭐ （业务方拍板）：**卡落成 `listed` 与它的隐藏单同生共死**（同一个事务）：
    #    建单 / 复活任一失败 ⇒ 这张卡这一次落地一起回滚，不会留下「厅里有卡、下家却收不到」
    #    的半新半旧态。推送支与拉取支都经过本函数 ⇒ 两条路只有一个落点（⛔ 不各抄一份）。
    with transaction.atomic():
        current = PeerUpstreamBinding.objects.select_for_update().get(pk=binding.pk)
        existing = None if current.game_rules is None else PeerHallMirror.objects.filter(
            peer_base_url=current.peer_base_url, peer_order_no=target_ref,
        ).first()
        game = resolve_game(game_name, binding=current, new_sync=existing is None)
        if game is None:
            return True, None, None
        outcome = _hall_mirror_upsert_throw(
            binding=current, payload=payload, game=game,
            price=incoming_price, target_ref=target_ref, now=now,
            hop=hop, pull_mode=pull_mode, round_started_at=round_started_at,
            # ⭐ A3 / M-3：推送支的复活闸要事件时刻（拉取侧不传 ⇒ 闸不参与，走轮次闸）。
            event_occurred_at=event_occurred_at,
        )
        row = None
        if outcome in (
            HALL_UPSERT_CREATED, HALL_UPSERT_RESURRECTED, HALL_UPSERT_REFRESHED
        ):
            # ⚠️ 状态要**读回来判**：`promoted` / `claiming` 那两支 upsert 只碰
            #    `last_seen_at`，不该往外推（理由见 `_apply_hall_event` 的 docstring）。
            row = (
                PeerHallMirror.objects.filter(
                    peer_base_url=binding.peer_base_url, peer_order_no=target_ref,
                    status=PeerHallMirror.Status.LISTED,
                )
                .select_related("peer_upstream_binding", "game")
                .first()
            )
            if row is not None:
                if outcome == HALL_UPSERT_CREATED:
                    from apps.accounts.wxpusher_hall import queue_hall_publication

                    queue_hall_publication("peer", row, listed_at=now)
                # ⭐ 卡在厅里 ⇒ 派生 / 复活隐藏单（`hall_card` = 这张卡），并走既有
                #    `queue_hall_launch` 抛给开启大厅同步的下家鸡蛋铺（过滤只有它那一处）。
                hall_shadow.ensure_for_card(row, now=now)
    return True, outcome, row


def _collect_assigned_hall_orders(binding, *, target_ref) -> None:
    """上家指派离厅（take 带 leave_reason="assigned"）时，把本租户已转正、仍挂本方大厅
    的正式单收回归池；锚 = 镜像行 promoted_order 或上游链行的本地单（排除隐藏单），条件
    = WAITING_GRAB 且 MAIN 行无接手人；预期失败 ⇒ savepoint 回滚、放弃该单（摘牌与 applied 不动）。
    """
    from django.db import transaction

    from apps.business.commission import CommissionError
    from apps.business.models import (
        EmpType,
        Order,
        OrderStatus,
        PeerHallMirror,
        PeerOrderLink,
        Performance,
    )
    from apps.business.order_status import (
        IllegalTransition,
        apply_transition,
        machine_target_nodes,
    )
    from apps.business.performance_amount import AmountError

    mirror_orders = PeerHallMirror.objects.filter(
        peer_base_url=binding.peer_base_url,
        peer_order_no=target_ref,
        status=PeerHallMirror.Status.PROMOTED,
        promoted_order__isnull=False,
    ).values_list("promoted_order_id", flat=True)
    link_orders = (
        PeerOrderLink.objects.filter(
            upstream_binding=binding, peer_order_no=target_ref
        )
        # 镜像卡派生的隐藏单不是同步的合法目标（照 _local_order_for_peer_ref 的两道 exclude）。
        .exclude(order__hall_card__isnull=False)
        .exclude(order__upstream_hall_card__isnull=False)
        .values_list("order_id", flat=True)
    )
    for order_id in dict.fromkeys([*mirror_orders, *link_orders]):
        # ⛔ 默认 manager：已软删的单取不到（照接收口 fail-closed 的同一口径）。
        order = Order.objects.filter(pk=order_id).first()
        if order is None or str(order.status) != str(OrderStatus.WAITING_GRAB):
            continue
        perf = Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN
        ).first()
        if perf is not None and perf.user_id is not None:
            continue
        try:
            with transaction.atomic():
                apply_transition(
                    order,
                    OrderStatus.UNASSIGNED,
                    nodes=machine_target_nodes(OrderStatus.UNASSIGNED),
                    # 外部同步驱动的回拉口径：鸡蛋铺两线 + 群消息不回声；
                    # 大厅镜像 hall.take 照发并带同款 reason ⇒ 多跳链式收单。
                    suppress_outbound_sync=True,
                    hall_leave_reason=HALL_TAKE_LEAVE_ASSIGNED,
                )
                _record_peer_sync(
                    binding,
                    action="peer.inbound_hall_collect",
                    order=order,
                    to_value=_peer_status_label(OrderStatus.UNASSIGNED),
                )
        except (IllegalTransition, CommissionError, AmountError):
            continue


def _apply_hall_event(
    *, kind, payload, binding, target_ref, note_skip, now,
    event_occurred_at=None, hall_leave_reason=None,
) -> bool:
    """下家大厅事件的落地分派（`apply_events` 的一支）。回 `True` = 算 applied。⚠️ **排在任何状态闸之前**（与截图同一位置纪律）：大厅载荷里可能有 `status`（它就是订单白名单），但那**不是**下行订单事件的接受集语义，落到状态闸只会被静默判成 `unknown_status` / `stale`。形状闸：`target_ref` 按 **`PeerHallMirror.peer_order_no`** 的列宽判（非空字符串、⛔ 绝不截断——截断后可能命中另一行，同 `_reference_key_shape_error` 的口径）；`hall.throw` 的 `game_name` 缺失 = `bad_shape` 硬跳过（同 A14 口径：建不出无名游戏）；`hall.take` / 两个应答不消费载荷（`payload` 形状已由调用方闸过）。幂等 / 竞态全部靠**条件更新**（见 `_hall_mirror_upsert_throw`）：重复事件、双事件同窗（take 撞上 assign）都是良性 applied，⛔ 不进 `skipped_details`（那会让发送侧退避重投一条注定不改状态的事件）；`now` 由调用方传（`timezone.now()`，整批一个时刻）：`first_seen_at` / `gone_at` 是事件时刻的本地近似，与截图 `ready_at` 同一条口径。
    ## ⭐ P4 二次转发（多跳）：这一支同时是**中间层**的转发点——本租户这张镜像行**因为收到上游事件而变化**时，要朝**本租户的下家**再广播一条（share-design §4.1 第 2 条：B 的大厅 = 原生待抢单 ∪ 从 A 镜像来且 `listed` 的行）。`hall.throw`（新建 / 复活 / 刷新，落成 `listed`）⇒ 往下推 `hall.throw`（载荷用 `peer_dispatch.mirror_hall_payload`，带**本层展示价** + **源头单号**）；`hall.reprice`（行仍是 `listed`）⇒ 推 `hall.reprice`（⭐ B1：事件跳数 + 1，到上限就**只刷新不转发**，防止互绑成环无限互相改价）；`hall.take`（行 `listed → gone`）⇒ 其它下家的同款卡也一起撤；`hall.claim_ok` / `hall.claim_refused` ⇒ 收口本地 `claiming` 并转给 `claim_downstream`（那才是「下游那张卡的答复」）；⭐ B3：`claiming` 带着 take 证据收口成 `gone` 时，**其它下家**也补一条 `hall.take`（他们手里的 `listed` 卡再也等不到答复）。
    ⚠️ **`claiming` / `promoted` 的行不往下推 throw**：它们不在本租户的大厅里，推出去等于让下家看到我这边看不见的卡。`hall.take` 对 `claiming` 行只写 `gone_at` 证据（M2）⇒ **不往下推**：那一行的收口在 `hall.claim_refused` 上，推 take 会让下游那张 `claiming` 的卡也带上 gone 证据、把「手慢了」那一档收口成 `gone`（回不到 `listed` 就再也没法抢）。"""
    from apps.business import peer_dispatch
    from apps.business.models import PeerHallMirror

    ref_error = _reference_key_shape_error(target_ref, PeerHallMirror, "peer_order_no")
    if ref_error is not None:
        note_skip(target_ref, SKIP_BAD_SHAPE, ref_error)
        return False

    if kind == HALL_TAKE_KIND:
        # `listed → gone` 条件更新；`promoted` 更不该动。行不存在 = 摘牌的终态已成立 ⇒ 良性 applied。
        # ⭐ M2：`claiming` 行**状态不动**（它的收口在 claim 应答上，spec §3.2）但必须**留下「上家已摘牌」的证据**——写 `gone_at`。没有这一条，「claiming 期间上家撤了单 → refused 无条件回 listed」会造出一张**永远抢不到**的僵尸行（上家那张单早没了）。见 `hall.claim_refused` 那一支的判据。
        taken = PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url,
            peer_order_no=target_ref,
            status=PeerHallMirror.Status.LISTED,
        ).update(status=PeerHallMirror.Status.GONE, gone_at=now)
        # 证据支：只写 `gone_at`（恒保留**最早**那一次：`gone_at__isnull=True` 才写），
        # ⛔ 不改状态、不清 claiming——那两件事仍归应答链收口。
        PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url,
            peer_order_no=target_ref,
            status=PeerHallMirror.Status.CLAIMING,
            gone_at__isnull=True,
        ).update(gone_at=now)
        if taken:
            # ⭐ P4：这张卡在本租户的大厅里**真的摘了牌** ⇒ 往下游也撤（多跳状态跟随）。
            #    ⚠️ 只在 `taken`（= `listed → gone` 那一支命中了）时推，理由见 docstring。
            row = (
                PeerHallMirror.objects.filter(
                    peer_base_url=binding.peer_base_url, peer_order_no=target_ref
                )
                .select_related("peer_upstream_binding", "game")
                .first()
            )
            if row is not None:
                _withdraw_mirror_from_downstreams(
                    row, occurred_at=now, leave_reason=hall_leave_reason
                )
                # ⭐ 卡离开大厅 ⇒ 派生的隐藏单跟着退、下家鸡蛋铺撤下副本。
                hall_shadow.refund_for_card(row, now=now)
        if hall_leave_reason == HALL_TAKE_LEAVE_ASSIGNED:
            # 指派离厅 ⇒ 尝试把已转正、仍挂本方大厅的正式单收回归池（条件见收单函数）。
            _collect_assigned_hall_orders(binding, target_ref=target_ref)
        return True

    if kind == HALL_CLAIM_REFUSED_KIND:
        # claiming 的收口（spec §4.3 + ⭐ 业务方**按原因分因**）：拒绝原因是**订单已不存在 / 没共享给这家**（永久）⇒ 卡彻底下架（`gone`）；其余（可重试：数据冲突 / 认不出 / 真正的一直没回话）⇒ 回 `listed` 等重试。`last_error` 给打手 / 客服一条行上可见的原因；没抢过（非 claiming）⇒ 良性。
        # ⚠️ 判据只有一处：`CLAIM_REFUSED_PERMANENT_REASONS`（取值复用本模块那几句既有人话，⛔ 不在这里另写一张词表）；载荷里的 `reason` 与本地台账列**同一句**。⭐ M2 保留：claiming 期间 take 来过（`gone_at` 有证据）⇒ 也收口成 `gone`——「上家已摘牌」与「拒绝原因说单没了」是同一个事实的两条证据链。
        # ⭐ P4：四列（`claiming_by` / `claiming_at` / `claim_downstream` / `claim_id`）**跟着一起清**——这一跳已经收口，留着「是谁在要」就是一句假话（E-0027 同族）；⚠️ 先拍下来「答复要发给谁」：更新会把那一列清空。
        reason = str((payload or {}).get(CLAIM_REFUSED_REASON_KEY) or "").strip()
        claimed = (
            PeerHallMirror.objects.filter(
                peer_base_url=binding.peer_base_url,
                peer_order_no=target_ref,
                status=PeerHallMirror.Status.CLAIMING,
            )
            .select_related("claim_downstream")
            .first()
        )
        take_evidence = claimed is not None and claimed.gone_at is not None
        permanent = reason in CLAIM_REFUSED_PERMANENT_REASONS
        #: ⭐ B3：`gone` 这一支**要记下「真的翻成了」** ⇒ 给其它下家补发 `hall.take`（多跳下游手里那张 `listed` 的卡也得消失：它等的答复永远不会来）。改成**逐行条件更新**（`pk` + 读到的状态 [+ 证据列]）才拿得到「这一次真的翻过去」；⛔ 不按行数反推是谁（批量 `update()` 只回个数）。
        gone_flipped = False
        gone_last_error = (
            CLAIM_REFUSED_AFTER_TAKE_NOTE if take_evidence else (reason or CLAIM_REFUSED_NOTE)
        )
        if claimed is not None and (take_evidence or permanent):
            updates = PeerHallMirror.objects.filter(
                pk=claimed.pk, status=PeerHallMirror.Status.CLAIMING
            )
            if take_evidence:
                # 证据支只认**带着证据**的那一行（避免把一条刚好在这窗口里被重新接住的卡误下架）。
                updates = updates.filter(gone_at__isnull=False)
            updates_kwargs = dict(
                status=PeerHallMirror.Status.GONE,
                claiming_by=None,
                claiming_at=None,
                claim_downstream=None,
                claim_id="",
                last_error=gone_last_error,
            )
            if not take_evidence:
                # 永久拒绝那一档没有 take 事件可留证据 ⇒ 下架时刻就是**这一刻**
                #（`gone_at` 是这条卡「什么时候离开大厅」的唯一事实，⛔ 不许空着）。
                updates_kwargs["gone_at"] = now
            gone_flipped = bool(updates.update(**updates_kwargs))
        # 可重试 / 未知原因：claiming 且**没有**摘牌证据 ⇒ 回大厅（⛔ 有证据的那一行
        # 已被上面翻成 gone，不会再命中这一条）。
        PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url,
            peer_order_no=target_ref,
            status=PeerHallMirror.Status.CLAIMING,
            gone_at__isnull=True,
        ).update(
            status=PeerHallMirror.Status.LISTED,
            claiming_by=None,
            claiming_at=None,
            claim_downstream=None,
            claim_id="",
            last_error=reason or CLAIM_REFUSED_NOTE,
        )
        if gone_flipped and claimed is not None:
            row = (
                PeerHallMirror.objects.filter(pk=claimed.pk)
                .select_related("peer_upstream_binding", "game")
                .first()
            )
            if row is not None:
                # ⚠️ **排除本次发起方**：它在要的那张卡此刻由 `hall.claim_refused` 收口
                #    （下面那一句转发），take 过去会把它写成 gone 证据、把答复的语义搅乱。
                #    其它下家手里的 `listed` 卡则必须一起撤——它们等不到任何答复了。
                _withdraw_mirror_from_downstreams(
                    row,
                    except_binding_id=claimed.claim_downstream_id,
                    occurred_at=now,
                )
                # ⭐ 这一跳把卡彻底下架（`gone`）⇒ 隐藏单退、下家鸡蛋铺撤副本。
                hall_shadow.refund_for_card(row, now=now)
        if claimed is not None and claimed.claim_downstream_id is not None:
            # ⭐ P4：上游拒的是**下一跳**要的那张卡 ⇒ 把这条「被拒绝」连同**原因**原样转给它（它据此把自己那张 `claiming` 收口成 gone / listed，share-design §5.3）。
            # ⚠️ 只有 take 证据、没有原因那一档：转一个**永久**的既有原因，别让下游误以为还能再抢（它那张卡同样永远等不到答复了）。
            forward_reason = reason or (
                CLAIM_REFUSED_AFTER_TAKE_NOTE if gone_flipped else ""
            )
            # ⚠️ 走 `_refuse_hall_claim`（与本地拒绝同一个写口）：它会同时写台账列与载荷里的 `reason`，⛔ 不在这里另拼一份。
            _refuse_hall_claim(
                claimed.claim_downstream,
                target_ref=target_ref,
                note=forward_reason,
            )
        return True

    if kind == HALL_CLAIM_OK_KIND:
        # 成功应答本身不改镜像行：claiming 的收口是随后的 `order.assign`（→ promoted，见 `apply_events` 建单/更新支之后的回填）。它的作用只是让发送侧把这条应答记 `done`（接收了、没有待办），⛔ 不把「还没转正」记成跳过。
        # ⭐ P4：多跳那一档要把「上游已受理」转给**下一跳**（它那张卡也还在等答复）。⚠️ 行可能已经 `promoted` 了（同一批投递里 `order.assign` 可能先到）——`claim_downstream` **只在收口路径上被清**，转正不清 ⇒ 两种状态都能查到。
        row = (
            PeerHallMirror.objects.filter(
                peer_base_url=binding.peer_base_url,
                peer_order_no=target_ref,
                status__in=(
                    PeerHallMirror.Status.CLAIMING,
                    PeerHallMirror.Status.PROMOTED,
                ),
                claim_downstream__isnull=False,
            )
            .select_related("claim_downstream")
            .first()
        )
        if row is not None:
            peer_dispatch.enqueue_hall_claim_reply(
                row.claim_downstream,
                kind=HALL_CLAIM_OK_KIND,
                target_ref=target_ref,
            )
        return True

    if kind == HALL_THROW_KIND:
        applied, _outcome, row = _land_hall_throw(
            binding=binding, payload=payload, target_ref=target_ref,
            note_skip=note_skip, now=now,
            # ⭐ A3 / M-3：推送支的复活闸吃事件时刻（缺了 = 旧对端 ⇒ fail-open）。
            event_occurred_at=event_occurred_at,
        )
        # ⭐ P4：这张卡现在在本租户大厅里（新建 / 复活 / 刷新）⇒ 继续往本租户的下家推。
        #    ⚠️ 推送侧口径**逐字不变**：只要本行最终是 `listed` 就转发（刷新也转发）。
        if row is not None:
            peer_dispatch.enqueue_hall_mirror_event(
                row, kind=HALL_THROW_KIND, occurred_at=now
            )
        return applied

    # `hall.reprice`：只刷新在挂行的价与投影。终态 / 不存在 ⇒ 良性 applied（最终价会随转正订单 / 下一次 throw 的载荷到达，不需要为它保序）。
    # ⭐⭐ B1：**改价也要防环**。A↔B 互绑时一次改价会沿环无限来回转发（两边各收到就转发、价格每次 ×百分比 还会越滚越偏），throw 有 P5 的跳数上限、reprice 原来没有 ⇒ 这里补同一条。判据取 `hop + 1 > PEER_HALL_HOP_LIMIT`（**包含**「入站 hop 已超上限」那一档）：转发出去就是第 `hop + 1` 跳，而接收侧对 `hop > limit` 一律丢弃（同 throw 口径）⇒ 发出去只会多一条注定被弃的事件、白烧一次投递；**不外发**让环上的出站条数恰好 ≤ 上限。
    #    ⚠️ **本地镜像行照常刷新价格**（上面那两句与顺序一字未改）：这一跳的价格事实已经到达，环被截断不该让本机的价停在旧值。转发载荷的 `hop` = **这次改价事件的跳数 + 1**（`mirror_hall_payload(hop=…)`），⛔ 不是本行 `hop + 1`：行上那个数是**卡**的深度（只被 throw 写），环上改价事件可能从很小的跳数重新开始，拿它转发就永远到不了上限（B1 的现场）。截断时记一条 `peer.hall_hop_limit` 台账（`hop_limit_reprice_reason` 的人话；throw 那一档仍用 `hop_limit_reason`）：排障时能区分「卡被丢了」与「只是价不再往下走」。
    hop = _incoming_hop(payload)
    updates: dict = {"payload": payload, "last_seen_at": now}
    # ⭐ D-0306：**负价 reprice 不传播 / 不落价**。正常版本更早的整条硬跳过已经拦住它；这里防版本偏斜：负价按「没带价」处理（镜像行的 `remote_hall_price` 一格不动 ⇒ 展示价永远非负），而且**不再往下游转发**这条改价（转发出去的价来自行上的展示价，转发只会让下游收到一条「价没变」的噪声事件）。
    reprice_price = payload.get("hall_price")
    negative_reprice = is_negative_amount(reprice_price)
    if reprice_price is not None and not negative_reprice:
        updates["remote_hall_price"] = reprice_price
    PeerHallMirror.objects.filter(
        peer_base_url=binding.peer_base_url,
        peer_order_no=target_ref,
        status=PeerHallMirror.Status.LISTED,
    ).update(**updates)
    # ⭐ P4：在挂行改价 ⇒ 往下游也刷新（载荷里的价是**本层展示价**）。
    row = (
        PeerHallMirror.objects.filter(
            peer_base_url=binding.peer_base_url, peer_order_no=target_ref,
            status=PeerHallMirror.Status.LISTED,
        )
        .select_related("peer_upstream_binding", "game")
        .first()
    )
    if row is not None and not negative_reprice:
        # ⭐ N5：本地落价（隐藏单）与「往下游转发」是两件事，判据各自保留：
        #    - 负价闸留在**事件分支**（它说的是「这条入站事件的价是负的」）；
        #    - `hop` = **这次改价事件**的跳数（⛔ 不是行上的卡深度）。
        hall_shadow.sync_hidden_order_price(row, now=now)
        forward_hall_reprice(row, hop=hop, now=now)
    return True


def apply_hall_pull_rows(binding, rows, *, now, round_started_at) -> dict:
    """把**一页拉取结果**落到本租户的大厅镜像（慢路，计划 `2026-10-25-peer-hall-pull`）。回 `{"seen": set[卡号], "applied": int, "failures": int}`：`seen` = 本轮（跨页累计）**真的见到了**的卡号 ⇒ 只有完整 pass 才拿它判消失；`failures` > 0 ⇒ 这一页有行没落地 ⇒ 本轮**不算 complete**（不判 gone、不清标记）；只在 `created / resurrected` 时**转发下游**（`refreshed` 不转发，防每 5 分钟一次事件风暴；`terminal` 什么都不做）。
    ⚠️ **复用** `_land_hall_throw`（同一个跳数闸 / `resolve_game` / upsert），⛔ 不另抄一份；`hop` 由载荷决定（镜像源 = 上行行 hop + 1，原生 = 0），upsert 里取 `max(existing, incoming)`。被跳数闸丢弃的行仍算 `seen`（它确实在厅里），但**不建行、不转发**——下一次完整 pass 不会因为它没行可判而误伤别的东西。"""
    from apps.business import peer_dispatch
    from apps.business.models import Order, PeerHallMirror

    seen: set[str] = set()
    applied = 0
    failures = 0
    for item in rows:
        if not isinstance(item, dict):
            failures += 1
            continue
        ref = item.get("target_ref")
        payload = item.get("payload")
        if (
            not isinstance(ref, str) or not ref
            or _reference_key_shape_error(ref, PeerHallMirror, "peer_order_no") is not None
            or not isinstance(payload, dict)
        ):
            failures += 1
            continue
        # ⭐ A4 / M-5：拉取行的 `hall_price` 也要过**金额形状闸**——推送路在 `_peer_value_shape_error` 早就有这一条，两路不对称正是 M-5 的现场（畸形价让落地写库炸掉 ⇒ 轮轮崩、`hall_pull_next_at` 不推进、判消失永不执行）；⭐ A7 / L-5：同闸加**值域**半边（负价不进镜像行，与事件路同一口径）。
        #    坏行计 `failures` 跳过（本轮不算 complete ⇒ 不判 gone），⛔ 不让一行坏价把整页 / 整轮打崩。
        if payload.get("hall_price") is not None and (
            not _amount_fits_column(payload["hall_price"], Order, "hall_price")
            or _negative_hall_price_error(payload) is not None
        ):
            failures += 1
            continue
        ok, outcome, row = _land_hall_throw(
            binding=binding, payload=payload, target_ref=ref,
            note_skip=lambda *args: None, now=now,
            pull_mode=True, round_started_at=round_started_at,
        )
        if not ok:
            failures += 1
            continue
        seen.add(ref)
        applied += 1
        if outcome in (HALL_UPSERT_CREATED, HALL_UPSERT_RESURRECTED) and row is not None:
            peer_dispatch.enqueue_hall_mirror_event(
                row, kind=HALL_THROW_KIND, occurred_at=now
            )
    return {"seen": seen, "applied": applied, "failures": failures}


def mark_missing_hall_rows_gone(binding, *, seen, now) -> int:
    """**只有完整拉取**才调：把本轮没见到的陈旧 `listed` 卡判 `gone` 并补发 `hall.take`。判据三条同时成立（计划 §3）：① 归属这条上家绑定、状态 `listed`；② `last_seen_at < now - 600s`（`HALL_PULL_GONE_AFTER_SECONDS`；`NULL` 的 legacy 行按「足够陈旧」同档——误判可由下一轮「见到即复活」自愈，计划 §11.1）；③ **本轮完整 pass 里没见到**（`seen` 不含该卡号）。
    ⚠️ 逐行**条件更新**（`pk` + `listed` + 当时的年龄判据）：读到落写之间被抢走 / 转正 / 被别的路径复活 ⇒ 命中 0 行、尊重那一次；也只有真的翻成 `gone` 的行才补发 take。补发 take 照 `peer_hall.unlist_binding_mirrors` 的同款（`enqueue_hall_mirror_event`），否则多跳下家的卡会一直挂着（计划 §3 末句）；**纯本地影响**：只改本租户镜像行 + 排下行事件，⛔ 不向上家发任何请求。"""
    from django.db.models import Q

    from apps.business import peer_dispatch
    from apps.business.models import PeerHallMirror

    cutoff = now - timedelta(seconds=HALL_PULL_GONE_AFTER_SECONDS)
    qs = PeerHallMirror.objects.filter(
        peer_upstream_binding=binding,
        status=PeerHallMirror.Status.LISTED,
    )
    if seen:
        qs = qs.exclude(peer_order_no__in=seen)
    gone = 0
    for row in qs.order_by("pk"):
        updated = PeerHallMirror.objects.filter(
            pk=row.pk, status=PeerHallMirror.Status.LISTED
        ).filter(
            Q(last_seen_at__lt=cutoff) | Q(last_seen_at__isnull=True)
        ).update(status=PeerHallMirror.Status.GONE, gone_at=now)
        if not updated:
            continue
        gone += 1
        row.status = PeerHallMirror.Status.GONE
        row.gone_at = now
        peer_dispatch.enqueue_hall_mirror_event(
            row, kind=HALL_TAKE_KIND, occurred_at=now
        )
        # ⭐ 判消失也是「卡离开大厅」的一支 ⇒ 隐藏单退、下家鸡蛋铺撤副本。
        hall_shadow.refund_for_card(row, now=now)
    return gone


def apply_events(events, *, binding, direct_taker_assignment=False) -> Outcome:
    """把一批事件落到本地（spec §16.2 / §16.3 / §16.5）。`binding` 是命中令牌的 `PeerUpstreamBinding`（下家侧的绑定行）；租户上下文由调用方所在请求承载，⛔ 本函数不接受租户参数（`AGENTS.md` §4）。每条跳过的 `reason` 是人读文本、`code` 是机器契约：发送侧按 `code` 分流（`stale` ⇒ `done`，其余 ⇒ `failed` + 原因），⛔ 不按文本匹配（文本会变，spec §16.5）。
    ⚠️ **游戏名解析不到 ⇒ 自动建同名游戏并把单录进去**（T5 起，spec §16.4 的 A14/A15）：`Game.name` 在租户内由 `uniq_game_name` 约束保证唯一（**软删行也算**）⇒ 同名对不上就 `resolve_game` 建一款 / 复活那一款，并把这款游戏授权给**建这条绑定的那个客服**；⛔ 不再"响亮跳过"（那会漏单）；只有**名字缺失 / 空串 / 不是字符串**仍是 `bad_shape` 硬跳过（"建不出无名游戏"，代价见 `resolve_game` 的 docstring）。
    ⚠️ **`create_performance_for_order` 本期不调用**：来源与销售归属照 A10 落库了，但本期不建 SALES 绩效行 ⇒ 同步进来的单暂时不进客服提成。这是 spec A2（绩效/结算不同步）的边界。"""
    from apps.business import peer_hall
    from apps.business.models import (
        EmpType,
        Order,
        OrderSource,
        OrderStatus,
        PeerHallMirror,
        PeerOrderLink,
        PeerUpstreamBinding,
        Performance,
    )
    from apps.business.order_no import generate_order_no
    from apps.business.order_source import source_snapshot_overrides

    from apps.internal_chat import order_groups as chat_groups

    applied = 0
    skipped = 0
    skipped_details: list[dict] = []

    def note_skip(target_ref: str, code: str, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        skipped_details.append({"target_ref": target_ref, "code": code, "reason": reason})

    pending: deque[tuple[object, IntegrityError | None]] = deque((raw, None) for raw in events)
    while pending:
        raw, link_conflict = pending.popleft()
        # ⚠️ 逐条先归一形状：`events` 是**外部来的数组**，里面可能是字符串 / 数字 / 缺 payload
        # 的 dict ⇒ 直接 `.get` 会 500（E-0010 的"这类请求还会不会 500"）。
        if not isinstance(raw, dict):
            note_skip("", SKIP_BAD_SHAPE, "事件不是对象")
            continue
        # ⭐ 裁决 R-P4-23：**每条事件在碰任何 DB 之前**先过一次"PostgreSQL 存得下吗"的整类判据。它同时覆盖 **P1–P3 就有的**那些字段（`target_ref` / `game_name` / `project_text` / `content` / `customer_phone` / `customer_qq` / `remark` / `withdraw_reason`）：一个 NUL 或孤立代理项今天就能让写库抛非 DRF 异常 ⇒ **整批回滚 + 500**，发送侧会一直重投这条毒事件、把绑定推向自动停绑。⛔ 这不是"某个字段"的补丁，是一条类判据。
        #    ⚠️ 位置在 `target_ref` 形状闸**之前**：target_ref 自己中招时也必须走这条出口（`_reported_ref` 保证不把存不下的串回进响应体）。
        if _has_unstorable_text(raw):
            note_skip(
                _reported_ref(raw.get("target_ref")), SKIP_BAD_SHAPE,
                "事件里有 PostgreSQL 存不下的字符串（NUL 或孤立代理项）",
            )
            continue
        payload = raw.get("payload")
        # ⭐ P4 挂账⑨：**有界列 / 精确查找键**的整类形状闸（口径见 `_peer_value_shape_error`）。位置与 `_has_unstorable_text` 同一处：**碰任何 DB 之前**（既不查链行、也不建游戏行）⇒ 被拒的事件**零写入**。
        #    ⚠️ `kind` 先读一次喂给闸：截图事件的载荷里没有订单字段（那些值不落任何列）⇒ 那一支只查 `target_ref`。
        kind = raw.get("kind")
        shape_error = _peer_value_shape_error(
            raw,
            payload,
            checked_fields=(
                () if kind in SCREENSHOT_KINDS else _DOWNSTREAM_BOUNDED_FIELDS
            ),
        )
        if shape_error is not None:
            note_skip(
                _reported_ref(raw.get("target_ref")), SKIP_BAD_SHAPE, shape_error
            )
            continue
        # 闸已保证 `target_ref` 是**非空字符串**且装得进列宽 ⇒ 下面用到的都是真 `str`
        # （⛔ 不再 `or ""`：那会把非字符串静默换成空串，再被当成"缺少 target_ref"）。
        peer_order_no = raw["target_ref"]
        if not isinstance(payload, dict):
            note_skip(peer_order_no, SKIP_BAD_SHAPE, "payload 不是对象")
            continue
        if kind in chat_groups.EVENT_KINDS:
            error = chat_groups.receive(
                kind, payload, binding=binding, binding_role=chat_groups.UPSTREAM, target_ref=peer_order_no,
                event_key=raw.get("idempotency_key"),
            )
            if error is None:
                applied += 1
            else:
                note_skip(peer_order_no, SKIP_BAD_SHAPE, error)
            continue
        # ⭐ D-0306（业务方 终裁）：**负价闸只留在大厅路**。**大厅事件**（`hall.throw` / `hall.reprice` 的载荷也带 `hall_price`）继续整条硬跳过（`negative_price_rejected`）：大厅镜像行永不负价（与拉取行同一闸对称）；**订单事件**（建单 / `order.update` / 接手类 / 撤单）**放行负价**：负价照落 `total_price`（显式 0 落 0、负数照落，D-0015 的合法边界）——旧口径对订单流的整条跳过已下线（审计 M-2 的另一半由 D-0307 的改价语义收口）。
        #    位置仍与形状闸同一处：碰任何 DB 之前 ⇒ 被拒事件零写入。
        if kind in HALL_INBOUND_KINDS:
            negative_error = _negative_hall_price_error(payload)
            if negative_error is not None:
                note_skip(peer_order_no, SKIP_NEGATIVE_PRICE, negative_error)
                logger.warning(
                    "上下游落单跳过一次大厅事件（对端单号 %s，绑定 %s）：%s",
                    peer_order_no, binding.pk, negative_error,
                )
                continue
        # ⭐ P4：这张事件对应的**大厅卡号**（多跳链上是源头单号；单跳 / 原生单就是
        #    `peer_order_no`，行为逐字不变）。⚠️ 只在这里算一次：转正回填、`promoting_claim`、
        #    `promoted_hall` 三处必须用**同一个**键，分头算就是两份口径（见 `_hall_card_no`）。
        card_no = _hall_card_no(payload, peer_order_no)

        # ⭐ **截图事件走独立的一支**，⛔ 不参与下面任何状态闸（新旧裁决 / 接受集 / 未知状态）。⚠️ 位置是**承重的**：必须排在 `raw_status not in OrderStatus.values` 那个硬跳过**之前**——截图载荷里根本没有 `status` 字段，排在后面会让每条截图事件都被静默判成 `unknown_status`（症状看起来像"对端没发"，根因却在本地）。
        if kind in SCREENSHOT_KINDS:
            shot_link = PeerOrderLink.objects.filter(
                upstream_binding=binding, peer_order_no=peer_order_no
            ).first()
            if shot_link is None:
                # 裁决 R-P4-15.4：这不是良性的——对端认为**我们**持有那张单（同 `order.withdraw`
                # 指向一张本地没有的单），静默吞掉会让发送侧看不见。
                note_skip(
                    peer_order_no, SKIP_UNKNOWN_ORDER, "截图事件指向一张本地没有的订单"
                )
            elif _apply_screenshot(
                shot_link.order, kind=kind, payload=payload, binding=binding,
                target_ref=peer_order_no, note_skip=note_skip,
                direction="inbound",
            ):
                applied += 1
            continue

        # ⭐ D-0303：**团团通大厅镜像事件走独立的一支**，⛔ 不参与下面任何状态闸（新旧裁决 / 接受集 / 未知状态）。⚠️ 位置与截图同一纪律：必须排在 `raw_status not in OrderStatus.values` 那个硬跳过**之前**——大厅载荷就是订单白名单、带着 `status=WAITING_GRAB` 一类值，落到状态闸会被静默判成 `unknown_status` 或误进订单更新支。
        if kind in HALL_INBOUND_KINDS:
            # ⭐ P1 的第一处闸（`PeerUpstreamBinding.hall_sync_enabled`，**默认关**）：关着 ⇒ `hall.*` **整批跳过**——不建行、不改行、**不报错**。⛔ 三个"为什么"：
            #    ① 不 `note_skip`：那会把这条写进 `skipped_details`，发送侧按非 `stale` 码判失败、退避重投，烧到终态 `failed`——而「本租户还没打开接收」不是对端的错，也不该在它的台账上留红字；
            #    ② 不 `applied += 1`：`applied` 的语义是"落到本地了"，这里一个字节都没落；③ 不计入 `skipped`：跳过的语义是"这条事件有问题"，这里是整条功能没开。⇒ 计数两边都不占（响应里 applied/skipped 都比事件条数少），发送侧对「没有本事件明细」的处置本来就是**算成功**（`peer_worker._hard_skip_reason`）。
            if not binding.hall_sync_enabled:
                continue
            if _apply_hall_event(
                kind=kind, payload=payload, binding=binding, target_ref=peer_order_no,
                note_skip=note_skip, now=timezone.now(),
                # ⭐ A3 / M-3：把**事件自己的时刻**（原始串）带给 throw 复活闸——只在闸里解析一次（`_parse_event_time` 认串不认 datetime）；缺失 = 旧对端，闸 fail-open。大厅事件不走下面的 `occurred_at = _parse_event_time(...)`，所以在这里单独取出。
                event_occurred_at=raw.get("occurred_at"),
                # leave_reason 是 take 的「为什么离厅」（wire 顶层键，服务器常量过形状闸）。
                hall_leave_reason=raw.get("leave_reason"),
            ):
                applied += 1
            continue

        occurred_at = _parse_event_time(raw.get("occurred_at"))
        chat_invite = payload.get("chat_group") if kind == "order.assign" and settings.TT_CHAT_ORDER_GROUPS_ENABLED else None
        # ⚠️ 这一查是**读**、放在 `atomic()` 之外：下面所有跳过分支都在任何写之前发生
        # （`AGENTS.md` §5-E / E-0022 —— `atomic()` 是事务不是回滚点，块里 `continue` 会
        #  提交它前面的写入）。事务里只剩写入。
        link = PeerOrderLink.objects.filter(
            upstream_binding=binding, peer_order_no=peer_order_no
        ).first()
        upstream_order_no = payload.get("order_no")
        if not isinstance(upstream_order_no, str) or len(upstream_order_no) > 64:
            upstream_order_no = ""
        ref_conflicts = bool(link and link.upstream_internal_order_no and link.upstream_internal_order_no != upstream_order_no)
        if upstream_order_no and (link is None or link.upstream_internal_order_no != upstream_order_no):
            # 来源单号可编辑或重复。已有对应关系用建单时记录的系统单号确认。
            known_link = PeerOrderLink.objects.filter(
                upstream_binding=binding, upstream_internal_order_no=upstream_order_no,
            ).first()
            if ref_conflicts and known_link is None:
                note_skip(peer_order_no, SKIP_BAD_SHAPE, "该来源单号已对应到另一笔上家订单")
                continue
            if known_link is not None:
                link = known_link
        if link is None:
            # ⭐ 兼容（承重，计划 `2026-10-25-peer-source-identity` §4）：老链路那一列的值是**对端当年的内置号**（新口径前 `target_ref` 就是它）。对端升级后 `target_ref` 变成来源号 ⇒ 先按来源号找不到，再拿**载荷里那个内置号**（`PEER_ORDER_FIELDS` 恒带 `order_no`，逐字就是当年建行时的键）精确兜一次。
            #    ⛔ 不反推 / 不改写老行（`peer_order_no` 保持旧值）——命中后这条链路继续按旧键工作，下一次事件仍走这条兜底；⛔ 也不拿 `payload["source_order_no"]` 当键：那是链身份（会漂），不是查找键。
            legacy_ref = upstream_order_no
            if legacy_ref and legacy_ref != peer_order_no:
                link = PeerOrderLink.objects.filter(
                    upstream_binding=binding, peer_order_no=legacy_ref, upstream_internal_order_no="",
                ).first()

        if link is None and link_conflict is not None:
            raise link_conflict
        if link is not None:
            # 已有单 ⇒ 必须先能裁决新旧。
            if occurred_at is None:
                note_skip(
                    peer_order_no, SKIP_BAD_SHAPE,
                    "这条事件没有可用的 occurred_at（无法与本地已应用到的时刻比较）",
                )
                continue
            # ⚠️ 这个 `<=`（不是 `<`）有**变异证据**：同一条事件重投时 `occurred_at` 逐字相同，判成"应用"会让撤单写下第二条订单日志。把 `<=` 改成 `<` ⇒
            # `tests/test_peer_inbound_update.py::PeerInboundUpdateTests::test_the_same_timestamp_is_a_benign_stale_skip` 与 `tests/test_peer_order_push.py::ReceiveEventTests::test_a_replayed_event_is_stale_and_a_newer_one_updates` 两条都红（KILLED）。
            if chat_invite is None and link.last_applied_at is not None and occurred_at <= link.last_applied_at:
                note_skip(
                    peer_order_no, SKIP_STALE,
                    "事件不晚于本地已应用到的时刻（重复或更旧）",
                )
                continue
            # ⭐ 与上面那条**并列**的第二条 stale 判据（业务方口径「以最后更改的为主」）：上面比的是「上家事件之间」的新旧，这一条比的是「本地动作 vs 上家事件」——本地那次改状态更晚 ⇒ 更旧的上家事件不许把它盖回去。
            #    ⚠️ 同样用 `<=`（相等 = 本地动作不比事件旧 ⇒ 本地赢），⛔ 不是 `<`；判据必须 `is not None` 守卫（E-0021 / 同文件的 `last_applied_at`）：为空 = 这张单从来没被本地改过 ⇒ 本判据**不参与**，既有行为逐字不变；只读不写：跳过时⛔ 不推进 `last_applied_at`（没应用就不算"已应用到的时刻"）。
            if chat_invite is None and link.local_action_at is not None and occurred_at <= link.local_action_at:
                note_skip(
                    peer_order_no, SKIP_STALE,
                    "事件不晚于本地最后一次动作（本地更新）",
                )
                continue
        elif kind == "order.withdraw":
            # ⛔ 不建单（spec §16.3 末表）：退单指向一张我这里没有的单说明上游那条指派没到，
            # 这是真信号，当良性吞掉会让发送侧看不见（它有 `unknown_order` 这个 code 可分流）。
            note_skip(
                peer_order_no, SKIP_UNKNOWN_ORDER, "撤单指向一张本地没有的订单"
            )
            continue

        # ⛔ **不许回落默认态**（spec §10.2 / §12.3 #16）：未知或缺失的状态响亮失败。
        raw_status = payload.get("status")
        if raw_status not in OrderStatus.values:
            # 状态值原样带回（截断）便于对端排障：⛔ 不落库、也不进日志的其它字段。
            reason = f"未知或缺失的订单状态：{str(raw_status)[:60]!r}"
            note_skip(peer_order_no, SKIP_UNKNOWN_STATUS, reason)
            logger.warning(
                "上下游落单跳过一次事件（对端单号 %s，绑定 %s）：%s",
                peer_order_no, binding.pk, reason,
            )
            continue

        # ⭐ A2 / M-2：**没有显式分支的 kind** 走「状态照抄」路，必须过下家接受集（业务方裁定的 4 种）；集外 ⇒ 整条硬跳过。
        #    - 已建链：`assign` / `grab` / `withdraw` / `order.update` 四种 kind 各有显式分支（UNASSIGNED / REFUNDED / R3 的接受集闸）⇒ 不进本闸；其余 kind（两机版本偏斜的未知 kind）集外状态一律拒——这是 M-2 的现场。
        #    - 未建链（建单支）：`assign` / `grab` 显式落 UNASSIGNED（或转正的代练中）⇒ 不进本闸；其余 kind 的**首条**事件（首条 `order.update` 与未知 kind）照抄 `raw_status` ⇒ 同一把闸。⚠️ 位置在任何写之前（下面 `resolve_game` 会建游戏行）⇒ 被拒事件零写入。
        _explicit_kinds = (
            ("order.assign", "order.grab")
            if link is None
            else ("order.assign", "order.grab", "order.withdraw", "order.update")
        )
        if (
            kind not in _explicit_kinds
            and str(raw_status) not in DOWNSTREAM_ACCEPTED_STATUSES
        ):
            reason = (
                f"状态 {str(raw_status)[:60]!r} 不在下家接受集里"
                f"（{sorted(DOWNSTREAM_ACCEPTED_STATUSES)}），"
                f"kind={str(kind)[:60]!r} 不照抄"
            )
            note_skip(peer_order_no, SKIP_STATUS_NOT_ACCEPTED_FOR_KIND, reason)
            logger.warning(
                "上下游落单跳过一次事件（对端单号 %s，绑定 %s）：%s",
                peer_order_no, binding.pk, reason,
            )
            continue

        # 撤单原因只认事件级枚举 ⇒ 翻成逐字中文（⛔ 不把中文当协议传、也不自己编文案）。
        # ⚠️ 这一段与下面的形状闸都排在**任何写之前**：形状不对、注定要跳过的事件不该在库里
        #     留下游戏行（`AGENTS.md` §5-E / E-0022 同族的"被拒事件零写入"）。
        withdraw_reason = None
        if kind == "order.withdraw":
            withdraw_reason = raw.get("withdraw_reason")
            if (
                not isinstance(withdraw_reason, str)
                or withdraw_reason not in WITHDRAW_REASON_TEXT
            ):
                note_skip(
                    peer_order_no, SKIP_BAD_SHAPE,
                    f"未知的撤单原因：{str(withdraw_reason)[:60]!r}",
                )
                continue

        # ⚠️ 这一段只判**名字形状**（缺失 / 空串 / 非字符串 ⇒ `bad_shape` 硬跳过）。
        #    "名字对不上"**不在这里**判——那是 A14 的**自动建游戏**（写），在下面的事务里。
        game_name = payload.get("game_name")
        if not isinstance(game_name, str) or not game_name:
            reason = (
                "事件没有可用的游戏名（缺失 / 空串 / 不是字符串）："
                f"{str(game_name)[:60]!r}"
            )
            note_skip(peer_order_no, SKIP_BAD_SHAPE, reason)
            logger.warning(
                "上下游落单跳过一次事件（对端单号 %s，绑定 %s）：%s",
                peer_order_no, binding.pk, reason,
            )
            continue

        with transaction.atomic():
            if link is not None:
                locked_order = Order.all_objects.select_for_update().filter(pk=link.order_id).first()
                link = PeerOrderLink.objects.select_for_update().filter(pk=link.pk).first()
                if link is None or locked_order is None:
                    note_skip(peer_order_no, SKIP_UNKNOWN_ORDER, "订单或同步关联已不存在")
                    continue
                if link.last_applied_at is not None and occurred_at <= link.last_applied_at:
                    # A newer order update can arrive before the original chat invitation.
                    _confirm_promoted_order_chat(binding, card_no, locked_order, chat_invite)
                    note_skip(peer_order_no, SKIP_STALE, "事件不晚于本地已应用到的时刻（重复或更旧）")
                    continue
                if link.local_action_at is not None and occurred_at <= link.local_action_at:
                    _confirm_promoted_order_chat(binding, card_no, locked_order, chat_invite)
                    note_skip(peer_order_no, SKIP_STALE, "事件不晚于本地最后一次动作（本地更新）")
                    continue
                if locked_order.peer_direct_pending is not None:
                    note_skip(peer_order_no, "direct_assignment_pending", PEER_DIRECT_PENDING_MESSAGE)
                    continue
                if locked_order.status == OrderStatus.IN_PROGRESS and not jidanpu_assignment_lock.lock_order_change(locked_order.pk):
                    note_skip(peer_order_no, "jidanpu_assignment_busy", jidanpu_assignment_lock.ORDER_BUSY)
                    continue
                link.order = locked_order
            # ⭐ 裁决 R21 + A14/A15：游戏按名字对齐（⛔ 不照抄对端 id）；名字对不上就**自动建**同名（超长按列宽截断）游戏、把单录进去，并授权给建这条绑定的那个客服。
            #    ⚠️ **排在事务之内**（出题方裁定 2）：建 / 复活 `Game` 与授权都是**写** ⇒ 与下面的订单写入同生共死，落单失败时不会留下一条没人用的游戏行（E-0022 同族）；`game_name` 已过上面的形状闸 ⇒ 这里 `resolve_game` 一定给行（它的 `None` 分支由 `tests/test_peer_game_autocreate.py` 直接钉住）。
            # ⭐ D-0303：这条事件会不会**完成一次抢单转正**？是 ⇒ 接手类落地**不再是 UNASSIGNED** 而是「代练中」——抢单握手里「谁接」已经落定（claiming 行恒带人），转正单挂的就是那个打手，落「未分配」会造出「有人、但不在进行中」的隐形单（解冻闸 / 我的单都看不到）。
            #    ⚠️ 这是 P4 口径（业务方「接手类落地即未分配」）**唯一**的例外，只认「本事件那一刻还有一条本绑定的 claiming 镜像行」——普通 peer 单（没有镜像行 / 行已终态）逐字照旧。
            promoting_claim = PeerHallMirror.objects.filter(
                peer_base_url=binding.peer_base_url,
                # ⭐ P4：认**卡号**（多跳时是源头单号，单跳就是 `peer_order_no`）。
                peer_order_no=card_no,
                status=PeerHallMirror.Status.CLAIMING,
            )
            # ⭐ （业务方拍板）：抢单转正时**直接用那张隐藏单**（不再新建单）。取法 = 反向 OneToOne（`shadow_order`），⛔ 不写一句新的 `Order.objects` 读点（转正这条路的落点已经够多，能走反向关系就不另开一个取数口）；⚠️ `shadow_order` 反向关系能看到**已软删**的行 ⇒ 下面显式挡掉。
            claiming_card = None if direct_taker_assignment else (
                promoting_claim.select_related("game", "peer_upstream_binding")
                .order_by("-pk")
                .first()
            )
            promoting_claim = claiming_card is not None
            new_sync = link is None and not promoting_claim
            # 抢单沿用镜像快照。仅直接落新单与保存绑定设置串行。
            current = (
                PeerUpstreamBinding.objects.select_for_update().get(pk=binding.pk)
                if new_sync else binding
            )
            game = resolve_game(game_name, binding=current, new_sync=new_sync)
            if game is None:
                # 游戏未选中是本端配置，确认收到但不让上家重投。
                continue
            if link is None:
                # ── 首单 / 补回来的单（spec §16.3 末表的 `order.assign` / `order.grab`）──
                source, _ = OrderSource.objects.get_or_create(
                    name=source_name_for(binding.peer_name)
                )
                # ⛔ **不要手写 `source="…"`**：`Order.source`（名字快照）与 `order_source`
                # （字典 FK）的写入语义有唯一出处 ——
                # `apps/business/order_source.py::source_snapshot_overrides`（D7 不变式）。
                snapshot = source_snapshot_overrides({"order_source": source})
                # ⭐⭐ P4：**接手类**事件（`order.assign` / `order.grab`）⇒ 对方拿到的是**未分配**的单（业务方 裁定）；口径与更新支一致。⭐ D-0303 的例外见上面 `promoting_claim` 那段（抢单转正落「代练中」）。
                #    ⚠️ 其它 kind（`order.update`）**保持 `raw_status`**：建单支在 P4 之前就不带接受集闸、照抄事件里的状态（P3 语义），⛔ 不借本期改它——Global Constraints 5/6：改的只是「接手那一刻落地成什么」。
                #    ⚠️ 这是**本次要落的状态**的唯一表达式：新建支（`create`）与转正复用支（`_apply_peer_order_fields`）共用它，⛔ 不各写一份（两份口径必然漂移）。
                new_status = (
                    (
                        OrderStatus.IN_PROGRESS
                        if promoting_claim
                        else OrderStatus.UNASSIGNED
                    )
                    if kind in ("order.assign", "order.grab")
                    else raw_status
                )
                # ⭐ 建单字段收成一份 dict：新建走 `Order.objects.create(**fields)`、
                #    转正复用走「逐字段赋值 + save(update_fields)」——两支**同一份字段**，
                #    ⛔ 不许为了复用再抄一遍（抄一遍必然漂移）。
                incoming_price = payload.get("hall_price")
                if claiming_card is not None:
                    new_percent = claiming_card.hall_price_percent_at_sync
                elif current.game_rules is not None or direct_taker_assignment:
                    new_percent = upstream_game_price_percent(current, game.pk)
                else:
                    new_percent = None
                order_fields = dict(
                    game=game,
                    # ⛔ 业务方裁定：客户价（= 接单价）取**发单价**——
                    #    上家的客户价不再随载荷下发（`PEER_ORDER_FIELDS` 已删它）。
                    total_price=incoming_price if incoming_price is not None else 0,
                    hall_price=(
                        peer_hall.price_at_percent(incoming_price, new_percent)
                        if new_percent is not None else incoming_price
                    ),
                    # ⭐ A1 / H-1：`remark` / `content` 过类型闸——非字符串按「没带该字段」
                    #    处理（回空串 + `logger.info`），⛔ 不让一个畸形展示字段把整批落单
                    #    打成 500（判据见 `_peer_text_or_empty`）。
                    remark=_peer_text_or_empty(payload, "remark"),
                    project_text=_clip_to_column(
                        payload.get("project_text") or "", Order, "project_text"
                    ),
                    customer_phone=payload.get("customer_phone") or "",
                    customer_qq=payload.get("customer_qq") or "",
                    content=_peer_text_or_empty(payload, "content"),
                    # ⭐ （计划 `2026-10-25-peer-source-identity` §2 第 2 条）：这一列 = **对端的来源单号**（`target_ref`；升级后的发送侧送的就是 `effective_source_order_no(order)`）。
                    #    ⚠️ 老对端仍发它的内置号 ⇒ 落进来的就是内置号（**存量不反推**，业务方已裁定；RUNBOOK §3.2f）。
                    source_order_no=peer_order_no,
                    order_source=source,
                    sales_user=binding.bound_by,
                    # ⚠️ 显示名比 `sales_agent` 列宽（64）长 ⇒ 按裁决 R19 截断，
                    # 否则整批落单回滚（见 `_clip_to_column` 的代价说明）。
                    sales_agent=_clip_to_column(
                        user_display_name(binding.bound_by), Order, "sales_agent"
                    ),
                    # ⚠️ `created_by` **刻意保持 `get_username()`**：模型那一列的注释与既有
                    # 消费方（「被抢走」通知按 username 解析）都把它定义成**用户名**快照，
                    # 与 `sales_agent` 这个展示用文本快照语义不同，⛔ 不要顺手一起改。
                    created_by=binding.bound_by.get_username(),
                    **snapshot,
                )
                shadow = (
                    getattr(claiming_card, "shadow_order", None)
                    if claiming_card is not None
                    else None
                )
                if shadow is not None and shadow.deleted_at is None:
                    # ⭐ （业务方拍板）：**复用那张隐藏单**（同一张单转正）：挂打手 / 状态 / 展示价 / 来源都由本支按建单支的**同一份字段**写；订单号**保留隐藏单自己的**（它在大厅里被抛出去时就用这个号）；`hall_card` 由 `_promote_claiming_mirror` 收口时清空（单点）。
                    order = _apply_peer_order_fields(
                        shadow, status=new_status, fields=order_fields, event_at=occurred_at
                    )
                else:
                    order = Order(
                        order_no=generate_order_no(),
                        status=new_status,
                        **order_fields,
                    )
                    from apps.wecom.notifications import prepare_order_status
                    prepare_order_status(order, None, event_at=occurred_at)
                    order = Order.objects.create(
                        order_no=order.order_no, status=new_status, **order_fields,
                        wecom_notice_journal=order.wecom_notice_journal,
                        wecom_notice_pending=order.wecom_notice_pending,
                    )
                # ⭐ A2 / M-2（D-0305 裁定）：raw_status 照抄路**必须过** `DOWNSTREAM_ACCEPTED_STATUSES`（见上面那道闸）⇒ 建单支**写不出**「待验收」（接受集不含它；下家自己的待验收只能由本系统改——与 `UPSTREAM_STATUS_MAP` 那条「已完成只能由本系统自己更改」同源口径）⇒ 这里**不调** `order_time_fields.mark_submitted`（可证的死代码，按「防御性写法不当承重逻辑」移除）；交单时刻的活写入点只剩状态机与上行回传支（结构性网：`tests/test_order_time_fields.py::ORDER_STATUS_WRITE_SITES`）。
                # ⭐ P4 链身份的形状闸（`AGENTS.md` §5-E / E-0010：「**这类请求**还会不会 500」）。`source_order_no` 是**对端 JSON** 给的值，⛔ 不许裸值直接切片：真值非字符串（`123` / `{"a": 1}`）⇒ `TypeError` ⇒ 整批落单一起 500（本批共用一个事务，对端重投同样失败）；JSON 列表能切片、再被 `CharField` `str()` 成 `"[1, 2]"` ⇒ **静默错身份**。口径：**非空字符串**才用它，其余（缺失 / 空串 / 非字符串）⇒ 退回对端单号；⚠️ 兜底也要 `str()`：`target_ref` 同样是对端给的，非字符串时不许 500，`str()` 之后与 `peer_order_no` 列里 Django 落的值逐字一致。
                raw_origin = payload.get("source_order_no")
                try:
                    PeerOrderLink.objects.create(
                        order=order, upstream_binding=binding,
                        peer_order_no=peer_order_no,
                        upstream_internal_order_no=upstream_order_no,
                        origin_order_no=_clip_to_column(
                            raw_origin if isinstance(raw_origin, str) and raw_origin else peer_order_no,
                            PeerOrderLink,
                            "origin_order_no",
                        ),
                        last_applied_at=occurred_at,
                        hall_price_percent_at_sync=new_percent,
                    )
                except IntegrityError as exc:
                    diagnostic = getattr(exc.__cause__, "diag", None)
                    if getattr(diagnostic, "constraint_name", None) not in {
                        "uniq_peer_link_up_peer", "uniq_peer_link_up_order", "uniq_peer_link_up_internal_no",
                    }:
                        raise
                    # 回滚本次建单或隐藏单改写，再按另一事务提交的订单对应重判事件。
                    transaction.set_rollback(True)
                    pending.appendleft((raw, exc))
                    continue
                _warn_watermark_anomaly(
                    None, occurred_at, now=timezone.now(),
                    binding=binding, peer_order_no=peer_order_no,
                )
                # ⭐ D-0303：建单支的转正收口（两个调用点之二；更新支的那一个见 `else` 尾部。为什么必须两支各挂一次：`order.update` 先到的批次里建单发生在更新支看不到的这条路上——见 `_promote_claiming_mirror`）。
                # ⭐ A5 / M-8：转正撞上**业务类**失败（提成配置坏 / 金额越界 / 归属说不出）⇒ 照更新支「改价重算失败」同族的 `set_rollback(True)` + skip 形状分流——⛔ 不再裸 500（M-8 现场：500 被发送侧归「连接类失败」计停绑，~5 轮后自动停绑的毒循环）。
                # ⚠️ `note_skip` 只改内存清单（⛔ 不写库），不被那次回滚带走（同族先例：`SKIP_RECOMPUTE_FAILED` 那一支，顺序与理由逐字同源）。
                from apps.business.commission import CommissionError
                from apps.business.performance_amount import AmountError

                try:
                    if direct_taker_assignment:
                        # Leave evidence belongs to the same transaction as the direct assignment.
                        _apply_hall_event(
                            kind=HALL_TAKE_KIND, payload={}, binding=binding,
                            target_ref=card_no, note_skip=note_skip, now=timezone.now(),
                            hall_leave_reason=HALL_TAKE_LEAVE_ASSIGNED,
                        )
                    else:
                        _promote_claiming_mirror(
                            binding, card_no, order, chat_invite=chat_invite,
                        )
                except (CommissionError, AmountError, ValueError) as exc:
                    transaction.set_rollback(True)
                    note_skip(
                        peer_order_no,
                        SKIP_PROMOTION_FAILED,
                        f"抢单转正落库失败（提成 / 金额 / 归属）：{exc}；"
                        "本次事件未生效，配置修好后会随重投再试",
                    )
                    continue
                # ⭐ （业务方要求补的缺失）：**建单成功**写一条订单日志（「这单什么时候从哪家同步来的」）。位置在两条补偿出口（`continue` 的自检 / 回滚）**之后**、仍在同一个 `atomic()` 里 ⇒ 建单与日志同生共死；⛔ 跳过的事件走不到这里（上面全部 continue）。
                _record_peer_sync(
                    binding,
                    action="peer.inbound_order",
                    order=order,
                    to_value=_peer_status_label(order.status),
                )
            else:
                # ── 同一张单的后续事件：按时刻更新（spec §16.3）──
                order = link.order
                # ⭐ D-0343：「回池清人」那一档的**挡住说明**（已结账档点名给订单日志；其余取值恒为空串）。两支共用一个收口（文末 `_record_peer_sync`），所以在分流之前初始化。
                #    ⭐ 案 A / B1（D-0364）：本变量是**审计行 reason 的共用通道**——`order.update` 支：池内豁免的「状态保持」（`PEER_INBOUND_STATUS_HELD_NOTE`）；接手类支：D-0343 已结账挡住的那句。
                inbound_note = ""
                # ⭐ 案 B1（D-0364）：入站行带状态 from→to。**只有 `peer.inbound_update` 的模板带状态段**（指派 / 接单沿用各自旧模板）⇒ 只在 order.update 支填它（本地旧状态 label；与 to 相同 = 状态没被这条事件改 ⇒ 传空，渲染器把状态段整段剔除——R3 / 池内豁免行不留「（状态： → X）」空壳）。
                inbound_from_value = ""
                if kind == "order.update":
                    # ⭐ P3 起：下行 = 状态 + 发单价 + 内容面六列（D-0377 扩内容面，见下面那一段；发单价只动客户价，D-0307）。状态还要过**接受集**闸（下家只认那 4 种）；发单价判 `is not None`（E-0021：`hall_price = 0` 是合法金额，⛔ 别用真值）；状态**合法但不在集合里**（如 `PENDING_REVIEW`）⇒ 不改状态，但这**不是跳过**（裁定 R3）：事件照样算 `applied`、不进 `skipped_details`——判成跳过会让发送侧退避重投一条注定不改状态的事件。
                    #    ⭐⭐ 案 A（D-0364 生产实锤）：**池内豁免**——本地还在上家不可能知道的两个池内态（`PEER_POOL_LOCAL_STATUSES`）时，入站的 `IN_PROGRESS` 只能是上家**过期视图的回放**（上家每次改价 / 改备注生成的 `order.update` 载荷恒带它**当前**状态；触发事件又确实比本地动作新，stale 双闸拦不住）⇒ 状态**不写**（否则单被静默打回「代练中」并掉出大厅——现场僵尸态 2h48m 至退款），其余字段照旧；仍走 R3 同款：事件算 `applied`、水位照推进（公共尾），⛔ 不进 skipped，审计行 reason 写「状态保持」（见 `inbound_note`）。
                    #    ⚠️ 入站为 `ISSUE` / `PAUSED` / `REFUNDED` **照旧穿透**：上家的异常 / 暂停 / 退单是真实业务事件，退单必须能落到已回池的单上；`assign` / `grab` / `withdraw` 三个显式分支与 promote 镜像单支**一字不动**——上家真要收回 / 重派走的是 `order.assign` / `withdraw`，不走普通更新。
                    previous_status = order.status
                    pool_status_exempt = (
                        str(previous_status) in PEER_POOL_LOCAL_STATUSES
                        and str(raw_status) == "IN_PROGRESS"
                    )
                    # 这条事件**真的写了状态吗**：同一把判据供「状态赋值」与下面的第三方
                    # 推送闸共用（⛔ 不是两份口径——见推送闸那段的注释）。
                    inbound_status_written = (
                        str(raw_status) in DOWNSTREAM_ACCEPTED_STATUSES
                        and not pool_status_exempt
                    )
                    if inbound_status_written:
                        order.status = raw_status
                    if pool_status_exempt:
                        inbound_note = PEER_INBOUND_STATUS_HELD_NOTE
                    from_label = _peer_status_label(previous_status)
                    to_label = _peer_status_label(order.status)
                    if from_label != to_label:
                        inbound_from_value = from_label
                    # ⭐ D-0377：order.update 也落内容面字段（白名单原样、类型闸与接手类支同一套；
                    #   stale 双闸已在上面跑过）。game 照建单支按名字 resolve；server/category/project
                    #   载荷只有 id、接收侧无对齐键不消费（peer_sync.py 登记）⇒ 按跳过该字段处理、不炸。
                    #    ⭐ project/remark 旧值快照照接手支 B1 同款（save 后比对，变了才排下家）。
                    previous_project_text = order.project_text
                    previous_remark = order.remark
                    order.remark = _peer_text_or_empty(payload, "remark")
                    order.project_text = _clip_to_column(
                        payload.get("project_text") or "", Order, "project_text"
                    )
                    order.customer_phone = payload.get("customer_phone") or ""
                    order.customer_qq = payload.get("customer_qq") or ""
                    order.content = _peer_text_or_empty(payload, "content")
                    order.game = game
                    # ⭐⭐ D-0307（业务方 终裁）：**上家改价只动客户价**。上家发单价落到下家账上 = 「接单客户价」（`Order.total_price`，进账侧）⇒ 上家改价改的是它；下家给打手的价（`Order.hall_price`）由下家自己决定改不改 ⇒ **这一支一格都不动它**，打手金额也因此不重算（`performance.order.hall_price` 未变 ⇒ 算式输入未变）。
                    #    ⚠️ 判据 `is None`（E-0021 / L-4）：显式 0 落真 0、负价照落（D-0306 订单流口径）；只有「载荷没带价」才保留旧值。旧写法走 `_landing_hall_price` 并写 `hall_price`（P4/P1 的展示价覆盖）——D-0307 之后那一整套前提不再成立：转正那一刻已把展示价写进 `hall_price`，之后上家改价不该再碰它。
                    incoming_total_price = payload.get("hall_price")
                    previous_total_price = order.total_price
                    if incoming_total_price is not None:
                        order.total_price = incoming_total_price
                    # 价真的变了才补刷客服行（列精度比较，见 `_amounts_equal`：
                    # 库里的 Decimal 与载荷的 str 直接 `!=` 恒为真）。
                    total_price_changed = (
                        incoming_total_price is not None
                        and not _amounts_equal(previous_total_price, incoming_total_price)
                    )
                    from apps.wecom.notifications import prepare_order_status
                    notice_fields = ['wecom_notice_journal', 'wecom_notice_pending'] if prepare_order_status(order, previous_status, event_at=occurred_at) else []
                    order.save(update_fields=[
                        "status", "total_price", "remark", "project_text",
                        "customer_phone", "customer_qq", "content", "game",
                    ] + notice_fields)
                    if total_price_changed:
                        from apps.business.order_writes import queue_externalpay_recheck
                        queue_externalpay_recheck(order)
                    # ⭐ D-0377（B1 同款先例，位置与事务口径照接手支那一段）：这一支真改了
                    #    「接手项目 / 备注」⇒ 同事务排 `member.project` 给鸡蛋铺下家（消费口
                    #    ModifyOrder 整单覆盖一次带两格）；不接这句，双挂租户的上家改内容会停在
                    #    我与鸡蛋铺下家之间。判据 = 上面那两份旧值快照（⛔ 不是「带了键」——
                    #    对端载荷恒带这两列）。
                    if (
                        order.project_text != previous_project_text
                        or order.remark != previous_remark
                    ):
                        from apps.business import jidanpu_member
                        jidanpu_member.queue_project_update(order)
                    # ⭐ T2/D-0307：改价留痕记**客户价**旧→新（这一支不再动 `hall_price`；
                    #    helper 的 `column` 决定台账里那两个金额是哪一列的口径）。
                    _record_upstream_price_change(
                        order,
                        binding=binding,
                        before=previous_total_price,
                        after=incoming_total_price,
                        occurred_at=occurred_at,
                        column="total_price",
                    )
                    # ⭐ 开放平台 v2（T8，设计文档 §7.2.1 裁决 1）：这是**绕过状态机的 7 处直接写入**之一（上下游同步接收侧）——对端推来的是一次**真实的本地状态变化** ⇒ 第三方应用必须知道（⛔ 不许只挂 `apply_transition` 一处了事，那会**静默漏推**）。
                    #    ⚠️ 位置在**同一个事务**里（上面 `with transaction.atomic()` 的块内），排在 `save()` 之后 ⇒ 「库里真改了」与「推送行在」同生共死。⚠️ 时刻**显式取**（⛔ 不读 `order.updated_at`：本次 `update_fields` 里**没有** `updated_at` ⇒ 那一列**两边都不会被刷新**——Django 的 `Model._save_table` 只对 `update_fields` 里列出的字段调 `f.pre_save()`（`auto_now` 挂的就是它，实测：`django/db/models/base.py` 的 `non_pks_non_generated` 过滤 + `values = [... f.pre_save(self, False) ...]`；`DateTimeField.pre_save` 的 `auto_now` 分支才 `setattr`）⇒ 读它只会拿到**更早**的时刻）。
                    #    ⚠️⭐ **推送只在状态真的被这条事件改过时才排**（判据 = `inbound_status_written`，与上面那行状态赋值**同一个**变量）：这一支的 `save()` **无条件**写 `status` 列（不在接受集里就是把旧值原样写回、池内豁免就是保住旧值，见上面 R3 / 案 A 那两段注释）⇒ 把推送放在无条件分支里，一条**只改价**的事件也会推一条"状态变成 X"，而 X 根本没变（对着第三方说假话）。⛔ 判据必须**与状态赋值共用同一份**：另写一份"新旧值比较"或把 `DOWNSTREAM_ACCEPTED_STATUSES` 单独抄来这里，就是第二份口径（迟早与上面那几行漂移）。⭐ 案 A（D-0364）：池内豁免的场合状态**没被改** ⇒ 同一个变量为假 ⇒ 也不会对着第三方推一条「代练中」（那正是把单打回池内的说假话）。
                    if inbound_status_written:
                        # ⚠️⭐ `spec=OPENAPI_SPEC` 显式（回调线 T1b；漏传 = 当场 `TypeError`）。
                        from apps.openapi import dispatch as openapi_dispatch
                        from apps.openapi.credentials import OPENAPI_SPEC
                        from apps.takerapi.api import callback_recipients as taker_callback

                        # ⚠️ 两族共用**同一个**时刻对象（同一次动作、两个当事人）。
                        moment = timezone.now()
                        openapi_dispatch.notify_order_status_changed(
                            order,
                            spec=OPENAPI_SPEC,
                            status=raw_status,
                            occurred_at=moment,
                        )
                        # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 2 行前半）：**紧挨着**上面那句，同一个事务、同一个 `moment`。
                        #    ⚠️ 它落在**同一个** `if str(raw_status) in …` 块里（⛔ 不是块外）：那条判据的语义是"状态**真的被这条事件改过**"，打手侧同样不该对一条只改价的事件说"你的单状态变成 X 了"（⛔ 不另写一份判据）。
                        taker_callback.notify_order_status_changed_for_taker(
                            order, status=raw_status, occurred_at=moment
                        )
                        queue_order_status_notice(order, previous_status, event_at=occurred_at)
                    # ⭐ D-0307：这一支**不再改 `hall_price`** ⇒ 下游鸡蛋铺那条线的价格（它跟的正是 `hall_price`）**不排**：价没变，排一条「价改成同一个值」的活只是噪声。⛔ 不是忘了接线（旧 T3 那段接线的理由随 D-0307 失效）。
                    # ⭐⭐ D-0307补接线：这一支现在写 `total_price`（净实收 / 客服提成算式的输入）⇒ 客服估算行必须跟上。⚠️ 旧注释说「该支不写 total_price ⇒ 不用刷」——那个前提已随 D-0307 翻转；`refresh_customer_estimate` 自己逐行判 `is_settled`（已结账行不动），且「值没变就不写」⇒ 这里再判一层「价真的变了」只是省一次无谓读写。
                    #    ⚠️ `hall_price` 未变 ⇒ **打手金额不重算**（D-0307：上家改价不自动动打手价），下游鸡蛋铺那条线的价**也不排**（见上一条）——两者都不是「忘了」。
                    if total_price_changed:
                        from apps.business.settlement_hooks import (
                            refresh_customer_estimate,
                        )

                        refresh_customer_estimate(order)
                else:
                    # ── `order.assign` / `order.grab` / `order.withdraw` ──
                    # ⭐ 结算 P0-7（`D-0287`）：退单钩子（`on_order_refunded`）要的是**赋值之前**的来源态（它据此判「这是不是一次真实的退单迁移」，传 `REFUNDED` 会被当成重跑 ⇒ 第 1.5 / 1.6 步全跳过、钱留在钱包里）。
                    #    ⚠️ 必须在**下面三个赋值之前**拍；⛔ 不要在 `save()` 之后读 `order.status`——那时它已经是写进去的新值。
                    previous_status = order.status
                    # ⭐ P4：接手类事件 ⇒ 对方拿到的是**未分配**的单（业务方 裁定）。⛔ 不照抄对端状态，也⛔ 不许与上面建单支打架（口径同一）。
                    #    ⭐ D-0303 的例外（与建单支同一段判据的**第二半**）：这张单**已经是**某次抢单转正的产物（镜像行 `promoted` 且 `promoted_order` 就是它）⇒ 接手事实早已由本线落定（打手已挂），⛔ 不回落 UNASSIGNED——否则「update 先建单（已转正、已挂人、代练中）→ assign 后到」这批会被打回「未分配」，解冻闸 / 我的单全部失明。
                    #    ⚠️ 只认 `promoted_order = 这张单`：上家对**别的单**的指派照旧落 P4 口径。
                    if kind in ("order.assign", "order.grab"):
                        promoted_hall = PeerHallMirror.objects.filter(
                            peer_base_url=binding.peer_base_url,
                            # ⭐ P4：同上，认**卡号**（多跳时 `target_ref` 是本跳上家的
                            #    单号，而卡上记的是源头单号）。
                            peer_order_no=card_no,
                            status=PeerHallMirror.Status.PROMOTED,
                            promoted_order=order,
                        ).exists()
                        # ⭐ D-0343：**转正确认在途**（本卡此刻还有一条 `claiming` 镜像行）与 `promoted_hall` 同一档——这次 assign 就是**那次抢单的确认**，文末的 `_promote_claiming_mirror` 马上会把人挂上 ⇒ 落「代练中」、⛔ 不落「未分配」、⛔ 不清人。
                        #    （建单支的 `promoting_claim` 判据是 D-0303 的既有口径；`_promote_claiming_mirror` 的 docstring 本来就声明两个调用点各带它一份——更新支此前漏了，本批补齐。）
                        order.status = (
                            OrderStatus.UNASSIGNED
                            if not (promoted_hall or promoting_claim)
                            else OrderStatus.IN_PROGRESS
                        )
                    elif kind == "order.withdraw":
                        order.status = OrderStatus.REFUNDED
                    else:
                        # ⭐ A2 / M-2（D-0305 裁定）：上面那道 `DOWNSTREAM_ACCEPTED_STATUSES` 闸**盖住本支**（未知 kind 的 `raw_status` 不在集内 ⇒ 整条硬跳过，进不到这里）⇒ 能走到这一行的 `raw_status` 必在集内 ⇒ **写不出「待验收」**，因此这里**不调** `mark_submitted`（可证的死代码，按「防御性写法不当承重逻辑」移除）；下家自己的待验收只能由本系统改（与 `UPSTREAM_STATUS_MAP`「已完成只能由本系统自己更改」同源口径）。结构性网登记：`tests/test_order_time_fields.py` 把本落点钉为 `gated`（不算交单时刻写入点）。
                        #    ⭐⭐ 案 A（D-0364）：**池内豁免与 `order.update` 支同一把判据**（`PEER_POOL_LOCAL_STATUSES` 是唯一出处，⛔ 不在这里另写一份）：本支与更新支落的是同一条「状态照抄」路（`_PEER_INBOUND_ACTION_BY_KIND` 的注释），上家的过期 `IN_PROGRESS` 回放同样不许把池内单打回代练中。
                        #    ⚠️ 只豁免**状态**：本支的字段面（`remark` / `project_text` / `content` …，上面那几行）照常写——两支的口径差就这一条。⚠️ 事件照旧 `applied`、水位照推进（公共尾），与更新支同款。
                        if (
                            str(previous_status) in PEER_POOL_LOCAL_STATUSES
                            and str(raw_status) == "IN_PROGRESS"
                        ):
                            inbound_note = PEER_INBOUND_STATUS_HELD_NOTE
                        else:
                            order.status = raw_status
                    # ⛔ 同上：客户价取发单价；载荷没带价（**`is None`**）⇒ 保留旧值。⭐ L-4（E-0021 家族）：旧写法 `or` 把**显式 0**（以及负价）当成「没带价」⇒ 对端发 `0` 时客户价停在旧值。
                    #    D-0015 / D-0306：0 与负数都是合法且有意义的边界 ⇒ 判据必须是 `is None`（「没带」与「带了 0 / 负数」是两件事）。
                    incoming_total_price = payload.get("hall_price")
                    previous_total_price = order.total_price
                    if incoming_total_price is not None:
                        order.total_price = incoming_total_price
                    # ⭐ A1 / H-1：更新支的两处 TextField 同一把类型闸（非字符串按
                    #    「没带该字段」处理 = 与缺键/`None` 既有口径一致地写空串；
                    #    ⛔ 不让 psycopg 的类型错把整批打成 500，判据见 `_peer_text_or_empty`）。
                    #    ⭐ D-0377：`remark` 也要旧值快照（下面 `queue_project_update` 的
                    #    「真的变了」判据与 project_text 同一把口径）。
                    previous_remark = order.remark
                    order.remark = _peer_text_or_empty(payload, "remark")
                    # ⭐⭐ （红队 B1）：这一支**直接**改 `project_text`、`save()` 里也不经过 `order_writes.update_order_fields` 那份 `changed` 清单 ⇒ 「这一列**真的**变了没有」必须在这里**自己比一次**，判据口径与本地编辑那条**同一个**：**比旧值**（`!=`，与原样落库的值同形）。
                    #    ⛔ **不是「这一支带没带 `project_text`」**：对端载荷 `PEER_ORDER_FIELDS`（`peer_sync.py`）**恒带** `project_text`（不只是"改了项目"的事件）⇒ 拿「带没带键」当判据 ⇒ 上家每一条状态类 `order.update` / 接手类事件都会排一条改项目（对着下家做无谓的写）。
                    #    ⚠️ 旧值必须**在赋值之前**拍（下面紧接着就是那句赋值）。⚠️ 取的内存值是本次事务里读出来的那一行（`link.order`），与 `save()` 真正写回的是同一份 ⇒ 这份快照就是"库里改之前的值"。
                    previous_project_text = order.project_text
                    order.project_text = _clip_to_column(
                        payload.get("project_text") or "", Order, "project_text"
                    )
                    order.customer_phone = payload.get("customer_phone") or ""
                    order.customer_qq = payload.get("customer_qq") or ""
                    order.content = _peer_text_or_empty(payload, "content")
                    order.game = game
                    # ⭐ P3（R10）：`hall_price` 已进 `_UPDATE_FIELDS` ⇒ 这一支不赋值就等于把内存里的旧值原样写回（P1/P2 事件也带发单价）；同样判 `is not None`。
                    # ⭐ P4/P1：与 `order.update` 那一支**同一个落点**（已转正的镜像单落本层展示价，理由见 `_landing_hall_price`）。
                    hall_price = _landing_hall_price(
                        order, incoming=payload.get("hall_price"),
                        percent_at_sync=link.hall_price_percent_at_sync,
                    )
                    # ⚠️ 上家改价留痕（T2，spec §3-D2）：旧值同样**在赋值之前**拍
                    #    （理由与上面 `order.update` 那一支同源：载荷里没有旧值）。
                    previous_hall_price = order.hall_price
                    if hall_price is not None:
                        order.hall_price = hall_price
                    from apps.wecom.notifications import prepare_order_status
                    notice_fields = ['wecom_notice_journal', 'wecom_notice_pending'] if prepare_order_status(order, previous_status, event_at=occurred_at) else []
                    order.save(update_fields=list(_UPDATE_FIELDS) + notice_fields)
                    if incoming_total_price is not None and not _amounts_equal(previous_total_price, incoming_total_price):
                        from apps.business.order_writes import queue_externalpay_recheck
                        queue_externalpay_recheck(order)
                    # ⭐ T2：改价留痕——**同一个 `atomic()` 块内、`save()` 之后**。
                    #    `order.assign` / `order.grab` / `order.withdraw` 三种 kind 共用这一处
                    #    （它们都会写 `hall_price`）；同价 / 不带价 ⇒ helper 自己不写。
                    _record_upstream_price_change(
                        order,
                        binding=binding,
                        before=previous_hall_price,
                        after=hall_price,
                        occurred_at=occurred_at,
                    )
                    # ⭐⭐ D-0343：回池档必须清人——本支直写 status、绕过 `apply_transition`，回池钩子 `_on_pool_entry`（D-0032「进池⇔无人」）从不触发；五步与三道闸与人工 `/clear-taker` 同批函数（⛔ 不另写第二套；枚举外移 docs/code-comment-notes.md）。
                    #    三道闸承重：只回池档清（promoted_hall/promoting_claim ⛔ 不清）；无行/无人零动作（⛔ 不补绩效行）；已结账只挡钱（D-0156 同口径，说明进 `peer.inbound_assign` reason）。
                    #    ⚠️ 清人与状态写同一个 `atomic()`（E-0022，事件没生效清人一起消失）；`inbound_note` ⛔ 重复初始化（会静默抹掉未知 kind 的豁免说明）。
                    if (
                        kind in ("order.assign", "order.grab")
                        and not promoted_hall
                        and not promoting_claim
                    ):
                        perf = Performance.objects.filter(
                            order=order, emp_type=EmpType.MAIN
                        ).first()
                        if perf is not None and perf.user_id is not None:
                            if perf.is_settled:
                                inbound_note = (
                                    "上家已把这张单的接手人移除，但该单绩效已结账，"
                                    "不能自动清人；请先撤销结账再人工处理"
                                )
                            else:
                                released_taker = perf.user
                                from apps.business import peer_dispatch
                                from apps.business.performance import (
                                    reset_taker_performance,
                                )
                                from apps.business.settlement_hooks import (
                                    refresh_customer_estimate,
                                )

                                reset_taker_performance(perf)
                                chat_groups.assignment_changed(order, None, former_worker=released_taker)
                                refresh_customer_estimate(order)
                                record(
                                    log_type=ACTION_LOG_TYPES["order.clear_taker"],
                                    action="order.clear_taker",
                                    # 机器来源（常驻同步进程落地对端事件）⇒
                                    # `actor_id` 留空 + 显式 `system=True`；
                                    # `actor` 用对方可读名（`_record_peer_sync` 同源）。
                                    actor=_peer_counterparty_name(binding),
                                    system=True,
                                    target_kind="user",
                                    target_id=str(released_taker.pk),
                                    target_name=user_display_name(released_taker),
                                    order_no=order.order_no,
                                )
                                # 下行 peer 副本退（`_on_pool_entry` 的既有第 6 步同形：人没了，告诉我们的下家）。⭐ F-1：时刻**显式取 now**，⛔ 不走 `enqueue_order_event` 的 `updated_at` 默认值。
                                #    钩子（`performance.py`）不传是有前提的：`apply_transition` 刚把 `updated_at` 写新；本支的 `save(update_fields=list(_UPDATE_FIELDS))` **不含** `updated_at` ⇒ 默认值 = **上一次**订单写入（多跳场景恰是当初派给下家的 `/assign` 时刻），而下家接收侧的 stale 闸是 `occurred_at <= last_applied_at` 相等即丢 ⇒ 这条撤单会被下家**静默跳过**，接手人照样挂着（E-0307 往下传一跳）。
                                #    与四行之后打手回调的显式 `occurred_at=timezone.now()` 同一处置。
                                peer_dispatch.enqueue_order_withdrawn(
                                    order,
                                    released_taker,
                                    "taker_removed",
                                    occurred_at=timezone.now(),
                                )
                                # 打手侧回调（`_on_pool_entry` 的既有第 7 步同形；
                                # 收件人是**前任**——此刻行上的 user 已被清空）。
                                from apps.takerapi.api import (
                                    callback_recipients as taker_callback,
                                )

                                taker_callback.notify_order_reassigned_for_taker(
                                    order,
                                    former_taker_id=released_taker.pk,
                                    reason="taker_removed",
                                    occurred_at=timezone.now(),
                                )
                                # 鸡蛋铺下家副本退（人工 `/clear-taker` 同形；
                                # 排在清人之后，理由见上面 ⑤）。
                                from apps.business import jidanpu_member

                                jidanpu_member.queue_withdraw(order)
                    # ⭐⭐ 结算 P0-7（`D-0287`）：撤单落库后**跑退单钩子**。为什么必须有这一句：钱包成为发薪唯一出口之后，完成单在完成那一刻就已**自动入账**（`settled_by="system"`，钱进了打手钱包）；只写状态列的话上家撤单后**钱留在钱包里、绩效行一个字节不动**（本条要堵的形状）。钩子做的四件事（绩效两列清零 / 自动发薪回冲 + 结账四列复位 / 客服行清 0 / 中断在途提现）正是「撤单」该有的钱上后果（口径见模块 docstring 那一节）。
                    #    ⚠️ 位置：`save()` **之后**、仍在同一个 `atomic()` 内 ⇒ 钱与状态同生共死；排在改价留痕之后（那条是审计、先记「价改过」，与本句互不覆盖）。⚠️ 传 `previous_status`（**赋值之前**的状态，见上面那段注释）：钩子用它判「是不是一次真实的退单迁移」。⚠️ **懒导入**（与本模块别处同一处置）：本模块在同步进程里被导入，顶层 import `refunds` 会把它依赖的钩子注册链一起拉进来。
                    #    ⚠️ 失败处置与下面「改价重算失败」那一档同形（E-0022：`atomic()` 是事务不是回滚点 ⇒ 先 `set_rollback(True)` 再 `continue`，⛔ 块内不裸 `return`）；只接**金额 / 提成类**领域错误（可预期、人改完配置能重投），数据脏 / DB 故障照旧响亮 500（⛔ 不许伪装成「没什么要回冲的」）。
                    if kind == "order.withdraw":
                        from apps.business.commission import CommissionError
                        from apps.business.performance_amount import AmountError
                        from apps.business.refunds import on_order_refunded

                        try:
                            on_order_refunded(order, previous_status)
                        except (CommissionError, AmountError) as exc:
                            transaction.set_rollback(True)
                            note_skip(
                                peer_order_no,
                                SKIP_WITHDRAW_REFUND_FAILED,
                                f"撤单落库后跑退单钩子失败，本次撤单未生效：{exc}；"
                                "钱与绩效一个字节都没动，配置 / 数据修好后会随重投再试",
                            )
                            continue
                        if withdraw_reason == "taker_removed":
                            chat_groups.disconnect_from_upstream(order, binding)
                    # ⛔ **这一支也写 `hall_price`，但结算 P0-5 有意不给它补重算**（计划 §5-5，不是"忘了"）：`order.assign` / `order.grab` 是**接手类**事件 ⇒ 按 P4 口径（业务方裁定）落地即 `UNASSIGNED`，「这张单在我这里**没有**打手」是常态——本地那条 MAIN 行要么不存在、要么是**上一手**留下的，钱该由本地重新指派（`set_taker` → `recompute_order_amounts`）时按**那一刻**的价重算 ⇒ 这里先算一次只会把一笔**不属于任何人**的金额写进那一行；`order.withdraw` 是**撤单**事件 ⇒ 本地落 `REFUNDED`，绩效侧由退单钩子（`refunds.on_order_refunded`）负责清零 / 回冲，在这里重算会与那个钩子抢同一批金额列；只有 `order.update`（上家改价、单仍在本地下家手上）才常带「有 MAIN 归属人且未结账」的形态 ⇒ 重算补在那一支。⚠️ 若哪天接手类落地不再是 `UNASSIGNED`（改 P4 口径），这一档要回来补。
                    # ⭐⭐ （红队 B1 的裁定：**接上**）：这一支改了「接手项目」或「备注」（`Order.project_text` / `Order.remark`，`_UPDATE_FIELDS` 里都有）⇒ 与**同一条路上已经接了的**价格那一档语义完全一致（`queue_price_update`，见下面 `hall_price is not None` 那一段的三条理由）：**下家那张单该跟**（remark 的消费口 = ModifyOrder 的 orderRemark 格，取的就是 Order.remark，D-0377）。⭐ **可达性**（业务方报的「改了项目名鸡蛋铺不跟」的另一扇门）：一张单可以**同时**挂 `PeerOrderLink`（上家推给我）与 `JidanpuMemberOrderLink`（我推给我下家的代练员）⇒ 不接这一句，上家推来的项目名会**停在我下家那一格**。
                    #    ⚠️ **位置与事务口径照该支既有的那几行**：`save()` **之后**、仍在 `with transaction.atomic()` 块内——台账行与这次订单写入同生共死（⛔ 不留「单改了、下家不知道」的半截状态），且排事件**不调上游**（上游由 `jidanpu_sync` 的 worker 在事务外做）；判据是上面那份 `previous_project_text` 快照（**真的变了**才排），⛔ 不在这里另写一份「值变没变」；⛔ 也**只接鸡蛋铺下家这一条线**（peer 线自己的「转发给下游租户」不在本线范围 ⇒ 不在这里调 `peer_dispatch.notify_order_project_changed` 之类，那会把事件 echo 回 peer 线）。
                    if (
                        order.project_text != previous_project_text
                        or order.remark != previous_remark
                    ):
                        from apps.business import jidanpu_member
                        jidanpu_member.queue_project_update(order)
                    # ⭐ 开放平台 v2（T8，设计文档 §7.2.1 裁决 1）：`order.assign` / `order.grab` / `order.withdraw` 这一支**也**直接写状态列（上面那三个赋值是 `REFUNDED` / `UNASSIGNED` / 对端原状态）⇒ 同样要推。
                    #    ⚠️ 三条赋值都只改内存里的 `order.status`，所以这里取 `order.status` 就是**这次真的写进去**的那个值（`save()` 的字段集里含 `status`）。
                    #    ⚠️ 「同一件事被多条路径触发」的去重判据在 `dispatch._is_duplicate`（同一订单 + 同一事件 + 同一状态不重复入队），⛔ 不在这里另判一档。⚠️⭐ `spec=OPENAPI_SPEC` 显式（回调线 T1b；漏传 = 当场 `TypeError`）。
                    from apps.openapi import dispatch as openapi_dispatch
                    from apps.openapi.credentials import OPENAPI_SPEC
                    from apps.takerapi.api import callback_recipients as taker_callback

                    # ⚠️ 两族共用**同一个**时刻对象（同一次动作、两个当事人）。
                    moment = timezone.now()
                    openapi_dispatch.notify_order_status_changed(
                        order,
                        spec=OPENAPI_SPEC,
                        status=order.status,
                        occurred_at=moment,
                    )
                    # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 2 行中段）：**紧挨着**上面那句，同一个 `atomic()` 块、同一个 `moment`。
                    #    ⚠️ 这一支是 `order.assign` / `order.grab` / `order.withdraw` 三种 kind 共用的一处（接管 / 退单**都会**改状态）⇒ 打手侧也必须接："这单现在归谁"变了，正是他要知道的事。
                    #    ⚠️ 收件人取**写完之后**的当前接手人（`order.status` 同源）——本条事件说的是"这张单现在的状态"，与"不再是你的了"（`order.reassigned`）**两件事**。
                    taker_callback.notify_order_status_changed_for_taker(
                        order, status=order.status, occurred_at=moment
                    )
                    queue_order_status_notice(order, previous_status, event_at=occurred_at)
                    if hall_price is not None:
                        # ⭐ T3：接手类事件（`order.assign` / `order.grab` / `order.withdraw`）同样带发单价（P3/R10：`hall_price` 已进 `_UPDATE_FIELDS`）⇒ 与上面 `order.update` 那一支**同一个口径**：带了价就把它跟到**第三方鸡蛋铺**那条线。
                        #    ⚠️ **只接鸡蛋铺下家这条线**；peer 线自己的「转发给下游租户」不在本线范围 ⇒ ⛔ 不在这里调 `notify_order_price_changed`。
                        #    ⚠️ 判据同样是 `is not None`、同样**有意不做**「价真的变了」比较（三条理由与代价见上面 `order.update` 那一支的注释，⛔ 不在这里再抄一份）。
                        from apps.business import jidanpu_member
                        jidanpu_member.queue_price_update(order)
                    # ⭐ 第二批（当场 grep 复核）：这一支重写了 `order.total_price`（净实收的输入之一）⇒ 客服行那两列要跟上（闸门在 `refresh_customer_estimate` 里逐行判 `is_settled`）。⚠️ **判据只有 `kind` 这一层，⛔ 没有「金额有没有被这一支改过」那一层**（原文写「只在金额真的被这一支改过时刷」——与代码不符，已改准）：代码就是「`kind != "order.withdraw"` ⇒ 刷」。
                    #    ⚠️ **这个守卫已降级为「防御性」**（P0-7 / `D-0287`；原文写它是「承重的」并挂了一条变异判据，那个说法**今天不再成立**）：`order.withdraw` 那一支的绩效侧现在由上面那次退单钩子负责，而钩子内部**自己就会调** `refresh_customer_estimate`（排在清 0 之后）⇒ 撤单这档再调一次是**幂等**的（`REFUNDED` 上它只写 `temp_amount=0.00`，而第 2.7 步已把未结账行清成 `0.00`，第 5 条「值没变就不写」⇒ 一行不写）⇒ **去掉本守卫，本批没找到会红的用例**（⛔ 别再把「缺一不可」写在这里）。保留它有两个理由（都不是「承重」）：① 计划 §2 明确要求保留这一行；② 它挡住的是「**同一件事被刷两遍**」这个形状本身——万一将来钩子里的那次内部刷新被移走，本守卫仍在，撤单后客服行不会停在旧值上。
                    #    ⚠️ **判据更新（同批改准）**：`tests/test_peer_inbound_update.py` 那条原来钉「撤单让绩效行**逐字节不变**」的反向控制已按新口径改写（现在钉「钩子跑过：两列清 0 + 回冲 + 结账痕迹复位」）。⚠️ **新挂点真正服务的形态**（Minor-10 改准）：本 `else` 支里的 `order.assign` / `order.grab` 等事件**重写了 `order.total_price`**（`order.total_price = payload.get("total_price") or 0`）——`total_price` 是客服算式（`settlement_amount.net_received`）的输入 ⇒ 刷客服行有意义。⛔ **原文那句「服务『本地已经做过、单又被上家改价』那种形态」不可达**（Minor-10 / D-0307 先后两次改准）：**入向改价走的是上面 `order.update` 那一支**，而那一支现在**自己写 `total_price`**（`incoming_total_price`）并在 `total_price_changed` 为真时**自己**调 `refresh_customer_estimate` ⇒ 本 `else` 支不必替那种形态刷（原文写的「不写 total_price」已不成立）。⚠️ **懒导入**（与 `refunds.py` 同一种处置）：本模块在同步进程里被导入，顶层 import `settlement_hooks` 会把钩子注册链也拉进来。⚠️ **可达性的如实登记**：镜像单在本仓**通常没有绩效行**（没有 SALES 行 ⇒ 本句是纯读、一行不写）。
                    if kind != "order.withdraw":
                        from apps.business.settlement_hooks import (
                            refresh_customer_estimate,
                        )

                        refresh_customer_estimate(order)
                # ⭐ D-0303：更新支的转正收口（两个调用点之一；实现与判据见 `_promote_claiming_mirror`——它必须在两支各自出现，原因见其 docstring）。
                # ⭐ A5 / M-8：与建单支同一把分流（业务类失败 ⇒ set_rollback + skip + 可重投，⛔ 不裸 500、不计连接失败）；`continue` 在 `link.last_applied_at` 推进之前 ⇒ 这次事件不算已应用，配置修好后随重投再试。
                from apps.business.commission import CommissionError
                from apps.business.performance_amount import AmountError

                try:
                    _promote_claiming_mirror(
                        binding, card_no, order, chat_invite=chat_invite,
                    )
                except (CommissionError, AmountError, ValueError) as exc:
                    transaction.set_rollback(True)
                    note_skip(
                        peer_order_no,
                        SKIP_PROMOTION_FAILED,
                        f"抢单转正落库失败（提成 / 金额 / 归属）：{exc}；"
                        "本次事件未生效，配置修好后会随重投再试",
                    )
                    continue
                # ⭐ B5 / L-10：落水位前的自检（旧水位 → 新水位：倒退 / 超前两腿都查）。
                #    ⚠️ **核实的口径（B5）**：这一列写的**本来就是事件时刻**（`occurred_at`，
                #    建单支同）⇒ 本批不改写法、只加自检日志（计划 B5 的如实分支）。
                _warn_watermark_anomaly(
                    link.last_applied_at, occurred_at, now=timezone.now(),
                    binding=binding, peer_order_no=peer_order_no,
                )
                link.last_applied_at = occurred_at
                link.save(update_fields=["last_applied_at"])
                if kind == "order.withdraw":
                    # ⭐ 撤单落库（spec §16.2）：直接写状态列（上面那次 save），并写**一条**
                    # 订单日志。`record()` 没有 `summary` 形参 ⇒ 文案由
                    # `apps/audit/actions.py::ORDER_PEER_WITHDRAW` 渲染，⛔ 调用方不拼串。
                    actor_name = raw.get("actor_name")
                    record(
                        log_type=ACTION_LOG_TYPES["order.peer_withdraw"],
                        action="order.peer_withdraw",
                        # `{昵称}` 由**上家**带（下家算不出显示名）；缺失时回落绑定里的
                        # 上家子账号（spec §16.2 / Ruling 1）。⛔ `actor_id` 留空：
                        # 这是机器来源，本地没有人在操作。
                        actor=(
                            actor_name
                            if isinstance(actor_name, str) and actor_name
                            else binding.peer_account
                        ),
                        # ⭐ `actor_id` 留空是**有意的**（机器来源），
                        # 但必须**显式**声明 `system=True`——否则 `record()` 的 fail-closed
                        # 判据会把「漏传主键」与「机器来源」混为一谈（见 `log.py`）。
                        system=True,
                        target_kind="order",
                        order_no=order.order_no,
                        source_order_no=order.source_order_no,
                        to_value="已退单",
                        reason=WITHDRAW_REASON_TEXT[withdraw_reason],
                    )
                else:
                    # ⭐ （业务方要求补的缺失）：**已建链的后续事件**落地成功也写一条订单日志（指派 / 接单 / 更新）。⛔ 撤单**不在这里**——它沿用上面那条 `order.peer_withdraw`，⛔ 不写第二行（同一件事两行日志会把订单详情读成两笔撤单）。
                    # ⚠️ 位置在水位推进之后（本支所有 `continue` / `set_rollback` 出口都已过）且仍在同一个 `atomic()` 内 ⇒ 事件没生效就不会有日志。
                    # ⭐ 案 B1（D-0364）：`inbound_from_value` = 本地旧状态 label（只有 order.update 支填它；状态没被这条事件改时它是空串 ⇒ 渲染器把状态段整段剔除，R3 / 池内豁免行不留空壳）；⭐ D-0343：已结账挡住那一档点名「为什么没自动清人」、案 A：池内豁免档写「状态保持」（其余取值恒为空串 = 行为一字不变）。
                    _record_peer_sync(
                        binding,
                        action=_PEER_INBOUND_ACTION_BY_KIND.get(
                            kind, "peer.inbound_update"
                        ),
                        order=order,
                        from_value=inbound_from_value,
                        to_value=_peer_status_label(order.status),
                        # ⭐ D-0343：已结账挡住那一档，这里点名「为什么没自动清人」；
                        # ⭐ 案 A（D-0364）：池内豁免档写「状态保持」。
                        #   （其余取值恒为空串 = 行为一字不变。）
                        reason=inbound_note,
                    )
        applied += 1

    return Outcome(applied=applied, skipped=skipped, skipped_details=skipped_details)


# ── 上行侧（D-0303）：上家收下家的**抢单请求** ────────────────────────────────
def _hall_throw_delivered(binding, *, target_ref, alt_refs=()) -> bool:
    """这张单**曾经真的通过 `hall.throw` 投递到**这条下家绑定吗（Z20）。判据 = `(direction="out", kind="hall.throw", binding_role="downstream", binding_id=<binding pk>, target_ref=<源头单号>)` 且 **`status="done"` ＋ `reason=""`**。
    ⚠️ **只判「有这一行」不够**（审查原文的改准点）：同一形状还留着两类**不是共享**的行——① 投递失败 ⇒ `failed`；② `hall.take` 把未投的 throw **作废** ⇒ `done` ＋ 非空 `reason`（`peer_dispatch._void_pending_hall_throw`）。两类都说明「对端从没在大厅里看见过这张单」，拿它们放行等于留一个「猜单号就能接走没共享过的单」的口子 ⇒ 两条列一起进判据。
    ⚠️ 多跳同样成立（**两条 claim 支都调它**：原生支 `_apply_upstream_hall_claim` 与镜像支 `_forward_mirror_claim`）：中间层把镜像卡往下转发时也用 `kind="hall.throw"` / `direction="out"` / `binding_role="downstream"`（`enqueue_hall_mirror_event`），`target_ref` 是**卡的源头单号** ⇒ 同一个判据对原生单与镜像卡都读得通。⭐ `alt_refs`（**兼容，承重**）：同一张单在旧口径下投递过的 `hall.throw` 用的是**内置号**（`peer_order_no` = 当年那个号），而升级后新投的是**来源号** ⇒ 老下家拿旧号来 claim 时，同一个事实存在**两把键**里的任意一把；判据因此收 `{target_ref} ∪ alt_refs`——两把键都指**同一张单、同一条绑定**，放宽不改变 Z20 的安全语义（仍然要求「真投递成功过」，⛔ 不是「有这张单就行」）；⛔ 别拿它去兜别的东西：调用方只传 `effective_source_order_no(那张单)`，不传任意猜测键。"""
    from apps.business.models import PeerSyncEvent

    refs = {target_ref}
    for ref in alt_refs:
        if isinstance(ref, str) and ref:
            refs.add(ref)
    if PeerSyncEvent.objects.filter(
        direction="out",
        kind=HALL_THROW_KIND,
        binding_role="downstream",
        binding_id=str(binding.pk),
        target_ref__in=refs,
        status=PeerSyncEvent.Status.DONE,
        reason="",
    ).exists():
        return True
    # ⭐ 计划 `2026-10-25-peer-hall-pull` §2：**拉取证据**是第二类来源。上家端点把真正返回给这条绑定的每一张卡记成 `direction="in" / kind="hall.pull"`；判据是**两个 `exists()` 的 or** ⇒ 绑定前就在厅、从没被推送过的卡，下家拉过一次就能接（否则「点一次拒一次」）。
    #    ⚠️ 第二个 `exists()` 的过滤必须写全（direction / kind / binding_role / binding_id / status / reason）：放宽成「有过任意 pull」= 拉一次整厅可接，是安全倒退。
    return PeerSyncEvent.objects.filter(
        direction="in",
        kind=HALL_PULL_KIND,
        binding_role="downstream",
        binding_id=str(binding.pk),
        target_ref__in=refs,
        status=PeerSyncEvent.Status.DONE,
        reason="",
    ).exists()


def _event_wire_shape(event) -> dict:
    """`PeerSyncEvent` 行 → **出站事件线形状**（⭐ D-0347，同步抢单的响应车用）。与 `peer_worker._attempt` 真投递时发的形状**同一条**（`idempotency_key` / `kind` / `target_ref` / 事件级 `meta` 展开在顶层、`payload` 收尾）——两处各写一份迟早漂，⛔ 改形状必须两处同一次改；接收侧不读 `idempotency_key`（既有口径），带上只是让这条线形状与真投递逐字可比。"""
    return {
        "idempotency_key": event.idempotency_key,
        "kind": event.kind,
        "target_ref": event.target_ref,
        **(event.meta or {}),
        "payload": event.payload,
    }


def _refuse_hall_claim(binding, *, target_ref, note=""):
    """排一条 `hall.claim_refused` 应答（可附一句**本地**留痕人话），回那行应答事件。⚠️ 应答载荷**刻意为空对象**（协议里答案只在 kind 与 ref 里，见 `peer_dispatch.enqueue_hall_claim_reply`）⇒ 人话只落在我们自己的台账列 `reason` 上，⛔ 不外发、也⛔ 不往 payload 塞第二份真相；只写**仍 `pending`** 的应答行（条件更新）：同一窗口里已有应答被投出去时，改它的 `reason` 只是给一条已完成的台账补话，没有意义。
    ⭐ D-0347：返回那行应答事件（同步抢单的响应车要把它搭车带回下家；既有调用方不接返回值，行为逐字不变）。"""
    from apps.business import peer_dispatch
    from apps.business.models import PeerSyncEvent
    from apps.business.peer_sync import HALL_CLAIM_REFUSED_KIND

    event = peer_dispatch.enqueue_hall_claim_reply(
        binding,
        kind=HALL_CLAIM_REFUSED_KIND,
        target_ref=target_ref,
        # ⭐ 业务方：拒绝原因要**随应答过线**（接收侧据此分永久 / 可重试）。
        # ⚠️ 同一句人话既进载荷、也进本地台账列（⛔ 不写第二套措辞）。
        reason=note,
    )
    if note:
        # ⚠️ 幂等命中（同一窗口里已有一条 pending 应答）时 `enqueue` **不改载荷** ⇒
        #    这里把「拒绝原因」一起补上，否则对端会拿到一条**没有原因**的拒绝、按可重试
        #    处理（该下架的卡就下不了架）。⛔ 只改仍 pending 的行。
        PeerSyncEvent.objects.filter(
            pk=event.pk, status=PeerSyncEvent.Status.PENDING
        ).update(
            reason=note[:255],
            payload={CLAIM_REFUSED_REASON_KEY: note},
        )
        logger.info(
            "大厅镜像：%s binding=%s ref=%s", note, binding.pk, target_ref
        )
    return event


def _claim_already_accepted(binding, *, target_ref) -> bool:
    """这个 `hall.claim` 是不是这条绑定**自己那次已受理请求**的重放（⭐ A9 / L-7）。判据 = **PeerSyncEvent 里现存给这条绑定排过的 `hall.claim_ok` 应答行**（`direction="out"`、`binding_role="downstream"`、该绑定、该 ref——任何状态：`pending` 是「已受理、应答在途」的窗口，`done` / `failed` 是它投递过后的余温）；这是计划 A9 写明的「在飞 / 已受理证据」，重放窗口 = 投递退避梯（~81 分钟）比台账保留期（90 天）短几个量级 ⇒ 以台账行为判据是稳的。
    ⚠️ **有意不认「MAIN 接手人 = 绑定账号」当证据**（实测的误报形状）：单被**别的路**先接走、接手人恰好就是绑定账号（`claim_peer_mirror` 之外的任意指派）时，claim 还**从来没被受理过**——那一刻正确答复是永久拒绝（卡下架），回 `claim_ok` 反而造幽灵（下家等一条永远不来的 `order.assign`）；`tests/test_peer_hall_claim.py` 的「上家单已被别人接走」两条用例钉住这个边界。
    ⚠️ 判据**只认这一条绑定**：换一家下家来要同一张单 ⇒ 无应答行 ⇒ 走既有的「已不在大厅」拒绝，逐字不变；纯读（一个查询，⛔ 不写）——调用方据此**改答复**，不做任何业务动作。"""
    from apps.business.models import PeerSyncEvent

    return PeerSyncEvent.objects.filter(
        direction="out",
        kind=HALL_CLAIM_OK_KIND,
        binding_role="downstream",
        binding_id=str(binding.pk),
        target_ref=target_ref,
    ).exists()


def _apply_upstream_hall_claim(binding, *, target_ref, note_skip) -> tuple[bool, dict | None]:
    """上家处理下家的 `hall.claim`（spec §4.2）：单还在大厅就指派给绑定账号，否则回绝。⭐ **D-0347 起返回 `tuple[bool, claim_reply]`**（既有调用方不接第二格，行为不变）：`claim_reply` 是这次判决的**响应车**——`{"target_ref", "outcome", "reply_events"}`（reply_events = 刚排好的应答事件线形状，`_event_wire_shape`），由 `apply_upstream_events` 累积进 `Outcome.claim_replies`、搭本次 HTTP 响应带回下家，下家的同步抢单（`peer_hall.claim_peer_mirror_sync`）据此在请求内收口；各档 outcome：`accepted`（原生单受理，带 `order.assign` + `hall.claim_ok` 两条）/ `refused`（带 `hall.claim_refused`）/ `replay`（A9 重放口，只带 `claim_ok`——下家据此**给不出终局**，按未响应处理）/ `forwarded`（多跳转发，空事件——判决在更上游）；⛔ **outbox 照常排这些行**：响应车不替代投递，响应丢失时异步重发兜底（幂等见 `Outcome` docstring）。
    这一跳有**两种形态**，都从同一个入口进来（下游只知道卡上的单号）：① **本租户的原生在厅单** ⇒ 走既有机器接手路（`claim_order` 的 `enforce_limits=False` 档，裁定 8：外部事实已定——下游打手在他那边**已经过了**他自己的抢单闸；被我们的按人闸门再拦一次只会造出「对方在做、我方没接」的错位；⛔ 不改 `claim.py` 对内部单的任何闸判定，只是走它已有的机器档）；成功 ⇒ 显式排**既有 `order.assign`** 推送 ＋ 一条 `hall.claim_ok` 应答，单不在厅 ⇒ 排 `hall.claim_refused`；② ⭐ **本租户是镜像行**（这张卡是**从本租户的上家**镜像来的，share-design §5.3.1 第 1/2 条）⇒ **逐跳转发**（`_forward_mirror_claim`）：占住那一行（`claim_downstream` / `claim_id` / `claiming_at`）、用**这一行自己的**上家绑定向它的上家排一条 `hall.claim`；⚠️ 这一档**当场不回**任何应答——上游的 `hall.claim_ok` / `hall.claim_refused` 会作为**下行事件**回到本层，由 `_apply_hall_event` 转给 `claim_downstream`（那才是下游那张卡收到答复的时刻）；③ 两者都没有 ⇒ `hall.claim_refused`（单号不存在 / 已接走 / 已撤下 / 已软删——同一答复，不泄漏「这里有没有这张单」）；④ ⭐ **Z20**：原生单存在且在厅，还要再核一道**来源**——这张单**曾经真的通过 `hall.throw` 投递到这条下家绑定**（`_hall_throw_delivered`：`done` ＋ 空 `reason`，⛔ 不是「有这一行就算」），没投递过 ⇒ 回绝——没有这道判据，任何持有绑定令牌的下家都能**猜单号**直接要走一张从没共享给它的单。
    ⚠️ **逐条回绝**（`return True` + 一条 `hall.claim_refused`），⛔ 不 `note_skip` 整条跳过——跳过会让发送侧把这次请求记成「投递成功但被忽略」，对端既拿不到答复、也不会重试，卡在他那边永远停在 `claiming`。⚠️ **半个 claim 的回滚是承重的**：`claim_order` 的绩效条件更新在状态机判定**之前**——节点门 / 来源态守卫拒绝时那半截行**已经写下去**（它的 docstring 明说，调用方必须显式回滚）；本函数用**内层 `transaction.atomic()`（savepoint）**接住三种预期失败（`ClaimError` / `IllegalTransition` / ⭐ Z8 的 `IntegrityError`），异常出块 = savepoint 回滚 = 半截行消失，外层整批事务不受影响（E-0022 同族：块内不裸 `return`，先出块再应答）。⚠️ 节点门照走（`nodes_of(binding.account)`，照 `jidanpu_member.machine_claim_order` 的先例）：绑定账号没有接单节点 ⇒ `IllegalTransition` ⇒ 回绝——那是绑定配置问题，回绝 + 下家行上可见比静默挂死诚实；⭐ **Z8**：`claim_order` 撞上数据库完整性约束（`IntegrityError`，例如同单并发写下第二条 MAIN 绩效行）同样是**可预期的拒绝** ⇒ 回绝 + 人话，⛔ 不 500（照 `machine_claim_order` 的先例：裸逃会让整批 500，对端反复重投同一个毒批）；回 `True`（= applied）的口径与截图一致：请求被**处理**了（应答已排 / 已转发），⛔ 不把「没抢到」记成 skip——那会让发送侧退避重投一条注定失败的请求。"""
    from django.utils import timezone

    from apps.business import peer_dispatch
    from apps.business.claim import ClaimError, claim_order
    from apps.business.models import Order, OrderStatus
    from apps.business.order_status import IllegalTransition
    from apps.business.peer_sync import (
        HALL_CLAIM_OK_KIND,
        HALL_CLAIM_REFUSED_KIND,
    )
    from apps.rbac.permissions import nodes_of

    # ⛔ 走默认 manager：已软删的单取不到 ⇒ 与「不在大厅」同档回绝（fail-closed）。
    # ⭐ 按**同步身份键**解析（来源号或老口径的内置号，唯一解析口
    #    `_local_order_for_peer_ref`）——下家升级后 claim 里带的是**来源号**。
    order = _local_order_for_peer_ref(target_ref)
    if order is None:
        # ⭐ P4：本租户没有这张原生单 ⇒ 看看它是不是**我镜像来的**那张卡（多跳中间层）。
        #    ⚠️ 顺序是承重的：原生单优先——同号时「我方真单」必须是判决对象，
        #    ⛔ 不许被一张旧镜像行抢在它前面判掉（那会让一张真实在厅单永远接不了）。
        card = _claimable_mirror_row(target_ref)
        if card is None:
            # ⭐ 订单已不存在（本租户既没有这张原生单、也没有镜像卡）⇒ **永久**拒绝：
            #    下游那张卡要彻底下架（回大厅永远抢不到）。
            refused = _refuse_hall_claim(
                binding, target_ref=target_ref, note=CLAIM_REFUSED_NOTE
            )
            return True, {
                "target_ref": target_ref,
                "outcome": "refused",
                "reply_events": [_event_wire_shape(refused)],
            }
        return _forward_mirror_claim(binding, row=card, target_ref=target_ref), {
            "target_ref": target_ref,
            # 多跳转发：判决在更上游，本层当场给不出终局（下家按「未响应」放回重试）。
            "outcome": "forwarded",
            "reply_events": [],
        }
    if str(order.status) != str(OrderStatus.WAITING_GRAB):
        # ⭐ A9 / L-7：单已不在大厅时，先分「是不是它自己那次**已受理** claim 的重放」——判重成立 ⇒ 回 `claim_ok`、⛔ 不重新处理（不重跑 `claim_order`、也不排第二条 `order.assign`）。
        #    没有这一闸的重放序（L-7 现场）：受理成功 → 应答在途丢了 → 下游重投同一请求 → 这里按「已不在大厅」回**永久拒绝** → 下游把一张已受理的卡下架 / 拉回（随后迟到的 `order.assign` 落成无主单）。
        if _claim_already_accepted(binding, target_ref=target_ref):
            replayed = peer_dispatch.enqueue_hall_claim_reply(
                binding, kind=HALL_CLAIM_OK_KIND, target_ref=target_ref
            )
            return True, {
                "target_ref": target_ref,
                # 只带 `claim_ok`、不带 `order.assign` ⇒ 下家据此给不出终局（按未响应）。
                "outcome": "replay",
                "reply_events": [_event_wire_shape(replayed)],
            }
        # ⭐ 单还在、但**已不在大厅**（被接走 / 撤回 / 暂停）⇒ 同一条永久拒绝。
        refused = _refuse_hall_claim(
            binding, target_ref=target_ref, note=CLAIM_REFUSED_NOTE
        )
        return True, {
            "target_ref": target_ref,
            "outcome": "refused",
            "reply_events": [_event_wire_shape(refused)],
        }
    # ⭐ Z20：来源校验（位置在「单存在且 `WAITING_GRAB`」**之后**：前三档的答复语义不变，⛔ 不让「不在厅」这条与「没共享」混成同一句话——虽然对外答复同形，本地台账要分得开）；⭐ 两把键都认（老下家用旧内置号 claim、新下家用来源号 claim）——见 `_hall_throw_delivered` 的 `alt_refs` 段。
    if not _hall_throw_delivered(
        binding,
        target_ref=target_ref,
        alt_refs=(effective_peer_ref(order),),
    ):
        refused = _refuse_hall_claim(
            binding, target_ref=target_ref, note=CLAIM_REFUSED_NOT_SHARED_NOTE
        )
        return True, {
            "target_ref": target_ref,
            "outcome": "refused",
            "reply_events": [_event_wire_shape(refused)],
        }
    assigned = False
    conflict = False
    try:
        with transaction.atomic():
            claim_order(
                user_id=binding.account_id,
                order_id=order.pk,
                nodes=nodes_of(binding.account),
                # 机器接手（裁定 8）：跳过按人闸门；押金判据已移出 `enforce_limits`，
                # 机器路径必须显式关（照 `machine_claim_order` 逐字）。
                enforce_limits=False,
                enforce_deposit=False,
            )
            # ⭐ （业务方要求补的缺失）：下家在我方大厅**接单成功** ⇒ 写一条订单日志（订单详情里看得出「下家什么时候接的单」）。位置在接手写入的**同一个 `atomic()` 里** ⇒ 接手回滚时日志一起消失；被回绝的三档（`ClaimError` / `IllegalTransition` / `IntegrityError`）走不到这里。
            _record_peer_sync(
                binding,
                action="peer.hall_claim",
                order=order,
                to_value=_peer_status_label(OrderStatus.IN_PROGRESS),
            )
            assigned = True
    except (ClaimError, IllegalTransition):
        assigned = False
    except IntegrityError:
        # ⭐ Z8：可预期的数据冲突（拆走半截写入由内层 savepoint 回滚）⇒ 回绝，⛔ 不 500。
        assigned = False
        conflict = True
    if not assigned:
        refused = _refuse_hall_claim(
            binding,
            target_ref=target_ref,
            note=CLAIM_REFUSED_CONFLICT_NOTE if conflict else "",
        )
        return True, {
            "target_ref": target_ref,
            "outcome": "refused",
            "reply_events": [_event_wire_shape(refused)],
        }
    # 成功：既有 order.assign 推送带正式订单给下家（转正收口在下家侧的回填）。
    # ⛔ 时刻必须显式给（修复波 I-1 同一条）：claim 期间 `apply_transition` 里已经先入队了一条 `order.update`（状态漏斗对 `IN_PROGRESS` 的下行），两条事件共用 `order.updated_at` 会让更全的 `order.assign` 被对端判 `stale` 顶掉。
    assigned_event = peer_dispatch.enqueue_order_assigned(
        order, binding.account, occurred_at=timezone.now()
    )
    if assigned_event is not None:
        from apps.internal_chat.order_groups import authorize_claim

        invitation = authorize_claim(order, binding)
        if invitation is not None:
            assigned_event.payload = {**assigned_event.payload, "chat_group": invitation}
            assigned_event.save(update_fields=["payload"])
    claim_ok_event = peer_dispatch.enqueue_hall_claim_reply(
        binding, kind=HALL_CLAIM_OK_KIND, target_ref=target_ref
    )
    # ⭐ D-0347：`assign` 理论上恒非空（能来 claim 的绑定就是 ACTIVE 的下行绑定，
    #    `enqueue_order_event` 按同一账号找回它）；形状兜底只带 `claim_ok` ⇒ 下家
    #    据此按「未响应」处理（E-0021 同族：不拿「应该有」当「一定有」）。
    reply_events = [
        _event_wire_shape(event)
        for event in (assigned_event, claim_ok_event)
        if event is not None
    ]
    return True, {
        "target_ref": target_ref,
        "outcome": "accepted",
        "reply_events": reply_events,
    }


# ── 上行侧：上家收下家的**状态回传** ─────────────────────────────
def _upstream_accepted() -> frozenset[str]:
    """上家**接受**的下家回传状态 = `UPSTREAM_STATUS_MAP` 的**值域**。
    ⛔ 从 `UPSTREAM_STATUS_MAP` 派生、⛔ **不另抄一份**：抄一份就与发送侧的映射表漂移（对端改了映射、这里还按旧集合收，症状是回传被静默硬跳过）。
    ⚠️ 值是 `{"PENDING_REVIEW", "ISSUE", "PAUSED"}`——`UPSTREAM_STATUS_MAP` 把下家的 `COMPLETED` **也映成** `PENDING_REVIEW`（业务方原话：「已完成只能由本系统自己更改」），而 `REFUNDED` **根本不在**映射表里（下家没有「退单上行」这条裁定）。"""
    from apps.business.peer_sync import UPSTREAM_STATUS_MAP

    return frozenset(UPSTREAM_STATUS_MAP.values())


def apply_upstream_events(events, *, binding) -> Outcome:
    """上家接收**下家的状态回传**（P3；`binding` 是命中令牌的 `PeerDownstreamBinding`）。⚠️ 与 `apply_events`（下行）**刻意分开**：那个函数会建单 / 建游戏 / 写撤单日志，而上行**只改状态**（业务方：下家只回传状态）⇒ ⛔ 不建单、⛔ 不碰价格 / 备注 / 游戏 / 归属、⛔ 不走 `apps/business/order_status.py::apply_transition`（那条入口按目标态注册副作用，没人操作时不该跑）；状态回传落地成功后写一条 `peer.upstream_status`（`hall.claim` 受理成功写 `peer.hall_claim`、截图真增删写 `peer.upstream_screenshot`；「跳过 / 重投不写」与唯一写出口见模块 docstring 的「操作日志」一节）。
    ⭐ **D-0303 的例外**：`hall.claim`（下家的抢单请求）也走本支（spec §4.2）——它是「指派」不是「状态回传」，走 `_apply_upstream_hall_claim`（机器接手 + 既有 `order.assign` 推送 + claim 应答），其余 kind 的语义一字不变。
    ⚠️ **安全判据（承重）**：一条上行事件只能改**「当前 MAIN 接手人 = 这条绑定的账号」**的那张单，否则硬跳过 `not_your_order`——⛔ 不接受对端用 `target_ref` 指认别人的单：令牌只能证明"你是这个下家"，不能证明"这张单是你的"（spec §9 的跨租户 / 越权边界）。形状校验在事务外；写入前在锁下重验归属、时刻和结账状态。⚠️ **冲突裁决 = 最后更改者胜**：上家侧没有 `PeerOrderLink`（它不建单、只推）⇒ 本地动作时刻用 `order.updated_at` 作**代理**；`occurred_at <= order.updated_at` ⇒ 良性 `stale`（与下行按 `link.last_applied_at` 裁决同口径，⛔ 相等也算"重投/更旧"）。代价不在时间精度（`auto_now=True` 的 `DateTimeField`，PostgreSQL `timestamptz` 存微秒，`isoformat` / `parse_datetime` 往返不掉精度），而在 `updated_at` 量的是**"最后一次写这张单的时刻"**：任何一次无关写入都会推高它、与本次业务动作无关（登记的残差）。⚠️ 应用状态时 `update_fields` 必须**同时**列 `status` 与 `updated_at`：`Order.updated_at` 是 `auto_now=True`，而 `pre_save` 只对 `update_fields` 里列出的字段跑 ⇒ 漏掉它这一列一个字都不改，「最后更改者胜」会把**下一条**回传判成 stale（自己判死自己）；`tests/test_peer_p3_inbound_up.py` 那条 `…updated_at_advances…` 用例钉住这一点。"""
    from apps.business import peer_direct_status
    from apps.business.models import EmpType, Order, Performance

    from apps.internal_chat import order_groups as chat_groups

    applied = 0
    skipped = 0
    skipped_details: list[dict] = []
    # ⭐ D-0347：上行 `hall.claim` 的判决（响应车；见 `Outcome.claim_replies`）。
    claim_replies: tuple = ()

    def note_skip(target_ref: str, code: str, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        skipped_details.append({"target_ref": target_ref, "code": code, "reason": reason})

    accepted = _upstream_accepted()
    for raw in events:
        # ⚠️ 逐条先归一形状：`events` 是**外部来的数组**，里面可能是字符串 / 数字 / 缺 payload
        # 的 dict ⇒ 直接 `.get` 会 500（E-0010 的"这类请求还会不会 500"）。
        if not isinstance(raw, dict):
            note_skip("", SKIP_BAD_SHAPE, "事件不是对象")
            continue
        # ⭐ 裁决 R-P4-23（与下行支**同一条**判据、同一出口）：碰任何 DB 之前先判"存得下吗"。
        #    上行支同样会写 `Order` / `OrderScreenshot` 的文本列（目标态来自对端，截图 key /
        #    备注来自对端）⇒ 一个 NUL 或孤立代理项以前会把整批打成 500。
        if _has_unstorable_text(raw):
            note_skip(
                _reported_ref(raw.get("target_ref")), SKIP_BAD_SHAPE,
                "事件里有 PostgreSQL 存不下的字符串（NUL 或孤立代理项）",
            )
            continue
        payload = raw.get("payload")
        # ⭐ P4 挂账⑨（与下行支**同一把闸**、同一出口）：`target_ref` 在这一支是**精确查找键**（下面交给 `_local_order_for_peer_ref`：`Order.order_no` 或 `source_order_no`）⇒ 非空字符串、装得进 `PeerOrderLink.peer_order_no` 列宽；⚠️ **比的是这一列、不是 `Order.order_no`**（64 > 20）：21–64 字的 ref 在这里不判 `bad_shape`，而是走到下面的 `unknown_order`（两列都命中不了）。两条出口又都是硬跳过 ⇒ **有意不改这个界**，只在跳过原因里写清比的是哪一列（`_reference_key_shape_error` 的消息已点名列）；⛔ **绝不截断**（截断后可能命中另一张单）。
        #    ⚠️ `checked_fields=()` 是**有意的**：这一支只读 `status`（已有白名单闸）与截图三键（已有截图形状闸），那些订单字段**根本不落任何列** ⇒ 不把它们塞进来，否则一条只改了状态的合法回传会因为一个多余的键被判 `bad_shape`。
        shape_error = _peer_value_shape_error(raw, payload, checked_fields=())
        if shape_error is not None:
            note_skip(
                _reported_ref(raw.get("target_ref")), SKIP_BAD_SHAPE, shape_error
            )
            continue
        # 闸已保证 `target_ref` 是**非空字符串**且装得进列宽 ⇒ 下面都是真 `str`（⛔ 不再 `or ""`）。
        ref = raw["target_ref"]
        if not isinstance(payload, dict):
            note_skip(ref, SKIP_BAD_SHAPE, "payload 不是对象")
            continue
        kind = raw.get("kind")
        if kind in chat_groups.EVENT_KINDS:
            error = chat_groups.receive(
                kind, payload, binding=binding, binding_role=chat_groups.DOWNSTREAM, target_ref=ref,
                event_key=raw.get("idempotency_key"),
            )
            if error is None:
                applied += 1
            else:
                note_skip(ref, SKIP_BAD_SHAPE, error)
            continue
        # ⭐ **上行支也要处理截图事件**（裁决 R-P4-15.1）。落地逻辑与下行支**同一份**（`_apply_screenshot`：形状闸 / 幂等 / 删行）；⚠️ 位置在 `occurred_at` 形状闸**之前**：截图不参与新旧裁决（R-P4-15.3），⛔ 不因为对端没带时刻就丢掉它。
        #    ⚠️ 上行支没有 `PeerOrderLink`（它不建单）⇒ 单号按**同步身份键**找（`_local_order_for_peer_ref`：来源号 / 老口径的内置号都认）；下面那条**承重安全判据**（当前 MAIN 接手人 = 这条绑定的账号）照旧适用：截图行也是挂在这张单上的数据，令牌只能证明"你是这个下家"，不能证明"这张单是你的"。
        if kind in SCREENSHOT_KINDS:
            shot_order = _local_order_for_peer_ref(ref)
            if shot_order is None:
                note_skip(ref, SKIP_UNKNOWN_ORDER, "截图事件指向一张本地没有的订单")
                continue
            shot_perf = (
                Performance.objects.filter(order=shot_order, emp_type=EmpType.MAIN)
                .select_related("user")
                .first()
            )
            if shot_perf is None or shot_perf.user_id != binding.account_id:
                note_skip(
                    ref, SKIP_NOT_YOUR_ORDER, "这张单的当前接手人不是这条绑定的账号"
                )
                continue
            if _apply_screenshot(
                shot_order, kind=kind, payload=payload, binding=binding,
                target_ref=ref, note_skip=note_skip,
                direction="upstream",
            ):
                applied += 1
            continue
        # ⭐ D-0303：**上家收下家的抢单请求**（spec §4.2）。与截图同一位置纪律：排在 `occurred_at` 形状闸之前——claim 的裁决依据是**当下的大厅状态**（单还在不在 `WAITING_GRAB`、MAIN 行还空不空），不是事件时刻的新旧；载荷里的 `claim_by` **v1 仅透传、上家不消费**（M6）。
        if kind == HALL_CLAIM_KIND:
            applied_flag, claim_reply = _apply_upstream_hall_claim(
                binding, target_ref=ref, note_skip=note_skip
            )
            if applied_flag:
                applied += 1
            if claim_reply is not None:
                # ⭐ D-0347：判决搭响应车（见 `Outcome.claim_replies`）。
                claim_replies = claim_replies + (claim_reply,)
            continue
        occurred_at = _parse_event_time(raw.get("occurred_at"))
        if occurred_at is None:
            note_skip(ref, SKIP_BAD_SHAPE, "这条事件没有可用的 occurred_at")
            continue
        target_status = payload.get("status")
        if not isinstance(target_status, str) or target_status not in accepted:
            # ⛔ 不接受的状态**响亮跳过**（⛔ 不回落、⛔ 不另造 code）：下家只该发映射后的值，
            #    发来别的说明对端口径不对，静默吞掉会让它对账时看不见。
            note_skip(
                ref, SKIP_UNKNOWN_STATUS,
                f"未知或不被接受的状态：{str(target_status)[:60]!r}",
            )
            continue
        # 锁外预判减少无效加锁，锁内仍须重验所有会变化的条件。
        # ⭐ 按**同步身份键**解析（新对端发来源号、老对端发内置号，唯一解析口 `_local_order_for_peer_ref`）——只按 `Order.order_no` 查会让升级后的下家回传一律 `unknown_order`（状态回传永久丢失）。
        direct = None
        if kind == "order.update" and peer_direct_status.WIRE_FIELD in payload:
            direct = peer_direct_status.parse_identity(payload[peer_direct_status.WIRE_FIELD])
            if direct is None:
                note_skip(ref, SKIP_BAD_SHAPE, "直派状态回传缺少可用的指派操作号或订单号")
                continue
        order = _local_order_for_peer_ref(direct[1] if direct is not None else ref)
        if order is None or (direct is not None and order.order_no != direct[1]):
            note_skip(ref, SKIP_UNKNOWN_ORDER, "回传指向一张本地没有的订单")
            continue
        if direct is None:
            # ⭐ 承重安全判据：当前 **MAIN** 接手人必须就是这条绑定的账号。
            #    ⛔ 别放宽成"有任意一张绩效行归它"：SALES 行装的是销售归属，不是接手人。
            perf = (
                Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
                .select_related("user")
                .first()
            )
            if perf is None or perf.user_id != binding.account_id:
                note_skip(ref, SKIP_NOT_YOUR_ORDER, "这张单的当前接手人不是这条绑定的账号")
                continue
            if order.updated_at is not None and occurred_at <= order.updated_at:
                note_skip(ref, SKIP_STALE, "回传不晚于本地最后一次动作（本地更新）")
                continue

        with transaction.atomic():
            order = Order.objects.select_for_update().filter(pk=order.pk).first()
            if order is None:
                note_skip(ref, SKIP_UNKNOWN_ORDER, "回传指向一张本地没有的订单")
                continue
            handoff = None
            if direct is not None:
                if order.peer_direct_pending is not None:
                    if peer_direct_status.stage_status(order, binding, raw, direct[0], occurred_at):
                        applied += 1
                    else:
                        note_skip(ref, SKIP_NOT_YOUR_ORDER, "这次直派不属于这条绑定")
                    continue
                handoff = peer_direct_status.find_handoff(order, binding, ref, direct[0])
                if handoff is None:
                    note_skip(ref, SKIP_NOT_YOUR_ORDER, "这次直派不属于这条绑定")
                    continue
            # 与结账按相同顺序锁定订单和绩效，防止回传覆盖刚结账的订单。
            performances = list(
                Performance.objects.select_for_update().filter(order=order).order_by("pk")
            )
            perf = next((row for row in performances if row.emp_type == EmpType.MAIN), None)
            if perf is None or perf.user_id != binding.account_id:
                note_skip(ref, SKIP_NOT_YOUR_ORDER, "这张单的当前接手人不是这条绑定的账号")
                continue
            if handoff is not None:
                fresh = peer_direct_status.status_is_fresh(order, handoff, occurred_at)
            else:
                fresh = order.updated_at is None or occurred_at > order.updated_at
            if not fresh:
                note_skip(ref, SKIP_STALE, "回传不晚于最后一次已应用的状态或本地更新")
                continue
            if any(row.is_settled for row in performances):
                note_skip(ref, SKIP_STALE, "本地报酬已结账，保持订单状态")
                continue
            if any(row.online_payroll_batch_id for row in performances):
                note_skip(ref, SKIP_STALE, "本地报酬正在在线结算，保持订单状态")
                continue
            persist_order_status(order, target_status, event_at=occurred_at)
            if handoff is not None:
                peer_direct_status.advance_handoff(order, handoff, occurred_at)
            # 两类回调留在本次事务内，沿用同一个本地入队时刻。
            from apps.openapi import dispatch as openapi_dispatch
            from apps.openapi.credentials import OPENAPI_SPEC
            from apps.takerapi.api import callback_recipients as taker_callback

            # ⚠️ 两族共用**同一个**时刻对象（同一次动作、两个当事人）。
            moment = timezone.now()
            openapi_dispatch.notify_order_status_changed(
                order,
                spec=OPENAPI_SPEC,
                status=target_status,
                occurred_at=moment,
            )
            # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 2 行后半）：**紧挨着**上面那句，同一个 `atomic()` 块、同一个 `moment`。⚠️ 本处**绕过状态机**直接写状态列（上家把我们这单的状态改了）——状态**真的变了**，所以打手侧也要推（回传回来的状态也是真实的本地状态，⛔ 不是回声）。
            taker_callback.notify_order_status_changed_for_taker(
                order, status=target_status, occurred_at=moment
            )
            # ⭐ （业务方要求补的缺失）：上家（我方）收下家的**状态回传**落地成功 ⇒ 写一条订单日志（「下家什么时候改了什么状态」）。
            # ⚠️ 位置在同一个 `atomic()` 里、`save()` 之后（同生共死）；本支所有跳过（`unknown_status` / `not_your_order` / `stale` / `bad_shape`）都在写入**之前** `continue` ⇒ 跳过不写、重投被判 `stale` 也不写。
            _record_peer_sync(
                binding,
                action="peer.upstream_status",
                order=order,
                to_value=_peer_status_label(target_status),
            )
        applied += 1

    return Outcome(
        applied=applied,
        skipped=skipped,
        skipped_details=skipped_details,
        claim_replies=claim_replies,
    )
