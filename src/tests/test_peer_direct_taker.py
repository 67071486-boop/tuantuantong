from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import peer_inbound
from apps.business.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import PeerRejected, PeerUnreachable
from apps.business.peer_dispatch import enqueue_upstream_status
from apps.business.peer_sync import hash_token, new_token
from apps.business.roles import WORKER
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


PEER_PATH = "/api/business/peer-sync/direct-taker/"
DOMAIN = "tenant.test.com"


class PeerDirectTakerInboundTests(TenantTestCase):
    def setUp(self):
        self.http = Client(SERVER_NAME=DOMAIN)
        self.game = Game.objects.create(name="测试游戏")
        self.token = new_token()
        self.user = get_user_model().objects.create_user(username="direct-operator")
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家",
            peer_base_url="https://source.test.invalid",
            peer_account="source-binding-account",
            local_base_url="https://receiver.test.invalid",
            bound_by=self.user,
            token_in_hash=hash_token(self.token),
            token_out=new_token(),
            hall_price_percent=50,
        )
        self.group, _ = Group.objects.get_or_create(name=WORKER)

    def worker(self, username="taker-login", name="测试飞鸟"):
        taker = get_user_model().objects.create_user(username=username, first_name=name)
        taker.groups.add(self.group)
        profile, _ = StaffProfile.objects.get_or_create(user=taker)
        profile.games.add(self.game)
        return taker

    def post(self, action, **values):
        return self.http.post(
            PEER_PATH, {"action": action, **values}, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        ).json()

    def match_taker(self):
        found = self.post("search", game_name=self.game.name, account_name="测试飞鸟")
        self.assertEqual(found["code"], 0)
        self.assertEqual(found["data"]["account_name"], "测试飞鸟")
        self.assertEqual(len(found["data"]["match_token"]), 64)
        return found["data"]["match_token"]

    def assign(self, operation_id, match_token, account_name="测试飞鸟"):
        return self.post(
            "assign",
            operation_id=str(operation_id),
            target_ref="SYN-DIRECT-NEW",
            account_name=account_name,
            match_token=match_token,
            payload={
                "order_no": "SYN-LOCAL-NEW",
                "status": "UNASSIGNED",
                "game_name": self.game.name,
                "hall_price": "100.00",
            },
        )

    def test_receiver_is_disabled_by_default(self):
        result = self.post("availability")
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["data"]["enabled"], False)

    def test_direct_assignment_freezes_result_and_calculates_both_prices(self):
        self.binding.direct_taker_assignment_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        taker = self.worker()
        operation = uuid4()
        match_token = self.match_taker()
        result = self.assign(operation, match_token)
        self.assertEqual(
            result["data"],
            {"status": "assigned", "operation_id": str(operation), "account_name": "测试飞鸟"},
        )
        link = PeerOrderLink.objects.get(upstream_binding=self.binding, peer_order_no="SYN-DIRECT-NEW")
        self.assertEqual(link.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(link.order.total_price, Decimal("100"))
        self.assertEqual(link.order.hall_price, Decimal("50"))
        self.assertEqual(
            Performance.objects.get(order=link.order, emp_type=EmpType.MAIN).user_id, taker.pk
        )
        link.order.status = OrderStatus.ISSUE
        link.order.save(update_fields=["status", "updated_at"])
        event = enqueue_upstream_status(link.order)
        self.assertEqual(event.payload, {
            "status": "ISSUE",
            "direct_assignment": {"operation_id": str(operation), "order_no": "SYN-LOCAL-NEW"},
        })
        self.binding.direct_taker_assignment_enabled = False
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        PeerSyncEvent.objects.filter(idempotency_key=str(operation)).update(
            created_at=timezone.now() - timedelta(days=91),
        )
        call_command("prune_sync_events", "--commit", "--tenant", "test", stdout=StringIO())
        self.assertEqual(self.assign(operation, match_token), result)
        self.assertEqual(Order.objects.count(), 1)


    def test_search_matches_account_name_only_and_duplicate_names_prevent_landing(self):
        self.binding.direct_taker_assignment_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        self.worker()
        match_token = self.match_taker()
        self.assertNotEqual(
            self.post("search", game_name=self.game.name, account_name="taker-login")["code"], 0,
        )
        self.worker(username="different-login")
        result = self.assign(uuid4(), match_token)
        self.assertEqual(result["data"]["status"], "rejected")
        self.assertIn("同名", result["data"]["message"])
        self.assertFalse(Order.objects.exists())

    def test_direct_assignment_wins_over_a_pending_downstream_hall_claim(self):
        self.binding.direct_taker_assignment_enabled = True
        self.binding.hall_sync_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled", "hall_sync_enabled"])
        taker = self.worker()
        competing_worker = self.worker(username="other-worker", name="测试另一位")
        now = timezone.now()
        mirror = PeerHallMirror.objects.create(
            peer_upstream_binding=self.binding, peer_base_url=self.binding.peer_base_url,
            peer_order_no="SYN-DIRECT-NEW", game=self.game, status=PeerHallMirror.Status.CLAIMING,
            claiming_by=competing_worker, claiming_at=now, hall_price_percent_at_sync=70,
            first_seen_at=now, last_seen_at=now,
        )
        result = self.assign(uuid4(), self.match_taker())
        self.assertEqual(result["data"]["status"], "assigned", result)
        mirror.refresh_from_db()
        self.assertIsNotNone(mirror.gone_at)
        link = PeerOrderLink.objects.get(upstream_binding=self.binding, peer_order_no=mirror.peer_order_no)
        self.assertEqual(link.order.hall_price, Decimal("50"))
        self.assertEqual(Performance.objects.get(order=link.order, emp_type=EmpType.MAIN).user, taker)

        peer_inbound.apply_events([{
            "kind": "hall.claim_refused", "target_ref": mirror.peer_order_no,
            "payload": {"reason": "订单正在直派"},
        }], binding=self.binding)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, PeerHallMirror.Status.GONE)
        self.assertIsNone(mirror.promoted_order_id)

    def test_partial_search_returns_only_unique_eligible_account_names(self):
        self.worker(username="valid-partial-user", name="测试68432")
        self.worker(username="first-duplicate", name="同名68432")
        self.worker(username="second-duplicate", name="同名68432")
        no_game = self.worker(username="no-game", name="没游戏68432")
        no_game.staff_profile.games.clear()
        get_user_model().objects.create_user(username="not-a-worker", first_name="客服68432")
        query = {"game_name": self.game.name, "query": "68432"}
        self.assertNotEqual(self.post("search_matches", **query)["code"], 0)
        self.binding.direct_taker_assignment_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        result = self.post("search_matches", **query)
        self.assertEqual(result["code"], 0, result)
        [item] = result["data"]["items"]
        self.assertEqual(item["account_name"], "测试68432")
        self.assertEqual(self.assign(uuid4(), item["match_token"], item["account_name"])["data"]["status"], "assigned")

    def test_partial_search_keeps_exact_match_ahead_of_the_result_limit(self):
        self.binding.direct_taker_assignment_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        self.worker(username="exact-account", name="z68432")
        for index in range(20):
            self.worker(username=f"similar-account-{index}", name=f"a{index:02}-z68432")
        response = self.post("search_matches", game_name=self.game.name, query="z68432")
        self.assertEqual(response["code"], 0, response)
        self.assertEqual(len(response["data"]["items"]), 20)
        self.assertEqual(response["data"]["items"][0]["account_name"], "z68432")

    def test_search_proof_rejects_name_transferred_to_a_different_worker(self):
        self.binding.direct_taker_assignment_enabled = True
        self.binding.save(update_fields=["direct_taker_assignment_enabled"])
        original = self.worker()
        match_token = self.match_taker()
        original.first_name = "另一个名称"
        original.save(update_fields=["first_name"])
        self.worker(username="replacement-login")
        result = self.assign(uuid4(), match_token)
        self.assertEqual(result["data"]["status"], "rejected")
        self.assertIn("重新搜索", result["data"]["message"])
        self.assertFalse(Order.objects.exists())

    def test_old_order_read_uses_its_binding_and_does_not_reveal_logins(self):
        taker = self.worker()
        order = Order.objects.create(
            order_no="SYN-PREVIOUS-ORDER", game=self.game, status=OrderStatus.COMPLETED,
            total_price=Decimal("100"), hall_price=Decimal("50"),
        )
        PeerOrderLink.objects.create(
            order=order, upstream_binding=self.binding, peer_order_no="SYN-OLD-BUILTIN",
            origin_order_no="SYN-OLD-SOURCE",
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, user=taker)
        result = self.post("assignee", target_ref="SYN-OLD-SOURCE", order_no="SYN-OLD-BUILTIN")
        self.assertEqual(result["data"]["account_name"], "测试飞鸟")
        taker.first_name = ""
        taker.save(update_fields=["first_name"])
        result = self.post("assignee", target_ref="SYN-OLD-SOURCE", order_no="SYN-OLD-BUILTIN")
        self.assertIsNone(result["data"]["account_name"])
        other_token = new_token()
        PeerUpstreamBinding.objects.create(
            peer_name="另一家", peer_base_url="https://other.test.invalid",
            peer_account="other-source-account", local_base_url="https://receiver.test.invalid",
            bound_by=self.user, token_in_hash=hash_token(other_token), token_out=new_token(),
        )
        self.token = other_token
        result = self.post("assignee", target_ref="SYN-OLD-SOURCE", order_no="SYN-OLD-BUILTIN")
        self.assertIsNone(result["data"])



class PeerDirectTakerIsolationTests(TenantTestCase):
    OTHER_DOMAIN = "peer-direct-other.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        provision_tenant(
            schema_name="peer_direct_other", name="对照工作室", domain=cls.OTHER_DOMAIN,
        )

    def test_two_tenants_cannot_use_each_others_tokens(self):
        user = get_user_model().objects.create_user(username="direct-operator")
        token = new_token()
        PeerUpstreamBinding.objects.create(
            peer_name="测试上家", peer_base_url="https://source.test.invalid",
            peer_account="source-binding-account", local_base_url="https://receiver.test.invalid",
            bound_by=user, token_in_hash=hash_token(token), token_out=new_token(),
            direct_taker_assignment_enabled=True,
        )
        game = Game.objects.create(name="测试游戏")
        worker = get_user_model().objects.create_user(username="direct-worker", first_name="测试68432")
        worker.groups.add(Group.objects.get_or_create(name=WORKER)[0])
        profile, _ = StaffProfile.objects.get_or_create(user=worker)
        profile.games.add(game)
        for domain, expected in [(DOMAIN, 200), (self.OTHER_DOMAIN, 401)]:
            try:
                response = Client(SERVER_NAME=domain).post(
                    PEER_PATH, {"action": "search_matches", "game_name": game.name, "query": "68432"},
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {token}",
                )
                self.assertEqual(response.status_code, expected)
                if expected == 200:
                    [match] = response.json()["data"]["items"]
                    self.assertEqual(match["account_name"], worker.first_name)
            finally:
                connection.set_tenant(self.tenant)


class PeerDirectTakerOutboundTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏")
        self.http = make_logged_in_client(
            username="direct-sender", groups=("测试客服",), nodes=("order.assign_taker", "order.submit_payout_price"),
        )
        self.order = Order.objects.create(
            order_no="20261117000000000001",
            source_order_no="SYN-NEW-DIRECT",
            game=self.game,
            total_price=Decimal("150"),
            hall_price=Decimal("0"),
        )
        self.studio_user = get_user_model().objects.create_user(
            username="receiver-studio", first_name="下家工作室",
        )
        profile, _ = StaffProfile.objects.get_or_create(user=self.studio_user)
        profile.games.add(self.game)
        self.status_token = new_token()
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.studio_user, peer_base_url="https://receiver.test.invalid",
            token_in_hash=hash_token(self.status_token), token_out=new_token(),
        )
        self.path = f"/api/business/orders/{self.order.order_no}/peer-direct-assignment/"
        self.payload = {"studio_id": self.binding.pk, "account_name": "测试飞鸟"}
        self.selection = {**self.payload, "match_token": "fixture-match-token"}

    def post(self, path, payload):
        return self.http.post(path, payload, content_type="application/json").json()

    @staticmethod
    def availability(binding, action, **kwargs):
        if action == "availability":
            return {"enabled": True, "name": "下家工作室"}
        raise AssertionError(f"unexpected {action}")

    @patch("apps.business.peer_client.direct_taker_request")
    def test_hall_order_leaves_only_after_direct_assignment_succeeds(self, send):
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.hall_price = Decimal("88")
        self.order.save(update_fields=["status", "hall_price"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)
        taker = make_logged_in_client(username="parallel-taker", groups=(WORKER,), nodes=("grab_hall.take_order",))

        def reply(binding, action, **kwargs):
            if action == "assign":
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
                self.assertIsNotNone(self.order.peer_direct_pending)
                self.assertEqual(kwargs["payload"]["status"], OrderStatus.UNASSIGNED)
                self.assertFalse(PeerSyncEvent.objects.filter(kind="hall.take").exists())
                claim = taker.post(
                    f"/api/business/hall/{self.order.order_no}/claim/", {}, content_type="application/json",
                ).json()
                self.assertIn("正在确认", claim["msg"])
                self.assertIsNone(Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id)
                return dict(status="assigned", operation_id=kwargs["operation_id"], account_name=kwargs["account_name"])
            if action == "search":
                return {"account_name": kwargs["account_name"], "match_token": "fixture-match-token"}
            return self.availability(binding, action, **kwargs)

        send.side_effect = reply
        options = self.http.get(self.path).json()
        [studio] = options["data"]["studios"]
        self.assertEqual(studio["id"], self.binding.pk)
        selection = {**self.payload, "studio_id": studio["id"]}
        match = self.post(self.path + "search/", selection)["data"]
        result = self.post(self.path, {**selection, "match_token": match["match_token"]})
        self.assertEqual(result["code"], 0, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(self.order.peer_direct_pending)
        take = PeerSyncEvent.objects.get(kind="hall.take", binding_id=str(self.binding.pk))
        self.assertEqual(take.meta["leave_reason"], "assigned")

    @patch("apps.business.peer_client.direct_taker_request")
    def test_downstream_rejection_leaves_waiting_order_available_to_claim(self, send):
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.hall_price = Decimal("88")
        self.order.save(update_fields=["status", "hall_price"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)

        def reply(binding, action, **kwargs):
            if action == "assign":
                return {"status": "rejected", "operation_id": kwargs["operation_id"], "message": "授权已关闭"}
            return self.availability(binding, action, **kwargs)

        send.side_effect = reply
        result = self.post(self.path, self.selection)
        self.assertEqual(result["msg"], "授权已关闭")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(PeerSyncEvent.objects.filter(kind="hall.take").exists())

        taker = make_logged_in_client(username="after-rejection", groups=(WORKER,), nodes=("grab_hall.take_order",))
        claim = taker.post(
            f"/api/business/hall/{self.order.order_no}/claim/", {}, content_type="application/json",
        ).json()
        self.assertEqual(claim["code"], 0, claim)

    @patch("apps.business.peer_client.direct_taker_request")
    def test_draft_search_scopes_by_authenticated_game_and_keeps_legacy_search(self, send):
        client = make_logged_in_client(
            username="kefu-draft", groups=("测试录单",), games=[self.game],
            nodes=("order.add_edit", "order.assign_taker"),
        )

        def reply(binding, action, **kwargs):
            if action == "search_matches":
                self.assertEqual(kwargs, {"game_name": self.game.name, "query": "68432"})
                return {"items": [{"account_name": "测试68432", "match_token": "fixture-match-token"}]}
            if action == "search":
                return {"account_name": kwargs["account_name"], "match_token": "fixture-match-token"}
            return self.availability(binding, action, **kwargs)

        send.side_effect = reply
        url = "/api/business/orders/peer-direct-assignment/"
        other = Game.objects.create(name="其他游戏")
        query = {"studio_id": self.binding.pk, "query": "68432"}
        forbidden = client.post(url + "search/", {"game_id": other.pk, **query}, content_type="application/json")
        self.assertEqual(forbidden.json()["msg"], "记录不存在或已被删除")
        send.assert_not_called()
        options = client.get(url + "options/", {"game_id": self.game.pk}).json()
        self.assertEqual(options["data"]["studios"], [{"id": self.binding.pk, "name": "下家工作室"}])
        result = client.post(url + "search/", {"game_id": self.game.pk, **query}, content_type="application/json").json()
        self.assertEqual(result["data"]["items"][0]["account_name"], "测试68432")
        exact = client.post(url + "search/", {"game_id": self.game.pk, **self.payload}, content_type="application/json").json()
        self.assertEqual(exact["data"]["account_name"], self.payload["account_name"])
        order_result = self.post(self.path + "search/", query)
        self.assertEqual(order_result["data"], result["data"])

    @patch("apps.business.peer_client.direct_taker_request")
    def test_legacy_downstream_can_still_be_searched_by_full_account_name(self, send):
        account_name = self.payload["account_name"]
        send.side_effect = [
            PeerRejected('"search_matches" is not a valid choice.', code=ErrorCode.VALIDATION),
            {"account_name": account_name, "match_token": "fixture-match-token"},
        ]
        result = self.post(self.path + "search/", {"studio_id": self.binding.pk, "query": account_name})
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["data"], {"items": [
            {"account_name": account_name, "match_token": "fixture-match-token"},
        ]})
        self.assertEqual(send.call_args_list[0].args[1], "search_matches")
        self.assertEqual(send.call_args.args[1], "search")
        self.assertEqual(send.call_args.kwargs, {"game_name": self.game.name, "account_name": account_name})

    @patch("apps.business.peer_client.direct_taker_request")
    def test_edit_options_follow_the_selected_game_and_keep_the_order_price(self, send):
        other = Game.objects.create(name="下家开放的游戏")
        self.studio_user.staff_profile.games.set([other])
        client = make_logged_in_client(
            username="kefu-edit", groups=("测试编辑",), games=[self.game, other],
            nodes=("order.submit_edit", "order.assign_taker"),
        )
        original = client.get(self.path).json()
        self.assertEqual(original["data"]["studios"], [])
        send.assert_not_called()
        send.side_effect = self.availability
        changed = client.get(self.path, {"game_id": other.pk}).json()
        self.assertEqual(changed["data"]["studios"], [{"id": self.binding.pk, "name": "下家工作室"}])
        self.assertEqual(changed["data"]["price"], original["data"]["price"])
        send.assert_called_once_with(self.binding, "availability", game_name=other.name)

    @patch("apps.business.peer_client.direct_taker_request")
    def test_explicit_rejection_keeps_order_unassigned_and_old_price(self, send):
        def reply(binding, action, **kwargs):
            if action == "assign":
                return {
                    "status": "rejected", "operation_id": kwargs["operation_id"],
                    "message": "授权已关闭",
                }
            return self.availability(binding, action, **kwargs)

        send.side_effect = reply
        response = self.post(self.path, {**self.selection, "expected_price": "0", "price": "100"})
        self.assertEqual(response["msg"], "授权已关闭")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.hall_price, Decimal("0"))
        self.assertFalse(Performance.objects.filter(order=self.order, user__isnull=False).exists())

    @patch("apps.business.peer_client.direct_taker_request")
    def test_timeout_blocks_ordinary_assign_until_same_operation_can_complete(self, send):
        operation_ids = []

        def reply(binding, action, **kwargs):
            if action != "assign":
                return self.availability(binding, action, **kwargs)
            self.assertEqual(kwargs["payload"]["hall_price"], "100.00")
            self.assertEqual(kwargs["match_token"], "fixture-match-token")
            self.assertNotIn("total_price", kwargs["payload"])
            operation_ids.append(kwargs["operation_id"])
            if len(operation_ids) == 1:
                raise PeerUnreachable("模拟下家完成事务后应答丢失")
            return {
                "status": "assigned", "operation_id": kwargs["operation_id"],
                "account_name": kwargs["account_name"],
            }

        send.side_effect = reply
        result = self.post(self.path, {**self.selection, "expected_price": "0", "price": "100"})
        self.assertEqual(result["code"], 1021)
        pending = self.http.get(self.path).json()["data"]["pending"]
        self.assertEqual(pending["price"], "100.00")
        self.assertEqual(pending["studio_name"], "下家工作室")
        self.assertNotIn("payload", pending)
        another = get_user_model().objects.create_user(username="other-taker")
        profile, _ = StaffProfile.objects.get_or_create(user=another)
        profile.games.add(self.game)
        regular_assignment = self.post(
            f"/api/business/orders/{self.order.order_no}/assign/", {"user_id": another.pk},
        )
        self.assertIn("正在确认", regular_assignment["msg"])
        result = self.post(self.path, {**self.payload, "account_name": "另一个打手", "expected_price": "100"})
        self.assertNotEqual(result["code"], 0)
        self.assertEqual(len(operation_ids), 1)
        result = self.post(self.path, {**self.payload, "expected_price": "100"})
        self.assertEqual(result["data"], {"studio_name": "下家工作室", "account_name": "测试飞鸟"})
        self.assertEqual(len(set(operation_ids)), 1)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.order.hall_price, Decimal("100"))
        self.assertEqual(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user_id,
            self.studio_user.pk,
        )

    @patch("apps.business.peer_client.direct_taker_request")
    def test_penalty_price_override_is_rejected_before_downstream_assign(self, send):
        self.order.hall_price = Decimal("-30.00")
        self.order.penalty_approval = Approval.objects.create(
            kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED,
            target=self.studio_user, amount=Decimal("30.00"),
        )
        self.order.save(update_fields=["hall_price", "penalty_approval"])
        send.side_effect = self.availability
        result = self.post(self.path, {**self.selection, "expected_price": "-30.00", "price": "-25.00"})
        self.assertIn("罚款金额已由审批确定", result["msg"])
        self.assertEqual([call.args[1] for call in send.call_args_list], ["availability"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)

    @patch("apps.business.peer_client.direct_taker_request")
    def test_stale_price_confirmation_never_sends_the_order(self, send):
        send.side_effect = self.availability
        result = self.post(self.path, {**self.selection, "expected_price": "50", "price": "100"})
        self.assertIn("发单价已变化", result["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)


    def deliver_status(self, operation_id, status, event_at):
        return Client(SERVER_NAME=DOMAIN).post(
            "/api/business/peer-sync/inbound/events/",
            {"events": [{
                "kind": "order.update",
                "target_ref": self.order.source_order_no,
                "occurred_at": event_at.isoformat(),
                "payload": {
                    "status": status,
                    "direct_assignment": {"operation_id": operation_id, "order_no": self.order.order_no},
                },
            }]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.status_token}",
        ).json()["data"]

    @patch("apps.business.peer_client.direct_taker_request")
    def test_progress_during_and_after_confirmation_retains_the_original_timestamps(self, send):
        operation_id = None

        def reply(binding, action, **kwargs):
            nonlocal operation_id
            if action != "assign":
                return self.availability(binding, action, **kwargs)
            if operation_id is None:
                operation_id = kwargs["operation_id"]
                raise PeerUnreachable("下家成功后应答丢失")
            return {"status": "assigned", "operation_id": operation_id, "account_name": kwargs["account_name"]}

        send.side_effect = reply
        result = self.post(self.path, {**self.selection, "expected_price": "0", "price": "100"})
        self.assertEqual(result["code"], 1021)
        earlier = timezone.now() - timedelta(seconds=3)
        later = earlier + timedelta(seconds=1)
        still_later = later + timedelta(seconds=1)
        forged = self.deliver_status(str(uuid4()), "PAUSED", earlier)
        self.assertEqual(forged["skipped_details"][0]["code"], "not_your_order")
        self.assertEqual(self.deliver_status(operation_id, "ISSUE", later)["applied"], 1)
        self.assertEqual(self.deliver_status(operation_id, "PAUSED", earlier)["applied"], 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self.post(self.path, {**self.payload, "expected_price": "100"})["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)
        self.assertFalse(PeerSyncEvent.objects.filter(direction="out", kind="order.update").exists())
        self.assertLess(still_later, self.order.updated_at)
        self.assertEqual(self.deliver_status(operation_id, "PENDING_REVIEW", still_later)["applied"], 1)
        self.assertEqual(self.deliver_status(operation_id, "PAUSED", earlier)["skipped_details"][0]["code"], "stale")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
