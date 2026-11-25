"""每单最多一张 `ready` 群二维码：DB 部分唯一索引 + 迁移去重（计划 Rev.2 Task 4）。

被钉住的三件事（⛔ 不是「模型里写了约束」就算数）：

① **真库约束**：第二张 `ready` 群码插入 ⇒ `IntegrityError`（`pending` 与别的单不受限）；
② **迁移去重**：从 `0090` 前放到 `0091` ⇒ 每单只剩 `max(id)`；空表不炸；反向只删索引、
   ⛔ 不复活被删的行（`RunPython` 的反向是 `noop`，去重不可逆）；
③ 回滚口径：约束是**部分唯一索引**（`CREATE UNIQUE INDEX … WHERE …`），回滚只能走
   `migrate_schemas … 0090`（反向 `RemoveConstraint` = `DROP INDEX`），⛔ 不是
   `ALTER TABLE … DROP CONSTRAINT`。

⚠️ 迁移测试照 `tests/test_jidanpu_upstream_models.py::UpstreamOfficialApiMigrationTests`
的配方：`MigrationExecutor` + `tearDown` **无条件**前放回叶子（DDL 会漏给同进程的下一条用例）。
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderScreenshot, OrderStatus

ORDER_NO = "20261025000000009999"
ORDER_NO_B = "20261025000000009998"


class OrderGroupQrUniqueTests(TenantTestCase):
    """模型 / 迁移产出的**部分唯一索引**真的在库里生效（判据打在 `IntegrityError` 上）。"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="qr-uniq", password="x")
        self.game = Game.objects.create(name="合成游戏-约束")
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS,
        )

    def _qr(self, key, *, order=None, status=OrderScreenshot.STATUS_READY):
        return OrderScreenshot.objects.create(
            order=order or self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key=key, uploaded_by=self.user, status=status,
        )

    def test_second_ready_group_qr_is_rejected(self):
        self._qr(f"test/orders/{ORDER_NO}/qr.a.png")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._qr(f"test/orders/{ORDER_NO}/qr.b.png")

    def test_pending_group_qr_is_allowed(self):
        """索引条件带 `status='ready'` ⇒ `pending` 群码不受限（上传中的那一张）。"""
        self._qr(f"test/orders/{ORDER_NO}/qr.a.png")
        self._qr(f"test/orders/{ORDER_NO}/qr.pending.png",
                 status=OrderScreenshot.STATUS_PENDING)
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR).count(), 2,
        )

    def test_the_limit_is_per_order_not_per_tenant(self):
        """另一张单的 `ready` 群码不受影响（约束是「**每单**一张」）。"""
        other = Order.objects.create(
            game=self.game, order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS,
        )
        self._qr(f"test/orders/{ORDER_NO}/qr.a.png")
        self._qr(f"test/orders/{ORDER_NO_B}/qr.b.png", order=other)
        self.assertEqual(
            OrderScreenshot.objects.filter(
                img_type=OrderScreenshot.IMG_GROUP_QR).count(), 2,
        )


def _business_leaf_nodes():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return [tuple(node) for node in loader.graph.leaf_nodes() if node[0] == "business"]


def _applied_migrations():
    return set(MigrationRecorder(connection).applied_migrations())


def _migrate_forward_to_leaves():
    """把当前 schema 前放到迁移图上的**全部叶子**（⛔ 不写死迁移名，见上游那份同名 helper）。"""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    MigrationExecutor(connection).migrate([tuple(node) for node in loader.graph.leaf_nodes()])


#: 本计划那条迁移（新的迁移插在它之后时**不用改这里**：只用于「退回它的父节点」）。
MIGRATION_NAME = "0092_orderscreenshot_uniq_order_ready_group_qr"
BEFORE_NODES = [("business", "0090_unlist_legacy_peer_hall_cards")]


def _historical_apps_at_before_nodes():
    """`BEFORE_NODES` 那个迁移状态点的**历史模型注册表**（Django「测试迁移」的标准形状：
    `MigrationLoader.project_state(nodes, at_end=True).apps`）。

    ⭐ 2026-11-01 评审修复：`_order()` / `_dup_rows()` 改用这里的模型插行——列清单
    **跟着迁移点走**，之后任何新迁移都不用再回来补脚手架（此前逐迁移 `ADD COLUMN`
    垒了五列，`0129` 的 `receipt_sync_notfound_count` 又把这条队补上了）。
    ⚠️ 注册表按**调用时的迁移图**构建 ⇒ 必须在 `migrate(BEFORE_NODES)` **之后**取
    （setUp 里取，存 `self._historical`；同一次构建出的 Order / OrderScreenshot 才能
    互相赋值 FK——两个注册表的历史模型互不为同一类，跨注册表赋 FK 会 ValidationError）。
    """
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return loader.project_state(BEFORE_NODES, at_end=True).apps


def _flush_deferred_constraints():
    """把本事务里 Django 那些 `DEFERRABLE INITIALLY DEFERRED` 外键触发事件现在就跑掉。

    ⚠️ 不这么做，`CREATE UNIQUE INDEX`（迁移里的 `AddConstraint`）会报
    `cannot CREATE INDEX ... because it has pending trigger events`（实测）——
    那不是迁移写错了，而是**测试外层事务**把「插入后的延迟外键检查」攒到了提交前。
    """
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _index_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "select 1 from pg_indexes where schemaname = current_schema() and indexname = %s",
            [name],
        )
        return cursor.fetchone() is not None


class DedupeMigrationTests(TenantTestCase):
    """迁移的去重与回滚口径（计划 Task 4 Step 6 / Task 6 Step 6）。"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="qr-migrate", password="x")
        self.game = Game.objects.create(name="合成游戏-迁移")
        self.assertIn(
            ("business", MIGRATION_NAME), _applied_migrations(),
            "前置：迁移没被应用，本用例无从退回（父节点/名字写错了？）",
        )
        MigrationExecutor(connection).migrate(BEFORE_NODES)
        # ⭐ 回归钉（当时全量跑才抓到）：本用例把库退到 `0092` 之前，
        #    而 `_order()` / `_dup_rows()` 原先用**当前最新模型**插行——列清单带着全部
        #    最新列（本次 `0129` 的 `receipt_sync_notfound_count` 就是这么炸的：
        #    `UndefinedColumn: column "receipt_sync_notfound_count" does not exist`）。
        #    旧行 shapes 是「每并进来一枚迁移就在 setUp 手工 `ADD COLUMN` 补一列」——
        #    那正是「下一枚迁移又会红」的形状（hall_card / order_label / external_id /
        #    upstream_hall_card / qr_fetch_suppress_until 五列都是这么垒出来的）。
        #    改用 **`BEFORE_NODES` 状态点的历史模型**（`project_state(...).apps`，
        #    Django「测试迁移」标准形状）：列清单**跟着迁移点走**，此后新迁移零维护。
        #    ⛔ 对应地，setUp 不再补脚手架列、tearDown 不再删它们（见下）。
        self._historical = _historical_apps_at_before_nodes()

    def tearDown(self):
        # ⚠️ 无条件前放：断言失败时也要把结构还给同进程的下一条用例。
        # ⚠️ 脚手架列已随历史模型方案整体退场（见 setUp 的注释）——
        #    前放迁移自己会把各列 `ADD COLUMN` 回来，无需（也不该）在这里 DROP。
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        _migrate_forward_to_leaves()
        super().tearDown()

    def _order(self, order_no=ORDER_NO):
        # 历史模型（`0092` 之前的状态）：列清单只含那个时点存在的列 ⇒ INSERT 不会带上
        # 后来迁移才加的列。⚠️ FK 一律走 **`_id` 直赋**（`game_id` / `uploaded_by_id`）：
        # 历史模型的 FK 实例校验只认**同一注册表**的历史实例，当前 `Game` / `User`
        # 实例塞进去会 ValidationError。
        HistoricalOrder = self._historical.get_model("business", "Order")
        return HistoricalOrder.objects.create(
            game_id=self.game.pk, order_no=order_no, status=OrderStatus.IN_PROGRESS,
        )

    def _dup_rows(self, order, count=3):
        # 迁移前那条索引已被反向删掉 ⇒ 这里能造出重复行（正是生产迁移前的形态）。
        HistoricalScreenshot = self._historical.get_model("business", "OrderScreenshot")
        # ⚠️ 取值常量用**当前模块**的（`IMG_GROUP_QR` / `STATUS_READY` 是 models.py 里的
        #    类常量，历史模型从迁移文件重建、不带它们；两者都是跨迁移点稳定的字符串值，
        #    去重迁移 0092 的 WHERE 用的就是这些值）。
        return [
            HistoricalScreenshot.objects.create(
                order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
                object_key=f"test/orders/{order.order_no}/qr.dup-{index}.png",
                uploaded_by_id=self.user.pk, status=OrderScreenshot.STATUS_READY,
            )
            for index in range(count)
        ]

    def test_duplicates_are_reduced_to_the_latest_row_per_order(self):
        order = self._order()
        rows = self._dup_rows(order)
        keep_id = rows[-1].pk
        _flush_deferred_constraints()

        MigrationExecutor(connection).migrate([("business", MIGRATION_NAME)])

        # ⚠️ 断言读行也走历史模型：此刻库还在 `0092` 状态点，当前模型的 SELECT 会
        #    带上后来才加的列（同一形态的坑）；`0090` 与 `0092` 的截图模型字段集相同
        #    （0092 只加约束）⇒ 用它读安全。
        self.assertEqual(
            list(
                self._historical.get_model("business", "OrderScreenshot")
                .objects.filter(order=order)
                .values_list("pk", flat=True)
            ),
            [keep_id],
        )
        self.assertTrue(_index_exists("uniq_order_ready_group_qr"))

    def test_an_empty_table_does_not_break_the_migration(self):
        self._order()

        MigrationExecutor(connection).migrate([("business", MIGRATION_NAME)])

        self.assertEqual(OrderScreenshot.objects.count(), 0)
        self.assertTrue(_index_exists("uniq_order_ready_group_qr"))

    def test_reverse_only_drops_the_index_and_never_resurrects_rows(self):
        order = self._order()
        rows = self._dup_rows(order)
        keep_id = rows[-1].pk
        _flush_deferred_constraints()
        MigrationExecutor(connection).migrate([("business", MIGRATION_NAME)])

        MigrationExecutor(connection).migrate(BEFORE_NODES)

        self.assertFalse(_index_exists("uniq_order_ready_group_qr"))
        # ⚠️ 此刻库已退回 `0090`（比上面那条还早）⇒ 断言读行同样走历史模型。
        self.assertEqual(
            list(
                self._historical.get_model("business", "OrderScreenshot")
                .objects.filter(order=order)
                .values_list("pk", flat=True)
            ),
            [keep_id],
            "反向迁移复活了被去重删掉的行（RunPython 反向必须是 noop）",
        )
