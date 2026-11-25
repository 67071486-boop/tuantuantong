"""数据权限的唯一卡点（spec §7）：**默认不过滤**，过滤规则由模块注入。⚠️ 本模块**不内置任何业务规则**。依据 `docs/ERRORS.md` **E-0006**：
可见范围的真实口径是「客服默认可见所有，取决于该账号是否具备『全部游戏』权限」，而需求文档里"客服仅可见自己负责的客户/订单"是**被写反了**的那条（E-0006）。所以 P5 只留扩展点；
具体过滤维度**待业务方确认**后由落地模块传 `scope_filter`。把一条猜出来的规则写进这里，会让它变成所有模块的默认行为——那正是 E-0006 的代价。为什么"唯一卡点"不写成基类/Manager：卡点越多，漏掉一个就等于没有数据权限；调用方显式调用本函数时，`scope_filter` 的有无在代码里是**可见**的（一眼能看出哪个查询带范围、哪个不带），而 Manager 的默认过滤是隐式的。"""


def scope_queryset(user, queryset, scope_filter=None):
    """按 `scope_filter` 收窄 `queryset`；**不传就原样返回**。`scope_filter(queryset, user) -> QuerySet`——过滤维度由调用方决定，本函数只负责两件不变量：
    ① **不传即不过滤**（E-0006：P5 不发明业务规则）；② 租户的管理员**永不**被过滤——管理员判据与 `HasRole` 共用 `is_tenant_admin`（租户平面认「系统管理员」职位，不再看 `is_superuser`），两处各写一遍会漂移。返回的都是 QuerySet（懒执行）：调用方可以继续 `.filter()` / 分页。
    """
    from apps.core.permissions import is_tenant_admin

    if is_tenant_admin(user) or scope_filter is None:
        return queryset
    return scope_filter(queryset, user)