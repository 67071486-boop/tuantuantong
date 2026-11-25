"""Task 1「完成即生效」（业务方 2026-09-23 裁定）：订单进入 `COMPLETED` 时，
把打手生效绩效按预计绩效生成。

业务方原话（`docs/plans/2026-09-23-performance-rewards-fix.md` §0 Q1）：
「订单被直接改成『已完成』、打手从没点过『交单』时 —— **完成即生效**」。

⭐ **R5 / R6 改准（编排方裁定）**：原来的 `review_status is not None or
bonus_amount != 0.00 ⇒ 不动` 守卫**整条移除** ⇒ 进 `COMPLETED` 时**一律**
`bonus_amount := temp_amount`、`review_status := 已通过`（仍受「已结账 / 无接手人」约束）。
理由：只删 `review_status` 半边时，**存量**（旧 `/submit/` 写过非 0 `bonus` 的行）仍被
`bonus != 0.00` 那一半挡住 ⇒「存量交过单的单，完成后永久没钱」。

规则（改准后，逐条）：

| MAIN 行的形态 | 完成时的行为 |
|---|---|
| 没有 MAIN 行 | 不动（客服提成的减项按 `0.00` 算） |
| `user_id is None`（没有接单人） | 不动（`bonus` 保持 `0.00`） |
| 行已结账（`Performance.is_settled`） | 不动（新增守卫；防存量「带着结账痕迹」的脏行被重写） |
| 其余（有接单人 + 未结账） | `bonus = temp_amount`、`review_status = 已通过`（**不按** `review_status` 分支） |

⭐ **顺序本身就是修复点**：补打手必须发生在**算客服提成之前**（客服提成的基数是
`profit_of(order, 打手.bonus_amount)`——不补就打手按 0 算、客服**多拿**），
也必须发生在 `on_order_completed` 的两个早退（无客服行 / `sales_user_id is None`）
**之前**——原来这两个早退会跳过打手，漏掉的正是本任务要补的那笔钱。

⚠️ **期望值一律写十进制字面量**（E-0024）：金额写 `Decimal("123.00")` 这种字面值，
不引生产常量当期望值（自指断言在常量改坏时不红）。
⚠️ 本文件**不**放宽任何断言（brief §3.3 明令）：全部是 `assertEqual` + 具体值。
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
from apps.business.performance import (
    create_performance_for_order,
    main_performance_of,
)
from apps.business.settlement_hooks import on_order_completed
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 「已完成」的授权节点（对标 Menu_ID 17）。
COMPLETED_NODE = "order.change_status_completed"
#: 「非已完成」的授权节点（对标 Menu_ID 16）；重开（`COMPLETED → PENDING_REVIEW`）要它。
PLAIN_NODE = "order.change_status"


class CompletionActivationFixtureMixin:
    """建单 / 建绩效 / 建登录 client 的小夹具（各用例只表达**自己的差异**）。

    ⚠️ **每个 client 用独立的节点组**（`节点{n}`）：节点挂在 `Role` 上，而 `Role.name`
    唯一、与 `Group` 一对一 ⇒ 同组名会**共享**节点（`test_p4fu_order_complete.py` 的
    `CompletionFixtureMixin` 已记过这条实测）。
    """

    _GROUP_SEQ = 0

    def setUp(self):
        from apps.rbac.seed import seed_catalog

        seed_catalog()
        self.game = Game.objects.create(name="原神")

    def _client(self, username, *codes, role_group="客服"):
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            domain="tenant.test.com", username=username, groups=(role_group, node_group)
        )
        from apps.rbac.models import PermissionNode, Role

        user = User.objects.get(username=username)
        # 写路径和读路径都按游戏收窄。不显式持有本单的游戏，过渡会回 1002，
        # 测不到「完成即生效」。
        grant_games(user, self.game)
        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        return client

    def _sales(self, username, *, value="20.00"):
        """客服：利润百分比 20%（`(净实收 − 打手生效绩效) × 20%`）。"""
        user = User.objects.create_user(username=username)
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal(value),
        )
        return user

    def _order(self, no, *, status=OrderStatus.PENDING_REVIEW, sales=None, **overrides):
        # ⚠️ spec §2.3：入 `COMPLETED` 的边只剩
        #    `PENDING_REVIEW → COMPLETED` ⇒ 本夹具的默认来源态从「代练中」换成「待验收」。
        #    下面所有金额与状态断言一个字没动（判据是「完成那一刻打手行生效」，与来源态无关）。
        # 毛收款只读 total_price。received_price 固定成陷阱值 1.00：
        # 实现退回旧口径时，客服提成基数会变成 1.00，字面量期望当场红。
        fields = {"hall_price": Decimal("88.00"), "received_price": Decimal("1.00")}
        fields.update(overrides)
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            sales_user=sales,
            **fields,
        )

    def _complete(self, client, order):
        return self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
        )

    @staticmethod
    def _post(client, url, payload):
        return client.post(url, payload, content_type="application/json")


# ── ① 规则表逐条：补 / 不补 ────────────────────────────────────────────
class TakerActivationRuleTests(CompletionActivationFixtureMixin, TenantTestCase):
    """「有接单人 + 未结账」⇒ 完成时按 `temp_amount` 生成；「无 MAIN 行 / 无接单人 / 已结账」
    这三种形态**一个字不改**（R5 之后不再看 `review_status`）。"""

    def test_never_submitted_taker_becomes_effective_on_completion(self):
        """⭐ 核心判据：`temp_amount=123.00` / `bonus_amount=0.00` / `review_status=NULL`
        的打手行，订单完成 ⇒ `bonus_amount == Decimal("123.00")`
        **且** `review_status == ReviewStatus.APPROVED`。

        ⭐ 为什么 `review_status` 也要写：`/adjust` 判「交过单」的依据是
        `review_status is not None`——只补 `bonus` 会让这一行出现「`temp` 会被重算、
        `bonus` 不动」的两套金额并存（brief §3.1 的 ⭐ 段）。
        """
        sales = self._sales("act-sales")
        order = self._order("20260923000000000101", sales=sales)
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="act-taker")
        main.temp_amount = Decimal("123.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("act-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)      # 阳性对照：迁移真的发生了
        self.assertEqual(main.bonus_amount, Decimal("123.00"))
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)

    def test_customer_commission_uses_the_activated_taker_amount(self):
        """⭐ 顺序判据：客服提成的减项是**补齐之后**的打手生效绩效。

        实数：净实收 `108.00`、打手 `temp_amount = 88.00`（补齐后 `bonus = 88.00`）、
        客服利润提成率 `20%` ⇒ 客服 `bonus_amount = (108.00 − 88.00) × 20% = ` **`4.00`**。

        ⚠️ **判别力**：不补打手时减项是 `0.00` ⇒ 客服拿到 `21.60`，本条当场红。
        """
        sales = self._sales("order-sales")
        order = self._order(
            "20260923000000000102", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="order-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("order-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("88.00"))       # 打手先补齐
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("4.00"))    # 客服再按补齐后的减项算
        self.assertEqual(customer.commission_rate, Decimal("20.00"))

    def test_submitted_row_is_regenerated_on_completion(self):
        """⭐ **R5 反转**：已交过单的行（`review_status = 待审核`、`bonus = 60.00`、`temp = 88.00`）
        完成时**照常生成** ⇒ `bonus_amount == 88.00`（= `temp_amount`）、`review_status = 已通过`。

        本条的判别面是「`review_status` 非空**不再**挡生成」：夹具刻意让 `temp != bonus`
        （`88.00` vs `60.00`）⇒ 一个「按 `review_status` 分支、交过单就不动」的实现
        （旧的守卫、或只留半边的半修）在这里当场红。

        客服行的阳性对照随生成值一起改准：减项是打手**生成后**的 `88.00`
        ⇒ `(108.00 − 88.00) × 20% = 4.00`（旧值 `9.60` 对应的是「打手停在 `60.00`」那种半修）。
        """
        sales = self._sales("submitted-sales")
        order = self._order(
            "20260923000000000103", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="submitted-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("60.00")
        main.review_status = ReviewStatus.PENDING
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("submitted-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("88.00"))        # ← 不是 60.00
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)  # ← 不是保持 待审核
        # 阳性对照：钩子确实跑了，且客服的减项用的是**生成后**的 88.00（(108−88)×20%）。
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("4.00"))

    def test_a_stale_effective_amount_is_overwritten_on_completion(self):
        """⭐ **R5「整条移除」的第二道子句**：`user` 有、`review_status is None`（从没交过单）、
        但 `bonus_amount = 60.00`（存量：旧 `/submit/` 写过非 0）⇒ 完成后**照常**按 `temp` 生成。

        ⚠️ **为什么单列一条**：只删 `review_status is not None` 半边、留着
        `bonus_amount != Decimal("0.00")` 那一半时，**存量行**（`review_status` 为空 +
        `bonus` 非零）会被那一半挡住 ⇒「存量交过单的单，完成后永久没钱」。
        本条刻意把 `review_status` 留成 `None`、只让 `bonus` 非零：那种半修在这里当场红。

        ⚠️ 夹具刻意让 `temp != bonus`（`88.00` vs `60.00`），与同组用例同一手法。
        """
        sales = self._sales("nonzero-sales")
        order = self._order(
            "20260923000000000107", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="nonzero-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("60.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("nonzero-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)       # 阳性对照：迁移真的发生了
        self.assertEqual(main.bonus_amount, Decimal("88.00"))       # ← 不是 60.00
        self.assertEqual(main.review_status, ReviewStatus.APPROVED) # ← 被写成「已通过」
        # 阳性对照：钩子确实跑了，且减项用的是**覆盖后**的 88.00（(108−88)×20%）。
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("4.00"))

    def test_no_taker_row_is_not_activated(self):
        """没有接单人（MAIN 行在、`user_id is None`）⇒ 完成后 `bonus_amount == 0.00`
        **且** `review_status` 仍是 `None`；客服提成的减项按 `0.00` 算（`21.60`）。

        ⚠️ 夹具把 `temp_amount` 也写成 `123.00`：一个「只看 `bonus == 0` 就补」的
        实现会把 `123.00` 写给一个**没有接单人**的行——那是凭空的应付款。
        """
        sales = self._sales("notaker-sales")
        order = self._order(
            "20260923000000000104", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        self.assertIsNone(main.user_id)                             # 夹具自证：真的没有接单人
        main.temp_amount = Decimal("123.00")
        main.save(update_fields=["temp_amount", "updated_at"])
        client = self._client("notaker-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertIsNone(main.user_id)
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertIsNone(main.review_status)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("21.60"))   # 108.00 × 20%

    def test_rejected_row_is_regenerated_on_completion(self):
        """⭐ **R6（业务方裁定 2026-09-28）反转**：被驳回过（`review_status = 已驳回`、
        `bonus = 0.00`）的行，后来订单变成已完成 ⇒ **照常生成**生效绩效。

        口径只有一条：「订单是否变成已完成」。⇒ `review_status == 已通过`、`bonus == 88.00`。
        判别力：实现里再按 `review_status` 加任何分支 ⇒ 本条当场红。
        """
        sales = self._sales("rejected-sales")
        order = self._order(
            "20260923000000000105", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="rejected-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = ReviewStatus.REJECTED
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("rejected-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("88.00"))        # ← 不是 0.00
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)  # ← 不是保持 已驳回

    def test_a_submitted_row_with_a_zero_bonus_is_regenerated_on_completion(self):
        """§7.3 #25 的**新数据形态**：`review_status = 待审核`、`temp = 88.00`、
        `bonus = 0.00`（R2 之后 `/submit/` 不写金额 ⇒ 增量数据长这样）⇒ 完成 ⇒ `bonus == 88.00`。

        ⚠️ 与 `test_submitted_row_is_regenerated_on_completion`（存量形态：`bonus = 60.00`）
        合起来才是 R5「整条移除」的完整钉子：只删半边时，本条会绿、那条会红。
        """
        sales = self._sales("newshape-sales")
        order = self._order(
            "20260923000000000108", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="newshape-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = ReviewStatus.PENDING
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("newshape-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("88.00"))
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)

    def test_a_settled_row_is_not_overwritten_by_the_completion_hook(self):
        """⭐ **R5 新增守卫**（spec §4.2.4 ③）：该行**已结账** ⇒ 完成钩子**不做**。

        ⚠️ 这条**直接调钩子**：正常路径上「已结账 ⇒ 不许改状态」（D4b）会先挡住，
        本守卫防的是**存量脏行**——它可能已经在 `PENDING_REVIEW` 上带着结账痕迹。
        直接调用正是为了绕开 D4b 那道闸、把这条守卫单独钉住。

        判别力：删掉 `if taker.is_settled: return taker` ⇒ 钩子会把 `bonus` 覆写成
        `temp_amount = 88.00` ⇒ 本条红。
        """
        order = self._order("20260923000000000109", status=OrderStatus.COMPLETED)
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="settled-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("60.00")
        main.review_status = None
        main.settle_status = SettleStatus.SETTLED
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "settle_status",
                "updated_at",
            ]
        )

        on_order_completed(order, OrderStatus.PENDING_REVIEW)

        main.refresh_from_db()
        self.assertTrue(main.is_settled)                             # 前置哨兵
        self.assertEqual(main.bonus_amount, Decimal("60.00"))        # ← 不是 88.00
        self.assertIsNone(main.review_status)

    def test_moving_to_pending_review_writes_no_amount(self):
        """§7.3 #23：非完成态 + `/transition/` 到「待验收」⇒ 生效绩效**一个字节不变**。

        `PENDING_REVIEW` 上挂的只有通知钩子（`notifications._on_pending_review`）——
        一个「顺手也把钱写上」的实现（例如把它挂成金额写入点）在这里当场红。
        """
        order = self._order("20260923000000000110", status=OrderStatus.IN_PROGRESS)
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="pending-taker")
        main.temp_amount = Decimal("88.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("pending-cs", PLAIN_NODE)

        body = self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.PENDING_REVIEW},
        ).json()

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)   # 阳性对照：迁移真的发生了
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertEqual(main.temp_amount, Decimal("88.00"))
        self.assertIsNone(main.review_status)

    def test_re_entering_completed_overwrites_the_bonus_without_duplicating_rows(self):
        """完成 → 重开（`COMPLETED → PENDING_REVIEW`）→ 再完成 ⇒ 金额**按当时的 `temp` 覆盖**、
        不重复建行、不报错。

        每次进入 COMPLETED 都按当时的 temp 重写 bonus。
        重开时把 temp 改成 999.00，第二次完成 bonus 必须是 999.00。
        不重复建行、也不报错。
        """
        sales = self._sales("idem-sales")
        order = self._order(
            "20260923000000000106", sales=sales, total_price=Decimal("300.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="idem-taker")
        main.temp_amount = Decimal("123.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("idem-cs", COMPLETED_NODE, PLAIN_NODE)

        first = self._complete(client, order).json()
        self.assertEqual(first["code"], 0, first)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("123.00"))

        # 重开：`COMPLETED → PENDING_REVIEW`（边存在，节点 16）。
        # ⚠️ spec §2.3：重开只去「待验收」而不是 `ISSUE`，而
        #    入 `COMPLETED` 的边只剩「待验收 → 已完成」⇒ 再完成会回 1002。重开目标改成
        #    「待验收」之后，本用例要测的**幂等性**（第二次完成不把金额改成 999.00、
        #    不重复建行）一字没改。
        reopen = self._post(
            client,
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.PENDING_REVIEW},
        ).json()
        self.assertEqual(reopen["code"], 0, reopen)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)  # 阳性对照：重开真的发生了
        main.temp_amount = Decimal("999.00")
        main.save(update_fields=["temp_amount", "updated_at"])

        second = self._complete(client, order).json()

        self.assertEqual(second["code"], 0, second)
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(main.bonus_amount, Decimal("999.00"))      # ← 覆盖生成，不是 123.00
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)
        rows = Performance.objects.filter(order=order, emp_type=EmpType.SALES)
        self.assertEqual(rows.count(), 1)                           # 不重复建行


# ── ② 顺序：补打手必须早于那两个早退 ──────────────────────────────────
class TakerActivationOrderingTests(CompletionActivationFixtureMixin, TenantTestCase):
    """`on_order_completed` 的两个早退（无客服行 / `sales_user_id is None`）**在补打手之后**
    ——这正是本任务的修复点：原来它们会**跳过打手**，`bonus` 永远停在 `0.00`。

    ⚠️ 这两条是本文件里**唯一**能钉住「补打手放在哪一行」的判据：规则表那六条用例
    都带客服行、`sales_user` 也在，一个把补齐写在两个早退**之后**的实现会全绿。
    """

    def test_activation_happens_even_without_a_customer_row(self):
        """`sales_user` 为空 ⇒ 没有客服行（第一个早退）⇒ 打手**照样**被补齐。"""
        order = self._order("20260923000000000201")
        create_performance_for_order(order)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )                                                           # 夹具自证
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="norow-taker")
        main.temp_amount = Decimal("123.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        client = self._client("norow-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("123.00"))
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)

    def test_activation_happens_after_the_sales_user_is_cleared(self):
        """归属人事后被清空（第二个早退：不重算、不指给别人）⇒ 打手**照样**被补齐，
        而客服行**一个字节不改**（仍是 `0.00` / 率 `NULL`）。
        """
        sales = self._sales("cleared-sales")
        order = self._order(
            "20260923000000000202", sales=sales, total_price=Decimal("108.00")
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="cleared-taker")
        main.temp_amount = Decimal("123.00")
        main.bonus_amount = Decimal("0.00")
        main.review_status = None
        main.save(
            update_fields=[
                "user",
                "temp_amount",
                "bonus_amount",
                "review_status",
                "updated_at",
            ]
        )
        # 归属人事后被清空：客服行**仍在**（第一个早退不会命中）。
        order.sales_user = None
        order.save(update_fields=["sales_user", "updated_at"])
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertIsNotNone(customer)                              # 夹具自证：客服行还在
        client = self._client("cleared-cs", COMPLETED_NODE)

        body = self._complete(client, order).json()

        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("123.00"))
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))    # 不重算
        self.assertIsNone(customer.commission_rate)
