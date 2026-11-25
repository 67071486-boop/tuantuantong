"""`Order` / `Performance` 四列软删标记 + `Order` 自定义默认 manager 的口径
（spec §4.1 / §4.2 / §4.3-C1；D2 / D3）。

五件事：① 四列的形状（类型 / 可空 / 宽度）；② 默认 manager **排除**已删、`all_objects` **看得到**；
③ `alive()` / `dead()` 两个显式方法；④ 部分索引**恰好一条**、条件恰为 `deleted_at IS NOT NULL`
（⛔ 没有 `db_index=True`）；⑤ `Performance` **没有**换默认 manager（D3 明确不加）。

⚠️ ③ 分两类路径各钉一遍：`SoftDeleteManagerTests` 走 `Order.objects`（纯 manager），
`ReverseManagerSoftDeleteTests` 走反向外键 manager（`game.orders`，真实类型是 Django 动态生成的
`RelatedManager`）——两类 manager 的 MRO 不同，`super()` 上溯的落点也不同，一类全绿不代表另一类对。

⚠️ 期望值一律写**字面值**（E-0024）：不从 `models.py` 导常量来断言它自己。
⚠️ 夹具值一律合成（E-0077）。
"""
from django.db import models
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderManager, OrderQuerySet, Performance


class SoftDeleteColumnShapeTests(TenantTestCase):
    """四列的形状。"""

    def test_order_has_a_nullable_deleted_at_and_a_150_char_deleted_by(self):
        at = Order._meta.get_field("deleted_at")
        self.assertEqual(at.get_internal_type(), "DateTimeField")
        self.assertTrue(at.null)
        self.assertTrue(at.blank)
        self.assertEqual(at.verbose_name, "删除时间")
        by = Order._meta.get_field("deleted_by")
        self.assertEqual(by.get_internal_type(), "CharField")
        self.assertEqual(by.max_length, 150)
        self.assertFalse(by.null)          # 空串是「没删 / 没记」这一态，不是 NULL
        self.assertTrue(by.blank)
        self.assertEqual(by.verbose_name, "删除人")

    def test_performance_has_the_same_two_columns(self):
        at = Performance._meta.get_field("deleted_at")
        self.assertEqual(at.get_internal_type(), "DateTimeField")
        self.assertTrue(at.null)
        self.assertTrue(at.blank)
        by = Performance._meta.get_field("deleted_by")
        self.assertEqual(by.get_internal_type(), "CharField")
        self.assertEqual(by.max_length, 150)
        self.assertFalse(by.null)
        self.assertEqual(by.verbose_name, "删除人")

    def test_deleted_at_has_no_plain_db_index(self):
        """⛔ `db_index=True` 是**明确不要**的（spec §4.2）：全表绝大多数行是 NULL，
        普通 b-tree 对 `IS NULL` 没有选择度。有人加上它 ⇒ 这条红。"""
        self.assertFalse(Order._meta.get_field("deleted_at").db_index)

    def test_the_partial_index_is_exactly_one_and_its_condition_is_deleted_rows_only(self):
        found = [
            idx for idx in Order._meta.indexes if idx.name == "idx_order_deleted_at"
        ]
        self.assertEqual(len(found), 1)
        idx = found[0]
        self.assertEqual(list(idx.fields), ["-deleted_at"])
        # 条件逐字：只索引已删行。
        self.assertEqual(idx.condition, models.Q(deleted_at__isnull=False))
        # `Performance` 不建索引（spec §4.2 的那句话由这条钉住）。
        self.assertEqual(
            [i.name for i in Performance._meta.indexes if "deleted" in i.name], []
        )


class SoftDeleteManagerTests(TenantTestCase):
    """默认 manager 的排除面 + 逃生口 + 两个显式方法。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.alive = Order.objects.create(
            game=self.game, order_no="20260924000000000901"
        )
        self.dead = Order.objects.create(
            game=self.game,
            order_no="20260924000000000902",
            deleted_at="2026-09-24T10:00:00+08:00",
            deleted_by="测试操作人甲",
        )

    def test_the_default_manager_hides_soft_deleted_rows(self):
        self.assertEqual([o.order_no for o in Order.objects.all()], ["20260924000000000901"])
        self.assertEqual(Order.objects.count(), 1)

    def test_all_objects_sees_both_rows(self):
        nos = sorted(o.order_no for o in Order.all_objects.all())
        self.assertEqual(nos, ["20260924000000000901", "20260924000000000902"])

    def test_the_manager_is_our_subclass_and_exposes_alive_and_dead(self):
        self.assertIsInstance(Order.objects, OrderManager)
        self.assertIsInstance(Order.objects.all(), OrderQuerySet)
        self.assertEqual(
            [o.order_no for o in Order.objects.alive()], ["20260924000000000901"]
        )
        self.assertEqual(
            [o.order_no for o in Order.objects.dead()], ["20260924000000000902"]
        )

    def test_performance_keeps_the_plain_default_manager(self):
        """⛔ `Performance` **不加**自定义默认 manager（D3）：它靠 `order__deleted_at` 逐处收窄。
        有人给它也套一个软删 manager ⇒ 这条红。"""
        self.assertIs(type(Performance.objects), models.Manager)


class ReverseManagerSoftDeleteTests(TenantTestCase):
    """反向外键 manager（`game.orders`）上的软删口径。

    ⚠️ 上面 `SoftDeleteManagerTests` 全部走 `Order.objects`（纯 manager），
    而 `game.orders` 的真实类型是 Django 按关系动态生成的 `RelatedManager`，
    它的 `get_queryset()` 才是叠 FK 条件（该游戏）的那一跳。所以这两条路要**各钉一遍**：
    只测纯 manager 时，反向 manager 上「条件丢失 ⇒ 静默返回别的游戏的行」不会被任何用例看到。

    夹具用两款游戏 × 各一笔未删 + 一笔已删，**四笔单号互不相同**——若 FK 条件丢失，
    取值会串到另一款游戏上，等值断言立刻红（若两边取值相同就成了恒真断言，见 E-0024 同族）。
    """

    def setUp(self):
        self.game_a = Game.objects.create(name="测试游戏甲")
        self.game_b = Game.objects.create(name="测试游戏乙")
        self.order_a_alive = Order.objects.create(
            game=self.game_a, order_no="20260924000000000911"
        )
        self.order_a_dead = Order.objects.create(
            game=self.game_a,
            order_no="20260924000000000912",
            deleted_at="2026-09-24T10:00:00+08:00",
            deleted_by="测试操作人甲",
        )
        self.order_b_alive = Order.objects.create(
            game=self.game_b, order_no="20260924000000000913"
        )
        self.order_b_dead = Order.objects.create(
            game=self.game_b,
            order_no="20260924000000000914",
            deleted_at="2026-09-24T11:00:00+08:00",
            deleted_by="测试操作人乙",
        )

    def test_the_reverse_manager_goes_through_our_manager_subclass(self):
        """反向 manager 真的经过 `OrderManager`（否则下面的用例只是打在无关对象上，
        恒真）。`related_name="orders"` 是本仓 `Order.game` 的实际取值。"""
        self.assertIsInstance(self.game_a.orders, OrderManager)

    def test_reverse_manager_all_and_alive_only_return_this_games_live_rows(self):
        self.assertEqual(
            [o.order_no for o in self.game_a.orders.all()],
            ["20260924000000000911"],
        )
        self.assertEqual(
            [o.order_no for o in self.game_a.orders.alive()],
            ["20260924000000000911"],
        )
        self.assertEqual(
            [o.order_no for o in self.game_b.orders.all()],
            ["20260924000000000913"],
        )
        self.assertEqual(
            [o.order_no for o in self.game_b.orders.alive()],
            ["20260924000000000913"],
        )

    def test_reverse_manager_dead_only_returns_this_games_deleted_rows(self):
        self.assertEqual(
            [o.order_no for o in self.game_a.orders.dead()],
            ["20260924000000000912"],
        )
        self.assertEqual(
            [o.order_no for o in self.game_b.orders.dead()],
            ["20260924000000000914"],
        )

    def test_the_plain_manager_still_spans_all_games(self):
        """修反向 manager 时**不能**把正向修坏：`Order.objects` 仍看全量。"""
        self.assertEqual(
            sorted(o.order_no for o in Order.objects.alive()),
            ["20260924000000000911", "20260924000000000913"],
        )
        self.assertEqual(
            sorted(o.order_no for o in Order.objects.dead()),
            ["20260924000000000912", "20260924000000000914"],
        )
