from django.db import migrations, models


def initialize_builtin_layouts(apps, schema_editor):
    roles = apps.get_model("rbac", "Role").objects
    roles.filter(name="代练员").update(dashboard_type="worker")
    roles.filter(name__in=("客服", "客服主管")).update(dashboard_type="work")


class Migration(migrations.Migration):
    dependencies = [("rbac", "0019_enable_alipay_authorization")]
    operations = [
        migrations.AddField(
            model_name="role", name="dashboard_type",
            field=models.CharField(blank=True, choices=[("", "默认"), ("worker", "打手"), ("work", "工作")], default="", max_length=16, verbose_name="首页看板"),
        ),
        migrations.RunPython(initialize_builtin_layouts, migrations.RunPython.noop),
    ]
