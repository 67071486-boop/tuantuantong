from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderStatus, PeerDownstreamBinding, PeerUpstreamBinding, Performance
from apps.business.peer_sync import hash_token, new_token
from apps.business.roles import WORKER
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


class PeerDirectTakerRecoveryTests(TenantTestCase):
    receiver_host = "peer-direct-recovery.test.local"
    game_name = "测试直派恢复游戏"
    account_name = "测试接手人"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.receiver, _ = provision_tenant(
            schema_name="peer_direct_recovery", name="测试下家工作室", domain=cls.receiver_host,
        )

    def setUp(self):
        game = Game.objects.create(name=self.game_name)
        self.http = make_logged_in_client(
            username="test-direct-service", groups=("客服",),
            nodes=("order.list", "order.assign_taker", "order.submit_payout_price"),
        )
        self.order = Order.objects.create(
            order_no="20261117000000000068", source_order_no="SYN-DIRECT-RECOVERY",
            game=game, total_price=Decimal("180.00"), hall_price=Decimal("50.00"),
        )
        self.proxy = get_user_model().objects.create_user(username="test-direct-proxy")
        self.proxy_profile, _ = StaffProfile.objects.get_or_create(user=self.proxy)
        self.proxy_profile.games.add(game)
        self.source_token = new_token()
        self.receiver_token = new_token()
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.proxy, peer_base_url=f"https://{self.receiver_host}",
            token_in_hash=hash_token(self.receiver_token), token_out=self.source_token,
        )
        self.url = f"/api/business/orders/{self.order.order_no}/peer-direct-assignment/"
        self.selection = {"studio_id": self.binding.pk, "account_name": self.account_name}
        self.requests = []

    @patch("apps.business.peer_client.direct_taker_request")
    def test_source_commission_failure_prevents_sending_or_staging(self, request):
        request.return_value = {"enabled": True, "name": "测试下家"}
        self.proxy_profile.commission_mode = CommissionMode.FIXED
        self.proxy_profile.save(update_fields=["commission_mode"])

        response = self.confirm(match_token="synthetic-search-match", price="100.00", expected_price="50.00")
        self.assertIn("提成模式", response["msg"])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["availability"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual((self.order.status, self.order.hall_price), (OrderStatus.UNASSIGNED, Decimal("50.00")))

    def test_rebind_recovers_exact_receipt_and_rejects_changed_price(self):
        with tenant_context(self.receiver):
            game = Game.objects.create(name=self.game_name)
            worker = get_user_model().objects.create_user(username="test-recovery-worker", first_name=self.account_name)
            group, _ = Group.objects.get_or_create(name=WORKER)
            worker.groups.add(group)
            profile, _ = StaffProfile.objects.get_or_create(user=worker)
            profile.games.add(game)
            operator = get_user_model().objects.create_user(username="test-recovery-operator")
            upstream = {
                "peer_name": "测试上家工作室", "peer_base_url": "https://peer-source-recovery.test.invalid",
                "peer_account": self.proxy.get_username(), "local_base_url": f"https://{self.receiver_host}",
                "bound_by": operator, "direct_taker_assignment_enabled": True,
            }
            old_binding = PeerUpstreamBinding.objects.create(
                **upstream, token_in_hash=hash_token(self.source_token), token_out=self.receiver_token,
            )

        with patch("apps.business.peer_client.httpx.stream", self.route_request):
            match = self.http.post(self.url + "search/", self.selection, content_type="application/json").json()
            first = self.confirm(match_token=match["data"]["match_token"], price="100.00", expected_price="50.00")
            self.assertEqual(first["code"], 1021)
            self.order.refresh_from_db()
            pending = self.order.peer_direct_pending
            original_url = self.binding.peer_base_url
            self.binding.peer_base_url = "https://different-studio.test.invalid"
            self.binding.token_out = new_token()
            self.binding.save(update_fields=["peer_base_url", "token_out"])
            self.assertEqual(self.confirm(expected_price="100.00")["code"], 1021)
            self.assertEqual([request["action"] for request in self.requests], ["search", "availability", "assign"])
            self.order.refresh_from_db()
            self.assertEqual(self.order.peer_direct_pending, pending)

            with tenant_context(self.receiver):
                old_binding.deleted_at = timezone.now()
                old_binding.save(update_fields=["deleted_at"])
                PeerUpstreamBinding.objects.create(
                    **upstream, token_in_hash=hash_token(self.binding.token_out), token_out=new_token(),
                )
            self.binding.peer_base_url = original_url
            self.binding.save(update_fields=["peer_base_url"])
            response = self.confirm(expected_price="100.00")
            self.assertEqual(response["code"], 0, response["msg"])
            receipt_request = self.requests[-1]
            self.assertEqual(receipt_request, {
                "action": "receipt", "target_ref": pending["target_ref"],
                "operation_id": pending["operation_id"], "account_name": self.account_name,
                "binding_token_hash": hash_token(self.source_token),
            })
            with tenant_context(self.receiver):
                other_token = new_token()
                PeerUpstreamBinding.objects.create(
                    **{**upstream, "peer_base_url": "https://other-source-recovery.test.invalid"},
                    token_in_hash=hash_token(other_token), token_out=new_token(),
                )
                response = Client(SERVER_NAME=self.receiver_host).post(
                    "/api/business/peer-sync/direct-taker/", receipt_request, content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {other_token}",
                ).json()
                self.assertEqual(response["code"], 0)
                self.assertIsNone(response["data"])
            self.assertIn("本次发单价未生效", self.confirm(price="101.00")["msg"])
            self.assertIn("本次发单价未生效", self.confirm(expected_price="101.00")["msg"])
            self.assertEqual(self.confirm(price="100.00")["code"], 0)
            self.assertEqual(len(self.requests), 4)

            # 模拟普通重派给同一工作室，旧直派回执已经不对应当前的接手。
            current = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
            current.claimed_at += timedelta(seconds=1)
            current.save(update_fields=["claimed_at"])
            self.assertNotEqual(self.confirm(price="100.00")["code"], 0)

        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual((self.order.status, self.order.hall_price), (OrderStatus.IN_PROGRESS, Decimal("100.00")))
        with tenant_context(self.receiver):
            self.assertEqual(Order.objects.count(), 1)

    def confirm(self, **payload):
        return self.http.post(self.url, {**self.selection, **payload}, content_type="application/json").json()

    @contextmanager
    def route_request(self, method, url, *, json, headers, timeout):
        parsed = urlsplit(url)
        self.assertEqual((method, parsed.hostname), ("POST", self.receiver_host))
        self.requests.append(json)
        with tenant_context(self.receiver):
            response = Client(SERVER_NAME=self.receiver_host).post(
                parsed.path, json, content_type="application/json", HTTP_AUTHORIZATION=headers["Authorization"],
            )
        if json["action"] == "assign":
            self.assertEqual(response.json()["data"]["status"], "assigned")
            raise httpx.ReadTimeout("模拟下家落单后应答丢失")
        yield httpx.Response(response.status_code, content=response.content)
