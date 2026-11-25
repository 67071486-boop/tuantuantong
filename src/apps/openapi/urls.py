"""开放平台路由。挂在 `/api/open/`，和 `/api/business/` 并列；版本号写在路径里。

三套认证不要混：
- 对外 `v1/…`：`ApiAppBearerAuthentication`，不装权限类。一个应用凭据看本租户全部业务数据。
- `oauth/token`：不装认证类，它自己就是凭据入口；回的是 OAuth2 标准体，不是本仓信封。
- 内部 `apps/`、`wecom-qr-app/`：会话认证 + `system.pinduoduo_settings`。

入站 HMAC 签名已经没有调用点。出站推送是另一族头（`apps.openapi.outbound`），不要当成同一件事。

同一条路径的第二个方法不要再注册一条路由：Django 先匹配先赢，第二条永远走不到。
改状态和查状态、改单和详情、认领和图片列表，都挂在同一个视图类上。

全部用显式 `path()`，不用 router：router 会露出 `destroy`，而应用只能停用不能删，`DELETE` 应是 405。
匿名或没节点会先 401/403，405 在认证和权限之后。

带路径转换器的路由不进 `test_no_route_500` 的显式腿，畸形入参由各自的 HTTP 用例补，那些清单是恰好集合。
`apps/` 上 `<int:pk>` 只认数字，不是上界守卫；超 int8 由数据库比较成 0 行，走查不到。
不带转换器、只认 Bearer 的列表要进 `TOKEN_ONLY_ROUTES`。

`oauth/token` 不带尾斜杠，契约就是 `/oauth/token`。
`v1/orders/` 和详情的段数不同，不会互相吃掉，顺序不是判据。
"""
from django.urls import path

# ⚠️ **必须 import**：它在模块级 `register_converter(..., "order_no")`，下面的
# `<order_no:order_no>` 依赖它已登记（⛔ 不重新实现那个转换器：它挡 NUL 字节这件事
# 只有一份实现，见 `apps/business/converters.py` 的 docstring）。
from apps.business import converters  # noqa: F401  （仅为登记转换器）
from apps.openapi.api.app_views import (
    ApiAppDeliveriesView,
    ApiAppDeliveryRetryView,
    ApiAppDetailView,
    ApiAppListCreateView,
    ApiAppRotateSecretView,
)
from apps.openapi.api.order_extra_views import (
    BillListView,
    GameListView,
    OrderImagesView,
    OrderStatusView,
    ProjectListView,
    RankListView,
)
from apps.openapi.api.order_image_views import OrderImageTicketView
from apps.openapi.api.order_query_views import OrderDetailView, OrderSourceQueryView
from apps.openapi.api.order_write_views import OrderPriceAssignView
from apps.openapi.api.token_views import TokenView
from apps.openapi.api.wecom_qr_app_views import (
    WecomQrAppDeliveriesView,
    WecomQrAppRotateSecretView,
    WecomQrAppSingletonView,
)

urlpatterns = [
    # ⭐ 取令牌端点（v2 §5.1；T2）。⚠️ **不带路径转换器、也⛔ 不带尾斜杠**：
    # 不带转换器 ⇒ 它会落进 `tests/test_no_route_500.py` 的显式路由腿（那张网跳过带转换器的路由），
    # 也就是它的畸形入参判据由那张网**同一次**登记（`OAUTH2_SHAPED_ROUTES`，v2 §13.4-16）；
    # 不带尾斜杠是 §5.1 的契约逐字（`/oauth/token`，⛔ 不是 `/oauth/token/`）。
    path("oauth/token", TokenView.as_view(), name="open-oauth-token"),
    # ⚠️ 两条**不会互相吃掉**（`v1/orders/` 编译成 `^v1/orders/\Z`，段数不同）⇒ 顺序
    # 不构成判据；这里按「列表在前、详情在后」的常规读法排，⛔ 别据此写「必须这个顺序」
    # 那种能被一个反例证伪的绝对句。
    path("v1/orders/", OrderSourceQueryView.as_view(), name="open-order-query"),
    path(
        "v1/orders/<order_no:order_no>/",
        OrderDetailView.as_view(),
        name="open-order-detail",
    ),
    # 对外只读扩展。认证是 Bearer，不装权限类。
    # 带 order_no 转换器的路由不进 test_no_route_500 的显式腿，畸形入参由
    # tests/test_openapi_order_extra.py 用 HTTP 断言补，并进共享转换器的恰好集合。
    path(
        "v1/orders/<order_no:order_no>/status/",
        OrderStatusView.as_view(),
        name="open-order-status",
    ),
    # ── 对外 v2 的**写端点**第一批（T5；设计文档 §6.1 / §6.2 / §6.3）────────────────────
    # ⚠️ 改状态与查状态**共用上面那一条路由**（§6.1 的两条契约：`GetOrderStatus` 与
    # `ChangeOrderStatus` 是同一条路径的两个方法）⇒ 这里**不重复注册** `status/`。
    # 理由（框架事实）：Django 的 `URLResolver.resolve` 按顺序取**第一个**匹配的 pattern
    # ⇒ 再注册一条只收 `POST` 的同 pattern 路由**永远不会被走到**（第一条对 `POST` 回 405）。
    # ⇒ 两个方法挂**同一个视图类**：`OrderStatusView` 混入了
    # `apps/openapi/api/order_write_views.py` 的 `OrderStatusChangeMixin`（那个 mixin 里
    # 有完整的理由与代价）。上面那一条路由**一个字都没改**。
    path(
        "v1/orders/<order_no:order_no>/assign-price/",
        OrderPriceAssignView.as_view(),
        name="open-order-assign-price",
    ),
    path(
        "v1/orders/<order_no:order_no>/images/",
        OrderImagesView.as_view(),
        name="open-order-images",
    ),
    # ⭐⭐ 追加（T10a）—— 进度图的**取票 + 认领**两条写端点（设计文档 §13.10-3）：
    # 业务方把对标的「传一个 imgUrl」改成「**取票 + 认领**」两步（服务端⛔ **不抓**第三方 URL，
    # 因而没有 SSRF 面）。
    # ⚠️ **这里只有取票那一条**：认领（`POST v1/orders/<order_no>/images/`）与上面 T4 的
    # **同一条路由**（同一条路径的两个方法、挂在同一个视图类上）──
    # ⛔ **不许**为它再注册一条同 pattern 的路由：Django 先匹配先赢，第二条永远走不到
    # （T5 已为 `status/` 实测过同一个坑；逐条理由见
    # `api/order_image_views.OrderImageClaimMixin` 的 docstring）。
    # ⚠️ 它带 `order_no` 共享转换器 ⇒ 与其它几条一样落进 `test_no_route_500` 的**盲区**
    # （那张网跳过带转换器的路由）⇒ 它的畸形入参判据由 `tests/test_openapi_order_images.py`
    # 用 **HTTP 级**断言补（⛔ 不是不管），并同一次进 `test_p4_no_500` 的
    # 「共享转换器路由**恰好**集合」。
    # ⚠️ 它**不带** `TOKEN_ONLY_ROUTES` / 形状豁免（它回本仓标准信封 + 401 有 `code`），
    # 但它落不进那张网的显式路由腿（转换器），所以那两处清单都不动它 —— 与 T4/T5 的
    # 三条带转换器的 v2 路由**同一处置**。
    path(
        "v1/orders/<order_no:order_no>/images/ticket/",
        OrderImageTicketView.as_view(),
        name="open-order-image-ticket",
    ),
    # ⚠️ 下面三条**不带路径转换器** ⇒ 它们落进 `test_no_route_500` 的显式路由腿，
    # 而它们是 **Bearer-only**（会话请求必然 401）⇒ 同一次登记进 `TOKEN_ONLY_ROUTES`
    # （那张表与派生集合是**恰好**对拍，⛔ 不许放宽）。
    path("v1/ranks/", RankListView.as_view(), name="open-rank-list"),
    path("v1/bills/", BillListView.as_view(), name="open-bill-list"),
    path("v1/games/", GameListView.as_view(), name="open-game-list"),
    path("v1/projects/", ProjectListView.as_view(), name="open-project-list"),
    # ── 应用管理（**内部**，会话认证 + `system.pinduoduo_settings` 节点门；§7.3）──────
    # ⚠️ 这里**没有** `DELETE`，也**没有** `PUT /apps/<pk>/`（改名走 `PATCH`）：
    # 未定义的方法在**已存在的路径**上回 405（由用例钉住）。⚠️ 准确表述是「**认证 +
    # 权限过了之后**才是 405」——DRF 的 405 判定在 `initial()`（认证 / 权限）**之后**，
    # 所以匿名 / 无节点的 `DELETE` 会先被 **401 / 403** 挡下（⛔ 不是 405，也不是说
    # "路径不存在"）。
    path("apps/", ApiAppListCreateView.as_view(), name="open-app-list-create"),
    path("apps/<int:pk>/", ApiAppDetailView.as_view(), name="open-app-detail"),
    path(
        "apps/<int:pk>/rotate-secret/",
        ApiAppRotateSecretView.as_view(),
        name="open-app-rotate-secret",
    ),
    # ⭐ 推送台账的两个**内部**端点（T9a，设计文档 §7.4 / §8 的「最近推送记录 + 手工重投」）。
    # ⚠️ 两条**都带路径转换器** ⇒ 与上面那四条一样落进 `test_no_route_500` 的**盲区**
    # （那张网的显式路由腿按 Django 自己的事实跳过带转换器的路由）⇒ 它们的畸形入参判据
    # **不是"不管"**：六条带转换器的应用管理路由（原有四条 + 本处两条）一起进
    # `tests/test_openapi_app_api.py` 的 `CONVERTER_ROUTES`（那张表按实测扩项，
    # 判据一个字没放宽），更细的一批（`delivery_pk` 非法、`status` 非法、`page` 非法）在
    # `tests/test_openapi_delivery_admin.py` 里用 HTTP 级断言逐条钉住。
    # ⚠️ 重投那条只收 `POST`，且它的路径段数比台账列表多两段 ⇒ 两条**不会互相吃掉**
    # （⛔ 别据此写"必须这个顺序"那种能被一个反例证伪的绝对句）。
    path(
        "apps/<int:pk>/deliveries/",
        ApiAppDeliveriesView.as_view(),
        name="open-app-deliveries",
    ),
    path(
        "apps/<int:pk>/deliveries/<int:delivery_pk>/retry/",
        ApiAppDeliveryRetryView.as_view(),
        name="open-app-delivery-retry",
    ),
    # ── ⭐ 企微群码**锁定应用**的单例管理（T3，设计文档 §7.3 / D7 / D9）────────────
    # 装配与上面那六条**逐字相同**（全局默认的会话认证 + 现成的 `system.pinduoduo_settings`
    # 节点门）：三个视图类都继承 `ApiAppAdminView`（见 `api/wecom_qr_app_views.py` 的模块 docstring）。
    # ⚠️ **单例资源形状**（D7）：路径里**没有 pk** —— 「唯一、不能多个」写成 URL 形状，
    # 前端就没有"换个 pk 试试"的余地，也不会让应用管理页多出一类它管不了的行。
    # ⚠️ `GET`（单例）那条**不存在时回 `data=null`**（⛔ 不是 404 / 1002）——它与
    # "按 pk 取一条"的 `apps/<int:pk>/` 是**两件事**，判据在 `tests/test_openapi_wecom_qr_app.py`。
    # ⚠️ 这三条**不带路径转换器** ⇒ 它们落进 `test_no_route_500` 的**显式路由腿**
    # （那条腿要求"畸形入参也不许 500、且仍是标准信封"，⛔ 不是不管）；
    # 上面那六条带转换器的仍走 `CONVERTER_ROUTES` 那条路（两边**互不影响**）。
    # ⛔ 同样**没有 `DELETE`**（沿用「只能停用不能删」，§7.3 最后一行）。
    path(
        "wecom-qr-app/",
        WecomQrAppSingletonView.as_view(),
        name="open-wecom-qr-app",
    ),
    path(
        "wecom-qr-app/rotate-secret/",
        WecomQrAppRotateSecretView.as_view(),
        name="open-wecom-qr-app-rotate-secret",
    ),
    path(
        "wecom-qr-app/deliveries/",
        WecomQrAppDeliveriesView.as_view(),
        name="open-wecom-qr-app-deliveries",
    ),
]
