"""B2（终审修复轮）：大厅共享升级时把**存量在挂镜像卡**统一下架。

## 为什么需要这一步

`PeerHallMirror`（团团通上家大厅在我方的镜像卡）在 v1 就已经上线。升级到 P1–P5 这版之后，
这张表的语义变了两处：① 接收侧多了一道**默认关**的开关（`hall_sync_enabled`）；
② 卡要沿链**再转发**给下家（多跳），载荷里的游戏 / 价格 / 跳数口径都对 v1 的残留行不再成立。

升级那一刻还挂在 `listed` 的卡是**旧口径**留下的投影：它们的 `payload` 可能是半成品、
`hop` 没有意义（0089 才加列）、而且上游不会因为这次升级主动重抛一遍。把它们留在厅里
会让打手看到一批**接不了 / 价不对 / 转正落空**的卡（B2 的现场）。⇒ 统一标 `gone`：

- `gone_at = now`（离开大厅的时刻是这次升级）；
- `last_error` = 人话，行上 / 排障时直接看得见为什么；
- ⛔ **不删行**（`peer_upstream_binding` 是 `PROTECT`，且 share-design §11-3 要求镜像行留档）；
- `claiming` / `promoted` **一个字节都不动**：`claiming` 正在等对端应答（改它会搞坏一次
  正在进行的接单），`promoted` 是已经转成正式单的历史事实（复活 / 改写 = 重复接单）。
- 之后上游**重新抛单**（`hall.throw`）时，`gone` 行按 D-0300 口径**自动复活** ⇒ 清理是
  可逆的**业务**动作，不需要数据回填。

## 为什么是**新迁移**而不是改 `0087`（后者只加了两列开关）

`0087` 已经在 `main` 上、且现网 v1 **已经应用过它**。往一条已应用的迁移里追加 `RunPython`
在生产上**不会执行**（`django_migrations` 认为它做完了）⇒ 存量卡永远不会被清。新迁移才能
保证**升级到本版的每一次部署**都真的跑一次这一步。

## 反向迁移（`reverse`）为什么是**有意的 no-op**

反向只需要「迁移能往回退」这一个性质，⛔ 不需要把数据改回去——而且**改不回去**：
今天没有任何一列记录「哪几张卡在升级前是 `listed`」。把表里所有 `gone` 行翻回 `listed`
会复活一批升级后才变成 `gone` 的卡（上家撤回 / 被接走 / 关开关下架）——那正是这张表要防的
僵尸卡。所以反向函数**只**写 `pass`（Django `RunPython` 的 `reverse_code` 必须可调用，
`noop` 让它可回滚），并在本 docstring 里如实说明「回滚不会恢复这次下架的数据」。
"""
from django.db import migrations
from django.utils import timezone

#: 下架时写进 `PeerHallMirror.last_error` 的**人话**（列宽 255，文案很短）。
#: ⚠️ 用例按字面值钉它（`tests/test_peer_hall_hop_and_unlist.py`）⇒ 改文案会一起红。
LEGACY_UNLISTED_NOTE = "大厅共享升级：旧卡统一下架，等上家重新抛单"


def unlist_legacy_hall_cards(apps, schema_editor):
    """把所有**此刻仍在挂**的镜像卡标 `gone`（`claiming` / `promoted` / 已 `gone` 不碰）。"""
    PeerHallMirror = apps.get_model("business", "PeerHallMirror")
    PeerHallMirror.objects.filter(status="listed").update(
        status="gone",
        gone_at=timezone.now(),
        last_error=LEGACY_UNLISTED_NOTE,
    )


def keep_data_as_is(apps, schema_editor):
    """反向 = 有意的 no-op（理由见模块 docstring：今天没有列能回答「升级前哪几张在挂」）。"""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("business", "0089_peerhallmirror_hop"),
    ]

    operations = [
        migrations.RunPython(unlist_legacy_hall_cards, keep_data_as_is),
    ]
