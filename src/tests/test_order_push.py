"""自动发消息到微信群设置（2026-10-17 实装）的判据。

覆盖四层：
1. Webhook 地址校验（SSRF 的第一道：只放行 http(s)，挡回环与云元数据地址）；
2. 消息拼装（企微 text / markdown、自定义通用 JSON、@全员 的差异）；
3. 规则匹配（时机 / 游戏 / 启用状态）与「提交后才发」；
4. 接口层：CRUD 的 RBAC 节点、`games` 必填、测试消息端点。
"""
import json
from decimal import Decimal
from unittest import mock

from django_tenants.test.cases import TenantTestCase

from apps.business import order_push
from apps.business.models import Game, Order, OrderPushSetting, OrderStatus
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.order_push_group_settings"


def _game(name="原神"):
    return Game.objects.create(name=name)


def _order(game, *, status=OrderStatus.WAITING_GRAB, no="20261017000000000001"):
    return Order.objects.create(
        game=game,
        order_no=no,
        status=status,
        project_text="深渊满星",
        hall_price=Decimal("88.00"),
        total_price=Decimal("100.00"),
    )


def _setting(game, **kwargs):
    values = {
        "channel": OrderPushSetting.Channel.WECOM,
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
        "timing": OrderPushSetting.Timing.HALL,
        "msg_type": OrderPushSetting.MsgType.MARKDOWN,
        "call_all": False,
        "is_active": True,
    }
    values.update(kwargs)
    row = OrderPushSetting.objects.create(**values)
    row.games.add(game)
    return row


class ValidateWebhookUrlTests(TenantTestCase):
    def test_http_and_https_pass(self):
        self.assertEqual(
            order_push.validate_webhook_url("https://example.com/hook"),
            "https://example.com/hook",
        )
        self.assertEqual(
            order_push.validate_webhook_url("http://10.0.0.8:8080/hook"),
            "http://10.0.0.8:8080/hook",
        )

    def test_bad_shapes_rejected(self):
        for raw in ("", "ftp://x", "example.com/hook", "https://"):
            with self.assertRaises(ValueError):
                order_push.validate_webhook_url(raw)

    def test_loopback_and_metadata_rejected(self):
        for raw in (
            "http://localhost:8000/hook",
            "http://127.0.0.1/hook",
            "http://[::1]/hook",
            "http://169.254.169.254/latest/meta-data",
            "http://127.0.0.1./hook",
            "http://[::ffff:127.0.0.1]/hook",
            "http://[fe80::1]/hook",
            "http://0.0.0.0/hook",
        ):
            with self.assertRaises(ValueError):
                order_push.validate_webhook_url(raw)

    def test_private_networks_are_allowed_on_purpose(self):
        # 租户可能把自定义 Webhook 放在内网；这是**有意**放行，不是遗漏。
        self.assertEqual(
            order_push.validate_webhook_url("http://192.168.1.20/hook"),
            "http://192.168.1.20/hook",
        )


class BuildPayloadTests(TenantTestCase):
    def setUp(self):
        self.game = _game()
        self.order = _order(self.game)

    def test_wecom_markdown_shape(self):
        setting = _setting(self.game)
        payload = order_push.build_payload(setting, self.order, order_push.EVENT_HALL)
        self.assertEqual(payload["msgtype"], "markdown")
        content = payload["markdown"]["content"]
        self.assertIn("新单上架", content)
        self.assertIn("原神", content)
        self.assertIn("深渊满星", content)
        self.assertIn("20261017000000000001", content)
        self.assertIn("¥88.00", content)

    def test_wecom_text_with_call_all_mentions_everyone(self):
        setting = _setting(
            self.game,
            msg_type=OrderPushSetting.MsgType.TEXT,
            call_all=True,
        )
        payload = order_push.build_payload(setting, self.order, order_push.EVENT_HALL)
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["mentioned_list"], ["@all"])
        self.assertIn("新单上架", payload["text"]["content"])

    def test_custom_channel_is_generic_json_with_both_fragments(self):
        setting = _setting(
            self.game,
            channel=OrderPushSetting.Channel.CUSTOM,
            webhook_url="https://example.com/hook",
        )
        payload = order_push.build_payload(
            setting,
            self.order,
            order_push.EVENT_IMPORTANT,
            from_status=OrderStatus.WAITING_GRAB,
        )
        self.assertEqual(payload["event"], "order.important")
        self.assertEqual(payload["order_no"], self.order.order_no)
        self.assertEqual(payload["status"], OrderStatus.WAITING_GRAB)
        self.assertEqual(payload["from_status"], OrderStatus.WAITING_GRAB)
        self.assertIn("content", payload)
        self.assertIn("text", payload)
        self.assertIn("markdown", payload)
        self.assertEqual(payload["msg_type"], "markdown")

    def test_remark_is_appended_but_no_customer_pii(self):
        setting = _setting(self.game, remark="新人优先")
        self.order.customer_phone = "13800000000"
        self.order.save(update_fields=["customer_phone"])
        text = order_push.build_payload(
            setting, self.order, order_push.EVENT_HALL
        )["markdown"]["content"]
        self.assertIn("新人优先", text)
        self.assertNotIn("13800000000", text)

    def test_test_message_price_is_never_the_python_literal_none(self):
        """「发送测试消息」没有订单 ⇒ 两个金额都是 None（E-0263-F2）。

        修前：`build_payload` 的 `str(...)` 把 Python 字面量 `"None"` 混进 custom 渠道
        的对外 JSON（`"price":"None"`）；修后与人话正文 `_money` 同款口径 = 「未设置」。
        钉**值**（`AGENTS.md` §5-G / E-0024），并扫整个 JSON 不许出现 `"None"`。
        """
        setting = _setting(
            self.game,
            channel=OrderPushSetting.Channel.CUSTOM,
            webhook_url="https://example.com/hook",
        )
        payload = order_push.build_test_payload(setting)
        self.assertEqual(payload["price"], "未设置")
        self.assertNotIn("None", json.dumps(payload, ensure_ascii=False))


class HttpShapeTests(TenantTestCase):
    class _Response:
        status = 200

        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def test_wecom_errcode_check_is_not_applied_to_custom_channels(self):
        body = b'{"errcode": 1, "msg": "custom ok"}'
        with mock.patch.object(
            order_push._OPENER, "open", return_value=self._Response(body)
        ):
            # 自定义渠道：`errcode` 是它自己的字段，不该按企微语义判失败。
            self.assertEqual(
                order_push._post_json(
                    "https://example.com/hook", {}, check_wecom_errcode=False
                ),
                {"errcode": 1, "msg": "custom ok"},
            )
            # 企微渠道：非 0 errcode 必须当失败。
            with self.assertRaises(order_push.PushDeliveryError):
                order_push._post_json(
                    "https://example.com/hook", {}, check_wecom_errcode=True
                )


class QueueMatchingTests(TenantTestCase):
    def setUp(self):
        self.game = _game()
        self.other = _game("鸣潮")
        self.order = _order(self.game)

    def test_hall_event_matches_timing_0_and_10_only_for_that_game(self):
        hall = _setting(self.game, timing=OrderPushSetting.Timing.HALL)
        both = _setting(self.game, timing=OrderPushSetting.Timing.HALL_AND_PLATFORM)
        _setting(self.game, timing=OrderPushSetting.Timing.IMPORTANT)
        _setting(self.other, timing=OrderPushSetting.Timing.HALL)
        _setting(self.game, timing=OrderPushSetting.Timing.HALL, is_active=False)

        with mock.patch.object(order_push, "_deliver_later") as deliver:
            sent = order_push.queue_order_push(self.order, order_push.EVENT_HALL)
        self.assertEqual(sent, 2)
        self.assertEqual({c.args[0] for c in deliver.call_args_list}, {hall.pk, both.pk})

    def test_important_event_matches_timing_30_only(self):
        """对标的 `0` 是「大厅 + 交易平台」，**不是**「大厅 + 重要变化」⇒ 重要事件只匹配 30。"""
        hall_and_platform = _setting(
            self.game, timing=OrderPushSetting.Timing.HALL_AND_PLATFORM
        )
        important = _setting(self.game, timing=OrderPushSetting.Timing.IMPORTANT)
        with mock.patch.object(order_push, "_deliver_later") as deliver:
            sent = order_push.queue_order_push(self.order, order_push.EVENT_IMPORTANT)
        self.assertEqual(sent, 1)
        self.assertEqual(deliver.call_args.args[0], important.pk)
        self.assertNotEqual(deliver.call_args.args[0], hall_and_platform.pk)

    def test_platform_event_matching_exists_but_has_no_caller_today(self):
        """业务上「平台发单」还没接（M6）；函数本身能匹配，但没有任何调用方。"""
        _setting(self.game, timing=OrderPushSetting.Timing.PLATFORM)
        with mock.patch.object(order_push, "_deliver_later") as deliver:
            sent = order_push.queue_order_push(self.order, order_push.EVENT_PLATFORM)
        self.assertEqual(sent, 1)
        self.assertEqual(deliver.call_count, 1)

    def test_status_change_picks_hall_or_important_not_both(self):
        self.order.status = OrderStatus.WAITING_GRAB
        with mock.patch.object(order_push, "queue_order_push") as queue:
            queue.return_value = 1
            order_push.queue_status_change_push(
                self.order, from_status=OrderStatus.UNASSIGNED, hall_publisher=object()
            )
        queue.assert_called_once_with(self.order, order_push.EVENT_HALL)

        self.order.status = OrderStatus.IN_PROGRESS
        with mock.patch.object(order_push, "queue_order_push") as queue:
            queue.return_value = 1
            order_push.queue_status_change_push(
                self.order, from_status=OrderStatus.WAITING_GRAB, hall_publisher=None
            )
        queue.assert_called_once_with(
            self.order, order_push.EVENT_IMPORTANT, from_status=OrderStatus.WAITING_GRAB
        )

    def test_ignorable_status_does_not_push(self):
        self.order.status = OrderStatus.UNASSIGNED
        with mock.patch.object(order_push, "queue_order_push") as queue:
            sent = order_push.queue_status_change_push(
                self.order, from_status=OrderStatus.ISSUE, hall_publisher=None
            )
        self.assertEqual(sent, 0)
        queue.assert_not_called()

    def test_deliver_later_carries_plain_values_not_a_model(self):
        """线程只拿普通值：多租户下新线程没有请求线程的 schema，不能再查库。"""
        setting = _setting(self.game)
        with mock.patch.object(order_push, "_deliver_later") as deliver:
            order_push.queue_order_push(self.order, order_push.EVENT_HALL)
        kwargs = deliver.call_args.kwargs
        self.assertEqual(kwargs["url"], setting.webhook_url)
        self.assertEqual(kwargs["channel"], OrderPushSetting.Channel.WECOM)
        self.assertIn("markdown", kwargs["payload"])
        # 第一个位置参数仍是规则主键（日志 / 线程名用）
        self.assertEqual(deliver.call_args.args[0], setting.pk)

    def test_thread_entry_never_queries_the_database(self):
        with mock.patch.object(order_push, "_send") as send:
            order_push._send_in_thread(
                setting_id=9,
                url="https://example.com/hook",
                channel="custom",
                payload={"content": "x"},
            )
        send.assert_called_once_with(
            setting_id=9,
            url="https://example.com/hook",
            channel="custom",
            payload={"content": "x"},
        )


class PushSettingApiTests(TenantTestCase):
    def setUp(self):
        self.game1 = _game("原神")
        self.game2 = _game("鸣潮")
        self.client = make_logged_in_client(username="push-ops", groups=("客服",), nodes=(NODE,))

    def _payload(self, **overrides):
        data = {
            "name": "大厅提醒",
            "channel": "wecom",
            "webhook_url": "https://example.com/hook",
            "games": [self.game1.id, self.game2.id],
            "timing": 10,
            "msg_type": "markdown",
            "call_all": False,
            "remark": "秒回",
            "is_active": True,
        }
        data.update(overrides)
        return data

    def test_create_list_and_patch(self):
        created = self.client.post(
            "/api/business/order-push-settings/",
            self._payload(),
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0)
        self.assertEqual(sorted(created["data"]["games"]), sorted([self.game1.id, self.game2.id]))

        listed = self.client.get("/api/business/order-push-settings/").json()
        self.assertEqual(listed["code"], 0)
        self.assertEqual(listed["data"]["total"], 1)

        patched = self.client.patch(
            f"/api/business/order-push-settings/{created['data']['id']}/",
            {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(patched["code"], 0)
        self.assertFalse(patched["data"]["is_active"])

    def test_games_is_required(self):
        body = self.client.post(
            "/api/business/order-push-settings/",
            self._payload(games=[]),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("至少要选择一个游戏", body["msg"])

    def test_non_http_url_is_1000(self):
        body = self.client.post(
            "/api/business/order-push-settings/",
            self._payload(webhook_url="ftp://example.com/hook"),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)

    def test_test_endpoint_calls_sender_and_reports_failure(self):
        row = _setting(self.game1)
        with mock.patch(
            "apps.business.api.order_push_views.send_test_message",
            return_value=None,
        ) as sender:
            ok = self.client.post(
                f"/api/business/order-push-settings/{row.id}/test/"
            ).json()
        self.assertEqual(ok["code"], 0)
        sender.assert_called_once()

        with mock.patch(
            "apps.business.api.order_push_views.send_test_message",
            side_effect=order_push.PushDeliveryError("Webhook 返回 HTTP 500（example.com）"),
        ):
            bad = self.client.post(
                f"/api/business/order-push-settings/{row.id}/test/"
            ).json()
        self.assertEqual(bad["code"], 1002)
        self.assertIn("HTTP 500", bad["msg"])
        self.assertNotIn("key=", bad["msg"])

    def test_without_node_is_403(self):
        nobody = make_logged_in_client(username="push-nobody")
        response = nobody.get("/api/business/order-push-settings/")
        self.assertEqual(response.status_code, 403)


class TriggerIntegrationTests(TenantTestCase):
    """真实抛单端点 → `apply_transition` → 推送钩子（不是直接调函数）。"""

    def test_throw_to_hall_enqueues_a_hall_message(self):
        game = _game()
        order = _order(game, status=OrderStatus.UNASSIGNED)
        setting = _setting(game, timing=OrderPushSetting.Timing.HALL)
        client = make_logged_in_client(
            username="push-thrower",
            groups=("客服",),
            nodes=("order.send_to_grab_hall", "order.submit_payout_price"),
        )
        with mock.patch.object(order_push, "_deliver_later") as deliver:
            body = client.post(
                f"/api/business/orders/{order.order_no}/throw-to-hall/",
                {"price": "88.00"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0)
        deliver.assert_called_once()
        kwargs = deliver.call_args.kwargs
        self.assertEqual(deliver.call_args.args[0], setting.pk)
        self.assertEqual(kwargs["url"], setting.webhook_url)
        self.assertIn("新单上架", kwargs["payload"]["markdown"]["content"])
        self.assertIn(order.order_no, kwargs["payload"]["markdown"]["content"])

    def test_push_failure_does_not_rollback_the_transition(self):
        """群消息是提示不是凭据：推送入队炸了，订单状态仍要改成功。"""
        game = _game()
        order = _order(game, status=OrderStatus.UNASSIGNED)
        client = make_logged_in_client(
            username="push-thrower2",
            groups=("客服",),
            nodes=("order.send_to_grab_hall", "order.submit_payout_price"),
        )
        with mock.patch.object(
            order_push, "queue_status_change_push", side_effect=RuntimeError("boom")
        ):
            body = client.post(
                f"/api/business/orders/{order.order_no}/throw-to-hall/",
                {"price": "88.00"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
