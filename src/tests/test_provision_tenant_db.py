"""`provision_tenant_db`：端到端供给命令（Task 4）。

⚠️ **基类**：真建库真建角色的用例用 `TransactionTestCase`——`CREATE DATABASE` /
`CREATE ROLE` **不能在事务块里跑**（Task 1 计划的「已实测的事实」第 2 条），而
`TenantTestCase` / `TestCase` 会把用例包在外层事务里。纯函数断言放 `SimpleTestCase`（R4）。

⚠️ **每个探针名都带本会话片段**（从测试库名派生，见 `_session_segment()`）：database / role 是
**集群级**对象，固定名会与并行会话互踩（本仓实测过，症状看起来像代码坏了）。schema 名也一并带，
保证派生出的库/角色名也带。

⚠️ **清理**：`addClassCleanup` 里对每个探针 schema 调 `drop_tenant_database(confirm=<库名>)`——
它先 `DROP DATABASE ... WITH (FORCE)` 再 `DROP ROLE`（角色被库占用时删不掉，顺序不能反），
两步都带 `IF EXISTS` 所以「从没建过」也安全。租户库内的目录行（`platform_tenant` /
`platform_domain`）就在被删的那个库的 `public` 里，随库一起消失。

⚠️ **本机 TCP 是 `trust`（`.tools/pgdata/pg_hba.conf` 实测）**：用「新密码能连上」证明
密码真的换了是**没有判别力**的（任何密码都连得上）。所以轮换用例除了照 brief 建连接之外，
还用 `pg_authid.rolpassword` 的 SCRAM 存储键**真正验证**那个密码（见 `_scram_password_matches`）。
"""
import base64
import hashlib
import hmac
import os
import re
import shutil
import stat
import tempfile
from io import StringIO
from pathlib import Path

import psycopg
from psycopg import sql
from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TransactionTestCase

from apps.platform.db_provisioning import (
    admin_connection_params,
    database_exists,
    drop_tenant_database,
    role_exists,
    tenant_database_name,
)

def _session_segment() -> str:
    """本会话的探针片段：从**本会话测试库名**派生（`settings.DATABASES["default"]["NAME"]`）。

    database / role 是**集群级**对象，而 `TENANT_DB_PREFIX` 默认 `tt_`、每个 worktree 都一样
    ⇒ 硬编码的 `_tN` **不是会话后缀**，并行会话会互踩（一个会话 `drop_tenant_database` 的
    `DROP DATABASE … WITH (FORCE)` 会掐掉另一个会话正在用的探针库）。本仓跑测试的约定是
    `POSTGRES_DB=<会话后缀>`，所以这个值已经**会话唯一**；这里只把它清洗成合法标识符并截断
    （给 `tt_` + schema 名留长度）。
    """
    raw = re.sub(r"[^a-z0-9_]", "_", str(settings.DATABASES["default"]["NAME"]).lower())
    segment = raw.strip("_") or "s"
    if segment[0].isdigit():
        segment = "s" + segment
    return segment[:32]


SEGMENT = _session_segment()

# ── 探针值（全部字面量；**不**从被测代码里取常量，避免自指断言 E-0024）────────────────
SCHEMA_A = f"prov_a_{SEGMENT}"
SCHEMA_B = f"prov_b_{SEGMENT}"
SCHEMA_ROTATE = f"prov_rot_{SEGMENT}"
#: 只作为 dry-run / 前置闸门的输入：这些用例必须证明它**从没被建出来**。
SCHEMA_UNTOUCHED = f"prov_untouched_{SEGMENT}"
#: 空白剥离探针：证明 CLI 给的地址**去掉首尾空白后**才落进凭据文件。
SCHEMA_WS = f"prov_ws_{SEGMENT}"

#: database / role 名（派生规则的**字面量**；`tt_` 前缀写死，会话片段派生）。
#: 若派生规则漂移，下面 `_assert_derivation` 会先红。
DATABASE_A = f"tt_prov_a_{SEGMENT}"
DATABASE_B = f"tt_prov_b_{SEGMENT}"
DATABASE_ROTATE = f"tt_prov_rot_{SEGMENT}"
DATABASE_UNTOUCHED = f"tt_prov_untouched_{SEGMENT}"
DATABASE_WS = f"tt_prov_ws_{SEGMENT}"

NAME_A = "供给探针甲"
NAME_B = "供给探针乙"
NAME_ROTATE = "轮换探针"
NAME_UNTOUCHED = "未触碰探针"

DOMAIN_A = f"prov-a-{SEGMENT}.test.local"
DOMAIN_B = f"prov-b-{SEGMENT}.test.local"
DOMAIN_ROTATE = f"prov-rot-{SEGMENT}.test.local"
DOMAIN_UNTOUCHED = f"prov-untouched-{SEGMENT}.test.local"
#: `--fleet` 模式的清单域名（与命令行那组不同，用来证明清单分支真的被读了）。
DOMAIN_FLEET = f"fleet-{SEGMENT}.test.local"
#: 空白剥离探针的域名（独立一个，避免与上面几条探针混用同一个库/角色）。
DOMAIN_WS = f"prov-ws-{SEGMENT}.test.local"

#: `--emit-host` / `--emit-port` 的探针值：必须与 settings 的默认值**不同形**，否则
#: 「CLI 的值真的落进文件」与「静默回退到 Mac 侧 settings」两种实现都能让断言通过（E-0024 同族）。
EMIT_HOST_LITERAL = "10.9.0.7"
EMIT_PORT_LITERAL = "5433"


def _assert_derivation(testcase, schema_name, database_name):
    """把上面的字面量与 Task 1 的派生函数对一次账。

    只在模块级常量被改动时才需要看：下游断言一律用**字面量**，这条只是防止「字面量抄错」
    把测试变成「测了个别的名字」。
    """
    testcase.assertEqual(tenant_database_name(schema_name), database_name)


def _connect(database, user, password):
    """用给定身份连给定库（host/port 跟应用库同一台）。"""
    application_database = settings.DATABASES["default"]
    return psycopg.connect(
        host=str(application_database["HOST"]),
        port=str(application_database["PORT"]),
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
    )


def _read_env_value(path, key):
    """从写出的 `.env` 片段里读一个键（忽略注释行；找不到就断言失败）。"""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :]
    raise AssertionError(f"{path} 里没有 {key}= 这一行")


def _role_password_hash(role):
    """读角色在 `pg_authid` 里的密码存储值。

    ⚠️ `pg_authid` 只有**超管**能读（本线的运维身份是 `postgres`，事实 7）。这是本文件
    唯一一条需要超管的断言，理由见模块 docstring：本机 `trust` 之下连得上不等于密码对。
    """
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        ).fetchone()
    if row is None:
        raise AssertionError(f"角色 {role!r} 不存在，读不到 rolpassword")
    return row[0]


def _scram_password_matches(password, rolpassword):
    """用 SCRAM-SHA-256 存储键判定「库里的密码就是 `password`」（RFC 5802 的口径）。

    `rolpassword` 形如 `SCRAM-SHA-256$<迭代>:<salt_b64>$<StoredKey_b64>:<ServerKey_b64>`；
    重算 `StoredKey = SHA256(HMAC(PBKDF2(password, salt, 迭代), "Client Key"))` 并比对。
    `trust` 之下这是唯一有判别力的证据。
    """
    scheme, rest = rolpassword.split("$", 1)
    if scheme != "SCRAM-SHA-256":
        return False
    iterations_part, keys_part = rest.split("$", 1)
    iterations, salt_b64 = iterations_part.split(":", 1)
    stored_key_b64, _server_key_b64 = keys_part.split(":", 1)
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations)
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    return hmac.compare_digest(
        hashlib.sha256(client_key).digest(), base64.b64decode(stored_key_b64)
    )


class ProvisionTenantDbPureFunctionTests(SimpleTestCase):
    """纯函数（不碰数据库，R4）：命令**怎么跑**这件事本身要能被断言。"""

    def test_build_install_env_pins_the_five_postgres_settings_verbatim(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_install_env,
        )

        env = build_install_env(
            database=DATABASE_A, role=DATABASE_A, password="pw-literal-1"
        )

        # 逐字断言：这三个值就是子进程连目标库所需要的全部身份信息。
        self.assertEqual(env["POSTGRES_DB"], DATABASE_A)
        self.assertEqual(env["POSTGRES_USER"], DATABASE_A)
        self.assertEqual(env["POSTGRES_PASSWORD"], "pw-literal-1")
        # 键集合封闭：多一个键（比如替租户编一个 DJANGO_SECRET_KEY）就是错。
        self.assertEqual(
            set(env),
            {
                "POSTGRES_DB",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "POSTGRES_HOST",
                "POSTGRES_PORT",
            },
        )
        # 主机/端口必须跟应用库**同一台**服务器（否则装到别的集群去了）。
        self.assertEqual(env["POSTGRES_HOST"], str(settings.DATABASES["default"]["HOST"]))
        self.assertEqual(env["POSTGRES_PORT"], str(settings.DATABASES["default"]["PORT"]))

    def test_build_install_env_carries_no_secret_key(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_install_env,
        )

        env = build_install_env(
            database=DATABASE_A, role=DATABASE_A, password="pw-literal-1"
        )

        # 每个租户服务器要有自己的 DJANGO_SECRET_KEY——命令不许替它编一个，
        # 也不许把平台这把带进去。
        self.assertNotIn("DJANGO_SECRET_KEY", env)
        self.assertNotIn(settings.SECRET_KEY, set(env.values()))

    def test_build_emitted_env_uses_the_app_side_host_and_port_verbatim(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_emitted_env,
        )

        env = build_emitted_env(
            database=DATABASE_A,
            role=DATABASE_A,
            password="pw-literal-1",
            host="10.9.0.7",
            port="5432",
        )

        # 租户机器看到的主机/端口必须是**应用侧视角**的值，不是 Mac 的隧道地址。
        self.assertEqual(env["POSTGRES_HOST"], "10.9.0.7")
        self.assertEqual(env["POSTGRES_PORT"], "5432")
        self.assertEqual(
            set(env),
            {
                "POSTGRES_DB",
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "POSTGRES_HOST",
                "POSTGRES_PORT",
            },
        )
        # 机械判据：这个文件会落到租户机器上，一个 POSTGRES_ADMIN_* 键都不许有。
        self.assertFalse([k for k in env if k.startswith("POSTGRES_ADMIN_")])

    def test_build_install_env_still_points_at_the_tunnel_side(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_install_env,
        )

        env = build_install_env(
            database=DATABASE_A, role=DATABASE_A, password="pw-literal-1"
        )

        # 回归钉子：install_tenant 子进程是在 **Mac** 上跑的，它必须走隧道，
        # 否则"给租户机器用的地址"会被误用到安装这一步（连不上，且症状像密码错）。
        self.assertEqual(env["POSTGRES_HOST"], str(settings.DATABASES["default"]["HOST"]))
        self.assertEqual(env["POSTGRES_PORT"], str(settings.DATABASES["default"]["PORT"]))

    def test_build_install_command_is_the_install_tenant_invocation(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_install_command,
        )

        argv = build_install_command()

        # 以 `install_tenant` 结尾（brief 测试 A2 的字面要求之一）。
        self.assertEqual(argv[-1], "install_tenant")
        self.assertEqual(len(argv), 3)
        self.assertEqual(argv[1], str(settings.BASE_DIR / "manage.py"))

    def test_build_install_argv_carries_schema_name_and_domain_verbatim(self):
        from apps.platform.management.commands.provision_tenant_db import (
            build_install_argv,
        )

        argv = build_install_argv(
            schema_name=SCHEMA_A, name=NAME_A, domain=DOMAIN_A
        )

        # ⛔ 不在这里拿 `argv[:3]` 与 `build_install_command()` 比——那是**代码与自己比**
        # （那个函数由 `test_build_install_command_is_the_install_tenant_invocation` 单独钉）。
        # 有判别力的是下面这条**字面量**断言：参数名与值都在。
        self.assertEqual(
            argv[3:],
            ["--schema", SCHEMA_A, "--name", NAME_A, "--domain", DOMAIN_A],
        )

    def test_rotate_password_statement_quotes_and_escapes(self):
        from apps.platform.db_provisioning import build_rotate_password_statement

        # 唯一新增的**原语**：标识符走 Identifier、密码走 Literal。
        rendered = build_rotate_password_statement(
            role=DATABASE_A, password="pw'1"
        ).as_string(None)
        self.assertEqual(
            rendered, f'ALTER ROLE "{DATABASE_A}" WITH PASSWORD \'pw\'\'1\''
        )


class ProvisionTenantDbInputGateTests(SimpleTestCase):
    """`--schema` 由使用者输入，且会被用来**派生库名与角色名** ⇒ 闸门必须在任何 DDL 之前。"""

    #: ⚠️ 第二条语句必须是**惰性**的（`SELECT 1`），**不许**改成 `DROP DATABASE …`：
    #: 这是「只在闸门坏掉时才真的跑起来」的 tripwire，而闸门一旦坏掉，本串会被拿去派生
    #: 库名/角色名并进到 `CREATE DATABASE`；psycopg 单次 execute 会执行多条语句（实测），
    #: 破坏性的第二条语句就会在**跑测试的时候**真的执行（Ruling 6）。形状（引号逃逸 +
    #: 语句分隔 + 行注释）必须保留，否则 tripwire 就没有形状了。
    INJECTION_SCHEMA = 'x"; SELECT 1; --'

    def test_injection_shaped_schema_is_rejected_before_any_output_or_ddl(self):
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=self.INJECTION_SCHEMA,
                name="注入探针",
                domain="inject-t4.test.local",
                stdout=out,
            )

        # 走 `FleetEntry` 的构造闸门（严格白名单），信息里点名那个值。
        self.assertIn(self.INJECTION_SCHEMA, str(ctx.exception))
        # 连「将要执行的 SQL」都没打印 ⇒ 拒绝发生在执行顺序第 2 步之前，更在任何 DDL 之前。
        self.assertEqual(out.getvalue(), "")

    def test_empty_emit_host_is_rejected_in_dry_run_too(self):
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_A,
                name=NAME_A,
                domain=DOMAIN_A,
                emit_host="",
                dry_run=True,
                stdout=out,
            )

        # 空串 = 「显式给了但变量没设」（`--emit-host "$APP_HOST"`）⇒ 必须响亮报错，
        # 不许静默回退成 Mac 侧 settings 的地址（那正是本任务要消除的那个）。
        self.assertIn("--emit-host", str(ctx.exception))
        # 「参数形状错」与「闸门未评估」是两件事：dry-run 也要拒，且拒在任何输出之前。
        self.assertEqual(out.getvalue(), "")

    def test_empty_emit_port_is_rejected_in_dry_run_too(self):
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_A,
                name=NAME_A,
                domain=DOMAIN_A,
                emit_port="",
                dry_run=True,
                stdout=out,
            )

        self.assertIn("--emit-port", str(ctx.exception))
        self.assertEqual(out.getvalue(), "")


class ProvisionTenantDbFleetModeTests(SimpleTestCase):
    """`--fleet PATH`（走清单）：解析阶段是纯的，不需要数据库。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmpdir = tempfile.mkdtemp(prefix="prov-fleet-t4-")
        cls.addClassCleanup(shutil.rmtree, cls._tmpdir, True)

    def _write_manifest(self):
        manifest = Path(self._tmpdir) / "fleet.json"
        manifest.write_text(
            '{"tenants": [{"schema_name": "%s", "name": "清单里的名字", '
            '"domain": "%s"}]}' % (SCHEMA_A, DOMAIN_FLEET),
            encoding="utf-8",
        )
        return manifest

    def test_fleet_mode_reads_name_and_domain_from_the_manifest(self):
        manifest = self._write_manifest()
        out = StringIO()

        call_command(
            "provision_tenant_db",
            schema=SCHEMA_A,
            fleet=str(manifest),
            dry_run=True,
            stdout=out,
        )

        # 清单里的域名/名字（而不是命令行）被采用了——这条是清单分支的判据。
        self.assertIn(DOMAIN_FLEET, out.getvalue())
        self.assertIn("清单里的名字", out.getvalue())
        self.assertIn(f'CREATE DATABASE "{DATABASE_A}"', out.getvalue())

    def test_fleet_mode_rejects_explicit_name_and_domain(self):
        manifest = self._write_manifest()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_A,
                fleet=str(manifest),
                name="命令行名字",
                domain=DOMAIN_A,
                dry_run=True,
            )

        # 两个出处不许静默取一个（D-0080：静默取错比报错更糟）。
        message = str(ctx.exception)
        self.assertIn("--fleet", message)
        self.assertIn("--name", message)

    def test_fleet_mode_reports_a_schema_that_is_not_in_the_manifest(self):
        manifest = self._write_manifest()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_B,
                fleet=str(manifest),
                dry_run=True,
            )

        # `entry_for_schema` 的信息要原样带出来：点名要找的值与清单里现有的值。
        message = str(ctx.exception)
        self.assertIn(SCHEMA_B, message)
        self.assertIn(SCHEMA_A, message)


class ProvisionTenantDbEndToEndTests(TransactionTestCase):
    """端到端：真建库、真建角色、真在**空库**里跑 `install_tenant`（子进程）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmpdir = tempfile.mkdtemp(prefix="prov-tenant-db-t4-")
        # 注册在 setUpClass 里：后面某个用例把类打挂，清理照跑。
        cls.addClassCleanup(shutil.rmtree, cls._tmpdir, True)
        for schema_name in (SCHEMA_A, SCHEMA_B, SCHEMA_ROTATE, SCHEMA_UNTOUCHED, SCHEMA_WS):
            cls.addClassCleanup(_purge_tenant_database, schema_name)

    def test_provisions_installs_and_denies_cross_database_connect(self):
        # 这条是**真正空库**的端到端：「install_tenant 在真正空库上没被跑过」
        # 这件事 defer 给 （在既有测试库里，它的迁移步骤是幂等的 no-op）。
        # 本用例建的是一个**全新的库**，所以 install_tenant 的 public 迁移 + 建租户 schema
        # 两条路都被真的走了一遍。
        _assert_derivation(self, SCHEMA_A, DATABASE_A)
        env_a = os.path.join(self._tmpdir, "a.env")

        call_command(
            "provision_tenant_db",
            schema=SCHEMA_A,
            name=NAME_A,
            domain=DOMAIN_A,
            emit_env=env_a,
            # 显式给**应用机视角**的地址：本任务唯一可观察的行为就是这两个值落进凭据文件。
            emit_host=EMIT_HOST_LITERAL,
            emit_port=EMIT_PORT_LITERAL,
            stdout=StringIO(),
        )

        self.assertTrue(database_exists(DATABASE_A))
        self.assertTrue(role_exists(DATABASE_A))
        # 凭据文件必须是 0600（同组/其他用户不可读）。
        self.assertEqual(stat.S_IMODE(os.stat(env_a).st_mode), 0o600)
        password_a = _read_env_value(env_a, "POSTGRES_PASSWORD")
        # CLI→handle 接线（F1）：`--emit-host/--emit-port` 必须**真的落进写出的文件**。
        # 若 handle 的调用点退回 `build_install_env`（读 settings）或忽略这两个参数，这里会红。
        # ⚠️ 探针值刻意与 settings 默认值不同形（EMIT_* 常量），否则两种实现都能通过。
        self.assertEqual(_read_env_value(env_a, "POSTGRES_HOST"), EMIT_HOST_LITERAL)
        self.assertEqual(_read_env_value(env_a, "POSTGRES_PORT"), EMIT_PORT_LITERAL)

        # 正向对照：用**租户自己的角色与密码**连它自己的库，库里真的有自己的目录行。
        # 判据用 schema_name / domain（两边取值不同），不用主键——两个库的 id 序列各自从 1 开始。
        with _connect(DATABASE_A, DATABASE_A, password_a) as conn:
            tenants = conn.execute(
                "SELECT schema_name, name FROM platform_tenant WHERE schema_name = %s",
                (SCHEMA_A,),
            ).fetchall()
            domains = conn.execute(
                "SELECT domain FROM platform_domain WHERE domain = %s", (DOMAIN_A,)
            ).fetchall()
            self.assertIsNone(conn.execute("SELECT to_regclass('public.settlement_control_authorization')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass(%s)", (f'{SCHEMA_A}.settlement_control_authorization',)).fetchone()[0])
            conn.execute(sql.SQL("INSERT INTO {}.settlement_control_authorization (client,request_id,payload_hash,ciphertext,app_id,app_name,phase,status,next_action,unknown_action,review_code,configured,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())").format(sql.Identifier(SCHEMA_A)), ('local', 'a0000000-0000-4000-8000-000000000001', 'a', b'a', '', '仅甲工作室', 'new', 'pending', 'agreement', '', '', False))
        self.assertEqual(tenants, [(SCHEMA_A, NAME_A)])
        self.assertEqual(domains, [(DOMAIN_A,)])

        # 第二个租户库：红线目标。
        # ⛔ 不许拿 `POSTGRES_ADMIN_DB`（`postgres` 库）当红灯目标——Ruling 1 实测那个库的
        # CONNECT 默认给 PUBLIC、本线从不 REVOKE 它，租户角色**连得上**，拿它当红灯会假绿。
        env_b = os.path.join(self._tmpdir, "b.env")
        call_command(
            "provision_tenant_db",
            schema=SCHEMA_B,
            name=NAME_B,
            domain=DOMAIN_B,
            emit_env=env_b,
            stdout=StringIO(),
        )
        self.assertTrue(database_exists(DATABASE_B))
        password_b = _read_env_value(env_b, "POSTGRES_PASSWORD")
        with _connect(DATABASE_B, DATABASE_B, password_b) as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('public.settlement_control_authorization')").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT to_regclass(%s)", (f'{SCHEMA_B}.settlement_control_authorization',)).fetchone()[0])
            conn.execute(sql.SQL("INSERT INTO {}.settlement_control_authorization (client,request_id,payload_hash,ciphertext,app_id,app_name,phase,status,next_action,unknown_action,review_code,configured,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())").format(sql.Identifier(SCHEMA_B)), ('local', 'b0000000-0000-4000-8000-000000000002', 'b', b'b', '', '仅乙工作室', 'new', 'pending', 'agreement', '', '', False))
            names = conn.execute(sql.SQL("SELECT app_name FROM {}.settlement_control_authorization").format(sql.Identifier(SCHEMA_B))).fetchall()
            self.assertEqual(names, [('仅乙工作室',)])

        # 红线：同一个角色连另一个租户库 ⇒ 权限拒绝（不是「密码错」，是 CONNECT 被 REVOKE 了）。
        with self.assertRaises(psycopg.OperationalError) as ctx:
            _connect(DATABASE_B, DATABASE_A, password_a)
        self.assertIn("permission denied for database", str(ctx.exception))

    def test_dry_run_creates_nothing_and_prints_the_sql(self):
        _assert_derivation(self, SCHEMA_UNTOUCHED, DATABASE_UNTOUCHED)
        out = StringIO()

        call_command(
            "provision_tenant_db",
            schema=SCHEMA_UNTOUCHED,
            name=NAME_UNTOUCHED,
            domain=DOMAIN_UNTOUCHED,
            dry_run=True,
            stdout=out,
        )

        self.assertFalse(database_exists(DATABASE_UNTOUCHED))
        self.assertFalse(role_exists(DATABASE_UNTOUCHED))

        # 打印的 SQL 要被逐字钉住（标识符带引号），且**密码脱敏**：
        # stdout 会进终端/CI 日志，真实凭据只该落在 0600 的 --emit-env 文件里。
        printed = out.getvalue()
        self.assertIn(
            f'CREATE DATABASE "{DATABASE_UNTOUCHED}" OWNER "{DATABASE_UNTOUCHED}" '
            "TEMPLATE template0",
            printed,
        )
        self.assertIn("PASSWORD '***'", printed)
        self.assertIn(
            f'REVOKE ALL ON DATABASE "{DATABASE_UNTOUCHED}" FROM PUBLIC', printed
        )

    def test_emit_env_refuses_an_existing_file_without_force_before_any_ddl(self):
        path = os.path.join(self._tmpdir, "taken.env")
        Path(path).write_text("OLD-CONTENT\n", encoding="utf-8")

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_UNTOUCHED,
                name=NAME_UNTOUCHED,
                domain=DOMAIN_UNTOUCHED,
                emit_env=path,
                # 命令在闸门之前会打印 SQL（执行顺序第 2 步）⇒ 必须接住它的 stdout，
                # 否则这段输出会漏进测试运行器的输出里。
                stdout=StringIO(),
            )

        message = str(ctx.exception)
        self.assertIn("--force", message)
        self.assertIn(path, message)
        # 文件一个字节都没变。
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "OLD-CONTENT\n")
        # 且在**任何 DDL 之前**就拒了：没建库、没建角色。
        self.assertFalse(database_exists(DATABASE_UNTOUCHED))
        self.assertFalse(role_exists(DATABASE_UNTOUCHED))

    def test_empty_emit_host_is_rejected_before_any_ddl_or_file(self):
        path = os.path.join(self._tmpdir, "empty-host.env")
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_UNTOUCHED,
                name=NAME_UNTOUCHED,
                domain=DOMAIN_UNTOUCHED,
                emit_env=path,
                emit_host="",
                stdout=out,
            )

        # 点名字段，且信息里说清「空串会回退成 Mac 侧地址」这个危险动作。
        self.assertIn("--emit-host", str(ctx.exception))
        # 形状闸门在任何输出 / 文件 / DDL 之前（与注入闸门同口径）。
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(database_exists(DATABASE_UNTOUCHED))
        self.assertFalse(role_exists(DATABASE_UNTOUCHED))

    def test_empty_emit_port_is_rejected_before_any_ddl_or_file(self):
        path = os.path.join(self._tmpdir, "empty-port.env")
        out = StringIO()

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_UNTOUCHED,
                name=NAME_UNTOUCHED,
                domain=DOMAIN_UNTOUCHED,
                emit_env=path,
                emit_port="",
                stdout=out,
            )

        self.assertIn("--emit-port", str(ctx.exception))
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(database_exists(DATABASE_UNTOUCHED))
        self.assertFalse(role_exists(DATABASE_UNTOUCHED))

    def test_rerun_requires_rotate_password_and_rotation_really_sets_the_credential(self):
        _assert_derivation(self, SCHEMA_ROTATE, DATABASE_ROTATE)
        env_path = os.path.join(self._tmpdir, "rotate.env")

        call_command(
            "provision_tenant_db",
            schema=SCHEMA_ROTATE,
            name=NAME_ROTATE,
            domain=DOMAIN_ROTATE,
            emit_env=env_path,
            stdout=StringIO(),
        )
        password_before = _read_env_value(env_path, "POSTGRES_PASSWORD")
        # 硬口径：两个新参数**都不给**时，文件里的 HOST/PORT 与今天逐字相同（回退到 settings）。
        # 这条走的是真 CLI（不是纯函数），所以「不给就回退」这条路也有可观察判据守着。
        self.assertEqual(
            _read_env_value(env_path, "POSTGRES_HOST"),
            str(settings.DATABASES["default"]["HOST"]),
        )
        self.assertEqual(
            _read_env_value(env_path, "POSTGRES_PORT"),
            str(settings.DATABASES["default"]["PORT"]),
        )
        stored_before = _role_password_hash(DATABASE_ROTATE)
        self.assertTrue(_scram_password_matches(password_before, stored_before))

        # 第二次重跑：`provision_database` 对已存在的角色是**跳过** ⇒ 本次生成的密码
        # 并没有写进库里。带 `--emit-env` 时必须响亮失败（点名 --rotate-password），
        # 否则就是写出一份「库里不认」的凭据文件（Ruling 2 / R5 / D-0080）。
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_ROTATE,
                name=NAME_ROTATE,
                domain=DOMAIN_ROTATE,
                emit_env=env_path,
                stdout=StringIO(),
            )
        self.assertIn("--rotate-password", str(ctx.exception))
        # 文件与库里的密码都没被这次失败改动。
        self.assertEqual(_read_env_value(env_path, "POSTGRES_PASSWORD"), password_before)
        self.assertEqual(_role_password_hash(DATABASE_ROTATE), stored_before)

        # 再加 `--force` 也不行：--force 只解决「文件已存在」，**不能**把「凭据不是本次设的」
        # 这条挡掉（R5）。少了这条断言，一个「先查文件、--force 之后跳过轮换闸门」的实现会
        # 静默写出一份库里不认的密码。
        with self.assertRaises(CommandError) as ctx_force:
            call_command(
                "provision_tenant_db",
                schema=SCHEMA_ROTATE,
                name=NAME_ROTATE,
                domain=DOMAIN_ROTATE,
                emit_env=env_path,
                force=True,
                stdout=StringIO(),
            )
        self.assertIn("--rotate-password", str(ctx_force.exception))
        self.assertEqual(_read_env_value(env_path, "POSTGRES_PASSWORD"), password_before)
        self.assertEqual(_role_password_hash(DATABASE_ROTATE), stored_before)

        # 第三次：显式轮换 + 覆盖文件 ⇒ 成功，且新密码**真的**写进了角色。
        os.chmod(env_path, 0o644)  # 故意放松：--force 覆盖后必须收回 0600
        call_command(
            "provision_tenant_db",
            schema=SCHEMA_ROTATE,
            name=NAME_ROTATE,
            domain=DOMAIN_ROTATE,
            emit_env=env_path,
            rotate_password=True,
            force=True,
            stdout=StringIO(),
        )
        password_after = _read_env_value(env_path, "POSTGRES_PASSWORD")
        stored_after = _role_password_hash(DATABASE_ROTATE)
        self.assertNotEqual(password_after, password_before)
        self.assertNotEqual(stored_after, stored_before)
        self.assertEqual(stat.S_IMODE(os.stat(env_path).st_mode), 0o600)

        # brief 字面要求的「用新密码建连接、断言连上」——⚠️ 本机 TCP 是 trust，
        # 单凭「连上了」任何密码都成立（实测：拿错密码照样连上）。真正有判别力的是下面两条
        # SCRAM 存储键断言：新密码对得上、旧密码对不上。
        with _connect(DATABASE_ROTATE, DATABASE_ROTATE, password_after) as conn:
            self.assertEqual(
                conn.execute("SELECT current_user").fetchone(), (DATABASE_ROTATE,)
            )
        self.assertTrue(_scram_password_matches(password_after, stored_after))
        self.assertFalse(_scram_password_matches(password_before, stored_after))

    def test_emit_address_whitespace_is_stripped_before_it_is_written(self):
        """空白闸门用 `strip()` 判、落盘也必须用 `strip()` 后的值。

        真实触发场景：从控制台复制内网地址常带尾随/前导空格（`--emit-host " 10.9.0.7 "`）。
        若闸门只判 `str(value).strip()` 而落盘写原值，命令**过闸门**却把带空格的地址
        写进 0600 凭据文件 ⇒ 租户机器拿到连不上、且极难查（地址肉眼看是对的）。
        判据打在**写出的文件**上（不是内部返回值），因为文件才是租户机器拿到的东西。
        """
        _assert_derivation(self, SCHEMA_WS, DATABASE_WS)
        env_path = os.path.join(self._tmpdir, "ws.env")

        call_command(
            "provision_tenant_db",
            schema=SCHEMA_WS,
            name="空白探针",
            domain=DOMAIN_WS,
            emit_env=env_path,
            emit_host=f"  {EMIT_HOST_LITERAL}  ",
            emit_port=f"\t{EMIT_PORT_LITERAL}\t",
            stdout=StringIO(),
        )

        # 逐字断言：写出的值不带任何首尾空白（tab 也算空白）。
        self.assertEqual(_read_env_value(env_path, "POSTGRES_HOST"), EMIT_HOST_LITERAL)
        self.assertEqual(_read_env_value(env_path, "POSTGRES_PORT"), EMIT_PORT_LITERAL)


def _purge_tenant_database(schema_name):
    """删掉探针的库与角色（幂等：从没建过也安全）。

    清理不是断言：`confirm` 用派生值，保证「字面量抄错」时也不会漏删一个真库。
    """
    drop_tenant_database(
        schema_name=schema_name, confirm=tenant_database_name(schema_name)
    )
