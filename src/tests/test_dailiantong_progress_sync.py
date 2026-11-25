"""代练通进度图 → 本仓「订单截图」同步（2026-10-28）。

判据打在**真出口**上：本地 `OrderScreenshot` 行 + 媒体存储里的字节 + 审计行；
⛔ 不访问真实网络（`_fetch_image` 打桩）。
"""

from __future__ import annotations

import pathlib
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_progress_sync as sync_module
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    GameServer,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.core.storage import get_storage
from apps.openapi.models import OpenApiDelivery
from apps.openapi.models import ApiApp
from apps.openapi.models import CallbackState as OpenApiCallbackState
from apps.takerapi.models import TakerApiDelivery, TakerApp

User = get_user_model()

JPEG = b"\xff\xd8\xff\xe0" + b"synth-progress-image" * 10


class _TempMediaRootMixin:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class ProgressSyncTests(_TempMediaRootMixin, TenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        # ⭐ M7 负缓存是进程级 cache，而 `TenantTestCase` 的各用例共用一个 schema 名
        # ⇒ 不清会让上一条用例的「这张图失败过」漏到下一条（同 push.pk + external_id）。
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.game = Game.objects.create(name="王者荣耀")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261028000000000901",
            status=OrderStatus.IN_PROGRESS,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000901",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=1,
        )

    def _row(self, **overrides):
        row = {
            "ID": 367274039,
            "CreateDate": "2026-10-28 11:38:12",
            "Img": "https://img004.dailiantong.com/@/Progress/abc.jpg",
            "Comment": "12*****23",
            "Source": 10,
            "NickName": "接单者",
        }
        row.update(overrides)
        return row

    def _client(self, rows):
        return SimpleNamespace(
            progress_list=lambda serial, max_count=None: SimpleNamespace(rows=rows)
        )

    def test_taker_progress_image_becomes_a_local_order_screenshot(self):
        row = self._row()
        with mock.patch.object(sync_module, "_fetch_image", return_value=JPEG):
            added, failed, note = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([row])
            )

        self.assertEqual((added, failed, note), (1, 0, ""))
        screenshot = OrderScreenshot.objects.get(order=self.order)
        self.assertEqual(screenshot.img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(screenshot.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(screenshot.external_id, f"dailiantong:{self.push.pk}:367274039")
        self.assertIn("接单者", screenshot.remark)
        self.assertIn("12*****23", screenshot.remark)
        self.assertIn("2026-10-28 11:38:12", screenshot.remark)
        self.assertEqual(screenshot.size_bytes, len(JPEG))
        self.assertEqual(screenshot.content_type, "image/jpeg")
        head = get_storage().head(screenshot.object_key)
        self.assertIsNotNone(head, "字节必须真的写进媒体存储")
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_progress",
                order_no=self.order.order_no,
            )
            .get()
            .reason.startswith("从代练通同步了 1 张")
        )

    def test_our_own_publisher_image_is_skipped(self):
        with mock.patch.object(sync_module, "_fetch_image") as fetch:
            added, failed, note = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([self._row(Source=11)])
            )
        self.assertEqual((added, failed, note), (0, 0, ""))
        fetch.assert_not_called()
        self.assertFalse(OrderScreenshot.objects.exists())

    def test_repeated_sync_does_not_create_a_second_row(self):
        row = self._row()
        with mock.patch.object(sync_module, "_fetch_image", return_value=JPEG):
            first = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([row])
            )
            second = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([row])
            )
        self.assertEqual(first[0], 1)
        self.assertEqual(second[0], 0, "同一上游进度 ID 不重复下载/建行")
        self.assertEqual(OrderScreenshot.objects.filter(order=self.order).count(), 1)
        self.assertEqual(
            OperationLog.objects.filter(action="order.dailiantong_progress").count(),
            1,
            "没有新增就不重复写审计",
        )

    def test_download_failure_is_readable_and_does_not_raise(self):
        with mock.patch.object(
            sync_module,
            "_fetch_image",
            side_effect=sync_module.ProgressImageError("下载进度图失败（ReadTimeout）"),
        ):
            added, failed, note = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([self._row()])
            )
        self.assertEqual((added, failed), (0, 1))
        self.assertIn("同步失败", note)
        self.assertIn("ReadTimeout", note)
        self.assertFalse(OrderScreenshot.objects.exists())

    def test_non_image_bytes_are_rejected(self):
        with mock.patch.object(
            sync_module, "_fetch_image", return_value=b"<html>not an image</html>"
        ):
            added, failed, note = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([self._row()])
            )
        self.assertEqual((added, failed), (0, 1))
        self.assertIn("图片类型无法识别", note)
        self.assertFalse(OrderScreenshot.objects.exists())

    def test_non_dailiantong_image_host_is_rejected_before_any_download(self):
        """⭐ M1：只允许 *.dailiantong.com（SSRF 边界），且非 https 直接拒。"""
        for url in (
            "https://evil.example/progress.jpg",
            "http://img004.dailiantong.com/@/Progress/abc.jpg",
            "https://dailiantong.com.evil.example/abc.jpg",
        ):
            with self.subTest(url=url):
                with self.assertRaises(sync_module.ProgressImageError):
                    sync_module._fetch_image(url)

    def test_missing_progress_id_still_deduplicates_by_url(self):
        row = self._row()
        row.pop("ID")
        with mock.patch.object(sync_module, "_fetch_image", return_value=JPEG):
            first = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([row])
            )
            second = sync_module.sync_progress_screenshots_for_push(
                self.push, client=self._client([row])
            )
        self.assertEqual(first[0], 1)
        self.assertEqual(second[0], 0)
        self.assertEqual(OrderScreenshot.objects.count(), 1)


# ── ⭐ M7（审计）：平台过滤 / 失败负缓存 / 单轮耗时上限 / 命令进程锁 ──


class ProgressSyncM7Tests(_TempMediaRootMixin, TenantTestCase):
    """M7 的四条可靠性判据（判据都打在「有没有上游调用」上，⛔ 不看计数器）。"""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.game = Game.objects.create(name="原神")
        self.server = GameServer.objects.create(game=self.game, name="天空岛")
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261028000000000950",
            status=OrderStatus.IN_PROGRESS,
            project_text="深渊满星",
            total_price=Decimal("50.00"),
        )

    def _push(self, *, platform=10, serial="19000000000000000950", index=1):
        return DailiantongPush.objects.create(
            order=self.order,
            platform=platform,
            state=DailiantongPush.State.SENT,
            serial_no=serial,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=index,
        )

    def _never_called_client(self):
        return mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=AssertionError("这条用例不许打开上游客户端"),
        )

    def test_a_push_on_another_platform_is_never_synced(self):
        """候选查询按平台收窄：别的平台的行⛔ 不能拿代练通凭据去拉进度。"""
        self._push(platform=30, serial="19000000000000000330")

        with self._never_called_client():
            added, failed = sync_module.sync_active_progress_screenshots()

        self.assertEqual((added, failed), (0, 0))

    def test_a_failed_image_is_not_retried_within_the_backoff_window(self):
        """失败图负缓存：同一张图（同一推送行 + 上游进度 ID）本轮失败后，下一轮不再重试。"""
        push = self._push()
        row = {
            "ID": 9001,
            "Img": "https://img004.dailiantong.com/@/Progress/dead.jpg",
            "Comment": "死链",
            "Source": 10,
        }
        client = SimpleNamespace(
            progress_list=lambda serial, max_count=None: SimpleNamespace(rows=[row])
        )
        with mock.patch.object(
            sync_module,
            "_fetch_image",
            side_effect=sync_module.ProgressImageError("下载进度图失败（ReadTimeout）"),
        ) as fetch:
            first = sync_module.sync_progress_screenshots_for_push(push, client=client)
            second = sync_module.sync_progress_screenshots_for_push(push, client=client)

        self.assertEqual(first[1], 1, "第一次要如实记失败")
        self.assertEqual(second, (0, 0, ""), "负缓存窗口内不重试、不计失败")
        self.assertEqual(fetch.call_count, 1, "第二次不许再下载（死链每轮 15 秒超时的来源）")

    def test_the_round_stops_at_the_time_cap(self):
        """单轮总耗时上限：到点就停，剩下的留给下一轮（⛔ 不把回调消费拖到几十分钟）。"""
        self._push()

        with self._never_called_client():
            added, failed = sync_module.sync_active_progress_screenshots(
                max_seconds=1e-9
            )

        self.assertEqual((added, failed), (0, 0))

    def test_the_manual_command_refuses_while_the_lock_is_held(self):
        """手工命令进程锁：第二个进程立刻失败并说清锁文件在哪（⛔ 不陪等 / 不并发下载）。"""
        import os
        import tempfile
        from pathlib import Path

        from django.core.management import CommandError, call_command

        from apps.business.process_lock import ProcessLock

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock_path = Path(tmp.name) / "progress.lock"
        patcher = mock.patch.dict(
            os.environ,
            {"DAILIANTONG_PROGRESS_LOCK_PATH": str(lock_path)},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        held = ProcessLock(lock_path)
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "dailiantong_sync_progress",
                "--tenant", self.tenant.schema_name,
                "--order", self.order.order_no,
            )
        self.assertIn("dailiantong_sync_progress", str(ctx.exception))
        self.assertIn("锁文件", str(ctx.exception))


# ── ⭐ 业务方拍板补推：进度图入库 ⇒ 两族 `order.image_added` 事件行 ──
#
# 进度图变 `ready` 的**写入点④**（代练通打手进度图拉回入库；设计文档
# `docs/specs/2026-10-08-open-platform-v2-parity-design.md` §7.2.1 裁决 3 的同族，
# 先例 = `peer_inbound._apply_screenshot`（②）与 `jidanpu_upstream_pics.store_remote_pics`（③））。
# 判据全部打在**两族台账行**（`OpenApiDelivery` / `TakerApiDelivery`）这个真实出口上
# （`AGENTS.md` §5-G：不打中间层）；夹具凭据 / 回调地址一律合成值（`93.184.216.34`
# 是公网字面量，⛔ 不是任何真实第三方）。


class ProgressSyncOpenapiPushTests(_TempMediaRootMixin, TenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = User.objects.create_user(
            username="dlt-openapi-admin", password="pw-dlt-openapi"
        )
        self.game = Game.objects.create(name="代练通推送夹具游戏")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261028000000000970",
            status=OrderStatus.IN_PROGRESS,
            project_text="进度图推送判据",
            total_price=Decimal("66.00"),
            sales_user=self.admin,
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000970",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=1,
        )

    def _row(self, **overrides):
        row = {
            "ID": 367274070,
            "CreateDate": "2026-10-28 12:00:00",
            "Img": "https://img004.dailiantong.com/@/Progress/push.jpg",
            "Comment": "进度备注",
            "Source": 10,
            "NickName": "接单者",
        }
        row.update(overrides)
        return row

    def _sync(self, rows):
        with mock.patch.object(sync_module, "_fetch_image", return_value=JPEG):
            return sync_module.sync_progress_screenshots_for_push(
                self.push, client=SimpleNamespace(
                    progress_list=lambda serial, max_count=None: SimpleNamespace(
                        rows=rows
                    )
                )
            )

    def _make_openapi_app(self):
        return ApiApp.objects.create(
            name="代练通推送夹具应用",
            app_key="dltpushkey00000001",
            app_secret="dlt-push-secret-0000000000000001",
            callback_url="https://93.184.216.34/openapi/hook",
            callback_state=OpenApiCallbackState.ENABLED,
            created_by=self.admin,
        )

    def _assign_local_taker(self):
        """给本单一个**本地接手人** + 其启用回调的应用（内部打手与平台推送并存的过渡态）。"""
        taker = User.objects.create_user(
            username="dlt-openapi-taker", password="pw-dlt-openapi"
        )
        Performance.objects.create(
            order=self.order,
            user=taker,
            emp_type=EmpType.MAIN,
            claimed_at=timezone.now(),
        )
        app = TakerApp.objects.create(
            name="代练通推送打手应用",
            app_key="dltpushtaker000001",
            app_secret="dlt-taker-secret-000000000000001",
            taker=taker,
            created_by=taker,
            callback_url="https://93.184.216.34/taker/hook",
        )
        return taker, app

    def test_ingested_image_enqueues_openapi_and_skips_taker_when_unclaimed(self):
        """建行 ⇒ openapi 台账恰好一行 `order.image_added`；无本地接手人 ⇒ 打手侧零行。

        打手侧收件人 = **当前接手人**名下的应用（代练通外部打手不在本租户）⇒ 零行是
        正确读数、⛔ 不是漏接（两族收件人语义：`openapi/dispatch.py::eligible_apps`
        是整租户口径，`takerapi/api/callback_recipients.py` 是接手人口径）。
        """
        self._make_openapi_app()
        added, failed, _note = self._sync([self._row()])

        self.assertEqual((added, failed), (1, 0))
        screenshot = OrderScreenshot.objects.get(order=self.order)
        rows = list(OpenApiDelivery.objects.all())
        self.assertEqual(len(rows), 1, "openapi 台账必须恰好一行")
        row = rows[0]
        self.assertEqual(row.event, "order.image_added")
        self.assertEqual(row.order_no, self.order.order_no)
        entry = row.payload["images"][0]
        self.assertEqual(entry["object_key"], screenshot.object_key)
        self.assertNotIn("url", entry, "库里存稳定身份，投递那一刻才换预签名 URL")
        self.assertEqual(entry["img_type"], 0)
        self.assertEqual(entry["remark"], screenshot.remark)
        self.assertEqual(TakerApiDelivery.objects.count(), 0)

    def test_a_local_taker_also_receives_the_image_callback_once(self):
        """有本地接手人 ⇒ 两族各一行；第二轮（exists 预查跳过）两族都不再排第二行。"""
        self._make_openapi_app()
        _taker, taker_app = self._assign_local_taker()

        self.assertEqual(self._sync([self._row()])[:2], (1, 0))
        self.assertEqual(
            OpenApiDelivery.objects.filter(event="order.image_added").count(), 1
        )
        taker_rows = list(TakerApiDelivery.objects.all())
        self.assertEqual(len(taker_rows), 1, "打手侧台账必须恰好一行")
        self.assertEqual(taker_rows[0].event, "order.image_added")
        self.assertEqual(taker_rows[0].app_id, taker_app.pk)
        self.assertEqual(
            taker_rows[0].payload["images"][0]["object_key"],
            OrderScreenshot.objects.get(order=self.order).object_key,
        )

        # 第二轮同步同一张图：预查命中 ⇒ 不建行、不下载、两族都不重复排。
        self.assertEqual(self._sync([self._row()])[:2], (0, 0))
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        self.assertEqual(TakerApiDelivery.objects.count(), 1)

    def test_the_integrity_error_branch_enqueues_nothing(self):
        """并发撞唯一键（预查漏掉、插入撞）⇒ 那一笔回滚 ⇒ 两族一行都不排。

        钉的是**派发位置**：在 `create()` 之后、savepoint **之内** ⇒ `IntegrityError`
        从 `create` 抛出时还没走到派发。若有人把派发挪到 except 之后 / 先排再建 ⇒ 本条红。
        ⚠️ 本条在「补推」落地**之前**的基线上也是绿的（那时本来就没有投递行）——
        它是防回归钉子，不是咬合证明；咬合证明是上面两条（基线红）。
        """
        self._make_openapi_app()
        self._assign_local_taker()  # 两族都 eligible ⇒ 任何一边误推都红
        with mock.patch.object(sync_module, "_fetch_image", return_value=JPEG):
            with mock.patch.object(
                OrderScreenshot.objects,
                "create",
                side_effect=IntegrityError("duplicate external_id"),
            ):
                added, failed, _note = sync_module.sync_progress_screenshots_for_push(
                    self.push,
                    client=SimpleNamespace(
                        progress_list=lambda serial, max_count=None: SimpleNamespace(
                            rows=[self._row()]
                        )
                    ),
                )
        self.assertEqual((added, failed), (0, 0))
        self.assertFalse(OrderScreenshot.objects.exists())
        self.assertEqual(OpenApiDelivery.objects.count(), 0)
        self.assertEqual(TakerApiDelivery.objects.count(), 0)
