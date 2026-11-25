from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('settlement_control', '0006_authorization_review_code'),
    ]

    operations = [
        migrations.CreateModel(
            name='ManualReview',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client', models.CharField(max_length=128)),
                ('request_id', models.UUIDField()),
                ('operator', models.CharField(max_length=128)),
                ('process_user', models.CharField(max_length=128)),
                ('decision', models.CharField(max_length=32)),
                ('evidence_ciphertext', models.BinaryField()),
                ('evidence_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
