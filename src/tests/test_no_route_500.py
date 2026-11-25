"""契约测试：`GET <collection>/` 带畸形**查询参数**时，已注册集合都不许 500。C1 的三条 500（PROTECT 删除、`?game=abc`、超长 actor）是同一类缺陷的三个实例：**请求来源的值没有走过校验就进了类型化的列或查询**；逐条修只能修掉已知的三个，这条测试守的是「未校验的搜索参数」这一类——集合那条腿只发 `GET <collection>/`（显式路由腿对只收 POST 的字面路由发畸形**请求体**），`DELETE` 形状与写路径的 500 覆盖不到：前者见 `tests/test_config_api.py`，后者见 `tests/test_order_api.py`（4 条写路径）。⚠️ **两条腿**（Ruling 40①）：`router.registry` 只认 router 注册过的集合，而 `/api/business/hall/` 是 `urls.py` 里的**显式 `path()`**（刻意不用 `@action`）——只遍历 registry 会让它整个逃出这张网，所以显式路由单独遍历（`_explicit_routes()`：`urlpatterns` 减去 `router.urls`，不手抄 URL）；`/api/audit/logs/` 也没有 router，同样走显式路由那条腿。
⚠️ **畸形参数是「共享语料」**：每个端点只认识其中一部分，其余参数被各自的序列化器**忽略**（不是报错），所以这里可以放各端点自己的畸形值（`log_type` / `stime` 只属于审计日志端点）而不必为每个端点单开一份语料——判据仍是「这一类请求不许 500」。⚠️ **显式路由那条腿按视图允许的方法发请求**：`POST /api/audit/events/`（spec §6.2 要求它进这张网）只收 POST，沿用「一律 GET」会拿到 405 假红；选方法的依据是**框架事实**（`ViewSet.actions` / `APIView.allowed_methods`），不是手抄的「哪些路由是 POST」名单——后者会随新增路由漂移；只在 GET 不可用时才发 POST，`MALFORMED_QUERY` / `MALFORMED_BODY` 各服务一半。
⚠️ **形状那一半有三档例外**（都显式枚举 + 与派生集合对拍，⛔ 都不放宽「不许 500」）：`TOKEN_ONLY_ROUTES`（只认 Bearer 令牌 ⇒ 正确答复是 401）、`VENDOR_SHAPED_ROUTES`（闲管家推送接收端，spec §D8 ⇒ 回第三方形状 `{"result", "msg"}`、没有 `code`）、`OAUTH2_SHAPED_ROUTES`（取令牌端点，v2 设计文档 §5.4 选定的**选项 A** ⇒ 401 + **恰好** `{"error", "error_description"}`、同样没有 `code`）。除这三档外，通用断言仍是「200 + 标准信封」。"""
from cryptography.fernet import Fernet
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from rest_framework.authentication import SessionAuthentication

from apps.core.authentication import PeerTokenAuthentication, TenantBoundAuthentication
from tests.p5_auth_helpers import make_logged_in_client

#: 只认**非会话**凭据、**刻意拒绝会话 cookie** 的显式路由：它们对畸形入参的正确答复是 **401 + 信封**，不是本腿通用的 200。
#: ⚠️ 豁免必须**显式枚举**且与派生集合对拍（见 `test_token_only_exemptions_are_exactly_the_enumerated_ones`），
#: ⛔ 不能放宽成「状态码 ∈ {200, 401}」那种全员后门（`AGENTS.md` §5-G / E-0065）。
TOKEN_ONLY_ROUTES = (
    "/api/business/peer-sync/unbind/",
    # 下家侧接收端点（spec §8.3）同属「只认 Bearer 令牌」这一类
    # （`authentication_classes = [PeerTokenAuthentication]`，会话请求必然 401）。
    "/api/business/peer-sync/inbound/events/",
    "/api/business/peer-sync/direct-taker/",
    # 上家侧对账端点（spec §16.8）同属这一类（Bearer 令牌，⛔ 不收会话 cookie）。
    "/api/business/peer-sync/reconcile/",
    # ⭐ 上家侧**大厅全量拉取**端点（计划 `2026-10-25-peer-hall-pull` §1）：
    # `authentication_classes = [PeerTokenAuthentication]` ⇒ 会话请求必然 401。
    "/api/business/peer-sync/hall/list/",
    # ⭐ 开放平台对外查单端点（spec §7.1 / §7.2）：它**只认对外凭据**、会话请求必然 401；今天装的是 `ApiAppBearerAuthentication`（对外**令牌**；旧 HMAC 签名认证已随 v1 面整体删除，§13.1）。
    # ⚠️ **本表一个字未改**：它登记的是"这条路径不吃会话"，与"装的是哪一种对外认证类"无关（下面 `_is_token_only` 就是按"认不认会话"推导的）。
    # ⚠️ **只有列表这一条进得来**：详情那条是 `v1/orders/<order_no:order_no>/`，而 `_explicit_route_entries` 会跳过**带路径转换器**的路由（它自己登记的已知盲区：转换器位没法直接拼成可发的 URL）⇒ 派生集合里没有它，硬写进来会让下面那条「恰好对拍」的断言红；详情路由的 401 判据在 `tests/test_openapi_order_api.py` 里用**HTTP 级**断言补（⛔ 不是不管）。
    "/api/open/v1/orders/",
    # ⭐ 开放平台 v2 的**只读扩展端点**（v2 设计文档 §6.1 / §13.10）：`authentication_classes = [ApiAppBearerAuthentication]` ⇒ 同样只认令牌、会话请求必然 401。
    # ⚠️ **只有不带路径转换器的那四条进得来**（`v1/ranks/` / `v1/bills/` / `v1/games/` / `v1/projects/`）：`v1/orders/<order_no:order_no>/status/` 与 `.../images/` 带转换器 ⇒ 被 `_explicit_route_entries` 跳过（它自己登记的已知盲区），硬写进来会让下面那条「恰好对拍」的断言红；
    # 那两条的 401 与畸形入参判据由 `tests/test_openapi_order_extra.py` 用 **HTTP 级**断言补（⛔ 不是不管）。
    "/api/open/v1/ranks/",
    "/api/open/v1/bills/",
    "/api/open/v1/games/",
    "/api/open/v1/projects/",
    "/api/integration/v1/changes/",
    "/api/integration/v1/orders/",
    "/api/integration/v1/orders/recycle-bin/",
    "/api/integration/v1/orders/assignees/",
    "/api/integration/v1/takers/",
    "/api/integration/v1/taker-token/",
    "/api/integration/v1/taker/hall/",
    "/api/integration/v1/taker/my/orders/",
    "/api/integration/v1/catalog/games/",
    "/api/integration/v1/catalog/projects/",
    "/api/integration/v1/catalog/servers/",
    "/api/integration/v1/catalog/order-sources/",
    # ⭐ 打手侧对外端点（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5）：**拉大厅**那条——`authentication_classes = [TakerAppBearerAuthentication]` ⇒ 同样只认令牌、会话请求必然 401。
    # ⚠️ 本行是**本网先报出来**的（执行期实测两条报文：`test_every_explicit_route_survives_malformed_input` 报 `'/api/taker/v1/hall/' not found in (…)`、`test_token_only_exemptions_are_exactly_the_enumerated_ones` 报「Items in the first set but not the second」），⛔ 不是按"应该要登记"推的。
    # ⚠️ **只有大厅这一条进得来**：抢单那条是 `v1/hall/<order_no:order_no>/claim/`，而 `_explicit_route_entries` 会跳过**带路径转换器**的路由（它自己登记的已知盲区）⇒ 派生集合里没有它，硬写进来会让下面那条「恰好对拍」的断言红；那条的畸形入参判据由 `tests/test_takerapi_claim_api.py` 用 **HTTP 级**断言补（⛔ 不是不管），并同一次进 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」。
    "/api/taker/v1/hall/",
    # ⭐ 打手侧对外端点**第二批**的**我的单**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 3 行）：`authentication_classes = [TakerAppBearerAuthentication]` ⇒ 同样只认令牌、会话请求必然 401。
    # ⚠️ 本行是**本网先报出来**的（执行期实测两条报文：`test_every_explicit_route_survives_malformed_input` 报 `'/api/taker/v1/my/orders/' not found in (…)`、`test_token_only_exemptions_are_exactly_the_enumerated_ones` 报「Items in the first set but not the second」），⛔ 不是按"应该要登记"推的。
    # ⚠️ **这一批只有这一条进得来**：查单 / 改状态 / 进度图三条的**五条**路由全部是 `v1/orders/<order_no:order_no>/…`（带路径转换器）⇒ 被 `_explicit_route_entries` 跳过（它自己登记的已知盲区），硬写进来会让下面那条「恰好对拍」的断言红；那五条的畸形入参判据由 `tests/test_takerapi_my_orders_api.py` 与 `tests/test_takerapi_order_status_api.py` 用 **HTTP 级**断言补（⛔ 不是不管），并同一次进 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」。
    "/api/taker/v1/my/orders/",
)

#: 回**第三方规定的形状**（`{"result": …, "msg": …}`，**没有**本仓信封的 `code`）的显式路由。
#: 它们仍然一律 HTTP 200（畸形入参也是 200 + `result: "fail"`），判据只是从「有 `code` 键」换成「键集恰好是 `{"result", "msg"}`」——⛔ 没有放宽「不许 500」那一条。豁免必须**显式枚举**且与派生集合对拍（见 `test_vendor_shaped_exemptions_are_exactly_the_enumerated_ones`），
#: ⛔ 不能放宽成「响应里没有 `code` 也算过」那种全员后门（`AGENTS.md` §5-G / E-0065）。
VENDOR_SHAPED_ROUTES = (
    # 闲管家推送接收端（spec `2026-10-04-xgj-push-inbound-design.md` §D8）：
    # 对外契约只有 `result` 一个字段可读（文档：「只有 result 返回 success 才认为成功」），
    # 套本仓统一信封会让厂商**永远判失败**。
    "/api/business/xgj/push/order/",
    "/api/business/xgj/push/product/",
)

#: 回 **OAuth2 标准错误体**的显式路由：`401` ＋ **恰好** `{"error", "error_description"}`——**没有**本仓信封的 `code`（v2 设计文档 §5.4 选定的**选项 A**）：取令牌端点对畸形入参的正确答复就是这个形状（凭据错 ⇒ `invalid_client`；`grant_type` 不对 ⇒ `unsupported_grant_type`）。
#: 它落在既有两类**之外**：`TOKEN_ONLY_ROUTES` 那条腿断言 `"code" in body`（本类的体里没有），而 `VENDOR_SHAPED_ROUTES` 的键集是 `{"result", "msg"}`（也对不上）⇒ 硬塞进任何一类都会红，所以**新增第三个类别**。豁免必须**显式枚举**且与派生集合对拍（见 `test_oauth2_shaped_exemptions_are_exactly_the_enumerated_ones`），⛔ 不能放宽成「没有 `code` 也算过」那种全员后门（`AGENTS.md` §5-G / E-0065）。
#: 回**第三方规定的纯文本**（`success` / `fail`）的显式路由：Jeepay 的异步回调约定「返回字符串 success 表示成功，否则重试」——套信封会让它**永远重试**；这类端点的畸形入参答复是 `400 fail`（验签失败），**这也是正确行为**；判据从「200 + 有 code」换成「内容 ∈ {success, fail} 且状态码 ∈ {200,400,404}」，⛔ 「不许 500」那一条**一个字没放宽**。
TEXT_SHAPED_ROUTES = (
    "/api/app/pay/notify/",
)

OAUTH2_SHAPED_ROUTES = (
    # ⚠️ 字面量**不带尾斜杠**：`apps/openapi/urls.py` 的契约就是 `path("oauth/token", …)`
    # （v2 §5.1 逐字），写成带斜杠的版本在这里会红 —— 那正是这条枚举的作用。
    "/api/open/oauth/token",
    # ⭐ **打手侧**的取令牌端点（spec `docs/specs/2026-10-08-taker-open-api-design.md` §2）。它与上面那一条**同款但各自独立**：同一个共用流程（`apps/openapi/api/token_views.py::token_exchange_response`）+ 各自一份 spec ⇒ 字面与形状逐字相同，而**契约是两条**（⚠️ 所以这里必须是**两条**登记，⛔ 不是"上面那条覆盖了它"）。
    # ⚠️ 同样**不带尾斜杠**：`apps/takerapi/urls.py` 的契约就是 `path("oauth/token", …)`（spec §2 逐字：`/api/taker/oauth/token`），写成带斜杠的版本在这里会红。
    # ⚠️ 这条路由**不带路径转换器** ⇒ 它确实落进本类枚举来源（`EXPLICIT_ROUTE_SOURCES` 里 `("/api/taker/", "apps.takerapi.urls")`）⇒ 漏登这一格会让 `derived - enumerated` 非空、当场红。
    "/api/taker/oauth/token",
)

#: 回**微信 / 浏览器原生形状**的显式路由（⭐ D-0268 公众号线新增的第四类）：`GET /api/wechatmp/callback` 回 echostr / 空串 / 403 纯文本（对端是微信服务器，只认原生文本，套本仓信封它解析不了）；`bind-entry` / `bind-callback` 回 **302 跳转**；`/wechatmp/bind-confirm` 是**服务端渲染 HTML 页**（微信内置浏览器直接看）。
#: 畸形入参的正确答复 = 200 / 302 / 403（见形状腿的新分支），套不上前三类的任何断言（有 `code` / `result,msg` / `error,error_description` 的全都不对）⇒ 新增一类。豁免必须**显式枚举**且与派生集合对拍（见 `test_wechatmp_shaped_exemptions_are_exactly_the_enumerated_ones`），
#: ⛔ 不能放宽成「不是 500 也算过」那种全员后门（`AGENTS.md` §5-G / E-0065）。
WECHATMP_SHAPED_ROUTES = (
    # 微信服务器回调（匿名；路径**不带尾斜杠**——公众平台里填的就是这条逐字路径）。
    "/api/wechatmp/callback",
    # ⭐ 企微服务器回调（D-0379；匿名；路径不带尾斜杠——企微后台里填的就是这条逐字路径）。
    "/api/wecom/callback",
    # OAuth 绑定入口 / 微信回跳（302 跳微信 / 跳确认页；不带尾斜杠，公众平台的菜单 URL 逐字）。
    "/api/wechatmp/bind-entry",
    "/api/wechatmp/bind-callback",
    # 绑定确认页（独立服务端渲染页，**不在 api/ 前缀下**——前缀见 EXPLICIT_ROUTE_SOURCES）。
    "/wechatmp/bind-confirm",
)


class No500OnMalformedQueryParamsTests(TenantTestCase):
    # 畸形但**语法合法**的参数：真实客户端会送出的那种。
    # `log_type` / `stime` 是 `/api/audit/logs/` 独有的——
    # 其余端点会把它们当未知参数忽略，所以放进同一份语料是安全的（见模块 docstring）。
    MALFORMED_QUERY = {
        "game": "abc",
        "server": "abc",
        "status": "BOGUS",
        "date_from": "garbage",
        "date_to": "garbage",
        "is_urgent": "maybe",
        "page": "abc",
        "log_type": "999",
        "stime": "garbage",
    }

    #: 只收 POST 的显式路由要发**畸形请求体**（今天只有 `/api/audit/events/`）。
    #: 与 `MALFORMED_QUERY` 同一口径：语法合法、值畸形，各端点只认识其中一部分，
    #: 其余键被自己的序列化器忽略。
    MALFORMED_BODY = {
        "action": "x" * 200,   # 超长 + 不在白名单
        "order_no": "0" * 500, # 远超 `OperationLog.order_no` 的 20
        "page": "abc",         # 只为证明「不属于本端点的畸形键被忽略、不炸」
        "log_type": "999",
    }

    def setUp(self):
        # P2 的集合端点没有声明权限类 → 受 fail-closed 默认权限类（spec D11）保护。**只加认证，不放宽断言**：下面仍要求 HTTP 200（E-0010 的结构兜底）。
        # ⚠️ **用超级管理员登录**：P2 的集合端点不按职位放行，所以「只认证」够用；但 `/performances/` 要 `my_performance.export` 节点、且还会按数据范围收窄行，普通账号会拿到 403——而这条腿断言的是「**畸形参数不许 500** 且**响应仍是标准信封**」，403 是**另一个关注点**（授权），混进来只会让判据从「没有 500」退化成「200 或 403」。
        # 超级管理员在 `has_node` 里短路为真（`nodes_of` 也短路成全目录）⇒ 任何**按节点放行**的新集合都仍然被真正覆盖，将来再加也不需要改这条腿；判据本身（`!= 500` / `== 200` / 有 `code`）一个字没放宽。
        self.client = make_logged_in_client(is_superuser=True)

    def _collections(self):
        from apps.business.urls import router

        return [prefix for prefix, _viewset, _basename in router.registry]

    #: 显式路由的枚举来源：`(URL 前缀, urls 模块路径)`。模块里挂了 `router` 的，其 `router.urls` 由上面 registry 那条腿覆盖，这里减掉。
    #: 枚举来源本身**按模块给**（不是手抄 URL 清单）——新端点只要进了某个 app 的 `urlpatterns` 就自动落进这张网。
    EXPLICIT_ROUTE_SOURCES = (
        ("/api/business/", "apps.business.urls"),
        ("/api/audit/", "apps.audit.urls"),
        # ⭐ 开放平台对外端点（spec §3.3 的 ⚠️）：**漏这一条不是变红，是整张网静默漏掉本线全部端点**——`_explicit_route_entries` 只从这份清单里的模块枚举，不登记就没人检查它们（而断言本身照样绿）。
        # ⚠️ 已知盲区（实测并登记，⛔ 不改它）：**带路径转换器**的路由会被跳过（`v1/orders/<order_no:order_no>/` 就在里面）；把跳过规则放宽会让一批既有转换器路由一起进通用腿，风险远大于收益 ⇒
        # 详情那条的判据由 `tests/test_openapi_order_api.py` 的 HTTP 级用例单独钉。
        ("/api/open/", "apps.openapi.urls"),
        ("/api/integration/v1/", "apps.openapi.integration_urls"),
        # ⭐ 打手侧开放接口（spec `docs/specs/2026-10-08-taker-open-api-design.md` §8）：⚠️ **在还没有任何端点时就登记**（**有意**，与"声明了就必须有 leaf"那类网不同）：`_explicit_route_entries` 按模块枚举 `urlpatterns`，空表**贡献 0 条路由、不会假绿**；后来新增的**不带路径转换器**的对外路由从第一天起就在这张结构兜底里——漏了它的后果与上面 `api/open/` 那一格**逐字同款**（整张网静默漏掉本线全部端点，而断言本身照样绿）。
        # ⚠️ 上面那条**已知盲区**（带路径转换器的路由被跳过）**逐字适用于本线**：`/api/taker/v1/orders/<order_no>/…` 与 `hall/<order_no>/claim/` 都带共享转换器
        # ⇒ 它们的畸形入参判据用 **HTTP 级**用例单独钉（`tests/test_takerapi_claim_api.py` / `test_takerapi_my_orders_api.py` / `test_takerapi_order_status_api.py`，⛔ 不是不管），并同一次进 `tests/test_p4_no_500.py` 的「共享转换器路由**恰好**集合」。
        ("/api/taker/", "apps.takerapi.urls"),
        # ⭐ 微信公众号集成（D-0268）：API 前缀 + **独立绑定确认页**两份 urlconf（确认页在 `api/` 之外、挂在 `/wechatmp/`）。
        # ⚠️ 与 `test_operation_log_coverage` 的 `COVERAGE_SCOPE` / `NON_ROUTER_MODULES` **同一次**登记——那边的恰好断言漏登就红，这边漏登是**静默漏覆盖**。
        ("/api/wechatmp/", "apps.wechatmp.urls"),
        ("/wechatmp/", "apps.wechatmp.page_urls"),
        # ⭐ QQ 机器人线：设置页端点同一条口径——**漏登不是红，是整张网静默漏掉本线全部端点**。
        # 字面路由（binding / groups / menu / share-link / messages）从第一天起进这张兜底；`groups/<pk>/…` 五条带转换器、被既有的跳过规则跳过（同上面两条的已知盲区），
        # 它们的畸形入参判据由 `tests/test_qqbot_views.py` 用 HTTP 级用例钉（⛔ 不是不管）。
        ("/api/qqbot/", "apps.qqbot.urls"),
        # ⭐ 企微自建应用线（D-0379）：同一条口径——漏登不是红，是整张网静默漏掉本线全部端点。
        ("/api/wecom/", "apps.wecom.urls"),
        # ⭐ 打手 App 线（D-0269 起）：`/api/app/auth/*` 与 `/api/app/wallet/*` 是 `apps.accounts.urls` 里的**显式 path**（不是 router）⇒ 不登记这条源，它们整片逃出畸形入参网（实测：本网的枚举源原先完全不含本 app）。
        # ⚠️ 前缀写 `/api/`（本模块在 `api/` 下被 include），不是 `/api/accounts/`。
        ("/api/", "apps.accounts.urls"),
    )

    def _explicit_route_entries(self):
        """`urls.py` 里**不属于** router 的显式 `path()`——返回 `(URL, 视图函数)`，按模块枚举。`router.registry` 只认 router 注册过的集合前缀，而 `/api/business/hall/` 是显式 `path()`（刻意不用 `@action`，见 `apps/business/urls.py` 的注释）⇒ 它会**整个逃出**这条结构兜底；这里用「`urlpatterns` 减去 `router.urls`」枚举：手抄一条 URL 只能覆盖那一条，下一个显式路由照样逃出去。`/api/audit/logs/` 同属这一类，且 `apps.audit` **没有 router** ⇒ 它的 `urlpatterns` 全是显式路由，一并枚举（第二条来源）。⚠️ **跳过带路径转换器的路由**：`hall/<order_no:order_no>/claim/` 的 `<order_no:order_no>` 是**参数位**，不是字面路径——把 pattern 原样拼成 URL 发出去得到的是 405 / 404，**那是假红**，与「兜底真正要抓的 500」无关；判据取「这条路由有没有转换器」而不是手抄一份忽略名单：名单会随新增路由漂移，而转换器是 Django 自己知道的事实。
        ⚠️ 代价是**覆盖集合变小** ⇒ 必须显式钉住大厅、审计日志与读操作上报还在里面（见各条 `*_is_inside_the_explicit_route_coverage`），否则「跳过规则」会退化成「什么都没覆盖」，那正是这条腿存在的理由（Ruling 46 / 0a）。⚠️ **返回视图函数**：字面路由里有了**只收 POST** 的 `/api/audit/events/`，沿用「一律 GET」会让它 405 假红；调用方按视图**允许的方法**选发 GET 还是 POST（见 `_request_method` / `_allowed_methods`）。
        ⚠️ **两条跳过规则只保留「带转换器」这一条**：另一条「跳过不接受 GET 的路由」（为 `path("settlements/settle", SettleView.as_view())` 而设——**字面路径**却只收 POST，GET 它必然 405、与带参数路由的假红同型）被丢弃：本网**不跳过它、而是给它发 POST** ⇒ 覆盖集合**更大**（它留在网内，且不靠一份「哪些路由不吃 GET」的判据——那份判据会随动作形状变化静默变薄）。仍在网内的字面路由由各自的钉点守住：大厅、`audit/logs/`、`audit/events/` + `notifications/`、`my-orders/`、`settlements`、`settlements/unattributed`、`settlements/people`（D-0089）。"""
        from importlib import import_module

        routes = []
        for prefix, module_path in self.EXPLICIT_ROUTE_SOURCES:
            module = import_module(module_path)
            router = getattr(module, "router", None)
            router_ids = {id(p) for p in router.urls} if router is not None else set()
            for p in module.urlpatterns:
                if id(p) in router_ids:
                    continue
                # `path()` 产出 `RoutePattern`（有 `converters`）；`re_path()` 产出
                # `RegexPattern`（没有这个属性）——后者同样是字面 URL，不该被跳过。
                if getattr(p.pattern, "converters", None):
                    continue
                routes.append((f"{prefix}{p.pattern}", p.callback))
        return routes

    def _explicit_routes(self):
        """上面的 URL 列表（三条前置断言与既有调用点用）。"""
        return [route for route, _callback in self._explicit_route_entries()]

    @staticmethod
    def _allowed_methods(callback):
        """这个视图函数允许哪些 HTTP 方法——取**框架事实**，不手抄名单。
        - `ViewSet.as_view({"get": "hall"})` 产出的函数带 `actions`（`rest_framework/viewsets.py:138`）；
        - 普通 `APIView.as_view()` 产出的函数带 `cls`（`rest_framework/views.py:140`），实例上的 `allowed_methods`（`rest_framework/views.py:152-156`）委托 Django 的 `View._allowed_methods` = `http_method_names` ∩ 已定义的处理方法（`django/views/generic/base.py:180-181`）⇒ 只定义了 `post` 的 APIView 不含 GET。"""
        actions = getattr(callback, "actions", None)
        if actions:
            return {method.upper() for method in actions}
        cls = getattr(callback, "cls", None)
        return set() if cls is None else set(cls().allowed_methods)

    def _request_method(self, callback):
        """按视图允许的方法选一条请求方法：GET 优先，否则 POST。
        `events/` 只收 POST ⇒ 它是这条腿里**唯一**走 POST 的路由；两条腿（集合 / 显式）的判据仍是「不许 500 且响应是标准信封」，一个字没放宽。"""
        methods = self._allowed_methods(callback)
        for candidate in ("GET", "POST"):
            if candidate in methods:
                return candidate
        raise AssertionError(f"显式路由既不吃 GET 也不吃 POST：{sorted(methods)}")

    @staticmethod
    def _is_token_only(callback):
        """这个视图是不是**只认 Bearer 令牌**（刻意拒绝会话，spec §9-S2）。判据取**框架事实**（视图类自己的 `authentication_classes`），**⛔ 不是手抄名单**：「**这个方法里没有任何一个认证类会认会话 cookie**」——即 `authentication_classes` **非空**、且**没有一个是 `SessionAuthentication` 的子类**（本仓唯一认会话的认证类是 `TenantBoundAuthentication`，它就是 `SessionAuthentication` 的子类；`PeerTokenAuthentication` 与开放平台那两个对外认证类都是 `BaseAuthentication` 的直系——开放平台那两个里的**签名**那一个已随 v1 面删除（§13.1），今天只剩 `ApiAppBearerAuthentication`）⇒ 这类视图对**会话请求**必然 401，与它是不是显式路由无关。
        ⚠️ **为什么按"有没有会话认证类"推导，而不是写死「装了 `PeerTokenAuthentication`」**：写死类别名时，**新加一个只认对外凭据的认证类**会被漏判成普通路由 ⇒ 通用腿会拿「必须是 200」去量一个**正确回 401** 的端点（红），而修法只有两条：要么放宽断言（⛔ 不许），要么把新类别名补进这一行（每加一种认证就得改一次测试）；按"认不认会话"推导既不需要改这一行，判的又始终是**同一个语义**（会话请求会不会被受理）。
        ⚠️ **空列表特判是承重的**：`authentication_classes = []`（`PeerBindView` / 闲管家推送接收端）在 DRF 里就是**匿名**——「一个都不认」会让上面的推导得出"全是非会话 ⇒ 只认令牌"这个**错的**结论；那两条端点对畸形入参走序列化器 ⇒ 仍是 200 + `1000`。"""
        cls = getattr(callback, "cls", None)
        if cls is None:
            return False
        classes = set(getattr(cls, "authentication_classes", []) or [])
        if not classes:
            return False
        return not any(issubclass(klass, SessionAuthentication) for klass in classes)

    @staticmethod
    def _is_text_shaped(callback):
        """纯文本契约（框架事实：视图类的 `text_response_contract`），⛔ 不手抄名单。"""
        cls = getattr(callback, "cls", None)
        return bool(getattr(cls, "text_response_contract", False))

    @staticmethod
    def _is_vendor_shaped(callback):
        """这个视图回的是不是**第三方形状**的响应体（`{"result", "msg"}`，spec §D8）。
        判据取**框架事实**（视图类自己的类属性 `vendor_response_contract`），不是手抄名单：声明它的类就是「对外契约只有 `result` / `msg`」那一类。⚠️ 该属性由**每个视图类各自声明**（基类不预置）⇒ 新加一个回信封的类不会因为继承被误判成第三方形状。"""
        cls = getattr(callback, "cls", None)
        if cls is None:
            return False
        return bool(getattr(cls, "vendor_response_contract", False))

    @staticmethod
    def _is_oauth2_shaped(callback):
        """这个视图回的响应体是不是 **OAuth2 标准形状**（`{"error", "error_description"}`）。
        判据取**框架事实**（视图类自己的类属性 `oauth2_response_contract`，照 `_is_vendor_shaped` 的同一套手法），⛔ 不是手抄名单、⛔ 更不是按「响应里有没有 `code`」反推（那是用被测结果证明被测结果，恒真）；⚠️ 该属性由**每个这类视图类各自声明**（基类不预置）⇒ 新加一个回信封的 APIView 不会因为继承被误判成这一类。
        ⚠️ **判据只看类属性，不看路由**：所以它同时管住"登记了一条其实回信封的路由"（`enumerated - derived` 非空 ⇒ 恰好对拍那条红）。"""
        cls = getattr(callback, "cls", None)
        if cls is None:
            return False
        return bool(getattr(cls, "oauth2_response_contract", False))

    @staticmethod
    def _is_wechatmp_shaped(callback):
        """这个视图说的是不是**微信 / 浏览器原生形状**（⭐ D-0268 新增的第四类）。
        判据取**框架事实**（视图类自己的类属性 `wechatmp_raw_contract`，照 `_is_vendor_shaped` 的同一套手法），⛔ 不是手抄名单。该属性由**每个这类视图类各自声明**（基类不预置）⇒ 新加一个回信封的 APIView 不会被继承误判。
        ⚠️ **判据只看类属性，不看路由**：登记了一条其实回信封的路由会被恰好对拍那条当场红。"""
        cls = getattr(callback, "cls", None)
        if cls is None:
            return False
        return bool(getattr(cls, "wechatmp_raw_contract", False))

    @staticmethod
    def _auth_probe(authentication_classes):
        """造一个「像 DRF 路由回调」的探针（只有 `cls.authentication_classes` 这一个事实）。
        用它做 `_is_token_only` 的**对照组**：那条例据今天只吃「类 + 认证类」两个属性，所以一个最小对象就够；
        ⛔ 不为了对照组去造一个真视图类（那会连带把 URL 也拉进来）。"""
        cls = type("_AuthProbeView", (), {"authentication_classes": authentication_classes})
        return type("_AuthProbeCallback", (), {"cls": cls})()

    def test_token_only_derivation_is_not_a_blanket_yes(self):
        """⭐ 对照（**能失败**）：认会话的视图、以及「一个认证类都不装」的视图，⛔ 都不许被判成 token-only（判据从"装了某个具体类"推广成"没有任何认证类认会话"，这条对照就是那次推广的判据）。⚠️ 没有它，把判据写成 `return True` 之类的退化形状**不会有任何用例变红**（豁免集合那条例据只比对**枚举到的**路由，退化后它反而更容易通过）。
        逐条（每一侧都有一个**反例**，所以不是同义反复）：`[PeerTokenAuthentication]`（既有那一档）⇒ **是**；`[一个不是 SessionAuthentication 子类的认证类]`（新那一档的**抽象形状**）⇒ **是**——用匿名类而不是 import 开放平台的认证类：这条对照测的是**推导规则**，⛔ 不该随某个具体 app 的类名漂移；`[TenantBoundAuthentication]`（本仓唯一认会话的那个）⇒ **不是**；
        `[TenantBoundAuthentication, PeerTokenAuthentication]`（两者并存）⇒ **不是**（只要有一个认会话，会话请求就会被受理 ⇒ 通用腿要的 200 仍然成立）；`[]`（`PeerBindView` / 闲管家推送接收端那种"匿名"装配）⇒ **不是**。"""
        from rest_framework.authentication import BaseAuthentication

        non_session = type("_NonSessionProbeAuth", (BaseAuthentication,), {})
        self.assertTrue(self._is_token_only(self._auth_probe([PeerTokenAuthentication])))
        self.assertTrue(self._is_token_only(self._auth_probe([non_session])))
        self.assertFalse(self._is_token_only(self._auth_probe([TenantBoundAuthentication])))
        self.assertFalse(
            self._is_token_only(
                self._auth_probe([TenantBoundAuthentication, PeerTokenAuthentication])
            )
        )
        self.assertFalse(self._is_token_only(self._auth_probe([])))

    def test_hall_is_inside_the_explicit_route_coverage(self):
        """大厅**必须**还在显式路由的覆盖集合里。
        ⚠️ 这条是上一条腿的**前置条件**，不是同义反复（Ruling 46 / 0a）：`_explicit_routes()` 现在会跳过带转换器的路由，若哪天「跳过」写宽了（例如把大厅也跳掉），上面那条腿会**空转通过**——被测的 URL 一条都没有。
        故意让 `hall` 抛未预期异常时，上面那条腿必须红。"""
        self.assertIn("/api/business/hall/", self._explicit_routes())

    def test_app_routes_are_inside_the_explicit_route_coverage(self):
        """打手 App 的 14 条显式路由**必须**都在覆盖集合里。
        来源清单被删 / 前缀写错时，这条腿对 App 端点会**静默失效**而断言照绿；
        这里把 14 条逐一钉住——少一条、或前缀从 `/api/` 改成别的都会红。"""
        routes = self._explicit_routes()
        expected = [
            "/api/app/auth/login/",
            "/api/app/auth/sms/send/",
            "/api/app/auth/sms/login/",
            "/api/app/auth/logout/",
            "/api/app/auth/me/",
            "/api/app/wallet/",
            "/api/app/wallet/release/",
            "/api/app/wallet/withdraw/",
            "/api/app/wallet/withdrawals/",
            # 充值（D-0269）：下单 / 流水 / 回调三类端点。
            # ⭐ 小程序 openid（WX_LITE）。
            "/api/app/wallet/wx-openid/",
            "/api/app/wallet/deposit/",
            "/api/app/wallet/transactions/",
            "/api/app/pay/notify/",
            # ⭐ 在线客服身份：本行把文件头的计数从 13 条改成 14 条。
            "/api/app/chat/identity/",
        ]
        missing = [url for url in expected if url not in routes]
        self.assertEqual(missing, [], f"这些 App 路由没进畸形入参网：{missing}")

    def test_audit_logs_is_inside_the_explicit_route_coverage(self):
        """审计日志端点同样**必须**在覆盖集合里（理由同上一条：防止空转通过）。
        `apps.audit` 没有 router ⇒ 它是「显式路由」这条腿唯一的新来源；来源写错（例如模块路径拼错、或 `_explicit_routes` 又加了一条过宽的跳过规则）会让这条腿对审计日志**静默失效**，而断言本身仍是绿的。
        ⚠️ 字面量**带尾斜杠**（R18）：它必须与 `apps/audit/urls.py` 的真实 pattern 一致；写成不带斜杠时这里的 `assertIn` 会红——那正是这条前置断言的作用。"""
        self.assertIn("/api/audit/logs/", self._explicit_routes())

    def test_audit_events_is_inside_the_explicit_route_coverage(self):
        """读操作上报端点**必须**在覆盖集合里，且被**按 POST** 覆盖（不是被跳过）。
        `POST /api/audit/events/`（spec §6.2 要求它进这张网）是这条腿里**只收 POST 的字面路由之一**（⚠️ **不是**全仓唯一：`path("settlements/settle", SettleView.as_view())` 也是，其 `urls.py:95-103` 的注释自述是「全仓第一条只接受 POST 的字面显式路由」——本句若写「唯一」就被当场证伪）。
        让它仍留在覆盖集合里、且选中的方法确实是 POST——这两点合起来才排除「`_request_method` 哪天把它判成 GET，于是循环里拿一个 405 当覆盖」的空转。"""
        callback = dict(self._explicit_route_entries())["/api/audit/events/"]
        self.assertEqual(self._request_method(callback), "POST")

    def test_audit_order_action_logs_is_inside_the_explicit_route_coverage(self):
        """交易订单操作记录端点同样**必须**在覆盖集合里（理由同上一条）。
        ⚠️ 它**接受 GET**、且读 `stime` / `etime` / `action` / `actor` / `key1` / `ordering` 六个查询参数（非法值 ⇒ 业务码 `1000`）⇒ 必须在网内，否则「畸形查询参数不许 500」这条结构兜底会**静默**少覆盖一条新端点（与 `settlements/people` 那两条钉点同因）。
        ⚠️ 字面量**带尾斜杠**（R18）：它必须与 `apps/audit/urls.py` 的真实 pattern 一致；写成不带斜杠时这里的 `assertIn` 会红——那正是这条前置断言的作用。"""
        self.assertIn("/api/audit/order-action-logs/", self._explicit_routes())

    def test_notifications_is_inside_the_explicit_route_coverage(self):
        """站内通知**必须**还在显式路由的覆盖集合里（同 `hall/` 那条的护栏理由）。
        ⚠️ **只增加钉点，不放宽任何既有断言**：`notifications/` 现在在网内（它的 `actions` 带 `get`）。这条钉点让「它哪天从枚举里静默掉出」变成红的——**不接受 GET 的路由也不再被跳过**（改为发 POST，见 `_explicit_route_entries` 的注释），
        所以这里的风险只剩「枚举来源漏了它」。"""
        self.assertIn("/api/business/notifications/", self._explicit_routes())

    def test_my_orders_is_inside_the_explicit_route_coverage(self):
        """我的订单**必须**还在显式路由的覆盖集合里（同上，只增加钉点）。"""
        self.assertIn("/api/business/my-orders/", self._explicit_routes())

    def test_settlement_list_is_inside_the_explicit_route_coverage(self):
        """结算列表**必须**还在显式路由的覆盖集合里（同上，只增加钉点）。
        ⚠️ 本用例与下面那条的**前提**是「`_explicit_routes()` 只跳过带转换器的路由」（`settlements/settle` 留在网内、只是发 POST）；
        结算列表必须在网内；否则它新加的两个读接口会**静默**逃出这张结构兜底。"""
        self.assertIn("/api/business/settlements", self._explicit_routes())

    def test_unattributed_is_inside_the_explicit_route_coverage(self):
        """「待归属」**必须**还在显式路由的覆盖集合里（P4-8，同上）。"""
        self.assertIn("/api/business/settlements/unattributed", self._explicit_routes())

    def test_settlement_people_is_inside_the_explicit_route_coverage(self):
        """「结账对象人员」**必须**还在显式路由的覆盖集合里（D-0089，同上，只增加钉点）。
        ⚠️ 它**接受 GET**、且**读 `game` 查询参数**（非法值 ⇒ 业务码 1000）⇒ 必须在网内，
        否则「畸形查询参数不许 500」这条结构兜底会**静默**少覆盖一条新端点（与 `settlements` / `settlements/unattributed` 两条钉点同因）。"""
        self.assertIn("/api/business/settlements/people", self._explicit_routes())

    def test_settlement_preview_is_inside_the_explicit_route_coverage(self):
        """结算预览**必须**还在显式路由的覆盖集合里（同上，只增加钉点）。
        ⚠️ 它**接受 GET**、且读 `performance_ids` 查询参数（非整数 / 超长 ⇒ 业务码 `1000`）⇒ 必须在网内，否则「畸形查询参数不许 500」这条结构兜底会**静默**少覆盖一条新端点（与 `settlements` / `settlements/unattributed` / `settlements/people` 三条钉点同因）。
        ⚠️ 字面量**不带**尾斜杠（与 `/settlements/settle` 同形）。"""
        self.assertIn("/api/business/settlements/preview", self._explicit_routes())

    def test_settlement_daily_sources_is_inside_the_explicit_route_coverage(self):
        """⭐ 来源分解端点**必须**还在显式路由的覆盖集合里（D-0323，同上，只增加钉点）。
        ⚠️ 它**接受 GET**、且读 `date` 查询参数（缺失 / 非法 ⇒ 业务码 `1000`）⇒ 必须在网内，否则「畸形查询参数不许 500」这条结构兜底会**静默**少覆盖一条新端点——尤其当哪天 `_explicit_route_entries` 的跳过规则被加宽时，通用腿会**静默**放走它，而下面那条通用断言照样绿（"空转通过"形态，本文件反复钉的就是它）。
        ⚠️ 字面量**不带**尾斜杠（与 `/settlements/daily-summary` 同形）；⚠️ `daily-summary` 自己（D-0319）没有钉点是**预存缺口**（登记在案），本条只补 D-0323 这一条，⛔ 不顺手替别的线补。"""
        self.assertIn(
            "/api/business/settlements/daily-summary/sources", self._explicit_routes()
        )

    def test_export_is_inside_the_explicit_route_coverage(self):
        """导出绩效**必须**还在显式路由的覆盖集合里（同上，只增加钉点）。
        ⚠️ 本用例是上面那条腿的**前置条件**：`/performances/export` 是本仓第一条「成功时不是信封」的端点（`.xlsx` 文件流），但它接受 GET ⇒ 必须在网内，
        否则「畸形查询参数不许 500」这条结构兜底会静默少覆盖一条新端点。"""
        self.assertIn("/api/business/performances/export", self._explicit_routes())

    def test_open_apps_is_inside_the_explicit_route_coverage(self):
        """⭐ 开放平台**应用管理**的集合路由（spec §7.3）必须还在覆盖集合里。
        ⚠️ 它是本线唯一**不带路径转换器**的那一条：同一 urlconf 里的另外四条（`apps/<int:pk>/…`）会被 `_explicit_route_entries` 的转换器跳过规则排除 ⇒ 「枚举来源漏了它」或「哪天多出一条更宽的跳过规则」都会让本线在**这张结构兜底里静默消失**，而下面那条通用断言照样绿（这正是本文件反复钉的"空转通过"形态）。
        ⚠️ 字面量**带尾斜杠**（R18）：它必须与 `apps/openapi/urls.py` 的真实 pattern 一致。"""
        self.assertIn("/api/open/apps/", self._explicit_routes())

    def test_v2_read_only_routes_are_inside_the_explicit_route_coverage(self):
        """⭐ 开放平台 v2 的**三条不带转换器**的只读路由必须还在覆盖集合里。理由与 `test_open_apps_is_inside_the_explicit_route_coverage` 逐字同源（只增加钉点）：它们**不带路径转换器** ⇒ 能落进枚举；一旦枚举来源漏了 `apps.openapi.urls`、或哪天多出一条更宽的跳过规则，它们会在这张结构兜底里**静默消失**，而下面那条「畸形参数不许 500」的通用断言照样绿（"空转通过"形态）。
        ⚠️ 同一次新增的两条 `<order_no:order_no>` 路由（`status/` / `images/`）**进不来**（被转换器跳过规则排除）⇒ 它们的畸形入参判据在 `tests/test_openapi_order_extra.py` 里用 **HTTP 级**断言补，⛔ 不在这里硬塞。
        ⚠️ 字面量都**带尾斜杠**：必须与 `apps/openapi/urls.py` 的真实 pattern 一致（写错时这里的 `assertIn` 会红——那正是这条钉点的作用）。"""
        for route in (
            "/api/open/v1/ranks/",
            "/api/open/v1/bills/",
            "/api/open/v1/games/",
            "/api/open/v1/projects/",
        ):
            self.assertIn(route, self._explicit_routes())

    def test_order_export_is_inside_the_explicit_route_coverage(self):
        """订单导出**必须**在显式路由的覆盖集合里（形状照绩效导出那条钉点）。
        ⚠️ 它**接受 GET**（是「成功时不是信封」的文件流端点）⇒ 必须在网内，否则「畸形查询参数不许 500」这条结构兜底会**静默**少覆盖一条新端点。
        ⚠️ 字面量**不带**尾斜杠（spec §2-4 实测）：路由是字面 `path("orders/export", …)`。"""
        self.assertIn("/api/business/orders/export", self._explicit_routes())

    def test_alipay_duplicates_is_inside_the_explicit_route_coverage(self):
        """支付宝查重**必须**还在显式路由的覆盖集合里（同上，只增加钉点）。
        ⚠️ 它**接受 GET**、且**不读任何查询参数** ⇒ 上面的畸形参数腿对它天然是「200 + 信封」。若哪天它被改成 POST-only，`_explicit_routes()` 会**静默**跳过它、
        护栏因此变薄——这条钉点让「变薄」变成红的。"""
        self.assertIn(
            "/api/business/performances/alipay-duplicates", self._explicit_routes()
        )

    def test_customer_members_is_inside_the_explicit_route_coverage(self):
        """⭐ 客户（会员）列表端点**必须**还在显式路由的覆盖集合里（2026-11-10 客户线，同上只增加钉点）。
        ⚠️ 它**接受 GET**、且读 `source` / `recorder` / `page` / `page_size` 查询参数
        （畸形值 ⇒ 业务码 `1000`）⇒ 必须在网内，否则「畸形查询参数不许 500」这条
        结构兜底会静默少覆盖它（"空转通过"形态）。
        ⚠️ 字面量**带尾斜杠**（与 xgj-receipts 同段习惯）；写错时这里的 `assertIn` 会红。"""
        self.assertIn("/api/business/customers/members/", self._explicit_routes())

    def test_customer_member_filters_is_inside_the_explicit_route_coverage(self):
        """⭐ 客户筛选字典端点**必须**还在覆盖集合里（同上；它不读查询参数 ⇒
        畸形参数腿天然覆盖，钉点防的是「被更宽的跳过规则静默摘出去」）。"""
        self.assertIn("/api/business/customers/member-filters/", self._explicit_routes())

    def test_customer_member_orders_is_inside_the_explicit_route_coverage(self):
        """⭐ 客户订单抽屉端点**必须**还在覆盖集合里（2026-11-10 第二批，同上只增钉点）。
        ⚠️ 它**接受 GET**、且读 `eid` / `page` / `page_size`（畸形值 ⇒ 业务码 `1000`）⇒
        必须在网内，否则「畸形查询参数不许 500」这条结构兜底会静默少覆盖它。
        ⚠️ 字面量**带尾斜杠**（与 members 同段习惯）；写错时这里的 `assertIn` 会红。"""
        self.assertIn("/api/business/customers/member-orders/", self._explicit_routes())

    def test_customer_analysis_is_inside_the_explicit_route_coverage(self):
        """⭐ 客户分析报表端点**必须**还在覆盖集合里（2026-11-10 第三批，同上只增钉点）。
        ⚠️ 它**接受 GET**、且读 `range` 查询参数（畸形值 ⇒ 业务码 `1000`）⇒ 必须在网内，
        否则「畸形查询参数不许 500」这条结构兜底会静默少覆盖它。
        ⚠️ 字面量**带尾斜杠**（与 members 同段习惯）；写错时这里的 `assertIn` 会红。"""
        self.assertIn("/api/business/customers/analysis/", self._explicit_routes())

# ⛔ 租户级鸡蛋铺线的两条**只读列表**路由（`/api/business/jidanpu-sync/events/`、`/api/business/jidanpu-sync/links/`）的两条钉点已随整条线拆除
#（`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）一并退场。⚠️ 这是**删掉已消失端点的钉点**，⛔ 不是放宽：本文件其余钉点照旧逐条 `assertIn`，
# 而「端点不存在」本身由上面那条通用腿（枚举 + 畸形参数不 500）兜着。


    def test_every_collection_survives_malformed_query_params(self):
        collections = self._collections()
        # 先自证路由表读到了东西，否则下面的循环会空转通过
        self.assertTrue(collections, "router.registry 是空的——测试本身失效了")
        for prefix in collections:
            with self.subTest(prefix=prefix):
                response = self.client.get(f"/api/business/{prefix}/", self.MALFORMED_QUERY)
                self.assertNotEqual(response.status_code, 500)
                self.assertEqual(response.status_code, 200)
                self.assertIn("code", response.json())

    @override_settings(ALIPAY_AUTH_DATA_KEY=Fernet.generate_key().decode())
    def test_every_explicit_route_survives_malformed_input(self):
        # ⚠️ 显式路由现在**跨两个 app、准入不同**：`/api/business/hall/` 要 `grab_hall.take_order` 节点（职位闸门已删，见 spec §6 第 1 行），`/api/business/settlements` 与 `/settlements/unattributed` 要 `finance.payroll`（RBAC 节点），`/api/audit/logs/` 要 `system.logs` 节点，`/api/audit/events/` 只要登录。超管对**全部**节点门都短路放行（`has_node` 的 `is_superuser` 短路），所以这里用超管保持「一条腿、一个 client」——与上面集合腿的理由同源（那里也是用超管挡住「新端点按节点放行」带来的 403）。
        # ⚠️ 为什么是超管而不是「系统管理员」这个**真实的非 superuser 职位**（它同时持 `grab_hall.take_order` 与 `finance.payroll`）：职位是**数据**、会被改；只有超管这条**不依赖任何一条业务授权规则**的前提，才能保证这条腿问的始终是「畸形参数会不会 500」，而不是随权限配置漂移。⚠️ 判据本身（`!= 500` / `== 200` / 有 `code`）一个字没放宽；各端点自己的准入由各自的用例钉住（大厅见 `test_hall_api.py`，审计见 `test_operation_log_api.py` / `test_operation_log_events_api.py`）。
        # ⚠️ **按视图允许的方法发请求**：这条腿原先是「GET 每一个字面路由」，而 `events/` 只收 POST ⇒ 旧写法会给它一个 405 假红。方法从**框架事实**推出来（`_allowed_methods`），不是手抄一份「哪些路由是 POST」的名单。
        client = make_logged_in_client(
            username="explicit-route-superuser", is_superuser=True
        )
        entries = self._explicit_route_entries()
        # 先自证枚举读到了东西，否则下面的循环会空转通过
        self.assertTrue(entries, "显式路由枚举是空的——测试本身失效了")
        for route, callback in entries:
            method = self._request_method(callback)
            # ⚠️ **分类顺序**：oauth2 最先（既有理由），wechatmp 次之——回调 / 绑定入口的 `authentication_classes` 是**空列表**，`_is_token_only`
            #    对空列表的特判今天恰好给 False；把它排在 token_only 之前，将来那条
            #    特判若被改动也不会把微信端点卷进信封断言。
            oauth2_shaped = self._is_oauth2_shaped(callback)
            wechatmp_shaped = self._is_wechatmp_shaped(callback)
            token_only = self._is_token_only(callback)
            vendor_shaped = self._is_vendor_shaped(callback)
            text_shaped = self._is_text_shaped(callback)
            with self.subTest(route=route, method=method):
                if method == "GET":
                    response = client.get(route, self.MALFORMED_QUERY)
                else:
                    headers = {}
                    if route == "/api/business/screenshot-browser/start":
                        headers["User-Agent"] = "Mozilla/5.0/WxPusher-Android"
                    response = client.post(
                        route, self.MALFORMED_BODY, content_type="application/json", headers=headers,
                    )
                # 判据本身（`!= 500` / 有 `code`）一个字没放宽；只有「业务失败是 200」
                # 这一条对**只认令牌**的端点是 401——它连会话都不收（spec §9-S2），
                # 那正是它该有的答复。豁免是显式枚举的 `TOKEN_ONLY_ROUTES`。
                self.assertNotEqual(response.status_code, 500)
                if oauth2_shaped:
                    # OAuth2 形状那一类（v2 §5.4 选项 A）：断言换成「401 + 键集**恰好**是
                    # error/error_description」，⛔ **不是**「含这两个键」——后者放不进
                    # 「顺手包了信封」的回归（信封里没有它们，有 code/msg/data）。
                    self.assertIn(route, OAUTH2_SHAPED_ROUTES)
                    self.assertEqual(response.status_code, 401, response.content[:200])
                    body = response.json()
                    self.assertEqual(
                        set(body), {"error", "error_description"}, response.content[:200]
                    )
                    self.assertNotIn("code", body)
                elif wechatmp_shaped:
                    # 微信 / 浏览器原生形状那一类（⭐ D-0268 第四类）：echostr / 空串 / 403 纯文本 / 302 跳转 / HTML 页。畸形入参的正确答复不是信封，
                    # 判据 = 不 500 + 状态码在本契约集合内 + **不是本仓 JSON 信封**
                    # （有 `code` 键 = 有人顺手包了信封，照样红）。
                    self.assertIn(route, WECHATMP_SHAPED_ROUTES)
                    self.assertIn(response.status_code, (200, 302, 403), response.content[:200])
                    if response.status_code != 302:
                        try:
                            body = response.json()
                        except ValueError:
                            body = None  # 纯文本 / HTML：原生形状 ✓
                        if body is not None:
                            self.assertNotIn("code", body, response.content[:200])
                elif token_only:
                    self.assertIn(route, TOKEN_ONLY_ROUTES)
                    self.assertEqual(response.status_code, 401, response.content[:200])
                    self.assertIn("code", response.json())
                elif text_shaped:
                    self.assertIn(route, TEXT_SHAPED_ROUTES)
                    self.assertIn(response.status_code, {200, 400, 404}, response.content[:200])
                    self.assertIn(response.content, {b"success", b"fail"}, response.content[:200])
                elif vendor_shaped:
                    # 第三方形状那一类（spec §D8）：形状断言换成「键集恰好是 result/msg」，
                    # ⛔ **不是**「含这两个键」——后者放不进统一信封的回归（信封里没有它们，
                    # 有 `code`/`msg`/`data`；「含」会把「顺手包了信封」判成绿）。
                    self.assertIn(route, VENDOR_SHAPED_ROUTES)
                    self.assertEqual(response.status_code, 200, response.content[:200])
                    body = response.json()
                    self.assertEqual(set(body), {"result", "msg"}, response.content[:200])
                    self.assertIn(body["result"], {"success", "fail"})
                else:
                    self.assertEqual(response.status_code, 200, response.content[:200])
                    self.assertIn("code", response.json())

    def test_token_only_exemptions_are_exactly_the_enumerated_ones(self):
        """豁免集合与**框架事实**逐条对拍（E-0065：豁免必须显式枚举、且能失败）。
        两侧不同就红，所以它同时挡两件事：① 新加一条只认令牌的路由却忘了登记（`derived - enumerated` 非空）；
        ② 登记了一条**其实收会话**的路由（`enumerated - derived` 非空，等于白送一条后门）。"""
        derived = {
            route for route, callback in self._explicit_route_entries()
            if self._is_token_only(callback)
        }
        self.assertEqual(derived, set(TOKEN_ONLY_ROUTES))
        # 自证非空：豁免集合为空时上面那条断言在两侧都空的情况下也通过。
        self.assertTrue(TOKEN_ONLY_ROUTES)

    def test_vendor_shaped_exemptions_are_exactly_the_enumerated_ones(self):
        """第三方形状豁免与**框架事实**逐条对拍（E-0065：豁免必须显式枚举、且能失败）。
        两侧不同就红，所以它同时挡两件事：① 新加一条回 `{"result", "msg"}` 的路由却忘了登记（`derived - enumerated` 非空 ⇒ 它会掉进通用断言、拿不到 `code` 而红）；② 登记了一条**其实回本仓信封**的路由（`enumerated - derived` 非空 ⇒ 它逃掉了「必须恰好是 result/msg」那条断言，等于白送一条后门）。
        ⚠️ 上面的 `derived` 取的是**类属性** `vendor_response_contract`（框架事实），与 `TOKEN_ONLY_ROUTES` 那条同一条手法：⛔ 不按「响应里有没有 `code`」反推（那是用被测结果证明被测结果，恒真）。"""
        derived = {
            route for route, callback in self._explicit_route_entries()
            if self._is_vendor_shaped(callback)
        }
        self.assertEqual(derived, set(VENDOR_SHAPED_ROUTES))
        # 自证非空：豁免集合为空时上面那条断言在两侧都空的情况下也通过。
        self.assertTrue(VENDOR_SHAPED_ROUTES)

    def test_oauth2_shaped_exemptions_are_exactly_the_enumerated_ones(self):
        """⭐ OAuth2 形状豁免与**框架事实**逐条对拍（E-0065：豁免必须显式枚举、且能失败）。
        两侧不同就红，所以它同时挡两件事：① 新加一条回 OAuth2 错误体的路由却忘了登记（`derived - enumerated` 非空 ⇒ 它会掉进通用断言、拿不到 `code` 而红）；② 登记了一条**其实回本仓信封**的路由（`enumerated - derived` 非空 ⇒ 它逃掉了「键集恰好是 error/error_description」那条断言，等于白送一条后门）。
        ⚠️ `derived` 取的是**类属性** `oauth2_response_contract`（框架事实），与另外两类同一条手法：⛔ 不按「响应里有没有 `code`」反推（那是用被测结果证明被测结果，恒真）。"""
        derived = {
            route for route, callback in self._explicit_route_entries()
            if self._is_oauth2_shaped(callback)
        }
        self.assertEqual(derived, set(OAUTH2_SHAPED_ROUTES))
        # 自证非空：豁免集合为空时上面那条断言在两侧都空的情况下也通过。
        self.assertTrue(OAUTH2_SHAPED_ROUTES)

    def test_wechatmp_shaped_exemptions_are_exactly_the_enumerated_ones(self):
        """微信 / 浏览器原生形状豁免与**框架事实**逐条对拍（⭐ D-0268 第四类；E-0065）。
        两侧不同就红，同时挡两件事：① 新加一条回原生形状的路由却忘了登记（`derived - enumerated` 非空 ⇒ 它掉进通用断言、拿不到 `code` 而红）；② 登记了一条**其实回本仓信封**的路由（`enumerated - derived` 非空 ⇒ 它逃掉「状态码 ∈ {200,302,403} 且不是信封」那条断言，等于白送一条后门）。
        ⚠️ `derived` 取的是**类属性** `wechatmp_raw_contract`（框架事实），⛔ 不按「响应里有没有 `code`」反推（恒真）。"""
        derived = {
            route for route, callback in self._explicit_route_entries()
            if self._is_wechatmp_shaped(callback)
        }
        self.assertEqual(derived, set(WECHATMP_SHAPED_ROUTES))
        # 自证非空：豁免集合为空时上面那条断言在两侧都空的情况下也通过。
        self.assertTrue(WECHATMP_SHAPED_ROUTES)

    def test_oauth2_shaped_derivation_is_not_a_blanket_yes(self):
        """⭐ 对照（**能失败**）：没有那个类属性的视图、以及把它显式声明成假的视图，⛔ 都不许被判成 OAuth2 形状（写法照 `test_token_only_derivation_is_not_a_blanket_yes`）。⚠️ 没有它，把判据写成 `return True` 之类的退化形状**不会有任何用例变红**（豁免集合那条例据只比对**枚举到的**路由，退化后它反而更容易通过）。
        逐条（每一侧都有一个**反例**，所以不是同义反复）：类上声明 `oauth2_response_contract = True` ⇒ **是**；类上**没有**这个属性（今天绝大多数视图）⇒ **不是**；
        类上显式声明 `= False` ⇒ **不是**（声明成假就是"我回信封"，不许被当成形状豁免）；回调是**函数**（没有 `.cls`，例如 `path("x/", some_view)`）⇒ **不是**。"""
        self.assertTrue(
            self._is_oauth2_shaped(self._oauth2_probe(oauth2_response_contract=True))
        )
        self.assertFalse(self._is_oauth2_shaped(self._oauth2_probe()))
        self.assertFalse(
            self._is_oauth2_shaped(self._oauth2_probe(oauth2_response_contract=False))
        )
        # 函数视图那一档：回调对象上直接没有 `cls`。
        self.assertFalse(self._is_oauth2_shaped(lambda request: None))

    @staticmethod
    def _oauth2_probe(**attributes):
        """造一个「像 DRF 路由回调」的探针（只有 `.cls` 上的那几个类属性这一个事实）。
        与 `_auth_probe` 同一手法：⛔ 不为了这个对照去 import 某个具体 app 的视图类
        （这条对照测的是**推导规则**，不该随某个 app 的类名漂移）。"""
        cls = type("_OAuth2ProbeView", (), dict(attributes))
        return type("_OAuth2ProbeCallback", (), {"cls": cls})()

    def test_wechatmp_shaped_derivation_is_not_a_blanket_yes(self):
        """⭐ 对照（**能失败**）：`wechatmp_raw_contract` 的推导不许退化成"全员是"
        （写法照 `test_oauth2_shaped_derivation_is_not_a_blanket_yes`；探针同一只）。
        """
        self.assertTrue(
            self._is_wechatmp_shaped(self._oauth2_probe(wechatmp_raw_contract=True))
        )
        self.assertFalse(self._is_wechatmp_shaped(self._oauth2_probe()))
        self.assertFalse(
            self._is_wechatmp_shaped(self._oauth2_probe(wechatmp_raw_contract=False))
        )
        # 函数视图那一档：回调对象上直接没有 `cls`。
        self.assertFalse(self._is_wechatmp_shaped(lambda request: None))

    def test_oauth_token_is_inside_the_explicit_route_coverage(self):
        """⭐ 取令牌端点**必须**在显式路由的覆盖集合里、且被**按 POST** 覆盖（只增加钉点）。
        ⚠️ 这条是上面那条腿的**前置条件**（同族：`test_audit_events_is_inside_the_explicit_route_coverage`）：它**不带路径转换器**、所以**能**落进枚举（带转换器的会被跳过）；一旦枚举来源漏了 `apps.openapi.urls`，或哪天多出一条更宽的跳过规则，它就会从这张结构兜底里**静默消失**，而豁免集合那条"恰好对拍"的断言反而会因为在两侧都空而绿。
        后半个判据（选中的方法确实是 POST）排除 `_request_method` 把它判成 GET、于是循环里拿一个 405 当覆盖的空转。"""
        self.assertIn("/api/open/oauth/token", self._explicit_routes())
        callback = dict(self._explicit_route_entries())["/api/open/oauth/token"]
        self.assertEqual(self._request_method(callback), "POST")


    def test_text_shaped_exemptions_are_exactly_the_enumerated_ones(self):
        """纯文本类豁免与框架事实逐条对拍（E-0065：豁免必须显式枚举、且能失败）。"""
        derived = {
            route
            for route, callback in self._explicit_route_entries()
            if self._is_text_shaped(callback)
        }
        self.assertEqual(derived, set(TEXT_SHAPED_ROUTES))
