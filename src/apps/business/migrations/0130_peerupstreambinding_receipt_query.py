from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0129_order_receipt_sync_notfound_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='peerupstreambinding',
            name='receipt_query_password',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='收款查询密码'),
        ),
        migrations.AddField(
            model_name='peerupstreambinding',
            name='receipt_query_password_set',
            field=models.BooleanField(default=False, verbose_name='已配收款查询密码'),
        ),
        migrations.AddField(
            model_name='peerupstreambinding',
            name='receipt_query_precheck_ok',
            field=models.BooleanField(default=False, verbose_name='收款查询预检通过'),
        ),
    ]
