from django.urls import include, path

from apps.core.views import health

urlpatterns = [
    path("healthz", health, name="health"),
    path("api/business/", include("apps.business.urls")),
    path("api/audit/", include("apps.audit.urls")),
    path("api/internal-chat/", include("apps.internal_chat.urls")),
    # 开放平台对外端点（spec D2）。⚠️ 必须排在 `path("", include("apps.core.urls"))` 之前：
    # core 那份 urlconf 里有几条 `path("api/…")`（`api/auth/*` 等）。今天两边路径段不重叠、顺序不构成判据，
    # 但写在前面才符合「更具体的前缀优先」的读法，将来 core 加更宽的 `api/` 路由也不会截走本线。
    path("api/open/", include("apps.openapi.urls")),
    path("api/integration/v1/", include("apps.openapi.integration_urls")),
    # 打手侧开放接口（spec `docs/specs/2026-10-08-taker-open-api-design.md` §2）。与 `api/open/` 同一条口径：
    # 必须排在 core include 之前（本文件 `api/` 前缀的排序纪律，见上）。⛔ `api/taker/` 与 `api/open/` 是两条通道、不要合并：
    # 「打手令牌打不开上家侧端点」是结构性的（各自认证类只查自己那张表），见 `apps/takerapi/models.py` 的 `TakerApp` docstring ①。
    path("api/taker/", include("apps.takerapi.urls")),
    # 实名认证租户侧端点（`POST /api/auth/realname/verify/`、`GET /api/auth/realname/`；spec
    # `docs/specs/2026-10-10-realname-verification-design.md` §9.1 前两条）。必须排在 core include 之前（排序纪律见上）。
    # ⚠️ 两条端点都在租户面：视图体先过平台平面守卫，public 平面回 404（表只在租户 schema，不守就是 500）。
    path("api/auth/realname/", include("apps.realname.urls")),
    # 微信公众号集成端点（D-0268；回调/设置/菜单/OAuth 绑定）。必须排在 core include 之前（排序纪律见上）。
    # ⚠️ 回调那条逐视图匿名（`authentication_classes=[]`+`AllowAny`，先例 `xgj_push_views.py`），但仍在租户面：
    # public 平面请求在视图体里回 404（表只在租户 schema，不守就是 500）。
    path("api/wechatmp/", include("apps.wechatmp.urls")),
    # 企微自建应用端点（D-0379；设置/自检/只读 proxy/匿名回调）。必须排在 core include 之前（排序纪律见上）。
    # ⚠️ 回调那条逐视图匿名（照 wechatmp 先例），但仍在租户面：public 平面请求在视图体里回 404。
    path("api/wecom/", include("apps.wecom.urls")),
    # 绑定确认页（`/wechatmp/bind-confirm`，**独立服务端渲染页**，不在 m/ SPA 内 —— 计划
    # T4 / H3：m 端有「只服务打手」身份门，客服走不进去 ⇒ 绑定页自成一路，自带登录表单）。
    # ⚠️ 它在租户面（域名路由进租户 schema 才有 `WechatMpBinding` 表）；视图体里做平面守卫。
    path("wechatmp/", include("apps.wechatmp.page_urls")),
    # P5 认证端点（`/api/auth/{login,logout,me,csrf}`）。
    # 放在 `api/business/` **之后**不影响匹配：四条路径的前缀是 `api/auth/`，
    # 与业务前缀无重叠。

    # QQ 机器人设置页端点（`feat/qqbot-line`）。必须排在 core include 之前（本文件 `api/` 前缀的排序纪律，
    # 见上方 `api/open/` 那条注释）：今天 core 里没有 `api/qqbot/` 前缀的路由（顺序不构成判据），
    # 写在前面是为了将来 core 加宽路由时不会截走本线。
    path("api/qqbot/", include("apps.qqbot.urls")),
    # 工作室频道公开读端点（`GET /api/channel/{profile,articles,articles/<id>}/`；匿名可读、只回已发布）。
    # 必须排在 core include 之前（排序纪律见上）。⚠️ 本模块在租户面：视图体里做平面守卫
    # （`public` 平面回 404，本 app 的表只在租户 schema 里）。
    path("api/channel/", include("apps.business.channel_urls")),
    path("", include("apps.core.urls")),
]
