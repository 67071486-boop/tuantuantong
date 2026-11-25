"""P1-5：完成钩子**不得改写已结账的客服行**（计划 §6）。

## 现场

`settlement_hooks._persist_customer_commission` 是「订单完成 ⇒ 确定客服提成」的**权威写入者**
（`recompute_customer_commission` → 它）。它原先不看目标 SALES 行的结账状态 ⇒
「完成 → 财务把客服行结掉 → 订单回退待验收 → 再完成（金额又变了）」这条路上，
**已经付出去的钱**会被新的提成数覆盖。

`refresh_customer_estimate` 的第 0 条（业务方 Q1）已经是这个口径
（`is_settled` ⇒ 一个字节都不动）⇒ 本批让权威写入者**对齐它**：`is_settled` ⇒ 不写
`bonus_amount` / `commission_rate`，只记一条 warning。

⚠️ 判据打在**数据库行**上（E-0009）；期望值写字面十进制（E-0024）；夹具用合成值（E-0077）。
⚠️ 只有这一条主用例：它同时钉住「钩子照旧跑（打手那行变了）」与「客服那行一个字节没变」，
两半缺一，变异（把闸去掉 / 把整个钩子短路）都能各自骗过它。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import make_logged_in_client

#: 夹具的字面值：净实收 130.00 − 打手预计 17.60 = 112.40 ⇒ 客服 40% = 44.96。
#: 第二次完成时把打手预计改成 30.00 ⇒ 若**没有**闸门，客服会变成 (130 − 30) × 40% = 40.00
#: （两个数可分辨，这就是本用例的判别力）。
SALES_RATE = Decimal("40.00")
TAKER_RATE = Decimal("20.00")
ORDER_TOTAL = Decimal("130.00")
ORDER_HALL = Decimal("88.00")
TAKER_TEMP_BEFORE = Decimal("17.60")
CUSTOMER_BEFORE = Decimal("44.96")
TAKER_TEMP_AFTER = Decimal("30.00")
CUSTOMER_IF_UNGUARDED = Decimal("40.00")

#: 两条端点路径要的节点：`/review/`（过审 ⇒ 完成）与 `/transition/`（回退待验收）。
NODES = ("order.change_status_completed", "order.change_status")
REVIEW_URL = "/api/business/performances/{pk}/review/"
TRANSITION_URL = "/api/business/orders/{order_no}/transition/"


class CompletionHookDoesNotTouchASettledCustomerRowTests(TenantTestCase):
    """完成 → 结客服行 → 回退待验收 →（重开后又变价）→ 再 `/review/` ⇒ 客服金额不变。"""

    def setUp(self):
        self.game = Game.objects.create(name="P1-5 客服行闸门游戏")
        self.sales = User.objects.create_user(username="p1-guard-sales")
        StaffProfile.objects.create(
            user=self.sales,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.taker = User.objects.create_user(username="p1-guard-taker")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261022000000000901",
            status=OrderStatus.PENDING_REVIEW,
            total_price=ORDER_TOTAL,
            hall_price=ORDER_HALL,
            sales_user=self.sales,
        )
        self.main = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=TAKER_TEMP_BEFORE,
            bonus_amount=Decimal("0.00"),
            review_status=ReviewStatus.PENDING,
        )
        self.customer = Performance.objects.create(
            order=self.order,
            user=self.sales,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("0.00"),
            bonus_amount=Decimal("0.00"),
        )
        self.client = make_logged_in_client(
            username="p1-guard-clerk", groups=("客服乙",), nodes=NODES
        )
        #: `settle_performances` 的 `user=` 是**数据权限那一维**（不是端点调用方）。
        self.clerk = User.objects.get(username="p1-guard-clerk")

    # ── 断言用的两个出口 ────────────────────────────────────────────────
    def _review(self):
        response = self.client.post(
            REVIEW_URL.format(pk=self.main.pk),
            {"approved": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def test_a_settled_customer_row_survives_the_second_completion(self):
        # ① 第一次完成：钩子写下权威提成。
        body = self._review()
        self.assertEqual(body["code"], 0, body)
        self.main.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.main.bonus_amount, TAKER_TEMP_BEFORE)
        self.assertEqual(self.customer.bonus_amount, CUSTOMER_BEFORE)
        self.assertEqual(self.customer.temp_amount, CUSTOMER_BEFORE)
        self.assertEqual(self.customer.commission_rate, SALES_RATE)

        # ② 财务把**客服行**结掉（只结这一行；打手那行不受影响）。
        result = settle_performances(
            [self.customer.pk], actor="财务甲", user=self.clerk
        )
        self.assertEqual(result.settled_performances, [self.customer.pk])
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.settle_status, SettleStatus.SETTLED)
        self.assertEqual(self.customer.settled_amount, CUSTOMER_BEFORE)

        # ③ 订单回退到待验收（既有流程：重开再完成）。
        response = self.client.post(
            TRANSITION_URL.format(order_no=self.order.order_no),
            {"target": OrderStatus.PENDING_REVIEW},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.json()["code"], 0, response.json())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)

        # ④ 重开之后打手那一侧又变了（重新交单 + 预计绩效 17.60 → 30.00）。
        #    ⚠️ 直接写库：本用例要钉的是**第二次完成时**钩子的行为，
        #    「怎么变成 30.00 的」不是判据（走 `/adjust-amount/` 会牵进 P1-6 那条线）。
        Performance.objects.filter(pk=self.main.pk).update(
            review_status=ReviewStatus.PENDING, temp_amount=TAKER_TEMP_AFTER
        )

        # ⑤ 再 `review`（通过 ⇒ 完成）：钩子会跑，但客服行**已结账**⇒ 一个字节都不动。
        # ⚠️ 断言写在 `with` **里面**：`assertLogs` 在退出时才检查「到底有没有 warning」，
        #    放在外面的话「金额被改写」那条主判据会被它抢先掩盖（红得不是地方）。
        with self.assertLogs("apps.business.settlement_hooks", level="WARNING") as logs:
            body = self._review()
            self.assertEqual(body["code"], 0, body)

            self.main.refresh_from_db()
            self.customer.refresh_from_db()
            # 钩子**确实跑了**：打手那行按新的预计绩效重写
            # （否则本用例可以靠「整个钩子没跑」通过）。
            self.assertEqual(self.main.bonus_amount, TAKER_TEMP_AFTER)
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, OrderStatus.COMPLETED)
            # 已结账的客服行：两列金额与提成率快照**一个字节都不动**。
            self.assertEqual(self.customer.bonus_amount, CUSTOMER_BEFORE)
            self.assertEqual(self.customer.temp_amount, CUSTOMER_BEFORE)
            self.assertEqual(self.customer.commission_rate, SALES_RATE)
            # 判别力自证：这条链本该算出 `CUSTOMER_IF_UNGUARDED`，它必须**没有**被写进去。
            self.assertNotEqual(self.customer.bonus_amount, CUSTOMER_IF_UNGUARDED)
            # 留痕（warning）能指认是哪一行，且说清为什么没写。
            messages = [record.getMessage() for record in logs.records]
            self.assertTrue(
                any(
                    str(self.customer.pk) in message and "已结账" in message
                    for message in messages
                ),
                messages,
            )
