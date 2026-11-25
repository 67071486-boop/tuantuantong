from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0160_merge_order_finance_memory"),
        ("internal_chat", "0017_system_notice_settings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderGrabQrNotice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("claimed_at", models.DateTimeField()),
                ("next_attempt_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="business.order")),
                ("worker", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(
                    fields=("order", "worker", "claimed_at"), name="chat_grab_qr_notice_once",
                )],
                "indexes": [models.Index(
                    fields=["next_attempt_at", "id"], condition=models.Q(finished_at__isnull=True),
                    name="chat_grab_qr_notice_due",
                )],
            },
        ),
    ]
