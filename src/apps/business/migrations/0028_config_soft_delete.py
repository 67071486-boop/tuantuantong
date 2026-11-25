"""配置三表（游戏 / 项目分类 / 代练项目）加「删除时间」列 —— **软删除**（业务方裁定，D-0085）。

**改动只有一件事**：三个模型各加一列 `deleted_at`（可空）。语义：

- 置上时间戳 = 「已删除」：行**留在库里**，历史订单上的 `Order.game` / `Order.category` /
  `Order.project`（都是 `on_delete=PROTECT`）原样有效；录单与各页下拉不再出现它；
- 置回 `NULL` = 恢复（`PATCH /api/business/<资源>/<id>/ {"deleted_at": null}`）。

⚠️ **为什么不是真删**：真删会撞上 PROTECT（被订单引用就删不掉，这正是业务方要改掉的那个
「不能删除」），绕过 PROTECT 就只能连带删/置空历史订单上的引用 ⇒ **对不上账**。
软删除把「从选择项里去掉」与「历史数据留着」这两件事分开，代价见 spec §5。

⚠️ **唯一约束刻意不动**：`uniq_game_name` / `uniq_project_category_name` / `uniq_project_name`
仍是全局唯一（**含已删除的行**）⇒ 删掉「原神」之后再建一个同名的会被唯一约束挡住（业务码 1000）。
要拿回「原神」请走**恢复**，不要指望重建同名的。这是数据库层的既有约束，本迁移不改。

⚠️ **本迁移只加列、不改数据**：既有的行 `deleted_at` 全是 `NULL`（= 都没被删），所以升级是
纯增量、无回填、无破坏性变更（升级要跑 `migrate_schemas --tenant`）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0027_xgjshop_per_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="game",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="删除时间"),
        ),
        migrations.AddField(
            model_name="projectcategory",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="删除时间"),
        ),
        migrations.AddField(
            model_name="project",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="删除时间"),
        ),
    ]
