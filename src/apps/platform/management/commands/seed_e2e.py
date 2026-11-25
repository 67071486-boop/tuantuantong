"""`seed_e2e`：给 E2E 租户灌一套合成夹具（幂等）。必须有它：新租户基线数据（`seed_tenant`）刻意不预置游戏与项目
（`apps/platform/provisioning.py` 的 `DEFAULT_GAMES = ()`），而黄金路径要录单就必须先有「游戏→项目分类→项目」三层配置——这一层由 E2E 自己补，不改 `seed_tenant`。
⛔ 两道闸门（会给账号设口令，绝不能变生产后门/灌错库）：`settings.DEBUG` 为假 ⇒ 拒绝；schema 不在显式白名单 `E2E_SCHEMAS` 里 ⇒ 拒绝（⛔ 不是 `e2e` 前缀匹配——隐式豁免会把 `e2e_other` 放进来，`AGENTS.md` §5G）。"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import Game, Project, ProjectCategory
from apps.business.roles import DEVELOPER, SERVICE, SUPERVISOR, TENANT_ADMIN, WORKER
from apps.platform.models import Tenant
from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import builtin_role_nodes

#: 读租户（默认目标）。本线**不动**它的行为：读模式仍然只用 `e2e`、只读。
E2E_SCHEMA = "e2e"
#: E2E 专用 schema 的显式白名单（判据 = 集合成员，⛔ 不是前缀匹配）。
#: - `e2e`：只读租户（默认）；`e2e_write`：写模式一次性租户（`reset_e2e_write` 建，见写模式 spec §2）。
#: 新增一个值必须同时改 `tests/test_seed_e2e.py` 里独立写死的那份期望（那份不是从本常量派生的）。
E2E_SCHEMAS = frozenset({"e2e", "e2e_write"})
E2E_GAME = "E2E 测试游戏"
#: 第 2 款游戏（spec §2 第 1 条）——合成名，⛔ 不是真名/真数据。它必须存在：在这条线之前 E2E 租户只有 1 款游戏，
#: 「角色只看得到某几款游戏」这条权限轴（`apps/business/game_scope.py::games_of`）一次都没被测过——「全给」与「只给一款」在库里是同一种状态，有第 2 款才可分辨。
#: ⚠️ 它会改变所有角色"可见游戏"的集合 ⇒ 报告必须逐账号写清"设计上应当挂几款"（见 `_assert_game_scopes_are_as_designed`）。
E2E_GAME_SECOND = "E2E 测试游戏乙"
#: 第 2 款游戏的排序值。⚠️ `Game.Meta.ordering` 是 `["-sort_order", "id"]`（**数字越大越靠前**）
#: ⇒ 取 `-1` 让**第 1 款**（`E2E_GAME`，`sort_order=0`）在各页下拉 / 列表里仍排在前面：
#: 「第 1 款」这个说法在**名字**与**展示顺序**上一致，减少"审计选了另一款"的歧义。
E2E_GAME_SECOND_SORT_ORDER = -1
E2E_CATEGORY = "E2E 测试分类"
E2E_PROJECT = "E2E 测试项目"

#: E2E 账号口令。**合成值**，只在本机 E2E 租户里有意义（同 `src/tests/p5_auth_helpers.py`
#: 的 `P2_TEST_PASSWORD` 先例）。⛔ 改这里必须**同一次**改 `e2e/fixtures/auth.ts` 的常量。
PASSWORDS = {
    "admin": "e2e-pw-admin",
    "cs001": "e2e-pw-cs001",
    "taker001": "e2e-pw-taker001",
    "nobody001": "e2e-pw-nobody001",
    # 本线（role-promise-audit）补的两个职位账号：客服主管 / 系统开发员。
    "supervisor001": "e2e-pw-supervisor001",
    "dev001": "e2e-pw-dev001",
    # 本线（minimal-role-audit）补的**自定义最小权限角色**账号（spec §2）。
    "minrole001": "e2e-pw-minrole001",
    # 本线（game-scope-audit）补的**单游戏范围角色**账号（spec §2）。
    "gamescope001": "e2e-pw-gamescope001",
}

#: minimal-role-audit（spec §2）的自定义最小权限角色——⛔ 不是内置组预设（`apps/rbac/seed.py::BUILTIN_ROLE_MODULES` 那五个），是一条自己建的 RBAC 角色。
#: 关联形状照 `apps/rbac/models.py`：账号挂 `auth.Group`、节点判据读 `Group → Role → permissions`（`user_node_codes`）⇒ 建同名 `Group` + 一条 `Role(group=…)`（OneToOne，两处名字必须成对出现）+ `set()` 节点。
#: 节点集显式枚举（只 `order.list`；「某些节点没有」正是本线要考的形态）——⛔ 不许写"内置预设减某几个"的派生（派生集会随预设变化静默变味）；单一出处 = `MINROLE_NODE_CODES`（`tests/test_seed_e2e.py` 用独立字面量再钉一份）。
MINROLE_GROUP = "最小权限"
#: `Role.name` 的 `max_length=5` ⇒ 这个名字必须 ≤5 个字符（「最小权限」= 4）。
MINROLE_ROLE_NAME = "最小权限"
#: `Role.remark`（角色说明），仅供人读。
MINROLE_REMARK = "E2E 自定义最小权限角色（只给 order.list；职位对拍用）"
MINROLE_NODE_CODES = ("order.list",)

#: game-scope-audit（spec §2 第 2 条）的单游戏范围角色：节点集照抄「客服」那一档、`StaffProfile.games` 只挂第 1 款——本线要的正是「节点够、游戏不够」这一格。
#: 节点集单一出处 = `apps/rbac/seed.py::builtin_role_nodes(SERVICE)`（内置角色授权用的那个函数本身，⛔ 不另抄一份栏目清单——会随内置预设静默漂移）；判据在 `tests/test_seed_e2e.py`：与真库内置「客服」角色的 `permissions` 集合相等。
#: ⚠️ 这是同源一致性检查（两侧都经 `builtin_role_nodes("客服")`，本质 `f(x)==f(x)`，⛔ 不是跨来源对拍）：挡得住档位被换/写路径分叉，挡不住该函数或「客服」清单本身被改坏 ⇒ 同条用例另补独立字面量（节点数 63 + 关键 code 子集）。
GAMESCOPE_GROUP = "客服单游戏"
#: `Role.name` 的 `max_length=5` ⇒ 必须 ≤5 个字符（「客服单游戏」= 5，正好到界）。
GAMESCOPE_ROLE_NAME = "客服单游戏"
#: `Role.remark`（角色说明），仅供人读。
GAMESCOPE_REMARK = "E2E 单游戏范围角色（节点照抄客服；游戏只挂第 1 款；职位对拍用）"
#: 节点集**照抄哪一个内置角色**。用 `apps/business/roles.py` 的常量（=「客服」），
#: ⛔ 不写字面量——名字改词时这里跟着改，不会变成一个查不到 key 的静默分支。
GAMESCOPE_NODE_SOURCE_ROLE = SERVICE
#: 账号名（进 `PASSWORDS`；`auth.ts::ACCOUNTS` 与 `01-auth-guard` 的 `PY_USERNAME` 同批加）。
GAMESCOPE_USERNAME = "gamescope001"

#: 提成模式。⚠️ 客服必须是 `PROFIT_PERCENT`：订单改成「已完成」时读销售客服的模式，
#: 是 `ORDER_PERCENT`（`StaffProfile` 默认值）就 200 + 业务码 1000 并整笔回滚。
#: ⚠️ 客服主管在订单线上同样会当 `sales_user` ⇒ 沿用客服模式；系统开发员持 `my_orders`（当接单人侧）⇒ 沿用代练员模式。
CS_COMMISSION_MODE = CommissionMode.PROFIT_PERCENT
TAKER_COMMISSION_MODE = CommissionMode.ORDER_PERCENT
E2E_COMMISSION_VALUE = 10

#: 应当持有 `StaffProfile` 的 E2E 账号——显式枚举。⛔ 刻意不写「凡不是 nobody001」的反选：反选是隐式豁免，
#: 每新增一个本该「无档案」的账号都得记得回来改判据；显式枚举的代价 = 新增应当有档案的账号要手工加进来，漏加不会被这条守卫发现
#: （它的职责是「枚举到的账号没被 seed 坏」，不是「枚举完整」）。不在内的两个按设计无档案：`admin` 走租户管理员短路（`games_of` ⇒ 全部游戏）、`nobody001` 是零权限对照组（spec §5）。
PROFILE_ACCOUNT_USERNAMES = (
    "cs001",
    "taker001",
    "supervisor001",
    "dev001",
    "minrole001",
    # 本线（game-scope-audit）新增的单游戏范围账号（spec §2）。
    GAMESCOPE_USERNAME,
)


def _assert_e2e_schema(schema: str) -> None:
    """闸门（纯函数）：目标 schema 必须在显式白名单 `E2E_SCHEMAS` 里，否则响亮拒绝。
    ⛔ 判据是集合成员、不是前缀；抽成纯函数是为了让白名单能被直接钉住——`handle()` 里那个位置若被改成 `startswith("e2e")` 这类隐式豁免，`tests/test_seed_e2e.py` 的白名单用例必须红。"""
    if schema not in E2E_SCHEMAS:
        raise CommandError(
            f"seed_e2e 只认 E2E 专用 schema，显式白名单 {sorted(E2E_SCHEMAS)}，"
            f"收到的是 {schema!r}（拒绝灌别的库）。"
        )


def _assert_seeded_accounts_are_usable(profiles):
    """校验「应当带档案的 E2E 账号」真的能用：档案存在，且 `games` 非空（spec §5 点名的假缺陷：游戏权限为空会让页面因权限不足而整页空，
    审计把「空」误判成「用不动」；⛔ 只判「档案存在」不够——没有档案与有档案零游戏页面上后果完全一样）。判据就这两条：抓得到「漏给 `commission_mode` ⇒ 档案被删」；
    ⛔ 抓不到「漏传 `all_games=True`」——那条由 `_assert_game_scopes_are_as_designed` 判（出现第 2 款游戏后两者才可分辨，见 spec `2026-09-24-role-promise-audit-design.md` §11）。`None`（按设计无档案）不报；不满足抛 `CommandError` 点名。"""
    problems = []
    for username in PROFILE_ACCOUNT_USERNAMES:
        profile = profiles.get(username)
        if profile is None:
            problems.append(f"{username}：没有 StaffProfile（该账号应当带档案）")
            continue
        if not profile.games.exists():
            problems.append(f"{username}：档案存在但 games 为空（页面会因游戏权限不足而空）")
    if problems:
        raise CommandError(
            "seed_e2e 种出来的 E2E 账号不可用（spec §5 要求枚举里的账号带档案且给游戏）："
            + "；".join(problems)
        )


def _assert_game_scopes_are_as_designed(profiles, expected_scopes):
    """校验每个带档案账号的 `games` 恰好是它在 `upsert` 里声明的范围——母规范 §11 残差要求的那条真判据（旧守卫在只有 1 款游戏时分不出「只给第 1 款」与「给全部」，出现 `E2E_GAME_SECOND` 后可分辨）。
    ⛔ 期望值不由写入分支派生：`expected_scopes` 按 `all_games` 声明算（True ⇒ 租户全集快照、False ⇒ {第 1 款}），「传了 `all_games=True` 但写入只落第 1 款」会在这里红；
    ⛔ 若改成读 `profile.games` 跟自己比则恒真、什么也证明不了。只查 `expected_scopes` 里的账号（admin/nobody001 按设计无档案不进表）；不满足抛 `CommandError` 点名期望/实际几款。"""
    problems = []
    for username, expected in expected_scopes.items():
        profile = profiles.get(username)
        if profile is None:
            # 「无档案」由上面那条守卫负责报；这里不重复（它不进 expected_scopes 才对，
            # 进来了说明调用方口径不一致 ⇒ 也点名报出来）。
            problems.append(f"{username}：声明了一个游戏范围，却没有档案")
            continue
        actual = set(profile.games.values_list("pk", flat=True))
        if actual != set(expected):
            problems.append(
                f"{username}：声明的游戏范围是 {sorted(expected)}（{len(expected)} 款），"
                f"库里实际是 {sorted(actual)}（{len(actual)} 款）"
            )
    if problems:
        raise CommandError(
            "seed_e2e 种出来的账号游戏范围与声明不符（母规范 §11 那条残差要求的可分辨判据）："
            + "；".join(problems)
        )


def _seed_minrole():
    """建/收敛 minimal-role-audit 的自定义最小权限角色（spec §2）。关联形状照 `apps/rbac/models.py`：账号挂 `auth.Group`、节点判据读 `Group → Role → permissions`
    ⇒ 建 `MINROLE_GROUP` 同名 `Group` + 一条 `Role(group=…)` + `set()` 显式枚举节点。⚠️ 幂等口径与内置角色相反（有意）：每次重跑都把权限重设成 `MINROLE_NODE_CODES`——
    E2E 夹具「重跑结果确定」比「保全手工改动」重要（否则被改过的存量角色会让审计静默换被测对象）；缺节点 ⇒ `CommandError`，⛔ 不许静默建零节点角色。"""
    group, _ = Group.objects.get_or_create(name=MINROLE_GROUP)
    role, _ = Role.objects.get_or_create(
        name=MINROLE_ROLE_NAME,
        defaults={"group": group, "remark": MINROLE_REMARK},
    )
    # 收敛两个可能漂移的指针（都按本常量走，重跑结果确定）。
    updates = []
    if role.group_id != group.id:
        role.group = group
        updates.append("group")
    if role.remark != MINROLE_REMARK:
        role.remark = MINROLE_REMARK
        updates.append("remark")
    if updates:
        role.save(update_fields=updates)
    nodes = list(PermissionNode.objects.filter(code__in=MINROLE_NODE_CODES))
    missing = sorted(set(MINROLE_NODE_CODES) - {n.code for n in nodes})
    if missing:
        raise CommandError(
            f"E2E 租户的 RBAC 目录里缺这些节点：{missing} —— 自定义最小权限角色建不出来"
            "（先确认租户供给时跑过 `seed_rbac`）。"
        )
    role.permissions.set(nodes)
    return role


def _seed_gamescope():
    """建/收敛 game-scope-audit 的单游戏范围角色（spec §2 第 2 条）：形状与 `_seed_minrole` 同构（同名 Group + Role + set()），幂等口径相同（每次重跑重设，理由同上）。
    ⚠️ 节点集调既有的 `builtin_role_nodes(GAMESCOPE_NODE_SOURCE_ROLE)`——内置角色授权用的同一个函数 ⇒ 与内置「客服」同源，⛔ 不另写一份栏目清单（会随预设漂移）；游戏范围不在这里给（由调用方 `all_games=False` 落成只挂第 1 款，节点与游戏是两条独立的轴）。
    目录缺失/空集 ⇒ `CommandError`（⛔ 不许静默建零节点角色；空集还会让"与客服相等"退化成"空 = 空"，那是最贵的假绿）。"""
    group, _ = Group.objects.get_or_create(name=GAMESCOPE_GROUP)
    role, _ = Role.objects.get_or_create(
        name=GAMESCOPE_ROLE_NAME,
        defaults={"group": group, "remark": GAMESCOPE_REMARK},
    )
    # 收敛两个可能漂移的指针（都按本常量走，重跑结果确定）。
    updates = []
    if role.group_id != group.id:
        role.group = group
        updates.append("group")
    if role.remark != GAMESCOPE_REMARK:
        role.remark = GAMESCOPE_REMARK
        updates.append("remark")
    if updates:
        role.save(update_fields=updates)
    nodes = list(builtin_role_nodes(GAMESCOPE_NODE_SOURCE_ROLE))
    if not nodes:
        raise CommandError(
            f"E2E 租户的 RBAC 目录里查不到「{GAMESCOPE_NODE_SOURCE_ROLE}」那一档的任何节点 —— "
            "单游戏范围角色建不出来（先确认租户供给时跑过 `seed_rbac`）。"
        )
    role.permissions.set(nodes)
    return role


class Command(BaseCommand):
    help = (
        "给 E2E 租户灌一套合成夹具（幂等；DEBUG=false 或 schema 不在 E2E 白名单时拒绝）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            default=E2E_SCHEMA,
            help=(
                f"目标租户 schema；本命令只接受白名单 {sorted(E2E_SCHEMAS)} 里的值"
                "（该参数只作闸门跳板，别的值一律拒绝）"
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_e2e 只在 DEBUG=true 下运行——它会给账号设口令，绝不能碰非开发环境。"
            )
        schema = options["schema"]
        _assert_e2e_schema(schema)

        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(
                f"schema {schema!r} 不存在——先跑 `node e2e/lib/tenant.mjs` 供给租户。"
            )

        _seed_tenant_fixtures(tenant)

        self.stdout.write(
            f"✅ E2E 夹具已就绪（库={settings.DATABASES['default']['NAME']}，schema={schema}）："
            f"游戏「{E2E_GAME}」＋「{E2E_GAME_SECOND}」/ 分类 / 项目（只挂第 1 款），"
            f"账号 {'、'.join(PASSWORDS)}"
        )


def _seed_tenant_fixtures(tenant):
    """在 `tenant` 的 schema 里灌一整套 E2E 夹具，并在收尾校验账号可用性。单独抽出来是因为 `handle()` 之前的两道闸门（DEBUG/白名单）会挡住任何"拿测试租户跑一遍主体"的写法
    ⇒ `tests/test_seed_e2e.py` 的租户夹具可以直接调本函数、用真库断言。⛔ 它不是给测试用的旁路：生产路径只有 `Command.handle()` 一个调用点（闸门 → 租户查找 → 本函数）；
    整段写入在一个事务里，守卫抛错时整体回滚，不留"一半种好"的库。"""
    with tenant_context(tenant), transaction.atomic():
        game, _ = Game.objects.get_or_create(name=E2E_GAME, defaults={"sort_order": 0})
        # 本线（game-scope-audit）的第 2 款游戏（spec §2 第 1 条）。⛔ 顺序承重：
        # 它必须建在**下面那些 `upsert` 之前**——`all_games=True` 那一支写的是"当时的全部游戏"
        # 快照，建晚了那几个账号就只拿得到第 1 款（正好是母规范 §11 那条残差点名的形态）。
        Game.objects.get_or_create(
            name=E2E_GAME_SECOND, defaults={"sort_order": E2E_GAME_SECOND_SORT_ORDER}
        )
        category, _ = ProjectCategory.objects.get_or_create(
            game=game, name=E2E_CATEGORY, defaults={"sort_order": 0}
        )
        Project.objects.get_or_create(
            category=category, name=E2E_PROJECT, defaults={"base_price": "100.00", "sort_order": 0}
        )

        #: 租户全集的主键快照（在 upsert 之前取）。⛔ 它是 `all_games=True` 的期望值来源（见
        #: `_assert_game_scopes_are_as_designed` 的"期望值不由写入分支派生"段）；⛔ 不要改成写入时
        #: 从 `Game.objects.all()` 现取——那会让期望与写入同源、退化成自指。
        tenant_game_ids = set(Game.objects.values_list("pk", flat=True))
        #: `{username: 声明的游戏集}`（只有带档案的账号在内）。收尾的 G3 守卫读它。
        declared_game_scopes = {}

        user_model = get_user_model()

        #: `upsert` 实际种出来的档案（`None` = 被收敛成「无档案」）。收尾的守卫读它——
        #: 它要验的正是「这一次真的写成了什么」，所以不另查一遍库。
        seeded_profiles = {}

        def upsert(username, group_name=None, commission_mode=None, all_games=False):
            user, _ = user_model.objects.get_or_create(username=username)
            user.set_password(PASSWORDS[username])  # 每次都设 ⇒ 重跑结果确定
            user.is_active = True
            # ⚠️ `display_name`（账号接口那个字段）落的是 **`user.first_name`**
            # （`apps/accounts/views.py:1009-1010`），**不是** `StaffProfile` 的列。
            # 这里把两者设成同一个值：指派弹层的下拉显示哪个都确定，T5 就能按可见文本点名。
            user.first_name = username
            user.save()
            if group_name:
                group, _ = Group.objects.get_or_create(name=group_name)
                user.groups.set([group])  # 职位是**单选**（D8）
            else:
                # 收敛到「零职位」。只"不创建"收敛不了历史遗留的组：重跑会把它们原样留下 ⇒
                # 本命令声称的幂等不成立，而 T6 的「零权限必须 403」正以「零权限」为前件。
                user.groups.clear()
            if commission_mode is not None:
                profile, _ = StaffProfile.objects.get_or_create(user=user)
                profile.commission_mode = commission_mode
                profile.commission_value = E2E_COMMISSION_VALUE
                profile.save()
                # 录单要求「当前账号持有该游戏」⇒ 枚举里带档案的账号都必须挂游戏（权限为空会让页面因权限不足而空，审计把「空」误判成「用不动」）。
                # ⚠️ `all_games` 只在 `commission_mode is not None` 这一支被消费：漏给 `commission_mode` 时它被静默忽略（且走进 else 删档案）⇒ 收尾 `_assert_seeded_accounts_are_usable` 为这个漏法兜底。
                # 两支：False ⇒ 只挂第 1 款；True ⇒ 当前租户全部游戏（口径先例 `tests/p5_auth_helpers.py::_apply_games` 默认支）。⚠️ 与 `games_of` 管理员分支同义但性质不同——那条是动态重算，这里 `set(...)` 是写入当时快照 ⇒ 之后新增的游戏不会自动进这些账号。
                profile.games.set(Game.objects.all() if all_games else [game])
                seeded_profiles[username] = profile
                # G3 的期望值：**按声明（`all_games`）算**，⛔ 不是读 `profile.games` 反推
                # （那样是自指，见 `_assert_game_scopes_are_as_designed` 的说明）。
                declared_game_scopes[username] = (
                    set(tenant_game_ids) if all_games else {game.pk}
                )
            else:
                # 同 `groups`：不建档案的账号（admin / nobody001）也收敛到「无档案」，
                # 否则遗留档案会让提成模式/游戏权限与接口约定不一致。⚠️ 没有别的模型以 PROTECT
                # 指向 `StaffProfile` ⇒ 删除只带走档案行与游戏关联行；历史订单挂 `Order.sales_user`（指 `auth.User`）不受影响。
                StaffProfile.objects.filter(user=user).delete()
                seeded_profiles[username] = None
            return user

        upsert("admin", TENANT_ADMIN)
        upsert("cs001", SERVICE, CS_COMMISSION_MODE)
        upsert("taker001", WORKER, TAKER_COMMISSION_MODE)
        # role-promise-audit 新增：五个职位都要能登录（spec §5）。两者带档案 + 全部游戏（理由见上面 `all_games` 那段）。
        # ⚠️ 收尾 `_assert_seeded_accounts_are_usable` 只验「档案存在 + games 非空」，验不出拿到几款；
        # 「拿到几款」由 `_assert_game_scopes_are_as_designed` 单独判（母规范 §11 那条残差）。
        upsert("supervisor001", SUPERVISOR, CS_COMMISSION_MODE, all_games=True)
        upsert("dev001", DEVELOPER, TAKER_COMMISSION_MODE, all_games=True)
        # minimal-role-audit 新增：自定义最小权限角色 + 它的账号（spec §2）。⚠️ 顺序承重：角色必须在账号之前建好——
        # 账号挂的是同名 Group，角色没建好时账号会是「有组但零节点」，与本线要考的「只给 order.list」不是同一件事（静默换被测对象的形态）。
        _seed_minrole()
        # 提成模式对只读审计无影响（本角色没有任何写动作），取默认那一支（代练员同款）。
        upsert("minrole001", MINROLE_GROUP, TAKER_COMMISSION_MODE, all_games=True)
        # 本线（game-scope-audit）新增：**单游戏范围角色** + 它的账号（spec §2）。
        # ⚠️ 与 `_seed_minrole` 同一条顺序承重：角色必须先建好（账号挂同名 Group）。
        _seed_gamescope()
        # ⚠️ `all_games=False` 是承重的：本线要的正是「节点够、游戏不够」——只挂第 1 款，⛔ 不含第 2 款。它在订单线上会当 `sales_user`
        # ⇒ 提成模式必须与客服同款（`PROFIT_PERCENT`）。⛔ 用户名这里写字面量：`05` 那条跨来源结构钉子按 `upsert("…", <组常量>` 的源码文本
        # 解析"种子里该被审计的账号"，写成 `GAMESCOPE_USERNAME` 会让它解析不到 ⇒ 那条钉子会以"矩阵里多一个账号"红。
        upsert("gamescope001", GAMESCOPE_GROUP, CS_COMMISSION_MODE, all_games=False)
        # 「完全没有权限」= 不挂职位组、不建档案：以**节点判据**把关的端点会 403
        # （机制：`nodes_of` 对无组用户取空集 ⇒ 判据为假；端到端由 T6 的越权用例兜底）。
        upsert("nobody001")

        # 收尾校验（真的接在生产路径上）：账号没被种成「有档案 + 有游戏」时，审计会把「页面因权限不足而空」
        # 误判成「控件用不动」⇒ 宁可让命令响亮失败。⚠️ 顺序承重：先「可用性」（文案被既有用例按
        # `assertRaisesMessage` 钉着），再「游戏范围与声明一致」（母规范 §11 残差要求的那条判据）。
        _assert_seeded_accounts_are_usable(seeded_profiles)
        _assert_game_scopes_are_as_designed(seeded_profiles, declared_game_scopes)
