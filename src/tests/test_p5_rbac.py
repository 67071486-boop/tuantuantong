"""RBAC 权限类、数据权限基线、非请求入口平面判定（spec §6 / §7.2 / D14）。

三块被测物（brief 的 Interfaces）：

- `apps/core/permissions.py`：`HasRole` / `HasPermission` / `is_tenant_admin`——
  端点级的**准入**判定；
- `apps/core/scoping.py`：`scope_queryset`——数据权限的**唯一卡点**，**默认不过滤**；
- `apps/core/plane.py`：`assert_plane`——**非请求入口**（管理命令 / 任务队列 / webhook）
  的平面判定。

本文件还补了 **Task 8 顺延过来的缺口**：`403` 有三个来源
（无权限 `PermissionDenied` / 本项目 `csrf_failure` / DRF `enforce_csrf`），
Task 8 只能落 CSRF 那半边（当时没有"要求职位"的端点，`HasRole` 是本文件的产物），
「已认证但无权限」那半边在这里闭合（spec §5.1-11）。

⚠️ 三条必须照做的裁决（与 Task 7/8 的同类注释互为索引）：

1. **每个 `Client` 都带 `enforce_csrf_checks=True`**：`Client` 默认把
   `request._dont_enforce_csrf_checks` 置 True（`django/test/client.py:183`），
   而 `CsrfViewMiddleware.process_view` 读同一个 flag
   （`django/middleware/csrf.py:427`）——默认 client 下每条 CSRF 断言都**恒真**。
2. **`@override_settings` 一律放方法上**：`TenantTestCase.setUpClass`
   （`django_tenants/test/cases.py`）**不调** `super().setUpClass()`，类级 override
   在 `SimpleTestCase.setUpClass` 里生效，于是被**静默丢弃**。
3. **额外租户绝不写 `cls.tenant` / `cls.domain`**：基类 `tearDownClass` 正是用后两个
   属性删它自己建的共享 `test` 租户，覆盖它就是 **E-0015**（共享 schema 留在库里，
   下一个子类撞 `duplicate key ... platform_tenant_schema_name_key`）。
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.platform.provisioning import provision_tenant

DOMAIN_A = "rbac-a.test.local"
DOMAIN_SCOPE = "rbac-scope.test.local"
DOMAIN_PERM = "rbac-perm.test.local"


class RoleGateTests(TenantTestCase):
    """两个方向都要测：有权限 → 200；无权限 → 403（spec §6.2）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase.setUpClass` 会 `connection.set_tenant(cls.tenant)`，
        # 所以额外租户在这里必须经 `provision_tenant`（它自己切 public）。
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_A,) if d not in settings.ALLOWED_HOSTS
        ]
        # ⚠️ 用**不同名字**的属性：见模块 docstring 第 3 条（E-0015）。
        cls.rbac_tenant, _ = provision_tenant(
            schema_name="rbac_a", name="A", domain=DOMAIN_A
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if DOMAIN_A in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN_A)

    def _login_client(self, username):
        """走真实登录接口，拿到带 plane 的会话（不要手工拼 session——那样测不到登录路径）。

        用户名第一次用时建号并设密码：`get_or_create` 之后**无条件**
        `set_password` + `save`，否则第二次调用拿到的是一个与本次密码不匹配的行
        （同一 schema 内同名会撞唯一约束，没法再 create 一个）。
        """
        with tenant_context(self.rbac_tenant):
            user, _ = get_user_model().objects.get_or_create(username=username)
            user.set_password("pw")
            user.save()
        client = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/auth/login",
            data={"username": username, "password": "pw"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        return client, token

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_user_without_role_gets_403(self):
        client, _ = self._login_client("no-role")
        response = client.get("/__probe__/service-only")
        self.assertEqual(response.status_code, 403, response.content[:200])

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_no_permission_403_is_distinguishable_from_csrf_403(self):
        """403 有三个来源，必须能区分（spec §4.6 / §5.1-11）。

        ⚠️ 这条是 **Task 8 顺延过来的缺口**：Task 8 只能落 CSRF 那半边
        （当时没有"要求职位"的端点，而 `HasRole` 是本任务的产物）。
        判据不是状态码（两处都是 403），而是 **msg**：无权限 msg 不得含 "CSRF"。
        """
        no_role_client, _ = self._login_client("no-role-403")
        no_role_response = no_role_client.get("/__probe__/service-only")
        self.assertEqual(no_role_response.status_code, 403, no_role_response.content[:200])
        no_role_msg = no_role_response.json()["msg"]

        csrf_client, _ = self._login_client("csrf-403")
        csrf_response = csrf_client.post("/__probe__/write")   # 无 token
        self.assertEqual(csrf_response.status_code, 403, csrf_response.content[:200])
        csrf_msg = csrf_response.json()["msg"]

        self.assertNotEqual(no_role_msg, csrf_msg, "无权限 403 与 CSRF 403 的 msg 不可区分")
        self.assertNotIn("CSRF", no_role_msg)
        # 对照半边（§5.2-4）：CSRF 那条**必须**含 "CSRF"，否则"不相等"可能只是因为
        # 两条都各自写着别的句子——那样本方法证明不了"来源可区分"。
        self.assertIn("CSRF", csrf_msg)

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_user_with_role_gets_200(self):
        client, _ = self._login_client("with-role")
        with tenant_context(self.rbac_tenant):
            from django.contrib.auth.models import Group

            from apps.business.roles import SERVICE

            user = get_user_model().objects.get(username="with-role")
            user.groups.add(Group.objects.get(name=SERVICE))
        response = client.get("/__probe__/service-only")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json(), {"ok": True})

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_user_with_a_different_role_gets_403(self):
        """**按职位名**匹配的判别物：持有别的职位 ≠ 持有「客服」（spec §6.2）。

        为什么上面两条合起来钉不住这条语义：`test_user_without_role_gets_403` 的用户
        属于**零个** Group，`test_user_with_role_gets_200` 的用户恰好在「客服」——
        两者都满足「有没有 Group ⟺ 有没有权限」，于是把实现里
        `_has_role` 的 `user.groups.filter(name=name).exists()` 换成
        `user.groups.exists()`（丢掉了 `name=name` 这个判据），那两条**照样全绿**
        （实测 m6：变异下 `Ran 3 tests ... OK`）。

        只有「在 Group 里、但**不是**被要求的那个」才能把两种实现分开：
        正确实现 403（账号只有「代练员」，不在「客服」里），
        `groups.exists()` 变异 200 → 变红。
        取值用 `apps/business/roles.WORKER`（`代练员`），**不是** `SERVICE`。
        """
        client, _ = self._login_client("worker-role")
        with tenant_context(self.rbac_tenant):
            from django.contrib.auth.models import Group

            from apps.business.roles import SERVICE, WORKER

            user = get_user_model().objects.get(username="worker-role")
            user.groups.add(Group.objects.get(name=WORKER))
            # 构造前提显式断言：若该账号碰巧也在「客服」里，本方法就证不了「按名匹配」
            # （E-0003 的教训：构造失败会让断言失去判别力）。
            self.assertNotIn(
                SERVICE,
                user.groups.values_list("name", flat=True),
                "构造失败：该账号也持有客服职位，无法区分按名匹配与『有任意 Group』",
            )
        response = client.get("/__probe__/service-only")
        self.assertEqual(response.status_code, 403, response.content[:200])

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_tenant_admin_passes_without_holding_the_role(self):
        """租户的管理员**不持有**「客服」职位也放行（spec §6.1 的管理员旁路）。

        这条补的是上面那条的**另一半**：`test_user_with_role_gets_200` 里那个用户
        **真的**在「客服」Group 里，所以它证不了管理员旁路——
        把 `_has_role` 的管理员分支整个删掉，它照样绿。
        而这条的构造物（`provision_tenant` 灌的无密码 `admin`）**只在**
        「系统管理员」这一个 Group 里（`provisioning.py::seed_tenant`），
        旁路一旦失效它立刻变 403。

        现实里为什么必须有这条旁路：租户开通时的初始管理员账号只有那一个职位，
        没有它，管理员会被所有要求具体职位的端点拒掉——一个连自己租户都进不去的管理员。
        """
        client, _ = self._login_client("admin")
        with tenant_context(self.rbac_tenant):
            from django.contrib.auth.models import Group

            from apps.business.roles import SERVICE

            admin = get_user_model().objects.get(username="admin")
            # 构造前提必须**显式**断言，否则"管理员旁路"可能只是"这个账号恰好有客服职位"
            # 造成的假通过（E-0003 的教训：构造失败会让断言失去判别力）。
            self.assertTrue(admin.is_superuser)
            self.assertNotIn(
                SERVICE,
                admin.groups.values_list("name", flat=True),
                "构造失败：admin 若持有客服职位，本方法就证不了管理员旁路",
            )
        response = client.get("/__probe__/service-only")
        self.assertEqual(response.status_code, 200, response.content[:200])


class PermissionClassTests(TenantTestCase):
    """`HasPermission` 的两个方向（Global Constraints：两个权限类都要双向被测）。

    走**真实端点**而不是在进程内手工调 `has_permission`：与 `HasRole` 的测法对齐
    （`200` 与 `403` 各自落到 HTTP 层），否则两个权限类会拿着两套不同强度的证据
    通过同一条约束。

    端点 `/__probe__/perm-only` 由本任务补进 `p5_probe_views.py`（`HasPermission`
    的 import 也必须在那个文件里——`apps/core/permissions.py` 到本任务才存在）。
    需要的 codename 是 `business.add_game`：`business` 是 `Game` 所在 app 的
    `AppConfig.label`（`apps/business/apps.py:6`），P5 不新增模型，借它的默认权限。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_PERM,) if d not in settings.ALLOWED_HOSTS
        ]
        # 独立 schema + 独立域名：本类必须能**单独运行**（不蹭别的测试类的租户）。
        cls.perm_tenant, _ = provision_tenant(
            schema_name="rbac_perm", name="Perm", domain=DOMAIN_PERM
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if DOMAIN_PERM in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN_PERM)

    def _login_client(self, username):
        client = Client(SERVER_NAME=DOMAIN_PERM, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/auth/login",
            data={"username": username, "password": "pw"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        return client

    def _make_user(self, username, *, with_permission, permission_codename="add_game"):
        with tenant_context(self.perm_tenant):
            from django.contrib.auth.models import Group, Permission

            user = get_user_model().objects.create_user(username=username, password="pw")
            if with_permission:
                # 走 Django 自己的授权模型拿权限（不手工塞 `_perm_cache`——
                # 手工塞等于用实现断言实现）。
                group = Group.objects.create(name=f"game-admins-{username}")
                group.permissions.add(Permission.objects.get(codename=permission_codename))
                user.groups.add(group)

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_has_permission_denies_user_without_the_codename(self):
        self._make_user("plain", with_permission=False)
        client = self._login_client("plain")
        response = client.get("/__probe__/perm-only")
        self.assertEqual(response.status_code, 403, response.content[:200])

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_has_permission_denies_user_holding_a_different_codename(self):
        """**按 codename** 匹配的判别物：持有别的权限 ≠ 持有被要求的那条（spec §6.2）。

        `test_has_permission_denies_user_without_the_codename` 的用户是**零权限**
        （`_make_user(with_permission=False)` 一个 Group 都不加），
        与「有权限 → 200」那条合起来同样只证明「有没有任何权限」：
        把 `HasPermission.has_permission` 的 `any(user.has_perm(c) ...)` 换成
        `bool(user.get_all_permissions())`（丢掉 `c` 这个判据），那两条**照样全绿**。

        只有「有权限、但**不是**被要求的那条」能分开两种实现：
        正确实现 403（本端点要 `business.add_game`），`get_all_permissions()` 变异 200 → 变红。

        构造物是 `business.add_project`（`Project` 在 `apps/business/models.py:82`，
        P5 不新增模型、借它的默认权限），**刻意不碰** `add_game`。
        """
        self._make_user(
            "other-perm", with_permission=True, permission_codename="add_project"
        )
        client = self._login_client("other-perm")
        response = client.get("/__probe__/perm-only")
        self.assertEqual(response.status_code, 403, response.content[:200])

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_has_permission_allows_user_who_holds_the_codename(self):
        self._make_user("granted", with_permission=True)
        client = self._login_client("granted")
        response = client.get("/__probe__/perm-only")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json(), {"ok": True})

    def test_tenant_schema_auth_permission_has_content(self):
        """spec §9「租户 schema 中 `auth_permission` **确实有内容**」直到本波才被显式断言。

        此前只有**隐式**兜住：`PermissionClassTests` 的用例都自己
        `Permission.objects.get(codename=...)`，一旦拿到 `DoesNotExist` 会以别的理由报错；
        而 §10 把这条列为"防配置漂移"的正面断言（评审实测 32 行）。
        漂移的后果很实：租户 schema 的迁移没跑全、或 `post_migrate` 的
        `create_permissions` 没生效时，`auth_permission` 会是空的，
        `HasPermission` 对所有非 superuser **一律 403**（spec §11-5 的后果），
        而这几个端点平时都有手工授权的用户兜着，未必有人发现。

        按**具体行 / 内容**断言，不用 `count()`（spec §5.2-3）：
        先断言 `business.add_game` / `business.add_project`（`HasPermission` 的两个测试判据）
        这两行真的在租户 schema 的权限表里。

        ⚠️ 阴性方向**不能**写成"public 平面没有 `business` 权限行"——实测那是错的：
        `INSTALLED_APPS` 含全部已安装 app（`SHARED_APPS` + 不在其中的 `TENANT_APPS`），
        `post_migrate` 会给**每个** schema 各灌一份权限行，所以 public 里同样有
        `business.*` 24 行（本波实测）。改用**只写在租户 schema 的探针行**证明查询是
        schema 键控的（与 `test_p5_envelope_csrf.SessionTablePlacementTests` 的阳性对照同一手法）。
        """
        from django.contrib.auth.models import Group, Permission
        from django.contrib.contenttypes.models import ContentType
        from django.db import connection

        with tenant_context(self.perm_tenant):
            # 断言**选中的 schema**（§5.2-4）：下面两次查询的落点由它决定。
            self.assertEqual(connection.schema_name, self.perm_tenant.schema_name)
            tenant_rows = set(
                Permission.objects.filter(
                    content_type__app_label="business"
                ).values_list("content_type__app_label", "codename")
            )
            # `add_game` / `add_project` 是 `HasPermission` 的测试判据（本类上面的用例）。
            self.assertIn(("business", "add_game"), tenant_rows)
            self.assertIn(("business", "add_project"), tenant_rows)
            # 阳性对照：只在租户 schema 里建一行探针权限（用 auth 的 ContentType，
            # 不依赖任何业务模型）。
            Permission.objects.create(
                content_type=ContentType.objects.get_for_model(Group),
                codename="p5-tenant-only-probe",
                name="p5 tenant-only probe",
            )
            self.assertEqual(
                list(
                    Permission.objects.filter(codename="p5-tenant-only-probe")
                    .values_list("codename", flat=True)
                ),
                ["p5-tenant-only-probe"],
            )

        with schema_context("public"):
            # 探针行**只**存在于租户 schema：证明上面的查询确实按 schema 键控，
            # 而不是"读到了一份全局共享的表"。
            self.assertEqual(
                list(
                    Permission.objects.filter(codename="p5-tenant-only-probe")
                    .values_list("codename", flat=True)
                ),
                [],
            )


class ScopeQuerysetTests(TenantTestCase):
    """三个方向：默认不过滤 / 注入过滤器才过滤 / 租户的管理员永不被过滤（spec §7.2）。

    自己 `provision_tenant` 一个租户（schema `rbac_scope`），**不蹭 RoleGateTests 的
    `rbac_tenant`**：跨测试类共享实例属性会让本类**无法单独运行**
    （`self.rbac_tenant` 在另一个类的 `setUpClass` 里才有），而"逐条单跑"是本轮验收项。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_SCOPE,) if d not in settings.ALLOWED_HOSTS
        ]
        cls.scope_tenant, _ = provision_tenant(
            schema_name="rbac_scope", name="Scope", domain=DOMAIN_SCOPE
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if DOMAIN_SCOPE in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN_SCOPE)

    def test_default_is_pass_through(self):
        """P5 不发明规则：不传 scope_filter 时原样返回（E-0006 的教训）。"""
        from apps.core.scoping import scope_queryset

        with tenant_context(self.scope_tenant):
            get_user_model().objects.create_user(username="u1", password="pw")
            get_user_model().objects.create_user(username="u2", password="pw")
            all_users = get_user_model().objects.all()
            user = get_user_model().objects.get(username="u1")
            self.assertEqual(
                set(scope_queryset(user, all_users).values_list("pk", flat=True)),
                set(all_users.values_list("pk", flat=True)),
            )

    def test_injected_scope_filter_is_applied(self):
        from apps.core.scoping import scope_queryset

        with tenant_context(self.scope_tenant):
            get_user_model().objects.create_user(username="u3", password="pw")
            get_user_model().objects.create_user(username="u4", password="pw")
            user = get_user_model().objects.get(username="u3")
            filtered = scope_queryset(
                user,
                get_user_model().objects.all(),
                scope_filter=lambda qs, u: qs.filter(pk=u.pk),
            )
            self.assertEqual(list(filtered.values_list("pk", flat=True)), [user.pk])

    def test_tenant_admin_is_never_filtered(self):
        from apps.core.scoping import scope_queryset

        with tenant_context(self.scope_tenant):
            admin = get_user_model().objects.create_superuser(username="adm", password="pw")
            # ⚠️ 判据统一：管理员 = 持「系统管理员」职位（不再看 `is_superuser`）。
            # 只置超管标志的账号**不是**管理员 ⇒ 会被过滤掉，本用例的命题（管理员不被过滤）
            # 必须靠职位构造。该 Group 由 `setUpClass` 的 `provision_tenant` 灌过。
            from django.contrib.auth.models import Group

            admin.groups.add(Group.objects.get(name="系统管理员"))
            get_user_model().objects.create_user(username="u5", password="pw")
            filtered = scope_queryset(
                admin,
                get_user_model().objects.all(),
                scope_filter=lambda qs, u: qs.filter(pk=u.pk),
            )
            # 按**具体行**断言（spec §5.2-3 的精神）。原来比的是 `count()`：
            #   - 数量相等挡不住"过滤掉一半、又混进别的行"这种**同数不同集**；
            #   - 也看不出到底**谁**在里面，"管理员没被过滤"这个命题落不到行上。
            # `admin` 与 `u5` 都必须原样在结果里：`admin` 证明旁路生效，
            # `u5` 证明"不过滤"是真的原样返回（而不是恰好只剩管理员一个人的假通过）。
            self.assertEqual(
                set(filtered.values_list("pk", flat=True)),
                set(get_user_model().objects.values_list("pk", flat=True)),
            )
            # 断言**选中的身份**（§5.2-4）：管理员本人与普通用户都必须原样在结果里。
            # `adm`（旁路生效）与 `u5`（真的被原样返回，而不是恰好只剩管理员）缺一不可。
            self.assertLessEqual(
                {"adm", "u5"},
                set(filtered.values_list("username", flat=True)),
            )


class PlaneHelperTests(SimpleTestCase):
    """`assert_plane`：非请求入口的平面判定（spec D14）。

    三条缺一不可：不符（非 None）→ 抛；**缺平面（None）→ 抛**；相符且非 None → 通过。
    中间那条是 D14 的核心——`identity_plane is None` 是"调用方没声明平面"，
    默认放行等于把"显式声明"变成可选，整个判定就白做了。

    ⚠️ **`test_missing_plane_raises` 为什么不写成 `assert_plane(None, "tenant_b")`**
    （计划与 brief 原来就是那么写的，实测**零判别力**，这是实施期抓到的计划缺陷）：

    判据是"这条例会不会因为**实现里删掉 `is None` 那一半**而变红"。把实现改成
    `if identity_plane != expected_schema:` 时，`assert_plane(None, "tenant_b")`
    仍然抛——`None != "tenant_b"` 本来就是 True，于是两条实现给出**完全相同**的
    可观测行为，那条例恒绿（实测：变异 m4 下 `Ran 1 test ... OK`）。
    None 与普通不符值要真的分开，**只有**让比较本身在缺平面时成立：
    `assert_plane(None, None)`——正确实现第一半命中（显式 None 检查）而抛；
    删掉第一半的实现落到 `None != None` → False → 放行。这才是"缺平面"的判别物。

    所以本类**刻意保留两条**：
    - `test_missing_plane_raises_even_when_comparison_would_pass`：抗变异的那条；
    - `test_missing_plane_raises`：向 brief 逐字对齐的那条（它证明的是另一半：
      缺平面**不得**因为"执行平面有值"而被当成普通不符之外的例外处理）。
    """

    def test_mismatched_plane_raises(self):
        from apps.core.plane import assert_plane

        with self.assertRaises(PermissionError):
            assert_plane("tenant_a", "tenant_b")

    def test_missing_plane_raises_even_when_comparison_would_pass(self):
        """抗变异的那条：**比较本身成立**（`None != None` 为 False）时仍必须抛。

        删掉实现里的 `identity_plane is None` 这一半 → 本方法变红（实测 m4）。
        """
        from apps.core.plane import assert_plane

        with self.assertRaises(PermissionError):
            assert_plane(None, None)

    def test_missing_plane_raises(self):
        """缺平面 + 执行平面有值：同样必须抛（brief 逐字对齐的那半边）。"""
        from apps.core.plane import assert_plane

        with self.assertRaises(PermissionError):
            assert_plane(None, "tenant_b")

    def test_matching_plane_passes(self):
        from apps.core.plane import assert_plane

        assert_plane("tenant_a", "tenant_a")      # 不抛即通过
