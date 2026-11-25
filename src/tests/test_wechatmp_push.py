"""公众号模板消息推送渠道（计划 T5）的判据。

## 判据打在哪

- **载荷钉值**：`build_template_payload` 纯函数（hall / important 两档逐字段钉）；
- **收件人**：`sales_user`（S5 钉字段）与 MAIN 接手打手中**已绑定且 subscribed** 的人；
  同一个人两重身份 ⇒ **一**条（去重）；
- **线程禁查库**：`_deliver_template_later` 收到的 kwargs 全是**普通值**
  （token 字符串 / payload dict / openid 字符串，⛔ 没有任何模型实例）；
- **token 行锁刷新**：`access_token` / `token_expires_at` 落到**配置行**（终态）；
  未过期不取（`cgi-bin/token` 计数不涨）、过期才取；
- **失败口径**：errcode 信封 / HTTP 错 ⇒ 只记日志（不抛、不重试）；
- **零调用**：未启用 / 无 template / 无人绑定 / 未订阅 ⇒ stub 一个请求都没有。
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.wechatmp.models import WechatMpBinding, WechatMpConfig
from apps.wechatmp import push
from tests._wechatmp_helpers import (
    APPID,
    APPSECRET,
    OPENID,
    OPENID_OTHER,
    TEMPLATE_ID,
    TOKEN,
    WechatApiStub,
)

TOKEN_OK = {"access_token": "token-from-stub", "expires_in": 7200}


def _game(name="原神"):
    return Game.objects.create(name=name)


def _order(game, *, no="20261019000000000001"):
    return Order.objects.create(
        game=game,
        order_no=no,
        status=OrderStatus.IN_PROGRESS,
        project_text="深渊满星",
        hall_price=Decimal("88.00"),
        total_price=Decimal("100.00"),
    )


class PushBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.stub = WechatApiStub().install()
        self.addCleanup(self.stub.stop)
        self.user_sales = self._user("mp-push-sales")
        self.user_taker = self._user("mp-push-taker")

    @staticmethod
    def _user(name):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(username=name, password=f"pw-{name}")

    def enable(self, **overrides):
        values = {
            "appid": APPID,
            "appsecret": APPSECRET,
            "token": TOKEN,
            "enabled": True,
            "template_id": TEMPLATE_ID,
        }
        values.update(overrides)
        return WechatMpConfig.objects.create(**values)

    def bind(self, user, openid, *, subscribed=True):
        return WechatMpBinding.objects.create(
            user=user, appid=APPID, openid=openid, subscribed=subscribed
        )


class BuildTemplatePayloadTests(PushBase):
    def test_hall_payload_is_pinned(self):
        order = _order(_game())
        payload = push.build_template_payload(
            order, "hall", None, url="https://t.example/m/"
        )
        self.assertEqual(payload["data"]["first"]["value"], "新单上架")
        self.assertEqual(payload["data"]["keyword1"]["value"], "原神")
        self.assertEqual(payload["data"]["keyword2"]["value"], order.order_no)
        self.assertEqual(payload["data"]["keyword3"]["value"], "发单价：¥88.00")
        self.assertEqual(payload["url"], "https://t.example/m/")
        # touser / template_id 由投递侧按收件人填（构造函数留空）。
        self.assertEqual(payload["touser"], "")
        self.assertEqual(payload["template_id"], "")

    def test_important_payload_carries_status_transition(self):
        order = _order(_game())
        payload = push.build_template_payload(
            order, "important", OrderStatus.WAITING_GRAB, url=""
        )
        self.assertEqual(payload["data"]["first"]["value"], "订单动态")
        self.assertEqual(
            payload["data"]["keyword3"]["value"],
            f"状态：{OrderStatus.WAITING_GRAB.label} → {OrderStatus.IN_PROGRESS.label}",
        )
        self.assertNotIn("url", payload)  # 基址为空 ⇒ 不带跳转


class QueueWechatmpPushTests(PushBase):
    def test_disabled_config_sends_nothing(self):
        self.enable(enabled=False)
        self.bind(self.user_sales, OPENID)
        sent = push.queue_wechatmp_push(_order(_game()), "hall")
        self.assertEqual(sent, 0)
        self.assertEqual(self.stub.requests, [])

    def test_missing_template_sends_nothing(self):
        self.enable(template_id="")
        self.bind(self.user_sales, OPENID)
        sent = push.queue_wechatmp_push(_order(_game()), "hall")
        self.assertEqual(sent, 0)
        self.assertEqual(self.stub.requests, [])

    def test_no_bindings_sends_nothing_and_fetches_no_token(self):
        self.enable()
        sent = push.queue_wechatmp_push(_order(_game()), "hall")
        self.assertEqual(sent, 0)
        self.assertEqual(self.stub.requests, [])  # 收件人判断在取 token **之前**

    def test_bound_sales_user_gets_one_message_with_plain_values(self):
        self.enable()
        self.bind(self.user_sales, OPENID)
        order = _order(_game())
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        self.stub.enqueue(dict(TOKEN_OK))
        with mock.patch.object(push, "_deliver_template_later") as deliver, self.captureOnCommitCallbacks(
            execute=True
        ):
            sent = push.queue_wechatmp_push(order, "hall")
        self.assertEqual(sent, 1)  # ⭐ M1：返回的是**排队**数（提交后真发）
        kwargs = deliver.call_args.kwargs
        # ⛔ 线程禁查库：交出去的全是普通值（django-tenants schema 是连接级状态）。
        self.assertIsInstance(kwargs["access_token"], str)
        self.assertIsInstance(kwargs["payload"], dict)
        self.assertEqual(kwargs["payload"]["touser"], OPENID)
        self.assertEqual(kwargs["payload"]["template_id"], TEMPLATE_ID)
        self.assertIsInstance(kwargs["openid"], str)

    def test_unsubscribed_binding_is_skipped(self):
        self.enable()
        self.bind(self.user_sales, OPENID, subscribed=False)
        order = _order(_game())
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        sent = push.queue_wechatmp_push(order, "hall")
        self.assertEqual(sent, 0)

    def test_main_taker_receives_and_dedup_happens(self):
        self.enable()
        self.bind(self.user_taker, OPENID)
        game = _game()
        order = _order(game)
        order.sales_user = self.user_taker  # 同一人：销售归属 + MAIN 接手
        order.save(update_fields=["sales_user"])
        Performance.objects.create(order=order, user=self.user_taker, emp_type=EmpType.MAIN)
        self.stub.enqueue(dict(TOKEN_OK))
        with mock.patch.object(push, "_deliver_template_later") as deliver, self.captureOnCommitCallbacks(
            execute=True
        ):
            sent = push.queue_wechatmp_push(order, "important", from_status=OrderStatus.WAITING_GRAB)
        self.assertEqual(sent, 1)  # ⛔ 去重：同一人只收一条
        self.assertEqual(deliver.call_args.kwargs["payload"]["touser"], OPENID)

    def test_sales_user_and_taker_both_bound_get_two(self):
        self.enable()
        self.bind(self.user_sales, OPENID)
        self.bind(self.user_taker, OPENID_OTHER)
        game = _game()
        order = _order(game, no="20261019000000000002")
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        Performance.objects.create(order=order, user=self.user_taker, emp_type=EmpType.MAIN)
        self.stub.enqueue(dict(TOKEN_OK))
        with mock.patch.object(push, "_deliver_template_later") as deliver, self.captureOnCommitCallbacks(
            execute=True
        ):
            sent = push.queue_wechatmp_push(order, "hall")
        self.assertEqual(sent, 2)
        self.assertEqual(
            {c.kwargs["payload"]["touser"] for c in deliver.call_args_list},
            {OPENID, OPENID_OTHER},
        )

    def test_token_persisted_and_reused_within_expiry(self):
        self.enable()
        self.bind(self.user_sales, OPENID)
        order = _order(_game())
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        self.stub.enqueue(dict(TOKEN_OK))
        with self.captureOnCommitCallbacks(execute=True):
            push.queue_wechatmp_push(order, "hall")
        row = WechatMpConfig.objects.get()
        self.assertEqual(row.access_token, "token-from-stub")  # ⭐ 库表持久化（终态）
        self.assertIsNotNone(row.token_expires_at)
        self.assertGreater(row.token_expires_at, timezone.now())
        # 第二次：token 未过期 ⇒ 不再取（stub 里没有余粮，再取就会拿到底线回包）。
        with mock.patch.object(push, "_deliver_template_later"), self.captureOnCommitCallbacks(
            execute=True
        ):
            push.queue_wechatmp_push(order, "hall")
        self.assertEqual(self.stub.call_count_for("cgi-bin/token"), 1)

    def test_expired_token_is_refreshed(self):
        from datetime import timedelta

        self.enable(access_token="stale-token")
        # 显式给一个已过期的时刻：行锁刷新的 expires_at 双查必须判它过期。
        WechatMpConfig.objects.update(token_expires_at=timezone.now() - timedelta(hours=1))
        self.bind(self.user_sales, OPENID)
        order = _order(_game())
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        self.stub.enqueue(dict(TOKEN_OK))
        with mock.patch.object(push, "_deliver_template_later"), self.captureOnCommitCallbacks(
            execute=True
        ):
            push.queue_wechatmp_push(order, "hall")
        self.assertEqual(self.stub.call_count_for("cgi-bin/token"), 1)
        self.assertEqual(WechatMpConfig.objects.get().access_token, "token-from-stub")


class SendTemplateFailureTests(PushBase):
    def test_failure_envelope_is_logged_not_raised(self):
        self.stub.enqueue({"errcode": 43004, "errmsg": "require subscribe"})
        # ⛔ 不抛：照 webhook 通道口径只记日志（v1 不重试、不落库）。
        # ⭐ M3（独立评审）：「不抛」不构成判据 —— 钉住**真的走了失败分支**
        #    （失败信封被 judge_body 判成失败并落了 warning；否则桩的回包被静默吞掉也绿）。
        with self.assertLogs("apps.wechatmp.push", level="WARNING") as logs:
            push._send_template(
                access_token="tok", payload={"touser": OPENID}, openid=OPENID
            )
        self.assertTrue(any("43004" in line or "require subscribe" in line for line in logs.output),
                        f"失败分支没有留下 warning：{logs.output}")

    def test_http_error_is_logged_not_raised(self):
        push._send_template(
            access_token="tok", payload={"touser": OPENID}, openid=OPENID
        )  # stub 无余粮 ⇒ 底线回包 errcode=-1，同样不抛


class IntegrationThroughQueueOrderPushTests(PushBase):
    def test_queue_order_push_dispatches_mp_channel_without_rules(self):
        """租户级开关：一条 webhook 规则都不配，模板消息照样发（H2 订正后的形态）。"""
        self.enable()
        self.bind(self.user_sales, OPENID)
        game = _game()
        order = _order(game)
        order.sales_user = self.user_sales
        order.save(update_fields=["sales_user"])
        from apps.business import order_push

        self.stub.enqueue(dict(TOKEN_OK))
        with mock.patch.object(push, "_deliver_template_later") as deliver, self.captureOnCommitCallbacks(
            execute=True
        ):
            sent = order_push.queue_order_push(order, order_push.EVENT_HALL)
        self.assertEqual(sent, 1)  # 规则 0 条 + MP 1 条
        deliver.assert_called_once()
