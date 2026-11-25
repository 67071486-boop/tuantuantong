"""微信公众号集成的 API 路由（挂载点：`config/urls.py` 的 `api/wechatmp/`）。

⚠️ 回调路径**不带尾斜杠**（`callback`）：计划与微信公众平台里填的 URL 逐字一致
（`/api/wechatmp/callback`）；其余端点带尾斜杠（DRF 侧惯例）。
"""
from django.urls import path

from apps.wechatmp.bind_views import (
    WechatMpBindCallbackView,
    WechatMpBindEntryView,
)
from apps.wechatmp.views import (
    WechatMpCallbackView,
    WechatMpConfigView,
    WechatMpMenuSyncView,
    WechatMpMenuView,
)

urlpatterns = [
    # 设置：读 / 保存（appsecret/token/aes_key 留空 = 保持不变，S4）。
    path("config/", WechatMpConfigView.as_view(), name="wechatmp-config"),
    # 菜单：同步预设 / 查看 / 删除。
    path("menu/sync/", WechatMpMenuSyncView.as_view(), name="wechatmp-menu-sync"),
    path("menu/", WechatMpMenuView.as_view(), name="wechatmp-menu"),
    # 微信服务器回调（匿名；微信后台填的就是这条不带尾斜杠的路径）。
    path("callback", WechatMpCallbackView.as_view(), name="wechatmp-callback"),
    # OAuth 绑定入口 / 回跳（T4；确认页在 page_urls.py）。
    path("bind-entry", WechatMpBindEntryView.as_view(), name="wechatmp-bind-entry"),
    path("bind-callback", WechatMpBindCallbackView.as_view(), name="wechatmp-bind-callback"),
]
