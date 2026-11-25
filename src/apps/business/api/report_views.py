"""客服接发单统计 / 经营报表的 HTTP 层。spec：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` §6；计划：`docs/plans/2026-10-17-support-stats-and-business-report.md` §3。两个**只读**端点：`GET /api/business/support-stats`（页面一：按操作人员 / 按店铺的两张表）与 `GET /api/business/business-report`（页面二：6 卡 + 来源分布 + 客服统计 + 趋势）。
口径全在 `apps.business.report_stats`（**只读**、可脱离 HTTP 直接测）；本模块只做**参数校验 → 授权 → 信封**。⚠️ 参数校验一律过序列化器（E-0010）：`?date_from=garbage` 进 `filter()` 会抛 `ValueError`（HTML 500），非法值一律业务码 `1000` + 说真话的 msg。未认证 401、无节点 403（DRF 的 `permission_denied` 转调 `super()` 才保留 401 分支）。
"""

from datetime import timedelta

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business import report_stats
from apps.business.report_stats import ReportError, business_report, support_stats
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 准入节点（`apps/rbac/catalog.py` 的 1192「客服接单统计」）。
#: 两页共用一个点：勾了它就能同时看「客服接发单统计」与「经营报表」
#: （spec §0 第三条裁定；代价 = 不能单独授权，见 spec §9 第 8 条）。
REPORT_NODE = "finance.support_statistics"

#: 缺省日期窗 = 最近 30 天（含今天与起点），与两个页面的默认值同口径（spec §5）。
DEFAULT_RANGE_DAYS = 30


class _ReportRangeSerializer(serializers.Serializer):
    """两个日期参数的**唯一**校验口（`yyyy-MM-dd`，都可选）。"""

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


class SupportStatsQuerySerializer(_ReportRangeSerializer):
    """`GET /api/business/support-stats` 的参数（只有日期）。"""


class BusinessReportQuerySerializer(_ReportRangeSerializer):
    """`GET /api/business/business-report` 的参数。

    `stat_type`：`claim`（默认，按接单时间）/ `submit`（按交单时间）；
    `game`：游戏主键；`kefu`：逗号分隔的用户主键。
    """

    stat_type = serializers.ChoiceField(
        choices=("claim", "submit"),
        required=False,
        default="claim",
        error_messages={"invalid_choice": "stat_type 只能是 claim 或 submit"},
    )
    game = serializers.IntegerField(
        required=False,
        min_value=1,
        error_messages={
            "invalid": "game 必须是正整数",
            "min_value": "game 必须是 ≥ 1 的整数",
        },
    )
    kefu = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
        error_messages={"max_length": "kefu 太长了"},
    )

    def validate_kefu(self, value):
        """逗号分隔的用户主键 → `list[int]`；空串 = 不筛。"""
        raw = (value or "").strip()
        if raw == "":
            return []
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if not part.isdigit() or int(part) < 1:
                raise serializers.ValidationError(
                    "kefu 必须是逗号分隔的正整数用户 ID"
                )
            ids.append(int(part))
        return ids


def _range(data) -> tuple:
    """把可选日期补成区间（缺省最近 30 天，含今天）。"""
    date_to = data.get("date_to")
    date_from = data.get("date_from")
    if date_to is None:
        date_to = report_stats._today()
    if date_from is None:
        date_from = date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    return date_from, date_to


def _clean_query(request) -> dict:
    """空串按「没传」处理（前端清空日期框时会发 `?date_from=`）。"""
    return {key: value for key, value in request.query_params.items() if value != ""}


class SupportStatsView(APIView):
    """`GET /api/business/support-stats`（页面一）。"""

    def get_permissions(self):
        return [has_node(REPORT_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看客服接发单统计的权限", code=code
        )

    def get(self, request):
        params = SupportStatsQuerySerializer(data=_clean_query(request))
        params.is_valid(raise_exception=True)
        date_from, date_to = _range(params.validated_data)
        try:
            data = support_stats(
                date_from=date_from, date_to=date_to, viewer=request.user
            )
        except ReportError as exc:
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(data)


class BusinessReportView(APIView):
    """`GET /api/business/business-report`（页面二）。"""

    def get_permissions(self):
        return [has_node(REPORT_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有查看经营报表的权限", code=code
        )

    def get(self, request):
        params = BusinessReportQuerySerializer(data=_clean_query(request))
        params.is_valid(raise_exception=True)
        data = params.validated_data
        date_from, date_to = _range(data)
        try:
            payload = business_report(
                date_from=date_from,
                date_to=date_to,
                stat_type=data.get("stat_type") or "claim",
                game_id=data.get("game"),
                kefu_ids=data.get("kefu") or [],
                viewer=request.user,
            )
        except ReportError as exc:
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(payload)
