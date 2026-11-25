"""`/adjust/` 的两个新金额键 + 垃圾桶的归零语义（spec §5.3 / §5.7 / §8 第 6–9 条 + Z1–Z5）。

判据全部打在**数据库行**与 `OperationLog` 行上（不是响应体）；期望值写字面值。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from apps.business.settlement_amount import net_received, payable_net
from tests.p5_auth_helpers import make_logged_in_client

ZERO_REASON = "清空该接手绩效金额（订单列表-接手绩效弹层）"


class _AdjustFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="adj-amounts-cs",
            groups=("客服",),
            nodes=("order.submit_payout_price", "order.list"),
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260923000000000901",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        self.user = self._user("adj-amounts-taker")
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.user,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("100.00"),
            bonus_amount=Decimal("80.00"),
            deduct_mode="",
            deduct_value=Decimal("0.00"),
            review_status=ReviewStatus.PENDING,
        )

    def _user(self, username):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(username=username)

    def _adjust(self, payload):
        return self.client.post(
            f"/api/business/performances/{self.perf.id}/adjust/",
            payload,
            content_type="application/json",
        ).json()

    def _row(self):
        return Performance.objects.get(pk=self.perf.pk)


class DirectAmountWriteTests(_AdjustFixture):
    """spec §8 第 6、8 条：直写两列、各留一行痕、同值不留痕。"""

    def test_temp_amount_is_written_directly(self):
        body = self._adjust({"temp_amount": "66.00"})
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(row.temp_amount, Decimal("66.00"))
        self.assertEqual(row.bonus_amount, Decimal("80.00"))     # 另一列不动
        logs = OperationLog.objects.filter(action="performance.adjust", order_no=self.order.order_no)
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.field_name, "temp_amount")
        self.assertEqual(log.from_value, "100.00")
        self.assertEqual(log.to_value, "66.00")

    def test_bonus_amount_is_written_directly_and_does_not_touch_temp_amount(self):
        """⭐ **R3 改准（2026-09-28）**：显式 `bonus_amount` 只在**完成态**放行
        ⇒ 本用例把订单置为 `COMPLETED`（判据本身「直写 bonus、不动 temp、留一行痕」一字未改）。
        """
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        body = self._adjust({"bonus_amount": "5.00"})
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(row.bonus_amount, Decimal("5.00"))
        self.assertEqual(row.temp_amount, Decimal("100.00"))
        log = OperationLog.objects.get(action="performance.adjust", order_no=self.order.order_no)
        self.assertEqual(log.field_name, "bonus_amount")
        self.assertEqual(log.from_value, "80.00")
        self.assertEqual(log.to_value, "5.00")

    def test_the_same_value_writes_nothing_and_leaves_no_audit_row(self):
        body = self._adjust({"temp_amount": "100.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            OperationLog.objects.filter(action="performance.adjust").count(), 0
        )

    def test_a_null_amount_is_rejected_with_1000(self):
        for key in ("temp_amount", "bonus_amount"):
            with self.subTest(key=key):
                body = self._adjust({key: None})
                self.assertEqual(body["code"], 1000, body)
                row = self._row()
                self.assertEqual(row.temp_amount, Decimal("100.00"))
                self.assertEqual(row.bonus_amount, Decimal("80.00"))

    def test_an_empty_payload_still_has_no_side_effect(self):
        body = self._adjust({})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.filter(action="performance.adjust").count(), 0)

    def test_temp_amount_accepts_zero_and_negative_literally(self):
        """D-0015 / 计划「全局约束」第 2 条：这两个键**没有** `min_value`——0 与负数都合法。

        判据打在**数据库行**上、期望值写字面值。判别力（两版变异都真跑过，逐档如实）：

        - **负数那一档**由 `min_value` 变异杀死：给 `temp_amount` / `bonus_amount` 加
          `min_value=Decimal("0.00")` ⇒ 送 `"-12.34"` 回 `1000`、库不变 ⇒ 红
          （评审 `task-4-review.md` 的变异 ③：加回 `min_value` 原先**无人变红**）。
        - **0 那一档**由「判据改成真值」的变异杀死：`min_value=0.00` 是**闭**区间、放行 0，
          所以杀 0 的是 `if "temp_amount" in data:` → `if data.get("temp_amount"):`
          ⇒ 送 `"0.00"` 被当成「没送」、行不变 ⇒ 红（E-0021 的形状）。
        """
        for literal, expected in (("0.00", Decimal("0.00")), ("-12.34", Decimal("-12.34"))):
            with self.subTest(literal=literal):
                body = self._adjust({"temp_amount": literal})
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(self._row().temp_amount, expected)

    def test_bonus_amount_accepts_zero_and_negative_literally(self):
        """`bonus_amount` 的同一条判据（与上一条对称，两列各一条；期望值写字面值）。

        ⚠️ **R3 改准**：这一档只在**完成态**放行（非完成态 + 非 0 ⇒ `1014`）
        ⇒ 拆成两段：
        ① 非完成态 + `"0.00"` ⇒ **放行**（`0` ≠「没传」，E-0021）；非完成态 + `"-12.34"` ⇒ **拒**；
        ② 完成态 + `"-12.34"` ⇒ **放行**（D-0015：这两个键**没有** `min_value`，0 与负数都合法）。

        判别力：给 `bonus_amount` 加 `min_value=Decimal("0.00")` ⇒ 第 ② 段回 `1000`、库不变 ⇒ 红。
        """
        # ① 非完成态：0 放行、负数拒（R3）
        body = self._adjust({"bonus_amount": "0.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row().bonus_amount, Decimal("0.00"))

        body = self._adjust({"bonus_amount": "-12.34"})
        self.assertEqual(body["code"], 1014, body)
        self.assertEqual(
            body["msg"],
            "生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改",
        )
        self.assertEqual(self._row().bonus_amount, Decimal("0.00"))    # 一行都没写

        # ② 完成态：负数照旧放行（没有 `min_value`；D-0015）
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        body = self._adjust({"bonus_amount": "-12.34"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row().bonus_amount, Decimal("-12.34"))


class AdjustGateTests(_AdjustFixture):
    """spec §8 第 7 条：两道既有闸门对新键同样生效，且带阳性对照。"""

    def _settled(self):
        Performance.objects.filter(pk=self.perf.pk).update(
            settle_status=SettleStatus.SETTLED
        )

    def test_a_settled_row_rejects_all_three_keys_the_same_way(self):
        self._settled()
        for payload in (
            {"temp_amount": "1.00"},
            {"bonus_amount": "1.00"},
            {"deduct_value": "1.00"},      # 阳性对照：不是新键特有
        ):
            with self.subTest(payload=payload):
                body = self._adjust(payload)
                self.assertEqual(body["code"], 1014, body)
                self.assertIn("已结账的绩效不能改金额", body["msg"])
                # spec §8 的 Z5① 逐字要求「`code == 1014` **且两列仍是旧值**」：
                # 闸门在所有内存改动与写库之前，所以这里断库不变是有意义的判据。
                row = self._row()
                self.assertEqual(row.temp_amount, Decimal("100.00"))
                self.assertEqual(row.bonus_amount, Decimal("80.00"))

    def test_a_row_without_a_taker_rejects_the_new_keys_too(self):
        # ⚠️ `review_status` 必须一起清成 NULL：`performance_review_needs_user`
        #    这条 `CheckConstraint`（`models.py`）不允许「没有归属人却有审核状态」
        #    ⇒ 只改 `user=None` 会直接撞约束（IntegrityError），测不到本闸门。
        Performance.objects.filter(pk=self.perf.pk).update(
            user=None, review_status=None
        )
        body = self._adjust({"temp_amount": "1.00"})
        self.assertEqual(body["code"], 1014, body)
        self.assertIn("该绩效还没有接单人", body["msg"])


class DeductCeilingTests(_AdjustFixture):
    """spec §8 第 9 条：改小 `temp_amount` 撞固定额扣点上界 ⇒ 业务码且一行都不写。"""

    def test_shrinking_temp_amount_below_a_fixed_deduction_is_refused(self):
        Performance.objects.filter(pk=self.perf.pk).update(
            deduct_mode="1", deduct_value=Decimal("80.00")
        )
        body = self._adjust({"temp_amount": "50.00"})
        self.assertNotEqual(body["code"], 0)
        self.assertIn("扣点金额不能超过预计绩效总计", body["msg"])
        self.assertEqual(self._row().temp_amount, Decimal("100.00"))    # 库不变

    def test_shrinking_it_to_exactly_the_deduction_is_allowed(self):
        Performance.objects.filter(pk=self.perf.pk).update(
            deduct_mode="1", deduct_value=Decimal("80.00")
        )
        body = self._adjust({"temp_amount": "80.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row().temp_amount, Decimal("80.00"))


class ZeroingTests(_AdjustFixture):
    """Z1 / Z2 / Z3：垃圾桶＝两列归零、恰两行留痕、**不建** `PerformanceAdjustment`。"""

    def setUp(self):
        super().setUp()
        self.perf.taker_price = Decimal("55.00")
        self.perf.deduct_mode = "0"
        self.perf.deduct_value = Decimal("10.00")
        self.perf.hours = 24
        self.perf.project = "三角洲"
        self.perf.save(
            update_fields=["taker_price", "deduct_mode", "deduct_value", "hours", "project"]
        )
        self.before_adjustments = PerformanceAdjustment.objects.filter(
            performance=self.perf
        ).count()

    def _zero(self):
        return self._adjust(
            {"temp_amount": "0.00", "bonus_amount": "0.00", "reason": ZERO_REASON}
        )

    def test_both_amounts_are_zeroed_and_everything_else_is_untouched(self):
        body = self._zero()
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(row.temp_amount, Decimal("0.00"))
        self.assertEqual(row.bonus_amount, Decimal("0.00"))
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.hours, 24)
        self.assertEqual(row.project, "三角洲")
        self.assertEqual(row.taker_price, Decimal("55.00"))
        self.assertEqual(row.deduct_mode, "0")
        self.assertEqual(row.deduct_value, Decimal("10.00"))

    def test_exactly_two_audit_rows_carry_the_fixed_reason(self):
        self._zero()
        logs = OperationLog.objects.filter(
            action="performance.adjust", order_no=self.order.order_no
        )
        self.assertEqual(logs.count(), 2)
        self.assertEqual(
            sorted(logs.values_list("field_name", flat=True)),
            ["bonus_amount", "temp_amount"],
        )
        for log in logs:
            self.assertEqual(log.reason, ZERO_REASON)
            self.assertEqual(log.to_value, "0.00")
        self.assertEqual(
            logs.get(field_name="temp_amount").from_value, "100.00"
        )
        self.assertEqual(
            logs.get(field_name="bonus_amount").from_value, "80.00"
        )

    def test_no_performance_adjustment_row_is_created(self):
        """Z3：`/adjust/` 不建调整记录——建了会与 `payable_net` 里的 `bonus_amount` 二次扣减。"""
        self._zero()
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=self.perf).count(),
            self.before_adjustments,
        )


class ZeroingDownstreamTests(_AdjustFixture):
    """Z4：归零后 `payable_net` 只剩 `− 扣点 + Σ未结账调整`；订单列表的实收款**不受影响**。"""

    def test_payable_net_drops_to_zero_and_net_received_is_unchanged(self):
        before = net_received(self.order)
        body = self._adjust(
            {"temp_amount": "0.00", "bonus_amount": "0.00", "reason": ZERO_REASON}
        )
        self.assertEqual(body["code"], 0, body)
        row = self._row()
        self.assertEqual(payable_net(row, []), Decimal("0.00"))
        self.order.refresh_from_db()
        self.assertEqual(net_received(self.order), before)
        self.assertEqual(net_received(self.order), Decimal("200.00"))
