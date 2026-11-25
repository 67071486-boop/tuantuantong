"""`Performance.hours` / `Performance.project` 两列的口径（spec §4.1 / C6 / §8 第 1 条）。

四件事：列的**形状**（类型 / 可空 / 默认值 / 宽度）；新行落 `NULL` / `""`；
`0` 能落库（列上**没有** `hours >= 1` 的 DB 约束，下界只在入口序列化器）；
两列**不在** `MONEY_FIELDS` 里（它们是数量与文本——进了那个元组，订单侧的金额出参
`order_views.py` 会把小时数格式化成两位小数）。

⚠️ 期望值一律写**字面值**（E-0024）：不从 `models.py` 导常量来断言它自己。
"""
from django_tenants.test.cases import TenantTestCase

from apps.business.models import MONEY_FIELDS, Game, Order, Performance


class PerformanceHoursProjectFieldShapeTests(TenantTestCase):
    """两列的形状。"""

    def test_hours_is_a_nullable_positive_integer(self):
        field = Performance._meta.get_field("hours")
        self.assertEqual(field.get_internal_type(), "PositiveIntegerField")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertEqual(field.verbose_name, "时间要求（小时）")

    def test_project_is_a_900_char_non_null_blank_text_column(self):
        # 900（E-0239）：与 `Order.project_text` 同宽——本列是指派弹窗预填它的快照列。
        field = Performance._meta.get_field("project")
        self.assertEqual(field.get_internal_type(), "CharField")
        self.assertEqual(field.max_length, 900)
        self.assertFalse(field.null)                 # 有 default ⇒ 不需要 NULL 态
        self.assertTrue(field.blank)
        self.assertEqual(field.default, "")
        self.assertEqual(field.verbose_name, "接手项目")

    def test_the_two_columns_are_not_money_fields(self):
        self.assertNotIn("hours", MONEY_FIELDS)
        self.assertNotIn("project", MONEY_FIELDS)


class PerformanceHoursProjectValueTests(TenantTestCase):
    """新行的默认值，以及「`0` 能落库」这一条（`>= 1` 只在入口序列化器）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _order(self, no):
        return Order.objects.create(game=self.game, order_no=no)

    def test_a_fresh_row_lands_null_and_empty_string(self):
        perf = Performance.objects.create(
            order=self._order("20260923000000000901"), temp_amount="0.00"
        )
        perf.refresh_from_db()
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")

    def test_a_zero_hour_value_is_accepted_by_the_database(self):
        """列上**没有** `hours >= 1` 的约束——下界由入口序列化器的 `min_value=1` 保证（spec §4.1）。

        判别力：谁把 `CheckConstraint(hours__gte=1)` 或模型校验器加回来，本用例在
        `objects.create` 这一步就会红（DB 约束抛 `IntegrityError`）。
        """
        perf = Performance.objects.create(
            order=self._order("20260923000000000902"), hours=0, project="三角洲"
        )
        perf.refresh_from_db()
        self.assertEqual(perf.hours, 0)
        self.assertEqual(perf.project, "三角洲")
