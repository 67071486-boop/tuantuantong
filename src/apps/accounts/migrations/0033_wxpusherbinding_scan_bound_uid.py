from django.db import migrations, models
from django.db.models.functions import Coalesce


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0032_wxpusherbinding_last_test_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="wxpusherbinding",
            name="scan_bound_uid",
            field=models.CharField(max_length=128, null=True),
        ),
        migrations.AddConstraint(
            model_name="wxpusherbinding",
            constraint=models.UniqueConstraint(
                models.F("app_id"), Coalesce("uid", "scan_bound_uid"), name="wxpusher_binding_effective_uid",
            ),
        ),
        migrations.AddConstraint(
            model_name="wxpusherbinding",
            constraint=models.CheckConstraint(
                condition=models.Q(uid__isnull=True) | models.Q(scan_bound_uid__isnull=True),
                name="wxpusher_binding_one_uid_source",
            ),
        ),
    ]
