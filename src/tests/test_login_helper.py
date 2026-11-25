"""Task 3 Step 0：登录夹具的 `groups` / `is_superuser` 参数。

⚠️ 这两个参数是给 P3a 的**带角色**测试用的；`force_login` 在本项目**不可用**
（会话没有 `identity_plane`，会被 fail-closed 拒掉）。
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context

from apps.accounts.models import StaffProfile
from apps.business.models import Game
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()


class LoginHelperParamsTests(TenantTestCase):
    def test_groups_are_attached_and_visible(self):
        client = make_logged_in_client(username="cs_with_group", groups=("客服",))
        me = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(me["roles"], ["客服"])

    def test_default_does_not_grant_any_group(self):
        """不传 groups 时**不能**凭空给角色——否则既有调用方的语义被悄悄改了。"""
        client = make_logged_in_client(username="plain_user")
        me = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(me["roles"], [])

    def test_superuser_flag(self):
        client = make_logged_in_client(username="boss", is_superuser=True)
        me = client.get("/api/auth/me").json()["data"]["user"]
        self.assertTrue(me["is_tenant_admin"])
        # ⚠️ payload 里**没有** `is_superuser` 字段（已核实 `auth_views._user_payload`
        #    只给 `is_tenant_admin`）——**不要**写「字段存在才断言」那种恒真断言。


class LoginHelperGamesTests(TenantTestCase):
    """`make_logged_in_client(..., games=…)` 的**四条字面语义**（2026-09-23 业务方裁定）。

    这四条是**本夹具与所有调用方之间的契约**；下面每条都用**字面值**断言
    （E-0024：不拿 `Game.objects.all()` / `games_of(user)` 当期望值——那是被断言的
    那条查询本身，自指断言不会红）。
    """

    def setUp(self):
        super().setUp()
        # 三只游戏的 pk 一定递增（同一个 schema 的自增序列），但**名字顺序故意打乱**：
        # 断言按 pk 排序 ⇒ 若实现返回名字序 / 建库序，判别力还在。
        self.game_b = Game.objects.create(name="按 pk 排第二")
        self.game_c = Game.objects.create(name="按 pk 排第三")
        self.game_a = Game.objects.create(name="按 pk 排第一")

    def test_default_grants_exactly_every_game_of_this_tenant(self):
        """① 不传 `games` ⇒ 当前租户的**全部游戏**（字面 pk 集合）。

        判别力：夹具默认还是「不给游戏」时，`profile.games` 为空 ⇒ 本用例红
        （改动前的实测红原文见任务报告）。
        """
        make_logged_in_client(username="games-default")
        user = User.objects.get(username="games-default")
        profile = StaffProfile.objects.get(user=user)  # 没有档案会 DoesNotExist ⇒ 红
        # 字面期望集合：按 pk 升序的**三只** pk 列表（先建 b、c，后建 a）。
        self.assertEqual(
            list(profile.games.order_by("pk").values_list("pk", flat=True)),
            sorted([self.game_a.pk, self.game_b.pk, self.game_c.pk]),
        )
        self.assertEqual(profile.games.count(), 3)  # 字面 3，不是「有就行」

    def test_games_none_leaves_the_account_without_a_profile(self):
        """② `games=None` ⇒ **不建档案**（「没有档案」那条形状的字面 `0`）。"""
        make_logged_in_client(username="games-none", games=None)
        user = User.objects.get(username="games-none")
        self.assertEqual(StaffProfile.objects.filter(user=user).count(), 0)

    def test_games_empty_builds_a_profile_with_zero_games(self):
        """③ `games=[]` ⇒ 档案**在**、游戏**一款都不勾**（字面 `0`）。"""
        make_logged_in_client(username="games-empty", games=[])
        user = User.objects.get(username="games-empty")
        profile = StaffProfile.objects.get(user=user)  # 档案必须在
        self.assertEqual(profile.games.count(), 0)

    def test_games_list_grants_exactly_those_games(self):
        """④ `games=[a]` ⇒ **恰好**那一只（字面 pk），不多不少。"""
        make_logged_in_client(username="games-one", games=[self.game_a])
        user = User.objects.get(username="games-one")
        profile = StaffProfile.objects.get(user=user)
        self.assertEqual(
            list(profile.games.order_by("pk").values_list("pk", flat=True)),
            [self.game_a.pk],
        )


class LoginHelperGamesOnPublicPlaneTests(TenantTestCase):
    """⑤ 默认值在**平台平面（public）**上不许建档案——那个 schema 里没有
    `accounts_staffprofile` 表，写它会 `ProgrammingError`（本仓前面踩过一次）。

    构造方式与 P5 红线测试同源：`ensure_public_tenant` + **在 `schema_context("public")`
    里调夹具**（账号由夹具当场建在 public，且**取平台超管形状**：真实平台账号由
    `createsuperuser` 建在 public，见 `docs/RUNBOOK.md` —— 收口评审 C-1）+ 真实
    `POST /api/auth/login`（平台域名）；平台账号在本项目口径里没有游戏权限
    （守卫 `on_public_plane()` 不看 superuser，形状对齐不改变那条覆盖）。
    """

    #: 与 `apps/platform/bootstrap.py::PLATFORM_DOMAIN` 同值，**独立抄写**
    #: （E-0024 同族：不引用被断言的东西）。
    PLATFORM_DOMAIN = "admin.test.local"

    def setUp(self):
        super().setUp()
        from apps.platform.bootstrap import ensure_public_tenant

        # 平台平面必须先有 Host（`TenantMainMiddleware` 查不到 Domain 直接 404）。
        # 放 `setUp`（测试事务内）而不是 `setUpClass`：后者会被提交并活过整个进程，
        # 打红 `test_provisioning.py` 里「public 租户行不存在」的断言。
        ensure_public_tenant(self.PLATFORM_DOMAIN)
        if self.PLATFORM_DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.append(self.PLATFORM_DOMAIN)
        # ⚠️ **账号由夹具在 `schema_context("public")` 里当场建**，
        #    `setUp` 不再预建：夹具建的账号必须与 `_apply_games` 落在**同一个** schema，
        #    否则「夹具在平台平面上跳过档案」这条判据测不到（见下面用例的注释）。

    def tearDown(self):
        # ⚠️ 跨域名请求**不会**在响应结束时还原 schema（`TenantMainMiddleware` 只在请求
        # 进来时 `set_tenant`）。不钉回来，上一条用例残留的 `public` 会跟着进下一条。
        from django.db import connection

        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_default_does_not_build_a_profile_and_does_not_raise_on_the_public_plane(self):
        # 默认（不传 `games`）⇒ 只能走平台守卫，不许碰档案表。
        # ⚠️ 夹具调用**必须包在 `schema_context("public")` 里**：
        #    夹具建账号与 `_apply_games` 用的都是**当前连接**的 schema
        #    （守卫 `on_public_plane()` 读的正是它），而 `TenantTestCase` 的连接默认钉在
        #    **租户** schema 上 ⇒ 不切 schema 时夹具压根不在平台平面跑，那道守卫
                #    **从未被进入**（实测：把守卫的 `return` 改成 `pass`，全套仍绿——
        #    「平台平面不炸」的 DoD 因此不成立）。切进 public 之后，夹具若在平台平面上
        #    建档案，这里就会直接抛
        #    `ProgrammingError: relation "accounts_staffprofile" does not exist` ⇒ 本用例红。
        with schema_context("public"):
            client = make_logged_in_client(
                domain=self.PLATFORM_DOMAIN,
                username="public-helper-user",
                password="pw-public-helper",
                # 平台账号的**真实形状**是超管（`createsuperuser` 建在 public）——
                # 守卫 `on_public_plane()` 不看 superuser，覆盖不变。
                is_superuser=True,
            )
        # 登录确实成功、且真在平台平面（否则上面的"不抛"可能测的是别的平面）。
        me = client.get("/api/auth/me").json()["data"]["user"]
        self.assertEqual(me["username"], "public-helper-user")
        self.assertEqual(me["identity_plane"], "public")

        # 构造前提自证：平台平面**按构造没有** `accounts_staffprofile` 表 ⇒ 「建档案会炸」
        # 不是推测。⚠️ 这条断言故意放在**最后**：PostgreSQL 的失败语句会把当前事务标成
        # aborted，之后任何查询都只会得到 `InFailedSqlTransaction`（实测踩到，第一版
        # 就是把这个 count 放在它后面才 Error 的）。
        from django.db.utils import ProgrammingError

        with schema_context("public"):
            with self.assertRaises(ProgrammingError):
                StaffProfile.objects.count()
