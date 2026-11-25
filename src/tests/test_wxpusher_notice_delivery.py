import io
import json
import signal
from datetime import timedelta
from html import escape
from unittest.mock import ANY, patch

import httpx
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, schema_context

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.accounts.wxpusher_config import update_wxpusher_config
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, Project, ProjectCategory
from apps.business.roles import WORKER
from apps.business.wxpusher_events import queue_order_notice
from apps.core.wxpusher import WxPusherClient
from apps.platform.models import Tenant
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

APP_ID = "9123"
TOKEN = "synthetic-order-notices-token"
ORDER_NO = "10000000000000000777"
PREFERENCES = "/api/auth/wxpusher/preferences"


class WxPusherNoticeDeliveryTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID=APP_ID,
            WXPUSHER_APP_TOKEN=TOKEN,
        ))
        self.worker = make_logged_in_client(
            username="order-notice-owner", groups=(WORKER,),
            nodes=("my_profile", "my_orders", "my_orders.send_to_my_system"),
        )
        self.user = get_user_model().objects.get(username="order-notice-owner")
        self.binding = WxPusherBinding.objects.create(
            user=self.user, app_id=APP_ID, scan_bound_uid="UID_order_owner", last_start_at=timezone.now(),
        )
        self.other_worker = make_logged_in_client(username="order-notice-other", groups=(WORKER,), nodes=("my_profile",))
        self.other_user = get_user_model().objects.get(username="order-notice-other")
        WxPusherBinding.objects.create(user=self.other_user, app_id=APP_ID, uid="UID_unrelated")
        self.requests = []
        self.timeout = False
        self.enterContext(patch("apps.accounts.wxpusher_notifications.WxPusherClient", side_effect=self._provider))

    def _provider(self, **kwargs):
        def respond(request):
            self.requests.append((str(request.url), json.loads(request.content)))
            if self.timeout:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return httpx.Response(200, json={
                "code": 1000, "data": [{"code": 1000, "uid": "UID_order_owner", "messageContentId": 123}],
            })

        return WxPusherClient(transport=httpx.MockTransport(respond), **kwargs)

    def _enqueue(self, *, kind, reference, order_no=ORDER_NO, user=None, **extra):
        enqueue_worker_notice(
            user_id=(user or self.user).pk, kind=kind, reference=reference, order_no=order_no, **extra,
        )

    def _preferences(self, payload):
        response = self.worker.patch(PREFERENCES, payload, content_type="application/json")
        self.assertEqual(response.json()["code"], 0)

    def _drain(self):
        call_command("send_wxpusher_notices", stdout=io.StringIO())

    def test_uses_https_order_link_only_for_current_owner_and_delivers_event_once(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game, remark="合成私密备注")
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        self._enqueue(kind="assigned", reference="same-event")
        self._enqueue(kind="assigned", reference="same-event")
        self._drain()
        self._drain()
        self.assertEqual(len(self.requests), 1)
        endpoint, body = self.requests[0]
        self.assertEqual(endpoint, "https://wxpusher.zjiecode.com/api/send/message")
        self.assertEqual(body["uids"], ["UID_order_owner"])
        self.assertEqual(body["summary"], "收到新的订单指派")
        self.assertEqual(body["contentType"], 2)
        self.assertIn(ORDER_NO, body["content"])
        self.assertNotIn("接单金额", body["content"])
        self.assertNotIn("合成私密备注", body["content"])
        self.assertEqual(body["url"], f"https://{self.tenant.get_primary_domain().domain}/m/orders/{ORDER_NO}")
        self.assertTrue(body["content"].endswith(f'<p><a href="{body["url"]}">请前往查看</a></p>'))
        self.assertEqual(WxPusherNotice.objects.get(reference="same-event").status, "sent")

    def test_daidaitong_order_notice_uses_order_entry_for_original_url_and_body(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game)
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        self._enqueue(kind="assigned", reference="app-order-link")

        origin = f"https://{self.tenant.get_primary_domain().domain}"
        with patch("apps.accounts.wxpusher_notifications.DAIDAITONG_ORIGIN", origin):
            self._drain()

        body = self.requests[0][1]
        self.assertEqual(body["url"], f"{origin}/m/wxpusher/orders/{ORDER_NO}?entry=wxpusher")
        self.assertIn(f'<a href="{body["url"]}">打开订单</a>', body["content"])
        self.assertEqual(body["content"].count("<a "), 1)

    def test_worker_order_notifications_include_project_and_worker_demand_for_each_game(self):
        first_game = Game.objects.create(name="合成游戏甲")
        second_game = Game.objects.create(name="合成游戏乙")
        project = Project.objects.create(
            name="配置项目", category=ProjectCategory.objects.create(name="合成分类", game=second_game),
        )
        scenarios = [
            (first_game, "", None, "从白银完成五场排位", None),
            (second_game, "", project, "完成活动 <第二区> & 截图", "配置项目"),
            (second_game, "指定项目", project, "在指定地图完成任务", "指定项目"),
        ]
        for offset, (game, project_text, catalog_project, demand, _expected_project) in enumerate(scenarios):
            order = Order.objects.create(
                order_no=f"{int(ORDER_NO) + offset:020}", game=game, project_text=project_text,
                project=catalog_project, demand=demand, content="合成客户原始需求", game_account="合成登录账号",
            )
            Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
            queue_order_notice(order, self.user.pk, "assigned", timezone.now())

        self._drain()

        inbox = self.worker.get("/api/auth/wxpusher/inbox").json()["data"]["items"]
        for offset, ((_, _, _, demand, expected_project), (_, body)) in enumerate(
            zip(scenarios, self.requests, strict=True),
        ):
            with self.subTest(order=offset):
                self.assertIn("<strong>需求描述</strong>", body["content"])
                self.assertIn(escape(demand), body["content"])
                self.assertNotIn("合成客户原始需求", body["content"])
                self.assertNotIn("合成登录账号", body["content"])
                item = next(row for row in inbox if row["order_no"] == f"{int(ORDER_NO) + offset:020}")
                self.assertIn(f"需求描述　{demand}", item["lines"])
                if expected_project:
                    self.assertIn(f"代练内容　{expected_project}", item["lines"])
                    self.assertIn(f"<strong>代练内容</strong>　{expected_project}", body["content"])

    def test_previous_worker_cannot_read_demand_in_delayed_notice_or_inbox(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(
            order_no=ORDER_NO, game=game, project_text="合成项目", demand="从白银完成五场排位",
        )
        second_order = Order.objects.create(
            order_no=f"{int(ORDER_NO) + 1:020}", game=game, project_text="合成项目", demand="从白银完成五场排位",
        )
        original = Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        removed = Performance.objects.create(order=second_order, user=self.user, emp_type=EmpType.MAIN)
        for affected in (order, second_order):
            queue_order_notice(affected, self.user.pk, "assigned", timezone.now())
        original.user = self.other_user
        original.save(update_fields=["user"])
        removed.deleted_at = timezone.now()
        removed.save(update_fields=["deleted_at"])

        self._drain()

        self.assertEqual(len(self.requests), 2)
        for _, body in self.requests:
            self.assertIn("合成项目", body["content"])
            self.assertNotIn("从白银完成五场排位", body["content"])
        items = self.worker.get("/api/auth/wxpusher/inbox").json()["data"]["items"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertNotIn("需求描述　从白银完成五场排位", item["lines"])

    def test_worker_order_notifications_hide_sensitive_free_text(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(
            order_no=ORDER_NO, game=game, project_text="待打十场排位",
            demand="游戏密码 synthetic-play-secret，微信号 synthetic-chat",
        )
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        queue_order_notice(order, self.user.pk, "assigned", timezone.now())

        self._drain()

        _, body = self.requests[0]
        self.assertIn("待打十场排位", body["content"])
        self.assertIn("需求描述</strong>　请在团团通查看", body["content"])
        self.assertNotIn("synthetic-play-secret", body["content"])
        self.assertNotIn("synthetic-chat", body["content"])

    def test_legacy_order_number_still_sends_a_web_link_to_the_order_list(self):
        legacy_no = "1000000000000000777"
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(order_no=legacy_no, game=game)
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        self._enqueue(kind="assigned", reference="legacy-order-link", order_no=legacy_no)

        origin = f"https://{self.tenant.get_primary_domain().domain}"
        with patch("apps.accounts.wxpusher_notifications.DAIDAITONG_ORIGIN", origin):
            self._drain()

        body = self.requests[0][1]
        self.assertEqual(body["url"], f"{origin}/m/mine")
        self.assertIn(f'<a href="{origin}/m/mine">请前往查看</a>', body["content"])
        self.assertEqual(WxPusherNotice.objects.get(reference="legacy-order-link").status, "sent")

    def test_sends_all_template_titles_details_and_matching_links_to_the_recipient(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game, remark="旧订单的私密备注")
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        order_details = {
            "display_order_no": "DEMO-0086",
            "project": "无畏契约 <b>排位</b> & 复盘",
            "amount": "0.00",
            "reason": "需要补充 <截图> & 说明",
        }
        settlement_details = {
            "amount": "268.00",
            "count": 3,
            "occurred_at": "2026-11-16T13:35:00+00:00",
        }
        cases = [
            ("assigned", "收到新的订单指派", ("接单金额", "¥0.00")),
            ("unassigned", "订单已撤销指派", ("说明", "已撤销指派")),
            ("review_rejected", "订单提交验收不通过", ("未通过原因", "需要补充 &lt;截图&gt; &amp; 说明")),
            ("completed", "订单已完成", ("接单金额", "¥0.00")),
            ("paused", "订单已暂停", ("暂停原因", "需要补充 &lt;截图&gt; &amp; 说明", "请先暂停操作")),
            ("resumed", "订单已恢复", ("可以继续处理",)),
            ("issue", "订单被标为问题单", ("问题说明", "需要补充 &lt;截图&gt; &amp; 说明")),
            ("refunded", "订单已退单", ("退单原因", "需要补充 &lt;截图&gt; &amp; 说明", "请停止处理")),
            ("settled", "你的订单报酬已结账", ("3 笔订单", "¥268.00", "结账时间", "11 月 16 日 21:35")),
            ("transfer_succeeded", "线上转账成功", ("转账金额", "支付宝", "¥268.00", "转账时间", "11 月 16 日 21:35")),
        ]
        settlement_kinds = {"settled", "transfer_succeeded"}
        for kind, _title, _expected in cases:
            self._enqueue(
                kind=kind, reference=f"template:{kind}", order_no=None if kind in settlement_kinds else ORDER_NO,
                details=settlement_details if kind in settlement_kinds else order_details,
            )
        self._enqueue(
            kind="assigned", reference="template:assigned", details={"amount": "999.00"},
        )
        self._drain()

        origin = f"https://{self.tenant.get_primary_domain().domain}"
        for (kind, title, expected), (_, body) in zip(cases, self.requests, strict=True):
            with self.subTest(kind=kind):
                self.assertEqual(body["summary"], title)
                self.assertEqual(body["uids"], ["UID_order_owner"])
                self.assertEqual(body["contentType"], 2)
                if kind in settlement_kinds:
                    self.assertEqual(body["url"], f"{origin}/m/performance")
                else:
                    tail = "/m/mine" if kind == "unassigned" else f"/m/orders/{ORDER_NO}"
                    self.assertEqual(body["url"], f"{origin}{tail}")
                    self.assertIn("DEMO-0086", body["content"])
                    self.assertIn("无畏契约 &lt;b&gt;排位&lt;/b&gt; &amp; 复盘", body["content"])
                for value in expected:
                    self.assertIn(value, body["content"])
                self.assertNotIn("<b>排位</b>", body["content"])
                self.assertNotIn("旧订单的私密备注", body["content"])
                self.assertNotIn("交单时间", body["content"])
                self.assertTrue(body["content"].endswith(f'<p><a href="{body["url"]}">请前往查看</a></p>'))
                if kind == "unassigned":
                    self.assertNotIn("需要补充", body["content"])

    def test_status_preferences_group_and_settlement_interrupt_independently(self):
        self._preferences({"order_events": {"paused_resumed": False}})
        self._enqueue(kind="paused", reference="paused-event")
        self._enqueue(kind="resumed", reference="resumed-event")
        self._enqueue(kind="issue", reference="disabled-before-dispatch")
        self._preferences({"order_enabled": False})
        self._enqueue(kind="assigned", reference="master-off")
        self._enqueue(kind="settled", reference="settlement", order_no=None)
        self._drain()
        self.assertEqual([body["summary"] for _, body in self.requests], ["你的订单报酬已结账"])
        self.assertEqual(self.requests[0][1]["url"], f"https://{self.tenant.get_primary_domain().domain}/m/performance")
        self.assertEqual(WxPusherNotice.objects.get(reference="disabled-before-dispatch").status, "skipped")
        self._preferences({"order_enabled": True, "settlement_enabled": False})
        self._enqueue(kind="transfer_succeeded", reference="payout-off", order_no=None)
        self._enqueue(kind="review_rejected", reference="review-on")
        self._drain()
        self.assertEqual([body["summary"] for _, body in self.requests], ["你的订单报酬已结账", "订单提交验收不通过"])

    def test_refund_link_preserves_access_to_own_order_and_falls_back_if_ownership_was_removed(self):
        game = Game.objects.create(name="示例游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game, status=OrderStatus.REFUNDED)
        performance = Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        origin = f"https://{self.tenant.get_primary_domain().domain}"

        self._enqueue(kind="refunded", reference="refund-retains-worker")
        self._drain()
        self.assertEqual(self.requests[0][1]["url"], f"{origin}/m/orders/{ORDER_NO}")

        performance.user = None
        performance.save(update_fields=["user"])
        self._enqueue(kind="refunded", reference="refund-after-removing-worker")
        with patch("apps.accounts.wxpusher_notifications.DAIDAITONG_ORIGIN", origin):
            self._drain()
        body = self.requests[1][1]
        self.assertEqual(body["url"], f"{origin}/m/mine")
        self.assertIn(f'<a href="{body["url"]}">请前往查看</a>', body["content"])

    def test_order_link_falls_back_to_worker_profile_if_access_is_revoked_before_delivery(self):
        game = Game.objects.create(name="合成游戏")
        order = Order.objects.create(order_no=ORDER_NO, game=game)
        Performance.objects.create(order=order, user=self.user, emp_type=EmpType.MAIN)
        self._enqueue(kind="assigned", reference="after-permission-change")
        permissions = Role.objects.get(group__name=WORKER).permissions
        permissions.remove(PermissionNode.objects.get(code="my_orders.send_to_my_system"))

        self._drain()

        body = self.requests[0][1]
        self.assertEqual(body["uids"], ["UID_order_owner"])
        self.assertEqual(body["url"], f"https://{self.tenant.get_primary_domain().domain}/m/me")
        self.assertIn(f'<a href="{body["url"]}">请前往查看</a>', body["content"])

        self._enqueue(kind="assigned", reference="without-available-list")
        permissions.set(PermissionNode.objects.filter(code="my_orders"))
        self._drain()
        self.assertEqual(self.requests[1][1]["url"], f"https://{self.tenant.get_primary_domain().domain}/m/empty")

        self._enqueue(kind="assigned", reference="accessible-performance-fallback")
        permissions.set(PermissionNode.objects.filter(code__in=("my_orders", "my_performance", "order.list")))
        self._drain()
        self.assertEqual(self.requests[2][1]["url"], f"https://{self.tenant.get_primary_domain().domain}/m/performance")

    def test_skips_old_binding_or_inactive_user_and_never_sends_on_rollback(self):
        self._enqueue(kind="assigned", reference="old-binding")
        self.binding.scan_bound_uid = "UID_rebound"
        self.binding.last_start_at += timedelta(seconds=20)
        self.binding.save(update_fields=["scan_bound_uid", "last_start_at"])
        self._enqueue(kind="assigned", reference="inactive-user", user=self.other_user)
        self.other_user.is_active = False
        self.other_user.save(update_fields=["is_active"])
        with transaction.atomic():
            self._enqueue(kind="completed", reference="rolled-back")
            transaction.set_rollback(True)
        self._drain()
        self.assertEqual(self.requests, [])
        self.assertFalse(WxPusherNotice.objects.filter(reference="rolled-back").exists())
        self.assertEqual(set(WxPusherNotice.objects.values_list("status", flat=True)), {"skipped"})

    def test_timeout_is_not_resent_and_revocation_opens_my_order_list(self):
        self.timeout = True
        self._enqueue(kind="unassigned", reference="unassigned-event")
        self._drain()
        self._drain()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1]["url"], f"https://{self.tenant.get_primary_domain().domain}/m/mine")
        self.assertEqual(WxPusherNotice.objects.get(reference="unassigned-event").status, "unconfirmed")

    def _watch_two_rounds(self, between_rounds):
        command = "apps.accounts.management.commands.send_wxpusher_notices"
        clock = [0.0]
        changed = False

        def wait_for_next_round(_listener, seconds):
            nonlocal changed
            if not changed and seconds > 0:
                between_rounds()
                changed = True
            if clock[0] >= 4:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            clock[0] += seconds
            return False

        with (
            patch(f"{command}.close_old_connections"),
            patch(f"{command}.monotonic", side_effect=lambda: clock[0]),
            patch(f"{command}.wait_for_hall_wakeup", side_effect=wait_for_next_round),
            patch("apps.accounts.wxpusher_notifications.monotonic", side_effect=lambda: clock[0]),
        ):
            call_command("send_wxpusher_notices", watch=True, stdout=io.StringIO())

    def test_watch_checks_tenant_is_still_active_before_each_round(self):
        self._enqueue(kind="assigned", reference="first-round")

        def deactivate():
            self._enqueue(kind="assigned", reference="second-round")
            with schema_context(get_public_schema_name()):
                Tenant.objects.filter(pk=self.tenant.pk).update(is_active=False)

        self._watch_two_rounds(deactivate)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(WxPusherNotice.objects.get(reference="second-round").status, "pending")

    @override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA="", WXPUSHER_APP_ID="", WXPUSHER_APP_TOKEN="")
    def test_watch_reads_new_tenant_settings_without_restart(self):
        def configure():
            self.assertEqual(self.requests, [])
            with transaction.atomic():
                update_wxpusher_config({"app_id": APP_ID, "app_token": TOKEN, "push_enabled": True})
            self._enqueue(kind="assigned", reference="enabled-while-watching")

        self._watch_two_rounds(configure)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1]["appToken"], TOKEN)
        self.assertEqual(WxPusherNotice.objects.get(reference="enabled-while-watching").status, "sent")

    def test_watch_finishes_the_current_notice_after_sigterm_leaving_the_next_pending(self):
        self._enqueue(kind="assigned", reference="in-flight")
        self._enqueue(kind="assigned", reference="next-notice")
        previous_handler = signal.getsignal(signal.SIGTERM)
        original_request = httpx.MockTransport.handle_request

        def request_stop(transport, request):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIsNot(handler, previous_handler)
            handler(signal.SIGTERM, None)
            return original_request(transport, request)

        command = "apps.accounts.management.commands.send_wxpusher_notices"
        stdout = io.StringIO()
        with (
            patch.object(httpx.MockTransport, "handle_request", new=request_stop),
            patch(f"{command}.close_old_connections"),
            patch(f"{command}.wait_for_hall_wakeup", return_value=False) as wait,
        ):
            call_command("send_wxpusher_notices", watch=True, stdout=stdout)
        wait.assert_called_once_with(ANY, 0)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(WxPusherNotice.objects.get(reference="in-flight").status, "sent")
        self.assertEqual(WxPusherNotice.objects.get(reference="next-notice").status, "pending")
        self.assertIn("已停止处理手机推送", stdout.getvalue())

    @override_settings(WXPUSHER_LOGIN_SIGNING_KEY="synthetic-legacy-key")
    def test_rollback_rejects_discarding_saved_login_and_push_controls(self):
        call_command("send_wxpusher_notices", check_rollback=True)
        with transaction.atomic():
            update_wxpusher_config({"login_enabled": False})
        with self.assertRaisesMessage(CommandError, "旧版会恢复旧的 WxPusher"):
            call_command("send_wxpusher_notices", check_rollback=True)
        with override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=""):
            call_command("send_wxpusher_notices", check_rollback=True)
        self.assertEqual(self.requests, [])

    def test_invalid_primary_domain_fails_before_sending(self):
        self._enqueue(kind="assigned", reference="invalid-tenant-domain")
        with schema_context(get_public_schema_name()):
            domain = self.tenant.get_primary_domain()
            domain.domain = "tenant.test.com/wrong-path"
            domain.save(update_fields=["domain"])
        with self.assertRaisesMessage(CommandError, "主域名无法用于手机推送链接"):
            self._drain()
        self.assertEqual(self.requests, [])
