"""绑定确认页的路由（挂载点：`config/urls.py` 的 `wechatmp/`，**在 `api/` 之外**）。

⚠️ 它与 `urls.py` 分成两份是**有意的**：确认页是给微信内置浏览器看的服务端渲染页，
不在 `/api/` 前缀下；两张网（`test_no_route_500.py` 的 `EXPLICIT_ROUTE_SOURCES` 与
`test_operation_log_coverage.py` 的 `COVERAGE_SCOPE` / `NON_ROUTER_MODULES`）两份
urlconf **各自登记**（计划 T7）。
"""
from django.urls import path

from apps.wechatmp.bind_views import WechatMpBindConfirmView

urlpatterns = [
    path("bind-confirm", WechatMpBindConfirmView.as_view(), name="wechatmp-bind-confirm"),
]
