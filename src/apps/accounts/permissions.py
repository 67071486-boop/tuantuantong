"""账号准入：管本租户账号，平台平面（`public`）一律 False。租户平面上 `is_account_admin` 与 `is_tenant_admin` 同真同假（都认「系统管理员」职位，不认只置了超管开关的账号）；平台平面上本判据恒 False、而 `is_tenant_admin` 仍看超管，所以两个函数不能合并。
`is_account_manager` 是另一扇门：系统管理员，或持任一账号管理节点。人事要能管子账号，但不能因此放宽 `is_account_admin`——那条还管首页概览、请求耗时页和前端 `session.isAccountAdmin`；逐动作节点门不动。
"""
from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.permissions import BasePermission

from apps.business.roles import TENANT_ADMIN
from apps.core.permissions import is_tenant_admin
from apps.rbac.permissions import nodes_of

#: 账号端点用到的全部节点 code。不 import 视图里那份 dict：模块级会成环，
#: 而且视图那份必须保持字面量（结构网用 AST 取字符串常量）。两份「恰好相等」由结构网钉住。
#: flag 类节点不在这里：它们是限制开关，不是「能管账号」的凭据。
ACCOUNT_MANAGEMENT_NODES = frozenset({
    "system_user.list",
    "system_user.add_edit",
    "system_user.change_status",
    "system_user.reset_password",
    "system_user.realname_records",
    "system_user.reset_realname",
})


def is_account_admin(user) -> bool:
    """能不能管本租户的账号。匿名 / `None` 一律 False（fail-closed）。
    平台平面（`public`）恒 False：账号档案只在租户 schema，放行会打到不存在的表上变成 500；判据读连接上的 `schema_name`，不看 Host、也不看请求参数。租户平面委托 `is_tenant_admin`（持「系统管理员」职位）；只置超管开关、不持职位的不算。
    """
    if connection.schema_name == get_public_schema_name():
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if is_tenant_admin(user):
        return True
    # ⚠️ **这一行是上面那条快路径的安全网，不是冗余**（⛔ 不要把它"合并"成 `is_tenant_admin(user)`）：`is_tenant_admin` 对**预取过** `groups` 的对象走缓存，而 Django 只在**正向**管理器（`user.groups.add/remove/set`）上清缓存；用**反向**管理器（`group.user_set.add(user)`）改组**不清** ⇒ 缓存会说"还不是管理员"（库里已经是）——这条 `filter(...)` **走库**，恰好把那个方向纠正回来（新加的管理员不会被漏掉）。
    # 机制与两个方向的实测见 `tests/test_admin_predicate_unified.py::PrefetchCacheBoundaryTests` 与 `apps/core/permissions.py::is_tenant_admin` 里"已知脏读边界"那段。
    return user.groups.filter(name=TENANT_ADMIN).exists()


def is_account_manager(user) -> bool:
    """账号端点的准入：系统管理员，或持任一账号管理节点。
    第一支委托 `is_account_admin`（`public` 平面恒 False 跟着继承）；第二支只回答「是不是管账号这块」，具体动作仍由节点门决定——只持 `system_user.list` 过得了这扇门，`POST /api/accounts/` 仍会按 `add_edit` 403。已登记动作上，过节点门就蕴含本判据为真，它不承重；承重的是未登记动作：`get_permissions()` 对那些 action 只返回默认类。
    """
    if is_account_admin(user):
        return True
    return bool(nodes_of(user) & ACCOUNT_MANAGEMENT_NODES)


class IsAccountAdmin(BasePermission):
    """无参可实例化的权限类（DRF 会无参构造 `permission_classes` 里的每一项）。"""

    def has_permission(self, request, view):
        return is_account_admin(request.user)


class IsAccountManager(BasePermission):
    """账号端点的准入类。不要拿它换掉首页概览、请求耗时页和 `me` 载荷上的 `IsAccountAdmin`。"""

    def has_permission(self, request, view):
        return is_account_manager(request.user)
