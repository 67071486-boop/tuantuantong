"""两个目标态的副作用（P4 **唯一的钩子注册点**；spec §4.2/§5.2/§5.3，由 `apps.py::ready()` 保证被导入）。合约（spec §5.3）：钩子在迁移保存**之后**、调用方的 `atomic()` **之内**跑，抛异常则订单状态与钩子写下的行一起回滚；⚠️ 钩子不能用 `except ValueError` 包住调用方（`IllegalTransition` 继承它，会把算错金额伪装成「状态非法」）。挂载点是 `apply_transition` 的目标态钩子而非 `/review` 视图（D7，挂端点会「只在不走那条路径时漏算」）；D-0071 后 `COMPLETED` 有 `/review/` 与 `/transition/` 两条端点路径，两个调用方都必须按 E-0010 把预期失败（`CommissionError` / `AmountError`）翻成业务码——异常翻译在调用方，别只加路径不加 `except`。
`COMPLETED` 顺序不能换：补打手 → `recompute_customer_commission`（权威，算不出就抛）→ `refresh_customer_estimate`（只写 `temp_amount`，业务异常不外抛）→ 发货钩子（`on_commit` 后才真调）；`REFUNDED` = 撤单入队（注册在清绩效之前，理由见文件末尾注册处）→ 全额退款清空绩效（函数体在 `refunds.py`，注册只在这里）。
"""
from decimal import Decimal
import logging

from django.utils import timezone

from apps.accounts.choices import CommissionMode
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import split_group
from apps.business.commission import CommissionError, commission_percent_of
from apps.business.models import EmpType, OrderStatus, Performance, ReviewStatus
from apps.business.order_status import clear_target_hooks, register_target_hook
from apps.business.performance_amount import AmountError, commission_amount
# 退单时给当时的 MAIN 接手人入队一条撤单。不写在 refunds.py 里，避免平级模块互 import。
from apps.business.peer_dispatch import enqueue_order_withdrawn
from apps.business.refunds import on_order_refunded
from apps.business.settlement_amount import (
    legacy_group_net_received,
    profit_of,
    taker_bonus_base,
)
# 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.4）：订单进入「已完成」时自动发一次货。
# 真正的 HTTP 调用排在 `transaction.on_commit` 之后、失败不影响订单状态 ⇒ 本模块只负责注册，
# 判据与留痕全在 `apps/business/xgj_ship.py`。
from apps.business.xgj_ship import SYSTEM_OPERATOR, schedule_auto_ship_gated

logger = logging.getLogger(__name__)


def _persist_customer_commission(customer, *, bonus_amount, rate):
    """把确定结果写进客服行（单独一个函数：测试要能打桩到「写了一半就炸」的形状；打桩打在 `settlement_hooks._persist_customer_commission` 这个**模块全局名**上，E-0029）。⚠️ P1-5：目标 SALES 行**已结账**（`is_settled`，判据打在本行自己身上，与 `refresh_customer_estimate` 第 0 条同口径）⇒ 一个字节都不写、只记 warning（预期内跳过；留痕要能查出「为什么这个数没跟上」）。⛔ 不是 `_activate_taker_performance` 那道 MAIN 闸：SALES 行可**独立**已结账，拿 MAIN 状态代替会改写已付出去的钱；不给 `settled_by="system"` 开例外（自动入账只结 MAIN 行，SALES 行只可能是财务手工结的）。
    `review_status = 已通过`（D-0071 审核取消后它**不再是钱的闸门**）：写它表达「这一行的钱已确定生效」；写 `NULL` 会让绩效页凭空多一片空值而没有判据变好。代价如实登记：这一列从此主要是**展示态**。"""
    if customer.is_settled:
        logger.warning(
            "客服行已结账，完成钩子不改写它的提成（perf=%s order=%s）",
            customer.pk,
            customer.order.order_no,
        )
        return customer
    customer.bonus_amount = bonus_amount
    customer.commission_rate = rate
    customer.review_status = ReviewStatus.APPROVED
    customer.save(
        update_fields=[
            "bonus_amount",
            "commission_rate",
            "review_status",
            "updated_at",
        ]
    )
    return customer


def _activate_taker_performance(order):
    """订单完成时把打手绩效按预计绩效生成（业务方裁定「完成即生效」，R5/R6 落定）；返回本单 MAIN 行（可能 `None`）供调用方算客服提成减项。进 `COMPLETED` 且 MAIN 行有接单人、未（手工）结账 ⇒ `bonus_amount := temp_amount`、`review_status = 已通过`。R5：原先「交过单或 bonus 非 0 就跳过」的判断**整条移除**（只删半边会让存量交过单的单完成后永久没钱）；R6：客服驳回过的单订单完成时**照常生成**（这里没有任何按 `review_status` 的分支）。
    ⚠️ 仍然不动的形态（⛔ 不要顺手「统一」）：没有 MAIN 行 ⇒ `None`；`user_id is None` ⇒ 不做（没有归属人，补了是凭空应付款）；该行 `is_settled`（手工结账）⇒ 不做（防存量脏行带着结账痕迹被覆盖）。`review_status` 要一起写：`/adjust` 判「交过单」依据是它非空，只补 `bonus_amount` 会出现两套金额并存（D-0071：这一列表达「钱已确定生效」）；`temp_amount` 允许负数（D-0015，⛔ 不加 `>= 0` 夹取）。
    """
    # ⭐ D-0144（C19）：已软删单的 MAIN 行不在这里生效（跨关系 JOIN 不经过 `Order` 的默认
    #    manager ⇒ 必须显式补，spec §4.3-C2）。调用方本已挡住已删单（A3/A5）。
    taker = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).first()
    if taker is None:
        return None
    if taker.user_id is None:
        return taker
    # 已结账且**不是系统自动入账** ⇒ 不动（防存量脏行带着结账痕迹被覆盖）；⭐⭐ 自动入账（`settled_by="system"`，D-0279 起只对 App 注册打手）的行**不在此列**：钱包发薪后完成单会被自动标成已结账，沿用「已结账就早退」会让「完成 → 重开 → 改预计金额 → 再完成」永久停发（判据 = `tests/test_performance_completion_activation.py::test_re_entering_completed_overwrites_the_bonus_without_duplicating_rows` 要求按当时 `temp` 覆盖）⇒ 自动入账的行照常覆盖，差额由 `accounts.payout.payout_completed_order` 同事务按新净额补差（只补不退）。
    # ⛔ 别把两档合并成「一律覆盖」：存量手工结账的行被覆盖后钱包不会跟着动（那些钱没进过钱包），账面与实付对不上。
    if taker.is_settled and taker.settled_by != "system":
        return taker
    _save_activated_taker(taker)
    return taker


def _save_activated_taker(taker):
    taker.bonus_amount = taker.temp_amount
    taker.review_status = ReviewStatus.APPROVED
    taker.save(update_fields=["bonus_amount", "review_status", "updated_at"])


def activate_early_dailiantong_taker_cost(order):
    """在完成事务内补平台先结账时遗漏的打手成本。"""
    from apps.business.dailiantong_push import is_platform_settled_performance

    taker = (
        Performance.objects.select_for_update(of=("self",))
        .filter(order=order, emp_type=EmpType.MAIN, review_status__isnull=True,
                bonus_amount=Decimal("0.00"), settled_amount__isnull=True)
        .first()
    )
    if taker is not None and is_platform_settled_performance(taker):
        _save_activated_taker(taker)


def customer_commission_amount(order, taker_basis) -> Decimal:
    """客服提成 = （净实收 − `taker_basis`）× 客服提成率，**客服算式只此一份**（两个因子用既有唯一实现 `settlement_amount.profit_of` 与 `commission_percent_of(..., PROFIT_PERCENT)`；⛔ 不另写 `× rate / 100`）。它同时服务「预计」（`taker_basis` = MAIN 的 `temp_amount`）与「生效」（= MAIN 的 `bonus_amount`）两侧（D-0160 防漂移纪律；两侧差别只在减项——完成那一刻 `bonus := temp` ⇒ 同刻同值）。
    ⚠️ 会抛 `CommissionError`（模式不匹配 / 越界）：`recompute_customer_commission` 让它抛（端点翻业务码、整笔回滚），`refresh_customer_estimate` 吞掉它（估算值不许把订单动作带崩）。（对标 8 组对拍实测依据与 1 组反例：源件在不进 git 的 `.tools/recon/settlementemp/`，登记见 `docs/code-comment-notes.md` 本文件分节。）
    """
    rate = commission_percent_of(order.sales_user, mode=CommissionMode.PROFIT_PERCENT)
    legacy_group = split_group.is_legacy_member(order) and split_group.is_main_member(order)
    profit = (
        profit_of(
            order,
            taker_basis,
            net_received_override=legacy_group_net_received(order),
        )
        if legacy_group
        else profit_of(order, taker_basis)
    )
    return commission_amount(profit, rate)


def refresh_customer_estimate(order):
    """把客服行的「预计」（并在订单已完成时连「生效」一起）刷成按**当前**订单金额该拿多少；本函数自己的业务判据不往外抛异常（算不出 ⇒ 不写、回 `None`；DB 层故障照旧响亮 500）。⚠️ **函数名不改**（今天做的事不只是「预计」：`COMPLETED` 且未结账时 `bonus_amount` 也写）——改名牵动 11 处调用点而收益只有名字，读它按本句理解、⛔ 别按名字。分工：权威的钱由完成时的 `recompute_customer_commission` 写（算不出就抛）；本函数服务**订单事件**（改价 / 指派 / 抢单 / 部分退款 / 回池…）把两列跟上此刻金额，两条共用算式 `customer_commission_amount`。判据按序（完整判据表与各条理由、落点权衡、11 处调用点逐条枚举、失败面登记：见 `docs/code-comment-notes.md` 本文件分节）：**0** 该 SALES 行 `is_settled`（三字段并集，判据打在本行自己身上）⇒ 一个字节不写——⛔ 不是 MAIN 那道闸（SALES 行可独立已结账，拿 MAIN 状态代替会改写已付出去的钱；取并集是保守侧防脏行被改写；判别 = `SettledRowGateTests`；与 `refunds.on_order_refunded` 第 2.7 步共用同一属性，⛔ 别再抄）；**1** `sales_user_id is None` ⇒ `None`；**2** 没有 SALES 行 ⇒ 不写（⛔ 不补建）；**2.5** `REFUNDED` ⇒ `temp_amount` 写 `0.00`（业务方口径：判据取订单此刻状态、与触发路径无关；排在算式之前 ⇒ 提成率算不出也写 0——已退单留非 0 估值是假话，判别 = `RefundedStatusCriterionTests.test_a_refunded_order_gets_zero_even_when_the_rate_is_uncomputable`；D-0181 后照常算会比退款前还多 7.04；`bonus` 不由本函数写，未结账行由 `refunds._clear_unsettled_sales_amounts` 清 0）。
    **3** 无 MAIN 行 / MAIN 无 `user` ⇒ 减项 `0.00`（容错读）；**4** 可算且值变了 ⇒ 写 `temp_amount`；**4b** `COMPLETED` ⇒ `bonus_amount` 用「生效」减项写（两列各按自己的减项，⛔ 不是同一个 `amount`；非 `COMPLETED` ⇒ `bonus` 一个字节不动；生效那半算不出 ⇒ 两列都不写）；**5** 值相同 ⇒ 不写（判据 `!=`，E-0021）；**6** 算不出 ⇒ 不写、回 `None`、⛔ 不抛（客服档案默认 `ORDER_PERCENT`，跟着抛会让抢单 / 指派 / 改价在默认档普遍失败；判别 = `tests/test_customer_estimate.py::OrderPercentDefaultTests`）。失败面：`AmountError` 那半今天不可达（D-0142 起 `net_received` 恒有值），留在 `except` 是让「算不出就不写」成立，判别力由打桩用例承担（E-0065 同族）；并发丢更新最多停在上一刻的值，不加锁（已知代价）。"""
    if order.sales_user_id is None:
        # 与 `create_customer_performance` 同一个口径：没有归属人 ⇒ 连行都没有。
        return None
    # ⭐ D-0144（C20 的同一条 join 条件）：已软删单的客服行不算。跨关系 JOIN 不经过
    #    `Order` 的默认 manager ⇒ 必须显式补（与 `recompute_customer_commission` 同形）。
    customer = Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ).first()
    if customer is None:
        return None
    # ⭐⭐ D-0357（G11/G12/G14）：**拆单组闸 + 跨单跟随**（判据唯一出处 `split_group`，⛔ 不抄第二份）。子单 ⇒ 本行 SALES 行 `temp`/`bonus` 恒 0 原样返回（11 处调用点一次堵死「子单总价 0 ⇒ 负数客服提成」，spec §2.3），但 G14 跨单跟随仍要走；主单组未齐 ⇒ `temp` 维持 0、`bonus` 不写（利润未定，预计不预支）；主单组齐 ⇒ 落既有算式（`bonus` 侧取 `taker_bonus_base`）。
    #    ⚠️ 位置在 `is_settled` 之前是有意的：两道闸对已结账行给同一答案，组闸先行让「子单/组未齐」连算式都不进；退款那半边没有缺口（清 0 直接写入点是 `refunds._clear_unsettled_sales_amounts`，不经本函数）。
    if split_group.is_legacy_member(order):
        if not split_group.is_main_member(order):
            _refresh_split_main_estimate(order)
            return None
        if not split_group.group_all_completed(order):
            return None
    # ⭐ 业务方 Q1 的**逐行闸门**：已结账的行一个字节都不动——判据打在**目标 SALES 行自己**的 `is_settled`（三字段并集，非全仓唯一口径；⛔ 不是 MAIN 那道闸：SALES 行可独立已结账，拿 MAIN 状态代替会改写已付出去的钱；取并集是保守侧，防「状态未结账却留着 settled_at/amount」的脏行被改写）。
    #    ⚠️ 排查别只看这里：`refunds.on_order_refunded` 第 2.7 步用的是**同一个属性**。
    if customer.is_settled or customer.online_payroll_batch_id:
        return None
    # ⭐ 第 2.5 条（业务方口径，完整理由见 docstring）：**已退单状态上的任何刷新都一律得 `0.00`**——判据取**订单此刻的状态**，⛔ 不是「这次调用是否来自退款钩子」；落在本函数是因为它有 11 处调用点、退款只占一处，别的路径会把同一列**又算回非 0**（枚举见 docstring）；排在算式之前 ⇒ 提成率算不出也写 `0.00`；D-0181 后必须兜住（照常算会比退款前还多 7.04）。
    # ⭐（I-1）：`bonus_amount` **只在订单当前是 `COMPLETED`** 时写（与 `recompute_order_amounts` 对 MAIN 行同口径）。
    write_bonus = str(order.status) == str(OrderStatus.COMPLETED)
    if str(order.status) == str(OrderStatus.REFUNDED):
        amount = Decimal("0.00")
        # ⚠️ 这一格只为「变量已定义」存在：`REFUNDED` 时 `write_bonus` 恒 `False`
        #    ⇒ `bonus_amount` 由本函数**一个字节都不动**（未结账那一行的清 0 由
        #    `refunds._clear_unsettled_sales_amounts` 承担，见 docstring 第 2.5 条）。
        effective_amount = Decimal("0.00")
    else:
        taker = Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
        ).first()
        # ⚠️ `Decimal(...)` 收口：调用方手里的实例可能是**未从库里刷新**的（见 `settlement_amount`
        #    的模块 docstring「返回值一律是 Decimal」那条）。
        taker_temp_basis = (
            Decimal("0.00")
            if taker is None or taker.user_id is None
            else Decimal(taker.temp_amount)
        )
        try:
            amount = customer_commission_amount(order, taker_temp_basis)
        except (CommissionError, AmountError):
            return None
        # ⭐ I-1：**生效那一半自己取减项**（打手 `bonus_amount`），与 `recompute_customer_commission` 逐字同一个减项口径（D-0182 原文：预计传 `temp_amount`、生效传 `bonus_amount`）。正常路径两值自然相同（改价时 MAIN 的 temp/bonus 一起写成同一新值）；只有 `temp != bonus` 才分叉，那是 D-0182 明确允许的，⛔ 不是缺陷。生效那份算不出时两列都不写（`return None` 落在任何写入之前）。
        if write_bonus:
            # ⭐ D-0357（G13）：生效侧减项走**唯一出处** `taker_bonus_base`（非组内单 = 本单 MAIN `bonus_amount`，
            #    逐字等价；组内主单 = 全组成员 MAIN `bonus` 之和）。没有这半句，「末单完成」那跳的 G14 跟随刷新
            #    会按「主单自己（通常无打手）」的减项把权威生效值**覆盖成全额**（spec §2.3 点名的错账形状）。
            taker_bonus_basis = taker_bonus_base(order)
            try:
                effective_amount = customer_commission_amount(
                    order, taker_bonus_basis
                )
            except (CommissionError, AmountError):
                return None
    # 预计减打手预计，生效减打手生效；非 COMPLETED（含 REFUNDED）不动 bonus_amount（REFUNDED 上只写 temp）。
    # ⚠️ 判据是 `!=`（E-0021：⛔ 不写真值判断）——`temp_amount` 的 `0.00` 与负数都是合法值，
    #    真值判断会把「算出来正好是 0」当成「没变」而漏写。
    changed = customer.temp_amount != amount
    if write_bonus and customer.bonus_amount != effective_amount:
        changed = True
    if changed:
        update_fields = ["temp_amount", "updated_at"]
        customer.temp_amount = amount
        if write_bonus:
            # ⚠️ 只把**真的会写**的那一列放进 `update_fields`：非 `COMPLETED` 时带上它
            #    会把内存里那个（刻意不动的）旧值原样写回。
            customer.bonus_amount = effective_amount
            update_fields.insert(1, "bonus_amount")
        updated = Performance.objects.filter(pk=customer.pk, online_payroll_batch__isnull=True).update(
            **{field: getattr(customer, field) for field in update_fields if field != "updated_at"},
            updated_at=timezone.now(),
        )
        if not updated:
            return None
    return amount


def recompute_customer_commission(order, *, dry_run):
    """**客服那一半**：确定 / 重算客服销售提成（spec §D15）。完成时「确定提成」与批量端点 `orders/batch/recompute-commission/` 是**同一件事**，按 §D5 只允许一处实现（从 `on_order_completed` 原样搬出；对完成态逐字等价——早退分支、算式三输入与写入函数一字未改，唯一结构差异是打手行改由本函数重查一次，读到的都是补完并已 `save` 的同一行）。
    `dry_run=True` 只跑「**该不该算、算得出吗**」：「该不该算」= 有归属人 + 已完成；「算得出吗」= `commission_percent_of` 抛 `CommissionError` ⇒ 让它抛（调用方翻成逐笔 1000 不合格）；⚠️ 「客服行缺失」不构成 `dry_run` 早退（缺行照样跑，否则「缺行 + 算不出」会**漏报**成合格，而 `apply` 会先补建再整批回滚、用户白确认一次）；⛔ 不调写入、不读打手行、不算金额（一行都不许写）。
    `dry_run=False`（完成态）：`profit = 净实收 − 打手 bonus` ⇒ 写三列（覆盖写同一行 ⇒ 幂等）。⭐ 新增档「订单不是已完成」（§D15 第 5 条）：**金额不动**、只保证行存在，如实回报「已生成 / 无需更新」；必须早于 `commission_percent_of`（非完成态不进算钱段 ⇒ `dry_run` 与 `apply` 同一答案；对完成态单不可达）。`order.status` 判据用 `str(...)` 比较（`TextChoices` 在 DB 与内存里 `str()` 后同值；完成钩子路上恒为 `COMPLETED`）。"""
    # ⭐ D-0144（C20）：已软删单的客服行不算提成（同一处 join 条件）。
    customer = Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ).first()
    # ⚠️ **真写路径**上「无客服行 ⇒ `None`」排在最前（与原 `on_order_completed` 逐字相同）；`dry_run` 在这一句
    #    **不早退**：「算得出吗」那道闸与那一行在不在无关（批量路径缺行是常态——补建正是这一笔要做的事）
    #    ⇒ 让它继续往下跑，否则「缺行 + 提成率算不出」的现场会**漏报**成合格。
    if customer is None and not dry_run:
        # 录单时 `sales_user` 为空 ⇒ 没有客服行（P4-8 的「待归属」兜底）。无可确定。
        return None

    if order.sales_user_id is None:
        # 归属人事后被清空 ⇒ **不重算**，也不把这条已存在的行指给别人（等 P4-8 待归属）。
        # ⚠️ 不能落到 `commission_percent_of(None)`：那会按「无档案」回落 100% 利润，
        #    是一笔**静默算多**的钱。
        return None

    # ⭐⭐ D-0357（G11/G12）：**拆单组闸**（判据唯一出处 `split_group`）。本函数同时服务完成钩子与批量重算端点（`dry_run` 两段共用）⇒ 闸落这里两条路一次堵死：子单 ⇒ `temp`/`bonus` 恒 0 原样返回（`dry_run` 同答案，批量侧如实报「已生成/无需更新」）；主单组未齐 ⇒ 不写 `bonus`，`rate` / `review_status` 也不动（组齐那刻的生效由两路触发：末单完成那一跳或 G8 出组收口）。
    #    ⚠️ 位置在「非完成态」档之前不改变任何答案（逆否：本单未完成 ⇒ 组必未齐，两档都原样返回；「已完成、组未齐」恰是组闸要接、非完成态档接不住的形状——主单可先于子单完成，`test_main_completing_with_the_group_incomplete_writes_nothing` 就是它）。
    if split_group.is_legacy_member(order):
        if not split_group.is_main_member(order):
            return customer
        if not split_group.group_all_completed(order):
            return customer

    # ⭐ 新增档（spec §D15 第 5 条）：订单不是「已完成」⇒ 只保证行存在、**金额不动**。
    #    ⛔ 必须早于 `commission_percent_of`：这一档不进算钱那一段（见 docstring）；
    #    两个 `dry_run` 取值在这里给出**同一个**答案（`apply` 也不算 ⇒ 没有 dry_run/apply 不对称）。
    if str(order.status) != str(OrderStatus.COMPLETED):
        return customer

    # ⑥「算得出吗」：`dry_run` 与真写**共用这一处**（§D5 的唯一判据入口）。
    #    `CommissionError` 由调用方翻成逐笔 1000（本函数不吞它，E-0010）。
    rate = commission_percent_of(order.sales_user, mode=CommissionMode.PROFIT_PERCENT)
    if dry_run:
        # ⛔ 到此为止：`dry_run` 只回答「该不该算、算得出吗」，不试算金额、不写任何东西。
        #    真写路径到这里 `customer` 一定非空（上面那句 `and not dry_run` 已保证）。
        return customer

    # ⚠️ 无接单人 ⇒ 打手行仍在（`user` 为空）、`bonus_amount` 是初始 0.00 ⇒ 利润 = 净实收，客服提成照算（brief「订单可以没派过人就被改完」要的结果，⛔ 不要加「没接单人就不算提成」——那是**少发钱**的方向）。打手行整个不存在也走同一口径（I-4 容错查询；原先 `main_performance_of` 缺行即抛，会让「只有客服行」的形态一完成就 500；不违反「不变量破损要响亮」——Ruling 15 服务的是必须拿到那行才能干活的调用方，本函数缺打手行时答案仍唯一）。方向是**多发钱** ⇒ 必须有测试钉住（`tests/test_p4fu_order_complete.py::test_order_with_only_a_customer_row_completes_without_500`）。
    # ⚠️ 抽取后这里改成重查（原实现用 `_activate_taker_performance` 的返回值，那个值只在 `on_order_completed` 局部）：完成钩子路上读到的是补完已保存的值（逐字等价）；批量端点直接调时读到「当刻」值（**不激活**打手，spec §D15「不碰打手那一侧」正靠这一点：本函数对 MAIN 行只读不写）。⭐ D-0357（G13）：减项唯一出处 = `taker_bonus_base`（非组内单 = 本单 MAIN `bonus_amount`，逐字等价；组内主单 = 全组成员 MAIN `bonus` 之和——业务方口径 10：利润 = 主单接单价 − 各子单打手价总和）。
    taker_bonus = taker_bonus_base(order)
    # ⭐ 算式走**共用**的那一份（减项传**生效**绩效）⇒ 「预计 / 生效」两侧不可能各漂一份实现。
    #    `rate` 上面已取过一次（`dry_run` 的「算得出吗」闸），内部再取一次：多一次读，换「只有一处算式」。
    return _persist_customer_commission(
        customer,
        bonus_amount=customer_commission_amount(order, taker_bonus),
        rate=rate,
    )


def _refresh_split_main_estimate(order):
    """**G14 跨单跟随**：组内成员的刷新末尾追加「刷新主单」（唯一咽喉在 `refresh_customer_estimate` 内部）。`recompute_order_amounts` 末尾本就调 `refresh_customer_estimate` ⇒ 改价 / 指派 / 抢单等跟随路径自动获得传导，⛔ 不在那些调用点再各加一遍（两处都加会对主单重复刷一遍）。防递归：成员就是主单时跳过；主单自己的组闸（G12）挡「组未齐」档；主单缺行 ⇒ 一笔不写、跟随失败不外抛（「预计值不许把订单动作带崩」同口径）。
    """
    if not split_group.is_legacy_member(order) or split_group.is_main_member(order):
        return
    main = split_group.main_member(order.split_id)
    if main is None:
        return
    main = split_group.lock_main_and_sales_nowait(main)
    refresh_customer_estimate(main)


def _recompute_main_commission_for_group(main) -> None:
    """**G15 跨单触发的回滚语义**：组齐时刻对主单的权威重算，**吞 `CommissionError`** 并**响亮留痕**一条订单日志 ⇒ 最后一单的完成绝不被主单客服的提成配置挡住。只吞 `CommissionError`（`AmountError` 此路不可达），其余异常照旧响亮——主单提成留 0、`review_status` 不动、完成事务**不回滚**（业务方拍板）；⚠️ 主单自己完成且组已齐（普通路）时 `CommissionError` **照旧整笔回滚**——吞只在**跨单**触发。算成 ⇒ 补一次主单预计跟随；日志落在主单名下（查「主单提成为什么是 0」的人直接看得到）。⭐ 第三个调用方（M3）：`recycle` 的 prune 到 1 收口——幸存单 `split_id` 已清、走正常单口径，语义同款（删除不被提成配置挡住）。
    """
    try:
        recompute_customer_commission(main, dry_run=False)
    except CommissionError as exc:
        record(
            log_type=ACTION_LOG_TYPES["order.split_main_commission_error"],
            action="order.split_main_commission_error",
            actor=SYSTEM_OPERATOR,
            # 自动触发的机器行：显式声明（`record()` 的 fail-closed 判据）。
            system=True,
            target_kind="order",
            target_id=main.order_no,
            order_no=main.order_no,
            reason=exc.message,
        )
        return
    refresh_customer_estimate(main)


def _settle_split_main_after_last_member(order) -> None:
    """**G12 组齐生效的触发点之一**：末单完成那一跳（`on_order_completed` 内、同事务）。本单是组内**非主单成员**、完成那一刻全组已 `COMPLETED` ⇒ 对主单做一次权威重算（走 `_recompute_main_commission_for_group` 的吞错语义）。⚠️ 本单**是**主单的那一跳不进这里（普通路，权威重算已在 `on_order_completed` 第二句发生过）；组未齐 ⇒ 什么都不做（主单 SALES 由组闸保持 0）。
    """
    if not split_group.is_legacy_member(order) or split_group.is_main_member(order):
        return
    if not split_group.group_all_completed(order):
        return
    main = split_group.main_member(order.split_id)
    if main is None:
        return
    main = split_group.lock_main_and_sales_nowait(main)
    _recompute_main_commission_for_group(main)


def on_order_completed(order, from_status):
    """订单 → `COMPLETED`：**确定生效**客服销售提成（spec §4.2 第二段），写 `bonus_amount` / `commission_rate` / `review_status` 三列（幂等：更新既有行，重复到达只覆盖同值）。⭐ **第一件事是补打手**（完成即生效），且必须在算客服提成与客服那一半的两个早退**之前**：① 减项——不补则打手按 `0.00` 算 ⇒ **客服多拿**（判别 = `test_performance_completion_activation.py::test_customer_commission_uses_the_activated_taker_amount`）；② 早退顺序——无客服行 / `sales_user_id is None` 两支原先直接 `return` 会跳过打手（判别 = 同文件 `TakerActivationOrderingTests`）。
    ⚠️ 调用**不接收返回值**（补打手是写入副作用，减项由 `recompute_customer_commission` 自己重查；⛔ 别改回 `taker = …`——改坏它没有任何用例会红）。利润用 `profit_of(order, 打手.bonus_amount)`（唯一算式；⛔ 别把「录单读提成率」加回去——建单会绑到薪酬配置）。提成率不可算抛 `CommissionError`（预期失败），端点按 E-0010 翻业务码、钩子不吞（D-0071 后 `/review/` 与 `/transition/` 两端点都可达）。`from_status` 刻意不用；D-0071：审核闸门取消、完成即生效（那道自加的闸会挡住钱）。无覆盖分支的完整行为表见 `docs/code-comment-notes.md` 本文件分节 ⇒ 没有一条会在完成这一步产生未捕获 500。
    """
    # 先补打手，再算权威提成，再刷预计。客服早退也不能跳过补打手。
    _activate_taker_performance(order)
    # ⭐ 完成时打手「完成即生效」⇒ 此刻客服预计与生效同基数同算式、值相同；把预计也刷成同一个值，
    #    完成那一刻两列不会各说一个数。⚠️ 位置在 `recompute_customer_commission` **之后**：那条是权威
    #    （算不出就抛、整笔回滚），这条只是跟随、⛔ 自己不抛 ⇒ 完成态的失败语义一个字未改。
    result = recompute_customer_commission(order, dry_run=False)
    refresh_customer_estimate(order)
    # ⭐ D-0357（G12/G15）：**末单完成那一跳**——本单是组内子单、完成那一刻全组已齐 ⇒ 主单客服提成跨单生效
    #    （吞 `CommissionError` + 响亮日志）；主单自己完成的那一跳不进这里（普通路，上面已权威生效）。
    #    ⚠️ 位置在 `refresh_customer_estimate` **之后**：那一步的 G14 跟随已把主单按组口径刷过，这里补权威三列。
    _settle_split_main_after_last_member(order)
    return result


def on_order_completed_ship(order, from_status):
    """订单 → `COMPLETED`：**自动发一次货**（闲管家「无需物流发货」；spec §3.4）；`from_status` 刻意不用。两条硬要求（spec §3.4）：① 发货失败不得让订单状态回滚（本钩子只**排**一次，异常翻译全在 `xgj_ship`，不抛业务异常）；② 不在数据库事务里干等外部 HTTP——真调用排在 `on_commit` 回调里（⭐ D-0358 起回调只入队、由本进程节流队列的 worker 出网：闸放行 ⇒ 排队；闸 hold（组未齐）/ 组幂等 ⇒ 零入队只留人可见订单日志；早期同步等上游被实测拖满 30 秒杀成 worker timeout，动因是批量验收 59 张挤同一秒被限流——读数登记外移）。闸 vs 删除路径的 AB-BA 残差已在两侧 docstring 登记（接受毫秒级跨族锁窗口，最坏一次响亮 500、重试即成）。
    ⚠️ 与 `on_order_completed` 的先后（注册处见文件末尾）**今天不改变可观测结果**（只排回调，算钱失败时回调被 Django 丢弃）；保留是为了将来有人把发货改成事务内同步调时不会得到「订单回滚了、货却已经发了」。幂等：重复到达时已有成功记录不重发（D7 只补日志），前次失败会再发一次。⭐ D-0357：入口换**发货闸** `schedule_auto_ship_gated`（非拆单属性短路直排；拆单组全组完成才排、未齐只留日志；组内已有 SUCCESS 只补日志不重发）。**完成流转零新增闸**（G5）：挡的只是「发货该不该排」，判别网 = `tests/test_split_ship_gate.py`。
    """
    schedule_auto_ship_gated(order)
    return None


def on_split_member_refunded_ship(order, from_status):
    """退单可能使已完成的剩余订单达到整组发货条件。"""
    if split_group.is_split_member(order):
        schedule_auto_ship_gated(order)


def _enqueue_withdraw_before_refund_clears(order, from_status):
    """订单 → `REFUNDED`：给当时的 MAIN 接手人记一条撤单事件（上下游事件；spec §16.2，原因取 `refunded`）。`from_status` 刻意不用（目标态钩子，与从哪来无关）。⚠️ 取人用**容错**查询（无 MAIN 行 / 那行无 `user` ⇒ 没有可通知的人、一笔不写）：⛔ 不用 `main_performance_of`——缺行即抛会让**退单**路 500（`on_order_refunded` 与回池钩子对缺行都是容错口径）。
    """
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    taker = perf.user if perf is not None else None
    if taker is not None:
        enqueue_order_withdrawn(order, taker, "refunded")
        # ⭐⭐ 打手侧回调线（T3，spec §4.3 表第 9 行后半）：同一位置、收件人是**前任**接手人——上面容错查询拿到的 `taker` 就是它。⛔ 不能改成现查一次 `current_taker_id`：退单钩子链里 `reset_taker_performance` 会把 MAIN 行 `user` 清空，那时查不到前任（要通知的正是前任）。时刻取 `timezone.now()`（打手侧不掺和 `order.updated_at` 默认值——那是 peer 接收侧 `stale` 判据要的）。
        from apps.takerapi.api import callback_recipients as taker_callback

        taker_callback.notify_order_reassigned_for_taker(
            order,
            former_taker_id=taker.pk,
            reason="refunded",
            occurred_at=timezone.now(),
        )
    return None


def unregister():
    """清掉本模块在 `COMPLETED` / `REFUNDED` 上注册的**全部**钩子（**测试清理用**；注册表是模块级的）。
    两个目标态都有生产钩子、必须一起清，否则模块级注册表会串到别的测试；探针挂在没有生产钩子的
    `ISSUE` 上避免误伤。"""
    clear_target_hooks(OrderStatus.COMPLETED)
    clear_target_hooks(OrderStatus.REFUNDED)


# ⚠️ 注册放在**模块导入时**，由 `apps.py::ready()` 保证本模块一定被导入——否则「钩子没注册」的静默漏副作用
#    （Ruling O #1：光有这行调用不算数）。
register_target_hook(OrderStatus.COMPLETED, on_order_completed)
# ⭐ 自动发货线（spec §3.4）按 spec 排在 `on_order_completed` 之后。⚠️ 如实登记：这个顺序**今天不改变可观测结果**
#    （只排 `on_commit` 回调，算钱失败时回调被 Django 丢弃）；保留它是为了将来有人把发货改成事务内同步调时，
#    不会得到「订单回滚了、货却已经发了」。判别用例：`tests/test_xgj_auto_ship.py::HookRegistrationTests`。
register_target_hook(OrderStatus.COMPLETED, on_order_completed_ship)
# ⚠️ 这两行的先后今天不改变结果：`_run_target_hooks` 按注册顺序跑，而 `on_order_refunded` 清的是 MAIN 行的两个**金额列**、保留 `user`（`reset_taker_performance` 全仓调用点只有 `performance._on_pool_entry` 与 `/assign/`）⇒ 上面那个钩子读「此刻仍挂在 MAIN 行上的接手人」拿得到。保留「读人的钩子排在清人的钩子之前」是**将来**防线：哪天某个 `REFUNDED` 钩子开始清接手人，排在后面只能读到 `None` ⇒ 一条不发且无报错。
register_target_hook(OrderStatus.REFUNDED, _enqueue_withdraw_before_refund_clears)
register_target_hook(OrderStatus.REFUNDED, on_order_refunded, wants_finance_policy=True)
register_target_hook(OrderStatus.REFUNDED, on_split_member_refunded_ship)
