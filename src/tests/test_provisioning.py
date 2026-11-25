from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_exists, tenant_context

from apps.business.models import Game
from apps.platform.models import Domain, Tenant
from apps.platform.provisioning import provision_tenant, seed_tenant


class ProvisioningTests(TenantTestCase):
    def test_provision_creates_tenant_domain_and_schema(self):
        tenant, domain = provision_tenant(
            schema_name="acme", name="ACME 工作室", domain="acme.test.local"
        )
        self.assertTrue(schema_exists("acme"))
        self.assertEqual(domain.domain, "acme.test.local")
        self.assertTrue(domain.is_primary)
        self.assertEqual(domain.tenant_id, tenant.id)

    def test_provision_is_idempotent(self):
        first, first_domain = provision_tenant(
            schema_name="acme", name="ACME 工作室", domain="acme.test.local"
        )
        second, second_domain = provision_tenant(
            schema_name="acme", name="ACME 工作室", domain="acme.test.local"
        )
        self.assertEqual(first.pk, second.pk)
        # 必须是**同一行**被返回，而不是删掉重建后计数恰好为 1
        self.assertEqual(first_domain.pk, second_domain.pk)
        self.assertEqual(Tenant.objects.filter(schema_name="acme").count(), 1)
        self.assertEqual(Domain.objects.filter(domain="acme.test.local").count(), 1)

    def test_seed_tenant_seeds_nothing_by_default(self):
        """新租户不预置任何游戏——配置由租户自己维护（P2 起）。

        P1 时这里预置过一个占位的 GameItem；换成真实配置模型后不再预置，
        以免假数据污染配置表。重复调用仍然安全。
        """
        tenant, _ = provision_tenant(
            schema_name="acme", name="ACME 工作室", domain="acme.test.local"
        )
        seed_tenant(tenant)
        seed_tenant(tenant)
        with tenant_context(tenant):
            self.assertFalse(Game.objects.exists())

    def test_provision_rejects_public_schema(self):
        """public 只放平台数据，绝不能当租户 schema 用。

        这条必须显式挡在 provisioning 里：django-tenants 的名字校验器放行 "public"，
        `create_schema(check_if_exists=True)` 又会因它已存在而短路，
        于是迁移被跳过、seed 直接写进共享 schema。
        """
        with self.assertRaises(ValueError):
            provision_tenant(
                schema_name="public", name="伪租户", domain="public.test.local"
            )
        self.assertFalse(Tenant.objects.filter(schema_name="public").exists())

    def test_provision_rejects_domain_owned_by_another_tenant(self):
        """域名被别的租户占用时必须显式失败，不能静默返回错配的组合。"""
        provision_tenant(schema_name="acme", name="ACME 工作室", domain="acme.test.local")
        with self.assertRaises(ValueError):
            provision_tenant(
                schema_name="rival", name="另一家", domain="acme.test.local"
            )
        self.assertFalse(Tenant.objects.filter(schema_name="rival").exists())
        # 失败路径不得留下孤儿 schema（守卫已提到 tenant.save() 之前，这里做回归守卫）
        self.assertFalse(schema_exists("rival"))
