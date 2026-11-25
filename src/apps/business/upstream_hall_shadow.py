"""上家鸡蛋铺镜像卡派生的**隐藏单**（`Order.upstream_hall_card`，D-0325 业务方拍板）。 ## 为什么要有这张单我方大厅里摆的「上家鸡蛋铺镜像卡」是 `JidanpuUpstreamHallOrder`（**不是**我方 `Order`）。而「抛到下家鸡蛋铺大厅」那条同步（`jidanpu_member.queue_hall_launch`）只认 `Order` ⇒ 卡在厅里时，开了 `JidanpuMemberBinding.hall_sync_enabled` 的下家鸡蛋铺**收不到**它（这就是业务方说的「目前是不能的」）。业务口径（D-0325）：**上家镜像卡挂在我方大厅期间，也要抛给所有开了大厅同步的下家鸡蛋铺** ⇒ 给每张 `listed` 的卡派生一张 `Order` （`upstream_hall_card` 指回那张卡），建单后立刻走**既有** `queue_hall_launch` （它自己按 `hall_sync_enabled` / 启用 / 范围 / 同地址去重过滤，⛔ 本模块不另写一套）。 ## 与 peer 线 `hall_shadow` 的关系（并列且互斥，⛔ 不合并）两条线**零关系口径**（`models.py` PeerHallMirror 一侧的既有约定）：peer 线的隐藏单挂 `Order.hall_card`，本线挂 `Order.upstream_hall_card`；一张隐藏单两列最多一列非空（生命周期唯一入口保证）。本模块照 `hall_shadow.py` 的三钩子模板写，但判据独立维护（价格算法、生命周期状态机都是上家线自己的）。 ## 为什么必须**隐藏**（与 `hall_shadow` 同一条口径）这张单只是「把卡片同步给下家大厅」的载体，**不是**本租户的一笔生意 ⇒ 所有面向人的订单读面统一排除（`exclude(upstream_hall_card__isnull=False)`，读点登记： `tests/test_order_read_path_net.py`）；**详情按单号仍可打开**（客服排障要看得到）。 ## 生命周期（三条，各有唯一入口；调用点在 `jidanpu_upstream` 的五类事件上） | 时刻 | 谁调 | 做什么 | |---|---|---| | 镜像行 `listed`（新建 / gone 复活 / skipped→listed / 每轮刷新） | `ensure_for_card` | 建（`WAITING_GRAB`）或**复活**同一张，建 / 复活后 `queue_hall_launch`（**硬闸**：排除来源上家地址）；已是待抢 ⇒ 只对账展示价（`sync_price_for_card`）。⚠️ 整条面受 **D-0329 常量水位**管（见下；改准：总闸撤除、随发版自动生效） | | 本租户打手把卡抢走、卡转正 | `jidanpu_upstream.promote_upstream_mirror` 复用优先支 | **同一张单接着用**：挂打手 / 建绩效行 / `IN_PROGRESS`，并清空 `upstream_hall_card`（转正后它就是我方真单，必须回到列表）；⭐ **改准（2026-10-30 D-0342）**：同一次改写 `source_order_no` 为上家展示号（D-0272 全端口径；转正后回环语义反转——见模块文件头「与 peer 线关系」下方的行为矩阵出处） |
| 镜像行 `gone`（消失 / 被别家接走 / 回环拦截 / 僵尸卡下架 / 接单被拒） | `refund_for_card` | 落 `REFUNDED`（**不清 `upstream_hall_card`**），并走既有 `queue_withdraw` 撤下家副本 | ## 价闸（D-0325 拍板②：0 元卡 / 负价卡不抛）本线展示价**恒有值**（`upstream_hall_price` 缺价按 `0.00` 算、负价放行 D-0309）⇒ 照搬引擎会把 0 元 / 负价卡真抛进下家大厅。⇒ `ensure_for_card` 第一道闸：展示价 `<= 0` ⇒ **不派生**、只记一次台账（`upstream.hall_throw_no_price`，reason 区分 0 元 / 负价）；已派生的卡后来改价成 `<= 0` ⇒ `refund_for_card`（下家大厅不留错价卡）。 ⚠️ **1:1（`Order.upstream_hall_card` 是 OneToOne）**：同一张卡只有一张隐藏单；重复拉取轮都走 `get_or_create`，⛔ 不会建出第二张。 ⚠️ **⛔ 不在这里建绩效行**：隐藏单无人接手（绩效行是「接单」的事实，转正那一刻才建）。 ⚠️ **隐藏单生命周期不进 180 秒对账**（`orders_of` / `reconcile` 双双排除）⇒ **绑定后启用不补推先前进厅的卡**（与 peer 线现状对齐；卡复活 / 重进厅时钩子重抛，D-0325 拍板⑧）。 ## 自动生效 + 常量水位（D-0329 改准，2026-10-29 业务方拍板） D-0325 的派生是「每张 `listed` 卡都抛」——直接发版会把**全部存量** listed 卡（现场实查 **823 张**，其中 734 张挂在已停用的自家上游绑定上；租户名不进 git，原始记录只在不进 git 的 `.tools/recon/`）整批灌给下家（× 3-4 家启用下家 = 首轮 2500-3300 条真实录单，事故级）。**2026-10-28 那一轮的裁量是「租户开关 `hall_upstream_throw` + 开关行水位」；业务方当晚拍板改准：原话口径「同步订单是一整个功能」⇒ 总闸撤除，功能随发版自动生效**（第五车已把带总闸的版本发上线、两台均无覆盖行 = 一直零派生；改准后总闸从注册表撤掉）。下家「收不收」仍由绑定上 **既有的**两开关管（`JidanpuMemberBinding.enabled` + `hall_sync_enabled`）—— 那才是用户心智里的阀，本模块一处都不复制。 ⇒ `ensure_for_card` 入口处**一道常量水位闸**：只处理 `first_seen_at >= UPSTREAM_THROW_SINCE` 的卡。`UPSTREAM_THROW_SINCE` 钉在**第五车发版时刻**（2026-10-29 20:30 Asia/Shanghai；实发 kexing 20261029T202949 / suqing 20261029T203049，读数在 `docs/STATUS.md`）——存量 823 张
的进厅时刻全部远早于它 ⇒ 「存量不补抛」的原判据原样保留；「为什么是常量不是开关行」：总闸撤了就没有「开启时刻」可读，水位改钉发版时刻（改准前它是开关行的 `updated_at`）。取 20:30 而不是各台精确到秒的发版完成时刻：水位**略晚**于 kexing 的 20:29:49 只会让发版前后几十秒内新进厅的卡**少抛**（下一轮拉取也追不回，属可接受的边缘），⛔ 不会反出「把存量整批灌给下家」的事故方向。 ⛔ **闸只管「新建 / 复活 / 对价」这条派生面**：`refund_for_card`（回收路）**不受水位管**——已有隐藏单的卡下架必须照常撤（一致性优先，别把「暂停新抛」写成「断链不回收」）。 **残差（如实登记）**：已派生的隐藏单**不撤**——它们走完自己的生命周期（卡 gone 时由 gone 路的 `refund_for_card` 收走；在厅期间本模块对它不再对价、也不再补抛）。 **要补抛存量另行按需命令。** ## 人工退款优先（S-03，D-0331，业务方拍板） **一句话**：隐藏单被人手动退款后，下一轮拉取**不得**静默撤销该退款——复活支只认「本引擎自己退的」。判据与两支处置（为什么这么定，每条都有读码出处）： - **怎么分「引擎退的」与「人工退的」**：本引擎把隐藏单翻成 `REFUNDED` 的**唯一出口**是 `refund_for_card`（全部调用点六处：`_mark_gone` 的离厅支、拉取侧回环拦截的存量卡支、停用绑定僵尸卡下架、`_claim_refused` 请求路与 outbox 重试路、本模块价闸——除价闸外各处卡都已转 `gone`，价闸卡留在厅）；人工退款走订单域的 `apply_transition` 漏斗，**不经过** `refund_for_card`。⇒ `refund_for_card` 在**翻转成功的同一事务**里写一条**引擎退款标记**台账（`upstream.hall_shadow_refunded`，payload 带 `order_no` + 与订单行同一次写进去的同一个 `updated_at` 时刻）；复活判据 = 「存在标记，且标记里的时刻 == 订单行现在的 `updated_at`」＝**这次**退款是引擎退的、且此后没有别的手碰过这张单。⛔ 不按「标记行存在」裸判：同一张卡多个 gone 周期会各留一条，老周期的标记不得给后来的**人工**退款背书。⛔ 也不比「台账时间与订单时间谁晚」：夹具/跨机时钟表示不可靠，**同一次写入的同一个值**才是精确判据（`refund_for_card` 用同一个 `refund_at` 写订单行与标记，读回时按时刻相等对拍）。 - **人工退款优先支**：卡 `listed` 而隐藏单 `REFUNDED` 且标记对不上 ⇒ **不复活、不补抛、不对价**，记一次人话台账（`upstream.hall_manual_refund`，`note_once` 幂等）后回 `None`。软删档（`deleted_at` 非空）**不走**本支、照旧复活——软删是另一个人工动作、有自己的语义（回收站恢复守卫钉着它），S-03 只接管退款档。 - **卡要不要顺带下架（gone）：不下架**。理由：①镜像的生命周期属**上家**——卡仍挂在上家大厅，本地置 `gone` 会被下一轮拉取的 D-0300 复活支翻回 `listed`（「看见了就是直接证据」），每轮 listed→gone→listed 打摆，且每轮复活都写一条**不幂等**的 `upstream.hall_rescued`（`_record` 形态）⇒ 台账风暴 + 大厅闪卡 + 快照反复写清； ②下游一致性已由人工退款自身的漏斗覆盖：`apply_transition` 的出站段给**已有链路**的绑定排 `member.status_out`（目标态 REFUNDED，消费端把下家那张副本退掉），与本引擎 `queue_withdraw` 同样按推送证据到达、同一覆盖面；本支再补退是重复（且 `refund_for_card` 对已 `REFUNDED` 的单本来就翻不动，无可复用）。 - **残差（如实登记，不在本拍板面内）**：打手**抢卡**支——卡 `listed` 就在我方大厅可见可抢，`promote_upstream_mirror` 的复用支会把 `REFUNDED` 隐藏单收编回 `IN_PROGRESS` （D-0325 拍板的「gone 期退掉又被接单」复活场景与它共用同一入口）。那是**人主动接单**、不是拉取轮的静默撤销；要不要给「人工退款后的卡」拦接单属另一拍板。 - **已知残差（2026-10-30 评审 F2，登记不修）**：引擎退款翻成功后、复活判据读之前，若订单行被**无关写入**（人工迁移 / admin 直改等）换掉 `updated_at` ⇒ 标记失配 ⇒ 被认成人工退：不复活、落一条 `hall_manual_refund`（此刻认不出，方向保守——不动钱、不重抛）。真实可达性低：窗口 ≤ 一个拉取轮（快路 5s / 全量 300s）、隐藏单不入任何读面、窗口内自动写入点枚举为 0。"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from apps.business.models import JidanpuSyncEvent, JidanpuUpstreamHallOrder, Order, OrderStatus

logger = logging.getLogger(__name__)

#: ⭐ D-0329（业务方拍板「同步订单是一整个功能」，总闸 `hall_upstream_throw` 撤除、功能随发版自动生效）后的**常量水位**：只派生 `first_seen_at >= 本时刻` 的卡。取值 = **第五车发版时刻**（2026-10-29 20:30
#: Asia/Shanghai；实发 kexing 20261029T202949 / suqing 20261029T203049，读数在 `docs/STATUS.md`）——存量 listed 823 张的进厅时刻全部远早于它 ⇒ 「存量不补抛」
#: 原判据原样保留；改准前水位是开关行的 `updated_at`，总闸撤了就没有「开启时刻」可读 ⇒ 改钉发版时刻常量。⛔ 想让更老的卡也派生 = 「补抛存量」，那要另行按需命令，⛔ 不要顺手改小这个常量（那是把 823 张整批灌给下家的事故方向）。
UPSTREAM_THROW_SINCE = timezone.make_aware(
    datetime(2026, 10, 29, 20, 30), timezone.get_current_timezone()
)

#: ⭐ D-0325 拍板②的台账 kind（形态照 `upstream.hall_skipped` 那一族：direction=in、
#: 只登记不驱动动作）。⛔ 不复用 peer 线 / member 线的 kind：那条下拉是恰好集合网。
KIND_THROW_NO_PRICE = "upstream.hall_throw_no_price"
#: 0 元（含「上家没给价 ⇒ 展示价 0.00」那一档）：不抛。
THROW_NO_PRICE_ZERO_REASON = (
    "上家镜像卡展示价为 0（上家没给接单价或 0 元单），不抛给下家鸡蛋铺"
)
#: 负价（D-0309 负价放行 ⇒ 展示价可能是负数）：不抛。
THROW_NO_PRICE_NEGATIVE_REASON = "上家镜像卡展示价为负（负价单），不抛给下家鸡蛋铺"

#: ⭐ S-03（D-0331 业务方拍板「人工退款优先」）：`refund_for_card` **翻转成功** 的同一事务里写的**引擎退款标记**。台账形态照 `upstream.hall_gone` 那一族
#: （direction=in、`_record` 直写）——⛔ **不用 note_once**：同一张卡多个 gone 周期各退一次，每次都要一条**新**标记，四元组幂等会把后续周期的标记吞掉、老标记就会给后来
#: 的人工退款背书。判据读法见 `_engine_refund_alive`。kind 限长 32（`JidanpuSyncEvent.kind` max_length），本值 29 ✓。
KIND_SHADOW_REFUNDED = "upstream.hall_shadow_refunded"
#: 标记行 payload 里「订单行被写成退款态那一刻」的键（与订单 `updated_at` **同一个值**，
#: 见 `refund_for_card`；复活判据按时刻相等对拍）。
PAYLOAD_REFUNDED_AT = "order_updated_at"
#: 标记行的 reason（这条台账兼作「引擎退了隐藏单」的留痕——此前引擎退款在台账上无痕，
#: 只剩下家 withdraw 一行间接证据）。
ENGINE_REFUND_MARK_REASON = "上家镜像卡离开大厅（或触发价闸），派生隐藏单随之退款并撤下家副本"

#: ⭐ S-03 的人工退款优先台账（`note_once` 幂等：同一张卡 + 同一句原因只记一条；重复轮
#: 静默跳过，⛔ 不每轮灌一条）。kind 限长 32，本值 27 ✓。
KIND_MANUAL_REFUND = "upstream.hall_manual_refund"
#: 人工退款优先支的 reason（人话、点名处置与恢复路径；措辞对「引擎退后又被人工碰过」
#: 的场合同样成立——判据本来就是「不是引擎（此刻认得出）的那次退款」）。
MANUAL_REFUND_REASON = (
    "镜像卡仍在上家大厅，隐藏单已是「已退单」且不是本引擎的卡下架退款"
    "（人工退款优先）：本卡不再派生、不再重抛、不再对价；如需恢复请人工重新指派"
)


def _display_price(mirror: JidanpuUpstreamHallOrder) -> Decimal:
    """这张卡的**我方大厅展示价**＝它的隐藏单发单价基数（D-0325 拍板①）。唯一算法出处 `jidanpu_upstream.upstream_hall_price`（`recFee × 上家绑定
    hall_price_percent / 100`，缺价按 0 算）——与大厅投影 `serialize_upstream_row`、抢单闸 `claim_upstream_mirror`、转正 `promote_upstream_mirror` **同一份**，
    ⛔ 本模块不另算一遍百分比。恒回 `Decimal`（本线没有「无价」那一档）。函数内 import（环）：`jidanpu_upstream` 的同步轮会反向调本模块的钩子。"""
    from apps.business import jidanpu_upstream

    return jidanpu_upstream.upstream_mirror_hall_price(mirror)


def _shadow_of(mirror: JidanpuUpstreamHallOrder) -> Order | None:
    """这张卡的隐藏单（含软删行——复活判据要看 `deleted_at`，默认 manager 看不见）。"""
    return Order.all_objects.filter(upstream_hall_card=mirror).first()


def _projection_fields(mirror: JidanpuUpstreamHallOrder, *, price: Decimal) -> dict:
    """卡 → 隐藏单的投影字段（建单与复活**同一份**，⛔ 不抄第二处）。 - `total_price` 照抄上家 `recFee`（客户价不受任何百分比影响——转正支 A9 同口径；缺价落 `0.00`，`total_price` 是非空列）； - `hall_price` = 展示价（下家抛单价基数 = 我方发单价，`_throw_fee` 再乘绑定 throw%）；
    - `source_order_no` = **上家内部单号 `upstream_order_id`**（镜像表幂等键）， ⛔ 不取展示号 `orderIdView`——同一家店既是上家又是下家（租户 S-chuyin）时，展示号同号会让拉取侧回环拦截把**原卡**误杀（勘察 brief §3.3 分叉 B）；转正**改写**为展示号（2026-10-30 D-0342 改准：创建期键=内部号防误杀、转正后键=展示号
    让真回环命中——行为矩阵见 `docs/plans/2026-10-30-promote-source-no.md`；改写点在 `jidanpu_upstream.promote_upstream_mirror` 复用支）； - 区服 / 项目 FK **刻意留空**：上家给的是文本区服，没有我方 FK； - `project_text`（镜像列宽 300 < 订单列宽 900）/ `remote_role_name`（128 == 订单 `game_role_name` 128）宽度天然够，无需再截。"""
    return {
        "game": mirror.game,
        "project_text": str(mirror.project_text or ""),
        "total_price": (
            mirror.remote_rec_fee
            if mirror.remote_rec_fee is not None
            else Decimal("0.00")
        ),
        "hall_price": price,
        "remark": str(mirror.remote_remark or ""),
        "game_role_name": str(mirror.remote_role_name or ""),
        "source_order_no": str(mirror.upstream_order_id or ""),
    }


def _note_no_price(mirror: JidanpuUpstreamHallOrder, price: Decimal) -> None:
    """价闸台账（一次，`note_once` 幂等：同一张卡 + 同一句原因只记一条）。"""
    from apps.business import jidanpu_upstream

    reason = (
        THROW_NO_PRICE_NEGATIVE_REASON
        if price < Decimal("0.00")
        else THROW_NO_PRICE_ZERO_REASON
    )
    jidanpu_upstream.note_once(
        mirror=mirror, kind=KIND_THROW_NO_PRICE, status="skipped", reason=reason,
    )


def _queue_launch(order: Order, mirror: JidanpuUpstreamHallOrder) -> int:
    """抛给所有「开启了大厅同步」的下家绑定——**硬闸在这里**（D-0325 拍板③）。 `exclude_addresses={来源上家地址}`：绝不推回该卡的来源上家（租户 S-chuyin 既是上家
    又是下家的现场，不排除就立刻回环）。判据（`queue_hall_launch` 那一处）按**原样字符串地址**比对；拉取侧 `_loop_throw_keys` 只是兜底，⛔ 不当闸门用。"""
    from apps.business import jidanpu_member

    return jidanpu_member.queue_hall_launch(
        order, exclude_addresses={mirror.upstream_base_url}
    )


def _hall_ref(mirror: JidanpuUpstreamHallOrder) -> str:
    """这条线的台账 `target_ref`（形状照 `jidanpu_upstream.note_once` 的 mirror 支：
    `(上家地址, 上家单号)`——镜像表的唯一键；截断宽度同源 `_TARGET_REF_MAX` = 191）。"""
    return f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"[:191]


def _engine_refund_event_fields(
    mirror: JidanpuUpstreamHallOrder, order: Order, *, refund_at: datetime
) -> dict:
    """每次退款用相同的订单更新时间留一条凭证，供复活时区分人工退款。"""
    return {
        "direction": "in",
        "target_type": "hall", "target_ref": _hall_ref(mirror),
        "reason": ENGINE_REFUND_MARK_REASON,
        "payload": {
            "order_no": order.order_no,
            PAYLOAD_REFUNDED_AT: refund_at.isoformat(),
        },
    }


def _engine_refund_alive(mirror: JidanpuUpstreamHallOrder, order: Order) -> bool:
    """这张隐藏单**此刻**的退款态是不是本引擎 `refund_for_card` 留下的（S-03 判据读半边）。判据 = 「存在**这张单**的引擎退款标记，且**最新一条**标记里钉的时刻与订单行现在的 `updated_at` 相等」。两个「且」各自承重： - **按单过滤**（payload.order_no）：target_ref 是卡粒度，同一张卡的多个周期可能对应
    同一张隐藏单，但 payload 钉的是单号——防串单； - **时刻相等**：`refund_for_card` 把 `refund_at` 同一次写进订单行与标记 ⇒ 引擎退款之后 **没有任何别的手碰过这张单**（人工退款 / 改单都会换掉 `updated_at`）两半才成立。
    老周期的标记对不上后来人工退款写下的时刻 ⇒ 不会给人工退款背书。回 `False` = 认不出是引擎退的（无标记 / 标记是别的单的 / 时刻对不上）⇒ 调用方走人工退款优先支。时刻比较用 `parse_datetime` 还原后按**时刻**相等（aware datetime 的 `==` 比瞬间不比表示，时区偏移写法不同不影响）。"""
    row = (
        JidanpuSyncEvent.objects.filter(
            kind=KIND_SHADOW_REFUNDED, target_type="hall", target_ref=_hall_ref(mirror),
            payload__order_no=order.order_no,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if row is None:
        return False
    raw = (row.payload or {}).get(PAYLOAD_REFUNDED_AT)
    if not raw:
        return False
    stamped = parse_datetime(str(raw))
    return (
        stamped is not None
        and order.updated_at is not None
        and stamped == order.updated_at
    )


def _note_manual_refund(mirror: JidanpuUpstreamHallOrder) -> None:
    """人工退款优先支的台账（一次，`note_once` 幂等；mirror 支自动带归属绑定的
    `upstream_binding_id`）。写不写得出新行都不影响处置——处置由判据回答，台账只留痕。"""
    from apps.business import jidanpu_upstream

    jidanpu_upstream.note_once(
        mirror=mirror, kind=KIND_MANUAL_REFUND, status="skipped",
        reason=MANUAL_REFUND_REASON,
    )


def ensure_for_card(mirror: JidanpuUpstreamHallOrder, *, now=None) -> Order | None:
    """卡**在厅里** ⇒ 取/建/复活它的隐藏单；建了 / 复活了就往合格下家抛一次。回隐藏单（拿不到前置或被价闸拦下 ⇒ `None`）： - `mirror` 缺 / `status != listed` ⇒ `None`（`gone` / `claiming` / `promoted` 都不该有「待抢单」形态的载体；`claiming` 期间旧隐藏单保持原状等转正/被拒）； - `game is None` ⇒ `None`（`Order.game` 非空；镜像行 `listed` 恒有 game，这一档只防数据被手改成畸形，⛔ 不为它建单）； - **常量水位**（D-0329 改准，2026-10-29 自动生效）：只处理 `first_seen_at >= UPSTREAM_THROW_SINCE`（第五车发版时刻）的卡——存量在厅卡
    不补抛；租户总闸 `hall_upstream_throw` 已撤（「同步订单是一整个功能」），下家收不收由绑定自己的 `enabled` + `hall_sync_enabled` 管（在 `queue_hall_launch` 那一侧）。⚠️ 闸只管这条派生面：`refund_for_card`（回收路）不受它管；已派生的隐藏单不撤、走完自己的生命周期（模块 docstring 「自动生效 + 常量水位」一节）； - **价闸**：展示价 `<= 0`（0 元 / 负价）⇒ 已有隐藏单先退（`refund_for_card`），记一次台账后回 `None`（D-0325 拍板②）； - 已有隐藏单且仍 `WAITING_GRAB` ⇒ 原样返回前先对一次价（`sync_price_for_card`，同值 no-op）——每轮 listed 刷新都会走到这里，上家改价就是靠它传导到下家；
    - 已有隐藏单但 `REFUNDED` 且**是本引擎退的**（`_engine_refund_alive`：卡 gone 后又复活 / 被清理过、价闸退后价又回来）⇒ **同一张**单回 `WAITING_GRAB` + 刷新投影 + 重新 `queue_hall_launch`（⛔ 不新建第二张）； - ⭐ **人工退款优先**（S-03 / D-0331，2026-10-30 业务方拍板）：隐藏单 `REFUNDED` 而**认不出是本引擎退的**（`refund_for_card` 的同事务标记对不上——人工经 `apply_transition` 退款不经过它）⇒ **不复活、不补抛、不对价**，记一次 `upstream.hall_manual_refund` 台账后回 `None`。卡**不下架**：镜像生命周期属上家（本地置 gone 会被 D-0300 复活支每轮翻回 listed 打摆），下游副本由人工退款自身的出站漏斗（`member.status_out`）撤——完整理由与残差见模块 docstring「人工退款优先」。软删档（`deleted_at` 非空）**不走**本支、照旧复活。 ⚠️ `queue_hall_launch` 的闸门（`hall_sync_enabled` / 启用 / 范围 / 同地址去重 / 硬闸）**一处都不在这里复制**——那是它的既有判据（D-0299 / D-0325）。"""
    if mirror is None or mirror.status != JidanpuUpstreamHallOrder.Status.LISTED:
        return None
    if mirror.game_id is None:
        logger.warning(
            "上家镜像卡没有游戏，跳过隐藏单派生：binding=%s card=%s",
            mirror.owner_binding_id, mirror.upstream_order_id,
        )
        return None
    # ⭐ D-0329 改准（业务方拍板：「同步订单是一整个功能」⇒ 租户总闸 `hall_upstream_throw` 撤除、随发版自动生效）：**常量水位**——只派生「水位之后新进厅」的卡；存量在厅卡（现场实查 upstream listed 823 张的场景——租户名不进
    # git）不补抛。改准前这里是「开关关 ⇒ 零派生 + 开着才看开关行 updated_at 水位」两道闸；改准后只剩 `first_seen_at >= UPSTREAM_THROW_SINCE` 一道。 ⚠️ 闸只管「新建 / 复活 / 对价」这条派生面；`refund_for_card`（回收路）⛔ 不受
    # 本闸管——已有隐藏单的卡下架必须照常撤（一致性优先）。残差（如实登记）：已派生的隐藏单**不撤**——走完自己的生命周期（卡 gone 时由 gone 路的 `refund_for_card` 收走；在厅期间本模块对它不再对价、也不再补抛）。
    if mirror.first_seen_at < UPSTREAM_THROW_SINCE:
        return None
    price = _display_price(mirror)
    if price <= Decimal("0.00"):
        # ⭐ 价闸：先撤掉此前可能已抛出去的副本（下家大厅不留错价卡），再记台账。
        refund_for_card(mirror, now=now)
        _note_no_price(mirror, price)
        return None
    order = _shadow_of(mirror)
    # ⭐ S-03 / D-0331 人工退款优先支（判据与完整理由见模块 docstring「人工退款优先」）：
    # 退款态 + 认不出是本引擎退的 ⇒ 不复活、不补抛、不对价，台账一次，本轮回 None。放在 atomic 块**外**：本支不写订单行，只是「这轮跳过 + 留痕」，无需占事务；
    # 软删档（deleted_at 非空）刻意不进本支——它有自己的复活语义（S-03 只接管退款档）。
    if (
        order is not None
        and order.deleted_at is None
        and str(order.status) == str(OrderStatus.REFUNDED)
        and not _engine_refund_alive(mirror, order)
    ):
        _note_manual_refund(mirror)
        return None
    with transaction.atomic():
        if order is None:
            # ⚠️ **必须走 `all_objects`**（照 `hall_shadow` 的同一理由）：默认 manager
            #    看不见软删行 ⇒ 查不到会去新建，而 OneToOne 的唯一索引会撞库 500。
            order, created = Order.all_objects.get_or_create(
                upstream_hall_card=mirror,
                defaults={
                    "order_no": _new_order_no(),
                    "status": OrderStatus.WAITING_GRAB,
                    "server": None,
                    "category": None,
                    "project": None,
                    **_projection_fields(mirror, price=price),
                },
            )
            if created:
                _queue_launch(order, mirror)
                return order
            # get_or_create 输给并发（对方已建）⇒ 走下面的复活 / 刷新判据，不重复抛。 ⭐ S-03 / D-0331：复活判据在此**收窄**——退款档只有「引擎退的（标记对得上）」
            # 才算可复活。人工退款 normally 已被入口处的优先支接走；这里再判一次是给「入口读到无单、atomic 里 get_or_create 输给并发」那一条缝兜底（最后一道
            # 条件更新必须与判据同口径，⛔ 不留给未判过的 REFUNDED 一条复活路）。
        revivable = order.deleted_at is not None or (
            order.status == OrderStatus.REFUNDED
            and _engine_refund_alive(mirror, order)
        )
        if not revivable:
            # 已在厅（WAITING_GRAB）：只对一次价（同值 no-op；上家改价 / 换绑定百分比
            # 的传导点）。⚠️ `claiming` 期间的卡不会走到这里（上面第一道闸拦了）。
            sync_price_for_card(mirror, now=now or timezone.now())
            return order
        # 复活：卡 gone 后又回大厅 / 隐藏单被软删后卡又落厅 ⇒ **同一张**单回待抢。
        # ⚠️ 条件更新用 `all_objects`（默认 manager 的软删过滤对 `.update()` 的匹配集
        #    生效：软删行会匹配 0 行，复活静默失败）。
        fields = _projection_fields(mirror, price=price)
        updated = Order.all_objects.filter(pk=order.pk).filter(
            Q(status=OrderStatus.REFUNDED) | Q(deleted_at__isnull=False)
        ).update(
            status=OrderStatus.WAITING_GRAB,
            deleted_at=None,
            deleted_by="",
            updated_at=now or timezone.now(),
            **fields,
        )
        if not updated:
            # 读到落写之间被别人改了（并发复活 / 人工处置）⇒ 尊重那一次。
            return order
        from apps.business.order_writes import queue_externalpay_recheck
        queue_externalpay_recheck(order)
        # 实例回填：同一事务里的后续代码 / 返回值会读这个实例（判据仍是 `.update()`
        # 落的那一行——下游消费端按单号现查 DB）。
        order.status = OrderStatus.WAITING_GRAB
        order.deleted_at = None
        order.deleted_by = ""
        for name, value in fields.items():
            setattr(order, name, value)
        _queue_launch(order, mirror)
        return order


def _new_order_no() -> str:
    """订单号走**既有**生成器（⛔ 不在这里另拼一段数字）。"""
    from apps.business.order_no import generate_order_no

    return generate_order_no()


def sync_price_for_card(mirror: JidanpuUpstreamHallOrder, *, now) -> bool:
    """把**大厅展示价**对到这张卡的隐藏单上（每轮 listed 刷新的唯一落价口）。回「这次真的写了吗」——`False` 覆盖四种 no-op（无卡 / 价 `<= 0` / 单不在待抢 / 同值）。 - **只动 `WAITING_GRAB`**：转正后的单是生意价，⛔ 不能被上家后续改价改写
    （照 `hall_shadow.sync_hidden_order_price` 的同一条口径）； - **价 `<= 0` 在这里不写**（⛔ 不把负价 / 0 写进已抛出的单）：那一档的处置（退款 + 撤副本 + 台账）在 `ensure_for_card` 的价闸里；
    - 与 `order.hall_price` 相等 ⇒ no-op：不刷 `updated_at`、不排下家改价事件； - 写库后把新价回填内存实例，拿**同一实例**调既有 `queue_price_update` （它只读 pk / 链路，价由消费端现查 DB）。"""
    from apps.business import jidanpu_member

    if mirror is None:
        return False
    price = _display_price(mirror)
    if price <= Decimal("0.00"):
        return False
    qs = Order.objects.filter(
        upstream_hall_card=mirror, status=OrderStatus.WAITING_GRAB
    )
    order = qs.first()
    if order is None or order.hall_price == price:
        return False
    qs.update(hall_price=price, updated_at=now)
    order.hall_price = price
    order.updated_at = now
    jidanpu_member.queue_price_update(order)
    return True


def refund_for_card(mirror: JidanpuUpstreamHallOrder, *, now=None) -> int:
    """卡**离开上家大厅**（`gone` 一族）⇒ 隐藏单跟着 `REFUNDED`，并撤下家副本。回「这次真的退掉了几张」（0 / 1）——逐条收口让调用方把「真的翻了」与「并发已处理」分开（照 `hall_shadow.refund_for_card` 的同一口径）。 - **只有 `WAITING_GRAB` 会翻**：其余状态（`IN_PROGRESS` 等说明已转正——那一支转正时已清空 `upstream_hall_card`，走到这里说明数据被手改）⇒ ⛔ 不覆盖、
    也不报错（返回 0 是「这次没有翻任何一张」的诚实答复，别当异常上抛）； - ⭐ **翻转成功 ⇒ 同事务写一条引擎退款标记**（`_mark_engine_refunded`，S-03 / D-0331）：本函数是引擎把隐藏单翻 `REFUNDED` 的**唯一出口**（人工退款走 `apply_transition`、不经过这里），这条标记就是 `ensure_for_card` 复活支区分「引擎退的（可复活）」与
    「人工退的（人工优先、不复活）」的判据写半边。已 `REFUNDED` 的单翻不动 ⇒ 也不写标记（并发输给人工退款时不会留下冒名的引擎凭证）； - **`upstream_hall_card` 不清**：退掉的派生单仍属隐藏面（它没有真实客户与退款额， ⛔ 不许在列表里冒充一笔真退单）； - 下家大厅的副本走**既有** `jidanpu_member.queue_withdraw`（按推送证据找绑定、同店接手人闸在消费侧兜着——⚠️ 隐藏单没有 MAIN 接手人，那道闸救不了它，「上游 gone + 下家已接」竞态由 `_consume_withdraw` 的写前回读闸保护，E-0236 口径）。"""
    from apps.business import jidanpu_member

    if mirror is None:
        return 0
    order = _shadow_of(mirror)
    if order is None or order.deleted_at is not None:
        return 0
    with transaction.atomic():
        refund_at = now or timezone.now()
        updated = Order.objects.filter(
            pk=order.pk, status=OrderStatus.WAITING_GRAB, deleted_at__isnull=True
        ).update(status=OrderStatus.REFUNDED, updated_at=refund_at)
        if not updated:
            return 0
        # ⭐ S-03 / D-0331：引擎退款标记（与上面的订单行更新同一事务、同一个时刻值；
        #    顺序承重——翻转拿不到 rowcount 就根本不写，⛔ 不会给没翻成的单留凭证）。
        JidanpuSyncEvent.objects.create(
            kind=KIND_SHADOW_REFUNDED, status="done",
            **_engine_refund_event_fields(mirror, order, refund_at=refund_at),
        )
        # ⚠️ 不回写内存 `order.status`：`queue_withdraw` 只读单号 / 主键与推送证据。
        jidanpu_member.queue_withdraw(order)
    return 1


@transaction.atomic
def refund_for_cards(mirrors, *, now) -> None:
    """下架一批卡时，在同一事务退款并登记下家撤回。"""
    from apps.business import jidanpu_member

    mirrors_by_id = {mirror.pk: mirror for mirror in mirrors}
    waiting = Order.objects.filter(
        upstream_hall_card_id__in=mirrors_by_id, status=OrderStatus.WAITING_GRAB,
    )
    orders = list(
        waiting.select_for_update(no_key=True).order_by("pk").only("pk", "order_no", "upstream_hall_card")
    )
    if not orders:
        return
    Order.objects.filter(pk__in=[order.pk for order in orders]).update(
        status=OrderStatus.REFUNDED, updated_at=now,
    )
    JidanpuSyncEvent.objects.bulk_create([
        JidanpuSyncEvent(
            kind=KIND_SHADOW_REFUNDED, status="done",
            **_engine_refund_event_fields(mirrors_by_id[order.upstream_hall_card_id], order, refund_at=now),
        )
        for order in orders
    ], batch_size=250)
    jidanpu_member.queue_withdrawals(orders)
