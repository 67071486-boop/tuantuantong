"""订单导出端点（`GET /api/business/orders/export`，spec §2 / §5 / §6）。⭐ **本端点与绩效导出同形：成功时**有意**打破 `api_response()` 的信封约定**——成功回 xlsx 字节、失败（参数畸形 / 超上限）回 **HTTP 200 + 统一信封**（认证 / 权限失败回 401 / 403 + 信封）⇒ **同一个 URL 有两种形状**，前端必须分支（`api/orders.ts::downloadOrderExport`）。
⚠️ **取数必须与列表同源**（spec §2-7）：同一个 `order_search_params` + `search_orders` + `scope_queryset` + `scope_by_creator` + `TAKER_NAME` 注解，⛔ 少任何一项都是「导出的是另一批数据」；**刻意不叠 `order.list` 数据范围收口**（spec §9-10）：`order.export` 本身就是完整授权。
`record(...)` **只在成功且已建出文件之后**调用；超上限 / 参数畸形提前回信封、不记。
"""
from urllib.parse import quote

from django.db.models import OuterRef, Subquery
from django.http import HttpResponse
from django.utils import timezone

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.display import display_names_for_usernames
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.order_views import order_search_params
from apps.business.api.performance_write_views import EXPORT_MAX_ROWS
from apps.business.api.serializers import OrderSearchSerializer
from apps.business.game_scope import game_scope
from apps.business.models import EmpType, OrderRefund, Performance
from apps.business.order_export import (
    CLAIMED_AT,
    REFUNDED_AT,
    build_workbook,
    row_values,
)
from apps.business.order_flags import (
    can_view_offer_price,
    can_view_taker,
    scope_by_creator,
)
from apps.business.selectors import TAKER_NAME, search_orders, taker_name_expression
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node

#: 响应的 `Content-Type`（成功时是文件流，不是信封）。
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class OrderExportView(APIView):
    """导出订单（GET）。权限闸 = 既有 RBAC 节点 `order.export`（对标 31「导出订单」）。"""

    def get_permissions(self):
        """闸门 = `has_node("order.export")()`（写在**方法体**里，与先例同形）。"""
        return [has_node("order.export")()]

    def permission_denied(self, request, message=None, code=None):
        """403 必须转调 `super()`（它先判认证并抛 401）；403 信封由异常处理器统一构造。"""
        super().permission_denied(request, message="你没有导出订单的权限", code=code)

    def get(self, request):
        data = order_search_params(request, OrderSearchSerializer)
        payload, _ = render_order_export(request.user, data)
        record(
            log_type=ACTION_LOG_TYPES["order.export"],
            action="order.export",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
        )
        response = HttpResponse(payload, content_type=XLSX_CONTENT_TYPE)
        response["Content-Disposition"] = order_export_disposition(timezone.localtime())
        return response


def order_export_disposition(now):
    chinese_name = f"订单导出{now:%m%d%H%M%S}.xlsx"
    return (
        f'attachment; filename="orders-{now:%Y%m%d}.xlsx"; '
        f"filename*=UTF-8''{quote(chinese_name)}"
    )


def render_order_export(user, data):
    """Build the workbook and its exported order IDs."""
    queryset = search_orders(
        status=data.get("status"),
        payment_status=data.get("payment_status"),
        customer_kind=data.get("customer_kind"),
        game_id=data.get("game"),
        server_id=data.get("server"),
        project_text=data.get("project_text"),
        keyword=data.get("keyword"),
        is_urgent=data.get("is_urgent"),
        tag=data.get("tag"),
        date_from=data.get("date_from"),
        date_to=data.get("date_to"),
        order_source=data.get("order_source"),
        # ⭐（公司店铺分组）：**必须与列表同批转发**——漏这一行不会报错，
        #    只是「导出的比页面看到的多」（静默丢条件，计划 §4 #3 / `test_order_export_api.py`）。
        source_group=data.get("source_group"),
        order_type=data.get("order_type"),
        sales_user=data.get("sales_user"),
        kefu_type=data.get("kefu_type"),
        is_leak=data.get("is_leak"),
        show_price_type=data.get("show_price_type"),
        price_min=data.get("price_min"),
        price_max=data.get("price_max"),
        # ⭐ 价格口径线：`3 接发组合` 档的**发单价**那一对区间。⚠️ 本视图与列表**共用** `order_search_params` + `search_orders`（模块 docstring 的「取数必须与列表同源」）⇒ 序列化器收了新参数就必须在这里一起转发：漏转发的表现不是报错，而是**导出的比页面看到的多**（静默丢条件，E-0021 同族）。
        hall_price_min=data.get("hall_price_min"),
        hall_price_max=data.get("hall_price_max"),
        order_nos=data.get("order_nos"),
        batch_lookup_nos=data.get("batch_lookup_nos"),
        taker_user=data.get("taker_user"),
        hall_publisher_user=data.get("hall_publisher_user"),
        # ⭐ 拆单：按拆单组取**该组全部成员**（同一条转发纪律，见上面那段）。⚠️ **本视图刻意不折叠**（列表那条折叠挂在 `OrderViewSet.filter_queryset` 上、只对 `list` 生效）：spec §6 已登记的代价就是「列表只数主行 ⇒ 用导出 / 统计看到的行数更大」⇒ 导出照旧出**每一张真实订单行**。
        split_id=data.get("split_id"),
    )
    # 列表载荷里没有的两个键（spec §3 第 19 / 21 行）⇒ **相关子查询注解**一次查完。⛔ 不逐行查库、⛔ 不 prefetch 再在 Python 里挑（判据见 tests 的 B17）。
    queryset = queryset.annotate(**{TAKER_NAME: taker_name_expression()})
    queryset = queryset.annotate(**{
        CLAIMED_AT: Subquery(
            Performance.objects.filter(
                order=OuterRef("pk"), emp_type=EmpType.MAIN
            ).values("claimed_at")[:1]
        ),
        # ⛔ **不另写排序**：`OrderRefund.Meta.ordering = ["-created_at", "-id"]`
        # 就是并列时的 tie-break（spec §3 第 21 行的裁定）。
        REFUNDED_AT: Subquery(
            OrderRefund.objects.filter(order=OuterRef("pk"))
            .exclude(kind=OrderRefund.Kind.REVERSAL)
            .values("created_at")[:1]
        ),
    })
    queryset = scope_queryset(user, queryset, scope_filter=game_scope("game_id"))
    # ⚠️ `order.restrict_to_self_created` **必须显式施加**（spec §2-7 第 4 项）：
    # 列表侧它挂在 `filter_queryset()` 上、只对 `list` 生效 ⇒ 导出这条读路径拿不到它，
    # 不施加就是「导出比页面多」（这里最容易漏的一处）。
    queryset = scope_by_creator(user, queryset)

    total = queryset.count()
    if total > EXPORT_MAX_ROWS:
        raise ValidationError(f"本次导出 {total} 行，超过上限 {EXPORT_MAX_ROWS} 行，请先收窄筛选条件")

    # ⚠️ 脱敏 / 接手人两道闸按**当前请求的 user** 算一次（不是逐行算）。
    can_see_offer_price = can_view_offer_price(user)
    can_see_taker = can_view_taker(user)
    # 「录单人」列（2026-11-05 业务方：导出也不出登录账号）：`created_by` 是账号快照
    # （存储值不动——下游按账号消费），导出时批量映射成显示名。**一次**查询管全表
    # （`display_names_for_usernames` 内部 `username__in`；⛔ 不逐行查库）。
    orders = list(queryset)
    created_by_names = display_names_for_usernames(o.created_by for o in orders)
    rows = [
        row_values(
            order,
            can_view_offer_price=can_see_offer_price,
            can_view_taker=can_see_taker,
            created_by_names=created_by_names,
        )
        for order in orders
    ]
    return build_workbook(rows), [order.pk for order in orders]
