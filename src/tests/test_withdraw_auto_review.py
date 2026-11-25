"""超时自动审核（2026-10-21，提现改造增量 §2.1）。

判据钉在**数据库行**上：

- 到期的 `pending` ⇒ `approved` + `payout_after = approved_at + 3 天`；
- 未到期 / 早于 `WITHDRAW_AUTO_REVIEW_SINCE` 的存量单 / 配置为空 ⇒ **一行都不动**；
- 自动通过必须留一条 `finance.withdraw_approve` 审计（`system=True`）；
- 自动审核不受 `WITHDRAW_AUTO_PAYOUT`（自动打款开关）影响。
"""
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apps.accounts.wallet import auto_approve_due_withdrawals
from apps.audit.models import OperationLog
from tests.test_withdraw_payout_command import PayoutCommandFixture


class AutoReviewTests(PayoutCommandFixture):
    def _pending_aged(self, *, hours):
        order = self._pending()
        # `created_at` 是 `auto_now_add`，测试里用 `update()` 把它挪到过去。
        from apps.accounts.models import WithdrawOrder

        WithdrawOrder.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(hours=hours)
        )
        order.refresh_from_db()
        return order

    @override_settings(
        WITHDRAW_AUTO_REVIEW_SINCE=(timezone.now() - timedelta(days=2)).isoformat(),
        WITHDRAW_PAYOUT_DELAY_MINUTES=4320,
    )
    def test_a_due_pending_order_is_auto_approved(self):
        order = self._pending_aged(hours=25)

        approved = auto_approve_due_withdrawals()

        self.assertEqual(approved, 1)
        order.refresh_from_db()
        self.assertEqual(order.state, "approved")
        self.assertIsNone(order.reviewer_id)
        self.assertIsNotNone(order.approved_at)
        self.assertEqual(order.approved_at, order.reviewed_at)
        self.assertEqual(order.payout_after, order.approved_at + timedelta(days=3))
        logs = OperationLog.objects.filter(action="finance.withdraw_approve")
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs[0].reason, "超时自动通过")
        self.assertEqual(logs[0].to_value, "10.00")

    @override_settings(
        WITHDRAW_AUTO_REVIEW_SINCE=(timezone.now() - timedelta(days=2)).isoformat()
    )
    def test_a_recent_pending_order_is_not_approved(self):
        order = self._pending_aged(hours=1)

        self.assertEqual(auto_approve_due_withdrawals(), 0)

        order.refresh_from_db()
        self.assertEqual(order.state, "pending")

    @override_settings(
        WITHDRAW_AUTO_REVIEW_SINCE=(timezone.now() - timedelta(hours=2)).isoformat()
    )
    def test_a_pending_order_created_before_since_is_not_approved(self):
        order = self._pending_aged(hours=25)

        self.assertEqual(auto_approve_due_withdrawals(), 0)

        order.refresh_from_db()
        self.assertEqual(order.state, "pending")

    @override_settings(WITHDRAW_AUTO_REVIEW_SINCE="")
    def test_an_empty_since_disables_auto_review(self):
        order = self._pending_aged(hours=25)

        self.assertEqual(auto_approve_due_withdrawals(), 0)

        order.refresh_from_db()
        self.assertEqual(order.state, "pending")

    @override_settings(
        WITHDRAW_AUTO_REVIEW_SINCE=(timezone.now() - timedelta(days=2)).isoformat()
    )
    def test_the_command_reviews_even_when_auto_payout_is_off(self):
        from apps.business.models import SystemSwitch

        SystemSwitch.objects.create(key="WITHDRAW_AUTO_PAYOUT", value="N")
        order = self._pending_aged(hours=25)

        self._run()

        order.refresh_from_db()
        self.assertEqual(order.state, "approved")
