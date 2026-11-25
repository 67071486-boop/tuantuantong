"""P4-1「订单价格段修订」的迁移探针（spec §9 #10 四条 + §3.1 硬约束 1 的结构兜底）。

⚠️ 这里的断言必须打在**数据库列**上（`information_schema`），**不是**打在模型上：
查模型时列还在也会「通过」——模型属性是声明式的，删列后的模型与迁移后的库
在「字段存在」这件事上给出同一个答案，判别力为零。

⚠️ 但「列断言」测不到**顺序**：测试库每次从空表迁建，回填改 0 行，
于是删掉/挪后那条 `RunPython` 全量门禁照样全绿（P4-1 的变异 B/C 实测）。
所以下面 `test_runpython_backfill_precedes_total_price_alter` 单独钉**结构**。
"""
import importlib
from decimal import Decimal

from django.db import connection, migrations
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order


class PriceRevisionMigrationTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _columns(self):
        with connection.cursor() as c:
            c.execute(
                """SELECT column_name, is_nullable, column_default
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = 'business_order'""",
                [connection.schema_name],
            )
            return {r[0]: (r[1], r[2]) for r in c.fetchall()}

    def test_total_price_is_not_null_with_zero_default(self):
        cols = self._columns()
        self.assertIn("total_price", cols)
        self.assertEqual(cols["total_price"][0], "NO")
        order = Order.objects.create(game=self.game, order_no="20260920000000000031")
        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("0.00"))

    def test_deposit_columns_are_gone(self):
        cols = self._columns()
        self.assertNotIn("deposit_safe", cols)
        self.assertNotIn("deposit_efficiency", cols)

    def test_refund_fee_column_exists(self):
        cols = self._columns()
        self.assertIn("refund_fee", cols)
        self.assertEqual(cols["refund_fee"][0], "NO")

    def test_backfill_function_is_importable_and_idempotent(self):
        mod = importlib.import_module("apps.business.migrations.0010_p4_price_revision")
        order = Order.objects.create(game=self.game, order_no="20260920000000000032")
        changed = mod.backfill_total_price_nulls(__import__("django").apps.apps, None)
        self.assertEqual(changed, 0)
        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("0.00"))


class PriceRevisionMigrationStructureTests(TenantTestCase):
    """spec §3.1 硬约束 1 的结构兜底：**回填必须在 `AlterField(total_price)` 之前**。

    判别力：把 `RunPython` 挪到 `AlterField` **之后** → 红；
    把 `RunPython` 整个删掉 → 红。列断言（上一个类）这两种改坏都**测不出来**——
    测试库从空表迁建，回填改 0 行，有没有它、在不在前面，结果完全一样。

    ⚠️ 这条断言的是**迁移文件的结构**，不是「回填真的改到了行」；
    后者要 `MigrationExecutor` 退库插 `NULL` 再迁，超出 P4-1 范围（已在报告里登记）。
    """

    MIGRATION = "apps.business.migrations.0010_p4_price_revision"

    def _total_price_alter_index(self, operations):
        """`AlterField(order.total_price)` 在 operations 里的下标；找不到就失败（不静默跳过）。"""
        for i, op in enumerate(operations):
            if not isinstance(op, migrations.AlterField):
                continue
            if op.model_name == "order" and op.name == "total_price":
                return i
        self.fail("0010 迁移里没有 AlterField(order.total_price)——迁移被改坏了？")

    def test_runpython_backfill_precedes_total_price_alter(self):
        mod = importlib.import_module(self.MIGRATION)
        ops = list(mod.Migration.operations)
        alter_at = self._total_price_alter_index(ops)

        # ① 回填操作**存在**
        # 断言的是「operations 里有这个条目」而不是「模块里有个同名函数」：
        # 变异 B 删掉的正是条目，函数本身还在模块里，所以只查函数会假绿（P4-1 实测）。
        run_python_at = [i for i, op in enumerate(ops) if isinstance(op, migrations.RunPython)]
        self.assertEqual(
            len(run_python_at),
            1,
            f"0010 迁移应当恰有 1 个 RunPython 回填，实际 {len(run_python_at)} 个",
        )

        # ② 它的下标**严格早于** `AlterField(total_price)`
        # —— `total_price` 转非空之前不回填，PostgreSQL 会以
        # `column "total_price" contains null values` 让迁移中途失败。
        self.assertLess(
            run_python_at[0],
            alter_at,
            "回填 RunPython 必须排在 AlterField(total_price) 之前（spec §3.1 硬约束 1）",
        )

        # ③ 顺带确认那一条回填的就是本迁移的回填函数（防止塞进一个空 RunPython 骗过 ①②）
        self.assertIs(ops[run_python_at[0]].code, mod.backfill_total_price_nulls)
