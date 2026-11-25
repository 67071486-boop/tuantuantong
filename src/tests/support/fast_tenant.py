"""进程级共享租户 schema —— 把 `TenantTestCase` 的「每个类建库 + 跑迁移」压成「每个进程一次」。

## 为什么会有这个文件

`django_tenants.test.cases.TenantTestCase` 的 `setUpClass()` 对**每一个测试类**都做：

1. `sync_shared()` → `migrate_schemas --shared`；
2. `tenant.save()` → `CREATE SCHEMA` + 该 schema 的**全量 `migrate_schemas`**。

`tearDownClass()` 再 `domain.delete()` + `tenant.delete(force_drop=True)`（= `DROP SCHEMA ... CASCADE`）。
本仓 749 个测试类继承它（实测 2026-10-27 分片计时：类级合计 19,572 秒 = 全量墙钟的 74%），
而类里用例本体只占 6,744 秒 —— 大头是**同样的 schema 被反复建、反复迁移、反复删**。

## 怎么做到「每进程一次」

1. 第一次用到某个 schema 名时，走**原逻辑**建 schema + 迁移，然后记进进程级缓存；
2. 类结束时**不删 schema**，只 `ALTER SCHEMA ... RENAME TO <隐藏名>`，并把 public 里的
   Tenant / Domain 行删掉 —— 对外部完全恢复成「这个租户不存在」的形态。
   必须这样做的原因：仓库里有一批 `TransactionTestCase` 自己建 `schema_name="test"` 的租户
   （`test_account_api` / `test_claim_concurrency` / `test_openapi_rate_limit` …），
   还会插 `tenant.test.com` 的 Domain 行；缓存如果留着这些，它们会撞唯一约束。
3. 下一个类开始时把隐藏 schema 改名回来，再把 Tenant / Domain 行**重新 INSERT**
   （只 INSERT：schema 已存在 ⇒ `create_schema(check_if_exists=True)` 短路，不迁移）；
4. 每个类仍然开一个**类级事务**（Django `TestCase` 的标准语义：`cls._enter_atomics()`），
   `setUpClass()` 里写的数据在类结束时回滚 ⇒ 类与类之间不串数据。
   原实现是靠「删掉整个 schema」达到这一点的；共用 schema 之后必须显式补上这一层。
5. 进程退出（`atexit`）删掉缓存 schema，⛔ 不留脏库。

## 怎么用

不要改 749 个测试文件的 import。由 `tests.fast_tenant_runner.FastTenantRunner`（`--testrunner` /
`settings.TEST_RUNNER`）在跑测试前调 `install_patch()`，把
`TenantTestCase.setUpClass / tearDownClass` 换成这里的实现。

开关：环境变量 `FAST_TENANT_SCHEMA=0` ⇒ `install_patch()` 直接不做事，行为与改动前**逐字一致**。

## 边界 / 已知取舍

- **序列（sequence）显式回拨**：事务回滚不会把 Postgres 序列拨回去，而「每类全新 schema」的
  语义里序列是从 1 开始的（`seed_tenant` 的 admin 恰好是 `pk=1`，`test_account_api` 有两条
  用例依赖这一点、实测会红）。所以每个类开始时按 `_reset_sequences()` 把 serial / identity
  序列拨回 `max(列)+1`：空表 ⇒ 下一条是 1（与全新 schema 一致）；迁移期灌进来的基线数据
  （`django_content_type` / `auth_permission` / 数据迁移）⇒ 留在它们的 max+1，不会撞主键。
- **类里新建的第二个租户**（`provision_tenant(...)` 在 `setUpClass` 里）现在会随类级事务一起回滚，
  即真的「类结束就没了」。原来它会留到进程结束（泄漏）。这是收紧，不是放宽。
- **声明的辅助租户**：`extra_test_tenants` 在类级事务前真实创建与播种，类间只保留隐藏结构。
  夹具里的 `provision_tenant(...)` 仍走生产幂等路径；未声明的辅助租户仍随类级事务回滚。
- **opt-out**：确实需要「每个类全新 schema」的类，把模块/类名登记在 `OPT_OUT_PREFIXES`，
  或给类加 `_fast_tenant_opt_out = True`，它就走原实现。
"""

from __future__ import annotations

import atexit
from contextlib import ExitStack
import os
import re
import sys
import time

from django.core.management import call_command
from django.db import connections
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_database_alias,
    get_tenant_domain_model,
    get_tenant_model,
)

#: 开关：`FAST_TENANT_SCHEMA=0` 关掉整个优化（行为回到改动前）。
ENV_FLAG = "FAST_TENANT_SCHEMA"

#: 缓存 schema 在「类与类之间」挂的隐藏名。改名只是元数据操作（毫秒级），不搬数据。
_HIDDEN_PREFIX = "fast_tenant_cache_"

#: 需要**每个类全新 schema** 的模块 / 类（`"tests.test_xxx"` 或 `"tests.test_xxx.Cls"` 前缀）。
#: 默认空；跑全量时哪些必须排除，以实测为准（见 docs/STATUS.md 那一轮的记录）。
OPT_OUT_PREFIXES: tuple[str, ...] = ()

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_installed = False
_original_setUpClass = TenantTestCase.__dict__["setUpClass"]
_original_tearDownClass = TenantTestCase.__dict__["tearDownClass"]

#: schema 名 → 缓存条目；辅助租户另存 extra 声明，共用进程级清理。
_cache: dict[str, dict] = {}
_shared_migrated = False


# ── 小工具 ────────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """`FAST_TENANT_SCHEMA` 没给 / 给了真值 ⇒ 开；`0/false/no/off` ⇒ 关。"""
    raw = os.environ.get(ENV_FLAG, "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _connection():
    return connections[get_tenant_database_alias()]


def _note(message: str) -> None:
    """写一行到 stderr（分片脚本把 stdout/stderr 都收进日志；不干扰测试输出）。"""
    sys.stderr.write(f"[fast_tenant] {message}\n")
    sys.stderr.flush()


def _hidden_name(schema_name: str) -> str:
    return f"{_HIDDEN_PREFIX}{schema_name}"


def _checked(name: str) -> str:
    """标识符只用我们自己拼出来的名字，进来前再核一次，绝不把外部串插进 SQL。"""
    if not _IDENT_RE.match(name):
        raise RuntimeError(f"非法的 schema 名（拒绝拼进 SQL）：{name!r}")
    return name


def _schema_exists(name: str) -> bool:
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s)",
            [name],
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _rename_schema(old: str, new: str) -> None:
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(
            f'ALTER SCHEMA "{_checked(old)}" RENAME TO "{_checked(new)}"'
        )


def _drop_schema(name: str) -> None:
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{_checked(name)}" CASCADE')


def _tenant_model():
    return get_tenant_model()


def _domain_model():
    return get_tenant_domain_model()


def _opt_out(cls) -> bool:
    if getattr(cls, "_fast_tenant_opt_out", False):
        return True
    fqcn = f"{cls.__module__}.{cls.__qualname__}"
    for prefix in OPT_OUT_PREFIXES:
        if fqcn == prefix or fqcn.startswith(prefix + ".") or cls.__module__ == prefix:
            return True
        # 也接受 "tests.test_x.SomeClass" 这种以类名结尾的前缀
        if fqcn.startswith(prefix):
            return True
    return False


# ── 缓存的生命周期 ────────────────────────────────────────────────────────────

def _sync_shared_once() -> None:
    """原实现每个类都跑一遍 `migrate_schemas --shared`；这里每个进程一遍就够（它是幂等的检查）。"""
    global _shared_migrated
    if _shared_migrated:
        return
    call_command(
        "migrate_schemas",
        schema_name=get_public_schema_name(),
        interactive=False,
        verbosity=0,
    )
    _shared_migrated = True


#: 找出该 schema 里「serial / identity 序列 → 它服务的表 + 列」。
#: 用来在**复用** schema 时把序列拨回「刚迁移完」的位置（见 `_reset_sequences`）。
_SEQUENCE_SQL = """
SELECT seq.relname, tab.relname, att.attname
FROM pg_class AS seq
JOIN pg_namespace AS ns ON ns.oid = seq.relnamespace
JOIN pg_depend AS dep
     ON dep.objid = seq.oid
    AND dep.classid = 'pg_class'::regclass
    AND dep.refclassid = 'pg_class'::regclass
    AND dep.deptype IN ('a', 'i')
JOIN pg_class AS tab ON tab.oid = dep.refobjid
JOIN pg_attribute AS att
     ON att.attrelid = tab.oid AND att.attnum = dep.refobjsubid
WHERE seq.relkind = 'S' AND ns.nspname = %s AND tab.relnamespace = ns.oid
"""


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _reset_sequences(schema_name: str) -> None:
    """把 schema 里的序列拨回「刚迁移完」的位置（= 每个类全新 schema 时的状态）。

    为什么需要：事务回滚**不会**回退 Postgres 序列。原实现每个类都重建 schema ⇒ 序列从 1
    开始（`seed_tenant` 的 admin 恰好是 `pk=1`，有多条例用依赖这一点）；共用 schema 之后
    不回拨就会变成 `pk=N`，把「全新租户」的语义悄悄改掉。

    做法：对每个 serial / identity 序列，`setval(max(列) + 1, is_called=false)`。
    ⛔ 不是无脑 `RESTART WITH 1`：迁移期灌进来的基线数据（`django_content_type` /
    `auth_permission` / 数据迁移）是**已提交**的，序列必须留在它们的 max+1，否则下一条
    INSERT 会撞主键。
    """
    conn = _connection()
    with conn.cursor() as cur:
        cur.execute(_SEQUENCE_SQL, [schema_name])
        rows = cur.fetchall()
        if not rows:
            return
        statements = []
        for seq_name, table_name, column_name in rows:
            regclass = _ql(f"{_qi(schema_name)}.{_qi(seq_name)}")
            statements.append(
                f"SELECT setval({regclass}, "
                f"COALESCE((SELECT max({_qi(column_name)}) "
                f"FROM {_qi(schema_name)}.{_qi(table_name)}), 0) + 1, false)"
            )
        # 一条 round-trip 发完（每类 ~80 条小语句，实测 <10ms）
        cur.execute(";".join(statements))


def _delete_catalog_rows(schema_name: str, domain_name: str) -> None:
    """删掉 public 里的 Domain / Tenant 行（**不**动 schema：`QuerySet.delete()` 不会走
    `TenantMixin.delete()` 的落 schema 分支）。"""
    conn = _connection()
    conn.set_schema_to_public()
    _domain_model().objects.filter(
        domain=domain_name, tenant__schema_name=schema_name
    ).delete()
    _tenant_model().objects.filter(schema_name=schema_name).delete()


def _create_fresh_schema(cls, schema_name: str):
    """按 django_tenants 原逻辑建 schema + 跑一遍迁移 + INSERT 租户行（每进程每 schema 一次）。"""
    tenant = _tenant_model()(schema_name=schema_name)
    cls.setup_tenant(tenant)
    started = time.monotonic()
    tenant.save(verbosity=cls.get_verbosity())
    _note(
        f"共享租户 schema {schema_name!r} 建好并迁移完（本进程只此一次，"
        f"{time.monotonic() - started:.1f}s）"
    )
    return tenant


def _acquire(cls):
    """保证「真名 schema + Tenant 行 + Domain 行」都在；返回 `(state, tenant, domain)`。"""
    schema_name = cls.get_test_schema_name()
    domain_name = cls.get_test_tenant_domain()
    _connection().set_schema_to_public()

    state = _cache.get(schema_name)
    if state is None:
        hidden = _hidden_name(schema_name)
        # 首次：把可能残留的（`--keepdb` / 上一进程被杀 / 遗留 schema）清掉，
        # 否则 `create_schema(check_if_exists=True)` 会短路，复用一份陈旧 schema。
        if _schema_exists(schema_name):
            _note(f"发现残留 {schema_name!r} schema，先删掉再建，避免复用陈旧结构")
            _drop_schema(schema_name)
        if _schema_exists(hidden):
            _note(f"发现上次进程残留的缓存 schema {hidden!r}，先删掉")
            _drop_schema(hidden)
        _delete_catalog_rows(schema_name, domain_name)
        # `tenant.save()` 会 `CREATE SCHEMA` + 全量迁移 + INSERT 租户行。
        tenant = _create_fresh_schema(cls, schema_name)
        state = {"schema": schema_name, "hidden": hidden}
        _cache[schema_name] = state
    else:
        hidden = state["hidden"]
        if _schema_exists(hidden):
            if _schema_exists(schema_name):
                # 真名被别人占了（有测试自己建了 `test` 又不删）。我们对这个名字有独占
                # 约定 ⇒ 丢掉那份不可信的结构，用自己缓存的那份。
                _note(
                    f"{schema_name!r} 已被别的测试留下（未清理），丢掉它并用缓存 schema"
                )
                _drop_schema(schema_name)
            _rename_schema(hidden, schema_name)
        elif not _schema_exists(schema_name):
            # 缓存 schema 被谁删了（例如某个测试 drop 了它）。只能重建，代价一次迁移。
            _note(f"缓存 schema {schema_name!r} 不见了，重建一次（本应只发生一次）")
            _delete_catalog_rows(schema_name, domain_name)
            tenant = _create_fresh_schema(cls, schema_name)
            state = {"schema": schema_name, "hidden": hidden}
            _cache[schema_name] = state
            domain = _domain_model()(tenant=tenant, domain=domain_name)
            cls.setup_domain(domain)
            domain.save()
            cls.add_allowed_test_domain()
            return state, tenant, domain

        _reset_sequences(schema_name)
        _delete_catalog_rows(schema_name, domain_name)
        # 重新插 Tenant 行：schema 已存在 ⇒ `create_schema(check_if_exists=True)` 短路，
        # 这里只有一条 INSERT 的开销，**不会**再迁移。
        tenant = _tenant_model()(schema_name=schema_name)
        cls.setup_tenant(tenant)
        tenant.save(verbosity=cls.get_verbosity())

    domain = _domain_model()(tenant=tenant, domain=domain_name)
    cls.setup_domain(domain)
    domain.save()

    cls.add_allowed_test_domain()
    return state, tenant, domain


def _extra_definitions(cls) -> list[dict[str, str]]:
    definitions = []
    main_state = _cache.get(cls.get_test_schema_name())
    if main_state is not None and "extra" in main_state:
        raise ValueError("辅助测试租户缓存不能用作主测试租户")
    schemas = {get_public_schema_name(), cls.get_test_schema_name()}
    domains = {cls.get_test_tenant_domain()}
    for definition in getattr(cls, "extra_test_tenants", ()):
        if not isinstance(definition, dict) or set(definition) != {"schema_name", "name", "domain"}:
            raise ValueError("辅助测试租户须声明 schema_name、name 和 domain")
        if not all(isinstance(value, str) and value.strip() for value in definition.values()):
            raise ValueError("辅助测试租户声明须使用非空字符串")
        schema_name = _checked(definition["schema_name"])
        domain_name = definition["domain"]
        if schema_name in schemas or schema_name.startswith(_HIDDEN_PREFIX) or domain_name in domains:
            raise ValueError("辅助测试租户的结构名或域名与已有声明冲突")
        cached = _cache.get(schema_name)
        if cached is not None and cached.get("extra") != definition:
            raise ValueError("辅助测试租户缓存与本次声明不一致")
        schemas.add(schema_name)
        domains.add(domain_name)
        definitions.append(dict(definition))
    return definitions


def _acquire_extra(definition: dict[str, str]) -> dict:
    from apps.platform.provisioning import provision_tenant

    _connection().set_schema_to_public()
    schema_name = definition["schema_name"]
    domain_name = definition["domain"]
    hidden = _hidden_name(schema_name)
    state = _cache.get(schema_name)
    if state is not None and state.get("extra") != definition:
        raise ValueError("辅助测试租户缓存与本次声明不一致")
    if (
        _tenant_model().objects.filter(schema_name=schema_name).exists()
        or _domain_model().objects.filter(domain=domain_name).exists()
        or _schema_exists(schema_name)
    ):
        raise ValueError("辅助测试租户已有活动结构或目录，不能覆盖")
    hidden_exists = _schema_exists(hidden)
    if state is None and hidden_exists:
        raise ValueError("辅助测试租户存在未知缓存，不能覆盖")
    restored = state is not None and hidden_exists
    renamed = False
    try:
        if restored:
            _rename_schema(hidden, schema_name)
            renamed = True
            _reset_sequences(schema_name)
        provision_tenant(**definition)
    except BaseException:
        if renamed:
            _rename_schema(schema_name, hidden)
        raise
    state = {"schema": schema_name, "hidden": hidden, "extra": dict(definition)}
    _cache[schema_name] = state
    return state


def _release(state: dict, domain_name: str) -> None:
    """类结束：删目录行，把 schema 改名藏起来（数据留在里面，下次改名回来）。"""
    _connection().set_schema_to_public()
    schema_name = state["schema"]
    hidden = state["hidden"]
    _delete_catalog_rows(schema_name, domain_name)
    if _schema_exists(schema_name):
        if _schema_exists(hidden):  # 不该发生；真发生了就丢旧缓存，避免改名撞名
            _drop_schema(hidden)
        _rename_schema(schema_name, hidden)


def cleanup_cached_schemas() -> None:
    """删掉本进程缓存的 schema（⛔ 不留脏库）。失败不抛：清理不能把测试结果判红。

    ⚠️ 必须在 **runner 拆测试库之前** 调用（`FastTenantRunner.teardown_databases`）：
    Django 的 `teardown_databases` 会把连接 `NAME` 还原成开发库名并关连接，等到
    `atexit` 时 `_connection()` 已经指回开发库 —— 那样 `--keepdb` 下隐藏 schema 会留下
    （实测过：上一版只在 atexit 清，`--keepdb` 第二次跑会看到残留）。`atexit` 只作兜底。
    """
    if not _cache:
        return
    try:
        _connection().set_schema_to_public()
    except Exception:  # noqa: BLE001 —— 测试库可能已经被删掉了
        return
    for state in list(_cache.values()):
        for name in (state["hidden"], state["schema"]):
            try:
                if _schema_exists(name):
                    _drop_schema(name)
            except Exception:  # noqa: BLE001
                pass
    _cache.clear()


def _cleanup() -> None:
    """`atexit` 兜底：正常路径已经由 runner 的 `teardown_databases` 清过了。"""
    cleanup_cached_schemas()


# ── 替换到 TenantTestCase 上的两个类方法 ──────────────────────────────────────

def _fast_setUpClass(cls):  # noqa: N802 —— 与 django_tenants / unittest 的命名一致
    if not enabled() or _opt_out(cls):
        cls._fast_tenant_state = None
        return _original_setUpClass.__func__(cls)

    definitions = _extra_definitions(cls)
    _connection().set_schema_to_public()
    _sync_shared_once()
    state, tenant, domain = _acquire(cls)
    cls.tenant = tenant
    cls.domain = domain
    cls._fast_tenant_state = state
    cls._fast_tenant_extra_states = []
    cls.cls_atomics = None
    if definitions:
        cls.addClassCleanup(_fast_class_cleanup, cls)
    for definition in definitions:
        cls._fast_tenant_extra_states.append(_acquire_extra(definition))
    # 先定租户 / search_path，再开类级事务：SQL 是惰性发的（`set_tenant` 只记状态），
    # 事务里第一条语句前会按当前 schema 发 `SET search_path`。
    _connection().set_tenant(tenant)

    # Django `TestCase` 的标准语义：类级事务 ⇒ `setUpClass()` 里写的数据在类结束时回滚。
    # ⚠️ 原 `TenantTestCase` 从不调 `super().setUpClass()`，所以它没有这一层（靠删 schema
    # 达到隔离）。共用 schema 之后必须显式补上，否则前一个类 setUpClass 写的数据会留在
    # 后一个类里（撞唯一约束 / 计数不对）。
    cls.cls_atomics = cls._enter_atomics()


def _fast_tearDownClass(cls):  # noqa: N802
    state = getattr(cls, "_fast_tenant_state", None)
    if state is None:
        return _original_tearDownClass.__func__(cls)
    _fast_class_cleanup(cls)


def _fast_class_cleanup(cls):
    state = cls.__dict__.get("_fast_tenant_state")
    if state is None:
        return

    try:
        atomics = getattr(cls, "cls_atomics", None)
        if atomics is not None:
            cls._rollback_atomics(atomics)
            cls.cls_atomics = None
    finally:
        _connection().set_schema_to_public()
        try:
            cls.remove_allowed_test_domain()
        finally:
            try:
                with ExitStack() as releases:
                    releases.callback(_release, state, cls.get_test_tenant_domain())
                    for extra_state in cls._fast_tenant_extra_states:
                        releases.callback(_release, extra_state, extra_state["extra"]["domain"])
            finally:
                cls._fast_tenant_state = None
                cls._fast_tenant_extra_states = []


# ── 安装 / 卸载 ───────────────────────────────────────────────────────────────

def install_patch() -> bool:
    """把共享 schema 版接上 `TenantTestCase`。幂等；开关关掉时什么都不做。"""
    global _installed
    if _installed:
        return True
    if not enabled():
        _note(f"{ENV_FLAG}=0，保持 django_tenants 原行为（每个测试类建/删一次 schema）")
        _installed = True
        return False
    TenantTestCase.setUpClass = classmethod(_fast_setUpClass)
    TenantTestCase.tearDownClass = classmethod(_fast_tearDownClass)
    atexit.register(_cleanup)
    _installed = True
    return True


def uninstall_patch() -> None:
    """还原（只给排障 / 自检用；正常进程用不到）。"""
    TenantTestCase.setUpClass = _original_setUpClass
    TenantTestCase.tearDownClass = _original_tearDownClass
