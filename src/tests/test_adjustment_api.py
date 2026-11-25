"""P4-11：人工修正端点（`POST /performances/{id}/adjustments`）+ `/adjust` 已结账拒绝复核。

依据：spec §7（端点与错误码表）· §3.3（`PerformanceAdjustment`）· §6（权限表）·
§9 #3 / #8 / #16 / #17 · §10 #14；计划 §P4-11 与它的「执行期修订（裁定 V/W/X/Y）」。

⭐ **TDD 第 1 步的判据（已实测，见报告）**：本文件先只放 M6b + M1 跑了一遍——
**M1 在任何生产代码改动之前就已经绿**（`/adjust` 的已结账判定由 B1-5 落地，
`performance_views.py` 在本任务里**一个字节都不改**），而 M6b 以
`TypeError: PerformanceAdjustment() got unexpected keyword arguments: 'request_id'` 红。
那条实测输出就是「本任务只复核、没有重复实现」的证据。

⚠️ 期望值一律写**字面值**（E-0024）：业务码写 `1000` / `1010` / `1014`、状态写
`"未结账"`、金额写 `Decimal("-30.00")`——不引被测模块导出的常量（自指断言不会红）。
⚠️ 留痕断言打在**新表 `OperationLog`** 的数据库行上（E-0009；M3a 后 P4 写入点已迁到
`apps/audit/log.py::record(...)`）；本文件**不写**任何日志断言。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, Permission, User
from django.db import IntegrityError
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog,
    EmpType,
    Game,
    Order,
    OrderRefund,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.core.error_codes import ErrorCode
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: spec §7 的两条路径；调整记录链**没有**尾斜杠，`/adjust/` 是 P3b 的既有形状。
ADJUSTMENTS_URL = "/api/business/performances/{performance_id}/adjustments"
ADJUST_URL = "/api/business/performances/{performance_id}/adjust/"

#: ⭐ 订单日志人名线 spec §3-A2 / 判据 5：归属人的**昵称**。
#: ⚠️ 与登录账号 `p4-adj-taker` **不同形**——两者相同时「落的是昵称还是账号」会**假绿**。
TAKER_NICKNAME = "测试昵称打手癸"
REFUND_URL = "/api/business/orders/{order_no}/refunds"
#: 两个闸门节点（spec §6 的表）：读 = `finance.payroll`；人工修正 = **同结账**。
READ_NODE = "finance.payroll"
WRITE_NODE = "finance.change_settlement_status"


class _AdjustmentFixture(TenantTestCase):
    """建单 / 建绩效 / 发请求的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 管理员登录：`has_node_now` 对 `is_superuser` 有短路 ⇒ 业务逻辑用例不必先搭角色目录。
        self.admin = make_logged_in_client(username="p4-adj-admin", is_superuser=True)
        # ⭐ 订单日志人名线 spec §3-A2：归属人的**昵称**与登录账号**不同形**
        # ⇒ 「`target_name` 落的是账号名称还是登录账号」这条判据才有判别力（spec §5 的纪律）。
        self.taker = User.objects.create_user(
            username="p4-adj-taker", first_name=TAKER_NICKNAME
        )
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self._seq = 0

    def _order(self, *, status=OrderStatus.IN_PROGRESS, received=None, hall="88.00",
               total="0.00"):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092200000{self._seq:07d}",
            status=status,
            received_price=None if received is None else Decimal(received),
            hall_price=None if hall is None else Decimal(hall),
            total_price=Decimal(total),
        )

    def _perf(self, order=None, *, settle=SettleStatus.UNSETTLED, with_taker=True):
        order = order or self._order()
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=self.taker if with_taker else None,
            temp_amount=Decimal("88.00"),
            settle_status=settle,
        )

    def _cs_client(self, username="p4-adj-cs"):
        """客服登录态 + `business.override_taker_price`（`/adjust` 带 `taker_price` 时要求）。

        📌 **夹具声明它要过的门**：`/adjust` 的类级节点门是
        `PerformanceViewSet.PERFORMANCE_PERMISSIONS["adjust"] = ("order.submit_payout_price",)`。
        `adjust()` 没有客服职位门，节点门是唯一判据，`groups=("客服",)` 只影响
        `Role` 的名字、**不参与判据**；`nodes=` 才是放行来源。
        这是「声明夹具需要的门」，不是放宽门：门本身一个字节都没改。
        ⚠️ body 层的 `can_edit_taker_price`（= 不被 `order.restrict_edit_taker_price` 限制）
        在本夹具下**自然为真**：这里只挂 `order.submit_payout_price`，没有那条 flag。
        上面那行 legacy `business.override_taker_price` 保留下来存查，但它**已不参与判据**
        （D-0028 冻结；body 层读 flag 的反面）。
        """
        client = make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.submit_payout_price",)
        )
        # ⭐ 按游戏权限收窄：`/adjust` 走 `_base_queryset` ⇒ 请求方
        # 必须显式持有本夹具的游戏，否则 `get_object()` 找不到那张绩效（200 + 1002）。
        grant_games(User.objects.get(username=username), self.game)
        User.objects.get(username=username).user_permissions.add(
            Permission.objects.get(codename="override_taker_price")
        )
        return client

    def _adjust(self, client, perf, payload):
        return client.post(
            ADJUST_URL.format(performance_id=perf.id),
            payload,
            content_type="application/json",
        )

    def _post(self, perf, payload, client=None):
        """`POST /performances/{id}/adjustments`（人工修正）。"""
        return (client or self.admin).post(
            ADJUSTMENTS_URL.format(performance_id=perf.id),
            payload,
            content_type="application/json",
        )

    def _grant(self, username, role_name, *codes):
        """照抄 `tests/test_refund_partial.py:558` 的越权夹具手法（⛔ 不自己发明第二套）。"""
        nodes = [
            PermissionNode.objects.get_or_create(
                code=code,
                defaults={
                    "name": code,
                    "note": f"测试节点 {code}",
                    "kind": NodeKind.API,
                    "level": 1,
                },
            )[0]
            for code in codes
        ]
        role = Role.objects.create(
            name=role_name, group=Group.objects.create(name=f"g-{username}"[:150])
        )
        role.permissions.set(nodes)
        User.objects.get(username=username).groups.add(role.group)


class RequestIdNullCoexistenceTests(_AdjustmentFixture):
    """**M6b**（裁定 V 的前提）：PostgreSQL 的唯一索引对 `NULL` 是 `NULLS DISTINCT`
    ⇒ 退款联动会建出的**多条** `request_id IS NULL` 的行必须能共存。

    ⚠️ 用 `objects.create()` 直接建两行，**不经过端点**——端点强制要求 `request_id`
    （入参契约），而退款联动的行不走本端点、也就没有客户端幂等键。
    """

    def test_two_rows_with_a_null_request_id_can_coexist(self):
        perf = self._perf()

        first = PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-30.00"),
            reason="退款联动的第一行",
            operator="sys",
            settle_status=SettleStatus.UNSETTLED,
            request_id=None,
        )
        second = PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("-40.00"),
            reason="退款联动的第二行",
            operator="sys",
            settle_status=SettleStatus.UNSETTLED,
            request_id=None,
        )

        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=perf).count(), 2
        )
        self.assertIsNone(
            PerformanceAdjustment.objects.get(pk=first.pk).request_id
        )
        self.assertIsNone(
            PerformanceAdjustment.objects.get(pk=second.pk).request_id
        )


class AdjustSettledRegressionTests(_AdjustmentFixture):
    """**M1**（§9 #8）：`/adjust` 的「已结账 ⇒ 拒绝」是 **B1-5 已落地**的判定。

    本类**只做复核 + 回归**——`performance_views.py` 在本任务里**一个字节都不改**
    （brief 的 Files 行：⛔ 不改 `performance_views.py`）。
    判别力：码值必须**恰是 1014**、且**不是** 1006（spec §12.2 明令不要用 P4 的 `1006`）；
    原金额必须未变；**未结账的同一操作必须成功**（防「一律拒绝」假通过）。
    """

    def test_the_settled_rejection_is_1014_not_1006_and_leaves_the_amount_alone(self):
        perf = self._perf()
        perf.temp_amount = Decimal("17.60")
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["temp_amount", "settle_status"])
        client = self._cs_client()

        body = self._adjust(
            client, perf, {"taker_price": "66.00", "reason": "已结账回归"}
        ).json()

        self.assertEqual(int(body["code"]), 1014)
        # ⭐ 独立性断言（spec §12.2「不许用 1006」的**显式**判据；保留意图、钉常量）。
        # ⚠️ 它在**现值**上被上一行蕴含，但那正是「一句话两处保证」——
        #    `PERFORMANCE_STATE_INVALID` 哪天被改回 `1006` 时，这一行是唯一会红的地方。
        # ⚠️ 这是本文件「期望值不引被测常量」那条纪律的**有意例外**：这里断的**就是**
        #    常量本身的值（不像 `assertEqual(f(x), SOME_CONST)` 那样把结果与常量对拍）。
        self.assertNotEqual(int(ErrorCode.PERFORMANCE_STATE_INVALID), 1006)
        # ⭐ 钉**整句**：`assertIn("结账", msg)` 对
        #    「**未**结账…」也会通过，等于没断「拒绝的是已结账」这件事。
        # ⚠️ `/adjust/` 这句与另三条写价路径同族：「请先撤销结账再改」。
        self.assertEqual(body["msg"], "已结账的绩效不能改金额；请先撤销结账再改")
        perf.refresh_from_db()
        self.assertIsNone(perf.taker_price)
        self.assertEqual(perf.temp_amount, Decimal("17.60"))

    def test_the_same_adjustment_succeeds_while_unsettled(self):
        """正向对照：同一操作在**未结账**时必须成功并重算出派生金额。

        `66.00 × 20% = 13.20`（字面值；不引被测模块导出的常量）。
        """
        perf = self._perf()
        client = self._cs_client(username="p4-adj-cs-forward")

        body = self._adjust(
            client, perf, {"taker_price": "66.00", "reason": "未结账对照"}
        ).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.taker_price, Decimal("66.00"))
        self.assertEqual(perf.temp_amount, Decimal("13.20"))


class ManualAdjustmentHappyPathTests(_AdjustmentFixture):
    """**M2**：人工修正正路——负值是**正常输入**（D-0015），不是异常。

    ⚠️ 订单上**故意放一条已有的退款行**：`refund` 必须仍是 `None`——
    人工修正**不挂在任何退款事件上**（spec §3.3）。只断「请求没带 refund」证明不了这一点，
    因为服务端完全可以自己去「顺手关联」订单上那条退款（那会让审计对不上账）。
    """

    def test_a_manual_adjustment_row_is_created_with_the_expected_shape(self):
        perf = self._perf()
        OrderRefund.objects.create(
            order=perf.order,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"),
            reason="这张单此前退过款",
            operator="someone",
            request_id="m2-existing-refund",
        )

        body = self._post(
            perf,
            {"amount": "-30.00", "reason": "客户投诉补偿", "request_id": "req-m2"},
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)
        row = PerformanceAdjustment.objects.get()
        self.assertEqual(row.kind, "MANUAL")
        self.assertIsNone(row.refund_id)
        self.assertEqual(row.settle_status, "未结账")
        self.assertEqual(row.amount, Decimal("-30.00"))
        self.assertEqual(row.operator, "p4-adj-admin")
        self.assertEqual(row.performance_id, perf.id)
        self.assertIsNone(row.settled_at)
        self.assertEqual(row.request_id, "req-m2")
        # 出参形状：既有序列化器 + 只读 `request_id`。
        self.assertEqual(body["data"]["performance_id"], perf.id)
        self.assertEqual(body["data"]["kind"], "MANUAL")
        self.assertEqual(body["data"]["request_id"], "req-m2")
        self.assertEqual(Decimal(body["data"]["amount"]), Decimal("-30.00"))


class ZeroAmountTests(_AdjustmentFixture):
    """**M5**（裁定 X）：`amount == 0` **只拒 0** ⇒ `1000`；负值必须成功。

    ⛔ 这一组守的是「不许顺手写成 `<= 0` / `min_value`」——spec §7 明写 `amount` 带符号，
    负值是本端点的正常输入（也是「负应付的唯一出路」）。
    """

    def test_a_zero_amount_is_rejected_with_1000_and_writes_nothing(self):
        perf = self._perf()

        for raw, request_id in (("0.00", "req-m5a"), (0, "req-m5b")):
            with self.subTest(amount=raw):
                body = self._post(
                    perf,
                    {"amount": raw, "reason": "零金额", "request_id": request_id},
                ).json()

                self.assertEqual(body["code"], 1000)
                self.assertEqual(PerformanceAdjustment.objects.count(), 0)
                self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_negative_amount_is_accepted(self):
        perf = self._perf()

        body = self._post(
            perf,
            {"amount": "-30.00", "reason": "冲减", "request_id": "req-m5c"},
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            PerformanceAdjustment.objects.get().amount, Decimal("-30.00")
        )

    def test_a_positive_amount_is_accepted(self):
        """正向的另一端：补正为正（带符号，两个方向都能走）。"""
        perf = self._perf()

        body = self._post(
            perf,
            {"amount": "12.34", "reason": "补正", "request_id": "req-m5d"},
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            PerformanceAdjustment.objects.get().amount, Decimal("12.34")
        )


class RequestIdIdempotencyTests(_AdjustmentFixture):
    """**M6**：`request_id` 幂等 + `NULL` 共存 + 幂等范围是**本表**。

    幂等的**唯一**来源是那个具名唯一约束 `uniq_perf_adjustment_request_id`
    （裁定 X：**不做 `exists()` 预查**——有预查则「去掉唯一约束」的判别力实验不会红）。
    """

    def test_the_same_request_id_is_rejected_the_second_time_with_only_one_row(self):
        """**M6a**。"""
        perf = self._perf()

        first = self._post(
            perf,
            {"amount": "-30.00", "reason": "第一次", "request_id": "req-m6a"},
        ).json()
        second = self._post(
            perf,
            {"amount": "-40.00", "reason": "第二次", "request_id": "req-m6a"},
        ).json()

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 1010)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=perf).count(), 1
        )
        self.assertEqual(
            PerformanceAdjustment.objects.get().amount, Decimal("-30.00")
        )
        self.assertEqual(OperationLog.objects.filter(action="performance.adjust_manual").count(), 1)

    def test_a_different_request_id_creates_a_second_row(self):
        """**M6c**：防「一律拒绝」假通过。"""
        perf = self._perf()

        first = self._post(
            perf,
            {"amount": "-30.00", "reason": "第一次", "request_id": "req-m6c-1"},
        ).json()
        second = self._post(
            perf,
            {"amount": "-40.00", "reason": "第二次", "request_id": "req-m6c-2"},
        ).json()

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=perf).count(), 2
        )
        self.assertEqual(
            sorted(
                PerformanceAdjustment.objects.values_list("amount", flat=True)
            ),
            [Decimal("-40.00"), Decimal("-30.00")],
        )

    def test_a_request_id_already_used_by_a_refund_can_still_be_used_here(self):
        """**M6d**（裁定 X）：幂等范围**只是本表**——两张表各自唯一。

        同一个 `request_id` **先**用于一次退款（`OrderRefund`，成功）**再**用于一次人工修正
        ⇒ **两次都成功**。这条是「将来有人把它改成全局幂等键」的哨兵。
        """
        order = self._order(
            status=OrderStatus.COMPLETED, received="1.00", hall=None, total="100.00"
        )
        sales_user = User.objects.create_user(username="p4-adj-sales")
        sales = Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=sales_user,
            temp_amount=Decimal("0.00"),
            bonus_amount=Decimal("8.00"),
            commission_rate=Decimal("20.00"),
        )
        shared = "shared-request-id"

        refund_body = self.admin.post(
            REFUND_URL.format(order_no=order.order_no),
            {"refund_total": "50.00", "reason": "退款用掉这个 request_id", "request_id": shared},
            content_type="application/json",
        ).json()
        manual_body = self._post(
            sales,
            {"amount": "-30.00", "reason": "人工修正用同一个", "request_id": shared},
        ).json()

        self.assertEqual(refund_body["code"], 0)
        self.assertEqual(manual_body["code"], 0)
        self.assertEqual(OrderRefund.objects.filter(request_id=shared).count(), 1)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(request_id=shared).count(), 1
        )
        self.assertEqual(
            PerformanceAdjustment.objects.filter(request_id=shared).get().kind,
            "MANUAL",
        )


class ManualAdjustmentAuditTests(_AdjustmentFixture):
    """**M8**（§9 #17 / E-0009）：留痕断言打在**新表 `OperationLog`** 的数据库行上
    （⛔ 不用 `assertLogs`）。

    裁定 Y 的取值：`from_value = ""`（**新增**没有变更前值）、`to_value = str(amount)`。
    ⭐ M3a：动作是 `performance.adjust_manual`（**不是** `performance.adjust`，Ruling N）；
    `performance_id` 的承重语义改由 R11 的 `target_kind="user"` + `target_id`/`target_name`
    承载（`OperationLog` 没有 `performance_id` 列）。
    """

    def test_the_audit_row_lands_in_the_database_with_the_exact_values(self):
        perf = self._perf()

        body = self._post(
            perf,
            {"amount": "-30.00", "reason": "客户投诉补偿", "request_id": "req-m8"},
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(OperationLog.objects.count(), 1)
        row = OperationLog.objects.get()
        self.assertEqual(row.action, "performance.adjust_manual")
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(perf.user_id))
        # ⭐ spec §3-A2）：`target_name` 是**账号名称**（昵称），⛔ 不是登录账号。
        # ⚠️ 两向断言：昵称在、账号不在（只断前者的话「两个都写进去」也能绿）。
        self.assertEqual(row.target_name, TAKER_NICKNAME)
        self.assertNotEqual(row.target_name, perf.user.get_username())
        self.assertIn(TAKER_NICKNAME, row.summary)
        self.assertNotIn(perf.user.get_username(), row.summary)
        self.assertEqual(row.order_no, perf.order.order_no)
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.to_value, "-30.00")
        self.assertEqual(row.actor, "p4-adj-admin")
        self.assertEqual(row.actor_id, User.objects.get(username="p4-adj-admin").pk)
        self.assertEqual(row.reason, "客户投诉补偿")
        # 旧出口退役：这条端点**一行都不许**再写旧表。
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_the_rejected_duplicate_leaves_no_second_audit_row(self):
        """幂等被拒的那一次**不留痕**（否则审计会记两次「发生了什么」）。"""
        perf = self._perf()
        self._post(perf, {"amount": "-30.00", "reason": "第一次", "request_id": "req-m8b"})

        second = self._post(
            perf, {"amount": "-40.00", "reason": "第二次", "request_id": "req-m8b"}
        ).json()

        self.assertEqual(second["code"], 1010)
        self.assertEqual(
            OperationLog.objects.filter(action="performance.adjust_manual").count(), 1
        )


class SettledPerformanceCanStillBeAdjustedTests(_AdjustmentFixture):
    """**M3** ⭐：「**即使绩效行已结账也能修正**」（spec §3.3 的坏账论证）。

    已结账的绩效**不得改金额**（改走新增调整记录），但**必须能追加**一条人工修正记录——
    否则「已结账绩效上的修正额**永远不会被结账**」这个坏账就没有出路。
    新行强制 `未结账`（它进入下一次结账），而绩效行本身**一个字段都不动**。
    """

    def test_a_settled_performance_can_still_receive_a_manual_adjustment(self):
        perf = self._perf(settle=SettleStatus.SETTLED)

        body = self._post(
            perf,
            {"amount": "-30.00", "reason": "已结账后追加修正", "request_id": "req-m3"},
        ).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        row = PerformanceAdjustment.objects.get()
        self.assertEqual(row.settle_status, "未结账")
        self.assertEqual(row.settled_at, None)
        self.assertEqual(row.amount, Decimal("-30.00"))


class ServerSideFieldsAreNotOverridableTests(_AdjustmentFixture):
    """**M4 + M4b**（裁定 W）：「请求能决定什么」是**服务端的决定**，不是请求的形状。

    载荷里塞 `settle_status=已结账` / `kind=PARTIAL_REFUND` / `refund=<已有退款 id>`，
    建出来的行**必须**仍是 `未结账` / `MANUAL` / `refund IS NULL`——
    请求若能覆盖就会造出「人工修正伪造成退款冲减」的行（审计对不上账）。
    """

    def test_settle_status_is_forced_to_unsettled(self):
        perf = self._perf()

        body = self._post(
            perf,
            {
                "amount": "-30.00",
                "reason": "试图覆盖结账状态",
                "request_id": "req-m4",
                "settle_status": "已结账",
            },
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(PerformanceAdjustment.objects.get().settle_status, "未结账")

    def test_kind_and_refund_cannot_be_overridden(self):
        """**M4b**：`kind` / `refund` 由服务端决定（⛔ 不由请求体）。"""
        perf = self._perf()
        refund = OrderRefund.objects.create(
            order=perf.order,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"),
            reason="既有的退款行",
            operator="someone",
            request_id="existing-refund",
        )

        body = self._post(
            perf,
            {
                "amount": "-30.00",
                "reason": "试图伪造成退款冲减",
                "request_id": "req-m4b",
                "settle_status": "已结账",
                "kind": "PARTIAL_REFUND",
                "refund": refund.id,
            },
        ).json()

        self.assertEqual(body["code"], 0)
        row = PerformanceAdjustment.objects.get()
        self.assertEqual(row.settle_status, "未结账")
        self.assertEqual(row.kind, "MANUAL")
        self.assertIsNone(row.refund_id)


class MalformedInputTests(_AdjustmentFixture):
    """**M7**（§9 #16 / E-0010）：每类畸形入参各一条 ⇒ **200 + 业务码**，且**零新增行**。

    ⚠️ 本路由带路径转换器 ⇒ `test_no_route_500` 的显式路由腿**跳过它**（假红防护），
    所以「这类请求不会 500」必须由本文件自己钉住。
    """

    def _assert_rejected(self, payload):
        """畸形入参 ⇒ 200 + `1000`，且**零新增行**。

        ⚠️ 本夹具**不收 `request_id` 参数**（清单 29a / P4-11 评审 Minor 1）：
        那个形参从未被用过，6 个调用点却逐一传它——一个「看起来在核对幂等键」的假信号。
        畸形的 `request_id` 本身就是载荷里的一个字段，不需要再传一遍。
        """
        perf = self._perf()
        response = self._post(perf, payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_non_numeric_amount_is_a_business_error(self):
        self._assert_rejected(
            {"amount": "abc", "reason": "非数字", "request_id": "bad-1"}
        )

    def test_an_over_wide_amount_is_a_business_error(self):
        self._assert_rejected(
            {"amount": "1234567890123.00", "reason": "超列宽", "request_id": "bad-2"}
        )

    def test_a_missing_reason_is_a_business_error(self):
        self._assert_rejected({"amount": "-30.00", "request_id": "bad-3"})

    def test_an_over_long_reason_is_a_business_error(self):
        self._assert_rejected(
            {"amount": "-30.00", "reason": "长" * 256, "request_id": "bad-4"}
        )

    def test_an_over_long_request_id_is_a_business_error(self):
        self._assert_rejected(
            {"amount": "-30.00", "reason": "超长 request_id", "request_id": "r" * 65}
        )

    def test_a_missing_request_id_is_a_business_error(self):
        self._assert_rejected({"amount": "-30.00", "reason": "缺 request_id"})


class AdjustmentPermissionTests(_AdjustmentFixture):
    """**M9**（§9 #3）：两个方法**两个节点**，两个方向各成立（spec §6 的表 / 裁定 W）。

    ⚠️ **必须用非管理员账号**：`has_node_now` 对 `is_superuser` 有**短路**（平面守卫在前），
    拿超管测闸门等于没测。夹具手法照抄 `tests/test_refund_partial.py:558`。
    ⚠️ 403 断 `code == 403` **且** `msg` 可辨（spec §17.4 #4）——
    两句 `msg` 分别对应「读被拒」与「写被拒」，两边都用同一句就说明分派没做。
    """

    def _payload(self, request_id):
        return {"amount": "-30.00", "reason": "越权用例", "request_id": request_id}

    def test_the_write_node_holder_can_create(self):
        perf = self._perf()
        client = make_logged_in_client(username="p4-adj-writer")
        self._grant("p4-adj-writer", "结账角色", WRITE_NODE)
        # ⭐ 按游戏权限收窄：人工修正取绩效那一步按**请求方**的游戏权限
        # 收窄 ⇒ 这个夹具显式声明它持有本夹具的游戏（否则 `1000`「绩效不存在或不可见」）。
        grant_games(User.objects.get(username="p4-adj-writer"), self.game)

        response = self._post(perf, self._payload("perm-write"), client=client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)

    def test_the_read_node_holder_cannot_create_and_gets_the_write_message(self):
        perf = self._perf()
        client = make_logged_in_client(username="p4-adj-reader")
        self._grant("p4-adj-reader", "查看角色", READ_NODE)

        response = self._post(perf, self._payload("perm-read"), client=client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["msg"], "你没有人工修正绩效调整记录的权限")
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)

    def test_the_read_node_holder_can_list(self):
        perf = self._perf()
        client = make_logged_in_client(username="p4-adj-reader-ok")
        self._grant("p4-adj-reader-ok", "查看角色2", READ_NODE)
        # 同 `test_the_write_node_holder_can_create`：不声明游戏时这条链恒空，
        # 用例只剩「code == 0」，判别力归零（不是放宽，是补前置）。
        grant_games(User.objects.get(username="p4-adj-reader-ok"), self.game)

        response = client.get(ADJUSTMENTS_URL.format(performance_id=perf.id))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_the_write_node_holder_cannot_list_and_gets_the_read_message(self):
        perf = self._perf()
        client = make_logged_in_client(username="p4-adj-writer-only")
        self._grant("p4-adj-writer-only", "结账角色2", WRITE_NODE)

        response = client.get(ADJUSTMENTS_URL.format(performance_id=perf.id))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["msg"], "你没有查看绩效调整记录的权限")

    def test_a_user_without_any_node_is_denied(self):
        """无节点账号 ⇒ 403 **且 `msg` 可辨**（清单 29c / P4-11 评审 Minor 3）。

        ⚠️ 只断 403 与同组其它用例不一致：403 有多个来源（CSRF、认证失败…），
        断不出权限语义的 403 会把别的原因当成「无权限」通过（spec §17.4 #4）。
        """
        perf = self._perf()
        client = make_logged_in_client(username="p4-adj-no-node")

        response = self._post(perf, self._payload("perm-none"), client=client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["msg"], "你没有人工修正绩效调整记录的权限")
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)

    def test_anonymous_request_is_401_not_403(self):
        """未认证 ⇒ **401**（spec §7 首段的分层：401 与 403 不能混）。

        这条守的是 `permission_denied` 的**转调写法**：它给 403 换了一句中文 `msg`，
        而实现必须转调 `super()`——DRF 在 `permission_denied` 里先判「有没有认证成功」，
        自己直接 `raise PermissionDenied` 会把匿名请求也变成 403。
        """
        perf = self._perf()
        url = ADJUSTMENTS_URL.format(performance_id=perf.id)
        anonymous = Client(SERVER_NAME="tenant.test.com")

        post = anonymous.post(url, self._payload("perm-anon"), content_type="application/json")
        get = anonymous.get(url)

        self.assertEqual(post.status_code, 401)
        self.assertEqual(get.status_code, 401)


class AuditFailureRollsBackTheAdjustmentTests(_AdjustmentFixture):
    """⭐ 清单 28（spec §9 #14 / P4-11 评审 Minor 1）：**审计写失败 ⇒ 调整行也回滚**。

    ⚠️ 为什么必须有这条：原有的 `UnexpectedIntegrityErrorTests` 打的是
    `PerformanceAdjustment.objects.create` 抛错——那一刻**还没有任何已写入的行**，
    它证明不了「**第二笔**写（审计）失败时第一笔回滚」。而 §8 表把「调整行 + 审计」
    列成**同事务的两笔写**，§9 #14 要求每一行各有一条。
    ⚠️ 桩打在**被查找的那个模块**上（E-0029）：`adjustment_views` 是
    `from apps.audit.log import record` ⇒ 符号住在
    `apps.business.api.adjustment_views`。
    （M3a 前这里打的是 `apps.business.api.adjustment_views.log_manual_adjustment`；
    旧出口退役后按同一条 E-0029 规则换成 `record`——**被查找的位置**，不是定义它的模块。）
    ⚠️ 判据是**数据库行数 = 0**（E-0009：断言打在真实出口上），不是「返回了错误码」。
    """

    def test_a_failing_audit_write_leaves_zero_adjustment_rows(self):
        perf = self._perf()
        self.admin.raise_request_exception = False

        with mock.patch(
            "apps.business.api.adjustment_views.record",
            side_effect=RuntimeError("审计写失败（探针）"),
        ):
            response = self._post(
                perf,
                {
                    "amount": "-30.00",
                    "reason": "审计失败要回滚",
                    "request_id": "req-audit-rollback",
                },
            )

        # 第一笔写（调整行）必须被第二笔的失败一起回滚。
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)
        # 未预期异常 ⇒ 不是 200（响亮地失败；具体码由全局处理器决定，这里只断不是业务成功）。
        self.assertNotEqual(response.status_code, 200)

    def test_the_same_request_succeeds_when_the_audit_write_works(self):
        """对照：**不接桩**时同一载荷成功并把两行都落库（防「这条路径根本走不通」假绿）。"""
        perf = self._perf()

        body = self._post(
            perf,
            {
                "amount": "-30.00",
                "reason": "审计正常",
                "request_id": "req-audit-ok",
            },
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)
        self.assertEqual(OperationLog.objects.filter(action="performance.adjust_manual").count(), 1)


class UnknownPerformanceTests(_AdjustmentFixture):
    """**M10**：不存在的 `performance_id` ⇒ `1000`（且零新增行）。

    与 `GET` 的处置**刻意不同**：读的 `performance_id` 只当筛选键（空链），
    而写路径要判存在——不然会 500（FK 指向不存在的行）。形状照 `refund_views.py:97-99`。
    """

    def test_an_unknown_performance_id_returns_1000_and_writes_nothing(self):
        response = self.admin.post(
            ADJUSTMENTS_URL.format(performance_id=999999),
            {"amount": "-30.00", "reason": "不存在的绩效", "request_id": "req-m10"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)


class UnexpectedIntegrityErrorTests(_AdjustmentFixture):
    """**变异 7 的判据**（E-0010）：别的 `IntegrityError` 必须**响亮地 500**。

    把「是 `IntegrityError`」当成「就是重复请求」会把「写坏了」伪装成「你重复提交了」
    （监控失明 + 下一个漏校验的字段静默失败）。判据：`1010` 的来源只能是**那处具名唯一约束**。

    ⚠️ 桩打在**被查找的那个模块**上（E-0029）：视图里的符号住在
    `apps.business.api.adjustment_views`（照 `tests/test_rbac_services.py:147` 的手法）。
    桩用的约束名是**别的**约束（`performance_adjustment_pkey`）。
    ⚠️ `raise_request_exception = False` 是拿 500 响应的必需设置（Django 5.2 默认 `True`
    会把未预期异常重新抛出；实测行号 `django/test/client.py:804`）。
    """

    def test_an_unrelated_integrity_error_is_a_500_not_a_duplicate_request_code(self):
        perf = self._perf()
        self.admin.raise_request_exception = False

        with mock.patch(
            "apps.business.api.adjustment_views.PerformanceAdjustment.objects.create",
            side_effect=IntegrityError(
                'duplicate key value violates unique constraint '
                '"performance_adjustment_pkey"'
            ),
        ):
            response = self._post(
                perf,
                {
                    "amount": "-30.00",
                    "reason": "别的完整性错误",
                    "request_id": "req-other-integrity",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_foreign_key_integrity_error_is_also_a_500(self):
        """⭐ 清单 30a：**外键**完整性错误（并发删掉绩效行）同样**响亮地 500**。

        P4-11 评审列出的未验证怀疑：若行在「取绩效」与「建调整行」之间被删，
        `PerformanceAdjustment` 的 FK 会抛 `IntegrityError`——它**不是**那处具名唯一约束，
        所以必须落到「原样 re-raise」这一支；**不能**被翻译成 `1010`（那会把「写坏了」
        伪装成「你重复提交了」），**也不能**静默吞掉（`AGENTS.md` §5 / E-0010）。
        ⚠️ P4/P3 **没有**删除 `Performance` 的端点 ⇒ 真实路径难以触发，这里是**打桩**钉住形状。
        """
        perf = self._perf()
        self.admin.raise_request_exception = False

        with mock.patch(
            "apps.business.api.adjustment_views.PerformanceAdjustment.objects.create",
            side_effect=IntegrityError(
                'insert or update on table "business_performanceadjustment" violates '
                'foreign key constraint "business_performanceadjustment_performance_id_fk"'
            ),
        ):
            response = self._post(
                perf,
                {
                    "amount": "-30.00",
                    "reason": "绩效被并发删除",
                    "request_id": "req-fk-integrity",
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)
