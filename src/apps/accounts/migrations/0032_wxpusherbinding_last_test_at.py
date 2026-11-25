from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0031_wxpusher_login_receipt_acceptance"),
    ]

    operations = [
        migrations.AddField(
            model_name="wxpusherbinding",
            name="last_test_at",
            field=models.DateTimeField(null=True),
        ),
    ]
