"""订单群码自动拉取（马上来查码；D-0348）——三态落库 / 防抖 / 配置闸 / 两触发点 / 失败面。

## 测试纪律

- ⛔ **不打真接口** `malailai.suqing.chat`：查码走 `httpx.MockTransport`（patch 模块的
  `_build_transport`，E-0029 口径——桩打在被查找的模块上）；下载 patch `_download_image`
  （照 dailiantong `_fetch_image` 先例），下载**守卫**用坏 URL 直调钉住。
- ⛔ **测试里不起线程**：patch `_spawn` 为同步调 `_run_fetch`（`_run_fetch_careful` 的
  槽位 / 关连接 / 兜底是线程形态的机械件，槽位护栏另有直调用例，用的是 setUp 里存下的
  **真函数引用**，不经桩）。
- on_commit：TestCase 全程在测试事务里 ⇒ 用 `captureOnCommitCallbacks(execute=True)` 收口。
- 夹具全部**合成值**（单号 `2026…`、密钥 `synthetic-*`、基址 `https://malailai.test`——RFC 2606
  保留域，⛔ 不是对端真域名）。
"""
from __future__ import annotations

import pathlib
import tempfile
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import order_qr_fetch
from apps.business.claim import claim_order
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderQrFetchConfig,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

#: 合成密钥（全文件唯一；密钥不落日志 / 台账的判据拿它当探针）。
SYNTHETIC_KEY = "synthetic-secret-key-123"
#: 合成基址（保留测试域，⛔ 不是对端真域名）。
SYNTHETIC_BASE = "https://malailai.test"


def _ok_envelope(qr_url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "msg": "", "data": {"order_no": "x", "status": "ok", "qr_url": qr_url}},
    )


def _status_envelope(status: str) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "msg": "", "data": {"order_no": "x", "status": status}}
    )


class QrFetchTestBase(TenantTestCase):
    """公共夹具：一张在途单 + 一份**已启用**的配置 + 同步桩（默认对端回 group_not_found）。"""

    JPEG = b"\xff\xd8\xff" + b"synthetic-jpeg-body"

    def setUp(self):
        # ⭐ 先存真函数引用（护栏 / 守卫直调用例要绕开桩），再打桩。
        self.real_spawn = order_qr_fetch._spawn
        self.real_download = order_qr_fetch._download_image
        cache.clear()  # 冷却走进程内 cache：用例间互不串味
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261031000000000101",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
            source_order_no="1234567890123456789",
        )
        self.config = OrderQrFetchConfig.objects.create(
            tenant_schema=connection.schema_name,
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            enabled=True,
        )
        #: 对端收到的请求（逐次记完整 request；只有落库用例断言它的头与参数）。
        self.peer_calls: list[httpx.Request] = []
        self._peer_patches: list[mock._patch] = []
        self._apply_peer(self._peer_handler)
        # 同步 worker（不起线程；见模块 docstring 的测试纪律）。
        self._patch(order_qr_fetch, "_spawn", side_effect=self._sync_spawn)
        # 下载打桩（照 dailiantong `_fetch_image` 先例）；守卫另有直调用例。
        self._patch(order_qr_fetch, "_download_image", return_value=self.JPEG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage_override = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        storage_override.enable()
        self.addCleanup(storage_override.disable)

    # ── 桩的机械件 ──────────────────────────────────────────────────

    def _sync_spawn(self, **kwargs):
        return order_qr_fetch._run_fetch(**kwargs)

    def _patch(self, target, attr, **kwargs):
        patcher = mock.patch.object(target, attr, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._peer_patches.append(patcher)

    def _apply_peer(self, handler):
        self._patch(
            order_qr_fetch, "_build_transport", return_value=httpx.MockTransport(handler)
        )

    def _peer_handler(self, request: httpx.Request) -> httpx.Response:
        """默认对端：回 `group_not_found`（不落库的静默态）；子用例用 `_peer_*` 改写。"""
        self.peer_calls.append(request)
        return _status_envelope("group_not_found")

    def _peer_ok(self, qr_url: str = "https://wework.example/qr.png") -> None:
        """把对端改成 ok 态（LIFO 覆盖基座的 transport 桩）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            self.peer_calls.append(request)
            return _ok_envelope(qr_url)

        self._apply_peer(handler)

    def _peer_raw(self, response: httpx.Response) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.peer_calls.append(request)
            return response

        self._apply_peer(handler)

    def _trigger(self) -> bool:
        """入队 + 当场执行 on_commit（= 同步跑完整个 worker）。"""
        with self.captureOnCommitCallbacks(execute=True):
            return order_qr_fetch.maybe_enqueue_fetch(self.order)

    def _make_taker(self, username: str) -> User:
        return User.objects.create_user(username=username)


class ThreeStateLandingTests(QrFetchTestBase):
    """三态：ok 转存落 `img_type=1`；group_not_found / generating 静默。"""

    def test_ok_downloads_and_lands_ready_group_qr(self):
        self._peer_ok()
        self.assertTrue(self._trigger())
        row = OrderScreenshot.objects.get(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR
        )
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        # ⭐「转存」判据：字节真的进了本仓存储，⛔ 不是把临时 URL 存下来。
        stored = get_storage().head(row.object_key)
        self.assertIsNotNone(stored)
        self.assertGreater(stored[0], 0)
        self.assertEqual(row.size_bytes, stored[0])
        self.assertEqual(row.content_type, "image/jpeg")  # 魔数嗅探的值
        self.assertTrue(row.object_key.startswith("shared-orders/"))  # D-0316 共享前缀
        # 本单无客服 ⇒ 落在占位账号「马上来」名下（NOT NULL + PROTECT 的机器侧口径）。
        self.assertIsNotNone(row.uploaded_by_id)
        self.assertEqual(row.uploaded_by.username, order_qr_fetch.VIRTUAL_MALAILAI_USERNAME)
        # 对端收到了 order_no 与来源单号（直查更快的那半句契约）与 Bearer 头。
        request = self.peer_calls[0]
        self.assertIn("order_no=20261031000000000101", str(request.url))
        self.assertIn("source_order_no=1234567890123456789", str(request.url))
        self.assertEqual(request.headers["Authorization"], f"Bearer {SYNTHETIC_KEY}")
        # 成功 ⇒ 台账一行。
        self.assertEqual(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").count(), 1
        )

    def test_group_not_found_is_silent(self):
        self.assertTrue(self._trigger())
        self.assertFalse(
            OrderScreenshot.objects.filter(order=self.order, img_type=1).exists()
        )
        self.assertFalse(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").exists()
        )
        # 静默 ≠ 没防抖：冷却已铺（下次自然触发点之前不再打对端）。
        self.assertGreater(
            order_qr_fetch.cooldown_remaining(connection.schema_name, self.order.order_no),
            0,
        )
        self.assertEqual(len(self.peer_calls), 1)

    def test_generating_is_silent(self):
        self._peer_raw(_status_envelope("generating"))
        self.assertTrue(self._trigger())
        self.assertFalse(
            OrderScreenshot.objects.filter(order=self.order, img_type=1).exists()
        )
        self.assertEqual(len(self.peer_calls), 1)

    def test_second_fetch_after_success_keeps_single_ready_row(self):
        """成功后短路：第二次触发不入队（成功后永不重触发 = ready 行在即短路）。"""
        self._peer_ok()
        self.assertTrue(self._trigger())
        cache.clear()
        self.assertFalse(self._trigger())
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR
            ).count(),
            1,
        )


class DebounceAndGateTests(QrFetchTestBase):
    """防抖（冷却内不再触发 / 成功后短路）与配置闸（未配 / 关 / 无密钥 / 无基址 / 闩 /
    软删 ⇒ 零动作；入队与 worker 两道复核各钉一遍）。"""

    def test_cooldown_blocks_second_trigger(self):
        self.assertTrue(self._trigger())
        # 冷却内第二次入队被拒（返回 False；对端不再被打）。
        self.assertFalse(self._trigger())
        self.assertEqual(len(self.peer_calls), 1)

    def test_success_short_circuit_never_refetches(self):
        self._peer_ok()
        self.assertTrue(self._trigger())
        calls_after_first = len(self.peer_calls)
        cache.clear()  # 冷却不是成功的防线：已有 ready 群码才是
        self.assertFalse(self._trigger())
        self.assertEqual(len(self.peer_calls), calls_after_first)

    def test_no_config_is_zero_action(self):
        self.config.delete()
        cache.clear()
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])

    def test_disabled_config_is_zero_action(self):
        self.config.enabled = False
        self.config.save()
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])

    def test_missing_api_key_is_zero_action(self):
        self.config.api_key = ""
        self.config.save()
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])

    def test_missing_base_url_is_zero_action(self):
        """F-1（评审 2026-10-31）：填了密钥、开了启用、漏填基址 = 未配置 ⇒ 零动作。

        ⛔ 不判它就会放 worker 拿空基址拼相对 URL 必败（UnsupportedProtocol），
        每单每 10 分钟刷一行失败台账，淹没 RUNBOOK 指定的排查入口。
        """
        self.config.base_url = ""
        self.config.save()
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])
        self.assertFalse(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").exists()
        )

    def _run_worker_now(self) -> None:
        """绕开入队闸，直接同步跑一次 worker 复核之后的链路（复核闸的专用探针）。"""
        order_qr_fetch._run_fetch(
            schema=connection.schema_name,
            order_no=self.order.order_no,
            source_order_no=self.order.source_order_no or "",
            order_pk=self.order.pk,
        )

    def test_worker_recheck_blocks_when_base_url_cleared_after_enqueue(self):
        """F-1 worker 复核：入队后配置被改成「开着开关却无基址」⇒ worker 也零动作。"""
        self.config.base_url = ""
        self.config.save()
        self._run_worker_now()
        self.assertEqual(self.peer_calls, [])
        self.assertFalse(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").exists()
        )

    def test_worker_gives_up_when_ready_group_qr_appeared_after_enqueue(self):
        """S-1（计划 §3.2 承诺的第三道复核）：入队→执行窗口内人工传上群码 ⇒ 放弃。

        ⛔ 窄竞态下自动拉取不得覆盖窗口内的人工上传。
        """
        uploader = self._make_taker("qr-manual-uploader")
        manual_row = OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key="shared-orders/origin-1/qr.synthetic03.png",
            uploaded_by=uploader,
            status=OrderScreenshot.STATUS_READY,
        )
        self._peer_ok()  # 对端就算就绪，worker 也该在复核闸放弃
        self._run_worker_now()
        self.assertEqual(self.peer_calls, [])
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR
            ).count(),
            1,
        )
        manual_row.refresh_from_db()
        self.assertEqual(manual_row.object_key, "shared-orders/origin-1/qr.synthetic03.png")

    def test_soft_deleted_order_is_zero_action(self):
        from django.utils import timezone

        self.order.deleted_at = timezone.now()
        self.order.save(update_fields=["deleted_at"])
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])

    def test_latched_config_is_zero_action(self):
        from django.utils import timezone

        self.config.auth_failed_at = timezone.now()
        self.config.save()
        self.assertFalse(self._trigger())
        self.assertEqual(self.peer_calls, [])


class FailurePathTests(QrFetchTestBase):
    """429 按 Retry-After 冷却（封顶）；401 闩 + 台账恰一次 + 配置变更解锁；密钥不外流。"""

    def test_429_sets_retry_after_cooldown(self):
        self._peer_raw(
            httpx.Response(
                429, json={"code": 429, "msg": "限速"}, headers={"Retry-After": "1200"}
            )
        )
        self.assertTrue(self._trigger())
        remaining = order_qr_fetch.cooldown_remaining(
            connection.schema_name, self.order.order_no
        )
        self.assertGreaterEqual(remaining, 1200)
        self.assertLessEqual(remaining, 1201)

    def test_429_retry_after_is_capped(self):
        self._peer_raw(
            httpx.Response(
                429, json={"code": 429, "msg": "限速"}, headers={"Retry-After": "99999"}
            )
        )
        self.assertTrue(self._trigger())
        remaining = order_qr_fetch.cooldown_remaining(
            connection.schema_name, self.order.order_no
        )
        self.assertLessEqual(remaining, order_qr_fetch.RATE_LIMIT_COOLDOWN_CAP + 1)

    def test_401_latches_once_ledgers_once_and_blocks_until_config_change(self):
        self._peer_raw(httpx.Response(401, json={"code": 401, "msg": "凭据无效"}))
        self.assertTrue(self._trigger())
        self.config.refresh_from_db()
        self.assertIsNotNone(self.config.auth_failed_at)
        ledgers = list(OperationLog.objects.filter(action="order.malailai_qr_fetch"))
        self.assertEqual(len(ledgers), 1)
        # 密钥绝不进台账（逐行扫全部文本列）。
        for row in ledgers:
            for field in row._meta.get_fields():
                value = getattr(row, field.attname, None) if getattr(field, "attname", None) else None
                if isinstance(value, str):
                    self.assertNotIn(SYNTHETIC_KEY, value)
        # 闩住 ⇒ 再触发零动作（先清冷却，排除冷却的干扰）；台账不重复。
        cache.clear()
        self.assertFalse(self._trigger())
        self.assertEqual(len(self.peer_calls), 1)
        self.assertEqual(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").count(), 1
        )
        # 配置变更走**端点**（清闩是视图层行为）⇒ 清闩后可再触发（端到端）。
        from tests.p5_auth_helpers import make_logged_in_client as _mlc

        admin = _mlc(
            username="qr-401-admin", groups=("管Q2",), nodes=("system.pinduoduo_settings",)
        )
        response = admin.patch(
            "/api/business/order-qr/config/",
            {"enabled": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertIsNone(self.config.auth_failed_at)
        cache.clear()
        self._peer_ok()
        self.assertTrue(self._trigger())
        self.assertEqual(len(self.peer_calls), 2)

    def test_peer_500_ledgers_and_stays_cool(self):
        self._peer_raw(httpx.Response(200, json={"code": 500, "msg": "服务内部错误"}))
        self.assertTrue(self._trigger())
        self.assertEqual(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").count(), 1
        )
        self.assertFalse(
            OrderScreenshot.objects.filter(order=self.order, img_type=1).exists()
        )

    def test_peer_404_is_a_silent_not_found_with_a_host_detail(self):
        """⭐ 案 C（D-0364）：对端对**没有的单回 HTTP 404**（文档外实测形态）。

        折成 `not_found` 静默档：**零台账行**（与 `group_not_found` 同档；旧折法
        `peer_error` 会在「已有 ready 群码就短路」闸前留一条失败台账——现场全租户
        台账只有这一条 404 的根因）。`detail` 带可辨人话（队列通道据此在 journald
        可数）。

        ⚠️ **变异自证（必须能红）**：把 `fetch_order_qr` 里 404 那一支删掉
        （还原成「非 200 ⇒ peer_error」兜底）⇒ 本条在「零台账」断言上红
        （peer_error 会写一条 `order.malailai_qr_fetch` 失败行）。
        """
        # 对端桩改 404（LIFO 覆盖基座；单元直调与集成触发共用它）。
        self._peer_raw(httpx.Response(404))

        # 单元层：kind 折对 + detail 是「对端查无此单」人话（含 host，⭐ 不含单号以外业务值）。
        result = order_qr_fetch.fetch_order_qr(
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            order_no=self.order.order_no,
            source_order_no=self.order.source_order_no,
        )
        self.assertEqual(result.kind, "not_found")
        self.assertIn("对端系统里没有这张单", result.detail)
        self.assertIn("malailai.test", result.detail)

        # 集成层：web 通道走完整 worker ⇒ 同样零台账、零截图行。
        self.assertTrue(self._trigger())
        self.assertEqual(
            OperationLog.objects.filter(action="order.malailai_qr_fetch").count(), 0
        )
        self.assertFalse(
            OrderScreenshot.objects.filter(order=self.order, img_type=1).exists()
        )

    def test_api_key_never_reaches_logs(self):
        self._peer_raw(httpx.Response(401, json={"code": 401, "msg": "凭据无效"}))
        with self.assertLogs("apps.business.order_qr_fetch", level="INFO") as captured:
            self.assertTrue(self._trigger())
        self.assertNotIn(SYNTHETIC_KEY, "\n".join(captured.output))

    def test_ledger_never_carries_qr_url(self):
        # 下载失败（守卫拒了临时地址的怪形态）⇒ 台账 reason 只有人话，没有 URL 全文。
        def broken_download(url: str) -> bytes:
            raise order_qr_fetch.QrFetchError("下载群码失败（HTTP 404）")

        self._peer_ok()
        self._patch(order_qr_fetch, "_download_image", side_effect=broken_download)
        self.assertTrue(self._trigger())
        row = OperationLog.objects.get(action="order.malailai_qr_fetch")
        self.assertNotIn("wework.example", row.reason)


class WorkerGuardUnitTests(QrFetchTestBase):
    """护栏 / 下载守卫的直调单元（不经过完整流程、不经 `_spawn` 桩）。"""

    def test_download_guard_rejects_bad_targets(self):
        for bad in (
            "ftp://wework.example/qr.png",
            "http://127.0.0.1/qr.png",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/qr.png",
            "not-a-url",
        ):
            with self.assertRaises(order_qr_fetch.QrFetchError):
                self.real_download(bad)

    def test_rate_guard_stops_after_window_is_full(self):
        order_qr_fetch._rate_window.clear()
        try:
            for _ in range(order_qr_fetch.MAX_STARTS_PER_MINUTE):
                self.assertTrue(order_qr_fetch._rate_guard_allows())
            self.assertFalse(order_qr_fetch._rate_guard_allows())
        finally:
            order_qr_fetch._rate_window.clear()

    def test_concurrency_slot_exhaustion_drops_spawn(self):
        """真实 `_spawn`（setUp 存下的引用，绕开同步桩）：槽满 ⇒ 丢弃。"""
        acquired = [order_qr_fetch._slots.acquire(blocking=False) for _ in range(2)]
        order_qr_fetch._rate_window.clear()
        try:
            self.assertFalse(
                self.real_spawn(
                    schema=connection.schema_name,
                    order_no=self.order.order_no,
                    source_order_no="",
                    order_pk=self.order.pk,
                )
            )
        finally:
            for _ in acquired:
                order_qr_fetch._slots.release()
            order_qr_fetch._rate_window.clear()


class TriggerPointTests(QrFetchTestBase):
    """两触发点：接单收敛点（claim）/ 打手详情群码区块 / 客服端截图列表。"""

    def _ready_order_for_claim(self):
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )

    def _client_with_main_taker(self, username: str, group: str):
        """建已登录打手（夹具建号）+ 把本单 MAIN 绩效挂到他名下；返回 (client, user)。"""
        client = make_logged_in_client(
            username=username, groups=(group,), nodes=("my_orders.send_to_my_system",)
        )
        user = User.objects.get(username=username)
        Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=user,
            temp_amount=Decimal("88.00"),
        )
        return client, user

    def test_claim_triggers_exactly_one_fetch(self):
        self._ready_order_for_claim()
        taker = self._make_taker("qr-claim-taker")
        with self.captureOnCommitCallbacks(execute=True):
            claim_order(
                user_id=taker.id,
                order_id=self.order.id,
                nodes={"grab_hall.take_order"},
            )
        self.assertEqual(len(self.peer_calls), 1)

    def test_claim_without_config_is_zero_action(self):
        self._ready_order_for_claim()
        self.config.delete()
        taker = self._make_taker("qr-claim-taker-2")
        with self.captureOnCommitCallbacks(execute=True):
            claim_order(
                user_id=taker.id,
                order_id=self.order.id,
                nodes={"grab_hall.take_order"},
            )
        self.assertEqual(self.peer_calls, [])

    def test_detail_view_triggers_when_group_qr_missing(self):
        client, _user = self._client_with_main_taker("qr-detail-taker", "打Q1")
        with self.captureOnCommitCallbacks(execute=True):
            response = client.get(
                f"/api/business/my-orders/{self.order.order_no}/detail/"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["data"]["group_qr_url"])
        self.assertEqual(len(self.peer_calls), 1)

    def test_detail_view_does_not_trigger_when_group_qr_exists(self):
        client, user = self._client_with_main_taker("qr-detail-taker-2", "打Q2")
        OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key="shared-orders/origin-1/qr.synthetic01.png",
            uploaded_by=user,
            status=OrderScreenshot.STATUS_READY,
        )
        with self.captureOnCommitCallbacks(execute=True):
            client.get(f"/api/business/my-orders/{self.order.order_no}/detail/")
        self.assertEqual(self.peer_calls, [])

    def test_screenshots_list_triggers_when_no_group_qr_row(self):
        client = make_logged_in_client(
            username="qr-kefu-1", groups=("客Q1",), nodes=("order.view_screenshot",)
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = client.get(
                f"/api/business/orders/{self.order.order_no}/screenshots"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.peer_calls), 1)

    def test_screenshots_list_does_not_trigger_when_group_qr_row_exists(self):
        OrderScreenshot.objects.create(
            order=self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            object_key="shared-orders/origin-1/qr.synthetic02.png",
            uploaded_by=self._make_taker("qr-uploader-1"),
            status=OrderScreenshot.STATUS_READY,
        )
        client = make_logged_in_client(
            username="qr-kefu-2", groups=("客Q2",), nodes=("order.view_screenshot",)
        )
        with self.captureOnCommitCallbacks(execute=True):
            client.get(f"/api/business/orders/{self.order.order_no}/screenshots")
        self.assertEqual(self.peer_calls, [])


class QrConfigApiTests(TenantTestCase):
    """配置端点：掩码 / 留空不改 / 清闩 / 无变更拒绝 / 非法基址 / 无节点 403 / 台账。"""

    def setUp(self):
        cache.clear()
        self.game = Game.objects.create(name="原神")
        self.admin = make_logged_in_client(
            username="qr-config-admin",
            groups=("管Q1",),
            nodes=("system.pinduoduo_settings",),
        )
        self.url = "/api/business/order-qr/config/"

    def test_get_without_config_returns_empty_shape(self):
        response = self.admin.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["configured"])
        self.assertEqual(response.data["data"]["api_key_masked"], "")

    def test_patch_creates_masks_and_never_echoes_secret(self):
        response = self.admin.patch(
            self.url,
            {"enabled": True, "base_url": SYNTHETIC_BASE, "api_key": SYNTHETIC_KEY},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SYNTHETIC_KEY, str(response.data))
        self.assertTrue(response.data["data"]["api_key_masked"].startswith("s"))
        row = OrderQrFetchConfig.objects.get()
        self.assertEqual(row.api_key, SYNTHETIC_KEY)
        self.assertTrue(row.enabled)
        # 改配置 ⇒ 台账一行（改凭据与启停必须记，照 QQ 机器人口径）。
        self.assertEqual(
            OperationLog.objects.filter(action="system.order_qr_config_update").count(),
            1,
        )

    def test_patch_with_blank_api_key_keeps_old_secret(self):
        OrderQrFetchConfig.objects.create(
            tenant_schema=connection.schema_name,
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            enabled=True,
        )
        response = self.admin.patch(
            self.url, {"api_key": ""}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        row = OrderQrFetchConfig.objects.get()
        self.assertEqual(row.api_key, SYNTHETIC_KEY)

    def test_patch_clears_auth_latch(self):
        from django.utils import timezone

        OrderQrFetchConfig.objects.create(
            tenant_schema=connection.schema_name,
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            enabled=True,
            auth_failed_at=timezone.now(),
        )
        response = self.admin.patch(
            self.url, {"enabled": True}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        row = OrderQrFetchConfig.objects.get()
        self.assertIsNone(row.auth_failed_at)

    def test_patch_with_no_change_is_rejected(self):
        response = self.admin.patch(self.url, {}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)

    def test_patch_with_invalid_base_url_is_rejected(self):
        response = self.admin.patch(
            self.url, {"base_url": "http://127.0.0.1/"}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)
        self.assertFalse(OrderQrFetchConfig.objects.exists())

    def test_patch_enabled_without_base_url_is_rejected(self):
        """F-1②（评审 2026-10-31）：填密钥+开启用+漏基址 ⇒ 保存当场被拒，不落半套配置。"""
        response = self.admin.patch(
            self.url,
            {"enabled": True, "api_key": SYNTHETIC_KEY},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)
        self.assertFalse(OrderQrFetchConfig.objects.exists())
        self.assertFalse(
            OperationLog.objects.filter(action="system.order_qr_config_update").exists()
        )

    def test_patch_clearing_base_url_while_enabled_is_rejected(self):
        """F-1② 边界：启用态下把基址清成空串也被拒（按合并后的终态判）——旧行不动。"""
        OrderQrFetchConfig.objects.create(
            tenant_schema=connection.schema_name,
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            enabled=True,
        )
        response = self.admin.patch(
            self.url, {"base_url": ""}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)
        row = OrderQrFetchConfig.objects.get()
        self.assertEqual(row.base_url, SYNTHETIC_BASE)

    def test_without_node_is_403(self):
        plain = make_logged_in_client(username="qr-nobody", groups=("客Q3",))
        response = plain.get(self.url)
        self.assertEqual(response.status_code, 403)
