"""目录与内置角色种子的**幂等**（spec §10-A3）。"""
from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.platform.provisioning import provision_tenant
from apps.rbac.catalog import MODULES, NODES
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_builtin_roles, seed_catalog, seed_rbac

BUILTIN = ("客服", "客服主管", "代练员", "系统管理员", "系统开发员", "人事")

#: 某几个**内置角色默认权限条目**在 `seed_builtin_roles` 下的**预期权限行数**。
#: 条目有两种形态（与 `BUILTIN_ROLE_MODULES` 的写法一一对应）：
#: * **栏目 code**（如 `"my_orders"`）⇒ 栏目行自身 + 它下面**全部非 `flag`** 子节点；
#: * **单个功能点 code**（如 `"order.submit_work"`）⇒ **只算它自己一行**，
#: ⛔ **不按点号前缀展开**（把叶子当栏目会让 `order` 前缀把整个「订单业务」栏目一起算进来）。
#: 「是不是栏目」以 `MODULES` 那份权威清单为准，**不用字符串前缀猜**。
#: ⚠️ 排除 `flag` 的口径：`flag` 全是限制性动作，
#: 按模块批发等于给客服套上"禁止录入新订单"，与 spec §6.1 的 remark 原文正好相反。
#: 这里仍用**逐条相等**的判据（不是 `> 0` / `< 全量`），只是语料换成了非 flag 那一半。
def _default_permission_count(entries):
    module_codes = {code for code, *_rest in MODULES}
    total = 0
    for entry in entries:
        total += 1  # 该条目自己在库里占一行（栏目行，或单点行）
        if entry in module_codes:
            total += sum(
                1 for _code, _bid, _name, _note, _url, kind, _avail in NODES
                if _code.split(".")[0] == entry and kind != NodeKind.FLAG
            )
    return total



class SeedTests(TenantTestCase):
    def test_seed_creates_all_nodes_with_parents(self):
        seed_catalog()
        self.assertEqual(PermissionNode.objects.count(), len(NODES) + len(MODULES))
        self.assertEqual(PermissionNode.objects.filter(level=0).count(), 13)
        # 每个功能点都必须挂到一个栏目上——孤儿点会让权限树渲染不出来
        self.assertEqual(
            PermissionNode.objects.filter(level=1, parent__isnull=True).count(), 0
        )
        node = PermissionNode.objects.get(code="order.list")
        self.assertEqual(node.parent.code, "order")
        self.assertEqual(node.kind, NodeKind.PAGE)

    def test_seed_is_idempotent_and_does_not_touch_assignments(self):
        seed_catalog()
        role = Role.objects.create(
            name="客服", group=Group.objects.create(name="客服")
        )
        role.permissions.set(PermissionNode.objects.filter(code="order.list"))
        snapshot = PermissionNode.objects.count()

        seed_catalog()  # 第二次
        self.assertEqual(PermissionNode.objects.count(), snapshot, "重复种子产生了新行")
        self.assertEqual(
            list(role.permissions.values_list("code", flat=True)), ["order.list"],
            "重复种子改动了已有分配",
        )

    def test_two_own_nodes_coexist_without_blowing_up(self):
        """⭐ **T4（2026-10-08）新增**：`benchmark_id is None` 的节点**按 `code` upsert**。

    **为什么必须有这一条**（它挡的是一个**只在第二条自有节点进来时才出现**的缺陷）：
    `seed_catalog` 原先一律 `update_or_create(benchmark_id=…)`，而 `benchmark_id=None`
    在 ORM 里是 `filter(benchmark_id__isnull=True)` ⇒ 目录里**只有一条**自有节点时它
    **碰巧**能跑，**第二条一进来就抛 `MultipleObjectsReturned`**——症状是整个目录灌不进去
    （不是"第二条没建上"，是**一条都没有**）。

    **判据形态**：当场把目录换成「真目录 + 第二条合成自有节点」再驱动**真** `seed_catalog()`
    （⛔ 不是断言实现里有个 `if`），跑两遍（第二次覆盖"已存在 ⇒ 走 update"那一支），
    并要求**两条自有节点各自一行、且互不串**。
    ⚠️ 桩打在**被查找的那个模块**上（`apps.rbac.seed.NODES`，`E-0029`），⛔ 不打在
    `apps.rbac.catalog` 上——`seed_catalog` 读的是自己模块里那个绑定。
    ⚠️ 合成夹具是**编的**（`E-0077`），只用于本用例内的一次调用，跑完即随 `mock` 撤销。
"""
        extra = (
            "system.synthetic_own_node_second", None, "合成自有节点乙",
            "只在本用例里存在（E-0077 的合成夹具）", "/open-platform/synthetic-b",
            NodeKind.PAGE, False,
        )
        with mock.patch("apps.rbac.seed.NODES", NODES + (extra,)):
            seed_catalog()
            seed_catalog()  # 第二遍：走"已存在 ⇒ update"那一支，仍不许炸
            rows = {
                n.code: n
                for n in PermissionNode.objects.filter(benchmark_id__isnull=True)
            }
            # 真目录里已有全部自有节点（清单见断言）⇒ 本用例的合成节点与它们**共存**：
            # 真目录一条不缺、合成节点自己一行，互不串（这条判据随自有节点入表自动变强）。
        self.assertEqual(
            set(rows),
            {
                "order.platform_write",
                "finance.taker_withdraw_review",
                "finance.taker_wallet_adjust",
                "finance.xgj_receipt_detail",
                "system.allow_taker_open_api",
                "system.edit_settings",
                "system.article_manage",
                "home.qr_manage",
                "home.approval_apply",
                "home.approval_penalty_approve",
                "home.approval_refund_handle",
                "home.approval_configure",
                "system.qqbot_settings",
                "customer.member_list_self_only",
                "system.wecom_settings",
                "system.synthetic_own_node_second",
            },
            "自有节点没有各自一行（按 `benchmark_id` upsert 时这里会抛 "
            "MultipleObjectsReturned、或者互相覆盖成一条）",
        )
        # ⚠️ 反向：两条**不能**互相串（`update_or_create` 匹配键写错时最容易出现的坏法）。
        self.assertEqual(rows["system.synthetic_own_node_second"].name, "合成自有节点乙")
        self.assertFalse(rows["system.synthetic_own_node_second"].available)
        self.assertEqual(rows["system.allow_taker_open_api"].name, "允许打手使用开发平台应用")
        self.assertTrue(rows["system.allow_taker_open_api"].available)
        # 合成那条也挂在 `system` 栏目下（父级解析走 `code.split(".")[0]`，与对标点同一支）。
        self.assertEqual(rows["system.synthetic_own_node_second"].parent.code, "system")

    def test_seed_does_not_reconfigure_existing_role_permissions(self):
        """幂等的第二层含义：已存在角色**不重配**权限（管理员改过的授权不能被回滚）。

    ⚠️ 上面那条 `test_seed_is_idempotent_and_does_not_touch_assignments` **不覆盖**这条：
    它只调 `seed_catalog()`，而那个函数根本不碰 `Role.permissions` ⇒ 它的断言相对
    自己的测试名是空的。本用例两个方向都钉：
    ① 手工改过的分配在下一轮种子后**原样存活**；
    ② 同一轮里**新建**的内置角色**仍**拿到默认权限——否则"对任何角色都不配权限"
    同样能让 ① 变绿。
"""
        seed_catalog()
        seed_builtin_roles()

        service = Role.objects.get(name="客服")
        service.permissions.set(
            PermissionNode.objects.filter(code__in=["order", "order.list"])
        )
        self.assertEqual(service.permissions.count(), 2)  # 构造物自证：确实被改过

        # 造一个"新建"的内置角色：删角色行、留同名 Group（= 管理员删过该职位，再被种子补回）
        Role.objects.filter(name="客服主管").delete()

        seed_builtin_roles()

        self.assertEqual(
            sorted(Role.objects.get(name="客服").permissions.values_list("code", flat=True)),
            ["order", "order.list"],
            "重复种子把已存在角色被改过的分配覆盖回去了",
        )
        supervisor_modules = (
            "order", "system_user", "my_orders", "my_performance", "my_profile",
            "grab_hall", "home",
        )
        expected = _default_permission_count(supervisor_modules) - 3
        self.assertEqual(
            Role.objects.get(name="客服主管").permissions.count(), expected,
            "新建的内置角色没有拿到默认权限",
        )

    def test_seed_updates_changed_fields(self):
        """upsert 语义：改 `catalog.py` 的文案后重跑，库里跟着变（按 code 匹配）。"""
        seed_catalog()
        PermissionNode.objects.filter(code="order.list").update(name="被手改了")
        seed_catalog()
        self.assertEqual(PermissionNode.objects.get(code="order.list").name, "订单管理列表")

    def test_seed_propagates_available_flag_to_existing_rows(self):
        """`available` 必须进 `defaults`： 把某些点翻成 `True` 后重灌要能传播。

    漏掉它的话"改 `catalog.py` 再重灌"是空操作（旧值永远留在库里），
    而单跑一次种子看不出来——只有"先灌、再改、再灌"这一步能抓到。

    ⚠️ **2026-09-23 换过夹具**：原用 `order.send_sms`（短信整块已下线，该点随本线裁剪
    从 `catalog.py` 删除）⇒ 换成同为 `available=False`、且仍留在目录与 `wiring.PENDING`
    （理由「功能未实现」）的 `order.toggle_urgent`。断言强度不变：仍是"库里原本为 `False`
    的那个点被重灌成 `True`"。
"""
        seed_catalog()
        flipped = tuple(
            (code, bid, name, note, url, kind, True if code == "order.toggle_urgent" else avail)
            for code, bid, name, note, url, kind, avail in NODES
        )
        with mock.patch("apps.rbac.seed.NODES", flipped):
            seed_catalog()
        self.assertTrue(PermissionNode.objects.get(code="order.toggle_urgent").available)
        self.assertEqual(PermissionNode.objects.count(), len(NODES) + len(MODULES))

    def test_builtin_roles_are_created_with_same_name_groups(self):
        seed_builtin_roles()
        self.assertEqual(
            sorted(Role.objects.values_list("name", flat=True)), sorted(BUILTIN)
        )
        for name in BUILTIN:
            self.assertTrue(Group.objects.filter(name=name).exists(), name)

    def test_builtin_roles_are_created_with_the_sales_agent_mark(self):
        """种子对**新建**租户那条路：只有「客服」「客服主管」带「可作为销售客服」标记。

    依据：业务方裁定候选按角色标记取（`apps/business/selectors.py::kefu_options`），
    默认那两个内置角色带上它（`KEFU_CANDIDATE_ROLES`）。
    ⚠️ 期望值写**字面集合**（E-0024）：引用 `KEFU_CANDIDATE_ROLES` 就成了自指断言
    ——常量被改坏时两边一起变，本用例照样绿。
    ⚠️ 存量租户那条路（数据迁移 `0005`）在 `tests/test_rbac_sales_agent_flag.py`，**不在这里**：
    两处是两份代码，各自要有自己的用例。
"""
        seed_builtin_roles()
        marked = set(
            Role.objects.filter(can_be_sales_agent=True).values_list("name", flat=True)
        )
        self.assertEqual(marked, {"客服", "客服主管"})

    def test_super_admin_gets_every_node_others_do_not(self):
        seed_catalog()
        seed_builtin_roles()
        admin = Role.objects.get(name="系统管理员")
        # 「系统管理员」按 `BUILTIN_ROLE_MODULES` 的 `None` 拿**全目录**：全部非 flag 点
        # （含全部自有节点）+ 业务方实测配置的 4 条 flag；读数以字面断言为准（各线入表
        # 时自有节点随之入「系统管理员」、2026-11-11 客户维护组收敛删 8 点，逐条见
        # `catalog.py` 模块 docstring）。
        # ⭐ 企微自建应用线（D-0379）：147 → **148**（第十一条自有节点是非 flag 的 PAGE 点、进全目录）。
        self.assertEqual(admin.permissions.count(), 152)
        service = Role.objects.get(name="客服")
        self.assertGreater(service.permissions.count(), 0)
        self.assertLess(service.permissions.count(), 152)

    def test_super_admin_flags_match_the_business_configuration(self):
        """系统管理员的默认 flag 对齐业务方 2026-09-23 的实测配置。

    这 4 条 flag 与总数都钉成字面值。

    ⚠️ 期望值写**字面集合**（E-0024）：引用 `BUILTIN_ROLE_FLAGS` 就成了自指断言——
    改常量时两边一起变，本用例照样绿。
    ⚠️ 口径边界：`seed_builtin_roles()` 对**已存在**的角色不重配（`if not was_created`）
    ⇒ 这里的默认值落在**新建租户**；存量租户由业务方已经配好的授权兜着，本改动不做回填。
"""
        seed_catalog()
        seed_builtin_roles()

        admin = Role.objects.get(name="系统管理员")
        self.assertEqual(
            set(
                admin.permissions.filter(kind=NodeKind.FLAG).values_list("code", flat=True)
            ),
            {
                "finance.override_global_settlement",
                "home.restrict_upstream_market_entry",
                "order.allow_change_sales_agent",
                "order.restrict_to_own_group_leader",
            },
            "「系统管理员」的默认 flag 与业务方实测配置不一致",
        )
        # 非 flag（全目录扣掉 21 条 flag，含全部自有节点）+ flag 4 = 总数，判据见断言。
        self.assertEqual(
            admin.permissions.count(), 152,
            "「系统管理员」的默认权限总数变了（非 flag 148 + flag 4）",
        )

    def test_builtin_role_gets_both_module_node_and_its_children(self):
        """栏目节点本身也要给——只给 `parent__code__in=...` 会漏掉它。

    brief 正文那条 ⚠️ 的判据：漏栏目时权限树上的父勾选框是空的，而
    `> 0` / `< 150`（管理员的权限数）对两种实现**都**成立，抓不到。所以这里钉两件事：
    ① 客服同时拿到栏目（`order`）与它的子节点（`order.list`）；
    ② 代练员的权限数**恰好**等于它那几个栏目（栏目数 + 非 flag 子节点数），
    元组末尾的 `order.submit_work` 算在里面——多一个是越权、少一个就是漏了栏目。
    数量对拍走 `_default_permission_count(worker_modules)`（按元组逐项算）。
"""
        seed_catalog()
        seed_builtin_roles()

        service_codes = set(
            Role.objects.get(name="客服").permissions.values_list("code", flat=True)
        )
        self.assertIn("order", service_codes)
        self.assertIn("order.list", service_codes)

        # ⚠️ 种子只对**新建**角色生效（后果与部署必做项见
        # `.agents/notes/implemented/architecture/2026-09-23-rbac-roles-and-catalog-consolidation.md`
        # 的 **§7.1**——⛔ 注意 `seed_rbac_catalog` 补灌**无效**，
        # 只能管理员手工勾）：元组末尾有「订单业务」栏目下的**单点**
        # `order.submit_work`（交单是这个职位的核心动作，而它不属代练员默认的栏目）。
        # 本条断言是**自指的**（本地字面量与生产常量各写一份）⇒ 按 E-0024 的口径
        # **它不是本任务的判据**，只是「多一个/少一个」的数量对拍；真判据是下面
        # `test_the_worker_role_gets_the_submit_node_by_default`（集合成员）。
        # ⭐ 代练员另配两个「订单业务」单点
        # （`order.view_screenshot` / `order.upload_screenshot`）⇒ 本元组同步。
        worker_modules = ("my_orders", "my_performance", "my_profile", "grab_hall", "home",
                          "order.submit_work",
                          "order.view_screenshot", "order.upload_screenshot")
        expected = _default_permission_count(worker_modules) - 4
        self.assertIn("home.approval_apply", service_codes)
        self.assertNotIn("home.approval_penalty_approve", service_codes)
        self.assertEqual(Role.objects.get(name="代练员").permissions.count(), expected)

    def test_the_worker_role_gets_the_submit_node_by_default(self):
        """2026-09-23（本线 D6）：默认「代练员」必须能提交完单。

    ⚠️ 这修的是一个**现存缺陷**：`BUILTIN_ROLE_MODULES` 给代练员的模块
    （`my_orders / my_performance / my_profile / grab_hall / home`）**不含 `order` 栏目**
    ⇒ 非超管的代练员点「提交完单」403。
    ⚠️ 判据是**节点是否在角色的权限集里**，不是「代码里写了」——
    写成同义反复（拿 `BUILTIN_ROLE_MODULES` 自己比）就抓不到"忘了 set"。
"""
        seed_rbac()
        role = Role.objects.get(name="代练员")
        self.assertIn("order.submit_work", set(role.permissions.values_list("code", flat=True)))

    def test_the_worker_role_can_view_and_upload_order_screenshots(self):
        """⭐ 2026-10-20（业务方当场拍板）：默认「代练员」必须能**看 / 传订单截图**。

    理由：打手 App 的进度截图要能传（也要能看已有的），而这两个点的判据不在
    `order` 栏目批发里（代练员不拿整个 `order` 栏目）⇒ 必须显式单点。
    ⚠️ 判据是**节点在角色的权限集里**（读库），⛔ 不是拿 `BUILTIN_ROLE_MODULES` 自己比
    （那是自指断言，改坏常量两侧一起变）。
    ⚠️ **只对新建租户生效**：`seed_builtin_roles` 对已存在的角色 `continue`
    ⇒ 存量租户不会被改写（生产两台已人工补过）。
"""
        seed_rbac()
        codes = set(Role.objects.get(name="代练员").permissions.values_list("code", flat=True))
        self.assertIn("order.view_screenshot", codes)
        self.assertIn("order.upload_screenshot", codes)

    def test_the_hr_role_gets_exactly_the_account_management_nodes(self):
        """⭐ 线 D（2026-10-12）：内置「人事」的节点集合 = `system_user` + `my_profile` + `home`。

    期望值写**字面集合**（E-0024）：引用 `BUILTIN_ROLE_MODULES` 就成了自指断言
    ——把 `system_user` 从人事的模块里删掉时两边一起变、本用例照样绿。
    判据形态：**恰好相等**（多一个 = 越权，少一个 = 漏了栏目行或子节点）。

    ⚠️ 这三条栏目下的 `flag` 点**一个都不给**（`seed_builtin_roles` 的模块批发排除
    `flag`，见 `builtin_role_nodes()`）；本集合里因此没有
    `system_user.restrict_*` / `home.restrict_upstream_market_entry`。
    ⚠️ `system_user` 栏目下的 `available=False` 点**照给**：`available` 是「有没有消费方」
    的登记（D5），不是授权判据；对标权限树上这一栏整栏勾上就是给全部点。
"""
        seed_catalog()
        seed_builtin_roles()

        role = Role.objects.get(name="人事")
        self.assertEqual(
            set(role.permissions.values_list("code", flat=True)),
            {
                # 栏目行自身（父与子在界面上互不联动 ⇒ 只给子节点时父勾选框是空的）
                "system_user",
                "system_user.list",
                "system_user.add_edit",
                "system_user.change_status",
                "system_user.reset_password",
                "system_user.batch_set_game_permissions",
                "system_user.login_restriction",
                "system_user.add_edit_login_restriction",
                "system_user.blacklist",
                "system_user.add_edit_blacklist",
                "system_user.delete_blacklist",
                "system_user.view_order_stats",
                "system_user.realname_records",
                "system_user.reset_realname",
                "system_user.contract_management",
                "my_profile",
                "my_profile.edit",
                "home",
                "home.index",
                "home.enter",
                "home.change_password",
                "home.feedback",
                # ⭐ `home.qr_manage`（D-0262）经 `home`
                # 栏目批发**自动含于新建的人事角色**（非 flag 子节点；`builtin_role_nodes`
                # 的模块展开口径）⇒ `seed.py` 的 `BUILTIN_ROLE_MODULES` **一个字没改**，
                # 只是这张字面集合同批多一行。存量租户的人事拿不到它（种子不重配已存在
                # 角色）——那是发版后的运维点，见交付说明。
                "home.qr_manage",
                "home.order_ranking",
                "home.system_circle",
                "home.system_circle_publish",
                "home.blacklist_board",
                "home.blacklist_report",
            },
            "「人事」的节点集合与线 D 的目标不一致（多一个 = 越权，少一个 = 漏了栏目/子节点）",
        )
        # 反面控制：人事**不碰订单**（⛔ 不给 `order` 栏目）——上面那条集合相等已蕴含它，
        # 这里再点名一次，让「为什么没有 order」在断言里可读。
        self.assertNotIn("order", set(role.permissions.values_list("code", flat=True)))
        # 对标原文的 remark（`docs/JIDANPU-API.md:355` 的 roleId 21）。
        self.assertEqual(role.remark, "负责人事招聘、开子账户")

    def test_the_hr_role_seed_is_idempotent_and_respects_manual_grants(self):
        """⭐ 线 D 的幂等判据（spec §6.1 的两层含义都钉）：

    ① 重复种子**不重复建行**、不重复授权；
    ② 管理员在界面上改过的人事授权**不被下一次种子回滚**。

    ② 的判别力载体是「先删掉一个节点，再跑种子，它**不许**回来」——拿
    `role.permissions.count()` 比大小也能发现回滚，但删一个**具体 code** 更直接
    （E-0024：钉值，不钉常量）。
"""
        seed_rbac()
        role = Role.objects.get(name="人事")
        self.assertTrue(
            role.permissions.filter(code="system_user.list").exists(), "夹具前提不成立"
        )

        seed_rbac()  # 第二次
        self.assertEqual(Role.objects.filter(name="人事").count(), 1, "重复种子建了第二行")
        self.assertTrue(
            role.permissions.filter(code="system_user.list").exists(),
            "重复种子把人事的授权收掉了",
        )

        # 管理员改过的授权：摘掉一个节点，再跑种子（`seed_builtin_roles` 对已存在角色 `continue`）。
        role.permissions.remove(PermissionNode.objects.get(code="system_user.list"))
        seed_rbac()
        self.assertFalse(
            role.permissions.filter(code="system_user.list").exists(),
            "重复种子把管理员改过的人事授权回滚了（幂等的第二层含义被破坏）",
        )
        # 反向控制：其余授权仍在（否则上面那条可能由「权限被清空」凑出来）。
        self.assertTrue(role.permissions.filter(code="system_user.add_edit").exists())


class SeedTenantWiringTests(TenantTestCase):
    """接线判据：新租户由 `seed_tenant` 自动灌（spec §6.1）。"""

    def test_provision_tenant_seeds_catalog_and_builtin_roles(self):
        """`provision_tenant` 建出来的租户里必须有目录 + **全部内置角色**。

    ⚠️ 原文写「5 个内置角色」——线 D加了「人事」之后那个数过期；
    个数以内置清单 `BUILTIN_ROLE_MODULES` 为准（本用例的 `BUILTIN` 字面量同批更新）。

    没有这条断言时，把 `provisioning.py` 里那两行 `seed_rbac()` 删掉，
    本文件的其余用例**全绿**——它们都自己显式调了种子函数。
"""
        tenant, _ = provision_tenant(
            schema_name="seed_wire", name="WIRE", domain="seed-wire.test.local"
        )
        with tenant_context(tenant):
            self.assertEqual(PermissionNode.objects.count(), len(NODES) + len(MODULES))
            self.assertEqual(
                sorted(Role.objects.values_list("name", flat=True)), sorted(BUILTIN)
            )
            # 内置角色不是空壳：客服拿到栏目（order）与它的子节点（order.list）。
            service_codes = set(
                Role.objects.get(name="客服").permissions.values_list("code", flat=True)
            )
            self.assertIn("order", service_codes)
            self.assertIn("order.list", service_codes)
            # 两个内置角色带着「可作为销售客服」标记建出来。
            self.assertEqual(
                set(
                    Role.objects.filter(can_be_sales_agent=True).values_list(
                        "name", flat=True
                    )
                ),
                {"客服", "客服主管"},
            )


class SeedCommandTests(TenantTestCase):
    """补灌命令：给**既有**租户灌库，并打印实际的 `(created, updated)`。"""

    def _backfill(self, schema="seed_cmd", domain="seed-cmd.test.local"):
        tenant, _ = provision_tenant(schema_name=schema, name="CMD", domain=domain)
        with tenant_context(tenant):
            # 模拟"目录是本功能之后才加的"：租户里已有 Group/Role/管理员，但没有目录行。
            PermissionNode.objects.all().delete()
        out = StringIO()
        call_command("seed_rbac_catalog", stdout=out)
        return tenant, out.getvalue()

    def test_command_backfills_existing_tenant_and_prints_counts(self):
        tenant, text = self._backfill()
        with tenant_context(tenant):
            self.assertEqual(PermissionNode.objects.count(), len(NODES) + len(MODULES))
            self.assertEqual(Role.objects.count(), len(BUILTIN))
        # 打印的是**实际**结果（不是占位的 None）：13 栏目 + 全目录（含自有节点，也走
            # 同一次 upsert）全是新建；读数以断言为准。
        # ⭐ 企微自建应用线（D-0379）：165 → **166**（第十一条自有节点入目录）。
        self.assertIn("seed_cmd: 170 created / 0 updated", text)

    def test_command_is_idempotent(self):
        tenant, _ = self._backfill()
        out = StringIO()
        call_command("seed_rbac_catalog", stdout=out)
        # 可重跑、不重复建行（自有节点同样走 update 那一支）；读数以断言为准。
        # ⭐ 企微自建应用线（D-0379）：165 → **166**（第十一条自有节点入目录）。
        self.assertIn("seed_cmd: 0 created / 170 updated", out.getvalue())
        with tenant_context(tenant):
            self.assertEqual(PermissionNode.objects.count(), len(NODES) + len(MODULES))

    def test_command_skips_the_public_tenant_row(self):
        """`public` 行真实存在时必须跳过。

    切进 public 后命令会去查 `rbac_permissionnode`，而这张表**只**建在租户 schema 里
    （`apps.rbac` 只在 `TENANT_APPS`；`django_tenants/routers.py` 的 `allow_migrate`
    在 public 上只放行 `SHARED_APPS`）⇒ 不排除它就会抛 `ProgrammingError`。

    先自证构造物：`ensure_public_tenant` 建出的 public 行**确实在表里**，
    否则"命令不打印 public"只是因为压根没有 public 行，断言恒真。
"""
        from apps.platform.bootstrap import ensure_public_tenant
        from apps.platform.models import Tenant

        ensure_public_tenant("admin.test.local")
        self.assertTrue(
            Tenant.objects.filter(schema_name="public").exists(), "构造物不成立：没有 public 行"
        )
        _, text = self._backfill(schema="seed_cmd2", domain="seed-cmd2.test.local")
        self.assertNotIn("public:", text)
