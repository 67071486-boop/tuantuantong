from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_staffprofile_opened_by_selfregistrationconfig_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='selfregistrationconfig',
            name='number_prefix',
            field=models.CharField(blank=True, default='', max_length=5, verbose_name='编号前缀'),
        ),
        migrations.AddField(
            model_name='selfregistrationconfig',
            name='number_start',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='起始编号'),
        ),
    ]
