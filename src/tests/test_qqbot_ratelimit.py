"""限频判据（批次A）：60/30 按 `auth_verified` 切档、群窗口、日限 1000、跨日重置。

日限/跨日两条走的是 **sender 的 DB 计数判定**（`GroupSender._daily_limit_reached`）——
「DailyCounter 交给调用方按 DB 计数」的落地（`ratelimit.py` 只放常量与纯窗口）。
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.qqbot.models import QQBotBinding, QQBotGroup, QQBotOutboundMessage
from apps.qqbot.ratelimit import (
    BOT_QPM_UNVERIFIED,
    BOT_QPM_VERIFIED,
    GROUP_DAILY_LIMIT,
    GROUP_QPM,
    SlidingWindow,
)
from apps.qqbot.worker import GroupSender, bot_qpm


class SlidingWindowTests(TestCase):
    def _window(self, limit, clock):
        return SlidingWindow(limit, clock=lambda: clock[0])

    def test_acquire_within_limit_returns_zero(self):
        clock = [0.0]
        window = self._window(3, clock)
        self.assertEqual(window.acquire(), 0.0)
        self.assertEqual(window.acquire(), 0.0)
        self.assertEqual(window.acquire(), 0.0)

    def test_full_window_reports_wait_seconds(self):
        clock = [0.0]
        window = self._window(2, clock)
        window.acquire()
        window.acquire()
        self.assertEqual(window.wait_time(), 60.0)  # 最早一击 + 60s − 现在
        clock[0] = 10.0
        self.assertEqual(window.wait_time(), 50.0)
        # ⭐ wait_time 不消耗名额：连问多次答案一致。
        self.assertEqual(window.wait_time(), 50.0)

    def test_window_slides_open_again(self):
        clock = [0.0]
        window = self._window(2, clock)
        window.acquire()
        window.acquire()
        clock[0] = 60.0
        self.assertEqual(window.wait_time(), 0.0)
        self.assertEqual(window.acquire(), 0.0)

    def test_qpm_constants_match_the_registered_document_values(self):
        """文档值钉死（出处见 ratelimit 模块 docstring；发版前真机校准）。"""
        self.assertEqual(BOT_QPM_VERIFIED, 60)
        self.assertEqual(BOT_QPM_UNVERIFIED, 30)
        self.assertEqual(GROUP_QPM, 20)
        self.assertEqual(GROUP_DAILY_LIMIT, 1000)


class BotQpmTests(TenantTestCase):
    def test_verified_binding_gets_60_and_unverified_30(self):
        verified = QQBotBinding(app_id="A", app_secret="S", auth_verified=True)
        unverified = QQBotBinding(app_id="A", app_secret="S", auth_verified=False)
        self.assertEqual(bot_qpm(verified), 60)
        self.assertEqual(bot_qpm(unverified), 30)


def _group(openid="GROUP-1"):
    return QQBotGroup.objects.create(group_openid=openid)


class DailyLimitTests(TenantTestCase):
    def setUp(self):
        self.binding = QQBotBinding.get_solo()
        self.binding.app_id = "APP-1"
        self.binding.app_secret = "SECRET-1"
        self.binding.enabled = True
        self.binding.save()
        self.group = _group()
        self.sender = GroupSender(self.binding)

    def _pending_row(self, **kw):
        now = timezone.now()
        defaults = dict(
            group=self.group,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
            payload_text="hi",
            scheduled_at=now,
        )
        defaults.update(kw)
        return QQBotOutboundMessage.objects.create(**defaults)

    def _bulk_sent_today(self, count, *, sent_at=None):
        sent_at = sent_at or timezone.now()
        rows = [
            QQBotOutboundMessage(
                group=self.group,
                kind=QQBotOutboundMessage.Kind.HALL_PUSH,
                msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
                payload_text="已发",
                status=QQBotOutboundMessage.Status.SENT,
                sent_at=sent_at,
                scheduled_at=sent_at,
            )
            for _ in range(count)
        ]
        QQBotOutboundMessage.objects.bulk_create(rows)

    def test_daily_limit_reached_marks_the_row_failed(self):
        self._bulk_sent_today(GROUP_DAILY_LIMIT)
        row = self._pending_row()
        resolved = self.sender.drain()
        self.assertEqual(resolved, 1)
        row.refresh_from_db()
        self.assertEqual(row.status, QQBotOutboundMessage.Status.FAILED)
        self.assertEqual(row.error_code, "DAILY_LIMIT")

    def test_yesterdays_sends_do_not_count_against_today(self):
        """跨日重置：`sent_at` 是昨天的行不计今天 ⇒ 新行照发。"""
        self._bulk_sent_today(GROUP_DAILY_LIMIT, sent_at=timezone.now() - timedelta(days=1))
        row = self._pending_row()
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(True, {"id": "MSG-1"}, "0", ""),
        ) as send:
            self.sender.drain()
        send.assert_called_once()
        row.refresh_from_db()
        self.assertEqual(row.status, QQBotOutboundMessage.Status.SENT)

    def test_limit_is_per_group(self):
        """日限是**单群**口径：A 群打满不影响 B 群。"""
        self._bulk_sent_today(GROUP_DAILY_LIMIT)
        other = _group("GROUP-2")
        row = QQBotOutboundMessage.objects.create(
            group=other,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
            payload_text="hi",
            scheduled_at=timezone.now(),
        )
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(True, {"id": "MSG-2"}, "0", ""),
        ):
            self.sender.drain()
        row.refresh_from_db()
        self.assertEqual(row.status, QQBotOutboundMessage.Status.SENT)


class GroupWindowDeferralTests(TenantTestCase):
    def test_full_group_window_defers_the_row_without_sending(self):
        """②群窗口满：该行 `scheduled_at` 顺延到窗口空位、**不消耗名额也不发送**。"""
        binding = QQBotBinding.get_solo()
        group = _group()
        sender = GroupSender(binding)
        # 注入一个已满的群窗口（stub 报告还要等 42 秒）。
        class _FullWindow:
            def wait_time(self):
                return 42.0

            def acquire(self):
                raise AssertionError("窗口已满，不允许再占名额")

        sender._group_windows[group.id] = _FullWindow()
        row = QQBotOutboundMessage.objects.create(
            group=group,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
            payload_text="hi",
            scheduled_at=timezone.now(),
        )
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message"
        ) as send:
            resolved = sender.drain()
        send.assert_not_called()
        self.assertEqual(resolved, 0)  # 顺延不算「了结」
        row.refresh_from_db()
        self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)
        self.assertGreater(
            row.scheduled_at,
            timezone.now() + timedelta(seconds=30),
            "顺延目标应落在窗口空位（≈ now + 42s）",
        )
