"""**单条绩效行**被软删（订单活着）后，结算 / 财务读路径里没有它（spec §4.5 更正块 R-6）。

背景（D-0144 没覆盖的形状）：D-0144 的软删是**随订单**发生的（订单与它的绩效行在同一个
`transaction.atomic()` 里同向标记），于是「订单活着、**单条**绩效行被软删」这个新形状
（D-0147 批 E 任务 3 的垃圾桶端点造出来的）在结算 / 财务侧当时只判 `order__deleted_at`
⇒ 被删的行仍可能出现在结算列表里、仍可能被勾选结账。

判据（逐条对应 R-6 的裁定）：

- ① 它**不出现**在结算列表（`GET /settlements`）里；
- ② 它**不能被结账**：本文件选的是「**请求被拒**」那条路（整批 `1006`），理由与 D-0144
  对「已软删单」的处置同源——`settle_performances` 第 3 步是**逐 id fail-closed**，
  把被删行排掉就自然落进「不合格 ⇒ 整批回滚」，不新增任何分支，且不会产生
  「一半结掉、一半没结」的半态（E-0022 同族）；
- 其余读路径（结算对象人员 / 待归属 / 接单统计 / 人工修正取数 / **调整记录链** /
  **撤销结账的逐条原因**）各补一条——
  每条都自证「删之前它在、删之后它不在」，否则「空」是 E-0004 的假通过。

⚠️ **R-8a（评审后裁定）**：`GET /performances/<id>/adjustments`（调整记录链，
财务读路径）此前只判 `performance__order__deleted_at`，**漏判父绩效行自己的 `deleted_at`**
⇒ 打一个已软删绩效 id 仍能读到它的调整链。本文件补 `AdjustmentChainExclusionTests` 钉住。
⭐ **同一次 sweep 重做**又发现 `performance_writes._revoke_reject_message` 的 `visible`
同类漏判（与 `settlement._reject_message` 同形的姊妹函数）⇒ 同批补上，
判据 `RevokeRejectMessageExclusionTests`。
⚠️ **R-8e**：`settlement.with_unsettled_adjustments` 同批补过滤——它只在**已被过滤的**
`visible` 行上被消费 ⇒ 无可观测判据（本文件不为它造用例，理由是「不产生漏网行」；
登记在任务报告里以免下一个读者以为漏了）。

⚠️ 夹具值一律合成（E-0077）。「随订单软删」那条既有机制不受影响由
`tests/test_order_soft_delete_read_paths.py` 继续守着（本文件不复制它）。
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_stats import _window_rows
from apps.business.performance_writes import _revoke_reject_message
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

SETTLEMENTS_URL = "/api/business/settlements"
SETTLE_URL = "/api/business/settlements/settle"
PEOPLE_URL = "/api/business/settlements/people"
UNATTRIBUTED_URL = "/api/business/settlements/unattributed"
ADJUSTMENTS_URL = "/api/business/performances/{performance_id}/adjustments"
ACTOR = "测试操作人甲"
ORDER_NO = "20260924000000000901"


class _RowSoftDeleteFixture(TenantTestCase):
    """一张 `COMPLETED` 单 + 一条 MAIN 行（对照，不动）+ 一条 SALES 行（靶子，各用例自删）。

    ⚠️ 金额给非零值：0 值下「有没有算进去」不可区分。
    """

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.finance = make_logged_in_client(username="测试财务甲", is_superuser=True)
        self.finance_user = User.objects.get(username="测试财务甲")
        self.taker = User.objects.create_user(username="测试打手甲")
        self.sales_user = User.objects.create_user(username="测试销售甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.COMPLETED,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        self.main = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            deduct_mode="",
            deduct_value=Decimal("0.00"),
        )
        self.sales = Performance.objects.create(
            order=self.order,
            user=self.sales_user,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("5.00"),
            bonus_amount=Decimal("5.00"),
            commission_rate=Decimal("10.00"),
            deduct_mode="",
            deduct_value=Decimal("0.00"),
        )

    def _soft_delete_row(self, performance):
        """把**绩效行**标删（订单**不**动）——正是本批垃圾桶端点造出来的形状。"""
        Performance.objects.filter(pk=performance.pk).update(
            deleted_at=timezone.now(), deleted_by=ACTOR
        )

    def _settlement_ids(self):
        body = self.finance.get(SETTLEMENTS_URL).json()
        self.assertEqual(body["code"], 0, body)
        return [row["id"] for row in body["data"]["items"]]

    def _settle(self, performance_ids):
        return self.finance.post(
            SETTLE_URL,
            {"performance_ids": performance_ids},
            content_type="application/json",
        ).json()

    def _settle_status(self, performance):
        return Performance.objects.get(pk=performance.pk).settle_status


class PerformanceListExclusionTests(_RowSoftDeleteFixture):
    """⭐ **R-9 连带**：绩效列表（`performance_queryset`）按**绩效行自身**的 `deleted_at` 过滤。

    ⚠️ 这条隔离判据在垃圾桶端点的边界用例里**做不到了**：R-9 之后端点只删得掉 MAIN 行，
    而删 MAIN 会连带清它的 `user` ⇒ 列表的 `user__isnull=False` 那道过滤也会把它拿掉
    （两条原因叠加，看不出是哪一条在起作用）。所以这里用 ORM 直写软删一条 `user` **仍在**
    的行，单独钉住 `deleted_at__isnull=True`（判据自证：删后那一行的 `user_id` 没变）。
    """

    URL = "/api/business/performances/"

    def _ids(self):
        # 列表面另需 `my_performance.export`(1163) 这扇门；财务账号是租户管理员 ⇒ 短路放行。
        body = self.finance.get(self.URL).json()
        self.assertEqual(body["code"], 0, body)
        return [row["id"] for row in body["data"]["items"]]

    def test_a_soft_deleted_row_with_a_live_user_disappears_from_the_performance_list(self):
        before = self._ids()
        # 前提判据：删之前它**真的看得见**（否则「看不见」是恒真的假保证，E-0004）。
        self.assertIn(self.sales.id, before)
        self.assertIn(self.main.id, before)

        self._soft_delete_row(self.sales)

        after = self._ids()
        self.assertNotIn(self.sales.id, after)
        self.assertIn(self.main.id, after)          # 对照：活行仍在
        row = Performance.objects.get(pk=self.sales.pk)
        self.assertIsNotNone(row.deleted_at)        # 行没被硬删
        self.assertEqual(row.user_id, self.sales_user.id)   # 归属人没被清 ⇒ 排除只可能来自 deleted_at


class SettlementListExclusionTests(_RowSoftDeleteFixture):
    """① 结算列表（`SettlementViewSet.get_queryset`）。"""

    def test_a_soft_deleted_row_is_absent_from_the_settlement_list(self):
        before = self._settlement_ids()
        # 前提判据：删之前它**真的看得见**（否则「看不见」是恒真的假保证，E-0004）。
        self.assertIn(self.sales.id, before)
        self.assertIn(self.main.id, before)

        self._soft_delete_row(self.sales)

        after = self._settlement_ids()
        self.assertNotIn(self.sales.id, after)
        self.assertIn(self.main.id, after)      # 对照：别的行仍在
        # 行没被硬删（软删的判据）。
        self.assertIsNotNone(Performance.objects.get(pk=self.sales.pk).deleted_at)


class SettleActionExclusionTests(_RowSoftDeleteFixture):
    """② 结算动作（`settle_performances`）——选「请求被拒（整批 1006）」那条路。"""

    def test_the_row_leg_is_refused_and_the_whole_batch_rolls_back(self):
        self._soft_delete_row(self.sales)
        body = self._settle([self.main.id, self.sales.id])
        self.assertEqual(body["code"], 1006, body)
        # 逐 id 说明原因：被删的行在这条读路径上与「不存在或不可见」同一个出口。
        self.assertIn("不存在或不可见", body["msg"])
        # 整批回滚：合格行也**一条没结**，且没有审计行。
        self.assertEqual(self._settle_status(self.main), SettleStatus.UNSETTLED)
        self.assertEqual(self._settle_status(self.sales), SettleStatus.UNSETTLED)
        from apps.audit.models import OperationLog

        self.assertFalse(
            OperationLog.objects.filter(action="performance.settle").exists()
        )

    def test_the_adjustments_leg_is_refused_too(self):
        """⭐ 承重：只补 `rows` 腿不够——调整记录那条腿会把被删行算成「合格」而只结调整。

        形态：被删的绩效行上挂一条**未结账**调整记录 + 订单 `COMPLETED` ⇒ 没有这条过滤时
        第 3 步会把它算作合格、第 5 步只把调整记录标成已结账（绩效行留在未结账的半态）。
        """
        PerformanceAdjustment.objects.create(
            performance=self.sales,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-1.00"),
            reason="测试修正甲",
            operator=ACTOR,
            settle_status=SettleStatus.UNSETTLED,
        )
        self._soft_delete_row(self.sales)
        body = self._settle([self.sales.id])
        self.assertEqual(body["code"], 1006, body)
        adjustment = PerformanceAdjustment.objects.get(performance=self.sales)
        self.assertEqual(adjustment.settle_status, SettleStatus.UNSETTLED)

    def test_the_same_endpoint_settles_a_row_that_was_not_soft_deleted(self):
        """对照（可达性）：同一端点对未删行仍然成功——否则上面两条是「端点坏了」的假通过。"""
        body = self._settle([self.main.id])
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._settle_status(self.main), SettleStatus.SETTLED)


class SettlementPeopleExclusionTests(_RowSoftDeleteFixture):
    """结算对象人员候选（`SettlementPeopleView`）：只出现在被删行上的人不进候选。"""

    def _candidate_ids(self):
        body = self.finance.get(PEOPLE_URL).json()
        self.assertEqual(body["code"], 0, body)
        return [row["id"] for row in body["data"]["items"]]

    def test_the_people_candidates_drop_a_person_who_only_has_a_soft_deleted_row(self):
        before = self._candidate_ids()
        self.assertIn(self.sales_user.id, before)
        self.assertIn(self.taker.id, before)

        self._soft_delete_row(self.sales)

        after = self._candidate_ids()
        self.assertNotIn(self.sales_user.id, after)
        self.assertIn(self.taker.id, after)


class UnattributedExclusionTests(_RowSoftDeleteFixture):
    """待归属（`UnattributedView`）：判据是「有没有 SALES 行」⇒ 被删的 SALES 行不算有。"""

    def _order_nos(self):
        body = self.finance.get(UNATTRIBUTED_URL).json()
        self.assertEqual(body["code"], 0, body)
        return [row["order_no"] for row in body["data"]["items"]]

    def test_an_order_whose_only_sales_row_is_soft_deleted_becomes_unattributed(self):
        # 前提：有 SALES 行 ⇒ 不在待归属里。
        self.assertNotIn(ORDER_NO, self._order_nos())

        self._soft_delete_row(self.sales)

        # 被删的 SALES 行不再算「有销售提成行」⇒ 这笔提成没人领，必须回到待归属由财务处置。
        self.assertIn(ORDER_NO, self._order_nos())


class OrderStatsWindowExclusionTests(_RowSoftDeleteFixture):
    """接单统计的窗口行（`_window_rows`）：被软删的绩效行不计入任何人的金额。"""

    def test_the_window_rows_exclude_the_soft_deleted_row(self):
        Performance.objects.filter(pk__in=[self.main.pk, self.sales.pk]).update(
            claimed_at=timezone.now()
        )
        self._soft_delete_row(self.sales)

        rows = _window_rows(
            stime=date.today(), etime=date.today(), viewer=self.finance_user
        )
        perf_ids = {row.pk for group in rows.values() for row in group}
        self.assertIn(self.main.pk, perf_ids)
        self.assertNotIn(self.sales.pk, perf_ids)


class ManualAdjustmentTargetExclusionTests(_RowSoftDeleteFixture):
    """人工修正的取数（`adjustment_views`）：被软删的行与「不存在或不可见」同一个出口。"""

    def _post_adjustment(self, performance, request_id):
        return self.finance.post(
            f"/api/business/performances/{performance.pk}/adjustments",
            {"amount": "-1.00", "reason": "测试修正甲", "request_id": request_id},
            content_type="application/json",
        ).json()

    def test_a_manual_adjustment_on_a_soft_deleted_row_is_refused_and_writes_nothing(self):
        self._soft_delete_row(self.sales)
        body = self._post_adjustment(self.sales, "测试-修正-甲")
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "绩效不存在或不可见")
        self.assertFalse(
            PerformanceAdjustment.objects.filter(performance=self.sales).exists()
        )

    def test_a_manual_adjustment_on_a_live_row_still_succeeds(self):
        body = self._post_adjustment(self.main, "测试-修正-乙")
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(
            PerformanceAdjustment.objects.filter(performance=self.main).exists()
        )


class AdjustmentChainExclusionTests(_RowSoftDeleteFixture):
    """调整记录链（`GET /performances/<id>/adjustments`）：父绩效行被软删后链不可见（R-8a）。

    这条读路径此前只判 `performance__order__deleted_at`（订单那道），漏判父绩效行自己的
    `deleted_at` ⇒ 打一个已软删绩效 id 仍能读到它的调整链。
    """

    def _chain_ids(self, performance):
        body = self.finance.get(
            ADJUSTMENTS_URL.format(performance_id=performance.pk)
        ).json()
        self.assertEqual(body["code"], 0, body)
        return [row["id"] for row in body["data"]["items"]]

    def _adjustment(self, performance, request_id):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-1.00"),
            reason="测试修正甲",
            operator=ACTOR,
            settle_status=SettleStatus.UNSETTLED,
            request_id=request_id,
        )

    def test_the_adjustment_chain_of_a_soft_deleted_row_disappears(self):
        sales_adjustment = self._adjustment(self.sales, "测试-链-甲")
        main_adjustment = self._adjustment(self.main, "测试-链-乙")
        # 前提判据：删之前这条链**真的看得见**（否则「看不见」是恒真的假保证，E-0004）。
        self.assertIn(sales_adjustment.id, self._chain_ids(self.sales))

        self._soft_delete_row(self.sales)

        self.assertNotIn(sales_adjustment.id, self._chain_ids(self.sales))
        # 对照：活行（订单活着、绩效行没软删）的链仍在 ⇒ 不是端点坏了。
        self.assertIn(main_adjustment.id, self._chain_ids(self.main))


class RevokeRejectMessageExclusionTests(_RowSoftDeleteFixture):
    """撤销结账的逐条原因（`performance_writes._revoke_reject_message`）。

    ⭐ R-8a **重做 sweep 时新发现**的一条：这个函数与 `settlement._reject_message` 是
    「同形姊妹」，但它的 `visible` 此前只判 `order__deleted_at`、不判绩效行自己的
    `deleted_at` ⇒ 传一个「订单活着、绩效行已被软删」的 id 会念出
    「不是已结账状态」（把被删行当活行的假话），与 R-6 修好的那一半不对称。
    """

    def test_a_soft_deleted_row_is_reported_as_absent_in_the_revoke_message(self):
        self._soft_delete_row(self.sales)
        message = _revoke_reject_message([self.sales.pk], user=self.finance_user)
        self.assertIn("不存在或不可见", message)

    def test_a_live_unsettled_row_still_reports_its_own_reason(self):
        """对照（防「一律说成不存在」恒真）：活行的原因仍是它自己的「不是已结账状态」。"""
        message = _revoke_reject_message([self.main.pk], user=self.finance_user)
        self.assertIn("不是已结账状态", message)
        self.assertNotIn("不存在或不可见", message)


