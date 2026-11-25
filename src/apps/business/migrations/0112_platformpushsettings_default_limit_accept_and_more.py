"""一键抛单 B2/B4：

- `PlatformPushSettings` 新增三列（门槛默认值两份 + 代练要求模板）；
- `PlatformAutoReplyRule` 加 `(platform, game, trigger)` 唯一约束（含 `game IS NULL`、
  含停用的；`nulls_distinct=False` = PG 15+ 的 `NULLS NOT DISTINCT`）；
- `DailiantongPush` 的六个 QR 列只改 **verbose_name**（B1 把入口合并成「接手后发送」），
  没有任何数据结构变化。

## ⭐ 唯一约束上线前**先查重、有重复就响亮失败**（计划 §2.2）

数据库层加唯一约束时，既有重复数据会让迁移在 DDL 处直接报错，错误信息里只有约束名 ——
对自己库里有哪几条重复、该怎么清，一句都看不出来。所以这里先查一遍，命中就带
「哪一组 + 有几条 + 怎么清」抛错（整个迁移回滚，⛔ 不会留下半套结构）。

⚠️ 生产当前 0 条规则（计划 §2.2），这条路径正常情况下不会触发；它是给「已经配过重复」
的租户库准备的。
"""
from django.db import migrations, models
from django.db.models import Count


def _assert_no_duplicate_rule_combos(apps, schema_editor):
    """同一 `(platform, game, trigger)` 出现多条 ⇒ **响亮失败**并给清理方法。"""
    PlatformAutoReplyRule = apps.get_model("business", "PlatformAutoReplyRule")
    duplicates = (
        PlatformAutoReplyRule.objects.values("platform", "game_id", "trigger")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
        .order_by("platform", "game_id", "trigger")
    )
    rows = list(duplicates)
    if not rows:
        return
    detail = "；".join(
        f"平台={row['platform']}、游戏={'全部游戏' if row['game_id'] is None else row['game_id']}、"
        f"时机={row['trigger']} 有 {row['total']} 条"
        for row in rows
    )
    raise RuntimeError(
        "升级中止：交易平台自动回复规则存在同一（平台 + 游戏 + 时机）多条，"
        "无法建立唯一约束。请先清理重复规则（每条组合只留一条，**停用的也算一条**）"
        "后重新执行迁移。重复项：" + detail
    )


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0111_dailiantong_idempotency_backoff'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformpushsettings',
            name='default_limit_accept',
            field=models.PositiveSmallIntegerField(blank=True, default=None, help_text='发单页「拒绝新手和介入率」的默认值；留空 = 不设置（不发该参数）。', null=True, verbose_name='默认拒绝新手和介入率'),
        ),
        migrations.AddField(
            model_name='platformpushsettings',
            name='default_power_level',
            field=models.PositiveIntegerField(default=1, help_text='发单页「要求最低完单数」的默认值；0 = 不限制。', verbose_name='默认要求最低完单数'),
        ),
        migrations.AddField(
            model_name='platformpushsettings',
            name='require_template',
            field=models.TextField(blank=True, default='', help_text='发单时写进代练通 Actors 第五段；留空 = 用系统默认占位文案。', verbose_name='代练/陪玩要求模板'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_attempts',
            field=models.PositiveIntegerField(default=0, verbose_name='接手后发送连续失败次数'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_claim_token',
            field=models.CharField(blank=True, default='', max_length=32, verbose_name='接手后发送占位令牌'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_last_error',
            field=models.CharField(blank=True, default='', max_length=200, verbose_name='接手后发送最后一次错误'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_retry_source_key',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='接手后发送重试来源键'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_send_started_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='接手后发送占位时间'),
        ),
        migrations.AlterField(
            model_name='dailiantongpush',
            name='qr_sent_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='接手后发送时间'),
        ),
        migrations.RunPython(
            _assert_no_duplicate_rule_combos,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='platformautoreplyrule',
            constraint=models.UniqueConstraint(fields=('platform', 'game', 'trigger'), name='uniq_platform_auto_reply_rule_combo', nulls_distinct=False),
        ),
    ]
