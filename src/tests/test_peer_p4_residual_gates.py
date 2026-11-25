"""上下游 P4 挂账⑨：接收侧「对端值进**有界列** / 做**精确查找键**」的整类形状闸。

## 这一类洞为什么必须按类收

接收端点（`peer-sync/inbound/events/`）把**整批**事件包在一个 `transaction.atomic()` 里，
而每条事件自己还有一个 `transaction.atomic()`（`apps/business/peer_inbound.py`）。所以：

- 任何一条事件里**一个对端可控的值**让 PostgreSQL 抛非 DRF 异常（`value too long` /
  `numeric field overflow` / `ValidationError`）⇒ **整批 500 + 全部回滚**，发送侧会一直重投
  这条毒事件，把绑定推向自动停绑；
- 一个非字符串被 Django 的 `str()` 悄悄收下 ⇒ **静默换成另一个值**。落在**精确查找键**上时
  这比 500 更坏：截断 / 强转后的键可能命中**另一张单**（静默错单）。

本文件是 Task 1 的 **Step-1 探针**，跑完后**保留成正式回归网**：逐条喂敌意值，断言
「`bad_shape` 硬跳过 + 零写入 + 不抛异常」，并钉住**边界值仍然放行**（否则"一律拒绝"也会全绿）。

## 闸的边界（⛔ 与实现里的注释同一条口径）

进闸的**只有**「会落进有界列」或「做精确查找键」的对端可控值：

- `target_ref`（两个接收支）：精确查找键 + `PeerOrderLink.peer_order_no`（有界）⇒ **绝不截断**；
- 下行 `payload` 的 `total_price` / `hall_price`（`Order` 的 numeric 列）、
  `customer_phone` / `customer_qq`（`CharField(64)`）、`project_text`（`CharField(900)`（D-0218，此前 300），
  **已登记**的按列宽截断口径 ⇒ 只判类型、超宽照旧截断）。

**有意不进闸**（各自已有出口，或不在"有界列"这一类里）：

- `status` / `withdraw_reason`：已有枚举闸（`unknown_status` / `bad_shape`）；
- `occurred_at`：已有 `_parse_event_time` 形状闸（不可用 ⇒ `bad_shape`）；
- `remark` / `content`：`Order` 上是 **`TextField`（无列宽）**⇒ 超长本来就存得下；
- `source_order_no`：已有「非字符串退回对端单号 + 按列宽截断」口径；
- `actor_name`：`apps/audit/log.py` 的 `_clip` 已归一化 + 截断；
- 截图载荷的 `object_key` / `img_type` / `remark`：已有 `_is_shared_screenshot_key` 与备注截断。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
⚠️ 夹具一律**合成值**（`*.test.invalid` / `测试…` / `SYN-P4-GATE-*`）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone as django_timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_inbound import (
    SKIP_BAD_SHAPE,
    SKIP_UNKNOWN_ORDER,
    SKIP_UNKNOWN_STATUS,
    apply_events,
    apply_upstream_events,
)
from apps.business.peer_sync import hash_token, new_token

#: 两端对齐用的游戏名（合成值）。
GAME = "测试残留闸游戏"
#: 事件时刻的基准（合成值）。
BASE_TIME = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
#: `TenantTestCase` 自带租户的域名（与 `make_logged_in_client` 的默认值同源）。
PEER_DOMAIN = "tenant.test.com"
INBOUND_URL = "/api/business/peer-sync/inbound/events/"
#: 一条合法的共享截图对象名（合成值；形状闸要求 `shared-orders/<origin>/<随机>.<ext>`）。
SHARED_KEY = "shared-orders/SYN-P4-GATE-ORIG-1/abcdef123456.jpg"


class TestPeerBase(TenantTestCase):
    """共用夹具：一款游戏 + 两个方向的绑定（下行 / 上行各一条）。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME)
        self.kefu = get_user_model().objects.create_user(
            username="syn-p4-gate-kefu", password="pw-syn-p4-gate"
        )
        # 下行支：我方是**下家**，收上家推来的单。
        self.raw_upstream_token = new_token()
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(self.raw_upstream_token),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )
        # 上行支：我方是**上家**，收下家回传的状态。
        self.downstream_account = get_user_model().objects.create_user(
            username="syn-p4-gate-down", password="pw-syn-p4-gate"
        )
        self.raw_downstream_token = new_token()
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.downstream_account,
            peer_base_url="https://peer-c.test.invalid",
            token_in_hash=hash_token(self.raw_downstream_token),
            token_out=new_token(),
        )

    # ── 造事件 ────────────────────────────────────────────────────────────
    def _event(self, kind, ref, *, payload=None, **top):
        """一条**形状合法**的事件；敌意值由调用方覆盖 / 删键。"""
        body = {
            "status": "IN_PROGRESS",
            "total_price": "88.00",
            "hall_price": "12.00",
            "remark": "上家备注",
            "project_text": "项目文本",
            "customer_phone": "13800000000",
            "customer_qq": "10001",
            "content": "客户原始需求",
            "game_name": GAME,
        }
        body.update(payload or {})
        event = {
            "kind": kind,
            "target_ref": ref,
            "occurred_at": BASE_TIME.isoformat(),
            "payload": body,
        }
        event.update(top)
        return event

    def _screenshot_event(self, kind, ref, *, payload=None, **top):
        body = {"object_key": SHARED_KEY, "img_type": 0, "remark": "截图备注"}
        body.update(payload or {})
        return self._event(kind, ref, payload=body, **top)

    def _future(self, seconds=30):
        """相对**现在**往后的 aware 时刻（上行支按 `order.updated_at` 裁决新旧，得比它晚）。"""
        return (django_timezone.now() + timedelta(seconds=seconds)).isoformat()

    def _local_order(self, number):
        """一张本地单 + MAIN 绩效行（接手人 = 这条下游绑定的账号）⇒ 上行安全判据通过。"""
        order = Order.objects.create(
            order_no=number, game=self.game, status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, user=self.downstream_account, emp_type=EmpType.MAIN
        )
        return order

    # ── 跑与断言 ──────────────────────────────────────────────────────────
    def _apply(self, events):
        return apply_events(events, binding=self.upstream_binding)

    def _apply_up(self, events):
        return apply_upstream_events(events, binding=self.downstream_binding)

    def _assert_bad_shape(self, outcome):
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], SKIP_BAD_SHAPE, outcome
        )

    def _assert_zero_writes(self):
        """被形状闸拒掉的事件**零写入**（⛔ 不许在配置表里留下游戏行）。"""
        self.assertEqual(Order.objects.count(), 0, "不应落任何订单行")
        self.assertEqual(PeerOrderLink.objects.count(), 0, "不应落任何链行")
        self.assertEqual(OrderScreenshot.objects.count(), 0, "不应落任何截图行")
        self.assertEqual(Game.objects.count(), 1, "不应新建 / 复活游戏行")


class DownstreamTargetRefGateTests(TestPeerBase):
    """`target_ref`：精确查找键 + 有界列 ⇒ 非空字符串、装得进列、**绝不截断**。"""

    def test_a_long_target_ref_is_a_bad_shape_skip_not_a_500(self):
        outcome = self._apply([self._event("order.assign", "X" * 65)])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_a_target_ref_at_the_column_width_is_accepted(self):
        """边界值照旧放行（列宽 64 逐字写库，⛔ 不是"一律拒绝长串"）。"""
        ref = "SYN-P4-GATE-" + "x" * 52
        self.assertEqual(len(ref), 64, "夹具自证：这条 target_ref 恰好等于列宽")
        outcome = self._apply([self._event("order.assign", ref)])
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertTrue(PeerOrderLink.objects.filter(peer_order_no=ref).exists())
        self.assertTrue(Order.objects.filter(source_order_no=ref).exists())

    def test_a_non_string_target_ref_list_is_a_bad_shape_skip(self):
        outcome = self._apply([self._event("order.assign", ["SYN-P4-GATE-LIST"])])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_an_integer_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply([self._event("order.assign", 424242)])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_a_mapping_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply([
            self._event("order.assign", {"peer_order_no": "SYN-P4-GATE-DICT"})
        ])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_an_empty_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply([self._event("order.assign", "")])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_a_missing_target_ref_is_a_bad_shape_skip(self):
        event = self._event("order.assign", "SYN-P4-GATE-MISSING")
        del event["target_ref"]
        outcome = self._apply([event])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()
        self.assertEqual(outcome.skipped_details[0]["target_ref"], "")

    def test_a_long_target_ref_on_a_screenshot_event_is_a_bad_shape_skip(self):
        outcome = self._apply([
            self._screenshot_event("order.screenshot.add", "X" * 65)
        ])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()


class DownstreamMoneyGateTests(TestPeerBase):
    """`hall_price`：`Order` 的 `numeric(12,2)` 有界列 ⇒ 形状闸。

    ⛔ 2026-10-25 业务方裁定：客户价（= 接单价）取**发单价**（`hall_price`），载荷里的
    `total_price` 不再参与写入 ⇒ 形状闸只对 `hall_price` 生效。
    """

    def _one(self, key, value):
        return self._apply([self._event("order.assign", "SYN-P4-GATE-" + key, payload={key: value})])

    def test_a_non_numeric_hall_price_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("hall_price", "abc"))
        self._assert_zero_writes()

    def test_an_empty_hall_price_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("hall_price", ""))
        self._assert_zero_writes()

    def test_a_list_hall_price_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("hall_price", [1, 2]))
        self._assert_zero_writes()

    def test_an_over_precision_hall_price_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("hall_price", "99999999999999"))
        self._assert_zero_writes()

    def test_the_largest_storable_amount_is_accepted(self):
        """边界值放行：`numeric(12,2)` 的**最大值**逐字写库，⛔ 不是一律拒绝。"""
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-MAXAMT", payload={
            "total_price": "9999999999.99", "hall_price": "9999999999.99",
        })])
        self.assertEqual(outcome.applied, 1, outcome)
        order = Order.objects.get(source_order_no="SYN-P4-GATE-MAXAMT")
        self.assertEqual(order.total_price, Decimal("9999999999.99"))
        self.assertEqual(order.hall_price, Decimal("9999999999.99"))

    def test_zero_and_negative_prices_land_on_the_order_flow(self):
        """金额边界（⭐ D-0306，2026-10-26 业务方终裁）：`0` 与**负数**都照常接受。

        - `0` 是 D-0015 的合法边界（⛔ 真值判据不许把它当"没传"）；
        - **负价在订单流放行**（旧口径 A7 / L-5 的 `negative_price_rejected` 已下线）；
        - 大厅路的负价仍然不同步（另见 `test_a_negative_hall_price_is_still_rejected`）。

        ⛔ 2026-10-25 业务方裁定：客户价（= 接单价）取**发单价** ⇒ 建单支两列同源。
        """
        outcome = self._apply([
            self._event("order.assign", "SYN-P4-GATE-ZERO", payload={"hall_price": 0}),
        ])
        self.assertEqual(outcome.applied, 1, outcome)
        zero = Order.objects.get(source_order_no="SYN-P4-GATE-ZERO")
        self.assertEqual(zero.total_price, Decimal("0.00"))
        self.assertEqual(zero.hall_price, Decimal("0.00"))
        # ⭐ D-0306：负发单价照落两列（订单流放行）。
        outcome = self._apply([
            self._event("order.assign", "SYN-P4-GATE-NEG", payload={"hall_price": -1}),
        ])
        self.assertEqual(outcome.applied, 1, outcome)
        negative = Order.objects.get(source_order_no="SYN-P4-GATE-NEG")
        self.assertEqual(negative.total_price, Decimal("-1.00"))
        self.assertEqual(negative.hall_price, Decimal("-1.00"))

    def test_a_negative_hall_price_is_still_rejected_on_the_hall_flow(self):
        """⭐ D-0306：**大厅路**负价仍然整条跳过（`negative_price_rejected`，零写入）。

        与上一条并列才说明「按 kind 分流」是真的：订单事件放行、大厅事件不放行。
        """
        outcome = self._apply([
            self._event("hall.throw", "SYN-P4-GATE-HALL-NEG", payload={"hall_price": -1}),
        ])
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], "negative_price_rejected", outcome
        )
        self.assertFalse(PeerHallMirror.objects.filter(
            peer_order_no="SYN-P4-GATE-HALL-NEG"
        ).exists())

    def test_the_payload_total_price_is_ignored_and_hall_price_is_used(self):
        """⛔ 载荷里的 `total_price` 不再参与写入（`PEER_ORDER_FIELDS` 已删它）。"""
        outcome = self._apply([self._event(
            "order.assign", "SYN-P4-GATE-IGNORED",
            payload={"total_price": "1.00", "hall_price": "2.00"},
        )])
        self.assertEqual(outcome.applied, 1, outcome)
        order = Order.objects.get(source_order_no="SYN-P4-GATE-IGNORED")
        self.assertEqual(order.total_price, Decimal("2.00"))
        self.assertEqual(order.hall_price, Decimal("2.00"))

    def test_missing_amounts_are_accepted(self):
        """缺键 / `None` = 对端没给价 ⇒ 照旧（建单支回落 0 / 可空列留空），⛔ 不是形状错。"""
        event = self._event("order.assign", "SYN-P4-GATE-NOAMT", payload={"hall_price": None})
        del event["payload"]["total_price"]
        outcome = self._apply([event])
        self.assertEqual(outcome.applied, 1, outcome)
        order = Order.objects.get(source_order_no="SYN-P4-GATE-NOAMT")
        self.assertEqual(order.total_price, Decimal("0.00"))
        self.assertIsNone(order.hall_price)


class DownstreamBoundedTextGateTests(TestPeerBase):
    """`customer_phone` / `customer_qq`（`CharField(64)`）与 `project_text`（已登记截断口径）。"""

    def _one(self, key, value):
        return self._apply([self._event("order.assign", "SYN-P4-GATE-" + key, payload={key: value})])

    def test_an_over_long_customer_phone_is_a_bad_shape_skip_not_a_500(self):
        self._assert_bad_shape(self._one("customer_phone", "1" * 65))
        self._assert_zero_writes()

    def test_a_customer_phone_at_the_column_width_is_accepted(self):
        outcome = self._one("customer_phone", "1" * 64)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(
            Order.objects.get(source_order_no="SYN-P4-GATE-customer_phone").customer_phone,
            "1" * 64,
        )

    def test_a_non_string_customer_phone_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("customer_phone", ["13800000000"]))
        self._assert_zero_writes()

    def test_an_over_long_customer_qq_is_a_bad_shape_skip_not_a_500(self):
        self._assert_bad_shape(self._one("customer_qq", "1" * 65))
        self._assert_zero_writes()

    def test_a_non_string_customer_qq_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("customer_qq", {"qq": "10001"}))
        self._assert_zero_writes()

    def test_a_non_string_project_text_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("project_text", 5))
        self._assert_zero_writes()

    def test_a_list_project_text_is_a_bad_shape_skip(self):
        self._assert_bad_shape(self._one("project_text", ["项目文本"]))
        self._assert_zero_writes()

    def test_an_over_long_project_text_is_still_clipped_and_applies(self):
        """已登记口径（R19）：`project_text` 超列宽**按列宽截断**，⛔ 不改判成 `bad_shape`。"""
        width = Order._meta.get_field("project_text").max_length
        outcome = self._one("project_text", "项" * (width + 100))
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(
            Order.objects.get(source_order_no="SYN-P4-GATE-project_text").project_text,
            "项" * width,
        )


class DownstreamAlreadyGatedTests(TestPeerBase):
    """已有出口的字段：探针钉住**今天的**行为，证明闸没有把它们改判。"""

    def test_an_unacceptable_status_is_an_unknown_status_skip(self):
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-STATUS", payload={
            "status": ["IN_PROGRESS"],
        })])
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], SKIP_UNKNOWN_STATUS, outcome
        )
        self._assert_zero_writes()

    def test_a_bad_occurred_at_on_an_existing_order_is_a_bad_shape_skip(self):
        self._apply([self._event("order.assign", "SYN-P4-GATE-TIME")])
        outcome = self._apply([self._event(
            "order.update", "SYN-P4-GATE-TIME", occurred_at=123,
        )])
        self._assert_bad_shape(outcome)

    def test_a_bad_withdraw_reason_is_a_bad_shape_skip(self):
        self._apply([self._event("order.assign", "SYN-P4-GATE-WD")])
        event = self._event(
            "order.withdraw", "SYN-P4-GATE-WD",
            # 必须**晚于**上一次已应用的时刻，否则会先被判 `stale`，走不到撤单原因那一闸。
            occurred_at=(BASE_TIME + timedelta(seconds=30)).isoformat(),
        )
        event["withdraw_reason"] = ["taker_removed"]
        outcome = self._apply([event])
        self._assert_bad_shape(outcome)

    def test_an_over_long_game_name_is_clipped_and_applies(self):
        """已登记口径：`game_name` 按 `Game.name` 列宽截断、名字对不上就自动建（⛔ 不改判）。"""
        width = Game._meta.get_field("name").max_length
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-GAME", payload={
            "game_name": "测" * (width + 36),
        })])
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertTrue(Game.objects.filter(name="测" * width).exists())

    def test_a_non_string_game_name_is_a_bad_shape_skip(self):
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-GAMEBAD", payload={
            "game_name": ["测试残留闸游戏"],
        })])
        self._assert_bad_shape(outcome)
        self._assert_zero_writes()

    def test_a_bad_source_order_no_falls_back_to_the_peer_order_no(self):
        """已有口径：`source_order_no` 非字符串 ⇒ 退回对端单号（链身份不丢、也不 500）。"""
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-ORIGIN", payload={
            "source_order_no": ["SYN-P4-GATE-ANCESTOR"],
        })])
        self.assertEqual(outcome.applied, 1, outcome)
        link = PeerOrderLink.objects.get(peer_order_no="SYN-P4-GATE-ORIGIN")
        self.assertEqual(link.origin_order_no, "SYN-P4-GATE-ORIGIN")


class DownstreamOutOfScopeTests(TestPeerBase):
    """**有意不进闸**的值：`remark` / `content` 是 `TextField`（无列宽）⇒ 超长本来就存得下。"""

    def test_an_over_long_remark_is_stored_intact(self):
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-REMARK", payload={
            "remark": "备" * 5000, "content": "需" * 5000,
        })])
        self.assertEqual(outcome.applied, 1, outcome)
        order = Order.objects.get(source_order_no="SYN-P4-GATE-REMARK")
        self.assertEqual(len(order.remark), 5000)
        self.assertEqual(len(order.content), 5000)

    def test_a_bogus_order_field_in_a_screenshot_event_does_not_block_the_screenshot(self):
        """截图事件不读订单字段 ⇒ 那些字段**没有落进任何列**，⛔ 不许拿它们把截图顶掉。"""
        outcome = self._apply([self._screenshot_event(
            "order.screenshot.add", "SYN-P4-GATE-SHOTMIX",
            payload={"total_price": [1, 2]},
        )])
        # 本地没有这张单的链行 ⇒ 截图本身走 `unknown_order`（与 `target_ref` 的形状无关）。
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], SKIP_UNKNOWN_ORDER, outcome
        )

    def test_a_non_string_remark_is_treated_as_absent_not_coerced(self):
        """⭐ A1 / H-1 改准（2026-10-25）：非字符串 `remark` 按「没带该字段」处理——
        落空串、事件照常应用，⛔ 不再被 Django 静默 `str()`（原残差已随关闭）。

        ⚠️ 原用例钉的「`str(["上家备注"])` 直落列」正是 H-1 的毒载荷形状（dict / list
        打 500 ⇒ 重试风暴 ⇒ 停绑）；现在的口径见 `_peer_text_or_empty`。
        """
        outcome = self._apply([self._event("order.assign", "SYN-P4-GATE-REMARKLIST", payload={
            "remark": ["上家备注"],
        })])
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        self.assertEqual(
            Order.objects.get(source_order_no="SYN-P4-GATE-REMARKLIST").remark,
            "",
        )


class UpstreamTargetRefGateTests(TestPeerBase):
    """上行支：`target_ref` 是**精确查找键**（查 `Order.order_no`）⇒ 同一形状闸。"""

    def setUp(self):
        super().setUp()
        self.order = self._local_order("SYN-P4-GATE-UP-1")

    def _echo(self, ref, *, payload=None, **top):
        # 上行支按 `order.updated_at`（本地动作时刻）裁决新旧 ⇒ 事件时刻必须比它晚。
        top.setdefault("occurred_at", self._future())
        return self._event("order.update", ref, payload={"status": "ISSUE", **(payload or {})}, **top)

    def test_a_long_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply_up([self._echo("X" * 65)])
        self._assert_bad_shape(outcome)

    def test_a_non_string_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply_up([self._echo(["SYN-P4-GATE-UP-1"])])
        self._assert_bad_shape(outcome)

    def test_an_integer_target_ref_is_a_bad_shape_skip(self):
        outcome = self._apply_up([self._echo(20260927)])
        self._assert_bad_shape(outcome)

    def test_a_long_target_ref_on_a_screenshot_event_is_a_bad_shape_skip(self):
        outcome = self._apply_up([self._screenshot_event(
            "order.screenshot.add", "X" * 65,
        )])
        self._assert_bad_shape(outcome)

    def test_an_unknown_but_well_shaped_ref_is_still_unknown_order(self):
        """对照：形状合法但查不到 ⇒ 仍是 `unknown_order`（⛔ 不许把"查不到"改判成形状错）。"""
        outcome = self._apply_up([self._echo("SYN-P4-GATE-UP-NOPE")])
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], SKIP_UNKNOWN_ORDER, outcome
        )

    def test_a_valid_ref_still_applies_the_status(self):
        outcome = self._apply_up([self._echo("SYN-P4-GATE-UP-1")])
        self.assertEqual(outcome.applied, 1, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "ISSUE")


class MixedBatchTests(TestPeerBase):
    """一条毒事件不许把**同一批**里其它正常事件一起带走（零写入 / 不 500）。"""

    def test_a_hostile_event_does_not_stop_a_good_one(self):
        outcome = self._apply([
            self._event("order.assign", "X" * 65),
            self._event("order.assign", "SYN-P4-GATE-MIX-OK"),
        ])
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        self.assertEqual(outcome.skipped_details[0]["code"], SKIP_BAD_SHAPE, outcome)
        self.assertTrue(
            Order.objects.filter(source_order_no="SYN-P4-GATE-MIX-OK").exists()
        )
        self.assertFalse(PeerOrderLink.objects.exclude(peer_order_no="SYN-P4-GATE-MIX-OK").exists())


class MixedBatchEndpointTests(TestPeerBase):
    """端点层：整批共用一个 `transaction.atomic()` ⇒ 毒事件不许把它打成 500 / 全批回滚。"""

    def test_the_endpoint_keeps_the_good_event_and_skips_the_hostile_one(self):
        resp = Client(SERVER_NAME=PEER_DOMAIN).post(
            INBOUND_URL,
            {"events": [
                self._event("order.assign", "X" * 65),
                self._event("order.assign", "SYN-P4-GATE-EP-OK"),
            ]},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)
        self.assertEqual(body["data"]["skipped"], 1, body)
        self.assertEqual(
            body["data"]["skipped_details"][0]["code"], SKIP_BAD_SHAPE, body
        )
        self.assertTrue(
            Order.objects.filter(source_order_no="SYN-P4-GATE-EP-OK").exists()
        )
