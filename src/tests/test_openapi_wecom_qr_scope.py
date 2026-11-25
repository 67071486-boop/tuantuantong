"""⭐ **对外端点全集**上「锁定应用只准打六条」的判据（设计文档 §6 / D3 / D4 / D5；T2）。

## 本文件证什么（三块，缺一块就是假绿）

A. ⭐ **一张新建的「全集网」**：`apps/openapi/urls.py` 的 `urlpatterns` **程序化**收出全部
   `(HTTP 方法, url_name)`（递归展开 include、按视图类**实际定义**的方法），再做**两向断言**：
   ① 收出来的集合 **`==`** 「白名单六条 ∪ **没装** Bearer 认证的那十二条」——⛔ 不是抽几条写死、
   ⛔ 不是 `issubset` / `toContain`；② 锁定应用打**白名单外、且真的过认证层**的每一条
   ⇒ **HTTP 403 + 业务码 403**（= 本仓既有那一档 403 的信封形状，见下面 FORBIDDEN_HTTP 的
   改准块；⛔ spec D5 字面那句「HTTP 200」在本仓的异常处理器里产不出来）。
   ⇒ 新增一条对外端点会**同一次**让这张网红
   （要么它进白名单，要么它被证明根本没装 Bearer 认证）。

   ⚠️ **为什么必须新建这张网**（spec §10.1 第 2 条点名）：现仓的三张候选都会漏 ——
   `test_no_route_500` 的显式路由腿**跳过带路径转换器的路由**（本项目 v2 那几条全带
   `order_no` ⇒ 整条落进它自己登记的盲区）、`test_p4_no_500` 只收「**共享转换器路由**」那一张
   恰好集合（它是畸形入参网，不是端点全集网）、`test_operation_log_coverage` / `test_rbac_no_500`
   是埋点与权限节点网（与锁定应用的白名单无关）。⛔ **复用其中任何一张都会漏**。

   ⚠️⭐ **「没装 Bearer 认证」那十二条不是托词，是实测的装配事实**（取令牌端点 + `apps/` 与
   `wecom-qr-app/` 的十一条管理端点，后者吃**全局默认的会话认证**）：本文件有一条**拓扑对拍**
   拿真 urlconf 的 `authentication_classes` 逐条与那张表核对；还有一条把代价如实钉住 ——
   锁定应用的令牌打那八条管理端点回的是会话层的 **401**（比 403 更靠前，安全结论成立）。

B. ⭐ **同名挂多方法的四组，逐组都有反例**（D4 明文点名，红队 2026-10-09 抓到初稿漏了第一组）：

   | `url_name` | 放行 | 同一条路由上的反例 |
   |---|---|---|
   | `open-order-query` | `GET` | **`POST` 录单 ⇒ 403** |
   | `open-order-detail` | `GET` | **`PATCH` 改单 ⇒ 403** |
   | `open-order-status` | `GET` | **`POST` 改状态 ⇒ 403** |
   | `open-order-images` | `GET` **与** `POST` | —— 两个都放行（这组是唯一的例外） |

C. **对照（与 A/B 同等重要）**：**通用应用**打**同一批端点** ⇒ **一条都不 403**。
   没有它，A 组的 403 可能由「这条路对**谁**都不生效」凑出来（E-0004 同族：没有对照的
   负断言可能是空转）。⚠️ 这条对照还兼着 D8 的判据：本线的收窄**不许**碰到既有应用。

D. 两个 fail-closed 边界 + 对照：`url_name` 为空 / `resolver_match` 整个取不到 ⇒ **拒**；
   **通用应用在同一情形下不受影响**（证明这条闸只认锁定应用）。外加一条：锁定应用打
   **别人的租户域名** ⇒ 仍是 **401**（既有口径不变，⛔ 没有被这条新闸改成 403）。

## 判据写在**哪一层**（⛔ 别只看状态码）

- 「放行」那一半断言的是：**HTTP 不是 403 且信封 `code` 不是 403**（⛔ 不是"HTTP 200 + code 0"）
  —— 白名单那六条各有自己的业务码，`1000` / `1002` 都是**合法答复**；把它们全造出成功态
  是别的网的事（本网证明的是"这条闸没拦它"）；
- 「拒绝」那一半断言的是**三件事一起**：HTTP `200`、信封 `code == 403`、`msg` **逐字**等于
  实现里那句话（D5 的越权形状）。⚠️ `msg` 比对用的是本文件里的**字面量**
  （`AGENTS.md` §5-G / E-0024：钉值不钉常量），另有一条把实现常量与那个字面量对齐的钉子。

## 夹具纪律

- 凭据与订单号一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的凭据 / 订单无关；
- ⛔ **不发真实网络请求**：全部经 Django 测试客户端打**真实 URLconf + 真实中间件 + 真认证**。

## ⚠️ 本文件**不碰**的东西（分属别的任务 / 别的网）

- `img_type` 按 `kind` 分叉（D6）、群码取票与认领的领域行为 ⇒ `tests/test_openapi_order_images.py`；
- 「每租户唯一」那几条（D2）⇒ `tests/test_openapi_wecom_group_qr.py`；
- 锁定应用自己的**管理端点**（`/api/open/wecom-qr-app/`）⇒ `tests/test_openapi_wecom_qr_app.py`。
  它们在本文件里只作为**被拒的端点**出现（锁定应用打不了管理端，与通用应用一样）。
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, path, resolve, reverse
from django.urls.exceptions import NoReverseMatch
from django_tenants.test.cases import TenantTestCase
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.openapi import tokens
from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.models import ApiApp, ApiAppKind
from apps.platform.provisioning import provision_tenant
from apps.openapi.permissions import (
    WECOM_GROUP_QR_ALLOWED_ENDPOINTS,
    WECOM_GROUP_QR_ENDPOINT_NOT_ALLOWED_MSG,
    is_endpoint_allowed,
)

User = get_user_model()

# ── 常量（全部合成值）────────────────────────────────────────────────────────

#: 走一遍 URL 时填进 `<order_no:…>` 的那个合成订单号（20 位数字，与真实单号**同形**）。
PROBE_ORDER_NO = "20261009000000000091"
#: 走一遍 URL 时填进 `<int:pk>` / `<int:delivery_pk>` 的值。
PROBE_PK = 1

#: 来源 IP（RFC 5737 的文档保留段，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.31"

#: 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）。
LOCKED_KEY = "probescopeokey0001"
LOCKED_SECRET = "probe-scope-secret-00000000000001"
GENERAL_KEY = "probescopeokey0002"
GENERAL_SECRET = "probe-scope-secret-00000000000002"

#: 越权那一档的**两个数字**（都写死在用例里 ⇒ 实现改掉任一个都会红）。
#: ⚠️⭐ **HTTP 那一个是 403、⛔ 不是 D5 字面上写的 200** —— 这是本任务实测出来的偏差：
#: 本仓的异常处理器对没枚举的异常类型把 `code` 与 HTTP 状态码取**同一个数**
#: ⇒「HTTP 200 + 业务码 403」这个组合在**不改 `apps/core/exceptions.py`** 的前提下产不出来。
#: 逐条理由与两难见 `apps/openapi/permissions.py` 的 `EndpointOutOfScope` docstring。
#: ⚠️ 判据仍钉**四件事一起**（HTTP 码 / `code` / `msg` / `data`），⛔ 没有放宽成"随意一个 403"。
FORBIDDEN_HTTP = 403
#: 信封里的业务码（与既有那一档 403 逐字同值）。
FORBIDDEN = 403
#: D5 那句话的**字面量**（⛔ 不从实现 import 来做断言，见模块 docstring）。
FORBIDDEN_MSG = "该应用无权访问本端点"
#: 401 那一档的 HTTP 码（"打别人的租户域名"那条用的既有口径）。
UNAUTHORIZED = 401

#: ⛔ **不装** `ApiAppBearerAuthentication` 的对外端点：本线这条闸在它们身上**根本跑不到**
#: （认证类没被装配 ⇒ 一条 Bearer 令牌到不了认证层）。两类，共 **12** 条（写这条时实测）：
#:
#: 1. `open-oauth-token`（1 条）—— 取令牌端点自己就是凭据入口，`authentication_classes = []`；
#: 2. `apps/` 与 `wecom-qr-app/` 那**十一条管理端点** —— 它们吃**全局默认**的会话认证
#:    （`ApiAppAdminView` 刻意不声明 `authentication_classes`，D13），Bearer 认证**不在**
#:    全局默认里（`config/settings.py` 的 `DEFAULT_AUTHENTICATION_CLASSES` 只有
#:    `TenantBoundAuthentication`）。
#:
#: ⚠️⭐ **这是一条实测的拓扑事实，不是"我们不想管它们"的托词**：本文件里那条
#: `test_the_bearer_gated_set_is_exactly_the_six_plus_the_eight` 会拿
#: **真 urlconf 的 `authentication_classes`** 与这张表**逐条对拍**（⛔ 不是自证：
#: 只要有人把这十二条里的任一条改成装 Bearer 认证，那张表就与读数对不上、当场红）。
#: ⚠️ 代价如实登记在 `test_the_admin_endpoints_are_refused_by_the_session_layer` 里：
#: 锁定应用的令牌打这十一条管理端点，答复是会话层那句 **401**，⛔ 不是 D5 那档 403。
BEARER_GATED_EXEMPT_ENDPOINTS = frozenset({
    ("POST", "open-oauth-token"),
    ("GET", "open-app-list-create"),
    ("POST", "open-app-list-create"),
    ("PATCH", "open-app-detail"),
    ("POST", "open-app-rotate-secret"),
    ("GET", "open-app-deliveries"),
    ("POST", "open-app-delivery-retry"),
    ("GET", "open-wecom-qr-app"),
    ("POST", "open-wecom-qr-app"),
    ("PATCH", "open-wecom-qr-app"),
    ("POST", "open-wecom-qr-app-rotate-secret"),
    ("GET", "open-wecom-qr-app-deliveries"),
})

#: 上表里**管理端点**那一档（用来把"取令牌端点"与"管理端点"两组分开钉）。
#: ⚠️ 它是上表减去取令牌端点 —— ⛔ 不另抄一份清单（两份必然漂）。
ADMIN_ENDPOINTS = frozenset(
    endpoint
    for endpoint in BEARER_GATED_EXEMPT_ENDPOINTS
    if endpoint != ("POST", "open-oauth-token")
)

#: ⭐ **应该被本线那道闸拒**的那些端点：**装**了 Bearer 认证（⇒ 请求真的会走到认证层）、
#: 但**不在**白名单里。⛔ 它是**显式枚举**的（判据要能红：漏列一条下面的恰好集合断言就红，
#: 多列一条则底下那条 `test_every_non_whitelisted_endpoint_is_refused_for_the_locked_app`
#: 会真的去打它并断言 403）。
#: ⚠️ 与 `BEARER_GATED_EXEMPT_ENDPOINTS` 那十二条**互不相交**（下面有用例钉这一点）。
REFUSED_UNDER_THE_GATE = frozenset({
    ("POST", "open-order-query"),        # 录单（D4 点名的第一组反例）
    ("PATCH", "open-order-detail"),      # 改单（第二组）
    ("POST", "open-order-status"),       # 改状态（第三组）
    ("PUT", "open-order-assign-price"),  # 改发单价
    ("GET", "open-rank-list"),
    ("GET", "open-bill-list"),
    ("GET", "open-game-list"),
    ("GET", "open-project-list"),
})

#: spec D4 逐字列出的六条（在本文件里**再写一遍**，用来两向对拍实现里的那张表：
#: 只断言 `== 实现常量` 是自指断言，改坏常量它不会红 —— `AGENTS.md` §5-G / E-0024）。
SPEC_D4_ENDPOINTS = frozenset({
    ("GET", "open-order-query"),
    ("GET", "open-order-detail"),
    ("GET", "open-order-status"),
    ("GET", "open-order-images"),
    ("POST", "open-order-images"),
    ("POST", "open-order-image-ticket"),
})

#: ⭐ D4 点名的那四组「同一个 `url_name` 上挂着第二个方法」的逐组反例。
#: `(url_name, 放行的方法, 必须被拒的方法或 None)`。
#: ⚠️ 第四组的反例是 `None`：那一条**两个方法都放行**，它留在表里是为了让「四组」这件事
#: 在判据代码里**看得见**（⛔ 不是"这组不用测"）——它的正面腿在下面那条用例里显式写了。
METHOD_PAIRS = (
    ("open-order-query", "GET", "POST"),        # POST = 录单
    ("open-order-detail", "GET", "PATCH"),      # PATCH = 改单
    ("open-order-status", "GET", "POST"),       # POST = 改状态
    ("open-order-images", "GET", None),         # 认领（POST）也放行
)

#: 本网**至少**要收出这么多条 `(方法, url_name)`。
#: ⚠️ 它是**下界**、不是恰好值 —— `urls.py` 由别的任务同时改（本线还在加端点），
#: 恰好值由「与 D4 的白名单两向对拍」承担（那一条才是承重的）。
#: 取 24（写这条时实测 19 条路由 / 27 条 `(方法, url_name)`）。
MIN_UNIVERSE_SIZE = 24

#: 白名单**之外**、且**真的过 Bearer 认证层**的条数下界（写这条时实测**恰好 8 条**：
#: `open-order-query` 的 `POST`（录单）、`open-order-detail` 的 `PATCH`（改单）、
#: `open-order-status` 的 `POST`（改状态）、`assign-price` 的 `PUT`，以及
#: `v1/ranks|bills|games|projects` 四条只读）。
#: ⚠️ 同样只是下界：上界由「全集 == 白名单 ∪ 未过认证层那十二条」那条恰好断言承担。
MIN_REFUSED_UNDER_THE_GATE = 8

#: 视图类上会被算作"这条路由收这个方法"的五个方法名（对外路由全是 DRF 的 `APIView` 子类）。
HTTP_METHOD_NAMES = ("get", "post", "put", "patch", "delete")

#: 路径段里的转换器占位符（`<int:pk>` / `<order_no:order_no>` / `<pk>`）。
_CONVERTER_RE = re.compile(
    r"<(?:(?P<conv>[A-Za-z_][A-Za-z0-9_]*):)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)>"
)

#: 每个转换器填什么（值一律合成）。⚠️ `order_no` 是**本仓自定义**的转换器
#: （`apps/business/converters.py`，只排除控制字符）⇒ 它接受这个 20 位数字。
SAMPLE_BY_CONVERTER = {
    "int": str(PROBE_PK),
    "str": "probe-segment",
    "slug": "probe-segment",
    "path": "probe/segment",
    "uuid": "00000000-0000-0000-0000-0000000000a1",
    "order_no": PROBE_ORDER_NO,
}


def _view_class(callback):
    """取出路由背后的**视图类**（`View.as_view()` 的返回值带 `view_class` / `cls`）。

    ⛔ 别改成"发一遍请求看回什么"：本网要的是「这个 `(方法, url_name)` 组合**存不存在**」，
    而它由视图类上有没有那个 handler 定义（D4 的口径逐字如此）。发请求会把
    `HEAD` / `OPTIONS`（DRF 的出厂行为）也拉进来，那些不是任何一条契约。
    """
    return (
        getattr(callback, "view_class", None)
        or getattr(callback, "cls", None)
        or getattr(callback, "initkwargs", {}).get("view_class")
    )


def _methods_of(callback) -> tuple[str, ...]:
    """这条路由的视图类**实际定义**的方法（大写、按固定顺序）。"""
    view_class = _view_class(callback)
    if view_class is None:
        return ()
    return tuple(name.upper() for name in HTTP_METHOD_NAMES if hasattr(view_class, name))


def _concrete_path(route: str) -> str:
    """把一条 `path()` 的 route 变成**可解析**的具体 URL（相对 openapi 前缀）。

    ⚠️ 转换器按 `apps.business.converters` 的真实口径填（`order_no` 只排除控制字符
    ⇒ 20 位数字是合法段；`int` 填 `1`）。⛔ 不手抄每条路由的示例值 —— 那会随路由表漂移。
    """

    def _fill(match: re.Match) -> str:
        converter = match.group("conv") or "str"
        return quote(SAMPLE_BY_CONVERTER.get(converter, "probe-segment"), safe="")

    return _CONVERTER_RE.sub(_fill, route)


def _openapi_mount_prefix(resolver) -> str:
    """从**根 urlconf** 里读出 `apps.openapi.urls` 的挂载前缀（`api/open/`）。

    ⛔ 不手抄 `"/api/open/"`：挂载点搬走了而前缀没跟着改，收出来的路径会全部 404
    ⇒ 那会让"拒绝"那一半**假绿**（403 断言变成 404 断言）。找不到就**响亮地报错**。
    """
    from apps.openapi import urls as openapi_urls

    for pattern in resolver.url_patterns:
        if (
            isinstance(pattern, URLResolver)
            and getattr(pattern, "urlconf_module", None) is openapi_urls
        ):
            return str(pattern.pattern)
    raise AssertionError("根 urlconf 里找不到 apps.openapi.urls 的挂载点（本网的前缀假设失效）")


def collect_openapi_endpoints():
    """⭐ 从 `apps/openapi/urls.py` 收**全部** `(HTTP 方法, url_name)` + 它们的可解析路径。

    ## 返回

    `(universe, path_by_endpoint, view_by_url_name)`：

    - `universe`：`frozenset[(方法, url_name)]` —— 对外端点**全集**；
    - `path_by_endpoint`：`{(方法, url_name): "/api/open/…"}` —— 每条可直接发的 URL；
    - `view_by_url_name`：`{url_name: 视图}` —— 供"这条路由装了什么认证类"那几条判据用。

    ## ⛔ 三条实现纪律

    1. **递归展开 include**（`URLResolver` / `URLPattern` 两类分开走）：今天的
       `apps/openapi/urls.py` 是平的，但"平的"是**今天的巧合** —— 写死一层的话，
       哪天多一个 `include()`，新端点会**静默**掉出这张网（这正是本网存在的理由）；
    2. **前缀从根 urlconf 的真挂载点读**（见 `_openapi_mount_prefix`）；
    3. **`url_name` 必须非空、且不重复**：本网的全部判据都以 `url_name` 为身份（D3），
       空名路由在这里**响亮地报错**（⛔ 不是静默跳过）；同名两条路由会让
       `(方法, url_name)` 静默合并（那也是**身份判据失效**，同样响亮地报错）。
    """
    from django.urls import get_resolver

    prefix = _openapi_mount_prefix(get_resolver())
    root = URLResolver(prefix, "apps.openapi.urls")

    universe: set[tuple[str, str]] = set()
    path_by_endpoint: dict[tuple[str, str], str] = {}
    view_by_url_name: dict[str, object] = {}

    def walk(patterns, accumulated: str) -> None:
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, accumulated + str(pattern.pattern))
                continue
            if not isinstance(pattern, URLPattern):
                continue
            if not pattern.name:
                raise AssertionError(
                    f"对外路由缺 name=（本网的判据以 url_name 为身份）：{pattern.pattern}"
                )
            url = "/" + _concrete_path(accumulated + str(pattern.pattern))
            view_by_url_name[pattern.name] = pattern.callback
            for method in _methods_of(pattern.callback):
                key = (method, pattern.name)
                if key in universe:
                    raise AssertionError(f"重复的 (方法, url_name)：{key}（收集会静默合并）")
                universe.add(key)
                path_by_endpoint[key] = url

    # ⚠️ 累积串**从挂载前缀本身开始**（`root.pattern` 就是 `api/open/`）：只把它交给
    # `URLResolver(...)` 而不加进累积串的话，收出来的路径会缺一段（实测：全部变成
    # `/v1/orders/` 这种 —— 请求会 404，而"拒绝"那一半断言的是 403 ⇒ 当场红，不会静默；
    # 但这条自证用例把它变成**一条**清楚的失败，而不是 20 条 404）。
    walk(root.url_patterns, prefix)
    return frozenset(universe), path_by_endpoint, view_by_url_name


# ── D 组用的探针 urlconf（`url_name` 为空 / `resolver_match` 缺失）────────────────
#
# ⚠️ **为什么不把探针路由挂进真 urlconf**：真 urlconf 里加一条无名路由会撞上
# `test_operation_log_coverage` 那张按 `get_resolver()` 枚举端点的承重网（§17.1：
# 「先登记后建端点得到的是红」）。所以这里用一个**只在本文件内**的 urlconf 模块，
# 经 `override_settings(ROOT_URLCONF=…)` 临时顶掉根 urlconf，其余（认证 / 中间件 /
# 租户解析 / 异常处理器）全部照真实形态走。
PROBE_URLCONF = "tests.test_openapi_wecom_qr_scope"


@api_view(["GET"])
@authentication_classes([ApiAppBearerAuthentication])
#: ⚠️ 探针视图**必须** `AllowAny`：对外端点⛔ 不能用 `IsAuthenticated`
#: （`AnonymousUser.is_authenticated` 是 `False` ⇒ 合法请求会被判 401）。
@permission_classes([AllowAny])
def _scope_probe_view(request):
    """回显「认证把什么挂上来了」+ 请求自己看到的 `resolver_match.url_name`。

    ⚠️ 先读 `request.user`：DRF 的认证是**惰性**的，视图不碰它就不会跑 —— 那样探针恒 200、
    零判别力。真端点由权限类或视图体去碰它，这里的写法等价。
    """
    app = getattr(request, "_api_app", None)
    resolver_match = getattr(request, "resolver_match", None)
    return Response(
        {
            "app_key": app.app_key if app is not None else None,
            "url_name": getattr(resolver_match, "url_name", None),
            "user_is_authenticated": request.user.is_authenticated,
        }
    )


_SCOPE_PROBE_HANDLER = _scope_probe_view.view_class.as_view()

#: ⭐ 这份模块**自己就是**那个临时 urlconf（`ROOT_URLCONF` 指到本模块）
#: ⇒ 下面这两条 `path()` 就是它的全部路由。⚠️ 它们**只**在 `override_settings` 生效期间被读。
#: 一条**有 `name=`**、一条**刻意没有**：D 组的两条判据分别打它们。
urlpatterns = [
    path("api/open/probe/named/", _SCOPE_PROBE_HANDLER, name="open-scope-probe-named"),
    path("api/open/probe/unnamed/", _SCOPE_PROBE_HANDLER),
]

PROBE_NAMED_PATH = "/api/open/probe/named/"
PROBE_UNNAMED_PATH = "/api/open/probe/unnamed/"

#: 第二个租户（D 组最后那两条要用它）：域名与 schema 都刻意与 `TenantTestCase` 的那个不同。
#: ⚠️ `Domain.domain` 上有唯一约束 ⇒ 撞了 `provision_tenant` 会报错。
#: ⚠️ 它**必须在 `setUpClass` 里建**（schema 的 DDL 不能回滚在 `TestCase` 的事务里），
#: 手法与 `tests/test_openapi_isolation.py` 逐字相同。
TENANT_B_DOMAIN = "openapi-scope-b.test.local"
TENANT_B_SCHEMA = "openapi_scope_b"


def _make_app(*, name, app_key, app_secret, kind):
    """建一行 `ApiApp`（`created_by` 必填 ⇒ 给本用例自己造的那个账号）。

    ⚠️ `kind` 是**必填**参数（⛔ 不给默认值）：本文件的全部判据都绕「哪一类应用」分叉，
    每一行都要自己说清它是哪一类，读起来不会有"这个默认值是哪个"的歧义。
    """
    creator, _ = User.objects.get_or_create(
        username="openapi-scope-probe", defaults={"password": "unused-in-this-test"}
    )
    return ApiApp.objects.create(
        name=name,
        app_key=app_key,
        app_secret=app_secret,
        kind=kind,
        created_by=creator,
    )


class _ScopeNetBase(TenantTestCase):
    """共用的装配：两类应用各一把钥匙、一份收好的端点全集、两个发请求的助手。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⭐ 端点在**类级别**收一次：它只依赖路由表（不依赖数据库），而且失败要**早**
        # （收不出来就整类报错，⛔ 不是伪装成某条用例的假绿）。
        (
            cls.UNIVERSE,
            cls.PATH_BY_ENDPOINT,
            cls.VIEW_BY_URL_NAME,
        ) = collect_openapi_endpoints()
        # 第二个租户（见 TENANT_B_DOMAIN 的注释）：跨租户那两条的对照面。
        cls.tenant_b, _ = provision_tenant(
            schema_name=TENANT_B_SCHEMA,
            name="开放平台端点范围租户 B",
            domain=TENANT_B_DOMAIN,
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回本租户：跨域名请求会把连接留在那个租户的 schema 上。
        connection.set_tenant(self.tenant)
        super().setUp()
        # 失败计数 / 兜底闸都落 DB 行（`OpenApiRateBucket`）⇒ 由事务回滚清掉；
        # 这一行是"只加不删"的纪律（别的线的缓存仍可能跨用例残留）。
        cache.clear()
        # ⚠️ `TenantTestCase.domain` 是 `Domain` **实例**（不是字符串）。
        self.domain_name = self.domain.domain
        self.locked_app = _make_app(
            name="探针锁定应用",
            app_key=LOCKED_KEY,
            app_secret=LOCKED_SECRET,
            kind=ApiAppKind.WECOM_GROUP_QR,
        )
        self.general_app = _make_app(
            name="探针通用应用",
            app_key=GENERAL_KEY,
            app_secret=GENERAL_SECRET,
            kind=ApiAppKind.GENERAL,
        )

    # ── 发请求的两个助手（⛔ 不经 `force_authenticate`：那会把被测对象摘掉）──
    def _request(self, *, method, path_, app_key, app_secret, domain=None, body=None, query=None):
        """按真实 URLconf + 真实中间件发一条带 Bearer 令牌的请求。

        ⚠️ 令牌**当场签发**（`tokens.issue`）——⛔ 不抄签发 / 编码逻辑（否则就是
        "用被测者证明被测者"）。⚠️ `finally` 把连接钉回本租户（跨域名请求之后必须做）。
        """
        token = tokens.issue(app_key, app_secret)
        url = path_ + (("?" + query) if query else "")
        # ⚠️ 请求体一律给**合法 JSON**（`{}`），⛔ 不给空串：对外写端点的入口是 DRF 的
        # 解析器，空体在**带** Content-Type 时会被判成 400（不带的 GET 才无所谓）
        # ⇒ 空体让"这条端点的答复"变成参数问题、把本网的读数搅浑。
        # 本网测的是**授权层**，所以统一送一个语法合法、业务上必然不完整的小体。
        body = {} if body is None else body
        try:
            return Client(SERVER_NAME=domain or self.domain_name).generic(
                method.upper(),
                url,
                data=json.dumps(body),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _hit(self, endpoint, *, locked=True, domain=None, body=None, query=None):
        """打一条 `(方法, url_name)`（路径从全集表里取，⛔ 不手抄 URL）。"""
        method, _url_name = endpoint
        app_key, app_secret = (
            (LOCKED_KEY, LOCKED_SECRET) if locked else (GENERAL_KEY, GENERAL_SECRET)
        )
        return self._request(
            method=method,
            path_=self.PATH_BY_ENDPOINT[endpoint],
            app_key=app_key,
            app_secret=app_secret,
            domain=domain,
            body=body,
            query=query,
        )

    # ── 断言的两个出口 ──
    def _payload(self, response):
        return json.loads(response.content)

    def _assert_allowed(self, response, endpoint, note=""):
        """「放行」= **不是**越权那一档（HTTP 与业务码都不是 403）。

        ⚠️ 读的是**信封里的 `code`**，⛔ 不是 `response.data is not None`（E-0063：
        `api_response` 永远返回信封 dict ⇒ 那个判据恒真）。
        """
        payload = self._payload(response)
        self.assertNotEqual(
            response.status_code, FORBIDDEN, f"{endpoint} 不该被拒（HTTP）：{note} {payload}"
        )
        self.assertNotEqual(
            payload.get("code"), FORBIDDEN, f"{endpoint} 不该被拒（业务码）：{note} {payload}"
        )
        return payload

    def _assert_forbidden(self, response, endpoint, note=""):
        """「拒绝」= 三件事一起：HTTP 码 / 业务码 / **逐字**那句话（D5 的语义，形状照本仓既有 403）。"""
        payload = self._payload(response)
        self.assertEqual(
            response.status_code,
            FORBIDDEN_HTTP,
            f"{endpoint} 越权形状的 HTTP 码不对：{note} {payload}",
        )
        self.assertEqual(payload["code"], FORBIDDEN, f"{endpoint} 业务码不对：{note}")
        self.assertEqual(payload["msg"], FORBIDDEN_MSG, f"{endpoint} 那句话不对：{note}")
        self.assertIsNone(payload["data"], f"{endpoint} 失败信封的 data 应为 null：{note}")
        return payload

    # ── 路由装配的读数（"这条路由装了什么认证类"那两条判据用）──
    def _authentication_classes_of(self, url_name):
        """真 urlconf 里那条路由的视图**实际装配**的认证类（元组）。"""
        view = self.VIEW_BY_URL_NAME[url_name]
        return tuple(getattr(view, "cls", view).authentication_classes)


class EndpointUniverseTests(_ScopeNetBase):
    """A 组前半：这张网**自己**的判据（收出来的集合是全集、且恰好等于该相等的那些）。"""

    def test_the_universe_is_big_enough_to_be_a_real_net(self):
        """收出来的条数有**下界**（这条网要是恒收空集，下面每条断言都会"全绿"）。"""
        self.assertGreaterEqual(
            len(self.UNIVERSE),
            MIN_UNIVERSE_SIZE,
            f"收出来的端点太少，网是空的：{sorted(self.UNIVERSE)}",
        )

    def test_every_collected_endpoint_resolves_to_a_real_route(self):
        """构造物自证：每条 `(方法, url_name)` 的路径**真的能解析回同一个 `url_name`**。

        ⚠️ 没有这一条，转换器填错一个字符就会让一批路由统统 404，而"拒绝"那一半
        （断言那个 403 信封）会**当场红** —— 那是好的失败；坏的是反过来：
        要是断言写成 `assertNotEqual(403)` 就会**静默全绿**。所以这里先证明路径是对的。
        """
        resolver = get_resolver()
        for endpoint, url in sorted(self.PATH_BY_ENDPOINT.items()):
            with self.subTest(endpoint=endpoint, url=url):
                # ⚠️ 用 `get_resolver().resolve()`（⛔ 不是模块级的 `django.urls.resolve`）：
                # 后者读的是**当时的 `ROOT_URLCONF`**，而本文件会在 D 组临时把它换掉 ——
                # 那种情况下模块级 `resolve` 会去打探针 urlconf，报一堆与被测对象无关的 404。
                # ⚠️ `URLResolver.resolve()` 吃的是**含前导斜杠的完整 path**（根的 pattern 是
                # `^/`；实测：去掉斜杠一律 `Resolver404`）—— 两种写法都试过，别再来回改。
                self.assertEqual(resolver.resolve(url).url_name, endpoint[1])

    def test_collection_covers_all_four_multi_method_groups(self):
        """⭐ D4 那四组「同名挂多方法」**逐组**都在全集里（漏一组 ⇒ 那组没被判据覆盖）。"""
        for url_name, allowed_method, rejected_method in METHOD_PAIRS:
            self.assertIn((allowed_method, url_name), self.UNIVERSE, url_name)
            if rejected_method is not None:
                self.assertIn((rejected_method, url_name), self.UNIVERSE, url_name)

    def test_the_universe_is_exactly_the_whitelist_plus_the_ungated_ten(self):
        """⭐⭐ **两向断言 ①**：全集 `==` 「**被拒的 ∩ 过认证层**」∪ 白名单 ∪ 过不了认证层的那些。

        ⛔ 不是 `issubset` / `toContain` / 抽几条写死：新增一条对外端点会让**左边变大**
        ⇒ 这条当场红（然后它要么进白名单、要么被证明"根本没装 Bearer 认证"）。
        ⚠️ 三块**互不相交**（下面那条用例钉住这一点）⇒ 这个等式把"哪些端点该被拒、
        哪些该放行、哪些压根到不了这道闸"三件事一次性钉死。
        """
        self.assertEqual(
            self.UNIVERSE,
            REFUSED_UNDER_THE_GATE | SPEC_D4_ENDPOINTS | BEARER_GATED_EXEMPT_ENDPOINTS,
            "对外端点全集与「被拒的 + 白名单六条 + 未过认证层的那些」对不上",
        )

    def test_the_three_blocks_are_pairwise_disjoint(self):
        """三块**两两不相交**（重叠 ⇒ 上面那个等式会因为"并集缩水"而假绿）。"""
        blocks = {
            "白名单": WECOM_GROUP_QR_ALLOWED_ENDPOINTS,
            "被拒的": REFUSED_UNDER_THE_GATE,
            "过不了认证层的": BEARER_GATED_EXEMPT_ENDPOINTS,
        }
        for left, right in (("白名单", "被拒的"), ("白名单", "过不了认证层的"),
                            ("被拒的", "过不了认证层的")):
            with self.subTest(left=left, right=right):
                self.assertFalse(blocks[left] & blocks[right], f"{left} 与 {right} 重叠")

    def test_the_ungated_set_is_not_empty_or_swallowing_the_whitelist(self):
        """豁免那一档的两个形状判据（⛔ 防它变成"凡对不上就放过"的后门）。

        ① 它**不与白名单重叠**（重叠 ⇒ 有人在用豁免掩盖一条本该走白名单的端点）；
        ② 它**非空**（空集会让下面那条对拍恒真、等于没测，见 E-0065 的"结构性网恒真"一族）。
        """
        self.assertFalse(
            BEARER_GATED_EXEMPT_ENDPOINTS & WECOM_GROUP_QR_ALLOWED_ENDPOINTS,
            "豁免与白名单重叠：豁免被用来掩盖一条本该走白名单的端点",
        )
        self.assertTrue(BEARER_GATED_EXEMPT_ENDPOINTS)
        self.assertTrue(ADMIN_ENDPOINTS, "管理端点那一档不该是空的（它是本表的主体）")

    def test_the_bearer_gated_set_is_exactly_the_six_plus_the_eight(self):
        """⭐⭐ **拓扑对拍**：真 urlconf 里「装没装 Bearer 认证」的读数**逐条**等于本表。

        这是上面那条豁免名单的**唯一**依据 —— 它按每条路由的
        `authentication_classes`（Django 自己的装配事实）算出"哪些端点的请求**根本到不了**
        本线这道闸"，再与写死在文件里的那张表对拍。⛔ 不是"我们不想管它们"：
        谁把其中任一条改成装 Bearer 认证，这张表立刻与读数对不上、这条当场红。

        ⚠️ 同时它也是本网"拒绝"那一半的**前件**证明：下面那些 403 只在**装了** Bearer 的
        端点上才有意义（没装的会以别的理由拒，见
        `test_the_admin_endpoints_are_refused_by_the_session_layer`）。
        """
        gated = frozenset(
            endpoint
            for endpoint in self.UNIVERSE
            if ApiAppBearerAuthentication in self._authentication_classes_of(endpoint[1])
        )
        self.assertEqual(
            self.UNIVERSE - gated,
            BEARER_GATED_EXEMPT_ENDPOINTS,
            "「没装 Bearer 认证」的端点集合与文件里那张表对不上",
        )
        self.assertTrue(
            WECOM_GROUP_QR_ALLOWED_ENDPOINTS <= gated,
            "白名单里有端点没装 Bearer 认证 ⇒ 本文件对它的正面判据不成立",
        )
        # 下界（防这张网被"收成空集"式的改动骗过）
        self.assertGreaterEqual(len(gated - WECOM_GROUP_QR_ALLOWED_ENDPOINTS), 8)

    def test_the_whitelist_constant_matches_spec_d4_word_for_word(self):
        """实现里的那张表与本文件**再写一遍**的 D4 六条逐条相同（钉值不钉常量）。"""
        self.assertEqual(WECOM_GROUP_QR_ALLOWED_ENDPOINTS, SPEC_D4_ENDPOINTS)

    def test_the_forbidden_message_constant_matches_the_literal(self):
        """那句话的常量与上面写死的字面量相同（防"改常量 + 改实现"一起漂）。"""
        self.assertEqual(WECOM_GROUP_QR_ENDPOINT_NOT_ALLOWED_MSG, FORBIDDEN_MSG)

    def test_the_admin_endpoints_are_refused_by_the_session_layer(self):
        """⭐ 如实钉住代价：锁定应用的令牌打那**十一条管理端点** ⇒ 会话层回 **401**。

        ## 这不是缺陷，是本仓装配的拓扑（读码 + 实测两路都核过）

        `apps/` 与 `wecom-qr-app/` 那八条走 `ApiAppAdminView`，它**刻意不声明**
        `authentication_classes`（D13：管理页要能调通）⇒ 吃全局默认，而全局默认
        （`config/settings.py` 的 `DEFAULT_AUTHENTICATION_CLASSES`）**只有**
        `TenantBoundAuthentication`（会话）。⇒ 一条 `Authorization: Bearer …` 在那些端点上
        **没有任何认证类认领**，认证层那段代码**根本没跑** ⇒ 答复是 DRF 的
        `NotAuthenticated`（**401** + 那句「身份认证信息未提供。」）。

        ⚠️ **为什么"拒在 401"这件事可以接受**（而不是把它当成漏掉了 403）：安全结论
        「锁定应用进不了管理端点」**成立**，而且比 403 更靠前（连认证都没过）。
        ⛔ 本任务**不许**去改 `api/app_views.py` 的装配把它变成 403 —— 那会在管理页上多
        装一个只会失败的认证类，且那份文件本线有别的任务在改。
        ⚠️ 这条用例存在的意义是**把这件事写在判据里**（而不是让它成为一条没人知道的边界）：
        哪天有人给管理端点装上 Bearer 认证，本条会红，而上面那条拓扑对拍也会同时红。
        """
        for endpoint in sorted(ADMIN_ENDPOINTS):
            with self.subTest(endpoint=endpoint):
                response = self._hit(endpoint)
                payload = self._payload(response)
                # ⛔ 不是 403（本线那档）；也不是 200/201（那就是**真的**进去了）
                self.assertEqual(
                    response.status_code,
                    UNAUTHORIZED,
                    f"{endpoint} 的答复与实测的会话层口径不符：{payload}",
                )
                self.assertNotEqual(payload.get("code"), FORBIDDEN, f"{endpoint}：{payload}")


class LockedAppDeniedByDefaultTests(_ScopeNetBase):
    """A 组后半 + B 组：锁定应用打**白名单外、过认证层**的每一条 ⇒ 那个 403 信封。"""

    def test_every_non_whitelisted_endpoint_is_refused_for_the_locked_app(self):
        """⭐⭐ **两向断言 ②**：白名单**之外**、且**过 Bearer 认证层**的每一条都要被拒。

        ⚠️ 这条是"deny-by-default"的**唯一**判据：它不列端点，而是拿全集减去白名单
        （再减去那十二条**根本没装** Bearer 认证的，见 `EndpointUniverseTests` 的拓扑对拍）
        ⇒ 明天新增一条对外端点，它会**自动**进来并要求 403。
        """
        self.assertGreaterEqual(
            len(REFUSED_UNDER_THE_GATE),
            MIN_REFUSED_UNDER_THE_GATE,
            f"被拒那一块只剩这么几条：{sorted(REFUSED_UNDER_THE_GATE)}",
        )
        for endpoint in sorted(REFUSED_UNDER_THE_GATE):
            with self.subTest(endpoint=endpoint):
                self._assert_forbidden(self._hit(endpoint), endpoint)

    def test_the_four_multi_method_groups_each_have_a_counterexample(self):
        """⭐ B 组：D4 点名的那四组**逐组**有反例（只按 `url_name` 放行会在这些行上红）。"""
        for url_name, allowed_method, rejected_method in METHOD_PAIRS:
            with self.subTest(url_name=url_name, method=allowed_method, leg="放行"):
                self._assert_allowed(
                    self._hit((allowed_method, url_name)), (allowed_method, url_name)
                )
            if rejected_method is None:
                # 第四组：`open-order-images` 的 `POST`（认领）**也**放行。
                with self.subTest(url_name=url_name, method="POST", leg="放行（认领）"):
                    self._assert_allowed(
                        self._hit(("POST", url_name)), ("POST", url_name), note="认领那一半"
                    )
                continue
            with self.subTest(url_name=url_name, method=rejected_method, leg="必须被拒"):
                self._assert_forbidden(
                    self._hit((rejected_method, url_name)), (rejected_method, url_name)
                )

    def test_the_whitelist_six_are_allowed(self):
        """A 组前半的正面腿：白名单**六条**逐条都不是 403（取票那条带 `img_type=1`）。"""
        bodies = {
            ("POST", "open-order-images"): {"key": "probe/not-a-real-object.png"},
            ("POST", "open-order-image-ticket"): {"ext": "png", "img_type": 1},
        }
        for endpoint in sorted(self.UNIVERSE & WECOM_GROUP_QR_ALLOWED_ENDPOINTS):
            with self.subTest(endpoint=endpoint):
                self._assert_allowed(self._hit(endpoint, body=bodies.get(endpoint)), endpoint)

    def test_the_list_endpoint_accepts_the_incremental_query(self):
        """`GET /v1/orders/?updated_since=…`（本线新增的用法）**放行**。

        ⚠️ 它是白名单第一条**真正被用到**的那个形状 ⇒ 单独钉一次带查询串的调用。
        参数本身的行为判据在别的网（这里只钉"这条路不带 403"）。
        """
        endpoint = ("GET", "open-order-query")
        response = self._hit(
            endpoint,
            query="updated_since=2026-10-09T10%3A00%3A00%2B08%3A00&page=1&page_size=100",
        )
        self._assert_allowed(response, endpoint, note="带 updated_since")

    def test_the_method_scoping_is_not_a_blanket_url_name_allow(self):
        """⭐ 判据函数的**单元**判据：同名不同方法**逐条**分得开（D3 的"按方法收窄"）。

        这条与上面两条走的是不同的层：上面是 HTTP 级的整链判据，这里是**判据函数本身**的
        口径（`is_endpoint_allowed`）—— 它保证"看方法"这件事**在函数里**成立，
        而不是靠某条视图恰好没实现那个方法（那种覆盖会随视图改动漂）。
        """
        for url_name, allowed_method, rejected_method in METHOD_PAIRS:
            self.assertTrue(
                is_endpoint_allowed(method=allowed_method, url_name=url_name), url_name
            )
            if rejected_method is not None:
                self.assertFalse(
                    is_endpoint_allowed(method=rejected_method, url_name=url_name),
                    f"{url_name} 的 {rejected_method} 不该被放行",
                )
        # 白名单**外**的 url_name：两个方法都不放行
        self.assertFalse(is_endpoint_allowed(method="GET", url_name="open-app-list-create"))
        self.assertFalse(is_endpoint_allowed(method="POST", url_name="open-app-list-create"))
        # `url_name` 为空 / None ⇒ 拒（D3；HTTP 级的那两条在 D 组）
        self.assertFalse(is_endpoint_allowed(method="GET", url_name=""))
        self.assertFalse(is_endpoint_allowed(method="GET", url_name=None))


class GeneralAppUnaffectedTests(_ScopeNetBase):
    """C 组：**通用应用**打同一批端点 ⇒ **一条都不 403**（⛔ 本线的收窄不许碰到既有应用）。

    ⚠️ 这一组不是"附带的对拍"，它是**D8 / §1.2 非目标**的判据：本线的授权收窄**只针对
    锁定应用**。没有它，"锁定应用被拒"这件事可能由「这条闸对谁都生效」凑出来。
    """

    def test_the_general_app_is_not_refused_anywhere(self):
        """逐条打**全集**（含白名单外那些）⇒ 没有一条回 403。

        ⚠️ 这里**不**断言"每条都 200 + code 0"：管理端点上会话认证会回 **401**
        （通用应用的**令牌**打不过会话认证 —— 那是既有口径，与本次收窄无关）、
        取令牌端点回 OAuth2 体（没有 `code`）—— 三种都是**合法答复**；
        本组要证明的只有"**没有任何一条**是本线那道闸拒的"。
        """
        for endpoint in sorted(self.UNIVERSE):
            with self.subTest(endpoint=endpoint):
                response = self._hit(endpoint, locked=False)
                payload = self._payload(response)
                self.assertNotEqual(
                    response.status_code, FORBIDDEN, f"{endpoint} 通用应用被 403 了：{payload}"
                )
                self.assertNotEqual(
                    payload.get("code"),
                    FORBIDDEN,
                    f"{endpoint} 通用应用被业务码 403 拒了：{payload}",
                )

    def test_the_general_app_still_reaches_the_order_detail_endpoint(self):
        """正向对照：通用应用打**白名单里的**详情端点 ⇒ 真的跑到了视图（不是被别的闸挡下）。

        ⚠️ 判据是**信封的 `code`**（`1002` 查不到），⛔ 不是 `response.data is not None`（E-0063）。
        """
        endpoint = ("GET", "open-order-detail")
        payload = self._payload(self._hit(endpoint, locked=False))
        self.assertEqual(payload["code"], 1002, payload)

    def test_the_general_app_is_not_refused_on_a_non_whitelisted_endpoint(self):
        """⭐ 与 A 组**成对**的那条：同一条白名单外端点，锁定应用 403 / 通用应用**不是** 403。

        单独拎出来（而不是只靠上面那个循环）是因为它是本文件里**唯一**一处"同一个请求、
        只换应用类别"的**直接对照** —— 两个读数在同一条用例里并排出现。
        端点选 `PUT open-order-assign-price`（改发单价）：它在白名单外、**且**装了 Bearer 认证
        （所以它真的是被本线那道闸拒的，不是被别的层拒的）。
        """
        endpoint = ("PUT", "open-order-assign-price")  # 改发单价：白名单外
        self.assertIn(endpoint, self.UNIVERSE, "这条端点不在全集里，对照组不成立")
        self.assertNotIn(endpoint, BEARER_GATED_EXEMPT_ENDPOINTS, "这条端点没装认证类，对照不成立")
        self._assert_forbidden(self._hit(endpoint), endpoint, note="锁定应用")
        self._assert_allowed(self._hit(endpoint, locked=False), endpoint, note="通用应用（对照）")


class FailClosedBoundariesTests(_ScopeNetBase):
    """D 组：`url_name` 为空 / `resolver_match` 缺失 ⇒ 拒；**通用应用在同一情形下不受影响**。"""

    def test_a_route_without_a_url_name_is_refused_for_the_locked_app(self):
        """路由**没有 `name=`** ⇒ `url_name` 为空 ⇒ 拒（D3：fail-closed）。

        ⚠️ 探针路由住在一个**临时** urlconf 里（`override_settings(ROOT_URLCONF=…)`），
        它不在真 urlconf 里 —— 所以不会撞上按 `get_resolver()` 枚举端点的承重网。
        """
        with override_settings(ROOT_URLCONF=PROBE_URLCONF):
            response = self._request(
                method="GET",
                path_=PROBE_UNNAMED_PATH,
                app_key=LOCKED_KEY,
                app_secret=LOCKED_SECRET,
            )
            self._assert_forbidden(response, ("GET", "<无名路由>"), note="无名路由")

    def test_the_named_probe_route_proves_the_probe_urlconf_is_live(self):
        """⭐ 上一条的**自证**（探针自身没接上，那种失败看起来跟被测对象坏了一模一样）。

        同一份临时 urlconf 里那条**有名字**的路由：① 锁定应用打它 ⇒ **拒**
        （它的名字**不在白名单里** ⇒ 这条同时证明"拒"这个读数源自**判据**、
        而不是"临时 urlconf 整个没生效 / 请求没打到视图"）；② 通用应用打它 ⇒ **放行**，
        且视图自己回显的 `url_name` **逐字**等于路由上的那个名字（证明它真的走到了视图）。
        """
        with override_settings(ROOT_URLCONF=PROBE_URLCONF):
            locked = self._request(
                method="GET",
                path_=PROBE_NAMED_PATH,
                app_key=LOCKED_KEY,
                app_secret=LOCKED_SECRET,
            )
            self._assert_forbidden(locked, ("GET", "open-scope-probe-named"), note="探针路由")

            general = self._request(
                method="GET",
                path_=PROBE_NAMED_PATH,
                app_key=GENERAL_KEY,
                app_secret=GENERAL_SECRET,
            )
            payload = self._payload(general)
            self.assertEqual(general.status_code, 200, payload)
            self.assertEqual(
                payload["url_name"],
                "open-scope-probe-named",
                "视图自己看到的 url_name 不对 ⇒ 这条探针的读数不能用（证据自证）",
            )

    def test_the_general_app_is_unaffected_when_the_route_has_no_url_name(self):
        """⭐ 对照：**通用应用**打同一条无名路由 ⇒ 照常跑到视图（`url_name` 回 `None`）。"""
        with override_settings(ROOT_URLCONF=PROBE_URLCONF):
            response = self._request(
                method="GET",
                path_=PROBE_UNNAMED_PATH,
                app_key=GENERAL_KEY,
                app_secret=GENERAL_SECRET,
            )
            payload = self._payload(response)
            self.assertEqual(response.status_code, 200, payload)
            self.assertIsNone(payload["url_name"], payload)
            self.assertEqual(payload["app_key"], GENERAL_KEY, payload)

    def test_a_missing_resolver_match_is_refused_for_the_locked_app(self):
        """`resolver_match` **取不到**（`None`）⇒ 拒（fail-closed）。

        ## ⚠️ 实测的框架事实（⛔ 别再照着"属性不存在"写判据）

        `HttpRequest.__init__` **总是**写 `self.resolver_match = None`
        （`django/http/request.py:75`），真正给它赋值的只有 Django 的请求处理器
        （`django/core/handlers/base.py:314`，紧跟在那次 `resolve()` 之后）。
        ⇒ "取不到"这条分支的**真实形状是属性值 `None`**，⛔ 不是 `hasattr` 为假
        （本任务第一版就写成了后者，当场红了 —— 那条自证是对的，别把它删掉）。

        ## 怎么造成"`None`"

        用 `APIRequestFactory` 把请求**直接**打进视图：请求是**真** `HttpRequest`，
        但**不经 URLconf**（处理器那一跳没跑）⇒ `resolver_match` 停在 `None`。
        这不是人造的边角：它正是"有人绕过路由表直接调视图"的那个形状。
        ⚠️ 被测视图是**真端点**（从真 urlconf 上取回 `open-order-query` 的视图）
        —— ⛔ 不是本文件自造的假视图（那样测的是假视图的装配）。
        """
        view = self._resolve_endpoint_view("open-order-query")
        request = APIRequestFactory().get(PROBE_NAMED_PATH, REMOTE_ADDR=SOURCE_IP)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {tokens.issue(LOCKED_KEY, LOCKED_SECRET)}"
        self.assertIsNone(
            request.resolver_match,
            "构造物自证失败：这个请求居然带着 resolver_match ⇒ 下面的判据测不到东西",
        )
        response = self._call_view_directly(view, request)
        payload = self._payload(response)
        self.assertEqual(response.status_code, FORBIDDEN_HTTP, payload)
        self.assertEqual(payload["code"], FORBIDDEN, payload)
        self.assertEqual(payload["msg"], FORBIDDEN_MSG, payload)

    def test_the_general_app_is_unaffected_when_the_resolver_match_is_missing(self):
        """⭐ 对照：同一情形下**通用应用**照常跑进视图（证明这条闸只认锁定应用）。"""
        view = self._resolve_endpoint_view("open-order-query")
        request = APIRequestFactory().get(PROBE_NAMED_PATH, REMOTE_ADDR=SOURCE_IP)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {tokens.issue(GENERAL_KEY, GENERAL_SECRET)}"
        response = self._call_view_directly(view, request)
        payload = self._payload(response)
        self.assertEqual(response.status_code, 200, payload)
        # 走到视图了 ⇒ 拿到的是那条端点**自己的**答复（`1000` 缺参数），⛔ 不是本线那道 403。
        # ⚠️ 判据钉的是 ENVELOPE 的码与那句文案（写数字、写字面量）——`route` 通不通这件事
        # 由"码是 1000 而不是 403"直接承担，⛔ 别改成 `response.data is not None`（E-0063 恒真）。
        self.assertEqual(payload["code"], 1000, payload)
        self.assertEqual(payload["msg"], "缺少参数：source_order_no 或 updated_since", payload)

    def _call_view_directly(self, view, request):
        """把请求**直接**打进视图并渲染出响应（不经中间件、不经 URLconf）。

        ⚠️ 必须渲染：`APIRequestFactory` **不走中间件**，DRF 的 `Response` 平时是在请求周期的
        `finalize_response` 里补上「渲染三件套」再 `render()` 的 ⇒ 不补的话读
        `response.content` 会抛 `ContentNotRenderedError`。
        这里用**项目自己的** `rendered_response`（`apps/core/envelope.py`），⛔ 不手搓三件套：
        那份三件套决定响应的**字节形状**（渲染器 / 媒体类型），手抄一份就会与线上不一致。
        """
        from apps.core.envelope import rendered_response

        return rendered_response(view(request))

    def _resolve_endpoint_view(self, url_name):
        """从真 urlconf 上取回某条 `url_name` 的视图（`as_view()` 的结果）。

        ⚠️ 用 `reverse()` + `resolve()` 走**Django 自己**的解析（⛔ 不按全集的路径表手拼：
        那样测的是本文件的路径拼接，而不是真路由）。
        """
        # ⚠️ 先按**不带参数**反查（`open-order-query` 这条没有命名参数），不行再带 pk
        # （`apps/<int:pk>/…` 那几条要）—— ⛔ 不是手抄"哪条要哪个参数"。
        try:
            return resolve(reverse(url_name)).func
        except NoReverseMatch:
            return resolve(reverse(url_name, kwargs={"pk": PROBE_PK})).func


class CrossTenantCredentialTests(_ScopeNetBase):
    """D 组最后一条：锁定应用打**别人的租户域名** ⇒ 仍是 **401**（既有口径不变）。

    ⚠️ **必须用一个真实存在的第二个租户**（`TENANT_B_DOMAIN`，在 `setUpClass` 里建）：
    打一个**不存在**的域名时租户中间件回的是 **404**（HTML，实测）——那不是本条的判据
    （既不是 401 也不是 403，读数不指向任何东西）。只有域名**解析得到**一个真租户、
    而那个租户的 schema 里**没有**这把 appKey 时，才真的在测"凭据是跨租户的"。
    """

    def test_the_locked_app_hits_401_on_a_foreign_tenant_domain(self):
        """租户 A 的锁定应用 + 租户 B 的域名 ⇒ **401**（B 里没有这个 appKey）。

        ⚠️ 判据同时钉住"它**不是**本线那道 403"：这条新闸只在**认出了应用之后**才生效
        （第七步之后），跨租户的请求**根本走不到**那里 —— 既有口径一个字没变。
        """
        endpoint = ("GET", "open-order-detail")
        response = self._hit(endpoint, domain=TENANT_B_DOMAIN)
        self.assertEqual(
            response.status_code,
            UNAUTHORIZED,
            "锁定应用打别人的域名应当是 401（凭据问题），⛔ 不是本线那道 403",
        )
        payload = self._payload(response)
        self.assertEqual(payload["code"], UNAUTHORIZED, payload)
        self.assertNotEqual(payload.get("msg"), FORBIDDEN_MSG, payload)

    def test_the_same_request_on_its_own_domain_is_not_401(self):
        """⚠️ 对照：同一个应用、同一条端点，打**自己**的域名 ⇒ **不是** 401。

        没有它，上面那条 401 可能只是"这把凭据本来就坏 / 这条路对谁都不生效"。
        """
        endpoint = ("GET", "open-order-detail")
        response = self._hit(endpoint)
        self.assertNotEqual(response.status_code, UNAUTHORIZED, self._payload(response))

    def test_the_second_tenant_really_exists(self):
        """构造物自证：第二个租户**真的**建起来了（否则上面那条 401 是 404 冒充的）。"""
        from django_tenants.utils import get_tenant_model

        self.assertTrue(
            get_tenant_model().objects.filter(schema_name=TENANT_B_SCHEMA).exists(),
            "第二个租户没建起来 ⇒ 跨租户那条用例的读数不能用",
        )
