from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0003_order"),
    ]

    operations = [
        # 序列建在租户 schema 内：django-tenants 跑迁移时会切 search_path，
        # 因此每个租户 schema 各有一份自己的序列，天然隔离且 nextval 原子。
        migrations.RunSQL(
            sql="CREATE SEQUENCE IF NOT EXISTS order_no_counter_seq;",
            reverse_sql="DROP SEQUENCE IF EXISTS order_no_counter_seq;",
        ),
    ]
