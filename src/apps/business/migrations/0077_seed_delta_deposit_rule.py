"""给「三角洲」游戏落一条 300 元的押金规则（业务方口径：三角洲 300、其余默认 50）。

⚠️ **数据迁移，不是 schema 迁移**：只写数据，不改表结构。
⚠️ **按名字包含「三角洲」匹配**：业务方没有给出稳定的机器标识（没有 code/enum 列），
   只能按名称——名字里带「三角洲」的都算。⚠️ 如果将来有「三角洲行动（国际服）」这类
   需要不同金额的，得改成逐条配置（后台调金额的入口是后续任务）。
⚠️ 用 `get_or_create`：租户里可能已经手工配过，⛔ 不覆盖既有值。
"""
from decimal import Decimal

from django.db import migrations


def seed_delta(apps, schema_editor):
    Game = apps.get_model("business", "Game")
    GameDepositRule = apps.get_model("business", "GameDepositRule")
    for game in Game.objects.filter(name__contains="三角洲"):
        GameDepositRule.objects.get_or_create(
            game=game, defaults={"amount": Decimal("300")}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0076_gamedepositrule"),
    ]

    operations = [
        migrations.RunPython(seed_delta, migrations.RunPython.noop),
    ]
