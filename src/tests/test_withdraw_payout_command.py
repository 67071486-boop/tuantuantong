"""二期：押金提现的冷静期 + 自动代付命令（2026-10-21，`feat/wallet-single-payout` spec §2）。

## 判据钉在哪

- `approve_withdraw` ⇒ 数据库行上的 `approved_at` / `payout_after` 的值（不是「函数被调了」）；
- 命令 ⇒ **提现单的状态 / `transfer_attempts` / `transfer_error`**，以及上游调用次数；
- 开关关 ⇒ 上游一次都没被调。

## 规格 §4 的二期四条

8. `approve_withdraw` 写 `payout_after = approved_at + 冷静期`（默认 7 天；可配 1 分钟）；
9. 命令：到期的发、未到期的不发、有进行中单的跳过、开关关时一条都不发；
10. 代付失败 ⇒ 重试计数递增、原因落库，到上限不再发起。
"""
import io
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import (
    StaffProfile,
    TakerWallet,
    WalletTransaction,
    WithdrawOrder,
)
from apps.accounts.wallet import approve_withdraw
from apps.audit.models import OperationLog
from apps.accounts.jeepay import JeepayError
from apps.business.models import SystemSwitch
from tests.p5_auth_helpers import make_logged_in_client

ADMIN_USERNAME = "payout-command-admin"


class PayoutCommandFixture(TenantTestCase):
    def setUp(self):
        # 结算批二 P1-4：`start_transfer` 现在自己读代付总开关，本文件
        # 九条都在验命令的取数 / 跳过 / 重试语义，按「开关开」跑。
        # ⚠️ 不用类装饰器：`TenantTestCase` 的 `setUpClass` 不走 `SimpleTestCase` 那套
        # 注入（同 `test_withdraw_review_api.py` 的注释），必须每个用例在 `_run` 外启用。
        overrides = override_settings(WITHDRAW_AUTO_TRANSFER=True)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.taker = User.objects.create_user(username="payout-cmd-taker")
        StaffProfile.objects.create(
            user=self.taker,
            mobile="13700003333",
            alipay_account="cmd@example.com",
            alipay_name="测试打手",
        )
        make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        self.admin = User.objects.get(username=ADMIN_USERNAME)

    def _pending(self, amount="10.00"):
        return WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal(amount),
            alipay_account="cmd@example.com",
            alipay_name="测试打手",
        )

    def _approved(self, *, payout_after, amount="10.00", user=None):
        return WithdrawOrder.objects.create(
            user=user or self.taker,
            amount=Decimal(amount),
            alipay_account="cmd@example.com",
            alipay_name="测试打手",
            state=WithdrawOrder.STATE_APPROVED,
            approved_at=timezone.now(),
            reviewed_at=timezone.now(),
            payout_after=payout_after,
        )

    def _run(self):
        """跑一遍命令（租户清单打成当前测试 schema，`tenant_context` 做成空操作）。"""
        from apps.accounts.management.commands import payout_due_withdrawals as module

        out, err = io.StringIO(), io.StringIO()
        tenant = SimpleNamespace(schema_name="test")
        with mock.patch.object(module, "Tenant") as tenant_model, \
             mock.patch.object(module, "tenant_context", lambda t: nullcontext()):
            tenant_model.objects.exclude.return_value.order_by.return_value = [tenant]
            call_command("payout_due_withdrawals", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _fake_upstream(self, *, state="1"):
        """把 Jeepay 配置自检与发起代付都换成桩（`state` = 上游回的转账状态）。

        ⚠️ 桩打在 `apps.accounts.wallet`（`start_transfer` **查找**它的模块，E-0029）。
        """
        return (
            mock.patch(
                "apps.accounts.wallet.ensure_configured", return_value=None
            ),
            mock.patch(
                "apps.accounts.wallet.create_transfer",
                return_value={"state": state, "transferId": "T-1"},
            ),
        )


class CoolingPeriodTests(PayoutCommandFixture):
    def test_approve_writes_payout_after_three_days_by_default(self):
        order = self._pending()

        approve_withdraw(order, operator=self.admin)

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertIsNotNone(order.approved_at)
        self.assertEqual(order.approved_at, order.reviewed_at)
        self.assertEqual(
            order.payout_after, order.approved_at + timedelta(days=3)
        )

    @override_settings(WITHDRAW_PAYOUT_DELAY_MINUTES=1)
    def test_the_cooling_period_is_configurable_in_minutes(self):
        order = self._pending()

        approve_withdraw(order, operator=self.admin)

        order.refresh_from_db()
        self.assertEqual(
            order.payout_after, order.approved_at + timedelta(minutes=1)
        )


class AutoPayoutCommandTests(PayoutCommandFixture):
    def test_a_due_order_is_sent_once(self):
        order = self._approved(payout_after=timezone.now() - timedelta(minutes=1))
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            self._run()
            # 第二轮：已经进入 `paying` ⇒ 取数条件不再命中，⛔ 不重复发。
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)
        self.assertTrue(order.mch_order_no)
        self.assertEqual(create_transfer.call_count, 1)
        self.assertEqual(order.transfer_attempts, 0)

    def test_an_order_inside_the_cooling_period_is_not_sent(self):
        order = self._approved(payout_after=timezone.now() + timedelta(minutes=5))
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        create_transfer.assert_not_called()

    def test_a_user_with_another_active_withdrawal_is_skipped(self):
        order = self._approved(payout_after=timezone.now() - timedelta(minutes=1))
        other = self._pending()  # 同一用户的另一张进行中提现单
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            out, _err = self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        create_transfer.assert_not_called()
        self.assertIn(f"提现单 {order.pk} 跳过", out)
        self.assertEqual(other.state, WithdrawOrder.STATE_PENDING)

    def test_a_historical_fast_lane_row_no_longer_jumps_the_queue(self):
        """⛔ 2026-10-23（spec §8.2）：**快路豁免已删除**，历史行也不例外。

        历史 `fast_lane=True` 的行（列保留、行不动）今天与普通单走**同一条**跳过判据：
        同一用户还有进行中的提现 ⇒ 本轮跳过。判别力：把 `payout_due_withdrawals` 里那句
        `if not order.fast_lane and ...` 恢复回来 ⇒ 本用例红（历史行会被付掉）。
        """
        other = self._pending()
        legacy_fast = WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal("100.00"),
            alipay_account="cmd@example.com",
            alipay_name="测试打手",
            fast_lane=True,
            state=WithdrawOrder.STATE_APPROVED,
            approved_at=timezone.now(),
            reviewed_at=timezone.now(),
            payout_after=timezone.now() - timedelta(minutes=1),
        )
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            out, _err = self._run()

        legacy_fast.refresh_from_db()
        self.assertEqual(legacy_fast.state, WithdrawOrder.STATE_APPROVED)   # 没被付
        create_transfer.assert_not_called()
        self.assertIn(f"提现单 {legacy_fast.pk} 跳过", out)
        other.refresh_from_db()
        self.assertEqual(other.state, WithdrawOrder.STATE_PENDING)

    def test_the_switch_off_sends_nothing(self):
        SystemSwitch.objects.create(key="WITHDRAW_AUTO_PAYOUT", value="N")
        order = self._approved(payout_after=timezone.now() - timedelta(minutes=1))
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            out, _err = self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        create_transfer.assert_not_called()
        self.assertIn("一条都不发", out)

    def test_a_failed_transfer_increments_the_attempts_and_keeps_the_reason(self):
        order = self._approved(payout_after=timezone.now() - timedelta(minutes=1))
        with mock.patch(
            "apps.accounts.wallet.ensure_configured",
            side_effect=JeepayError("代付未配置"),
        ):
            _out, err = self._run()
            self._run()

        order.refresh_from_db()
        # 配置失败发生在改状态之前 ⇒ 单子停在 `approved`，下一轮还能重试。
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertEqual(order.transfer_attempts, 2)
        self.assertIn("代付未配置", order.transfer_error)
        self.assertIn("代付失败", err)

    def test_an_order_at_the_attempt_limit_is_not_sent_again(self):
        order = self._approved(payout_after=timezone.now() - timedelta(minutes=1))
        order.transfer_attempts = 5
        order.save(update_fields=["transfer_attempts"])

        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertEqual(order.transfer_attempts, 5)
        create_transfer.assert_not_called()


class RefundDebtOffsetTests(PayoutCommandFixture):
    """⭐ 先扣再打（2026-10-30 业务方拍板，D-0336）：发起代付前先扣该打手未了结的退款欠款。

    欠款的定义与来源见 `wallet.refund_debt`（= 钱包负余额的绝对值；生产上把余额扣成
    负的只有 `payout_adjustment` 的退款冲账）。判据打在**数据库行**上（钱包余额、
    `withdraw_offset` 流水、提现单状态 / `transfer_error` / `remark`、操作日志行）
    与上游 mock 的**调用参数**上（Jeepay 收到的 `amount`）。
    """

    def _wallet(self, balance: str) -> TakerWallet:
        wallet, _ = TakerWallet.objects.get_or_create(user=self.taker)
        wallet.balance = Decimal(balance)
        wallet.save(update_fields=["balance", "updated_at"])
        return wallet

    def _offset_txs(self, wallet):
        return list(
            WalletTransaction.objects.filter(
                wallet=wallet,
                kind=WalletTransaction.KIND_WITHDRAW_OFFSET,
            ).order_by("pk")
        )

    def test_no_debt_sends_the_full_amount_as_before(self):
        """对照（欠款 = 0）：行为与拍板前逐字一致——原额发出、零新增流水。"""
        order = self._approved(
            payout_after=timezone.now() - timedelta(minutes=1), amount="10.00"
        )
        ensure, create = self._fake_upstream(state="2")
        with ensure, create as create_transfer:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAID)
        self.assertEqual(
            create_transfer.call_args.kwargs["amount"], Decimal("10.00")
        )
        self.assertEqual(order.remark, "")
        self.assertEqual(
            WalletTransaction.objects.filter(wallet__user=self.taker).count(), 0
        )

    def test_a_debt_under_the_amount_is_offset_and_only_the_rest_is_sent(self):
        """欠款 < 单额 ⇒ 先扣再打：负余额归零、只发差额、流水与 remark 都留痕。"""
        order = self._approved(
            payout_after=timezone.now() - timedelta(minutes=1), amount="10.00"
        )
        self._wallet("-4.00")
        ensure, create = self._fake_upstream(state="2")
        with ensure, create as create_transfer:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAID)
        # Jeepay 收到的是扣完欠款的 6 元，⛔ 不是原额 10 元。
        self.assertEqual(
            create_transfer.call_args.kwargs["amount"], Decimal("6.00")
        )
        # 钱包：负余额被抵扣归零，留一条带符号的 `withdraw_offset` 流水（+4.00）。
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        txs = self._offset_txs(wallet)
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0].amount, Decimal("4.00"))
        self.assertEqual(txs[0].ref_no, f"W{order.pk}")
        # `remark` 留痕：人工兜底登记打款时操作员能看到实打金额。
        self.assertEqual(
            order.remark, "扣退款欠款 4.00 元，实打 6.00 元"
        )

    def test_a_debt_at_or_over_the_amount_holds_the_payout_and_writes_the_ledger(self):
        """欠款 ≥ 单额 ⇒ 不打：单子留在 approved、钱包一分未动、台账人话落两处。"""
        order = self._approved(
            payout_after=timezone.now() - timedelta(minutes=1), amount="10.00"
        )
        self._wallet("-12.00")
        ensure, create = self._fake_upstream()
        with ensure, create as create_transfer:
            out, err = self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        create_transfer.assert_not_called()
        # 命令侧的失败闸照走：计数 +1、原因落库（5 轮后停手留人工）。
        self.assertEqual(order.transfer_attempts, 1)
        self.assertIn("退款欠款", order.transfer_error)
        self.assertIn("退款欠款", err)
        # 钱包一分未动、一条抵扣流水都没有。
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("-12.00"))
        self.assertEqual(self._offset_txs(wallet), [])
        # 台账人话：操作日志一行（`finance.withdraw_transfer` 族，reason 指名欠款）。
        log = OperationLog.objects.get(
            action="finance.withdraw_transfer", target_id=str(order.pk)
        )
        self.assertIn("退款欠款", log.reason)
        self.assertIn("暂停打款", log.reason)

    def test_a_sync_failed_transfer_re_owes_the_offset(self):
        """同步失败（state=3/4）⇒ 退款退回单据全额，先扣的欠款要**记回**欠着。

        账：发起抵扣 +4（余额 −4 → 0）→ 失败退款 +10 → 记回 −4 ⇒ 6.00
        = 单额 10 − 欠款 4（欠款继续欠着，下一轮再先扣）。
        """
        order = self._approved(
            payout_after=timezone.now() - timedelta(minutes=1), amount="10.00"
        )
        self._wallet("-4.00")
        ensure, create = self._fake_upstream(state="3")
        with ensure, create as create_transfer:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_FAILED)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("6.00"))
        # 两条带符号流水：发起抵扣 +4.00、同步失败记回 −4.00（同 ref、同 kind）。
        self.assertEqual(
            [tx.amount for tx in self._offset_txs(wallet)],
            [Decimal("4.00"), Decimal("-4.00")],
        )
