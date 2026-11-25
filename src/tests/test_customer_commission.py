"""客服销售提成的**生成**（录单建行）与**确定**（订单 → `COMPLETED`）。

**两段式**（spec §4.2，业务方原话「产生打手预绩效的时候便可以生成销售客服的预绩效；
订单状态完成后打手绩效为真实绩效后客服绩效即可确定」）：
① **录单**：`create_performance_for_order` 同时建**客服行**（`emp_type = 销售提成`、
`user = order.sales_user`、`temp_amount = 0.00`、`commission_rate = NULL`、
`bonus_amount = 0.00`、`review_status = NULL`）；**录单不读提成率**；`sales_user` 为空 ⇒ **不建行**；
`Order.sales_user` 由录单视图按 `request.user` 写入。
② **订单 → `COMPLETED`**：`settlement_hooks.on_order_completed` 写
`bonus_amount = 利润 × 提成率`、`commission_rate = 提成率`、`review_status = 已通过`，
与状态迁移**同一事务**。

**「录单那一刻 `temp_amount` 就是 0」是无条件的**（Ruling P）：读率会把**订单创建**耦合到
**薪酬配置**上（默认档案 `ORDER_PERCENT`(20)、客服路径要 `PROFIT_PERCENT`(30) ⇒ 录单失败），
且不做中途重算 ⇒ 算出来的数立刻陈旧，**0 是诚实的**；业务方要的「录单时就有客服预绩效」
在**行的存在性**上已满足，真正的钱在 `COMPLETED` 的 `bonus_amount`。

⚠️ 期望值一律写字面十进制值（E-0024）；`COMPLETED` 有**两条端点路径**（D-0071）：
`/review` 与通用 `/transition/` **都要断**（漏一条路径的副作用是最难发现的一类 bug）；
`mock.patch` 打在**被查找的模块**上（E-0029）：钩子经 `settlement_hooks._persist_customer_commission`
这个**模块全局名**查找写入函数。
⭐ **本文件刻意不 import `settlement_hooks`**（Ruling O #1 的判别力）：它**模块导入时**注册钩子，
测试自己 import 会掩盖「`apps.py::ready()` 漏注册」的变异；只用 `sys.modules` **查**
（`_settlement_hooks()`）不触发 import ⇒ `ready()` 没导入 ⇒ 判据与端到端用例**当场红**。
"""
import sys
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission, User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.api.serializers import OrderSerializer
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from apps.business.order_status import (
    _TARGET_HOOKS,
    IllegalTransition,
    apply_transition,
    clear_target_hooks,
    register_target_hook,
)
from apps.business.performance import (
    create_performance_for_order,
    main_performance_of,
)
from apps.core.error_codes import ErrorCode as EC
from tests.order_rules_helpers import release_taker
from tests.p5_auth_helpers import grant_games, make_logged_in_client

REVIEW_PERM = "review_performance"
#: 测试用的销售提成率（利润百分比）：20% ⇒ 利润 48.00 的提成是 9.60。
SALES_RATE = Decimal("20.00")


def _settlement_hooks():
    """取**已被 `apps.py::ready()` 导入**的 `settlement_hooks` 模块。

    ⚠️ **不能用 `import`**：那会自己触发那次导入、把「ready() 漏了 import」这个缺陷掩盖掉
    （见模块 docstring）。`ready()` 没导入时这里直接失败 —— 那正是我们要的**响亮**。
    """
    module = sys.modules.get("apps.business.settlement_hooks")
    if module is None:
        raise AssertionError(
            "apps.business.settlement_hooks 不在 sys.modules 里 —— "
            "apps.py::ready() 没有 import 它 = **钩子静默不注册**（Ruling O #1）"
        )
    return module


def _sales_user(username="cs-seller", *, rate=SALES_RATE, mode=CommissionMode.PROFIT_PERCENT):
    """建一个带提成档案的销售客服。"""
    user = User.objects.create_user(username=username)
    StaffProfile.objects.create(
        user=user, commission_mode=mode, commission_value=rate
    )
    return user


def _reviewer_client(username="reviewer-1", *, game):
    """真实登录的客服主管——本夹具声明它要过的门（判据是节点门，`groups=` 只是挂点）。

    - `/review`：类级节点门 `order.change_status_completed`
      （`PERFORMANCE_PERMISSIONS["review"]`；函数体的**职位**判据已收口删除，
      `groups=("客服主管",)` 只当 `nodes=` 的挂点）；
    - `/transition/`：类级节点门**按 target 挑**——`target=COMPLETED` 时是
      `order.change_status_completed`（17），其余目标是 `order.change_status`（16）
      （`OrderViewSet.get_permissions()`，D-0071）。这条路径真的会成功 ⇒ 夹具必须同时声明 16 与 17。
    `groups`/`nodes` 缺一不可（`nodes=` 只挂到用户已有的 Group 的 Role 上）。
    **这是「声明夹具需要的门」，不是放宽门。**

    （按游戏权限收窄）：**夹具补齐**，不是放宽判据。有一条路径要经 `get_object()` 取单
    （通用 `/transition/`，`game_scope` 收窄的 queryset）⇒ 必须显式持有 `game`；
    `/review` 自己不过 `game_scope`，但同一账号两处都用，授一次即可。期望值一个字没改。
    """
    client = make_logged_in_client(
        username=username,
        groups=("客服主管",),
        nodes=("order.change_status", "order.change_status_completed"),
        # ⚠️ 显式 opt-out（默认值翻面）：游戏由下面那句
        # `grant_games(user, game)` 逐款补；本夹具下面自己建档案 ⇒ 夹具必须**不建**。
        games=None,
    )
    user = User.objects.get(username=username)
    user.user_permissions.add(Permission.objects.get(codename=REVIEW_PERM))
    grant_games(user, game)
    return client, user


def _logged_in_sales(username, *, game, mode=None, rate=SALES_RATE, admin=False):
    """建一个**已登录**的客服并（可选）配提成档案，返回 `(client, user)`。

    Ruling Q 之后录单的归属人**就是** `request.user`，所以本夹具的 `user` 既是登录人、
    也是 `order.sales_user`（`_sales_user` 那份夹具用于**直接构造 Order** 的用例）。
    `mode=None` ⇒ 不建**提成配置**档案（「无档案」用例）。

    ⚠️ **`admin=True`：让账号持「系统管理员」职位、且⛔ 不建 `StaffProfile`。**
    游戏权限存在 `StaffProfile.games` 上 ⇒ **「有游戏权限」蕴含「有档案」**，「没有档案」的
    账号在读写两侧都被空集拦住（读侧 `scope_queryset`、写侧 `assert_game_allowed`），
    唯一例外是租户管理员（`is_tenant_admin` 短路，判据是**职位**不是档案）——即
    `apps/platform/provisioning.py::seed_tenant` 供给的形状（按构造没有 `StaffProfile`）。
    ⛔ 「无档案」用例**不能**用 `grant_games` 补——`get_or_create` 会补出档案，
    把用例要钉的前提（没有档案）悄悄换掉；只能挂职位。

    **本夹具声明它要过的门**：客服要走 `POST /api/business/orders/`（录单，`order.add_edit`）与
    `PATCH /api/business/orders/{no}/`（改归属用例，`order.add_edit` 或 `order.submit_edit`
    任一），两条 code 都声明。组名用**非内置职位**的「录入组」：判据只看节点，这个组名不会
    顺带改变 `allowed_transitions` 之类的行为；⚠️ `Role.name` 是 `max_length=5` ⇒ 组名 ≤5 字。
    **这是「声明夹具需要的门」，不是放宽门。**

    （按游戏权限收窄）：**夹具补齐**，不是放宽判据。客服是**发请求的一方**（写侧过
    `assert_game_allowed`）⇒ 必须显式持有 `game`；缺 `StaffProfile` ⇒ `games_of` 空集 ⇒ 1000。
    ⚠️ `mode=None`（「无档案」那条）走的是**职位**形状（见上），不经 `grant_games`。
    """
    client = make_logged_in_client(
        username=username,
        groups=("录入组", "系统管理员") if admin else ("录入组",),
        nodes=("order.add_edit", "order.submit_edit"),
        # ⚠️ 显式 opt-out（默认值翻面）：本夹具自己建提成档案，游戏也自己
        # 声明（`grant_games` 或 `admin=True` 刻意不补）⇒ 夹具必须**不建**档案。
        games=None,
    )
    user = User.objects.get(username=username)
    if mode is not None:
        StaffProfile.objects.create(
            user=user, commission_mode=mode, commission_value=rate
        )
    # `admin=True` 时**刻意不补游戏权限**：那会 `get_or_create` 出一份档案，
    # 把「没有档案」这个前提换掉（详见本夹具 docstring）。
    if not admin:
        grant_games(user, game)
    return client, user


# ── ① 录单建客服行 ──────────────────────────────────────────────────
class IntakeCustomerPerformanceTests(TenantTestCase):
    """spec §4.2 的**第一段**：录单那一刻就有客服行。本类钉两条（Ruling P / Q）：
    ① 录单**完全不读提成率** ⇒ 档案是哪种 mode（含默认的 20）都不影响录单成功；
    ② 录单视图按 `request.user` 写 `Order.sales_user` ⇒ 客服行归属人 = **登录人**，
       请求体里的 `sales_user` 不作数。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.base = "/api/business/orders"

    def _create(self, client, **overrides):
        payload = {"game": self.game.id, "project_text": "满星", "hall_price": "88.00"}
        payload.update(overrides)
        return client.post(self.base + "/", payload, content_type="application/json")

    def test_intake_succeeds_when_the_sales_profile_uses_order_percent_mode(self):
        """⭐ **Ruling P 的判据**：档案是 **mode 20（订单价百分比 = `StaffProfile` 的默认值）**
        时录单**依然成功**：订单落库、客服行被建、`temp_amount == 0.00`、`commission_rate is None`。

        能红的方向：录单若读率（读 `PROFIT_PERCENT`(30)）⇒ mode 20 不匹配 ⇒ `CommissionError`
        ⇒ **录单整体失败、订单不落库**（本用例当场红）。
        """
        client, sales = _logged_in_sales(
            "cs-order-percent", game=self.game, mode=CommissionMode.ORDER_PERCENT
        )
        body = self._create(client).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.user_id, sales.id)
        self.assertEqual(customer.temp_amount, Decimal("0.00"))    # ← 无条件 0
        self.assertIsNone(customer.commission_rate)                # 快照在 COMPLETED 才写
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.review_status)

    def test_intake_writes_the_request_user_as_the_order_sales_user(self):
        """⭐ **Ruling Q 的判据**：走**真实 HTTP 录单**后 `order.sales_user == request.user`，
        且客服行**确实被建**——这是整条客服提成路径端到端串起来的证据。

        （没有 `sales_user` 写入点 ⇒ 客服行永不建、`on_order_completed` 早退 = 整条路径死代码。）

        本用例不带 `sales_user`，断的是默认口径（归属 = 认证态）。
        持 `order.allow_change_sales_agent` 才能显式指定；本夹具没有那个节点，带上那个键会 403。
        显式指定的判据在 `tests/test_order_sales_agent.py`。
        """
        client, sales = _logged_in_sales("cs-ruling-q", game=self.game)   # 不配提成率也算
        body = self._create(client).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_user_id, sales.id)
        self.assertEqual(body["data"]["sales_user"], sales.id)     # 响应里也是登录人
        self.assertTrue(
            Performance.objects.filter(
                order=order, emp_type=EmpType.SALES, user_id=sales.id
            ).exists()
        )

    def test_intake_succeeds_when_the_sales_profile_uses_profit_percent_mode(self):
        """档案是 **mode 30（利润的百分比）**时同样成功，且**依然不读率**（`temp_amount = 0`）。"""
        client, sales = _logged_in_sales(
            "cs-profit-percent", game=self.game, mode=CommissionMode.PROFIT_PERCENT
        )
        body = self._create(client).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.temp_amount, Decimal("0.00"))
        self.assertIsNone(customer.commission_rate)

    def test_intake_succeeds_when_the_sales_user_has_no_profile(self):
        """**无档案**（`commission_percent_of` 本会回落 100%）时同样成功——
        因为这里**根本不读率**，回落分支进不来。

        夹具形状（**夹具补齐**，不是放宽判据）：这个「无档案」的客服是**发请求的一方**
        （写侧过 `assert_game_allowed`），而 `grant_games` 会 `get_or_create` 出档案、
        把本用例要钉的前提（「档案不存在」）悄悄换掉 ⇒ 走**唯一**能保住该前提的形状：
        挂「系统管理员」**职位**（`is_tenant_admin` 在 `scope_queryset` / `assert_game_allowed`
        短路），⛔ **不建档案**——与
        `tests/test_submit_api.py::test_falls_back_to_full_when_the_taker_has_no_profile`
        同型，也正是 `apps/platform/provisioning.py::seed_tenant` 供给的租户管理员形状。
        断的仍是「录单**不读率**」；下面的构造物哨兵钉住「档案确实不存在」。
        """
        client, sales = _logged_in_sales("cs-no-profile", game=self.game, mode=None, admin=True)
        # 构造物：挂的是**职位**、不是档案 —— 本用例的前提（没有档案）确实成立。
        # 没有这一条的话，「补了游戏权限」的改法会让本用例在**另一条分支**上碰巧变绿。
        self.assertFalse(StaffProfile.objects.filter(user=sales).exists())
        body = self._create(client).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.temp_amount, Decimal("0.00"))
        self.assertIsNone(customer.commission_rate)

    def test_intake_without_a_sales_user_builds_no_customer_row(self):
        """`sales_user` 为空 ⇒ **不建**客服行；这类单由 P4-8 的「待归属」接口兜底。

        ⚠️ **修复轮 1 之后本判据只能走函数级**：录单端点现在**总是**按 `request.user`
        写归属人（Ruling Q），经 HTTP 建不出「没有归属人」的单。这里直接构造
        `sales_user=None` 的订单再调**生产同一入口**，保住 spec §4.2 的不变量。
        """
        order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000601",
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("88.00"),
        )
        create_performance_for_order(order)
        # 非空哨兵：打手行**必须**在，否则下面的否定断言可能只是「建行整个坏了」。
        self.assertTrue(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )


# ── ①b `sales_user` 一律只读─────────────────────────────
class SalesUserIsReadOnlyTests(TenantTestCase):
    """`Order.sales_user` **只能由视图按认证态写入**，两个序列化器都收口。

    **为什么这不是「顺手加个 `read_only`」**：`sales_user` 决定**客服提成的唯一归属**
    （spec §3.8），而客服行建行那一刻就把 `user = order.sales_user` 写死、P4 **不迁移 / 不重算**；
    `bonus_amount` 在 `COMPLETED` 已确定、可能**已结账**。通用 PATCH 改归属人会让
    **客服行与订单归属不一致**：提成照旧付给**旧**归属人，无报错、无测试红——正是「静默算错钱」
    （E-0027 同族）。改归属是**受治理的动作**（要同时搬客服行、尊重已结账的钱），
    不是通用 PATCH 的职责。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.base = "/api/business/orders"

    def _create(self, client, **overrides):
        payload = {"game": self.game.id, "project_text": "满星", "hall_price": "88.00"}
        payload.update(overrides)
        return client.post(self.base + "/", payload, content_type="application/json")

    def test_patch_cannot_reassign_the_sales_user(self):
        """⭐ **PATCH 改归属无效**：录单归属 A → 用户 B 用通用 PATCH 传 `sales_user=B.id`
        → `order.sales_user` **仍是 A**，且**客服行的 `user` 也仍是 A**（两侧一致才是这条的意义）。

        能红的方向：`read_only_fields` 漏了 `sales_user` ⇒ PATCH 真的把归属改成 B、
        客服行留在 A（提成错人）⇒ 本用例当场红。

        ⚠️ **哨兵**：同一次 PATCH 还改 `tag`，并断言它**确实**落了库 ——
        否则「`sales_user` 没变」也可能只是 PATCH 被拒/没生效（假通过）。
        """
        owner_client, owner = _logged_in_sales("cs-owner", game=self.game)
        body = self._create(owner_client).json()
        self.assertEqual(body["code"], 0)
        order_no = body["data"]["order_no"]
        customer = Performance.objects.get(
            order__order_no=order_no, emp_type=EmpType.SALES
        )
        self.assertEqual(customer.user_id, owner.id)      # 建行时 = A

        intruder_client, intruder = _logged_in_sales("cs-intruder", game=self.game)
        response = intruder_client.patch(
            f"{self.base}/{order_no}/",
            {"sales_user": intruder.id, "tag": "被改过的备注"},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)

        order = Order.objects.get(order_no=order_no)
        self.assertEqual(order.tag, "被改过的备注")         # 哨兵：PATCH 真的生效了
        self.assertEqual(order.sales_user_id, owner.id)    # ← 修复前是 intruder.id
        self.assertNotEqual(order.sales_user_id, intruder.id)
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, owner.id)       # 客服行与订单**两侧一致**

    def test_intake_serializer_does_not_accept_the_sales_user_in_the_body(self):
        """⭐ **录单请求体里的 `sales_user` 不被接受**：POST 故意传别人的 id
        → 落库的是 `request.user`，**不是**请求体那个。

        ⚠️ **判别力说明**（重要，写在这里免得下一个人误判「这条测不出来」）：
        使本用例对「录单 `OrderSerializer` 的 `read_only_fields` 变异」有判别力的，
        是**序列化器层那句 `assertNotIn("sales_user", validated_data)`**。
        只看 HTTP 结果的半条**测不出来**：视图调的是 `serializer.save(sales_user=request.user)`，
        而 DRF 的 `save(**kwargs)` 把 kwargs 并进 `validated_data`
        （`rest_framework/serializers.py:202` 的 `{**self.validated_data, **kwargs}`，**kwargs 覆盖**）
        ⇒ 无论 `sales_user` 是不是只读，落库的都是 `request.user`。HTTP 那半条因此只钉
        **端到端事实**，钉不住「序列化器根本不接受它」。
        """
        decoy = _sales_user("cs-decoy-readonly")
        serializer = OrderSerializer(
            data={
                "game": self.game.id,
                "project_text": "满星",
                "hall_price": "88.00",
                "sales_user": decoy.id,
            }
        )
        serializer.is_valid(raise_exception=True)
        self.assertNotIn("sales_user", serializer.validated_data)   # ← 判别力在这句

        # ⚠️ HTTP 这半条**不传** `sales_user`
        #     ——本夹具的登录人**不持** `order.allow_change_sales_agent`，而那个键今天
        #     **带节点才收**（不持却带 ⇒ HTTP 403，理由是"静默忽略会让界面与提成归属分叉"）。
        #     下面是**默认口径**那一半：不传那个键 ⇒ 归属 = 认证态。
        #     ⚠️ `OrderSerializer.validated_data` 那一句**照旧**成立且是承重的：
        #     视图读的是 `CreateOrderAssignSerializer`，而**订单列** `sales_user`
        #     在两个模型序列化器里都仍然只读（判据见上面那句 `assertNotIn`）。
        #     显式指定那三档（持节点传 / 不持节点传 / 目标不在候选）见
        #     `tests/test_order_sales_agent.py::IntakeSalesUserTests`。
        client, owner = _logged_in_sales("cs-readonly-intake", game=self.game)
        body = self._create(client).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_user_id, owner.id)
        self.assertNotEqual(order.sales_user_id, decoy.id)

    def test_read_only_does_not_block_the_views_save_kwarg(self):
        """**机制探针**：`read_only_fields` 里加了 `sales_user` 之后，
        `serializer.save(sales_user=<认证态>)` **照样写得进去**（否则整条路径会静默变 NULL）。

        依据是 DRF 源码 `rest_framework/serializers.py:202` 的
        `validated_data = {**self.validated_data, **kwargs}`——只读字段**不进**
        `validated_data`，但 `save(**kwargs)` 是**另外**合进来的，二者互不干扰。
        本条把这句话落成可重跑的判据，而不是只信注释。
        """
        owner = User.objects.create_user(username="cs-kwarg")
        serializer = OrderSerializer(
            data={
                "game": self.game.id,
                "project_text": "满星",
                "hall_price": "88.00",
            }
        )
        serializer.is_valid(raise_exception=True)
        order = serializer.save(
            order_no="20260920000000000701", sales_user=owner
        )
        self.assertEqual(order.sales_user_id, owner.id)


# ── ② 订单 → COMPLETED：确定客服提成 ────────────────────────────────
class CustomerCommissionDeterminationTests(TenantTestCase):
    """spec §4.2 的**第二段** + §9 #12 的路径覆盖（②③④）。

    金额实数（都是字面值）：`total_price = 108.00`（D-0142 起提成基数只看订单总价）、
    `received_price = 1.00`（**陷阱值**）、`hall_price = 88.00`、
    打手 `temp_amount = bonus_amount = 60.00` ⇒ 利润 = 108.00 − 60.00 = **48.00**；
    提成率 **20.00**（利润百分比）⇒ `bonus_amount = 48.00 × 20 / 100 = **9.60**`。
    ⭐ **R5**：完成时打手 `bonus := temp`（覆盖生成）⇒ 夹具两列同值 `60.00`，
    这样「减项 = 打手生效绩效 60.00」在生成之后仍成立。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.sales = _sales_user()
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000001",
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下不参与提成基数
            sales_user=self.sales,
        )
        # 用**生产同一入口**建两行（打手 + 客服），保证夹具与线上一致。
        create_performance_for_order(self.order)
        self.main = main_performance_of(self.order)
        self.main.user = User.objects.create_user(username="taker-1")
        self.main.temp_amount = Decimal("60.00")
        self.main.bonus_amount = Decimal("60.00")
        self.main.review_status = ReviewStatus.PENDING
        self.main.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "review_status", "updated_at"
            ]
        )
        self.customer = Performance.objects.get(order=self.order, emp_type=EmpType.SALES)
        self.client, self.reviewer = _reviewer_client(game=self.game)
        self.url = f"/api/business/performances/{self.main.id}/review/"

    def _approve(self):
        return self.client.post(
            self.url, {"approved": True}, content_type="application/json"
        )

    def test_review_approval_determines_the_customer_commission(self):
        """⭐ §9 #12②：经 **`/review`** 端点过审 → 客服行**确实被确定**（断具体十进制值）。

        ⚠️ 走的是 `/review` 而**不是**通用端点——后者对 `target=COMPLETED` 已被守卫挡住。
        """
        body = self._approve().json()
        self.assertEqual(body["code"], 0)
        # 阳性对照：订单与打手行确实推进了，否则「客服行被写」可能来自别的路径。
        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.main.review_status, ReviewStatus.APPROVED)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(self.customer.commission_rate, Decimal("20.00"))
        self.assertEqual(self.customer.review_status, ReviewStatus.APPROVED)

    def test_direct_apply_transition_determines_the_customer_commission(self):
        """⭐ §9 #12③：**直接调 `apply_transition`** → 同上。

        spec §17.4 #2：只靠端点断言证明不了「副作用挂在 `apply_transition` 上」。
        """
        self.main.review_status = ReviewStatus.APPROVED
        self.main.save(update_fields=["review_status", "updated_at"])
        apply_transition(
            self.order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"}
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(self.customer.commission_rate, Decimal("20.00"))
        self.assertEqual(self.customer.review_status, ReviewStatus.APPROVED)

    def test_bypassing_the_transition_does_not_touch_the_customer_row(self):
        """⭐ §9 #12④ 反向：`Order.objects.update(status=COMPLETED)` 绕过迁移 → 副作用**没有**发生。"""
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(self.customer.commission_rate)
        self.assertIsNone(self.customer.review_status)

    def test_completed_hook_is_registered_in_the_target_hook_table(self):
        """⭐ Ruling O #1：`COMPLETED` 的钩子表里**真的有**这个函数。

        结构判据防「没人 import 模块」；端到端判据（上面两条）防「注册了但逻辑不对」——
        **两者都要**（不能靠「模块里有 `register_target_hook` 调用」就当它生效）。
        """
        module = _settlement_hooks()
        hooks = _TARGET_HOOKS.get(str(OrderStatus.COMPLETED), [])
        self.assertIn(module.on_order_completed, hooks)

    def test_generic_transition_endpoint_completes_and_determines_the_commission(self):
        """⭐ **通用 `/transition/` 也能把订单改完成，并且钱在那一刻生效**（D-0071 取消了
        spec §4.1.1 的路径守卫；授权判据 = 节点 `order.change_status_completed`（17），
        失败形态是 403）。本夹具的客服主管持该节点（`_reviewer_client` 的 `nodes=`），
        所以这条路径**应当成功**。

        判别力：同时断三件事——① 订单真的变成 `COMPLETED`；② 客服行的钱**真的被写**
        （`9.60` = 利润 `48.00` × `20%`，字面值）；③ 通用端点**不替你做审核**
        （`reviewed_by` / `reviewed_at` 仍空，且没有 `performance.review` 留痕；
        ⭐ R5 起 `review_status` 由完成钩子写成「已通过」，不再是 ③ 的承重项）。
        少了 ③，一个「顺手把审核也做了」的实现会照样绿。
        """
        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(self.customer.commission_rate, Decimal("20.00"))
        # ③ 审核**动作**没有被顺手做掉（R5：`review_status` 已由钱生效钩子写成「已通过」）
        self.assertEqual(self.main.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.main.reviewed_by, "")
        self.assertIsNone(self.main.reviewed_at)
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.review"
            ).exists()
        )

    def test_completed_hook_is_idempotent(self):
        """重复到达 `COMPLETED` **不重复建行**、不写出不同的值。

        钩子按 `(order, SALES)` 更新**已存在**的那一行（不是 `create`），
        所以重跑只覆盖同样的值。
        """
        self.main.review_status = ReviewStatus.APPROVED
        self.main.save(update_fields=["review_status", "updated_at"])
        apply_transition(self.order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"})
        # 再直接跑一次钩子（第二次「到达」）
        _settlement_hooks().on_order_completed(self.order, OrderStatus.PENDING_REVIEW)
        rows = Performance.objects.filter(order=self.order, emp_type=EmpType.SALES)
        self.assertEqual(rows.count(), 1)
        customer = rows.get()
        self.assertEqual(customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(customer.commission_rate, Decimal("20.00"))
        self.assertEqual(customer.review_status, ReviewStatus.APPROVED)

    def test_order_without_a_customer_row_completes_without_error(self):
        """没有客服行（录单时 `sales_user` 为空）时，钩子**什么都不做**、不报错。"""
        order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000009",
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username="taker-9")
        main.review_status = ReviewStatus.PENDING
        main.save(update_fields=["user", "review_status", "updated_at"])
        client, _ = _reviewer_client("reviewer-9", game=self.game)
        body = client.post(
            f"/api/business/performances/{main.id}/review/",
            {"approved": True},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )


# ── ③ D-0071：`ISSUE`/`PAUSED → COMPLETED` 的钱**照样生效**（审核闸门取消）──────
class CompletedWithoutTakerApprovalTests(TenantTestCase):
    """订单从**待验收**改完成时，**D-0071 取消了「打手绩效必须已通过」这道闸门**
    ⇒ 打手行停在「已驳回」也**必须照常确定客服提成**（正向判据；对标没有审核、
    完成后绩效即生效）。**这不是放宽判据**——真正的授权判据（节点 17）与跨租户隔离在
    `tests/test_p4fu_order_complete.py` 里另有判别力测试。

    ⚠️ 入 `COMPLETED` 的边只剩 `PENDING_REVIEW → COMPLETED`（spec §2.3）⇒ `ISSUE` /
    `PAUSED` 两个来源态已删，被删的那两条边由
    `test_removed_source_states_do_not_write_the_commission` 单独钉住。
    ⭐ R6：完成时打手行**照常生成**（`bonus := temp`、`review_status := 已通过`）。

    金额实数：`total_price = 88.00`（`received_price` 为空 ⇒ 回落**订单总价**；
    陷阱值 `hall_price = 50.00`）、打手 `temp_amount = 50.00`（录单入口按发单价建）⇒
    完成时 `bonus := 50.00` ⇒ 利润 `38.00`；提成率 `20.00` ⇒ **`7.60`**。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.sales = _sales_user()

    def _order(self, no, status):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("50.00"),
            total_price=Decimal("88.00"),
            sales_user=self.sales,
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username=f"taker-{no}")
        main.review_status = ReviewStatus.REJECTED      # ← 「没审过」的打手行，**不再**是障碍
        main.save(update_fields=["user", "review_status", "updated_at"])
        return order, main

    def _assert_determined(self, order):
        order.refresh_from_db()
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        # ⭐ **R5/R6 改准**：完成时打手 `bonus := temp`（夹具 `hall_price=50.00`
        # ⇒ `temp = 50.00`）⇒ 利润 = 88.00 − 50.00 = 38.00 ⇒ 客服 = 38.00 × 20% = 7.60
        #（旧值 17.60 对应的是「打手行停在 0.00、不被生成」那种旧口径）。
        self.assertEqual(customer.bonus_amount, Decimal("7.60"))
        self.assertEqual(customer.commission_rate, Decimal("20.00"))
        return customer

    def test_pending_review_to_completed_determines_the_commission(self):
        """经 `apply_transition` 从待验收完成 ⇒ 钱生效。

        ⭐ **R6 改准**：打手行**起始**停在「已驳回」也照常完成、照常生成
        ——完成时 `bonus := temp`（`50.00`）、`review_status` 置「已通过」。
        旧断言「仍停在已驳回 / 没有被顺手改成已通过」已按 R6 反转。
        """
        order, main = self._order("20260920000000000011", OrderStatus.PENDING_REVIEW)
        apply_transition(order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"})
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self._assert_determined(order)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("50.00"))          # R6：照常生成
        self.assertEqual(main.review_status, ReviewStatus.APPROVED)

    def test_removed_source_states_do_not_write_the_commission(self):
        """F3：`PAUSED → COMPLETED` 已被删除 ⇒ 抛 `IllegalTransition` 且**一分钱都不写**。

        ⚠️ 判别力在**第二半**：只断「抛了」会漏掉「先写了钱再抛」的实现
        （钩子是在状态写库之后才跑的，若把这条边又放回来，钱会被写）。
        """
        order, _ = self._order("20260920000000000012", OrderStatus.PAUSED)
        with self.assertRaises(IllegalTransition):
            apply_transition(
                order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"}
            )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        # 客服行仍是**录单时**的初始形态（`bonus_amount = 0.00`、率未落）
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.commission_rate)

    def test_generic_endpoint_completes_from_pending_review(self):
        """同一件事的**端点侧**判据：通用 `/transition/` 从待验收能完成并写钱。

        📌 **夹具声明的门**：`target=COMPLETED` 要节点
        `order.change_status_completed`（17，D-0071 起由 `get_permissions()` 按 target 挑）。
        **这是声明夹具需要的门，不是放宽门。**
        """
        order, _ = self._order("20260920000000000020", OrderStatus.PENDING_REVIEW)
        client = make_logged_in_client(
            username="cs-complete-0", groups=("客服",),
            nodes=("order.change_status_completed",),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 通用 `/transition/` 经 `get_object()`（被 `game_scope` 收窄的 queryset）
        # 取单 ⇒ 这个客服必须显式持有这张单的游戏；缺档案 ⇒ 1002。
        grant_games(User.objects.get(username="cs-complete-0"), self.game)
        body = client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self._assert_determined(order)


# ── ④ Global Constraints 18 / E-0027：`commission_rate` 的回退路径 ────
class CommissionRateRollbackTests(TenantTestCase):
    """**结论（逐条）**——客服行的 `commission_rate` 在三处「回退 / 清理」路径上**都不清**：

    | 路径 | 结论 | 为什么 |
    |---|---|---|
    | 清除接单人（`reset_taker_performance` 的四元组） | **不清** | 那四元组是**打手行**的字段；`commission_rate` 是**客服行**上的、销售客服配置的快照。客服行的归属人不是接单人，清接单人不改变客服的提成率——不存在 E-0027 那种「上一个人的值传给下一个人」的泄漏面 |
    | 驳回（`/review` 驳回 → `IN_PROGRESS`） | **不清** | 驳回只动**打手行**（`review_status = 已驳回`）与订单状态；客服行只在 `COMPLETED` 写。清它会让快照进入「有金额无率」的半新半旧态，而下次 `COMPLETED` 本来就会覆盖 |
    | 退单清空（全额退款 → `REFUNDED`） | **不清**（且绝不能清） | 退款钩子不读 `commission_rate`。冲到 0 只用 `bonus_amount` 和冲减链。 |

    ⚠️ 退单那条的**执行钩子在退款线**；这里用一条**可构造的回归**先钉住不变量：
    迁到 `REFUNDED` 不得清客服行的率（将来若加清理，此条会红）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.sales = _sales_user()

    def _determined(self, no, status):
        """造一张「已经走到过 `COMPLETED`、客服行快照已写」的单。"""
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("50.00"),
            total_price=Decimal("88.00"),
            sales_user=self.sales,
        )
        create_performance_for_order(order)
        main = main_performance_of(order)
        main.user = User.objects.create_user(username=f"taker-{no}")
        main.save(update_fields=["user", "updated_at"])
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        # 直接落快照：模拟「曾经 COMPLETED」。`order.sales_user` 未变 ⇒ 归属一致。
        Performance.objects.filter(pk=customer.pk).update(
            commission_rate=Decimal("20.00"), bonus_amount=Decimal("9.60"),
            review_status=ReviewStatus.APPROVED,
        )
        customer.refresh_from_db()
        return order, main, customer

    def test_clearing_the_taker_keeps_the_customer_rate(self):
        order, main, customer = self._determined(
            "20260920000000000021", OrderStatus.IN_PROGRESS
        )
        # 让打手行的四元组处在**非初始**态，回退才有判别力。
        main.review_status = ReviewStatus.REJECTED
        main.bonus_amount = Decimal("66.00")
        main.save(update_fields=["review_status", "bonus_amount", "updated_at"])
        # ⭐ D2（spec §4.1.2 例外①）：该 MAIN 行此刻有 `user` ⇒ 回池要走「显式清除接单人」
        #    这个产品动作（= `/clear-taker/` 那条路）才行；本用例钉的是「清打手 **不**清客服快照」。
        release_taker(order, nodes={"order.change_status"})

        main.refresh_from_db()
        # 阳性对照：打手行的四元组**确实**回退了（否则「客服行没变」可能只是迁移没跑）。
        self.assertIsNone(main.user_id)
        self.assertIsNone(main.review_status)
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertIsNone(main.taker_price)

        customer.refresh_from_db()
        self.assertEqual(customer.commission_rate, Decimal("20.00"))
        self.assertEqual(customer.bonus_amount, Decimal("9.60"))
        self.assertEqual(customer.review_status, ReviewStatus.APPROVED)

    def test_rejection_keeps_the_customer_rate(self):
        order, main, customer = self._determined(
            "20260920000000000022", OrderStatus.PENDING_REVIEW
        )
        main.review_status = ReviewStatus.PENDING
        main.save(update_fields=["review_status", "updated_at"])
        client, _ = _reviewer_client("reviewer-rollback", game=self.game)
        body = client.post(
            f"/api/business/performances/{main.id}/review/",
            {"approved": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        main.refresh_from_db()
        order.refresh_from_db()
        # 阳性对照：驳回落库了。
        self.assertEqual(main.review_status, ReviewStatus.REJECTED)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

        customer.refresh_from_db()
        self.assertEqual(customer.commission_rate, Decimal("20.00"))
        self.assertEqual(customer.bonus_amount, Decimal("9.60"))

    def test_refund_transition_keeps_the_customer_rate(self):
        """退单清空**不得**清客服行的率。

        快照是历史事实，原行不可变。退单只做目标式冲减，那个算式不读 `commission_rate`。
        """
        order, main, customer = self._determined(
            "20260920000000000023", OrderStatus.COMPLETED
        )
        apply_transition(order, OrderStatus.REFUNDED, nodes={"order.change_status"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)   # 阳性对照：迁移发生了
        customer.refresh_from_db()
        self.assertEqual(customer.commission_rate, Decimal("20.00"))


# ── ⑤ 事务原子性（§9 #14）──────────────────────────────────────────
class CustomerCommissionAtomicityTests(TenantTestCase):
    """钩子内某笔写抛异常 ⇒ **订单状态与客服行两个方向都回滚**（spec §5.3 细节 0/1）。

    ⚠️ **桩必须打在「被查找的模块」**（E-0029）：`on_order_completed` 通过
    `settlement_hooks._persist_customer_commission` 这个模块全局名查写入函数，
    所以打在那里。打在被定义处 / 注册表里的函数对象上**不会生效**——
    表现是「没有异常抛出」，而不是「回滚没生效」。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.sales = _sales_user()
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000031",
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
            total_price=Decimal("108.00"),
            received_price=Decimal("1.00"),        # 陷阱值：新口径下不参与提成基数
            sales_user=self.sales,
        )
        create_performance_for_order(self.order)
        self.main = main_performance_of(self.order)
        self.main.user = User.objects.create_user(username="taker-atomic")
        self.main.temp_amount = Decimal("60.00")
        self.main.bonus_amount = Decimal("60.00")
        self.main.review_status = ReviewStatus.PENDING
        self.main.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "review_status", "updated_at"
            ]
        )
        self.customer = Performance.objects.get(order=self.order, emp_type=EmpType.SALES)
        self.client, _ = _reviewer_client("reviewer-atomic", game=self.game)
        self.url = f"/api/business/performances/{self.main.id}/review/"

    def _approve(self):
        return self.client.post(
            self.url, {"approved": True}, content_type="application/json"
        )

    def test_without_the_stub_the_same_flow_succeeds(self):
        """**对照**：不接桩时这条流程本来会成功并写出 `9.60`——
        它证明下面那条的失败**确实**来自桩，而不是流程本来就坏。"""
        body = self._approve().json()
        self.assertEqual(body["code"], 0)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.bonus_amount, Decimal("9.60"))

    def test_hook_failure_rolls_back_the_status_and_the_customer_row(self):
        seen = {}
        module = _settlement_hooks()
        real_persist = module._persist_customer_commission

        def _write_then_boom(customer, *, bonus_amount, rate):
            # 先让客服行**真的**落库（模拟「钩子写了一半」），再抛
            real_persist(customer, bonus_amount=bonus_amount, rate=rate)
            seen["bonus"] = Performance.objects.get(pk=customer.pk).bonus_amount
            # 订单状态此刻也已经落库（`apply_transition` 在跑钩子**之前**保存）。
            # ⚠️ 必须在**桩内部**读：异常冒出 `atomic()` 之后两边都已回滚。
            seen["status"] = Order.objects.get(pk=self.order.pk).status
            raise RuntimeError("boom")

        # ⚠️ 用 `patch.object`（模块对象已取到）而**不是**字符串路径：字符串路径会
        #    `import_module`，那会自己触发 `settlement_hooks` 的注册、掩盖 M1 变异。
        with mock.patch.object(
            module, "_persist_customer_commission", side_effect=_write_then_boom
        ):
            with self.assertRaises(RuntimeError):
                self._approve()

        # 哨兵：抛错前客服行与订单状态**都已经落库**了——否则「回滚」对「根本没写」
        # 不加区分（这正是本用例要防的假通过）。
        self.assertEqual(seen["bonus"], Decimal("9.60"))
        self.assertEqual(seen["status"], OrderStatus.COMPLETED)

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)   # ← 状态回滚
        self.assertEqual(self.main.review_status, ReviewStatus.PENDING)   # ← 打手行回滚
        self.assertEqual(self.customer.bonus_amount, Decimal("0.00"))     # ← 客服行回滚
        self.assertIsNone(self.customer.commission_rate)
        self.assertIsNone(self.customer.review_status)

    def test_fixed_amount_profile_returns_a_business_code_and_rolls_everything_back(self):
        """销售档案是 `固定金额`(10) ⇒ 确定客服提成时 `CommissionError`。

        `COMPLETED` 钩子要的是 `利润的百分比`(30)，档案却是 `固定金额` ⇒
        `commission_percent_of` 抛 `CommissionError`（`code = 1000`），由 `/review` 的
        `except (CommissionError, AmountError)` 翻成业务码并**显式回滚**。

        ⚠️ 期望值写字面量 `1000`——**不引 `EC.VALIDATION`**（E-0024：常量被改坏时自指断言不会红）。
        ⚠️ 三处回滚各断**具体值**（订单态 / 打手行审计字段 / 客服行三字段），
        而不是「不等于 APPROVED」那种对「根本没写」同样成立的断言。
        """
        StaffProfile.objects.filter(user=self.sales).update(
            commission_mode=CommissionMode.FIXED
        )

        response = self._approve()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 1000)              # ← 字面值，不引常量（E-0024）
        self.assertIn("固定金额", body["msg"])             # 说清档案是哪种
        self.assertIn("利润的百分比", body["msg"])         # 也说清本处期望哪种

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)  # 订单回滚
        self.assertEqual(self.main.review_status, ReviewStatus.PENDING)  # 打手行回滚
        self.assertEqual(self.main.reviewed_by, "")                      # 审核人回滚
        self.assertIsNone(self.main.reviewed_at)                         # 审核时间回滚
        self.assertEqual(self.customer.bonus_amount, Decimal("0.00"))    # 客服行回滚
        self.assertIsNone(self.customer.commission_rate)
        self.assertIsNone(self.customer.review_status)


# ── ⑥ 兼岗账号：销售归属人不能同时成为接单人──────────
class SalesUserCannotAlsoBeTakerTests(TenantTestCase):
    """`uniq_performance_order_user` 是 **不分 `emp_type`** 的 `(order, user)` 唯一约束
    （`models.py:440-442`），而录单会为 `order.sales_user` 建一条 SALES 行
    （`performance.py:62-72`）。于是**同一个人**再成为该单的接单人时，MAIN 行的写入会撞
    唯一索引 ⇒ `psycopg.errors.UniqueViolation` ⇒ **DRF 500**（实测复现两条路径）。

    裁定：**保持约束现状**，在 `/assign` 与 `/hall/{no}/claim/` 两条路径上**显式拒绝**
    并返回业务码（fail-closed、可逆）。

    ⛔ **不要**把约束放宽成 `(order, user, emp_type)` 或给它加 `condition=`：
    `tests/test_my_orders_api.py` 的
    `test_same_order_and_user_cannot_have_a_second_performance_row` 已经**刻意钉住**
    「同一 `(order, user)` 不能有第二行」，且它的 docstring 记着「P3b brief 说这种行合法
    —— **实测不合法**」。那是**被实测裁定过的既有不变量**，P4 不得为了让路而静默反转它。
    是否支持兼岗是**业务政策问题**，已登记为待决项，本轮不猜。

    ⚠️ 拒绝**只是拒绝**：不删 / 不改已建的客服行、也不改 `sales_user`
    ——所以 `_assert_untouched` 断的是**精确行集合**与订单状态，而不是「没报错」。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 兼岗账号 U：持「代练员」（`/claim` 的入口闸门要求它），同时是两张单的销售归属人。
        # **本夹具声明它要过的门**：`/claim` 的类级节点门是 `grab_hall.take_order`
        # （`ORDER_PERMISSIONS["claim"]`）；`groups=("代练员",)` 只用于建组，放行靠
        # `nodes=("grab_hall.take_order",)`（函数体不再有职位闸门）。
        # 两处缺一不可（`nodes=` 只挂到已有 Group 的 Role 上）。**这是声明夹具需要的门，不是放宽门。**
        self.dual_client = make_logged_in_client(
            username="dual-role", groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        self.dual = User.objects.get(username="dual-role")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # U 在 `test_claim_by_the_sales_user_is_refused_with_a_business_code` 里**发请求**
        # （`/claim` 经 `get_object()` 取单）⇒ 必须显式持有这张单的游戏，否则它拿到的是
        # 1002「记录不存在或已被删除」，而不是本文件要测的「不能抢自己录的单」。
        # ⚠️ 它同时是「销售归属人」（`/assign` 的**被指派**对象）——那是被拒绝的一侧，
        # 本函数只是让「它作为请求方」这条路成立，不是替那枚拒绝钉子开口子。期望值一个字没改。
        grant_games(self.dual, self.game)
        self.assign_order = self._order("20260920000000000051", OrderStatus.UNASSIGNED)
        self.claim_order = self._order("20260920000000000052", OrderStatus.WAITING_GRAB)
        self.assign_url = f"/api/business/orders/{self.assign_order.order_no}/assign/"
        self.claim_url = f"/api/business/hall/{self.claim_order.order_no}/claim/"

    def _order(self, no, status):
        """建一张 `sales_user = U` 的单并走**真实录单建行**（MAIN 无归属 + SALES 归 U）。"""
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("88.00"),
            sales_user=self.dual,
        )
        create_performance_for_order(order)
        return order

    def _rows(self, order):
        """该单的绩效行集合 `{(emp_type, user_id)}`——**精确集合**同时钉住行数与归属。"""
        return {
            (row.emp_type, row.user_id)
            for row in Performance.objects.filter(order=order)
        }

    def _assert_untouched(self, order, expected_status):
        order.refresh_from_db()
        self.assertEqual(order.status, expected_status)
        # 没有多出任何绩效行，也没有被改：MAIN 一条（user 空）+ SALES 一条（user=U）。
        self.assertEqual(
            self._rows(order),
            {(EmpType.MAIN, None), (EmpType.SALES, self.dual.id)},
        )

    def test_assign_to_the_sales_user_is_refused_with_a_business_code(self):
        """① `/assign` 指向销售归属人：**HTTP 200 + 业务码**（**不是 500**）+ 单仍可指派 + 零新行。

        判别力：修复前这条路径以 `IntegrityError`（`uniq_performance_order_user`）炸出，
        客户端拿到 500；去掉拒绝判据即回到那个形状（见报告「变异三组」）。
        """
        # `/assign` 的类级节点门是 `order.assign_taker`
        # （或 `order.submit_payout_price` 任一，`ORDER_PERMISSIONS["assign"]`）。
        # 声明它，本用例测的才是「按 `sales_user` 拒绝」而不是节点门的 403。
        client = make_logged_in_client(
            username="cs-dispatch", groups=("客服",), nodes=("order.assign_taker",)
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。`/assign` 经 `get_object()`
        # （`game_scope` 收窄的 queryset）取单 ⇒ 发指派的账号必须显式持有这张单的游戏；
        # 缺档案 ⇒ 1002 而不是本用例的判据。期望值一个字没改。
        grant_games(User.objects.get(username="cs-dispatch"), self.game)
        response = client.post(
            self.assign_url, {"user_id": self.dual.id}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)          # ⚠️ 修复前这里是 500
        body = response.json()
        self.assertEqual(body["code"], EC.BUSINESS)
        self.assertIn("销售提成归属人", body["msg"])          # 判据来源可辨
        self._assert_untouched(self.assign_order, OrderStatus.UNASSIGNED)

    def test_claim_by_the_sales_user_is_refused_with_a_business_code(self):
        """③ U 以「代练员」身份抢**自己录的单**：同上（**不是 500**）+ 单仍在待抢单。

        ⚠️ 起点必须是 `WAITING_GRAB`（否则 `claim_order` 会先回「不在抢单大厅」，
        `msg` 断言当场把这种「其实没走到冲突那一步」的假通过区分开）。
        """
        response = self.dual_client.post(
            self.claim_url, {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)          # ⚠️ 修复前这里是 500
        body = response.json()
        self.assertEqual(body["code"], EC.BUSINESS)
        self.assertIn("销售提成归属人", body["msg"])
        self._assert_untouched(self.claim_order, OrderStatus.WAITING_GRAB)

    def test_assign_to_another_user_still_succeeds(self):
        """④ **正向对照**：指派给**另一个**人（≠ `sales_user`）必须成功。

        没有这一条，「按 `sales_user` 拒绝」与「一律拒绝」在测试上不可区分。
        """
        other = User.objects.create_user(username="taker-other")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 被指派者也要授权
        #    （见 tests/test_order_assign_game_gate.py 的专钉）。
        grant_games(other, self.game)
        # 同 `test_assign_to_the_sales_user_...`，`/assign` 要 `order.assign_taker`。
        client = make_logged_in_client(
            username="cs-dispatch-2", groups=("客服",), nodes=("order.assign_taker",)
        )
        # （按游戏权限收窄）：**夹具补齐**（同 `test_assign_to_the_sales_user_...`）。
        # 这是**正向对照**：断言它**能**指派成功，所以必须持有这张单的游戏。
        grant_games(User.objects.get(username="cs-dispatch-2"), self.game)
        body = client.post(
            self.assign_url, {"user_id": other.id}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0)
        self.assign_order.refresh_from_db()
        self.assertEqual(self.assign_order.status, OrderStatus.IN_PROGRESS)
        self.assertIn((EmpType.MAIN, other.id), self._rows(self.assign_order))

    def test_claim_by_another_taker_still_succeeds(self):
        """④ **正向对照**（`/claim` 侧）：另一个代练员抢同一张单必须成功。"""
        # `/claim` 要 `grab_hall.take_order`（同 `setUp` 的 `dual_client`）。
        client = make_logged_in_client(
            username="taker-2", groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        other = User.objects.get(username="taker-2")
        # （按游戏权限收窄）：**夹具补齐**（同 `test_assign_to_another_user_...`）。
        # 这是**正向对照**：断言另一个代练员**能**抢到这张单 ⇒ 必须持有这张单的游戏。
        grant_games(other, self.game)
        body = client.post(
            self.claim_url, {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0)
        self.claim_order.refresh_from_db()
        self.assertEqual(self.claim_order.status, OrderStatus.IN_PROGRESS)
        self.assertIn((EmpType.MAIN, other.id), self._rows(self.claim_order))


# ── ⑦ 注册表清理（`_TARGET_HOOKS` 是模块级字典）──────────────────────
class HookRegistryCleanupTests(TenantTestCase):
    """测试自己挂的钩子必须清掉，否则**跨测试污染**同进程里后面的用例。

    ⚠️ 生产钩子挂在哪几个目标态（按实现核过）：`UNASSIGNED` / `WAITING_GRAB`
    （`performance.py`）、`IN_PROGRESS` / `PENDING_REVIEW`（`notifications.py`）、
    `COMPLETED` / `REFUNDED`（`settlement_hooks.py`）。所以探针挑 `ISSUE`——**当前没有生产钩子**
    （`PAUSED` 已被 `test_order_status.TargetHookTests` 用作探针；`REFUNDED` 上已有退款钩子）。

    ⚠️ 写法必须**与执行顺序无关**：每条用例的 `setUp` 自己先 `clear_target_hooks`、再挂自己的
    探针——靠方法名字母序建立依赖时，「上一条的 `tearDown` 没清干净」只有它先跑过才看得见，
    加用例就可能让哨兵**悄悄失效**；「注册表里只有我一个」是每条用例都独立成立的判据。
    """

    PROBE_TARGET = OrderStatus.ISSUE

    def setUp(self):
        # ⚠️ **先清后建**：不依赖「上一条用例有没有跑过 / 它的 tearDown 有没有生效」。
        clear_target_hooks(self.PROBE_TARGET)
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000041",
            status=OrderStatus.IN_PROGRESS,
        )
        self.calls = []
        register_target_hook(
            self.PROBE_TARGET,
            lambda order, from_status: self.calls.append(from_status),
        )

    def tearDown(self):
        # 注册表是**模块级**的：不清理就会污染同进程里后面的测试。
        clear_target_hooks(self.PROBE_TARGET)

    def test_probe_fires_on_the_probe_target(self):
        apply_transition(self.order, self.PROBE_TARGET, nodes={"order.change_status"})
        self.assertEqual(self.calls, [OrderStatus.IN_PROGRESS])

    def test_only_this_tests_probe_is_registered(self):
        """注册表里**恰好**只有本用例刚挂的那一个——与用例执行顺序无关。

        判别力：把 `setUp` 里的 `clear_target_hooks` 删掉、并让别的用例/文件在本类之前
        污染 `ISSUE`，本条就会读到 >1 个探针而变红。
        （旧版靠 `test_a_` 先跑来制造这个前置污染，所以它测的是「顺序」而不是「清理」。）
        """
        self.assertEqual(len(_TARGET_HOOKS.get(str(self.PROBE_TARGET), [])), 1)
