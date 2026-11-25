"""`POST /api/business/performances/<id>/delete/`（spec §4.5 / §4.6；D-0147 批 E 任务 3）。

判据 = spec §6.1 的 **T11–T13**，加任务书点名的两条边界：

- **读路径边界**：软删后的行在绩效列表（`fetchPerformances` 走的那条 queryset）里看不见；
- **emp_type 门**（R-9）：端点只接受 `EmpType.MAIN`（打单工资）的行；SALES（销售提成）
  ⇒ 业务码 `1014`、文案「只能删除接单人的绩效记录」，且一行不写；
- **权限门**（业务方裁定后）：软删**任何**绩效行 ⇒ `order.submit_payout_price`(1162)
  这一道底门；连带清接手人也**只要** 1162（不再额外要 `order.clear_taker`(19)）；
- **T15 跨租户**：两个 schema 的目标行**主键相同**，判据一律用**订单号 / 用户名**，
  不用主键（AGENTS §5-G：两个 schema 的主键各自从 1 开始，拿主键指认另一租户的行是恒假判据）。

全部断言打在**数据库行**与 `OperationLog` 行上（E-0009 / E-0024），不是"响应没报错"。
⚠️ 夹具一律合成值（E-0077）。

⚠️ **审计槽位**经既有的 `performance.delete` 模板（`apps/audit/actions.py` 早已随
`main` 的硬删端点登记：动作码 + 六个槽位全有真实来源）⇒ 本端点**复用**它，不新造模板；
`reason` 落 `settle_status` 这个真实值（模板正文给标签"结账状态："）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 夹具的节点集：`order.submit_payout_price`(1162) 是软删**任何**绩效行都要的**底门**，
#: 连带清接手人**也只要它**（业务方裁定，不再额外要 `order.clear_taker`(19)）；
#: `order.clear_taker`(19) 仍在集里，用来钉「只有 19、没有 1162 ⇒ 仍 403」那一档；
#: `my_performance.export`(1163) + `order.list`(14) 是绩效列表读端点（边界判据）的两扇门。
DELETE_NODES = (
    "order.submit_payout_price",
    "order.clear_taker",
    "my_performance.export",
    "order.list",
)

#: `ORDER_PERCENT`(=20) 是打手路径声明的口径，档案模式与它一致才可算提成。
PERCENT_MODE = CommissionMode.ORDER_PERCENT


class _DeleteFixture(TenantTestCase):
    """一条 `IN_PROGRESS` 单 + 当前接单人那条 MAIN 行 + 一条 SALES 行。

    ⚠️ 金额刻意给非零值（`25.00` / `7.77`）：0 值下「写没写」与「本来就是 0」不可区分。
    """

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260924000000000801",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("200.00"),
            hall_price=Decimal("100.00"),
        )
        self.taker = User.objects.create_user(username="测试打手甲")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=PERCENT_MODE, commission_value=Decimal("25.00")
        )
        self.main = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("25.00"),
            bonus_amount=Decimal("7.77"),
        )
        self.sales_user = User.objects.create_user(username="测试销售甲")
        self.sales = Performance.objects.create(
            order=self.order,
            user=self.sales_user,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("1.00"),
            bonus_amount=Decimal("2.00"),
        )
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=DELETE_NODES
        )
        self.actor = User.objects.get(username="测试客服甲")

    def _delete(self, performance_id):
        return self.client.post(
            f"/api/business/performances/{performance_id}/delete/"
        ).json()

    def _order(self):
        return Order.objects.get(pk=self.order.pk)

    def _perf(self, performance_id):
        return Performance.objects.get(pk=performance_id)


class SoftDeleteAndClearTakerTests(_DeleteFixture):
    """T11：软删该行（行还在）+ 同一步清接手人。"""

    def test_deleting_the_current_takers_main_row_soft_deletes_and_clears_the_taker(self):
        body = self._delete(self.main.id)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"], {"deleted": self.main.id, "taker_cleared": True})

        # 「软删」的判据：两列有值，**行还在**（`Performance.objects` 没有默认 manager 过滤）。
        row = self._perf(self.main.id)
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(row.deleted_by, self.actor.username)

        # 同一步清接手人：订单 `UNASSIGNED`、MAIN 行 `user is None`。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertIsNone(row.user_id)
        # ⚠️ 这是**绩效行**的垃圾桶，不是订单回收站：订单自己没有 `deleted_at`。
        self.assertIsNone(self._order().deleted_at)
        # 对照：另一条（SALES）行没有被连带软删。
        self.assertIsNone(self._perf(self.sales.id).deleted_at)

    def test_the_delete_writes_one_audit_row_with_the_performance_target(self):
        self._delete(self.main.id)
        log = OperationLog.objects.get(action="performance.delete")
        self.assertEqual(log.target_kind, "performance")
        self.assertEqual(log.target_id, str(self.main.id))
        self.assertEqual(log.target_name, "测试打手甲")
        self.assertEqual(log.order_no, self.order.order_no)
        self.assertEqual(log.actor, self.actor.username)
        # 留痕记的是**删除那一刻**的值（清接手人会把 `bonus_amount` 归零）。
        self.assertEqual(log.from_value, "25.00")
        self.assertEqual(log.to_value, "7.77")

    def test_the_order_row_is_locked_before_the_performance_row(self):
        """⭐ I4（终审修复轮）：`/performance/delete/` 统一锁序 —— 先锁 Order、再锁 Performance。

        这条边原来只有绩效行锁（Z1 的 TOCTOU 修复），订单行锁要等下面
        `apply_transition` 才拿到 ⇒ 与 `/assign/` 方向相反。判据是**SQL 顺序**。
        判别力：把 `soft_delete` 里的 `lock_order_row(perf.order)` 删掉 ⇒ 本用例红。
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.lock_order_helpers import assert_order_locked_before_performance

        with CaptureQueriesContext(connection) as captured:
            body = self._delete(self.main.id)
        self.assertEqual(body["code"], 0, body)
        assert_order_locked_before_performance(self, captured)
        # 哨兵：软删真的发生了（不是在一笔失败的调用上断锁序）。
        self.assertIsNotNone(self._perf(self.main.id).deleted_at)


class TakerUntouchedTests(_DeleteFixture):
    """T12：删得掉的那一档「不是当前接单人」⇒ 只软删、不动接单人。

    ⚠️ **R-9 之后 SALES 行已不可删**（`EmpType` 门，见 `EmpTypeGateTests`）⇒ 本类可达的
    那一档只剩「`user` 为空的 MAIN 行」；下面第一条用例按新语义改成**断言 SALES 被拒**，
    夹具不变（⛔ 没有删用例、没有放宽期望）。
    """

    def test_a_sales_row_is_refused_and_leaves_the_taker_and_the_order_status_alone(self):
        """SALES（销售提成）行 ⇒ 业务码拒绝；接单人 / 订单状态 / 审计行都没动。"""
        body = self._delete(self.sales.id)
        self.assertEqual(body["code"], 1014, body)
        self.assertEqual(body["msg"], "只能删除接单人的绩效记录")
        row = self._perf(self.sales.id)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")
        self.assertEqual(row.user_id, self.sales_user.id)
        self.assertEqual(self._order().status, OrderStatus.IN_PROGRESS)
        main = self._perf(self.main.id)
        self.assertEqual(main.user_id, self.taker.id)
        self.assertIsNone(main.deleted_at)
        self.assertFalse(
            OperationLog.objects.filter(action="performance.delete").exists()
        )

    def test_deleting_a_main_row_without_a_taker_leaves_the_order_alone(self):
        Performance.objects.filter(pk=self.main.pk).update(user=None)
        body = self._delete(self.main.id)
        self.assertEqual(body["code"], 0, body)
        self.assertFalse(body["data"]["taker_cleared"])
        self.assertIsNotNone(self._perf(self.main.id).deleted_at)
        self.assertEqual(self._order().status, OrderStatus.IN_PROGRESS)


class EmpTypeGateTests(_DeleteFixture):
    """R-9：端点只接受 `emp_type == EmpType.MAIN`（打单工资）的行；SALES 行 ⇒ 业务码拒绝。

    判据 ③ 的对照在**同一次用例、同一个订单**上做：SALES 被拒之后，同一订单的 MAIN 行
    仍删得掉 ⇒ 钉住「不是一刀切拒绝」这个恒真陷阱（AGENTS §5-G）。
    """

    def test_the_main_row_of_the_same_order_still_deletes_in_the_same_run(self):
        refused = self._delete(self.sales.id)
        self.assertEqual(refused["code"], 1014, refused)
        self.assertIsNone(self._perf(self.sales.id).deleted_at)

        accepted = self._delete(self.main.id)
        self.assertEqual(accepted["code"], 0, accepted)
        self.assertEqual(
            accepted["data"], {"deleted": self.main.id, "taker_cleared": True}
        )
        self.assertIsNotNone(self._perf(self.main.id).deleted_at)
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        # SALES 行在两次请求里都没被动（拒绝那次 + MAIN 成功那次都没连带它）。
        self.assertIsNone(self._perf(self.sales.id).deleted_at)


class SettledUnionGateTests(_DeleteFixture):
    """T13：已结账 ⇒ `1014`，且**一行不写**。

    判据取 `settle_status` / `settled_at` / `settled_amount` 三字段的**并集**
    （存量行可能有「已结账但没写 `settled_at`」的形态）⇒ 三个字段**各自单独**命中都要拦。
    """

    def test_each_of_the_three_settled_columns_alone_is_refused(self):
        for field, value in (
            ("settle_status", SettleStatus.SETTLED),
            ("settled_at", timezone.now()),
            ("settled_amount", Decimal("7.77")),
        ):
            with self.subTest(field=field):
                Performance.objects.filter(pk=self.main.pk).update(
                    settle_status=SettleStatus.UNSETTLED,
                    settled_at=None,
                    settled_amount=None,
                )
                Performance.objects.filter(pk=self.main.pk).update(**{field: value})

                body = self._delete(self.main.id)
                self.assertEqual(body["code"], 1014, body)
                self.assertIn("已结账", body["msg"])

                row = self._perf(self.main.id)
                self.assertIsNone(row.deleted_at)
                self.assertEqual(row.deleted_by, "")
                self.assertEqual(row.user_id, self.taker.id)
                self.assertEqual(self._order().status, OrderStatus.IN_PROGRESS)
                self.assertFalse(
                    OperationLog.objects.filter(action="performance.delete").exists()
                )


class PerformanceListBoundaryTests(_DeleteFixture):
    """边界：软删后的行在绩效列表里看不见，但**行还在库里**。"""

    URL = "/api/business/performances/"

    def _ids(self):
        body = self.client.get(self.URL).json()
        self.assertEqual(body["code"], 0, body)
        return [row["id"] for row in body["data"]["items"]]

    def test_a_soft_deleted_row_disappears_from_the_performance_list(self):
        before = self._ids()
        # 前提判据：删之前两条**真的看得见**（否则「看不见」是恒真的假保证）。
        self.assertIn(self.main.id, before)
        self.assertIn(self.sales.id, before)

        # R-9 后端点只删得掉 MAIN 行 ⇒ 靶子从 SALES 改成 MAIN，SALES 改当对照行。
        # ⚠️ 删 MAIN 会连带清它的 `user`，而本列表还有 `user__isnull=False` 那道过滤
        #    ⇒ 本用例证明的是「端点删完，它在列表里看不见了」，**不是**单独隔离
        #    `deleted_at__isnull=True`（那道过滤的隔离判据在
        #    `tests/test_performance_row_soft_delete_read_paths.py::PerformanceListExclusionTests`，
        #    那里用 ORM 直写软删一条 `user` 仍在的行）。
        self._delete(self.main.id)

        after = self._ids()
        self.assertNotIn(self.main.id, after)
        self.assertIn(self.sales.id, after)     # 对照：别的行仍在
        self.assertIsNotNone(self._perf(self.main.id).deleted_at)   # 行没被硬删


class PermissionGateTests(_DeleteFixture):
    """权限门（业务方 2026-09-25 裁定取代 R-8b 的「额外要 19」那一句）：
    软删**任何**绩效行 ⇒ `order.submit_payout_price`(1162) 这一道底门；
    连带清接手人**也只要** 1162。`order.clear_taker`(19) 不再单独构成软删权。

    类级门保持 `{1162, 19}` 的并集（粗门，表达不了「这一行是不是接单人那条」这个对象级条件），
    真正的判据在函数体里：1162 是唯一底门。逐条钉住：

    | # | 账号节点 | 目标行 | 期望 |
    |---|---|---|---|
    | ① | 只 19 | 非接单人行 | 403「你没有删除绩效的权限」且一行不写 |
    | ② | 只 19 | 接单人行 | 403（同样缺 1162） |
    | ③ | 只 1162 | 非接单人行 | 成功 |
    | ④ | 只 1162 | 接单人行 | **成功**且接手人被清（前这一档是 403） |
    | ⑤ | 1162 + 19 | 接单人行 | 成功 |
    """

    def _post(self, username, group, nodes, performance_id):
        client = make_logged_in_client(username=username, groups=(group,), nodes=nodes)
        return client.post(
            f"/api/business/performances/{performance_id}/delete/", {}
        )

    def test_a_submit_payout_price_only_client_may_delete_a_non_taker_row(self):
        """③ 持 1162 删非接单人行 ⇒ 成功。

        ⚠️ **R-9 后「非接单人行」只剩 `user` 为空的 MAIN 行**（SALES 行会被 `EmpType` 门拒）
        ⇒ 夹具从 SALES 行改成「MAIN 行 + `user=None`」；期望不变
        （`taker_cleared=false`、只软删）。
        """
        Performance.objects.filter(pk=self.main.pk).update(user=None)
        response = self._post(
            "测试客服丙", "财务乙", ("order.submit_payout_price",), self.main.id
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["data"]["taker_cleared"], False)
        self.assertIsNotNone(self._perf(self.main.id).deleted_at)

    def test_a_clear_taker_only_client_is_403_on_a_non_taker_row_and_writes_nothing(self):
        """① 只持 `order.clear_taker`(19) 删**非接单人**行 ⇒ 403 且一行不写。

        R-8b：清接手人权**不蕴含**软删绩效权——否则只持 19 的账号能删任意
        （含别人的）SALES 提成行。
        """
        response = self._post(
            "测试客服戊", "财务戊", ("order.clear_taker",), self.sales.id
        )
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response.json()["msg"], "你没有删除绩效的权限")
        # 「一行不写」的判据：绩效行两列、归属人、审计行**三样都没动**。
        row = self._perf(self.sales.id)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")
        self.assertEqual(row.user_id, self.sales_user.id)
        self.assertFalse(
            OperationLog.objects.filter(action="performance.delete").exists()
        )

    def test_a_clear_taker_only_client_is_403_on_the_current_takers_row(self):
        """② 只持 19 删**接单人**行 ⇒ 403（它同样缺 1162，而 1162 是软删的底门）。"""
        response = self._post(
            "测试客服己", "财务己", ("order.clear_taker",), self.main.id
        )
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response.json()["msg"], "你没有删除绩效的权限")
        row = self._perf(self.main.id)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")
        self.assertEqual(row.user_id, self.taker.id)
        self.assertEqual(self._order().status, OrderStatus.IN_PROGRESS)
        self.assertFalse(
            OperationLog.objects.filter(action="performance.delete").exists()
        )

    def test_a_client_with_both_nodes_may_delete_and_clear_the_takers_row(self):
        """⑤ 持 1162 + 19 删接单人行 ⇒ 成功（19 在这里只是冗余，不再是必需项）。"""
        response = self._post(
            "测试客服庚",
            "财务庚",
            ("order.submit_payout_price", "order.clear_taker"),
            self.main.id,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["taker_cleared"], True)
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)

    def test_a_client_without_either_node_is_403_and_writes_nothing(self):
        response = self._post("测试无权限甲", "财务丙", ("order.list",), self.sales.id)
        self.assertEqual(response.status_code, 403, response.content[:300])
        row = self._perf(self.sales.id)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.user_id, self.sales_user.id)

    def test_a_submit_payout_price_only_client_may_delete_and_clear_the_takers_row(self):
        """④ 只持 1162 删**当前接单人那条 MAIN 行** ⇒ 成功且接手人被清。

        业务方裁定（spec §4.5 的 ⛔ 更正块）：清接手人**不需要** 19，
        只持 `order.submit_payout_price`(1162) 就能做这件事。本端点因此在该动作上
        合法代行 `order.clear_taker`(19)，状态机那一步不再拒——否则会回到
        「端点放行、状态机拒绝、整趟回滚」的双闸形状（前这一档是 403）。
        """
        response = self._post(
            "测试客服丁", "财务丁", ("order.submit_payout_price",), self.main.id
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["taker_cleared"], True)
        # 绩效行被软删（两列有值）、接手人被清（订单回未分配）。
        row = self._perf(self.main.id)
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "测试客服丁")
        self.assertIsNone(row.user_id)
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        # 审计行也在（同一步留痕）。
        log = OperationLog.objects.get(action="performance.delete")
        self.assertEqual(log.target_id, str(self.main.id))
        self.assertEqual(log.actor, "测试客服丁")


# ── T15 跨租户：两个 schema 的**目标行主键相同**，判据用订单号 / 用户名 ──────────
SCHEMA_B = "tp3_del_b"
DOMAIN_B = "tp3-del-b.test.local"

NO_A = "20260924000000000811"
NO_B = "20260924000000000812"
#: 只在 B 存在的第三条（A 打它的 id ⇒ 真的"找不到"）。
NO_B_ONLY = "20260924000000000813"
USER_A = "测试打手甲租户A"
USER_B = "测试打手乙租户B"
USER_B_ONLY = "测试打手乙租户B备"
#: 只在 B 建出来的职位名（`roles` 的租户隔离判据）。
ROLE_B_ONLY = "乙职位"


def _build_order_with_main(order_no, username, game_name):
    """在当前 schema 里建「一条 `IN_PROGRESS` 单 + 当前接单人那条 MAIN 行」。"""
    game = Game.objects.create(name=game_name)
    order = Order.objects.create(
        game=game,
        order_no=order_no,
        status=OrderStatus.IN_PROGRESS,
        total_price=Decimal("200.00"),
        hall_price=Decimal("100.00"),
    )
    user = User.objects.create_user(username=username)
    StaffProfile.objects.create(
        user=user, commission_mode=PERCENT_MODE, commission_value=Decimal("25.00")
    )
    performance = Performance.objects.create(
        order=order,
        user=user,
        emp_type=EmpType.MAIN,
        temp_amount=Decimal("25.00"),
        bonus_amount=Decimal("7.77"),
    )
    return order, user, performance


class CrossTenantDeleteTests(TenantTestCase):
    """以租户 A 的登录态打绩效 id：只动 A 自己的行；B 的行一个字不动。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="绩效删除隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant_a)
        self.order_a, self.taker_a, self.perf_a = _build_order_with_main(
            NO_A, USER_A, "测试游戏A"
        )
        self.client_a = make_logged_in_client(
            username="测试客服A", groups=("客服",), nodes=DELETE_NODES
        )

    def _delete_a(self, performance_id):
        return self.client_a.post(
            f"/api/business/performances/{performance_id}/delete/"
        ).json()

    def test_a_same_id_row_in_b_is_not_touched_and_a_b_only_id_is_refused(self):
        # B 侧造两条：目标那条（先建 ⇒ 主键与 A 的目标行**相同**）与一条只在 B 存在的。
        with tenant_context(self.tenant_b):
            _, _, perf_b = _build_order_with_main(NO_B, USER_B, "测试游戏B")
            _, _, perf_b_only = _build_order_with_main(NO_B_ONLY, USER_B_ONLY, "测试游戏B备")
            b_target_id, b_only_id = perf_b.id, perf_b_only.id
            # 自证判据的前提：危险形态（两个 schema 同 id）真的被构造出来了。
            self.assertEqual(b_target_id, self.perf_a.id)
            self.assertNotEqual(b_only_id, self.perf_a.id)
        connection.set_tenant(self.tenant_a)

        # ── 判据 1：同 id 的请求只动 A 自己的行 ──
        body = self._delete_a(b_target_id)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["deleted"], self.perf_a.id)
        with tenant_context(self.tenant_a):
            self.assertIsNotNone(Performance.objects.get(pk=self.perf_a.id).deleted_at)
            self.assertEqual(Order.objects.get(order_no=NO_A).status, OrderStatus.UNASSIGNED)
            self.assertTrue(
                OperationLog.objects.filter(
                    action="performance.delete", order_no=NO_A
                ).exists()
            )
        connection.set_tenant(self.tenant_a)

        # ── B 的目标行按**订单号 / 用户名**查证：四列都没动 ──
        with tenant_context(self.tenant_b):
            row = Performance.objects.get(order__order_no=NO_B)
            self.assertEqual(row.user.username, USER_B)
            self.assertIsNone(row.deleted_at)
            self.assertEqual(row.order.status, OrderStatus.IN_PROGRESS)
            self.assertFalse(
                OperationLog.objects.filter(
                    action="performance.delete", order_no=NO_B
                ).exists()
            )
        connection.set_tenant(self.tenant_a)

        # ── 判据 2：只在 B 存在的那个 id ⇒ A 里找不到，且 B 的那行不变 ──
        body = self._delete_a(b_only_id)
        self.assertEqual(body["code"], 1002, body)
        with tenant_context(self.tenant_b):
            row = Performance.objects.get(order__order_no=NO_B_ONLY)
            self.assertEqual(row.user.username, USER_B_ONLY)
            self.assertIsNone(row.deleted_at)
            self.assertEqual(row.order.status, OrderStatus.IN_PROGRESS)
        connection.set_tenant(self.tenant_a)

    def test_the_roles_list_is_scoped_to_the_current_tenant(self):
        """`assignee-options` 的 `roles` 也是租户内的职位名（B 自建的职位不出现在 A）。"""
        with tenant_context(self.tenant_a):
            Role.objects.create(name="甲职位", group=Group.objects.create(name="甲职位"))
        with tenant_context(self.tenant_b):
            Role.objects.create(name=ROLE_B_ONLY, group=Group.objects.create(name=ROLE_B_ONLY))
        connection.set_tenant(self.tenant_a)

        body = self.client_a.get("/api/business/orders/assignee-options/").json()
        self.assertEqual(body["code"], 0, body)
        roles_a = set(body["data"]["roles"])
        self.assertIn("甲职位", roles_a)
        self.assertNotIn(ROLE_B_ONLY, roles_a)
