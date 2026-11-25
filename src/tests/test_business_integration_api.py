from django.contrib.auth import get_user_model
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import DailiantongPush, Game, Order, OrderStatus, Performance
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_rbac
from apps.openapi.models import ApiApp


class BusinessIntegrationTests(TenantTestCase):
    prefix = "/api/integration/v1/"

    def setUp(self):
        self.client = Client(HTTP_HOST=self.domain.domain, REMOTE_ADDR="203.0.113.41")
        user = get_user_model().objects.create_user(username="integration-owner")
        seed_rbac()
        self.app = ApiApp.objects.create(
            name="SDK 测试应用", kind="business_integration", state="sdk_enabled", app_key="sdk-synthetic-key",
            app_secret="sdk-synthetic-secret-for-tests", created_by=user,
        )
        self.game = Game.objects.create(name="SDK 测试游戏")
        self.order = Order.objects.create(game=self.game, order_no="20261116000000000041")

    def token(self, app):
        response = self.client.post("/api/open/oauth/token", {
            "grant_type": "client_credentials", "client_id": app.app_key,
            "client_secret": app.app_secret,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["access_token"]

    def test_business_credentials_read_local_orders_and_cannot_read_bills(self):
        token = self.token(self.app)
        response = self.client.get(self.prefix + "orders/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual([row["order_no"] for row in data["results"]], [self.order.order_no])
        self.assertNotIn("costs", data["results"][0])
        response = self.client.get("/api/open/v1/bills/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 403, response.content)

    def test_general_credentials_cannot_call_integration_endpoints(self):
        self.app.kind = "general"
        self.app.state = "enabled"
        self.app.save(update_fields=["kind", "state"])
        token = self.token(self.app)
        response = self.client.get(self.prefix + "orders/", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 403, response.content)

    def request(self, method, path, token, data=None, *, key="sdk-request-key-01"):
        client_method = getattr(self.client, method.lower())
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}", "HTTP_X_TT_ACTOR": "app-operations"}
        if method != "GET":
            kwargs.update({"content_type": "application/json", "HTTP_IDEMPOTENCY_KEY": key})
        return client_method(self.prefix + path, data={} if data is None else data, **kwargs)

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def create_taker(self, token, suffix):
        return self.ok(self.request("POST", "takers/", token, {
            "username": f"sdk-worker-{suffix}", "display_name": f"测试打手 {suffix}",
            "games": [self.game.pk], "grab_limit_dl": 20,
        }, key=f"sdk-create-taker-{suffix}"))

    def bind_taker(self, token, taker, subject):
        self.ok(self.request("PUT", f"taker-bindings/{subject}/", token, {
            "taker_id": taker["id"],
        }, key=f"sdk-bind-taker-{subject}"))
        response = self.request("POST", "taker-token/", token, {"external_user_id": subject})
        self.assertEqual(response["Cache-Control"], "no-store")
        return self.ok(response)["access_token"]

    def make_order(self, token, source_no):
        return self.ok(self.request("POST", "orders/", token, {
            "game": self.game.pk, "project_text": "SDK 合成项目", "total_price": "100.00",
            "hall_price": "88.00", "source_order_no": source_no,
        }, key=f"sdk-create-order-{source_no}"))

    def test_workshop_assignment_and_taker_submission_routes(self):
        token = self.token(self.app)
        taker = self.create_taker(token, "assign")
        actor = self.bind_taker(token, taker, "subject-assign")
        other_taker = self.create_taker(token, "other")
        other_actor = self.bind_taker(token, other_taker, "subject-other")
        Role.objects.get(name="代练员").permissions.add(PermissionNode.objects.get(code="order.submit_work"))
        order = self.make_order(token, "sdk-assign")
        no = order["order_no"]
        edited = self.ok(self.request("PATCH", f"orders/{no}/", token, {
            "remark": "SDK 测试", "total_price": "120.00",
        }, key="sdk-order-price-change"))
        self.assertEqual(edited["total_price"], "120.00")
        assigned = self.ok(self.request("POST", f"orders/{no}/assign/", token, {
            "taker_id": taker["id"],
        }, key="sdk-assign-order"))
        self.assertEqual(assigned["taker_id"], taker["id"])
        self.assertEqual(assigned["status"], "IN_PROGRESS")
        order_list = self.ok(self.request("GET", "taker/my/orders/", actor))
        self.assertIn(no, [item["order_no"] for item in order_list["results"]])
        self.assertEqual(self.request("GET", f"taker/orders/{no}/", other_actor).json()["code"], 1002)
        submitted = self.ok(self.request("POST", f"taker/orders/{no}/status/", actor, {
            "status": "PENDING_REVIEW",
        }, key="sdk-submit-work"))
        self.assertEqual(submitted["status"], "PENDING_REVIEW")
        no_replay = self.request("POST", f"taker/orders/{no}/status/", other_actor, {
            "status": "PENDING_REVIEW",
        }, key="sdk-submit-work")
        self.assertEqual(no_replay.json()["code"], 1002)
        done = self.ok(self.request("POST", f"orders/{no}/status/", token, {
            "status": "COMPLETED",
        }, key="sdk-accept-order"))
        self.assertEqual(done["status"], "COMPLETED")
        self.assertTrue(OperationLog.objects.filter(order_no=no, actor__contains="app-operations").exists())

    def test_local_claim_cannot_be_overwritten_by_ordinary_assignment(self):
        token = self.token(self.app)
        taker = self.create_taker(token, "claim")
        other = self.create_taker(token, "reassign")
        actor = self.bind_taker(token, taker, "subject-claim")
        role = Role.objects.get(name="代练员")
        role.permissions.add(PermissionNode.objects.get(code="grab_hall.take_order"))
        order = self.make_order(token, "sdk-claim")
        no = order["order_no"]
        self.ok(self.request("POST", f"orders/{no}/publish/", token, key="sdk-publish-order"))
        hall = self.ok(self.request("GET", "taker/hall/", actor))
        self.assertIn(no, [item["order_no"] for item in hall["results"]])
        claim = self.request("POST", f"taker/hall/{no}/claim/", actor, key="sdk-claim-order")
        self.assertEqual(self.ok(claim)["status"], "IN_PROGRESS")
        assign = self.request("POST", f"orders/{no}/assign/", token, {
            "taker_id": other["id"],
        }, key="sdk-assign-conflict")
        self.assertEqual(assign.json()["code"], 1002)
        self.assertEqual(self.ok(self.request("GET", f"orders/{no}/", token))["taker_id"], taker["id"])
        reassigned = self.ok(self.request("POST", f"orders/{no}/reassign/", token, {
            "taker_id": other["id"],
        }, key="sdk-reassign-order"))
        self.assertEqual(reassigned["taker_id"], other["id"])

    def test_external_pending_order_cannot_be_claimed_or_assigned(self):
        token = self.token(self.app)
        taker = self.create_taker(token, "external")
        actor = self.bind_taker(token, taker, "subject-external")
        Role.objects.get(name="代练员").permissions.add(PermissionNode.objects.get(code="grab_hall.take_order"))
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.hall_price = "88.00"
        self.order.save(update_fields=["status", "hall_price"])
        performance = Performance.objects.create(order=self.order)
        DailiantongPush.objects.create(order=self.order, price="88.00")
        no = self.order.order_no
        hall = self.ok(self.request("GET", "taker/hall/", actor))
        self.assertNotIn(no, [item["order_no"] for item in hall["results"]])
        claim = self.request("POST", f"taker/hall/{no}/claim/", actor, key="sdk-claim-foreign")
        self.assertEqual(claim.json()["code"], 1000)
        assign = self.request("POST", f"orders/{no}/assign/", token, {
            "taker_id": taker["id"],
        }, key="sdk-assign-foreign")
        self.assertEqual(assign.json()["code"], 1000)
        performance.refresh_from_db()
        self.assertIsNone(performance.user_id)

    def test_disabled_and_unbound_takers_cannot_use_issued_tokens(self):
        token = self.token(self.app)
        taker = self.create_taker(token, "disabled")
        actor = self.bind_taker(token, taker, "subject-disabled")
        self.ok(self.request("GET", "taker/hall/", actor))
        self.ok(self.request("POST", f"takers/{taker['id']}/disable/", token, key="sdk-taker-disable"))
        self.assertEqual(self.request("GET", "taker/hall/", actor).status_code, 401)
        self.ok(self.request("POST", f"takers/{taker['id']}/enable/", token, key="sdk-taker-enable"))
        refreshed = self.ok(self.request("POST", "taker-token/", token, {"external_user_id": "subject-disabled"}))
        self.ok(self.request("DELETE", "taker-bindings/subject-disabled/", token, key="sdk-taker-unbind"))
        self.assertEqual(self.request("GET", "taker/hall/", refreshed["access_token"]).status_code, 401)

    def test_worker_endpoint_cannot_create_a_manager(self):
        token = self.token(self.app)
        response = self.request("POST", "takers/", token, {
            "username": "sdk-synthetic-admin", "games": [self.game.pk], "role": "系统管理员",
        })
        self.assertEqual(response.json()["code"], 1000)
        self.assertFalse(get_user_model().objects.filter(username="sdk-synthetic-admin").exists())

    def test_clear_taker_and_recycle_use_existing_hooks(self):
        token = self.token(self.app)
        taker = self.create_taker(token, "clear")
        order = self.make_order(token, "sdk-clear")
        no = order["order_no"]
        self.ok(self.request("POST", f"orders/{no}/assign/", token, {"taker_id": taker["id"]}, key="sdk-clear-assign"))
        cleared = self.ok(self.request("POST", f"orders/{no}/clear-taker/", token, key="sdk-clear-taker"))
        self.assertEqual(cleared["status"], "UNASSIGNED")
        self.assertIsNone(cleared["taker_id"])
        self.ok(self.request("POST", f"orders/{no}/publish/", token, key="sdk-clear-publish"))
        not_allowed = self.request("POST", f"orders/{no}/delete/", token, {"isDelEmp": "Y"}, key="sdk-financial-delete")
        self.assertEqual(not_allowed.json()["code"], 1000)
        deleted = self.ok(self.request("POST", f"orders/{no}/delete/", token, key="sdk-order-delete"))
        self.assertIsNotNone(deleted["deleted_at"])
        self.assertIsNone(Performance.objects.get(order__order_no=no).deleted_at)
        recycle_bin = self.ok(self.request("GET", "orders/recycle-bin/", token))
        self.assertIn(no, [row["order_no"] for row in recycle_bin["results"]])
        restored = self.ok(self.request("POST", f"orders/{no}/restore/", token, key="sdk-order-restore"))
        self.assertIsNone(restored["deleted_at"])
        self.assertEqual(restored["status"], "UNASSIGNED")

    def test_local_split_creates_unassigned_zero_price_child(self):
        token = self.token(self.app)
        order = self.make_order(token, "sdk-work-split")
        no = order["order_no"]
        split = self.ok(self.request("POST", f"orders/{no}/split/", token, {
            "projects": ["原单项目", "拆出项目"],
        }, key="sdk-split-order"))
        child = self.ok(self.request("GET", f"orders/{split['created'][0]['order_no']}/", token))
        self.assertEqual(child["status"], "UNASSIGNED")
        self.assertEqual(child["total_price"], "0.00")
        self.assertEqual(child["order"]["project_text"], "拆出项目")

    def test_management_command_writes_credentials_only_to_private_files(self):
        import json
        import stat
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from django.core.management import call_command

        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.json"
            rotated = Path(temporary) / "rotated.json"
            stdout = StringIO()
            options = {"name": "命令管理应用", "schema": self.tenant.schema_name, "stdout": stdout}
            call_command("configure_business_integration", "create", owner="integration-owner", output_file=first, **options)
            payload = json.loads(first.read_text())
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
            self.assertNotIn(payload["client_secret"], stdout.getvalue())
            app = ApiApp.objects.get(app_key=payload["client_id"])
            old = self.token(app)
            self.ok(self.request("GET", "orders/", old))
            call_command("configure_business_integration", "rotate", output_file=rotated, **options)
            rotated_payload = json.loads(rotated.read_text())
            self.assertNotIn(rotated_payload["client_secret"], stdout.getvalue())
            self.assertEqual(self.request("GET", "orders/", old).status_code, 401)
            app.refresh_from_db()
            self.ok(self.request("GET", "orders/", self.token(app)))

    def test_local_transitions_cannot_interrupt_withdrawal_or_reverse_refunds(self):
        from apps.accounts.models import WithdrawOrder
        from apps.business.models import OrderRefund

        token = self.token(self.app)
        taker = self.create_taker(token, "finance-guard")
        order = self.make_order(token, "sdk-finance-guard")
        no = order["order_no"]
        self.ok(self.request("POST", f"orders/{no}/assign/", token, {"taker_id": taker["id"]}, key="sdk-guard-assign"))
        withdrawal = WithdrawOrder.objects.create(
            user_id=taker["id"], amount="12.00", alipay_account="synthetic", alipay_name="测试账号",
            state=WithdrawOrder.STATE_APPROVED,
        )
        refused = self.request("POST", f"orders/{no}/status/", token, {"status": "REFUNDED"}, key="sdk-guard-refund")
        self.assertEqual(refused.json()["code"], 1002)
        self.assertIn("财务", refused.json()["msg"])
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.state, WithdrawOrder.STATE_APPROVED)
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.IN_PROGRESS)
        withdrawal.delete()
        refunded = self.ok(self.request("POST", f"orders/{no}/status/", token, {"status": "REFUNDED"}, key="sdk-local-refund"))
        self.assertEqual(refunded["status"], OrderStatus.REFUNDED)
        Order.objects.filter(order_no=no).update(refund_fee="7.00")
        cleared = self.request("POST", f"orders/{no}/clear-taker/", token, key="sdk-reverse-refund")
        self.assertEqual(cleared.json()["code"], 1002)
        self.assertEqual(str(Order.objects.get(order_no=no).refund_fee), "7.00")
        self.assertFalse(OrderRefund.objects.filter(order__order_no=no).exists())

    def test_membership_tasks_and_automatic_external_publish_block_writes(self):
        from apps.business import jidanpu_member
        from apps.business.models import JidanpuMemberBinding, JidanpuSyncEvent

        token = self.token(self.app)
        taker = self.create_taker(token, "member-guard")
        binding = JidanpuMemberBinding.objects.create(
            user_id=taker["id"], jidanpu_base_url="https://sdk-member.test.invalid",
            account="synthetic-account", password="synthetic-password", enabled=True,
            hall_sync_enabled=True,
        )
        order = self.make_order(token, "sdk-member-guard")
        no = order["order_no"]
        pending_publish = self.request("POST", f"orders/{no}/publish/", token, key="sdk-guard-member-publish")
        self.assertEqual(pending_publish.json()["code"], 1000)
        pending_assign = self.request("POST", f"orders/{no}/assign/", token, {"taker_id": taker["id"]}, key="sdk-guard-member-assign")
        self.assertEqual(pending_assign.json()["code"], 1000)
        self.assertFalse(JidanpuSyncEvent.objects.filter(target_ref=no).exists())
        binding.enabled = False
        binding.save(update_fields=["enabled"])
        published = self.ok(self.request("POST", f"orders/{no}/publish/", token, key="sdk-local-publish"))
        self.assertIsNotNone(published["published_at"])
        jidanpu_member.queue_member_event(Order.objects.get(order_no=no), binding, kind=jidanpu_member.KIND_LAUNCH)
        refused = self.request("POST", f"orders/{no}/withdraw/", token, key="sdk-guard-member-withdraw")
        self.assertEqual(refused.json()["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.WAITING_GRAB)
        self.assertEqual(list(JidanpuSyncEvent.objects.filter(target_ref=no).values_list("kind", flat=True)), [jidanpu_member.KIND_LAUNCH])

    def test_deleted_main_performance_cannot_block_new_assignment(self):
        from django.utils import timezone

        token = self.token(self.app)
        first = self.create_taker(token, "former")
        next_taker = self.create_taker(token, "replacement")
        order = self.make_order(token, "sdk-legacy-deleted-perf")
        no = order["order_no"]
        Performance.objects.filter(order__order_no=no).update(user_id=first["id"], deleted_at=timezone.now())
        before = self.ok(self.request("GET", f"orders/{no}/", token))
        self.assertIsNone(before["taker_id"])
        filtered = self.ok(self.request("GET", "orders/", token, {"taker_id": first["id"]}))
        self.assertNotIn(no, [row["order_no"] for row in filtered["results"]])
        assigned = self.ok(self.request("POST", f"orders/{no}/assign/", token, {"taker_id": next_taker["id"]}, key="sdk-guard-live-assign"))
        self.assertEqual(assigned["taker_id"], next_taker["id"])

    def test_priced_split_and_child_repricing_preserve_group_total(self):
        token = self.token(self.app)
        order = self.make_order(token, "sdk-priced-split")
        no = order["order_no"]
        split = self.ok(self.request("POST", f"orders/{no}/split-peer/", token, {
            "remaining_project_text": "余下项目", "new_orders": [{"project_text": "拆出项目", "total_price": "35.00"}],
        }, key="sdk-priced-split"))
        self.assertEqual(split["source_total_price"], "65.00")
        child_no = split["created"][0]["order_no"]
        child = self.ok(self.request("PATCH", f"orders/{child_no}/", token, {"total_price": "40.00"}, key="sdk-split-child-price"))
        self.assertEqual(child["total_price"], "40.00")
        self.assertEqual(self.ok(self.request("GET", f"orders/{no}/", token))["total_price"], "60.00")

    def test_binding_path_with_nul_is_rejected_before_the_idempotency_insert(self):
        token = self.token(self.app)
        response = self.request("PUT", "taker-bindings/bad%00subject/", token, {"taker_id": 1})
        self.assertEqual(response.status_code, 404, response.content)

    def test_active_peer_sharing_binding_and_old_hall_events_prevent_sdk_write(self):
        from apps.business.models import PeerDownstreamBinding, PeerSyncEvent
        from apps.business.peer_sync import HALL_PULL_KIND, HALL_THROW_KIND

        token = self.token(self.app)
        taker = self.create_taker(token, "peer-downstream")
        binding = PeerDownstreamBinding.objects.create(
            account_id=taker["id"], peer_base_url="https://sdk-peer.test.invalid",
            token_in_hash="synthetic-peer-hash", token_out="synthetic-peer-key", hall_push_enabled=True,
        )
        order = self.make_order(token, "sdk-peer-exposure")
        no = order["order_no"]
        publish = self.request("POST", f"orders/{no}/publish/", token, key="sdk-peer-block-publish")
        self.assertEqual(publish.json()["code"], 1000)
        assign = self.request("POST", f"orders/{no}/assign/", token, {"taker_id": taker["id"]}, key="sdk-peer-block-assign")
        self.assertEqual(assign.json()["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED)
        self.assertFalse(PeerSyncEvent.objects.all().exists())
        binding.hall_push_enabled = False
        binding.save(update_fields=["hall_push_enabled"])
        self.ok(self.request("POST", f"orders/{no}/publish/", token, key="sdk-peer-local-publish"))
        refs = {"binding_role": "downstream", "binding_id": str(binding.pk), "target_type": "hall", "target_ref": "sdk-peer-exposure"}
        for index, event_fields in enumerate((
            {"direction": "out", "kind": HALL_THROW_KIND},
            {"direction": "in", "kind": HALL_PULL_KIND, "status": PeerSyncEvent.Status.DONE},
        )):
            with self.subTest(**event_fields):
                event = PeerSyncEvent.objects.create(**refs, **event_fields, idempotency_key=f"sdk-peer-evidence-{index}")
                blocked = self.request("POST", f"orders/{no}/withdraw/", token, key=f"sdk-peer-refuse-withdraw-{index}")
                self.assertEqual(blocked.json()["code"], 1000)
                self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.WAITING_GRAB)
                self.assertEqual(PeerSyncEvent.objects.count(), 1)
                event.delete()

    def test_claim_rechecks_game_scope_after_lock(self):
        from unittest.mock import patch

        from apps.business.order_writes import lock_order_row

        token = self.token(self.app)
        taker = self.create_taker(token, "changed-game")
        actor = self.bind_taker(token, taker, "subject-changed-game")
        order = self.make_order(token, "sdk-changed-game")
        no = order["order_no"]
        self.ok(self.request("POST", f"orders/{no}/publish/", token, key="sdk-changed-game-publish"))
        other_game = Game.objects.create(name="SDK 不在打手范围内的游戏")

        def moved_order(candidate):
            Order.objects.filter(pk=candidate.pk).update(game=other_game)
            return lock_order_row(candidate)

        with patch("apps.openapi.api.integration_hall.lock_order_row", side_effect=moved_order):
            denied = self.request("POST", f"taker/hall/{no}/claim/", actor, key="sdk-changed-game-claim")
        self.assertEqual(denied.json()["code"], 1002)
        self.assertIsNone(Performance.objects.get(order__order_no=no).user_id)

    def test_delete_of_cross_game_split_child_uses_workshop_authority(self):
        token = self.token(self.app)
        order = self.make_order(token, "sdk-cross-game-split")
        no = order["order_no"]
        split = self.ok(self.request("POST", f"orders/{no}/split-peer/", token, {
            "remaining_project_text": "余下项目", "new_orders": [{"project_text": "拆出项目", "total_price": "35.00"}],
        }, key="sdk-cross-game-split"))
        child_no = split["created"][0]["order_no"]
        new_game = Game.objects.create(name="SDK 子单游戏")
        Order.objects.filter(order_no=child_no).update(game=new_game)
        self.ok(self.request("POST", f"orders/{child_no}/delete/", token, key="sdk-cross-game-delete"))
        self.assertEqual(self.ok(self.request("GET", f"orders/{no}/", token))["total_price"], "100.00")
