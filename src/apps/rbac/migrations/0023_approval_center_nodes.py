from django.db import migrations

NODES = (
    ("home.approval_apply", "发起罚款审批", "可以从审批中心或管理订单发起罚款审批", "/api/business/approvals/"),
    ("home.approval_penalty_approve", "审批罚款", "可以处理指派给本人的罚款审批", "/api/business/approvals/"),
    ("home.approval_refund_handle", "处理退款审批", "可以处理指派给本人的平台退款待办", "/api/business/approvals/"),
    ("home.approval_configure", "配置审批中心", "可以配置罚款审批人、抄送人和退款处理人", "/api/business/approvals/settings/"),
)

def add_approval_nodes(apps, schema_editor):
    Node = apps.get_model("rbac", "PermissionNode")
    Role = apps.get_model("rbac", "Role")
    parent = Node.objects.filter(code="home").first()
    for code, name, note, url in NODES:
        node, created = Node.objects.get_or_create(
            code=code, defaults={
                "name": name, "note": note, "url": url, "kind": "api",
                "level": 1, "parent": parent, "available": True,
            },
        )
        if created and code == "home.approval_apply":
            for role in Role.objects.filter(name__in=("客服", "客服主管")):
                role.permissions.add(node)


class Migration(migrations.Migration):
    dependencies = [("rbac", "0022_keep_defaults_for_rollback")]
    operations = [migrations.RunPython(add_approval_nodes, migrations.RunPython.noop)]
