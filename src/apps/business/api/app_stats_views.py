"""「APP 与小程序 → 数据概览」端点：`GET /api/business/app-stats/overview/?days=7`（计划 `docs/plans/2026-10-23-app-mp-stats.md` §7 / §8）。权限：准入节点**复用** `finance.support_statistics`（经营报表 / 客服接发单统计那一个点），照 `report_views.py::BusinessReportView` 的写法在 `get_permissions` 里 `has_node(REPORT_NODE)`；⛔ **不新增节点、不建表、不加迁移**（计划 §8 第一条）；未认证 ⇒ 401，已认证但没节点 ⇒ HTTP 403（`permission_denied` 转调 `super()` 才保留 401 分支）。
参数：`days` 默认 **7**、范围 **1..30**（计划 §8 / 业务方），一律过序列化器（E-0010）：`?days=abc` / `?days=0` / `?days=31` 都是 `200 + 1000`、⛔ 不是 500；空串按「没传」处理（前端清空时可能发 `?days=`）。
"""
from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.app_stats import app_stats_overview
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 准入节点（与 `report_views.REPORT_NODE` **同值**：经营报表 / 客服接发单统计那一个点）。⚠️ 这里**另写一份模块级常量**是有意的：接线图的结构网（`tests/test_rbac_wiring.py::test_t2_intercept_sites_really_carry_the_code`）只在**本模块**里解一层常量，直接 import 别处的常量会让它找不到 code；两份同值由 `tests/test_app_stats_api.py::PermissionAndDaysTests.test_node_constant_matches_report_views` 对拍（改一处漏另一处会红）。
REPORT_NODE = "finance.support_statistics"

#: 缺省窗口（天）。
DEFAULT_DAYS = 7
#: 上限（天）。
MAX_DAYS = 30


class AppStatsQuerySerializer(serializers.Serializer):
    """`GET /api/business/app-stats/overview/` 的参数（只有 `days`）。"""

    days = serializers.IntegerField(
        required=False,
        default=DEFAULT_DAYS,
        min_value=1,
        max_value=MAX_DAYS,
        error_messages={
            "invalid": "days 必须是 1 到 30 之间的整数",
            "min_value": "days 最小是 1",
            "max_value": "days 最大是 30",
        },
    )


class AppStatsOverviewView(APIView):
    """`GET /api/business/app-stats/overview/`。"""

    def get_permissions(self):
        return [has_node(REPORT_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看 APP 与小程序数据概览的权限", code=code
        )

    def get(self, request):
        # 空串按「没传」处理（与 report_views._clean_query 同口径）。
        params = AppStatsQuerySerializer(
            data={k: v for k, v in request.query_params.items() if v != ""}
        )
        params.is_valid(raise_exception=True)
        return api_response(app_stats_overview(days=params.validated_data["days"]))
