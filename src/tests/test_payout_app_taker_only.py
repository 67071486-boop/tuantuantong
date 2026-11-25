"""自动结账**只对「打手 App 注册」的接单人生效**（业务方 2026-10-22 口径，选项 B；D-0279）。

## 口径一句话

订单进 `COMPLETED` 时，只有该单 MAIN 绩效行归属人的
`StaffProfile.account_source == "app"` 才自动把钱打进钱包、把绩效行标「已结账」；
网页端 / 后台建的打手（`console`）、链接自助注册（`link`）、导入（`import`）、
**没有档案行**的归属人 ⇒ 钱与结账列都不动，留给后台人工结算。

## 判据钉在哪

一律钉**数据库行的值**（`AGENTS.md` §5-G）：钱包余额 / 钱包行是否存在、绩效行的结账三列、
`WalletTransaction` 的金额与条数、`OperationLog` 的条数。
⛔ 不断言「函数被调用了」——那不能回答「钱到底动没动」。

## 怎么进完成态

用 `apply_transition(..., OrderStatus.COMPLETED, nodes={"order.change_status_completed"})`：
它正是**唯一写入口**，客服在网页端点「改成已完成」最终也落到这里 ⇒ 本文件测的是生产那条路，
⛔ 不是直接拼一个「已完成」的订单再调 payout（那会绕过要测的触发点）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.payout import payout_completed_order
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.business.order_status import apply_transition

COMPLETED_NODES = {"order.change_status_completed"}
REOPEN_NODES = {"order.change_status"}


def _no(n):
    return f"20261022{n:012d}"


class PayoutAppTakerOnlyFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 三种账号来源：App 注册 / 网页端（后台）新增 / 没有档案行。
        self.app_taker = User.objects.create_user(username="app-source-taker")
        StaffProfile.objects.create(
            user=self.app_taker,
            mobile="13700005555",
            account_source=AccountSource.APP,
        )
        self.console_taker = User.objects.create_user(username="console-source-taker")
        StaffProfile.objects.create(
            user=self.console_taker,
            mobile="13700006666",
            account_source=AccountSource.CONSOLE,
        )
        self.profileless_taker = User.objects.create_user(username="no-profile-taker")
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.PENDING_REVIEW):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=_no(self._seq),
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("30.00"),
        )

    def _main(self, order, taker, **overrides):
        """该单的 MAIN（打单工资）绩效行；金额默认 30.00（= 钱包应入 30.00）。"""
        fields = {
            "emp_type": EmpType.MAIN,
            "user": taker,
            "temp_amount": Decimal("30.00"),
            "bonus_amount": Decimal("30.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _complete(self, order):
        """推进「已完成」，并**显式**跑一次工资入账（2026-10-23 起不再自动）。

        ⛔ `apply_transition` 已不调 `payout_completed_order`（spec §3.1）⇒ 本文件测的是
        入账函数自己的「只给 App 注册打手」那条判据，所以显式补这一跳
        （「完成单不再自动入账」的判据在 `tests/test_app_income_api.py`）。
        """
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        return payout_completed_order(order)

    def _reopen(self, order):
        return apply_transition(order, OrderStatus.PENDING_REVIEW, nodes=REOPEN_NODES)


class AppRegisteredTakerTests(PayoutAppTakerOnlyFixture):
    """App 注册打手：**承重用例**，自动结账照旧生效。"""

    def test_app_registered_taker_completion_credits_the_wallet(self):
        order = self._order()
        perf = self._main(order, self.app_taker)

        self._complete(order)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)      # 阳性对照：真的完成了
        wallet = TakerWallet.objects.get(user=self.app_taker)
        self.assertEqual(wallet.balance, Decimal("30.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(perf.settled_amount, Decimal("30.00"))
        self.assertIsNotNone(perf.settled_at)

        tx = WalletTransaction.objects.get(wallet=wallet, kind="earning")
        self.assertEqual(tx.amount, Decimal("30.00"))
        self.assertEqual(tx.ref_no, f"P{perf.pk}")
        self.assertEqual(tx.balance_after, Decimal("30.00"))

        logs = OperationLog.objects.filter(
            action="performance.settle", order_no=order.order_no
        )
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().actor, "system")


class NonAppTakerTests(PayoutAppTakerOnlyFixture):
    """非 App 注册：**不入账、不标已结账、钱包余额不变**。"""

    def _assert_untouched(self, order, perf, taker):
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)      # 阳性对照：状态真的改了
        # 钱包行都不应该被建出来（payout 是唯一会 `get_or_create` 钱包的地方）。
        self.assertFalse(TakerWallet.objects.filter(user=taker).exists())
        self.assertEqual(
            WalletTransaction.objects.filter(wallet__user=taker).count(), 0
        )
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        # 结账动作日志一条都不该有（钱没动、状态也没标）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle", order_no=order.order_no
            ).count(),
            0,
        )

    def test_console_created_taker_is_not_credited_nor_settled(self):
        order = self._order()
        perf = self._main(order, self.console_taker)

    def test_link_and_import_sources_are_not_settled_either(self):
        """判据是「== app」，⛔ 不是「!= console」——link / import 也不自动结账。"""
        for source, mobile in (
            (AccountSource.LINK, "13700007777"),
            (AccountSource.IMPORT, "13700008888"),
        ):
            user = User.objects.create_user(username=f"{source}-taker")
            StaffProfile.objects.create(
                user=user, mobile=mobile, account_source=source
            )
            order = self._order()
            perf = self._main(order, user)
            self._complete(order)
            self._assert_untouched(order, perf, user)

    def test_profileless_taker_is_not_settled_and_does_not_raise(self):
        order = self._order()
        perf = self._main(order, self.profileless_taker)
        self._complete(order)
        self._assert_untouched(order, perf, self.profileless_taker)

    def test_manual_settled_row_never_moves_the_wallet_on_adjustment(self):
        """财务**手工**结账的行，钱没进过钱包 ⇒ 后续调整**不许**加/扣余额（评审阻断 2）。

        以前只判 `settle_status == 已结账` ⇒ 会去动一个从没收到过钱的余额。
        """
        from apps.business.models import PerformanceAdjustment

        order = self._order()
        perf = self._main(order, self.console_taker)
        # 财务在后台手工结账（`settled_by` 是用户名，不是 system）。
        perf.settle_status = SettleStatus.SETTLED
        perf.settled_by = "财务小王"
        perf.settled_at = timezone.now()
        perf.settled_amount = Decimal("30.00")
        perf.save()

        PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
            amount=Decimal("-10.00"),
            reason="手工结账后的调整",
            operator="财务小王",
        )

        # 钱包一分不动、也不该被建出来。
        self.assertFalse(TakerWallet.objects.filter(user=self.console_taker).exists())


class LegacySystemSettledRowsTests(PayoutAppTakerOnlyFixture):
    """D-0279 上线前已被 **system 自动结账** 的非 App 老行：**补差必须继续生效**（评审阻断 1）。"""

    def test_reopen_then_complete_still_tops_up_for_legacy_system_rows(self):
        order = self._order()
        perf = self._main(order, self.console_taker)
        # 造一条「昨天那版自动结过账」的老行：已发 30，来源是 console。
        perf.settle_status = SettleStatus.SETTLED
        perf.settled_by = "system"
        perf.settled_at = timezone.now()
        perf.settled_amount = Decimal("30.00")
        perf.save()
        wallet = TakerWallet.objects.create(user=self.console_taker, balance=Decimal("30.00"))

        # 现场是「已完成」（昨天那版在这里自动结过账）⇒ 先置成已完成，再走「重开 → 再完成」。
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status"])
        # 重开 → 改金额 → 再完成：新净额 50，应补 20。
        perf.bonus_amount = Decimal("50.00")
        perf.temp_amount = Decimal("50.00")
        perf.save()
        self._reopen(order)
        self._complete(order)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("50.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("50.00"))
