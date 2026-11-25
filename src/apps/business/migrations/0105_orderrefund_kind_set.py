"""`OrderRefund.kind` 追加 `SET`（设置累计退款额）。

⭐（`feat/refund-set`，D-0312）：部分退款入口从「本次退款额（累加到
`Order.refund_fee`）」改成「**累计退款总额**（绝对值）」⇒ 台账那一行记的是
**带符号的差额** `total − 旧累计`，`kind` 需要一个新取值表达「这是一次设置动作」。

⚠️ **手写编号**（计划 §2-9）：从 `main` 切支时 `makemigrations` 只会产 `0102`
（在飞分支同号、`0103` 不存在）⇒ 本迁移直接取 `0104`，依赖最新的叶子
`0101_merge_20261026_1817`；合并回 `main` 时用 `makemigrations business --merge`
收叶子（⛔ 不改名）。
⚠️ 只改 `choices`（Django 的 choices 变更不触碰数据库结构），`refund_fee` 列一字不动。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("business", "0104_ordersourcegroup_ordersource_group")]

    operations = [
        migrations.AlterField(
            model_name="orderrefund",
            name="kind",
            field=models.CharField(
                choices=[
                    ("PARTIAL", "部分退款"),
                    ("FULL", "全额退款"),
                    ("REVERSAL", "退款冲销（退单回池）"),
                    ("SET", "设置累计退款额"),
                ],
                max_length=16,
                verbose_name="退款类型",
            ),
        ),
    ]
