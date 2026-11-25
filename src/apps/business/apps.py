from django.apps import AppConfig


class BusinessConfig(AppConfig):
    name = "apps.business"
    label = "business"
    verbose_name = "租户业务"

    def ready(self):
        # ⚠️ 必须在 ready() 里导入：`performance.py` 在**模块导入时**注册迁移副作用钩子
        # （spec §4.1.2）。不在这里保证导入，就会出现「某些路径没注册钩子」的静默漏副作用。
        from apps.business import performance  # noqa: F401
        # 站内通知（B2-1）：`notifications.py` 在**模块导入时**注册 target hook 与
        # `Performance` 的两个信号接收器。漏了这行 = 静默不注册（钩子/信号都不生效）。
        from apps.business import notifications  # noqa: F401
        # 结算副作用（P4-6）：`settlement_hooks.py` 在**模块导入时**注册 `COMPLETED` 钩子
        # （确定客服销售提成）。同样：漏了这行 = 静默不注册。
        from apps.business import settlement_hooks  # noqa: F401
