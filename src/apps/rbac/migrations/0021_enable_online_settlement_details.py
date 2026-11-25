from django.db import migrations


def enable_node(apps, schema_editor):
    apps.get_model("rbac", "PermissionNode").objects.filter(
        code="finance.online_settlement_details",
    ).update(available=True)


class Migration(migrations.Migration):
    dependencies = [("rbac", "0020_role_dashboard_type")]
    operations = [migrations.RunPython(enable_node, migrations.RunPython.noop)]
