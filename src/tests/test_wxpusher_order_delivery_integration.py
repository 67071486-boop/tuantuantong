import json
from io import StringIO
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import WxPusherBinding
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, PerformanceAdjustment
from apps.business.roles import WORKER
from tests.p5_auth_helpers import make_logged_in_client


class WxPusherOrderDeliveryTests(TenantTestCase):
    def test_assignment_reaches_only_the_bound_worker_and_respects_a_saved_opt_out(self):
        game = Game.objects.create(name="示例游戏")
        operator = make_logged_in_client(
            username="push-operator", groups=("客服",),
            nodes=("order.view_detail", "order.assign_taker", "order.submit_payout_price"),
        )
        worker = make_logged_in_client(
            username="push-worker", groups=(WORKER,),
            nodes=("my_profile", "my_orders", "my_orders.send_to_my_system"),
        )
        make_logged_in_client(username="push-bystander", groups=(WORKER,), nodes=("my_profile",))
        users = get_user_model().objects
        taker = users.get(username="push-worker")
        WxPusherBinding.objects.create(user=taker, app_id="6123", scan_bound_uid="UID_push_worker")
        WxPusherBinding.objects.create(user=users.get(username="push-bystander"), app_id="6123", uid="UID_bystander")

        sent = []

        def upstream(request):
            self.assertEqual(str(request.url), "https://wxpusher.zjiecode.com/api/send/message")
            self.assertEqual(request.method, "POST")
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": body["uids"][0], "code": 1000, "messageContentId": 51},
            ]})

        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="6123",
            WXPUSHER_APP_TOKEN="synthetic-order-push-key",
        ), patch("apps.core.wxpusher.httpx.HTTPTransport", return_value=httpx.MockTransport(upstream)):
            self.assign(operator, game, taker, "00000000000000000111")
            Order.objects.filter(order_no="00000000000000000111").update(
                source_order_no="SYN-CHANGED", project_text="指派之后修改", hall_price="99.00",
            )
            Performance.objects.filter(order__order_no="00000000000000000111").update(taker_price="99.00")
            call_command("send_wxpusher_notices", stdout=StringIO())
            self.assertEqual(len(sent), 1)
            notice = sent[0]
            url = f"https://{self.domain.domain}/m/orders/00000000000000000111"
            self.assertEqual(notice["uids"], ["UID_push_worker"])
            self.assertEqual(notice["url"], url)
            self.assertEqual(notice["summary"], "收到新的订单指派")
            self.assertEqual(notice["contentType"], 2)
            for expected in ("SYN-ORIGINAL", "原目标 &lt;铂金&gt;", "57.25", url, "请前往查看"):
                self.assertIn(expected, notice["content"])
            for absent in ("SYN-CHANGED", "指派之后修改", "99.00", "88.00", "专属合成敏感字段", "交单时间"):
                self.assertNotIn(absent, notice["content"])

            self.assign(operator, game, taker, "00000000000000000112")
            setting = worker.patch(
                "/api/auth/wxpusher/preferences", {"order_events": {"assigned": False}},
                content_type="application/json",
            ).json()
            self.assertEqual(setting["code"], 0)
            self.assertIs(setting["data"]["order_events"]["assigned"], False)
            self.assertIs(setting["data"]["settlement_enabled"], True)

            call_command("send_wxpusher_notices", stdout=StringIO())
            self.assertEqual(len(sent), 1)

    def test_manual_settlement_sends_one_message_with_the_actual_net_payment(self):
        game = Game.objects.create(name="示例游戏")
        operator = make_logged_in_client(
            username="push-finance", groups=("财务",), nodes=("finance.change_settlement_status",),
        )
        make_logged_in_client(username="settled-worker", groups=(WORKER,))
        worker = get_user_model().objects.get(username="settled-worker")
        WxPusherBinding.objects.create(user=worker, app_id="6123", uid="UID_settled_worker")
        order = Order.objects.create(
            game=game, order_no="00000000000000000113", status=OrderStatus.COMPLETED, hall_price="88.00",
        )
        performance = Performance.objects.create(order=order, emp_type=EmpType.MAIN, user=worker, bonus_amount="88.00")
        PerformanceAdjustment.objects.create(
            performance=performance, kind=PerformanceAdjustment.Kind.MANUAL, amount="-11.75", reason="合成扣款",
        )
        sent = []

        def upstream(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_settled_worker", "code": 1000, "messageContentId": 52},
            ]})

        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="6123", WXPUSHER_APP_TOKEN="synthetic-order-push-key",
        ), patch("apps.core.wxpusher.httpx.HTTPTransport", return_value=httpx.MockTransport(upstream)):
            response = operator.post(
                "/api/business/settlements/settle", {"performance_ids": [performance.pk]},
                content_type="application/json",
            )
            self.assertEqual(response.json()["code"], 0, response.content)
            call_command("send_wxpusher_notices", stdout=StringIO())

        self.assertEqual(len(sent), 1)
        notice = sent[0]
        self.assertEqual(notice["uids"], ["UID_settled_worker"])
        self.assertEqual(notice["summary"], "你的订单报酬已结账")
        self.assertIn("1 笔订单", notice["content"])
        self.assertIn("¥76.25", notice["content"])
        self.assertNotIn("¥88.00", notice["content"])
        self.assertIn("结账时间", notice["content"])
        self.assertEqual(notice["url"], f"https://{self.domain.domain}/m/performance")
        self.assertIn(f'<a href="{notice["url"]}">请前往查看</a>', notice["content"])

    def assign(self, operator, game, taker, order_no):
        order = Order.objects.create(
            game=game, order_no=order_no, status=OrderStatus.UNASSIGNED, hall_price="88.00",
            source_order_no="SYN-ORIGINAL" if order_no.endswith("111") else "",
            project_text="原目标 <铂金>", content="专属合成敏感字段",
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, temp_amount="88.00", taker_price="57.25")
        response = operator.post(
            f"/api/business/orders/{order_no}/assign/",
            {"user_id": taker.pk}, content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
