from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='UpstreamSession',
            fields=[
                ('key', models.CharField(default='agent', max_length=16, primary_key=True, serialize=False)),
                ('token', models.BinaryField(null=True)),
                ('attempted_at', models.DateTimeField(null=True)),
                ('token_created_at', models.DateTimeField(null=True)),
            ],
        ),
    ]
