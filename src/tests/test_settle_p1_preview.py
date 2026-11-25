"""P1-2：结算预览与结账**同口径**（计划 `docs/plans/2026-10-22-settlement-p1-and-peer.md` §3）。

## 这条判据在钉什么

预览弹窗上的「实际需结总额」（`bonus_total`）与「结算对象」的金额，必须**只**包含
`/settle` 那一趟真的会结掉的钱。结账侧的可结集合是**三分**（`settlement.settle_performances`
第 2–3 步）：

1. `rows` 腿：未结账 ∧ 订单已完成 ∧ 有归属人 ∧ 归属人未被冻结 ∧ 不在已软删单/行上；
2. `adjustments` 腿（**坏账出口**）：该行名下有未结账调整记录，且「行已结账 **或** 订单已完成」、
   订单不在池内两态、归属人未被冻结；
3. 「已完全结清」⇒ **跳过**（成功但一分钱不结）；其余不合格 ⇒ 整批 `1006`。

⇒ 预览若只按「可见行」求和，就会把**永远结不掉**（或这次不会被结）的钱算进总额——
冻结归属人 / 池内订单 / 已完全结清 / 无归属人这四类都是这个形态。
本文件钉的是**数字**（`bonus_total` 与 `owners[].amount`），原因仍写进 `errors`
（前端不渲染它，只供接口层排查；计划 §3 已登记）。

⚠️ 期望值一律写字面十进制（E-0024）；夹具用合成值（E-0077）。
⚠️ 与 `/settle` 的对照打**真实出口**：结账写下的 `Performance.settled_amount` 快照。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
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
from tests.p5_auth_helpers import make_logged_in_client

PREVIEW_URL = "/api/business/settlements/preview"
SETTLE_URL = "/api/business/settlements/settle"
#: 两个端点的闸门节点（预览 = 读、结账 = 写；实测值见各自的既有判据网）。
NODES = ("finance.payroll", "finance.change_settlement_status")
#: 本文件里「扣点模式」的字面值（⛔ 不从被测模块 import，E-0024）。
DEDUCT_FIXED = "1"


class _PreviewConsistencyFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整的最小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="P1-2 同口径游戏")
        # 财务账号：一次授全两个端点要的节点（本文件不测权限，不写「没权限」的用例）。
        self.finance = make_logged_in_client(
            username="p1-preview-fin", groups=("结算甲",), nodes=NODES
        )
        self.finance_user = User.objects.get(username="p1-preview-fin")
        self.taker = User.objects.create_user(username="p1-preview-taker")
        # 打手给 20% 订单价提成（夹具的行都直接写金额，这里只为「有档案」的形状）。
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self._seq = 0

    # ── 夹具 ────────────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.COMPLETED, total="100.00"):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026102200000{self._seq:07d}",
            status=status,
            total_price=Decimal(total),
            hall_price=Decimal("50.00"),
        )

    def _perf(self, order, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            # CHECK 约束：没有归属人 ⇒ 审核状态必须是 NULL。
            "review_status": ReviewStatus.APPROVED if user is not None else None,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(self, performance, amount, *, settle=SettleStatus.UNSETTLED):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="P1-2 测试调整",
            operator="fin",
            settle_status=settle,
        )

    # ── 发请求 ──────────────────────────────────────────────────────────
    def _preview(self, ids):
        response = self.finance.get(
            PREVIEW_URL, {"performance_ids": ",".join(str(one) for one in ids)}
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsNotNone(body["data"], body)
        return body["data"]

    def _settle(self, ids):
        response = self.finance.post(
            SETTLE_URL, {"performance_ids": list(ids)}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()


class FrozenOwnerPreviewTests(_PreviewConsistencyFixture):
    """归属人被冻结 ⇒ 预览不多算（结账侧那一行会整批 `1006`）。"""

    def test_a_frozen_owner_is_not_counted_in_the_preview_total(self):
        frozen = User.objects.create_user(username="p1-preview-frozen")
        StaffProfile.objects.create(user=frozen, settle_forbidden=True)
        row = self._perf(
            self._order(), frozen, temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
        )

        data = self._preview([row.id])

        # 笔数照旧是「所选行数」（对标那一格不筛结账状态）——只有钱被排掉。
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["temp_total"], "30.00")
        self.assertEqual(data["bonus_total"], "0.00")
        self.assertEqual(data["owners"], [])
        # 原因仍留在 `errors`（前端不渲染，供接口层排查）。
        self.assertEqual([entry["performance_id"] for entry in data["errors"]], [row.id])
        self.assertIn("禁止结算", data["errors"][0]["message"])

        # 对照：结账侧对同一行确实一分钱都结不了（整批 1006）⇒ 预览那个 0.00 是真话。
        body = self._settle([row.id])
        self.assertEqual(body["code"], 1006, body)
        row.refresh_from_db()
        self.assertEqual(row.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(row.settled_amount)


class PoolOrderPreviewTests(_PreviewConsistencyFixture):
    """订单还在池内（无接单人 / 待抢单）⇒ 预览不多算。"""

    def test_a_pool_order_is_not_counted_in_the_preview_total(self):
        row = self._perf(
            self._order(status=OrderStatus.WAITING_GRAB),
            self.taker,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
        )

        data = self._preview([row.id])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["bonus_total"], "0.00")
        self.assertEqual(data["owners"], [])
        self.assertEqual([entry["performance_id"] for entry in data["errors"]], [row.id])
        self.assertIn("订单未完成", data["errors"][0]["message"])

        body = self._settle([row.id])
        self.assertEqual(body["code"], 1006, body)


class AlreadySettledPreviewTests(_PreviewConsistencyFixture):
    """「已完全结清」的那一行：结账侧跳过、一分不结 ⇒ 预览也不该把它算进总额。"""

    def test_a_fully_settled_row_is_not_counted_and_settle_pays_nothing(self):
        row = self._perf(
            self._order(),
            self.taker,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
            settle_status=SettleStatus.SETTLED,
        )

        data = self._preview([row.id])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["settled_count"], 1)
        self.assertEqual(data["bonus_total"], "0.00")
        self.assertEqual(data["owners"], [])

        # 结账侧：成功但跳过（Q3=A），一分钱不结 ⇒ 与预览那个 0.00 同口径。
        body = self._settle([row.id])
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["skipped_performances"], [row.id])
        self.assertEqual(body["data"]["settled_performances"], [])


class OwnerlessRowPreviewTests(_PreviewConsistencyFixture):
    """无归属人的行：结账侧 `1006`（D-0069）⇒ 预览不算这笔钱。"""

    def test_an_ownerless_row_is_not_counted_in_the_preview_total(self):
        row = self._perf(
            self._order(), None, temp_amount=Decimal("7.00"),
            bonus_amount=Decimal("7.00"),
        )

        data = self._preview([row.id])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["bonus_total"], "0.00")
        self.assertEqual(data["owners"], [])
        self.assertIn("还没有接单人", data["errors"][0]["message"])

        body = self._settle([row.id])
        self.assertEqual(body["code"], 1006, body)


class SettleableTotalMatchesSettleTests(_PreviewConsistencyFixture):
    """可结的行：预览总额 == `/settle` 真正写下的实结金额之和（**同一个数**）。"""

    def test_the_preview_total_equals_the_sum_of_settled_amounts(self):
        plain = self._perf(
            self._order(),
            self.taker,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            deduct_mode=DEDUCT_FIXED,
            deduct_value=Decimal("5.00"),
        )                                                    # 净额 = 20 − 5 = 15.00
        adjusted = self._perf(
            self._order(),
            self.taker,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
        )
        self._adj(adjusted, "2.00")                          # 净额 = 30 + 2 = 32.00

        data = self._preview([plain.id, adjusted.id])

        self.assertEqual(data["bonus_total"], "47.00")
        self.assertEqual(
            [(owner["username"], owner["amount"]) for owner in data["owners"]],
            [("p1-preview-taker", "47.00")],
        )

        body = self._settle([plain.id, adjusted.id])

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            sorted(body["data"]["settled_performances"]),
            sorted([plain.id, adjusted.id]),
        )
        plain.refresh_from_db()
        adjusted.refresh_from_db()
        settled = (plain.settled_amount or Decimal("0")) + (
            adjusted.settled_amount or Decimal("0")
        )
        self.assertEqual(settled, Decimal(data["bonus_total"]))


class SettledRowWithPendingAdjustmentTests(_PreviewConsistencyFixture):
    """「已结账行 + 未结账调整」走**坏账出口**⇒ 预览必须照样算这笔（⛔ 只复用 rows 腿）。"""

    def test_a_settled_row_with_a_pending_adjustment_is_counted(self):
        settled_with_adj = self._perf(
            self._order(),
            self.taker,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            settle_status=SettleStatus.SETTLED,
        )
        self._adj(settled_with_adj, "5.00")                  # 净额 = 20 + 5 = 25.00

        data = self._preview([settled_with_adj.id])

        self.assertEqual(data["bonus_total"], "25.00")
        self.assertEqual(data["owners"][0]["amount"], "25.00")

        # 对照：结账侧真的会结这条调整记录（坏账出口），所以那 25.00 不是编的。
        body = self._settle([settled_with_adj.id])
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"]["settled_adjustments"],
            [settled_with_adj.adjustments.get().id],
        )
