from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, PerformanceAdjustment
from apps.business.order_status import apply_transition
from tests.p5_auth_helpers import make_logged_in_client


class WxPusherBusinessEventsTests(TenantTestCase):
    def setUp(self):
        self.settings_override = override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="904", WXPUSHER_APP_TOKEN="AT_synthetic",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.game = Game.objects.create(name="通知测试游戏")
        self.operator = make_logged_in_client(
            username="notification-operator", groups=("客服主管",), nodes=(
                "order.assign_taker", "order.submit_payout_price", "order.view_detail",
                "order.clear_taker", "order.change_status", "order.change_status_completed",
                "finance.change_settlement_status",
            ),
        )
        self.first, self.first_client = self._worker("first")
        self.second, self.second_client = self._worker("second")
        self._worker("uninvolved")

    def _worker(self, suffix):
        username = f"notification-worker-{suffix}"
        client = make_logged_in_client(
            username=username, groups=("代练员",), nodes=("order.view_detail", "order.submit_work"),
        )
        user = User.objects.get(username=username)
        WxPusherBinding.objects.create(user=user, app_id="904", uid=f"UID_synthetic_{suffix}")
        return user, client

    def _order(self, *, status=OrderStatus.UNASSIGNED, taker=None, suffix="001"):
        order = Order.objects.create(
            order_no=f"20261116000000000{suffix}", game=self.game, status=status,
            hall_price=Decimal("80.00"),
        )
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker,
            temp_amount=Decimal("80.00"), bonus_amount=Decimal("80.00"),
        )
        return order, perf

    def _post_order(self, order, action, payload, *, client=None):
        result = (client or self.operator).post(
            f"/api/business/orders/{order.order_no}/{action}/", payload, content_type="application/json",
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["code"], 0, result.content)

    def _notices(self):
        return list(WxPusherNotice.objects.order_by("pk").values_list(
            "kind", "user_id", "order_no",
        ))

    def test_assign_reassign_same_taker_edit_and_clear_notify_only_affected_workers(self):
        order, _ = self._order()
        self._post_order(order, "assign", {"user_id": self.first.pk})
        self._post_order(order, "assign", {"user_id": self.first.pk, "hours": "3"})
        self._post_order(order, "assign", {"user_id": self.second.pk})
        self._post_order(order, "clear-taker", {"isDelEmp": "N"})
        self.assertEqual(self._notices(), [
            ("assigned", self.first.pk, order.order_no),
            ("unassigned", self.first.pk, order.order_no),
            ("assigned", self.second.pk, order.order_no),
            ("unassigned", self.second.pk, order.order_no),
        ])

    def test_a_real_assignment_creates_a_business_notice_without_phone_configuration(self):
        order, _ = self._order()
        with override_settings(WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=""):
            self._post_order(order, "assign", {"user_id": self.first.pk})

        notice = WxPusherNotice.objects.get(user=self.first, order_no=order.order_no, kind="assigned")
        self.assertEqual((notice.status, notice.skip_reason), ("skipped", "app_unconfigured"))
        self.assertIsNone(notice.binding_id)

    def test_assignment_preserves_the_first_workers_details_after_reassignment(self):
        order, _ = self._order()
        order.source_order_no = "SOURCE-SYNTHETIC-86"
        order.project_text = "排位 黄金二 → 铂金一"
        order.content = "合成机密备注"
        order.game_account = "合成机密账户"
        order.save(update_fields=["source_order_no", "project_text", "content", "game_account"])
        self._post_order(order, "assign", {"user_id": self.first.pk, "price": "88.00"})
        self._post_order(order, "assign", {"user_id": self.second.pk, "price": "99.00"})

        notice = WxPusherNotice.objects.get(user=self.first, kind="assigned")
        self.assertEqual(notice.details, {
            "display_order_no": "SOURCE-SYNTHETIC-86",
            "project": "排位 黄金二 → 铂金一",
            "amount": "88.00",
        })
        self.assertEqual(WxPusherNotice.objects.get(user=self.second, kind="assigned").details["amount"], "99.00")

    def test_completed_notice_uses_project_and_price_persisted_before_the_status_change(self):
        stale, _ = self._order(status=OrderStatus.PENDING_REVIEW, taker=self.first)
        updated = Order.objects.get(pk=stale.pk)
        updated.source_order_no = "SOURCE-SNAPSHOT-87"
        updated.project_text = "排位 铂金一 → 钻石一"
        updated.hall_price = Decimal("108.00")
        updated.save(update_fields=["source_order_no", "project_text", "hall_price"])

        with transaction.atomic():
            apply_transition(stale, OrderStatus.COMPLETED, nodes={"order.change_status_completed"})

        self.assertEqual(WxPusherNotice.objects.get(kind="completed").details, {
            "display_order_no": "SOURCE-SNAPSHOT-87",
            "project": "排位 铂金一 → 钻石一",
            "amount": "108.00",
        })

    def test_pause_resume_issue_and_refund_only_notify_the_current_worker(self):
        order, _ = self._order(status=OrderStatus.IN_PROGRESS, taker=self.first)
        self._post_order(order, "report-issue", {"reason": "本人已报问题"}, client=self.first_client)
        self.assertEqual(self._notices(), [])
        self._post_order(order, "transition", {"target": "IN_PROGRESS"})
        self._post_order(order, "transition", {"target": "ISSUE", "reason": "客服已报问题"})
        self._post_order(order, "transition", {"target": "PAUSED", "reason": "请等客户退出游戏"})
        self._post_order(order, "transition", {"target": "IN_PROGRESS"})
        self._post_order(order, "transition", {"target": "REFUNDED", "reason": "客户取消代练需求"})
        self.assertEqual(self._notices(), [
            ("issue", self.first.pk, order.order_no),
            ("paused", self.first.pk, order.order_no),
            ("resumed", self.first.pk, order.order_no),
            ("refunded", self.first.pk, order.order_no),
        ])
        self.assertEqual(
            {notice.kind: notice.details.get("reason") for notice in WxPusherNotice.objects.all()},
            {"issue": "客服已报问题", "paused": "请等客户退出游戏", "resumed": None, "refunded": "客户取消代练需求"},
        )
        self.assertNotIn("project", WxPusherNotice.objects.get(kind="paused").details)

    def test_replacing_a_paused_worker_does_not_tell_the_outgoing_worker_to_resume(self):
        order, _ = self._order(status=OrderStatus.PAUSED, taker=self.first)
        self._post_order(order, "assign", {"user_id": self.second.pk})
        self.assertEqual(self._notices(), [
            ("unassigned", self.first.pk, order.order_no),
            ("assigned", self.second.pk, order.order_no),
        ])

    def test_review_rejected_then_worker_resubmits_and_completed_once(self):
        order, perf = self._order(status=OrderStatus.IN_PROGRESS, taker=self.first)
        review_url = f"/api/business/performances/{perf.pk}/review/"
        self._post_order(order, "submit", {}, client=self.first_client)
        self.assertEqual(self._notices(), [])
        result = self.operator.post(
            review_url, {"approved": False, "comment": "需补交段位截图"}, content_type="application/json",
        )
        self.assertEqual(result.json()["code"], 0, result.content)
        self._post_order(order, "submit", {}, client=self.first_client)
        Performance.objects.filter(pk=perf.pk).update(taker_price=Decimal("0.00"))
        self._post_order(order, "transition", {"target": "COMPLETED"})
        self.assertEqual(self._notices(), [
            ("review_rejected", self.first.pk, order.order_no),
            ("completed", self.first.pk, order.order_no),
        ])
        self.assertEqual(WxPusherNotice.objects.get(kind="review_rejected").details["reason"], "需补交段位截图")
        self.assertEqual(WxPusherNotice.objects.get(kind="completed").details["amount"], "0.00")

    def test_manual_settlement_notifies_each_worker_once_per_batch(self):
        _, perf1 = self._order(status=OrderStatus.COMPLETED, taker=self.first)
        _, perf2 = self._order(status=OrderStatus.COMPLETED, taker=self.first, suffix="002")
        _, perf3 = self._order(status=OrderStatus.COMPLETED, taker=self.second, suffix="003")
        PerformanceAdjustment.objects.create(
            performance=perf1, kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-7.50"), reason="合成扣款",
        )
        payload = {"performance_ids": [perf1.pk, perf2.pk, perf3.pk]}
        for _ in range(2):
            result = self.operator.post("/api/business/settlements/settle", payload, content_type="application/json")
            self.assertEqual(result.json()["code"], 0, result.content)
        self.assertCountEqual(self._notices(), [
            ("settled", self.first.pk, ""),
            ("settled", self.second.pk, ""),
        ])
        perf1.refresh_from_db()
        settlement_time = perf1.settled_at.isoformat()
        self.assertEqual(WxPusherNotice.objects.get(user=self.first).details, {
            "count": 2, "amount": "152.50", "occurred_at": settlement_time,
            "related_order_nos": [perf1.order.order_no, perf2.order.order_no],
        })
        self.assertEqual(WxPusherNotice.objects.get(user=self.second).details, {
            "count": 1, "amount": "80.00", "occurred_at": settlement_time,
            "related_order_nos": [perf3.order.order_no],
        })

    def test_worker_notices_roll_back_if_reassignment_fails_late(self):
        order, perf = self._order(status=OrderStatus.IN_PROGRESS, taker=self.first)
        with patch("apps.business.order_writes.jidanpu_member.queue_assign", side_effect=RuntimeError("模拟后续写入失败")):
            with self.assertRaisesMessage(RuntimeError, "模拟后续写入失败"):
                self._post_order(order, "assign", {"user_id": self.second.pk})
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.first.pk)
        self.assertEqual(self._notices(), [])
