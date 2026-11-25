"""`install_tenant` 命令：在一个**已经建好、但还空**的库里装上一个租户（Task 3）。

⚠️ **为什么本文件必须用 `TransactionTestCase`，而不是仓库惯用的 `TenantTestCase`**：
`TenantTestCase` 继承 `django.test.TestCase`，每条用例都被包在**外层事务**里
（`TestCase._fixture_setup` → `_enter_atomics`）。而本命令的第 2 步会跑 **DDL**：
`provision_tenant` 里的 `Tenant.save()` 会 `CREATE SCHEMA` 并执行租户迁移。
`TenantTestCase` 能这么用，是因为它的 schema 建在 `setUpClass`（事务之外）；
本文件要测的恰恰是「命令自己在用例里建 schema」，放进外层事务就变成了
「同一事务里跑了一遍」——那种绿不是生产形态的绿。所以这里自己搭 autocommit 的基类，
并在 `addClassCleanup` 里把探针 schema 与 `Tenant` / `Domain` 目录行一起清掉。

⚠️ **探针 schema 名一律带本会话片段**（从测试库名派生，见 `_session_segment()`）：并行会话
用同一个名字会互踩（本仓实测过，症状看起来像代码坏了）。这里虽然只建 schema（库内对象），
但 `provision_tenant` 会拿它写 `Tenant` / `Domain` 目录行，名字撞了同样会互相删。
探针域名同理——各用各的，避免撞 `Domain.domain` 的唯一约束。

⚠️ **清理为什么要连原始 `DROP SCHEMA` 一起做**：`TransactionTestCase` 每条用例后会
`flush` public 表，于是 `Tenant` / `Domain` 目录行可能**已经不在了**，而 schema 是 DDL、
`flush` 管不着 ⇒ 只靠目录行删不掉 schema。所以 `_purge` 两条路都走。
"""
import re
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TransactionTestCase
from django_tenants.utils import schema_context, schema_exists
from psycopg import sql

from apps.platform.models import Domain, Tenant
from apps.platform.provisioning import provision_tenant

User = get_user_model()


def _session_segment() -> str:
    """本会话的探针片段：从**本会话测试库名**派生（`settings.DATABASES["default"]["NAME"]`）。

    并行会话的测试库名不同（本仓约定 `POSTGRES_DB=<会话后缀>`）⇒ 探针 schema / 域名也各用各的，
    不会互删。清洗成合法标识符并截断。
    """
    raw = re.sub(r"[^a-z0-9_]", "_", str(settings.DATABASES["default"]["NAME"]).lower())
    segment = raw.strip("_") or "s"
    if segment[0].isdigit():
        segment = "s" + segment
    return segment[:32]


SEGMENT = _session_segment()

#: 探针 schema —— **必须**带本会话片段，理由见模块 docstring。
SCHEMA = f"install_probe_{SEGMENT}"
SCHEMA_IDEMPOTENT = f"install_idem_{SEGMENT}"
SCHEMA_SEEDED = f"install_seed_{SEGMENT}"
SCHEMA_OWNER = f"install_owner_{SEGMENT}"
SCHEMA_CONFLICT = f"install_conflict_{SEGMENT}"

DOMAIN = f"install-probe-{SEGMENT}.test.local"
DOMAIN_IDEMPOTENT = f"install-idem-{SEGMENT}.test.local"
DOMAIN_SEEDED = f"install-seed-{SEGMENT}.test.local"
DOMAIN_OWNER = f"install-owner-{SEGMENT}.test.local"

#: 闸门用例的输入。`public` 与注入口径的名字都是 `provision_tenant` 会拒的形状，
#: 命令必须把它们翻成 `CommandError` 而不是裸 traceback。
PUBLIC_SCHEMA = "public"
#: ⚠️ **第二条语句必须保持惰性**（`SELECT 1`，**不要**改成 `DROP DATABASE postgres`
#: 那种看起来更「有说服力」的串）：这条探针是**只在闸门坏掉时才真的跑起来**的 tripwire，
#: 而闸门一旦坏掉，本串会被一路送进 django-tenants 那句字符串拼接的
#: `CREATE SCHEMA "…"`（它只有 `^(?!pg_).{1,63}$` 一道正则，**放行**本串），
#: 且 psycopg3 单次 `execute` **会执行多条语句**（实测）⇒ 破坏性的第二条语句会在
#: **跑测试的时候**真的执行，也就是「修这条测试的人先删掉了开发机的 `postgres` 库」。
#: 注入形状的三件（引号逃逸 `"` + 语句分隔 `;` + 行注释 `--`）必须都在，否则 tripwire 就没了形状。
INJECTION_SCHEMA = 'x"; SELECT 1; --'

#: 每个探针名配一个清理函数（schema + 目录行）。
#: ⚠️ `PUBLIC_SCHEMA` / `INJECTION_SCHEMA` **故意不在**这张表里：它们只作为闸门输入，
#: 命令必须在建任何东西之前就拒掉——尤其 `public`，把它注册进清理函数会去删平台 schema。
PROBES = (
    (SCHEMA, DOMAIN),
    (SCHEMA_IDEMPOTENT, DOMAIN_IDEMPOTENT),
    (SCHEMA_SEEDED, DOMAIN_SEEDED),
    (SCHEMA_OWNER, DOMAIN_OWNER),
    (SCHEMA_CONFLICT, None),  # 兜底断言「别留下 schema」；它本身不该被建出来
)


def _purge(schema_name, domain):
    """删掉探针 schema 与它的目录行。幂等：没建过也不抛。

    两条路都走（理由见模块 docstring 最后一段）：先按目录行删（能连带 `force_drop`
    schema），再兜一次原始 `DROP SCHEMA`——`TransactionTestCase` 的 flush 可能已经把
    目录行清掉了，那时只有 schema 还在。
    """
    connection.set_schema_to_public()
    if domain is not None:
        Domain.objects.filter(domain=domain).delete()
    tenant = Tenant.objects.filter(schema_name=schema_name).first()
    if tenant is not None:
        # `Tenant.auto_drop_schema = False`（误删数据不可接受），删除路径必须显式 force_drop。
        tenant.delete(force_drop=True)
    if schema_exists(schema_name):
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


class InstallTenantCommandTests(TransactionTestCase):
    """命令的两步（先迁移 public，再 `provision_tenant`）、它的失败路径与「不吞异常」。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 注册在 `setUpClass` 里：即使后面某个用例把类打挂，清理照跑。
        for schema_name, domain in PROBES:
            cls.addClassCleanup(_purge, schema_name, domain)

    def test_install_creates_the_tenant_the_domain_and_the_schema(self):
        out = StringIO()
        call_command(
            "install_tenant",
            schema=SCHEMA,
            name="安装探针工作室",
            domain=DOMAIN,
            stdout=out,
        )

        tenant = Tenant.objects.get(schema_name=SCHEMA)
        self.assertEqual(tenant.name, "安装探针工作室")
        self.assertTrue(schema_exists(SCHEMA))

        domain_obj = Domain.objects.get(domain=DOMAIN)
        self.assertEqual(domain_obj.tenant_id, tenant.id)
        self.assertTrue(domain_obj.is_primary)

        # 结尾那行摘要要说清「装的是谁」，且首次安装标成「新建」。
        summary = out.getvalue()
        self.assertIn(SCHEMA, summary)
        self.assertIn(DOMAIN, summary)
        self.assertIn("新建", summary)

    def test_install_twice_is_idempotent(self):
        call_command(
            "install_tenant",
            schema=SCHEMA_IDEMPOTENT,
            name="幂等工作室",
            domain=DOMAIN_IDEMPOTENT,
            stdout=StringIO(),
        )
        second_run = StringIO()
        call_command(
            "install_tenant",
            schema=SCHEMA_IDEMPOTENT,
            name="幂等工作室",
            domain=DOMAIN_IDEMPOTENT,
            stdout=second_run,
        )

        self.assertTrue(schema_exists(SCHEMA_IDEMPOTENT))
        self.assertEqual(Tenant.objects.filter(schema_name=SCHEMA_IDEMPOTENT).count(), 1)
        self.assertEqual(Domain.objects.filter(domain=DOMAIN_IDEMPOTENT).count(), 1)
        # 第二次跑的是同一条路径，摘要必须改口说「已存在」——否则运维看不出这次有没有新建。
        self.assertIn("已存在", second_run.getvalue())

    def test_install_seeds_an_admin_that_has_no_usable_password(self):
        call_command(
            "install_tenant",
            schema=SCHEMA_SEEDED,
            name="播种工作室",
            domain=DOMAIN_SEEDED,
            stdout=StringIO(),
        )

        # 在**租户 schema 内**查：这样「admin 在不在正确的地方」也一起钉住了。
        with schema_context(SCHEMA_SEEDED):
            admin = User.objects.get(username="admin")
            # `seed_tenant` 的既有约定（D12）：初始管理员**无可用密码**，
            # 同一份哈希灌给多个租户正是跨租户冒充的触发条件。
            self.assertFalse(admin.has_usable_password())

    def test_public_schema_is_rejected_with_a_command_error(self):
        with mock.patch(
            "apps.platform.management.commands.install_tenant.call_command"
        ) as migrate:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "install_tenant",
                    schema=PUBLIC_SCHEMA,
                    name="伪租户",
                    domain="install-public-t3.test.local",
                )

        # 错误信息要带原因（哪个值、为什么不行），不是一句空的 CommandError。
        self.assertIn(PUBLIC_SCHEMA, str(ctx.exception))
        # 闸门在**任何 DB 动作之前**：public 的迁移这一步压根不该被调到。
        migrate.assert_not_called()
        self.assertFalse(Tenant.objects.filter(schema_name=PUBLIC_SCHEMA).exists())

    def test_injection_shaped_schema_name_is_rejected_with_a_command_error(self):
        with mock.patch(
            "apps.platform.management.commands.install_tenant.call_command"
        ) as migrate:
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "install_tenant",
                    schema=INJECTION_SCHEMA,
                    name="注入",
                    domain="install-inject-t3.test.local",
                )

        # 走的是严格白名单（django-tenants 那个校验器**放行**这个值）。
        # ⚠️ `INJECTION_SCHEMA` 的第二条语句刻意是惰性的 `SELECT 1`——本用例的**失败态
        # 本身**就是那条语句被执行（闸门坏 ⇒ 串进 `CREATE SCHEMA` ⇒ 多语句 execute），
        # 所以它必须无害。为什么不能改回 `DROP DATABASE …` 见常量处的注释。
        self.assertIn(INJECTION_SCHEMA, str(ctx.exception))
        migrate.assert_not_called()
        self.assertFalse(Tenant.objects.filter(schema_name=INJECTION_SCHEMA).exists())

    def test_domain_taken_by_another_tenant_becomes_a_command_error(self):
        # 这个形状**过得了**闸门（名字合法），只有 `provision_tenant` 会拒它
        # ⇒ 它专门钉「把 provision_tenant 的 ValueError 翻成 CommandError」这条翻译。
        provision_tenant(
            schema_name=SCHEMA_OWNER, name="先来的工作室", domain=DOMAIN_OWNER
        )

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "install_tenant",
                schema=SCHEMA_CONFLICT,
                name="后来的工作室",
                domain=DOMAIN_OWNER,
            )

        message = str(ctx.exception)
        self.assertIn(DOMAIN_OWNER, message)
        self.assertIn(SCHEMA_OWNER, message)
        # 拒绝发生在建行/建 schema 之前：两个都不该留下。
        self.assertFalse(Tenant.objects.filter(schema_name=SCHEMA_CONFLICT).exists())
        self.assertFalse(schema_exists(SCHEMA_CONFLICT))

    def test_unexpected_error_is_not_disguised_as_a_command_error(self):
        # 仓库规矩（E-0010 同族）：**预期**失败翻成 CommandError，**未预期**异常让它响亮地冒泡。
        # 有人把 except ValueError 写成 except Exception 时，这条必须变红。
        with mock.patch(
            "apps.platform.management.commands.install_tenant.provision_tenant",
            side_effect=RuntimeError("连接断了"),
        ):
            with self.assertRaises(RuntimeError):
                call_command(
                    "install_tenant",
                    schema=SCHEMA_CONFLICT,
                    name="意外",
                    domain=DOMAIN,
                )
