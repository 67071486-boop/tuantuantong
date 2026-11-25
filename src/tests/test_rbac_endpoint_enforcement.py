"""承重判据：逐点双向（有判据 ⇒ 放行 / 无判据 ⇒ 403）+ flag 的读取点行为。⚠️ 为什么单独一个文件而不是散进各接口测试：本线的 DoD 是"**每一个**翻真的点都有可指认的消费方与一条会红的拒绝用例"——散着写就无法回答"82 点里还有几点没验"。
⚠️ 403 必须可辨来源（Ruling 32）。做法分两种：**同一路径上可能有多个 403 来源**的用例（例：`/review` 上既有职位闸门又有 codename 闸门）断 `msg`；只有**一个** 403 来源时（例：节点门）判别力来自**同一次运行里的配对证据**——"带判据 ⇒ 200 / 不带 ⇒ 403"，因为 DRF 对没有 `message` 的权限类给的是同一句默认文案，断它区分不出任何东西。"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    Order,
    OrderSource,
    OrderSourceGroup,
    OrderStatus,
    Performance,
    Project,
    ProjectCategory,
    ReviewStatus,
)
from apps.rbac.permissions import user_node_codes
from apps.rbac.seed import seed_catalog
from apps.business.order_flags import (
    ORDER_FLAG_CODES,
    can_edit_taker_price,
    can_view_offer_price,
    can_view_taker_price,
    flag_enabled,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client
from tests.xlsx_helpers import workbook_of


class FixtureTests(TenantTestCase):
    """`make_logged_in_client(nodes=...)` 自身的判据。
    ⚠️ **为什么夹具要有自己的用例**：`nodes` 的默认值是**空集**，而"默认空"这条事实决定了既有调用方的行为（不挂节点 ⇒ 受节点门保护的端点 403）。它被改坏（例如默认改成"挂上目录里的全部节点"）时，业务用例**不会**有反应——
    `groups=("客服",)` 的夹具在两种默认下都能过大部分测试，只是"过"的理由变了；夹具的默认值是静默生效的，只有直接断言它才看得见。"""

    def test_default_grant_is_empty_so_callers_must_declare_their_gate(self):
        """默认**不挂任何节点** ⇒ 调用方必须自己声明判据，否则 403。"""
        make_logged_in_client(username="fixture-default", groups=("客服",))
        user = User.objects.get(username="fixture-default")
        self.assertEqual(user_node_codes(user), set())

    def test_explicit_nodes_are_granted_exactly(self):
        """显式 `nodes=(...)` ⇒ `user_node_codes` **恰含**它（不多不少）。"""
        make_logged_in_client(
            username="fixture-nodes", groups=("客服",), nodes=("order.list",)
        )
        user = User.objects.get(username="fixture-nodes")
        self.assertEqual(user_node_codes(user), {"order.list"})


class FlagReaderTests(TenantTestCase):
    """flag 的读取点：`has_node_now` 的正反两向，不经 HTTP。"""

    def setUp(self):
        seed_catalog()
        self.client = make_logged_in_client(username="flag-u", groups=("客服",))
        self.user = User.objects.get(username="flag-u")

    def test_flag_defaults_to_absent_which_means_no_restriction(self):
        """⚠️ 本线最容易被误读的一条：**没有**这个 flag ⇒ **不施加**限制。"""
        for code in ORDER_FLAG_CODES:
            self.assertFalse(flag_enabled(self.user, code), code)
        self.assertTrue(can_view_taker_price(self.user))
        self.assertTrue(can_view_offer_price(self.user))
        self.assertTrue(can_edit_taker_price(self.user))

    def test_granting_a_restrict_flag_turns_the_capability_off(self):
        grant_nodes(self.user, "order.restrict_view_taker_price")
        self.assertTrue(flag_enabled(self.user, "order.restrict_view_taker_price"))
        self.assertFalse(can_view_taker_price(self.user))
        # 只挂一条不影响其它三条（四条互不蕴含——防止"按模块批发"式的实现）
        self.assertTrue(can_view_offer_price(self.user))
        self.assertTrue(can_edit_taker_price(self.user))

    def test_unregistered_code_raises_at_the_call_site(self):
        """拼错的 code 必须在**声明处**炸，而不是安静地恒 False。"""
        with self.assertRaises(ValueError):
            flag_enabled(self.user, "order.restrict_view_taker_prices")

    def test_superuser_is_never_restricted_by_a_restrict_flag(self):
        """限制型 flag 的**管理员旁路**（裁决）：管理员不能被这四条锁住。
        ⚠️ 这条用例**必须有判别力**：给管理员**所在的 Role** 手工挂上 flag 后再断言 `flag_enabled` 为 `False`——若实现里没有旁路，它会红（而不是"恰好为 False"）。
        ⚠️ 旁路认的是「持『系统管理员』职位」（不是 `is_superuser`）：拿超管夹具构不出旁路（限制会被正常施加 ⇒ 本条红）；前置条件读**库里「系统管理员」Role** 的授权，不能再用 `has_node_now`（它对管理员恒真，等于没有前置；E-0024 族）。"""
        from django.contrib.auth.models import Group, User as UserModel

        from apps.business.roles import TENANT_ADMIN
        from apps.rbac.models import Role

        admin = UserModel.objects.create_superuser(username="flag-super")
        admin.groups.add(Group.objects.get_or_create(name=TENANT_ADMIN)[0])
        grant_nodes(admin, "order.restrict_view_taker_price")
        self.assertIn(
            "order.restrict_view_taker_price",
            set(
                Role.objects.get(name=TENANT_ADMIN)
                .permissions.values_list("code", flat=True)
            ),
            "前置条件：节点确实挂在管理员的 Role 上（否则本条会因别的原因而绿）",
        )
        self.assertFalse(flag_enabled(admin, "order.restrict_view_taker_price"))
        self.assertTrue(can_view_taker_price(admin))

    def test_unregistered_code_still_raises_for_an_admin(self):
        """守卫顺序是刻意的：**先判 code 合法性，再判管理员**。
        ⚠️ 两个顺序都"能跑"，所以只有一条把它钉住：若旁路排在前面，管理员调用拼错的 code
        会静默拿到 `False` ⇒ 生产上表现为"限制静默失效"，正是 `ValueError` 守卫本想防的事。"""
        from django.contrib.auth.models import User

        admin = User.objects.create_superuser(username="flag-super-typo")
        with self.assertRaises(ValueError):
            flag_enabled(admin, "order.restrict_view_taker_prices")


class OrderEndpointNodeGateTests(TenantTestCase):
    """`OrderViewSet` 的读路径 + 逐 action 节点门。
    ⚠️ **客户端一律经 `_cs()`**（`make_logged_in_client(..., nodes=...)`）：节点默认空集，所以每条用例必须显式声明它要的那条判据——「放行」是这个声明的结果，不是默认状态。"""

    LIST_URL = "/api/business/orders/"
    ORDER_NO_1 = "20260921000000000001"
    ORDER_NO_2 = "20260921000000000002"
    ORDER_NO_3 = "20260921000000000003"

    _GROUP_SEQ = 0

    def setUp(self):
        from decimal import Decimal

        from apps.business.models import Game, Order

        seed_catalog()
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no=self.ORDER_NO_1,
            created_by="cs-a",
            total_price=Decimal("100.00"),
        )

    def _cs(self, username, *codes):
        """客服 + 显式节点集（走夹具的 `nodes=`；**不**用 `grant_nodes` 两套写法）。
        ⚠️ **每个 client 用独立的 Group**（`客服1` / `客服2` …，名字 ≤5 字以适配 `Role.name` 的 `max_length=5`）。**不能**都叫「客服」：节点挂在 `Role` 上，而 `Role.name` 唯一、与 `Group` 一对一（`_grant_nodes` 用 `get_or_create(name=…)`）⇒ 同组名共享同一个 Role，后一个 client 的 `nodes` 会**加在**前一个的节点上。
        实测：两个 client 同在「客服」组时，只挂 `order.view_detail` 的那个会继承 `order.list`、`GET /orders/` 拿到 **200**——`denied` 分支的 403 就永远断不出来。"""
        type(self)._GROUP_SEQ += 1
        group = f"客服{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(username=username, groups=(group,), nodes=codes)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类的订单都归 `self.game`，每个要读/写它的账号都得**显式**授那款游戏（缺档案 ⇒ 空集 ⇒ 列表少行 / 详情 200 + 1002 / 录单 1000）。
        # ⛔ 授的只是**游戏**，不碰任何节点判据——「无节点 ⇒ 403」那几条的钉子原样保留。
        grant_games(User.objects.get(username=username), self.game)
        return client

    def _list_order_nos(self, client):
        body = client.get(self.LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"] for row in body["data"]["items"]}

    def test_list_requires_order_list_node(self):
        """`order.list` 是列表的门：有它 200，只有别条的 code 一律 403。"""
        allowed = self._cs("cs-list-ok", "order.list")
        denied = self._cs("cs-list-no", "order.view_detail")
        self.assertEqual(allowed.get(self.LIST_URL).status_code, 200)
        resp = denied.get(self.LIST_URL)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], 403)

    def test_detail_accepts_either_view_detail_or_view_content(self):
        """两个 code 是 `any` 语义（`has_node(*codes)` 任一命中即放行）。"""
        a = self._cs("cs-det-a", "order.view_detail")
        b = self._cs("cs-det-b", "order.view_content")
        c = self._cs("cs-det-c", "order.list")
        url = f"/api/business/orders/{self.order.order_no}/"
        self.assertEqual(a.get(url).status_code, 200)
        self.assertEqual(b.get(url).status_code, 200)
        self.assertEqual(c.get(url).status_code, 403)

    def test_list_only_returns_own_created_orders_when_the_flag_is_set(self):
        """`flag` 的失败表现是**少行**，不是 403（spec §5.2）。⚠️ **必须配一条「没有 flag」的对照**（本用例的第一段断言）：只断"受限者看到两张"的话，
        "`scope_by_creator` 无条件按 `created_by=自己` 过滤"这种坏实现也会绿——两张是它唯一的输出，与"限制生效"在观测上不可区分。
        对照给出三张 ⇒ 第三张（`someone-else` 录的）**只**被 flag 滤掉，判据才落在 flag 上。"""
        from apps.business.models import Order

        Order.objects.create(game=self.game, order_no=self.ORDER_NO_2, created_by="cs-b")
        # 前两张都归到受限者名下，第三张归别人 —— 受限者应当**只**看到前两张。
        Order.objects.filter(
            order_no__in=[self.ORDER_NO_1, self.ORDER_NO_2]
        ).update(created_by="cs-mine")
        Order.objects.create(
            game=self.game, order_no=self.ORDER_NO_3, created_by="someone-else"
        )

        # 对照：另一个用户**没有**挂 flag ⇒ 三张全见（证明"少行"来自 flag，不是来自查询范围）
        unrestricted = self._cs("cs-unrestricted", "order.list")
        self.assertEqual(
            self._list_order_nos(unrestricted),
            {self.ORDER_NO_1, self.ORDER_NO_2, self.ORDER_NO_3},
            "对照前提不成立：没有 flag 的人也看不全三张，本用例的『少行』就无法归因到 flag",
        )

        restricted = self._cs(
            "cs-mine", "order.list", "order.restrict_to_self_created"
        )
        self.assertEqual(
            self._list_order_nos(restricted),
            {self.ORDER_NO_1, self.ORDER_NO_2},
        )

    def test_restrict_flag_narrows_the_list_but_not_the_detail(self):
        """`restrict_to_self_created` **只收窄列表**，不让详情 404（Ruling 9）。⚠️ 为什么详情那条是承重的：`retrieve` 与所有 detail 动作都经 `get_object()`，而 `get_object()` 走的是 `self.filter_queryset(self.get_queryset())`（`rest_framework/generics.py:87`）⇒ 若把 flag 施加在 `get_queryset()` 上，受限用户看**别人的单**会拿到 **404**——与"这单真的不存在"不可区分；而 flag 的失败形态（spec §5.2）是"数据被过滤"，不是"存在的行看起来不存在"。
        ⚠️ 本用例**必须配对照**：同一张 `someone-else` 的单，**详情 200** 而**列表里没有**。只断列表没有它的话，"`get_queryset()` 无条件按自己过滤"那种坏实现也会绿（它会让同一张单的详情也 404）——配对断言才把"只收窄列表"与"哪都收窄"分开。"""
        from apps.business.models import Order

        Order.objects.filter(order_no=self.ORDER_NO_1).update(created_by="cs-both")
        Order.objects.create(
            game=self.game, order_no=self.ORDER_NO_3, created_by="someone-else"
        )
        client = self._cs(
            "cs-both",
            "order.list",
            "order.view_detail",
            "order.restrict_to_self_created",
        )

        # ① 详情：**别人的**单仍然 200（不是 404）
        detail = client.get(f"/api/business/orders/{self.ORDER_NO_3}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["code"], 0, detail.json())
        self.assertEqual(detail.json()["data"]["order_no"], self.ORDER_NO_3)

        # ② 列表：**同一张单**必须被滤掉，自己那张留下
        self.assertEqual(self._list_order_nos(client), {self.ORDER_NO_1})

    def test_create_records_the_authenticated_username_even_if_the_body_says_otherwise(self):
        """录单的 `created_by` 是**认证态用户名**，不是请求体的 `actor`（Ruling 7）。⚠️ 为什么承重（两件事必须一起钉）：① `被抢走` 通知把 `created_by` 当 **username** 解析（`apps/business/notifications.py:70-75`，解析不到就不写行）；② `order.restrict_to_self_created` 按 `created_by` 过滤（`order_flags.scope_by_creator`）。
        若它仍是客户端自选的值（前端送 `display_name || username`），限制就会按一个可伪造的值过滤——要么失效、要么错人。
        所以本用例同时断言：**写进去的是真用户名**，且同一用户带 flag 时**确实看得到自己刚录的单**、看不到别人录的。"""
        from apps.business.models import Order

        creator = self._cs(
            "cs-creator",
            "order.add_edit",
            "order.list",
            "order.restrict_to_self_created",
        )
        body = creator.post(
            self.LIST_URL,
            {
                "game": self.game.id,
                "project_text": "满星",
                "total_price": "199.50",
                # 请求体里写别人：这个值**不得**进 `created_by`
                "actor": "someone-else",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        created_no = body["data"]["order_no"]
        self.assertEqual(Order.objects.get(order_no=created_no).created_by, "cs-creator")

        # 让"少行"有判别力：creator 名下共 2 张，另有 2 张属于别人
        # （`ORDER_NO_1` 是 setUp 里 `cs-a` 录的，`ORDER_NO_2` 是 `cs-someone-else` 录的）。
        Order.objects.create(
            game=self.game, order_no=self.ORDER_NO_2, created_by="cs-someone-else"
        )
        Order.objects.create(
            game=self.game, order_no=self.ORDER_NO_3, created_by="cs-creator"
        )
        self.assertEqual(
            self._list_order_nos(creator),
            {created_no, self.ORDER_NO_3},
        )

    def test_anonymous_request_is_401_not_403(self):
        """匿名请求的 **401 契约**（P5 §4.2：401 = 没登录，403 = 登录了但没权限）。本条**不能**分辨"装配 `get_permissions()` 时有没有丢掉 `IsAuthenticated`"：`permission_denied()` 只要 `request.authenticators` 非空、`request.successful_authenticator` 为空就抛 `NotAuthenticated`（401；`rest_framework/views.py:174-180`，与哪个权限类拒的、有没有碰 `.user` 无关），而本项目的认证器对匿名返回 `None`（`apps/core/authentication.py:14-15`）、`DEFAULT_AUTHENTICATION_CLASSES` 非空 ⇒ 条件恒成立——就算把 `IsAuthenticated` 整个从并集里删掉，`has_node_now` 拒匿名照样 401，本条不会红。
        所以"并集里有没有 `IsAuthenticated`"由 `PermissionAssemblyTests` 的**结构断言**钉住（那才是能红的判据）；
        本条保留为**对外契约**：匿名打这个端点必须 401、不能是 403（前端据状态码分支，不读 msg）。"""
        from django.test import Client

        resp = Client(SERVER_NAME="tenant.test.com").get(self.LIST_URL)
        self.assertEqual(resp.status_code, 401)
        # 顺手排除"401 是因为 body 里拿了没登录的 request.user 而炸"这类旁路：
        # 响应必须是标准信封（P1 的异常处理器产出的），不是 Django 的 HTML 页。
        self.assertEqual(resp.json()["code"], 401, resp.content[:200])


class PermissionAssemblyTests(TenantTestCase):
    """`get_permissions()` 的**并集结构**——不经 HTTP，直接看装配出来的权限类。
    ⚠️ 为什么必须用结构断言：`IsAuthenticated` 在不在并集里**无法**由匿名请求的 401 验证（见 `test_anonymous_request_is_401_not_403` 的说明：`permission_denied` 会把任何权限类的拒绝都翻成 401）。
    能红的判据只能是"装配结果里有没有它、顺序对不对、别的默认类有没有被丢掉"。"""

    def _classes(self, *permission_classes):
        from apps.business.api.order_views import OrderViewSet

        view = OrderViewSet()
        view.action = "list"
        if permission_classes:
            view.permission_classes = list(permission_classes)
        return view.get_permissions()

    def test_list_assembly_is_isauthenticated_then_node_gate(self):
        """默认配置下：`[IsAuthenticated, HasNode(list 的 codes)]`，顺序固定。"""
        from rest_framework.permissions import IsAuthenticated

        from apps.rbac.permissions import HasNode

        classes = self._classes()
        self.assertEqual(len(classes), 2, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        # 绑定的是**这个 action** 的 code（不只是"有个节点门"）
        self.assertEqual(classes[1].codes, ("order.list",))

    def test_the_union_keeps_every_other_default_permission_class(self):
        """第二条默认类必须**活下来**，且落在节点门**之后**（并集不是"只留 IsAuthenticated"）。"""
        from rest_framework.permissions import BasePermission, IsAuthenticated

        from apps.rbac.permissions import HasNode

        class SecondDefault(BasePermission):
            def has_permission(self, request, view):
                return True

        classes = self._classes(IsAuthenticated, SecondDefault)
        self.assertEqual(len(classes), 3, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        self.assertIs(type(classes[2]), SecondDefault)

    def test_an_isauthenticated_subclass_is_not_dropped(self):
        """`IsAuthenticated` 的**子类**不能被当成同一个类丢掉。
        ⚠️ 判别力：去重若写成 `isinstance(p, IsAuthenticated)`，`MyAuth` 会被一并丢弃 ⇒ 少一条权限类而**没有任何报错**（正是"其余默认类一条都不丢"要防的事）。
        本用例断言它仍在列表里。"""
        from rest_framework.permissions import IsAuthenticated

        from apps.rbac.permissions import HasNode

        class MyAuth(IsAuthenticated):
            """假想的自定义认证类（今天不存在，但它证明去重判等是精确的）。"""

        classes = self._classes(IsAuthenticated, MyAuth)
        self.assertEqual(len(classes), 3, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        self.assertIs(type(classes[2]), MyAuth)

    def test_an_unregistered_action_gets_the_default_classes_untouched(self):
        """未登记在表里的 action：**照原样**返回默认类（fail-closed 到"仍要登录"）。
        ⚠️ 这条钉的是"忘记登记 ≠ 默认放行"：装配结果里既没有节点门、也没有被加进 `IsAuthenticated` 之外的东西。"""
        from rest_framework.permissions import IsAuthenticated

        from apps.business.api.order_views import OrderViewSet
        from apps.rbac.permissions import HasNode

        view = OrderViewSet()
        view.action = "destroy"          # 表里没有它
        classes = view.get_permissions()
        self.assertEqual([type(c) for c in classes], [IsAuthenticated])
        self.assertFalse(any(issubclass(type(c), HasNode) for c in classes))


class LongUsernameCreationTests(TenantTestCase):
    """>64 字符的 username 录单必须成功、且 `created_by` **不被截断**（Ruling 8）。⚠️ 为什么值得单独一条：`Order.created_by` 曾是 `varchar(64)`，而 Django 的 `User.username` 上限是 **150**、本项目账号接口也放行 150（`apps/accounts/serializers.py` 的 `username = CharField(max_length=150)`）⇒ 认证态用户名进这一列会 `DataError` → **HTML 500**（修前实测探针 100 字符用户名得 `status=500` 的 500 HTML 页——本用例修前红、修后绿）。
    ⚠️ 修法是**统一上限**（列与 `ActorSerializer` 都对齐 `User.username` 的 150），**不是截断**：截断会把一个真用户的用户名写成另一个不存在的名字，`被抢走` 通知（按 username 解析收件人）就再也找不到人——失败会从"响亮 500"变成"通知静默不送达"。
    本用例因此同时断"值相等"与"长度未被截断"。"""

    URL = "/api/business/orders/"

    def test_a_username_longer_than_64_can_create_an_order_without_truncation(self):
        from apps.business.models import Game, Order

        long_username = "u" * 100
        self.assertGreater(len(long_username), 64)   # 前置：这条用例真的越过了旧上限
        client = make_logged_in_client(
            username=long_username,
            groups=("录入组",),          # 非职位组：录单不需要职位，只需要 order.add_edit
            nodes=("order.add_edit",),
        )
        game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 录单是**写**路径（`assert_game_allowed`）⇒ 不显式授这款游戏会拿到业务码 1000，
        # 而本条要测的是「长用户名不被截断」，不是游戏权限。
        grant_games(User.objects.get(username=long_username), game)
        response = client.post(
            self.URL,
            {"game": game.id, "project_text": "满星", "total_price": "1.00"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.created_by, long_username)
        self.assertEqual(len(created.created_by), len(long_username))


class DispatchActionNodeGateTests(TenantTestCase):
    """派单四个动作 + 三个改价入口 + `/review` 的节点门。覆盖形状：**每个端点一对**——「持判据 ⇒ 200」+「持无关节点 ⇒ 403」；判别力来自这对配对：DRF 对没有 `message` 的权限类给的是同一句默认文案，单看拒绝响应区分不出是节点门拒的还是别的门拒的。
    ⚠️ **职位组与节点组分开挂**（本类与 `OrderEndpointNodeGateTests` 唯一的夹具差别）：本类覆盖的端点里，`/review` **仍有函数体职位门**（客服主管 / 系统管理员；单轴化后由 `performance_views` 的 Group 判据承担），而 `/assign`、`/submit`、`/claim` 的职位门已随单轴化**删除**——只剩类级节点门。
    夹具沿用「职位组只负责建组、节点挂在本用例专属的『节点N』组」的形状：两个 client 若把节点挂在同一个职位组上，`Role.name` 唯一（`_grant_nodes` 用 `get_or_create`）⇒ 后一个的 nodes 会**加在前一个上**，「无关判据 ⇒ 403」就永远断不出来。"""

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="原神")

    # ── 夹具 ─────────────────────────────────────────────────────
    def _client(self, username, role_group, *codes):
        """真实登录：`role_group` 用于建职位组（`/review` 仍看它），节点挂在专属「节点N」组。"""
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            username=username, groups=(role_group, node_group)
        )
        user = User.objects.get(username=username)
        self._grant(user, node_group, codes)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类的订单都归 `self.game`：读侧（列表 / 详情 / 大厅 / 取绩效）与写侧（抛单 / 撤回 / 指派 / 清除接单人 / 提交 / 改价 / 流转 / 抢单）都按它收窄
        # ⇒ 每个发请求的账号都要**显式**授这款游戏，否则拿到的是 1002 / 1000，而不是本用例要断的那处节点判据。⛔ 节点判据一个字没动。
        grant_games(user, self.game)
        return client, user

    @staticmethod
    def _grant(user, group_name, codes):
        from apps.rbac.models import PermissionNode, Role

        group = user.groups.get(name=group_name)
        role, _ = Role.objects.get_or_create(name=group_name, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))

    def _order(self, no, status, *, user=None, hall_price="88.00", review_status=None):
        """建一对「订单 + MAIN 绩效」（状态机的进池钩子与 `/submit` 都要求那一行）。"""
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=None if hall_price is None else Decimal(hall_price),
        )
        perf = Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=user,
            temp_amount=Decimal("88.00"),
            review_status=review_status,
        )
        return order, perf

    @staticmethod
    def _post(client, url, payload):
        return client.post(url, payload, content_type="application/json")

    # ── 逐端点：有判据 ⇒ 200 / 无关判据 ⇒ 403 ────────────────────
    def test_throw_to_hall_requires_the_send_to_grab_hall_node(self):
        """⭐（计划 ④）类级门是**独占** 36。
        「只持 20」那一侧的拒绝判据**不在这条里**（它单独由 `tests/test_rbac_throw_hall_gate.py` 的钉子①钉住，避免同一条用例既断正门又断反门）。"""
        order, _ = self._order("20260921000000000101", OrderStatus.UNASSIGNED)
        allowed, _ = self._client("gate-throw-ok", "客服", "order.send_to_grab_hall")
        denied, _ = self._client("gate-throw-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/throw-to-hall/"
        self.assertEqual(self._post(denied, url, {}).status_code, 403)
        ok = self._post(allowed, url, {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_withdraw_requires_the_withdraw_node(self):
        order, _ = self._order("20260921000000000102", OrderStatus.WAITING_GRAB)
        allowed, _ = self._client(
            "gate-withdraw-ok", "客服", "order.withdraw_from_grab_hall"
        )
        denied, _ = self._client("gate-withdraw-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/withdraw/"
        self.assertEqual(self._post(denied, url, {}).status_code, 403)
        ok = self._post(allowed, url, {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_assign_requires_the_assign_node(self):
        order, _ = self._order("20260921000000000103", OrderStatus.UNASSIGNED)
        taker = User.objects.create_user(username="gate-assign-target")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（本条考的是节点门）。
        grant_games(taker, self.game)
        allowed, _ = self._client("gate-assign-ok", "客服", "order.assign_taker")
        denied, _ = self._client("gate-assign-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/assign/"
        payload = {"user_id": taker.id}
        self.assertEqual(self._post(denied, url, payload).status_code, 403)
        ok = self._post(allowed, url, payload)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_clear_taker_requires_the_clear_taker_node(self):
        taker = User.objects.create_user(username="gate-clear-target")
        order, _ = self._order(
            "20260921000000000104", OrderStatus.IN_PROGRESS, user=taker
        )
        allowed, _ = self._client("gate-clear-ok", "客服", "order.clear_taker")
        denied, _ = self._client("gate-clear-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/clear-taker/"
        self.assertEqual(self._post(denied, url, {}).status_code, 403)
        ok = self._post(allowed, url, {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_submit_requires_the_submit_node(self):
        """拒绝侧的 403 **只能**来自节点门：他是**另一张单的接单人**。
        ⚠️ 不能拿「不是接单人」的账号当拒绝侧——`submit()` 里还有一条 `PermissionDenied("该单不是你接的，不能提交")`（资源级授权），删掉 `ORDER_PERMISSIONS["submit"]` 后它照样 403 ⇒ 用例恒绿、证明不了节点门。
        所以拒绝侧也建**自己名下**的订单，让角色门、归属门、来源态门全部通过。"""
        allowed, taker = self._client(
            "gate-submit-ok", "代练员", "order.submit_work"
        )
        denied, other_taker = self._client(
            "gate-submit-no", "代练员", "order.list"
        )
        order, _ = self._order(
            "20260921000000000105", OrderStatus.IN_PROGRESS, user=taker
        )
        other_order, _ = self._order(
            "20260921000000000115", OrderStatus.IN_PROGRESS, user=other_taker
        )
        denied_url = f"/api/business/orders/{other_order.order_no}/submit/"
        self.assertEqual(self._post(denied, denied_url, {}).status_code, 403)
        ok = self._post(allowed, f"/api/business/orders/{order.order_no}/submit/", {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_price_requires_a_price_node(self):
        order, _ = self._order("20260921000000000106", OrderStatus.UNASSIGNED)
        allowed, _ = self._client(
            "gate-price-ok", "客服", "order.submit_payout_price"
        )
        denied, _ = self._client("gate-price-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/price/"
        payload = {"total_price": "99.00", "reason": "节点门用例"}
        self.assertEqual(self._post(denied, url, payload).status_code, 403)
        ok = self._post(allowed, url, payload)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_transition_requires_the_change_status_node(self):
        """通用 `/transition/` 的**默认**门仍是 16（目标 ≠ 已完成 / 待抢单）。
        ⚠️ 样本单的目标态用 `ISSUE`：待抢单目标已改判 36，继续拿它当「16 就能改状态」的样本会变成断错门；
        36 那一侧的两向判据在 `tests/test_rbac_throw_hall_gate.py` 的钉子④。"""
        order, _ = self._order("20260921000000000107", OrderStatus.UNASSIGNED)
        allowed, _ = self._client(
            "gate-transition-ok", "客服", "order.change_status"
        )
        denied, _ = self._client("gate-transition-no", "客服", "order.list")
        url = f"/api/business/orders/{order.order_no}/transition/"
        payload = {"target": OrderStatus.ISSUE}
        self.assertEqual(self._post(denied, url, payload).status_code, 403)
        ok = self._post(allowed, url, payload)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_claim_requires_the_take_order_node(self):
        order, _ = self._order("20260921000000000108", OrderStatus.WAITING_GRAB)
        allowed, _ = self._client("gate-claim-ok", "代练员", "grab_hall.take_order")
        denied, _ = self._client("gate-claim-no", "代练员", "order.list")
        url = f"/api/business/hall/{order.order_no}/claim/"
        self.assertEqual(self._post(denied, url, {}).status_code, 403)
        ok = self._post(allowed, url, {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    def test_review_requires_the_change_status_completed_node(self):
        taker = User.objects.create_user(username="gate-review-taker")
        order, perf = self._order(
            "20260921000000000109",
            OrderStatus.PENDING_REVIEW,
            user=taker,
            review_status=ReviewStatus.PENDING,
        )
        allowed, _ = self._client(
            "gate-review-ok", "客服主管", "order.change_status_completed"
        )
        denied, _ = self._client("gate-review-no", "客服主管", "order.list")
        url = f"/api/business/performances/{perf.id}/review/"
        payload = {"approved": True}
        self.assertEqual(self._post(denied, url, payload).status_code, 403)
        ok = self._post(allowed, url, payload)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())

    # ── body 层的条件判据（带价才判）──────────────────────────────
    def test_throw_to_hall_with_a_price_needs_the_price_node(self):
        """类级门（独占 36）只回答「为什么能到这个端点」；带价要**单独**再问改价 code。
        配对：同一个 client、同一条路径——带价 403（body 层），不带价 200（类级门通过）。
        ⚠️ 夹具持 36、**刻意不持 1162**：这样「带价那半」仍判别得出——若把类级门与价格闸并成一道，本用例会红。"""
        order, _ = self._order("20260921000000000111", OrderStatus.UNASSIGNED)
        cs, _ = self._client("body-throw-cs", "客服", "order.send_to_grab_hall")
        url = f"/api/business/orders/{order.order_no}/throw-to-hall/"
        priced = self._post(cs, url, {"price": "66.00"})
        self.assertEqual(priced.status_code, 403)
        self.assertIn("你没有改接单价的权限", priced.json()["msg"])
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("88.00"))     # 没被写
        plain = self._post(cs, url, {})
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.json()["code"], 0, plain.json())

    def test_assign_with_a_price_needs_the_price_node(self):
        order, _ = self._order("20260921000000000112", OrderStatus.UNASSIGNED)
        taker = User.objects.create_user(username="body-assign-target")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（本条考的是带价节点门）。
        grant_games(taker, self.game)
        cs, _ = self._client("body-assign-cs", "客服", "order.assign_taker")
        url = f"/api/business/orders/{order.order_no}/assign/"
        priced = self._post(cs, url, {"user_id": taker.id, "price": "66.00"})
        self.assertEqual(priced.status_code, 403)
        self.assertIn("你没有改接单价的权限", priced.json()["msg"])
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("88.00"))
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        plain = self._post(cs, url, {"user_id": taker.id})
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.json()["code"], 0, plain.json())

    def test_adjust_has_two_layers_and_the_restrict_flag_denies(self):
        """`/adjust` 的**两层**（Ruling 12）：类级 `order.submit_payout_price` + body 层 flag。
        - 只持节点、没有限制 flag ⇒ 200 且**真的写入**（flag fail-open 是现行取值口径）；
        - 持节点但被 `order.restrict_edit_taker_price` 限制 ⇒ **仍然 200 / code 0**，但 `taker_price` **没被写**——flag 的失败表现**不是 403**（spec §4.2 / §5.2）。"""
        allowed, _ = self._client(
            "body-adjust-ok", "客服", "order.submit_payout_price"
        )
        restricted, _ = self._client(
            "body-adjust-flag",
            "客服",
            "order.submit_payout_price",
            "order.restrict_edit_taker_price",
        )
        ok_taker = User.objects.create_user(username="body-adjust-taker-ok")
        no_taker = User.objects.create_user(username="body-adjust-taker-flag")
        _, ok_perf = self._order(
            "20260921000000000113", OrderStatus.IN_PROGRESS, user=ok_taker
        )
        _, no_perf = self._order(
            "20260921000000000114", OrderStatus.IN_PROGRESS, user=no_taker
        )

        ok = self._post(
            allowed,
            f"/api/business/performances/{ok_perf.id}/adjust/",
            {"taker_price": "66.00"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())
        ok_perf.refresh_from_db()
        self.assertEqual(ok_perf.taker_price, Decimal("66.00"))

        denied = self._post(
            restricted,
            f"/api/business/performances/{no_perf.id}/adjust/",
            {"taker_price": "66.00"},
        )
        self.assertEqual(denied.status_code, 200, denied.json())
        self.assertEqual(denied.json()["code"], 0, denied.json())
        self.assertEqual(denied.json()["msg"], "接单价已按权限忽略")
        no_perf.refresh_from_db()
        self.assertIsNone(no_perf.taker_price)

    def test_adjust_restrict_flag_ignores_taker_price_but_applies_the_rest(self):
        """flag 只挡接单价这一项：同一载荷里的合法字段照常应用，且**不是 403**。
        spec §4.2 / §5.2：flag 的失败表现是「数据被过滤 / 字段脱敏 / 按钮显隐，**不是 403**」；对标 1165 的原文是「只能编辑其它的内容」。
        判据必须**同时**钉住两件事——`taker_price` 没被写 **和** `deduct_mode` 真的落库；只看其一，「整个请求被丢掉不处理」的实现也能蒙混过去。"""
        restricted, _ = self._client(
            "body-adjust-mixed",
            "客服",
            "order.submit_payout_price",
            "order.restrict_edit_taker_price",
        )
        taker = User.objects.create_user(username="body-adjust-mixed-taker")
        _, perf = self._order(
            "20260921000000000117", OrderStatus.IN_PROGRESS, user=taker
        )
        resp = self._post(
            restricted,
            f"/api/business/performances/{perf.id}/adjust/",
            {"taker_price": "66.00", "deduct_mode": "1", "deduct_value": "10.00"},
        )
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertEqual(resp.json()["code"], 0, resp.json())
        self.assertIn("接单价已按权限忽略", resp.json()["msg"])
        perf.refresh_from_db()
        self.assertIsNone(perf.taker_price)                 # 被忽略的那个键
        self.assertEqual(perf.deduct_mode, "1")             # 合法字段**真的**落库
        self.assertEqual(perf.deduct_value, Decimal("10.00"))

    def test_adjust_requires_the_submit_payout_price_node(self):
        """`/adjust` 的**类级**门 = `order.submit_payout_price`（有 ⇒ 200 / 无关节点 ⇒ 403）。
        与 `test_adjust_has_two_layers_and_the_restrict_flag_denies` 配对：那条钉的是 **body 层 flag**，本条钉的是**类级节点门**——删掉 `PERFORMANCE_PERMISSIONS["adjust"]` 必须让**一条 HTTP 用例**变红，而不是只让结构断言（`PerformancePermissionAssemblyTests`）红。
        拒绝侧是**客服**（角色闸门通过），所以 403 只能来自节点门。"""
        allowed, _ = self._client(
            "gate-adjust-ok", "客服", "order.submit_payout_price"
        )
        denied, _ = self._client("gate-adjust-no", "客服", "order.list")
        _, perf = self._order("20260921000000000116", OrderStatus.IN_PROGRESS)
        url = f"/api/business/performances/{perf.id}/adjust/"
        self.assertEqual(self._post(denied, url, {}).status_code, 403)
        ok = self._post(allowed, url, {})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], 0, ok.json())


class PerformancePermissionAssemblyTests(TenantTestCase):
    """`PerformanceViewSet.get_permissions()` 的**并集结构**（Ruling 12 的第二个装配点）。
    ⚠️ 为什么不靠 HTTP 判：`IsAuthenticated` 在不在并集里**无法**由匿名请求的 401 验证（`permission_denied` 会把任何权限类的拒绝都翻成 401，理由见 `OrderEndpointNodeGateTests.test_anonymous_request_is_401_not_403`）——能红的判据只能是装配结果本身。"""

    def _classes(self, action, *permission_classes):
        from apps.business.api.performance_views import PerformanceViewSet

        view = PerformanceViewSet()
        view.action = action
        if permission_classes:
            view.permission_classes = list(permission_classes)
        return view.get_permissions()

    def test_each_action_binds_its_own_code_after_isauthenticated(self):
        from rest_framework.permissions import IsAuthenticated

        from apps.rbac.permissions import HasNode

        expected = {
            # ⭐ D-0219：列表门任一即进（1163 ∪ order.list）——
            # 「不勾 1163 的客服进不了绩效列表、改不了打手绩效」的修复落点。
            "list": ("my_performance.export", "order.list"),
            "adjust": ("order.submit_payout_price",),
            "review": ("order.change_status_completed",),
        }
        for action, codes in expected.items():
            with self.subTest(action=action):
                classes = self._classes(action)
                self.assertEqual(len(classes), 2, classes)
                self.assertIs(type(classes[0]), IsAuthenticated)
                self.assertTrue(issubclass(type(classes[1]), HasNode))
                self.assertEqual(classes[1].codes, codes)

    def test_the_union_keeps_every_other_default_permission_class(self):
        from rest_framework.permissions import BasePermission, IsAuthenticated

        from apps.rbac.permissions import HasNode

        class SecondDefault(BasePermission):
            def has_permission(self, request, view):
                return True

        classes = self._classes("review", IsAuthenticated, SecondDefault)
        self.assertEqual(len(classes), 3, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        self.assertIs(type(classes[2]), SecondDefault)


#: 两个 `flag` 的 code（ 的常量在这里写**字面量**：结构兜底的 B2 走 AST 取字符串常量）。
OFFER_FLAG = "order.restrict_view_offer_price"
TAKER_FLAG = "order.restrict_view_taker_price"

#: DRF 对**没有自定义 `message` 的权限类**给的默认 403 文案（本项目 zh-hans 下的实测值）。类级节点门 `HasNode` 就是这一类
#: ⇒ "msg 是不是这句"可以用来**指认拒绝来自节点门**；职位门抛的是自定义文案，不可能命中这一句。
#: ⚠️ 期望值写字面量（E-0024），不引用 DRF 内部符号。
NODE_DENIED_MSG = "您没有执行该操作的权限"


class PriceRedactionTests(TenantTestCase):
    """两个价格 flag 的**读取脱敏**：值变 `None`、**键仍在**、两条互不蕴含。
    ⚠️ **落点按代码现状，不按名字**（实测：`apps/business/api/serializers.py`）：`hall_price` 出现在 `OrderSerializer`（`exclude=[]`）、`OrderListSerializer` 与 `HallListSerializer` 里——spec §5 / C6（第二批列）起 `/orders/` 列表也带这个键（「发单价」列要读它），所以矩阵打在**订单详情 / 大厅列表 / 绩效列表**三处仍成立（列表这一处的脱敏由 `tests/test_order_list_columns.py::ListOfferPriceTests` 覆盖）；`taker_price` 只出现在 `PerformanceListSerializer` 里（订单读序列化器不暴露接单价）。
    ⚠️ 每个 client 一个**专属节点组**：`Role.name` 唯一、节点挂 `Role` 上，两个 client 同组会让后一个的 nodes **加在**前一个上——「不带 flag」的对照会继承 flag、整张矩阵塌成一个方向。"""

    ORDER_NO = "20260921000000000201"          # 大厅 / 详情：hall_price=88.00
    PERF_ORDER_NO = "20260921000000000202"     # 绩效列表：taker_price=66.00

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no=self.ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
            created_by="redaction-seed",
        )
        # 大厅/详情那条的 MAIN 绩效**不带覆盖价** ⇒ 生效接单价 = 订单默认价 88.00。
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        # 第二条订单/绩效专门承载 `taker_price`。与大厅那条分开是刻意的：生效接单价会把
        # MAIN 行的 `taker_price` 顶上去（Ruling 44），合在一条上会让大厅期望值变 66.00、
        # 把两个落点耦在一起。
        self.perf_order = Order.objects.create(
            game=self.game,
            order_no=self.PERF_ORDER_NO,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("99.00"),
        )
        # ⚠️ **必须带归属人**：绩效列表**排除 `user IS NULL` 的行**（`performance_queryset`），而本类要用列表端点验脱敏 ⇒ 没有归属人的行根本不在列表里（下面 `next(...)` 会 StopIteration）。
        # 大厅那条（`self.order` 的绩效）仍**不带**归属人——它要留在抢单池里，本类不读它的绩效列表。
        self.perf_taker = User.objects.create_user(username="redaction-perf-taker")
        self.perf = Performance.objects.create(
            order=self.perf_order,
            emp_type=EmpType.MAIN,
            user=self.perf_taker,
            temp_amount=Decimal("66.00"),
            taker_price=Decimal("66.00"),
        )

    def _client(self, username, *codes, role_group="客服"):
        """真实登录：`role_group` 只用于建职位组，节点挂在本 client 专属的「节点N」组。"""
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(username=username, groups=(role_group, node_group))
        user = User.objects.get(username=username)
        from apps.rbac.models import PermissionNode, Role

        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 本类三个读落点里，订单详情与大厅都按 `self.game` 收窄（绩效列表不过游戏卡点）⇒ 发请求的账号要**显式**授这款游戏，否则拿到空集 / 1002，脱敏矩阵根本走不到。
        # ⛔ 两条 flag 的授予与断言一个字没动，此处只加游戏可见范围。
        grant_games(user, self.game)
        return client

    def _assert_price(self, payload, key, visible, *, hidden):
        """键**恒在**；`hidden` 时值为 `None`，否则等于字面期望值（E-0024：钉字面值）。"""
        self.assertIn(key, payload, f"{key} 的键必须恒在（前端契约）")
        if hidden:
            self.assertIsNone(payload[key], payload)
        else:
            self.assertEqual(Decimal(payload[key]), visible, payload)

    def _detail_hall_price(self, client):
        body = client.get(f"/api/business/orders/{self.ORDER_NO}/").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_redaction_matrix_hides_only_the_flag_that_is_held(self):
        """四条组合：只施加一条时另一条照常可见；两条互不蕴含。"""
        combos = (
            ((), False, False),
            ((OFFER_FLAG,), True, False),
            ((TAKER_FLAG,), False, True),
            ((OFFER_FLAG, TAKER_FLAG), True, True),
        )
        for index, (flags, hide_offer, hide_taker) in enumerate(combos):
            with self.subTest(flags=flags):
                detail = self._client(
                    f"redact-detail-{index}", "order.view_detail", *flags
                )
                self._assert_price(
                    self._detail_hall_price(detail),
                    "hall_price",
                    Decimal("88.00"),
                    hidden=hide_offer,
                )

                hall = self._client(
                    f"redact-hall-{index}",
                    "grab_hall.take_order",
                    *flags,
                    role_group="代练员",
                )
                hbody = hall.get("/api/business/hall/").json()
                self.assertEqual(hbody["code"], 0, hbody)
                hrow = next(
                    row
                    for row in hbody["data"]["items"]
                    if row["order_no"] == self.ORDER_NO
                )
                self._assert_price(
                    hrow, "hall_price", Decimal("88.00"), hidden=hide_offer
                )

                perf = self._client(
                    f"redact-perf-{index}",
                    "my_performance.export",
                    # ：绩效列表按数据范围收窄 ⇒ 本用例要看**全部**行（含 `self.perf`）
                    # 才能验脱敏，故显式给管理侧节点 `order.list`。
                    "order.list",
                    *flags,
                )
                pbody = perf.get("/api/business/performances/").json()
                self.assertEqual(pbody["code"], 0, pbody)
                prow = next(
                    row for row in pbody["data"]["items"] if row["id"] == self.perf.id
                )
                self._assert_price(
                    prow, "taker_price", Decimal("66.00"), hidden=hide_taker
                )

    def test_redaction_hides_a_value_not_rows(self):
        """脱敏**不丢行**：行数与键集合在带 flag / 不带 flag 之间一致。
        ⚠️ 为什么与矩阵用例分开：矩阵断的是"值变 None 且键在"，本条断的是"限制不产生/吞掉整行"
        （一个把脱敏写成 `.exclude()` 或删键的实现能过其中一条、过不了另一条）。"""
        extra = Order.objects.create(
            game=self.game,
            order_no="20260921000000000203",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("10.00"),
        )
        # ⚠️ `extra` 的绩效**刻意不带归属人**：这张单要留在抢单池里凑「大厅两行」那条判据
        # （池内 = 没有归属人，见下方 `plain_rows` 期望值 2）。它因此**不进绩效列表**
        # ——下面绩效列表的期望值随之是 1（只有 setUp 的 `self.perf`，它带了归属人）。
        Performance.objects.create(
            order=extra, emp_type=EmpType.MAIN, temp_amount=Decimal("10.00")
        )
        plain = self._client(
            "count-hall-plain", "grab_hall.take_order", role_group="代练员"
        )
        hidden = self._client(
            "count-hall-hidden",
            "grab_hall.take_order",
            OFFER_FLAG,
            role_group="代练员",
        )
        plain_rows = plain.get("/api/business/hall/").json()["data"]["items"]
        hidden_rows = hidden.get("/api/business/hall/").json()["data"]["items"]
        # 期望值写字面量 2：只断"两边相等"的话，一个恒返回 0 行的坏实现也会绿。
        self.assertEqual(len(plain_rows), 2, plain_rows)
        self.assertEqual(len(hidden_rows), 2, hidden_rows)
        self.assertEqual(
            {r["order_no"] for r in plain_rows}, {r["order_no"] for r in hidden_rows}
        )
        self.assertEqual(set(plain_rows[0]), set(hidden_rows[0]))

        perf_plain = self._client(
            "count-perf-plain", "my_performance.export", "order.list"
        )
        perf_hidden = self._client(
            "count-perf-hidden", "my_performance.export", "order.list", TAKER_FLAG
        )
        # 期望值写字面量 1（setUp 里唯一**带归属人**的那条 `self.perf`）；只断"两边相等"的话，一个恒返回 0 行的坏实现也会绿（1 ≠ 0）。
        # ⚠️ 这条判据的**口径**：绩效列表现在排除 `user IS NULL` 的行，而 setUp 的大厅那条与这里的 `extra` 都刻意没有归属人
        # （它们要留在抢单池里）⇒ 期望值是 1，**没有**放宽成「两边相等」。
        self.assertEqual(
            perf_plain.get("/api/business/performances/").json()["data"]["total"], 1
        )
        self.assertEqual(
            perf_hidden.get("/api/business/performances/").json()["data"]["total"], 1
        )

    def test_missing_request_context_does_not_redact(self):
        """`context["request"] is None`（内部构造）**不脱敏**——保持既有行为。
        ⚠️ 承重理由：录单入参就是 `OrderSerializer(data=...)`（**无 context**），而它必须仍然接受 `hall_price`（`tests/test_roles_and_intake_perf.py` 钉着 `temp_amount` 由它决定）。
        若"缺 request 也脱敏"，那条路径会**静默**把 `hall_price` 变只读/读成 None，且没有任何报错。"""
        from apps.business.api.serializers import (
            OrderSerializer,
            PerformanceListSerializer,
        )

        # 直接构造（无 context）与显式 `request=None` 是同一条分支
        self.assertEqual(
            Decimal(OrderSerializer(self.order).data["hall_price"]), Decimal("88.00")
        )
        self.assertEqual(
            Decimal(
                OrderSerializer(self.order, context={"request": None}).data["hall_price"]
            ),
            Decimal("88.00"),
        )
        self.assertEqual(
            Decimal(PerformanceListSerializer(self.perf).data["taker_price"]),
            Decimal("66.00"),
        )

    def test_create_still_writes_hall_price_for_a_user_holding_the_offer_flag(self):
        """脱敏只管**读**：持 `restrict_view_offer_price` 的人照样能录单带抛单价。
        ⚠️ 这条钉的是**机制选择**：若把 `hall_price` 声明成只读的 `SerializerMethodField`，录单入参会**静默丢掉**这个键（库里落 `None`），
        而响应照样是 200 + code 0——只有断言数据库里的值才看得见。"""
        client = self._client("redact-intake", "order.add_edit", OFFER_FLAG)
        body = client.post(
            "/api/business/orders/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "hall_price": "77.00",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        created = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(created.hall_price, Decimal("77.00"))     # 写进去了
        self.assertIsNone(body["data"]["hall_price"])              # 但回读里被隐藏


class HallAndMyOrdersGateTests(TenantTestCase):
    """`/hall` 与 `/my-orders` 的节点门 + 职位门。⚠️ **`/hall` 的节点判据只声明一次**：类级 `ORDER_PERMISSIONS["hall"]`；裁定**不在函数体重复**这条判据——两处同码时后执行的那处永远拒不掉一个类级门放行的人，而"看起来像安全门、实际不可达"的代码比没有更糟；`hall()` 函数体里只有**职位门**。
    ⚠️ **每个拒绝半场都断 msg，指认是哪个门拒的**：同一条路径上 403 有两个可能来源，只断状态码是非判别性的。判据形状：职位门 ⇒ msg 含「代练员」（`PermissionDenied` 的自定义文案）且**不含**默认文案；
    节点门 ⇒ msg 含 `NODE_DENIED_MSG`（DRF 对无 `message` 权限类的默认文案）且**不含**「代练员」。"""

    HALL_URL = "/api/business/hall/"
    MY_ORDERS_URL = "/api/business/my-orders/"

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260921000000000301",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )

    def _client(self, username, role_group, *codes):
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(username=username, groups=(role_group, node_group))
        user = User.objects.get(username=username)
        from apps.rbac.models import PermissionNode, Role

        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # `/hall` 是订单读路径、按 `self.game` 收窄 ⇒ 发请求的账号要**显式**授这款游戏（否则大厅恒空，而「200 + code 0」在空集上也会绿——那是假绿）。
        # ⛔ 职位门 / 节点门的判据与「无节点 ⇒ 403」的断言一个字没动。
        grant_games(user, self.game)
        return client

    # ── /hall ────────────────────────────────────────────────────
    def test_hall_allows_a_taker_holding_the_take_order_node(self):
        client = self._client("hall-both", "代练员", "grab_hall.take_order")
        resp = client.get(self.HALL_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0, resp.json())

    def test_hall_allows_a_node_holder_who_is_not_a_taker(self):
        """**只看节点、不看职位**（单轴化）：持 `grab_hall.take_order` 的非代练员（这里是「客服」）也能看大厅 ⇒ 200。
        ⚠️ 新口径下**自定义职位与内置职位走同一套节点判据**，这正是本线的目标形态（`order_views` 里那道 `Role.TAKER` 职位门已随单轴化删除，本条钉的就是这一判据）；
        「是代练员但没节点 ⇒ 403」仍由下一条（节点门）钉住。"""
        client = self._client("hall-node-only", "客服", "grab_hall.take_order")
        resp = client.get(self.HALL_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0, resp.json())

    def test_hall_rejects_a_taker_without_the_take_order_node(self):
        """**节点门**（类级 `ORDER_PERMISSIONS["hall"]`）：是代练员但没节点 ⇒ 403 + 指认节点门。
        ⚠️ 只断 403 分不出是哪个门拒的：这条路径两个门都能拒。节点门来自类级的 `HasNode`，它没有自定义 `message` ⇒ DRF 给默认文案 `NODE_DENIED_MSG`；
        本用例断言 msg **含**它、且**不含**「代练员」（后者只有职位门会说）。"""
        client = self._client("hall-role-only", "代练员")
        resp = client.get(self.HALL_URL)
        self.assertEqual(resp.status_code, 403)
        msg = resp.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("代练员", msg)

    # ── /my-orders ───────────────────────────────────────────────
    def test_my_orders_allows_a_taker_holding_the_node(self):
        client = self._client(
            "mine-both", "代练员", "my_orders.send_to_my_system"
        )
        resp = client.get(self.MY_ORDERS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0, resp.json())

    def test_my_orders_rejects_a_taker_without_the_node(self):
        """**节点门**（类级 `MY_ORDERS_PERMISSIONS["list"]`）⇒ 403 + msg 指认节点门。"""
        client = self._client("mine-role-only", "代练员")
        resp = client.get(self.MY_ORDERS_URL)
        self.assertEqual(resp.status_code, 403)
        msg = resp.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("代练员", msg)

    def test_my_orders_allows_a_node_holder_who_is_not_a_taker(self):
        """**只看节点、不看职位**（单轴化）：持 `my_orders.send_to_my_system` 的非代练员（这里是「客服」）也能读「我的订单」⇒ 200（行集仍是自己的）。
        ⚠️ `my_orders_views` 里那道 `Role.TAKER` 职位门已随单轴化删除，本条钉的就是这一判据；
        「是代练员但没节点 ⇒ 403」仍由上一条（节点门）钉住。"""
        client = self._client(
            "mine-node-only", "客服", "my_orders.send_to_my_system"
        )
        resp = client.get(self.MY_ORDERS_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0, resp.json())


class MyOrdersPermissionAssemblyTests(TenantTestCase):
    """`MyOrdersViewSet.get_permissions()` 的并集结构（与另两个装配点同形）。"""

    def _classes(self, action, *permission_classes):
        from apps.business.api.my_orders_views import MyOrdersViewSet

        view = MyOrdersViewSet()
        view.action = action
        if permission_classes:
            view.permission_classes = list(permission_classes)
        return view.get_permissions()

    def test_list_assembly_is_isauthenticated_then_the_node_gate(self):
        from rest_framework.permissions import IsAuthenticated

        from apps.rbac.permissions import HasNode

        classes = self._classes("list")
        self.assertEqual(len(classes), 2, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        self.assertEqual(classes[1].codes, ("my_orders.send_to_my_system",))

    def test_the_union_keeps_every_other_default_permission_class(self):
        from rest_framework.permissions import BasePermission, IsAuthenticated

        from apps.rbac.permissions import HasNode

        class SecondDefault(BasePermission):
            def has_permission(self, request, view):
                return True

        classes = self._classes("list", IsAuthenticated, SecondDefault)
        self.assertEqual(len(classes), 3, classes)
        self.assertIs(type(classes[0]), IsAuthenticated)
        self.assertTrue(issubclass(type(classes[1]), HasNode))
        self.assertIs(type(classes[2]), SecondDefault)


class CrossTenantOrderEndpointIsolationTests(TenantTestCase):
    """`AGENTS.md` §4：以租户 A 的凭据访问租户 B 的数据必须失败（改动过的订单读路径）。覆盖本批挂了节点门、且**能读到订单行**的路径：`GET /api/business/orders/`（列表，`order.list`）；`GET /api/business/orders/<order_no>/`（详情，`order.view_detail`）；`GET /api/business/my-orders/`（`my_orders.send_to_my_system` + 代练员职位门）；`GET /api/business/orders/export`（导出，`order.export`；成功时是 xlsx 字节，不是信封）。
    ⚠️ **判据一律用 `order_no`，不用主键**：两个 schema 的主键序列**各自从 1 开始**（`setUp` 在两边各造一张单 ⇒ 两边 pk 都是 1，`filter(id=a_pk)` 在 B 里命中的是 **B 自己的行**，断言恒真/恒假）；A / B 的订单号是**两个字面值**，且 `setUp` 用数据库行自证「A 的 schema 里没有 B 的订单号、B 的 schema 里没有 A 的订单号」⇒ 否定断言的前提**可指认**，不是靠空集凑出来的。⚠️ **失败形状**：资源不存在在出信封之前被归一化成 DRF `NotFound` ⇒ 对外是 **HTTP 200 + `code` 1002**（`apps/core/exceptions.py`），**不是 404** 也不是 403（403 是"没有节点判据"，本类里 A 的 client 有节点判据）；期望值写字面值（`1002`），不引 `ErrorCode.BUSINESS`——E-0024：断言钉**值**，不钉常量。
    ⚠️ **跨域名请求不会在响应结束时还原 schema**（`tests/test_account_isolation.py` 的模块说明有完整实测记录）：`django_tenants` 的 `TenantMainMiddleware` 只在请求**进来时** `connection.set_tenant(...)` ⇒ B 侧一切请求都走 `_get_b()`、一切 ORM 读都走 `_in_b()`，两者用 `try/finally` 把连接钉回租户 A；`setUp` 第一行也显式 `set_tenant`（挡住上一条用例的残留，保证用例可单独运行）。⚠️ **本类抓不到什么**（照 spec 要求写下，别读成"跨租户已被完全证明"）：只对**走当前 schema 下 ORM** 的查询路径有判别力——手工拼 SQL、或把 schema 固定在别处的查询路径不会被这里抓到，那是 `tests/test_p5_credential_plane.py` 与框架层的行为。"""

    LIST_URL = "/api/business/orders/"
    MY_ORDERS_URL = "/api/business/my-orders/"
    EXPORT_URL = "/api/business/orders/export"

    #: 租户 A = `TenantTestCase` 的 `test` schema（域名与既有测试同口径）。
    DOMAIN_A = "tenant.test.com"
    #: 租户 B 的域名 / schema（`.test.local` 在 `DJANGO_ALLOWED_HOSTS` 白名单里）。
    DOMAIN_B = "rbac3-xt-b.test.local"
    SCHEMA_B = "rbac3_xt_b"

    # ── 判据用的业务值：租户内唯一、且两租户取值不同（**不是主键**）──────────
    NO_A = "20260921000000000901"
    NO_B = "20260921000000000902"
    GAME_A = "跨租户-A 的游戏"
    GAME_B = "跨租户-B 的游戏"
    CS_A = "xt-iso-cs-a"
    CS_B = "xt-iso-cs-b"
    TAKER_A = "xt-iso-taker-a"
    TAKER_B = "xt-iso-taker-b"
    #: 订单导出线补的跨租户判据用账号（持 `order.export`）。
    EXPORTER_A = "xt-iso-export-a"
    EXPORTER_B = "xt-iso-export-b"

    _GROUP_SEQ = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="③线跨租户隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（见类说明）。
        connection.set_tenant(self.tenant)
        seed_catalog()

        self.cs_a = self._client(self.CS_A, "客服", "order.list", "order.view_detail")
        self.taker_a = self._client(
            self.TAKER_A, "代练员", "my_orders.send_to_my_system"
        )
        game_a = Game.objects.create(name=self.GAME_A)
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 读路径（订单列表 / 详情）按 `StaffProfile.games` 收窄 ⇒ 要读 A 侧订单的两个账号都**显式**授 `game_a`，否则列表空、详情 1002，
        # 跨租户的否定断言会因「自己那张也读不到」而失去前提（本类 setUp 的非空哨兵就是挡这个的）。
        grant_games(User.objects.get(username=self.CS_A), game_a)
        grant_games(User.objects.get(username=self.TAKER_A), game_a)
        self.order_a = Order.objects.create(
            game=game_a,
            order_no=self.NO_A,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order_a,
            emp_type=EmpType.MAIN,
            user=User.objects.get(username=self.TAKER_A),
            temp_amount=Decimal("88.00"),
        )

        # ── B 侧：同样形状，在 B 的 schema 里（登录夹具发跨域名请求，故 try/finally 还原）──
        try:
            with tenant_context(self.tenant_b):
                seed_catalog()
                self.cs_b = self._client(
                    self.CS_B, "客服", "order.list", "order.view_detail",
                    domain=self.DOMAIN_B,
                )
                self.taker_b = self._client(
                    self.TAKER_B, "代练员", "my_orders.send_to_my_system",
                    domain=self.DOMAIN_B,
                )
                game_b = Game.objects.create(name=self.GAME_B)
                # 同上（B 侧）：两个账号都要**显式**授 `game_b`，B 侧的非空哨兵才成立。
                # ⚠️ 在 B 的 schema 上下文里取用户（`tenant_context` 内），授的也是 B 的 `Game`。
                grant_games(User.objects.get(username=self.CS_B), game_b)
                grant_games(User.objects.get(username=self.TAKER_B), game_b)
                self.order_b = Order.objects.create(
                    game=game_b,
                    order_no=self.NO_B,
                    status=OrderStatus.IN_PROGRESS,
                    hall_price=Decimal("88.00"),
                )
                Performance.objects.create(
                    order=self.order_b,
                    emp_type=EmpType.MAIN,
                    user=User.objects.get(username=self.TAKER_B),
                    temp_amount=Decimal("88.00"),
                )
        finally:
            connection.set_tenant(self.tenant)

        # ── 夹具自证（**不是**用例，是承重点）：否定断言的靶子在 A 里确实不存在 ──
        self.assertTrue(Order.objects.filter(order_no=self.NO_A).exists())
        self.assertFalse(
            Order.objects.filter(order_no=self.NO_B).exists(),
            "B 的单号在 A 的 schema 里也存在——跨租户前提坏了，下面的否定断言无意义",
        )
        self.assertTrue(self._in_b(lambda: Order.objects.filter(order_no=self.NO_B).exists()))
        self.assertFalse(
            self._in_b(lambda: Order.objects.filter(order_no=self.NO_A).exists()),
            "A 的单号在 B 的 schema 里也存在——跨租户前提坏了",
        )

    # ── 工具 ───────────────────────────────────────────────────────
    def _client(self, username, role_group, *codes, domain=DOMAIN_A):
        """真实登录 client：`role_group` 承载**职位门**，另起**独立节点组**承载**节点门**。
        ⚠️ 每个 client 用**独立的节点组**（`节点{n}`）：节点挂在 `Role` 上，而 `Role.name` 唯一、与 `Group` 一对一 ⇒ 同组名会**共享**节点——本文件 `OrderEndpointNodeGateTests._cs` 的 docstring 有实测记录。
        ⚠️ `role_group` 沿用内置职位名（`客服` / `代练员`）是安全的：A / B 是**两个 schema**，同名 Group 在两边是两行，节点互不影响。"""
        type(self)._GROUP_SEQ += 1
        node_group = f"节点{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            domain=domain,
            username=username,
            groups=(role_group, node_group),
        )
        from apps.rbac.models import PermissionNode, Role

        user = User.objects.get(username=username)
        group = user.groups.get(name=node_group)
        role, _ = Role.objects.get_or_create(name=node_group, defaults={"group": group})
        role.permissions.add(*PermissionNode.objects.filter(code__in=codes))
        return client

    def _in_b(self, function):
        """在 B 的 schema 上下文里执行，结束后把连接钉回租户 A。"""
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def _get_b(self, client, url):
        """在 B 的域名上发请求，**结束后把连接钉回租户 A**（见类说明）。"""
        try:
            return client.get(url, SERVER_NAME=self.DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def _items(self, response):
        """取信封里的 `items`，先断言端点本身成功（前置哨兵）。"""
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _order_nos(self, response):
        return {row["order_no"] for row in self._items(response)}

    def _export_order_nos(self, response):
        """导出响应的数据行里**第 2 列**（系统内置单号）的集合；先断端点成功（前置哨兵）。
        ⚠️ 导出成功时**不是信封而是 xlsx 字节** ⇒ 不能读 `.json()`，要读回工作簿。
        ⚠️ 判据落在**第 2 列**（系统内置单号）：第 1 列在"有来源单号"时会回落成来源单号，而本类夹具没设 `source_order_no`（两列同值）——但仍然按列语义取系统单号那一列。"""
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        sheet = workbook_of(response)["Sheet1"]
        return {
            row[1].value for row in sheet.iter_rows(min_row=2) if row[1].value is not None
        }

    # ── 详情：越界读 = 200 + 1002（不是 404 / 403）──────────────────
    def test_a_credentials_cannot_read_the_order_that_lives_only_in_b(self):
        """A 的凭据读**只在 B 里存在**的单号 ⇒ 200 + `code` 1002 + `data` 为 None。
        正向对照用**同一个** client（`cs_a`）读自己租户的 `NO_A` ⇒ 200 + `code` 0，且回来的 `order_no` **就是** `NO_A`：没有它，"1002" 可能由"这个 client 的详情压根不可用"凑出来，与租户边界无关。
        再配 B 侧哨兵（B 用**自己的** client 读 `NO_B` ⇒ 200 + `code` 0），证明 `NO_B` 是**真实存在**的行、只是不在 A 里——否定断言因此不空。"""
        own = self.cs_a.get(f"{self.LIST_URL}{self.NO_A}/")
        self.assertEqual(own.status_code, 200, own.content[:200])
        self.assertEqual(own.json()["code"], 0, own.json())
        self.assertEqual(own.json()["data"]["order_no"], self.NO_A)

        crossed = self.cs_a.get(f"{self.LIST_URL}{self.NO_B}/")
        self.assertEqual(crossed.status_code, 200, crossed.content[:200])
        body = crossed.json()
        self.assertEqual(body["code"], 1002, body)          # 资源不存在 = 业务结果
        self.assertEqual(body["msg"], "记录不存在或已被删除")
        self.assertIsNone(body["data"])

        # 非空哨兵：B 自己的那一行**确实可读**
        ok_b = self._get_b(self.cs_b, f"{self.LIST_URL}{self.NO_B}/")
        self.assertEqual(ok_b.status_code, 200, ok_b.content[:200])
        self.assertEqual(ok_b.json()["code"], 0, ok_b.json())
        self.assertEqual(ok_b.json()["data"]["order_no"], self.NO_B)

    # ── 列表：只含本租户的行 ────────────────────────────────────────
    def test_order_list_returns_only_the_current_tenants_rows(self):
        """`/api/business/orders/`：每个租户只看到自己的订单号（两个方向都断）。"""
        a_nos = self._order_nos(self.cs_a.get(self.LIST_URL))
        self.assertIn(self.NO_A, a_nos)          # 非空哨兵
        self.assertNotIn(self.NO_B, a_nos)       # 判据：B 的订单号（不是主键）

        b_nos = self._order_nos(self._get_b(self.cs_b, self.LIST_URL))
        self.assertIn(self.NO_B, b_nos)          # 非空哨兵
        self.assertNotIn(self.NO_A, b_nos)

    def test_my_orders_returns_only_the_current_tenants_rows(self):
        """`/api/business/my-orders/`：代练员只看到**本租户**自己接的单。
        两边的代练员各自持有 `my_orders.send_to_my_system` 节点与「代练员」职位，
        所以两条路径都**真的进了视图**（否则空列表会让 `assertNotIn` 假通过——哨兵 `assertIn` 就是挡这个的）。"""
        a_nos = self._order_nos(self.taker_a.get(self.MY_ORDERS_URL))
        self.assertIn(self.NO_A, a_nos)
        self.assertNotIn(self.NO_B, a_nos)

        b_nos = self._order_nos(self._get_b(self.taker_b, self.MY_ORDERS_URL))
        self.assertIn(self.NO_B, b_nos)
        self.assertNotIn(self.NO_A, b_nos)

    def test_export_returns_only_the_current_tenants_rows(self):
        """`/api/business/orders/export`（订单导出线）：A 的凭据导不出 B 的单（两个方向都断）。
        ⚠️ 判据用**订单号**（租户内唯一、两租户取值不同），⛔ 不用主键——两个 schema 的主键序列各自从 1 开始（见类说明）。
        ⚠️ 导出账号在本用例里现建（`setUp` 的三个 client 都没挂 `order.export`），并**显式**补授本租户的游戏：`setUp` 的三个 client 都是在建游戏**之前**建的，夹具"当前租户全部游戏"那一支当时看不到 `game_a` / `game_b`（`setUp` 里那两处 `grant_games` 同因）；本用例不依赖夹具默认值，与那两处保持同一写法。"""
        exporter_a = self._client(self.EXPORTER_A, "客服", "order.export")
        grant_games(
            User.objects.get(username=self.EXPORTER_A),
            Game.objects.get(name=self.GAME_A),
        )
        a_nos = self._export_order_nos(exporter_a.get(self.EXPORT_URL))
        self.assertIn(self.NO_A, a_nos)          # 非空哨兵：导出的确实是本租户那一行
        self.assertNotIn(self.NO_B, a_nos)       # 判据：B 的订单号（不是主键）

        exporter_b = self._in_b(lambda: self._client(
            self.EXPORTER_B, "客服", "order.export", domain=self.DOMAIN_B))
        self._in_b(lambda: grant_games(
            User.objects.get(username=self.EXPORTER_B),
            Game.objects.get(name=self.GAME_B),
        ))
        b_nos = self._export_order_nos(self._get_b(exporter_b, self.EXPORT_URL))
        self.assertIn(self.NO_B, b_nos)          # 非空哨兵：B 侧那一行可导出
        self.assertNotIn(self.NO_A, b_nos)


class ConfigWriteEndpointNodeGateTests(TenantTestCase):
    """游戏配置 / 订单来源写端点的逐动作节点门。本类覆盖 7 个点、每个点**两个方向**（无该节点 ⇒ 403；持该节点 ⇒ 通过）；⭐ `system.add_edit_source` / `system.delete_source` 各多了**一个落点**（公司店铺分组写端点，拍板④复用同一节点）⇒ 表里多两行、**节点数仍是 7**。逐点：`game.add_edit` = `create`/`update`/`partial_update`（`POST /games/`、`PUT/PATCH /games/<id>/`）；`game.delete` = `destroy`（`DELETE /games/<id>/`）；`system.add_edit_source` = `create`/`update`/`partial_update`（`POST /order-sources/`、`PUT/PATCH /order-sources/<id>/`；**公司店铺分组**那套同理：`POST /order-source-groups/`、`PUT/PATCH /order-source-groups/<id>/`）；`system.delete_source` = `destroy`（`DELETE /order-sources/<id>/` 与 `DELETE /order-source-groups/<id>/`）；`game.server` / `game.order_type_categories` / `game.common_projects` 各四个写动作（`POST|PUT|PATCH|DELETE` 于 `/servers/`、`/categories/`、`/projects/`）。
    后 3 个点此前**只有体验层位点**（菜单可见性），零权限角色照样能真删/改区服 / 分类 / 项目（独立评审实测探针穿过权限层）。⚠️ **拒绝侧刻意挂「同域的另一条」节点**（不是空集）：只断「没挂任何节点 ⇒ 403」无法区分「判据按 code 精确匹配」与「有节点就放行」——挂上同域另一条 code 后仍然 403，才是「这个动作要的就是这条 code」的证据。⚠️ **放行侧同时断 HTTP 200 与业务码 0**：仓里有过「响应是失败信封却被当成成功」的形状（E-0063），只断状态码不足以证明写真的发生；写路径另配一条数据库行断言。
    ⚠️ **`list` / `retrieve` 刻意不在本类**：它们的判据没有接（行为逐字不变），拿它们当「通过」的对照会把未接线的读动作误报成已接线。⚠️ **每个 client 用独立 Group**（同 `OrderEndpointNodeGateTests._cs` 的理由）：节点挂在 `Role` 上、`Role.name` 唯一，同名组会共享同一个 `Role`，后一个 client 的 `nodes` 会加在前一个上，拒绝侧的 403 就断不出来。"""

    GAMES = "/api/business/games/"
    SOURCES = "/api/business/order-sources/"
    SOURCE_GROUPS = "/api/business/order-source-groups/"
    SERVERS = "/api/business/servers/"
    CATEGORIES = "/api/business/categories/"
    PROJECTS = "/api/business/projects/"

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="原神")
        self.source = OrderSource.objects.create(name="闲鱼A")
        self.source_group = OrderSourceGroup.objects.create(name="甲组")
        # 三个配置写端点的靶行：夹具直接走 ORM（不过端点）⇒ 不产生留痕行、不受节点门影响。
        self.server = GameServer.objects.create(game=self.game, name="国服")
        self.category = ProjectCategory.objects.create(game=self.game, name="日常")
        self.project = Project.objects.create(category=self.category, name="委托")

    def _client(self, username, *nodes):
        type(self)._GROUP_SEQ += 1
        return make_logged_in_client(
            username=username, groups=(f"配{type(self)._GROUP_SEQ}",), nodes=nodes
        )

    # ── game.add_edit ─────────────────────────────────────────────
    def test_game_add_edit_without_the_node_is_403(self):
        """`create` / `update` / `partial_update` 三个动作都拒（挂的是另一条 `game.delete`）。"""
        denied = self._client("cfg-game-ae-no", "game.delete")
        created = denied.post(
            self.GAMES, {"name": "崩坏"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.GAMES}{self.game.id}/", {"name": "改名"}, content_type="application/json"
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.GAMES}{self.game.id}/", {"name": "改名2"}, content_type="application/json"
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        # 被拒的写请求一个字都没落库
        self.assertFalse(Game.objects.filter(name="崩坏").exists())
        self.game.refresh_from_db()
        self.assertEqual(self.game.name, "原神")

    def test_game_add_edit_with_the_node_passes(self):
        """持 `game.add_edit` ⇒ 三个动作都通过（POST / PATCH / PUT）。"""
        allowed = self._client("cfg-game-ae-ok", "game.add_edit")
        created = allowed.post(
            self.GAMES, {"name": "崩坏"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(Game.objects.filter(name="崩坏").exists())

        patched = allowed.patch(
            f"{self.GAMES}{self.game.id}/", {"name": "改名"}, content_type="application/json"
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.game.refresh_from_db()
        self.assertEqual(self.game.name, "改名")

        put = allowed.put(
            f"{self.GAMES}{self.game.id}/", {"name": "改名2"}, content_type="application/json"
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.game.refresh_from_db()
        self.assertEqual(self.game.name, "改名2")

    # ── game.delete ───────────────────────────────────────────────
    def test_game_delete_without_the_node_is_403(self):
        """持 `game.add_edit`（同域另一条）⇒ `DELETE` 仍 403，行未被软删。"""
        denied = self._client("cfg-game-del-no", "game.add_edit")
        resp = denied.delete(f"{self.GAMES}{self.game.id}/")
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        self.assertEqual(resp.json()["code"], 403, resp.json())
        self.game.refresh_from_db()
        self.assertIsNone(self.game.deleted_at)

    def test_game_delete_with_the_node_passes(self):
        """持 `game.delete` ⇒ 软删成功（200 + code 0 + `deleted_at` 有值）。"""
        allowed = self._client("cfg-game-del-ok", "game.delete")
        resp = allowed.delete(f"{self.GAMES}{self.game.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        self.game.refresh_from_db()
        self.assertIsNotNone(self.game.deleted_at)

    # ── system.add_edit_source ────────────────────────────────────
    def test_order_source_add_edit_without_the_node_is_403(self):
        """`create` / `update` / `partial_update` 三个动作都拒（挂的是 `system.delete_source`）。"""
        denied = self._client("cfg-src-ae-no", "system.delete_source")
        created = denied.post(
            self.SOURCES, {"name": "闲鱼B"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.SOURCES}{self.source.id}/",
            {"name": "闲鱼C"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.SOURCES}{self.source.id}/",
            {"name": "闲鱼D"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        self.assertFalse(OrderSource.objects.filter(name="闲鱼B").exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.name, "闲鱼A")

    def test_order_source_add_edit_with_the_node_passes(self):
        """持 `system.add_edit_source` ⇒ 三个动作都通过（POST / PATCH / PUT）。"""
        allowed = self._client("cfg-src-ae-ok", "system.add_edit_source")
        created = allowed.post(
            self.SOURCES, {"name": "闲鱼B"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(OrderSource.objects.filter(name="闲鱼B").exists())

        patched = allowed.patch(
            f"{self.SOURCES}{self.source.id}/",
            {"name": "闲鱼C"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.source.refresh_from_db()
        self.assertEqual(self.source.name, "闲鱼C")

        put = allowed.put(
            f"{self.SOURCES}{self.source.id}/",
            {"name": "闲鱼D"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.source.refresh_from_db()
        self.assertEqual(self.source.name, "闲鱼D")

    # ── system.delete_source ──────────────────────────────────────
    def test_order_source_delete_without_the_node_is_403(self):
        """持 `system.add_edit_source`（同域另一条）⇒ `DELETE` 仍 403，行还在。"""
        denied = self._client("cfg-src-del-no", "system.add_edit_source")
        resp = denied.delete(f"{self.SOURCES}{self.source.id}/")
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        self.assertEqual(resp.json()["code"], 403, resp.json())
        self.assertTrue(OrderSource.objects.filter(id=self.source.id).exists())

    def test_order_source_delete_with_the_node_passes(self):
        """持 `system.delete_source` ⇒ 真删成功（200 + code 0 + 行不在库里）。"""
        allowed = self._client("cfg-src-del-ok", "system.delete_source")
        resp = allowed.delete(f"{self.SOURCES}{self.source.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        self.assertFalse(OrderSource.objects.filter(id=self.source.id).exists())

    # ── game.server────────────────
    def test_game_server_write_without_the_node_is_403(self):
        """`create` / `update` / `partial_update` / `destroy` 四个动作都拒。
        拒绝侧挂的是**同域另一条** `game.common_projects`（不是空集）：若只断「没挂节点 ⇒ 403」，「有任意游戏域节点就放行」也会绿；
        挂上邻点后仍 403 才是按 code 精确匹配的证据。"""
        denied = self._client("cfg-srv-no", "game.common_projects")
        created = denied.post(
            self.SERVERS,
            {"game": self.game.id, "name": "霓虹服"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.SERVERS}{self.server.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.SERVERS}{self.server.id}/",
            {"game": self.game.id, "name": "改名2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        deleted = denied.delete(f"{self.SERVERS}{self.server.id}/")
        self.assertEqual(deleted.status_code, 403, deleted.content[:200])
        # 被拒的写请求一个字都没落库（区服没有软删除列 ⇒ 行必须原样在库里）。
        self.assertFalse(GameServer.objects.filter(name="霓虹服").exists())
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "国服")

    def test_game_server_write_with_the_node_passes(self):
        """持 `game.server` ⇒ 四个动作都通过（POST / PATCH / PUT / DELETE）。"""
        allowed = self._client("cfg-srv-ok", "game.server")
        created = allowed.post(
            self.SERVERS,
            {"game": self.game.id, "name": "霓虹服"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(GameServer.objects.filter(name="霓虹服").exists())

        patched = allowed.patch(
            f"{self.SERVERS}{self.server.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "改名")

        put = allowed.put(
            f"{self.SERVERS}{self.server.id}/",
            {"game": self.game.id, "name": "改名2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, "改名2")

        deleted = allowed.delete(f"{self.SERVERS}{self.server.id}/")
        self.assertEqual(deleted.status_code, 200, deleted.content[:200])
        self.assertEqual(deleted.json()["code"], 0, deleted.json())
        self.assertFalse(GameServer.objects.filter(id=self.server.id).exists())

    # ── game.order_type_categories──────────────
    def test_project_category_write_without_the_node_is_403(self):
        """四个写动作都拒（拒绝侧挂的是**同域另一条** `game.server`）。"""
        denied = self._client("cfg-cat-no", "game.server")
        created = denied.post(
            self.CATEGORIES,
            {"game": self.game.id, "name": "深渊"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.CATEGORIES}{self.category.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.CATEGORIES}{self.category.id}/",
            {"game": self.game.id, "name": "改名2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        deleted = denied.delete(f"{self.CATEGORIES}{self.category.id}/")
        self.assertEqual(deleted.status_code, 403, deleted.content[:200])
        self.assertFalse(ProjectCategory.objects.filter(name="深渊").exists())
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "日常")
        self.assertIsNone(self.category.deleted_at)

    def test_project_category_write_with_the_node_passes(self):
        """持 `game.order_type_categories` ⇒ 四个动作都通过（软删也算 destroy 成功）。"""
        allowed = self._client("cfg-cat-ok", "game.order_type_categories")
        created = allowed.post(
            self.CATEGORIES,
            {"game": self.game.id, "name": "深渊"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(ProjectCategory.objects.filter(name="深渊").exists())

        patched = allowed.patch(
            f"{self.CATEGORIES}{self.category.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "改名")

        put = allowed.put(
            f"{self.CATEGORIES}{self.category.id}/",
            {"game": self.game.id, "name": "改名2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.category.refresh_from_db()
        self.assertEqual(self.category.name, "改名2")

        deleted = allowed.delete(f"{self.CATEGORIES}{self.category.id}/")
        self.assertEqual(deleted.status_code, 200, deleted.content[:200])
        self.assertEqual(deleted.json()["code"], 0, deleted.json())
        self.category.refresh_from_db()
        self.assertIsNotNone(self.category.deleted_at)

    # ── game.common_projects───────────────────
    def test_project_write_without_the_node_is_403(self):
        """四个写动作都拒（拒绝侧挂的是**同域另一条** `game.order_type_categories`）。"""
        denied = self._client("cfg-proj-no", "game.order_type_categories")
        created = denied.post(
            self.PROJECTS,
            {"category": self.category.id, "name": "新项目", "base_price": "9.00"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.PROJECTS}{self.project.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.PROJECTS}{self.project.id}/",
            {"category": self.category.id, "name": "改名2", "base_price": "9.00"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        deleted = denied.delete(f"{self.PROJECTS}{self.project.id}/")
        self.assertEqual(deleted.status_code, 403, deleted.content[:200])
        self.assertFalse(Project.objects.filter(name="新项目").exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "委托")
        self.assertIsNone(self.project.deleted_at)

    def test_project_write_with_the_node_passes(self):
        """持 `game.common_projects` ⇒ 四个动作都通过（软删也算 destroy 成功）。"""
        allowed = self._client("cfg-proj-ok", "game.common_projects")
        created = allowed.post(
            self.PROJECTS,
            {"category": self.category.id, "name": "新项目", "base_price": "9.00"},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(Project.objects.filter(name="新项目").exists())

        patched = allowed.patch(
            f"{self.PROJECTS}{self.project.id}/",
            {"name": "改名"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "改名")

        put = allowed.put(
            f"{self.PROJECTS}{self.project.id}/",
            {"category": self.category.id, "name": "改名2", "base_price": "9.00"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, "改名2")

        deleted = allowed.delete(f"{self.PROJECTS}{self.project.id}/")
        self.assertEqual(deleted.status_code, 200, deleted.content[:200])
        self.assertEqual(deleted.json()["code"], 0, deleted.json())
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.deleted_at)

    # ── 公司店铺分组（拍板④：复用来源那两条节点）────────────
    def test_order_source_group_add_edit_without_the_node_is_403(self):
        """`create` / `update` / `partial_update` 三个动作都拒（挂的是 `system.delete_source`）。
        拒绝侧挂**同域另一条**节点（不是空集）：只断「没挂节点 ⇒ 403」区分不出
        「按 code 精确匹配」与「有任意来源域节点就放行」。"""
        denied = self._client("cfg-grp-ae-no", "system.delete_source")
        created = denied.post(
            self.SOURCE_GROUPS, {"name": "闲鱼组"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 403, created.content[:200])
        self.assertEqual(created.json()["code"], 403, created.json())
        patched = denied.patch(
            f"{self.SOURCE_GROUPS}{self.source_group.id}/",
            {"name": "改名组"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 403, patched.content[:200])
        put = denied.put(
            f"{self.SOURCE_GROUPS}{self.source_group.id}/",
            {"name": "改名组2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 403, put.content[:200])
        # 被拒的写一个字都没落库。
        self.assertFalse(OrderSourceGroup.objects.filter(name="闲鱼组").exists())
        self.source_group.refresh_from_db()
        self.assertEqual(self.source_group.name, "甲组")

    def test_order_source_group_add_edit_with_the_node_passes(self):
        """持 `system.add_edit_source` ⇒ 三个动作都通过（POST / PATCH / PUT）。"""
        allowed = self._client("cfg-grp-ae-ok", "system.add_edit_source")
        created = allowed.post(
            self.SOURCE_GROUPS, {"name": "闲鱼组"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 200, created.content[:200])
        self.assertEqual(created.json()["code"], 0, created.json())
        self.assertTrue(OrderSourceGroup.objects.filter(name="闲鱼组").exists())

        patched = allowed.patch(
            f"{self.SOURCE_GROUPS}{self.source_group.id}/",
            {"name": "改名组"},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200, patched.content[:200])
        self.assertEqual(patched.json()["code"], 0, patched.json())
        self.source_group.refresh_from_db()
        self.assertEqual(self.source_group.name, "改名组")

        put = allowed.put(
            f"{self.SOURCE_GROUPS}{self.source_group.id}/",
            {"name": "改名组2"},
            content_type="application/json",
        )
        self.assertEqual(put.status_code, 200, put.content[:200])
        self.assertEqual(put.json()["code"], 0, put.json())
        self.source_group.refresh_from_db()
        self.assertEqual(self.source_group.name, "改名组2")

    def test_order_source_group_delete_without_the_node_is_403(self):
        """持 `system.add_edit_source`（同域另一条）⇒ `DELETE` 仍 403，行还在。"""
        denied = self._client("cfg-grp-del-no", "system.add_edit_source")
        resp = denied.delete(f"{self.SOURCE_GROUPS}{self.source_group.id}/")
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        self.assertEqual(resp.json()["code"], 403, resp.json())
        self.assertTrue(OrderSourceGroup.objects.filter(id=self.source_group.id).exists())

    def test_order_source_group_delete_with_the_node_passes(self):
        """持 `system.delete_source` ⇒ 真删成功（200 + code 0 + 行不在库里）。"""
        allowed = self._client("cfg-grp-del-ok", "system.delete_source")
        resp = allowed.delete(f"{self.SOURCE_GROUPS}{self.source_group.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        self.assertFalse(OrderSourceGroup.objects.filter(id=self.source_group.id).exists())


class SalesAgentChangeGateTests(TenantTestCase):
    """换接单销售客服（`sales_agent`）的节点门：`order.allow_change_sales_agent`(1272)。⭐ 接单销售客服可改线（计划 `docs/plans/2026-10-08-sales-agent-change.md` §5 T2）；业务方**简化裁定**：不复刻对标那条全局设置项，规则就一句「持本点的人才能改这一格」；判据落在 `OrderViewSet.partial_update` 的**动作体**里（形状照 `_resolve_assign_taker` 那两道「带了价格才要另一个节点」的闸）。
    五条用例各自钉什么（每条都能**分辨**对错，没有恒真的断言）：① **无节点 + 改一个不同的值** ⇒ HTTP 403（`PermissionDenied` 的既有翻译路径，**不是**业务码）+ 库里一个字没变；② **有节点 + 同一个不同的值** ⇒ **业务码 1000 + 一句指向专用端点的提示**、库里一个字没变（通用 PATCH 一律改不了这一列：它是归属人的派生快照）；③ **无节点 + 相同的值 + 另一个字段**（`remark`）⇒ **整体成功**、另一个字段确实改了——专门分辨「只要带了 `sales_agent` 就拒」的错实现（前端编辑态是整表单提交，没改的那格也会带原值 ⇒ 按「带键就拒」会把「没改」误报成越权）；④ **无节点 + 录单（POST）带 `sales_agent`** ⇒ 成功（值取**录单人自己的**显示名；⛔ 「带一个别的名字」那一档是**拒绝**，见该用例的 docstring）；⑤ 「值变了才判」的反向对照：把闸改成**无条件拒**之后 **③ 必须红**（做法与读数在交付报告里，本文件里不留变异）。
    ⚠️ 为什么夹具的「职位组」与「节点组」分开挂：`make_logged_in_client(nodes=…)` 把节点挂到该用户所在的**每一个** `Group` 上，而 `Role.name` 唯一、`Role.permissions.add` 是**并集** ⇒ 两个 client 若共用同一个职位组，后一个的节点会加在前一个上，「无节点 ⇒ 403」那条就永远断不出来；逐用例给组名编号（组名 ≤ 5 字，`Role.name` 的 `max_length=5`）。"""

    #: 本类用到的订单号复用区间（`OrderViewSet` 的 `lookup_value_regex = r"\\d{20}"`）。
    ORDER_URL = "/api/business/orders"

    _GROUP_SEQ = 0

    def setUp(self):
        seed_catalog()
        self.game = Game.objects.create(name="原神")

    # ── 夹具 ─────────────────────────────────────────────────────
    def _client(self, username, *codes):
        """真实登录的客服：职位组名逐用例唯一，节点挂在自己的那一组上（见类 docstring）。"""
        type(self)._GROUP_SEQ += 1
        role_group = f"销客{type(self)._GROUP_SEQ}"
        client = make_logged_in_client(
            username=username, groups=("客服", role_group), nodes=codes
        )
        user = User.objects.get(username=username)
        # （按游戏权限收窄）：夹具补齐，不是放宽判据——本类的单都归 `self.game`。
        grant_games(user, self.game)
        return client, user

    def _order(self, no, sales_agent=""):
        """一张可编辑的单。
        ⚠️ `project_text` 必须有值：`partial_update` 会把「本次没传的项目文本」合并进来再走 `validate_required`，空串会让 PATCH 以业务码 1000 被拒（与本用例无关的噪音）。"""
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("88.00"),
            project_text="满星",
            sales_agent=sales_agent,
        )

    def _patch(self, client, order, payload):
        return client.patch(
            f"{self.ORDER_URL}/{order.order_no}/",
            payload,
            content_type="application/json",
        )

    # ── ① 无节点 + 改一个不同的值 ⇒ 403，且库里没变 ────────────────
    def test_changing_the_sales_agent_without_the_node_is_denied(self):
        """判别力：把闸去掉，本条立刻变成 200 + 库里的值真变了。"""
        order = self._order("20260921000000000120", sales_agent="客服甲")
        client, _user = self._client("sa-deny", "order.add_edit")
        response = self._patch(client, order, {"sales_agent": "客服乙"})
        # ⚠️ 断 HTTP 状态码而**不是**业务码：`PermissionDenied` 走的是
        # `apps/core/exceptions.py` 的传输层分支（403 + code 403），与业务失败（200 + 1000）不同。
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertIn("你没有改销售客服的权限", response.json()["msg"])
        self.assertEqual(Order.objects.get(pk=order.pk).sales_agent, "客服甲")   # 库里没变

    # ── ② 有节点也**不许**走 PATCH ⇒ 业务码 1000、库里一个字不变 ──────
    def test_changing_the_sales_agent_with_the_node_is_rejected_too(self):
        """⭐ **通用 PATCH 改不了 `sales_agent`，有节点也不行**：`Order.sales_agent` 是 `Order.sales_user`（提成归属）的**派生快照** ⇒ 换人只有 `POST /orders/<单号>/sales-agent/` 一条路（同一个事务里改归属 + 搬绩效行 + 同步名字 + 重算）。
        理由：单独写这一列能造出「**列表显示乙、提成归甲**」，而那种现场没有任何用例钉住。
        ⛔ 这不是放宽判据：这格在 PATCH 上是「拒绝」；仓库侧没有合法请求依赖放行它（录单页编辑态把这一列从 PATCH 载荷里删掉了）。"""
        order = self._order("20260921000000000121", sales_agent="客服甲")
        client, _user = self._client(
            "sa-allow", "order.add_edit", "order.allow_change_sales_agent"
        )
        response = self._patch(client, order, {"sales_agent": "客服乙"})
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("换销售客服", body["msg"])
        # 断言打在**数据库行**上（E-0009）：只断响应体的话，"返回了拒绝但库里改了"也会绿。
        self.assertEqual(Order.objects.get(pk=order.pk).sales_agent, "客服甲")

    # ── ③ 判别用例：同值 + 另一个字段 ⇒ 整体成功 ──────────────────
    def test_patching_the_same_sales_agent_alongside_another_field_succeeds(self):
        """**这条是「带了键就拒」那种错实现的探针。**
        无节点的人整表单提交（`sales_agent` 原值 + 真改了的 `remark`）必须**整体成功**：
        值没变 ⇒ 不算「更换销售客服」⇒ 不该要那个节点。"""
        order = self._order("20260921000000000122", sales_agent="客服甲")
        client, _user = self._client("sa-same", "order.add_edit")
        response = self._patch(
            client, order, {"sales_agent": "客服甲", "remark": "只改备注"}
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.json())
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.remark, "只改备注")      # 另一个字段**确实改了**
        self.assertEqual(row.sales_agent, "客服甲")   # 这一格没被动

    # ── ④ 记录用例：录单（POST）不受本节点限制 ─────────────────────
    def test_creating_an_order_with_a_sales_agent_needs_no_node(self):
        """⚠️ **实现者的判断**（按对标那条权限点的原文「在**编辑订单页面**更换销售客服」）：新建单不是「更换」，不受本节点限制。⛔ 这条不是业务方原话，可被推翻。
        ⚠️ **录单也不收客户端自填的名字**（它是归属人的派生快照）——要么不带这个键（服务端按归属人写），要么带一个**与服务端将要写入的那个名字逐字相同**的值。⇒ 本条送「带**录单人自己的**显示名」这一档：它证明的仍然是本条的命题（**录单不受那个权限节点限制**），而「带一个别的名字」那一档由
        `tests/test_order_sales_agent.py::test_with_a_foreign_sales_agent_name_nothing_is_created` 与 `tests/test_order_api.py::test_create_rejects_a_client_supplied_sales_agent` 钉着。"""
        client, user = self._client("sa-create", "order.add_edit")
        # 录单人的显示名（缺 `first_name` ⇒ 服务端口径回落 `username`，见 `user_display_name`）。
        own_name = user.first_name or user.username
        response = client.post(
            f"{self.ORDER_URL}/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "total_price": "199.50",
                "sales_agent": own_name,
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        row = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(row.sales_agent, own_name)
        self.assertEqual(row.sales_user_id, user.pk)      # 归属 = 认证态（本条的另一半命题）
