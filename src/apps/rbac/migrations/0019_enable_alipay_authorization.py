from django.db import migrations


def enable_node(apps, schema_editor):
    apps.get_model("rbac", "PermissionNode").objects.filter(
        code="finance.alipay_authorization"
    ).update(available=True)


class Migration(migrations.Migration):
    dependencies = [("rbac", "0018_merge_20261111_1457")]
    operations = [migrations.RunPython(enable_node, migrations.RunPython.noop)]
