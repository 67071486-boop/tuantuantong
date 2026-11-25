from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0002_upstreamsession'),
    ]

    operations = [
        migrations.CreateModel(
            name='SecurityEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client', models.CharField(max_length=128)),
                ('action', models.CharField(max_length=32)),
                ('result', models.PositiveSmallIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='RateWindow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client', models.CharField(max_length=128)),
                ('minute', models.BigIntegerField()),
                ('count', models.PositiveIntegerField(default=0)),
                ('rejected_logged', models.BooleanField(default=False)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('client', 'minute'), name='sc_rate_unique')],
            },
        ),
    ]
