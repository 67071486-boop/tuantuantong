"""打手侧对外端点：**我的单**（列表 + 查单）——契约以 spec `docs/specs/2026-10-08-taker-open-api-design.md` §5.2-5.4 为准。⛔⭐ 范围判据 = 「他是这张单的当前接手人」（不是游戏范围）：
两条都走 `api/order_scope.py`（唯一出处 `claimed_order_qs`/`claimed_perf_of`），⛔ 不 import 大厅取单口那套 `scope_queryset`/`game_scope`（会让"范围内、别人接的单"被查到，spec §5.3-2 红队改准；`_live_order_in_scope` 那份是大厅口径）。
行 = §5.1 ＋ 7 键恰好 17，判据在 `tests/test_takerapi_my_orders_api.py`（键集字面量写死，⛔ 不从本模块派生，E-0024）；分页复用 `openapi/api/order_extra_views.py::_paginated`（非法入参 ⇒ `1000`；跨 app import 其私有名有先例：大厅那条线同款）。"""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.models import Order
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.order_extra_views import _paginated
from apps.openapi.serializers import money
from apps.takerapi.api.hall_views import hall_row_payload
from apps.takerapi.api.order_scope import (
    CLAIMED_AT,
    TAKER_ORDER_NOT_FOUND_MSG,
    claimed_order_with_price_qs,
    claimed_perf_of,
)
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.throttling import TakerAppFallbackThrottle


class MyOrdersQuerySerializer(serializers.Serializer):
    """`GET /api/taker/v1/my/orders/` 的查询参数（唯一的校验口）。`page`/`page_size` 不在这里校验（`_paginated` 先经 `_page_window`，默认 20/上限 100 只有一处实现）。
    `updated_since` 可选 ISO8601；非法值 ⇒ `1000`（⛔ 不是 500，⛔ 不是"静默不过滤"——静默会让对端以为拿到增量、实际全量，可能据此删自己那边的单）。
    ⚠️ `parse_datetime` 格式不对回 `None`、格式对值不合法（`…T25:00:00`）抛 `ValueError` ⇒ 两种都要接住；裸日期 `20261008` 在 3.13 上也能解析 ⇒ 显式要求 `T`/空格分隔的"时刻"（契约是时刻，不是哪一天）。"""

    updated_since = serializers.CharField(
        required=False, allow_blank=False, error_messages={"blank": "updated_since 不能为空"}
    )
    #: ⭐ ：按**三方/来源单号**精确定位自己的单（只在自己的单集合里筛）。
    source_order_no = serializers.CharField(required=False, allow_blank=False, trim_whitespace=True)

    def validate_updated_since(self, value):
        # ⚠️ **函数内 import**（与 `peer_inbound` 的那一处同形）：本模块的模块级 import
        # 清单只保留"读代码的人一眼要看的东西"，解析器是这一格自己的实现细节。
        import re

        from django.utils.dateparse import parse_datetime

        # ⚠️ 裸日期（`20300102` / `2030-01-02`）**刻意不收**：它们是"哪一天"、不是"哪一刻"，
        # 而本参数比的是时刻 ⇒ 收下来就要**猜**一个时分秒（猜出来的那个是**我们**的解读，
        # 不是调用方的意思）。判据 = 必须有 `T` 或空格把日期与时间分开。
        if not re.search(r"[T ]\d", value):
            raise serializers.ValidationError("updated_since 必须是 ISO8601 时刻（含时间部分）")
        try:
            parsed = parse_datetime(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            # ⚠️ 同时拒**无时区**的值：本仓 `USE_TZ=True`，无时区的值会被 Django 当成
            # 本机时区（`Asia/Shanghai`）⇒ "同一个字符串在两台机器上解析成两个时刻"。
            # ⛔ 不把 `value` 回显进文案（请求来源的值不回抄，`E-0010` 同族）。
            raise serializers.ValidationError("updated_since 必须是带时区的 ISO8601 时刻")
        return parsed


def claimed_order_row(order) -> dict:
    """一行「我的单」→ 对外载荷（唯一装配出口；§5.2 = §5.1 ＋ 7 键，恰好 17）。§5.1 的 10 键不在这里重写：走 `hall_views.hall_row_payload(order)`——
    同一订单对外形状只有一处出处（两处各写一份=大厅将来加键时本行静默缺键、没有网会红）。入参必须是带两个注解的 `Order` 实例（都由取数口负责）：
    `claimed_at`（⭐ 在绩效行上，`Order` 没有这一列，由 `claimed_order_qs` 注解或查单端点搬到同名类属性）与生效接单价（`selectors.EFFECTIVE_TAKER_PRICE`，`hall_row_payload` 自己要）。"""
    payload = hall_row_payload(order)
    payload.update(
        {
            # ── §5.2 加的那 7 个键（顺序与 spec 那一行一致，便于逐字对读）──────
            "status": order.status,
            "status_text": order.get_status_display(),
            # ⭐ 那一条 MAIN 绩效行的 `claimed_at`（注解带上来的）。⚠️ D-0302：两个时刻键走 `local_iso`（项目时区 +08:00）；
            # ⛔ 不改 `apps/openapi/serializers.py::_iso`——那是上家侧机器契约的出口；本族是打手侧（TAKER-API 文档 §5.2 示例本就是 +08:00）。
            "claimed_at": local_iso(order.claimed_at),
            "updated_at": local_iso(order.updated_at),
            # ⭐ P2：客户联系方式**抢到之后才给**（本行是"我的单"，所以给）。
            "customer_phone": order.customer_phone,
            "customer_ww": order.customer_ww,
            # ⚠️ 金额出口复用仓内唯一那个（`None` ⇒ `null`；`0.00` 与负数都是合法金额，E-0021）。
            "total_price": money(order.total_price),
        }
    )
    return payload


class MyOrdersAssemblyMixin:
    """两条端点的共同装配（认证 / 无权限类 / 兜底闸）。授权面逐字相同 ⇒ 装配只写一处
    （`AGENTS.md` §5-E：去掉它没有任何用例会红的重复装配不留）。"""

    authentication_classes = [TakerAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（不写它会吃全局默认的 `IsAuthenticated`
    #: ⇒ 合法令牌反被判 401；与 T5 对外面同款）。
    permission_classes = []
    #: 兜底闸（DRF 的 `initial()` 顺序 = 认证 → 权限 → 限流 ⇒ 401 先于 429，且不进桶）。
    throttle_classes = [TakerAppFallbackThrottle]


class MyOrdersListView(MyOrdersAssemblyMixin, APIView):
    """`GET /api/taker/v1/my/orders/` —— 他当前接手的单（分页）。"""

    def get(self, request):
        taker = request.auth.taker
        # ⚠️ 入参先过序列化器（`?updated_since=abc` ⇒ `1000` + **一条查询都不发**）。
        params = MyOrdersQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        queryset = claimed_order_with_price_qs(taker)
        source_order_no = params.validated_data.get("source_order_no")
        if source_order_no:
            queryset = queryset.filter(source_order_no=source_order_no)
        updated_since = params.validated_data.get("updated_since")
        if updated_since is not None:
            # ⚠️ `is not None`（本仓口径 E-0021：边界一律不用真值，跟写以免后来者照抄出真会踩的版本）；
            # ⚠️ 过滤的是订单的 `updated_at`（spec §5 端点表第 3 行），底集主体是 `Performance` ⇒ 跨关系写成 `order__updated_at`。
            queryset = queryset.filter(updated_at__gte=updated_since)
        # ⚠️ `select_related("game")` 覆盖行载荷读的跨表字段（`hall_row_payload` 读 `order.game.name`）；⛔ 不预取 = 每行一条查询。
        # ⚠️ 只预取 `game`：本查询的主体就是 `Order` ⇒ 写 `"order"` 会 FieldError。
        queryset = queryset.select_related("game")
        return api_response(_paginated(queryset, request, claimed_order_row))


class MyOrderDetailView(MyOrdersAssemblyMixin, APIView):
    """`GET /api/taker/v1/orders/<order_no>/` —— 一张单（§5.2 的**单数版**）。"""

    def get(self, request, order_no):
        # ⚠️ 取的是**那一条 MAIN 绩效行**（`claimed_perf_of`）：本端点要装配的行载荷里
        # `claimed_at` 在**绩效行**上（`Order` 上没有这一列）；而两个注解在 `Order` 上
        # ⇒ 判据与装配**各取一次**（⛔ 不是"一次查询就装配得完"——T6① 改准）。
        perf = claimed_perf_of(request.auth.taker, order_no)
        if perf is None:
            # 不存在 / 不是他接的 / 已软删：**同一个出口**（spec §5.3-2 的"同形"）。
            return api_response(
                data=None, msg=TAKER_ORDER_NOT_FOUND_MSG, code=ErrorCode.BUSINESS
            )
        # ⭐ 行载荷走同一个装配出口（`claimed_order_row`）⇒ 单数版与列表版逐键同形；`claimed_at`（单笔上是绩效行真列）用同一个类属性名搬过去、生效接单价走同一个 `claimed_order_with_price_qs`（⛔ 绝不写第二个装配函数——两个出口必然漂移）。
        # ⭐⭐ 竞态闸：判据与装配是两次查询，中间有一瞬竞态（软删/换接手人）⇒ `.get()` 会抛 `Order.DoesNotExist`，不接住就是 500；正确答复是 `1002`（与"查不到/不是他的"同码同文案，spec §5.3-2 同形），判据 `tests.test_takerapi_my_orders_api.py::DetailAssemblyRaceTests`。
        # ⚠️ 不改"一次查询"：那会把查单授权面换成另一份实现，而改状态/进度图仍走 `claimed_perf_of` ⇒ 第二份判据（`AGENTS.md` §5-E）；本做法不消除那一瞬窗口，只把"取不到"译成正确业务答复。
        try:
            order = claimed_order_with_price_qs(request.auth.taker).get(pk=perf.order_id)
        except Order.DoesNotExist:
            return api_response(
                data=None, msg=TAKER_ORDER_NOT_FOUND_MSG, code=ErrorCode.BUSINESS
            )
        setattr(order, CLAIMED_AT, perf.claimed_at)
        return api_response(claimed_order_row(order))