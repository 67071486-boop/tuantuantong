"""「怎么显示一个人」的**单一出处**：昵称优先，**空串**回落登录账号（`first_name or get_username()`）。原本多处各写一遍（`apps/business/api/serializers.py` 的 `display_name` / `bound_by_name`、`apps/business/order_stats.py` 的 `userName`、审计日志行那处批量解析），这条语义现统一住 `apps/audit/display.py::actor_display_names`（公开函数，`apps/audit/api/views.py` 与 `apps/business/api/order_views.py` 都 import 它，⛔ 不再有第二份实现）——分成两份就会**各自漂移**（踩过：同一名客服两处显示出两个名字）。⚠️ 口径**只把空串当没有**，不额外判 `strip()` / `is None`：`first_name` 是 `CharField(blank=True)`、`None` 到不了这里；「空白串也算空」是另一套判据，本仓其余显示名口径都没这么判。
⚠️ **别把「单一出处」放大**：本函数覆盖「手上已有用户对象」的点（视图 / 序列化器 / 统计行）；SQL 注解侧（`apps/business/selectors.py` 的 `taker_name_expression()` 等 `Coalesce(NullIf(...))`）是**另一套表达**（表达式必须留在查询里），⛔ 不要为「统一」改坏它们。也**别**与 `apps/core/profile_views.py` / `apps/accounts/payloads.py` 的 `"display_name"` 混为一谈：那两处是账号接口**可编辑的昵称字段本身**（空就是空、**不回落**账号），语义不同。批量版 `display_names_for_usernames(...)` 给「手上只有**登录账号快照**」的读面用（`Order.deleted_by` 那类文本列）：**一次**查询把一页行的账号映射成显示名，⛔ 不逐行查库（N+1）；它与 `actor_display_names()` 是**两把不同的钥匙**（那个入参 `actor_id` 主键、这个入参 `username` 登录账号），两者都只是 `user_display_name()` 的批量包装，口径仍只有一处。
"""
from __future__ import annotations

from django.contrib.auth import get_user_model


def user_display_name(user) -> str:
    """返回 `user` 的显示名：`first_name` 非空用它，否则回落 `get_username()`。
    ⚠️ 参数只要求「有 `first_name` 与 `get_username()` 的对象」（本仓的 `AUTH_USER_MODEL`）；⛔ 不在这里查库：调用方需要预取就自己 `select_related`（列表页的 N+1 由各自用例钉住）。
    """
    return user.first_name or user.get_username()


def display_names_for_usernames(usernames) -> dict[str, str]:
    """`登录账号 → 显示名` 的**一次性**映射（订单日志人名线 spec §3-B1）。入参是登录账号的**可迭代对象**（允许重复，`None` / 空串跳过；空集 ⇒ `{}`、**一次查询都不发**）；无论多少行只发**一次** `filter(username__in=...)`（⛔ 不许逐行查）；`.only("pk", "username", "first_name")`——账号表那侧挂着手机号 / 身份证号 / 收款账号等 PII，本函数**不读**别的列；返回 `{username: user_display_name(user)}`，**查不到**的账号（已硬删 / 改名）**不在返回值里**，由调用方回落原始值（显示用快照列，不参与筛选）。
    ⚠️ 与 `apps/audit/display.py::actor_display_names()` 的分工：那个按**主键**批量解析（审计日志行的 `actor_id`），这个按**登录账号**批量解析（回收站 `deleted_by` / 操作统计 `actor` 这类**文本快照**列）⇒ ⛔ 不要合成一份（键的语义不同）。
    """
    names = {name for name in usernames if name}
    if not names:
        return {}
    return {
        # 口径的唯一出处仍是本模块的 `user_display_name`（昵称优先、空串回落账号）。
        user.get_username(): user_display_name(user)
        for user in get_user_model()
        .objects.filter(username__in=names)
        .only("pk", "username", "first_name")
    }
