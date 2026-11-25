"""打手侧凭据族（T2b）的**族间隔离判据**：跨凭据类型 / 台账 / 桶 / 额度 / 四张表的 spec。

spec：`docs/specs/2026-10-08-taker-open-api-design.md`（§4.1 的四条硬口径、§9-2 / §9-15 / §9-16）。

## 本文件证什么（四条，逐条对应 spec 的一行）

| # | 判据 | spec |
|---|---|---|
| 1 | **跨凭据类型双向 401** —— 拿 `TakerApp` 的令牌打上家侧认证器 ⇒ 401；拿 `ApiApp` 的令牌打打手侧认证器 ⇒ 401 | §9-2（承重判据） |
| 2 | **幂等台账不串台** —— 上家侧写一次之后，**pk 相同**的 `TakerApp` 用**同一个 key** 打打手侧 ⇒ 出新结果、⛔ 不回放上家侧那一份 | §9-15 |
| 3 | **限流 / 失败计数不串台** —— 打满一族之后**同一 IP** 打另一族**仍然可用**（两向）；判据读**两张桶表**的行 | §9-16 |
| 4 | **两族额度不同** + `spec_of` 的响亮失败 + 四张新表各自的 `spec` 用对了 + **漏传 `spec` 响亮失败** | §4.1-1 / §4.1-3 / §4.1-4 |
| 5 | ⭐ **停用 / 软删打手的对外令牌立即失效**（Bearer 引擎第 5.5 步 + 取令牌流程各一条口；与「令牌无效」**同形状**拒绝，防探测） | D-0333（业务方拍板「需要的」；旧账 = 第五轮安全审计 M3 半边） |

## ⚠️ 令牌一律**从取令牌端点拿到**（⛔ 不手搓夹具）

spec §9-2 的原文要求"真的从取令牌端点拿到令牌再用"。所以：
- 打手侧的令牌走 **`/api/taker/oauth/token`**（`TakerTokenView`，经**真路由** + 真 `Client`）；
- 上家侧的令牌走 **`/api/open/oauth/token`**（同一条形状，既有端点）；
⛔ 没有一处是 `tokens.issue(...)` 直接造的。⚠️ 那两条端点各自的成功体是**裸三字段**
（⛔ 没有本仓信封），判据从"调用方真正看到的东西"里取 `access_token`。

## 为什么用 `APIRequestFactory` + 本文件内的探针视图（⛔ 不建 urls 模块）

与 `tests/test_openapi_auth.py` **同一条理由**（那份 docstring 逐字写着它）：
这些判据的对象是**认证类**，而不是某一条端点的契约。为了测它们去建一个 `urls.py` 并把路由
挂进 `config/urls.py`，会**顺手**撞上一张承重网（`tests/test_operation_log_coverage.py`
按 `get_resolver()` 枚举真实 urlconf ⇒ 新端点会被它抓，且本线今天只有**一条**真实路由）。
⇒ 探针视图定义在**本文件内**，装配方式（`authentication_classes` + 视图里碰 `request.auth`
触发惰性认证）与真端点**逐字相同**。

⚠️ 而"**令牌从哪来**"那一半**仍然走真路由**（`Client` 打真实 URL）—— 两条要求**同时**满足：
认证类是探针的，令牌是端点的。

## 断言写在哪一层

响应**状态码** + 响应信封的 `code` / `msg`（`E-0063`：不看 `response.data is not None`）+ **数据库行**。
`msg` 用**字面量**比对（`E-0024`：钉值不钉常量），并另有钉子把 spec 的值与字面量对齐。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C）。
"""
import json
from dataclasses import replace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.accounts.models import StaffProfile
from apps.core.envelope import rendered_response
from apps.openapi import idempotency as openapi_idempotency
from apps.openapi.api.token_views import TOKEN_FALLBACK_MAX_REQUESTS as OPENAPI_TOKEN_QUOTA
from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec, spec_of
from apps.openapi.models import ApiApp, OpenApiIdempotency, OpenApiRateBucket
from apps.openapi.throttling import (
    OPENAPI_FALLBACK_MAX_REQUESTS,
    ApiAppFallbackThrottle,
)
from apps.takerapi import throttling as taker_throttling
from apps.takerapi.api.hall_views import HallListView
from apps.takerapi.api.order_write_views import HallClaimView
from apps.takerapi.api.token_views import (
    TAKER_TOKEN_FALLBACK_MAX_REQUESTS,
    TakerTokenView,
)
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import TakerApiIdempotency, TakerApiRateBucket, TakerApp
from apps.takerapi.throttling import TakerAppFallbackThrottle

# ── 路径与合成凭据（⛔ 与任何真实租户 / 真实业务数据无关）──────────────────────
TAKER_TOKEN_PATH = "/api/taker/oauth/token"
OPENAPI_TOKEN_PATH = "/api/open/oauth/token"
PROBE_PATH = "/api/taker/v1/orders/PROBE-1/"

GRANT_TYPE = "client_credentials"
TAKER_KEY = "takerappkey000001"
TAKER_SECRET = "taker-app-secret-00000000000001"
OPENAPI_KEY = "openappkey0000001"
OPENAPI_SECRET = "open-app-secret-000000000001"
WRONG_SECRET = "wrong-secret-000000000000000000"
UNKNOWN_KEY = "nosuchappkey0000001"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.11"
OTHER_SOURCE_IP = "198.51.100.21"

#: `api/open/v1/…` 那条真路由（跨凭据类型那一半打它）。
OPENAPI_BUSINESS_PATH = "/api/open/v1/orders/"

#: 一句话文案的**字面量**（E-0024：钉值）。两族**今天逐字相同**，而它们是**两份**数据
#: ⇒ 最后一节逐族钉一遍（谁改了哪一族，哪一族红）。
INVALID_CREDENTIALS_MSG = "应用凭据无效"


def _user(username: str):
    user, _ = get_user_model().objects.get_or_create(
        username=username, defaults={"password": "unused-in-this-test"}
    )
    return user


# ── 探针视图：**打手侧**认证类的装配（与真端点逐字相同的手法）─────────────────
@api_view(["GET"])
@authentication_classes([TakerAppBearerAuthentication])
@permission_classes([AllowAny])
def taker_probe_view(request):
    """回显"打手侧认证把什么东西挂到了请求上"。

    ⚠️ 先碰 `request.auth` / `request.user`：DRF 的认证是**惰性**的，视图不碰它就不会跑
    （那样本探针会恒 200、零判别力）。
    """
    app = getattr(request, "_api_app", None)
    return Response(
        {
            "app_key": app.app_key if app is not None else None,
            "auth_is_taker_app": isinstance(request.auth, TakerApp),
            "auth_is_api_app": isinstance(request.auth, ApiApp),
            "user_is_authenticated": request.user.is_authenticated,
        }
    )


TAKER_PROBE_HANDLER = taker_probe_view.view_class.as_view()


class _FamilyFixture(TenantTestCase):
    """两族的夹具：一个租户域名上的 `Client`（走真路由换令牌）+ 一个打手侧应用。"""

    def setUp(self):
        super().setUp()
        self.client = Client(SERVER_NAME=self.domain.domain)
        self.factory = APIRequestFactory()
        self.taker_app = self.make_taker_app()
        self.api_app = self.make_api_app()

    # ── 建行 ────────────────────────────────────────────────────────────
    def make_taker_app(self, *, name="探针打手应用甲", app_key=TAKER_KEY,
                       app_secret=TAKER_SECRET, **kwargs):
        """建一行 `TakerApp`（`taker` / `created_by` 都必填 ⇒ 给本用例自己造的账号）。"""
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=kwargs.pop("taker", _user("takerapi-t2b-taker")),
            created_by=kwargs.pop("created_by", _user("takerapi-t2b-admin")),
            **kwargs,
        )

    def make_api_app(self, *, name="探针上家应用甲", app_key=OPENAPI_KEY,
                     app_secret=OPENAPI_SECRET, **kwargs):
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            created_by=kwargs.pop("created_by", _user("takerapi-t2b-admin")),
            **kwargs,
        )

    # ── 换令牌：**真的走端点**（⛔ 不手搓夹具）──────────────────────────
    def _token_request(self, path, *, client_id, client_secret, remote_addr=SOURCE_IP):
        return self.client.post(
            path,
            {
                "grant_type": GRANT_TYPE,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            REMOTE_ADDR=remote_addr,
        )

    def taker_token_request(self, **kwargs):
        kwargs.setdefault("client_id", TAKER_KEY)
        kwargs.setdefault("client_secret", TAKER_SECRET)
        return self._token_request(TAKER_TOKEN_PATH, **kwargs)

    def openapi_token_request(self, **kwargs):
        kwargs.setdefault("client_id", OPENAPI_KEY)
        kwargs.setdefault("client_secret", OPENAPI_SECRET)
        return self._token_request(OPENAPI_TOKEN_PATH, **kwargs)

    def issue_taker_token(self) -> str:
        """走**真实的**打手侧取令牌端点换一个令牌，回 `access_token`（⛔ 不是自己签一个）。"""
        response = self.taker_token_request()
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        # ⚠️ 成功体是**裸三字段**（⛔ 没有本仓信封的 code/msg/data）：从"调用方真正看到的
        #    载荷"里取令牌，⛔ 不从数据库对象拼。
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    def issue_openapi_token(self) -> str:
        """同上，走**上家侧**那条取令牌端点。"""
        response = self.openapi_token_request()
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    # ── 打起手侧探针（走完整 DRF 周期）─────────────────────────────────
    def build_taker_request(self, token, *, remote_addr=SOURCE_IP, path=PROBE_PATH):
        request = self.factory.get(path, REMOTE_ADDR=remote_addr)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return request

    def call_taker_probe(self, token, **kwargs):
        return rendered_response(TAKER_PROBE_HANDLER(self.build_taker_request(token, **kwargs)))


class CrossCredentialTypeTests(_FamilyFixture):
    """⭐ spec §9-2：**跨凭据类型双向 401**（这是"两族分开"的承重判据，不是附带）。

    ## 为什么它是**结构性**的、⛔ 不是靠每条端点记得过滤

    两个认证器各自只查自己那张表（表来自各自的 `spec.app_model`）：打手侧的令牌里的 `ak`
    就是 `TakerApp.app_key` ⇒ 在上家侧认证器查的 `ApiApp` 表里**必然查不到**
    ⇒ 未命中那一支 ⇒ 401（走哑 HMAC，时序不可分辨）。反之亦然。
    ⇒ ⛔ 漏一条端点的过滤**不会**让这条变成静默全租户（那正是 spec §2 否掉"一张表加 kind"的理由）。
    """

    def test_a_taker_token_is_rejected_by_the_openapi_authenticator(self):
        """拿 `TakerApp` 的令牌打**上家侧认证器** ⇒ 抛 401「应用凭据无效」。"""
        from rest_framework import exceptions

        token = self.issue_taker_token()
        request = self.factory.get(OPENAPI_BUSINESS_PATH, REMOTE_ADDR=SOURCE_IP)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        with self.assertRaises(exceptions.AuthenticationFailed) as caught:
            ApiAppBearerAuthentication().authenticate(request)
        self.assertEqual(str(caught.exception.detail), INVALID_CREDENTIALS_MSG)
        # ⭐ 承重的一半：不是"因为别的原因被拒"，而是**在那张表里查不到这个 ak**。
        self.assertFalse(ApiApp.objects.filter(app_key=TAKER_KEY).exists())
        self.assertTrue(TakerApp.objects.filter(app_key=TAKER_KEY).exists())

    def test_an_openapi_token_is_rejected_by_the_taker_authenticator(self):
        """拿 `ApiApp` 的令牌打**打手侧**探针（= 打手侧的认证器） ⇒ **401**。"""
        token = self.issue_openapi_token()
        response = self.call_taker_probe(token)
        self.assertEqual(response.status_code, 401, response.content[:200])
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], 401)
        self.assertEqual(payload["msg"], INVALID_CREDENTIALS_MSG)
        self.assertIsNone(payload["data"])
        self.assertFalse(TakerApp.objects.filter(app_key=OPENAPI_KEY).exists())

    def test_the_taker_authenticator_accepts_its_own_token(self):
        """⭐ **正对照**（缺了它上面两条对"什么都拒"的实现也绿）：打手侧自己的令牌 ⇒ 放行。

        ⚠️ 探针回的是**裸载荷**（对外端点是 `APIView` ⇒ 通过时没有 `code` 键可断言），
        判据打在"认证真的把那个 `TakerApp` 挂上去了"这一件事上。
        """
        token = self.issue_taker_token()
        response = self.call_taker_probe(token)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(body["app_key"], TAKER_KEY)
        self.assertIs(body["auth_is_taker_app"], True)
        self.assertIs(body["auth_is_api_app"], False)

    def test_the_two_authenticators_are_two_families_not_one_class(self):
        """⛔ 两个认证器**不是同一个类**（"不抄第二份"⛔ 不等于"共用同一个类"）。

        判别力：若打手侧那个认证类被写成 `= ApiAppBearerAuthentication`（或没覆盖 `spec`），
        上面几条 401 判据会以"两边都查同一张表"的形式**变绿**（打手令牌在上家侧表里也查不到、
        上家令牌在打手侧表里也查不到 —— 恰好都 401），而这条会红。
        ⚠️ 这正是 spec §2 要的形状：**同一个基类、两份 spec**（⛔ 不是两份判据、也⛔ 不是同一个类）。
        """
        self.assertIsNot(TakerAppBearerAuthentication, ApiAppBearerAuthentication)
        self.assertIs(TakerAppBearerAuthentication.spec, TAKER_SPEC)
        self.assertIs(ApiAppBearerAuthentication.spec, OPENAPI_SPEC)
        # ⚠️ 两族的凭据表 / 台账表 / 桶表**三张都不能是同一个类对象**。
        self.assertIsNot(TAKER_SPEC.app_model, OPENAPI_SPEC.app_model)
        self.assertIsNot(TAKER_SPEC.idem_model, OPENAPI_SPEC.idem_model)
        self.assertIsNot(TAKER_SPEC.bucket_model, OPENAPI_SPEC.bucket_model)
        self.assertNotEqual(
            TAKER_SPEC.bucket_model._meta.db_table,
            OPENAPI_SPEC.bucket_model._meta.db_table,
        )

    def test_a_wrong_spec_would_let_the_foreign_token_through(self):
        """⭐ **判别力自证**（不是复制上面那几条，而是证明它们**真的能红**）。

        做法：临时把打手侧认证类的 `spec` 换成**一份指向 `ApiApp` 的** spec
        （= spec §2 否掉的那个"两族共用一张表 / 漏声明 `spec`"的形态）
        ⇒ 上家侧的令牌**当场通过**（探针 200）。
        ⇒ 反过来说明：上面那条 401 判据**确实是** `spec` 选对了表带来的，
        ⛔ 不是"令牌本来就谁都认不过"那种恒真的绿。
        ⚠️ 打完**立刻还原**（`try/finally`）—— 本文件其余用例都依赖那个正确的 `spec`。
        """
        token = self.issue_openapi_token()
        # 先证"正常形态下它是 401"（否则下面的 200 说明不了任何事）。
        self.assertEqual(self.call_taker_probe(token).status_code, 401)

        wrong_spec = replace(
            TAKER_SPEC,
            app_model=ApiApp,
            enabled_state_value=OPENAPI_SPEC.enabled_state_value,
            # ⚠️ D-0333：`bound_user_field` 也得换成这一族的真值——`ApiApp` **没有**
            # `taker` 这一列，继承打手侧的 `"taker"` 会让引擎在取属性时 500（响亮失败），
            # 本用例要的是"指错表 ⇒ 令牌通过"这个旧形状本身。与上面
            # `enabled_state_value` 的换法同一手法（族自己的值随表一起换）。
            bound_user_field=None,
        )
        original = TakerAppBearerAuthentication.spec
        TakerAppBearerAuthentication.spec = wrong_spec
        try:
            response = self.call_taker_probe(token)
            self.assertEqual(
                response.status_code,
                200,
                "把 spec 指向 ApiApp 之后，上家侧的令牌居然仍被拒 ⇒ 上面那条 401 判据"
                "分辨不出'spec 选对了表'与'令牌本来就过不去'",
            )
            self.assertIs(json.loads(response.content)["auth_is_api_app"], True)
        finally:
            TakerAppBearerAuthentication.spec = original
        # 还原已生效（自证：还原后同一把令牌又变回 401）。
        self.assertEqual(self.call_taker_probe(token).status_code, 401)


class LedgerIsolationTests(_FamilyFixture):
    """⭐ spec §9-15：**幂等台账不串台**（两族各写各的台账）。

    ## 被挡的形状是什么

    spec §4 那张表的第 1 项：`_find_existing` 若硬编码 `OpenApiIdempotency`，那么打手侧的
    `filter(app=<TakerApp 实例>, key=…)` 会**按 pk 比较** ⇒ 命中一个 **pk 相同的 `ApiApp` 行**
    ⇒ 打手侧收到**上家侧应用的回放响应**。⚠️ 认证层那两条 401 判据**抓不到**这条。

    ## 怎么造出"pk 相同"

    ⚠️ 两个 schema 的主键序列各自从 1 开始，但**同一张库里的两张表**各有自己的序列
    ⇒ 本项目里 `TakerApp.objects.create()` 与 `ApiApp.objects.create()` **通常**都会拿到 pk=1。
    ⛔ 本用例**不靠巧合**：建行时就把主键写成同一个值（`objects.create(id=…)`），
    并在用例里**断言它们确实相同**（否则"pk 相同"这个前提没成立，被测形状压根没出现
    ⇒ 绿得没有意义）。
    ⚠️ 用 `create(id=…)` 而**不是** `update(pk=…)`：后者会抛
    `FieldDoesNotExist: TakerApp has no field named 'pk'`（`QuerySet.update` 不吃
    那个别名，只认真列名）—— T2b 执行期实测踩到过。
    """

    #: 两行应用**故意共用的主键**（= spec §4 第 1 项"按 pk 比较"那个形状的构造）。
    SHARED_PK = 7

    def setUp(self):
        super().setUp()
        # ⚠️ 先删掉夹具里那两行，再用**显式主键**重建（理由见类 docstring）。
        TakerApp.objects.all().delete()
        ApiApp.objects.all().delete()
        self.taker_app = self.make_taker_app(id=self.SHARED_PK)
        self.api_app = self.make_api_app(id=self.SHARED_PK)
        self.shared_key = "shared-idem-key-0001"   # 8–128 字符，两族用**同一个**

    def test_the_two_rows_really_share_the_same_pk(self):
        """⚠️ **前置条件自证**：两行应用的 pk 真的相同（否则本类的判据恒真）。

        这条不是同义反复：它挡的是"哪天建行方式改了 / 主键不生效"
        ⇒ 那种情况下后两条用例会**静默失去判别力**（被测的"pk 撞车"形状压根没出现）。
        """
        self.assertEqual(self.taker_app.pk, self.api_app.pk)
        self.assertEqual(self.taker_app.pk, self.SHARED_PK)

    def test_the_foreign_ledger_row_is_not_replayed_by_the_taker_family(self):
        """⭐ 上家侧写过同一个 key 之后，打手侧拿**同一个 key** ⇒ **出新结果**、⛔ 不回放别人的。

        做法（⛔ 不手搓令牌）：走**真实的**打手侧取令牌端点，带同一颗 `Idempotency-Key`。
        ⚠️ 取令牌端点是**读**形状（不接幂等入口），所以这里同时用**台账表**那一层做判据：
        下面 `test_the_taker_ledger_keeps_its_own_row_for_the_same_key` 直接打
        `openapi_idempotency._find_existing`（那正是 spec §4 第 1 项点名的入口）。
        """
        # 上家侧先落一行（那份快照就是"别人的回放内容"）。
        foreign_snapshot = {"code": 0, "msg": "上家侧的回放内容", "data": {"who": "openapi"}}
        OpenApiIdempotency.objects.create(
            app=self.api_app,
            key=self.shared_key,
            method="POST",
            path="/api/open/v1/orders/",
            request_hash="a" * 64,
            response_snapshot=foreign_snapshot,
            status_code=200,
        )
        self.assertEqual(OpenApiIdempotency.objects.filter(key=self.shared_key).count(), 1)

        # ⛔ 打手侧那一族的入口（`_find_existing`，收的是 **spec**）不许看见它。
        found = openapi_idempotency._find_existing(
            app=self.taker_app, key=self.shared_key, spec=TAKER_SPEC
        )
        self.assertIsNone(
            found,
            "打手侧的幂等入口在上家侧那张台账里命中了同一号行 ⇒ 打手侧会收到**上家侧的回放**"
            "（spec §9-15 / §4 第 1 项）。根因形状：`_find_existing` 没有经 `spec.idem_model`。",
        )
        # 反面对照：**同一族**里那颗键确实还在（否则上面那句 `assertIsNone` 可能是"表里根本没行"）。
        same_family = openapi_idempotency._find_existing(
            app=self.api_app, key=self.shared_key, spec=OPENAPI_SPEC
        )
        self.assertIsNotNone(same_family)
        self.assertEqual(same_family.response_snapshot, foreign_snapshot)

    def test_the_taker_ledger_keeps_its_own_row_for_the_same_key(self):
        """⭐ 反向：打手侧落了行之后，**上家侧**同 key 查不到它（两向都不串台）。"""
        TakerApiIdempotency.objects.create(
            app=self.taker_app,
            key=self.shared_key,
            method="POST",
            path="/api/taker/v1/hall/TT-T2B-0001/claim/",
            request_hash="b" * 64,
            response_snapshot={"code": 0, "msg": "打手侧自己的内容", "data": None},
            status_code=200,
        )
        self.assertIsNone(
            openapi_idempotency._find_existing(
                app=self.api_app, key=self.shared_key, spec=OPENAPI_SPEC
            ),
            "上家侧的幂等入口在打手侧那张台账里命中了同一号行 ⇒ 上家侧会收到打手侧的回放",
        )
        self.assertIsNotNone(
            openapi_idempotency._find_existing(
                app=self.taker_app, key=self.shared_key, spec=TAKER_SPEC
            )
        )
        # 两张台账表**各自只有自己那一行**（同一个 key 值住在**两张不同的表**里）。
        # ⚠️ 上家侧那张是 **0 行**（本用例只写了打手侧那一次）—— 这个 0 正是判别力所在：
        #    它证明"同一个 key 值"确实**没有**被写进上家侧那张表（若两族共用一张表，
        #    这里会是 1，而上面那句 `assertIsNone` 也会红）。
        self.assertEqual(OpenApiIdempotency.objects.filter(key=self.shared_key).count(), 0)
        self.assertEqual(TakerApiIdempotency.objects.filter(key=self.shared_key).count(), 1)

    def test_the_wrong_idempotency_model_is_a_loud_failure_not_silent_replay(self):
        """⭐ **判别力自证**：把 spec 的台账指成上家侧那张 ⇒ **响亮**（⛔ 不是静默回放）。

        ⚠️ 这条钉的是一件**结构性**的事：`spec.idem_model` 是**唯一**决定"查哪张台账"的东西。
        把它指错、却仍然拿一个 `TakerApp` 实例去查 `OpenApiIdempotency`
        （那一列的外键指向 `ApiApp`）⇒ **`ValueError` 当场炸**，⛔ 不是"悄悄返回别人的行"。
        ⇒ 反过来说明上面那两条 `assertIsNone` 打的是**真的那个入口**：
        若 `_find_existing` 硬编码了台账模型，它连"传 `spec` 有什么后果"都不会体现出来
        （本用例会以 `ValueError not raised` 红）。

        ⚠️ 走的是**公开入口**（`seen_source_response`，一个真的调用 `_find_existing` 的函数），
        ⛔ 不是直接调私有函数 —— 这样"哪张台账"这条判据落在**生产路径**上。
        """
        # 上家侧先有一行（"别人的回放内容"确实存在，⛔ 不是空表）。
        OpenApiIdempotency.objects.create(
            app=self.api_app,
            key=f"{OPENAPI_SPEC.source_key_prefix}TT-T2B-SRC-0001",
            method="POST",
            path="/api/open/v1/orders/",
            request_hash="c" * 64,
            response_snapshot={"code": 0, "msg": "上家侧内容", "data": None},
            status_code=200,
        )
        wrong_spec = replace(TAKER_SPEC, idem_model=OpenApiIdempotency)
        with self.assertRaises(ValueError):
            openapi_idempotency.seen_source_response(
                app=self.taker_app, source_order_no="TT-T2B-SRC-0001", spec=wrong_spec
            )
        # 对照：**正确那一份** spec 下同一个调用（它只是查不到 ⇒ 回 `None`，⛔ 不抛）。
        # ⚠️ 没有这一半，"上面那句 raise" 可能来自与 spec 无关的原因（例如 app 类型不对）。
        self.assertIsNone(
            openapi_idempotency.seen_source_response(
                app=self.taker_app, source_order_no="TT-T2B-SRC-0001", spec=TAKER_SPEC
            )
        )


class RateBucketIsolationTests(_FamilyFixture):
    """⭐ spec §9-16（+ §4.1-4）：**限流 / 失败计数不串台**，判据读**两张桶表**的行。

    被挡的形状（spec §4 第 5 / 6 / 9 项）：一族穷举凭据会把**另一族同一来源 IP** 一起挡掉
    —— 而 `test_openapi_*` 只打 openapi 路径，**抓不到**。

    ⚠️ 判据**不看日志**：读 `TakerApiRateBucket` / `OpenApiRateBucket` 里的行。

    ## ⛔⭐ 灌量**必须灌到 22 次**，⛔ 不是 20（T2b 执行期实测踩到的一条）

    取令牌端点那道**早退**闸门的判据是 `_failure_count(ip) > IP_FAILURE_MAX`
    （**严格大于**，见 `authentication.ip_failure_gate_already_exceeded` 的 docstring：
    它有意与 Bearer 那条路的 `>=` 不同，好让第 21 次由 `consume` 的判定来拒）。
    ⇒ 灌 **20** 次只把计数推到 20，第 21 次请求**仍然会被放进来**（它由 `consume` 判成 429，
    而 `count_ip_failure` 把计数推到 21）⇒ **到第 22 次才走早退**、才真的"被那道闸挡住"。
    ⚠️ 本用例要判的是"**同一 IP 在另一族是否仍可用**"，所以必须把闸真的关到底
    （`FLOOD = 22`），否则会拿到一个**假红**（改造前实测：`200 != 429`）。
    """

    #: 打手侧那一段"灌量"的请求数。⭐ **22 = `IP_FAILURE_MAX`(20) + 2**：见类 docstring。
    FLOOD = 22

    def _flood_taker_failures(self, *, remote_addr=SOURCE_IP):
        """用**打手侧取令牌端点**、错的密钥灌 `FLOOD` 次。

        ⚠️ 前 20 次回 **401**（凭据错），第 21 次起回 **429**（不再是"凭据无效"那个语义）
        —— 两条读数**都要断言**：否则"灌量"这一步的形态没人钉
        （它是本类所有判据的**前置条件**）。
        """
        for index in range(self.FLOOD):
            response = self.taker_token_request(
                client_secret=WRONG_SECRET, remote_addr=remote_addr
            )
            expected = 401 if index < TAKER_SPEC.failure_max else 429
            self.assertEqual(
                response.status_code, expected, (index, response.content[:200])
            )

    def _flood_openapi_failures(self, *, remote_addr=SOURCE_IP):
        """同上，用**上家侧取令牌端点**（两族各灌各的）。"""
        for index in range(self.FLOOD):
            response = self.openapi_token_request(
                client_secret=WRONG_SECRET, remote_addr=remote_addr
            )
            expected = 401 if index < OPENAPI_SPEC.failure_max else 429
            self.assertEqual(
                response.status_code, expected, (index, response.content[:200])
            )

    def test_the_failure_gate_really_closes_for_the_taker_family(self):
        """⚠️ **前置条件自证**：灌满之后打手侧**确实**被挡住了（否则下面那条对照恒真）。

        ⚠️ 缺了这条，"打满打手侧之后上家侧仍可用"可能只是"打手侧压根没被打满"造成的假绿。
        ⚠️ 这里用的是**正确**凭据（全对）⇒ 429 只可能来自那两道按来源 IP 的闸。
        """
        self._flood_taker_failures()
        blocked = self.taker_token_request()          # 凭据**全对**，照理该 200
        self.assertEqual(blocked.status_code, 429, blocked.content[:200])
        self.assertEqual(set(blocked.json()), {"error", "error_description"})

    def test_the_openapi_family_is_still_usable_after_the_taker_gate_is_full(self):
        """⭐ 打手侧灌满之后，**同一 IP** 打上家侧 ⇒ **仍然可用**（200 + 真令牌）。"""
        self._flood_taker_failures()
        self.assertEqual(self.taker_token_request().status_code, 429)   # 前置条件
        still_ok = self.openapi_token_request()
        self.assertEqual(
            still_ok.status_code,
            200,
            "打手侧灌满之后，同一 IP 的上家侧取令牌被一起挡掉了 ⇒ 两族共用了一行失败计数"
            "（spec §9-16 / §4 第 6 项）",
        )
        self.assertEqual(set(still_ok.json()), {"access_token", "token_type", "expires_in"})

    def test_the_taker_family_is_still_usable_after_the_openapi_gate_is_full(self):
        """⭐ **反向**：上家侧灌满之后，同一 IP 打打手侧 ⇒ **仍然可用**（200 + 真令牌）。"""
        self._flood_openapi_failures()
        self.assertEqual(self.openapi_token_request().status_code, 429)  # 前置条件
        still_ok = self.taker_token_request()
        self.assertEqual(
            still_ok.status_code,
            200,
            "上家侧灌满之后，同一 IP 的打手侧取令牌被一起挡掉了 ⇒ 两族共用了一行失败计数",
        )
        self.assertEqual(set(still_ok.json()), {"access_token", "token_type", "expires_in"})

    def test_the_two_families_write_two_different_bucket_tables(self):
        """⭐ 判据打在**两张桶表**上：一次打手侧失败 + 一次上家侧失败 ⇒ **两行各住一张表**。"""
        self.taker_token_request(client_secret=WRONG_SECRET)
        self.openapi_token_request(client_secret=WRONG_SECRET, client_id=OPENAPI_KEY)

        taker_failure_rows = list(
            TakerApiRateBucket.objects.filter(
                scope=TakerApiRateBucket.Scope.IP
            ).values_list("ident", "count")
        )
        openapi_failure_rows = list(
            OpenApiRateBucket.objects.filter(
                scope=OpenApiRateBucket.Scope.IP
            ).values_list("ident", "count")
        )
        # ⚠️ 期望值是**字面量**（E-0024）：`scope` 的取值两族**逐字相同**（`"ip"`）——
        #    隔离**不是**靠 scope 不同，而是靠**表**不同。这正是这条用例要钉住的形状。
        self.assertEqual(taker_failure_rows, [(SOURCE_IP, 1)], taker_failure_rows)
        self.assertEqual(openapi_failure_rows, [(SOURCE_IP, 1)], openapi_failure_rows)
        # 兜底额度那两行也各住一张表（同一次调用里两道闸各记一行）。
        self.assertEqual(
            list(
                TakerApiRateBucket.objects.filter(
                    scope=TakerApiRateBucket.Scope.IP_TOKEN
                ).values_list("ident", "count")
            ),
            [(SOURCE_IP, 1)],
        )
        self.assertEqual(
            list(
                OpenApiRateBucket.objects.filter(
                    scope=OpenApiRateBucket.Scope.IP_TOKEN
                ).values_list("ident", "count")
            ),
            [(SOURCE_IP, 1)],
        )

    def test_another_source_is_unaffected_within_the_same_family(self):
        """⚠️ 对照（同族内）：换一个来源 IP ⇒ 同族**照常可用**（证明"灌满"是**按 IP** 生效的）。

        缺了它，"打满之后另一族仍可用"有可能被"闸门其实压根没按 IP 分桶"骗过
        （那样两族都拦不住、或都拦住，两种实现里有一种会让上面两条恰好绿）。
        """
        self._flood_taker_failures(remote_addr=SOURCE_IP)
        self.assertEqual(
            self.taker_token_request(remote_addr=SOURCE_IP).status_code, 429
        )
        other = self.taker_token_request(remote_addr=OTHER_SOURCE_IP)
        self.assertEqual(other.status_code, 200, other.content[:200])


class TakerTokenEndpointTests(_FamilyFixture):
    """打手侧取令牌端点（`POST /api/taker/oauth/token`）的**类级契约**与"钱记在谁头上"。

    ## 为什么这三条要在这里（而不是"反正 `test_no_route_500` 会跑它"）

    `test_no_route_500` 跑的是**畸形入参的形状**（不许 500 / 键集恰好两键）；
    本类钉的是那三个**承重的类属性**——它们各自都能被一个"顺手改回默认值"的改动破坏，
    而破坏之后**执行路径照样通**（只是答案变了）：

    | 属性 | 改成别的值会怎样 |
    |---|---|
    | `authentication_classes = []` | 不写 ⇒ DRF 取全局默认（会话认证）⇒ **第三方机器被 401 挡在门口**（而它本来就不该登录） |
    | `permission_classes = [AllowAny]` | 改成默认 ⇒ 401 来自**权限层**、而那句话**不是 OAuth2 体** |
    | `oauth2_response_contract = True` | 抹掉 ⇒ `test_no_route_500` 的 OAuth2 那一档**认不出它**（那张网会按普通信封去量它，当场红） |

    ⚠️ 三条都打在**类属性**上（⛔ 不是"看响应像不像"——那是用被测结果证明被测结果）。
    """

    def test_the_class_level_contract_is_the_oauth2_entry_point(self):
        """⭐ 三个类属性各自钉住（逐条后果见类 docstring）。"""
        from rest_framework.permissions import AllowAny

        self.assertEqual(TakerTokenView.authentication_classes, [])
        self.assertEqual(TakerTokenView.permission_classes, [AllowAny])
        self.assertIs(TakerTokenView.oauth2_response_contract, True)
        # ⚠️ 与上家侧那条**同款但各自声明**（⛔ 不是同一个类、也⛔ 不是继承来的标记）。
        from apps.openapi.api.token_views import TokenView

        self.assertIsNot(TakerTokenView, TokenView)
        self.assertEqual(TakerTokenView.authentication_classes, TokenView.authentication_classes)

    def test_the_two_token_endpoints_write_into_their_own_bucket_tables(self):
        """⭐ **同一来源 IP、两边各换一次令牌** ⇒ 兜底额度那一行**各住一张表**。

        这条与 `RateBucketIsolationTests` 打的是同一件事，但走的是**成功路径**
        （那边走失败 + 闸门）⇒ 两条合起来覆盖了两族在**两类调用结果**下的记账面。
        """
        self.assertEqual(self.taker_token_request().status_code, 200)
        self.assertEqual(self.openapi_token_request().status_code, 200)
        taker_rows = list(
            TakerApiRateBucket.objects.filter(
                scope=TakerApiRateBucket.Scope.IP_TOKEN
            ).values_list("ident", "count")
        )
        openapi_rows = list(
            OpenApiRateBucket.objects.filter(
                scope=OpenApiRateBucket.Scope.IP_TOKEN
            ).values_list("ident", "count")
        )
        self.assertEqual(taker_rows, [(SOURCE_IP, 1)], taker_rows)
        self.assertEqual(openapi_rows, [(SOURCE_IP, 1)], openapi_rows)

    def test_the_taker_token_is_signed_with_the_taker_app_secret(self):
        """⭐ 令牌是用**打手侧那一行的密钥**签的（⛔ 不是上家侧那份密钥 / 也不是写死的）。

        判据：拿换来的令牌按 `TakerApp` **库里那一行**的明文密钥验签 ⇒ 通过；
        拿上家侧应用的密钥验 ⇒ **不通过**（`TokenError`）。
        ⚠️ 这条挡的是"两族共用一份密钥 / 签发时读错表"这类串台，而它们在
        "令牌能不能用"那一层**看不出异常**（认证器那边也会跟着读错同一张表 ⇒ 自洽）。
        """
        from apps.openapi import tokens

        raw = self.issue_taker_token()
        payload = tokens.verify(raw, self.taker_app.app_secret)
        self.assertEqual(payload["ak"], TAKER_KEY)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(raw, OPENAPI_SECRET)


class PerFamilyQuotaTests(TenantTestCase):
    """⭐ spec §4.1-4：**两族额度不同**，且打手侧读的是**它自己**的常量（⛔ 不是上家侧的 6000）。

    ## 被挡的形状

    > 打手侧若照抄 openapi 那两个"读模块名"的薄壳，会**静默用到上家侧的额度**（6000/分钟）：
    > 行为看起来一切正常，只有"量级是别人的"这一件事没有任何症状。

    ⇒ 本类把"**值不同**"与"**决策点读的是本族那个名字**"两件事**分开**钉住：
    前者是读数，后者是**打桩能打得中**（`E-0029`：从 spec 现取会让补丁静默失效）。
    """

    def test_the_two_families_fallback_quotas_are_different(self):
        """⭐ 两族的兜底额度**不相等**（且都不是 0 / 负数这种退化值）。

        ⚠️ 这条**故意不写"打手侧必须比上家侧小"**：那会把一个**可调的实现选择**
        （数值本身见 `apps/takerapi/credentials.py` 的常量注释）钉成契约。
        契约只有一条 —— **两族不同**（spec §4.1-4 逐字要求）。
        ⚠️ 判别力：把 `TAKER_FALLBACK_MAX_REQUESTS` 改成 `OPENAPI_FALLBACK_MAX_REQUESTS`
        （= 地雷的形状）⇒ 这条当场红。
        """
        self.assertNotEqual(
            TakerAppFallbackThrottle().fallback_max_requests(),
            ApiAppFallbackThrottle().fallback_max_requests(),
            "两族的兜底额度相等 ⇒ 打手侧很可能在读上家侧那个常量（spec §4.1-4 的地雷）",
        )
        self.assertGreater(TakerAppFallbackThrottle().fallback_max_requests(), 0)
        self.assertGreater(ApiAppFallbackThrottle().fallback_max_requests(), 0)

    def test_the_two_families_token_quotas_are_different(self):
        """⭐ 取令牌端点的兜底额度同理（两族**各有各的**模块级名字与值）。"""
        self.assertNotEqual(TAKER_TOKEN_FALLBACK_MAX_REQUESTS, OPENAPI_TOKEN_QUOTA)
        self.assertGreater(TAKER_TOKEN_FALLBACK_MAX_REQUESTS, 0)
        # ⚠️ 打手侧那个名字的**初始化来源**是它自己的 spec（⛔ 不是上家侧的模块常量）。
        self.assertEqual(
            TAKER_TOKEN_FALLBACK_MAX_REQUESTS, TAKER_SPEC.token_fallback_max_requests
        )
        self.assertIsNot(
            TAKER_SPEC.token_fallback_max_requests, OPENAPI_SPEC.token_fallback_max_requests
        )

    def test_the_taker_quota_literals_are_pinned(self):
        """⚠️ 钉**值**（E-0024）：3000 / 60 与 300 / 60 —— 调它要同一次改这里。

        ⚠️ 调小会打到正在跑的第三方（口径同 `apps/openapi/throttling.py` 的模块 docstring）⇒
        改这个数必须是有意的，而这条用例就是那次"有意"的落款。
        """
        self.assertEqual(TAKER_SPEC.app_fallback_max_requests, 3000)
        self.assertEqual(TAKER_SPEC.app_fallback_window_seconds, 60)
        self.assertEqual(TAKER_SPEC.token_fallback_max_requests, 300)
        self.assertEqual(TAKER_SPEC.token_fallback_window_seconds, 60)
        # 上家侧那一对**一个字没改**（两族的差别是真的，⛔ 不是"把上家侧也改小了"）。
        self.assertEqual(OPENAPI_SPEC.app_fallback_max_requests, 6000)
        self.assertEqual(OPENAPI_FALLBACK_MAX_REQUESTS, 6000)
        self.assertEqual(OPENAPI_TOKEN_QUOTA, 600)

    def test_the_taker_throttle_reads_its_own_module_name_at_call_time(self):
        """⭐ 打手侧的两个钩子在**调用时**读**本模块**的模块级名字（打桩打得中，`E-0029`）。

        判别力：把两个钩子改成 `return self.spec.app_fallback_max_requests`（从 spec 现取）
        ⇒ 下面那个补丁**静默失效**、读数仍是 3000 ⇒ 这条红。
        ⚠️ 同时钉"⛔ 不是读上家侧那个名字"：给上家侧那个名字打桩，打手侧的读数**不许**跟着变。
        """
        throttle = TakerAppFallbackThrottle()
        with mock.patch.object(taker_throttling, "TAKER_FALLBACK_MAX_REQUESTS", 7):
            self.assertEqual(throttle.fallback_max_requests(), 7)
        with mock.patch.object(taker_throttling, "TAKER_FALLBACK_WINDOW_SECONDS", 11):
            self.assertEqual(throttle.fallback_window_seconds(), 11)
        # 还原已生效（自证：补丁出块后回到 spec 里的值）。
        self.assertEqual(throttle.fallback_max_requests(), TAKER_SPEC.app_fallback_max_requests)
        # ⛔ 打上家侧那个名字 ⇒ 打手侧的读数**一个字不动**。
        from apps.openapi import throttling as openapi_throttling

        with mock.patch.object(openapi_throttling, "OPENAPI_FALLBACK_MAX_REQUESTS", 12345):
            self.assertEqual(
                throttle.fallback_max_requests(),
                TAKER_SPEC.app_fallback_max_requests,
                "打手侧的额度跟着上家侧的模块名变了 ⇒ 它读错了模块（地雷的形状）",
            )

    def test_the_taker_throttle_counts_into_the_taker_bucket_table(self):
        """⭐ 打手侧兜底闸的计数落**打手侧那张**桶表（⛔ 不落 `OpenApiRateBucket`）。"""
        app = TakerApp.objects.create(
            name="额度探针应用",
            app_key="takerquota000001",
            app_secret="taker-quota-secret-000000001",
            taker=_user("takerapi-t2b-quota-taker"),
            created_by=_user("takerapi-t2b-quota-admin"),
        )
        request = APIRequestFactory().get(PROBE_PATH)
        request.auth = app
        throttle = TakerAppFallbackThrottle()
        self.assertIs(throttle.allow_request(request, None), True)
        self.assertEqual(
            list(
                TakerApiRateBucket.objects.filter(
                    scope=TakerApiRateBucket.Scope.APP
                ).values_list("ident", "count")
            ),
            [("takerquota000001", 1)],
        )
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)

    def test_the_base_class_still_has_no_default_quota(self):
        """⛔ 基础类**仍然不给**额度 / `spec`（漏声明 ⇒ 第一次请求响亮报错）。

        这条**不是**重复：它是"两族各自显式声明"这件事的**前提**（spec §4.1-4 的形状）。
        判别力：给基类加一个默认 `fallback_max_requests` ⇒ 这条红
        （那时子类漏声明就会**静默**拿到上一族的额度）。
        """
        from apps.openapi.throttling import SpecDrivenFallbackThrottle

        with self.assertRaises(NotImplementedError):
            SpecDrivenFallbackThrottle().fallback_max_requests()
        with self.assertRaises(NotImplementedError):
            SpecDrivenFallbackThrottle().fallback_window_seconds()
        # ⚠️ 打手侧那个子类**继承的是基类**、⛔ 不是上家侧那个子类（禁令写在它的 docstring 里）。
        self.assertIsNot(TakerAppFallbackThrottle.__bases__, (ApiAppFallbackThrottle,))
        self.assertTrue(issubclass(TakerAppFallbackThrottle, SpecDrivenFallbackThrottle))


class SpecOfAndSpecAssignmentTests(_FamilyFixture):
    """`spec_of` 的**响亮失败**（§4.1-3）与**四张新表各自的 `spec` 都用对了**（§4.1-1）。

    ⚠️ `spec_of` 的三条边界里，前两条（"根本没有认证器那一格" / "认证器是 `None`"）已经有
    既有判据（`tests/test_openapi_*`）；本类补的是**打手侧这一族**的落点，以及第三条
    （"认证器存在却没有有效的 `spec`" ⇒ `ImproperlyConfigured`）在**打手侧**的读数。
    """

    def test_spec_of_returns_the_taker_spec_for_the_taker_authenticator(self):
        """⭐ 打手侧认证器在请求上 ⇒ `spec_of` 回**打手侧那一份**（⛔ 不是回默认的上家侧）。

        ## ⚠️ 走的是 **DRF 自己的记账**，⛔ 不是手搓一个属性

        `successful_authenticator` 是 `Request` 上的**惰性属性**：读它就触发
        `Request._authenticate()`（`rest_framework/request.py`），而那里面
        `self._authenticator = authenticator` 这一行**就是生产那条路**。
        ⛔ 手搓 `request.successful_authenticator = …` 会把被测对象（DRF 的记账）摘掉，
        那样"打手侧认证器真的被记上了"这一件事**没有被测**。

        ⚠️ **必须先读 `successful_authenticator` 再调 `spec_of`**（顺序本身是判据）：
        `spec_of` 只读那一格、⛔ **不触发认证** ⇒ 反过来写会拿到 `None`、落进"回默认"那一支
        （T2b 执行期实测踩到过：`AttributeError: 'NoneType' has no attribute '…'` 形态的假红）。

        ⚠️ 顺带钉住"认证真的跑过"：`TakerAppBearerAuthentication` 成功时返回的是
        **`AnonymousUser()`**（对外端点的既定形状）⇒ `request.user` 是匿名的，
        而 `request.auth` 是那一行 `TakerApp`。
        """
        from rest_framework.request import Request as DRFRequest

        token = self.issue_taker_token()   # ⛔ 先真的换一个令牌（前置条件）
        drf_request = DRFRequest(
            self.build_taker_request(token),
            authenticators=[TakerAppBearerAuthentication()],
        )
        # ⭐ 第一读：**先**读那一格（惰性触发 DRF 自己的 `_authenticate()`）。
        authenticator = drf_request.successful_authenticator
        self.assertIsNotNone(authenticator, "认证没有跑过 ⇒ 下面那条判据会以'回默认'的形式假绿")
        self.assertIs(authenticator.__class__, TakerAppBearerAuthentication)
        # 认证真的认出了那一行打手侧应用（⛔ 不是"随手记了个类名"）。
        self.assertIsInstance(drf_request.auth, TakerApp)
        self.assertEqual(drf_request.auth.app_key, TAKER_KEY)
        # ⭐ 第二读：`spec_of` 从**同一次记账**里取到了**打手侧**那一份。
        self.assertIs(spec_of(drf_request), TAKER_SPEC)
        self.assertIsNot(spec_of(drf_request), OPENAPI_SPEC)

    def test_spec_of_is_loud_when_the_authenticator_declares_no_spec(self):
        """⭐ §4.1-3：认证器存在、却**没有有效的 `spec`** ⇒ **抛** `ImproperlyConfigured`。

        判别力：这一支若回退成"回默认（上家侧）"，那么"打手侧端点忘挂认证器 / 挂了没声明
        `spec`"就**完全静默**地按上家侧的台账 / 桶 / 文案处理 —— 那正是评审点名的
        "静默串台面"。⇒ 这条用例就是那件事的钉子。
        """
        from django.core.exceptions import ImproperlyConfigured
        from django.test import RequestFactory

        class _NoSpecAuthenticator:
            pass

        class _WrongTypeSpecAuthenticator:
            spec = "not-a-spec"

        for authenticator in (_NoSpecAuthenticator(), _WrongTypeSpecAuthenticator()):
            with self.subTest(authenticator=type(authenticator).__name__):
                request = RequestFactory().get(PROBE_PATH)
                request.successful_authenticator = authenticator
                with self.assertRaises(ImproperlyConfigured) as caught:
                    spec_of(request)
                # ⚠️ 消息里点名是**哪一类认证器**、⛔ 不回显任何凭据值。
                self.assertIn(type(authenticator).__name__, str(caught.exception))
                self.assertNotIn(TAKER_SECRET, str(caught.exception))
                self.assertNotIn(TAKER_KEY, str(caught.exception))

    def test_spec_of_still_falls_back_for_a_request_with_no_authenticator_slot(self):
        """⚠️ 前两条边界**没变**（⛔ 别把"响亮"读成"一律抛"）：没有那一格 / 那一格是 `None` ⇒ 回默认。

        判据是**既有行为不变**（spec §4.1-3 只改了第三条），⛔ 不是新增约束。
        """
        from django.test import RequestFactory

        bare = RequestFactory().get(PROBE_PATH)
        self.assertIs(spec_of(bare), OPENAPI_SPEC)
        none_slot = RequestFactory().get(PROBE_PATH)
        none_slot.successful_authenticator = None
        self.assertIs(spec_of(none_slot), OPENAPI_SPEC)

    def test_the_four_new_tables_each_use_their_own_spec_entry(self):
        """⭐ 四类新表**各自的 `spec` 都用对了**（⛔ 没有一处指回上家侧）。

        逐项（每一项都是"引擎里的一处硬编码被参数化掉"的落点，spec §4 那张表）：
        凭据模型 / 幂等台账模型 / 桶模型 / 三个 `scope` / 可用状态值 / 前缀 / 文案 / 额度。
        ⚠️ 判据是**对象同一性**与**字面值**，⛔ 不是"两个都非空"这种恒真写法。
        """
        # ① 三张表**都是打手侧自己的**（对象同一性 + 表名）。
        self.assertIs(TAKER_SPEC.app_model, TakerApp)
        self.assertIs(TAKER_SPEC.idem_model, TakerApiIdempotency)
        self.assertIs(TAKER_SPEC.bucket_model, TakerApiRateBucket)
        self.assertEqual(TAKER_SPEC.idem_model._meta.db_table, "takerapi_takerapiidempotency")
        self.assertEqual(TAKER_SPEC.bucket_model._meta.db_table, "takerapi_takerapiratebucket")
        # ② 三个 `scope` 取自**打手侧**那个枚举（对象同一性 + 字面值）。
        self.assertIs(TAKER_SPEC.failure_scope, TakerApiRateBucket.Scope.IP)
        self.assertIs(TAKER_SPEC.app_fallback_scope, TakerApiRateBucket.Scope.APP)
        self.assertIs(TAKER_SPEC.token_fallback_scope, TakerApiRateBucket.Scope.IP_TOKEN)
        self.assertEqual(
            [
                TAKER_SPEC.failure_scope,
                TAKER_SPEC.app_fallback_scope,
                TAKER_SPEC.token_fallback_scope,
            ],
            ["ip", "app", "ip_token"],
        )
        # ⚠️ 两族的 scope **取值逐字相同**、而它们是**两个不同枚举类的成员**
        #    （对象同一性分辨得出 ⇒ 这正是"引擎 import 哪一份都错"那条理由的判据）。
        self.assertNotIsInstance(TAKER_SPEC.failure_scope, OpenApiRateBucket.Scope)
        self.assertEqual(TAKER_SPEC.failure_scope, OPENAPI_SPEC.failure_scope)
        # ③ 可用状态值取自**打手侧**的 `AppState`。
        self.assertEqual(TAKER_SPEC.enabled_state_value, "enabled")
        self.assertIsNot(TAKER_SPEC.enabled_state_value, OPENAPI_SPEC.enabled_state_value)
        # ④ 来源单号去重的前缀**与上家侧不同**（spec §4 第 4 项）。
        self.assertNotEqual(TAKER_SPEC.source_key_prefix, OPENAPI_SPEC.source_key_prefix)
        self.assertEqual(TAKER_SPEC.source_key_prefix, "takerapi:source:")
        # ⑤ 文案：四句**值**都是对的（钉值，E-0024）。
        self.assertEqual(TAKER_SPEC.invalid_credentials_msg, "应用凭据无效")
        self.assertEqual(TAKER_SPEC.expired_msg, "令牌已过期")
        self.assertEqual(TAKER_SPEC.ip_not_allowed_msg, "来源 IP 不在白名单内")
        self.assertEqual(TAKER_SPEC.unsupported_grant_type_description, "仅支持 client_credentials")
        self.assertEqual(TAKER_SPEC.rate_limited_description, "请求过于频繁，请稍后再试")
        # ⑥ 超长路径那条闸的**数字拼自列宽**（两族列宽今天相同 ⇒ 文案逐字相同）。
        self.assertEqual(TAKER_SPEC.max_path_chars, 191)
        self.assertEqual(TAKER_SPEC.path_too_long_msg, "请求路径过长（上限 191 个字符）")

    def test_both_specs_are_credential_specs(self):
        """⚠️ 两份 spec 都是**真 `CredentialSpec`**（不是"长得像"的东西）。

        判别力：`spec_of` / 引擎按 `isinstance(spec, CredentialSpec)` 判 —— 一份"鸭子类型"
        的对象会**响亮**失败（那是有意的设计），而这条把两族都先钉住。
        """
        for spec in (TAKER_SPEC, OPENAPI_SPEC):
            with self.subTest(spec=spec.app_model.__name__):
                self.assertIsInstance(spec, CredentialSpec)


class RequiredSpecKeywordTests(TenantTestCase):
    """⭐ 步骤 0（spec §4.1-1）：共享地基里**最后 5 处**静默默认值已收掉 ⇒ **漏传 = `TypeError`**。

    ## 被收掉的形状（评审点名的"静默串台面"）

    改之前这 5 个函数的 `spec` 有默认值 `= OPENAPI_SPEC` ⇒ "打手侧漏传"这件事**完全静默**：
    请求照常成功，只是计数落进**上家侧**那张桶表 / 幂等查**上家侧**那张台账 /
    文案用**上家侧**那一套。而同一类错误在 `_find_existing` / `_prune` 那几处**是响亮**的
    （必填 keyword-only）—— 同一种错一处响亮一处静默，是最坏的一种不一致。

    ## 判据为什么打在**这几个函数自己**身上（⛔ 不是"扫一遍源码"）

    结构网（例如 grep / AST 扫"有没有 `= OPENAPI_SPEC`"）只能证明**这一次**改对了，
    而这条判据钉的是**行为**：调它、且**不传** `spec` ⇒ **当场 `TypeError`**。
    ⇒ 谁哪天"为了兼容"把默认值加回来，这条立刻红（实测：T2b 的变异 M4
    `spec: CredentialSpec = OPENAPI_SPEC` 正是被这一条杀掉的）。

    ⚠️ 五个函数逐条点名（`_bucket_table_and_constraint` / `consume` / `current_count` /
    `_app_ident` / `source_key`）—— 少点名一个，那个函数的默认值加回来就**没人拦**
    （本类的判别力完全来自"逐个函数都真的打过一次"）。
    ⚠️ **对照**：同一个函数**传了** `spec` 就**不抛**（下半段）——
    否则"这五个函数其实都炸"也能让上面那五条绿。
    """

    #: `spec` 缺失时 CPython 抛出的那句（`TypeError` 的**消息形态**是契约的一部分：
    #: 它要点名缺的是哪个参数，排障时不必去猜）。⚠️ 用正则匹配前缀、⛔ 不整句相等
    #: （解释器版本不同时后半句会漂，而"缺的是 `spec`"这件事不漂）。
    MISSING_SPEC_PATTERN = r"missing 1 required keyword-only argument: 'spec'"

    def test_the_five_remaining_defaults_are_gone(self):
        """⭐ 五个函数**漏传 `spec`** ⇒ 各自 `TypeError`（逐个点名，⛔ 不合并成一条）。"""
        from apps.openapi import idempotency, rate_limit, throttling

        calls = {
            "rate_limit._bucket_table_and_constraint": lambda: (
                rate_limit._bucket_table_and_constraint()
            ),
            "rate_limit.consume": lambda: rate_limit.consume(
                scope="ip", ident="203.0.113.99", limit=1, window_seconds=60
            ),
            "rate_limit.current_count": lambda: rate_limit.current_count(
                scope="ip", ident="203.0.113.99", window_seconds=60
            ),
            "throttling._app_ident": lambda: throttling._app_ident(None),
            "idempotency.source_key": lambda: idempotency.source_key("TT-T2B-KEY-0001"),
        }
        for name, call in calls.items():
            with self.subTest(function=name):
                with self.assertRaises(TypeError) as caught:
                    call()
                self.assertRegex(str(caught.exception), self.MISSING_SPEC_PATTERN)

        # ⚠️ **结构化复核**（独立于上面那五条行为判据）：这五个函数自己的签名里
        #    `spec` **没有默认值**。它与行为判据**不是同一件事**（行为判据能抓"值被换掉"，
        #    这一条能抓"某个函数压根没被测到"）⇒ 两条一起才钉住"五处都收掉了"。
        import inspect

        for func in (
            rate_limit._bucket_table_and_constraint,
            rate_limit.consume,
            rate_limit.current_count,
            throttling._app_ident,
            idempotency.source_key,
        ):
            with self.subTest(signature=func.__qualname__):
                parameter = inspect.signature(func).parameters["spec"]
                self.assertIs(
                    parameter.default,
                    inspect.Parameter.empty,
                    f"{func.__qualname__} 的 spec 又有默认值了 ⇒ 漏传会静默按那一族处理",
                )
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_the_same_five_functions_work_when_the_spec_is_given(self):
        """⚠️ **对照**：传了 `spec` 就**不抛**（否则上面那五条可能只是"函数本来就炸"）。"""
        from apps.openapi import idempotency, rate_limit, throttling

        self.assertEqual(
            rate_limit._bucket_table_and_constraint(spec=TAKER_SPEC),
            ("takerapi_takerapiratebucket", "uniq_taker_rate_bucket"),
        )
        allowed, _retry_after = rate_limit.consume(
            scope=TAKER_SPEC.failure_scope,
            ident="203.0.113.99",
            limit=1,
            window_seconds=60,
            spec=TAKER_SPEC,
        )
        self.assertIs(allowed, True)
        self.assertEqual(
            rate_limit.current_count(
                scope=TAKER_SPEC.failure_scope,
                ident="203.0.113.99",
                window_seconds=60,
                spec=TAKER_SPEC,
            ),
            1,
        )
        self.assertEqual(throttling._app_ident(TAKER_SPEC.app_model(), spec=TAKER_SPEC), "")
        # ⚠️ `source_key` 传了 spec ⇒ 回**打手侧那个前缀**（⛔ 不是上家侧那个）。
        self.assertEqual(
            idempotency.source_key("TT-T2B-KEY-0001", spec=TAKER_SPEC),
            "takerapi:source:TT-T2B-KEY-0001",
        )

    def test_the_preserved_bucket_key_still_speaks_for_the_openapi_family(self):
        """⚠️ 保留的历史函数 `throttling.bucket_key` 仍可用 —— 它在**内部**显式传了 `OPENAPI_SPEC`。

        `_app_ident` 的 `spec` 改成必填之后，`bucket_key` 这条**已退役的计数路径**
        （它服务的是上家侧那条 `LocMemCache` 口径）如果**漏传**，它会在**调用时**炸 ——
        而本任务对它的要求是「⛔ 不许删、行为不变」。⇒ 这条钉住它**还能跑**、
        且键里那一段仍然是**上家侧应用**的 `app_key`。

        判据：拿一份**同时有 `app_key`** 的假对象调它 ⇒ 不抛，且键里含那个 key 与窗口序号。
        ⚠️ 判别力：把那个显式实参去掉（回到"漏传"）⇒ 本用例 ERROR。
        """
        from apps.openapi import throttling

        class _FakeApp:
            app_key = "openprobeappkey001"

        key = throttling.bucket_key(_FakeApp(), 1_800_000_000.0)
        self.assertIn("openprobeappkey001", key)
        self.assertIn(str(int(1_800_000_000.0 // throttling.OPENAPI_FALLBACK_WINDOW_SECONDS)), key)


#: ⭐ 两条对外端点：**路径**（字面量，带尾斜杠）↔ **视图类**（`E-0065`：这类"装配挂没挂上"的
#: 判据必须**显式枚举**，⛔ 不用"遍历所有对外视图"那种派生写法——派生会让**将来新增**的
#: 对外端点**自动**进不了这张网，而"漏挂兜底闸"正是本网要挡的那种静默）。
OUTWARD_ROUTES = (
    ("/api/taker/v1/hall/", HallListView),
    # ⚠️ 抢单那条路径带 `<order_no>` 转换器 ⇒ 解析时给一个**形状合法**的单号
    #    （值本身不参与解析，只走 URL 匹配）。
    ("/api/taker/v1/hall/20261008000000000001/claim/", HallClaimView),
)


class OutwardViewAssemblyTests(TenantTestCase):
    """⭐ **判据 15（红队 2026-10-08 补，T5 修复轮落地）**：两条对外端点的**三件装配**。

    ## 为什么这三条类属性是**承重**的（"漏挂"是静默的）

    `src/config/settings.py` 的 `REST_FRAMEWORK` **只**设了默认**权限**类
    （`IsAuthenticated`）、**没有设**默认**限流**类 ⇒

    | 属性 | 漏挂 / 改错会怎样 |
    |---|---|
    | `authentication_classes = [TakerAppBearerAuthentication]` | 漏 ⇒ 吃全局默认（会话认证）⇒ 合法的打手令牌**永远 401** |
    | `permission_classes = []` | 漏 ⇒ 吃全局默认 `IsAuthenticated`，而 Bearer 认证下 `request.user` 是 `AnonymousUser` ⇒ **合法令牌反而 401**（⛔ 所以这个**空列表是有意的**，不是漏写） |
    | `throttle_classes = [TakerAppFallbackThrottle]` | 漏 ⇒ **完全无限流**，而且**没有任何网会红**：既有测试只测 throttle 类**自身**的行为（桶表怎么记 / 何时 429 / 族间隔离），⛔ 没有一条网钉"这两条路由**挂没挂**它" |

    ## 判据为什么打在**类属性**上（而不是再打一遍行为）

    行为层面的读数（401 先于 429、被 401 挡掉的请求不进桶、跨凭据类型 401…）已经各有别的网在钉；
    这里是**装配**本身——它坏了在行为上**看不见**（限流漏挂时全部行为用例照样绿）。
    ⚠️ 判据是**恰好**（`assertEqual` 整个列表，⛔ 不是 `in` / 长度）：多挂一个类同样是装配变更
    （例如把上家侧的认证类也挂上来 ⇒ 两族凭据互相认账，spec §9-2 的"三组不混"就没了）。

    ⚠️ **两条测试分工**（各自能单独红）：
    ① `test_..._carry_exactly_the_outward_assembly` 钉**类属性**（漏挂 / 改错 ⇒ 红）；
    ② `test_the_assembly_sits_on_the_classes_the_real_routes_resolve_to` 钉"这套装配挂在**真路由**
    解析到的那个类上"（把某条路由改指到一个新视图类 ⇒ 红）。
    """

    #: 三件装配的**期望值**（⛔ 不写"应该有一个认证类"这类模糊判据：那一句话在
    #: 认证类被换成**上家侧那一个**时也成立，而那是 spec §9-2 明确要挡的）。
    EXPECTED_AUTHENTICATION = [TakerAppBearerAuthentication]
    EXPECTED_PERMISSIONS = []
    EXPECTED_THROTTLES = [TakerAppFallbackThrottle]

    def test_the_two_outward_views_carry_exactly_the_outward_assembly(self):
        """① ⭐ 两条对外视图类的三件装配**恰好**是那三个值（逐个类断言，⛔ 不合并成一条）。

        ⚠️ 变异自证：把 `HallListView` 上那行 `throttle_classes = [...]` 删掉 ⇒ 本条**当场红**
        （`APIView` 会回落到 `api_settings.DEFAULT_THROTTLE_CLASSES`，而设置里是**空**）。
        """
        for label, view in (
            ("HallListView", HallListView),
            ("HallClaimView", HallClaimView),
        ):
            with self.subTest(view=label):
                self.assertEqual(view.authentication_classes, self.EXPECTED_AUTHENTICATION)
                self.assertEqual(view.permission_classes, self.EXPECTED_PERMISSIONS)
                self.assertEqual(view.throttle_classes, self.EXPECTED_THROTTLES)
        # ⚠️ 两条路由**各自是独立的类**（⛔ 不是同一个类被登记两次）：否则上面那一圈
        #    只是在同一个类上断言两遍（一条漏、另一条也漏）。
        self.assertIsNot(HallListView, HallClaimView)

    def test_the_assembly_sits_on_the_classes_the_real_routes_resolve_to(self):
        """② ⭐ 装配挂在**真路由解析到的那个类**上（⛔ 不是"某个没人路由的类装配得很整齐"）。

        判据 = 直接 `resolve()` 那两条**字面量路径**，取 `func.cls`（DRF 的 `as_view()` 会把它
        挂在视图函数上）⇒ **必须是**被断言装配的那两个类。
        ⚠️ 判别力：把 `apps/takerapi/urls.py` 里某条路由改指到一个新写的视图类（哪怕它
        `permission_classes` 忘了写）⇒ 本条红，而**只读类属性**的那条①**不会红**。
        """
        from django.urls import resolve

        for path, view in OUTWARD_ROUTES:
            with self.subTest(path=path):
                self.assertIs(
                    resolve(path).func.cls,
                    view,
                    f"{path} 解析到的不是 {view.__name__}（装配可能挂在一个没人路由的类上）",
                )


class BoundTakerActiveGateTests(_FamilyFixture):
    """⭐ D-0333（M3 半边收口；2026-10-30 业务方拍板「需要的」）：**停用打手的对外令牌立即失效**。

    ## 旧账形状（第五轮安全审计 M3 半边）与「半边」实况

    App（小程序）令牌链那半边早已查 `token.user.is_active`
    （`apps/core/authentication.py::AppTokenAuthentication`，2026-10-19 落的）；而**对外 API
    令牌链**这半边（打手侧走 `TakerAppBearerAuthentication` ⇒ 两族共用的 Bearer 引擎；
    取令牌走两族共用的 `token_exchange_response`）只查 `app.state`、⛔ 不查绑定的打手账号
    ⇒ 停用 / 软删的打手凭已发令牌照常调接口、也照常换得出新令牌。
    ⇒ 修法落在**共用实现**里（spec 新增必填格 `bound_user_field`：打手侧 `"taker"`、
    上家侧 `None` 早退），两处消费点 = 两条口，本类每条口各配「active 通行钉 +
    停用后同凭据拒绝」：

    | 口 | 位置 | 用例 |
    |---|---|---|
    | Bearer 认证引擎（第 5.5 步） | `apps/openapi/authentication.py::ApiAppBearerAuthentication.authenticate` | ①通行钉 ②停用同令牌拒 ③读的是当场值 ④软删同形拒 |
    | 取令牌共用流程 | `apps/openapi/api/token_views.py::token_exchange_response` | ⑤active 换得出 ⑥停用后换不出 ⑦上家侧不受影响 |

    ## 「同形状」判据（本拍板的另一半，防探测）

    拒绝答复必须与「令牌无效」**逐字相同**（`应用凭据无效`）——单独说破「账号被停用」
    等于给凭据持有者一条探测「这个账号还活着吗」的信道（同族先例
    `apps/takerapi/api/hall_views.py::NO_TAKER_NODE_MSG`）。⇒ ②④⑥各自把
    「停用打手」与「错令牌 / 错密钥」的**响应体整体**对拍（逐键相等，E-0024 钉值）。

    ## 软删口径（为什么闸只读 `is_active` 一格）

    本仓软删账号 = `staff_profile.deleted_at` + `is_active=False` **同一个事务**里的两笔写入
    （`apps/accounts/views.py::AccountViewSet.delete`）；恢复动作**碰都不碰** `is_active`
    （「恢复 ≠ 启用」，代码即口径）⇒ `is_active` 一格就同时盖住「停用」与「软删」两档，
    与 App 令牌链既有那半边同一口径。④按生产同一形状写两笔、断言同形拒绝。
    """

    # ── 写入口（照 `AccountViewSet.delete` / 状态切换的生产形状，合成值）─────────

    def _set_taker_active(self, active: bool) -> None:
        """停用 / 启用绑定的打手账号（只写 `is_active` 一列，⛔ 不碰其余列）。"""
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk=self.taker_app.taker_id).update(
            is_active=active
        )

    def _soft_delete_taker(self) -> None:
        """按生产**软删**的同一形状写两笔（`deleted_at` + `is_active=False`，合成时刻）。"""
        from django.utils import timezone

        profile, _ = StaffProfile.objects.get_or_create(user=self.taker_app.taker)
        profile.deleted_at = timezone.now()
        profile.save(update_fields=["deleted_at"])
        self._set_taker_active(False)

    # ── 口 1：Bearer 认证引擎 ──────────────────────────────────────────

    def test_an_active_takers_token_still_passes_the_bearer_gate(self):
        """① active 通行回归钉：停用判据落下之后，正常打手的令牌**照常放行**。"""
        token = self.issue_taker_token()
        response = self.call_taker_probe(token)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(json.loads(response.content)["app_key"], TAKER_KEY)

    def test_a_disabled_takers_existing_token_is_rejected_exactly_like_an_invalid_token(self):
        """② 停用后**同一枚**令牌 ⇒ 401，且响应体与「错令牌」**逐键相同**（同形状，防探测）。"""
        token = self.issue_taker_token()
        self._set_taker_active(False)
        rejected = self.call_taker_probe(token)
        self.assertEqual(rejected.status_code, 401, rejected.content[:200])
        # 对照组：一把从头到尾无效的令牌。两份响应体必须**整体相等**
        # （状态码之外：code / msg / data 逐键一致）⇒ 从任何可观察面都分不出
        # 「账号被停用」与「令牌无效」。
        bogus = self.call_taker_probe("bogus-token-not-issued-by-anyone")
        self.assertEqual(bogus.status_code, 401)
        self.assertEqual(
            json.loads(rejected.content),
            json.loads(bogus.content),
        )
        # 钉值（E-0024）：那一句就是「应用凭据无效」的字面量。
        self.assertEqual(json.loads(rejected.content)["msg"], INVALID_CREDENTIALS_MSG)
        # 库里那一行没动：闸只是拒绝，⛔ 不顺手替租户停用应用 / 改账号。
        self.taker_app.refresh_from_db()
        self.assertEqual(self.taker_app.state, "enabled")

    def test_the_bearer_gate_reads_the_account_live_not_a_cached_state(self):
        """③ 闸读的是**当场**那一行：停用 ⇒ 401；重新启用 ⇒ **同一枚**令牌又放行。

        （判「不缓存」的另一半：只测「停用 ⇒ 401」的实现拿一个启动时读一次的缓存也绿。）
        """
        token = self.issue_taker_token()
        self._set_taker_active(False)
        self.assertEqual(self.call_taker_probe(token).status_code, 401)
        self._set_taker_active(True)
        response = self.call_taker_probe(token)
        self.assertEqual(response.status_code, 200, response.content[:200])

    def test_a_soft_deleted_takers_existing_token_is_rejected_the_same_way(self):
        """④ **软删**（生产同一形状的两笔写入）后同令牌 ⇒ 401，与错令牌**同形状**。

        （软删的账号 `is_active=False` ⇒ 与②同一格拦住；这条钉的是「软删不会漏」——
        若哪天有人把软删改成不写 `is_active`，这条当场红。）
        """
        token = self.issue_taker_token()
        self._soft_delete_taker()
        rejected = self.call_taker_probe(token)
        self.assertEqual(rejected.status_code, 401, rejected.content[:200])
        bogus = self.call_taker_probe("bogus-token-not-issued-by-anyone")
        self.assertEqual(json.loads(rejected.content), json.loads(bogus.content))

    # ── 口 2：取令牌共用流程 ───────────────────────────────────────────

    def test_an_active_taker_can_still_mint_tokens(self):
        """⑤ active 通行回归钉（口 2）：停用判据落下之后，换令牌照常 200。"""
        response = self.taker_token_request()
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(
            set(response.json()), {"access_token", "token_type", "expires_in"}
        )

    def test_a_disabled_taker_cannot_mint_a_new_token(self):
        """⑥ 停用后同一套凭据 ⇒ 401 `invalid_client`，与「错密钥」响应体**逐键相同**。

        （否则停用只挡住旧令牌、对方转头就用 `client_secret` 换一枚新的——旧账就只收了一半。）
        """
        self.issue_taker_token()  # 先证 active 时这套凭据真的换得出令牌
        self._set_taker_active(False)
        rejected = self.taker_token_request()
        self.assertEqual(rejected.status_code, 401, rejected.content[:200])
        body = rejected.json()
        # OAuth2 裸两键（⛔ 没有本仓信封）+ 那一句与三档凭据失败逐字相同。
        self.assertEqual(set(body), {"error", "error_description"})
        self.assertEqual(body["error"], "invalid_client")
        self.assertEqual(body["error_description"], INVALID_CREDENTIALS_MSG)
        # 对照组：同一端点、密钥写错的响应体——两份必须整体相等（防探测）。
        wrong_secret = self.taker_token_request(client_secret=WRONG_SECRET)
        self.assertEqual(wrong_secret.status_code, 401)
        self.assertEqual(body, wrong_secret.json())

    def test_the_openapi_family_has_no_bound_account_and_stays_unaffected(self):
        """⑦ 上家侧**没有**绑定账号这一格（`None` 早退）：把两族应用的**创建人**停用，
        上家侧照常换令牌、打手侧也照常（判别力：若引擎误读了 `created_by` 这一类列，
        两条当场一起红；若打手侧误读了 `created_by`，第二条单独红）。
        ⚠️ 顺带钉两族那一格的**显式值**（构造处漏写会直接构造期炸，这里钉的是值本身）。
        """
        self.assertIsNone(OPENAPI_SPEC.bound_user_field)
        self.assertEqual(TAKER_SPEC.bound_user_field, "taker")
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(username="takerapi-t2b-admin").update(
            is_active=False
        )
        self.assertEqual(self.openapi_token_request().status_code, 200)
        self.assertEqual(self.taker_token_request().status_code, 200)
