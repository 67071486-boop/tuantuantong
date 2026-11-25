"""开放平台 v2 T3：限流计数（DB 固定窗口桶）与取令牌端点的两道闸。

判据来源 = `docs/specs/2026-10-08-open-platform-v2-parity-design.md` §10 / §12 /
§13.11-1，以及 `apps/openapi/rate_limit.py` / `token_views.py` 的模块 docstring。

## ⭐ 本文件里最要紧的一条：`MultiConnectionEquivalenceTests`（spec §12 那条"从推断升级为实测"）

旧实现（`LocMemCache`）的代价是「实际额度 ≈ 设定值 × worker 数」。本文件用**两条独立的数据库
连接**（两个线程各一条；Django 的 `connections` 是 thread-critical 的）各自打满额度，断言
**总放行次数恰好等于 `limit`**（⛔ 不是 2×limit），并断言两条连接写的是**同一行**、
行里的 `count` 等于**两次尝试之和**。

⛔ **这条对照证明什么、不证明什么**（如实登记，⛔ 别读大）：
- **证明**：① 计数**不落在"每条连接各一份"的内存里**（那种实现会给出 2×limit）；
  ② 两次连接的每一次 `consume` 都**落到同一行**上（"计数住在共享存储里"的直接证据）；
  ③ 那条原子的 `INSERT … ON CONFLICT DO UPDATE … RETURNING` 在**真并发**下不丢计数。
- ⛔ **不证明"跨进程"**：两个线程仍在**同一个解释器**里 ⇒ 一个**模块级**的 dict（进程内全局）
  在同进程的两条线程之间是**共享的**，它照样能让"总放行数 == limit"这条成立
  ⇒ 那一条**单独**分辨不出"进程内内存"。分辨它的是**第二条判据**（同一行、count 等于两次之和）
  —— 内存实现根本不写这一行。⚠️ 这正是 T3 执行期实测到的那件事：把 `consume` 的计数换成
  **模块级 dict** 之后，①仍绿、②红（读数见 T3 报告的弄红记录）。
  ⛔ 真正的多**进程**读数（两个解释器）不在这里 —— 它由总指挥另行代跑并登记。
- ⛔ **不证明"额度精确到恰好"**：固定窗口跨边界时相邻两个窗口各自能放满（口径见
  `rate_limit.py` 的模块 docstring），本文件用**注入的固定 `now`** 把窗口钉死，⛔ 不测边界那件事。

## 夹具纪律

- 凭据 / 来源 IP 一律**合成值**（`AGENTS.md` §5-C；IP 用文档保留段 `203.0.113.0/24`）。
- 本文件**不**碰真 nginx / 真 worker：多连接那一条是**线程 + 独立连接**（同款先例
  `tests/test_claim_concurrency.py`，那里也写明了"不覆盖跨进程 / 多副本"）。
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.openapi import rate_limit
from apps.openapi.api import token_views
from apps.openapi.api.token_views import (
    ERROR_INVALID_CLIENT,
    ERROR_TEMPORARILY_UNAVAILABLE,
    GRANT_TYPE_CLIENT_CREDENTIALS,
    RATE_LIMITED_DESCRIPTION,
    TOKEN_FALLBACK_MAX_REQUESTS,
)
from apps.openapi.authentication import (
    # ⛔ `BAD_SIGNATURE_MSG`（签名那档文案）已随 v1 面删除（§13.1），不再 import；
# 判据一条未改。
    BEARER_INVALID_CREDENTIALS_MSG,
    IP_FAILURE_MAX,
    ApiAppBearerAuthentication,
    count_ip_failure,
    ip_failure_gate_already_exceeded,
)
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, OpenApiRateBucket
from apps.openapi.rate_limit import UNPARSABLE_IDENT
from apps.openapi.tokens import issue
from apps.takerapi.models import TakerApiRateBucket

User = get_user_model()

#: 契约路径（与 `apps/openapi/urls.py` 逐字一致：**不带**尾斜杠）。
TOKEN_PATH = "/api/open/oauth/token"
#: 现成的业务端点：
#: - `BUSINESS_PATH`（集合路径）今天只认 **Bearer 令牌** ⇒ 给"Bearer 路径"那一侧当取样点；
#: - `DETAIL_PATH` 与 `BUSINESS_PATH` 都走 Bearer。详情路径留着当取样点，
#:   验的是「来源不可解析在对外路径上被闸拦住」，与凭据形态无关。
BUSINESS_PATH = "/api/open/v1/orders/"
DETAIL_PATH = "/api/open/v1/orders/20261007120000000001/"

#: 合成凭据（⛔ 不是任何真实租户的凭据）。
APP_NAME = "限流探针应用甲"
APP_KEY = "probe-ratelimit-key-000001"
APP_SECRET = "probe-ratelimit-secret-00000000001"
WRONG_SECRET = "probe-ratelimit-secret-000000000ff"
UNKNOWN_KEY = "probe-ratelimit-key-0000ff"

#: 来源 IP（文档保留段 203.0.113.0/24 与 198.51.100.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.77"
OTHER_SOURCE_IP = "198.51.100.77"

#: 多连接那一条用的合成维度与窗口（⛔ 不用真实 IP：它不经过 HTTP，只有 `consume` 的接口）。
MULTI_SCOPE = OpenApiRateBucket.Scope.IP
MULTI_IDENT = "203.0.113.78"
MULTI_LIMIT = 5
MULTI_WINDOW_SECONDS = 60
#: 每次连接各打几次（必须 **> limit**：否则"每条连接各算一份"的错实现也会凑出恰好 limit）。
MULTI_ATTEMPTS_PER_CONNECTION = MULTI_LIMIT + 3
#: 注入的"现在"（固定值 ⇒ 窗口确定、与真实时钟无关；T3 之前与之后都好复算）。
MULTI_NOW = 1_800_000_000.0
#: 栅栏超时：正常应远小于 1s（两条本地连接），放 10s 只是为了机器抖动时不误判。
_BARRIER_TIMEOUT = 10

#: 多连接那一条用的租户 schema（与 `tests/test_claim_concurrency.py` 同款：
#: `django_tenants` **没有** `TenantTransactionTestCase`，夹具照 `TenantTestCase.setUpClass` 自己搭）。
TENANT_SCHEMA = "test"


def _window_start_of(now: float, window_seconds: int) -> datetime:
    """**用例自己算**窗口左端（⛔ 不调 `rate_limit._window_start`：那是被测实现的一部分）。

    ⚠️ 与实现同一条算式（`int(now // 窗口) * 窗口`、UTC aware）是**有意**的：
    判据要断言的正是"行落在哪一个窗口上"，用它自己的算式算出来才有判别力。
    """
    return datetime.fromtimestamp(
        int(now // window_seconds) * window_seconds, tz=dt_timezone.utc
    )


class MultiConnectionEquivalenceTests(TransactionTestCase):
    """⭐ 两条独立连接各打满额度 ⇒ **总放行次数恰好 = `limit`**（spec §12 / §10）。

    ⚠️ **为什么必须是 `TransactionTestCase`**：`TenantTestCase` 继承 Django 的 `TestCase`，
    把每条测试包在一个事务里 —— 另一条连接**看不到未提交的行** ⇒ 「两条连接共用一行」这件事
    根本无从谈起（症状：另一条连接每次都从 0 开始，读数看起来像"每条连接各一份计数"，
    而那是**夹具造成的假象**）。`TransactionTestCase` 不包事务（每条语句自动提交）⇒ 两条连接
    互相看得见，这才是"两个 worker 打同一个桶"的**同进程代理**。
    ⚠️ 代价（如实登记）：`TransactionTestCase` 比 `TestCase` 慢（每条用例后 `flush` 表），
    且它**不覆盖跨进程**（见模块 docstring 的边界）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("migrate_schemas", schema_name="public", interactive=False, verbosity=0)
        cls.tenant = get_tenant_model()(schema_name=TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="openapi-ratelimit.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_connections_share_one_bucket_so_the_total_is_exactly_the_limit(self):
        """⭐ 两条连接、真并发 ⇒ 放行总数**恰好** `MULTI_LIMIT`，且两条连接写的是**同一行**。

        | # | 判据 | 它怎么才会红 |
        |---|---|---|
        | ① | 两次连接的放行总数 == `MULTI_LIMIT` | 计数落"每条连接各一份"的内存 ⇒ 2×limit |
        | ② | 那一行的 `count` == 两次尝试之和（`2 × 8 = 16`） | 计数落内存 ⇒ 这一行压根不存在 / count 对不上 |
        | ③ | 两条线程都跑完、都留下了读数（哨兵） | 线程死在启动阶段时，①②会在**样本变少**的情况下照样成立 |

        ⚠️ 判据 ② 是**必须**的：①单独分辨不出"模块级 dict"（进程内共享，见模块 docstring）。
        """
        results: dict[int, list[bool]] = {}
        barrier = threading.Barrier(2)

        def _worker(index: int) -> None:
            # 每个线程一条独立连接：先关掉本线程可能残留的连接，再切 schema
            # （新连接默认落在 public schema；`business` / `openapi` 的表都在租户 schema 里）。
            connections.close_all()
            try:
                with schema_context(TENANT_SCHEMA):
                    # 起跑线：两条连接**同时**开始打，让"读-改-写"的竞争窗口真的出现。
                    barrier.wait(timeout=_BARRIER_TIMEOUT)
                    results[index] = [
                        rate_limit.consume(
                            scope=MULTI_SCOPE,
                            ident=MULTI_IDENT,
                            limit=MULTI_LIMIT,
                            window_seconds=MULTI_WINDOW_SECONDS,
                            now=MULTI_NOW,
                            # ⚠️ T2b：`spec` 改成必填 keyword-only
                            # ⇒ 原先由默认值兜住的这一件**写出来**（⛔ 断言未动）。
                            spec=OPENAPI_SPEC,
                        )[0]
                        for _ in range(MULTI_ATTEMPTS_PER_CONNECTION)
                    ]
            finally:
                connections.close_all()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # ③ 哨兵：两条线程都跑完了、都留下了读数。
        self.assertFalse([t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出")
        self.assertEqual(len(results), 2, f"有线程没留下结果（死在 try 之外）: {results}")
        self.assertEqual(
            [len(results[i]) for i in range(2)],
            [MULTI_ATTEMPTS_PER_CONNECTION] * 2,
            "有线程没打满它那几次",
        )

        # ① 放行总数**恰好**等于额度（⛔ 不是 2×limit）。
        allowed_total = sum(outcome for outcomes in results.values() for outcome in outcomes)
        self.assertEqual(
            allowed_total,
            MULTI_LIMIT,
            f"两条连接一共放行了 {allowed_total} 次（额度 {MULTI_LIMIT}）⇒ "
            f"计数没落在共享存储里（每条连接各一份会给出 {2 * MULTI_LIMIT}）：{results}",
        )

        # ② 两条连接写的是**同一行**，且那一行的 count 是**两次尝试之和**
        #    —— 这是"计数住在共享存储里"的直接证据（内存实现不会写这一行）。
        window_start = _window_start_of(MULTI_NOW, MULTI_WINDOW_SECONDS)
        row = OpenApiRateBucket.objects.filter(
            scope=MULTI_SCOPE, ident=MULTI_IDENT, window_start=window_start
        ).first()
        self.assertIsNotNone(
            row,
            "这一行不存在 ⇒ 计数没有落在共享存储里（两条连接的每一次 consume 都该写同一行）",
        )
        self.assertEqual(
            row.count,
            2 * MULTI_ATTEMPTS_PER_CONNECTION,
            "两条连接的每一次 consume 都该落在同一行上（含被挡的那几次）",
        )

    def test_a_single_connection_control_reaches_exactly_the_limit(self):
        """对照（上一条要有判别力）：**同一条连接**打满 ⇒ 一样是恰好 `limit` 次放行。

        ⚠️ 它的作用是证明上面那条断言**不是恒真 / 恒假**：若 `consume` 的实现被改成
        "一律放行"或"一律拒绝"，这里当场红（上面那条会给出 16 或 0，而不是 5）。
        """
        with schema_context(TENANT_SCHEMA):
            outcomes = [
                rate_limit.consume(
                    scope=MULTI_SCOPE,
                    ident=OTHER_SOURCE_IP,
                    limit=MULTI_LIMIT,
                    window_seconds=MULTI_WINDOW_SECONDS,
                    now=MULTI_NOW,
                    # ⚠️ T2b：`spec` 必填 keyword-only（原先由默认值兜住 ⇒ 现在写出来）。
                    spec=OPENAPI_SPEC,
                )[0]
                for _ in range(MULTI_ATTEMPTS_PER_CONNECTION)
            ]
        # 前 limit 次放行、其后全挡（边界语义：放行总数恰好 = limit）。
        self.assertEqual(outcomes, [True] * MULTI_LIMIT + [False] * 3, outcomes)


class RateBucketSemanticsTests(TenantTestCase):
    """`rate_limit.consume` / `current_count` 的边界、`Retry-After` 与机会式清理。

    ⚠️ 用 `TenantTestCase`（事务回滚）—— 这些判据都**不需要**另一条连接看见未提交的行；
    多连接那一面在 `MultiConnectionEquivalenceTests`，⛔ 别把两者混起来。
    """

    NOW = 1_800_000_500.0  # 与上面不同的合成时刻（⛔ 不用真实时钟 ⇒ 窗口确定）
    WINDOW = 60

    def _consume(self, ident="203.0.113.79", limit=3, now=None):
        return rate_limit.consume(
            scope=OpenApiRateBucket.Scope.IP,
            ident=ident,
            limit=limit,
            window_seconds=self.WINDOW,
            now=self.NOW if now is None else now,
            # ⚠️ T2b：`spec` 必填 keyword-only（原先由默认值兜住 ⇒ 现在写出来，⛔ 断言未动）。
            spec=OPENAPI_SPEC,
        )

    def test_the_boundary_is_count_at_most_limit(self):
        """⭐ 边界：**第 `limit` 次放行、第 `limit + 1` 次被挡**（放行总数恰好 = `limit`）。

        ⚠️ 与旧实现（先判后记）**结果相同**、⛔ 不是"差不多"：这里的期望值是**逐项**写死的
        列表（⛔ 不用 `>=` / `<=` 那种含糊断言），任一项翻转都红。
        """
        outcomes = [self._consume(limit=3)[0] for _ in range(4)]
        self.assertEqual(outcomes, [True, True, True, False], outcomes)

    def test_the_retry_after_is_a_positive_integer_within_the_window(self):
        """`Retry-After` 用的秒数：**≥ 1 的整数**、且不超过一个窗口（放行时也给，调用方按需忽略）。"""
        allowed, retry_after = self._consume(limit=1, now=self.NOW + 0.5)
        self.assertIs(allowed, True)
        self.assertIsInstance(retry_after, int)
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, self.WINDOW)
        # 被挡的那一次同样给（DRF 拿它塞 `Retry-After` 头）。
        _allowed, blocked_retry_after = self._consume(limit=1, now=self.NOW + 0.5)
        self.assertGreaterEqual(blocked_retry_after, 1)

    def test_a_window_boundary_starts_a_new_row(self):
        """跨过窗口边界 ⇒ 换一行（**固定窗口**口径，⛔ 不是滑动窗口）。"""
        first = self._consume(limit=1, now=self.NOW)[0]
        same_window = self._consume(limit=1, now=self.NOW + 1)[0]
        next_window = self._consume(limit=1, now=self.NOW + self.WINDOW)[0]
        self.assertEqual([first, same_window, next_window], [True, False, True])

    def test_current_count_is_read_only_and_does_not_create_a_row(self):
        """`current_count` **只读**：⛔ 不建行、⛔ 不加一（它是闸门"先看一眼"那一档用的）。"""
        self.assertEqual(
            rate_limit.current_count(
                scope=OpenApiRateBucket.Scope.IP,
                ident="203.0.113.80",
                window_seconds=self.WINDOW,
                now=self.NOW,
                # ⚠️ T2b：`spec` 必填 keyword-only（原先由默认值兜住 ⇒ 现在写出来，⛔ 断言未动）。
                spec=OPENAPI_SPEC,
            ),
            0,
        )
        self.assertFalse(
            OpenApiRateBucket.objects.filter(ident="203.0.113.80").exists(),
            "只读的那一档不该留下任何行",
        )
        self._consume(ident="203.0.113.80")
        self.assertEqual(
            rate_limit.current_count(
                scope=OpenApiRateBucket.Scope.IP,
                ident="203.0.113.80",
                window_seconds=self.WINDOW,
                now=self.NOW,
                # ⚠️ T2b：同上（第二次「先看一眼」那一档）。
                spec=OPENAPI_SPEC,
            ),
            1,
        )

    def test_the_ip_ident_normalises_spellings_and_falls_back_to_a_placeholder(self):
        """规范化：**同一地址的多种写法**归成一个 `ident`；解析不了的落**固定占位值**。

        ⚠️ 期望值是**字面量**（E-0024）：`::1` 与 `0:0:0:0:0:0:0:1` 必须收敛到同一个串。
        ⛔ 占位值**不是**截断出来的：它一个字面都不含原始输入（截断会把两个不同的伪造值
        并成一行，而"归并了什么"没人说得清）。
        """
        self.assertEqual(rate_limit.ip_ident("203.0.113.7"), "203.0.113.7")
        self.assertEqual(rate_limit.ip_ident("::1"), "::1")
        self.assertEqual(rate_limit.ip_ident("0:0:0:0:0:0:0:1"), "::1")
        for garbage in ("", "not-an-ip", "x" * 500, "203.0.113.7/24"):
            with self.subTest(garbage=len(garbage)):
                self.assertEqual(rate_limit.ip_ident(garbage), "unparsable")
                self.assertNotIn("x", rate_limit.ip_ident(garbage))
                # ⭐ R1：fail-closed 的判据本身（各计数入口靠它"直接拒"，⛔ 不再落那个占位桶）。
                self.assertIs(rate_limit.is_unparsable(garbage), True)
        for parsable in ("203.0.113.7", "::1", "0:0:0:0:0:0:0:1"):
            with self.subTest(parsable=parsable):
                self.assertIs(rate_limit.is_unparsable(parsable), False)

    def test_two_spellings_of_one_address_share_one_bucket(self):
        """桶层面：`::1` 与 `0:0:0:0:0:0:0:1` 各失败一次 ⇒ **同一行**、`count == 2`。"""
        # ⚠️ `spec` 是必填 keyword-only ⇒ 这两处"只传来源"的调用必须显式给实参
                #    （⛔ 断言一字未改）。
        self.assertIs(count_ip_failure("::1", spec=OPENAPI_SPEC), False)
        self.assertIs(count_ip_failure("0:0:0:0:0:0:0:1", spec=OPENAPI_SPEC), False)
        rows = list(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP).values_list(
                "ident", "count"
            )
        )
        self.assertEqual(rows, [("::1", 2)], rows)

    def test_stale_rows_are_pruned_on_the_first_hit_of_a_window(self):
        """⭐ 机会式清理：**本窗口第一次命中**时删掉早于「当前窗口 − 3 个窗口」的行。

        两半都要证（只证前一半时，"每次调用都全表删"照样绿）：
        ① 超过保留期的行被删；② **保留期之内**的行**不许**被删（否则清理会误伤上一个窗口的计数）。
        """
        window_start = _window_start_of(self.NOW, self.WINDOW)
        stale = OpenApiRateBucket.objects.create(
            scope=OpenApiRateBucket.Scope.IP,
            ident="203.0.113.81",
            window_start=window_start - timedelta(seconds=4 * self.WINDOW),
            count=7,
        )
        kept = OpenApiRateBucket.objects.create(
            scope=OpenApiRateBucket.Scope.IP,
            ident="203.0.113.82",
            window_start=window_start - timedelta(seconds=1 * self.WINDOW),
            count=7,
        )
        # 第一次命中本窗口 ⇒ 触发清理。
        self._consume(ident="203.0.113.83", limit=2)
        self.assertFalse(
            OpenApiRateBucket.objects.filter(pk=stale.pk).exists(), "超过保留期的行该被删掉"
        )
        self.assertTrue(
            OpenApiRateBucket.objects.filter(pk=kept.pk).exists(),
            "保留期之内的行不许被删（它还在 3 个窗口以内）",
        )

    def test_a_stale_row_survives_when_the_window_was_already_hit(self):
        """⚠️ 清理**只在**「本窗口第一次命中」时发生：本窗口已经记过之后再放一行旧行 ⇒ 它**留着**。

        ⚠️ 这条钉的是"清理挂在哪一刻"：若实现改成"每次调用都清理"，本用例红
        （旧行会被当场删掉）。⚠️ 如实登记代价：真跑生产时旧行最多多留一个窗口 ——
        **有意的**（每次调用都扫一遍删除的代价比它贵，见 `rate_limit.py` 的模块 docstring）。
        """
        ident = "203.0.113.84"
        self._consume(ident=ident, limit=5)  # 本窗口的第一次命中（此时还没有旧行）
        window_start = _window_start_of(self.NOW, self.WINDOW)
        stale = OpenApiRateBucket.objects.create(
            scope=OpenApiRateBucket.Scope.IP,
            ident="203.0.113.85",
            window_start=window_start - timedelta(seconds=9 * self.WINDOW),
            count=1,
        )
        self._consume(ident=ident, limit=5)  # 本窗口第二次命中 ⇒ 不清理
        self.assertTrue(OpenApiRateBucket.objects.filter(pk=stale.pk).exists())


class TokenEndpointFailureGateTests(TenantTestCase):
    """⭐ 取令牌端点的**按来源 IP 失败闸**（v2 §13.11-1 ①；T2 的真空面）。

    ## 这一份证什么（三条原始读数，缺一条就分辨不出 (A) 那个取舍）

    ① 前 `IP_FAILURE_MAX` 次凭据失败都是 **401**（且都**真的被记了数** —— 行里的 `count` == 20）；
    ② 第 `IP_FAILURE_MAX + 1` = **21 次 attempt 仍被拒**（回 **429**，⛔ 不是 401）；
    ③ 第 22 次（哪怕凭据是**对的**）⇒ 429，且**一次都不查 `ApiApp`**（那一支就是"直接拒"）。

    ⚠️ 为什么 ② 是 429 而⛔ 不是 401：第 21 次 attempt 的 `consume` 判定已经是"不放行"
    （`count = 21 > 20`）⇒ 本端点那一次回的是限流那一档（细节与 `>`/`>=` 的取舍见
    `authentication.ip_failure_gate_already_exceeded` 的 docstring）。
    """

    def setUp(self):
        super().setUp()
        clock = mock.patch.object(rate_limit, "time", mock.Mock(time=lambda: 1800000000))
        clock.start()
        self.addCleanup(clock.stop)
        self.domain_name = self.domain.domain
        creator, _ = User.objects.get_or_create(
            username="openapi-ratelimit-probe", defaults={"password": "unused-in-this-test"}
        )
        self.app = ApiApp.objects.create(
            name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET, created_by=creator
        )

    def _token_request(self, *, client_id=APP_KEY, client_secret=APP_SECRET, remote_addr=SOURCE_IP):
        return Client(SERVER_NAME=self.domain_name).post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            REMOTE_ADDR=remote_addr,
        )

    def _failure_row(self, ident=SOURCE_IP):
        """取此来源最新窗口的失败计数；跨窗口对照也使用该读取方式。"""
        return (
            OpenApiRateBucket.objects.filter(
                scope=OpenApiRateBucket.Scope.IP, ident=ident
            )
            .order_by("-window_start")
            .first()
        )

    def test_control_two_windows_split_the_failure_count(self):
        clock = mock.Mock()
        with mock.patch.object(rate_limit, "time", clock):
            clock.time.return_value = 1800000000
            for _ in range(8):
                self.assertEqual(self._token_request(client_secret=WRONG_SECRET).status_code, 401)
            clock.time.return_value += 60
            for _ in range(13):
                self.assertEqual(self._token_request(client_secret=WRONG_SECRET).status_code, 401)
        self.assertEqual(self._failure_row().count, 13)
        counts = OpenApiRateBucket.objects.filter(
            scope=OpenApiRateBucket.Scope.IP, ident=SOURCE_IP
        ).order_by("window_start").values_list("count", flat=True)
        self.assertEqual(list(counts), [8, 13])

    def test_the_first_twentieth_attempt_is_401_and_the_twenty_first_is_429(self):
        """⭐ ① + ②：前 20 次 401（都记了数）、**第 21 次 attempt 是 429**。"""
        self.assertEqual(IP_FAILURE_MAX, 20)
        for index in range(IP_FAILURE_MAX):
            response = self._token_request(client_secret=WRONG_SECRET)
            self.assertEqual(response.status_code, 401, (index, response.content[:200]))
            body = response.json()
            self.assertEqual(set(body), {"error", "error_description"})
            self.assertEqual(body["error"], ERROR_INVALID_CLIENT)
        # ① 后一半：失败**真的被记了数**（⛔ 不是只有拒绝、没有记账）。
        self.assertEqual(self._failure_row().count, IP_FAILURE_MAX)
        # ⭐ ② 第 21 次 attempt：仍被拒 —— 这一档回的是**限流**（429），不再是"凭据无效"。
        over = self._token_request(client_secret=WRONG_SECRET)
        self.assertEqual(over.status_code, 429, over.content[:200])
        payload = over.json()
        self.assertEqual(set(payload), {"error", "error_description"})
        self.assertEqual(payload["error"], ERROR_TEMPORARILY_UNAVAILABLE)
        self.assertEqual(payload["error_description"], RATE_LIMITED_DESCRIPTION)

    def test_the_twenty_second_attempt_is_refused_without_touching_the_database(self):
        """⭐ ③ "直接拒"：已经超限 ⇒ 429，且**一次都不查 `ApiApp`**（省掉那条查询与那次比较）。

        ⚠️ 这是**早退**那一支的判据（`authentication.ip_failure_gate_already_exceeded`）。
        配套的对照在下面（`test_the_control_...`）—— 桩接没接上本身要有判据（E-0029）。
        """
        for _ in range(IP_FAILURE_MAX + 1):  # 攒到 21（第 21 次已经是 429 了）
            self._token_request(client_secret=WRONG_SECRET)
        self.assertGreater(self._failure_row().count, IP_FAILURE_MAX)
        with mock.patch.object(
            token_views.ApiApp.objects, "filter", side_effect=AssertionError("不该查库")
        ):
            # ⚠️ 用**正确**的凭据发：这样"被拒"只可能来自闸门，⛔ 不是来自凭据那一档。
            blocked = self._token_request()
        self.assertEqual(blocked.status_code, 429, blocked.content[:200])
        self.assertEqual(set(blocked.json()), {"error", "error_description"})

    def test_the_control_the_stub_really_intercepts_the_lookup(self):
        """对照（E-0029）：闸门**开着**时，`ApiApp.objects.filter` **一定被调用** ⇒ 桩接得上。

        ⚠️ 没有这一条，上面那条"不查库"可能是桩**没接上**（`mock.patch` 打错了模块）
        而假绿 —— 那时它其实查了库、只是没有断言看见。
        """
        with mock.patch.object(
            token_views.ApiApp.objects, "filter", side_effect=AssertionError("查库了")
        ):
            with self.assertRaises(AssertionError):
                self._token_request()

    def test_the_unsupported_grant_type_is_not_counted_as_a_failure(self):
        """`grant_type` 那一档**不记**失败计数（它不是"猜凭据"）。

        判据：连打 `IP_FAILURE_MAX + 5` 次错 `grant_type`，之后**合法**凭据仍然能换到令牌，
        且**一行失败计数都没有**（⛔ 若实现把它算成失败，这里会因为同一 IP 被拦而红）。
        """
        for _ in range(IP_FAILURE_MAX + 5):
            response = Client(SERVER_NAME=self.domain_name).post(
                TOKEN_PATH,
                {
                    "grant_type": "password",
                    "client_id": APP_KEY,
                    "client_secret": APP_SECRET,
                },
                REMOTE_ADDR=SOURCE_IP,
            )
            self.assertEqual(response.status_code, 401, response.content[:200])
            self.assertEqual(response.json()["error"], "unsupported_grant_type")
        self.assertFalse(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP).exists(),
            "错 grant_type 不该在失败桶里留下任何行",
        )
        ok = self._token_request()
        self.assertEqual(ok.status_code, 200, ok.content[:200])

    def test_the_failure_row_is_shared_with_the_bearer_path(self):
        """⭐ 「共用同一套 DB 计数」（§13.11-1 ①）：取令牌端点的失败**也算进 Bearer 那条闸**。

        判据两条（缺一不可）：
        ① 在取令牌端点错满 20 次之后，**Bearer** 那条路带着**合法令牌**也被拦（同一行计数）；
        ② **另一个来源**的合法令牌照样通过（⛔ 不是"任何失败都永久拉黑所有人"）。
        ⚠️ 行里的 `ident` 就是**纯来源 IP**（⛔ 不带用途前缀 / 不带 schema）。
        """
        for _ in range(IP_FAILURE_MAX):
            self._token_request(client_secret=WRONG_SECRET)
        row = self._failure_row()
        self.assertEqual((row.scope, row.ident, row.count), ("ip", SOURCE_IP, IP_FAILURE_MAX))
        token = issue(APP_KEY, APP_SECRET)

        def _bearer(remote_addr):
            request = APIRequestFactory().get(BUSINESS_PATH, REMOTE_ADDR=remote_addr)
            request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            return ApiAppBearerAuthentication().authenticate(request)

        with self.assertRaises(AuthenticationFailed) as caught:
            _bearer(SOURCE_IP)
        self.assertEqual(str(caught.exception.detail), BEARER_INVALID_CREDENTIALS_MSG)
        # ② 另一个来源**不受**影响。
        _user, app = _bearer(OTHER_SOURCE_IP)
        self.assertEqual(app.pk, self.app.pk)

    def test_the_shared_boundary_is_the_same_attempt_for_both_paths(self):
        """一次性把两条路的边界摆在同一个读数里：`count == IP_FAILURE_MAX` 时

        - Bearer 路径（`>=` 口径）**当场**拒；
        - 取令牌端点（`>` 早退 + `consume` 判定）**第 21 次 attempt 拒**（429）。
        ⚠️ 上一条已经证了"第 21 次是 429"；这一条证的是**同一行计数**下**两条路都从第 21 次被拒**，
        ⛔ 不是"看起来一样"（两侧各有自己的断言）。
        """
        for _ in range(IP_FAILURE_MAX):
            self._token_request(client_secret=WRONG_SECRET)
        # 正好 20 ⇒ 早退**不**生效（判据是 `>` 而不是 `>=`；理由见
        # `authentication.ip_failure_gate_already_exceeded` 的 docstring）。
        self.assertFalse(ip_failure_gate_already_exceeded(SOURCE_IP, spec=OPENAPI_SPEC))
        # 第 21 次 attempt：由 `consume` 自己的判定拒（429），⛔ 不是走到"凭据无效"那一档。
        over = self._token_request(client_secret=WRONG_SECRET)
        self.assertEqual(over.status_code, 429, over.content[:200])
        # 从此早退生效（21 > 20）⇒ 之后的请求连库都不查（上一条用例证了"不查库"）。
        self.assertTrue(ip_failure_gate_already_exceeded(SOURCE_IP, spec=OPENAPI_SPEC))
        # 直调那一个"记一次并回答是否超限"的出口：已经超限 ⇒ `True`（计数变成 22，无后续依赖）。
        self.assertTrue(count_ip_failure(SOURCE_IP, spec=OPENAPI_SPEC))


#: ⭐ 伪造的来源头：**超长且不是 IP**（500 个字符）。
#: ⚠️ 它钉的是"**这类**请求还会不会 500"（`AGENTS.md` §5-E），⛔ 不是"这个字段"：
#: `ident` 列宽 64，而 `authentication.client_ip()` 明说自己**不管**能不能解析成 IP。
UNPARSABLE_X_REAL_IP = "x" * 500
#: 第二个**不同**的垃圾来源（探"互相伤害"那条对照用；⛔ 与上面那个逐字不同）。
OTHER_UNPARSABLE_X_REAL_IP = "y" * 400


class UnparsableSourceIpTests(TenantTestCase):
    """⭐ 来源串**解析不成 IP** 时的口径：**fail-closed 直接拒**（R1 的裁决）。

    ## 判据三条（③ 是那条"互相伤害已消除"的对照）

    | # | 判据 | 旧实现（归并到占位桶）下会怎样 |
    |---|---|---|
    | ① | 垃圾来源（超长 / 空串 / 非 IP 文本）⇒ **被拒**，且按各路径既有契约的形状 | 前 20 次**不是**直接被拒（取令牌端点会走到凭据那一档、回 `invalid_client`）⇒ **红** |
    | ② | 计数表里**没有** `ident == UNPARSABLE_IDENT` 的行（那条路径根本没写桶） | 会有一行 ⇒ **红** |
    | ③ | ⭐ 先用**来源 G1** 灌 N 条，**另一个不同**的垃圾来源 **G2** 的答复与灌之前**逐字相同** | G2 在灌之前 = 凭据那一档的答复、灌之后 = 被共享桶挡住 ⇒ **红** |

    ⚠️ **如实登记 ③ 的判别力边界**：③ 里"另一个**合法（可解析）**来源照常"那半句
    **在两种实现下都是绿的**（合法来源本来就有自己的桶，不会被别人的灌量碰到）
    ⇒ 有判别力的是"**同为不可解析**的两个来源之间不互相伤害"那一半。两条都写在这里，
    ⛔ 别把前一半读成"旧实现也会红"。

    ## 它在 T3 里还兼任另一件事

    T3 引入过一条**自己的 500 路径**（500 字符的伪造 `X-Real-IP` 撞 `varchar(64)`）；
    现在垃圾来源在**写桶之前**就被拒 ⇒ 那条路径也不存在了（⛔ 不是靠截断兜住的）。
    """

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        creator, _ = User.objects.get_or_create(
            username="openapi-unparsable-probe", defaults={"password": "unused-in-this-test"}
        )
        ApiApp.objects.create(
            name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET, created_by=creator
        )

    def _ip_rows(self):
        return list(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP).values_list(
                "ident", "count"
            )
        )

    def _token_request(self, real_ip, *, client_secret=WRONG_SECRET):
        return Client(SERVER_NAME=self.domain_name).post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                "client_id": APP_KEY,
                "client_secret": client_secret,
            },
            HTTP_X_REAL_IP=real_ip,
            REMOTE_ADDR="127.0.0.1",
        )

    def test_the_token_endpoint_refuses_an_unparsable_source_without_touching_a_bucket(self):
        """① + ②（取令牌端点）：三档垃圾来源都 ⇒ **429 ＋ OAuth2 体**，且**一行桶都不产生**。

        ⚠️ 为什么这一档回 `temporarily_unavailable`（429）而⛔ 不是 `invalid_client`（401）：
        这一档里我们**根本没看凭据**（连 `ApiApp` 都没查）⇒ 用 `invalid_client` 是说假话。
        理由逐条写在 `api/token_views.py` 的类 docstring 里。
        ⚠️ 文案必须**与调用内容无关**：三种垃圾来源、以及"凭据对不对"两种输入，
        答复的 `(status, error, error_description)` 必须**逐字相同**。
        """
        triples = []
        # ⚠️ 三档都是**经 HTTP 真的能产生**的形态：超长 / 非 IP 文本 / 带端口后缀（反代常见误配）。
        # ⛔ **空串不在其列**：`authentication.client_ip()` 把"头存在但是空白"视同**没有**这个头
        # ⇒ 它会退回 `REMOTE_ADDR`（一个合法 IP），这时来源是**可解析**的
        # （空串这一档由 `is_unparsable("")` 那条例句在单元层判，见 `RateBucketSemanticsTests`）。
        for garbage in (UNPARSABLE_X_REAL_IP, "not-an-ip", "203.0.113.7:8080"):
            with self.subTest(garbage=len(garbage)):
                response = self._token_request(garbage)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 429, response.content[:300])
                body = response.json()
                self.assertEqual(set(body), {"error", "error_description"}, response.content[:300])
                self.assertEqual(body["error"], ERROR_TEMPORARILY_UNAVAILABLE)
                self.assertEqual(body["error_description"], RATE_LIMITED_DESCRIPTION)
                triples.append((response.status_code, body["error"], body["error_description"]))
        # 对照：**正确**凭据打同一个垃圾来源 ⇒ 答复逐字相同（文案与调用内容无关）。
        right = self._token_request(UNPARSABLE_X_REAL_IP, client_secret=APP_SECRET)
        self.assertEqual(right.status_code, 429, right.content[:300])
        triples.append((right.status_code, right.json()["error"], right.json()["error_description"]))
        self.assertEqual(len(set(triples)), 1, triples)
        # ② ⛔ 一行桶都不该产生 —— 这条用例里**每一条**请求都来自垃圾来源
        #    （连"兜底额度"也不该写）⇒ 整张表必须是空的。
        self.assertEqual(
            OpenApiRateBucket.objects.count(),
            0,
            "垃圾来源既不该进失败桶、也不该进兜底额度桶",
        )
        self.assertFalse(
            OpenApiRateBucket.objects.filter(ident=UNPARSABLE_IDENT).exists(),
            "占位桶自 R1 起不该再被当作桶使用",
        )

    def test_the_signature_path_refuses_an_unparsable_source_with_the_gate_sentence(self):
        """① + ②（**对外路径**）：⇒ **401 ＋ 本仓信封**（被闸拦住时那**同一句**文案），且不写桶。

        ⚠️ 签名 / Bearer 两条路对"被闸拦住"的既有口径就是 **401 ＋ 本仓信封**、
        文案与该路径"猜 key"那一档**逐字相同**——⛔ 不新造一句话
        （新文案本身会变成一条可分辨的信道）。这里沿用那条口径，⛔ 不改它的形状。

        两条路径都走 Bearer，没有只认签名的生产路由。
        本条仍打详情路径，请求故意带签名头和一个不可解析的 `X-Real-IP`。
        来源不可解析 ⇒ 认证层第一道闸当场拒（401 + 本仓信封），一个桶行都不写。
        文案用 `BEARER_INVALID_CREDENTIALS_MSG`，不新造一句可分辨的话。
        单号不必存在：这件事发生在认证层。
        签名头今天被忽略。留着它们是为了证明旧协议的头不再产生效果
        （换成真令牌会让这条判据少掉那一半）。
        """
        response = Client(SERVER_NAME=self.domain_name).get(
            DETAIL_PATH,
            HTTP_X_TT_APPKEY="probe-ratelimit-key-unknown",
            HTTP_X_TT_TIMESTAMP=str(int(time.time())),
            HTTP_X_TT_SIGN="0" * 64,
            HTTP_X_REAL_IP=UNPARSABLE_X_REAL_IP,
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 401, response.content[:300])
        payload = response.json()
        self.assertEqual(set(payload), {"code", "msg", "data"}, response.content[:300])
        self.assertEqual(payload["code"], 401)
        # 与这条路径"猜凭据"那一档**逐字相同**（被闸拦住时不许给出可分辨的新话术）。
        self.assertEqual(payload["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertEqual(self._ip_rows(), [], self._ip_rows())

    @mock.patch.object(rate_limit, "time", mock.Mock(time=lambda: 1_800_000_000))
    def test_a_flood_from_one_unparsable_source_does_not_change_another_ones_answer(self):
        """⭐ ③ 对照：**互相伤害已消除**（这条有判别力，⛔ 不是恒真）。

        | 序 | 动作 | 期望 |
        |---|---|---|
        | ① | 垃圾来源 **G2**（= `OTHER_UNPARSABLE_X_REAL_IP`）打一条 ⇒ 记下答复 | 429 ＋ 固定文案 |
        | ② | **合法（可解析）来源**打一条 ⇒ 记下答复（**对照的一半**） | 它该得的正常答复（这里用错密钥 ⇒ 401 `invalid_client`） |
        | ③ | 用**另一个**垃圾来源 **G1** 灌 `IP_FAILURE_MAX + 5` 条 | 每条都是 429 |
        | ④ | **再打一次 G2** | 答复与 ① **逐字相同** |
        | ⑤ | **再打一次那个合法来源** | 答复与 ② **逐字相同**（⚠️ 这一半在旧实现下也是绿的，如实登记） |

        ⚠️ **为什么 ④ 有判别力**：旧实现把 G1 与 G2 归并到**同一个**占位桶 ⇒ G1 灌满之后
        G2 会从"凭据那一档的答复"变成"被共享桶挡住" ⇒ ④ 红。
        """
        def reply(real_ip, secret=WRONG_SECRET):
            response = self._token_request(real_ip, client_secret=secret)
            body = response.json()
            return (response.status_code, body.get("error"), body.get("error_description"))

        # ① G2 的基准答复（灌量**之前**）——⚠️ 这里**只记不判形状**（形状由①那条用例钉）：
        #    本用例要判的是"**灌量前后一不一样**"，拿绝对形状去判会让它在变异下
        #    在**基准那一行**就红，判别力那一半（④）反而没被执行到。
        baseline_unparsable = reply(OTHER_UNPARSABLE_X_REAL_IP)
        # ② 合法来源的基准答复（同样是"灌量之前"）
        baseline_legal = reply(SOURCE_IP)

        # ③ G1 灌满阈值还多（旧实现下这些会全部进同一个占位桶）
        # ⚠️ 这里**只判"不许 500"、⛔ 不判形状**：形状是判据①的活（上面那条用例）。
        #    在这里判形状会让本用例在**灌量那一步**就红 ⇒ 真正有判别力的④永远跑不到
        #    （实测过：M-R1 下先红在"灌量那一步的 401 != 429"，那条读数说明不了④的判别力）。
        for _ in range(IP_FAILURE_MAX + 5):
            flooded = self._token_request(UNPARSABLE_X_REAL_IP)
            self.assertNotEqual(flooded.status_code, 500, flooded.content[:200])

        # ④ G2 的答复**不受** G1 的灌量影响（旧实现：被共享桶挡住 ⇒ 红）。
        self.assertEqual(
            reply(OTHER_UNPARSABLE_X_REAL_IP),
            baseline_unparsable,
            "另一个不可解析来源的答复被别人的灌量改变了 ⇒ 共享毒桶又回来了",
        )
        # ⑤ 合法来源照常（⚠️ 这一半在旧实现下也绿，如实登记）。
        self.assertEqual(reply(SOURCE_IP), baseline_legal)
        # ⭐ ② 在"灌过之后"这一侧的形态：**表里唯一那一行就是合法来源自己的**
        #    （它两次凭据错 ⇒ count 2）⇒ 垃圾来源一行都没写；占位桶不存在。
        self.assertEqual(self._ip_rows(), [(SOURCE_IP, 2)], self._ip_rows())
        self.assertFalse(
            OpenApiRateBucket.objects.filter(ident=UNPARSABLE_IDENT).exists(),
            "占位桶自 R1 起不该再被当作桶使用",
        )


class TokenFallbackQuotaTests(TenantTestCase):
    """⭐ 取令牌端点的**兜底额度**（v2 §13.11-1 ② / §10 的口径：⛔ 仍不是配额）。

    三条判据：① 它**含成功与失败**；② 超限回 **429 + OAuth2 体**（⛔ 不是信封）；
    ③ 它和失败闸**不是同一行**（两个阈值不许互相污染）。
    """

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        creator, _ = User.objects.get_or_create(
            username="openapi-quota-probe", defaults={"password": "unused-in-this-test"}
        )
        ApiApp.objects.create(
            name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET, created_by=creator
        )

    def _token_request(self, *, client_id=APP_KEY, client_secret=APP_SECRET, remote_addr=SOURCE_IP):
        return Client(SERVER_NAME=self.domain_name).post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            REMOTE_ADDR=remote_addr,
        )

    def test_the_quota_counts_successes_and_failures_and_returns_an_oauth2_429(self):
        """① + ②：成功与失败**都**吃额度；超限 ⇒ **429 + 恰好两键的 OAuth2 体**。

        形状（额度压到 2）：第 1 次成功（200）、第 2 次失败（401）、第 3 次**成功**也被挡（429）。
        ⚠️ 第 3 次用的是**正确**凭据 ⇒ 429 只可能来自额度那一道闸。
        """
        with mock.patch.object(token_views, "TOKEN_FALLBACK_MAX_REQUESTS", 2):
            first = self._token_request()
            self.assertEqual(first.status_code, 200, first.content[:200])
            second = self._token_request(client_secret=WRONG_SECRET)
            self.assertEqual(second.status_code, 401, second.content[:200])
            third = self._token_request()
            self.assertEqual(third.status_code, 429, third.content[:200])
            body = third.json()
            # ⛔ **不许退化成普通信封**：键集**恰好**两个（⛔ 没有 code / msg / data）。
            self.assertEqual(set(body), {"error", "error_description"})
            self.assertEqual(body["error"], "temporarily_unavailable")
            self.assertEqual(body["error_description"], RATE_LIMITED_DESCRIPTION)

    def test_the_rate_limited_sentence_does_not_depend_on_the_call_content(self):
        """⚠️ 文案**与调用内容无关**（⛔ 不许让"这个 `client_id` 存在吗"从限流文案里漏出去）。

        判据：用**存在**的 `client_id` 与**不存在**的 `client_id` 各打一次超限请求
        ⇒ 两次的 `(状态码, error, error_description)` 三元组**逐字相同**，且文案里**不含**
        任何一个 `client_id` 字面量。
        """
        with mock.patch.object(token_views, "TOKEN_FALLBACK_MAX_REQUESTS", 1):
            self._token_request()  # 用掉唯一的那一次额度
            known = self._token_request(client_id=APP_KEY, client_secret=WRONG_SECRET)
            unknown = self._token_request(client_id=UNKNOWN_KEY, client_secret=WRONG_SECRET)
        triples = [
            (r.status_code, r.json()["error"], r.json()["error_description"])
            for r in (known, unknown)
        ]
        self.assertEqual(triples, [(429, ERROR_TEMPORARILY_UNAVAILABLE, RATE_LIMITED_DESCRIPTION)] * 2)
        for response in (known, unknown):
            self.assertNotIn(APP_KEY, response.json()["error_description"])
            self.assertNotIn(UNKNOWN_KEY, response.json()["error_description"])

    def test_the_quota_row_is_a_separate_row_from_the_failure_gate(self):
        """⭐ ③ 兜底额度与失败闸**不是同一行**：两个阈值不许互相污染。

        形状（额度压到 2）：第 1 次成功、第 2 次失败、第 3 次被额度挡 ⇒
        - `ip_token` 行（兜底额度）：`count == 3`，`ident` 就是**纯来源 IP**；
        - `ip` 行（失败闸）：`count == 1`（只有那一次失败）。
        ⚠️ 若两条闸共用一个 `scope`，这里的 `ip` 行会变成 3 ⇒ 第 20 次**成功**调用之后
        失败闸就会把合法调用方当成"猜了 20 次凭据"挡掉 —— 那正是这一条要拦的形状。
        """
        with mock.patch.object(token_views, "TOKEN_FALLBACK_MAX_REQUESTS", 2):
            self._token_request()
            self._token_request(client_secret=WRONG_SECRET)
            self.assertEqual(self._token_request().status_code, 429)
        quota_rows = list(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP_TOKEN).values_list(
                "ident", "count"
            )
        )
        self.assertEqual(quota_rows, [(SOURCE_IP, 3)], quota_rows)
        failure_rows = list(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP).values_list(
                "ident", "count"
            )
        )
        self.assertEqual(failure_rows, [(SOURCE_IP, 1)], failure_rows)

    def test_the_default_quota_is_the_module_constant(self):
        """⚠️ 额度只有**一处**出处（`TOKEN_FALLBACK_MAX_REQUESTS`），且与 §10 的口径一致。

        ⚠️ 这里钉的是**值本身**（E-0024：钉值不钉常量）—— 改它会红，那正是要的信号
        （调小会打到正在跑的第三方，⛔ 不许顺手改）。
        """
        self.assertEqual(TOKEN_FALLBACK_MAX_REQUESTS, 600)


class CredentialSpecDrivenBucketTests(TenantTestCase):
    """⭐ T2（2026-10-08）：**计数桶由 spec 指定**（⛔ 不是引擎里写死的那一张表）。

    ## 这条判据为什么值钱（它是一条**跨族隔离**的网）

    改造前"哪张桶表"是硬编码在 `rate_limit` 里的（模型名 + 表名 + 唯一约束名都由
    `OpenApiRateBucket` 现取）。打手侧复用同一份引擎时，只要还剩**任何一处**硬编码，
    计数就会落进**上家侧**那张表 —— 症状是"两边共用同一行计数：一边穷举凭据把另一边
    同一来源一起拉黑"，而既有网**全绿**（它们只打 openapi 这条路）。

    ## 判据（两半，缺一半就能被"什么都写进同一张表"骗过）

    拿一份把 `bucket_model` 换成**打手侧那张桶表**（表名 `takerapi_takerapiratebucket`、
    唯一约束名 `uniq_taker_rate_bucket`，**两个都与 openapi 那张不同**）的 spec 调引擎：

    ① 行必须落在**那张**表里，且**两次同窗**的 `count` 累加到 2（那是 `ON CONFLICT ON
       CONSTRAINT` 真的按**约束名**撞上了 —— 硬编码旧约束名会 `ProgrammingError`）；
    ② 只读那一半（`current_count`）读到的是**同一个**计数（读与写必须看同一张表）；
    ③ **对照**：不传 spec（= 默认那一族）时，行落在 openapi 那张表、打手侧那张**一行没有**。
    """

    NOW = 1_800_000_900.0
    WINDOW = 60
    #: 合成来源 IP（文档保留段）。
    IDENT = "203.0.113.97"

    @staticmethod
    def _taker_spec():
        """把 spec 的**桶**换成打手侧那一份（模型 / 三个 scope 一起换，口径与 T1 的模型一致）。"""
        return replace(
            OPENAPI_SPEC,
            bucket_model=TakerApiRateBucket,
            failure_scope=TakerApiRateBucket.Scope.IP,
            app_fallback_scope=TakerApiRateBucket.Scope.APP,
            token_fallback_scope=TakerApiRateBucket.Scope.IP_TOKEN,
        )

    def test_the_rows_land_in_the_table_named_by_the_spec(self):
        spec = self._taker_spec()
        outcomes = [
            rate_limit.consume(
                scope="ip",
                ident=self.IDENT,
                limit=5,
                window_seconds=self.WINDOW,
                now=self.NOW,
                spec=spec,
            )[0]
            for _ in range(2)
        ]
        self.assertEqual(outcomes, [True, True], outcomes)
        # ① 行落在**打手侧**那张表里，且 UPSERT 按**那张表的约束名**累加到了 2。
        self.assertEqual(
            list(
                TakerApiRateBucket.objects.values_list("scope", "ident", "count")
            ),
            [("ip", self.IDENT, 2)],
        )
        # ② 只读那一半读的是同一张表（读到 2，⛔ 不是 0）。
        self.assertEqual(
            rate_limit.current_count(
                scope="ip",
                ident=self.IDENT,
                window_seconds=self.WINDOW,
                now=self.NOW,
                spec=spec,
            ),
            2,
        )
        # ③ 上家侧那张表**一行都没有**（两族不共用桶）。
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)

    def test_the_control_explicitly_naming_the_openapi_spec(self):
        """对照：**显式传上家侧那一份 spec** ⇒ 行落在 openapi 那张表、打手侧那张一行没有。

        ⚠️ 没有这一半，上面那条可能被"引擎其实往**某一张固定表**里写、而那条用例恰好
        只看了另一张"骗过（两张表都断言到，才是"落对了表"）。
        ⚠️⭐ **T2b改准**：本条原名 `test_the_control_without_a_spec_…`、
        靠的是"**不传** `spec` ⇒ 默认落到上家侧"那件事；`spec` 改成**必填 keyword-only**
        之后那条路**不存在了**（不传当场 `TypeError`）⇒ 对照的形状改成**显式写出来**
        （`spec=OPENAPI_SPEC` = 改造前那个默认值所指的东西）。
        ⛔ **判据一条没改**（两张表的读数逐字原样），改的只是"原先由默认值兜住的那件事写出来"。
        """
        rate_limit.consume(
            scope="ip",
            ident=self.IDENT,
            limit=5,
            window_seconds=self.WINDOW,
            now=self.NOW,
            spec=OPENAPI_SPEC,
        )
        self.assertEqual(
            list(OpenApiRateBucket.objects.values_list("scope", "ident", "count")),
            [("ip", self.IDENT, 1)],
        )
        self.assertEqual(TakerApiRateBucket.objects.count(), 0)
