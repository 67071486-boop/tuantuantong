# tests/test_peer_p3_pure.py
from types import SimpleNamespace
from django.test import SimpleTestCase
from apps.business import peer_sync


class P3StatusTableTests(SimpleTestCase):
    def test_downstream_accepts_exactly_the_four_ruled_statuses(self):
        self.assertEqual(
            set(peer_sync.DOWNSTREAM_ACCEPTED_STATUSES),
            {"IN_PROGRESS", "ISSUE", "PAUSED", "REFUNDED"},
        )

    def test_upstream_trigger_set_is_exactly_the_four_ruled_statuses(self):
        self.assertEqual(
            set(peer_sync.UPSTREAM_TRIGGER_STATUSES),
            {"PENDING_REVIEW", "COMPLETED", "ISSUE", "PAUSED"},
        )

    def test_completed_maps_to_pending_review_upstream(self):
        # 业务方原话：「已完成只能由本系统自己更改——订单需要验收」
        self.assertEqual(peer_sync.UPSTREAM_STATUS_MAP["COMPLETED"], "PENDING_REVIEW")
        self.assertEqual(peer_sync.UPSTREAM_STATUS_MAP["PENDING_REVIEW"], "PENDING_REVIEW")
        self.assertEqual(set(peer_sync.UPSTREAM_STATUS_MAP), set(peer_sync.UPSTREAM_TRIGGER_STATUSES))
        # 映射的目标态必须都是合法的 OrderStatus 值（⛔ 不许映射出本地不认的态）
        from apps.business.models import OrderStatus
        for target in peer_sync.UPSTREAM_STATUS_MAP.values():
            self.assertIn(target, OrderStatus.values)

    def test_hall_price_is_in_the_whitelist_and_payload(self):
        self.assertIn("hall_price", peer_sync.PEER_ORDER_FIELDS)
        order = SimpleNamespace(
            order_no="SYN-1", status="IN_PROGRESS", total_price="100.00",
            hall_price="88.00", remark="", game_name=None, game=None,
            server_id=None, project_id=None, project_text="", customer_phone="",
            customer_qq="", content="", source_order_no="",
        )
        self.assertEqual(peer_sync.order_payload(order)["hall_price"], "88.00")
