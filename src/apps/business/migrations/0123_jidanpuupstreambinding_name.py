# ⭐（业务方指令：「绑定鸡蛋铺的时候不能填上家系统名称，需要加上，
# 方便区分绑定了哪些上家鸡蛋铺系统」）：`JidanpuUpstreamBinding` 新增人填的 `name` 列
# （选填、空串默认）。⚠️ 与 `account_name`（同步引擎缓存的只读「上家账号显示名」）是两回事，
# 取舍与只读语义见模型那一列的注释。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0122_dailiantongpush_accept_over_count_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='jidanpuupstreambinding',
            name='name',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='上家系统名称'),
        ),
    ]
