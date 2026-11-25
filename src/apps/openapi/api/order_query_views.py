"""对外查单端点 + 集合路径录单/增量拉单 + 详情路径改单（v2 §6.1/§7.1/§7.2；四条契约两条路径，全走 Bearer 令牌）。
共同装配（`ApiOrderView`）：`ApiAppBearerAuthentication`（⛔ 不用 `IsAuthenticated`——认证成功返回 `(AnonymousUser(), app)`，装了会把合法请求判 401）+ ⛔ 显式空 `permission_classes`（§13.10 一个应用=本租户全部业务数据；不写属性会吃全局 `IsAuthenticated`）+ 兜底闸装类属性（`initial()` 顺序保证 401 先于 429、被挡请求不进桶）。锁定应用（`wecom_group_qr`）的授权面在**认证层**白名单（`apps/openapi/permissions.py`，D3-D5），⛔ 别把"这里没权限类"读成"那道闸不存在"——它在认证层、对白名单外的端点回 403。
取数=本租户**全部**订单（v1 面下线时删了节点门与游戏范围收窄——可读面从可收窄变整租户，泄漏 `app_secret` 的爆炸半径=该租户全部订单含成本提成，代价登记在 spec §13.1/§35；`game_scope` 原语本身没删，判据=`GameScopePrimitiveTests`）；「查不到」唯一出口=`NotFound` ⇒ `1002`；只读 ⛔ 不写业务表、⛔ 不直接读 `request.user`（Bearer 下是匿名，只许调已做匿名防护的共享函数）。"""
from django.db.models import Prefetch
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.business.models import Order, Performance
from apps.core.envelope import api_response
from apps.openapi.api.order_create_views import OrderCreateMixin
# ⚠️ **分页口径只有一处实现**（默认 20 / 上限 100 / 畸形 `1000`）：`page_window`/`_paginated` 住
# `order_extra_views`，本模块 `updated_since` 分支**调它** ⛔ 不自己拼一份；这条 import 不构成循环
# （`order_extra_views` 不 import 本模块；从本模块出发的那条 import 在 `apps/openapi/urls.py`、方向相反）。
from apps.openapi.api.order_extra_views import page_window
from apps.openapi.api.order_write_views import OrderUpdateMixin
from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.serializers import (
    SOURCE_ORDER_NO_LIMIT,
    OrderQuerySerializer,
    order_payload,
)
from apps.openapi.throttling import ApiAppFallbackThrottle


class ApiOrderView(APIView):
    """两个查单端点的共同装配（认证/权限/兜底闸/取数）。
    ⚠️ 用普通 `APIView` ⛔ 不是 ViewSet：路由是显式 `path()`、没有集合语义；ViewSet 还会带出 router 的 `list/retrieve/delete` 一整套动作（D5：⛔ 没有删除端点）。"""

    authentication_classes = [ApiAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（见模块 docstring 第 2 条：不写这个属性会吃全局默认的
    #: `IsAuthenticated`，把合法的令牌请求判成 401）。v1 面下线后四条方法的授权面**同一个**。
    permission_classes = []
    #: ⭐ 极宽兜底闸（默认 6000 次/分钟/应用，⛔ 不是配额）。装在这里（而非视图体）的理由：
    #: DRF `initial()` 是「认证→权限→限流」⇒ 401/403 一律先于 429、被挡请求不进桶（全文见 `apps.openapi.throttling` docstring）。
    throttle_classes = [ApiAppFallbackThrottle]

    @staticmethod
    def _orders_queryset():
        """本租户**全部**订单的基础集（按 `id` 升序）。`select_related`/`prefetch_related` 是**必须**的（不是优化）：载荷要读游戏/项目/区服/分类名与绩效行（含 D-0320 起的接手人显示名）⇒ 不预取每张单多 5 条查询（一页 ~100 条）。
        `Meta.ordering` 在这里**显式改成 `id` 升序**（§7.2 逐字冻结 ⛔ 不靠模型默认排序）。
        ⛔ 今天**没有**"收窄"这一步（v1 面下线时删了游戏范围过滤，§13.1）：应用看到的就是本租户全部订单；镜像卡派生的**隐藏单**一并 exclude（不是本租户的真实生意，取不到=「不存在」同形 `1002`）。"""
        return (
            Order.objects.select_related("game", "project", "server", "category")
            .prefetch_related(
                # ⭐ D-0320：顶层 `taker_name` 要读绩效行的 `user`（显示名口径
                # `user_display_name` **不查库**）⇒ 把它**合进既有那条预取**，
                # 仍是一次查询（JOIN 进同一条预取 SQL），⛔ 不是每行再查一次（N+1）。
                Prefetch(
                    "performances",
                    queryset=Performance.objects.select_related("user"),
                )
            )
            # ⭐ 评审 Important 4：镜像卡派生的**隐藏单**不是本租户的真实生意
            # （没有客户、没有绩效、金额也不是收入）⇒ 对外查单（详情 / 来源号 /
            # 增量流）一并排除；按单号取不到 ⇒ 与「不存在」同形的 `1002`。
            .exclude(hall_card__isnull=False)
            .exclude(upstream_hall_card__isnull=False)
            .order_by("id")
        )


class OrderDetailView(OrderUpdateMixin, ApiOrderView):
    """`v1/orders/<order_no>/` 单张订单资源：`GET` 查一张 + `PATCH` 改单（§6.1）。两条方法同住一个类=框架事实：同一路径第二条路由永远走不到（理由全文在 `OrderUpdateMixin` docstring）。
    授权面两条方法**相同**（都不装权限类）：v1 节点门已删（§13.1）⇒ `get_permissions` 覆写没有存在理由、已删（AGENTS §5-E）。
    路径参数用既有 `order_no` 转换器（挡 NUL 字节——`%00` 进 `filter` 会撞 PostgreSQL `DataError` 变 HTML 500）⇒ 畸形路径段不匹配路由 ⇒ 404（⛔ 不是 500、也不进本视图）。"""

    def get(self, request, order_no):
        # ⚠️ 参数校验**排在取数之前**：它是纯入参问题（不查库、不涉授权范围）而「畸形路径段」
        # 由路由的转换器挡掉 ⇒ 这里只剩"查不到"一个出口。
        order = self._orders_queryset().filter(order_no=order_no).first()
        if order is None:
            # 不存在 / 不在本租户：**同一个出口**（§7.1 的「同形」）。
            # `NotFound` 经本项目的异常处理器变成 `code=1002` + HTTP 200
            # （本仓 NotFound 口径：业务结果不用 HTTP 状态码表达）。
            raise NotFound()
        return api_response(order_payload(order))


class OrderSourceQueryView(OrderCreateMixin, ApiOrderView):
    """`v1/orders/` 集合资源：`GET` 查单（两个分支）+ `POST` 录单（§6.1）。两条方法同住一个类且授权面相同（节点门已删；`authentication_classes` 保留=「这条路径提前切过认证」的显式声明）。
    ⭐ `updated_since` 是**加的一支** ⛔ 不是改掉旧的：住在同一个 `get` 里一次分派——「再注册一条只收 `updated_since` 的路由」做不到（同路径第二条 `path()` 永远走不到；先例 `status/`、`images/`）。"""

    authentication_classes = [ApiAppBearerAuthentication]

    def get(self, request):
        """集合路径的 `GET`：**单号档**（§7.2 逐字不变）或**时间档**（§7.1）。分派判据用 `is not None` ⛔ 真值：
        `updated_since` 经序列化器后要么 `None`（没传 ⇒ 单号档）、要么带时区的 datetime；`?updated_since=`（空串）在字段校验那关就回 `1000`（⛔ 不会走到这里被当"没传"静默换档——那正是本条要堵的静默形态）。
        单号档的**空串**仍是真的查询值（`source_order_no=""`）⇒ 仍走单号档（既有行为，用例钉着）。"""
        serializer = OrderQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        updated_since = serializer.validated_data.get("updated_since")
        source_order_no = serializer.validated_data.get("source_order_no")

        if updated_since is not None:
            return api_response(self._feed_payload(updated_since, request))

        # ⚠️ 判据用**序列化器给的值**（`is not None` 家族）：传了空串就是按空串查
        # （命中 0 条 ⇒ 空列表 + `total=0`），⛔ 绝不把它当成"没传"而回全部订单。
        matched = self._orders_queryset().filter(source_order_no=source_order_no)
        return api_response(
            {
                "items": [
                    order_payload(order) for order in matched[:SOURCE_ORDER_NO_LIMIT]
                ],
                "total": matched.count(),
            }
        )

    def _feed_payload(self, updated_since, request) -> dict:
        """时间档的 `data`：**恰好四键** `{"count", "results", "page", "page_size"}`（§7.1）。取数三条构成窗口：① `updated_at >= updated_since` 含端点（D12）；② 只含本租户未软删的单——复用 `_orders_queryset()`（⛔ 本方法不自己写 `Order.objects`：多一个读点+第二份"对外怎么取单"判据）；③ 排序 `updated_at` 升序 → `id` 升序（`id` 那档必须在：同刻并列时只按 `updated_at` 数据库任选顺序 ⇒ 对接方翻页漏单或重复，症状只在同刻并列时现、平时全绿）。
        分页走 `page_window`（与对外三个列表端点同一套；⛔ 不再写一份 `min(…, 100)`）；每行=`order_payload(order)`、与详情端点**同一个函数出口**（D13）⇒「同一张单两种取法拿到的东西一样」由同一个装配保证 ⛔ 不靠两份字段表对齐。"""
        page, page_size = page_window(request)
        matched = self._orders_queryset().filter(
            updated_at__gte=updated_since
        ).order_by("updated_at", "id")
        offset = (page - 1) * page_size
        rows = list(matched[offset : offset + page_size])
        return {
            "count": matched.count(),
            "results": [order_payload(order) for order in rows],
            "page": page,
            "page_size": page_size,
        }
