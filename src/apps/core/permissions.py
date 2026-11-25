"""端点级准入：职位（Group）和权限 codename 是两套判据，不要并成一个。- `HasRole` 认 Django Group 名（客服 / 客服主管 / 代练员 / 系统管理员 / 系统开发员）。
- `HasPermission` 认 `app_label.codename`，走 `user.has_perm`。租户管理员 = 持有「系统管理员」这个 Group。平台平面（`public`）仍看 `is_superuser`，那边没有这套职位表。
User 在租户库里，职位天然按租户隔开。带参权限不能直接放进 `permission_classes`：DRF 会无参再实例化一次，`HasRole("客服")` 这种已经是实例，再调用会抛 `TypeError`。小写工厂返回绑好参数的真子类；`functools.partial` 也能被无参调用，但声明不再是类。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.db import DEFAULT_DB_ALIAS, connections
from rest_framework.permissions import BasePermission

from apps.core.plane import on_public_plane


@dataclass(slots=True)
class _RequestPermissionReads:
    user: object
    database: object
    database_name: str
    schema: str
    tenant_admin: bool | None = None
    nodes: frozenset[str] | None = None


_permission_reads = ContextVar("request_permission_reads", default=None)


@contextmanager
def reuse_request_permission_reads(user):
    """Reuse permission queries for one already authenticated, read-only view."""
    database = connections[DEFAULT_DB_ALIAS]
    reads = _RequestPermissionReads(
        user, database, database.settings_dict["NAME"], database.schema_name,
    )
    token = _permission_reads.set(reads)
    try:
        yield
    finally:
        # A copied context can still hold the same record after the view returns.
        reads.user = None
        _permission_reads.reset(token)


def request_permission_reads_for(user) -> _RequestPermissionReads | None:
    reads = _permission_reads.get()
    if user is None or reads is None or reads.user is not user:
        return None
    database = connections[DEFAULT_DB_ALIAS]
    if (
        reads.database is database
        and reads.database_name == database.settings_dict["NAME"]
        and reads.schema == database.schema_name
    ):
        return reads
    return None


def is_tenant_admin(user):
    """租户管理员：租户平面认「系统管理员」Group，不认 `is_superuser`。`public` 平面没有这套职位表，运营后台菜单只靠 `is_superuser`；
    不给这条分支菜单会全空。旁路、节点全集、账号守卫和限制型 flag 都走这里，别处再写一遍会漂移。`TENANT_ADMIN` 放在函数内导入：
    `apps.core` 不在模块导入期拉起业务词汇表。`accounts.permissions.is_account_admin` 是另一处等价查询，它走库、且 public 恒为假，跟这里方向相反，不要并掉。改判据时两处一起看。"""
    from apps.business.roles import TENANT_ADMIN

    if on_public_plane():
        return bool(getattr(user, "is_superuser", False))
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    # ⚠️ **预取缓存那条快路径是承重的读取路径，不只是优化**：`accounts.payloads.account_payload` 在列表里**逐行**问这个问题（`is_tenant_admin` 字段），而 `user.groups.filter(...)` **不读** `prefetch_related("groups")` 的缓存 ⇒ 每行多一条查询，`tests/test_account_api.py::AccountListQueryCountTests`（钉「查询数不随行数增长」）会红。
    # ⚠️ **已知脏读边界（本仓实测，两个方向——不要把它当「只是优化」读）**：Django 只在**正向**管理器（`user.groups.add/remove/set/clear`）上清这个缓存；用**反向**管理器（`group.user_set.add(user)` / `remove(user)`）改组**不清** ⇒ 在预取过的 `user` 对象上本函数会给出**与库相反**的答案（`tests/test_admin_predicate_unified.py::PrefetchCacheBoundaryTests` 把两个方向都钉了下来）。**不变量（调用方要遵守）**：不得在**预取过** `groups` 的 `user` 上、用**反向**管理器改组后再依赖本函数的返回值；要改就先重新取行（新对象没有缓存），或改完显式 `user._prefetched_objects_cache.pop("groups", None)`。
    # **为什么这条边界不构成安全洞**：守卫代数（`accounts.guards.active_admin_count()` 与 `rbac.guards` 的 `dependent`）走的是 **queryset**（读库）；`is_account_admin` 的**第二次** group 查询也读库（本条快路径的安全网）。生产上唯一拿到带缓存对象的地方是 `accounts.selectors.base_queryset()` 的列表 / 详情行，它们只被读、本仓没有在它们上改组的分支。
    prefetched = getattr(user, "_prefetched_objects_cache", {}).get("groups")
    if prefetched is not None:
        return any(group.name == TENANT_ADMIN for group in prefetched)
    reads = request_permission_reads_for(user)
    if reads is not None and reads.tenant_admin is not None:
        return reads.tenant_admin
    is_admin = user.groups.filter(name=TENANT_ADMIN).exists()
    if reads is not None:
        reads.tenant_admin = is_admin
    return is_admin


def _has_role(user, name):
    """租户的管理员**无条件**持有全部职位。
    否则 `provision_tenant` 灌的无密码 `admin`（`apps/platform/provisioning.py::seed_tenant`）会因为"只有『系统管理员』
    这一个 Group"而被所有要求具体职位的端点拒掉——一个连自己租户都进不去的管理员账号。⚠️ `is_tenant_admin` 认的就是**那个 Group**（判据已从 `is_superuser` 换过来）——旁路因此仍然恰好覆盖 `seed_tenant` 的初始管理员（`provisioning.py` 把它加进该组）。"""
    if is_tenant_admin(user):
        return True
    return user.groups.filter(name=name).exists()


class HasRole(BasePermission):
    """任一职位命中即放行（`any`，不是全部命中）。

    类本身**不可**直接放进 `permission_classes`（见模块 docstring）——
    带参用法请用 `has_role(...)`，或直接 `HasRole("客服")` 当普通对象使。
    """

    #: 由 `has_role` 工厂在子类上写入；直接实例化时为空元组，此时用构造参数。
    _configured_names = ()

    def __init__(self, *names):
        self.names = names or self._configured_names

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return any(_has_role(user, name) for name in self.names)


class HasPermission(BasePermission):
    """任一 codename 命中即放行。走 Django 自己的 `has_perm`（含 superuser 短路与缓存）。

    与 `HasRole` 同理：带参用法请用 `has_permission(...)`。
    """

    _configured_codenames = ()

    def __init__(self, *codenames):
        self.codenames = codenames or self._configured_codenames

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user and user.is_authenticated and any(user.has_perm(c) for c in self.codenames)
        )


def has_role(*names):
    """`HasRole` 的工厂：返回绑好职位名的子类，供 `permission_classes` 无参实例化。"""
    return type(
        "HasRole",
        (HasRole,),
        {"_configured_names": names, "__module__": __name__},
    )


def has_permission(*codenames):
    """`HasPermission` 的**工厂**。理由同 `has_role`。"""
    return type(
        "HasPermission",
        (HasPermission,),
        {"_configured_codenames": codenames, "__module__": __name__},
    )
