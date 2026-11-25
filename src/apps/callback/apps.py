"""AppConfig：把 `apps.callback` 挂进 `SHARED_APPS`（只为让 `manage.py callback_events` 能被发现）。

⚠️ **为什么进 `SHARED_APPS` 而不是 `TENANT_APPS`**：接收器收到的回调**没有租户归属**
（第一版连业务字段都不解析）⇒ 它是**平台面**的东西，与任何租户 schema 无关。
⚠️ 本 app **没有模型** ⇒ 这个选择**不产生任何表**、也不影响 `migrate_schemas`；
`tests/test_p2_isolation.py` 那张「租户专属表不得出现在 public」的枚举**有意不收录本 app**
（它逐个 label 要求 `get_models()` 非空，而本 app 按设计一个模型都没有）。
"""
from django.apps import AppConfig


class CallbackConfig(AppConfig):
    name = "apps.callback"
    verbose_name = "鸡蛋铺回调接收器"
