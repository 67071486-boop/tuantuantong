"""`apps.openapi.authentication` 里**两条认证路径共用**的那几件的逐条判据。

签名路径已删除。Bearer 判据在 `tests/test_openapi_token.py`。
本文件测两条路还共用的东西：来源 IP、白名单、按 IP 的失败闸、失败文案、不进全局默认认证。

## 本文件今天证什么（⛔ 不是"剩下的零头"）

签名路径删掉之后，本模块仍有几件**两条路共用、且今天仍在服役**的东西，它们的判据都在本文件：

1. `client_ip()` 的**取值三级优先**（`X-Real-IP` → XFF **最右段** → `REMOTE_ADDR`）
   与那条**安全判据**：伪造 `X-Forwarded-For` **绕不过**白名单；
2. `_ip_allowed()` 的逐条口径（空 = 不限制 / 单地址与网段都认 / 一条写错跳过其余照常 /
   只有写错的条目 ⇒ 拒 / 来源解析不了 ⇒ fail-closed）；
3. **按来源 IP 的失败闸**（阈值 20 / 窗口 60）：错满之后**合法令牌也被拦**、
   阈值下界仍放行、被拦那一档回**同一句话**、桶按 IP 分、**闸排在查库之前**（含对照）、
   停用那一档**不扣**额度；
4. 失败文案里**今天仍在服役**的那几句（钉**值**，E-0024）与挑战头（非空、不漏凭据）；
5. **装配口径**：本类⛔ 不在 DRF 的**全局默认**认证类里（D13 的判据）。

## 为什么用 `APIRequestFactory` + 本模块内定义的极简探针视图

这些判据的对象是**认证类 / 模块级函数**，而不是某一条端点的契约。若为了测它们去建一个
`urls.py` 并把路由挂进 `config/urls.py`，就会**顺手**碰上一张承重网：
`tests/test_operation_log_coverage.py` 按 `get_resolver()` 枚举真实 urlconf，新模块 /
新端点会被它抓（§17.1 写明「先登记后建端点得到的是**红**而不是暂时无害」）。
⇒ 这里**不建任何 urls 模块**：请求经 `APIRequestFactory` 直接打进一个本文件内的探针视图，
认证类的装配方式（`authentication_classes` + 视图里读 `request.user` 触发认证）与真端点
**逐字相同**。

## 判据写在哪一层

断言全部打在**响应状态码 + 响应信封的 `msg` + 数据库行**上，不打 logger、
不看 `response.data is not None`（E-0063：信封恒非 `None`）。`msg` 一律用**字面量**比对
（E-0024：钉值不钉常量），并另有一条把实现常量与字面量对齐的钉子。
失败计数是 DB 行，不是缓存键。
"""
import json
from dataclasses import replace
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.openapi import authentication as auth_module
from apps.openapi import tokens
from apps.openapi.authentication import (
    ApiAppBearerAuthentication,
    _count_failure,
    _failure_count,
    count_ip_failure,
    ip_failure_gate_already_exceeded,
)
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, OpenApiRateBucket
from apps.takerapi.models import TakerApp, TakerApiRateBucket

#: 探针端点的路径（本文件不经路由，值本身不承重；保留它是为了让"请求形状"贴近真端点）。
PROBE_PATH = "/api/open/v1/orders/PROBE-1/"

#: 合成凭据（`AGENTS.md` §5-C：夹具一律用合成值）。⚠️ `app_key` 是**非机密**、
#: `app_secret` 才是机密；两者都是编出来的，⛔ 与任何真实租户的凭据无关。
APP_KEY = "probeappkey000001"
APP_SECRET = "probe-app-secret-0000000000000001"
OTHER_KEY = "probeappkey000002"
OTHER_SECRET = "probe-app-secret-0000000000000002"
#: ⛔ 一定不存在的 key：第 2 步要证明"没命中"与"凭据不对"**同形**。
UNKNOWN_KEY = "probeappkey0000ff"
#: 形状合法、但验签一定不过的密钥（用来构造"带了凭据但凭据不对"那一档）。
WRONG_SECRET = "probe-app-secret-00000000000000ff"

#: 默认来源 IP（文档保留段 203.0.113.0/24 的地址，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.9"
OTHER_SOURCE_IP = "198.51.100.7"


def _make_api_app(*, app_key=APP_KEY, app_secret=APP_SECRET, name="探针应用甲", **kwargs):
    """建一行 `ApiApp`（`created_by` 必填，给本用例自己造的那个账号）。"""
    user, _ = get_user_model().objects.get_or_create(
        username="openapi-auth-probe", defaults={"password": "unused-in-this-test"}
    )
    return ApiApp.objects.create(
        name=name, app_key=app_key, app_secret=app_secret, created_by=user, **kwargs
    )


@api_view(["GET"])
@authentication_classes([ApiAppBearerAuthentication])
#: ⚠️ 探针视图**必须** `AllowAny`：对外端点⛔ 不能用 `IsAuthenticated`
#: （`AnonymousUser.is_authenticated` 是 `False` ⇒ 会 401）。
@permission_classes([AllowAny])
def probe_view(request):
    """回显"认证把什么东西挂到了请求上"。

    ⚠️ 先读 `request.user` 再回包：DRF 的认证是**惰性**的，视图不碰它就不会跑
    （那样本探针会恒 200、零判别力）。真端点由权限类或视图体去碰它，这里的写法等价。
    """
    app = getattr(request, "_api_app", None)
    return Response(
        {
            "app_key": app.app_key if app is not None else None,
            "auth_is_app": isinstance(request.auth, ApiApp),
            "user_is_authenticated": request.user.is_authenticated,
            "user_pk": request.user.pk,
        }
    )


#: `@api_view` 包出来的是一个**函数**，但它带着 `view_class`（`rest_framework/decorators.py`
#: 里的 `WrappedAPIView.as_view()`）⇒ 这里取回那个 Django 视图函数，于是请求能**走完整条
#: DRF 请求周期**（认证 → 权限 → 视图 → 异常处理器 → 渲染）。
#: ⚠️ 不用 `force_authenticate` 之类的测试旁路：本文件测的**正是**认证类，旁路掉它等于把
#: 被测对象摘了 —— 那样 401 用例仍然"绿"，但绿的是别的东西。
PROBE_HANDLER = probe_view.view_class.as_view()


class _BearerRequestMixin:
    """构造带（或故意不带）令牌的请求，并把它打进探针视图。"""

    def setUp(self):
        super().setUp()
        # ⚠️ 计数落 DB 行（T3 起），由 `TenantTestCase` 的事务回滚清掉 ⇒ 本行**不是承重逻辑**。
        # 保留它是因为同一进程里别的东西（节流 / 别的线的缓存）仍可能跨用例残留。
        cache.clear()
        self.factory = APIRequestFactory()
        self.view = PROBE_HANDLER  # 走完整 DRF 周期的那个 Django 视图函数
        self.app = _make_api_app()

    def build(self, *, key=APP_KEY, secret=APP_SECRET, remote_addr=SOURCE_IP, token=None):
        """造一个**带令牌**的 GET 请求（令牌默认是有效的）。

        ⚠️ 令牌**当场签发**（`tokens.issue`）——⛔ 不抄签发 / 编码逻辑（那会变成
        "用被测者证明被测者"）。`secret=` 用来构造"形状合法、验签不过"那一档。
        """
        request = self.factory.get(PROBE_PATH, REMOTE_ADDR=remote_addr)
        raw = tokens.issue(key, secret) if token is None else token
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {raw}"
        return request

    def build_without_credentials(self, *, remote_addr=SOURCE_IP):
        """造一个**不带任何凭据**的 GET 请求（那一档的判据见下面各条用例）。"""
        return self.factory.get(PROBE_PATH, REMOTE_ADDR=remote_addr)

    def call(self, request):
        """把请求打进探针视图，回**已渲染**的响应。

        ⚠️ 必须渲染：DRF 的 `Response` 平时是在视图周期的 `finalize_response` 里补上
        「渲染三件套」再 `render()` 的，而 `APIRequestFactory` **不走中间件** ⇒ 不补的话
        读 `response.content` 会抛 `ContentNotRenderedError`。这里用**项目自己的**
        `rendered_response`（`apps/core/envelope.py`）而不是手搓三件套：那份三件套决定的是
        响应的**字节形状**（渲染器 / 媒体类型），手抄一份就会与线上不一致。
        """
        from apps.core.envelope import rendered_response

        return rendered_response(self.view(request))

    def assert_ok(self, request):
        """断言通过，并回**探针视图自己回的那个载荷**（不是信封）。

        ⚠️ 通过路径上拿到的是**裸载荷**（`{"app_key": …}`）：本仓的二次包装是
        `EnvelopeModelViewSet.finalize_response` 做的，而对外端点与这个探针一样是
        `APIView` ⇒ 认证**通过**时没有 `code` 键可断言，有的只是"视图真的跑到了、且拿到
        了那个应用"。
        """
        response = self.call(request)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def assert_401(self, request, msg):
        response = self.call(request)
        self.assertEqual(response.status_code, 401)
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], 401)
        self.assertEqual(payload["msg"], msg)
        self.assertIsNone(payload["data"])
        return payload


class IpAllowlistTests(_BearerRequestMixin, TenantTestCase):
    """§5.2 第 6 步：`ip_allowlist` 为空 **或** 来源 IP 在白名单内。

    ⛔⭐ **改准（T10）**：本类原属签名那条路（类名里带 `Signature`），
    但被测对象（`client_ip` / `_ip_allowed` / 白名单这一步）**今天由 Bearer 路径使用**
    ⇒ 判据**逐条保留**，只把"送哪一套凭据"换成令牌。
    """

    def test_empty_allowlist_allows_any_source(self):
        """白名单**为空 = 不限制**（默认值就是空串，§5.1）。"""
        self.assertEqual(self.app.ip_allowlist, "")
        for remote in (SOURCE_IP, OTHER_SOURCE_IP, "::1"):
            with self.subTest(remote_addr=remote):
                self.assert_ok(self.build(remote_addr=remote))

    def test_source_outside_allowlist_is_401(self):
        """白名单外 ⇒ 401「来源 IP 不在白名单内」。"""
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist="203.0.113.0/24")
        self.assert_401(self.build(remote_addr=OTHER_SOURCE_IP), "来源 IP 不在白名单内")

    def test_single_address_and_network_entries_both_match(self):
        """白名单内 ⇒ 放行；条目**单地址**与**网段**两种写法都认，逗号分隔也认。"""
        for allowlist in (SOURCE_IP, "203.0.113.0/24", f"{OTHER_SOURCE_IP}, {SOURCE_IP}"):
            with self.subTest(ip_allowlist=allowlist):
                ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist=allowlist)
                self.assert_ok(self.build(remote_addr=SOURCE_IP))

    def test_malformed_allowlist_entry_is_skipped_and_the_rest_still_apply(self):
        """一条**写错的**条目 ⇒ 跳过它，其余条目照常生效（取舍理由见 `_ip_allowed`）。"""
        ApiApp.objects.filter(pk=self.app.pk).update(
            ip_allowlist=f"203.0.113.0-24\n{SOURCE_IP}"
        )
        self.assert_ok(self.build(remote_addr=SOURCE_IP))

    def test_allowlist_with_only_malformed_entries_denies(self):
        """对子：表里**只有**写错的条目 ⇒ 一条都不匹配 ⇒ 拒绝（fail-closed，⛔ 不是"空表放行"）。

        ⚠️ 与 `test_empty_allowlist_allows_any_source` 配对：两条一起才说明
        "空串"与"有条目但都不合法"是**两种**不同的状态。
        """
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist="203.0.113.0-24")
        self.assert_401(self.build(remote_addr=SOURCE_IP), "来源 IP 不在白名单内")

    def test_x_real_ip_wins_over_x_forwarded_for(self):
        """第 1 级：**两个头都在** ⇒ 取 `X-Real-IP`（nginx 覆盖写、伪造不了的那个）。

        ⚠️ 这条与下一条**配对**才分辨得出优先级：本用例里 XFF 的**最右**段（`10.0.0.1`）
        与 `X-Real-IP` 故意**不同**，于是"取 XFF 最右"的实现会在这里红。
        白名单只放行 `X-Real-IP` 那个值 ⇒ 取对了才放行。
        """
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist=SOURCE_IP)
        request = self.build(remote_addr="127.0.0.1")
        request.META["HTTP_X_REAL_IP"] = SOURCE_IP
        request.META["HTTP_X_FORWARDED_FOR"] = "9.9.9.9, 10.0.0.1"
        self.assert_ok(request)

    def test_x_forwarded_for_falls_back_to_the_rightmost_entry(self):
        """第 2 级：**没有 `X-Real-IP`** ⇒ 取 `X-Forwarded-For` 的**最右**段（⛔ 不是最左）。

        判据直接钉值：`9.9.9.9, 203.0.113.7` ⇒ 必须是 **`203.0.113.7`**。
        ⚠️ 白名单只放行最右段 ⇒ "取最左"的实现会得到 401 而红；反过来若实现取最右但
        白名单放行的是最左段，也会红 —— 两个方向都锁死。
        """
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist=SOURCE_IP)
        request = self.build(remote_addr="127.0.0.1")
        request.META["HTTP_X_FORWARDED_FOR"] = f"9.9.9.9, {SOURCE_IP}"
        self.assert_ok(request)
        # 反向：白名单只放行**最左**那个（9.9.9.9）⇒ 必须拒（证明取的确实是最右段）。
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist="9.9.9.9")
        other = self.build(remote_addr="127.0.0.1")
        other.META["HTTP_X_FORWARDED_FOR"] = f"9.9.9.9, {SOURCE_IP}"
        self.assert_401(other, "来源 IP 不在白名单内")

    def test_client_ip_prefers_x_real_ip_then_rightmost_then_remote_addr(self):
        """三级优先逐级钉值（直调 `client_ip`，把三级的取值都写出来）。

        ⚠️ 这条不经过视图，所以它单独能分辨"取值顺序"这件事；上两条是它在白名单上的落地。
        """
        from apps.openapi.authentication import client_ip

        request = self.factory.get(PROBE_PATH, REMOTE_ADDR="127.0.0.1")
        # ③ 只有 REMOTE_ADDR
        self.assertEqual(client_ip(request), "127.0.0.1")
        # ② 加 XFF（无 X-Real-IP）⇒ 最右段
        request.META["HTTP_X_FORWARDED_FOR"] = f"9.9.9.9, {SOURCE_IP}"
        self.assertEqual(client_ip(request), SOURCE_IP)
        # ① 再加 X-Real-IP ⇒ 它赢
        request.META["HTTP_X_REAL_IP"] = "198.51.100.7"
        self.assertEqual(client_ip(request), "198.51.100.7")
        # 空白头视同没有 ⇒ 退回下一级（⛔ 不是拿空白当 IP 用）
        request.META["HTTP_X_REAL_IP"] = "   "
        self.assertEqual(client_ip(request), SOURCE_IP)
        request.META["HTTP_X_FORWARDED_FOR"] = " , "
        self.assertEqual(client_ip(request), "127.0.0.1")

    def test_forged_x_forwarded_for_cannot_bypass_the_allowlist(self):
        """⭐ **反例（核心判据）**：伪造 `X-Forwarded-For: <白名单里的 IP>` **绕不过白名单**。

        形状照真机 nginx：`X-Real-IP` 是**覆盖写**的真客户端（伪造不了），XFF 是**追加**的
        （函数把真 IP 追在调用方送来的值后面）。
        这里调用方送 `X-Forwarded-For: <白名单里的 IP>`、而 `X-Real-IP` 是白名单**外**的地址
        ⇒ 必须 **401**。
        ⚠️ 把 `client_ip` 改回"取最左段"时，本用例会**放行**（200）⇒ 红 —— 这正是它存在的理由。
        """
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist=SOURCE_IP)
        request = self.build(remote_addr="127.0.0.1")
        request.META["HTTP_X_REAL_IP"] = OTHER_SOURCE_IP          # 真客户端，白名单外
        request.META["HTTP_X_FORWARDED_FOR"] = f"{SOURCE_IP}, {OTHER_SOURCE_IP}"  # 伪造的在前
        self.assert_401(request, "来源 IP 不在白名单内")
        # 对照：同一个伪造值，若它**真的**是来源（X-Real-IP 也是它）⇒ 放行
        # （证明上一条拒的是"白名单外"，不是"带了 XFF 就拒"）。
        control = self.build(remote_addr="127.0.0.1")
        control.META["HTTP_X_REAL_IP"] = SOURCE_IP
        control.META["HTTP_X_FORWARDED_FOR"] = f"9.9.9.9, {SOURCE_IP}"
        self.assert_ok(control)

    def test_unparsable_source_ip_is_denied_when_allowlist_is_not_empty(self):
        """来源 IP 解析不了 ⇒ 被拒（fail-closed）。

        ⛔⭐ **改准（T7 执行期发现）—— 这条在 T7 开工前**就已经是红的**
        （与本任务改动无关：它由 R1 `00ae0d3e` 留下）：
        原文（逐字保留）：「来源 IP 解析不了 ⇒ **不在白名单内**（fail-closed）」+
        `self.assert_401(request, "来源 IP 不在白名单内")`。
        **实测读数**：`AssertionError: '签名校验失败' != '来源 IP 不在白名单内'`。
        **根因（读码）**：R1 之后 `_failure_count(不可解析来源)` 直接回 `IP_FAILURE_MAX`
        （fail-closed），而认证入口里那次 `_failure_count(...) >= IP_FAILURE_MAX` 的
        失败闸排在 `_ip_allowed(...)` **之前** ⇒ 不可解析来源**先**被闸拒。
        ⇒ 判据的**内核**（"不可解析来源 ⇒ 一律拒"）**没变**，变的只是那句文案的出处
        （闸那一档的口径就是"与凭据不对同一句"）。
        ⭐ **原来那条判据（`_ip_allowed` 对不可解析来源 fail-closed）没有丢**：它在下面
        直接打在**函数**上（`_ip_allowed(...) is False`）—— 那条函数本身 R1 ⛔ 没动。
        ⛔⭐ **改准（T10）**：上面那句实测的文案（签名那句）已随 v1 面删除
        （§13.1）⇒ 今天闸那一档的那句话是 **Bearer 那句**（`应用凭据无效`）。
        """
        # ① 原判据的内核：函数级——不可解析来源**不在**任何非空白名单内（fail-closed）。
        from apps.openapi.authentication import _ip_allowed

        self.assertFalse(_ip_allowed(SOURCE_IP, "not-an-ip"))
        # ② HTTP 级：不可解析来源**当场被拒**（文案是失败闸那一档的）。
        ApiApp.objects.filter(pk=self.app.pk).update(ip_allowlist=SOURCE_IP)
        request = self.build(remote_addr="not-an-ip")
        self.assert_401(request, "应用凭据无效")

    def test_failure_counter_uses_the_same_source_ip(self):
        """失败计数与白名单**共用**同一个取值口径（⛔ 不是两处各取一次）。

        判据：只有 `X-Real-IP` 那一级的地址进桶；伪造的 XFF 最左段不进桶。
        读的是 `OpenApiRateBucket` 行。`ident` 是纯来源 IP，租户隔离靠行住在哪个 schema。
        """
        request = self.build(secret=WRONG_SECRET, remote_addr="127.0.0.1")
        request.META["HTTP_X_REAL_IP"] = SOURCE_IP
        request.META["HTTP_X_FORWARDED_FOR"] = f"9.9.9.9, {SOURCE_IP}"
        self.assert_401(request, "应用凭据无效")
        rows = OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP)
        self.assertEqual(
            list(rows.values_list("ident", "count")),
            [(SOURCE_IP, 1)],
            "只有 X-Real-IP 那一级的地址该进桶（且只记了一次）",
        )
        # ⭐ 改准的那一半：`ident` 是**纯来源 IP**，⛔ 不带 schema 段。
        for ident in rows.values_list("ident", flat=True):
            self.assertNotIn(connection.schema_name, ident)
        # 对照：伪造的 XFF 最左段**不进桶**（它连一行都不该有）。
        self.assertFalse(rows.filter(ident="9.9.9.9").exists())


class ApiAppFailureCounterTests(_BearerRequestMixin, TenantTestCase):
    """⭐ D11：**按来源 IP 的失败计数**（防猜凭据）。

    ⛔⭐ **改准（T10）**：本类原属签名那条路；失败闸本身**今天由 Bearer 路径
    与取令牌端点使用** ⇒ 判据**逐条保留**，只把"送哪一套凭据"换成令牌
    （"签名错"那一档换成"形状合法但验签不过的令牌"）。
    """

    def _fail_once(self, remote_addr=SOURCE_IP):
        request = self.build(secret=WRONG_SECRET, remote_addr=remote_addr)
        self.assert_401(request, "应用凭据无效")

    def test_failures_from_one_source_block_even_a_valid_credential(self):
        """⭐ 计数**真的会拦**：同一来源连错 `IP_FAILURE_MAX` 次 ⇒ 之后**合法令牌**也被拦。

        判据的两半缺一不可：① 合法令牌被拦（阈值真的起作用）；② **另一个来源**的合法令牌
        照样通过（桶是按 IP 分的，⛔ 不是全局桶 —— 只写①的话"任何失败都永久拉黑全部来源"
        也能绿）。
        """
        limit = auth_module.IP_FAILURE_MAX
        self.assertGreater(limit, 0)
        for _ in range(limit):
            self._fail_once()
        self.assert_401(self.build(), "应用凭据无效")
        self.assert_ok(self.build(remote_addr=OTHER_SOURCE_IP))

    def test_one_below_the_limit_still_passes(self):
        """阈值下界：错 `IP_FAILURE_MAX - 1` 次之后，合法令牌**仍然通过**。

        ⚠️ 与上一条配对：只写上一条时，把实现改成"错 1 次就拦"照样绿。
        """
        for _ in range(auth_module.IP_FAILURE_MAX - 1):
            self._fail_once()
        self.assert_ok(self.build())

    def test_blocked_request_gets_the_same_message_as_a_bad_credential(self):
        """被失败闸拦住的请求回**同一句话**（⛔ 不是"太频繁"之类 —— 那会给猜凭据的人反馈）。"""
        for _ in range(auth_module.IP_FAILURE_MAX):
            self._fail_once()
        payload = self.assert_401(self.build(), "应用凭据无效")
        self.assertEqual(payload["msg"], auth_module.BEARER_INVALID_CREDENTIALS_MSG)

    def test_counter_state_is_stored_per_source_ip_inside_the_tenant_schema(self):
        """计数是 `OpenApiRateBucket` 行：`(scope="ip", ident=纯来源 IP, window_start)`。

        每失败一次加一；另一个来源没有计数。租户隔离靠行住在租户 schema，`ident` 里没有 schema 段。
        """
        for expected in (1, 2, 3):
            self._fail_once()
            row = OpenApiRateBucket.objects.get(
                scope=OpenApiRateBucket.Scope.IP, ident=SOURCE_IP
            )
            self.assertEqual(row.count, expected)
        # ② `ident` 是纯来源 IP（⛔ 不带 schema 段）——旧写法里"裸键存在不了"那条断言的新形态。
        idents = set(
            OpenApiRateBucket.objects.filter(scope=OpenApiRateBucket.Scope.IP).values_list(
                "ident", flat=True
            )
        )
        self.assertEqual(idents, {SOURCE_IP})
        self.assertNotIn(connection.schema_name, SOURCE_IP)
        # ③ 另一个来源**没有**计数（桶是按 IP 分的）。
        self.assertFalse(
            OpenApiRateBucket.objects.filter(
                scope=OpenApiRateBucket.Scope.IP, ident=OTHER_SOURCE_IP
            ).exists()
        )

    def test_disabled_app_does_not_consume_the_source_ip_budget(self):
        """⚠️ 有意的口径：**只有"猜凭据"那两档**计入失败（停用 / 过期 / 白名单都不计）。

        应用**存在、密钥正确**、只是被停用（或白名单外）时，来源 IP 不该被扣失败额度 ——
        那是配置状态，不是"猜"。判据：同一个来源连打 `IP_FAILURE_MAX` 次「停用」之后，
        **另一个应用**的合法令牌仍然通过（若实现把停用也算失败，这里会因为同一 IP 被拦而红）。
        ⚠️ 顺带钉住"失败闸排在查库之前"不影响本条：被拦时回的是「凭据无效」那一句，
        而这里每一条回的都是停用那一档的答复（两档在 Bearer 上**是同一句**，
        所以下面用"另一个应用的合法令牌仍通过"这一半守住判别力 —— 它才是本条的牙）。
        """
        ApiApp.objects.filter(pk=self.app.pk).update(state="disabled")
        other = _make_api_app(app_key=OTHER_KEY, app_secret=OTHER_SECRET, name="探针应用乙")
        for _ in range(auth_module.IP_FAILURE_MAX):
            self.assert_401(self.build(), "应用凭据无效")
        self.assert_ok(self.build(key=OTHER_KEY, secret=OTHER_SECRET))
        self.assertIsNotNone(other.pk)

    def test_throttle_gate_runs_before_the_database_lookup(self):
        """失败闸排在**查库之前**：被拦住时连"这个 key 存不存在"都不再问库。

        判据：把 `ApiApp.objects.filter` 桩掉，只允许它被调用**零**次 —— 直接看闸门那一支
        有没有提前返回。⚠️ 用 `mock.patch` 打在**被查找的那个模块**上（E-0029），
        并先证"不接桩时它会被调用"（下一条用例就是那个对照）。
        """
        for _ in range(auth_module.IP_FAILURE_MAX):
            self._fail_once()
        request = self.build()
        with mock.patch.object(
            auth_module.ApiApp.objects, "filter", side_effect=AssertionError("不该查库")
        ):
            self.assert_401(request, "应用凭据无效")

    def test_database_lookup_is_reached_when_the_gate_is_open(self):
        """对照（上一条的判据要有判别力）：闸门开着时**一定会**查库。

        ⚠️ `assertRaises(AssertionError)` 这条对照证明桩**接得上**（E-0029 要求的
        "桩接没接上本身要有判据"）—— 不接上时它会静默地真去查库、上一条就变成恒真。
        ⚠️ 请求**先在 `with` 外面造好**：`build()` 自己也要动库（签发令牌不查库，
        但夹具里那行 `ApiApp.objects.create` 会），把它放进来会撞上桩。
        """
        request = self.build()
        with mock.patch.object(
            auth_module.ApiApp.objects, "filter", side_effect=AssertionError("桩生效了")
        ):
            with self.assertRaises(AssertionError):
                self.call(request)


class FailureMessageContractTests(TenantTestCase):
    """今天仍在服役的失败文案是**对外契约**（文档里逐字列出）。"""

    def test_the_surviving_messages_are_word_for_word(self):
        """⛔ 钉**值**、不钉常量（E-0024）：逐字写在这里，改实现就得改用例。

        ⛔⭐ **改准（T10）**：本条原来钉的是签名那条路的**五句**
        （缺参数 / 已过期 / 校验失败 / 已停用 / 不在白名单）—— 那五句随 v1 面删除（§13.1）。
        今天仍在服役的对外文案只有**三句**（两句在对外的认证层、一句给取令牌端点的三档）：
        """
        self.assertEqual(auth_module.IP_NOT_ALLOWED_MSG, "来源 IP 不在白名单内")
        self.assertEqual(auth_module.BEARER_INVALID_CREDENTIALS_MSG, "应用凭据无效")
        self.assertEqual(auth_module.BEARER_EXPIRED_MSG, "令牌已过期")

    def test_the_messages_are_distinct(self):
        """三句话**互不相同**（否则"靠 msg 区分"这件事本身不成立）。"""
        messages = {
            auth_module.IP_NOT_ALLOWED_MSG,
            auth_module.BEARER_INVALID_CREDENTIALS_MSG,
            auth_module.BEARER_EXPIRED_MSG,
        }
        self.assertEqual(len(messages), 3)


class AuthenticateHeaderTests(_BearerRequestMixin, TenantTestCase):
    """`authenticate_header()` 必须返回**非空**，否则 401 会丢 `WWW-Authenticate`。"""

    def test_view_sets_the_challenge_header_on_401(self):
        """走**真实视图**看那条头（⛔ 不只断言方法返回值 —— 那是自指判据）。

        ⚠️ 判据是「401 的响应上真的带着 `WWW-Authenticate`」：DRF 是从
        `authenticate_header()` 算出这条头塞进响应的，所以把它改成返回 `None` 时**本用例红**
        （本项目的异常处理器会保住状态码 401，消失的只有这条头）。
        """
        request = self.factory.get(PROBE_PATH, REMOTE_ADDR=SOURCE_IP)
        response = self.view(request)
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.headers.get("WWW-Authenticate"))

    def test_header_value_is_not_leaking_secrets(self):
        """挑战头里⛔ 不带密钥 / 应用信息（它是对外可见的字符串）。"""
        header = ApiAppBearerAuthentication().authenticate_header(
            self.factory.get(PROBE_PATH)
        )
        self.assertTrue(isinstance(header, str) and header)
        self.assertNotIn(APP_SECRET, header)
        self.assertNotIn(APP_KEY, header)

    def test_a_valid_token_returns_the_app_and_an_anonymous_user(self):
        """✅ 正向（⛔ 缺了它，下面所有 401 断言可能是"这条路对谁都不通"凑出来的）。

        ⚠️ 判据用 `isinstance(user, AnonymousUser)`（⛔ 不是"不是 `None`"——`(None, app)`
        在那种弱判据下也绿）；两半都打：**探针视图**（走完整 DRF 周期，证明视图真的跑到了）
        + **直调认证类**（拿到那段返回值自己判类型）。
        """
        payload = self.assert_ok(self.build())
        self.assertEqual(payload["app_key"], APP_KEY)
        self.assertTrue(payload["auth_is_app"])
        self.assertFalse(payload["user_is_authenticated"])
        # 直调：`(AnonymousUser(), app)` 的**左边那个值的类型**自己判（⛔ 不靠探针的布尔）。
        user, app = ApiAppBearerAuthentication().authenticate(self.build())
        self.assertIsInstance(user, AnonymousUser)
        self.assertEqual(app.pk, self.app.pk)


class AuthenticationClassContractTests(TenantTestCase):
    """认证类的**装配口径**（D13：逐视图装配，⛔ 不进全局默认）。"""

    def test_class_is_not_in_the_global_default_authentication_classes(self):
        """⛔ 本类**不在** DRF 的全局默认里：装上去会让页面的会话认证被旁路（D13 的代价栏）。

        判据读的是**运行时生效的** `api_settings`（⛔ 不是读 settings 源码字面量）——
        显式装配过的视图由自己的 `authentication_classes` 负责，与全局默认无关。
        ⚠️ 除了"本类不在里面"，还钉住全局默认**恰好**是会话认证这一条：否则把全局默认清空
        （= 所有页面端点变匿名）也能让本用例绿。
        ⛔⭐ **改准（T10）**：本判据原先钉的是签名那个认证类（已删除，§13.1）
        ⇒ 换成今天唯一存在的对外认证类。**判据内核一字未改。**
        """
        from apps.core.authentication import TenantBoundAuthentication
        from rest_framework.settings import api_settings

        defaults = list(api_settings.DEFAULT_AUTHENTICATION_CLASSES)
        self.assertNotIn(ApiAppBearerAuthentication, defaults)
        # （打手 App 线，D-0269）：全局多了一条 `AppTokenAuthentication`
        # （Bearer 通道，Session 在后）。判据内核不变：**对外认证类**仍然一个都不在全局里；
        # 这里按新配置改准，⛔ 不是放宽。
        from apps.core.authentication import AppTokenAuthentication

        self.assertEqual(
            defaults, [TenantBoundAuthentication, AppTokenAuthentication]
        )

    def test_probe_proves_the_auth_class_is_actually_attached(self):
        """探针视图不带任何凭据 ⇒ 401（证明认证类**真的挂上了**）。

        ⚠️ 若探针忘了 `authentication_classes`，请求会走全局默认的会话认证 ⇒ 匿名**同样** 401，
        只有 `msg` 不一样 ⇒ 本用例用**逐字 msg** 把两者分开：⛔ 不用
        `assertNotEqual(msg, 框架那句)`，那要手抄框架的英文文案（会随 DRF 版本漂）。
        """
        from apps.core.envelope import rendered_response

        response = rendered_response(
            PROBE_HANDLER(APIRequestFactory().get(PROBE_PATH, REMOTE_ADDR=SOURCE_IP))
        )
        self.assertEqual(response.status_code, 401)
        payload = json.loads(response.content)
        self.assertEqual(payload["msg"], "应用凭据无效")


class CredentialSpecDrivenFailureCounterTests(TenantTestCase):
    """⭐⭐ T2 修复轮（2026-10-08）：失败计数 / `_mark_used` **不再有"静默按上家侧"那一支**。

    ## 这条判据为什么值钱

    改造前这五处（`_failure_count` / `_count_failure` / `count_ip_failure` /
    `ip_failure_gate_already_exceeded` / `_mark_used`）的 `spec` 都带默认值
    （`= OPENAPI_SPEC`）⇒ **忘了传**这件事完全静默：闸照常工作，只是落进/改动的是
    **上家侧**那一族的桶与凭据行。而同一类错误在幂等台账那几处（`_find_existing` 的
    `spec`）是**必填**、当场 `TypeError` —— 同一种错一个响亮一个静默，是最坏的形状。

    ## 判据（五条，⛔ 不接受只有前两条）

    ① 用**非默认那一份** spec 调两个计数出口 ⇒ 行落在**那一族的桶表**里、
       `OpenApiRateBucket` **一行没多**；阈值 / 窗口 / `scope` 也取**那一份**的值；
    ② **对照**：默认 spec 的那条路仍然落 `OpenApiRateBucket`（否则 ① 可能是
       "这个函数压根不写桶"的恒真式）；
    ③ `_mark_used` 同理：非默认 spec ⇒ 改的是**那一族**的凭据行，上家侧那行**一个字没动**；
    ④ 五个函数的 `spec` 都是**必填 keyword-only** ⇒ 漏传 = `TypeError`（签名层判据）；
    ⑤ ⛔ **不是"改成"**：默认 spec 那条路上的读数与改造前**逐字相同**（阈值 20 / 窗口 60）。

    ⚠️ 本任务⛔ **不实现打手侧**（那是 T2b）：这里那份"另一族"的 spec 是**用例内**用
    `dataclasses.replace` 拼的探针（唯一改动的字段是模型与额度），⛔ 没有往仓库里加
    `TAKER_SPEC` / 任何打手侧类。
    """

    #: 来源 IP 一律用文档保留段（合成值，`AGENTS.md` §5-C）。
    IP = "203.0.113.77"

    def setUp(self):
        connection.set_tenant(self.tenant)
        super().setUp()

    @staticmethod
    def _probe_spec():
        """另一族那份 spec：桶表 / 凭据模型 / 额度 / 窗口都**与上家侧不同**。"""
        return replace(
            OPENAPI_SPEC,
            bucket_model=TakerApiRateBucket,
            app_model=TakerApp,
            failure_max=3,
            failure_window_seconds=120,
        )

    def test_the_failure_counters_write_into_the_spec_bucket(self):
        """① + ② + ⑤：读 / 写 / 阈值 / 窗口全部取自传进来的那一份。"""
        spec = self._probe_spec()
        # ① 非默认那一份 ⇒ 行落那一族的桶表。
        _count_failure(self.IP, spec=spec)
        self.assertEqual(
            list(TakerApiRateBucket.objects.values_list("scope", "ident", "count")),
            [(spec.failure_scope, self.IP, 1)],
        )
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)
        self.assertEqual(_failure_count(self.IP, spec=spec), 1)
        # 阈值也取那一份（= 3，⛔ 不是上家侧的 20）：第 4 次才超限。
        self.assertIs(count_ip_failure(self.IP, spec=spec), False)
        self.assertIs(count_ip_failure(self.IP, spec=spec), False)
        self.assertIs(ip_failure_gate_already_exceeded(self.IP, spec=spec), False)
        self.assertIs(count_ip_failure(self.IP, spec=spec), True)
        self.assertIs(ip_failure_gate_already_exceeded(self.IP, spec=spec), True)
        self.assertEqual(
            list(TakerApiRateBucket.objects.values_list("ident", "count")),
            [(self.IP, 4)],
        )
        # ⛔ 全程上家侧那张桶表**一行没多**。
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)

        # ② 对照：默认那一份 ⇒ 落 `OpenApiRateBucket`（证明上一条不是"压根不写桶"）。
        count_ip_failure(OTHER_SOURCE_IP, spec=OPENAPI_SPEC)
        self.assertEqual(
            list(OpenApiRateBucket.objects.values_list("scope", "ident", "count")),
            [(OpenApiRateBucket.Scope.IP, OTHER_SOURCE_IP, 1)],
        )
        # ⑤ 默认那一份的阈值仍是改造前那个 20（⛔ 别把它当"顺手写个 20"）。
        self.assertEqual(OPENAPI_SPEC.failure_max, 20)
        self.assertEqual(OPENAPI_SPEC.failure_window_seconds, 60)

    def test_the_failure_gate_of_the_default_family_still_blocks_at_twenty(self):
        """⑤ 对照（判据本身）：默认那一份连打 21 次 ⇒ 第 21 次起被闸拦（改造前的读数）。

        ⚠️ 这条钉的是"改必填**没有顺手改口径**"：阈值仍是 20、`>` / `>=` 的分别也照旧。
        """
        for _ in range(20):
            self.assertIs(count_ip_failure(self.IP, spec=OPENAPI_SPEC), False)
        self.assertIs(ip_failure_gate_already_exceeded(self.IP, spec=OPENAPI_SPEC), False)
        self.assertIs(count_ip_failure(self.IP, spec=OPENAPI_SPEC), True)
        self.assertIs(ip_failure_gate_already_exceeded(self.IP, spec=OPENAPI_SPEC), True)

    def test_mark_used_updates_the_spec_credential_row(self):
        """③ `_mark_used` 改的是**那一族**的凭据行（⛔ 不是上家侧那一行）。"""
        spec = self._probe_spec()
        creator, _ = get_user_model().objects.get_or_create(
            username="openapi-auth-probe", defaults={"password": "unused-in-this-test"}
        )
        taker_app = TakerApp.objects.create(
            name="T2 修复轮探针打手应用甲",
            app_key="probe-t2fix-taker-key-0001",
            app_secret="probe-t2fix-taker-secret-000001",
            taker=creator,
            created_by=creator,
        )
        api_app = _make_api_app(app_key="probe-t2fix-openapi-key-01")
        self.assertIsNone(TakerApp.objects.get(pk=taker_app.pk).last_used_at)
        self.assertIsNone(ApiApp.objects.get(pk=api_app.pk).last_used_at)

        ApiAppBearerAuthentication._mark_used(taker_app, spec=spec)
        self.assertIsNotNone(
            TakerApp.objects.get(pk=taker_app.pk).last_used_at,
            "非默认那一份 spec 的 `_mark_used` 没有更新**那一族**的凭据行",
        )
        self.assertIsNone(
            ApiApp.objects.get(pk=api_app.pk).last_used_at,
            "`_mark_used` 改到了上家侧那一行 ⇒ app_model 没经 spec 取",
        )

        # ② 对照：默认那一份 ⇒ 改的是 `ApiApp` 那一行。
        ApiAppBearerAuthentication._mark_used(api_app, spec=OPENAPI_SPEC)
        self.assertIsNotNone(ApiApp.objects.get(pk=api_app.pk).last_used_at)
        self.assertIsNotNone(TakerApp.objects.get(pk=taker_app.pk).last_used_at)

    def test_omitting_the_spec_is_a_type_error(self):
        """④ 签名层判据：漏传 `spec` ⇒ `TypeError`（⛔ 不是静默用上家侧那一份）。"""
        with self.assertRaises(TypeError):
            _failure_count(self.IP)
        with self.assertRaises(TypeError):
            _count_failure(self.IP)
        with self.assertRaises(TypeError):
            count_ip_failure(self.IP)
        with self.assertRaises(TypeError):
            ip_failure_gate_already_exceeded(self.IP)
        with self.assertRaises(TypeError):
            ApiAppBearerAuthentication._mark_used(_make_api_app(app_key="probe-t2fix-key-0002"))
        # ⛔ 一次都不该写桶（TypeError 发生在**任何库读写之前**）。
        self.assertEqual(OpenApiRateBucket.objects.count(), 0)
        self.assertEqual(TakerApiRateBucket.objects.count(), 0)
