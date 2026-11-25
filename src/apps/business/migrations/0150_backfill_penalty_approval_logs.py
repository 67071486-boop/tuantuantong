from django.db import migrations
from django.db.models import F


def backfill_penalty_approval_logs(apps, schema_editor):
    """Only restore events whose linked order, actors and times were persisted."""
    Approval = apps.get_model("business", "Approval")
    OperationLog = apps.get_model("audit", "OperationLog")
    alias = schema_editor.connection.alias
    approvals = (
        Approval.objects.using(alias)
        .filter(
            kind="penalty", status="approved", generated_order__isnull=False,
            initiator__isnull=False, approver__isnull=False, decided_at__gte=F("created_at"),
        )
        .select_related("initiator", "approver", "generated_order")
        .only(
            "created_at", "decided_at",
            "initiator__username", "initiator__first_name",
            "approver__username", "approver__first_name",
            "generated_order__order_no", "generated_order__source_order_no",
        )
        .order_by("pk")
        .select_for_update(of=("self",))
    )
    logs = OperationLog.objects.using(alias)
    for approval in approvals.iterator(chunk_size=500):
        order = approval.generated_order
        display_no = order.source_order_no.strip() or order.order_no
        events = (
            ("order.penalty_submit", approval.initiator, approval.created_at, "发起罚款审批"),
            ("order.penalty_approve", approval.approver, approval.decided_at, "通过罚款审批"),
        )
        for action, actor, occurred_at, message in events:
            if logs.filter(order_no=order.order_no, action=action).exists():
                continue
            actor_name = actor.first_name or actor.username
            logs.create(
                log_type=10, action=action, actor=actor.username, actor_id=actor.pk,
                actor_name=actor_name, target_kind="order", target_id=order.order_no,
                order_no=order.order_no, source_order_no=order.source_order_no,
                created_at=occurred_at,
                summary=f"【{actor_name}】{message}（订单 {display_no}）"[:255],
            )


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0008_merge_0007_operationlog_actor_name_0007_requestmetric"),
        ("business", "0149_approval_center"),
    ]

    operations = [migrations.RunPython(backfill_penalty_approval_logs, migrations.RunPython.noop)]
