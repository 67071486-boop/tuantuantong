from uuid import uuid4

from django.conf import settings
from django.db import models


class FinanceExportJob(models.Model):
    class Kind(models.TextChoices):
        ORDERS = "orders", "订单"
        SETTLEMENTS = "settlements", "结算"

    class Status(models.TextChoices):
        PENDING = "pending", "排队中"
        RUNNING = "running", "生成中"
        READY = "ready", "可下载"
        FAILED = "failed", "失败"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    filters = models.JSONField(default=dict)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    authorization = models.CharField(max_length=64)
    row_scope = models.JSONField(null=True, editable=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_until = models.DateTimeField(null=True)
    ready_at = models.DateTimeField(null=True)
    size = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"], name="finance_export_poll_idx"),
            models.Index(fields=["requester", "kind", "-created_at"], name="finance_export_owner_idx"),
        ]
