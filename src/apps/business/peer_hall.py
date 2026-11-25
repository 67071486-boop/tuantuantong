"""团团通之间的大厅镜像：**下家侧**的抢单领域层 + 大厅读面那一路（D-0303，spec=`docs/specs/2026-10-25-peer-hall-mirror.md` §4 / §7）。与鸡蛋铺上家线（`jidanpu_upstream.claim_upstream_mirror`）的两条本质差异：① **推式 + 确认制**——本线 claim 是事件（`hall.claim` 入 outbox、`peer_sync` worker 投递），正式订单由上家 `order.assign` 推回来才算数（spec §4.1）⇒ 条件更新 + 入队后即返回；⭐ **D-0347（业务方拍板「能抢就是抢到 抢不到就是抢不到」）改准**：人类单跳的两处接单端点改为**同步直连**（`claim_peer_mirror_sync`：≤3 秒当场成交 / 当场 1012 / 无结论当场放回 + 1017），「不打出站 HTTP、不等待转正」自此只对**机器路径与多跳转发**成立（照旧 outbox），「接单中」成功响应随之退役（成功 = 已成交）；② **价格 = 上家发单价 × 本绑定百分比**（`PeerUpstreamBinding.hall_price_percent` 1–100 默认 100），大厅展示价 = `remote_hall_price × 本绑定百分比 / 100`，唯一算法出处 = `display_price(row)`（⛔ 闸门 / 投影 / 转正各算一份是两套公式，必然漂移，`AGENTS.md` §5-E）。
接收侧开关（P1，默认关）`PeerUpstreamBinding.hall_sync_enabled` 管**收**：关着 = 行为等同「没有这条功能」——① `peer_inbound` 的 `hall.*` 分支整批跳过；② 大厅读口（内部 + takerapi）不投影；③ 接单端点拒绝（`HALL_SYNC_OFF_MESSAGE`）；⛔ **三处同一道判据**（看得见却接不了比看不见更糟，E-0288 的教训）。读面 `peer_hall_queryset` / `serialize_peer_row` / `peer_hall_price_in_range` / `serialize_peer_hall_rows` 照 `upstream_hall_queryset` 一族的**同源圈定**模式抄（筛选语义 / `server_id` 置空 / 权限卡点留给调用方 / 价格内存过）；行键表与鸡蛋铺镜像行**恰好同键**（`REMOTE_ROW_BASE` 基底 + 同一组合并键，`origin="peer"`）——前端「一张表渲染三路行」靠的就是键表同形，⛔ 不新增不删减键（spec §7 的禁改令）。
⭐ `hall.claim_refused` **按原因分因**（业务方拍板）：拒绝原因 = **订单已不存在 / 没共享给这家** ⇒ 卡**彻底下架**（`gone`）；= **网络超时 / 上家没响应** ⇒ 卡回 `listed` 等重试（判据与取值面在 `peer_inbound._apply_hall_event` 的 `CLAIM_REFUSED_PERMANENT_REASONS`，⛔ 不在这里另写一份）。P3 增量补两支（都**只动本地状态**）：`reclaim_stuck_claims`（**纯本地零网络**，挂常驻循环 `peer_sync` **每轮开头**：`claiming` 超时且无 `promoted_order` ⇒ 条件更新回 `listed`、清四个接单列、`last_error` 留人话、记台账；多跳（P4）中间层卡住的行走**同一支**）与 `claim_peer_mirror` 的**幂等重投**档（卡已 `promoted` 且那张正式单的 MAIN 绩效行**就是**这次来要的打手 ⇒ 回「正在处理中」（⛔ 不是「已被接走」）+ 重投一次 `order.grab`，⛔ 不重复建单、⛔ 不改价）。Z5 增量：`release_claiming_mirrors`——绑定被停用 ⇒ 这份绑定名下 `claiming` 的卡**当场退回 `listed`**（⚠️ 不是 `gone`：抢单没确认时上家那张单很可能还在厅里，`gone` 会误杀真单）。"""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.business.claim import ClaimError, GrabCandidate, check_grab_limits
from apps.business.jidanpu_common import REMOTE_ROW_BASE
from apps.business.models import (
    EmpType,
    OrderType,
    PeerHallMirror,
    PeerSyncEvent,
    Performance,
)
from apps.core.datetime_display import local_iso
from apps.core.error_codes import ErrorCode

#: 回收 / 重投这类「本地决策」的排障留痕（⛔ 不发信、不进汇总行）。
logger = logging.getLogger(__name__)

#: 本模块行在大厅里的 `origin` 标（与 `native` / `upstream` / `simulated` 并列的第四档）。
PEER_ORIGIN = "peer"

#: `source_system` 键的取值（鸡蛋铺那一路是 `"jidanpu"`；本线不是鸡蛋铺，⛔ 不冒用）。
PEER_SOURCE_SYSTEM = "peer"

#: 接收侧开关关着时接单端点回的那句**人话**（业务方 的口径：「还没打开上家大厅同步」）。⚠️ 与「绑定已停用 / 已解绑」那句**分开**：那一句让人去修绑定，这一句让人去**打开开关**——两件事、两个去处（⛔ 不许合成一句看不出该改哪）。单独常量让用例钉字面值（E-0024）。
HALL_SYNC_OFF_MESSAGE = "还没打开上家大厅同步"

#: ⭐ P3：`claiming` 卡多久算「卡死」（秒）。判据是 **`claiming_at < now - 60s`**（**严格小于**：恰好 60 秒**不**回收——「1 分钟不动」与「已到 1 分钟」是两件事，用例把这一档钉死）。
#: ⭐ **D-0335（业务方拍板「消掉抢单锁定」）**：这是打手点完接单之后**别人接不了**的最长窗口（业务方明确「哪有这种功能……结果卡着 不让别人接」⇒ 从 5 分钟压到 **1 分钟**级）。窗口 = 60 秒（本常量）+ `peer_sync` 轮间隔（`LOOP_SLEEP_SECONDS = 10`）+ 对端派单延迟（秒级）；推式确认制（D-0303）的异步本身绕不开，能消的是这个**兜底窗**。
RECLAIM_STUCK_CLAIMING_SECONDS = 60

#: 回收时写进镜像行 `last_error` 的**人话**（打手 / 客服在行上直接可见：这张卡为什么
#: 又回到大厅了、能不能再接）。单独常量让用例钉字面值（E-0024）。
RECLAIMED_NOTE = "接单超过 1 分钟仍未收到正式订单，已自动放回大厅，可以重新接单"

#: ⭐ B3 / L-8：回收时**带着「上家已摘牌」证据**（`gone_at` 非空）的行收口成 `gone`，写进行 `last_error` 的那句。**直接复用** `peer_inbound.CLAIM_REFUSED_AFTER_TAKE_NOTE`（refused-after-take 同族常量）：同一句「这张单在接单确认期间已从对方大厅撤下」描述的是**同一个事实**（claiming 期间 take 来过、写下 `gone_at` 证据）——两处各说一套迟早漂（M2 的分因判据就住在那边）。
#: 修前病根：回收不看 `gone_at`，带证据的死卡被放回 `listed` 让打手**再点一次**——而上家那张单早就不在厅里了，这一枪注定是被拒绝（L-8 的现场）。

#: 回收的**台账 kind**（`PeerSyncEvent`，`direction="in"` / `status="done"`）。⚠️ 与 `hall.*` 那六个**协议事件**分开命名：本行只是我们自己的决策留痕，⛔ 从不外发（`direction="in"` 不在 `peer_worker.claim_due_events` 的取活面里），也⛔ 不进 `HALL_INBOUND_KINDS`（对端永远不该收到它）。
RECLAIM_LEDGER_KIND = "hall.claim_recycle"


# ── ⭐ D-0347（业务方拍板「能抢就是抢到 抢不到就是抢不到」）：同步抢单直推 ──
#: 人类单跳的两处接单端点不再排 outbox 等 worker 投递，而是在**请求内**把 `hall.claim` 直接推到对端（复用既有 `peer-sync/inbound/events/` 端点与受理机），**最多等 3 秒**：受理 ⇒ 当场转正成交；回绝 ⇒ 当场 1012；给不出结论 ⇒ 当场放回大厅 + 1017「未响应」（迟到的判决按既有补派 / refused 支收口，零新机制）。
#: ⛔ 机器路径（`jidanpu_member.machine_claim_order` 的 peer 档）与多跳中间层转发**照旧走 outbox**（`queue_claim=True` 缺省不变）——后台接手无人等待、中间层判决在更上游。

#: 同步推的**超时秒数**（默认 **3.0**；env `PEER_CLAIM_SYNC_TIMEOUT` 可调：空 / 坏值 / 非有限数 ⇒ 回默认，夹紧 [0.5, 30]——比一拍更短没有意义、比 App 的等待更长相当于挂死）。
#: ⭐ **实测承诺（D-0347 登记）**：上线后第一天按本模块 `logger.info` 的耗时读数统计 p50 / p90 / 超时率回填 `docs/STATUS.md`，本值按实测调（env 可调，无需发版）。
PEER_CLAIM_SYNC_TIMEOUT = 3.0
PEER_CLAIM_SYNC_TIMEOUT_ENV = "PEER_CLAIM_SYNC_TIMEOUT"
PEER_CLAIM_SYNC_TIMEOUT_MIN = 0.5
PEER_CLAIM_SYNC_TIMEOUT_MAX = 30.0

#: 同步推给不出终局时回给打手的**人话**（响应 `msg`；1017 `PLATFORM_QUERY_FAILED` 同码，
#: 与上游族「暂时无法接单」同一家族口径）。单独常量让用例钉字面值（E-0024）。
PEER_CLAIM_UNRESPONSIVE_MESSAGE = "对端未响应，请稍后重试"
#: 同一把伞下写进镜像行 `last_error` 的**行上人话**（客服在行上看这张卡为什么回来了；
#: 与上面那句**分开**——一句是设备提示、一句是行上留痕，⛔ 不共用一句）。
PEER_CLAIM_UNRESPONSIVE_NOTE = "对端未响应，已放回大厅，可重新接单"


def _peer_claim_sync_timeout() -> float:
    """读同步推超时（秒）：env 注入口，空 / 坏值 / 非有限数一律回落默认（⛔ 不把坏配置当 0）。"""
    import math

    raw = (os.environ.get(PEER_CLAIM_SYNC_TIMEOUT_ENV) or "").strip()
    if not raw:
        return PEER_CLAIM_SYNC_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return PEER_CLAIM_SYNC_TIMEOUT
    if not math.isfinite(value):
        return PEER_CLAIM_SYNC_TIMEOUT
    return min(max(value, PEER_CLAIM_SYNC_TIMEOUT_MIN), PEER_CLAIM_SYNC_TIMEOUT_MAX)


def _money_text(value) -> str | None:
    """金额 → 两位小数字符串；`None` 原样（「上家没给价」是事实，⛔ 不伪装成 0.00）。"""
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def peer_display_no(row: PeerHallMirror) -> str:
    """镜像行的**展示号**：上家单号本身（payload 里的 `order_no` 与它是同一个值；
    payload 缺坏时回落列值，两处来源是同一事实的快照）。"""
    payload = row.payload if isinstance(row.payload, dict) else {}
    return str(payload.get("order_no") or "") or row.peer_order_no


def price_at_percent(remote_price, percent) -> Decimal | None:
    if remote_price is None:
        return None
    amount = Decimal(remote_price) * percent / Decimal(100)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def display_price(row: PeerHallMirror) -> Decimal | None:
    """有入场快照的卡按快照比例，旧全局模式的卡按当前绑定比例。"""
    percent = row.hall_price_percent_at_sync
    if percent is None:
        binding = row.peer_upstream_binding
        percent = getattr(binding, "hall_price_percent", None) or 100
    return price_at_percent(row.remote_hall_price, percent)


def peer_hall_queryset(*, game_id=None, server_id=None, project_text=None, keyword=None):
    """本线镜像单的候选集（**与另几路同一组筛选 / 同一语义**，spec §7）。⚠️ `server_id is not None` ⇒ **整张表置空**：团团通单的 `server_id` 是**对端的区服主键**（R21 之下不消费、镜像行也没有我方区服 FK），传 `?server=` 时它**不出现**（照 `upstream_hall_queryset` 同口径，⛔ 不是「按文本碰运气」）；`keyword` 只搜**可见字段**：上家单号（= 大厅行的 `order_no` 键）、项目名、备注、游戏名——对外载荷（takerapi 13 键）里有的才可搜，「可搜而不可见」是一条信道（`takerapi/api/hall_views.py` 模块 docstring 的不变式，两路同一条纪律）；匹配语义是**子串包含（区分大小写）**：`icontains` 在本仓 PostgreSQL `collate C` 之下就是大小写敏感的 `LIKE %…%`（⛔ 别读成「不区分大小写」）。
    ⚠️ 游戏权限卡点由**调用方**照另几路那样过 `scope_queryset`（⛔ 不在这里写第二道判据）。
    ⚠️ 只列 `status=listed` 且**归属绑定启用、未解绑、且大厅同步开关开着**的行——与鸡蛋铺那一路同一条「可见与可接同一道判据」的修订口径（E-0288）：绑定停用 / 开关关掉之后，名下僵尸行在读口就地消失。⭐ `hall_sync_enabled` 是**接收侧总开关**（P1，默认关）：关着 ⇒ 本函数一行都不给（内部读口与 takerapi 两路共用本函数 ⇒ 一处判据管两路）。"""
    from django.db.models import Q

    qs = PeerHallMirror.objects.filter(
        status=PeerHallMirror.Status.LISTED,
        peer_upstream_binding__deleted_at__isnull=True,
        peer_upstream_binding__enabled=True,
        peer_upstream_binding__hall_sync_enabled=True,
    ).select_related("game", "peer_upstream_binding")
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if server_id is not None:
        return qs.none()
    if project_text:
        # ⚠️ 本表**没有** `project_text` 列（spec §3.1 的列清单里只有 `payload` JSONB）
        #    ⇒ 项目名筛选打在 **payload 的 JSON 键**上（与 keyword 那一支同源）。
        qs = qs.filter(payload__project_text__icontains=project_text)
    if keyword:
        qs = qs.filter(
            Q(peer_order_no__icontains=keyword)
            | Q(payload__order_no__icontains=keyword)
            | Q(payload__project_text__icontains=keyword)
            | Q(payload__remark__icontains=keyword)
            | Q(game__name__icontains=keyword)
        )
    return qs.order_by("-first_seen_at", "-id")


def peer_hall_price_in_range(row, *, price_min=None, price_max=None) -> bool:
    """镜像行是否落在 `?price_min` / `?price_max` 区间内（**按展示价**，两端含）。⚠️ 判据一律 `is not None`（E-0021）：`?price_min=0` 必须真的筛。
    ⚠️ 展示价为 `None`（上家没给价）的行：**任何区间都不命中**——它在大厅上显示为 `null`，把它按 0 去比区间就是说假话（鸡蛋铺那一路恒有价、没有这一档）。
    ⚠️ 比的是 `display_price`（打手看到的那个数，已乘本绑定的百分比），⛔ 不是 `row.remote_hall_price` 原价——否则「筛 40 元以下」会把一张展示价 45 的行筛出来（上家价 50 × 90%）。"""
    price = display_price(row)
    if price is None:
        return price_min is None and price_max is None
    if price_min is not None and price < price_min:
        return False
    if price_max is not None and price > price_max:
        return False
    return True


def serialize_peer_row(row: PeerHallMirror) -> dict:
    """本线镜像行 → **与另几路恰好同键**的字典（键表判据见模块 docstring）。逐键来源（与 `serialize_upstream_row` 对位）：`order_no` / `source_order_no` ← `peer_display_no`（上家单号，展示号即对端单号）；`origin` / `source_system` = `"peer"`（行身份，前端据此分辨四路）；`remote_order_id` / `remote_order_view` ← `row.peer_order_no`（键表同形，值即上家单号）；`remote_game_area` / `remote_game_server` / `remote_hours_desc` / `game_role_name` = 空串（团团通载荷没有这三样/角色名）；`mirror_pk` = `row.pk`（接单端点 `POST /…/hall/peer/<pk>/claim/` 的路径参数）；`game` = `row.game_id`（收方 `resolve_game` 对齐的本地游戏）；`project_text` / `remark` ← payload 同名键（上家行原样）；`hall_price` = **展示价**（`display_price` = 上家发单价 × 本绑定百分比 / 100，两位小数 / `null`，唯一算法出处）。
    ⚠️ `created_at` = `first_seen_at`（进厅时刻；上家事件没带发布时刻）。⭐ B6 / L-12：走 `local_iso`（项目时区 `+08:00`）——同一份合并响应里原生行（DRF `DateTimeField`）与鸡蛋铺镜像行（`serialize_upstream_row` 已收口）都是 `+08:00`，本路裸 datetime 被 DRF 渲染成 `+00:00` 是混排（内部读口与 takerapi 同一次收口）。
    ⚠️ `is_preferred` 按**展示价**判（`hall_preferred.is_preferred_price`），无价 ⇒ 非优选。"""
    from apps.business import hall_preferred

    data = dict(REMOTE_ROW_BASE)
    payload = row.payload if isinstance(row.payload, dict) else {}
    data.update(
        {
            "origin": PEER_ORIGIN,
            "source_system": PEER_SOURCE_SYSTEM,
            "remote_order_id": row.peer_order_no,
            "remote_order_view": peer_display_no(row),
            "remote_game_area": "",
            "remote_game_server": "",
            "remote_hours_desc": "",
            "mirror_pk": row.pk,
            "game": row.game_id,
            "project_text": str(payload.get("project_text") or ""),
            "remark": str(payload.get("remark") or ""),
            "game_role_name": "",
            "hall_price": _money_text(display_price(row)),
            "created_at": local_iso(row.first_seen_at),
            "is_preferred": hall_preferred.is_preferred_price(display_price(row)),
        }
    )
    return data


def serialize_peer_hall_rows(
    rows, *, price_min=None, price_max=None
) -> list[tuple[datetime, dict]]:
    """已 scoped 的镜像行 → **已按展示价筛过**的 `(进厅时刻, 行)` 列表（另几路同构）。与鸡蛋铺的 `serialize_upstream_hall_rows` 同一条形状：价格判定只写在 `peer_hall_price_in_range` 一处；拿「已收窄的行」不拿筛选参数（权限卡点在调用方）。"""
    return [
        (row.first_seen_at, serialize_peer_row(row))
        for row in rows
        if peer_hall_price_in_range(row, price_min=price_min, price_max=price_max)
    ]


def merge_hall_rows_peer(native, upstream, simulated, peer):
    """可接区四路归并：原生 ∪ 鸡蛋铺镜像 ∪ 模拟单 ∪ **团团通镜像**，按进厅时刻倒序。⛔ **不动** `hall_preferred.merge_hall_rows_with_simulated`（D-0296 的新函数，有三路既有用例钉着）——本函数是它形状上的加法。
    排序键与并列次序同一条性质：**相同时间戳 ⇒ 原生 → 鸡蛋铺镜像 → 模拟单 → 团团通镜像**（写进排序键自己的性质，⛔ 不依赖构造顺序 + 稳定排序）。"""
    items = [(moment, 0, payload) for moment, payload in native]
    items.extend((moment, 1, payload) for moment, payload in upstream)
    items.extend((moment, 2, payload) for moment, payload in simulated)
    items.extend((moment, 3, payload) for moment, payload in peer)
    items.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [payload for _moment, _rank, payload in items]


# ── P5 下架存量卡（关接收开关 / 解绑；纯本地零网络） ─────────────────────────────
#: 关闭 `hall_sync_enabled` 时写进下架行 `last_error` 的**人话**（行上可见；只是留档，
#: ⛔ 不参与任何判据）。单独常量让用例钉字面值（E-0024）。
UNLISTED_ON_SWITCH_OFF = "上家大厅同步已关闭，这张卡已从大厅下架"
#: 解绑（软删上家绑定）时写进下架行 `last_error` 的**人话**（同上，留档用）。
UNLISTED_ON_UNBIND = "与上家的绑定已解除，这张卡已从大厅下架"
#: ⭐ Z5（审计批次二）：绑定停用时把**抢单未确认**（`claiming`）的卡退回大厅，
#: 写进行 `last_error` 的**人话**（打手 / 客服在行上直接可见）。单独常量让用例钉字面值（E-0024）。
CLAIM_RELEASED_ON_UNBIND = "绑定停用，抢单未确认已退回大厅"
#: 退回台账的 **kind**（`PeerSyncEvent`，`direction="in"` / `status="done"`）。
#: ⚠️ 与 `hall.*` 协议事件分开命名：这一行是我们自己的决策留痕，⛔ 从不外发
#: （`direction="in"` 不在 `peer_worker.claim_due_events` 的取活面里）。
RELEASED_CLAIM_LEDGER_KIND = "hall.claim_release"


def unlist_binding_mirrors(binding, *, note: str, now: datetime | None = None) -> int:
    """把**这份上家绑定名下**还在挂的镜像卡全部下架（P5），回下架了几行。触发点**只有两个**（都在绑定页面端点的写事务里，见 `apps/business/api/peer_views.py::PeerBindingDetailView`）：① `PUT` 把 `hall_sync_enabled` 从 `True` 改成 `False`（接收开关关掉）；② `DELETE` 软删绑定（解绑）。为什么必须有这一步（业务方裁定 + share-design §8 / §11-2「关掉开关时存量镜像全部下架，不留僵尸卡」）：开关关着时读口与接单口已经被 P1 的判据挡住 ⇒ 大厅上**看不见**这些卡，但行还在 `listed`；再打开开关（或同一域名重新绑定、新绑定行接管了旧行）时，它们会**原地复活成一批早就过期的卡**——上家那张单可能早就不在厅里了。所以关开关 / 解绑时把数据清掉：这是**清数据**，不是给读口补闸。
    口径（两条都是**承重**的）：只动 `status=listed` 的行 ⇒ `claiming` **不动**（它的收口在 claim 应答链上，另有超时回收 `reclaim_stuck_claims` 兜底（D-0335 起 60 秒）；这里改它会把一次正在等真单的接单搞坏）；`promoted` / `gone` / `skipped` 一律不碰（转正是历史事实；`gone` 已经是终态）。⭐ **B3（终审修复轮）：下架要**通知本租户的下家**（发 `hall.take`）**——多跳链（A→B→C）上 C 手里那张卡是 B 转发过去的，B 关开关 / 解绑时只把**本地**行标 `gone`，C 的大厅里那张 `listed` 卡不会自己消失（它不在 B 的读面上、也不会再收到任何事件）⇒ 一张永远接不了、也回不来的僵尸卡。所以每**真的**翻成 `gone` 的一行都要照 `hall.take` 现有的转发形状补发（`peer_dispatch.enqueue_hall_mirror_event`：同一个 outbox、同一个事件形状、同一道目标闸 `_hall_targets`——只发给**生效且开着大厅推送**的下家绑定，撤销 / 未启用的绑定天然被排除）；仍是**同一个事务**：回滚时 take 一起消失。
    ⚠️ 逐行**条件更新**（`pk` + 读到的状态）而不是一句批量 `update()`：只有真的翻过去的那几行才发 `take`（读到落写之间被并发接走 / 转正的行不发——它们已经不在厅里，take 是噪声），回给调用方的行数也按**这一次真的下架的行**计。⚠️ 行**不删**（`peer_upstream_binding` 是 `PROTECT`，且 share-design §11-3 要求解绑时镜像行留档）：只标 `gone` + `gone_at`，`payload` / 价格 / 单号全部原样。⚠️ 回下架行数供调用方计入日志（本端点**不改响应体形状**：P1 的 PUT 回的就是绑定行，GET 那条「键集恰好」的网也在钉同一份序列化器 ⇒ 加一个键会让两个端点各说一套）。"""
    from apps.business import peer_dispatch
    from apps.business.peer_sync import HALL_TAKE_KIND

    now = now if now is not None else timezone.now()
    rows = list(
        PeerHallMirror.objects.filter(
            peer_upstream_binding=binding,
            status=PeerHallMirror.Status.LISTED,
        )
        .select_related("game", "peer_upstream_binding")
        .order_by("pk")
    )
    unlisted = 0
    for row in rows:
        updated = PeerHallMirror.objects.filter(
            pk=row.pk,
            status=PeerHallMirror.Status.LISTED,
        ).update(
            status=PeerHallMirror.Status.GONE,
            gone_at=now,
            # ⚠️ 截到列宽（`last_error` 是 255）：文案常量本身很短，这里只是不让将来改长时
            #    在 PostgreSQL 上炸成 500（同 `_record_reclaim` 的 `reason[:255]` 惯例）。
            last_error=note[:255],
        )
        if not updated:
            continue
        unlisted += 1
        # ⭐ B3：这张卡离开**本租户**的大厅 ⇒ 下家的大厅里同款卡也一起撤（多跳状态跟随）。
        #    ⚠️ `occurred_at=now` 与本地 `gone_at` 同一个时刻（同一次下架决策）。
        peer_dispatch.enqueue_hall_mirror_event(
            row, kind=HALL_TAKE_KIND, occurred_at=now
        )
        # ⭐（业务方拍板）：卡下架 ⇒ 派生的隐藏单退、下家鸡蛋铺撤下副本。
        from apps.business import hall_shadow

        hall_shadow.refund_for_card(row, now=now)
    return unlisted


def release_claiming_mirrors(binding, *, note: str, now: datetime | None = None) -> int:
    """把**这份上家绑定名下**抢单未确认（`claiming`）的卡退回大厅（⭐ Z5，审计批次二）。触发点**只有两个**，都在「这条绑定不能再用」的写事务里（见 `apps/business/api/peer_views.py`）：① `PeerBindingDetailView.delete`（页面解绑，软删上家绑定）；② `PeerUnbindView.post` 的**下家分支**（对端发来的解绑通知）。为什么必须有这一步：`claiming` 的行**不在大厅**（读口只投影 `listed`），它的两条出路——上家判决后转正 / `hall.claim_refused` 收回 `listed`——**都需要对端回音**；绑定一停那个回音永远不来 ⇒ 这张卡既接不了也看不见，而**上家那张单可能还好端端挂在厅里**；超时回收（`reclaim_stuck_claims`，D-0335 起 60 秒）只在常驻 `peer_sync` 循环里跑，绑定已经停了的租户不一定还来得及处置它 ⇒ 在停绑这一刻就地退回。
    ⚠️ **退回 `listed`、⛔ 不是 `gone`**（与 `unlist_binding_mirrors` 的 `listed → gone` 相反，这是本函数的承重口径）：`gone` 断言「这张单不在厅了」，而抢单没确认时我们**不知道**上家那张单还在不在——它很可能还在（正是「手慢了」之外的另一半），`gone` 会误杀真单；与 `hall.claim_refused` 的「无摘牌证据 ⇒ 回 listed」同一口径。⚠️ **调用顺序承重**：必须先调 `unlist_binding_mirrors`（`listed → gone`）、**再**调本函数——反过来的话，本函数刚退回 `listed` 的行会被随手一起标成 `gone`（那就白退了）。
    ⚠️ 清**四个接单列**（`claiming_by` / `claiming_at` / `claim_downstream` / `claim_id`）：这张卡已经不在任何人手里，留着「谁在接 / 何时开始 / 哪个下家在要」就是一句假话（E-0027 同族：加了写入点就要覆盖回退 / 清理路径；回收 `reclaim_stuck_claims` 同一条口径）；`gone_at` **不动**：它是「上家曾经摘牌」的既存证据，收口仍归 `hall.claim_refused`（M2 的判据就是它），本函数只把**状态与归属**退回大厅。⚠️ **逐行条件更新**（`pk` ＋ `status=claiming`）：读到落写之间这一行可能刚好转正 / 刚被回收 ⇒ 命中 0 行、尊重那一次（⛔ 不抢回来）；逐行还让台账 payload 里的卡号**恰好是这次真退回的那几张**（批量 `update()` 只回个数、说不出是谁）。⚠️ **纯本地、零网络、零出站事件**：⛔ 不通知上家 / 下家（协议里没有「撤销接单」这个动作）；回退回的行数，> 0 时记**一条** `direction="in"` 台账（`_record_claim_release`）。"""
    now = now if now is not None else timezone.now()
    rows = list(
        PeerHallMirror.objects.filter(
            peer_upstream_binding=binding,
            status=PeerHallMirror.Status.CLAIMING,
        ).order_by("pk")
    )
    released: list[str] = []
    for row in rows:
        updated = PeerHallMirror.objects.filter(
            pk=row.pk, status=PeerHallMirror.Status.CLAIMING
        ).update(
            status=PeerHallMirror.Status.LISTED,
            claiming_by=None,
            claiming_at=None,
            claim_downstream=None,
            claim_id="",
            last_error=note[:255],
        )
        if updated:
            released.append(row.peer_order_no)
    if released:
        _record_claim_release(binding, released=released, note=note, now=now)
    return len(released)


def _record_claim_release(binding, *, released, note: str, now) -> None:
    """记**一条**「绑定停用，抢单未确认已退回大厅」台账（本线既有台账形状，一张表兼 outbox）。`direction="in"` / `status="done"`：这是我们登记的**已知事实**（退回是决定，不是待办），`peer_worker` 只取 `direction="out"` + `pending` ⇒ 永远不会把它投出去；⛔ 不记 `failed`——退回不是投递失败，不该进失败计数 / 对账重排。
    `binding_role="upstream"` / `binding_id` = 这张卡归属的那份上家绑定（两张绑定表 pk 各自从 1 起 ⇒ 只认 `binding_id` 会指到无关的行，所以这一列与 role 成对）；`target_ref` 留空（**一次操作退多张卡**，没有唯一对象可指）⇒ 卡号逐张进 `payload`；幂等键用随机值（照本线其余台账）：一次解绑只调一次本函数。"""
    PeerSyncEvent.objects.create(
        binding_role="upstream",
        binding_id=str(binding.pk),
        direction="in",
        kind=RELEASED_CLAIM_LEDGER_KIND,
        target_type="hall",
        target_ref="",
        status=PeerSyncEvent.Status.DONE,
        reason=note[:255],
        payload={"released": list(released), "count": len(released)},
        meta={"occurred_at": now.isoformat()},
        idempotency_key=uuid.uuid4().hex,
    )
    logger.info(
        "大厅镜像：绑定停用退回抢单未确认的卡 binding=%s count=%s",
        binding.pk, len(released),
    )


# ── 卡死回收（P3，纯本地零网络；挂在常驻循环 `peer_sync` 的每轮开头）──────────────
def reclaim_stuck_claims(*, now: datetime | None = None, should_stop=None) -> int:
    """把**卡在 `claiming` 太久的镜像行**放回大厅，回回收了几行（P3，spec §4.4 的增量）。⭐ **D-0347 起角色改为「清道夫」**：人类单跳的接单已改**同步直连**（`claim_peer_mirror_sync` ≤3 秒当场给结论，给不出当场放回）⇒ 正常操作的 `claiming` 存活不过一个请求窗、**永远够不到本支的 60 秒阈值**；本支兜的是两类**没有其它出口**的残留：① 请求进程死在「预占提交后、放回前」的行（不收口会一直拦着抢单人的押金解冻申请 `wallet._has_active_work`）；② 多跳中间层的行（`_forward_mirror_claim` 占住卡等它自己的上家异步判决，烧完投递退避只有本支能收口）。⛔ 因此**不可退役**（`peer_sync` 命令每轮调用 + `test_openapi_event_sites_net` 把它钉成事件位点 + 两个上述消费方）。
    判据三条**同时**成立（⛔ 少一条就是误伤）：① `status = claiming`（打手点过接单、还在等上家的正式订单）；② `claiming_at < now - RECLAIM_STUCK_CLAIMING_SECONDS`（⭐ D-0335 起 **60 秒**；**严格小于**——恰好 60 秒不回收，用例钉死）；③ `promoted_order_id IS NULL`（正式订单还没落进来）。⚠️ 判据**在 Python 里**拿主键列做，⛔ **不写** `promoted_order__isnull=True`：那是一条到 `Order` 的**关联 / join**，会触发 `tests/test_order_read_path_net.py` 的落点登记，而且那条 join 的默认语义在这里是**反向**的（转正后正式单被软删时 FK 仍然指着它）⇒ 补 `order__deleted_at__isnull=True` 会把「已经转正」读成「没转正」，把一张已经在本租户名下的卡放回大厅。为什么必须有这一支：`claiming` 的行**不在厅里**（读口只投影 `listed`），它的两条出路——上家判决后转正 / `hall.claim_refused` 收回 `listed`——都需要**对端有回音**，「`hall.claim` 事件丢了 / 上家一直没指派」这一档两条都不成立 ⇒ 这张卡永久既接不了也看不见（上家那边还可能认为它已经被接走了）；回收放回 `listed`：下一轮 `hall.throw` / 铺底（`push_peer_hall_snapshot`）重新挂回来，打手还能再接一次。
    ⚠️ **纯本地**：只动我方镜像行与本地台账，⛔ 不向上家发任何请求（协议里没有「撤销接单」）。已知代价（如实登记）：若上家其实已经受理、正式订单只是**晚到**，那张单到达时 `_promote_claiming_mirror` 找不到 `claiming` 行 ⇒ **照常建单但不转正**（价格按载荷走），镜像行留在 `listed` 由后续 `hall.take` 收掉；⛔ 不在这里写「补偿建单」——那是第二套转正语义（spec §10 既有口径：宁可多一张挂在厅里的卡，也不重复建单）。⚠️ 判据里没有「本地点过接单」这一条（`claiming_by` 非空）：只看三列 ⇒ 将来「下游发起」的 `claiming` 行（多跳）卡住一样会被放回大厅（v1 的 `claiming` 行恒带 `claiming_by`，这一条只是不把判据写窄）。⚠️ **条件更新**（`pk` + 读到的 `status` + 读到的 `claiming_at`）：读到落写之间这一行可能已经转正 / 刚被 `hall.take` 收回 / 刚又被重新接单 ⇒ 命中 0 行、**尊重那一次**、⛔ 不抢回来；`claiming_at` 进 `WHERE` 是为了挡「收回 → 立刻被重新接单」那个极窄窗口（新一次占住的时刻不同，不该按旧的年龄被当场回收）。⚠️ `claiming_at` 为空的行判不出年龄（`claiming_at__lt` 天然排除 NULL）⇒ **不回收**（拿 `first_seen_at` 去猜会把一张刚被接住的行当场收回去）。⚠️ **不按绑定开关过滤**：开关关着时读口本来就不投影这些行，但把它们留在 `claiming` 只会让「再打开开关」之后多一张僵尸卡——回收是无害的。⚠️ **零网络、零出站事件**：⭐ **B3 / L-8（审计批）的例外形状**——判 `gone` 的那一支也**不补发**下游 `hall.take`：本函数的契约是纯本地（调用点在常驻循环每轮开头、不在任何绑定写事务里），改它会把「零网络」变成每次回收都在发请求；多跳下游那张同款卡的收口仍归 `hall.claim_refused` / `unlist_binding_mirrors` 的转发（残差如实登记）。⭐ **`gone_at` 分叉（B3 / L-8）**：`claiming` 期间上家的 `hall.take` 来过 ⇒ M2 的证据支写下了 `gone_at`（状态不动、收口归应答链）；可应答链**没来**、卡在这里卡死了——这条线分明已经知道「上家把单撤了」，把卡放回 `listed` 让打手**再点一次**就是明着害他（注定被 refused、还白冻一次押金冷静期）⇒ 按证据分两支：`row.gone_at` 非空 ⇒ 收口成 **`gone`**（`last_error` 用 `peer_inbound.CLAIM_REFUSED_AFTER_TAKE_NOTE` 同一句人话；`gone_at` **不动**：恒保留最早那一次，是证据本体）；`row.gone_at` 为空（事件丢了 / 上家一直没指派）⇒ **现行为**：回 `listed`。⚠️ 上家那张单其实还在、只是应答迟到的形状走不到 `gone` 支：take 没来过就没有 `gone_at` 证据 ⇒ 照旧回 `listed`（不会误杀真单——与 refused 分因的口径同一条）。"""
    now = now if now is not None else timezone.now()
    deadline = now - timedelta(seconds=RECLAIM_STUCK_CLAIMING_SECONDS)
    rows = PeerHallMirror.objects.filter(
        status=PeerHallMirror.Status.CLAIMING,
        claiming_at__lt=deadline,
    ).order_by("pk")
    reclaimed = 0
    candidates = rows.filter(promoted_order_id__isnull=True).iterator(chunk_size=64) if should_stop else rows
    for row in candidates:
        if should_stop is not None and should_stop():
            break
        # ③ 正式订单还没落进来（判据用**主键列**，理由见 docstring）。
        if row.promoted_order_id is not None:
            continue
        # ⭐ B3 / L-8：「上家已摘牌」的证据在**更新之前**读——条件更新本身不判它，
        #    两支各翻各的目标状态。
        gone_evidence = row.gone_at is not None
        if gone_evidence:
            from apps.business.peer_inbound import CLAIM_REFUSED_AFTER_TAKE_NOTE

            close_note = CLAIM_REFUSED_AFTER_TAKE_NOTE
            close_status = PeerHallMirror.Status.GONE
        else:
            close_note = RECLAIMED_NOTE
            close_status = PeerHallMirror.Status.LISTED
        # ⚠️ 占住时刻必须在**更新之前**拍下来：更新会把它清空，而台账要记的正是
        #    「这一次」占住的时刻（更新之后读只能拿到 None）。
        stuck_since = row.claiming_at
        updated = PeerHallMirror.objects.filter(
            pk=row.pk,
            status=PeerHallMirror.Status.CLAIMING,
            claiming_at=stuck_since,
        ).update(
            status=close_status,
            # 四列一起清（E-0027 同族：加了写入点就要覆盖回退 / 清理路径）——这张卡已经不在任何人手里，留着「谁在接 / 何时开始」就是一句假话；`claiming_at` 留着不动的后果更实：下一轮回收会拿一次**已作废**的占住时刻算年龄，把一张刚被重新接住的卡当场又收回去。
            # ⭐ P4 多跳的两列（`claim_downstream` / `claim_id`）与本行「这次接单」同生共死——判据三条只看 `claiming` + `claiming_at` + `promoted_order`、**不区分**是本地点单还是下家在要 ⇒ 中间层卡住的行同样会被放回大厅，必须把「是谁在要」一起清掉（否则**交单钩子会认错人**：下一次转正按旧的 `claim_downstream` 把单交给一个早就不再等的下家）。
            claiming_by=None,
            claiming_at=None,
            claim_downstream=None,
            claim_id="",
            last_error=close_note,
        )
        if not updated:
            # 并发走掉（刚转正 / 刚收到 take / 刚被重新接单）⇒ 尊重那一次。
            continue
        reclaimed += 1
        _record_reclaim(row, stuck_since=stuck_since, note=close_note,
                        closed_gone=gone_evidence)
        if gone_evidence:
            # ⭐：按摘牌证据收口成 `gone` 也是「卡离开大厅」⇒ 隐藏单退。
            #    ⚠️ 回 `listed` 那一支**不退**：卡还在厅里，隐藏单照旧待抢。
            from apps.business import hall_shadow

            hall_shadow.refund_for_card(row, now=now)
    return reclaimed


def _record_reclaim(row: PeerHallMirror, *, stuck_since: datetime,
                    note: str = RECLAIMED_NOTE,
                    closed_gone: bool = False) -> None:
    """记一条回收台账（`PeerSyncEvent`：本线既有台账形状，一张表兼 outbox 与台账）。`direction="in"` / `status="done"`：**我们登记的已知事实**、不是待投递事件 ⇒ `peer_worker.claim_due_events`（只取 `direction="out"` + `pending`）永远不碰它；⛔ 不是 `failed`——回收是「我们做出的决定、并且已经留痕」，不该进投递失败计数 / 对账重排（照鸡蛋铺线 `upstream.hall_gone` 那一族同口径）。
    `binding_role` / `binding_id` 记**这张卡归属的那份上家绑定**（两张绑定表 pk 各自从 1 起 ⇒ 只认 `binding_id` 会指到无关的行，这也是本列存 role 的理由）；⭐ B3 / L-8：`note` 随**这一支**的收口文案走（回 `listed` = `RECLAIMED_NOTE`；收口 `gone` = `CLAIM_REFUSED_AFTER_TAKE_NOTE`），`closed_gone` 进 `payload`（台账上分得清「放回大厅」与「按摘牌证据下架」两种结局）。
    幂等键用随机值（照本线其余台账）：本行的唯一性由**条件更新**保证——一次卡死的接单只会有一次收口、只有一条台账；同一张卡后来又卡住 ⇒ 那是**另一**次事故 ⇒ 另记一条（这正是我们要的）。"""
    PeerSyncEvent.objects.create(
        binding_role="upstream",
        # `binding_id` 是字符串列（两张绑定表共用），见模型 docstring。
        binding_id=str(row.peer_upstream_binding_id),
        direction="in",
        kind=RECLAIM_LEDGER_KIND,
        target_type="hall",
        target_ref=row.peer_order_no,
        status=PeerSyncEvent.Status.DONE,
        reason=note[:255],
        payload={
            "peer_upstream_binding_id": row.peer_upstream_binding_id,
            "claiming_at": stuck_since.isoformat(),
            "claiming_by_id": row.claiming_by_id,
            "stuck_seconds": RECLAIM_STUCK_CLAIMING_SECONDS,
            "closed_gone": closed_gone,
        },
        meta={"occurred_at": stuck_since.isoformat()},
        idempotency_key=uuid.uuid4().hex,
    )
    logger.info(
        "大厅镜像回收了一张卡死的接单卡 mirror=%s order_no=%s",
        row.pk, row.peer_order_no,
    )


def _promoted_order_owned_by(row: PeerHallMirror, user_id: int):
    """`row` 已转正、且那张正式单**现在**挂在 `user_id` 名下 ⇒ 回那张单，否则 `None`。判据 = `row.promoted_order` 的 **MAIN 绩效行 `user_id`**（「打单工资」那一条就是接单人，本仓「接手人身份只有 MAIN 行表达」的既有口径，见 `takerapi/api/order_scope.py`）；⛔ **不看**镜像行的 `claiming_by`——那是**当初**点接单的人，改派之后它不变，拿它判会把「已经不是你（被改派走）的单」当成你的（见 `claim_peer_mirror` 的 P3 段）。
    ⚠️ `promoted_order` 用**延迟外键**取（`row.promoted_order`），⛔ 不写 `promoted_order__…` 的关联过滤：那样会造出到 `Order` 的 join，进 `tests/test_order_read_path_net.py` 的读点 / join 登记面；本函数要的只是「那张单的 MAIN 行归谁」，两跳主键查询已经够。"""
    if row.promoted_order_id is None:
        return None
    order = row.promoted_order
    main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if main is None or main.user_id != user_id:
        return None
    return order


# ── 抢单（确认制的我方那一半，spec §4.1）────────────────────────────────────
def claim_peer_mirror(*, user_id: int, mirror_id: int, now: datetime | None = None,
                      enforce_deposit: bool | None = None,
                      enforce_limits: bool = True,
                      queue_claim: bool = True) -> PeerHallMirror:
    """下家打手抢一张团团通镜像行（spec §4.1 的 ①–②）：过闸 → 预占 → 排 `hall.claim`。⭐ **`queue_claim`（D-0347）**：`True`（缺省）= 既有行为——预占成功后**同事务**排一条上行 `hall.claim` 到 outbox（机器路径 peer 档与全部既有调用方逐字不变）；`False` = **人类单跳的同步抢单**用：只做本地预占，`hall.claim` 由 `claim_peer_mirror_sync` 在请求内**直推对端**（见该函数）。⛔ 只有这两个语义档，不存在第三种取值。前置与判据照 `claim_upstream_mirror` 同一族（⛔ 不另开一道门）：行必须 `listed`（`claiming` 且是本人 ⇒ 「正在处理中」；其余 ⇒ 「已被接走」）；归属绑定必须存在且启用（僵尸行的前置同鸡蛋铺 E-0288 的口径）；⭐ **接收侧开关**必须开着（`hall_sync_enabled`，P1 默认关）：关着 ⇒ 拒绝 + 人话 `HALL_SYNC_OFF_MESSAGE`（⛔ 这不是「已接走」那种竞争失败——人要去打开开关，与「手慢了」是两件不同的事）；抢单限制四支 + 实名闸在任何写入之前跑（`check_grab_limits`，闸价 = **展示价** = `display_price(row)`，⛔ 与投影 / 读口同一个算法）；优选闸（个人打手两档门槛，判价同上）；本地预占 = **条件更新** `listed → claiming` + 抢单人两列，输给并发就「手慢了」；预占成功 ⇒ **同事务**排一条上行 `hall.claim`（`peer_dispatch.enqueue_hall_claim`）。
    ⭐ **P3 幂等重投**（行已经 `promoted`、且那张正式单的 MAIN 绩效行**就是**本人 ⇒ 回「正在处理中」+ **重投一次** `order.grab`，⛔ 不重复建单、⛔ 不改价）：场景是这张卡**已经交给过这个人**，但打手 / 客服那边只看到「接单中」或一张没刷新的卡又点了一次——旧行为回「该笔订单已被其他打手接取」是**假话**（这张单就在他自己名下），还会把人引到「去别处找单」的错误处置上。判据（⛔ 不是 `binding.account`）：`row.status == promoted` 且 `row.promoted_order` 的 **MAIN 绩效行 `user_id` == 本次 `user_id`**——为什么用 MAIN 而不是镜像行的 `claiming_by`：转正时 `_promote_claiming_mirror` 把 `claiming_by` 挂成 MAIN 行的人（H1 口径），而**之后**这张单可能被改派给别人（MAIN 跟着变、`claiming_by` 不变）⇒ 只有 MAIN 那一列能回答「现在这张单是不是**你**的」；MAIN 归别人 / 没有 MAIN 行 ⇒ 落回「已被接走」那一档（那是真的被别人拿走了）。这一档**不查绑定开关 / 启用态**（照鸡蛋铺线 F3 同一条口径）：单已经在我方名下，「受理 + 重投」与那条边此刻开不开无关；⛔ 此时用「还没打开上家大厅同步」答复会把一件**已经成了**的事说成没成。⚠️ 重投的是 `order.grab`（`peer_dispatch.enqueue_order_grabbed`）：幂等由它自己的未决去重键 `(binding, kind, target_ref)` 保证；单跳 ⇒ 该函数回 `None`、**一行事件都不写**（正常形状，⛔ 不是漏做）。
    ⛔ **不打上游、不等转正**（与鸡蛋铺线的本质差异，见模块 docstring）：正式订单由上家的 `order.assign` 推回来才落地（`peer_inbound` 的回填把行转成 `promoted`）；`hall.claim_refused` 按原因收口（⭐：可重试 ⇒ `listed`；**订单已不存在 / 没共享给这家 ⇒ `gone`**，见模块 docstring 的改准块）；v1 **不做 claiming 超时自动回退**（spec §4.4 待拍板项②），★ P3 起由纯本地的 `reclaim_stuck_claims` 兜这一档（见上）。⚠️ 押金口径与鸡蛋铺镜像单同一族（调用方显式传 / 回落租户总闸 + 优选强制）；镜像行 `game` 由 `resolve_game` 建出、恒非空 ⇒ 押金规则查得到，没有「无游戏不冻」支。⛔ 租户上下文只来自认证态：`user_id` 由端点在已认证的请求上算出来（`AGENTS.md` §4）。⭐ **`enforce_limits`（评审阻断 1）**——机器接手跳过「按人」闸门：与 `claim.claim_order` 的**同名同语义**开关（裁定 8 的既有口径），`False` ⇒ 跳过 `check_grab_limits`（没有接单价 / 未实名 / 禁自助接单 / 价格区间 / 持单上限）与优选闸（`hall_preferred.check_preferred_claim`）；**唯一调用方** = `jidanpu_member.machine_claim_order` 的「下家鸡蛋铺打手接了隐藏单」那一档——那时单已经在**对方**手里（外部事实已定），被这几道按人闸门挡住会造出「对方接了、我方没接」的错位 ⇒ 卡永远回不来。⚠️ 其余判据**一道都不跳**：行状态（`listed` / 幂等 `promoted`）、归属绑定（存在 / 启用 / `hall_sync_enabled`）、`listed → claiming` 条件更新、`hall.claim` 排队；人类入口一律用默认值 `True`，行为一字不变。"""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from apps.accounts.wallet import WalletError, cancel_release_on_claim, ensure_deposit_for_claim
    from apps.business import hall_preferred, peer_dispatch
    from apps.business.jidanpu_common import (
        ALREADY_TAKEN_MESSAGE,
        IN_PROGRESS_MESSAGE,
    )
    from apps.business.models import PeerUpstreamBinding

    now = now or timezone.now()
    # ⚠️ `select_related("peer_upstream_binding")`：`display_price(row)` 要读那一行的
    #    百分比（延迟外键，不加就是每接一次多一条查绑定表的查询）。
    row = (
        PeerHallMirror.objects.filter(pk=mirror_id)
        .select_related("peer_upstream_binding")
        .first()
    )
    if row is None or row.status != PeerHallMirror.Status.LISTED:
        if row is not None and row.status == PeerHallMirror.Status.PROMOTED:
            # ⭐ P3 幂等重投（判据见 docstring）。⚠️ 这一段**有意排在「已被接走」之前**，
            #    也排在绑定 / 开关两道闸之前：它回答的是「这张单已经是你的了」这个事实。
            promoted = _promoted_order_owned_by(row, user_id)
            if promoted is not None:
                # 重投一次 `order.grab`（幂等；⛔ 不建单、⛔ 不改价）。⚠️ `occurred_at` 显式给 `now`：默认取 `order.updated_at`，而转正那一刻早就把它推到过对端一次——拿旧时刻重投会被对端按 `stale` 静默丢掉（`order_payload` 的 staleness 口径），那这一跳等于没做。
                peer_dispatch.enqueue_order_grabbed(
                    promoted,
                    get_user_model().objects.get(pk=user_id),
                    occurred_at=now,
                )
                raise ClaimError(IN_PROGRESS_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
        if (
            row is not None
            and row.status == PeerHallMirror.Status.CLAIMING
            and row.claiming_by_id == user_id
        ):
            raise ClaimError(IN_PROGRESS_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
        raise ClaimError(ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)

    binding = PeerUpstreamBinding.objects.filter(pk=row.peer_upstream_binding_id).first()
    if (
        binding is None
        or binding.deleted_at is not None
        or not binding.enabled
    ):
        # ⭐ 业务方报障修法三（与鸡蛋铺线 `claim_upstream_mirror` 的同支同一口径）：停用 / 软删 / 缺失绑定名下滞留的 `listed` 僵尸卡，对打手而言与「已下架」不可区分 ⇒ 回 `1012`，⛔ 不再回 `1015`「还没配置好鸡蛋铺同步账号」把人推给管理员。
        #    ⚠️ 与鸡蛋铺线的差异（刻意）：**不做**命中即下架（不改行状态）——peer 镜像行的 `gone` 由上家 `hall.claim_refused` 的回包驱动（生命周期与鸡蛋铺的拉取侧不同），且本支生产零命中；行留着，绑定恢复启用后由 peer 同步自然收敛。
        #    ⚠️ 机器路径（`jidanpu_member.machine_claim_order` 那一档）把 `1012` 当「我方别的打手先接走 ⇒ 去对方退单」的承重语义（A10 竞态分支）——本支生产零命中（调度确认），若将来命中需要连同那边的语义面一起复核。
        raise ClaimError(ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
    # ⭐ P1 的第三处闸（默认关）：开关关着 ⇒ 拒绝。⛔ 不是「已被接走」那句：那张卡的
    # 归属没坏、也没人抢，是**本租户还没打开接收**——两件事该去的地方不同。
    if not binding.hall_sync_enabled:
        raise ClaimError(HALL_SYNC_OFF_MESSAGE, ErrorCode.PLATFORM_NOT_CONFIGURED)
    if binding.bound_by_id == user_id:
        raise ClaimError(
            "该账号是本单的销售提成归属人，不能同时接单", ErrorCode.BUSINESS,
        )

    price = display_price(row)
    # ⚠️ 四支限制在条件更新之前跑（被拒时一行都不写，E-0022 同族）。⛔ **价用展示价**（与投影 / 优选同源同一列）；无价（`None`）⇒ `check_grab_limits` 的「NULL 接单价」那一支拒（与原生单同一档）。
    #    `order_type` 恒 `BOOST`：团团通载荷里没有订单类型（对端单不分区代练 / 陪玩），照鸡蛋铺镜像线同口径取 `BOOST`（上限按 `grab_limit_dl`）。
    #    ⭐ `enforce_limits=False`（机器接手）⇒ 这两道**按人**闸门一起跳过（理由见 docstring）。
    if enforce_limits:
        check_grab_limits(
            user_id=user_id,
            candidate=GrabCandidate(hall_price=price, order_type=OrderType.BOOST),
        )
        hall_preferred.check_preferred_claim(
            user=get_user_model().objects.get(pk=user_id), price=price
        )

    with transaction.atomic():
        # ⭐ 解冻冷静期 + 押金冻结：口径与 `claim.claim_order` / `claim_upstream_mirror` 同一族（接单 ⇒ 作废冷静期内的解冻申请；押金不够在**排事件之前**拒，预占失败回滚会连带撤销冻结）。
        #    ⚠️ 机器路径**存在**于本函数（`jidanpu_member.machine_claim_order` 的「下家鸡蛋铺打手接了隐藏单」那档，显式传 `enforce_limits=False` / `enforce_deposit=False`——机器接手不碰按人闸门与钱包，其余判据（行状态 / 归属绑定 / 条件更新 / `hall.claim` 排队）一道不跳，见上方 `enforce_limits` 那节）。
        cancel_release_on_claim(user_id)
        deposit_gate = (
            settings.DEPOSIT_ENFORCED if enforce_deposit is None else enforce_deposit
        )
        # ⭐ 优选单强制押金（D-0296 口径）：租户总闸关着也要冻（判价 = 展示价）。⚠️ 机器接手（`enforce_limits=False`）不覆盖：那条路显式传 `enforce_deposit=False`（与 `claim.claim_order` 里「人类路径判据 = `enforce_limits`」同一条纪律）。
        #    ⭐ 优选租户开关（D-0328）：开关关（N）⇒ 本覆盖整支跳过（高价单回归普通接单规则，押金只按租户总闸）。
        if (
            enforce_limits
            and not deposit_gate
            and hall_preferred.preferred_enabled()
            and hall_preferred.is_preferred_price(price)
        ):
            deposit_gate = True
        if deposit_gate:
            try:
                ensure_deposit_for_claim(
                    get_user_model().objects.get(pk=user_id), row.game,
                    ref_no=row.peer_order_no or str(row.pk),
                )
            except WalletError as exc:
                raise ClaimError(str(exc), exc.code) from exc

        updated = PeerHallMirror.objects.filter(
            pk=row.pk, status=PeerHallMirror.Status.LISTED
        ).update(
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by_id=user_id,
            claiming_at=now,
        )
        if updated != 1:
            transaction.set_rollback(True)
            raise ClaimError(ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
        if queue_claim:
            peer_dispatch.enqueue_hall_claim(row, taker_id=user_id)
    row.refresh_from_db(fields=["status", "claiming_by", "claiming_at"])
    return row


# ── ⭐ D-0347：同步抢单直推（人类单跳；机器 / 多跳照旧走 outbox）───────────────
def _claim_reply_events_for(data, *, target_ref: str) -> tuple[str | None, list[dict] | None]:
    """从对端事件端点的响应里取**本单**的判决（`(outcome, 应答事件)`；拿不到 ⇒ `(None, None)`）。
    ⛔ E-0010 同族：`data` 是**对端来的响应**，每层形状都先验再用；任何拿不到显式判决的形状一律 `(None, None)`（调用方按「未响应」放回），⛔ 不猜。"""
    if not isinstance(data, dict):
        return None, None
    replies = data.get("claim_replies")
    if not isinstance(replies, list):
        return None, None
    for entry in replies:
        if not isinstance(entry, dict) or entry.get("target_ref") != target_ref:
            continue
        events = entry.get("reply_events")
        if not isinstance(events, list) or not events:
            return None, None
        cleaned = [
            event
            for event in events
            if isinstance(event, dict)
            and isinstance(event.get("kind"), str)
            and isinstance(event.get("target_ref"), str)
            and isinstance(event.get("payload"), dict)
        ]
        if not cleaned:
            return None, None
        outcome = entry.get("outcome")
        return (outcome if isinstance(outcome, str) else None), cleaned
    return None, None


def claim_peer_mirror_sync(*, user_id: int, mirror_id: int, now: datetime | None = None,
                           enforce_deposit: bool | None = None,
                           enforce_limits: bool = True) -> PeerHallMirror:
    """人类单跳的**同步抢单**（D-0347，业务方原话「能抢就是抢到 抢不到就是抢不到」）。一句话：`claim_peer_mirror(queue_claim=False)`（本地预占，事务在此提交）→ **请求内直推** `hall.claim` 到对端（复用既有 `peer-sync/inbound/events/` 端点与受理机，最多等 3 秒，env 可调）→ 按判决收口：**对端受理** ⇒ 响应车带着刚排好的 `order.assign` + `hall.claim_ok` 回来 ⇒ 喂给**既有** `apply_events`（与 worker 收到时同一个入口）⇒ 建单 + 转正 ⇒ 返回行（`status="promoted"`，当场成交）；**对端回绝** ⇒ 响应车带 `hall.claim_refused`（分因）⇒ 同样喂 `apply_events` ⇒ 行收口成 `gone` / `listed` ⇒ 抛 1012「已被其他打手接取」；**给不出结论**（超时 / 对端老版本响应无键 / 多跳转发 / A9 重放只带 `claim_ok` / 应答形状坏 / 落库非预期异常）⇒ **当场放回大厅**（条件更新 `claiming → listed`、清两接单列、`last_error` 留人话）⇒ 抛 1017 `PEER_CLAIM_UNRESPONSIVE_MESSAGE`。
    ⛔ 不等、不重试、不留确认窗——迟到的判决按**既有**分支收口（E-0236 家族口径「我方已回大厅为准」）：迟到的 `order.assign` 落**未分配池**（`apply_events` 建单支 `promoting_claim=False` 那一档）或**归现持有人**（放回后被别的打手抢走、行又在 claiming ⇒ 照常转正）；迟到的 `hall.claim_refused` 找不到 `claiming` 行 ⇒ 条件更新命中 0 行 no-op。⛔ 协议没有「撤销接单」动词（`peer_sync.HALL_*` 六个 kind 之外无它）⇒ 超时不发撤销。
    ⛔ **E-0022：对端 HTTP 在事务外**（本仓库无 `ATOMIC_REQUESTS`，`order_intake.py` 注释在案）——预占事务已提交、本函数的落库各自成事务；⛔ 本函数不打第二个 HTTP 口（唯一出站口 `peer_client.push_events`）。⚠️ 每次同步推都 `logger.info` 记**耗时毫秒 + 结局**（D-0347 实测承诺：上线第一天统计 p50 / p90 / 超时率，超时常量按实测调）。⚠️ 判成败只认响应里**显式**的 `claim_replies`（E-0087 同族：失败信封必须判失败、拿不到业务结果不猜）；成交只可能来自真实落地的 `order.assign`。⚠️ 机器路径与多跳中间层**不进本函数**（照旧 outbox，`queue_claim=True`）。"""
    from django.db import transaction

    from apps.business import peer_client, peer_dispatch
    from apps.business.jidanpu_common import ALREADY_TAKEN_MESSAGE
    from apps.business.peer_inbound import apply_events
    from apps.core.error_codes import ErrorCode

    now = now or timezone.now()
    # ① 本地预占（闸门 / 押金 / 条件更新全在既有函数里；不排 outbox）。⛔ 同一事务提交
    #    之后才准打对端（E-0022）。
    row = claim_peer_mirror(
        user_id=user_id, mirror_id=mirror_id, now=now,
        enforce_deposit=enforce_deposit,
        enforce_limits=enforce_limits,
        queue_claim=False,
    )
    # ⚠️ `claim_peer_mirror` 已 `select_related("peer_upstream_binding")` ⇒ 直读缓存，不回库。
    binding = row.peer_upstream_binding
    started = time.monotonic()
    outcome: str | None = None
    reply_events: list[dict] | None = None
    try:
        data = peer_client.push_events(
            binding.peer_base_url,
            binding.token_out,
            [{
                "idempotency_key": uuid.uuid4().hex,
                "kind": "hall.claim",
                "target_ref": row.peer_order_no,
                "occurred_at": now.isoformat(),
                # 载荷与 outbox 路同一份（`peer_dispatch.hall_claim_payload`，⛔ 不抄两份）。
                "payload": peer_dispatch.hall_claim_payload(taker_id=user_id),
            }],
            timeout=_peer_claim_sync_timeout(),
        )
    except (peer_client.PeerUnreachable, peer_client.PeerRejected) as exc:
        logger.warning(
            "peer 同步抢单：对端没回话（%s），按未响应处理 mirror=%s ref=%s",
            type(exc).__name__, row.pk, row.peer_order_no,
        )
    else:
        outcome, reply_events = _claim_reply_events_for(
            data, target_ref=row.peer_order_no
        )
    if reply_events:
        try:
            with transaction.atomic():
                # 与 worker 收到时**同一个入口**：`claim_ok` 只记账、`claim_refused` 分因、
                # `order.assign` 建单 + 转正（被跳过 ⇒ 行不动 ⇒ 走下面各自的收口）。
                apply_events(events=reply_events, binding=binding)
        except Exception:
            # 落库失败按「未响应」放回（可恢复：对端 outbox 照常重发同一批事件）。
            # ⛔ 不让一个可恢复的落库异常给打手 500；poison 事件由异步重投路径响亮。
            logger.exception(
                "peer 同步抢单：应答落地失败，按未响应处理 mirror=%s", row.pk,
            )
            outcome, reply_events = None, None
    duration_ms = int((time.monotonic() - started) * 1000)

    def _release_to_hall() -> None:
        """给不出终局 ⇒ 当场放回大厅（条件更新：并发已收口时命中 0 行、尊重那一次）。"""
        PeerHallMirror.objects.filter(
            pk=row.pk,
            status=PeerHallMirror.Status.CLAIMING,
            claiming_at=row.claiming_at,
        ).update(
            status=PeerHallMirror.Status.LISTED,
            claiming_by=None,
            claiming_at=None,
            last_error=PEER_CLAIM_UNRESPONSIVE_NOTE[:255],
        )
        row.refresh_from_db(fields=["status", "claiming_by", "claiming_at"])

    def _unresponsive(reason: str) -> ClaimError:
        logger.info(
            "peer 同步抢单：未响应（%s）mirror=%s ref=%s 耗时=%dms",
            reason, row.pk, row.peer_order_no, duration_ms,
        )
        return ClaimError(
            PEER_CLAIM_UNRESPONSIVE_MESSAGE, ErrorCode.PLATFORM_QUERY_FAILED
        )

    if outcome not in ("accepted", "refused"):
        # ② 给不出终局（超时 / 老对端无键 / 多跳转发 / A9 重放只带 claim_ok / 形状坏 /
        #    落库失败）⇒ 当场放回 + 1017。
        _release_to_hall()
        raise _unresponsive("no-verdict")
    # ⚠️ 重取一行再映射：上面的 `refresh_from_db` 只刷了三列，实例上的 `promoted_order`
    #    延迟外键还是预占时刻的旧值（`None`）——拿旧值判会把自己成交的单说成没成交。
    row = PeerHallMirror.objects.filter(pk=row.pk).first()
    if outcome == "accepted":
        if row.status == PeerHallMirror.Status.PROMOTED:
            logger.info(
                "peer 同步抢单：当场成交 mirror=%s ref=%s order=%s 耗时=%dms",
                row.pk, row.peer_order_no, row.promoted_order_id, duration_ms,
            )
            return row
        # 转正被跳过（建单支 M-8 档：提成配置坏 / 金额越界 ⇒ 事件 skipped）或并发抢先
        # ⇒ 这一枪没拿到终局。⚠️ **不放回**：异步重投的 `order.assign` 马上会再试，
        # 放回反而把将成的单送进未分配池；60 秒回收仍是这一档的兜底。
        raise _unresponsive("promotion-pending")
    logger.info(
        "peer 同步抢单：对端回绝（%s）mirror=%s ref=%s 耗时=%dms",
        row.status, row.pk, row.peer_order_no, duration_ms,
    )
    raise ClaimError(ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
