"""退款：部分退款（客户退差价，spec §5.1 四步 + §4.5 冲减算式）与全额退款（订单进 REFUNDED 的绩效清空钩子，spec §5.2 第 0–4 步；P4-10）。为什么单独成模块：一次退款同时改三处（OrderRefund 事件行 / Order.refund_fee / 每条客服行的 PerformanceAdjustment）外加一条审计、它们之间没有任何数据库约束能表达联合一致性（spec §8）——不加事务失效模式是脏数据而不是报错；做法与 settlement.py 同形：领域逻辑收在一个函数里、视图只做「过序列化器→调它→把预期失败翻成业务码」。
三条入口共用本模块的 _gross_received（订单总价 total_price，D-0142）与 RefundError：refund_total_write（领域核心 order_writes.py，POST /orders/{no}/refunds，_write_refund_total 自己包事务）；refund_set_total（本模块公开 API，不再是 HTTP 端点直接被调方，tests/test_refund_full.py 等直接调它，同上）；on_order_refunded（状态迁到 REFUNDED，调用方包事务——apply_transition 的钩子契约）。⭐ 部分退款语义（D-0312）：入参是累计退款总额（绝对值）——填多少就是退了多少、填错能改回去；台账 OrderRefund 因此记带符号的差额 total − 旧累计（kind=SET）、refund_fee = Σ OrderRefund.amount 这条不变量逐字成立。⭐ A5（spec §D5 防漂移）：内存判据抽成模块级 refund_total_denial、commission_rate 闸抽成 assert_sales_commissions_configured、写入收在 _write_refund_total——refund_set_total 与 order_writes.refund_total_write 调的是同一批函数 ⛔ 判据只有一份（E-0021 的形状）。
⛔ 不新建第二套钩子机制：on_order_refunded 由 settlement_hooks.py（P4 唯一的钩子注册点）注册到 order_status._TARGET_HOOKS 的 REFUNDED 上、视图里不另调一遍（A4 的教训）。⚠️ P4-10：第 3 步的目标式偏离 spec §5.2 的字面公式（字面 = Σ未结账调整，而结账状态与 bonus_amount 不对称，见 on_order_refunded docstring「为什么偏离 §5.2 的字面公式」）——裁定 Z 的净额口径是整条退款冲减链（PARTIAL_REFUND + FULL_REFUND、与结账状态无关）、MANUAL 不参与；本次修复不碰 settlement_amount.payable_net。前置校验顺序（spec §5.1 表）：订单存在（视图判）→ status==COMPLETED(1007) → refund_total >= 0（0=清空合法，否则 1008）→ refund_total <= total_price(1008) → request_id 未出现过（仅当差额≠0 真要 INSERT 时，1010）。差额=0 不是一条独立判据而是全空操作（不建行不写审计不改列、changed=false），同一 request_id 在差额为 0 时重提不会回 1010。⭐ 第 5 条为什么不做 exists() 预查：判据是「具名唯一约束 uniq_order_refund_request_id 在 create() 时拒掉」这一条路径——spec §9 #5 的判别力实验要求「临时去掉那处唯一约束 ⇒ 测试必须变红」，先做 exists() 预查会让实验不会红（假保证）；预查也挡不住并发、真正兜底的只有约束（IntegrityError 是唯一的 1010 来源，_is_duplicate_request_id 认名字、不把别的完整性约束也翻译成「重复提交」）。§4.5 四条写死的细节：①已结账的客服行照旧建冲减、未结账的由 bonus_amount 重算体现（refresh_customer_estimate 回 None 算不出时必须回落到建冲减，否则这类客服退款一分不减）；②打手行一条都不碰（查询固定 emp_type=SALES，有意的不对称——打手绩效交单时定死、退款只冲客服提成）；③commission_rate 为空 ⇒ 拒绝整笔退款（1009）不静默按 0 冲减（无声的错账；⚠️ 正常路径不可达——部分退款要求 COMPLETED 而 commission_rate 正是 COMPLETED 时写的 ⇒ 测试必须直接 update(commission_rate=None) 构造；⚠️ 与 commission.commission_percent_of 的 CommissionError 不是一回事：这里判的是客服行的列本身为空）；④一条客服行都没有的订单退款照常成功（钱确实退给客户了、账必须记）。冲减算式一律 ×rate/100、复用 performance_amount.commission_amount 不另写一份 /100（「一处乘一处除」的量纲不一致出过真错）。部分退款后订单保持 COMPLETED（spec §10 #11）。⛔ 本模块不给金额列加任何 ≥0 校验（D-0015）：refund_total >= 0 是请求形状规则、PerformanceAdjustment.amount 正是负的、OrderRefund.amount 也允许负数（差额台账）。"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import (
    EmpType,
    LegacySplitRefundPolicy,
    Order,
    OrderRefund,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.performance_amount import DEDUCT_FIXED, commission_amount
from apps.business.settle_requests import cancel_pending_settle_requests
from apps.core.error_codes import ErrorCode

#: 幂等键撞车时认的那处**具名**唯一约束（`OrderRefund.Meta.constraints`，spec §3.2 逐字指定）。
DUPLICATE_CONSTRAINT = "uniq_order_refund_request_id"


class RefundError(Exception):
    """退款的预期失败（状态不允许 / 金额非法 / 提成率未配 / 幂等键重复）。
    形状与 AmountError / SettleError / CommissionError 同构：code 直接进信封当业务码、message 直接当 msg（给中文界面显示）。
    在 atomic() 块内抛它 ⇒ 随异常整笔回滚（不是 set_rollback + 裸 return 那条补偿路径，见 E-0022）。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def _raise_financial_transition_required():
    raise RefundError("订单涉及财务调整，请到团团通原入口处理", ErrorCode.BUSINESS)


def _is_duplicate_request_id(exc):
    """IntegrityError 是不是那处具名唯一约束造成的。
    ⚠️ 不能把「是 IntegrityError」当成「就是重复请求」：OrderRefund 上还有 NOT NULL / FK 等别的完整性约束、
    把它们也翻译成 1010 会把「写坏了」伪装成「你重复提交了」（E-0010 的同族：预期失败要翻译、未预期的不能伪装）。"""
    return DUPLICATE_CONSTRAINT in str(exc)


def _gross_received(order):
    """毛收款 = 订单总价 total_price（业务方裁定，D-0142；spec §4.2）。⛔ 不读 received_price（接单实收款）：录单页那个隐藏框把它写成 0（display:none + value="0"，C10）、当毛收款用会把每一张新单算成 0；⛔ 不读 hall_price：那是给打手的价格（D-0081 的裁定理由不变）。⚠️ 本函数不会失败：total_price 恒有值（null=False default=0.00）。
    连带的恒等式（已量化条件，A-Minor-4）：在两位小数的订单行上（total_price / refund_fee 都是 DecimalField(12,2)）毛收款 − refund_fee == settlement_amount.net_received(order)（绝对值口径下退款上限就是毛收款本身，见 refund_total_denial；净实收的恒等式仍由 tests/test_p4fu_price_fallback.py 钉住）。
    ⛔ 对未落库、带三位以上小数的 Order 实例不成立：net_received 会 quantize(CENT, ROUND_HALF_UP) 而本函数不量化（例：total_price=Decimal("1.005") ⇒ 本函数 1.005、net_received 1.01）；原句未量化入参、按实现补上这个条件（E-0011）。"""
    return Decimal(order.total_price)


def assert_sales_commissions_configured(order, *, unfinished=False, lock=False, refunded=False):
    """每条（未软删的）客服行的 commission_rate 都非空；有空的 ⇒ 抛 1009。通过 ⇒ 返回那些客服行的 queryset（供调用方复用同一份过滤条件 ⛔ 不再拼一次）。
    ⭐ 这是「commission_rate 为空 ⇒ 拒绝整笔退款」这条判据的唯一出处（spec §4.5 细节 3；编排方 Ruling 10）：_apply_sales_deductions 在循环之前调它一次、refund_total_denial 也调它一次（判据与写入共用同一份过滤条件）——⛔ 不许在任一处再写一份 if performance.commission_rate is None；⚠️ 顺带把那条查询（emp_type=SALES + order__deleted_at__isnull=True）也收在这里：两处各拼一次会让「已软删单的客服行算不算」有两个答案（读路径网 C22 就是钉这个的）。
    为什么从「循环里逐行判」改成「循环前查一遍」是行为等价的：真写路径的写入整个在 _write_refund_total 的 transaction.atomic() 里、而本函数只读不写——「循环前抛」⇒ 该事务里还什么都没写、异常冒出 atomic() ⇒ 空回滚；修前「循环到第 k 行才抛」⇒ 前面 k−1 条 PerformanceAdjustment 已写进同一个事务、但异常同样冒出那个 atomic() ⇒ 那 k−1 行随事务一起回滚、可观测结果是同一个（库里一行不剩、refund_fee 未变、审计未写、响应同一个 1009 + 同一句 msg）。⚠️ 等价性依赖「这一遍在任何写入之前、且异常最终冒出同一个 atomic()」：它是判据（在任何写入之前跑一次）、所以本函数也可以被 dry_run 分支（事务之外）调用；反过来说若将来有人把真写路径改成「块内裸 return」而不 set_rollback（E-0022）两者就不再等价——那时循环前的这一遍会变成「明明还没写却已经报 1009」而循环里那一遍不会。"""
    # ⭐ D-0144（C22）：已软删单的客服行**不冲减**，因此也**不参与**这条判据
    #    （跨关系 JOIN 不经 `Order` 的默认 manager ⇒ 显式补 `order__deleted_at__isnull=True`）。
    rows = Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    )
    if lock:
        rows = rows.select_for_update(of=("self",)).order_by("id")
    for performance in rows:
        if not refunded and performance.commission_rate is None and (not unfinished or performance.is_settled):
            raise RefundError(
                f"客服行（绩效 {performance.id}）没有配置提成率，不能冲减退款",
                ErrorCode.COMMISSION_NOT_CONFIGURED,
            )
    return rows


def _create_sales_deduction(performance, refund, delta, actor):
    """建一条「退款差额 ⇒ 客服提成调整」的带符号记录（已结账行与回落两条路共用）。
    ⚠️ commission_amount 就是 ×rate/100 + 量化到分（复用、不另写一处 /100）。⭐ 绝对值口径：喂进来的是差额（可正可负）⇒ 这里取负号——差额为正（本次调高累计退款）⇒ 负调整（冲减）；差额为负（本次调低累计退款，例如 500→50）⇒ 正调整 = 退还上次多扣的提成；commission_amount 对负数不设闸（纯乘法+量化）、这条符号翻转有用例钉住（tests/test_refund_partial.py 的「已结算 + 往下改」那一组）。
    ⚠️ 新调整记录自身恒为「未结账」：原行已结账也照建、这笔增减进入下一次结账（spec §3.3 / §4.5 细节 1 的另一半）。"""
    return PerformanceAdjustment.objects.create(
        performance=performance,
        refund=refund,
        kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
        amount=-commission_amount(delta, performance.commission_rate),
        reason=f"退款差额 ¥{delta}：利润差额 {delta} × 提成率 {performance.commission_rate}%",
        operator=actor or "",
        settle_status=SettleStatus.UNSETTLED,
    )


def _apply_sales_deductions(refund, order, delta, actor, *, unfinished=False):
    """给该订单的客服行建带符号的调整记录（spec §4.5）；返回推迟到刷新之后的那些行。
    ⭐ P0-2：按「该行自己的 settle_status」分两档建——不按它过滤正是「同一笔退款扣两次」的来源：已结账 ⇒ 当场建冲减（不进返回值；钱已经发过、bonus_amount 冻结不动、退款只能以新调整记录的身份进下一次结账，细节 1）；未结账 ⇒ 只记下来不建（进返回值；同一笔退款在 _write_refund_total 末尾会被 refresh_customer_estimate 按新利润重算进 bonus_amount、再建一条就是扣两次）。
    ⚠️ 回落的义务在调用方（_write_refund_total 第 5 步之后）：refresh_customer_estimate 回 None（算不出，典型是客服档案 FIXED 模式）⇒ 对它返回的这份名单逐行建冲减（否则这类客服退款一分不减）；⛔ 本函数不自己判断「算不算得出」——那要读刷新结果、只有调用方手里有。⚠️ 判据是该行自己的 is_settled ⛔ 不是 MAIN 那道闸（SALES 行可以独立已结账）；⚠️ 查询固定 emp_type=EmpType.SALES（细节 2：打手行一条都不碰）；⚠️ commission_rate 为空 ⇒ 抛 1009 让整笔退款回滚（细节 3：不静默按 0）——该判据已抽成模块级 assert_sales_commissions_configured(order)、在下面循环之前调一次并复用它返回的 queryset（过滤条件只拼一处 ⛔ 循环里不再逐行判）。"""
    deferred = []
    # ⭐ D-0144（C22）：已软删单的客服行不冲减 ⇒ 那一份过滤条件在
    #    `assert_sales_commissions_configured` 里（下面直接遍历它返回的行集）。
    for performance in assert_sales_commissions_configured(order, unfinished=unfinished, lock=unfinished):
        if performance.is_settled:
            _create_sales_deduction(performance, refund, delta, actor)
        else:
            deferred.append(performance)
    return deferred


def _external_settlement_pending(order):
    from apps.business import split_group

    if order.status != OrderStatus.COMPLETED:
        return True
    return split_group.is_legacy_member(order) and (
        not split_group.is_main_member(order) or not split_group.group_all_completed(order)
    )


def external_refund_history_denial(order):
    from apps.business import split_group

    if split_group.is_legacy_member(order):
        main = split_group.main_member(order.split_id)
        if main and split_group.legacy_children_with_historical_deductions(order.split_id, main.pk):
            raise RefundError('旧式拆单子单存在历史退款提成调整，请人工核对', ErrorCode.BUSINESS)


def refund_total_denial(order, refund_total, *, allow_external_refund=False, lock=False):
    """前置校验 #2/#3/#4 + 客服提成率闸（全在内存里）：不满足 ⇒ raise RefundError(msg, code)；通过 ⇒ 返回已 Decimal 化的累计退款总额（供写入步骤复用、避免转两次）。
    ⭐ 这是这几条判据的唯一出处（spec §5.1 的表；编排方 Ruling 10）：refund_set_total（单笔）与 order_writes.refund_total_write（核心/批量）都调它 ⛔ 不许任一处再写一份。
    ⚠️ 判的是「新累计值」而不是「本笔差额」（D-0312）：0 是合法输入（= 清空累计退款）、负数才拒；上限是毛收款 total_price（不是「剩余可退额」——差值由写入函数在行锁内算 ⛔ 这里不读 refund_fee）。⚠️ Decimal(refund_total) 的转换时机照搬（旧 refund_denial 是「进函数第一句就转」）：转完再比大小；decimal.InvalidOperation 仍是未预期异常（序列化器已经挡过非数字、正常 HTTP 路径到不了）。⚠️ 客服提成率闸（1009）也在这里跑：它只读不写、dry_run 与真写共用同一份。"""
    # 企微自动同步记录实际退款；人工入口仍限定完成状态和订单总价。
    refund_total = Decimal(refund_total)
    unfinished = allow_external_refund and _external_settlement_pending(order)
    if order.status != OrderStatus.COMPLETED and not unfinished:
        raise RefundError(
            f"订单当前状态为「{order.get_status_display()}」，只有「已完成」的订单可以设置累计退款额",
            ErrorCode.REFUND_STATE_INVALID,
        )
    if refund_total < 0:
        raise RefundError("累计退款金额不能为负数", ErrorCode.REFUND_AMOUNT_INVALID)
    gross = _gross_received(order)
    if refund_total > gross and not allow_external_refund:
        raise RefundError(
            f"累计退款金额超过订单总价（订单总价 {gross}，累计退款 {refund_total}）",
            ErrorCode.REFUND_AMOUNT_INVALID,
        )
    assert_sales_commissions_configured(
        order, unfinished=unfinished, lock=lock and allow_external_refund,
        refunded=allow_external_refund and order.status == OrderStatus.REFUNDED,
    )
    return refund_total


def _write_refund_total(order, refund_total, reason, request_id, actor, actor_id=None, *, allow_external_refund=False):
    """事务内改累计退款额（绝对值）、返回新建的 OrderRefund；差额为 0 返回 None。
    时序写死（B2）：with transaction.atomic(): row = lock_order_row(order) or order; old = row.refund_fee; delta = total - old; …后续判据/台账/落库/审计/冲减全部用 row。⚠️ 旧值必须在行锁之后读：绝对值语义下「读改写」必须串行化、否则两个并发请求各读旧值各写自己的 total、Σ OrderRefund.amount 与 refund_fee 就会对不上；锁形状只此一处（order_writes.lock_order_row ⛔ 不手写第二份 select_for_update）；批量侧 run_batch 已先锁过——同事务内再加同向锁合法、锁序不变。
    差额 = 0 是全空操作（B1、响应契约见视图）：不建 OrderRefund、不写审计、不建调整、不改列、直接返回 None ⇒ 同一 request_id 再提不会撞唯一约束（1010 只在 INSERT 时发生）。台账记带符号差额（不是 total）：OrderRefund(kind=SET, amount=delta) 才保住 refund_fee = Σ OrderRefund.amount；客服调整按 delta 的方向走（负数 ⇒ 正调整，见 _create_sales_deduction）。三条事务路径：①成功 ⇒ atomic() 提交、返回 refund 新行；②幂等冲突（1010）⇒ 嵌套 atomic()（savepoint）回滚那条 INSERT、异常被内层 except 翻成 RefundError 并向上抛 ⇒ 外层整笔回滚；③判据失败（1007/1008/1009）⇒ RefundError 冒出 atomic() ⇒ 一行都没写。"""
    with transaction.atomic():
        # ⚠️ 局部导入：`order_writes` 在模块顶层 import 本模块的判据与写入（反向依赖），
        #    顶层互相 import 会成环；锁形状仍只有 `order_writes.lock_order_row` 一处。
        from apps.business.order_writes import lock_order_row
        from apps.business import split_group

        row = lock_order_row(order) or order
        settlement_order = row
        if allow_external_refund and split_group.is_legacy_member(row) and not split_group.is_main_member(row):
            main = split_group.main_member(row.split_id)
            if main is not None:
                settlement_order = lock_order_row(main) or main
        if allow_external_refund:
            external_refund_history_denial(row)
        if Performance.objects.filter(
            order_id=settlement_order.pk, online_payroll_batch__isnull=False, settled_at__isnull=True,
        ).exists():
            raise RefundError("订单有正在处理的在线付款，暂时不能退款", ErrorCode.BUSINESS)
        # 判据打在**加锁后读回的行**上（状态 / 总价都是最新值，避免 TOCTOU）。
        refund_total = refund_total_denial(row, refund_total, allow_external_refund=allow_external_refund, lock=True)
        unfinished = (allow_external_refund or settlement_order.pk != row.pk) and _external_settlement_pending(settlement_order)
        if settlement_order.pk != row.pk:
            assert_sales_commissions_configured(settlement_order, unfinished=unfinished, lock=True)
        old_refund_fee = Decimal(row.refund_fee)
        delta = refund_total - old_refund_fee
        if delta == 0:
            # 全空操作：把调用方实例也同步成库里的真值（视图/调用方可能拿它拼响应）。
            order.refund_fee = refund_total
            return None
        if settlement_order.pk != row.pk:
            policy, _ = LegacySplitRefundPolicy.objects.get_or_create(order=row, split_id=row.split_id)
            policy.refund_total = refund_total
            policy.save(update_fields=['refund_total'])
        try:
            # 第 1 步：建 `OrderRefund`——`request_id` 的唯一约束在此兜底幂等（前置校验 #5）。
            # ⚠️ 嵌套 `atomic()` = savepoint：`IntegrityError` 在这里被接住，若不回到
            #    savepoint，外层事务会被标记成「必须回滚」⇒ 后面每一句 SQL 都 25P02。
            with transaction.atomic():
                refund = OrderRefund.objects.create(
                    order=row,
                    kind=OrderRefund.Kind.SET,
                    amount=delta,
                    reason=reason,
                    operator=actor or "",
                    request_id=request_id,
                )
        except IntegrityError as exc:
            if _is_duplicate_request_id(exc):
                raise RefundError(
                    "该退款请求已处理过（request_id 重复）",
                    ErrorCode.REFUND_DUPLICATE_REQUEST,
                ) from exc
            raise
        # 第 2 步：`refund_fee = total`（**绝对值**，⛔ 不再 `F() + amount`）。
        # 行锁在手 ⇒ 读改写安全；`save(update_fields=…)` 只碰这一列（`updated_at` 不动，
        # 与旧的 `.update()` 同形）。
        row.refund_fee = refund_total
        row.save(update_fields=["refund_fee"])
        if not allow_external_refund:
            from apps.wecom.external_pay import enqueue_order_refund_recheck
            enqueue_order_refund_recheck(row)
        # 第 3 步：客服行的带符号调整记录（细节 1–4 全在里面、含共用的 assert_sales_commissions_configured）。
        # ⭐ P0-2：已结账的行在这一步当场建；未结账的行只被记下来（返回值就是那份名单）、由下面第 5 步的刷新结果决定要不要回落。
        # 退单钩子已清算客服绩效，额外冲减会重复扣款。
        if allow_external_refund and settlement_order.status == OrderStatus.REFUNDED:
            deferred = []
        else:
            deferred = _apply_sales_deductions(refund, settlement_order, delta, actor, unfinished=unfinished)
        # 第 4 步：退款留痕（from/to 是 refund_fee 的旧/新累计额）。⭐ M3a：旧出口 log_refund 已迁到唯一写入出口 record(...)（spec §7.1）。
        # 退款是订单级事件（不针对某一条绩效）⇒ target_kind="order"、target_id = 订单号（OperationLog 没有 performance_id 列、也不该有：legacy 的 log_refund 本来就不接 performance_id、该列留 NULL）。
        record(
            log_type=ACTION_LOG_TYPES["order.refund"],
            action="order.refund",
            actor=actor,
            actor_id=actor_id,
            # ⚠️ 值级的机器来源：actor_id 默认 None（refund_set_total 的兼容形参；生产 HTTP 单笔/批量两条路都从认证态把主键传下来：_write_context 与 refund_views 的 WriteContext）。
            # 拿不到主键的调用（今天只有直接调 refund_set_total 的用例/探针）⇒ 显式声明机器来源 ⛔ 不静默通过。
            system=actor_id is None,
            target_kind="order",
            target_id=row.order_no,
            order_no=row.order_no,
            from_value=old_refund_fee,
            to_value=refund_total,
            reason=reason,
        )
        # ── 第 5 步（⭐ 业务方口径 Q2）：退款 ⇒ 客服行的「预计」与「生效」一起按新的净实收重算（只对未结账的行、闸门在 refresh_customer_estimate 里逐行判 is_settled）。
        # 为什么必须有这一步：第 2 步改了 refund_fee、而「净实收 = total_price − refund_fee」正是那个算式的输入 ⇒ 不刷就会出现「钱退了一部分、客服两列还按退前的净实收」的陈旧值。
        # ⚠️ 这里不需要旧口径那句 refresh_from_db()：第 2 步是 save()、内存实例与库里同值（旧实现用 F() 才必须回读）；⚠️ 位置在事务之内（第 4 步之后、atomic() 出口之前）：命中「算不出」时 refresh_customer_estimate 自己吞掉、但 DB 层故障照旧让整笔退款回滚（E-0022：块内没有裸 return）。
        from apps.business.settlement_hooks import refresh_customer_estimate

        refreshed = refresh_customer_estimate(settlement_order)
        # ── 第 6 步（⭐ P0-2 的回落）：刷新算不出 ⇒ 给刚才推迟的那些行建回落调整。判据 = refresh_customer_estimate 回 None：它算不出时（典型是客服档案 FIXED 模式 ⇒ commission_percent_of 抛 CommissionError 被它吞掉）一行都不写 ⇒ 这些行的差额既没进 bonus_amount、也没有调整记录 ⇒ 不回落的话这笔退款一分不减（错钱的方向之一）。
        # ⚠️ 返回非 None ⇒ 未结账行的差额已经落在 bonus_amount 上 ⇒ ⛔ 再建一条就是 P0-2 那个「同一笔扣两次」。
        # ⚠️ 前提（如实登记）：本单至多一条未结账客服行——refresh_customer_estimate 只刷 .first() 那一行、而全仓建客服行只有 create_customer_performance 一个入口、每单至多一条；若将来出现多条、本条要改成逐行判「刷的是不是它」；⚠️ 位置仍在事务之内（atomic() 出口之前）：与第 3 步同生共死。
        # 未完成订单之后会按净实收结算，无须提前补一条扣减。
        if refreshed is None and not unfinished:
            for performance in deferred:
                _create_sales_deduction(performance, refund, delta, actor)
    order.refund_fee = refund_total
    return refund


def refund_set_total(order, refund_total, reason, request_id, actor, actor_id=None):
    """把 order 的累计退款额设成 refund_total（绝对值）；返回新建的 OrderRefund。
    refund_total 等于当前累计 ⇒ 返回 None（全空操作，见 _write_refund_total）；预期失败抛 RefundError。前置校验 #1（订单存在）由调用方（视图）在取订单时判——本模块收到的是已存在的 Order 实例、所以这里从 #2 开始。
    ⚠️ actor_id（M3a 追加、默认 None）只用于审计行的 actor_id 列（spec §5.9：「操作人一律取认证态、actor_id 同步取 request.user.pk」）：认证态的用户名由视图给 actor、主键顺手带上；默认值让既有的位置参数调用形状不变。⭐ A5 起本函数是「共用判据 + 共用写入」的薄壳（spec §D5 的防漂移）：内存判据在 refund_total_denial、写入在 _write_refund_total、commission_rate 闸在 assert_sales_commissions_configured——领域核心 order_writes.refund_total_write 调的是同一批函数（不是复制一份判据）。"""
    return _write_refund_total(
        order, refund_total, reason, request_id, actor, actor_id=actor_id
    )

#: 全额退款的原因文案（第 3 步那条目标式冲减记录的 `reason`）。
#: ⚠️ **D-0181 后它只服务这一处**：退单不再建 `OrderRefund(kind=FULL)`
#: 事件行、也不再写 `order.refund` 审计（原第 0/1/4 步已整体删除，理由见`on_order_refunded` 的 docstring）。
FULL_REFUND_REASON = "全额退款清空绩效"

#: ⭐ 自动入账的工资在退单时的**回冲**调整。
#: 这笔钱在验收时已经打进打手钱包（`accounts.payout`）⇒ 退单时必须同事务扣回来。
#: ⚠️ `settle_status` 直接落「已结账」：这笔钱**已经通过钱包实际收回**，不能再进结算批（否则它会以一条「未结账调整」的身份永远留在队列里，甚至会因为 `payable_net` 为负让这个人的整批结算 `1011` 结不掉）。
PAYOUT_CLAWBACK_REASON = "退单回冲自动入账的工资"
#: 回冲调整的幂等键前缀（`PerformanceAdjustment.request_id` 唯一约束兜底）。
#: ⭐ **P0-3：键 = 前缀 + `{绩效主键}-{settled_at:%Y%m%d%H%M%S%f}`**，
#: 即**这一轮结算的身份**——同一轮重试 ⇒ 同键 ⇒ 幂等；「退单 → 回池 → 重做完成」是新一轮、新 `settled_at` ⇒ 新键 ⇒ **会再回冲一次**。⛔ 不要退回「前缀 + 单号」：那让同一张单的**第二次**退款命中旧键、直接返回（钱包留着第二轮的钱，P0-3 的实测就是这个形状）。
PAYOUT_CLAWBACK_REQUEST_PREFIX = "PAYOUT-CLAWBACK-"


def _claw_back_auto_paid_taker(order, operator, *, allow_financial_writes=True):
    """退单时把自动入账的打手工资从钱包扣回来、并复位该行的结账痕迹。返回写下的 PerformanceAdjustment；不需要回冲（未结账 / 非自动入账 / 金额 ≤ 0）⇒ None。
    幂等键 = 这一轮的结算身份（⭐ P0-3）：request_id = PAYOUT-CLAWBACK-{perf.pk}-{settled_at:%Y%m%d%H%M%S%f}（16 位前缀+主键+“-”+20 位时间戳 ≤ 56 字符、列宽 64）。同一轮的重试 ⇒ settled_at 没变 ⇒ 同键 ⇒ exists() 挡住第二次；「退单 → 回池 → 重做完成 → 再退单」是新一轮、新的 settled_at ⇒ 新键 ⇒ 会再回冲一次（修前用「前缀+单号」⇒ 第二次退款的键与第一次相同、exists() 命中即返回 ⇒ 钱包留着第二轮那笔钱，P0-3 的实测）。⚠️ 键必须在读了本行 settled_at 之后生成、且读之前先 select_for_update() 锁住这一行——锁序 = 调用方已持有的订单行锁 → 本绩效行（与 accounts.payout.payout_completed_order 的 Order → Performance 同序 ⛔ 不要反过来）；⚠️ 本函数要求 settled_at 非空：生产上把「已结账 + settled_by="system"」写得出来的只有 payout_completed_order（它四列同写）；脏行（settle_status=已结账而 settled_at 为空）会在格式化那一句响亮报错（E-0022：不可预期的错误不许伪装成「没什么要回冲的」）。
    为什么必须有这一步：钱包作为发薪唯一出口之后、apply_transition(→ COMPLETED) 会把 MAIN 行标成「已结账」并把钱打进钱包；退单如果只清 bonus_amount（第 2 步）而不回冲、打手会白拿这笔钱——「客户退了、打手照拿」是错账；规格 §1.3 的「调整冲账」落在这里：写一条带符号的 PerformanceAdjustment、由 apps.accounts.payout 的 post_save 接收器同事务扣钱包。只回冲 settled_by == "system" 的行：规格 §3 明写「存量已结算的单不回冲（历史工资是财务手工付的）」——那些行的钱从来没进过钱包、扣钱包就是凭空多扣；判据因此是行的结账来源、不是「有没有结账」。复位结账痕迹（settle_status/settled_at/settled_by/settled_amount）：钱已经退回 ⇒ 这一轮不再算结过账；不复位有两个后果（都实测得到）：①settled_pool_entry_denial（R-10a）会把「退单 → 回池」挡死、重做那一轮永远走不了；②重做完成时 _activate_taker_performance 的「已结账 ⇒ 不做」会把 bonus 停在旧值；⚠️「原来结了多少」的证据不靠这一列：回冲那条调整 + 它的 WalletTransaction（ref_no = A<调整id>）都在。"""
    perf = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).first()
    if perf is None or perf.user_id is None:
        return None
    # ⭐ P0-3：**先把这一行锁住再看结账四列**——幂等键要拿 `settled_at`，
    #    而它必须与「这个键属不属于这一轮」在同一个事务里成立（锁序见 docstring：
    #    订单行锁已由 `apply_transition` 持有 ⇒ 这里是 Performance 行）。
    perf = Performance.objects.select_for_update().get(pk=perf.pk)
    if perf.settle_status != SettleStatus.SETTLED:
        return None
    if perf.settled_by != "system":
        return None
    if perf.settled_amount is None:
        return None
    amount = Decimal(perf.settled_amount)
    if amount <= 0:
        return None
    if not allow_financial_writes:
        _raise_financial_transition_required()
    # ⭐ 键 = 这一轮结算的身份（见 docstring）：同轮重试同键、新一轮新键。
    request_id = (
        f"{PAYOUT_CLAWBACK_REQUEST_PREFIX}{perf.pk}-"
        f"{perf.settled_at:%Y%m%d%H%M%S%f}"
    )
    if PerformanceAdjustment.objects.filter(request_id=request_id).exists():
        return None
    adjustment = PerformanceAdjustment.objects.create(
        performance=perf,
        refund=None,
        kind=PerformanceAdjustment.Kind.FULL_REFUND,
        amount=-amount,
        reason=f"{PAYOUT_CLAWBACK_REASON}：{amount}",
        operator=operator,
        # ⚠️ 直接落「已结账」：钱已经通过钱包实际收回，不能再进结算批（见常量注释）。
        settle_status=SettleStatus.SETTLED,
        settled_at=timezone.now(),
        request_id=request_id,
    )
    # ⚠️ 复位必须排在**创建调整之后**：钱包信号判的是「该绩效行此刻已结账」，
    #    先复位会让那笔回冲被静默跳过（钱扣不回来）。
    Performance.objects.filter(pk=perf.pk).update(
        settle_status=SettleStatus.UNSETTLED,
        settled_at=None,
        settled_by="",
        settled_amount=None,
    )
    return adjustment

#: F-1：「退单清空前接单人」那条留痕的 `reason` 前缀。
#: ⭐ **两个写入点共用同一个前缀**——主留痕在本模块的 `on_order_refunded`
#: （第 2 步清零**之前**，即那里的「第 1.5 步」），兜底在`performance._record_taker_before_pool_entry`（服务「绕开退单钩子」的老数据），兜底靠这个前缀判「该绩效行上已经有过这条留痕了」。⛔ **不要把字面量抄成两份**：前缀一旦漂移，兜底就会在正常链路上写出第二条（同一事实两条记录），而那正是「两套机制各自漂移」的形状。
TAKER_TRACE_PREFIX = "退单清空前接单人："

#: ⭐ Major-3：「退单清空**客服行**」那条留痕的 `reason` 前缀。
#: 与 MAIN 侧 `TAKER_TRACE_PREFIX` **同形**（`kind=MANUAL` / `amount=0.00` / `refund=None` /
#: `settle_status=未结账`），落在 `_clear_unsettled_sales_amounts` 里、清 0 **之前**。目的：让「原来 `8.00` → 现在 `0.00`」这件事在**数据库行**上查得到（MAIN 侧早有这条留痕，SALES 侧此前**一条都没有**）。
SALES_TRACE_PREFIX = "退单清空客服行："

# ── P4-M2（业务方选定候选 A）：退单回池时冲销上一轮 ──────────────
#: 冲销事件（`OrderRefund(kind=REVERSAL)`）的幂等键前缀。
#: 形状与已删除的 `FULL_REFUND_REQUEST_PREFIX` 同构、但**前缀不同**（`FULL-` vs `REVERSAL-`）⇒ 两者**不可能**相撞：第一次 `REVERSAL-<单号>`，第 n 次（n ≥ 2）`REVERSAL-<单号>-<n>`（20 位单号 + 9 + 1 + 至多几位 ≪ 64）。
REVERSAL_REQUEST_PREFIX = "REVERSAL-"

#: 冲销事件行的 `reason`（第 1 步的 `OrderRefund` 与它的审计行**共用一处字面量**）。
REVERSAL_REFUND_REASON = "退单回池冲销上一轮退款"

#: 客服行反向调整的 `reason`（第 3 步的 `PerformanceAdjustment` 与它的审计行共用）。
REVERSAL_DEDUCTION_REASON = "退单回池冲销上一轮退款冲减"


def _record_taker_trace_before_clearing(order, operator):
    """F-1 主留痕：把清零之前的原接单人与真实原金额写成一条 MANUAL 调整记录。返回写下的行；没有可留痕的人（没有 MAIN 行，或它的 user 为空）⇒ None。
    ⭐ 为什么必须由本模块写、且必须在第 2 步之前（P4-M1 实测）：on_order_refunded 的第 2 步把 MAIN 行的 temp_amount/bonus_amount 清了 0.00、而回池（performance._on_pool_entry）发生在那之后 ⇒ 在回池这个落点读永远是 0.00；「原金额」这个需求与「落点选在清零之后」在时序上矛盾、所以主留痕挪到清零之前。
    写入形状（spec §5.2 预案② + P4-M1 brief 的逐项指定）：kind=MANUAL / amount=Decimal("0.00") / refund=None / settle_status=未结账 / operator = 本钩子已有的那一个（order.updated_by）。⚠️ 只在这一次是「真实的退单迁移」时才写（调用点的 not is_rerun）——手工重跑不是一次新的退单动作、也就不该再留一条痕；这条判据打在 from_status 上（真实迁移一定不是 REFUNDED，见 on_order_refunded）。⛔ 本函数不改任何既有行为：第 2 步照旧清零、reset_taker_performance 照旧清 user。"""
    # ⭐ D-0144（C23）：已软删单的原接单人**不留痕**（同一处 join 条件）。
    performance = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).first()
    # ⚠️ 没有接单人 ⇒ 不写（写一条「接单人 None」只会给结算列表添噪声；与兜底同判据）。
    if performance is None or performance.user_id is None:
        return None
    # ⚠️ 必须在第 2 步清零**之前**取值（本函数由调用点放在那之前），并当场 `Decimal` 收口。
    amount = Decimal(performance.bonus_amount)
    return PerformanceAdjustment.objects.create(
        performance=performance,
        refund=None,
        kind=PerformanceAdjustment.Kind.MANUAL,
        amount=Decimal("0.00"),
        reason=f"{TAKER_TRACE_PREFIX}{performance.user.username} / 原金额 {amount}",
        operator=operator,
        settle_status=SettleStatus.UNSETTLED,
    )


def _next_reversal_ordinal(order):
    """本次冲销事件的序号 n（第 1 步 request_id 的后缀）。
    ⛔ 与已删除的 _next_full_refund_ordinal 同形、也同样只服务「不并发」这个前提（D-0181 之后全仓只剩这一处 count + 1 派生）：count + 1 恒大于任何已有序号 ⇒ 每次真事件都派生出一个新键；重跑不靠这个序号兜底——重跑由「余额已经是 0」在 reverse_previous_round_refund 的开头挡掉（见那里的幂等小节）。"""
    return (
        OrderRefund.objects.filter(
            order=order, kind=OrderRefund.Kind.REVERSAL
        ).count()
        + 1
    )


def _reverse_sales_refund_deductions(order, operator, *, actor_id=None, allow_financial_writes=True):
    """第 3 步：把每条客服行「上一轮退款冲减链」的余额等额反向记一条调整。amount = −Σ(该行 kind ∈ {PARTIAL_REFUND, FULL_REFUND} 的全部调整)；余额已经是 0（本来就没冲减过，或本函数已经跑过一次）⇒ 不写（避免噪声行）。
    ⚠️ 为什么这一条必须存在（P4-M2 探针实测）：Performance.bonus_amount 是一条列、第二轮 COMPLETED 时被 settlement_hooks._persist_customer_commission 覆盖写（实测 8.00 → -12.00、不是累加）、而第一轮退款留下的 FULL_REFUND(-8.00) 仍在链上 ⇒ 不反向就又冲掉一次新一轮赚到的提成、净额被静默算成 0.00（钱少给、不是报错）。⚠️ kind 为什么是 FULL_REFUND 而不是 MANUAL（⭐ 本节与 brief 的字面写法不同、理由与实测见 reverse_previous_round_refund 的 docstring）：MANUAL 被裁定 Z 刻意排除在目标式之外 ⇒ 写 MANUAL 会让下一次退单再也冲不掉新一轮的提成（客户的钱退光了、客服却留着 8.00——算错钱的方向）；反向记录必须留在链上。
    refund=None（它不对应某一次新的退款事件）、settle_status=未结账（进入下一次结账）；⚠️ D-0181 后 on_order_refunded 第 3 步写的行也是 refund=None（事件行已被删除）⇒「FULL_REFUND + refund=None」不再唯一指认「回池反向」这一类、两类只能靠 reason 区分（见 on_order_refunded 的 docstring）。⭐ F-2：审计记真实的链余额前后值——from_value = 冲销前的链余额（上面求出的 chain，例如 -8.00）、to_value = Decimal("0.00")（本次反向之后归零）；修前写的是 "" / 本次反向金额：第 1 步的 log_refund 能查到「refund_fee 从多少变到多少」、而这一步查不出「链余额从多少变到多少」——两处留痕的追溯力不对称。"""
    written = []
    # ⭐ D-0144（C24）：已软删单的客服行不进「上一轮冲减链」的反向记账。
    for performance in Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ).select_related("user"):
        chain = Decimal("0.00")
        for adjustment in PerformanceAdjustment.objects.filter(
            performance=performance,
            kind__in=(
                PerformanceAdjustment.Kind.PARTIAL_REFUND,
                PerformanceAdjustment.Kind.FULL_REFUND,
            ),
        ):
            chain += Decimal(adjustment.amount)
        if chain == 0:
            continue
        if not allow_financial_writes:
            _raise_financial_transition_required()
        row = PerformanceAdjustment.objects.create(
            performance=performance,
            refund=None,
            kind=PerformanceAdjustment.Kind.FULL_REFUND,
            amount=-chain,
            reason=REVERSAL_DEDUCTION_REASON,
            operator=operator,
            settle_status=SettleStatus.UNSETTLED,
        )
        # 审计落在数据库行上（E-0009）：ADJUST + performance_id 指明改的是哪条绩效。⭐ F-2：from/to 记真实的链余额前后值——from_value = 冲销前的 Σ(kind ∈ {PARTIAL_REFUND, FULL_REFUND})（就是上面的 chain）、to_value = 0.00（本次反向之后余额归零）；修前写的是 "" / 本次金额：log_refund（第 1 步）能查到「refund_fee 从多少到多少」、而这里查不出「链余额从多少到多少」⇒ 追溯弱一档。
        # ⭐ M3a：迁到 record(...)、动作 = performance.adjust_manual（不是 performance.adjust：这里新增的是一条 PerformanceAdjustment 记录、不是改绩效上的既有字段；Ruling N 明确两者不得合并）；身份按 R11 落到绩效归属人（target_kind="user" + target_id/target_name）。
        # ⚠️ actor_id 沿域层链路传下来（apply_transition(actor_id=…) → _run_target_hooks(actor_id=…) → performance._on_pool_entry(actor_id=…) → 本函数）⛔ 不把 request 塞进域层、⛔ 也不用 order.updated_by 反查用户凑 pk（那是"猜操作人"：updated_by 是最后改单的人、不一定是这次冲销的人）；⚠️ 机器/同步路径（jidanpu_converge 等）不传 pk ⇒ system=actor_id is None 显式声明「这一行没有操作人」（record() 的 fail-closed 判据、见 log.py）。
        record(
            log_type=ACTION_LOG_TYPES["performance.adjust_manual"],
            action="performance.adjust_manual",
            actor=operator,
            actor_id=actor_id,
            system=actor_id is None,
            target_kind="user",
            target_id=performance.user_id,
            # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`）；
            # 归属关系为空 ⇒ 保持空串（⛔ 不拿 `None` 去调、也不渲染 `None`）。
            target_name=(
                user_display_name(performance.user) if performance.user_id else ""
            ),
            order_no=order.order_no,
            from_value=chain,
            to_value=Decimal("0.00"),
            reason=REVERSAL_DEDUCTION_REASON,
        )
        written.append(row)
    return written


def _reverse_split_child_deductions(order, reversal, operator, *, actor_id=None):
    from django.db.models import Sum
    from apps.business import split_group
    if split_group.is_legacy_member(order) and not split_group.is_main_member(order):
        main = split_group.main_member(order.split_id)
        if main is not None:
            split_group.lock_main_and_sales_nowait(main)

    rows = (PerformanceAdjustment.objects.filter(
        refund__order_id=order.pk,
        kind__in=(PerformanceAdjustment.Kind.PARTIAL_REFUND, PerformanceAdjustment.Kind.FULL_REFUND),
    ).exclude(performance__order_id=order.pk)
        .values('performance_id').annotate(total=Sum('amount')).order_by('performance_id'))
    for item in rows:
        if item['total'] == 0:
            continue
        performance = split_group.lock_performance_nowait(item['performance_id'])
        PerformanceAdjustment.objects.create(
            performance=performance, refund=reversal,
            kind=PerformanceAdjustment.Kind.FULL_REFUND, amount=-item['total'],
            reason=REVERSAL_DEDUCTION_REASON, operator=operator,
            settle_status=SettleStatus.UNSETTLED,
        )
        record(
            log_type=ACTION_LOG_TYPES['performance.adjust_manual'], action='performance.adjust_manual',
            actor=operator, actor_id=actor_id, system=actor_id is None,
            target_kind='user', target_id=performance.user_id,
            target_name=user_display_name(performance.user) if performance.user_id else '',
            order_no=order.order_no, from_value=item['total'], to_value=Decimal('0.00'),
            reason=REVERSAL_DEDUCTION_REASON,
        )


def reverse_previous_round_refund(order, *, actor_id=None, allow_financial_writes=True):
    """⭐ P4-M2：退单回池时把「上一轮」结清——复位累计退款额 + 冲销上一轮的退款冲减。
    调用点：performance._on_pool_entry、且只在来源态是 REFUNDED 时（与 P4-M1 的留痕同一个来源态守卫；ISSUE/PAUSED → 池内不走这里）；整段与调用方在同一个事务里（apply_transition 的调用方 atomic()）、任一步抛异常 ⇒ 三件事一起回滚。⭐ actor_id（keyword-only、默认 None）：这两条留痕（本函数的 order.refund 与第 3 件的 performance.adjust_manual）由人为动作触发（/clear-taker/、/transition/ 从「已退单」回池内）、按业务口径必须带上操作人；主键的来路是显式传参：apply_transition(actor_id=<认证态主键>) → _run_target_hooks(actor_id=…) → _on_pool_entry(actor_id=…) → 本函数；⛔ 不把 request 塞进域层、⛔ 不用 order.updated_by 反查（见 _reverse_sales_… 那一处的注释）；拿不到 pk 的路径（常驻同步/自动接手调 apply_transition 时没传 actor_id）⇒ 留痕显式声明 system=True（机器行）、不是静默通过。
    三件事（顺序即编号）：①OrderRefund(kind=REVERSAL) 新事件行：amount = −(复位前的 refund_fee)、reason=REVERSAL_REFUND_REASON、request_id=REVERSAL-<单号>[-<n>]、operator=order.updated_by；②order.refund_fee 置 0（Order.objects.filter(pk=...).update(...) + 回写内存实例）；③每条客服行的上一轮冲减等额反向一条 FULL_REFUND 调整（见 _reverse_sales_refund_deductions）。为什么必须三件一起（少一件就是新的算错钱）：探针实测（A=100/接单价 100/打手 60%/客服 20%、走生产 HTTP；见 P4-M2 报告）——第二轮 COMPLETED 时 net_received = 100−100 = 0、利润 0−60 = −60 ⇒ 客服提成 −12.00、终身合计 −20.00；第 1/2 件 ⇒ 第二轮读到 refund_fee = 0、net_received = 100、利润 40 ⇒ 8.00；第 3 件 ⇒ 第一轮那条 FULL_REFUND(−8.00) 不再第二次吞掉新赚的提成。⭐ 第 3 件的 kind 为什么是 FULL_REFUND（与 brief 字面写法 MANUAL 不同）：brief 写「用 MANUAL，因为裁定 Z 的全额退款目标式排除 MANUAL ⇒ 不干扰下一轮」——实测（本任务的代数+变异）证明这条推论是反的：MANUAL 确实不进目标式的链、而正是这个「不进」让下一次退单冲不掉新一轮的提成（反向记 MANUAL：链余额 −8.00 没被反向掉 ⇒ 目标式 −(8 + (−8)) = 0.00 ⇒ 终身合计 8.00 ❌ 钱退光了还留着；反向记 FULL_REFUND：−8.00 + 8.00 = 0.00 ⇒ −(8+0) = −8.00 ⇒ 终身 0.00 ✓）；即反向记录必须留在目标式的链上（PARTIAL_REFUND / FULL_REFUND）、否则「客户第二次全额退款、客服却仍拿到 8.00」；⛔ 因此不动 on_order_refunded 的 kind__in（MANUAL 的排除原样保留：财务的人工修正依然不被退款抹掉，D-0047 的边界不变）；⚠️ 语义代价（如实登记）：这些反向行与「真的由某次全额退款产生的冲减」同 kind，旧口径靠 refund=None + reason=REVERSAL_DEDUCTION_REASON 区分；D-0181 后真冲减那类也是 refund=None（退单不再建事件行）⇒ 两类今天只能靠 reason 区分、FULL_REFUND 的汇总口径因此不再等于「全额退款事件造成的冲减总额」。退单不再写 refund_fee：第 1/2 件（REVERSAL 事件行 + 复位）只对「部分退款留下的 refund_fee != 0」生效；写成非 0 的写入点只剩部分退款（refund_set_total 与 order_writes.refund_total_write、都经 _write_refund_total）；第 3 件（等额反向冲减链）照旧必须做——第一轮退款写下的 FULL_REFUND 仍在链上、不反向就会吞掉新一轮赚到的提成；纯退单回池只剩第 3 件有事可做、其余按余额判据跳过。幂等（重跑不增加行、refund_fee 不变、不抛异常）：判据是余额、不是「已有几行」（后者与 n = count + 1 同类：永远派生出新键、永远撞不上——AGENTS §5 记的 P4-M1 教训）；第 1/2 件只在 refund_fee != 0 时做 ⇒ 跑过一次之后 refund_fee 就是 0、重跑跳过；第 3 件只在链余额 != 0 时写 ⇒ 跑过一次之后余额就是 0、重跑跳过；两步各自独立判：refund_fee == 0（毛收款为 0 的边角单）而链上有冲减时第 3 件仍然要写——否则那笔冲减会继续吞下一轮的提成。⚠️ 并发（登记、不在本任务修）：两次并发的「REFUNDED → 池内」可各自算出同一个 n ⇒ 一个成功、另一个撞 uniq_order_refund_request_id；撞车那一次走幂等出口（return None、不再做第 2/3 件、因为余额已由另一个事务复位）、不会 500；与 on_order_refunded 的并发残差同形：进入本函数的真实路径只有 apply_transition（它的 select_for_update 行锁 + 状态复读把同一张单上的并发迁移串行化、后到者抛 IllegalTransition）⇒ 本函数不引入新的锁；绕过 apply_transition（探针/用例直接调 _on_pool_entry）不在那条保护里。"""
    old_refund_fee = Decimal(order.refund_fee)
    operator = getattr(order, "updated_by", "") or ""

    # ── 第 1/2 件：有可冲销的累计退款额才做（重跑时它是 0 ⇒ 跳过）────────────
    if old_refund_fee != 0:
        if not allow_financial_writes:
            _raise_financial_transition_required()
        n = _next_reversal_ordinal(order)
        request_id = (
            f"{REVERSAL_REQUEST_PREFIX}{order.order_no}"
            if n == 1
            else f"{REVERSAL_REQUEST_PREFIX}{order.order_no}-{n}"
        )
        # ⚠️ 嵌套 `atomic()` = **savepoint**：`IntegrityError` 会被下面接住，
        #    若不回到 savepoint，调用方的外层事务会被标记成「必须回滚」
        #    （同 `on_order_refunded` 第 0 步的理由与用例）。
        try:
            with transaction.atomic():
                reversal = OrderRefund.objects.create(
                    order=order,
                    kind=OrderRefund.Kind.REVERSAL,
                    amount=-old_refund_fee,
                    reason=REVERSAL_REFUND_REASON,
                    operator=operator,
                    request_id=request_id,
                )
        except IntegrityError as exc:
            if not _is_duplicate_request_id(exc):
                raise
            # 并发出口：这一次冲销已被另一个事务记下 ⇒ 余额也已由它复位，
            # 第 2/3 件**都不该再做**（否则会多冲一次）。
            return None
        # 第 2 件：置 0。用 `.update()` 写绝对值（读改写会在并发下丢更新），
        # 并**回写内存实例**——同一实例后面还要被读（`_on_pool_entry` 的其余步骤、
        # 以及直接调钩子的探针），不写回就会读到陈旧的 `100.00`。⭐ D-0144（A-w2）：同 A-w1——默认 manager 的已知副作用（已删单 ⇒ 静默 0 行）；判定在动作入口（A6）。
        Order.objects.filter(pk=order.pk).update(refund_fee=Decimal("0.00"))
        order.refund_fee = Decimal("0.00")
        LegacySplitRefundPolicy.objects.filter(order=order, split_id=order.split_id).update(refund_total=Decimal('0.00'))
        _reverse_split_child_deductions(order, reversal, operator, actor_id=actor_id)
        from apps.wecom.external_pay import enqueue_order_refund_recheck
        enqueue_order_refund_recheck(order)
        # 审计**落在数据库行**上（E-0009）：`from_value` = 复位前的 `refund_fee`、
        # `to_value` = `0.00`（与 `on_order_refunded` 的 `log_refund` 同形）。
        # ⭐ M3a：迁到 `record(...)`（动作 `order.refund`，订单级）。
        record(
            log_type=ACTION_LOG_TYPES["order.refund"],
            action="order.refund",
            actor=operator,
            actor_id=actor_id,
            # 同 `_reverse_sales_refund_deductions`：机器 / 同步路径没有 pk ⇒ 显式声明。
            system=actor_id is None,
            target_kind="order",
            target_id=order.order_no,
            order_no=order.order_no,
            from_value=old_refund_fee,
            to_value=Decimal("0.00"),
            reason=REVERSAL_REFUND_REASON,
        )

    # ── 第 3 件：客服行反向调整（自带「余额为 0 ⇒ 不写」的判据）──────────────
    return _reverse_sales_refund_deductions(
        order, operator, actor_id=actor_id, allow_financial_writes=allow_financial_writes,
    )


def _clear_unsettled_sales_amounts(order, operator):
    """退单的第 2.7 步：未结账的客服行 temp_amount / bonus_amount 都清 0、并先留痕。
    ⭐（业务方逐条选定的 Q1/Q2）：业务方原话「也需要清0 做吧，完成订单事后改价 预计和生效需要一起重新计算改掉」；追问后选定「只对未结账的行生效」（已结账的行保持不动并留痕）。⭐ Major-3：清 0 必须在数据库行上留痕——原文只有 save()、一条痕迹都不写 ⇒「原来 8.00 → 现在 0.00」在库里任何一行都查不到（MAIN 侧早有 _record_taker_trace_before_clearing 那条 MANUAL amount=0 的留痕、SALES 侧没有对应物）；现在与 MAIN 侧同形：清 0 之前给每条将被清的 SALES 行写一条 PerformanceAdjustment(kind=MANUAL, amount=Decimal("0.00"), refund=None, settle_status=未结账, reason=f"{SALES_TRACE_PREFIX}原预计 … / 原生效 …", operator=调用方传进来的那一个)；⛔ amount 必须是 0.00：非 0 会改 payable_net 的 Σ、把这一批的代数全打乱（settlement_amount.payable_net 无条件累加未结账调整）；⚠️ 判据打在数据库行上（AGENTS.md §5-G）：断 kind/amount/reason 含原值；⚠️ 幂等：三个待清列都已是终态的行 ⇒ 这一行「没有东西将被清」⇒ 不写留痕、也不 save（重跑因此不会多写第二条 MANUAL 留痕，见 UnsettledClearIdempotencyTests）。
    判据（一处）：该行自己的 is_settled——Performance.is_settled（models.py 的实现）：settle_status == 已结账 或 settled_at is not None 或 settled_amount is not None（三字段并集）；⚠️ 措辞收窄（Minor-7）：不写「唯一」这种绝对断言——models.py 自己的 docstring 登记了本仓另有更窄的判法；⛔ 不是 MAIN 那道闸（order_writes.change_hall_price 只看 MAIN）：SALES 行可以独立已结账、取并集是保守的一侧（单看 settle_status 的脏行会被误判成未结账而被改写已付出的钱）；settlement_hooks.refresh_customer_estimate 用的是同一个属性。为什么连扣点列一起清（deduct_mode="1" 且 value > 0 那一档）：清 0 之后 temp_amount = 0.00 就是扣点的基数、而 performance_amount.deduct_amount 对固定额档判 value > base ⇒ 这一行永远算不出应付（结算预览进 errors、/settle 整批 1006）；⚠️ 本机探针原始读数（合成夹具 total=130 / hall=88 / 打手 17.60 / 客服 44.96 / 固定额扣点 5.00、走真实 /adjust/ 配置这条生产路径）：退款前 payable_net = 39.96；退款后（只清两列金额、不清扣点）⇒ payable_net 抛 AmountError「扣点金额不能超过预计绩效总计（0.00），当前 5.00」⇒ 按计划 §2 A2 的口径把这两列也清掉；⚠️ 代价（如实登记；本轮已按 Minor-11 写进 docs/DECISIONS.md 的 D-0187「已知代价/残差」段 ⛔ 不能只留在本 docstring 里）：回池重做后这条客服行的扣点要财务重新配——全仓只有 /adjust/ 那一条写路径能重配（客服行同样能被 /adjust/ 配上扣点）；⚠️ 只对真的会抛的那一档动手（固定额 + value > 0）：比例档（"0"）的扣点额恒为 base × 比例 / 100、基数 0 ⇒ 恒 0.00、不动（少改一列）。⚠️ 本 docstring 里不写那句查询的原文（filter(order=…, order__deleted_at__isnull=True)）：tests/test_order_read_path_net.py 的扫描器会把它当成一处真实的 order__ join 落点计入本函数的重数（本网的口径是「看见的都算」）⇒ 写了就要在同一张登记表里多记一处、而那不是本函数真正多出来的读路径——下面这句只描述口径、不复现代码。⛔ 不要「清 0 之后就跳过第 3 步」：本函数由 on_order_refunded 放在第 3 步之前调用；若把两列提前清 0 再跳过第 3 步：链非空时会留下正的 PARTIAL_REFUND ⇒ payable_net = 0 + (−8.00) = −8.00 ⇒ 结算列表显示负数、/settle 触发 1011；而第 3 步放在清 0 之后还有第二个理由：未结账的行被清 0 后 net 只剩链 ⇒ 它写 −chain 把链也归零；已结账的行没被清 ⇒ 它们读到的仍是自己的 bonus_amount、走的是今天那一份口径 −(bonus + chain)（完整代数与三条实测读数见 on_order_refunded 第 3 步的注释）。⚠️ D-0144（C26 的同一条 join）：已软删单的客服行不在这里清（与第 3 步同口径）。⚠️ operator 从调用方传进来（on_order_refunded 里那个 order.updated_by）⛔ 不在本函数里再取一次——两处取值会各漂一份（E-0021 的形状）。"""
    for performance in Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ):
        if performance.is_settled:
            continue
        # ⚠️ 必须在**改写之前**取值（留痕要带**原**金额），并当场 `Decimal` 收口。
        temp_before = Decimal(performance.temp_amount)
        bonus_before = Decimal(performance.bonus_amount)
        clear_deduct = (
            str(performance.deduct_mode) == DEDUCT_FIXED
            and Decimal(performance.deduct_value) > 0
        )
        # ⭐ 幂等（Major-3 的连带判据）：三个待清列都已是终态 ⇒ 「没有东西将被清」
        #    ⇒ 不写留痕、也不 `save`。重跑因此不会多写第二条 `MANUAL` 留痕
        #    （与 `settlement_hooks.refresh_customer_estimate` 第 5 条「值没变就不写」同族）。
        if (
            temp_before == Decimal("0.00")
            and bonus_before == Decimal("0.00")
            and not clear_deduct
        ):
            continue
        # ⭐ Major-3：清 0 **之前**留痕（与 MAIN 侧 `_record_taker_trace_before_clearing` 同形）。
        PerformanceAdjustment.objects.create(
            performance=performance,
            refund=None,
            kind=PerformanceAdjustment.Kind.MANUAL,
            # ⛔ 必须是 0.00（见 docstring）：非 0 会改 `payable_net` 的 Σ。
            amount=Decimal("0.00"),
            reason=(
                f"{SALES_TRACE_PREFIX}原预计 {temp_before} / 原生效 {bonus_before}"
            ),
            operator=operator,
            settle_status=SettleStatus.UNSETTLED,
        )
        performance.temp_amount = Decimal("0.00")
        performance.bonus_amount = Decimal("0.00")
        update_fields = ["temp_amount", "bonus_amount", "updated_at"]
        if clear_deduct:
            performance.deduct_mode = ""
            performance.deduct_value = Decimal("0.00")
            update_fields += ["deduct_mode", "deduct_value"]
        performance.save(update_fields=update_fields)


def on_order_refunded(order, from_status, *, allow_financial_writes=True):
    """订单 → REFUNDED：全额退款清空绩效（spec §5.2 的绩效侧；D-0181 后只剩三件）。钩子签名 func(order, from_status) 由 order_status._run_target_hooks 约定、没有 actor 参数（spec §5.3 的「已核对过的两点」②）：操作人从 order.updated_by 取——apply_transition 在同一次 save() 里写好了它。四件（顺序即编号；调用方的 transaction.atomic() 之内；⚠️ 序号保留历史值 1.5/2/3/5、本批新增的一步插在 2 与 3 之间记作 2.7 ⛔ 不重排既有编号——docs/ 与别的模块大量引用「第 2 步」「第 3 步」）：①1.5 打手行的 MANUAL 留痕——F-1：清零之前写一条 amount=0 的调整记录、reason 带真实原金额；②1.6 自动入账的打手工资回冲——该 MAIN 行 settled_by=="system" 且 settled_amount>0 ⇒ 写一条 amount=−settled_amount 的 FULL_REFUND 调整（settle_status=已结账、钱包由 accounts.payout 的信号同事务扣回、允许负）、并把该行的结账痕迹复位成未结账（钱已退回 ⇒ 这一轮不再算结过账、重做那一轮才能再入账/回池）；③2 打手行（打单工资）temp_amount=0、bonus_amount=0、行与 user 都保留；④2.1 打手行的待处理提前结算申请（spec §8.1）pending→cancelled（settled 的不动：钱已按结账打了；cancelled 幂等）；⑤2.7 未结账的客服行（销售提成）先写一条 MANUAL amount=0.00 的留痕（reason 带原 temp_amount/bonus_amount）再把两列清 0，固定额扣点那一档连 deduct_mode/deduct_value 一起清，判据是该行自己的 is_settled（已结账的行一个字节都不动）；⑥3 客服行（销售提成）目标式冲减：amount = −(该行 bonus_amount + Σ该行整条退款冲减链)、算出 0 ⇒ 不写行。
    ⭐（业务方逐条选定 Q1/Q2）：退单把未结账的客服行清 0——业务方原话「也需要清0 做吧，完成订单事后改价 预计和生效需要一起重新计算改掉」；追问后逐条选定：只对「未结账」的行生效（已结账的行保持不动并留痕）、退款口径都要（退单清 0；部分退款也重算）。两列的终态（未结账的行）：temp_amount = 0.00、bonus_amount = 0.00 且链余额 = 0（第 3 步读到 bonus = 0 ⇒ net 只剩链 ⇒ 它写 −chain 把链也归零）⇒ payable_net = 0.00（⛔ 不许变负、⛔ 不许抛 AmountError）；⭐ Major-3：清 0 之前多一条 MANUAL amount=0.00 的留痕（DB 行，见 _clear_unsettled_sales_amounts 的 docstring）；⚠️ Major-4（口径全文见 docs/DECISIONS.md 的 D-0187）：已结账行的 temp_amount 不被退单改写（第 0 条闸，业务方 Q1「只对未结账的行生效」）、链那一半（第 3 步对已结账行写 −(bonus + chain)）一字未变。清 0 落在第 2.7 步（第 3 步之前）——计划 §6.1 的字面写法、也是代数上唯一闭合的那一种：第 3 步先读到 bonus = 0 ⇒ net 只剩链 ⇒ 它写一条 −chain 把链也归零 ⇒ payable_net = 0 + 0 = 0.00；⛔ 反例（本机逐格实测 ⛔ 不要照抄）：把清 0 放到循环之后（让第 3 步读旧 bonus）⇒ 纯退单写 −8.00 而 bonus 已被清 0 ⇒ payable_net = 0 + (−8.00) = −8.00（先部分退款时是 0 + (−6 −2) = −8.00）⇒ 结算列表负数、/settle 触发 1011；⚠️ 代价（如实登记、本批报告的待裁项）：纯退单那一格不再写 FULL_REFUND 冲减行（deduction == 0）⇒ 既有「每条 SALES 行恰好一条 FULL_REFUND、−8.00」与「bonus + Σ全部调整 == 0」两类判据与 Q1 正面冲突（本机实测清单 = test_refund_full 8 条 + test_p4_redo_round 7 条 + test_customer_estimate 1 条、含 6 个 DoesNotExist ERROR）；⛔ 没有「既清 bonus、又保留那条 −8.00 行、还让 payable_net 为 0」的写法——三者数学上互斥（清 0 后任何非 0 的链都会把应付拉离 0）；⚠️ 扣点列的代价（计划 §2 A2、实测依据见函数体）：清 temp_amount 之后固定额扣点（deduct_mode="1"、value>0）会让 payable_net 抛 AmountError（探针读数「扣点金额不能超过预计绩效总计（0.00），当前 5.00」）⇒ 按口径同时清掉这两列（代价如实登记：回池重做后这条客服行的扣点要财务重新配——全仓只有 /adjust/ 这条写路径能重配，已写进 docs/DECISIONS.md 的 D-0187 ⛔ 不能只留在 docstring 里）；⚠️ 已知代价/残差（Major-4 的连带；业务方裁定「保持现状即可 只登记」）：已结账+该行配有扣点+退单那一档 payable_net 为负——⛔ 不是「固定额扣点」才负、比例扣点同样负（代数：已结账行两列都不动 ⇒ 第 3 步照旧写 −(bonus + 链) ⇒ payable_net = bonus − 扣点 + (−bonus) = −扣点额）；两档各自的基线对照：比例档基线是 0.00（那时没有 is_settled 闸 ⇒ 已结账行的 temp 被第 2.5 条清成 0.00 ⇒ 扣点额 0.00）、固定额档基线是抛 AmountError ⇒ 准确的定性：这是本批 is_settled 闸的连带代价 ⛔ 不是老残差（比例那一档是从 0.00 变成负数）；判据见 tests/test_refund_full.py 的成对两条（test_a_settled_fixed_deduction_row_still_computes_a_negative_payable 与 SettledRowDeductionResidualTests 的比例档那条）；触发条件与影响、以及为什么不在本批修（要动 refunds 的冲减额算式 = D-0181 的地盘）逐条登记在 D-0187 的残差段；⛔ 不要把它改成「按 payable_net 归零」的算式（那是另一批的裁定）、⛔ 不要为了消掉负数去清已结账行的扣点列（违反业务方 Q1）；⛔ 本批不动 bonus_amount 的「退款冲减链」代数（裁定 Z）：MANUAL 仍不参与目标式、FULL_REFUND/PARTIAL_REFUND 仍都参与——那两件事与本批无关；⚠️ Major-3 之后本函数自己也会写 MANUAL amount=0.00 的行（SALES 侧留痕）：它刻意不参与目标式（kind__in 里没有它）、金额是 0 ⇒ 对 Σ 与目标式都没有影响。
    ⛔ D-0181（业务方本人裁定）：退单不写退款额、不建全额退款记录——原第 0 步（建 OrderRefund(kind=FULL)）、原第 1 步（把 order.refund_fee 设成毛收款）与原第 4 步（order.refund 审计）已整体删除、理由是不留一句假话：原第 4 步审计记的事实是「退款额从多少变到多少」——新口径下 refund_fee 根本不动、写 0.00→0.00 是假的（退单这件事本身仍由调用方的 order.transition 审计记着：order_writes.transition_order、from=代练中/to=已退单 + 操作人）；原第 0/1 步是旧口径「退单 = 全额退款」的载体、裁定后没有别的承重消费者（OrderRefund(kind=FULL) 原先的用途正是「第 3 步冲减的事件锚 + 幂等键」与退款明细——前者随幂等判据换成「冲减额是否为 0」、后者随本口径退出、明细里只剩部分退款与冲销行）；含上家跟退：jidanpu_upstream.follow_refund 只调 apply_transition(→ REFUNDED)、自己一行不改 ⇒ 自动跟随本口径（上游 spec §15.3.1 / D-0176 的那半条被推翻，spec 2026-10-03-refund-amount-and-restore-sync-design.md §5 登记）。退单不再做事件行、不再把 refund_fee 设成毛收款、也不再撤下租户级鸡蛋铺单。代价（业务方在追问里看到并接受、spec 的 §G5 逐条登记）：net_received = 总价 − 退款额 ⇒ 已退单的单从此按「没退过钱」算（结算/统计/导出）；GET /orders/{单号}/refunds 不再有这次退单、订单检索的「退款记录者」轴对「只有退单、没有部分退款」的单不再命中；OrderRefund.Kind.FULL 不再有新行（枚举值保留：历史行仍在用、删它要迁移）；⛔ 部分退款（kind=PARTIAL）与 reverse_previous_round_refund 的第 1/2 件一个字未改：部分退款照旧写 refund_fee、回池照旧按它冲销。⭐ 为什么 PerformanceAdjustment.refund 现在恒为 None：refund 那一列是 OrderRefund 事件行的外键（事件锚 + 幂等键）、事件行没了 ⇒ 第 3 步的冲减记录只能 refund=None；⚠️ 语义代价如实登记：链上 FULL_REFUND + refund=None 不再唯一指认「回池反向」那一类（_reverse_sales_refund_deductions 也写 refund=None）⇒ 两类今天只能靠 reason 区分（真冲减 = FULL_REFUND_REASON、回池反向 = REVERSAL_DEDUCTION_REASON）⛔ 不要为了「区分两类」去恢复事件行、也不要为此加列。⚠️ 幂等/并发（D-0181 重写；这一条动到钱、逐条写清）：重跑不写第二行——apply_transition 不会重跑本钩子（REFUNDED 没有自环边、IllegalTransition 在跑钩子之前就抛）；直接手工重跑本函数时第 3 步算出的冲减额是 0 ⇒ 不写行（与旧口径的「同一事件只冲一次」在代数上等价、而判据不再依赖事件行）；第 1.5 步（留痕）另按 is_rerun 跳过。is_rerun 判据 = str(from_status) == str(OrderStatus.REFUNDED)：真实迁移一定来自 apply_transition（它传迁移前的状态）、而 REFUNDED 无自环 ⇒ 真实事件的 from_status 一定不是 REFUNDED；⛔ 旧式里还有半条 and latest_existing is not None——那半条依赖「上一次退单留下的 kind=FULL 行」、新口径下它恒 None ⇒ 留着会把手工重跑当成真实事件、多留一条打手痕。同一个订单上的并发迁移：apply_transition 的开头就有一句 Order.objects.select_for_update().only("status").get(pk=order.pk)（行锁）、紧接着复读 locked.status != str(order.status) 就抛 IllegalTransition ⇒ 两次并发「改退单」里后到的那一笔阻塞在那句行锁上、等前一笔提交后复读到 locked.status == "REFUNDED" ≠ 它内存里的来源态 ⇒ 直接被拒、根本走不到本函数；所以「同一次退单只冲一次」的第二道保险不是我们新加的锁；本批不引入新的锁、不加列、不加迁移。✅ 残余风险（如实登记、与旧版 A3 同形）：绕过 apply_transition 直接调本函数的路径（探针/用例）不在这条保护里；那类调用若谎报 from_status、第 1.5/第 5 件会误判成真实事件（第 3 步仍由「冲减额为 0」兜住）；这条调用路径不存在于生产（唯一调用点是 apply_transition 的目标态钩子表）。⭐ F-1：留痕的「原金额」为什么落在这里：spec §5.2 预案② 要的是「原接单人 + 原金额」；P4-M1 的实测证明：把这条留痕挂在回池那一刻（performance._on_pool_entry）拿不到原金额——本函数的第 2 步先把 MAIN 行清零、回池在其后、所以那里读到的恒为 0.00（需求说「清零前留痕」、而落点选在了清零之后：落点与时序矛盾）；修法是把主留痕放在第 2 步之前（即上表的 1.5）、兜底仍留在回池钩子里、服务「绕开本钩子」的老数据、并按 TAKER_TRACE_PREFIX 判重以免正常链路上写两条；两处都不改既有行为：第 2 步照旧清零、reset_taker_performance 照旧清 user。⚠️ 第 3 步为什么不适用 §4.5 的 −amount × rate / 100：那条公式的论证前提是「打手绩效不动」——退差价只冲客服提成、打手的钱照旧、所以「利润差额 = 退款额」；全额退款的第 2 步恰恰把打手绩效清了 0、前提不成立；代入 实收 A=100、打手 T=60、提成率 r=20%：COMPLETED 时利润 100−60 = 40、客服行 bonus_amount = 40 × 20% = 8；全额退款后利润 100−0−100 = 0、客服行应为 0、第 3 步应冲减 −8；若误用 §4.5 公式 −(100 × 20%) = −20 ❌ ⇒ 净额 8 − 20 = −12（恰是 −T×r）⇒ 撞 P4-7 结账的 1011（应付净额为负）⇒ 这条调整记录永久结不了账；所以这里用目标式：直接算出「要冲到 0 需要多少」、并在实现里显式声明这一处偏离；⛔ 不套用 §4.5 的公式、也不调用 commission_amount / 不读 commission_rate。⭐ 为什么偏离 §5.2 的字面公式（P4-10 / 裁定 Z）：字面公式是 Σ该行未结账调整；原实现逐字转录了它、实测出过真错账——部分退款 30（冲减 −6）→ 财务把客服行连 −6 一起结账 → 全额退款：字面公式净额 = 8.00 + 0（−6 已结账、被排除）⇒ −8.00；本实现（整条链）净额 = 8.00 + (−6.00) ⇒ −2.00；字面版算完的终身合计是 −6.00 而不是 0.00：客户的 100 全退了、账上却还欠客服 6 元提成；且它正好让 payable_net（只看未结账）看到 0 ⇒ P4-7 的 1011 闸门静默放行、错误以「结账成功」的形态落地；根因是不对称：Σ 排除了已结账的冲减、却把同样已结账的 bonus_amount 算了进去；裁定 Z 因此把净额口径改成整条退款冲减链、与结账状态无关：bonus_amount 是绩效行自己的原始金额（它的结账状态由行本身表达）、退款冲减链上与它配对、两者同进同出才谈得上「冲到 0」。⭐ 边界（编排方裁定、登记为待确认）：kind=MANUAL（财务人工修正）不参与目标式——只取 PARTIAL_REFUND 与 FULL_REFUND；语义：全额退款把「这条订单的提成」冲到 0、财务的人工修正是独立的账务决定、不被退款抹掉；若把 MANUAL 也算进去、一条已结账的人工扣款会被全额退款反向撤销（钱退给客服）——那是错的方向；📌 业务方未表态 ⇒ 登记为待确认；若业务方要求退款同时抹掉人工修正、只需把下面的 kind__in 去掉（净额改为「Σ全部调整」）。⛔ 不用 settlement_amount.payable_net 代替这段求和：payable_net 的结账口径（只算未结账调整 + 扣点项）是 P4-7 的语义、且它会因坏行抛 AmountError——那是「结账」的判断、不是「退款目标式」的判断；本模块不碰 payable_net / settlement.py。⭐ P4-M2 之后：链上会出现 refund=None 的 FULL_REFUND 行（有意留在链上）——「退单 → 回池 → 重做」时 reverse_previous_round_refund 会给客服行写一条 kind=FULL_REFUND、refund=None 的反向调整（冲销上一轮的冲减链）；它必须留在本函数第 3 步的 kind__in 里：否则下一次全额退款冲不掉新一轮赚到的提成（客户钱退光、客服仍留着提成）；所以看到 refund=None 的 FULL_REFUND 行时不要把它当成脏数据；⚠️ D-0181 之后本函数自己写的行也是 refund=None ⇒ 两者靠 reason 区分（真冲减 = FULL_REFUND_REASON、回池反向 = 反向那条 reason）；MANUAL 的排除原样不变（财务人工修正仍不被退款抹掉，裁定 Z / D-0047）。"""
    operator = getattr(order, "updated_by", "") or ""
    # ⭐ D-0181：判据 = 「这一次是不是**真实的退单迁移**」。
    #    真实事件一定来自 `apply_transition`（它传迁移**前**的状态），而 `REFUNDED` 没有自环
    #    ⇒ 真实事件的 `from_status` 一定不是 `REFUNDED`；等号成立就说明这是**重跑**。⛔ 旧式还有半条 `and latest_existing is not None`——它读的是「上一次退单留下的`kind=FULL` 事件行」，新口径下那行不再产生（恒 `None`）⇒ 留着会让**手工重跑**被当成真实事件、多留一条打手痕（见 docstring 的幂等小节）。
    is_rerun = str(from_status) == str(OrderStatus.REFUNDED)

    # ── 第 1.5 步（F-1）：退单清空**之前**留痕「原接单人 + 原金额」。
    #    ⭐ 落点是**这里**而不是回池那一刻（`performance._on_pool_entry`）：第 2 步会把
    #    MAIN 行金额清零，那之后读到的恒为 `0.00`（P4-M1 实测）。⚠️ 只在**这是一次真实的退单迁移**时写（重跑不写）。⛔ 不改第 2 步行为：这只是在它**之前**多写一条留痕。
    if not is_rerun:
        _record_taker_trace_before_clearing(order, operator)
        # ── 第 1.55 步（提现改造增量）：先把该打手「已通过、未打款」的
        #    提现单中断（钱退回余额），**再**做下面的自动入账回冲。
        #    顺序理由：否则「提现单里压着钱、余额不够扣」会让冲账扣成负得更多，而用户看到的是「提现还在打款中、余额已经是负的」。⚠️ 必须在 `not is_rerun` 里：手工重跑不是一次新的退单动作，误中断会拦掉退单后重新提交的合法提现。
        from apps.accounts.models import WithdrawOrder
        from apps.accounts.wallet import interrupt_open_withdrawals

        taker_id = (
            Performance.objects.filter(
                order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
            )
            .values_list("user_id", flat=True)
            .first()
        )
        if taker_id is not None:
            if allow_financial_writes:
                interrupt_open_withdrawals(taker_id, reason="订单退款")
            elif WithdrawOrder.objects.filter(user_id=taker_id, state=WithdrawOrder.STATE_APPROVED).exists():
                _raise_financial_transition_required()
        # ── 第 1.6 步（钱包发薪线）：自动入账的工资在退单时从钱包扣回。
        #    ⚠️ 必须在第 2 步清零**之前**（它要读 `settled_amount`；第 2 步只清
        #    `temp_amount` / `bonus_amount`，本来不碰结账四列，但顺序写死更安全）。
        _claw_back_auto_paid_taker(order, operator, allow_financial_writes=allow_financial_writes)

    # ── 第 2 步：打手行金额置 0。**行保留、`user` 保留**（方案 A / D8）：
    #    删行会断掉 P3 的唯一索引与「谁交的单」这条审计事实。
    # ⭐ D-0144（C25，**写侧 join**）：已软删单的 MAIN 行不归零。判据的「已删」由动作入口（A6）挡住 ⇒ 这里多一层 join 条件只是纵深；⚠️ 若它单独被调用在已删单上，`.update()` 会**静默 0 行**（不报错）——与 A-w1 同形。
    Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
    ).update(
        temp_amount=Decimal("0.00"), bonus_amount=Decimal("0.00")
    )
    # ── 第 2.1 步（spec §8.1）：**这条单上打手行的待处理提前结算申请随退款作废**。
    #    `pending → cancelled`；`settled` 的**不动**（那笔钱已经按那次结账打了，退款是另一条
    #    回冲链，不改「当时申请被满足过」这个事实）。⚠️ **只改 `pending` 是退款这一条路径的口径**——「撤销结账」那条连 `settled` 一起作废（结算被撤销 ⇒ 那次「已结」不成立）；两条路径的对照表在`apps/business/settle_requests.py` 的模块 docstring。⛔ 别把两边统一成一种。⚠️ **幂等**：条件更新只命中 `pending` ⇒ 同一单重复退款再跑一次命中 0 行，不会重复扣额度（`cancelled` 本来就不占额度，见 `settle_requests.QUOTA_STATES`）。⚠️ 同一个事务：本函数在调用方 `apply_transition` 的 `atomic()` 里跑。⚠️ 只取**未软删**的 MAIN 行（与第 2 步同一组行）：已删行的申请不在财务视野里。
    performance_ids = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True,
    ).values_list("id", flat=True)
    if allow_financial_writes:
        cancel_pending_settle_requests(performance_ids)
    else:
        from apps.business.models import PerformanceSettleRequest

        if PerformanceSettleRequest.objects.filter(
            performance_id__in=performance_ids, state=PerformanceSettleRequest.State.PENDING,
        ).exists():
            _raise_financial_transition_required()
    _clear_unsettled_sales_amounts(order, operator)

    # ⭐（客服预计绩效线，计划 §2 T1.2 的显式挂点 12）：上面这一句改掉了客服预计的
    #    **减项**（MAIN `temp_amount` → `0.00`），第 1 步又改过 `order.refund_fee`（净实收变）
    #    —— 两个输入都变了，而它是 queryset `.update()`、**不过** `save()` ⇒ 主挂点看不见。⚠️ **必须函数内懒导入**：`settlement_hooks` 顶层 import 本模块（为了注册`on_order_refunded` 钩子）⇒ 在本模块顶层 import 它会成环。⚠️ 与上面那句`from apps.business import jidanpu_hall` 同一种处置。⚠️ 位置在**第 3 步之前**：第 3 步只动 SALES 行的 `bonus_amount`（走 `PerformanceAdjustment`冲减链），与 `temp_amount` 无关；两条互不覆盖，顺序不改变结果。⚠️ **本句必须排在第 2.7 步之后**（清 0 后它在 `REFUNDED` 单上写不出任何东西）。上面那句「顺序不改变结果」指的是**与第 3 步**的关系（仍成立）；与第 2.7 步的关系是：清 0 之后本函数在 `REFUNDED` 单上**写不出任何东西**（`temp` 已是 `0.00`，第 5 条「值没变就不写」）。保留这次调用是**口径兜底**——第 2.5 条按**订单状态**生效、与哪条路径无关（本函数另有 10 处调用点照旧吃它），⛔ **不是**因为它在这条路上还改值。
    from apps.business.settlement_hooks import refresh_customer_estimate

    refresh_customer_estimate(order)

    # ── 第 3 步：客服行**目标式**冲减（见 docstring 的代数对照与裁定 Z）。
    # ⭐ D-0144（C26）：同一 join 条件，已软删单的客服行不进这一步。
    for performance in Performance.objects.filter(
        order=order, emp_type=EmpType.SALES, order__deleted_at__isnull=True
    ):
        # ⚠️ 这里读的是**第 2.7 步清 0 之后**的值：未结账的行恒 `0.00`（`net` 就只剩链），
        #    已结账的行**没被清**（第 2.7 步跳过它们）⇒ 它们读到的仍是自己的 `bonus_amount`
        #    ⇒ 走的还是**今天那一份**冲减口径 `−(bonus + chain)`。⚠️ **措辞收窄（Major-4）**：这里的「不变」**只指这条冲减链的算式**；已结账行的 `temp_amount` 在本批**不再**被退单改写（第 0 条闸），那是**有意引入的变化**，见 `D-0187` 的第 ③ 段。
        net = Decimal(performance.bonus_amount)
        # ⭐ 裁定 Z：净额按**整条退款冲减链**算，**与结账状态无关**——
        #    字面公式「只算未结账」会让「已结账的部分退款冲减」漏出净额，
        #    终身合计停在 −T×r 而不是 0（见 docstring 的时序表）。⛔ `MANUAL`（财务人工修正）**刻意不参与**：它是独立的账务决定，不能被全额退款反向撤销（裁定 Z 的边界，登记为待确认）。
        for adjustment in PerformanceAdjustment.objects.filter(
            performance=performance,
            kind__in=(
                PerformanceAdjustment.Kind.PARTIAL_REFUND,
                PerformanceAdjustment.Kind.FULL_REFUND,
            ),
        ):
            net += Decimal(adjustment.amount)
        # ⭐ **D-0181 的幂等判据**：本次算出来的冲减额是 `0` ⇒ **不写行**。
        #    重跑天然幂等（余额已被上一次冲到 0）；而「退单 → 回池 → 重做 → 再退单」里
        #    回池已把上一轮等额反向 ⇒ 第二次退单算出来非 `0`、照旧写一行（P4-M1 / P4-M2 的判据在代数上等价，见 `reverse_previous_round_refund`）。⛔ 不要退回旧式「`(performance, refund=本次事件行)` 是否已有 `FULL_REFUND` 行」——那条判据的锚是一个**事件行**，而事件行已按 D-0181 删除。
        deduction = -net
        if deduction == 0:
            continue
        if not allow_financial_writes:
            _raise_financial_transition_required()
        PerformanceAdjustment.objects.create(
            performance=performance,
            refund=None,                         # ⭐ D-0181：事件行已不存在
            kind=PerformanceAdjustment.Kind.FULL_REFUND,
            amount=deduction,                    # 把净额冲到 0（**不是** −amount×rate/100）
            reason=FULL_REFUND_REASON,
            operator=operator,
            # 原行已结账也照建；新调整记录自身是「未结账」，进入下一次结账
            # （与 P4-9 §4.5 细节 1 同源）。
            settle_status=SettleStatus.UNSETTLED,
        )

    # 第 5 件（退单时去租户级鸡蛋铺撤对应单）已删：那条线和模块都不在了，没有可调的对象。
    # 不要把那段调用补回来。`is_rerun` 留痕那一步还在用，不要顺手删掉。
    # 退单的本地后果（状态 / 绩效 / 留痕）不变。
