"""结算批二 P1-3 / P1-4：登记打款的跨事务复查 + 代付总开关（2026-10-22）。

## P1-3 的窗口是怎么打开的

`mark_withdraw_paid` 故意分成两段短事务（中间隔着 HTTP `query_transfer`），
所以「首段已提交、终段还没开始」是一个真实存在的时间窗口。本文件用
`_AtomicHook` 把**第一次** `transaction.atomic()` 的出口接上回调：在测试里
从那个窗口插一次 `start_transfer`，模拟另一条路（定时命令 / 别的客服）刚好
把同一张单推进 `paying`。

判据打在**数据库行**上：单子停在 `paying`、`paid_at` 仍为空、`remark`
没有被人工单号改写——⛔ 不是「函数抛错了就算」。

## P1-4 的判据

开关关 ⇒ 到点 `approved` 单不变 `paying`、上游（`ensure_configured` /
`create_transfer`）**零调用**；开关开 ⇒ 照旧发起。
"""
import io
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts import wallet
from apps.accounts.jeepay import JeepayError
from apps.accounts.models import StaffProfile, WithdrawOrder
from apps.accounts.wallet import WalletError, mark_withdraw_paid, start_transfer
from apps.core.error_codes import ErrorCode

TAKER = "p1-guard-taker"
OPERATOR = "p1-guard-operator"


class _AtomicHook:
    """把 `wallet.transaction.atomic` 包一层：**第一次成功退出后**跑一次回调。

    ⚠️ 只在第一次退出时触发，且跑回调前先置 `_fired`：`start_transfer` 自己也会
    用 `transaction.atomic()`，不能让回调递归进去。
    """

    def __init__(self, after_first):
        self._after_first = after_first
        self._fired = False
        self.calls = 0

    def __call__(self):
        inner = transaction.atomic()
        hook = self
        call_no = self.calls
        self.calls += 1

        class _Managed:
            def __enter__(self):
                return inner.__enter__()

            def __exit__(self, exc_type, exc, tb):
                result = inner.__exit__(exc_type, exc, tb)
                if call_no == 0 and exc_type is None and not hook._fired:
                    hook._fired = True
                    hook._after_first()
                return result

        return _Managed()


class WithdrawGuardFixture(TenantTestCase):
    def setUp(self):
        self.taker = User.objects.create_user(username=TAKER)
        StaffProfile.objects.create(
            user=self.taker,
            mobile="13700004444",
            alipay_account="p1-guard@example.com",
            alipay_name="测试打手",
        )
        self.operator = User.objects.create_user(username=OPERATOR)

    def _approved(self, amount="10.00"):
        return WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal(amount),
            alipay_account="p1-guard@example.com",
            alipay_name="测试打手",
            state=WithdrawOrder.STATE_APPROVED,
            approved_at=timezone.now(),
            reviewed_at=timezone.now(),
            payout_after=timezone.now() - timedelta(minutes=1),
        )


class MarkWithdrawPaidCrossTransactionTests(WithdrawGuardFixture):
    """P1-3：首段读到的状态不能代表终段看到的状态。"""

    def test_manual_mark_is_rejected_when_auto_transfer_starts_between_the_two_transactions(
        self,
    ):
        order = self._approved("10.00")
        started = []

        def interleave():
            started.append(
                start_transfer(
                    order,
                    operator=None,
                    client_ip="127.0.0.1",
                    confirm_amount=order.amount,
                )
            )

        hook = _AtomicHook(interleave)
        with mock.patch.object(wallet, "transaction", SimpleNamespace(atomic=hook)), \
             mock.patch("apps.accounts.wallet.ensure_configured", return_value=None), \
             mock.patch(
                 "apps.accounts.wallet.create_transfer",
                 return_value={"state": "1", "transferId": "T-1"},
             ) as create, \
             override_settings(WITHDRAW_AUTO_TRANSFER=True):
            with self.assertRaises(WalletError) as caught:
                mark_withdraw_paid(
                    order, operator=self.operator, transfer_no="ALI-1"
                )

        # 窗口真的被打开了：代付那一步成功落成 `paying`（不是「桩没接上」）。
        self.assertEqual(len(started), 1)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)
        self.assertIn("正在自动打款", str(caught.exception))

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)
        self.assertIsNone(order.paid_at)
        self.assertNotIn("ALI-1", order.remark or "")

    @mock.patch(
        "apps.accounts.wallet.query_transfer",
        side_effect=JeepayError("订单不存在（code=1002）"),
    )
    def test_manual_mark_is_rejected_when_the_inflight_marker_appears_mid_window(
        self, query
    ):
        order = self._approved("10.00")
        # 首段读到的 `paying` 不在途（窗口已过）⇒ 现有闸放行、会去查单。
        WithdrawOrder.objects.filter(pk=order.pk).update(
            state=WithdrawOrder.STATE_PAYING,
            mch_order_no=f"TW{order.pk}-aaaaaa",
        )
        order.refresh_from_db()

        def interleave():
            # 模拟另一条路在窗口里写上了「我方 HTTP 在途」。
            WithdrawOrder.objects.filter(pk=order.pk).update(
                transfer_started_at=timezone.now()
            )

        hook = _AtomicHook(interleave)
        with mock.patch.object(wallet, "transaction", SimpleNamespace(atomic=hook)):
            with self.assertRaises(WalletError) as caught:
                mark_withdraw_paid(
                    order, operator=self.operator, transfer_no="ALI-2"
                )

        self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)
        self.assertIn("正在自动打款", str(caught.exception))
        query.assert_called_once_with(order.mch_order_no)

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)
        self.assertIsNotNone(order.transfer_started_at)
        self.assertIsNone(order.paid_at)
        self.assertNotIn("ALI-2", order.remark or "")


class AutoTransferSwitchGateTests(WithdrawGuardFixture):
    """P1-4：总开关关着时，既不能发起代付，也不能留下「已尝试」痕迹。"""

    def _run(self):
        """跑一遍 `payout_due_withdrawals`（同 `test_withdraw_payout_command` 的隔离法）。"""
        from apps.accounts.management.commands import payout_due_withdrawals as module

        out, err = io.StringIO(), io.StringIO()
        tenant = SimpleNamespace(schema_name="test")
        with mock.patch.object(module, "Tenant") as tenant_model, \
             mock.patch.object(module, "tenant_context", lambda t: nullcontext()):
            tenant_model.objects.exclude.return_value.order_by.return_value = [tenant]
            call_command("payout_due_withdrawals", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _upstream(self):
        return (
            mock.patch("apps.accounts.wallet.ensure_configured", return_value=None),
            mock.patch(
                "apps.accounts.wallet.create_transfer",
                return_value={"state": "1", "transferId": "T-1"},
            ),
        )

    def test_switch_off_is_a_clean_skip_with_no_failure_traces(self):
        order = self._approved("10.00")
        ensure, create = self._upstream()

        with override_settings(WITHDRAW_AUTO_TRANSFER=False), ensure, create as create_transfer:
            out, _err = self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertIsNone(order.transfer_started_at)
        # 干净跳过：既不记失败次数、也不写失败原因（否则「关一周再开」会静默挂单）。
        self.assertEqual(order.transfer_attempts, 0)
        self.assertEqual(order.transfer_error, "")
        create_transfer.assert_not_called()
        self.assertIn("WITHDRAW_AUTO_TRANSFER=关", out)
        self.assertNotIn("已发起代付", out)
        self.assertNotIn("代付失败", out)

    def test_switch_off_then_on_pays_the_same_order_on_the_next_round(self):
        order = self._approved("10.00")

        off_ensure, off_create = self._upstream()
        with override_settings(WITHDRAW_AUTO_TRANSFER=False), off_ensure, off_create as create_off:
            self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertEqual(order.transfer_attempts, 0)
        self.assertEqual(order.transfer_error, "")
        create_off.assert_not_called()

        on_ensure, on_create = self._upstream()
        with override_settings(WITHDRAW_AUTO_TRANSFER=True), on_ensure, on_create as create_on:
            out, _err = self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)
        self.assertEqual(order.transfer_attempts, 0)
        create_on.assert_called_once()
        self.assertIn("已发起代付", out)

    def test_start_transfer_itself_refuses_when_the_switch_is_off(self):
        order = self._approved("10.00")

        with override_settings(WITHDRAW_AUTO_TRANSFER=False), \
             mock.patch(
                 "apps.accounts.wallet.ensure_configured", return_value=None
             ) as ensure, \
             mock.patch("apps.accounts.wallet.create_transfer") as create, \
             mock.patch.object(wallet.logger, "warning") as warning:
            with self.assertRaises(WalletError) as caught:
                start_transfer(
                    order,
                    operator=None,
                    client_ip="127.0.0.1",
                    confirm_amount=order.amount,
                )

        self.assertEqual(caught.exception.code, ErrorCode.BUSINESS)
        self.assertIn("自动代付未开启", str(caught.exception))
        ensure.assert_not_called()
        create.assert_not_called()
        self.assertTrue(warning.called)
        self.assertIn("WITHDRAW_AUTO_TRANSFER", str(warning.call_args))
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)

