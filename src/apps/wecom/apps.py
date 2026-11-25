"""企微业务数据属于租户，迁移使用独立序列。"""
from django.apps import AppConfig


class WecomConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.wecom"
    label = "wecom"
    verbose_name = "企微自建应用"
