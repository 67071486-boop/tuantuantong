"""闲管家**收款明细**读端点（收款线 spec §6；自建读面，统一信封 + 登录 + 节点门）。两条路由（`apps/business/urls.py`，照 `order-stats` 的既有形状）：| 路径 | 干什么 ||---|---|| `GET /api/business/xgj-receipts/summary/` | 按店铺分组：
店名 / 笔数 / 实收总额 / 金额缺失笔数（+ 合计块） || `GET /api/business/xgj-receipts/` | 同筛选的分页明细行（单号 / 店名 / 金额 / 时间 / 匹配结果） |## 形态（照 `order_stats_views.py` 先例，一个字都不发明）**序列化器校验 → 节点门 → `api_response`**；取数与口径全在`apps/business/xgj_receipt_stats.py`（只读、可脱离 HTTP 直接测）。- ⚠️ 请求来源的值进类型化查询之前一律过序列化器（E-0010）：
`?date_from=garbage`直接进 `filter()` 会抛 `ValueError`（HTML 500）。两参都**可选**，缺省 = **前一天..前一天**（业务方 R6：默认展示前一天的全部明细）。- ⚠️ `date_from > date_to` ⇒ 显式业务码 `1000` + 说真话的 msg（⛔ 不静默交换、也不回空表）。- ⚠️ 授权判据是 RBAC 节点 code（`has_node`，本仓第**七**条自有节点`finance.xgj_receipt_detail`，spec §4-D8）；未认证 401，无权限 403。- ⚠️ 读端点**不写操作日志**（既有读面口径；`DELIBERATELY_NOT_LOGGED` 只收写端点）⇒本模块的两个 GET **不需要**登记（spec §6，评审已核 `test_operation_log_ledger.py` 的口径）。"""
from datetime import date, timedelta

from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.api.base import EnvelopePagination
from apps.business import xgj_receipt_stats
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 准入节点（`apps/rbac/catalog.py` 的第七条自有节点，spec §4-D8）。
XGJ_RECEIPT_NODE = "finance.xgj_receipt_detail"

#: 缺省窗口 = **前一天..前一天**（业务方 R6；`USE_TZ` 下的「今天」按本地日）。
_DEFAULT_DAYS_BACK = 1


class XgjReceiptRangeSerializer(serializers.Serializer):
    """日期区间的**唯一**校验口（照 `OrderStatsSearchSerializer` 的写法）。

    ⚠️ 只吃 `yyyy-MM-dd`（`strptime`），带时分秒 / 紧凑形式一律 `1000` + 指名道姓的 msg。
    """

    date_from = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "date_from 格式必须是 yyyy-MM-dd"},
    )
    date_to = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "date_to 格式必须是 yyyy-MM-dd"},
    )
    #: ⭐ 公司店铺分组（计划 §4 #6）：只留「店铺**当前**所属来源属于该分组」的行。
    #: `min_value=1` 挡掉 `0`/负数；`abc` 由 `IntegerField` 回 200 + `1000`（⛔ 不是 HTML 500）。
    #: ⚠️ 语义是「店铺**当前**来源」⇒ 历史行会随归组变化（hint 与交付说明已写明）。
    source_group = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        """缺省 = 前一天；起 > 止 ⇒ 说真话的业务错（在序列化器层抛 `ValidationError`
        ⇒ 200 + `1000`，与「格式错」同一出口）。"""
        yesterday = timezone.localdate() - timedelta(days=_DEFAULT_DAYS_BACK)
        date_from = attrs.get("date_from", yesterday)
        date_to = attrs.get("date_to", yesterday)
        if date_from > date_to:
            raise serializers.ValidationError({"date_from": "date_from 不能晚于 date_to"})
        attrs["date_from"] = date_from
        attrs["date_to"] = date_to
        return attrs


class XgjReceiptSummaryView(APIView):
    """`GET /api/business/xgj-receipts/summary/` —— 按店铺分组的交易成功统计。"""

    def get_permissions(self):
        """闸门 = `has_node(XGJ_RECEIPT_NODE)`（写成方法的理由见
        `order_stats_views.OrderStatsView.get_permissions`：接线位点表按方法体判据）。"""
        return [has_node(XGJ_RECEIPT_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个可辨的权限语义 `msg`（必须转调 `super()`，理由同 `OrderStatsView`）。"""
        super().permission_denied(request, message="你没有查看闲管家收款明细的权限", code=code)

    def get(self, request):
        serializer = XgjReceiptRangeSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        return api_response(
            data=xgj_receipt_stats.summary_rows(
                data["date_from"], data["date_to"],
                # ⭐：两个视图各自转发一次（summary / list），漏一处就一边多一边少。
                source_group=data.get("source_group"),
            )
        )


class XgjReceiptListQuerySerializer(XgjReceiptRangeSerializer):
    """明细端点在区间之外**多收**的键：`shop_identity`（点店铺看单店的明细行，spec §8.1 / §12 F2-D）。⚠️ 可选；
    **缺席**（不设 `default` ⇒ 键不进 `validated_data`）= 不筛；
    给了就按它精确筛——**空串 = 只看「未识别店铺」桶**（identity 为空的行）。空串与缺席必须可分辨，否则点桶会静默变成「整窗明细都算这一家的」（取数层`detail_rows` 的 docstring 有同一条）。"""

    shop_identity = serializers.CharField(max_length=128, required=False, allow_blank=True)


class XgjReceiptListPagination(EnvelopePagination):
    """明细分页：默认 50 / 上限沿用全局 200（每天几十到几百行，一页足够装下大多数日子）。"""

    page_size = 50
    page_size_query_param = "page_size"


class XgjReceiptListView(APIView):
    """`GET /api/business/xgj-receipts/` —— 同筛选的分页明细行。"""

    pagination_class = XgjReceiptListPagination

    def get_permissions(self):
        return [has_node(XGJ_RECEIPT_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(request, message="你没有查看闲管家收款明细的权限", code=code)

    def get(self, request):
        serializer = XgjReceiptListQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        rows = xgj_receipt_stats.detail_rows(
            data["date_from"],
            data["date_to"],
            # ⚠️ 缺席 = None（不筛）、空串 = 未识别桶（F2-D）；⛔ 不写 `or None`——
            #    那会把两档折成一档（E-0021 同族：边界值不许过真值折叠）。
            shop_identity=data.get("shop_identity"),
            # ⭐（公司店铺分组）：明细与 summary 用**同一个**筛法。
            source_group=data.get("source_group"),
        )
        paginator = XgjReceiptListPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            xgj_receipt_stats.serialize_details(page)
        )