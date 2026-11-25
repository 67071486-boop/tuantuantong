"""收款状态端点 `POST /api/business/orders/<单号>/payment-status/`（spec §5.1 / §8 第 1–5 条）。

判据一律打在**数据库行**上（既有纪律：断言留痕要打真实出口，不打日志或响应体）；
403 / 401 两个方向都断（只测通过等于没测）；同值调用不留痕但仍推 `updated_by`。
"""
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from decimal import Decimal

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.refunds import refund_set_total
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE = "/api/business/orders"
SETTLE_URL = "/api/business/settlements/settle"
#: 已结账的绩效行不许再改订单状态 ⇒ 退单前必须先撤销结账。
REVOKE_URL = "/api/business/settlements/revoke"
NODE = "order.change_payment_status"


class PaymentStatusEndpointTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # ⚠️ **硬化（可选项 A）**：这里刻意用**非内置组名**——`Role` 是**租户级共享**的，
        #    而 `_grant_nodes` 的语义是「往该用户所在各组的同名 `Role` 上加权限」⇒ 组名一旦与别处
        #    （或内置角色）同名，节点集就会互相泄漏：探针实测，同一次测试里**同组**的第二个账号
        #    哪怕自己不声明 `nodes=`，也照样继承 `setUp` 挂上去的节点。用非内置组名 + 显式 `nodes=`
        #    之后，本账号的节点集**只来自这一行声明**（同文件退款那一腿的 `groups=("退款小组",)` 手法）。
        #    ⛔ 更正：本注释早先写「内置『客服』的 `Role` 按 `order` 栏目整批授予」——实测**不是**，
        #    `TenantTestCase` 不调 `seed_builtin_roles()`（见 `tests/p5_auth_helpers.py` 的 docstring），
        #    探针实测 `Role(客服).permissions.count() == 1`（只有显式挂的那一个）。
        self.client = make_logged_in_client(
            username="paystat-cs", groups=("收款组",), nodes=(NODE, "order.list", "order.add_edit")
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。收款状态端点是写路径：取单过 `game_scope`
        # （读不到 ⇒ 1002）、写侧过 `assert_game_allowed`（⇒ 业务码 1000）——本类
        # 前七条用例的每一次 `_post` 都是对本账号 `self.game` 那张单的操作。
        grant_games(User.objects.get(username="paystat-cs"), self.game)

    def _order(self, no="20260923000000000401", **kw):
        # ⚠️ 计划里的版本把 `status=OrderStatus.UNASSIGNED` 写死再 `**kw` ⇒ 传 `status=`
        #    会 `TypeError`（计划自带的缺陷）。改成 `setdefault`：默认仍是未派单，
        #    但要建「已完成」的单（不联动那条要退单 / 结账）时能覆盖。
        kw.setdefault("status", OrderStatus.UNASSIGNED)
        return Order.objects.create(game=self.game, order_no=no, **kw)

    def _post(self, no, payload):
        return self.client.post(
            f"{BASE}/{no}/payment-status/", payload, content_type="application/json"
        ).json()

    def test_each_of_the_three_tiers_lands_in_the_database_row(self):
        order = self._order()
        for code in ("SETTLING", "SETTLED", "UNSETTLED"):
            body = self._post(order.order_no, {"payment_status": code})
            self.assertEqual(body["code"], 0, (code, body))
            self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, code)

    def test_bad_values_are_validation_errors_and_leave_the_row_unchanged(self):
        order = self._order(payment_status="SETTLING")
        for payload in ({}, {"payment_status": ""}, {"payment_status": "未收款"}, {"payment_status": "BOGUS"}):
            # ⚠️ **硬化（可选项 B）**：spec §5.1 / §5.4 写的是「**HTTP 200** + `code=1000`」
            #    ——只断信封里的 `code` 钉不住那个 200（E-0010 同族：只测「翻译对了」，不测
            #    「没变成 500」）。所以这里把状态码也断上（T4 同类用例已这么写）。
            response = self.client.post(
                f"{BASE}/{order.order_no}/payment-status/",
                payload,
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200, (payload, response.content[:200]))
            body = response.json()
            self.assertEqual(body["code"], 1000, (payload, body))
            self.assertEqual(
                Order.objects.get(order_no=order.order_no).payment_status, "SETTLING", payload
            )

    def test_the_two_actor_sources_are_kept_apart(self):
        """`Order.updated_by`（业务列）取**请求体** `actor`；留痕的 `actor` 取**认证态**。

        spec §5.1 明写本端点的业务列走 `self._actor(request)`（与 `/price/` 同源），
        而留痕的操作人取 `request.user.get_username()`——两个值**刻意不同源**（R20：
        请求体里自称的身份可伪造，不能进留痕）。所以本用例故意送一个与登录名不同的
        `actor`，两个落点各断各的字面值。
        """
        order = self._order()
        body = self._post(order.order_no, {"payment_status": "SETTLED", "actor": "冒名者"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.get(order_no=order.order_no).updated_by, "冒名者")
        log = OperationLog.objects.get(
            action="order.change_payment_status", order_no=order.order_no
        )
        self.assertEqual(log.actor, "paystat-cs")

    def test_change_is_recorded_once_with_chinese_labels(self):
        order = self._order()
        self._post(order.order_no, {"payment_status": "SETTLED"})
        logs = OperationLog.objects.filter(
            action="order.change_payment_status", order_no=order.order_no
        )
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.field_name, "payment_status")
        self.assertEqual(log.from_value, "未收款")
        self.assertEqual(log.to_value, "已结款")
        self.assertEqual(log.actor, "paystat-cs")
        self.assertEqual(log.log_type, 10)
        self.assertEqual(log.target_kind, "order")
        self.assertEqual(log.target_id, order.order_no)

    def test_same_value_writes_no_new_log_row_but_still_saves(self):
        order = self._order(payment_status="SETTLED")
        body = self._post(
            order.order_no, {"payment_status": "SETTLED", "actor": "最后确认的人"}
        )
        self.assertEqual(body["code"], 0, body)
        self.assertFalse(
            OperationLog.objects.filter(
                action="order.change_payment_status", order_no=order.order_no
            ).exists()
        )
        self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, "SETTLED")
        # 仍执行 save ⇒ 谁最后确认过这一档也留痕（与 `/price/` 同口径：业务列取请求体 actor）
        self.assertEqual(
            Order.objects.get(order_no=order.order_no).updated_by, "最后确认的人"
        )

    def test_the_write_rolls_back_when_the_audit_row_fails(self):
        """写动作与留痕**同事务**（spec §10 第 7 条）：留痕抛错 ⇒ `payment_status` 也回滚。

        ⛔ 本条是**补的**（整体评审 A-M5）：本端点在收口时**没有**同型用例
        ⇒「`record` 抛错时业务列必须回滚」这条性质无用例可证（实现形状与 `/hall-price/` 相同）。
        照 `tests/test_order_hall_price_api.py::test_the_write_rolls_back_when_the_audit_row_fails`
        同形；桩打在 `order_writes.record`（**被查找的那个模块**，不是定义它的 `apps.audit.log`，
        见 E-0029）。⚠️ A1 把本动作体搬进了 `apps.business.order_writes`（唯一判据出处，
        spec §D5）⇒ 查找点随之搬走，桩的**目标模块**从 `order_views` 改成 `order_writes`；
        期望值与用例语义**一个字未改**。判别力：把核心函数里的 `with transaction.atomic():`
        去掉 ⇒ 列已提交、本用例红。
        """
        order = self._order(payment_status="SETTLING")

        def _boom(**kwargs):
            raise RuntimeError("留痕写不进去")

        with mock.patch("apps.business.order_writes.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"{BASE}/{order.order_no}/payment-status/",
                    {"payment_status": "SETTLED"},
                    content_type="application/json",
                )
        self.assertEqual(
            Order.objects.get(order_no=order.order_no).payment_status, "SETTLING"
        )
        self.assertFalse(
            OperationLog.objects.filter(
                action="order.change_payment_status", order_no=order.order_no
            ).exists()
        )

    def test_a_partial_refund_a_refund_transition_and_a_settlement_do_not_move_it(self):
        """spec §8 第 5 条：退款 / 退单 / 结账都不联动这条裁定钉成用例。

        判据是**三个动作各自执行后**该列仍是 `SETTLED`（不是「没写过」——最前面的成功调用
        是对照）。三条腿的顺序：结账要求订单仍在 `COMPLETED`，所以它排在退单之前。
        """
        order = self._order(status=OrderStatus.COMPLETED, total_price="88.00")
        body = self._post(order.order_no, {"payment_status": "SETTLED"})
        self.assertEqual(body["code"], 0, body)

        # ① 结账（绩效行要已生效：订单 `COMPLETED` + 有归属人）
        taker = User.objects.create_user(username="paystat-taker")
        perf = Performance.objects.create(
            order=order,
            user=taker,
            emp_type=EmpType.MAIN,
            temp_amount="20.00",
            bonus_amount="20.00",
            commission_rate="10.00",
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )
        mover = make_logged_in_client(username="paystat-mover", is_superuser=True)
        resp = mover.post(
            SETTLE_URL,
            {"performance_ids": [perf.id]},
            content_type="application/json",
        ).json()
        self.assertEqual(resp["code"], 0, resp)
        self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, "SETTLED")

        # ② 部分退款（`POST …/refunds`，要 `order.online_refund`）
        # ⚠️ `nodes=` 只挂到用户**已有**的 Group 上（`p5_auth_helpers._grant_nodes`），
        #    所以必须同时给一个组名；用一个非内置组名，免得蹭到「客服」角色的整批节点。
        refunder = make_logged_in_client(
            username="paystat-refunder",
            groups=("退款小组",),
            nodes=("order.online_refund",),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。退款是写路径（取单过 `game_scope`、写侧过
        # `assert_game_allowed`）⇒ 发起退款的这位账号必须显式持有这一单的游戏，
        # 否则它拿到 1000，本用例退化成「退款根本没发生」（钱那一腿空转）。
        grant_games(User.objects.get(username="paystat-refunder"), self.game)
        resp = refunder.post(
            f"{BASE}/{order.order_no}/refunds",
            {"refund_total": "10.00", "reason": "退差价", "request_id": "paystat-refund-1"},
            content_type="application/json",
        ).json()
        self.assertEqual(resp["code"], 0, resp)
        self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, "SETTLED")

        # ③ 退单：`/transition/` 到 REFUNDED（要 `order.change_status`，本账号没有 ⇒ superuser 走一遍）
        # 已结账的绩效行不许再改订单状态，退单前必须先撤销结账。
        # 本条钉的是「退款 / 退单 / 结账 / 撤销结账都不联动 `payment_status`」。
        # 不能把这一步改成「期望被拒」，否则就钉不住「不联动」。
        resp = mover.post(
            REVOKE_URL,
            {"performance_ids": [perf.id], "reason": "退单前先撤销结账"},
            content_type="application/json",
        ).json()
        self.assertEqual(resp["code"], 0, resp)
        self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, "SETTLED")
        resp = mover.post(
            f"{BASE}/{order.order_no}/transition/",
            {"target": OrderStatus.REFUNDED},
            content_type="application/json",
        ).json()
        self.assertEqual(resp["code"], 0, resp)
        self.assertEqual(Order.objects.get(order_no=order.order_no).payment_status, "SETTLED")

    def test_a_user_without_the_node_gets_403(self):
        order = self._order()
        # `Role` 在租户里是共享的。同组第二个账号不声明节点也会继承已挂上的节点，断言会放行。
        # 本条用「持别的节点、不持本节点」的账号，403 才是节点级判据。
        nobody = make_logged_in_client(
            username="paystat-nobody", groups=("旁观组",), nodes=("order.list",)
        )
        # 阳性对照：同一账号**读得动列表**（它确实持有 `order.list`）⇒ 这个账号不是空节点集，
        # 下面拿到的 403 只可能来自「缺 `order.change_payment_status`」。
        self.assertEqual(nobody.get(BASE + "/").status_code, 200)
        resp = nobody.post(
            f"{BASE}/{order.order_no}/payment-status/",
            {"payment_status": "SETTLED"},
            content_type="application/json",
        )
        body = resp.json()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(body["code"], 403, body)
        # 同一次运行里有配对证据（本类其余用例带 `NODE` ⇒ 200），所以这里断的是
        # 「403 不是 CSRF 那一类」——403 在 P5 有多个来源（Ruling 32）。
        self.assertNotEqual(body["msg"], "")
        self.assertNotIn("CSRF", body["msg"])

    def test_anonymous_gets_401(self):
        order = self._order()
        anon = Client(SERVER_NAME="tenant.test.com")
        resp = anon.post(
            f"{BASE}/{order.order_no}/payment-status/",
            {"payment_status": "SETTLED"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)


class PaymentStatusStaysSettledWhenOrderRefundsTests(TenantTestCase):
    """⭐ D-0336（2026-10-30 拍板②）：收款已标「已结款」，之后订单退款 ⇒ 标记保持不动。

    上面的 `test_a_partial_refund_a_refund_transition_and_a_settlement_do_not_move_it`
    在**端点层**钉了同一条裁定（走 HTTP 视图）；本类在**域层**直接驱动写入路径
    （`apply_transition` 的退单钩子 / `refund_set_total` 部分退款写账），不依赖端点
    包装——将来谁给退款链加「顺手回改收款状态」的代码，两层各有一条会红。
    判据打在**数据库行**上：`Order.payment_status` 在退款动作之后仍是字面 `"SETTLED"`。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _settled_order(self, no: str) -> Order:
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.COMPLETED,
            total_price="88.00",
            payment_status="SETTLED",
        )

    def test_a_full_refund_transition_keeps_the_settled_mark(self):
        """退单（`COMPLETED → REFUNDED`，跑全额退款钩子）后标记不动。"""
        order = self._settled_order("20261030000000000701")
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(order.payment_status, "SETTLED")

    def test_a_partial_refund_write_keeps_the_settled_mark(self):
        """部分退款（`refund_set_total` 写 `refund_fee` + `OrderRefund`）后标记不动。"""
        order = self._settled_order("20261030000000000702")

        row = refund_set_total(order, Decimal("10.00"), "退差价", "d0336-1", "cs-1")

        self.assertIsNotNone(row)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("10.00"))
        self.assertEqual(order.payment_status, "SETTLED")
