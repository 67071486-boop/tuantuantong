from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Authorization',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client', models.CharField(max_length=128)),
                ('request_id', models.UUIDField()),
                ('payload_hash', models.CharField(max_length=64)),
                ('ciphertext', models.BinaryField()),
                ('app_id', models.CharField(blank=True, max_length=128)),
                ('app_name', models.CharField(max_length=128)),
                ('phase', models.CharField(default='new', max_length=32)),
                ('status', models.CharField(default='pending', max_length=16)),
                ('configured', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('client', 'request_id'), name='sc_auth_unique')],
            },
        ),
        migrations.CreateModel(
            name='Nonce',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('client', models.CharField(max_length=128)),
                ('nonce', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('client', 'nonce'), name='sc_nonce_unique')],
            },
        ),
    ]
