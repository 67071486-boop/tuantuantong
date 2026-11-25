"""上家侧的**触发点**（spec §7.1 / §16.2 / §16.3 / §16.6）。 ⚠️ 本模块**只入队、不投递**（spec §16.6）：写的每一行都是 `pending`，真投递在**后台 worker** （`apps/business/peer_worker.py`，后续任务落地）。⛔ 这里既不发 HTTP、也不排 `transaction.on_commit` 回调——P1 曾把投递排在提交后的回调上（`_deliver_later`），那条路径已随 §16.6 **整体删除**；「一个回调都不排」由 `tests/test_peer_order_push.py::AssignEndpointHookTests::test_assign_registers_no_delivery_callback` 钉住。 ⚠️ 写事件与业务动作**同一个事务**（spec §7.2）：留痕写不进去就一起回滚。
⚠️ 本模块是**租户 schema** 内的写路径（`PeerSyncEvent` / `PeerDownstreamBinding` 都落租户 schema，见 `models.py` 里 Peer 四表那段的注释）——租户上下文来自调用方所在请求， ⛔ 本模块不接受租户参数。 ⚠️ 三类事件（`order.assign` / `order.grab` / `order.withdraw`）共用 `enqueue_order_event`：去重、升级、事件级 `meta` 只有这一份实现（spec §16.2）。 ⭐ P3 起多**第四类** `order.update`（状态 / 发单价共用，上下行同种类）也走同一个内核；它与前三类的差别只有一处：命中 pending 时**刷新载荷与事件时刻**（「以最后更改的为主」， ⛔ 不新开一行）——那一段在 `enqueue_order_event` 里，是 P3 唯一的共享内核改动。
⭐ P4 Task 5 起多**第五 / 第六类** `order.screenshot.add` / `order.screenshot.delete`：它们**不走** `enqueue_order_event`（载荷不是订单白名单、也没有 pending 去重——每张图是一个不同的 key），而由 `enqueue_screenshot_event` 往**链上两个方向**各入队一行。 ⭐ **R-P4-28 起方向反过来一次**：接手类（`order.assign` / `order.grab`）在订单事件写出来之后 **调用** `enqueue_screenshot_event` 补发本单已有的截图（上家先传图、后派单 ⇒ 下家原本看不到）。 ⚠️ 状态变化的**唯一入口**是 `notify_order_status_changed`（漏斗）：调用方⛔ 不要各自拼 `enqueue_order_update` / `enqueue_upstream_status`，否则「哪些状态要同步」会有多份口径。 ⚠️ 撤单原因 → 逐字中文的 `WITHDRAW_REASON_TEXT` 定义在**纯函数模块** `peer_sync.py` （上家入队与下家落库都要读它，放这里会引出 `dispatch ↔ inbound` 的循环风险）⇒ 本模块把它 **re-export** 出来，供调用方与测试取用（⛔ 别当未用导入删掉）。"""
from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.business import peer_wakeup
from apps.business.media_access import is_shared_object_key
from apps.business.models import OrderStatus, PeerDownstreamBinding, PeerSyncEvent
from apps.business.peer_sync import (
    HALL_CLAIM_KIND,
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
    HALL_REPRICE_KIND,
    HALL_TAKE_KIND,
    HALL_THROW_KIND,
    WITHDRAW_REASON_TEXT,  # noqa: F401  —— 有意 re-export，见模块 docstring
    effective_peer_ref,
    is_negative_amount,
    order_payload,
)

logger = logging.getLogger(__name__)

#: M1：`hall.take` 作废同键未投 `hall.throw` 时写进台账 `reason` 的人话。
#: 单独常量让用例钉字面值（E-0024）。
VOIDED_THROW_REASON = "该单已离开大厅，这条未投递的上厅事件作废（以 hall.take 为准）"


def _origin_order_no(order) -> str:
    """这张单在**链上最初那一跳**的单号（没有上游链路行 ⇒ 空串）。 ⭐ **P4 多跳链身份**（spec `2026-10-25-peer-hall-share-design.md` §5.3.1 第 6 条）：出站订单事件（`order.assign` / `order.grab` / `order.update` / `order.withdraw`）在 **这张单挂着上游链路**时多带一个 `origin_order_no` 键，下一跳靠它认回自己大厅里那张卡。为什么必须是它、而不是 `order.order_no`：二次转发沿链**不换卡上的单号**
    （`mirror_hall_payload` 送的是镜像行的 `peer_order_no` = 源头单号），而真单的 `order_no` 是**每一跳自己生成**的 ⇒ A→B→C 时 C 手里那张真单的号是 **B 生成的**，而它大厅里那张卡记的是 **A 的号**，拿 `order_no` 从第三层起永远认不回来。取值口径（两列都是 `PeerOrderLink` 上的「链身份」）： - 首选 `link.origin_order_no`（P4 的「链上最初单号」列）； - 它为空时回落 `link.peer_order_no`（= **直接上家**的单号）。今天 `origin_order_no`
    恒非空（建行时 `payload.source_order_no or peer_order_no`）⇒ 这一格是**形状兜底**、不是常用分支（E-0021 同族：不拿真值判「有没有这一列」）。 ⚠️ **不过滤绑定生死**：链身份是**历史事实**（这张单最初从哪个号来的），解绑 / 停用不改变它——滤掉只会让「上家解绑后下家交单」突然丢掉认单键。 ⚠️ **已知可达范围 = 三级链**（与本列自己的模型注释同一条）：`origin_order_no` 在第 5 跳起会退成「祖辈」的单号（接收侧建行时写的是对端的 `source_order_no`，每传一跳只往前挪一级） ⇒ 本键只保证 A→B→C 这一段；更深的链不承诺。 ⚠️ 一次查库（`order` 上有索引的 FK 反查）。调用点在 `enqueue_order_event` **判过** 「这个接手人有没有生效下家绑定」之后 ⇒ 没有下游的事件一分钱都不花（同那里的既有纪律）。"""
    from apps.business.models import PeerOrderLink

    link = (
        PeerOrderLink.objects.filter(order=order, upstream_binding__isnull=False)
        .order_by("pk")
        .first()
    )
    if link is None:
        return ""
    return link.origin_order_no or link.peer_order_no


def enqueue_order_event(
    order, taker, *, kind, withdraw_reason="", occurred_at=None
) -> PeerSyncEvent | None:
    """把「这个账号身上发生了这一类事」记成一行待投递事件（三类事件共用的入队内核）。回 `None` 表示**这个 taker 没有生效绑定**（绝大多数单都是这样）⇒ 一行都不写。 ⚠️ **判据顺序是承重的**：先查绑定、没有就返回，`order` 与 `order_payload()` 都不会被碰 ——这正是 `tests/test_peer_order_push.py::test_no_binding_means_no_event` 敢传 `order=None` 的理由。⛔ 不要把绑定查询挪到 `order_payload(order)` 之后。 ⚠️ `state=ACTIVE` 这条过滤也是承重的：已解除的绑定不得再收推送（`test_a_revoked_binding_means_no_event` 以「有绑定、但不在生效态」把两种实现分开）。 ⭐ **`occurred_at`（事件级时刻）默认取 `order.updated_at`**（spec §16.3）。调用方显式传它有两类理由，**判据都是「不传就会被下游按 `stale` 静默丢掉」**： ① 业务动作**不推** `Order.updated_at` ⇒ 默认值会是「上一次订单写入」的时刻，与下游已应用过的事件相等或更旧。今天两处：删单（`recycle.soft_delete_order` 的 `update_fields` 不含 `updated_at`）；`/assign/` 换人档（订单已是 `IN_PROGRESS`、
    R-3 跳过 `apply_transition`，且没带 `price` 时一次 `order.save()` 都不发生）。 ② **同一次请求先入队了另一条更早的事件**（I-1：`/assign/` / `/claim/` 带价或带状态变化时，`order.update` 先入队）⇒ 两条事件共用同一个 `Order.updated_at`，而投递**最老优先** ⇒ 更全的 `order.assign` / `order.grab` 会被那条更窄的 `order.update` 顶掉。整批调用点见 `enqueue_order_assigned` / `enqueue_order_grabbed`，判据见 `tests/test_peer_p3_event_ordering.py`。 ⛔ 其余调用方**不要**传它（默认值就是它们要的口径）。 ⚠️ **去重按 `(binding, target_ref, kind)` 打在未投递行上**（spec §16.2）：同一张单在同一次请求里可能被多条路径同时想发**同类**事件（例：人工退款那一次既迁状态又清接手人） ⇒ 只留一行。⛔ `kind` 必须进这个键：指派与撤单是两件事，撤单不得复用它前面那条 `order.assign` 的空位（`test_a_withdraw_is_a_separate_row_from_a_pending_assign`）。 ⚠️ **升级**（spec §16.2）：`refunded` / `deleted` 可以改写一条尚未投递的 `taker_removed`
    的原因；**反向不升级**（后到的 `taker_removed` 不得把 `refunded` / `deleted` 降级）， ⛔ 且 `refunded` 与 `deleted` 之间**不互相改写**（spec 没规定谁覆盖谁 ⇒ 取先到的那个原因，见 `test_a_pending_refunded_is_not_rewritten_by_a_later_deleted`）。 ⭐ **这条升级分支是可达的（Task 3 实测取证，⛔ 不是防御性代码）**：同一个 *pending 窗口* （后台 worker 尚未投递）里可以**跨请求**先后写出两种原因——「先在池内清人（`taker_removed`）→ 再把**同一个绑定账号**派回来 → 随后删单 / 改已退单」这条链全走真端点即可命中。取证用例打真端点走完整条链（`tests/test_peer_withdraw_triggers.py` 的 `UpgradeBranchReachabilityTests`），⛔ 不是直接连调本函数两次。 ⚠️ **原先的 `withdraw_reason in ("refunded", "deleted")` 那半句判据已删**（Task 3）：在「原原因是 `taker_removed`」成立时它与剩下的条件**等价**（新原因是 `taker_removed` 只是把同一个值写回去），删掉不改变任何取值。 ⚠️ **事件级元数据**（`occurred_at` / `withdraw_reason` / `actor_name`）落 `meta`、 ⛔ **不进 `payload`**——`payload` 只放 spec §10.1 白名单里的订单字段。 ⭐ **接手类（`order.assign` / `order.grab`）在订单事件写出来之后补发本单已有的图片** （R-P4-28）：上家**先传图、后把单派给那个绑了下游的账号**时，认领那一刻这张单的接手人还不是它 ⇒ 下行判据不成立、下家一张图都收不到；补发按本单 `status=ready` 且 `img_type ∈ {0,1}` 的行逐条走 `enqueue_screenshot_event`（见 `_enqueue_existing_order_screenshots`；⭐ D-0316 起群码也补发）。 ⛔ **只有这两类 kind 补发**：`order.update` / `order.withdraw` 与截图无关。"""
    binding = PeerDownstreamBinding.objects.filter(
        account=taker, state=PeerDownstreamBinding.State.ACTIVE
    ).first()
    if binding is None:
        return None
    # ⭐（计划 `2026-10-25-peer-source-identity`）：身份键 = **来源单号** （唯一出处 `jidanpu_member.effective_source_order_no`，经 `peer_sync.effective_peer_ref`
    # 转发）——跨系统一律用来源单号做身份，内置号只在各系统内排重用。 ⚠️ 放在**绑定判据之后**：`order=None` 的调用（没有绑定 ⇒ 一行都不写）不许碰它
    # （`test_no_binding_means_no_event` 钉的就是这个顺序）。
    sync_ref = effective_peer_ref(order)
    pending = PeerSyncEvent.objects.filter(
        binding_role="downstream",
        binding_id=str(binding.pk),
        kind=kind,
        target_ref=sync_ref,
        status=PeerSyncEvent.Status.PENDING,
    ).first()
    if pending is not None:
        if kind == "order.update":
            # ⭐ P3：状态 / 发单价共用一行 pending。同一个 pending 窗口里可能先改价、再改状态
            # ⇒ 把载荷与事件时刻刷成**最新**那一份（业务方：「以最后更改的为主」）。 ⛔ 不新开一行（否则一次改单会推两条，且两条的顺序不由本机决定）。
            # ⭐ P4：链身份跟着一起刷新（多跳时下一跳认卡靠它）。
            pending.payload = order_payload(
                order, origin_order_no=_origin_order_no(order)
            )
            pending.meta = {
                **(pending.meta or {}),
                "occurred_at": (
                    occurred_at if occurred_at is not None else order.updated_at
                ).isoformat(),
            }
            pending.save(update_fields=["payload", "meta"])
            return pending
        # ⚠️ **升级条件只剩「这一行原本的原因是 `taker_removed`」**：条件成立时新原因只可能是三个枚举值之一——`refunded` / `deleted` 是升级，`taker_removed` 是把同一个值写回
        # 去（值不变的空写）。所以原先那句 `withdraw_reason in ("refunded", "deleted")` 与剩下的这个条件**等价**，不构成一条独立判据（AGENTS §5-E：别把防御性写法写成
        # 承重逻辑）。见本函数 docstring 的「升级」一节。
        if (
            kind == "order.withdraw"
            and (pending.meta or {}).get("withdraw_reason") == "taker_removed"
        ):
            pending.meta = {**(pending.meta or {}), "withdraw_reason": withdraw_reason}
            pending.save(update_fields=["meta"])
        return pending
    # 事件级时刻（spec §16.3）：默认取上家 `Order.updated_at`，⛔ 不是本函数的执行时刻；调用方显式传 `occurred_at` 时才用它（删单 / `/assign/` 换人档 / 同请求里另有更早事件的
    # `/assign/`、`/claim/` 两处，见函数 docstring 与两个壳的 docstring）。 ⚠️ 判据用 `is not None`（E-0021 同口径）：不拿真值判这个形参。
    meta = {
        "occurred_at": (
            occurred_at if occurred_at is not None else order.updated_at
        ).isoformat()
    }
    if withdraw_reason:
        meta["withdraw_reason"] = withdraw_reason
        # ⭐ 下家算不出显示名（它的绑定表里只有上家子账号的**登录名**）⇒ 上家带上（spec §16.2）。
        # 口径走本仓「怎么显示一个人」的**唯一出处**。⛔ 调用方不传这个参数（它已经查到了 binding）。
        meta["actor_name"] = user_display_name(binding.account)
    event = PeerSyncEvent.objects.create(
        binding_role="downstream",
        binding_id=str(binding.pk),
        direction="out",
        kind=kind,
        target_type="order",
        target_ref=sync_ref,
        # ⭐ P4 多跳链身份（spec §5.3.1 第 6 条）：这张单挂着上游链路时多带一个
        #    `origin_order_no`，下一跳靠它认回自己大厅里那张卡；单跳 / 原生单 ⇒ 键不出现
        #    （接收侧按 `order_no` 回落，行为逐字不变）。
        payload=order_payload(order, origin_order_no=_origin_order_no(order)),
        meta=meta,
        # 幂等键由**发送方**生成、随载荷发给对端。 ⛔ **本行原写「对端按它去重」（spec §6.4）——那是错的**（文档波改准）：
        # 接收侧**从不读** `idempotency_key`，真正的去重键是 `(upstream_binding, peer_order_no)`
        # （`PeerOrderLink` 的唯一约束，spec §6.3 / §12.3 第 15 条）。本列今天**只落库、只外发、没有消费方**，是 P2 重投机制的预留位（spec §6.4 已按同口径更正）。
        idempotency_key=uuid.uuid4().hex,
    )
    if kind in ("order.assign", "order.grab"):
        # ⭐ R-P4-28：接手类事件写出来之后，把本单已有的、已认领的订单截图补发一遍
        #    （详见 `_enqueue_existing_order_screenshots` 的 docstring）。
        _enqueue_existing_order_screenshots(order)
    return event


def _is_shared_order_screenshot_key(value) -> bool:
    """`object_key` 是否**在共享前缀下**（补发只发这一类，见 `_enqueue_existing_order_screenshots`）。
    ⭐ D-0316：形状判据已统一到 `apps.business.media_access.is_shared_object_key`（**全仓一份**），
    本函数只做**委托**——发送侧 / 接收侧 / 群码字节保留三处共用同一套归一化（`/` 切段、剔除空段与 `.`、任何 `..` 一律拒、首段 == `SHARED_ORDER_PREFIX`）。"""
    return is_shared_object_key(value)


def _enqueue_existing_order_screenshots(order) -> None:
    """把本单**已经认领**的图片逐条补发（R-P4-28；只由接手类事件调用）。 ⚠️ 为什么必须有它：`enqueue_screenshot_event` 只在**认领**与**删除**两处被调 ⇒ 上家先传图、后把单派给那个绑了下游的账号时，认领那一刻这张单的接手人还不是它 ⇒ 下行判据不成立、下家接手后看到的是**空列表**。这与「一个订单不管被链上几个租户接过，截图都是同一套」相反，也会让真机验收的截图那一步按操作次序假绿 / 假红。 ⭐ **D-0316：群聊二维码也补发**（`img_type__in=(0, 1)`）——群码随订单同步，
    "先传群码后派单"与"先传订单截图后派单"是同一个坑（接手人看不到图）。代价 = 每单最多多 1 条事件。`pending` 仍是「签了 ticket 还没认领」⇒ 不发，认领成功那条路径自己会广播一次。 ⛔ **还必须筛「键在共享前缀下」**（`_is_shared_order_screenshot_key`）：**P4 之前上传的老截图 / D-0316 之前上传的老群码键在 `<租户>/orders/…` 下**（残差 ⑪：字节不搬、对象名不改），而接收侧的形状闸（R-P4-16）只认共享前缀 ⇒ 把这些行补发出去**只会**在对端被
    `bad_shape` 拒收、事件永远重试、`consecutive_failures` 一路涨到自动停绑（真机验收实测）。替代做法（给老图重派 key / 复制字节过去）**本计划明令不做**（残差 ⑩⑪）⇒ 唯一正确的处置是**跳过不发**（历史群码不迁移：业务方 已确认，要重传一次才会同步）。 ⚠️ **顺序取 `pk`（最早在前）**，与两条读路径的 `order_by("pk")` 同口径 ⇒ 事件顺序确定。 ⚠️ **发送侧不去重**（`enqueue_screenshot_event` 本来就不做 pending 去重）：接手前那次认领若已经发过上行，这里会再发一条；接收侧按 `object_key` 幂等，重复投递天然无害。"""
    from apps.business.models import OrderScreenshot

    rows = OrderScreenshot.objects.filter(
        order=order,
        img_type__in=(OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_GROUP_QR),
        status=OrderScreenshot.STATUS_READY,
    ).order_by("pk")
    for row in rows:
        if not _is_shared_order_screenshot_key(row.object_key):
            continue
        enqueue_screenshot_event(
            order,
            kind="order.screenshot.add",
            object_key=row.object_key,
            img_type=row.img_type,
            remark=row.remark,
        )


def enqueue_screenshot_event(
    order, *, kind, object_key, img_type=0, remark=""
) -> list[PeerSyncEvent]:
    """截图的增 / 删在**链上两个方向**广播（P4 Task 5；业务方：「截图都是同一套」）。 ⚠️ 与状态那条（`enqueue_upstream_status`）**刻意不同**：状态只往一个方向发（上下一级），截图**两个方向都发**——上游也要看到下家传的图。两个方向各自独立：链头（没有上游绑定）只发下行、链尾（接手人没有生效下游绑定）只发上行；两侧都没有 ⇒ 回空列表。
    ⚠️ **载荷不是订单白名单**：`object_key` / `img_type` / `remark` 走 `payload`， ⛔ 别把它们塞进 `PEER_ORDER_FIELDS`（那是订单字段的契约）。 ⚠️ **本函数只入队**（与 P2 起的纪律一致）：真投递交给常驻的 `peer_sync` / worker。 ⚠️ `meta.occurred_at` 仍按惯例带上（事件级字段的形状），但**接收侧不用它做新旧裁决**
    ——截图与状态是两条独立时间线（裁决 R-P4-15.3），拿它推进状态时钟会把一条更旧但合法的状态事件静默判成 `stale`。 ⚠️ **不做 pending 去重**（与三类订单事件不同）：每一张图是一个**不同的 key**，各自都要送达；同一 key 的重复投递由**接收侧**按 `object_key` 幂等（不建第二行）。回**本次创建的事件列表**（`PeerSyncEvent`），供调用方与测试断言方向。"""
    from apps.business.models import EmpType, PeerOrderLink, Performance

    payload = {"object_key": object_key, "img_type": img_type, "remark": remark}
    meta = {"occurred_at": order.updated_at.isoformat()}
    made: list[PeerSyncEvent] = []

    # 下行：这张单的**当前接手人**有生效下游绑定（判据与状态漏斗/`enqueue_order_event` 同一套）。
    perf = (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .select_related("user")
        .first()
    )
    if perf is not None:
        binding = PeerDownstreamBinding.objects.filter(
            account=perf.user, state=PeerDownstreamBinding.State.ACTIVE
        ).first()
        if binding is not None:
            made.append(PeerSyncEvent.objects.create(
                binding_role="downstream", binding_id=str(binding.pk), direction="out",
                kind=kind, target_type="order",
                # ⭐：下行身份键 = **来源单号**（`effective_peer_ref`）；
                #    `object_key` 逐字不变（载荷见上）——图还是同一份共享字节。
                target_ref=effective_peer_ref(order),
                payload=payload, meta=meta, idempotency_key=uuid.uuid4().hex,
            ))

    # 上行：这张单挂着**未软删且启用**的上游绑定（判据与 `enqueue_upstream_status` 同一套）。 ⚠️ `target_ref` 用 `link.peer_order_no`（**上家的单号**）：上家按它直接查自己那张单。
    # ⭐ 起这一列的语义 = **对端的来源单号**（收向建行时写的是 `target_ref`，见 `peer_inbound.apply_events`）⇒ 上行身份与下行同一口径，⛔ 本函数不改取值。
    link = (
        PeerOrderLink.objects.filter(
            order=order, upstream_binding__isnull=False,
            upstream_binding__deleted_at__isnull=True, upstream_binding__enabled=True,
        )
        .select_related("upstream_binding")
        .first()
    )
    if link is not None:
        made.append(PeerSyncEvent.objects.create(
            binding_role="upstream", binding_id=str(link.upstream_binding_id), direction="out",
            kind=kind, target_type="order", target_ref=link.peer_order_no,
            payload=payload, meta=meta, idempotency_key=uuid.uuid4().hex,
        ))
    return made


def enqueue_order_assigned(order, taker, *, occurred_at=None) -> PeerSyncEvent | None:
    """「这一单**指派**给了这个账号」（spec §7.1 第 1 行；P1 壳，保持既有调用点）。 ⚠️ `occurred_at` 默认 `None`（= 取 `order.updated_at`，既有口径不变）；`/assign/` 的调用点
    一律显式传一个时刻进来（⛔ 不分成"带价 / 不带价"两档——带价时同一次请求里**先**入队了一条 `order.update`，两条事件若共用 `order.updated_at`，下家投递时会把这条更全的
    `order.assign` 判 `stale` 跳过、整张白名单静默丢掉）。理由与判据见 `enqueue_order_event` 的 docstring 与 `tests/test_peer_p3_event_ordering.py`。"""
    return enqueue_order_event(
        order, taker, kind="order.assign", occurred_at=occurred_at
    )


def enqueue_order_grabbed(order, taker, *, occurred_at=None) -> PeerSyncEvent | None:
    """「这一单在抢单大厅被这个账号**抢到**」（spec §7.1 第 2 行）。 ⚠️ `occurred_at` 的语义与理由同 `enqueue_order_assigned`（抢单那一档同样可能先写出
    一条 `order.update`：状态漏斗里那条状态变化事件在 `enqueue_order_grabbed` **之前**入队）。"""
    return enqueue_order_event(
        order, taker, kind="order.grab", occurred_at=occurred_at
    )


def enqueue_order_withdrawn(
    order, previous_taker, reason, *, occurred_at=None
) -> PeerSyncEvent | None:
    """「这一单**不再属于**这个账号」——清人 / 换人 / 改退单 / 删单（spec §16.2）。 ⚠️ `previous_taker` 是**改之前**的接手人（⛔ 不是新接手人）：它自己没有生效绑定就一行都不写。
    `reason` 取 `WITHDRAW_REASON_TEXT` 的三个键（`refunded` / `taker_removed` / `deleted`），中文文案由下家侧翻，⛔ 不在这里拼文案。
    ⚠️ `occurred_at` 只有**两处**调用方会传（业务动作不推 `Order.updated_at` 的只有它们：删单、`/assign/` 的换人档）；其余保持默认（= `order.updated_at`），理由见 `enqueue_order_event` 的 docstring。"""
    return enqueue_order_event(
        order,
        previous_taker,
        kind="order.withdraw",
        withdraw_reason=reason,
        occurred_at=occurred_at,
    )


def enqueue_order_update(order, taker, *, occurred_at=None) -> PeerSyncEvent | None:
    """上家改了这张单的**状态 / 发单价** ⇒ 一行下行 `order.update`（P3；spec §7.1 第 3 行下行那一半）。"""
    return enqueue_order_event(order, taker, kind="order.update", occurred_at=occurred_at)


def enqueue_upstream_status(order, *, occurred_at=None) -> PeerSyncEvent | None:
    """下家把**本地状态**回传上家（P3；spec §7.1 第 3 行上行那一半）。 ⚠️ 载荷只有 `{"status": <映射后的目标态>}`——业务方：「下家只回传状态」。
    ⚠️ `target_ref` 用 `link.peer_order_no`（**上家的单号**），⛔ 不是本地下家单号：上家收到的 `target_ref` 要能直接查到它自己那张单。
    ⚠️ 只认**未软删且启用**的上游绑定（`deleted_at__isnull=True, enabled=True`）。 ⚠️ 同一个 `(上游绑定, 上家单号)` 的 pending 行已存在时**刷新**它（与下行同口径）：「以最后更改的为主」，⛔ 不新开一行。"""
    from apps.business.models import PeerOrderLink
    from apps.business.peer_direct_status import outgoing_status_identity
    from apps.business.peer_sync import UPSTREAM_STATUS_MAP, UPSTREAM_TRIGGER_STATUSES

    if str(order.status) not in UPSTREAM_TRIGGER_STATUSES:
        return None
    link = (
        PeerOrderLink.objects.filter(
            order=order, upstream_binding__isnull=False,
            upstream_binding__deleted_at__isnull=True, upstream_binding__enabled=True,
        )
        .select_related("upstream_binding")
        .first()
    )
    if link is None:
        return None
    binding = link.upstream_binding
    payload = {"status": UPSTREAM_STATUS_MAP[str(order.status)], **outgoing_status_identity(link)}
    pending = PeerSyncEvent.objects.filter(
        binding_role="upstream", binding_id=str(binding.pk), kind="order.update",
        target_ref=link.peer_order_no, status=PeerSyncEvent.Status.PENDING,
    ).first()
    moment = (occurred_at if occurred_at is not None else order.updated_at).isoformat()
    if pending is not None:
        pending.payload = payload
        pending.meta = {**(pending.meta or {}), "occurred_at": moment}
        pending.save(update_fields=["payload", "meta"])
        return pending
    return PeerSyncEvent.objects.create(
        binding_role="upstream", binding_id=str(binding.pk), direction="out",
        kind="order.update", target_type="order", target_ref=link.peer_order_no,
        payload=payload, meta={"occurred_at": moment},
        idempotency_key=uuid.uuid4().hex,
    )


def notify_order_status_changed(order, *, occurred_at=None) -> None:
    """**状态变化的唯一入队漏斗**（P3）：按当前状态决定下行 / 上行 / 都不发。下行：这张单的当前接手人若有一条生效的下游绑定 ⇒ `enqueue_order_update`。上行：这张单若挂在一条生效的上游绑定下 ⇒ `enqueue_upstream_status`。
    同一张单可能**两者都有**（A→B→C 场景的中间租户）⇒ 两个都发（本期不做转发，但这里天然成立）。 ⛔ 调用方只传 `order`；时刻默认取 `order.updated_at`（与 P2 同口径）。 ⚠️ **两个方向各自按状态集判一次**（`DOWNSTREAM_ACCEPTED_STATUSES` /
    `UPSTREAM_TRIGGER_STATUSES`）：漏斗自己的 docstring 承诺「按状态决定发哪个方向」，那个决定就落在**这里**，⛔ 不外包给被调函数（否则「一个方向都不发」在漏斗这一层看不见、也无从断言）。⚠️ `enqueue_upstream_status` 自己的状态守卫**照样保留**：它同时是一个独立入口，直接调用方（不经漏斗）仍受它保护。"""
    from apps.business.models import EmpType, Performance
    from apps.business.peer_sync import (
        DOWNSTREAM_ACCEPTED_STATUSES,
        UPSTREAM_TRIGGER_STATUSES,
    )

    if str(order.status) in DOWNSTREAM_ACCEPTED_STATUSES:
        perf = (
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
            .select_related("user")
            .first()
        )
        if perf is not None:
            enqueue_order_update(order, perf.user, occurred_at=occurred_at)
    if str(order.status) in UPSTREAM_TRIGGER_STATUSES:
        enqueue_upstream_status(order, occurred_at=occurred_at)


def notify_order_price_changed(order) -> None:
    """上家改了这张单的**发单价** ⇒ 记一行下行 `order.update`（P3；业务方裁定：下行同步状态 + 发单价）。 ⚠️ 与状态那条**分开**成两个入口（虽然共用同一个 kind）：价格变化时状态可能**不在** 下行接受集里（例如仍在 `WAITING_GRAB`），那条漏斗不会发，价格不能跟着一起被吞掉。
    ⚠️ 接手人没有生效绑定 ⇒ `enqueue_order_update` 自己返回 `None`，本函数是 no-op。 ⚠️ 只认**当前** MAIN 行的归属人：调用方必须在「人还在这一行上」的那一刻调它。 `/throw-to-hall/` 带价那一支是一条**已知例外**——回池钩子先清人，这里因此没有可投递
    的人（下游收到的是那条 `order.withdraw`），行为与判据见 `tests/test_peer_p3_triggers.py` 的两条 `/throw-to-hall/` 用例。 ⭐ **D-0303 追加**：这张单还在 `WAITING_GRAB`（= 大厅在挂）⇒ 同一次改价还要给下家排一行 `hall.reprice`（spec §2.1：在挂期间发单价变化 ⇒ 下家刷新镜像价格）。判据用 `str(...)` 比较（与状态机同口径）；不在厅（已接走 / 池内未抛）⇒ 不发——那张单要么没有镜像行、要么镜像行已摘牌，发只会造出对端处理不了的噪声。"""
    from apps.business.models import EmpType, Performance

    perf = (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .select_related("user").first()
    )
    if perf is not None:
        enqueue_order_update(order, perf.user)
    if str(order.status) == str(OrderStatus.WAITING_GRAB):
        enqueue_hall_event(order, kind=HALL_REPRICE_KIND)


#: D-0377 内容同步触发面 = 「已对外白名单内的内容字段」在 `update_order_fields.changed`
#: 清单里的列名（FK 对外形状 game_name / server_id / project_id；`PEER_ORDER_FIELDS`
#: 协议白名单 ⛔ 不扩，demand / 时间要求 / 账号类字段无对外出口、不在内）。
CONTENT_SYNC_FIELDS = frozenset({
    "project_text", "content", "remark", "customer_phone", "customer_qq",
    "game", "server", "category", "project",
})


def notify_order_content_changed(order, changed_fields) -> None:
    """上家（本租户）编辑改了**内容面字段** ⇒ 下行同步（D-0377）。照 `notify_order_price_changed` 的两支：MAIN 绩效行的人有生效下家绑定 ⇒ 排 `order.update`（载荷是全量白名单，pending 同 kind 命中自动刷新）；在挂（`WAITING_GRAB`）⇒ 重发 `hall.throw` 刷新下家的镜像卡——⛔ 只对在挂单重发（下家已接走后被 throw 复活会假拉回大厅）。`changed_fields` 是调用方 `update_order_fields` 里那份 `changed` 清单；不含触发面字段 ⇒ 直接返回（零开销）。调用点唯一（`order_writes.update_order_fields`，与 `queue_project_update` 同位）。"""
    changed = {entry[0] for entry in changed_fields}
    if not changed.intersection(CONTENT_SYNC_FIELDS):
        return
    from apps.business.models import EmpType, Performance

    perf = (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .select_related("user").first()
    )
    if perf is not None:
        enqueue_order_update(order, perf.user)
    if str(order.status) == str(OrderStatus.WAITING_GRAB):
        enqueue_hall_event(order, kind=HALL_THROW_KIND)


# ── 团团通之间的大厅镜像（D-0303）────────────────────────────────────────────
def _hall_targets(bindings=None):
    """要推给哪些下游绑定：`state=ACTIVE` 且 `hall_push_enabled=True`（**两道**闸）。
    ⚠️ `bindings=None`（默认）= 现查全部；传入时**照样**按这两条过滤（铺底命令传单个绑定
    进来，也不能绕过开关，spec §5）。两条闸的过滤只此一处（native 与镜像行两条入队共用）。"""
    if bindings is None:
        bindings = PeerDownstreamBinding.objects.all()
    return [
        binding
        for binding in bindings
        if binding.state == PeerDownstreamBinding.State.ACTIVE
        and binding.hall_push_enabled
    ]


def _void_pending_hall_throw(binding, *, target_ref: str) -> int:
    """`hall.take` 作废同键的 pending `hall.throw`（⭐ **两条入队路共用**）。投递**最老优先**、去重不刷 `created_at` ⇒ 「throw（未投）→ take → 再抛（去重回填到
    老 throw 行）」的 pending 队列会变成 `[throw(旧), take]`，先投的 throw 把 take 顶成终态 `gone`——而上家实挂。收口：排 take 时把同一 `(绑定, 单号)` 还没投的 throw
    **当场作废**（`done` + 人话原因；对端永远不需要它——take 才是终态事实）。作废走台账形状（⛔ 不删行），与 worker 的撤单复核作废同一族。"""
    return PeerSyncEvent.objects.filter(
        binding_role="downstream",
        binding_id=str(binding.pk),
        kind=HALL_THROW_KIND,
        target_ref=target_ref,
        status=PeerSyncEvent.Status.PENDING,
    ).update(
        status=PeerSyncEvent.Status.DONE,
        reason=VOIDED_THROW_REASON[:255],
        processed_at=timezone.now(),
        next_attempt_at=None,
    )


@transaction.atomic
def _enqueue_hall_row(
    binding, *, kind, target_ref: str, payload: dict, occurred_at,
    meta_extra: dict | None = None,
) -> PeerSyncEvent | None:
    """**一条**大厅事件行（native 单与镜像行两条入队路共用的内核，⛔ 不各写一份）。 ⚠️ **未决去重键照既有形状** `(binding_role, binding_id, kind, target_ref)`、只认 `pending` 行（spec §2.2）；命中时**刷新载荷与事件时刻**（「以最后更改的为主」，
    与 `order.update` 那一支同口径）——同一次请求里可能先抛单又撤单，两条事件各自成行，但同类事件在投出去之前只留最新一份。 ⭐ **D-0306 推送路不变量：负价卡不抛 / 负价 reprice 不传播**。判据落在**入队前**（对端看到的 `hall_price` 就是这一份载荷，落地侧再判一次是兜底）：
    - `hall.throw` / `hall.reprice` 的 `payload.hall_price` 是负数 ⇒ **一行都不建**、回 `None`（调用方跳过）。理由：大厅镜像行永不负价（D-0306：大厅负价不同步），负价卡进下家大厅会造出负价的打手接单价 / 负的结算基数； - ⛔ `hall.take` **不判价**：它是离厅事实，与价无关（照旧入队，否则负价卡撤不掉）。"""
    if kind in (HALL_THROW_KIND, HALL_REPRICE_KIND):
        payload_price = payload.get("hall_price") if isinstance(payload, dict) else None
        if is_negative_amount(payload_price):
            logger.warning(
                "大厅事件未入队（负价不往外传播）：kind=%s ref=%s 绑定=%s",
                kind, target_ref, binding.pk,
            )
            return None
    if kind == HALL_TAKE_KIND:
        voided = _void_pending_hall_throw(binding, target_ref=target_ref)
        if voided:
            logger.info(
                "大厅镜像： hall.throw 因离厅作废 binding=%s ref=%s count=%s",
                binding.pk, target_ref, voided,
            )
    candidates = PeerSyncEvent.objects.filter(
        binding_role="downstream",
        binding_id=str(binding.pk),
        kind=kind,
        target_ref=target_ref,
        status=PeerSyncEvent.Status.PENDING,
    )
    if kind == HALL_TAKE_KIND:
        candidates = candidates.select_for_update()
    pending = candidates.first()
    moment = (occurred_at or timezone.now()).isoformat()
    if pending is not None:
        pending.payload = payload
        # ⭐ 合并保留旧键、occurred_at 照旧刷新、新键覆盖写入——
        #    否则 pending 窗口里先到的裸 take 会吞掉后来带因 take 的 leave_reason（或反向）。
        pending.meta = {
            **(pending.meta or {}),
            "occurred_at": moment,
            **(meta_extra or {}),
        }
        fields = ["payload", "meta"]
        if kind == HALL_TAKE_KIND:
            # A worker may have sent the old payload under its old idempotency key.
            pending.idempotency_key = uuid.uuid4().hex
            fields.append("idempotency_key")
        pending.save(update_fields=fields)
    else:
        pending = PeerSyncEvent.objects.create(
            binding_role="downstream",
            binding_id=str(binding.pk),
            direction="out",
            kind=kind,
            target_type="order",
            target_ref=target_ref,
            payload=payload,
            meta={"occurred_at": moment, **(meta_extra or {})},
            idempotency_key=uuid.uuid4().hex,
        )

    if kind == HALL_TAKE_KIND:
        peer_wakeup.notify_hall_take()
    return pending


def mirror_hall_payload(mirror, *, hop: int | None = None) -> dict:
    """镜像行 → 继续往**下一跳**广播的载荷（P4 二次转发；形状与 native 那条同源）。形状 = `peer_sync.order_payload` 白名单的同形（下一跳走**同一个** `hall.*` 接收支， `_hall_mirror_upsert_throw` 只认 `game_name` / `hall_price`，其余原样存进 `PeerHallMirror.payload`）。两处**必须**改： - `order_no` = 这张卡的**源头单号**（`mirror.peer_order_no`）：二次转发沿链**不换卡上的单号**，下一跳的 `hall.claim` 才能用同一个 ref 逐跳回到源头（源头那张单的单号就是它）； - `hall_price` = **本层展示价**（`peer_hall.display_price` = 上家发单价 × 本绑定百分比）：
    逐跳相乘的落点——⛔ 不是上家原价。送原价会让最下家少乘一层（A 100 → B 90% → C 80% 时 C 看到 80 而不是 72，spec §6 / 业务方裁定 ⑦）。 ⚠️ `None`（上家没给价）**原样传** `None`，⛔ 不补 0.00（E-0021）。 - `game_name` 用本行 `game` 的名字兜底：`resolve_game` 建出来的那款才是下一跳能对上的。 - ⭐ **P5 `hop` = 本行 `hop` + 1**（纯控制字段，不是 PII）：源头（本租户原生单）那张卡是 0、它发出去的这一份是 1，下一跳落卡是 1、再发出去是 2…… 接收侧对 `hop > PEER_HALL_HOP_LIMIT`（5）的 `hall.throw` **不建卡、不转发**，只记一条
    `peer.hall_hop_limit` 台账 ⇒ 互绑成环时大厅不会被无限镜像撑爆（share-design §3）。 ⚠️ `hop` **只由本函数加**（`peer_sync.order_payload` 的白名单不含它）：单跳 / 老版本对端的载荷没有这个键，接收侧按 0 处理 ⇒ 行为逐字不变。 - ⭐ **`hop` 可显式覆盖**：`hall.reprice` 的转发不能用本行 `hop`（那是**卡**在链上的深度），要用**这次改价事件自己**的跳数 +1 （见 `peer_inbound._apply_hall_event` 的 reprice 支）。两件事分开的理由：互绑成环时各行的 `hop` 只被 `hall.throw` 写过，而改价事件在环上可能从很小的跳数重新开始 ⇒ 拿行上那个数转发，事件跳数就**不再单调**、环不会被上限截断（B1 的现场）。**卡上的 `hop` 不为改价事件改写**（深度是卡的事实）。 ⚠️ 其余键（`remark` / `project_text` / `content` / `customer_phone` …）**原样继承** 上家给的那一份：它们就是这张卡在大厅上展示的内容，⛔ 不在这里增删（白名单的权威在 `peer_sync.PEER_ORDER_FIELDS`）。"""
    from apps.business import peer_hall

    payload = dict(mirror.payload) if isinstance(mirror.payload, dict) else {}
    payload["order_no"] = mirror.peer_order_no
    price = peer_hall.display_price(mirror)
    payload["hall_price"] = None if price is None else f"{price:.2f}"
    payload["hop"] = (
        hop if hop is not None else (getattr(mirror, "hop", 0) or 0) + 1
    )
    # ⭐ P5：跳数逐跳 +1（`or 0`：老行 / 手工建的行 `hop` 缺省 = 0；列是非空整数，
    # 这里只是不让 `None` 混进载荷——`None + 1` 会在投递前就炸掉一条大厅事件）。
    # ⭐ B1：`hop` 显式给了就用它（`hall.reprice` 转发传「这次改价事件的跳数 + 1」，与卡上的深度分开，理由见 docstring）。
    game = getattr(mirror, "game", None)
    if game is not None:
        payload["game_name"] = game.name
    return payload


def enqueue_hall_event(order, *, kind, bindings=None, meta_extra=None) -> list[PeerSyncEvent]:
    """大厅镜像事件（`hall.throw` / `hall.take` / `hall.reprice`）的 per-binding 入队。 ⛔ **不能复用 `enqueue_order_event`**（spec §2.2）：那是「taker 是绑定账号才推」—— 大厅镜像要推给**全部**启用了大厅推送的下游绑定，跟这张单现在归谁无关。触发点接线（穷举表见 `docs/specs/2026-10-25-peer-hall-mirror.md` §2.2 与本函数调用点）： - `hall.throw`：进 `WAITING_GRAB`（唯一收口 `order_status.apply_transition` 里
    `hall_publisher is not None` 那一支，覆盖 `/throw-to-hall/`、通用 `/transition/` 目标 `WAITING_GRAB`、录单即抛单三条路）； - `hall.take`：离 `WAITING_GRAB`（同函数里 `from == WAITING_GRAB` 且目标不是它的那支，覆盖被接走 / 撤回 / 指派；**软删不经状态机** ⇒ `recycle.soft_delete_order` 另接一处）； - `hall.reprice`：在挂期间改发单价（`notify_order_price_changed` 尾上那一支）。
    ⚠️ 载荷 = `peer_sync.order_payload(order)` 白名单原样（含 `game_name` / `hall_price`， spec §2.1）；`target_type="order"`、`target_ref=<来源单号>` （`effective_peer_ref`，起；载荷里的 `order_no` 仍是**我方内置号**）。 ⚠️ 去重 / take 作废 pending throw / 目标绑定两道闸：见 `_enqueue_hall_row` / `_void_pending_hall_throw` / `_hall_targets`（⭐ P4 起与镜像行那条 `enqueue_hall_mirror_event` **共用同一份**内核，⛔ 别读成两套语义）。回本次**落库的事件列表**（每绑定至多一行）。"""
    payload = order_payload(order)
    made: list[PeerSyncEvent] = []
    # ⭐：大厅广播的身份键同样是**来源单号**（`effective_peer_ref`）。
    #    下游那张镜像行的 `peer_order_no` 就照它落库 ⇒ 抢单 / 摘牌 / 改价沿链同一个号。
    sync_ref = effective_peer_ref(order)
    for binding in _hall_targets(bindings):
        event = _enqueue_hall_row(
            binding,
            kind=kind,
            target_ref=sync_ref,
            payload=payload,
            occurred_at=order.updated_at,
            meta_extra=meta_extra,
        )
        if event is not None:
            made.append(event)
    return made


def enqueue_hall_mirror_event(
    mirror, *, kind, bindings=None, occurred_at=None, hop: int | None = None,
    meta_extra=None,
) -> list[PeerSyncEvent]:
    """**镜像行**继续往本租户的下家广播一条大厅事件（P4 二次转发，spec §4.1 第 2 条）。 A→B→C 时，B 大厅里那张卡是**从 A 镜像来的**、不是 B 的原生单 ⇒ `enqueue_hall_event` （它要一个 `Order`）根本用不上。本函数是它在镜像行上的**同形**入口：同一个内核（`_enqueue_hall_row`：同一套未决去重键、同一套 take 作废 throw）、同一道目标闸（`_hall_targets`：绑定生效 + `hall_push_enabled`），只有两处不同：
    - `target_ref` = **卡的源头单号**（`mirror.peer_order_no`）——沿链不换号； - 载荷 = `mirror_hall_payload`（展示价 + 源头单号 + 跳数，见那里的口径）。 ⚠️ **只由「这张镜像行因为收到上游事件而变化」的地方调**（`peer_inbound._apply_hall_event` 的 throw / take / reprice 三支 + 交单 / 收口那几处 + `peer_hall.unlist_binding_mirrors`
    的关开关 / 解绑下架）：`claiming` / `promoted` / `gone` 的行不再往外推（它们不在本租户的大厅里，推出去等于让下家看到我这边看不见的卡）。 ⚠️ `occurred_at` 默认 `None` ⇒ 取当前时刻；重投同一条上游事件时调用方**传上游那一刻** （`now`）而不是各自取时钟，事件时刻才能沿链一致（与本线 `hall.*` 的 `now` 口径同源）。 ⭐ **`hop`**：透传给 `mirror_hall_payload`，只给 `hall.reprice` 的转发用（事件跳数 + 1；⛔ 不传就仍按卡上的 `hop + 1`，`hall.throw` / `hall.take` 行为逐字不变）。"""
    payload = mirror_hall_payload(mirror, hop=hop)
    made: list[PeerSyncEvent] = []
    for binding in _hall_targets(bindings):
        event = _enqueue_hall_row(
            binding,
            kind=kind,
            target_ref=mirror.peer_order_no,
            payload=payload,
            occurred_at=occurred_at,
            meta_extra=meta_extra,
        )
        if event is not None:
            made.append(event)
    return made


def hall_claim_payload(*, taker_id: int) -> dict:
    """`hall.claim` 的**载荷**（⭐ D-0347 抽出：outbox 入队与**同步抢单直推**共用同一份）。
    载荷只有 `claim_by`（抢单人的**显示名**）：**v1 仅透传、上家不消费** （D-0303 改准；身份对上家是纯展示信息，⛔ 不把本地用户主键外发）。
    ⛔ 别在第二处再写一遍「查人 + 取显示名」——两条路载荷漂了，上家看到的就是两套口径。"""
    from django.contrib.auth import get_user_model

    from apps.accounts.display import user_display_name

    return {"claim_by": user_display_name(get_user_model().objects.get(pk=taker_id))}


def enqueue_hall_claim(mirror, *, taker_id: int | None = None,
                       claim_by: str | None = None) -> PeerSyncEvent:
    """抢到（或**代下一跳转发**）一条镜像行 ⇒ 排一条上行 `hall.claim` 给**这一家**上家。 ⭐ **D-0347起调用面收窄**：人类单跳的两处接单端点改为**同步直连** （`peer_hall.claim_peer_mirror_sync` 请求内直推、不进本队列）⇒ 本口剩下的调用方只有**两个**：① 机器路径 peer 档（`jidanpu_member.machine_claim_order`，后台接手无人等待）；② 多跳中间层转发（`peer_inbound._forward_mirror_claim`，判决在更上游）。 ⚠️ `binding_role="upstream"`（照 `enqueue_upstream_status` 的上行先例）；
    `target_ref` = **上家单号**（镜像行的 `peer_order_no`）——上家收到的 `target_ref` 要能直接查到它自己那张单（与 `enqueue_upstream_status` 同一条口径）。 ⚠️ 载荷只有 `claim_by`（抢单人的**显示名**）：**v1 仅透传、上家不消费** （D-0303 改准；身份对上家是纯展示信息，⛔ 不把本地用户主键外发）。 ⚠️ **不做 pending 去重**（与三类订单事件不同）：同一条镜像行可以先后排**多条** claim ——接单失败当场放回（D-0335）与超时回收（P3；D-0335 起 60 秒）都会把行送回
    `listed`，再接就再排（⭐ 回收落地后已可重排；上游族同款口径见 `jidanpu_upstream.queue_hall_claim` 的 docstring）。 ## ⭐ P4：两个入口、**同一个事件形状**（⛔ 不新造第二套投递） | 谁在要 | 入参 | `claim_by` | |---|---|---| | 本租户打手在大厅里点接单（下家侧） | `taker_id` | 那个打手的显示名 | | **下一跳**（本租户是中间层，`_apply_upstream_hall_claim` 转发） | `claim_by` | 下一跳绑定账号的显示名 | 多跳那一档本租户**没有**「点单的人」（点单的人在下游租户里）⇒ 调用方直接传 `claim_by`（下一跳绑定账号的显示名），`taker_id` 留空。两条路的**事件行本身逐字同形** （同一 outbox、同一 `target_ref`、同一投递链），只是显示名来源不同。"""
    from apps.business.models import PeerUpstreamBinding

    if claim_by is not None:
        payload = {"claim_by": claim_by}
    elif taker_id is not None:
        # ⭐ D-0347：显示名与载荷键的唯一出处（同步抢单直推共用这一份，⛔ 不各写一份）。
        payload = hall_claim_payload(taker_id=taker_id)
    else:
        raise ValueError("hall.claim 必须要一个「谁在接」的来源（taker_id 或 claim_by）")
    binding = PeerUpstreamBinding.objects.get(pk=mirror.peer_upstream_binding_id)
    return PeerSyncEvent.objects.create(
        binding_role="upstream",
        binding_id=str(binding.pk),
        direction="out",
        kind=HALL_CLAIM_KIND,
        target_type="order",
        target_ref=mirror.peer_order_no,
        payload=payload,
        meta={"occurred_at": timezone.now().isoformat()},
        idempotency_key=uuid.uuid4().hex,
    )


def enqueue_hall_claim_reply(binding, *, kind, target_ref, reason="") -> PeerSyncEvent:
    """上家对 `hall.claim` 的**应答**（`hall.claim_ok` / `hall.claim_refused`）发给那个下家。 ⚠️ `kind` 必须是两个应答枚举之一（传错 = 协议外的 kind 会被对端硬跳，本函数先响亮）。 ⚠️ `target_ref` = **上家单号**（原样回传 claim 事件带来的那个 ref）——下家按它找自己那条 `claiming` 镜像行收口。
    ⭐ **`reason`（业务方）**：`hall.claim_refused` 的**拒绝原因**，是载荷里 **唯一**的键。接收侧按它区分「永久（订单已不存在 / 没共享给这家）⇒ 卡彻底下架」与「可重试 ⇒ 回大厅」。取值**复用** `peer_inbound` 那几句既有台账人话（`CLAIM_REFUSED_*_NOTE`），⛔ 不新造一套 reason 词表。空串 ⇒ 载荷仍为**空对象**
    （`ok` 应答与「认不出原因」都走这一档，接收侧按可重试处理）。 ⚠️ 除 `reason` 外载荷刻意为空：应答的其他信息都在 kind 与 ref 里（成功后的正式订单走 **既有 `order.assign`** 推送带全量白名单，spec §4.2），再塞一份只会造出两份真相。 ⚠️ 去重照 `(binding_role, binding_id, kind, target_ref)` 的 pending 形状：同一窗口里重复应答只留一行。 ⚠️ 载荷恒为**对象**（⛔ 不是 `None`）：接收侧对「payload 不是对象」统一硬跳（`bad_shape`），空对象既过闸又不含第二份真相。"""
    if kind not in (HALL_CLAIM_OK_KIND, HALL_CLAIM_REFUSED_KIND):
        raise ValueError(f"hall.claim 的应答只能是 ok / refused，收到：{kind!r}")
    pending = PeerSyncEvent.objects.filter(
        binding_role="downstream",
        binding_id=str(binding.pk),
        kind=kind,
        target_ref=target_ref,
        status=PeerSyncEvent.Status.PENDING,
    ).first()
    if pending is not None:
        return pending
    reason = str(reason or "").strip()
    return PeerSyncEvent.objects.create(
        binding_role="downstream",
        binding_id=str(binding.pk),
        direction="out",
        kind=kind,
        target_type="order",
        target_ref=target_ref,
        # ⚠️ 只有 `reason` 一个键（空串 ⇒ 空对象）；见 docstring 的三条口径。
        payload={"reason": reason} if reason else {},
        meta={"occurred_at": timezone.now().isoformat()},
        idempotency_key=uuid.uuid4().hex,
    )


def notify_hall_membership(order, *, from_status, target, hall_publisher,
                           leave_reason=None) -> None:
    """peer 大厅镜像的**进 / 出厅漏斗**（D-0303）：`apply_transition` 成功后的唯一接线点。 - **进厅**（目标 `WAITING_GRAB` 且带发单人）⇒ `hall.throw`。`hall_publisher is not None` 与「写 `published_at`」的判据同一支（`/throw-to-hall/`、通用 `/transition/` 目标
    `WAITING_GRAB`、录单即抛单三条路都经它，见 `models.Order.published_at` 的写入点注释）； - **出厅**（`from == WAITING_GRAB` 且目标不是它）⇒ `hall.take`——被接走 / 撤回 / 指派给绑定账号都收敛在这一支（与离厅快照 `hall_snapshot.record_native_exit` 的
    同一个写点枚举）。⚠️ 软删不经状态机 ⇒ `recycle.soft_delete_order` 另接一处。 ⚠️ 位置在 `suppress_outbound_sync` 闸**之外**（与 `notify_order_status_changed` 同排）：那道闸关的是「发回鸡蛋铺 / 上家的回声」，而下家的大厅镜像是**另一个当事人**—— 押金线机器接手把我方单从大厅接走时，下家照样要收到摘牌事件。 ⚠️ 判据用 `str(...)`（`Order.status` 存的是字符串，`target` 可能是 `OrderStatus` 枚举）。"""
    if str(target) == str(OrderStatus.WAITING_GRAB) and hall_publisher is not None:
        enqueue_hall_event(order, kind=HALL_THROW_KIND)
    if (
        str(from_status) == str(OrderStatus.WAITING_GRAB)
        and str(target) != str(OrderStatus.WAITING_GRAB)
    ):
        # 指派族经 hall_leave_reason 带上「为什么离厅」，接收侧据此收单；其余裸 take。
        enqueue_hall_event(
            order,
            kind=HALL_TAKE_KIND,
            meta_extra={"leave_reason": leave_reason} if leave_reason else None,
        )
