"""每租户独立 database / 角色的供给原语（B 档隔离）。

隔离模型（本线的唯一红线）：一个租户 = 一个 PostgreSQL database + 一个同名角色，
该角色**只对自己那一个库**有 `CONNECT`——`REVOKE ALL ON DATABASE ... FROM PUBLIC`
回收的是 PUBLIC 的隐式 `CONNECT`，属主不受影响（计划「已实测的事实」第 1/2 条）。
PG 不支持跨库引用（事实 4），而 `dblink` / `postgres_fdw` 要超管（事实 5）⇒ 两条路
都堵上之后，跨租户在 PG 层**做不到**，而不是「权限不够」。

两条实现上的硬约束，改之前先读理由：

1. **必须用 `psycopg.connect(..., autocommit=True)` 直连，不能用 `django.db.connection`**：
   `CREATE DATABASE` / `CREATE ROLE` 不能在事务块里跑，而 Django 的连接在测试期
   正处在事务中。库名与角色名一律 `{TENANT_DB_PREFIX}{schema_name}` **派生**（不存储，
   存了必漂移），并且**所有**标识符走 `psycopg.sql.Identifier`——本模块任何地方都不许用
   f-string / `%` 去拼**要执行的 SQL 文本**。（用 f-string 拼出一个标识符**值**、
   再交给 `sql.Identifier` 转义是允许的，例如 `_derived_name` 里的前缀拼接——
   被禁的是把值直接插进 SQL 字符串。）
2. **四条语句的顺序固定**：角色 → 成员资格（`GRANT`）→ 库 → REVOKE。成员资格必须排在
   `CREATE DATABASE … OWNER <角色>` **之前**：PG 要求建库的执行者能 `SET ROLE` 到属主角色，
   而托管的运维身份（`pg_tencentdb_superuser`）不是真超管——本机 PG 16.15 实测，少了这一句
   报 `ERROR: must be able to SET ROLE`。REVOKE 打的是 `DATABASE` 对象，库还不存在时执行会
   直接报错，所以它必须留在最后。这四条的先后都不能换。
"""
import secrets
from collections.abc import Sequence

import psycopg
from django.conf import settings
from psycopg import sql

from apps.platform.identifiers import (
    InvalidIdentifierError,
    validate_identifier,
    validate_schema_name,
)


def _derived_name(schema_name: str, *, what: str) -> str:
    """`{TENANT_DB_PREFIX}{schema_name}`，并对**结果**再跑一次白名单。

    第二次校验不是形式：前缀会把长度顶过 63（`tt_` + 61 = 64），
    而 `schema_name` 自己单独看是合法的。
    """
    return validate_identifier(
        f"{settings.TENANT_DB_PREFIX}{validate_schema_name(schema_name)}", what=what
    )


def tenant_database_name(schema_name: str) -> str:
    """租户的 database 名 = `{TENANT_DB_PREFIX}{schema_name}`。"""
    return _derived_name(schema_name, what="database name")


def tenant_role_name(schema_name: str) -> str:
    """租户的 role 名 = 同一个值。PG 里 role / database / schema 是三个独立命名空间，同名合法。"""
    return _derived_name(schema_name, what="role name")


def admin_connection_params() -> dict[str, str]:
    """供给/巡检用的运维身份的 `psycopg.connect(**...)` 参数。

    主机与端口跟应用库**同一台**服务器（供给的是同一集群里的另一个库）；
    `dbname` 用 `POSTGRES_ADMIN_DB`（默认 `postgres`）——建库建角色在这个库上执行。
    本地/CI 没配独立运维角色时会回退到应用身份，此时供给会以
    `permission denied to create role` 明确失败（事实 6），而不是静默建错。
    """
    application_database = settings.DATABASES["default"]
    return {
        "host": str(application_database["HOST"]),
        "port": str(application_database["PORT"]),
        "dbname": str(settings.POSTGRES_ADMIN_DB),
        "user": str(settings.POSTGRES_ADMIN_USER),
        "password": str(settings.POSTGRES_ADMIN_PASSWORD),
    }


def build_provision_statements(
    *, role: str, database: str, password: str, operator: str
) -> list[sql.Composable]:
    """组装供给一个租户库所需的**四条**语句，顺序即执行顺序（见模块 docstring 第 2 条）。

    `operator` 刻意**必填、不给默认值**：它决定那条 `GRANT ... TO` 授权给谁，是本改动里
    唯一有安全后果的决定。给默认值会让调用方在没想清楚「用哪个运维身份」时**静默授权给
    `settings.POSTGRES_ADMIN_USER`**；值一律由调用点从 settings 取（纯函数不读 settings）。

    返回 `Composable` 而不是字符串：调用方交给 psycopg 执行，标识符的引号转义由
    `sql.Identifier` 负责（事实 13：`sql.Identifier('x"; DROP').as_string(None)`
    → `"x""; DROP"`），本模块不参与拼接。
    """
    return [
        # 角色：能登录 + 密码。不带 CREATEDB / CREATEROLE / SUPERUSER——租户角色不该有。
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        ),
        # 成员资格：**非真超管**的运维身份靠它才能执行下面那句 CREATE DATABASE ... OWNER
        # （PG 要求执行者能 SET ROLE 到属主角色）。本机 PG 16.15 实测：少了它报
        # `ERROR: must be able to SET ROLE "<租户角色>"`；补上它之后建库 / REVOKE / 巡检连接
        # 三件事一起解决（见 docs/RUNBOOK.md §2.5.1 的 实测补充）。
        # ⚠️ 代价：运维身份因此对所有租户角色都有成员资格——它本来就能连每个租户库。
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(role), sql.Identifier(operator)
        ),
        # 库：属主 = 该角色。`TEMPLATE template0`：template1 可能被别的会话占用，
        # 且 template0 是干净的（不带本地加装的东西）。
        sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
            sql.Identifier(database), sql.Identifier(role)
        ),
        # 隔离的**唯一**承重点：回收 PUBLIC 的隐式 CONNECT（事实 1）。
        # 漏掉这一句，任何角色都能连任何库，本线直接退化成「应用层约定」。
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
            sql.Identifier(database)
        ),
    ]


def role_exists(name: str) -> bool:
    """集群级角色是否已存在（会自己开一条 admin 连接）。"""
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        return _role_exists(conn, name)


def database_exists(name: str) -> bool:
    """database 是否已存在（会自己开一条 admin 连接）。"""
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        return _database_exists(conn, name)


def _role_exists(conn: psycopg.Connection, name: str) -> bool:
    """`role_exists` 的连接内版本：供给流程复用它，避免为每步各开一条连接。"""
    return (
        conn.execute(
            sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {}").format(
                sql.Literal(name)
            )
        ).fetchone()
        is not None
    )


def _database_exists(conn: psycopg.Connection, name: str) -> bool:
    """`database_exists` 的连接内版本，理由同上。"""
    return (
        conn.execute(
            sql.SQL("SELECT 1 FROM pg_database WHERE datname = {}").format(
                sql.Literal(name)
            )
        ).fetchone()
        is not None
    )


def generate_password() -> str:
    """给租户角色生成密码。URL-safe 字母表 ⇒ 可原样进 SQL 字面量、也可写进 `.env` 片段。"""
    return secrets.token_urlsafe(32)


def _apply_provision_statements(
    *, role: str, database: str, statements: Sequence[sql.Composable]
) -> None:
    """把 `build_provision_statements` 的四条语句按**固定顺序、幂等**地执行到集群上。

    这是 `provision_database`（租户）的执行
    半场：两条路的「逐条一致」由**同一份代码**保证，而不是靠两边各抄一遍（抄一遍必漂移，
    AGENTS §5-E 的 E-0027 同族）。四条语句的先后理由见模块 docstring 第 2 条。

    幂等语义（三方都必须一致，改这里就是改两条路的语义）：

    - `CREATE ROLE`：**仅当角色不存在**时执行（已存在时跳过，密码**不会**被重置）；
    - `GRANT`（成员资格）：**无条件**执行——角色已存在时也要补上，否则「先建过角色、再换
      非超管身份重跑」仍会卡在 `must be able to SET ROLE`；
    - `CREATE DATABASE`：**仅当库不存在**时执行；
    - `REVOKE ALL ON DATABASE ... FROM PUBLIC`：**无条件**执行——它是隔离红线的承重点，
      不能只跟着建库走（库可能是先于本函数存在的）。
    """
    (
        create_role,
        grant_membership,
        create_database,
        revoke_public,
    ) = statements

    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        if not _role_exists(conn, role):
            with conn.cursor() as cur:
                cur.execute(create_role)
        # 成员资格**无条件**执行（理由同 REVOKE）：角色已存在时要补上，重复执行也无害。
        with conn.cursor() as cur:
            cur.execute(grant_membership)
        # 角色的存在与库的存在分别判断：上一次供给可能在两者之间失败，重试要能补齐。
        if not _database_exists(conn, database):
            with conn.cursor() as cur:
                cur.execute(create_database)
        # REVOKE **无条件**执行：即使库是先于本函数存在的（或上一次供给半途失败），
        # 也要把 PUBLIC 的隐式 CONNECT 收掉——它才是隔离红线的承重点，不能只跟着建库走。
        with conn.cursor() as cur:
            cur.execute(revoke_public)


def provision_database(
    *, schema_name: str, password: str, operator: str | None = None
) -> None:
    """建租户的库与角色，补运维身份的成员资格，并收掉 PUBLIC 的 CONNECT。**幂等**：已存在的对象跳过、不抛。

    ⚠️ 幂等只覆盖「对象在不在」：目标角色**已存在**时密码**不会**被重置成本次传入的值
    （`CREATE ROLE` 被跳过）。调用方要保证「记下来的密码」与首次供给时的一致——
    重复供给不会把密码轮换过去。

    `operator` 是执行供给的运维角色（`GRANT <租户角色> TO <operator>` 的目标）。**不给**
    （`None`）时回退 `settings.POSTGRES_ADMIN_USER`，既有调用点因此不必全改。与非超管托管
    实例有关的那句 `GRANT` **无条件执行**（与 REVOKE 同规格）：角色**已存在**时也要补上成员
    资格，否则「先建过角色、再换非超管身份重跑」这条路仍会卡在 `must be able to SET ROLE`。

    `CREATE ROLE` / `CREATE DATABASE` 不能在事务里跑 ⇒ 本函数自己开一条 autocommit 连接，
    刻意**不**用 Django 的 `django.db.connection`（测试期它处在事务中）。
    """
    # 先派生（= 先过闸门）：不合法的名字在连库之前就该被拒绝。
    database = tenant_database_name(schema_name)
    role = tenant_role_name(schema_name)
    if operator is None:
        operator = str(settings.POSTGRES_ADMIN_USER)
    _apply_provision_statements(
        role=role,
        database=database,
        statements=build_provision_statements(
            role=role, database=database, password=password, operator=operator
        ),
    )


def drop_tenant_database(*, schema_name: str, confirm: str) -> None:
    """删掉租户的库与角色。`confirm` 必须**逐字等于库名**（如 `tt_dev`），否则拒绝。

    确认串刻意是**库名**而不是 schema 名（`dev`）：最容易犯的手滑正是拿 schema 名来确认，
    而这两个值长得很像。先用 `DROP DATABASE ... WITH (FORCE)` 断开并删库，**再**删角色——
    角色还被库占用时 `DROP ROLE` 会失败。两步都带 `IF EXISTS`，所以重复调用是安全的
    （测试的 `addClassCleanup` 需要这一点）。

    ⚠️ 破坏性操作，调用方负责先拿到人的确认。
    """
    database = tenant_database_name(schema_name)
    role = tenant_role_name(schema_name)
    if confirm != database:
        raise ValueError(
            f"确认串不匹配：必须逐字等于库名 {database!r}（收到 {confirm!r}）；未做任何修改"
        )
    _drop_database_and_role(database=database, role=role)


def _drop_database_and_role(*, database: str, role: str) -> None:
    """`DROP DATABASE ... WITH (FORCE)` 然后 `DROP ROLE`（两步都 `IF EXISTS`，顺序承重）。

    顺序不能反：角色还被库占用时 `DROP ROLE` 会失败（先断连接、删库，角色才放得掉）。
    这是 `drop_tenant_database` 的执行体。
    """
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )


def build_rotate_password_statement(*, role: str, password: str) -> sql.Composable:
    """`ALTER ROLE <role> WITH PASSWORD <literal>` 的语句（标识符/字面量都走 psycopg，不拼串）。

    与 `build_provision_statements` 同层：本模块只提供**原语**，不决定「何时轮换」——
    那个策略属于调用方（`provision_tenant_db` 的 `--rotate-password`）。
    """
    return sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(password)
    )


def _alter_role_password(*, role: str, password: str) -> None:
    """`ALTER ROLE` 的执行体。"""
    statement = build_rotate_password_statement(role=role, password=password)
    with psycopg.connect(**admin_connection_params(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(statement)


def rotate_role_password(*, schema_name: str, password: str) -> None:
    """把**已存在**租户角色的密码改成 `password`。

    为什么必须有这个原语：`provision_database` 对已存在的角色是「跳过 `CREATE ROLE`」，
    所以重跑供给**不会**重置密码。要让「库里的密码」与「本次记下来的密码」一致，
    只能显式 `ALTER ROLE`——这正是供给命令 `--rotate-password` 的实现基础。

    ⚠️ 角色**不存在**时 `ALTER ROLE` 会直接报错（不静默成功）；调用方必须保证已供给。
    """
    _alter_role_password(role=tenant_role_name(schema_name), password=password)


