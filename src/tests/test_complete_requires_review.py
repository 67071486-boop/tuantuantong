"""「不能直接改已完成」（spec §2.3 / F3；分支 `feat/perf-dialog-sync-and-complete-gate`）。

四条判据：

1. `PENDING_REVIEW → COMPLETED` 仍成功（通用 `/transition/` 与 `/review/` 都不回归）；
2. 六条被删的入边（`UNASSIGNED` / `WAITING_GRAB` / `IN_PROGRESS` / `ISSUE` / `PAUSED` /
   `REFUNDED` → `COMPLETED`）各自回业务码、**状态不变**、零留痕，且文案是业务方给的
   **逐字**原文 `必须先把订单状态改成待验收才能改成已完成`；
3. **读侧保留那一项**（业务方要求「下拉里已完成也要有」）：当前不是 `COMPLETED` 时
   `allowed_transitions` **仍列出** `COMPLETED`，并由 `completed_requires_review`
   标出「会被拒的提示项」——这是**有意的读/写不对称**；
4. `/review/`（审核通过）走的就是 `PENDING_REVIEW → COMPLETED`，不许回归。

⛔ 本文件**不**把「列表 == 端点接受集合」放宽成恒真：第 3 组用例显式枚举
「已完成」这一项是例外（依据 spec §2.3）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from apps.business.order_status import (
    TRANSITIONS,
    IllegalTransition,
    apply_transition,
    can_transition,
    transition_denial,
)
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 业务方给的对标报错**逐字**原文（spec §2.3）。
EXACT_MESSAGE = "必须先把订单状态改成待验收才能改成已完成"

#: 本批删掉的六条入边（唯一保留的是 `PENDING_REVIEW → COMPLETED`）。
DELETED_SOURCES = (
    OrderStatus.UNASSIGNED,
    OrderStatus.WAITING_GRAB,
    OrderStatus.IN_PROGRESS,
    OrderStatus.ISSUE,
    OrderStatus.PAUSED,
    OrderStatus.REFUNDED,
)

NODES = (
    "order.add_edit",
    "order.list",
    "order.view_detail",
    "order.change_status",
    "order.submit_payout_price",
    "order.assign_taker",
    "order.change_status_completed",
)


class CompleteEdgeTableTests(SimpleTestCase):
    """状态机那一层：入 `COMPLETED` 的边只剩一条（纯逻辑，不碰数据库）。"""

    def test_only_pending_review_has_an_in_edge_to_completed(self):
        self.assertIn(
            (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED),
            TRANSITIONS,
            "业务方要求：只有「待验收 → 已完成」这条边必须留着",
        )
        self.assertTrue(
            can_transition(
                OrderStatus.PENDING_REVIEW,
                OrderStatus.COMPLETED,
                {"order.change_status_completed"},
            )
        )
        for source in DELETED_SOURCES:
            with self.subTest(source=source):
                self.assertNotIn((source, OrderStatus.COMPLETED), TRANSITIONS)
                self.assertFalse(can_transition(source, OrderStatus.COMPLETED, None))

    def test_the_denial_message_is_the_verbatim_business_copy(self):
        for source in DELETED_SOURCES:
            with self.subTest(source=source):
                self.assertEqual(
                    transition_denial(
                        source, OrderStatus.COMPLETED, {"order.change_status_completed"}
                    ),
                    EXACT_MESSAGE,
                )

    def test_other_illegal_transitions_keep_the_generic_copy(self):
        """⛔ 别的非法迁移的文案**一个字都不改**。

        ⚠️ 靶子在 本线 T4）换过一次：上一版拿**自环** `COMPLETED → COMPLETED`
        当通用那句的样本，自环现在回人话（`SELF_TRANSITION_MESSAGE`，值钉在
        `tests/test_order_status.py::SelfTransitionMessageTests`）⇒ 通用那句的样本换成
        「当前状态不是已知 `OrderStatus` 码」那一档（仍是「边不存在」）。
        """
        self.assertEqual(
            transition_denial("ARCHIVED", OrderStatus.UNASSIGNED, {"order.change_status"}),
            "非法状态迁移：ARCHIVED → UNASSIGNED",
        )
        # 节点不够那一支仍带后缀（两种拒绝可分辨，spec §2 A10）
        self.assertTrue(
            transition_denial(
                OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW, {"grab_hall.take_order"}
            ).endswith("（缺少所需权限节点）")
        )
        # ⭐ T4：自环**不是**通用那句（这里只钉「换档了」；逐字值在 `test_order_status.py`）。
        self.assertNotIn(
            "非法状态迁移",
            transition_denial(
                OrderStatus.COMPLETED, OrderStatus.COMPLETED, {"order.change_status_completed"}
            ),
        )


class _CompleteGateFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=NODES
        )

    def _order(self, no, status):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _transition(self, order, target):
        return self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": target},
            content_type="application/json",
        ).json()

    def _detail(self, order):
        body = self.client.get(f"/api/business/orders/{order.order_no}/").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]


class PendingReviewStillCompletesTests(_CompleteGateFixture):
    """判据①④：唯一的合法入边与 `/review/` 都不回归。"""

    def test_generic_transition_completes_from_pending_review(self):
        order = self._order("20260925000000000911", OrderStatus.PENDING_REVIEW)

        body = self._transition(order, OrderStatus.COMPLETED)

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.transition"
            ).exists()
        )

    def test_review_approval_still_completes(self):
        order = self._order("20260925000000000912", OrderStatus.PENDING_REVIEW)
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        perf.user = User.objects.create_user(username="测试打手甲")
        perf.review_status = ReviewStatus.PENDING
        perf.save(update_fields=["user", "review_status"])

        body = self.client.post(
            f"/api/business/performances/{perf.id}/review/",
            {"approved": True, "comment": "金额无误"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)


class DeletedInEdgesAreRefusedTests(_CompleteGateFixture):
    """判据②：六条被删的入边各自回业务码、状态不变、零留痕、文案逐字。"""

    def test_each_deleted_in_edge_is_refused_and_writes_nothing(self):
        for index, source in enumerate(DELETED_SOURCES):
            with self.subTest(source=source):
                order = self._order(f"2026092500000000092{index}", source)

                body = self._transition(order, OrderStatus.COMPLETED)

                self.assertNotEqual(body["code"], 0, (source, body))
                self.assertEqual(body["msg"], EXACT_MESSAGE, (source, body))
                order.refresh_from_db()
                self.assertEqual(order.status, source)
                self.assertFalse(
                    OperationLog.objects.filter(order_no=order.order_no).exists(),
                    f"{source}：被拒的迁移不该留痕",
                )


class ReadSideKeepsCompletedTests(_CompleteGateFixture):
    """判据③：读侧**仍列出**「已完成」，并用标记字段说明它是「会被拒的提示项」。"""

    def test_non_pending_review_lists_completed_and_marks_it_as_refused(self):
        for index, source in enumerate(DELETED_SOURCES):
            with self.subTest(source=source):
                order = self._order(f"2026092500000000093{index}", source)

                data = self._detail(order)

                self.assertIn(
                    OrderStatus.COMPLETED,
                    data["allowed_transitions"],
                    f"{source}：业务方要求下拉里「已完成」照旧要有",
                )
                self.assertIs(data["completed_requires_review"], True)

    def test_pending_review_lists_completed_without_the_warning(self):
        order = self._order("20260925000000000941", OrderStatus.PENDING_REVIEW)

        data = self._detail(order)

        self.assertIn(OrderStatus.COMPLETED, data["allowed_transitions"])
        self.assertIs(data["completed_requires_review"], False)

    def test_completed_order_does_not_list_itself(self):
        order = self._order("20260925000000000942", OrderStatus.COMPLETED)

        data = self._detail(order)

        self.assertNotIn(OrderStatus.COMPLETED, data["allowed_transitions"])
        self.assertIs(data["completed_requires_review"], False)

    def test_without_the_completed_node_it_is_not_listed_at_all(self):
        """没有节点 17 的调用方看不到「已完成」（节点判据没有被这条不对称放宽）。

        ⚠️ 反向账号必须换一个**组名**：节点挂在 `Role` 上、`Role` 与组同名唯一
        （`p5_auth_helpers._grant_nodes`），沿用「客服」会**继承**上一个 client 的节点 17
        ——那样这条判据会恒真（实测踩过）。
        """
        client = make_logged_in_client(
            username="测试客服乙",
            groups=("旁观组",),
            nodes=tuple(n for n in NODES if n != "order.change_status_completed"),
        )
        order = self._order("20260925000000000943", OrderStatus.IN_PROGRESS)

        body = client.get(f"/api/business/orders/{order.order_no}/").json()

        self.assertEqual(body["code"], 0, body)
        self.assertNotIn(OrderStatus.COMPLETED, body["data"]["allowed_transitions"])
        self.assertIs(body["data"]["completed_requires_review"], False)


class _StubOrder:
    """`apply_transition` 的替身：非法迁移路径**不该写库**。"""

    def __init__(self, status):
        self.status = status
        self.pk = 1
        self.peer_direct_pending = None

    def save(self, **kwargs):  # pragma: no cover - 拒绝路径不该写库
        raise AssertionError("被拒的迁移不该写库")


class ApplyTransitionMessageTests(SimpleTestCase):
    """唯一写入口上也回同一句话（不只在 `transition_denial` 那一层）。"""

    def test_apply_transition_raises_the_exact_message(self):
        order = _StubOrder(OrderStatus.IN_PROGRESS)
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(
                order,
                OrderStatus.COMPLETED,
                nodes={"order.change_status_completed"},
            )
        self.assertEqual(str(ctx.exception), EXACT_MESSAGE)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_the_message_does_not_come_from_the_stale_state_branch(self):
        """复读那一支的文案**不**被本批改动（仍然说清库里已经是哪个状态）。"""
        order = _StubOrder(OrderStatus.PENDING_REVIEW)
        locked = _StubOrder(OrderStatus.IN_PROGRESS)
        with (
            mock.patch("apps.business.order_status.Order.objects") as objects,
            mock.patch("apps.business.order_status.jidanpu_assignment_lock.lock_order_change", return_value=False) as order_lock,
        ):
            objects.select_for_update.return_value.only.return_value.get.return_value = locked
            with self.assertRaises(IllegalTransition) as ctx:
                apply_transition(
                    order,
                    OrderStatus.COMPLETED,
                    nodes={"order.change_status_completed"},
                )
        self.assertIn("该单当前状态已是", str(ctx.exception))
        order_lock.assert_not_called()
