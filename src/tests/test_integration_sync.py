from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model
from psycopg import sql

from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, Performance
from apps.integration_sync.delivery import enqueue_deliveries, publish_and_enqueue
from apps.integration_sync.feed import ExpiredCursor, InvalidCursor, publish_changes, read_page
from apps.integration_sync.models import BusinessChange
from apps.openapi import dispatch, outbound
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, OpenApiDelivery


def create_integration_app(*, kind="business_integration", callback_url="", state=None):
    if state is None:
        state = "sdk_enabled" if kind == "business_integration" else "enabled"
    owner = get_user_model().objects.create(username=f"integration-sync-{kind}")
    return ApiApp.objects.create(
        name=f"同步测试 {kind}",
        app_key=f"sync-{kind}",
        app_secret="integration-synthetic-test-secret",
        state=state,
        kind=kind,
        created_by=owner,
        callback_url=callback_url,
    )


class IntegrationSyncCaptureTests(TenantTestCase):
    def test_each_editable_account_info_field_triggers_an_order_change(self):
        create_integration_app()
        game = Game.objects.create(name="SDK 回补资料游戏")
        order = Order.objects.create(order_no="20261116000000000053", game=game)
        BusinessChange.objects.all().delete()

        for field in ("current_info_1", "current_info_2"):
            Order.objects.filter(pk=order.pk).update(**{field: "新的账号资料"})

        self.assertEqual(list(BusinessChange.objects.values_list("resource_id", "action")), [
            (order.order_no, "updated"), (order.order_no, "updated"),
        ])

    def test_capture_when_an_integration_exists_including_during_pause(self):
        game = Game.objects.create(name="同步测试游戏")
        order = Order.objects.create(order_no="20261116000000000041", game=game)
        self.assertFalse(BusinessChange.objects.exists())
        create_integration_app(state="disabled")
        Order.objects.filter(pk=order.pk).update(remark="后台批量修改")
        row = BusinessChange.objects.get()
        self.assertEqual(
            (row.resource, row.resource_id, row.action),
            ("order", "20261116000000000041", "updated"),
        )

    def test_order_lifecycle_includes_reassignment_and_soft_delete_restore(self):
        app = create_integration_app()
        game = Game.objects.create(name="订单变更游戏")
        first = get_user_model().objects.create(username="first-taker")
        second = get_user_model().objects.create(username="second-taker")
        order = Order.objects.create(order_no="20261116000000000042", game=game)
        assignment = Performance.objects.create(
            order=order, user=first, temp_amount="10.00", emp_type=EmpType.MAIN,
        )
        Performance.objects.filter(pk=assignment.pk).update(user=second)
        Order.objects.filter(pk=order.pk).update(deleted_at=timezone.now())
        Order.all_objects.filter(pk=order.pk).update(deleted_at=None)
        publish_and_enqueue()
        page = read_page(app_key=app.app_key)
        self.assertEqual(
            [item["action"] for item in page["results"]],
            ["created", "updated", "updated", "deleted", "restored"],
        )
        self.assertEqual(
            {item["resource_id"] for item in page["results"]}, {order.order_no},
        )

    def test_disabling_a_soft_deleted_worker_does_not_republish_the_removed_worker(self):
        create_integration_app()
        worker_group = Group.objects.create(name="代练员")
        user = get_user_model().objects.create(username="soft-deleted-worker")
        user.groups.add(worker_group)
        profile = StaffProfile.objects.create(user=user)
        BusinessChange.objects.all().delete()

        profile.deleted_at = timezone.now()
        profile.save(update_fields=["deleted_at"])
        user.is_active = False
        user.save(update_fields=["is_active"])

        self.assertEqual(list(BusinessChange.objects.values_list("action", flat=True)), ["deleted"])

    def test_old_worker_disabled_delivery_is_recovered_without_changing_its_identity(self):
        app = create_integration_app(callback_url="https://sdk-callback.test.invalid/changes")
        row = OpenApiDelivery.objects.create(
            app=app, event="integration.change", order_no="integration:rollback",
            status=OpenApiDelivery.Status.FAILED, reason=OPENAPI_SPEC.callback_disabled_reasons[0],
            attempts=1, processed_at=timezone.now(), payload={"delivery_id": "stable-delivery-id"},
        )
        publish_and_enqueue()

        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.payload["delivery_id"], "stable-delivery-id")
        self.assertIsNone(row.processed_at)

    def test_only_workers_are_captured_with_game_and_status_changes(self):
        app = create_integration_app()
        game = Game.objects.create(name="资格变更游戏")
        worker_group, _ = Group.objects.get_or_create(name="代练员")
        admin_group, _ = Group.objects.get_or_create(name="系统管理员")
        user = get_user_model().objects.create(username="worker", first_name="旧昵称")
        user.groups.add(worker_group)
        profile = StaffProfile.objects.create(user=user)
        profile.games.add(game)
        get_user_model().objects.filter(pk=user.pk).update(first_name="新昵称")
        profile.games.remove(game)
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
        user.groups.add(admin_group)
        get_user_model().objects.filter(pk=user.pk).update(first_name="管理员昵称")
        operator = get_user_model().objects.create(username="operator", is_superuser=True)
        operator.groups.add(worker_group)
        StaffProfile.objects.create(user=operator, mobile="synthetic-phone")
        mixed = get_user_model().objects.create(username="mixed-non-worker")
        mixed.groups.add(worker_group, admin_group)
        StaffProfile.objects.create(user=mixed, mobile="synthetic-phone")
        user.groups.remove(admin_group)
        get_user_model().objects.filter(pk=user.pk).update(is_staff=True)
        get_user_model().objects.filter(pk=user.pk).update(first_name="后台昵称")
        get_user_model().objects.filter(pk=user.pk).update(is_staff=False)
        publish_and_enqueue()
        events = read_page(app_key=app.app_key)["results"]
        self.assertEqual(
            [(item["action"], item["resource_id"]) for item in events],
            [(operation, str(user.pk)) for operation in [
                "created", "updated", "updated", "updated", "updated", "updated",
                "deleted", "created", "deleted", "restored",
            ]],
        )
        self.assertEqual(
            set(events[0]), {"event_id", "resource", "resource_id", "action", "occurred_at"},
        )

    def test_hard_deleting_a_non_worker_still_deletes_the_account(self):
        create_integration_app()
        user = get_user_model().objects.create(username="non-worker")
        user_id = user.pk
        user.delete()
        self.assertFalse(get_user_model().objects.filter(pk=user_id).exists())
        self.assertFalse(BusinessChange.objects.exists())

    def test_rolled_back_business_write_does_not_publish(self):
        app = create_integration_app()
        game = Game.objects.create(name="回滚游戏")
        with transaction.atomic():
            Order.objects.create(order_no="20261116000000000043", game=game)
            transaction.set_rollback(True)
        publish_and_enqueue()
        self.assertEqual(read_page(app_key=app.app_key)["results"], [])

    def test_feed_is_paged_and_can_start_at_current_watermark(self):
        app = create_integration_app()
        game = Game.objects.create(name="翻页游戏")
        for number in ("20261116000000000044", "20261116000000000045"):
            Order.objects.create(order_no=number, game=game)
        publish_and_enqueue()
        first = read_page(app_key=app.app_key, page_size=1)
        self.assertTrue(first["has_more"])
        self.assertEqual([x["resource_id"] for x in first["results"]], ["20261116000000000044"])
        second = read_page(app_key=app.app_key, cursor=first["next_cursor"], page_size=1)
        self.assertFalse(second["has_more"])
        self.assertEqual([x["resource_id"] for x in second["results"]], ["20261116000000000045"])
        with self.assertRaises(InvalidCursor):
            read_page(app_key="another-app-key", cursor=first["next_cursor"])
        snapshot = read_page(app_key=app.app_key, start_now=True)
        self.assertEqual(snapshot["results"], [])
        Order.objects.create(order_no="20261116000000000046", game=game)
        publish_and_enqueue()
        page = read_page(app_key=app.app_key, cursor=snapshot["next_cursor"])
        self.assertEqual([x["resource_id"] for x in page["results"]], ["20261116000000000046"])

    def test_pruned_cursor_requires_another_snapshot(self):
        app = create_integration_app()
        game = Game.objects.create(name="过期游戏")
        snapshot = read_page(app_key=app.app_key, start_now=True)
        Order.objects.create(order_no="20261116000000000047", game=game)
        now = timezone.now()
        publish_changes(enqueue_deliveries, now=now - timedelta(days=31))
        Order.objects.create(order_no="20261116000000000048", game=game)
        publish_changes(enqueue_deliveries, now=now)
        with self.assertRaises(ExpiredCursor):
            read_page(app_key=app.app_key, cursor=snapshot["next_cursor"])
        self.assertEqual(
            [x["resource_id"] for x in read_page(app_key=app.app_key)["results"]],
            ["20261116000000000048"],
        )

    def test_callback_uses_feed_event_ids_and_existing_retry_engine(self):
        app = create_integration_app(callback_url="https://example.com/sync")
        legacy = create_integration_app(kind="general", callback_url="https://example.com/legacy")
        self.assertEqual([item.pk for item in dispatch.eligible_apps()], [legacy.pk])
        order = Order.objects.create(
            order_no="20261116000000000049", game=Game.objects.create(name="投递测试游戏"),
        )
        Order.objects.filter(pk=order.pk).update(remark="改一次")
        self.assertEqual(publish_and_enqueue(), 2)
        self.assertEqual(publish_and_enqueue(), 0)
        events = read_page(app_key=app.app_key)["results"]
        deliveries = list(OpenApiDelivery.objects.order_by("id"))
        self.assertEqual([row.app_id for row in deliveries], [app.pk, app.pk])
        self.assertEqual([row.payload["event_id"] for row in deliveries], ["1", "2"])
        first = deliveries[0]
        with mock.patch(
            "apps.openapi.dispatch.outbound.deliver",
            side_effect=[
                outbound.DeliveryResult(ok=False, status_code=503, reason="测试故障"),
                outbound.DeliveryResult(ok=True, status_code=204, reason=""),
            ],
        ) as send:
            dispatch.deliver_one(first.pk, spec=OPENAPI_SPEC)
            dispatch.deliver_one(first.pk, spec=OPENAPI_SPEC)
        payloads = [call.kwargs["payload"] for call in send.call_args_list]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0], {
            "event": "integration.change", "delivery_id": first.payload["delivery_id"], **events[0],
        })
        first.refresh_from_db()
        self.assertEqual(first.status, "done")
        self.assertEqual(first.attempts, 2)

    def test_failed_callback_queue_write_rolls_back_publication(self):
        app = create_integration_app(callback_url="https://example.com/sync")
        Order.objects.create(
            order_no="20261116000000000050", game=Game.objects.create(name="队列故障游戏"),
        )
        with mock.patch(
            "apps.integration_sync.delivery.OpenApiDelivery.objects.bulk_create",
            side_effect=RuntimeError("测试故障"),
        ), self.assertRaises(RuntimeError):
            publish_and_enqueue()
        self.assertEqual(read_page(app_key=app.app_key)["results"], [])
        self.assertEqual(publish_and_enqueue(), 1)
        page = read_page(app_key=app.app_key)
        self.assertEqual(page["results"][0]["event_id"], "1")


class IntegrationSyncCommitTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="integration_sync_commit")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model().objects.create(
            tenant=cls.tenant, domain="integration-sync-commit.test.local",
        )
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_early_uncommitted_change_does_not_block_or_get_skipped(self):
        app = create_integration_app()
        game = Game.objects.create(name="并发顺序游戏")
        older = Order.objects.create(order_no="20261116000000000051", game=game)
        newer = Order.objects.create(order_no="20261116000000000052", game=game)
        publish_and_enqueue()
        baseline = read_page(app_key=app.app_key, start_now=True)
        with connection.Database.connect(**connection.get_connection_params()) as late_writer:
            late_writer.execute(sql.SQL("SET LOCAL search_path TO {}").format(
                sql.Identifier(self.tenant.schema_name),
            ))
            late_writer.execute(
                "UPDATE business_order SET remark = %s WHERE id = %s", ["稍后提交", older.pk],
            )
            Order.objects.filter(pk=newer.pk).update(remark="先提交")
            publish_and_enqueue()
            first_page = read_page(app_key=app.app_key, cursor=baseline["next_cursor"])
            self.assertEqual(
                [x["resource_id"] for x in first_page["results"]], [newer.order_no],
            )

        publish_and_enqueue()
        page = read_page(app_key=app.app_key, cursor=first_page["next_cursor"])
        self.assertEqual([x["resource_id"] for x in page["results"]], [older.order_no])
