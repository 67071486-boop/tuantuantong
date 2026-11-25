"""身份适配层、录单建绩效、/transition/ 收紧。"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from tests.p5_auth_helpers import grant_games, make_logged_in_client   # 真实登录夹具（fail-closed 后必须用它，不能用 force_login）

from apps.business.models import (
    EmpType, Game, Order, OrderStatus, Performance, ReviewStatus,
)
from apps.business.performance import (
    create_performance_for_order as real_create_performance_for_order,
)
from apps.rbac.permissions import nodes_of
from apps.rbac.seed import seed_builtin_roles, seed_catalog

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
ROLES_ORDER_NODES = (
    "order.add_edit",                # 录单
    "order.list",
    "order.view_detail",             # 详情（`allowed_transitions` 读它）
    "order.change_status",           # `/transition/`
    # ⭐ 计划 ④：`/transition/` 的**待抢单**目标已改判独占 36 ⇒ 本文件
    #    有 `test_cs_can_throw_to_hall`（名字带 throw、实际打 `/transition/`）等用例要它。
    #    **这是补前提、不是放宽判据**：门本身一字未动。
    "order.send_to_grab_hall",
    "order.submit_payout_price",     # `/price/` 与 `/assign/` 带价
    "order.edit_payout_price",
    "order.assign_taker",            # `/assign/`
    "grab_hall.take_order",          # `/claim/`
    "order.clear_taker",             # `/clear-taker/`
)


APPROVAL_NODES = frozenset({
    "home.approval_apply",
    "home.approval_penalty_approve",
    "home.approval_refund_handle",
    "home.approval_configure",
})


class NodesOfTests(TenantTestCase):
    """`nodes_of` 是判据出口——不经 HTTP 直接测（2026-09-23 单轴化：取代 `roles_of` 测试）。

    期望值**不 import 生产常量**（E-0024）：内置职位的**判别性**节点写字符串字面量；
    「系统管理员」的基数是字面量：租户管理员短路拿到全目录，不是种子批发的那一份。
    目录增减这里会红。
    """

    def setUp(self):
        seed_catalog()
        seed_builtin_roles()

    def _user(self, *group_names, **kw):
        u = User.objects.create_user(username=kw.get("username", "u1"))
        for name in group_names:
            g, _ = Group.objects.get_or_create(name=name)
            u.groups.add(g)
        return u

    def test_cs_group_holds_the_order_nodes(self):
        """客服 = `order` + `my_*` + `grab_hall` + `home` 栏目，**精确 66** 个非 flag 点。

        ⚠️ **修复轮 1（复审）**：旧版只断一个 2 元子集 + 一条 `assertNotIn`，比原来的
        「角色集精确相等」弱。这里补**精确基数**（字面量，独立于生产常量；含**栏目行**
        本身，因为种子的模块批发把父节点也给了——`PermissionNode` 的栏目行不是 flag）——
        子集断言挡不住「多给了别的栏目」，基数把它变成可判红的回归；
        再加一条负向（不含 `system_user` 栏目，那是客服主管多出来的）。
        **不抄角色节点全集**：那会把某个职位的目录快照钉死在测试里。
        ⚠️ **本用例自己的边界**（别把它的绿灯读大）：它对客服 / 客服主管只钉**基数**与
        **判别性子集/负向**，**不钉逐行全集**——`test_rbac_seed` 对客服只断 `>0` / `<150`，
        对客服主管的期望值由生产 `NODES` 派生，`test_rbac_structure` 也不是逐行快照
        ⇒ **逐行目录口径没有被任何一条测试完整钉住**，这是已知残差；
        在这里再抄一份全集只会在目录演进时制造两处要改的地方——**这条取舍写进报告**。
        基数写字面量。客服栏目里多一个非 flag 点，这里就红。
        """
        held = nodes_of(self._user("客服"))
        self.assertEqual(len(held), 66)
        self.assertEqual(held & APPROVAL_NODES, {"home.approval_apply"})
        self.assertTrue({"order", "order.change_status", "order.change_status_completed"} <= held)
        self.assertNotIn("finance.payroll", held)
        self.assertNotIn("system_user.list", held)

    def test_each_default_group_maps(self):
        """内置职位的节点集：**小集合写精确字面量全集**，大集合见下面两条。

        ⚠️ 偏离 brief（**必做**，报告 §偏离 9）：brief 这里三次都写 `self._user("代练员")`
            这种形式，而 `_user` 的默认 username 是 `"u1"` —— 同一条测试里创建三个
            `u1` 会直接撞 `auth_user_username_key`，那条测试**永远不可能绿**。
        ⚠️ **2026-09-23 换轴 + 修复轮 1**：旧断言钉的是角色集（`代练员 → {TAKER}` 等），
        是**精确相等**；换轴后我先写成「子集 + 不含某几个」，复审指出那是**削弱**。
        代练员的集合只有 22 个（含 `grab_hall` / `home` / `my_*` 五个**栏目行**）
        ⇒ 恢复**精确字面量全集**。
        ⭐ 22 → **23**（D-0262 在 `home` 栏目新增非 flag 点 `home.qr_manage`
        ——代练员的模块批发含 `home` 栏目 ⇒ 字面量全集同批补这个 code）。
        ⭐ 业务方当场拍板）：23 → **25** —— seed 给代练员补两个「订单业务」单点
        `order.view_screenshot` / `order.upload_screenshot`（打手 App 要能看 / 传进度截图）。
        真值由本网自己报出（先跑一次，它报 `AssertionError: 25 != 23`），⛔ 不按「23 + 2」推算。
        """
        self.assertEqual(
            nodes_of(self._user("代练员", username="taker1")),
            frozenset({
                "grab_hall",
                "grab_hall.take_order",
                "home",
                "home.blacklist_board",
                "home.blacklist_report",
                "home.change_password",
                "home.enter",
                "home.feedback",
                "home.index",
                "home.order_ranking",
                "home.qr_manage",
                "home.system_circle",
                "home.system_circle_publish",
                "my_orders",
                "my_orders.export",
                "my_orders.import_to_my_system",
                "my_orders.send_to_my_system",
                "my_orders.set_seat",
                "my_performance",
                "my_performance.export",
                "my_profile",
                "my_profile.edit",
                "order.submit_work",
                "order.view_screenshot",
                "order.upload_screenshot",
            }),
        )

    def test_supervisor_group_holds_the_order_and_system_user_nodes(self):
        """客服主管 = **七个**栏目（`order` / `system_user` / `my_orders` / `my_performance` /
        `my_profile` / `grab_hall` / `home`，见 `apps/rbac/seed.py` 的
        `BUILTIN_ROLE_MODULES`），**精确 81** 个非 flag 点。

        ⚠️ **修复轮 1**：与客服同理，补精确基数与判别性正/负向，替掉原先的弱子集断言。
        基数写字面量。栏目里多一个非 flag 点，这里就红。
        """
        held = nodes_of(self._user("客服主管", username="reviewer1"))
        self.assertEqual(len(held), 81)
        self.assertEqual(held & APPROVAL_NODES, {"home.approval_apply"})
        self.assertTrue({"order", "order.change_status", "system_user", "system_user.list"} <= held)
        self.assertNotIn("finance.payroll", held)

    def test_system_admin_group_holds_the_whole_catalog(self):
        """`系统管理员` → 租户管理员的**全目录短路**（`nodes_of` 经 `is_tenant_admin`）。

        持这个职位走管理员短路，拿到全目录。条数写字面量，不引用 `ALL_CODES`。
        种子真正批发的那一份由 `test_rbac_seed` 单独钉。
        """
        held = nodes_of(self._user("系统管理员"))
        self.assertEqual(len(held), 170)
        self.assertTrue(APPROVAL_NODES <= held)
        # （`customer.export_fund_details` 已随客户维护组收敛裁掉，2026-11-11 ⇒ 换存活点。）
        self.assertTrue({"order", "customer.member_list_self_only"} <= held)

    def test_developer_group_holds_my_orders_and_my_performance_only(self):
        """`系统开发员` 的节点集 = 我的订单 / 我的绩效 / 我的资料 / 主页（精确 19 个字面量）。

        ⚠️ **修复轮 1（复审）**：旧版是「子集 + 两条 `assertNotIn`」，比原来的精确相等弱；
        这里恢复**精确字面量全集**（19 个，含四个**栏目行**，规模可接受）。它**不含**
        抢单厅（`grab_hall.take_order`）与交单点（`order.submit_work`）——
        与种子 `BUILTIN_ROLE_MODULES` 一致（spec §4.4 的「系统开发员」行）。
        ⭐ 19 → **20**（D-0262 在 `home` 栏目新增非 flag 点 `home.qr_manage`
        ——系统开发员的模块批发含 `home` 栏目 ⇒ 字面量全集同批补这个 code）。
        """
        self.assertEqual(
            nodes_of(self._user("系统开发员")),
            frozenset({
                "home",
                "home.blacklist_board",
                "home.blacklist_report",
                "home.change_password",
                "home.enter",
                "home.feedback",
                "home.index",
                "home.order_ranking",
                "home.qr_manage",
                "home.system_circle",
                "home.system_circle_publish",
                "my_orders",
                "my_orders.export",
                "my_orders.import_to_my_system",
                "my_orders.send_to_my_system",
                "my_orders.set_seat",
                "my_performance",
                "my_performance.export",
                "my_profile",
                "my_profile.edit",
            }),
        )

    def test_unknown_group_yields_no_nodes(self):
        """未知 Group 没有任何节点——fail-closed。"""
        self.assertEqual(nodes_of(self._user("某个自定义职位")), frozenset())

    def test_no_group_yields_no_nodes(self):
        self.assertEqual(nodes_of(self._user()), frozenset())

    def test_tenant_admin_gets_every_node(self):
        """持「系统管理员」**职位**的账号拥有一切节点——对标物 roleId 6 的官方说明。

        管理员判据是持「系统管理员」职位，不是只置 `is_superuser`。
        条数写字面量，不引用 `ALL_CODES`。
        """
        u = User.objects.create_superuser(username="admin", password="pw")
        u.groups.add(Group.objects.get_or_create(name="系统管理员")[0])
        # 170 = 13 栏目行 + 157 功能点。
        self.assertEqual(len(nodes_of(u)), 170)

    def test_bare_superuser_gets_no_role_from_the_django_flag(self):
        """反例：只置 `is_superuser`、不持该职位的账号拿到**空集**（`main` 的裁定）。

        出口是 `nodes_of`。只置 `is_superuser`、不持该职位 ⇒ 空集。
        """
        u = User.objects.create_superuser(username="su-bare", password="pw")
        self.assertEqual(nodes_of(u), frozenset())


class IntakeCreatesPerformanceTests(TenantTestCase):
    def setUp(self):
        # ⚠️ 偏离 brief（**必做**，见报告 §偏离 6）：brief 这里写的是裸
        #    `Client(SERVER_NAME="tenant.test.com")`（匿名）。P5 的 fail-closed 默认权限类
        #    （`IsAuthenticated`，spec D11）会让匿名 POST 直接 401，而本类的断言要求
        #    `code == 0`——那一行照抄就**永远不可能绿**。
        #    录单不要求任何**职位**，所以用**非职位组**的真实登录（`force_login` 不可用）。
                #    ⚠️ ：录单现在要 `order.add_edit` 节点，而节点只能经 `Group → Role`
        #    拿到 ⇒ 夹具必须给一个组。组名刻意用**非内置职位**的「录入组」——
                #    单轴化后判据只看节点，「录单不要求任何内置职位」**依然为真**。
        self.client = make_logged_in_client(
            username="intake-writer", groups=("录入组",), nodes=("order.add_edit",)
        )
        self.base = "/api/business/orders"
        self.game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 录单是写路径（`assert_game_allowed`）⇒ 录入账号必须显式持有本用例那款游戏。
        grant_games(User.objects.get(username="intake-writer"), self.game)

    def _create(self, **overrides):
        payload = {"game": self.game.id, "project_text": "满星", "hall_price": "88.00"}
        payload.update(overrides)
        return self.client.post(
            self.base + "/", payload, content_type="application/json"
        )

    def test_intake_creates_the_main_performance(self):
        body = self._create().json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        # ⚠️ **Ruling Q 之后录单还会按 `request.user` 建一条客服行**，
        #    所以这里必须按 `emp_type` 取行——`.get(order=order)` 会 MultipleObjectsReturned。
        #    客服行本身由 `test_customer_commission.py` 专门钉（本用例只管打手行）。
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)          # 还没人接
        self.assertIsNone(perf.review_status)    # 还没有可审的东西
        # 录单时按「接单价全额」估值（spec §6.1 的顺序问题：那一刻不知道是谁接）
        self.assertEqual(perf.temp_amount, Decimal("88.00"))

    def test_order_and_performance_counts_match(self):
        """⚠️ 防孤儿订单：`/claim` 的前提是"这单一定有绩效"。

        断言**具体集合**而不是 `count()`——`count()` 相等也可能是错的单配错了绩效。
        """
        self._create()
        self._create(hall_price="1.00")
        order_ids = set(Order.objects.values_list("id", flat=True))
        perf_order_ids = set(Performance.objects.values_list("order_id", flat=True))
        self.assertEqual(order_ids, perf_order_ids)

    def test_missing_hall_price_yields_zero_not_null(self):
        """没有接单价时落 0，不是 NULL——否则后续金额计算要多一处判空。"""
        body = self._create(hall_price=None).json()
        order = Order.objects.get(order_no=body["data"]["order_no"])
        # 同上：Ruling Q 之后还有一条客服行，必须按 `emp_type` 取 MAIN。
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).temp_amount,
            Decimal("0.00"),
        )

    def test_intake_rollback_leaves_neither_order_nor_performance(self):
        """建绩效中途失败时**订单行与绩效行都不留**（spec §7.4 / §10 #10–#11）。

        为什么必须有这一条：`order_views.create` 里那句 `transaction.atomic()` 是
        「订单与绩效必须同生共死」的**唯一**落地机制，而在本测试之前**把它删掉全套
        244 条测试一条都不会红**——正是 `AGENTS.md` §5 / E-0009 说的
        「测了动作、没测动作的落地机制」的假通过形状。
        （实测判别力对照：删掉 `atomic()` 后 246 条里**只有**本测试变红，见报告 §3。）

        ⚠️ 打桩路径必须是**视图模块里的那个绑定名**：`order_views` 是
        `from ... import create_performance_for_order` 绑进来的，打在
        `apps.business.performance` 上视图根本看不见（会照常建出绩效行）。

        ⛔⭐ **改准（原句在此保留：「打桩路径必须是**视图模块里的那个绑定名**」）**：
        上面那条口径本身没变（E-0029：`mock.patch` 必须打在**被查找的那个模块**上），
        变的是**那个模块是谁**——T6 把录单的领域核心抽进了 `apps/business/order_intake.py`，
        它在**自己的模块命名空间**里 import 并调用 `create_performance_for_order`
        （`order_intake.py` 顶部的 import + `create_order` 里那一次调用），
        于是「被查找的模块」从 `apps.business.api.order_views` 变成了 `apps.business.order_intake`
        ⇒ 老的那条桩**再也接不上**，本用例在 **T6 的提交（`6ab5b1ae`）上就已经红**
        （实测读数：`AssertionError: RuntimeError not raised`，且 `seen` 长度为 0），
        而它**当时没被发现**——T6 的受影响网清单漏了本文件。
        ⚠️ 这正是 AGENTS §5-E / E-0029 点名的形态：**搬了落点就必须同一次搬桩**。
        ⇒ 打桩路径改到 `apps.business.order_intake.create_performance_for_order`。
        ⛔ **判别力没有丢**（改准后实测）：把 `order_intake.create_order` 里那个
        `with transaction.atomic():` 删掉 ⇒ 本用例**仍然**红在
        `AssertionError: Tuples differ: (True, True) != (False, False)`，
        且只有它红（30 条里 1 失败）——两次写入都发生了、只是不再同生共死。
        ⚠️ 三条断言（`seen` 长度 / 订单行不存在 / 绩效行不存在）一个字都没改。

        ⚠️ 偏离 brief（报告 §偏离 2）：brief 让桩直接 `side_effect=RuntimeError("boom")`。
        那样「绩效行不存在」这条断言**恒真**——桩抛在创建之前，绩效行不可能存在，
        删掉 `atomic()` 它照样绿，是 brief 自己禁止的那种无判别力断言。
        这里让桩**先真正建出绩效行、再抛**：同一事务里的两次写入都发生了，
        于是订单行与绩效行**两条都真的能红**（判别力对照见报告 §3）。
        """
        seen = []

        def _create_then_boom(order):
            # 先落地绩效行（调生产同一函数），再模拟「建绩效过程中途失败」
            seen.append(order.order_no)
            real_create_performance_for_order(order)
            raise RuntimeError("boom")

        with patch(
            "apps.business.order_intake.create_performance_for_order",
            side_effect=_create_then_boom,
        ):
            with self.assertRaises(RuntimeError):
                self._create()

        # 前置条件：桩确实被调到了。否则下面两条断言会因为「请求根本没走到那一步」
        # 而假通过（那时的绿说明的是别的东西）。
        self.assertEqual(len(seen), 1, "建绩效函数没被调用，本测试没测到要测的东西")
        order_no = seen[0]
        # 两条都用**具体行**判定（不用 count()）。合成一个断言是为了让红的时候
        # 一次性看到两边的真实值：`(True, True) != (False, False)`。
        self.assertEqual(
            (
                Order.objects.filter(order_no=order_no).exists(),
                Performance.objects.filter(order__order_no=order_no).exists(),
            ),
            (False, False),
            f"录单失败后仍留下了行（order_no={order_no}）：订单与绩效没有同生共死",
        )


class TransitionRequiresIdentityTests(TenantTestCase):
    """/transition/ 不再接受「匿名传 target 就改状态」。"""

    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001"
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN
        )
        self.url = f"/api/business/orders/{self.order.order_no}/transition/"

    def _login(self, *groups, username="u1", nodes=ROLES_ORDER_NODES):
        """真实登录（fail-closed，spec D11）并返回该用户。

        ⚠️ **不能用 `force_login`**：它产出的会话里**没有 `identity_plane`**，
        会被守卫中间件与 `TenantBoundAuthentication` 按 fail-closed 不变式拒掉——
        结果是**每个业务请求都 401**（完整说明见 `tests/p5_auth_helpers.py`）。
        本方法顺手把 `self.client` 换成已登录的 client，所以调用点可以直接写
        `self._login("客服")`；需要用户对象时写 `u = self._login("客服")`。
        `nodes` 可覆盖：单轴化之后**节点**才是判据，拒绝侧要显式少给那一个（spec §4.4）。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类每次都 POST `/transition/`（该路径取订单过 `game_scope`）⇒ 登录账号必须显式持有
        # 本用例那款游戏；不补的话拿到的不是要测的角色/节点判据，而是 1002「记录不存在或已被删除」。
        grant_games(user, self.game)
        return user

    def test_anonymous_is_rejected(self):
        body = self.client.post(
            self.url, {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertNotEqual(body["code"], 0)          # 必须失败
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)   # 且没改

    def test_cs_can_throw_to_hall(self):
        self._login("客服")
        body = self.client.post(
            self.url, {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_a_user_without_the_change_status_node_is_stopped_by_the_node_gate(self):
        """没有 `order.change_status`（16）的账号在**类级节点门**被挡（403）。

        ⚠️ 2026-09-23 换轴：本条替换了旧的 `test_taker_cannot_throw_to_hall`
        （「代练员不得抛单」——角色口径）。夹具本来就会把声明的节点挂到该组上，
        所以单轴化之后「代练员 + 给了 16」**会**被放行：职位名不再是判据。
        判据改成**节点口径**：只给抢单节点（`grab_hall.take_order`）、不给 16 ⇒ 403。

        ⛔ **改准（抛大厅独占闸，计划 ④）**：本用例打的 `target` 是
        `WAITING_GRAB`，而**这个目标的类级门已改判独占 36**（不再是 16）
        ⇒ 该账号「缺 16」与「缺 36」两件事都成立，403 本身**分不出**是哪一扇门挡的。
        它钉住的是「这个账号到不了」，⛔ 不是「16 单独管这个目标」；「只持 16 也要 403」
        由 `tests/test_rbac_throw_hall_gate.py` 的 pin4 钉住（那个账号**持 16**、只缺 36）。
        """
        self._login("代练员", nodes=("grab_hall.take_order",))
        response = self.client.post(
            self.url, {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_a_custom_title_holding_the_node_can_transition(self):
        """**自定义职位（任意 Group）持有该节点 ⇒ 放行**——这是单轴化的核心价值。

        ⚠️ 2026-09-23 换轴：本条替换了旧的
        `test_authenticated_with_a_group_but_no_position_is_rejected_by_the_role_layer`。
        旧断言的前提是「职位是授权轴」：一个不在 `GROUP_TO_ROLES` 里的组名映射不出职位
        ⇒ 状态机拒绝（200 + 1002）。单轴化之后**节点才是授权轴**：同一个「无职位组」账号，
        只要它的角色挂着 `order.change_status`（16），`/transition/` 就**真的放行**
        ——自定义职位与内置「客服」走同一套判据（spec §4.4「自定义职位」行）。
        """
        self._login("无职位组", username="u-custom-title")   # 任意组名 + 节点 16
        response = self.client.post(
            self.url, {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_authenticated_without_any_group_is_stopped_by_the_node_gate(self):
        """零 Group 的账号在**节点门**被挡（403）——它到不了角色层（③线 Task 2）。

        ⚠️ 这是新口径的**如实登记**，不是把上一条放宽：同一端点、同一请求体，
        「有节点、无职位」→ 200 + 1002（角色层），「零 Group（⇒ 零节点）」→ 403
        （权限层）。两层判据分开可见，各自都还测得动。
        """
        self._login()          # 真实登录，但**不给任何 Group**（⇒ 零节点）
        response = self.client.post(
            self.url, {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_clear_taker_edge_resets_performance(self):
        """`IN_PROGRESS → UNASSIGNED` 必须把绩效整条回退（spec §4.1.2 的三元组）。

        ⚠️ 自审加固（报告 §偏离 13）：brief 那版只把 `user` 设好就迁移，
        于是 `review_status`（本来就是 NULL）与 `bonus_amount`（本来就是 0.00）
        两条断言**恒真**——「只清了 user」的残缺实现照样能过。这里把三个字段都先设成
        非初始值，三元组才真的被钉住。

        ⭐ 这条边**有接手人**时通用 `/transition/`
        已被新闸拒（有接手人 ⇒ 不许回池）⇒ 改走它**自己的产品动作**入口
        `POST …/clear-taker/`（spec §4.1.2 例外①）。**判据一条没放宽**：三列仍各自
        从非初始值出发，`/clear-taker/` 的钩子（`_on_pool_entry`）照旧负责回退。
        """
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        u = self._login("客服")
        self.perf.user = u
        self.perf.review_status = ReviewStatus.REJECTED
        self.perf.bonus_amount = Decimal("66.00")
        self.perf.save(update_fields=["user", "review_status", "bonus_amount"])
        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/clear-taker/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertIsNone(self.perf.user_id)
        self.assertIsNone(self.perf.review_status)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))


class TransitionHookCoverageTests(TenantTestCase):
    """A4 验收：改状态的**每一条**路径都自动带上副作用。

    ⚠️ 与 Task 2 的 `TargetHookTests` 互补：那里守**机制**（钩子会不会跑），
    这里守**覆盖**（各条真实入口有没有都走到机制上）。

    ⛔ **本类只落地了 brief Step 5b 三条里的第一条**（报告 §偏离 2）。另两条
    （`test_dedicated_clear_taker_endpoint_also_resets_performance`、
    `test_probe_hook_fires_from_every_entry_point`）要打 `/assign/`、`/claim/`、
    `/clear-taker/`——**这三个端点在 Task 3 时并不存在**（它们是 Task 4「四个派单动作」
    与 Task 6「抢单」的产出，排在本任务之后；而 Task 4 又反过来依赖本任务的**节点判据**）。
    照抄那两条会得到 `code != 0` 的失败。**由 Task 4 / Task 6 补上它们**。

    ✅ **`/clear-taker/` 那半已由 Task 4 交付**：测试在
    `tests/test_dispatch_api.py::DispatchApiTests.test_clear_taker_resets_a_dirty_triple`
    ——它从**非初始态**的三元组出发打**专用端点**，判别力与
    `test_generic_transition_endpoint_also_resets_performance` 同级。
    `/claim/` 那半仍等 **Task 6**（抢单端点尚不存在）。
    """

    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self._login("客服")

    def _login(self, *groups, username="u1", nodes=ROLES_ORDER_NODES):
        """真实登录（fail-closed，spec D11）并返回该用户。

        ⚠️ **不能用 `force_login`**：它产出的会话里**没有 `identity_plane`**，
        会被守卫中间件与 `TenantBoundAuthentication` 按 fail-closed 不变式拒掉——
        结果是**每个业务请求都 401**（完整说明见 `tests/p5_auth_helpers.py`）。
        本方法顺手把 `self.client` 换成已登录的 client，所以调用点可以直接写
        `self._login("客服")`；需要用户对象时写 `u = self._login("客服")`。
        `nodes` 可覆盖：单轴化之后**节点**才是判据，拒绝侧要显式少给那一个（spec §4.4）。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类每次都 POST `/transition/`（该路径取订单过 `game_scope`）⇒ 登录账号必须显式持有
        # 本用例那款游戏；不补的话拿到的不是要测的角色/节点判据，而是 1002「记录不存在或已被删除」。
        grant_games(user, self.game)
        return user

    def _order(self, no, status=OrderStatus.IN_PROGRESS, *, taker=True):
        order = Order.objects.create(
            game=self.game, order_no=no, status=status, hall_price=Decimal("88.00")
        )
        # ⚠️ 偏离 brief（**必做**，报告 §偏离 10）：brief 这里只写
                #    `review_status=ReviewStatus.REJECTED` 而**不写 `user`** —— 那会撞约束
        #    的约束③ `performance_review_needs_user`（`review_status` 非空 ⇒ `user` 非空），
        #    `Performance.objects.create` 抛 IntegrityError，本测试根本跑不起来。
        #    而且 brief 那版下 `user_id` 本来就是 NULL、`bonus_amount` 本来就是 0.00，
        #    回退前后的断言全**恒真**（自审抓掉的那一类）：所以这里让它真的处在
        #    「有归属人 + 已驳回 + 有生效绩效」的可回退状态。
        # ⭐ `taker=False` 造「行上无接单人」的形状——
        #    新闸只拦「`user` 非空 ⇒ 迁入池内两态」，`user` 非空时那条入口现在被拒。
        #    ⚠️ 这一档 `review_status` 只能为空（CheckConstraint `performance_review_needs_user`）
        #    ⇒ 唯一**非初始**且可观测的列是 `bonus_amount`（起始 `66.00` ⇒ 回退 `0.00`）。
        fields = {
            "order": order,
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("88.00"),
            "bonus_amount": Decimal("66.00"),
        }
        if taker:
            fields["user"] = User.objects.create_user(username=f"taker-{no}")
            fields["review_status"] = ReviewStatus.REJECTED
        perf = Performance.objects.create(**fields)
        return order, perf

    def test_generic_transition_endpoint_also_resets_performance(self):
        """原生缺陷路径：P2 的通用 /transition/ **没有**专用端点也要带上副作用。

        ⭐ 起点改成「行上无接单人」（新闸下唯一
        可达的形状）。⚠️ 这样 `user_id` / `review_status` 两条断言是**恒真**的
        （行上本来就没有）——保留它们只为把「回退成空」写下来；**有判别力的是
        `bonus_amount`**（`66.00` ⇒ `0.00`：删掉 `_on_pool_entry` 的注册本条就红）。
        `user` / `review_status` 的非恒真版本由上面的
        `test_clear_taker_edge_resets_performance`（走例外①）钉住。
        """
        order, perf = self._order("20260920000000000001", taker=False)
        self.assertEqual(perf.bonus_amount, Decimal("66.00"))   # 起点哨兵：非初始
        body = self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.UNASSIGNED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)          # ← 走通用端点也被回退了
        self.assertIsNone(perf.review_status)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))

    def test_issue_to_unassigned_resets_performance(self):
        """`ISSUE → UNASSIGNED` **必须**回退三元组——**D-0032 显式推翻了本用例的旧断言**。

        ⚠️ 本用例原名 `test_issue_to_unassigned_does_not_reset_performance`，
        钉的是旧口径：「`_on_unassigned` 的 `from_status` 守卫让
        `ISSUE/PAUSED → UNASSIGNED` 不回退绩效」。业务方 **2026-09-20 裁定 D-0032**
        「池内状态 ⇒ 无接单人」（原文：「只要回到未分配/待抢单就清掉」），
        所以**断言方向被翻过来**——旧断言不是被删掉，而是被本裁定**证伪**的证据。

        为什么必须翻：旧口径下，一张**已有人接**的单经 P2 遗留的四条进池边
        （`ISSUE/PAUSED → UNASSIGNED/WAITING_GRAB`）进入池内后，大厅
        （入选条件 = 待抢单 **且** 绩效 `user` 为空）**看不见它**，
        抢单的条件更新会 `rowcount == 0` 报「订单已被接走」——与「真的被别人抢走」
        **完全无法区分**。失效模式不是报错，是**卡死**。
        决策记录（含三个候选方案与**被接受的代价**：原接单人已记录的工时被清空；
        绩效行是**认领记录**不是历史账）：
        `.agents/notes/implemented/architecture/2026-09-20-pool-states-mean-no-taker.md`。
        spec §4.1.2 已按新口径改写。

        ⭐ **D2 之后这条「有人接也照样回池」的
        路径本身被产品口径取代**（有接手人 ⇒ 拒绝 + 提示清人，而不是静默回退）
        ⇒ 本用例的起点改成新闸下唯一可达的形状（行上无接单人）。
        ⚠️ 如实登记：`user_id` / `review_status` 两半在这条入口上因此**恒真**；
        有判别力的仍是 `bonus_amount`（`66.00` ⇒ `0.00`）。
        """
        order, perf = self._order(
            "20260920000000000002", status=OrderStatus.ISSUE, taker=False
        )
        # 起点哨兵：唯一可非初始的那一列必须真的非初始，否则「回退成 0」与「本来就是 0」
        # 不加区分（判别力判据 1）。
        self.assertEqual(perf.bonus_amount, Decimal("66.00"))
        body = self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.UNASSIGNED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        # 阳性对照：迁移**确实**发生了——否则这条测试对「迁移整个坏掉」也会通过
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.review_status)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))

    def test_all_pool_entry_edges_reset_performance(self):
        """其余三条「迁入池内」的进池边也必须回退三元组（D-0032）。

        四条边**逐条枚举**（不举例、不含糊）：`ISSUE → UNASSIGNED`（被推翻的那条，
        由上面那条用例单独钉住）、`ISSUE → WAITING_GRAB`、`PAUSED → UNASSIGNED`、
        `PAUSED → WAITING_GRAB`。后两条进 `WAITING_GRAB` 的边**正是大厅依赖的路径**：
        已有人接的单被退回大厅时若不清人，大厅就看不见它、抢单会误报「已被接走」。

        ⚠️ 判别力对照：把 `performance.py` 的注册改回「只注册 `UNASSIGNED` +
        `from_status == IN_PROGRESS` 守卫」→ 本用例三个子用例与上面那条**全部变红**
        （三个子用例的可观测列是 `bonus_amount`，见下）。

        ⭐ 起点统一改成「行上无接单人」——
        D2 之下「有接手人 ⇒ 迁入池内两态」一律被拒，这三条边**只有这个形状可达**。
        ⚠️ 如实登记：`user_id` / `review_status` 两半因此**恒真**；有判别力的仍是
        `bonus_amount`（`66.00` ⇒ `0.00`）。`user` / `review_status` 的非恒真版本由
        `test_clear_taker_edge_resets_performance`（走例外①）钉住。
        """
        cases = [
            ("20260920000000000003", OrderStatus.ISSUE, OrderStatus.WAITING_GRAB),
            ("20260920000000000004", OrderStatus.PAUSED, OrderStatus.UNASSIGNED),
            ("20260920000000000005", OrderStatus.PAUSED, OrderStatus.WAITING_GRAB),
        ]
        for no, source, target in cases:
            with self.subTest(source=source, target=target):
                order, perf = self._order(no, status=source, taker=False)
                self.assertEqual(perf.bonus_amount, Decimal("66.00"))   # 起点哨兵
                body = self.client.post(
                    f"/api/business/orders/{order.order_no}/transition/",
                    {"target": target}, content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 0)
                order.refresh_from_db()
                self.assertEqual(order.status, target)  # 阳性对照：迁移真的发生
                perf.refresh_from_db()
                self.assertIsNone(perf.user_id)
                self.assertIsNone(perf.review_status)
                self.assertEqual(perf.bonus_amount, Decimal("0.00"))


class AllowedTransitionsFollowCallerNodesTests(TenantTestCase):
    """Step 5d：详情列出的迁移必须是**该调用方真的能走的**——判据与 `/transition/` 同源。

    ⚠️ **2026-09-23 换轴**：旧版这里按**角色集**推理（`_ANY_STAFF` / `_REVIEWER` / `_CS`）。
    单轴化之后本字段读的是**边的节点集 ∩ 调用方节点集**，所以本类的判据也换成节点：
    - `PENDING_REVIEW → IN_PROGRESS`（驳回）的节点集 = `{16, 17}` ⇒ 持 16 就列出；
    - `PENDING_REVIEW → COMPLETED` 的节点集 = `{17}` ∧ 通用端点的类级门 17 ⇒ **必须**持 17；
    - 异常态三条的节点集 = `{16}`。
    旧口径「客服看不到驳回边」**已随换轴作废**（spec §5.3 明确接受这条放宽）。

    ⚠️ **D-0071之后本类多一层判据**：`COMPLETED` 是否列出还取决于
    **节点 `order.change_status_completed`（17）**——业务方原话「有权限的职位都可以用」，
    而「有权限」= 持该节点。所以本类的夹具**显式区分**「持节点 / 不持节点」两种客服：
    这是 `test_listing_agrees_with_the_transition_endpoint` 在**同一订单、同一状态**上
    真正能分辨两种判据（持 17 / 不持 17）的地方。
    """

    def setUp(self):
        self.client = Client(SERVER_NAME="tenant.test.com")
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000009",
            status=OrderStatus.PENDING_REVIEW,
        )
        self.detail = f"/api/business/orders/{self.order.order_no}/"

    #: 带「已完成」节点（17）的节点集：本类多数用例要能看见 `COMPLETED`。
    NODES_WITH_COMPLETED = ROLES_ORDER_NODES + ("order.change_status_completed",)

    #: 从 `PENDING_REVIEW` 出发、持 16（本类夹具同批另持 36）、不持 17 时能列出的目标
    #: （读节点表核到）。
    #: （D-0147 / R8）：状态机整体放开 ⇒ 两个池内态（`UNASSIGNED` /
    #: `WAITING_GRAB`）也进来了（**当时**它们的 target 节点集都含 16）。`COMPLETED` 仍缺
    #: （要节点 17）。
    #: ⛔ **抛大厅独占闸**：`WAITING_GRAB` 的 target 节点集
    #: **已不含 16**（该目标改判独占 36，见 `order_status._TARGET_NODES`）——它仍在本集合里，
    #: 是因为本类夹具 `ROLES_ORDER_NODES` 同批补持了 36（**补前提，不是放宽判据**）；
    #: `UNASSIGNED` 那一条照旧含 16。
    WITHOUT_COMPLETED = {
        OrderStatus.UNASSIGNED,
        OrderStatus.WAITING_GRAB,
        OrderStatus.IN_PROGRESS,
        OrderStatus.ISSUE,
        OrderStatus.PAUSED,
        OrderStatus.REFUNDED,
    }

    def _login(self, *groups, username="u1", nodes=ROLES_ORDER_NODES):
        """真实登录（fail-closed，spec D11）——`force_login` 产出的会话没有
        `identity_plane`，会被守卫中间件按 fail-closed 拒掉，每个请求都 401。

        ⚠️ `nodes` 可覆盖：本类要区分「持节点 17 / 不持节点 17」两种客服
        （D-0071 之后 `COMPLETED` 的列出与否由**节点**决定，不是由职位决定）。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 详情 GET 过 `game_scope` 取订单 ⇒ 每个登录账号（含两次 `_login` 的客服）都要显式
        # 持有本用例那款游戏，否则详情回 1002，`allowed_transitions` 根本取不到。
        grant_games(user, self.game)
        return user

    def _allowed(self):
        body = self.client.get(self.detail).json()
        self.assertEqual(body["code"], 0)
        return body["data"]["allowed_transitions"]

    def test_cs_without_the_completed_node_does_not_see_completed(self):
        """客服**不持**节点 17 ⇒ 列表里**没有** `COMPLETED`；持了 ⇒ 有。

        ⚠️ **本条替换了旧的 `test_cs_does_not_see_the_review_edges`**，并在
        **2026-09-23 换轴**：旧断言的否定侧是「客服看不到 `IN_PROGRESS`（驳回）」，
        那在角色轴下成立；新轴下驳回边的节点集是 `{16, 17}`，持 16 就列出
        （spec §5.3 接受这条放宽）⇒ 否定侧只剩 `COMPLETED`。
        节点维度仍是本条的主判据：**同一职位、同一状态**，只差节点 17
        ⇒ 一个有 `COMPLETED`、一个没有；其余目标集完全不变（逐字比对，不是包含）。
        """
        # ① 不持节点 17：集合必须非空（否则「漏传 context ⇒ 对所有人变空」也会假通过）
        self._login("客服")
        without = set(self._allowed())
        self.assertEqual(without, self.WITHOUT_COMPLETED)
        self.assertNotIn(OrderStatus.COMPLETED, without)

        # ① 的另一半：**同一职位、同一状态**，只多挂节点 17 ⇒ `COMPLETED` 出现
        self._login("客服", username="cs_with_node", nodes=self.NODES_WITH_COMPLETED)
        with_node = set(self._allowed())
        self.assertEqual(with_node, self.WITHOUT_COMPLETED | {OrderStatus.COMPLETED})

    def test_reviewer_sees_the_reject_edge_and_completed(self):
        """客服主管（持 16 + 17）看到的集合 = 驳回边 + 已完成 + 异常态三条。

        ⚠️ **`COMPLETED` 的判据更新（D-0071）**：本字段的契约仍是
        「**通用 `/transition/` 真的会接受**的目标集合」。旧注释写「完成：只能走 `/review`」
        ——那句随 D-0071 作废：通用端点现在按**节点 17** 接受 `COMPLETED`，
        所以持有 17 的主管**必须**在列表里看到它（否则前端会渲染一个缺失的按钮）。

        ⚠️ **2026-09-23 换轴**：旧注释说「集合里没有异常态三条，因为主管不含 `CS`」——
        那是角色轴下的事实，**已作废**：新轴只看节点，主管的夹具持有 16
        ⇒ 异常态三条**也在**集合里（这正是 spec A3 登记的放宽）。
        全表比对（不是 `assertIn`）把「集合恰好是这五个」钉住。
        """
        self._login("客服主管", username="reviewer1", nodes=self.NODES_WITH_COMPLETED)
        allowed = set(self._allowed())
        self.assertIn(OrderStatus.IN_PROGRESS, allowed)        # 驳回：持 16 即可
        self.assertIn(OrderStatus.COMPLETED, allowed)          # 完成：持节点 17 即可
        self.assertEqual(
            allowed,
            self.WITHOUT_COMPLETED | {OrderStatus.COMPLETED},
        )

    def test_listing_agrees_with_the_transition_endpoint(self):
        """同源判据的**双向**行为侧对照：列表恰好是「`/transition/` 会接受」的集合。

        拿真实端点当 oracle，而不是把节点集合抄第二遍——将来 `TRANSITIONS` 改了集合、
        或节点判据改了，本测试依旧成立（它测的是「两处判据一致」本身）。
        **两个方向都断**：
        - 端点返回 403（权限层）⇒ 目标**不得**出现在列表里；
        - 端点没有 403 ⇒ `code == 0`（接受）**当且仅当**该目标在列表里。

        ⚠️ 为什么不能简化成「`target in listing` ⟺ `status_code != 403`」：**非法边**
        由 `apply_transition` 抛 `IllegalTransition`、端点回 **200 + 业务码**（不是 403），
        而它当然不在列表里 ⇒ 那个等价式对非法边恒假。所以 403 只用来判「不许列出」，
        「列出 ⟺ 接受」用业务码判。

        ⚠️ **Task 4 收紧后本夹具（持 16 + 17）不会触发 403 分支**：它对每个目标都过得了
        类级门。403 那一支由下面的
        `test_a_specialised_endpoint_node_is_not_listed_by_the_generic_endpoint` 实测钉住。

        ⚠️ **夹具持节点 17**（`NODES_WITH_COMPLETED`）：不持的话 `COMPLETED` 在两侧
        都不可达，「一致」会**平凡成立**（两边都拒绝）——那就测不出 D-0071 新增的那条判据。

        ⚠️ **本用例的单显式补了价**（`received_price=100.00`，见下）：P4FU-F
        之前，无价订单迁 `REFUNDED` 会被 §10 #14 判 `1008`（双空），而 `allowed_transitions`
        仍会列出它 ⇒ 那是 spec §5.3 与 §10 #14 的**已知张力**（曾登记为 D-0046 待决项）。
        **该张力已随 P4FU-F 解开**：毛收款回落档改成订单总价（`null=False default=0.00`）
        ⇒ 无价单照常能退（毛收款 0），不再有 `1008` 那道闸。
        补价因此**不再是必要条件**，保留它只是让退款金额非 0、更贴近生产形态；
        本用例要测的仍旧是「列表与端点判据一致」。
        无价单的新行为由 `test_order_api.RefundedWithoutPriceTests` 单独钉住。

        ⭐ **本批 F3，spec §2.3）——「已完成」是本条等价式里一条
        显式枚举的例外**：入 `COMPLETED` 的边只剩「待验收 → 已完成」，而读侧**有意**
        仍把「已完成」列出来当**会被拒的提示项**（业务方明确要求「下拉里已完成也要有」，
        选中时由前端弹提示、后端再拒一次）。所以来源态 != 待验收 时，
        「列表里有」为真而「端点接受」为假。下半场**逐条枚举**那六个来源态把这层不对称
        钉住（⛔ 不是把上面那条 `assertEqual` 放宽——上面那半仍按原样判「一致」）。
        """
        self._login("客服", nodes=self.NODES_WITH_COMPLETED)
        listed = set(self._allowed())
        self.assertIn(OrderStatus.COMPLETED, listed)   # 哨兵：否则「一致」是平凡成立的
        for index, target in enumerate(OrderStatus.values):
            order = Order.objects.create(
                game=self.game,
                order_no=f"2026092{index:013d}",          # 20 位
                status=OrderStatus.PENDING_REVIEW,
                received_price=Decimal("100.00"),         # 让退款金额非 0（P4FU-F 前是「退单必需」）
            )
            response = self.client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": target},
                content_type="application/json",
            )
            if response.status_code == 403:
                self.assertNotIn(
                    target, listed, f"{target}：端点 403，详情却把它列出来"
                )
                continue
            body = response.json()
            self.assertEqual(
                body["code"] == 0,
                target in listed,
                f"{target}：详情列表与 /transition/ 的判据不一致",
            )

        # 例外那一半：来源态 != 待验收（且 != 已完成，自环本来就不列）时，
        # 「已完成」**列出但会被拒**——这就是有意的读/写不对称本身。
        for index, source in enumerate(
            s for s in OrderStatus.values
            if s not in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED)
        ):
            order = Order.objects.create(
                game=self.game,
                order_no=f"2026093{index:013d}",          # 20 位
                status=source,
                received_price=Decimal("100.00"),
            )
            detail = self.client.get(f"/api/business/orders/{order.order_no}/").json()
            self.assertEqual(detail["code"], 0, (source, detail))
            row = detail["data"]
            self.assertIn(
                OrderStatus.COMPLETED,
                row["allowed_transitions"],
                f"{source}：业务方要求下拉里「已完成」照旧要有",
            )
            self.assertIs(row["completed_requires_review"], True, source)

            response = self.client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": OrderStatus.COMPLETED},
                content_type="application/json",
            )
            body = response.json()
            self.assertEqual(response.status_code, 200, (source, body))
            self.assertNotEqual(body["code"], 0, (source, body))
            self.assertEqual(
                body["msg"],
                "必须先把订单状态改成待验收才能改成已完成",
                source,
            )
            order.refresh_from_db()
            self.assertEqual(order.status, source)

    def test_a_specialised_endpoint_node_is_not_listed_by_the_generic_endpoint(self):
        """持专属端点节点（50）但没有 16 ⇒ 那条边**不列**，而专属端点**确实**接受它。

        本字段的契约是「**通用 `/transition/`** 真的会接受的目标集合」（spec §6 第 15 行），
        ⛔ 不是「状态机允许的目标集合」。边 `WAITING_GRAB → IN_PROGRESS` 的节点集含
        `grab_hall.take_order`，`/claim/` 持 50 就能走；但通用端点的类级门是 16
        ⇒ 这个调用方点通用端点必然 403，所以**不能**列出——旧实现（只判边）会列出它，
        那是「列了却做不到」（Task 4 收掉的既存缺口）。

        三件一起断，少一件就有假通过面：① 列表恰为空集；② 通用端点 403；
        ③ 同一条边经专属端点 `/claim/` **真的走通**（否则「边不存在」也能凑出 ①②）。
        """
        order = Order.objects.create(
            game=self.game,
            order_no="20260920000000008801",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self._login(
            "代练员", username="taker_take_order",
            nodes=("order.view_detail", "grab_hall.take_order"),
        )

        detail = self.client.get(f"/api/business/orders/{order.order_no}/").json()
        self.assertEqual(detail["code"], 0)
        self.assertEqual(detail["data"]["allowed_transitions"], [])

        blocked = self.client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 403)

        claimed = self.client.post(
            f"/api/business/hall/{order.order_no}/claim/", {},
            content_type="application/json",
        )
        self.assertEqual(claimed.json()["code"], 0, claimed.content[:200])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)


class WriteResponseAllowedTransitionsTests(TenantTestCase):
    """Step 5d 的下半场：**四条写路径的响应**也必须带按节点算的 `allowed_transitions`。

    详情 GET 走 DRF 的 `get_serializer(instance)`（自动带 `request` context），
    所以它**覆盖不到** `order_views.py` 里那 4 处手写的 `OrderSerializer(order)`。
    那 4 处漏传 `context` 的话节点落到空集 —— 字段会对**所有人**变空，
    比原来的分叉更糟（静默丢功能）。这里逐条打在写路径的响应上。

    期待值都用客服在 `WAITING_GRAB` / `UNASSIGNED` 上的**非空**集合：
    各 6 条 —— 任一为空都会红。
    ⚠️ **2026-09-23 换轴**：`AT_HALL` 从 4 条变 5 条——旧值不含 `IN_PROGRESS`
    （那是「代练员专属的接单边」，角色轴下客服过不去）；新轴下该边的节点集含 16，
    客服持 16 ⇒ 列出（spec A3 登记的放宽，与 `/transition/` 的判据同源）。
    ⚠️ **D-0147 / R8）**：两组各再 +1 = 6 条——状态机整体放开后，
    两个池内态都能去 `PENDING_REVIEW`（target 节点集含 16）。`COMPLETED` 仍不在
    （要节点 17，客服不持）。
    """

    AT_HALL = [
        "UNASSIGNED", "IN_PROGRESS", "PENDING_REVIEW", "ISSUE", "PAUSED", "REFUNDED",
    ]
    AT_UNASSIGNED = [
        "WAITING_GRAB", "IN_PROGRESS", "PENDING_REVIEW", "ISSUE", "PAUSED", "REFUNDED",
    ]

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.base = "/api/business/orders"
        self.client = make_logged_in_client(
            username="cs_writer", groups=("客服",), nodes=ROLES_ORDER_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 录单是写路径（`assert_game_allowed`），改单 / 改价 / 迁移取订单过 `game_scope`
        # ⇒ 这个客服账号必须显式持有本用例那款游戏。
        grant_games(User.objects.get(username="cs_writer"), self.game)

    def _order(self, no, status):
        """建单**并建绩效**。

        ⚠️ 绩效行不是可选的（Task 4 评审修复，③ 的连带）：**D-0032** 之后
        「迁入 `UNASSIGNED` / `WAITING_GRAB`」会经钩子读那条 MAIN 绩效行。
        本类原先只建 `Order`，于是 `test_transition_response`（`UNASSIGNED → WAITING_GRAB`）
        实测报 `Performance.DoesNotExist`。
        补法就是让夹具满足**生产本来就保证**的不变量（录单必建绩效，spec §7.4）；
        本类三条断言一条没动。

        进池钩子缺行就补一行（`ensure_main_performance_of`）。缺行不是不变量破损：
        一张已退单、无绩效行的旧单恢复回池时，不补行会 500。夹具仍建行，但不是为了防止钩子炸。
        """
        order = Order.objects.create(
            game=self.game, order_no=no, status=status, project_text="满星"
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _transitions_in(self, body):
        self.assertEqual(body["code"], 0)
        return body["data"]["allowed_transitions"]

    def test_create_response(self):
        body = self.client.post(
            self.base + "/",
            {"game": self.game.id, "project_text": "满星"},
            content_type="application/json",
        ).json()
        self.assertEqual(
            self._transitions_in(body), self.AT_UNASSIGNED
        )

    def test_transition_response(self):
        order = self._order("20260920000000000021", OrderStatus.UNASSIGNED)
        body = self.client.post(
            f"{self.base}/{order.order_no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertEqual(self._transitions_in(body), self.AT_HALL)

    def test_price_response(self):
        order = self._order("20260920000000000022", OrderStatus.WAITING_GRAB)
        body = self.client.post(
            f"{self.base}/{order.order_no}/price/",
            {"total_price": "66.00", "reason": "调整"},
            content_type="application/json",
        ).json()
        self.assertEqual(self._transitions_in(body), self.AT_HALL)

    def test_partial_update_response(self):
        order = self._order("20260920000000000023", OrderStatus.WAITING_GRAB)
        body = self.client.patch(
            f"{self.base}/{order.order_no}/",
            {"project_text": "满星 改"},
            content_type="application/json",
        ).json()
        self.assertEqual(self._transitions_in(body), self.AT_HALL)
