import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0041_wxpusher_binding_prompt")]

    operations = [
        migrations.AddField(
            model_name="wxpusherbinding", name="last_link_start_at", field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="wxpusherbinding", name="last_link_code_at", field=models.DateTimeField(null=True),
        ),
        migrations.CreateModel(
            name="WxPusherLinkRecipientThrottle",
            fields=[
                ("digest", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("blocked_until", models.DateTimeField(db_index=True)),
            ],
        ),
        migrations.CreateModel(
            name="WxPusherLinkAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("challenge", models.CharField(max_length=32, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("verified_uid", models.CharField(blank=True, default="", max_length=128)),
                ("checked_at", models.DateTimeField(null=True)),
                ("code_uid", models.CharField(blank=True, default="", max_length=128)),
                ("code_digest", models.CharField(blank=True, default="", max_length=64)),
                ("code_expires_at", models.DateTimeField(null=True)),
                ("code_failures", models.PositiveSmallIntegerField(default=0)),
                ("binding", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to="accounts.wxpusherbinding")),
            ],
        ),
    ]
