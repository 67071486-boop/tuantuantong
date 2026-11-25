from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0005_authorization_unknown_action'),
    ]

    operations = [
        migrations.AddField(
            model_name='authorization',
            name='review_code',
            field=models.CharField(blank=True, max_length=32),
        ),
    ]
