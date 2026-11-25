from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("internal_chat", "0016_visitor_service_seats")]

    operations = [
        migrations.CreateModel(
            name="SystemNoticeSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("approval_submitted", models.BooleanField(default=True)),
                ("approval_approved", models.BooleanField(default=True)),
                ("wecom_aftersales", models.BooleanField(default=True)),
                ("order_grab_qr", models.BooleanField(default=False)),
                ("wecom_aftersales_resumed_at", models.DateTimeField(blank=True, null=True)),
                ("order_grab_qr_resumed_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
