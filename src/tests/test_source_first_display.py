r"""全端「来源单号优先展示」统一口径（D-0301，2026-10-24）的后端值断言。

口径（D-0301 四条裁定的第 1 / 3 条，只覆盖**本文件**管的那些面）：
**展示值 = `source_order_no` 非空取它，否则回退内置单号 `order_no`**。

每类改点至少两条变体（spec §6 的两变体写法）：

- 夹具来源号非空 ⇒ 返回/渲染的值**逐字**是来源号（`SRC-TEST-…`）；
- 来源号为空（`Order.source_order_no` 是 `blank=True` 的非 NULL 列 ⇒ 空串）⇒ 回落内置号。

⛔ 寻址键不在本文件管：`lookup_field`、URL `\d{20}`、`tt_order_no` 传参等一律照旧内置号。
⛔ 推送里的**机器**字段（custom 渠道 JSON 顶层 `order_no`）也不换——见 `OrderPushTextSwapTests`。
夹具一律合成值（E-0077）；期望值写**字面值**（E-0024）；`mock` 不需要（全走真实出口）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Notification,
    Order,
    OrderPushSetting,
    OrderRefund,
    OrderStatus,
    Performance,
    UpstreamPriceChangeLog,
)
from apps.business.notifications import notify_assigned
from apps.business.order_push import build_payload as order_push_build_payload
from apps.qqbot.payloads import build_hall_texts
from apps.wechatmp.push import (
    KEYWORD_VALUE_MAX,
    build_template_payload,
)
from tests.p5_auth_helpers import make_logged_in_client

MY_ORDERS_URL = "/api/business/my-orders/"
NOTIFICATIONS_URL = "/api/business/notifications/"
REFUND_NODE = "order.online_refund"

#: 合成来源单号（自由文本形状，与 20 位内置号**不同形** ⇒ 断言能分清谁是谁）。
SRC = "SRC-TEST-1"
#: 合成内置单号（20 位形状，照 `order_no.py` 的形状、值本身无意义）。
NO = "20261024000000000001"


def _game(name="来源展示游戏"):
    return Game.objects.create(name=name)


def _order(game, *, no=NO, source_order_no=SRC, **kw):
    fields = {
        "game": game,
        "order_no": no,
        "status": OrderStatus.IN_PROGRESS,
        "hall_price": Decimal("88.00"),
        "total_price": Decimal("100.00"),
        "project_text": "深渊满星",
        "source_order_no": source_order_no,
    }
    fields.update(kw)
    return Order.objects.create(**fields)


class _MyOrdersFixture(TenantTestCase):
    """「我接的单」列表 / 详情共用的最小夹具（夹具形态照 `test_my_orders_api.py`）。"""

    def setUp(self):
        self.game = _game()
        # ⚠️ 游戏先建、账号后建（`make_logged_in_client` 不传 `games` 给全部游戏，D-0136）。
        self.client = make_logged_in_client(
            username="taker-src", groups=("代练员",),
            nodes=("my_orders.send_to_my_system",),
        )
        self.taker = User.objects.get(username="taker-src")
        self.order = _order(self.game)
        self.perf = Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=self.taker,
            temp_amount=Decimal("88.00"),
            bonus_amount=Decimal("8.80"),
        )

    def _row(self, url=MY_ORDERS_URL):
        body = self.client.get(url).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵
        return items[0]


class MyOrdersListCarriesBothNumbersTests(_MyOrdersFixture):
    def test_row_shows_source_when_present(self):
        """来源号非空 ⇒ 行里 `source_order_no` 逐字是来源号、`order_no` 照旧内置号。"""
        row = self._row()
        self.assertEqual(row["source_order_no"], SRC)
        self.assertEqual(row["order_no"], NO)

    def test_blank_source_serializes_as_empty_string(self):
        """来源号为空 ⇒ `source_order_no` 是**空串**（回退由前端做、载荷给原值）。"""
        Order.objects.all().update(source_order_no="")
        row = self._row()
        self.assertEqual(row["source_order_no"], "")
        self.assertIsNotNone(row["source_order_no"])
        self.assertEqual(row["order_no"], NO)


class MyOrderDetailCarriesBothNumbersTests(_MyOrdersFixture):
    DETAIL_URL = f"{MY_ORDERS_URL}{NO}/detail/"

    def test_detail_shows_source_when_present(self):
        body = self.client.get(self.DETAIL_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["source_order_no"], SRC)
        self.assertEqual(body["data"]["order_no"], NO)

    def test_blank_source_is_an_empty_string(self):
        Order.objects.all().update(source_order_no="")
        body = self.client.get(self.DETAIL_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["source_order_no"], "")
        self.assertEqual(body["data"]["order_no"], NO)


class NotificationCarriesSourceTests(TenantTestCase):
    """通知两条路（建行 payload / 读面行）都给出来源号（D-0301 §1.1）。"""

    def setUp(self):
        self.game = _game()
        self.order = _order(self.game)
        self.recipient = User.objects.create_user(username="notify-src")

    def test_payload_row_writes_both_numbers(self):
        """⭐ 建行 payload 恰好两键：来源号非空 ⇒ payload 逐字带来源号。"""
        row = notify_assigned(order=self.order, user_id=self.recipient.pk)
        self.assertEqual(row.payload, {"order_no": NO, "source_order_no": SRC})

    def test_payload_blank_source_is_empty_string(self):
        Order.objects.all().update(source_order_no="")
        self.order.refresh_from_db()
        row = notify_assigned(order=self.order, user_id=self.recipient.pk)
        self.assertEqual(row.payload, {"order_no": NO, "source_order_no": ""})

    def test_list_row_reads_source_from_the_order_fk(self):
        """读面行**实时读 `order` FK**（存量 payload 没这个键也能回值）。"""
        client = make_logged_in_client(username="notify-row", groups=("代练员",))
        recipient = User.objects.get(username="notify-row")
        Notification.objects.create(
            user=recipient, kind="被指派", order=self.order,
            # ⚠️ 故意写**旧形状**的 payload（没有 source 键）——模拟存量行。
            payload={"order_no": NO},
        )
        body = client.get(NOTIFICATIONS_URL).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵
        self.assertEqual(items[0]["source_order_no"], SRC)
        self.assertEqual(items[0]["order_no"], NO)


class RefundRowCarriesSourceTests(TenantTestCase):
    REFUNDS_URL = f"/api/business/orders/{NO}/refunds"

    def setUp(self):
        self.game = _game()
        self.order = _order(self.game)
        self.client = make_logged_in_client(
            username="refund-src", groups=("客服",), nodes=(REFUND_NODE,)
        )

    def _row(self):
        body = self.client.get(self.REFUNDS_URL).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵
        return items[0]

    def test_row_carries_the_source_when_present(self):
        OrderRefund.objects.create(
            order=self.order, kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"), reason="测试退款", operator="客服甲",
        )
        row = self._row()
        self.assertEqual(row["source_order_no"], SRC)
        self.assertEqual(row["order_no"], NO)

    def test_blank_source_is_an_empty_string_not_the_system_no(self):
        Order.objects.all().update(source_order_no="")
        OrderRefund.objects.create(
            order=self.order, kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"), reason="测试退款", operator="客服甲",
        )
        row = self._row()
        self.assertEqual(row["source_order_no"], "")
        self.assertEqual(row["order_no"], NO)


class UpstreamPriceLogSourceTests(TenantTestCase):
    """改价记录页：行补来源号 + 搜索双列（D-0301 §1.1 / §1.3）。"""

    LOG_NODE = "order.upstream_price_change_logs"
    LOG_URL = "/api/business/upstream-price-change-logs/"
    #: 留痕行自己的单号（本表只有内置号；来源号要回查订单）。
    LOG_NO = "20260926000000000091"

    def setUp(self):
        from datetime import datetime, timedelta, timezone as dt_timezone

        self.game = _game()
        self.order = _order(self.game, no=self.LOG_NO)
        self.client = make_logged_in_client(
            username="log-src", groups=("客服",), nodes=(self.LOG_NODE,)
        )
        UpstreamPriceChangeLog.objects.create(
            order_no=self.LOG_NO,
            game_name=self.game.name,
            game_role_name="角色甲",
            before_amount=Decimal("10.00"),
            after_amount=Decimal("12.00"),
            source_name="测试上家甲",
            note="上家改价",
            # ⚠️ `occurred_at` 非空列：给一个合成时刻（固定 +08:00，不依赖本机 tzdata；
            # 形态照 `test_upstream_price_change_log.py` 的 `local()`）。
            occurred_at=datetime(2026, 10, 24, 9, 0, tzinfo=dt_timezone(timedelta(hours=8))),
        )

    def _row(self, query=""):
        body = self.client.get(self.LOG_URL + query).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵
        return items[0]

    def test_row_carries_the_live_source_order_no(self):
        row = self._row()
        self.assertEqual(row["source_order_no"], SRC)
        self.assertEqual(row["order_no"], self.LOG_NO)

    def test_row_of_a_deleted_order_keeps_the_source(self):
        """软删单的留痕行还在 ⇒ 来源号照常回出（读的是 `all_objects` 回查）。"""
        from django.utils import timezone

        Order.objects.filter(order_no=self.LOG_NO).update(deleted_at=timezone.now())
        row = self._row()
        self.assertIs(row["is_deleted"], True)
        self.assertEqual(row["source_order_no"], SRC)

    def test_search_hits_by_the_source_order_no(self):
        """来源号片段能命中（⭐ 双向之一）。"""
        row = self._row("?key=SRC-TEST")
        self.assertEqual(row["order_no"], self.LOG_NO)

    def test_search_still_hits_by_the_system_no(self):
        """系统号照旧命中（⭐ 双向之二）。"""
        row = self._row(f"?key={self.LOG_NO}")
        self.assertEqual(row["order_no"], self.LOG_NO)


class OrderPushTextSwapTests(TenantTestCase):
    """企微机器人 / 自定义 webhook 的**人读文本**换值（D-0301 §1.2）。"""

    def setUp(self):
        self.game = _game()
        self.order = _order(self.game)

        def _setting(**kw):
            fields = {
                "channel": OrderPushSetting.Channel.WECOM,
                "webhook_url": "https://example.com/hook",
                "timing": OrderPushSetting.Timing.HALL,
            }
            fields.update(kw)
            setting = OrderPushSetting.objects.create(**fields)
            setting.games.add(self.game)
            return setting

        self.text_setting = _setting(msg_type=OrderPushSetting.MsgType.TEXT)
        self.markdown_setting = _setting(msg_type=OrderPushSetting.MsgType.MARKDOWN)

    def test_text_and_markdown_show_the_source_when_present(self):
        text = order_push_build_payload(self.text_setting, self.order, "hall")
        markdown = order_push_build_payload(
            self.markdown_setting, self.order, "important", from_status="WAITING_GRAB"
        )
        self.assertIn(f"订单号：{SRC}", text["text"]["content"])
        self.assertIn(f"订单号：{SRC}", markdown["markdown"]["content"])

    def test_blank_source_falls_back_to_the_system_no(self):
        Order.objects.all().update(source_order_no="")
        self.order.refresh_from_db()
        text = order_push_build_payload(self.text_setting, self.order, "hall")
        markdown = order_push_build_payload(
            self.markdown_setting, self.order, "important", from_status="WAITING_GRAB"
        )
        self.assertIn(f"订单号：{NO}", text["text"]["content"])
        self.assertNotIn(SRC, text["text"]["content"])
        self.assertIn(f"订单号：{NO}", markdown["markdown"]["content"])

    def test_custom_channel_top_level_order_no_stays_the_system_no(self):
        """⛔ 机器合同字段不换：custom 渠道 JSON 顶层 `order_no` 仍逐字是内置号。"""
        setting = OrderPushSetting.objects.create(
            channel=OrderPushSetting.Channel.CUSTOM,
            webhook_url="https://example.com/hook",
            timing=OrderPushSetting.Timing.HALL,
        )
        setting.games.add(self.game)
        payload = order_push_build_payload(setting, self.order, "hall")
        self.assertEqual(payload["order_no"], NO)


class QqbotHallTextSwapTests(TenantTestCase):
    """QQ 群大厅推送：**格式**是 D-0265 的契约 ⛔ 不动，**值**按 D-0301 换。"""

    def setUp(self):
        self.game = _game()

    def test_order_no_line_shows_the_source_when_present(self):
        order = _order(self.game, no="20261019000000000001")
        text, markdown = build_hall_texts(order, "可行工作室")
        self.assertIn(f"订单号：{SRC}", text)
        self.assertIn(f"订单号：{SRC}", markdown)
        # ⚠️ 格式契约的另一半：仍是最后一行、行首标签逐字不变（D-0265）。
        self.assertEqual(text.splitlines()[-1], f"订单号：{SRC}")

    def test_blank_source_falls_back_to_the_system_no(self):
        order = _order(self.game, no="20261019000000000001", source_order_no="")
        text, _markdown = build_hall_texts(order, "可行工作室")
        self.assertIn("订单号：20261019000000000001", text)


class WechatmpKeyword2SwapTests(TenantTestCase):
    """公众号模板 keyword2：换值（D-0301 §1.2）+ 超长自截（KEYWORD_VALUE_MAX）。"""

    def setUp(self):
        self.game = _game()
        self.order = _order(self.game)

    def test_keyword2_shows_the_source_when_present(self):
        payload = build_template_payload(self.order, "hall", None, url="")
        self.assertEqual(payload["data"]["keyword2"]["value"], SRC)

    def test_blank_source_falls_back_to_the_system_no(self):
        Order.objects.all().update(source_order_no="")
        self.order.refresh_from_db()
        payload = build_template_payload(self.order, "hall", None, url="")
        self.assertEqual(payload["data"]["keyword2"]["value"], NO)

    def test_an_overlong_source_is_truncated_by_ourself(self):
        """⚠️ 来源号最长 128、模板 keyword 超长会被微信截 ⇒ 出站前自截到
        `KEYWORD_VALUE_MAX`（= 内置号恰好 20 位 ⇒ 不缩短任何过去发得出去的值）。"""
        Order.objects.all().update(source_order_no="S" * 128)
        self.order.refresh_from_db()
        payload = build_template_payload(self.order, "hall", None, url="")
        value = payload["data"]["keyword2"]["value"]
        self.assertEqual(value, "S" * KEYWORD_VALUE_MAX)
        self.assertLessEqual(len(value), KEYWORD_VALUE_MAX)
