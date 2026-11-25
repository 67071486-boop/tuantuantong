"""每租户独立 database / 角色的供给原语（Task 1：B 档隔离）。

前半是**纯函数级**（`SimpleTestCase`）、**不碰数据库**：只钉「闸门 / 派生 / SQL 文本」。
`CREATE DATABASE` / `CREATE ROLE` 不能在事务块里跑（计划「已实测的事实」第 2 条），
端到端证据另在 Task 4（供给命令）与 Task 6（隔离红线）。

⚠️ 末尾的 `NonSuperuserProvisioningTests` 是**有意的例外**：它必须真建库建角色
（`TransactionTestCase`）。理由：「非超管的运维身份能不能开通租户」是**集群行为**，不是 SQL
文本——纯函数用例看不见 `provision_database` 里那句 `GRANT` 到底执不执行（评审 F1）。
"""
import re
from unittest import mock

import psycopg
from django.conf import settings
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from psycopg import sql

from apps.platform.db_provisioning import (
    admin_connection_params,
    build_provision_statements,
    database_exists,
    drop_tenant_database,
    generate_password,
    provision_database,
    role_exists,
    tenant_database_name,
    tenant_role_name,
)
from apps.platform.identifiers import (
    InvalidIdentifierError,
    validate_identifier,
    validate_schema_name,
)


class IdentifierGateTests(SimpleTestCase):
    """严格白名单——**不要**改成 django-tenants 的 is_valid_schema_name（事实 11：它放行任意字符）。"""

    def test_accepts_lowercase_snake_case(self):
        self.assertEqual(validate_identifier("tenant_01", what="schema"), "tenant_01")

    def test_rejects_the_injection_shaped_name(self):
        # 这个值在 django-tenants 的校验器下是**合法**的（实测 `is_valid_schema_name` 返回一个
        # **匹配对象**、真值，不是 `True`）——所以那个校验器不能当注入闸门。
        with self.assertRaises(InvalidIdentifierError):
            validate_identifier('x"; DROP DATABASE postgres; --', what="schema")

    def test_rejects_space_uppercase_leading_digit_and_over_63_bytes(self):
        # `"dev\n"` 是 `$` 锚点的反例：`re.match` 会在结尾换行**之前**收尾、把它放行（实测）
        # ⇒ 校验器必须用 `fullmatch`。少了这一行，白名单那句「只允许…」就是假的（E-0011 同族）。
        for bad in ("has space", "UPPER", "1leading", "a" * 64, "dev\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidIdentifierError):
                    validate_identifier(bad, what="schema")

    def test_rejects_a_non_string(self):
        # 非 str 交给正则会抛 TypeError（不是本模块的异常类型）⇒ 必须显式挡在前面。
        with self.assertRaises(InvalidIdentifierError):
            validate_identifier(None, what="schema")

    def test_error_message_names_the_field_and_the_offending_value(self):
        # 「哪个字段、什么值」——排障时最少的可用信息，值本身不是机密。
        with self.assertRaises(InvalidIdentifierError) as ctx:
            validate_identifier("bad name", what="database name")
        self.assertIn("database name", str(ctx.exception))
        self.assertIn("bad name", str(ctx.exception))


class SchemaNameGateTests(SimpleTestCase):
    """`validate_schema_name` 在通用白名单之上多挡两个 Postgres 特殊名。"""

    def test_accepts_a_valid_schema_name(self):
        self.assertEqual(validate_schema_name("tenant_01"), "tenant_01")

    def test_rejects_the_public_schema(self):
        # public 是平台平面：放行它会把租户数据写进共享 schema。
        with self.assertRaises(InvalidIdentifierError):
            validate_schema_name("public")

    def test_rejects_the_reserved_pg_prefix(self):
        # `pg_` 是 PostgreSQL 自己的保留前缀（django-tenants 也挡它）。
        with self.assertRaises(InvalidIdentifierError):
            validate_schema_name("pg_tenant")

    def test_rejects_the_injection_shaped_name(self):
        with self.assertRaises(InvalidIdentifierError):
            validate_schema_name('x"; DROP DATABASE postgres; --')


class DerivedNameTests(SimpleTestCase):
    @override_settings(TENANT_DB_PREFIX="tt_")
    def test_names_are_derived_from_schema_name(self):
        self.assertEqual(tenant_database_name("dev"), "tt_dev")
        self.assertEqual(tenant_role_name("dev"), "tt_dev")

    def test_derived_name_over_63_bytes_is_rejected(self):
        with override_settings(TENANT_DB_PREFIX="tt_"):
            with self.assertRaises(InvalidIdentifierError):
                tenant_database_name("a" * 61)   # tt_ + 61 = 64 > 63

    def test_derived_role_name_over_63_bytes_is_rejected_too(self):
        with override_settings(TENANT_DB_PREFIX="tt_"):
            with self.assertRaises(InvalidIdentifierError):
                tenant_role_name("a" * 61)

    @override_settings(TENANT_DB_PREFIX="tt_")
    def test_derived_names_reject_a_trailing_newline(self):
        # 派生路径不能成为绕过闸门的第二条路：`'dev\n'` 必须在这里也被拒（实测 `fullmatch` 之前
        # 会静默派生出 `'tt_dev\n'`，那种库名连 drop 的确认串都对不上）。
        with self.assertRaises(InvalidIdentifierError):
            tenant_database_name("dev\n")
        with self.assertRaises(InvalidIdentifierError):
            tenant_role_name("dev\n")


class ProvisionStatementsTests(SimpleTestCase):
    def test_statements_pin_the_isolation_revoke_verbatim(self):
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role="tt_dev", database="tt_dev", password="pw-1", operator="tt_ops"
            )
        ]
        self.assertEqual(
            rendered,
            [
                'CREATE ROLE "tt_dev" LOGIN PASSWORD \'pw-1\'',
                'GRANT "tt_dev" TO "tt_ops"',
                'CREATE DATABASE "tt_dev" OWNER "tt_dev" TEMPLATE template0',
                'REVOKE ALL ON DATABASE "tt_dev" FROM PUBLIC',
            ],
        )

    def test_identifiers_are_quoted_not_interpolated(self):
        # 注入形状的值必须被**引号包住**而不是逃出去（事实 13）
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role="a_b", database="a_b", password="pw", operator="tt_ops"
            )
        ]
        self.assertIn('"a_b"', rendered[0])

    def test_injection_shaped_identifier_cannot_escape_its_quotes(self):
        # 事实 13：`sql.Identifier('x"; DROP')` → `"x""; DROP"`——引号被**成对转义**，
        # 整个值仍是一个标识符，没有半句 SQL 逃到外面。
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role='x"; DROP DATABASE postgres; --',
                database='x"; DROP DATABASE postgres; --',
                password="pw",
                operator="tt_ops",
            )
        ]
        escaped = '"x""; DROP DATABASE postgres; --"'
        self.assertEqual(
            rendered,
            [
                f"CREATE ROLE {escaped} LOGIN PASSWORD 'pw'",
                f"GRANT {escaped} TO \"tt_ops\"",
                f"CREATE DATABASE {escaped} OWNER {escaped} TEMPLATE template0",
                f"REVOKE ALL ON DATABASE {escaped} FROM PUBLIC",
            ],
        )

    def test_password_with_a_quote_is_a_literal_not_f_string_interpolation(self):
        # 密码同理：单引号必须成对转义，否则末尾那个 `'` 能被用来提前闭合字面量。
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role="a_b", database="a_b", password="pw'1", operator="tt_ops"
            )
        ]
        self.assertEqual(rendered[0], 'CREATE ROLE "a_b" LOGIN PASSWORD \'pw\'\'1\'')

    def test_the_membership_grant_precedes_the_create_database(self):
        # 承重顺序：CREATE DATABASE ... OWNER 要求执行者**先**能 SET ROLE 到该角色
        # （本机 PG 16 实测：没有这一句 ⇒ ERROR: must be able to SET ROLE）。
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role="tt_dev", database="tt_dev", password="pw-1", operator="tt_ops"
            )
        ]
        self.assertLess(
            next(i for i, s in enumerate(rendered) if s.startswith("GRANT")),
            next(i for i, s in enumerate(rendered) if s.startswith("CREATE DATABASE")),
        )

    def test_statements_come_back_in_the_fixed_order(self):
        # 顺序是承重的：REVOKE 在库存在之前执行会报错（R4）；
        # GRANT 必须在 CREATE DATABASE 之前（否则 must be able to SET ROLE）。
        rendered = [
            s.as_string(None)
            for s in build_provision_statements(
                role="tt_dev", database="tt_dev", password="pw-1", operator="tt_ops"
            )
        ]
        self.assertTrue(rendered[0].startswith("CREATE ROLE "))
        self.assertTrue(rendered[1].startswith("GRANT "))
        self.assertTrue(rendered[2].startswith("CREATE DATABASE "))
        self.assertTrue(rendered[3].startswith("REVOKE ALL ON DATABASE "))


class AdminConnectionParamsTests(SimpleTestCase):
    """键名是下游 `psycopg.connect(**admin_connection_params())` 的调用契约（Task 4/5）。"""

    @override_settings(
        POSTGRES_ADMIN_USER="admin_probe",
        POSTGRES_ADMIN_PASSWORD="pw_probe",
        POSTGRES_ADMIN_DB="db_probe",
    )
    def test_returns_psycopg_connect_kwargs_for_the_admin_identity(self):
        params = admin_connection_params()
        self.assertEqual(
            set(params), {"host", "port", "dbname", "user", "password"}
        )
        self.assertEqual(params["user"], "admin_probe")
        self.assertEqual(params["password"], "pw_probe")
        self.assertEqual(params["dbname"], "db_probe")
        # 供给必须打到应用库所在的**同一台**服务器上，否则探到的是另一个集群。
        self.assertTrue(params["host"])
        self.assertTrue(params["port"])


class PasswordGenerationTests(SimpleTestCase):
    def test_password_is_url_safe_ascii_and_long_enough(self):
        # 它要能原样进 SQL 字面量、也要能写进 .env（`--emit-env`）⇒ 只允许 URL-safe 字母表。
        password = generate_password()
        self.assertRegex(password, r"^[A-Za-z0-9_-]+$")
        self.assertGreaterEqual(len(password), 32)

    def test_two_calls_differ(self):
        self.assertNotEqual(generate_password(), generate_password())


class DropConfirmationTests(SimpleTestCase):
    """确认串语义：必须逐字等于**库名**（`tt_dev`），照 `set_tenant_password` 的显式确认风格。"""

    @override_settings(TENANT_DB_PREFIX="tt_")
    def test_the_schema_name_alone_is_not_a_valid_confirmation(self):
        # 最容易犯的手滑：拿 schema 名（dev）当库名（tt_dev）。
        for wrong in ("dev", "TT_DEV", "tt_dev ", ""):
            with self.subTest(confirm=wrong):
                with self.assertRaises(ValueError):
                    drop_tenant_database(schema_name="dev", confirm=wrong)

    @override_settings(TENANT_DB_PREFIX="tt_")
    def test_wrong_confirmation_is_rejected_before_any_connection(self):
        with mock.patch("apps.platform.db_provisioning.psycopg.connect") as connect:
            with self.assertRaises(ValueError):
                drop_tenant_database(schema_name="dev", confirm="dev")
        connect.assert_not_called()


class ProvisionGateTests(SimpleTestCase):
    """供给前先过闸门：不合法的名字必须**在连库之前**就被拒绝。"""

    def test_rejects_an_injection_shaped_schema_name_without_connecting(self):
        with mock.patch("apps.platform.db_provisioning.psycopg.connect") as connect:
            with self.assertRaises(InvalidIdentifierError):
                provision_database(
                    schema_name='x"; DROP DATABASE postgres; --', password="pw"
                )
        connect.assert_not_called()

    @override_settings(TENANT_DB_PREFIX="tt_")
    def test_rejects_a_derived_name_over_63_bytes_without_connecting(self):
        with mock.patch("apps.platform.db_provisioning.psycopg.connect") as connect:
            with self.assertRaises(InvalidIdentifierError):
                provision_database(schema_name="a" * 61, password="pw")
        connect.assert_not_called()


# ── 非超管运维身份：这一组**真建库建角色**，与上面那些纯函数用例不同 ──────────
def _session_segment() -> str:
    """本会话片段：从**配置的库名**（`settings.DATABASES["default"]["NAME"]`）派生。

    跑测试时的约定是 `POSTGRES_DB=<会话后缀>` ⇒ 这个值已经会话唯一（它在**导入期**取到的就是
    那个后缀，不是 `test_` 前缀的名字）。硬编码的名字只是「任务后缀」而不是「会话后缀」⇒
    并行会话会互踩（同 `test_database_isolation` 的探针纪律：一方 `DROP DATABASE … WITH (FORCE)`
    能删掉另一方的对象）。这里只清洗成合法标识符并截断（给 `ops_` / `tt_` + 名字主体留出长度）。
    """
    raw = re.sub(r"[^a-z0-9_]", "_", str(settings.DATABASES["default"]["NAME"]).lower())
    segment = raw.strip("_") or "s"
    if segment[0].isdigit():
        segment = "s" + segment
    return segment[:24]


SEGMENT = _session_segment()

#: 运维身份：**非超管**，只有 `CREATEDB + CREATEROLE`（照 RUNBOOK §2.5.1 的复现配方）。
OPS_ROLE = f"ops_{SEGMENT}"
OPS_PASSWORD = "pw-ops-nonsuper"

#: 两个探针 schema（派生出的库/角色名带会话片段）：fresh = 全新供给；exist = 角色**已存在**。
FRESH_SCHEMA = f"ns_fresh_{SEGMENT}"
EXISTING_SCHEMA = f"ns_exist_{SEGMENT}"


def _ops_connection_params() -> dict[str, str]:
    """以**那个非超管运维角色**连集群维护库（host/port/dbname 与 `admin_connection_params` 同源）。"""
    params = admin_connection_params()
    params["user"] = OPS_ROLE
    params["password"] = OPS_PASSWORD
    return params


def _has_role_option(member: str, role: str, kind: str) -> bool:
    """`pg_has_role(<member>, <role>, <kind>)` —— `kind` 取 `MEMBER` / `SET` / `USAGE`。

    ⚠️ **`MEMBER` 在这里不是判据**（本机 PG 16.15 实测）：运维角色**建**出租户角色后，
    PG 会自动写一行 `admin_option=true, set_option=false` 的成员行 ⇒ 此时 `MEMBER` 已经是
    `true`，而 `CREATE DATABASE … OWNER` 仍然报 `must be able to SET ROLE`。真正能分辨
    「我们那句 `GRANT` 执行了没有」的是 **`SET`**（或 `pg_auth_members.set_option`）。
    用 `MEMBER` 当判据会得到一个**恒真**的假保证——那正是本用例要避免的东西。
    """
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        return bool(
            conn.execute(
                "SELECT pg_has_role(%s, %s, %s)", (member, role, kind)
            ).fetchone()[0]
        )


def _drop_role(name: str) -> None:
    """`DROP ROLE IF EXISTS`（幂等；清理用，不是断言）。"""
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def _purge_tenant(schema_name: str) -> None:
    """删掉探针的库与角色（`confirm` 用派生值，字面量抄错时也不会漏删一个真库）。"""
    drop_tenant_database(
        schema_name=schema_name, confirm=tenant_database_name(schema_name)
    )


class NonSuperuserProvisioningTests(TransactionTestCase):
    """非超管运维身份也能开通租户：那句 `GRANT` 的**真集群**判据（评审 F1）。

    为什么非要有这一条：纯函数用例只钉 SQL 文本，它们**看不见** `provision_database` 里那句
    `GRANT` 到底执不执行——把它挪进 `if not _role_exists` 分支、或把 `operator` 的回退写死，
    那些用例全绿。本类用一个 `rolsuper=f` 的运维角色**真跑一遍供给**，钉住：
    ① 库与租户角色都建出来了；② 能 `SET ROLE` 到租户角色的成员资格**真的存在**；
    ③ `REVOKE` 真的生效；④ 清理得掉（本用例不往集群里留东西）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 清理**先注册**（`addClassCleanup` 是 LIFO ⇒ 这一行最后执行）：运维角色必须在
        # 探针租户库/租户角色都删掉之后再删。
        cls.addClassCleanup(_drop_role, OPS_ROLE)
        # 此刻 settings **还没被覆盖** ⇒ 这条连接是真超管，用它建那个**非超管**运维角色。
        with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
            # 先 `DROP … IF EXISTS`：上一次被中断的运行会留下同名角色（CREATE 撞名会让
            # 整个 setUpClass 崩在别人的残骸上）。
            conn.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(OPS_ROLE))
            )
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CREATEDB CREATEROLE").format(
                    sql.Identifier(OPS_ROLE), sql.Literal(OPS_PASSWORD)
                )
            )

    def setUp(self):
        # 每个用例自己登记清理（`TransactionTestCase` 不回滚 DDL）。
        for schema_name in (FRESH_SCHEMA, EXISTING_SCHEMA):
            self.addCleanup(_purge_tenant, schema_name)

    def _provision_as_operator(self, schema_name: str) -> None:
        """把运维身份临时换成那个非超管角色，再走**生产入口** `provision_database`。"""
        with self.settings(
            POSTGRES_ADMIN_USER=OPS_ROLE,
            POSTGRES_ADMIN_PASSWORD=OPS_PASSWORD,
            POSTGRES_ADMIN_DB="postgres",
        ):
            provision_database(
                schema_name=schema_name, password="pw-ns-tenant", operator=OPS_ROLE
            )

    def _assert_provisioned_and_isolated(self, schema_name: str) -> None:
        """① + ③：库/角色真的建出来了、`REVOKE` 真的生效。"""
        database = tenant_database_name(schema_name)
        role = tenant_role_name(schema_name)
        self.assertTrue(role_exists(role), f"{role} 应当已建出来")
        self.assertTrue(database_exists(database), f"{database} 应当已建出来")
        with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
            acl = conn.execute(
                "SELECT datacl::text[] FROM pg_database WHERE datname = %s",
                (database,),
            ).fetchone()[0]
            # `datacl` 为 NULL = 用默认权限，而默认权限给 PUBLIC `CONNECT` ⇒ 那句 REVOKE 没执行。
            self.assertIsNotNone(
                acl, f"{database} 的 datacl 为 NULL ⇒ REVOKE 没生效（用的是默认权限）"
            )
            public_entries = [item for item in acl if item.split("=", 1)[0] == ""]
            self.assertEqual(
                public_entries, [], f"PUBLIC 在这个库上仍有显式权限：{acl}"
            )
            # 与 `datacl` 形状判据**相互独立**的第二条证据。
            self.assertIs(
                conn.execute(
                    "SELECT has_database_privilege('public', %s, 'CONNECT')",
                    (database,),
                ).fetchone()[0],
                False,
                f"PUBLIC 在 {database} 上不该还有 CONNECT",
            )

    def test_non_superuser_operator_can_provision_a_fresh_tenant(self):
        """全新租户：非超管运维身份建角色 → 补成员资格 → 建库 → 收 PUBLIC 的 CONNECT。"""
        self._provision_as_operator(FRESH_SCHEMA)
        self._assert_provisioned_and_isolated(FRESH_SCHEMA)

        role = tenant_role_name(FRESH_SCHEMA)
        # ② 成员资格真的存在。`SET`（而不是 `MEMBER`）才是分辨力所在——见 `_has_role_option`。
        self.assertIs(
            _has_role_option(OPS_ROLE, role, "SET"),
            True,
            f"{OPS_ROLE} 应当能 SET ROLE 到 {role}（那句 GRANT 的效果）",
        )
        self.assertIs(_has_role_option(OPS_ROLE, role, "MEMBER"), True)
        # 反向对照：成员资格是**单向**的——租户角色不该反过来能变成运维身份。
        self.assertIs(_has_role_option(role, OPS_ROLE, "SET"), False)
        self.assertIs(_has_role_option(role, OPS_ROLE, "MEMBER"), False)

        # ④ 清理得掉：删库删角色之后两者都不在。
        _purge_tenant(FRESH_SCHEMA)
        self.assertFalse(database_exists(tenant_database_name(FRESH_SCHEMA)))
        self.assertFalse(role_exists(role))

    def test_membership_is_granted_even_when_the_tenant_role_already_exists(self):
        """角色**已存在**时也要补成员资格 —— 「`GRANT` 无条件执行」那句的钉子。

        场景就是「上一次供给建过角色、在建库那一步失败了」：此时 `CREATE ROLE` 被跳过，
        成员资格只能靠**无条件执行**的那句 `GRANT` 补上。把它挪进 `if not _role_exists`
        分支，本用例会以 `must be able to SET ROLE` **红**（见 report 的变异自证）。
        """
        role = tenant_role_name(EXISTING_SCHEMA)
        # 前置：由**运维身份自己**建出租户角色（与生产的 `CREATE ROLE` 同形）。PG 16 会给建角色者
        # 自动写一行 `admin_option=true, set_option=false` 的成员行 ⇒ 它能 GRANT、但**不能** SET ROLE。
        with psycopg.connect(**_ops_connection_params(), autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal("pw-ns-tenant")
                )
            )
        # 前置对照：此刻**没有**能 SET ROLE 的成员资格 —— 证明下面的断言不是恒真。
        self.assertIs(
            _has_role_option(OPS_ROLE, role, "SET"),
            False,
            "前置态就该是不能 SET ROLE（否则本用例分辨不出那句 GRANT）",
        )

        self._provision_as_operator(EXISTING_SCHEMA)
        self._assert_provisioned_and_isolated(EXISTING_SCHEMA)
        # ② 角色已存在 ⇒ 成员资格只能来自那句**无条件执行**的 GRANT。
        self.assertIs(
            _has_role_option(OPS_ROLE, role, "SET"),
            True,
            f"角色已存在时也要补上成员资格（{OPS_ROLE} → {role}）",
        )

        # ④ 清理得掉。
        _purge_tenant(EXISTING_SCHEMA)
        self.assertFalse(database_exists(tenant_database_name(EXISTING_SCHEMA)))
        self.assertFalse(role_exists(role))
