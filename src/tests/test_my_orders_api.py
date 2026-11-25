"""B2-2：`GET /api/business/my-orders`（「我接的单」，spec §4.4 / §5）。

四条判据：
- **谁**：持有 `my_orders.send_to_my_system` 节点即可看
  （`MyOrdersViewSet.MY_ORDERS_PERMISSIONS` 的类级门）；没有它 ⇒ HTTP 403；匿名 ⇒ 401。
  ⚠️ **2026-09-23 单轴化**：原先还有一道 `Role.TAKER` 职位门（③线 Task 4），本线**删除**了它
  ——自定义职位 /「客服」持节点时**放行**（本线的目标形态）；
  职位与节点两向配对的用例在 `tests/test_rbac_endpoint_enforcement.py`；
- **哪几行**：`Performance` 里 `user == request.user` **且** `emp_type == 打单工资`
  的那些行——spec §4.4 的原话是「会在 view 内再叠加 `user == request.user`」
  （这**不是**数据权限，是功能语义）。
  ⚠️ 后半句（`emp_type=MAIN`）**不是**冗余：**同一个人可以在不同订单上分别是
  `打单工资` 与 `销售提成`**（`uniq_performance_order_user` 只约束**同一** `(order, user)`）。
  所以「我**接**的单」必须按 emp_type 收窄——`user=X` 单独用会把「X 只是拿销售提成的单」
  也算进来。判别力用例见 `test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`。
  （📌 本项目实测过的**错误前提**：曾据「同单同人不可能有两种 emp_type」推出「过滤冗余」——
  那只在**同一张单**上成立，跨单就有一个反例——本文件的
  `test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on` 构造的就是那个反例。）
- **形状**：P2 的分页形状 `{items,total,page,page_size}`，`page_size` 默认 20；
  畸形 `?page=abc` / `?page_size=abc` **不是** 500；
- **接线**：`scope_queryset` 必须**真的被用上**——桩打在**返回值**上（清空），
  并配不接桩的对照（P3a 的教训：只测函数本身 = 接线没人守）。

⚠️ 断言纪律：
- 期望值一律写**字面值**（E-0024），金额断具体数字；
- 可见性**不用 `count()` 单判**（`count()` 相等可能来自两套不同的过滤），
  断言具体的订单号在 / 不在；
- 数据权限的接线断言打**响应体**，不打 logger（E-0009）。

本视图只解析 `income`（`all` / `unsettled` / `settled` / `completed`）。
非法值是 `200 + code 1000`，不是静默忽略。四档、非法值、空串等于默认，
判据在 `tests/test_app_income_api.py` 的 `MyOrdersIncomeFilterTests`。
`?emp_type=BOGUS` 这类没接管的参数仍静默忽略。
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.api.my_orders_views import MyOrderSerializer
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, SettleStatus
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/my-orders/"

#: ：`/my-orders` 的类级节点门 = `my_orders.send_to_my_system`
#: （`MyOrdersViewSet.MY_ORDERS_PERMISSIONS["list"]`）。只建 `Group` 不放行 ⇒ 夹具必须显式挂。
MY_ORDERS_NODES = ("my_orders.send_to_my_system",)

#: `/my-orders/` 一行的**键集字面量**：恰好 **17** 个
#: （计划 §1 给「指派给我的订单」补的 `taker_name` / `taker_price` 是 13 + 2；
#: 「我的收入」线再加一个 `settle_requested_at` ⇒ 15 + 1；
#: ⭐ D-0301 展示回退线再加 `source_order_no` ⇒ 16 + 1）。
#: ⚠️ 写死字面值、⛔ 不从实现模块 import / 派生（派生是自指恒真，E-0024）：
#: 少一个键（静默丢字段）或多一个键（顺手扩契约）都要在这里红。
MY_ORDER_ROW_KEYS = frozenset({
    "id",
    "username",
    "taker_name",
    "order_no",
    # ⭐ D-0301：来源单号——打手端「来源号优先、空回退内置号」
    #    展示回退的取数前提（回退渲染在前端做，载荷给原值）。
    "source_order_no",
    "status",
    "game_name",
    "project_text",
    "is_urgent",
    "temp_amount",
    "bonus_amount",
    "taker_price",
    "emp_type",
    "settle_status",
    "review_status",
    "created_at",
    # ⭐ 这一行**自己的**提前结算申请时刻（没有申请 ⇒ `null`）。
        #    App 据此把「申请提前结算」按钮换成「已申请」（产品缺口，编排方 追加）。
    "settle_requested_at",
    "game_role_name",
})

# 代练员 A 的固定字面值（金额断言钉具体数字，E-0024）。
GAME_A = "仅 A 的游戏"
PROJECT_A = "深渊满星"
TEMP_A = Decimal("88.00")
BONUS_A = Decimal("17.60")
NO_A = "20260920000000000101"
NO_B = "20260920000000000102"
#: 代练员 C **自己接**的那张单（`...103`）——它与代练员 A 的 `NO_A`（`...101`）是**不同的单**，
#: 只是同属代练员 C 的构造：见 `setUp` 的注释与
#: `test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`。
NO_A_OWN = "20260920000000000103"
#: 一条 `user=None`（还没归属人）的绩效行用的订单号——「我的收入」页那条空 user 用例。
NO_UNASSIGNED = "20260920000000000104"


class MyOrdersApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name=GAME_A)
        #: username → 已登录 client。夹具只建号一次（见 `_login`）。
        self._clients = {}
        # ⚠️ `make_logged_in_client` **自己建号**（它不接受已存在的用户），所以代练员 A 由它创建，
        #    再取回来当绩效归属人——不要另外 `create_user(username="taker_a")`，
        #    那会撞 `auth_user_username_key`（实测：9 条全 ERROR 在 setUp，与端点无关）。
        self.client = make_logged_in_client(
            username="taker_a", groups=("代练员",), nodes=MY_ORDERS_NODES
        )
        self._clients["taker_a"] = self.client
        self.taker_a = User.objects.get(username="taker_a")
        # ⚠️ `taker_b` / `taker_cs` **不在这里建**：它们需要能登录，而本项目里
        #    「能登录」只有 `make_logged_in_client`（真实登录端点）能保证——
        #    `User.objects.create_user` + `client.login()` 产出的会话**没有 `identity_plane`**，
        #    会被 fail-closed 的认证类拒成 401（实测：401 != 0）。
        #    所以它们的账号由 `_login` 当场建（每个用例第一次调用时建一次）。
        self._login("代练员", username="taker_b")
        self.taker_b = User.objects.get(username="taker_b")
        self._login("代练员", username="taker_cs")
        self.taker_cs = User.objects.get(username="taker_cs")
        self.perf_a = self._perf(
            NO_A, self.taker_a, TEMP_A, bonus=BONUS_A, project_text=PROJECT_A
        )
        self.perf_b = self._perf(NO_B, self.taker_b, Decimal("100.00"))
        # 代练员 C 在**订单 A** 上接单（`打单工资`）——那是它唯一"接"的单。
        self.perf_c = self._perf(NO_A_OWN, self.taker_cs, Decimal("66.00"))
        # ⚠️ 关键构造（见 `test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`）：
        #    代练员 C 在**别人的订单 B** 上只有一条 `销售提成`（B 的接单人是代练员 B）。
        #    约束 `(order, user)` 唯一只禁**同一** (order, user) 重复 ⇒ 这条**合法**。
        self.sales_c = Performance.objects.create(
            order=self.perf_b.order,
            emp_type=EmpType.SALES,
            user=self.taker_cs,
            temp_amount=Decimal("7.00"),
            bonus_amount=Decimal("7.00"),
        )
        # 上面几次 `_login` 会把 `self.client` 换成别人；测试默认身份回到代练员 A。
        self.client = self._clients["taker_a"]

    def _perf(self, no, user, price, bonus="0.00", project_text="", game=None, **kw):
        order = Order.objects.create(
            game=game or self.game,
            order_no=no,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal(price),
            project_text=project_text,
        )
        return Performance.objects.create(
            order=order,
            emp_type=kw.pop("emp_type", EmpType.MAIN),
            user=user,
            temp_amount=Decimal(price),
            bonus_amount=Decimal(bonus),
            **kw,
        )

    def _login(self, *groups, username, nodes=MY_ORDERS_NODES):
        """以 `username` 登录，**同一个号在一个用例内只建一次**。

        `make_logged_in_client` 每次都会 `create_user`，重复调用同一 username 会撞
        `auth_user_username_key`——那不是端点行为，是夹具用法错误。
        缓存命中就复用；缓存只在本测试实例内存活（每个用例一个实例），
        所以建号不会跨用例泄漏（`TenantTestCase` 每个用例回滚）。
        `nodes` 可覆盖：单轴化后**节点**才是判据，拒绝侧要显式不给（spec §4.4）。
        """
        cached = self._clients.get(username)
        if cached is not None:
            self.client = cached
            return User.objects.get(username=username)
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        self._clients[username] = self.client
        return User.objects.get(username=username)

    def _body(self, params=None):
        return self.client.get(URL, params or {}).json()

    def _items(self, params=None):
        body = self._body(params)
        self.assertEqual(body["code"], 0)          # 前置哨兵：端点本身是成功的
        return body["data"]["items"]

    def _nos(self, params=None):
        return sorted(row["order_no"] for row in self._items(params))

    # ── 只看到自己的（承重用例）─────────────────────────────────
    def test_taker_sees_only_own_performances_each_way(self):
        """⚠️ **两个方向都断**，且断言**具体订单号集合**。

        只断 `count()` 不够：两套不同的过滤（例如「只按 emp_type」或
        「`user` 取错成别人」）都可能凑出同一个条数。这里用**精确集合**：
        代练员 A 只能看到 `NO_A`，绝不能看到 `NO_B`。
        """
        self.assertEqual(self._nos(), [NO_A])                        # 代练员 A：只有 101
        self._login("代练员", username="taker_b")
        self.assertEqual(self._nos(), [NO_B])                        # 代练员 B：只有 102

    def test_taker_without_any_performance_gets_the_empty_pagination_shape(self):
        """空列表**仍然是完整的分页形状**（`total=0`、`items=[]`），不是 `data=null`。"""
        self._login("代练员", username="taker_no_perf")
        body = self._body()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["page_size"], 20)
        self.assertEqual(set(data), {"items", "total", "page", "page_size"})

    def test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on(self):
        """⚠️ **`emp_type=MAIN` 的判别力**：过滤的是**接单人身份**，不是「我在这单上有绩效」。

        构造（两条都必须能落库——见自证前提）：

        - 订单 `NO_A_OWN`：`taker_cs` = **`打单工资`**（他接的单）；
        - 订单 `NO_B`：`taker_b` = `打单工资`（**别人**接的），`taker_cs` = **`销售提成`**
          ——他在这一单上只是拿销售提成，**不是接单人**。

        `uniq_performance_order_user` 只约束**同一** `(order, user)`，所以「同一个人跨订单
        分别是 MAIN 与 SALES」**合法**（`setUp` 已建好，下面再断言一次）。
        于是 `user=taker_cs` 单独用会**多**返回 `NO_B`；加上 `emp_type=MAIN` 才正确。
        """
        # 自证前提：两行真的都在库里（否则「不含 B」可能只是 B 那条没建成）。
        self.assertTrue(
            Performance.objects.filter(
                order=self.perf_c.order, user=self.taker_cs, emp_type=EmpType.MAIN
            ).exists()
        )
        self.assertTrue(
            Performance.objects.filter(
                order=self.perf_b.order, user=self.taker_cs, emp_type=EmpType.SALES
            ).exists()
        )
        # 反向哨兵：单 B 的接单人是**代练员 B**，不是 taker_cs。
        self.assertEqual(
            Performance.objects.get(
                order=self.perf_b.order, emp_type=EmpType.MAIN
            ).user_id,
            self.taker_b.id,
        )

        self._login("代练员", username="taker_cs")
        nos = self._nos()
        self.assertIn(NO_A_OWN, nos)                    # 我接的单：在
        self.assertNotIn(NO_B, nos)                     # 我只是拿销售提成的单：不在
        self.assertEqual(nos, [NO_A_OWN])               # 精确集合（非空哨兵 + 具体值）
        self.assertEqual(len(self._items()), 1)

    def test_same_order_and_user_cannot_have_a_second_performance_row(self):
        """钉住约束 ① `uniq_performance_order_user`：同一 `(order, user)` 不能有**两行**绩效。

        brief §3 判据 8 的原话是「同一订单上再造一条 `emp_type=SALES`、**`user=同一个代练员`**
        的绩效（部分唯一索引只约束 MAIN，所以合法）」——**实测不合法**：
        `uniq_performance_order_user` 是**无条件**的 `(order, user)` 唯一约束，
        先把这种行挡住了；`uniq_order_main_taker` 的 `condition=Q(emp_type=MAIN)`
        管的是**另一个**问题（一张单只能有一个接单人）。

        ⚠️ 本条**只**声明它真正钉住的东西：**同一 (order,user) 不能有第二行**。
        它**不**能推出「`emp_type` 过滤冗余」——那只在**同一张单**内成立；
        跨订单时同一个人可以分别是 MAIN 与 SALES，所以过滤有判别力
        （见 `test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`）。
        📌 这个错误结论是本任务实施期踩到的，已登记在
        `docs/plans/2026-09-20-p3b-performance-review.md` 的 Part 2。

        ⚠️ 越界写入必须用 `transaction.atomic()` 包住：PostgreSQL 里约束冲突会**污染当前事务**，
        之后任何查询都报 `TransactionManagementError`——不包的话本用例会以「查不了库」
        而不是「约束真的挡住了」结束（实测踩到）。
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            # 不能用 `get_or_create`：它内部吞掉 IntegrityError（spec §16.4 #2 已记这条坑）。
            Performance.objects.create(
                order=self.perf_c.order,      # 与既有 MAIN 行**同一张单**
                emp_type=EmpType.SALES,       # 换个收入类型
                user=self.taker_cs,           # 同一个人 ⇒ 撞约束 ①
                temp_amount=Decimal("5.00"),
                bonus_amount=Decimal("5.00"),
            )
        # 落库检查：那条非法行**没有**进去，库里仍是 MAIN 一条。
        # ⚠️ 本用例到数据库为止——**不**再断言端点（那是另一个关注点，见上面的判别力用例）。
        self.assertEqual(Performance.objects.filter(order=self.perf_c.order).count(), 1)
        self.assertEqual(
            list(
                Performance.objects.filter(order=self.perf_c.order).values_list(
                    "emp_type", flat=True
                )
            ),
            [EmpType.MAIN],
        )

    # ── 谁可以看 ────────────────────────────────────────────────
    def test_a_non_taker_node_holder_can_read_my_orders(self):
        """持 `my_orders.send_to_my_system` 的**非代练员**也能看（2026-09-23 单轴化）。

        ⚠️ 本条替换了旧的 `test_non_taker_is_rejected_with_a_distinguishable_message`
        ——它钉的是 `my_orders_views` 里那道 `Role.TAKER` 职位闸门，那道门已随单轴化删除。
        新口径：**类级节点门是唯一判据**，查询集又已收窄到 `user=request.user`
        ⇒ 内置「客服」或任意自定义职位持该节点时**放行**（本线的目标形态）；
        不持节点 ⇒ 403（类级节点门）。
        行集断言一并保留：客服没有 MAIN 行 ⇒ `total == 0`（**不是**全量）。
        """
        self._login("客服", username="cs_my_orders")
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 0)

        self._login("无节点组", username="no_node_my_orders", nodes=())
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").get(URL)
        self.assertEqual(response.status_code, 401)

    def test_node_gate_runs_before_query_params(self):
        """**节点门先于参数解析**：无节点账号带畸形 `?page=abc` ⇒ **403**，不是 200 + 1002。

        判别力（复审 Finding 8 + 2026-09-23 单轴化）：类级节点门由
        `get_permissions()` 在函数体之前执行；若把它挪到分页解析**之后**，
        未授权者就能从「403」与「200 + 1002」的**差异**里探测端点行为。
        `?page=abc` 正是**实测过**会让授权用户拿到 `1002` 的畸形值
        （见 `test_malformed_pagination_params_are_not_500`）⇒ 若顺序反了，本用例会拿到 200。
        """
        self._login("无节点组", username="no_node_my_orders_gate", nodes=())
        response = self.client.get(URL, {"page": "abc"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)   # 传输层码，**不是** 1002

    def test_superuser_passes_the_gate_and_sees_own_row(self):
        """**超管放行**：`nodes_of` 对超管短路为全目录 ⇒ 200 + `code 0`，且**看得见**一条绩效。

        非空哨兵：单纯「200 + 空集」也能通过状态码断言，所以断言**具体订单号集合**。
        ⚠️ 行集仍是「`user == request.user`」的**功能语义**（spec §4.4），**不是**超管看全量
        ⇒ 哨兵是**挂在超管名下**的一条绩效，而不是去代练员 A 的单。
        """
        su_client = make_logged_in_client(username="su_my_orders", is_superuser=True)
        su = User.objects.get(username="su_my_orders")
        self._perf("20260920000000000199", su, Decimal("55.00"))

        body = su_client.get(URL).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]],
            ["20260920000000000199"],                # 非空哨兵 + 只列自己的
        )

    # ── 形状与畸形参数（200 + 业务码，不是 500）─────────────────
    def test_payload_carries_the_fields_the_list_needs(self):
        """金额 / 文本断**字面值**；`status` 是**订单**状态（不是绩效的审核状态）。"""
        Order.objects.filter(order_no=NO_A).update(game_role_name="合成角色甲")
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["game_role_name"], "合成角色甲")
        self.assertEqual(row["status"], OrderStatus.IN_PROGRESS)     # 订单状态
        self.assertEqual(row["game_name"], GAME_A)
        self.assertEqual(row["project_text"], PROJECT_A)
        self.assertEqual(Decimal(row["temp_amount"]), TEMP_A)
        self.assertEqual(Decimal(row["bonus_amount"]), BONUS_A)
        # `is_urgent` 是**订单**的加急位（`order.is_urgent`）：夹具没设过它 ⇒ 断言字面值 `False`
        # （`is` 而不是真值判断：`0` / `None` 都不会混进来，E-0021 同族）。
        self.assertIs(row["is_urgent"], False)
        self.assertIsNone(row["review_status"])
        # `id` 是绩效主键：`/adjust`、`/review` 用 `{performance_id}` 定位它。
        # ⚠️ **不是** `/submit` 用的——`/submit` 的 URL 是 `/orders/{order_no}/submit/`，
        #    按 `order_no` 标识（原文写成「`/submit` 之类要用的 `performance_id`」，不准确）。
        # 钉**具体值**：`assertIn("id", row)` 只断存在性，字段取错行（或串到别的单）也能绿。
        self.assertEqual(row["id"], self.perf_a.id)
        self.assertIn("created_at", row)
        # 「我的收入」页新增的三个只读字段（全部字面值，E-0024）：
        self.assertEqual(row["username"], "taker_a")
        self.assertEqual(row["emp_type"], "打单工资")
        self.assertEqual(row["settle_status"], "未结账")

    def test_the_row_key_set_is_exactly_the_eighteen_contract_keys(self):
        """键集**恰好** 18 个（多了 / 少了都红）。

        判据打在**响应体**上（不是序列化器类属性）：`Meta.fields` 与真实载荷之间还隔着
        `SerializerMethodField` / `allow_null` 那几层（空关系会让键**静默消失**，见
        `test_null_user_row_serializes_username_as_null`）⇒ 只有响应体才是真的出口。
        键集是**字面量**（`MY_ORDER_ROW_KEYS`，⛔ 不从实现模块派生，E-0024）。

        ⚠️ **16 → 17**（D-0301）：新增的 `source_order_no` 让键集多一个
        ——本方法名里的数字与断言必须同源（同上一批「15 → 16」的口径）。
        """
        row = self._items()[0]
        self.assertEqual(set(row), set(MY_ORDER_ROW_KEYS), row)
        self.assertEqual(
            len(MY_ORDER_ROW_KEYS), 18
        )

    def test_taker_name_prefers_the_nickname_and_falls_back_to_the_login_name(self):
        """「接手人」= 显示名（昵称优先、**空串回落登录账号**）。

        ⚠️ 两半都断（否则只实现一半也能绿）：
        - 只断昵称那一半 ⇒ 「恒取 `first_name`」在空昵称时会回空串、本用例的前半就红了；
        - 只断回落那一半 ⇒ 「恒取 `username`」也能绿 ⇒ 必须改真昵称再取一次。
        """
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_name"], "taker_a")      # 昵称为空 ⇒ 回落登录账号
        User.objects.filter(pk=self.taker_a.pk).update(first_name="打手昵称甲")
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_name"], "打手昵称甲")    # 昵称优先

    def test_taker_price_falls_back_to_the_hall_price_then_prefers_the_override(self):
        """接手价：**覆盖价优先**（`Performance.taker_price`），否则回落**大厅接单价**。

        夹具的 `hall_price` = `TEMP_A`（88.00），绩效行没有覆盖价 ⇒ 先断言回落值；
        再写一个覆盖价（66.50）⇒ 断言覆盖价赢。⚠️ 两半都断：只断回落，一个
        「恒取 `hall_price`」的实现也能绿。**⛔ 不用 `temp_amount` 冒充**：本用例把
        `taker_price` 改成与 `temp_amount` 不同的值，正是为了让两者混用时红。
        """
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_price"], "88.00")       # 没有覆盖价 ⇒ 大厅接单价
        Performance.objects.filter(pk=self.perf_a.pk).update(
            taker_price=Decimal("66.50")
        )
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_price"], "66.50")       # 覆盖价优先

    def test_taker_price_is_null_when_both_sources_are_missing(self):
        """覆盖价与大厅接单价**都没有** ⇒ `null`（⛔ 回 `"0.00"`：那是「这单不要钱」）。

        `Order.hall_price` 本身可空（还没定价的指派单）⇒ 这一档是**真实可达**的。
        """
        Order.objects.filter(pk=self.perf_a.order_id).update(hall_price=None)
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertIsNone(row["taker_price"])

    def test_other_amount_errors_are_not_swallowed_into_null(self):
        """只吞 `MissingPriceError`；别的 `AmountError` **照抛**（⛔ 全吞成 `null`）。

        判别力：把 `except MissingPriceError` 写成 `except AmountError` / `except Exception`
        ⇒ 本用例红（金额口径坏了会被静默显示成「没有接手价」）。E-0063 同族：
        「只吞没价那一档」必须按**异常类型**判，⛔ 不能按「当时的值是不是 None」判。
        """
        from apps.business.performance_amount import AmountError

        with mock.patch(
            "apps.business.api.my_orders_views.effective_taker_price",
            side_effect=AmountError("金额口径坏了", 1000),
        ):
            with self.assertRaises(AmountError):
                MyOrderSerializer(self.perf_a).data

    def test_a_zero_taker_price_is_a_real_value_not_null(self):
        """`0.00` 是**合法金额**（D-0015）⇒ 判据必须是 `is not None`（E-0021），⛔ 不是真值。

        写 `0.00` 之后回 `"0.00"`：一个 `if not value: return None` 的实现会在这里红。
        """
        Performance.objects.filter(pk=self.perf_a.pk).update(
            taker_price=Decimal("0.00")
        )
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_price"], "0.00")

    def test_the_adjust_endpoint_is_the_writer_whose_value_my_orders_shows(self):
        """⭐ **witness**（2026-10-23 评审 Important-4）：接单价的**生产写入口** → 本端点读回。

        判据 = **整链**：从 `POST /api/business/performances/<pk>/adjust/`（`taker_price` 的
        生产写入方之一）写进库，再从 `GET /api/business/my-orders/` 的**响应体**读到它。
        ⛔ 不是直接 `Performance.objects.update(taker_price=…)` 再读——那只能证明「读侧接线了」，
        证明不了「生产上真的有人写这一列」（`AGENTS.md` §5-G 那条 ⭐）。
        ⚠️ 为什么必须给接单人一份提成档案：`/adjust/` 改接单价会按提成口径**重算** `temp_amount`，
        缺档案它会在写库之前回业务码（一行都不写）⇒ 前置不成立，witness 就变成假绿。
        """
        # ⚠️ 用 `get_or_create`：`make_logged_in_client` 的默认夹具**已经**给接单人建过档案
        # （它要把当前租户的全部游戏勾上）⇒ 直接 `create` 会撞 `user_id` 唯一约束。
        profile, _created = StaffProfile.objects.get_or_create(user=self.taker_a)
        profile.commission_mode = CommissionMode.ORDER_PERCENT
        profile.commission_value = Decimal("20.00")
        profile.save(update_fields=["commission_mode", "commission_value"])
        cs = make_logged_in_client(
            username="cs_my_orders_adjust",
            groups=("客服",),
            nodes=("order.submit_payout_price",),
        )
        resp = cs.post(
            f"/api/business/performances/{self.perf_a.id}/adjust/",
            json.dumps({"taker_price": "66.00", "reason": "witness"}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 0, resp.content)
        # 前置哨兵：写入口**真的落库了**（否则下面从列表读到 66.00 可能是别的原因）。
        self.perf_a.refresh_from_db()
        self.assertEqual(self.perf_a.taker_price, Decimal("66.00"))
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["taker_price"], "66.00")

    def test_settled_performance_is_reported_as_settled(self):
        """把行改成 `SettleStatus.SETTLED` ⇒ **HTTP 响应体**回字面值 `"已结账"`。

        与上一条的 `"未结账"` 构成一对：常量 `"未结账"` 过不了这一条，
        常量 `"已结账"` 过不了那一条。
        """
        self.perf_a.settle_status = SettleStatus.SETTLED
        self.perf_a.save(update_fields=["settle_status"])
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertEqual(row["settle_status"], "已结账")

    # ── 新增字段的取值来源（钉序列化器层）────────────────────────
    def test_emp_type_is_read_from_the_row_not_a_constant(self):
        """`emp_type` 必须**读行**：`销售提成` 的行序列化出 `"销售提成"`。

        `/my-orders/` 只回 `打单工资`（`get_queryset` 的 `emp_type=EmpType.MAIN`），
        所以 HTTP 那一层永远看不到 `销售提成`。这条钉在**序列化器**上，
        给「把 `emp_type` 写成常量 `"打单工资"`」的变异提供判别力——
        否则 HTTP 层「永远是打单工资」会让常量实现也全绿。
        """
        self.assertEqual(self.sales_c.emp_type, EmpType.SALES)    # 自证前提：这行确实是销售提成
        data = MyOrderSerializer(self.sales_c).data
        self.assertEqual(data["emp_type"], "销售提成")

    def test_null_user_row_serializes_username_as_null(self):
        """⚠️ `Performance.user` 是 `null=True` 的 FK（还没归属人的行）⇒ 嵌套 source 要 `allow_null=True`。

        **实测**（DRF 3.18.1；用两个只差 `allow_null` 的序列化器各序列化一条 `user_id=None` 的
        `Performance`，探针跑完即删，原始输出抄在下面；做法见 AGENTS §5「先读源码」）：
        - 无 `allow_null`：`data = {"id": 4242}`——**不是 500**，而是该字段被整个跳过
          （`read_only` ⇒ `required=False`；空关系的 `AttributeError` 落到 `Field.get_attribute`
          里 `raise SkipField()` 的分支；`Serializer.to_representation` 捕获 `SkipField` 后 `continue`）
          ⇒ `username` 这个**键从载荷里静默消失**；
        - `allow_null=True`：`data = {"id": 4242, "username": null}`——`Field.get_attribute`
          的 `if self.allow_null: return None` 分支回 `None`，`Serializer.to_representation`
          的「值为 `None` 就写 `null`」分支输出 `null`（`CharField.to_representation` 是
          `str(value)`，所以 `None` 不能只靠字段自己兜）。

        这条钉**序列化器层**：HTTP 那一层到不了空 user 的行（下一条用例给证据）。
        """
        row = self._perf(NO_UNASSIGNED, None, Decimal("3.00"))
        self.assertIsNone(row.user_id)                          # 前置：真的没有归属人
        data = MyOrderSerializer(row).data
        self.assertIn("username", data)                         # 键在（没有被 SkipField 丢掉）
        self.assertIsNone(data["username"])
        # ⭐ 接手人同理 —— 空 user ⇒ `null`，**不是**丢键、也不是空串
        # （页面把 `null` 渲染成空串；丢键会让那一列恒空而看不出是"没人接"还是"字段没回"）。
        self.assertIn("taker_name", data)
        self.assertIsNone(data["taker_name"])

    def test_null_user_row_is_not_reachable_through_the_viewset(self):
        """空 user 的行**到不了** HTTP 载荷（`get_queryset` 的 `user=request.user`），
        它在库里也不会把请求打成 500——这是上一条为什么钉在序列化器上的证据。

        同一次请求里三条同时断：该行**在**库里、响应 `code == 0`、它**不在** `items` 里。
        """
        null_row = self._perf(NO_UNASSIGNED, None, Decimal("3.00"))
        body = self._body()
        self.assertEqual(body["code"], 0)
        nos = [r["order_no"] for r in body["data"]["items"]]
        self.assertNotIn(NO_UNASSIGNED, nos)
        self.assertEqual(nos, [NO_A])
        # 自证前提：那行真的落库了（否则「不在载荷里」可能只是没建成）。
        self.assertTrue(
            Performance.objects.filter(pk=null_row.pk, user__isnull=True).exists()
        )

    def test_urgent_order_is_reported_as_true_in_the_response_body(self):
        """订单加急 ⇒ **HTTP 响应体**里 `is_urgent` 是 `True`。

        断言打在**响应体**上（不是去调序列化器 / 看实例属性）：只有真实出口才能证明
        跨表字段（`source="order.is_urgent"`）真的接上了。夹具的订单本来不加急
        （上一条用例已钉 `False`），这里改真值再取一次 ⇒ 两个取值都被断过，
        常量（恒 `True` 或恒 `False`）都过不了这一对。
        """
        self.assertIs(self._items()[0]["is_urgent"], False)          # 改之前：不加急
        order = self.perf_a.order
        order.is_urgent = True
        order.save(update_fields=["is_urgent"])
        row = next(r for r in self._items() if r["order_no"] == NO_A)
        self.assertIs(row["is_urgent"], True)

    def test_pagination_shape_defaults_to_twenty(self):
        body = self._body()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(set(data), {"items", "total", "page", "page_size"})
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 20)

    def test_malformed_pagination_params_are_not_500(self):
        """畸形分页参数 ⇒ **200 + 业务码**，不是 HTML 500（E-0010）。

        ⚠️ 期望值是**实测**出来的（当时用一次性探针跑出，探针跑完即删）：
        - `?page=abc` / `?page=0` ⇒ DRF 分页器抛 `NotFound`，异常处理器映射成 **1002**；
        - `?page_size=abc` / `?page_size=0` ⇒ **不报错**，回落到默认 `20`（DRF 的
          `_positive_int(..., strict=True)` 用 `try/except` 吞掉非法值），所以 code 是 **0**。
        断言只断 `code`，不钉 `msg` 文案。

        ⚠️ **期望值写字面值**（`1002` / `0`），**不引用 `ErrorCode`**（AGENTS §5 / E-0024）：
        `assertEqual(f(x), ErrorCode.BUSINESS)` 在枚举被改坏时**不会红**——被断言的东西与
        决定结果的东西是同一个符号（自指断言）。同批的 `test_notification.py` 也用字面值
        （`1002`），这里与它对齐（B2 整体评审 FIX 5）。
        """
        self.assertEqual(self._body({"page": "abc"})["code"], 1002)
        self.assertEqual(self._body({"page": "0"})["code"], 1002)
        body = self._body({"page_size": "abc"})
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["page_size"], 20)
        body = self._body({"page_size": "0"})
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["page_size"], 20)

    # ── 查询数不随行数增长（`select_related("user")` 的判别力）──────
    def test_query_count_does_not_grow_with_the_number_of_rows(self):
        """⚠️ 载荷新读的 `user.username` 必须走 `select_related`：**查询数不随行数增长**。

        判据不用绝对条数（会被会话 / 租户中间件等无关改动改烂），而是**两个请求相等**：
        同一个用户 1 行 vs 4 行，两次 `GET` 的 SQL 条数必须一样。
        少了 `select_related("user")` 时，多出来的 3 行各自回查同一个 `request.user` ⇒
        实测 1 行 **14** 条 / 4 行 **20** 条（差 6）。⚠️ 差值是**每多一行 2 条**、不是 1 条：
        一条 `SELECT auth_user`（N+1 本身）＋ django-tenants 给**每条语句**发的
        `SET search_path`（逐条打印捕获到的 SQL 核实：多出的正是这两条）。

        ⚠️ 这条用例真正钉住的**只是**「`count(1 行) == count(4 行)`」这一个等式，
        不是「除行数外没有任何按行增长的查询」：任何让第二次比第一次多（或少）一条 SQL 的改动
        都会让它红，包括「只在 rows > 1 时才发」那类查询。断**相等**而不是某个绝对数字，
        是为了让无关的查询增减（多一次 session、多一次 count）在两边**同时**出现而不误报。
        """
        with CaptureQueriesContext(connection) as one_row:
            body = self._body()
        self.assertEqual(body["code"], 0)
        self.assertEqual(len(body["data"]["items"]), 1)      # 非空哨兵：真的是 1 行

        for i in range(3):                                    # 同一用户再加 3 行 ⇒ 共 4 行
            self._perf(f"2026092000000000020{i}", self.taker_a, Decimal("10.00"))
        with CaptureQueriesContext(connection) as four_rows:
            body = self._body()
        self.assertEqual(body["code"], 0)
        self.assertEqual(len(body["data"]["items"]), 4)      # 非空哨兵：真的是 4 行

        self.assertEqual(
            len(one_row.captured_queries),
            len(four_rows.captured_queries),
            f"列表端点的查询数随行数增长了：1 行 {len(one_row.captured_queries)} 条 / "
            f"4 行 {len(four_rows.captured_queries)} 条"
            "（不含修复时每多一行多 2 条：一条 `SELECT auth_user` + 它的 `SET search_path`）"
            "——`select_related` 没覆盖序列化器读的跨表字段",
        )

    # ── 数据权限接线（判别力看变异）──────────────────────────────
    def test_scoping_seam_is_actually_used(self):
        """桩打在**返回值**上：`scope_queryset` 被换成「清空」，列表必须真的变空。

        只断言「被调用」不够——视图完全可以调用它却丢掉返回值。
        配一条不接桩的对照，排除「空集来自别的条件」。
        """
        self.assertEqual(self._nos(), [NO_A])                        # 对照：本来看得见一条
        with mock.patch(
            "apps.business.api.my_orders_views.scope_queryset",
            side_effect=lambda user, qs: qs.none(),
        ) as seam:
            self.assertEqual(self._nos(), [])
        self.assertTrue(seam.called, "我的订单没有经过 scope_queryset 卡点")

    # 跨租户红线**不在本文件**（brief §4）：那是 B2-3 的范围
    # （`tests/test_p3b_isolation.py`）。本文件只测租户内的可见性。
