"""价格看板中心**四张表的模型级判据**（批次 1 计划 Task 1）。

设计稿：`docs/plans/2026-10-11-price-board-center.md` §2.1 / §2.2 / §2.3。

## 这份网钉什么（每条都对应设计稿的一行）

1. **四张表的列恰好等于设计稿那一组**（⛔ 多一列少一列都红）。
   ⚠️ 用「恰好集合」而不是「不包含那几个名字」：后者能被"换个名字加回来"绕过（E-0011 同族）。
2. ⭐ **三条唯一约束真的在库上**（`OrderProjectTag(tagging, tag)` / `ProjectTagDict(game, name)`
   / `ProjectTagStat(game, tag, window_days)`）。⚠️ 每条都**先自证第一行真的落库**再撞库，
   否则「根本没写进去」的实现也会让 `IntegrityError` 看着像约束生效；
   每条另配**对照**（换一个维度 ⇒ **允许**），否则「什么都拒」也能让撞库那条绿。
3. ⭐ **金额列没有「必须 > 0」的约束**（D-0015：0 与负数都是合法值，E-0021）：
   `0` / 负数 / `allocated_hall=NULL` 三种都要能**写进去并读回来**。
   ⛔ 这一条是**正面**判据（写进去读得出来），不是"表上没有那个约束名"。
4. **`ProtectError` / CASCADE 的方向对**：`OrderProjectTag.tag` / `OrderTagging.game` /
   `ProjectTagDict.game` 是 PROTECT（配置被删不得带走派生数据）；
   `OrderTagging.order` 是 CASCADE（订单没了，派生行留着没有意义）。
5. **`window_days` 的五档 / `is_active` 默认值 / `content_clean` 允许空串**等默认行为。

⚠️ 只覆盖**数据面**：清洗（`tests/test_project_tag_cleaning.py`）、导入命令
（`tests/test_import_project_tag_dict.py`）、归类与预聚合分属其它任务，⛔ 本文件不测那些。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C），与任何真实租户 / 真实订单无关。
⚠️ 断言打在 `_meta` / **数据库行**这些真实出口上（E-0009），字段名与取值写**字面量**，
⛔ 不从常量模块 import（E-0024：钉值不钉常量）。
"""
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Game,
    Order,
    OrderProjectTag,
    OrderTagging,
    ProjectTagDict,
    ProjectTagStat,
)

ORDER_NO = "20261011000000000001"


def columns_of(model):
    """模型的**列名**集合（`local_fields` 的名字，含 Django 自己加的 `id`）。"""
    return {field.name for field in model._meta.local_fields}


class ProjectTagColumnTests(TenantTestCase):
    """四张表的列**恰好**是设计稿那一组。"""

    def test_project_tag_dict_columns_are_exactly_the_spec_ones(self):
        self.assertEqual(
            columns_of(ProjectTagDict),
            {"id", "game", "name", "aliases", "category", "is_active",
             "created_at", "updated_at"},
        )

    def test_order_tagging_columns_are_exactly_the_spec_ones(self):
        self.assertEqual(
            columns_of(OrderTagging),
            {"id", "order", "game", "content_clean", "status", "created_at", "updated_at"},
        )

    def test_order_project_tag_columns_are_exactly_the_spec_ones(self):
        self.assertEqual(
            columns_of(OrderProjectTag),
            {"id", "tagging", "tag", "allocated_total", "allocated_hall",
             "is_allocated", "created_at"},
        )

    def test_project_tag_stat_columns_are_exactly_the_spec_ones(self):
        self.assertEqual(
            columns_of(ProjectTagStat),
            {"id", "game", "tag", "window_days", "sample_count",
             "tp25", "tp50", "tp75", "hp25", "hp50", "hp75", "computed_at"},
        )

    def test_no_table_carries_a_tenant_column(self):
        """schema 就是隔离边界（`AGENTS.md` §4）：⛔ 不许用 `tenant_id` 伪装隔离。"""
        for model in (OrderTagging, OrderProjectTag, ProjectTagDict, ProjectTagStat):
            with self.subTest(model=model.__name__):
                names = {field.name for field in model._meta.get_fields()}
                self.assertNotIn("tenant", names)
                self.assertNotIn("tenant_id", names)

    def test_the_four_tables_live_in_the_tenant_schema(self):
        """四张表都在**当前租户 schema**里（不是 public）。

        ⚠️ 判据打在**数据库表清单**上，不是模型元数据——模型注册对不代表表建在哪儿。
        """
        for table in ("business_ordertagging", "business_orderprojecttag",
                      "business_projecttagdict", "business_projecttagstat"):
            with self.subTest(table=table):
                self.assertIn(table, connection.introspection.table_names())


class MoneyColumnTests(TenantTestCase):
    """⭐ 金额列**没有「必须 > 0」的约束**（D-0015 / E-0021）。"""

    def setUp(self):
        self.game = Game.objects.create(name="合成游戏甲")
        self.tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        self.other_tag = ProjectTagDict.objects.create(game=self.game, name="合成项目乙")
        self.order = Order.objects.create(game=self.game, order_no=ORDER_NO)
        self.tagging = OrderTagging.objects.create(order=self.order, game=self.game)

    def _row(self, total, hall, **kwargs):
        kwargs.setdefault("tag", self.tag)
        return OrderProjectTag.objects.create(
            tagging=self.tagging,
            allocated_total=Decimal(total), allocated_hall=hall, **kwargs
        )

    def test_zero_and_negative_amounts_round_trip(self):
        """0 与负数都是合法值（退款 / 冲抵 / 调整）⇒ 必须能写进去、原样读回来。

        正反两面都写：只测 0 的实现可能把负数拒掉，只测负数的可能把 0 当"没填"。
        """
        row = self._row("0.00", Decimal("0.00"))
        self.assertEqual(row.allocated_total, Decimal("0.00"))
        self.assertEqual(row.allocated_hall, Decimal("0.00"))

        self.tagging.project_tags.all().delete()
        row = self._row("-199.50", Decimal("-10.00"))
        self.assertEqual(row.allocated_total, Decimal("-199.50"))
        self.assertEqual(row.allocated_hall, Decimal("-10.00"))

        again = OrderProjectTag.objects.get(pk=row.pk)
        self.assertEqual(again.allocated_total, Decimal("-199.50"))
        self.assertEqual(again.allocated_hall, Decimal("-10.00"))

    def test_allocated_hall_can_be_null_but_total_cannot(self):
        """`Order.hall_price` 可空 ⇒ 「这单没有发价」是真实状态，写 `NULL` **不是**写 0。

        ⚠️ 0 是合法成交价（上一条），所以拿 0 冒充「没有」是**丢信息**，不是省事。
        """
        row = self._row("88.00", None)
        self.assertIsNone(row.allocated_hall)
        self.assertIsNotNone(row.allocated_total)
        self.assertEqual(OrderProjectTag.objects.get(pk=row.pk).allocated_hall, None)
        # 判「有没有值」必须用 `is not None`：0 与 None 是两个不同的事实。
        # ⚠️ 换一个项目再写：`(tagging, tag)` 上有唯一约束，同项目不能来两行。
        zero = self._row("0.00", Decimal("0.00"), tag=self.other_tag)
        self.assertIsNot(zero.allocated_hall, None)
        self.assertEqual(zero.allocated_hall, Decimal("0.00"))
        self.assertIsNone(OrderProjectTag.objects.get(pk=row.pk).allocated_hall)


class UniqueConstraintTests(TenantTestCase):
    """⭐⭐ 三条唯一约束**真的在数据库上**（每条先自证第一行落库，再配对照）。"""

    def setUp(self):
        self.game = Game.objects.create(name="合成游戏甲")
        self.other_game = Game.objects.create(name="合成游戏乙")
        self.tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        self.other_tag = ProjectTagDict.objects.create(game=self.game, name="合成项目乙")
        self.order = Order.objects.create(game=self.game, order_no=ORDER_NO)
        self.tagging = OrderTagging.objects.create(order=self.order, game=self.game)

    def test_one_row_per_tagging_and_tag(self):
        first = OrderProjectTag.objects.create(
            tagging=self.tagging, tag=self.tag, allocated_total=Decimal("10.00")
        )
        # 自证：第一行**真的落库了**（否则下面的 IntegrityError 可能只是"表是空的"）。
        self.assertEqual(OrderProjectTag.objects.filter(pk=first.pk).count(), 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderProjectTag.objects.create(
                tagging=self.tagging, tag=self.tag, allocated_total=Decimal("20.00")
            )

        # 对照：**换一个项目**就允许（否则"什么都拒"也能让上面那条绿）。
        OrderProjectTag.objects.create(
            tagging=self.tagging, tag=self.other_tag, allocated_total=Decimal("20.00")
        )
        self.assertEqual(OrderProjectTag.objects.filter(tagging=self.tagging).count(), 2)

    def test_one_standard_name_per_game(self):
        first = ProjectTagDict.objects.create(game=self.game, name="合成项目丙")
        self.assertEqual(ProjectTagDict.objects.filter(pk=first.pk).count(), 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectTagDict.objects.create(game=self.game, name="合成项目丙")

        # 对照：**同一个名字换一个游戏**允许——不同游戏的项目名互不相干（设计稿 §2.2）。
        ProjectTagDict.objects.create(game=self.other_game, name="合成项目丙")
        self.assertEqual(ProjectTagDict.objects.filter(name="合成项目丙").count(), 2)

    def test_one_row_per_game_tag_and_window(self):
        first = ProjectTagStat.objects.create(
            game=self.game, tag=self.tag, window_days=30, sample_count=5
        )
        self.assertEqual(ProjectTagStat.objects.filter(pk=first.pk).count(), 1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectTagStat.objects.create(
                game=self.game, tag=self.tag, window_days=30, sample_count=7
            )

        # 对照：换一档时间窗允许（五档各一行是设计稿 §8.1 ② 的要求）。
        for days in (60, 90, 180, 360):
            ProjectTagStat.objects.create(
                game=self.game, tag=self.tag, window_days=days, sample_count=5
            )
        self.assertEqual(
            ProjectTagStat.objects.filter(game=self.game, tag=self.tag).count(), 5
        )


class DeleteDirectionTests(TenantTestCase):
    """外键方向：配置 PROTECT、订单 CASCADE。"""

    def setUp(self):
        self.game = Game.objects.create(name="合成游戏甲")
        self.tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        self.order = Order.objects.create(game=self.game, order_no=ORDER_NO)
        self.tagging = OrderTagging.objects.create(order=self.order, game=self.game)
        self.row = OrderProjectTag.objects.create(
            tagging=self.tagging, tag=self.tag, allocated_total=Decimal("10.00")
        )

    def test_dictionary_row_in_use_cannot_be_deleted(self):
        """词典行被引用着就删不掉（设计稿 §2.2：作废用 `is_active=False`，⛔ 不删行）。"""
        with self.assertRaises(ProtectedError):
            self.tag.delete()
        self.assertEqual(ProjectTagDict.objects.filter(pk=self.tag.pk).count(), 1)

    def test_game_in_use_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.game.delete()
        self.assertEqual(Game.objects.filter(pk=self.game.pk).count(), 1)

    def test_deleting_the_order_takes_the_derived_rows_with_it(self):
        """订单没了，派生行留着没有意义 ⇒ 两张派生表一起走（CASCADE）。"""
        self.order.delete()
        self.assertEqual(OrderTagging.objects.filter(pk=self.tagging.pk).count(), 0)
        self.assertEqual(OrderProjectTag.objects.filter(pk=self.row.pk).count(), 0)

    def test_deleting_the_tagging_takes_its_project_rows_with_it(self):
        self.tagging.delete()
        self.assertEqual(OrderProjectTag.objects.filter(pk=self.row.pk).count(), 0)
        # 对照：词典行**不**跟着走（PROTECT ⇒ 它本来就还在）。
        self.assertEqual(ProjectTagDict.objects.filter(pk=self.tag.pk).count(), 1)


class DefaultsTests(TenantTestCase):
    """默认值与可空性（设计稿每一列的语义）。"""

    def setUp(self):
        self.game = Game.objects.create(name="合成游戏甲")

    def test_dictionary_defaults(self):
        tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        self.assertEqual(tag.aliases, "")
        self.assertEqual(tag.category, "")
        self.assertIs(tag.is_active, True)
        self.assertIsNotNone(tag.created_at)
        self.assertIsNotNone(tag.updated_at)

    def test_tagging_defaults_to_pending_with_an_empty_clean_text(self):
        """⭐ 洗完是空串 ⇒ **照样落行**、状态仍是 `PENDING`（设计稿 §2.1 的原话）。

        「待归类队列」就是靠这一行存在的；⛔ 空串不是"不用落行"的理由。
        """
        order = Order.objects.create(game=self.game, order_no=ORDER_NO)
        tagging = OrderTagging.objects.create(
            order=order, game=self.game, content_clean=""
        )
        self.assertEqual(tagging.content_clean, "")
        self.assertEqual(tagging.status, "PENDING")
        self.assertIsNotNone(tagging.updated_at)

    def test_the_three_status_values_are_exactly_the_spec_ones(self):
        self.assertEqual(
            {value for value, _ in OrderTagging._meta.get_field("status").choices},
            {"PENDING", "DONE", "FAILED"},
        )

    def test_stat_defaults_are_all_null_and_computed_at_is_set(self):
        tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        stat = ProjectTagStat.objects.create(
            game=self.game, tag=tag, window_days=90, sample_count=3
        )
        for column in ("tp25", "tp50", "tp75", "hp25", "hp50", "hp75"):
            with self.subTest(column=column):
                self.assertIsNone(getattr(stat, column))
        self.assertIsNotNone(stat.computed_at)

    def test_stat_accepts_exactly_the_five_windows(self):
        """五档 30/60/90/180/360（业务方 2026-10-11 定）——一档一行、都能落库。"""
        tag = ProjectTagDict.objects.create(game=self.game, name="合成项目甲")
        stored = set()
        for days in (30, 60, 90, 180, 360):
            ProjectTagStat.objects.create(
                game=self.game, tag=tag, window_days=days, sample_count=3
            )
            stored.add(
                ProjectTagStat.objects.get(
                    game=self.game, tag=tag, window_days=days
                ).window_days
            )
        self.assertEqual(stored, {30, 60, 90, 180, 360})
