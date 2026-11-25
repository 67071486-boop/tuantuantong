r"""客户（会员）**读端点**（2026-11-10 客户线；第二批/第三批增强同日；第五/六批 2026-11-11）。

| 路径 | 方法 | 入参 | 出参 |
|---|---|---|---|
| `/api/business/customers/members/` | GET | `source`（OrderSource id）/ `recorder`（`Order.created_by` 精确值）/ `phone`（customer_phone 包含匹配）/ `order_by`（`order_count` 默认 \| `total_amount`）/ `page` / `page_size` | `{total, filtered_total, page, page_size, items}` |
| `/api/business/customers/member-filters/` | GET | 无 | `{sources, recorders}`（recorders = `[{value: 登录账号, label: 显示名}]`） |
| `/api/business/customers/member-orders/` | GET | `eid`（必填）/ `page` / `page_size` | `{total, page, page_size, items, customer_info}`（第六批：该客户**系统订单 + 有效来源订单**的单一合并列表，时间倒序、统一行形状，不受外层筛选影响；第七批 +`customer_info` 抽屉头部信息卡） |
| `/api/business/customers/analysis/` | GET | `range`（`30d` 缺省 \| `90d` \| `12m`） | `{range, kpi, source_dist, source_customer_dist, consume_tiers, growth}`（第三批，一次性聚合；第七批 +`source_customer_dist` 各店铺有订单客户数；口径见 `customers.py` 分析段） |

口径（业务方 2026-11-10 拍板；第五批扩容；第六批 2026-11-11 统计合并）：`total` 恒 =
`Customer.objects.count()`（客户表全量、不随筛选变，分页翻页用的行集总数另放
`filtered_total`）；**行 = 全部建档客户**（没连过系统单的收割客户也在列），行统计
（订单数/最近下单/累计消费）= **系统订单 + 有效来源订单合并**（有效口径 = 排除已退款/
已关闭 + 已在库系统单同号者，见 `customers.valid_source_orders`；⚠️ 同单两面只算一张），
筛选仍只作用于系统订单（命中 ⇒ 行可见；来源单-only 的客户不被任何筛选命中，清空筛选
即见）；行的展示列取**筛选范围内最近一单**的值（零单客户 = 空串/0.00）。取数与聚合的
唯一实现在 `apps/business/customers.py`。

录单人显示名（第二批）：`created_by` 是用户名快照（部分账号用户名=手机号）；显示一律经
`apps/accounts/display.py::display_names_for_usernames` 批量映射（与导出线同一出处），
映射不到（账号已删）回落原用户名。`recorder` 键仍回原值（筛选入参语义不变）。

⚠️ 授权 = RBAC 三节点**任一**（`has_node` any 语义；管理员短路）：`customer.member_list`
（2026-11-11 翻真，树名「客户管理」）、`customer.member_analysis_report`（同批翻真，
**只**作 analysis 端点的门）与第五批自有限制节点 `customer.member_list_self_only`。
只持限制节点的用户 = **限制档**：行集 / 抽屉 / `total` / 录单人下拉全部由服务端收口到
「录单人=自己」（`_restricted_creator`），来源单-only 的客户对其不可见，行统计保持
**系统单口径**（来源单无录单人无法归属）；analysis 端点过守卫但聚合**不**按录单人
收口（拍板口径）。
⚠️ 请求来源的值一律过序列化器（E-0010）；读端点**不写操作日志**（既有读面口径）。
"""
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count
from rest_framework import serializers
from rest_framework.views import APIView

from apps.accounts.display import display_names_for_usernames
from apps.business import customers as customer_domain
from apps.business.api.base import EnvelopePagination
from apps.business.api.serializers import order_source_display_name
from apps.business.models import Customer, Order, OrderSource
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node, has_node_now

#: 客户管理节点（2026-11-11 翻真，树名「客户管理」）：四个端点共用的主准入节点。
CUSTOMER_MEMBER_NODE = "customer.member_list"
#: 客户分析报表节点（同批翻真）：**只**作 analysis 端点的门——「只勾分析报表」的职位
# 页面可进、接口必须放行（成员/筛选/订单抽屉仍不认它）。
CUSTOMER_ANALYSIS_NODE = "customer.member_analysis_report"
#: 限制节点（第五批 2026-11-11 自有点，available=True）：只持它 ⇒ 行集收口到
# 「录单人=自己的系统订单所连的客户」；任一节点即可过守卫（`has_node` 是 any 语义）。
CUSTOMER_SELF_ONLY_NODE = "customer.member_list_self_only"

#: `total_amount` 的出参形状 = 两位小数字符串（与 `MONEY_FIELDS` 各序列化出口同形状）；
#: **前端不算钱**——字符串原样展示，正负号照实（D-0015：负数合法）。
TOTAL_AMOUNT_DECIMALS = 2

#: 闲管家来源单状态码 → 中文（权威出处 `docs/XGJ-INTEGRATION.md` §5；⚠️ 这套码是
#: 闲管家自己的，与系统单 `OrderStatus` 的码不可比）。未知码兜底「未知」。
XGJ_SOURCE_STATUS_LABELS = {
    11: "待付款",
    12: "待发货",
    21: "已发货",
    22: "已完成",
    23: "已退款",
    24: "已关闭",
}
XGJ_SOURCE_STATUS_UNKNOWN = "未知"

#: 「客户不存在」的统一文案（member-orders 的 `eid` 查无此行时回 `1000` + 这句）。
CUSTOMER_NOT_FOUND_MSG = "客户不存在"


def mask_eid(eid: str) -> str:
    """`Customer.eid` 出参打掩码（身份键不出完整值）：留头 4 尾 2，短串只留头 2。"""
    if len(eid) <= 8:
        return eid[:2] + "***"
    return f"{eid[:4]}***{eid[-2:]}"


class CustomerMembersQuerySerializer(serializers.Serializer):
    """会员列表的入参判据（**唯一**校验口；畸形值在这里回 `1000`，不进查询）。

    `source` / `recorder` / `phone` 缺席（不设 default ⇒ 键不进 validated_data）= 不筛；
    `recorder` 是 `Order.created_by` 的**精确值**（下拉候选来自 member-filters，不是模糊搜）；
    `phone` 对 `customer_phone` 做 icase contains（第二批）；
    `order_by` 两档都**降序**（排序语义在 `customers.members_queryset`，choice 集合与它同源）。
    """

    source = serializers.IntegerField(required=False, min_value=1)
    recorder = serializers.CharField(required=False, max_length=150, trim_whitespace=True)
    phone = serializers.CharField(required=False, max_length=64, trim_whitespace=True)
    order_by = serializers.ChoiceField(
        choices=list(customer_domain.MEMBER_ORDER_FIELDS), required=False
    )


class CustomerMemberOrdersQuerySerializer(serializers.Serializer):
    """客户订单抽屉的入参判据（`eid` = `Customer.eid`，身份键）。"""

    eid = serializers.CharField(required=True, max_length=64, trim_whitespace=True)


class CustomerMembersPagination(EnvelopePagination):
    """分页默认 50（计划 §2.3），每页上限沿用全局 300。"""

    page_size = 50
    page_size_query_param = "page_size"


def recorder_display_map(recorders) -> dict[str, str]:
    """登录账号 → 显示名的批量映射（`accounts/display.py` 同一出处；查不到回落原账号）。
    ⚠️ 入参先物化：下面要迭代它两次（映射一次、回落一次），生成器会被第一次消费光。"""
    names = list(recorders)
    mapped = display_names_for_usernames(names)
    return {name: mapped.get(name, name) for name in names if name}


def _restricted_creator(request) -> str | None:
    """限制档判据（服务端收口，客户端不可放大）：持 `member_list`（含管理员短路）⇒
    全量档，回 `None`；只持 `member_list_self_only` ⇒ 回自己的登录账号（行集 / 抽屉 /
    录单人下拉全部按它收窄）。调用前提 = 端点守卫已过（两节点任一）。"""
    if has_node_now(request.user, CUSTOMER_MEMBER_NODE):
        return None
    return request.user.get_username()


class CustomerMembersView(APIView):
    """`GET /api/business/customers/members/` —— 会员列表（聚合 + 筛选 + 排序 + 分页）。"""

    def get_permissions(self):
        """闸门 = 两节点任一（`has_node` any 语义；限制档的行集收口在 `get` 里强制）。"""
        return [has_node(CUSTOMER_MEMBER_NODE, CUSTOMER_SELF_ONLY_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看客户（会员）列表的权限", code=code
        )

    def get(self, request):
        serializer = CustomerMembersQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        scope = serializer.validated_data
        order_source = scope.get("source")
        recorder = scope.get("recorder")
        # 限制档（只持 self_only）⇒ 行集强制收口到「录单人=自己」，`total` = 可见行数
        # （不再是全量；计划 §2.4）。全量档 creator=None ⇒ 行集/total 与改动前逐字一致。
        creator = _restricted_creator(request)

        base_orders = Order.objects.filter(
            customer_domain.member_scope_q(
                order_source=order_source,
                recorder=recorder,
                phone=scope.get("phone"),
                self_only_creator=creator,
            )
        )
        paginator = CustomerMembersPagination()
        # 有任何筛选键（或限制档）⇒ 行可见 = 「筛选范围内 ≥1 单」
        # （来源单-only / 裸档客不被筛选命中；限制档连全量行都只留自己录过的）。
        has_filter = any(key in scope for key in ("source", "recorder", "phone"))
        aggregate_rows = paginator.paginate_queryset(
            customer_domain.members_queryset(
                base_orders,
                order_by=scope.get("order_by", "order_count"),
                require_matching_order=has_filter or creator is not None,
                with_source_stats=creator is None,
            ),
            request,
            view=self,
        )
        customer_ids = [row.pk for row in aggregate_rows]
        customer_map = {
            row.pk: row for row in Customer.objects.filter(pk__in=customer_ids)
        }
        latest_map = customer_domain.latest_orders_for_customers(base_orders, customer_ids)
        display_names = recorder_display_map(
            latest.created_by for latest in latest_map.values()
        )
        items = [
            _serialize_member(
                aggregate_row=row,
                customer=customer_map[row.pk],
                # 零系统单客户不在 latest_map 里（第五批扩容出的新行形状）⇒ None。
                latest_order=latest_map.get(row.pk),
                # 显示名映射的键 = 最近一单的 `created_by`（与 `recorder` 出参同一来源）。
                recorder_display=(
                    display_names.get(latest.created_by, latest.created_by)
                    if (latest := latest_map.get(row.pk)) is not None
                    else ""
                ),
            )
            for row in aggregate_rows
        ]
        if creator is not None:
            # 限制档：`total` = 自己可见的行数（不再是客户表全量）。
            total = Customer.objects.filter(
                customer_domain.restricted_member_customer_q(creator)
            ).count()
        else:
            total = Customer.objects.count()
        payload = paginated(
            items,
            # ⭐ 业务方拍板「直接全算」：`total` 恒为客户表全量数（限制档 = 其可见行数），
            #   不受筛选影响；分页用的「筛选后行数」单独放 `filtered_total`（翻页按它算页数）。
            total=total,
            page=paginator.page.number,
            page_size=paginator.get_page_size(request),
        )
        payload["filtered_total"] = paginator.page.paginator.count
        recorder_counts = list(
            base_orders.filter(customer__isnull=False)
            .exclude(created_by="")
            .order_by()
            .values("created_by")
            .annotate(customer_count=Count("customer_id", distinct=True))
            .order_by("-customer_count", "created_by")
        )
        names = recorder_display_map(row["created_by"] for row in recorder_counts)
        payload["recorder_counts"] = [
            {"recorder": row["created_by"], "name": names.get(row["created_by"], row["created_by"]), "count": row["customer_count"]}
            for row in recorder_counts
        ]
        return api_response(payload)


class CustomerMemberFiltersView(APIView):
    """`GET /api/business/customers/member-filters/` —— 列表页两个下拉的候选字典。"""

    def get_permissions(self):
        return [has_node(CUSTOMER_MEMBER_NODE, CUSTOMER_SELF_ONLY_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看客户（会员）列表的权限", code=code
        )

    def get(self, request):
        # 来源 = 现成 OrderSource 字典（Meta.ordering：-order_by, id）；录单人 = 本租户
        # 在库订单 `created_by` 去重（默认 manager 排除已删单；空串 = 手工漏填，不进候选）。
        # 第二批起出参 = [{value: 登录账号, label: 显示名}]——下拉显示账号名、提交 value。
        sources = [
            {"id": row["id"], "name": row["name"]}
            for row in OrderSource.objects.values("id", "name")
        ]
        creator = _restricted_creator(request)
        if creator is not None:
            # 限制档（第五批 §2.4）：录单人下拉只回自己（别人不在其可见范围，给了也无意义）。
            recorder_names = [creator]
        else:
            recorder_names = list(
                Order.objects.exclude(created_by="")
                .values_list("created_by", flat=True)
                .distinct()
                .order_by("created_by")
            )
        display_names = recorder_display_map(recorder_names)
        recorders = [
            {"value": name, "label": display_names.get(name, name)}
            for name in recorder_names
        ]
        return api_response(data={"sources": sources, "recorders": recorders})


def _serialize_member(
    *, aggregate_row, customer: Customer, latest_order: Order | None,
    recorder_display: str,
) -> dict:
    """一行会员 = 客户档案键 + 合并统计 + 最近一单的展示快照。

    行集含**零单客户**（来源单-only / 裸档等）：`latest_order=None` 时展示列全空、
    金额归 "0.00"（E-0021：零是合法值不是缺值），`last_order_at` 回空串。
    统计 = 系统单 + 有效来源单合并（第六批；限制档 = 系统单口径，见 `members_queryset`）。
    行 = 带注水的 Customer 实例，聚合值走属性。
    """
    total = aggregate_row.total_amount
    if total is None:
        total = Decimal("0.00")
    return {
        "eid": customer.eid,
        "buyer_nick": customer.buyer_nick,
        "phone": latest_order.customer_phone if latest_order else "",
        "order_count": aggregate_row.order_count,
        "last_order_at": (
            local_iso(aggregate_row.last_order_at)
            if aggregate_row.last_order_at is not None
            else ""
        ),
        # 金额出参恒为两位小数字符串（Decimal 不进 JSON：DRF 的编码器会把它转 float）。
        "total_amount": f"{total:.{TOTAL_AMOUNT_DECIMALS}f}",
        "order_source": latest_order.order_source_id if latest_order else None,
        "source_name": order_source_display_name(latest_order) if latest_order else "",
        "recorder": latest_order.created_by if latest_order else "",
        "recorder_display": recorder_display,
    }


def _serialize_drawer_system_order(order: Order) -> dict:
    """抽屉统一行（系统单侧，第六批 §2.3）：`no` = 系统单号（前端可点开详情）；
    `status` = OrderStatus 枚举值——v2 页显示词由前端 V2 词表映射（「待接单」覆盖只落 v2，
    ⛔ 不改共享 label；`status_text` = 共享 label 兜底）；`recorder` 切片后批量换显示名。"""
    return {
        "no": order.order_no,
        "is_system": True,
        "status": order.status,
        "game": order.game.name,
        "project": order.project_text,
        "amount": f"{order.total_price:.{TOTAL_AMOUNT_DECIMALS}f}",
        "time": local_iso(order.created_at),
        "status_text": order.get_status_display(),
        "shop": order_source_display_name(order),
        "recorder": order.created_by,
    }


def _serialize_drawer_source_order(row) -> dict:
    """抽屉统一行（来源单侧）：单不在系统 ⇒ 无游戏/项目/录单人（空串，前端画「—」）、
    不可点详情；`status` = 闲管家码原值、`status_text` 按码表中文（未知码兜底「未知」）。"""
    return {
        "no": row.source_order_no,
        "is_system": False,
        "status": row.order_status,
        "game": "",
        "project": "",
        "amount": f"{row.total_amount:.{TOTAL_AMOUNT_DECIMALS}f}",
        "time": local_iso(row.updated_time) if row.updated_time else "",
        "status_text": XGJ_SOURCE_STATUS_LABELS.get(
            row.order_status, XGJ_SOURCE_STATUS_UNKNOWN
        ),
        "shop": row.shop_name,
        "recorder": "",
    }


class CustomerMemberOrdersView(APIView):
    """`GET /api/business/customers/member-orders/?eid=…` —— 客户订单抽屉（只读列表）。

    出参 = **单一合并列表**（第六批 2026-11-11，业务方拍板「两段并一张表」）：系统订单
    （`Order.customer` FK 反查）+ 有效来源订单（`customers.drawer_source_orders`，去重
    剔无效）按时间倒序（系统单 `created_at` / 来源单 `updated_time`；同时刻系统单在前
    ——两段拼接后稳定排序；时间可空的来源单垫底）。合并排序在 Python 完成（单客户
    量级 ≤ 数百；两侧各截断 `SOURCE_ORDERS_MERGE_CAP` / `SYSTEM_ORDERS_MERGE_CAP`，
    同值）。统一行带 `status` 枚举键（系统单 = OrderStatus 枚举值、来源单 = 闲管家码；
    v2 显示词 = 前端 V2 词表，来源单中文仍由 `status_text` 给）。外层 members 筛选
    **不**作用在这里——抽屉是客户档案的事实面（业务方 2026-11-10 拍板）。
    第七批 §1.1：出参另带 `customer_info`（头部信息卡 = 档案键 + 合并统计，口径 =
    `customers.members_queryset` 全量底集、不随外层筛选变；eid 打掩码）。
    """

    def get_permissions(self):
        return [has_node(CUSTOMER_MEMBER_NODE, CUSTOMER_SELF_ONLY_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看客户（会员）列表的权限", code=code
        )

    def get(self, request):
        serializer = CustomerMemberOrdersQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        eid = serializer.validated_data["eid"]
        # 限制档：抽屉同范围收口——别人的客户查不到（与列表同一句「客户不存在」，
        # 不暴露「存在但无权」）。
        customer_qs = Customer.objects.filter(eid=eid)
        creator = _restricted_creator(request)
        if creator is not None:
            customer_qs = customer_qs.filter(
                customer_domain.restricted_member_customer_q(creator)
            )
        customer = customer_qs.first()
        if customer is None:
            return api_response(data=None, msg=CUSTOMER_NOT_FOUND_MSG, code=ErrorCode.VALIDATION)
        pairs = [
            (order.created_at, _serialize_drawer_system_order(order))
            for order in customer.orders.select_related("game", "order_source")[
                :customer_domain.SYSTEM_ORDERS_MERGE_CAP
            ]
        ] + [
            (row.updated_time, _serialize_drawer_source_order(row))
            for row in customer_domain.drawer_source_orders(customer)
        ]
        # 时间倒序（系统单 created_at 恒非空；来源单 updated_time 可空 ⇒ 垫底）。
        timed = sorted(
            (pair for pair in pairs if pair[0] is not None),
            key=lambda pair: pair[0],
            reverse=True,
        )
        merged = [row for _, row in timed] + [row for at, row in pairs if at is None]
        paginator = CustomerMembersPagination()
        items = paginator.paginate_queryset(merged, request, view=self)
        # 录单人显示名只在当前页上映射（批量一次查询；来源单行 recorder 恒空串）。
        display_names = recorder_display_map(
            {item["recorder"] for item in items if item["recorder"]}
        )
        for item in items:
            if item["recorder"]:
                item["recorder"] = display_names.get(item["recorder"], item["recorder"])
        payload = paginated(
            items,
            total=paginator.page.paginator.count,
            page=paginator.page.number,
            page_size=paginator.get_page_size(request),
        )
        # 头部信息卡（第七批 §1.1）：档案键 + 合并统计（与会员行同一口径的查询实现，
        # ⛔ 不抄第二份聚合）；eid 打掩码出参。
        stats_row = customer_domain.members_queryset(Order.objects.all()).filter(
            pk=customer.pk
        ).first()
        latest = customer_domain.latest_orders_for_customers(
            Order.objects.all(), [customer.pk]
        ).get(customer.pk)
        info_total = (
            stats_row.total_amount
            if stats_row is not None and stats_row.total_amount is not None
            else Decimal("0.00")
        )
        payload["customer_info"] = {
            "eid_masked": mask_eid(customer.eid),
            "buyer_nick": customer.buyer_nick,
            "phone": latest.customer_phone if latest else "",
            "total_amount": f"{info_total:.{TOTAL_AMOUNT_DECIMALS}f}",
            "order_count": stats_row.order_count if stats_row is not None else 0,
            "last_order_at": (
                local_iso(stats_row.last_order_at)
                if stats_row is not None and stats_row.last_order_at is not None
                else ""
            ),
            "created_at": local_iso(customer.created_at),
        }
        return api_response(payload)


class CustomerAnalysisQuerySerializer(serializers.Serializer):
    """analysis 的入参判据：`range` 三档 choice（default = `30d`，缺席与畸形都收不进查询口，E-0010）。"""

    range = serializers.ChoiceField(
        choices=list(customer_domain.ANALYSIS_RANGES), default="30d"
    )


def _money(value) -> str:
    """聚合金额出口 = 两位小数字符串（量化 ROUND_HALF_UP，与 dashboard_views._money 同款）。"""
    if value is None:
        value = Decimal("0.00")
    return f"{Decimal(value).quantize(customer_domain.ANALYSIS_CENT, rounding=ROUND_HALF_UP):.2f}"


class CustomerAnalysisView(APIView):
    """`GET /api/business/customers/analysis/?range=30d|90d|12m` —— 客户分析报表一次性聚合。

    随 range：kpi 的复购 / 本期消费 / 客单价、source_dist 与 source_customer_dist（第七批
    §1.3 各店铺有订单客户数）；不随 range：客户总数（恒全量）、
    消费分层（全时段累计画像）、客户增长（固定近 12 月）。取数唯一实现 =
    `apps/business/customers.py` 的分析段（本文件只做出参与信封，不抄第二份聚合）。
    """

    def get_permissions(self):
        # 三节点任一（本点的自家门 + 客户管理 + 限制档）；聚合数据**不**按录单人收口——
        # 「分析报表不受限制节点影响」是 2026-11-11 的拍板口径（各自节点独立）。
        return [has_node(
            CUSTOMER_MEMBER_NODE, CUSTOMER_ANALYSIS_NODE, CUSTOMER_SELF_ONLY_NODE,
        )()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看客户（会员）列表的权限", code=code
        )

    def get(self, request):
        serializer = CustomerAnalysisQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        range_key = serializer.validated_data["range"]
        start, end = customer_domain.analysis_window(range_key)
        kpi = customer_domain.analysis_kpi(start, end)
        return api_response(
            {
                "range": range_key,
                "kpi": {
                    "customer_total": kpi["customer_total"],
                    "repeat_customers": kpi["repeat_customers"],
                    "period_amount": _money(kpi["period_amount"]),
                    "period_orders": kpi["period_orders"],
                    "avg_order": _money(kpi["avg_order"]),
                },
                "source_dist": [
                    {
                        "order_source": row["order_source"],
                        "name": row["name"],
                        "amount": _money(row["amount"]),
                    }
                    for row in customer_domain.analysis_source_dist(start, end)
                ],
                "source_customer_dist": customer_domain.analysis_source_customer_dist(
                    start, end
                ),
                "consume_tiers": customer_domain.analysis_consume_tiers(),
                "growth": customer_domain.analysis_growth(),
            }
        )
