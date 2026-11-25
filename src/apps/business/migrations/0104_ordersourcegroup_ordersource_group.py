# 手写迁移（计划 `docs/plans/2026-10-26-order-source-groups.md` §2）。
#
# 取号纪律（计划 §2 / `AGENTS.md` §5-F）：本检出叶子是 `0101_merge_20261026_1817`；
# `0102` 已被在飞分支 `.worktrees/dlt-push-live` 的
# `0102_dailiantong_zone_map_and_push.py` 占掉，同一分支还在同时生 `0103_...`（未提交）⇒
# 按纪律**顺延到 0104**（没有撞号）。合并回 `main` 若又长出新叶子，用
# `makemigrations business --merge` 收成一个叶子，⛔ 别改名。
#
# 依赖**手写**、只指向本检出当前叶子；本迁移只做两件事：
#   ① 建 `OrderSourceGroup`（名称在租户内唯一，per-schema）；
#   ② 给 `OrderSource` 加可空 `group` 外键（`SET_NULL`：删分组不删来源）。
# ⛔ 不回填数据（存量 `group IS NULL` = 未分组，计划 §2 明写）。
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0101_merge_20261026_1817'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderSourceGroup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=20, verbose_name='分组名称')),
                ('order_by', models.IntegerField(default=0, verbose_name='排序数值')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '公司店铺分组',
                'verbose_name_plural': '公司店铺分组',
                'ordering': ['-order_by', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='ordersourcegroup',
            constraint=models.UniqueConstraint(
                fields=('name',), name='uniq_order_source_group_name',
            ),
        ),
        migrations.AddField(
            model_name='ordersource',
            name='group',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='sources',
                to='business.ordersourcegroup',
                verbose_name='公司店铺分组',
            ),
        ),
    ]
