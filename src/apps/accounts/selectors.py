"""账号列表 / 详情的查询入口；列表的筛选条件只在这里加。租户上下文由 django-tenants 的连接 `search_path` 提供——**不接受任何请求参数**（AGENTS.md §4）；跨租户读在这里没有入口：`User.objects` 拿到的就是当前 schema 的行。
`select_related` / `prefetch_related` 是列表页的 N+1 防线：载荷会摸 `staff_profile`、`staff_profile__opened_by`（开户人显示名 `createUserName`）、`groups`（在 `payloads.account_payload` 里做名称定序）与 `staff_profile__games` 四个关系，不预取就会按行数放大查询。
"""
from django.contrib.auth import get_user_model
from apps.business.roles import WORKER
from django.db.models import Q

from apps.accounts.models import StaffProfile

User = get_user_model()


def base_queryset(*, include_deleted=False):
    """列表 / 详情共用的基础集。`order_by("id")` 是**确定性给定**：`auth.User.Meta` 不带 `ordering`，不显式排序时分页结果的跨页归属取决于数据库返回顺序（同一行可能在两页里各出现一次，或都不出现）。默认排除已软删的人：`deleted_at` 有值即视为已删。
    用 `exclude(staff_profile__deleted_at__isnull=False)` 不用 `filter(...isnull=True)`：后者是 INNER JOIN、会把没有档案行的账号（如按设计没有 `StaffProfile` 的 `admin`）一并丢掉；`exclude` 实际是 LEFT OUTER JOIN + `NOT (deleted_at IS NOT NULL)`，没档案的行补来的 NULL 让条件为真、人还在。`include_deleted=True` 时不过滤——软删动作自己的取对象路径要它（默认排除已删，已删的人 `get_object()` 会 404）。
    """
    qs = (
        User.objects.select_related("staff_profile", "staff_profile__opened_by")
        .prefetch_related("groups", "staff_profile__games")
        .order_by("id")
    )
    if not include_deleted:
        qs = qs.exclude(staff_profile__deleted_at__isnull=False)
    return qs


def list_accounts(
    *,
    role=None,
    is_active=None,
    key=None,
    game=None,
    opened_by=None,
    business_type=None,
    member_type=None,
    is_studio=None,
    is_worker=None,
    is_cert=None,
    settle_forbidden=None,
    key2=None,
    key3=None,
    deleted=None,
):
    """按筛选条件取账号。`role` / `game` / `key` / `key2` / `key3` 都会跨表 join（一对多 / 多对多），末尾的 `distinct()` **保留**：`role` 是多选（`groups__name__in=` 做 OR），`__in` 的 join 会为每个匹配职位各产出一行、一个账号同时持两个被筛职位时会重复出现（`total` 也翻倍），去掉 `distinct()` 会让「持两个被筛职位只出现一次」变红。⚠️ **数值 / 布尔的边界判据一律 `is not None`，不要用真值**（E-0021）：`settle_forbidden=False`（只看未禁止结算的人）与 `business_type=0`（代练+陪玩）都是**合法且有意义的取值**，真值写法会把它们当成「没传」、静默丢掉筛选条件。
    ⚠️ `is_cert` 的两支不对称：`Y` 只看有号码的；`N` 是「号码为空**或没有档案行**」（没档案的人显然没实名），所以 `N` 那支必须带 `staff_profile__isnull=True`，否则所有没有档案的账号会被漏掉。`deleted=True` ⇒ 只看已删除的人（恢复入口用）；其余（`None` / `False`）⇒ 默认口径（**排除已删**），它**不动** `is_active` 的语义。
    """
    if deleted:
        qs = base_queryset(include_deleted=True).filter(staff_profile__deleted_at__isnull=False)
    else:
        qs = base_queryset()
    # `role` 是职位列表：`__in` = OR，多值 join 会重复行，
    # 靠末尾的 `distinct()` 收口（见 docstring）。
    if role:
        qs = qs.filter(groups__name__in=role)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    if key:
        qs = qs.filter(
            Q(username__icontains=key)
            | Q(first_name__icontains=key)
            | Q(staff_profile__qq__icontains=key)
            | Q(staff_profile__mobile__icontains=key)
        )
    # `games` 是多对多，join 面最宽的一条；`distinct()` 的如实口径见函数 docstring。
    if game is not None:
        qs = qs.filter(staff_profile__games__id=game)
    if opened_by is not None:
        qs = qs.filter(staff_profile__opened_by_id=opened_by)
    if business_type is not None:
        qs = qs.filter(staff_profile__business_type=business_type)
    if member_type is not None:
        qs = qs.filter(staff_profile__member_type=member_type)
    # ⭐ 工作室分档（D-0228）：is_studio=False 是「打手」那一档的**显式取值**，
    # 判据 is not None（E-0021）——真值判断会把「只看打手」静默变成「不过滤」。
    if is_studio is not None:
        qs = qs.filter(staff_profile__is_studio=is_studio)
    # ⭐ 打手身份（D-0228 改准：按**职位**不按成员类型——业务方的识别习惯是「持代练员职位的人」，且对标自己的 userType tooltip 就写着「并无实际作用」）。`is_worker=False`（内部员工那一档）同样是显式取值（E-0021）；多对多 join 由函数末尾既有的 `distinct()` 去重（先例：`role` 的 `__in` 过滤）。
    if is_worker is not None:
        if is_worker:
            qs = qs.filter(groups__name=WORKER)
        else:
            qs = qs.exclude(groups__name=WORKER)
    if is_cert is not None:
        # `Y` ⇒ 有身份证号；`N` ⇒ 空**或没有档案行**（没档案的人显然没实名）。
        if is_cert == "Y":
            qs = qs.filter(staff_profile__id_card_no__gt="")
        else:
            qs = qs.filter(Q(staff_profile__isnull=True) | Q(staff_profile__id_card_no=""))
    if settle_forbidden is not None:
        qs = qs.filter(staff_profile__settle_forbidden=settle_forbidden)
    if key2:
        qs = qs.filter(
            Q(staff_profile__alipay_account__icontains=key2)
            | Q(staff_profile__alipay_name__icontains=key2)
        )
    if key3:
        qs = qs.filter(
            Q(staff_profile__remark__icontains=key3) | Q(staff_profile__tag__icontains=key3)
        )
    return qs.distinct()


def settle_forbidden_user_ids(user_ids=None):
    """这批 user id 里**被禁止结算**的那些（一次查询）；`user_ids=None` 回**全租户**被禁集（不加 `user_id__in`，绩效列表「打手范围」筛选消费；与按名单传入恒可区分——名单语义是 `[]` 回空集，既有用例钉着）。结账（`apps.business.settlement.settle_performances` 第 2 步的两条腿与其 `_reject_message`）与扣点（`apps.business.api.performance_views.PerformanceViewSet.adjust`）都调它，**不各自写一份过滤条件**（E-0021 同族：同一事实两套判据，只用对了一套）。⚠️ **范围写死**（E-0011：绝对句必须能被 grep 验证）：`list_accounts` 里另有一处 `staff_profile__settle_forbidden=` 是**列表筛选**、不是拦截判据；「唯一来源」只覆盖这些**消费点**。
    ⚠️ 只回**当前租户 schema 里存在**的行——租户上下文由连接的 `search_path` 提供，不接受请求参数（`AGENTS.md` §4）；跨租户判据见 `tests/test_settle_forbidden_selector.py::SettleForbiddenCrossTenantTests`。判据用 `settle_forbidden=True` 显式比较，不用真值（E-0021）。⚠️ **没有档案行**的人**不在**返回值里（INNER JOIN）：调用方据此**保留**他——「查不到档案」不等于「被冻结」，没有档案行的账号（如 `seed_tenant` 灌的 `admin`）不该被挡在结账之外；判别力用例 `tests/test_settle_api.py::SettleForbiddenTests::test_an_owner_without_a_profile_row_is_not_blocked`。
    """
    qs = StaffProfile.objects.filter(settle_forbidden=True)
    if user_ids is not None:
        qs = qs.filter(user_id__in=user_ids)
    return set(qs.values_list("user_id", flat=True))
