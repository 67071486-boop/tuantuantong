from django.db import migrations


NODES = (
    (
        "home.approval_penalty_approve", "审批罚款",
        "可以处理指派给本人的罚款审批", "罚款审批按指派处理",
    ),
    (
        "home.approval_refund_handle", "处理退款审批",
        "可以处理指派给本人的平台退款待办", "平台退款待办按指派处理",
    ),
)


def retire_nodes(apps, schema_editor):
    node = apps.get_model("rbac", "PermissionNode")
    for code, name, _, retired_note in NODES:
        node.objects.filter(code=code).update(
            name=f"{name}（旧权限已停用）", note=retired_note, available=False,
        )


def restore_nodes(apps, schema_editor):
    node = apps.get_model("rbac", "PermissionNode")
    for code, name, note, _ in NODES:
        node.objects.filter(code=code).update(name=name, note=note, available=True)


class Migration(migrations.Migration):
    dependencies = [("rbac", "0023_approval_center_nodes")]
    operations = [migrations.RunPython(retire_nodes, restore_nodes)]
