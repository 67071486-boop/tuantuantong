"""账号模块的限制型 flag：输出层读它来脱敏字段，不进权限类、失败不是 403。

勾了节点 = 施加限制（看不到身份证号 / 提成参数）。没勾 = 不限制。
这是刻意的（D-0053）：「没配」和「允许」在数据上分不开，新角色默认能看见这些字段。

系统管理员走旁路，否则会变成最受限的人；旁路认「系统管理员」职位，不认 Django 超管开关。
未登记的 code 抛错，避免拼错后限制静默失效。

形状照 `apps/business/order_flags.py`，读取函数按本模块叫 `*_visible`。
不登记 `system_user.restrict_mobile_app_access`：没有消费方；写进常量表会让
「catalog 外字面量 = 已接线」那张网在它将来翻真时误判有落点。
"""
from apps.core.permissions import is_tenant_admin
from apps.rbac.permissions import has_node_now

#: 禁止查看子账号身份证号码（对标 1265 `ForbiddenToViewCertUserCid`）。
RESTRICT_VIEW_ID_CARD = "system_user.restrict_view_id_card"
#: 禁止查看其他人的提成数值参数（对标 1281 `NotAllowmodifySelfCommissionParameter`）。
RESTRICT_VIEW_COMMISSION_PARAMS = "system_user.restrict_view_commission_params"

#: 本模块承认的 flag 全集。视图与输出层不要再散落字符串字面量。
ACCOUNT_FLAG_CODES = (
    RESTRICT_VIEW_ID_CARD,
    RESTRICT_VIEW_COMMISSION_PARAMS,
)


def flag_enabled(user, code):
    """该用户是否持有这条 flag（= 是否施加这条限制）。

    未登记的 code 抛 ValueError：拼错会变成限制静默失效。
    系统管理员旁路返回 False（D-0054）：限制型 flag 上「持有」=「被限制」。
    先校验 code、再判管理员：反过来时管理员传入拼错的 code 会静默拿到 False。
    """
    if code not in ACCOUNT_FLAG_CODES:
        raise ValueError(f"未登记的账号 flag：{code!r}；已登记的是 {ACCOUNT_FLAG_CODES}")
    if is_tenant_admin(user):
        return False
    return has_node_now(user, code)


def id_card_visible(user):
    """能不能看身份证号（对标 1265 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_ID_CARD)


def commission_params_visible(user):
    """能不能看提成数值参数（对标 1281 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_COMMISSION_PARAMS)
