import os
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.business.roles import DEFAULT_ROLES, TENANT_ADMIN
from apps.platform.provisioning import provision_tenant, seed_tenant

# 平台（public 平面）域名。测试用常量，不碰生产域名。
# `settings.ALLOWED_HOSTS` 里已有通配 `.test.local`（见 .env.example），所以这个 Host 天然放行。
PLATFORM_DOMAIN = "admin.test.local"

# 管理命令要设的初始密码。不放进任何生产常量，只在本文件里用来断言"命令确实设进去了"。
NEW_PASSWORD = "p5-bootstrap-pw"


class SeedTenantTests(TenantTestCase):
    def test_seed_creates_default_roles_and_admin_without_password(self):
        tenant, _ = provision_tenant(schema_name="seed_a", name="A", domain="seed-a.test.local")
        with tenant_context(tenant):
            names = set(Group.objects.values_list("name", flat=True))
            for role in DEFAULT_ROLES:
                self.assertIn(role, names)
            admin = get_user_model().objects.get(username="admin")
            self.assertFalse(admin.has_usable_password())   # D12：不设密码

    def test_reseeding_does_not_reset_an_existing_admins_password(self):
        """重新 seed 不得覆盖管理员已设置的可用密码（`provisioning.py` 的 `if created` 护栏）。

        没有这条测试时，把 `if created:` 删成「每次 seed 都无条件 set_unusable_password()」
        全套测试依然全绿：上面那条只在一个全新 admin 上看 `has_usable_password()`，
        幂等测试只数行数，两者都看不见密码被冲掉。
        """
        tenant, _ = provision_tenant(schema_name="seed_c", name="C", domain="seed-c.test.local")
        user_model = get_user_model()

        with tenant_context(tenant):
            admin = user_model.objects.get(username="admin")
            admin.set_password("p5-temp")
            admin.save()

        seed_tenant(tenant)

        with tenant_context(tenant):
            admin = user_model.objects.get(username="admin")
            self.assertTrue(
                admin.check_password("p5-temp"),
                "重新 seed 把管理员已设置的密码覆盖了（`if created` 护栏失效）",
            )

    def test_seed_admin_has_superuser_staff_flags_and_tenant_admin_group(self):
        """`seed_tenant` 建的管理员：标志位与 Group 此前**零测试承重**。

        删掉 `provisioning.py:56` 的 `defaults={"is_staff": True, "is_superuser": True}`
        或 `:62` 的 `admin.groups.add(...)`，本波之前**整套测试照样全绿**——
        上面那条只看职位名与"无可用密码"，幂等测试只数行数。
        代价是实打实的：**2026-09-23 起**租户平面的管理员判据是「持该 Group」（不再是
        `is_superuser`），两条默认值里**任一条**一丢，初始管理员都会被所有要求具体职位的
        端点拒掉——一个连自己租户都进不去的管理员（`permissions.py::_has_role` 的管理员
        旁路失效）。所以下面两行**都要**断言：标志位（本行）与职位（`:74` 那行）。
        租户管理员认的是「系统管理员」职位，不是 `is_superuser`。两条都要断言。
        按**具体行**断言，与 `E-0003` 的构造物自证同一条纪律。
        """
        tenant, _ = provision_tenant(schema_name="seed_d", name="D", domain="seed-d.test.local")
        with tenant_context(tenant):
            admin = get_user_model().objects.get(username="admin")
            # `is_superuser`（本行）与「在『系统管理员』组里」（下一行）**都**要成立：
            # 前者是 `provisioning.py` 的 `defaults`，后者是 `admin.groups.add(...)`。
            self.assertTrue(admin.is_superuser, "该账号也应是超管（`defaults` 里那一位）")
            self.assertTrue(admin.is_staff)
            # 只应持有「系统管理员」这一个默认职位（构造物自证：多一个就会让
            # `RoleGateTests.test_tenant_admin_passes_without_holding_the_role` 失去判别力）。
            # ⚠️ 这一行同时是**新判据**的钉子：该账号**也在**「系统管理员」组里。
            self.assertEqual(list(admin.groups.values_list("name", flat=True)), [TENANT_ADMIN])

    def test_seed_is_idempotent(self):
        tenant, _ = provision_tenant(schema_name="seed_b", name="B", domain="seed-b.test.local")
        provision_tenant(schema_name="seed_b", name="B", domain="seed-b.test.local")
        seed_tenant(tenant)
        seed_tenant(tenant)
        with tenant_context(tenant):
            self.assertEqual(Group.objects.filter(name__in=DEFAULT_ROLES).count(), len(DEFAULT_ROLES))
            self.assertEqual(get_user_model().objects.filter(username="admin").count(), 1)


class PublicTenantBootstrapTests(TenantTestCase):
    """公共租户行 + 平台域名。

    这一条补的是 P1 留下的真实缺口：`TenantMainMiddleware` 只认 `platform.Domain`
    里有的 Host（查不到直接 404），而 `provision_tenant` 只建**租户**域名——
    所以平台管理员今天**根本没有登录入口**。
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        # 本类把 PLATFORM_DOMAIN 追加进了全局 ALLOWED_HOSTS（settings 是进程级对象，
        # 不会被测试事务回滚），必须自己摘掉，否则污染后跑的测试类。
        if PLATFORM_DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(PLATFORM_DOMAIN)

    def test_creates_public_tenant_row_and_domain_idempotently(self):
        from apps.platform.models import Domain, Tenant
        from apps.platform.bootstrap import ensure_public_tenant

        tenant, domain_obj = ensure_public_tenant(PLATFORM_DOMAIN)
        self.assertEqual(tenant.schema_name, "public")
        self.assertEqual(domain_obj.domain, PLATFORM_DOMAIN)
        self.assertTrue(domain_obj.is_primary)

        again_tenant, again_domain = ensure_public_tenant(PLATFORM_DOMAIN)
        self.assertEqual(again_tenant.pk, tenant.pk)
        self.assertEqual(again_domain.pk, domain_obj.pk)
        # 只断言 pk 相同还不够：`Tenant.objects.filter(...).first()` 撑不起唯一性——
        # 重复插入两行、只返回第一行，上面的 pk 断言照样绿。必须数行数。
        self.assertEqual(Tenant.objects.filter(schema_name="public").count(), 1)
        self.assertEqual(Domain.objects.filter(domain=PLATFORM_DOMAIN).count(), 1)

        # `provision_tenant` 明确拒绝 public（P1 的记录）：本函数走的必须是**另一条**路，
        # 且不得因为 public schema 已存在而误以为"建好了"。
        self.assertFalse(tenant.auto_create_schema)

    def test_platform_domain_constant_is_importable(self):
        """brief 把 `PLATFORM_DOMAIN` 定为模块级接口（供部署脚本/其他测试复用）。

        断言它等于本文件用的测试域名：没有这条时，常量被改成生产域名也没人发现，
        而下面的路由测试用的是本文件自己的 `PLATFORM_DOMAIN`，两边会**各走各的**。
        """
        from apps.platform.bootstrap import PLATFORM_DOMAIN as module_domain

        self.assertEqual(module_domain, PLATFORM_DOMAIN)

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_platform_domain_routes_to_public_schema(self):
        from apps.platform.bootstrap import ensure_public_tenant

        ensure_public_tenant(PLATFORM_DOMAIN)
        settings.ALLOWED_HOSTS += [PLATFORM_DOMAIN]
        response = Client(SERVER_NAME=PLATFORM_DOMAIN).get("/healthz")
        self.assertEqual(response.status_code, 200)
        # 断言**选中的 schema**：只看 200 的话，"解析到别的租户但恰好也能 200"同样会绿，
        # 而本条的题目正是 Host → public 平面。`TenantTestCase` 默认连接在 `test` schema 上，
        # 所以这个断言确实能区分（不是恒真的）。
        self.assertEqual(connection.schema_name, "public")


class PlatformPlaneLoginTests(TenantTestCase):
    """验收标志 1 的**后半**：平台平面（`public`）真能登录，不只是"有 Host"。

    `PublicTenantBootstrapTests` 只证明平台域名能被路由到 `public`（`/healthz` → 200）。
    照 `RUNBOOK.md` §2.1 做完前两步之后，平台端**仍然登不进去**——因为
    `seed_tenant` 只建**租户** admin、`set_tenant_password` 明确拒绝 `public`，
    而"平台管理员从哪来"没有任何建号路径（P5 spec §4.5 / §12 把它记成 P5 之后的缺口）。
    本类用**端到端**（真实 `POST /api/auth/login` + `GET /api/auth/me`）钉住：
    `public.auth_user` 里的超级用户在平台域名上能登进来、且 `identity_plane == "public"`。

    ⚠️ 建号走 Django 自带的 `createsuperuser`（就是 RUNBOOK §2.1 第 3 步给运维的那条命令），
    不直接 `create_superuser()`：本条的命题是"**照 RUNBOOK 做**，平台端真能登进去"，
    绕过命令就证不到那件事。密码从 `DJANGO_SUPERUSER_PASSWORD` 传入——那是
    `createsuperuser --noinput` 的**唯一**口子（`createsuperuser.py:196-201`），
    生产里该命令是交互式 `getpass`，密码同样不进 argv。
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if PLATFORM_DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(PLATFORM_DOMAIN)

    def setUp(self):
        super().setUp()
        # 平台平面必须先有 Host（TenantMainMiddleware 查不到 Domain 直接 404）。
        # 放 `setUp`（测试事务内）而不是 `setUpClass`：后者会被提交并活过整个进程，
        # 打红 `test_provisioning.py` 里"public 租户行不存在"的断言（见
        # `test_p5_credential_plane.CredentialPlaneTests.setUp` 的同类注释）。
        from apps.platform.bootstrap import ensure_public_tenant

        ensure_public_tenant(PLATFORM_DOMAIN)

    def _create_platform_superuser(self, username, password):
        """在 `public` 平面用 Django 自带命令建平台超级用户（照 RUNBOOK 第 3 步）。"""
        with mock.patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": password}):
            with schema_context("public"):
                call_command(
                    "createsuperuser",
                    username=username,
                    # `User.email` 是 `REQUIRED_FIELDS` 里的字段；非交互模式下空串是
                    # 合法的"不填"（`createsuperuser.py:221-231`，`email` 是 blank=True）。
                    email="",
                    interactive=False,
                    verbosity=0,
                )

    def test_platform_superuser_created_in_public_can_log_in_end_to_end(self):
        username = "platform-admin"
        password = "pw-platform-admin"
        self._create_platform_superuser(username, password)

        # 建号落点先自证：账号在 `public` 平面（不在任何租户 schema），且是超级用户。
        with schema_context("public"):
            self.assertEqual(
                list(
                    get_user_model()
                    .objects.filter(username=username)
                    .values_list("username", "is_superuser")
                ),
                [(username, True)],
            )

        # D15 的真实流程：先取 csrftoken cookie，再带 X-CSRFToken 头登录。
        client = Client(SERVER_NAME=PLATFORM_DOMAIN, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/auth/login",
            data={"username": username, "password": password},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        payload = response.json()["data"]["user"]
        self.assertEqual(payload["username"], username)
        # 平面必须是 `public`——这条把"平台域名被路由到某个租户"这种串号形态挡在外面。
        self.assertEqual(payload["identity_plane"], "public")
        self.assertTrue(payload["is_tenant_admin"])

        # `/me` 用**同一条会话**复述同一个身份：登录成功但请求立刻被守卫判成跨平面
        # （spec §4.3 的"登录成功但立刻 401"）会在这里变红。
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.content[:200])
        self.assertEqual(me.json()["data"]["user"], payload)


class SetTenantPasswordCommandTests(TenantTestCase):
    """`set_tenant_password`（D12）：初始管理员由 `seed_tenant` 建成**无密码**，
    首次密码只能这样交互式单独设置。

    职责边界：本命令只改**已存在**用户的密码，不建用户、不建租户。
    """

    TENANT_DOMAIN = "pwboot-a.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ **绝不覆盖基类的 `cls.tenant` / `cls.domain`**（见 `docs/ERRORS.md` E-0015）。
        # `TenantTestCase.tearDownClass` 正是**通过这两个属性**收拾它自己建的共享
        # `test` 租户与 schema（`django_tenants/test/cases.py:49-56`：
        # `cls.domain.delete()` + `cls.tenant.delete(force_drop=True)`，后者连带
        # `DROP SCHEMA test CASCADE`）。子类一旦覆盖，基类删掉的就变成这里新建的
        # `pwboot_a`，而共享 `test` 的**租户行与 schema 双双留下**——下一个
        # `TenantTestCase` 子类用同名 `schema_name='test'` 再插一次，撞
        # `platform_tenant_schema_name_key` 报 `IntegrityError`，**故障落在别人无辜的
        # 测试类上**；更隐蔽的一半是留下的 schema 让 `create_schema(check_if_exists=True)`
        # 短路，**共享 schema 再也不会被重建**（陈旧 schema 被反复复用）。
        # 也别写自定义 `tearDownClass` 去"删掉遗留的 `test` 行"：那只是把 schema 复用藏起来
        # （删掉行后 INSERT 不再撞唯一约束，故障消失，但陈旧 schema 照旧被复用）。
        # 存进不同名字的属性，基类就能按原样收拾，本类的额外租户不归它管。
        cls.pw_tenant, cls.pw_domain = provision_tenant(
            schema_name="pwboot_a", name="BootstrapA", domain=cls.TENANT_DOMAIN
        )

    def tearDown(self):
        # 每个用例后把 admin 恢复到"无可用密码"，与 `seed_tenant` 的出厂状态一致：
        # 不然"设密码"用例会把可用密码留给下一个用例，失败原因变得难归因。
        super().tearDown()
        with tenant_context(self.pw_tenant):
            admin = get_user_model().objects.get(username="admin")
            admin.set_unusable_password()
            admin.save(update_fields=["password"])

    def _run_and_capture_error(self, **kwargs):
        """跑命令并返回它抛出的 `CommandError`。

        刻意**不用**裸的 `assertRaises(CommandError)`：命令还不存在时
        `call_command` 抛的也是 `CommandError("Unknown command")`，
        那种用例会以**错误的理由**变绿（E-0003 的教训：症状可能来自别处）。
        返回异常对象，让调用方断言 message，才能钉住"拒绝理由正确"。
        """
        with self.assertRaises(CommandError) as ctx:
            call_command("set_tenant_password", **kwargs)
        return ctx.exception

    def test_mismatched_prompts_are_rejected_and_write_nothing(self):
        with tenant_context(self.pw_tenant):
            admin = get_user_model().objects.get(username="admin")
            self.assertFalse(admin.has_usable_password(), "前置条件：出厂 admin 必须无可用密码")
            before = admin.password

        with mock.patch(
            "getpass.getpass", side_effect=["first-input", "second-input"]
        ):
            error = self._run_and_capture_error(schema="pwboot_a")
        self.assertIn("不一致", str(error))

        with tenant_context(self.pw_tenant):
            admin = get_user_model().objects.get(username="admin")
            self.assertEqual(admin.password, before, "两次输入不一致时不得写库")
            self.assertFalse(
                admin.has_usable_password(), "两次输入不一致时不得让密码变得可用"
            )

    def test_sets_password_and_never_echoes_it(self):
        """密码只允许以数据库里那份哈希存在：**输出与日志都不得出现明文**。

        命令今天不写日志，但"不落日志"是 brief 的硬承诺，必须有东西钉住它——
        只截 stdout/stderr 的话，将来谁加一句 `logger.info("pw=%s", first)` 全套依然绿。
        这里把 stdlib Logger 的**所有**输出入口打桩（`log` 也打，挡住直调
        `logger.log(level, ...)`），再断言没有任何一次调用的实参里出现密码。
        """
        import io
        import logging
        from contextlib import ExitStack, redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        # `getpass` 换成测试替身：**只**从 stdin 那一遍借位，绝不把密码写进 argv
        # （命令行参数会进 shell history / 进程表，brief 明确禁止）。
        with ExitStack() as stack:
            log_mocks = {
                level: stack.enter_context(mock.patch.object(logging.Logger, level))
                for level in ("debug", "info", "warning", "error", "critical", "log")
            }
            stack.enter_context(
                mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD])
            )
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            call_command("set_tenant_password", schema="pwboot_a")

        with tenant_context(self.pw_tenant):
            admin = get_user_model().objects.get(username="admin")
            self.assertTrue(admin.has_usable_password())
            self.assertTrue(
                admin.check_password(NEW_PASSWORD), "命令没有把密码真正写进该租户的 admin"
            )

        printed = out.getvalue() + err.getvalue()
        self.assertNotIn(NEW_PASSWORD, printed, "命令把密码打到输出里了")

        # 断言**整次调用的 repr**（含位置参数与关键字参数），而不是只查第一个参数：
        # `logger.info("设置 %s 的密码", NEW_PASSWORD)` 这种"密码在实参里、message 里没有"
        # 的写法同样会被抓到。
        for level, log_mock in log_mocks.items():
            for logged_call in log_mock.call_args_list:
                self.assertNotIn(
                    NEW_PASSWORD,
                    repr(logged_call),
                    f"命令把密码写进了 logging.{level}()",
                )

    def test_unknown_schema_is_rejected_without_writing(self):
        with mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
            error = self._run_and_capture_error(schema="pwboot_missing")
        self.assertIn("schema", str(error).lower())

    def test_invalid_schema_name_is_rejected_before_touching_db(self):
        """`--schema` 会进 `SET search_path`，非法名必须在**提示输密码之前**就挡住。

        用与 `provision_tenant` 同一把尺子（django-tenants 的 `is_valid_schema_name`）：
        `pg_` 前缀被 Postgres 保留，连"schema 不存在"都不该走到。

        ⚠️ 只断言 message 里含"合法"**钉不住"在碰 DB / 提问之前"**：守卫挪到两次
        `getpass` 之后照样以"合法"报错，用例依旧绿，而承诺的"先挡住"已经破了
        （E-0003 的教训：断言要盯行为，不能只盯措辞）。所以这里把两个副作用都钉死——
        **没有查库**（命令模块里的 `schema_exists` 未被调用）与**没有提示输密码**
        （`getpass.getpass` 未被调用）。
        """
        with mock.patch(
            "apps.platform.management.commands.set_tenant_password.schema_exists"
        ) as schema_exists_mock, mock.patch("getpass.getpass") as getpass_mock:
            error = self._run_and_capture_error(schema="pg_evil")

        self.assertIn("合法", str(error))
        schema_exists_mock.assert_not_called()
        getpass_mock.assert_not_called()

    def test_public_schema_is_not_a_valid_target(self):
        """public 平面**没有**登录用户（`seed_tenant` 只在租户 schema 里建 admin）。

        允许 `--schema public` 会把密码设到一个谁也登不进去的 schema 上，
        还会让"平台管理员"和"租户的管理员"在语义上搅在一起。
        """
        with mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
            error = self._run_and_capture_error(schema="public")
        self.assertIn("public", str(error))

    def test_unknown_username_is_rejected(self):
        with mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
            error = self._run_and_capture_error(schema="pwboot_a", username="nobody-here")
        self.assertIn("nobody-here", str(error))
