"""按游戏权限收窄可见范围的**规则本体**（业务方 2026-09-23 裁定）。

**本文件钉的是口径本身，不是某一个端点**：`apps.business.game_scope` 的三个导出
（`games_of` / `game_scope` / `assert_game_allowed`）与 `/api/auth/me` 的 `game_ids`
载荷；后续订单/绩效/结算/统计四条线的接线都消费同一份规则。

⚠️ **期望值一律写字面值**（E-0024）：断言与被断言的东西不能是同一个符号。
特别是「系统管理员」这个组名——这里写字面 `"系统管理员"` 而不是导入
`apps.business.roles.TENANT_ADMIN`；导入常量的话，常量被改错时断言跟着改，
判别力就没了。

⚠️ **「空游戏列表 ⇒ 什么都看不到」是本文件最承重的一条**（业务方选的**严格**口径）：
`__in=[]` 在 Django 里是**空结果**，不是「不过滤」。所以每条过滤用例都要有一个
「空游戏表的人一张都看不到」的反面，光写「能看到自己的那一款」证明不了口径。
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.game_scope import assert_game_allowed, game_scope, games_of
from apps.business.models import Game, Order
from apps.business.validation import ValidationProblem
from apps.core.scoping import scope_queryset
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()


def _profile(user, *games):
    """给账号建档案并勾游戏。`games=()` 即「有档案但一款没勾」（存量里存在这种行）。"""
    profile = StaffProfile.objects.create(user=user)
    if games:
        profile.games.set(games)
    return profile


def _order(game, order_no):
    """只有 `game` 与 `order_no` 是必填的（`validate_required`：游戏 + 项目文本在
    `project_text` 上；测试不关心项目，给个占位文本）。"""
    return Order.objects.create(game=game, order_no=order_no, project_text="占位项目")


class GameScopeTests(TenantTestCase):
    """①–④：规则本体的字面口径与反面。"""

    def setUp(self):
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="崩铁")
        self.global_game = Game.objects.create(name="不勾给任何人的游戏")

    # ── ① games_of ────────────────────────────────────────────────────
    def test_games_of_returns_the_two_granted_games(self):
        user = User.objects.create_user(username="two-games", password="pw-123456")
        _profile(user, self.game_a, self.game_b)
        self.assertEqual(
            sorted(games_of(user).values_list("pk", flat=True)),
            sorted([self.game_a.pk, self.game_b.pk]),
        )

    def test_games_of_is_empty_without_a_profile(self):
        user = User.objects.create_user(username="no-profile", password="pw-123456")
        self.assertEqual(StaffProfile.objects.filter(user=user).count(), 0)  # 构造物
        self.assertEqual(list(games_of(user)), [])

    def test_games_of_is_empty_when_no_game_is_checked(self):
        user = User.objects.create_user(username="zero-games", password="pw-123456")
        _profile(user)
        self.assertEqual(StaffProfile.objects.filter(user=user).exists(), True)  # 构造物
        self.assertEqual(list(games_of(user)), [])

    # ── ② game_scope("game_id")：过滤 + 「空游戏 ⇒ 零行」的钉子 ──────────
    def test_game_scope_keeps_only_orders_of_my_games(self):
        user = User.objects.create_user(username="one-game", password="pw-123456")
        _profile(user, self.game_a)
        _order(self.game_a, "20260923000000000001")
        _order(self.game_a, "20260923000000000002")
        _order(self.game_b, "20260923000000000003")

        visible = scope_queryset(user, Order.objects.all(), scope_filter=game_scope("game_id"))
        self.assertEqual(
            sorted(visible.values_list("order_no", flat=True)),
            ["20260923000000000001", "20260923000000000002"],
        )
        # 反面：不在自己名下的那张单**确实**不在结果里（不是"没造出来"）。
        self.assertEqual(list(visible.filter(order_no="20260923000000000003")), [])

    def test_game_scope_hides_everything_from_a_user_with_no_games(self):
        """严格口径的钉子：空游戏列表 ⇒ 一张都看不到。"""
        user = User.objects.create_user(username="zero-scope", password="pw-123456")
        _profile(user)
        for i, game in enumerate((self.game_a, self.game_b, self.global_game), start=1):
            _order(game, f"2026092300000000001{i}")
        self.assertEqual(Order.objects.count(), 3)  # 构造物：库里确实有 3 张

        visible = scope_queryset(user, Order.objects.all(), scope_filter=game_scope("game_id"))
        self.assertEqual(list(visible), [])

    # ── ③ assert_game_allowed ─────────────────────────────────────────
    def test_assert_game_allowed_accepts_every_game_i_hold(self):
        """**两只**游戏都要放行，不是一只。

        ⚠️ 这条的判别力来自「多于一」：只勾**一款**游戏的账号在被取反的判据下
        （`exclude(pk=…)` 为空 ⇒ 不抛）会**碰巧通过**，于是取反变异活下来
        （实测：本文件第一版就是那样，被变异②抓到）。
        一个「通过」的用例只有在「去掉它哪条会红」问得出答案时才算钉子（E-0065 同族）。
        """
        user = User.objects.create_user(username="allowed", password="pw-123456")
        _profile(user, self.game_a, self.game_b)
        assert_game_allowed(user, self.game_a)  # 不抛即通过
        assert_game_allowed(user, self.game_b)  # 第二只同样不抛

    def test_assert_game_allowed_rejects_someone_elses_game(self):
        user = User.objects.create_user(username="denied", password="pw-123456")
        _profile(user, self.game_a)
        self.assertEqual(
            list(games_of(user).values_list("pk", flat=True)), [self.game_a.pk]
        )  # 构造物：本账号只有这一只
        with self.assertRaises(ValidationProblem) as ctx:
            assert_game_allowed(user, self.game_b)
        self.assertEqual(ctx.exception.code, 1000)  # 字面业务码（E-0024）

    def test_assert_game_allowed_does_not_reject_none(self):
        user = User.objects.create_user(username="none-game", password="pw-123456")
        _profile(user)
        assert_game_allowed(user, None)  # 「游戏必填」由既有校验管，这里不抛

    # ── ④ 管理员旁路：短路在 `scope_queryset` 这个卡点里，不在过滤器里 ────
    def test_admin_bypasses_the_filter_through_the_chokepoint(self):
        admin = User.objects.create_user(
            username="scope-admin", password="pw-123456", is_superuser=True
        )
        # 「租户管理员」 = 持「系统管理员」职位（字面组名，E-0024）。
        from django.contrib.auth.models import Group

        admin.groups.add(Group.objects.create(name="系统管理员"))
        self.assertIs(admin.is_superuser, True)
        _order(self.game_a, "20260923000000000021")
        _order(self.game_b, "20260923000000000022")

        visible = scope_queryset(admin, Order.objects.all(), scope_filter=game_scope("game_id"))
        self.assertEqual(
            sorted(visible.values_list("order_no", flat=True)),
            ["20260923000000000021", "20260923000000000022"],
        )


class MeGameIdsTests(TenantTestCase):
    """⑤ `/api/auth/me` 的 `game_ids`（升序；没有档案 ⇒ `[]`，**管理员除外**）。

    ⚠️ **管理员那一半是收口评审 A-1 的新钉子**：`games_of` 原先**没有**管理员短路，
    而供给出来的租户 `admin` **按构造没有 `StaffProfile`** ⇒ `game_ids == []` ⇒ 前端
    `fetchGames()`（全量 ∩ `game_ids`）拿到空下拉 ⇒ **管理员从前端录不了单**。
    短路与 `scope_queryset` / `assert_game_allowed` 用的是**同一个**
    `apps.core.permissions.is_tenant_admin`（判据只有一处，不另写第二份）。
    """

    def test_game_ids_are_sorted_ascending(self):
        # ⚠️ `games=None`（夹具默认翻面后的显式 opt-out）：本用例紧接着自己用
        # `_profile(user, late, early)` 建档案 → 夹具这一份必须**不建**，否则撞唯一约束。
        client = make_logged_in_client(
            username="me-games", password="pw-123456", games=None
        )
        user = User.objects.get(username="me-games")
        # 先建 pk 大的、后建 pk 小的：升序断言才有判别力（否则建库顺序碰巧就是升序）。
        late = Game.objects.create(name="后建的游戏")
        early = Game.objects.create(name="先建的游戏")
        _profile(user, late, early)

        payload = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(payload["game_ids"], sorted([early.pk, late.pk]))

    def test_game_ids_are_every_game_for_a_tenant_admin_without_a_profile(self):
        """① 无档案的**租户管理员** ⇒ `game_ids` 恰好是全部游戏（升序字面列表）。

        判别力：删掉 `games_of` 的管理员短路 ⇒ 该账号没有档案 ⇒ 短路后面的分支回
        `Game.objects.none()` ⇒ `game_ids == []` ⇒ 本用例红。
        """
        # ⚠️ `games=None` 是**必须的**（夹具默认翻面后的显式 opt-out）：
        # 本用例的**承重前提就是「这个管理员没有档案」**（下面那条 count()==0 的构造物
        # 断言直接依赖它）。夹具新默认会建一份全量档案 ⇒ 本用例变红、判别力归零。
        client = make_logged_in_client(
            username="me-admin-no-profile", password="pw-123456",
            is_superuser=True, games=None,
        )
        admin = User.objects.get(username="me-admin-no-profile")
        # 构造物：这个管理员**真的没有档案**（正是供给出来的 `admin` 的形状）——
        # 有档案的话本用例会因为别的原因通过，判别力就没了。
        self.assertEqual(StaffProfile.objects.filter(user=admin).count(), 0)
        # 先建 pk 大的、后建 pk 小的：升序断言才有判别力。
        late = Game.objects.create(name="管理员后建的游戏")
        early = Game.objects.create(name="管理员先建的游戏")

        payload = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(payload["is_tenant_admin"], True)  # 构造物：短路的前提真的成立
        # ⛔ 期望值是**字面 pk 列表**（E-0024）：不写 `Game.objects.values_list(...)`
        # ——那是被断言的那条查询本身，自指断言不会红。
        self.assertEqual(payload["game_ids"], sorted([early.pk, late.pk]))

    def test_game_ids_are_empty_without_a_profile(self):
        """② 反例：无档案的**非**管理员 ⇒ `game_ids == []`（豁免只给管理员）。

        ⚠️ `games=None` 是**必须的**（夹具默认翻面后的显式 opt-out）：
        本用例断的就是「**没有档案** ⇒ `game_ids == []`」——夹具新默认会建一份全量档案
        ⇒ `game_ids` 变成全部游戏 ⇒ 本用例变红，而且这条钉子会恒真失效。

        ⚠️ **租户里必须真有游戏（收口评审 A-1 补的构造物自证）**：游戏数为 0 时
        `game_ids == []` 对**任何**实现都成立 ⇒ 这条断言的判别力是**零**（评审实测：
        生产侧「没有档案 ⇒ `Game.objects.none()`」改成「⇒ `Game.objects.all()`」、夹具侧
        `games is None` 分支改成「建档案 + 勾全部游戏」，两条变异本条**都活下来**）。
        所以下面先在**本用例内部**建一只游戏——而且必须建在**夹具调用之前**：
        夹具的 `_apply_games` 在那一刻才读 `Game.objects.all()`，游戏若在夹具之后才建，
        「夹具侧那条变异」仍然杀不掉（那时库里还是 0 只）。建完之后租户里就有游戏了，
        「实现坏了会返回非空」这条推理才成立。
        放**用例内部**而不是类级 `setUp` 是必须的：同类里
        `test_game_ids_are_sorted_ascending` 与管理员那条断言的是
        `game_ids == sorted([early.pk, late.pk])` 的**字面集合**，类级游戏会让它们多出一只、
        把字面期望打破（那两条会红，且不是它们要测的东西）。
        """
        # 构造物自证：建之前租户里一张游戏都没有、建之后**恰好一只**（「空」这个断言
        # 才有判别力；两个数字都是字面值，不是 `Game.objects.count()` 自指）。
        self.assertEqual(Game.objects.count(), 0)
        game = Game.objects.create(name="租户里存在的一只游戏")
        self.assertEqual(Game.objects.count(), 1)
        # 构造物：这只游戏真的在库里（后面「实现坏了会返回非空」的判据就是它）。
        self.assertIsNotNone(Game.objects.filter(pk=game.pk).first())

        client = make_logged_in_client(
            username="me-no-profile", password="pw-123456", games=None
        )
        user = User.objects.get(username="me-no-profile")
        # 构造物：确认这真是非管理员——用**独立**的字段判（不 import `is_tenant_admin`，
        # 否则「判据坏了」与「夹具造错了」不可区分）。
        self.assertEqual(user.is_superuser, False)
        self.assertEqual(user.groups.count(), 0)

        payload = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(payload["game_ids"], [])


class MeGameIdsOnPublicPlaneTests(TenantTestCase):
    """⑤ 续：`/api/auth/me` 在 **public（平台）平面**也要能跑、**不抛**。

    ⚠️ **这条是实测踩出来的**：`StaffProfile` 属 `TENANT_APPS` ⇒
    `accounts_staffprofile` 只在租户 schema 里。平台平面上访问反向描述符会去查一张
    **不存在的表**，抛的是 `ProgrammingError`（不是 `AttributeError` 子类，
    `getattr` 的默认值兜不住）⇒ 不早返回会让运营后台的 `/api/auth/me` 直接 500。
    平台账号在本模块口径里没有游戏权限 ⇒ `game_ids == []`。

    会话走 `materialize_session` 而不是真实登录：平台平面的登录端点可用，但这里要的
    只是「一个**已认证的 public 平面请求**」；构造方式与 P5 红线测试同源
    （`tests/test_p5_credential_plane.py`），不自造第二套。
    """

    #: 平台域名。与 `apps/platform/bootstrap.py::PLATFORM_DOMAIN` 同值，这里**独立抄写**：
    #: 断言的东西不引用被断言的东西（E-0024 同族）。
    PLATFORM_DOMAIN = "admin.test.local"

    def setUp(self):
        super().setUp()
        from django.conf import settings

        from apps.platform.bootstrap import ensure_public_tenant

        # ⚠️ 放 `setUp`（测试事务内）而不是 `setUpClass`：`test_provisioning.py` 断言的
        # 正是「public tenant 行不存在」，放类级会把那条打红（P5 红线测试记了这条）。
        ensure_public_tenant(self.PLATFORM_DOMAIN)
        if self.PLATFORM_DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.append(self.PLATFORM_DOMAIN)

    def test_game_ids_is_empty_and_does_not_raise_on_the_public_plane(self):
        from django.test import Client
        from django_tenants.utils import schema_context

        from tests.test_p5_credential_plane import materialize_session

        with schema_context("public"):
            user = User.objects.create_superuser(username="platform-me", password="pw-123456")
            pk, password_hash = user.pk, user.password

        session_id = materialize_session(
            "public", user_pk=pk, password_hash=password_hash, plane="public"
        )
        client = Client(SERVER_NAME=self.PLATFORM_DOMAIN)
        client.cookies["sessionid"] = session_id

        response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = response.json()["data"]["user"]
        self.assertEqual(payload["username"], "platform-me")  # 构造物：真是那个平台账号
        self.assertEqual(payload["game_ids"], [])


class ReportAccountsWithoutGamesTests(TenantTestCase):
    """巡检命令：只读 + 两个计数（字面数字）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant

        # 额外租户只写 `cls.tenant_b`：`cls.tenant` / `cls.domain` 是基类 `tearDownClass`
        # 用来删自己那个共享租户的（E-0015）。
        cls.tenant_b, _ = provision_tenant(
            schema_name="scope_report_b", name="巡检 B", domain="scope-report-b.test.local"
        )

    def _fixtures(self):
        """两个租户各造「有档案但零游戏」与「没有档案」各一个活跃账号。"""
        game = Game.objects.create(name="有权限的游戏")
        with_profile = User.objects.create_user(username="zero-games-acc", password="pw-123456")
        _profile(with_profile)  # 有档案、一款没勾
        User.objects.create_user(username="no-profile-acc", password="pw-123456")
        # 一个对照组：有档案且勾了游戏 ⇒ **不该**被数进任何一类。
        healthy = User.objects.create_user(username="healthy-acc", password="pw-123456")
        _profile(healthy, game)
        # 一个停用账号 ⇒ **不该**被数（口径是活跃账号）。
        disabled = User.objects.create_user(
            username="disabled-acc", password="pw-123456", is_active=False
        )
        _profile(disabled)

    def test_counts_are_reported_per_tenant_with_literal_numbers(self):
        self._fixtures()
        out = StringIO()
        call_command("report_accounts_without_games", "--schema", self.tenant.schema_name, stdout=out)
        text = out.getvalue()
        self.assertIn("有档案但零游戏: 1", text)
        self.assertIn("没有档案: 1", text)
        self.assertIn("zero-games-acc", text)
        self.assertIn("no-profile-acc", text)
        self.assertNotIn("disabled-acc", text)
        self.assertNotIn("healthy-acc", text)

    def test_iterates_every_tenant_by_default(self):
        self._fixtures()
        out = StringIO()
        call_command("report_accounts_without_games", stdout=out)
        text = out.getvalue()
        self.assertIn(self.tenant.schema_name, text)
        self.assertIn(self.tenant_b.schema_name, text)

    def test_command_is_read_only(self):
        """跑前跑后，库里每一行都**逐字节**不变。

        判据是「所有行的字段值 —— 含 M2M 关联 —— 完全一致」，不是"行数一样"：
        一个 `profile.games.clear()` 不改行数却会改内容。
        """
        self._fixtures()

        def snapshot():
            return (
                sorted(User.objects.values_list("username", "is_active", "last_login", "password")),
                sorted(StaffProfile.objects.values_list("pk", "user_id")),
                sorted(StaffProfile.games.through.objects.values_list("staffprofile_id", "game_id")),
                sorted(Game.objects.values_list("pk", "name", "deleted_at")),
            )

        before = snapshot()
        call_command("report_accounts_without_games", stdout=StringIO())
        self.assertEqual(snapshot(), before)

    def test_context_does_not_leak_after_the_command(self):
        """命令不许把连接留在别人的 schema 上（AGENTS.md §4 的作用域纪律）。"""
        from django.db import connection

        before = connection.schema_name
        call_command("report_accounts_without_games", stdout=StringIO())
        self.assertEqual(connection.schema_name, before)
