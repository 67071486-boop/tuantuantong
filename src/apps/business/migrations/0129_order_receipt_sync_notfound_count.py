from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0128_order_qr_fetch_suppress_until'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='receipt_sync_notfound_count',
            field=models.IntegerField(default=0, verbose_name='上家收款查无计数'),
        ),
    ]
