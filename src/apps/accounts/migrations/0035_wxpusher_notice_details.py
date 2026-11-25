from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0034_wxpusher_preferences_and_notices"),
    ]

    operations = [
        migrations.AddField(
            model_name="wxpushernotice",
            name="details",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
