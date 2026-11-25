"""单轴化基座：`nodes_of` 必须与 `has_node_now` 同源（spec §3 的钉子）。

判据形态（刻意**不**自指）：对四类主体 × 若干 code，逐条断言
`(code in nodes_of(u)) == has_node_now(u, code)`。
计数与清单类期望值写**字面量**（合并树的全目录 = 163、裸超管 = 空集），不 import 生产常量
来当期望值——`assertEqual(f(x), SOME_CONST)` 里被断言的与决定结果的是同一个符号，
改坏 `SOME_CONST` 也不会红（E-0024）。
"""
from contextvars import copy_context

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.core.permissions import request_permission_reads_for, reuse_request_permission_reads
from apps.rbac.models import PermissionNode, Role
from apps.rbac.permissions import has_node_now, nodes_of
from apps.rbac.seed import seed_builtin_roles, seed_catalog

User = get_user_model()

#: 抽查的 code（含一个 `available=False` 的、一个 flag、一个别的栏目）
_PROBE_CODES = (
    "order.change_status",
    "order.change_status_completed",
    "grab_hall.take_order",
    "order.submit_work",
    "order.restrict_to_self_created",
    "finance.payroll",
)


class NodeCodesOfTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        seed_builtin_roles()

    def _user_in(self, group_name):
        user = User.objects.create_user(username=f"u-{group_name}", password="x")
        user.groups.add(Group.objects.get(name=group_name))
        return user

    def test_anonymous_gets_nothing(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(nodes_of(AnonymousUser()), frozenset())
        self.assertFalse(has_node_now(AnonymousUser(), "order.change_status"))

    def test_none_gets_nothing(self):
        self.assertEqual(nodes_of(None), frozenset())

    def test_copied_context_cannot_select_permission_reads_after_the_scope_exits(self):
        user = self._user_in("客服")
        with reuse_request_permission_reads(user):
            inherited = copy_context()
            self.assertIsNotNone(inherited.run(request_permission_reads_for, user))

        self.assertIsNone(inherited.run(request_permission_reads_for, user))
        self.assertIsNone(inherited.run(request_permission_reads_for, None))

    def test_service_group_holds_the_order_module_nodes(self):
        user = self._user_in("客服")
        held = nodes_of(user)
        self.assertIn("order.change_status", held)
        self.assertIn("order.change_status_completed", held)
        self.assertNotIn("finance.payroll", held)

    def test_custom_role_gets_exactly_its_nodes(self):
        """自定义职位（任意 Group）不再 fail-closed：勾了就有。"""
        role = Role.objects.create(name="自定义职位", group=Group.objects.create(name="自定义职位"))
        role.permissions.set(PermissionNode.objects.filter(code="grab_hall.take_order"))
        user = User.objects.create_user(username="custom", password="x")
        user.groups.add(role.group)
        self.assertEqual(nodes_of(user), frozenset({"grab_hall.take_order"}))

    def test_tenant_admin_position_holds_every_code(self):
        """租户管理员（持「系统管理员」职位）= 全目录。期望值写**字面量**（E-0024）。

        不能写 `assertEqual(nodes_of(su), ALL_CODES)`：被断言的与决定结果的是同一个符号，
        把栏目 code 整体删掉也照样绿。字面量 `166` 是当前全目录条数（13 栏目 + 153 功能点，
        2026-11-11 企微线合并树实数）；目录增减时这条会红。
        夹具必须持「系统管理员」职位：只置超管开关不再短路。裸超管的反例在下一条。
        """
        user = User.objects.create_user(username="admin-position", password="x")
        user.groups.add(Group.objects.get(name="系统管理员"))
        held = nodes_of(user)
        self.assertEqual(len(held), 170)
        # 栏目级 + 功能点级各举一个：只数个数漏得掉「删栏目、补同样多的别的东西」。
        # （`customer.export_fund_details` 已随客户维护组收敛裁掉，2026-11-11 ⇒ 换存活点。）
        self.assertTrue({"order", "customer.member_list_self_only"} <= held)

    def test_bare_superuser_gets_no_code(self):
        """反例：只置 `is_superuser`、不持「系统管理员」职位的账号拿到**空集**（`main` 的裁定）。"""
        user = User.objects.create_superuser(username="root-bare", password="x")
        self.assertEqual(nodes_of(user), frozenset())

    def test_public_plane_guard_precedes_the_admin_short_circuit(self):
        """平面守卫必须排在**管理员短路**之前：public 上的超管也拿空集。

        本条就是 `nodes_of` 守卫顺序的钉子（`test_rbac_predicate.py` 的那两条只钉 `has_node_now`）：
        把 `nodes_of` 里的平面守卫挪到管理员分支之后，本条立刻红（`is_tenant_admin` 在 public
        平面返回 `is_superuser` ⇒ 那个分支会短路成 `ALL_CODES`）。
        用 `schema_context("public")` 直接切连接来构造该平面（判据只认连接，不看 Host）。
        """
        from django_tenants.utils import schema_context

        user = User.objects.create_superuser(username="root-pub", password="x")
        with schema_context("public"):
            self.assertEqual(nodes_of(user), frozenset())

    def test_zero_code_admin_call_is_true_and_nodes_of_is_a_frozenset(self):
        """两条接口契约：零 code 的**管理员**调用恒真；`nodes_of` 的返回值是 `frozenset`。

        ① 管理员短路是**显式**的（不是 `bool(nodes_of(user) & set(codes))`）：后者在
           `codes` 为空时是 `False`，而管理员短路应当连空入参也返回真。
           短路认「系统管理员」职位，不认裸超管开关，所以夹具必须进那个组。
        ② `-> frozenset[str]` 是接口契约（后续任务拿节点集去交边的集合），
           返回裸 `set` 时集合比较照样绿，所以类型要单独钉。
        """
        root = User.objects.create_user(username="root-zero", password="x")
        root.groups.add(Group.objects.get(name="系统管理员"))
        self.assertTrue(has_node_now(root))
        self.assertIsInstance(nodes_of(root), frozenset)
        plain = self._user_in("客服")
        self.assertFalse(has_node_now(plain))
        self.assertIsInstance(nodes_of(plain), frozenset)

    def test_nodes_of_agrees_with_has_node_now_on_every_probe(self):
        """**本任务的核心钉子**：两条路不许各判一套（E-0021 的形状）。"""
        for group_name in ("客服", "客服主管", "代练员", "系统管理员", "系统开发员"):
            user = self._user_in(group_name)
            held = nodes_of(user)
            for code in _PROBE_CODES:
                with self.subTest(group=group_name, code=code):
                    self.assertEqual(code in held, has_node_now(user, code))
        root = User.objects.create_superuser(username="root2", password="x")
        for code in _PROBE_CODES:
            with self.subTest(group="<superuser>", code=code):
                self.assertEqual(code in nodes_of(root), has_node_now(root, code))

    def test_any_semantics_is_preserved(self):
        """`has_node_now` 是 any：给一串 code，命中一个就真。"""
        user = self._user_in("代练员")
        self.assertTrue(has_node_now(user, "finance.payroll", "grab_hall.take_order"))
        self.assertFalse(has_node_now(user, "finance.payroll", "order.change_status"))
