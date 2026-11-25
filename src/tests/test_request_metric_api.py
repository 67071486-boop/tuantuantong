"""Task 2 判据：`GET /api/system/request-metrics/`（查询接口）。

判据出处：`docs/plans/2026-10-04-request-metrics.md` §4（接口契约）与 §6「Task 2」的六条：
① 非管理员 403；② 管理员 200 且 `summary.requests` == 真实请求数；
③ 按接口 / 按人聚合的**排序与求和**对得上；④ `slow_only=true` 只统计 `slow` 行；
⑤ 畸形日期回 1000 而不是 500；⑥ 不传时间 ⇒ 默认最近 7 天。

⚠️ 断言全部打在**响应载荷的值**上（`response.data["data"][...]`），⛔ 不断言常量、
⛔ 不用 `response.data is not None` 这种恒真句（E-0063）。判据「响应是失败信封」一律看
`response.data["code"]`。

⚠️ **埋点中间件开着**（`REQUEST_METRICS_ENABLED` 默认 True）⇒ 每次请求都会往
`RequestMetric` 写行，包括**打本端点的那些请求**。两个后果，处理方式写在这里免得后来者踩：

1. 本端点自己那一次请求的行是在**视图返回之后**才写的（中间件在 `get_response()` 之后落库）
   ⇒ 它**不会**进本次响应的载荷。⚠️ **改准（本轮 M-3 修复）**：视图现在**显式排除**
   `route == ENDPOINT_ROUTE` 的行（`RequestMetricViews._scoped_queryset`）⇒ 就算表里已经
   躺着上一次请求留下的本端点行，**聚合也不会把它算进去**；`_MetricsCase.get()` 里那次
   删除是**额外的**保险（让夹具读数从零开始），⛔ 不再是正确性的前提。
2. 登录那一次 POST 也会写行 ⇒ `setUp` 里登录完先 `RequestMetric.objects.all().delete()`，
   判据才有「从零开始」的读数。
"""
import datetime as dt

from django.test import Client, SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.api.request_metric_views import HIST_LABELS
from apps.audit.metrics import HIST_FIELDS
from apps.audit.models import RequestMetric, RequestMetricKind
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

#: `TenantTestCase` 自带的租户域名（`django_tenants/test/cases.py`）。
DOMAIN = "tenant.test.com"
#: 端点路径（spec §4，**逐字冻结**——前端 `api/requestMetrics.ts` 按它写死）。
URL = "/api/system/request-metrics/"
#: 本端点自己的 `resolver_match.route`（= 中间件写进 `RequestMetric.route` 的值）。
ENDPOINT_ROUTE = "api/system/request-metrics/"

#: 两个接口（判据 ③ 要「造两个接口」）。
ROUTE_A = "api/business/orders/"
ROUTE_B = "api/auth/me"
#: `/api/auth/me` 的**请求路径**（`ROUTE_B` 是 `resolver_match.route`，不是 URL）。
URL_ME = "/api/auth/me"
#: 两个账号（判据 ③ 要「两个人」）。用**合成**用户名，不用任何真实值。
USER_A = "测试客服甲"
USER_B = "测试客服乙"
USER_A_ID = 4
USER_B_ID = 5
#: 判据 ⑥ 用的三个接口名（同一批夹具、三个日期）。
ROUTE_SEVENTH_DAY = "api/business/orders/seventh-day/"
ROUTE_EIGHTH_DAY = "api/business/orders/eighth-day/"
ROUTE_NINTH_DAY = "api/business/orders/ninth-day/"


def bucket(day, hour=12, minute=0):
    """那一天的**本地** `hour:minute`（aware）——判据里的所有 `bucket_start` 都从它来。

    ⚠️ 用 `timezone.get_current_timezone()`（= `settings.TIME_ZONE`，本仓 `Asia/Shanghai`）：
    与视图的窗口口径同源。⛔ 不要用 `datetime.timezone.utc`（整体偏 8 小时）。
    """
    return dt.datetime.combine(
        day, dt.time(hour=hour, minute=minute), tzinfo=timezone.get_current_timezone()
    )


def make_metric(
    *,
    route,
    bucket_start,
    kind=RequestMetricKind.AGG,
    count=1,
    total_ms=10,
    max_ms=10,
    hist_field="hist_lt100",
    user_id=USER_A_ID,
    username=USER_A,
    method="GET",
    status_code=200,
):
    """直接造一行 `RequestMetric`（夹具；Task 1 的写入原语已由它自己的 20 条判据覆盖）。

    `hist_field` 指定的那一个直方图桶置 `count`，其余为 0 —— 与 Task 1 写入原语的形状一致。
    """
    histogram = {name: 0 for name in HIST_FIELDS}
    histogram[hist_field] = count
    return RequestMetric.objects.create(
        kind=kind,
        bucket_start=bucket_start,
        route=route,
        method=method,
        status_code=status_code,
        user_id=user_id,
        username=username,
        count=count,
        total_ms=total_ms,
        max_ms=max_ms,
        **histogram,
    )


class _MetricsCase(TenantTestCase):
    """本文件共用的夹具：管理员 client + 「从零开始」的表 + 请求辅助。"""

    def setUp(self):
        # 判据 ② 之外的用例只需要"一个能过权限门的人" ⇒ 账号管理员（超管 + 系统管理员职位，
        # 见 `make_logged_in_client` 的 docstring）。
        self.client = make_logged_in_client(is_superuser=True)
        # 登录那一次 POST 也会被埋点 ⇒ 先清空，读数才是从零开始。
        RequestMetric.objects.all().delete()

    def get(self, params=None):
        """打本端点，并先清掉**上一次**请求留下的本端点行。

        ⚠️ 视图自己已经排除端点路由（本轮 M-3）⇒ 这一次删除是**额外**的保险
        （让夹具读数从零开始），⛔ 不是「不清就会被算进 summary」那种正确性前提。
        """
        RequestMetric.objects.filter(route=ENDPOINT_ROUTE).delete()
        return self.client.get(URL, params or {})

    def payload(self, params=None):
        """打本端点并断言「成功信封」，返回 `data`。"""
        response = self.get(params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.data["code"], int(ErrorCode.OK), response.data)
        return response.data["data"]


# ══════════════════════════════════════════════════════════════════════════
# 结构：档位名与直方图列必须一一对应（少一档就是少报一个数字）
# ══════════════════════════════════════════════════════════════════════════
class HistogramLabelsTests(SimpleTestCase):
    def test_hist_labels_pair_with_the_histogram_columns(self):
        """契约写死**6 档**（spec §4 的 `hist` 六格）、首档 `<100ms`、末档 `>=3000ms`。

        ⚠️ **本轮 m-6 修复**：原先断的是 `len(HIST_LABELS) == len(HIST_FIELDS)`——两者同源
        派生（`HIST_LABELS` 就是从 `HIST_BOUNDS_MS` 算的，`HIST_FIELDS` 是同一组档位的列名）
        ⇒ 那条断言**恒真**、少一档也不红。这里改成**字面量 6** + 两个端点标签。
        """
        self.assertEqual(len(HIST_LABELS), 6, "spec §4 的 `hist` 是**恰好 6 档**（字面量契约）")
        self.assertEqual(HIST_LABELS[-1], ">=3000ms")
        self.assertEqual(HIST_LABELS[0], "<100ms")


# ══════════════════════════════════════════════════════════════════════════
# 结构：`data` 的键**逐字**等于 spec §4 冻结的那几个（前端按它们取值）
# ══════════════════════════════════════════════════════════════════════════
class ResponseKeyShapeTests(_MetricsCase):
    """前端 `src/frontend/src/api/requestMetrics.ts` 按这些键名取值 ⇒ 键名**恰好集合**是契约。

    ⚠️ 与前端那侧的类型定义逐条对应（前端不镜像直方图词表，`label` 由后端给）：
    `range{date_from,date_to}` / `summary{requests,slow_requests,avg_ms,max_ms,hist}` /
    `by_route[]` / `by_user[]` / `slowest[]`。多一个键是"契约外的东西"，少一个键是页面空格子。
    """

    def setUp(self):
        super().setUp()
        day = timezone.localdate()
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 1), count=5,
            total_ms=500, max_ms=300, hist_field="hist_lt500",
        )
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 12), kind=RequestMetricKind.SLOW,
            count=1, total_ms=1200, max_ms=1200, hist_field="hist_lt3000",
        )

    def test_data_keys_are_exactly_the_frozen_five(self):
        payload = self.payload({})

        self.assertEqual(
            set(payload), {"range", "summary", "by_route", "by_user", "slowest"}
        )
        self.assertEqual(set(payload["range"]), {"date_from", "date_to"})
        self.assertEqual(
            set(payload["summary"]),
            {"requests", "slow_requests", "avg_ms", "max_ms", "hist"},
        )
        self.assertEqual(set(payload["summary"]["hist"][0]), {"label", "count"})
        self.assertEqual(len(payload["summary"]["hist"]), len(HIST_FIELDS))
        self.assertEqual(
            set(payload["by_route"][0]), {"route", "requests", "avg_ms", "max_ms"}
        )
        self.assertEqual(
            set(payload["by_user"][0]),
            {"username", "user_id", "requests", "avg_ms", "max_ms"},
        )
        self.assertEqual(
            set(payload["slowest"][0]),
            {"occurred_at", "route", "method", "status_code", "username", "duration_ms"},
        )


# ══════════════════════════════════════════════════════════════════════════
# 判据 ①：非管理员 403
# ══════════════════════════════════════════════════════════════════════════
class PermissionTests(_MetricsCase):
    def test_1_signed_in_non_admin_gets_403(self):
        """已登录、但**不是**账号管理员 ⇒ 403（不是 200，也不是 404）。"""
        non_admin = make_logged_in_client(username="测试普通客服")
        response = non_admin.get(URL)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], 403)
        self.assertIsNone(response.data["data"])

    def test_1b_anonymous_is_401_not_403(self):
        """对照组：匿名走 `NotAuthenticated` ⇒ 401（与"登录了但没权限"可区分）。"""
        response = Client(SERVER_NAME=DOMAIN).get(URL)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], 401)

    def test_1c_admin_gets_200(self):
        """对照组的另一侧：账号管理员 ⇒ 200 + 成功信封。"""
        response = self.get({})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], int(ErrorCode.OK))


# ══════════════════════════════════════════════════════════════════════════
# 判据 ②：管理员 200 且 `summary.requests` == 真实请求数
# ══════════════════════════════════════════════════════════════════════════
class SummaryCountsRealRequestsTests(_MetricsCase):
    def test_2_summary_requests_equals_the_real_number_of_requests(self):
        """发 **3 次真实请求**（不造夹具），聚合端点必须回 `requests == 3`。

        ⚠️ 这 3 次打到 `/api/auth/me`，同人同接口同整点 ⇒ Task 1 的 UPSERT 会合并成
        **一行**（`count=3`）⇒ 这条判据同时钉住「求和口径」而不是"数行数"。
        """
        for _ in range(3):
            self.assertEqual(self.client.get(URL_ME).status_code, 200)

        payload = self.payload({})

        self.assertEqual(payload["summary"]["requests"], 3)
        by_route = {row["route"]: row for row in payload["by_route"]}
        self.assertIn(ROUTE_B, by_route)
        self.assertEqual(by_route[ROUTE_B]["requests"], 3)
        # 求和口径的**反向**证据：表里确实只有一行（合并过），不是"三行加起来"。
        self.assertEqual(
            RequestMetric.objects.filter(
                route=ROUTE_B, kind=RequestMetricKind.AGG
            ).count(),
            1,
        )


# ══════════════════════════════════════════════════════════════════════════
# 判据 ③：按接口 / 按人聚合的排序与求和
# ══════════════════════════════════════════════════════════════════════════
class AggregationTests(_MetricsCase):
    """夹具（两个接口 × 两个人，全部落在今天，`bucket_start` = 今天 01:00）：

    | 接口 | 人 | count | total_ms | max_ms | 直方图桶 |
    |---|---|---|---|---|---|
    | A | 甲 | 5 | 500 | 300 | `<500ms` |
    | A | 乙 | 1 | 900 | 900 | `<1000ms` |
    | B | 甲 | 2 | 40  | 25  | `<100ms` |

    ⇒ `summary`：requests=8、total=1440、avg=180、max=900、hist=[2,0,5,1,0,0]
    ⇒ `by_route`：A(6, avg 233, max 900) 在前，B(2, avg 20, max 25) 在后
    ⇒ `by_user` ：甲(7, avg 77, max **300**) 在前，乙(1, avg 900, max 900) 在后
      （甲的两行 max 是 300 与 25 ⇒ 取 300；900 那一行是**乙**的，别把 summary 的
       `max_ms` 抄到甲身上——这正是"聚合要按分组算"的判据所在）
    """

    def setUp(self):
        super().setUp()
        day = timezone.localdate()
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 1), count=5,
            total_ms=500, max_ms=300, hist_field="hist_lt500",
        )
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 1), count=1,
            total_ms=900, max_ms=900, hist_field="hist_lt1000",
            user_id=USER_B_ID, username=USER_B,
        )
        make_metric(
            route=ROUTE_B, bucket_start=bucket(day, 1), count=2,
            total_ms=40, max_ms=25, hist_field="hist_lt100",
        )

    def test_3_summary_sums_match_the_fixture(self):
        payload = self.payload({})

        self.assertEqual(payload["summary"]["requests"], 8)
        self.assertEqual(payload["summary"]["avg_ms"], 180)
        self.assertEqual(payload["summary"]["max_ms"], 900)
        self.assertEqual(payload["summary"]["slow_requests"], 0)
        self.assertEqual(
            payload["summary"]["hist"],
            [
                {"label": "<100ms", "count": 2},
                {"label": "<200ms", "count": 0},
                {"label": "<500ms", "count": 5},
                {"label": "<1000ms", "count": 1},
                {"label": "<3000ms", "count": 0},
                {"label": ">=3000ms", "count": 0},
            ],
        )
        # 六个桶的总和 == 请求数（直方图是"每个请求恰好进一个桶"，不是另一套口径）。
        self.assertEqual(
            sum(row["count"] for row in payload["summary"]["hist"]), 8
        )

    def test_3_by_route_is_ordered_by_requests_and_sums_agree(self):
        payload = self.payload({})

        self.assertEqual(
            payload["by_route"],
            [
                {"route": ROUTE_A, "requests": 6, "avg_ms": 233, "max_ms": 900},
                {"route": ROUTE_B, "requests": 2, "avg_ms": 20, "max_ms": 25},
            ],
        )

    def test_3_by_user_is_ordered_by_requests_and_sums_agree(self):
        payload = self.payload({})

        self.assertEqual(
            payload["by_user"],
            [
                {
                    "username": USER_A, "user_id": USER_A_ID,
                    "requests": 7, "avg_ms": 77, "max_ms": 300,
                },
                {
                    "username": USER_B, "user_id": USER_B_ID,
                    "requests": 1, "avg_ms": 900, "max_ms": 900,
                },
            ],
        )

    def test_3c_route_and_username_filters_narrow_the_aggregates(self):
        """契约里的另外两个参数（前端今天没实现，但 §4 要求支持）。"""
        by_route = self.payload({"route": ROUTE_A})["summary"]
        by_user = self.payload({"username": USER_B})["summary"]

        # 接口 A：5 + 1 = 6；接口 B 的 2 条被筛掉。
        self.assertEqual(by_route["requests"], 6)
        # 人乙：只有 1 条。
        self.assertEqual(by_user["requests"], 1)
        self.assertEqual(by_user["max_ms"], 900)

    def test_3d_top_n_is_fifty_and_keeps_the_biggest(self):
        """`by_route` 取前 **50** 条（按请求数倒序）—— 多造 5 条，断言恰好回 50 条。

        ⚠️ **本轮 m-7 修复**：原先整条由 `TOP_N` 派生（`range(TOP_N + 5)`、`extras[:TOP_N - 2]`）
        ⇒ 把 `TOP_N` 改成 20 也**不会红**，"契约数字 50"其实是**没有网**的。这里全部用字面量
        `50`（spec §4 冻结的分页数），`TOP_N` 一漂就当场红。
        """
        day = timezone.localdate()
        for index in range(55):
            make_metric(
                route=f"api/business/extra/{index:03d}/",
                bucket_start=bucket(day, 2, minute=index % 60),
                count=1,
                total_ms=1,
                max_ms=1,
            )

        payload = self.payload({})

        returned = [row["route"] for row in payload["by_route"]]
        self.assertEqual(len(returned), 50, "spec §4：`by_route` 前 50 条（字面量契约）")
        # 夹具里最大的一条是 6 条请求的接口 A、第二是 2 条请求的接口 B ⇒ 两条都必须在
        # （截断截掉的是 count=1 的小头）。剩下的 48 格按接口名升序，位置确定。
        extras = sorted(f"api/business/extra/{index:03d}/" for index in range(55))
        self.assertEqual(returned, [ROUTE_A, ROUTE_B, *extras[:48]])


# ══════════════════════════════════════════════════════════════════════════
# 判据 ④：`slow_only=true` 只统计 slow 行
# ══════════════════════════════════════════════════════════════════════════
class SlowOnlyTests(_MetricsCase):
    """夹具：agg 行 `count=3`，另加 **2 条** slow 行（1200ms / 800ms）。

    ⚠️ 这条判据**必须两面试**（`slow_only` 缺省 + `slow_only=true`）：只测
    `slow_only=true` 时，"把真值判断改成恒真"这种变异照样绿（恒真也是只统计 slow）
    ⇒ 那是一个**没有判别力**的用例。缺省那一半才是能红的那一半。
    """

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate()
        make_metric(
            route=ROUTE_A, bucket_start=bucket(self.day, 1), count=3,
            total_ms=300, max_ms=200, hist_field="hist_lt500",
        )
        make_metric(
            route=ROUTE_A, bucket_start=bucket(self.day, 12), kind=RequestMetricKind.SLOW,
            count=1, total_ms=1200, max_ms=1200, hist_field="hist_lt3000",
        )
        make_metric(
            route=ROUTE_A, bucket_start=bucket(self.day, 12, minute=5),
            kind=RequestMetricKind.SLOW, count=1, total_ms=800, max_ms=800,
            hist_field="hist_lt1000",
        )

    def test_4_default_counts_only_agg_rows(self):
        payload = self.payload({})

        self.assertEqual(payload["summary"]["requests"], 3)
        self.assertEqual(payload["summary"]["max_ms"], 200)
        self.assertEqual(payload["summary"]["slow_requests"], 2)
        self.assertEqual(
            payload["by_route"],
            [{"route": ROUTE_A, "requests": 3, "avg_ms": 100, "max_ms": 200}],
        )
        # `slowest` 恒取 slow 行（与 `slow_only` 无关）⇒ 缺省也有两条。
        self.assertEqual(len(payload["slowest"]), 2)

    def test_4_slow_only_counts_only_slow_rows(self):
        payload = self.payload({"slow_only": "true"})

        self.assertEqual(payload["summary"]["requests"], 2)
        self.assertEqual(payload["summary"]["slow_requests"], 2)
        self.assertEqual(payload["summary"]["avg_ms"], 1000)
        self.assertEqual(payload["summary"]["max_ms"], 1200)
        # agg 行的 3 条**必须**消失（值断言，不是"两个字段都在"那种弱判据）。
        self.assertEqual(
            payload["by_route"],
            [{"route": ROUTE_A, "requests": 2, "avg_ms": 1000, "max_ms": 1200}],
        )
        self.assertEqual(
            [row["duration_ms"] for row in payload["slowest"]], [1200, 800]
        )

    def test_4b_slowest_is_ordered_by_duration_and_carries_the_contract_keys(self):
        payload = self.payload({})

        slowest = payload["slowest"]
        self.assertEqual([row["duration_ms"] for row in slowest], [1200, 800])
        self.assertEqual(
            set(slowest[0]),
            {"occurred_at", "route", "method", "status_code", "username", "duration_ms"},
        )
        self.assertEqual(slowest[0]["route"], ROUTE_A)
        self.assertEqual(slowest[0]["method"], "GET")
        self.assertEqual(slowest[0]["status_code"], 200)
        self.assertEqual(slowest[0]["username"], USER_A)
        # `occurred_at` 是**本地**时区下的那一格夹具时刻（不是 UTC 的 `+00:00`：
        # 直接 `isoformat()` 一个库里读出的 aware datetime 会回 UTC，比用户看到的早 8 小时）。
        self.assertEqual(
            slowest[0]["occurred_at"], bucket(self.day, 12).isoformat()
        )


# ══════════════════════════════════════════════════════════════════════════
# 判据 ⑤：畸形日期回 1000 而不是 500
# ══════════════════════════════════════════════════════════════════════════
class MalformedDateTests(_MetricsCase):
    """判据是「**这类请求**还会不会 500」而不是「这个字段还会不会 500」（E-0010）：
    所以语料里除日期外还有另外三个参数的畸形值（`slow_only` / `route` / `username`）。
    """

    MALFORMED = (
        {"date_from": "garbage"},
        {"date_to": "garbage"},
        # 前端注释点名的形状（laydate 点选的一定是 `yyyy-MM-dd`，手输可能是这种）。
        {"date_from": "2026/10/04"},
        {"date_from": "2026-13-45"},
        {"date_to": "2026-02-30"},
        # 带时分秒（前端不会发，但契约只认日期 ⇒ 必须当场拒，不能当成合法值去查库）。
        {"date_from": "2026-10-04T00:00:00"},
        # 其余三个参数的每类畸形值。
        {"slow_only": "maybe"},
        {"route": "x" * 300},
        {"username": "x" * 200},
    )

    def test_5_malformed_dates_return_business_code_1000_not_500(self):
        for params in self.MALFORMED:
            with self.subTest(params=params):
                response = self.client.get(URL, params)

                self.assertNotEqual(response.status_code, 500)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["code"], int(ErrorCode.VALIDATION))
                self.assertIsNone(response.data["data"])
                # 「不是 HTML 500」按**响应体**再钉一次（状态码相同也可能回错东西）。
                self.assertNotIn(b"<html", response.content.lower())

    def test_5b_well_formed_dates_are_accepted(self):
        """对照组：同一条路径上，合法的日期**不会**被这条校验挡住（否则上一条恒真）。"""
        payload = self.payload({"date_from": "2026-10-01", "date_to": "2026-10-04"})

        self.assertEqual(
            payload["range"], {"date_from": "2026-10-01", "date_to": "2026-10-04"}
        )


# ══════════════════════════════════════════════════════════════════════════
# 判据 ⑥：不传时间 ⇒ 默认最近 7 天
# ══════════════════════════════════════════════════════════════════════════
class DefaultWindowTests(_MetricsCase):
    """夹具：**第 7 天**（= 今天往前 6 天，默认窗的**最后一格**）、
    **第 8 天**（= 今天往前 7 天，"8 天解释"才会包含的那一天）、**第 9 天**（更早）各一行。

    ⚠️ 判别力全在「第 8 天」这一行上：默认窗写成 `today - 7`（8 个自然日）时它会被算进来
    ⇒ 本用例必须红。「第 9 天」那行钉的是"默认窗不是全表"，但它**分辨不出** 7 天与 8 天
    两种解释（两种都不含它）。
    """

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.seventh_day = self.today - dt.timedelta(days=6)
        self.eighth_day = self.today - dt.timedelta(days=7)
        self.ninth_day = self.today - dt.timedelta(days=8)
        make_metric(route=ROUTE_SEVENTH_DAY, bucket_start=bucket(self.seventh_day))
        make_metric(route=ROUTE_EIGHTH_DAY, bucket_start=bucket(self.eighth_day))
        make_metric(route=ROUTE_NINTH_DAY, bucket_start=bucket(self.ninth_day))

    def test_6_default_window_is_today_plus_the_previous_six_days(self):
        payload = self.payload({})

        self.assertEqual(
            payload["range"],
            {
                "date_from": self.seventh_day.isoformat(),
                "date_to": self.today.isoformat(),
            },
        )
        # 窗口天数是 7 个自然日（含今天）：`date_from` 恰好是"今天往前 6 天"那一格。
        # ⚠️ 上面那条 `range` 等值断言（不是"天数 == 7"这种常量断言）就是这条判据的主体：
        #    默认窗写成 `today - 7`（8 个自然日）时它会红。
        # **第 8 天那一行必须不在**（默认窗不是 8 天），第 9 天也不在（不是全表），
        # 只有第 7 天那一行在（默认窗的最后一格确实算数）。
        self.assertEqual(payload["summary"]["requests"], 1)
        self.assertEqual(
            [row["route"] for row in payload["by_route"]], [ROUTE_SEVENTH_DAY]
        )

    def test_6b_explicit_window_still_includes_the_eighth_day(self):
        """对照组：**显式**把窗口放宽到第 8 天 ⇒ 那一行必须出现。

        ⚠️ 没有这一条，上面「第 8 天不在结果里」可能只是"那行根本查不到"
        （夹具写错 / 路由筛错），而不是"默认窗把它挡在外面"（E-0004 家族：否定性结论
        要有对照）。这里同时证明那行**是可见的**、且**默认窗挡住了它**。
        """
        payload = self.payload(
            {
                "date_from": self.eighth_day.isoformat(),
                "date_to": self.today.isoformat(),
            }
        )

        self.assertEqual(payload["summary"]["requests"], 2)
        self.assertEqual(
            sorted(row["route"] for row in payload["by_route"]),
            sorted([ROUTE_SEVENTH_DAY, ROUTE_EIGHTH_DAY]),
        )
        self.assertNotIn(
            ROUTE_NINTH_DAY, [row["route"] for row in payload["by_route"]]
        )


# ══════════════════════════════════════════════════════════════════════════
# 只读边界：本端点不接受任何写方法（spec §4「⛔ 只读」，D3 同款边界）
# ══════════════════════════════════════════════════════════════════════════
class ReadOnlyTests(_MetricsCase):
    def test_no_write_method_is_accepted(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(URL)
                self.assertEqual(response.status_code, 405, response.content[:200])

    def test_there_is_no_export_endpoint(self):
        for path in (f"{URL}export/", "/api/system/request-metrics/export"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class UserIdIsVisibleForRenamedAccountsTests(_MetricsCase):
    """`by_user` 的 `user_id` 是契约的一部分（前端 `personLabel(username, userId)`）。

    ⚠️ 未登录行的 `username` 是**空串**、`user_id` 是 `0`（spec §2）⇒ 这个字段不能被
    "取整成 0"之外的处理吃掉；这里顺带钉住"改了账号名之后两行分得开"。
    """

    def test_by_user_keeps_user_id_and_separates_anonymous_rows(self):
        day = timezone.localdate()
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 3), count=2,
            user_id=0, username="", total_ms=20, max_ms=15,
        )
        make_metric(
            route=ROUTE_A, bucket_start=bucket(day, 3), count=1,
            user_id=USER_A_ID, username=USER_A, total_ms=10, max_ms=10,
        )

        payload = self.payload({})

        self.assertEqual(
            payload["by_user"],
            [
                {
                    "username": "", "user_id": 0,
                    "requests": 2, "avg_ms": 10, "max_ms": 15,
                },
                {
                    "username": USER_A, "user_id": USER_A_ID,
                    "requests": 1, "avg_ms": 10, "max_ms": 10,
                },
            ],
        )


class EndpointExcludesItselfTests(_MetricsCase):
    """判据 M-3（本轮修复）：**端点自己那一行不进聚合**——"看数据"这件事不许改数据。

    评审现场：`by_route` 里有一行 `{"route": "api/system/request-metrics/", "requests": 7, …}`
    ——页面每刷一次，它自己那一行的 `count` 就大一点（自我指涉的污染）。

    ⚠️ 判据必须**两半都在**：① 表里**确实有**这个 route 的行（证明是"排除"而不是"没埋点"，
    否则下面的空集断言恒真）；② 返回载荷里**一处都不含**它。
    """

    def test_endpoint_route_is_excluded_from_aggregates_but_stays_in_the_table(self):
        day = timezone.localdate()
        # 夹具：端点自己的 agg 行 + 一条端点自己的 slow 行（后者用来判 `slowest`）。
        make_metric(
            route=ENDPOINT_ROUTE, bucket_start=bucket(day, 5), count=9,
            total_ms=900, max_ms=900, hist_field="hist_lt1000",
        )
        make_metric(
            route=ENDPOINT_ROUTE, bucket_start=bucket(day, 5),
            kind=RequestMetricKind.SLOW, count=1, total_ms=900, max_ms=900,
            hist_field="hist_lt1000",
        )
        # ① 对照：这两行**确实**在表里（⛔ 不是"从来没埋点"⇒ 判据不恒真）。
        self.assertEqual(
            RequestMetric.objects.filter(route=ENDPOINT_ROUTE).count(), 2
        )

        # ② 真打一次端点：这一次请求自己也会写一行（在响应之后），但载荷必须**一处都不含**它。
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        data = response.data["data"]

        self.assertEqual(data["summary"]["requests"], 0, data["by_route"])
        self.assertEqual(data["summary"]["slow_requests"], 0)
        self.assertEqual(data["by_route"], [])
        self.assertEqual(data["by_user"], [])
        self.assertEqual(data["slowest"], [])
        self.assertNotIn(ENDPOINT_ROUTE, response.content.decode("utf-8"))

        # **连打第二次**（评审现场就是"连打两次"）：这一发自己又写了一行 ⇒ 第二次载荷
        # 面对的端点自身行更多，但同样必须一处都不含它。
        second = self.client.get(URL)
        self.assertEqual(second.status_code, 200, second.content[:300])
        second_data = second.data["data"]
        self.assertEqual(second_data["summary"]["requests"], 0, second_data["by_route"])
        self.assertEqual(second_data["by_route"], [])
        self.assertEqual(second_data["slowest"], [])
        self.assertNotIn(ENDPOINT_ROUTE, second.content.decode("utf-8"))

        # 埋点本身没被关掉：这两次真实请求**折进端点自己那行 agg**（UPSERT 会合并
        # 同人同接口同整点的请求）⇒ 端点 AGG 行的 `count` 之和 = 夹具 9 + 两次 = **11**。
        # ⚠️ 不看"行数"：两次真实请求合并成**一行**，行数还取决于跑测试的整点与夹具整点是否相同。
        endpoint_agg = RequestMetric.objects.filter(
            route=ENDPOINT_ROUTE, kind=RequestMetricKind.AGG
        )
        self.assertEqual(
            sum(row.count for row in endpoint_agg),
            11,
            "两次真实请求没有折进端点自己那行（埋点被关掉了？）",
        )
