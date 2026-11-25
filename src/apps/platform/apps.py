"""`apps/platform` 的 AppConfig。

⚠️ **平台控制台的 admin AppConfig 刻意不放在本模块**，在 `apps/platform/admin_config.py`：
Django 处理 `INSTALLED_APPS` 里的 `"apps.platform"` 时会扫**本模块**里所有
`AppConfig` 的候选类（`django/apps/config.py::AppConfig.create`），多于一个就按
`default = True` 兜底挑一个；把 `django.contrib.admin` 的 config（`AdminConfig`，它
`default = True`）import 进来会**污染这次扫描**——实测报
`RuntimeError: 'apps.platform.apps' declares more than one default AppConfig`，
而且就算压住报错，被挑中的也会是**名字不对的那个 config**（它属于 app `admin`）。
"""
from django.apps import AppConfig


class PlatformConfig(AppConfig):
    name = "apps.platform"
    label = "platform"
    verbose_name = "平台"
