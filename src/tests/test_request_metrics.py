"""Task 1 判据：请求耗时埋点（模型 + 迁移 + 中间件 + settings + 白名单）。

判据出处：`docs/plans/2026-10-04-request-metrics.md` §6「Task 1」的六条。
⚠️ 全部断言打在**数据库行**上（`AGENTS.md` §5-G）：⛔ 不断言 `logger.info`，
`logger.exception` 那条也只作为**次要**证据（主证据是「表里多没多一行」）。
"""
import datetime as dt
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.test import Client, SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.audit.metrics import HIST_FIELDS, UNIQUE_FIELDS
from apps.audit.models import RequestMetric, RequestMetricKind
from apps.core.request_metrics import EXEMPT_PATH_PREFIXES, RequestMetricsMiddleware
from tests.p5_auth_helpers import P2_TEST_USERNAME, make_logged_in_client

#: `TenantTestCase` 自带的租户域名（`django_tenants/test/cases.py:83` 的
#: `get_test_tenant_domain()` 返回值；本轮 m-9 当场读码重取——原写 `:76` 是错的，
#: 而该路径在 `tests/test_doc_line_references.py` 的跳过清单里，行号错了那道网抓不到）。
DOMAIN = "tenant.test.com"
#: 判据用的接口：登录态 200、匿名 401，路由模板无参数 ⇒ `route` 每次都一样。
URL = "/api/auth/me"
ROUTE = "api/auth/me"

#: 钉死的「整点内时刻」：让「两次同整点」这条判据不依赖跑测试的钟点
#: （否则在整点前后那一瞬间跑，两次请求会落进两个桶——那是判据自己不稳，不是产品错）。
FROZEN_NOW = dt.datetime(2026, 10, 4, 5, 30, tzinfo=dt.timezone.utc)

#: **列清单**（`RequestMetric` 的全部具体列）。加列/改列必须同一次改这里（`AGENTS.md` §5-G）。
REQUEST_METRIC_COLUMNS = {
    "id",
    "kind",
    "bucket_start",
    "route",
    "method",
    "status_code",
    "user_id",
    "username",
    "count",
    "total_ms",
    "max_ms",
    "hist_lt100",
    "hist_lt200",
    "hist_lt500",
    "hist_lt1000",
    "hist_lt3000",
    "hist_gte3000",
    "created_at",
    "updated_at",
}


def histogram(row):
    """把六个直方图桶读成 dict（只读不写，便于整体比对）。"""
    return {name: getattr(row, name) for name in HIST_FIELDS}


class RequestMetricStructureTests(SimpleTestCase):
    """结构网：模型列 / 直方图列 / 唯一键必须与写入原语里那三组字面量一致。

    ⚠️ 这些断言**不是**自指套自指：右边是写死在测试里的字面量，左边从模型取
    ⇒ 改模型（改窄 `username`、把 `user_id` 改成 `null=True`、动直方图列）会让它红。
    """

    def test_columns_are_exactly_the_pinned_set(self):
        self.assertEqual(
            {field.name for field in RequestMetric._meta.fields},
            REQUEST_METRIC_COLUMNS,
        )

    def test_histogram_columns_match_the_write_primitive(self):
        """`metrics.HIST_FIELDS` 决定原始 SQL 的列名与顺序 —— 与模型漂移会写错桶。"""
        model_hist = tuple(
            field.name
            for field in RequestMetric._meta.fields
            if field.name.startswith("hist_")
        )
        self.assertEqual(model_hist, HIST_FIELDS)
        self.assertEqual(len(HIST_FIELDS), 6)

    def test_unique_constraint_columns_match_the_conflict_target(self):
        """`ON CONFLICT (…)` 的推断目标必须与模型上的唯一约束**同一组列**。"""
        constraints = [
            c
            for c in RequestMetric._meta.constraints
            if isinstance(c, models.UniqueConstraint)
        ]
        self.assertEqual(len(constraints), 1, constraints)
        self.assertEqual(tuple(constraints[0].fields), UNIQUE_FIELDS)

    def test_user_id_is_not_nullable(self):
        """spec §2 的 `NULL` 陷阱：唯一约束里 `NULL` 互不相等 ⇒ 匿名行不会合并。"""
        field = RequestMetric._meta.get_field("user_id")
        self.assertFalse(field.null)
        self.assertEqual(field.default, 0)

    def test_username_column_is_as_wide_as_auth_user_username(self):
        """列宽 150 = `auth_user.username` 的列宽（先例：`OperationLog.actor` 64 → 150）。"""
        self.assertEqual(RequestMetric._meta.get_field("username").max_length, 150)

    def test_bucket_start_is_indexed(self):
        self.assertTrue(RequestMetric._meta.get_field("bucket_start").db_index)

    def test_settings_defaults_are_on_and_500ms(self):
        self.assertIs(settings.REQUEST_METRICS_ENABLED, True)
        self.assertEqual(settings.REQUEST_METRICS_SLOW_MS, 500)

    def test_middleware_is_wired_after_authentication(self):
        """spec §5：位置只要在 `AuthenticationMiddleware` **之后**就算量到了全链路。

        ⚠️ **口径（本轮 m-8 修复）**：位置**只**为了一件事——让 `get_response()` 里包含认证
        （以及更外层的租户解析 / 会话 / CSRF）的耗时。⛔ 不断言它是 `MIDDLEWARE[-1]`：
        后来者**正常地**在末尾再加一个中间件时，那种断言会**误导性地红**
        （它挡住的不是缺陷，而是一次合法改动）。
        """
        path = "apps.core.request_metrics.RequestMetricsMiddleware"
        middleware = list(settings.MIDDLEWARE)
        self.assertIn(path, middleware)
        self.assertLess(
            middleware.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
            middleware.index(path),
            "埋点中间件排在 AuthenticationMiddleware 之前 ⇒ 量不到认证的耗时",
        )

    def test_whitelist_covers_the_required_paths(self):
        """spec §2 的最低白名单（判据的主要出口是下面的『一条都不写』用例）。"""
        for path in ("/healthz", "/static/", "/media/", "/favicon.ico"):
            self.assertIn(path, EXEMPT_PATH_PREFIXES)


class RequestMetricWriteTests(TenantTestCase):
    """六条判据（除第 5 条的例外分支外都在这里）。"""

    def setUp(self):
        # 真实登录（`tests/p5_auth_helpers.py`）：登录那次 POST 自己也会被埋点
        # ⇒ 先清空，判据才有「从零开始」的读数。
        self.client = make_logged_in_client()
        RequestMetric.objects.all().delete()
        self.user_id = (
            get_user_model().objects.get(username=P2_TEST_USERNAME).pk
        )

    # ── 只读工具 ─────────────────────────────────────────────────
    def rows(self, kind=None):
        queryset = RequestMetric.objects.all()
        if kind is not None:
            queryset = queryset.filter(kind=kind)
        return list(queryset.order_by("kind", "id"))

    def get(self, duration_ms, *, path=URL, client=None, now=None):
        """发一次请求，并让中间件在这**一次**请求里量到 `duration_ms`（两次时钟调用）。

        `now` 非空时同时把 `timezone.now()` 钉死 —— 用于「同整点」类判据。
        """
        clock = iter([1000.0, 1000.0 + duration_ms / 1000.0])
        with mock.patch("apps.core.request_metrics.perf_counter", side_effect=clock):
            if now is None:
                return (client or self.client).get(path)
            with mock.patch(
                "apps.core.request_metrics.timezone.now", return_value=now
            ):
                return (client or self.client).get(path)

    # ── 判据 1：一次请求 ⇒ 一条 agg 行，字段值对得上 ──────────────
    def test_1_one_request_writes_one_agg_row_with_matching_values(self):
        response = self.get(42)

        self.assertEqual(response.status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.kind, RequestMetricKind.AGG)
        self.assertEqual(row.route, ROUTE)
        self.assertEqual(row.method, "GET")
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.user_id, self.user_id)
        self.assertEqual(row.username, P2_TEST_USERNAME)
        self.assertEqual(row.count, 1)
        self.assertEqual(row.total_ms, 42)
        self.assertEqual(row.max_ms, 42)
        self.assertEqual(
            histogram(row),
            {
                "hist_lt100": 1,
                "hist_lt200": 0,
                "hist_lt500": 0,
                "hist_lt1000": 0,
                "hist_lt3000": 0,
                "hist_gte3000": 0,
            },
        )
        # `bucket_start` 是**整点**（分/秒/微秒都是 0）。
        self.assertEqual(
            (row.bucket_start.minute, row.bucket_start.second, row.bucket_start.microsecond),
            (0, 0, 0),
        )

    # ── 判据 1b–1d：解析不到路由 ⇒ 固定哨兵（⛔ 不落真实路径）────
    def test_1b_unresolved_route_is_a_fixed_sentinel(self):
        """解析不到 `resolver_match.route`（404 就是这种情形）⇒ `route` 是固定哨兵。

        ⚠️ 本条钉的是**旧实现的那个洞**（本轮 I-1）：原先回落 `request.path`，
        于是「互不相同的 404 路径」会把表撑成无界、且真实路径（可能带令牌）会入库 90 天。
        """
        response = self.get(10, path="/no-such-endpoint/abc123")

        self.assertEqual(response.status_code, 404)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].route, "<unresolved>")
        self.assertEqual(rows[0].status_code, 404)

    def test_1c_twenty_distinct_404_paths_merge_into_one_sentinel_row(self):
        """**20 个互不相同**的 404 路径、同一整点同一身份 ⇒ 表里只有 **1** 行。

        ⚠️ 判别力：旧实现（回落真实路径）下这里是 **20 行** ⇒ 这条用例当场红。
        """
        for index in range(20):
            response = self.get(
                10, path=f"/no-such-endpoint/{index:03d}", now=FROZEN_NOW
            )
            self.assertEqual(response.status_code, 404)

        rows = self.rows()
        self.assertEqual(len(rows), 1, f"404 路径没有归一（表基数无界）：{rows}")
        self.assertEqual(rows[0].route, "<unresolved>")
        self.assertEqual(rows[0].count, 20)

    def test_1d_a_token_in_a_404_path_never_reaches_any_column(self):
        """带令牌的 404 路径 ⇒ **整行所有列**都不含那个串（⛔ 不是只看 `route`）。"""
        token = "SECRET-TOKEN-abc123"
        response = self.get(10, path=f"/no-such-endpoint/{token}")

        self.assertEqual(response.status_code, 404)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        for row in rows:
            for field in RequestMetric._meta.fields:
                self.assertNotIn(
                    token,
                    str(getattr(row, field.name)),
                    f"{field.name} 里出现了 404 路径里的令牌串",
                )

    # ── 判据 2（核心）：两次同接口同人同整点 ⇒ 仍然只有一行 ───────
    def test_2_two_requests_in_the_same_hour_merge_into_one_row(self):
        first = self.get(150, now=FROZEN_NOW)
        second = self.get(250, now=FROZEN_NOW)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"第二次请求没有被 UPSERT 合并：{rows}")
        row = rows[0]
        self.assertEqual(row.count, 2)
        self.assertEqual(row.total_ms, 400)
        self.assertEqual(row.max_ms, 250)
        self.assertEqual(
            histogram(row),
            {
                "hist_lt100": 0,
                "hist_lt200": 1,  # 150ms
                "hist_lt500": 1,  # 250ms
                "hist_lt1000": 0,
                "hist_lt3000": 0,
                "hist_gte3000": 0,
            },
        )
        # 两次都没超阈值 ⇒ 没有 slow 行（顺带证明第二行不是"另一条 agg"）。
        self.assertEqual(self.rows(kind=RequestMetricKind.SLOW), [])

    def test_2b_max_ms_keeps_the_greater_duration_when_the_second_is_smaller(self):
        """**承重算术**：同一整点先写 250ms、再写 150ms ⇒ `max_ms` 必须仍是 **250**。

        ⚠️ 判别力（本轮 I-2 补）：旧夹具要么只写一次、要么耗时**单调递增**
        （`test_2` 是 150 → 250）⇒ 把 `max_ms = GREATEST(...)` 变异成「末次覆盖」
        （`= EXCLUDED."max_ms"`）时，本批 52 条用例**全绿**（评审实测）。
        本条就是那条算术的判据：先大后小，末次覆盖会得到 150。
        """
        first = self.get(250, now=FROZEN_NOW)
        second = self.get(150, now=FROZEN_NOW)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"两次请求没有被 UPSERT 合并：{rows}")
        row = rows[0]
        self.assertEqual(row.count, 2)
        self.assertEqual(row.total_ms, 400)
        # ⛔ 不是 150：`max_ms` 取两次里**更大的那个**（GREATEST），不是末次覆盖。
        self.assertEqual(row.max_ms, 250)
        # 两次落在**不同**的直方图桶（250 → `hist_lt500`；150 → `hist_lt200`）⇒ 各 +1。
        self.assertEqual(
            histogram(row),
            {
                "hist_lt100": 0,
                "hist_lt200": 1,  # 150ms
                "hist_lt500": 1,  # 250ms
                "hist_lt1000": 0,
                "hist_lt3000": 0,
                "hist_gte3000": 0,
            },
        )
        self.assertEqual(self.rows(kind=RequestMetricKind.SLOW), [])

    # ── 判据 3：超阈值有额外 slow 行；未超没有 ────────────────────
    def test_3_over_threshold_request_adds_an_extra_slow_row(self):
        from django.utils import timezone

        response = self.get(800)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows()), 2)
        agg = self.rows(RequestMetricKind.AGG)
        slow = self.rows(RequestMetricKind.SLOW)
        self.assertEqual(len(agg), 1)
        self.assertEqual(len(slow), 1)
        self.assertEqual(agg[0].count, 1)
        self.assertEqual(agg[0].total_ms, 800)
        self.assertEqual(agg[0].max_ms, 800)
        # slow 行：那一次的精确耗时，`count=1`、直方图只置对应那一个桶。
        self.assertEqual(slow[0].count, 1)
        self.assertEqual(slow[0].total_ms, 800)
        self.assertEqual(slow[0].max_ms, 800)
        self.assertEqual(histogram(slow[0])["hist_lt1000"], 1)
        self.assertEqual(sum(histogram(slow[0]).values()), 1)
        self.assertEqual(slow[0].route, ROUTE)
        self.assertEqual(slow[0].user_id, self.user_id)
        # slow 行的 `bucket_start` 是**那一次请求的时刻**（不是整点）。
        self.assertLessEqual(slow[0].bucket_start, timezone.now())
        self.assertNotEqual(
            slow[0].bucket_start,
            slow[0].bucket_start.replace(minute=0, second=0, microsecond=0),
            "slow 行不该被截到整点",
        )

    def test_3b_at_or_below_threshold_writes_no_slow_row(self):
        """阈值口径是**严格大于**（spec §2「耗时 > 阈值」）⇒ 恰好等于 500ms 不写 slow。"""
        for duration_ms in (0, 499, 500):
            with self.subTest(duration_ms=duration_ms):
                RequestMetric.objects.all().delete()
                self.get(duration_ms)
                self.assertEqual(self.rows(RequestMetricKind.SLOW), [])

    def test_3c_two_slow_rows_at_the_same_instant_merge(self):
        """如实登记的那条语义（本轮 m-10 改准）：两条 `bucket_start` **完全相同**的慢请求会合并。

        ⚠️ 旧注释写「同一微秒实际不会发生」——那是**绝对句**、且能被一个反例证伪（评审实测）。
        这里把两件事都钉住：`slow` 行**会**合并，且合并口径与 `agg` 一致
        （`count` 累加、`total_ms` 相加、`max_ms` 取大者）。
        """
        first = self.get(800, now=FROZEN_NOW)
        second = self.get(900, now=FROZEN_NOW)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        slow = self.rows(kind=RequestMetricKind.SLOW)
        self.assertEqual(len(slow), 1, f"同一时刻的两条慢请求本该合并：{slow}")
        self.assertEqual(slow[0].count, 2)
        self.assertEqual(slow[0].total_ms, 1700)
        self.assertEqual(slow[0].max_ms, 900)

    # ── 判据 4：白名单路径一条都不写 ─────────────────────────────
    def test_4_whitelisted_paths_write_nothing(self):
        for path in ("/healthz", "/static/app.css", "/media/x.png", "/favicon.ico"):
            with self.subTest(path=path):
                self.get(120, path=path)
                self.assertEqual(
                    RequestMetric.objects.count(),
                    0,
                    f"白名单路径 {path} 被写进了表",
                )

    # ── 判据 5 的对照 + 主测在 WriteFailureIsFailOpenTests ────────
    def test_4b_non_whitelisted_path_still_writes(self):
        """白名单判据的**对照组**：同一条请求换个非白名单路径就应当写一行。"""
        self.get(120, path="/api/auth/csrf")
        self.assertEqual(RequestMetric.objects.count(), 1)

    def test_metrics_can_be_switched_off(self):
        with self.settings(REQUEST_METRICS_ENABLED=False):
            self.get(42)
        self.assertEqual(RequestMetric.objects.count(), 0)

    # ── 判据 6：未登录 ⇒ user_id == 0、username == ""，两次合并一行 ─
    def test_6_anonymous_requests_use_user_id_zero_and_merge(self):
        anonymous = Client(SERVER_NAME=DOMAIN)
        first = self.get(30, client=anonymous, now=FROZEN_NOW)
        second = self.get(40, client=anonymous, now=FROZEN_NOW)

        self.assertEqual(first.status_code, 401)
        self.assertEqual(second.status_code, 401)
        rows = self.rows()
        self.assertEqual(len(rows), 1, f"匿名请求没有合并（NULL 陷阱）：{rows}")
        row = rows[0]
        self.assertEqual(row.user_id, 0)
        self.assertEqual(row.username, "")
        self.assertEqual(row.count, 2)
        self.assertEqual(row.total_ms, 70)
        self.assertEqual(row.max_ms, 40)
        self.assertEqual(row.status_code, 401)


class WriteFailureIsFailOpenTests(TenantTestCase):
    """判据 5：**写库抛异常时用户请求仍然成功**（spec §5 的失败口径，承重）。

    ⚠️ `mock.patch` 打在**被查找的那个模块**上（E-0029）：中间件里写的是
    `from apps.audit.metrics import upsert_request_metric`
    ⇒ 有效的目标是 `apps.core.request_metrics.upsert_request_metric`。
    下面第二条就是「打在定义模块上」的对照 —— 它**必须**证明那个桩不生效
    （表里照旧多一行），否则第一条的「没有行」是假的。
    """

    def setUp(self):
        self.client = make_logged_in_client()
        RequestMetric.objects.all().delete()

    def _one_request(self):
        clock = iter([1000.0, 1000.5])
        with mock.patch("apps.core.request_metrics.perf_counter", side_effect=clock):
            return self.client.get(URL)

    def test_5_write_failure_keeps_the_user_request_successful(self):
        with mock.patch(
            "apps.core.request_metrics.upsert_request_metric",
            side_effect=RuntimeError("埋点写库炸了"),
        ) as patched:
            with self.assertLogs("apps.core.request_metrics", level="ERROR") as logs:
                response = self._one_request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(patched.call_count, 1, "桩没被调到 ⇒ 打的不是被查找的那个模块")
        # ⚠️ 这一条同时是「桩真的接上了」的判据：桩没接上时，真实的 UPSERT 会写下这一行。
        self.assertEqual(RequestMetric.objects.count(), 0)
        # 次要证据（不是主判据）：异常真的被记了日志，不是静默 `pass`。
        self.assertTrue(
            any("请求耗时埋点写入失败" in entry for entry in logs.output),
            logs.output,
        )

    def test_5_control_patching_the_definition_module_does_not_reach_the_middleware(self):
        """对照（E-0029）：桩打在 `apps.audit.metrics`（定义它的模块）⇒ 中间件不受影响。

        读数应当是「替身一次都没被调到 + 表里真的多了一行」——它证明
        「打在定义模块上」这种写法**没有判别力**，也反过来证明上一条的
        `count() == 0` 只有在桩接对时才成立。
        """
        with mock.patch(
            "apps.audit.metrics.upsert_request_metric",
            side_effect=RuntimeError("这个桩不该被调到"),
        ) as patched:
            response = self._one_request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(patched.call_count, 0)
        self.assertEqual(
            RequestMetric.objects.filter(kind=RequestMetricKind.AGG).count(),
            1,
            "打在定义模块上的桩竟然拦住了写库 —— 那说明中间件不是按名字查它的",
        )


class PurgeRequestMetricsCommandTests(TenantTestCase):
    """Task 4 判据：保留期清理命令 `purge_request_metrics`（spec §6 Task 4 + 任务书的四条）。

    ① 100 天前的删、10 天前的留；
    ② 再跑一次删 0 行（幂等）；
    ③ `--dry-run` 一行不删；
    ④ **先枚举再删**：枚举输出里出现命中行数；命中 0 行时就地结束、不放宽条件。

    ⚠️ 主判据一律打在**库里的行**上（`AGENTS.md` §5-G）；stdout 只作**次要证据**
    （「先枚举再删」这条要求本身是输出，所以它必须在输出上钉一次）。
    """

    def _row(self, *, age_days, route="api/auth/me", method="GET", status_code=200):
        from datetime import timedelta

        from django.utils import timezone

        return RequestMetric.objects.create(
            kind=RequestMetricKind.AGG,
            bucket_start=timezone.now() - timedelta(days=age_days),
            route=route,
            method=method,
            status_code=status_code,
            user_id=0,
            username="",
        )

    def _run(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command(
            "purge_request_metrics", *args, stdout=out, stderr=StringIO()
        )
        return out.getvalue()

    def test_1_only_rows_older_than_the_retention_window_are_deleted(self):
        from django.db import connection

        old = self._row(age_days=100)
        fresh = self._row(age_days=10)

        output = self._run("--schema", connection.schema_name)

        self.assertFalse(RequestMetric.objects.filter(pk=old.pk).exists())
        self.assertTrue(RequestMetric.objects.filter(pk=fresh.pk).exists())
        # 次要证据：枚举行打出了命中的 1 行，随后确实删了 1 行。
        self.assertIn("命中 1 行", output)
        self.assertIn("删除 1 行", output)

    def test_2_running_twice_is_idempotent_and_the_second_run_hits_zero(self):
        from django.db import connection

        old = self._row(age_days=100)
        fresh = self._row(age_days=10)

        self._run("--schema", connection.schema_name)
        output = self._run("--schema", connection.schema_name)

        self.assertFalse(RequestMetric.objects.filter(pk=old.pk).exists())
        self.assertTrue(RequestMetric.objects.filter(pk=fresh.pk).exists())
        self.assertEqual(RequestMetric.objects.count(), 1)
        # 「再跑一次删 0 行」= 枚举命中 0 行、且就地结束（⛔ 不放宽条件再试）。
        self.assertIn("命中 0 行", output)
        self.assertIn("不删除、不放宽条件", output)

    def test_3_dry_run_deletes_nothing(self):
        from django.db import connection

        old = self._row(age_days=100)

        output = self._run("--dry-run", "--schema", connection.schema_name)

        self.assertTrue(RequestMetric.objects.filter(pk=old.pk).exists())
        self.assertIn("[dry-run]", output)
        self.assertIn("命中 1 行", output)

    def test_4_deletes_in_batches_until_nothing_is_left(self):
        """`--batch-size 1` + 3 条过期行 ⇒ 全部删掉（分批循环有界且收敛）。"""
        from django.db import connection

        rows = [self._row(age_days=100 + index) for index in range(3)]
        kept = self._row(age_days=1)

        output = self._run("--schema", connection.schema_name, "--batch-size", "1")

        for row in rows:
            self.assertFalse(RequestMetric.objects.filter(pk=row.pk).exists())
        self.assertTrue(RequestMetric.objects.filter(pk=kept.pk).exists())
        self.assertIn("命中 3 行", output)
        self.assertIn("删除 3 行", output)

    def test_5_exactly_at_the_cutoff_is_kept(self):
        """边界：判据是**严格早于** cutoff ⇒ 恰好 90 天那一侧保留、早 1 微秒的删掉。"""
        from datetime import timedelta
        from unittest import mock

        from django.db import connection
        from django.utils import timezone

        fixed = timezone.now()
        kept = RequestMetric.objects.create(
            kind=RequestMetricKind.AGG,
            bucket_start=fixed - timedelta(days=90),
            route="api/auth/me",
            method="GET",
            status_code=200,
            user_id=0,
            username="",
        )
        deleted = RequestMetric.objects.create(
            kind=RequestMetricKind.AGG,
            bucket_start=fixed - timedelta(days=90, microseconds=1),
            route="api/auth/me",
            method="GET",
            status_code=200,
            user_id=0,
            username="",
        )
        with mock.patch(
            "apps.audit.management.commands.purge_request_metrics.timezone.now",
            return_value=fixed,
        ):
            self._run("--schema", connection.schema_name)

        self.assertTrue(RequestMetric.objects.filter(pk=kept.pk).exists())
        self.assertFalse(RequestMetric.objects.filter(pk=deleted.pk).exists())

    def test_6_runs_without_schema_flag_by_iterating_tenants(self):
        """⛔ 不给 `--schema` 也必须生效（命令必须自己进 `tenant_context`）。

        只查当前连接会以 `relation "audit_requestmetric" does not exist` 收场
        ⇒ 清理根本不运行（与孤儿截图清理同族）。
        """
        old = self._row(age_days=100)

        self._run()

        self.assertFalse(RequestMetric.objects.filter(pk=old.pk).exists())

    def test_7_zero_days_is_refused_loudly(self):
        """`--days 0` 会把刚写下的行也当过期（cutoff = 现在）⇒ 响亮拒绝，⛔ 不静默全删。"""
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command(
                "purge_request_metrics", "--days", "0",
                stdout=StringIO(), stderr=StringIO(),
            )


import unittest
from pathlib import Path

_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class TuantuantongReleaseFaceTests(SimpleTestCase):
    """Task 4 的部署面判据（结构网形态）：主服务单元带耗时、且**随发版**上传 / 安装 / 幂等。

    ⚠️ 证明力边界（如实）：本类只读**仓库里的文本**，回答的是「三处清单还在不在、
    `%` 有没有按 systemd 规格双写、调用顺序对不对」；它**不**回答「真机上装上了没有」
    —— 那是发版窗口的实测（`docs/STATUS.md` 登记）。
    """

    @staticmethod
    def _repo_root():
        from pathlib import Path

        # `src/tests/` 往上两级 = 仓库根。
        return Path(__file__).resolve().parents[2]

    @staticmethod
    def _logical_lines(text):
        """把 shell 的 `\\` 续行拼成逻辑行（否则「这一行里有 rsync 吗」会看错行）。"""
        joined = []
        buffer = ""
        for line in text.splitlines():
            buffer += line
            if buffer.endswith("\\"):
                buffer = buffer[:-1] + " "
                continue
            joined.append(buffer)
            buffer = ""
        if buffer:
            joined.append(buffer)
        return joined

    def _unit_text(self):
        return (self._repo_root() / "deploy" / "systemd" / "tuantuantong.service").read_text(
            encoding="utf-8"
        )

    def test_unit_writes_decimal_seconds_into_the_access_log(self):
        """gunicorn 的 `%(L)s` = 请求耗时（秒，带小数）；systemd 里必须把 `%` 双写成 `%%`。

        依据：gunicorn 的 `%(L)s` 定义在它自己的 `atoms()`（`"L": "%d.%06d" % (秒, 微秒)`）；
        而 systemd 的 `ExecStart` 命令行会做规格符展开 ⇒ 单个 `%` 会被当成规格符
        （`systemd.unit(5)` 的规格符表：`%%` = 一个百分号）⇒ 必须双写。
        """
        import re

        text = self._unit_text()
        match = re.search(r"--access-logformat\s+'([^']*)'", text)
        self.assertIsNotNone(
            match, "单元里没有 `--access-logformat '…'`（访问日志就没有耗时这一列）"
        )
        fmt = match.group(1)
        self.assertIn(
            "%(L)s", fmt.replace("%%", "%"),
            f"格式串里没有 gunicorn 的耗时占位符 %(L)s：{fmt}",
        )
        # ⛔ 判据：把成对的 `%%` 去掉后**不能再有单个 `%`**（单个 `%` 会被 systemd 当规格符）。
        self.assertEqual(
            fmt.replace("%%", "").count("%"), 0,
            f"格式串里有没双写的单个 `%`（systemd 会当规格符、单元加载失败）：{fmt}",
        )

    def test_deploy_sh_uploads_and_normalises_the_main_unit(self):
        """主单元必须进 `deploy.sh` 的**两处**：rsync 上传面 + `chmod 644 …; md5sum …` 那一面。"""
        lines = self._logical_lines(
            (self._repo_root() / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        )
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        upload_surface = "\n".join(rsync_lines)
        self.assertIn(
            "deploy/systemd/tuantuantong.service", upload_surface,
            "deploy.sh 的 rsync 源文件清单里缺主单元 ⇒ 机器上装的是旧单元",
        )
        ssh_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(ssh_lines), 1, ssh_lines)
        surface = ssh_lines[0]
        self.assertIn("$R/shared/tuantuantong.service", surface,
                      "`chmod 644 …; md5sum …` 那一面里缺主单元（文件传上去了、模式/校验没管）")

    def test_activate_sh_installs_the_main_unit_idempotently(self):
        """`activate.sh` 必须 install 主单元 + `daemon-reload`；`cmp -s` **只**决定装不装。

        （`daemon-reload` 无条件执行的口径见下一条 `…_reloads_systemd_even_when_…`。）
        """
        text = (self._repo_root() / "deploy" / "activate.sh").read_text(encoding="utf-8")
        self.assertIn('MAIN_UNIT="${SERVICE}.service"', text)
        self.assertIn('cmp -s "$MAIN_UNIT_SRC" "$MAIN_UNIT_DST"', text,
                      "没有 `cmp -s` 的幂等判据（会导致每次发版都白装一次）")
        self.assertIn('sudo install -m 644 "$MAIN_UNIT_SRC" "$MAIN_UNIT_DST"', text)
        self.assertIn("sudo systemctl daemon-reload", text,
                      "install 之后没有 daemon-reload ⇒ systemd 仍按旧单元跑")

    def test_activate_sh_reloads_systemd_even_when_the_main_unit_is_unchanged(self):
        """`daemon-reload` **无条件**执行（本轮 m-11 修复），⛔ 不放进 `cmp -s` 的 `else` 里。

        判据是**结构**而不是一句 `assertIn`：那段 `if [[ -r … ]] && cmp -s …` 块里**不许**
        出现 `daemon-reload`，而块**之后**必须有它。理由：手工改过 `/etc` 里那份、又改回与
        仓库逐字节相同时，`cmp -s` 说"没变"⇒ 若 reload 也绑在上面就会**静默不生效**。
        """
        text = (self._repo_root() / "deploy" / "activate.sh").read_text(encoding="utf-8")
        block_start = text.index('if [[ -r "$MAIN_UNIT_DST" ]]')
        block_end = text.index("\nfi\n", block_start)
        self.assertNotIn(
            "daemon-reload",
            text[block_start:block_end],
            "`daemon-reload` 写在了 `cmp -s` 的 if/else 块里 ⇒ 内容没变就不 reload（静默不生效）",
        )
        self.assertIn(
            "sudo systemctl daemon-reload",
            text[block_end:],
            "那个 if/else 块之后没有无条件的 `daemon-reload`",
        )

    def test_rollback_sh_loudly_says_the_main_unit_is_not_rolled_back(self):
        """`rollback.sh` 必须**响亮提示**「主单元不回滚」（本轮 M-5）。

        ⚠️ **证明力边界（如实）**：本条是**文本 + 位置**匹配，回答的是「那句用户可见的提示
        还在不在、是不是排在切 `current` 之前」；它**不**证明机器上真的发生了覆盖
        （那是部署窗口的实测）。所以把两半绑在一起：先钉住 `activate.sh` **确实**会 install
        主单元（= "会被覆盖"这个前提在仓库里成立），再钉 `rollback.sh` 里那句提示及其位置。
        """
        activate = (self._repo_root() / "deploy" / "activate.sh").read_text(encoding="utf-8")
        rollback = (self._repo_root() / "deploy" / "rollback.sh").read_text(encoding="utf-8")

        # 前提：activate.sh 确实会覆盖 /etc 里的主单元（否则下面那条提示是无的放矢）。
        self.assertIn(
            'sudo install -m 644 "$MAIN_UNIT_SRC" "$MAIN_UNIT_DST"',
            activate,
            "activate.sh 不再安装主单元 ⇒ 这条提示失去前提，两处要一起改",
        )
        # 钉的是那句**用户可见的 `echo`**（⛔ 不是随便一处注释里出现「主单元」）。
        echo_line = 'echo "⚠️ 主单元 ${SERVICE}.service **不回滚**'
        self.assertIn(echo_line, rollback, "rollback.sh 里没有那句用户可见的提示（静默）")
        self.assertIn("最近一次 activate", rollback)
        # 位置：提示必须在切 `current` **之前**（切完了再说就晚了）。
        self.assertLess(
            rollback.index(echo_line),
            rollback.index('ln -sfn "$PREV" "$R/current"'),
            "提示排在切 current 之后 ⇒ 用户是在服务已经切走之后才看到它",
        )

    def test_activate_sh_purges_metrics_after_migrate_and_before_switching_current(self):
        """清理必须排在 `migrate_schemas` **之后**（那时才有表）、切 `current` **之前**（失败不留半新半旧）。"""
        text = (self._repo_root() / "deploy" / "activate.sh").read_text(encoding="utf-8")
        migrate_at = text.index("uv run python manage.py migrate_schemas")
        purge_at = text.index("uv run python manage.py purge_request_metrics --days 90")
        switch_at = text.index('ln -sfn "$REL" "$R/current"')
        restart_at = text.index('sudo systemctl restart "$SERVICE"')
        self.assertLess(migrate_at, purge_at, "清理排在 migrate_schemas 之前 ⇒ 首次发版时表还不存在")
        self.assertLess(purge_at, switch_at, "清理排在切 current 之后 ⇒ 失败会留下半新半旧")
        # 主单元的 install 也必须在那次唯一的 restart 之前（否则要再补一次 restart）。
        self.assertLess(text.index('sudo install -m 644 "$MAIN_UNIT_SRC"'), restart_at)
        # ⛔ 清理这一步不许被 `|| true` 之类吞掉退出码（失败要响亮）。
        purge_line = text.splitlines()[
            text[:purge_at].count("\n")
        ]
        self.assertNotIn("||", purge_line)
