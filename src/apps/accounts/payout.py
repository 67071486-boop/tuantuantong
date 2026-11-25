"""打手工资入账——**已不再由订单完成自动调用**（停用口径 `docs/specs/2026-10-23-taker-income-and-early-settlement.md` §3.1，历史口径 `docs/specs/2026-10-21-wallet-single-payout.md` §1）：钱包只做「充值 / 押金冻结 / 提现」，订单绩效不再进钱包；完成单只生成绩效行，钱由财务按「结账状态」线下打款（「已结账」= 已打款）。`payout_completed_order` 已无生产调用点（`apply_transition` 不再调它），保留实现只服务：① 历史——存量 `settled_by="system"` 的行要被结算 / 冲账正确对待；② 人工——按老口径补一笔入账（⛔ 没有任何定时任务或信号调它；调用它的人必须自己提供事务边界）。
两件事各只有一个入口：① 自动入账——⭐ 只对「打手 App 注册」的接单人结账（D-0279：判据 = 归属人 `StaffProfile.account_source == "app"`，判据只有这一处）；② `PerformanceAdjustment` 的 `post_save` 接收器——只在 `created=True` 冲账，由 `apps/accounts/apps.py` 的 `ready()` 注册（漏了 = 静默不注册）⭐ 钱与结账快照一起动（D-0280）。⛔ 钱必须与「已结账」同一事务（分开 = 双付或「标了已结但钱没到」）。三条钱的口径：金额 = `payable_net(perf, 未结账调整)`（含扣点可为负）；**只加不减**（`amount <= 0` 一分不动钱包，亏损由后台结算的 `1011` 处理，但仍标已结账并快照）；**冲账允许扣负**（追回已发的钱，`request_withdraw` 的闸挡负余额提现）。幂等判据落在数据库行：自动入账 `settle_status == SETTLED` 直接返回；冲账以 `WalletTransaction(ref_no=f"A{adj.pk}", kind=earning_adjust)` 那一行为准。
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.wallet import _record
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import EmpType, Performance, PerformanceAdjustment, SettleStatus
from apps.business.performance_amount import AmountError
from apps.business.settlement_amount import payable_net

logger = logging.getLogger(__name__)

#: 自动入账写进 `settled_by` 的字面量（规格 §1.2 第 6 条逐字）。
SYSTEM_SETTLED_BY = "system"

#: 自动入账的流水说明（规格 §1.2 第 5 条逐字）。
EARNING_NOTE = "订单完成自动入账"

#: ⭐ 重开后再完成时的补差说明（追加口径：自动入账后重开改金额再完成）。
#: 这一档**只补不退**（与自有金额「只加不减」同一条口径）：新净额比已发的多，就把差额补上；
#: 比已发的少则一分不动（多发的靠退款 / 手工调整那条线追，⛔ 不在这里静默倒扣）。
REPAY_NOTE = "订单再次完成补差"

#: 冲账流水的说明前缀（`ref_no` 已经能指认调整行，说明只给人看）。
ADJUST_NOTE = "已结账绩效的调整冲账"


def _main_performance(order):
    """该单的 MAIN（打单工资）绩效行；没有 ⇒ `None`（历史单 / 异常单不能把验收卡死）。"""
    return Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, deleted_at__isnull=True
    ).first()


def _is_app_registered_taker(user_id):
    """这个打手是不是**打手 App 注册**出来的（`StaffProfile.account_source == "app"`）。
    业务方口径（D-0279）：自动结账**只对 App 注册的接单人生效**。没有档案行 ⇒ `False`（平台管理员 / `seed_tenant` 的 `admin` / `seed_e2e` 的开发账号按设计没有档案行，语义就是「非 App」）；`console`（网页端 / 后台新增）、`link`（链接自助注册）、`import` ⇒ 一律 `False`：这些账号的钱由财务在后台人工结算。
    """
    return StaffProfile.objects.filter(
        user_id=user_id, account_source=AccountSource.APP
    ).exists()


def _mark_adjustments_settled(unsettled, settled_at):
    """把本次算进净额的调整记录标成「已结账」（钱已经发 / 补过，不能再进结算批）。
    ⚠️ 必须有这一句：`settle_performances` 的调整腿会捞「已结账绩效行名下的未结账调整」（坏账出口）⇒ 不标的话，一笔已随工资发出去的负数调整会**第二次**进入结算的净额（`payable_net` 再减一次），极端情况下让整批结算 `1011` 永远结不掉（钱没有第二次移动，但账目是错的）。⚠️ 用 `update()` 而不是逐行 `save()`：`post_save` 接收器只在 `created=True` 时冲账，`update()` 不触发任何信号，这里本来也不该再动钱包。
    """
    if not unsettled:
        return
    PerformanceAdjustment.objects.filter(
        pk__in=[row.pk for row in unsettled]
    ).update(settle_status=SettleStatus.SETTLED, settled_at=settled_at)


def payout_completed_order(order):
    """把一张已完成订单的 MAIN 绩效应付净额打进打手钱包，并标记绩效已结账。返回写下的 `WalletTransaction`（`amount <= 0` 或无需入账时为 `None`）。
    ⛔ **已不再自动调用**（spec §3.1，见模块 docstring 顶部）：只用于**历史 / 人工**场景，调用方必须自己提供事务边界。⚠️ 缺 MAIN 行 / 缺归属人 ⇒ 不付、记 warning、不抛错（规格 §1.2 第 1 条；历史 / 异常单不能让验收失败）。⭐ **只给 App 注册打手自动结账**（D-0279）：归属人没有档案行 / 来源不是 `app`（`console` / `link` / `import`）⇒ 记 warning、**钱与结账列都不动**，留给后台人工结算；判据放在本函数最前 ⇒ 所有调用方自动一致。⭐ **重开后再完成**（`COMPLETED → PENDING_REVIEW → COMPLETED`）：自动入账的行第二次完成按**新净额补差**（只补不退），`settled_amount` 记已发总额；存量财务手工结账的行照旧直接返回、一分不动。⚠️ **金额算不出来**（`AmountError`：扣点越界 / 提成模式不可算）⇒ 记 `exception`、**不标已结账、不加钱**，留给后台人工结算——「不标已结账」而不是「标 0 元」是为了**不丢钱**（标了就再也不会被结算列表捞到）。
    """
    with transaction.atomic():
        perf = _main_performance(order)
        if perf is None:
            logger.warning(
                "订单完成自动入账：没有 MAIN 绩效行，跳过（order=%s）",
                order.order_no,
            )
            return None
        if perf.user_id is None:
            logger.warning(
                "订单完成自动入账：MAIN 绩效行没有归属人，跳过（order=%s perf=%s）",
                order.order_no,
                perf.pk,
            )
            return None
        # ⭐⭐ 自动结账的**唯一判据**（业务方口径，选项 B；D-0279）：业务方只愿意给「打手 App 自己注册的接单人」自动发钱；⛔ 判据只写这一处——不要在 `order_status.py` 或视图里再抄一份（两处会漂移，E-0021 同族）；位置在拿到 `perf` / 归属人之后、算钱（`payable_net`）之前；非 App ⇒ 钱、结账列、调整记录、操作日志**一个字节都不动**。
        # ⚠️ 只挡「第一次自动结账」：已由 system 结过账的行**不看来源**、照常往下走（「重开 → 改金额 → 再完成」时必须继续走下面的**补差**分支，否则会静默少发钱）。
        if not perf.is_settled and not _is_app_registered_taker(perf.user_id):
            # ⚠️ 用 `warning` 而不是 `info`：本项目**没有 LOGGING 配置** ⇒ `logger.info`
            # 在生产是空操作（`tests/test_p4_audit.py` 那几条注释写的就是这个），
            # 「留一条日志」用 info 等于没留。⛔ 别改回 info。
            logger.warning(
                "订单完成自动入账：非 App 注册打手，等待后台人工结算"
                "（order=%s perf=%s user=%s）",
                order.order_no,
                perf.pk,
                perf.user_id,
            )
            return None
        # ⚠️ 行锁：同一张单的两次验收在 `apply_transition` 的订单行锁上已经串行，
        # 这里再锁绩效行是为了让「读状态 → 加钱 → 标已结账」这一组在并发下也只有
        # 一次能进来（幂等判据仍然是下面那一句 `settle_status`）。
        perf = Performance.objects.select_for_update().get(pk=perf.pk)

        unsettled = list(
            perf.adjustments.filter(settle_status=SettleStatus.UNSETTLED)
        )
        try:
            amount = payable_net(perf, unsettled)
        except AmountError:
            logger.exception(
                "订单完成自动入账：应付净额算不出来，保留未结账交给后台人工结算"
                "（order=%s perf=%s）",
                order.order_no,
                perf.pk,
            )
            return None

        settled_at = timezone.now()
        # ⚠️ 判据用 `is_settled`（三字段并集）而不是单看 `settle_status`：脏行里可能
        # 「状态列未结账、却留着 `settled_amount` / `settled_at`」——那是已经发过的钱，
        # 单看状态列会**再付一次**。
        if perf.online_payroll_batch_id:
            return None
        if perf.is_settled:
            # ⭐ **重开后再完成**：`COMPLETED → PENDING_REVIEW → COMPLETED` 是既有流程（判据 = `tests/test_performance_completion_activation.py::test_re_entering_completed_overwrites_the_bonus_without_duplicating_rows`）。已结账的行若照规格第 2 条直接 return，第二次完成既不更新 `settled_amount`、也不补差 ⇒ 按**新净额补差**：只补不退（见 `REPAY_NOTE`）；`settled_amount` 记**已发总额**（新净额比它低时保持原值 = 实付快照，⛔ 不写成应付值）。
            # ⚠️ 只认 `settled_by=="system"`：存量财务手工结账的行钱没进过钱包，照旧直接返回（上面 `_activate_taker_performance` 也不会覆盖它的 bonus）。
            if perf.settled_by != SYSTEM_SETTLED_BY:
                return None
            paid = (
                Decimal("0")
                if perf.settled_amount is None
                else max(Decimal(perf.settled_amount), Decimal("0"))
            )
            # ⭐ 这里的「应付」必须按**历次调整的累计**算，⛔ 不能只拿未结账那几条：人工负向调账（P0-4 之后）会把钱包**扣回**并同步调低 `settled_amount`；只算 `bonus + 未结账调整` 会与 `settled_amount` 差出一截 ⇒ 重开再完成时把刚才扣回的钱**又补回去**、人工扣款被静默撤销（一次性探针实测 20.00 → 30.00）。
            # ⚠️ 已结账的调整都是「已经体现在 settled_amount 里」的：要么 `payout_adjustment` 当场付掉、要么上一轮 `_mark_adjustments_settled` 跟这条链路付掉 ⇒ 用全部调整求和不会重复计。
            try:
                due_total = payable_net(perf, list(perf.adjustments.all()))
            except AmountError:
                # 与上面同口径：算不出就留给人工，⛔ 不静默按 0 补。
                logger.exception(
                    "订单完成自动入账：补差时应付累计算不出来，保留未结账交给后台人工结算"
                    "（order=%s perf=%s）",
                    order.order_no,
                    perf.pk,
                )
                return None
            due = max(due_total, Decimal("0"))
            delta = due - paid
            if delta <= 0:
                return None
            wallet, _ = TakerWallet.objects.select_for_update().get_or_create(
                user_id=perf.user_id
            )
            wallet.balance += delta
            wallet.save(update_fields=["balance", "updated_at"])
            tx = _record(
                wallet,
                WalletTransaction.KIND_EARNING,
                delta,
                ref_no=f"P{perf.pk}",
                note=REPAY_NOTE,
            )
            perf.settled_amount = due
            perf.settled_at = settled_at
            perf.save(
                update_fields=["settled_amount", "settled_at", "updated_at"]
            )
            _mark_adjustments_settled(unsettled, settled_at)
            record(
                log_type=ACTION_LOG_TYPES["performance.settle"],
                action="performance.settle",
                actor="system",
                actor_id=None,
                system=True,
                target_kind="performance",
                target_id=perf.pk,
                target_name=order.order_no,
                order_no=order.order_no,
                from_value="已结账",
                to_value="已结账",
                reason=f"{REPAY_NOTE} {delta} 元",
            )
            return tx

        tx = None
        if amount > 0:
            wallet, _ = TakerWallet.objects.select_for_update().get_or_create(
                user_id=perf.user_id
            )
            wallet.balance += amount
            wallet.save(update_fields=["balance", "updated_at"])
            tx = _record(
                wallet,
                WalletTransaction.KIND_EARNING,
                amount,
                ref_no=f"P{perf.pk}",
                note=EARNING_NOTE,
            )

        # ⚠️ 无论加没加钱，绩效行都要标已结账：后台结算只捞「未结账」的单
        # （`settle_performances` 的第 2 步）⇒ 不标它就会双付。
        perf.settle_status = SettleStatus.SETTLED
        perf.settled_at = settled_at
        perf.settled_by = SYSTEM_SETTLED_BY
        perf.settled_amount = amount
        perf.save(
            update_fields=[
                "settle_status",
                "settled_at",
                "settled_by",
                "settled_amount",
                "updated_at",
            ]
        )
        _mark_adjustments_settled(unsettled, settled_at)

        # 操作日志复用后台结算的动作码（规格 §1.2 第 7 条）；机器来源显式 `system=True`。
        record(
            log_type=ACTION_LOG_TYPES["performance.settle"],
            action="performance.settle",
            actor="system",
            actor_id=None,
            system=True,
            target_kind="performance",
            target_id=perf.pk,
            target_name=order.order_no,
            order_no=order.order_no,
            from_value="未结账",
            to_value="已结账",
            reason=EARNING_NOTE,
        )
        return tx


def payout_adjustment(adjustment):
    """把一条 `PerformanceAdjustment` 冲进钱包（只对**已结账**的 MAIN 绩效行）。返回写下的 `WalletTransaction`；不需要冲 / 已经冲过 ⇒ `None`。幂等判据：`WalletTransaction(kind=earning_adjust, ref_no=f"A{adj.pk}")` 已存在 ⇒ 一分不动（与 `_refund_withdraw` 拿流水当「钱已经动过」的证据同源）。⭐ **锁序：先绩效行、后钱包**（与 `payout_completed_order` 同序）——反过来会在并发下互相等对方的行。
    ⭐ **钱与结账快照必须一起动**（结算 P0-4；同事务）：否则「重开再完成」会把同一笔调整**再补一次**、全额退单又按 `settled_amount` **少扣**这笔 ⇒ 落三件事：钱包 `+= amount`、`perf.settled_amount += amount`、这条调整记录置「已结账 / settled_at=now()」。⚠️ **只在这一支**（MAIN ∧ 有归属人 ∧ `amount != 0` ∧ 库里此刻 `settle_status=已结账` ∧ `settled_by == system`）；SALES / 未结账 / 手工结账 / `amount=0` 一律提前返回、**一个字节都不动**（各自的创建点照旧走「强制未结账、下次结账」——退款冲减 / 上轮冲销是 SALES 行，回池留痕是 `amount=0`）。
    """
    with transaction.atomic():
        perf = adjustment.performance
        if perf.emp_type != EmpType.MAIN:
            # ⛔ 客服提成不走钱包（规格开头）：SALES 行的退款冲减绝不能改到谁的余额上。
            return None
        if perf.user_id is None:
            return None
        amount = Decimal(adjustment.amount)
        if amount == 0:
            # 0 元的留痕行（退单清空前接单人 / 清空客服行）不构成钱包变动。
            return None
        # 重新读一次绩效行的结账状态（信号触发时拿到的可能是创建前的实例，这里要最新值）；⚠️ 先判状态再建钱包：未结账的行一个字节都不动（规格 §1.3 最后一句）。
        # ⚠️ 必须同时是「system 自动结账」：财务**手工结账**的行钱从来没进过钱包，只看 `settle_status` 会让后续调整去加/扣一个根本不存在的余额（D-0279 把大量非 App 行送回人工结账后，这条老洞会变成主路径）⇒ 判据补成「已结账 **且** settled_by == system」。
        # ⭐ 用 `select_for_update()`（P0-4）：「读结账状态 → 加钱 → 写快照」必须原子，否则并发调整各自读到同一个 `settled_amount`、最后一个覆盖前一个（快照丢一次调整）；锁对象变了但判据一个字没改——命不中返回 `None`（一笔都不动）。
        locked = (
            Performance.objects.select_for_update()
            .filter(
                pk=perf.pk,
                settle_status=SettleStatus.SETTLED,
                settled_by=SYSTEM_SETTLED_BY,
            )
            .first()
        )
        if locked is None:
            return None
        perf = locked

        wallet, _ = TakerWallet.objects.select_for_update().get_or_create(
            user_id=perf.user_id
        )
        ref_no = f"A{adjustment.pk}"
        already = WalletTransaction.objects.filter(
            wallet=wallet,
            kind=WalletTransaction.KIND_EARNING_ADJUST,
            ref_no=ref_no,
        ).exists()
        if already:
            return None

        # ⚠️ 允许扣成负数（规格 §1.3）：这是对已经发出去的钱的追回。
        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])
        tx = _record(
            wallet,
            WalletTransaction.KIND_EARNING_ADJUST,
            amount,
            ref_no=ref_no,
            note=f"{ADJUST_NOTE}：{adjustment.reason}"[:200],
        )

        # ⭐ P0-4：同步「已发总额」快照。**不夹 0**（计划 §4 第 3 条）：`settled_amount` 的语义是「这一轮实际发出去多少」，钱包本身允许负余额；夹 0 会让全额退单的 `_claw_back_auto_paid_taker` 只扣 0（少扣 = 打手白拿）。
        # ⚠️ 脏行（已结账但快照为 NULL）按 0 起算：它的「已发金额」本来就无从得知。
        paid = (
            Decimal("0")
            if perf.settled_amount is None
            else Decimal(perf.settled_amount)
        )
        perf.settled_amount = paid + amount
        # ⚠️ **不动 `perf.settled_at` / `settled_by`**：它们记的是「这一行上一轮被谁、
        # 何时结清」；调账不是一次新的结账动作（改它们会让财务对账看到假话）。
        perf.save(update_fields=["settled_amount", "updated_at"])

        # 这条调整记录同样已结账：钱已经真的动了，它不能再以「未结账调整」的身份进入
        # 下一次结账（否则 `payable_net` 会把它再加/再减一遍）。
        settled_at = timezone.now()
        # ⚠️ 用 `update()`（与 `_mark_adjustments_settled` 同形）：调整记录的 `post_save`
        # 接收器只认 `created=True`，这里再 `save()` 一次既不会二次冲账、也只会多绕一圈。
        PerformanceAdjustment.objects.filter(pk=adjustment.pk).update(
            settle_status=SettleStatus.SETTLED, settled_at=settled_at
        )
        # ⚠️ 内存里的实例同步一份：`adjustment_views` 直接把 `create()` 拿到的实例交给
        # 序列化器当响应体 ⇒ 不同步的话，接口会回一个和库里不同的「未结账」。
        adjustment.settle_status = SettleStatus.SETTLED
        adjustment.settled_at = settled_at
        return tx


@receiver(post_save, sender=PerformanceAdjustment, dispatch_uid="accounts.payout_adjustment")
def _on_adjustment_created(sender, instance, created, raw=False, **kwargs):
    """调整记录**创建之后**冲账（退款联动 / 财务手工调整的所有创建点共用这一处）。
    ⚠️ 只认 `created=True`：`PerformanceAdjustment` 的 id 是幂等键的一半（`ref_no=A<pk>`），重复 `save()` 不产生第二笔冲账。⚠️ `raw=True`（`loaddata` / 数据迁移）不冲账：那是历史数据，钱包不该被回溯改动（规格 §3「存量已结算的单不回冲」）。
    """
    if raw or not created:
        return
    payout_adjustment(instance)
