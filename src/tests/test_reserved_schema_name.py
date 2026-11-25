"""`platform` 是**平台库自己的保留名**，两条租户路径都必须拒它。

**本文件为什么存在（阶段 2）**：原先把这三条判据放在
`tests/test_platform_tenant_model.py` 里，而那个文件在阶段 2 随平台控制台一起**迁出主仓**。
但被判据守着的 `identifiers.py::validate_schema_name` 与 `provisioning.py::provision_tenant`
**留在主仓**（租户站与开通租户都要用）⇒ 不拆出来就会变成「闸门还在、覆盖没了」。
（独立评审 2026-10-06 点出：当时留下的网里断言 `validate_schema_name("platform")` 的**一条都没有**。）

对应 spec `docs/specs/2026-10-04-platform-console-design.md` §7.1 / §7.2 的处置 1、2。

⚠️ **输入一律写字面量 `"platform"`**（⛔ 不是把 `PLATFORM_SCHEMA_NAME` 当输入再拿它当期望）：
常量被改名成别的值时，拿常量当输入的用例**照样绿**（`AGENTS.md` §5-G：断言要钉**值**、不要钉常量，E-0024）。
保留名的**值**由 spec §7-Q2 逐字给定，所以下面单独把它钉一次。

⚠️ **夹具一律合成值**（`AGENTS.md` §5-C）：探针名带**本会话片段**（从测试库名派生，避免并行会话互踩），
域名一律 `.test.local`；⛔ 零真实租户名。
"""
import re

from django.conf import settings
from django.db import connection
from django.test import TransactionTestCase
from django_tenants.postgresql_backend.base import is_valid_schema_name
from django_tenants.utils import schema_exists
from psycopg import sql

from apps.platform.fleet import FleetEntry, FleetError
from apps.platform.identifiers import (
    PLATFORM_SCHEMA_NAME,
    PUBLIC_SCHEMA_NAME,
    InvalidIdentifierError,
    validate_schema_name,
)
from apps.platform.models import Domain, Tenant
from apps.platform.provisioning import provision_tenant


def _session_segment() -> str:
    """本会话的探针片段：从**本会话测试库名**派生（并行会话各用各的库名 ⇒ 天然唯一）。"""
    raw = re.sub(r"[^a-z0-9_]", "_", str(settings.DATABASES["default"]["NAME"]).lower())
    segment = raw.strip("_") or "s"
    if segment[0].isdigit():
        segment = "s" + segment
    return segment[:32]


SEGMENT = _session_segment()
DOMAIN_RESERVED_PROBE = f"reserved-{SEGMENT}.test.local"


def _drop_schema(name: str) -> None:
    """删掉探针 schema（幂等）。⚠️ 硬保险：⛔ 绝不许删到平台平面（`AGENTS.md` §5-A）。"""
    if name == PUBLIC_SCHEMA_NAME:
        raise AssertionError(f"探针清理不许删平台平面：{name!r}")
    if not schema_exists(name):
        return
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


class ReservedPlatformSchemaNameTests(TransactionTestCase):
    """`platform` 不能当租户 schema 名——两条路径都要拒。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        # 守卫若失效（或变异验证时被拿掉），`provision_tenant` 会真的建出 `platform` schema
        # ⇒ 收尾把它清掉。⛔ 只清这一个保留名，不做任何前缀 / 模糊匹配。
        cls.addClassCleanup(_drop_schema, PLATFORM_SCHEMA_NAME)

    def test_validate_schema_name_rejects_the_reserved_platform_name(self):
        """路径 ①：`install_tenant` / `fleet.json` 走的闸门。"""
        self.assertEqual(PLATFORM_SCHEMA_NAME, "platform")
        with self.assertRaises(InvalidIdentifierError):
            validate_schema_name("platform")
        # 对照：挡的是**恰好等于**保留名，⛔ 不是前缀 / 子串（否则 `platform_a` 这种正常租户名
        # 也会被误杀，而「挡住保留名」那半边照样绿 ⇒ 只钉「它会抛」是不够的）。
        self.assertEqual(validate_schema_name("platform_a"), "platform_a")
        self.assertEqual(validate_schema_name("my_platform"), "my_platform")

    def test_fleet_entry_rejects_the_reserved_platform_name(self):
        """路径 ① 的入口之一：`fleet.json` 里出现 `platform` 必须响亮失败。"""
        with self.assertRaises(FleetError):
            FleetEntry(
                schema_name="platform",
                name="合成租户乙",
                domain=DOMAIN_RESERVED_PROBE,
            )

    def test_provision_tenant_rejects_the_reserved_platform_name(self):
        """路径 ②：供给入口。

        `provision_tenant` 用的 `is_valid_schema_name`（django-tenants）**放行 `platform`**
        ⇒ 挡它的必须是本仓自己那条保留名判据。
        """
        # 对照（读依赖源码 + 实测）：那把尺子对 `platform` 是**放行**的，所以这条判据不是多余的。
        self.assertTrue(is_valid_schema_name("platform"))

        with self.assertRaises(ValueError):
            provision_tenant(
                schema_name="platform",
                name="合成租户丙",
                domain=DOMAIN_RESERVED_PROBE,
            )

        # 失败必须是**在写任何东西之前**：没有 schema、也没有目录行（`Tenant` / `Domain`）。
        self.assertFalse(schema_exists("platform"))
        self.assertFalse(Tenant.objects.filter(schema_name="platform").exists())
        self.assertFalse(Domain.objects.filter(domain=DOMAIN_RESERVED_PROBE).exists())
