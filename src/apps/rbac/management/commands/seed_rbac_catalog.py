"""给**既有**租户补灌权限目录与内置角色（新租户由 `seed_tenant` 自动灌）。

**为什么需要它**：`seed_tenant` 只在 `provision_tenant` 首次建租户时跑（`provisioning.py`
的 `if tenant is None` 分支），所以目录/内置角色的**变更**（改文案、Task 7 把某些点翻成
`available=True`）不会自己流到已开通的租户里——那些租户要跑这条命令补灌。

用法：`python manage.py seed_rbac_catalog`（遍历所有非 public 租户）。

幂等由 `seed_rbac()` 保证：重复执行不产生重复行，且**不重配已存在角色的权限**
（管理员改过的授权不会被回滚）。
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import tenant_context

from apps.platform.models import Tenant
from apps.rbac.seed import seed_rbac


class Command(BaseCommand):
    help = "对所有租户幂等补灌 RBAC 目录与内置角色"

    def handle(self, *args, **options):
        # 每个租户一个 schema：必须逐个 `tenant_context` 切进去，否则会写错平面。
        # `Tenant` 表住在 public，public 自己也在表里；`PermissionNode` / `Role` 是租户表
        # （`apps/rbac` 只进 `TENANT_APPS`），切进 public 会查无此表 ⇒ 排除它。
        for tenant in Tenant.objects.exclude(schema_name="public"):
            with tenant_context(tenant):
                created, updated = seed_rbac()
            self.stdout.write(
                f"{tenant.schema_name}: {created} created / {updated} updated"
            )
