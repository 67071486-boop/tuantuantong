"""P4-9：退款端点（**设置累计退款额**）、退款记录、幂等 `1010` 与客服提成冲减。

依据：spec §5.1（五条前置校验 + 事务内四步，**2026-10-26 改准为绝对值口径**）·
§4.5（冲减算式与四条细节）· §7（端点与错误码）· §8（事务边界）·
§9 #1 / #2 / #3 / #5 / #14 / #16 / #17 · §10 #11 · 计划
`docs/plans/2026-10-26-refund-total-set.md` §5 的十条用例。

⭐ **2026-10-26 改语义（D-0312，`feat/refund-set`）**：入参是 `refund_total`
（**累计退款总额**，绝对值），不是「本次退款额」。台账 `OrderRefund(kind=SET)` 记的是
**带符号的差额** `total − 旧累计`；差额为 0 ⇒ 全空操作、响应 `changed:false`。
本文件的判据已按新口径逐条改准（旧「累加」口径的期望值全部作废）。

八条纪律（每条都有会红的用例）：

1. **只冲客服、不动打手**（§4.5 细节 2）：断言打手行的**每个字段**与退款前逐字段相等，
   不是「没报错」——`temp_amount` 默认就是 `0`，写 0 也会通过（spec §9 前言）。
2. **原行一个字节都不改**（§9 #2）：客服行的金额字段退款前后快照比对（除「算得出来、
   按新利润重算」那一档，见各用例的限定语）。
3. **幂等靠那处具名唯一约束**（§9 #5）：`1010` 是数据库约束拒出来的，
   没有 `exists()` 预查——预查会让「去掉约束必须变红」的判别力实验变成假保证。
   ⭐ 新口径下**差额为 0 不 INSERT** ⇒ 同一 `request_id` 在「设成同值」时**不会**回
   `1010`（另一条用例单独钉这个契约）。
4. **`mock.patch` 打在「被查找的那个模块」**（E-0029）：`refunds.py` 里
   `from ...performance_amount import commission_amount` ⇒ 符号住在 `apps.business.refunds`。
   桩**自证接上**：另起一条订单跑「不接桩时它应当成功」的对照。
5. **留痕断言打在新表 `OperationLog` 的数据库行上**（E-0009 / §9 #17；本项目**没有**
   `LOGGING` 配置）：本文件**不写**任何日志断言。
   ⭐ M3a：P4 退款写入点已从旧 `AuditLog` 迁到 `apps/audit/log.py::record(...)`
   ⇒ 留痕断言搬到新表（动作 `order.refund`），并额外断言**旧表零新增行**。
6. **期望值一律写字面值**（E-0024）：业务码写 `1000`/`1007`/`1008`/`1009`/`1010`，
   状态写 `"COMPLETED"`/`"未结账"`，金额写 `Decimal("-10.00")`——
   不引被测模块导出的常量（自指断言不会红）。
7. **毛收款 = 订单总价 `total_price`**（D-0142；`refunds._gross_received` **只读**总价）：
   `received_price` 从此**不是**任何量的来源——录单页那个隐藏框把它写成 `0`（C10），
   当毛收款用是**算错钱**。夹具里它一律是**陷阱值**（改它不该影响任何断言）。
   `total_price` 是 `null=False default=0.00` ⇒ `1008` 的两条来源是
   **`refund_total < 0`** 与 **`refund_total > total_price`**（不是「剩余可退额」——
   绝对值口径下上限就是毛收款本身）。
8. **越权用例必须用非管理员账号**（spec §6 末条）：`has_node_now` 对 `is_superuser` 有短路。
   本端点的闸门是**节点 code**（Ruling T），不是角色——「财务 + 客服都能退」由
   **把同一个节点授予两个角色**表达（D-0030 之后没有「财务」这个角色）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
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
from apps.business.refunds import refund_set_total
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: spec §7 的最终路径（**没有**尾斜杠）。
REFUND_URL = "/api/business/orders/{order_no}/refunds"
#: 批量入口（spec §D13）。
BATCH_REFUND_URL = "/api/business/orders/batch/refunds/"
#: 退款闸门的节点 code（`apps/rbac/catalog.py` 里 `benchmark_id = 1205` / `code = "order.online_refund"`
#: 那一行；⚠️ **不写行号**——目录裁剪会让它漂，按 id/code 指认）。
NODE_CODE = "order.online_refund"


class _RefundFixture(TenantTestCase):
    """建单 / 建绩效 / 发退款请求的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 管理员登录：`has_node_now` 短路为真 ⇒ 业务逻辑用例不必先搭角色目录。
        self.admin = make_logged_in_client(username="refund-admin", is_superuser=True)
        self.sales_user = User.objects.create_user(username="sales-refund")
        # ⭐ P0-2：客服档案 = 「利润的百分比 20%」。
        #    为什么必须有：部分退款末尾的 `refresh_customer_estimate` 按**订单归属人**的档案
        #    算提成率（`commission_percent_of(order.sales_user, mode=PROFIT_PERCENT)`）；
        #    没有档案时它回落到 `100%`（Q3 裁定），于是「重算出来的数」与客服行上那份
        #    `commission_rate = 20.00` 的快照**对不上**，断言会变成考一个奇怪的数。
        #    ⚠️ 它只影响**挂了 `sales_user` 的用例**（本文件只有 P0-2 那两条）：没挂的单子
        #    在 `refresh_customer_estimate` 第 1 条就早退，根本不读档案。
        StaffProfile.objects.create(
            user=self.sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self.taker_user = User.objects.create_user(username="taker-refund")
        self._seq = 0

    def _order(
        self,
        *,
        status=OrderStatus.COMPLETED,
        trap_received="1.00",      # 陷阱值：改成任何真实数字都不该影响任何断言
        hall=None,
        total="100.00",            # ⭐ 毛收款 = 订单总价（D-0142）
        refund_fee="0.00",
        sales_user=None,           # 客服归属人：⭐ 2026-10-22（P0-2）起它决定「退款能不能重算」
    ):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092100000{self._seq:07d}",
            status=status,
            received_price=None if trap_received is None else Decimal(trap_received),
            hall_price=None if hall is None else Decimal(hall),
            total_price=Decimal(total),
            refund_fee=Decimal(refund_fee),
            sales_user=sales_user,
        )

    def _sales(self, order, **overrides):
        """客服行（`销售提成`）。默认提成率 20.00%。"""
        fields = {
            "emp_type": EmpType.SALES,
            "user": self.sales_user,
            "temp_amount": Decimal("0.00"),
            "bonus_amount": Decimal("8.00"),
            "commission_rate": Decimal("20.00"),
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _taker(self, order, **overrides):
        """打手行（`打单工资`）。"""
        fields = {
            "emp_type": EmpType.MAIN,
            "user": self.taker_user,
            "temp_amount": Decimal("60.00"),
            "bonus_amount": Decimal("55.00"),
            "commission_rate": Decimal("20.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, **fields)

    def _url(self, order):
        return REFUND_URL.format(order_no=order.order_no)

    def _payload(self, **overrides):
        data = {"refund_total": "50.00", "reason": "客户投诉进度慢", "request_id": "req-1"}
        data.update(overrides)
        return data

    def _post(self, order, payload, client=None):
        return (client or self.admin).post(
            self._url(order), payload, content_type="application/json"
        )


class RefundDeductionTests(_RefundFixture):
    """冲减算式与「原行不可变」的两条主线（§9 #1 / #2 / #17）。"""

    def test_a_partial_refund_deducts_sales_and_leaves_the_taker_alone(self):
        """§9 #1 + §4.5：客服的减量落在 **`temp_amount` / `bonus_amount` 的重算**上，打手逐字段不动。

        ⭐ **P0-2）改了本条的期望**（原文钉「一条 `-10.00` 的
        `PARTIAL_REFUND` 调整」）：`50.00 × 20% = 10.00` 是**退款额**的提成，而同一笔退款
        末尾的 `refresh_customer_estimate` 已经按**新利润**把客服两列重算过（代数见下），
        再建一条冲减就是同一笔钱扣两次（评审 P0-2）。

        代数（本夹具：订单总价 `100.00`、打手 `temp = 60.00` / `bonus = 55.00`、客服率 `20%`）：
        「预计」按打手**预计** `60.00` 算 ⇒ 利润 `50 − 60 = −10.00` ⇒ `temp = −2.00`；
        「生效」按打手**生效** `55.00` 算（`D-0182`：两列各按自己的减项）⇒
        利润 `50 − 55 = −5.00` ⇒ `bonus = −1.00`。**负数合法**（D-0015）。
        修前：两列同样被刷成这两个数，**另加**一条 `−10.00` 的冲减
        ⇒ `payable_net = −1.00 − 10.00 = −11.00`；修后 `payable_net = −1.00`（只扣一次）。
        ⚠️ 本条的 `_order` 挂 `sales_user` 是**必须的**：不挂的话 `refresh_customer_estimate`
        第 1 条就早退、走的是「算不出 ⇒ 回落建冲减」那条路（护栏在
        `tests/test_settle_p0_refund.py::…test_a_fixed_mode_row_still_gets_the_explicit_deduction`）。
        """
        order = self._order(sales_user=self.sales_user)
        sales = self._sales(order)
        taker = self._taker(order)
        taker_before = (taker.temp_amount, taker.bonus_amount)

        body = self._post(order, self._payload(refund_total="50.00")).json()

        self.assertEqual(body["code"], 0)
        # ⛔ 没有独立的冲减行：钱由下面两列的重算体现。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(), 0
        )
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("-2.00"))
        self.assertEqual(sales.bonus_amount, Decimal("-1.00"))

        taker.refresh_from_db()
        self.assertEqual(taker.temp_amount, Decimal("60.00"))
        self.assertEqual(taker.bonus_amount, Decimal("55.00"))
        self.assertEqual(
            (taker.temp_amount, taker.bonus_amount), taker_before
        )
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=taker).count(), 0
        )

    def test_the_refund_writes_one_audit_row_with_the_accumulated_values(self):
        """§9 #17：留痕落在**新表 `OperationLog`** 上，`from/to` 是累计退款额的旧 / 新值。

        ⭐ M3a：动作 `order.refund`、分类 10，且是**订单级**（`target_kind="order"`）。
        """
        order = self._order()
        self._sales(order)

        body = self._post(order, self._payload(refund_total="50.00")).json()

        self.assertEqual(body["code"], 0)
        audit = OperationLog.objects.get(action="order.refund")
        self.assertEqual(audit.log_type, 10)
        self.assertEqual(audit.target_kind, "order")
        self.assertEqual(audit.target_id, order.order_no)
        self.assertEqual(audit.order_no, order.order_no)
        self.assertEqual(audit.from_value, "0.00")
        self.assertEqual(audit.to_value, "50.00")
        self.assertEqual(audit.reason, "客户投诉进度慢")
        self.assertEqual(audit.actor, "refund-admin")
        self.assertEqual(
            audit.summary,
            f"【refund-admin】将订单 {order.order_no} 的累计退款额从 0.00 元更新为 50.00 元"
            "（原因：客户投诉进度慢）",
        )
        # 旧出口退役：这条端点**一行都不许**再写旧表（spec §16.1）。
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_the_original_sales_row_is_never_modified(self):
        """§9 #2（**只对「算不出」那一档**）：客服行金额字段退款前后相等，调整记录条数 +1。

        ⚠️ **P0-2）补的限定语**：本条的 `_order()` **没有挂 `sales_user`**
        ⇒ `refresh_customer_estimate` 第 1 条就早退、走的是「算不出 ⇒ 回落建冲减」那条路
        ⇒ 「原行一个字节不改」成立于**这一档**。挂了归属人且算得出来的单子**会**改写客服行的
        `temp_amount` / `bonus_amount`（那正是 P0-2 修完的正常路径，判据见同类的
        `test_a_partial_refund_deducts_sales_and_leaves_the_taker_alone`）。
        ⛔ 不要把本条读成「部分退款永不改客服行」——那是修之前的假话。
        """
        order = self._order()
        sales = self._sales(order)
        fields = [
            "temp_amount",
            "bonus_amount",
            "deduct_value",
            "taker_price",
            "commission_rate",
        ]
        before = {name: getattr(sales, name) for name in fields}
        count_before = PerformanceAdjustment.objects.filter(performance=sales).count()

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 0)
        sales.refresh_from_db()
        after = {name: getattr(sales, name) for name in fields}
        self.assertEqual(after, before)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=sales).count(),
            count_before + 1,
        )

    def test_the_taker_row_is_not_touched_at_all(self):
        """§4.5 细节 2：打手行**完全没动**——所有具体字段逐字段相等，且零调整记录。"""
        order = self._order()
        self._sales(order)
        taker = self._taker(order)
        before = {
            field.attname: getattr(taker, field.attname)
            for field in Performance._meta.concrete_fields
        }

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 0)
        taker.refresh_from_db()
        after = {
            field.attname: getattr(taker, field.attname)
            for field in Performance._meta.concrete_fields
        }
        self.assertEqual(after, before)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(performance=taker).count(), 0
        )

    def test_the_order_stays_completed_after_a_partial_refund(self):
        """§10 #11：部分退款**不加状态、也不加标记**。"""
        order = self._order()
        self._sales(order)

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, "COMPLETED")

    def test_setting_a_new_total_records_only_the_signed_delta(self):
        """`refund_fee` 是**累计值**：第二次设成 50 ⇒ 只记差额 `30 → 50` 的 `+20`。

        ⭐ **D-0312）**：入参是绝对值 ⇒ 两次设置**不是**「两次之和」，
        而是「后到者的值」；台账每行是差额。本条同时钉住「一行不重复记账」。
        ⭐ **P0-2）**：未结账的客服行不建独立冲减 ⇒ 钱由 `bonus_amount`
        的**重算**体现。本夹具**没有打手行**（减项 `0`）⇒ 判据落在客服行的重算值上：
        退前按录单口径摆成 `100 × 20% = 20.00`；设 30 ⇒ 净 `70` ⇒ `14.00`；
        再设 50 ⇒ 净 `50` ⇒ `10.00`。
        ⚠️ `sales_user` 必须挂：不挂的话 `refresh_customer_estimate` 第 1 条早退、
        走「算不出 ⇒ 回落建冲减」那条路（与 P0-2 修的那条路**不是**同一条）。
        """
        order = self._order(sales_user=self.sales_user)
        sales = self._sales(order, bonus_amount=Decimal("20.00"))

        first = self._post(
            order, self._payload(refund_total="30.00", request_id="acc-1")
        ).json()
        second = self._post(
            order, self._payload(refund_total="50.00", request_id="acc-2")
        ).json()

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        self.assertEqual(OrderRefund.objects.count(), 2)
        # 差额台账：第一行 +30、第二行 +20（⛔ 第二行不是 50）。
        self.assertEqual(
            [(row.kind, row.amount) for row in OrderRefund.objects.order_by("id")],
            [("SET", Decimal("30.00")), ("SET", Decimal("20.00"))],
        )
        # ⛔ 一条独立冲减都没有：钱由下面那一列的重算体现。
        self.assertEqual(
            PerformanceAdjustment.objects.filter(kind="PARTIAL_REFUND").count(), 0
        )
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("10.00"))
        self.assertEqual(sales.bonus_amount, Decimal("10.00"))
        rows = list(OperationLog.objects.filter(action="order.refund").order_by("id"))
        self.assertEqual(
            [(row.from_value, row.to_value) for row in rows],
            [("0.00", "30.00"), ("30.00", "50.00")],
        )


class RefundPreconditionTests(_RefundFixture):
    """§5.1 的五条前置校验，各一条（含边界「恰好相等」与「超一分」）。"""

    def test_an_unknown_order_is_a_business_code_1000_not_a_404(self):
        """#1：订单不存在（或不在本租户 schema）⇒ 200 + `1000`。"""
        response = self.admin.post(
            REFUND_URL.format(order_no="20990101000000000001"),
            self._payload(),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_a_non_completed_order_is_rejected_with_1007(self):
        """#2：只有 `COMPLETED` 能设置累计退款额；未完成 / 已退单 ⇒ `1007`，一行都没写。"""
        for status in (OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED):
            with self.subTest(status=status):
                order = self._order(status=status)

                body = self._post(order, self._payload()).json()

                self.assertEqual(body["code"], 1007)
                self.assertEqual(OrderRefund.objects.count(), 0)
                self.assertEqual(PerformanceAdjustment.objects.count(), 0)
                order.refresh_from_db()
                self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_a_negative_total_is_rejected_with_1008(self):
        """#3：`refund_total < 0` ⇒ `1008`（**不是**序列化器的 `1000`）。

        ⚠️ `0` 已**不是**非法值：它是「清空累计退款」，合法（见
        `RefundTotalSetTests.test_setting_zero_clears_the_total`）。
        """
        order = self._order()

        body = self._post(order, self._payload(refund_total="-1.00")).json()

        self.assertEqual(body["code"], 1008)
        self.assertEqual(OrderRefund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_the_exact_order_price_is_allowed(self):
        """#4 边界：`refund_total == total_price` 恰好相等 ⇒ **允许**（退满）。"""
        order = self._order(refund_fee="30.00")
        self._sales(order)

        body = self._post(order, self._payload(refund_total="100.00")).json()

        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("100.00"))
        # 台账记差额：30 → 100 = +70。
        self.assertEqual(
            OrderRefund.objects.get().amount, Decimal("70.00")
        )

    def test_one_cent_over_the_order_price_is_rejected_with_1008(self):
        """#4 边界：超一分 ⇒ `1008`，`refund_fee` 不动（**这就是它的非负性保证**）。"""
        order = self._order(refund_fee="30.00")

        body = self._post(order, self._payload(refund_total="100.01")).json()

        self.assertEqual(body["code"], 1008)
        self.assertIn("订单总价", body["msg"])
        self.assertEqual(OrderRefund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("30.00"))

    def test_a_zero_total_price_only_allows_the_zero_total(self):
        """#4 边界（P4FU-F 重写）：`total_price=0.00` ⇒ 只能设成 `0`（= 清空/无操作）。

        原用例钉的是「接单实收与接单价**双空** ⇒ `1008`」；回落档改成订单总价、该列
        `null=False default=0.00` 之后，「双空」这个状态不存在 ⇒ 按新语义重写为：
        毛收款 0 ⇒ `refund_total > 0` 撞 #4；`refund_total = 0` 在「当前累计也是 0」时
        是**全空操作**（200 + `changed:false`）——**`1008` 这条路仍然可达**。
        ⚠️ `hall = "80.00"` 是**陷阱值**：回落大厅价会让上限变成 80.00、退款成功 ⇒ 本用例红。
        """
        order = self._order(trap_received=None, hall="80.00", total="0.00")

        response = self._post(
            order, self._payload(refund_total="0.01", request_id="zero-total-1")
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1008)
        self.assertIn("订单总价", body["msg"])
        self.assertEqual(OrderRefund.objects.count(), 0)

        # 设成 0 且当前累计已是 0 ⇒ 全空操作（不是 1008、不是 1010）。
        noop = self._post(
            order, self._payload(refund_total="0.00", request_id="zero-total-2")
        ).json()
        self.assertEqual(noop["code"], 0)
        self.assertEqual(noop["data"]["changed"], False)
        self.assertEqual(OrderRefund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_a_zero_received_price_is_ignored(self):
        """⭐ spec §8 第 13 条：`received_price = 0.00` 在新口径下**不参与毛收款**。

        `Decimal("0.00")` 在旧口径下是「真实零收款 ⇒ 任何正数退款 `1008`」；
        新口径（D-0142）下毛收款 = 订单总价 `100.00` ⇒ **设成 100.00 成功**，
        再设成 `100.01` 才撞 #4。这一条把「`received_price` 不再是闸门来源」钉住。
        ⚠️ 陷阱值 `hall = "50.00"`：改读大厅价 ⇒ 设 100.00 会先撞 #4 ⇒ 本用例红。
        """
        order = self._order(trap_received="0.00", hall="50.00", total="100.00")

        body = self._post(order, self._payload(refund_total="100.00")).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("100.00"))

        second = self._post(
            order, self._payload(refund_total="100.01", request_id="zero-received-2")
        ).json()
        self.assertEqual(second["code"], 1008)
        self.assertIn("订单总价", second["msg"])
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("100.00"))

    def test_the_total_price_is_the_only_source_of_the_refund_ceiling(self):
        """⭐ spec §8 第 13 条：退款上限 = `total_price`，**与 `received_price` 无关**。

        夹具 `trap_received = "999.00"`（陷阱值）、`total = "80.00"`、`hall = "50.00"`：
        设 `80.00` 成功（上限恰好 80.00），再设 `80.01` ⇒ `1008`。
        判别力：若误读 `received_price`，上限会是 999.00 ⇒ **第二半不会红** ⇒ 本用例当场红。
        ⚠️ `hall = "50.00"` 同样是陷阱值：改读大厅价会在**第一半**就撞 #4。
        """
        order = self._order(trap_received="999.00", hall="50.00", total="80.00")
        self._sales(order)

        body = self._post(order, self._payload(refund_total="80.00")).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("80.00"))

        second = self._post(
            order, self._payload(refund_total="80.01", request_id="total-only-2")
        ).json()
        self.assertEqual(second["code"], 1008)
        self.assertIn("订单总价", second["msg"])
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("80.00"))


class RefundIdempotencyTests(_RefundFixture):
    """§9 #5：同一 `request_id` 第二次 ⇒ `1010`，账只记一次；**差额为 0 时不会 1010**。

    ⚠️ 判别力实验（见报告「变异记录表」）：临时去掉 `OrderRefund.Meta.constraints` 的
    `uniq_order_refund_request_id`（**连迁移一起改**）⇒ 本类第一条**必须变红**。
    实现里**没有** `exists()` 预查正是为了保住这条实验的判别力——有预查时，去掉约束后
    第二次请求仍会被预查拦下，实验不会红（保护被架空）。
    ⭐ **D-0312）补的第二条**：差额为 0 的请求**不 INSERT**
    ⇒ 同一键重提**不会**回 1010（这是「设成同值 = 全空操作」契约的一半）。
    """

    def test_the_same_request_id_twice_with_a_different_total_is_rejected_with_1010(self):
        order = self._order()
        self._sales(order)

        first = self._post(
            order, self._payload(refund_total="30.00", request_id="dup-1")
        ).json()
        # 第二次**改了累计值**（30 → 50）⇒ 真要 INSERT ⇒ 撞唯一约束。
        second = self._post(
            order, self._payload(refund_total="50.00", request_id="dup-1")
        ).json()

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 1010)
        self.assertEqual(OrderRefund.objects.filter(request_id="dup-1").count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("30.00"))
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)
        self.assertEqual(OperationLog.objects.filter(action="order.refund").count(), 1)

    def test_the_same_request_id_with_the_same_total_is_not_1010(self):
        """差额 = 0 不 INSERT ⇒ 同一个键重提是 **200 + changed:false**，不是 1010。"""
        order = self._order()
        self._sales(order)
        payload = self._payload(refund_total="50.00", request_id="dup-same")

        first = self._post(order, payload).json()
        second = self._post(order, payload).json()

        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 0)
        self.assertEqual(second["data"]["changed"], False)
        self.assertEqual(OrderRefund.objects.filter(request_id="dup-same").count(), 1)
        self.assertEqual(OperationLog.objects.filter(action="order.refund").count(), 1)


class RefundSalesDeductionDetailTests(_RefundFixture):
    """§4.5 的四条细节（每条一个用例，且每条都能被一个变异打红）。"""

    def test_a_sales_row_without_a_commission_rate_rejects_the_whole_refund(self):
        """细节 3：`commission_rate` 为空 ⇒ `1009`，**一行都没写**。

        ⚠️ 正常路径**不可达**（部分退款要求 `COMPLETED`，而 `commission_rate` 正是
        `COMPLETED` 时写的）⇒ 这里直接 `update(commission_rate=None)` 构造，否则是死代码。
        """
        order = self._order()
        sales = self._sales(order)
        Performance.objects.filter(pk=sales.pk).update(commission_rate=None)

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 1009)
        self.assertEqual(OrderRefund.objects.count(), 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.filter(action="order.refund").count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_an_order_without_any_sales_row_still_records_the_refund(self):
        """细节 4：一条客服行都没有 ⇒ 退款**照常成功**，零条调整记录。"""
        order = self._order()
        self._taker(order)

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(OrderRefund.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)

    def test_a_settled_sales_row_is_still_deducted_with_an_unsettled_adjustment(self):
        """细节 1：**已结账**的客服行也要冲减；**新调整记录自身**是「未结账」。"""
        order = self._order()
        sales = self._sales(order, settle_status=SettleStatus.SETTLED)

        body = self._post(order, self._payload()).json()

        self.assertEqual(body["code"], 0)
        adjustment = PerformanceAdjustment.objects.get(performance=sales)
        self.assertEqual(adjustment.amount, Decimal("-10.00"))
        self.assertEqual(adjustment.settle_status, "未结账")
        sales.refresh_from_db()
        self.assertEqual(sales.settle_status, "已结账")


class RefundInputValidationTests(_RefundFixture):
    """§9 #16 / 计划 §5-④：畸形入参一律 **200 + 业务码**（不是 500）。

    ⭐ 旧前端（只发 `amount`）必须**响亮失败**：缺 `refund_total` ⇒ `1000` + 必填文案，
    ⛔ 不允许把旧 `amount` 静默当成「累计额」。
    """

    def test_malformed_refund_total_reason_and_request_id_are_business_codes_not_500(self):
        order = self._order()
        cases = [
            {"refund_total": "abc"},            # 非十进制
            {"refund_total": "1.234"},          # 超两位小数
            {"refund_total": "1" * 14},         # 超 12 位
            {"reason": "x" * 256},        # 超列宽 255
            {"request_id": "x" * 65},     # 超列宽 64
        ]

        for overrides in cases:
            with self.subTest(overrides=overrides):
                response = self._post(order, self._payload(**overrides))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)

        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_a_legacy_amount_key_is_1000_with_the_required_message(self):
        """旧前端只发 `amount` ⇒ 缺必填键 ⇒ `1000`，msg 是必填文案（⛔ 不静默兼容）。"""
        order = self._order()

        response = self._post(
            order,
            {"amount": "50.00", "reason": "旧前端", "request_id": "legacy-1"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], "请填写累计退款总额")
        self.assertEqual(OrderRefund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))


class RefundAtomicityTests(_RefundFixture):
    """§9 #14：第 3 步抛异常 ⇒ `OrderRefund` / `refund_fee` / 调整记录 / 审计**都未变**。

    桩打在 `apps.business.refunds`（**被查找的那个模块**，E-0029）上，并且**自证接上**：
    另起一条订单跑「不接桩时它应当成功」的对照。
    """

    def test_a_failure_in_step_three_rolls_back_every_write(self):
        # 对照：不接桩时同一路径必须成功 —— 桩「接没接上」本身要有判据（E-0029）。
        control_order = self._order()
        self._sales(control_order)
        control = self._post(
            control_order, self._payload(request_id="control-ok")
        ).json()
        self.assertEqual(control["code"], 0)
        control_order.refresh_from_db()
        self.assertEqual(control_order.refund_fee, Decimal("50.00"))

        order = self._order()
        self._sales(order)
        calls = []

        def boom(base, percent):
            calls.append((base, percent))
            raise RuntimeError("步骤 3 打桩失败")

        # ⚠️ `mock.patch` 打在**被查找的位置**：`refunds.py` 是
        # `from apps.business.performance_amount import commission_amount`，
        # 符号住在 `apps.business.refunds`；打 `...performance_amount` 不生效（E-0029）。
        with mock.patch("apps.business.refunds.commission_amount", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self._post(order, self._payload(request_id="boom"))

        # 桩自证接上：它真的被第 3 步以「退款额 × 该行提成率」调过。
        self.assertEqual(calls, [(Decimal("50.00"), Decimal("20.00"))])
        # 四类写入都未变（对照那笔仍是库里唯一的一笔）。
        self.assertEqual(OrderRefund.objects.filter(request_id="boom").count(), 0)
        self.assertEqual(OrderRefund.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)
        self.assertEqual(
            PerformanceAdjustment.objects.filter(refund__request_id="boom").count(), 0
        )
        self.assertEqual(OperationLog.objects.filter(action="order.refund").count(), 1)


class RefundListTests(_RefundFixture):
    """`GET /api/business/orders/{order_no}/refunds`（spec §7）。"""

    def test_the_list_returns_every_row_of_that_order_only(self):
        order = self._order()
        other = self._order()
        self._sales(order)
        self._sales(other)
        self._post(order, self._payload(refund_total="10.00", request_id="list-1"))
        # 第二次设成 30 ⇒ 台账第二行是差额 +20（⛔ 不是 30）。
        self._post(order, self._payload(refund_total="30.00", request_id="list-2"))
        self._post(other, self._payload(refund_total="30.00", request_id="list-3"))

        body = self.admin.get(self._url(order)).json()

        self.assertEqual(body["code"], 0)
        items = body["data"]["items"]
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(
            {item["request_id"] for item in items}, {"list-1", "list-2"}
        )
        self.assertEqual({item["order_no"] for item in items}, {order.order_no})
        self.assertEqual(
            sorted(item["amount"] for item in items), ["10.00", "20.00"]
        )

    def test_a_negative_set_row_is_returned_as_is(self):
        """⭐ 2026-10-26（D-0312）：`kind=SET` 的金额是**带符号差额**，列表原样上屏。

        ⛔ 不格式化成绝对值：`-450.00` 是「500 → 50」这个真实资金事件，取绝对值会把它
        伪装成一笔正的 450 元退款。
        """
        order = self._order(total="1000.00", refund_fee="50.00")
        OrderRefund.objects.create(
            order=order,
            kind="SET",
            amount=Decimal("-450.00"),
            reason="设置累计退款额",
            operator="客服甲",
            request_id="neg-set-1",
        )

        body = self.admin.get(self._url(order)).json()

        self.assertEqual(body["code"], 0)
        item = body["data"]["items"][0]
        self.assertEqual(item["kind"], "SET")
        self.assertEqual(item["amount"], "-450.00")

    def test_an_unknown_order_returns_an_empty_list(self):
        body = self.admin.get(
            REFUND_URL.format(order_no="20990101000000000002")
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 0)
        self.assertEqual(body["data"]["items"], [])


class RefundTotalSetTests(_RefundFixture):
    """计划 §5 的 ①②③⑥⑦：绝对值语义 / 差额台账 / 全空操作 / 已结算不回写。

    ⭐ **D-0312）**：这一组是本轮语义变更的主判据——旧「累加」口径下
    「先 500 再 50 ⇒ 550」，新口径必须是 **50**；台账必须是 `+500 / −450` 两行。
    """

    def test_a_lower_total_after_a_higher_one_lands_the_absolute_value(self):
        """① 先设 500 再设 50 ⇒ `refund_fee=50`（**不是 550**），台账 Σ=50，客服净冲减 −10。"""
        order = self._order(total="1000.00")
        sales = self._sales(
            order,
            settle_status=SettleStatus.SETTLED,
            settled_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
        )

        first = self._post(
            order, self._payload(refund_total="500.00", request_id="set-1")
        ).json()
        second = self._post(
            order, self._payload(refund_total="50.00", request_id="set-2")
        ).json()

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        rows = list(OrderRefund.objects.filter(order=order).order_by("id"))
        self.assertEqual(
            [(row.kind, row.amount) for row in rows],
            [("SET", Decimal("500.00")), ("SET", Decimal("-450.00"))],
        )
        # Σ 不变量：台账行之和 == 列。
        self.assertEqual(
            sum((row.amount for row in rows), Decimal("0.00")),
            order.refund_fee,
        )
        # 已结账的客服行：两条调整的净额 = −50 × 20% = −10，且历史列一个字节都不改。
        adjustments = list(
            PerformanceAdjustment.objects.filter(performance=sales).order_by("id")
        )
        self.assertEqual(
            [row.amount for row in adjustments],
            [Decimal("-100.00"), Decimal("90.00")],
        )
        self.assertEqual(
            sum((row.amount for row in adjustments), Decimal("0.00")),
            Decimal("-10.00"),
        )
        self.assertTrue(all(row.settle_status == "未结账" for row in adjustments))
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("8.00"))
        self.assertEqual(sales.settled_amount, Decimal("8.00"))
        self.assertEqual(sales.settle_status, "已结账")

    def test_setting_zero_clears_the_total(self):
        """② 填 0 ⇒ 清空：`refund_fee=0`、Σ=0（一行 `-旧累计`）。"""
        order = self._order(total="100.00", refund_fee="80.00")
        # 历史台账（Σ 不变量的另一半：列上的 80 必须有行来源）。
        OrderRefund.objects.create(
            order=order,
            kind="PARTIAL",
            amount=Decimal("80.00"),
            reason="历史退款",
            operator="历史",
            request_id="clear-hist-1",
        )
        self._sales(order)

        body = self._post(
            order, self._payload(refund_total="0.00", request_id="clear-1")
        ).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        rows = list(OrderRefund.objects.filter(order=order))
        self.assertEqual(
            [(row.kind, row.amount) for row in rows],
            # 历史行（夹具造）+ 本次清空行；`Meta.ordering` 是新的在前。
            [("SET", Decimal("-80.00")), ("PARTIAL", Decimal("80.00"))],
        )
        self.assertEqual(sum((row.amount for row in rows), Decimal("0.00")), Decimal("0.00"))
        audit = OperationLog.objects.get(action="order.refund")
        self.assertEqual((audit.from_value, audit.to_value), ("80.00", "0.00"))

    def test_setting_the_same_value_is_a_full_noop(self):
        """③ 设成当前值 ⇒ 200 + `changed:false`；四类副作用**一行都不新增**，且不 500。

        同一 `request_id` 重提也**不会** `1010`（差额为 0 不 INSERT）。
        """
        order = self._order(total="100.00")
        sales = self._sales(order)
        payload = self._payload(refund_total="50.00", request_id="noop-1")
        first = self._post(order, payload).json()
        self.assertEqual(first["code"], 0, first)

        counts = {
            "refunds": OrderRefund.objects.count(),
            "logs": OperationLog.objects.filter(action="order.refund").count(),
            "adjustments": PerformanceAdjustment.objects.count(),
        }

        response = self._post(order, payload)  # 同值 + 同键

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            set(body["data"]), {"order_no", "refund_fee", "changed"}
        )
        self.assertEqual(body["data"]["order_no"], order.order_no)
        self.assertEqual(body["data"]["refund_fee"], "50.00")
        self.assertIsInstance(body["data"]["refund_fee"], str)
        self.assertEqual(body["data"]["changed"], False)
        self.assertEqual(OrderRefund.objects.count(), counts["refunds"])
        self.assertEqual(
            OperationLog.objects.filter(action="order.refund").count(), counts["logs"]
        )
        self.assertEqual(PerformanceAdjustment.objects.count(), counts["adjustments"])
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        sales.refresh_from_db()

    def test_the_ledger_and_audit_carry_the_signed_delta_and_new_total(self):
        """⑦ 一行 `kind=SET, amount=差额`；一条 `order.refund`，from/to = 旧/新累计。"""
        order = self._order(total="100.00", refund_fee="20.00")
        self._sales(order)

        body = self._post(
            order, self._payload(refund_total="80.00", request_id="shape-1")
        ).json()

        self.assertEqual(body["code"], 0, body)
        row = OrderRefund.objects.get(order=order)
        self.assertEqual(row.kind, "SET")
        self.assertEqual(row.amount, Decimal("60.00"))
        audit = OperationLog.objects.get(action="order.refund")
        self.assertEqual(audit.from_value, "20.00")
        self.assertEqual(audit.to_value, "80.00")
        self.assertEqual(
            audit.summary,
            f"【refund-admin】将订单 {order.order_no} 的累计退款额从 20.00 元更新为 80.00 元"
            "（原因：客户投诉进度慢）",
        )

    def test_settled_rows_are_not_rewritten_and_the_correction_lands_next_period(self):
        """⑥ 已结算：历史列一个字节不改；纠正落**下一条未结账调整**（往下改得**正**调整）。"""
        order = self._order(total="200.00")
        sales = self._sales(
            order,
            settle_status=SettleStatus.SETTLED,
            settled_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
        )
        before = {
            "bonus_amount": sales.bonus_amount,
            "settled_amount": sales.settled_amount,
            "settle_status": sales.settle_status,
            "commission_rate": sales.commission_rate,
        }

        self._post(order, self._payload(refund_total="80.00", request_id="settled-up"))
        self._post(order, self._payload(refund_total="30.00", request_id="settled-down"))

        sales.refresh_from_db()
        after = {
            "bonus_amount": sales.bonus_amount,
            "settled_amount": sales.settled_amount,
            "settle_status": sales.settle_status,
            "commission_rate": sales.commission_rate,
        }
        self.assertEqual(after, before)
        adjustments = list(
            PerformanceAdjustment.objects.filter(performance=sales).order_by("id")
        )
        # 差额 +80 ⇒ −16；差额 −50 ⇒ **+10**（退还上次多扣的）。符号翻转分支。
        self.assertEqual(
            [row.amount for row in adjustments],
            [Decimal("-16.00"), Decimal("10.00")],
        )
        self.assertGreater(adjustments[1].amount, Decimal("0.00"))
        self.assertTrue(all(row.settle_status == "未结账" for row in adjustments))

    def test_the_fixed_profile_fallback_creates_a_positive_adjustment_when_the_total_goes_down(self):
        """⑥ 后半：`refresh_customer_estimate` 回 `None`（FIXED 档）时，往下改要建**正**调整。

        这条分支（符号翻转）在本轮之前**从未被测过**：旧口径只会往上加，
        回落分支永远只需建负数冲减。
        """
        fixed_user = User.objects.create_user(username="fixed-sales")
        StaffProfile.objects.create(
            user=fixed_user,
            commission_mode=CommissionMode.FIXED,
            commission_value=Decimal("10.00"),
        )
        order = self._order(total="200.00", sales_user=fixed_user)
        sales = self._sales(order, user=fixed_user, commission_rate=Decimal("20.00"))

        first = self._post(
            order, self._payload(refund_total="80.00", request_id="fixed-up")
        ).json()
        second = self._post(
            order, self._payload(refund_total="30.00", request_id="fixed-down")
        ).json()

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        adjustments = list(
            PerformanceAdjustment.objects.filter(performance=sales).order_by("id")
        )
        self.assertEqual(
            [row.amount for row in adjustments],
            [Decimal("-16.00"), Decimal("10.00")],
        )
        # 回落建出来的那条必须是**正**数（= 把上次多扣的提成退回来）。
        self.assertEqual(adjustments[1].amount, Decimal("10.00"))


class BatchRefundTotalTests(_RefundFixture):
    """⑧ 批量入口与单笔同一判据：先 500 再 50 ⇒ 50（不是 550）。"""

    def test_the_batch_entry_sets_the_absolute_total_too(self):
        order = self._order(total="1000.00")
        self._sales(order)

        def send(refund_total, request_id):
            return self.admin.post(
                BATCH_REFUND_URL,
                {
                    "order_nos": [order.order_no],
                    "refund_total": refund_total,
                    "reason": "批量设置",
                    "request_ids": [request_id],
                    "dry_run": False,
                },
                content_type="application/json",
            ).json()

        first = send("500.00", "batch-1")
        second = send("50.00", "batch-2")

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        rows = list(OrderRefund.objects.filter(order=order).order_by("id"))
        self.assertEqual(
            [row.amount for row in rows],
            [Decimal("500.00"), Decimal("-450.00")],
        )
        self.assertEqual(
            sum((row.amount for row in rows), Decimal("0.00")),
            order.refund_fee,
        )


class StaleInstanceConcurrencyTests(_RefundFixture):
    """⑩ 并发定死：差额必须按**行锁后读回的值**算，⛔ 不用调用方手里的陈旧实例。

    现场：库里的 `refund_fee` 已经被别人写成 `100`，而本请求手里的 `Order` 实例
    还停在 `0`。请求「设成 150」⇒ 正确 `delta=50`、Σ=150；
    错实现（拿陈旧值算）会得 `delta=150`、Σ=250 ⇒ 本条必红。
    """

    def test_the_delta_is_computed_from_the_locked_row_not_the_caller_instance(self):
        order = self._order(total="200.00")
        # 模拟「另一个请求已经写了 100」，而本实例从未回读。
        # 同时补一条历史台账：`refund_fee` 的 100 必须有行来源，否则 Σ 不变量没有起点
        # （与本类「并发前已有退款」的语义一致，不是为测试放宽判据）。
        OrderRefund.objects.create(
            order=order,
            kind="PARTIAL",
            amount=Decimal("100.00"),
            reason="并发前已有退款",
            operator="别的请求",
            request_id="stale-hist-1",
        )
        Order.objects.filter(pk=order.pk).update(refund_fee=Decimal("100.00"))
        self.assertEqual(order.refund_fee, Decimal("0.00"))

        row = refund_set_total(
            order, Decimal("150.00"), "并发设置", "stale-1", "cs-1"
        )

        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("150.00"))
        self.assertIsNotNone(row)
        self.assertEqual(row.amount, Decimal("50.00"))
        rows = list(OrderRefund.objects.filter(order=order))
        self.assertEqual(
            sum((item.amount for item in rows), Decimal("0.00")),
            Decimal("150.00"),
        )


class RefundPermissionTests(TenantTestCase):
    """§9 #3：`has_node("order.online_refund")` 真的生效（非管理员账号）。

    spec §10 #10 的业务方裁定是「**财务 + 客服都能退**，两者皆可」——
    D-0030 之后没有「财务」这个角色，所以这条裁定**靠权限点表达**：
    这里用两个不同名字的角色（`财务` / `客服`）**各自**持有同一个节点，两个方向各一条 200。
    ⛔ 视图里**不写** `has_role(...)`（写了的话这两条会红）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260921000000000001",
            status=OrderStatus.COMPLETED,
            received_price=Decimal("1.00"),        # 陷阱值：新口径下毛收款只看总价
            total_price=Decimal("100.00"),         # ⭐ 毛收款 = 订单总价（D-0142）
        )

    def _grant(self, username, role_name, *codes):
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

    def _post(self, client):
        return client.post(
            REFUND_URL.format(order_no=self.order.order_no),
            {"refund_total": "50.00", "reason": "越权用例", "request_id": "perm-1"},
            content_type="application/json",
        )

    def test_a_finance_role_holding_the_node_can_refund(self):
        client = make_logged_in_client(username="refund-finance-role")
        self._grant("refund-finance-role", "财务", NODE_CODE)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 退款是写路径：发起时取订单过 `game_scope`、写侧过 `assert_game_allowed` ⇒
        # 这个账号必须显式持有本用例那款游戏的权限，否则拿到业务码 1000。
        grant_games(User.objects.get(username="refund-finance-role"), self.game)

        response = self._post(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(OrderRefund.objects.count(), 1)

    def test_a_service_role_holding_the_node_can_refund(self):
        client = make_logged_in_client(username="refund-service-role")
        self._grant("refund-service-role", "客服", NODE_CODE)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        grant_games(User.objects.get(username="refund-service-role"), self.game)

        response = self._post(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(OrderRefund.objects.count(), 1)

    def test_a_user_without_the_node_is_denied_with_a_permission_message(self):
        client = make_logged_in_client(username="refund-no-node")

        response = self._post(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有订单退款权限")
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_a_user_holding_a_different_node_is_still_denied(self):
        client = make_logged_in_client(username="refund-wrong-node")
        self._grant("refund-wrong-node", "别的角色", "order.list")

        response = self._post(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_anonymous_request_is_401_not_403(self):
        """未认证 ⇒ **401**（spec §7 首段的分层：401 与 403 不能混）。

        这条守的是 `RefundView.permission_denied` 的**转调写法**：它给 403 换了一句中文
        `msg`，而实现必须转调 `super()`——DRF 在 `permission_denied` 里先判「有没有认证成功」，
        自己直接 `raise PermissionDenied` 会把匿名请求也变成 403。
        """
        anonymous = Client(SERVER_NAME="tenant.test.com")

        response = anonymous.post(
            REFUND_URL.format(order_no=self.order.order_no),
            {"refund_total": "50.00", "reason": "匿名", "request_id": "perm-anon"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)


#: 清接单人（`UNASSIGNED` 的唯一写入口之一；`REFUNDED → UNASSIGNED` 那条边正是「退单回池」）。
CLEAR_TAKER_URL = "/api/business/orders/{order_no}/clear-taker/"


class ReversalAuditCarriesTheOperatorTests(_RefundFixture):
    """⭐ MUST-FIX B（2026-10-03 修复轮）：退单回池冲销的**两条留痕必须带操作人主键**。

    ## 走的是**生产 HTTP 路径**（不是直接调域层）

    ① `/refunds` 部分退款 ⇒ `order.refund_fee != 0`（唯一还写它的入口）；
    ② 订单被判成「已退单」；
    ③ `/clear-taker/` ⇒ `apply_transition(UNASSIGNED, actor_id=…)` ⇒ 回池钩子
       （`performance._on_pool_entry`）的 `from_status == REFUNDED` 守卫 ⇒
       `refunds.reverse_previous_round_refund(order, actor_id=…)` ⇒ 两条留痕。

    ## 判别力（三点）

    1. **修前**这两条写死 `actor_id=None`（业务口径「所有人为操作必须带上操作人」下的漏网）
       ⇒ `assertEqual(log.actor_id, 操作人.pk)` 当场红；
    2. ⛔ **不许用 `order.updated_by` 反查用户凑 pk**：夹具把 `updated_by` 设成**另一个人**
       ⇒ 留痕的操作人必须是**清接单人那个认证态用户**，不是 `updated_by`；
    3. 正文是**逐字**的 `【昵称】…`（`record()` 按主键派生显示名）。
    """

    #: 清接单人的那个认证态用户（`_RefundFixture.setUp` 里的 `refund-admin`）。
    OPERATOR = "refund-admin"
    OPERATOR_NICKNAME = "测试昵称甲"
    #: `order.updated_by` 上的**另一个人**（⛔ 它绝不是这次冲销的操作人）。
    WRONG_OPERATOR = "最后改单的人"

    def _arrange_a_refunded_order_with_a_nonzero_refund_fee(self):
        order = self._order()
        self._sales(order)                       # 客服行 bonus 8.00，冲减链起点
        body = self._post(order, self._payload(refund_total="30.00", request_id="rev-1")).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        # ③ 之前的来源态：`REFUNDED`（用 ORM 摆来源态，⛔ 不假装走了一次完整退单流程），
        #    并把 `updated_by` 设成**另一个人**——判据 2 的现场。
        order.status = OrderStatus.REFUNDED
        order.updated_by = self.WRONG_OPERATOR
        order.save(update_fields=["status", "updated_by"])
        return order

    def test_the_reversal_audit_rows_carry_the_operator_primary_key(self):
        from apps.business.refunds import (
            REVERSAL_DEDUCTION_REASON,
            REVERSAL_REFUND_REASON,
        )

        order = self._arrange_a_refunded_order_with_a_nonzero_refund_fee()
        operator = User.objects.get(username=self.OPERATOR)
        operator.first_name = self.OPERATOR_NICKNAME
        operator.save(update_fields=["first_name"])

        response = self.admin.post(
            CLEAR_TAKER_URL.format(order_no=order.order_no),
            {},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.refund_fee, Decimal("0.00"))   # 冲销真的发生了

        # ── 第 1 条：`order.refund`（复位 `refund_fee`）──────────────────
        refund_log = OperationLog.objects.get(
            action="order.refund", reason=REVERSAL_REFUND_REASON
        )
        self.assertEqual(
            refund_log.summary,
            f"【{self.OPERATOR_NICKNAME}】将订单 {order.order_no} 的累计退款额"
            "从 30.00 元更新为 0.00 元（原因：退单回池冲销上一轮退款）",
        )
        self.assertEqual(refund_log.actor_id, operator.pk)
        self.assertEqual(refund_log.actor, self.OPERATOR)
        self.assertEqual(refund_log.actor_name, self.OPERATOR_NICKNAME)
        # ⛔ 判据 2：不是 `order.updated_by` 上那个人。
        self.assertNotEqual(refund_log.actor, self.WRONG_OPERATOR)

        # ── 第 2 条：`performance.adjust_manual`（客服行冲减链反向）──────
        adjust_log = OperationLog.objects.get(
            action="performance.adjust_manual", reason=REVERSAL_DEDUCTION_REASON
        )
        self.assertEqual(
            adjust_log.summary,
            f"【{self.OPERATOR_NICKNAME}】新增了{self.sales_user.username}的绩效调整记录"
            "（变更前：-6.00，变更后：0.00），备注：退单回池冲销上一轮退款冲减"
            f"（订单 {order.order_no}）",
        )
        self.assertEqual(adjust_log.actor_id, operator.pk)
        self.assertEqual(adjust_log.actor, self.OPERATOR)
        self.assertNotEqual(adjust_log.actor, self.WRONG_OPERATOR)

    def test_the_second_human_entry_to_the_pool_also_carries_the_operator(self):
        """⭐ 同一类漏传的**第二条人力入口**：`POST /performances/<id>/delete/` 软删当前接单人。

        `PerformanceViewSet.soft_delete` 清当前接单人那一步也迁 `UNASSIGNED`（池内）
        ⇒ 从「已退单」过来时同样触发回池冲销 ⇒ 那两条留痕也必须带操作人主键。
        （修复轮用 AST 扫 `apply_transition` 调用点发现：它当时与截图删除端点
        是同一类「人为端点没传 pk」的漏传。）
        """
        from apps.business.refunds import REVERSAL_REFUND_REASON

        order = self._arrange_a_refunded_order_with_a_nonzero_refund_fee()
        taker = self._taker(order, settle_status=SettleStatus.UNSETTLED)
        operator = User.objects.get(username=self.OPERATOR)
        operator.first_name = self.OPERATOR_NICKNAME
        operator.save(update_fields=["first_name"])

        response = self.admin.post(
            f"/api/business/performances/{taker.pk}/delete/",
            {},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

        refund_log = OperationLog.objects.get(
            action="order.refund", reason=REVERSAL_REFUND_REASON
        )
        self.assertEqual(
            refund_log.summary,
            f"【{self.OPERATOR_NICKNAME}】将订单 {order.order_no} 的累计退款额"
            "从 30.00 元更新为 0.00 元（原因：退单回池冲销上一轮退款）",
        )
        self.assertEqual(refund_log.actor_id, operator.pk)
        self.assertNotEqual(refund_log.actor, self.WRONG_OPERATOR)
