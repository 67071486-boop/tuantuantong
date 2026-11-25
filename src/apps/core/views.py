from django.http import JsonResponse

from apps.core.envelope import error_response, rendered_response


def health(request):
    """存活探针，供负载均衡与部署脚本使用。注意：
    本视图自身不查数据库，但它**排在 `TenantMainMiddleware` 之后**——该中间件对每个请求都会查 `
    platform.Domain`，所以经 HTTP 访问 `/healthz`仍然依赖数据库，且探针宿主必须有对应的 `Domain` 行。真正「不依赖数据库」的探针需要绕过租户解析，见「已知边界」。"""
    return JsonResponse({"status": "ok"})


def csrf_failure(request, reason=""):
    """`CSRF_FAILURE_VIEW`（settings 里绑定）：中间件层的 CSRF 失败也回统一信封。**为什么必须存在**：`CsrfViewMiddleware` 拒绝请求时走 `_get_failure_view()`（`django/middleware/csrf.py:52,210`），默认实现 `django.views.csrf.csrf_failure` 渲染 **HTML**（`django/views/csrf.py`）；而登录视图显式加了 `csrf_protect` 后，「不带 token 的裸 POST」会命中这条路——不换掉它，同一个 403 会有两套契约（中间件层是 HTML、`SessionAuthentication.enforce_csrf` 抛出的是统一信封），前端没法用同一段代码处理（spec §4.4 把 CSRF 失败的表现也写进了契约）。
    **信封来源必须唯一**：本视图是 Django 层视图、不经 DRF 请求周期，自己 `JsonResponse` 拼信封会**逐字节**背离 DRF 渲染（`JsonResponse` 走 `json.dumps` 默认参数带空格，DRF 的 `COMPACT_JSON` 是 `SHORT_SEPARATORS`），将来换信封也静默漏掉这里。现在一律 `rendered_response(error_response(...))`：信封只有 `envelope.py` 一个来源，渲染三件套也只有 `rendered_response` 一份（与守卫中间件共用）。
    `reason` 是 Django 传入的诊断串（如 "CSRF cookie not set."），**故意不回显**：spec §4.2 要求 403 的 msg 可辨即可，不必向调用方交代内部判定细节。
    """
    return rendered_response(error_response(403, "CSRF 校验失败", 403))