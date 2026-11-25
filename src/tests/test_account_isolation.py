"""AGENTS.md §4 / spec §7.3：以租户 A 的凭据访问 B 的数据必须失败（逐**写**路径）。

**本文件证什么、不证什么**（照 spec §7.3 划分的三类，别混着读）：

- **第一类·凭据层**（P5 的守卫，本线只补测试）：A 的会话拿到 B 的域名上 → 401。
  它由中间件短路，**根本没进视图**，所以它**不是**写路径作用域的测试。
- **第二类·写路径作用域**：用 B 的合法会话去打 A 的对象主键，逐写路径一条
  （#2 `create`、#4 `PATCH`/`PUT`、#5 `status`、#6 批量 `status`、#7 `reset-password`）。
  断言打在**数据库行**上（E-0009），不是响应文案。
- **第三类·跨租户外键**：A 的 game id 在 B 里必须解析不到 ⇒ `1000` 且不落任何行。

⚠️ **断言不能写成「按 A 的 id 取一定是 404」**（spec §7.3 明确点出的坑）：
两个 schema 的 `auth_user` 自增序列各自从 1 开始（`seed_tenant` 给**每个**租户灌一个
`admin`），id 天然重叠，所以那条 404 永远不会发生。要断言的是
「**A 里那一行没被改动**」——和「取到的是**本 schema** 的那一行」。

⚠️ **判别力从哪来**：光断言「A 的行没变」在「B 其实取到了 A 的行、只是没改」
或「请求压根没发出去」时也会绿。所以每条负向用例都配一条**正向对照**：
同一夹具下，B 用**自己**（甚至**同号**）的主键做同一个动作**必须真的生效**。
没有正向对照，「没改 A」可能由「这个动作对谁都不生效」凑出来（spec §7.1 的 I1）。

⚠️ **两条写路径构造上的实测坑**（别把它们「修」回去）：
1. **对象主键不能与请求者自己同号**：两个 schema 的 pk 各自从 1 开始，「B 的管理员」
   与「A 的某个账号」很容易撞号，而 `_apply_status` 的 ③（自锁，1004）在**目标 pk ==
   请求者 pk** 时**先于**「这行到底是谁的」被判定 ⇒ 负断言会读到 1004 而不是预期的
   1002/1000。所以 A-only 靶子要造成一个 **B 里不存在的 pk**。
2. **`member_marker.pk` 必须来自同一个测试的夹具**：`TenantTestCase` 每条用例回滚，
   跨用例缓存的对象主键可能悬空。

⚠️ **跨域名请求会**在请求结束后**把连接留在那个租户的 schema 上**（**实测**）。
`django_tenants` 的 `TenantMainMiddleware` 在**请求进来时**
`connection.set_tenant(...)`，但**不在响应结束时还原**——所以一次
`client_b.get("/api/accounts/")` 之后，测试进程的连接停在 `acct_iso_b`，
接下来那句 `User.objects.get(pk=<A 的主键>)` 会去 B 的 schema 里查 ⇒ `DoesNotExist`
（症状看着像「A 的行被删了」，其实是查错了库）。本文件**所有** B 侧操作一律走
`_b_call()`，它用 `try/finally` 把连接显式钉回租户 A；`setUp` 第一行也显式
`set_tenant(self.tenant)`（挡住上一条用例的残留）。**别把这些还原去掉**：
它们是本文件能重复运行的前提。

⚠️ **本类抓不到什么（spec §7.3 要求写下来，不许读成「跨租户已被完全证明」）**：
它只对**走当前 schema 下 ORM** 的查询路径有判别力。一条手工拼 SQL、或把 schema
固定在别处的查询路径**不会被这里抓到**。

⚠️ **不用「同名行」当锚点**（E-0011 的陷阱）：两个租户都有 `admin` 这一行，拿「用户名是 admin」当断言会被两个 schema 同时满足——本文件的**类级标记账号**（`A_MARKER` / `B_MARKER`）一律带租户后缀。
⚠️ **例外**（故上句不是全文件的不变量）：`test_forbid_self_service_does_not_cross_tenants` 刻意在两侧都建 `iso-same-name`，判别力来自**两侧取值不同**（见该用例 docstring）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.accounts.permissions import is_account_admin
from apps.business.grab_limits import limits_of
from apps.business.models import Game
from apps.business.roles import SERVICE, SUPERVISOR, TENANT_ADMIN
from apps.core.permissions import _has_role, is_tenant_admin
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 租户 A（即 `TenantTestCase` 的 `test` schema）的域名，与既有账号测试同口径。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名。刻意与 A 不同：`Domain` 上有唯一约束，撞了会在 `provision_tenant` 里报错。
DOMAIN_B = "acct-iso-b.test.local"

B_SCHEMA = "acct_iso_b"

#: A 与 B 各自的标记账号。**名字不同**（见模块说明：同名会让断言失去判别力）。
A_MARKER = "iso-only-in-a"
B_MARKER = "iso-only-in-b"

#: 职位下拉跨租户用例的两个自建角色名。**名字不同**、且**各占一个租户**：
#: `Role.name` 的 `max_length=5`，所以这两个恰好 5 个字符（超长会 `DataError`）。
A_ONLY_ROLE = "A专有职位"
B_ONLY_ROLE = "B专有职位"

#: 错误码字面值。写数字而不是枚举，是为了让「码值被改掉」在测试里可见。
OK = 0
VALIDATION = 1000
BUSINESS = 1002


def _row_snapshot(user):
    """把一行 `auth_user` 的**全部具体列**取成 dict，供逐字段比对。

    用 `_meta.concrete_fields` 而不是手写清单：手写清单里漏掉的那一列，
    正是「悄悄被改了却全绿」的入口。
    """
    return {
        field.attname: getattr(user, field.attname)
        for field in User._meta.concrete_fields
    }


class AccountIsolationTests(TenantTestCase):
    """逐写路径的跨租户判别力用例。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # R5：`TenantTestCase` 只建 schema 跑迁移，不灌基线数据；本类要
        # `Group.objects.get(TENANT_ADMIN)` 与可登录的管理员。
        seed_tenant(cls.tenant)
        # 第二个租户**必须**在 `setUpClass` 建：在一个用例内部建 schema 会与外层事务相冲
        # （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。与
        # `tests/test_account_api.py::AccountCrossTenantTests` 同一手法。
        cls.tenant_b, _ = provision_tenant(
            schema_name=B_SCHEMA, name="账号隔离租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据。不变量「B 也有 admin 与
        # 默认 Group」是本类的前提，不能依赖 schema 恰好是本次运行刚建的。
        seed_tenant(cls.tenant_b)

    # ── 夹具 ───────────────────────────────────────────────────────
    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A。见模块说明「schema 上下文会跨用例残留」。
        connection.set_tenant(self.tenant)
        self.tenant_admin = Group.objects.get(name=TENANT_ADMIN)

        # A 侧：申请者（管理员）与标记账号。标记账号的 pk 与**B 的管理员**同号是常态
        # （两边序列都从 1 起）。下面的跨租户用例因此都用 A-only pk（B 里不存在），
        # 免得撞上 `_apply_status` 的 ③ 自锁判定（见模块说明坑 #1）。
        self.client_a = make_logged_in_client(domain=DOMAIN_A, username="iso-admin-a")
        self.admin_a = User.objects.get(username="iso-admin-a")
        self.admin_a.groups.add(self.tenant_admin)
        self.game_a = Game.objects.create(name="仅 A 的游戏")
        self.marker_a = self._make_account(A_MARKER)

        # B 侧：同样的形状（管理员 + 标记账号），在 B 的 schema 里。
        try:
            with tenant_context(self.tenant_b):
                self.client_b = make_logged_in_client(domain=DOMAIN_B, username="iso-admin-b")
                self.admin_b_pk = User.objects.get(username="iso-admin-b").pk
                User.objects.get(username="iso-admin-b").groups.add(
                    Group.objects.get(name=TENANT_ADMIN)
                )
                self.marker_b = self._make_account(B_MARKER)
        finally:
            # 夹具里也显式还原：登录夹具发过一条跨域名请求（见模块说明）。
            connection.set_tenant(self.tenant)

        # A-only 靶子：一个**只在 A 里存在**的 pk（B 的 schema 里没有这个号）。
        # 它比两侧现有主键都大，且两侧的 admin/标记账号都不在这个号上。
        self.a_only_pk = max(self.marker_a.pk, self.marker_b.pk, self.admin_b_pk) + 100
        self.a_only = User.objects.create_user(
            pk=self.a_only_pk, username=f"{A_MARKER}-target", password="pw-123456"
        )

        # 夹具自证（**不是**用例，是承重点）：这些前提不成立时下面每条负断言都失去意义。
        # 放在 `setUp` 里而不是单列一条用例，是因为它们是**本类每条**用例的前提。
        assert self.a_only.pk == self.a_only_pk
        assert User.objects.filter(pk=self.a_only_pk, username=A_MARKER + "-target").exists(), \
            "A-only 靶子没落到 A 的 schema 上"
        with tenant_context(self.tenant_b):
            assert not User.objects.filter(pk=self.a_only_pk).exists(), \
                "A-only 靶子居然在 B 里也存在——本类的前提坏了"
            assert User.objects.filter(username=B_MARKER).exists(), "B 的标记账号缺失"

    # ── 工具 ───────────────────────────────────────────────────────
    def _make_account(self, username):
        """在当前 schema 里建一个带档案的账号（`User` 的返回实例属于当前 schema）。"""
        user = User.objects.create_user(username=username, password="pw-123456")
        StaffProfile.objects.create(user=user)
        return user

    def _a_row(self, pk):
        """在 **A 的 schema** 里读一行——负向断言必须打在 A 的真实行上。"""
        return User.objects.get(pk=pk)

    def _a_state(self, pk):
        return _row_snapshot(self._a_row(pk))

    def _b_call(self, function):
        """在 B 的 schema 上下文里执行 `function()`，**结束后把连接钉回租户 A**。

        见模块说明：跨域名请求**不会**在响应结束时还原 schema，所以「事后还在 A 上」
        这件事必须由这里保证，不能靠框架默认行为。
        """
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def _b_row(self, pk):
        return self._b_call(lambda: User.objects.get(pk=pk))

    def _b_state(self, pk):
        return _row_snapshot(self._b_row(pk))

    # ── 构造物自证（先跑，否则下面的负断言可能是「夹具不成立」凑出来的）────
    def test_the_fixture_actually_builds_two_separate_tenants(self):
        """两侧都有管理员、都有标记账号，且**互相看不见**。

        这一段是整份文件的承重点——「A 的行没变」若因为 A 的行压根不存在而成立，
        下面每条都是假的。
        """
        self.assertEqual(User.objects.filter(username=A_MARKER).count(), 1)
        self.assertFalse(User.objects.filter(username=B_MARKER).exists())
        self.assertEqual(User.objects.filter(username=f"{A_MARKER}-target").count(), 1)
        self.assertTrue(self._b_call(
            lambda: User.objects.filter(username=B_MARKER).count()
        ) == 1)
        self.assertTrue(self._b_call(
            lambda: User.objects.filter(username=A_MARKER).exists()
        ) is False, "B 的 schema 里看到了 A 的标记账号")
        self.assertTrue(self._b_call(
            lambda: User.objects.filter(pk=self.a_only_pk).exists()
        ) is False, "A-only 靶子的主键在 B 里也存在——重叠构造失效")
        # 两侧都有可登录的管理员（夹具本身的前提）。
        self.assertTrue(self.admin_a.groups.filter(name=TENANT_ADMIN).exists())
        self.assertTrue(self._b_call(
            lambda: User.objects.get(pk=self.admin_b_pk).groups.filter(
                name=TENANT_ADMIN
            ).exists()
        ))

    # ── 第一类：凭据层（P5 的守卫，本线只补测试）────────────────────
    def test_a_session_on_b_host_is_rejected(self):
        """A 的会话 + B 的域名 → 401（中间件短路，未进视图）。

        正向对照：**同一个** client 回到自己的域名上必须 200（否则这条 401 可能由
        「这个会话压根不可用」凑出来，与域名绑定无关）。
        """
        on_own_host = self.client_a.get("/api/accounts/", SERVER_NAME=DOMAIN_A)
        self.assertEqual(on_own_host.status_code, 200, on_own_host.content[:200])
        self.assertEqual(on_own_host.json()["code"], OK)

        crossed = self.client_a.get("/api/accounts/", SERVER_NAME=DOMAIN_B)
        self.assertEqual(crossed.status_code, 401, crossed.content[:200])

    # ── 第二类(a)：列表 / 详情不泄漏另一租户的**值** ────────────────
    def test_b_list_never_contains_a_accounts(self):
        """另一租户的**特征用户名**不得出现在本租户的列表里。

        ⚠️ 断言落在**值**上，且先自证 B 的列表**非空**：空列表能让
        `assertNotIn` 通过，却什么也没证明。
        """
        body = self._b_call(lambda: self.client_b.get("/api/accounts/").json())
        self.assertEqual(body["code"], OK)
        usernames = [item["username"] for item in body["data"]["items"]]
        # 正向对照（非空）：B 自己的标记账号确实在列表里。
        self.assertTrue(usernames, "B 的列表为空——这条负断言由空列表凑出来了，测试自身失效")
        self.assertIn(B_MARKER, usernames)
        self.assertIn("admin", usernames)          # `seed_tenant` 灌的基线管理员
        self.assertNotIn(A_MARKER, usernames)
        self.assertNotIn(f"{A_MARKER}-target", usernames)

    def test_roles_dropdown_never_contains_the_other_tenants_role(self):
        """查询 `role_names()` 的跨租户读数（`AGENTS.md` §4）：A 的下拉不含 B 自建的角色。

        ⚠️ **只比名字，绝不比主键**：两个 schema 的主键序列各自从 1 起，拿 pk 指认
        「另一个租户的那一行」会在本租户命中**自己**的行（`AGENTS.md` §5 的踩坑记录）。
        正向对照：同一个响应里**必须**含本租户自建的名字——否则「不含 B」可能只是
        「响应是空的 / 端点根本没吐出租户自建角色」凑出来的。
        """
        Role.objects.create(name=A_ONLY_ROLE, group=Group.objects.create(name=A_ONLY_ROLE))
        self._b_call(lambda: Role.objects.create(
            name=B_ONLY_ROLE, group=Group.objects.create(name=B_ONLY_ROLE)
        ))

        # 夹具自证：两个名字各自只落在一个 schema 里（不成立时下面的负断言无意义）。
        self.assertTrue(Role.objects.filter(name=A_ONLY_ROLE).exists())
        self.assertFalse(Role.objects.filter(name=B_ONLY_ROLE).exists())
        self.assertTrue(self._b_call(lambda: Role.objects.filter(name=B_ONLY_ROLE).exists()))
        self.assertFalse(self._b_call(lambda: Role.objects.filter(name=A_ONLY_ROLE).exists()))

        body_a = self.client_a.get("/api/accounts/roles/").json()
        self.assertEqual(body_a["code"], OK)
        self.assertIn(A_ONLY_ROLE, body_a["data"])          # 正向对照：非空且含本租户自建
        self.assertNotIn(B_ONLY_ROLE, body_a["data"])

        body_b = self._b_call(lambda: self.client_b.get("/api/accounts/roles/").json())
        self.assertEqual(body_b["code"], OK)
        self.assertIn(B_ONLY_ROLE, body_b["data"])
        self.assertNotIn(A_ONLY_ROLE, body_b["data"])

    def test_a_role_from_another_tenant_is_rejected(self):
        """租户 A 的职位名在租户 B 的**写**（新增）请求里必须无效（`AGENTS.md` §4）。

        判据是请求期的 `Role.objects.filter(name=…)`（查**当前 schema**）：B 的 schema 里
        没有 A 那一行 ⇒ `1000`，且一行都不落库。
        ⚠️ **只比名字，绝不比主键**（同上面那条读路径用例）：两个 schema 的主键序列各自
        从 1 起，拿 pk 指认「另一个租户的那一行」会在本租户命中**自己**的行。
        正向对照：同一个端点、同一载荷，换成 **B 自己**的自建职位名（`B_ONLY_ROLE`）必须
        建号成功——没有它，「A 的名字被拒」可能只是「这个端点拒掉一切自建职位」凑出来的。
        """
        Role.objects.create(name=A_ONLY_ROLE, group=Group.objects.create(name=A_ONLY_ROLE))
        self._b_call(lambda: Role.objects.create(
            name=B_ONLY_ROLE, group=Group.objects.create(name=B_ONLY_ROLE)
        ))
        # 夹具自证：两个名字各占一个 schema（不成立时下面的负断言无意义）。
        self.assertTrue(Role.objects.filter(name=A_ONLY_ROLE).exists())
        self.assertFalse(Role.objects.filter(name=B_ONLY_ROLE).exists())
        self.assertTrue(self._b_call(lambda: Role.objects.filter(name=B_ONLY_ROLE).exists()))
        self.assertFalse(self._b_call(lambda: Role.objects.filter(name=A_ONLY_ROLE).exists()))

        game_b = self._b_call(lambda: Game.objects.create(name="仅 B 的游戏-跨境职位"))

        def payload(role):
            # R13：六个「必填，照抄」字段带齐（同 `test_b_cannot_submit_a_game_id`）。
            return {
                "username": "iso-cross-role",
                "display_name": "跨境职位",
                "role": role,
                "games": [game_b.pk],
                "commission_value": "0.00",
                "deposit_amount": "0.00",
                "grab_limit_dl": 0,
                "grab_limit_pw": 0,
                "hired_on": "2026-01-02",
            }

        body = self._b_call(lambda: self.client_b.post(
            "/api/accounts/", payload(A_ONLY_ROLE), content_type="application/json"
        ).json())
        self.assertEqual(body["code"], VALIDATION, body)
        # 断言**数据库行**：被拒的请求不得留下半个账号。
        self.assertFalse(self._b_call(
            lambda: User.objects.filter(username="iso-cross-role").exists()
        ))

        # 正向对照：换成 B 自己的自建职位名 ⇒ 建号成功，且职位落的是 B 的那一行。
        control = self._b_call(lambda: self.client_b.post(
            "/api/accounts/", payload(B_ONLY_ROLE), content_type="application/json"
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertEqual(
            self._b_call(
                lambda: User.objects.get(username="iso-cross-role")
                .groups.values_list("name", flat=True).first()
            ),
            B_ONLY_ROLE,
        )

    def test_detail_of_an_overlapping_pk_returns_this_tenants_row(self):
        """同一 pk 在**两个** schema 里都有行时，取到的是**本** schema 那一行。

        构造重叠：A 与 B 各自把新账号造成**同一个** pk（序列各自推进，巧合不可依赖）。
        ⚠️ 断言落在 username 上，不是状态码——那个 pk 在两边都存在，404 永远不会发生。
        """
        overlap_pk = max(self.marker_a.pk, self.marker_b.pk) + 200
        # ⚠️ pk 必须在 `create_user` 时给：建成后再 `update(id=…)` 会把
        # `auth_user_groups.user_id` 之类的外键悬空（实测 IntegrityError）。
        User.objects.create_user(pk=overlap_pk, username=f"{A_MARKER}-overlap",
                                 password="pw-123456")
        self._b_call(lambda: User.objects.create_user(
            pk=overlap_pk, username=f"{B_MARKER}-overlap", password="pw-123456"
        ))
        # 自证前提：两侧同号、值不同。
        self.assertEqual(self._a_row(overlap_pk).username, f"{A_MARKER}-overlap")
        self.assertEqual(self._b_row(overlap_pk).username, f"{B_MARKER}-overlap")

        detail = self.client_a.get(f"/api/accounts/{overlap_pk}/").json()
        self.assertEqual(detail["code"], OK, "重叠 pk 在 A 里必须能取到 A 自己的行")
        self.assertEqual(detail["data"]["username"], f"{A_MARKER}-overlap")
        self.assertNotEqual(detail["data"]["username"], f"{B_MARKER}-overlap")
        self.assertEqual(detail["data"]["id"], overlap_pk)

        # 反向对照：B 的会话取同一个 pk，必须拿到 **B** 那一行（同一个 id，两个答案）。
        detail_b = self._b_call(
            lambda: self.client_b.get(f"/api/accounts/{overlap_pk}/").json()
        )
        self.assertEqual(detail_b["code"], OK)
        self.assertEqual(detail_b["data"]["username"], f"{B_MARKER}-overlap")

    def test_detail_of_pk_one_returns_this_tenants_admin(self):
        """`admin` 在**每个**租户都存在（`seed_tenant`）且 pk 都是 1——最容易串号的一格。

        ⚠️ 两个租户的 `admin` **用户名相同**，所以「用户名是 admin」这条**没有判别力**。
        这里把 A 的基线 `admin` 改成只有 A 才有的值（改名 + 换显示名），再断言取到的是
        **这些值**——串号时读到的会是 B 的 `admin`（用户名仍为 `admin`、显示名为空）。
        """
        a_admin = self._a_row(1)
        self.assertEqual(a_admin.username, "admin")           # 先自证基线形状
        a_admin.username = "iso-seeded-admin-a"
        a_admin.first_name = "仅A的超管"
        a_admin.save(update_fields=["username", "first_name"])
        with tenant_context(self.tenant_b):
            self.assertEqual(User.objects.get(pk=1).username, "admin")
            self.assertEqual(User.objects.get(pk=1).first_name, "")

        detail = self.client_a.get("/api/accounts/1/").json()
        self.assertEqual(detail["code"], OK)
        self.assertEqual(detail["data"]["username"], "iso-seeded-admin-a")
        self.assertEqual(detail["data"]["display_name"], "仅A的超管")
        self.assertTrue(detail["data"]["is_tenant_admin"],
                        "取到了 B 的 admin —— 主键相同，值必须由 schema 决定")
        self.assertEqual(detail["data"]["role"], TENANT_ADMIN)

    def test_forbid_self_service_does_not_cross_tenants(self):
        """A 把某人设成 `True` ⇒ B 侧读到的仍是 `False`（`AGENTS.md` §4）。

        两条判据**都留**，各自覆盖一层、且都自带判别力：
        ① **按用户名**：两个 schema 各有一行**同名**账号、取值不同 ⇒ 串 schema 时按名字读
           会命中 A 的 `True`；
        ② **同一个 pk 在两侧取值相反**（覆盖读取层 `limits_of`）：两侧都用 **A 的 pk** 去读
           ⇒ 串 schema 时同样读到 A 的 `True`。
        ⚠️ **不要**把 ② 退回「按用户名取 **B 的 pk** 再喂 `limits_of`」：两个 schema 的 pk
        序列各自从 1 起、本例实测**不相等**，且 **A 里不存在 `user_id = B 的 pk` 的档案**
        ⇒ 串到 A 时读取层 `_limits(None, …)` 恰好返回 `False` = 断言期望值 ⇒ 负断言由
        「A 里没这一行」凑出来，**判别力为零**。判别力的来源是**取值相反**，与
        `test_detail_of_pk_one_returns_this_tenants_admin`（两个租户都有 `admin`、靠值区分）
        同一手法；模块说明那条「同名不构成判别力」说的是**拿名字的有无**当断言。
        """
        same = "iso-same-name"
        a_user = User.objects.create_user(username=same, password="pw-123456")
        StaffProfile.objects.create(user=a_user)
        self._b_call(lambda: StaffProfile.objects.create(
            user=User.objects.create_user(username=same, password="pw-123456")
        ))
        # 夹具自证：两侧各有一行同名账号（否则下面的负断言可能由「B 里没有这行」凑出来）。
        self.assertTrue(StaffProfile.objects.filter(user__username=same).exists())
        self.assertTrue(self._b_call(
            lambda: StaffProfile.objects.filter(user__username=same).exists()
        ))

        # A 侧经**真实写路径**打开这个开关。
        body = self.client_a.patch(
            f"/api/accounts/{a_user.pk}/",
            {"grab_forbid_self_service": True},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], OK, body)
        # 落库判据（A 的 schema）。
        self.assertIs(
            StaffProfile.objects.get(user__username=same).grab_forbid_self_service, True
        )
        # 读取层判据：P3e 的**新增读取**（`grab_limits._limits`）在本租户带出的也是 `True`。
        # ⚠️ 这里用 **A 的 pk**（`a_user.pk`）——下面的跨租户对照要与它读**同一个数字**，
        #    才谈得上「串 schema 就读到相反的取值」。这条是 A 侧的**对照锚点**。
        self.assertIs(limits_of(a_user.pk).forbid_self_service, True)

        # ── 判据②的读取层版本：拿 **A 的 pk** 去读 **B** ⇒ 必须 `False` ──────────
        # 判别力从哪来：同一个 pk 在这两个 schema 里**取值相反**（A 的档案是 `True`）。
        # 若读取层串到 A（`limits_of` 查的是**当前连接所在 schema**），这里会读到 A 的
        # `True` ⇒ 本断言**红**。
        self.assertIs(
            self._b_call(lambda: limits_of(a_user.pk).forbid_self_service), False
        )

        # ── 判据①：按用户名读（`61328cb` 的旧写法，**也保留**，自带判别力）──────
        # B 侧读 B 自己的同名行 ⇒ `False`；若 ORM 读串到 A，会命中 **A 的同名行**
        # （`True`）⇒ 红。它覆盖「按值取名」的读法，上面那条覆盖 P3e 新增的读取层。
        self.assertIs(
            self._b_call(
                lambda: StaffProfile.objects.get(
                    user__username=same
                ).grab_forbid_self_service
            ),
            False,
        )

    # ── 第二类(b)：逐写路径 —— B 的会话打 A-only 的对象 ─────────────
    def test_b_cannot_update_a_target(self):
        """#4 编辑（`PATCH`）：A-only 主键 ⇒ B 取不到该行，A 的行逐列未变。"""
        target_pk = self.a_only_pk
        before = self._a_state(target_pk)
        body = self._b_call(lambda: self.client_b.patch(
            f"/api/accounts/{target_pk}/",
            {"username": "iso-hijacked-by-b"},
            content_type="application/json",
        ).json())
        self.assertEqual(body["code"], BUSINESS, body)     # A 的 schema 里没有这个主键
        # 断言打在**数据库行**上，逐列（不是「某个字段没变」）。
        self.assertEqual(self._a_state(target_pk), before)
        self.assertFalse(User.objects.filter(username="iso-hijacked-by-b").exists())

        # 正向对照：B 用**自己**的标记账号做**同一个** PATCH，必须真的生效——
        # 否则上面的「A 没变」可能由「这个动作对谁都不生效」凑出来。
        control = self._b_call(lambda: self.client_b.patch(
            f"/api/accounts/{self.marker_b.pk}/",
            {"username": f"{B_MARKER}-renamed"},
            content_type="application/json",
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertEqual(self._b_row(self.marker_b.pk).username, f"{B_MARKER}-renamed")
        # A 的行在这一步之后仍然未变（跨请求的稳定性）。
        self.assertEqual(self._a_state(target_pk), before)

    def test_b_cannot_put_a_target(self):
        """#4 的另一半（`PUT`）：`PUT` 与 `PATCH` 走同一个 `update()`，但路由入口不同。"""
        target_pk = self.a_only_pk
        before = self._a_state(target_pk)
        body = self._b_call(lambda: self.client_b.put(
            f"/api/accounts/{target_pk}/",
            {"username": "iso-put-hijack", "role": SERVICE, "games": [self.game_a.pk]},
            content_type="application/json",
        ).json())
        self.assertEqual(body["code"], BUSINESS, body)
        self.assertEqual(self._a_state(target_pk), before)

        # 正向对照：B 对自己那一行发同形状的 PUT。`PUT` 是整单替换（`partial=False`），
        # 序列化器要求 `username` / `role` / `games` 三个必填项都在（`games=[]` 会被
        # `allow_empty=False` 拒成 1000），所以这里带齐三项。
        game_b = self._b_call(lambda: Game.objects.create(name="仅 B 的游戏-PUT"))
        control = self._b_call(lambda: self.client_b.put(
            f"/api/accounts/{self.marker_b.pk}/",
            {"username": f"{B_MARKER}-put", "role": SERVICE, "games": [game_b.pk]},
            content_type="application/json",
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertEqual(self._b_row(self.marker_b.pk).username, f"{B_MARKER}-put")
        self.assertEqual(self._b_row(self.marker_b.pk).first_name, "")
        self.assertEqual(self._a_state(target_pk), before)

    def test_b_cannot_disable_a_target(self):
        """#5 单个启停：B 的会话停用 A-only 主键 ⇒ A 的 `is_active` 必须仍然为真。"""
        target_pk = self.a_only_pk
        self.assertTrue(self._a_row(target_pk).is_active)   # 夹具前提
        before = self._a_state(target_pk)
        body = self._b_call(lambda: self.client_b.post(
            f"/api/accounts/{target_pk}/status/",
            {"is_active": False},
            content_type="application/json",
        ).json())
        self.assertEqual(body["code"], BUSINESS, body)
        self.assertTrue(self._a_row(target_pk).is_active,
                        "B 的请求把 A 的账号停用了——租户上下文没绑在连接上")
        self.assertEqual(self._a_state(target_pk), before)

        # 正向对照：B 停用**自己**的标记账号必须生效。
        control = self._b_call(lambda: self.client_b.post(
            f"/api/accounts/{self.marker_b.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertFalse(self._b_row(self.marker_b.pk).is_active)
        self.assertTrue(self._a_row(target_pk).is_active)   # A 仍然未变

    def test_b_cannot_batch_disable_a_targets(self):
        """#6 批量启停：A-only 主键放进 `ids` ⇒ 整批被拒（存在性核对先于写库），A 行未变。"""
        target_pk = self.a_only_pk
        before = self._a_state(target_pk)
        body = self._b_call(lambda: self.client_b.post(
            "/api/accounts/status/",
            {"ids": [target_pk], "is_active": False},
            content_type="application/json",
        ).json())
        # `status_batch` 对「整批 id 都得解析到行」的核对落在当前 schema 上，
        # 所以 A-only 的主键在 B 里取不到 ⇒ 1000（`views.status_batch` 的那条）。
        self.assertEqual(body["code"], VALIDATION, body)
        self.assertTrue(self._a_row(target_pk).is_active)
        self.assertEqual(self._a_state(target_pk), before)

        # 正向对照：同一个批量端点、同一载荷形状，换成 B 自己的 id 就必须成功。
        control = self._b_call(lambda: self.client_b.post(
            "/api/accounts/status/",
            {"ids": [self.marker_b.pk], "is_active": False},
            content_type="application/json",
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertEqual(control["data"]["updated"], [self.marker_b.pk])
        self.assertFalse(self._b_row(self.marker_b.pk).is_active)
        self.assertEqual(self._a_state(target_pk), before)

    def test_b_cannot_batch_disable_a_targets_while_hitting_an_overlapping_pk(self):
        """#6 的**同号**形态：id 在两个 schema 里都有行 ⇒ 请求成功，但打到的是 B 自己那行。

        这是批量写路径上最刁的一格：不重叠的 id 让「B 把 A 的行改了」不可观测
        （A 里压根没那个号）。同号时两侧都可观测——若查询没绑当前 schema，
        请求会改到 A 的那一行（这里两侧初始 `is_active` 刻意不同，所以两个方向都可观测）。
        """
        overlap_pk = max(self.marker_a.pk, self.marker_b.pk, self.admin_b_pk) + 300
        User.objects.create_user(pk=overlap_pk, username=f"{A_MARKER}-overlap2",
                                 password="pw-123456")
        self._b_call(lambda: User.objects.create_user(
            pk=overlap_pk, username=f"{B_MARKER}-overlap2", password="pw-123456",
            # ⚠️ 必须显式给 `is_active=False`：`create_user` 的默认是 True，
            # 那样下面「B 的行被**启用**」这条断言在**不做任何事**时也成立
            # （实测：忘了这一步，用例红在 `assertFalse` 上）。
            is_active=False,
        ))
        # 自证前提：两侧同号、`is_active` **不同**（A 真、B 假）——串号时两个方向都可见。
        self.assertTrue(self._a_row(overlap_pk).is_active)
        self.assertFalse(self._b_row(overlap_pk).is_active)

        a_before = self._a_state(overlap_pk)
        body = self._b_call(lambda: self.client_b.post(
            "/api/accounts/status/",
            {"ids": [overlap_pk], "is_active": True},
            content_type="application/json",
        ).json())
        self.assertEqual(body["code"], OK, body)            # 重叠 id 在 B 里解析得到
        self.assertTrue(self._b_row(overlap_pk).is_active)  # B 的那行被**启用**
        self.assertEqual(self._b_row(overlap_pk).username, f"{B_MARKER}-overlap2")
        self.assertEqual(self._a_row(overlap_pk).username, f"{A_MARKER}-overlap2")
        self.assertEqual(self._a_state(overlap_pk), a_before)

    def test_b_cannot_reset_a_targets_password(self):
        """#7 重置密码：B 的会话重置 A-only 主键 ⇒ A 的口令列逐字节未变。

        断言打在**库里的哈希**上（`password` 列），不是响应码——
        `set_password()` 只改内存，这一列才是「真的落库了没有」的出口（E-0009）。
        """
        target_pk = self.a_only_pk
        before = self._a_state(target_pk)
        self.assertTrue(before["password"])                # 夹具前提：本来就有可用口令
        body = self._b_call(lambda: self.client_b.post(
            f"/api/accounts/{target_pk}/reset-password/",
            {},
            content_type="application/json",
        ).json())
        self.assertEqual(body["code"], BUSINESS, body)
        self.assertEqual(self._a_state(target_pk)["password"], before["password"],
                         "B 的重置请求改掉了 A 的口令哈希")

        # 正向对照：B 重置**自己**那行必须真的改掉口令哈希（同一个端点、同一载荷）。
        b_before = self._b_state(self.marker_b.pk)
        control = self._b_call(lambda: self.client_b.post(
            f"/api/accounts/{self.marker_b.pk}/reset-password/",
            {},
            content_type="application/json",
        ).json())
        self.assertEqual(control["code"], OK, control)
        self.assertNotEqual(self._b_state(self.marker_b.pk)["password"],
                            b_before["password"])
        self.assertEqual(self._a_state(target_pk)["password"], before["password"])

    # ── 第三类：跨租户外键（#2 新增）────────────────────────────────
    def test_b_cannot_submit_a_game_id(self):
        """#2 新增：B 提交 **A 的 game id** ⇒ `1000`，且**一行都不落库**。

        `AccountWriteSerializer.validate_games` 在当前 schema 里解析 game id，
        别的租户的 game 查不到。断言两条：响应码 + 数据库里没有半个账号
        （不是断言 msg）。
        """
        payload = {
            # R13：六个「必填，照抄」字段——正向对照那一半要走通，所以必须带齐。
            "username": "iso-cross-game",
            "display_name": "跨境游戏",
            "role": SERVICE,
            "games": [self.game_a.pk],
            "commission_value": "0.00",
            "deposit_amount": "0.00",
            "grab_limit_dl": 0,
            "grab_limit_pw": 0,
            "hired_on": "2026-01-02",
        }
        body = self._b_call(lambda: self.client_b.post(
            "/api/accounts/", payload, content_type="application/json"
        ).json())
        self.assertEqual(body["code"], VALIDATION, body)
        # 断言**数据库行**：不得留下半个账号（`auth_user` 与档案两张表都要看）。
        with tenant_context(self.tenant_b):
            self.assertFalse(User.objects.filter(username="iso-cross-game").exists())
            self.assertFalse(
                StaffProfile.objects.filter(user__username="iso-cross-game").exists()
            )
        connection.set_tenant(self.tenant)   # 见模块说明：别留下别的 schema 在连接上

        # 正向对照：同一个端点、同一载荷，把 game 换成 B 自己的 ⇒ 必须建号成功。
        game_b = self._b_call(lambda: Game.objects.create(name="仅 B 的游戏"))
        payload["games"] = [game_b.pk]
        control = self._b_call(lambda: self.client_b.post(
            "/api/accounts/", payload, content_type="application/json"
        ).json())
        self.assertEqual(control["code"], OK, control)
        with tenant_context(self.tenant_b):
            created = User.objects.get(username="iso-cross-game")
            self.assertEqual(
                list(created.staff_profile.games.values_list("pk", flat=True)),
                [game_b.pk],
            )
        connection.set_tenant(self.tenant)

    # ── 索引：防止将来漏写某条写路径 ───────────────────────────────
    def test_each_write_path_is_covered(self):
        """#2 #4 #5 #6 #7 各有对应的跨租户用例（本测试是索引，不是覆盖本身）。

        ⚠️ 如实口径：`hasattr` 只证明「方法名存在」。每一条的**判别力**由它自己的
        正向对照承担（上面每条都带一条）；本索引挡的是「新增写路径时忘了补用例」。
        """
        covered = {
            "create": "test_b_cannot_submit_a_game_id",
            "update_patch": "test_b_cannot_update_a_target",
            "update_put": "test_b_cannot_put_a_target",
            "status": "test_b_cannot_disable_a_target",
            "status_batch": "test_b_cannot_batch_disable_a_targets",
            "reset_password": "test_b_cannot_reset_a_targets_password",
        }
        missing = [name for name in covered.values() if not hasattr(self, name)]
        self.assertEqual(missing, [], f"缺少跨租户用例：{missing}")
        # 自证索引非空：`covered` 为空时上面那条断言会空转通过。
        self.assertTrue(covered)


class AccountPermissionNonSpillTests(TenantTestCase):
    """外溢的是判据，不是数据。

    租户管理员就是持「系统管理员」职位。`is_tenant_admin` 认这个职位，
    `_has_role` 对管理员无条件短路。本类钉的边界是：

    - **判据层**：职位持有者在单一口径下就是管理员，经管理员旁路拿到全部职位；
    - **数据层**：它的 Group 集合里仍然只有「系统管理员」——旁路是**运行时**的，
      没有把别的职位真的写进库里。

    ⚠️ 谓词用 `_has_role`（P5 `HasRole` 真正调用的那个内部判据）：`apps.core.permissions.has_role`
    是**工厂**，返回一个类，不是布尔值，没法直接 `assertFalse(has_role("客服主管"))`。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)     # R5：要 TENANT_ADMIN / SUPERVISOR 两个 Group 行。

    def setUp(self):
        self.user = User.objects.create_user(
            username="non-spill-admin", password="pw-123456"
        )
        self.user.groups.add(Group.objects.get(name=TENANT_ADMIN))
        # 真实登录：端点层那半必须也成立（不只是谓词好看）。
        self.client = Client(SERVER_NAME=DOMAIN_A)
        login = self.client.post(
            "/api/auth/login",
            data={"username": "non-spill-admin", "password": "pw-123456"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

    def test_admin_position_borrows_roles_in_the_predicate_but_not_in_the_data(self):
        # 构造前提：这个人**不是**超管，所以真值全由那个 Group 决定。
        self.assertFalse(self.user.is_superuser)
        # 正：账号管理判据为真，且端点层真的能读账号列表。
        self.assertTrue(is_account_admin(self.user))
        listing = self.client.get("/api/accounts/")
        self.assertEqual(listing.status_code, 200, listing.content[:200])
        self.assertEqual(listing.json()["code"], OK)
        # 判据层（**口径变更**）：持该职位就是管理员，`_has_role` 对它短路为真。
        self.assertIs(is_tenant_admin(self.user), True)
        self.assertIs(_has_role(self.user, SUPERVISOR), True)
        # 数据层（本轮真正**没有**外溢的那一半）：Group 集合里只有「系统管理员」。
        self.assertEqual(
            list(self.user.groups.values_list("name", flat=True)), [TENANT_ADMIN]
        )
        # 正向对照：给**同一个**账号真的加上「客服主管」后集合才变两个——
        # 否则上面那条"只有一个"可能由"这个夹具压根加不进任何组"凑出来。
        self.user.groups.add(Group.objects.get(name=SUPERVISOR))
        self.assertEqual(
            sorted(self.user.groups.values_list("name", flat=True)),
            sorted([TENANT_ADMIN, SUPERVISOR]),
        )
        self.assertTrue(_has_role(self.user, SUPERVISOR))
