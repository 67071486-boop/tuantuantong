"""租户感知的群码重复审计/去重命令（计划 Rev.2 Task 5）。

判据打在**真实出口**上（数据库行 + 对象存储 + 删对象的**时刻**），⛔ 不打 logger：

- 默认 **dry-run**：一行不删、一个对象不删；
- `--execute`：每单保留 `max(id)`，其余行删掉（**只碰 `img_type=1 AND status='ready'`**）；
- 第二次 `--execute` 命中 0（幂等）；
- `--delete-objects`：**行先删、事务提交后**才删对象（用 `captureOnCommitCallbacks` 驱动；
  在删对象那一刻回查库，被删的行必须已经不在了）；
- `--limit N` 真的限轮次；`--schema` 指到不存在的 schema ⇒ `CommandError`。

⚠️ 约束已在 `0091` 上（每单一张 ready 群码）⇒ 造重复行要先**临时移除**那条部分唯一索引
（`schema_editor.remove_constraint`）。本类的 schema 由 `TenantTestCase` 按类建/删，
所以移除后不必还原（⛔ 不把还原放到 `tearDown`：那时造过行，再 `CREATE INDEX` 会撞
「pending trigger events」）。
"""
from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderScreenshot, OrderStatus

ORDER_NO = "20261025000000010001"
ORDER_NO_B = "20261025000000010002"
ORDER_NO_C = "20261025000000010003"
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


class AuditOrderGroupQrTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        constraint = next(
            item for item in OrderScreenshot._meta.constraints
            if item.name == "uniq_order_ready_group_qr"
        )
        with connection.schema_editor() as editor:
            editor.remove_constraint(OrderScreenshot, constraint)

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="audit-qr", password="x")
        self.game = Game.objects.create(name="合成游戏-审计")

    def _order(self, order_no=ORDER_NO):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
        )

    def _qr(self, order, suffix, *, status=OrderScreenshot.STATUS_READY, shared=False):
        """造一张群码行 + 真字节。

        ⚠️ `shared=True` ⇒ key 在共享前缀下（D-0316 之后新群码的形状），字节**不归本租户独有**。
        """
        key = (
            f"shared-orders/{order.order_no}/qr.{suffix}.png" if shared
            else f"test/orders/{order.order_no}/qr.{suffix}.png"
        )
        from apps.core.storage import get_storage

        get_storage().put_bytes(key, PNG_BODY, content_type="image/png")
        return OrderScreenshot.objects.create(
            order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key=key, uploaded_by=self.user, status=status,
        )

    def _run(self, *args, **kwargs):
        out = StringIO()
        call_command(
            "audit_order_group_qr", "--schema", connection.schema_name,
            *args, stdout=out, **kwargs,
        )
        return out.getvalue()

    def test_dry_run_is_the_default_and_deletes_nothing(self):
        order = self._order()
        rows = [self._qr(order, index) for index in range(3)]
        from apps.core.storage import get_storage

        out = self._run()

        self.assertIn("dry-run", out)
        self.assertEqual(
            list(OrderScreenshot.objects.filter(order=order).values_list("pk", flat=True)),
            [row.pk for row in rows],
        )
        for row in rows:
            self.assertIsNotNone(get_storage().head(row.object_key))

    def test_execute_keeps_only_the_latest_row_and_is_idempotent(self):
        order = self._order()
        rows = [self._qr(order, index) for index in range(3)]
        keep_id = rows[-1].pk

        self._run("--execute")

        self.assertEqual(
            list(OrderScreenshot.objects.filter(order=order).values_list("pk", flat=True)),
            [keep_id],
        )
        # 第二次：命中 0，一行都不再删。
        out = self._run("--execute")
        self.assertIn("orders=0", out)
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 1)

    def test_only_ready_group_qr_rows_are_touched(self):
        """订单截图 / `pending` 群码都不在命令范围（⛔ 不拿宽条件补删）。"""
        order = self._order()
        ready = [self._qr(order, index) for index in range(2)]
        pending = self._qr(order, "pending", status=OrderScreenshot.STATUS_PENDING)
        shot = OrderScreenshot.objects.create(
            order=order, img_type=OrderScreenshot.IMG_ORDER,
            object_key=f"shared-orders/{ORDER_NO}/shot.jpg",
            uploaded_by=self.user, status=OrderScreenshot.STATUS_READY,
        )

        self._run("--execute")

        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 3)
        self.assertTrue(OrderScreenshot.objects.filter(pk=pending.pk).exists())
        self.assertTrue(OrderScreenshot.objects.filter(pk=shot.pk).exists())
        self.assertTrue(OrderScreenshot.objects.filter(pk=ready[-1].pk).exists())

    def test_delete_objects_happens_after_the_rows_are_gone(self):
        """`--delete-objects`：删除对象的那一刻，被删的行必须**已经不在**（先删行、提交后再删对象）。"""
        order = self._order()
        rows = [self._qr(order, index) for index in range(3)]
        drop_keys = [row.object_key for row in rows[:-1]]
        seen_row_counts: list[int] = []

        storage = mock.Mock()
        storage.delete.side_effect = lambda _key: seen_row_counts.append(
            OrderScreenshot.objects.filter(order=order).count()
        )

        with mock.patch(
            "apps.business.management.commands.audit_order_group_qr.get_storage",
            return_value=storage,
        ), self.captureOnCommitCallbacks(execute=True):
            self._run("--execute", "--delete-objects")

        self.assertEqual(sorted(call.args[0] for call in storage.delete.call_args_list),
                         sorted(drop_keys))
        # 删对象的时候库里只剩保留的那一张（说明行删除已在提交前生效）。
        self.assertEqual(set(seen_row_counts), {1})

    def test_shared_prefix_group_qr_bytes_are_never_deleted(self):
        """⭐ D-0316 / B1：`--delete-objects` 只删**本租户前缀**的群码字节。

        共享前缀的群码字节链上别的租户可能还在用 ⇒ **行照删、字节不删**（判据 = 存储层
        真实出口：`storage.delete` 的调用参数里只有那条本租户前缀 key）。
        """
        order = self._order()
        local = self._qr(order, "local")
        shared = [self._qr(order, index, shared=True) for index in range(2)]
        storage = mock.Mock()

        with mock.patch(
            "apps.business.management.commands.audit_order_group_qr.get_storage",
            return_value=storage,
        ), self.captureOnCommitCallbacks(execute=True):
            self._run("--execute", "--delete-objects")

        # 行：保留 max(id) 那张（最后一张共享群码），其余两张（本租户前缀 + 一张共享）照删。
        self.assertEqual(
            list(OrderScreenshot.objects.filter(order=order).values_list("pk", flat=True)),
            [shared[-1].pk],
        )
        # 字节：只删本租户前缀那条；共享前缀那两条**一次都没被 delete**。
        self.assertEqual(
            [call.args[0] for call in storage.delete.call_args_list],
            [local.object_key],
        )
        self.assertNotIn(
            shared[0].object_key,
            [c.args[0] for c in storage.delete.call_args_list],
        )

    def test_limit_caps_the_number_of_orders_per_round(self):
        orders = [self._order(order_no) for order_no in (ORDER_NO, ORDER_NO_B, ORDER_NO_C)]
        for order in orders:
            self._qr(order, "a")
            self._qr(order, "b")
        out = self._run("--execute", "--limit", "2")
        self.assertIn("orders=2", out)
        remaining = {
            order.order_no: OrderScreenshot.objects.filter(order=order).count()
            for order in orders
        }
        self.assertEqual(sorted(remaining.values()), [1, 1, 2])

    def test_an_unknown_schema_is_a_command_error(self):
        with self.assertRaises(CommandError):
            call_command("audit_order_group_qr", "--schema", "no-such-schema", stdout=StringIO())
