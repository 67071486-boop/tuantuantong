"""跨租户库不可达：本线隔离承诺的**可执行表述**（Task 6）。

本文件**不写生产代码**。Task 1 的 `apps/platform/db_provisioning.py` 已经把隔离做出来了
（每租户一个属主角色 + `REVOKE ALL ON DATABASE … FROM PUBLIC`，那是本线计划「已实测的事实」
第 1 条指认的承重点），这里只把它的效果钉成判据 —— 本任务是一条**判断**，不是一条功能。

⚠️ **基类必须是 `TransactionTestCase`（R1）**：`CREATE DATABASE` / `CREATE ROLE` **不能在事务块里跑**，
而 `TestCase` / `TenantTestCase` 会把用例包在外层事务里。Task 1 的探针实测踩过这条。

⚠️ **本文件声称的边界（R4）**：它证明的是「**租户角色**在**租户库之间**不可达」。
它**不**声称运维身份（`POSTGRES_ADMIN_USER`）也被关住 —— 那个身份**按设计**能连每一个租户库
（Task 5 的巡检正是靠它逐库读目录），所以它**绝不能出现在任何租户服务器上**。

⚠️ **已知残差（R3，如实钉住，不是漏掉）**：租户角色**连得上**集群维护库 `postgres` 与 `template1`
—— 它们的 `CONNECT` 默认给 `PUBLIC`，本线**刻意不去收**（里面没有租户数据）。
`test_maintenance_databases_are_a_pinned_residual` 就是这句话的可执行版本：它将来若变红，
**不许只改断言** —— 要同改本计划（`docs/plans/2026-09-23-per-tenant-database-isolation.md`）
Task 6 判据 5 与 Task 7 的 RUNBOOK 收口清单（第 7、11–13 条），以及 `docs/RUNBOOK.md` 的残差表述。
⛔ 本文件**不写**「租户角色连不上任何别的库」—— 那句绝对话**已经为假**（`postgres` 就是反例；
`AGENTS.md` §5：能被一个反例证伪的绝对句比不写更糟）。

⚠️ **探针名一律带本会话片段**（从测试库名派生，见 `_session_segment()`）：database / role 是
**集群级**对象（不是库内对象），而 `TENANT_DB_PREFIX` 默认 `tt_`、每个 worktree 都一样 ⇒
写死的名字会与并行会话互踩。后果不只是「报错」：`drop_tenant_database` 会
`DROP DATABASE … WITH (FORCE)`，一个会话的清理能掐掉另一个会话的探针库并把它删掉
（两个套件同时跑就会撞上）。本仓约定用 `POSTGRES_DB=<会话后缀>` 跑测试 ⇒
`settings.DATABASES["default"]["NAME"]` 已经**会话唯一**，探针片段就从它派生。

⚠️ **清理**：`addClassCleanup`（注册在**建对象之前**，`setUpClass` 半途失败也照删）里逐 schema 调
`drop_tenant_database` —— 它先 `DROP DATABASE … WITH (FORCE)` 再 `DROP ROLE`（角色还被库占用时
删不掉，顺序不能反），两步都带 `IF EXISTS`，所以「从没建过」也安全。

⚠️ **注入形状探针的第二条语句是惰性的**（`SELECT 1`，见 `INJECTION_SCHEMA`）：本文件会**真建库建角色**，
闸门一旦失效，一条带破坏性的第二条语句就会在**跑测试的时候**真的执行（Ruling 6 / 全局约束 5）。

本机的一点前提（Task 4 实测，见 `tests/test_provision_tenant_db.py` 的模块 docstring）：
TCP 是 `trust` ⇒ 「连得上」与「密码对不对」无关。所以隔离判据一律落在 **CONNECT 权限**上，
密码只是连接参数 —— 下面写死字面量，便于复现失败输出。

判据与用例的对应（brief 的 1–5）：

1. 正向（自己的库可达）…… `test_tenant_role_reaches_its_own_database`
2. 红线（另一个租户的库不可达）…… `test_tenant_role_cannot_reach_another_tenants_database`
3. 权限判据（`CONNECT` / `TEMP` 为假）…… `test_connect_privilege_is_false_for_another_tenants_database`
4. 反向对照（**双向**）…… `test_the_reachability_predicate_is_two_way`
5. 已知残差（维护库仍可达）…… `test_maintenance_databases_are_a_pinned_residual`
"""
import re

import psycopg
from django.conf import settings
from django.test import TransactionTestCase
from psycopg import sql

from apps.platform.db_provisioning import (
    admin_connection_params,
    build_provision_statements,
    database_exists,
    drop_tenant_database,
    provision_database,
    role_exists,
    tenant_database_name,
    tenant_role_name,
)
from apps.platform.identifiers import InvalidIdentifierError


def _session_segment() -> str:
    """本会话的探针片段：从**本会话测试库名**派生（`settings.DATABASES["default"]["NAME"]`）。

    database / role 是**集群级**对象；硬编码的 `_tN` 只是**任务**后缀、不是**会话**后缀
    （`TENANT_DB_PREFIX` 默认 `tt_`，每个 worktree 都一样）⇒ 并行会话会互踩，而
    `drop_tenant_database` 的 `DROP DATABASE … WITH (FORCE)` 会让一方**删掉**另一方的探针库。
    本仓跑测试的约定是 `POSTGRES_DB=<会话后缀>` ⇒ 这个值已经会话唯一；这里只清洗成合法
    标识符并截断（给 `tt_` + schema 名留长度）。
    """
    raw = re.sub(r"[^a-z0-9_]", "_", str(settings.DATABASES["default"]["NAME"]).lower())
    segment = raw.strip("_") or "s"
    if segment[0].isdigit():
        segment = "s" + segment
    return segment[:32]


SEGMENT = _session_segment()

# ── 探针（schema 名带会话片段；**不从被测代码里取常量**，避免自指断言 E-0024）──────────
SCHEMA_A = f"iso_a_{SEGMENT}"
SCHEMA_B = f"iso_b_{SEGMENT}"
SCHEMA_OPEN = f"iso_open_{SEGMENT}"

#: 派生结果的**字面量**（`tt_` 前缀写死、会话片段由上面的派生给出）。
#: 派生规则若漂移，`test_probe_names_match_the_derivation_rule` 先红——它比的是
#: 「写死 `tt_` 的字面量」与「`tenant_database_name()` 的实际输出」，
#: 所以**前缀真被改坏时这条会红**（会话片段两边都派生、不参与判别）。
DATABASE_A = f"tt_iso_a_{SEGMENT}"
DATABASE_B = f"tt_iso_b_{SEGMENT}"
DATABASE_OPEN = f"tt_iso_open_{SEGMENT}"
ROLE_A = f"tt_iso_a_{SEGMENT}"
ROLE_B = f"tt_iso_b_{SEGMENT}"
ROLE_OPEN = f"tt_iso_open_{SEGMENT}"

PASSWORD_A = "pw-iso-a"
PASSWORD_B = "pw-iso-b"
PASSWORD_OPEN = "pw-iso-open"

#: 集群维护库（R3 的残差）：`CONNECT` 默认给 `PUBLIC`，本线刻意不 REVOKE。
MAINTENANCE_DB = "postgres"
TEMPLATE_DB = "template1"

#: ⛔ 注入形状探针 —— 第二条语句**必须是惰性的** `SELECT 1`，**不许**换成 `DROP DATABASE …`：
#: 本文件会真建库建角色，闸门一旦失效，这条串会进到 `CREATE DATABASE`/`CREATE ROLE`，
#: 而破坏性的第二条语句就会在跑测试时真的执行（Ruling 6）。
#: 形状（引号逃逸 + 语句分隔 + 行注释）必须保留，否则它就不再是一条 tripwire。
INJECTION_SCHEMA = 'x"; SELECT 1; --'


def _injection_derived_name() -> str:
    """闸门失效时**派生**这条路会拿到的名字（`{TENANT_DB_PREFIX}{schema}`，逐字拼接）。

    这里用 `settings` 逐字拼、**不调** `tenant_database_name`：后者自己就带闸门，
    压根产不出这个名字，而本函数要在「闸门坏了」的假设下工作（否定断言 + 清理共用）。
    """
    return f"{settings.TENANT_DB_PREFIX}{INJECTION_SCHEMA}"


def _connect(database, user, password):
    """用给定身份连给定库（host / port 与应用库是**同一台**服务器）。"""
    application_database = settings.DATABASES["default"]
    return psycopg.connect(
        host=str(application_database["HOST"]),
        port=str(application_database["PORT"]),
        dbname=database,
        user=user,
        password=password,
        connect_timeout=5,
    )


def _reachable_from_role_a(database) -> bool:
    """判据一：**租户 A 的角色**能不能连上 `database`（真建连接，不是查权限表）。

    ⚠️ 这个谓词**两个方向都会被断言**（R2）：未 REVOKE 的库为 `True`、已 REVOKE 且不在白名单的库
    为 `False`。只断言一个方向时，一个「恒定回答同一个答案」的谓词照样能让用例全绿 ——
    那正是 Task 5 评审 Minor 1 记下的盲区。
    """
    try:
        with _connect(database, ROLE_A, PASSWORD_A):
            return True
    except psycopg.OperationalError:
        return False


def _public_has_connect(database) -> bool:
    """判据二（与巡检同形）：`PUBLIC` 在这个库上还留着 `CONNECT` 吗。

    这是巡检命令 `fleet_status` 泄漏探测所依据的**同一个事实**（那条命令现居独立项目
    `../ttt-platform`），但这里**直接问集群**
    （`SELECT has_database_privilege('public', %s, 'CONNECT')`），不经那条命令 ——
    巡检命令不能当自己的 oracle。同样**两个方向都会被断言**。
    """
    with psycopg.connect(
        **admin_connection_params(), autocommit=True, connect_timeout=5
    ) as conn:
        row = conn.execute(
            "SELECT has_database_privilege('public', %s, 'CONNECT')", (database,)
        ).fetchone()
    return bool(row[0])


def _has_privilege(role, database, privilege) -> bool:
    """`has_database_privilege(role, database, privilege)` —— 与连接侧**独立**的第二条判据。"""
    with psycopg.connect(
        **admin_connection_params(), autocommit=True, connect_timeout=5
    ) as conn:
        row = conn.execute(
            "SELECT has_database_privilege(%s, %s, %s)", (role, database, privilege)
        ).fetchone()
    return bool(row[0])


def _provision_database_without_the_revoke() -> None:
    """建一个**不做 REVOKE** 的租户库 —— 反向对照（R2）的正向一半。

    用的仍是 Task 1 的 `build_provision_statements`，只执行到 **`REVOKE` 之前**——
    四条里的**前三条**（角色 → 成员资格 → 库）。这个对照的全部意义在「**只差那一句**」：
    第四条是不是那句 REVOKE 由 `test_the_reachability_predicate_is_two_way` 逐字钉住，
    这里跳过它（跳过的是**最后一条**，不是「随便少一条」）。

    刻意不用 `provision_database`：它对已存在的库**无条件**执行 REVOKE（Task 1 docstring
    第 2 段），所以拿它建不出「没被 REVOKE 的库」，而这个对照要的正是那个库。
    """
    (
        create_role,
        grant_membership,
        create_database,
        _revoke_public,
    ) = build_provision_statements(
        role=ROLE_OPEN,
        database=DATABASE_OPEN,
        password=PASSWORD_OPEN,
        operator=settings.POSTGRES_ADMIN_USER,
    )
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        # 先 `DROP … IF EXISTS`：上一次被中断（SIGKILL）的运行会留下同名库/角色，
        # 而 `CREATE` 撞名会直接报错、让整个 `setUpClass` 崩在别人留下的残骸上。
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DATABASE_OPEN)
            )
        )
        conn.execute(
            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(ROLE_OPEN))
        )
        with conn.cursor() as cur:
            cur.execute(create_role)
        # 成员资格也要执行：这个对照与「完整供给」应当**只差那句 REVOKE**。
        with conn.cursor() as cur:
            cur.execute(grant_membership)
        with conn.cursor() as cur:
            cur.execute(create_database)


def _purge_tenant_database(schema_name) -> None:
    """删掉探针的库与角色（幂等：从没建过也安全）。

    清理不是断言：`confirm` 用派生值，保证「字面量抄错」时也不会漏删一个真库。
    顺序由 `drop_tenant_database` 内部保证：先 `DROP DATABASE … WITH (FORCE)`、再 `DROP ROLE`。
    """
    drop_tenant_database(
        schema_name=schema_name, confirm=tenant_database_name(schema_name)
    )


def _drop_objects_by_literal_name(name) -> None:
    """按**字面名字**删库与角色，不走 `drop_tenant_database` 的确认串/闸门。

    只给注入形状探针用：正常路径下这些对象不存在（no-op），只有**闸门失效**时才会有东西要清，
    而那种名字过不了 `validate_schema_name`，只能从这条缝里删。SQL 仍走 `sql.Identifier`，
    本文件不拼标识符。顺序同上：先库后角色。
    """
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


class TenantDatabaseIsolationTests(TransactionTestCase):
    """判据 1–5：真建三个库（两个 REVOKE 过、一个**故意不 REVOKE**），全部打在真集群上。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 清理**先注册**：即便下面的供给半途失败，已经建出来的库/角色也会被删掉。
        for schema_name in (SCHEMA_A, SCHEMA_B, SCHEMA_OPEN):
            cls.addClassCleanup(_purge_tenant_database, schema_name)

        # 租户 A / B：走 `provision_database`（角色 → 成员资格 → 库 → REVOKE 四条全跑）。
        provision_database(schema_name=SCHEMA_A, password=PASSWORD_A)
        provision_database(schema_name=SCHEMA_B, password=PASSWORD_B)
        # 反向对照：同样两个对象，但**只执行到 REVOKE 之前**（前三条，少了那句 REVOKE）。
        _provision_database_without_the_revoke()

    def test_probe_names_match_the_derivation_rule(self):
        # 这条不测隔离，它保证上面那些**字面量**真的就是派生函数产出的名字：
        # 字面量抄错会让整个文件在测「别的库」，而那种错是静默的。
        self.assertEqual(tenant_database_name(SCHEMA_A), DATABASE_A)
        self.assertEqual(tenant_database_name(SCHEMA_B), DATABASE_B)
        self.assertEqual(tenant_database_name(SCHEMA_OPEN), DATABASE_OPEN)
        self.assertEqual(tenant_role_name(SCHEMA_A), ROLE_A)
        self.assertEqual(tenant_role_name(SCHEMA_B), ROLE_B)
        self.assertEqual(tenant_role_name(SCHEMA_OPEN), ROLE_OPEN)
        # 探针库/角色真的建出来了（否则下面的否定断言会因为「库不存在」而假绿）。
        for database in (DATABASE_A, DATABASE_B, DATABASE_OPEN):
            with self.subTest(database=database):
                self.assertTrue(database_exists(database))
                self.assertTrue(role_exists(database))

    def test_tenant_role_reaches_its_own_database(self):
        """判据 1（正向）：租户角色连**自己的库**成功。"""
        with _connect(DATABASE_A, ROLE_A, PASSWORD_A) as conn:
            who = conn.execute("SELECT current_user, current_database()").fetchone()
        # 逐字钉住「以谁的身份连上了哪个库」——只断言「连上了」在 `trust` 集群上判别力更弱。
        self.assertEqual(who, (ROLE_A, DATABASE_A))
        # 同一个事实的第二条独立判据：属主对自己那个库**确实**有 CONNECT。
        # 少了这条，下面那条 `False` 可能是「判据恒定回答 False」——R2 要排除的正是这种。
        self.assertIs(
            _has_privilege(ROLE_A, DATABASE_A, "CONNECT"),
            True,
            f"{ROLE_A} 对自己那个库 {DATABASE_A} 应当有 CONNECT",
        )

    def test_tenant_role_cannot_reach_another_tenants_database(self):
        """判据 2（红线）：租户 A 的角色连租户 B 的库 ⇒ 权限拒绝（不是「密码错」）。"""
        with self.assertRaises(psycopg.OperationalError) as ctx:
            _connect(DATABASE_B, ROLE_A, PASSWORD_A)
        message = str(ctx.exception)
        self.assertIn("permission denied for database", message)
        # 报错必须点名**目标库**：否则这条「拒绝」可能来自别的原因（写错 user/host 也会失败）。
        self.assertIn(DATABASE_B, message)

    def test_connect_privilege_is_false_for_another_tenants_database(self):
        """判据 3（权限判据）：与连接侧**独立**的第二条证据 —— CONNECT 与 TEMP 都为假。"""
        self.assertIs(
            _has_privilege(ROLE_A, DATABASE_B, "CONNECT"),
            False,
            f"{ROLE_A} 对别人的库 {DATABASE_B} 不该有 CONNECT",
        )
        self.assertIs(
            _has_privilege(ROLE_A, DATABASE_B, "TEMP"),
            False,
            f"{ROLE_A} 对别人的库 {DATABASE_B} 不该有 TEMP",
        )

    def test_the_reachability_predicate_is_two_way(self):
        """判据 4（反向对照，**双向**）：未 REVOKE ⇒ 可达；已 REVOKE 且不在白名单 ⇒ 不可达。

        正向那一半证明「隔离来自那句 REVOKE」，而不是「反正谁都连不上」；
        负向那一半排除「判据恒定回答同一个答案」—— 少了它，一个把 `PUBLIC` 是否可连读成永远
        `True` 的巡检会把每个治理好的库都误报成泄漏（Task 5 评审 Minor 1 的假阳性盲区）。
        """
        # 先证明两个对照**只差那一句 REVOKE**：那个「不 REVOKE」的库也是用 同一套原语建的，
        # 四条语句逐字如下，本文件只执行到 REVOKE **之前**（前三条）。差异不是「库的形态不同」。
        self.assertEqual(
            [
                statement.as_string(None)
                for statement in build_provision_statements(
                    role=ROLE_OPEN,
                    database=DATABASE_OPEN,
                    password=PASSWORD_OPEN,
                    operator=settings.POSTGRES_ADMIN_USER,
                )
            ],
            [
                f'CREATE ROLE "{ROLE_OPEN}" LOGIN PASSWORD \'{PASSWORD_OPEN}\'',
                f'GRANT "{ROLE_OPEN}" TO "{settings.POSTGRES_ADMIN_USER}"',
                f'CREATE DATABASE "{DATABASE_OPEN}" OWNER "{ROLE_OPEN}" TEMPLATE template0',
                f'REVOKE ALL ON DATABASE "{DATABASE_OPEN}" FROM PUBLIC',
            ],
        )

        # 前置：两个探针库都**不在泄漏白名单**里（白名单 = 集群维护库 + 本项目自己的库，
        # 见 `fleet_status.leak_whitelist`，现居 `../ttt-platform`）⇒ 它们正是「未 REVOKE 就该被报出来」
        # 那一类。这里逐字列出而不 import 既有代码：本文件的判据只打 同一套原语
        # （brief 的接口约束）。
        for database in (DATABASE_OPEN, DATABASE_B):
            with self.subTest(database=database):
                self.assertNotIn(database, ("postgres", "template0", "template1"))
                self.assertNotEqual(
                    database, str(settings.DATABASES["default"]["NAME"])
                )

        # 连接侧：不归 role_a 所有、也不在任何白名单里的两个库，答案必须**相反**。
        self.assertIs(
            _reachable_from_role_a(DATABASE_OPEN),
            True,
            f"未 REVOKE 的 {DATABASE_OPEN} 必须连得上（否则隔离不是来自那句 REVOKE）",
        )
        self.assertIs(
            _reachable_from_role_a(DATABASE_B),
            False,
            f"已 REVOKE 的 {DATABASE_B} 必须连不上",
        )

        # 泄漏探测侧（巡检用的同一个事实）：同样必须**相反**。
        self.assertIs(
            _public_has_connect(DATABASE_OPEN),
            True,
            f"未 REVOKE 的 {DATABASE_OPEN} 的 PUBLIC 应当仍开着 CONNECT",
        )
        self.assertIs(
            _public_has_connect(DATABASE_B),
            False,
            f"已 REVOKE 的 {DATABASE_B} 的 PUBLIC 不该还有 CONNECT",
        )

    def test_maintenance_databases_are_a_pinned_residual(self):
        """判据 5（R3）：如实钉住「维护库仍对租户角色敞开」这条**已知残差**。

        它**不是**租户数据破口（维护库里没有租户数据），但它是「本线隔离承诺只覆盖
        **租户库之间**」这句话的可执行版本。将来有人收紧 `postgres` / `template1` 时这条会红 ——
        **那是该红的**；改它时必须同改本计划 Task 6 判据 5、Task 7 的 RUNBOOK 收口清单
        （第 7、11–13 条）与 `docs/RUNBOOK.md` 的残差表述，**不许只改测试**。
        """
        # 机制侧：这两个库的 `CONNECT` 默认给 `PUBLIC`（`datacl` 为默认值），本线刻意不 REVOKE。
        self.assertIs(
            _public_has_connect(MAINTENANCE_DB),
            True,
            f"{MAINTENANCE_DB} 的 PUBLIC 按设计仍开着 CONNECT（已知残差）",
        )
        self.assertIs(
            _public_has_connect(TEMPLATE_DB),
            True,
            f"{TEMPLATE_DB} 的 PUBLIC 按设计仍开着 CONNECT（已知残差）",
        )
        # 后果侧：租户角色**真的**连得上它们（连上并点名自己落在哪个库）。
        with _connect(MAINTENANCE_DB, ROLE_A, PASSWORD_A) as conn:
            self.assertEqual(
                conn.execute("SELECT current_database()").fetchone(),
                (MAINTENANCE_DB,),
            )
        with _connect(TEMPLATE_DB, ROLE_A, PASSWORD_A) as conn:
            self.assertEqual(
                conn.execute("SELECT current_database()").fetchone(), (TEMPLATE_DB,)
            )


class ProvisioningGateTests(TransactionTestCase):
    """闸门在**任何 DDL 之前**生效：注入形状的 schema 名建不出任何东西。

    本文件会真建库建角色，所以这条探针的**第二条语句是惰性的** `SELECT 1`（见 `INJECTION_SCHEMA`）：
    闸门一旦失效，带破坏性的第二条语句就会在跑测试时真的执行。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 正常路径下这两个名字都不存在 ⇒ no-op；只有**闸门失效**时才真的有东西要清。
        # 清理用的是字面名字，因为那种名字过不了 `validate_schema_name` 闸门
        # （`drop_tenant_database` 会先拒它）。
        cls.addClassCleanup(_drop_objects_by_literal_name, INJECTION_SCHEMA)
        cls.addClassCleanup(_drop_objects_by_literal_name, _injection_derived_name())

    def test_injection_shaped_schema_is_rejected_before_any_ddl(self):
        # 类内**正向对照**：这两个判据**能**回答 True（下面那几条 False 才不是「恒定回答 False」
        # 的假绿）。`postgres` 库与角色在集群里必然存在，不必新建对象。
        # ⚠️ 少了这一条，本类否定断言的 oracle 完全落在另一个类（TenantDatabaseIsolationTests）
        # 的对照上——只跑本类时就没有判别力了。
        self.assertTrue(database_exists(MAINTENANCE_DB), f"{MAINTENANCE_DB} 应当存在")
        self.assertTrue(role_exists("postgres"), "postgres 角色应当存在")

        with self.assertRaises(InvalidIdentifierError) as ctx:
            provision_database(schema_name=INJECTION_SCHEMA, password=PASSWORD_A)
        # 报错点名那个值（排障最少的可用信息）。
        self.assertIn(INJECTION_SCHEMA, str(ctx.exception))
        # 集群里**没有**这个名字的库/角色……
        self.assertFalse(database_exists(INJECTION_SCHEMA))
        self.assertFalse(role_exists(INJECTION_SCHEMA))
        # ……**也没有**它在派生之后的名字（派生是闸门失效时唯一真会走到 DDL 的那条路）。
        self.assertFalse(database_exists(_injection_derived_name()))
        self.assertFalse(role_exists(_injection_derived_name()))

    def test_injection_shaped_schema_is_rejected_before_any_connection(self):
        """同一条闸门也在**派生**这两个纯函数上生效（它们根本不会开连接）——
        注入探针走不到任何 DDL 的前提。"""
        with self.assertRaises(InvalidIdentifierError):
            tenant_database_name(INJECTION_SCHEMA)
        with self.assertRaises(InvalidIdentifierError):
            tenant_role_name(INJECTION_SCHEMA)
