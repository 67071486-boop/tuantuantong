"""`is_account_admin` 与 `is_tenant_admin` 的分工。

租户平面上两者同真同假：账号管理员直接委托租户管理员，判据都是「系统管理员」这个 Group。
分叉只在平台平面。
`is_tenant_admin` 对超管为真、`is_account_admin` 恒为假（R14）。
所以本文件改成钉这条**现存的**分叉 + 两个判据在租户平面的同向行为。

## ⭐ 线 D：账号端点准入放宽到 `is_account_manager` / `IsAccountManager`

本文件下半部分（`AccountManagerPredicateTests` / `AccountManagementNodesStructuralNetTests`
与 `AccountAdminPredicateOnThePublicPlaneTests` 里新增的那条）钉的是线 D 的判据：
- **端到端**：一个只持 `system_user.list` / `add_edit` / `change_status`、
  **不持**「系统管理员」的账号，能对账号端点做完 list / create / patch / delete 四个动作；
- **承重反向钉子**：同一个账号打首页数据概览 `/api/business/home/overview/` **仍 403**
  （证明放宽没有溢出到首页）；
- **反向**：什么节点都没有的账号打账号端点仍 403（fail-closed）；
- **平面**：`public` 平面上新判据恒 `False`（继承 `nodes_of` / `is_account_admin` 的平面守卫）；
- **结构网**：`apps/accounts/permissions.py` 的节点集合与 `AccountViewSet.ACCOUNT_PERMISSIONS`
  用到的 code **恰好相等**（两份清单必须有一条会红的网钉住，⛔ 不靠注释）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.accounts.permissions import is_account_admin
from apps.business.roles import TENANT_ADMIN
from apps.core.permissions import is_tenant_admin
from apps.platform.bootstrap import PLATFORM_DOMAIN, ensure_public_tenant
from apps.platform.provisioning import seed_tenant
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

#: 首页「数据概览」的端点（判据是 `is_account_admin`，**不是** RBAC 节点；
#: 见 `apps/business/api/console_views.py` 与 `apps/rbac/wiring.py` 的 `home.index` 登记）。
HOME_OVERVIEW_URL = "/api/business/home/overview/"

#: 线 D 的「人事」形态：**恰好**这三个账号管理节点，⛔ 不持「系统管理员」职位。
HR_NODES = ("system_user.list", "system_user.add_edit", "system_user.change_status")

#: 线 D 的「人事」自建职位名（`Role.name` 是 `max_length=5` ⇒ 三个字）。
HR_ROLE = "人事岗"


class AccountAdminPredicateTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        # ⚠️ 实测更正 brief 的一处**事实错误**，不是代码错：
        #    `TenantTestCase.setUpClass` **不会** `seed_tenant`——它只
        #    `cls.tenant = get_tenant_model()(schema_name=...)` 然后 `save()`
        #    （`django_tenants/test/cases.py:36-38`，即只建 schema + 跑迁移），
        #    **生产代码里**只有 `provision_tenant` 调它
        #    （`apps/platform/provisioning.py:106`；全仓 grep `seed_tenant` 的非测试
        #    调用点仅此一处）。测试里直接调用的先例不止一处：
        #    `tests/test_provisioning.py:41-42`、`tests/test_p5_bootstrap.py:49,81-82`，
        #    以及本文件的 `setUpClass` 自身。所以 brief 说「TenantTestCase
        #    建租户时 provision_tenant 会 seed_tenant」不成立：本测试原样跑会以
        #    `User.DoesNotExist` / `Group.DoesNotExist` 报错（实测 2 errors）。
        #    这里显式灌一次基线（admin + DEFAULT_ROLES 的 Group），让真值表第 1 行
        #    断言的确实是 `seed_tenant` 造出来的那个 admin，而不是测试自造的替身。
        #    灌基线是 DML（非 DDL），且 `seed_tenant` 走 `tenant_context`（退出时还原
        #    调用前的租户，`django_tenants/utils.py:136-150`），放在类级安全。
        super().setUpClass()
        seed_tenant(cls.tenant)

    def _user(self, username, *, superuser=False, with_group=False):
        user = User.objects.create_user(username=username, password="pw-123456")
        if superuser:
            user.is_superuser = True
            user.save(update_fields=["is_superuser"])
        if with_group:
            user.groups.add(Group.objects.get(name=TENANT_ADMIN))
        return user

    def test_plain_service_user_is_not_admin(self):
        user = self._user("plain")
        self.assertFalse(is_account_admin(user))
        self.assertFalse(is_tenant_admin(user))

    def test_group_holder_is_admin_on_both_predicates(self):
        """真值表第 3 行（**口径变更**）：持「系统管理员」职位 ⇒ **两个判据都为真**。

        原文断言 `is_tenant_admin` 为假，并把这行说成"两个判据的分叉行"——本线把
        `is_tenant_admin` 的租户平面判据换成同一个 Group 之后，这一行不再分叉
        （分叉搬到平台平面，见 `AccountAdminPredicateOnThePublicPlaneTests`）。
        """
        user = self._user("group-admin", with_group=True)
        self.assertTrue(is_account_admin(user))
        self.assertIs(is_tenant_admin(user), True)

    def test_superuser_without_group_is_neither_admin(self):
        """真值表第 2 行（**反例变正例**）：只置超管标志、不持职位的账号**两个判据都为假**。

        ⚠️ 原文断言 `is_account_admin` 为真（理由是"防止实现写成只看 Group、把 seed 出来的
        admin 锁在门外"）——那句的前提"seed 的 admin 只有标志、不在组里"与实现不符：
        `apps/platform/provisioning.py` 的 `admin.groups.add(...)` 一直把它加进「系统管理员」组，
        所以"seed 出来的管理员仍然算管理员"由下一格（`test_seeded_admin_satisfies_both`）钉。
        """
        user = self._user("super-only", superuser=True)
        self.assertFalse(user.groups.filter(name=TENANT_ADMIN).exists())  # 先证构造物成立
        self.assertIs(is_account_admin(user), False)
        self.assertIs(is_tenant_admin(user), False)

    def test_seeded_admin_satisfies_both(self):
        """真值表第 1 行：`seed_tenant` 的 admin 两者都满足。"""
        user = User.objects.get(username="admin")
        self.assertTrue(is_tenant_admin(user))
        self.assertTrue(is_account_admin(user))

    def test_anonymous_is_not_admin(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertFalse(is_account_admin(AnonymousUser()))

    def test_permission_class_is_instantiable_without_args(self):
        """DRF 会无参实例化权限类（`rest_framework/views.py:283`），P5 在这上面栽过。"""
        from apps.accounts.permissions import IsAccountAdmin

        self.assertIsInstance(IsAccountAdmin(), IsAccountAdmin)

    def test_permission_class_grants_the_position_holder_and_rejects_a_bare_superuser(self):
        """`has_permission` 必须真的接上同一个判据——只测「可实例化」会放过恒 False 的实现。

        请求对象用 `SimpleNamespace(user=...)` 替身即可：`IsAccountAdmin.has_permission`
        是 `request.user` 的**纯函数**（`apps/accounts/permissions.py:53` 只读 `request.user`，
        不读 `view`、不读其它请求属性），替身不会改写被测分支。
        ⚠️ 2026-09-23 后"正向对照"只能是**持职位**的人：裸超管在租户平面上不再是账号管理员。
        """
        from types import SimpleNamespace

        from apps.accounts.permissions import IsAccountAdmin

        perm = IsAccountAdmin()
        group_admin = self._user("perm-group", with_group=True)
        bare_superuser = self._user("perm-super", superuser=True)
        self.assertTrue(perm.has_permission(SimpleNamespace(user=group_admin), None))
        self.assertIs(
            perm.has_permission(SimpleNamespace(user=bare_superuser), None), False
        )

    def test_permission_class_denies_anonymous_and_plain_user(self):
        """反向：匿名与普通用户必须被拒。同一测试里带正向对照（持职位 → True），

        否则「恒 False」的实现也能让这两条 `assertFalse` 通过。
        """
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser

        from apps.accounts.permissions import IsAccountAdmin

        perm = IsAccountAdmin()
        # 正向对照：同一实例、同一调用形状下必须放行（持「系统管理员」职位的人）。
        holder = self._user("perm-holder-control", with_group=True)
        self.assertTrue(perm.has_permission(SimpleNamespace(user=holder), None))
        # 反向断言。
        self.assertFalse(perm.has_permission(SimpleNamespace(user=AnonymousUser()), None))
        self.assertFalse(
            perm.has_permission(SimpleNamespace(user=self._user("perm-plain")), None)
        )


class AccountAdminPredicateOnMeEndpointTests(TenantTestCase):
    """/api/auth/me 必须把两个管理员判据**同时**回给前端（账号线 spec §5.4）。

    上面那组测的是谓词本身；这组测的是**载荷**——谓词对、载荷漏字段，前端照样看不见。
    断言打在真实登录后的真实响应上（不是 logger，E-0009）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)   # Group 行由 seed_tenant 灌；TenantTestCase 自己不灌。

    def test_me_exposes_group_holder_as_admin_on_both_fields(self):
        """持「系统管理员」职位的人：**两个字段都为真**（2026-09-23 口径变更后如此）。

        ⚠️ 原文断言 `is_tenant_admin` 为 `False`，并把这解释成"新字段的存在理由"
        （前端若只看 `is_tenant_admin` 就会把账号管理入口藏起来）。判据统一后两个字段
        在租户平面上一致，该理由作废；本格改成钉"两个字段都为真"，
        而 `is_account_admin` 仍保留的理由是**平台平面**的差异（R14）。
        """
        client = make_logged_in_client(username="me-group-admin", password="pw-123456")
        user = User.objects.get(username="me-group-admin")
        # 先证构造物成立：这个登录者确实不是 superuser（否则测的是另一条分支）。
        self.assertFalse(user.is_superuser)
        user.groups.add(Group.objects.get(name=TENANT_ADMIN))

        # 持 Group **之后**再走一次真实登录：让"登录这个动作本身"也是持组者发出的。
        login = client.post(
            "/api/auth/login",
            data={"username": "me-group-admin", "password": "pw-123456"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200)

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        for label, payload in (
            ("login", login.json()["data"]["user"]),
            ("me", me.json()["data"]["user"]),
        ):
            with self.subTest(endpoint=label):
                self.assertIs(payload["is_account_admin"], True)
                self.assertIs(payload["is_tenant_admin"], True)

    def test_me_reports_plain_user_as_not_account_admin(self):
        """普通用户 `False`；**同一测试内**带正向对照，否则「恒 False」的实现也能通过。"""
        plain_client = make_logged_in_client(username="me-plain", password="pw-123456")
        plain = plain_client.get("/api/auth/me").json()["data"]["user"]
        self.assertIs(plain["is_account_admin"], False)
        self.assertIs(plain["is_tenant_admin"], False)

        # 正向对照：同一端点、同一请求形状，持组者必须 True。
        holder_client = make_logged_in_client(username="me-holder-ctl", password="pw-123456")
        User.objects.get(username="me-holder-ctl").groups.add(
            Group.objects.get(name=TENANT_ADMIN)
        )
        holder = holder_client.get("/api/auth/me").json()["data"]["user"]
        self.assertIs(holder["is_account_admin"], True)


class AccountAdminPredicateOnThePublicPlaneTests(TenantTestCase):
    """R14 / 整体评审：`is_account_admin` 是**租户范围**的判据，平台平面上一律 `False`。

    ⚠️ 为什么必须有**平台平面**的用例：`config/urls.py` 对**每个** Host 都 include 了
    `apps.core.urls`，账号路由因此在平台域名上也可达；而 `apps.accounts` 只装在租户平面
    （`TENANT_APPS`），`public.accounts_staffprofile` 不存在。平台平面的超管在原实现下
    被判成 `is_account_admin=True`，于是 `select_related("staff_profile")` 打到一张
    `public` 里不存在的表 ⇒ `ProgrammingError` ⇒ HTML 500；`/api/auth/me` 还会把
    `is_account_admin: True` 回给前端，把菜单门引到同一个 500。

    本类同时钉住 `is_tenant_admin` **不被**这条改动波及（它仍是 P5 的 `is_superuser` 契约）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 租户平面的正向对照要「系统管理员」Group（`TenantTestCase` 自己不灌基线）。
        seed_tenant(cls.tenant)

    def setUp(self):
        super().setUp()
        # 平台平面必须先有 Host（`TenantMainMiddleware` 查不到 `Domain` 直接 404）。
        # 放 `setUp`（测试事务内）而不是 `setUpClass`：后者会被提交并活过整个进程，
        # 打红 `test_provisioning.py` 里「public 租户行不存在」的断言（与
        # `test_p5_bootstrap.PlatformPlaneLoginTests` 同一手法）。
        ensure_public_tenant(PLATFORM_DOMAIN)
        with schema_context("public"):
            self.public_super = User.objects.create_superuser(
                username="pub-plane-super", email="", password="pw-pub-plane"
            )
        # 真实登录（`Client` 默认不强制 CSRF，与 `make_logged_in_client` 同口径）。
        self.client = Client(SERVER_NAME=PLATFORM_DOMAIN)
        login = self.client.post(
            "/api/auth/login",
            data={"username": "pub-plane-super", "password": "pw-pub-plane"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

    def tearDown(self):
        # ⚠️ 跨域名请求**不会**在响应结束时还原 schema（`TenantMainMiddleware` 只在请求
        # 进来时 `set_tenant`，见 `test_account_isolation` 模块说明）。不钉回来的话，
        # 上一条用例残留的 `public` 会跟着进下一条。
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_predicate_is_false_on_the_public_plane(self):
        with schema_context("public"):
            # 构造前提：这个人确实是平台平面的超管（否则测的可能是别的分支）。
            self.assertTrue(self.public_super.is_superuser)
            self.assertFalse(
                is_account_admin(self.public_super),
                "平台平面仍有账号管理员判据 —— 账号端点会打到不存在的 accounts_staffprofile",
            )
            # 反向边界：本改动**只收**账号判据，P5 的 `is_tenant_admin` 语义不动。
            self.assertTrue(is_tenant_admin(self.public_super))

        # 正向对照：同一个谓词在**租户平面**上对系统管理员仍为 True——否则上面的 False
        # 可能由「谓词恒 False」凑出来。⚠️ 必须显式切回租户平面：`setUp` 里那条平台域名
        # 的登录请求把连接留在了 `public`（见 `test_account_isolation` 模块说明）。
        with tenant_context(self.tenant):
            holder = User.objects.create_user(username="plane-holder", password="pw-123456")
            holder.groups.add(Group.objects.get(name=TENANT_ADMIN))
            self.assertTrue(is_account_admin(holder))

    def test_me_does_not_advertise_account_admin_on_the_public_plane(self):
        """前端菜单门读的就是这个载荷：平台平面上不得把账号管理说成可用。"""
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.content[:200])
        payload = me.json()["data"]["user"]
        self.assertEqual(payload["identity_plane"], "public")
        self.assertIs(payload["is_tenant_admin"], True)      # P5 契约不变
        self.assertIs(payload["is_account_admin"], False)    # R14 要钉的那一格

    def test_account_endpoint_on_the_public_plane_is_a_clean_403(self):
        """平台平面超管访问账号端点：干净的 403，不是 HTML 500（R14 的直接目的）。"""
        response = self.client.get("/api/accounts/")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["code"], 403)

        # 正向对照：同一个端点、同一形状的请求在**租户平面**上必须可用——
        # 否则上面的 403 可能由「端点压根不可用」凑出来。
        connection.set_tenant(self.tenant)
        tenant_client = make_logged_in_client(username="plane-tenant-admin")
        User.objects.get(username="plane-tenant-admin").groups.add(
            Group.objects.get(name=TENANT_ADMIN)
        )
        ok = tenant_client.get("/api/accounts/")
        self.assertEqual(ok.status_code, 200, ok.content[:200])
        self.assertEqual(ok.json()["code"], 0, ok.content[:300])

    def test_the_account_manager_predicate_is_false_on_the_public_plane(self):
        """⭐ 线 D：新判据 `is_account_manager` 在**平台平面恒 `False`**（fail-closed 继承）。

        ⚠️ 它是**继承**来的、不是另写的一份：`is_account_admin` 在 public 恒 False，
        而 `nodes_of` 在 public 返回空集（`apps/rbac/permissions.py`）⇒ 两支都不成立。
        本条把这个继承关系**跑出来**，因为 `is_account_manager` 的第二支读的是**租户表**
        （`rbac_permissionnode` 等只在租户 schema 里）——少了 `nodes_of` 的平面守卫
        就会变成 `ProgrammingError` → HTML 500，与 R14 当初的形态同源。
        """
        from apps.accounts.permissions import is_account_manager

        with schema_context("public"):
            self.assertFalse(
                is_account_manager(self.public_super),
                "平台平面上账号管理判据为真 —— 账号端点会打到不存在的 accounts_staffprofile",
            )

        # 正向对照：同一个谓词在**租户平面**上对「只持账号节点、不是管理员」的人为真
        # ——否则上面的 False 可能由「谓词恒 False」凑出来。
        connection.set_tenant(self.tenant)
        holder = User.objects.create_user(username="plane-hr-holder", password="pw-123456")
        holder.groups.add(Group.objects.get(name="客服"))
        grant_nodes(holder, "system_user.list")
        self.assertTrue(is_account_manager(holder))
        self.assertFalse(is_account_admin(holder), "本对照要求他不是系统管理员")


class AccountManagerPredicateTests(TenantTestCase):
    """⭐ 线 D 的真值表：`is_account_manager` = 系统管理员 **或** 持账号管理节点。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 目录 / 内置角色 / `admin` 都要 `seed_tenant` 灌（`TenantTestCase` 只建空 schema）。
        seed_tenant(cls.tenant)

    def setUp(self):
        # HTTP 用例要一款游戏才能新增账号（`seed_tenant` 的 `DEFAULT_GAMES = ()`）。
        from apps.business.models import Game

        self.game = Game.objects.create(name="王者荣耀")

    def _hr_client(self, username="hr-manager"):
        """线 D 的「人事」：只持三个账号节点，**不持**「系统管理员」职位。"""
        client = make_logged_in_client(
            username=username, groups=(HR_ROLE,), nodes=HR_NODES
        )
        return client, User.objects.get(username=username)

    # ── 真值表 ─────────────────────────────────────────────────────
    def test_the_truth_table_has_four_rows(self):
        """四格：系统管理员 ⇒ True；只持账号节点 ⇒ True；普通账号 ⇒ False；匿名 ⇒ False。

        ⚠️ 每格都带**同一测试内**的对照（否则「恒 True/恒 False」的实现也能通过其中一半）。
        """
        from types import SimpleNamespace

        from django.contrib.auth.models import AnonymousUser

        from apps.accounts.permissions import is_account_manager

        admin = User.objects.get(username="admin")           # seed_tenant 灌的系统管理员
        _, hr = self._hr_client()
        plain = User.objects.create_user(username="mgr-plain", password="pw-123456")
        plain.groups.add(Group.objects.get(name="客服"))     # 客服没有 system_user 节点

        self.assertTrue(is_account_manager(admin))
        self.assertTrue(is_account_manager(hr))
        self.assertFalse(is_account_manager(plain))
        self.assertFalse(is_account_manager(AnonymousUser()))
        self.assertFalse(is_account_manager(None))

        # 判据类本身（形状照 `IsAccountAdmin`）：无参可实例化 + 真的接上同一个判据。
        from apps.accounts.permissions import IsAccountManager

        perm = IsAccountManager()
        self.assertTrue(perm.has_permission(SimpleNamespace(user=hr), None))
        self.assertIs(perm.has_permission(SimpleNamespace(user=plain), None), False)

    def test_a_node_holder_is_not_an_account_admin(self):
        """**承重的前提**：人事过得了新门，但**过不了**旧判据（否则首页概览也会开给他）。

        这条是反向钉子 #2 的**判据层**版：`is_account_admin(hr) is False` 是
        「首页概览对人事 403」的根因。
        """
        _, hr = self._hr_client()
        self.assertFalse(is_account_admin(hr))
        self.assertTrue(
            User.objects.filter(pk=hr.pk, groups__name=TENANT_ADMIN).exists() is False
        )

    # ── 端到端（判据 #1：「人事能开子账户」的可重跑判据）────────────────
    def test_an_hr_can_list_create_edit_and_soft_delete_an_account(self):
        """四个动作全 200 + `code 0`；写动作的终态打在**数据库行**上。

        ⚠️ 判「成功」必须看**业务码**（本仓信封把业务失败也回成 HTTP 200；
        E-0063：`response.data is not None` 恒真，不能当判据）。
        ⚠️ 目标账号由**这次请求自己创建**（`create` 的响应体里拿 pk），
        避免拿数据库对象拼参数（E-0176 的口径）。
        """
        from apps.accounts.models import StaffProfile
        from tests.test_account_api import CREATE_REQUIRED_PROFILE_FIELDS

        client, hr = self._hr_client()
        self.assertFalse(is_account_admin(hr), "夹具前提：人事不是系统管理员")

        listed = client.get("/api/accounts/")
        self.assertEqual(listed.status_code, 200, listed.content[:200])
        self.assertEqual(listed.json()["code"], 0, listed.content[:300])

        created = client.post(
            "/api/accounts/",
            {
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "made-by-hr",
                "role": "客服",
                "games": [self.game.pk],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content[:300])
        self.assertEqual(created.json()["code"], 0, created.content[:300])
        new_pk = created.json()["data"]["account"]["id"]
        # 终态：账号行真的落库了（不只是响应好看）。
        self.assertTrue(User.objects.filter(pk=new_pk, username="made-by-hr").exists())

        edited = client.patch(
            f"/api/accounts/{new_pk}/",
            {"display_name": "人事改的名字"},
            content_type="application/json",
        )
        self.assertEqual(edited.status_code, 200, edited.content[:300])
        self.assertEqual(edited.json()["code"], 0, edited.content[:300])
        self.assertEqual(User.objects.get(pk=new_pk).first_name, "人事改的名字")

        deleted = client.post(f"/api/accounts/{new_pk}/delete/")
        self.assertEqual(deleted.status_code, 200, deleted.content[:300])
        self.assertEqual(deleted.json()["code"], 0, deleted.content[:300])
        # 终态：软删的两笔写入都落在**数据库行**上（不是只看信封）。
        profile = StaffProfile.objects.get(user_id=new_pk)
        self.assertIsNotNone(profile.deleted_at)
        self.assertIs(User.objects.get(pk=new_pk).is_active, False)

    # ── 承重反向钉子 #2 ─────────────────────────────────────────────
    def test_the_same_hr_is_still_403_on_the_home_overview(self):
        """⭐ **承重反向钉子**：同一个账号打首页数据概览 ⇒ **403**。

        这条证明线 D 的放宽**没有溢出**到首页概览（那是 `is_account_admin` 的消费点，
        本线刻意一个字不改）。⛔ 不接受「200 + 全 0」——那正是前端会当真的假数据。
        """
        client, hr = self._hr_client()
        response = client.get(
            HOME_OVERVIEW_URL, {"start": "2026-09-10", "end": "2026-09-12"}
        )
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response.json()["code"], 403, response.content[:300])
        self.assertIsNone(response.json()["data"])

        # 正向对照：同一形状的请求换成系统管理员必须 200 —— 否则上面的 403 可能由
        # 「端点坏了 / 参数格式变了」凑出来。
        admin_client = make_logged_in_client(username="mgr-home-admin")
        User.objects.get(username="mgr-home-admin").groups.add(
            Group.objects.get(name=TENANT_ADMIN)
        )
        ok = admin_client.get(
            HOME_OVERVIEW_URL, {"start": "2026-09-10", "end": "2026-09-12"}
        )
        self.assertEqual(ok.status_code, 200, ok.content[:300])
        self.assertEqual(ok.json()["code"], 0, ok.content[:300])

    # ── 反向 #3：放宽后仍 fail-closed ────────────────────────────────
    def test_a_plain_account_with_no_nodes_is_still_403_on_the_account_endpoints(self):
        """什么账号节点都没有的普通账号 ⇒ 账号端点 **403**（放宽后仍 fail-closed）。

        ⚠️ 夹具自证：先证明这个账号的节点集与 `ACCOUNT_MANAGEMENT_NODES` **交集为空**
        ——否则「403 是权限挡的」这句话可能是别的节点凑出来的。
        """
        from apps.accounts.permissions import ACCOUNT_MANAGEMENT_NODES, is_account_manager
        from apps.rbac.permissions import nodes_of

        client = make_logged_in_client(username="mgr-no-node", groups=("客服",))
        user = User.objects.get(username="mgr-no-node")
        self.assertEqual(nodes_of(user) & ACCOUNT_MANAGEMENT_NODES, frozenset())
        self.assertFalse(is_account_manager(user))

        for label, call in (
            ("list", lambda: client.get("/api/accounts/")),
            ("roles", lambda: client.get("/api/accounts/roles/")),
        ):
            with self.subTest(action=label):
                response = call()
                self.assertEqual(response.status_code, 403, response.content[:300])
                self.assertEqual(response.json()["code"], 403)
                self.assertIsNone(response.json()["data"])

        # 写动作同样被拒，且**不留副作用**（新增一行都没落库）。
        created = client.post(
            "/api/accounts/",
            {"username": "never-created", "role": "客服", "games": [self.game.pk]},
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 403, created.content[:300])
        self.assertFalse(User.objects.filter(username="never-created").exists())

    # ── D5 核对：职位下拉是**角色数据**，不是硬编码枚举 ────────────────
    def test_the_roles_dropdown_offers_the_hr_role_from_the_data(self):
        """`GET /api/accounts/roles/` 的值域是**当前租户的 `Role` 表**。

        线 D 只往种子里加了「人事」角色 ⇒ 前端「新增子账户」的职位下拉**不需要改**
        （`src/frontend/src/layui-island/pages/user-adduser.ts` 调 `fetchAccountRoles()`
        → `GET /api/accounts/roles/`）。本条把「下拉取数能看到它」跑出来
        （⛔ 不是只读代码就说「能选到」）。
        """
        admin_client = make_logged_in_client(username="mgr-dropdown-admin")
        User.objects.get(username="mgr-dropdown-admin").groups.add(
            Group.objects.get(name=TENANT_ADMIN)
        )
        body = admin_client.get("/api/accounts/roles/").json()
        self.assertEqual(body["code"], 0, body)
        self.assertIn("人事", body["data"])
        # 反面控制：下拉不是「什么都回」——一个不存在的角色名必须**不在**里面。
        self.assertNotIn("不存在的职位", body["data"])


class AccountManagementNodesStructuralNetTests(SimpleTestCase):
    """⭐ 线 D 的**结构网**：两份清单必须**恰好**相等（⛔ 不许靠注释说「保持一致」）。

    两份清单是**两份真代码**、不是同一份的两个引用：
    - `apps/accounts/permissions.py::ACCOUNT_MANAGEMENT_NODES`（新判据读它）；
    - `apps/accounts/views.py::AccountViewSet.ACCOUNT_PERMISSIONS`（节点门读它）。
    它们**不能**互相 import（模块级会成环，而且 `views` 那份必须是字面量——
    `tests/test_rbac_structure.py` 的 B2 走 AST 取字符串常量）⇒ 靠本条钉住。
    """

    def test_the_node_set_is_exactly_the_codes_the_view_set_uses(self):
        from apps.accounts.permissions import ACCOUNT_MANAGEMENT_NODES
        from apps.accounts.views import AccountViewSet

        used = {
            code
            for codes in AccountViewSet.ACCOUNT_PERMISSIONS.values()
            for code in codes
        }
        # ① 值钉子：常量本身 = 这 6 条字面量（⛔ 不引视图那份 dict 当期望值，那是自指）。
        #    往 `ACCOUNT_PERMISSIONS` 加一个新 code 并同步改常量时，这一条会红 ⇒ 强制复核。
        self.assertEqual(
            set(ACCOUNT_MANAGEMENT_NODES),
            {
                "system_user.list",
                "system_user.add_edit",
                "system_user.change_status",
                "system_user.reset_password",
                "system_user.realname_records",
                "system_user.reset_realname",
            },
        )
        # ② 结构网：两份清单**恰好相等**（不是「包含即可」）——
        #    少一条 ⇒ 判据放宽的范围比节点门窄（某个动作永远进不去）；
        #    多一条 ⇒ 判据放宽的范围比节点门宽（多出来的 code 能过准入却不被任何动作要求）。
        self.assertEqual(
            set(ACCOUNT_MANAGEMENT_NODES),
            used,
            "账号管理节点集合与 `ACCOUNT_PERMISSIONS` 用到的 code 不再恰好相等",
        )
