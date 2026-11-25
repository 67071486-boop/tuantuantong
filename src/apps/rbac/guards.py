"""角色改名与删除的守卫，共用账号域的租户锁和活跃管理员计数。"""
from django.contrib.auth import get_user_model

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.accounts.guards import active_admin_count
from apps.business.roles import TENANT_ADMIN
from apps.rbac.services import RoleServiceError

User = get_user_model()


def ensure_not_last_admin_role(role, *, new_name=None):
    """在变更事务内检查角色是否离开管理员职位，或通过改名把活跃成员升成管理员。
    涉及管理员身份时先取租户锁，再统计受影响的人。
    """
    leaving = role.name == TENANT_ADMIN and new_name != TENANT_ADMIN
    entering = role.name != TENANT_ADMIN and new_name == TENANT_ADMIN
    if not (leaving or entering):
        return
    lock_tenant_admin_mutations()
    # ⚠️ **不再排除 superuser**（判据统一）：`_admin_filter()` 已是**纯职位**判据，
    # 所以该 Group 的**每一个**活跃成员都会因改名 / 删除失去管理员身份（超管也不例外）。
    # 留着旧的 `is_superuser=False` 会把"超管 + 在组"的人当成**与角色无关的幸存者**，
    # 守卫于是放行一次会清零管理员的改名 / 删除（`tests/test_admin_predicate_unified.py`
    # 的 `AdminRoleGuardAlgebraTests` 与 `tests/test_rbac_rename_guard.py` 钉这一点）。
    dependent = (
        User.objects.filter(is_active=True)
        .filter(groups=role.group)
        .distinct()
        .count()
    )
    if entering:
        if dependent > 0:
            raise RoleServiceError(
                "role_admin_escalation",
                f"不能把「{role.name}」改名为系统管理员："
                f"该职位的 {dependent} 个活跃成员会一次性获得管理员身份",
            )
        return
    if active_admin_count() - dependent <= 0:
        raise RoleServiceError(
            "last_admin_role",
            "不能删除最后一个系统管理员角色：删除后本租户将没有任何管理员账号"
            if new_name is None
            else "不能把最后一个系统管理员角色改名：改名后本租户将没有任何管理员账号",
        )


def ensure_role_unused(role):
    """删 `role` 之前调用：只要该职位**还挂着用户**就拒。

    ⚠️ **判据含已停用账号**（`user_set`，不过滤 `is_active`）：停用账号仍挂在组上，
    而 `delete_role` 会把同名 `Group` 一起删 ⇒ 它会当场丢职位（业务方裁定）。

    ⚠️ **只守删除、不守改名**：账号挂的是 `Group` 行，`rename_role` 把组一起改名、
    成员跟着走 ⇒ 不会孤儿化。**不要**顺手加到 `update` 上（那会拦住一个合法操作）。

    ⚠️ **不取锁**：本函数只做普通 `SELECT`，`ensure_not_last_admin_role` 的
    `pg_advisory_xact_lock` 必须仍是本事务**第一条取锁语句**（既有不变式）。

    ⚠️ **存在一个并发窗口（已知残差，本轮有意不改）**：`user_set.count() == 0` 判定之后、
    调用方 `delete_role` 的 `Group.delete()` 之前，另一个请求可以把用户加进该组 ⇒
    级联删掉那条 M2M、**用户静默丢职位** —— 正是本守卫要防的那件事。
    **为什么不在这里堵**：① 在本函数里对 `Group` 行取 `select_for_update` **并不能**堵住它 ——
    并发方是往 M2M 中间表 **INSERT** 一行，行锁挡不住；真要堵住得让「计数 + 删除」与「加成员」
    争**同一把锁**（例如在同一个 `advisory` 锁下做两件事），那属**另一条线**的改动；
    ② 本函数保持只读普通 `SELECT`，也就不引入新的取锁点（`ensure_not_last_admin_role` 的
    `pg_advisory_xact_lock` 由调用方排在**本函数之前**，故仍满足上面那条不变式）。
    spec §3.2 那句「并发判别力照账号线的 `pg_advisory_xact_lock` 做法」
    与实现**不符**（本守卫不取锁、新用例也全是单线程），已在 spec 文末「执行期修订 9」更正。

    失败形态：`RoleServiceError("role_in_use", …)` —— 第一个参数是 **DRF 的字段键**
    （与 `last_admin_role` 同形），不是新错误码；视图经 `_run` 翻成 200 + `code 1000`。
    """
    total = role.group.user_set.count()
    if total == 0:
        return
    inactive = role.group.user_set.filter(is_active=False).count()
    raise RoleServiceError(
        "role_in_use",
        f"该职位还有 {total} 个系统用户在使用（其中 {inactive} 个已停用），请先改掉他们的职位",
    )
