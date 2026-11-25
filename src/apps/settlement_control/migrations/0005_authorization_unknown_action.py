from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0004_authorization_next_action'),
    ]

    operations = [
        migrations.AddField(
            model_name='authorization',
            name='unknown_action',
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
