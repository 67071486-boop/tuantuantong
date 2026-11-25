"""孤儿清理命令的用例（spec §6.3 / §11）。

⚠️ 断言打在**真实出口**上：库里的行 + 磁盘上的对象（⛔ 不打在 stdout 文本上）。
"""
from datetime import timedelta
from unittest import mock
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderScreenshot, OrderStatus
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client


class PurgePendingScreenshotsTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260925000000000202",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        make_logged_in_client(username="purge-user")
        from django.contrib.auth.models import User

        self.user = User.objects.get(username="purge-user")
        self.storage = get_storage()

    def _row(self, *, age_hours, status):
        key = f"{self.order.order_no}/{status}-{age_hours}.jpg"
        self.storage.put_bytes(key, b"\xff\xd8\xff" + b"body", content_type="image/jpeg")
        row = OrderScreenshot.objects.create(
            order=self.order, object_key=key, uploaded_by=self.user, status=status
        )
        # `created_at` 是 `auto_now_add` ⇒ 只能创建后再 UPDATE 成「很久以前」
        OrderScreenshot.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(hours=age_hours)
        )
        row.refresh_from_db()
        return row

    def test_only_stale_pending_rows_are_purged(self):
        stale = self._row(age_hours=25, status=OrderScreenshot.STATUS_PENDING)
        fresh = self._row(age_hours=1, status=OrderScreenshot.STATUS_PENDING)
        ready = self._row(age_hours=25, status=OrderScreenshot.STATUS_READY)

        call_command("purge_pending_screenshots", "--schema", connection.schema_name,
                     stdout=StringIO())

        # 老的 pending：行与对象都没了
        self.assertFalse(OrderScreenshot.objects.filter(pk=stale.pk).exists())
        self.assertIsNone(self.storage.head(stale.object_key))
        # 新的 pending：一动不动（没到 24 小时）
        self.assertTrue(OrderScreenshot.objects.filter(pk=fresh.pk).exists())
        self.assertIsNotNone(self.storage.head(fresh.object_key))
        # ready：**永久保留**（A3）⇒ 25 小时也不许动
        self.assertTrue(OrderScreenshot.objects.filter(pk=ready.pk).exists())
        self.assertIsNotNone(self.storage.head(ready.object_key))

    def test_dry_run_deletes_nothing(self):
        stale = self._row(age_hours=25, status=OrderScreenshot.STATUS_PENDING)
        out = StringIO()
        call_command("purge_pending_screenshots", "--dry-run",
                     "--schema", connection.schema_name, stdout=out)
        # ⚠️ 判据打在**真实出口**上（行 + 磁盘对象）。⛔ 不断言 stdout 文本——本文件 docstring
                #    自己写着「不打在 stdout 上」，而初版这里自相矛盾。
        self.assertTrue(OrderScreenshot.objects.filter(pk=stale.pk).exists())
        self.assertIsNotNone(self.storage.head(stale.object_key))

    def test_row_is_kept_when_object_delete_fails(self):
        """删对象失败 ⇒ **保留行**（它是那个对象的唯一指针；下次运行重试）。"""
        from unittest import mock

        stale = self._row(age_hours=25, status=OrderScreenshot.STATUS_PENDING)
        # ⚠️ `mock.patch` 必须打在「**被查找的那个模块**」上：命令体里调的是
        #    `…purge_pending_screenshots.get_storage()`，它每次返回**新实例** ——
        #    只 patch 测试自己持有的 `self.storage` 是**没接上桩**（本行实测过一次：
        #    用例因此假绿/假红都会发生）。同族：`AGENTS.md` §5-E 的 `E-0029`。
        with mock.patch.object(self.storage, "delete", side_effect=RuntimeError("后端抽风")), \
                mock.patch(
                    "apps.business.management.commands.purge_pending_screenshots.get_storage",
                    return_value=self.storage,
                ):
            call_command("purge_pending_screenshots", "--schema", connection.schema_name,
                         stdout=StringIO(), stderr=StringIO())
        self.assertTrue(OrderScreenshot.objects.filter(pk=stale.pk).exists())

    def test_runs_without_schema_flag_by_iterating_tenants(self):
        """⛔ 不给 `--schema` 也必须生效——**这正是 Batch A 评审的 Critical**：
        命令只查当前连接 schema 时（django-tenants 默认停在 `public`）会
        `relation "business_orderscreenshot" does not exist`，清理根本不运行。"""
        stale = self._row(age_hours=25, status=OrderScreenshot.STATUS_PENDING)
        call_command("purge_pending_screenshots", stdout=StringIO())
        self.assertFalse(OrderScreenshot.objects.filter(pk=stale.pk).exists())

    def test_exactly_at_the_ttl_is_not_purged(self):
        """`created_at__lt=cutoff` ⇒ **恰好 24 小时**那一侧属于「还没到期」（边界钉住）。

        ⚠️ **必须把「现在」钉死**再测：直接用墙钟造 `created_at = now - 24h`，而命令里取的 `now`
        晚了几毫秒 ⇒ `lt` 成立、那一行被当成过期删掉，用例会以「边界写错」的形式假红
        （本行第一版就是这么错的）。这里 patch 命令模块里的 `timezone.now`，让两侧共用同一个瞬间。
        """
        fixed = timezone.now()
        on_the_line = self._row(age_hours=24, status=OrderScreenshot.STATUS_PENDING)
        OrderScreenshot.objects.filter(pk=on_the_line.pk).update(created_at=fixed - timedelta(hours=24))
        with mock.patch(
            "apps.business.management.commands.purge_pending_screenshots.timezone.now",
            return_value=fixed,
        ):
            call_command("purge_pending_screenshots", "--schema", connection.schema_name,
                         stdout=StringIO())
        self.assertTrue(OrderScreenshot.objects.filter(pk=on_the_line.pk).exists())
