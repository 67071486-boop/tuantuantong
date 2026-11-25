"""平台推送读面：列表徽标 / 第三方订单列表 / 区服映射。

三块判据：① 管理订单列表载荷的 `platform_push`（只读键；有推送行 ⇒ **最新一笔**摘要，
统一口径 ⛔ 不再活跃优先；没有 ⇒ `null`；两条单互不串；**SQL 条数不随订单数增长**，⛔ N+1）；
② `GET /api/business/platform-pushes/`（分页 / 平台筛选 / 上游状态筛选 / 关键字 / 权限 /
租户隔离；载荷**只给今天真有数据源的列**，键集合恰好 50 个；并覆盖 `?group_by=order`、
`counts/` 订单口径 `order_total`/`order_counts`、每行 `arbitration_available`/
`arbitration_blocked_reason`、`cancel_initiator` / `agree_cancel_available` /
`cancel_cmt` / `cancel_reason`）；③ `GET /api/business/platform-integrations/zones/map/`（命中与未命中）。

口径与纪律：断言打在**真出口**上（HTTP 响应体 / 数据库行），⛔ 不从被测代码导出常量对拍自己；
夹具一律**合成值**；跨租户否定断言**不用主键**（两个 schema 主键各自从 1 开始，
`AGENTS.md` §5-G），用本租户内唯一、且两租户取值不同的**单号**；
⛔ 不访问真实网络（本文件不构造任何上游客户端）。
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    DailiantongPush,
    DailiantongZoneMap,
    Game,
    GameServer,
    Order,
    OrderSource,
    OrderStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

LIST_URL = "/api/business/orders/"
PUSHES_URL = "/api/business/platform-pushes/"
ZONE_MAP_URL = "/api/business/platform-integrations/zones/map/"

#: 订单页节点（两条读端点都复用它）。
NODE = "order.list"

#: 代练通平台 id（写死字面量：与契约同值，⛔ 不从实现导出）。
DAILIANTONG = 10

#: `platform_push` 承诺的**恰好**七个键（业务方的形状：6 + `price`）。
PLATFORM_PUSH_KEYS = frozenset(
    {
        "platform",
        "serial_no",
        "state",
        "remote_status",
        "remote_cancel_status",
        "is_active",
        # ⭐ 业务方：「发单价」列在有推送时显示**推送行的 price**
        # （发到代练通 3.00、本地 hall_price 为空时列表却显示 0.00 的根因就在这）。
        # 与 hall_price 吃同一道 `order.restrict_view_offer_price` 脱敏（值可为 None）。
        "price",
    }
)

#: 第三方订单列表行承诺的**恰好** 50 个键——页面**今天真有数据源**的列。
#: ⛔ 利润 / 剩余时间 / 撤销时间这些没有数据源的列**不在**这里，也不许回 `0` / `null` 之类的假值。
#: ⭐ 发群二维码 2 键（14 → 16）：`qr_sent_at` / `qr_last_error`——后端下发后页面 tooltip
#: 才**真的**能看到失败原因（`last_error` 每 30 秒被状态回读清空，不能靠它）。
#: ⭐ 自动回复失败原因（16 → 17）：`auto_reply_last_error`，理由同上。
#: ⭐ 来源与接单 3 键（22 → 25）：`source_order_no`（空则前端回退内置单号）、
#: `accept_price`（接单价 = 本地订单 `total_price`）、`accepted_at`（接单时间 =
#: `remote_accepted_at`，上游详情键 `StartDate`）。
#: ⭐ 仲裁 2 键（25 → 27）：`arbitration_available` / `arbitration_blocked_reason`
#: ——「申请客服介入」按钮的可用性与中文原因（未满 1 小时时含「还需 X 分钟」）。
#: ⭐ 自动仲裁 4 列 + 综合状态 2 键 ⇒ 27 + 4 + 2 = **33**。
#: ⭐ 聚合父行 `is_active`（还值不值得轮询；父行要显示「最新活跃推送」的交易状态）⇒ **34**。
#: ⭐ 第三方页改列：打手 UID / 显示名 / 完成单数 / 投诉取消次数 +
#: 本地利润 / 剩余时间 / 订单来源 / 验收倒计时三格 ⇒ 40 + 10 = **50**。
PLATFORM_PUSH_ROW_KEYS = frozenset(
    {
        "id",
        "platform",
        "serial_no",
        "order_no",
        "game",
        "server",
        "price",
        "state",
        "remote_status",
        "remote_cancel_status",
        # ⭐ h1（真机）：综合状态（`Status` + `CancelStatus` 一起判）。
        "display_status",
        "display_status_label",
        "order_status",
        "created_at",
        "last_synced_at",
        "last_error",
        "qr_sent_at",
        "qr_last_error",
        "auto_reply_last_error",
        # ⭐ A2（计划 Rev.9 §2）：远端接单人 / 接单时间 / 撤销原因 +
        # 「最近一次同步结论」——客服要能在列表里看到「谁接的、系统做了什么」。
        "remote_taker_uid",
        "remote_taker_name",
        "remote_accepted_at",
        "remote_cancel_reason",
        "remote_note",
        # ⭐ fx-D：来源单号 / 接单价 / 接单时间（页面三个格）。
        "source_order_no",
        "accept_price",
        "accepted_at",
        # ⭐ 真机定案：申请客服介入的可用性（前端按钮置灰 + 原因）。
        "arbitration_available",
        "arbitration_blocked_reason",
        # ⭐ h13：撤销协商发起方 + 「同意撤销」可用性（34 + 2 = 36）。
        "cancel_initiator",
        "agree_cancel_available",
        # ⭐ 真机读数：撤销要求原文（CancelCmt）/ 撤销原因（CancelReason）
        # ⇒ 36 + 2 = **38**（客服同意撤销前要看到的内容）。
        "cancel_cmt",
        "cancel_reason",
        # ⭐ 详情页「已结代练费 / 已赔保证金」⇒ 38 + 2 = **40**
        # （只在 Status 16/17 有值，其它状态为 null）。
        "settled_pay_level_bal",
        "settled_rep_ensure_bal",
        # ⭐ 协商撤销满 24 小时的自动申请客服介入（开关 / 计时起点 /
        # 已申请时刻 / 可读结论）。`auto_arbitrate_note` 是失败与停止重试的唯一持久出处。
        "auto_arbitrate",
        "cancel_requested_at",
        "auto_arbitrated_at",
        "auto_arbitrate_note",
        # ⭐ 业务方：还值不值得轮询（远端到终态后置 `False`）。
        # 聚合行父行「交易状态」按它选「最新**活跃**推送」。
        "is_active",
        # ⭐ 第三方页改列：A 组打手字段 + B 组本地计算字段。
        "accept_uid",
        "accept_user_name",
        "accept_over_count",
        "cust_cancel_count",
        "profit",
        "remaining_seconds",
        "order_source",
        "verify_deadline_at",
        "verify_remaining_seconds",
        "verify_overdue",
    }
)

#: ⭐ 业务方：`?group_by=order` 的**组**承诺的**恰好**六个键。
#: ⚠️ 组内 `pushes` 的行形状与扁平模式**同一个**序列化器
#: ⇒ 每行键集仍恰好等于 `PLATFORM_PUSH_ROW_KEYS`（本文件另有用例钉）。
PLATFORM_PUSH_GROUP_KEYS = frozenset(
    {
        "order_no",
        "source_order_no",
        "order_status",
        "game",
        "server",
        "pushes",
    }
)

NEW2 = datetime(2026, 10, 3, 8, 0, 0, tzinfo=dt_timezone.utc)
OLD = datetime(2026, 10, 1, 8, 0, 0, tzinfo=dt_timezone.utc)
NEW = datetime(2026, 10, 2, 8, 0, 0, tzinfo=dt_timezone.utc)


class _OrderFixture(TenantTestCase):
    """一个游戏 / 一个区服 / 一个持 `order.list` 的客服 + 两个小工具。"""

    NODES = (NODE,)

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.server = GameServer.objects.create(game=self.game, name="天空岛")
        self.client = make_logged_in_client(
            username="platform-push-cs", groups=("客服",), nodes=self.NODES
        )

    def _order(self, order_no, **kwargs):
        kwargs.setdefault("status", OrderStatus.COMPLETED)
        kwargs.setdefault("server", self.server)
        return Order.objects.create(
            game=self.game, order_no=order_no, **kwargs
        )

    def _push(self, order, **kwargs):
        payload = {
            "platform": DAILIANTONG,
            "state": DailiantongPush.State.SENT,
            "price": Decimal("5.00"),
            "ensure1": Decimal("0.00"),
            "ensure2": Decimal("0.00"),
            "time_limit_hours": 24,
            "ex_order_no": order.order_no,
            "attempts": 1,
        }
        payload.update(kwargs)
        return DailiantongPush.objects.create(order=order, **payload)

    def _items(self, client=None, query=""):
        body = (client or self.client).get(LIST_URL + query).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _row(self, order_no, client=None):
        rows = {row["order_no"]: row for row in self._items(client=client)}
        self.assertIn(order_no, rows, rows)
        return rows[order_no]


class OrderDetailDailiantongHistoryTests(_OrderFixture):
    NODES = (NODE, "order.view_detail")

    def _history(self, order):
        body = self.client.get(f"{LIST_URL}{order.order_no}/").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["has_dailiantong_history"]

    def test_previous_publish_remains_visible_after_withdrawal_and_failed_repost(self):
        order = self._order("20261027000000000111")
        published = self._push(order, serial_no="19000000000000000111")
        self.assertIs(self._history(order), True)

        published.state = DailiantongPush.State.CLOSED
        published.is_active = False
        published.save(update_fields=["state", "is_active"])
        self._push(order, state=DailiantongPush.State.FAILED, is_active=False)

        self.assertIs(self._history(order), True)
        self.assertNotIn("has_dailiantong_history", self._row(order.order_no))

    def test_unpublished_attempts_and_other_platform_do_not_count(self):
        order = self._order("20261027000000000112")
        self.assertIs(self._history(order), False)

        self._push(order, state=DailiantongPush.State.FAILED, is_active=False)
        self._push(order, state=DailiantongPush.State.CLOSED, is_active=False)
        self._push(order, state=DailiantongPush.State.UNKNOWN)
        self._push(order, platform=30, serial_no="19000000000000000112")
        self.assertIs(self._history(order), False)


class OrderListPlatformPushTests(_OrderFixture):
    """管理订单列表的 `platform_push` 键（徽标 + 平台单号的取数口）。"""

    NO = "20261027000000000101"
    OTHER = "20261027000000000102"

    def test_without_a_push_row_the_key_is_null(self):
        self._order(self.NO)
        row = self._row(self.NO)
        self.assertIn("platform_push", row)
        self.assertIsNone(row["platform_push"])

    def test_without_an_active_row_the_key_is_the_latest_push_row(self):
        """两条推送行**都不活跃**（旧 failed / 新 sent）⇒ 回退**最近一条**，形状恰好七个键。"""
        order = self._order(self.NO)
        failed = self._push(
            order,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
            remote_status=None,
            is_active=False,
            last_error="可操作资金不足，不能发布.",
        )
        sent = self._push(
            order,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000101",
            remote_status=11,
            remote_cancel_status=10,
            is_active=False,
        )
        # `auto_now_add` 只能事后改：把两条的先后钉死，⛔ 不靠创建顺序碰运气。
        DailiantongPush.objects.filter(pk=failed.pk).update(created_at=OLD)
        DailiantongPush.objects.filter(pk=sent.pk).update(created_at=NEW)

        payload = self._row(self.NO)["platform_push"]
        self.assertEqual(set(payload), PLATFORM_PUSH_KEYS)
        self.assertEqual(payload["platform"], DAILIANTONG)
        self.assertEqual(payload["serial_no"], "19000000000000000101")
        self.assertEqual(payload["state"], "sent")
        self.assertEqual(payload["remote_status"], 11)
        self.assertEqual(payload["remote_cancel_status"], 10)
        self.assertIs(payload["is_active"], False)
        # 发单价那一列要读它（本行 failed 是 `_push` 默认价 5.00，sent 也是 5.00）。
        self.assertEqual(payload["price"], "5.00")

    def test_a_newer_inactive_push_beats_an_older_active_one(self):
        """⭐ 2026-10-28 下午统一：取**最新一笔**（⛔ 不再活跃优先）。

        判别力：把选行改回「第一个 active」⇒ 本用例读到旧行 serial/price 而变红。
        父行「状态 / 发单价」与详情页 / 第三方页聚合都按这一条。
        """
        order = self._order(self.NO)
        active = self._push(
            order,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000111",
            remote_status=11,
            remote_cancel_status=10,
            price=Decimal("3.00"),
            is_active=True,
        )
        newer = self._push(
            order,
            state=DailiantongPush.State.CLOSED,
            serial_no="19000000000000000112",
            remote_status=17,
            remote_cancel_status=10,
            price=Decimal("9.00"),
            is_active=False,
        )
        DailiantongPush.objects.filter(pk=active.pk).update(created_at=OLD)
        DailiantongPush.objects.filter(pk=newer.pk).update(created_at=NEW)

        payload = self._row(self.NO)["platform_push"]
        self.assertEqual(set(payload), PLATFORM_PUSH_KEYS)
        self.assertEqual(payload["serial_no"], "19000000000000000112")
        self.assertEqual(payload["state"], "closed")
        self.assertEqual(payload["price"], "9.00")
        self.assertIs(payload["is_active"], False)

    def test_list_summary_and_detail_return_exactly_the_same_latest_row(self):
        """⭐ 2026-10-28：旧活跃 sent / 旧 closed / 新 failed 三条 ⇒ 列表摘要与详情页逐键一致。"""
        order = self._order(self.NO)
        old_active = self._push(
            order,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000131",
            remote_status=12,
            remote_cancel_status=10,
            price=Decimal("3.00"),
            is_active=True,
        )
        old_closed = self._push(
            order,
            state=DailiantongPush.State.CLOSED,
            serial_no="19000000000000000132",
            remote_status=17,
            remote_cancel_status=10,
            price=Decimal("4.00"),
            is_active=False,
        )
        newest_failed = self._push(
            order,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
            remote_status=None,
            remote_cancel_status=None,
            price=Decimal("7.00"),
            is_active=False,
        )
        DailiantongPush.objects.filter(pk=old_active.pk).update(created_at=OLD)
        DailiantongPush.objects.filter(pk=old_closed.pk).update(created_at=NEW)
        DailiantongPush.objects.filter(pk=newest_failed.pk).update(
            created_at=NEW2
        )

        summary = self._row(self.NO)["platform_push"]
        response = self.client.get(
            f"/api/business/orders/{self.NO}/platform-push/"
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        detail = response.data["data"][0]
        self.assertEqual(detail["id"], newest_failed.pk, "详情第一条必须是最新一笔")
        for key in (
            "platform",
            "serial_no",
            "state",
            "remote_status",
            "remote_cancel_status",
            "is_active",
        ):
            self.assertEqual(detail[key], summary[key], key)
        self.assertEqual(str(detail["price"]), summary["price"])

    def test_a_restricted_reader_gets_the_price_redacted_with_the_key_still_present(self):
        """`price` 与 `hall_price` 吃**同一道**脱敏：受限角色读到 `None`（键恒在）。

        判别力：把 `get_platform_push` 里的 `price_visible` 判断删掉 ⇒ 受限角色也能读到
        被隐藏的报价（本用例红）。
        """
        order = self._order(self.NO)
        self._push(order, price=Decimal("3.00"))
        restricted = make_logged_in_client(
            username="platform-push-restricted",
            groups=("客服",),
            nodes=(NODE, "order.restrict_view_offer_price"),
        )
        payload = self._row(self.NO, client=restricted)["platform_push"]
        self.assertIn("price", payload)
        self.assertIsNone(payload["price"])

    def test_a_failed_latest_push_is_reported_with_its_empty_serial(self):
        """最近一条是 `failed`（没有上游单号）⇒ `state=failed`、`serial_no=""`（不是 null）。"""
        order = self._order(self.NO)
        self._push(
            order,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
            last_error="可操作资金不足，不能发布.",
        )
        payload = self._row(self.NO)["platform_push"]
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["serial_no"], "")
        self.assertIsNone(payload["remote_status"])

    def test_two_orders_do_not_share_their_push_rows(self):
        """一张单的推送行不许串到另一张单的徽标上（预取的 to_attr 必须按单隔离）。"""
        first = self._order(self.NO)
        second = self._order(self.OTHER)
        self._push(first, serial_no="19000000000000000201", remote_status=11)
        self._push(
            second,
            state=DailiantongPush.State.UNKNOWN,
            serial_no=None,
            last_error="请求超时，是否成功未知",
        )

        rows = {row["order_no"]: row for row in self._items()}
        self.assertEqual(rows[self.NO]["platform_push"]["serial_no"], "19000000000000000201")
        self.assertEqual(rows[self.NO]["platform_push"]["state"], "sent")
        self.assertEqual(
            rows[self.OTHER]["platform_push"]["state"], "unknown"
        )
        self.assertEqual(rows[self.OTHER]["platform_push"]["serial_no"], "")

    def test_a_non_dailiantong_push_row_is_not_the_badge(self):
        """口径是**代练通（platform=10）**：别的平台行不算（本键只服务代练通徽标）。"""
        order = self._order(self.NO)
        self._push(
            order,
            platform=30,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
        )
        self.assertIsNone(self._row(self.NO)["platform_push"])

    def test_query_count_does_not_grow_with_row_count(self):
        """⛔ 不许 N+1：订单从 1 涨到 5（每单都有推送行），一次列表请求的 SQL 条数不变。

        判别力：把 `platform_push` 改成在 `SerializerMethodField` 里逐行
        `DailiantongPush.objects.filter(order=…)` ⇒ 5 行那次会多出 4 条 SQL ⇒ 本用例红。
        """
        first = self._order(self.NO)
        self._push(first, serial_no="19000000000000000301")

        def count():
            with CaptureQueriesContext(connection) as captured:
                items = self._items()
            return len(captured), items

        small_count, small_items = count()
        self.assertEqual(len(small_items), 1, small_items)  # 非空前置
        self.assertIsNotNone(small_items[0]["platform_push"])  # 预取真的生效了

        for index in range(1, 5):
            order = self._order(f"2026102700000000030{index}")
            self._push(order, serial_no=f"1900000000000000030{index + 1}")

        big_count, big_items = count()
        self.assertEqual(len(big_items), 5, big_items)  # 非空哨兵：第二次真的序列化了 5 行
        self.assertTrue(all(row["platform_push"] for row in big_items), big_items)
        self.assertEqual(small_count, big_count)


class PlatformPushListApiTests(_OrderFixture):
    """`GET /api/business/platform-pushes/`：形状 / 筛选 / 分页 / 权限。"""

    NO_A = "20261027000000000201"
    NO_B = "20261027000000000202"

    def _two_orders(self):
        first = self._order(self.NO_A)
        second = self._order(self.NO_B)
        push_a = self._push(
            first,
            serial_no="19000000000000000201",
            remote_status=11,
            remote_cancel_status=10,
            last_error="",
        )
        push_b = self._push(
            second,
            serial_no="19000000000000000202",
            remote_status=17,
            remote_cancel_status=16,
            state=DailiantongPush.State.UNKNOWN,
            last_error="上游超时",
        )
        return push_a, push_b

    def _body(self, query="", client=None):
        response = (client or self.client).get(PUSHES_URL + query)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_the_row_shape_is_exactly_the_columns_with_real_sources(self):
        push_a, _ = self._two_orders()
        data = self._body()
        self.assertEqual(data["total"], 2, data)
        rows = {row["order_no"]: row for row in data["items"]}
        row = rows[self.NO_A]
        self.assertEqual(set(row), PLATFORM_PUSH_ROW_KEYS)
        self.assertEqual(row["id"], push_a.pk)
        self.assertEqual(row["platform"], DAILIANTONG)
        self.assertEqual(row["serial_no"], "19000000000000000201")
        self.assertEqual(row["game"], "原神")
        self.assertEqual(row["server"], "天空岛")
        self.assertEqual(Decimal(row["price"]), Decimal("5.00"))
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["remote_status"], 11)
        self.assertEqual(row["remote_cancel_status"], 10)
        self.assertEqual(row["order_status"], OrderStatus.COMPLETED)
        self.assertIsNone(row["last_synced_at"])
        self.assertEqual(row["last_error"], "")
        # ⭐ T12：群二维码的两个状态列读面默认值。
        self.assertIsNone(row["qr_sent_at"])
        self.assertEqual(row["qr_last_error"], "")
        # ⭐ T14：自动回复的失败原因列读面默认值。
        self.assertEqual(row["auto_reply_last_error"], "")
        # ⭐ 自动仲裁的读面默认值（开关**默认关**，业务方改准；
        # 还没撤销 ⇒ 其余三格全空）。
        self.assertIs(row["auto_arbitrate"], False)
        self.assertIsNone(row["cancel_requested_at"])
        self.assertIsNone(row["auto_arbitrated_at"])
        self.assertEqual(row["auto_arbitrate_note"], "")

    def test_cancel_requirement_text_and_reason_are_exposed_verbatim(self):
        """⭐ 2026-10-28 真机：同意撤销前要看到上游拼好的 `CancelCmt` + `CancelReason`。"""
        requirement = (
            '接单者"新手USR-SYNTH-806325"申请撤销,需发单者支出:0.00, '
            "接单者支出:0.00. 请尽快协商解决.撤销原因：好的"
        )
        serial = "19000000000000000231"
        self._push(
            self._order(self.NO_A),
            serial_no=serial,
            remote_status=16,
            remote_cancel_status=11,
            remote_cancel_cmt=requirement,
            remote_cancel_reason="好的",
        )
        data = self._body()
        row = {item["serial_no"]: item for item in data["items"]}[serial]
        self.assertEqual(row["cancel_cmt"], requirement, "原样下发，⛔ 不改写")
        self.assertEqual(row["cancel_reason"], "好的")

    def test_settled_amounts_follow_the_remote_status(self):
        """⭐ 2026-10-28：已结代练费 / 已赔保证金只在 Status 16/17 有值。"""
        self._push(
            self._order(self.NO_A),
            serial_no="19000000000000000232",
            remote_status=17,
            remote_cancel_status=10,
            remote_pay_level_bal=Decimal("1.50"),
            remote_rep_ensure_bal=Decimal("0.00"),
        )
        self._push(
            self._order(self.NO_B),
            serial_no="19000000000000000233",
            remote_status=12,
            remote_cancel_status=10,
            remote_pay_level_bal=Decimal("1.50"),
            remote_rep_ensure_bal=Decimal("0.00"),
        )

        data = self._body()
        rows = {row["serial_no"]: row for row in data["items"]}
        settled = rows["19000000000000000232"]
        self.assertEqual(settled["settled_pay_level_bal"], "1.50")
        self.assertEqual(settled["settled_rep_ensure_bal"], "0.00")
        in_progress = rows["19000000000000000233"]
        self.assertIsNone(in_progress["settled_pay_level_bal"])
        self.assertIsNone(in_progress["settled_rep_ensure_bal"])

    def test_auto_arbitrate_columns_are_read_from_the_push_row(self):
        """⭐ 2026-10-28 读面：自动仲裁的开关 / 计时起点 / 结论都是**行上的值**。"""
        push = self._push(
            self._order(self.NO_A),
            serial_no="19000000000000000223",
            remote_status=12,
            remote_cancel_status=11,
        )
        requested_at = datetime(2026, 10, 27, 3, 0, 0, tzinfo=dt_timezone.utc)
        arbitrated_at = datetime(2026, 10, 28, 3, 0, 0, tzinfo=dt_timezone.utc)
        push.auto_arbitrate = True  # 只有客服勾选过的行才会自动仲裁；读面要如实回出。
        push.cancel_requested_at = requested_at
        push.auto_arbitrated_at = arbitrated_at
        push.auto_arbitrate_note = "已自动申请客服介入（回读撤销状态 客服介入中 / 订单状态 正在代练）"
        push.save(
            update_fields=[
                "auto_arbitrate",
                "cancel_requested_at",
                "auto_arbitrated_at",
                "auto_arbitrate_note",
                "updated_at",
            ]
        )

        row = self._body()["items"][0]

        self.assertIsNotNone(row["cancel_requested_at"])
        self.assertIsNotNone(row["auto_arbitrated_at"])
        self.assertIn("已自动申请客服介入", row["auto_arbitrate_note"])
        self.assertIs(row["auto_arbitrate"], True)

    def test_the_row_display_status_follows_the_cancel_status(self):
        """⭐ 真机 bug：列表行的「交易状态」必须**两个码一起判**。

        现场：`Status=16`（撤销中）+ `CancelStatus=12`（协商已处理 = 打手已同意撤销），
        本地已是已退单，页面却一直显示「撤销中」。⇒ 读面给 `display_status` /
        `display_status_label`，12/15/16 落「已撤销 / 已强制撤销」，11/14 仍是撤销在途，
        13（撤销已取消）回到按 `Status` 显示；⭐ 补 `Status=18` = 已下架（上游码表没列这一档）。
        """
        order = self._order(self.NO_A)
        cases = [
            # (remote_status, remote_cancel_status, display_status, label)
            (16, 12, "revoked", "已撤销"),
            (13, 15, "revoked", "已撤销"),
            (16, 16, "force_revoked", "已强制撤销"),
            (16, 11, "cancelling", "撤销中"),
            (16, 14, "arbitrating", "客服介入中"),
            # 13 撤销已取消 ⇒ 按 Status（这里 Status=16 撤销中、12 正在代练）
            (16, 13, "cancelling", "撤销中"),
            (12, 13, "in_progress", "正在代练"),
            # 没有撤销流程 ⇒ 按 Status
            (17, 10, "settled", "已结算"),
            # ⭐ 真机读数：下架后上游回 Status=18（码表没列这一档）⇒ 读面说「已下架」。
            (18, 10, "off_shelf", "已下架"),
            (None, None, None, ""),
        ]
        expected: dict[int, tuple[str | None, str]] = {}
        for index, (remote, cancel, token, label) in enumerate(cases):
            # 一条推送一行、一张单一行：同单同平台的**有效推送**只能有一条（部分唯一索引）。
            push = self._push(
                order if index == 0 else self._order(f"2026102700000000050{index}"),
                serial_no=f"1900000000000000040{index}",
                remote_status=remote,
                remote_cancel_status=cancel,
            )
            expected[push.pk] = (token, label)

        for row in self._body()["items"]:
            with self.subTest(push=row["id"]):
                token, label = expected[row["id"]]
                self.assertEqual(row["display_status"], token)
                self.assertEqual(row["display_status_label"], label)

    def test_qr_columns_are_read_from_the_push_row(self):
        """T12 读面：`qr_sent_at` / `qr_last_error` 是**行上的值**，不是再造的假值。"""
        push = self._push(
            self._order(self.NO_A),
            serial_no="19000000000000000221",
            remote_status=12,
            remote_cancel_status=10,
        )
        sent_at = datetime(2026, 10, 27, 2, 30, 0, tzinfo=dt_timezone.utc)
        push.qr_sent_at = sent_at
        push.qr_last_error = "上传到代练通 OSS 失败（HTTP 403，Code=AccessDenied）"
        push.save(update_fields=["qr_sent_at", "qr_last_error", "updated_at"])

        row = self._body()["items"][0]

        # DRF 按 settings 的 DATETIME_FORMAT 序列化；这里只钉「是同一个瞬时」。
        self.assertIsNotNone(row["qr_sent_at"])
        self.assertEqual(
            datetime.fromisoformat(row["qr_sent_at"].replace("Z", "+00:00")),
            sent_at,
        )
        self.assertEqual(
            row["qr_last_error"],
            "上传到代练通 OSS 失败（HTTP 403，Code=AccessDenied）",
        )

    def test_auto_reply_last_error_is_read_from_the_push_row(self):
        """T14 读面：自动回复的失败原因是**行上的值**（页面 / 排查能直接看到）。"""
        push = self._push(
            self._order(self.NO_A),
            serial_no="19000000000000000222",
            remote_status=12,
            remote_cancel_status=10,
        )
        push.auto_reply_last_error = "接单后自动回复失败：订单状态异常"
        push.save(update_fields=["auto_reply_last_error", "updated_at"])

        row = self._body()["items"][0]

        self.assertEqual(
            row["auto_reply_last_error"],
            "接单后自动回复失败：订单状态异常",
        )

    def test_an_order_without_server_gets_a_null_server(self):
        order = self._order(self.NO_A, server=None)
        self._push(order, serial_no="19000000000000000211")
        row = self._body()["items"][0]
        self.assertIsNone(row["server"])

    def test_bad_columns_are_not_faked(self):
        """没有数据源的列**不给键**（⛔ 不拿 0 / null 冒充）。"""
        self._two_orders()
        row = self._body()["items"][0]
        for forbidden in ("cancel_at",):
            self.assertNotIn(forbidden, row, f"页面没有数据源的列不该出现在载荷里：{forbidden}")

    def test_default_platform_is_dailiantong(self):
        other = self._order(self.NO_A)
        self._push(
            other,
            platform=30,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
        )
        self._push(self._order(self.NO_B), serial_no="19000000000000000221")
        data = self._body()
        self.assertEqual(data["total"], 1, data)
        self.assertEqual({row["platform"] for row in data["items"]}, {DAILIANTONG})

    def test_platform_filter_can_ask_for_another_platform(self):
        other = self._order(self.NO_A)
        self._push(
            other,
            platform=30,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
        )
        self._push(self._order(self.NO_B), serial_no="19000000000000000231")
        data = self._body(f"?platform=30")
        self.assertEqual(data["total"], 1, data)
        self.assertEqual(data["items"][0]["platform"], 30)

    def test_remote_status_filter_and_blank_means_no_filter(self):
        self._two_orders()
        filtered = self._body("?status=17")
        self.assertEqual(filtered["total"], 1, filtered)
        self.assertEqual(filtered["items"][0]["order_no"], self.NO_B)
        # 前端「没选」时送空串：空串 = 不筛（不是「筛 remote_status = ''」）。
        unfiltered = self._body("?status=")
        self.assertEqual(unfiltered["total"], 2, unfiltered)

    def test_keyword_matches_our_order_no(self):
        self._two_orders()
        data = self._body(f"?keyword={self.NO_A}")
        self.assertEqual(data["total"], 1, data)
        self.assertEqual(data["items"][0]["order_no"], self.NO_A)

    def test_keyword_matches_the_platform_serial(self):
        self._two_orders()
        data = self._body("?keyword=19000000000000000202")
        self.assertEqual(data["total"], 1, data)
        self.assertEqual(data["items"][0]["order_no"], self.NO_B)

    def test_pagination_slices_and_reports_the_page(self):
        for index in range(5):
            order = self._order(f"2026102700000000040{index}")
            self._push(order, serial_no=f"1900000000000000040{index}")
        data = self._body("?page=2&page_size=2")
        self.assertEqual(data["total"], 5, data)
        self.assertEqual(data["page"], 2)
        self.assertEqual(data["page_size"], 2)
        self.assertEqual(len(data["items"]), 2, data)

    def test_page_size_defaults_to_30_and_is_capped_at_100(self):
        self._push(self._order(self.NO_A), serial_no="19000000000000000241")
        self.assertEqual(self._body()["page_size"], 30)
        # 上限 100：传 500 不报错、按 100 回（⛔ 不把它做成 1000）。
        capped = self._body("?page_size=500")
        self.assertEqual(capped["page_size"], 100, capped)
        self.assertEqual(capped["total"], 1, capped)

    def test_malformed_query_values_are_a_business_error_not_a_500(self):
        for query in ("?status=abc", "?page=abc", "?platform=abc", "?page=0"):
            with self.subTest(query=query):
                response = self.client.get(PUSHES_URL + query)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.content[:200])

    def test_without_the_order_list_node_it_is_403(self):
        # ⚠️ `groups=()`：`groups=("客服",)` 会让夹具给同名 Role 补节点（本类 setUp 刚把
        # `order.list` 挂上去）⇒ 403 会静默变成 200。
        denied = make_logged_in_client(
            username="platform-push-no-node", groups=()
        )
        response = denied.get(PUSHES_URL)
        self.assertEqual(response.status_code, 403, response.content[:200])


class PlatformPushGroupByOrderApiTests(_OrderFixture):
    """⭐ 业务方口径：`?group_by=order` 按订单聚合、分页按订单。

    判据（与视图 docstring 的「按订单聚合」一节逐条对应）：

    - 一单两条推送 ⇒ **1 个 item**、`pushes` 长 2、`total=1`；
    - 分页按**订单**（3 单 5 推送、`page_size=2` ⇒ 第 1 页 2 单、第 2 页 1 单）；
    - `status` 命中组内任一条 ⇒ **整组**返回，且组内仍给**全部**推送行；
    - `keyword` 命中订单号 / 平台单号 / **来源单号** ⇒ 整组（来源单号这一条只在分组模式有）；
    - 组的排序 = 该单最新一条推送时间倒序；组内推送行时间倒序；
    - 空结果 / 越界页不报错（`items` 空、`total` 照旧）；
    - 已软删的订单**不出现**（与扁平模式同一个底集 ⇒ 边界不放宽）。
    """

    NO_A = "20261028000000000701"
    NO_B = "20261028000000000702"
    NO_C = "20261028000000000703"

    #: 时间轴（钉死「谁更新」，⛔ 不靠创建顺序碰运气）。
    T1 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=dt_timezone.utc)
    T2 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=dt_timezone.utc)
    T3 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=dt_timezone.utc)
    T4 = datetime(2026, 10, 1, 11, 0, 0, tzinfo=dt_timezone.utc)
    T5 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt_timezone.utc)

    def _grouped(self, query="", *, client=None):
        url = PUSHES_URL + "?group_by=order" + (("&" + query) if query else "")
        body = (client or self.client).get(url).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _flat(self, query="", *, client=None):
        url = PUSHES_URL + ("?" + query if query else "")
        body = (client or self.client).get(url).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _when(self, push, moment):
        """把 `created_at` 钉到指定时刻（`auto_now_add` 只能事后改）。"""
        DailiantongPush.objects.filter(pk=push.pk).update(created_at=moment)
        return push

    def test_one_order_with_two_pushes_is_one_item_with_both_pushes(self):
        order = self._order(self.NO_A, source_order_no="SRC-G5-0001")
        old = self._when(
            self._push(order, serial_no="19000000000000000701", remote_status=11),
            self.T1,
        )
        new = self._when(
            self._push(
                order,
                state=DailiantongPush.State.FAILED,
                serial_no="19000000000000000702",
                remote_status=None,
            ),
            self.T2,
        )
        # ⭐ 业务方：远端终态行 `is_active=False` —— 聚合父行「交易状态」
        # 要按「最新**活跃**推送」选行，所以这一列必须真的下发。
        DailiantongPush.objects.filter(pk=new.pk).update(is_active=False)

        data = self._grouped()

        self.assertEqual(data["total"], 1, data)
        self.assertEqual(len(data["items"]), 1, data)
        item = data["items"][0]
        # 组的**恰好**六个键（与前端契约一致）。
        self.assertEqual(set(item), PLATFORM_PUSH_GROUP_KEYS)
        self.assertEqual(item["order_no"], self.NO_A)
        self.assertEqual(item["source_order_no"], "SRC-G5-0001")
        self.assertEqual(item["order_status"], OrderStatus.COMPLETED)
        self.assertEqual(item["game"], "原神")
        self.assertEqual(item["server"], "天空岛")
        self.assertEqual(len(item["pushes"]), 2, item["pushes"])
        # 组内按时间倒序（新失败行在前、旧 11 行在后）。
        self.assertEqual(
            [row["serial_no"] for row in item["pushes"]],
            ["19000000000000000702", "19000000000000000701"],
        )
        # 组内行与扁平模式**同一个序列化器** ⇒ 键集仍是那 34 个。
        self.assertEqual(set(item["pushes"][0]), PLATFORM_PUSH_ROW_KEYS)
        self.assertEqual(
            {row["id"] for row in item["pushes"]}, {old.pk, new.pk}
        )
        # ⭐ `is_active` 逐行下发（⛔ 不是常量）。
        self.assertEqual(
            {row["serial_no"]: row["is_active"] for row in item["pushes"]},
            {"19000000000000000702": False, "19000000000000000701": True},
        )

    def test_pagination_slices_orders_not_rows(self):
        """3 单 5 推送、`page_size=2` ⇒ 第 1 页 2 单、第 2 页 1 单，`total=3`。"""
        a = self._order(self.NO_A)
        b = self._order(self.NO_B)
        c = self._order(self.NO_C)
        # A 的最新推送最晚（T5），B 次之（T4），C 最早（T3）⇒ 分组顺序 [A, B, C]。
        self._when(self._push(a, serial_no="19000000000000001701"), self.T1)
        self._when(
            self._push(a, state=DailiantongPush.State.FAILED, serial_no="19000000000000001702"),
            self.T5,
        )
        self._when(self._push(b, serial_no="19000000000000001703"), self.T4)
        self._when(self._push(c, serial_no="19000000000000001704"), self.T2)
        self._when(
            self._push(c, state=DailiantongPush.State.FAILED, serial_no="19000000000000001705"),
            self.T3,
        )

        first = self._grouped("page=1&page_size=2")
        second = self._grouped("page=2&page_size=2")

        self.assertEqual(first["total"], 3, first)
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["page_size"], 2)
        self.assertEqual(
            [item["order_no"] for item in first["items"]],
            [self.NO_A, self.NO_B],
            first,
        )
        # 推送行数是 5，但分页单位是**订单** ⇒ 第 2 页仍然有 1 个 item。
        self.assertEqual(second["total"], 3, second)
        self.assertEqual(second["page"], 2)
        self.assertEqual(
            [item["order_no"] for item in second["items"]], [self.NO_C], second
        )
        self.assertEqual(len(second["items"][0]["pushes"]), 2, second)

    def test_status_filter_returns_the_whole_group(self):
        """`status` 命中组内任一条 ⇒ 整组返回，组内仍给**全部**推送行。"""
        a = self._order(self.NO_A)
        b = self._order(self.NO_B)
        self._when(
            self._push(
                a,
                serial_no="19000000000000002701",
                remote_status=17,
                state=DailiantongPush.State.CLOSED,
            ),
            self.T2,
        )
        self._when(self._push(a, serial_no="19000000000000002702", remote_status=11), self.T1)
        self._when(self._push(b, serial_no="19000000000000002703", remote_status=17), self.T3)

        grouped = self._grouped("status=11")
        flat = self._flat("status=11")

        self.assertEqual(grouped["total"], 1, grouped)
        self.assertEqual(grouped["items"][0]["order_no"], self.NO_A)
        self.assertEqual(len(grouped["items"][0]["pushes"]), 2, grouped)
        self.assertEqual(
            {row["remote_status"] for row in grouped["items"][0]["pushes"]},
            {17, 11},
            "组内必须是该单**全部**行（⛔ 不按 status 收窄）",
        )
        # 对照：扁平模式仍然只回命中的那一行（既有行为，⛔ 不改）。
        self.assertEqual(flat["total"], 1, flat)
        self.assertEqual(flat["items"][0]["remote_status"], 11)

    def test_keyword_matches_source_order_no_in_grouped_mode_only(self):
        """分组模式的关键字多认**来源单号**；扁平模式保持原口径（只认订单号 + 平台单号）。"""
        source_order = self._order(self.NO_A, source_order_no="SRC-G5-7777")
        self._push(source_order, serial_no="19000000000000003701", remote_status=11)
        self._push(self._order(self.NO_B), serial_no="19000000000000003702")

        grouped = self._grouped("keyword=SRC-G5-7777")
        flat = self._flat("keyword=SRC-G5-7777")

        self.assertEqual(grouped["total"], 1, grouped)
        self.assertEqual(grouped["items"][0]["order_no"], self.NO_A)
        self.assertEqual(flat["total"], 0, flat)

    def test_keyword_matches_order_no_and_platform_serial_in_grouped_mode(self):
        a = self._order(self.NO_A)
        self._push(a, serial_no="19000000000000004701")
        self._push(self._order(self.NO_B), serial_no="19000000000000004702")

        by_order_no = self._grouped(f"keyword={self.NO_A}")
        by_serial = self._grouped("keyword=19000000000000004702")

        self.assertEqual(by_order_no["total"], 1, by_order_no)
        self.assertEqual(by_order_no["items"][0]["order_no"], self.NO_A)
        self.assertEqual(by_serial["total"], 1, by_serial)
        self.assertEqual(by_serial["items"][0]["order_no"], self.NO_B)

    def test_groups_are_ordered_by_their_latest_push(self):
        """组序 = 该单最新一条推送的 `created_at` 倒序（⛔ 不是该单第一条 / 订单创建时间）。"""
        a = self._order(self.NO_A)
        b = self._order(self.NO_B)
        self._when(self._push(a, serial_no="19000000000000005701"), self.T1)
        self._when(
            self._push(a, state=DailiantongPush.State.FAILED, serial_no="19000000000000005702"),
            self.T3,
        )
        self._when(self._push(b, serial_no="19000000000000005703"), self.T2)

        data = self._grouped()

        self.assertEqual(
            [item["order_no"] for item in data["items"]],
            [self.NO_A, self.NO_B],
            "A 的最新推送（T3）晚于 B 的（T2）⇒ A 在前",
        )

    def test_empty_result_and_out_of_range_page(self):
        empty = self._grouped()
        self.assertEqual(empty["total"], 0, empty)
        self.assertEqual(empty["items"], [], empty)

        self._push(self._order(self.NO_A), serial_no="19000000000000006701")
        beyond = self._grouped("page=99&page_size=10")
        self.assertEqual(beyond["total"], 1, beyond)
        self.assertEqual(beyond["page"], 99)
        self.assertEqual(beyond["items"], [], beyond)

    def test_query_count_does_not_grow_with_order_count(self):
        """⛔ 不许 N+1：订单从 1 涨到 4（每单都有 1–2 条推送），一次聚合请求的 SQL 条数不变。

        判别力：把「取本页推送行」改成逐组查（`DailiantongPush.objects.filter(order_id=…)`）
        ⇒ 4 单那次会多出 3 条 SQL ⇒ 本用例红。
        """
        first = self._order(self.NO_A)
        self._push(first, serial_no="19000000000000010711")

        def count():
            with CaptureQueriesContext(connection) as captured:
                data = self._grouped()
            return len(captured), data

        small_count, small = count()
        self.assertEqual(small["total"], 1, small)  # 非空前置
        self.assertEqual(len(small["items"]), 1, small)

        for index, no in enumerate((self.NO_B, self.NO_C, "20261028000000000704")):
            order = self._order(no)
            self._push(order, serial_no=f"1900000000000001072{index}")
            self._push(
                order,
                state=DailiantongPush.State.FAILED,
                serial_no=f"1900000000000001073{index}",
            )

        big_count, big = count()
        self.assertEqual(big["total"], 4, big)  # 非空哨兵：第二次真的聚合了 4 单
        self.assertEqual(len(big["items"]), 4, big)
        self.assertEqual(small_count, big_count)

    def test_soft_deleted_order_is_not_grouped_in(self):
        """D-0144：常规读面上已软删的单**不存在**（分组口径不放松）。"""
        order = self._order(self.NO_A)
        self._push(order, serial_no="19000000000000007701")
        self.assertEqual(self._grouped()["total"], 1)  # 非空前置

        Order.objects.filter(pk=order.pk).update(
            deleted_at=datetime.now(dt_timezone.utc)
        )

        grouped = self._grouped()
        self.assertEqual(grouped["total"], 0, grouped)
        self.assertEqual(grouped["items"], [], grouped)

    def test_platform_filter_still_scopes_the_group_pushes(self):
        """组内 `pushes` = 该单在**当前平台口径**下的全部行（换平台只看那个平台的行）。"""
        order = self._order(self.NO_A)
        self._push(order, platform=DAILIANTONG, serial_no="19000000000000008701")
        self._push(
            order,
            platform=30,
            state=DailiantongPush.State.FAILED,
            serial_no=None,
        )

        dailiantong = self._grouped()
        other = self._grouped("platform=30")

        self.assertEqual(dailiantong["total"], 1, dailiantong)
        self.assertEqual(len(dailiantong["items"][0]["pushes"]), 1, dailiantong)
        self.assertEqual(
            {row["platform"] for row in dailiantong["items"][0]["pushes"]},
            {DAILIANTONG},
        )
        self.assertEqual(other["total"], 1, other)
        self.assertEqual(len(other["items"][0]["pushes"]), 1, other)
        self.assertEqual(
            {row["platform"] for row in other["items"][0]["pushes"]}, {30}
        )

    def test_malformed_group_by_is_a_business_error_and_blank_means_flat(self):
        self._push(self._order(self.NO_A), serial_no="19000000000000009701")
        response = self.client.get(PUSHES_URL + "?group_by=foo")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000, response.content[:200])
        self.assertIn("group_by", response.json()["msg"])
        # 空串 = 前端「没选」⇒ 扁平口径（⛔ 不报错）。
        blank = self._flat("group_by=")
        self.assertEqual(blank["total"], 1, blank)
        self.assertTrue(blank["items"][0]["serial_no"])

    def test_row_level_arbitration_availability_is_in_the_grouped_pushes(self):
        """组内每一行也带仲裁可用性两键（与扁平模式同一个序列化器）。"""
        order = self._order(self.NO_A)
        self._push(order, serial_no="19000000000000010701", remote_status=11)
        row = self._grouped()["items"][0]["pushes"][0]
        self.assertIn("arbitration_available", row)
        self.assertIn("arbitration_blocked_reason", row)


class PlatformPushArbitrationAvailabilityTests(_OrderFixture):
    """⭐ 真机定案：列表行带「能不能申请客服介入」两键。

    口径 = `dailiantong_push.arbitration_availability`（**本地快照**：`remote_cancel_status`
    + `cancel_requested_at`）。⚠️ 它只作按钮提示；放不放行以动作端点的**当次详情回读**为准
    （动作侧用例在 `tests/test_dailiantong_actions.py::ArbitrationActionTests`）。
    """

    NO = "20261028000000001101"

    def _row(self):
        body = self.client.get(PUSHES_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.NO, rows, rows)
        return rows[self.NO]

    def test_a_non_negotiating_status_is_blocked_with_the_upstream_wording(self):
        order = self._order(self.NO)
        self._push(
            order,
            serial_no="19000000000000011101",
            remote_status=12,
            remote_cancel_status=10,
        )
        row = self._row()
        self.assertIs(row["arbitration_available"], False)
        self.assertIn("不能申请客服介入", row["arbitration_blocked_reason"])
        self.assertIn(
            "订单异常，无法申请仲裁介入", row["arbitration_blocked_reason"]
        )

    def test_negotiating_under_one_hour_reports_the_remaining_minutes(self):
        order = self._order(self.NO)
        push = self._push(
            order,
            serial_no="19000000000000011102",
            remote_status=16,
            remote_cancel_status=11,
        )
        requested_at = datetime.now(dt_timezone.utc) - timedelta(minutes=30)
        DailiantongPush.objects.filter(pk=push.pk).update(
            cancel_requested_at=requested_at
        )

        row = self._row()
        self.assertIs(row["arbitration_available"], False)
        self.assertIn("还需", row["arbitration_blocked_reason"])
        self.assertIn("分钟", row["arbitration_blocked_reason"])

    def test_negotiating_over_one_hour_is_available(self):
        order = self._order(self.NO)
        push = self._push(
            order,
            serial_no="19000000000000011103",
            remote_status=16,
            remote_cancel_status=11,
        )
        DailiantongPush.objects.filter(pk=push.pk).update(
            cancel_requested_at=datetime.now(dt_timezone.utc) - timedelta(hours=2)
        )

        row = self._row()
        self.assertIs(row["arbitration_available"], True)
        self.assertEqual(row["arbitration_blocked_reason"], "")

    def test_negotiating_without_a_local_timestamp_is_left_to_upstream(self):
        """打手侧发起（本地没记到时刻）⇒ 可用（放行给上游判，⛔ 不本地永久禁用）。"""
        order = self._order(self.NO)
        self._push(
            order,
            serial_no="19000000000000011104",
            remote_status=16,
            remote_cancel_status=11,
        )
        row = self._row()
        self.assertIs(row["arbitration_available"], True)
        self.assertEqual(row["arbitration_blocked_reason"], "")

    def test_an_unsynced_row_says_to_sync_first(self):
        """还没回读过撤销状态（`remote_cancel_status is None`）⇒ 先同步，⛔ 不猜一个状态。"""
        order = self._order(self.NO)
        self._push(order, serial_no="19000000000000011105")
        row = self._row()
        self.assertIs(row["arbitration_available"], False)
        self.assertIn("同步", row["arbitration_blocked_reason"])


class PlatformPushAcceptAndSourceFieldsTests(_OrderFixture):
    """⭐ 来源单号 / 接单价 / 接单时间三个格。

    - `source_order_no`：`Order.source_order_no`（可能空串 ⇒ 前端回退内置单号）；
    - `accept_price`：**本地订单的接单价** `Order.total_price`（对标 `GetPlatformOrderList`
      的 `price` 一格 =「该笔订单的接单(销售)金额」；`LevelOrderDetail` 里**没有**单独的
      接单价键——`Price` 是平台订单价 = 我方发单价，已由 `price` 下发）；
    - `accepted_at`：平台打手接单时间 = 上游详情 `StartDate`，库里的 `remote_accepted_at`。
    """

    NO = "20261028000000000401"
    DETAIL_URL = f"/api/business/orders/{NO}/platform-push/"

    def _push_row(self, order_no):
        body = self.client.get(PUSHES_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(order_no, rows, rows)
        return rows[order_no]

    def test_source_order_no_is_forwarded_and_may_be_blank(self):
        order = self._order(self.NO, source_order_no="SRC-2026-0001")
        self._push(order, serial_no="19000000000000000401")
        row = self._push_row(self.NO)
        self.assertEqual(row["source_order_no"], "SRC-2026-0001")

        Order.objects.filter(pk=order.pk).update(source_order_no="")
        self.assertEqual(self._push_row(self.NO)["source_order_no"], "")

    def test_accept_price_is_the_local_taker_price(self):
        order = self._order(
            self.NO, total_price=Decimal("88.00"), source_order_no="SRC-88"
        )
        self._push(order, serial_no="19000000000000000402", remote_status=12)

        row = self._push_row(self.NO)
        self.assertEqual(Decimal(row["accept_price"]), Decimal("88.00"))

    def test_accepted_at_is_the_remote_accept_time(self):
        accepted_at = datetime(2026, 10, 28, 1, 2, 3, tzinfo=dt_timezone.utc)
        order = self._order(self.NO, total_price=Decimal("88.00"))
        self._push(
            order,
            serial_no="19000000000000000403",
            remote_status=12,
            remote_accepted_at=accepted_at,
        )

        row = self._push_row(self.NO)
        self.assertIsNotNone(row["accepted_at"])
        self.assertEqual(
            datetime.fromisoformat(row["accepted_at"].replace("Z", "+00:00")),
            accepted_at,
        )
        # 旧键仍在（不删历史列）；新键与它是同一个瞬时。
        self.assertEqual(row["accepted_at"], row["remote_accepted_at"])

    def test_the_order_detail_push_row_also_carries_the_three_keys(self):
        accepted_at = datetime(2026, 10, 28, 2, 3, 4, tzinfo=dt_timezone.utc)
        order = self._order(
            self.NO, total_price=Decimal("66.00"), source_order_no="SRC-DETAIL"
        )
        self._push(
            order,
            serial_no="19000000000000000404",
            remote_status=12,
            remote_accepted_at=accepted_at,
        )
        body = self.client.get(self.DETAIL_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = body["data"]
        row = rows[0] if isinstance(rows, list) else rows
        self.assertEqual(row["source_order_no"], "SRC-DETAIL")
        self.assertEqual(Decimal(row["accept_price"]), Decimal("66.00"))
        self.assertEqual(
            datetime.fromisoformat(row["accepted_at"].replace("Z", "+00:00")),
            accepted_at,
        )


class PlatformPushTakerAndVerifyFieldsTests(_OrderFixture):
    """⭐ 2026-10-28（业务方）：打手资料四格 + 本地利润 / 来源 / 验收倒计时。"""

    NO = "20261028000000000421"

    def _row(self):
        body = self.client.get(PUSHES_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(self.NO, rows, rows)
        return rows[self.NO]

    def test_accepted_order_exposes_taker_fields_and_local_calculations(self):
        started = datetime.now(dt_timezone.utc) - timedelta(hours=49)
        accepted_at = datetime(2026, 10, 28, 1, 2, 3, tzinfo=dt_timezone.utc)
        order = self._order(
            self.NO, total_price=Decimal("88.00"), source="抖音"
        )
        self._push(
            order,
            serial_no="19000000000000000421",
            remote_status=13,
            remote_accepted_at=accepted_at,
            accept_uid="USR-SYNTH-TAKER-421",
            remote_taker_name="合成打手",
            accept_over_count=37,
            cust_cancel_count=2,
            remote_verify_started_at=started,
        )

        row = self._row()
        self.assertEqual(row["accept_uid"], "USR-SYNTH-TAKER-421")
        self.assertEqual(row["accept_user_name"], "合成打手")
        self.assertEqual(row["accept_over_count"], 37)
        self.assertEqual(row["cust_cancel_count"], 2)
        self.assertIsNotNone(row["remote_accepted_at"])
        self.assertEqual(Decimal(row["accept_price"]), Decimal("88.00"))
        self.assertEqual(row["profit"], "83.00", "88 - 5")
        self.assertEqual(row["order_source"], "抖音")

        deadline = datetime.fromisoformat(
            row["verify_deadline_at"].replace("Z", "+00:00")
        )
        self.assertEqual(deadline, started + timedelta(hours=48))
        self.assertLess(row["verify_remaining_seconds"], 0, "已过期 ⇒ 负数")
        self.assertIs(row["verify_overdue"], True)

    def test_verify_deadline_is_start_plus_48_hours(self):
        """⭐ 业务方 2026-10-28：代练通默认验收时限 = **48 小时**。"""
        started = datetime.now(dt_timezone.utc) - timedelta(hours=1)
        order = self._order(self.NO, total_price=Decimal("88.00"))
        self._push(
            order,
            serial_no="19000000000000000425",
            remote_status=13,
            remote_verify_started_at=started,
        )

        row = self._row()
        deadline = datetime.fromisoformat(
            row["verify_deadline_at"].replace("Z", "+00:00")
        )
        self.assertEqual(deadline, started + timedelta(hours=48))
        self.assertGreater(row["verify_remaining_seconds"], 46 * 3600)
        self.assertLessEqual(row["verify_remaining_seconds"], 48 * 3600)
        self.assertIs(row["verify_overdue"], False)

    def test_unaccepted_order_has_no_taker_or_verify_values(self):
        order = self._order(self.NO, total_price=Decimal("88.00"))
        self._push(order, serial_no="19000000000000000422", remote_status=11)

        row = self._row()
        self.assertEqual(row["accept_uid"], "")
        self.assertEqual(row["accept_user_name"], "")
        self.assertIsNone(row["accept_over_count"])
        self.assertIsNone(row["cust_cancel_count"])
        self.assertIsNone(row["remote_accepted_at"])
        self.assertIsNone(row["verify_deadline_at"])
        self.assertIsNone(row["verify_remaining_seconds"])
        self.assertIs(row["verify_overdue"], False)

    def test_profit_is_null_when_either_side_is_missing(self):
        # 生产表两列都 NOT NULL ⇒ 用 stub 直接打「缺一边」的分支（⛔ 不拿 0 顶）。
        from apps.business.api.platform_push_views import PlatformPushRowSerializer

        serializer = PlatformPushRowSerializer()
        self.assertIsNone(
            serializer.get_profit(
                SimpleNamespace(order=SimpleNamespace(total_price=None), price=Decimal("5.00"))
            )
        )
        self.assertIsNone(
            serializer.get_profit(
                SimpleNamespace(order=SimpleNamespace(total_price=Decimal("88.00")), price=None)
            )
        )
        self.assertEqual(
            serializer.get_profit(
                SimpleNamespace(
                    order=SimpleNamespace(total_price=Decimal("88.00")),
                    price=Decimal("5.00"),
                )
            ),
            "83.00",
        )

    def test_order_source_is_always_a_display_name_never_a_bare_id(self):
        source = OrderSource.objects.create(name="抖音", shop_type=10)
        order = self._order(self.NO, total_price=Decimal("10.00"), source="抖音")
        self._push(order, serial_no="19000000000000000426", remote_status=12)
        self.assertEqual(self._row()["order_source"], "抖音")

        # 只有 FK ⇒ 取字典 name。
        Order.objects.filter(pk=order.pk).update(source="", order_source=source)
        self.assertEqual(self._row()["order_source"], "抖音")

        # 历史/导入把快照写成 id 数字串 ⇒ ⛔ 不能把数字下发给前端，回退字典 name。
        Order.objects.filter(pk=order.pk).update(source=str(source.pk))
        self.assertEqual(self._row()["order_source"], "抖音")

        # 两边都取不到 ⇒ 空串（不是 id）。
        Order.objects.filter(pk=order.pk).update(source="", order_source=None)
        self.assertEqual(self._row()["order_source"], "")

    def test_remaining_seconds_comes_from_accept_time_plus_time_limit(self):
        accepted_at = datetime.now(dt_timezone.utc) - timedelta(hours=2)
        order = self._order(self.NO, total_price=Decimal("10.00"))
        self._push(
            order,
            serial_no="19000000000000000427",
            remote_status=12,
            remote_accepted_at=accepted_at,
            time_limit_hours=24,
        )
        remaining = self._row()["remaining_seconds"]
        self.assertGreater(remaining, 21 * 3600)
        self.assertLessEqual(remaining, 22 * 3600)

        # 还没接单 ⇒ 没有可靠起点 ⇒ null（前端画 —，⛔ 不拿创建时刻冒充）。
        DailiantongPush.objects.filter(order=order).update(remote_accepted_at=None)
        self.assertIsNone(self._row()["remaining_seconds"])

        # 已过期 ⇒ 0（不画负时间）。
        DailiantongPush.objects.filter(order=order).update(
            remote_accepted_at=datetime.now(dt_timezone.utc) - timedelta(hours=30)
        )
        self.assertEqual(self._row()["remaining_seconds"], 0)

    def test_accept_uid_falls_back_to_the_historical_remote_taker_uid(self):
        order = self._order(self.NO, total_price=Decimal("10.00"))
        self._push(
            order,
            serial_no="19000000000000000424",
            remote_status=12,
            accept_uid="",
            remote_taker_uid="USR-LEGACY-421",
        )
        self.assertEqual(self._row()["accept_uid"], "USR-LEGACY-421")


COUNTS_URL = "/api/business/platform-pushes/counts/"


class PlatformPushCountsApiTests(_OrderFixture):
    """⭐ 14 个页签的括号数字**一次**取回。

    口径与页签映射一致：`counts` 按上游 `remote_status` 分桶（11/12/13/14/15/16/17/18
    已下架，`"18"` 与其他码一样**恒在**；还没回读的落 `none`）；`states` 按本地推送状态
    分桶（含 `closed`，即「已在代练通删除」的历史行）；`total` = 范围内全部行。

    ⭐ 另有**订单口径**：`order_total` / `order_counts`（键形状与 `counts` 同）。
    两者的差异见 `test_order_counts_count_orders_not_rows`：一张单两条推送行 ⇒
    `total=2` 但 `order_total=1`；一张单先 11 再重发 12 ⇒ 两个订单桶**各计 1**。
    """

    def _three_rows(self):
        first = self._order("20261028000000000501")
        second = self._order("20261028000000000502")
        third = self._order("20261028000000000503")
        self._push(first, serial_no="19000000000000000501", remote_status=11)
        self._push(second, serial_no="19000000000000000502", remote_status=12)
        # 已删除的推送：远端码还停在 11，但本地态是 closed（页签计数要能分开看）。
        self._push(
            third,
            serial_no="19000000000000000503",
            remote_status=11,
            state=DailiantongPush.State.CLOSED,
        )

    def _body(self, query=""):
        response = self.client.get(COUNTS_URL + query)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_counts_are_one_batch_for_every_status_plus_the_local_states(self):
        self._three_rows()
        data = self._body("?platform=10")
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["counts"]["11"], 2)
        self.assertEqual(data["counts"]["12"], 1)
        self.assertEqual(data["counts"]["13"], 0)
        self.assertEqual(data["counts"]["17"], 0)
        # ⭐ H7：18 已下架是**恒在**桶（0 条也要回），这样页签才能显示「(0)」而不是「—」。
        self.assertEqual(data["counts"]["18"], 0)
        self.assertEqual(data["counts"]["none"], 0)
        self.assertEqual(data["states"]["sent"], 2)
        self.assertEqual(data["states"]["closed"], 1)
        self.assertEqual(data["states"]["pending"], 0)

    def test_keyword_narrows_the_counts_the_same_way_as_the_list(self):
        self._three_rows()
        data = self._body("?keyword=19000000000000000502")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["counts"]["12"], 1)
        self.assertEqual(data["counts"]["11"], 0)

    def test_off_shelf_status_18_has_its_own_bucket_and_filter(self):
        """⭐ H7（2026-10-28）：「已下架」页签的数据源 = `?status=18` + `counts["18"]`。

        页面前端按 `TAB_STATUS_MAP['75'] = 18` 发筛选、按 `counts["18"]` 填括号数字 ⇒
        这两条后端契约都得真的通（不只是「码表里有个 18」）。
        """
        order = self._order("20261028000000000511")
        self._push(order, serial_no="19000000000000000511", remote_status=18)

        data = self._body("?platform=10")
        self.assertEqual(data["counts"]["18"], 1, data)
        self.assertEqual(data["order_counts"]["18"], 1, data)

        listed = self.client.get(PUSHES_URL + "?status=18").json()
        self.assertEqual(listed["code"], 0, listed)
        self.assertEqual(listed["data"]["total"], 1, listed)
        self.assertEqual(listed["data"]["items"][0]["remote_status"], 18, listed)
        self.assertEqual(
            listed["data"]["items"][0]["display_status_label"], "已下架", listed
        )

    def test_order_counts_count_orders_not_rows(self):
        """⭐ 同一份行集算出两套数字，行口径与订单口径**并列**回。

        夹具：订单 A 有 2 条推送（先 11、后重发 12），订单 B 有 1 条（还没回读 = `none`）。
        ⇒ `total=3` 行、`order_total=2` 单；`counts["11"]=1`、`counts["12"]=1`、
        `counts["none"]=1`；而 `order_counts["11"]=1`、`order_counts["12"]=1`、
        `order_counts["none"]=1`（A 同时落在 11 与 12 两个桶里 ⇒ 各桶之和 3 > `order_total`）。
        """
        a = self._order("20261028000000000511")
        b = self._order("20261028000000000512")
        self._push(a, serial_no="19000000000000000511", remote_status=11)
        self._push(
            a,
            serial_no="19000000000000000512",
            remote_status=12,
            state=DailiantongPush.State.CLOSED,
        )
        self._push(
            b,
            serial_no="19000000000000000513",
            state=DailiantongPush.State.PENDING,
            remote_status=None,
        )

        data = self._body()

        # 行口径（旧数字**保留**：前端可能还在用）。
        self.assertEqual(data["total"], 3, data)
        self.assertEqual(data["counts"]["11"], 1, data)
        self.assertEqual(data["counts"]["12"], 1, data)
        self.assertEqual(data["counts"]["none"], 1, data)
        # 订单口径。
        self.assertEqual(data["order_total"], 2, data)
        self.assertEqual(data["order_counts"]["11"], 1, data)
        self.assertEqual(data["order_counts"]["12"], 1, data)
        self.assertEqual(data["order_counts"]["none"], 1, data)
        self.assertEqual(data["order_counts"]["13"], 0, data)
        # 键形状与 `counts` 完全一致（同桶、同名字，未知码也会两边一起出现）。
        self.assertEqual(set(data["order_counts"]), set(data["counts"]), data)

    def test_order_counts_agree_with_the_grouped_list_total(self):
        """`order_counts[X]` 与 `?group_by=order&status=X` 的 `total` **同源**（同一判据）。"""
        a = self._order("20261028000000000521")
        b = self._order("20261028000000000522")
        self._push(a, serial_no="19000000000000000521", remote_status=11)
        self._push(
            a,
            serial_no="19000000000000000522",
            remote_status=12,
            state=DailiantongPush.State.CLOSED,
        )
        self._push(b, serial_no="19000000000000000523", remote_status=12)

        counts = self._body("?platform=10")
        for code in ("11", "12", "13"):
            with self.subTest(code=code):
                listed = self.client.get(
                    PUSHES_URL + f"?group_by=order&status={code}"
                ).json()
                self.assertEqual(listed["code"], 0, listed)
                self.assertEqual(
                    listed["data"]["total"], counts["order_counts"][code], listed
                )

    def test_malformed_values_are_a_business_error_not_a_500(self):
        for query in ("?platform=abc", "?keyword=" + "x" * 65):
            with self.subTest(query=query):
                response = self.client.get(COUNTS_URL + query)
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], 1000, response.content[:200])

    def test_without_the_order_list_node_it_is_403(self):
        denied = make_logged_in_client(
            username="platform-push-counts-no-node", groups=()
        )
        response = denied.get(COUNTS_URL)
        self.assertEqual(response.status_code, 403, response.content[:200])


class PlatformPushTenantIsolationTests(TenantTestCase):
    """以租户 A 的凭据**看不到**租户 B 的推送行（`AGENTS.md` §4 的强制测试）。"""

    DOMAIN_A = "tenant.test.com"
    DOMAIN_B = "platform-push-iso-b.test.local"
    SCHEMA_B = "platform_push_iso_b"
    ORDER_A = "20261027000000000501"
    ORDER_B = "20261027000000000502"
    SERIAL_A = "19000000000000000501"
    SERIAL_B = "19000000000000000502"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="平台推送读面隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)

    def _seed_tenant(self, *, order_no, serial_no, game_name, domain, username):
        """在**当前** schema 里造游戏 / 订单 / 推送行，并返回登录 client。"""
        game = Game.objects.create(name=game_name)
        order = Order.objects.create(
            game=game, order_no=order_no, status=OrderStatus.COMPLETED
        )
        DailiantongPush.objects.create(
            order=order,
            platform=DAILIANTONG,
            state=DailiantongPush.State.SENT,
            serial_no=serial_no,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order_no,
            attempts=1,
        )
        client = make_logged_in_client(
            domain=domain, username=username, groups=("客服",), nodes=(NODE, "order.view_detail")
        )
        return client

    def test_each_tenant_only_sees_its_own_push_rows(self):
        with tenant_context(self.tenant):
            client_a = self._seed_tenant(
                order_no=self.ORDER_A,
                serial_no=self.SERIAL_A,
                game_name="原神-A",
                domain=self.DOMAIN_A,
                username="platform-push-iso-a",
            )
        with tenant_context(self.tenant_b):
            client_b = self._seed_tenant(
                order_no=self.ORDER_B,
                serial_no=self.SERIAL_B,
                game_name="原神-B",
                domain=self.DOMAIN_B,
                username="platform-push-iso-b",
            )

        detail_a = client_a.get(f"{LIST_URL}{self.ORDER_A}/").json()
        self.assertEqual(detail_a["code"], 0, detail_a)
        self.assertIs(detail_a["data"]["has_dailiantong_history"], True)
        detail_b_from_a = client_a.get(f"{LIST_URL}{self.ORDER_B}/").json()
        self.assertEqual(detail_b_from_a["code"], 1002, detail_b_from_a)
        self.assertIsNone(detail_b_from_a["data"])

        body_a = client_a.get(PUSHES_URL).json()
        self.assertEqual(body_a["code"], 0, body_a)
        serials_a = {row["serial_no"] for row in body_a["data"]["items"]}
        self.assertEqual(serials_a, {self.SERIAL_A}, body_a)
        self.assertNotIn(self.SERIAL_B, serials_a)

        body_b = client_b.get(PUSHES_URL).json()
        self.assertEqual(body_b["code"], 0, body_b)
        serials_b = {row["serial_no"] for row in body_b["data"]["items"]}
        self.assertEqual(serials_b, {self.SERIAL_B}, body_b)
        self.assertNotIn(self.SERIAL_A, serials_b)

        # ⭐ 按订单聚合的读口径也只看自己租户（与扁平模式同一个底集）。
        grouped_a = client_a.get(PUSHES_URL + "?group_by=order").json()
        self.assertEqual(grouped_a["code"], 0, grouped_a)
        self.assertEqual(
            [item["order_no"] for item in grouped_a["data"]["items"]],
            [self.ORDER_A],
            grouped_a,
        )
        grouped_b = client_b.get(PUSHES_URL + "?group_by=order").json()
        self.assertEqual(grouped_b["code"], 0, grouped_b)
        self.assertEqual(
            [item["order_no"] for item in grouped_b["data"]["items"]],
            [self.ORDER_B],
            grouped_b,
        )
        # 计数也一样：各自只看得到自己的那一单。
        counts_a = client_a.get(COUNTS_URL).json()
        self.assertEqual(counts_a["code"], 0, counts_a)
        self.assertEqual(counts_a["data"]["order_total"], 1, counts_a)
        self.assertEqual(counts_a["data"]["total"], 1, counts_a)


class ZoneMapApiTests(_OrderFixture):
    """`GET platform-integrations/zones/map/`：命中 / 未命中 / 坏入参 / 权限。"""

    ZONE_ID = "100100010000000"

    def test_hit_returns_the_remembered_zone_and_our_server_name(self):
        DailiantongZoneMap.objects.create(
            game=self.game, server=self.server, zone_server_id=self.ZONE_ID
        )
        body = self.client.get(
            ZONE_MAP_URL,
            {"game_id": self.game.pk, "server_id": self.server.pk},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["zone_server_id"], self.ZONE_ID)
        self.assertEqual(body["data"]["server_name"], "天空岛")

    def test_miss_returns_a_null_zone_but_still_names_the_server(self):
        body = self.client.get(
            ZONE_MAP_URL,
            {"game_id": self.game.pk, "server_id": self.server.pk},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsNone(body["data"]["zone_server_id"])
        self.assertEqual(body["data"]["server_name"], "天空岛")

    def test_a_server_of_another_game_is_not_a_hit(self):
        other_game = Game.objects.create(name="星穹铁道")
        body = self.client.get(
            ZONE_MAP_URL,
            {"game_id": other_game.pk, "server_id": self.server.pk},
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsNone(body["data"]["zone_server_id"])
        self.assertIsNone(body["data"]["server_name"])

    def test_malformed_ids_are_a_business_error_not_a_500(self):
        response = self.client.get(
            ZONE_MAP_URL, {"game_id": "abc", "server_id": "abc"}
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000, response.content[:200])

    def test_without_the_order_list_node_it_is_403(self):
        # ⚠️ `groups=()`：`groups=("客服",)` 会让夹具给同名 Role 补节点 ⇒ 403 变 200。
        denied = make_logged_in_client(username="zone-map-no-node", groups=())
        response = denied.get(
            ZONE_MAP_URL, {"game_id": self.game.pk, "server_id": self.server.pk}
        )
        self.assertEqual(response.status_code, 403, response.content[:200])


import unittest
from pathlib import Path

_HAS_INTERNAL_DOCS = (Path(__file__).resolve().parents[2] / "docs").is_dir()

@unittest.skipUnless(_HAS_INTERNAL_DOCS, "本仓库只开源网页前后端源码，不含内部文档 docs/")
class CredentialDocTests(SimpleTestCase):
    """⭐ P3-17：接口笔记必须写「凭据只在租户库 `PlatformIntegration`」。"""

    def _doc(self) -> str:
        path = Path(__file__).resolve().parents[2] / "docs" / "DAILIANTONG-API.md"
        return path.read_text(encoding="utf-8")

    def test_doc_points_credentials_at_platform_integration(self):
        doc = self._doc()
        self.assertIn("PlatformIntegration", doc)
        self.assertIn("不被代码读取", doc)
        self.assertNotIn("已写进**本机** `.env`", doc)

    def test_the_legacy_env_keys_are_not_read_by_any_dailiantong_module(self):
        root = Path(__file__).resolve().parents[1] / "apps" / "business"
        legacy = (
            "DAILIANTONG_APPID",
            "DAILIANTONG_SECRET",
            "DAILIANTONG_USER_ID",
            "DAILIANTONG_PAYPASS_MD5",
        )
        for path in root.glob("dailiantong_*.py"):
            text = path.read_text(encoding="utf-8")
            for key in legacy:
                self.assertNotIn(key, text, f"{path.name} 不该读 .env 里的 {key}")


# ── ⭐ 一键抛单 B5 / B6：默认区服 + 右面板账户摘要 ──────────────

from unittest import mock  # noqa: E402  —— 与文件既有风格一致，按用例就近分组

from apps.business import dailiantong_push as push_module  # noqa: E402
from apps.business.dailiantong_push import DailiantongPushError  # noqa: E402
from apps.business.models import PlatformIntegration, PlatformPushSettings  # noqa: E402

DEFAULT_ZONE_URL = "/api/business/platform-integrations/zones/default/"
SUMMARY_URL = "/api/business/platform-integrations/summary/"

#: 合成的代练通区服字典（顺序就是上游返回顺序 ⇒ 「第一条」的判据靠它）。
#: ⚠️ 合成值（不是任何真实区服编码）。
ZONE_ROWS = (
    {"ZoneServerID": "1002", "ServerName": "原神/提瓦特"},
    {"ZoneServerID": "1001", "ServerName": "原神/天空岛"},
    {"ZoneServerID": "2002", "ServerName": "星穹铁道/仙舟"},
)


class _DefaultZoneFixture(_OrderFixture):
    def setUp(self):
        super().setUp()
        push_module.clear_zones_cache()
        patcher = mock.patch.object(
            push_module, "_fetch_zone_rows", return_value=ZONE_ROWS
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(push_module.clear_zones_cache)


class DefaultZoneApiTests(_DefaultZoneFixture):
    """`GET platform-integrations/zones/default/?game_id=`：映射优先 / 首匹配 / none。"""

    def _get(self, game_id, client=None):
        return (client or self.client).get(DEFAULT_ZONE_URL, {"game_id": game_id})

    def test_existing_map_wins_over_the_dictionary_scan(self):
        DailiantongZoneMap.objects.create(
            game=self.game, server=self.server, zone_server_id="1001"
        )

        body = self._get(self.game.pk).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"],
            {"zone_server_id": "1001", "server_name": "原神/天空岛", "source": "map"},
        )

    def test_without_a_map_the_first_dictionary_row_starting_with_the_game_name_wins(self):
        body = self._get(self.game.pk).json()

        self.assertEqual(body["code"], 0, body)
        # 字典原序的第一条 = 1002（不是排序意义上的「第一条」）。
        self.assertEqual(
            body["data"],
            {
                "zone_server_id": "1002",
                "server_name": "原神/提瓦特",
                "source": "first",
            },
        )

    def test_no_match_returns_source_none_as_a_business_result(self):
        other = Game.objects.create(name="字典里没有的游戏")

        body = self._get(other.pk).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"],
            {"zone_server_id": None, "server_name": None, "source": "none"},
        )

    def test_unknown_game_id_is_also_source_none(self):
        body = self._get(987654).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["source"], "none")

    def test_malformed_game_id_is_a_business_error_not_a_500(self):
        response = self.client.get(DEFAULT_ZONE_URL, {"game_id": "abc"})

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000, response.content[:200])

    def test_dictionary_unavailable_is_a_chinese_business_error(self):
        with mock.patch.object(
            push_module,
            "_fetch_zone_rows",
            side_effect=DailiantongPushError("代练通区服字典拉不到（合成原因）"),
        ):
            push_module.clear_zones_cache()
            body = self._get(self.game.pk).json()

        self.assertEqual(body["code"], 1002, body)
        self.assertIn("代练通区服字典", body["msg"])

    def test_dictionary_unavailable_still_returns_a_remembered_zone(self):
        """有映射时字典拉不到不该把页面打成错误：映射本身就是可用的默认值。"""
        DailiantongZoneMap.objects.create(
            game=self.game, server=self.server, zone_server_id="1002"
        )
        with mock.patch.object(
            push_module,
            "_fetch_zone_rows",
            side_effect=DailiantongPushError("代练通区服字典拉不到（合成原因）"),
        ):
            push_module.clear_zones_cache()
            body = self._get(self.game.pk).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["source"], "map")
        self.assertEqual(body["data"]["zone_server_id"], "1002")
        # 字典不可用 ⇒ 名字尽力而为（空串），⛔ 不编一个。
        self.assertEqual(body["data"]["server_name"], "")

    def test_without_the_order_list_node_it_is_403(self):
        denied = make_logged_in_client(username="default-zone-no-node", groups=())
        response = denied.get(DEFAULT_ZONE_URL, {"game_id": self.game.pk})
        self.assertEqual(response.status_code, 403, response.content[:200])


class _FakeSummaryClient:
    """假代练通客户端：只实现 `user_money`（余额查询）。"""

    def __init__(self):
        self.calls = 0
        self.error: Exception | None = None
        self.money = {"SumBal": "12.34", "FreezeBal": "5.00", "ActiveBal": "7.34"}
        self.closed = False

    def user_money(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.money

    def close(self):
        self.closed = True


class _SummaryFixture(_OrderFixture):
    def setUp(self):
        super().setUp()
        push_module.clear_summary_cache()
        self.addCleanup(push_module.clear_summary_cache)
        self.integration = PlatformIntegration.objects.create(
            platform=10,
            account="USR-SYNTH-SUMMARY-01",
            app_id="synth-summary-app",
            app_secret="synth-summary-secret",
            pay_password_md5="0123456789abcdef0123456789abcdef",
            is_enabled=True,
            is_default=True,
        )
        self.fake = _FakeSummaryClient()

    def _get(self, client=None):
        with mock.patch.object(
            push_module, "_open_client", side_effect=lambda _integration: self.fake
        ):
            return (client or self.client).get(SUMMARY_URL)


class AccountSummaryApiTests(_SummaryFixture):
    """`GET platform-integrations/summary/`：平台 / 账号 / 可用 / 冻结 + 30s 缓存。"""

    def test_success_returns_platform_account_and_two_decimal_balances(self):
        body = self._get().json()

        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(data["platform"], 10)
        self.assertEqual(data["platform_label"], "代练通")
        self.assertEqual(data["account"], "USR-SYNTH-SUMMARY-01")
        self.assertIs(data["configured"], True)
        self.assertEqual(data["available_balance"], "7.34")
        self.assertEqual(data["frozen_balance"], "5.00")
        self.assertEqual(data["error"], "")
        # 凭据不进响应（账号标识与余额可以）。
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("synth-summary-secret", dumped)
        self.assertNotIn("0123456789abcdef0123456789abcdef", dumped)

    def test_balance_is_cached_for_the_ttl_window(self):
        self._get()
        self._get()
        self.assertEqual(self.fake.calls, 1, "30 秒内第二次读不许再打上游")

        # 把 TTL 打到 0 ⇒ 下一次必须重新查（证明缓存真的按 TTL 失效，不是永远缓存）。
        with mock.patch.object(push_module, "SUMMARY_CACHE_TTL_SECONDS", 0.0):
            self._get()
        self.assertEqual(self.fake.calls, 2)

    def test_no_integration_returns_a_chinese_reason_and_zero_upstream_calls(self):
        PlatformIntegration.objects.all().delete()

        body = self._get().json()

        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertIs(data["configured"], False)
        self.assertIsNone(data["available_balance"])
        self.assertIsNone(data["frozen_balance"])
        self.assertIn("代练通账号", data["error"])
        self.assertIn("交易平台对接设置", data["error"], "原因要带下一步去哪配")
        self.assertEqual(self.fake.calls, 0)

    def test_upstream_failure_returns_a_chinese_reason_and_no_fake_zero(self):
        from apps.business.dailiantong_client import DailiantongError

        self.fake.error = DailiantongError(
            code="-102", message="签名错误", action="UserMoney"
        )

        body = self._get().json()

        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertIs(data["configured"], True)
        self.assertIsNone(data["available_balance"])
        self.assertIsNone(data["frozen_balance"])
        self.assertIn("查询代练通余额失败", data["error"])
        self.assertIn("签名错误", data["error"])

    def test_invalid_money_is_a_readable_error_not_a_zero(self):
        self.fake.money = {"SumBal": "x", "FreezeBal": "y", "ActiveBal": "not-a-number"}

        data = self._get().json()["data"]

        self.assertIsNone(data["available_balance"])
        self.assertIsNone(data["frozen_balance"])
        self.assertIn("不是有效金额", data["error"])

    def test_failures_are_cached_too(self):
        from apps.business.dailiantong_client import DailiantongError

        self.fake.error = DailiantongError(code="-1", message="boom", action="UserMoney")

        self._get()
        self._get()

        self.assertEqual(self.fake.calls, 1, "失败结果同样要吃 30 秒缓存（别每开一次页面打一次）")

    def test_settings_missing_credentials_is_a_chinese_reason_not_an_exception(self):
        self.integration.app_secret = ""
        self.integration.save(update_fields=["app_secret"])

        data = self._get().json()["data"]

        self.assertIs(data["configured"], False)
        self.assertIn("配置不完整", data["error"])
        self.assertIn("AppSecret", data["error"])
        self.assertEqual(self.fake.calls, 0)

    def test_without_the_order_list_node_it_is_403(self):
        denied = make_logged_in_client(username="summary-no-node", groups=())
        response = denied.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 403, response.content[:200])


class SummaryCacheTenantIsolationTests(TenantTestCase):
    """摘要缓存键必须带 **schema**：两个租户的配置行 pk 都是 1，⛔ 不能串余额。

    ⚠️ 用两个真租户（`provision_tenant` + `tenant_context`），⛔ 不靠改
    `connection.schema_name` 假装换租户——那会把 django-tenants 的 search_path 一起改坏
    （实测：`relation "business_platformintegration" does not exist`）。
    """

    SCHEMA_B = "summary_cache_iso_b"
    DOMAIN_B = "summary-cache-iso-b.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B,
            name="摘要缓存隔离租户 B",
            domain=cls.DOMAIN_B,
        )

    def setUp(self):
        push_module.clear_summary_cache()
        self.addCleanup(push_module.clear_summary_cache)

    def _integration(self, account: str) -> PlatformIntegration:
        return PlatformIntegration.objects.create(
            platform=10,
            account=account,
            app_id=f"synth-{account}-app",
            app_secret="synth-summary-secret",
            pay_password_md5="0123456789abcdef0123456789abcdef",
            is_enabled=True,
            is_default=True,
        )

    def test_each_tenant_reads_its_own_balance(self):
        with tenant_context(self.tenant):
            integration_a = self._integration("USR-SYNTH-CACHE-A")
            fake_a = _FakeSummaryClient()
            fake_a.money = {
                "SumBal": "7.34",
                "FreezeBal": "0.00",
                "ActiveBal": "7.34",
            }
            with mock.patch.object(
                push_module, "_open_client", side_effect=lambda _i: fake_a
            ):
                first = push_module.platform_account_summary()
                again = push_module.platform_account_summary()
            self.assertEqual(first["available_balance"], "7.34")
            self.assertEqual(again["available_balance"], "7.34")
            self.assertEqual(fake_a.calls, 1, "同租户第二次读要吃缓存")
            pk_a = integration_a.pk

        with tenant_context(self.tenant_b):
            integration_b = self._integration("USR-SYNTH-CACHE-B")
            # 两个租户的配置行 pk 各自从 1 开始（这就是「只按 pk 当缓存键」会串的现场）。
            self.assertEqual(integration_b.pk, pk_a)
            fake_b = _FakeSummaryClient()
            fake_b.money = {
                "SumBal": "99.00",
                "FreezeBal": "1.00",
                "ActiveBal": "98.00",
            }
            with mock.patch.object(
                push_module, "_open_client", side_effect=lambda _i: fake_b
            ):
                second = push_module.platform_account_summary()
            self.assertEqual(second["available_balance"], "98.00")
            self.assertEqual(second["account"], "USR-SYNTH-CACHE-B")
            self.assertEqual(fake_b.calls, 1, "换租户必须重新查（⛔ 用 A 的缓存）")


class GameNameMatchingTests(_DefaultZoneFixture):
    """⭐ 区服匹配**只认精确 / 归一化同名**（评审阻断级判据）。

    旧实现用 `ServerName.startswith(游戏名)`：
    - 「王者荣耀」会把「王者荣耀世界/安卓区」一起收进来；
    - 默认区服取第一条 ⇒ 错配会被当成默认，甚至写进 `DailiantongZoneMap` 一直错下去；
    - 游戏名与字典写法对不上时又**一个都匹配不到**（下拉 0 个选项 ⇒ 永远发不出单）。
    """

    #: 合成字典：包含一个「前缀撞车」的别的游戏、一个带修饰词的同一个游戏。
    ZONE_ROWS = (
        {"ZoneServerID": "9001", "ServerName": "王者荣耀世界/安卓QQ区"},
        {"ZoneServerID": "9002", "ServerName": "王者荣耀/安卓QQ区"},
        {"ZoneServerID": "9003", "ServerName": "王者荣耀（手游）/苹果QQ区"},
        {"ZoneServerID": "9004", "ServerName": "英雄联盟手游/安卓QQ区"},
    )

    def setUp(self):
        super().setUp()
        # 覆盖夹具里的字典：**再叠一层** patch（后打的赢），⛔ 不去动父类那份
        # （它没把 patcher 挂在 self 上；停掉它反而会露出真实网络）。
        patcher = mock.patch.object(
            push_module, "_fetch_zone_rows", return_value=self.ZONE_ROWS
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        push_module.clear_zones_cache()

    def _game(self, name: str) -> Game:
        return Game.objects.create(name=name)

    def test_a_longer_game_name_with_the_same_prefix_is_not_matched(self):
        game = self._game("王者荣耀")

        body = self.client.get(DEFAULT_ZONE_URL, {"game_id": game.pk}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["zone_server_id"], "9002")
        self.assertEqual(body["data"]["server_name"], "王者荣耀/安卓QQ区")
        self.assertEqual(body["data"]["source"], "first")

    def test_the_dropdown_excludes_the_other_game_and_keeps_normalized_names(self):
        game = self._game("王者荣耀")

        response = self.client.get("/api/business/platform-integrations/zones/", {"game_id": game.pk})
        body = response.json()

        self.assertEqual(body["code"], 0, body)
        ids = [row["zone_server_id"] for row in body["data"]]
        # 精确同名那一档优先 ⇒ 「王者荣耀世界」整条不进候选；
        # 「王者荣耀（手游）」归一化后同名 ⇒ 与精确那一条一起列出（字典原序）。
        self.assertEqual(ids, ["9002", "9003"])

    def test_a_修饰词_tail_is_matched_by_the_normalized_tier(self):
        """我方游戏名干净、字典带「手游」尾巴 ⇒ 归一化同名要能匹配上（第二级）。"""
        game = self._game("英雄联盟")

        body = self.client.get(DEFAULT_ZONE_URL, {"game_id": game.pk}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["zone_server_id"], "9004")
        self.assertEqual(body["data"]["source"], "first")

    def test_no_match_stays_none_and_empty_instead_of_guessing(self):
        game = self._game("字典里没有的游戏")

        default = self.client.get(DEFAULT_ZONE_URL, {"game_id": game.pk}).json()
        zones = self.client.get(
            "/api/business/platform-integrations/zones/", {"game_id": game.pk}
        ).json()

        self.assertEqual(default["data"]["source"], "none")
        self.assertIsNone(default["data"]["zone_server_id"])
        self.assertEqual(zones["data"], [])

    def test_the_prefix_collision_is_never_remembered_as_the_default(self):
        """默认值只来自**用户确认过**的选择：错配的「前缀撞车」不会进映射表。

        判据：替「用户没选过」的调用方（预览 / 默认区服端点）**不写** `DailiantongZoneMap`；
        写映射只发生在 `push_order` 收到**提交上来的**区服编码时。
        """
        game = self._game("王者荣耀")
        DailiantongZoneMap.objects.filter(game=game).delete()

        self.client.get(DEFAULT_ZONE_URL, {"game_id": game.pk})

        self.assertFalse(
            DailiantongZoneMap.objects.filter(game=game).exists(),
            "读端点/预览不该写映射表（只有用户确认的提交才记）",
        )
