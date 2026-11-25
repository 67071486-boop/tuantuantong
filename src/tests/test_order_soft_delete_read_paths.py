"""软删单在**全部读路径**里都不存在（spec §8-T3 第 10–25 条；裁定 3）。

⚠️ 每条都断言**两半**：已删的不出现 **且** 未删的仍然出现——只断言「空」是 E-0004 的假通过。
⚠️ 夹具值一律合成（E-0077）；跨租户的否定断言用订单号、**不用主键**（§5-G）。
⚠️ 授权节点必须与 `groups=` 成对给：节点挂在 `Group → Role → permissions` 上，
   只给 `nodes=` 不给组 ⇒ 一个节点都挂不上 ⇒ 全 403（看起来像代码坏了）。
⚠️ `make_logged_in_client` 返回的是 Django 测试 client（**没有 `.user`**）⇒ 要用到 user
   对象时按用户名回查。

覆盖 ↔ spec 条目对照（逐条列在类的 docstring 里；**没覆盖的条目在 T2 报告里如实列出**）。
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import notifications, refunds, settlement, settlement_hooks
from apps.business.api import serializers as business_serializers
from apps.business.claim import ClaimError, check_grab_limits
from apps.business.models import (
    EmpType,
    Game,
    Notification,
    NotificationKind,
    Order,
    OrderRefund,
    OrderStatus,
    PaymentStatus,
    Performance,
    PerformanceAdjustment,
    Project,
    ProjectCategory,
    ReviewStatus,
    SettleStatus,
)
from apps.business.order_stats import _window_rows
from apps.business.performance_writes import (
    DeleteError,
    _revoke_reject_message,
    delete_performances,
)
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

User = get_user_model()

ALIVE_NO = "20260924000000000101"
DEAD_NO = "20260924000000000102"
ACTOR = "测试操作人甲"
TAKER = "测试打手甲"
#: 只出现在已软删单上的合成人名（C6 用例的第二半判据）。
DEAD_ONLY = "测试打手丁"


class _Fixture(TenantTestCase):
    """一张在库单 + 一张已软删单（订单号指认），每条读路径都拿它们当两半。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        # 打手（登录态拿得到，「我的订单 / 通知」两条路要用）。
        self.taker_client = make_logged_in_client(
            username=TAKER, groups=("打手甲",), nodes=("my_orders.send_to_my_system",)
        )
        self.taker = User.objects.get(username=TAKER)

        self.alive = Order.objects.create(
            game=self.game, order_no=ALIVE_NO, status=OrderStatus.COMPLETED,
            total_price=Decimal("100.00"), hall_price=Decimal("80.00"),
        )
        self.dead = Order.objects.create(
            game=self.game, order_no=DEAD_NO, status=OrderStatus.COMPLETED,
            total_price=Decimal("200.00"), hall_price=Decimal("90.00"),
        )
        self.alive_main = Performance.objects.create(
            order=self.alive, user=self.taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00"),
        )
        self.dead_main = Performance.objects.create(
            order=self.dead, user=self.taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"), bonus_amount=Decimal("20.00"),
        )
        self.alive_sales = Performance.objects.create(
            order=self.alive, emp_type=EmpType.SALES,
            temp_amount=Decimal("5.00"), bonus_amount=Decimal("5.00"),
            commission_rate=Decimal("10"),
        )
        self.dead_sales = Performance.objects.create(
            order=self.dead, emp_type=EmpType.SALES,
            temp_amount=Decimal("6.00"), bonus_amount=Decimal("6.00"),
            commission_rate=Decimal("10"),
        )
        # 软删：走 `all_objects`（默认 manager 看不到它，这正是本文件要断言的事实）。
        Order.all_objects.filter(pk=self.dead.pk).update(
            deleted_at=timezone.now(), deleted_by=ACTOR
        )

    def _client(self, username, group, nodes):
        return make_logged_in_client(username=username, groups=(group,), nodes=nodes)


class OrderListExclusionTests(_Fixture):
    """第 10 / 24 条：订单列表（A1）的行与头部统计块。"""

    def test_the_deleted_order_is_absent_from_both_the_rows_and_the_summary(self):
        client = self._client("测试客服甲", "客服甲", ("order.list",))
        body = client.get("/api/business/orders/").json()
        self.assertEqual(body["code"], 0)
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)
        # 头部统计块与列表**同一份筛选后的 queryset** ⇒ 已删单不计入（第 24 条）。
        summary = body["data"]["summary"]
        self.assertEqual(summary["total_price"], "100.00")
        self.assertEqual(summary["count"], 1)


class HallExclusionTests(_Fixture):
    """第 11 条：抢单大厅（A2）——已抛单 + 已删 ⇒ 不在大厅；同条件的未删单**在**。"""

    def setUp(self):
        super().setUp()
        Order.objects.filter(pk=self.alive.pk).update(status=OrderStatus.WAITING_GRAB)
        Order.all_objects.filter(pk=self.dead.pk).update(status=OrderStatus.WAITING_GRAB)
        # 大厅入选条件之一是 MAIN 行的 `user` 为空。
        Performance.objects.filter(order__in=[self.alive, self.dead]).update(user=None)

    def test_the_deleted_order_is_not_in_the_hall_while_the_alive_one_is(self):
        client = self._client("测试打手乙", "打手乙", ("grab_hall.take_order",))
        body = client.get("/api/business/hall/").json()
        self.assertEqual(body["code"], 0)
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)


class DetailAndWriteActionsOnDeletedOrderTests(_Fixture):
    """第 12 / 14 条：详情与全部 detail 动作 ⇒ 业务码失败，**且库里一列没变**。"""

    #: `audit` 是 **GET**（`@action(methods=["get"])`）⇒ 它不进下面的 POST 循环，单独断言。
    WRITE_ACTIONS = (
        "transition", "throw-to-hall", "withdraw", "assign",
        "clear-taker", "submit", "price", "payment-status", "hall-price",
    )
    WRITE_ACTION_NODES = (
        "order.view_detail", "order.change_status", "order.set_offer_price",
        "order.send_to_grab_hall", "order.submit_payout_price",
        "order.withdraw_from_grab_hall", "order.assign_taker",
        "order.clear_taker", "order.submit_work", "order.edit_payout_price",
        "order.change_payment_status",
    )

    def _snapshot(self):
        row = Order.all_objects.get(order_no=DEAD_NO)
        perf = Performance.objects.get(order=row, emp_type=EmpType.MAIN)
        return (
            row.status, str(row.total_price), str(row.hall_price), str(row.refund_fee),
            str(perf.temp_amount), str(perf.bonus_amount), perf.user_id,
        )

    def test_detail_of_a_deleted_order_is_gone_with_business_code(self):
        client = self._client("测试客服乙", "客服乙", ("order.view_detail",))
        body = client.get(f"/api/business/orders/{DEAD_NO}/").json()
        self.assertNotEqual(body["code"], 0)
        self.assertIsNone(body["data"])

    def test_audit_of_a_deleted_order_is_gone_too(self):
        client = self._client("测试客服丙", "客服丙", ("order.view_detail",))
        body = client.get(f"/api/business/orders/{DEAD_NO}/audit/").json()
        self.assertNotEqual(body["code"], 0)

    def test_every_detail_write_action_fails_and_changes_no_column(self):
        before = self._snapshot()
        client = self._client("测试客服丁", "客服丁", self.WRITE_ACTION_NODES)
        for action in self.WRITE_ACTIONS:
            with self.subTest(action=action):
                resp = client.post(
                    f"/api/business/orders/{DEAD_NO}/{action}/", {},
                    content_type="application/json",
                )
                self.assertNotEqual(
                    resp.json()["code"], 0, f"{action} 对已删单竟然成功了"
                )
                self.assertEqual(self._snapshot(), before, f"{action} 改了库里的列")

    # ── A3 的**对照半**：同一批动作打未删单 ⇒ `code == 0` ────────────
    # ⚠️ 上面三条只断言「打已删单失败」；若端点**整体**坏掉（`get_object()` 恒 404、
    #    权限门全线 403 之类），它们**照样绿**。下面三条是阳性对照，让上面那批「非 0」
    #    真的有分辨力（否则那三条断言可能是恒真的）。
    def test_detail_of_an_alive_order_is_reachable(self):
        client = self._client("测试客服庚", "客服庚", ("order.view_detail",))
        body = client.get(f"/api/business/orders/{ALIVE_NO}/").json()
        self.assertEqual(body["code"], 0)

    def test_audit_of_an_alive_order_is_reachable(self):
        client = self._client("测试客服辛", "客服辛", ("order.view_detail",))
        body = client.get(f"/api/business/orders/{ALIVE_NO}/audit/").json()
        self.assertEqual(body["code"], 0)

    def test_every_detail_write_action_succeeds_on_an_alive_order(self):
        """同一批动作打**未删单** ⇒ `code == 0`（每步用各自新的未删单 + 合法前置态与载荷）。"""
        # `/submit/` 必须由**这张单的接单人**发起 ⇒ 给既有打手账号补 `order.submit_work`
        # 并复用它的登录态（`make_logged_in_client` 不接受重复用户名，不能再建一个同名账号）。
        grant_nodes(self.taker, "order.submit_work")
        actor = self._client("测试客服壬", "客服壬", self.WRITE_ACTION_NODES)
        assignee = User.objects.create_user(username="测试被指派人甲")
        # ⭐ D-0252：`assign` 动作判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 被指派人也得持本单游戏（grant_games 会 get_or_create 档案，提成可算）。
        grant_games(assignee, self.game)
        #: (动作, 前置状态, 载荷, 用哪个 client, MAIN 行的 `user`)
        #: `throw-to-hall` / `withdraw` 的目标是池内两态，只有「无接手人」的行到得了。
        #: 本用例钉的是未删单上端点通不通，所以这两档把 MAIN 行的 `user` 置空。
        cases = (
            ("transition", OrderStatus.COMPLETED, {"target": OrderStatus.ISSUE}, actor, self.taker),
            ("throw-to-hall", OrderStatus.UNASSIGNED, {}, actor, None),
            ("withdraw", OrderStatus.WAITING_GRAB, {}, actor, None),
            ("assign", OrderStatus.UNASSIGNED, {"user_id": assignee.id}, actor, self.taker),
            # 清除接单人用 `IN_PROGRESS`：要测的是未删单上这个端点通不通，与来源态无关。
            # 有接单人必须放行，所以 `user` 保持非空。
            ("clear-taker", OrderStatus.IN_PROGRESS, {}, actor, self.taker),
            ("submit", OrderStatus.IN_PROGRESS, {}, self.taker_client, self.taker),
            ("price", OrderStatus.COMPLETED, {"total_price": "123.00"}, actor, self.taker),
            (
                "payment-status", OrderStatus.COMPLETED,
                {"payment_status": PaymentStatus.SETTLED}, actor, self.taker,
            ),
            ("hall-price", OrderStatus.COMPLETED, {"price": "77.00"}, actor, self.taker),
        )
        for index, (action, status, payload, client, main_user) in enumerate(cases, start=1):
            with self.subTest(action=action):
                order = Order.objects.create(
                    game=self.game, order_no=f"2026092400000000{index:04d}",
                    status=status, total_price=Decimal("100.00"),
                    hall_price=Decimal("80.00"),
                )
                Performance.objects.create(
                    order=order, user=main_user, emp_type=EmpType.MAIN,
                    temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00"),
                )
                body = client.post(
                    f"/api/business/orders/{order.order_no}/{action}/", payload,
                    content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 0, f"{action} 对未删单失败了：{body}")


class ClaimOnDeletedOrderTests(_Fixture):
    """第 13 条：抢单（A4）——已删单抢不到，且 `Order` 行无变化。"""

    def setUp(self):
        super().setUp()
        Order.all_objects.filter(pk=self.dead.pk).update(status=OrderStatus.WAITING_GRAB)
        Order.objects.filter(pk=self.alive.pk).update(status=OrderStatus.WAITING_GRAB)
        Performance.objects.filter(order__in=[self.alive, self.dead]).update(user=None)

    def test_claim_on_a_deleted_order_fails_and_writes_nothing(self):
        before = Order.all_objects.get(order_no=DEAD_NO).status
        client = self._client("测试打手丙", "打手丙", ("grab_hall.take_order",))
        resp = client.post(f"/api/business/hall/{DEAD_NO}/claim/", {}, content_type="application/json")
        self.assertNotEqual(resp.json()["code"], 0)
        self.assertEqual(Order.all_objects.get(order_no=DEAD_NO).status, before)
        # 控制组：同一路径对未删单是可达的（否则上面的断言可能是「端点整体坏了」）。
        self.assertEqual(
            client.post(f"/api/business/hall/{ALIVE_NO}/claim/", {}, content_type="application/json").json()["code"],
            0,
        )


class RefundAndRefundListTests(_Fixture):
    """第 15 条：退款（A6）+ 退款记录列表（C8）。"""

    def test_refund_on_a_deleted_order_fails_and_the_list_hides_its_rows(self):
        OrderRefund.objects.create(
            order=self.dead, kind=OrderRefund.Kind.PARTIAL, amount=Decimal("1.00"),
            reason="测试退款甲", operator=ACTOR, request_id="测试-退款-死单",
        )
        OrderRefund.objects.create(
            order=self.alive, kind=OrderRefund.Kind.PARTIAL, amount=Decimal("2.00"),
            reason="测试退款乙", operator=ACTOR, request_id="测试-退款-活单",
        )
        client = self._client("测试财务甲", "财务甲", ("order.online_refund",))
        self.assertEqual(
            client.get(f"/api/business/orders/{DEAD_NO}/refunds").json()["data"]["total"], 0
        )
        self.assertEqual(
            client.get(f"/api/business/orders/{ALIVE_NO}/refunds").json()["data"]["total"], 1
        )
        # 写侧：对已删单退款必须失败，且 `refund_fee` 一列没变。
        resp = client.post(
            f"/api/business/orders/{DEAD_NO}/refunds",
            {"refund_total": "1.00", "reason": "测试退款丙", "request_id": "测试-退款-丙"},
            content_type="application/json",
        )
        self.assertNotEqual(resp.json()["code"], 0)
        self.assertEqual(
            str(Order.all_objects.get(order_no=DEAD_NO).refund_fee), "0.00"
        )


class MyOrdersAndPerformanceTests(_Fixture):
    """第 16 / 17 条：我的订单（C1）与绩效列表（C2）。"""

    def test_my_orders_hides_the_deleted_order_and_keeps_the_alive_one(self):
        body = self.taker_client.get("/api/business/my-orders/").json()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)

    def test_performance_list_hides_the_deleted_orders_rows(self):
        client = self._client("测试绩效甲", "绩效甲", ("my_performance.export", "order.list"))
        body = client.get("/api/business/performances/").json()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)


class SettlementExclusionTests(_Fixture):
    """第 18 条：结算列表（C4）/ 结算池（C11）/ 结账（C11）。"""

    def test_settlement_list_hides_the_deleted_orders_rows(self):
        client = self._client("测试财务乙", "财务乙", ("finance.payroll",))
        body = client.get("/api/business/settlements").json()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)

    def test_unattributed_list_hides_the_deleted_order(self):
        # 待归属的入选条件是「已完成 + 没有销售提成行」⇒ 先把 SALES 行移开。
        Performance.objects.filter(emp_type=EmpType.SALES).delete()
        client = self._client("测试财务丙", "财务丙", ("finance.payroll",))
        body = client.get("/api/business/settlements/unattributed").json()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)

    def test_a_deleted_orders_performance_row_cannot_be_settled_while_the_alive_one_can(self):
        client = self._client("测试财务丁", "财务丁", ("finance.change_settlement_status",))
        dead = client.post(
            "/api/business/settlements/settle",
            {"performance_ids": [self.dead_main.pk]}, content_type="application/json",
        ).json()
        self.assertNotEqual(dead["code"], 0)
        self.assertEqual(
            Performance.objects.get(pk=self.dead_main.pk).settle_status,
            SettleStatus.UNSETTLED,
        )
        # 控制组：同一端点对未删单是可达的。
        alive = client.post(
            "/api/business/settlements/settle",
            {"performance_ids": [self.alive_main.pk]}, content_type="application/json",
        ).json()
        self.assertEqual(alive["code"], 0)
        self.assertEqual(
            Performance.objects.get(pk=self.alive_main.pk).settle_status,
            SettleStatus.SETTLED,
        )

    def test_people_candidates_drop_the_deleted_orders_person_but_keep_the_alive_one(self):
        """第 18 条附带：`GET /settlements/people`（C6，结算对象人员候选）的两半。

        ⚠️ 这条是本端点**唯一**的行为用例（D-0089；此前只有登记网与 `scope` 用例）。
        两半都打在**真实返回**上：把已删单的 MAIN 行改挂到一个「只在这张已删单上出现过」
        的人名下 ⇒ 他**不在**候选里；未删单的 MAIN 行仍挂在 `self.taker` 名下 ⇒ 他**仍在**。
        """
        dead_only = User.objects.create_user(username=DEAD_ONLY)
        Performance.objects.filter(pk=self.dead_main.pk).update(user=dead_only)
        client = self._client("测试财务己", "财务己", ("finance.payroll",))
        body = client.get("/api/business/settlements/people").json()
        self.assertEqual(body["code"], 0)
        usernames = [row["username"] for row in body["data"]["items"]]
        # 半 ①：已删单涉及的人不在候选名单里。
        self.assertNotIn(DEAD_ONLY, usernames)
        # 半 ②：未删单涉及的人仍在候选名单里。
        self.assertIn(TAKER, usernames)
        # 恰好只剩未删单那一个人（防止「两半」被别的人头掩盖）。
        self.assertEqual(usernames, [TAKER])


class AdjustmentListTests(_Fixture):
    """第 19 条：调整记录列表（C7 的列表读法）。"""

    def test_the_list_hides_adjustments_of_a_deleted_orders_performance(self):
        for perf, rid in ((self.dead_main, "测试-调整-死单"), (self.alive_main, "测试-调整-活单")):
            PerformanceAdjustment.objects.create(
                performance=perf, kind=PerformanceAdjustment.Kind.MANUAL,
                amount=Decimal("1.00"), reason="测试调整", operator=ACTOR,
                settle_status=SettleStatus.UNSETTLED, request_id=rid,
            )
        client = self._client("测试财务戊", "财务戊", ("finance.payroll",))
        dead = client.get(f"/api/business/performances/{self.dead_main.pk}/adjustments").json()
        alive = client.get(f"/api/business/performances/{self.alive_main.pk}/adjustments").json()
        self.assertEqual(dead["data"]["total"], 0)
        self.assertEqual(alive["data"]["total"], 1)


class PerformanceDetailActionTests(_Fixture):
    """C3：`PerformanceViewSet._base_queryset`（detail 面）的两半（`/adjust/` 与 `/review/`）。

    ⚠️ 这两条 detail 动作**不吃列表筛选参数**，走的是 `_base_queryset()`；已软删单的绩效行
    对它们就是「不存在」（业务码非 0），而未删单的同一行动作必须可达（`code == 0`）——
    只断言「非 0」会让「端点整体坏了」也全绿（E-0004 的假通过）。
    """

    #: 两个动作各自的类级节点门（`PerformanceViewSet.PERFORMANCE_PERMISSIONS`）。
    NODES = ("order.submit_payout_price", "order.change_status_completed")

    def test_the_two_detail_actions_miss_a_deleted_orders_row_and_hit_an_alive_one(self):
        """判据打在**真实出口**：响应业务码 + 库里那一行没变（⛔ 不断言中间变量）。"""
        snapshot = ("temp_amount", "bonus_amount", "review_status", "reviewed_by")
        # ⚠️ **已删单也要备齐它的合法前置态**（订单 `待验收` + 绩效 `待审核`）：否则 `/review/`
        #    会因**状态不对**而返回非 0，那条断言对「软删收口」就没有分辨力（假通过）。
        Order.all_objects.filter(pk=self.dead.pk).update(status=OrderStatus.PENDING_REVIEW)
        Performance.objects.filter(pk=self.dead_main.pk).update(
            review_status=ReviewStatus.PENDING
        )
        before = Performance.objects.filter(pk=self.dead_main.pk).values(*snapshot).get()
        # 未删对照单要备齐 `/review/` 的前置态：订单 `待验收` **且** 绩效 `待审核`。
        Order.objects.filter(pk=self.alive.pk).update(status=OrderStatus.PENDING_REVIEW)
        Performance.objects.filter(pk=self.alive_main.pk).update(
            review_status=ReviewStatus.PENDING
        )
        client = self._client("测试绩效乙", "绩效乙", self.NODES)
        cases = (
            ("adjust", {"temp_amount": "12.00"}),
            ("review", {"approved": True, "comment": "测试审核甲"}),
        )
        for action, payload in cases:
            with self.subTest(action=action, side="dead"):
                body = client.post(
                    f"/api/business/performances/{self.dead_main.pk}/{action}/",
                    payload, content_type="application/json",
                ).json()
                self.assertNotEqual(body["code"], 0, f"{action} 竟然命中了已删单的绩效行")
        # 已删单那一行**一列没变**（非 0 必须是「没拿到」，不是「拿到了但写坏了」）。
        self.assertEqual(
            Performance.objects.filter(pk=self.dead_main.pk).values(*snapshot).get(),
            before,
        )
        # 阳性对照：同一批动作打未删单 ⇒ 可达（否则上面的「非 0」可能只是端点整体坏了）。
        for action, payload in cases:
            with self.subTest(action=action, side="alive"):
                body = client.post(
                    f"/api/business/performances/{self.alive_main.pk}/{action}/",
                    payload, content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 0, f"{action} 对未删单失败了：{body}")


class OrderStatsWindowTests(_Fixture):
    """第 20 条：职业统计的窗口行（C9）——**函数级**（端点出参很宽，两半判据在这里更直接）。"""

    def test_the_window_rows_exclude_the_deleted_orders_rows(self):
        Performance.objects.filter(order__in=[self.alive, self.dead]).update(
            claimed_at=timezone.now()
        )
        rows = _window_rows(stime=date.today(), etime=date.today(), viewer=self.taker)
        perf_ids = {row.pk for group in rows.values() for row in group}
        self.assertIn(self.alive_main.pk, perf_ids)
        self.assertNotIn(self.dead_main.pk, perf_ids)


class NotificationListTests(_Fixture):
    """第 22 条：站内通知列表（C17）——`Notification.order` 是 `SET_NULL`，`order` 可为 NULL。"""

    def test_notifications_of_a_deleted_order_are_hidden_but_nulls_are_kept(self):
        Notification.objects.create(
            user=self.taker, kind=NotificationKind.ASSIGNED,
            order=self.dead, payload={"order_no": DEAD_NO},
        )
        Notification.objects.create(
            user=self.taker, kind=NotificationKind.ASSIGNED,
            order=self.alive, payload={"order_no": ALIVE_NO},
        )
        # 控制组：`order` 为 NULL 的通知**必须留着**。
        # ⚠️ 别把这条读成「`.filter(order__deleted_at__isnull=True)` 会把它丢掉」——实测：
        #    在 Django 5.2 下两种写法**等价**、NULL-order 行在两边都留（SQL 都是 LEFT OUTER
        #    JOIN，见 `apps/business/api/notification_views.py` 的 C17 注释与源码依据）。
        #    本用例钉的是**结果**（NULL 行不许少），不是某种写法。
        Notification.objects.create(
            user=self.taker, kind=NotificationKind.PENDING_REVIEW, order=None, payload={},
        )
        body = self.taker_client.get("/api/business/notifications/").json()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(DEAD_NO, nos)
        self.assertIn(ALIVE_NO, nos)
        self.assertIn(None, nos)


class GrabbedNotificationSourceTests(_Fixture):
    """C15：`notifications._main_performance_taker_id` 不把**已删单**的 MAIN 行当「当前接手人」。

    已删单的 MAIN 行仍有归属人（`self.taker`）⇒ 两半的**唯一**区别就是订单是否已软删。
    判据打在**数据库行**（`Notification` 的 `被抢走` 那一类），⛔ 不断言中间变量。
    """

    def test_a_deleted_orders_main_row_does_not_trigger_the_grabbed_notification(self):
        thrower = User.objects.create_user(username="测试抛单人甲")
        Order.all_objects.filter(pk__in=[self.alive.pk, self.dead.pk]).update(
            created_by=thrower.username, status=OrderStatus.WAITING_GRAB
        )
        # 半 ①：已删单 + MAIN 行有归属人 ⇒ **不发**（否则会冒出一条假的「被抢走」）。
        notifications._on_in_progress(
            Order.all_objects.get(pk=self.dead.pk), OrderStatus.WAITING_GRAB
        )
        dead_rows = Notification.objects.filter(kind=NotificationKind.GRABBED)
        self.assertEqual(dead_rows.count(), 0)
        # 半 ②（阳性对照）：同一钩子打未删单 ⇒ 给抛单人写一行，且钉在**这张单**上。
        notifications._on_in_progress(
            Order.objects.get(pk=self.alive.pk), OrderStatus.WAITING_GRAB
        )
        alive_rows = Notification.objects.filter(kind=NotificationKind.GRABBED)
        self.assertEqual([row.payload["order_no"] for row in alive_rows], [ALIVE_NO])
        self.assertEqual([row.user_id for row in alive_rows], [thrower.pk])


class GrabLimitHeldCountTests(_Fixture):
    """第 23 条：抢单持有量闸（C18）——已删单不计入持有量。"""

    def test_the_held_count_ignores_a_deleted_orders_active_row(self):
        # 已删单占着一个「未完成」额度（`IN_PROGRESS` 在 `_ACTIVE_STATUSES` 里）。
        Order.all_objects.filter(pk=self.dead.pk).update(status=OrderStatus.IN_PROGRESS)
        Order.objects.filter(pk=self.alive.pk).update(status=OrderStatus.WAITING_GRAB)
        Performance.objects.filter(order=self.alive).update(user=None)
        profile = self.taker.staff_profile
        profile.grab_limit_dl = 1
        profile.grab_price_min = Decimal("1.00")
        profile.grab_price_max = Decimal("999.00")
        profile.save()
        target = Order.objects.get(pk=self.alive.pk)
        # 控制组前一半：已删单**不**计入 ⇒ 持有 0 < 上限 1 ⇒ 通过。
        check_grab_limits(user_id=self.taker.pk, order=target)
        # 控制组后一半：把**未删**单也弄成进行中 ⇒ 持有 1 ≥ 上限 1 ⇒ 必须拒。
        Order.objects.filter(pk=self.alive.pk).update(status=OrderStatus.IN_PROGRESS)
        Performance.objects.filter(order=self.alive, emp_type=EmpType.MAIN).update(user=self.taker)
        with self.assertRaises(ClaimError):
            check_grab_limits(user_id=self.taker.pk, order=target)


class RejectMessageAndHookTests(_Fixture):
    """C10 / C13 / C14 / C19 / C22 / C23 / C24 的**函数级**两半（端点出参很宽，这里更直接）。"""

    def test_reject_message_says_missing_for_a_deleted_orders_row(self):
        self.assertIn(
            "不存在或不可见", settlement._reject_message([self.dead_main.pk], user=self.taker)
        )
        self.assertNotIn(
            "不存在或不可见", settlement._reject_message([self.alive_main.pk], user=self.taker)
        )

    def test_revoke_and_delete_reject_messages_say_missing_for_a_deleted_orders_row(self):
        self.assertIn(
            "不存在或不可见",
            _revoke_reject_message([self.dead_main.pk], user=self.taker),
        )
        # `delete_performances` 对已删单的行落「不存在或不可见」（**不是**那条
        # 「MAIN 行不能删除」）——两半都能从 msg 区分。
        with self.assertRaises(DeleteError) as dead_ctx:
            delete_performances([self.dead_main.pk], actor=ACTOR, user=self.taker)
        self.assertIn("不存在或不可见", dead_ctx.exception.message)
        with self.assertRaises(DeleteError) as alive_ctx:
            delete_performances([self.alive_main.pk], actor=ACTOR, user=self.taker)
        self.assertNotIn("不存在或不可见", alive_ctx.exception.message)
        # `revoke_settlements` 的已删单那一支在 `_revoke_reject_message` 里已单独断言。
        self.assertIn(
            "不存在或不可见", _revoke_reject_message([self.dead_main.pk], user=self.taker)
        )

    def test_completion_hook_ignores_a_deleted_orders_rows(self):
        self.assertIsNone(settlement_hooks._activate_taker_performance(self.dead))
        self.assertEqual(
            settlement_hooks._activate_taker_performance(self.alive).pk, self.alive_main.pk
        )

    def test_refund_helpers_walk_only_the_alive_orders_rows(self):
        # ⭐ 结算 P0-2，D-0281：部分退款的冲减现在**只给已结账的客服行**建，
        #   未结账的行改成由 `refresh_customer_estimate` 重算 bonus。本用例要判的是
        #   「**只走活单**的行」这把尺（死单 0 条 / 活单 1 条），所以把两行都摆成**已结账**
        #   ——那是仍然会建冲减的那一档，判别力不变；⛔ 不要改成「两边都 0」，
        #   那会让这条用例对「死单被误扫」彻底失明。
        Performance.objects.filter(
            pk__in=[self.dead_sales.pk, self.alive_sales.pk]
        ).update(
            settle_status=SettleStatus.SETTLED,
            settled_by="system",
            settled_at=timezone.now(),
            settled_amount=Decimal("0.00"),
        )
        refund = OrderRefund.objects.create(
            order=self.dead, kind=OrderRefund.Kind.PARTIAL, amount=Decimal("1.00"),
            reason="测试退款丁", operator=ACTOR, request_id="测试-退款-丁",
        )
        # C22：客服行冲减 —— 已删单一条都不建，未删单建一条。
        refunds._apply_sales_deductions(refund, self.dead, Decimal("1.00"), ACTOR)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=self.dead_sales).count(), 0
        )
        refunds._apply_sales_deductions(refund, self.alive, Decimal("1.00"), ACTOR)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=self.alive_sales).count(), 1
        )
        # C23：清零前的原接单人留痕 —— 已删单不写。
        self.assertIsNone(
            refunds._record_taker_trace_before_clearing(self.dead, ACTOR)
        )
        self.assertIsNotNone(
            refunds._record_taker_trace_before_clearing(self.alive, ACTOR)
        )

    def test_reverse_sales_deductions_skips_a_deleted_orders_rows(self):
        for perf, rid in ((self.dead_sales, "测试-冲减-死单"), (self.alive_sales, "测试-冲减-活单")):
            PerformanceAdjustment.objects.create(
                performance=perf, kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
                amount=Decimal("-1.00"), reason="测试冲减", operator=ACTOR,
                settle_status=SettleStatus.UNSETTLED, request_id=rid,
            )
        self.assertEqual(refunds._reverse_sales_refund_deductions(self.dead, ACTOR), [])
        written = refunds._reverse_sales_refund_deductions(self.alive, ACTOR)
        self.assertEqual([row.performance_id for row in written], [self.alive_sales.pk])

    def test_reverse_sales_deductions_logs_the_owner_display_name(self):
        """⭐ 2026-10-17（spec §3-A2 / 判据 5）：回池冲销的 `performance.adjust_manual`
        留痕里，`target_name` 是**账号名称**（昵称优先、空串回落登录账号），不是登录账号。

        正反两半：正文里**有**昵称、**没有**登录账号（夹具昵称与账号不同形）。
        操作人另建一个人（昵称也不同形），避免「操作人 == 目标人」把两处名字混成一条。
        """
        owner = User.objects.create_user(
            username="sales-nick", first_name="测试昵称销售"
        )
        operator = User.objects.create_user(
            username="refund-operator", first_name="测试昵称退款员"
        )
        Performance.objects.filter(pk=self.alive_sales.pk).update(user=owner)
        PerformanceAdjustment.objects.create(
            performance=self.alive_sales,
            kind=PerformanceAdjustment.Kind.PARTIAL_REFUND,
            amount=Decimal("-1.00"),
            reason="测试冲减",
            operator=ACTOR,
            settle_status=SettleStatus.UNSETTLED,
            request_id="测试-冲减-昵称",
        )

        written = refunds._reverse_sales_refund_deductions(
            self.alive, operator.get_username(), actor_id=operator.pk
        )
        self.assertEqual([row.performance_id for row in written], [self.alive_sales.pk])

        row = OperationLog.objects.get(
            action="performance.adjust_manual", order_no=ALIVE_NO
        )
        self.assertEqual(row.target_name, "测试昵称销售")
        self.assertIn("测试昵称销售", row.summary)
        self.assertNotIn("sales-nick", row.summary)
        # 操作人（另一个人）也按显示名落前缀，不与目标人混淆。
        self.assertIn("测试昵称退款员", row.summary)


class ReferenceCountTests(_Fixture):
    """spec §8-T5 第 28 / 29 条（B 组）：`PROTECT` 引用是**物理**的 ⇒ 已软删单也要计入。"""

    def test_the_three_serializers_count_the_deleted_order_too(self):
        game = Game.objects.get(pk=self.game.pk)
        category = ProjectCategory.objects.create(game=self.game, name="测试分类甲")
        project = Project.objects.create(category=category, name="测试项目甲")
        Order.all_objects.filter(pk__in=[self.alive.pk, self.dead.pk]).update(
            category=category, project=project
        )
        self.assertEqual(business_serializers.GameSerializer(game).data["order_count"], 2)
        self.assertEqual(
            business_serializers.ProjectCategorySerializer(category).data["order_count"], 2
        )
        self.assertEqual(
            business_serializers.ProjectSerializer(project).data["order_count"], 2
        )

    def test_the_viewset_annotation_counts_the_deleted_order_too(self):
        from apps.business.api.config_views import GameViewSet

        annotated = GameViewSet.count_annotations["order_count"]
        counted = Game.objects.annotate(n=annotated).get(pk=self.game.pk).n
        self.assertEqual(counted, 2)
