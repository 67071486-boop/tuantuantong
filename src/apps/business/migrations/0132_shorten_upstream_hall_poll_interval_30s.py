"""业务方：上家线大厅全量拉取间隔默认值 **120 ⇒ 30 秒**（D-0363）。

## 为什么

同一条动机链：上家线**没有操作日志快路**（那个鸡蛋铺账号是打手权限，读不到对方的系统
操作日志，spec `docs/specs/2026-09-28-upstream-jidanpu-sync-design.md` §2.5 **实测**）⇒
「新单进我方大厅」只能靠大厅拉取。`120` 秒一拍最坏等 2 分钟；业务方拍板再缩到 `30`
（更快发现上家撤单、清僵尸），并**确认接受代价**（见文末）。

## 为什么是**一条迁移**而不是零迁移

判据用的是**现有配置列** `hall_poll_seconds`（⛔ 没有加列），但两件事只有迁移能做：
① `AlterField` 让**新建**绑定拿到新默认值 `30`；
② `RunPython` 把**存量**恰好等于上一代默认 `120` 的行一起缩到 `30`
（模型默认值改了不会回头改已存的行，不跑这一步现网的绑定照旧 120 秒）。
⚠️ 只命中 `120`：运营手工调过的其它值（例如 `180` / `300`）**一个字节都不动**
（⛔ 不按时间 / 大小兜底）。

## 反向迁移为什么是 no-op

反向只需要「迁移能往回退」这个性质，⛔ 不需要把数据改回去：今天没有任何一列记录
「哪一行的 30 是这次迁移改的」还是「本来就是 30」。把表里所有 30 翻回 120 会把运营
手工设过 30 的行也打回去——那是在改别人的配置。⇒ 反向函数只 `return None`。

## 代价（如实登记，业务方确认接受）

大厅全量一轮 ≈ 44 次请求（spec §2.1 实测 870 行 / 约 18 秒）⇒ `30` 秒一拍
≈ **88 次请求/分钟**，是 `120` 秒时的 **4 倍**；一轮实测约 18 秒 ⇒ 大厅慢路接近
常开（高占空比）。有 2026-10-20 被对端 WAF 封 IP 的前科；退路就是这一列本身
（可回 180 / 300），⛔ 不改代码。
"""
from django.db import migrations, models

#: 上一代默认值（这条迁移只动**恰好等于它**的行；⛔ 不按时间 / 大小兜底）。
OLD_DEFAULT_SECONDS = 120
NEW_DEFAULT_SECONDS = 30


def shorten_existing_bindings(apps, schema_editor):
    """把存量里**恰好等于上一代默认 120** 的大厅间隔缩到 30（别的手工值不动）。"""
    binding = apps.get_model("business", "JidanpuUpstreamBinding")
    binding.objects.filter(hall_poll_seconds=OLD_DEFAULT_SECONDS).update(
        hall_poll_seconds=NEW_DEFAULT_SECONDS
    )


def keep_data_as_is(apps, schema_editor):
    """反向 = 有意的 no-op（理由见模块 docstring：分不清「谁原本就是 30」）。"""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0131_receiptsyncconfig"),
    ]

    operations = [
        migrations.AlterField(
            model_name="jidanpuupstreambinding",
            name="hall_poll_seconds",
            field=models.PositiveIntegerField(
                default=NEW_DEFAULT_SECONDS, verbose_name="大厅拉取间隔(秒)"
            ),
        ),
        migrations.RunPython(shorten_existing_bindings, keep_data_as_is),
    ]
