"""目录裁剪**数据迁移**的判据：`0002` 删 43 行 + 对标 105 就地改名 `order.submit_work`；`0004` 删 4 个已下线系统设置页的点；`0015` 删客户维护组收敛的 8 个点（2026-11-11 业务方拍板）；`0006` / `0010` / `0011` / `0013` / `0014` 各补一条自有节点。
判据打在**数据库行**上：`seed_catalog()` 只按 `benchmark_id` upsert、**从不删行** ⇒ 只改 `catalog.py` 对**已有**租户是**空操作**，漏跑的症状不是报错、是「界面没变」；105 的改名就地传播（`Role.permissions` 指向同一行）⇒ 授权不丢，实质工作只剩删 43 行 + 它们在各角色上的授权；测试库建库时迁移**已经跑过** ⇒ 「迁移前」状态必须由 `_reset_to_pre_migration_state` 自己造（直接 `get` 会 `DoesNotExist`）。
三份清单互相钉住（任一侧单独打错一个字都红）：`PRUNED_NODES` 是**唯一字面映射**（来源 spec §3.5 六张表），与迁移 `PRUNED_CODES` 比 **code 集合等号**、与 `PRUNED_BENCHMARK_IDS`（**import** 自 structure 测试）比 **id 集合等号**；夹具三类行各挡一种错法——43 条被裁全造（迁移常量被截短才红）、2 条幸存者（「除 105 全删」才红）、105 那行（delete+insert 才红）；`0004` ⛔ 不改已应用的 `0002`（存量库不会重跑 ⇒ 假保证），邻接物**不许碰**（spec §10.2）；被裁行的 `name` / `note` / `url` 在夹具里是占位值——迁移只按 `code` 删、`level` / `parent` 不参与判据。"""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth.models import Group
# ⚠️ `Q` 的 import 已随 T4删除：`_reset_to_pre_migration_state` 改成
# `PermissionNode.objects.all().delete()`（理由见那里的 docstring），文件里再无 `Q(` 用法。
from django_tenants.test.cases import TenantTestCase

from apps.rbac.catalog import NODES
from apps.rbac.models import PermissionNode, Role
from apps.rbac.selectors import node_tree
#: ⭐ 43 个对标 id 的**权威字面量**在 structure 兜底里已有一份（与清单文档、`catalog.py` 的裁剪口径同源）
#: ⇒ 这里 **import** 而不是再抄一份，避免出现「第三份可以单独漂移的副本」。
#: ⚠️ 该模块 import 期无副作用（只建常量与类、不碰数据库）。
from tests.test_rbac_structure import PRUNED_BENCHMARK_IDS

#: 模块名以数字开头 ⇒ 只能用 `importlib`（`import` 语句做不到）。
#: RED 状态下这个模块还不存在 ⇒ 本文件在**收集期**就 `ModuleNotFoundError`（这就是第一步的红）。
MIGRATION = importlib.import_module(
    "apps.rbac.migrations.0002_remove_pruned_nodes_and_rename_submit"
)
#: ⭐ 本线：删 4 个已下线设置页点的**第二个**迁移（`0002` 已应用、不动它）。
MIGRATION_0004 = importlib.import_module(
    "apps.rbac.migrations.0004_remove_dropped_system_page_nodes"
)
#: ⭐ T4：给**存量租户**补上自有节点 `system.allow_taker_open_api` 的**第三个**迁移。
#: ⚠️ 与 `0002` / `0004` **方向相反**（那两条是删点、这条是**补点**），但口径同源：
#: 都靠数据迁移落到存量租户，⛔ 不是靠"记得手工重灌目录"。
MIGRATION_0006 = importlib.import_module(
    "apps.rbac.migrations.0006_add_taker_open_api_node"
)
#: ⭐ 2026-11-11 客户维护组收敛（业务方拍板「收敛到 3 个页面」）：删 8 个无功能 / 已删
#: 功能点的**第五个**删除迁移（`0002` / `0004` 已应用、不动它们；口径同源）。
MIGRATION_0015 = importlib.import_module(
    "apps.rbac.migrations.0015_prune_customer_module_nodes"
)
#: ⭐ 客户线第五批（2026-11-11）：给存量租户补自有限制节点的**补点**迁移（方向与 0015 相反）。
MIGRATION_0016 = importlib.import_module(
    "apps.rbac.migrations.0016_add_customer_member_list_self_only_node"
)

#: ⭐ 43 条被裁点的 `(benchmark_id, code)` **唯一字面映射**（E-0024：期望值写字面值）。
#: 来源：spec §3.5 的六张表（`docs/specs/2026-09-23-rbac-roles-and-catalog-consolidation-design.md`）
#: 与裁剪前的 `catalog.py`（`git show adadb3a^:src/apps/rbac/catalog.py`）逐条对齐；id 侧另与 `tests/test_rbac_structure.py::PRUNED_BENCHMARK_IDS` 比等号（见下面那条用例）。
PRUNED_NODES = (
    (1236, "order.platform_order_center"), (1160, "order.create_success_guide"),
    (1268, "order.player_chat_logs"), (1280, "system_user.invite_registration"),
    (59, "system_user.checkin_logs"), (60, "system_user.checkin_stats"),
    (81, "finance.daily_report"), (139, "finance.daily_ledger"),
    (1179, "finance.add_edit_ledger"), (144, "finance.training_materials"),
    (145, "finance.add_edit_training_material"), (146, "finance.delete_training_material"),
    (147, "finance.view_training_material"), (1171, "finance.private_number_call_logs"),
    (1202, "finance.qa_center"), (1203, "finance.add_edit_qa"), (64, "game.items"),
    (1218, "game.price_calculator"), (1217, "game.honor_of_kings_price_settings"),
    (1232, "game.other_price_settings"), (1170, "system.private_number_settings"),
    (1191, "system.wechat_miniprogram_management"),
    (1243, "system.online_consultation_settings"), (28, "order.add_item_requirement"),
    (29, "order.remove_item_requirement"), (34, "order.link_group"),
    (35, "order.unlink_group"), (1178, "order.new_link_group_view"),
    (106, "my_group.add_items"), (1180, "order.push_to_platform"),
    (1181, "order.platform_grab_hall"), (1182, "order.platform_order_detail"),
    (1183, "order.my_pushed_platform_orders"), (1184, "order.platform_transaction_detail"),
    (1188, "order.my_received_platform_orders"), (1260, "order.restrict_agree_revoke"),
    (1261, "order.restrict_manual_accept_platform"), (1168, "finance.apply_settlement"),
    (1167, "finance.settlement_preview"), (1174, "finance.online_settle_payout"),
    (1204, "order.restrict_reopen_completed"), (90, "system.sms_settings"),
    (30, "order.send_sms"),
)

#: 被裁 code 的元组视图（夹具与逐条断言都用它；顺序与迁移文件的常量一致，
#: 由下面第一条用例用**元组等号**钉住）。
PRUNED_CODES = tuple(code for _benchmark_id, code in PRUNED_NODES)

#: ⭐ 第二批被裁点：4 个已下线系统设置页的权限点，由**独立的数据迁移 `0004`** 删除（`0002` 已应用，⛔ 不动它）。
#: 形状与 `PRUNED_NODES` 一致，逐条判据也照它写：code 侧与 `0004.PRUNED_CODES` 比**元组等号**，
#: id 侧与 `PRUNED_BENCHMARK_IDS` 的**并集**比**集合等号**。来源：`docs/specs/2026-09-23-order-recognition-settings-design.md` §10（业务方裁定）。
DROPPED_PAGE_NODES = (
    (123, "system.wechat_settings"),
    (134, "system.taobao_ship_settings"),
    (1257, "system.order_labels"),
    (1271, "system.order_correction_settings"),
)
#: 本批被裁 code 的元组视图（顺序与 `0004.PRUNED_CODES` 一致）。
DROPPED_PAGE_CODES = tuple(code for _benchmark_id, code in DROPPED_PAGE_NODES)

#: ⭐ 第三批被裁点（2026-11-11 业务方拍板「客户维护组收敛到 3 个页面」，迁移 `0015`）：
#: 8 个无功能 / 已删功能点，逐条判据照 `DROPPED_PAGE_NODES` 的形状。
#: 保留的三点（blacklist 138 / member_list 141 / member_analysis_report 1226）是**邻接物**。
DROPPED_CUSTOMER_NODES = (
    (143, "customer.export_phones"),
    (1211, "customer.export_member_data"),
    (1224, "customer.add_edit_member"),
    (1225, "customer.member_level_settings"),
    (1251, "customer.delete_member"),
    (1252, "customer.adjust_member_balance"),
    (1254, "customer.member_balance_details"),
    (1282, "customer.export_fund_details"),
)
#: 本批被裁 code 的元组视图（顺序与 `0015.PRUNED_CODES` 一致）。
DROPPED_CUSTOMER_CODES = tuple(code for _benchmark_id, code in DROPPED_CUSTOMER_NODES)

#: ⭐ 第三批的**邻接物**（必须活着）：收敛后保留的 3 个客户维护点（各有 v2 页面 / 端点在用）。
CUSTOMER_SURVIVOR_NODES = (
    (138, "customer.blacklist"),
    (141, "customer.member_list"),
    (1226, "customer.member_analysis_report"),
)
CUSTOMER_SURVIVOR_CODES = tuple(code for _bid, code in CUSTOMER_SURVIVOR_NODES)

#: ⭐ 邻接物（**不许碰**，spec §10.2）：与被裁的 4 个设置页**名字近似**，但它们是功能页 / 写动作 ⇒ 必须活着。
#: 用途：① 断言它们**仍在 `catalog.NODES` 里**（没被顺手裁掉）；② 断言 `0004` 跑完它们**一行不动**。
#: ⚠️ 字面值（E-0024）：不从 `catalog.py` 派生，否则「catalog 被改坏」不红。
ADJACENT_SURVIVOR_NODES = (
    (1227, "order.taobao_ship_records"),  # 淘宝发货记录页（`order-tbsendrecordlist.ts`）
    (1258, "order.update_tags"),          # 改/清除订单标签的 API 点
    (1212, "system.pinduoduo_settings"),  # 其它平台（拼多多）店铺对接
)

#: ⭐ **必须活下来**的两条行（夹具第二类，把「多删」变成红）：取今天 `available=True`、且真挂在角色上的两个点
#:（`catalog.py` 的 `order.list`(14) / `order.change_status`(16)，后者正是 `has_node("order.change_status")` 的判据点）。
#: 元组 = `(benchmark_id, code, name, note, url, kind)`，与 `catalog.py` 逐字一致。
SURVIVOR_NODES = (
    (14, "order.list", "订单管理列表", "订单管理列表", "/Order/OrderList", "page"),
    (16, "order.change_status", "更改订单状态（非已完成）",
     "更改订单状态（除了变为已完成之外的操作）", "/OrderAjax/UpOrderDLStaByIdx", "api"),
)
SURVIVOR_CODES = tuple(code for _bid, code, *_rest in SURVIVOR_NODES)

#: 105 改造前后的 code（字面值；`name` / `note` 的期望值写在用例里）。
OLD_CODE = "order.confirm_link_group"
NEW_CODE = "order.submit_work"
BENCHMARK_ID_105 = 105


class CatalogPruneMigrationTests(TenantTestCase):
    """在「存量租户还留着旧行」的形状上跑迁移函数，逐条断言。"""

    def _reset_to_pre_migration_state(self):
        """把库恢复成「迁移前」：105 还是旧 code，43 条被裁点都在，另加 2 条必须活下来的点。
        返回 `(105 那一行, 43 条被裁点的列表, 2 条必须活下来的点的列表)`。
        ⚠️ 先**清空整表**再建：跑完迁移的租户**恒有 1 行**自有节点（`0006` 会建行），且租户建库路径可能顺手灌目录（`provision_tenant` 就会）——不显式清空的话 `benchmark_id` 唯一约束会让 `create` 抛 `IntegrityError`，症状看起来像「迁移坏了」（`_backfill` 是同一手法）。"""
        PermissionNode.objects.all().delete()
        old = PermissionNode.objects.create(
            benchmark_id=BENCHMARK_ID_105, code=OLD_CODE, name="订单关联排团操作",
            note="在订单关联排团时请求确认关联操作", url="/PveTaskAjax/JoinOrderByGroupID",
            kind="api", level=1, available=True,
        )
        pruned = [
            # ⚠️ `name`/`note`/`url` 是占位值、`parent` 留空：迁移**只按 code 删**，
            #    这三列与父子关系都不参与判据（真实库里它们各有对标原文与父栏目）。
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name="旧点位", note="", url="",
                kind="page", level=1, available=False,
            )
            for benchmark_id, code in PRUNED_NODES
        ]
        survivors = [
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name=name, note=note, url=url,
                kind=kind, level=1, available=True,
            )
            for benchmark_id, code, name, note, url, kind in SURVIVOR_NODES
        ]
        return old, pruned, survivors

    def _make_role(self, name):
        return Role.objects.create(name=name, group=Group.objects.create(name=name))

    def _run(self):
        """跑迁移函数本体（历史模型不可靠，直接给**运行期** registry，见迁移文件注释）。"""
        MIGRATION.remove_pruned_nodes_and_rename_submit(django_apps, None)

    def _run_0004(self):
        """跑**第二个**迁移（4 个已下线设置页的点）的函数本体。"""
        MIGRATION_0004.remove_dropped_system_page_nodes(django_apps, None)

    def _make_dropped_page_fixture(self):
        """造 4 条被裁行 + 3 条**邻接物**（都用占位 `name`/`note`，迁移只按 code 删）；返回 `(dropped, adjacent)` 两组 `PermissionNode`。"""
        dropped = [
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name="旧设置页点", note="", url="",
                kind="page", level=1, available=False,
            )
            for benchmark_id, code in DROPPED_PAGE_NODES
        ]
        adjacent = [
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name="邻接物", note="", url="",
                kind="page", level=1, available=False,
            )
            for benchmark_id, code in ADJACENT_SURVIVOR_NODES
        ]
        return dropped, adjacent

    def _snapshot(self):
        """全表**所有列** + 两张角色授权关系的取值快照（用于「重跑 / 反向都是 no-op」）。
        ⚠️ 只快照 `id/code/name/note` 时，将来「改 `benchmark_id`/`available`/`url`/`kind`/`level`/`parent`/`order_by` 的迁移变异」抓不到（no-op 判据静默放过）⇒ 逐列列全。"""
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(Role.objects.values_list("id", "name")),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    # ------------------------------------------------------------------
    # 常量层：43 条清单自己也要有判据（否则迁移常量被截短时下面全是"真空绿"）
    # ------------------------------------------------------------------
    def test_the_migration_ships_exactly_the_43_pruned_codes(self):
        """迁移里的常量必须与 `PRUNED_NODES` 一致，且没有一条还留在 `catalog.py` 里。"""
        self.assertEqual(len(PRUNED_NODES), 43, "被裁清单不再是 43 条——先核 spec §3.5")
        self.assertEqual(len(PRUNED_CODES), 43, "被裁清单的 code 数不对")
        self.assertEqual(len(set(PRUNED_CODES)), 43, "被裁清单里有重复 code")
        self.assertEqual(
            len({bid for bid, _code in PRUNED_NODES}), 43, "被裁清单里有重复 benchmark_id"
        )
        self.assertEqual(
            tuple(MIGRATION.PRUNED_CODES), PRUNED_CODES,
            "迁移里的被裁清单与本文件的字面映射不一致（少删/多删/抄错/次序变了）",
        )
        still_in_catalog = sorted(set(PRUNED_CODES) & {code for code, *_ in NODES})
        self.assertEqual(
            still_in_catalog, [],
            f"这些 code 仍在 catalog.py 里声明，却被迁移当成被裁点：{still_in_catalog}",
        )

    def test_the_pruned_codes_and_benchmark_ids_are_cross_checked(self):
        """⭐ `code` 侧与 `benchmark_id` 侧必须被**同一个中介**绑住：此前是两份互不相干的字面量（迁移的 `PRUNED_CODES` 与 structure 的 `PRUNED_BENCHMARK_IDS`）⇒ 两边**同时**打错同一个点，`still_in_catalog` 那条自证照样绿（它只问「这个 code 还在不在 `catalog.py`」），而生产上那一行会**留在库里**。
        现在 `PRUNED_NODES` 是唯一字面映射：**集合等号**（不是包含）钉两侧。"""
        self.assertEqual(
            {code for _bid, code in PRUNED_NODES}, set(MIGRATION.PRUNED_CODES),
            "映射的 code 集合与迁移里的被裁清单不相等（集合等号，不是包含）",
        )
        self.assertEqual(
            {bid for bid, _code in PRUNED_NODES}
            | {bid for bid, _code in DROPPED_PAGE_NODES}
            | {bid for bid, _code in DROPPED_CUSTOMER_NODES},
            set(PRUNED_BENCHMARK_IDS),
            "三份 (benchmark_id, code) 映射的 **id 并集**与 "
            "tests/test_rbac_structure.py::PRUNED_BENCHMARK_IDS 不相等——"
            "code 侧与 id 侧至少有一侧抄错了点（仍是**等号**：少登记/多登记/抄错 id 都红）",
        )
        # ⭐ 本线：第二批（4 个已下线设置页点）走的是**独立迁移 `0004`**，
        #    这里把它的 code 侧也钉成**元组等号**（顺序也钉）——「迁移删的」与「目录裁的」
        #    必须是同一批，不会各自漂移。
        self.assertEqual(
            tuple(MIGRATION_0004.PRUNED_CODES), DROPPED_PAGE_CODES,
            "0004 迁移里的被裁清单与本文件的第二份字面映射不一致（少删/多删/抄错/次序变了）",
        )
        # ⭐ 2026-11-11 第三批（0015）同样钉成**元组等号**（顺序也钉）。
        self.assertEqual(
            tuple(MIGRATION_0015.PRUNED_CODES), DROPPED_CUSTOMER_CODES,
            "0015 迁移里的被裁清单与本文件的第三份字面映射不一致（少删/多删/抄错/次序变了）",
        )
        # ⭐ 本线：两份被裁清单**不许重叠**（同一 code 登记两次 = 两份事实）。
        self.assertEqual(
            set(PRUNED_CODES) & set(DROPPED_PAGE_CODES), set(),
            "0002 与 0004 的被裁清单有重叠——同一个 code 被登记了两次",
        )
        self.assertEqual(
            (set(PRUNED_CODES) | set(DROPPED_PAGE_CODES)) & set(DROPPED_CUSTOMER_CODES), set(),
            "0015 与前两批的被裁清单有重叠——同一个 code 被登记了两次",
        )
        # ⭐ 邻接物（spec §10.2）**必须仍在 catalog 里**，且**不在**任何一份被裁清单里。
        catalog_codes = {code for code, *_ in NODES}
        adjacent_codes = {code for _bid, code in ADJACENT_SURVIVOR_NODES}
        self.assertEqual(
            sorted(adjacent_codes - catalog_codes), [],
            "spec §10.2 明令保留的邻接物从 catalog.py 消失了——删宽了",
        )
        self.assertEqual(
            sorted(adjacent_codes & (set(PRUNED_CODES) | set(DROPPED_PAGE_CODES))), [],
            "spec §10.2 明令保留的邻接物被登记进了被裁清单——那会把功能页/写动作一起删掉",
        )

    # ------------------------------------------------------------------
    # 断言 ①：43 行全删 + 它们在角色上的授权一起走（隐性 through 表 CASCADE）
    # ------------------------------------------------------------------
    def test_all_pruned_nodes_and_their_role_assignments_are_deleted(self):
        """级联断言：删点必须把 `Role.permissions` 中间表那些行也带走。
        这是「点删了、角色上还挂着」的**唯一**防线——界面上不会报错，只会在权限清单里多出一条点不开的项。"""
        _, pruned, _ = self._reset_to_pre_migration_state()
        role = self._make_role("被裁岗")
        role.permissions.add(*pruned)
        self.assertEqual(role.permissions.count(), 43, "前置：43 条被裁点应当都挂上了")

        self._run()

        for code in PRUNED_CODES:
            with self.subTest(code=code):
                self.assertFalse(
                    PermissionNode.objects.filter(code=code).exists(),
                    f"{code} 没有被迁移删掉（存量租户会继续看见这个已裁定不做的权限点）",
                )
        self.assertEqual(
            role.permissions.count(), 0,
            "被裁的点删了，但角色上的授权还在（中间表那一行没跟着走）",
        )
        self.assertEqual(list(role.permissions.values_list("code", flat=True)), [])

    # ------------------------------------------------------------------
    # ⭐ F1：不该删的点**一条都不许少**（否则"除 105 全删"这种放宽写法全绿）
    # ------------------------------------------------------------------
    def test_nodes_outside_the_pruned_set_survive_with_their_role_assignments(self):
        """⭐ 把删除条件放宽成 `exclude(benchmark_id=105).delete()` **必须红**：没有这条，夹具里除 105 就只有被裁点 ⇒ **多删**（乃至「除 105 全删」）在测试里完全看不出来，生产上那是「权限树被清空」。
        夹具额外造两条**已实现、已授权**的点（`order.list`(14) / `order.change_status`(16)），这里钉住「行还在 + 授权还在 + PK 没变」。"""
        _, _, survivors = self._reset_to_pre_migration_state()
        role = self._make_role("存活岗")
        role.permissions.add(*survivors)
        pks = {row.code: row.pk for row in survivors}
        self.assertEqual(len(pks), 2, "前置：两条存活点应当各有唯一 code")

        self._run()

        self.assertEqual(
            PermissionNode.objects.count(), 3,
            "迁移后库里应当只剩「105 一行 + 2 条存活点」——多了说明没删干净，"
            "少了说明**删宽了**（生产上会删掉不该删的权限点）",
        )
        for benchmark_id, code, name, note, url, kind in SURVIVOR_NODES:
            with self.subTest(code=code):
                row = PermissionNode.objects.filter(code=code).first()
                self.assertIsNotNone(
                    row, f"{code} 不在库里了——删除条件放宽了（这一行今天还在用，不该删）"
                )
                self.assertEqual(row.pk, pks[code], f"{code} 的 PK 变了（不该被重建）")
                self.assertEqual(row.benchmark_id, benchmark_id)
                self.assertEqual(row.name, name)
                self.assertTrue(row.available, f"{code} 的 available 被迁移改了")
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            sorted(SURVIVOR_CODES),
            "存活点在角色上的授权被迁移动掉了",
        )
        self.assertEqual(
            sorted(role.permissions.values_list("id", flat=True)),
            sorted(pks.values()),
            "存活点的授权指向了别的行（PK 变了）",
        )
        self.assertFalse(
            PermissionNode.objects.filter(
                code__in=PRUNED_CODES
            ).exists(),
            "被裁点还在",
        )

    # ------------------------------------------------------------------
    # 断言 ②③：105 那一行**还在、PK 没变**，挂在它上面的角色授权仍然在
    # ------------------------------------------------------------------
    def test_the_rename_keeps_the_primary_key_and_the_role_assignments(self):
        """本文件最重要的断言：105 是**就地改名**，不是删除 + 重建。
        `delete()` + `create()` 的实现能让本文件**其余全部断言变绿**，而存量租户里**所有持有该点的角色当场丢掉 `/submit/`**（代练员交单一律 403）——`Role.permissions` 的旧关系随旧行级联删掉；唯一能挡住这种写法的门就是「PK 与迁移前相同」。"""
        old, _, _ = self._reset_to_pre_migration_state()
        role = self._make_role("代练岗")
        role.permissions.add(old)
        old_pk = old.pk
        self.assertEqual(list(role.permissions.values_list("id", flat=True)), [old_pk])

        self._run()

        rows = list(PermissionNode.objects.filter(benchmark_id=BENCHMARK_ID_105))
        self.assertEqual(len(rows), 1, "对标 105 应当**恰好**一行（不能新建出第二行）")
        row = rows[0]
        # ② **PK 不变** —— 把 delete+insert 挡在门外的唯一判据
        self.assertEqual(
            row.pk, old_pk,
            "105 的 PK 变了 ⇒ 迁移把它做成了「删旧建新」⇒ 存量租户所有角色当场丢掉 /submit/（403）",
        )
        # 就地改名的目标值（字面值，E-0024）
        self.assertEqual(row.code, NEW_CODE)
        # （业务方裁定）：105 改名（复用同一点当「报问题单」的门）
        # ⇒ 名字与说明跟着改；`code` 仍未动（上一行）。
        self.assertEqual(row.name, "提交完单和提交问题单")
        self.assertEqual(row.note, "把订单从「代练中」推进到「待验收」（代练员交单）或提交问题单")
        self.assertFalse(
            PermissionNode.objects.filter(code=OLD_CODE).exists(), "旧 code 还留在库里"
        )
        # ③ 角色授权跟着同一行走
        self.assertEqual(
            list(role.permissions.values_list("id", flat=True)), [old_pk],
            "角色在 105 上的授权丢了（或指向了另一行）——存量租户会交不了单",
        )
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)), [NEW_CODE]
        )

    # ------------------------------------------------------------------
    # 反向：**有意为空**，且不许写成「假装能恢复」
    # ------------------------------------------------------------------
    def test_the_reverse_is_registered_and_is_a_real_noop(self):
        """`reverse_code` 必须是那个空函数，且调用它**一行都不许动**。
        重建 43 个点需要**同时**重建角色上的分配，而那份信息已经丢了（`seed_catalog` 重灌只按 `benchmark_id` 建点、不会恢复谁被授过权）⇒ 任何「反向恢复」都必然是骗人的。"""
        operation = MIGRATION.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION.noop_reverse,
            "RunPython 的 reverse_code 不是那个显式的空函数",
        )

        old, _, survivors = self._reset_to_pre_migration_state()
        role = self._make_role("反向岗")
        role.permissions.add(old, *survivors)
        self._run()
        before = self._snapshot()

        MIGRATION.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "反向迁移动了数据——它必须是有意为之的空操作")

    # ------------------------------------------------------------------
    # 部署面：在**新建**租户（空表）与**已跑过**的库上都必须安全
    # ------------------------------------------------------------------
    def test_running_on_a_fresh_tenant_with_an_empty_catalog_is_safe(self):
        """新租户的顺序是「先 migrate、后 seed」⇒ 迁移会看到空表，不许抛。
        ⚠️ 跑完迁移的租户里不再恒空（`0006` 会建 1 行自有节点）⇒ 先把表清空、把「空表」这个前提**显式**做出来（⛔ 不改断言的字面值）。"""
        PermissionNode.objects.all().delete()
        self.assertEqual(PermissionNode.objects.count(), 0)
        self._run()
        self.assertEqual(PermissionNode.objects.count(), 0)

    def test_re_running_the_migration_changes_nothing(self):
        """部署脚本 / 补灌命令可能重跑 ⇒ 第二次必须是彻底的 no-op。"""
        old, pruned, survivors = self._reset_to_pre_migration_state()
        role = self._make_role("重跑岗")
        role.permissions.add(old, *pruned, *survivors)

        self._run()
        after_first = self._snapshot()
        # 44 条被裁/旧行 → 剩下 3 条 = 105 那一行 + 2 条存活点（F1 夹具）。
        self.assertEqual(PermissionNode.objects.count(), 3)

        self._run()

        self.assertEqual(self._snapshot(), after_first, "迁移重跑改动/复制了数据")
        self.assertEqual(PermissionNode.objects.count(), 3)

    # ⭐ 第二批：4 个已下线系统设置页的点（迁移 `0004`）——独立的一批，与上面 `0002` 的 43 条分开登记、分开断言。
    def test_the_second_pruned_list_is_four_literal_rows(self):
        """常量层：4 条清单自己要有判据（否则 `0004` 被截短时下面的用例是"真空绿"）。"""
        self.assertEqual(
            len(DROPPED_PAGE_NODES), 4, "第二批被裁清单不再是 4 条——先核 spec §10"
        )
        self.assertEqual(len(DROPPED_PAGE_CODES), 4, "第二批被裁清单的 code 数不对")
        self.assertEqual(len(set(DROPPED_PAGE_CODES)), 4, "第二批被裁清单里有重复 code")
        self.assertEqual(
            len({bid for bid, _code in DROPPED_PAGE_NODES}), 4,
            "第二批被裁清单里有重复 benchmark_id",
        )
        # 它们**确实不在** catalog 里（防「裁了又加回来」；B5 从文档侧也守一遍）。
        self.assertEqual(
            sorted({code for _bid, code in DROPPED_PAGE_NODES} & {code for code, *_ in NODES}),
            [],
            "这 4 个 code 仍在 catalog.py 里声明，却已被当成被裁点",
        )

    def test_second_migration_deletes_the_four_dropped_points_and_their_assignments(self):
        """`0004` 删 4 行 + 角色授权随隐式 through 表 CASCADE 一起走（与 0002 同形状）。"""
        dropped, adjacent = self._make_dropped_page_fixture()
        role = self._make_role("下线页岗")
        role.permissions.add(*dropped, *adjacent)
        self.assertEqual(
            role.permissions.count(), 7, "前置：4 条被裁点 + 3 条邻接物应当都挂上了"
        )

        self._run_0004()

        for code in DROPPED_PAGE_CODES:
            with self.subTest(code=code):
                self.assertFalse(
                    PermissionNode.objects.filter(code=code).exists(),
                    f"{code} 没有被 0004 删掉（存量租户会继续看见这个已裁定下线的权限点）",
                )
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            sorted(code for _bid, code in ADJACENT_SURVIVOR_NODES),
            "被裁的点删了，但角色上的授权没有一起走（中间表那一行留下了）",
        )

    def test_second_migration_never_touches_the_adjacent_nodes(self):
        """⛔ spec §10.2 的两条边界：删宽了（或顺手把邻接物登记进被裁清单）必须红。
        邻接物（`order.taobao_ship_records` / `order.update_tags` / `system.pinduoduo_settings`）名字与被裁的 4 个设置页相近，但它们是功能页 / 写动作 ⇒ 行的 **PK 与授权都不许变**（PK 变了等于被「删旧建新」，存量租户会丢授权）。"""
        _, adjacent = self._make_dropped_page_fixture()
        role = self._make_role("邻接岗")
        role.permissions.add(*adjacent)
        pks = {row.code: row.pk for row in adjacent}

        self._run_0004()

        for benchmark_id, code in ADJACENT_SURVIVOR_NODES:
            with self.subTest(code=code):
                row = PermissionNode.objects.filter(code=code).first()
                self.assertIsNotNone(row, f"{code} 被 0004 删了——它是功能页/写动作，必须保留")
                self.assertEqual(row.pk, pks[code], f"{code} 的 PK 变了（不该被重建）")
                self.assertEqual(row.benchmark_id, benchmark_id)
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            sorted(code for _bid, code in ADJACENT_SURVIVOR_NODES),
            "邻接物在角色上的授权被 0004 动掉了",
        )

    def test_second_migration_is_safe_on_empty_table_and_re_runnable(self):
        """新租户（空表）与重跑都必须安全：`filter(...).delete()` 命中 0 行也是成功；先清空表把「空表」前提显式做出来（理由同上一条）。"""
        PermissionNode.objects.all().delete()
        self.assertEqual(PermissionNode.objects.count(), 0)
        self._run_0004()
        self.assertEqual(PermissionNode.objects.count(), 0)

        dropped, adjacent = self._make_dropped_page_fixture()
        role = self._make_role("四号岗")
        role.permissions.add(*dropped, *adjacent)
        self._run_0004()
        after_first = self._snapshot()
        self.assertEqual(PermissionNode.objects.count(), 3, "应当只剩 3 条邻接物")

        self._run_0004()

        self.assertEqual(self._snapshot(), after_first, "0004 重跑改动/复制了数据")

    def test_second_migration_reverse_is_registered_and_is_a_real_noop(self):
        """`0004` 的反向也必须是那个**显式空函数**（口径与 `0002` 一致）。"""
        operation = MIGRATION_0004.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0004.noop_reverse,
            "0004 的 RunPython reverse_code 不是那个显式的空函数",
        )

        _, adjacent = self._make_dropped_page_fixture()
        self._run_0004()
        before = self._snapshot()

        MIGRATION_0004.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0004 的反向迁移动了数据——它必须是有意为之的空操作")


class CustomerPruneMigrationTests(TenantTestCase):
    """`0015_prune_customer_module_nodes`（2026-11-11 业务方拍板客户维护组收敛）：
    与 `0004` 同形状的第三批删除迁移，逐条判据照 `test_second_migration_*` 写。

    ⚠️ 邻接物 = 收敛后**保留**的 3 个客户维护点（各有 v2 页面 / 端点在用），
    删宽了（把保留点一起删）必须红。
    """

    def _make_role(self, name):
        """照 `CatalogPruneMigrationTests._make_role`（本类独立一份，不跨类借方法）。"""
        return Role.objects.create(name=name, group=Group.objects.create(name=name))

    def _make_customer_fixture(self):
        """造 8 条被裁行 + 3 条保留点（占位 `name`/`note`；迁移只按 code 删）。

        ⚠️ 先删两条翻真行（2026-11-11 起 `0017` 会在租户建库迁移时按 code 建出
        141 / 1226 ⇒ 不删的话 `benchmark_id` 唯一约束让 `create` 抛 `IntegrityError`）。
        """
        PermissionNode.objects.filter(
            code__in=("customer.member_list", "customer.member_analysis_report")
        ).delete()
        dropped = [
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name="旧客户点", note="", url="",
                kind="page", level=1, available=False,
            )
            for benchmark_id, code in DROPPED_CUSTOMER_NODES
        ]
        survivors = [
            PermissionNode.objects.create(
                benchmark_id=benchmark_id, code=code, name="客户保留点", note="", url="",
                kind="page", level=1, available=False,
            )
            for benchmark_id, code in CUSTOMER_SURVIVOR_NODES
        ]
        return dropped, survivors

    def _run_0015(self):
        MIGRATION_0015.remove_pruned_customer_nodes(django_apps, None)

    def _snapshot(self):
        """全列快照（口径同 `CatalogPruneMigrationTests._snapshot`；本类独立一份）。"""
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(Role.objects.values_list("id", "name")),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    def test_the_third_pruned_list_is_eight_literal_rows(self):
        """常量层：8 条清单自己要有判据（迁移常量被截短时下面的用例是"真空绿"）。"""
        self.assertEqual(len(DROPPED_CUSTOMER_NODES), 8, "第三批被裁清单不再是 8 条")
        self.assertEqual(len(DROPPED_CUSTOMER_CODES), 8)
        self.assertEqual(len(set(DROPPED_CUSTOMER_CODES)), 8)
        self.assertEqual(
            sorted({code for _bid, code in DROPPED_CUSTOMER_NODES} & {code for code, *_ in NODES}),
            [],
            "这 8 个 code 仍在 catalog.py 里声明，却已被当成被裁点",
        )
        # 保留点必须仍在 catalog 里（防「顺手把收敛后的 3 页也删了」）。
        self.assertEqual(
            sorted(code for _bid, code in CUSTOMER_SURVIVOR_NODES if code not in {
                code for code, *_ in NODES
            }),
            [],
            "收敛后保留的 3 个客户维护点从 catalog.py 消失了——删宽了",
        )

    def test_third_migration_deletes_the_eight_points_and_their_assignments(self):
        """`0015` 删 8 行 + 角色授权随隐式 through 表 CASCADE 一起走（与 0002/0004 同形状）。"""
        dropped, survivors = self._make_customer_fixture()
        role = self._make_role("收敛岗")
        role.permissions.add(*dropped, *survivors)
        self.assertEqual(role.permissions.count(), 11, "前置：8 被裁 + 3 保留应当都挂上")

        self._run_0015()

        for code in DROPPED_CUSTOMER_CODES:
            with self.subTest(code=code):
                self.assertFalse(
                    PermissionNode.objects.filter(code=code).exists(),
                    f"{code} 没有被 0015 删掉（存量租户会继续看见这个已裁定不做的权限点）",
                )
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            sorted(CUSTOMER_SURVIVOR_CODES),
            "被裁的点删了，但角色上的授权没有一起走（中间表那一行留下了）",
        )

    def test_third_migration_never_touches_the_kept_customer_nodes(self):
        """保留的 3 个点：行的 PK 不许变（删宽 / 删旧建新都必须红）。"""
        _, survivors = self._make_customer_fixture()
        pks = {row.code: row.pk for row in survivors}

        self._run_0015()

        for benchmark_id, code in CUSTOMER_SURVIVOR_NODES:
            with self.subTest(code=code):
                row = PermissionNode.objects.filter(code=code).first()
                self.assertIsNotNone(row, f"{code} 被 0015 删了——收敛后保留的页面点")
                self.assertEqual(row.pk, pks[code], f"{code} 的 PK 变了（不该被重建）")
                self.assertEqual(row.benchmark_id, benchmark_id)

    def test_third_migration_is_safe_on_empty_table_and_re_runnable(self):
        """空表安全 + 重跑 no-op（口径与 0002/0004 一致）。"""
        PermissionNode.objects.all().delete()
        self._run_0015()
        self.assertEqual(PermissionNode.objects.count(), 0)

        dropped, survivors = self._make_customer_fixture()
        role = self._make_role("重跑收敛岗")
        role.permissions.add(*dropped, *survivors)
        self._run_0015()
        after_first = self._snapshot()
        self.assertEqual(
            PermissionNode.objects.count(), 3, "应当只剩 3 条保留点"
        )

        self._run_0015()
        self.assertEqual(self._snapshot(), after_first, "0015 重跑改动/复制了数据")

    def test_third_migration_reverse_is_registered_and_is_a_real_noop(self):
        """`0015` 的反向必须是那个显式空函数（口径与 0002/0004 一致）。"""
        operation = MIGRATION_0015.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0015.noop_reverse,
            "0015 的 RunPython reverse_code 不是那个显式的空函数",
        )

        _, survivors = self._make_customer_fixture()
        self._run_0015()
        before = self._snapshot()

        MIGRATION_0015.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0015 的反向迁移动了数据——必须是有意空操作")



#: ⭐ 自有节点的那一行（**字面值**，E-0024）：`(code, benchmark_id)`。
#: ⚠️ `benchmark_id` 是 `None`（自有节点）——这也是本迁移**必须按 `code` 匹配**的原因
#:（`filter(benchmark_id=None)` 是 `isnull` 查找，第二条自有节点一进来就 `MultipleObjectsReturned`，同 `apps/rbac/seed.py::seed_catalog` 的 docstring）。
OWN_NODE_CODE = "system.allow_taker_open_api"
OWN_NODE_BENCHMARK_ID = None
#: 它挂在哪个栏目下（对标点里那个 `system` 栏目行）。
OWN_NODE_MODULE_CODE = "system"


class TakerOpenApiNodeMigrationTests(TenantTestCase):
    """`0006_add_taker_open_api_node`：**存量租户**补上自有节点。
    判据打在**数据库行**上的理由同 `0002` / `0004`（见本文件开头）：只改 `catalog.py` 对存量租户是空操作，漏跑的症状是「界面没变」——打手进不来「打手对接」页、管理员在权限树上找不到这个点。
    三条最要紧的口径，逐条有判据：① **真把点建出来**（行存在、列值正确、挂在 `system` 栏目下）；② ⛔ **只补不改 + 一行角色授权都不碰**（先造一条同名但被改过的行 ⇒ 跑迁移后一个字段都不变）；③ **重跑 / 空表 / 反向**都必须安全（幂等口径与 `0002` / `0004` 一致）。"""

    def _make_module_row(self):
        """造 `system` 栏目行（自有节点的父级；真实库里由 `seed_catalog` 建）。"""
        return PermissionNode.objects.create(
            benchmark_id=11, code=OWN_NODE_MODULE_CODE, name="系统维护", note="管理系统设置",
            url="/System/", kind="page", level=0, order_by=0,
        )

    def _reset_like_a_tenant_that_never_ran_0006(self):
        """把库清空 ⇒ 前提是「这个租户还没有本点」（= `0006` 还没在它上面跑过）。
        ⚠️ 必须显式清空：`0006` 是**第一条会建行的数据迁移**，跑完迁移的租户 schema 里**恒有 1 行** ⇒ 不断言「表是空的」，而是断言「清空之后跑一次、点被建出来」——这样用例在**空库**与**已迁移库**上都成立（`_backfill` 用同一手法）。"""
        PermissionNode.objects.all().delete()

    def _run_0006(self):
        """跑 `0006` 的函数本体（照 `_run` / `_run_0004` 的写法）。"""
        MIGRATION_0006.add_taker_open_api_node(django_apps, None)

    def _snapshot(self):
        """本类自己的全列快照（口径同 `CatalogPruneMigrationTests._snapshot`）。"""
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    # ------------------------------------------------------------------
    # 常量层：迁移里的冻结副本必须与 `catalog.NODES` 那一行**逐字同值**
    # ------------------------------------------------------------------
    def test_the_frozen_copy_matches_the_catalog_row(self):
        """⛔ 红线：迁移里那三列文案与口径是**冻结副本**（迁移是历史记录，⛔ 不 import `apps.rbac.catalog`）⇒ 它与 `catalog.NODES` 那一行**分离时先红**（同 `0005` 的手法）。
        判据逐列钉：`code` / `benchmark_id` / `name` / `note` / `url` / `kind` / `level` / `available`；断言的字面值来自**两边**（`catalog.NODES` 与迁移常量）⇒ 「把 catalog 改坏」与「把迁移改坏」都会红。"""
        rows = [n for n in NODES if n[0] == OWN_NODE_CODE]
        self.assertEqual(len(rows), 1, f"catalog.NODES 里应当**恰好一条** {OWN_NODE_CODE}")
        code, benchmark_id, name, note, url, kind, available = rows[0]

        self.assertEqual(MIGRATION_0006.TAKER_OPEN_API_CODE, code)
        self.assertEqual(MIGRATION_0006.MODULE_CODE, OWN_NODE_MODULE_CODE)
        defaults = MIGRATION_0006.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], benchmark_id)
        self.assertIsNone(defaults["benchmark_id"], "自有节点的 benchmark_id 必须是 None")
        self.assertEqual(defaults["name"], name)
        self.assertEqual(defaults["note"], note)
        self.assertEqual(defaults["url"], url)
        self.assertEqual(defaults["kind"], kind)
        self.assertEqual(defaults["level"], 1)
        self.assertTrue(defaults["available"])
        self.assertEqual(defaults["available"], available)
        # 页面形态的冻结点：`url` 以 `/` 开头（网钉形态，见 `test_rbac_catalog.py`）。
        self.assertTrue(defaults["url"].startswith("/"))

    def test_it_depends_on_the_previous_rbac_migration(self):
        """⛔ 依赖必须指向**上一个** rbac 迁移（`0005`，⛔ 不是 `0004`、也不是别的 app 的迁移）：Django 按依赖图排 `migrate_schemas --tenant` 的顺序，指错会让本迁移抢在 `0005`（`Role.can_be_sales_agent` 那一列）之前跑——今天看不出问题，将来一旦要用新列就是 `ProgrammingError`。
        ⚠️ 本仓另有约定：**不许改既有迁移的名字** ⇒ 这里只钉「指向 `0005`」这一件事。"""
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0006.Migration.dependencies],
            [("rbac", "0005_role_can_be_sales_agent")],
        )

    # ------------------------------------------------------------------
    # 断言 ①：在**没有该节点**的租户上真把点建出来
    # ------------------------------------------------------------------
    def test_it_creates_the_node_on_a_tenant_that_lacks_it(self):
        """存量租户（目录里**没有**本点）跑一次 ⇒ 行存在、列值对、**挂在 `system` 下**。"""
        self._reset_like_a_tenant_that_never_ran_0006()
        module_row = self._make_module_row()
        other = PermissionNode.objects.create(
            benchmark_id=14, code="order.list", name="订单管理列表", note="订单管理列表",
            url="/Order/OrderList", kind="page", level=1, parent=module_row, available=True,
        )
        role = Role.objects.create(name="存量岗", group=Group.objects.create(name="存量岗"))
        role.permissions.add(module_row, other)

        self.assertFalse(PermissionNode.objects.filter(code=OWN_NODE_CODE).exists(), "前置")

        self._run_0006()

        row = PermissionNode.objects.get(code=OWN_NODE_CODE)
        self.assertIsNone(row.benchmark_id, "自有节点的 benchmark_id 必须是 NULL")
        self.assertEqual(row.name, "允许打手使用开发平台应用")
        self.assertEqual(row.note, "打手可进入「打手对接」页并为自己创建一套接入凭据")
        self.assertEqual(row.url, "/open-platform/taker-apps")
        self.assertEqual(row.kind, "page")
        self.assertEqual(row.level, 1)
        self.assertTrue(row.available)
        self.assertEqual(
            row.parent_id, module_row.pk,
            "自有节点没有挂到 `system` 栏目下 ⇒ 权限树里它渲染不出来（父级为空 = 孤儿点）",
        )

    def test_the_created_node_shows_up_in_the_permission_tree(self):
        """端到端那一半：建出来的点**真的**出现在权限树里（不是只多了一行）。
        ⚠️ 走的是**视图用的那个取数层** `apps.rbac.selectors.node_tree`（两级树由 `parent` 关系给出），⛔ 不走 HTTP：本类不建登录态，而 HTTP 层（权限门、树载荷形状）已由 `tests/test_rbac_node_tree_api.py` 单独钉住——这里只回答「迁移建的那一行在不在树上」。"""
        self._reset_like_a_tenant_that_never_ran_0006()
        self._make_module_row()
        self._run_0006()
        system = next(m for m in node_tree() if m["code"] == OWN_NODE_MODULE_CODE)
        codes = {c["code"] for c in system["children"]}
        self.assertIn(OWN_NODE_CODE, codes)

    # ------------------------------------------------------------------
    # 断言 ②：⛔ 只补不改 + 一行角色授权都不碰
    # ------------------------------------------------------------------
    def test_it_never_overwrites_an_existing_row_and_never_touches_role_assignments(self):
        """⛔ 本迁移最承重的一条：`code` 已存在 ⇒ **一个字段都不改**、角色授权**逐字不变**。
        `get_or_create` 一旦被写成 `update_or_create`，租户在角色编辑页改过的显示名、以及「谁被授过权」会被**静默回滚**——而那种回滚**不报错、界面也不提示**。"""
        self._reset_like_a_tenant_that_never_ran_0006()
        module_row = self._make_module_row()
        # 造一条"已经在库里、但被改过"的同 code 行（模拟种子先跑过 / 管理员改过显示名）。
        existing = PermissionNode.objects.create(
            benchmark_id=OWN_NODE_BENCHMARK_ID, code=OWN_NODE_CODE, name="租户改过的名字",
            note="租户改过的说明", url="/open-platform/taker-apps", kind="page",
            level=1, parent=module_row, available=False,
        )
        other = PermissionNode.objects.create(
            benchmark_id=14, code="order.list", name="订单管理列表", note="订单管理列表",
            url="/Order/OrderList", kind="page", level=1, parent=module_row, available=True,
        )
        role = Role.objects.create(name="已授权岗", group=Group.objects.create(name="已授权岗"))
        role.permissions.add(existing, other)
        before_rows = sorted(
            PermissionNode.objects.values_list(
                "id", "benchmark_id", "code", "name", "note", "url", "kind",
                "level", "parent_id", "order_by", "available",
            )
        )

        self._run_0006()

        self.assertEqual(
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            before_rows,
            "迁移改了已存在的那一行（那些列在今天都是可以被租户改的）——它必须是「只补不改」",
        )
        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)),
            sorted([OWN_NODE_CODE, "order.list"]),
            "迁移动了角色授权——它⛔ 一行 `Role.permissions` 都不许写",
        )

    # ------------------------------------------------------------------
    # 断言 ③：空表 / 重跑 / 反向
    # ------------------------------------------------------------------
    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        """新租户的顺序是「先 migrate、后 seed」⇒ 空表上不许抛；重跑必须是彻底的 no-op。
        ⚠️ 空表这一支里**父栏目也不存在** ⇒ 点仍要建出来（`parent` 留空）；⛔ 不在这里顺手建栏目行（那是 `seed_catalog` 的职责，两处都建必然漂）。"""
        self._reset_like_a_tenant_that_never_ran_0006()
        self.assertEqual(PermissionNode.objects.count(), 0)
        self._run_0006()
        row = PermissionNode.objects.get(code=OWN_NODE_CODE)
        self.assertIsNone(row.parent_id, "空表上不该凭空造出父栏目行")
        self.assertEqual(PermissionNode.objects.count(), 1, "只许建这一个点")
        after_first = self._snapshot()

        self._run_0006()

        self.assertEqual(self._snapshot(), after_first, "0006 重跑改动/复制了数据")
        self.assertEqual(PermissionNode.objects.count(), 1)

    def test_reverse_is_registered_and_is_a_real_noop(self):
        """反向必须是那个**显式空函数**，且调用它一行都不许动（口径与 `0002` / `0004` 一致）。
        反向要删这个点，会经 `Role.permissions` 的 through 表 CASCADE 掉租户已经勾好的授权，而那份信息**无从区分**「本迁移建的」与「管理员勾的」（迁移文件 docstring 已写明）。"""
        operation = MIGRATION_0006.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0006.noop_reverse,
            "0006 的 RunPython reverse_code 不是那个显式的空函数",
        )
        self._reset_like_a_tenant_that_never_ran_0006()
        self._make_module_row()
        self._run_0006()
        before = self._snapshot()

        MIGRATION_0006.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0006 的反向迁移动了数据——它必须是有意为之的空操作")


#: ⭐ 打手钱包 / 提现审核线：第五条自有节点的数据迁移。
#: 与 `0006` 同口径（给**存量租户**补点、只补不改、反向 noop）；这条点由
#: `catalog.NODES` 那一行 + 本迁移的冻结副本共同定义，两边漂了就要红。
MIGRATION_0010 = importlib.import_module(
    "apps.rbac.migrations.0010_add_taker_withdraw_review_node"
)

#: 与 `catalog.NODES` 那一行**逐字同值**的期望（字面值，E-0024；不从 catalog 派生）。
WITHDRAW_REVIEW_ROW = (
    "finance.taker_withdraw_review",
    None,
    "打手提现审核",
    "打手提现审核：查看提现申请、通过 / 驳回、发起支付宝代付、登记人工打款",
    "/api/business/withdrawals/",
    "api",
    True,
)
WITHDRAW_REVIEW_MODULE_CODE = "finance"


class TakerWithdrawReviewNodeMigrationTests(TenantTestCase):
    """`0010_add_taker_withdraw_review_node`：存量租户补上第五条自有节点。
    判据为什么打在**数据库行**上、为什么反向往回删是骗人的：与 `0006` 逐条同源（见本文件开头那段与 `0006` 那个类的 docstring）。"""

    def _reset_like_a_tenant_that_never_ran_0010(self):
        """清空目录，把「这个租户还没有本点」这个前提做成**显式**的。"""
        PermissionNode.objects.all().delete()

    def _make_finance_row(self):
        return PermissionNode.objects.create(
            benchmark_id=10, code=WITHDRAW_REVIEW_MODULE_CODE, name="财务运营",
            note="内部运营分析管理", url="/Emp/", kind="page", level=0, order_by=0,
        )

    def _run_0010(self):
        MIGRATION_0010.add_taker_withdraw_review_node(django_apps, None)

    def _snapshot(self):
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    def test_the_frozen_copy_matches_the_catalog_row(self):
        """⛔ 迁移里的文案是**冻结副本**（⛔ 不 import `apps.rbac.catalog`）⇒ 逐列对拍。"""
        rows = [n for n in NODES if n[0] == WITHDRAW_REVIEW_ROW[0]]
        self.assertEqual(len(rows), 1, f"catalog.NODES 里应当**恰好一条** {WITHDRAW_REVIEW_ROW[0]}")
        self.assertEqual(tuple(rows[0]), WITHDRAW_REVIEW_ROW)

        self.assertEqual(MIGRATION_0010.WITHDRAW_REVIEW_CODE, WITHDRAW_REVIEW_ROW[0])
        self.assertEqual(MIGRATION_0010.MODULE_CODE, WITHDRAW_REVIEW_MODULE_CODE)
        defaults = MIGRATION_0010.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], WITHDRAW_REVIEW_ROW[1])
        self.assertIsNone(defaults["benchmark_id"], "自有节点的 benchmark_id 必须是 None")
        self.assertEqual(defaults["name"], WITHDRAW_REVIEW_ROW[2])
        self.assertEqual(defaults["note"], WITHDRAW_REVIEW_ROW[3])
        self.assertEqual(defaults["url"], WITHDRAW_REVIEW_ROW[4])
        self.assertEqual(defaults["kind"], WITHDRAW_REVIEW_ROW[5])
        self.assertEqual(defaults["level"], 1)
        self.assertEqual(defaults["available"], WITHDRAW_REVIEW_ROW[6])
        self.assertTrue(defaults["url"].startswith("/"))

    def test_it_depends_on_the_previous_rbac_migration(self):
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0010.Migration.dependencies],
            [("rbac", "0009_merge_20261019_1548")],
        )

    def test_it_creates_the_node_under_finance(self):
        self._reset_like_a_tenant_that_never_ran_0010()
        module_row = self._make_finance_row()

        self.assertFalse(
            PermissionNode.objects.filter(code=WITHDRAW_REVIEW_ROW[0]).exists(), "前置"
        )
        self._run_0010()

        row = PermissionNode.objects.get(code=WITHDRAW_REVIEW_ROW[0])
        self.assertIsNone(row.benchmark_id)
        self.assertEqual(row.name, WITHDRAW_REVIEW_ROW[2])
        self.assertEqual(row.note, WITHDRAW_REVIEW_ROW[3])
        self.assertEqual(row.url, WITHDRAW_REVIEW_ROW[4])
        self.assertEqual(row.kind, WITHDRAW_REVIEW_ROW[5])
        self.assertTrue(row.available)
        self.assertEqual(
            row.parent_id, module_row.pk,
            "自有节点没有挂到 `finance` 栏目下 ⇒ 权限树里它渲染不出来（父级为空 = 孤儿点）",
        )

    def test_it_only_adds_and_never_touches_role_assignments(self):
        """⛔ `code` 已存在 ⇒ 一个字段都不改、角色授权逐字不变（同 `0006` 口径）。"""
        self._reset_like_a_tenant_that_never_ran_0010()
        module_row = self._make_finance_row()
        existing = PermissionNode.objects.create(
            benchmark_id=None, code=WITHDRAW_REVIEW_ROW[0], name="租户改过的名字",
            note="租户改过的说明", url=WITHDRAW_REVIEW_ROW[4], kind="api",
            level=1, parent=module_row, available=False,
        )
        role = Role.objects.create(name="财务岗", group=Group.objects.create(name="财务岗"))
        role.permissions.add(existing)
        before = self._snapshot()

        self._run_0010()

        self.assertEqual(self._snapshot(), before, "迁移改了已存在的那一行 / 动了角色授权")

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        self._reset_like_a_tenant_that_never_ran_0010()
        self._run_0010()
        row = PermissionNode.objects.get(code=WITHDRAW_REVIEW_ROW[0])
        self.assertIsNone(row.parent_id, "空表上不该凭空造出父栏目行")
        after_first = self._snapshot()

        self._run_0010()

        self.assertEqual(self._snapshot(), after_first, "0010 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0010.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0010.noop_reverse,
            "0010 的 RunPython reverse_code 不是那个显式的空函数",
        )
        self._reset_like_a_tenant_that_never_ran_0010()
        self._make_finance_row()
        self._run_0010()
        before = self._snapshot()

        MIGRATION_0010.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0010 的反向迁移动了数据——它必须是有意为之的空操作")


#: ⭐ 客服后台打手钱包管理线（`feat/taker-wallet-adjust`）：第六条自有节点的数据迁移。
#: 与 `0006` / `0010` 同口径（给**存量租户**补点、只补不改、反向 noop）；这条点由
#: `catalog.NODES` 那一行 + 本迁移的冻结副本共同定义，两边漂了就要红。
MIGRATION_0011 = importlib.import_module(
    "apps.rbac.migrations.0011_add_taker_wallet_adjust_node"
)

#: 与 `catalog.NODES` 那一行**逐字同值**的期望（字面值，E-0024；不从 catalog 派生）。
WALLET_ADJUST_ROW = (
    "finance.taker_wallet_adjust",
    None,
    "打手钱包与押金管理",
    "打手钱包管理：查看余额 / 押金冻结、手工加减款；按游戏维护押金金额（影响谁能接单）",
    "/api/business/wallets/",
    "api",
    True,
)
WALLET_ADJUST_MODULE_CODE = "finance"


class TakerWalletAdjustNodeMigrationTests(TenantTestCase):
    """`0011_add_taker_wallet_adjust_node`：存量租户补上第六条自有节点。
    判据为什么打在**数据库行**上、为什么反向往回删是骗人的：与 `0006` 逐条同源（见本文件开头那段与 `0006` 那个类的 docstring）。"""

    def _reset_like_a_tenant_that_never_ran_0011(self):
        """清空目录，把「这个租户还没有本点」这个前提做成**显式**的。"""
        PermissionNode.objects.all().delete()

    def _make_finance_row(self):
        return PermissionNode.objects.create(
            benchmark_id=10, code=WALLET_ADJUST_MODULE_CODE, name="财务运营",
            note="内部运营分析管理", url="/Emp/", kind="page", level=0, order_by=0,
        )

    def _run_0011(self):
        MIGRATION_0011.add_taker_wallet_adjust_node(django_apps, None)

    def _snapshot(self):
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    def test_the_frozen_copy_matches_the_catalog_row(self):
        """⛔ 迁移里的文案是**冻结副本**（⛔ 不 import `apps.rbac.catalog`）⇒ 逐列对拍。"""
        rows = [n for n in NODES if n[0] == WALLET_ADJUST_ROW[0]]
        self.assertEqual(len(rows), 1, f"catalog.NODES 里应当**恰好一条** {WALLET_ADJUST_ROW[0]}")
        self.assertEqual(tuple(rows[0]), WALLET_ADJUST_ROW)

        self.assertEqual(MIGRATION_0011.WALLET_ADJUST_CODE, WALLET_ADJUST_ROW[0])
        self.assertEqual(MIGRATION_0011.MODULE_CODE, WALLET_ADJUST_MODULE_CODE)
        defaults = MIGRATION_0011.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], WALLET_ADJUST_ROW[1])
        self.assertIsNone(defaults["benchmark_id"], "自有节点的 benchmark_id 必须是 None")
        self.assertEqual(defaults["name"], WALLET_ADJUST_ROW[2])
        self.assertEqual(defaults["note"], WALLET_ADJUST_ROW[3])
        self.assertEqual(defaults["url"], WALLET_ADJUST_ROW[4])
        self.assertEqual(defaults["kind"], WALLET_ADJUST_ROW[5])
        self.assertEqual(defaults["level"], 1)
        self.assertEqual(defaults["available"], WALLET_ADJUST_ROW[6])
        self.assertTrue(defaults["url"].startswith("/"))

    def test_it_depends_on_the_previous_rbac_migration(self):
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0011.Migration.dependencies],
            [("rbac", "0010_add_taker_withdraw_review_node")],
        )

    def test_it_creates_the_node_under_finance(self):
        self._reset_like_a_tenant_that_never_ran_0011()
        module_row = self._make_finance_row()

        self.assertFalse(
            PermissionNode.objects.filter(code=WALLET_ADJUST_ROW[0]).exists(), "前置"
        )
        self._run_0011()

        row = PermissionNode.objects.get(code=WALLET_ADJUST_ROW[0])
        self.assertIsNone(row.benchmark_id)
        self.assertEqual(row.name, WALLET_ADJUST_ROW[2])
        self.assertEqual(row.note, WALLET_ADJUST_ROW[3])
        self.assertEqual(row.url, WALLET_ADJUST_ROW[4])
        self.assertEqual(row.kind, WALLET_ADJUST_ROW[5])
        self.assertTrue(row.available)
        self.assertEqual(
            row.parent_id, module_row.pk,
            "自有节点没有挂到 `finance` 栏目下 ⇒ 权限树里它渲染不出来（父级为空 = 孤儿点）",
        )

    def test_it_only_adds_and_never_touches_role_assignments(self):
        """⛔ `code` 已存在 ⇒ 一个字段都不改、角色授权逐字不变（同 `0006` / `0010` 口径）。"""
        self._reset_like_a_tenant_that_never_ran_0011()
        module_row = self._make_finance_row()
        existing = PermissionNode.objects.create(
            benchmark_id=None, code=WALLET_ADJUST_ROW[0], name="租户改过的名字",
            note="租户改过的说明", url=WALLET_ADJUST_ROW[4], kind="api",
            level=1, parent=module_row, available=False,
        )
        role = Role.objects.create(name="财务岗", group=Group.objects.create(name="财务岗"))
        role.permissions.add(existing)
        before = self._snapshot()

        self._run_0011()

        self.assertEqual(self._snapshot(), before, "迁移改了已存在的那一行 / 动了角色授权")

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        self._reset_like_a_tenant_that_never_ran_0011()
        self._run_0011()
        row = PermissionNode.objects.get(code=WALLET_ADJUST_ROW[0])
        self.assertIsNone(row.parent_id, "空表上不该凭空造出父栏目行")
        after_first = self._snapshot()

        self._run_0011()

        self.assertEqual(self._snapshot(), after_first, "0011 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0011.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0011.noop_reverse,
            "0011 的 RunPython reverse_code 不是那个显式的空函数",
        )
        self._reset_like_a_tenant_that_never_ran_0011()
        self._make_finance_row()
        self._run_0011()
        before = self._snapshot()

        MIGRATION_0011.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0011 的反向迁移动了数据——它必须是有意为之的空操作")


#: ⭐ 频道文章作品线：**第八条自有节点**的数据迁移。
#: 与 `0006` / `0011` 同口径（给**存量租户**补点、只补不改、反向 noop）；这条点由
#: `catalog.NODES` 那一行 + 本迁移的冻结副本共同定义，两边漂了就要红。
MIGRATION_0013 = importlib.import_module(
    "apps.rbac.migrations.0013_add_system_article_manage_node"
)

#: 与 `catalog.NODES` 那一行**逐字同值**的期望（字面值，E-0024；不从 catalog 派生）。
ARTICLE_MANAGE_ROW = (
    "system.article_manage",
    None,
    "发布文章作品",
    "编辑、发布、下架工作室频道文章",
    "/app-mp/articles",
    "page",
    True,
)
ARTICLE_MANAGE_MODULE_CODE = "system"


class ChannelArticleNodeMigrationTests(TenantTestCase):
    """`0013_add_system_article_manage_node`：存量租户补上第八条自有节点。
    判据为什么打在**数据库行**上、为什么反向往回删是骗人的：与 `0006` 逐条同源（见本文件开头那段与 `0006` 那个类的 docstring）。"""

    def _reset_like_a_tenant_that_never_ran_0013(self):
        """清空目录，把「这个租户还没有本点」这个前提做成**显式**的。"""
        PermissionNode.objects.all().delete()

    def _make_system_row(self):
        return PermissionNode.objects.create(
            benchmark_id=11, code=ARTICLE_MANAGE_MODULE_CODE, name="系统维护",
            note="管理系统设置", url="/System/", kind="page", level=0, order_by=0,
        )

    def _run_0013(self):
        MIGRATION_0013.add_system_article_manage_node(django_apps, None)

    def _snapshot(self):
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    def test_the_frozen_copy_matches_the_catalog_row(self):
        """⛔ 迁移里的文案是**冻结副本**（⛔ 不 import `apps.rbac.catalog`）⇒ 逐列对拍。"""
        rows = [n for n in NODES if n[0] == ARTICLE_MANAGE_ROW[0]]
        self.assertEqual(len(rows), 1, f"catalog.NODES 里应当**恰好一条** {ARTICLE_MANAGE_ROW[0]}")
        self.assertEqual(tuple(rows[0]), ARTICLE_MANAGE_ROW)

        self.assertEqual(MIGRATION_0013.ARTICLE_MANAGE_CODE, ARTICLE_MANAGE_ROW[0])
        self.assertEqual(MIGRATION_0013.MODULE_CODE, ARTICLE_MANAGE_MODULE_CODE)
        defaults = MIGRATION_0013.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], ARTICLE_MANAGE_ROW[1])
        self.assertIsNone(defaults["benchmark_id"], "自有节点的 benchmark_id 必须是 None")
        self.assertEqual(defaults["name"], ARTICLE_MANAGE_ROW[2])
        self.assertEqual(defaults["note"], ARTICLE_MANAGE_ROW[3])
        self.assertEqual(defaults["url"], ARTICLE_MANAGE_ROW[4])
        self.assertEqual(defaults["kind"], ARTICLE_MANAGE_ROW[5])
        self.assertEqual(defaults["level"], 1)
        self.assertEqual(defaults["available"], ARTICLE_MANAGE_ROW[6])
        self.assertTrue(defaults["url"].startswith("/"))

    def test_it_depends_on_the_previous_rbac_migration(self):
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0013.Migration.dependencies],
            [("rbac", "0012_add_xgj_receipt_detail_node")],
        )

    def test_it_creates_the_node_under_system(self):
        self._reset_like_a_tenant_that_never_ran_0013()
        module_row = self._make_system_row()

        self.assertFalse(
            PermissionNode.objects.filter(code=ARTICLE_MANAGE_ROW[0]).exists(), "前置"
        )
        self._run_0013()

        row = PermissionNode.objects.get(code=ARTICLE_MANAGE_ROW[0])
        self.assertIsNone(row.benchmark_id)
        self.assertEqual(row.name, ARTICLE_MANAGE_ROW[2])
        self.assertEqual(row.note, ARTICLE_MANAGE_ROW[3])
        self.assertEqual(row.url, ARTICLE_MANAGE_ROW[4])
        self.assertEqual(row.kind, ARTICLE_MANAGE_ROW[5])
        self.assertTrue(row.available)
        self.assertEqual(
            row.parent_id, module_row.pk,
            "自有节点没有挂到 `system` 栏目下 ⇒ 权限树里它渲染不出来（父级为空 = 孤儿点）",
        )

    def test_it_only_adds_and_never_touches_role_assignments(self):
        """⛔ `code` 已存在 ⇒ 一个字段都不改、角色授权逐字不变（同 `0006` / `0011` 口径）。"""
        self._reset_like_a_tenant_that_never_ran_0013()
        module_row = self._make_system_row()
        existing = PermissionNode.objects.create(
            benchmark_id=None, code=ARTICLE_MANAGE_ROW[0], name="租户改过的名字",
            note="租户改过的说明", url=ARTICLE_MANAGE_ROW[4], kind="page",
            level=1, parent=module_row, available=False,
        )
        role = Role.objects.create(name="客服岗", group=Group.objects.create(name="客服岗"))
        role.permissions.add(existing)
        before = self._snapshot()

        self._run_0013()

        self.assertEqual(self._snapshot(), before, "迁移改了已存在的那一行 / 动了角色授权")

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        self._reset_like_a_tenant_that_never_ran_0013()
        self._run_0013()
        row = PermissionNode.objects.get(code=ARTICLE_MANAGE_ROW[0])
        self.assertIsNone(row.parent_id, "空表上不该凭空造出父栏目行")
        after_first = self._snapshot()

        self._run_0013()

        self.assertEqual(self._snapshot(), after_first, "0013 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0013.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0013.noop_reverse,
            "0013 的 RunPython reverse_code 不是那个显式的空函数",
        )
        self._reset_like_a_tenant_that_never_ran_0013()
        self._make_system_row()
        self._run_0013()
        before = self._snapshot()

        MIGRATION_0013.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0013 的反向迁移动了数据——它必须是有意为之的空操作")


#: ⭐ 交易平台涉钱写动作线（审计 M5）：**第九条自有节点**的数据迁移。
#: 与 `0006` / `0013` 同口径（给**存量租户**补点、只补不改、反向 noop）；这条点由
#: `catalog.NODES` 那一行 + 本迁移的冻结副本共同定义，两边漂了就要红。
MIGRATION_0014 = importlib.import_module(
    "apps.rbac.migrations.0014_add_order_platform_write_node"
)

#: 与 `catalog.NODES` 那一行**逐字同值**的期望（字面值，E-0024；不从 catalog 派生）。
PLATFORM_WRITE_ROW = (
    "order.platform_write",
    None,
    "交易平台涉钱写操作",
    "对交易平台订单执行涉钱 / 写动作（确认完单、同意撤销、补款、重发、加价改价、"
    "编辑、撤回、模板维护等）的提交请求；只读查询仍用订单列表权限",
    "/api/business/orders/<order_no>/platform-push/",
    "api",
    True,
)
PLATFORM_WRITE_MODULE_CODE = "order"


class PlatformWriteNodeMigrationTests(TenantTestCase):
    """`0014_add_order_platform_write_node`：存量租户补上第九条自有节点（判据打在**数据库行**上、反向不删，同 `0006` / `0013` 的理由）。"""

    def _reset_like_a_tenant_that_never_ran_0014(self):
        PermissionNode.objects.all().delete()

    def _make_order_row(self):
        return PermissionNode.objects.create(
            benchmark_id=5, code=PLATFORM_WRITE_MODULE_CODE, name="订单业务",
            note="订单业务管理", url="/Order/", kind="page", level=0, order_by=0,
        )

    def _run_0014(self):
        MIGRATION_0014.add_order_platform_write_node(django_apps, None)

    def _snapshot(self):
        return (
            sorted(
                PermissionNode.objects.values_list(
                    "id", "benchmark_id", "code", "name", "note", "url", "kind",
                    "level", "parent_id", "order_by", "available",
                )
            ),
            sorted(
                (role_id, node_id)
                for role_id, node_id in Role.permissions.through.objects.values_list(
                    "role_id", "permissionnode_id"
                )
            ),
        )

    def test_the_frozen_copy_matches_the_catalog_row(self):
        rows = [n for n in NODES if n[0] == PLATFORM_WRITE_ROW[0]]
        self.assertEqual(len(rows), 1, f"catalog.NODES 里应当**恰好一条** {PLATFORM_WRITE_ROW[0]}")
        self.assertEqual(tuple(rows[0]), PLATFORM_WRITE_ROW)

        self.assertEqual(MIGRATION_0014.PLATFORM_WRITE_CODE, PLATFORM_WRITE_ROW[0])
        self.assertEqual(MIGRATION_0014.MODULE_CODE, PLATFORM_WRITE_MODULE_CODE)
        defaults = MIGRATION_0014.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], PLATFORM_WRITE_ROW[1])
        self.assertIsNone(defaults["benchmark_id"], "自有节点的 benchmark_id 必须是 None")
        self.assertEqual(defaults["name"], PLATFORM_WRITE_ROW[2])
        self.assertEqual(defaults["note"], PLATFORM_WRITE_ROW[3])
        self.assertEqual(defaults["url"], PLATFORM_WRITE_ROW[4])
        self.assertEqual(defaults["kind"], PLATFORM_WRITE_ROW[5])
        self.assertEqual(defaults["level"], 1)
        self.assertEqual(defaults["available"], PLATFORM_WRITE_ROW[6])
        self.assertTrue(defaults["url"].startswith("/"))

    def test_it_depends_on_the_previous_rbac_migration(self):
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0014.Migration.dependencies],
            [("rbac", "0013_add_system_article_manage_node")],
        )

    def test_it_creates_the_node_under_order(self):
        self._reset_like_a_tenant_that_never_ran_0014()
        module_row = self._make_order_row()

        self.assertFalse(
            PermissionNode.objects.filter(code=PLATFORM_WRITE_ROW[0]).exists(), "前置"
        )
        self._run_0014()

        row = PermissionNode.objects.get(code=PLATFORM_WRITE_ROW[0])
        self.assertIsNone(row.benchmark_id)
        self.assertEqual(row.name, PLATFORM_WRITE_ROW[2])
        self.assertEqual(row.note, PLATFORM_WRITE_ROW[3])
        self.assertEqual(row.url, PLATFORM_WRITE_ROW[4])
        self.assertEqual(row.kind, PLATFORM_WRITE_ROW[5])
        self.assertTrue(row.available)
        self.assertEqual(
            row.parent_id, module_row.pk,
            "自有节点没有挂到 `order` 栏目下 ⇒ 权限树里它渲染不出来（父级为空 = 孤儿点）",
        )

    def _make_order_list_node(self, module_row):
        return PermissionNode.objects.create(
            benchmark_id=13, code=MIGRATION_0014.GRANT_ANCHOR_CODE, name="订单管理列表",
            note="订单管理列表", url="/Order/List", kind="page", level=1,
            parent=module_row, available=True,
        )

    def _make_role(self, name):
        return Role.objects.create(name=name, group=Group.objects.create(name=name))

    def test_it_never_overwrites_an_existing_node_row(self):
        """⛔ 节点 `code` 已存在 ⇒ 那一行一个字段都不改（同 `0006` / `0013` 口径）。"""
        self._reset_like_a_tenant_that_never_ran_0014()
        module_row = self._make_order_row()
        existing = PermissionNode.objects.create(
            benchmark_id=None, code=PLATFORM_WRITE_ROW[0], name="租户改过的名字",
            note="租户改过的说明", url=PLATFORM_WRITE_ROW[4], kind="api",
            level=1, parent=module_row, available=False,
        )
        before = tuple(
            PermissionNode.objects.filter(pk=existing.pk).values_list(
                "benchmark_id", "code", "name", "note", "url", "kind",
                "level", "parent_id", "order_by", "available",
            )
        )

        self._run_0014()

        after = tuple(
            PermissionNode.objects.filter(pk=existing.pk).values_list(
                "benchmark_id", "code", "name", "note", "url", "kind",
                "level", "parent_id", "order_by", "available",
            )
        )
        self.assertEqual(after, before, "迁移改了已存在的那一行（租户可能改过显示名）")

    def test_it_grants_the_new_node_to_roles_that_already_have_order_list(self):
        """⭐ 上线不锁死客服：写端点的门从 `order.list` 换到本点 ⇒ 存量角色都没有它，不补授就**全部 403**。
        判据两向：有 `order.list` 的角色**拿到**；没有它的角色**一个都不给**。"""
        self._reset_like_a_tenant_that_never_ran_0014()
        module_row = self._make_order_row()
        anchor = self._make_order_list_node(module_row)
        with_list = self._make_role("有单列")
        without_list = self._make_role("无单列")
        with_list.permissions.add(anchor)
        # 无 `order.list` 的角色只拿一个无关点（证明它确实不在判据里）。
        other = PermissionNode.objects.create(
            benchmark_id=9999, code="my_profile.edit", name="编辑我的资料",
            note="编辑我的资料", url="/User/MyInfo", kind="api", level=1,
            parent=module_row, available=True,
        )
        without_list.permissions.add(other)

        self._run_0014()

        node = PermissionNode.objects.get(code=PLATFORM_WRITE_ROW[0])
        self.assertTrue(
            with_list.permissions.filter(pk=node.pk).exists(),
            "已有 order.list 的角色没拿到新节点 ⇒ 上线当天客服会被 403 锁死",
        )
        self.assertFalse(
            without_list.permissions.filter(pk=node.pk).exists(),
            "没有 order.list 的角色不该被补授（那不是「谁都给」）",
        )
        self.assertTrue(without_list.permissions.filter(pk=other.pk).exists(), "别的授权被动了")

        # 幂等：重跑不重复、不报错。
        before = self._snapshot()
        self._run_0014()
        self.assertEqual(self._snapshot(), before, "0014 重跑改了角色授权")

    def test_it_does_nothing_when_order_list_does_not_exist(self):
        """目录还没灌过（没有 `order.list`）⇒ 不补授任何角色，也不造栏目行。"""
        self._reset_like_a_tenant_that_never_ran_0014()
        module_row = self._make_order_row()
        role = self._make_role("空目录")
        other = PermissionNode.objects.create(
            benchmark_id=9998, code="my_profile.edit", name="编辑我的资料",
            note="编辑我的资料", url="/User/MyInfo", kind="api", level=1,
            parent=module_row, available=True,
        )
        role.permissions.add(other)
        before = self._snapshot()

        self._run_0014()

        node = PermissionNode.objects.get(code=PLATFORM_WRITE_ROW[0])
        self.assertFalse(role.permissions.filter(pk=node.pk).exists())
        self.assertEqual(
            PermissionNode.objects.filter(code=PLATFORM_WRITE_ROW[0]).count(), 1
        )

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        self._reset_like_a_tenant_that_never_ran_0014()
        self._run_0014()
        row = PermissionNode.objects.get(code=PLATFORM_WRITE_ROW[0])
        self.assertIsNone(row.parent_id, "空表上不该凭空造出父栏目行")
        after_first = self._snapshot()

        self._run_0014()

        self.assertEqual(self._snapshot(), after_first, "0014 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0014.Migration.operations[0]
        self.assertIs(
            operation.reverse_code, MIGRATION_0014.noop_reverse,
            "0014 的 RunPython reverse_code 不是那个显式的空函数",
        )
        self._reset_like_a_tenant_that_never_ran_0014()
        self._make_order_row()
        self._run_0014()
        before = self._snapshot()

        MIGRATION_0014.noop_reverse(django_apps, None)

        self.assertEqual(self._snapshot(), before, "0014 的反向迁移动了数据——它必须是有意为之的空操作")


class CustomerSelfOnlyNodeMigrationTests(TenantTestCase):
    """`0016_add_customer_member_list_self_only_node`（客户线第五批，2026-11-11）：
    与 `0006`（打手开放接口）同形状的**补点**迁移，判据照 `TakerOpenApiNodeMigrationTests` 写。

    ⚠️ 与 `0006` 的涉钱补授**不同**：本点是**限制**节点 ⇒ ⛔ 一行角色授权都不碰
    （默认谁都不给才是安全态，发放是管理员的决定）。
    """

    def _make_module_row(self):
        """造 `customer` 栏目行（父级；真实库里由 `seed_catalog` 建）。"""
        return PermissionNode.objects.create(
            benchmark_id=137, code="customer", name="客户维护", note="客户维护模块",
            url="/Customer/", kind="page", level=0, order_by=0,
        )

    def _run_0016(self):
        MIGRATION_0016.add_customer_member_list_self_only_node(django_apps, None)

    def test_the_frozen_copy_matches_the_catalog_row(self):
        """迁移里的冻结副本与 `catalog.NODES` 那一行逐字同值（⛔ 不 import catalog 的手法）。"""
        rows = [n for n in NODES if n[0] == "customer.member_list_self_only"]
        self.assertEqual(len(rows), 1, "catalog.NODES 里应当恰好一条 member_list_self_only")
        code, benchmark_id, name, note, url, kind, available = rows[0]
        self.assertEqual(MIGRATION_0016.SELF_ONLY_CODE, code)
        self.assertEqual(MIGRATION_0016.MODULE_CODE, "customer")
        defaults = MIGRATION_0016.NODE_DEFAULTS
        self.assertEqual(defaults["benchmark_id"], benchmark_id)      # None（自有节点）
        self.assertEqual(defaults["name"], name)
        self.assertEqual(defaults["note"], note)
        self.assertEqual(defaults["url"], url)
        self.assertEqual(defaults["kind"], kind)
        self.assertEqual(defaults["available"], available)

    def test_it_depends_on_the_previous_rbac_migration(self):
        deps = MIGRATION_0016.Migration.dependencies
        self.assertIn(("rbac", "0015_prune_customer_module_nodes"), deps)

    def test_it_creates_the_flag_node_under_customer(self):
        """真把点建出来：行存在、挂在 `customer` 栏目下、kind=flag、available=True。

        ⚠️ 先清空整表：测试库建库时迁移已跑过（彼时 `customer` 栏目行还不存在 ⇒
        那一版节点 parent=NULL），不清空 `get_or_create` 会命中旧行、断言 parent 失真。
        """
        PermissionNode.objects.all().delete()
        self._make_module_row()
        self._run_0016()
        row = PermissionNode.objects.get(code="customer.member_list_self_only")
        self.assertIsNone(row.benchmark_id)
        self.assertEqual(row.parent.code, "customer")
        self.assertEqual(row.kind, "flag")
        self.assertEqual(row.level, 1)
        self.assertTrue(row.available)
        self.assertEqual(row.url, "")

    def test_it_never_touches_role_assignments(self):
        """限制节点：迁移建点前后，任何角色的授权一行都不变（发放是管理员的业务决定）。"""
        # ⚠️ 先删翻真行：2026-11-11 起 `0017` 在建库迁移时已按 code 建出 141 ⇒
        # 不删的话这里的 `create` 撞 `benchmark_id` 唯一约束。
        PermissionNode.objects.filter(code="customer.member_list").delete()
        self._make_module_row()
        node_other = PermissionNode.objects.create(
            benchmark_id=141, code="customer.member_list", name="会员管理", note="",
            url="/Customer/CustomerList", kind="page", level=1, available=False,
        )
        role = self._make_role("限制岗") if hasattr(self, "_make_role") else None
        if role is None:
            from django.contrib.auth.models import Group

            role = Role.objects.create(
                name="限制岗", group=Group.objects.create(name="限制岗")
            )
        role.permissions.add(node_other)
        before = sorted(role.permissions.values_list("code", flat=True))

        self._run_0016()

        self.assertEqual(
            sorted(role.permissions.values_list("code", flat=True)), before,
            "0016 动了角色授权——限制节点默认谁都不给才是安全态",
        )
        self.assertFalse(
            role.permissions.filter(code="customer.member_list_self_only").exists()
        )

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        """空表（新租户先 migrate 后 seed 的形状）安全 + 重跑幂等。"""
        PermissionNode.objects.all().delete()
        self._run_0016()
        self.assertEqual(PermissionNode.objects.count(), 1)
        after_first = self._snapshot()
        self._run_0016()
        self.assertEqual(self._snapshot(), after_first, "0016 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0016.Migration.operations[0]
        self.assertIs(operation.reverse_code, MIGRATION_0016.noop_reverse)

        self._make_module_row()
        self._run_0016()
        before = self._snapshot()
        MIGRATION_0016.noop_reverse(django_apps, None)
        self.assertEqual(self._snapshot(), before, "0016 的反向迁移动了数据")

    def _snapshot(self):
        """全列快照（口径同 `CatalogPruneMigrationTests._snapshot`）。"""
        return sorted(
            PermissionNode.objects.values_list(
                "id", "benchmark_id", "code", "name", "note", "url", "kind",
                "level", "parent_id", "order_by", "available",
            )
        )


#: ⭐ 客户线翻真（2026-11-11）：给存量租户传播两点改名 + 翻真的迁移（`0017`）。
MIGRATION_0017 = importlib.import_module(
    "apps.rbac.migrations.0017_rename_and_flip_customer_member_nodes"
)


class CustomerRenameFlipMigrationTests(TenantTestCase):
    """`0017_rename_and_flip_customer_member_nodes`（客户线翻真，2026-11-11）：

    与 `0002` 的「105 就地改名」同族（改名**不删行** ⇒ PK 不变、已授的角色不丢点），
    判据照 `CustomerSelfOnlyNodeMigrationTests` / `CatalogPruneMigrationTests` 的手法写。
    """

    def _make_module_row(self):
        """造 `customer` 栏目行（父级；真实库里由 `seed_catalog` 建）。"""
        return PermissionNode.objects.create(
            benchmark_id=137, code="customer", name="客户维护", note="客户维护模块",
            url="/Customer/", kind="page", level=0, order_by=0,
        )

    def _make_old_row(self, benchmark_id, code, name, note):
        """造一条**翻真前**的旧值行（模拟存量租户：seed 之后没人重灌）。"""
        return PermissionNode.objects.create(
            benchmark_id=benchmark_id, code=code, name=name, note=note,
            kind="page", level=1, order_by=0, available=False,
        )

    def _run_0017(self):
        MIGRATION_0017.rename_and_flip_customer_member_nodes(django_apps, None)

    def test_the_frozen_copies_match_the_catalog_rows(self):
        """迁移里的冻结副本与 `catalog.NODES` 那两行逐字同值（⛔ 不 import catalog 的手法）。"""
        self.assertEqual(
            {code for code, _defaults in MIGRATION_0017.FROZEN_NODES},
            {"customer.member_list", "customer.member_analysis_report"},
            "0017 的传播面变了——冻结副本与断言要同一次改",
        )
        for code, defaults_src in MIGRATION_0017.FROZEN_NODES:
            rows = [n for n in NODES if n[0] == code]
            self.assertEqual(len(rows), 1, f"catalog.NODES 里应当恰好一条 {code}")
            _c, bid, name, note, url, kind, available = rows[0]
            defaults = dict(defaults_src)
            self.assertEqual(defaults["benchmark_id"], bid)
            self.assertEqual(defaults["name"], name)
            self.assertEqual(defaults["note"], note)
            self.assertEqual(defaults["url"], url)
            self.assertEqual(defaults["kind"], kind)
            self.assertEqual(defaults["available"], available)

    def test_it_depends_on_the_previous_rbac_migration(self):
        deps = MIGRATION_0017.Migration.dependencies
        self.assertIn(("rbac", "0016_add_customer_member_list_self_only_node"), deps)

    def test_it_renames_flips_in_place_and_keeps_role_assignments(self):
        """存量旧行：name / note / available 就地更新（**PK 不变**）、授权关系原样保留。"""
        # ⚠️ 先清空整表：跑完迁移的租户里 0017 已经建过同名行（不清空撞唯一约束）。
        PermissionNode.objects.all().delete()
        self._make_module_row()
        old_list = self._make_old_row(
            141, "customer.member_list", "会员管理", "勾选此项后代表此职位的人可以进入会员客户管理列表",
        )
        old_report = self._make_old_row(
            1226, "customer.member_analysis_report", "会员分析报表", "进入会员分析的页面",
        )
        role = Role.objects.create(name="客服乙", group=Group.objects.create(name="客服乙"))
        role.permissions.add(old_list, old_report)

        self._run_0017()

        new_list = PermissionNode.objects.get(code="customer.member_list")
        new_report = PermissionNode.objects.get(code="customer.member_analysis_report")
        self.assertEqual(new_list.pk, old_list.pk, "141 被删行重建了——授权会随级联丢")
        self.assertEqual(new_report.pk, old_report.pk, "1226 被删行重建了——授权会随级联丢")
        self.assertEqual(new_list.name, "客户管理")
        self.assertEqual(new_list.note, "勾选此项后代表此职位的人可以进入客户管理列表")
        self.assertTrue(new_list.available)
        self.assertEqual(new_list.parent.code, "customer")
        self.assertEqual(new_report.name, "客户分析报表")
        self.assertTrue(new_report.available)
        self.assertEqual(
            set(role.permissions.values_list("code", flat=True)),
            {"customer.member_list", "customer.member_analysis_report"},
            "0017 动了角色授权——本迁移只改点自身、不碰发放",
        )

    def test_it_is_safe_on_an_empty_catalog_and_is_re_runnable(self):
        """空表（新租户先 migrate 后 seed 的形状）安全 + 重跑幂等。"""
        PermissionNode.objects.all().delete()
        self._run_0017()
        self.assertEqual(PermissionNode.objects.count(), 2)
        after_first = self._snapshot()
        self._run_0017()
        self.assertEqual(self._snapshot(), after_first, "0017 重跑改动/复制了数据")

    def test_reverse_is_registered_and_is_a_real_noop(self):
        operation = MIGRATION_0017.Migration.operations[0]
        self.assertIs(operation.reverse_code, MIGRATION_0017.noop_reverse)

        PermissionNode.objects.all().delete()
        self._make_module_row()
        self._make_old_row(141, "customer.member_list", "会员管理", "旧 note")
        self._run_0017()
        before = self._snapshot()
        MIGRATION_0017.noop_reverse(django_apps, None)
        self.assertEqual(self._snapshot(), before, "0017 的反向迁移动了数据")

    def _snapshot(self):
        """全列快照（口径同 `CustomerSelfOnlyNodeMigrationTests._snapshot`）。"""
        return sorted(
            PermissionNode.objects.values_list(
                "id", "benchmark_id", "code", "name", "note", "url", "kind",
                "level", "parent_id", "order_by", "available",
            )
        )
