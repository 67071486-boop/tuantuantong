"""鸡蛋铺上家绑定挂「订单来源」字典项（D-0261）。

上家来的单此前两条落池路（指派录池 `record_assigned_order` / 镜像转正
`promote_upstream_mirror`）建出的我方订单**都没有来源** ⇒ 订单页「来源」列一直空。
本迁移给 `JidanpuUpstreamBinding` 加**可空**的 `order_source` 外键：每条绑定配一个
订单来源字典项，两条路建单时自动带出来源与名字快照（写点实现见 `jidanpu_upstream.py`）。

⚠️ **fail-soft 语义（⛔ 不是「必须配」）**：未配置 ⇒ 来的单照旧无来源，不挡接单；
字典项被删 ⇒ `SET_NULL`——绑定行后续单无来源，**已有**单不受影响
（`Order.source` 的名字快照留下，快照列就是为这一刻存在的）。
⚠️ **加列的加性变更**：全部存量行落 `NULL`（无回填、无数据改写）；发版时
`deploy/activate.sh` 的 `migrate_schemas` 会自动应用。
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0073_jidanpu_upstream_binding_user_to_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='jidanpuupstreambinding',
            name='order_source',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='business.ordersource', verbose_name='订单来源'),
        ),
    ]
