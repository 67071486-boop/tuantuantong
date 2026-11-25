"""绩效列表「count 并进聚合 + 筛选签名短缓存 + 参保日期窗」的验收网（性能线 2026-11-04）。

生产现场：可行库 `business_performance` 5.6 万行，进一次页 = 3 条双表全扫聚合
（分页 count + 行 SUM + 按单 SUM，EXPLAIN cost 4828~8984）。本文件钉四件事：

| # | 判据 | 用例类 |
|---|---|---|
| 1 | count 与两个行 SUM 在**同一条 SQL**（`COUNT(DISTINCT …id)` 与两个 `SUM(` 共存、恰好一条）；旧「独立 `COUNT(*) AS "__count"`」不再出现；不可见者价/退款**连查都不查** | `TotalsSingleQueryTests` |
| 2 | 缓存：同签名第二次请求零聚合 SQL；不同参数 / 不同用户 / 过期 / TTL=0 都重新查；items 行查询不受缓存影响 | `TotalsCacheTests` |
| 3 | 日期窗：date-only 恰 00:00:00.000 与 23:59:59.9 命中当日、次日 00:00:00.000 落次日（与旧 `__date` 形态同库同值对拍）；带时间两端按时刻生效（from 不含前一秒、to 含该秒）、非法值 1000 | `DateWindowBoundaryTests` |
| 4 | 对外形状：`{items,total,page,page_size,summary}` 与六键 summary 逐键一致（miss 与 hit 两路同值；2026-11-08 撤挂负账强制展示时随注脚摘除了第 7 键 `negative_debt_users`）；受限者「两有值 + 四 null」 | `ResponseShapeTests` |

夹具照 `tests/test_performance_api.py` 抄（⛔ 不自造第二套形状）。
⚠️ 测试进程里该缓存默认关（settings 测试块 `PERFORMANCE_LIST_CACHE_TTL_S=0`，
既有绩效用例大量是「GET→写→同参数立刻再 GET」）；依赖「缓存开着」的用例类
显式 `override_settings` 一个大于 0 的 TTL，且 `setUp` 先 `cache.clear()`
（locmem 住在进程里、不随 `TestCase` 的数据库回滚，照 `test_hall_short_cache.py` 先例）。
"""
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import re

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, SettleStatus
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client

URL = "/api/business/performances/"

#: 列表门（照 test_performance_api 抄）：1163 = 自助查看门；管理侧 = 1163 ∪ order.list。
PERF_LIST_NODES = ("my_performance.export",)
PERF_MANAGEMENT_NODES = PERF_LIST_NODES + ("order.list",)

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


def _at_shanghai(year, month, day, hour, minute, second=0, microsecond=0):
    """北京时间下的固定时刻（转 UTC 存库）——能表达秒与亚秒边界（照 settlement 日报网同款）。"""
    local = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=SHANGHAI)
    return local.astimezone(UTC)


class _PerfListFixture(TenantTestCase):
    """共用夹具：一款游戏 + 两位打手 + 持管理侧节点的客服（照 test_performance_api 的 setUp 抄）。"""

    def setUp(self):
        # locmem 不随数据库回滚 ⇒ 每条用例从空缓存出发（防上一条用例的键漏进来）。
        cache.clear()
        self.game = Game.objects.create(name="性能线游戏")
        self.taker_a = User.objects.create_user(username="perf-taker-a")
        self.taker_b = User.objects.create_user(username="perf-taker-b")
        self.client = make_logged_in_client(
            username="perf-cs", groups=("客服",), nodes=PERF_MANAGEMENT_NODES
        )
        # 按游戏权限收窄：请求方必须显式持有本夹具的游戏（否则列表恒空）。
        grant_games(User.objects.get(username="perf-cs"), self.game)

    def _order(self, no, *, published_at=None, price="100.00", status=OrderStatus.IN_PROGRESS, game=None):
        return Order.objects.create(
            game=self.game if game is None else game, order_no=no, status=status,
            hall_price=Decimal(price), total_price=Decimal(price),
            published_at=published_at,
        )

    def _perf(self, order, user, *, temp="20.00", bonus="0.00", emp_type=EmpType.MAIN, **kw):
        return Performance.objects.create(
            order=order, emp_type=emp_type, user=user,
            temp_amount=Decimal(temp), bonus_amount=Decimal(bonus), **kw
        )

    def _body(self, params=None):
        return self.client.get(URL, params or {}).json()

    @staticmethod
    def _has_aggregate_sql(ctx):
        return any("SUM(" in q["sql"] for q in ctx.captured_queries)


class TotalsSingleQueryTests(_PerfListFixture):
    """① 聚合单查询：count 与两个行 SUM 收成一条 SQL、旧的独立 count 消失。"""

    def test_count_and_the_two_row_sums_share_one_sql(self):
        order_a = self._order("20261104000000000001")
        self._perf(order_a, self.taker_a, temp="20.00", bonus="5.00")
        order_b = self._order("20261104000000000002", price="200.00")
        self._perf(order_b, self.taker_b, temp="30.00", bonus="6.00")

        with CaptureQueriesContext(connection) as ctx:
            body = self._body()
        self.assertEqual(body["code"], 0, body)

        # 恰好一条 SQL 同时承担 COUNT 与两个行 SUM（旧行为 = count 一条 + 行 SUM 一条）。
        count_sqls = [q["sql"] for q in ctx.captured_queries if "COUNT(DISTINCT" in q["sql"]]
        self.assertEqual(len(count_sqls), 1)
        row_sql = count_sqls[0]
        self.assertIn('"business_performance"."temp_amount"', row_sql)
        self.assertIn('"business_performance"."bonus_amount"', row_sql)

        # 旧的独立分页 count（COUNT(*) AS "__count"）不再出现；可见者聚合恰两条（行级 + 按单去重）。
        count_star = [q["sql"] for q in ctx.captured_queries if 'COUNT(*)' in q["sql"]]
        self.assertEqual(count_star, [])
        sum_sqls = [q["sql"] for q in ctx.captured_queries if "SUM(" in q["sql"]]
        # ⭐ M-5 修钉（2026-11-07 R3 恒开）：挂负账快照聚合与金额族同为 SUM 形状 ⇒ 绝对
        # SUM 计数（原 `==2`）把快照吃进去后必红；计数收窄到「含 COUNT(DISTINCT 的那条」
        # （行聚合本体），金额族 / 快照按 SQL 形状分账（快照 = 带 adjustments JOIN 的 SUM）。
        totals_sqls = [s for s in sum_sqls if "COUNT(DISTINCT" in s]
        self.assertEqual(len(totals_sqls), 1)
        self.assertIn('"business_performance"."temp_amount"', totals_sqls[0])
        self.assertIn('"business_performance"."bonus_amount"', totals_sqls[0])
        # 数字与改前逐值一致：total=2、summary 手算字面值（六键；第 7 键已随 2026-11-08
        # 撤注脚摘除）。
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(body["data"]["summary"], {
            "temp_amount": "50.00", "bonus_amount": "11.00",
            "order_price": "300.00", "refund_fee": "0.00",
            "profit_expected": "250.00", "profit_actual": "289.00",
        })

    def test_a_restricted_taker_runs_only_the_row_aggregate(self):
        """不可见者（只持 1163 的代练员）：价/退款连查都不查 ⇒ 行聚合恰好一条（含 COUNT）。
        ⭐ M-5 修钉（2026-11-07 R3 恒开）：快照聚合恒在（SUM 形状、无 COUNT）⇒ 原绝对
        计数 `==1` 收窄为「含 COUNT(DISTINCT 的 SUM SQL」恰一条。"""
        taker_client = make_logged_in_client(
            username="perf-taker-self", groups=("代练员",), nodes=PERF_LIST_NODES
        )
        grant_games(User.objects.get(username="perf-taker-self"), self.game)
        order = self._order("20261104000000000003")
        self._perf(order, User.objects.get(username="perf-taker-self"), temp="20.00")

        with CaptureQueriesContext(connection) as ctx:
            body = taker_client.get(URL).json()
        self.assertEqual(body["code"], 0, body)

        sum_sqls = [q["sql"] for q in ctx.captured_queries if "SUM(" in q["sql"]]
        totals_sqls = [s for s in sum_sqls if "COUNT(DISTINCT" in s]
        self.assertEqual(len(totals_sqls), 1)
        self.assertIn('"business_performance"."temp_amount"', totals_sqls[0])
        # 快照聚合真的在跑（M-5 的对照哨兵：它就是让原 `==1` 变 `==2` 的那条；
        # 2026-11-08 撤强制展示后快照仍每请求取——红签与净负判据的唯一来源）。
        self.assertTrue(
            [s for s in sum_sqls if "COUNT(DISTINCT" not in s],
            "前置哨兵失败：挂负账快照聚合应当真在跑（红签/净负判据来源，TTL=0 直通）",
        )


class SettlementSummaryTests(_PerfListFixture):
    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_four_amounts_share_a_scoped_query_and_cached_snapshot(self):
        other_game = Game.objects.create(name="未授权游戏")
        taker = make_logged_in_client(
            username="perf-summary-self", groups=("代练员",), nodes=PERF_LIST_NODES,
            games=[self.game],
        )
        user = User.objects.get(username="perf-summary-self")
        completed = self._order("20261104000000000091", status=OrderStatus.COMPLETED)
        in_progress = self._order("20261104000000000092")
        out_of_scope = self._order(
            "20261104000000000093", status=OrderStatus.COMPLETED, game=other_game
        )
        sold = self._order("20261104000000000094", status=OrderStatus.COMPLETED)
        moving = self._perf(completed, user, temp="5.05", bonus="-0.15")
        self._perf(
            sold, user, emp_type=EmpType.SALES, temp="2.02", bonus="2.02",
            settle_status=SettleStatus.SETTLED,
        )
        self._perf(in_progress, user, temp="3.03", bonus="0.10")
        self._perf(completed, self.taker_a, emp_type=EmpType.SALES, bonus="20.00")
        self._perf(out_of_scope, user, bonus="30.00")

        params = {"user": user.pk, "page_size": 1}
        plain = taker.get(URL, params).json()
        self.assertEqual(plain["code"], 0, plain)
        self.assertNotIn("unsettled_bonus_amount", plain["data"]["summary"])
        self.assertNotIn("settled_bonus_amount", plain["data"]["summary"])

        params["include_settlement_summary"] = 1
        with CaptureQueriesContext(connection) as ctx:
            first = taker.get(URL, params).json()
        self.assertEqual(first["code"], 0, first)
        self.assertEqual(first["data"]["total"], 3)
        self.assertEqual(len(first["data"]["items"]), 1)
        expected = {
            **plain["data"]["summary"],
            "temp_amount": "10.10", "bonus_amount": "1.97",
            "unsettled_bonus_amount": "-0.15", "settled_bonus_amount": "2.02",
        }
        self.assertEqual(first["data"]["summary"], expected)
        row_sqls = [
            q["sql"] for q in ctx.captured_queries
            if 'SUM("business_performance"."bonus_amount")' in q["sql"]
        ]
        self.assertEqual(len(row_sqls), 1)
        self.assertEqual(row_sqls[0].count('SUM("business_performance"."bonus_amount")'), 3)

        moving.settle_status = SettleStatus.SETTLED
        moving.save(update_fields=["settle_status"])
        cached = taker.get(URL, params).json()["data"]["summary"]
        self.assertEqual(cached, expected)
        cache.clear()
        fresh = taker.get(URL, params).json()["data"]["summary"]
        self.assertEqual(fresh, {
            **expected, "unsettled_bonus_amount": "0.00", "settled_bonus_amount": "1.87",
        })

        other_user = taker.get(URL, {**params, "user": self.taker_a.pk}).json()["data"]
        self.assertEqual(other_user["total"], 0)
        for key in ("temp_amount", "bonus_amount", "unsettled_bonus_amount", "settled_bonus_amount"):
            self.assertEqual(other_user["summary"][key], "0.00")


class TotalsCacheTests(_PerfListFixture):
    """② 签名短缓存：命中不打库；分签名 / 过期 / 关开关都重新查。
    ⚠️ override 用**方法级**：`TenantTestCase.setUpClass` 不链 `super()` ⇒ 类级
    `override_settings` 在它下面永远不生效（探针实测 TTL 读到 0；hall 网同款做法）。"""

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_same_signature_second_request_runs_zero_aggregate_sql(self):
        order_a = self._order("20261104000000000011")
        self._perf(order_a, self.taker_a, temp="20.00")

        with CaptureQueriesContext(connection) as first:
            body1 = self._body()
        self.assertTrue(self._has_aggregate_sql(first), "前置哨兵失败：第一次请求应当真的打聚合")

        # TTL 之内改库（加一行）⇒ total/summary 来自缓存：零聚合 SQL；items 行查询照常（新鲜）。
        order_b = self._order("20261104000000000012")
        self._perf(order_b, self.taker_a, temp="30.00")
        with CaptureQueriesContext(connection) as second:
            body2 = self._body()
        leftover = [
            q["sql"] for q in second.captured_queries
            if "SUM(" in q["sql"] or "COUNT(" in q["sql"]
        ]
        self.assertEqual(leftover, [])
        self.assertEqual(len(body2["data"]["items"]), 2)
        self.assertEqual(body2["data"]["total"], 1)
        self.assertEqual(body2["data"]["summary"]["temp_amount"], "20.00")
        # 缓存命中的 summary 与首算逐键一致。
        self.assertEqual(body1["data"]["summary"], body2["data"]["summary"])

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_a_different_filter_signature_recomputes(self):
        order_a = self._order("20261104000000000013")
        self._perf(order_a, self.taker_a, temp="20.00")
        order_b = self._order("20261104000000000014")
        self._perf(order_b, self.taker_b, temp="30.00")
        self._body()  # 预热：无筛选签名（total=3）

        with CaptureQueriesContext(connection) as ctx:
            body = self._body({"user": self.taker_a.pk})
        # 不同签名 ⇒ 不吃上一份缓存：重新聚合、数字按新筛选正确。
        self.assertTrue(self._has_aggregate_sql(ctx))
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["summary"]["temp_amount"], "20.00")

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_a_different_user_does_not_share_the_cache(self):
        order_a = self._order("20261104000000000015")
        self._perf(order_a, self.taker_a, temp="20.00")
        self._body()  # 第一位管理侧用户预热

        other = make_logged_in_client(
            username="perf-cs-two", groups=("客服",), nodes=PERF_MANAGEMENT_NODES
        )
        grant_games(User.objects.get(username="perf-cs-two"), self.game)
        with CaptureQueriesContext(connection) as ctx:
            body = other.get(URL).json()
        # 同参数、不同人 ⇒ 不同键（数据权限按人）：重新聚合、且金额族照常可见。
        self.assertTrue(self._has_aggregate_sql(ctx))
        self.assertEqual(body["data"]["summary"]["order_price"], "100.00")

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_expired_entry_is_recomputed(self):
        """到期重算：把已存键的到期时刻拨到过去模拟时间前进（⛔ 不 sleep）。
        判据出处：locmem 过期判定是 `exp <= time.time()`
        （django/core/cache/backends/locmem.py::_has_expired，实测 5.2.17）。"""
        order_a = self._order("20261104000000000016")
        self._perf(order_a, self.taker_a, temp="20.00")
        self._body()  # 预热

        self._perf(self._order("20261104000000000017"), self.taker_a, temp="30.00")
        with CaptureQueriesContext(connection) as within_ttl:
            body = self._body()
        self.assertFalse(self._has_aggregate_sql(within_ttl), "TTL 内应当命中缓存")
        self.assertEqual(body["data"]["total"], 1)

        self.assertTrue(cache._expire_info, "前置哨兵失败：缓存里应该有已存的键")
        for key in list(cache._expire_info):
            cache._expire_info[key] = 0
        with CaptureQueriesContext(connection) as after:
            body = self._body()
        self.assertTrue(self._has_aggregate_sql(after), "过期后应当重新聚合")
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(body["data"]["summary"]["temp_amount"], "50.00")

    def test_ttl_zero_passes_through_and_writes_nothing(self):
        order_a = self._order("20261104000000000018")
        self._perf(order_a, self.taker_a, temp="20.00")
        with CaptureQueriesContext(connection) as first:
            self._body()
        self.assertTrue(self._has_aggregate_sql(first))

        self._perf(self._order("20261104000000000019"), self.taker_a, temp="30.00")
        with CaptureQueriesContext(connection) as second:
            body = self._body()
        # TTL=0（紧急关闭）：零缓存读写 ⇒ 改库立即可见、不落任何本前缀的键。
        self.assertTrue(self._has_aggregate_sql(second))
        self.assertEqual(body["data"]["total"], 2)
        survivors = [k for k in cache._expire_info if "perftotals" in str(k)]
        self.assertEqual(survivors, [])

    # ── ⭐（2026-11-07 R3 / S-6）缓存用例一律写成**勾选参数下的请求**——净负集聚合只在
    #    `exclude_net_negative=1` 时取数；快照恒开但挂租户键（与筛选签名无关）。──

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_checked_params_second_request_runs_zero_aggregate_sql(self):
        """勾选排除的同签名第二发：totals / 净负集 / 快照三层全命中 ⇒ 零聚合 SQL。"""
        order_a = self._order("20261104000000000021")
        self._perf(order_a, self.taker_a, temp="20.00")
        params = {"exclude_net_negative": "1"}

        with CaptureQueriesContext(connection) as first:
            body1 = self._body(params)
        self.assertTrue(self._has_aggregate_sql(first), "前置哨兵失败：第一次请求应当真的打聚合")

        with CaptureQueriesContext(connection) as second:
            body2 = self._body(params)
        leftover = [
            q["sql"] for q in second.captured_queries
            if "SUM(" in q["sql"] or "COUNT(" in q["sql"]
        ]
        self.assertEqual(leftover, [])
        self.assertEqual(body1["data"]["summary"], body2["data"]["summary"])

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_the_tenant_snapshot_cache_is_shared_across_signatures(self):
        """挂负账快照走租户键（`perfnegdebt:<schema>`，与筛选签名无关）：换签名只重算
        totals，快照聚合不再打库（快照 SQL 是唯一带 adjustments JOIN 的聚合 ⇒ 可辨）。"""
        order_a = self._order("20261104000000000022")
        self._perf(order_a, self.taker_a, temp="20.00")
        self._body()  # 预热租户快照 + 无筛选签名

        with CaptureQueriesContext(connection) as ctx:
            self._body({"user": self.taker_a.pk})
        self.assertTrue(self._has_aggregate_sql(ctx), "totals 应当按新签名重算")
        self.assertEqual(
            [q for q in ctx.captured_queries if "business_performanceadjustment" in q["sql"]],
            [],
            "快照聚合应当命中租户键、不再打库",
        )


class DateWindowBoundaryTests(_PerfListFixture):
    """③ 参保日期窗：date-only = 半开区间 `[当日 00:00, 次日 00:00)`（与旧 `__date` 逐日等价）；
    带时间 = 两端按时刻生效（`from` 不含前一秒、`to` 含该秒；2026-11-07 交单时间筛选修复）。
    SQL 形状钉在裸列比较（套函数的列进不了 b-tree，判别力靠 ② 组的变异自证）。"""

    def _old_form_order_nos(self, date_from=None, date_to=None):
        """旧形态（`__date__gte/lte` 套函数筛）在同一批行上的命中集——改口径之前的行为。
        基集只留「published_at 非空」这一条与本批夹具对齐；两边各自独立算、才叫同库同值。"""
        qs = Order.objects.filter(published_at__isnull=False, game=self.game)
        if date_from is not None:
            qs = qs.filter(published_at__date__gte=date_from)
        if date_to is not None:
            qs = qs.filter(published_at__date__lte=date_to)
        return set(qs.values_list("order_no", flat=True))

    def _endpoint_order_nos(self, date_from=None, date_to=None):
        params = {"date_type": "published"}
        if date_from is not None:
            params["date_from"] = date_from
        if date_to is not None:
            params["date_to"] = date_to
        body = self._body(params)
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"] for row in body["data"]["items"]}

    def test_boundary_instants_land_in_the_same_buckets_as_the_old_date_lookup(self):
        """四个亚秒级边界行：恰 00:00:00.000 与 23:59:59.9 归当日；次日 00:00:00.000（判别点）落次日。"""
        self._order(
            "20261104000000000021", published_at=_at_shanghai(2026, 11, 4, 0, 0)
        )
        self._order(
            "20261104000000000022",
            published_at=_at_shanghai(2026, 11, 4, 23, 59, 59, 900000),
        )
        self._order(
            "20261104000000000023", published_at=_at_shanghai(2026, 11, 5, 0, 0)
        )
        self._order(
            "20261104000000000024",
            published_at=_at_shanghai(2026, 11, 5, 0, 0, 0, 100000),
        )
        # 四行各配一条绩效（user 非空）才进得了列表。
        for no in (
            "20261104000000000021", "20261104000000000022",
            "20261104000000000023", "20261104000000000024",
        ):
            self._perf(Order.objects.get(order_no=no), self.taker_a)

        day = date(2026, 11, 4)
        # 语义等价对拍（同库同值）：端点（新形态）与旧 `__date` 形态命中的是同一批单。
        self.assertEqual(
            self._endpoint_order_nos("2026-11-04", "2026-11-04"),
            self._old_form_order_nos(day, day),
        )
        self.assertEqual(
            self._endpoint_order_nos("2026-11-05"),
            self._old_form_order_nos(date(2026, 11, 5), None),
        )
        # 字面值钉死边界：当日两行、次日两行（⛔ 次日 00:00 不进当日）。
        self.assertEqual(
            self._endpoint_order_nos("2026-11-04", "2026-11-04"),
            {"20261104000000000021", "20261104000000000022"},
        )
        self.assertEqual(
            self._endpoint_order_nos("2026-11-05"),
            {"20261104000000000023", "20261104000000000024"},
        )

    def test_the_date_filter_sql_is_a_bare_column_comparison(self):
        """SQL 形状钉（变异判据，照 test_settlement_daily_sources 的 ② 组抄）：
        把实现改回 `__date__gte/lte`（SQL 生成 `(列 AT TIME ZONE …)::date >= …`）⇒ 本条红；
        选查询用「含 published_at 列」而非裸列界限——旧形态也选得到，形状断言自己承重。"""
        self._perf(
            self._order("20261104000000000025", published_at=_at_shanghai(2026, 11, 4, 12, 0)),
            self.taker_a,
        )
        with CaptureQueriesContext(connection) as ctx:
            body = self._body({"date_type": "published", "date_from": "2026-11-04", "date_to": "2026-11-04"})
        self.assertEqual(body["code"], 0, body)

        bounds_queries = [
            q["sql"] for q in ctx.captured_queries if '."published_at"' in q["sql"]
        ]
        # 前置哨兵：日期筛真的在跑（不是被短路成一条没有该列的查询）。
        self.assertTrue(bounds_queries, "没有捕获到带 published_at 列的查询")
        for sql in bounds_queries:
            # ① 没有任何时区转换 / 「套了 ::date 的列」参与比较（旧 `__date` 形状的整族判据）。
            self.assertNotIn("AT TIME ZONE", sql)
            self.assertIsNone(re.search(r"::date\s*(?:>=|<=|=|<|>)", sql))
            # ② 裸列半开区间的两端都在（sargable 的本体）。
            self.assertIn('."published_at" >= ', sql)
            self.assertIn('."published_at" < ', sql)

    def test_datetime_bounds_are_inclusive_at_the_second(self):
        """带时间的两端按时刻生效（业务方报障场景的字面回放，秒级断言）：
        `date_to=…20:59:59` 含 20:59:59 的单、不含 21:00:00 的单；
        `date_from=…09:30:00` 不含 09:29:59 的单；date-only 支路不受影响（上一条用例钉）。"""
        for no, (h, m, s) in {
            "20261104000000000041": (20, 59, 59),
            "20261104000000000042": (21, 0, 0),
            "20261104000000000043": (9, 29, 59),
            "20261104000000000044": (9, 30, 0),
        }.items():
            self._perf(
                self._order(no, published_at=_at_shanghai(2026, 11, 4, h, m, s)),
                self.taker_a,
            )

        # 双端同给：只中 09:30:00 与 20:59:59 两行（⭐ 21:00:00 与 09:29:59 都不中）。
        self.assertEqual(
            self._endpoint_order_nos("2026-11-04 09:30:00", "2026-11-04 20:59:59"),
            {"20261104000000000044", "20261104000000000041"},
        )
        # 单端钉：to 带时间**含该秒**（20:59:59 中、21:00:00 不中）。
        self.assertEqual(
            self._endpoint_order_nos(None, "2026-11-04 20:59:59"),
            {"20261104000000000041", "20261104000000000043", "20261104000000000044"},
        )
        # 单端钉：from 带时间**不含前一秒**（09:29:59 不中、09:30:00 中）。
        self.assertEqual(
            self._endpoint_order_nos("2026-11-04 09:30:00", None),
            {"20261104000000000041", "20261104000000000042", "20261104000000000044"},
        )

    def test_the_datetime_filter_sql_is_a_bare_column_comparison(self):
        """带时间支路的 SQL 形状同钉：裸列 `>=` / `<=`，⛔ 不是套时区函数的 `::date`。
        ⭐ 结束端从 `< 次日 00:00` 变成 `<= 该时刻` —— `<` 若漏改回旧形态，本条红。"""
        self._perf(
            self._order("20261104000000000045", published_at=_at_shanghai(2026, 11, 4, 12, 0)),
            self.taker_a,
        )
        with CaptureQueriesContext(connection) as ctx:
            body = self._body({
                "date_type": "published",
                "date_from": "2026-11-04 09:30:00",
                "date_to": "2026-11-04 20:59:59",
            })
        self.assertEqual(body["code"], 0, body)

        bounds_queries = [
            q["sql"] for q in ctx.captured_queries if '."published_at"' in q["sql"]
        ]
        self.assertTrue(bounds_queries, "没有捕获到带 published_at 列的查询")
        for sql in bounds_queries:
            self.assertNotIn("AT TIME ZONE", sql)
            self.assertIsNone(re.search(r"::date\s*(?:>=|<=|=|<|>)", sql))
            self.assertIn('."published_at" >= ', sql)
            self.assertIn('."published_at" <= ', sql)

    def test_partial_datetime_and_impossible_dates_are_rejected_with_1000(self):
        """非法值（两种格式都不匹配 / strptime 拒）⇒ 200 + 业务码 1000，不是 HTML 500（E-0010）。"""
        for bad in ("2026-10-31 20:59", "2026-13-45"):
            with self.subTest(value=bad):
                body = self._body({"date_type": "published", "date_to": bad})
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
                self.assertIsNone(body["data"])


class ResponseShapeTests(_PerfListFixture):
    """④ 对外形状：信封五键与六键 summary 在 miss / hit 两路逐键一致；受限者两有值四 null。"""

    def test_the_envelope_shape_is_identical_on_miss_and_hit(self):
        order_a = self._order("20261104000000000031")
        self._perf(order_a, self.taker_a, temp="20.00", bonus="5.00")
        order_b = self._order("20261104000000000032", price="200.00")
        self._perf(order_b, self.taker_b, temp="30.00", bonus="6.00")

        with override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45):
            first = self._body({"page_size": 1})
            second = self._body({"page_size": 1})  # 同签名 ⇒ 命中缓存
        self.assertEqual(first["code"], 0, first)
        self.assertEqual(
            set(first["data"]), {"items", "total", "page", "page_size", "summary"}
        )
        self.assertEqual(first["data"]["total"], 2)
        # summary 六键（第 7 键 `negative_debt_users` 已随 2026-11-08 撤注脚摘除）。
        self.assertEqual(first["data"]["summary"], {
            "temp_amount": "50.00", "bonus_amount": "11.00",
            "order_price": "300.00", "refund_fee": "0.00",
            "profit_expected": "250.00", "profit_actual": "289.00",
        })
        # 命中路整个 data 逐键一致（items / total / page / page_size / summary 一个不差）。
        self.assertEqual(second["data"], first["data"])

    def test_a_restricted_taker_gets_two_values_and_four_nones(self):
        taker_client = make_logged_in_client(
            username="perf-taker-shape", groups=("代练员",), nodes=PERF_LIST_NODES
        )
        grant_games(User.objects.get(username="perf-taker-shape"), self.game)
        order = self._order("20261104000000000033")
        self._perf(
            order, User.objects.get(username="perf-taker-shape"),
            temp="20.00", bonus="5.00",
        )

        body = taker_client.get(URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["summary"], {
            "temp_amount": "20.00", "bonus_amount": "5.00",
            "order_price": None, "refund_fee": None,
            "profit_expected": None, "profit_actual": None,
        })
