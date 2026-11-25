"""批次 5 · 线 E：历史回填数据迁移 `0070_backfill_order_time_fields` 的判据。

对应设计 §2.3 与计划 E3 的三条判据：

1. **只填 `NULL` 行**（幂等）：已有值**永不被覆盖**，重跑一个字节都不改；
2. **找不到事件 ⇒ 留空**（⛔ 不许拿 `created_at` / `updated_at` 兜底冒充）；
3. **填的是那一条事件的时刻**（⛔ 不是「跑完不报错」）——本文件的核心纪律：
   断言打在**数据库行**上，且钉的是**具体的那个时刻**。

⚠️ 做法是**直接调迁移里的那两个函数**（`importlib.import_module` 那个文件名以数字开头的
模块，与本仓既有先例 `tests/test_p4_migration.py` 同款），把 `django.apps.apps` 当
`apps` 参数传进去：那两个函数只用到 `Order.published_at` / `submitted_at` 与
`OperationLog` / `Performance` 的**现存**列，所以拿真实 app registry 跑就是它们的
生产行为。⛔ 不重放整棵迁移图（本仓既有的迁移探针也不这么做，代价与理由见那个文件的
docstring）。

⚠️ 两个来源**取先发生的**那条口径（业务方拍板）必须有**判别力**：
抛单比接手早 / 接手比抛单早，两个方向各一条用例（只测一个方向的话，把 `min` 写成「先看到的那个」
也照样绿）。
"""
import importlib
from datetime import timedelta

from django.db import migrations
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance

MIGRATION = "apps.business.migrations.0070_backfill_order_time_fields"

#: 与迁移文件里那条常量同值——但**这里独立写一遍**（E-0024：期望值写字面值，
#: ⛔ 不从被测模块派生；否则常量被改坏时两侧一起漂、用例照样绿）。
PENDING_REVIEW_TEXT = "待验收"
#: 同上：抛大厅那个 `action` 的日志值。
THROW_ACTION = "order.throw_to_hall"


class _BackfillTestCase(TenantTestCase):
    """共用夹具：一个游戏 + 一个「迁移模块」句柄。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.mod = importlib.import_module(MIGRATION)

    def _order(self, n, **kw):
        return Order.objects.create(
            game=self.game, order_no=f"2026101500{n:010d}", **kw
        )

    def _log(self, order, *, action="order.transition", to_value="", at):
        """写一行操作日志（⛔ 不走 `record()`：`created_at` 是 `default=timezone.now`
        ⇒ 只有直写才能把「历史时刻」摆到指定的位置）。"""
        return OperationLog.objects.create(
            log_type=10, action=action, target_kind="order",
            target_id=order.order_no, order_no=order.order_no,
            field_name="status", to_value=to_value, summary="夹具",
            created_at=at,
        )

    def _run(self):
        """跑那两条回填（顺序与迁移里的 `operations` 一致）。"""
        self.mod.backfill_submitted_at(__import__("django").apps.apps, None)
        self.mod.backfill_published_at(__import__("django").apps.apps, None)


class SubmittedAtBackfillTests(_BackfillTestCase):
    """`submitted_at` ← `OperationLog` 里 `to_value='待验收'` 的**最早** `created_at`。"""

    def test_fills_the_earliest_pending_review_log_time(self):
        """填的是**那一条日志的时刻**，且多条时取**最早**（钉值，⛔ 不是「非空」）。"""
        order = self._order(1)
        older = timezone.now() - timedelta(days=3)
        newer = timezone.now() - timedelta(days=1)
        # 故意**先写晚的、再写早的**：这样「取最早」与「取最后一条」才可区分。
        self._log(order, to_value=PENDING_REVIEW_TEXT, at=newer)
        self._log(order, to_value=PENDING_REVIEW_TEXT, at=older)
        self.assertIsNone(Order.objects.get(pk=order.pk).submitted_at)   # 阴性对照
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).submitted_at, older)

    def test_leaves_it_empty_when_there_is_no_pending_review_event(self):
        """⛔ **不猜**：没有任何「待验收」事件 ⇒ 留空（不许拿 `created_at` 兜底）。

        判别力：把回填改成「找不到事件就写 `order.created_at`」，本条当场红。
        """
        order = self._order(2)
        # 同一条单上有**别的**动作的日志（证明「有日志」≠「有交单事件」）。
        self._log(order, action="order.update", to_value="备注", at=timezone.now())
        self._run()
        row = Order.objects.get(pk=order.pk)
        self.assertIsNone(row.submitted_at)
        self.assertIsNotNone(row.created_at)          # 哨兵：兜底用的那个值确实存在

    def test_does_not_match_the_enum_name_instead_of_the_chinese_label(self):
        """`to_value` 是**中文标签**，⛔ 不是枚举名 —— 拿枚举名当判据会静默 0 行。

        判别力：把迁移里的 `STATUS_TEXT_PENDING_REVIEW` 改成 `"PENDING_REVIEW"`，
        本用例红（而上面那条「有事件就该填上」的用例也会红）。
        """
        order = self._order(3)
        at = timezone.now() - timedelta(hours=5)
        self._log(order, to_value=PENDING_REVIEW_TEXT, at=at)
        self._log(order, to_value="PENDING_REVIEW", at=timezone.now())   # 不是本仓的写法
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).submitted_at, at)

    def test_rerunning_does_not_overwrite_an_existing_value(self):
        """**幂等**：已有值（哪怕比日志里的时刻晚）**一个字不改**。

        判别力：去掉 `.update()` 的 `submitted_at__isnull=True`，本条当场红。
        """
        order = self._order(4)
        kept = timezone.now() - timedelta(hours=1)
        Order.objects.filter(pk=order.pk).update(submitted_at=kept)
        self._log(order, to_value=PENDING_REVIEW_TEXT, at=timezone.now() - timedelta(days=2))
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).submitted_at, kept)
        # 再跑一次（模拟「换个租户批次重跑」）：仍然不动。
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).submitted_at, kept)


class PublishedAtBackfillTests(_BackfillTestCase):
    """`published_at` ← min(最早抛大厅日志, 该单最早的 `Performance.claimed_at`)。"""

    def _perf(self, order, claimed_at):
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, claimed_at=claimed_at,
        )

    def test_fills_from_the_throw_log_when_there_is_no_claim(self):
        order = self._order(10)
        thrown = timezone.now() - timedelta(days=4)
        self._log(order, action=THROW_ACTION, to_value="88.00", at=thrown)
        self.assertIsNone(Order.objects.get(pk=order.pk).published_at)   # 阴性对照
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).published_at, thrown)

    def test_fills_from_the_earliest_claim_across_takers(self):
        """只有接手（没有抛大厅日志）也能填上，且取**最早**那一条绩效行。

        ⚠️ 一张单最多一条 MAIN 行（部分唯一索引 `uniq_order_main_taker`）⇒ 「取最早」
        这条口径只能靠**另一张单**上更晚的接手行来做对照：两张单各自只写一条，
        比对「同一时刻的两张单都填得上」（而不是在同一条单上堆两行）。
        """
        early_order = self._order(11)
        late_order = self._order(18)
        older = timezone.now() - timedelta(days=6)
        self._perf(early_order, older)
        self._perf(late_order, timezone.now() - timedelta(days=1))
        self._run()
        self.assertEqual(
            Order.objects.get(pk=early_order.pk).published_at, older
        )
        self.assertEqual(
            Order.objects.get(pk=late_order.pk).published_at,
            Performance.objects.get(order=late_order).claimed_at,
        )

    def test_takes_the_earlier_of_the_two_sources(self):
        """两个来源**取先发生的**——两个方向各一条（只测一个方向时把 `min` 写成
        「先看到的那个」照样绿）。"""
        thrown_first = self._order(12)
        thrown = timezone.now() - timedelta(days=7)
        self._log(thrown_first, action=THROW_ACTION, to_value="88.00", at=thrown)
        self._perf(thrown_first, timezone.now() - timedelta(days=2))
        claimed_first = self._order(13)
        self._perf(claimed_first, timezone.now() - timedelta(days=8))
        self._log(
            claimed_first, action=THROW_ACTION, to_value="88.00",
            at=timezone.now() - timedelta(days=3),
        )
        self._run()
        self.assertEqual(Order.objects.get(pk=thrown_first.pk).published_at, thrown)
        self.assertEqual(
            Order.objects.get(pk=claimed_first.pk).published_at,
            Performance.objects.get(order=claimed_first).claimed_at,
        )

    def test_leaves_it_empty_when_neither_event_exists(self):
        """⛔ **不猜**：两个来源都没有 ⇒ 留空（判别力同 `submitted_at` 那条）。"""
        order = self._order(14)
        self._log(order, action="order.update", to_value="备注", at=timezone.now())
        self._run()
        row = Order.objects.get(pk=order.pk)
        self.assertIsNone(row.published_at)
        self.assertIsNotNone(row.created_at)          # 哨兵

    def test_a_throw_log_without_a_matching_order_is_ignored(self):
        """日志里的单号在本租户**没有对应订单** ⇒ 跳过，⛔ 不抛异常、也不写别的行。

        （生产上这不是假想：日志比订单活得久，而租户里可能有跨库搬过来的单号。）
        """
        order = self._order(15)
        at = timezone.now() - timedelta(days=1)
        OperationLog.objects.create(
            log_type=10, action=THROW_ACTION, target_kind="order",
            target_id="20261015009999999999", order_no="20261015009999999999",
            field_name="status", summary="夹具", created_at=at,
        )
        self._run()
        self.assertIsNone(Order.objects.get(pk=order.pk).published_at)

    def test_rerunning_does_not_overwrite_an_existing_value(self):
        order = self._order(16)
        kept = timezone.now() - timedelta(minutes=30)
        Order.objects.filter(pk=order.pk).update(published_at=kept)
        self._log(
            order, action=THROW_ACTION, to_value="88.00",
            at=timezone.now() - timedelta(days=9),
        )
        self._perf(order, timezone.now() - timedelta(days=5))
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).published_at, kept)
        self._run()
        self.assertEqual(Order.objects.get(pk=order.pk).published_at, kept)

    def test_a_soft_deleted_order_is_not_touched(self):
        """软删单**不填**：`Order.objects` 是默认 manager（排除已删行）。

        ⚠️ 这是**有意**的口径（本仓软删的全部读路径都走默认 manager），登记在此以免
        将来有人把它读成漏行。
        """
        order = self._order(17)
        deleted_at = timezone.now()
        Order.all_objects.filter(pk=order.pk).update(deleted_at=deleted_at)
        self._log(
            order, action=THROW_ACTION, to_value="88.00",
            at=timezone.now() - timedelta(days=2),
        )
        self._run()
        self.assertIsNone(Order.all_objects.get(pk=order.pk).published_at)


class BackfillMigrationStructureTests(TenantTestCase):
    """迁移文件自己的结构：两个 `RunPython`、且加列那个迁移**排在它前面**。"""

    def test_it_is_a_separate_file_from_the_add_columns_migration(self):
        """加列与回填**分成两个文件**（设计 §2.3 逐字要求）——合成一个就没法单独重跑回填。"""
        add_columns = importlib.import_module(
            "apps.business.migrations.0069_order_time_fields"
        )
        backfill = importlib.import_module(MIGRATION)
        self.assertIsNot(add_columns.Migration, backfill.Migration)
        self.assertIn(
            ("business", "0069_order_time_fields"),
            [tuple(dep) for dep in backfill.Migration.dependencies],
        )

    def test_both_columns_have_a_runpython_backfill(self):
        mod = importlib.import_module(MIGRATION)
        run_python = [
            op for op in mod.Migration.operations
            if isinstance(op, migrations.RunPython)
        ]
        self.assertEqual(len(run_python), 2)
        # 两条都**不可逆**（`noop` 反向）：回填是可重跑的幂等操作，反向「撤销」没有意义
        # （也无法区分「迁移填的」与「后来业务写的」）。
        for op in run_python:
            self.assertEqual(op.reverse_code, migrations.RunPython.noop)


class ColumnsAreNullableTests(TenantTestCase):
    """加列那个迁移的两条判据：两列**都可空**（存量行不会被回填成假值）。

    ⚠️ 打在**数据库列**上（`information_schema`），⛔ 不是打在模型上：模型属性是声明式的，
    判别力为零（同 `tests/test_p4_migration.py` 的既有纪律）。
    """

    def _columns(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT column_name, is_nullable, data_type
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = 'business_order'""",
                [connection.schema_name],
            )
            return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    def test_both_new_columns_exist_and_are_nullable_datetime(self):
        columns = self._columns()
        for name in ("published_at", "submitted_at"):
            with self.subTest(column=name):
                self.assertIn(name, columns)
                self.assertEqual(columns[name][0], "YES")                 # 可空
                self.assertEqual(columns[name][1], "timestamp with time zone")

    def test_a_fresh_order_row_defaults_to_null(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game, order_no="20261015000000000001",
            status=OrderStatus.UNASSIGNED,
        )
        row = Order.objects.get(pk=order.pk)
        self.assertIsNone(row.published_at)
        self.assertIsNone(row.submitted_at)
