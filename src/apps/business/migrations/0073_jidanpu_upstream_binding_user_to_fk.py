"""鸡蛋铺上家绑定的 `user` 列：`OneToOneField` → `ForeignKey`（D-0256 / E-0255）。

⛔ **改准（D-0256）**：绑定从「按登录用户单行」改为**租户多行**资源
（业务方报障「只能绑两个、新增就挤掉一个」，实锤见 E-0255）。原 `OneToOneField`
带着库里 `user_id` 的唯一约束，物理挡住「同一客服绑第二个上家」⇒ 本迁移把它改回
`ForeignKey`（`on_delete` / `related_name` 不变）。

⚠️ **删约束的加性变更**：存量行原样保留（无回填、无数据改写）；发版时
`deploy/activate.sh` 的 `migrate_schemas`（activate.sh:150，不带 `--tenant`、shared+tenant 都跑）会自动应用。`user` 列语义从
「这行属于谁」改准为「**录入人**」（谁建的这一行）。
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0072_upstream_price_change_log'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='jidanpuupstreambinding',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='jidanpu_upstream_binding', to=settings.AUTH_USER_MODEL),
        ),
    ]
# ⚠️ **反向迁移不成立**：若历史上出现过同一 user 的重复行，FK→OneToOne 会因唯一约束失败 ⇒ 本迁移视为单向（删约束的加性变更，与 D-0256 多行口径一致）。
