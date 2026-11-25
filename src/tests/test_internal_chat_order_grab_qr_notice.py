from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from PIL import Image

from apps.business.claim import claim_order
from apps.business.models import EmpType, Game, Order, OrderScreenshot, OrderStatus, Performance
from apps.business.roles import WORKER
from apps.core.storage import build_order_key
from apps.internal_chat import order_grab_qr_notices as notices
from apps.internal_chat.models import Message, OrderGrabQrNotice
from apps.internal_chat.system_notice_settings import update_system_notice_settings
from apps.platform.provisioning import provision_tenant
from apps.wecom.models import WecomAppConfig, WecomGroupChat, WecomGroupQr
from tests.p5_auth_helpers import make_logged_in_client

ORDER_NO = "20260101010101000018"
SOURCE = "1234567890"
MODULE = "apps.internal_chat.order_grab_qr_notices"


class OrderGrabQrNoticeTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(
            DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_CORE_BACKEND="legacy", TT_CHAT_MAINTENANCE=False,
        ))
        game = Game.objects.create(name="测试游戏")
        self.client = make_logged_in_client(
            domain=self.domain.domain, username="qr-winner", groups=(WORKER,),
            nodes=("grab_hall.take_order", "order.view_content"),
        )
        self.worker = get_user_model().objects.get(username="qr-winner")
        self.order = Order.objects.create(
            game=game, order_no=ORDER_NO, source_order_no=SOURCE,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("88.00"),
        )
        self.main = Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00"))
        image = BytesIO()
        Image.new("RGB", (24, 24)).save(image, format="PNG")
        self.png = image.getvalue()

    def take(self, *, enabled=True):
        if enabled:
            update_system_notice_settings({"order_grab_qr": True})
            self.assertTrue(notices._enabled())
        with transaction.atomic():
            claim_order(user_id=self.worker.pk, order_id=self.order.pk, nodes={"grab_hall.take_order"})
        self.main.refresh_from_db()

    def screenshot(self, *, status=OrderScreenshot.STATUS_READY, schema=None):
        return OrderScreenshot.objects.create(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR, status=status, uploaded_by=self.worker,
            object_key=build_order_key(schema or connection.schema_name, ORDER_NO, "png", kind="qr"),
        )

    def wecom(self, *, source=SOURCE):
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, agent_id=123, corp_id="local", secret="synth-secret",
        )
        group = WecomGroupChat.objects.create(chat_id="synthetic-wecom", name=f"订单群 {source} 接单")
        WecomGroupQr.objects.create(group=group, config_id="join-way", corp_id="local", agent_id=123, state="ready")
        return group

    def provider(self, group, *, live_name=None):
        return [
            {"errcode": 0, "group_chat": {"chat_id": group.chat_id, "name": live_name or group.name}},
            {"errcode": 0, "join_way": {"qr_code": "https://wework.qpic.cn/test-qr"}},
        ]

    def test_default_off_does_not_enqueue(self):
        self.take(enabled=False)
        self.assertFalse(OrderGrabQrNotice.objects.exists())
        self.assertEqual(notices.run_due(), 0)

    def test_missing_notice_only_once_per_successful_claim(self):
        self.take()
        notices.enqueue(self.order, user_id=self.worker.pk, claimed_at=self.main.claimed_at)
        self.assertEqual(OrderGrabQrNotice.objects.count(), 1)
        self.assertEqual(notices.run_due(), 1)
        self.assertEqual(notices.run_due(), 0)
        message = Message.objects.get()
        self.assertEqual(message.conversation.system_recipient_id, self.worker.pk)
        self.assertEqual(message.system_order_no, ORDER_NO)
        self.assertEqual(message.text, "该订单暂无群二维码，请联系通讯录客服获取。")

    def test_native_qr_is_a_private_attachment_and_reads_after_claim(self):
        self.screenshot()
        with patch(f"{MODULE}.get_storage") as storage:
            self.take()
            storage.assert_not_called()
            storage.return_value.open_stream.return_value = BytesIO(self.png)
            self.assertEqual(notices.run_due(), 1)
        message = Message.objects.get()
        self.assertEqual(bytes(message.image_data), self.png)
        self.assertEqual(message.image_metadata, {"mime": "image/png", "width": 24, "height": 24})
        path = f"/api/internal-chat/conversations/{message.conversation_id}/messages/{message.pk}/image/"
        self.assertEqual(self.client.get(path).json()["code"], 0)
        outsider = make_logged_in_client(domain=self.domain.domain, username="qr-outsider")
        self.assertNotEqual(outsider.get(path).json()["code"], 0)
        foreign_tenant, foreign_domain = provision_tenant(
            schema_name="qr_notice_foreign", name="另一工作室", domain="qr-notice-foreign.test.local",
        )
        with tenant_context(foreign_tenant):
            foreign = make_logged_in_client(domain=foreign_domain.domain, username="qr-winner", groups=(WORKER,))
            self.assertEqual(notices.run_due(), 0)
            self.assertNotEqual(foreign.get(path).json()["code"], 0)

    def test_cross_tenant_media_key_is_never_read(self):
        self.screenshot(schema="unrelated")
        self.take()
        with patch(f"{MODULE}.get_storage") as storage:
            self.assertEqual(notices.run_due(), 1)
            storage.assert_not_called()
        self.assertFalse(Message.objects.exists())
        self.assertIsNone(OrderGrabQrNotice.objects.get().finished_at)

    def test_pending_screenshot_does_not_block_verified_wecom_qr(self):
        self.screenshot(status=OrderScreenshot.STATUS_PENDING)
        group = self.wecom()
        self.take()
        with (
            patch("apps.wecom.group_qr._call", side_effect=self.provider(group)) as remote,
            patch(f"{MODULE}.order_qr_fetch._download_image", return_value=self.png) as download,
        ):
            self.assertEqual(notices.run_due(), 1)
        self.assertEqual([call.args[1] for call in remote.call_args_list], ["get", "get_join_way"])
        download.assert_called_once()
        self.assertEqual(bytes(Message.objects.get().image_data), self.png)

    def test_wecom_match_cannot_use_another_orders_longer_number(self):
        self.wecom(source=SOURCE + "8")
        self.take()
        with patch("apps.wecom.group_qr._call") as remote:
            self.assertEqual(notices.run_due(), 1)
        remote.assert_not_called()
        self.assertEqual(Message.objects.get().text, notices.MISSING_QR)

    def test_live_group_name_must_contain_the_same_complete_number(self):
        group = self.wecom()
        self.take()
        with (
            patch("apps.wecom.group_qr._call", side_effect=self.provider(group, live_name=SOURCE + "8")) as remote,
            patch(f"{MODULE}.order_qr_fetch._download_image") as download,
        ):
            self.assertEqual(notices.run_due(), 1)
        self.assertEqual([call.args[1] for call in remote.call_args_list], ["get"])
        download.assert_not_called()
        self.assertFalse(Message.objects.exists())

    def test_wecom_source_change_after_download_gives_no_attachment(self):
        group = self.wecom()
        self.take()

        def download(url):
            self.order.source_order_no = "changed-source"
            self.order.save(update_fields=["source_order_no"])
            return self.png

        with patch("apps.wecom.group_qr._call", side_effect=self.provider(group)), patch(
            f"{MODULE}.order_qr_fetch._download_image", side_effect=download,
        ):
            self.assertEqual(notices.run_due(), 1)
        self.assertFalse(Message.objects.exists())
        self.assertIsNone(OrderGrabQrNotice.objects.get().finished_at)

    def test_reassignment_or_disable_and_reenable_cancels_old_notice(self):
        self.take()
        update_system_notice_settings({"order_grab_qr": False})
        self.assertEqual(notices.run_due(), 0)
        update_system_notice_settings({"order_grab_qr": True})
        self.assertEqual(notices.run_due(), 1)
        self.assertIsNotNone(OrderGrabQrNotice.objects.get().finished_at)
        self.assertFalse(Message.objects.exists())

        later = timezone.now() + timedelta(seconds=1)
        self.main.claimed_at = later
        self.main.save(update_fields=["claimed_at"])
        notices.enqueue(self.order, user_id=self.worker.pk, claimed_at=later)
        self.main.user = None
        self.main.save(update_fields=["user"])
        self.assertEqual(notices.run_due(), 1)
        self.assertFalse(Message.objects.exists())

    def test_database_error_in_enqueue_does_not_fail_claim(self):
        with self.assertLogs(MODULE, level="WARNING"), patch.object(
            OrderGrabQrNotice.objects, "get_or_create", side_effect=DatabaseError,
        ):
            self.take()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.main.user_id, self.worker.pk)

    def test_dedicated_command_delivers_with_provider_off_and_isolates_tenant_failure(self):
        self.take()
        provider_disabled = provision_tenant(
            schema_name="qr_notice_paused", name="不同工作室", domain="qr-notice-paused.test.local",
        )[0]
        visited = []

        def run_due():
            visited.append(connection.schema_name)
            if connection.schema_name == provider_disabled.schema_name:
                self.assertFalse(OrderGrabQrNotice.objects.exists())
                raise DatabaseError("simulated failure")
            return notices.run_due()

        # The alternate provider is absent. Its workers must not be run by notice delivery.
        with (
            patch("apps.internal_chat.management.commands.send_order_grab_qr_notices.run_due", side_effect=run_due),
            patch("apps.business.order_qr_fetch._run_fetch_careful") as external_fetch,
        ):
            with self.assertRaises(CommandError):
                call_command("send_order_grab_qr_notices", stdout=StringIO(), stderr=StringIO())
            external_fetch.assert_not_called()
        self.assertEqual(visited, [provider_disabled.schema_name, self.tenant.schema_name])
        self.assertEqual(connection.schema_name, self.tenant.schema_name)
        self.assertEqual(Message.objects.get().text, notices.MISSING_QR)

    def test_external_fetch_command_does_not_send_notifications(self):
        self.take()
        call_command("fetch_order_qr_queue", stdout=StringIO())
        self.assertFalse(Message.objects.exists())
        self.assertIsNone(OrderGrabQrNotice.objects.get().finished_at)
