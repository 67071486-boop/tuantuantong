from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, PaymentStatus
from apps.wecom.external_pay import SyncError, match_payment, parse_bill, prepare_window, process_refund, pull_one_page
from apps.wecom.models import ExternalPayBill, ExternalPayState, WecomAppConfig


class ExternalPayTests(TenantTestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.config = WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id="corp-a", agent_id=1, secret="secret-a")
        self.state = ExternalPayState.objects.create(identity_revision=0, corp_id="corp-a")
        self.game = Game.objects.create(name="测试游戏")

    def row(self, **overrides):
        return {"bill_type": 0, "transaction_id": "pay-123", "mch_id": "merchant-a", "out_trade_no": "merchant-order", "total_fee": 10000, "total_refund_fee": 0, "pay_time": int((self.now - timedelta(hours=1)).timestamp()), "trade_state": 1, **overrides}

    def bill(self, **overrides):
        return ExternalPayBill.objects.create(state=self.state, **parse_bill(self.row(**overrides)))

    def order(self, no, price="100"):
        return Order.objects.create(game=self.game, order_no=no, source_order_no="pay-123", total_price=Decimal(price))

    def test_unique_amount_sets_only_matching_order_and_preserves_manual_refund_without_external_refund(self):
        a = self.order("20261113000000000001")
        b = self.order("20261113000000000002", "90")
        a.refund_fee = Decimal("5")
        a.save(update_fields=["refund_fee"])
        bill = self.bill()
        match_payment(bill.pk, self.config, self.now)
        a.refresh_from_db(); b.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(a.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(a.refund_fee, Decimal("5"))
        self.assertNotEqual(b.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(bill.order_id, a.pk)

    def test_ambiguous_matching_and_mismatch_leave_orders_untouched(self):
        a = self.order("20261113000000000001")
        b = self.order("20261113000000000002")
        bill = self.bill()
        match_payment(bill.pk, self.config, self.now)
        a.refresh_from_db(); b.refresh_from_db(); bill.refresh_from_db()
        self.assertNotEqual(a.payment_status, PaymentStatus.SETTLED)
        self.assertNotEqual(b.payment_status, PaymentStatus.SETTLED)
        self.assertIn("多张", bill.issue)
        b.total_price = Decimal("90"); b.save(update_fields=["total_price"])
        a.total_price = Decimal("80"); a.save(update_fields=["total_price"])
        match_payment(bill.pk, self.config, self.now)
        bill.refresh_from_db()
        self.assertIn("不一致", bill.issue)

    def test_known_order_changed_price_still_gets_refund_but_no_payment_status(self):
        order = self.order("20261113000000000001", "50")
        bill = self.bill(total_refund_fee=6000, trade_state=3)
        bill.order = order; bill.matched_at = self.now; bill.save(update_fields=["order", "matched_at"])
        match_payment(bill.pk, self.config, self.now)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("60"))
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIn("不一致", bill.issue)

    def test_non_integer_and_over_refund_are_rejected(self):
        for fields in ({"total_fee": "10000"}, {"total_refund_fee": 10001}, {"total_fee": True}, {"trade_state": None}):
            with self.subTest(fields=fields), self.assertRaises(SyncError):
                parse_bill(self.row(**fields))

    def test_pages_resume_and_failure_does_not_move_cursor(self):
        self.state.window_start = self.now - timedelta(days=3)
        self.state.window_end = self.now
        self.state.save()
        with patch("apps.wecom.external_pay._get_page", side_effect=[([parse_bill(self.row())], "page2"), SyncError("失败"), ([], "")]):
            pull_one_page(self.config, self.state)
            self.state.refresh_from_db()
            self.assertEqual(self.state.cursor, "page2")
            with self.assertRaises(SyncError):
                pull_one_page(self.config, self.state)
            self.state.refresh_from_db()
            self.assertEqual(self.state.cursor, "page2")
            self.assertIsNone(self.state.watermark)
            pull_one_page(self.config, self.state)
        self.state.refresh_from_db()
        self.assertEqual(self.state.watermark, self.now)
        self.assertEqual(ExternalPayBill.objects.count(), 1)

    def test_expired_unmatched_payment_does_not_change_order(self):
        bill = self.bill(pay_time=int((self.now - timedelta(days=4)).timestamp()))
        match_payment(bill.pk, self.config, self.now)
        bill.refresh_from_db()
        self.assertIsNone(bill.next_retry_at)
        self.assertIn("超过三天", bill.issue)

    def test_historical_refund_uses_merchant_identity_and_persists_search(self):
        refund = self.bill(bill_type=1, transaction_id="refund-abc", out_refund_no="out-refund", total_refund_fee=1000, trade_state=None)
        row = parse_bill(self.row(pay_time=int((self.now - timedelta(days=50)).timestamp()), total_refund_fee=1000, trade_state=3))
        with patch("apps.wecom.external_pay._get_page", side_effect=[([], ""), ([row], "")]):
            process_refund(refund.pk, self.config, self.now)
            refund.refresh_from_db()
            self.assertEqual((refund.search_end - refund.search_start).days, 28)
            process_refund(refund.pk, self.config, self.now)
        payment = ExternalPayBill.objects.get(transaction_id="pay-123")
        self.assertTrue(payment.refund_origin)
        self.assertEqual(payment.mch_id, refund.mch_id)
        self.assertEqual(payment.out_trade_no, refund.out_trade_no)
        self.assertNotEqual(payment.transaction_id, refund.transaction_id)

    def test_restart_after_long_gap_uses_bounded_window(self):
        self.state.watermark = self.now - timedelta(days=100)
        self.state.save()
        self.assertTrue(prepare_window(self.config, self.state, self.now))
        self.state.refresh_from_db()
        self.assertEqual(self.state.window_end - self.state.window_start, timedelta(days=28))

    def test_failed_refund_write_rolls_back_payment_and_keeps_bill_unmatched(self):
        from apps.business.order_writes import WriteOutcome
        order = self.order("20261113000000000001")
        bill = self.bill(total_refund_fee=1000, trade_state=3)
        with patch("apps.wecom.external_pay.order_writes.refund_total_write", return_value=WriteOutcome(ok=False)):
            with self.assertRaises(SyncError):
                match_payment(bill.pk, self.config, self.now)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(order.refund_fee, Decimal("0"))
        self.assertIsNone(bill.order_id)

    def test_changed_credentials_prevent_inflight_payment_write(self):
        order = self.order("20261113000000000001")
        bill = self.bill()
        WecomAppConfig.objects.filter(pk=self.config.pk).update(identity_revision=1, secret="secret-b")
        with self.assertRaises(SyncError):
            match_payment(bill.pk, self.config, self.now)
        order.refresh_from_db()
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)

    def test_refund_candidate_is_not_applied_until_all_pages_checked(self):
        event = self.bill(bill_type=1, transaction_id="refund-abc", out_refund_no="out-refund", total_refund_fee=1000, trade_state=None)
        a = parse_bill(self.row(total_refund_fee=1000, trade_state=3))
        b = parse_bill(self.row(transaction_id="other-payment", total_refund_fee=1000, trade_state=3))
        with patch("apps.wecom.external_pay._get_page", side_effect=[([a], "next"), ([b], "")]):
            process_refund(event.pk, self.config, self.now)
            self.assertFalse(ExternalPayBill.objects.filter(bill_type=0).exists())
            process_refund(event.pk, self.config, self.now)
        event.refresh_from_db()
        self.assertIn("多笔付款", event.issue)
        self.assertFalse(ExternalPayBill.objects.filter(bill_type=0).exists())

    def test_two_bills_cannot_overwrite_same_order(self):
        order = self.order("20261113000000000001")
        first = self.bill()
        match_payment(first.pk, self.config, self.now)
        second = self.bill(mch_id="other-merchant", out_trade_no="other-order", total_refund_fee=1000, trade_state=3)
        match_payment(second.pk, self.config, self.now)
        order.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0"))
        self.assertIn("另一笔", second.issue)

    def test_refund_amount_does_not_move_backwards(self):
        from apps.wecom.external_pay import _save_bill
        from django.db import transaction
        bill = self.bill(total_refund_fee=2000, trade_state=3)
        with transaction.atomic():
            _save_bill(self.state, parse_bill(self.row(total_refund_fee=1000, trade_state=3)))
        bill.refresh_from_db()
        self.assertEqual(bill.total_refund_fee, 2000)

    def test_settings_api_respects_permission_and_returns_amount(self):
        from tests.p5_auth_helpers import make_logged_in_client
        denied = make_logged_in_client(username="payment-denied")
        permitted = make_logged_in_client(username="payment-settings", groups=("客服",), nodes=("system.wecom_settings",))
        bill = self.bill()
        bill.issue = "金额不一致"; bill.save(update_fields=["issue"])
        denied_body = denied.get("/api/wecom/external-pay/issues/").json()
        self.assertNotEqual(denied_body["code"], 0)
        body = permitted.get("/api/wecom/external-pay/issues/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"][0]["payment_amount"], "100.00")
        self.assertEqual(body["data"]["items"][0]["bill_type"], 0)
        self.assertNotEqual(permitted.get("/api/wecom/external-pay/issues/", {"page": "oops"}).status_code, 500)

    def test_credentials_cannot_read_other_tenant_bill(self):
        from django_tenants.utils import tenant_context
        from apps.platform.provisioning import provision_tenant
        from tests.p5_auth_helpers import make_logged_in_client
        own = make_logged_in_client(username="payment-tenant-a", groups=("客服",), nodes=("system.wecom_settings",))
        other, _ = provision_tenant(schema_name="external_payment_b", name="合成乙", domain="external-payment-b.test.com")
        with tenant_context(other):
            state = ExternalPayState.objects.create(identity_revision=0, corp_id="other-corp")
            WecomAppConfig.objects.create(tenant_schema=other.schema_name, corp_id="other-corp", agent_id=1, secret="other-secret", enabled=True)
            ExternalPayBill.objects.create(state=state, **parse_bill(self.row(transaction_id="private-b-order")), issue="需人工处理")
        own_bill = self.bill(transaction_id="own-a-order")
        own_bill.issue = "需人工处理"
        own_bill.save(update_fields=["issue"])
        data = own.get("/api/wecom/external-pay/issues/").json()["data"]["items"]
        self.assertEqual([row["transaction_id"] for row in data], ["own-a-order"])
        try:
            response = own.get("/api/wecom/external-pay/issues/", HTTP_HOST="external-payment-b.test.com")
            if response.status_code == 200:
                self.assertNotEqual(response.json()["code"], 0)
            else:
                self.assertIn(response.status_code, (401, 403, 404))
        finally:
            connection.set_tenant(self.tenant)

    def test_issue_hides_bill_and_order_for_games_outside_scope(self):
        from tests.p5_auth_helpers import make_logged_in_client
        self.order("20261113000000000001")
        hidden = self.bill()
        hidden.issue = "金额不一致"; hidden.save(update_fields=["issue"])
        unmatched = self.bill(transaction_id="unmatched-other")
        unmatched.issue = "尚未匹配到订单"; unmatched.save(update_fields=["issue"])
        restricted = make_logged_in_client(username="payment-no-games", groups=("客服",), nodes=("system.wecom_settings",), games=[])
        body = restricted.get("/api/wecom/external-pay/issues/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([row["transaction_id"] for row in body["data"]["items"]], ["unmatched-other"])

    def test_local_matches_continue_when_fetch_fails(self):
        from apps.wecom.external_pay import run_due
        order = self.order("20261113000000000001")
        bill = self.bill(); bill.next_retry_at = self.now; bill.save(update_fields=["next_retry_at"])
        with patch("apps.wecom.external_pay._get_page", side_effect=SyncError("拉取失败")):
            with self.assertRaises(SyncError):
                run_due(self.now)
        order.refresh_from_db(); self.state.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIn("拉取失败", self.state.last_error)

    def test_bad_bill_is_quarantined_without_blocking_valid_page(self):
        from apps.wecom.external_pay import _safe_invalid
        from apps.wecom.models import ExternalPayInvalidBill
        self.state.window_start = self.now - timedelta(days=1); self.state.window_end = self.now; self.state.save()
        bad = _safe_invalid(self.row(transaction_id="bad", total_fee="not-an-integer"), "金额不合法")
        with patch("apps.wecom.external_pay._get_page", return_value=([bad, parse_bill(self.row())], "")):
            pull_one_page(self.config, self.state)
        self.state.refresh_from_db()
        self.assertEqual(self.state.watermark, self.now)
        self.assertTrue(ExternalPayBill.objects.filter(transaction_id="pay-123").exists())
        self.assertEqual(ExternalPayInvalidBill.objects.count(), 1)
        from tests.p5_auth_helpers import make_logged_in_client
        client = make_logged_in_client(username="invalid-bill-admin", is_superuser=True)
        self.assertIn("1 条企微账单字段异常", client.get("/api/wecom/external-pay/status/").json()["data"]["last_error"])

    def test_unrelated_bad_bill_does_not_block_historical_refund(self):
        from apps.wecom.external_pay import _safe_invalid
        from apps.wecom.models import ExternalPayInvalidBill
        event = self.bill(bill_type=1, transaction_id="refund-abc", out_refund_no="out-refund", total_refund_fee=1000, trade_state=None)
        bad = _safe_invalid(self.row(transaction_id="other", mch_id="other-merchant", total_fee="bad"), "金额不合法")
        payment = parse_bill(self.row(total_refund_fee=1000, trade_state=3))
        with patch("apps.wecom.external_pay._get_page", return_value=([bad, payment], "")):
            process_refund(event.pk, self.config, self.now)
        self.assertEqual(ExternalPayInvalidBill.objects.count(), 1)
        self.assertTrue(ExternalPayBill.objects.filter(transaction_id="pay-123", refund_origin=True).exists())

    def test_zero_time_is_invalid(self):
        with self.assertRaises(SyncError):
            parse_bill(self.row(pay_time=0))

    def test_recheck_notice_requeues_matching_payment(self):
        from apps.wecom.external_pay import _process_rechecks, enqueue_order_refund_recheck
        from apps.wecom.models import ExternalPayRecheck
        order = self.order("20261113000000000001")
        bill = self.bill(); match_payment(bill.pk, self.config, self.now)
        bill.refresh_from_db(); self.assertIsNone(bill.next_retry_at)
        enqueue_order_refund_recheck(order)
        _process_rechecks(self.state, self.now)
        bill.refresh_from_db()
        self.assertEqual(bill.next_retry_at, self.now)
        self.assertFalse(ExternalPayRecheck.objects.get(order=order).pending)

    def test_old_snapshot_does_not_block_new_page(self):
        bill = self.bill(total_refund_fee=2000, trade_state=3)
        self.state.window_start = self.now - timedelta(days=1); self.state.window_end = self.now; self.state.save()
        stale = parse_bill(self.row(total_refund_fee=1000, trade_state=3))
        fresh = parse_bill(self.row(transaction_id="other", out_trade_no="other-order"))
        with patch("apps.wecom.external_pay._get_page", return_value=([stale, fresh], "")):
            pull_one_page(self.config, self.state)
        self.state.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(self.state.watermark, self.now)
        self.assertEqual(bill.total_refund_fee, 2000)
        self.assertTrue(ExternalPayBill.objects.filter(transaction_id="other").exists())

    def test_more_than_five_thousand_completed_payments_do_not_delay_new_one(self):
        from apps.wecom.external_pay import run_due
        ExternalPayBill.objects.bulk_create([
            ExternalPayBill(state=self.state, bill_type=0, transaction_id=f"old-{i}", mch_id="m", out_trade_no=f"old-order-{i}", total_fee=10000, total_refund_fee=0, pay_time=self.now - timedelta(hours=1), trade_state=1, matched_at=self.now, next_retry_at=None)
            for i in range(5001)
        ])
        order = self.order("20261113000000000001")
        bill = self.bill(); bill.next_retry_at = self.now; bill.save(update_fields=["next_retry_at"])
        with patch("apps.wecom.external_pay.prepare_window", return_value=False):
            run_due(self.now)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIsNone(bill.next_retry_at)

    def test_token_and_bill_requests_are_bounded_and_refresh_only_once(self):
        from apps.wecom.external_pay import _get_page
        from tests._wecom_helpers import WecomApiStub
        stub = WecomApiStub()
        stub.enqueue({"access_token": "synthetic-token-a", "expires_in": 7200})
        stub.enqueue({"errcode": 40014, "errmsg": "invalid token"})
        stub.enqueue({"access_token": "synthetic-token-b", "expires_in": 7200})
        stub.enqueue({"errcode": 40014, "errmsg": "invalid token"})
        with patch("requests.sessions.Session.request", autospec=True, side_effect=lambda session, method, url, **kw: stub._transport(method, url, **kw)):
            with self.assertRaises(Exception):
                _get_page(self.config, self.now - timedelta(days=1), self.now, "")
        self.assertEqual(len(stub.requests), 4)
        self.assertEqual([x.kwargs.get("timeout") for x in stub.requests], [(5, 10), (5, 20), (5, 10), (5, 20)])

    def test_upstream_corrected_invalid_bill_is_retrieved_and_resolved(self):
        from apps.wecom.external_pay import _retry_invalid, _safe_invalid
        from apps.wecom.models import ExternalPayInvalidBill
        self.state.window_start = self.now - timedelta(days=1); self.state.window_end = self.now; self.state.save()
        bad = _safe_invalid(self.row(pay_time=0), "时间无效")
        good = parse_bill(self.row())
        with patch("apps.wecom.external_pay._get_page", return_value=([bad], "")):
            pull_one_page(self.config, self.state)
        invalid = ExternalPayInvalidBill.objects.get()
        self.assertFalse(invalid.resolved)
        with patch("apps.wecom.external_pay._get_page", return_value=([good], "")), patch("apps.wecom.external_pay.time.sleep"):
            _retry_invalid(self.config, self.state, self.now)
        invalid.refresh_from_db()
        self.assertTrue(invalid.resolved)
        self.assertTrue(ExternalPayBill.objects.filter(transaction_id="pay-123").exists())

    def test_conflicting_bill_is_frozen_without_blocking_other_page_rows(self):
        from apps.wecom.models import ExternalPayInvalidBill
        self.state.window_start = self.now - timedelta(days=1); self.state.window_end = self.now; self.state.save()
        original = self.bill()
        conflict = parse_bill(self.row(total_fee=11000))
        other = parse_bill(self.row(transaction_id="other", out_trade_no="other-order"))
        with patch("apps.wecom.external_pay._get_page", return_value=([conflict, other], "")):
            pull_one_page(self.config, self.state)
        self.state.refresh_from_db(); original.refresh_from_db()
        self.assertTrue(original.conflicted)
        self.assertEqual(original.total_fee, 10000)
        self.assertIsNone(original.next_retry_at)
        self.assertEqual(self.state.watermark, self.now)
        self.assertTrue(ExternalPayBill.objects.filter(transaction_id="other").exists())
        self.assertEqual(ExternalPayInvalidBill.objects.count(), 1)

    def test_invalid_bill_details_are_hidden_from_non_admin(self):
        from apps.wecom.external_pay import _safe_invalid, _quarantine
        from tests.p5_auth_helpers import make_logged_in_client
        bad = _safe_invalid(self.row(pay_time=0), "时间无效")
        _quarantine(self.state, bad, self.now - timedelta(days=1), self.now)
        restricted = make_logged_in_client(username="invalid-restricted", groups=("客服",), nodes=("system.wecom_settings",), games=[])
        owner = make_logged_in_client(username="invalid-owner", groups=("系统管理员",), nodes=("system.wecom_settings",))
        hidden = restricted.get("/api/wecom/external-pay/invalid/").json()["data"]["items"][0]
        visible = owner.get("/api/wecom/external-pay/invalid/").json()["data"]["items"][0]
        self.assertIsNone(hidden["payload"])
        self.assertEqual(visible["payload"]["transaction_id"], "pay-123")
        self.assertTrue(hidden["identifiable"])

    def test_invalid_recovery_scans_over_72_pages_before_expiry(self):
        from apps.wecom.external_pay import _retry_invalid, _safe_invalid, _quarantine
        from apps.wecom.models import ExternalPayInvalidBill
        order = self.order("20261113000000000001")
        original_time = self.now - timedelta(days=2, hours=23)
        bad = _safe_invalid(self.row(pay_time=0), "时间无效")
        _quarantine(self.state, bad, self.now - timedelta(days=3), self.now)
        good = parse_bill(self.row(pay_time=int(original_time.timestamp())))
        pages = [([], f"page-{i}") for i in range(1, 74)] + [([good], "")]
        with patch("apps.wecom.external_pay._get_page", side_effect=pages) as fetch, patch("apps.wecom.external_pay.time.sleep") as pause:
            _retry_invalid(self.config, self.state, self.now)
        self.assertEqual(fetch.call_count, 74)
        self.assertEqual(pause.call_count, 74)
        invalid = ExternalPayInvalidBill.objects.get()
        self.assertTrue(invalid.resolved)
        bill = ExternalPayBill.objects.get(transaction_id="pay-123")
        match_payment(bill.pk, self.config, self.now)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)

    def test_invalid_recovery_round_robin_resumes_at_budget_boundary(self):
        from apps.wecom.external_pay import _retry_invalid, _safe_invalid, _quarantine
        from apps.wecom.models import ExternalPayInvalidBill
        for tid in ("bad-a", "bad-b"):
            _quarantine(self.state, _safe_invalid(self.row(transaction_id=tid, pay_time=0), "时间无效"), self.now - timedelta(days=1), self.now)
        with patch("apps.wecom.external_pay._get_page", side_effect=[([], "a-next"), ([], "b-next")]) as fetch, patch("apps.wecom.external_pay.time.sleep"):
            _retry_invalid(self.config, self.state, self.now, page_budget=2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(list(ExternalPayInvalidBill.objects.order_by("id").values_list("search_cursor", flat=True)), ["a-next", "b-next"])
        fixed = [parse_bill(self.row(transaction_id=tid)) for tid in ("bad-a", "bad-b")]
        with patch("apps.wecom.external_pay._get_page", side_effect=[([fixed[0]], ""), ([fixed[1]], "")]) as fetch, patch("apps.wecom.external_pay.time.sleep"):
            _retry_invalid(self.config, self.state, self.now, page_budget=2)
        self.assertEqual([call.args[3] for call in fetch.call_args_list], ["a-next", "b-next"])
        self.assertEqual(ExternalPayInvalidBill.objects.filter(resolved=True).count(), 2)

    def test_refund_history_conflict_freezes_payment_and_isolates_bill(self):
        from apps.wecom.models import ExternalPayInvalidBill
        payment = self.bill(total_refund_fee=0)
        refund = self.bill(bill_type=1, transaction_id="refund-abc", out_refund_no="out-refund", total_refund_fee=1000, trade_state=None)
        conflict = parse_bill(self.row(total_fee=11000, total_refund_fee=1000, trade_state=3))
        with patch("apps.wecom.external_pay._get_page", return_value=([conflict], "")):
            process_refund(refund.pk, self.config, self.now)
        payment.refresh_from_db(); refund.refresh_from_db()
        self.assertTrue(payment.conflicted)
        self.assertIsNone(payment.next_retry_at)
        self.assertIn("冲突", refund.issue)
        self.assertEqual(ExternalPayInvalidBill.objects.count(), 1)

    def test_recovered_bill_past_payment_deadline_does_not_mark_paid(self):
        from apps.wecom.external_pay import _retry_invalid, _safe_invalid, _quarantine
        from apps.wecom.models import ExternalPayInvalidBill
        order = self.order("20261113000000000001")
        bad = _safe_invalid(self.row(pay_time=0), "时间无效")
        _quarantine(self.state, bad, self.now - timedelta(days=5), self.now)
        corrected = parse_bill(self.row(pay_time=int((self.now - timedelta(days=3, seconds=1)).timestamp())))
        with patch("apps.wecom.external_pay._get_page", return_value=([corrected], "")), patch("apps.wecom.external_pay.time.sleep"):
            _retry_invalid(self.config, self.state, self.now)
        self.assertTrue(ExternalPayInvalidBill.objects.get().resolved)
        bill = ExternalPayBill.objects.get(transaction_id="pay-123")
        match_payment(bill.pk, self.config, self.now)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIn("超过三天", bill.issue)

    def test_repeated_invalid_bill_reopens_resolved_warning(self):
        from apps.wecom.external_pay import _safe_invalid, _quarantine
        from apps.wecom.models import ExternalPayInvalidBill
        bad = _safe_invalid(self.row(pay_time=0), "时间无效")
        _quarantine(self.state, bad, self.now - timedelta(days=1), self.now)
        invalid = ExternalPayInvalidBill.objects.get()
        invalid.resolved = True; invalid.save(update_fields=["resolved"])
        _quarantine(self.state, bad, self.now - timedelta(hours=1), self.now)
        invalid.refresh_from_db()
        self.assertFalse(invalid.resolved)
        self.assertEqual(invalid.search_start, self.now - timedelta(hours=1))

    def test_payment_lock_conflict_rolls_back_and_schedules_retry(self):
        from django.db import OperationalError
        from apps.wecom.external_pay import run_due

        class LockUnavailable(Exception):
            sqlstate = "55P03"

        error = OperationalError("lock unavailable")
        error.__cause__ = LockUnavailable()
        order = self.order("20261113000000000001")
        bill = self.bill(total_refund_fee=1000, trade_state=3)
        bill.next_retry_at = self.now; bill.save(update_fields=["next_retry_at"])
        with patch("apps.wecom.external_pay.prepare_window", return_value=False), patch("apps.wecom.external_pay.order_writes.lock_order_and_sales_nowait", side_effect=error):
            run_due(self.now)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0"))
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIsNone(bill.order_id)
        self.assertIn("稍后自动重试", bill.issue)
        self.assertIsNotNone(bill.next_retry_at)

    def test_equal_refund_still_checks_historical_adjustment_and_reports_issue(self):
        from apps.business.refunds import RefundError
        from apps.wecom.external_pay import run_due
        order = self.order("20261113000000000001")
        order.refund_fee = Decimal("10"); order.save(update_fields=["refund_fee"])
        bill = self.bill(total_refund_fee=1000, trade_state=3)
        bill.next_retry_at = self.now; bill.save(update_fields=["next_retry_at"])
        with patch("apps.wecom.external_pay.prepare_window", return_value=False), patch("apps.wecom.external_pay.refunds.external_refund_history_denial", side_effect=RefundError("旧式拆单子单存在历史退款提成调整，请人工核对", 1000)) as check:
            run_due(self.now)
        self.assertEqual(check.call_count, 1)
        order.refresh_from_db(); bill.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("10"))
        self.assertNotEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertIn("历史退款提成调整", bill.issue)
        self.assertIsNotNone(bill.next_retry_at)
