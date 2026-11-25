"""管理订单页「编辑订单」所依赖的**既有**更新端点（`PATCH /api/business/orders/{order_no}/`）。

    ## 为什么是「既有」

    `order-orderlist.ts` 的编辑弹窗（`layer.open({type:2, area:['95%','95%']})` iframe 内嵌录单页）
    在业务方要求「真提交保存」之前**从不发请求**——原因是 `api/orders.ts` 自述「不提供
    `updateOrder`」。但后端从 P2 起就有这条路径：`OrderViewSet.partial_update`
    （`order_views.py`）+ `OrderUpdateSerializer`（`api/serializers.py`），由 `router.register("orders", …)`
    自动生成 detail 路由，且 `http_method_names` 里已含 `"patch"`。

    所以本轮**复用**它，本文件把它的四类判据钉住（这些判据此前**没有**端点级覆盖）：
    更新成功 / 字段校验失败 / 跨租户隔离 / 权限与状态边界。

    ## 已知边界（不在本文件断言的范围内，写出来免得被误读成已保证）

    - **普通改价不经此路径**：只有拆单组非锚点可以在 PATCH 同时改内容和接单价；其余普通订单仍走 `/price/`。
    - **没有状态闸门**：PATCH 不判订单当前状态（spec §6 只写「编辑 = 改需求 / 改联系方式 / 改项目」，
    没有状态维度）。但 `status` 本身是只读 ⇒ 任何状态都**不能**经由编辑被改写，
    状态机因此不被绕过。哪几个状态允许改非状态字段，**业务口径未定**——不猜。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog, Game, JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent,
    Order, OrderStatus, Project, ProjectCategory,
)
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import P2_TEST_USERNAME, grant_games, make_logged_in_client

URL = "/api/business/orders"

#: 隔离用租户 B 的域名。后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "ordfix-b.test.local"
SCHEMA_B = "ordfix_b"

#: 两个租户**取值不同**的订单号（租户内唯一；不用主键指认另一租户的行——两个 schema 的
#: 主键序列各自从 1 开始，`filter(id=...)` 会命中本 schema 自己的行）。
A_ORDER_NO = "20260921000000000101"
B_ORDER_NO = "20260921000000000202"


class OrderUpdateApiTests(TenantTestCase):
    """更新成功 / 字段校验失败 / 权限与状态边界。"""

    def setUp(self):
        # 端点没有声明 `permission_classes` ⇒ fail-closed 默认 `IsAuthenticated`；
        # 用**客服**登录（与 `test_order_api.py` 同口径，本文件不测角色维度）。
        #
        # ⚠️ RBAC ：`partial_update` 从此要 `order.add_edit` / `order.submit_edit`
        # 节点（`OrderViewSet.ORDER_PERMISSIONS`）。`groups` 只建 Group、不放行 ⇒
        # 这里显式声明本文件用到的门。漏声明会以 403 的形式红（fail-closed）。
        self.client = make_logged_in_client(
            groups=("客服",),
            nodes=(
                "order.add_edit",     # PATCH / PUT（编辑）
                "order.submit_edit",
                "order.list",         # 集合列表（OrderListSourceOrderNoTests 用同一个 setUp 形状）
                "order.view_detail",  # 详情
            ),
        )
        self.game = Game.objects.create(name="原神")
        self.other_game = Game.objects.create(name="星穹铁道")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——本类所有 PATCH 都打在 `game=self.game` 的单上。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)
        self.cat = ProjectCategory.objects.create(game=self.game, name="深境螺旋")
        self.project = Project.objects.create(category=self.cat, name="满星")
        self.foreign_server = self.other_game.servers.create(name="国服")

    def test_source_order_no_edit_queues_sync_to_linked_jidanpu(self):
        order = self._make(source_order_no="SYNTH-SOURCE-BEFORE")
        owner = User.objects.create_user(username="jdp-source-owner")
        binding = JidanpuMemberBinding.objects.create(
            user=owner, jidanpu_base_url="https://jdp.test.invalid",
            account="jdp-source-owner", password="synthetic", enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="SYNTH-JDP-ORDER",
        )

        response = self.client.patch(
            f"{URL}/{order.order_no}/", {"source_order_no": "SYNTH-SOURCE-AFTER"},
            content_type="application/json",
        )

        self.assertEqual(response.json()["code"], ErrorCode.OK, response.content)
        order.refresh_from_db()
        self.assertEqual(order.source_order_no, "SYNTH-SOURCE-AFTER")
        event = JidanpuSyncEvent.objects.get(
            target_ref=order.order_no, kind="member.source_order_no",
        )
        self.assertEqual(event.payload["member_binding_id"], binding.pk)
        self.assertEqual(event.status, "pending")

    def _make(self, **overrides):
        fields = {
            "game": self.game,
            "order_no": "20260921000000000101",
            "project_text": "满星",
            "customer_phone": "13800138000",
            "total_price": Decimal("199.50"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    # ── 更新成功 ──────────────────────────────────────────────
    def test_patch_can_change_category_within_game_and_keep_project(self):
        order = self._make(category=self.cat, project=self.project)
        category = ProjectCategory.objects.create(game=self.game, name="合成另一分类")

        response = self.client.patch(
            f"{URL}/{order.order_no}/", {"category": category.pk},
            content_type="application/json",
        )

        self.assertEqual(response.json()["code"], ErrorCode.OK, response.content)
        order.refresh_from_db()
        self.assertEqual(order.category_id, category.pk)
        self.assertEqual(order.project_id, self.project.pk)

    def test_patch_remark_on_order_with_project_from_another_category(self):
        category = ProjectCategory.objects.create(game=self.game, name="合成另一分类")
        order = self._make(category=category, project=self.project)

        response = self.client.patch(
            f"{URL}/{order.order_no}/", {"category": category.pk, "remark": "合成备注"},
            content_type="application/json",
        )

        self.assertEqual(response.json()["code"], ErrorCode.OK, response.content)
        order.refresh_from_db()
        self.assertEqual(order.remark, "合成备注")
        self.assertEqual(order.category_id, category.pk)
        self.assertEqual(order.project_id, self.project.pk)

    def test_patch_persists_the_editable_fields(self):
        """编辑能**真提交**：非金额字段落库，`updated_by` 记操作人。

    断言打在**数据库行**上（E-0009）：只断响应体的话，「返回了新值但没落库」也会绿。
"""
        order = self._make()
        response = self.client.patch(
            f"{URL}/{order.order_no}/",
            {
                "project_text": "深渊 12 层",
                "customer_phone": "13900139000",
                "customer_qq": "10001",
                "remark": "客户要求静音",
                "tag": "加急",
                "is_urgent": True,
                "source_order_no": "TB-999",
                "actor": "客服小张",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

        order.refresh_from_db()
        self.assertEqual(order.project_text, "深渊 12 层")
        self.assertEqual(order.customer_phone, "13900139000")
        self.assertEqual(order.customer_qq, "10001")
        self.assertEqual(order.remark, "客户要求静音")
        self.assertEqual(order.tag, "加急")
        self.assertIs(order.is_urgent, True)
        self.assertEqual(order.source_order_no, "TB-999")
        # 编辑只写 `updated_by`，不碰创建人
        # ⚠️ **R20 的刻意分叉**：业务字段 `updated_by` 仍取**请求体**的 "客服小张"
        # （P2 契约，本次不动）；而审计行的 `actor` 取**认证态**用户名。
        self.assertEqual(order.updated_by, "客服小张")
        self.assertEqual(order.created_by, "")
        # 编辑是**自己的动作** `order.update`（一个被改字段一行）：它既不是一次迁移、
        # 也不是一次改价 —— 新表里只有 `order.update`，旧表一行都没有。
        # 7 个被改字段：project_text / customer_phone / customer_qq / remark / tag /
        # is_urgent / source_order_no（`actor` 不是模型字段，不进留痕）。
        self.assertEqual(
            set(
                OperationLog.objects.filter(order_no=order.order_no).values_list(
                    "action", flat=True
                )
            ),
            {"order.update"},
        )
        self.assertEqual(
            OperationLog.objects.filter(order_no=order.order_no).count(), 7
        )
        # 审计行的操作人一律是**认证态**；伪造的请求体名字不得出现
        self.assertEqual(
            set(
                OperationLog.objects.filter(order_no=order.order_no).values_list(
                    "actor", flat=True
                )
            ),
            {P2_TEST_USERNAME},
        )
        self.assertFalse(AuditLog.objects.filter(order_no=order.order_no).exists())
        # 响应回的是**改后**的完整订单（前端据此刷新，不该再发一次详情）
        self.assertEqual(response.json()["data"]["source_order_no"], "TB-999")

    def test_patch_cannot_rewrite_order_no_or_status_nor_money(self):
        """订单号 / 状态 / 其他金额字段只读，普通订单总价不能因 PATCH 变化——这是**加法之外**的硬边界。

    业务方规则「无论如何都有系统内置单号」：编辑不得把 `order_no` 改掉。
"""
        order = self._make()
        before_no = order.order_no
        response = self.client.patch(
            f"{URL}/{order.order_no}/",
            {
                "order_no": "20260101000000000001",
                "status": OrderStatus.COMPLETED,
                "total_price": "199.50",
                "received_price": "2.00",
                "customer_phone": "13900139000",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        order.refresh_from_db()
        # 阳性对照：这条 PATCH **确实生效了**（否则「没被改」可能只是请求没生效）
        self.assertEqual(order.customer_phone, "13900139000")
        self.assertEqual(order.order_no, before_no)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.total_price, Decimal("199.50"))
        self.assertIsNone(order.received_price)

    def test_editing_a_completed_order_does_not_move_its_status(self):
        """已完成这类终态的单：非状态字段可改，但状态**纹丝不动**。

    ⚠️ 原文写「已完成/已退单这类终态」——2026-09-21 业务方裁定给 `REFUNDED`
    加了两条回池出边后它**不再**是终态，故把措辞收窄到本用例真正覆盖的
    `COMPLETED`（用例本身一行没改）。
    状态机不被编辑绕过——`status` 只读是结构保证，本用例钉的是出口。
"""
        order = self._make(status=OrderStatus.COMPLETED)
        response = self.client.patch(
            f"{URL}/{order.order_no}/",
            {"remark": "完成后的补充备注", "status": OrderStatus.IN_PROGRESS},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.remark, "完成后的补充备注")
        self.assertEqual(order.status, OrderStatus.COMPLETED)

    # ── 字段校验失败 ──────────────────────────────────────────
    def test_invalid_field_values_are_business_errors_and_write_nothing(self):
        """非法值必须过序列化器（E-0010）：HTTP 200 + `code=1000`，一行都不落。

    ⚠️ 三者是**三个不同的失败口**（布尔解析 / 整数解析 / 外键解析），
    不是同一个字段的三种写法——「点名一处、留下同类」正是 E-0010 的形状。
"""
        order = self._make()
        cases = [
            ("is_urgent_not_a_bool", {"is_urgent": "也许"}),
            ("game_not_an_int", {"game": "abc"}),
            ("game_does_not_exist", {"game": 99999999}),
            ("actor_too_long", {"actor": "长" * 200}),
        ]
        for name, payload in cases:
            with self.subTest(case=name):
                response = self.client.patch(
                    f"{URL}/{order.order_no}/", payload, content_type="application/json"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], ErrorCode.VALIDATION)
                order.refresh_from_db()
                self.assertEqual(order.customer_phone, "13800138000")
                self.assertIs(order.is_urgent, False)
                self.assertEqual(order.updated_by, "")

    def test_server_from_another_game_is_rejected(self):
        """同链校验在**编辑**路径上同样生效（不是只在 create 里）。

    判据是业务口 `validate_order_chain`，出口同样是 `code=1000`。
"""
        order = self._make()
        response = self.client.patch(
            f"{URL}/{order.order_no}/",
            {"server": self.foreign_server.id, "customer_phone": "13900139000"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], ErrorCode.VALIDATION)
        order.refresh_from_db()
        self.assertIsNone(order.server)
        # 阳性对照：这条 PATCH 的**其余字段也没落库**（校验失败是整体的，不是逐字段部分成功）
        self.assertEqual(order.customer_phone, "13800138000")

    def test_blank_project_text_is_rejected(self):
        """`project_text` 是必填（D9）：PATCH 成空白必须被拒，不能把单改成无项目。"""
        order = self._make()
        response = self.client.patch(
            f"{URL}/{order.order_no}/",
            {"project_text": "   "},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], ErrorCode.VALIDATION)
        self.assertEqual(Order.objects.get(order_no=order.order_no).project_text, "满星")

    # ── 权限边界 ──────────────────────────────────────────────
    def test_anonymous_cannot_patch(self):
        """未认证 ⇒ 401（fail-closed 默认权限类），且一行都没改。"""
        order = self._make()
        anon = Client(SERVER_NAME="tenant.test.com")
        response = anon.patch(
            f"{URL}/{order.order_no}/",
            {"remark": "匿名改的"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Order.objects.get(order_no=order.order_no).remark, "")

    def test_a_plain_user_holding_the_node_can_edit(self):
        """持 `order.add_edit` 的**普通**（非 superuser、非管理员）账号就能编辑。

    📌 **改过名的用例**（Fix round 2 / F3e）：原名 `test_permission_class_is_the_list_endpoint_same_one`，
    但本方法**从来没有**与列表端点比较过任何东西——名字是 改口径时留下的旧壳。
    现在按它**实际断言的东西**命名：这道门是正常职位拿得到的能力，
    而不是只有管理员能过的门。**断言一字未改。**

    ⚠️ 它原来钉的是「任何登录用户都能改（**没有**角色闸门）」——那条口径随
    `ORDER_PERMISSIONS` 的 `partial_update → order.add_edit / order.submit_edit` 已作废；
    原 docstring 自己预告了"那时按新口径改"，本次照办（改的是名字与解释，
    不是把闸门去掉）。
"""
        order = self._make()
        plain = make_logged_in_client(
            username="ordfix-plain",
            groups=("客服",),
            nodes=("order.add_edit",),   # ：编辑的门就是这条
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——这张单是 `self.game` 的，账号得持有该游戏。
        grant_games(User.objects.get(username="ordfix-plain"), self.game)
        response = plain.patch(
            f"{URL}/{order.order_no}/",
            {"remark": "持节点就能改"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(Order.objects.get(order_no=order.order_no).remark, "持节点就能改")


class OrderListSourceOrderNoTests(TenantTestCase):
    """规则 A：**列表**必须回 `source_order_no`（详情与模型一直都有，列表此前没有）。

    前端「订单号」列的规则是「来源单号非空 ⇒ 显示来源单号，否则显示系统订单号」，
    「系统内置单号」列恒显示 `order_no`。列表拿不到 `source_order_no` 时前端只能恒显示订单号
    ——这条测试就是为了避免那种「静默退化」。
"""

    def setUp(self):
        # ：本用例同时走**列表**与**详情**，两条门都要显式声明。
        self.client = make_logged_in_client(
            groups=("客服",),
            nodes=("order.list", "order.view_detail"),
        )
        self.game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——列表与详情两侧都要看得见 `self.game` 的单。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)

    def test_list_returns_source_order_no_and_order_no_together(self):
        with_source = Order.objects.create(
            game=self.game,
            order_no="20260921000000000301",
            project_text="外部单",
            source_order_no="TB-2026-301",
        )
        without_source = Order.objects.create(
            game=self.game,
            order_no="20260921000000000302",
            project_text="内部单",
        )
        body = self.client.get(URL + "/").json()
        self.assertEqual(body["code"], 0)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        # 两张单都在（否则下面的断言可能由「列表为空」凑出来）
        self.assertIn(with_source.order_no, rows)
        self.assertIn(without_source.order_no, rows)
        # 来源单号非空的那张：两个字段都在，且**取值不同** ⇒ 前端能据此切换显示
        self.assertEqual(rows[with_source.order_no]["source_order_no"], "TB-2026-301")
        self.assertEqual(rows[with_source.order_no]["order_no"], with_source.order_no)
        # 来源单号为空的那张：字段仍在（回空串），`order_no` 恒有值
        self.assertEqual(rows[without_source.order_no]["source_order_no"], "")
        self.assertEqual(rows[without_source.order_no]["order_no"], without_source.order_no)

    def test_detail_still_carries_source_order_no(self):
        """加法不得顺手改掉详情（详情本来就有它）——一条最小回归。"""
        order = Order.objects.create(
            game=self.game,
            order_no="20260921000000000303",
            project_text="外部单",
            source_order_no="TB-2026-303",
        )
        body = self.client.get(f"{URL}/{order.order_no}/").json()
        self.assertEqual(body["data"]["source_order_no"], "TB-2026-303")


class OrderUpdateIsolationTests(TenantTestCase):
    """跨租户隔离：以租户 A 的凭据编辑**只在 B 里存在**的那张单必须失败。

    ⚠️ 判据用 `order_no`（租户内唯一、且**两租户取值不同**），**不用主键**——
    两个 schema 的订单主键序列各自从 1 开始，`filter(id=...)` 在 B 里命中的是 B 自己的行。

    ⚠️ 负向断言打在**数据库行**上，并配一条**正向对照**（A 用自己那张单做同一个动作必须生效）：
    没有对照的话，「B 那张没被改」可能由「这个动作对谁都不生效」凑出来。
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单编辑隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求**不会**在响应结束时还原 schema，
        # 见 `tests/test_account_isolation.py` 的模块说明）。
        connection.set_tenant(self.tenant)
        # ：编辑要 `order.add_edit`，两侧各挂一次（A / B 是**两个 schema**，
        # 同名 Group「客服」在两边是不同的 Role 行，节点各自独立）。
        self.client_a = make_logged_in_client(
            username="ordfix-a", groups=("客服",), nodes=("order.add_edit",)
        )
        game_a = Game.objects.create(name="仅 A 的游戏")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——A 侧「改自己那张单」的正向对照要真的生效。
        grant_games(User.objects.get(username="ordfix-a"), game_a)
        self.order_a = Order.objects.create(
            game=game_a,
            order_no=A_ORDER_NO,
            project_text="仅 A 可见",
            customer_phone="13800138000",
        )
        try:
            with tenant_context(self.tenant_b):
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B, username="ordfix-b", groups=("客服",),
                    nodes=("order.add_edit",),
                )
                game_b = Game.objects.create(name="仅 B 的游戏")
                # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
                # 该用例的期望值一个字没改——`test_each_tenant_can_edit_its_own_order`
                # 要的正是「B 用自己那张单能成功」。
                grant_games(User.objects.get(username="ordfix-b"), game_b)
                self.order_b = Order.objects.create(
                    game=game_b,
                    order_no=B_ORDER_NO,
                    project_text="仅 B 可见",
                    customer_phone="13900139000",
                )
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证（**不是**用例，是承重点）：两侧各有一张**不同号**的单，且互不可见。
        self.assertTrue(Order.objects.filter(order_no=A_ORDER_NO).exists())
        assert self._b(lambda: Order.objects.filter(order_no=B_ORDER_NO).exists()), \
            "B 的标记单没落到 B 的 schema 上"
        assert not self._b(lambda: Order.objects.filter(order_no=A_ORDER_NO).exists()), \
            "A 的单在 B 的 schema 里也存在——隔离前提坏了"

    def _b(self, function):
        """在 B 的 schema 上下文里执行，结束后把连接钉回租户 A。"""
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_a_client_cannot_edit_the_order_that_lives_only_in_b(self):
        response = self.client_a.patch(
            f"{URL}/{B_ORDER_NO}/",
            {"project_text": "A 越界改的", "customer_phone": "13700000000"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        # 资源不存在（不是 403）——消息由异常处理器给，别在这里改写它
        self.assertEqual(response.json()["code"], ErrorCode.BUSINESS)

        # B 的那一行**逐字段未变**（在 B 的 schema 里读，不是靠 A 的视图）
        b_row = self._b(lambda: Order.objects.get(order_no=B_ORDER_NO))
        self.assertEqual(b_row.project_text, "仅 B 可见")
        self.assertEqual(b_row.customer_phone, "13900139000")

        # 正向对照：**同一个 client** 用自己租户的单号做**同一个** PATCH，必须真的生效
        control = self.client_a.patch(
            f"{URL}/{A_ORDER_NO}/",
            {"project_text": "A 自己改的", "customer_phone": "13700000000"},
            content_type="application/json",
        )
        self.assertEqual(control.json()["code"], 0)
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.project_text, "A 自己改的")
        self.assertEqual(self.order_a.customer_phone, "13700000000")

    def test_each_tenant_can_edit_its_own_order(self):
        """B 用**它自己的**客户端与单号必须成功——否则上一条的失败可能只是「B 用不了」。"""
        response = self._b(
            lambda: self.client_b.patch(
                f"{URL}/{B_ORDER_NO}/",
                {"project_text": "B 自己改的"},
                content_type="application/json",
            )
        )
        self.assertEqual(response.json()["code"], 0)
        b_row = self._b(lambda: Order.objects.get(order_no=B_ORDER_NO))
        self.assertEqual(b_row.project_text, "B 自己改的")
        # A 的行不受影响
        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.project_text, "仅 A 可见")

    def test_users_are_not_shared_between_the_two_tenants(self):
        """夹具前提自证：两个租户各有自己的用户表，A 看不到 B 的账号。"""
        self.assertTrue(User.objects.filter(username="ordfix-a").exists())
        self.assertFalse(User.objects.filter(username="ordfix-b").exists())
        self.assertTrue(self._b(lambda: User.objects.filter(username="ordfix-b").exists()))
        self.assertFalse(self._b(lambda: User.objects.filter(username="ordfix-a").exists()))
