"""App 建号来源及网页会话的登录与权限回归。"""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile
from apps.business.models import Game
from apps.platform.provisioning import seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

DOMAIN = "tenant.test.com"

REGISTER_URL = "/api/app/auth/register/"
APP_LOGIN_URL = "/api/app/auth/login/"
WEB_LOGIN_URL = "/api/auth/login"
ACCOUNTS_URL = "/api/accounts/"
ME_URL = "/api/auth/me"

#: 迁移模块名（**不是** `from ... import`：模块名以数字开头，只能用 importlib 载入）。
MIGRATION_MODULE = "apps.accounts.migrations.0023_staffprofile_account_source"

#: 外部契约的**字面副本**（⛔ 不 import 视图/码表常量：被改坏时它们会跟着一起变）。
OK = 0
#: ⚠️ 网页登录口的**认证层**失败是 `HTTP 401 + code 401`（`_login_failed`），
#: 不是 `/api/app/auth/login/` 那种「HTTP 200 + 1000」的业务失败——两条口子的分层不同。
AUTH_FAILED = 401
GENERIC_FAIL_MSG = "用户名或密码错误"

#: 合成账号 / 口令（⛔ 不是任何真实账号）。
MOBILE = "13800004444"
PASSWORD = "pw-app-123456"


class _Base(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase` 只建空 schema：注册端点要「代练员」Group、控制台建号要「人事」Role，
        # 两者都由 `seed_tenant`（`seed_rbac`）灌入。⚠️ 它**不预置游戏**（`DEFAULT_GAMES = ()`）。
        seed_tenant(cls.tenant)

    def setUp(self):
        # 跨域名请求会把连接留到别的 schema（`TenantMainMiddleware` 不还原）；本类不跨域名，
        # 但按同族用例的写法显式钉回，免得将来加一条对照用例时静默查错库。
        connection.set_tenant(self.tenant)
        # 注册限流桶在进程缓存里，不随事务回滚 ⇒ 每条用例从零开始。
        cache.clear()

    def tearDown(self):
        connection.set_tenant(self.tenant)
        cache.clear()
        super().tearDown()

    def _client(self):
        return Client(SERVER_NAME=DOMAIN)

    def _post_json(self, url, payload, *, client=None):
        return (client or self._client()).post(
            url, payload, content_type="application/json"
        )

    def _register_app_account(self, *, mobile=MOBILE, password=PASSWORD):
        """走**真实**注册端点建一个 App 账号（顺带钉住写入点）。"""
        response = self._post_json(REGISTER_URL, {"mobile": mobile, "password": password})
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], OK, body)
        return body


class AppRegisteredAccountWebLoginTests(_Base):
    """自助建号的打手可使用同一密码建立 App 和网页会话。"""

    def test_register_marks_the_profile_as_app_source(self):
        self._register_app_account()
        # 字面值 "app"（E-0024：不拿 AccountSource.APP 当期望值，枚举被改坏时这里要红）。
        self.assertEqual(
            StaffProfile.objects.get(user__username=MOBILE).account_source, "app"
        )

    def test_app_registered_worker_can_log_into_h5_with_worker_permissions(self):
        self._register_app_account()
        client = self._client()
        response = self._post_json(
            WEB_LOGIN_URL, {"username": MOBILE, "password": PASSWORD}, client=client
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], OK)
        me = client.get(ME_URL)
        self.assertEqual(me.status_code, 200, me.content[:300])
        user = me.json()["data"]["user"]
        self.assertEqual(user["username"], MOBILE)
        self.assertEqual(user["roles"], ["代练员"])
        self.assertFalse(user["is_tenant_admin"])

    def test_app_registered_account_can_still_log_into_the_app(self):
        self._register_app_account()
        response = self._post_json(
            APP_LOGIN_URL, {"username": MOBILE, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        self.assertTrue(body["data"]["token"])

    def test_wrong_password_on_an_app_account_keeps_the_generic_message(self):
        self._register_app_account()
        response = self._post_json(
            WEB_LOGIN_URL, {"username": MOBILE, "password": "not-the-password"}
        )
        self.assertEqual(response.status_code, AUTH_FAILED)
        wrong = response.json()
        self.assertEqual(wrong["code"], AUTH_FAILED, wrong)
        self.assertEqual(wrong["msg"], GENERIC_FAIL_MSG)

        right = self._post_json(
            WEB_LOGIN_URL, {"username": MOBILE, "password": PASSWORD}
        ).json()
        self.assertEqual(right["code"], OK, right)

    def test_link_and_profileless_accounts_are_not_blocked(self):
        """链接注册与没有档案的老账号也可登录。"""
        link_user = User.objects.create_user(username="link-user", password=PASSWORD)
        StaffProfile.objects.create(user=link_user, account_source=AccountSource.LINK)

        link_login = self._post_json(
            WEB_LOGIN_URL, {"username": "link-user", "password": PASSWORD}
        )
        self.assertEqual(link_login.status_code, 200, link_login.content[:300])
        self.assertEqual(link_login.json()["code"], OK)

        User.objects.create_user(username="no-profile-user", password=PASSWORD)
        bare_login = self._post_json(
            WEB_LOGIN_URL, {"username": "no-profile-user", "password": PASSWORD}
        )
        self.assertEqual(bare_login.status_code, 200, bare_login.content[:300])
        self.assertEqual(bare_login.json()["code"], OK)


class ConsoleCreatedAccountWebLoginTests(_Base):
    """网页端和后台建的账号照常登录。"""

    def test_account_created_through_the_console_can_log_in(self):
        admin = make_logged_in_client(username="console-admin", is_superuser=True)
        # `seed_tenant` **不预置游戏**（`DEFAULT_GAMES = ()`），这里显式建一款：账号线的
        # `games` 是新增必填，且至少一款（序列化器 `allow_empty=False`）。
        game = Game.objects.create(name="控制台建号测试游戏")

        created = admin.post(
            ACCOUNTS_URL,
            {
                "username": "console-created",
                "display_name": "控制台建号",
                "role": "人事",
                "games": [game.pk],
                # 新增路径的六个必填字段（`AccountWriteSerializer.CREATE_REQUIRED_FIELDS`），
                # 少一个就是 1000「该字段是必填项」——本条测的是登录判据，别让它被参数校验挡下。
                "commission_value": "0.00",
                "grab_limit_dl": 0,
                "grab_limit_pw": 0,
                "hired_on": "2026-10-21",
                "deposit_amount": "0.00",
            },
            content_type="application/json",
        )
        created_body = created.json()
        self.assertEqual(created_body["code"], OK, created_body)
        # 网页端建号 ⇒ 来源必须是 console（写入点的判据，字面值）。
        self.assertEqual(
            StaffProfile.objects.get(user__username="console-created").account_source,
            "console",
        )

        # 初始口令只在新增响应里出现一次（账号线口径），本测试就是它的消费者。
        password = created_body["data"]["initial_password"]
        login = self._post_json(
            WEB_LOGIN_URL, {"username": "console-created", "password": password}
        )
        self.assertEqual(login.status_code, 200, login.content[:300])
        self.assertEqual(login.json()["code"], OK)


class AppAccountSourceBackfillMigrationTests(_Base):
    """建号来源的历史数据迁移。"""

    @staticmethod
    def _migration():
        return importlib.import_module(MIGRATION_MODULE)

    def _legacy_rows(self):
        """造三条「迁移前」的旧行：来源都停在默认码上，只有 `opened_by` 不同。"""
        hr = User.objects.create_user(username="legacy-hr", password=PASSWORD)
        old_app_user = User.objects.create_user(username="legacy-app", password=PASSWORD)
        old_app = StaffProfile.objects.create(user=old_app_user, opened_by=old_app_user)
        old_link_user = User.objects.create_user(username="legacy-link", password=PASSWORD)
        old_link = StaffProfile.objects.create(user=old_link_user, opened_by=hr)
        old_console_user = User.objects.create_user(
            username="legacy-console", password=PASSWORD
        )
        old_console = StaffProfile.objects.create(user=old_console_user, opened_by=hr)
        return old_app, old_link, old_console

    def test_migration_literals_match_the_enum(self):
        """迁移文件刻意不 import 活代码 ⇒ 两处字面量必须由这条网对拍（改一边就红）。"""
        migration = self._migration()
        self.assertEqual(migration.APP, AccountSource.APP)
        self.assertEqual(migration.CONSOLE, AccountSource.CONSOLE)

    def test_backfill_marks_only_the_self_opened_accounts(self):
        old_app, old_link, old_console = self._legacy_rows()
        # 前置：三条旧行在回填前都停在默认值上（否则「回填成功」可能是别的原因凑出来的）。
        for profile in (old_app, old_link, old_console):
            self.assertEqual(profile.account_source, "console")

        self._migration().backfill_app_source(django_apps, None)

        for profile in (old_app, old_link, old_console):
            profile.refresh_from_db()
        self.assertEqual(old_app.account_source, "app")
        # 链接自助注册（开户人是人事）与后台建号（开户人是别人）都不许被误标。
        self.assertEqual(old_link.account_source, "console")
        self.assertEqual(old_console.account_source, "console")

    def test_reverse_migration_clears_app_back_to_the_default(self):
        old_app, old_link, old_console = self._legacy_rows()
        migration = self._migration()
        migration.backfill_app_source(django_apps, None)

        migration.clear_app_source(django_apps, None)

        for profile in (old_app, old_link, old_console):
            profile.refresh_from_db()
            # 反向操作把来源清回默认（迁移前这一列不存在 ⇒ 语义上全是默认值）。
            self.assertEqual(profile.account_source, "console")
