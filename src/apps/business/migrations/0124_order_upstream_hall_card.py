# ⭐（D-0325，业务方拍板「上家镜像卡也要抛给打手的下家鸡蛋铺」）：
# `Order` 新增 `upstream_hall_card`（OneToOne → `JidanpuUpstreamHallOrder`，可空）——
# 上家大厅镜像卡挂厅期间派生的**隐藏单**载体，复用 `queue_hall_launch` 等既有引擎把卡抛给
# 下家鸡蛋铺。与 peer 线的 `hall_card` 并列且互斥（两条线零关系）；读面排除口径同批翻倍
# （`tests/test_order_read_path_net.py` 的登记表）。生命周期唯一入口：`upstream_hall_shadow`。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0123_jidanpuupstreambinding_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='upstream_hall_card',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='shadow_order',
                to='business.jidanpuupstreamhallorder',
                verbose_name='来源的上家鸡蛋铺镜像卡（隐藏单专用）',
            ),
        ),
    ]
