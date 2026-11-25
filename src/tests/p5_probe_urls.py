"""测试专用 urlconf：把探针端点与真实路由拼在一起。

用它的测试要 `@override_settings(ROOT_URLCONF="tests.p5_probe_urls")`。
生产路由零污染——本文件不进 `config/urls.py`。

**按任务切分**（预检裁决）：本文件由 Task 2 建立；Task 7 补 `plain-whoami` 与 `write`，
Task 9 补 `service-only` 与 `perm-only`（它们依赖 Task 9 才存在的 `HasRole` / `HasPermission`）；
`node-only`（节点判据载体）由 `rbac-foundation` 分支的 Task 5 补。
"""
from django.urls import include, path

from apps.core.views import health
from tests import p5_probe_views

urlpatterns = [
    path("__probe__/whoami", p5_probe_views.whoami, name="probe-whoami"),
    path("__probe__/plain-whoami", p5_probe_views.plain_whoami, name="probe-plain-whoami"),
    path("__probe__/write", p5_probe_views.write_probe, name="probe-write"),
    path("__probe__/service-only", p5_probe_views.service_only, name="probe-service-only"),
    path("__probe__/perm-only", p5_probe_views.perm_only, name="probe-perm-only"),
    path("__probe__/node-only", p5_probe_views.node_only, name="probe-node-only"),
    path("", include("apps.core.urls")),          # /api/auth/*
    path("healthz", health, name="health"),
]
