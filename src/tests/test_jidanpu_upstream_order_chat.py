from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.jidanpu_upstream import record_assigned_order
from apps.business.models import Game, JidanpuUpstreamBinding, JidanpuUpstreamHallOrder, Order, OrderStatus
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


class UpstreamOrderChatTests(TenantTestCase):
    def setUp(self):
        self.enterContext(override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True))
        self.game = Game.objects.create(name="鸡蛋铺订单内聊测试游戏")
        self.counselor, counselor = self.login("up-chat-counselor", admin=True)
        self.dispatcher, _ = self.login("up-chat-dispatcher", admin=True)
        self.worker, self.worker_user = self.login("up-chat-worker", worker=True)
        self.replacement, self.replacement_user = self.login("up-chat-replacement", worker=True)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=counselor, jidanpu_base_url="https://up-order-chat.test.invalid",
            account="order-chat-test", password="fixture-pass", enabled=True,
        )

    def login(self, username, *, admin=False, worker=False, domain=None):
        client = make_logged_in_client(
            domain=domain or self.domain.domain, username=username, is_superuser=admin,
            groups=("代练员",) if worker else (),
            nodes=("grab_hall.take_order",) if worker else (),
        )
        return client, get_user_model().objects.get(username=username)

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def imported_order(self):
        order = record_assigned_order(
            self.binding, {"orderId": "20261118000000000091", "recFee": "30.00"},
            game=self.game, now=timezone.now(),
        )
        self.assertIsNotNone(order)
        return order

    def post_order(self, order, action, payload):
        return self.dispatcher.post(
            f"/api/business/orders/{order.order_no}/{action}/", payload,
            content_type="application/json",
        )

    def conversations(self, client):
        return self.data(client.get("/api/internal-chat/conversations/"))["conversations"]

    def assert_active_names(self, group, *names):
        self.assertCountEqual(
            [member["name"] for member in group["members"] if member["left_at"] is None], names,
        )

    def test_assign_opens_one_local_group_and_replaces_the_worker(self):
        order = self.imported_order()
        self.assertEqual(self.conversations(self.counselor), [])
        self.assertNotEqual(self.post_order(order, "assign", {"user_id": 999_999}).json()["code"], 0)
        self.assertEqual(self.conversations(self.counselor), [])

        with transaction.atomic():
            self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
            self.assertEqual(len(self.conversations(self.worker)), 1)
            transaction.set_rollback(True)
        self.assertEqual(self.conversations(self.worker), [])
        self.assertEqual(self.conversations(self.counselor), [])

        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        groups = self.conversations(self.worker)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["title"], f"订单-{order.order_no}")
        self.assert_active_names(group, "up-chat-counselor", "up-chat-worker")
        self.assertEqual(self.conversations(self.counselor)[0]["id"], group["id"])
        self.assertEqual(self.conversations(self.dispatcher), [])
        path = f'/api/internal-chat/conversations/{group["id"]}/messages/'
        message = self.data(self.counselor.post(path, {
            "text": "请核对订单要求", "client_id": "up-order-chat-001",
        }, content_type="application/json"))
        self.assertEqual(self.data(self.worker.get(path))["messages"], [message])

        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        self.assertEqual([row["id"] for row in self.conversations(self.worker)], [group["id"]])
        self.data(self.post_order(order, "assign", {"user_id": self.replacement_user.pk}))
        after = self.conversations(self.replacement)[0]
        self.assertEqual(after["id"], group["id"])
        self.assert_active_names(after, "up-chat-counselor", "up-chat-replacement")
        self.assertEqual(self.conversations(self.worker), [])
        self.assertNotEqual(self.worker.get(path).json()["code"], 0)
        self.assertEqual(self.data(self.replacement.get(path))["messages"], [message])

        self.data(self.post_order(order, "clear-taker", {}))
        self.assertEqual(self.conversations(self.replacement), [])
        self.assert_active_names(self.conversations(self.counselor)[0], "up-chat-counselor")

    def test_native_claim_reuses_the_group_from_local_publication(self):
        order = self.imported_order()
        self.data(self.post_order(order, "throw-to-hall", {}))
        original = self.conversations(self.dispatcher)[0]
        self.assert_active_names(original, "up-chat-dispatcher")

        self.data(self.worker.post(f"/api/business/hall/{order.order_no}/claim/"))

        groups = self.conversations(self.worker)
        self.assertEqual([row["id"] for row in groups], [original["id"]])
        self.assert_active_names(
            groups[0], "up-chat-dispatcher", "up-chat-counselor", "up-chat-worker",
        )
        self.assertEqual([row["id"] for row in self.conversations(self.counselor)], [original["id"]])

    def test_first_local_publication_adds_publisher_to_existing_group(self):
        order = self.imported_order()
        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        original = self.conversations(self.counselor)[0]
        path = f'/api/internal-chat/conversations/{original["id"]}/messages/'
        message = self.data(self.counselor.post(path, {
            "text": "撤销指派前的记录", "client_id": "up-order-first-publish",
        }, content_type="application/json"))
        self.data(self.post_order(order, "clear-taker", {}))

        self.data(self.post_order(order, "throw-to-hall", {}))
        self.assertEqual([row["id"] for row in self.conversations(self.dispatcher)], [original["id"]])
        group = self.conversations(self.dispatcher)[0]
        self.assert_active_names(group, "up-chat-counselor", "up-chat-dispatcher")
        self.assertEqual(
            next(member["order_role"] for member in group["members"] if member["name"] == "up-chat-dispatcher"),
            "publisher",
        )
        self.assertEqual(self.data(self.dispatcher.get(path))["messages"], [message])

        republisher, _ = self.login("up-chat-republisher", admin=True)
        self.data(self.post_order(order, "withdraw", {}))
        self.data(republisher.post(
            f"/api/business/orders/{order.order_no}/throw-to-hall/", {}, content_type="application/json",
        ))
        self.assertEqual(self.conversations(republisher), [])
        self.assert_active_names(self.conversations(self.dispatcher)[0], "up-chat-counselor", "up-chat-dispatcher")

    def test_inactive_sales_contact_falls_back_to_binding_owner(self):
        order = self.imported_order()
        _, inactive = self.login("up-chat-inactive-counselor", admin=True)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        order.sales_user = inactive
        order.save(update_fields=["sales_user"])

        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        groups = self.conversations(self.counselor)
        self.assertEqual(len(groups), 1)
        self.assert_active_names(groups[0], "up-chat-counselor", "up-chat-worker")
        self.assertTrue(groups[0]["can_pin_messages"])
        self.assertFalse(self.conversations(self.worker)[0]["can_pin_messages"])

    def test_without_an_eligible_contact_worker_has_a_group_without_publisher_permissions(self):
        order = self.imported_order()
        counselor = self.binding.user
        counselor.is_active = False
        counselor.save(update_fields=["is_active"])

        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        groups = self.conversations(self.worker)
        self.assertEqual(len(groups), 1)
        self.assert_active_names(groups[0], "up-chat-worker")
        self.assertEqual(groups[0]["members"][0]["order_role"], "worker")
        self.assertFalse(groups[0]["can_pin_messages"])

    def test_disabled_feature_leaves_only_the_assigned_order(self):
        order = self.imported_order()
        with self.settings(TT_CHAT_ORDER_GROUPS_ENABLED=False):
            self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        self.assertEqual(self.conversations(self.counselor), [])
        self.assertEqual(self.conversations(self.worker), [])

    def test_source_label_alone_does_not_open_a_group(self):
        order = Order.objects.create(
            order_no="20261118000000000090", game=self.game, status=OrderStatus.UNASSIGNED,
            source="鸡蛋铺上家", hall_price=Decimal("30.00"), total_price=Decimal("30.00"),
            sales_user=self.binding.user,
        )
        self.data(self.post_order(order, "assign", {"user_id": self.worker_user.pk}))
        self.assertEqual(self.conversations(self.counselor), [])
        self.assertEqual(self.conversations(self.worker), [])

    def test_counselor_session_cannot_read_another_tenants_order_group(self):
        own_order = self.imported_order()
        self.data(self.post_order(own_order, "assign", {"user_id": self.worker_user.pk}))
        own_path = f"/api/internal-chat/orders/{own_order.order_no}/messages/"
        self.assertEqual(self.data(self.counselor.get(own_path))["messages"], [])
        other_domain = "up-order-chat-second.test.local"
        with self.settings(ALLOWED_HOSTS=[self.domain.domain, other_domain]):
            other, _ = provision_tenant(
                schema_name="up_order_chat_second", name="另一订单群工作室", domain=other_domain,
            )

            def delete_other_tenant():
                with tenant_context(other):
                    other.delete(force_drop=True)

            self.addCleanup(delete_other_tenant)
            with tenant_context(other):
                game = Game.objects.create(name="第二工作室测试游戏")
                counselor_client, counselor = self.login("second-up-chat-counselor", admin=True, domain=other_domain)
                _, worker = self.login("second-up-chat-worker", worker=True, domain=other_domain)
                binding = JidanpuUpstreamBinding.objects.create(
                    user=counselor, jidanpu_base_url="https://other-up-order-chat.test.invalid",
                    account="other-order-chat-test", password="fixture-pass", enabled=True,
                )
                order = record_assigned_order(
                    binding, {"orderId": "20261118000000000096", "recFee": "30.00"},
                    game=game, now=timezone.now(),
                )
                self.assertIsNotNone(order)
                self.assertNotEqual(order.order_no, own_order.order_no)
                self.data(counselor_client.post(
                    f"/api/business/orders/{order.order_no}/assign/", {"user_id": worker.pk},
                    content_type="application/json",
                ))
                group = self.conversations(counselor_client)[0]
                path = f'/api/internal-chat/conversations/{group["id"]}/messages/'
                sent = self.data(counselor_client.post(path, {
                    "text": "第二工作室订单消息", "client_id": "other-order-chat-001",
                }, content_type="application/json"))
                order_path = f"/api/internal-chat/orders/{order.order_no}/messages/"
                self.assertEqual(self.data(counselor_client.get(order_path))["messages"], [sent])
            self.assertEqual(self.counselor.get(order_path).json()["code"], 1002)
            with tenant_context(other):
                response = self.counselor.get(order_path, SERVER_NAME=other_domain)
                self.assertEqual(response.status_code, 401, response.content)
                self.assertEqual(response.json()["msg"], "会话与当前租户不匹配")

    def mirror(self, number):
        return JidanpuUpstreamHallOrder.objects.create(
            owner_binding=self.binding, upstream_base_url=self.binding.jidanpu_base_url,
            upstream_order_id=number, game=self.game, remote_rec_fee=Decimal("30.00"),
            first_seen_at=timezone.now(), upstream_payload={"orderId": number},
        )

    def claim_mirror(self, mirror, *, code=1):
        upstream = Mock()
        upstream.last_envelope = {"code": code, "msg": ("接单成功" if code else "订单已被接走"), "data": ""}
        upstream.call.return_value = ""
        with patch("apps.business.jidanpu.claim_session", return_value=nullcontext(upstream)):
            response = self.worker.post(f"/api/business/hall/upstream/{mirror.pk}/claim/")
        upstream.call.assert_called_once()
        return response

    def test_mirror_claim_creates_the_group_after_promotion_only(self):
        rejected = self.mirror("20261118000000000092")
        self.assertNotEqual(self.claim_mirror(rejected, code=0).json()["code"], 0)
        self.assertEqual(self.conversations(self.worker), [])

        for serial, has_shadow in enumerate((False, True), start=93):
            with self.subTest(has_shadow=has_shadow):
                mirror = self.mirror(f"20261118000000000{serial:03}")
                shadow = None
                if has_shadow:
                    shadow = Order.objects.create(
                        game=self.game, upstream_hall_card=mirror,
                        status=OrderStatus.WAITING_GRAB, order_no="20261118000000000095",
                    )
                before = {row["id"] for row in self.conversations(self.worker)}
                self.assertFalse(any(row["order_no"] == getattr(shadow, "order_no", None)
                                     for row in self.conversations(self.counselor)))
                local = self.data(self.claim_mirror(mirror))["order_no"]
                mirror.refresh_from_db()
                self.assertEqual(mirror.status, mirror.Status.PROMOTED)
                if shadow is not None:
                    self.assertEqual(local, shadow.order_no)
                groups = [row for row in self.conversations(self.worker) if row["id"] not in before]
                self.assertEqual([row["order_no"] for row in groups], [local])
                self.assert_active_names(groups[0], "up-chat-counselor", "up-chat-worker")
                replay = self.worker.post(f"/api/business/hall/upstream/{mirror.pk}/claim/")
                self.assertNotEqual(replay.json()["code"], 0)
                self.assertEqual(
                    {row["id"] for row in self.conversations(self.worker)}, before | {groups[0]["id"]},
                )
