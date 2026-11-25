"""`install_tenant`：在一个已建好但还空的库里装上一个租户。两步、顺序固定：① `migrate_schemas --schema public`——空库的 public 连平台表都还没有，不先迁移就没法写目录行（复用现成命令，不自己实现迁移）；
② `provision_tenant(...)`——建 `Tenant` 行（`save()` 自动建租户 schema 并跑租户迁移）→ `seed_tenant` 播种 → 建 `Domain` 行。⚠️ 本命令不建库、不建角色（集群级供给的职责）——这条分工正是它能在既有测试库里直接测的原因。
⚠️ 异常纪律：闸门与 `provision_tenant` 的 `ValueError` 都是预期失败 ⇒ 翻成带原因的 `CommandError`；⛔ 不接 `except Exception`——未预期异常按仓库规矩让它响亮冒泡，不许伪装成业务结果。"""
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, get_tenant_model

from apps.platform.identifiers import InvalidIdentifierError, validate_schema_name
from apps.platform.provisioning import provision_tenant

Tenant = get_tenant_model()


class Command(BaseCommand):
    help = "在一个已经建好、但还空的数据库里安装一个租户（先迁移 public，再供给租户）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            required=True,
            help="租户 schema 名（必须是合法标识符，且不能是 public）",
        )
        parser.add_argument("--name", required=True, help="租户名称")
        parser.add_argument("--domain", required=True, help="租户的独享域名")

    def handle(self, *args, **options):
        schema = options["schema"]
        name = options["name"]
        domain = options["domain"]

        # 闸门必须在任何 DB 动作之前：`--schema` 由使用者提供、会被用来派生 schema 名。
        # 走严格白名单——不是 django-tenants 那个放行 `'x"; DROP ...'` 的校验器；
        # `InvalidIdentifierError` 是 `ValueError` 子类（预期失败）⇒ 翻成带原因的 `CommandError`。
        try:
            validate_schema_name(schema)
        except InvalidIdentifierError as exc:
            raise CommandError(str(exc)) from exc

        # 步骤 1：`public` 是 django-tenants 的平台平面名。**不写字面量**，走
        # `get_public_schema_name()`——将来若覆盖了 PUBLIC_SCHEMA_NAME，这里跟着走。
        # verbosity=0：迁移日志绕过外层命令的 -v 开关直写输出，会污染调用方的输出。
        call_command(
            "migrate_schemas",
            schema_name=get_public_schema_name(),
            interactive=False,
            verbosity=0,
        )

        # 摘要要说清「这次是新建还是已存在」，所以先看目录里有没有这一行。
        # ⚠️ 必须在步骤 1 **之后**：空库上 `Tenant` 表还不存在，先查会
        # "relation does not exist"——那正是步骤 1 存在的理由。
        already_installed = Tenant.objects.filter(schema_name=schema).exists()

        # 步骤 2：`provision_tenant` 自己幂等。它的 `ValueError`（如域名已被另一个
        # 租户占用）同样是**预期**失败 ⇒ 一并翻成 `CommandError`；
        # 其余异常不接，照原样冒泡。
        try:
            tenant, domain_obj = provision_tenant(
                schema_name=schema, name=name, domain=domain
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"已安装租户 schema={tenant.schema_name} 域名={domain_obj.domain}"
                f"（{'已存在' if already_installed else '新建'}）"
            )
        )
