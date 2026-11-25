"""裁剪 43 个已裁定「不做」的目录点 + 把对标 105 就地改造为 `order.submit_work`。

## 为什么需要这个迁移

`seed_catalog()` 只 `update_or_create`、**从不删行**（`src/apps/rbac/seed.py`）
⇒ 只改 `apps/rbac/catalog.py` 对**已有**租户是**空操作**，权限树里照样看得见那 43 个
已裁定不做的点，管理员还能继续把它们勾给角色。漏跑的**症状不是 500、不是报错**，
而是「界面没变」。

## ⚠️ 理由更正（Task 8 附录 §0）：`seed_catalog` 是按 `benchmark_id` upsert

`seed_catalog()` 的匹配键是 **`benchmark_id`**（模型上 `unique=True`），`code` 只在
`defaults` 里（`code` 是允许改的唯一码，spec §4.3 的改名场景）⇒ 由此：

1. **105 的改名在存量租户里会「就地传播」**：同一条行（同一个 PK）被改成新 `code`，
   而 `Role.permissions` 指向的是**那一行**的 PK ⇒ 授权不会丢；
2. 真正残留的只有**被删的那 43 行**（`seed_catalog` 从不删行）
   ⇒ 本迁移的实质工作 = 删这 43 行 + 它们在各角色上的授权（隐性 through 表随 FK 级联）。

### 删点会走**两条**级联路径，今天只有第一条生效

- **① `Role.permissions` 的隐性 through 表**（本次真正用到的）：FK 带 CASCADE
  ⇒ 授权行随点一起走。它生效的判据在 `tests/test_rbac_catalog_migration.py`。
- **② `PermissionNode.parent` 自外键**（`apps/rbac/models.py`，`on_delete=CASCADE`）：
  删一个点会连带删**以它为父**的点。本迁移删的 43 个**全部是 `level=1` 的功能点**
  （来自 `catalog.py` 的 `NODES`），而目录只有两级、父一律是 `level=0` 的栏目
  （`seed_catalog` 的 `parent=modules[code.split(".")[0]]`）⇒ **没有任何点以它们为父，
  这条级联今天不会被触发**。
  ⚠️ 写下来是因为它是个**放大面**：若有人把 `PRUNED_CODES` 误改成栏目 code
  （或将来目录变成三级、被裁点开始有子节点），删除面会沿这条自外键**成片扩大**——
  那时 F1 的"存活点"断言就是你唯一的警报。

## ⛔ 实现禁忌：105 **绝不许**做成「删除 + 重建」

`delete()` 之后再 `create()`（或按 `code` 删旧建新）会让新行的 **PK 变化** ⇒
存量租户里**所有持有该点的角色当场丢掉 `/submit/`**（表现为代练员交单一律 403）——
旧行一删，`Role.permissions` 里指向它的中间表行就被级联带走，而 `seed_catalog` 只会重建
**点**、不会重建**谁被授过权**。

- ✅ 本迁移的做法：`filter(code=OLD_CODE).update(code=…, name=…, note=…)`（**in-place**，PK 不变）；
- ✅ 另一种同样正确的做法：这一支**干脆不做**，交给 `seed_catalog` 按 `benchmark_id` 传播；
- ⛔ 任何 `delete()` + `create()` 的组合。这条判据由
  `tests/test_rbac_catalog_migration.py::…test_the_rename_keeps_the_primary_key_and_the_role_assignments`
  （断言「105 的 PK 与迁移前相同」）钉住——那是唯一能把这种写法挡在门外的门。

## 反向迁移是 `noop`，这是**有意**的

重建 43 个点需要**同时**重建角色上的分配，而那份信息已经丢了（库里只剩"哪些角色有这些点"
的中间表行，随点一起被删）。要恢复请从 `docs/JIDANPU-PERMISSION-CATALOG.md` 的基线
重建目录并重跑 `seed_catalog`——**不许**写一个假装能恢复的 reverse：那种 reverse 会
重建点、静默丢掉授权，比没有更糟。

⚠️ 「级联真的生效」那条判据写在 `tests/test_rbac_catalog_migration.py` 里，**不在这里**：
历史模型的 M2M through 名与反向名依 Django 版本而变，在迁移文件里取它不可靠；
测试里可以用**运行期**模型。
"""
from django.db import migrations

#: 与 `apps/rbac/catalog.py` 的裁剪同步（43 条，spec §3.5）。
#: 「43 个对标 id」的权威字面量在 `tests/test_rbac_structure.py::PRUNED_BENCHMARK_IDS`
#: （本文件按 **code** 删——存量租户里这些行的 code 自种子之日起没变过，
#: 而 code 是本项目对外稳定标识）。
#: code 侧与 id 侧由 `tests/test_rbac_catalog_migration.py::PRUNED_NODES`
#: （**唯一一份** `(benchmark_id, code)` 字面映射）双向对拍：
#: 映射的 code 集合 == 本常量；映射的 id 集合 == `PRUNED_BENCHMARK_IDS`。
#: ⇒ 两侧不再是"两份可以同时打错、而测试全绿"的独立字面量。
PRUNED_CODES = (
    "order.platform_order_center", "order.create_success_guide", "order.player_chat_logs",
    "system_user.invite_registration", "system_user.checkin_logs", "system_user.checkin_stats",
    "finance.daily_report", "finance.daily_ledger", "finance.add_edit_ledger",
    "finance.training_materials", "finance.add_edit_training_material",
    "finance.delete_training_material", "finance.view_training_material",
    "finance.private_number_call_logs", "finance.qa_center", "finance.add_edit_qa",
    "game.items", "game.price_calculator", "game.honor_of_kings_price_settings",
    "game.other_price_settings", "system.private_number_settings",
    "system.wechat_miniprogram_management", "system.online_consultation_settings",
    "order.add_item_requirement", "order.remove_item_requirement", "order.link_group",
    "order.unlink_group", "order.new_link_group_view", "my_group.add_items",
    "order.push_to_platform", "order.platform_grab_hall", "order.platform_order_detail",
    "order.my_pushed_platform_orders", "order.platform_transaction_detail",
    "order.my_received_platform_orders", "order.restrict_agree_revoke",
    "order.restrict_manual_accept_platform", "finance.apply_settlement",
    "finance.settlement_preview", "finance.online_settle_payout",
    "order.restrict_reopen_completed", "system.sms_settings", "order.send_sms",
)

#: 对标 105「订单关联排团操作」被**改造**（不是被裁）成我方自有的「提交完单和提交问题单」。
#: `benchmark_id` 保留 105（业务方裁定），`url` 保留对标端点串以便溯源；
#: 目标值 = `apps/rbac/catalog.py` 里那一行的 `code` / `name` / `note`
#: （Task 7 的 `RETOOLED_105_FROM_CODE` 口径，spec §3.5.2）。
#: ⚠️ **2026-09-28（业务方裁定）**：`name` / `note` 随「复用 105 当『报问题单』的门、
#: 并把它改名」一起改（见 `catalog.py` 那一行的改名注释）。本迁移的契约是
#: 「目标值 = catalog 那一行」⇒ 两份字面量保持同源，`tests/test_rbac_catalog_migration.py`
#: 按字面值钉住本迁移的产出。⛔ `NEW_CODE`（`order.submit_work`）**一个字都不动**。
#: ⚠️ 本迁移在存量租户里**不会重跑**（Django 已记为 applied）⇒ 存量库的名字靠
#: `seed_catalog()` 按 `benchmark_id` upsert 传播（`name` 就在 `defaults` 里）；
#: 这里改的是**新建租户**从零迁移时看到的中间值，两者最终一致。
NEW_CODE = "order.submit_work"
NEW_NAME = "提交完单和提交问题单"
NEW_NOTE = "把订单从「代练中」推进到「待验收」（代练员交单）或提交问题单"
OLD_CODE = "order.confirm_link_group"


def remove_pruned_nodes_and_rename_submit(apps, schema_editor):
    """删 43 行 + 就地改造 105。两步都幂等（可重跑、可在新建租户的空表上跑）。"""
    Node = apps.get_model("rbac", "PermissionNode")
    # ① 删点：`Role.permissions` 的隐式 through 表带 CASCADE ⇒ 分配行随点一起走。
    #    不删授权行的话，界面上会留下"点不开的勾选项"（没有任何报错）。
    #    （另一条 `parent` 自外键级联今天不会被触发，理由见模块 docstring 的两条级联路径。）
    Node.objects.filter(code__in=PRUNED_CODES).delete()
    # ② 改造 105：**update 不新建行** ⇒ PK 不变，已分配的 `Role.permissions` 关系随之保留。
    #    ⛔ 不要改成 `delete()` + `create()`（理由见模块 docstring 的实现禁忌一节）。
    Node.objects.filter(code=OLD_CODE).update(code=NEW_CODE, name=NEW_NAME, note=NEW_NOTE)


def noop_reverse(apps, schema_editor):
    """有意为空：重建 43 个点需要重建角色分配，信息已丢（见模块 docstring）。"""


class Migration(migrations.Migration):

    dependencies = [
        ("rbac", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(remove_pruned_nodes_and_rename_submit, noop_reverse),
    ]
