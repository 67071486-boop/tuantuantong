"""推送管线判据（批次A）：大厅入队、载荷钉值、markdown 降级、可重试退避、被动回复。

⭐ 载荷是**钉值**断言（完整字符串逐字比对，D-0265 的格式契约）——不是「包含某子串」。
"""
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import order_push
from apps.business.models import Game, Order, OrderStatus
from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotGroupPushConfig,
    QQBotOutboundMessage,
)
from apps.qqbot.payloads import build_hall_texts, price_text
from apps.qqbot.push import (
    PASSIVE_REPLY_MAX_PER_MSG_ID,
    queue_hall_push,
    queue_passive_reply,
    queue_test_push,
)
from apps.qqbot.worker import MAX_ATTEMPTS, MARKDOWN_PRIV_CODES, GroupSender


def _game(name="原神"):
    return Game.objects.create(name=name)


def _order(game, *, no="20261019000000000001", hall_price=Decimal("330.00")):
    return Order.objects.create(
        game=game,
        order_no=no,
        status=OrderStatus.WAITING_GRAB,
        project_text="深渊满星",
        hall_price=hall_price,
        total_price=Decimal("400.00"),
    )


def _binding(**kw):
    row = QQBotBinding.get_solo()
    row.app_id = "APP-1"
    row.app_secret = "SECRET-1"
    row.enabled = True
    for key, value in kw.items():
        setattr(row, key, value)
    row.save()
    return row


def _config(group, games, **kw):
    row = QQBotGroupPushConfig.objects.create(group=group, **kw)
    for game in games:
        row.games.add(game)
    return row


# ── 载荷钉值 ───────────────────────────────────────────────────────────────


class HallPayloadPinTests(TenantTestCase):
    NOW = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.get_default_timezone())

    def test_payload_is_pinned_verbatim(self):
        """⭐ 完整字符串逐字比对（D-0265）。改格式先过业务方，这里会红。"""
        order = _order(_game())
        text, markdown = build_hall_texts(order, "可行工作室", now=self.NOW)
        self.assertEqual(
            text,
            "\n".join(
                [
                    "【新单上架】深渊满星",
                    "游戏：原神",
                    "系统：可行工作室",
                    "米 330",
                    "时间：2026-10-19 12:30",
                    "订单号：20261019000000000001",
                ]
            ),
        )
        # markdown 版 = 同内容、首行包 **。
        self.assertEqual(
            markdown,
            "\n".join(
                [
                    "**【新单上架】深渊满星**",
                    "游戏：原神",
                    "系统：可行工作室",
                    "米 330",
                    "时间：2026-10-19 12:30",
                    "订单号：20261019000000000001",
                ]
            ),
        )

    def test_price_normalization_rules(self):
        self.assertEqual(price_text(Decimal("330.00")), "330")
        self.assertEqual(price_text(Decimal("330.50")), "330.5")
        self.assertEqual(price_text(None), "未设置")
        order = _order(_game(), hall_price=Decimal("330.50"))
        text, _markdown = build_hall_texts(order, "可行工作室", now=self.NOW)
        self.assertIn("米 330.5", text)
        # ⛔ 价格行禁止 ¥ / 元 / 千分位。
        for line in text.splitlines():
            if line.startswith("米"):
                self.assertNotIn("¥", line)
                self.assertNotIn("元", line)
                self.assertNotIn(",", line)

    def test_empty_project_falls_back_like_order_push(self):
        order = _order(_game())
        order.project_text = ""
        order.save(update_fields=["project_text"])
        text, _markdown = build_hall_texts(order, "可行工作室", now=self.NOW)
        self.assertIn("【新单上架】—", text)  # 无项目 ⇒ 照 order_push 口径给「—」


# ── 大厅入队 ───────────────────────────────────────────────────────────────


class QueueHallPushTests(TenantTestCase):
    def setUp(self):
        _binding()
        self.game = _game()
        self.group_a = QQBotGroup.objects.create(group_openid="GROUP-A")
        self.group_b = QQBotGroup.objects.create(group_openid="GROUP-B")

    def test_matching_configs_enqueue_pending_rows(self):
        _config(self.group_a, [self.game], msg_type=QQBotGroupPushConfig.MsgType.MARKDOWN)
        _config(self.group_b, [self.game], msg_type=QQBotGroupPushConfig.MsgType.TEXT)
        order = _order(self.game)
        queued = queue_hall_push(order)
        self.assertEqual(queued, 2)
        rows = QQBotOutboundMessage.objects.filter(order=order)
        self.assertEqual(rows.count(), 2)
        for row in rows:
            self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)
            self.assertEqual(row.kind, QQBotOutboundMessage.Kind.HALL_PUSH)
            self.assertTrue(row.payload_text)
            self.assertTrue(row.payload_markdown)

    def test_inactive_or_non_matching_config_enqueues_nothing(self):
        _config(self.group_a, [self.game], is_active=False)
        _config(self.group_b, [], is_active=True)  # 没勾这个游戏
        other_game_order = _order(_game("永劫无间"))
        self.assertEqual(queue_hall_push(other_game_order), 0)
        self.assertEqual(QQBotOutboundMessage.objects.count(), 0)

    def test_same_order_twice_enqueues_again_without_dedup(self):
        """一条大厅事件 = 一批新行（不去重不更新——与 wecom 推送同口径）。"""
        _config(self.group_a, [self.game])
        order = _order(self.game)
        queue_hall_push(order)
        queue_hall_push(order)
        self.assertEqual(
            QQBotOutboundMessage.objects.filter(order=order).count(), 2
        )

    def test_status_change_hook_enqueues_qq_rows_after_wecom(self):
        """`queue_status_change_push` 的 EVENT_HALL 分支接到 QQ 入队（批次A 的接线判据）。"""
        _config(self.group_a, [self.game])
        order = _order(self.game)
        order.status = OrderStatus.WAITING_GRAB
        returned = order_push.queue_status_change_push(
            order, from_status=OrderStatus.UNASSIGNED, hall_publisher=object()
        )
        # wecom 无规则 ⇒ 返回 0；QQ 侧照样建了行。
        self.assertEqual(returned, 0)
        self.assertEqual(
            QQBotOutboundMessage.objects.filter(order=order).count(), 1
        )

    def test_status_change_hook_ignores_non_hall_events(self):
        """重要变化事件**不**进 QQ（本批只接大厅）。"""
        _config(self.group_a, [self.game])
        order = _order(self.game, no="20261019000000000002")
        order.status = OrderStatus.IN_PROGRESS
        order_push.queue_status_change_push(
            order, from_status=OrderStatus.WAITING_GRAB, hall_publisher=None
        )
        self.assertEqual(QQBotOutboundMessage.objects.count(), 0)

    def test_status_change_hook_swallows_qq_enqueue_failure(self):
        """QQ 入队炸了不许拖垮订单状态（与 wecom 同一口径：通知不是凭据）。"""
        _config(self.group_a, [self.game])
        order = _order(self.game)
        order.status = OrderStatus.WAITING_GRAB
        with mock.patch(
            "apps.qqbot.push.queue_hall_push", side_effect=RuntimeError("boom")
        ):
            returned = order_push.queue_status_change_push(
                order, from_status=OrderStatus.UNASSIGNED, hall_publisher=object()
            )
        self.assertEqual(returned, 0)  # 没抛 ⇒ wecom 路径返回值原样


class QueueTestAndPassiveTests(TenantTestCase):
    def setUp(self):
        _binding()
        self.group = QQBotGroup.objects.create(group_openid="GROUP-1")

    def test_test_push_uses_placeholder_texts(self):
        row = queue_test_push(self.group)
        self.assertEqual(row.kind, QQBotOutboundMessage.Kind.TEST_PUSH)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)
        self.assertIn("米 未设置", row.payload_text)
        self.assertIn("订单号：未设置", row.payload_text)
        self.assertIn("【", row.payload_text.splitlines()[0])

    def test_passive_reply_caps_at_five_per_msg_id(self):
        for seq in range(1, PASSIVE_REPLY_MAX_PER_MSG_ID + 1):
            self.assertTrue(
                queue_passive_reply(self.group, f"回复{seq}", "MSG-IN-1")
            )
        rows = QQBotOutboundMessage.objects.filter(passive_msg_id="MSG-IN-1").order_by(
            "passive_msg_seq"
        )
        self.assertEqual(
            list(rows.values_list("passive_msg_seq", flat=True)),
            [1, 2, 3, 4, 5],
        )
        # 第 6 条：不建行、回 False。
        self.assertFalse(queue_passive_reply(self.group, "回复6", "MSG-IN-1"))
        self.assertEqual(
            QQBotOutboundMessage.objects.filter(passive_msg_id="MSG-IN-1").count(),
            PASSIVE_REPLY_MAX_PER_MSG_ID,
        )

    def test_passive_reply_refuses_stale_msg_id_window(self):
        """`msg_id` 5 分钟窗口：同 id 最早一条回复超过 5 分钟 ⇒ 不再建行。"""
        now = timezone.now()
        stale = now - timedelta(seconds=301)
        old = QQBotOutboundMessage.objects.create(
            group=self.group,
            kind=QQBotOutboundMessage.Kind.PASSIVE_REPLY,
            msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
            payload_text="旧回复",
            passive_msg_id="MSG-OLD",
            passive_msg_seq=1,
            scheduled_at=stale,
            created_at=stale,
        )
        old.created_at = stale  # auto_now_add 不吃 update，显式写一次
        old.save(update_fields=["created_at"])
        self.assertFalse(queue_passive_reply(self.group, "新回复", "MSG-OLD", now=now))

    def test_passive_reply_rows_are_text_typed(self):
        self.assertTrue(queue_passive_reply(self.group, "回答", "MSG-IN-2"))
        row = QQBotOutboundMessage.objects.get(passive_msg_id="MSG-IN-2")
        self.assertEqual(row.msg_type_eff, QQBotOutboundMessage.MsgType.TEXT)
        self.assertEqual(row.kind, QQBotOutboundMessage.Kind.PASSIVE_REPLY)


# ── sender：降级 / 退避 / 终态 ──────────────────────────────────────────────


class SenderMarkdownDowngradeTests(TenantTestCase):
    def setUp(self):
        self.binding = _binding(markdown_ok=True)
        self.group = QQBotGroup.objects.create(group_openid="GROUP-1")
        self.row = QQBotOutboundMessage.objects.create(
            group=self.group,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            msg_type_eff=QQBotOutboundMessage.MsgType.MARKDOWN,
            payload_markdown="**【新单上架】x**",
            payload_text="【新单上架】x",
            scheduled_at=timezone.now(),
        )
        self.sender = GroupSender(self.binding)

    def test_markdown_priv_error_downgrades_and_resends_once_as_text(self):
        """⭐ 304036 与 40034127 **两码都有用例跑过**（评审 P3-1，`subTest` 参数化）：
        ⇒ `markdown_ok=False` + 同内容 msg_type=0 **重发一次** ⇒ sent。"""
        for code in sorted(MARKDOWN_PRIV_CODES):
            with self.subTest(code=code):
                # 每一轮从「能发 markdown」的原始状态出发（⛔ 多变异不许共享中间态）。
                self.binding.markdown_ok = True
                self.binding.save(update_fields=["markdown_ok"])
                self.row.msg_type_eff = QQBotOutboundMessage.MsgType.MARKDOWN
                self.row.status = QQBotOutboundMessage.Status.PENDING
                self.row.attempts = 0
                self.row.save(
                    update_fields=["msg_type_eff", "status", "attempts"]
                )
                responses = [
                    (False, None, code, "无 Markdown 权限"),
                    (True, {"id": "QQ-1", "timestamp": "t"}, "0", ""),
                ]

                def fake_send(tm, group_openid, **kw):
                    return responses.pop(0)

                with mock.patch(
                    "apps.qqbot.qqbot_api.send_group_message", side_effect=fake_send
                ) as send:
                    resolved = self.sender.drain()
                self.assertEqual(resolved, 1)
                self.assertEqual(send.call_count, 2)
                # 第一次发 markdown、第二次发文本（同内容）。
                self.assertEqual(send.call_args_list[0].kwargs["msg_type"], 2)
                self.assertEqual(send.call_args_list[1].kwargs["msg_type"], 0)
                self.assertEqual(send.call_args_list[1].kwargs["content"], "【新单上架】x")
                self.binding.refresh_from_db()
                self.assertFalse(self.binding.markdown_ok)
                self.row.refresh_from_db()
                self.assertEqual(self.row.status, QQBotOutboundMessage.Status.SENT)
                self.assertEqual(self.row.msg_type_eff, QQBotOutboundMessage.MsgType.TEXT)
                self.assertEqual(self.row.qq_message_id, "QQ-1")

    def test_after_downgrade_next_rows_go_out_as_text_directly(self):
        self.binding.markdown_ok = False
        self.binding.save(update_fields=["markdown_ok"])
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(True, {"id": "QQ-2"}, "0", ""),
        ) as send:
            self.sender.drain()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["msg_type"], 0)


class SenderRetryTests(TenantTestCase):
    def setUp(self):
        self.binding = _binding(markdown_ok=False)
        self.group = QQBotGroup.objects.create(group_openid="GROUP-1")

    def _row(self, attempts=0):
        return QQBotOutboundMessage.objects.create(
            group=self.group,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
            payload_text="hi",
            attempts=attempts,
            scheduled_at=timezone.now(),
        )

    def test_retryable_error_increments_attempts_and_pushes_out(self):
        row = self._row()
        sender = GroupSender(self.binding)
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(False, None, "40034100", "超频"),
        ):
            sender.drain()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)
        self.assertGreater(
            row.scheduled_at, timezone.now() + timedelta(seconds=20), "退避重排"
        )

    def test_retryable_error_beyond_max_attempts_marks_failed(self):
        row = self._row(attempts=MAX_ATTEMPTS - 1)
        sender = GroupSender(self.binding)
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(False, None, "50055001", "稍后再试"),
        ):
            sender.drain()
        row.refresh_from_db()
        self.assertEqual(row.attempts, MAX_ATTEMPTS)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.FAILED)
        self.assertEqual(row.error_code, "50055001")

    def test_network_error_counts_as_retryable(self):
        row = self._row()
        sender = GroupSender(self.binding)
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(False, None, "NETWORK", "ConnectError"),
        ):
            sender.drain()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)

    def test_permanent_error_fails_immediately_without_retry(self):
        """禁言（40054002）不在可重试清单 ⇒ 直接 failed，不烧重试次数。"""
        row = self._row()
        sender = GroupSender(self.binding)
        with mock.patch(
            "apps.qqbot.qqbot_api.send_group_message",
            return_value=(False, None, "40054002", "机器人被禁言"),
        ):
            sender.drain()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.FAILED)
        self.assertEqual(row.error_code, "40054002")
