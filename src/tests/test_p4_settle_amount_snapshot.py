"""P4-FE-4：结账时把「这一行本次实结多少」快照进 `Performance.settled_amount`（方案 B）。

需求来源：业务方裁定**方案 B**（不是「A 改成累计净额」）——理由是
「结算记录」页是财务对账/留痕用的，**历史数字不该随后来的调整变动**。

要解决的问题（业务方原话的算术）：应付净额只算**未结账**的调整，而结账会把调整翻成
「已结账」⇒ 佣金 100、一条 −30 的退款冲减（未结账）⇒ 应付净额 70 ⇒ 按 70 结账；
结账后那条 −30 变已结账 ⇒ **同一行现在显示 100**，而当初实际付的是 70。
`settled_amount` 就是「结账那一刻这一行的应付净额」，它**不随之后的调整变动**。

四条纪律（每条都有会红的用例）：

1. **T1**：结账后逐行断言快照 == **字面值** `Decimal("70.00")`（构造「佣金 100 + 一条 −30
   未结账调整」⇒ **不是** 100.00）；**未结账**的行 `settled_amount is None`。
2. ⭐ **T2**：「只结调整记录」**不覆盖**已结账行的快照——第一次结完整行后记下快照值，
   再追加一条未结账调整并再结一次，快照必须**逐字不变**（用第一次读到的值比对，
   ⛔ 不拿活值/`now()` 比），并配前置哨兵证明第二轮**真的结了调整**。
3. **T3**：**单行净额为负时快照也是负**（组内一正一负、归属人组和 ≥0 ⇒ 负的那行快照为负，
   断言字面负值 `Decimal("-5.00")`）。⭐ 这条同时是「快照写成**组和**」那组变异的判别用例：
   组和是 15.00，与逐行净额在**同一批的两行上都不相等**。
4. **T4**：`GET /api/business/settlements` 出参含 `settled_amount`；未结账为 `null`
   （**不是 0、也不是空串**）、已结账为字面值。

⚠️ **期望值一律写字面值**（E-0024）：金额写 `Decimal("70.00")` / `Decimal("-5.00")`，
状态写 `"已结账"` / `"未结账"`，出参写 `"70.00"`——不引被测模块导出的常量（自指断言不会红）。
⚠️ **落库断言打在数据库行上**（`refresh_from_db()`），不是打在内层函数返回值上。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

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
from apps.business.settlement import settle_performances
from apps.business.settlement_amount import payable_net
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 结算列表端点（无尾斜杠，与既有 P4-8 用例的约定一致）。
SETTLEMENTS_URL = "/api/business/settlements"

#: T1/T2 的判别力关键：结算人**与绩效归属人不同名**（归属人见 fixture 的 `taker-a`）。
ACTOR = "cs-1"


class _SnapshotFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整记录的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # API 用例用管理员只是为了跳过建角色目录（闸门本身由既有 P4-7/P4-8 的权限测试钉住）。
        self.finance = make_logged_in_client(
            username="finance-fe4", is_superuser=True
        )
        self.taker_a = User.objects.create_user(username="taker-a")
        self.taker_b = User.objects.create_user(username="taker-b")
        # ⭐ 按游戏权限收窄：本文件的直调用例把 `taker_a` 当作
        #    `settle_performances(..., user=)` 的**请求方**（数据权限那一维）⇒
        #    显式声明它持有本夹具的游戏（本文件不依赖「没有档案」，补档案不改期望值）。
        grant_games(self.taker_a, self.game)
        self._order_seq = 0

    def _order(self, *, project_text="原神-深渊代练"):
        self._order_seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092000001{self._order_seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
            project_text=project_text,
        )

    def _perf(self, order, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(self, performance, amount):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=SettleStatus.UNSETTLED,
        )


class SnapshotWriteTests(_SnapshotFixture):
    """T1 / T2 / T3：`settle_performances` 第 5 步写**逐行**快照，且不覆盖已结账行。"""

    def test_T1_the_snapshot_is_this_rows_net_at_settle_time_not_the_live_value(self):
        """T1：佣金 100 + 一条 −30 未结账调整 ⇒ 快照必须是 `70.00`（**不是** 100.00）。

        构造与业务方原话同形。⭐ 同时钉住「活值已经变了」：结账后那条 −30 已是已结账，
        `payable_net` 只看未结账调整 ⇒ 同一行的**活值**回到 `100.00`，
        而快照仍是 `70.00`——这正是这一列存在的理由（两者若相等，这列就没有意义）。
        未结账的对照行必须是 `None`（不是 `Decimal("0.00")`）。
        """
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("100.00"),
            bonus_amount=Decimal("100.00"),
        )
        self._adj(perf, "-30.00")
        # 对照组：**不在这批里**的行，快照必须保持「没有」（可空列的两个状态都要有断言）。
        untouched = self._perf(self._order(), self.taker_b)

        settle_performances([perf.id], actor=ACTOR, user=self.taker_a)

        perf.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("70.00"))
        self.assertEqual(perf.settle_status, "已结账")
        # ⭐ 活值（未结账调整已清零）与快照**不同**——快照不是「结账后再算一遍」的结果。
        self.assertEqual(payable_net(perf, []), Decimal("100.00"))
        self.assertIsNone(untouched.settled_amount)
        self.assertEqual(untouched.settle_status, "未结账")

    def test_T2_settling_only_an_adjustment_does_not_overwrite_the_snapshot(self):
        """⭐ T2：已结账行被追加新调整记录后**再结一次**，快照**逐字不变**。

        判据用**第一次读到的值**比对（`assertEqual(第二次, 第一次)`），
        ⛔ 不写 `assertIsNotNone`——那对「被覆盖成新值」照样绿。
        第二轮追加的是 `-5.00`：若实现用本轮净额（= 20.00 − 5.00 = 15.00）覆盖，
        或拿组和覆盖，断言都会红。
        """
        perf = self._perf(self._order(), self.taker_a)
        settle_performances([perf.id], actor=ACTOR, user=self.taker_a)
        perf.refresh_from_db()
        first_snapshot = perf.settled_amount
        self.assertEqual(first_snapshot, Decimal("20.00"))

        adjustment = self._adj(perf, "-5.00")

        result = settle_performances([perf.id], actor="cs-2", user=self.taker_a)

        # 前置哨兵：本轮**真的结了东西**（否则「没变」可能只是「什么都没发生」）。
        self.assertEqual(result.settled_performances, [])
        self.assertEqual(result.settled_adjustments, [adjustment.id])

        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settled_amount, first_snapshot)
        self.assertEqual(perf.settled_amount, Decimal("20.00"))
        self.assertEqual(adjustment.settle_status, "已结账")

    def test_T3_a_negative_row_snapshots_its_own_negative_net_not_the_owner_total(self):
        """T3：组内一正一负、归属人组和 ≥0 ⇒ **负的那行快照为负**（不是组和）。

        taker-a 两行：−5.00（10.00 + (−15.00)）与 +20.00 ⇒ 组和 15.00 ≥ 0 ⇒ 放行。
        ⭐ 快照必须是**逐行**值 `-5.00` / `20.00`——写成**组和**（15.00）时两行断言同时红，
        所以这条是那组变异的判别用例（T1 只有一行，分辨不出组和与逐行净额）。
        """
        negative = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
        )
        self._adj(negative, "-15.00")
        positive = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
        )

        settle_performances([negative.id, positive.id], actor=ACTOR, user=self.taker_a)

        negative.refresh_from_db()
        positive.refresh_from_db()
        self.assertEqual(negative.settle_status, "已结账")
        self.assertEqual(positive.settle_status, "已结账")
        self.assertEqual(negative.settled_amount, Decimal("-5.00"))
        self.assertEqual(positive.settled_amount, Decimal("20.00"))


class SnapshotListFieldTests(_SnapshotFixture):
    """T4：`GET /api/business/settlements` 的 `settled_amount` 出参形状。"""

    def _items(self, params=None):
        body = self.finance.get(SETTLEMENTS_URL, params or {}).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def test_T4_settled_rows_report_the_literal_snapshot_and_unsettled_rows_report_null(self):
        """T4：已结账 ⇒ 字面值 `"70.00"`；未结账 ⇒ `null`（不是 `"0.00"`、也不是 `""`）。

        ⚠️ 必须走 API：DRF 的 `DecimalField.to_representation` 默认把 `None` 渲染成**空串**
        （`rest_framework/fields.py:1107-1114`）——那是「看起来这一列没值」的静默错账，
        所以这条出参形状**只能**在端到端上钉。
        ⚠️ 同一次比对里同时放一条结过账的行：**先证明有数据**再下否定断言（E-0004）。
        """
        settled = self._perf(
            self._order(project_text="英雄联盟-排位"),
            self.taker_a,
            temp_amount=Decimal("100.00"),
            bonus_amount=Decimal("100.00"),
        )
        self._adj(settled, "-30.00")
        self._perf(self._order(project_text="原神-日常"), self.taker_b)
        settle_performances([settled.id], actor=ACTOR, user=self.taker_a)

        items = self._items()

        self.assertEqual(len(items), 2)
        by_project = {row["project"]: row for row in items}
        self.assertEqual(by_project["英雄联盟-排位"]["settled_amount"], "70.00")
        self.assertEqual(by_project["英雄联盟-排位"]["settle_status"], "已结账")
        unsettled = by_project["原神-日常"]
        self.assertIsNone(unsettled["settled_amount"])
        self.assertEqual(unsettled["settle_status"], "未结账")

    def test_T4b_a_negative_snapshot_is_rendered_as_a_signed_string(self):
        """T4 边界：负的快照出参是 `"-5.00"`（**不是** `0`、不被 `≥ 0` 拦下）。

        渲染格式与既有金额字段同源（`COERCE_DECIMAL_TO_STRING`）——
        用 `SerializerMethodField` / 关掉该开关会得到 `-5.0` 这种不同形状。
        """
        negative = self._perf(
            self._order(project_text="王者荣耀-上分"),
            self.taker_a,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
        )
        self._adj(negative, "-15.00")
        positive = self._perf(
            self._order(project_text="和平精英-陪玩"),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
        )
        settle_performances([negative.id, positive.id], actor=ACTOR, user=self.taker_a)

        items = self._items()

        by_project = {row["project"]: row for row in items}
        self.assertEqual(by_project["王者荣耀-上分"]["settled_amount"], "-5.00")
        self.assertEqual(by_project["和平精英-陪玩"]["settled_amount"], "20.00")
