"""P4FU-A：订单「已完成」按权限节点可达 + **完成即绩效生效**（D-0071，业务方 2026-09-22）。

业务方裁定原文（`docs/DECISIONS.md` D-0071）：「对标物没有那个审核状态吧？对标物只要订单
状态变成完成后绩效就生效了」。落地的三处：

1. **状态机**：订单**待验收**（`PENDING_REVIEW`）时能到 `COMPLETED`（`order_status.py`），边的节点集只表达
   「走这一步要哪些节点」——**授权判据在权限节点上**。
   ⚠️ **批 F / F3，spec §2.3）**：入 `COMPLETED` 的边只剩
   `PENDING_REVIEW → COMPLETED` 一条（D-0071 的「六个来源态都能到」已被收紧），
   所以本文件的夹具**统一把来源态放在「待验收」**——判据（钱 / 节点门 / 不 500）
   一条没改，只换了到达完成态的那条合法路径。`/review/` 仍走同一条边。
2. **端点**：通用 `/transition/` 的路径守卫删除；`target=COMPLETED` 判节点
   `order.change_status_completed`（17），其余目标判 `order.change_status`（16）。
3. **钱**：`settlement_hooks.on_order_completed` 去掉「打手行必须已通过」那道闸门。

本文件是**本线的承重判据**（本文件之外，既有测试的改前→改后逐条写在
`.superpowers/sdd/2026-09-22-p4-followup/task-P4FU-A-report.md`）：

- **节点就是授权**：持 17 ⇒ 能完成（`code 0`）；只持 16 ⇒ **403** 且订单状态不变、零留痕；
  只持 17 ⇒ **不能**改成非已完成的目标（按目标态挑 code 的另一半）。
- **完成即生效**：客服 SALES 行的 `bonus_amount` 被写成「利润 × 提成率」；
  打手行的金额**不动**（它由 `/submit` 写）。
- **不 500**：无接单人 / 无客服行 / 提成模式不匹配 / 无价，四种分支各有**确定结果**。
- **跨租户**：以租户 A 的凭据去动**只在 B 里存在**的订单，必须失败且 B 的行不动。
  ⚠️ 判据用 `order_no`（租户内唯一、两租户取值不同），**不用主键**——两个 schema 的主键
  序列各自从 1 开始，`filter(id=...)` 在 B 里命中的是 B 自己的行（AGENTS.md §5）。

⚠️ **期望值一律写字面值**（E-0024）：业务码写 `0` / `403` / `1000` / `1002`，金额写
`Decimal("9.60")` 这种十进制字面量——不引 `ErrorCode.*` 当期望值（自指断言改坏常量时不红）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
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
    ReviewStatus,
)
from apps.business.performance import (
    create_performance_for_order,
    main_performance_of,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 「已完成」的授权节点（对标 Menu_ID 17）。
COMPLETED_NODE = "order.change_status_completed"
#: 「非已完成」的授权节点（对标 Menu_ID 16）。
PLAIN_NODE = "order.change_status"


class CompletionFixtureMixin:
    """建单 / 建绩效 / 建登录 client 的小夹具（各用例只表达**自己的差异**）。

    ⚠️ **每个 client 用独立的节点组**（`节点{n}`）：节点挂在 `Role` 上，而 `Role.name`
    唯一、与 `Group` 一对一 ⇒ 同组名会**共享**节点，后一个 client 会继承前一个的授予
    （`test_rbac_endpoint_enforcement.py::OrderEndpointNodeGateTests._cs` 的 docstring
    有实测记录）。职位组（`客服`）可以共用——本文件里它的节点一直是空的。
    """

    _GROUP_SEQ = 0

    def setUp(self):
        from apps.rbac.seed import seed_catalog

        seed_catalog()
        self.game = Game.objects.create(name="原神")

    def _client(self, username, *codes, role_group="客服", domain="tenant.test.com"):
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            domain=domain, username=username, groups=(role_group, node_group)
        )
        from apps.rbac.models import PermissionNode, Role

        user = User.objects.get(username=username)
        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # `/transition/` 取订单走 `get_object()` ⇒ 过 `game_scope`；本文件每张单都归
        # `self.game` ⇒ 每个发请求的账号都要**显式**授这款游戏，否则拿到 200 + 1002
        # （「记录不存在」），而不是本用例要断的那条分支。
        # ⛔ 节点 17 / 16 的判据与「只持 16 ⇒ 403」的断言一个字没动。
        # ⛔ 只授**登录账号**：`_sales` 建的那些「没有提成档案」的账号是本文件要断言的对象，
        #    刻意不给它们补档案（补了就成恒真）。
        grant_games(user, self.game)
        return client

    def _sales(self, username, *, mode=CommissionMode.PROFIT_PERCENT, value="20.00"):
        user = User.objects.create_user(username=username)
        StaffProfile.objects.create(
            user=user, commission_mode=mode, commission_value=Decimal(value)
        )
        return user

    def _order(self, no, *, status=OrderStatus.PENDING_REVIEW, sales=None, **overrides):
        fields = {"hall_price": Decimal("88.00")}
        fields.update(overrides)
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            sales_user=sales,
            **fields,
        )

    @staticmethod
    def _post(client, url, payload):
        return client.post(url, payload, content_type="application/json")


# ── ① 节点就是授权：持 17 ⇒ 能完成 / 只持 16 ⇒ 403 ─────────────────────
class CompletedNodeGateTests(CompletionFixtureMixin, TenantTestCase):
    """`target=COMPLETED` 由**节点 17** 授权（D-0071）；其余目标仍由节点 16 授权。

    拒绝侧是**同一个职位**（客服，状态机角色对得上），所以 403 只能来自节点门——
    只换节点、不换角色，判据才落在节点上。
    """

    def _order_no(self):
        return "20260922000000000001"

    def test_holder_of_node_17_can_complete(self):
        """**只持 17、不持 16** 的客服也能把订单改成已完成（`code 0`）。

        ⚠️ 「只持 17」是刻意的：如果实现写成「类级门 16 且 target=COMPLETED 再要 17」，
        这个人会被 16 那道门挡下 ⇒ 本用例红。业务方口径是「**有权限的职位都可以用**」，
        而「有权限」对「改成已完成」这一个动作就是节点 17。
        """
        order = self._order(self._order_no())
        create_performance_for_order(order)
        client = self._client("gate17-only", COMPLETED_NODE)
        response = self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
        )
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        # 迁移必须留痕（spec §6 硬规则 2），断言**数据库行**（E-0009）。
        # ⭐ Task L（合并 `main`）：`/transition/` 的留痕已随系统操作日志线
        # 从旧表迁到 `OperationLog`（唯一写入出口 `record()`，动作 `order.transition`），
        # 前后值是**中文标签**（spec §5.1）。期望值写**字面值**（E-0024）。
        row = OperationLog.objects.get(
            order_no=order.order_no, action="order.transition"
        )
        self.assertEqual(row.from_value, "待验收")
        self.assertEqual(row.to_value, "已完成")
        self.assertEqual(row.actor, "gate17-only")

    def test_holder_of_node_16_only_is_denied_with_403(self):
        """**只持 16、不持 17** ⇒ **403**（不是 200 + 业务码），订单状态不变、零留痕。"""
        order = self._order(self._order_no())
        create_performance_for_order(order)
        client = self._client("gate16-only", PLAIN_NODE)
        response = self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()["code"], 403)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)
        self.assertFalse(
            OperationLog.objects.filter(order_no=order.order_no).exists()
        )

    def test_node_17_does_not_open_the_non_completed_targets(self):
        """**只持 17** 的人改**非已完成**的目标 ⇒ 403（那要 16）。

        与 `test_holder_of_node_17_can_complete` 配对：两条一起才证明判据是
        「**按目标态挑 code**」，而不是「16 或 17 任一即可」的宽口径。
        """
        order = self._order(self._order_no(), status=OrderStatus.PENDING_REVIEW)
        client = self._client("gate17-other", COMPLETED_NODE)
        response = self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_allowed_transitions_lists_completed_only_for_the_holder(self):
        """详情字段与端点**同一个判据**：持 17 ⇒ 列表里有 `COMPLETED`，不持 ⇒ 没有。

        （行为侧的「列表 == 端点接受集合」由
        `tests/test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint`
        拿端点当 oracle 钉住；本条只把**节点维度**的差异显式钉一次。）
        """
        order = self._order(self._order_no())
        url = f"/api/business/orders/{order.order_no}/"
        # `order.view_detail` 是详情读本身的门（与 `allowed_transitions` 的节点判据无关）。
        holder = self._client("list-with-17", COMPLETED_NODE, PLAIN_NODE, "order.view_detail")
        without = self._client("list-without-17", PLAIN_NODE, "order.view_detail")

        with_node = holder.get(url).json()["data"]["allowed_transitions"]
        without_node = without.get(url).json()["data"]["allowed_transitions"]

        self.assertIn(OrderStatus.COMPLETED, with_node)
        self.assertNotIn(OrderStatus.COMPLETED, without_node)
        # 阳性哨兵：不持 17 的人列表**非空**（否则「字段对所有人变空」也会让上面那条通过）
        self.assertTrue(without_node, without_node)


# ── ② 完成即生效：钱 + 三个绝不 500 的分支 ──────────────────────────────
class CompletionDeterminesMoneyTests(CompletionFixtureMixin, TenantTestCase):
    """D-0071 的**钱**这一侧：订单完成 ⇒ 绩效即生效。

    金额实数（都是字面值）：`total_price = 108.00`（D-0142：提成基数只看订单总价）、
    `received_price = 1.00`（**陷阱值**）、`hall_price = 88.00`、
    打手 `temp_amount = bonus_amount = 60.00` ⇒ 利润 = 108.00 − 60.00 = **48.00**；
    提成率 **20.00**（利润百分比）⇒ 客服 `bonus_amount = 48.00 × 20 / 100 = **9.60**`。
    ⭐ **R5**：完成时打手 `bonus := temp`（覆盖生成）⇒ 夹具把两者都设成 `60.00`，
    这样「减项 = 打手生效绩效 60.00」这个前提在生成之后仍成立。
    """

    def _complete(self, client, order):
        return self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
        )

    def test_completing_determines_the_customer_commission(self):
        """完成 ⇒ 客服 SALES 行被写 `9.60` / `20.00`；打手行按 `temp` 生成（同值 `60.00`）。"""
        sales = self._sales("money-sales")
        order = self._order(
            "20260922000000000011",
            sales=sales,
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下提成基数只看总价
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="money-taker")
        main.temp_amount = Decimal("60.00")
        main.bonus_amount = Decimal("60.00")
        main.review_status = ReviewStatus.PENDING      # ← 「没审过」不再是障碍
        main.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "review_status", "updated_at"
            ]
        )
        client = self._client("money-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(customer.commission_rate, Decimal("20.00"))
        # ⭐ R5：完成时 `bonus := temp`（两者同值 60.00）并把 `review_status` 置「已通过」
        self.assertEqual(main.bonus_amount, Decimal("60.00"))
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)

    def test_order_without_a_taker_still_completes_and_counts_zero_taker_pay(self):
        """**无接单人**（MAIN 行在、`user` 为空）也能完成：打手 `bonus_amount = 0.00`
        ⇒ 客服提成按 `净实收 × 提成率` = `108.00 × 20% = 21.60`。

        判据：① 不 500；② 订单真的到 `COMPLETED`；③ 客服行拿到**具体十进制值**。
        """
        sales = self._sales("notaker-sales")
        order = self._order(
            "20260922000000000012",
            status=OrderStatus.PENDING_REVIEW,
            sales=sales,
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下提成基数只看总价
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        self.assertIsNone(main.user_id)                # 夹具自证：真的没有接单人
        client = self._client("notaker-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertIsNone(main.user_id)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("21.60"))

    def test_order_without_a_customer_row_completes_and_writes_nothing(self):
        """**无客服行**（`sales_user` 为空）⇒ 完成成功、不建行、不报错。

        口径：这笔客服提成**不存在**（没有归属人），等 P4-8「待归属」兜底；
        `on_order_completed` 在这一支**直接返回**，连价都不读（所以这张单可以不填价）。
        """
        order = self._order(
            "20260922000000000013",
            status=OrderStatus.PENDING_REVIEW,
            sales=None,
            hall_price=None,
            received_price=None,
        )
        create_performance_for_order(order)
        client = self._client("norow-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )

    def test_commission_mode_mismatch_is_a_business_code_not_500(self):
        """**提成模式不匹配**（档案是订单价百分比、客服路径要利润百分比）⇒
        HTTP 200 + `code 1000`，订单**不**变成已完成、客服行一分钱没写。

        判别力：`CommissionError` 是**预期失败**，必须被端点翻成业务码并整笔回滚。
        没有 `except` 时它是未捕获异常 ⇒ **HTTP 500**（本用例当场红）。
        """
        sales = self._sales("mismatch-sales", mode=CommissionMode.ORDER_PERCENT)
        order = self._order(
            "20260922000000000014",
            status=OrderStatus.PENDING_REVIEW,
            sales=sales,
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下提成基数只看总价
        )
        create_performance_for_order(order)
        client = self._client("mismatch-cs", COMPLETED_NODE)

        response = self._complete(client, order)

        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("提成模式", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)   # 整笔回滚
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.commission_rate)
        self.assertFalse(
            OperationLog.objects.filter(order_no=order.order_no).exists()
        )

    def test_order_with_only_a_total_price_completes_and_pays_the_commission(self):
        """⭐ P4FU-F 判据 5（业务方要的那条端到端）：**只填了订单总价**的订单能改成「已完成」。

        夹具：`received_price = None`、`hall_price = None`、`total_price = 108.00`、
        打手 `temp_amount = bonus_amount = 60.00`、提成率 20%（利润百分比）⇒ 利润 `48.00`
        ⇒ 客服 `bonus_amount = 48.00 × 20 / 100 = ` **`9.60`**。
        走**真实端点** `POST /api/business/orders/{no}/transition/`（`target=COMPLETED`），
        断言 HTTP 200 + `code 0` + 具体十进制值（E-0024 字面值）。
        ⭐ **R5**：完成时打手 `bonus := temp` ⇒ 夹具的两列都设成 `60.00`。

        ⚠️ **改前这里是业务码 `1000`「该单没有实收款」**（`net_received` 实收款/大厅价双空
        ⇒ `AmountError`）。业务方裁定回落用**订单总价**之后，这条路径才成立。
        """
        sales = self._sales("totalonly-sales")
        order = self._order(
            "20260922000000000016",
            status=OrderStatus.PENDING_REVIEW,
            sales=sales,
            hall_price=None,
            received_price=None,
            total_price=Decimal("108.00"),
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="totalonly-taker")
        main.temp_amount = Decimal("60.00")
        main.bonus_amount = Decimal("60.00")
        main.review_status = ReviewStatus.PENDING
        main.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "review_status", "updated_at"
            ]
        )
        client = self._client("totalonly-cs", COMPLETED_NODE)

        response = self._complete(client, order)

        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(customer.commission_rate, Decimal("20.00"))

    def test_order_without_any_price_completes_with_a_zero_commission(self):
        """P4FU-F 判据 3 的端点侧：三处价都空（`total_price` 取模型默认 `0.00`）⇒
        **照常完成**，客服提成 `0.00`（利润 `0.00 − 0.00`），**不再**是业务码 `1000`。

        ⚠️ 本条**替换**原 `test_order_without_any_price_is_a_business_code_not_500`
        （它钉「无价 ⇒ `1000` 且订单不完成」）。理由：那断言依赖 `net_received` 的
        「双空 ⇒ `AmountError`」分支，而该分支随 P4FU-F 的口径变更**已删除、不可达**
        （brief §1.4 明令删不可达错误分支与其文案）。**这是行为变更、不是放宽校验**：
        「没填价格」现在等价于「订单总价 0」 ⇒ 提成 0。

        判别力：夹具 `hall_price=None` ⇒ 若有人在回落链里「先试大厅价」（或把回落档写回
        `hall_price`），这里会读不到价、抛异常 ⇒ HTTP 500 / 非 `code 0` ⇒ 红。
        """
        sales = self._sales("noprice-sales")
        order = self._order(
            "20260922000000000015",
            status=OrderStatus.PENDING_REVIEW,
            sales=sales,
            hall_price=None,
            received_price=None,
        )
        create_performance_for_order(order)
        client = self._client("noprice-cs", COMPLETED_NODE)

        response = self._complete(client, order)

        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))     # 利润 0.00 × 20%
        self.assertEqual(customer.commission_rate, Decimal("20.00"))

    def test_missing_commission_profile_falls_back_to_full_profit(self):
        """**没有提成档案 / 档案值为 `0` ⇒ 回落 100%**：客服拿走**全部利润**。

        评审后修复轮 I-2：这条口径此前只有读码，**没有测试**（方向是**多发钱**，最该钉）。
        实数：`total_price = 108.00`（`received_price = 1.00` 是**陷阱值**）；
        回落率 `100.00` ⇒ 客服 `commission_rate = 100.00`（期望值全是字面值，E-0024）。

        预计减打手预计，生效减打手生效。本夹具的 MAIN 行没有接单人，预计减项是
        `0.00` ⇒ `temp = 108.00`；`bonus_amount` 被摆成 `60.00` ⇒ `bonus = 48.00`。
        两列分叉只在 MAIN 的预计 ≠ 生效时出现。完成单事后改价会把 MAIN 两列写成同一个新值。
        本档是这种数据形态，不是「改价后两列一起动」的反例。

        依据：`commission.py` 模块 docstring 第 3 条 + Q3 裁定 + P4-5 的 Ruling M
        （**两个 mode 一致**：无档案或 `value == 0` 都当「未配置」）。
        代价（既有、已登记）：业务上**无法**把某人设成「不付提成」——要改就是改发钱结果。
        """
        cases = (
            ("无档案", None, "20260922000000000021", "fallback-noprofile"),
            ("档案值为 0", "0.00", "20260922000000000022", "fallback-zero"),
        )
        for label, value, order_no, username in cases:
            with self.subTest(case=label):
                if value is None:
                    sales = User.objects.create_user(username=username)
                else:
                    sales = self._sales(username, value=value)
                order = self._order(
                    order_no,
                    status=OrderStatus.PENDING_REVIEW,
                    sales=sales,
                    total_price=Decimal("108.00"),
                    received_price=Decimal("1.00"),    # 陷阱值：新口径下提成基数只看总价
                )
                create_performance_for_order(order)
                main = main_performance_of(order)
                main.bonus_amount = Decimal("60.00")
                main.save(update_fields=["bonus_amount", "updated_at"])
                client = self._client(f"fallback-cs-{order_no[-1]}", COMPLETED_NODE)

                body = self._complete(client, order).json()

                self.assertEqual(body["code"], 0, body)
                customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
                # ⛔ 两列各按自己的减项（`D-0182` 的逐字原文）——
                # 「预计」减打手预计（MAIN 行 `user` 为空 ⇒ `0.00`）⇒ `108.00`；
                # 「生效」减打手生效（`60.00`）⇒ `48.00`。
                self.assertEqual(customer.bonus_amount, Decimal("48.00"))
                self.assertEqual(customer.temp_amount, Decimal("108.00"))
                self.assertEqual(customer.commission_rate, Decimal("100.00"))

    def test_order_with_only_a_customer_row_completes_without_500(self):
        """**只有客服行、完全没有打手行** ⇒ 完成**不 500**，按「无接单人」口径算（减项 0）。

        评审后修复轮 I-4：`on_order_completed` 原来调 `main_performance_of(order)`
        （`Performance.objects.get(...)`，缺行即抛 `Performance.DoesNotExist`）⇒ 这种数据形态
        一完成就是**未捕获异常 = 500**。现在改成容错：缺打手行 = **没有接单人**（同一业务含义），
        减项按 `0.00` 算。

        ⚠️ 方向是**多发钱**（减项 0 ⇒ 利润更大 ⇒ 客服提成更高），所以必须有本条钉住它。
        实数：`total_price = 108.00`（`received_price = 1.00` 是**陷阱值**）、没有打手行
        ⇒ 利润 `108.00`、提成率 `20.00`
        ⇒ 客服 `21.60`。
        """
        sales = self._sales("salesonly-sales")
        order = self._order(
            "20260922000000000023",
            status=OrderStatus.PENDING_REVIEW,
            sales=sales,
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下提成基数只看总价
        )
        # 夹具自证：**只**建客服行（不调 `create_performance_for_order`）
        Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=sales,
            temp_amount=Decimal("0.00"),
            bonus_amount=Decimal("0.00"),
        )
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        client = self._client("salesonly-cs", COMPLETED_NODE)

        response = self._complete(client, order)

        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("21.60"))

    def test_the_commission_base_is_the_total_price_not_the_received_price(self):
        """⭐ spec §8 第 14 条：完成时客服提成 = `(total_price − refund_fee − 打手 bonus) × 率`。

        夹具：`total_price=100.00`、`received_price=0.00`（旧口径下利润是 `−40.00` ⇒ 提成 `−8.00`）、
        打手 `temp_amount = bonus_amount = 40.00`、客服率 20%（利润百分比）⇒ 利润 `60.00`
        ⇒ 客服 `12.00`。
        期望值写字面值；`received_price=0.00` 是**陷阱值**。
        ⭐ **R5**：完成时打手 `bonus := temp` ⇒ 夹具的两列都设成 `40.00`。
        """
        sales = self._sales("base-sales")
        order = self._order(
            "20260922000000000031",
            sales=sales,
            total_price=Decimal("100.00"),
            received_price=Decimal("0.00"),
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="base-taker")
        main.temp_amount = Decimal("40.00")
        main.bonus_amount = Decimal("40.00")
        main.save(update_fields=["user", "temp_amount", "bonus_amount", "updated_at"])
        client = self._client("base-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("12.00"))
        self.assertEqual(customer.commission_rate, Decimal("20.00"))


# ── ③ 跨租户隔离（本线新增/改动的写路径）──────────────────────────────
class CompletionCrossTenantIsolationTests(TenantTestCase):
    """以租户 A 的凭据去动**只在 B 里存在**的订单，必须失败，且 B 的行**不动**。

    ⚠️ **判据一律用 `order_no`，不用主键**：两个 schema 的主键序列各自从 1 开始，
    `filter(id=a_pk)` 在 B 里命中的是 **B 自己的行**（AGENTS.md §5）。本类的
    `setUp` 用数据库行自证「A 里没有 B 的单号、B 里没有 A 的单号」——否定断言的前提
    因此是**可指认**的，不是靠空集凑出来的。

    ⚠️ **跨域名请求不会在响应结束时还原 schema**（`django_tenants` 只在请求**进来时**
    `set_tenant`）⇒ 一切 ORM 读都走 `_in_b()`，它用 `try/finally` 把连接钉回租户 A。
    """

    DOMAIN_A = "tenant.test.com"
    DOMAIN_B = "p4fu-complete-b.test.local"
    SCHEMA_B = "p4fu_complete_b"

    NO_A = "20260922000000000901"
    NO_B = "20260922000000000902"
    SALES_A = "p4fu-xt-sales-a"
    SALES_B = "p4fu-xt-sales-b"

    _GROUP_SEQ = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="P4FU-A 跨租户隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        from apps.rbac.seed import seed_catalog

        connection.set_tenant(self.tenant)
        seed_catalog()
        self.game = Game.objects.create(name="跨租户-A 的游戏")
        self.sales_a = self._sales(self.SALES_A)
        self.order_a = self._order(self.NO_A, self.sales_a)
        self.client_a = self._client(self.SALES_A + "-login", COMPLETED_NODE)

        try:
            with tenant_context(self.tenant_b):
                seed_catalog()
                self.game_b = Game.objects.create(name="跨租户-B 的游戏")
                self.sales_b = self._sales(self.SALES_B)
                self.order_b = self._order(self.NO_B, self.sales_b)
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证：否定断言的靶子在 A 里确实不存在
        self.assertTrue(Order.objects.filter(order_no=self.NO_A).exists())
        self.assertFalse(
            Order.objects.filter(order_no=self.NO_B).exists(),
            "B 的单号在 A 的 schema 里也存在——跨租户前提坏了，下面的否定断言无意义",
        )
        self.assertTrue(self._in_b(lambda: Order.objects.filter(order_no=self.NO_B).exists()))
        self.assertFalse(self._in_b(lambda: Order.objects.filter(order_no=self.NO_A).exists()))

    def _sales(self, username):
        user = User.objects.create_user(username=username)
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("20.00"),
        )
        return user

    def _order(self, no, sales):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下提成基数只看总价
            sales_user=sales,
        )
        create_performance_for_order(order)
        return order

    def _client(self, username, *codes):
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            domain=self.DOMAIN_A, username=username, groups=("客服", node_group)
        )
        from apps.rbac.models import PermissionNode, Role

        user = User.objects.get(username=username)
        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # A 的 client 要能完成**自己**租户的单 ⇒ 必须**显式**授 A 的游戏；
        # 否则正向对照自己就 1002，下面「A 动不了 B 的单」那条否定断言失去前提。
        # ⛔ 只授 A 的 `self.game`，**不**碰 B 侧任何账号。
        grant_games(user, self.game)
        return client

    def _in_b(self, function):
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_positive_control_a_completes_its_own_order(self):
        """正向对照：A 的 client 完成**自己的**单 ⇒ `code 0`（否则下面的否定断言
        可能由「这个 client 压根动不了任何单」凑出来）。"""
        response = self.client_a.post(
            f"/api/business/orders/{self.NO_A}/transition/",
            {"target": OrderStatus.COMPLETED},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.json())
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.status, OrderStatus.COMPLETED)

    def test_a_credentials_cannot_complete_the_order_that_lives_only_in_b(self):
        """A 的凭据对**只在 B 里存在**的单号发「完成」⇒ 200 + `code` 1002；
        B 的订单状态、B 的客服行金额**都没动**；A 里零留痕。

        失败形状是 **HTTP 200 + 1002**（资源不存在在出信封之前被归一化成业务结果，
        见 `apps/core/exceptions.py`），**不是** 404、也不是 403（403 是"没有节点判据"，
        而本 client 有节点 17）。
        """
        response = self.client_a.post(
            f"/api/business/orders/{self.NO_B}/transition/",
            {"target": OrderStatus.COMPLETED},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIsNone(body["data"])
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.NO_B).exists()
        )

        # B 侧：订单与客服行都**没被碰过**（读 B 必须经 tenant_context）
        def _read_b():
            order = Order.objects.get(order_no=self.NO_B)
            customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
            return order.status, customer.bonus_amount, customer.commission_rate

        status, bonus, rate = self._in_b(_read_b)
        self.assertEqual(status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(bonus, Decimal("0.00"))
        self.assertIsNone(rate)
