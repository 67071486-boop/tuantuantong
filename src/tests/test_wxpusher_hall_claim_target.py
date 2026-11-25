from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import TakerWallet
from apps.business.models import (
    EmpType, Game, JidanpuUpstreamBinding, JidanpuUpstreamHallOrder, Order, OrderStatus,
    PeerHallMirror, PeerUpstreamBinding, Performance,
)
from apps.business.roles import WORKER
from tests.p5_auth_helpers import make_logged_in_client

TARGET = "/api/business/hall-claim-target/"
ORDER_NO = "20261117000000000061"
HALL_NODE = "grab_hall.take_order"


class WxPusherHallClaimTargetTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="合成游戏")
        self.browser = make_logged_in_client(groups=[WORKER], games=[self.game], nodes=[HALL_NODE])
        self.order = Order.objects.create(
            order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB, game=self.game,
            hall_price=Decimal("88.00"), project_text="完成合成任务",
        )
        self.performance = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, taker_price=Decimal("94.50"),
        )

    def test_get_loads_one_current_card_with_hall_price_without_claiming(self):
        response = self.browser.get(TARGET, {"source": "local", "ref": ORDER_NO})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        row = response.json()["data"]
        self.assertEqual((row["order_no"], row["game_name"], row["hall_price"]),
                         (ORDER_NO, "合成游戏", "94.50"))
        self.order.refresh_from_db()
        self.performance.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.performance.user_id)

    def test_exact_target_rechecks_game_permission_and_an_order_that_left_the_hall(self):
        denied = make_logged_in_client(username="other-game", groups=[WORKER], games=[], nodes=[HALL_NODE])
        query = {"source": "local", "ref": ORDER_NO}
        self.assertEqual(denied.get(TARGET, query).json()["code"], 1012)
        self.assertEqual(self.browser.get(TARGET, query).json()["code"], 0)

        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        response = self.browser.get(TARGET, query)
        self.assertEqual(response.json()["code"], 1012)
        self.assertIsNone(response.json()["data"])

    def test_anonymous_and_invalid_claim_reference_are_rejected(self):
        from django.test import Client

        query = {"source": "local", "ref": ORDER_NO}
        self.assertEqual(Client(SERVER_NAME=self.get_test_tenant_domain()).get(TARGET, query).status_code, 401)
        for bad in ("", "１２３", "1" * 20, "../1", "01", "1e3"):
            self.assertEqual(self.browser.get(TARGET, {"source": "peer", "ref": bad}).json()["code"], 1000)

    def test_both_real_mirror_sources_keep_their_target_and_current_display_price(self):
        owner = get_user_model().objects.create_user(username="same-studio-hall-binding-owner")
        upstream = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url="https://hall-upstream.example.invalid",
            account="synthetic", password="fixture", enabled=True,
        )
        peer = PeerUpstreamBinding.objects.create(
            peer_name="合成工作室", peer_base_url="https://hall-peer.example.invalid",
            peer_account="synthetic", local_base_url="https://hall-local.example.invalid",
            token_in_hash="fixture", token_out="fixture", bound_by=owner, hall_sync_enabled=True, enabled=True,
        )
        now = timezone.now()
        from_upstream = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=upstream.jidanpu_base_url, upstream_order_id="SYNTH-HALL-01",
            owner_binding=upstream, game=self.game, remote_rec_fee=Decimal("70.00"),
            hall_price_percent_at_sync=50, first_seen_at=now,
        )
        from_peer = PeerHallMirror.objects.create(
            peer_upstream_binding=peer, peer_base_url=peer.peer_base_url, peer_order_no="SYNTH-HALL-02",
            game=self.game, remote_hall_price=Decimal("70.00"), hall_price_percent_at_sync=80, first_seen_at=now,
        )
        for source, mirror, price in (("upstream", from_upstream, "35.00"), ("peer", from_peer, "56.00")):
            with self.subTest(source=source):
                result = self.browser.get(TARGET, {"source": source, "ref": str(mirror.pk)}).json()
                self.assertEqual(result["code"], 0)
                row = result["data"]
                self.assertEqual((row["origin"], row["mirror_pk"], row["hall_price"]), (source, mirror.pk, price))
                mirror.refresh_from_db()
                self.assertEqual(mirror.status, "listed")

        PeerUpstreamBinding.objects.filter(pk=peer.pk).update(hall_sync_enabled=False)
        response = self.browser.get(TARGET, {"source": "peer", "ref": str(from_peer.pk)})
        self.assertEqual(response.json()["code"], 1012)

    @override_settings(DEPOSIT_ENFORCED_APP=True)
    def test_shared_entry_only_offers_app_for_visible_order_and_sufficient_deposit(self):
        endpoint = "/api/auth/wxpusher/hall/continue"
        params = {"source": "local", "ref": ORDER_NO}
        self.assertFalse(self.browser.get(endpoint, params).json()["data"]["open_in_app"])

        worker = get_user_model().objects.get(pk=int(self.browser.session["_auth_user_id"]))
        TakerWallet.objects.create(user=worker, balance=Decimal("40.00"), frozen=Decimal("15.00"))
        self.assertTrue(self.browser.get(endpoint, params).json()["data"]["open_in_app"])
        TakerWallet.objects.filter(user=worker).update(frozen=Decimal("0.00"))
        self.assertFalse(self.browser.get(endpoint, params).json()["data"]["open_in_app"])

        denied = make_logged_in_client(username="wrong-game", groups=[WORKER], games=[], nodes=[HALL_NODE])
        outsider = get_user_model().objects.get(username="wrong-game")
        TakerWallet.objects.create(user=outsider, balance=Decimal("300.00"))
        self.assertFalse(denied.get(endpoint, params).json()["data"]["open_in_app"])
