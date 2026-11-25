"""AppConfig：`apps.wechatmp` 是**租户侧** app（D-0268；计划「落位与迁移（H1 订正）」）。

两张表（`WechatMpConfig` / `WechatMpBinding`）承载**租户业务数据**（谁的公众号、谁绑了
openid）⇒ 与 `apps.business` / `apps.realname` 同口径，**只在 `TENANT_APPS`**：每个租户
schema 各一份，表里不带 `tenant_id` 列（租户上下文由连接决定）。

⚠️ 本 app 的迁移是**独立序列**（`0001_initial` 起），⛔ 不进 business 序列
（0074/0075 已被其它在飞工作区占用，H1 实核；计划「落位与迁移」一节）。
"""
from django.apps import AppConfig


class WechatmpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.wechatmp"
    label = "wechatmp"
    verbose_name = "微信公众号"
