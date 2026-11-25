"""`backfill_hall_publisher`（D-0369 存量回填命令）的判据。

六条（每条都能红）：

1. 有人指派日志（人工行 `actor_id` 非空）且发单人为空 ⇒ 干跑**零写入**；
   `--apply` 后发单人 = 日志里那个人；
2. 只有机器日志行（`actor_id IS NULL`——`record()` fail-closed 的机器行口径）
   ⇒ **不回填**（计入「无任何指派日志」桶：机器指派按口径不算「有人发过单」）；
3. 发单人已有值 ⇒ **一律不动**——含「最后一次指派日志比现有发单人的写入更晚」的
   形状（钉死「只补空」口径，理由见命令 docstring）；
4. 日志 actor 解析不到 `User`（主键查不到、用户名也匹配不上）⇒ 跳过并**单独计数**；
5. 幂等：`--apply` 后再跑 ⇒ 命中 0（候选判据就是「发单人为空」）；
6. 同一张单两条人工指派日志 ⇒ 取**最新**一条（`created_at, id` 末条）的人。

另钉两条写法纪律：`--apply` **只动 `hall_publisher` 一列**（`updated_by` /
`updated_at` / `published_at` 逐列不变）且**不写审计行**（日志总数不变）。

⚠️ 断言一律打在**数据库行**上（E-0009）；夹具值一律合成值（E-0077）。
"""
from io import StringIO
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business.models import Game, Order, OrderStatus

#: 干跑 / 实跑两档都会用到的机器行来源名（合成值）。
MACHINE_ACTOR = "回填夹具-机器来源"


class BackfillHallPublisherTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="回填夹具游戏")
        self.alice = User.objects.create_user(username="backfill-alice", password="pw-a")
        self.bob = User.objects.create_user(username="backfill-bob", password="pw-b")
        self._seq = 0

    # ── 夹具 ─────────────────────────────────────────────────────────────
    def _order(self, **overrides):
        self._seq += 1
        fields = {
            "game": self.game,
            "order_no": f"2026110300000001{self._seq:04d}",
            "status": OrderStatus.IN_PROGRESS,
            "project_text": "回填夹具项目",
            "hall_price": "88.00",
            "updated_by": "合成-原修改人",
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _human_assign_log(self, order, user, *, action="performance.assign"):
        """人工指派日志行：走真实写入出口（`actor_id` = 该人主键，fail-closed 通过）。"""
        return record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=user.get_username(),
            actor_id=user.pk,
            target_kind="user",
            target_id=str(user.pk),
            order_no=order.order_no,
        )

    def _machine_assign_log(self, order):
        """机器指派日志行：`record()` 的 fail-closed 口径——机器行 = `actor_id` 必为 NULL。"""
        return record(
            log_type=ACTION_LOG_TYPES["performance.assign"],
            action="performance.assign",
            actor=MACHINE_ACTOR,
            actor_id=None,
            system=True,
            target_kind="user",
            target_id="0",
            order_no=order.order_no,
        )

    def _run(self, *args):
        out = StringIO()
        call_command(
            "backfill_hall_publisher",
            "--schema", connection.schema_name,
            *args,
            stdout=out,
        )
        return out.getvalue()

    def _row(self, order):
        return Order.objects.get(pk=order.pk)

    # ── ① 人工日志 + 空发单人：干跑零写入，--apply 回填 ─────────────────
    def test_dry_run_writes_nothing_then_apply_backfills_from_the_log(self):
        order = self._order()
        self._human_assign_log(order, self.alice)
        before_updated_at = self._row(order).updated_at
        logs_before = OperationLog.objects.count()

        out = self._run()  # 默认干跑

        self.assertIn("干跑", out)
        self.assertIn("可回填=1", out)
        row = self._row(order)
        self.assertIsNone(row.hall_publisher_id)          # 干跑零写入
        self.assertEqual(row.updated_at, before_updated_at)
        self.assertEqual(OperationLog.objects.count(), logs_before)  # 不写审计行

        out = self._run("--apply")

        self.assertIn("回填=1", out)
        self.assertIn(self.alice.username, out)           # 样例里点名人
        row = self._row(order)
        self.assertEqual(row.hall_publisher_id, self.alice.pk)

    def test_apply_touches_only_the_publisher_column(self):
        """`--apply` 只动 `hall_publisher` 一列：其余列（含审计行数）逐项不变。"""
        order = self._order(published_at=timezone.now() - timedelta(days=3))
        self._human_assign_log(order, self.bob)
        before = self._row(order)
        logs_before = OperationLog.objects.count()

        self._run("--apply")

        row = self._row(order)
        self.assertEqual(row.hall_publisher_id, self.bob.pk)      # 唯一被写的列
        self.assertEqual(row.updated_by, "合成-原修改人")          # ⛔ 不伪造最后修改人
        self.assertEqual(row.updated_at, before.updated_at)       # ⛔ 不推最后修改时刻
        self.assertEqual(row.published_at, before.published_at)   # ⛔ 不碰发单时间
        self.assertEqual(OperationLog.objects.count(), logs_before)

    # ── ② 只有机器行 ⇒ 不回填 ────────────────────────────────────────────
    def test_machine_only_log_does_not_backfill(self):
        order = self._order()
        self._machine_assign_log(order)

        out = self._run("--apply")

        self.assertIn("回填=0", out)
        # 机器行被人工判据（actor_id 非空）排除 ⇒ 落「无任何指派日志」桶。
        self.assertIn("无任何指派日志=1", out)
        self.assertIsNone(self._row(order).hall_publisher_id)

    # ── ③ 已有发单人 ⇒ 一律不动（只补空） ────────────────────────────────
    def test_non_empty_publisher_is_never_touched_even_when_log_is_later(self):
        """「最后一次指派日志比现有发单人的写入更晚」也不覆盖（钉死只补空口径）。"""
        order = self._order(hall_publisher=self.bob)   # 现有发单人 = bob（先写）
        self._human_assign_log(order, self.alice)      # 之后才发生的指派日志 = alice

        out = self._run("--apply")

        self.assertIn("有指派日志=0", out)   # 候选判据排掉了非空行 ⇒ 连日志都不找它
        self.assertEqual(self._row(order).hall_publisher_id, self.bob.pk)

    # ── ④ 解析不到用户 ⇒ 跳过并单独计数 ─────────────────────────────────
    def test_unresolvable_actor_is_skipped_and_counted(self):
        order = self._order()
        record(
            log_type=ACTION_LOG_TYPES["performance.assign"],
            action="performance.assign",
            actor="backfill-gone-user",      # 主键、用户名都匹配不到任何 User
            actor_id=987654321,
            target_kind="user",
            target_id="987654321",
            order_no=order.order_no,
        )

        out = self._run("--apply")

        self.assertIn("解析不到用户跳过=1", out)
        self.assertIn("回填=0", out)
        self.assertIsNone(self._row(order).hall_publisher_id)

    def test_actor_falls_back_to_username_when_pk_is_stale(self):
        """主键查不到、但 `actor` 用户名精确匹配得到 ⇒ 仍回填成那个人。"""
        order = self._order()
        record(
            log_type=ACTION_LOG_TYPES["performance.assign"],
            action="performance.assign",
            actor=self.alice.username,
            actor_id=987654321,              # 陈旧主键（查不到）
            target_kind="user",
            target_id="987654321",
            order_no=order.order_no,
        )

        out = self._run("--apply")

        self.assertIn("回填=1", out)
        self.assertEqual(self._row(order).hall_publisher_id, self.alice.pk)

    # ── ⑤ 幂等 ──────────────────────────────────────────────────────────
    def test_second_run_hits_zero(self):
        order = self._order()
        self._human_assign_log(order, self.alice)
        self._run("--apply")
        self.assertEqual(self._row(order).hall_publisher_id, self.alice.pk)  # 哨兵

        out = self._run("--apply")

        self.assertIn("空发单人活单=0", out)
        self.assertIn("回填=0", out)
        self.assertEqual(self._row(order).hall_publisher_id, self.alice.pk)

    # ── ⑥ 同一张单两条人工日志 ⇒ 取最新 ─────────────────────────────────
    def test_latest_log_wins(self):
        order = self._order()
        older = self._human_assign_log(order, self.alice)
        newer = self._human_assign_log(order, self.bob, action="performance.reassign")
        # 时刻显式拉开（`record()` 的默认时刻可能同微秒 ⇒ 判据里还有 id 决胜，这里钉时刻）。
        OperationLog.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        OperationLog.objects.filter(pk=newer.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        out = self._run("--apply")

        self.assertIn("回填=1", out)
        self.assertIn(self.bob.username, out)   # 样例点名的必须是**最新**那条的人
        self.assertEqual(self._row(order).hall_publisher_id, self.bob.pk)
