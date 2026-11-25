"""订单域按游戏权限收窄（计划 `2026-09-23-scope-by-game-permission.md` 的 Task 2）。

**本文件钉的是「接线」**——规则本体（`game_scope` / `assert_game_allowed`）的口径由
`tests/test_game_scope.py` 钉住；这里钉的是**订单域真的把那两个函数接上了**：
读路径（订单列表 / 详情 / 大厅 / 退款列表 / 发起退款）过
`scope_queryset(..., scope_filter=game_scope(...))`，写路径（录单 / 改单 / 复制）的
`game` 过 `assert_game_allowed`。

⚠️ **期望值一律写字面值**（E-0024）：订单号写 `"20260923000000000101"` 这类字面量，
业务码写 `1000` / `1002`，游戏名写 `"原神"`——不 import 被测模块导出的常量。

⚠️ **否定断言用租户内唯一的订单号，不用主键**（`AGENTS.md` §5 硬规则）：
两个 schema 的主键序列各自从 1 开始，「用主键指认别人的那一行」在跨租户时会命中**自己的行**。
本文件还一律配一条**前置哨兵**（「那张单真的在库里」），否则「被收窄掉」与「压根没造出来」
不可区分。

⚠️ **两个口径相反的做法是有依据的**（计划点名要求说明）：
`order.restrict_to_self_created` **刻意不进 `get_queryset()`**（它的失败形态是"少行"，
见 `order_views.py` 的注释），而本线的 `game_scope` **进**了
`get_queryset()` ⇒ `retrieve` 对「别人游戏的单」是 **200 + `1002`**。
两条的差别在于**「这行对他可不可见」是不是他应当知道的信息**：
按游戏权限收窄的是**可见范围本身**（他确实无权看这一行 ⇒ 不可见是**期望形态**）；
`restrict_to_self_created` 是**对可见范围之内的一行施加的额外限制**（他有权看那一行、
只是 flag 不让他看）⇒ 那种情况下 404 会把「被过滤」伪装成「不存在」。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderRefund,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

ORDERS_URL = "/api/business/orders/"
HALL_URL = "/api/business/hall/"
REFUNDS_URL = "/api/business/orders/{order_no}/refunds"
COPY_URL = "/api/business/orders/{order_no}/copy"

#: 本文件实际走到的门（`OrderViewSet.ORDER_PERMISSIONS` + `RefundView` 的节点）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
NODES = (
    "order.list",
    "order.view_detail",
    "order.add_edit",
    "grab_hall.take_order",
    "order.online_refund",
)

#: 租户内唯一的订单号（字面量，20 位）。A/B 分属两款游戏。
ORDER_A_NO = "20260923000000000101"
ORDER_B_NO = "20260923000000000102"
HALL_A_NO = "20260923000000000103"
HALL_B_NO = "20260923000000000104"

A_ONLY_USERNAME = "scope-a-only"
ADMIN_USERNAME = "scope-admin"
#: 没有档案的**非管理员**（写侧豁免的反面）。与 `A_ONLY_USERNAME` 的差别只有一条：
#: 有没有 `StaffProfile`。
NO_PROFILE_USERNAME = "scope-no-profile"
#: **有**档案、但一款游戏都没勾（`games=[]`）。读侧的零游戏钉子用它——与
#: `A_ONLY_USERNAME` 的 groups / nodes 逐字相同，唯一差别是「勾了 A」与「一款没勾」。
ZERO_GAMES_USERNAME = "scope-zero-games"


class OrderScopeTests(TenantTestCase):
    """两款游戏 + 各两张单（一张已完成、一张待抢单）+ 两个账号（只勾 A / 租户管理员）。"""

    def setUp(self):
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="崩铁")
        # 已完成：详情 / 列表 / 退款三条路径用得上（退款前置要求订单已完成）。
        self.order_a = self._order(ORDER_A_NO, self.game_a, status=OrderStatus.COMPLETED)
        self.order_b = self._order(ORDER_B_NO, self.game_b, status=OrderStatus.COMPLETED)
        # 大厅：池内状态 + 一条 `user` 为空的 MAIN 绩效（D-0032 的不变量）。
        self.hall_a = self._hall_order(HALL_A_NO, self.game_a)
        self.hall_b = self._hall_order(HALL_B_NO, self.game_b)

        # ① 只勾 A 游戏的账号：真实登录（fail-closed，不能用 `force_login`）。
        # ⚠️ `games=None` 是本用例的**显式 opt-out**（夹具默认翻面）：本用例的
        #    语义是「这个账号**只**勾了 A」，属部分权限。夹具必须**不建档案**（下面马上
        #    自己 `StaffProfile.objects.create(...)`，两边都建会撞唯一约束），也不预先
        #    授权 ⇒ 下面那句 `games.set([self.game_a])` 是唯一的权限来源（`set` 不是并集）。
        self.client = make_logged_in_client(
            username=A_ONLY_USERNAME, groups=("客服", "代练员"), nodes=NODES, games=None
        )
        self.a_only = User.objects.get(username=A_ONLY_USERNAME)
        profile = StaffProfile.objects.create(user=self.a_only)
        profile.games.set([self.game_a])

        # ② 租户管理员（`is_superuser` + 夹具顺手挂的「系统管理员」职位）：
        #    `scope_queryset` 对管理员短路 ⇒ 读路径不该被收窄。⚠️ 该账号**没有档案**
        #    （`games=None` ⇒ 夹具**不建** `StaffProfile`）——正是
        #    `apps/platform/provisioning.py::seed_tenant` 供给出来的那个租户 `admin`
        #    的形状，写侧的管理员豁免就是为它而设（见下面的用例；本文件有一条
        #    `StaffProfile.objects.filter(user=self.admin_user).count() == 0` 的构造物断言依赖它）。
        self.admin = make_logged_in_client(
            username=ADMIN_USERNAME, is_superuser=True, games=None
        )
        self.admin_user = User.objects.get(username=ADMIN_USERNAME)

        # ③ 没有档案的**非管理员**（反面）：写侧的豁免只对管理员生效。
        #    `games=None` 是本用例要的**形状本身**（下面有一条 count()==0 的构造物断言）。
        self.no_profile = make_logged_in_client(
            username=NO_PROFILE_USERNAME, groups=("客服",), nodes=NODES, games=None
        )
        self.no_profile_user = User.objects.get(username=NO_PROFILE_USERNAME)

        # ④ **有档案但零游戏**（`games=[]`）：严格口径在订单**读侧**的钉子。
        #    与 ① 的 `A_ONLY_USERNAME` 成对：groups / nodes 逐字相同，唯一差别是游戏数
        #    （A 一只 / 零只）⇒ 否则「零游戏什么都看不到」会被别的差异解释掉。
        self.zero_games = make_logged_in_client(
            username=ZERO_GAMES_USERNAME, groups=("客服", "代练员"), nodes=NODES, games=[]
        )
        self.zero_games_user = User.objects.get(username=ZERO_GAMES_USERNAME)

    # ── 小夹具 ────────────────────────────────────────────────────────
    def _order(self, order_no, game, *, status=OrderStatus.UNASSIGNED):
        # 毛收款 / 净实收只读 `total_price`。`received_price` 写成陷阱值 `1.00`：
        #    若实现退回旧口径，退款上限会读成 `1.00`（而不是 `100.00`），
        #    `test_partial_refund_accepts_my_own_order` 的 10.00 退款当场被 `1008` 拒 ⇒ 本行红。
        return Order.objects.create(
            game=game,
            order_no=order_no,
            status=status,
            received_price=Decimal("1.00"),
            total_price=Decimal("100.00"),
            project_text="占位项目",
        )

    def _hall_order(self, order_no, game):
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
            project_text="占位项目",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _list_order_nos(self, client):
        body = client.get(ORDERS_URL).json()
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"] for row in body["data"]["items"]}

    # ── ① 订单列表 ────────────────────────────────────────────────────
    def test_order_list_keeps_only_the_games_i_hold(self):
        """只有 A 权限 ⇒ 只看得到 A 的两张单；B 的两张**不在结果里**。

        判别力：把 `get_queryset()` 的 `scope_filter` 抽掉 ⇒ B 的两张一起出现，本用例变红。
        """
        nos = self._list_order_nos(self.client)
        self.assertEqual(nos, {ORDER_A_NO, HALL_A_NO})
        # 前置哨兵：B 的两张单**真的在库里**（不是"没造出来"）。
        self.assertEqual(
            Order.objects.filter(order_no__in=[ORDER_B_NO, HALL_B_NO]).count(), 2
        )
        self.assertNotIn(ORDER_B_NO, nos)
        self.assertNotIn(HALL_B_NO, nos)

    def test_order_list_shows_every_game_to_a_tenant_admin(self):
        """管理员旁路在 `scope_queryset` 这个卡点里 ⇒ 四张都看得到。"""
        self.assertEqual(
            self._list_order_nos(self.admin),
            {ORDER_A_NO, ORDER_B_NO, HALL_A_NO, HALL_B_NO},
        )

    def test_order_list_and_detail_hide_everything_from_a_user_with_zero_games(self):
        """零游戏（**有**档案、一款没勾）⇒ 订单列表空 + 详情看起来不存在（`1002`）。

        这是计划 Task 1 Step 5 点名的订单域钉子（「零游戏 ⇒ 什么都看不到」，绩效 /
        结算 / 统计各自已有，订单域此前只有「部分授权 ⇒ 看不到别的游戏」）：
        `__in=[]` 在 Django 里是**空结果**、不是「不过滤」，所以「看得到自己那一款」
        证明不了它——必须有「空游戏列表的人一张都看不到」的反面。

        判别力可以用两种变异证明（措辞修正，收口评审 C-3）：
        ① **窄谱**变异（经典缺陷形态「空游戏列表 ⇒ 不过滤」：`_filter` 里 `games_of` 为空时
        直接 `return queryset`）⇒ 在**本文件 18 条**里**只**红本条（实测 `Ran 18 / failures=1`）。
        ⚠️ `tests/test_game_scope.py` 里那条同型零游戏钉子也会一起红——同族钉子，不改变
        「本条自己被这条变异杀死」的归因；
        ② **宽谱**变异（`game_scope._filter` 整体弄失效、直接 `return queryset`）⇒ 会连带红
        **6 条**既有用例，所以它**不能**单独归因给本条。
        """
        # 构造物 1：档案在、游戏**恰好 0**（与 `self.a_only` 的唯一差别就是这里）。
        self.assertEqual(
            StaffProfile.objects.get(user=self.zero_games_user).games.count(), 0
        )
        # 构造物 2：库里**确实**有四张单（否则「看不到」与「没造出来」不可区分）。
        self.assertEqual(Order.objects.count(), 4)

        self.assertEqual(self._list_order_nos(self.zero_games), set())
        # 明细走 `get_object()` 的同一条 queryset ⇒ 对无权者与"真的不存在"同形。
        body = self.zero_games.get(f"{ORDERS_URL}{ORDER_A_NO}/").json()
        self.assertEqual(body["code"], 1002)
        self.assertIsNone(body["data"])

    # ── ② 订单详情（同时证明「不是只收窄列表」）────────────────────────
    def test_order_detail_of_another_game_looks_nonexistent(self):
        """B 游戏那张单对只勾 A 的人**不可见** ⇒ 走 `get_object()` 的同一条 queryset。

        失败形态是 **HTTP 200 + 业务码 `1002`**（`apps/core/exceptions.py` 把 DRF 的
        `NotFound` 归一到「记录不存在或已被删除」，不透传 404）——与"这单真的不存在"
        在此**不可区分**，而按游戏权限收窄时那正是期望形态（他确实无权看这一行）。
        """
        response = self.client.get(f"{ORDERS_URL}{ORDER_B_NO}/")
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 1002)
        self.assertIsNone(body["data"])
        # 前置哨兵：这张单真的在库里。
        self.assertEqual(Order.objects.filter(order_no=ORDER_B_NO).count(), 1)

    def test_order_detail_of_my_own_game_is_visible(self):
        body = self.client.get(f"{ORDERS_URL}{ORDER_A_NO}/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["order_no"], ORDER_A_NO)

    # ── ③ 大厅 ────────────────────────────────────────────────────────
    def test_hall_hides_orders_of_another_game(self):
        """只勾 A 的代练员 ⇒ 大厅里只有 A 的池内单，B 的不在。"""
        body = self.client.get(HALL_URL).json()
        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：两张池内单真的都在大厅的入选条件里（状态 + 无接单人）。
        self.assertEqual(
            Order.objects.filter(status=OrderStatus.WAITING_GRAB).count(), 2
        )
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertEqual(nos, [HALL_A_NO])
        self.assertNotIn(HALL_B_NO, nos)

    # ── ④ 退款：列表 + 发起退款 ───────────────────────────────────────
    def test_refund_list_hides_refunds_of_another_game(self):
        """退款记录按**订单的游戏**收窄（`OrderRefund` 自己只有 `order` FK）。"""
        OrderRefund.objects.create(
            order=self.order_b,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("10.00"),
            reason="B 的退款",
            request_id="scope-req-b",
        )
        OrderRefund.objects.create(
            order=self.order_a,
            kind=OrderRefund.Kind.PARTIAL,
            amount=Decimal("20.00"),
            reason="A 的退款",
            request_id="scope-req-a",
        )
        # 前置哨兵：两条退款行真的在库里。
        self.assertEqual(OrderRefund.objects.count(), 2)

        hidden = self.client.get(REFUNDS_URL.format(order_no=ORDER_B_NO)).json()
        self.assertEqual(hidden["code"], 0, hidden)
        self.assertEqual(hidden["data"]["items"], [])
        # 反面（正例）：自己游戏那张单的退款记录看得到。
        visible = self.client.get(REFUNDS_URL.format(order_no=ORDER_A_NO)).json()
        self.assertEqual(
            [row["request_id"] for row in visible["data"]["items"]], ["scope-req-a"]
        )

    def test_partial_refund_rejects_an_order_of_another_game(self):
        """对 B 的单发起退款 ⇒ `1000`，且**库里一行都没多**（E-0009：断言数据库）。"""
        before = OrderRefund.objects.filter(order=self.order_b).count()
        body = self.client.post(
            REFUNDS_URL.format(order_no=ORDER_B_NO),
            {"refund_total": "10.00", "reason": "越权退款", "request_id": "scope-refund-b"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIsNone(body["data"])
        self.assertEqual(OrderRefund.objects.filter(order=self.order_b).count(), before)
        self.order_b.refresh_from_db()
        self.assertEqual(self.order_b.refund_fee, Decimal("0.00"))

    def test_partial_refund_accepts_my_own_order(self):
        """反例的另一半：对自己游戏的单退款**成功**（不是"一律拒绝"）。"""
        body = self.client.post(
            REFUNDS_URL.format(order_no=ORDER_A_NO),
            {"refund_total": "10.00", "reason": "自己的单", "request_id": "scope-refund-a"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OrderRefund.objects.filter(order=self.order_a).count(), 1)

    # ── ⑤ 写侧：只能选自己有权限的游戏 ────────────────────────────────
    def test_create_rejects_a_game_i_have_no_permission_for(self):
        """录一张 B 游戏的单 ⇒ `1000` + **库里没有新订单**（E-0009）。"""
        before = Order.objects.count()
        self.assertEqual(before, 4)  # 前置哨兵：夹具确实造了四张
        body = self.client.post(
            ORDERS_URL,
            {"game": self.game_b.id, "project_text": "越权新单"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIsNone(body["data"])
        self.assertEqual(Order.objects.count(), before)
        self.assertEqual(Order.objects.filter(project_text="越权新单").count(), 0)

    def test_create_accepts_a_game_i_hold(self):
        """⑥ 反例：录一张 A 游戏的单 ⇒ **成功**（证明校验不是"一律拒绝"）。"""
        before = Order.objects.count()
        body = self.client.post(
            ORDERS_URL,
            {"game": self.game_a.id, "project_text": "自己的新单"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.count(), before + 1)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.game.name, "原神")

    def test_update_rejects_switching_to_a_game_i_have_no_permission_for(self):
        """改单换成 B 游戏 ⇒ `1000`，且那张单**还挂在 A 游戏上**。

        ⚠️ 必须把 `server` / `category` / `project` 一起显式清空：只传 `game` 的话，
        `validate_order_chain` 会拿**旧单的 A 游戏区服**去比 B 游戏 ⇒ 先以「所选区服不属于
        该游戏」（同为 `1000`）拒掉，本用例就测不到权限那道判据了（那样抽掉权限校验它
        仍然绿 ⇒ 判据失去判别力）。
        """
        body = self.client.patch(
            f"{ORDERS_URL}{ORDER_A_NO}/",
            {
                "game": self.game_b.id,
                "server": None,
                "category": None,
                "project": None,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=ORDER_A_NO).game.name, "原神")

    def test_copy_of_another_games_order_is_invisible_to_me(self):
        """复制走得也是 `get_object()` 的同一条 queryset ⇒ B 的单对我只勾 A 时不可见。"""
        before = Order.objects.count()
        body = self.client.post(
            COPY_URL.format(order_no=ORDER_B_NO), {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1002)
        self.assertIsNone(body["data"])
        self.assertEqual(Order.objects.count(), before)

    def test_copy_of_another_games_order_is_allowed_for_a_tenant_admin(self):
        """管理员「能看见那张单、却不持有它的游戏」⇒ 复制**照样成功**。

        ⛔ **本条的期望值在本任务里改过（原文按本仓惯例保留）**：原文叫
        `test_copy_rejects_a_game_i_have_no_permission_for`，断言的是管理员复制 B 的单
        拿到 **`1000`**——它钉的是 Task 1 交付的**接口不对称**（读侧 `scope_queryset`
        有管理员旁路、写侧 `assert_game_allowed` 只认 `games_of`）。该不对称正是本任务
        （执行期修订 2）判定为**必须修**的缺陷：供给出来的租户管理员
        **按构造没有 `StaffProfile`** ⇒ 读得到全部订单、却录单/改单/复制全被 `1000` 拒，
        与计划 DoD #2「管理员不受影响」直接矛盾。新契约 = 写侧复用**同一个**判据
        `is_tenant_admin`（与读侧短路同一个函数）⇒ 管理员不受影响，所以这里改成
        「复制成功 + 库里真的多一行，且新单继承原单的游戏」。
        写侧的**拒绝面**仍由下面两条钉住（非管理员、有档案、只勾 A）：
        `test_create_rejects_a_game_i_have_no_permission_for` 与
        `test_update_rejects_switching_to_a_game_i_have_no_permission_for`。
        """
        before = Order.objects.count()
        self.assertEqual(before, 4)  # 前置哨兵
        body = self.admin.post(
            COPY_URL.format(order_no=ORDER_B_NO), {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.count(), before + 1)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.game.name, "崩铁")  # 字面游戏名（E-0024）

    def test_copy_accepts_my_own_order(self):
        """反例：复制自己游戏的那张单 ⇒ **成功**（新单恒 `UNASSIGNED`，spec §8.2）。"""
        before = Order.objects.count()
        body = self.client.post(
            COPY_URL.format(order_no=ORDER_A_NO), {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.count(), before + 1)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.game.name, "原神")
        self.assertEqual(created.status, "UNASSIGNED")

    # ── ⑥ 写侧的管理员豁免（执行期修订 2：与读侧同一个判据 `is_tenant_admin`）──
    def test_a_tenant_admin_without_a_profile_can_create_an_order_of_any_game(self):
        """供给出来的租户管理员**没有档案** ⇒ 录单仍然成功（判据是职位，不是档案）。

        判别力（**收口评审 A-1 之后重核**）：现在 `games_of` **也有**同一条管理员短路
        ⇒ 单独删掉 `assert_game_allowed` 开头的早返回**不再变红**（两处互为纵深，
        实测见本线「执行期修订 3」）；要让本用例红，必须**同时**删掉 `games_of` 的
        管理员短路与这里的早返回（那时缺档案 ⇒ `games_of(admin)` 空集 ⇒ `1000`）。
        ⛔ 没有为了维持旧措辞而改判据。
        """
        self.assertEqual(StaffProfile.objects.filter(user=self.admin_user).count(), 0)  # 构造物
        before = Order.objects.count()
        body = self.admin.post(
            ORDERS_URL,
            {"game": self.game_b.id, "project_text": "管理员的新单"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.count(), before + 1)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.game.name, "崩铁")  # 字面游戏名（E-0024）
        self.assertEqual(created.project_text, "管理员的新单")

    def test_a_tenant_admin_without_a_profile_can_switch_an_order_to_another_game(self):
        """改单同理：把 A 的单改成 B ⇒ 成功，且**库里的游戏真的变了**。"""
        self.assertEqual(StaffProfile.objects.filter(user=self.admin_user).count(), 0)  # 构造物
        body = self.admin.patch(
            f"{ORDERS_URL}{ORDER_A_NO}/",
            {"game": self.game_b.id, "server": None, "category": None, "project": None},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.get(order_no=ORDER_A_NO).game.name, "崩铁")

    def test_a_non_admin_without_a_profile_is_still_rejected_on_write(self):
        """反面：**同一个「没有档案」的形状，非管理员仍然 `1000`** ⇒ 豁免只对管理员。

        与 `test_a_tenant_admin_without_a_profile_can_create_an_order_of_any_game` 成对：
        两条的**唯一差别**是「持不持『系统管理员』职位」（`self.no_profile_user` 与
        `self.admin_user` 都没有 `StaffProfile`）——否则「豁免只对管理员」证明不了。
        """
        self.assertEqual(StaffProfile.objects.filter(user=self.no_profile_user).count(), 0)
        before = Order.objects.count()
        body = self.no_profile.post(
            ORDERS_URL,
            {"game": self.game_a.id, "project_text": "没有档案的新单"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIsNone(body["data"])
        self.assertEqual(Order.objects.count(), before)
        self.assertEqual(Order.objects.filter(project_text="没有档案的新单").count(), 0)
