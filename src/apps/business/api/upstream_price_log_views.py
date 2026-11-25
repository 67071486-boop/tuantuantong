"""`GET /api/business/upstream-price-change-logs/`：上家订单改价记录的**只读**列表（spec `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §3-D3）。为什么是**独立视图**而不是给 `OrderViewSet` 加 `@action`：spec §D3 明文——`@action` 会多生成一个 `orders/…` 入口（同一件事两个入口），且独立 `ListAPIView` + `apps/business/urls.py` 一条显式 `path()` 文件所有权清晰，符合本仓「显式 path、⛔ 不给 ViewSet 加只读 action」的既有先例（`hall/`、`tb-send-logs/` 都这么落）。
查询参数（全部可空，空串按「没传」处理）：`stime` = `DateTimeField`（`occurred_at >= stime`，只给日期 ⇒ 当天 `00:00:00` 即含当天的起点）；`etime` = `InclusiveEndDateTimeField`（`occurred_at <= etime`，**只给日期 ⇒ 含当天**，复用 `apps/audit/api/serializers.py` 的唯一实现）；`key` = `order_no__icontains`（列宽从模型元数据取，⛔ 不手抄）；`page` / `page_size` 由 `EnvelopePagination`（默认 20、上限 200）自己认，这里照样声明（与同族端点同形）。非法值（`?stime=garbage` / 超长 `key`）→ DRF `ValidationError` → 统一异常处理器翻成 **HTTP 200 + `code=1000`**（E-0010：判据是「**这类**请求还会不会 500」）。
载荷：统一信封 + `EnvelopePagination` 分页块（`{items,total,page,page_size}` 在 `data` 内）；行形状恰好 10 个键（spec §D3 的 9 键 + D-0301 加的 `source_order_no`，键集由 `tests/test_upstream_price_change_log.py::ROW_KEYS` 钉住）：`order_no` / `game_name` / `game_role_name` / `before_amount` / `after_amount` / `source_name` / `note` / `occurred_at` / `is_deleted` / `source_order_no`——金额走 DRF `DecimalField` 默认投影 ⇒ **十进制字符串**（`"12.00"`，可 `null`），两位小数与时间格式化由前端负责（spec §D3 末段）。
"""

from django.db.models import Q
from rest_framework import generics, serializers

from apps.audit.api.serializers import InclusiveEndDateTimeField
from apps.business.api.base import EnvelopePagination
from apps.business.models import Order, UpstreamPriceChangeLog
from apps.rbac.permissions import has_node


class UpstreamPriceChangeLogSearchSerializer(serializers.Serializer):
    """查询参数（spec §D3）：`stime` / `etime` / `key` / `page` / `page_size`，全部可空。⚠️ `etime` 用 `InclusiveEndDateTimeField`（**只给日期 ⇒ 含当天**）。这是**复用**：
    实现在 `apps/audit/api/serializers.py`，⛔ 不在这里另写一份（那会让「含当天」这条口径出现第二个版本）。⚠️ `key` 的宽度**从模型元数据取**（`UpstreamPriceChangeLog.order_no` 的 20），⛔ 不手抄数字：
    抄一份就与模型漂移，模型加宽时合法单号会被拒成 1000。⚠️ `page` / `page_size` 声明在这里只是为了与同族端点同形（`OrderActionLogSearchSerializer`的写法）；真正的分页校验在 `EnvelopePagination`（它的 `page_size` 上限是 200），这两个值**本视图不消费**。"""

    stime = serializers.DateTimeField(required=False)
    etime = InclusiveEndDateTimeField(required=False)
    key = serializers.CharField(
        required=False,
        max_length=UpstreamPriceChangeLog._meta.get_field("order_no").max_length,
    )
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1)


class UpstreamPriceChangeLogRowSerializer(serializers.ModelSerializer):
    """行形状——spec §D3 的 9 键 + D-0301 加的 `source_order_no` = **恰好 10 个键**（键集由 `tests/test_upstream_price_change_log.py::ROW_KEYS` 钉住）。⚠️ `is_deleted` 不是模型列：当前页的 `order_no` 由视图**一次性批量**回查订单现状后经 `context["deleted_order_nos"]` 注入（照 `OrderActionLogRowSerializer` 的 `order_backfill` / `actor_display_names` 手法，⛔ 不许逐行 `Order.objects.get`）；回查用 `Order.all_objects`（**含软删**）⇒ 已删单要能被认出来，**查不到的订单**（硬删 / 跨库残留）⇒ `True`（已不可查看，spec §D3）。
    ⚠️ `before_amount` / `after_amount` 用 DRF 默认的 `DecimalField` 投影：**字符串**输出、可 `null`（首次从无价变有价时 `before_amount` 是 `null`）——前端 `formatMoney` 负责两位小数。⭐（D-0301）：加 `source_order_no`（来源单号，**实时回查 `Order`**——本表不存它）给前端做「来源号优先、空回退 `order_no`」的展示回退；键数 9 → 10，键集网同一次改，查不到订单 ⇒ `None`。
    """

    is_deleted = serializers.SerializerMethodField()
    source_order_no = serializers.SerializerMethodField()

    def get_is_deleted(self, obj) -> bool:
        """这张单现在还能不能打开（`True` = 已删除 / 订单行不存在）。"""
        # ⚠️ 默认 `True`（不是 `False`）：映射里没有这个单号 = 订单行查不到 ⇒ 已不可查看。
        return self._order_facts(obj)[0]

    def get_source_order_no(self, obj) -> str | None:
        """这张订单现在的来源单号（订单查不到 ⇒ `None`；在库 ⇒ 可能为空串）。"""
        return self._order_facts(obj)[1]

    def _order_facts(self, obj) -> tuple[bool, str | None]:
        """`is_deleted` 与 `source_order_no` 共用的批量回查读数（见 `_deleted_order_nos`）。"""
        facts = self.context.get("deleted_order_nos") or {}
        return facts.get(obj.order_no, (True, None))

    class Meta:
        model = UpstreamPriceChangeLog
        fields = (
            "order_no",
            # ⭐（D-0301）追加在 `order_no` 之后，既有键的位置一字不动。
            "source_order_no",
            "game_name",
            "game_role_name",
            "before_amount",
            "after_amount",
            "source_name",
            "note",
            "occurred_at",
            "is_deleted",
        )


def _deleted_order_nos(rows) -> dict[str, tuple[bool, str | None]]:
    """`order_no` → (是否已删/不存在, 现来源单号) 的**一次性**映射（spec §D3 + D-0301）。⚠️ **一次查询**（`order_no__in`，当前页去重之后）：⛔ 不许在序列化器里逐行`Order.objects.get(...)`——一页 200 行会打 200 次库。
    ⚠️ **必须用 `Order.all_objects`**（纯 `models.Manager`，**不带** `deleted_at IS NULL`默认排除）：本判据要的正是「已软删的那些单」；用默认 manager 会把它们全部漏掉 ⇒已删单的 `is_deleted` 恒为 `False`（判据 16 会红）。
    使用点已登记进`tests/test_order_read_path_net.py::ALL_OBJECTS_SITES`。⚠️ 只取三列（`values_list`；D-0301 加 `source_order_no`）：本项目里这一页是纯展示，别把订单整行拉回来。映射里没有的单号 = 订单行查不到 ⇒ 消费方按「已删 + `None`」处理。"""
    order_nos = {row.order_no for row in rows if row.order_no}
    if not order_nos:
        return {}
    return {
        order_no: (deleted_at is not None, source_order_no)
        for order_no, deleted_at, source_order_no in Order.all_objects.filter(
            order_no__in=order_nos
        ).values_list("order_no", "deleted_at", "source_order_no")
    }


class UpstreamPriceChangeLogListView(generics.ListAPIView):
    """上家订单改价记录的只读列表（spec §D3）。排序**固定** `-occurred_at, -id`。⚠️ 排序写死、不开放 `ordering` 参数：
    本页只有一个时间列可排（对标证据件 9 列里也没有排序控件）；
    固定排序的另一半理由是**翻页稳定**——`-id` 是同一时刻多行的 tie-break（与 `UpstreamPriceChangeLog.Meta.ordering` 同源）。⚠️ 只读：本类没有 `create` / `update` / `destroy`（`ListAPIView` 只有 GET/HEAD/OPTIONS），⛔ 不写审计行、⛔ 不写业务数据。"""

    permission_classes = [has_node("order.upstream_price_change_logs")]
    pagination_class = EnvelopePagination
    serializer_class = UpstreamPriceChangeLogRowSerializer

    def _search_params(self):
        """查询参数**唯一**的校验口（E-0010）。返回 `validated_data`。

        空串按「没传」处理：前端清空筛选栏时会送 `?stime=` / `?key=`，那不是非法值而是
        「没选」（口径与 `apps/audit/api/views.py` 的两处 `_search_params` 一致）。
        """
        query = self.request.query_params
        raw = {name: value for name, value in query.items() if value != ""}
        params = UpstreamPriceChangeLogSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_queryset(self):
        data = self._search_params()
        # 租户由连接（认证态）决定：本表没有 tenant 列、端点也没有 tenant/schema 参数入口。
        qs = UpstreamPriceChangeLog.objects.all()

        stime = data.get("stime")
        if stime is not None:
            qs = qs.filter(occurred_at__gte=stime)
        etime = data.get("etime")
        if etime is not None:
            qs = qs.filter(occurred_at__lte=etime)
        key = data.get("key")
        if key is not None:
            # ⭐（D-0301）：双列 icontains OR——系统内置号照旧命中、来源单号也能命中。本表**不存来源号**（⛔ 不建 FK 的留痕表，见模型 docstring）⇒ 走**子查询**：先按来源号找到订单（`Order.all_objects` 含软删，与展示侧 `_deleted_order_nos` 口径一致——软删单的留痕行还在列表里、也还能被搜到），再按它们的内置号收窄本表；子查询不经 join、不放大行。
            qs = qs.filter(
                Q(order_no__icontains=key)
                | Q(
                    order_no__in=Order.all_objects.filter(
                        source_order_no__icontains=key
                    ).values_list("order_no", flat=True)
                )
            )

        # 固定排序（spec §D3）：⛔ 不接受请求值（没有 `ordering` 参数，也就没有「把请求值
        # 直接交给 order_by」那条 E-0010 风险）。
        return qs.order_by("-occurred_at", "-id")

    def get_serializer_context(self):
        """把当前页的 `is_deleted` 映射塞进 context（**一次批量**查询，spec §D3）。⚠️ 时点：
        `ListModelMixin.list` 先 `paginate_queryset`（把页面挂在 `self.paginator.page`上）再 `get_serializer` ⇒ 
        这里拿到的是**当前页**的行，映射只查当前页的单号。⚠️ 分页关闭时 `self.paginator` 可能没有 `page` ⇒ 空列表 ⇒ 映射为空 ⇒ 序列化器全部回 `True`（fail-safe：不炸；本端点的分页始终开着，这条只是兜底）。"""
        context = super().get_serializer_context()
        page = getattr(self.paginator, "page", None) or []
        context["deleted_order_nos"] = _deleted_order_nos(page)
        return context