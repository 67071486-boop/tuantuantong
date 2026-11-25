"""账号管理员不变式的守卫：租户 schema 内必须恒有一个活跃「系统管理员」（`is_account_admin` 口径）。
边界（E-0011，如实口径）：只约束**经过本模块代码路径**的变更，直接改库 / 运维命令不受检；零管理员是
死角、不可自救（修只能走平台层 RUNBOOK，⛔ 本模块不提供后门）；各闸门与锁序见各函数 docstring。"""
from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.accounts.permissions import is_account_admin
from apps.business.roles import TENANT_ADMIN
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode

User = get_user_model()


def _admin_filter() -> Q:
    """「管理员」集合谓词，与 `permissions.is_account_admin` 同一口径：只认「系统管理员」Group
    （`TENANT_ADMIN`）**一个方向**。⛔ 不加 `Q(is_superuser=True)`：不持职位的超管被数成管理员，
    守卫会以为「还剩一个」而放行一次清零变更。`seed_tenant` 的 admin 也在该组，开张管理员不漏算。"""
    return Q(groups__name=TENANT_ADMIN)


def active_admin_count(*, exclude_pk=None) -> int:
    """活跃管理员数。`exclude_pk` 用于「变更后还剩几个**其他**管理员」——目标此刻在库里仍是
    「管理员 + 活跃」，不排掉就会把自己算成剩下的那一个，**保护形同虚设**。"""
    qs = User.objects.filter(is_active=True).filter(_admin_filter()).distinct()
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()


def check_admin_invariant(
    actor, target, *, will_be_active: bool, will_be_admin: bool, also_losing=frozenset()
):
    """三重判定（③ 自我锁定、①② 最后管理员不变式），返回 `None` 放行、否则返回**已封口**的错误响应；必须在
    管理员变更事务取得租户锁后调用。准入 `IsAccountManager` ⇒ 人事也能进本函数、
    ①② 端点层可达（用例 `HRActorOnTheAdminGuardTests` / `AdminInvariantArithmeticTests`）。"""
    # ③ 自我锁定：与不变式无关，是更严的一条（spec §6.2-③、§9-5）
    if target.pk == actor.pk:
        if not will_be_active:
            return api_response(data=None, msg="不能停用自己的账号", code=ErrorCode.SELF_LOCKOUT)
        if not will_be_admin and is_account_admin(actor):
            return api_response(
                data=None, msg="不能取消自己的管理员职位", code=ErrorCode.SELF_LOCKOUT
            )

    # ①② 不变式：变更后必须还剩至少一个**其他**活跃管理员。⚠️ `exclude_pk=target.pk` 必需：不把自己
    # 排掉就会把自己算成「剩下的那一个」⇒ 永远放行、保护形同虚设。⚠️ `also_losing` 同样必需：
    # 批量里别的成员也在被停用/降级、不能算进「剩下的」——漏掉它整批只能靠 ③ 兜底（码值 1003 变 1004）。
    losing_admin = is_account_admin(target) and (not will_be_active or not will_be_admin)
    if losing_admin:
        remaining = active_admin_count(exclude_pk=target.pk) - len(also_losing)
        if remaining <= 0:
            return api_response(
                data=None, msg="不能移除最后一个管理员", code=ErrorCode.LAST_ADMIN
            )
    return None


def check_delete_invariant(actor, target):
    """软删守卫：删自己 ⇒ `SELF_LOCKOUT`(1004)（文案须说明是「删除」），删后零活跃管理员 ⇒ `LAST_ADMIN`(1003)；
    调用纪律同 `check_admin_invariant`（锁在算术之前），不复用它只因文案分叉、判据 / 码值同源。
    准入 `IsAccountManager` ⇒ 1003 支端点层可达：`SoftDeleteGuardWithAnHRActorTests`（`tests/test_account_soft_delete.py`）。"""
    if target.pk == actor.pk:
        return api_response(data=None, msg="不能删除自己的账号", code=ErrorCode.SELF_LOCKOUT)
    if is_account_admin(target) and target.is_active:
        if active_admin_count(exclude_pk=target.pk) <= 0:
            return api_response(data=None, msg="不能删除最后一个管理员", code=ErrorCode.LAST_ADMIN)
    return None


def check_role_grant_invariant(actor, role_name):
    """**非系统管理员不得授予「系统管理员」职位**（含他自己）。⛔ 不限制其它职位的授予（那是人事的
    本职）、不动节点门与 `check_admin_invariant`。码值 `BUSINESS`(1002) + 一句人话 `msg`（选码理由两函数
    同族，统一见 `check_admin_target_invariant`）。**调用点必须覆盖全部会改职位归属的请求写路径**：今天
    只有 `_persist_account(role_name=…)` 写 `user.groups`（被 `create` / `update` 调用 ⇒ 恰好两处各判一次，
    `create` 在任何写之前、`update` 在 `atomic()` 内）；其余账号动作不碰 `groups`，rbac 角色 CRUD 改的是
    职位定义而非归属且有自己的节点门；运维 / E2E 命令直写 `groups` **不经过**本判据（同模块边界）。
    将来新增写 `groups` 的请求路径必须在那一处也调用本函数。判据：
    `tests/test_account_admin_guard.py::RoleGrantInvariantTests`（终态断言：提权失败后
    `is_account_admin` 仍 False、首页概览仍 403）。"""
    if role_name != TENANT_ADMIN:
        return None
    if is_account_admin(actor):
        return None
    return api_response(
        data=None,
        msg="只有系统管理员可以授予「系统管理员」职位",
        code=ErrorCode.BUSINESS,
    )


def check_admin_target_invariant(actor, target, *, action):
    """管理员身份和登录能力仅由管理员管理，普通资料可按既有权限编辑。
    在最后管理员与自锁检查之后执行，保留更具体的拒绝原因。
    """
    if is_account_admin(actor):
        return None
    if not is_account_admin(target):
        return None
    return api_response(
        data=None,
        msg=f"目标账号是系统管理员，只有系统管理员可以{action}",
        code=ErrorCode.BUSINESS,
    )


def _losing_admin_pks(users, *, is_active: bool) -> frozenset:
    """`users` 这批里「变更后**会失去**活跃管理员身份」的那些 pk。⚠️ `u.is_active` 这一条必须保留：
    `active_admin_count()` 只数**当前活跃**的管理员，把一个**已停用**的管理员也扣一次会让计数凭空
    少 1——「勾选里含一行已停用管理员、再批量停用其余（还有第三个管理员在）」会被误拒。"""
    return frozenset(
        u.pk for u in users if u.is_active and is_account_admin(u) and not is_active
    )


def apply_status(actor, users, is_active):
    """在已取得租户锁的事务中先校验整批，再写入，避免部分生效。
    updated 统计请求涉及的账号，包含原本已处于目标状态的账号。
    """
    also_losing = _losing_admin_pks(users, is_active=is_active)
    for user in users:
        blocked = check_admin_invariant(
            actor,
            user,
            will_be_active=is_active,
            will_be_admin=is_account_admin(user),
            also_losing=also_losing - {user.pk},
        )
        if blocked is not None:
            return blocked
        blocked = check_admin_target_invariant(actor, user, action="停用或启用其账号")
        if blocked is not None:
            return blocked

    for user in users:
        user.is_active = is_active
        user.save(update_fields=["is_active"])
    return api_response({"updated": [user.pk for user in users]})
