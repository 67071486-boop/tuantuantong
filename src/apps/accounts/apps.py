from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "租户内账号"

    def ready(self):
        # ⚠️ 必须在 ready() 里导入：`payout.py` 在**模块导入时**注册
        # `PerformanceAdjustment` 的 `post_save` 接收器（已结账绩效的调整冲账，
        # `feat/wallet-single-payout`）。漏了这行 = 静默不注册
        # ⇒ 退款 / 手工调整不会冲钱包（钱多付出去、没有任何报错）。
        from apps.accounts import payout  # noqa: F401

