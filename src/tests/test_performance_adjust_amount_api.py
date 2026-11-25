"""`POST /api/business/performances/<id>/adjust-amount/`（spec §4.1 / §4.6；D-0147 批 E 任务 2）。

判据 = spec §6.1 的 **T7–T10** 加各自点名的边界，**主要**打在**数据库行**与 `OperationLog`
行上；另有**一条**用例专断响应体（`test_the_response_carries_the_fresh_row`）。期望值写字面值
（E-0009 / E-0024）。

四条的骨架：

- **T7** `amount_kind="temp"`：填 V ⇒ 有效基价 = V/提成率（写对了列）、`temp_amount` 重算、
  `bonus_amount` 不动、订单状态不变；
- **T8** `amount_kind="bonus"`：填 V ⇒ `bonus_amount == V`，基价与 `temp_amount` 按 §4.1 同步。
  ⭐ **R3（编排方裁定）**：这一档**只在订单当前状态 == `COMPLETED`** 时放行；
  非完成态 + `amount != 0` ⇒ 拒**整笔**（`1014` + 逐字文案），`0` 放行（归零）；
- **T9** 已结账行 ⇒ `1014` 且一行不写（四列全不动）；
- **T10** 该行已有 `taker_price` 覆盖值 ⇒ 写的是 `taker_price`、不是 `order.hall_price`。

⚠️ 提成率夹具取 **25%**（`ORDER_PERCENT`）而不是默认回落 100%：回落档下
「反推基价 = V」与「直接写 V」不可区分，判据会失去判别力。
⚠️ **R3 文案逐字**（⛔ 不 import 生产常量，E-0024）：
`生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改`
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

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
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

User = get_user_model()

#: 夹具的提成率（百分数）。25% 让「V ÷ rate × 100」与「V」明显不同。
RATE = Decimal("25.00")
#: `ORDER_PERCENT`(=20) 是打手路径声明的口径，档案模式必须与它一致才可算。
PERCENT_MODE = CommissionMode.ORDER_PERCENT

#: `/adjust-amount/` 的节点门与既有 `/adjust/` 同门（spec §4.6）。
ADJUST_AMOUNT_NODES = ("order.submit_payout_price", "order.list")


class _AdjustAmountFixture(TenantTestCase):
    """共享夹具：一条 `IN_PROGRESS` 单 + 有 25% 提成的接单人 + 一条 MAIN 绩效。

    ⚠️ `bonus_amount` 刻意给 **7.77**（不是 0）：这样「`temp` 档不动 `bonus`」这条断言
    才有判别力——若初始是 0，「实现把它写成 0」也会通过。
    """

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260924000000000701",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        self.taker = User.objects.create_user(username="测试打手甲")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=PERCENT_MODE, commission_value=RATE
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 「三条写路径同一码」那类用例要走 /assign/，打手要持本类的游戏。
        grant_games(self.taker, self.game)
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),      # 100.00 × 25%
            bonus_amount=Decimal("7.77"),
        )
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=ADJUST_AMOUNT_NODES
        )
        #: R-4b 的 flag 要挂在**已登录那个账号**上（`grant_nodes` 的造法照抄
        #: `tests/test_adjust_api.py` 的 `self.cs`）；夹具只返回 client，所以按用户名取回来。
        self.cs = User.objects.get(username="测试客服甲")
        self.url = f"/api/business/performances/{self.perf.id}/adjust-amount/"

    def _post(self, payload):
        return self.client.post(
            self.url, payload, content_type="application/json"
        ).json()

    def _row(self):
        return Performance.objects.get(pk=self.perf.pk)

    def _order(self):
        return Order.objects.get(pk=self.order.pk)

    def _audit_fields(self):
        return sorted(
            OperationLog.objects.filter(
                action="performance.adjust", order_no=self.order.order_no
            ).values_list("field_name", flat=True)
        )


class TempKindTests(_AdjustAmountFixture):
    """T7：`amount_kind="temp"`。"""

    def test_the_order_row_is_locked_before_the_performance_row(self):
        """⭐ I4（终审修复轮）：`/adjust-amount/` 统一锁序 —— 先锁 Order、再锁 Performance。

        `apply_amount_to_effective_base` 会写 `Order.hall_price`，订单行锁原先要等那次
        `save()` 才拿到 ⇒ 与 `/assign/`（先锁 Order）方向相反。判据是**SQL 顺序**。
        判别力：把 `adjust_amount` 里的 `lock_order_row(perf.order)` 删掉 ⇒ 本用例红。
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.lock_order_helpers import assert_order_locked_before_performance

        with CaptureQueriesContext(connection) as captured:
            body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 0, body)
        assert_order_locked_before_performance(self, captured)
        # 哨兵：金额真的改了（不是在一笔失败的调用上断锁序）。
        self.assertEqual(self._order().hall_price, Decimal("200.00"))

    def test_temp_kind_writes_the_effective_base_and_recomputes_temp(self):
        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 0, body)
        # 反推基价 = 50.00 ÷ 25% = 200.00；本行没有 taker_price 覆盖 ⇒ 写 order.hall_price
        self.assertEqual(self._order().hall_price, Decimal("200.00"))
        row = self._row()
        self.assertIsNone(row.taker_price)
        self.assertEqual(row.temp_amount, Decimal("50.00"))     # 200.00 × 25%
        self.assertEqual(row.bonus_amount, Decimal("7.77"))     # temp 档不动 bonus
        self.assertEqual(self._order().status, OrderStatus.IN_PROGRESS)
        # 审计：恰好两行真实被改的列（hall_price / temp_amount），没有 bonus_amount
        self.assertEqual(self._audit_fields(), ["hall_price", "temp_amount"])

    def test_the_audit_rows_carry_the_literal_before_and_after_values(self):
        self._post({"amount_kind": "temp", "amount": "50.00", "reason": "客户议价"})
        logs = OperationLog.objects.filter(
            action="performance.adjust", order_no=self.order.order_no
        )
        hall = logs.get(field_name="hall_price")
        self.assertEqual(hall.from_value, "100.00")
        self.assertEqual(hall.to_value, "200.00")
        self.assertEqual(hall.reason, "客户议价")
        self.assertEqual(hall.target_name, "测试打手甲")
        temp = logs.get(field_name="temp_amount")
        self.assertEqual(temp.from_value, "25.00")
        self.assertEqual(temp.to_value, "50.00")

    def test_the_response_carries_the_fresh_row(self):
        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Decimal(body["data"]["temp_amount"]), Decimal("50.00"))
        self.assertEqual(body["data"]["order_no"], self.order.order_no)


class BonusKindTests(_AdjustAmountFixture):
    """T8：`amount_kind="bonus"`（⭐ R3 之后**只在完成态**可走这一档）。"""

    def test_a_completed_order_writes_bonus_and_syncs_the_base_and_temp(self):
        """完成态 + `amount_kind="bonus"` ⇒ 写 `bonus`、反推基价、重算 `temp`。

        ⚠️ **R3 夹具改准**：本用例原先把订单留在 `IN_PROGRESS`（非完成态），
        而 R3 之后那一档**必须被拒** ⇒ 把夹具置为 `COMPLETED`、用例名 / docstring 写明「完成态」。
        判据本身（三个列的字面值 + 三行留痕）一个字没放宽。
        """
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        body = self._post({"amount_kind": "bonus", "amount": "30.00"})
        self.assertEqual(body["code"], 0, body)
        # 反推基价 = 30.00 ÷ 25% = 120.00
        self.assertEqual(self._order().hall_price, Decimal("120.00"))
        row = self._row()
        self.assertEqual(row.bonus_amount, Decimal("30.00"))    # 用户填的原值
        self.assertEqual(row.temp_amount, Decimal("30.00"))     # 120.00 × 25%
        self.assertEqual(
            self._audit_fields(), ["bonus_amount", "hall_price", "temp_amount"]
        )

    def test_a_completed_order_keeps_the_user_supplied_bonus(self):
        """**F1（spec §2.1）+ 裁定 B4**：完成态下的 `amount_kind="bonus"`。

        完成态里 `apply_amount_to_effective_base` 会先把 `bonus_amount` 按新基价重算，
        随后本端点的显式 `bonus_amount = V` 覆盖它 ⇒ 最终值 = **用户填的 V**。
        夹具取**舍入边界**：`V = 100.00`、率 `99.99%` ⇒ 反推基价 = 100.01
        （`100.00 ÷ 99.99% = 100.010001…` 量化到分），而 `100.01 × 99.99% = 99.999999`
        也量化回 `100.00` ⇒ 「= V」与「= 基价×率」在实现里**逐分相等**、不可区分；
        按实现如实断言为「= V」，并把被舍入过的基价一列一起钉住。
        ⛔ 换个率也分不开：量化误差 ≤ 半分 × 率 ÷ 100，而率 ≤ 100 ⇒
        `round(round(V×100/率)×率/100) == V` 恒成立（本用例取这一个边界只为让
        「基价那一列**确实被舍入**（100.00 → 100.01）」也落在判据里）。
        """
        StaffProfile.objects.filter(user=self.taker).update(
            commission_value=Decimal("99.99")
        )
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)

        body = self._post({"amount_kind": "bonus", "amount": "100.00"})

        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(self._order().status, OrderStatus.COMPLETED)
        self.assertEqual(self._order().hall_price, Decimal("100.01"))   # 舍入后的基价
        self.assertEqual(row.bonus_amount, Decimal("100.00"))           # 用户填的 V
        self.assertEqual(row.temp_amount, Decimal("100.00"))            # 100.01 × 99.99%
        self.assertEqual(
            self._audit_fields(), ["bonus_amount", "hall_price", "temp_amount"]
        )


class BonusOnlyOnCompletedTests(_AdjustAmountFixture):
    """⭐ **R3**（spec §4.2.2 / §7.3 #26、#29）：非完成态 ⇒ 非 0 的 `amount_kind="bonus"` 拒**整笔**。

    夹具那条订单一律是 `IN_PROGRESS`（`_AdjustAmountFixture.setUp`）——正是要被拒的那一档。
    """

    #: R3 文案逐字（⛔ 不 import 生产常量：自指断言在被断言的常量被改坏时不红，E-0024）。
    R3_MSG = "生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改"

    def test_a_non_completed_order_refuses_a_non_zero_bonus_kind(self):
        """⭐ R3：非完成态 + `amount != 0` ⇒ 拒**整笔**，四个列全不变、无留痕。

        判别力：只跳过 `bonus` 那一写、仍跑 `apply_amount_to_effective_base`
        ⇒ `hall_price` / `taker_price` 会变 ⇒ 本条红。
        """
        before = (self._order().hall_price, self._row().taker_price,
                  self._row().temp_amount, self._row().bonus_amount)
        body = self._post({"amount_kind": "bonus", "amount": "30.00"})
        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID, body)
        self.assertEqual(int(body["code"]), 1014)
        self.assertEqual(body["msg"], self.R3_MSG)
        self.assertEqual(
            (self._order().hall_price, self._row().taker_price,
             self._row().temp_amount, self._row().bonus_amount), before,
        )
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no,
                                        action="performance.adjust").exists()
        )

    def test_a_taker_price_override_alone_does_not_change_the_verdict(self):
        """R3 的判据只看订单状态，与「有效基价落在哪一列」无关：有 `taker_price` 覆盖值同样拒。"""
        self.perf.taker_price = Decimal("80.00")
        self.perf.save(update_fields=["taker_price"])
        body = self._post({"amount_kind": "bonus", "amount": "30.00"})
        self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID, body)
        self.assertEqual(body["msg"], self.R3_MSG)
        row = self._row()
        self.assertEqual(row.taker_price, Decimal("80.00"))
        self.assertEqual(row.temp_amount, Decimal("25.00"))
        self.assertEqual(row.bonus_amount, Decimal("7.77"))

    def test_a_completed_order_still_accepts_a_bonus_kind(self):
        """反向对照（防「一刀切全拒」恒真）：`COMPLETED` + 未结账 + `amount_kind="bonus"` ⇒ 放行。

        判别力：把 R3 写成「不看订单状态一律拒」⇒ 本条红。
        """
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        body = self._post({"amount_kind": "bonus", "amount": "30.00"})
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(row.bonus_amount, Decimal("30.00"))
        self.assertEqual(row.temp_amount, Decimal("30.00"))

    def test_the_zero_boundary_is_allowed_and_a_cent_is_refused(self):
        """⭐ R3 的 `0` 边界（E-0021：判据用**值比**，⛔ 不用真值 `if amount:`）。

        非完成态 + `"0.01"` ⇒ **拒绝**（防「`0` 被当成假值全放行」）；
        非完成态 + `"0.00"` ⇒ ⭐ **放行**（归零）。

        ⚠️ 如实登记 `0` 走既有路径的已知语义（spec §8-13）：它会把有效基价反推成 `0`
        （连带改发单价 / 接单价）并重算 `temp_amount` —— **既有行为、本线不改**。
        """
        with self.subTest(amount="0.01"):
            body = self._post({"amount_kind": "bonus", "amount": "0.01"})
            self.assertEqual(body["code"], ErrorCode.PERFORMANCE_STATE_INVALID, body)
            self.assertEqual(body["msg"], self.R3_MSG)
            self.assertEqual(self._order().hall_price, Decimal("100.00"))
            row = self._row()
            self.assertEqual(row.temp_amount, Decimal("25.00"))
            self.assertEqual(row.bonus_amount, Decimal("7.77"))
            self.assertFalse(
                OperationLog.objects.filter(order_no=self.order.order_no).exists()
            )

        with self.subTest(amount="0.00"):
            body = self._post({"amount_kind": "bonus", "amount": "0.00"})
            self.assertEqual(body["code"], 0, body)
            order = self._order()
            row = self._row()
            # 归零：基价被反推成 0、`temp` 重算成 0、`bonus` 显式写成 0（既有路径 + R3 放行）
            self.assertEqual(order.hall_price, Decimal("0.00"))
            self.assertEqual(row.temp_amount, Decimal("0.00"))
            self.assertEqual(row.bonus_amount, Decimal("0.00"))


class SettledGateTests(_AdjustAmountFixture):
    """T9：已结账 ⇒ `1014`，且一行都不写。"""

    def test_a_settled_row_is_refused_and_nothing_is_written(self):
        self.perf.taker_price = Decimal("55.00")
        self.perf.save(update_fields=["taker_price"])
        Performance.objects.filter(pk=self.perf.pk).update(
            settle_status=SettleStatus.SETTLED
        )
        body = self._post({"amount_kind": "bonus", "amount": "30.00"})
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("已结账的绩效不能改金额", body["msg"])
        row = self._row()
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        self.assertEqual(row.taker_price, Decimal("55.00"))
        self.assertEqual(row.temp_amount, Decimal("25.00"))
        self.assertEqual(row.bonus_amount, Decimal("7.77"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )

    def test_the_other_two_settled_columns_alone_are_also_refused(self):
        """**R-10b**：判据必须是**三字段并集**，不是只看 `settle_status` 一列。

        下面两档都让 `settle_status` 保持「未结账」，只在 `settled_at`（存量行里
        P4-FE-0 之前结的账）或 `settled_amount` 上留值 ⇒ 只看状态列的实现在这里会
        放行并**真的改钱**（`hall_price` / `temp_amount` 被写），本用例因此变红。
        """
        for field, value in (
            ("settled_at", timezone.now()),
            ("settled_amount", Decimal("7.77")),
        ):
            with self.subTest(settled_field=field):
                Performance.objects.filter(pk=self.perf.pk).update(
                    settle_status=SettleStatus.UNSETTLED,
                    settled_at=None,
                    settled_amount=None,
                )
                Performance.objects.filter(pk=self.perf.pk).update(**{field: value})

                body = self._post({"amount_kind": "bonus", "amount": "30.00"})

                self.assertEqual(body["code"], 1014, body)
                self.assertIn("已结账的绩效不能改金额", body["msg"])
                row = self._row()
                self.assertEqual(self._order().hall_price, Decimal("100.00"))
                self.assertEqual(row.temp_amount, Decimal("25.00"))
                self.assertEqual(row.bonus_amount, Decimal("7.77"))
                self.assertFalse(
                    OperationLog.objects.filter(action="performance.adjust").exists()
                )


class TakerPriceOverrideTests(_AdjustAmountFixture):
    """T10：已有 `taker_price` 覆盖值时，写的是覆盖列。"""

    def test_an_existing_taker_price_override_is_the_column_written(self):
        self.perf.taker_price = Decimal("80.00")
        self.perf.save(update_fields=["taker_price"])
        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(row.taker_price, Decimal("200.00"))            # 50 ÷ 25%
        self.assertEqual(self._order().hall_price, Decimal("100.00"))   # 订单发单价不动
        self.assertEqual(row.temp_amount, Decimal("50.00"))
        self.assertEqual(self._audit_fields(), ["taker_price", "temp_amount"])


class AmountBoundaryTests(_AdjustAmountFixture):
    """D-0015：金额 0 与负数都合法；非法载荷回 `1000`。"""

    def test_amount_zero_and_negative_are_accepted_literally(self):
        for literal, expected_base, expected_temp in (
            ("0.00", Decimal("0.00"), Decimal("0.00")),
            ("-12.34", Decimal("-49.36"), Decimal("-12.34")),
        ):
            with self.subTest(literal=literal):
                body = self._post({"amount_kind": "temp", "amount": literal})
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(self._order().hall_price, expected_base)
                self.assertEqual(self._row().temp_amount, expected_temp)

    def test_a_non_decimal_amount_is_1000_and_writes_nothing(self):
        for bad in ("abc", "", "1.005"):
            with self.subTest(amount=bad):
                body = self._post({"amount_kind": "temp", "amount": bad})
                self.assertEqual(body["code"], 1000, body)
                self.assertEqual(self._order().hall_price, Decimal("100.00"))
                self.assertEqual(self._row().temp_amount, Decimal("25.00"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )

    def test_an_illegal_amount_kind_is_1000_and_writes_nothing(self):
        for bad in ("TEMP", "salary", ""):
            with self.subTest(amount_kind=bad):
                body = self._post({"amount_kind": bad, "amount": "50.00"})
                self.assertEqual(body["code"], 1000, body)
                self.assertEqual(self._order().hall_price, Decimal("100.00"))

    def test_the_payload_keys_are_required(self):
        for payload in ({}, {"amount_kind": "temp"}, {"amount": "50.00"}):
            with self.subTest(payload=payload):
                self.assertEqual(self._post(payload)["code"], 1000)


class UncomputableCommissionTests(_AdjustAmountFixture):
    """提成模式算不出（`FIXED`）⇒ 业务码，且一行不写。

    期望码取**既有 `/adjust/` 那条「算不出」支的码**（`CommissionError.code`，
    即 `ErrorCode.VALIDATION` = 1000）：本端点与该支同形（spec §4.1 第 2 步
    「回业务码（不猜）」；`tests/test_adjust_api.py` 的
    `test_uncomputable_commission_mode_is_a_business_code` 钉的是同一个值）。
    """

    def test_a_fixed_commission_mode_is_a_business_code_and_writes_nothing(self):
        profile = StaffProfile.objects.get(user=self.taker)
        profile.commission_mode = CommissionMode.FIXED
        profile.save(update_fields=["commission_mode"])
        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        row = self._row()
        self.assertEqual(row.temp_amount, Decimal("25.00"))
        self.assertEqual(row.bonus_amount, Decimal("7.77"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )

    def test_the_fixed_mode_code_equals_the_sibling_write_paths(self):
        """**R-4a**：同一异常（提成模式算不出）在三条写路径上必须是**同一个码**。

        ⛔ 不抄字面量（自指断言在被断言的常量被改坏时不会红，E-0024）：同时打本端点、
        既有 `/adjust/`、既有 `/assign/`，**当场断言三码相等**——一致性由用例自证。
        ⚠️ 另钉一条「不等于 `OK`」：否则三条路都返回 0 时上面那组相等断言会**恒真**
        （它就不再是判据，只是一句好听的话）。
        """
        profile = StaffProfile.objects.get(user=self.taker)
        profile.commission_mode = CommissionMode.FIXED
        profile.save(update_fields=["commission_mode"])

        mine = self._post({"amount_kind": "temp", "amount": "50.00"})["code"]
        sibling_adjust = self.client.post(
            f"/api/business/performances/{self.perf.id}/adjust/",
            {"taker_price": "66.00"},
            content_type="application/json",
        ).json()["code"]
        sibling_assign = self.client.post(
            f"/api/business/orders/{self.order.order_no}/assign/",
            {"user_id": self.taker.id},
            content_type="application/json",
        ).json()["code"]

        self.assertNotEqual(mine, ErrorCode.OK)
        self.assertEqual(mine, sibling_adjust)
        self.assertEqual(mine, sibling_assign)


class RestrictEditTakerPriceFlagTests(_AdjustAmountFixture):
    """**R-4b**：`order.restrict_edit_taker_price` 命中时，按「有效基价落在哪一列」分两档。

    该行**已有** `taker_price` ⇒ 落点正是 flag 保护的那一列 ⇒ HTTP 403 且一行不写；
    该行**没有** `taker_price` ⇒ 落点是 `Order.hall_price`（不受这条 flag 保护）⇒ 照常放行。

    ⚠️ 开 flag 的造法**照抄**既有 `/adjust/` 的用例（`grant_nodes(self.cs, "order.restrict_edit_taker_price")`
    ——`tests/test_adjust_api.py::test_the_restrict_flag_denies_the_override`）。
    """

    def test_the_flag_refuses_when_the_effective_base_is_the_protected_column(self):
        grant_nodes(self.cs, "order.restrict_edit_taker_price")
        self.perf.taker_price = Decimal("80.00")
        self.perf.save(update_fields=["taker_price"])

        response = self.client.post(
            self.url, {"amount_kind": "temp", "amount": "50.00"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertIn("你没有改接单价的权限", response.json()["msg"])
        # 一行不写：覆盖价、预计绩效、订单发单价三处都不动，也没有留痕。
        row = self._row()
        self.assertEqual(row.taker_price, Decimal("80.00"))
        self.assertEqual(row.temp_amount, Decimal("25.00"))
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )

    def test_the_flag_allows_the_order_base_column_and_recomputes_temp(self):
        grant_nodes(self.cs, "order.restrict_edit_taker_price")
        self.assertIsNone(self._row().taker_price)

        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 0, body)
        # 反推基价 = 50.00 ÷ 25% = 200.00，落 `Order.hall_price`（不是被保护的那一列）。
        self.assertEqual(self._order().hall_price, Decimal("200.00"))
        self.assertEqual(self._row().temp_amount, Decimal("50.00"))


class DerivedBaseRangeTests(_AdjustAmountFixture):
    """**R-10c**：反推出来的 `base` 超出目标列位宽 ⇒ **业务码**（⛔ 不是 500），一行不写。

    `amount` 的入口闸（`_ADJUST_AMOUNT_GATE`）只保证 **V** 自己 ≤ 12 位（10 位整数），
    而写库的是 `base = V ÷ 提成率 × 100`——低提成率会把 V 放大若干个量级 ⇒ 直接写库时
    PostgreSQL 抛 `DataError`（HTTP 500）。本类两条一正一反：
    超界必须被业务码拦下，**边界内**必须照旧成功（防「一刀切拒绝」恒真）。
    """

    def test_a_derived_base_beyond_the_column_is_a_business_code(self):
        # 25% 档下 V 取入口闸允许的最大值 ⇒ base = 9999999999.99 × 4 = 39999999999.96
        #（11 位整数 ⇒ 超出 `numeric(12,2)`）。
        body = self._post({"amount_kind": "temp", "amount": "9999999999.99"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        row = self._row()
        self.assertEqual(row.temp_amount, Decimal("25.00"))
        self.assertEqual(row.bonus_amount, Decimal("7.77"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )

    def test_the_largest_base_that_still_fits_is_accepted(self):
        """边界内对照：base 恰好 10 位整数 ⇒ 成功（`9999999999.96`）。"""
        body = self._post({"amount_kind": "temp", "amount": "2499999999.99"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._order().hall_price, Decimal("9999999999.96"))
        self.assertEqual(self._row().temp_amount, Decimal("2499999999.99"))


class NoTakerGateTests(_AdjustAmountFixture):
    """该行没有归属人 ⇒ 算不出提成率 ⇒ `1014`（沿用既有「还没有接单人」那支的码）。"""

    def test_a_row_without_a_taker_is_refused_with_1014(self):
        Performance.objects.filter(pk=self.perf.pk).update(user=None)
        body = self._post({"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("该绩效还没有接单人", body["msg"])
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        self.assertEqual(self._row().temp_amount, Decimal("25.00"))


class PermissionTests(_AdjustAmountFixture):
    """门与既有 `/adjust/` 同门 `order.submit_payout_price`。"""

    def test_without_the_node_the_request_is_refused_and_nothing_is_written(self):
        # ⚠️ 必须换一个**独立的组**（「客服」那个 `Role` 已被 setUp 的夹具挂上了
        #    `order.submit_payout_price`——节点挂在组上，同组的新账号会**继承**它）。
        client = make_logged_in_client(
            username="测试客服乙", groups=("财务",), nodes=("order.list",)
        )
        response = client.post(
            self.url, {"amount_kind": "temp", "amount": "50.00"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(self._order().hall_price, Decimal("100.00"))
        self.assertFalse(
            OperationLog.objects.filter(action="performance.adjust").exists()
        )


# ── 跨租户（AGENTS §4 / §5-G；判据用租户内唯一且两租户取值不同的业务值）────────
SCHEMA_B = "tp2_adj_amt_b"
DOMAIN_B = "tp2-adj-amt-b.test.local"

#: 订单号在**两个租户里都出现**才谈得上「用业务值指认」（主键各自从 1 开始，指认不了）。
NO_A = "20260924000000000711"
NO_B_DECOY = "20260924000000000712"
NO_B = "20260924000000000713"
USER_A = "测试打手甲租户A"
USER_B = "测试打手乙租户B"


def _build_perf(order_no, username, game_name):
    """在当前 schema 里建一条「有 25% 提成的 MAIN 绩效」（A / B 两侧共用同一形状）。"""
    game = Game.objects.create(name=game_name)
    order = Order.objects.create(
        game=game,
        order_no=order_no,
        status=OrderStatus.IN_PROGRESS,
        total_price=Decimal("200.00"),
        hall_price=Decimal("100.00"),
    )
    user = User.objects.create_user(username=username)
    StaffProfile.objects.create(
        user=user, commission_mode=PERCENT_MODE, commission_value=RATE
    )
    return Performance.objects.create(
        order=order,
        user=user,
        emp_type=EmpType.MAIN,
        temp_amount=Decimal("25.00"),
        bonus_amount=Decimal("7.77"),
    )


class CrossTenantAdjustAmountTests(TenantTestCase):
    """以租户 A 的登录态打「B 租户那一行」的 id ⇒ 拒绝，且 B 的行一个字不动。

    ⚠️ **不拿主键当判据**（`AGENTS.md` §5-G）：两个 schema 的主键序列各自从 1 开始。
    这里让 B 的目标行主键（第 2 条 ⇒ 2）**刻意不等于** A 侧唯一那条行（主键 1），
    这样这条请求在 A 侧真的落空；而「B 的行有没有被改」一律按**订单号**查证。

    阳性对照在前：先用 A 的凭据改 A 自己的行成功，再打 B 的 id。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="调整金额隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant_a)
        # A 侧先建游戏，再建 client（夹具默认给「当前租户已有的全部游戏」）。
        self.perf_a = _build_perf(NO_A, USER_A, "测试游戏A")
        self.client_a = make_logged_in_client(
            username="测试客服A", groups=("客服",), nodes=ADJUST_AMOUNT_NODES
        )

    def _post_a(self, perf_id, payload):
        return self.client_a.post(
            f"/api/business/performances/{perf_id}/adjust-amount/",
            payload,
            content_type="application/json",
        ).json()

    def test_b_row_id_from_a_is_refused_and_b_is_untouched(self):
        # B 侧造**两条**：目标那条主键为 2（A 侧只有主键 1 的一条 ⇒ 目标 id 在 A 里不存在）。
        with tenant_context(self.tenant_b):
            _build_perf(NO_B_DECOY, "测试打手乙租户B备", "测试游戏B备")
            perf_b = _build_perf(NO_B, USER_B, "测试游戏B")
            b_target_id = perf_b.id
            self.assertEqual(b_target_id, 2)     # 自证判据的前提：目标 id≠A 侧那条的 id
        connection.set_tenant(self.tenant_a)

        # ── 阳性对照：A 改**自己**那条成功 ──
        positive = self._post_a(self.perf_a.id, {"amount_kind": "temp", "amount": "50.00"})
        self.assertEqual(positive["code"], 0, positive)
        with tenant_context(self.tenant_a):
            self.assertEqual(
                Order.objects.get(order_no=NO_A).hall_price, Decimal("200.00")
            )
        connection.set_tenant(self.tenant_a)

        # ── 判据：拿 B 那条的 id 去 A 打 ⇒ 拒绝，且 A 自己的行不再变 ──
        body = self._post_a(b_target_id, {"amount_kind": "temp", "amount": "60.00"})
        self.assertEqual(body["code"], 1002, body)
        with tenant_context(self.tenant_a):
            self.assertEqual(
                Order.objects.get(order_no=NO_A).hall_price, Decimal("200.00")
            )
            self.assertFalse(
                OperationLog.objects.filter(
                    action="performance.adjust", order_no=NO_B
                ).exists()
            )
        connection.set_tenant(self.tenant_a)

        # ── B 的行按**订单号**查证：四列都没动（主键指认不了另一租户的行）──
        with tenant_context(self.tenant_b):
            row = Performance.objects.get(order__order_no=NO_B)
            self.assertEqual(row.user.username, USER_B)
            self.assertEqual(row.order.hall_price, Decimal("100.00"))
            self.assertEqual(row.temp_amount, Decimal("25.00"))
            self.assertEqual(row.bonus_amount, Decimal("7.77"))
            self.assertFalse(
                OperationLog.objects.filter(
                    action="performance.adjust", order_no=NO_B
                ).exists()
            )
        connection.set_tenant(self.tenant_a)
