from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0156_jidanpu_upstream_my_orders_progress"),
    ]

    operations = [
        migrations.AddField(
            model_name="jidanpuupstreamorderlink",
            name="last_partial_probe_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="上次未齐列表的单号核对时刻"),
        ),
    ]
