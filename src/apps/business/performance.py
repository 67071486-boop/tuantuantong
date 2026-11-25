"""绩效的纯逻辑：创建与状态副作用。**不碰 request**，可被不经 HTTP 直接测试。"""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone


from apps.business import order_time_fields
from apps.business.commission import commission_percent_of
from apps.business.models import (
    EmpType,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_status import register_target_hook, stamp_local_action
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.performance_amount import (
    CENT,
    AmountError,
    MissingPriceError,
    commission_amount,
    effective_taker_price,
    ensure_amount_fits,
)
# ⚠️ 只借**两个符号**（那条留痕的 `reason` 前缀 + P4-M2 的回池冲销函数），不借别的——
# `refunds.py` 不 import 本模块，方向单一，不成环。
from apps.business.refunds import TAKER_TRACE_PREFIX, reverse_previous_round_refund
# 进池清接手人是唯一漏斗。撤单事件只在这一处入队，不要在各个端点再写一套。
from apps.business.peer_dispatch import enqueue_order_withdrawn
# ⭐（本批）：客服行**预计**绩效的重算与「打手重算」同批发生 ⇒ 两者放在**同一次** `recompute_order_amounts` 里（下面那个函数体末尾的主挂点），⛔ 不给 8 个调用点各写一句。
# ⚠️ **不成环**：`settlement_hooks` 的顶层 import 只有 `refunds` / `peer_dispatch` / `order_status` / `commission` / `performance_amount` / `models`，**没有** `performance`
# （`refunds` 只在函数内懒导入 `jidanpu_hall`）⇒ `performance → settlement_hooks` 是单向的。
from apps.business.settle_requests import cancel_pending_settle_requests
from apps.business.settlement_hooks import refresh_customer_estimate
from apps.business.wxpusher_events import queue_order_notice
from apps.core.error_codes import ErrorCode


def create_performance_for_order(order):
    """录单时建打手绩效和客服绩效。打手行还没有人，预计绩效按发单价全额估：这时还不知道谁来接。
    客服行必须在这里一起建。另开一个入口，两条路总会漏一处。没有销售人就不建客服行。"""
    main = create_main_performance(order)
    create_customer_performance(order)
    return main


def create_main_performance(order):
    """建该订单的**代练员那条**绩效行（`temp_amount` 按接单价估值）。 ⚠️ **必须是本模块唯一建 MAIN 行的出口**：录单（`create_performance_for_order`）与
    「缺行补建」（`ensure_main_performance_of`）共用这一句，⛔ 不要在别处再抄一遍「`hall_price` or `0.00`」——那一抄就会在两条路上各漂一次（E-0021 同族）。"""
    return Performance.objects.create(
        order=order,
        emp_type=EmpType.MAIN,
        temp_amount=order.hall_price if order.hall_price is not None else Decimal("0.00"),
    )


def create_customer_performance(order):
    """录单时建客服销售提成那一行。没有销售人就不建。金额和提成率都留空。录单时读提成率会把建单绑到薪酬配置上，模式不对整单失败。
    中途不重算这一行，写下的非 0 立刻过时。真钱在完成时写入生效绩效。"""
    if order.sales_user_id is None:
        return None
    return Performance.objects.create(
        order=order,
        emp_type=EmpType.SALES,
        user=order.sales_user,
        temp_amount=Decimal("0.00"),
        commission_rate=None,
        bonus_amount=Decimal("0.00"),
        review_status=None,
    )


def set_taker(performance, user, *, allow_missing_price: bool = False):
    """把代练员绩效的归属人写成 `user`，并按这个人重算预计绩效。接单时刻和归属人同一次保存。拆开写会留下「有人却没有接单时刻」，统计按这一列取数。
    同时写订单的发单时间：只写第一次、不清空，和接单时刻用同一个时间。写完人之后作废冷静期内的解冻申请。钱包模块在函数内导入，顶层导入会成环。
    `allow_missing_price` 只给代练通同步：远端已接、本地还没有发单价。默认两边都没价就抛错，不猜 0。允许时人已经写上，金额等补价再算。按异常类型放行，不按「此刻发单价是不是空」判断。"""
    performance.user = user
    moment = timezone.now()
    performance.claimed_at = moment
    performance.save(update_fields=["user", "claimed_at", "updated_at"])
    # ⭐ 线 E：接手 = 「这张单第一次被发出去给打手」⇒ 写 `Order.published_at` 走**唯一实现** `order_time_fields.mark_published`（E-0021：`/claim` 的条件更新
    # 那一处写的是**同一个事实**，两处各写一遍必然漂移）。它内部用 `WHERE published_at IS NULL` 落实「只写第一次」、且**不清空**（与
    # `Order.completed_at` 离开已完成会清**有意不同**）。 ⚠️ **传的就是上面那个 `moment`**（⛔ 不要各取一次 `timezone.now()`）。
    order_time_fields.mark_published(performance.order, moment=moment)
    from apps.accounts.wallet import cancel_release_on_claim

    cancel_release_on_claim(performance.user_id)
    try:
        return recompute_temp_amount(performance)
    except MissingPriceError:
        # ⭐ FX2：只有「两边都没价」这一档、且**调用方明确允许**时才放行。
        # ⚠️ 接手人那几笔已经写完了（上面）⇒ 这里只是「金额待补价」，不是「没记上人」。
        if not allow_missing_price:
            raise
        return performance


def recompute_order_amounts(performance):
    """按有效基价重算这一行。订单已经完成时，生效绩效用同一个数一起写。改价、指派带价、改金额、改接单价都走这里，不要在调用方再写一遍算式。
    没完成就不动生效绩效，哪怕它原来有值。状态读的是挂在这行上的订单的当前状态，不要传请求开始时的旧状态。提成算不出就抛错，由端点整笔回滚。吞掉会留下一笔错的金额。
    保存之后，打手行会刷新客服预计。挂在这里，新增的打手重算入口会自动带着走。上游给定固定金额的直写不经过本函数，那条路自己补刷。客服行也能进到这里：不是打手行就不要再刷客服预计，否则会把打手算式盖掉。"""
    amount = commission_amount(
        effective_taker_price(performance), commission_percent_of(performance.user)
    )
    performance.temp_amount = amount
    update_fields = ["temp_amount", "updated_at"]
    if str(performance.order.status) == str(OrderStatus.COMPLETED):
        performance.bonus_amount = amount
        update_fields.insert(0, "bonus_amount")
    performance.save(update_fields=update_fields)
    # ⭐ 客服行的**主挂点**（计划 §2 T1.2）：主挂点覆盖「打手重算」的整批事件，见 docstring。
    # ⚠️ 位置在 `save()` **之后**：`refresh_customer_estimate` 重查 MAIN 行取减项
    # （`taker.temp_amount`），排在 save 之前会读到旧值。
    if str(performance.emp_type) == str(EmpType.MAIN):
        refresh_customer_estimate(performance.order)
    return performance


def recompute_temp_amount(performance):
    """按接单人重算预计绩效。只是 `recompute_order_amounts` 的别名。
    订单已完成时，那一次重算会连生效绩效一起写。名字留下来是因为抢单和指派都按它调用。"""
    return recompute_order_amounts(performance)


def apply_amount_to_effective_base(performance, amount):
    """把金额 V 反推成**有效基价**、写回该落的那一列，再按新基价重算预计绩效。这是 spec §4.1（D-0147 批 E）第 2–4 步的落点，服务 `POST /performances/<id>/adjust-amount/`： 1. 提成率 `rate = commission_percent_of(performance.user)`（无档案 / `value == 0` 回落 100）； 2. 反推基价 `base = (V ÷ rate × 100)`，量化到分、`ROUND_HALF_UP`（与 `commission_amount` 同口径）；
    3. 写回**有效基价该落的那一列**：`performance.taker_price` 已设 ⇒ 写它；否则写 `performance.order.hall_price`； 4. 调共用的 `recompute_order_amounts` 让预计绩效（完成态下**连生效绩效一起**）跟着新基价走。 ⚠️ **算式不在本函数里复制**：比例走 `commission_percent_of`、金额重算走 `recompute_order_amounts`（`commission_amount` 是它内部的一环）。⛔ 不要在这里另写一遍 `base × rate / 100`。
    ⚠️ **重算必须在写基价之后**：`recompute_order_amounts` 读的是 `effective_taker_price`，它优先取 `taker_price`、否则读 `order.hall_price` ⇒ 顺序反了会按旧基价算（「基价变了、金额没变」两套事实并存）。 ⚠️ **基价那一列只在值真的变了时才 `save`**（判据 `!=`，不是真值）：与库里同值的写入只会抖一次 `updated_at`。金额那一步仍走既有的共用重算，它有自己的一次 `save` （本函数不重复它的写入）；审计行由端点按**值有没有变**决定，与几次 `save` 无关。 ⚠️ 提成算不出（模式不匹配 / 数值越界）⇒ 抛 `CommissionError`，由端点翻成业务码（**一行不写**，E-0022 的同族）；本函数不吞异常。 ⚠️ **反推出的 `base` 超出目标列位宽**（R-10c：入口闸只管 V 自己）⇒ 抛 `AmountError` （业务码），同样**一行不写**——不判位宽就是 PostgreSQL `DataError` ⇒ 500。"""
    rate = commission_percent_of(performance.user)
    base = (Decimal(amount) / rate * Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    # ⭐ **R-10c**（控制方裁定；spec §4.1）：写库**之前** 按**目标列自己的**位宽校验反推出的 `base`。入口闸只管 `amount`（V）自己，
    # 而 `base = V ÷ rate × 100` 会被低提成率放大（25% 档 ×4、1% 档 ×100）⇒ 不判位宽就是 PostgreSQL `DataError` ⇒ **500**。超界 ⇒ `AmountError`（业务码），
    # 调用方整趟回滚（`atomic()` 因异常退出）⇒ **一行不写**。 ⚠️ 口径取自下面真正要写的那一列：`taker_price` 已设就写它，否则写 `hall_price`。
    target_field = (
        performance._meta.get_field("taker_price")
        if performance.taker_price is not None
        else performance.order._meta.get_field("hall_price")
    )
    if changes_approved_penalty_price(performance.order, base):
        raise AmountError(PENALTY_AMOUNT_LOCKED, ErrorCode.BUSINESS)
    ensure_amount_fits(base, target_field)
    if performance.taker_price is not None:
        if performance.taker_price != base:
            performance.taker_price = base
            performance.save(update_fields=["taker_price", "updated_at"])
    else:
        order = performance.order
        if order.hall_price != base:
            order.hall_price = base
            order.save(update_fields=["hall_price", "updated_at"])
            # ⭐ A8 / L-6（审计批）：反推基价**真的落在了订单的发单价列上** （客服调金额的反推结果）⇒ 同事务盖「我方最后一次本地动作时刻」
            # （唯一实现 `stamp_local_action`；这条本地价改动不该被一条更旧的上家
            # 事件冲掉）。`taker_price` 非空那一支动的是绩效列，与发单价无关 ⇒ 不盖。
            stamp_local_action(order)
            # ⭐ P3：基价落在**订单列**上的这一支（`taker_price` 为空）＝ 发单价变了
            # ⇒ 记一行下行 `order.update`。⚠️ 只在**真的写了价**这一支调
            # （`taker_price` 非空那一支动的是绩效列，与发单价无关）。
            from apps.business.peer_dispatch import notify_order_price_changed
            notify_order_price_changed(order)
            # ⭐ T3：**同一处**把价跟到下游鸡蛋铺那条线（第三方下家）—— 与上一句不是同一条线（那一句是团团通租户 ↔ 团团通租户，D-0152）。
            # ⚠️ 函数内 import：本模块顶部 import 它会与 `jidanpu_member` 那条链成环
            # （照上一句对 `peer_dispatch` 的既有处置）。
            from apps.business import jidanpu_member
            jidanpu_member.queue_price_update(order)
    recompute_order_amounts(performance)
    return base


def write_performance_amounts(performance, *, temp_amount=None, bonus_amount=None):
    """按「带值才写」的判据改这两个金额列（`None` = 本次不改这一列，判据 `is not None`）。 ⚠️ **它是本批两个触发点共用的写入口，但**不是**全仓唯一写这两列的地方**—— 既有的 `/submit/`、`settlement_hooks.py`、以及全额退款 `refunds.py` 的 `Performance.objects.filter(...).update(temp_amount=0.00, bonus_amount=0.00)`
    都照旧直接写（退款那处刻意用 queryset `.update()` 防并发丢更新），本批**不动**它们。 ⚠️ **为什么把归零收口成函数**（spec §6 的 E-0027 纪律 + 裁定 Z6）：`temp_amount` / `bonus_amount` 的**归零**有两个触发点（绩效弹层的垃圾桶走 `/adjust/` 的显式键、 `/clear-taker/?isDelEmp=Y` 直接调 `zero_performance_amounts`）。两处各写各的
    = 两套判据（E-0021 同族）⇒ 「这两处怎么写」只有这一处。 ⚠️ **只改内存**：`save()` 与留痕由调用方在自己的 `transaction.atomic()` 里做（两处的留痕语义不同：`/adjust/` 写 `performance.adjust` 逐字段行、`/clear-taker/` 写既有的 `order.clear_taker` 行）。 ⛔ 不加 `>= 0` 校验（D-0015）：这两个量 0 与负数都合法。"""
    if temp_amount is not None:
        performance.temp_amount = temp_amount
    if bonus_amount is not None:
        performance.bonus_amount = bonus_amount
    return performance


def zero_performance_amounts(performance):
    """把一条绩效的**两个金额列**都写成 `0.00` —— 本裁定两个触发点共用的唯一实现（spec §6 / Z6）。两个触发点共用它：① 绩效弹层的垃圾桶（前端发 `/adjust/` 的固定载荷，见 `performance_views.PerformanceViewSet.adjust`）；② `/clear-taker/?isDelEmp=Y`。
    ⚠️ 语义：**记录保留**、`user` / `hours` / `project` / `taker_price` / 扣点**都不动** ——这是它与 `reset_taker_performance`（会清归属人与那几列）的分工。
    ⚠️ 全额退款那条**既有**的清零路径（`refunds.py` 第 2 步、queryset `.update()`）**不**走本函数：它有自己的并发理由（`.update()` 防读改写丢更新），本批**不动**它。"""
    return write_performance_amounts(
        performance, temp_amount=Decimal("0.00"), bonus_amount=Decimal("0.00")
    )


def reset_taker_performance(performance):
    """清除接单人：属于这个人的字段一起清掉。清归属人、审核状态、生效绩效、覆盖价、接单时刻、时长和项目。预计绩效不动。
    认领和指派会重算预计绩效；抛进大厅改价不会，所以认领之前全额估值会留着。只清归属人会留下「没人接却已驳回」，或撞上「待审核必须有人」。
    审核状态写成空值，空串也会撞那条约束。覆盖价和接单时刻留下，会把上一个人的值带给下一个人。同时作废这一行上还没结算的提前结算申请；已经结算的不动。"""
    former_taker_id = performance.user_id
    performance.user = None
    # 空值，不是空串。空串会撞「待审核必须有人」。
    performance.review_status = None
    performance.bonus_amount = Decimal("0.00")
    # 覆盖价属于这个人。留下会把上一个人的价带进大厅。
    performance.taker_price = None
    # 接单时刻跟着人走。留下会算进新接单人的统计。
    performance.claimed_at = None
    # 时长和项目也属于这个人。清掉后要重设，这里不留快照。
    performance.hours = None
    performance.project = ""
    performance.save(
        update_fields=[
            "user",
            "review_status",
            "bonus_amount",
            "taker_price",
            "claimed_at",
            "hours",
            "project",
            "updated_at",
        ]
    )
    # ⭐ 第 N 件事（spec §8.1 的**第三条作废路径**）：**清除接单人 / 回池 / 重指派** ⇒ 这一行上 `pending` 的提前结算申请置 `cancelled`。 ⚠️ **为什么落在本函数**：三条入口（`/clear-taker/`、回池钩子 `_on_pool_entry`、
    # `/assign/` 换人）**共同**调它 ⇒ 一处覆盖三条（E-0021 同族；⛔ 不要在三个调用点各写一遍 update，那必然漂）。**重指派**那一档尤其需要：旧人的申请不作废，新人的财务列表就会挂着旧人的「已申请」。 ⚠️ **只作废 `pending`**：`settled` 的申请意味着那笔钱已经按当时的结账打了，
    # 换人不改变这个事实（业务方口径：settled 不动）。 ⚠️ 条件更新（只命中 `pending`）⇒ 幂等；`performance.pk` 恒有值（本函数只服务已落库的行）。**不开事务**：调用方各自在自己的 `atomic()` 里（回池钩子 / `/assign/` / 垃圾桶都在），这里跟着一起提交 / 回滚。
    cancel_pending_settle_requests([performance.pk])
    if former_taker_id is not None:
        queue_order_notice(
            performance.order, former_taker_id, "unassigned", performance.updated_at,
        )
    return performance


def main_performance_of(order):
    """取这张单的打手绩效。缺行就抛「不存在」。产品代码里已经没有调用方，只剩测试。缺行现在是正常形态：指派会补行，提交会按没有接单人拒绝。
    新调用方如果缺行也算正常，不要用本函数，否则会变成 500。"""
    return Performance.objects.get(order=order, emp_type=EmpType.MAIN)


def ensure_main_performance_of(order):
    """取打手绩效行；没有就补一条。客服行不在才另建。缺行不是数据坏了：没走录单入口的单、退单回池，都可能没有行。
    跳过会把失败挪到下一步，抢单会报「已被别人接走」。只补打手行。客服行还在时连它一起建，会撞唯一约束，指派整笔失败。
    新行没有人，预计绩效等于发单价。"""
    # 已软删的单不算有打手行，查询会落到补建。
    # 调用方自己挡住已删单。直接把已删单喂进来会撞唯一索引，不要改成兜底把异常吞掉。
    perf = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).first()
    if perf is not None:
        return perf
    # 只补打手行。客服行还在（含软删）就不再建，唯一索引不看软删，再建会让指派失败。
    main = create_main_performance(order)
    if not Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists():
        create_customer_performance(order)
    return main


# 钩子在模块导入时注册。ready() 保证本模块被导入，否则有的路径会静默没有钩子。
def _on_pool_entry(order, from_status, *, actor_id=None, allow_financial_writes=True):
    """订单回到「未分配 / 待抢单」时，把绩效退回「没人接」。回退不看来源状态：这两个状态就是没人接。来源状态留在参数里是钩子的约定，不要据此给回退加判断。没有绩效行就补，不跳过，也不报 500。
    来源状态只决定退单这一支要不要多做三件事：记下原接单人、冲掉上一轮退款、把客服这一轮的佣金快照归零。问题单和暂停回池不做这三件。正常退单的原金额记在清零之前；这里是兜底，已经记过就不再记。
    退单不再改累计退款，冲销只对部分退款留下的余额生效；客服那条反向调整仍然要做。归零必须在冲销之后，否则这段时间这笔佣金还能被财务勾去结账。 `actor_id` 是这次操作的人。回池冲销要带上他；机器路径不传。清人之前先记住被释放的人，再记撤单。记账排在回退之后，否则对方会当成过期消息跳过。"""
    perf = ensure_main_performance_of(order)
    # ⭐ 上下游 P2 ：**清人之前**记下被释放的那个人。`reset_taker_performance` 会把
    # `perf.user` 清成 `None`，之后再读就只剩 `None`（与下面那条留痕同一个「先取后改」
    # 的形状）。缺行补建出来的新行 `user` 为空 ⇒ 没有「不再属于这一单」的人可通知。
    released_taker = perf.user if perf.user_id is not None else None
    if str(from_status) == str(OrderStatus.REFUNDED):
        _record_taker_before_pool_entry(order, perf)
        # ⭐ P4-M2（业务方选定候选 A）：**同一个来源态守卫**下，把上一轮结清—— 复位 `order.refund_fee`（+ 一条 `REVERSAL` 事件行）+ 等额反向客服行的冲减链。三件事少一件就是新的算错钱，代数与实测见 `refunds.reverse_previous_round_refund`。
        # ⚠️ 与留痕共用守卫是**有意**的：它们都是「退单这条边」的副作用（`ISSUE`/`PAUSED → 池内` 既不写留痕、也不冲销）。 ⭐ 把本次迁移的**操作人主键**一起传下去——那两条冲销留痕
        # 是**人为动作**（`/clear-taker/`、`/transition/`），没有主键就只能是机器行。 `actor_id=None` = 机器 / 同步路径调进来的，`refunds` 侧会显式声明 `system=True`。
        reverse_previous_round_refund(
            order, actor_id=actor_id, allow_financial_writes=allow_financial_writes,
        )
        # ⭐ F-1：把客服行的 `bonus_amount` 一并归零。为什么必须在**冲销之后**：冲销把上一轮的 `FULL_REFUND(−8.00)` 等额反向（`+8.00`），而 `bonus_amount` 还留着上一轮的快照 `8.00` ⇒ 窗口期终身合计与 `payable_net`
        # 都读成 `8.00`，这一行会出现在财务结算列表里、**可以被勾选结账** （若一直不重做 = 纯多付这一轮的钱）。归零后窗口净额恒为 `0.00`。口径：客服行的 `bonus_amount` 是「**当前轮**的佣金快照」——历史不由这一列表达，
        # 而由 `OrderRefund` 事件行 + 调整链 + 审计重建（见 docstring）。 ⚠️ **只动 `bonus_amount` 这一列**：不碰 `temp_amount` / `user` / `commission_rate` （`reset_taker_performance` 只对 MAIN 行负责，两条线不重叠）。
        Performance.objects.filter(order=order, emp_type=EmpType.SALES).update(
            bonus_amount=Decimal("0.00")
        )
    reset_taker_performance(perf)
    # ⭐（客服预计绩效线，计划 §2 T1.2 的显式挂点 13）：本钩子改了**两个**算客服预计的输入 —— 上面 `reverse_previous_round_refund(order)` 把 `order.refund_fee` 复位（净实收变），`reset_taker_performance(perf)` 又清掉 `user`（⇒ 按
    # `refresh_customer_estimate` 的第 3 条判据，减项归 `0.00`）。而它**不过** `recompute_order_amounts` ⇒ 主挂点在这里看不见，必须显式接。 ⚠️ 与上面 F-1 那句「只动 `bonus_amount` 这一列」**不矛盾**：那句管的是它自己那次
    # `SALES.update(...)`（本函数不碰 `user` / `commission_rate`）；客服行的 `temp_amount` 由 `refresh_customer_estimate` 按**共用算式**重算，是**另一件事**。
    refresh_customer_estimate(order)
    # ⭐ 上下游 P2 （spec §16.2）：这个人**不再属于这一单** ⇒ 记一条待投递的撤单事件。⚠️ **入队排在回退之后**：`enqueue_order_event` 的 `occurred_at` 取
    # `order.updated_at`（`apply_transition` 刚写过状态），排在动作之后就拿到**动作后** 的时刻；⛔ 排在之前会留下一个陈旧的时刻 —— 下家会把它当 `stale` **良性跳过**。
    # `released_taker is None`（本来就没有接手人）⇒ 一笔都不写。
    if released_taker is not None:
        from apps.internal_chat.order_groups import assignment_changed

        assignment_changed(order, None, former_worker=released_taker)
        enqueue_order_withdrawn(order, released_taker, "taker_removed")
        # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 7 行）：**同一个位置**（⛔ 不新造第二个漏斗），收件人是**前任**接手人（spec §4.2 末段）——库里那条 MAIN 绩效行的 `user` 此刻**已经被上面那句 `reset_taker_performance(perf)` 清空了**，
        # ⇒ ⛔ 不能另查一次库去"猜前任"，只能把这一处**已有的**局部变量 `released_taker` 显式传进去（`former_taker_id` 就是干这个的）。 ⚠️ `reason` 沿用左边那条既有事件的取值（`taker_removed` = 清接手人 / 撤单回池），
        # ⛔ 不在这里另写一份 reason 枚举。 ⚠️ 时刻取 `timezone.now()`（打手侧这条没有"与对端水位比较"那回事，所以不掺和上一句的 `order.updated_at` 默认值——那是 peer 接收侧的 `stale` 判据要的）。
        from apps.takerapi.api import callback_recipients as taker_callback

        taker_callback.notify_order_reassigned_for_taker(
            order,
            former_taker_id=released_taker.pk,
            reason="taker_removed",
            occurred_at=timezone.now(),
        )


def _record_taker_before_pool_entry(order, performance):
    """退单回池时，在清人之前记下原接单人。正常路径不写，只兜没有走退单钩子的行。
    主记录在清零之前已经写下真实原金额。这里按同一前缀查重，写过就不再写。
    没有接单人也不写。这是内部写入，不要为此给调整接口放开金额 0。"""
    if performance.user_id is None:
        return None
    if PerformanceAdjustment.objects.filter(
        performance=performance,
        kind=PerformanceAdjustment.Kind.MANUAL,
        reason__startswith=TAKER_TRACE_PREFIX,
    ).exists():
        return None                                          # 主留痕已经写过 ⇒ 不重复
    amount = Decimal(performance.bonus_amount)
    return PerformanceAdjustment.objects.create(
        performance=performance,
        refund=None,
        kind=PerformanceAdjustment.Kind.MANUAL,
        amount=Decimal("0.00"),
        reason=f"{TAKER_TRACE_PREFIX}{performance.user.username} / 清空时金额 {amount}",
        operator=getattr(order, "updated_by", "") or "",
        settle_status=SettleStatus.UNSETTLED,
    )


# 订单变成「代练中」时重算预计绩效。
def _on_in_progress(order, from_status):
    """订单变成「代练中」时重算预计绩效。不看来源状态。只重算预计绩效。钩子跑在状态已经写成代练中之后，生效绩效那一支自然不走。不要在这里再写一份「不写生效绩效」的判断。
    没有打手行、没有归属人、或这一行已结账，都跳过。没有人时不许按 100% 落回全额。提成算不出就让异常抛出去，整笔回滚。吞掉会留下一笔错的金额。
    指派入口跳过本钩子，钱由紧接着的写归属人按新人算。抢单仍会再算一次，结果相同。鸡蛋铺后台和上下游入向有直接改状态、绕过本钩子的路径，那些路径不算钱。"""
    taker = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).first()
    if taker is None:
        return None
    if taker.user_id is None:
        return taker
    if taker.is_settled:
        return taker
    return recompute_temp_amount(taker)


# 两个池内状态都要注册——**这是不变量的一半**，不是可选的对称性。
# ⭐ `wants_actor_id=True`：本钩子要「这次迁移是谁做的」的主键（回池冲销那两条人为留痕），
# 见 `_on_pool_entry` 的 docstring 与 `order_status.register_target_hook`。
register_target_hook(OrderStatus.UNASSIGNED, _on_pool_entry, wants_actor_id=True, wants_finance_policy=True)
register_target_hook(OrderStatus.WAITING_GRAB, _on_pool_entry, wants_actor_id=True, wants_finance_policy=True)
# 这是唯一标了「会重算金额」的钩子。跳过金额重算只跳过它。
# 不要给回池、完成、退单打这个标记。
register_target_hook(OrderStatus.IN_PROGRESS, _on_in_progress, recomputes_amount=True)
