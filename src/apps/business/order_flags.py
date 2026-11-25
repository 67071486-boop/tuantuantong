"""订单模块的限制型 flag：序列化器与 queryset 读它来过滤或脱敏，不进权限类、失败不是 403。勾了节点 = 施加限制。没勾 = 不限制。
这是刻意的：没配和明确允许在数据上分不开，新角色默认不该被一堆限制套住。系统管理员走旁路，否则会变成最受限的人；旁路认「系统管理员」职位，不认 Django 超管开关。
未登记的 code 抛错，避免拼错后限制静默失效。先校验 code、再判管理员：反过来时管理员传入拼错的 code 会静默拿到 False。视图与序列化器经下面的函数读，不要再散落 code 字面量。"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Subquery

from apps.core.permissions import is_tenant_admin
from apps.core.plane import on_public_plane
from apps.rbac.models import Role
from apps.rbac.permissions import has_node_now

#: 只能看自己录的订单（对标 148 `IsOnlyViewSelfOrder`）。
RESTRICT_TO_SELF_CREATED = "order.restrict_to_self_created"
#: 不允许查看接单价（对标 125 `IsViewOrderPrice`，对标是正着命名的）。
RESTRICT_VIEW_TAKER_PRICE = "order.restrict_view_taker_price"
#: 不能看抛单价（对标 150 `IsDontViewThrowPrice`）。
RESTRICT_VIEW_OFFER_PRICE = "order.restrict_view_offer_price"
#: 不允许编辑订单接单价格（对标 1165 `NotAllowedEditOrderPrice`）。
RESTRICT_EDIT_TAKER_PRICE = "order.restrict_edit_taker_price"
#: 不能看接手人（对标 1156；节点说明在 `apps/rbac/catalog.py`）。
RESTRICT_VIEW_TAKER = "order.restrict_view_taker"
#: 不允许查看管理订单上的统计值（对标 1201；节点说明在 `apps/rbac/catalog.py`）。
RESTRICT_VIEW_TOTAL_STATS = "order.restrict_view_total_stats"

#: 本模块承认的 flag 全集。视图与序列化器里不要再散落字符串字面量。
ORDER_FLAG_CODES = (
    RESTRICT_TO_SELF_CREATED,
    RESTRICT_VIEW_TAKER_PRICE,
    RESTRICT_VIEW_OFFER_PRICE,
    RESTRICT_EDIT_TAKER_PRICE,
    RESTRICT_VIEW_TAKER,
    RESTRICT_VIEW_TOTAL_STATS,
)


def flag_enabled(user, code):
    """该用户是否持有这条 flag（= 是否施加这条限制）。未登记的 code 抛 ValueError：拼错会变成限制静默失效。
    系统管理员旁路返回 False：限制型 flag 上「持有」=「被限制」。旁路认职位，不认 Django 超管开关。代价是管理员无法给自己套上这类限制。
    先校验 code、再判管理员。旁路只收在这个读取点，不改 `has_node_now`。"""
    if code not in ORDER_FLAG_CODES:
        raise ValueError(f"未登记的订单 flag：{code!r}；已登记的是 {ORDER_FLAG_CODES}")
    if is_tenant_admin(user):
        return False
    return has_node_now(user, code)


def can_view_taker_price(user):
    """能不能看接单价（对标 125 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_TAKER_PRICE)


def can_view_offer_price(user):
    """能不能看抛单价（对标 150 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_OFFER_PRICE)


def users_with_hidden_offer_price(user_ids):
    from apps.business.roles import TENANT_ADMIN

    if on_public_plane():
        return set()

    # DISTINCT keeps the permission catalog as a separate input to the membership lookup.
    restricting_groups = Role.objects.filter(
        permissions__code=RESTRICT_VIEW_OFFER_PRICE,
    ).order_by().values("group_id").distinct()
    admin_group = Group.objects.filter(name=TENANT_ADMIN).values("pk")
    memberships = get_user_model().groups.through.objects.filter(user_id__in=user_ids)
    restricted = memberships.filter(group_id__in=restricting_groups).values_list("user_id", flat=True)
    admins = memberships.filter(group_id=Subquery(admin_group)).values_list("user_id", flat=True)
    return set(restricted.difference(admins))


def can_edit_taker_price(user):
    """能不能编辑订单的接单价（对标 1165 的反面）。"""
    return not flag_enabled(user, RESTRICT_EDIT_TAKER_PRICE)


def can_view_taker(user):
    """能不能看接手人（对标 1156 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_TAKER)


def can_view_total_stats(user):
    """能不能看列表头部的统计值（对标 1201 的反面）。"""
    return not flag_enabled(user, RESTRICT_VIEW_TOTAL_STATS)


def scope_by_creator(user, queryset):
    """`restrict_to_self_created` 的 queryset 施加点。过滤键用 `created_by`（用户名快照），不是 `created_by_id`：订单没有这条外键。
    列宽必须与 `User.username` 同顶，更窄会让长用户名录单时 DataError → 500。
    匿名用户由外层 `IsAuthenticated` 挡住。"""
    if not flag_enabled(user, RESTRICT_TO_SELF_CREATED):
        return queryset
    return queryset.filter(created_by=getattr(user, "username", "") or "")
