"""AppConfig：`apps.realname` 是**租户侧** app（设计基线 §4.2 / 计划 T3）。

⚠️ 本 app 目前**没有模型**（T3 只交付厂商客户端 `vendor.py`）⇒ 这个 AppConfig 不产生任何表；
它进 `TENANT_APPS` 是因为客户端**无状态、只被租户侧的核验视图调用**（⚠️ 注册在 `config/settings.py`，
那是 T6 的事，⛔ T3 不动 `settings.py`）。
⚠️ 同族的"没有模型"先例是 `apps.callback`（它的 AppConfig docstring 里写了同一件事）。
"""
from django.apps import AppConfig


class RealnameConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.realname"
    label = "realname"
    verbose_name = "实名认证"
