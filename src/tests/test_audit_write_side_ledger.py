"""审计线二期·写面留痕（D-0361，2026-11-02）——熔断挡单与查码关键时刻的行为网。

覆盖（计划 `docs/plans/2026-11-02-audit-write-side-ledger.md` §三.4）：

- **口径 A**：投递侧熔断挂起期被挡的单，落 `member.launch_blocked` 台账
  （每 (订单，绑定) 12h 窗内**恰一条**；窗过后可再条）。⛔ 闸的判定与拦截
  **零改动**——挂起期新单照旧被拦（行保持 pending、不投递、`stats.suspended`
  照计），挡单行只是留痕（回归钉钉住这一点）；
- **口径 B**：查码三关键时刻——首查 `qr.fetch_started`（恰一次）、查到**复用**
  既有台账（`order.malailai_qr_fetch` ok 行 + ready 截图行，⛔ 不另写
  `qr.fetch_hit`）、24h 窗关闭 `qr.fetch_gaveup`（每单恰一条；还在窗内 /
  已有群码截图行 / 已软删 / 从没查过 / 首查行老过回看界都**不记**）；
  `not_found` 等中间轮**零新行**；
- **时间线**：qr 节有行显示行（人话短名 + 状态）、无行保留提示语；
  挡单行（`target_type="order"`）落进 jdp 节。

## 弄红证明（实现轮留档）

- **kind 恰好集合网**：三个新 kind 写进代码、`JIDANPU_KIND_LABELS` 未登记时，
  `tests.test_sync_event_kind_registry` 红在「代码写出了、登记表没有」那一半
  （missing 集合 = `['member.launch_blocked', 'qr.fetch_gaveup', 'qr.fetch_started']`，
  除这三条外无任何 opaque / 写口缺登记）；同次登记后绿。证明留档见该网 docstring。

## 测试纪律（沿用既有同族口径）

- 夹具全部**合成值**（单号 `2026…`、店名、密钥、基址 RFC 2606 保留域）；
- A 的断言一律打在**数据库行**（台账行重取 / 行数字面），⛔ 不看内存对象与 mock；
- qr 侧 ⛔ 不打真接口（patch `_build_transport`）⛔ 不起线程（patch `_spawn`），
  照 `tests/test_order_qr_fetch.py` 的形状。
"""

from __future__ import annotations

import pathlib
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.audit.timeline import build_order_timeline
from apps.business import jidanpu_member, order_qr_fetch
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    Order,
    OrderQrFetchConfig,
    OrderScreenshot,
    OrderStatus,
)

BASE_URL = "https://jidanpu-wside.test.invalid"
ACCOUNT = "wside-store-a"
ORDER_NO = "20261102000000001001"
ORDER_NO_2 = "20261102000000001002"
ORDER_NO_3 = "20261102000000001003"

READBACK_LITERAL = "录单后回读不到对方订单号，本轮只保住「已录过」这个事实"
#: `_retry` 到上限时追加的后缀（字面副本；生产里族内失败行就长这样）。
ABANDONED_SUFFIX_LITERAL = "（已重试 5 次，不再自动重试，请人工核对）"

KIND_LAUNCH_LITERAL = "member.launch"
KIND_BLOCKED_LITERAL = "member.launch_blocked"

#: 注入式假客户端（照 breaker 网：真出口一旦被碰到就响亮炸，⛔ 不静默连真网）。
CLIENT = object()

#: 查码测试的合成常量（与 `test_order_qr_fetch.py` 同值：保留测试域）。
SYNTHETIC_KEY = "synthetic-secret-key-123"
SYNTHETIC_BASE = "https://malailai.test"


def _family_reason() -> str:
    return (
        f"{READBACK_LITERAL}：对方搜索接口无响应（上游码 0）{ABANDONED_SUFFIX_LITERAL}"
    )


def _qr_family_rows(order_no: str) -> "list[JidanpuSyncEvent]":
    """这单的鸡蛋铺台账行（⛔ 2026-11-03 起查码**不该**在这里留任何行——
    本帮助函数服务「零行」负性断言；查到行的判据在 OperationLog 侧）。"""
    return list(
        JidanpuSyncEvent.objects.filter(target_ref=order_no).order_by("id")
    )


def _qr_ok_rows(order_no: str) -> "list[OperationLog]":
    """这单的查码「查到」台账行（唯一该有的那种）。"""
    return list(
        OperationLog.objects.filter(
            action=order_qr_fetch.LEDGER_ACTION,
            order_no=order_no,
            reason=order_qr_fetch.LEDGER_OK_REASON,
        ).order_by("id")
    )


# ══ 口径 A：挡单台账（每 (单，绑定) 12h 恰一条；闸语义零改动）════════════════


class _BlockedLedgerBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="留痕测试游戏")
        self.user = User.objects.create_user(username=ACCOUNT, password="pw-wside")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            password="pw-wside",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
        )

    def _free_launch(self, order_no: str) -> Order:
        """无发单价的单：launch 消费对 NULL 价第一跳就 skipped（零上游）⇒
        假客户端 CLIENT 永不该被碰到真出口。"""
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=None,
            total_price="50.00",
        )

    def _seed_failures(self, n: int) -> None:
        for _ in range(n):
            JidanpuSyncEvent.objects.create(
                direction="out",
                kind=KIND_LAUNCH_LITERAL,
                status="failed",
                target_type="order",
                target_ref=self.order.order_no,
                attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
                reason=_family_reason(),
                payload={"member_binding_id": self.binding.pk},
            )

    def _blocked_rows(self) -> "list[JidanpuSyncEvent]":
        return list(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_BLOCKED_LITERAL,
                payload__member_binding_id=self.binding.pk,
            ).order_by("id")
        )

    def _suspend(self, *orders: Order):
        """达阈值 + 排新单 + 跑一轮 ⇒ 挂起（回本轮 stats）。"""
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        for order in orders:
            jidanpu_member.queue_member_event(
                order, self.binding, kind=jidanpu_member.KIND_LAUNCH
            )
        return jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )


class BlockedLedgerShapeTests(_BlockedLedgerBase):
    def test_blocked_order_leaves_exactly_one_ledger_row(self):
        """被挡的单落**恰一条**挡单台账，行形状逐字段钉死（真实出口 = 数据库行）。"""
        order2 = self._free_launch(ORDER_NO_2)
        stats = self._suspend(order2)
        self.assertEqual(stats.suspended, 1)
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.kind, KIND_BLOCKED_LITERAL)
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.status, "skipped", "终态 skipped（⛔ 不用 pending/failed）")
        self.assertEqual(row.target_type, "order")
        self.assertEqual(row.target_ref, order2.order_no, "按我方单号指认")
        self.assertEqual(row.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(row.attempts, 0, "台账行永不被 outbox 取用")
        # 人话点名店铺与恢复条件（照 member.launch_breaker_open 的 reason 风格）。
        self.assertIn(ACCOUNT, row.reason)
        self.assertIn("回读成功即自动恢复", row.reason)

    def test_regression_pin_suspension_still_holds_with_the_ledger_on(self):
        """回归钉：留痕上线后挂起语义**零改动**——新单照常排队、行保持 pending、
        不投递（假客户端没被碰到）、`stats.suspended` 照计。"""
        order2 = self._free_launch(ORDER_NO_2)
        got = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertIsNotNone(got, "挂起期新事件照常排队（D3：意图不丢）")
        stats = self._suspend(order2)
        self.assertEqual(stats.suspended, 1)
        row = JidanpuSyncEvent.objects.get(pk=got.pk)
        self.assertEqual(row.status, "pending", "被拦 = 行保持 pending，不是被改态")
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_LAUNCH_LITERAL, status="done"
            ).count(),
            0,
            "挂起期没有任何 launch 被投出去",
        )

    def test_two_held_orders_each_get_their_own_single_row(self):
        """多单同挂：每单各自的挡单行（按单号指认），不是一锅一行的绑定级台账。"""
        order2 = self._free_launch(ORDER_NO_2)
        order3 = self._free_launch(ORDER_NO_3)
        stats = self._suspend(order2, order3)
        self.assertEqual(stats.suspended, 2)
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.target_ref for row in rows}, {ORDER_NO_2, ORDER_NO_3}
        )


class BlockedLedgerDedupTests(_BlockedLedgerBase):
    def test_same_window_second_round_writes_no_second_row(self):
        """同窗只一条：挂起期内反复被挡（每 10 秒一拍的调度）不逐轮灌行（E-0304 同族）。"""
        order2 = self._free_launch(ORDER_NO_2)
        self._suspend(order2)
        self.assertEqual(len(self._blocked_rows()), 1)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now() + timedelta(minutes=1), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(len(self._blocked_rows()), 1, "12h 窗内不写第二条")

    def test_after_the_window_a_new_blocked_row_is_written(self):
        """窗过后可再条：挡单行老过 12h 界 ⇒ 同一单再被挡时落**新**行（≤2 行/天的上界）。"""
        order2 = self._free_launch(ORDER_NO_2)
        self._suspend(order2)
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_BLOCKED_LITERAL
        ).update(created_at=timezone.now() - timedelta(hours=13))
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 2, "12h 窗已过 ⇒ 新的一轮挡单留新的一条")
        self.assertEqual(rows[0].target_ref, ORDER_NO_2)
        self.assertEqual(rows[1].target_ref, ORDER_NO_2)

    def test_the_blocked_kind_is_never_picked_by_the_outbox(self):
        """契约钉：挡单 kind 不在 `OUTBOX_KINDS`（照 member.launch_breaker_open 同一条），
        行永远停在终态 skipped、attempts 不动。"""
        self.assertNotIn(
            jidanpu_member.KIND_LAUNCH_BLOCKED, jidanpu_member.OUTBOX_KINDS
        )
        order2 = self._free_launch(ORDER_NO_2)
        self._suspend(order2)
        row = self._blocked_rows()[0]
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now() + timedelta(minutes=1), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, "skipped")
        self.assertEqual(row.attempts, 0)


class _QrLedgerBase(TenantTestCase):
    """查码夹具（照 `test_order_qr_fetch.QrFetchTestBase` 的形状裁剪）。"""

    JPEG = b"\xff\xd8\xff" + b"synthetic-jpeg-body"

    def setUp(self):
        self.real_spawn = order_qr_fetch._spawn
        cache.clear()
        self.game = Game.objects.create(name="查码留痕游戏")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261102000000002001",
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
        self._patch(
            order_qr_fetch, "_build_transport",
            return_value=httpx.MockTransport(self._peer_handler),
        )
        self._patch(order_qr_fetch, "_spawn", side_effect=self._sync_spawn)
        self._patch(
            order_qr_fetch, "_download_image", return_value=self.JPEG
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage_override = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        storage_override.enable()
        self.addCleanup(storage_override.disable)

    def _patch(self, target, attr, **kwargs):
        patcher = mock.patch.object(target, attr, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _peer_handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 0, "msg": "", "data": {"order_no": "x", "status": "group_not_found"}},
        )

    def _peer_ok(self, qr_url: str = "https://wework.example/qr.png") -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0, "msg": "",
                    "data": {"order_no": "x", "status": "ok", "qr_url": qr_url},
                },
            )

        self._patch(
            order_qr_fetch, "_build_transport",
            return_value=httpx.MockTransport(handler),
        )

    def _sync_spawn(self, **kwargs):
        return order_qr_fetch._run_fetch(**kwargs)

    def _trigger(self) -> bool:
        with self.captureOnCommitCallbacks(execute=True):
            return order_qr_fetch.maybe_enqueue_fetch(self.order)


# ══ 口径 B：查码台账简化（2026-11-03 省库批）——只写「查到」那一行 ═════════════


class QrLedgerSimplifiedTests(_QrLedgerBase):
    """查码写面的拍板口径（原 D-0361 首查 / 放弃两个 kind 停写）：

    - 任何轮次都 ⛔ 不在鸡蛋铺台账（`JidanpuSyncEvent`）留查码行；
    - **查到**时恰好一条既有成功行（`order.malailai_qr_fetch` ok，reason = 常量）；
    - 首查 / 中间轮（not_found）/ 24h 关窗：零行（失败只 logger，timer 通道口径不变）。
    """

    def test_no_jdp_family_row_is_ever_written(self):
        """负性钉：首查轮次在鸡蛋铺台账零行（旧 `qr.fetch_started` 形态的坟）。"""
        self.assertTrue(self._trigger())
        self.assertEqual(_qr_family_rows(self.order.order_no), [])

    def test_hit_leaves_exactly_one_ok_ledger_row(self):
        """查到：恰一条 `order.malailai_qr_fetch` ok 行 + ready 群码行；族行仍零。"""
        self._peer_ok()
        self.assertTrue(self._trigger())
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR
            ).count(),
            1,
        )
        ok_rows = _qr_ok_rows(self.order.order_no)
        self.assertEqual(len(ok_rows), 1)
        self.assertEqual(ok_rows[0].reason, order_qr_fetch.LEDGER_OK_REASON)
        self.assertEqual(_qr_family_rows(self.order.order_no), [])

    def test_not_found_rounds_write_nothing_and_hit_after_them_still_one_row(self):
        """not_found 中间轮零行；之后查到 ⇒ 只有那一条成功行（不多不少）。"""
        self.assertTrue(self._trigger())
        cache.clear()
        self.assertTrue(self._trigger())
        cache.clear()
        self.assertEqual(_qr_ok_rows(self.order.order_no), [])
        self._peer_ok()
        cache.clear()
        self.assertTrue(self._trigger())
        self.assertEqual(len(_qr_ok_rows(self.order.order_no)), 1)
        self.assertEqual(_qr_family_rows(self.order.order_no), [])

    def test_gate_failure_writes_nothing(self):
        """配置闸没过（关 / 未配）⇒ 零台账（从没进入查码）。"""
        self.config.enabled = False
        self.config.save()
        self.assertFalse(self._trigger())
        self.assertEqual(_qr_family_rows(self.order.order_no), [])
        self.assertEqual(_qr_ok_rows(self.order.order_no), [])


# ══ 时间线接线：qr 节有行显示行；挡单行落 jdp 节 ═══════════════════════════


class TimelineRenderingTests(_BlockedLedgerBase):
    """`build_order_timeline` 的纯函数级判据（端点契约与隔离由
    `tests.test_audit_order_timeline` 钉，这里钉的是新行的渲染）。"""

    def _jdp_items(self, order):
        data = build_order_timeline(order)
        return data["sections"][1]["items"]  # jidanpu_sync

    def _qr_items(self, order):
        data = build_order_timeline(order)
        return data["sections"][5]["items"]  # qr_fetch

    def test_blocked_row_renders_in_the_jdp_section_with_human_label(self):
        """挡单行（target_type="order"）天然落进 jdp 节，人话短名渲染。"""
        order2 = self._free_launch(ORDER_NO_2)
        self._suspend(order2)
        items = [
            item for item in self._jdp_items(order2)
            if item["kind"] == KIND_BLOCKED_LITERAL
        ]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind_label"], "发单被熔断暂缓投递（挂起期挡单）")
        self.assertEqual(items[0]["status"], "skipped")

    def test_qr_section_shows_only_the_hit_row_and_hint_when_empty(self):
        """qr 节（2026-11-03 改准）：只显示查到行；无行保留如实提示语。

        ⚠️ 旧行（首查 / 放弃两个 kind 的台账行）即便库里还留着（发版前的历史行）
        也⛔ 不进本节——本节只认 OperationLog 的查到行，历史 kind 行不渲染。
        """
        # 用**真实写口**落那一条查到行（⛔ 不手拼字段：写形唯一出处是 _record_ledger）。
        order_qr_fetch._record_ledger(
            self.order, reason=order_qr_fetch.LEDGER_OK_REASON, ok=True
        )
        # 发版前的历史行（旧 kind）：本节⛔ 不显示。
        JidanpuSyncEvent.objects.create(
            direction="out", kind="qr.fetch_started", status="done",
            target_type="order_qr", target_ref=self.order.order_no,
            reason="开始自动补拉群二维码（首查；查到前的中间轮次不留行）",
        )
        items = self._qr_items(self.order)
        self.assertEqual([item["kind"] for item in items], ["qr_fetch.hit"])
        self.assertEqual(items[0]["status"], "ok")
        self.assertEqual(items[0]["summary"], order_qr_fetch.LEDGER_OK_REASON)
        # 阳性对照的另一面：没有查到行的单保留如实提示语。
        order3 = self._free_launch(ORDER_NO_3)
        hint_items = self._qr_items(order3)
        self.assertEqual(len(hint_items), 1)
        self.assertIn("查码只在查到时留行", hint_items[0]["summary"])
