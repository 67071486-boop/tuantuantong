"""Online payroll tests exercise the real tenant HTTP entry points and payment ledger."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, connections
from django.test import Client, TransactionTestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, tenant_context

from apps.accounts.models import StaffProfile, WxPusherBinding, WxPusherNotice
from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business import online_payroll_worker
from apps.business.alipay_auth_crypto import cipher, encrypt
from apps.business.models import (
    AlipaySettlementAuthorization,
    Game,
    Order,
    OnlinePayrollBatch,
    OnlinePayrollItem,
    OnlinePayrollTransfer,
    Performance,
    PerformanceAdjustment,
    PerformanceSettleRequest,
)
from apps.business.online_payroll import (
    apply_transfer_result,
    begin_sealing,
    begin_submission,
    record_sealed,
    record_submission,
)
from apps.platform.provisioning import provision_tenant
from apps.settlement_control.models import Authorization
from tests.p5_auth_helpers import make_logged_in_client


URL = "/api/business/online-payroll/"
MANUAL_SETTLE = "/api/business/settlements/settle"
MANUAL_REVOKE = "/api/business/settlements/revoke"


class _PayrollFixture:
    def setUp(self):
        super().setUp()
        self.settings_override = override_settings(
            ALIPAY_AUTH_DATA_KEY=Fernet.generate_key().decode(),
            ALIPAY_AUTH_LOCAL_ENABLED=True,
            ONLINE_PAYROLL_ENABLED=True,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.game = Game.objects.create(name="合成结算游戏")
        self.finance = make_logged_in_client(username="payroll-finance", is_superuser=True)
        self.payer = self.make_payer()

    @staticmethod
    def make_payer(status="authorized"):
        box = cipher()
        payer = AlipaySettlementAuthorization.objects.create(
            request_id=uuid4(), status=status,
            account_ciphertext=encrypt("payroll-payer@example.invalid", box),
            real_name_ciphertext=encrypt("合成付款人", box),
        )
        Authorization.objects.create(
            client="local", request_id=payer.request_id,
            phase=status, status=status, next_action="", configured=status == "authorized",
            app_id="synthetic-payroll-app", app_name="合成付款应用", ciphertext=b"synthetic-test-only",
        )
        return payer

    @staticmethod
    def staff(label, *, domain="tenant.test.com"):
        client = make_logged_in_client(username=f"payroll-{label}", domain=domain)
        user = get_user_model().objects.get(username=f"payroll-{label}")
        StaffProfile.objects.filter(user=user).update(
            alipay_account=f"payroll-{label}@example.invalid", alipay_name=f"合成收款{label}",
        )
        return client, user

    def performance(self, user, *, order_no, game=None, **overrides):
        order = overrides.pop("order", None)
        if order is None:
            order = Order.objects.create(
                game=game or self.game,
                order_no=order_no,
                status=overrides.pop("order_status", "COMPLETED"),
                total_price=Decimal("120.00"),
            )
        fields = {
            "emp_type": "打单工资",
            "temp_amount": Decimal("100.00"),
            "bonus_amount": Decimal("100.00"),
            "review_status": "已通过",
            "settle_status": "未结账",
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    @staticmethod
    def post(client, endpoint, payload):
        return client.post(f"{URL}{endpoint}", payload, content_type="application/json")

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content[:350])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def rejected(self, response):
        self.assertLess(response.status_code, 500, response.content[:350])
        self.assertNotEqual(response.json()["code"], 0, response.content[:350])

    def preview(self, *performances, category="taker", client=None):
        return self.post(client or self.finance, "preview/", {
            "performance_ids": [performance.pk for performance in performances],
            "category": category,
        })

    def submission(self, *performances, category="taker", client=None, payer=None):
        preview = self.data(self.preview(*performances, category=category, client=client))
        return {
            "performance_ids": [performance.pk for performance in performances],
            "category": category,
            "payer_id": (payer or self.payer).pk,
            "fingerprint": preview["fingerprint"],
            "request_id": str(uuid4()),
        }

    def create(self, payload, *, client=None):
        return self.post(client or self.finance, "batches/", payload)

    def cancel(self, batch_id):
        return self.post(self.finance, f"batches/{batch_id}/cancel/", {})

    def detail(self, batch_id, *, client=None):
        return (client or self.finance).get(f"{URL}batches/{batch_id}/")

    def mine(self, client):
        return client.get(f"{URL}mine/")


class OnlinePayrollSubmissionTests(_PayrollFixture, TenantTestCase):
    def setUp(self):
        super().setUp()
        self.recipient_client, self.recipient = self.staff("zeta")
        self.first = self.performance(self.recipient, order_no="20310101000000000001")
        self.second = self.performance(self.recipient, order_no="20310101000000000002")

    def test_same_request_and_same_selection_reuse_existing_payment(self):
        payload = self.submission(self.first, self.second)
        created = self.data(self.create(payload))
        replay = self.data(self.create(payload))
        reopened = self.data(self.create({
            **payload,
            "performance_ids": [self.second.pk, self.first.pk],
            "request_id": str(uuid4()),
        }))
        self.assertEqual([created["id"], replay["id"], reopened["id"]], [created["id"]] * 3)
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.assertEqual(OnlinePayrollTransfer.objects.count(), 1)
        self.assertEqual(OnlinePayrollItem.objects.count(), 2)
        self.assertEqual(OperationLog.objects.filter(action="finance.online_payroll", target_id=created["id"]).count(), 1)
        self.first.refresh_from_db()
        self.assertEqual(self.first.settle_status, "未结账")
        self.assertIsNone(self.first.settled_at)

    def test_overlapping_batch_and_manual_settlement_cannot_take_occupied_items(self):
        owner = self.submission(self.first)
        contender = self.submission(self.first, self.second)
        self.data(self.create(owner))
        self.rejected(self.create(contender))
        self.rejected(self.finance.post(
            MANUAL_SETTLE, {"performance_ids": [self.first.pk]}, content_type="application/json",
        ))
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.assertEqual(OnlinePayrollItem.objects.count(), 1)
        self.first.refresh_from_db()
        self.assertEqual(self.first.settle_status, "未结账")
        self.data(self.create(self.submission(self.second)))

    def test_request_id_cannot_be_reused_for_different_items(self):
        original = self.submission(self.first)
        different = self.submission(self.second)
        self.data(self.create(original))
        self.rejected(self.create({**different, "request_id": original["request_id"]}))
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.assertEqual(OnlinePayrollItem.objects.count(), 1)

    def test_finance_can_cancel_before_submission_and_pay_the_items_again(self):
        payload = self.submission(self.first)
        old = self.data(self.create(payload))
        self.data(self.cancel(old["id"]))
        self.data(self.cancel(old["id"]))
        logs = OperationLog.objects.filter(action="finance.online_payroll", target_id=old["id"]).order_by("pk")
        self.assertEqual(list(logs.values_list("reason", flat=True)), ["已确认制单", "取消 1 笔尚未制单的明细"])
        self.first.refresh_from_db()
        self.assertEqual(self.first.settle_status, "未结账")
        self.assertIsNone(self.first.settled_at)
        self.assertIsNone(self.first.settled_amount)
        new = self.data(self.create(self.submission(self.first)))
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(OnlinePayrollBatch.objects.count(), 2)
        self.assertEqual(OnlinePayrollItem.objects.count(), 2)

    def test_rejects_uncompleted_orders_already_paid_items_and_mixed_categories(self):
        open_order = self.performance(
            self.recipient, order_no="20310101000000000003", order_status="IN_PROGRESS",
        )
        paid = self.performance(
            self.recipient, order_no="20310101000000000004", settle_status="已结账",
        )
        _, service = self.staff("service")
        sales = self.performance(
            service, order_no=self.first.order.order_no, order=self.first.order, emp_type="销售提成",
        )
        for performances in ((open_order,), (paid,), (self.first, sales)):
            with self.subTest(ids=[performance.pk for performance in performances]):
                self.rejected(self.preview(*performances))
        sales_batch = self.data(self.create(self.submission(sales, category="customer_service")))
        taker_batch = self.data(self.create(self.submission(self.first)))
        self.assertEqual(sales_batch["category"], "customer_service")
        self.assertEqual(taker_batch["category"], "taker")
        self.assertNotEqual(sales_batch["id"], taker_batch["id"])
        self.assertEqual(OnlinePayrollBatch.objects.count(), 2)

    def test_submit_checks_for_changed_amount_and_revoked_payer(self):
        stale_amount = self.submission(self.first)
        self.first.bonus_amount = Decimal("110.00")
        self.first.save(update_fields=["bonus_amount"])
        self.rejected(self.create(stale_amount))
        stale_payer = self.submission(self.first)
        self.payer.status = "revoked"
        self.payer.save(update_fields=["status"])
        self.rejected(self.create(stale_payer))
        self.assertEqual(OnlinePayrollBatch.objects.count(), 0)

    def test_disabling_feature_stops_new_payment_without_hiding_existing_detail(self):
        existing = self.data(self.create(self.submission(self.first)))
        payload = self.submission(self.second)
        with override_settings(ONLINE_PAYROLL_ENABLED=False):
            self.rejected(self.create(payload))
            self.assertEqual(self.data(self.detail(existing["id"]))["id"], existing["id"])
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)

    def test_regular_staff_cannot_preview_or_open_finance_batch(self):
        batch = self.data(self.create(self.submission(self.first)))
        self.rejected(self.preview(self.first, client=self.recipient_client))
        self.rejected(self.detail(batch["id"], client=self.recipient_client))
        self.rejected(self.mine(Client(SERVER_NAME="tenant.test.com")))


class OnlinePayrollResultTests(_PayrollFixture, TenantTestCase):
    def setUp(self):
        super().setUp()
        self.alice_client, self.alice = self.staff("alice")
        self.bob_client, self.bob = self.staff("bob")
        self.alice_first = self.performance(
            self.alice, order_no="20310201000000000001", deduct_mode="1",
            deduct_value=Decimal("10.00"),
        )
        self.alice_second = self.performance(
            self.alice, order_no="20310201000000000002", bonus_amount=Decimal("30.00"),
        )
        self.bob_first = self.performance(self.bob, order_no="20310201000000000003")
        self.adjustment = PerformanceAdjustment.objects.create(
            performance=self.alice_first, kind="MANUAL", amount=Decimal("-15.00"),
            reason="合成退款扣回", operator="合成财务", settle_status="未结账",
        )
        self.paid_at = datetime(2025, 2, 3, 7, 24, 13, tzinfo=timezone.utc)

    def new_batch(self, *performances):
        data = self.data(self.create(self.submission(*performances)))
        return OnlinePayrollBatch.objects.get(pk=data["id"])

    def start_batch(self, batch):
        gateway_batch = batch.gateway_batches.get()
        transfers = list(gateway_batch.transfers.all())
        for transfer in transfers:
            self.assertTrue(begin_submission(transfer.pk))
            self.assertFalse(begin_submission(transfer.pk))
            record_submission(transfer.pk, created=True)
        self.assertTrue(begin_sealing(gateway_batch.pk))
        self.assertFalse(begin_sealing(gateway_batch.pk))
        record_sealed(gateway_batch.pk, payment_url="https://p.tb.cn/synthetic-payroll")
        return transfers

    def record_paid(self, transfer, *, paid_at=None):
        apply_transfer_result(
            transfer.pk, state=2, success_time=paid_at,
            transfer_no=f"synthetic-transfer-{transfer.pk}",
            channel_order_no=f"synthetic-alipay-{transfer.pk}",
        )

    def test_preview_reports_the_same_breakdown_and_total_written_to_the_payment(self):
        PerformanceAdjustment.objects.create(
            performance=self.alice_first, kind="MANUAL", amount=Decimal("99.00"),
            reason="合成已结调整", operator="合成财务", settle_status="已结账",
        )
        preview = self.data(self.preview(self.alice_first, self.alice_second))
        quoted, = preview["transfers"]
        items = {item["performance_id"]: item for item in quoted["items"]}
        first = items[self.alice_first.pk]
        self.assertEqual(
            {key: Decimal(str(first[key])) for key in ("bonus", "deduction", "adjustment", "amount")},
            {"bonus": Decimal("100.00"), "deduction": Decimal("10.00"),
             "adjustment": Decimal("-15.00"), "amount": Decimal("75.00")},
        )
        self.assertEqual(Decimal(str(quoted["amount"])), Decimal("105.00"))
        self.assertEqual(Decimal(str(preview["total"])), Decimal("105.00"))
        batch = self.new_batch(self.alice_first, self.alice_second)
        transfer = batch.gateway_batches.get().transfers.get()
        self.assertEqual(transfer.amount, Decimal("105.00"))
        self.assertEqual(transfer.items.get(performance=self.alice_first).amount, Decimal("75.00"))

    def test_attempted_submission_and_processing_cannot_be_released(self):
        before = self.submission(self.bob_first)
        batch = OnlinePayrollBatch.objects.get(pk=self.data(self.create(before))["id"])
        transfer = batch.gateway_batches.get().transfers.get()
        self.assertTrue(begin_submission(transfer.pk))
        record_submission(transfer.pk, created=False, error="支付平台响应超时")
        self.rejected(self.cancel(batch.pk))
        self.assertEqual(self.data(self.create({**before, "request_id": str(uuid4())}))["id"], str(batch.pk))
        apply_transfer_result(transfer.pk, state=1)
        self.rejected(self.cancel(batch.pk))
        self.assertEqual(self.data(self.create({**before, "request_id": str(uuid4())}))["id"], str(batch.pk))
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.bob_first.refresh_from_db()
        self.assertEqual(self.bob_first.settle_status, "未结账")
        self.assertIsNone(self.bob_first.settled_at)

    def test_missing_success_time_holds_the_paid_item_until_verified_timestamp_arrives(self):
        request = PerformanceSettleRequest.objects.create(performance=self.bob_first, user=self.bob)
        batch = self.new_batch(self.bob_first)
        transfer, = self.start_batch(batch)
        self.record_paid(transfer)
        self.bob_first.refresh_from_db()
        self.assertIsNone(self.bob_first.settled_at)
        self.assertEqual(self.bob_first.settle_status, "未结账")
        self.assertEqual(self.bob_first.online_payroll_batch_id, batch.pk)
        self.assertIsNone(transfer.items.get().released_at)
        request.refresh_from_db()
        self.assertEqual(request.state, "pending")
        self.assertIsNone(request.settled_at)
        self.rejected(self.cancel(batch.pk))
        self.record_paid(transfer, paid_at=self.paid_at)
        self.bob_first.refresh_from_db()
        self.assertEqual(self.bob_first.settlement_method, "online")
        self.assertEqual(self.bob_first.settled_at, self.paid_at)
        self.assertEqual(self.bob_first.settled_amount, Decimal("100.00"))
        request.refresh_from_db()
        self.assertEqual(request.state, "settled")
        self.assertEqual(request.settled_at, self.paid_at)

    def test_cancelling_partially_submitted_batch_only_releases_never_sent_items(self):
        batch = self.new_batch(self.alice_first, self.bob_first)
        sent = OnlinePayrollItem.objects.get(performance=self.alice_first).transfer
        untouched = OnlinePayrollItem.objects.get(performance=self.bob_first).transfer
        self.assertTrue(begin_submission(sent.pk))
        record_submission(sent.pk, created=False, error="合成超时")

        self.data(self.cancel(batch.pk))

        sent.refresh_from_db()
        untouched.refresh_from_db()
        self.alice_first.refresh_from_db()
        self.bob_first.refresh_from_db()
        self.assertEqual(sent.status, "unknown")
        self.assertEqual(untouched.status, "closed")
        self.assertEqual(self.alice_first.online_payroll_batch_id, batch.pk)
        self.assertIsNone(self.bob_first.online_payroll_batch_id)
        self.assertFalse(begin_sealing(sent.gateway_batch_id))
        self.assertFalse(begin_submission(untouched.pk))

    def test_confirmed_transfer_notifies_once_even_while_local_settlement_is_pending(self):
        workers, _ = Group.objects.get_or_create(name="代练员")
        for recipient, uid in ((self.alice, "UID_payroll_first"), (self.bob, "UID_payroll_second")):
            recipient.groups.add(workers)
            WxPusherBinding.objects.create(user=recipient, app_id="914", uid=uid)
        batch = self.new_batch(self.alice_first, self.alice_second, self.bob_first)
        self.start_batch(batch)
        paid = OnlinePayrollItem.objects.get(performance=self.alice_first).transfer
        failed = OnlinePayrollItem.objects.get(performance=self.bob_first).transfer
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name, WXPUSHER_APP_ID="914",
            WXPUSHER_APP_TOKEN="AT_synthetic_payroll",
        ):
            apply_transfer_result(paid.pk, state=1)
            apply_transfer_result(failed.pk, state=3, error="渠道确认失败")
            self.assertFalse(WxPusherNotice.objects.exists())
            self.record_paid(paid)
            paid.refresh_from_db()
            self.assertEqual(paid.status, "succeeded")
            self.assertIsNone(paid.finalized_at)
            self.assertEqual(list(WxPusherNotice.objects.values_list("kind", "user_id")), [
                ("transfer_succeeded", self.alice.pk),
            ])
            self.assertEqual(WxPusherNotice.objects.get().details, {
                "amount": "105.00",
                "related_order_nos": [self.alice_first.order.order_no, self.alice_second.order.order_no],
            })
            self.record_paid(paid)
            self.record_paid(paid, paid_at=self.paid_at)
            self.record_paid(paid, paid_at=self.paid_at)
        self.assertEqual(list(WxPusherNotice.objects.values_list("kind", "user_id", "order_no")), [
            ("transfer_succeeded", self.alice.pk, ""),
        ])

    def test_confirmed_transfer_notice_preserves_the_gateway_time_and_total_amount(self):
        workers, _ = Group.objects.get_or_create(name="代练员")
        self.alice.groups.add(workers)
        WxPusherBinding.objects.create(user=self.alice, app_id="914", uid="UID_payroll_paid")
        batch = self.new_batch(self.alice_first, self.alice_second)
        paid, = self.start_batch(batch)
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name, WXPUSHER_APP_ID="914",
            WXPUSHER_APP_TOKEN="AT_synthetic_payroll",
        ):
            self.record_paid(paid, paid_at=self.paid_at)
        self.assertEqual(WxPusherNotice.objects.get(user=self.alice).details, {
            "amount": "105.00", "occurred_at": "2025-02-03T07:24:13+00:00",
            "related_order_nos": [self.alice_first.order.order_no, self.alice_second.order.order_no],
        })

    def test_partial_success_sets_actual_time_once_and_only_failed_item_is_released(self):
        paid_request = PerformanceSettleRequest.objects.create(performance=self.alice_first, user=self.alice)
        cancelled_request = PerformanceSettleRequest.objects.create(
            performance=self.alice_second, user=self.alice, state="cancelled",
        )
        failed_request = PerformanceSettleRequest.objects.create(performance=self.bob_first, user=self.bob)
        batch = self.new_batch(self.alice_first, self.alice_second, self.bob_first)
        later_adjustment = PerformanceAdjustment.objects.create(
            performance=self.alice_first, kind="MANUAL", amount=Decimal("-3.00"),
            reason="制单后新增的合成调整", operator="合成财务", settle_status="未结账",
        )
        transfers = self.start_batch(batch)
        paid = OnlinePayrollItem.objects.get(performance=self.alice_first).transfer
        failed = OnlinePayrollItem.objects.get(performance=self.bob_first).transfer
        self.assertEqual(len(transfers), 2)
        self.assertEqual(paid.pk, OnlinePayrollItem.objects.get(performance=self.alice_second).transfer_id)
        self.assertNotEqual(paid.pk, failed.pk)
        self.record_paid(paid, paid_at=self.paid_at)
        paid.refresh_from_db()
        finalized_at = paid.finalized_at
        self.assertIsNotNone(finalized_at)
        audit_logs = OperationLog.objects.filter(action="performance.settle", order_no__in=[
            self.alice_first.order.order_no, self.alice_second.order.order_no,
        ])
        self.assertEqual(audit_logs.count(), 2)
        for log in audit_logs.iterator():
            self.assertTrue(log.summary.endswith(f"已向{paid.recipient_name}在线支付绩效款（订单 {log.order_no}）"), log.summary)
        audit_rows = audit_logs.count()
        self.record_paid(paid, paid_at=self.paid_at)
        apply_transfer_result(paid.pk, state=1)
        apply_transfer_result(paid.pk, state=4)
        paid.refresh_from_db()
        self.assertEqual(paid.finalized_at, finalized_at)
        self.assertEqual(paid.status, "succeeded")
        self.assertEqual(audit_logs.count(), audit_rows)
        apply_transfer_result(failed.pk, state=1)
        self.rejected(self.cancel(batch.pk))
        apply_transfer_result(failed.pk, state=3, error="渠道确认失败")
        apply_transfer_result(failed.pk, state=3, error="渠道确认失败")

        detail = self.data(self.detail(batch.pk))
        self.assertEqual(detail["status"], "partial")
        self.assertEqual(detail["gateway_batches"][0]["status"], "partial")

        for row, amount in ((self.alice_first, "75.00"), (self.alice_second, "30.00")):
            row.refresh_from_db()
            self.assertEqual(row.settle_status, "已结账")
            self.assertEqual(row.settled_amount, Decimal(amount))
            self.assertEqual(row.settled_at, self.paid_at)
            self.assertEqual(row.settlement_method, "online")
            self.assertEqual(row.settled_by, "payroll-finance")
            self.assertEqual(row.online_payroll_batch_id, batch.pk)
            self.assertIsNone(paid.items.get(performance=row).released_at)
        self.adjustment.refresh_from_db()
        self.assertEqual(self.adjustment.settle_status, "已结账")
        self.assertEqual(self.adjustment.settled_at, self.paid_at)
        later_adjustment.refresh_from_db()
        self.assertEqual(later_adjustment.settle_status, "未结账")
        self.assertIsNone(later_adjustment.settled_at)
        self.bob_first.refresh_from_db()
        self.assertEqual(self.bob_first.settle_status, "未结账")
        self.assertIsNone(self.bob_first.settled_at)
        self.assertIsNone(self.bob_first.online_payroll_batch_id)
        self.assertIsNotNone(failed.items.get().released_at)
        for request, state, settled_at in (
            (paid_request, "settled", self.paid_at),
            (cancelled_request, "cancelled", None),
            (failed_request, "pending", None),
        ):
            request.refresh_from_db()
            self.assertEqual(request.state, state)
            self.assertEqual(request.settled_at, settled_at)
        self.rejected(self.preview(self.alice_first))
        self.rejected(self.finance.post(
            MANUAL_REVOKE, {"performance_ids": [self.alice_first.pk], "reason": "合成撤销"},
            content_type="application/json",
        ))
        retry = self.new_batch(self.bob_first)
        self.assertNotEqual(retry.pk, batch.pk)
        self.assertEqual(OnlinePayrollBatch.objects.count(), 2)
        self.alice_first.refresh_from_db()
        self.assertEqual(self.alice_first.settle_status, "已结账")
        self.assertEqual(self.alice_first.settled_amount, Decimal("75.00"))

    def test_late_audit_error_rolls_back_online_settlement_and_allows_replay(self):
        request = PerformanceSettleRequest.objects.create(performance=self.alice_first, user=self.alice)
        batch = self.new_batch(self.alice_first, self.alice_second)
        transfer, = self.start_batch(batch)
        rows = (self.alice_first, self.alice_second)
        audit_logs = OperationLog.objects.filter(action="performance.settle", order_no__in=[
            row.order.order_no for row in rows
        ])

        def fail_after_last_audit(*, actor_id, **kwargs):
            record(actor_id=actor_id, **kwargs)
            if kwargs["order_no"] == self.alice_second.order.order_no:
                self.assertEqual(audit_logs.count(), 2)
                raise RuntimeError("合成审计异常")

        with patch("apps.business.online_payroll.record", side_effect=fail_after_last_audit):
            with self.assertRaisesRegex(RuntimeError, "合成审计异常"):
                self.record_paid(transfer, paid_at=self.paid_at)

        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.settle_status, "未结账")
            self.assertIsNone(row.settled_amount)
            self.assertIsNone(row.settled_at)
            self.assertEqual(row.settled_by, "")
            self.assertEqual(row.settlement_method, "")
            self.assertEqual(row.online_payroll_batch_id, batch.pk)
        self.adjustment.refresh_from_db()
        self.assertEqual(self.adjustment.settle_status, "未结账")
        self.assertIsNone(self.adjustment.settled_at)
        request.refresh_from_db()
        self.assertEqual(request.state, "pending")
        self.assertIsNone(request.settled_at)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, "created")
        self.assertIsNone(transfer.success_time)
        self.assertIsNone(transfer.finalized_at)
        self.assertEqual(audit_logs.count(), 0)

        self.record_paid(transfer, paid_at=self.paid_at)

        for row, amount in zip(rows, (Decimal("75.00"), Decimal("30.00")), strict=True):
            row.refresh_from_db()
            self.assertEqual(row.settled_amount, amount)
            self.assertEqual(row.settled_at, self.paid_at)
        self.adjustment.refresh_from_db()
        self.assertEqual(self.adjustment.settle_status, "已结账")
        self.assertEqual(self.adjustment.settled_at, self.paid_at)
        request.refresh_from_db()
        self.assertEqual(request.state, "settled")
        self.assertEqual(request.settled_at, self.paid_at)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, "succeeded")
        self.assertEqual(transfer.success_time, self.paid_at)
        self.assertIsNotNone(transfer.finalized_at)
        self.assertEqual(audit_logs.count(), 2)

    def test_failed_detail_keeps_result_visible_and_hides_old_qr(self):
        batch = self.new_batch(self.bob_first)
        transfer, = self.start_batch(batch)
        apply_transfer_result(transfer.pk, state=3, error="打款金额低于支付宝单笔最低限额")

        endpoint = f"batches/{batch.pk}/payment/"
        result = self.data(self.post(self.finance, endpoint, {"gateway_batch_id": transfer.gateway_batch_id}))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["active"])
        child, = result["gateway_batches"]
        self.assertEqual(child["status"], "failed")
        self.assertFalse(child["can_prepare_payment"])
        self.assertIsNone(child["payment_url"])
        self.assertIsNone(child["payment_qr_png_base64"])
        self.assertEqual(child["transfers"][0]["last_error"], "打款金额低于支付宝单笔最低限额")

    def test_computer_payment_is_scoped_to_original_batch_and_keeps_mobile_qr(self):
        from unittest.mock import patch

        batch = self.new_batch(self.bob_first)
        transfer, = self.start_batch(batch)
        endpoint = f"batches/{batch.pk}/payment/"
        payload = {"gateway_batch_id": transfer.gateway_batch_id, "desktop": True}
        with patch('apps.business.api.online_payroll_views.desktop_payment_url',
                   return_value='https://p.tb.cn/pc-fixture') as get_url:
            self.rejected(self.post(self.bob_client, endpoint, payload))
            get_url.assert_not_called()
            result = self.data(self.post(self.finance, endpoint, payload))
            get_url.assert_called_once_with(transfer.gateway_batch_id)
        self.assertEqual(result['id'], str(batch.pk))
        child, = result['gateway_batches']
        self.assertEqual(child['payment_pc_url'], 'https://p.tb.cn/pc-fixture')
        self.assertEqual(child['payment_url'], 'https://p.tb.cn/synthetic-payroll')
        self.assertTrue(child['payment_qr_png_base64'])
        self.assertEqual(OnlinePayrollTransfer.objects.count(), 1)

    def test_reopening_the_existing_payment_returns_its_existing_short_link(self):
        batch = self.new_batch(self.bob_first)
        self.start_batch(batch)
        gateway = batch.gateway_batches.get()
        read = self.detail(batch.pk)
        self.assertEqual(read["Cache-Control"], "no-store")
        self.assertIsNone(self.data(read)["gateway_batches"][0]["payment_url"])

        endpoint = f"batches/{batch.pk}/payment/"
        payload = {"gateway_batch_id": gateway.pk}
        self.rejected(self.post(self.bob_client, endpoint, payload))
        for _ in range(2):
            response = self.post(self.finance, endpoint, payload)
            data = self.data(response)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(data["id"], str(batch.pk))
            child, = data["gateway_batches"]
            self.assertEqual(child["payment_url"], "https://p.tb.cn/synthetic-payroll")
            self.assertTrue(child["payment_qr_png_base64"].startswith("iVBOR"))
        gateway.refresh_from_db()
        self.assertNotIn("p.tb.cn", gateway.payment_ciphertext)
        self.rejected(self.cancel(batch.pk))
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.assertEqual(OnlinePayrollTransfer.objects.count(), 1)

    def test_list_and_finance_detail_map_only_relevant_order_rows_to_alipay_trace(self):
        batch = self.new_batch(self.alice_first, self.alice_second, self.bob_first)
        self.start_batch(batch)
        alice = OnlinePayrollItem.objects.get(performance=self.alice_first).transfer
        bob = OnlinePayrollItem.objects.get(performance=self.bob_first).transfer
        self.record_paid(alice, paid_at=self.paid_at)
        self.record_paid(bob, paid_at=self.paid_at)

        finance_data = self.data(self.detail(batch.pk))
        own_data = self.data(self.mine(self.alice_client))
        other_data = self.data(self.mine(self.bob_client))
        def mapped_orders(transfers):
            return {
                transfer["channel_order_no"]: {item["order_no"] for item in transfer["items"]}
                for transfer in transfers
            }

        alice_mapping = {
            f"synthetic-alipay-{alice.pk}": {
                self.alice_first.order.order_no, self.alice_second.order.order_no,
            },
        }
        bob_mapping = {f"synthetic-alipay-{bob.pk}": {self.bob_first.order.order_no}}
        self.assertEqual(mapped_orders(finance_data["gateway_batches"][0]["transfers"]), {
            **alice_mapping, **bob_mapping,
        })
        self.assertEqual(mapped_orders(own_data["items"]), alice_mapping)
        self.assertEqual(mapped_orders(other_data["items"]), bob_mapping)
        for data in (finance_data, own_data, other_data):
            self.assertNotIn("payroll-alice@example.invalid", str(data))
            self.assertNotIn("payroll-bob@example.invalid", str(data))
        self.assertNotIn("https://p.tb.cn/synthetic-payroll", str(own_data))
        self.assertNotIn("https://p.tb.cn/synthetic-payroll", str(other_data))


class OnlinePayrollTenantIsolationTests(_PayrollFixture, TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="online_payroll_b", name="合成结算租户B", domain="payroll-b.test.local",
        )

    @override_settings(ALLOWED_HOSTS=[*settings.ALLOWED_HOSTS, "tenant.test.com", "payroll-b.test.local"])
    def test_tenant_a_session_and_mine_cannot_see_tenant_b_payment(self):
        taker_a_client, taker_a = self.staff("same-login")
        perf_a = self.performance(taker_a, order_no="20310301000000000001")
        batch_a = self.data(self.create(self.submission(perf_a)))
        self.assertIn(perf_a.order.order_no, str(self.data(self.mine(taker_a_client))))
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="合成结算租户B游戏")
            taker_b_client, taker_b = self.staff("same-login", domain="payroll-b.test.local")
            finance_b = make_logged_in_client(
                username="payroll-finance", domain="payroll-b.test.local", is_superuser=True,
            )
            perf_b = self.performance(taker_b, order_no="20310301000000000002", game=game_b)
            batch_b = self.data(self.create(self.submission(
                perf_b, client=finance_b, payer=self.make_payer(),
            ), client=finance_b))
            self.assertIn(perf_b.order.order_no, str(self.data(self.mine(taker_b_client))))
            self.assertIn(perf_b.order.order_no, str(self.data(self.detail(batch_b["id"], client=finance_b))))
        connection.set_tenant(self.tenant)

        mine_a = str(self.data(self.mine(taker_a_client)))
        self.assertIn(perf_a.order.order_no, mine_a)
        self.assertNotIn(perf_b.order.order_no, mine_a)
        self.assertNotIn(batch_b["id"], mine_a)
        self.rejected(self.detail(batch_b["id"]))
        self.assertIn(perf_a.order.order_no, str(self.data(self.detail(batch_a["id"]))))

        outsider = Client(SERVER_NAME="payroll-b.test.local")
        outsider.cookies = taker_a_client.cookies.copy()
        self.rejected(self.mine(outsider))


class OnlinePayrollConcurrencyTests(_PayrollFixture, TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        cls.tenant = get_tenant_model()(schema_name="payroll_concurrency")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model().objects.create(
            tenant=cls.tenant, domain="tenant.test.com",
        )

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        connection.set_tenant(self.tenant)
        hosts = override_settings(ALLOWED_HOSTS=[*settings.ALLOWED_HOSTS, "tenant.test.com"])
        hosts.enable()
        self.addCleanup(hosts.disable)
        super().setUp()

    def test_concurrent_confirmations_with_distinct_request_ids_share_one_batch(self):
        self.assertTrue(connection.get_autocommit())
        second_finance = make_logged_in_client(username="payroll-second-finance", is_superuser=True)
        _, recipient = self.staff("concurrency")
        first = self.performance(recipient, order_no="20310401000000000001")
        second = self.performance(recipient, order_no="20310401000000000002")
        first_payload = self.submission(first, second)
        second_payload = self.submission(second, first, client=second_finance)
        self.assertNotEqual(first_payload["request_id"], second_payload["request_id"])
        gate = Barrier(2, timeout=15)

        def submit(client, payload):
            try:
                with tenant_context(self.tenant):
                    gate.wait()
                    return self.create(payload, client=client)
            finally:
                connections.close_all()

        submitted = []

        def inspect_committed_batch(schema, batch_id):
            exists = OnlinePayrollItem.objects.filter(transfer__gateway_batch__batch_id=batch_id).exists()
            submitted.append((schema, batch_id, connection.get_autocommit(), exists))

        with (
            patch(
                "apps.business.online_payroll_worker._start_initial_processing",
                side_effect=inspect_committed_batch,
            ) as start,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            a = workers.submit(submit, self.finance, first_payload)
            b = workers.submit(submit, second_finance, second_payload)
            first_response, second_response = a.result(timeout=20), b.result(timeout=20)
        first_batch = self.data(first_response)
        second_batch = self.data(second_response)
        self.assertEqual(first_batch["id"], second_batch["id"])
        self.assertEqual(OnlinePayrollBatch.objects.count(), 1)
        self.assertEqual(OnlinePayrollTransfer.objects.count(), 1)
        start.assert_called_once_with(self.tenant.schema_name, OnlinePayrollBatch.objects.get().pk)
        self.assertEqual(submitted, [(self.tenant.schema_name, OnlinePayrollBatch.objects.get().pk, True, True)])
        self.assertEqual(list(OnlinePayrollItem.objects.order_by("performance_id").values_list(
            "performance_id", flat=True,
        )), [first.pk, second.pk])

    def test_confirm_starts_worker_and_saves_payment_code_without_timer(self):
        _, recipient = self.staff("initial-processing")
        performance = self.performance(recipient, order_no="20310401000000000003")
        payload = self.submission(performance)
        finished = Event()
        process = online_payroll_worker._process_initial_batch

        def run(*args):
            try:
                process(*args)
            finally:
                finished.set()

        with (
            patch.object(online_payroll_worker, "_process_initial_batch", side_effect=run),
            patch.object(online_payroll_worker, "PayrollCredentials"),
            patch.object(online_payroll_worker, "PayrollGateway") as gateway,
        ):
            gateway.return_value.seal.return_value = "https://p.tb.cn/initial-fixture"
            created = self.data(self.create(payload))
            self.assertTrue(finished.wait(10), "新付款批次应在定时扫描前启动制单")
            gateway.return_value.submit.assert_called_once()
            gateway.return_value.seal.assert_called_once()
        refreshed = self.data(self.detail(created["id"]))
        self.assertEqual(refreshed["status"], "ready")
        self.assertTrue(refreshed["gateway_batches"][0]["can_prepare_payment"])
