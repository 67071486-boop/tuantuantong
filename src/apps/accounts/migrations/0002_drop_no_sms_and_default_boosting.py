"""删掉 `StaffProfile.no_sms_on_intake`，并把 `business_type` 的默认值改成「专做代练」。

两件事同一次迁移（业务方 2026-09-22 一次会话内一起裁定，见 D-0086）：

1. **`RemoveField no_sms_on_intake`**：业务方原话「录的单不发短信，这个去掉，我们不需要短信功能」，
   并在「删到哪一步」的三选一里选了**彻底删**（前端格子 + 后端字段 + 数据库列）。
   ⚠️ 这一列**全仓没有读点**（唯一的读点是它自己的 API 回显），所以丢值不影响任何行为；
   但 `DROP COLUMN` **不可逆**，回滚只能把列加回来、值回不来。
2. **`AlterField business_type`**：默认值 `0 BOTH`（代练+陪玩）→ `1 BOOSTING`（专做代练）。
   ⚠️ **`default` 只影响「请求完全不带这个键」的新行**：存量行一律不动，显式发
   `business_type: 0` 仍然合法（「代练+陪玩」没有被禁掉）。
   ⚠️ 这条 `AlterField` **刻意不带数据迁移**：把存量行从 0 改成 1 会**改掉用户原本填的业务类型**，
   那不是本次裁定的意思（裁定的是「新账号默认哪一项」）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="staffprofile",
            name="no_sms_on_intake",
        ),
        migrations.AlterField(
            model_name="staffprofile",
            name="business_type",
            field=models.SmallIntegerField(
                choices=[(0, "代练+陪玩"), (1, "专做代练"), (2, "专做陪玩")],
                default=1,
                verbose_name="业务类型",
            ),
        ),
    ]
