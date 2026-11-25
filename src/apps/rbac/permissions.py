"""节点 code 判据。

带参用法必须是工厂：DRF 会无参实例化 `permission_classes` 里的每一项，
把 `HasNode("order.delete")` 这个实例再当类调用会抛 TypeError。
形状照 `apps/core/permissions.py`，不另立一套。

与已冻结的 Django codename 判据不是一回事：这里读的是角色上的节点 code。
不看 `available`（那只登记有没有消费方）。判据只问用户持不持有这个 code，
所以尚未翻真的点照样能拦。
"""
from rest_framework.permissions import BasePermission

from apps.core.permissions import is_tenant_admin, request_permission_reads_for
#: **转发**：平面判据的实现在 `apps/core/plane.py::on_public_plane`（本线从本模块上提，
#: 理由与"直接 import 会让基座模块也要求 settings"的实测见那里的 docstring）。
#: 保留这个名字是为了不打断本模块既有的引用点（`user_node_codes` / `has_node_now`）。
#:
#: ⚠️ **这条守卫是必需的，不是防御性编程**：`PermissionNode` / `Role` 是**租户表**
#: （`apps/rbac` 在 `TENANT_APPS`，不在 `SHARED_APPS`），`public` 平面里**没有这两张表**。
#: 不先挡掉，`user_node_codes()` 会抛 `ProgrammingError` ⇒ HTML 500。
#: 同一个坑 `apps/accounts/permissions.py::is_account_admin` 也踩过并写进了它的 docstring。
from apps.core.plane import on_public_plane as _on_public_plane

from apps.rbac.catalog import ALL_CODES
from apps.rbac.models import PermissionNode


def user_node_codes(user) -> set[str]:
    """当前用户**所有角色**的节点 code 并集。**平台平面一律空集**（fail-closed）。

    租户上下文来自连接（AGENTS.md §4）：`user.groups` 与 `Role` 都在当前 schema 内，
    这里不接受任何请求参数、也不看 Host。
    """
    if _on_public_plane():
        return set()
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    return set(
        PermissionNode.objects.filter(roles__group__in=user.groups.all())
        .values_list("code", flat=True)
        .distinct()
    )


def nodes_of(user) -> frozenset[str]:
    """该用户持有的**全部**节点 code。**平台平面 / 匿名一律空集**（fail-closed）。

    租户管理员（「系统管理员」职位，不是裸超管开关）短路为全目录。
    与 `has_node_now` 用同一个 `is_tenant_admin`，目录内两条路同判。
    目录外的串不在对拍域内：`has_node_now` 对管理员会放行任意串，本函数只返回目录里的 code。

    平面守卫必须在管理员短路之前：public 平面没有角色目录，超管不能在那里短路成真。
    """
    if _on_public_plane():
        return frozenset()
    if user is None or not getattr(user, "is_authenticated", False):
        return frozenset()
    if is_tenant_admin(user):
        return ALL_CODES
    reads = request_permission_reads_for(user)
    if reads is not None and reads.nodes is not None:
        return reads.nodes
    nodes = frozenset(user_node_codes(user))
    if reads is not None:
        reads.nodes = nodes
    return nodes


def has_node_now(user, *codes) -> bool:
    """任一命中即放行（`any`，不是全部命中）；租户管理员（持「系统管理员」职位）短路为真。

    管理员短路取自 `apps.core.permissions.is_tenant_admin`（2026-09-23 起租户平面认职位，
    平台平面仍认 `is_superuser`）——本模块**不另写一份判据**。

    ⚠️ **平面守卫必须排在管理员短路之前**：否则 public 平面的超管会短路成真，
    而那个平面上根本没有角色目录——这与 `is_account_admin` 的守卫顺序一致，别调换。
    ⚠️ 非管理员路径**委托 `nodes_of`**：这样「谁持有哪些 code」只有一处实现，
       `tests/test_rbac_permissions_axis.py` 拿它当钉子。
    """
    if _on_public_plane():
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if is_tenant_admin(user):
        return True
    return bool(nodes_of(user) & set(codes))


class HasNode(BasePermission):
    """类本身**不可**直接放进 `permission_classes`——带参用法请用 `has_node(...)`。"""

    _configured_codes = ()

    def __init__(self, *codes):
        self.codes = codes or self._configured_codes

    def has_permission(self, request, view):
        return has_node_now(request.user, *self.codes)


def has_node(*codes):
    """`HasNode` 的**工厂**：返回真类（不是实例、不是 `partial`），供 DRF 无参实例化。"""
    return type(
        "HasNode",
        (HasNode,),
        {"_configured_codes": codes, "__module__": __name__},
    )
