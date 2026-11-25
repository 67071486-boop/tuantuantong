from django.db import models


class Resource(models.TextChoices):
    ORDER = "order", "订单"
    TAKER = "taker", "打手"


class Action(models.TextChoices):
    CREATED = "created", "创建"
    UPDATED = "updated", "更新"
    DELETED = "deleted", "删除"
    RESTORED = "restored", "恢复"


class BusinessChange(models.Model):
    resource = models.CharField(max_length=8, choices=Resource.choices)
    resource_id = models.CharField(max_length=32)
    action = models.CharField(max_length=8, choices=Action.choices)
    occurred_at = models.DateTimeField()
    published_seq = models.PositiveBigIntegerField(null=True, unique=True)
    published_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["id"],
                condition=models.Q(published_seq__isnull=True),
                name="integration_unpublished_idx",
            ),
        ]


class ChangeCheckpoint(models.Model):
    # A tenant-scoped singleton serializes publication and retention.
    published_through = models.PositiveBigIntegerField(default=0)
    expired_through = models.PositiveBigIntegerField(default=0)
