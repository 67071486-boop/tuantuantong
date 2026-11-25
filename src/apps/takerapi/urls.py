"""打手侧开放接口的路由（**三组**：对外 / 取令牌 / 内部应用管理）。

挂载点是 `config/urls.py` 的 `path("api/taker/", include("apps.takerapi.urls"))`
——对外前缀与 `/api/open/`、`/api/business/` **并列**（版本号写进路径，契约一旦发布不能悄悄改）。

| 组 | 路径 | 认证 | 落在哪（T2–T6 落地） |
|---|---|---|---|
| **对外**（spec §5） | `/api/taker/v1/…` | `TakerAppBearerAuthentication`（**⛔ 不装权限类**，与上家侧同口径） | `api/hall_views.py` / `api/my_orders_views.py` / `api/order_status_views.py` / `api/order_images_views.py` / `api/order_write_views.py` |
| **取令牌**（spec §2 / §6） | `/api/taker/oauth/token` | ⛔ **不装配任何认证类**（它自己就是凭据入口） | `api/token_views.py` |
| **内部**应用管理（spec §6） | `/api/taker/apps/…` | **全局默认的会话认证** + 节点门 `system.allow_taker_open_api`（⛔ 不认 Bearer 令牌） | `api/app_views.py` |

⚠️ 三组**不要混**（同族先例：`apps/openapi/urls.py` 与 `apps/business/api/peer_views.py` 的模块
docstring）：对外端点只认 `Authorization: Bearer <令牌>`（会话请求必然 401），取令牌端点只认 form 里的
凭据（⚠️ 它回的是 **OAuth2 标准体**、不是本仓信封，判据在 `tests/test_no_route_500.py` 的
`OAUTH2_SHAPED_ROUTES` 那一档），内部端点只认会话（⛔ 不认令牌）。三组不混的**承重判据**是
"跨凭据类型必然 401"（spec §9-2：打手令牌打 `/api/open/v1/…` ⇒ 401，上家令牌打
`/api/taker/v1/…` ⇒ 401）。

⚠️ **端点的路由一律写显式 `path()`、⛔ 一个 router 都不建**（照 `apps/openapi/urls.py` 的纪律）：
对外接口没有集合语义，内部应用管理**刻意不用 router**——router 会自动暴露 `destroy`，
而本线的纪律是「**只能停用、不能删**」（spec §3.1 / §6），那条纪律的判据是 `DELETE` 回 **405**。

## ⛔⭐ 现行口径（T6）：上面那张表里**第一组（对外 `v1/…`）今天全部落地了**

本文件现在有 **12** 条显式 `path()`：取令牌 1（T2b）+ 对外 7（T5 两条 + T6 五条）
+ 内部应用管理 4（T3）。⚠️ 上表第一组那一行原先只列了 `hall_views` / `order_write_views`
两个落点，T6 同一次把实际上有**四个**模块这件事写准（对外端点的载体模块是：
`hall_views`（大厅只读）/ `order_write_views`（抢单）/ `my_orders_views`（我的单 + 查单）
/ `order_status_views`（改状态）/ `order_images_views`（进度图三条）——**五个**，
其中 `order_scope.py` 是**判据**模块（⛔ 不产路由）。

⭐ **T6 的五条各自的范围判据都是「他是这张单的当前接手人」**（spec §5.3-2 的红队改准），
**唯一出处** = `api/order_scope.py`。⛔ 与 `v1/hall/` 那一族用的"游戏范围内"
（`order_write_views._live_order_in_scope`）**不是同一件事**：那一份对**大厅**是对的，
而拿它当这五条的判据会让**"范围内、别人接的单"被查到 / 被改到**
（`tests/test_takerapi_my_orders_api.py` 有一条专门钉这件事的用例）。

## ⚠️ 本文件今天是**空的**（T1）

`urlpatterns = []` 是**有意**的：本任务（T1）只建骨架 + 三张表 + 迁移 + 登记，
⛔ 不写任何端点（那是 T2–T7）。⇒ **今天这三组一条路由都没有**。

⚠️ 由此带来一个必须留痕的**登记时点**问题（T1 实测）：`tests/test_operation_log_coverage.py`
的 `test_no_undeclared_urlconf_produces_endpoints` 有一条判据是「`COVERAGE_SCOPE` 声明的模块
**必须真的产出至少一个 leaf**」⇒ **在还没有端点的今天**把本模块登记进
`tests/test_operation_log_ledger.py` 的 `COVERAGE_SCOPE` / `test_operation_log_coverage.py` 的
`NON_ROUTER_MODULES`（以及那里的 `declared_modules` 字面量）会**当场红**，而它红得**有道理**
（"声明了却一个端点都没有"是无意义的声明；⛔ 不许为此放宽断言）。T1 实测的原始报文逐字：
`COVERAGE_SCOPE 声明了 apps.takerapi.urls，但它一个 leaf 端点都没有`、
`apps.takerapi.urls（前缀 api/taker/）在端点全集里一个端点都没有`
（另有 `declared_modules` 那条**恰好集合**也一起红）。
⇒ 本模块进这两处的**时点与第一批路由同一次**（T5/T6），⛔ 不是 T1。
⚠️ 而这**不是**一条静默的口子（T1 用一条临时探针路由**实测过**）：同一张网的另一条腿按
「leaf 的父级 `include()` 模块」反查（`produced - declared` 必须**恰好**等于 `["config.urls"]`）
⇒ 本模块一旦真的产出端点却漏登记，那张网**立刻红**并点名本模块（实测报文逐字：
`这些 urlconf 模块产出了端点，但不在 COVERAGE_SCOPE 声明里：['apps.takerapi.urls', 'config.urls']`）
—— ⛔ 不依赖任何人记得回来补。
⚠️ `tests/test_no_route_500.py` 的 `EXPLICIT_ROUTE_SOURCES` 相反：**T1 就登记**（它按模块枚举
`urlpatterns`，空表贡献 0 条路由、不会假绿），这样 T5/T6 新增的**不带转换器**的对外路由
从第一天起就在那张结构兜底里。

## ⛔⭐ 现行口径（T2b）：上面那一段「本文件今天是空的」**已过期**

本文件今天有**第一条真实路由**：`path("oauth/token", TakerTokenView.as_view(), …)`
（spec §2 的 `/api/taker/oauth/token`）。⚠️ 上表第二组那一行是**唯一**已落地的，
第一组（对外 `v1/…`）与第三组（内部 `apps/…`）**仍然没有**（T3 / T5 / T6）。

⭐ **因此上面那段"登记时点"的账同一次收掉**（T1 有意推到"第一条路由"的那一刻，
理由就是上面实测的那三条红）—— **同一次**改这三处，⛔ 一处都不能拖到 T5/T6：

| # | 文件 | 改什么 |
|---|---|---|
| ① | `tests/test_operation_log_ledger.py::COVERAGE_SCOPE` | 加 `("api/taker/", "apps.takerapi.urls")` |
| ② | `tests/test_operation_log_coverage.py::NON_ROUTER_MODULES` | 加 `"apps.takerapi.urls"`（本模块**无 router**，全是显式 `path()`） |
| ② | `tests/test_operation_log_ledger.py::DELIBERATELY_NOT_LOGGED` | 按既有 `api/open/oauth/token` 那一条的形状加**打手侧这一条**（取令牌端点**有意不写操作留痕**：它是纯本地凭据交换，没有可留痕的业务动作） |
| ③ | `tests/test_no_route_500.py::OAUTH2_SHAPED_ROUTES` | 加 `"/api/taker/oauth/token"`（⚠️ **字面量不带尾斜杠**，与 `path()` 的契约逐字一致） |

⚠️ 那一批（②③）都是**恰好集合**断言：登记多了 / 少了都红，⛔ 不许放宽成"包含即可"。
⚠️ `EXPLICIT_ROUTE_SOURCES`（`test_no_route_500`）**一个字都不用动** —— T1 已经登记过本模块。

## ⛔⭐ 现行口径（T3）：上表**第三组（内部应用管理）今天落地了**

本文件现在多出**四条内部路由**（spec §6）：`GET` / `POST` 都是 `/api/taker/apps/`、
`PATCH /api/taker/apps/<int:pk>/`、`POST /api/taker/apps/<int:pk>/rotate-secret/`
—— **全是显式 `path()`，一个 router 都不建**（理由同上面那段纪律：router 会自动暴露
`destroy`，而本线的纪律是「只能停用、不能删」，判据是那三条路由回 **405**）。
⚠️ 上表**第一组（对外 `v1/…`）仍然没有**（T5 / T6）。
⚠️ **四条路由都带路径转换器**（`<int:pk>`）⇒ 与上家侧那几条同款，落进
`tests/test_no_route_500.py` 显式路由腿的**已知盲区**（那张网按 Django 自己的事实跳过带
转换器的路由）⇒ 它们的畸形入参判据**不是"不管"**：由 `tests/test_takerapi_app_api.py`
用 **HTTP 级**用例逐条钉住（含"天文数字 pk"那一格）。
⛔ **本文件里那条 `oauth/token` 与它那四处登记一个字都不动**（那是 T2b 收的口，见上表）：
本任务只是**追加**路由。

## ⛔⭐ 现行口径（T5）：上表**第一组（对外 `v1/…`）今天落地了两条**

`GET /api/taker/v1/hall/`（拉大厅）与 `POST /api/taker/v1/hall/<order_no>/claim/`（抢单）
—— spec §5 端点表的前两条。⚠️ 上表第一组那一行写的落点（`api/hall_views.py` /
`api/order_write_views.py`）**同一次兑现**：只读那条住 `api/hall_views.py`、
写那条住 `api/order_write_views.py`（T6 的「我的单 / 查单 / 改状态 / 进度图」再补
`api/my_orders_views.py` 那一条）。

⭐ **本文件多了一处 import**：`from apps.business import converters`（**副作用 import**）。
`<order_no:order_no>` 这个转换器由 `apps/business/converters.py` 在 **import 时**
`register_converter` 登记——⛔ 不 import 它，`path()` 会在**启动时**就
`ImproperlyConfigured: No converter ... is registered`（响亮，不是静默）。
⚠️ 上家侧那条 `v1/orders/<order_no:order_no>/` 用的是**同一个**转换器与**同一条纪律**
（它自己的 `urls.py` 里也有这一行 import）——共享转换器是本仓对"路径段里的 NUL 会 500"
那件事的**唯一**防线（`apps/business/converters.py` 的模块 docstring 写着实测）。

⚠️ **两条新路由的登记各归各处**（⛔ 一处都不能漏，⛔ 也不许放宽成"包含即可"）：
- 不带转换器的 `v1/hall/` ⇒ 落进 `tests/test_no_route_500.py` 的**显式路由腿**，
  且它是"只认令牌"那一类 ⇒ 同一次登记进 `TOKEN_ONLY_ROUTES`（那张表是**恰好**集合断言）；
- 带转换器的 `v1/hall/<order_no:order_no>/claim/` ⇒ **逃出**那条腿（`_explicit_route_entries`
  刻意跳过带转换器的路由），⇒ 必须同一次进 `tests/test_p4_no_500.py` 的
  「共享转换器路由**恰好**集合」，并**另补 HTTP 级畸形入参用例**
  （`tests/test_takerapi_claim_api.py`，含 NUL 那一段 ⇒ 正确答复是 404）；
- 抢单是**写端点** ⇒ 同一次进 `tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`
  （代码里那一次 `record(...)` 在 `api/order_write_views.py`）；
- `GET v1/hall/` 是**读端点**（按既有口径不埋点）⇒ 进
  `tests/test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD`（反向钉子：
  这条路径上不许长出写方法）。

## ⛔⭐ 现行口径（T5，回调线）：内部应用管理那一组多了**两条**路由

本文件现在共 **14** 条显式 `path()`（原 **12** 条 + 本期 2 条）。⭐ 新加的两条是
第二期（`docs/specs/2026-10-09-taker-callback-design.md` §7 的第二、三行）：

| 方法 | 路径 | 读 / 写 | 登记落在哪（⛔ 一处都不能漏） |
|---|---|---|---|
| `GET` | `/api/taker/apps/<int:pk>/deliveries/` | **读** | `tests/test_operation_log_coverage.py::MUST_HAVE_NO_WRITE_METHOD`（反向钉子） |
| `POST` | `/api/taker/apps/<int:pk>/deliveries/<int:delivery_pk>/retry/` | **写** | `tests/test_operation_log_ledger.py::INSTRUMENTED`（动作码 `system.taker_api_delivery_retry`） |

⚠️ **两条都带路径转换器** ⇒ 与 T3 那两条明细路由**同款**：落进
`tests/test_no_route_500.py` 显式路由腿的**已知盲区**（那张网按 Django 自己的事实跳过带转换器的
路由）⇒ 它们的畸形入参判据**不是"不管"**：由 `tests/test_takerapi_callback_api.py` 用
**HTTP 级**用例逐条钉住。
⚠️ 本文件**仍然一个 router 都不建**（两条新路由同样是显式 `path()`），`DELETE` 在它们上面
照旧回 **405**（「只能停用不能删」那条纪律没有放松）。
⚠️ 上表第一组（对外 `v1/…`）的读数**不变**：它今天仍是**七条**（T5 两条 + T6 五条）。
⚠️ `COVERAGE_SCOPE` / `NON_ROUTER_MODULES` / `EXPLICIT_ROUTE_SOURCES` 那三处登记
**一个字都不用改**（它们是"前缀 → 模块"的声明，⛔ 不是端点清单）——
这正是"按模块枚举而不是手抄 URL 清单"的价值：新端点自动落进覆盖网。
"""
from django.urls import path

# ⚠️ **副作用 import**（见模块 docstring 的 T5 改准块）：`<order_no:order_no>` 由它登记。
from apps.business import converters  # noqa: F401
from apps.takerapi.api.app_views import (
    TakerAppDeliveriesView,
    TakerAppDeliveryRetryView,
    TakerAppDetailView,
    TakerAppListCreateView,
    TakerAppRotateSecretView,
)
from apps.takerapi.api.hall_views import (
    HallListView,
    HallMirrorClaimView,
    HallPeerClaimView,
)
from apps.takerapi.api.my_orders_views import MyOrderDetailView, MyOrdersListView
from apps.takerapi.api.order_images_views import (
    TakerOrderImageTicketView,
    TakerOrderImagesView,
)
from apps.takerapi.api.order_status_views import TakerOrderStatusChangeView
from apps.takerapi.api.order_write_views import HallClaimView
from apps.takerapi.api.token_views import TakerTokenView

urlpatterns = [
    # ⭐ 打手侧取令牌端点（spec §2；T2b）。⚠️ 与上家侧那条同款的两点：
    # ① **不带路径转换器** ⇒ 它落进 `tests/test_no_route_500.py` 的**显式路由腿**（那张网
    # 跳过带转换器的路由），⇒ 它的畸形入参判据由那张网**同一次**登记
    # （`OAUTH2_SHAPED_ROUTES`：正确答复是 401 + 恰好 `{"error", "error_description"}`）；
    # ② **不带尾斜杠**是 spec §2 的契约逐字（`/api/taker/oauth/token`，⛔ 不是 `…/token/`），
    # 上面那张清单里的字面量也**必须**同一条拼写。
    path("oauth/token", TakerTokenView.as_view(), name="taker-oauth-token"),
    # ── 对外抢单（spec §5 端点表第 1、2 行；T5）──────────────────────────────
    # 认证 = `TakerAppBearerAuthentication`（⛔ 不装权限类：范围由**绑定账号**决定，
    # 见 `api/hall_views.py` 的模块 docstring）+ 兜底闸 `TakerAppFallbackThrottle`。
    # ⚠️ 两条路径**都带尾斜杠**（spec §5 的契约逐字，与上家侧对外面同款）。
    # ⚠️ 抢单那条的路径段用**共享** `order_no` 转换器（`apps/business/converters.py`）：
    # 裸 `<str:order_no>` 会让 `%00` 进 `filter(order_no=…)` ⇒ PostgreSQL `DataError` ⇒
    # **HTML 500**（本仓实测过；畸形路径段因此**不匹配路由** ⇒ 404，⛔ 不是 500）。
    path("v1/hall/", HallListView.as_view(), name="taker-hall-list"),
    path(
        "v1/hall/<order_no:order_no>/claim/",
        HallClaimView.as_view(),
        name="taker-hall-claim",
    ),
    # ⭐ 镜像单接单（D-0296 / T6，业务方）：打手 API 的大厅并入了镜像单
    # （读侧见 `api/hall_views.py` 的两路并读）⇒ 接单侧同一次补这条路径。
    # ⚠️ 带 `<int:pk>` 路径转换器 ⇒ 与 T3 那几条同款，落进
    # `tests/test_no_route_500.py` 显式路由腿的已知盲区 ⇒ 畸形入参判据由
    # `tests/test_takerapi_hall_api.py` 的 HTTP 级用例逐条钉住。
    # ⚠️ 写端点（领域层转正会建单 + 写审计）⇒ 装配照 `HallClaimView` 同款三件；
    # 留痕在领域层（`promote_upstream_mirror` 的 record），本视图不重复记
    # （见 `HallMirrorClaimView` 的 docstring）。
    path(
        "v1/hall/upstream/<int:pk>/claim/",
        HallMirrorClaimView.as_view(),
        name="taker-hall-mirror-claim",
    ),
    # ⭐ 团团通上家镜像单接单（D-0303）：打手 API 的大厅并入了团团通镜像单
    # （读侧见 `api/hall_views.py` 的三路并读）⇒ 接单侧同一次补这条路径。
    # ⚠️ 与上面那条鸡蛋铺 `hall/upstream/<pk>/claim/` **同族但不同源**（确认制：
    # 预占 + 排 `hall.claim` 事件就返回「接单中」，正式订单由上家 `order.assign` 推回）。
    # ⚠️ 带 `<int:pk>` 路径转换器 ⇒ 落 `test_no_route_500.py` 显式路由腿的已知盲区
    # ⇒ 畸形入参判据由 `tests/test_peer_hall_read.py` 的 HTTP 级用例钉住。
    path(
        "v1/hall/peer/<int:pk>/claim/",
        HallPeerClaimView.as_view(),
        name="taker-hall-peer-claim",
    ),
    # ── 对外：我的单 / 查单 / 改状态 / 进度图（spec §5 端点表第 3–6 行；T6）────────
    # 认证与装配同 T5 那两条（`TakerAppBearerAuthentication` + ⛔ 不装权限类 +
    # `TakerAppFallbackThrottle`，三件写在各自的模块里）。
    # ⛔⭐ **范围判据一律是「他是这张单的当前接手人」**（`api/order_scope.py` 是那一条判据的
    # 唯一出处）——⛔ **不是** `v1/hall/` 那一族用的"游戏范围内"（大厅那条对大厅是对的，
    # 对这几条会让"范围内、别人接的单"被查到 / 被改到；理由逐条写在 `order_scope` 的
    # 模块 docstring 里）。
    # ⚠️ **两类路径各归各的登记**（⛔ 一处都不能漏、⛔ 也不许放宽成"包含即可"）：
    # · `v1/my/orders/` **不带转换器** ⇒ 落 `tests/test_no_route_500.py` 的**显式路由腿**
    # （它是"只认令牌"那一类 ⇒ 同一次登记进那张**恰好**集合 `TOKEN_ONLY_ROUTES`）；
    # · 下面**五条**全部带 `<order_no:order_no>` ⇒ **逃出**那条腿（`_explicit_route_entries`
    # 刻意跳过带转换器的路由）⇒ 必须同一次进 `tests/test_p4_no_500.py` 的
    # 「共享转换器路由**恰好**集合」，并各自补 HTTP 级畸形入参用例；
    # · 写端点（改状态 / 取票 / 认领）⇒ 各自登记进
    # `tests/test_operation_log_ledger.py`（留痕指针或"刻意不记"，见各自的模块 docstring）；
    # · 只读那两条（`v1/my/orders/` 与 `…/images/`）⇒ 进
    # `tests/test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD`（反向钉子）。
    # ⚠️ **`…/images/` 只有一条 pattern**：它的 `GET`（列表）与 `POST`（认领）**同住一个视图类**
    # （Django 的 `URLResolver.resolve` 按顺序取第一个匹配 ⇒ 同路径的第二条 `path()`
    # 永远不会被走到）。先例逐字相同：上家侧的 `OrderImagesView`、T5 的 `status/`。
    path("v1/my/orders/", MyOrdersListView.as_view(), name="taker-my-orders"),
    path(
        "v1/orders/<order_no:order_no>/",
        MyOrderDetailView.as_view(),
        name="taker-order-detail",
    ),
    path(
        "v1/orders/<order_no:order_no>/status/",
        TakerOrderStatusChangeView.as_view(),
        name="taker-order-status",
    ),
    path(
        "v1/orders/<order_no:order_no>/images/ticket/",
        TakerOrderImageTicketView.as_view(),
        name="taker-order-image-ticket",
    ),
    path(
        "v1/orders/<order_no:order_no>/images/",
        TakerOrderImagesView.as_view(),
        name="taker-order-images",
    ),
    # ── 内部应用管理（spec §6；T3）─────────────────────────────────────────────
    # 认证 = **全局默认的会话认证**（视图类不声明 `authentication_classes`）
    # ＋ 节点门 `has_node(*APP_MANAGE_NODES)`（`system.pinduoduo_settings` 或
    # `system.allow_taker_open_api`，**任一**即可）。⛔ 不认 Bearer 令牌。
    # ⚠️ **这里没有 `DELETE`**：未定义的方法在**已存在的路径**上回 405（由用例钉住）。
    # 准确表述是「**认证 + 权限过了之后**才是 405」——DRF 的 405 判定在 `initial()`
    # （认证 / 权限）**之后**，所以匿名 / 无节点的 `DELETE` 会先被 **401 / 403** 挡下
    # （⛔ 不是 405，也不是说"路径不存在"）。两条边界都在用例里逐条钉住。
    # ⚠️ 列表那条**不带分页**（照上家侧列表的形状：`api_response([...])` 直接给数组）。
    path("apps/", TakerAppListCreateView.as_view(), name="taker-app-list-create"),
    path("apps/<int:pk>/", TakerAppDetailView.as_view(), name="taker-app-detail"),
    path(
        "apps/<int:pk>/rotate-secret/",
        TakerAppRotateSecretView.as_view(),
        name="taker-app-rotate-secret",
    ),
    # ── 内部：推送台账 / 手工重投（第二期 spec §7 的第二、三行；T5）────────────────
    # 装配与上面那四条**逐字相同**（两个类都继承 `TakerAppAdminView`：会话认证 +
    # 同一个节点门 + `_get_app` 的**身份限权**）⇒ ⛔ 没有第二套认证 / 权限 / 取数。
    # ⚠️ **两条都带路径转换器**（`<int:pk>` / `<int:delivery_pk>`）⇒ 与上面那两条明细路由
    # **同款**，落进 `tests/test_no_route_500.py` 显式路由腿的**已知盲区**
    # （那张网按 Django 自己的事实跳过带转换器的路由）⇒ 它们的畸形入参判据**不是"不管"**：
    # 由 `tests/test_takerapi_callback_api.py` 用 **HTTP 级**用例逐条钉住。
    # ⚠️ **各归各的登记**（⛔ 一处都不能漏、⛔ 也不许放宽成"包含即可"）：
    # · **重投是写端点** ⇒ 同一次进 `tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`
    # （动作码 `system.taker_api_delivery_retry`，那一次 `record(...)` 在
    # `api/app_views.py::TakerAppDeliveryRetryView.post` 里、经 `_record_app` 走）；
    # · **台账那条是读端点**（按既有口径不埋点）⇒ 进
    # `tests/test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD`（反向钉子）。
    path(
        "apps/<int:pk>/deliveries/",
        TakerAppDeliveriesView.as_view(),
        name="taker-app-deliveries",
    ),
    path(
        "apps/<int:pk>/deliveries/<int:delivery_pk>/retry/",
        TakerAppDeliveryRetryView.as_view(),
        name="taker-app-delivery-retry",
    ),
]
