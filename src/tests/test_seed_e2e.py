"""`seed_e2e` 的两道闸门、「账号可用性」守卫，以及**真库**上的生产路径判据。

⚠️ 本文件原先只写「真库那半由 E2E 的端到端跑覆盖」——**这不够**：E2E 跑的是"种好之后"的路径，
而「命令在种不出可用账号时会不会响亮地失败」没有任何用例执行过。复评 2026-09-24 实测：
把 `_assert_seeded_accounts_are_usable(seeded_profiles)` 整行删掉，本文件仍然 `Ran 5 tests OK`
（上半那些用例是 `SimpleTestCase`，`handle()` 的主体从不执行）。真库判据见文件末尾的
`SeedE2EOnRealTenantTests`。
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import Game
from apps.platform.management.commands.seed_e2e import (
    E2E_GAME,
    E2E_GAME_SECOND,
    E2E_SCHEMA,
    E2E_SCHEMAS,
    GAMESCOPE_GROUP,
    GAMESCOPE_ROLE_NAME,
    GAMESCOPE_USERNAME,
    MINROLE_GROUP,
    MINROLE_ROLE_NAME,
    PROFILE_ACCOUNT_USERNAMES,
    Command,
    _assert_e2e_schema,
    _assert_seeded_accounts_are_usable,
    _seed_tenant_fixtures,
)
from apps.rbac.models import Role
from apps.rbac.seed import BUILTIN_ROLE_MODULES, seed_rbac


class SeedE2EGuardTests(SimpleTestCase):
    def test_refuses_when_debug_is_off(self):
        """⛔ 这道闸门的存在理由：本命令会给账号设口令，绝不能在非 DEBUG 环境跑。"""
        with override_settings(DEBUG=False):
            with self.assertRaisesMessage(CommandError, "只在 DEBUG=true 下运行"):
                Command().handle(schema=E2E_SCHEMA)

    def test_refuses_non_e2e_schema(self):
        """闸门二：schema 必须是 E2E 专用白名单里的值，防止误灌别人的库。

        ⚠️ 断言的锚点必须是**闸门的判别内容**（`只认 E2E 专用 schema`），不能只写 `e2e`——
        后者会被消息里的命令名 `seed_e2e` 满足：把判别文案改坏、只留命令名，用例照样绿。
        异常类型也收窄到命令实际抛的 `CommandError`（裸 `Exception` 把「根本没抛」之外的一切都算过）。

        ⚠️ `e2e_other` 是**故意的第三个 schema**（W4）：它最贴近白名单里的 `e2e_write`，
        也最容易被"以 `e2e` 开头"那类前缀匹配错误放行 ⇒ 它在，才能钉住白名单是**集合成员**判据。
        """
        with override_settings(DEBUG=True):
            for schema in ("dev", "tt_dev", "e2e_other", "e2e_write_extra"):
                with self.subTest(schema=schema):
                    with self.assertRaisesMessage(CommandError, "只认 E2E 专用 schema"):
                        Command().handle(schema=schema)


class E2ESchemaWhitelistTests(SimpleTestCase):
    """W4 的正面/反面判据：`seed_e2e` 的 schema 闸门是**显式白名单**，不是前缀匹配。

    **为什么单独一组**：`handle()` 在闸门之后还有「租户不存在」这一步，光靠 `handle()` 分不清
    "被闸门拒绝"与"过了闸门但租户没建"；而且闸门改坏成 `startswith("e2e")` 时，
    `e2e_other` 会**过闸门**、随后以"租户不存在"报错 —— 只看"抛没抛"的用例照样绿。
    所以这里的判据是**闸门函数本身**（`_assert_e2e_schema`）+ **白名单的字面值**，两条都要能红。
    """

    def test_accepts_every_whitelisted_schema(self):
        """正面：白名单里的两个值都**不抛**（`e2e` 是读租户，`e2e_write` 是写模式租户）。"""
        for schema in ("e2e", "e2e_write"):
            with self.subTest(schema=schema):
                _assert_e2e_schema(schema)  # 不抛即通过

    def test_rejects_third_schema(self):
        """反面：第三个 schema ⇒ 抛，文案点名白名单。

        ⛔ `e2e_other` / `e2e_write_extra` 是"前缀陷阱"：改成前缀匹配时它们会**过闸门**。
        ⛔ `tt_dev` 是"别的租户库"：白名单被悄悄放宽成"凡是开发环境能遇到的都放行"时会漏掉它。
        """
        for schema in ("e2e_other", "e2e_write_extra", "tt_dev", "e2eX", ""):
            with self.subTest(schema=schema):
                with self.assertRaisesMessage(CommandError, "只认 E2E 专用 schema"):
                    _assert_e2e_schema(schema)

    def test_whitelist_values_are_the_literal_pair(self):
        """白名单的**外部契约值**用字面量钉住（⛔ 不是"读常量再比常量"的自指断言，E-0024）。

        ⚠️ 光有"拒绝第三个 schema"还不够：把白名单**放宽**（例如塞进 `e2e_other`）时，
        表里的 `{"e2e", "e2e_write"}` 与常量不等 ⇒ 本用例红。
        """
        self.assertEqual(set(E2E_SCHEMAS), {"e2e", "e2e_write"})
        # 默认 schema（读租户）也不能漂：它决定 `--schema` 不传时灌到哪个租户。
        self.assertEqual(E2E_SCHEMA, "e2e")


class _FakeGames:
    """`StaffProfile.games` 的最小替身：守卫只调 `.exists()`（多给方法反而会掩盖接口漂移）。"""

    def __init__(self, names=()):
        self._names = tuple(names)

    def exists(self):
        return bool(self._names)


class _FakeProfile:
    def __init__(self, games=()):
        self.games = _FakeGames(games)


class SeededAccountsUsableGuardTests(SimpleTestCase):
    """守卫 `_assert_seeded_accounts_are_usable` 自己的用例。

    **它为什么存在**（评审实测的缺口）：去掉两个新账号的 `all_games=True`、或让它们
    `commission_mode` 传 `None`（会走 `else` 删档案）时，**原来没有任何用例会红**；
    而本线的审计会因此在「没有游戏权限 ⇒ 页面空」上**静默丢覆盖**——正是 spec §5 点名要防的假缺陷。

    ⚠️ **「应当有档案的账号」这份枚举写在下面，是**字面量**，⛔ 不从 `seed_e2e` 导常量**：
    拿命令自己的常量去断言命令自己 = 自指断言（`E-0024`），常量被改坏时用例不会红。
    """

    #: 与 `seed_e2e.py` 的 `PROFILE_ACCOUNT_USERNAMES` **逐字一致**，但**独立写死**（见类 docstring）。
    #: `admin` / `nobody001` **不在内**：它们按设计无档案（前者走管理员短路，后者是「零权限」对照）。
    SHOULD_HAVE_PROFILE = ("cs001", "taker001", "supervisor001", "dev001", "minrole001", "gamescope001")

    def _profiles(self, without_games=(), without_profile=()):
        """造一份输入的 `{username: _FakeProfile | None}`；两个参数用来构造反面输入。"""
        profiles = {}
        for username in self.SHOULD_HAVE_PROFILE:
            if username in without_profile:
                profiles[username] = None
            else:
                profiles[username] = _FakeProfile(() if username in without_games else ("game#1",))
        return profiles

    def test_accepts_usable_accounts(self):
        """正面：枚举里该有档案的账号都带着游戏 ⇒ 不抛（`admin` / `nobody001` 传 `None` 也不抛）。"""
        profiles = self._profiles()
        profiles["admin"] = None
        profiles["nobody001"] = None
        _assert_seeded_accounts_are_usable(profiles)  # 不抛即通过

    def test_rejects_account_without_games(self):
        """反面①：某个账号的档案**零游戏** ⇒ 抛，且文案点名账号与缺什么。

        ⚠️ 这条正是评审实测的缺口：把守卫里「`games` 非空」的判据删掉 / 改成恒真，本用例必须红
        。
        """
        with self.assertRaisesMessage(
            CommandError, "cs001：档案存在但 games 为空"
        ):
            _assert_seeded_accounts_are_usable(self._profiles(without_games=("cs001",)))

    def test_rejects_account_without_profile(self):
        """反面②：某个账号的档案**整个不存在** ⇒ 也抛（这与「零游戏」在页面上后果相同，都要判）。

        ⚠️ 只判「档案存在」的守卫会漏掉「档案在、游戏空」那一支；反过来只判 games 也会漏掉这一支。
        """
        with self.assertRaisesMessage(
            CommandError, "taker001：没有 StaffProfile"
        ):
            _assert_seeded_accounts_are_usable(self._profiles(without_profile=("taker001",)))


class SeedE2EOnRealTenantTests(TenantTestCase):
    """`seed_e2e` 在**真库**上的判据：跑生产路径（`_seed_tenant_fixtures`，以及 `handle()` 全路径）。

    **为什么必须补它**（复评实测的缺口）：本文件上半全是 `SimpleTestCase`，与 `handle()` 的
    主体无关 ⇒ 把守卫那一行整行删掉，`Ran 5 tests OK` 照样绿。这里把「种出来的账号到底能不能用」
    钉在**真库状态**（`auth_user` 行 + `StaffProfile` 行 + `profile.games` 的关联行）上。

    ⛔ **不用 spy / `mock.patch` 断言「守卫被调用过」**——那只证明接线、证明不了效果（复评原话）。
    效果由两条判据钉，各自的可证伪性不同，**别混**：

    1. `test_seed_fixtures_...`：正常路径 ⇒ 真库状态可用。
       ⚠️ **它单独不可能因为"删掉守卫"而红**——守卫只**校验**状态、不**生产**状态；
       删了它，种出来的库照样是对的。所以它证明的是 **seeding 本身**的结果，不是守卫的接线。
    2. `test_refuses_when_all_games_accounts_get_no_games`：**故意让 `all_games=True` 的
       账号一款游戏都拿不到**（= 复评点名的「漏传 `all_games=True`」在库里的后果）
       ⇒ 命令必须**响亮拒绝**、且**不留半成品**。**这条才是"守卫接在生产路径上"的可证伪判据：
       删掉守卫那一行，它必红。**

    ⚠️ 「应当有档案的账号」这份枚举在本类里**独立写死**，⛔ 不从 `seed_e2e` 导常量：
    拿命令自己的常量去断言命令自己 = 自指断言（`E-0024`），常量被改坏时用例不会红。
    """

    #: 与 `seed_e2e.py` 的 `PROFILE_ACCOUNT_USERNAMES` **逐字一致**，但**独立写死**（见类 docstring）。
    SHOULD_HAVE_PROFILE = ("cs001", "taker001", "supervisor001", "dev001", "minrole001", "gamescope001")
    #: 按设计**无档案**的两个（前者走管理员短路，后者是「零权限」对照）。
    NO_PROFILE = ("admin", "nobody001")
    ALL_ACCOUNTS = (
        "admin",
        "cs001",
        "taker001",
        "supervisor001",
        "dev001",
        "minrole001",
        "gamescope001",
        "nobody001",
    )

    @classmethod
    def get_test_schema_name(cls):
        """测试租户的 schema 名就取字面量 `e2e`：`handle()` 的第二道闸门只认它。

        这样本类才能顺带跑 `handle()` 的**全路径**（`test_handle_runs_the_same_seeding_path`），
        把「抽出来的函数真的挂在 `handle()` 上」也钉住——单独调 `_seed_tenant_fixtures` 证明不了这一点。
        """
        return "e2e"

    def setUp(self):
        """目录 + 内置角色先落地（**本线新增的依赖**）。

        ⚠️ `_seed_tenant_fixtures` 自本线起会建一个**自定义角色**，而它按 `code` 取
        `PermissionNode`（`order.list`）⇒ 测试租户里必须先有目录。真生产路径上这一步由
        `provision_tenant_db → seed_tenant → seed_rbac` 保证（`tests/test_rbac_seed.py` 有钉子），
        但 `TenantTestCase` 只建 schema、不灌种子 ⇒ 这里显式补，⛔ 不是给生产路径开后门。
        """
        super().setUp()
        seed_rbac()

    def _assert_real_db_state(self):
        """真库判据：该建的账号建出来了；该有档案的账号有档案且 `games` 非空。"""
        user_model = get_user_model()
        for username in self.ALL_ACCOUNTS:
            with self.subTest(username=username):
                self.assertTrue(
                    user_model.objects.filter(username=username).exists(),
                    f"{username} 没被种出来",
                )
        self.assertTrue(
            Game.objects.filter(name=E2E_GAME).exists(),
            "E2E 游戏没落库（页面清单与录单都依赖它）",
        )
        for username in self.SHOULD_HAVE_PROFILE:
            with self.subTest(username=username):
                profile = StaffProfile.objects.filter(user__username=username).first()
                self.assertIsNotNone(profile, f"{username} 应当带 StaffProfile")
                if profile is not None:
                    self.assertTrue(
                        profile.games.exists(),
                        f"{username} 的档案在、但 games 为空（页面会因游戏权限不足而空）",
                    )
        for username in self.NO_PROFILE:
            with self.subTest(username=username):
                self.assertFalse(
                    StaffProfile.objects.filter(user__username=username).exists(),
                    f"{username} 按设计不应带档案（本判据只管应当有档案的那四个）",
                )

    def test_seed_fixtures_makes_accounts_usable_in_real_db(self):
        """正常路径：直接调生产路径那一段 `_seed_tenant_fixtures(tenant)` ⇒ 真库状态必须可用。

        ⚠️ 这条**不能**证明守卫接在生产路径上（见类 docstring 第 1 条）——它证明的是 seeding 的结果。
        """
        _seed_tenant_fixtures(self.tenant)
        self._assert_real_db_state()

    def test_handle_runs_the_same_seeding_path(self):
        """`handle()` 的**全路径**：两道闸门 → 租户查找 → `_seed_tenant_fixtures` → 守卫。

        测试租户的 schema 名就取 `e2e`（见 `get_test_schema_name`）⇒ 闸门二放行；
        再叠一个 `DEBUG=True` 过闸门一（Django 测试环境默认 `DEBUG=False`）。
        """
        with override_settings(DEBUG=True):
            Command().handle(schema="e2e")
        self._assert_real_db_state()

    def test_handle_admits_write_schema_past_the_gate(self):
        """W4 的"不抛"一侧接在 `handle()` 上：`e2e_write` 必须**过 schema 闸门**。

        测试租户的 schema 名是 `e2e` ⇒ 这里查不到 `e2e_write`，于是报的是**租户查找**那一句
        （`schema 'e2e_write' 不存在`），而**不是**闸门文案。两条文案不同，所以本用例能分辨
        「过闸门」与「被闸门拒绝」——只断言"抛了 CommandError"是分辨不出来的。

        ⚠️ 这条也钉住 `handle()` 真的**调用**了闸门（把 `_assert_e2e_schema(schema)` 换成
        `schema.startswith("e2e")` 时本用例仍绿，但 `E2ESchemaWhitelistTests` 那三条会红）。
        """
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, "schema 'e2e_write' 不存在"):
                Command().handle(schema="e2e_write")

    def test_handle_rejects_third_schema_at_the_gate(self):
        """W4 的"抛"一侧接在 `handle()` 上：第三个 schema 在**闸门**就被拒（不是"租户不存在"）。

        文案锚点是闸门那一句 ⇒ 若闸门被删掉，本用例会拿到"schema 'e2e_other' 不存在"而红。
        """
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, "只认 E2E 专用 schema"):
                Command().handle(schema="e2e_other")

    def test_refuses_when_all_games_accounts_get_no_games(self):
        """**守卫承重的那一条**：让两个 `all_games=True` 的账号拿不到任何游戏 ⇒ 命令必须拒绝。

        构造方式：把「全部游戏」那一支的**数据来源**（`Game.objects.all()`）换成空集
        ⇒ `supervisor001` / `dev001` 的 `profile.games` 被 `set()` 成空，正是「漏传
        `all_games=True`」在库里的后果（页面因游戏权限不足而空 ⇒ 审计读成没问题）。

        ⛔ patch 打在**数据来源**上，不是守卫上；断言的是**效果**（点名账号地拒绝 + 无半成品），
        不是「守卫被调用过」。删掉 `_seed_tenant_fixtures` 末尾那行守卫调用 ⇒ 本用例必红。
        """
        with mock.patch.object(Game.objects, "all", return_value=Game.objects.none()):
            with self.assertRaisesMessage(CommandError, "supervisor001"):
                _seed_tenant_fixtures(self.tenant)

        # 拒绝时整段写入必须回滚（`transaction.atomic()`）：不留「一半种好」的库。
        user_model = get_user_model()
        for username in self.ALL_ACCOUNTS:
            with self.subTest(username=username):
                self.assertFalse(
                    user_model.objects.filter(username=username).exists(),
                    f"{username} 残留——守卫拒绝时整段写入应当回滚",
                )


class MinroleSeedOnRealTenantTests(TenantTestCase):
    """本线（minimal-role-audit）的**自定义最小权限角色**：真库上的形状判据。

    **它守的是什么**：本线 spec §2 要求这个角色是**自定义 RBAC 角色**（不是内置组预设）、
    节点集**显式枚举、只给 `order.list`**，而账号 `minrole001` 挂在**同名 Group** 上
    （节点判据读的是 `Group → Role → permissions`）。这几条只要漂一条，审计就会被喂一个
    **不同于设计的被测对象**（典型：角色变成零节点 ⇒ 页面因权限不足而空，被读成"没问题"），
    而那是本线最贵的假象形态。

    ⚠️ 期望值全部**独立写死**（⛔ 不从 `seed_e2e` 导常量）：拿命令自己的常量去断言命令自己
    = 自指断言（`E-0024`），常量被改坏时用例不会红。
    """

    @classmethod
    def get_test_schema_name(cls):
        return "e2e"

    #: 期望的节点集——**独立字面量**（spec §2：只给 order.list，⛔ 不给截图那两个点）。
    EXPECTED_NODE_CODES = frozenset({"order.list"})
    EXPECTED_GROUP = "最小权限"
    EXPECTED_ROLE_NAME = "最小权限"
    EXPECTED_USERNAME = "minrole001"

    def setUp(self):
        super().setUp()
        # 目录 + 内置角色先落地（自定义角色按 code 取 PermissionNode）。
        seed_rbac()

    def test_role_has_exactly_the_enumerated_nodes(self):
        """节点集**恰好**是显式枚举的那一个：多了=越权，少了=考不出本线要考的点。"""
        _seed_tenant_fixtures(self.tenant)
        role = Role.objects.filter(name=self.EXPECTED_ROLE_NAME).first()
        self.assertIsNotNone(role, f"自定义角色「{self.EXPECTED_ROLE_NAME}」没被种出来")
        self.assertEqual(
            role.group.name,
            self.EXPECTED_GROUP,
            "Role.group 必须与账号挂的 Group 同名（`Role.group` 是 OneToOne，见 models.py）",
        )
        self.assertEqual(
            set(role.permissions.values_list("code", flat=True)),
            set(self.EXPECTED_NODE_CODES),
            "最小权限角色的节点集与 spec §2 的显式枚举不一致",
        )

    def test_account_hangs_on_the_custom_group(self):
        """账号恰好在自定义角色的 Group 上（多一个组 ⇒ 节点判据读到的并集会变）。"""
        _seed_tenant_fixtures(self.tenant)
        user = get_user_model().objects.get(username=self.EXPECTED_USERNAME)
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {self.EXPECTED_GROUP},
            f"{self.EXPECTED_USERNAME} 必须恰好挂在自定义角色的同名 Group 上",
        )

    def test_role_is_not_a_builtin_preset(self):
        """⛔ 它不许是内置预设之一（spec §2）：内置角色的节点是**按栏目批发**的。

        ⚠️ 期望值来自 `BUILTIN_ROLE_MODULES` 这个**外部常量**（不是本用例自己算的集合）：
        把「最小权限」加进内置表 ⇒ 本用例红。
        """
        _seed_tenant_fixtures(self.tenant)
        self.assertNotIn(
            self.EXPECTED_ROLE_NAME,
            set(BUILTIN_ROLE_MODULES),
            "最小权限角色被塞进了内置预设表 ⇒ 它不再是 spec §2 要的**自定义**角色",
        )
        codes = set(
            Role.objects.get(name=self.EXPECTED_ROLE_NAME).permissions.values_list("code", flat=True)
        )
        # 内置「客服」那类批发会带上栏目码 `order`；自定义角色必须一个栏目码都没有。
        self.assertNotIn("order", codes, "自定义角色拿到了栏目级批发 ⇒ 那正是本线要避免的形态")

    def test_reseeding_is_idempotent_for_the_custom_role(self):
        """重跑结果确定（spec §2 的幂等要求）：角色仍只有一条、节点集原样。"""
        _seed_tenant_fixtures(self.tenant)
        _seed_tenant_fixtures(self.tenant)
        roles = Role.objects.filter(name=self.EXPECTED_ROLE_NAME)
        self.assertEqual(roles.count(), 1, "重跑后出现了第二条同名自定义角色")
        self.assertEqual(
            set(roles.get().permissions.values_list("code", flat=True)),
            set(self.EXPECTED_NODE_CODES),
        )


class GamescopeSeedOnRealTenantTests(TenantTestCase):
    """本线（game-scope-audit）的**单游戏范围角色** + **第 2 款游戏**：真库上的形状判据（G1 / G3）。

    **它守的是什么**：spec §2 要求这个角色「**节点集照抄客服那一档**、`StaffProfile.games`
    **只挂第 1 款**」——两条轴各错一条，审计就会被喂一个**不同于设计的被测对象**，
    而这条线正因为"看不到部分游戏"的形态**没有报错**（页面空 / 行数少都是**设计**）
    ⇒ 设计漂移不会自己发声。所以两条轴各钉一条判据。

    ⚠️ 期望值全部**独立写死**（⛔ 不从 `seed_e2e` 导常量）：拿命令自己的常量去断言命令自己
    = 自指断言（`E-0024`），常量被改坏时用例不会红。
    ⚠️ **例外**：`test_role_nodes_equal_the_builtin_service_role` 的**相等**那一条两侧**同源**
    ——左侧（本角色）与右侧（内置「客服」角色）都由 `builtin_role_nodes("客服")` 产出
    ⇒ 本质是 `f(x) == f(x)`，是**同源一致性检查**，⛔ 不是跨来源对拍。它能挡「档位被换」
    「两条写路径分叉」，**挡不住** `builtin_role_nodes` 或「客服」那一档被一起改坏
    ⇒ 同一条用例里另补了**独立字面量**断言（节点数 63 + 关键 code 子集），那条才钉外部契约值。
    """

    @classmethod
    def get_test_schema_name(cls):
        return "e2e"

    #: 节点集**照抄哪一个内置角色**——独立字面量（⛔ 不引 `seed_e2e.GAMESCOPE_NODE_SOURCE_ROLE`）。
    BUILTIN_SOURCE_ROLE = "客服"
    #: 期望的账号 / Group / 角色名（独立字面量）。
    EXPECTED_USERNAME = "gamescope001"
    EXPECTED_GROUP = "客服单游戏"
    EXPECTED_ROLE_NAME = "客服单游戏"
    #: 第 1 / 第 2 款游戏名（独立字面量）。
    FIRST_GAME = "E2E 测试游戏"
    SECOND_GAME = "E2E 测试游戏乙"

    def setUp(self):
        super().setUp()
        # 目录 + 内置角色先落地：自定义角色调 `builtin_role_nodes("客服")` 取节点，
        # 而那条判据的右侧（内置「客服」角色）本身由 `seed_builtin_roles()` 建出来。
        seed_rbac()

    def test_role_nodes_equal_the_builtin_service_role(self):
        """**G1 前半**：节点集与**真库里**内置「客服」那一档**集合相等**。

        ⚠️ **这是一条同源一致性检查，⛔ 不是跨来源对拍**：两侧都经
        `apps/rbac/seed.py::builtin_role_nodes("客服")`——
        - 左：`_seed_tenant_fixtures` 用它给本角色 `set()` 下去的那些；
        - 右：`seed_builtin_roles()` 授权内置「客服」时写下的那些（**同一个函数**）。
        ⇒ 本质是 `f(x) == f(x)`。**它挡得住**：档位被换（`GAMESCOPE_NODE_SOURCE_ROLE`
        改成代练员那一档）、两条写路径分叉（本角色不再走同一个出处）。
        **它挡不住**：`builtin_role_nodes` 自己或「客服」那一档的栏目清单被改坏——
        那时两侧一起变、相等照样成立。⇒ 本用例末尾另补了**独立字面量**断言
        （节点数 66 + 关键 code 子集），那条才钉得住外部契约值。

        ⚠️ 再加一条**非空**断言：两侧都空时"集合相等"会退化成"空 = 空"而恒真——
        那正是 `_seed_gamescope()` 里"空集 ⇒ 响亮失败"要防的形态，这里从判据侧再钉一次。
        """
        _seed_tenant_fixtures(self.tenant)
        role = Role.objects.filter(name=self.EXPECTED_ROLE_NAME).first()
        self.assertIsNotNone(role, f"单游戏范围角色「{self.EXPECTED_ROLE_NAME}」没被种出来")
        self.assertEqual(
            role.group.name,
            self.EXPECTED_GROUP,
            "Role.group 必须与账号挂的 Group 同名（`Role.group` 是 OneToOne，见 models.py）",
        )
        actual = set(role.permissions.values_list("code", flat=True))
        builtin = Role.objects.filter(name=self.BUILTIN_SOURCE_ROLE).first()
        self.assertIsNotNone(
            builtin, f"内置「{self.BUILTIN_SOURCE_ROLE}」角色没被 seed_rbac 建出来 ⇒ 判据右侧不存在"
        )
        expected = set(builtin.permissions.values_list("code", flat=True))
        self.assertTrue(
            expected,
            f"内置「{self.BUILTIN_SOURCE_ROLE}」的角色集是空的 ⇒ 这条相等判据会退化成「空 = 空」",
        )
        self.assertEqual(
            actual,
            expected,
            f"单游戏范围角色的节点集与内置「{self.BUILTIN_SOURCE_ROLE}」不一致"
            f"（差集：多={sorted(actual - expected)} / 少={sorted(expected - actual)}）",
        )
        #: **独立字面量**（⛔ 不引 `builtin_role_nodes`、⛔ 不引 `seed_e2e` 常量）——
        #: 上面那条相等判据两侧同源（见 docstring），挡不住"函数或客服那一档一起被改坏"。
        #: 这两条钉的是**外部契约值**：客服那一档今天**恰好 66 个**节点（自有节点入目录
        #: 时模块批发跟着涨——`home.qr_manage`（D-0262）与 `order.platform_write` 即两例；
        #: ⛔ 数字变了不是被改坏，先核对有没有新节点），且必须含下列代表性子集
        #: （跨模块挑：栏目根 `order`、状态机动作 `order.change_status*`、另一栏目 `my_orders`
        #: 与 `grab_hall`；`order.platform_write` = fix5 起客服档必须拿到的涉钱写门）——
        #: 档位被换成代练员（25 个）时，长度与子集**各自**都会红。
        self.assertEqual(
            len(actual),
            66,
            f"内置「客服」那一档的节点数不等于本断言的独立字面量（实际 {len(actual)}）——"
            "要么节点源档位被换、要么 `builtin_role_nodes` / 栏目清单被改坏；"
            "⛔ 改这条断言前先确认这是有意的契约变更，不是被改坏",
        )
        for code in (
            "order",
            "order.change_status",
            "order.change_status_completed",
            "order.list",
            "order.platform_write",
            "home.approval_apply",
            "my_orders",
            "grab_hall",
        ):
            with self.subTest(code=code):
                self.assertIn(
                    code,
                    actual,
                    f"客服那一档必须含 {code}（独立字面量子集断言）——档位被换或栏目清单被改坏",
                )

    def test_account_has_exactly_one_game_the_first(self):
        """**G1 后半**（spec §2）：`games` **恰好 1 款、且是第 1 款**（⛔ 不含第 2 款）。

        ⚠️ 判据是「长度 == 1 **且** 名字 == 第 1 款」两半：只判长度会放过"挂的是第 2 款"，
        只判名字会放过"两款都挂了"（后者正是把 `all_games` 误传成 `True` 的形态）。
        """
        _seed_tenant_fixtures(self.tenant)
        profile = StaffProfile.objects.filter(user__username=self.EXPECTED_USERNAME).first()
        self.assertIsNotNone(profile, f"{self.EXPECTED_USERNAME} 应当带 StaffProfile")
        games = list(profile.games.values_list("name", flat=True))
        self.assertEqual(
            len(games), 1, f"{self.EXPECTED_USERNAME} 应当**恰好**挂 1 款游戏，实际 {games}"
        )
        self.assertEqual(games, [self.FIRST_GAME], "挂的那一款必须是第 1 款")
        self.assertFalse(
            profile.games.filter(name=self.SECOND_GAME).exists(),
            "第 2 款游戏被挂上了 ⇒ 这条权限轴（只看得到 1 款）就不是它了",
        )

    def test_account_hangs_on_the_custom_group(self):
        """账号恰好在自定义角色的 Group 上（多一个组 ⇒ 节点判据读到的并集会变）。"""
        _seed_tenant_fixtures(self.tenant)
        user = get_user_model().objects.get(username=self.EXPECTED_USERNAME)
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)),
            {self.EXPECTED_GROUP},
            f"{self.EXPECTED_USERNAME} 必须恰好挂在自定义角色的同名 Group 上",
        )

    def test_second_game_is_seeded_idempotently(self):
        """**G3 前半**：第 2 款游戏**真的建出来了**，且重跑**不产生第二行**（幂等）。"""
        _seed_tenant_fixtures(self.tenant)
        _seed_tenant_fixtures(self.tenant)
        self.assertEqual(
            Game.objects.filter(name=self.SECOND_GAME).count(),
            1,
            "第 2 款游戏没建出来，或重跑产生了重复行（`get_or_create` 的幂等被破坏）",
        )
        self.assertEqual(
            set(Game.objects.values_list("name", flat=True)),
            {self.FIRST_GAME, self.SECOND_GAME},
            "E2E 租户的游戏集不是「第 1 款 + 第 2 款」这一对",
        )

    def test_existing_profile_accounts_game_scopes_are_as_designed(self):
        """**G3 后半**：既有带档案账号的游戏集**按设计**——没有被"多了一款游戏"弄坏。

        设计（单一出处 = `_seed_tenant_fixtures` 里各账号 `upsert(...)` 的 **`all_games` 实参**，
        本判据**独立字面量**枚举，⛔ 不读常量、⛔ 不读实际值反推）：
        - `all_games=False`（`cs001` / `taker001`）⇒ **只挂第 1 款**（1 款）；
        - `all_games=True`（`supervisor001` / `dev001` / `minrole001`）⇒ **租户全集**（本线起 = 2 款）。

        ⚠️ 这正是母规范 §11 那条残差要求的「**能分辨『账号游戏集 vs 租户全集』**的判据」：
        在本线之前租户只有 1 款，两类账号在库里是同一种状态、判据**不可分辨**。
        """
        _seed_tenant_fixtures(self.tenant)
        #: `账号 → 设计上应当挂的游戏集`（独立字面量；`all_games=False` 那一支只拿第 1 款）。
        designed = {
            "cs001": (self.FIRST_GAME,),
            "taker001": (self.FIRST_GAME,),
            "supervisor001": (self.FIRST_GAME, self.SECOND_GAME),
            "dev001": (self.FIRST_GAME, self.SECOND_GAME),
            "minrole001": (self.FIRST_GAME, self.SECOND_GAME),
            self.EXPECTED_USERNAME: (self.FIRST_GAME,),
        }
        for username, expected in designed.items():
            with self.subTest(username=username):
                profile = StaffProfile.objects.filter(user__username=username).first()
                self.assertIsNotNone(profile, f"{username} 应当带 StaffProfile")
                actual = set(profile.games.values_list("name", flat=True))
                self.assertEqual(
                    actual,
                    set(expected),
                    f"{username} 的游戏集与设计不符（设计={sorted(expected)}；实际={sorted(actual)}）",
                )

    def test_reseeding_is_idempotent_for_the_gamescope_role(self):
        """重跑结果确定（spec §2 的幂等要求）：角色仍只有一条、节点集 / 账号游戏集原样。"""
        _seed_tenant_fixtures(self.tenant)
        before = set(
            Role.objects.get(name=self.EXPECTED_ROLE_NAME).permissions.values_list("code", flat=True)
        )
        _seed_tenant_fixtures(self.tenant)
        roles = Role.objects.filter(name=self.EXPECTED_ROLE_NAME)
        self.assertEqual(roles.count(), 1, "重跑后出现了第二条同名单游戏范围角色")
        self.assertEqual(
            set(roles.get().permissions.values_list("code", flat=True)),
            before,
            "重跑把节点集改了（本角色的口径是每次重设成当前那一档，不该漂）",
        )
        self.assertEqual(
            set(
                StaffProfile.objects.get(user__username=self.EXPECTED_USERNAME)
                .games.values_list("name", flat=True)
            ),
            {self.FIRST_GAME},
            "重跑把账号的游戏集改了",
        )


class ProfileAccountUsernamesParityTests(SimpleTestCase):
    """`seed_e2e.PROFILE_ACCOUNT_USERNAMES` 与测试里**独立写死**的那份必须**集合相等**。

    **为什么补它**（评审点名的缺口）：本文件上面两处注释都声称与 `PROFILE_ACCOUNT_USERNAMES`
    「逐字一致」，却**没有任何断言** ⇒ 从真常量里**删掉**一个名字时本文件照样全绿
    （「加名字」会被别的用例碰红，**删名字**是**静默少覆盖**：守卫不再检查那个账号，
    审计于是在「页面因缺游戏权限而空」上把假缺陷读成没问题）。

    ⛔ 两条不许（评审原话）：
    - **不许只断长度**（`len(真) == len(测试里那份)`）——换掉一个名字长度不变，照样漏覆盖；
    - **不许改成"读真常量当期望值"、把测试里那份删掉**——那就是自指断言（`E-0024`），
      常量被改坏时用例不会红。**两份都要在**，判据是"两份相等"。

    ⚠️ 两份测试拷贝都断：`SeededAccountsUsableGuardTests` 与 `SeedE2EOnRealTenantTests`
    各独立写死了一份 `SHOULD_HAVE_PROFILE`，漏掉任一处都会留下同一类静默缺口。
    """

    def test_matches_every_hardcoded_copy(self):
        real = set(PROFILE_ACCOUNT_USERNAMES)
        copies = {
            "SeededAccountsUsableGuardTests.SHOULD_HAVE_PROFILE": (
                SeededAccountsUsableGuardTests.SHOULD_HAVE_PROFILE
            ),
            "SeedE2EOnRealTenantTests.SHOULD_HAVE_PROFILE": (
                SeedE2EOnRealTenantTests.SHOULD_HAVE_PROFILE
            ),
        }
        for label, copy in copies.items():
            with self.subTest(copy=label):
                self.assertEqual(
                    real,
                    set(copy),
                    f"{label} 与 seed_e2e.PROFILE_ACCOUNT_USERNAMES 不相等"
                    f"（真常量={sorted(real)}；测试里那份={sorted(copy)}）——"
                    "差集意味着某个账号只有一侧在检查，另一边是**静默少覆盖**",
                )
