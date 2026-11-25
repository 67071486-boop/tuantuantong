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

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType, Game, Order, OrderStatus, PeerDownstreamBinding, PeerOrderLink,
    PeerUpstreamBinding, Performance,
)
from apps.business.peer_sync import hash_token, new_token
from apps.business.roles import WORKER
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


class PeerDirectTakerRoundTripTests(TenantTestCase):
    destination_host = "peer-direct-roundtrip.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.destination, _ = provision_tenant(
            schema_name="peer_direct_roundtrip", name="测试下家工作室", domain=cls.destination_host,
        )

    def test_lost_reply_reuses_the_downstream_assignment_and_both_prices(self):
        game_name = "测试游戏甲"
        source_game = Game.objects.create(name=game_name)
        source_client = make_logged_in_client(
            username="test-direct-service", groups=("客服",),
            nodes=("order.list", "order.assign_taker", "order.submit_payout_price"),
        )
        order = Order.objects.create(
            order_no="20261117000000000067", source_order_no="SYN-DIRECT-ROUNDTRIP",
            game=source_game, total_price=Decimal("180.00"), hall_price=Decimal("0.00"),
        )
        receiver_proxy = self._worker("test-direct-studio", "测试下家账号", source_game)
        source_token, receiver_token = new_token(), new_token()
        source_binding = PeerDownstreamBinding.objects.create(
            account=receiver_proxy, peer_base_url=f"https://{self.destination_host}",
            token_in_hash=hash_token(receiver_token), token_out=source_token,
        )
        with tenant_context(self.destination):
            receiver_game = Game.objects.create(name=game_name)
            receiver_taker = self._worker("test-direct-worker-secret", "测试接手人", receiver_game)
            receiver_operator = get_user_model().objects.create_user(username="test-direct-receiver")
            PeerUpstreamBinding.objects.create(
                peer_name="测试上家工作室", peer_base_url="https://peer-source-roundtrip.test.invalid",
                peer_account=receiver_proxy.get_username(), local_base_url=f"https://{self.destination_host}",
                token_in_hash=hash_token(source_token), token_out=receiver_token,
                bound_by=receiver_operator, hall_price_percent=70, direct_taker_assignment_enabled=True,
                game_rules=[{"game_id": receiver_game.pk, "hall_price_percent": 50}],
            )

        operation_ids = []

        @contextmanager
        def route_request(method, url, *, json, headers, timeout):
            parsed = urlsplit(url)
            self.assertEqual((method, parsed.hostname), ("POST", self.destination_host))
            with tenant_context(self.destination):
                response = Client(SERVER_NAME=self.destination_host).post(
                    parsed.path, json, content_type="application/json",
                    HTTP_AUTHORIZATION=headers["Authorization"],
                )
            if json["action"] == "assign":
                self.assertEqual(response.json()["data"]["status"], "assigned")
                operation_ids.append(json["operation_id"])
                if len(operation_ids) == 1:
                    raise httpx.ReadTimeout("模拟下家成功落单后应答丢失")
            yield httpx.Response(response.status_code, content=response.content)

        url = f"/api/business/orders/{order.order_no}/peer-direct-assignment/"
        selection = {"studio_id": source_binding.pk, "account_name": "测试接手人"}
        original_ref = order.source_order_no
        with patch("apps.business.peer_client.httpx.stream", route_request):
            search = source_client.post(url + "search/", selection, content_type="application/json").json()
            self.assertEqual(search["data"]["account_name"], "测试接手人", search["msg"])
            first = source_client.post(url, {
                **selection, "expected_price": "0.00", "price": "100.00", "match_token": search["data"]["match_token"],
            }, content_type="application/json").json()
            self.assertEqual(first["code"], 1021)
            order.refresh_from_db()
            self.assertEqual((order.status, order.hall_price), (OrderStatus.UNASSIGNED, Decimal("0.00")))
            second = source_client.post(url, {
                **selection, "expected_price": "100.00",
            }, content_type="application/json").json()
            self.assertEqual(second["data"], {"studio_name": "测试下家工作室", "account_name": "测试接手人"})
            with tenant_context(self.destination):
                alias_response = Client(SERVER_NAME=self.destination_host).post(
                    "/api/business/peer-sync/inbound/events/",
                    {"events": [{
                        "kind": "order.update", "target_ref": "SYN-DIRECT-SOURCE-ALIAS",
                        "occurred_at": (timezone.now() + timedelta(seconds=1)).isoformat(),
                        "payload": {"order_no": order.order_no, "game_name": game_name, "status": "IN_PROGRESS", "hall_price": "100.00"},
                    }]}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {source_token}",
                ).json()
                self.assertEqual(alias_response["data"]["applied"], 1)
                other = Order.objects.create(order_no="20261117000000000068", status=OrderStatus.IN_PROGRESS, game=receiver_game)
                PeerOrderLink.objects.create(
                    order=other, upstream_binding=PeerUpstreamBinding.objects.get(),
                    peer_order_no="SYN-DIRECT-RENAMED",
                )
                overlap = Client(SERVER_NAME=self.destination_host).post(
                    "/api/business/peer-sync/inbound/events/",
                    {"events": [{
                        "kind": "order.update", "target_ref": "SYN-DIRECT-RENAMED",
                        "occurred_at": (timezone.now() + timedelta(seconds=2)).isoformat(),
                        "payload": {"order_no": order.order_no, "game_name": game_name, "status": "IN_PROGRESS", "hall_price": "100.00"},
                    }]}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {source_token}",
                ).json()
                self.assertEqual(overlap["data"]["applied"], 1)
                other.refresh_from_db()
                self.assertEqual(other.total_price, Decimal("0.00"))
            order.source_order_no = "SYN-DIRECT-RENAMED"
            order.save(update_fields=["source_order_no"])
            options = source_client.get(url).json()["data"]
            self.assertEqual(options["assignee"], second["data"])
            self.assertEqual(options["price"], "100.00")
            self._assert_legacy_assignee(source_client, source_game, receiver_game, receiver_proxy, receiver_taker)

        self.assertEqual(len(operation_ids), 2)
        self.assertEqual(operation_ids[0], operation_ids[1])
        order.refresh_from_db()
        self.assertEqual((order.status, order.hall_price), (OrderStatus.IN_PROGRESS, Decimal("100.00")))
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user, receiver_proxy)
        with tenant_context(self.destination):
            linked = PeerOrderLink.objects.filter(upstream_internal_order_no=order.order_no)
            self.assertEqual(linked.count(), 1)
            received = linked.get(peer_order_no=original_ref).order
            self.assertEqual(received.status, OrderStatus.IN_PROGRESS)
            self.assertEqual((received.total_price, received.hall_price), (Decimal("100.00"), Decimal("50.00")))
            self.assertEqual(Performance.objects.get(order=received, emp_type=EmpType.MAIN).user, receiver_taker)

    def _assert_legacy_assignee(self, client, game, receiver_game, proxy, taker):
        legacy = Order.objects.create(
            order_no="20261117000000000070", source_order_no="SYN-CURRENT-LEGACY-REF",
            game=game, status=OrderStatus.COMPLETED,
        )
        Performance.objects.create(order=legacy, emp_type=EmpType.MAIN, user=proxy)
        with tenant_context(self.destination):
            received = Order.objects.create(order_no="20261117000000000071", status=OrderStatus.COMPLETED, game=receiver_game)
            link = PeerOrderLink.objects.create(
                order=received, upstream_binding=PeerUpstreamBinding.objects.get(),
                peer_order_no=legacy.order_no, origin_order_no="SYN-OLD-REF",
            )
            Performance.objects.create(order=received, emp_type=EmpType.MAIN, user=taker)
        url = f"/api/business/orders/{legacy.order_no}/peer-direct-assignment/"
        self.assertEqual(client.get(url).json()["data"]["assignee"]["account_name"], "测试接手人")
        with tenant_context(self.destination):
            link.origin_order_no = legacy.order_no
            link.save(update_fields=["origin_order_no"])
        response = client.get(url).json()["data"]
        self.assertTrue(response["assignee_history_unverified"])
        self.assertIsNone(response["assignee"]["account_name"])

    @staticmethod
    def _worker(login_name, account_name, game):
        user = get_user_model().objects.create_user(username=login_name, first_name=account_name)
        group, _ = Group.objects.get_or_create(name=WORKER)
        user.groups.add(group)
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.games.add(game)
        return user
