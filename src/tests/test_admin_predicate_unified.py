"""「租户管理员」判据统一后的单一口径（业务方 2026-09-23 裁定）。

**裁定**：租户平面上的「租户管理员」= **持有租户内「系统管理员」职位（Group）**，
**不再看** Django 的 `is_superuser`；平台平面（`public`）**单独保留一条**仍看
`is_superuser` 的分支（否则运营后台 SPA 菜单会全空——`menu_permissions` 在 public 是空集）。

**本文件为什么存在**：口径变更后「谁算管理员」这件事在四个判据本体 + 两处 flag 旁路
里必须**同批**一致（少改一处就留下「能把最后一个管理员角色删掉」的锁死洞）。本文件
逐处钉**字面布尔**（E-0024：期望值不引用导出常量），并在每一格注明它的**判别力**：

- 实现**前**：①②④⑤⑥(b)⑥(c)⑦ 与守卫代数那一类会红（旧实现认 `is_superuser`）；
- ③（平台平面）**前后都为真**——它是「分支不许丢」的**回归钉子**，判别力来自
  「删掉 `is_tenant_admin` 的 public 分支 ⇒ 本格红」（变异①）。

⚠️ **与任务书 Step 1 的一处实施期偏差（如实登记）**：任务书 ⑥ 写「不持组的超管 ⇒
限制型 flag 返回 `has_node_now` 的结果，即 `False`」。**只按那一条写，这一格实现前后都是
`False`**（旧实现的 superuser 旁路同样返回 `False`）⇒ 它**没有判别力**、也无法承担
变异④（删掉旁路）。所以本文件把 ⑥ 拆成三格：

- (a) 字面口径：不持任何组的超管 ⇒ `False`（与任务书一致）；
- (b) **不持管理职位、但所在角色持有限制型 flag 的超管** ⇒ `True`
      ——这一格才把「旧 `is_superuser` 旁路」与「新职位判据」分开（旧实现返回 `False`）；
- (c) **持管理职位**（且该职位自己持有限制型 flag）⇒ `False`
      ——承重判据「管理员不该变成最受限的人」，删掉旁路（变异④）时它红。

⚠️ **评审 B2 追加**：`is_tenant_admin` 的预取缓存快路径有一条**已知脏读边界**
（反向管理器改组不清缓存）。`PrefetchCacheBoundaryTests` 钉住"守卫代数与账号门的安全网
都**走库**"，并把那条边界本身也实测钉下来。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context

from apps.accounts.guards import _admin_filter, active_admin_count
from apps.business.roles import TENANT_ADMIN
from apps.core.permissions import is_tenant_admin
from apps.rbac.guards import ensure_not_last_admin_role
from apps.rbac.models import PermissionNode, Role
from apps.rbac.permissions import has_node_now, nodes_of
from apps.rbac.seed import seed_catalog
from apps.rbac.services import RoleServiceError

User = get_user_model()

#: 两处读取点各取一条**限制型** flag 的 code。写字面值而不是导入常量：断言与被断言
#: 的东西不能是同一个符号（E-0024）。
ORDER_FLAG = "order.restrict_view_taker_price"
ACCOUNT_FLAG = "system_user.restrict_view_id_card"
#: 一条普通业务节点，用来证明「持角色 ⇒ 按该角色的授权返回」而不是恒真。
PLAIN_NODE = "order.list"


class TenantAdminPredicateTests(TenantTestCase):
    """①–⑦：单一口径 + 平台分支 + 反例。"""

    def setUp(self):
        seed_catalog()
        # 管理职位：「系统管理员」Group + 同名 Role。**刻意给它挂上两条限制型 flag**：
        # ⑥(c) 要靠「节点确实挂着却仍不施加限制」才有判别力（否则旁路删不删都一样）。
        self.admin_group = Group.objects.create(name=TENANT_ADMIN)
        self.admin_role = Role.objects.create(name=TENANT_ADMIN, group=self.admin_group)
        self.admin_role.permissions.set(
            PermissionNode.objects.filter(code__in=(ORDER_FLAG, ACCOUNT_FLAG))
        )
        # 一个**非管理员**职位：持一条普通节点 + 两条限制型 flag，供 ④⑤⑥(b) 的反面用。
        self.service_group = Group.objects.create(name="客服")
        self.service_role = Role.objects.create(name="客服", group=self.service_group)
        self.service_role.permissions.set(
            PermissionNode.objects.filter(code__in=(PLAIN_NODE, ORDER_FLAG, ACCOUNT_FLAG))
        )

    def _user(self, username, *, superuser=False, in_admin_group=False, in_service_group=False):
        user = User.objects.create_user(username=username, password="pw-123456")
        if superuser:
            user.is_superuser = True
            user.save(update_fields=["is_superuser"])
        if in_admin_group:
            user.groups.add(self.admin_group)
        if in_service_group:
            user.groups.add(self.service_group)
        return user

    # ── ① 租户平面：不持组的超管**不是**管理员（与今天相反，这就是本线的口径变更）──
    def test_tenant_plane_superuser_without_the_group_is_not_admin(self):
        user = self._user("su-bare", superuser=True)
        self.assertIs(user.groups.exists(), False, "构造失败：本账号不该属于任何组")
        self.assertIs(is_tenant_admin(user), False)

    # ── ② 租户平面：非超管但持「系统管理员」职位 ⇒ 是管理员 ─────────────────
    def test_tenant_plane_group_holder_is_admin(self):
        user = self._user("group-holder", in_admin_group=True)
        self.assertIs(user.is_superuser, False, "构造失败：本账号不该是超管")
        self.assertIs(is_tenant_admin(user), True)

    # ── ③ 平台平面：超管 ⇒ 真（**分支不许丢**；变异① 打在它上面）────────────
    def test_public_plane_keeps_the_superuser_branch(self):
        user = self._user("su-public", superuser=True)
        with schema_context("public"):
            self.assertIs(is_tenant_admin(user), True)

    # ── ④ nodes_of：持组 ⇒ 全目录；不持组的超管 ⇒ 按他实际职位算 ──────────────
    #: 出口是 `nodes_of`。管理职位拿到全目录；裸超管是空集。
    #: 条数写字面量，加一个目录节点这里就红。
    def test_nodes_of_gives_every_code_to_the_position_holder(self):
        user = self._user("codes-holder", in_admin_group=True)
        # 170 = 13 栏目行 + 157 功能点，含审批中心的 4 个节点。
        self.assertEqual(len(nodes_of(user)), 170)

    def test_nodes_of_gives_nothing_to_a_bare_superuser(self):
        user = self._user("codes-su-bare", superuser=True)
        self.assertEqual(nodes_of(user), frozenset())

    # ── ⑤ has_node_now：不再看超管标志；持角色按该角色的授权返回 ─────────────
    def test_has_node_now_ignores_a_bare_superuser(self):
        user = self._user("node-su-bare", superuser=True)
        self.assertIs(has_node_now(user, PLAIN_NODE), False)
        self.assertIs(has_node_now(user, ORDER_FLAG), False)

    def test_has_node_now_follows_the_role_of_a_non_admin_holder(self):
        """非管理职位的人：持的节点为真、没持的为假（证明判据落在角色授权上）。"""
        user = self._user("node-service", in_service_group=True)
        self.assertIs(has_node_now(user, PLAIN_NODE), True)
        self.assertIs(has_node_now(user, "order.delete"), False)

    def test_has_node_now_passes_any_code_for_the_admin_position_holder(self):
        """管理职位的人拿到管理员短路（`has_node_now` 的那条 `is_tenant_admin` 分支）。"""
        user = self._user("node-admin", in_admin_group=True)
        self.assertIs(has_node_now(user, PLAIN_NODE), True)
        self.assertIs(has_node_now(user, "order.delete"), True)

    # ── ⑥ 两处 flag 读取点：旁路判据换成职位（不是删掉）───────────────────
    def test_order_flag_bypass_requires_the_position_not_the_django_flag(self):
        # (a) 字面口径：不持任何组的超管 ⇒ 不施加限制（旧旁路也返回 False，本格无判别力，
        #     判别力在 (b)/(c)；保留它是为了钉住任务书明写的那一格）。
        bare = self._user("flag-su-bare", superuser=True)
        self.assertIs(_order_flag(bare, ORDER_FLAG), False)
        # (b) 超管 + **非管理**职位持该 flag ⇒ 限制**施加**（旧实现会因超管标志返回 False）。
        su_with_restriction = self._user(
            "flag-su-service", superuser=True, in_service_group=True
        )
        self.assertIs(_order_flag(su_with_restriction, ORDER_FLAG), True)
        # (c) 持管理职位（职位自己持该 flag）⇒ 仍旁路（管理员不该变成最受限的人）。
        admin_holder = self._user("flag-admin", in_admin_group=True)
        self.assertIs(has_node_now(admin_holder, ORDER_FLAG), True, "前置：职位确实持该节点")
        self.assertIs(_order_flag(admin_holder, ORDER_FLAG), False)

    def test_account_flag_bypass_requires_the_position_not_the_django_flag(self):
        bare = self._user("accflag-su-bare", superuser=True)
        self.assertIs(_account_flag(bare, ACCOUNT_FLAG), False)
        su_with_restriction = self._user(
            "accflag-su-service", superuser=True, in_service_group=True
        )
        self.assertIs(_account_flag(su_with_restriction, ACCOUNT_FLAG), True)
        admin_holder = self._user("accflag-admin", in_admin_group=True)
        self.assertIs(has_node_now(admin_holder, ACCOUNT_FLAG), True, "前置：职位确实持该节点")
        self.assertIs(_account_flag(admin_holder, ACCOUNT_FLAG), False)

    # ── ⑦ _admin_filter：数出来的人 = 组内活跃成员 ──────────────────────────
    def test_admin_filter_counts_only_the_active_position_members(self):
        self._user("af-su-bare", superuser=True)             # 超管、不在组 ⇒ 不算
        self._user("af-holder", in_admin_group=True)         # 在组 ⇒ 算
        self._user("af-plain")                               # 普通账号 ⇒ 不算
        self.assertEqual(
            User.objects.filter(is_active=True).filter(_admin_filter()).distinct().count(), 1
        )
        self.assertEqual(active_admin_count(), 1)

    def test_admin_filter_ignores_an_inactive_position_member(self):
        """反例：组内但**停用**的成员不计入（`active_admin_count` 的 `is_active=True`）。"""
        holder = self._user("af-inactive", in_admin_group=True)
        holder.is_active = False
        holder.save(update_fields=["is_active"])
        self.assertEqual(active_admin_count(), 0)


def _order_flag(user, code):
    from apps.business.order_flags import flag_enabled

    return flag_enabled(user, code)


def _account_flag(user, code):
    from apps.accounts.account_flags import flag_enabled

    return flag_enabled(user, code)


class AdminRoleGuardAlgebraTests(TenantTestCase):
    """`rbac/guards.py::dependent` 的代数必须与新判据同批改。

    **锁死洞的形状**：`_admin_filter()` 已是**纯职位**判据 ⇒ 删除 / 改名「系统管理员」
    这个角色时，该 Group 的**每一个**活跃成员都会失去管理员身份（`eligible` = 全部）。
    若 `dependent` 仍把 superuser 排除（旧写法 `is_superuser=False`），一个
    「超管 + 在组」的账号会被当成**与角色无关的幸存者** ⇒ 守卫**放行** ⇒ 改名/删除后
    本租户零管理员（不可自救）。

    判别力：本用例的实现前形态是 `1 - 0 > 0 ⇒ 放行`，因此它会红；变异③（恢复
    `is_superuser=False`）时同样红。
    """

    def setUp(self):
        seed_catalog()
        from apps.rbac.seed import seed_builtin_roles

        seed_builtin_roles()
        self.admin_group = Group.objects.get(name=TENANT_ADMIN)

    def test_a_superuser_inside_the_group_is_fated_with_the_role(self):
        su = User.objects.create_superuser(
            username="guard-su-in-group", password="pw-123456"
        )
        su.groups.add(self.admin_group)
        self.assertIs(su.is_superuser, True, "构造失败：本账号应是超管")
        self.assertIs(su.groups.filter(name=TENANT_ADMIN).exists(), True, "构造失败：应在组里")
        self.assertEqual(active_admin_count(), 1, "构造失败：他应是唯一活跃管理员")
        with self.assertRaises(RoleServiceError):
            ensure_not_last_admin_role(Role.objects.get(name=TENANT_ADMIN))


class PrefetchCacheBoundaryTests(TenantTestCase):
    """预取缓存的**已知脏读边界**（评审 B2）与为什么守卫不受它影响。

    **边界（本文件实测，两个方向）**：Django 只在**正向**管理器
    （`user.groups.add/remove/set`）上清 `prefetch_related("groups")` 的缓存；
    用**反向**管理器（`group.user_set.add(user)` / `remove(user)`）改组**不清**
    ⇒ 在预取过的 `user` 对象上，`is_tenant_admin` 会给出**与库相反**的答案。

    **为什么这不构成安全洞**：
    - 守卫代数（`active_admin_count()`、`rbac/guards.py` 的 `dependent`）**走 queryset**（读库）；
    - `is_account_admin` 的**第二次** group 查询也**走库**（`accounts/permissions.py` 里那行
      ——它是那条快路径的**安全网**，不是冗余），它兜住"库里有、缓存说没有"这个方向
      （新加的管理员不会被漏掉）。反方向（"缓存说是、库说不是"）会让 `is_tenant_admin`
      偏宽松，但**守卫代数仍按库算**，最后管理员不变式不受影响。

    本类把这两件事钉住：有人把守卫或那行安全网改成走 `is_tenant_admin`（即读缓存）时会红。
    ⚠️ 生产上唯一会拿到"带 `groups` 预取缓存"的对象的地方是
    `accounts.selectors.base_queryset()`（列表 / 详情行），那些对象只被**读**
    （`account_payload` 问 `is_tenant_admin`），本仓没有在它们上面改组的分支。
    """

    def setUp(self):
        seed_catalog()
        from apps.rbac.seed import seed_builtin_roles

        seed_builtin_roles()
        self.admin_group = Group.objects.get(name=TENANT_ADMIN)

    def test_the_guard_and_the_account_gate_read_the_database_not_the_cache(self):
        from apps.accounts.permissions import is_account_admin

        user = User.objects.create_user(username="prefetch-plain", password="pw-123456")
        # 取一个**带 groups 预取缓存**的对象（缓存里没有「系统管理员」）。
        user = User.objects.prefetch_related("groups").get(pk=user.pk)
        self.assertIs(
            any(g.name == TENANT_ADMIN for g in user._prefetched_objects_cache["groups"]),
            False,
            "构造失败：预取缓存里不该有「系统管理员」",
        )
        self.assertIs(active_admin_count(), 0, "构造失败：此刻库里还没有管理员")
        # ⚠️ **反向管理器**改组：库变了、缓存**没变**（这就是那条已知边界）。
        self.admin_group.user_set.add(user)
        self.assertIs(
            User.objects.get(pk=user.pk).groups.filter(name=TENANT_ADMIN).exists(),
            True,
            "构造失败：库里应当已经加进去了",
        )
        self.assertIs(
            is_tenant_admin(user),
            False,
            "已知脏读（本条钉的是这个事实本身）：缓存让 is_tenant_admin 看到旧值。"
            "若哪天真被框架修掉，这条会红——那时请连同 core/permissions.py 的边界注释"
            "与 accounts/permissions.py 的安全网说明一起更新。",
        )
        # ① 守卫代数**走库**：与库一致（不是缓存）。
        self.assertEqual(active_admin_count(), 1)
        # ② 账号门的安全网**走库**：第二次查询把"库里有、缓存说没有"纠正回来。
        self.assertIs(is_account_admin(user), True)

    def test_the_reverse_direction_of_the_stale_cache_still_leaves_the_guard_on_the_database(self):
        """反方向（缓存说"是"、库说"不是"）：`is_tenant_admin` 偏宽松，守卫仍按库算 0。"""
        user = User.objects.create_user(username="prefetch-admin", password="pw-123456")
        user.groups.add(self.admin_group)
        user = User.objects.prefetch_related("groups").get(pk=user.pk)
        self.admin_group.user_set.remove(user)
        self.assertIs(
            User.objects.get(pk=user.pk).groups.filter(name=TENANT_ADMIN).exists(),
            False,
            "构造失败：库里应当已经移出了",
        )
        self.assertIs(
            is_tenant_admin(user),
            True,
            "已知脏读的反方向：缓存仍在 ⇒ 看到旧值 True（库已经是 False）",
        )
        self.assertEqual(active_admin_count(), 0, "守卫代数必须按库算，不能按缓存算")
