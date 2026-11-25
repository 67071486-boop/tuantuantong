"""D4（fx-D，2026-10-28）：代练通已接手的单**不能再抛到大厅**。

业务漏洞（业务方真机反馈）：远端已被代练通打手接手（`remote_status` 12/13），
本地状态可能还停在「未分配 / 待抢单」（同步还没跑到）⇒ 一抛大厅，平台打手与内部打手
会同时接同一张单。

判据（与 A2「代练通优先」同一份实现）：
- 有**有效推送**（`ACTIVE_STATES` 且 `is_active=True`）**且** `remote_is_accepted()`
  ⇒ 中文拒绝「这张单已被代练通接单，不能再抛大厅」；
- 没有推送 / 推送只是 `Status=11`（还没人接）/ `Status=18`（⭐ H7：已下架）⇒ 允许
  （A2 第 2 条：内部先接，随后同步把平台单撤掉）；
- 两条入口都要挡：`POST …/throw-to-hall/` 与通用 `POST …/transition/`（后者目标 = 待抢单）。
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

THROW_NODES = ("order.send_to_grab_hall", "order.list")


class _HallBlockFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="hall-block-cs", groups=("客服",), nodes=THROW_NODES
        )

    def _order(self, order_no="20261028000000000901"):
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("5.00"),
            total_price=Decimal("10.00"),
        )

    def _push(self, order, *, remote_status, cancel_status=10, state=None):
        return DailiantongPush.objects.create(
            order=order,
            platform=10,
            state=state or DailiantongPush.State.SENT,
            serial_no="19000000000000000901",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order.order_no,
            remote_status=remote_status,
            remote_cancel_status=cancel_status,
            remote_taker_name="平台合成打手",
            attempts=1,
        )

    def _throw_url(self, order):
        return f"/api/business/orders/{order.order_no}/throw-to-hall/"


class ThrowToHallPlatformPriorityTests(_HallBlockFixture):
    def test_remote_accepted_12_is_rejected_with_zero_state_change(self):
        order = self._order()
        self._push(order, remote_status=12)

        body = self.client.post(self._throw_url(order)).json()

        self.assertEqual(body["code"], 1002, body)
        self.assertIn("这张单已被代练通接单", body["msg"])
        self.assertIn("不能再抛大厅", body["msg"])
        self.assertIn("平台合成打手", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_remote_pending_review_13_is_also_rejected(self):
        order = self._order()
        self._push(order, remote_status=13)

        body = self.client.post(self._throw_url(order)).json()

        self.assertEqual(body["code"], 1002, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_without_a_push_row_the_throw_is_allowed(self):
        order = self._order()

        body = self.client.post(self._throw_url(order)).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

    def test_an_unaccepted_platform_order_does_not_trip_the_priority_gate(self):
        """`Status=11`（只挂上平台、还没人接）⇒ **这道闸**不该拒（A2 第 2 条）。

        注意：它仍会被 A2 的另一道闸挡住（「已抛到代练通的单禁止手工改状态」）——
        那是既有行为，与 D4 无关。这里直接测判据本体，避免把两道闸混成一条。
        """
        from apps.business.dailiantong_push import (
            hall_throw_blocked_by_platform_denial,
        )

        order = self._order()
        self._push(order, remote_status=11)
        self.assertIsNone(hall_throw_blocked_by_platform_denial(order))

    def test_an_off_shelf_platform_order_does_not_trip_the_priority_gate(self):
        """⭐ H7（2026-10-28）：`Status=18` 已下架 = 没有打手 ⇒ 这道闸同样不拒。

        已下架与「只挂上去、还没人接」是同一族（`REMOTE_STATUS_NOT_ACCEPTED`）；
        旧实现把它算成已接手，抛大厅会被误拒成「已被代练通接单」。
        """
        from apps.business.dailiantong_push import (
            hall_throw_blocked_by_platform_denial,
        )

        order = self._order("20261028000000000903")
        self._push(order, remote_status=18)
        self.assertIsNone(hall_throw_blocked_by_platform_denial(order))

    def test_the_helper_returns_none_without_a_push(self):
        from apps.business.dailiantong_push import (
            hall_throw_blocked_by_platform_denial,
        )

        self.assertIsNone(hall_throw_blocked_by_platform_denial(self._order()))

    def test_a_failed_push_does_not_block_the_throw(self):
        order = self._order()
        self._push(order, remote_status=12, state=DailiantongPush.State.FAILED)

        body = self.client.post(self._throw_url(order)).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)


class TransitionToHallPlatformPriorityTests(_HallBlockFixture):
    """通用 `/transition/` 也是「抛大厅」的写入口 ⇒ 同一道闸必须挡得住（⛔ 留后门）。"""

    def test_transition_to_waiting_grab_is_rejected_when_remote_accepted(self):
        order = self._order()
        self._push(order, remote_status=12)

        body = self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            data='{"target": "WAITING_GRAB"}',
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 1002, body)
        self.assertIn("不能再抛大厅", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_transition_to_waiting_grab_is_allowed_without_a_push(self):
        order = self._order("20261028000000000902")

        body = self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            data='{"target": "WAITING_GRAB"}',
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)


class CancelApplicationAllowsThrowAfterRepushTests(_HallBlockFixture):
    """⭐ D8：撤销申请已提交 ⇒ 允许重发时**重新抛大厅**（连旧占位接手人一起释放）。"""

    def _order_with_taker(self, order_no="20261028000000000911"):
        order = self._order(order_no)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status"])
        taker = User.objects.create_user(username="old-platform-taker")
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker, temp_amount=Decimal("5.00")
        )
        return order

    def test_the_helper_does_not_block_when_cancel_was_applied(self):
        from apps.business.dailiantong_push import (
            hall_throw_blocked_by_platform_denial,
        )

        order = self._order()
        # ⚠️ C1之后「撤销申请已提交」要**两个证据同向**：本地标记
        # `cancel_requested_at` + 远端仍在途（`CancelStatus=11`）。只置本地标记不再是放行依据
        # （远端撤销可能已被取消 ⇒ 陈旧标记会放行第二张真钱单）。
        push = self._push(order, remote_status=12, cancel_status=11)
        push.cancel_requested_at = timezone.now()
        push.save(update_fields=["cancel_requested_at", "updated_at"])

        self.assertIsNone(hall_throw_blocked_by_platform_denial(order))

    def test_the_human_throw_endpoint_works_after_cancel_was_applied(self):
        order = self._order_with_taker()
        # ⚠️ 同上一例：C1 之后要本地标记 + 远端 `CancelStatus=11` 两个证据同向。
        push = self._push(order, remote_status=12, cancel_status=11)
        push.cancel_requested_at = timezone.now()
        push.save(update_fields=["cancel_requested_at", "updated_at"])

        body = self.client.post(self._throw_url(order)).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            "回池时旧占位接手人应被释放",
        )
