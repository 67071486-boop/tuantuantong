"""Task 8：上家侧投递 worker（取活租约 / 退避 / 投递前复核 / 自动停绑 / 命令）。

spec §16.2 / §16.5 / §16.6。

判据分五层，缺一层就漏一种错法：

① **取活**：`claim_due_events` 必须 `select_for_update(skip_locked=True)`（spec §7.7），
   打**租约**（`next_attempt_at = now + 300`）⇒ 同一轮不会把同一行取两次；
   且取活**不在事务里发 HTTP**（spec §16.6 第 2 条）。
② **退避**：失败不再直接终态——第一次失败 ⇒ `pending` + `now + 60`；
   第 1–4 次失败各取一档（含 `1h`），**第五次**（`attempts` 到手 5）⇒ `failed` 终态
   。
③ **成功**：`done` + `processed_at`，绑定 `last_ok_at` / 清零（spec §16.6 第 4 条）。
④ **撤单投递前复核**（spec §16.2，承重）：仍是接手人 ⇒ **不发**（作废）；单已软删 ⇒ 发；
   单不存在 ⇒ 不发；换人 ⇒ 发；非撤单类事件**不做**复核。
⑤ **自动停绑**：`consecutive_failures >= 10` ⇒ `REVOKED` + `revoked_at` + 人话
   `last_error` + **一条**操作日志（`system.peer_binding_auto_revoke`）。

⚠️ 判据一律钉**字面值**（`timedelta(seconds=60)` / `"连续失败 10 次已自动停止推送"`），
⛔ 不与实现里的常量对拍（E-0024：自指断言在被断言的常量改坏时不会红）。
⚠️ `mock.patch` 打在**被查找的那个模块**（`apps.business.peer_worker.push_events`），
⛔ 不是定义它的 `peer_client`（E-0029）。
⚠️ 夹具一律**合成值**（E-0077）：域名 `*.test.invalid`、单号 `2026…`。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import io
import json
import signal
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test import Client, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import peer_dispatch, peer_wakeup, peer_worker
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import PeerRejected, PeerUnreachable
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import (
    claim_due_events,
    deliver_one,
    run_round,
)

PEER_BASE_URL = "https://peer-down.test.invalid"


class _WorkerFixture(TenantTestCase):
    """一条生效的「我方为上家」的绑定 + 一个固定时刻。"""

    def setUp(self):
        self.game = Game.objects.create(name="worker 夹具游戏")
        self.account = get_user_model().objects.create_user(
            username="peer-worker-account", password="pw-worker"
        )
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url=PEER_BASE_URL,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        self.now = timezone.now()

    def _event(self, *, kind="order.assign", target_ref="A-1", attempts=0,
               direction="out", **over):
        return PeerSyncEvent.objects.create(
            binding_role="downstream",
            binding_id=str(self.binding.pk),
            direction=direction,
            kind=kind,
            target_type="order",
            target_ref=target_ref,
            payload={"status": OrderStatus.IN_PROGRESS},
            idempotency_key=uuid.uuid4().hex,
            attempts=attempts,
            **over,
        )

    def _order(self, *, no="20260926000000000901", taker=None, deleted=False,
               with_performance=True):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
        )
        if with_performance:
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=taker
            )
        if deleted:
            order.deleted_at = self.now
            order.save(update_fields=["deleted_at"])
        return order


class DeliveryListenerTests(SimpleTestCase):
    def test_disconnect_falls_back_to_periodic_scan_then_reconnects(self):
        import psycopg

        from apps.business.management.commands import peer_sync as module

        command = module.Command(stderr=io.StringIO())
        stop = threading.Event()
        clock = [0.0]
        scans = []
        first_listener = mock.Mock()
        replacement = mock.Mock()

        def fail_notifications(**_kwargs):
            raise psycopg.OperationalError("synthetic connection drop")
            yield

        first_listener.notifies.side_effect = fail_notifications

        def wait(seconds):
            clock[0] += seconds
            return stop.is_set()

        def scan(_schema, _windows):
            scans.append(clock[0])
            if len(scans) == 3:
                stop.set()
            return False

        def recovered():
            # Reach a fallback deadline while the reconnect attempt was unavailable.
            return replacement if clock[0] >= 11 else None

        with (
            mock.patch.object(module, "_STOP", stop),
            mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(stop, "wait", side_effect=wait),
            mock.patch.object(command, "_scan_delivery", side_effect=scan),
            mock.patch.object(command, "_open_delivery_listener", side_effect=recovered),
            mock.patch.object(module, "LOOP_SLEEP_SECONDS", 5),
        ):
            command._delivery_loop("tenant", listener=first_listener)

        self.assertEqual(scans, [0, 5, 10])
        first_listener.close.assert_called_once_with()
        self.assertIn("大厅摘牌监听断开", command.stderr._out.getvalue())


    def test_reconnection_scans_before_waiting_for_the_next_notification(self):
        from apps.business.management.commands import peer_sync as module

        command = module.Command()
        stop = threading.Event()
        listener = mock.Mock()
        clock = [0.0]
        scans = []

        def wait(seconds):
            clock[0] += seconds
            return stop.is_set()

        def scan(_schema, _windows):
            scans.append(clock[0])
            if len(scans) == 2:
                stop.set()
            return False

        with (
            mock.patch.object(module, "_STOP", stop),
            mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(stop, "wait", side_effect=wait),
            mock.patch.object(command, "_scan_delivery", side_effect=scan),
            mock.patch.object(command, "_open_delivery_listener", return_value=listener),
        ):
            command._delivery_loop("tenant", listener=None)

        self.assertEqual(scans, [0, 1])
        listener.notifies.assert_not_called()
        listener.close.assert_called_once_with()


class HallTakeWakeupTests(_WorkerFixture):
    def _take(self, *, moment=None, reason=""):
        return peer_dispatch._enqueue_hall_row(
            self.binding, kind="hall.take", target_ref="SYN-WAKEUP",
            payload={"status": OrderStatus.IN_PROGRESS}, occurred_at=moment or self.now,
            meta_extra={"leave_reason": reason},
        )

    def test_refresh_keeps_existing_backoff_and_queue_position(self):
        first = self._take()
        deadline = self.now + timedelta(seconds=60)
        PeerSyncEvent.objects.filter(pk=first.pk).update(next_attempt_at=deadline, attempts=1)
        refreshed = self._take(moment=self.now + timedelta(seconds=1), reason="assigned")
        self.assertEqual(refreshed.pk, first.pk)
        self.assertEqual(refreshed.next_attempt_at, deadline)
        self.assertEqual(refreshed.created_at, first.created_at)
        self.assertNotEqual(refreshed.idempotency_key, first.idempotency_key)
        self.assertEqual(refreshed.meta["leave_reason"], "assigned")
        self.assertEqual(claim_due_events(now=self.now), [])

    def test_refresh_during_send_is_reclaimed_before_the_next_event(self):
        first = self._take()
        later = self._event(target_ref="SYN-WAKEUP-LATER")
        sent = []

        def push(_base, _token, events):
            sent.append(events[0])
            if len(sent) == 1:
                self._take(moment=self.now + timedelta(seconds=1), reason="assigned")
            return {"skipped": []}

        with mock.patch.object(peer_worker, "push_events", side_effect=push):
            batch = peer_worker.run_delivery_batch(now=self.now, window=peer_worker.DeliveryWindow())
            self.assertEqual((batch["claimed"], batch["delivered"]), (2, 0))
            first.refresh_from_db()
            later.refresh_from_db()
            self.assertIsNone(first.next_attempt_at)
            self.assertIsNone(later.next_attempt_at)
            self.assertEqual(run_round(now=self.now)["delivered"], 2)

        self.assertEqual([event["target_ref"] for event in sent], [
            "SYN-WAKEUP", "SYN-WAKEUP", "SYN-WAKEUP-LATER",
        ])
        self.assertEqual(sent[1]["leave_reason"], "assigned")

    def test_refresh_of_a_busy_retry_keeps_its_original_ready_time(self):
        first = self._take()
        deadline = self.now + timedelta(seconds=15)
        PeerSyncEvent.objects.filter(pk=first.pk).update(
            next_attempt_at=deadline, reason=peer_worker.LOCK_BUSY_REASON,
        )
        earlier = self._event(target_ref="SYN-READY-EARLIER")
        PeerSyncEvent.objects.filter(pk=earlier.pk).update(created_at=deadline - timedelta(seconds=1))
        seen = []

        def push(_base, _token, events):
            seen.append(events[0]["target_ref"])
            if len(seen) == 2:
                self._take(moment=deadline, reason="assigned")
            return {"skipped": []}

        with mock.patch.object(peer_worker, "push_events", side_effect=push):
            summary = peer_worker.run_delivery_batch(now=deadline, window=peer_worker.DeliveryWindow())
        first.refresh_from_db()
        self.assertEqual(summary["delivered"], 1)
        self.assertEqual(first.next_attempt_at, deadline)
        self.assertEqual(seen, ["SYN-READY-EARLIER", "SYN-WAKEUP"])

    def test_success_after_a_throw_was_voided_keeps_void_reason_and_health(self):
        throw = self._event(kind="hall.throw", target_ref="SYN-WAKEUP")
        self.binding.consecutive_failures = 3
        self.binding.save(update_fields=["consecutive_failures"])

        def push(_base, _token, _events):
            self._take(reason="assigned")
            return {"skipped": []}

        with mock.patch.object(peer_worker, "push_events", side_effect=push):
            self.assertEqual(run_round(now=self.now)["delivered"], 0)

        throw.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(throw.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(throw.reason, peer_dispatch.VOIDED_THROW_REASON)
        self.assertEqual(self.binding.consecutive_failures, 3)

    def test_stop_leaves_unsent_due_events_available(self):
        first = self._event()
        original_deadline = self.now - timedelta(seconds=5)
        second = self._event(next_attempt_at=original_deadline)
        stop = threading.Event()

        def push(_base, _token, _events):
            stop.set()
            return {"skipped": []}

        with mock.patch.object(peer_worker, "push_events", side_effect=push) as send:
            summary = peer_worker.run_delivery_batch(
                now=self.now, window=peer_worker.DeliveryWindow(), should_stop=stop.is_set,
            )
        send.assert_called_once()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(summary["claimed"], 2)
        self.assertEqual(first.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(second.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(second.next_attempt_at, original_deadline)

    def test_connection_failures_in_multiple_batches_count_once_per_window(self):
        window = peer_worker.DeliveryWindow()
        with mock.patch.object(peer_worker, "push_events", side_effect=PeerUnreachable("offline")):
            for n in range(3):
                self._event(target_ref=f"SYN-FAIL-{n}")
                summary = peer_worker.run_delivery_batch(now=self.now, window=window)
                self.assertEqual((summary["claimed"], summary["retried"]), (1, 1))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 0)
        window.close(now=self.now, since=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 1)

    def test_success_in_a_later_batch_cancels_the_same_windows_failure(self):
        window = peer_worker.DeliveryWindow()
        self._event(target_ref="SYN-FAIL")
        with mock.patch.object(peer_worker, "push_events", side_effect=PeerUnreachable("offline")):
            peer_worker.run_delivery_batch(now=self.now, window=window)
        self._event(target_ref="SYN-OK")
        with mock.patch.object(peer_worker, "push_events", return_value={"skipped": []}):
            peer_worker.run_delivery_batch(now=self.now, window=window)
        window.close(now=self.now, since=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 0)

    def test_reconcile_does_not_requeue_a_row_changed_during_watermark_read(self):
        row = self._event(
            status=PeerSyncEvent.Status.DONE, processed_at=self.now,
            meta={"occurred_at": self.now.isoformat()},
        )

        def watermark(*args, **kwargs):
            PeerSyncEvent.objects.filter(pk=row.pk).update(reason="已作废")
            return {}

        with mock.patch.object(peer_worker, "fetch_applied_watermarks", side_effect=watermark):
            self.assertEqual(peer_worker.run_reconcile(now=self.now)["requeued"], 0)
        row.refresh_from_db()
        self.assertEqual((row.status, row.reason), (PeerSyncEvent.Status.DONE, "已作废"))


class ConstantsTests(TenantTestCase):
    """四个常量按 spec §16.6 的**字面值**（⛔ 不拿常量与常量对拍）。"""

    def test_the_pinned_constants_are_the_spec_values(self):
        self.assertEqual(peer_worker.RETRY_BACKOFF_SECONDS, (60, 300, 900, 3600))
        self.assertEqual(peer_worker.AUTO_REVOKE_AFTER_FAILURES, 10)
        self.assertEqual(peer_worker.LEASE_SECONDS, 300)
        self.assertEqual(peer_worker.BATCH_SIZE, 50)


class ClaimDueEventsTests(_WorkerFixture):
    """① 取活：`skip_locked` + 租约 + 不在事务里发 HTTP。"""

    def test_claim_uses_skip_locked(self):
        """spec §7.7：取活必须带 `SKIP LOCKED`——用捕获到的 SQL 断言，⛔ 不靠读代码。

        判别力：把 `skip_locked=True` 去掉 ⇒ 捕获到的 SQL 里没有 `SKIP LOCKED` ⇒ 红。
        """
        self._event()
        with CaptureQueriesContext(connection) as captured:
            claim_due_events(now=self.now)
        sql = " ".join(query["sql"] for query in captured.captured_queries).upper()
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("SKIP LOCKED", sql)

    def test_claim_stamps_the_lease_so_the_same_row_is_not_claimed_twice(self):
        event = self._event()
        self.assertEqual(claim_due_events(now=self.now), [event.pk])
        event.refresh_from_db()
        # 租约 = now + 300 秒（字面值，⛔ 不读 `LEASE_SECONDS`）。
        self.assertEqual(event.next_attempt_at, self.now + timedelta(seconds=300))
        # 同一时刻再取 ⇒ 一个都取不到（租约把它占住了）。
        self.assertEqual(claim_due_events(now=self.now), [])
        # 租约过去之后才重新可投。
        self.assertEqual(
            claim_due_events(now=self.now + timedelta(seconds=301)), [event.pk]
        )

    def test_claim_only_takes_pending_out_events(self):
        """已 `done` / 方向是 `in` / 未到点的行一律不进这一批。"""
        self._event(target_ref="A-PENDING")
        self._event(target_ref="A-DONE", status=PeerSyncEvent.Status.DONE)
        self._event(target_ref="A-FAILED", status=PeerSyncEvent.Status.FAILED)
        self._event(target_ref="A-IN", direction="in")
        self._event(target_ref="A-FUTURE",
                    next_attempt_at=self.now + timedelta(seconds=600))

        claimed = claim_due_events(now=self.now)

        self.assertEqual(
            sorted(PeerSyncEvent.objects.get(pk=pk).target_ref for pk in claimed),
            ["A-PENDING"],
        )

    def test_claim_takes_at_most_the_batch_size(self):
        for index in range(51):
            self._event(target_ref=f"A-{index:03d}")
        self.assertEqual(len(claim_due_events(now=self.now)), 50)
        self.assertEqual(PeerSyncEvent.objects.filter(
            next_attempt_at__isnull=True).count(), 1)

    def test_claim_does_not_send_any_http(self):
        """spec §16.6 第 2 条：投递在事务**外**做 ⇒ 取活期间一次出站都没有。"""
        self._event()
        with mock.patch("apps.business.peer_worker.push_events") as push:
            claim_due_events(now=self.now)
        push.assert_not_called()


class DeliverBackoffTests(_WorkerFixture):
    """② / ③：失败退避（四档全可达，第 5 次终态）、成功清零。"""

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_the_first_failure_is_rescheduled_with_the_first_backoff(self, _push):
        result = deliver_one(self._event().pk, now=self.now)
        self.assertEqual(result, "retry")
        event = PeerSyncEvent.objects.get()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.next_attempt_at, self.now + timedelta(seconds=60))
        self.assertEqual(event.reason, "connection refused")
        self.assertIsNone(event.processed_at)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 1)
        self.assertEqual(self.binding.last_error, "connection refused")
        self.assertEqual(self.binding.last_error_at, self.now)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_second_and_third_failures_walk_the_backoff_ladder(self, _push):
        first = self._event(target_ref="A-2", attempts=1)
        second = self._event(target_ref="A-3", attempts=2)

        deliver_one(first.pk, now=self.now)
        deliver_one(second.pk, now=self.now)

        first.refresh_from_db()
        second.refresh_from_db()
        # 第二次失败 ⇒ 5 分钟；第三次失败 ⇒ 15 分钟（字面值）。
        self.assertEqual(first.next_attempt_at, self.now + timedelta(seconds=300))
        self.assertEqual(second.next_attempt_at, self.now + timedelta(seconds=900))
        self.assertEqual(first.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(second.status, PeerSyncEvent.Status.PENDING)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_the_fourth_failure_takes_the_last_ladder_step(self, _push):
        """⭐ 修复轮（Minor 4）：第 4 次失败仍可重投，退避取到 **1h**（最后那一档）。

        判别力：把终态判据写成 `>=`（旧口径）⇒ 本用例红在 `failed` 上，
        而 `1h` 那一档就永远不可达（spec §16.6 第 3 条的四档少一档）。
        """
        event = self._event(attempts=3)

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "retry")
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.attempts, 4)
        self.assertEqual(event.next_attempt_at, self.now + timedelta(seconds=3600))
        self.assertEqual(event.reason, "connection refused")
        self.assertIsNone(event.processed_at)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_the_fifth_failure_becomes_terminal(self, _push):
        """四档退避都走完之后（第 5 次投递）才转 `failed` 终态。"""
        event = self._event(attempts=4)

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
        self.assertEqual(event.reason, "connection refused")
        self.assertIsNone(event.processed_at)
        self.assertIsNone(event.next_attempt_at)

    @mock.patch("apps.business.peer_worker.push_events", return_value={})
    def test_success_marks_done_and_clears_the_failure_count(self, push):
        self.binding.consecutive_failures = 3
        self.binding.last_error = "上一次的旧失败"
        self.binding.save(update_fields=["consecutive_failures", "last_error"])
        event = self._event()

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        event.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.processed_at, self.now)
        self.assertEqual(event.reason, "")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(self.binding.consecutive_failures, 0)
        self.assertEqual(self.binding.last_ok_at, self.now)
        push.assert_called_once()

    @mock.patch(
        "apps.business.peer_worker.push_events",
        return_value={"skipped_details": [
            {"target_ref": "A-1", "code": "stale", "reason": "对端已有更新的"},
        ]},
    )
    def test_a_benign_stale_skip_counts_as_success(self, _push):
        """spec §16.5：`stale` 是良性 ⇒ `done`（⛔ 不是失败）。"""
        event = self._event()
        self.assertEqual(deliver_one(event.pk, now=self.now), "done")
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.reason, "")

    @mock.patch("apps.business.peer_worker.push_events", return_value={})
    def test_the_meta_is_spread_at_the_top_level_and_payload_wins(self, push):
        """⭐ 跨进程契约：事件级元数据在**顶层**、且展开排在 `payload` **之前**。

        判别力：把 `**(event.meta or {})` 删掉 ⇒ 前三个断言红；把它挪到 `payload`
        **之后** ⇒ 第四条（meta 里的 `payload` 键不得顶掉真载荷）红。
        """
        event = self._event(meta={
            "occurred_at": "2026-09-26T10:00:00+08:00",
            "withdraw_reason": "taker_removed",
            "actor_name": "测试客服甲",
            # 故意在 meta 里放一个同名键：它**不得**顶掉真正的订单载荷。
            "payload": {"nope": 1},
        })

        deliver_one(event.pk, now=self.now)

        sent = push.call_args.args[2][0]
        self.assertEqual(sent["occurred_at"], "2026-09-26T10:00:00+08:00")
        self.assertEqual(sent["withdraw_reason"], "taker_removed")
        self.assertEqual(sent["actor_name"], "测试客服甲")
        self.assertEqual(sent["payload"], event.payload)
        self.assertEqual(sent["payload"]["status"], OrderStatus.IN_PROGRESS)
        for key in ("occurred_at", "withdraw_reason", "actor_name"):
            self.assertNotIn(key, sent["payload"])


class VoidedEventTests(_WorkerFixture):
    """⭐ Z6：处理期间被别处**作废**的事件，失败记账不得复活它。

    形状：`_attempt` 读事件（`pending`）→ 发 HTTP（这一拍可能很长）→ 记账。本类让 HTTP
    那一拍**先把行改成 `done` ＋ 非空 reason**（⇔ 人工处置 / 对账终态化 / admin 作废），
    再抛 `PeerUnreachable` ⇒ 失败记账必须看到条件更新命中 0 行并整体放弃。

    ⚠️ 判别力（改回 `event.save(update_fields=[...])` 就红）：旧写法会把内存里那份
    `pending` 的 `status` 原样写回 ⇒ 行变回 `pending`（且作废原因被擦成失败原因），
    本类三条断言（status / reason / attempts）与绑定的两条断言都会红。
    """

    VOID_REASON = "这条事件已被作废：人工处置"

    def _event_voided_during_push(self, *, attempts):
        event = self._event(attempts=attempts)

        def _side_effect(base_url, token, events, **kwargs):
            PeerSyncEvent.objects.filter(pk=event.pk).update(
                status=PeerSyncEvent.Status.DONE,
                reason=self.VOID_REASON,
                processed_at=self.now,
                next_attempt_at=None,
            )
            raise PeerUnreachable("connection refused")

        return event, _side_effect

    def _assert_row_kept(self, event, *, attempts):
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.reason, self.VOID_REASON)
        # ⛔ 行保持原样：`attempts` / 租约列一个字都不许被这次失败记账改写。
        self.assertEqual(event.attempts, attempts)
        self.assertIsNone(event.next_attempt_at)
        # ⛔ 绑定台账也不写：这次投递的结果已经被别处的判决覆盖。
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertEqual(self.binding.consecutive_failures, 0)

    @mock.patch("apps.business.peer_worker.push_events")
    def test_a_voided_event_is_not_resurrected_on_the_retry_path(self, push):
        """第 1–4 次失败（退避重排）那一支。"""
        event, side_effect = self._event_voided_during_push(attempts=0)
        push.side_effect = side_effect
        self.assertEqual(deliver_one(event.pk, now=self.now), "noop")
        self._assert_row_kept(event, attempts=0)

    @mock.patch("apps.business.peer_worker.push_events")
    def test_a_voided_event_is_not_resurrected_on_the_terminal_path(self, push):
        """第 5 次失败（终态 `failed`）那一支。"""
        event, side_effect = self._event_voided_during_push(attempts=4)
        push.side_effect = side_effect
        self.assertEqual(deliver_one(event.pk, now=self.now), "noop")
        self._assert_row_kept(event, attempts=4)


class HallRescueTests(_WorkerFixture):
    """⭐ B1 / M-4（2026-10-25 同步审计批）：`hall.*` 出站事件烧完退避梯子终态化时，
    克隆重投**一条**救援事件（`meta.rescued=true`，每条至多救一次）。

    背景：`hall.*` 不参与对账（`run_reconcile` exclude）⇒ 终态后没有任何收口，
    漏一条 `hall.take` = 下家永久挂错卡。
    """

    def _hall_event(self, *, attempts=4, meta=None, **over):
        merged = {"occurred_at": "2026-10-25T09:00:00+00:00"}
        merged.update(meta or {})
        return self._event(
            kind="hall.take", target_ref="SYN-HALL-1", attempts=attempts,
            meta=merged,
            **over,
        )

    def _rescues(self, *, exclude_pk=None):
        rows = PeerSyncEvent.objects.filter(
            kind="hall.take", status=PeerSyncEvent.Status.PENDING,
        )
        if exclude_pk is not None:
            rows = rows.exclude(pk=exclude_pk)
        return rows

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("对端没回话"),
    )
    def test_a_terminal_hall_event_queues_exactly_one_rescue(self, _push):
        """烧完梯子 ⇒ 终态原样保留 + **一条**救援事件（同形状、attempts=0、+10 分钟）。"""
        event = self._hall_event(attempts=4)

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        rescues = list(self._rescues(exclude_pk=event.pk))
        self.assertEqual(len(rescues), 1)
        rescue = rescues[0]
        self.assertEqual(rescue.binding_role, "downstream")
        self.assertEqual(rescue.binding_id, str(self.binding.pk))
        self.assertEqual(rescue.direction, "out")
        self.assertEqual(rescue.kind, "hall.take")
        self.assertEqual(rescue.target_ref, "SYN-HALL-1")
        self.assertEqual(rescue.attempts, 0)
        self.assertIs(rescue.meta.get("rescued"), True)
        # `occurred_at` 原样继承（接收侧幂等 / 迟到闸的判据，⛔ 不换新时刻）。
        self.assertEqual(rescue.meta.get("occurred_at"), "2026-10-25T09:00:00+00:00")
        self.assertEqual(rescue.payload, event.payload)
        self.assertEqual(
            rescue.next_attempt_at, self.now + timedelta(seconds=600)
        )

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("对端没回话"),
    )
    def test_the_rescue_travels_the_same_delivery_path(self, _push):
        """救援事件与普通事件**同一条投递路**：到点被 `claim_due_events` 正常取活。"""
        event = self._hall_event(attempts=4)
        deliver_one(event.pk, now=self.now)
        rescue = self._rescues(exclude_pk=event.pk).get()

        claimed = claim_due_events(now=self.now + timedelta(seconds=600))

        self.assertIn(rescue.pk, claimed)
        self.assertNotIn(event.pk, claimed)   # 终态行不再被取活

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("对端没回话"),
    )
    def test_a_rescued_event_burning_the_ladder_again_stays_terminal(self, _push):
        """救援事件（`meta.rescued=true`）再烧完 ⇒ 真终态，⛔ 不再排第二条救援。"""
        event = self._hall_event(attempts=4, meta={"rescued": True})

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "failed")
        self.assertEqual(self._rescues(exclude_pk=event.pk).count(), 0)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("对端没回话"),
    )
    def test_a_terminal_order_event_gets_no_rescue(self, _push):
        """订单类事件有自己的对账 / 上行救援，`hall.*` 的救援闸只认 `hall.` 前缀。"""
        event = self._event(kind="order.assign", attempts=4)

        self.assertEqual(deliver_one(event.pk, now=self.now), "failed")

        self.assertEqual(
            PeerSyncEvent.objects.filter(
                status=PeerSyncEvent.Status.PENDING
            ).count(),
            0,
        )


class StaleUpstreamRescueTests(TenantTestCase):
    """⭐ L-1（2026-10-26 审计批，并发实测）：PW-02 重排**不再捞救援行**。

    B1 的 hall 救援克隆行带 `meta.rescued=true`、`attempts` 从 0 重走梯子；它再次烧到
    终态时形状与原事件逐字相同 ⇒ 不排除就会把同一条事实重投第二次。
    """

    def setUp(self):
        self.now = timezone.now()
        self.kefu = get_user_model().objects.create_user(
            username="peer-rescue-kefu", password="pw-rescue"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="救援夹具上家",
            peer_base_url="https://peer-rescue.test.invalid",
            peer_account="peer-rescue-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-rescue-local.test.invalid",
            bound_by=self.kefu,
        )

    def _terminal_event(self, *, rescued: bool) -> PeerSyncEvent:
        event = PeerSyncEvent.objects.create(
            binding_role="upstream",
            binding_id=str(self.binding.pk),
            direction="out",
            kind="order.update",
            target_type="order",
            target_ref="UP-RESCUE-1",
            payload={},
            idempotency_key=uuid.uuid4().hex,
            attempts=5,
            status=PeerSyncEvent.Status.FAILED,
            meta={"rescued": True} if rescued else {},
        )
        # `created_at` 是 `auto_now_add` ⇒ 建完再改成「超龄」。
        PeerSyncEvent.objects.filter(pk=event.pk).update(
            created_at=self.now - timedelta(hours=30)
        )
        event.refresh_from_db()
        return event

    def test_a_rescued_row_is_not_requeued_again(self):
        rescued = self._terminal_event(rescued=True)
        normal = self._terminal_event(rescued=False)

        requeued = peer_worker._rescue_stale_upstream_events(now=self.now)

        # 阳性对照：普通终态行照旧被救（否则「零条」可能只是整条路径没跑）。
        self.assertEqual(requeued, 1)
        rescued.refresh_from_db()
        normal.refresh_from_db()
        self.assertEqual(rescued.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(normal.status, PeerSyncEvent.Status.PENDING)

    def test_the_rescued_flag_is_the_only_thing_that_changes(self):
        """判别力：把 `.exclude(meta__rescued=True)` 去掉 ⇒ 上面那条 `requeued` 会变成 2。"""
        rescued = self._terminal_event(rescued=True)
        self.assertEqual(
            peer_worker._rescue_stale_upstream_events(now=self.now), 0
        )
        rescued.refresh_from_db()
        self.assertEqual(rescued.status, PeerSyncEvent.Status.FAILED)


class WithdrawRecheckTests(_WorkerFixture):
    """④ 撤单投递前复核（spec §16.2，承重）。"""

    def test_a_withdraw_is_voided_when_the_order_still_belongs_to_the_binding(self):
        """仍是接手人（例如"先清后派回同一个账号"）⇒ 不发、只把事件记作废。

        ⭐ 作废**不算一次成功投递** ⇒ ⛔ 不碰绑定的健康列。
        判别力：改回复用"成功记账"（写 `last_ok_at` + 清零计数）⇒ 本用例红在那两条断言上
        （那会替一个**根本不通**的对端刷健康度、并推迟自动停绑）。
        """
        old_ok = self.now - timedelta(hours=3)
        self.binding.consecutive_failures = 4
        self.binding.last_error = "之前的失败"
        self.binding.last_ok_at = old_ok
        self.binding.save(update_fields=[
            "consecutive_failures", "last_error", "last_ok_at",
        ])
        order = self._order(no="20260926000000000960", taker=self.account)
        event = self._event(kind="order.withdraw", target_ref=order.order_no)

        with mock.patch("apps.business.peer_worker.push_events") as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "stale-void")
        push.assert_not_called()
        event.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.reason, "撤单已作废：该账号仍是这张单的当前接手人")
        self.assertEqual(event.processed_at, self.now)
        # ⛔ 绑定的健康列**一个字都没动**。
        self.assertEqual(self.binding.consecutive_failures, 4)
        self.assertEqual(self.binding.last_error, "之前的失败")
        self.assertEqual(self.binding.last_ok_at, old_ok)

    def test_a_withdraw_is_voided_when_the_order_is_absent(self):
        """本地上游台账里没有这张单 ⇒ 撤单无从谈起 ⇒ 作废、不发。"""
        event = self._event(kind="order.withdraw", target_ref="20260926000000009999")
        with mock.patch("apps.business.peer_worker.push_events") as push:
            result = deliver_one(event.pk, now=self.now)
        self.assertEqual(result, "stale-void")
        push.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.reason, "撤单已作废：本地上游台账里没有这张单")

    def test_a_withdraw_is_sent_when_the_order_was_soft_deleted(self):
        """删单 ⇒ 复核**必须放行**（单已删，撤单正是要通知下家这件事）。"""
        order = self._order(
            no="20260926000000000961", taker=self.account, deleted=True
        )
        event = self._event(kind="order.withdraw", target_ref=order.order_no)

        with mock.patch(
            "apps.business.peer_worker.push_events", return_value={}
        ) as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        push.assert_called_once()
        self.assertEqual(push.call_args.args[2][0]["target_ref"], order.order_no)

    def test_a_withdraw_is_sent_when_the_taker_changed(self):
        """当前接手人已经不是这个绑定账号 ⇒ 照发。"""
        other = get_user_model().objects.create_user(
            username="peer-worker-other-taker", password="pw-other"
        )
        order = self._order(no="20260926000000000962", taker=other)
        event = self._event(kind="order.withdraw", target_ref=order.order_no)

        with mock.patch(
            "apps.business.peer_worker.push_events", return_value={}
        ) as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        push.assert_called_once()

    def test_a_withdraw_for_an_order_without_a_performance_row_is_sent(self):
        """没有 MAIN 绩效行（不经录单端点落库的旧单）⇒ 没有接手人 ⇒ 照发。

        判别力：改成会抛 `Performance.DoesNotExist` 的严格取法 ⇒ 本用例以异常红
        （而不是让 `done` 断言红）。
        """
        order = self._order(no="20260926000000000963", with_performance=False)
        event = self._event(kind="order.withdraw", target_ref=order.order_no)

        with mock.patch(
            "apps.business.peer_worker.push_events", return_value={}
        ) as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        push.assert_called_once()

    def test_a_non_withdraw_event_is_not_rechecked(self):
        """复核只对 `order.withdraw` 生效：指派事件即便接手人是本绑定也照发。

        判别力：把复核条件从 `kind == "order.withdraw"` 放宽成"所有事件都复核"
        ⇒ 本用例在 `push.assert_called_once()` 上红。
        """
        order = self._order(no="20260926000000000964", taker=self.account)
        event = self._event(kind="order.assign", target_ref=order.order_no)

        with mock.patch(
            "apps.business.peer_worker.push_events", return_value={}
        ) as push:
            result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "done")
        push.assert_called_once()


class AutoRevokeTests(_WorkerFixture):
    """⑤ 自动停绑（spec §16.6 第 4 条）。"""

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_ten_consecutive_failures_revoke_the_binding_and_log_it(self, _push):
        self.binding.consecutive_failures = 9
        self.binding.save(update_fields=["consecutive_failures"])
        event = self._event()

        result = deliver_one(event.pk, now=self.now)

        self.assertEqual(result, "retry")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.REVOKED)
        self.assertEqual(self.binding.revoked_at, self.now)
        self.assertEqual(self.binding.consecutive_failures, 10)
        self.assertEqual(self.binding.last_error, "连续失败 10 次已自动停止推送")
        self.assertEqual(OperationLog.objects.count(), 1)
        log = OperationLog.objects.get()
        self.assertEqual(log.action, "system.peer_binding_auto_revoke")
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.target_kind, "peer_binding")
        self.assertEqual(log.target_id, str(self.binding.pk))
        self.assertEqual(log.target_name, PEER_BASE_URL)
        self.assertEqual(log.actor_name, "系统")
        self.assertIsNone(log.actor_id)
        self.assertEqual(
            log.summary,
            f"【系统】自动停止了向下家系统{PEER_BASE_URL}推送订单：连续失败 10 次已自动停止推送",
        )

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_nine_failures_do_not_revoke(self, _push):
        """阈值是 `>= 10`：第 9 次失败仍不许停绑（判别力：把 `>=` 写成 `>` 早期触发）。"""
        self.binding.consecutive_failures = 8
        self.binding.save(update_fields=["consecutive_failures"])
        deliver_one(self._event().pk, now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 9)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(OperationLog.objects.count(), 0)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_a_revoked_binding_drops_the_event_and_it_is_never_claimed_again(self, push):
        """⭐ C2（2026-09-26 修复波）：停绑后那条绑定的后续事件 ⇒ 终态 `failed`（`dropped`）。

        判据三条：⛔ 不投、⛔ 不再写第二行日志、⛔ 下一轮**不再被认领**。
        判别力：留在 `pending` 的旧实现 ⇒ `summary["dropped"]` / `status == FAILED` / 下一轮
        `claim_due_events` 为空这三条断言红——而"留在 pending"正是会把一个租户的名额吃光、
        饿死其它绑定的那个形态。
        """
        self.binding.consecutive_failures = 9
        self.binding.save(update_fields=["consecutive_failures"])
        deliver_one(self._event(target_ref="A-1").pk, now=self.now)
        push.reset_mock()

        later = self._event(target_ref="A-2")
        summary = run_round(now=self.now)

        self.assertEqual(summary["dropped"], 1)
        push.assert_not_called()
        later.refresh_from_db()
        self.assertEqual(later.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(later.attempts, 0)
        self.assertEqual(later.reason, "绑定已解除/失效，不再投递")
        self.assertIsNone(later.next_attempt_at)
        self.assertEqual(OperationLog.objects.count(), 1)
        # 下一轮不再被认领（`claim_due_events` 只取 `pending`）。
        self.assertEqual(claim_due_events(now=self.now), [])


class RoundCountingTests(_WorkerFixture):
    """⭐ PW-01（2026-10-20 修复线）：自动停绑的计数口径是「**按轮**」，⛔ 不是「按 HTTP 尝试」。

    旧口径：每次失败 `_record_failure` 就 +1 ⇒ 对端**短暂失联** + 一批 10 条 pending，
    第一轮（约 10 秒）计数就冲到阈值、把绑定打成 REVOKED——停绑是**双向 kill**
    （认证 filter 只认 ACTIVE，下家回传同样 401），恢复只能重走握手。
    新口径（先例 = 入站侧「一批硬跳只 +1」：
    `test_peer_binding_api.py::…::test_the_error_names_the_hard_skip_count_and_the_first_reason`）：

    - 一轮（一个绑定把当批事件处理完）**整轮连接类失败** ⇒ +1 **恰好一次**；
    - 轮内有任一**成功** ⇒ 清零（`_mark_done` 的既有行为）；
    - **业务类**拒绝（`PeerRejected` 信封业务码非 0 / 对端按事件硬跳非 stale 码，
      如 `not_your_order`）**不计入**——对端能回话本身就证明链路是通的；
    - 阈值 `AUTO_REVOKE_AFTER_FAILURES` 数值不动。

    ⚠️ 夹具与断言口径照 `_WorkerFixture`（合成值、钉字面值）。
    """

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_a_round_of_connection_failures_counts_exactly_one(self, _push):
        """一轮 10 条 pending 全因**连接类**失败 ⇒ 计数恰好 +1（⛔ 不是 +10）。

        判别力：把「按轮 +1」改回「按尝试累加」⇒ 计数 10 ≥ 阈值、绑定 REVOKED ⇒
        本用例的 `ACTIVE` / `+1` / `revoked == 0` 三条全红。
        """
        for index in range(10):
            self._event(target_ref=f"A-{index:02d}")

        summary = run_round(now=self.now)

        self.assertEqual(summary["retried"], 10)
        self.assertEqual(summary["revoked"], 0)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 1)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(OperationLog.objects.count(), 0)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerRejected("对端拒收了事件"),
    )
    def test_envelope_rejections_do_not_count_toward_auto_revoke(self, _push):
        """**业务类**拒绝（信封业务码非 0）不计入：预置 9，一轮 10 条被拒 ⇒ 仍是 9、不停绑。

        对端能回信封 = 链路是通的；停绑（双向 kill、恢复要重走握手）只留给「失联」。
        判别力：业务类也被计入 ⇒ 9 + 10 ≥ 10 ⇒ REVOKED + 一条日志 ⇒ 三条断言全红。
        """
        self.binding.consecutive_failures = 9
        self.binding.save(update_fields=["consecutive_failures"])
        for index in range(10):
            self._event(target_ref=f"REJ-{index:02d}")

        summary = run_round(now=self.now)

        self.assertEqual(summary["retried"], 10)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 9)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(OperationLog.objects.count(), 0)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        return_value={"skipped_details": [
            {"target_ref": "SKIP-1", "code": "not_your_order",
             "reason": "单不归这个下家"},
        ]},
    )
    def test_a_hard_skip_does_not_count_toward_auto_revoke(self, _push):
        """对端按**本事件**硬跳（`not_your_order` 一类）也是业务类 ⇒ 不计入停绑计数。

        ⚠️ 事件本身照旧落 `pending` 退避（`tests/test_peer_partial_failure.py` 钉的既有契约
        不因本修复改变）；变的只是健康计数。判别力：计入 ⇒ 10 ≥ 阈值 ⇒ REVOKED ⇒ 红。
        """
        self.binding.consecutive_failures = 9
        self.binding.save(update_fields=["consecutive_failures"])
        event = self._event(target_ref="SKIP-1")

        self.assertEqual(deliver_one(event.pk, now=self.now), "retry")

        event.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(self.binding.consecutive_failures, 9)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_mixed_round_counts_the_connection_failure_once(self):
        """混合轮（2 条连接类失败 + 1 条业务类拒绝、无成功）⇒ +1 **恰好一次**。

        口径：业务类**不计入**、连接类**整轮一次**；轮内没有成功 ⇒ 也不清零。
        判别力：业务类被计入 ⇒ +3 ⇒ 红；连接类按尝试累加 ⇒ +2 ⇒ 红。
        """
        self.binding.consecutive_failures = 5
        self.binding.save(update_fields=["consecutive_failures"])
        conn_a = self._event(target_ref="MIX-CONN-A")
        conn_b = self._event(target_ref="MIX-CONN-B")
        self._event(target_ref="MIX-REJECT")

        def _push(base_url, token, events):
            if events[0]["target_ref"] == "MIX-REJECT":
                raise PeerRejected("对端拒收了事件")
            raise PeerUnreachable("connection refused")

        with mock.patch("apps.business.peer_worker.push_events", side_effect=_push):
            run_round(now=self.now)

        conn_a.refresh_from_db()
        conn_b.refresh_from_db()
        self.assertEqual(conn_a.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(conn_b.status, PeerSyncEvent.Status.PENDING)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 6)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)

    def test_a_round_with_any_success_clears_instead_of_counting(self):
        """轮内任一**成功** ⇒ 清零（既有行为保持），同轮的连接类失败 ⛔ 不再 +1。

        判别力：成功被无视、仍按连接失败 +1 ⇒ 5 + 1 = 6 ⇒ 红；
        清零本身被删掉 ⇒ 停在 5 ⇒ 也红。
        """
        self.binding.consecutive_failures = 5
        self.binding.last_error = "上一次的旧失败"
        self.binding.save(update_fields=["consecutive_failures", "last_error"])
        self._event(target_ref="MIX-FAIL")
        self._event(target_ref="MIX-OK")

        def _push(base_url, token, events):
            if events[0]["target_ref"] == "MIX-OK":
                return {}
            raise PeerUnreachable("connection refused")

        with mock.patch("apps.business.peer_worker.push_events", side_effect=_push):
            summary = run_round(now=self.now)

        self.assertEqual(summary["delivered"], 1)
        self.assertEqual(summary["retried"], 1)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 0)
        self.assertEqual(self.binding.last_ok_at, self.now)

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_nine_failing_rounds_do_not_revoke_and_the_tenth_does(self, _push):
        """阈值语义搬到「轮」上不变：9 个整轮失败 = 9；第 10 个整轮才冲线停绑。

        判别力：改回「按尝试累加」⇒ 第一轮（10 条）就 REVOKED ⇒ 「第 1 轮后仍 ACTIVE」红；
        改成「按轮但阈值提前」⇒ 不足 10 轮就 REVOKED ⇒ 同样红。
        ⚠️ 每轮把事件放回可投（真实环境里退避到期后下一轮认领；夹具直接摘租约）。
        """
        events = [self._event(target_ref=f"A-{index:02d}") for index in range(10)]
        summary: dict = {}
        for round_index in range(1, 11):
            if round_index > 1:
                PeerSyncEvent.objects.filter(
                    pk__in=[event.pk for event in events]
                ).update(
                    status=PeerSyncEvent.Status.PENDING, next_attempt_at=None,
                )
            summary = run_round(now=self.now)
            self.binding.refresh_from_db()
            if round_index < 10:
                self.assertEqual(
                    self.binding.state, PeerDownstreamBinding.State.ACTIVE,
                    f"第 {round_index} 轮就停绑了（不是按轮的口径）",
                )
                self.assertEqual(self.binding.consecutive_failures, round_index)
                self.assertEqual(summary["revoked"], 0)
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.REVOKED)
        self.assertEqual(self.binding.revoked_at, self.now)
        self.assertEqual(self.binding.consecutive_failures, 10)
        self.assertEqual(self.binding.last_error, "连续失败 10 次已自动停止推送")
        self.assertEqual(summary["revoked"], 1)
        self.assertEqual(OperationLog.objects.count(), 1)


#: 握手端点的路径与租户域名（`TenantTestCase` 自带租户的域名，与 `PeerTokenAuth` 用例同形）。
BIND_URL = "/api/business/peer-sync/bind/"
PEER_DOMAIN = "tenant.test.com"


class RebindResetsFailuresTests(_WorkerFixture):
    """⭐ 修复轮（Important 1）：重新握手 = 重新开始 ⇒ 复位失败台账，下一次失败不被立刻停绑。"""

    def _rebind(self):
        return Client(SERVER_NAME=PEER_DOMAIN).post(
            BIND_URL,
            json.dumps({
                "account": "peer-worker-account",
                "password": "pw-worker",
                "peer_base_url": "https://peer-down2.test.invalid",
                "token_out": new_token(),
            }),
            content_type="application/json",
        )

    @mock.patch(
        "apps.business.peer_worker.push_events",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_rebinding_resets_the_counters_and_the_next_failure_does_not_revoke(self, _push):
        """计数 9 的绑定重新握手 ⇒ 计数 0 / 台账清空；再接一次失败 ⇒ **只 +1、不停绑**。

        判别力：`bind/` 的 `defaults` 里去掉那三列 ⇒
        ① 复位断言红；② 再失败一次会 `9 + 1 = 10` ⇒ 绑定变 `REVOKED`、并多写一条操作日志
        （"重绑等于没用"）⇒ 后面几条断言也红。
        """
        self.binding.consecutive_failures = 9
        self.binding.last_error = "连续失败 9 次之前的旧错"
        self.binding.last_error_at = self.now - timedelta(hours=1)
        self.binding.save(
            update_fields=["consecutive_failures", "last_error", "last_error_at"]
        )

        resp = self._rebind()
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(self.binding.consecutive_failures, 0)
        self.assertEqual(self.binding.last_error, "")
        self.assertIsNone(self.binding.last_error_at)

        # 重绑之后的第一条失败：计数 0 → 1，⛔ 不许被自动停绑。
        result = deliver_one(self._event().pk, now=self.now)
        self.assertEqual(result, "retry")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertEqual(self.binding.consecutive_failures, 1)
        self.assertFalse(
            OperationLog.objects.filter(
                action="system.peer_binding_auto_revoke"
            ).exists()
        )


class RunRoundTests(_WorkerFixture):
    """`run_round` 的汇总与逐事件异常边界。"""

    def test_run_round_summarises_every_outcome(self):
        order = self._order(no="20260926000000000950", taker=self.account)
        self._event(target_ref="A-OK")
        self._event(target_ref="A-RETRY")
        self._event(target_ref="A-TERMINAL", attempts=4)
        self._event(kind="order.withdraw", target_ref=order.order_no)

        other_account = get_user_model().objects.create_user(
            username="peer-worker-doomed", password="pw-doomed"
        )
        other_binding = PeerDownstreamBinding.objects.create(
            account=other_account,
            peer_base_url="https://peer-down2.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            consecutive_failures=9,
        )
        doomed = PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(other_binding.pk),
            direction="out", kind="order.assign", target_type="order",
            target_ref="B-FAIL", payload={"status": OrderStatus.IN_PROGRESS},
            idempotency_key=uuid.uuid4().hex,
        )

        # ⭐ C2：绑定已经 `REVOKED` 的事件 ⇒ 终态 `failed`（`dropped`），⛔ 一个 HTTP 都不发。
        dropped_account = get_user_model().objects.create_user(
            username="peer-worker-dropped", password="pw-dropped"
        )
        dropped_binding = PeerDownstreamBinding.objects.create(
            account=dropped_account,
            peer_base_url="https://peer-down3.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            state=PeerDownstreamBinding.State.REVOKED,
        )
        dropped = PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(dropped_binding.pk),
            direction="out", kind="order.assign", target_type="order",
            target_ref="C-DROPPED", payload={"status": OrderStatus.IN_PROGRESS},
            idempotency_key=uuid.uuid4().hex,
        )

        def _push(base_url, token, events):
            if events[0]["target_ref"] == "A-OK":
                return {}
            raise PeerUnreachable("connection refused")

        with mock.patch(
            "apps.business.peer_worker.push_events", side_effect=_push
        ) as push:
            summary = run_round(now=self.now)

        self.assertEqual(summary["delivered"], 1)
        self.assertEqual(summary["retried"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["voided"], 1)
        self.assertEqual(summary["dropped"], 1)
        self.assertEqual(summary["revoked"], 1)
        self.assertEqual(summary["errors"], [])
        # 撤单被复核拦下 ⇒ 一条都不发（按对端单号认领那次调用）。
        sent_refs = [call.args[2][0]["target_ref"] for call in push.call_args_list]
        self.assertNotIn(order.order_no, sent_refs)
        self.assertNotIn("C-DROPPED", sent_refs)
        self.assertEqual(
            sorted(sent_refs), ["A-OK", "A-RETRY", "A-TERMINAL", "B-FAIL"]
        )
        doomed.refresh_from_db()
        other_binding.refresh_from_db()
        self.assertEqual(doomed.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(doomed.next_attempt_at, self.now + timedelta(seconds=60))
        self.assertEqual(other_binding.state, PeerDownstreamBinding.State.REVOKED)
        dropped.refresh_from_db()
        self.assertEqual(dropped.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(dropped.reason, "绑定已解除/失效，不再投递")

    def test_run_round_isolates_an_unexpected_event_failure(self):
        """一个事件抛**未预期**异常 ⇒ 其余事件照跑，它进 `errors`（⛔ 不带 `str(exc)`）。"""
        bad = self._event(target_ref="A-BOOM")
        self._event(target_ref="A-OK")

        def _push(base_url, token, events):
            if events[0]["target_ref"] == "A-BOOM":
                raise RuntimeError("boom-secret")
            return {}

        with mock.patch("apps.business.peer_worker.push_events", side_effect=_push):
            summary = run_round(now=self.now)

        self.assertEqual(summary["delivered"], 1)
        self.assertEqual(summary["errors"], [f"事件 {bad.pk}: RuntimeError"])
        bad.refresh_from_db()
        self.assertEqual(bad.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(bad.attempts, 0)

    def test_run_round_is_a_noop_when_nothing_is_due(self):
        summary = run_round(now=self.now)
        self.assertEqual(
            summary,
            {"delivered": 0, "retried": 0, "failed": 0, "voided": 0,
             "dropped": 0, "revoked": 0, "errors": []},
        )


@contextmanager
def _noop_context(tenant):
    """把命令模块里的 `tenant_context` 换成「什么都不做」（只测循环逻辑）。"""
    yield


def _patch_tenants(module, tenants):
    """把 `Tenant.objects` 换成回给定租户的替身，返回 `(patcher, objects)`。"""
    patcher = mock.patch.object(module.Tenant, "objects")
    objects = patcher.start()
    objects.exclude.return_value.order_by.return_value = list(tenants)
    return patcher, objects


class PeerSyncCommandTests(TenantTestCase):
    """命令外壳（照 `jidanpu_sync` 的骨架）：互斥 / 未知租户 / 逐租户边界 / 退出码。"""

    def test_an_unknown_tenant_schema_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("peer_sync", "--tenant", "no-such-schema")

    def test_once_and_loop_together_is_rejected(self):
        """`--once --loop` 是矛盾的：⛔ 不许静默按 `--loop` 常驻。

        ⚠️ 守卫失效的失败形态必须是 FAIL/ERROR、不能是挂死（E-0064/E-0075）⇒ 把
        `_loop` 换成「一进就抛」的哨兵——守卫坏了的话它会先被哨兵打红，而不是挂死。
        """
        from apps.business.management.commands import peer_sync as module

        def _no_looping(schema):
            raise AssertionError("守卫失效：命令进入了常驻循环（--once 被忽略）")

        with mock.patch.object(module.Command, "_loop", side_effect=_no_looping):
            with self.assertRaises(CommandError):
                call_command("peer_sync", "--once", "--loop")

    def test_loop_registers_term_and_int_handlers(self):
        """⭐ M-12（2026-10-25 审计批 C9）：`--loop` 注册 SIGTERM / SIGINT → 停止位。

        ⚠️ 桩掉 `signal.signal`：测试进程里装真 handler 会污染全局状态（测试跑完
        SIGTERM 仍指向我们的停止位）。判据钉**接线**（哪两个信号、接的是哪个函数）。
        """
        from apps.business.management.commands import peer_sync as module

        registered = []

        def _fake_signal(signum, handler):
            registered.append((signum, handler))

        with mock.patch.object(module, "signal") as fake_signal:
            # ⛔ 假 signal 模块也要给**真常量**：否则 handle 里读到的 SIGTERM 是个
            #    MagicMock，判据就在和影子比对（E-0024 同族）。
            fake_signal.SIGTERM = signal.SIGTERM
            fake_signal.SIGINT = signal.SIGINT
            fake_signal.signal.side_effect = _fake_signal
            with mock.patch.object(module, "_STOP", new=threading.Event()):
                with mock.patch.object(
                    module.Command, "_run_once", side_effect=AssertionError("不许跑轮")
                ):
                    with mock.patch.object(module.Command, "_loop"):
                        call_command("peer_sync", "--loop")
        self.assertEqual(
            registered,
            [
                (signal.SIGTERM, module._request_stop),
                (signal.SIGINT, module._request_stop),
            ],
        )

    def test_loop_joins_maintenance_and_closes_delivery_connection(self):
        from apps.business.management.commands import peer_sync as module

        stop = threading.Event()
        maintained = threading.Event()
        threads = []

        def maintain(schema):
            threads.append(threading.current_thread())
            maintained.set()
            stop.wait(2)

        command = module.Command()
        with (
            mock.patch.object(module, "_STOP", stop),
            mock.patch.object(command, "_maintenance_loop", side_effect=maintain),
            mock.patch.object(command, "_open_delivery_listener", return_value=None),
            mock.patch.object(
                command, "_delivery_loop",
                side_effect=lambda schema, *, listener: self.assertTrue(maintained.wait(2)),
            ),
            mock.patch.object(module.connection, "close") as close,
        ):
            command._loop(None)
        self.assertTrue(stop.is_set())
        self.assertFalse(threads[0].is_alive())
        close.assert_called_once_with()

    def test_request_stop_only_sets_the_flag(self):
        """信号处理器只置位、不抛不重活（信号上下文不可重入——它是被 ``signal`` 调的）。"""
        from apps.business.management.commands import peer_sync as module

        stop = threading.Event()
        with mock.patch.object(module, "_STOP", new=stop):
            module._request_stop(signal.SIGTERM, None)
        self.assertTrue(stop.is_set())

    def test_once_exits_zero_and_prints_the_round_summary(self):
        from apps.business.management.commands import peer_sync as module

        summary = {"delivered": 2, "retried": 1, "failed": 0, "voided": 0,
                   "revoked": 0, "errors": []}
        # ⭐ P3 汇总行多了「对账补 / 对账失败」两个计数（见 `_run_tenant`）。
        reconcile = {"requeued": 3, "errors": 1}
        out = io.StringIO()
        with mock.patch.object(module, "tenant_context", _noop_context):
            patcher, _ = _patch_tenants(module, [mock.Mock(schema_name="tenant-a")])
            try:
                with mock.patch.object(
                    module.peer_worker, "run_round", return_value=summary
                ) as run, mock.patch.object(
                    module.peer_worker, "run_reconcile", return_value=reconcile
                ) as run_reconcile:
                    call_command("peer_sync", "--once", stdout=out)
            finally:
                patcher.stop()
        run.assert_called_once()
        run_reconcile.assert_called_once()
        self.assertIn("tenant-a", out.getvalue())
        self.assertIn("投递 2", out.getvalue())
        self.assertIn("重试 1", out.getvalue())
        self.assertIn("对账补 3", out.getvalue())
        self.assertIn("对账失败 1", out.getvalue())

    def test_reconcile_errors_alone_do_not_flip_the_exit_code(self):
        """对账失败是**预期内**噪音（对端不可达 / 拒收）⇒ ⛔ 不设退出码，也不走 stderr。

        判别力：把 `reconcile["errors"]` 并进「未预期失败」的判据 ⇒ 本用例以
        `CommandError` 红（`call_command` 会把它抛出来）。
        """
        from apps.business.management.commands import peer_sync as module

        summary = {"delivered": 0, "retried": 0, "failed": 0, "voided": 0,
                   "revoked": 0, "errors": []}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "tenant_context", _noop_context):
            patcher, _ = _patch_tenants(module, [mock.Mock(schema_name="tenant-a")])
            try:
                with mock.patch.object(
                    module.peer_worker, "run_round", return_value=summary
                ), mock.patch.object(
                    module.peer_worker, "run_reconcile",
                    return_value={"requeued": 0, "errors": 4},
                ):
                    call_command("peer_sync", "--once", stdout=out, stderr=err)
            finally:
                patcher.stop()
        self.assertIn("对账失败 4", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_once_exits_one_when_a_tenant_reports_unexpected_errors(self):
        from apps.business.management.commands import peer_sync as module

        summary = {"delivered": 0, "retried": 0, "failed": 0, "voided": 0,
                   "revoked": 0, "errors": ["事件 7: RuntimeError"]}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "tenant_context", _noop_context):
            patcher, _ = _patch_tenants(module, [mock.Mock(schema_name="tenant-a")])
            try:
                with mock.patch.object(
                    module.peer_worker, "run_round", return_value=summary
                ):
                    with self.assertRaises(CommandError) as caught:
                        call_command("peer_sync", "--once", stdout=out, stderr=err)
            finally:
                patcher.stop()
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn("tenant-a", err.getvalue())

    def test_one_tenant_blowing_up_does_not_stop_the_others(self):
        """逐租户独立异常边界：一个租户失败，下一个照跑；整趟以退出码 1 收尾。"""
        from apps.business.management.commands import peer_sync as module

        first = mock.Mock(schema_name="tenant-a")
        second = mock.Mock(schema_name="tenant-b")
        calls: list[str] = []

        def _run_tenant(self, tenant):
            calls.append(tenant.schema_name)
            self.stdout.write(f"{tenant.schema_name}: 测试汇总")
            if tenant.schema_name == "tenant-a":
                raise RuntimeError("boom")
            return False

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "tenant_context", _noop_context):
            patcher, _ = _patch_tenants(module, [first, second])
            try:
                with mock.patch.object(module.Command, "_run_tenant", _run_tenant):
                    with self.assertRaises(CommandError) as caught:
                        call_command("peer_sync", "--once", stdout=out, stderr=err)
            finally:
                patcher.stop()
        self.assertEqual(calls, ["tenant-a", "tenant-b"])
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn("tenant-a: 测试汇总", out.getvalue())
        self.assertIn("tenant-b: 测试汇总", out.getvalue())
        self.assertIn("RuntimeError", err.getvalue())
        # ⛔ 不带异常正文（可能含凭据 / 回包片段）。
        self.assertNotIn("boom", err.getvalue())

    def test_the_tenant_flag_runs_only_that_schema(self):
        """`--tenant <存在的 schema>` 的正向路径：只跑这一个租户。"""
        from apps.business.management.commands import peer_sync as module

        chosen = mock.Mock(schema_name="tenant-a")
        calls: list[str] = []

        def _run_tenant(self, tenant):
            calls.append(tenant.schema_name)
            return False

        summary = {"delivered": 0, "retried": 0, "failed": 0, "voided": 0,
                   "revoked": 0, "errors": []}
        with mock.patch.object(module, "tenant_context", _noop_context):
            patcher = mock.patch.object(module.Tenant, "objects")
            objects = patcher.start()
            objects.exclude.return_value.filter.return_value.exists.return_value = True
            objects.exclude.return_value.filter.return_value.order_by.return_value = [
                chosen
            ]
            try:
                with mock.patch.object(
                    module.peer_worker, "run_round", return_value=summary
                ), mock.patch.object(module.Command, "_run_tenant", _run_tenant):
                    call_command("peer_sync", "--once", "--tenant", "tenant-a")
            finally:
                patcher.stop()
        objects.exclude.return_value.filter.assert_called_once_with(
            schema_name="tenant-a"
        )
        self.assertEqual(calls, ["tenant-a"])


class OutboundSyncLogTests(_WorkerFixture):
    """⭐ 案 B2（D-0364）：真投递成功 ⇒ 「已同步给谁」机器行（`peer.outbound_sync`）。

    现场：示例租户本地动作（改价）同步给了谁，订单时间线上**完全没有行**——出站同步此前
    不写 OperationLog。写点在 `_attempt` 落 `done` 处，判据 = **仅 done 且 reason==""**
    （有意作废的 done+reason 与 failed / 重试中间态不写，防 81 分钟退避梯子刷屏）。

    ⚠️ 判据钉**字面值**（E-0024）：summary 整句 / 方向词 / 事件短句全部手写；
    ⛔ 不 import 句子常量拼断言（自指断言在被断言的表改坏时不会红）。
    ⚠️ **变异自证（必须能红）**：把 `peer_worker._record_outbound_sync_log` 的调用点
    去掉 ⇒ 本组第一条红（零行）；把「表外 kind 不写行」的 fail-closed 分支删掉
    （改成照写）⇒ 表外 kind 那条红。
    """

    def _out_event(self, *, kind, target_ref, payload=None, role="downstream",
                   binding_id=None):
        """出站事件构造器（⛔ 不用 `_WorkerFixture._event`：它钉死了 payload 与
        `binding_id` 两参，本组恰好要逐个改它们——撞名不是覆盖）。"""
        return PeerSyncEvent.objects.create(
            binding_role=role,
            binding_id=str(binding_id if binding_id is not None else self.binding.pk),
            direction="out",
            kind=kind,
            target_type="order",
            target_ref=target_ref,
            payload=payload if payload is not None else {"status": OrderStatus.IN_PROGRESS},
            idempotency_key=uuid.uuid4().hex,
        )

    def _done(self, event, *, order=None):
        """投递到 done（push 桩回空信封 = 真成功），回事件。"""
        with mock.patch("apps.business.peer_worker.push_events", return_value={}):
            result = deliver_one(event.pk, now=self.now)
        self.assertEqual(result, "done")
        event.refresh_from_db()
        return event

    def test_a_done_status_event_writes_one_sync_log_row(self):
        """order.update 投递 done ⇒ 一行：名字 / 方向 / 摘要 / 双单号列全对。"""
        order = self._order(no="20261102000000000001")
        order.source_order_no = "SRC-B2-1"
        order.save(update_fields=["source_order_no"])
        event = self._out_event(
            kind="order.update", target_ref=order.order_no,
            payload={"status": OrderStatus.IN_PROGRESS},
        )

        self._done(event)

        row = OperationLog.objects.get(action="peer.outbound_sync")
        self.assertEqual(row.log_type, 40)                    # peer.* 一族 = 系统相关
        self.assertIsNone(row.actor_id)                       # 机器行
        self.assertEqual(row.actor, "系统")
        self.assertEqual(row.target_kind, "order")
        # 对方可读名（`_peer_counterparty_name` 口径）：下家绑定 = 账号显示名（无昵称 ⇒ 账号）。
        self.assertEqual(row.target_name, "peer-worker-account")
        # ⭐ 双列口径（评审必修 5）：时间线两臂（order_no= 本地单号 或
        # source_order_no= 来源号）都必须能查到这一行。
        self.assertEqual(row.order_no, "20261102000000000001")
        self.assertEqual(row.source_order_no, "SRC-B2-1")
        self.assertEqual(row.to_value, "下家")                # role=downstream ⇒ 下家
        self.assertEqual(row.reason, "订单更新：代练中")
        # 模板无 {actor_name} ⇒ 机器行照常带【系统】前缀；整句钉字面值。
        self.assertEqual(
            row.summary,
            "【系统】已同步给下家「peer-worker-account」：订单更新：代练中（订单 SRC-B2-1）",
        )

    def test_an_upstream_event_synced_to_the_upstream_is_labelled_so(self):
        """上行事件（回传上家）⇒ 方向词是「上家」、对方名 = 上家绑定名。"""
        from apps.business.models import PeerUpstreamBinding

        upstream = PeerUpstreamBinding.objects.create(
            peer_name="worker 夹具上家甲",
            peer_base_url="https://worker-up.test.invalid",
            peer_account="worker-up-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://worker-local.test.invalid",
            bound_by=self.account,
        )
        order = self._order(no="20261102000000000002")
        order.source_order_no = "SRC-B2-2"
        order.save(update_fields=["source_order_no"])
        event = PeerSyncEvent.objects.create(
            binding_role="upstream",
            binding_id=str(upstream.pk),
            direction="out",
            kind="order.update",
            target_type="order",
            target_ref=order.order_no,
            payload={"status": "PENDING_REVIEW"},
            idempotency_key=uuid.uuid4().hex,
        )

        self._done(event)

        row = OperationLog.objects.get(action="peer.outbound_sync")
        self.assertEqual(row.to_value, "上家")
        self.assertEqual(row.target_name, "worker 夹具上家甲")
        self.assertEqual(
            row.summary,
            "【系统】已同步给上家「worker 夹具上家甲」：订单更新：待验收（订单 SRC-B2-2）",
        )

    def test_a_voided_or_failed_delivery_writes_no_row(self):
        """作废（done + 人话原因）与 failed ⇒ **零行**（防 81 分钟梯子刷屏）。"""
        order = self._order(no="20261102000000000003", taker=self.account)
        # 作废：撤单复核发现这个账号仍是接手人 ⇒ done + reason（不算真投递）。
        voided = self._out_event(kind="order.withdraw", target_ref=order.order_no)
        with mock.patch("apps.business.peer_worker.push_events") as push:
            self.assertEqual(deliver_one(voided.pk, now=self.now), "stale-void")
        push.assert_not_called()
        # 失败：对端连不上 ⇒ retry（中间态）；烧完梯子 ⇒ failed 终态。都不写。
        failing = self._out_event(kind="order.update", target_ref=order.order_no)
        with mock.patch(
            "apps.business.peer_worker.push_events",
            side_effect=PeerUnreachable("connection refused"),
        ):
            self.assertEqual(deliver_one(failing.pk, now=self.now), "retry")
            for _ in range(4):
                failing.status = PeerSyncEvent.Status.PENDING
                failing.next_attempt_at = None
                failing.save(update_fields=["status", "next_attempt_at"])
                deliver_one(failing.pk, now=self.now)
        failing.refresh_from_db()
        self.assertEqual(failing.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_screenshot_events_are_a_silent_noise_gate(self):
        """`order.screenshot.add` / `.delete` done ⇒ 零行、也**不**警告（已知低信息量）。"""
        order = self._order(no="20261102000000000004")
        for kind in ("order.screenshot.add", "order.screenshot.delete"):
            event = self._out_event(kind=kind, target_ref=order.order_no)
            self._done(event)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_unknown_kind_writes_no_row_and_warns(self):
        """表外 kind（两机版本偏斜）⇒ 零行 + `logger.warning`（fail-closed，不静默）。"""
        order = self._order(no="20261102000000000005")
        event = self._out_event(kind="order.teleport", target_ref=order.order_no)

        with self.assertLogs("apps.business.peer_worker", level="WARNING") as captured:
            self._done(event)

        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertTrue(
            any("order.teleport" in line for line in captured.output), captured.output
        )

    def test_each_binding_gets_its_own_row(self):
        """同一单广播给两条下家绑定 ⇒ **每绑定一行**（名字各是各的，⛔ 不合并）。"""
        order = self._order(no="20261102000000000006")
        other_account = get_user_model().objects.create_user(
            username="peer-worker-second", password="pw-worker"
        )
        second = PeerDownstreamBinding.objects.create(
            account=other_account,
            peer_base_url="https://peer-down-2.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        first_event = self._out_event(
            kind="hall.throw", target_ref=order.order_no,
            binding_id=str(self.binding.pk),
        )
        second_event = self._out_event(
            kind="hall.throw", target_ref=order.order_no,
            binding_id=str(second.pk),
        )
        self._done(first_event)
        self._done(second_event)

        rows = OperationLog.objects.filter(action="peer.outbound_sync").order_by("id")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            [row.target_name for row in rows],
            ["peer-worker-account", "peer-worker-second"],
        )
        self.assertTrue(
            all(row.reason == "大厅抛卡" for row in rows), [r.reason for r in rows]
        )
        self.assertTrue(
            all(row.order_no == "20261102000000000006" for row in rows)
        )
