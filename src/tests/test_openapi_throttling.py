"""T6：开放平台**兜底闸**的判据（spec §4-D11 / §9-4 / §9-5）。

## 为什么单独一个文件（⛔ 不是把 `test_openapi_isolation.py` 拆散）

`test_openapi_isolation.py` 那一条是 **spec §11.2 点名的隔离用例**（凭据层 / 数据层 / 跨租户
影响）。本文件证的是**另外一个事实**：那个极宽兜底闸**接上去了没有、接在哪一层**。
两者混在一个文件里，将来改限流的人会在一个叫"isolation"的文件里找它。

## 本文件证什么（每条都写明了"它怎么才会红"）

1. ⭐ **401 先于 429，且被 401 挡掉的请求一个字节都不进桶**。判据的形状是"额度压到 3 之后
   仍然要放行 ③④⑤ 三条合法请求"——若限流被挪到认证之前（或挪进中间件），①② 两次失败
   先把桶记满 ⇒ ③ 当场变 429（红）。
2. ⭐ **统一信封的 429 形状**：HTTP 429 ＋ `code=429` ＋ `data=null` ＋ 标准 `Retry-After`
   （整数秒、≥1）。这条钉的是"超限走 DRF 的 `Throttled`、不自己造状态码"。
3. ⭐ **桶按「应用」分，⛔ 不按端点分**：③④ 打列表端点、⑤⑥ 打详情端点，而 ⑥ 是超限的那一条
   ⇒ 若哪天变成"每个端点各一个桶"，这里会红（那时 ⑥ 还能过）。
4. **`mock.patch` 的目标真的是被读的那个名字**（E-0029 同族）：额度是**模块级常量**
   （`apps.openapi.throttling.OPENAPI_FALLBACK_MAX_REQUESTS`），若有人把它挪成类属性 /
   环境变量 / `settings`，上面几条用例里的补丁会**静默失效**（读到的还是 6000 ⇒ 永远不超限）
   而看起来像"限流没生效" ⇒ 用一条"不接桩时它**不**该被挡、接了桩立刻被挡"的对照钉住。

## 夹具纪律

- 凭据 / 订单号一律**合成值**（`AGENTS.md` §5-C）。
- 桶是 `OpenApiRateBucket` 的行。用例之间靠事务回滚清掉；同一用例里要重置就删行（`_clear_rate_buckets`）。`cache.clear()` 清不到桶。
- 探针走 Bearer。无凭据 / 凭据不对 ⇒ 401 且不进桶；三条合法请求放行；再多一条 ⇒ 429。
- 本文件只用一个租户。跨租户在 `test_openapi_isolation.py`。
"""
import json
from dataclasses import replace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.openapi import throttling, tokens
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, OpenApiRateBucket
from apps.openapi.throttling import (
    OPENAPI_FALLBACK_MAX_REQUESTS,
    SpecDrivenFallbackThrottle,
)
from apps.takerapi.models import TakerApp, TakerApiRateBucket

User = get_user_model()

#: 对外端点的两条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
LIST_PATH = "/api/open/v1/orders/"
DETAIL_TEMPLATE = "/api/open/v1/orders/{order_no}/"

#: 合成凭据与订单号（⛔ 不是任何真实租户的凭据 / 数据）。
APP_KEY = "probe-throttle-key-0001"
APP_SECRET = "probe-throttle-secret-0000000000001"
ORDER_NO = "20261007000000000081"
SOURCE_NO = "TT-THROTTLE-0001"
SOURCE_IP = "203.0.113.31"

#: 业务码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
OK = 0
#: 兜底闸超限的 HTTP 状态码与信封码（两者**同值**）。
THROTTLED_STATUS = 429
#: 压额度用的临时值（⛔ 只有 `mock.patch` 用，别把它写成生产的默认值）。
PATCHED_MAX = 3
def _clear_rate_buckets() -> None:
    """把限流桶的行清空（**只**给"要在同一窗口内重置计数"的用例用）。

    ## 为什么需要它（⛔ 不是"顺手加的工具"）

    T3 之前，桶活在 `LocMemCache` 里 ⇒ `cache.clear()` 就能重置。
    换成 DB 行之后，同一个测试方法**内部**（同一个窗口、同一个 `(scope, ident)`）
    想让计数从 0 重新开始，只能删行 —— 事务回滚只发生在**用例之间**。
    ⛔ 生产代码里**没有**这条路径（限流桶的过期行由 `rate_limit.consume` 的机会式清理负责，
    条件是「`window_start` 早于明确算出的时刻」，⛔ 不是"全表删"）。
    """
    OpenApiRateBucket.objects.all().delete()


#: 超限那句 `msg` 的片段。⚠️ 这句话**不是本仓写的**：它是 DRF 出厂的 `Throttled` 文案，
#: 经 DRF 自带的 zh_Hans 语言包渲染成中文（本仓 `LANGUAGE_CODE=zh-hans`）——**实测读数**见
#: 下面那条用例的断言。只钉片段（⛔ 不钉整句）：整句里带"预计 N 秒后可用"，秒数每次都不同。
THROTTLED_MSG_FRAGMENT = "请求已被限流"


class OpenApiFallbackThrottleTests(TenantTestCase):
    """兜底闸的装配位置与响应形状（单租户；跨租户那一面在 `test_openapi_isolation`）。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        super().setUp()
        # 桶是 DB 行，这条清缓存清不到它。同一用例内重置走 `_clear_rate_buckets`。
        cache.clear()
        _clear_rate_buckets()
        self.domain_name = self.domain.domain
        self.game = Game.objects.create(name="兜底闸用例的游戏")
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            source_order_no=SOURCE_NO,
            project_text="占位项目",
        )
        creator, _ = User.objects.get_or_create(
            username="openapi-throttle-probe",
            defaults={"password": "unused-in-this-test"},
        )
        self.app = ApiApp.objects.create(
            name="兜底闸用例的应用",
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            created_by=creator,
        )
# ⛔ 应用不挂节点 / 游戏范围（那三列已随 v1 面删除，§13.1）。
# ⚠️ 本类的判据（401 先于 429 / 桶不按端点分）**一条未改**。

    def test_unsigned_and_badly_signed_requests_never_fill_the_bucket(self):
        """⭐ **401 先于 429**，且被 401 挡掉的请求**一个字节都不进桶**。

        | 序 | 请求 | 期望 |
        |---|---|---|
        | ① | 不带任何凭据 | 401（`应用凭据无效`） |
        | ② | 凭据不对（形状合法但不存在的令牌） | 401（`应用凭据无效`） |
        | ③④ | 合法，打**列表**端点 | 200 / 200 |
        | ⑤ | 合法，打**详情**端点 | 200（与③④**共桶**） |
        | ⑥ | 合法，打**详情**端点 | 429（额度 = 3，第 4 条超限） |

        ⚠️ 判别力就在"③④⑤ 都得是 200"：若限流被放到认证之前，①② 会先把桶记满 ⇒ ③ 变 429。
        ⚠️ 另一处判别力在"⑤ 还得是 200、⑥ 才是 429"：若桶改成**按端点**分，⑥ 也会是 200。

        ⛔⭐ **改准（T7）**：本用例原先是「不带任何**签名头** / 拿别的 secret **签**」
        （①② 断言 `缺少签名参数` / `签名校验失败`），而 T6 把集合路径切成 Bearer 之后
        ①②③④ 就已经拿不到那些读数了（**T6 留下的既有红**，模块 docstring 的 T7 块记着）。
        T7 起**两条**路径都只认令牌 ⇒ 本用例整体换成令牌：①② 换成"没带 `Authorization`"与
        "带一把不存在的令牌"，断言换成 Bearer 那句（§5.4：两档**有意同一句**）。
        ⚠️ **上表四行期望的判别力一个字未改**（401 先于 429 / 桶按应用不按端点 / 429 的形状）。
        """
        with mock.patch.object(throttling, "OPENAPI_FALLBACK_MAX_REQUESTS", PATCHED_MAX):
            # ① 不带任何凭据（⛔ 它不该进桶）
            naked = Client(SERVER_NAME=self.domain_name).get(
                LIST_PATH, REMOTE_ADDR=SOURCE_IP
            )
            self.assertEqual(naked.status_code, 401, naked.content[:200])
            self.assertEqual(json.loads(naked.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

            # ② 凭据不对（形状合法、但不存在的令牌）
            wrong = self._bearer_get(LIST_PATH, token="0" * 64)
            self.assertEqual(wrong.status_code, 401, wrong.content[:200])
            self.assertEqual(json.loads(wrong.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

            # ③④ 合法（列表端点）——若上面两次失败进过桶，这里就该是 429
            for _ in (3, 4):
                allowed = self._bearer_get(LIST_PATH, data={"source_order_no": SOURCE_NO})
                self.assertEqual(allowed.status_code, 200, allowed.content[:300])
                self.assertEqual(json.loads(allowed.content)["code"], OK)

            # ⑤ 合法（**详情**端点）：与③④共桶 ⇒ 还能过
            still_allowed = self._bearer_get(DETAIL_TEMPLATE.format(order_no=ORDER_NO))
            self.assertEqual(
                still_allowed.status_code, 200, still_allowed.content[:300]
            )

            # ⑥ 第 4 条合法请求 ⇒ 429
            over = self._bearer_get(DETAIL_TEMPLATE.format(order_no=ORDER_NO))
            self.assertEqual(over.status_code, THROTTLED_STATUS, over.content[:300])
            body = json.loads(over.content)
            # 统一信封：code 与 HTTP 状态码**同值**（传输层失败不伪装成 200）
            self.assertEqual(body["code"], THROTTLED_STATUS)
            self.assertIsNone(body["data"])
            # ⚠️ `msg` 是 **DRF 出厂那句**（不是本仓写的）：本仓 `LANGUAGE_CODE=zh-hans`，
            # DRF 自带 zh_Hans 语言包 ⇒ 实际渲染成中文。这里只钉**片段**（照本仓对
            # DRF 中文默认文案的既有做法）：把整句冻死会把框架的措辞连同秒数一起钉住。
            self.assertIn(THROTTLED_MSG_FRAGMENT, body["msg"])
            # `Retry-After` 必须是"还要等几秒"的整数（DRF 由 `wait()` 算出来；⛔ 不能是 0）
            retry_after = over.headers.get("Retry-After")
            self.assertIsNotNone(retry_after, dict(over.headers))
            self.assertTrue(int(retry_after) >= 1, retry_after)

    def test_the_default_quota_is_the_module_constant_and_the_patch_reaches_it(self):
        """⚠️ `mock.patch` 的目标**必须真的是**被读的那个名字（E-0029 同族）。

        先跑**不接桩**的对照（额度是那个模块级常量 ⇒ 两次都该放行），再接桩压到 1
        ⇒ 立刻能挡住。若常量被挪成类属性 / `settings`，第二段会红 —— 那正是"补丁静默失效"
        这个坑的形状（它看起来像"限流没生效"，根因却在测试接错了桩）。
        ⚠️ 这里**不碰 HTTP**：它只判"被判的那个值从哪来"，走的是限流器自己的接口。

        两半之间必须删掉桶里的行。`cache.clear()` 清不到 DB 行，接桩后的第一次会读到
        前面已经记下的次数，当场返回 False。不接桩 ⇒ `[True, True]`；压到 1 ⇒ `[True, False]`。
        """

        class _FakeRequest:
            auth = self.app

        throttle = throttling.ApiAppFallbackThrottle()
        # 不接桩：额度是那个模块常量（默认 6000）⇒ 第二条不该被挡
        self.assertEqual(
            [throttle.allow_request(_FakeRequest(), None) for _ in range(2)],
            [True, True],
        )
        with mock.patch.object(throttling, "OPENAPI_FALLBACK_MAX_REQUESTS", 1):
            # 清的是表里的行。`cache.clear()` 清不到桶，接桩后会接着用前面记下的次数。
            _clear_rate_buckets()
            self.assertIs(throttle.allow_request(_FakeRequest(), None), True)
            self.assertIs(throttle.allow_request(_FakeRequest(), None), False)
            self.assertGreaterEqual(throttle.wait(), 1)
        # 与生产常量**同值**：数值被改掉时这里会红（⛔ 别把它当"顺手写个 6000"）
        self.assertEqual(OPENAPI_FALLBACK_MAX_REQUESTS, 6000)

    # ── 工具 ───────────────────────────────────────────────────────────
    def _bearer_get(self, path, data=None, *, token=None):
        """带 **Bearer 令牌**发一条 GET（T7 起**两条**对外路径都只认令牌）。

        ⚠️ 令牌当场签发（`tokens.issue`，用本文件那把合成凭据）——⛔ 不抄签发逻辑。
        `token=` 用来构造"带了凭据但凭据不对"那一档（形状合法、值不存在）。
        """
        token = tokens.issue(APP_KEY, APP_SECRET) if token is None else token
        return Client(SERVER_NAME=self.domain_name).get(
            path,
            data or {},
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
        )


class PerFamilyQuotaTests(TenantTestCase):
    """⭐⭐ T2 修复轮（2026-10-08）：兜底闸的**额度是按族可区分的**，⛔ 不是"谁继承谁就用谁的"。

    ## 这条判据为什么值钱

    改造前额度读的是**本模块的模块级名字**（`OPENAPI_FALLBACK_MAX_REQUESTS`，既有打桩用例
    要求它）。而打手侧（T2b）要复用同一个闸 ⇒ 若它只覆盖 `spec` 就完事，它会**静默用上家侧的
    额度**（6000/分钟）：闸照常工作、请求照常放行，**只有量级是别人的**这一件事没有任何症状。
    ⇒ 本类把"额度按族给"做成可红的判据，并钉住"漏给额度 ⇒ **响亮**失败"。

    ## 判据（四条，⛔ 不接受只有前两条）

    ① 子类**显式给**自己那一族的额度 ⇒ 用的是**那一份**（第 1 条放行、第 2 条被挡），
       且计数落在**它自己那一族的桶表**里、`OpenApiRateBucket` **一行没多**；
    ② **对照**：同样两条调用，上家侧那个类（额度 6000）⇒ 两次都放行
       （否则 ① 可能是"这个类本来就只放行一次"的恒真式）；
    ③ 子类**忘了给额度** ⇒ `NotImplementedError`（响亮），⛔ 不是静默拿上家侧那份；
    ④ 子类**忘了声明 spec** ⇒ `AttributeError`（响亮），⛔ 不是静默落上家侧的桶。

    ⚠️ 本任务⛔ **不实现打手侧**（那是 T2b）：这里的子类是**用例内**的探针，
    它模拟的是 T2b 那个形状（继承基类 + 显式给 `spec` 与额度）。
    """

    def setUp(self):
        connection.set_tenant(self.tenant)
        super().setUp()
        _clear_rate_buckets()
        TakerApiRateBucket.objects.all().delete()
        creator, _ = User.objects.get_or_create(
            username="openapi-throttle-quota-probe",
            defaults={"password": "unused-in-this-test"},
        )
        self.app = ApiApp.objects.create(
            name="额度按族探针的应用",
            app_key="probe-quota-key-0001",
            app_secret="probe-quota-secret-0000000001",
            created_by=creator,
        )
        self.taker_app = TakerApp.objects.create(
            name="额度按族探针的打手应用甲",
            app_key="probe-quota-taker-key-0001",
            app_secret="probe-quota-taker-secret-000001",
            taker=creator,
            created_by=creator,
        )

    @staticmethod
    def _probe_throttle_class(max_requests: int, window_seconds: int):
        """打手侧那个形状的探针类：**显式**给 spec 与额度（T2b 落地时照这个形状写）。"""

        class _ProbeFallbackThrottle(SpecDrivenFallbackThrottle):
            spec = replace(
                OPENAPI_SPEC,
                bucket_model=TakerApiRateBucket,
                app_model=TakerApp,
                # ⚠️ 额度 / 窗口也刻意取**不同值**：这条判据要能分辨"读的是哪一份"。
                app_fallback_max_requests=max_requests,
                app_fallback_window_seconds=window_seconds,
            )

            def fallback_max_requests(self) -> int:
                return max_requests

            def fallback_window_seconds(self) -> int:
                return window_seconds

        return _ProbeFallbackThrottle

    def test_the_declared_family_quota_is_the_one_that_applies(self):
        """① 子类显式给的额度生效；行落在子类那一族的桶表里。"""
        throttle = self._probe_throttle_class(1, 60)()

        class _FakeRequest:
            auth = self.taker_app

        self.assertIs(throttle.allow_request(_FakeRequest(), None), True)
        self.assertIs(throttle.allow_request(_FakeRequest(), None), False)
        self.assertGreaterEqual(throttle.wait(), 1)
        self.assertEqual(
            list(
                TakerApiRateBucket.objects.values_list("scope", "ident", "count")
            ),
            [("app", "probe-quota-taker-key-0001", 2)],
        )
        # ⛔ 上家侧那张桶表**一行都没多**（这正是"额度与桶都按族"的判据）。
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)

    def test_the_control_family_keeps_its_own_wide_quota(self):
        """② 对照：上家侧那个类仍然是 6000 的宽额度 ⇒ 两条都放行（⛔ 不是"只放行一次"）。"""
        throttle = throttling.ApiAppFallbackThrottle()

        class _FakeRequest:
            auth = self.app

        self.assertEqual(
            [throttle.allow_request(_FakeRequest(), None) for _ in range(2)],
            [True, True],
        )
        self.assertEqual(
            list(OpenApiRateBucket.objects.values_list("scope", "ident", "count")),
            [("app", "probe-quota-key-0001", 2)],
        )
        self.assertEqual(TakerApiRateBucket.objects.count(), 0)

    def test_a_subclass_that_forgets_its_quota_fails_loudly(self):
        """③ 漏给额度 ⇒ `NotImplementedError`（⛔ 不是静默拿上家侧那份）。"""

        class _NoQuotaThrottle(SpecDrivenFallbackThrottle):
            spec = replace(OPENAPI_SPEC, bucket_model=TakerApiRateBucket)

        throttle = _NoQuotaThrottle()

        class _FakeRequest:
            auth = self.taker_app

        with self.assertRaises(NotImplementedError):
            throttle.allow_request(_FakeRequest(), None)
        with self.assertRaises(NotImplementedError):
            throttle.fallback_max_requests()
        with self.assertRaises(NotImplementedError):
            throttle.fallback_window_seconds()
        # ⛔ 一行桶都没写（响亮失败发生在**任何库读写之前**）。
        self.assertEqual(TakerApiRateBucket.objects.count(), 0)
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)

    def test_a_subclass_that_forgets_its_spec_fails_loudly(self):
        """④ 漏声明 spec ⇒ `AttributeError`（⛔ 不是静默落上家侧的桶）。"""

        class _NoSpecThrottle(SpecDrivenFallbackThrottle):
            def fallback_max_requests(self) -> int:
                return 1

            def fallback_window_seconds(self) -> int:
                return 60

        throttle = _NoSpecThrottle()

        class _FakeRequest:
            auth = self.app

        with self.assertRaises(AttributeError):
            throttle.allow_request(_FakeRequest(), None)
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)
        self.assertEqual(TakerApiRateBucket.objects.count(), 0)
