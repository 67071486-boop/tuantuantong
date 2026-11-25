from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('settlement_control', '0007_manualreview')]

    operations = [
        migrations.AddField(
            model_name='upstreamsession',
            name='login_attempt',
            field=models.UUIDField(null=True),
        ),
    ]
