from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("internal_chat", "0014_visitor_change"),
    ]

    operations = [
        migrations.AddField(
            model_name="visitorreceptionsettings",
            name="greetings",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
