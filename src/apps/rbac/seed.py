"""目录与内置角色的幂等种子（spec §4.4 / §6.1）。

**幂等**的含义分两层，别混：① 重复执行不产生重复行；② **已存在的角色分配不动**——
管理员在界面上改过的授权，不会被下一次种子覆盖回去（spec D11：默认权限是种子不是硬约束）。
"""
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q

from apps.rbac.catalog import MODULES, NODES
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role

#: 内置角色的**默认权限**（spec §6.1）。按栏目 code 给，逐条列举可以避免
#: "给整棵子树"这种含糊写法——权限点是扁平的两级，没有继承。
#: `None` = 除 `flag` 点外的全部非 flag 点（系统管理员）。
#: 系统管理员的 `None` = 除 flag 外的全部非 flag 点（裁剪后 146；企微线 2026-11-11 加一 ⇒ 147）。
#:
#: 本表只管栏目。flag 不在这里批发：勾上之后，`allow_*` / `override_*` 是放开，
#: `restrict_*` / `forbid_*` 是施加限制。按模块一起给，限制型和放开型都会错。
#: 逐条决策在 `BUILTIN_ROLE_FLAGS`。
BUILTIN_ROLE_MODULES = {
    "系统管理员": None,
    "客服主管": ("order", "system_user", "my_orders", "my_performance", "my_profile",
                 "grab_hall", "home"),
    "客服": ("order", "my_orders", "my_performance", "my_profile", "grab_hall", "home"),
    "代练员": ("my_orders", "my_performance", "my_profile", "grab_hall", "home",
               # ⚠️（本线 D6）：**显式**补一个「订单业务」栏目下的单点。
               # 理由：交单（`/submit/`）是这个职位的**核心动作**，而它的判据点
               # `order.submit_work` 属 `order` 栏目，代练员默认模块不含该栏目 ⇒ 403。
               # ⛔ **不许**改成整个 `"order"` 栏目——那会把 16/18/19 等一起给出去
               # （代练员可绕过 `/claim` 改任意单状态，越权）。
               # ⚠️ 只对**新建**角色生效：`seed_builtin_roles` 对已存在的角色 `continue`
               # ⇒ 存量租户的内置「代练员」不会被自动补上，而且 ⛔ **`seed_rbac_catalog` 补灌也无效**
               # （它刻意「不重配已存在角色的权限」）⇒ **唯一路径 = 管理员在「角色职位权限」页
               # 手工勾上这个点**；不做的症状是「**改了但没变**」（代练员点交单照旧 403）。
               # 部署清单与自检命令见 `.agents/notes/implemented/architecture/2026-09-23-rbac-roles-and-catalog-consolidation.md`
               # 的 **§7.1「部署必做」**（那是一条**入库**的执行记录，不是临时台账）。
               #
               # ⭐（业务方当场拍板，`feat/taker-wallet-adjust`）：再补两个「订单业务」
               # 栏目下的单点——**看订单截图 / 上传订单截图**：
               # `order.view_screenshot` / `order.upload_screenshot`。
               # 理由：打手 App 要能传进度截图（与看已有截图），而这两个点的判据在
               # `screenshot_views.py` 上；代练员的默认模块**不含 `order` 栏目** ⇒ 新租户
               # 从 seed 起的默认角色缺这两个点，打手一上来连**自己是单的主人**都传不了。
               # 生产两台租户之前是**人工**给代练员补的（`docs/STATUS.md` 2026-10-19，
               # 22 → 25），这次把同一份口径落到 seed，新租户不再需要人工补。
               # ⚠️ **只对新建租户生效**：`seed_builtin_roles` 对已存在的角色 `continue`
               # ⇒ 存量租户的内置「代练员」不会被改写（生产那两台已人工补过、不受影响）；
               # ⛔ 不写数据迁移去替租户改授权（与 `order.submit_work` 同一条既定口径）。
               "order.submit_work",
               "order.view_screenshot", "order.upload_screenshot"),
    "系统开发员": ("my_orders", "my_performance", "my_profile", "home"),
    #: ⭐ **线 D新增内置职位「人事」**：业务方要「开一个人事子账号，
    #: 人事可以新增编辑删除子账户」。模块 = `system_user`（账号管理那一栏）+ `my_profile`
    #: + `home`，**最小集**（⛔ 不给 `order`：人事不碰订单）；对标 roleId 21
    #: 「人事客服 / 负责人事招聘、开子账户」（`docs/JIDANPU-API.md`）取的就是这个口径。
    #: ⚠️ **账号端点原先的准入是 `IsAccountAdmin`**（= 持「系统管理员」职位）⇒ 只加这个职位
    #: 仍然开不了子账户。本线同批把账号端点的准入换成 `IsAccountManager`
    #: （`apps/accounts/permissions.py`），人事靠 `system_user.*` 节点过那扇门。
    #: ⛔ **不动 `apps/business/roles.py::DEFAULT_ROLES`**：那是**供给时**的 Group 种子，
    #: 加进去会让**每一个新租户**多一个职位、并改掉 `DEFAULT_ROLES` 的顺序契约。
    #: ⚠️ **已知代价（如实登记，⛔ 不写数据迁移去替租户改授权）**：`seed_builtin_roles`
    #: 对**已存在**的角色 `continue` ⇒ **存量租户不会自动多出「人事」**，要让管理员在
    #: 「角色职位权限」页手工建「人事」并勾权限（或由运维手工跑一次种子）。
    #: 落到本表的改动只对**新建**租户生效。
    "人事": ("system_user", "my_profile", "home"),
}
# ⭐ 交易平台涉钱写动作线（审计 M5）：新增自有节点 `order.platform_write`
# （非 flag，属 `order` 栏目）⇒ **新建租户**的「客服 / 客服主管 / 系统管理员」按上面的
# 栏目批发**自动带上它**（这三类角色的本职就是操作订单）。存量租户由数据迁移
# `0014_add_order_platform_write_node` 建点，并**自动补授给已持有 `order.list` 的角色**
# （逐角色幂等 `add`、只认中间表不猜角色名，存量客服上线当天不会被锁在门外）。

#: 代练员多出来的点写在上面元组末尾，不要再另做一份补授常量。
#: `order.submit_completion` 这个 code 不存在。种子只覆盖新建角色。

#: 每个内置角色显式授予的 flag。不给限制型 = 不施加限制；不给放开型 = 不放开。
#: 按模块批发会把两种极性一起发错，所以决策只写在这张表。
#:
#: 系统管理员 4 条：放开 `order.allow_change_sales_agent`、`finance.override_global_settlement`；
#: 限制 `home.restrict_upstream_market_entry`、`order.restrict_to_own_group_leader`。
#: 后两条今天没有消费方，勾上不改变行为；接线后管理员靠职位旁路，不会被自己的限制套住。
#: 非 flag 146 + 这 4 条 = 150。其余内置角色（含人事）一条都不给：职位说明里没有限制要求。
#: 人事这个键不能漏，`builtin_role_nodes()` 会 KeyError。
#:
#: 改这张表时 B9 大部分是自指（它拿同一份常量比），只有「客服为空」是独立字面量。
#: `seed_builtin_roles()` 不重配已存在的角色，改动只落在新建租户。
BUILTIN_ROLE_FLAGS = {
    "系统管理员": frozenset({
        "finance.override_global_settlement",
        "home.restrict_upstream_market_entry",
        "order.allow_change_sales_agent",
        "order.restrict_to_own_group_leader",
    }),
    "客服主管": frozenset(),
    "客服": frozenset(),
    "代练员": frozenset(),
    "系统开发员": frozenset(),
    #: ⭐ 线 D：内置「人事」的 `flag` 决策 = **一条都不给**（同上一段的逐条口径：
    #: 业务方没有对人事提出任何限制性/放开型要求）。⚠️ 本表**必须有这个键**：
    #: `builtin_role_nodes()` 直接读 `BUILTIN_ROLE_FLAGS[name]`，漏键 = 种子 `KeyError`。
    "人事": frozenset(),
}

#: 内置角色里**默认**带「可作为销售客服」标记（`Role.can_be_sales_agent`）的两个
#: （业务方裁定：候选按角色标记取，默认这两个内置角色带上它）。
#: ⚠️ 它们就是 `kefu_options` 改动前那份**写死的名字**（`("客服", "客服主管")`）——
#: 放进种子 + 一条数据迁移之后，存量与新建租户的候选名单与改动前逐条相同。
#: ⛔ 这里只放**默认值**，不是「名单的唯一出处」：真判据是角色上那一列，租户自己勾的自建角色
#: 同样进候选（`apps/business/selectors.py::kefu_options`）。
KEFU_CANDIDATE_ROLES = ("客服", "客服主管")

#: 对标 remark 原文，作为角色说明的种子（spec §6.1）。原文照抄，不修文案。
BUILTIN_ROLE_REMARKS = {
    "客服": "负责订单操作，如录单、编辑订单、管理订单、指派、看图、发单等操作、导出订单，不能开子账号",
    "客服主管": "仅次于总经理的权限，拥有订单相关一切操作权限，并且兼顾新开子账号、管理子账号等权限，没有结算权限",
    "代练员": "权限最小，适用于代练员、打手、下家、陪玩师等职位用",
    "系统管理员": "系统最高权限，拥有一切权限操作能力",
    "系统开发员": "用于鸡蛋铺官方开发工作人员协助、调试使用，系统预留职位，若有顾虑可以删除此职位，不要给其它人设此职位",
    #: ⭐ 线 D：对标 roleId 21「人事客服」的 note 原文照抄
    #: （`docs/JIDANPU-API.md:355`：「负责人事招聘、开子账户」）——本仓没有单独的人事招聘页，
    #: 实际给的是账号管理那一栏（`system_user`），文案仍按对标原文留档。
    #: ⚠️ 本表**必须有这个键**：`seed_builtin_roles()` 直接读 `BUILTIN_ROLE_REMARKS[name]`。
    "人事": "负责人事招聘、开子账户",
}


@transaction.atomic
def seed_catalog():
    """按 `benchmark_id` upsert 目录（**自有节点那一族改按 `code`**）。返回 `(created, updated)`。

    匹配键用 `benchmark_id`（对标识别的锚点，spec §4.1），`code` 是**允许改**的唯一码
    （spec §4.3 的改名场景）⇒ 它进 `defaults`。`available` 同样在 `defaults` 里：
    某些点被翻成 `True` 之后，重跑种子必须能把新值**传播**进库，
    否则"改 catalog 再重灌"就是空操作。

    若 `catalog.py` 被改坏成"同一个 `code` 指向两个 `benchmark_id`"，
    `update_or_create` 会在第二行插入时撞上 `code` 的唯一约束抛 `IntegrityError`——
    **响亮地失败**，不静默合并两行。反向的坏法（同一个 `benchmark_id` 对应两个 `code`）
    只会在同一个 `benchmark_id` 上反复覆盖，**不会**抛错；`code`/`benchmark_id` 的
    目录级唯一性由 `tests/test_rbac_catalog.py` 兜底。

    ## ⭐ 自有节点（`benchmark_id is None`）**必须按 `code` upsert**（T4）

    `update_or_create(benchmark_id=None, …)` 里的 `None` **不是**"这个键没有值"，
    而是 `filter(benchmark_id__isnull=True)`：第一次跑时会**碰巧**命中一条（今天恰好只有
    一条自有节点），但**第二条自有节点一进来就抛 `MultipleObjectsReturned`**——
    而且抛在**种子**里 ⇒ 整个目录灌不进去。⇒ 判据：自有节点按 **`code`** upsert
    （`code` 是唯一列，天然一条），`benchmark_id` 进 `defaults`（值就是 `None`）。
    这条判据**有网**：`tests/test_rbac_seed.py` 里那条「两条自有节点共存不炸」
    （它当场构造出第二条自有节点、真跑一次 `seed_catalog()`，⛔ 不是只写在注释里）。

    ⚠️ **只动自有节点那一支**：对标点那一支（`benchmark_id` 是整数）**一个字不改**——
    它的匹配键是"对上标的溯源"，换成 `code` 会把 `spec §4.3` 的改名场景（code 可以改、
    对标 id 不变）反过来写死。
    """
    created = updated = 0
    modules = {}
    for code, benchmark_id, name, note, url in MODULES:
        obj, was_created = PermissionNode.objects.update_or_create(
            benchmark_id=benchmark_id,
            defaults={
                "code": code, "name": name, "note": note, "url": url,
                "kind": "page", "level": 0, "parent": None, "order_by": 0,
            },
        )
        modules[code] = obj
        created += was_created
        updated += not was_created

    for code, benchmark_id, name, note, url, kind, available in NODES:
        module_code = code.split(".")[0]
        defaults = {
            "code": code, "name": name, "note": note, "url": url,
            "kind": kind, "level": 1, "parent": modules[module_code],
            "order_by": 0, "available": available,
        }
        if benchmark_id is None:
            # ⭐ 自有节点：按 `code` 匹配（理由见 docstring）—— `benchmark_id` 只能当被写值。
            obj, was_created = PermissionNode.objects.update_or_create(
                code=code, defaults={**defaults, "benchmark_id": None},
            )
        else:
            obj, was_created = PermissionNode.objects.update_or_create(
                benchmark_id=benchmark_id, defaults=defaults,
            )
        created += was_created
        updated += not was_created
    return created, updated


def builtin_role_nodes(name):
    """内置角色 `name` 按**种子默认**应当拿到的节点（`QuerySet[PermissionNode]`）。

    **为什么抽成函数、而不是留在 `seed_builtin_roles()` 的循环里**：这是"内置角色那一档
    节点集"的**唯一出处**。任何需要"**照抄某个内置角色那一档**"的调用方都必须走这里——
    ⛔ 不许各自再写一份"栏目批发"的查询（那是两份实现，必然漂移；见
    `docs/specs/2026-09-26-game-scope-audit-design.md` §2 与 `apps/platform/management/commands/seed_e2e.py`
    的 `_seed_gamescope`：E2E 的"节点照抄客服、游戏只挂 1 款"角色就走这个函数）。

    语义**逐字**等于 `seed_builtin_roles()` 原先内联的那一段（把它抽出来是纯重构，
    ⛔ 没有改任何角色的授权）：
    - 栏目与子节点都要给：父与子在界面上**互不联动**（spec §8.2"父不联动子、子不影响父"），
      所以只给子节点的话，栏目自己的勾选框是空的；
    - **`flag` 点一律排除在模块批发之外**（终局评审 B1）：它们是限制性动作，
      按模块给会让客服被"禁止录入新订单"这类反义限制套住（见 `BUILTIN_ROLE_MODULES`
      与 `BUILTIN_ROLE_FLAGS` 的说明）。逐条授权只从显式常量来。
    - `BUILTIN_ROLE_MODULES` 里取 `None` 的角色（系统管理员）⇒ **全部节点**（再按上面的口径
      补上 `BUILTIN_ROLE_FLAGS` 里显式给它的 `flag` 点）。

    ⚠️ 返回的是 **queryset**（不是 list）：调用方自己决定 `set(...)` 还是先落成 list 校验。
    ⛔ 它**不建角色、不写库**——只算"应当给哪些点"。
    """
    module_codes = BUILTIN_ROLE_MODULES[name]
    all_nodes = PermissionNode.objects.all()
    wanted = (
        all_nodes
        if module_codes is None
        else all_nodes.filter(Q(parent__code__in=module_codes) | Q(code__in=module_codes))
    )
    # ⚠️ 排除 `flag` 必须在**模块展开之后**：栏目子节点里混着 flag，
    # 只按 `code__in=module_codes` 过滤是筛不掉的。
    wanted = wanted.exclude(kind=NodeKind.FLAG)
    if module_codes is not None:
        wanted = wanted.exclude(code__startswith="home.approval_")
        if name in ("客服", "客服主管"):
            wanted = wanted | all_nodes.filter(code="home.approval_apply")
    explicit_flags = BUILTIN_ROLE_FLAGS[name]
    if explicit_flags:
        wanted = wanted | all_nodes.filter(code__in=explicit_flags)
    return wanted


@transaction.atomic
def seed_builtin_roles():
    """建内置角色与**同名 Group**（spec D2），并给**新建**的角色配默认权限。

    ⚠️ 已存在的角色**不重配权限**（幂等的第二层含义）：管理员在界面上改过的授权不该被回滚。
    因此这里只对 `was_created` 的角色 `set(...)`，重复调用不碰 `Role.permissions`。

    权限集的口径（栏目节点 + 非 `flag` 子节点 + `BUILTIN_ROLE_FLAGS` 的显式 `flag` 点）
    **只在 `builtin_role_nodes()` 里**定义一次，这里只负责把它 `set(...)` 上去。

    ⚠️ **「可作为销售客服」标记（业务方裁定）**：内置「客服」「客服主管」
    （= `KEFU_CANDIDATE_ROLES`）在**新建**时带上 `can_be_sales_agent=True`
    ——录单页「接单销售客服」的候选按角色标记取（`apps/business/selectors.py::kefu_options`）。

    ⚠️ 与权限那条**同一条边界**：`if not was_created: continue` 之前就返回 ⇒ 已存在的角色
    **不重配**。理由与权限一样——**管理员可能在角色编辑页把这个勾去掉过**，种子无权把它翻回来。
    存量租户那两个角色由数据迁移 `0005_role_can_be_sales_agent` 置 `True`（那一次性做完），
    之后谁再改这个勾都是**管理员的决定**。⛔ 别把这一条挪到 `was_created` 判断之外：
    那会让「管理员取消勾选」在下次种子后静默复活（而 `role.permissions` 那半边仍是尊重的，
    两半就会互相矛盾）。
    """
    for name in BUILTIN_ROLE_MODULES:
        group, _ = Group.objects.get_or_create(name=name)
        role, was_created = Role.objects.get_or_create(
            name=name,
            defaults={
                "group": group,
                "remark": BUILTIN_ROLE_REMARKS[name],
                "can_be_sales_agent": name in KEFU_CANDIDATE_ROLES,
                "dashboard_type": {"代练员": "worker", "客服": "work", "客服主管": "work"}.get(name, ""),
            },
        )
        if not was_created:
            continue
        role.permissions.set(builtin_role_nodes(name))


def seed_rbac():
    """幂等入口：目录 + 内置角色。`seed_tenant` 与补灌命令都调它。

    返回 `seed_catalog()` 的 `(created, updated)`，让调用方（补灌命令）能打印实际结果。
    """
    created, updated = seed_catalog()
    # 内置角色的默认权限要落在目录之后——它按 `parent.code` 过滤。
    seed_builtin_roles()
    return created, updated
