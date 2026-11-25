"""结算金额纯函数（净实收额 / 利润 / 应付净额）——spec §4.4 ②③ 的唯一实现处。 **为什么单独成模块**：与 `performance_amount.py` 同因——金额算错**不会报错，只会算错钱**，所以三条算式各有**一处**实现，供 P4-6/P4-7/P4-8/P4-9 复用，且能被不经 HTTP 直接测试。三条纪律： 1. ⭐ **毛收款 = 订单总价 `total_price`**（业务方裁定，D-0142；spec §4.2）： `net_received = total_price − refund_fee`。 ⛔ **不读 `received_price`**（接单实收款）：录单页那个隐藏框把它写成 `0` （`display:none` + `value="0"`，C10），当毛收款用会把每一张新单算成 0。 ⛔ **不读 `hall_price`**：那是给打手的价格（D-0081 的裁定理由不变）。 ⚠️ **不再有「两边都空」这个状态**：`total_price` 是 `null=False default=0.00` 的列（`models.py`）⇒ 恒有值，本模块**不抛 `AmountError`**；原「两边都空 ⇒ 抛 `AmountError`」
的分支与文案**已删除**（不可达的死码 + 说假话的注释比没有更糟，E-0011）。「不许把没填的价静默按 0」这条纪律的**动机仍然成立**——只是现在**没有「没填」这个状态**： `total_price` 本身就是录单人填的数（没填即 0）。 📌 **`is not None` 那条教训（E-0021）在本函数上不再有可空分支**，但它仍然管着**别的调用点** （凡是读可空金额列的地方，判据都不许写真值）。 2. ⭐ **扣点额一律走 `performance_amount.deduct_amount`**（P4 的 B3 抽它出来就是为了这里）：应付净额与 P3 的扣点口径必须**同一份实现**，`base` 用 **`temp_amount`**（「预计绩效总计」），不是 `bonus_amount`。⚠️ 本模块**不出现** `x rate / 100`——量纲只允许有一处（spec §4.4「量纲统一」）。 ⭐ **D-0049 方案 A（业务方裁定）之后的口径**：交单（`/submit`）会把该行的 `temp_amount` **跟着实际佣金一起写**（`api/order_views.py`）⇒ **交过单的行上
`temp_amount == bonus_amount`**；未交单的行仍是认领 / 指派时刻的预计值。 `base` 这一处**一个字不改**——变的不是算式，是 `temp_amount` 的写入点。 3. ⭐ **负号语义**（spec §4.4 规则 4）：负的 `total_price` **照实代入**，不取绝对值、不拒绝；`payable_net` **允许返回负值**——拒绝结账是 P4-7 的 `1011`， **不是**这个函数的事。⛔ 本模块**不加任何 `>= 0` 校验**（D-0015）。 ⚠️ **返回值一律是 `Decimal`**：调用方手里的实例可能是**未从库里刷新的**——实测 `Order.objects.create(hall_price="88.00")` 之后，内存里的属性**仍是字符串**（Django 不会把 `get_prep_value` 的结果写回实例；同一坑见 `performance_amount.effective_taker_price` 的 docstring）。尤其 `deduct_amount(base=...)` 的 `base` 若是 `str`，`commission_amount` 里会 `TypeError: can't multiply sequence by non-int` ⇒ 这里统一 `Decimal(...)` 收口。 ⚠️ 本模块**不写库、不碰 request、也不构造查询**（`unsettled_adjustments` 由调用方筛好传进来，筛「未结账」是调用方 P4-7 的职责），本函数只对**给定**的记录求和。 ⚠️ 但「不构造查询」**不等于**「迭代时不执行 SQL」：调用方传 `QuerySet` / 生成器时， `payable_net` 里那次 `for` 迭代会**在本函数的栈内**执行那条查询（实测：两种入参都得到正确结果）。"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum

from apps.business.models import EmpType, LegacySplitRefundPolicy, Performance
from apps.business.performance_amount import CENT, deduct_amount
from apps.business import split_group


def net_received(order):
    """净实收额 = **订单总价 − 累计退款**（业务方裁定，D-0142；spec §4.2）。 ⛔ **不读 `received_price`**（接单实收款）：录单页那个隐藏框把它写成 `0`
    （`display:none` + `value="0"`，C10），当毛收款用会把每一张新单算成 0。 ⛔ **不读 `hall_price`**：那是给打手的价格（D-0081 的裁定理由不变）。
    ⚠️ **本函数不会失败**：`total_price` 是 `null=False default=0.00` ⇒ 恒有值。 ⚠️ 返回值恒为 `Decimal`（未刷新实例上属性可能是 `str`，见模块 docstring）。"""
    return (Decimal(order.total_price) - Decimal(order.refund_fee)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def profit_of(order, taker_bonus, *, net_received_override=None):
    """利润 = 净实收额 − 打手生效绩效（spec §4.4 ② / D3）。
    ⚠️ `fee` 与 `platform_price` **不参与**（spec §4.4 硬规则 1）。
    ⚠️ **允许为负**（亏损单）——这里不拒绝。"""
    received = net_received(order) if net_received_override is None else Decimal(net_received_override)
    return (received - Decimal(taker_bonus)).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def legacy_group_net_received(order):
    """旧式拆单主单的净收款按组内价格和退款合计。"""
    if not split_group.is_legacy_member(order) or not split_group.is_main_member(order):
        return net_received(order)
    members = split_group.active_members(order.split_id)
    totals = members.aggregate(total=Sum("total_price"))
    marked = LegacySplitRefundPolicy.objects.filter(
        split_id=order.split_id, order_id__in=members.values('pk')
    ).exclude(order_id=order.pk)
    refunds = marked.aggregate(total=Sum('refund_total'))
    return (Decimal(totals["total"] or "0.00") - Decimal(order.refund_fee) - Decimal(refunds["total"] or "0.00")).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def taker_bonus_base(order) -> Decimal:
    """客服提成算式的**打手减项**（D-0357 G13 的唯一出处；业务方口径 10）。 - **非组内单**：本单 MAIN 行的 `bonus_amount`（无行 / 未接单 ⇒ `0.00`）—— 与 `recompute_customer_commission` 原地那段容错查询**逐字等价**（Ruling 15 的「缺打手行时答案仍然唯一」口径照旧），本函数不新增行为，只是收口。
    - **组内主单**：**全组在组成员**（活跃成员，已软删者已出组）MAIN 行 `bonus_amount` 之**和**——客服利润 = 主单接单价 − 各子单打手价总和（`uniq_order_main_taker` 保证每单至多一行 MAIN，`Sum` 即逐单之和）。 ⚠️ 成员集合按 `split_group` 的口径：REFUNDED 成员**在组**、其打手行金额按现行
    退款清账机制反映（不特判，spec Q4）；「预计侧」**不用**本函数的组支（G12 裁定主单 temp 组未齐恒 0，预计减项仍取本单 MAIN 行——见 `refresh_customer_estimate` 的第 3/4 条）。 ⚠️ 本模块其余三条算式「不构造查询」的纪律对**本函数**是**显式豁免**（D-0357 spec §4 指名的落点）：减项要跨单取数，纯函数收不了这个形状；查询是两条 `split_id` 索引小查询，调用方（完成钩子 / 重算端点 / 预计跟随）本就坐在事务里。 ⚠️ 返回值恒为 `Decimal`（`Sum` 的 `None` 与未刷新实例上的 `str` 都在这里收口）。"""
    if not split_group.is_legacy_member(order):
        taker = Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
        ).first()
        return Decimal("0.00") if taker is None else Decimal(taker.bonus_amount)
    total = Performance.objects.filter(
        order__in=split_group.active_members(order.split_id),
        emp_type=EmpType.MAIN,
        order__deleted_at__isnull=True,
    ).aggregate(total=Sum("bonus_amount"))["total"]
    return Decimal(total or "0.00")


def payable_net(performance, unsettled_adjustments):
    """应付净额 = `bonus_amount − 扣点额 + Σ(未结账调整记录.amount)`（spec §4.4 ③）。 ⭐ **扣点项是必需的**（spec §17.1 #2）：`bonus_amount` 按 ① 是**未扣点**的，漏掉扣点项 ⇒ P3 的两种扣点模式在 P4 被静默废掉、**打手按未扣点金额结账**。
    ⭐ **允许返回负值**：亏损单拒绝结账是 P4-7 的 `1011`，不是这里。 `unsettled_adjustments` 是**已由调用方筛好的**「该绩效下尚未结账的 `PerformanceAdjustment`」（可迭代即可，传 `QuerySet` / 生成器都行）。⚠️ 本函数**不构造查询**，但入参若是惰性的
    `QuerySet`，上面那次 `for` 迭代会在**本函数栈内**执行 SQL；「筛法变了不会静默改变结果」只对**已经查好的序列**成立。 ⚠️ **会透传 `deduct_amount` 抛的 `AmountError`**（比例 > 100、或固定额 > `temp_amount`，见 `performance_amount.deduct_amount`）——**不吞、不折算成 0**。调用方（P4-7 / P4-8 的列表端点会**逐行**调本函数）必须预期这个失败形态，按业务码翻译，而不是当成「净额 0」。"""
    total = Decimal(performance.bonus_amount) - deduct_amount(
        mode=performance.deduct_mode,
        value=performance.deduct_value,
        # ⚠️ 必须收口成 `Decimal`：未刷新实例上它是 `str`，`commission_amount` 会 `TypeError`。
        base=Decimal(performance.temp_amount),
    )
    for adjustment in unsettled_adjustments:
        total += Decimal(adjustment.amount)
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
