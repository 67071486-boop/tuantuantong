from contextlib import contextmanager
import os
import unittest
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from apps.platform import provisioning
from tests.p5_auth_helpers import make_logged_in_client
from tests.support import fast_tenant


EXTRA_TENANTS = (
    {"schema_name": "reuse_extra_a", "name": "复用测试甲", "domain": "reuse-extra-a.test.local"},
    {"schema_name": "reuse_extra_b", "name": "复用测试乙", "domain": "reuse-extra-b.test.local"},
)
requires_schema_cache = unittest.skipUnless(fast_tenant.enabled(), "共享租户缓存已关闭，此例只验证缓存行为")


class _DeclaredFixture(TenantTestCase):
    extra_test_tenants = EXTRA_TENANTS

    @classmethod
    def get_test_schema_name(cls):
        return "reuse_extra_host"

    @classmethod
    def get_test_tenant_domain(cls):
        return "reuse-extra-host.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.a, _ = provision_tenant(**EXTRA_TENANTS[0])
        cls.b, _ = provision_tenant(**EXTRA_TENANTS[1])


@contextmanager
def fixture_class(**attributes):
    fixture = type("DeclaredFixture", (_DeclaredFixture,), attributes)
    try:
        fixture.setUpClass()
        yield fixture
    finally:
        fixture.tearDownClass()
        fixture.doClassCleanups()


def schema_oid(schema_name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT oid FROM pg_namespace WHERE nspname = %s", [schema_name])
        row = cursor.fetchone()
    return row[0] if row else None


class ExtraTenantReuseTests(TransactionTestCase):
    def clean_fixtures(self, *, preserve_cache=False):
        connection.set_schema_to_public()
        for name in ("reuse_extra_host", "reuse_extra_a", "reuse_extra_b"):
            if preserve_cache and name in fast_tenant._cache:
                continue
            fast_tenant._cache.pop(name, None)
            fast_tenant._delete_catalog_rows(name, "")
            for schema in (name, fast_tenant._hidden_name(name)):
                fast_tenant._drop_schema(schema)

    def tearDown(self):
        self.clean_fixtures(preserve_cache=True)
        super().tearDown()

    def run_fixture(self, fixture):
        result = unittest.TestResult()
        unittest.TestSuite([fixture("test_probe")]).run(result)
        return result

    def assert_catalogs_hidden(self):
        names = ["reuse_extra_host", *[item["schema_name"] for item in EXTRA_TENANTS]]
        self.assertFalse(fast_tenant._tenant_model().objects.filter(schema_name__in=names).exists())
        self.assertFalse(fast_tenant._domain_model().objects.filter(
            domain__in=["reuse-extra-host.test.local", *[item["domain"] for item in EXTRA_TENANTS]],
        ).exists())

    @requires_schema_cache
    def test_subclass_setup_failure_automatically_rolls_back_and_releases(self):
        class BrokenFixture(_DeclaredFixture):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                cls.original_oid = schema_oid(cls.a.schema_name)
                with tenant_context(cls.a):
                    Game.objects.create(name="失败类的数据")
                raise RuntimeError("合成子类初始化失败")

            def test_probe(self):
                pass

        result = self.run_fixture(BrokenFixture)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("合成子类初始化失败", result.errors[0][1])
        self.assert_catalogs_hidden()
        with fixture_class() as fixture:
            self.assertEqual(schema_oid(fixture.a.schema_name), BrokenFixture.original_oid)
            with tenant_context(fixture.a):
                self.assertFalse(Game.objects.filter(name="失败类的数据").exists())

    @requires_schema_cache
    def test_failed_extra_seed_rolls_back_creation_and_releases_previous_extra(self):
        self.clean_fixtures()
        original_seed = provisioning.seed_tenant
        first_oid = None

        def seed(tenant):
            nonlocal first_oid
            if tenant.schema_name == EXTRA_TENANTS[1]["schema_name"]:
                raise RuntimeError("合成种子初始化失败")
            original_seed(tenant)
            first_oid = schema_oid(tenant.schema_name)

        fixture = type("SeedFailureFixture", (_DeclaredFixture,), {"test_probe": lambda self: None})
        with mock.patch.object(provisioning, "seed_tenant", side_effect=seed):
            result = self.run_fixture(fixture)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("合成种子初始化失败", result.errors[0][1])
        self.assert_catalogs_hidden()
        self.assertEqual(schema_oid(fast_tenant._hidden_name("reuse_extra_a")), first_oid)
        self.assertIsNone(schema_oid("reuse_extra_b"))
        self.assertNotIn("reuse_extra_b", fast_tenant._cache)
        with fixture_class() as recovered:
            self.assertEqual(schema_oid(recovered.a.schema_name), first_oid)
            with tenant_context(recovered.b):
                self.assertFalse(get_user_model().objects.get(username="admin").has_usable_password())

    @requires_schema_cache
    def test_deleted_extra_cache_is_recreated_with_real_seed(self):
        with fixture_class() as first:
            first_oid = schema_oid(first.a.schema_name)
            other_oid = schema_oid(first.b.schema_name)
        fast_tenant._drop_schema(fast_tenant._hidden_name("reuse_extra_a"))
        with fixture_class() as second:
            self.assertNotEqual(schema_oid(second.a.schema_name), first_oid)
            self.assertEqual(schema_oid(second.b.schema_name), other_oid)
            with tenant_context(second.a):
                self.assertFalse(get_user_model().objects.get(username="admin").has_usable_password())

    @requires_schema_cache
    def test_invalid_declarations_and_cache_role_conflicts_do_not_touch_database(self):
        invalid = (
            ({**EXTRA_TENANTS[0], "schema_name": "reuse_extra_host"},),
            (EXTRA_TENANTS[0], EXTRA_TENANTS[0]),
            ({**EXTRA_TENANTS[0], "domain": "reuse-extra-host.test.local"},),
            ({"schema_name": "reuse_extra_a"},),
        )
        for definitions in invalid:
            fixture = type("InvalidFixture", (_DeclaredFixture,), {"extra_test_tenants": definitions})
            with self.subTest(definitions=definitions), CaptureQueriesContext(connection) as queries:
                with self.assertRaises(ValueError):
                    fixture.setUpClass()
                self.assertEqual(len(queries), 0)
        with fixture_class():
            pass
        auxiliary_as_main = type("AuxiliaryAsMain", (TenantTestCase,), {
            "get_test_schema_name": classmethod(lambda cls: "reuse_extra_a"),
        })
        original_oid = schema_oid(fast_tenant._hidden_name("reuse_extra_a"))
        with CaptureQueriesContext(connection) as queries:
            with self.assertRaises(ValueError):
                auxiliary_as_main.setUpClass()
            self.assertEqual(len(queries), 0)
        self.assertEqual(schema_oid(fast_tenant._hidden_name("reuse_extra_a")), original_oid)
        changed = type("ChangedFixture", (_DeclaredFixture,), {
            "extra_test_tenants": ({**EXTRA_TENANTS[0], "name": "另一个名称"}, EXTRA_TENANTS[1]),
            "test_probe": lambda self: None,
        })
        result = self.run_fixture(changed)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("声明不一致", result.errors[0][1])
        self.assertEqual(schema_oid(fast_tenant._hidden_name("reuse_extra_a")), original_oid)
        self.assert_catalogs_hidden()

    @requires_schema_cache
    def test_existing_extra_tenant_is_rejected_without_overwriting_its_data(self):
        self.clean_fixtures()
        existing, _ = provision_tenant(**EXTRA_TENANTS[0])
        original_oid = schema_oid(existing.schema_name)
        with tenant_context(existing):
            Game.objects.create(name="已有租户数据")
        fixture = type("ConflictingFixture", (_DeclaredFixture,), {"test_probe": lambda self: None})
        result = self.run_fixture(fixture)
        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("不能覆盖", result.errors[0][1])
        self.assertEqual(schema_oid(existing.schema_name), original_oid)
        self.assertTrue(fast_tenant._tenant_model().objects.filter(pk=existing.pk).exists())
        with tenant_context(existing):
            self.assertTrue(Game.objects.filter(name="已有租户数据").exists())

    def test_disabled_and_opt_out_keep_original_provisioning_behavior(self):
        self.clean_fixtures()
        for disabled in (True, False):
            with self.subTest(disabled=disabled), mock.patch.dict(
                os.environ, {fast_tenant.ENV_FLAG: "0" if disabled else "1"},
            ):
                with fixture_class(_fast_tenant_opt_out=not disabled) as fixture:
                    for name in ("reuse_extra_host", "reuse_extra_a", "reuse_extra_b"):
                        self.assertNotIn(name, fast_tenant._cache)
                    for tenant in (fixture.a, fixture.b):
                        self.assertIsNotNone(schema_oid(tenant.schema_name))
                        self.assertIsNone(schema_oid(fast_tenant._hidden_name(tenant.schema_name)))
                        with tenant_context(tenant):
                            self.assertFalse(get_user_model().objects.get(username="admin").has_usable_password())
                self.clean_fixtures()

    @requires_schema_cache
    def test_sequence_reset_failure_hides_restored_schema_and_cleans_catalogs(self):
        with fixture_class() as first:
            original_oid = schema_oid(first.a.schema_name)
        original_reset = fast_tenant._reset_sequences

        def reset(schema_name):
            if schema_name == EXTRA_TENANTS[0]["schema_name"]:
                raise RuntimeError("合成序列重置失败")
            return original_reset(schema_name)

        fixture = type("ResetFailureFixture", (_DeclaredFixture,), {"test_probe": lambda self: None})
        with mock.patch.object(fast_tenant, "_reset_sequences", side_effect=reset):
            result = self.run_fixture(fixture)

        self.assertEqual(len(result.errors), 1, result.errors)
        self.assertIn("合成序列重置失败", result.errors[0][1])
        self.assertIsNone(schema_oid(EXTRA_TENANTS[0]["schema_name"]))
        self.assertEqual(schema_oid(fast_tenant._hidden_name(EXTRA_TENANTS[0]["schema_name"])), original_oid)
        self.assertFalse(fast_tenant._tenant_model().objects.filter(
            schema_name__in=["reuse_extra_host", *[item["schema_name"] for item in EXTRA_TENANTS]],
        ).exists())

    @override_settings(ALLOWED_HOSTS=[item["domain"] for item in EXTRA_TENANTS])
    @requires_schema_cache
    def test_classes_reuse_structure_with_clean_data_sequences_and_credentials(self):
        with fixture_class() as first:
            first_oid = schema_oid(first.a.schema_name)
            with tenant_context(first.a):
                admin = get_user_model().objects.get(username="admin")
                admin_pk = admin.pk
                self.assertFalse(admin.has_usable_password())
                game = Game.objects.create(name="仅前类存在的游戏")
                first_game_pk = game.pk
                Order.objects.create(game=game, order_no="20261119000000008001", project_text="仅甲可见")
                client_a = make_logged_in_client(domain=EXTRA_TENANTS[0]["domain"], is_superuser=True)
            with tenant_context(first.b):
                game = Game.objects.create(name="乙自己的游戏")
                Order.objects.create(game=game, order_no="20261119000000008002", project_text="仅乙可见")
                client_b = make_logged_in_client(domain=EXTRA_TENANTS[1]["domain"], is_superuser=True)
            own_a = client_a.get("/api/business/orders/").json()
            own_b = client_b.get("/api/business/orders/").json()
            self.assertEqual((own_a["code"], own_b["code"]), (0, 0))
            self.assertIn("仅甲可见", str(own_a))
            self.assertIn("仅乙可见", str(own_b))
            moved = client_a.get("/api/business/orders/", SERVER_NAME=EXTRA_TENANTS[1]["domain"]).json()
            self.assertNotEqual(moved["code"], 0)
            self.assertNotIn("仅乙可见", str(moved))

        self.assertIsNone(schema_oid(EXTRA_TENANTS[0]["schema_name"]))
        self.assertEqual(schema_oid(fast_tenant._hidden_name(EXTRA_TENANTS[0]["schema_name"])), first_oid)
        tenant_model = fast_tenant._tenant_model()
        self.assertFalse(tenant_model.objects.filter(schema_name__in=[item["schema_name"] for item in EXTRA_TENANTS]).exists())

        with fixture_class() as second:
            self.assertEqual(schema_oid(second.a.schema_name), first_oid)
            with tenant_context(second.a):
                self.assertFalse(Game.objects.filter(name="仅前类存在的游戏").exists())
                self.assertFalse(Order.objects.filter(project_text="仅甲可见").exists())
                admin = get_user_model().objects.get(username="admin")
                self.assertEqual(admin.pk, admin_pk)
                self.assertFalse(admin.has_usable_password())
                self.assertEqual(Game.objects.create(name="下一类的游戏").pk, first_game_pk)
            with tenant_context(second.b):
                self.assertFalse(Order.objects.filter(project_text="仅乙可见").exists())
