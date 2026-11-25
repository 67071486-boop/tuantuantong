"""`OrderSource` / `ShopType` 的模型层判据（spec §8 #1 与 #2）。

**#1**：字段长度上限、`shop_type` 的 choices、`order_by` 默认 0、`Meta.ordering`。
**#2**：唯一约束 `uniq_order_source_name` **是 per-schema 的**——
同租户同名被拒（`IntegrityError`）、**跨租户同名允许**。

⚠️ **两条断言纪律**（`AGENTS.md` §5）：

1. **期望值写字面值，不写自指导出常量**（E-0024）：choices 断言钉 `[0,10,15,20,30,40,50,60,70]`，
   **不写** `[c.value for c in ShopType]`（那与 `ShopType` 是同一个符号，改坏了也不红）；
   `shop_type` / `order_by` 的默认值同样钉字面值 `0`。
2. **跨租户的否定/正向断言都不用主键指认「另一个租户的那一行」**：两个 schema 的主键序列
   **各自从 1 开始**，`filter(id=…)` 在另一个租户里命中的是**它自己的行**。
   本文件用 `name`（租户内唯一）＋ `remark`（两租户取值不同）作判据。
"""
from django.db import IntegrityError, models, transaction
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, OrderSourceGroup, ShopType
from apps.platform.provisioning import provision_tenant

#: 第二个租户（唯一约束是不是 per-schema，只有两个租户都建行才能回答）。
DOMAIN_B = "order-src-model-b.test.local"
SCHEMA_B = "order_src_model_b"


class OrderSourceModelTests(TenantTestCase):
    """spec §8 #1：字段上限 / choices 字面值 / 默认值 / ordering。"""

    def test_choices_are_literal(self):
        """⚠️ 钉**字面值**，不写 `ShopType.values`（自指断言，E-0024）。

        两处文案不一致是**故意的**（对标弹窗「淘宝/天猫」vs 列表「非店铺/淘宝/天猫」），
        这里只钉枚举自身的 label（列表那一套在页面层，见 spec §2.3）。
        """
        self.assertEqual([c.value for c in ShopType], [0, 10, 15, 20, 30, 40, 50, 60, 70])
        # 9 个 label **全部**钉字面值（T1 评审 M2：原先只钉 2 个 ⇒ 中间 7 个改坏不会红）。
        self.assertEqual(
            [c.label for c in ShopType],
            ["淘宝/天猫", "拼多多", "京东", "闲鱼", "抖音", "快手", "唯品会", "小红书", "其它平台"],
        )

    def test_field_limits_and_defaults(self):
        self.assertEqual(OrderSource._meta.get_field("name").max_length, 10)
        self.assertEqual(OrderSource._meta.get_field("remark").max_length, 50)
        # `remark` 可空串默认（编辑时清空描述的唯一表达；T1 评审 M2 原先漏钉）。
        self.assertTrue(OrderSource._meta.get_field("remark").blank)
        self.assertEqual(OrderSource._meta.get_field("remark").default, "")
        self.assertEqual(OrderSource._meta.get_field("order_by").default, 0)
        # 钉字面值 0（不写 `ShopType.TAOBAO`：那样改枚举值也不会红，E-0024）。
        self.assertEqual(OrderSource._meta.get_field("shop_type").default, 0)
        # 钉**模型字段的 choices 接线**本身（T1 评审 M1 的实测：只把 `choices=ShopType.choices`
        # 换成错误字面值时，原 5 条测试**全绿**——枚举没动、字段却坏了）。
        self.assertEqual(
            [tuple(c) for c in OrderSource._meta.get_field("shop_type").choices],
            [
                (0, "淘宝/天猫"), (10, "拼多多"), (15, "京东"), (20, "闲鱼"),
                (30, "抖音"), (40, "快手"), (50, "唯品会"), (60, "小红书"),
                (70, "其它平台"),
            ],
        )
        self.assertEqual(OrderSource._meta.ordering, ["-order_by", "id"])
        self.assertEqual(
            [c.name for c in OrderSource._meta.constraints], ["uniq_order_source_name"]
        )

    def test_default_order_by_is_zero_and_ordering_ties_break_by_id(self):
        """排序数值大在前；同值按 `id` 稳定；未传时默认 0。"""
        row = OrderSource.objects.create(name="甲")
        OrderSource.objects.create(name="乙", order_by=5)
        OrderSource.objects.create(name="丙")
        OrderSource.objects.create(name="丁", order_by=-3)
        self.assertEqual(row.order_by, 0)
        # ⚠️ 边界判据用 `is not None`（E-0021 的族）：`0` 是有意义的值。
        self.assertIsNotNone(row.order_by)
        self.assertEqual(
            list(OrderSource.objects.values_list("name", flat=True)),
            ["乙", "甲", "丙", "丁"],
        )

    def test_duplicate_name_in_same_tenant_raises_integrity_error(self):
        """同租户同名 ⇒ 库层拒绝（`uniq_order_source_name`）。"""
        OrderSource.objects.create(name="甲")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderSource.objects.create(name="甲")


class OrderSourceCrossTenantTests(TenantTestCase):
    """spec §8 #2 的后半：**跨租户同名允许**（证明约束是 per-schema 的）。

    判据用 `name`（租户内唯一）＋ `remark`（两租户取值不同），
    **不用主键**：两个 schema 的主键序列各自从 1 开始。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：用例内部的 schema DDL 与外层事务相冲。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单来源模型隔离租户 B", domain=DOMAIN_B
        )

    def test_same_name_across_tenants_is_allowed(self):
        shared_name = "同名来源"
        remark_a = "只在租户 A 的描述"
        remark_b = "只在租户 B 的描述"

        with tenant_context(self.tenant):
            OrderSource.objects.create(name=shared_name, remark=remark_a)

        # 跨租户同名**允许**：若约束不是 per-schema，这一行会抛 IntegrityError。
        with tenant_context(self.tenant_b):
            OrderSource.objects.create(name=shared_name, remark=remark_b)
            # 判据：B 里按 name 只能查到 B 自己那一行（remark 是 B 的值）。
            self.assertEqual(
                list(
                    OrderSource.objects.filter(name=shared_name).values_list(
                        "remark", flat=True
                    )
                ),
                [remark_b],
            )

        # 反向对照：A 的那一行仍在，且没有被 B 的写入覆盖。
        with tenant_context(self.tenant):
            self.assertEqual(
                list(
                    OrderSource.objects.filter(name=shared_name).values_list(
                        "remark", flat=True
                    )
                ),
                [remark_a],
            )


class OrderSourceGroupModelTests(TenantTestCase):
    """`OrderSourceGroup` 的模型层判据（2026-10-26，计划 §2）。

    与 `OrderSourceModelTests` 同形（E-0024：期望值写字面值，不从模型自指导出）。
    """

    def test_field_limits_defaults_and_ordering(self):
        self.assertEqual(OrderSourceGroup._meta.get_field("name").max_length, 20)
        self.assertEqual(OrderSourceGroup._meta.get_field("order_by").default, 0)
        self.assertEqual(OrderSourceGroup._meta.ordering, ["-order_by", "id"])
        self.assertEqual(
            [c.name for c in OrderSourceGroup._meta.constraints],
            ["uniq_order_source_group_name"],
        )

    def test_default_order_by_is_zero_and_ordering_ties_break_by_id(self):
        row = OrderSourceGroup.objects.create(name="甲组")
        OrderSourceGroup.objects.create(name="乙组", order_by=5)
        OrderSourceGroup.objects.create(name="丙组")
        self.assertEqual(row.order_by, 0)
        self.assertIsNotNone(row.order_by)  # 0 是合法值（E-0021 的族）
        self.assertEqual(
            list(OrderSourceGroup.objects.values_list("name", flat=True)),
            ["乙组", "甲组", "丙组"],
        )

    def test_duplicate_name_in_same_tenant_raises_integrity_error(self):
        OrderSourceGroup.objects.create(name="同名组")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderSourceGroup.objects.create(name="同名组")

    def test_source_group_fk_is_set_null_and_related_name(self):
        """拍板②：删分组 ⇒ 来源留下、`group` 置空（⛔ 不是级联删除）。"""
        field = OrderSource._meta.get_field("group")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertIs(field.remote_field.on_delete, models.SET_NULL)
        self.assertEqual(field.remote_field.related_name, "sources")

        group = OrderSourceGroup.objects.create(name="甲组")
        source = OrderSource.objects.create(name="来源甲", group=group)
        self.assertEqual(list(group.sources.values_list("name", flat=True)), ["来源甲"])
        group.delete()
        source.refresh_from_db()
        self.assertIsNone(source.group_id)
