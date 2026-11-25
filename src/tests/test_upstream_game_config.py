from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    PeerHallMirror,
    PeerUpstreamBinding,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


NODE = "system.bind_upstream_system"
PEER_URL = "/api/business/peer-bindings/"
JIDANPU_URL = "/api/business/upstream-bindings/jidanpu/"


class _BindingFixtures:
    def _login(self, *, username="上家配置测试员", domain="tenant.test.com"):
        return make_logged_in_client(domain=domain, username=username, groups=("绑定测试",), nodes=(NODE,))

    def _peer(self, *, username="上家配置测试员"):
        return PeerUpstreamBinding.objects.create(
            bound_by=get_user_model().objects.get(username=username),
            peer_base_url="https://upstream-peer.example.invalid",
            local_base_url="https://local-peer.example.invalid",
            peer_name="合成上家甲",
            peer_account="synthetic-taker",
            hall_sync_enabled=True,
            hall_price_percent=70,
        )

    def _put(self, url, **payload):
        return self.client.put(url, payload, content_type="application/json").json()


class UpstreamGameConfigTests(_BindingFixtures, TenantTestCase):
    def setUp(self):
        self.client = self._login()
        self.game = Game.objects.create(name="游戏配置甲")
        self.other_game = Game.objects.create(name="游戏配置乙")
        self.jidanpu_config = {
            "jidanpu_base_url": "https://upstream-jidanpu.example.invalid",
            "account": "synthetic-taker",
            "password": "synthetic-test-password",
            "enabled": False,
        }

    def _jidanpu_create(self, **payload):
        return self.client.post(
            JIDANPU_URL, {**self.jidanpu_config, **payload}, content_type="application/json",
        ).json()

    def _rules(self):
        return [
            {"game_id": self.game.pk, "hall_price_percent": 40},
            {"game_id": self.other_game.pk, "hall_price_percent": None},
        ]

    def test_jidanpu_create_edit_omit_and_reset_rules_and_keep_old_mirror_price(self):
        self.assertEqual(self._jidanpu_create(hall_price_percent=70)["code"], 0)
        binding = JidanpuUpstreamBinding.objects.get()
        url = f"{JIDANPU_URL}{binding.pk}/"
        self.assertIsNone(self.client.get(url).json()["data"]["game_rules"])
        mirror = JidanpuUpstreamHallOrder.objects.create(
            owner_binding=binding, upstream_base_url=binding.jidanpu_base_url,
            upstream_order_id="synthetic-upstream-old", game=self.game, first_seen_at=timezone.now(),
            remote_rec_fee=Decimal("50.00"),
        )
        rules = self._rules()
        response = self._put(url, **self.jidanpu_config, game_rules=rules, hall_price_percent=85)
        self.assertEqual(response["code"], 0, response)
        self.assertEqual(response["data"]["game_rules"], rules)
        mirror.refresh_from_db()
        self.assertEqual(mirror.hall_price_percent_at_sync, 70)
        self.assertEqual(mirror.remote_rec_fee, Decimal("50.00"))
        self.assertEqual(self._put(url, **self.jidanpu_config)["data"]["game_rules"], rules)
        self.assertIsNone(self._put(url, **self.jidanpu_config, game_rules=None)["data"]["game_rules"])
        mirror.refresh_from_db()
        self.assertEqual(mirror.hall_price_percent_at_sync, 70)

    def test_jidanpu_creates_with_rules_and_preserves_member_compatibility(self):
        rules = self._rules()
        body = self._jidanpu_create(game_rules=rules)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(JidanpuUpstreamBinding.objects.get().game_rules, rules)
        member = "/api/business/member/upstream-jidanpu-binding/"
        self.assertNotIn("game_rules", self.client.get(member).json()["data"])
        self.assertEqual(self._put(member, **self.jidanpu_config, game_rules=None)["code"], 0)
        self.assertEqual(JidanpuUpstreamBinding.objects.get().game_rules, rules)

    def test_peer_config_preserves_existing_mirrors_and_no_op_does_not_backfill(self):
        binding = self._peer()
        url = f"{PEER_URL}{binding.pk}/"
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=binding, peer_base_url=binding.peer_base_url,
            peer_order_no="synthetic-upstream-old", game=self.game, first_seen_at=timezone.now(),
            remote_hall_price=Decimal("50.00"),
        )
        with (
            patch("apps.business.hall_shadow.sync_hidden_order_price") as sync_price,
            patch("apps.business.peer_inbound.forward_hall_reprice") as forward_price,
        ):
            self.assertIsNone(self._put(url, game_rules=None)["data"]["game_rules"])
            card.refresh_from_db()
            self.assertIsNone(card.hall_price_percent_at_sync)
            rules = self._rules()
            response = self._put(url, game_rules=rules, hall_price_percent=85)
            self.assertEqual(response["code"], 0, response)
            self.assertEqual(self.client.get(url).json()["data"]["game_rules"], rules)
            card.refresh_from_db()
            self.assertEqual(card.hall_price_percent_at_sync, 70)
            self.assertEqual(card.remote_hall_price, Decimal("50.00"))
            self.assertEqual(self._put(url, game_rules=rules[::-1])["code"], 0)
            self.assertIsNone(self._put(url, game_rules=None)["data"]["game_rules"])
            self.assertEqual(self._put(url, hall_price_percent=90)["code"], 0)
            sync_price.assert_not_called()
            forward_price.assert_not_called()
        card.refresh_from_db()
        self.assertEqual(card.hall_price_percent_at_sync, 70)

    def test_invalid_rules_rejected_before_writing_either_binding(self):
        self._jidanpu_create()
        jidanpu = JidanpuUpstreamBinding.objects.get()
        peer = self._peer()
        deleted = Game.objects.create(name="已删游戏", deleted_at=timezone.now())
        valid_game = {"game_id": self.game.pk, "hall_price_percent": 50}
        invalid_rules = [
            [],
            [valid_game, valid_game],
            [{"game_id": 2**63, "hall_price_percent": 20}],
            [{"game_id": True, "hall_price_percent": 20}],
            [{"game_id": self.game.pk, "hall_price_percent": True}],
            [{"game_id": self.game.pk, "hall_price_percent": 0}],
            [{"game_id": self.game.pk, "hall_price_percent": 101}],
            [{"game_id": self.game.pk, "hall_price_percent": 1.5}],
            [{"game_id": self.game.pk}],
            [{"game_id": deleted.pk, "hall_price_percent": 12}],
        ]
        for rules in invalid_rules:
            with self.subTest(rules=rules):
                self.assertEqual(self._put(f"{PEER_URL}{peer.pk}/", game_rules=rules)["code"], 1000)
                self.assertEqual(self._put(
                    f"{JIDANPU_URL}{jidanpu.pk}/", **self.jidanpu_config, game_rules=rules,
                )["code"], 1000)
        peer.refresh_from_db()
        jidanpu.refresh_from_db()
        self.assertIsNone(peer.game_rules)
        self.assertIsNone(jidanpu.game_rules)


class UpstreamGameConfigTenantIsolationTests(_BindingFixtures, TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="upstream_rules_other",
            name="按游戏配置隔离乙站",
            domain="upstream-rules-other.test.local",
        )

    def test_game_only_in_other_tenant_cannot_be_added(self):
        self.client = self._login()
        peer = self._peer()
        local_game = Game.objects.create(name="本地合成游戏")
        try:
            with tenant_context(self.other_tenant):
                Game.objects.create(name="乙站占位游戏")
                foreign = Game.objects.create(name="异站合成游戏")
        finally:
            connection.set_tenant(self.tenant)

        self.assertFalse(Game.objects.filter(name="异站合成游戏").exists())
        local_rules = [{"game_id": local_game.pk, "hall_price_percent": 50}]
        url = f"{PEER_URL}{peer.pk}/"
        self.assertEqual(self._put(url, game_rules=local_rules)["code"], 0)
        self.assertEqual(self._put(url, game_rules=[{
            "game_id": foreign.pk, "hall_price_percent": 80,
        }])["code"], 1000)
        peer.refresh_from_db()
        self.assertEqual(peer.game_rules, local_rules)
