from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0003_securityevent_ratewindow'),
    ]

    operations = [
        migrations.AddField(
            model_name='authorization',
            name='next_action',
            field=models.CharField(default='agreement', max_length=16),
        ),
    ]
