from decimal import Decimal

from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderStatus


class OrderModelTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def test_default_status_is_unassigned(self):
        order = Order.objects.create(game=self.game, order_no="20260920000000000001")
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_all_eight_statuses_exist(self):
        self.assertEqual(
            {s.value for s in OrderStatus},
            {
                "UNASSIGNED", "WAITING_GRAB", "IN_PROGRESS", "PENDING_REVIEW",
                "COMPLETED", "ISSUE", "PAUSED", "REFUNDED",
            },
        )

    def test_order_no_is_unique_within_tenant(self):
        Order.objects.create(game=self.game, order_no="20260920000000000001")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(game=self.game, order_no="20260920000000000001")

    def test_price_fields_are_decimal(self):
        """金额字段必须是 Decimal 列，不是字符串列。

        ⚠️ 只断言 `str(...)` 是**空洞的**——用字符串创建再读字符串，CharField 也会通过。
        必须 `refresh_from_db()` 拿到数据库真实类型再断言。
        """
        order = Order.objects.create(
            game=self.game, order_no="20260920000000000002", total_price="99.90"
        )
        order.refresh_from_db()
        self.assertIsInstance(order.total_price, Decimal)
        self.assertEqual(str(order.total_price), "99.90")

    def test_prices_and_contacts_are_optional(self):
        """spec §5.2：必填只有游戏与项目。`total_price` 省略 → 落列默认 0。

        ⚠️这是**接口层 / ORM 层**口径：录单**页面**另有
        「订单总价必填、默认 0」，见 `docs/DECISIONS.md` D-0082。
        """
        order = Order.objects.create(
            game=self.game, order_no="20260920000000000003", project_text="随便什么"
        )
        self.assertEqual(order.total_price, Decimal("0.00"))
        self.assertEqual(order.customer_phone, "")

    def test_project_fk_is_optional_and_text_is_authoritative(self):
        """D9：项目可以只填文本、不选 FK。"""
        order = Order.objects.create(
            game=self.game, order_no="20260920000000000004", project_text="深境螺旋12层"
        )
        self.assertIsNone(order.project)
        self.assertEqual(order.project_text, "深境螺旋12层")

    def test_source_order_no_is_not_unique(self):
        """spec §3.2.2：来源单号 P2 不建唯一约束。"""
        Order.objects.create(
            game=self.game, order_no="20260920000000000005", source_order_no="TB-1"
        )
        Order.objects.create(
            game=self.game, order_no="20260920000000000006", source_order_no="TB-1"
        )
        self.assertEqual(Order.objects.filter(source_order_no="TB-1").count(), 2)

    def test_no_tenant_id_column(self):
        """schema 是隔离边界，不允许用 tenant_id 伪装隔离（沿用 P1 红线）。"""
        names = {f.name for f in Order._meta.get_fields()}
        self.assertNotIn("tenant_id", names)
        self.assertNotIn("tenant", names)

    def test_has_no_password_field(self):
        """D6：不存玩家密码。这个断言防止后来者顺手加上去。"""
        names = {f.name for f in Order._meta.get_fields()}
        for forbidden in ("game_password", "password", "game_pwd", "security_type"):
            self.assertNotIn(forbidden, names)
