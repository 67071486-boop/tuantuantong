from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0155_retire_rebound_upstream_order_links"),
    ]

    operations = [
        migrations.AddField(
            model_name="jidanpuupstreambinding",
            name="my_orders_progress",
            field=models.JSONField(blank=True, null=True, verbose_name="指派订单补拉进度"),
        ),
    ]
