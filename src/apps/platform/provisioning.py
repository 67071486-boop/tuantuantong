"""租户供给：建 schema、跑迁移、写入目录与域名、灌基线数据。

幂等性要求（见 docs/ARCHITECTURE.md §5「开通流程」）：同一 schema_name 重复调用
不得报错、不得产生重复行。失败可重试。
"""
from django.db import transaction
from django_tenants.postgresql_backend.base import is_valid_schema_name
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
    tenant_context,
)

from apps.platform.identifiers import PLATFORM_SCHEMA_NAME

Tenant = get_tenant_model()
Domain = get_tenant_domain_model()

# 新租户的基线数据。**当前为空**。
# P1 时这里预置过一个占位的 GameItem；P2 换成真实的配置模型后不再预置——
# 租户的游戏与项目由租户自己维护，预置假数据只会污染配置表。
# 保留这个空元组作为扩展点（将来若要预置订单来源/标签等，在这里加）。
DEFAULT_GAMES = ()


def seed_tenant(tenant):
    """向租户 schema 写入基线数据。

    当前预置三样东西：默认职位（Group）、RBAC 权限目录与内置角色（spec §6.1）、
    初始管理员（`admin`，**无可用密码**）。游戏不做预置——租户的游戏与项目由租户自己维护。

    幂等：默认职位与初始管理员走 get_or_create / add，RBAC 种子自身的幂等由
    `apps.rbac.seed.seed_rbac()` 保证——重复调用不产生重复行。
    """
    # 延迟导入：平台层模块不在导入期加载租户层模型，避免迁移顺序耦合。
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    from apps.business.models import Game
    from apps.business.roles import DEFAULT_ROLES, TENANT_ADMIN

    with tenant_context(tenant):
        for index, name in enumerate(DEFAULT_GAMES):
            Game.objects.get_or_create(name=name, defaults={"sort_order": index})

        for role in DEFAULT_ROLES:
            Group.objects.get_or_create(name=role)

        # RBAC 目录与内置角色（spec §6.1）。延迟导入的理由同上面的模型导入：
        # 平台层不在导入期加载租户层模块。必须在 `tenant_context` 内调用——
        # `PermissionNode` / `Role` 是租户表，跑到 public 平面会写错 schema。
        from apps.rbac.seed import seed_rbac

        seed_rbac()

        # 初始管理员：**不设密码**（D12）。绝不允许把同一份 password 哈希灌给多个租户——
        # 那正是跨租户冒充的触发条件（spec §3.4）：一旦两个租户的 admin 行 password 字段
        # 逐字节相同，凭据就能在租户之间互换。`set_unusable_password()` 写一个永不可能
        # 被 `check_password` 接受的哈希，首次密码由管理命令交互式单独设置。
        # 只在新建时赋值 password：已存在的 admin（含人为改过密码的）不得被本函数重置。
        user_model = get_user_model()
        admin, created = user_model.objects.get_or_create(
            username="admin",
            defaults={"is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_unusable_password()
            admin.save(update_fields=["password"])

        admin.groups.add(Group.objects.get(name=TENANT_ADMIN))


@transaction.atomic
def provision_tenant(*, schema_name, name, domain):
    """开通一个租户。首次调用建 schema、跑迁移并灌基线数据；
    重复调用只在 Domain 缺失时补建域名——不补 schema、不补基线数据。
    """
    # 目录表（Tenant/Domain）属于 public：public 只放平台数据。
    # 这里必须显式切到 public 并在退出时还原，原因有二：
    # 1. django-tenants 的 Tenant.save() 会拒绝在非 public 连接上新建租户
    # （`Can't create tenant outside the public schema`，见其 models.py）；
    # 2. 调用方可能正处在某个租户上下文里——测试基类 `TenantTestCase.setUpClass`
    # 就会在 schema `test` 下调用本函数。
    with schema_context(get_public_schema_name()):
        # schema_name 必须在这里挡住，不能指望 save()：
        # django-tenants 的校验器放行 "public"，Django 的 validators 又不跑在 save() 上，
        # 而 create_schema(check_if_exists=True) 会因 public 已存在而短路——
        # 结果是跳过迁移、把租户业务数据写进共享 schema。
        if schema_name == get_public_schema_name():
            raise ValueError("不能把 public schema 用作租户 schema")
        # `platform` 是**平台库自己**的保留 schema 名（业务方 定，spec §7-Q2）：
        # 平台库与租户库的库名/角色名都是 `{TENANT_DB_PREFIX}{schema_name}` 派生的 ⇒ 放行它
        # 会出现「一个租户和平台库争同一个派生名 tt_platform」。
        # ⚠️ **只在这里加这一条判据，下面那把尺子（django-tenants 的 `is_valid_schema_name`）
        # 一个字节都不换**：`set_tenant_password` 与本函数的口径钉着「两处用同一把尺子」，
        # 换成 `validate_schema_name` 会让 `tests/test_p5_bootstrap.py` 的既有用例变红
        # （它放行/拒绝的集合与那把尺子不同）。`install_tenant` / `fleet.json` 那两条路走的是
        # `validate_schema_name`，由那一侧的保留名判据挡住（spec §7.2 判据 ①②）。
        if schema_name == PLATFORM_SCHEMA_NAME:
            raise ValueError(
                f"不能把 {PLATFORM_SCHEMA_NAME!r} 用作租户 schema"
                "（那是平台库自己的保留名）"
            )
        if not isinstance(schema_name, str) or not is_valid_schema_name(schema_name):
            raise ValueError(
                f"schema_name 不是合法的 Postgres schema 名：{schema_name!r}"
            )

        # 域名冲突**必须先查**：否则要等 tenant.save() 建完 schema、跑完迁移才失败，
        # 失败路径就依赖事务回滚 DDL——能回滚，但没必要走这一趟。
        domain_obj = Domain.objects.filter(domain=domain).first()
        if domain_obj is not None and domain_obj.tenant.schema_name != schema_name:
            # 域名已被**另一个**租户占用。静默返回会给出「张冠李戴」的
            # (tenant, domain) 组合，是租户串号的入口，必须显式失败。
            raise ValueError(
                f"域名 {domain!r} 已属于租户 {domain_obj.tenant.schema_name!r}，"
                f"不能分配给 {schema_name!r}"
            )

        tenant = Tenant.objects.filter(schema_name=schema_name).first()
        if tenant is None:
            # Tenant.save() 会因 auto_create_schema=True 自动建 schema 并执行迁移。
            # verbosity=0：迁移日志绕过测试运行器的 -v 开关直接写输出，会污染测试输出；
            # 且测试会开通多个租户，噪声成倍放大。
            tenant = Tenant(schema_name=schema_name, name=name)
            tenant.save(verbosity=0)
            seed_tenant(tenant)

        if domain_obj is None:
            domain_obj = Domain.objects.create(
                domain=domain, tenant=tenant, is_primary=True
            )

        return tenant, domain_obj
