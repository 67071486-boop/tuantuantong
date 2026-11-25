from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("business", "0151_peer_direct_taker"),
    ]

    operations = [
        # 0151 已在本地环境运行过。反向仍保留默认值，与现有 0151 一致。
        migrations.RunSQL(
            """ALTER TABLE "business_peerorderlink" ALTER COLUMN "upstream_internal_order_no" SET DEFAULT '';""",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
