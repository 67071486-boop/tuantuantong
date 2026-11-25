"""`GET /api/system/request-metrics/`：请求耗时聚合）。契约（键名冻结）见 `docs/plans/2026-10-04-request-metrics.md` §4；`RequestMetric`（埋点中间件交付的）（`apps/audit/models.py`）是**唯一**数据源，本模块只读——⛔ 没有写接口、⛔ 没有导出（那是业务裁定，不是「还没做」，spec §8 第 3 条）。
权限 `IsAccountAdmin`（现成判据；⛔ 不新造权限节点——`apps/rbac/catalog.py` 是对标权限树声明式全表，自造对标没有的点会同时破掉计数 / 溯源 / 原文照抄三条不变式）：未登录 ⇒ DRF `NotAuthenticated` ⇒ **401**，已登录非账号管理员 ⇒ **403**。路由登记在 `apps/core/urls.py`：路径**逐字冻结**（前端 `requestMetrics.ts::REQUEST_METRICS_URL` 写死）、`api/system/` 前缀不属于任何既有 app 的 urlconf，且新挂 urlconf 或写根 `config/urls.py` 都会被 `tests/test_operation_log_coverage.py` 的 `COVERAGE_SCOPE` 网红（根 urlconf 容忍面钉死在 `{"healthz"}`）——登记在 core/urls 不需要动任何结构网。
聚合口径：默认窗 = 今天 + 往前 6 个自然日（与前端 `system-perfanalysis.ts::defaultPerfDateRange` 同一定义，⛔ 不是 `today - 7`；`date_from` 缺省锚在 `date_to`）；窗口是本地自然日、按瞬时比较（⛔ 不用 `__date` 查表——时区换算交给数据库会话会漂移）；`summary.requests` = 被统计行 `count` 之和，`slow_requests` **恒取** `kind="slow"` 批（与 `slow_only` 无关），`avg_ms` = `total_ms` 之和 ÷ `requests`；`by_route` / `by_user` / `slowest` 各取前 50 条、排序必须确定（⛔ 不走分页器，这是分析页不是列表页）；⚠️ `values(...).annotate(...)` **只允许**按分组字段或聚合结果排序（`RequestMetric` 刻意没有 `Meta.ordering`——模型级默认排序会被带进 GROUP BY，⛔ 别加 `order_by("id")`）；⚠️ 端点自己那一行排除在聚合外（页面越刷它自己 count 越大——「看数据」不能改数据，⛔ 数据不删）；⚠️ `by_user` 按 `username` 写入时刻快照分组 ⇒ 员工改名跨小时的请求裂两行（既定语义、改名不回溯，⛔ 不是缺陷）；参数语义：`date_from`/`date_to` 走 `DateField` iso-8601、畸形值当场拒成 200+业务码 1000（⛔ 不是 HTML 500，先例 E-0010），`route`/`username` 精确匹配、空串按「没传」，`slow_only` 归一成真 bool，`date_from` 晚于 `date_to` 不额外报错、按空窗口处理（`range` 回显实际窗口）。
"""
import datetime as dt

from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import generics, serializers

from apps.accounts.permissions import IsAccountAdmin
from apps.audit.metrics import HIST_BOUNDS_MS, HIST_FIELDS
from apps.audit.models import RequestMetric, RequestMetricKind
from apps.core.envelope import api_response

#: 默认窗的**自然日数**（含今天）。见模块 docstring「默认时间窗」。
DEFAULT_RANGE_DAYS = 7

#: `by_route` / `by_user` / `slowest` 各取前 N 条（spec §4）。⛔ 不做分页器。
TOP_N = 50

#: 本端点自己的 `resolver_match.route`（= 中间件写进 `RequestMetric.route` 的那个值）。
#: ⚠️ **唯一出处**：`apps/core/urls.py` 的 `path()` 也用它拼路由 ⇒ 路由字面量只有这一份，⛔ 不要在第二处再抄一遍（抄了就会在改路由时一半新一半旧、而且不会有任何网报错）。用途：把本端点自己产生的聚合行排除掉（见模块 docstring 的聚合口径）。
ENDPOINT_ROUTE = "api/system/request-metrics/"

#: `summary.hist` 的档位名。⚠️ 从 `metrics.HIST_BOUNDS_MS` **派生**（不手抄 6 个字符串）：与 `HIST_FIELDS` 是同一组档位，两处各写一份必有一处先漂移；长度 == `HIST_FIELDS` 由 `tests/test_request_metric_api.py::test_hist_labels_pair_with_the_histogram_columns` 钉住。
HIST_LABELS = tuple(
    [f"<{bound}ms" for bound in HIST_BOUNDS_MS] + [f">={HIST_BOUNDS_MS[-1]}ms"]
)

#: 列宽从**模型元数据**取（不另抄数字）：`route` / `username` 超长时当场拒成 1000，
#: 而不是拿一个永远匹配不到的值去查库（先例：`OperationLogSearchSerializer.actor`）。
_ROUTE_MAX_LENGTH = RequestMetric._meta.get_field("route").max_length
_USERNAME_MAX_LENGTH = RequestMetric._meta.get_field("username").max_length


class RequestMetricsSearchSerializer(serializers.Serializer):
    """5 个可选查询参数的**唯一**校验口（E-0010：请求来源的值不许直接进查询）。"""

    #: `DateField` 的默认 `input_formats = ["iso-8601"]` ⇒ 只认 `yyyy-MM-dd`。
    #: 畸形值（`garbage` / `2026/10/04` / `2026-13-45`）在这里被拒 ⇒ 1000。
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    route = serializers.CharField(required=False, max_length=_ROUTE_MAX_LENGTH)
    username = serializers.CharField(required=False, max_length=_USERNAME_MAX_LENGTH)
    #: 缺省 `False`（= 统计 agg 行）。`BooleanField` 把 `true` / `1` / `on` 等归一成真 bool。
    slow_only = serializers.BooleanField(required=False, default=False)


def _window(date_from, date_to):
    """把两个可选日期补成**有效的**窗口（见模块 docstring「默认时间窗」）：「最近 7 天」= 今天 + 往前 6 个自然日（⛔ 不是 `today - 7`，那是 8 天）；`date_from` 缺省时锚在 **`date_to`** 上——只传 `date_to` 的用户要的是「那一天往前一周」，锚在今天会得到起点晚于终点的空窗口。"""
    if date_to is None:
        date_to = timezone.localdate()
    if date_from is None:
        date_from = date_to - dt.timedelta(days=DEFAULT_RANGE_DAYS - 1)
    return date_from, date_to


def _bounds(date_from, date_to):
    """窗口的瞬时上下界：`[date_from 00:00, date_to 次日 00:00)`（本地时区）。⚠️ 用显式的 aware datetime 比较，⛔ **不用** `bucket_start__date__gte=...`：后者的时区换算发生在数据库会话上（`AT TIME ZONE`），口径会随连接设置漂移。"""
    tzinfo = timezone.get_current_timezone()
    start = dt.datetime.combine(date_from, dt.time.min, tzinfo=tzinfo)
    end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=tzinfo)
    return start, end


def _avg_ms(total_ms, count):
    """平均耗时（四舍五入到毫秒）。`count == 0` ⇒ 0（⛔ 不抛除零）。"""
    if not count:
        return 0
    return round((total_ms or 0) / count)


class RequestMetricsView(generics.GenericAPIView):
    """只读的请求耗时聚合端点（口径见模块 docstring）。"""

    permission_classes = [IsAccountAdmin]
    serializer_class = RequestMetricsSearchSerializer

    def _search_params(self):
        """查询参数**唯一**的校验口。返回 `validated_data`。⚠️ 先转成**普通 dict** 再过序列化器（`apps/accounts/views.py` 记录的坑）：`request.query_params` 是 `QueryDict`，DRF 见到它会走 `html.parse_html_dict` 把请求当 **HTML 表单**解析。空串按「没传」处理：前端清空筛选栏时会送 `?route=`，那不是非法值而是「没选」。"""
        raw = {
            name: value
            for name, value in self.request.query_params.items()
            if value != ""
        }
        params = RequestMetricsSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def _scoped_queryset(self, start, end, *, route, username, kind):
        """窗口 + 精确筛选 + `kind` 三件套。⛔ 这里没有任何 tenant / schema 入口：租户上下文只来自认证态（AGENTS.md §4），schema 由中间件钉在连接上。
        ⚠️ **排除端点自己那一行**（`route == ENDPOINT_ROUTE`，见模块 docstring）：不过滤的话 `summary` / `by_route` / `by_user` 会把「看数据」这件事本身算进去；⛔ 只排除聚合，**不删数据**。
        """
        queryset = RequestMetric.objects.filter(
            bucket_start__gte=start, bucket_start__lt=end
        ).exclude(route=ENDPOINT_ROUTE)
        if route is not None:
            queryset = queryset.filter(route=route)
        if username is not None:
            queryset = queryset.filter(username=username)
        return queryset.filter(kind=kind)

    def _summary(self, scoped, slow_scoped):
        """`summary`（spec §4）：被统计行的求和 + 六个直方图桶 + 慢请求数。"""
        totals = scoped.aggregate(
            requests=Sum("count"),
            total_ms=Sum("total_ms"),
            max_ms=Max("max_ms"),
            **{name: Sum(name) for name in HIST_FIELDS},
        )
        requests = totals["requests"] or 0
        slow_requests = slow_scoped.aggregate(requests=Sum("count"))["requests"] or 0
        return {
            "requests": requests,
            "slow_requests": slow_requests,
            "avg_ms": _avg_ms(totals["total_ms"], requests),
            "max_ms": totals["max_ms"] or 0,
            "hist": [
                # `strict=True`：档位名与直方图列**必须**一一对应，长度不等时
                # 静默截断会少报几档（那是一个看不出来的错数字）。
                {"label": label, "count": totals[name] or 0}
                for label, name in zip(HIST_LABELS, HIST_FIELDS, strict=True)
            ],
        }

    def _grouped(self, scoped, fields):
        """按 `fields` 分组求和（`by_route` / `by_user` 共用），取前 `TOP_N` 条。⚠️ 排序键**只**用分组字段与聚合结果：`RequestMetric` 没有 `Meta.ordering`，而 `values().annotate()` 里任何多余列都会进 `GROUP BY`（见模块 docstring）；同数时按分组字段升序，保证「同样的数据每次回同样的 50 条」。"""
        rows = (
            scoped.values(*fields)
            .annotate(
                requests=Sum("count"),
                total_ms=Sum("total_ms"),
                max_ms=Max("max_ms"),
            )
            .order_by("-requests", *fields)[:TOP_N]
        )
        return [
            {
                **{field: row[field] for field in fields},
                "requests": row["requests"],
                "avg_ms": _avg_ms(row["total_ms"], row["requests"]),
                "max_ms": row["max_ms"] or 0,
            }
            for row in rows
        ]

    def _slowest(self, slow_scoped):
        """最慢的 `TOP_N` 条慢请求（`kind="slow"` 的逐条留痕，spec §4）。`occurred_at` 转成**本地时区**再序列化：库里存的是 UTC（`USE_TZ=True`），直接 `isoformat()` 会回 `+00:00`，页面上比用户看到的钟点早 8 小时。"""
        rows = slow_scoped.order_by("-max_ms", "-bucket_start", "-id")[:TOP_N]
        return [
            {
                "occurred_at": timezone.localtime(row.bucket_start).isoformat(),
                "route": row.route,
                "method": row.method,
                "status_code": row.status_code,
                "username": row.username,
                "duration_ms": row.max_ms,
            }
            for row in rows
        ]

    def get(self, request, *args, **kwargs):
        data = self._search_params()
        date_from, date_to = _window(data.get("date_from"), data.get("date_to"))
        start, end = _bounds(date_from, date_to)
        route = data.get("route")
        username = data.get("username")
        # 统计哪一类行：缺省 agg（按小时汇总），`slow_only=true` 只统计 slow（spec §4）。
        kind = RequestMetricKind.SLOW if data["slow_only"] else RequestMetricKind.AGG

        scoped = self._scoped_queryset(
            start, end, route=route, username=username, kind=kind
        )
        slow_scoped = self._scoped_queryset(
            start, end, route=route, username=username, kind=RequestMetricKind.SLOW
        )

        return api_response(
            data={
                "range": {
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
                "summary": self._summary(scoped, slow_scoped),
                "by_route": self._grouped(scoped, ("route",)),
                "by_user": self._grouped(scoped, ("username", "user_id")),
                # ⚠️ `slowest` 恒取 slow 行（它只可能来自逐条留痕），与 `slow_only` 无关。
                "slowest": self._slowest(slow_scoped),
            }
        )
