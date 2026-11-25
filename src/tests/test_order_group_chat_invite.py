import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType, Game, Order, OrderStatus, PeerHallMirror, PeerOrderLink, PeerUpstreamBinding, Performance,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import make_logged_in_client


class LateOrderGroupInvitationTests(TenantTestCase):
    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_late_invite_preserves_newer_order_data_and_joins_current_worker(self):
        game = Game.objects.create(name="订单群补邀测试")
        domain = self.domain.domain
        acceptor = make_logged_in_client(domain=domain, username="late-acceptor")
        worker = make_logged_in_client(domain=domain, username="late-current-worker")
        binder = make_logged_in_client(domain=domain, username="late-peer-binder")
        users = get_user_model().objects
        token = new_token()
        source_no = "20261116000000000999"
        now = timezone.now()
        binding = PeerUpstreamBinding.objects.create(
            bound_by=users.get(username="late-peer-binder"), peer_name="测试源头", peer_base_url="https://late-source.test",
            peer_account="late-machine", local_base_url=f"https://{domain}",
            token_in_hash=hash_token(token), token_out=new_token(),
        )
        order = Order.objects.create(
            order_no="20261116000000000998", game=game, status=OrderStatus.IN_PROGRESS,
            remark="下家的较新备注",
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, user=users.get(username="late-current-worker"))
        PeerOrderLink.objects.create(
            order=order, upstream_binding=binding, peer_order_no=source_no,
            last_applied_at=now, local_action_at=now,
        )
        PeerHallMirror.objects.create(
            peer_upstream_binding=binding, peer_base_url=binding.peer_base_url,
            peer_order_no=source_no, status=PeerHallMirror.Status.PROMOTED,
            promoted_order=order, claiming_by=users.get(username="late-acceptor"), first_seen_at=now,
        )
        event = {
            "kind": "order.assign", "target_ref": source_no, "target_type": "order",
            "occurred_at": (now - timedelta(seconds=1)).isoformat(),
            "payload": {
                "status": "IN_PROGRESS", "game_name": game.name, "remark": "上家的较早备注",
                "chat_group": {"group_id": str(uuid.uuid4()), "order_no": source_no, "publisher_name": "发单客服"},
            },
        }
        response = Client(SERVER_NAME=domain).post(
            "/api/business/peer-sync/inbound/events/", {"events": [event]},
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        order.refresh_from_db()
        self.assertEqual(order.remark, "下家的较新备注")
        for viewer in [worker, acceptor]:
            chat = viewer.get("/api/internal-chat/conversations/").json()
            self.assertEqual(chat["code"], 0, chat)
            groups = chat["data"]["conversations"]
            self.assertEqual([row["title"] for row in groups], [f"订单-{source_no}"])
            self.assertEqual({row["name"] for row in groups[0]["members"]}, {
                "发单客服", "late-acceptor", "late-current-worker",
            })
        self.assertEqual(binder.get("/api/internal-chat/conversations/").json()["data"]["conversations"], [])
