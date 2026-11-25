from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from rest_framework.exceptions import PermissionDenied

from apps.accounts.choices import AccountSource, CommissionMode, MemberType
from apps.accounts.models import StaffProfile, WxPusherBinding, WxPusherNotice
from apps.audit.models import OperationLog
from apps.business.approvals import create_refund_approval, decide_approval, has_refund_approval
from apps.business.models import (
    Approval, ApprovalKind, ApprovalStatus, EmpType, Game, JidanpuMemberBinding, JidanpuSyncEvent,
    Order, OrderSource, OrderStatus, OrderType, Performance,
)
from apps.core.datetime_display import local_iso
from apps.internal_chat.models import Conversation, Message, SystemNoticeSettings
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


URL = "/api/business/approvals/"


def run_penalty_log_backfill():
    key = ("business", "0150_backfill_penalty_approval_logs")
    migration = import_module(f"apps.business.migrations.{key[1]}")
    historical_apps = MigrationExecutor(connection).loader.project_state([key], at_end=False).apps
    with connection.schema_editor(atomic=False) as editor:
        migration.backfill_penalty_approval_logs(historical_apps, editor)


class PenaltyApprovalTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="合成游戏")
        self.admin = make_logged_in_client(username="approval-manager", is_superuser=True)
        self.submitter = make_logged_in_client(
            username="approval-submit", groups=("罚款发起",),
            nodes=("home.approval_apply", "order.view_detail", "order.list", "order.assign_taker",
                   "order.restrict_to_self_created"),
        )
        self.approver = make_logged_in_client(username="approval-review", games=[])
        self.other_approver = make_logged_in_client(
            username="approval-other", groups=("罚款审批",),
            nodes=("home.approval_penalty_approve",),
        )
        users = get_user_model().objects
        self.reviewer = users.get(username="approval-review")
        self.initiator = users.get(username="approval-submit")
        StaffProfile.objects.filter(user=self.initiator).update(
            commission_mode=CommissionMode.PROFIT_PERCENT, commission_value=Decimal("20"),
        )
        self.target = users.create_user(username="penalty-target")
        StaffProfile.objects.create(user=self.target, member_type=MemberType.PARTNER).games.add(self.game)
        self.original = Order.objects.create(
            game=self.game, order_no="20261116000000001111",
            source_order_no="synthetic-orig", project_text="合成项目", total_price=50,
        )

    def apply(self, **overrides):
        return self.submitter.post(
            URL,
            {
                "original_order_no": self.original.order_no,
                "target_id": self.target.pk,
                "amount": "30",
                "reason": "合成罚款原因",
                "approver_id": self.reviewer.pk,
                "order": {"project_text": "合成罚款项目"},
                **overrides,
            },
            content_type="application/json",
        )

    def configure(self, **settings):
        res = self.admin.patch(
            URL + "settings/", {"approver_ids": [self.reviewer.pk], **settings},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], 0, res.json())
        return res.json()["data"]

    def test_approved_penalty_waits_for_manual_assignment_and_then_uses_normal_sync(self):
        self.configure()
        workers, _ = Group.objects.get_or_create(name="代练员")
        self.target.groups.add(workers)
        WxPusherBinding.objects.create(user=self.target, app_id="915", uid="UID_synthetic_penalty")
        binding = JidanpuMemberBinding.objects.create(
            user=self.target, jidanpu_base_url="https://penalty.test.invalid",
            account="synthetic-penalty", password="synthetic", enabled=True,
        )
        submitted = self.apply().json()
        self.assertEqual(submitted["code"], 0, submitted)
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name, WXPUSHER_APP_ID="915",
            WXPUSHER_APP_TOKEN="AT_synthetic_penalty",
        ):
            approved = self.approver.post(
                f"{URL}{submitted['data']['id']}/decision/", {"decision": "approved"},
                content_type="application/json",
            ).json()
        self.assertEqual(approved["code"], 0, approved)
        order = Order.objects.get(order_no=approved["data"]["generated_order_no"])
        self.assertEqual((order.status, order.hall_price), (OrderStatus.UNASSIGNED, Decimal("-30.00")))
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(main.user_id)
        self.assertIsNone(main.claimed_at)
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertFalse(main.is_settled or sales.is_settled)
        events = JidanpuSyncEvent.objects.filter(direction="out", target_ref=order.order_no)
        self.assertFalse(events.exists())
        self.assertEqual(list(WxPusherNotice.objects.filter(
            user=self.target, order_no=order.order_no,
        ).values_list("kind", flat=True)), [])

        visible = self.submitter.get("/api/business/orders/").json()
        self.assertEqual(visible["code"], 0, visible)
        self.assertEqual([row["order_no"] for row in visible["data"]["items"]], [order.order_no])
        assigned = self.submitter.post(
            "/api/business/orders/batch/assign/",
            {"order_nos": [order.order_no], "user_id": self.target.pk, "dry_run": False},
            content_type="application/json",
        ).json()
        self.assertEqual(assigned["code"], 0, assigned)
        order.refresh_from_db()
        self.assertEqual((order.status, order.hall_price), (OrderStatus.IN_PROGRESS, Decimal("-30.00")))
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.target.pk)
        self.assertEqual(list(events.values_list("kind", "status", "payload__member_binding_id")), [
            ("member.assign", "pending", binding.pk),
        ])

        for status in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            completed = self.admin.post(
                f"/api/business/orders/{order.order_no}/transition/", {"target": status},
                content_type="application/json",
            ).json()
            self.assertEqual(completed["code"], 0, completed)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).bonus_amount,
                         Decimal("-30.00"))
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.SALES).bonus_amount,
                         Decimal("6.00"))

    def test_penalty_waiting_on_an_employee_respects_the_specific_push_preference(self):
        approver = make_logged_in_client(
            username="staff-penalty-notice", groups=("审批员",), nodes=("my_profile",),
        )
        recipient = get_user_model().objects.get(username="staff-penalty-notice")
        self.configure(approver_ids=[recipient.pk])
        self.assertEqual(approver.patch(
            "/api/auth/wxpusher/staff/preferences", {"enabled": True, "events": {"approval_assigned": False}},
            content_type="application/json",
        ).json()["code"], 0)
        WxPusherBinding.objects.create(user=recipient, app_id="915", uid="UID_staff_penalty")
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name, WXPUSHER_APP_ID="915",
            WXPUSHER_APP_TOKEN="AT_synthetic_penalty",
        ):
            submitted = self.apply(approver_id=recipient.pk).json()
            self.assertEqual(submitted["code"], 0, submitted)
            notice = WxPusherNotice.objects.get(
                kind="approval_assigned", reference=f"approval:{submitted['data']['id']}",
            )
            self.assertEqual((notice.user_id, notice.status, notice.skip_reason),
                             (recipient.pk, "skipped", "personal_disabled"))
            self.assertEqual(approver.patch(
                "/api/auth/wxpusher/staff/preferences", {"events": {"approval_assigned": True}},
                content_type="application/json",
            ).json()["code"], 0)
            another = self.apply(approver_id=recipient.pk).json()
            self.assertEqual(another["code"], 0, another)
            self.assertEqual(WxPusherNotice.objects.get(
                kind="approval_assigned", reference=f"approval:{another['data']['id']}", user=recipient,
            ).status, "pending")

    @override_settings(DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True,
                       TT_CHAT_CORE_BACKEND="legacy", TT_CHAT_MAINTENANCE=False)
    def test_submission_notifies_only_approver_and_approval_notifies_only_initiator(self):
        copied = get_user_model().objects.get(username="approval-other")
        self.configure(available_cc_ids=[copied.pk], default_cc_ids=[copied.pk])

        def system_conversations(client):
            body = client.get("/api/internal-chat/conversations/").json()
            self.assertEqual(body["code"], 0, body)
            return [row for row in body["data"]["conversations"] if row["kind"] == "system"]

        submitted = self.apply().json()
        self.assertEqual(submitted["code"], 0, submitted)
        rooms = system_conversations(self.approver)
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["unread_count"], 1)
        self.assertIn("待你处理", rooms[0]["last_message"]["text"])
        self.assertIn("审批中心", rooms[0]["last_message"]["text"])
        self.assertEqual(system_conversations(self.submitter), [])
        self.assertEqual(system_conversations(self.other_approver), [])

        decision_url = f"{URL}{submitted['data']['id']}/decision/"
        decision = {"decision": "approved"}
        self.assertEqual(self.other_approver.post(decision_url, decision, content_type="application/json").status_code, 403)
        self.assertEqual(system_conversations(self.submitter), [])
        approved = self.approver.post(decision_url, decision, content_type="application/json").json()
        self.assertEqual(approved["code"], 0, approved)
        repeated = self.approver.post(decision_url, decision, content_type="application/json").json()
        self.assertNotEqual(repeated["code"], 0)
        recipient_rooms = system_conversations(self.submitter)
        self.assertEqual(len(recipient_rooms), 1)
        room = recipient_rooms[0]
        self.assertEqual(room["unread_count"], 1)
        self.assertIn("已通过", room["last_message"]["text"])
        self.assertIn("继续处理", room["last_message"]["text"])
        self.assertEqual(system_conversations(self.approver)[0]["unread_count"], 1)
        self.assertEqual(system_conversations(self.other_approver), [])
        denied = self.other_approver.get(f"/api/internal-chat/conversations/{room['id']}/messages/").json()
        self.assertEqual(denied["code"], 1002, denied)

        another = self.apply().json()
        self.assertEqual(another["code"], 0, another)
        rejected = self.approver.post(
            f"{URL}{another['data']['id']}/decision/", {"decision": "rejected", "comment": "合成驳回"},
            content_type="application/json",
        ).json()
        self.assertEqual(rejected["code"], 0, rejected)
        self.assertEqual(system_conversations(self.submitter), recipient_rooms)

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_MAINTENANCE=True)
    def test_chat_maintenance_preserves_submission_approval_and_order(self):
        self.configure()
        with self.assertLogs("apps.business.approvals", level="WARNING") as notices:
            submitted = self.apply().json()
            self.assertEqual(submitted["code"], 0, submitted)
            approved = self.approver.post(
                f"{URL}{submitted['data']['id']}/decision/", {"decision": "approved"},
                content_type="application/json",
            ).json()
            self.assertEqual(approved["code"], 0, approved)
        self.assertEqual(Approval.objects.get(pk=submitted["data"]["id"]).status, ApprovalStatus.APPROVED)
        order = Order.objects.get(order_no=approved["data"]["generated_order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertFalse(Conversation.objects.filter(system_recipient_id__in=[self.reviewer.pk, self.initiator.pk]).exists())
        self.assertEqual(len(notices.output), 2)
        self.assertIn("stage=submitted reason=ChatServiceUnavailable", notices.output[0])
        self.assertIn("stage=approved reason=ChatServiceUnavailable", notices.output[1])

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_MAINTENANCE=False, TT_CHAT_CORE_BACKEND="legacy")
    def test_approval_chat_switches_are_independent_of_business_and_staff_push(self):
        approver = make_logged_in_client(
            username="approval-notice-switch-reviewer", groups=("审批员",), nodes=("my_profile",),
        )
        reviewer = get_user_model().objects.get(username="approval-notice-switch-reviewer")
        self.configure(approver_ids=[reviewer.pk])
        rules = SystemNoticeSettings.objects.create(approval_submitted=False)

        first = self.apply(approver_id=reviewer.pk).json()
        self.assertEqual(first["code"], 0, first)
        first_id = first["data"]["id"]
        self.assertFalse(Message.objects.filter(client_id=f"approval:{first_id}:submitted").exists())
        self.assertTrue(WxPusherNotice.objects.filter(kind="approval_assigned", reference=f"approval:{first_id}").exists())
        decision = {"decision": "approved"}
        approved = approver.post(f"{URL}{first_id}/decision/", decision, content_type="application/json").json()
        self.assertEqual(approved["code"], 0, approved)
        self.assertEqual(Message.objects.get(client_id=f"approval:{first_id}:approved").conversation.system_recipient_id,
                         self.initiator.pk)

        rules.approval_submitted = True
        rules.approval_approved = False
        rules.save(update_fields=["approval_submitted", "approval_approved"])
        second = self.apply(approver_id=reviewer.pk).json()
        self.assertEqual(second["code"], 0, second)
        second_id = second["data"]["id"]
        self.assertEqual(Message.objects.get(client_id=f"approval:{second_id}:submitted").conversation.system_recipient_id,
                         reviewer.pk)
        approved = approver.post(f"{URL}{second_id}/decision/", decision, content_type="application/json").json()
        self.assertEqual(approved["code"], 0, approved)
        self.assertFalse(Message.objects.filter(client_id=f"approval:{second_id}:approved").exists())

    def test_only_designated_approver_can_generate_one_negative_child_order(self):
        self.configure()
        source = OrderSource.objects.create(name="合成来源")
        self.original.order_source = source
        self.original.order_type = OrderType.COMPANION
        self.original.content = "合成客户说明"
        self.original.demand = "合成代练说明"
        self.original.time_requirement = "合成时间"
        self.original.customer_phone = "合成联系电话"
        self.original.game_account = "合成游戏账号"
        self.original.received_price = Decimal("45.00")
        self.original.platform_price = Decimal("20.00")
        self.original.fee = Decimal("3.25")
        self.original.save()
        submitted = self.apply(order={
            "project_text": "合成罚款项目",
            "order_type": OrderType.BOOST,
            "total_price": "50",
        }).json()
        self.assertEqual(submitted["code"], 0, submitted)
        self.assertNotIn("customer_phone", submitted["data"]["order"])
        self.assertNotIn("game_account", submitted["data"]["order"])
        pending = self.approver.get(URL, {"mode": "pending"}).json()["data"]["items"]
        self.assertEqual([(row["id"], row["can_decide"]) for row in pending], [(submitted["data"]["id"], True)])
        self.assertEqual(self.approver.get(URL + "settings/").status_code, 403)
        self.assertEqual(self.approver.post(URL, {}, content_type="application/json").status_code, 403)
        order_url = f"/api/business/orders/{self.original.order_no}/"
        self.assertIn(self.approver.get(order_url).status_code, (403, 404))
        decide_url = f"{URL}{submitted['data']['id']}/decision/"
        decision = {"decision": "approved"}
        for unassigned in (self.other_approver, self.admin):
            self.assertEqual(unassigned.get(URL, {"mode": "pending"}).json()["data"]["items"], [])
            refused = unassigned.post(decide_url, decision, content_type="application/json")
            self.assertEqual(refused.status_code, 403)
            missing = unassigned.post(f"{URL}999999999/decision/", decision, content_type="application/json")
            self.assertEqual((missing.status_code, missing.json()), (403, refused.json()))
        with patch("apps.business.xgj_ship.dispatch_auto_ship") as ship:
            with self.captureOnCommitCallbacks(execute=True):
                result = self.approver.post(decide_url, decision, content_type="application/json").json()
        ship.assert_not_called()
        self.assertEqual(result["code"], 0, result)
        repeated = self.approver.post(decide_url, decision, content_type="application/json").json()
        self.assertNotEqual(repeated["code"], 0)

        child = Order.objects.get(order_no=result["data"]["generated_order_no"])
        self.assertEqual(child.hall_price, Decimal("-30"))
        self.assertEqual(child.created_by, self.initiator.username)
        self.assertEqual(child.game, self.game)
        self.assertEqual(child.order_source, source)
        self.assertEqual(child.order_type, OrderType.COMPANION)
        self.assertEqual(child.project_text, "合成罚款项目")
        self.assertEqual(child.content, "合成客户说明")
        self.assertEqual(child.demand, "合成代练说明")
        self.assertEqual(child.time_requirement, "合成时间")
        self.assertEqual(child.customer_phone, "")
        self.assertEqual(child.game_account, "")
        self.assertEqual(child.total_price, Decimal("0.00"))
        self.assertEqual(child.received_price, Decimal("0.00"))
        self.assertEqual(child.fee, Decimal("0.00"))
        self.assertIsNone(child.platform_price)
        self.assertEqual(child.sales_user_id, self.initiator.pk)
        self.assertEqual(child.status, OrderStatus.UNASSIGNED)
        self.assertEqual(child.penalty_approval.original_order_id, self.original.pk)
        self.assertIn("罚", child.source_order_no)
        self.assertNotEqual(child.source_order_no, self.original.source_order_no)
        self.assertEqual(Order.objects.count(), 2)
        self.original.refresh_from_db()
        self.assertEqual(self.original.total_price, Decimal("50"))
        self.assertEqual(self.original.received_price, Decimal("45.00"))
        self.assertEqual(self.original.fee, Decimal("3.25"))
        self.assertEqual(self.original.status, OrderStatus.UNASSIGNED)

        orders = "/api/business/orders/"
        parent_data = self.submitter.get(f"{orders}{self.original.order_no}/").json()["data"]
        child_data = self.submitter.get(f"{orders}{child.order_no}/").json()["data"]
        self.assertEqual(parent_data["penalty_children"], [{"order_no": child.order_no, "status": "UNASSIGNED"}])
        self.assertEqual(child_data["penalty_parent_order_no"], self.original.order_no)
        self.assertNotIn("penalty_approval", child_data)

        logs = OperationLog.objects.filter(
            order_no=child.order_no, action__in=("order.create", "order.penalty_submit", "order.penalty_approve"),
        ).values_list("action", "actor", "actor_id")
        self.assertCountEqual(logs, [
            ("order.create", "系统", None),
            ("order.penalty_submit", self.initiator.username, self.initiator.pk),
            ("order.penalty_approve", self.reviewer.username, self.reviewer.pk),
        ])
        audit_url = f"{orders}{child.order_no}/audit/?scope=business"
        audit = self.submitter.get(audit_url).json()
        self.assertEqual(audit["code"], 0, audit)
        approvals = [
            row for row in audit["data"]
            if row["action"] in {"order.penalty_submit", "order.penalty_approve"}
        ]
        self.assertEqual(
            [(row["action"], row["actor"], row["created_at"]) for row in approvals],
            [
                ("order.penalty_approve", self.reviewer.username, local_iso(child.penalty_approval.decided_at)),
                ("order.penalty_submit", self.initiator.username, local_iso(child.penalty_approval.created_at)),
            ],
        )
        self.assertIn("通过罚款审批", approvals[0]["summary"])
        self.assertIn("发起罚款审批", approvals[1]["summary"])
        self.assertEqual(self.other_approver.get(audit_url).status_code, 403)

    def test_backfill_uses_original_events_for_approved_penalty_orders_once(self):
        self.initiator.first_name = "合成发起人"
        self.initiator.save(update_fields=["first_name"])
        self.reviewer.first_name = "合成审批人"
        self.reviewer.save(update_fields=["first_name"])
        decided_at = timezone.now() - timedelta(days=1)
        submitted_at = decided_at - timedelta(hours=1)
        options = {
            "kind": ApprovalKind.PENALTY, "initiator": self.initiator, "approver": self.reviewer,
            "original_order": self.original, "target": self.target, "amount": 30, "reason": "合成罚款原因",
        }
        approved = Approval.objects.create(status=ApprovalStatus.APPROVED, **options)
        rejected = Approval.objects.create(status=ApprovalStatus.REJECTED, **options)
        Approval.objects.filter(pk__in=(approved.pk, rejected.pk)).update(
            created_at=submitted_at, decided_at=decided_at,
        )
        child = Order.objects.create(
            game=self.game, order_no="20261116000000001112", source_order_no="罚-历史样本",
            status=OrderStatus.COMPLETED, hall_price=-30, penalty_approval=approved,
        )
        Order.objects.create(game=self.game, order_no="20261116000000001113", penalty_approval=rejected)

        logs = OperationLog.objects.filter(action__in=("order.penalty_submit", "order.penalty_approve"))
        run_penalty_log_backfill()
        first_ids = list(logs.values_list("pk", flat=True))
        run_penalty_log_backfill()
        self.assertEqual(list(logs.values_list("pk", flat=True)), first_ids)
        self.assertEqual(
            list(logs.values_list("order_no", "action", "actor_id", "created_at", "summary")),
            [
                (child.order_no, "order.penalty_approve", self.reviewer.pk, decided_at,
                 "【合成审批人】通过罚款审批（订单 罚-历史样本）"),
                (child.order_no, "order.penalty_submit", self.initiator.pk, submitted_at,
                 "【合成发起人】发起罚款审批（订单 罚-历史样本）"),
            ],
        )
        child.refresh_from_db()
        self.assertEqual(child.hall_price, Decimal("-30.00"))
        self.assertEqual(child.penalty_approval.original_order_id, self.original.pk)

        audit_url = f"/api/business/orders/{child.order_no}/audit/?scope=business"
        stored = self.submitter.get(audit_url).json()["data"]
        logs.filter(action="order.penalty_submit").delete()
        partial = self.submitter.get(audit_url).json()["data"]
        self.assertEqual(len(partial), 2)
        self.assertEqual(partial[0], stored[0])
        self.assertLess(partial[1]["id"], 0)
        self.assertEqual(logs.count(), 1)

        logs.delete()
        restored = self.submitter.get(audit_url).json()["data"]
        self.assertEqual(len(restored), 2)
        self.assertLess(restored[0]["id"], 0)
        self.assertEqual(restored[1], partial[1])
        self.assertNotEqual(restored[0]["id"], restored[1]["id"])
        self.assertEqual(
            [{key: value for key, value in row.items() if key != "id"} for row in restored],
            [{key: value for key, value in row.items() if key != "id"} for row in stored],
        )
        self.assertEqual(self.submitter.get(audit_url).json()["data"], restored)
        self.assertEqual(self.other_approver.get(audit_url).status_code, 403)
        self.assertFalse(logs.exists())

    def test_older_pending_linked_penalty_starts_with_zero_order_amounts(self):
        self.configure()
        submitted = self.apply().json()
        self.assertEqual(submitted["code"], 0, submitted)
        pending = Approval.objects.get(pk=submitted["data"]["id"])
        pending.order_data.update(total_price="40", received_price="30", fee="5")
        pending.save(update_fields=["order_data"])

        decision = self.approver.post(
            f"{URL}{pending.pk}/decision/", {"decision": "approved"}, content_type="application/json",
        ).json()
        self.assertEqual(decision["code"], 0, decision)
        child = Order.objects.get(order_no=decision["data"]["generated_order_no"])
        self.assertEqual((child.total_price, child.received_price, child.fee), (0, 0, 0))
        self.assertEqual(child.hall_price, Decimal("-30.00"))

    def test_copied_account_can_read_approval_but_cannot_decide(self):
        viewer = make_logged_in_client(username="approval-copy")
        viewer_id = get_user_model().objects.get(username="approval-copy").pk
        self.configure(available_cc_ids=[viewer_id], default_cc_ids=[viewer_id])
        self.assertTrue(OperationLog.objects.filter(action="system.approval_settings_update").exists())
        row = self.apply(objection="对方有异议", remark="合成备注").json()["data"]
        defaults_only = self.apply(cc_ids=[]).json()
        self.assertEqual(defaults_only["code"], 0, defaults_only)
        self.assertEqual(defaults_only["data"]["cc_names"], ["approval-copy"])
        listed = viewer.get(URL + "?mode=copied").json()["data"]["items"]
        self.assertEqual([item["id"] for item in listed], [defaults_only["data"]["id"], row["id"]])
        self.assertFalse(listed[1]["can_decide"])
        self.assertEqual(listed[1]["objection"], "对方有异议")
        refused = viewer.post(
            f"{URL}{row['id']}/decision/", {"decision": "approved"}, content_type="application/json",
        )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(Order.objects.count(), 1)

    def test_another_users_evidence_cannot_be_attached(self):
        self.configure()
        ticket = self.submitter.post(URL + "media/ticket/", {"ext": "png"}, content_type="application/json")
        body = ticket.json()
        self.assertEqual(body["code"], 0, body)
        key = body["data"]["key"]
        with patch("apps.business.api.approval_views.get_storage") as storage:
            response = self.apply(image_keys=[key.replace(f"/{self.initiator.pk}/", f"/{self.target.pk}/")])
        self.assertEqual(response.status_code, 403)
        storage.return_value.head.assert_not_called()
        self.assertFalse(Approval.objects.exists())

    def test_target_search_combines_workers_and_studios_in_the_original_game(self):
        studio = get_user_model().objects.create_user(username="penalty-studio")
        StaffProfile.objects.create(
            user=studio, member_type=MemberType.PARTNER, is_studio=True,
        ).games.add(self.game)
        other_game = Game.objects.create(name="另一个合成游戏")
        outsider = get_user_model().objects.create_user(username="penalty-off-scope")
        StaffProfile.objects.create(user=outsider, member_type=MemberType.PARTNER).games.add(other_game)
        body = self.submitter.get(URL + "targets/", {
            "original_order_no": self.original.order_no, "key": "penalty",
        }).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            {item["id"] for item in body["data"]["items"]}, {self.target.pk, studio.pk},
        )
        self.assertEqual(
            {item["id"]: item["detail"] for item in body["data"]["items"]},
            {self.target.pk: f"打手 · #{self.target.pk}", studio.pk: f"下家工作室 · #{studio.pk}"},
        )
        self.assertIsNone(body["data"]["suggested"])
        without_game = self.submitter.get(URL + "targets/", {"key": "penalty"}).json()
        self.assertEqual(
            {item["id"] for item in without_game["data"]["items"]}, {self.target.pk, studio.pk},
        )
        Performance.objects.create(order=self.original, emp_type=EmpType.MAIN, user=studio)
        filtered = self.submitter.get(URL + "targets/", {
            "original_order_no": self.original.order_no, "key": "penalty-target",
        }).json()
        self.assertEqual([item["id"] for item in filtered["data"]["items"]], [self.target.pk])
        self.assertEqual(filtered["data"]["suggested"]["id"], studio.pk)

        self.configure()
        applied = self.apply(target_id=studio.pk, target_type="worker").json()
        self.assertEqual(applied["code"], 0, applied)
        self.assertEqual(applied["data"]["target_type"], "studio")

    def test_standalone_penalty_requires_an_explicit_choice_and_order_source(self):
        self.configure()
        source = OrderSource.objects.create(name="合成订单来源")
        payload = {
            "target_id": self.target.pk, "amount": "30", "reason": "合成罚款原因",
            "approver_id": self.reviewer.pk,
            "order": {"project_text": "合成罚款项目", "game": self.game.pk, "order_source": source.pk},
        }
        missing_choice = self.submitter.post(URL, payload, content_type="application/json").json()
        self.assertNotEqual(missing_choice["code"], 0)
        missing_source = self.apply(
            original_order_no=None, order={"game": self.game.pk, "project_text": "合成罚款项目"},
        ).json()
        self.assertNotEqual(missing_source["code"], 0)
        standalone = self.submitter.post(
            URL, payload | {"original_order_no": None}, content_type="application/json",
        ).json()
        self.assertEqual(standalone["code"], 0, standalone)
        self.assertIsNone(standalone["data"]["original_order_no"])
        decision = self.approver.post(
            f"{URL}{standalone['data']['id']}/decision/", {"decision": "approved"},
            content_type="application/json",
        ).json()
        self.assertEqual(decision["code"], 0, decision)
        child = Order.objects.get(order_no=decision["data"]["generated_order_no"])
        self.assertIsNone(child.penalty_approval.original_order_id)
        self.assertEqual(child.order_source, source)
        self.assertEqual(child.total_price, Decimal("0.00"))
        self.assertEqual(child.order_type, OrderType.BOOST)

    def test_refund_orphan_is_routed_and_can_be_decided_without_order_changes(self):
        handler_client = make_logged_in_client(username="refund-handler", games=[])
        handler = get_user_model().objects.get(username="refund-handler")
        first = create_refund_approval(self.original, reason="闲管家退款")
        second = create_refund_approval(self.original, reason="闲管家关闭")
        self.assertEqual(first.id, second.id)
        orphan = self.admin.get(URL + "?mode=pending").json()["data"]["items"][0]
        self.assertIsNone(orphan["approver_name"])
        self.assertTrue(orphan["requires_assignment"])
        self.assertFalse(orphan["can_decide"])

        settings = self.admin.patch(
            URL + "settings/", {"refund_handler_id": handler.pk}, content_type="application/json",
        ).json()
        self.assertEqual(settings["code"], 0, settings)
        assigned = handler_client.get(URL + "?mode=pending").json()["data"]["items"][0]
        self.assertTrue(assigned["can_decide"])
        self.assertEqual(assigned["original_order_no"], self.original.order_no)
        order_url = f"/api/business/orders/{self.original.order_no}/"
        self.assertEqual(self.admin.get(order_url).json()["code"], 0)
        self.assertIn(handler_client.get(order_url).status_code, (403, 404))
        self.assertEqual(self.admin.get(URL, {"mode": "pending"}).json()["data"]["items"], [])
        refused = self.admin.post(
            f"{URL}{assigned['id']}/decision/", {"decision": "approved"}, content_type="application/json",
        )
        self.assertEqual(refused.status_code, 403)
        response = handler_client.post(
            f"{URL}{assigned['id']}/decision/",
            {"decision": "approved", "comment": "人工已处理"}, content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)
        self.assertIsNone(response["data"]["generated_order_no"])
        self.original.refresh_from_db()
        self.assertEqual(self.original.status, OrderStatus.UNASSIGNED)
        self.assertEqual(Order.objects.count(), 1)

    def test_disabled_handler_requires_reassignment_and_replacement_needs_no_game_scope(self):
        handler_client = make_logged_in_client(
            username="refund-first-handler", groups=("退款处理",),
            nodes=("home.approval_refund_handle", "order.view_detail"),
        )
        replacement = make_logged_in_client(
            username="refund-replacement", groups=("订单查看",), nodes=("order.view_detail",), games=[],
        )
        users = get_user_model().objects
        handler = users.get(username="refund-first-handler")
        successor = users.get(username="refund-replacement")
        self.configure(refund_handler_id=handler.pk)
        approval = create_refund_approval(self.original, reason="拟处理退款")
        old_list = handler_client.get(URL + "?mode=pending").json()["data"]["items"]
        self.assertTrue(old_list[0]["can_decide"])

        handler.groups.clear()
        handler.staff_profile.games.clear()
        old_list = handler_client.get(URL + "?mode=pending").json()["data"]["items"]
        self.assertEqual([(r["id"], r["can_decide"]) for r in old_list], [(approval.pk, True)])

        users.filter(pk=handler.pk).update(is_active=False)
        need_assignment = self.admin.get(URL + "?mode=pending").json()["data"]["items"]
        self.assertEqual([item["id"] for item in need_assignment], [approval.pk])
        self.assertTrue(need_assignment[0]["requires_assignment"])
        self.assertFalse(need_assignment[0]["can_decide"])
        refused = handler_client.post(
            f"{URL}{approval.pk}/decision/", {"decision": "approved"}, content_type="application/json",
        )
        self.assertIn(refused.status_code, (401, 403))

        self.configure(refund_handler_id=successor.pk)
        new_list = replacement.get(URL + "?mode=pending").json()["data"]["items"]
        self.assertEqual([item["id"] for item in new_list], [approval.pk])
        self.assertFalse(new_list[0]["requires_assignment"])
        self.assertTrue(new_list[0]["can_decide"])
        order_url = f"/api/business/orders/{self.original.order_no}/"
        denied_order = replacement.get(order_url).json()
        self.assertNotEqual(denied_order["code"], 0, denied_order)
        self.assertIsNone(denied_order["data"])
        response = replacement.post(
            f"{URL}{approval.pk}/decision/", {"decision": "approved"}, content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)

    def test_settings_and_decision_exclude_ineligible_accounts(self):
        users = get_user_model().objects
        disabled = users.create_user(username="disabled-reviewer", is_active=False)
        app = users.create_user(username="app-reviewer")
        StaffProfile.objects.create(user=app, account_source=AccountSource.APP)
        deleted = users.create_user(username="deleted-reviewer")
        StaffProfile.objects.create(user=deleted, deleted_at=timezone.now())
        choices = self.configure()["users"]
        self.assertIn({
            "id": self.reviewer.pk, "name": self.reviewer.username,
            "can_approve": True, "can_handle_refund": True,
        }, choices)
        for user in (disabled, app, deleted):
            with self.subTest(user=user.username):
                self.assertNotIn(user.pk, {choice["id"] for choice in choices})
                for updates in ({"approver_ids": [user.pk]}, {"refund_handler_id": user.pk}):
                    rejected = self.admin.patch(URL + "settings/", updates, content_type="application/json").json()
                    self.assertNotEqual(rejected["code"], 0, rejected)

        applied = self.apply().json()
        self.assertEqual(applied["code"], 0, applied)
        StaffProfile.objects.filter(user=self.reviewer).update(deleted_at=timezone.now())
        options = self.submitter.get(URL + "options/").json()["data"]
        self.assertEqual(options["approvers"], [])
        self.assertNotEqual(self.apply().json()["code"], 0)
        response = self.approver.post(
            f"{URL}{applied['data']['id']}/decision/", {"decision": "approved"}, content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_decision_checks_current_account_state_after_authentication(self):
        self.configure(refund_handler_id=self.reviewer.pk)
        approval = create_refund_approval(self.original, reason="账号停用前收到的待办")
        get_user_model().objects.filter(pk=self.reviewer.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            decide_approval(approval.pk, user=self.reviewer, decision=ApprovalStatus.APPROVED, comment="")
        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalStatus.PENDING)

    def test_refund_configuration_repairs_orphans_without_replacing_valid_assignments(self):
        self.configure(refund_handler_id=self.reviewer.pk)
        existing = Approval.objects.create(
            kind=ApprovalKind.REFUND, approver=self.initiator, original_order=self.original, reason="原有指派",
        )
        order_url = f"/api/business/orders/{self.original.order_no}/"
        self.assertEqual(self.submitter.get(order_url).json()["code"], 0)
        self.original.deleted_at = timezone.now()
        self.original.save(update_fields=["deleted_at"])
        orphan = Approval.objects.create(
            kind=ApprovalKind.REFUND, original_order=self.original, refund_source_no="orphan", reason="待处理",
        )
        self.configure(refund_handler_id=self.reviewer.pk)
        for client, approval in ((self.submitter, existing), (self.approver, orphan)):
            rows = client.get(URL, {"mode": "pending"}).json()["data"]["items"]
            self.assertEqual([(r["id"], r["requires_assignment"], r["can_decide"]) for r in rows], [
                (approval.pk, False, True),
            ])
        denied_order = self.submitter.get(order_url).json()
        self.assertNotEqual(denied_order["code"], 0, denied_order)
        self.assertIsNone(denied_order["data"])
        response = self.approver.post(
            f"{URL}{orphan.pk}/decision/", {"decision": "approved"}, content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)

        self.configure(refund_handler_id=None)
        rows = self.admin.get(URL, {"mode": "pending"}).json()["data"]["items"]
        self.assertEqual([(r["id"], r["approver_name"], r["requires_assignment"]) for r in rows], [
            (existing.pk, None, True),
        ])
        self.configure(refund_handler_id=self.reviewer.pk)
        rows = self.approver.get(URL, {"mode": "pending"}).json()["data"]["items"]
        self.assertEqual([(r["id"], r["requires_assignment"], r["can_decide"]) for r in rows], [
            (existing.pk, False, True),
        ])

    def test_new_refund_source_number_can_have_its_own_approval(self):
        self.original.source_order_no = "  旧来源单  "
        self.original.save(update_fields=["source_order_no"])
        initial = create_refund_approval(self.original, reason="旧来源退款")
        self.assertTrue(has_refund_approval(self.original))
        self.assertEqual(initial.refund_source_no, "旧来源单")
        self.original.source_order_no = "新来源单"
        self.original.save(update_fields=["source_order_no"])
        self.assertFalse(has_refund_approval(self.original))
        new = create_refund_approval(self.original, reason="新来源退款")
        self.assertNotEqual(initial.pk, new.pk)
        repeat = create_refund_approval(self.original, reason="新来源关闭")
        self.assertEqual(new.pk, repeat.pk)
        self.assertTrue(has_refund_approval(self.original))


class ApprovalTenantIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(
            schema_name="approval_other", name="另一审批租户", domain="approval-other.test.local",
        )

    def test_other_tenant_cannot_read_or_decide_approval(self):
        origin = Game.objects.create(name="甲的游戏")
        order = Order.objects.create(game=origin, order_no="20261116000000002221")
        client_a = make_logged_in_client(username="log-approval", is_superuser=True)
        actor_a = get_user_model().objects.get(username="log-approval")
        settings = client_a.patch(
            URL + "settings/", {"refund_handler_id": actor_a.pk}, content_type="application/json",
        ).json()
        self.assertEqual(settings["code"], 0, settings)
        create_refund_approval(order, reason="仅甲退款")
        rows_a = client_a.get(URL + "?mode=pending").json()["data"]["items"]
        self.assertEqual([r["original_order_no"] for r in rows_a], [order.order_no])
        self.assertTrue(rows_a[0]["can_decide"])
        refund_id = rows_a[0]["id"]
        penalty = Approval.objects.create(
            kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED, original_order=order,
            initiator=actor_a, approver=actor_a,
        )
        penalty.decided_at = timezone.now()
        penalty.save(update_fields=["decided_at"])
        child_no = "20261116000000002223"
        Order.objects.create(game=origin, order_no=child_no, penalty_approval=penalty)
        audit_url = f"/api/business/orders/{child_no}/audit/?scope=business"
        run_penalty_log_backfill()
        self.assertEqual(len(client_a.get(audit_url).json()["data"]), 2)
        OperationLog.objects.filter(order_no=child_no).delete()
        restored = client_a.get(audit_url).json()["data"]
        self.assertEqual([row["action"] for row in restored], ["order.penalty_approve", "order.penalty_submit"])

        with tenant_context(self.other):
            control_game = Game.objects.create(name="乙的游戏")
            control = Order.objects.create(game=control_game, order_no="20261116000000002222")
            Order.objects.create(game=control_game, order_no=child_no)
            Approval.objects.create(pk=refund_id, kind=ApprovalKind.REFUND, original_order=control, reason="仅乙退款")
            client_b = make_logged_in_client(
                "approval-other.test.local",
                username="log-approval", is_superuser=True,
            )
            response = client_b.get(URL + "?mode=pending").json()
            self.assertEqual(response["code"], 0, response)
            self.assertEqual(
                [r["original_order_no"] for r in response["data"]["items"]], [control.order_no],
            )
            decision = {"decision": "approved"}
            denied = client_b.post(f"{URL}{refund_id}/decision/", decision, content_type="application/json")
            missing = client_b.post(f"{URL}999999999/decision/", decision, content_type="application/json")
            self.assertEqual((denied.status_code, denied.json()), (403, missing.json()))
            local_refund = Approval.objects.get(reason="仅乙退款", original_order=control)
            self.assertEqual(local_refund.status, ApprovalStatus.PENDING)
            local_targets = client_b.get(URL + "targets/", {"original_order_no": control.order_no}).json()
            self.assertEqual(local_targets["code"], 0, local_targets)
            foreign_targets = client_b.get(URL + "targets/", {"original_order_no": order.order_no}).json()
            self.assertNotEqual(foreign_targets["code"], 0)
            self.assertIsNone(foreign_targets["data"])
            audit_b = client_b.get(audit_url).json()
            self.assertEqual(audit_b["code"], 0, audit_b)
            self.assertEqual(audit_b["data"], [])

        remote_refund = Approval.objects.get(reason="仅甲退款", original_order=order)
        self.assertEqual(remote_refund.status, ApprovalStatus.PENDING)
