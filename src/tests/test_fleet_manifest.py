"""车队清单（Task 2）：运维库存的解析与闸门。

本文件是**纯解析级**（`SimpleTestCase`）——不碰数据库、不建库建角色：清单只是文本。
清单的定位（运维库存，**不是**权威租户目录）与 R2/R4 的口径见
`apps/platform/fleet.py` 的模块 docstring；这里只钉行为。

断言一律写字面值（`"tt_dev"` 而不是 `settings.TENANT_DB_PREFIX + "dev"`）——
钉常量会让「常量被改坏」时测试照样绿（AGENTS.md §5 / E-0024）。
"""
import json
import tempfile
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.platform.fleet import FleetEntry, FleetError, entry_for_schema, load_fleet


class FleetManifestTestCase(SimpleTestCase):
    """写临时清单的公共夹具：每条用例自己造文件，用例之间互不依赖。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "fleet.json"

    def write_raw(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def write_tenants(self, tenants: list) -> Path:
        return self.write_raw(json.dumps({"tenants": tenants}, ensure_ascii=False))


class ParseTests(FleetManifestTestCase):
    def test_parses_one_entry_and_derives_database_and_role(self):
        self.write_tenants(
            [{"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"}]
        )

        entries = load_fleet(self.path)

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.schema_name, "dev")
        self.assertEqual(entry.name, "开发租户")
        self.assertEqual(entry.domain, "dev.test.local")
        # 派生值：字面值，不是 `settings.TENANT_DB_PREFIX + "dev"`。
        self.assertEqual(entry.database, "tt_dev")
        self.assertEqual(entry.role, "tt_dev")

    def test_an_empty_fleet_is_allowed_and_comes_back_as_an_empty_tuple(self):
        self.write_tenants([])

        self.assertEqual(load_fleet(self.path), ())

    def test_database_and_role_are_derived_properties_not_stored_fields(self):
        # R2：存进 JSON（或变成 dataclass 字段）就会漂移 ⇒ 两者都必须只是**属性**。
        self.assertEqual(
            [f.name for f in fields(FleetEntry)],
            ["schema_name", "name", "domain"],
        )
        self.assertIsInstance(FleetEntry.database, property)
        self.assertIsInstance(FleetEntry.role, property)

    def test_entries_are_frozen(self):
        self.write_tenants(
            [{"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"}]
        )
        entry = load_fleet(self.path)[0]

        with self.assertRaises(FrozenInstanceError):
            entry.schema_name = "other"


class FileLevelErrorTests(FleetManifestTestCase):
    """文件级错误：信息里必须带**路径**（R3）。"""

    def test_missing_file_names_the_path(self):
        missing = self.root / "nope.json"

        with self.assertRaises(FleetError) as ctx:
            load_fleet(missing)

        self.assertIn(str(missing), str(ctx.exception))

    def test_broken_json_syntax_names_the_path(self):
        self.write_raw("{ not json")

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        self.assertIn(str(self.path), str(ctx.exception))

    def test_top_level_that_is_not_an_object_is_rejected(self):
        self.write_raw("[1, 2]")

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn(str(self.path), message)
        self.assertIn("list", message)

    def test_missing_tenants_key_is_rejected(self):
        self.write_raw("{}")

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        # 钉**带引号**的形式：`"tenants"` 在静态文案里也出现（「顶层只允许 ['tenants']」），
        # 不带引号的 `assertIn` 在缺别的键时照样通过（E-0024 同族的弱断言）。
        self.assertIn("'tenants'", message)
        self.assertIn(str(self.path), message)

    def test_tenants_must_be_an_array(self):
        self.write_raw(json.dumps({"tenants": {"dev": {}}}))

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("tenants", message)
        self.assertIn(str(self.path), message)

    def test_unknown_top_level_key_is_rejected(self):
        # R4 的封闭形状：顶层只允许 `tenants`（`tenant` 这种拼错的键不许被静默吞掉）。
        self.write_raw(json.dumps({"tenants": [], "tenant": []}))

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("tenant", message)
        self.assertIn(str(self.path), message)


class EntryLevelErrorTests(FleetManifestTestCase):
    """条目级错误：信息里必须带**第几条**与出错的值（R3/R4）。"""

    def test_entry_missing_a_field_names_the_entry_index(self):
        self.write_tenants(
            [
                {"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"},
                {"schema_name": "beta", "name": "测试租户"},  # 缺 domain
            ]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("domain", message)
        self.assertIn("第 2 条", message)
        self.assertIn(str(self.path), message)

    def test_entry_that_is_not_an_object_names_the_entry_index(self):
        self.write_tenants(["dev"])

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("第 1 条", message)
        self.assertIn(str(self.path), message)

    def test_unknown_entry_key_is_rejected(self):
        # R4：多余键不静默容忍；顺带钉住 R2——派生值 `database` 不许写进 JSON。
        self.write_tenants(
            [
                {
                    "schema_name": "dev",
                    "name": "开发租户",
                    "domain": "dev.test.local",
                    "database": "tt_dev",
                }
            ]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        # 钉带引号的形式：静态文案里也有「派生值（database / role）」这句，
        # 不带引号的 `assertIn("database", …)` 在**任何**未知键下都会通过（弱断言）。
        self.assertIn("'database'", message)
        self.assertIn("第 1 条", message)

    def test_injection_shaped_schema_name_is_rejected(self):
        # 注入形状值：它在 django-tenants 的校验器下是**合法**的（事实 11）。
        self.write_tenants(
            [
                {
                    "schema_name": 'x"; DROP DATABASE postgres; --',
                    "name": "坏租户",
                    "domain": "bad.test.local",
                }
            ]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn('x"; DROP DATABASE postgres; --', message)
        self.assertIn("第 1 条", message)

    def test_public_schema_name_is_rejected(self):
        # `public` 能过通用白名单，但它是平台平面，不是租户 schema。
        self.write_tenants(
            [{"schema_name": "public", "name": "平台", "domain": "public.test.local"}]
        )

        with self.assertRaises(FleetError):
            load_fleet(self.path)

    def test_schema_name_whose_derived_database_exceeds_63_bytes_is_rejected(self):
        # `tt_` + 61 = 64 > 63：不合法的**是派生名**，schema_name 自己看是合法的
        # ⇒ 必须在读清单时就挡住，否则错误会推迟到访问 `.database` 时才炸。
        self.write_tenants(
            [{"schema_name": "a" * 61, "name": "长名", "domain": "long.test.local"}]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        self.assertIn("database name", str(ctx.exception))

    def test_empty_domain_is_rejected(self):
        self.write_tenants(
            [{"schema_name": "dev", "name": "开发租户", "domain": ""}]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("domain", message)
        self.assertIn("第 1 条", message)

    def test_empty_name_is_rejected(self):
        self.write_tenants(
            [{"schema_name": "dev", "name": "", "domain": "dev.test.local"}]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("name", message)
        self.assertIn("第 1 条", message)

    def test_duplicate_schema_name_is_rejected_and_the_value_is_named(self):
        self.write_tenants(
            [
                {"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"},
                {"schema_name": "beta", "name": "测试租户", "domain": "beta.test.local"},
                {"schema_name": "dev", "name": "开发租户副本", "domain": "dev2.test.local"},
            ]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("schema_name", message)
        self.assertIn("'dev'", message)  # 点名**重复的值**，不是只说「有重复」
        self.assertIn(str(self.path), message)

    def test_duplicate_domain_is_rejected_and_the_value_is_named(self):
        # 一个域名只能路由到一个租户（Host 路由）⇒ 重复 domain 是清单错误。
        self.write_tenants(
            [
                {"schema_name": "dev", "name": "开发租户", "domain": "same.test.local"},
                {"schema_name": "beta", "name": "测试租户", "domain": "same.test.local"},
            ]
        )

        with self.assertRaises(FleetError) as ctx:
            load_fleet(self.path)

        message = str(ctx.exception)
        self.assertIn("domain", message)
        self.assertIn("'same.test.local'", message)
        self.assertIn(str(self.path), message)


class DefaultPathTests(FleetManifestTestCase):
    def test_path_none_reads_the_setting(self):
        self.write_tenants(
            [{"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"}]
        )

        with override_settings(TENANT_FLEET_FILE=str(self.path)):
            entries = load_fleet()

        self.assertEqual(entries[0].database, "tt_dev")


class EntryForSchemaTests(FleetManifestTestCase):
    def setUp(self):
        super().setUp()
        self.write_tenants(
            [
                {"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"},
                {"schema_name": "beta", "name": "测试租户", "domain": "beta.test.local"},
            ]
        )
        self.entries = load_fleet(self.path)

    def test_returns_the_matching_entry(self):
        entry = entry_for_schema(self.entries, "dev")

        self.assertEqual(entry.name, "开发租户")
        self.assertEqual(entry.database, "tt_dev")

    def test_unknown_schema_names_the_value_and_what_is_available(self):
        with self.assertRaises(FleetError) as ctx:
            entry_for_schema(self.entries, "ghost")

        message = str(ctx.exception)
        self.assertIn("'ghost'", message)
        self.assertIn("'dev'", message)
        self.assertIn("'beta'", message)


class CommittedExampleTests(SimpleTestCase):
    def test_the_example_manifest_parses_and_matches_the_documented_shape(self):
        # 仓库只提交 fleet.example.json（真实 fleet.json 不进 git）⇒ 它必须能被本模块读进来。
        path = Path(settings.REPO_ROOT) / "fleet.example.json"

        entries = load_fleet(path)

        self.assertEqual(
            [(e.schema_name, e.database, e.role, e.domain) for e in entries],
            [("dev", "tt_dev", "tt_dev", "dev.test.local")],
        )
