from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from openpyxl import load_workbook

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType, FinanceExportJob, Game, JidanpuUpstreamHallOrder, Order, OrderStatus, Performance,
)
from apps.core.storage import LocalStorage
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

ORDERS = "/api/business/orders/export-jobs/"
SETTLEMENTS = "/api/business/settlements/export-jobs/"


class OrderFinanceExportJobTests(TenantTestCase):
    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        options = override_settings(
            TT_ORDER_FINANCE_ASYNC_EXPORTS=True,
            TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS={self.tenant.schema_name},
            MEDIA_ROOT=Path(media.name),
            MEDIA_STORAGE_BACKEND="local",
        )
        options.enable()
        self.addCleanup(options.disable)
        self.media = Path(media.name)
        self.game = Game.objects.create(name="测试导出游戏")
        self.other_game = Game.objects.create(name="未授权游戏")
        self.owner = self.login("export-owner", games=[self.game])
        self.order = Order.objects.create(
            game=self.game, order_no="20261013000000000401", status=OrderStatus.COMPLETED,
        )
        Order.objects.create(
            game=self.other_game, order_no="20261013000000000402", status=OrderStatus.COMPLETED,
        )

    def login(self, username, *, games, domain="tenant.test.com"):
        return make_logged_in_client(
            domain=domain, username=username, groups=("客服",), games=games,
            nodes=("order.export", "finance.payroll", "finance.export_performance"),
        )

    def submit(self, kind=ORDERS, filters=None):
        resp = self.owner.post(kind, filters or {}, content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content[:300])
        self.assertEqual(resp.json()["code"], 0)
        info = resp.json()["data"]
        self.assertEqual(info["status"], "pending")
        return kind + info["id"] + "/"

    def workbook(self, url):
        resp = self.owner.get(url + "download/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.streaming)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        payload = b"".join(resp.streaming_content)
        self.assertEqual(len(payload), int(resp["Content-Length"]))
        return load_workbook(BytesIO(payload), read_only=True).active

    def work(self):
        call_command("run_order_finance_exports")
        connection.set_tenant(self.tenant)

    def test_submit_is_async_and_duplicate_pending_submission_returns_same_job(self):
        with patch("apps.business.export_jobs.render_order_export") as render:
            url = self.submit(filters={"game": self.game.pk})
            self.assertEqual(url, self.submit(filters={"game": self.game.pk}))
            self.assertEqual(self.owner.get(url).json()["data"]["status"], "pending")
            self.assertEqual(self.owner.get(url + "download/").json()["code"], 1000)
            render.assert_not_called()

    def test_bounded_active_queue_rejects_new_job_but_keeps_identical_retry(self):
        with patch("apps.business.export_jobs.MAX_ACTIVE_FOR_OWNER", 1):
            initial = self.submit(filters={"game": self.game.pk})
            self.assertEqual(initial, self.submit(filters={"game": self.game.pk}))
            resp = self.owner.post(ORDERS, {"game": self.other_game.pk}, content_type="application/json")
            self.assertEqual(resp.json()["code"], 1000)
        self.assertEqual(FinanceExportJob.objects.count(), 1)

    def test_worker_produces_one_scoped_workbook_and_one_audit_record(self):
        url = self.submit()
        self.work()
        self.work()
        self.assertEqual(self.owner.get(url).json()["data"]["status"], "ready")
        sheet = self.workbook(url)
        self.assertEqual([r[1] for r in sheet.iter_rows(min_row=2, values_only=True)], [self.order.order_no])
        self.assertEqual(OperationLog.objects.filter(action="order.export").count(), 1)
        self.assertEqual(len(list(self.media.rglob("*.xlsx"))), 1)

    def test_can_export_settlement_only_in_the_granted_game(self):
        taker = get_user_model().objects.create_user(username="export-taker")
        for order in Order.objects.order_by("pk"):
            Performance.objects.create(
                order=order, user=taker, emp_type=EmpType.MAIN, temp_amount=15,
            )
        url = self.submit(SETTLEMENTS)
        self.work()
        self.assertEqual(self.owner.get(url).json()["data"]["status"], "ready")
        sheet = self.workbook(url)
        self.assertEqual(sheet.title, "结算明细")
        self.assertEqual([r[4] for r in sheet.iter_rows(min_row=2, values_only=True)], [self.order.order_no])

    def test_same_tenant_bystander_cannot_get_status_or_file_or_media_object(self):
        url = self.submit()
        self.work()
        bystander = self.login("export-bystander", games=[self.game])
        self.assertEqual(bystander.get(ORDERS).json()["data"], [])
        for suffix in ("", "download/"):
            self.assertEqual(bystander.get(url + suffix).json()["code"], 1002)
        key = next(self.media.rglob("*.xlsx")).relative_to(self.media).as_posix()
        self.assertEqual(bystander.get("/api/media/blob/" + key).status_code, 403)

    def test_changed_visibility_rejects_previously_ready_file(self):
        url = self.submit()
        self.work()
        get_user_model().objects.get(username="export-owner").staff_profile.games.clear()
        self.assertEqual(self.owner.get(url).status_code, 403)
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_ready_order_file_denied_when_an_exported_row_moves_out_of_the_granted_game(self):
        url = self.submit()
        self.work()
        self.workbook(url)
        Order.objects.filter(pk=self.order.pk).update(game=self.other_game)
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_ready_order_file_denied_when_exported_row_is_soft_deleted(self):
        url = self.submit()
        self.work()
        self.workbook(url)
        Order.objects.filter(pk=self.order.pk).update(deleted_at=timezone.now())
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_ready_order_file_denied_when_exported_row_becomes_hidden_from_order_list(self):
        url = self.submit()
        self.work()
        self.workbook(url)
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://synthetic.example", upstream_order_id="hidden-job",
            game=self.game, first_seen_at=timezone.now(),
        )
        Order.objects.filter(pk=self.order.pk).update(upstream_hall_card=mirror)
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_ready_order_file_denied_when_an_exported_row_changes_creator(self):
        self.owner = make_logged_in_client(
            username="restricted-exporter",
            groups=("本人",),
            games=[self.game],
            nodes=("order.export", "order.restrict_to_self_created"),
        )
        Order.objects.filter(pk=self.order.pk).update(created_by="restricted-exporter")
        url = self.submit()
        self.work()
        sheet = self.workbook(url)
        self.assertEqual([r[1] for r in sheet.iter_rows(min_row=2, values_only=True)], [self.order.order_no])
        Order.objects.filter(pk=self.order.pk).update(created_by="another-creator")
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_worker_rechecks_exported_rows_before_publishing_the_uploaded_file(self):
        url = self.submit()
        original = LocalStorage.put_bytes

        def write_while_order_changes(storage, key, data, **kwargs):
            original(storage, key, data, **kwargs)
            Order.objects.filter(pk=self.order.pk).update(game=self.other_game)

        with patch.object(LocalStorage, "put_bytes", write_while_order_changes):
            self.work()
        self.assertEqual(self.owner.get(url).json()["data"]["status"], "failed")
        self.assertEqual(OperationLog.objects.filter(action="order.export").count(), 0)
        self.assertFalse(list(self.media.rglob("*.xlsx")))

    def test_settlement_snapshot_survives_amount_and_unrelated_row_changes(self):
        taker = get_user_model().objects.create_user(username="snapshot-taker")
        payment = Performance.objects.create(
            order=self.order, user=taker, emp_type=EmpType.MAIN, temp_amount=15,
        )
        url = self.submit(SETTLEMENTS)
        self.work()
        Order.objects.filter(game=self.other_game).update(created_by="other-owner", deleted_at=timezone.now())
        Performance.objects.filter(pk=payment.pk).update(temp_amount=80)
        sheet = self.workbook(url)
        rows = list(sheet.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 15)

    def test_ready_settlement_file_denied_when_exported_performance_changes_order(self):
        taker = get_user_model().objects.create_user(username="reassigned-performance")
        payment = Performance.objects.create(
            order=self.order, user=taker, emp_type=EmpType.MAIN, temp_amount=15,
        )
        url = self.submit(SETTLEMENTS)
        self.work()
        self.workbook(url)
        other_order = Order.objects.create(
            game=self.game, order_no="20261013000000000403", status=OrderStatus.COMPLETED,
        )
        Performance.objects.filter(pk=payment.pk).update(order=other_order)
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)

    def test_worker_refuses_to_generate_after_visibility_changes(self):
        url = self.submit()
        get_user_model().objects.get(username="export-owner").staff_profile.games.clear()
        self.work()
        self.assertEqual(self.owner.get(url + "download/").status_code, 403)
        self.assertEqual(FinanceExportJob.objects.get().status, "failed")
        self.assertFalse(list(self.media.rglob("*.xlsx")))

    def test_worker_recovers_stale_lease_and_removes_expired_private_file(self):
        url = self.submit()
        FinanceExportJob.objects.update(
            status="running", attempts=1, lease_until=timezone.now() - timedelta(seconds=1),
        )
        self.work()
        self.assertEqual(self.owner.get(url).json()["data"]["status"], "ready")
        self.assertEqual(FinanceExportJob.objects.get().attempts, 2)
        FinanceExportJob.objects.update(expires_at=timezone.now() - timedelta(minutes=4))
        self.assertEqual(self.owner.get(url + "download/").json()["code"], 1002)
        with override_settings(TT_ORDER_FINANCE_ASYNC_EXPORTS=False):
            self.work()
        self.assertFalse(list(self.media.rglob("*.xlsx")))
        self.assertFalse(FinanceExportJob.objects.exists())

    def test_an_unlisted_workspace_uses_the_synchronous_export_when_the_worker_is_present(self):
        self.submit()
        with override_settings(TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS={"different-workspace"}):
            resp = self.owner.post(ORDERS, {}, content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(resp.json()["data"])
            self.work()
            self.assertEqual(FinanceExportJob.objects.get().status, "pending")
            resp = self.owner.get("/api/business/orders/export")
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.content.startswith(b"PK"))

    def test_jobs_disabled_and_legacy_download_still_works(self):
        with override_settings(TT_ORDER_FINANCE_ASYNC_EXPORTS=False):
            resp = self.owner.post(ORDERS, {}, content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(resp.json()["data"])
            self.assertFalse(FinanceExportJob.objects.exists())
            resp = self.owner.get("/api/business/orders/export")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(load_workbook(BytesIO(resp.content), read_only=True).active.title, "Sheet1")

    def test_another_tenant_cannot_get_job_created_by_same_username(self):
        url = self.submit()
        self.work()
        tenant_b, _ = provision_tenant(
            schema_name="export_job_other", name="导出隔离工作室", domain="export-other.test.local",
        )
        with (tenant_context(tenant_b), override_settings(
            TT_ORDER_FINANCE_ASYNC_EXPORT_SCHEMAS={self.tenant.schema_name, tenant_b.schema_name},
        )):
            game = Game.objects.create(name="另一个工作室")
            actor = self.login("export-owner", games=[game], domain="export-other.test.local")
            self.assertEqual(actor.get(ORDERS).json()["data"], [])
            self.assertEqual(actor.get(url).json()["code"], 1002)
            self.assertEqual(actor.get(url + "download/").json()["code"], 1002)
        connection.set_tenant(self.tenant)
        self.assertEqual(self.owner.get(url).json()["data"]["status"], "ready")
