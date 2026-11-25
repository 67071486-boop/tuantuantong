from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("business", "0137_order_split_kind")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="split_ship_main_order_no",
            field=models.CharField(
                "拆单发货主订单号", max_length=20, null=True, blank=True, editable=False
            ),
        ),
    ]
