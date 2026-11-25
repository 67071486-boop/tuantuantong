"""平台平面的引导：让 `public` 有可用的 Host。

**这个模块补的是 P1 留下的一个真实缺口**：`TenantMainMiddleware` 对每个请求都查
`platform.Domain`，查不到直接返回 404；而 `provision_tenant` 只建**租户**域名
（且明确拒绝把 public 当租户 schema）。结果是平台平面（`public`）**没有 Host**，
平台管理员根本没有登录入口。

`provision_tenant` 走不了这条路，所以公共租户行只能在这里单独建。两条硬约束：

1. **必须在 `public` schema 内建**：`TenantMixin.save` 对新建租户会检查
   `connection.schema_name != get_public_schema_name()` 并抛
   `Can't create tenant outside the public schema`（`django_tenants/models.py:106-108`）。
2. **必须 `auto_create_schema = False`**：`Tenant` 模型类属性默认是 `True`
   （`apps/platform/models.py`），而 `TenantMixin.save` 对新建且
   `auto_create_schema` 为真的租户会走 `create_schema(check_if_exists=True)`
   （`django_tenants/models.py:116-118`）——public schema 早就存在，于是**静默短路**、
   什么都不建。看上去"成功"，实际什么都没发生（P1 在 `provisioning.py` 里记过这个陷阱）。
"""
from django.db import transaction
from django_tenants.utils import get_public_schema_name, schema_context

from apps.platform.models import Domain, Tenant

# 平台平面（public）的默认域名。部署时由调用方覆盖（管理命令 / 部署脚本传入实际域名）；
# 这里给的是测试与本地默认值，**不是**生产域名。
PLATFORM_DOMAIN = "admin.test.local"


class PublicTenantDomainConflictError(ValueError):
    """同一个域名已经指向了**另一个**租户。

    静默返回会给出「张冠李戴」的 `(tenant, domain)` 组合——调用方以为平台域名
    已经指向 `public`，实际指向某个租户，这就是租户串号的入口。必须显式失败
    （与 `provisioning.provision_tenant` 对域名冲突的处理同一口径）。
    """


@transaction.atomic
def ensure_public_tenant(domain, *, name="平台"):
    """确保 `public` 租户行与平台域名都存在，返回 `(tenant, domain_obj)`。

    幂等：重复调用不产生重复行（`Tenant.objects.filter(schema_name="public").count()`
    恒为 1，同一个 domain 同理）。已存在的行**不覆盖**——调用方可能已经改过 `name`。
    """
    public_schema = get_public_schema_name()

    # 目录表（Tenant/Domain）属于 public：必须显式切进去，否则 `Tenant.save()` 会抛
    # 「Can't create tenant outside the public schema」。调用方可能正处在某个租户
    # 上下文里（`TenantTestCase` 的默认连接就在 `test` schema 上），不能指望它。
    with schema_context(public_schema):
        tenant = Tenant.objects.filter(schema_name=public_schema).first()
        if tenant is None:
            tenant = Tenant(schema_name=public_schema, name=name)
            # 关键：置 False 后 save() 走 `elif is_new:` 分支，只发 `schema_needs_to_be_sync`
            # 信号（`django_tenants/models.py:125-127`），不会去 create_schema。
            # 若保留类默认的 True，会走 `create_schema(check_if_exists=True)`
            # （同文件 :116-118）——public 已存在则**静默短路返回 False**，
            # 看起来"成功"，实际只是白跑一趟。见模块 docstring。
            tenant.auto_create_schema = False
            tenant.save(verbosity=0)

        domain_obj = Domain.objects.filter(domain=domain).select_related("tenant").first()
        if domain_obj is not None:
            if domain_obj.tenant_id != tenant.pk:
                raise PublicTenantDomainConflictError(
                    f"域名 {domain!r} 已属于租户 "
                    f"{domain_obj.tenant.schema_name!r}，不能用作平台域名"
                )
        else:
            # 先查后建：`Domain.domain` 上有唯一约束，重复插入会抛 IntegrityError。
            domain_obj = Domain.objects.create(
                domain=domain, tenant=tenant, is_primary=True
            )

        return tenant, domain_obj
