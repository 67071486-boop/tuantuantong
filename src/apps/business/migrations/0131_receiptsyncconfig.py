from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0130_peerupstreambinding_receipt_query'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReceiptSyncConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tenant_schema', models.CharField(max_length=64, unique=True, verbose_name='租户标识')),
                ('enabled', models.BooleanField(default=True, verbose_name='启用上家收款同步')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '上家收款同步配置',
                'verbose_name_plural': '上家收款同步配置',
            },
        ),
    ]
