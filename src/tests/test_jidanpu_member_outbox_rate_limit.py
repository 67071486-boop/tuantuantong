"""下家①式出向的**降频 / 每轮封顶 / 468 冷却**（2026-10-27，业务方拍板）。

背景：suqing 的下家绑定 #1 被对端 WAF 拦成 HTTP 468（雷池「请求频率过高」），换了一个
代理 IP 仍被限流 ⇒ 光换 IP 不够，要**降频**；业务方口径「**先换代理、都不行最后才停**」。

六条判据（本文件逐条钉住）：

1. 两条出向请求之间真的隔了 `JIDANPU_MIN_REQUEST_INTERVAL_MS`（注入时钟断间隔，⛔ 不真睡）；
2. 一轮最多推 `JIDANPU_OUTBOX_MAX_PER_ROUND` 条（第 K+1 条留到下一轮）；
3. 468 ⇒ **先换下一个出口**（出口真的变了），⛔ 不直接进冷却；
4. 池里出口都试完仍 468 ⇒ 该店进入**冷却**（断言截止时刻 + 冷却期零请求）；
   再被拦 ⇒ **翻倍**（5 分钟 → 10 分钟，封顶 30 分钟）；
5. 冷却到期后恢复（再试一次）；
6. ⛔ 不破 B1（空闲轮不清共享计数）/ B2（状态文件无凭据、0600）——由
   `test_jidanpu_sync_outbox_switch.IdleTickKeepsSharedEgressCounterTests` 与
   `test_jidanpu_proxy.SharedEgressStateTests` 继续守，本文件只额外断「冷却也写进共享文件」。

⚠️ 桩打在**被查找的那个模块**上（E-0029）：本文件替的是 `jidanpu_member._consume`
（引擎级，不碰真实上游）；夹具一律合成值（E-0077）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import UpstreamBlockedError

from apps.business import jidanpu_member, jidanpu_proxy
from apps.business.models import JidanpuMemberBinding, JidanpuSyncEvent

BASE_URL = "https://member-rate-limit.test.invalid"
NOW = datetime(2026, 10, 27, 9, 0, 0, tzinfo=dt_timezone.utc)


class _FakeClock:
    """可注入的单调时钟：`sleep` 记账并推进时间（⛔ 真睡 500ms）。"""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _RateLimitBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username="kefu-rate-limit", password="pw-canary-x"
        )
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-rate-limit",
            password="pw-rate-limit-canary",
            enabled=True,
        )

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _event(self, ref: str) -> JidanpuSyncEvent:
        return JidanpuSyncEvent.objects.create(
            direction="out",
            kind=jidanpu_member.KIND_LAUNCH,
            target_type="order",
            target_ref=ref,
            status="pending",
            payload={"member_binding_id": self.binding.pk},
        )

    @contextmanager
    def _env(self, **extra):
        env = {
            "JIDANPU_PROXIES": (
                "http://egress-a.test.invalid:1,http://egress-b.test.invalid:2"
            ),
            "JIDANPU_PROXY": "",
            "JIDANPU_EGRESS_FAIL_THRESHOLD": "",
            "JIDANPU_EGRESS_QUARANTINE_SECONDS": "",
            "JIDANPU_EGRESS_STATE_FILE": "",
            "JIDANPU_MIN_REQUEST_INTERVAL_MS": "500",
            "JIDANPU_OUTBOX_MAX_PER_ROUND": "20",
            "JIDANPU_OUTBOX_COOLDOWN_SECONDS": "300",
        }
        env.update(extra)
        with mock.patch.dict(os.environ, env):
            jidanpu_proxy.reset_counter_for_tests()
            jidanpu_member.reset_outbound_rate_limit_for_tests()
            yield

    @contextmanager
    def _spawn(self):
        """替掉①式的建连接缝（每次 `reconnect()` 都真的再开一份）。"""
        opened: list[object] = []

        @contextmanager
        def fake_spawn(_binding):
            opened.append(object())
            yield mock.Mock(), None

        line = replace(jidanpu_member.member_outbox_line(), spawn_client=fake_spawn)
        yield line, opened

    # ── ① 最小间隔 ──────────────────────────────────────────────────────────

    def test_two_outbound_attempts_are_spaced_by_the_configured_interval(self):
        """两条出向请求之间至少隔 N 毫秒（注入时钟：断言 sleep 的**值**，⛔ 真睡）。"""
        self._event("rate-1")
        self._event("rate-2")
        clock = _FakeClock()
        calls: list[str] = []

        def fake_consume(event, **_kwargs):
            calls.append(event.target_ref)
            return "done"

        with self._env(JIDANPU_MIN_REQUEST_INTERVAL_MS="500"), \
             mock.patch.object(
                 jidanpu_member, "_outbound_monotonic", clock.monotonic,
             ), mock.patch.object(
                 jidanpu_member, "_outbound_sleep", clock.sleep,
             ), mock.patch.object(
                 jidanpu_member, "_consume", side_effect=fake_consume,
             ):
            stats = jidanpu_member.run_outbox(
                self.binding, now=NOW, client=mock.Mock(),
            )
        self.assertEqual(stats.done, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(clock.sleeps), 1, clock.sleeps)
        self.assertAlmostEqual(clock.sleeps[0], 0.5, places=3)

    # ── ② 每轮封顶 ──────────────────────────────────────────────────────────

    def test_a_round_pushes_at_most_k_and_the_rest_wait_for_the_next_round(self):
        """一轮最多推 K 条（默认 20；这里设 2）⇒ 第 3 条留到下一轮、⛔ 不丢。"""
        events = [self._event(f"cap-{i}") for i in range(3)]
        seen: list[str] = []

        def fake_consume(event, **_kwargs):
            seen.append(event.target_ref)
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(status="done")
            return "done"

        with self._env(
            JIDANPU_MIN_REQUEST_INTERVAL_MS="0", JIDANPU_OUTBOX_MAX_PER_ROUND="2",
        ), mock.patch.object(jidanpu_member, "_consume", side_effect=fake_consume):
            first = jidanpu_member.run_outbox(
                self.binding, now=NOW, client=mock.Mock(),
            )
            self.assertEqual(first.done, 2)
            self.assertEqual(first.capped, 1, "第 3 条必须记成「留到下轮」")
            self.assertEqual(
                JidanpuSyncEvent.objects.filter(status="pending").count(), 1
            )
            second = jidanpu_member.run_outbox(
                self.binding, now=NOW, client=mock.Mock(),
            )
        self.assertEqual(second.done, 1)
        self.assertEqual(seen, [events[0].target_ref, events[1].target_ref,
                                events[2].target_ref])

    # ── ③ 468 ⇒ 先换出口（不直接冷却）────────────────────────────────────────

    def test_a_468_switches_to_the_next_outlet_and_retries(self):
        """一次 468 ⇒ 出口真的换到下一位、这一条**重试**（⛔ 不等 3 次、⛔ 不冷却）。"""
        self._event("switch-1")
        attempts: list[int] = []

        def fake_consume(_event, **_kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise UpstreamBlockedError("468 请求频率过高")
            return "done"

        with self._env(), self._spawn() as (line, opened), \
             mock.patch.object(jidanpu_member, "_consume", side_effect=fake_consume):
            stats = jidanpu_member.run_outbox(self.binding, now=NOW, line=line)
            label = jidanpu_proxy.current_label()
        self.assertEqual(len(attempts), 2, "第 1 次 468 之后必须重试")
        self.assertEqual(len(opened), 2, "重试必须按新出口重建客户端")
        self.assertEqual(
            label, "1:egress-b.test.invalid",
            "468 之后出口没有真的换到下一位",
        )
        self.assertEqual(stats.done, 1)
        self.assertIsNone(stats.cooldown_until, "换出口能解决时⛔ 不许进冷却")

    # ── ④ 池里全试完仍 468 ⇒ 冷却 ────────────────────────────────────────────

    def test_when_every_outlet_is_blocked_the_shop_cools_down(self):
        """池里出口都试过仍 468 ⇒ 冷却截止 = now + 基数，且冷却期**零请求**。"""
        self._event("cooldown-1")
        attempts: list[int] = []

        def fake_consume(_event, **_kwargs):
            attempts.append(1)
            raise UpstreamBlockedError("468 请求频率过高")

        with self._env(), self._spawn() as (line, opened), \
             mock.patch.object(jidanpu_member, "_consume", side_effect=fake_consume):
            first = jidanpu_member.run_outbox(self.binding, now=NOW, line=line)
            self.assertEqual(len(attempts), 2, "两个出口各试一次")
            self.assertEqual(len(opened), 2)
            self.assertIsNotNone(first.cooldown_until)
            self.assertEqual(
                first.cooldown_until, NOW + timedelta(seconds=300),
                "冷却基数是 5 分钟",
            )
            # 冷却期：同一家**零请求**（连建连都不许），其它店不受影响（键含 binding.pk）。
            before = (len(attempts), len(opened))
            second = jidanpu_member.run_outbox(
                self.binding, now=NOW + timedelta(seconds=10), line=line,
            )
        self.assertEqual((len(attempts), len(opened)), before, "冷却期又打了上游")
        self.assertIsNotNone(second.cooldown_until)
        self.assertIn("冷却", second.blocked_reason)

    def test_a_second_cooldown_after_recovery_is_doubled(self):
        """冷却到期后又被拦 ⇒ 第二次冷却**翻倍**（5 分钟 → 10 分钟）。"""
        self._event("double-1")

        def always_blocked(_event, **_kwargs):
            raise UpstreamBlockedError("468 请求频率过高")

        with self._env(), self._spawn() as (line, _opened), \
             mock.patch.object(
                 jidanpu_member, "_consume", side_effect=always_blocked,
             ):
            first = jidanpu_member.run_outbox(self.binding, now=NOW, line=line)
            later = NOW + timedelta(seconds=301)
            second = jidanpu_member.run_outbox(self.binding, now=later, line=line)
        self.assertEqual(first.cooldown_until, NOW + timedelta(seconds=300))
        self.assertEqual(second.cooldown_until, later + timedelta(seconds=600))

    # ── ⑤ 冷却到期恢复 ──────────────────────────────────────────────────────

    def test_after_the_cooldown_expires_the_shop_is_tried_again(self):
        """冷却到期 ⇒ 再试一次（成功则清冷却、继续推）。"""
        self._event("resume-1")
        calls: list[int] = []
        blocked = {"now": True}

        def flaky_consume(_event, **_kwargs):
            calls.append(1)
            if blocked["now"]:
                raise UpstreamBlockedError("468 请求频率过高")
            return "done"

        with self._env(), self._spawn() as (line, _opened), \
             mock.patch.object(
                 jidanpu_member, "_consume", side_effect=flaky_consume,
             ):
            # ① 先真的被拦进冷却（池里两个出口都试过）。
            first = jidanpu_member.run_outbox(self.binding, now=NOW, line=line)
            self.assertEqual(first.cooldown_until, NOW + timedelta(seconds=300))
            # ② 冷却到期、对端恢复 ⇒ 再试一次、成功、冷却清掉。
            blocked["now"] = False
            before = len(calls)
            stats = jidanpu_member.run_outbox(
                self.binding, now=NOW + timedelta(seconds=301), line=line,
            )
        self.assertEqual(len(calls), before + 1, "冷却到期后没有再试一次")
        self.assertEqual(stats.done, 1)
        self.assertIsNone(stats.cooldown_until)

    # ── ④ 冷却写进共享状态文件（B2 的形状不变）────────────────────────────────

    def test_the_cooldown_is_written_into_the_shared_state_file(self):
        """设了共享状态文件 ⇒ 冷却截止写进**同一份**文件（运维可见；⛔ 无凭据）。"""
        self._event("shared-cooldown")
        directory = Path(tempfile.mkdtemp(prefix="egress-cooldown-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        state = directory / "egress-state.json"

        def always_blocked(_event, **_kwargs):
            raise UpstreamBlockedError("468 请求频率过高")

        with self._env(JIDANPU_EGRESS_STATE_FILE=str(state)), \
             self._spawn() as (line, _opened), mock.patch.object(
                 jidanpu_member, "_consume", side_effect=always_blocked,
             ):
            stats = jidanpu_member.run_outbox(self.binding, now=NOW, line=line)
        payload = json.loads(state.read_text(encoding="utf-8"))
        key = jidanpu_member._cooldown_key(self.binding)
        self.assertIn(key, payload["cooldowns"])
        self.assertAlmostEqual(payload["cooldowns"][key], stats.cooldown_until.timestamp())
        text = state.read_text(encoding="utf-8")
        self.assertNotIn("pw-rate-limit-canary", text, "状态文件里出现了绑定密码")
