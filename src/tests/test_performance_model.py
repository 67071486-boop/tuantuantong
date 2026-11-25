"""Task 1：`Performance` 模型与三条约束。

判据：**一个订单只能被一人接手**（D-0012）——由一个部分唯一索引直接表达。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    Performance,
    ReviewStatus,
    SettleStatus,
)


class PerformanceModelTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001"
        )

    def test_defaults(self):
        p = Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)
        self.assertIsNone(p.user_id)              # 还没人接
        self.assertIsNone(p.review_status)        # 还没有可审的东西
        self.assertEqual(p.temp_amount, Decimal("0.00"))
        self.assertEqual(p.bonus_amount, Decimal("0.00"))
        self.assertEqual(p.settle_status, SettleStatus.UNSETTLED)

    def test_only_one_main_taker_per_order(self):
        """⭐ 一个订单只能被一人接手：第二条「打单工资」必须被数据库拒绝。

        两个**不同**的人，所以这条只可能撞上部分唯一索引——不会与
        `(order, user)` 那条撞。这样测的才是我们想测的那条约束。
        """
        u1 = User.objects.create_user(username="taker1")
        u2 = User.objects.create_user(username="taker2")
        Performance.objects.create(
            order=self.order, user=u1, emp_type=EmpType.MAIN
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order, user=u2, emp_type=EmpType.MAIN
                )

    def test_sales_row_can_coexist(self):
        """客服的「销售提成」行必须能与代练员那行并存（D-0011 的能力）。

        这条是**反向对照**：如果部分唯一索引的条件写错（漏掉 `emp_type`），
        它会变红——所以它同时也在保护上面那条的写法正确。
        """
        taker = User.objects.create_user(username="taker")
        kefu = User.objects.create_user(username="kefu")
        Performance.objects.create(
            order=self.order, user=taker, emp_type=EmpType.MAIN
        )
        Performance.objects.create(
            order=self.order, user=kefu, emp_type=EmpType.SALES
        )
        self.assertEqual(Performance.objects.filter(order=self.order).count(), 2)

    def test_order_and_user_pair_is_unique(self):
        """同一订单同一个人只能有一条绩效（两条销售提成也不行）。"""
        kefu = User.objects.create_user(username="kefu")
        Performance.objects.create(
            order=self.order, user=kefu, emp_type=EmpType.SALES
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order, user=kefu, emp_type=EmpType.SALES
                )

    def test_review_status_requires_user(self):
        """没有归属人就不可能有「待审核」——否则就是没人接却等着审的脏行。"""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Performance.objects.create(
                    order=self.order,
                    emp_type=EmpType.MAIN,
                    review_status=ReviewStatus.PENDING,
                )

    def test_amounts_are_decimal_columns(self):
        """金额必须是 Decimal 列。只断言 `str(...)` 是空洞的——
        用字符串创建再读字符串，CharField 也会通过（P2 的教训）。
        """
        p = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount="99.90"
        )
        p.refresh_from_db()
        self.assertIsInstance(p.temp_amount, Decimal)
        self.assertEqual(str(p.temp_amount), "99.90")
