"""审计批 C1（M-1）：上游绑定**不再存明文握手密码**——加布尔列 + 清空存量明文。

## 为什么

`PeerUpstreamBinding.password` 存的是能登录**上游租户后台**的真实口令（bind 握手验证的
就是上家的 Django 用户表）。下游库被拖库 / 备份泄露 ⇒ 直接登进上家租户后台，跨库打穿。
而功能上**不需要**存它：页面每次绑定强制重填（序列化器 `write_only`），全仓唯一的
「读」只是序列化器的 `bool(obj.password)`（只回布尔）⇒ 改存布尔即可，明文一无所用。

## 这条迁移做什么（两条操作，顺序承重）

1. `AddField password_set`（`default=False`）：先有列，才有得回填；
2. `RunPython`：**单条 bulk UPDATE** —— `UPDATE … SET password_set=(password<>''),
   password='' WHERE password<>''`（ORM 写法 = `exclude(password="").update(...)`）：
   - 只有**真有明文**的行被动到（幂等可重放：重放时 `WHERE` 命中 0 行）；
   - 明文**就地清空**、布尔按原事实回填，页面「要不要重填」的展示不变。

## 反向为什么是 **no-op**（改准：全量门禁裁定）

首版「不写 `reverse_code`（反向响亮报错）」与仓里**四个迁移回放网**正面相撞
（`test_jidanpu_upstream_models` / `test_jidanpu_member_models` 的
`Upstream/Member/HallSync` 回放类与 `test_order_screenshot_group_qr_unique` 的
`DedupeMigrationTests`——它们要在 setUp 里 `MigrationExecutor.migrate` **倒回**到各自
目标节点之前 ⇒ 全图必须可回放；全量 16 片实测 14 条 error）。裁定的优先级：**存量结构网优先**。

反向语义（诚实口径，⛔ 不是「假装恢复」）：

- `reverse_code=RunPython.noop`：unapply 这条迁移 = **不再动数据**——明文保持清空、
  布尔保持原值。它**不**恢复明文（写得出恢复逻辑就等于没删干净）；
- 回滚这条迁移的正确姿势依旧是先回滚**代码**（旧代码读 `bool(password)`，空列 = `False`，
  页面只是要求重填一次密码），而不是把明文变回来；
- 「明文不复活」有判据钉着：`test_peer_models.PeerPasswordPlaintextPurgeTests` 的
  no-op 反向用例 + 上面四个回放类自身。

## 残差（如实登记）

新代码对这一列**零写点**（绑定视图写 `password_set=True` 并把列清成空串）；
列本身保留（撤销列又是一张迁移，而空列已无读者）。
"""
from django.db import migrations, models


def fill_password_set_and_clear_plaintext(apps, schema_editor):
    """存量回填：有明文的行 `password_set=True` 且明文清空；没明文的行一个字节都不动。

    ⚠️ 一条 `UPDATE`（不是逐行循环）：行数大时只跑一条 SQL；幂等可重放
    （第二次跑 `WHERE password <> ''` 命中 0 行）。
    """
    binding = apps.get_model("business", "PeerUpstreamBinding")
    binding.objects.exclude(password="").update(password_set=True, password="")


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0094_shorten_upstream_hall_poll_interval"),
    ]

    operations = [
        migrations.AddField(
            model_name="peerupstreambinding",
            name="password_set",
            field=models.BooleanField(default=False, verbose_name="已填握手密码"),
        ),
        # 反向 = no-op：unapply 不再动数据、**不**恢复明文（见模块 docstring「反向为什么是 no-op」）。
        migrations.RunPython(
            fill_password_set_and_clear_plaintext,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
