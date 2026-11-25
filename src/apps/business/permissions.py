"""改接单价与审核绩效两条 Django codename，已冻结为 legacy，不要新增用法。生产路径改走节点 code（`order.submit_payout_price` / `order.change_status_completed`）。
这里仍走 `user.has_perm`：只置超管开关的账号在这条旧路上恒放行；持「系统管理员」职位不会自动放行，除非那个组真被授予了这两条 codename（本仓没有）。判据复用 `apps.core.permissions.HasPermission`。DRF 会无参实例化 `permission_classes`，
把实例塞进去会炸 `TypeError`；带参权限用工厂 `has_biz_permission`，它返回的是真类。"""
from apps.core.permissions import HasPermission

#: 改接单价（订单默认价或绩效覆盖价）——spec §4.3 / §6.3。
OVERRIDE_TAKER_PRICE = "business.override_taker_price"
#: 审核绩效（通过 / 驳回）——spec §4.3 / §8.1。
REVIEW_PERFORMANCE = "business.review_performance"

#: 本模块承认的 codename 全集。视图里**不要**再散落字符串字面量。
BIZ_PERMISSIONS = (OVERRIDE_TAKER_PRICE, REVIEW_PERFORMANCE)


class HasBizPermission(HasPermission):
    """业务细粒度权限：任一 codename 命中即放行。
    判定委托给 `HasPermission`。构造得到的是实例，放进 `permission_classes` 要用工厂。"""


def has_biz_permission(codename):
    """工厂：把业务 codename 绑成可放进 `permission_classes` 的类。
    ⚠️ **未登记的 codename 直接抛 `ValueError`**（而不是安静地生成一个永不通过的
    权限类）：拼错一个字母的后果会是「所有人都 403」，而那种失败看起来像权限配置问题、排查成本高。这里让它**在声明处**就响亮地炸。"""
    if codename not in BIZ_PERMISSIONS:
        raise ValueError(
            f"未登记的业务权限 codename：{codename!r}；已登记的是 {BIZ_PERMISSIONS}"
        )
    return type(
        "HasBizPermission",
        (HasBizPermission,),
        {"_configured_codenames": (codename,), "__module__": __name__},
    )
