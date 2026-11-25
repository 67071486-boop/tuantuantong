"""完成态金额联动（spec §2.1 / F1；分支 `feat/perf-dialog-sync-and-complete-gate`）。

三件事一次钉住：

1. 订单当前状态 == `COMPLETED` 时，改「有效基价」的**四**条写路径 —— `/hall-price/`、
   `/assign/` 带 `price` 的那一支、`/adjust-amount/`、legacy `/adjust/`（裁定 A，
  纳入；它此前用 `review_status` 当判据） —— 都要把 `temp_amount` **与**
   `bonus_amount` 用**同一算式、同一有效基价**（`taker_price` 已设则优先，否则
   `order.hall_price` × 提成率）一起重算；
2. 非 `COMPLETED` 状态 ⇒ `bonus_amount` **不动**（反向对照，防「一刀切」）；
3. 该 MAIN 行**已结账**（`Performance.is_settled` 三字段并集）⇒ 四条路径**各**回
   `1014` 且**一行不写**（断数据库行，不是只看状态码）。

判据一律写字面值（E-0024），写调用断言**数据库行**（E-0009）。

⚠️ 夹具提成率取 **25%**（不是默认回落 100%）：回落档下「基价」与「金额」不可区分。
⚠️ 金额列刻意给**非 0** 的初值（`bonus_amount = 25.00`）：0 初值下「实现根本没写」与
「写成了 0」无法区分。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

User = get_user_model()

#: 夹具提成率（百分数）。25% 让「新基价 × 率」与旧值明显不同。
RATE = Decimal("25.00")

#: 三条写路径各自要过的节点门（字面量，取自 `order_views.ORDER_PERMISSIONS` 与
#: `performance_views` 的同一颗 `order.submit_payout_price`）。
NODES = (
    "order.set_offer_price",          # `/hall-price/`
    "order.assign_taker",             # `/assign/` 类级门之一
    "order.submit_payout_price",      # `/assign/` 带价 + `/adjust-amount/`
    "order.change_status",            # 「已完成 → 代练中」那一跳（反向对照用）
)


class _CompletedAmountFixture(TenantTestCase):
    """一张 `COMPLETED` 单 + 25% 提成的接单人 + 一条未结账的 MAIN 绩效。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.taker = User.objects.create_user(username="测试打手甲")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=RATE,
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ /assign/ 那两条用例的打手要持本类的游戏（走 make_logged_in_client 默认档的
        #   是**客服**，不是这个被指派的打手）。
        grant_games(self.taker, self.game)
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260925000000000901",
            status=OrderStatus.COMPLETED,
            total_price=Decimal("400.00"),
            hall_price=Decimal("100.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),      # 100.00 × 25%
            bonus_amount=Decimal("25.00"),     # 完成那一刻的快照
        )
        # ⚠️ `games` 走默认值（当前租户全部游戏，D-0136）：本夹具只建一款游戏，
        #    默认档已经覆盖它 ⇒ 不需要 `grant_games`。
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=NODES
        )

    # ── 三条写路径的驱动 ────────────────────────────────────────────────
    def _hall_price(self, price):
        return self.client.post(
            f"/api/business/orders/{self.order.order_no}/hall-price/",
            {"price": price},
            content_type="application/json",
        ).json()

    def _adjust_amount(self, payload):
        return self.client.post(
            f"/api/business/performances/{self.perf.id}/adjust-amount/",
            payload,
            content_type="application/json",
        ).json()

    def _adjust(self, payload):
        """legacy `/adjust/`（第 4 条改有效基价的路径，裁定 A）。"""
        return self.client.post(
            f"/api/business/performances/{self.perf.id}/adjust/",
            payload,
            content_type="application/json",
        ).json()

    def _order_row(self):
        return Order.objects.get(pk=self.order.pk)

    def _perf_row(self):
        return Performance.objects.get(pk=self.perf.pk)

    def _full_snapshot(self):
        """两张表的**整行**快照（`values()` 全列）——用来判「一行不写」。"""
        return (
            Order.objects.filter(pk=self.order.pk).values().get(),
            Performance.objects.filter(pk=self.perf.pk).values().get(),
        )

    def _mark_settled(self):
        """把 MAIN 行标成已结账（三字段并集里的一支就够，`is_settled` 是并集）。"""
        Performance.objects.filter(pk=self.perf.pk).update(
            settle_status=SettleStatus.SETTLED
        )


class CompletedHallPriceSyncTests(_CompletedAmountFixture):
    """`/hall-price/`：完成态改发单价 ⇒ 预计与生效**一起**按新基价重算。"""

    def test_completed_hall_price_change_syncs_temp_and_bonus(self):
        body = self._hall_price("200.00")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._order_row().hall_price, Decimal("200.00"))
        row = self._perf_row()
        # 200.00 × 25% = 50.00；两列必须**相等且等于这个算式**
        self.assertEqual(row.temp_amount, Decimal("50.00"))
        self.assertEqual(row.bonus_amount, Decimal("50.00"))

    def test_completed_hall_price_change_respects_the_taker_price_override(self):
        """`taker_price` 已设 ⇒ 两列都用**覆盖价**重算，发单价的改动对金额无影响。"""
        Performance.objects.filter(pk=self.perf.pk).update(
            taker_price=Decimal("70.00"),
            temp_amount=Decimal("17.50"),      # 70.00 × 25%
            bonus_amount=Decimal("7.77"),      # 陈旧值：重算后应变成 17.50
        )

        body = self._hall_price("200.00")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._order_row().hall_price, Decimal("200.00"))
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("17.50"))
        self.assertEqual(row.bonus_amount, Decimal("17.50"))

    def test_rolling_back_to_in_progress_leaves_bonus_alone(self):
        """反向对照：同一单改回「代练中」后再改发单价 ⇒ `bonus` **不动**、`temp` 照更新。"""
        rolled = self.client.post(
            f"/api/business/orders/{self.order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
            content_type="application/json",
        ).json()
        self.assertEqual(rolled["code"], 0, rolled)
        self.assertEqual(self._order_row().status, OrderStatus.IN_PROGRESS)

        body = self._hall_price("200.00")

        self.assertEqual(body["code"], 0, body)
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("50.00"))      # 200.00 × 25%
        self.assertEqual(row.bonus_amount, Decimal("25.00"))     # 非 COMPLETED ⇒ 不动


class CompletedAdjustAmountSyncTests(_CompletedAmountFixture):
    """`/adjust-amount/`：完成态反推基价之后 ⇒ 两列**一起**按新基价重算。"""

    def test_completed_adjust_amount_syncs_temp_and_bonus(self):
        body = self._adjust_amount({"amount_kind": "temp", "amount": "50.00"})

        self.assertEqual(body["code"], 0, body)
        # 反推基价 = 50.00 ÷ 25% = 200.00
        self.assertEqual(self._order_row().hall_price, Decimal("200.00"))
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("50.00"))
        self.assertEqual(row.bonus_amount, Decimal("50.00"))

    def test_non_completed_adjust_amount_leaves_bonus_alone(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)

        body = self._adjust_amount({"amount_kind": "temp", "amount": "50.00"})

        self.assertEqual(body["code"], 0, body)
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("50.00"))
        self.assertEqual(row.bonus_amount, Decimal("25.00"))


class CompletedAssignPriceSyncTests(_CompletedAmountFixture):
    """`/assign/` 带 `price` 的那一支也走**同一个**重算函数。

    ⚠️ `/assign/` 会先把订单改到 `IN_PROGRESS`（`apply_transition`），所以写价那一刻
    订单**已经是代练中** ⇒ 按 F1 的判据（只认**当前**状态）`bonus_amount` **不动**。
    这条用例钉的正是这件事：那条路径不能因为「参数上写着完成态」就一刀切地重算 bonus。
    """

    def test_assign_with_price_recomputes_temp_but_not_bonus(self):
        # ⚠️ 指派**同一个人**（避免 R-2b 之外的「换人 ⇒ 七列回退」把 `bonus_amount` 清 0，
        #    那是另一条既有语义、会把本条的反向对照搅浑）。
        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/assign/",
            {"user_id": self.taker.pk, "price": "200.00"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._order_row().hall_price, Decimal("200.00"))
        self.assertEqual(self._order_row().status, OrderStatus.IN_PROGRESS)
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("50.00"))      # 200.00 × 25%
        self.assertEqual(row.bonus_amount, Decimal("25.00"))     # 代练中 ⇒ 不动


class CompletedAdjustSyncTests(_CompletedAmountFixture):
    """legacy `/adjust/`（改 `taker_price`）也走**同一个**重算函数（裁定 A）。

    ⭐ 这是第 4 条改「有效基价」的路径。旧实现按 `perf.review_status is not None` 决定
    要不要同写 `bonus_amount`，而本夹具的 `review_status` 是 `None` ⇒ 完成态下
    `temp_amount` 变了、`bonus_amount` 停在旧值（违反 F1）。改判据后与另三条同源。
    """

    def test_completed_adjust_syncs_temp_and_bonus(self):
        body = self._adjust({"taker_price": "200.00"})

        self.assertEqual(body["code"], 0, body)
        row = self._perf_row()
        self.assertEqual(row.taker_price, Decimal("200.00"))
        self.assertEqual(row.temp_amount, Decimal("50.00"))      # 200.00 × 25%
        self.assertEqual(row.bonus_amount, Decimal("50.00"))     # 完成态 ⇒ 跟着走

    def test_non_completed_adjust_leaves_bonus_alone(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)

        body = self._adjust({"taker_price": "200.00"})

        self.assertEqual(body["code"], 0, body)
        row = self._perf_row()
        self.assertEqual(row.temp_amount, Decimal("50.00"))
        self.assertEqual(row.bonus_amount, Decimal("25.00"))     # 非完成态 ⇒ 不动


class SettledCompletedGateTests(_CompletedAmountFixture):
    """已结账 ⇒ 四条写价路径**各**一条「`1014` 且一行不写」。"""

    def _assert_refused_and_untouched(self, body, target, path):
        self.assertEqual(body["code"], 1014, (path, body))
        self.assertIn("已结账", body["msg"], (path, body))
        # 四条路径共用「请先撤销结账再改」的下一步提示（
        # legacy `/adjust/` 的旧句也补齐了这半句 ⇒ 四条同查这一句）。
        self.assertIn("撤销结账", body["msg"], (path, body))
        # 整行快照相等 = 「一行不写」；只看状态码会把「200 + 已经改了一半」放过
        self.assertEqual(self._full_snapshot(), target, path)
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists(),
            f"{path}：被拒的请求不该留痕",
        )

    def test_hall_price_is_refused(self):
        self._mark_settled()
        before = self._full_snapshot()

        body = self._hall_price("999.00")

        self._assert_refused_and_untouched(body, before, "hall-price")

    def test_assign_with_price_is_refused(self):
        other = User.objects.create_user(username="测试打手乙")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 本条的被指派者是
        #    「测试打手乙」（不是基线的 `self.taker`），也要授权——本条要考的才是 ② 已结账闸。
        grant_games(other, self.game)
        self._mark_settled()
        before = self._full_snapshot()

        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/assign/",
            {"user_id": other.pk, "price": "999.00"},
            content_type="application/json",
        ).json()

        self._assert_refused_and_untouched(body, before, "assign")

    def test_adjust_amount_is_refused(self):
        self._mark_settled()
        before = self._full_snapshot()

        body = self._adjust_amount({"amount_kind": "bonus", "amount": "999.00"})

        self._assert_refused_and_untouched(body, before, "adjust-amount")

    def test_adjust_is_refused(self):
        """legacy `/adjust/` 也必须过同一道已结账闸（裁定 A.2：`Performance.is_settled`）。

        ⚠️ 批 F 终审后统一（Minor 4）：它的文案也带上「请先撤销结账再改」⇒ 与另三条同查这句。
        """
        self._mark_settled()
        before = self._full_snapshot()

        body = self._adjust({"taker_price": "999.00"})

        self._assert_refused_and_untouched(body, before, "adjust")
