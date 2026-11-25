"""sync-event kind **恰好集合网**（D-0360 计划 §三；2026-11-01）。

`JidanpuSyncEvent.kind` / `PeerSyncEvent.kind` 此前没有全局恰好集合网 ⇒
「新增一个 kind 忘了在时间线人话表登记」不会红（时间线上 fail-open 显示原始
kind 串，排查人看不懂）。本网把两头钉死：

**登记表 keys（`apps/audit/timeline.py` 的 `JIDANPU_KIND_LABELS` / `PEER_KIND_LABELS`）
== 代码里实际写出的 kind 全集**（枚举形状见下），多登记 / 少登记都红。

## 枚举源 = 全部落库形状（计划 §1.2 四族，⛔ 不是只有 `write_event`）

| 形状 | 写口 | 例子 |
|---|---|---|
| ① ORM 直写 | `Model.objects.create / bulk_create`（含裸构造器 `Model(kind=…)`） | `jidanpu_openapi_consume._note_callback_failure`、`peer_views._write_pull_evidence` |
| ② 台账写口函数 | `write_event` / `_record` / `note_once` / `_note_once_ref` 族 / `queue_member_event` / `_queue_out_event` / `enqueue_*` / `line.note_event(...)` | 全仓台账行都经这些嘴落库 |

**kind 实参解析（一层模块常量解引用，T2 同款机制的运行期形态）**：字符串字面量直取；
`kind=SOME_NAME` 经 `getattr(<所在模块>, name)` 解到模块级常量（含 `from x import
KIND_Y` 的导入别名与 `KIND = 别的模块.KIND` 的再别名链）；`名字.属性` 两段式
（`jidanpu_openapi_reconcile.KIND_X`）解一层 getattr。**解不动**（形参流转 /
`event.kind` 复制 / `plan.kind or …` 回退 / `line.kinds.X` 引擎差异面）⇒ 落点必须
在下面的 `OPAQUE_SITES` **显式登记**（⛔ 不许匿名放过），豁免条目自带机器判据。

## 扫描面（如实声明，⛔ 不自称「全仓」）

- 认：`apps/**`（跳过 `migrations/`）里，① 的三种调用形状 + ② 的写口函数名表
  （`WRITE_MOUTHS`，**冻结枚举**——新增写口必须同一次登记，否则该口的 kind 落不进
  全集、网在「登记表多登记」那一半红）；
- 写口**定义体**自身把 `kind=<形参>` 透传给下一层（`write_event` 定义里的
  `JidanpuSyncEvent.objects.create(kind=kind)`）不算落点——那是管道，真 kind 由
  调用方贡献；但定义体里**写死**的 kind（`_note_identity_heal_once` 的
  `IDENTITY_HEAL_KIND`）**算**落点（判据：解得动才算）；
- 不认：动态拼 kind（`getattr` / `**{}` 间接构造）——今天全仓 0 处（本网扫不到
  它们，如实登记这个边界）；`log.repull` **无活写口**（`models.py` 只剩部分唯一
  约束引用它，写它的模块已拆——见 `business/models.py:2016` 一带的约束注释），故不登记；
  有人复活该写口 ⇒ 本网「代码写出了、登记表没有」那一半红，逼一次显式登记。

## ⛔ 本网只在**全量套件**报

（与 `test_order_read_path_net` / `test_openapi_event_sites_net` 同款口径：
受影响网清单必须含本文件，分模块跑看不见。合并树门禁面 §八 已点名。）

## 弄红证明（本网自己的断言必须能失败）

- **变异一**：临时从 `apps/audit/timeline.py` 的 `JIDANPU_KIND_LABELS` 删
  `"member.launch_breaker_open": …` 一条 ⇒
  `test_the_registered_tables_are_exactly_the_written_kinds` 红在
  「代码写出了、登记表没有」那一半（missing 集合含它）；随后把该行**逐字编辑恢复**
  （⛔ 未用 `git checkout --`），恢复后 `git diff apps/audit/timeline.py` 为空。
- **变异二**：同法删 `PEER_KIND_LABELS` 的 `"hall.pull"` ⇒ 同一半红（missing 含它）；
  逐字编辑恢复，`git diff` 为空。
- 两向的另一半（登记表多登记）由「登记表里的 kind 是代码写不出来的」那条断言红。
- **D-0361 线的「先红后绿」（实现轮留档，非变异、是真实的开发时序）**：
  新 kind（如 `member.launch_blocked`；⚠️ `qr.fetch_started` / `qr.fetch_gaveup`
  曾在此列，查码台账简化停写后两 kind 整段删除、登记表同步收回）
  先写进代码、登记表未动时跑本网 ⇒
  `AssertionError: Items in the first set but not the second: 'qr.fetch_started' /
  'member.launch_blocked' / 'qr.fetch_gaveup'`（除这三条外**零** opaque / 写口缺登记
  ——新写点全部经既有写口 `write_event` 且 kind 解析得动，`WRITE_MOUTHS` /
  `OPAQUE_SITES` 一字未改）；同次登记后绿。
"""
import ast
import importlib
from dataclasses import dataclass
from pathlib import Path

from django.test import SimpleTestCase

from apps.audit.timeline import JIDANPU_KIND_LABELS, PEER_KIND_LABELS

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"

JDP = "JidanpuSyncEvent"
PEER = "PeerSyncEvent"

#: ① 认成「落库」的 ORM 调用名（冻结枚举；加形状要同一次改扫描器与声明）。
ORM_WRITE_CALLS = frozenset({"create", "bulk_create", "get_or_create", "update_or_create"})

#: ② 台账写口名 → 它落哪张表（**冻结枚举**；新写口同一次登记，漏登的口会被
#: 「登记表有、代码扫不出」那半暴露不了——所以本表的多余项由下面的 mouth 真实性判据钉）。
WRITE_MOUTHS: dict[str, str] = {
    # jidanpu_common（租户级线已拆，只剩这一个共用写口）
    "write_event": JDP,
    # jidanpu_upstream（上家登录式线）
    "_record": JDP,
    "note_once": JDP,
    "_note_once_ref": JDP,
    "_queue_out_event": JDP,
    # jidanpu_member（下家鸡蛋铺线）
    "queue_member_event": JDP,
    "_queue_member_event": JDP,
    "_note_order_once": JDP,
    "_note_binding_once": JDP,
    "_note_identity_heal_once": JDP,
    # 订单维度的幂等 note（jidanpu_upstream 与 consume 各有一个同名定义，判据允许）
    "_note_order": JDP,
    # 回调共用引擎（`line.note_event(...)` 经 CallbackLine 分派到各线的实现）
    "note_event": JDP,
    "_note_event": JDP,
    "_note_reconcile": JDP,
    # jidanpu_member_openapi / jidanpu_openapi
    "_note_ref": JDP,
    "_note_order_ref": JDP,
    "_note_binding_ref": JDP,
    # peer（团团通租户之间）
    "enqueue_order_event": PEER,
    "enqueue_screenshot_event": PEER,
    "enqueue_hall_event": PEER,
    "enqueue_hall_mirror_event": PEER,
    "_enqueue_hall_row": PEER,
    "enqueue_hall_claim_reply": PEER,
    "_enqueue_chat_event": PEER,
}


@dataclass(frozen=True)
class OpaqueSite:
    """AST 解不动的落点（计划 §三.3：显式登记，⛔ 不许匿名放过）。"""

    path: str
    scope: str
    shape: str  # 落点的形状名（供失败信息可读）
    model: str
    #: 该落点能写出的 kind（**冻结清单**；copy-through 类落点为空集并在 reason 写明
    #: 它的 kind 域为什么已含在别处的落点里）。
    kinds: tuple[str, ...]
    reason: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.scope, self.shape)


#: ⚠️ 今天恰好五条。新增一条解不动的落点 ⇒ 「有落点没登记」红（⛔ 不许直接加进这里
#: 而不写 kinds 与机器判据）。
OPAQUE_SITES: tuple[OpaqueSite, ...] = (
    OpaqueSite(
        "business/peer_worker.py", "_rescue_hall_event", "direct-create", PEER,
        kinds=(),
        reason=(
            "大厅事件救援（原样重发一行新台账）：kind 复制自原事件（event.kind），"
            "且函数体首行守卫 `startswith(\"hall.\")` ⇒ 域 ⊆ 已扫出的 hall.* 协议 kind，"
            "不贡献新值。机器判据见 test_the_copy_through_guards_are_still_real。"
        ),
    ),
    OpaqueSite(
        "business/jidanpu_member_openapi_outbox.py", "_ensure_remote_id", "mouth-call", JDP,
        kinds=(),
        reason=(
            "②式 outbox 自愈补远端单号：`_note_order_once(kind=event.kind)` 复制原事件；"
            "该 outbox 的 kind 域 = `IMPLEMENTED_KINDS`（member.* 七类，全部已由 "
            "`queue_member_event` 的调用点扫出）⇒ 不贡献新值。"
        ),
    ),
    OpaqueSite(
        "business/jidanpu_member.py", "_queue_member_edit", "mouth-call", JDP,
        kinds=("member.price", "member.project"),
        reason="共享改单排队透传调用方传入的 kind，取值由真实调用点核对。",
    ),
    OpaqueSite(
        "business/jidanpu_openapi_consume.py", "apply_authoritative_status", "mouth-call", JDP,
        kinds=("openapi.unmapped_status", "openapi.status_in"),
        reason=(
            "权威状态裁决不写档的两支合一：`kind=plan.kind or KIND_STATUS_IN`——"
            "plan 臂的取值 = `jidanpu_openapi.OPENAPI_UNMAPPED_STATUS_KIND`（认不出），"
            "回退臂 = 本模块 `KIND_STATUS_IN`（幂等 / 明确不映射）。"
        ),
    ),
    OpaqueSite(
        "business/jidanpu_openapi_consume.py", "<line.kinds 引擎差异面>", "mouth-call", JDP,
        kinds=(),
        reason=(
            "`line.note_event / _note_reconcile(kind=line.kinds.X)` 的六个落点作用域"
            "（_note_foreign_path / _note_stale_skip / _admit / consume_callbacks / _consume_one /"
            " reconcile_openapi_statuses）：kind 经 CallbackLine 的差异面字段流转，AST 解不动。"
            "可取值 = 两张线的 `LineKinds(...)` 构造关键字全集（上家线 + member 线），由 "
            "test_the_line_kinds_family_matches_the_constructors 机器对拍——"
            "⚠️ 空的 kinds 是**有意的**：真清单住在这个测试里，⛔ 不在这里抄第二份。"
        ),
    ),
)

#: `LineKinds(...)` 构造关键字全集的**机器对拍判据**的来源形状：扫描全部模块里的
#: `LineKinds(...)` 调用、解析每个关键字值（都是模块常量）⇒ 并集必须等于已扫 kind
#: 并集里「引擎差异面」那一族（即：这个并集 ⊆ 登记表全集；且每次构造面新增关键字
#: ⇒ 并集变大 ⇒ 若登记表没同步，恰好集合断言红）。
LINE_KINDS_CTOR_NAME = "LineKinds"

#: getattr 的「模块上没有这个名字」哨兵（模块常量可能合法地是 `None` / 空串之外的假值）。
_MISSING = object()

#: 上面那条 OpaqueSite 声明的五个作用域（机器判据用：它们必须真的存在且含该形状）。
LINE_KINDS_SCOPES = (
    ("business/jidanpu_openapi_consume.py", "_note_foreign_path"),
    ("business/jidanpu_openapi_consume.py", "_note_stale_skip"),
    ("business/jidanpu_openapi_consume.py", "_admit"),
    ("business/jidanpu_openapi_consume.py", "consume_callbacks"),
    ("business/jidanpu_openapi_consume.py", "_consume_one"),
    ("business/jidanpu_openapi_reconcile.py", "reconcile_openapi_statuses"),
)


# ── AST 基建（照 test_openapi_event_sites_net / test_order_read_path_net 的口径）──


def _module_of(rel_path: str):
    """`business/peer_worker.py` → `apps.business.peer_worker`（测试期 import 是安全的：
    这些模块本就被 Django 装过；`SimpleTestCase` 不碰库）。"""
    return importlib.import_module("apps." + rel_path[: -len(".py")].replace("/", "."))


def _scopes(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """全部函数 / 方法的作用域名 + 节点（`Class.method` / 顶层 `func`；嵌套 def 各自一条）。"""
    found: list[tuple[str, ast.AST]] = []

    class Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_fn(self, node):
            found.append((".".join(self.stack + [node.name]), node))
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_fn
        visit_AsyncFunctionDef = _visit_fn

    Walker().visit(tree)
    return found


def _root_name(node: ast.AST) -> str:
    """表达式链最左端的名字（`Model.objects.create` ⇒ `Model`）。"""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            break
    return node.id if isinstance(node, ast.Name) else ""


def _resolve_kind(expr: ast.AST, module, scope_node: ast.AST | None = None) -> tuple[str, bool]:
    """kind 实参 → `(值, 是否解得动)`。一层模块常量解引用（运行期 getattr 形态）。

    解析序：字符串字面量 → 所在模块属性 → **所在作用域里的函数级 import 别名**
    （`from apps.business.peer_sync import HALL_TAKE_KIND` 不成为模块属性，按
    ImportFrom 解一次）→ 两段式 `别的模块.KIND_X`。
    """
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value, True
    if isinstance(expr, ast.Name):
        value = getattr(module, expr.id, _MISSING)
        if value is not _MISSING:
            return (value, True) if isinstance(value, str) else (f"<非常量 {expr.id}>", False)
        if scope_node is not None:
            for stmt in ast.walk(scope_node):
                if not isinstance(stmt, ast.ImportFrom) or stmt.level:
                    continue
                for alias in stmt.names:
                    if alias.name == expr.id:
                        try:
                            target = importlib.import_module(stmt.module)
                            value = getattr(target, expr.id)
                        except (ImportError, AttributeError):
                            return f"<别名解不动 {expr.id}>", False
                        return (value, True) if isinstance(value, str) else (f"<非常量 {expr.id}>", False)
        return f"<形参 {expr.id}>", False
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        # 两段式：`别的模块.KIND_X`（`line.kinds.X` 会在这里失败——`line` 不是模块属性）。
        base_id = expr.value.id
        base = None
        try:
            base = getattr(module, base_id)
        except AttributeError:
            # 兜底：兄弟模块（函数体内 `from … import jidanpu_openapi_reconcile` 的形状
            # 不会成为本模块属性）——按同包模块名 import 一次再取。
            try:
                base = importlib.import_module(f"{module.__package__}.{base_id}")
            except ImportError:
                return f"<形参 {base_id}.{expr.attr}>", False
        try:
            value = getattr(base, expr.attr)
        except AttributeError:
            return f"<不可解 {base_id}.{expr.attr}>", False
        return (value, True) if isinstance(value, str) else (f"<非常量 {expr.attr}>", False)
    return "<表达式>", False


_tree_cache: dict[str, ast.AST] = {}


def _tree_of(rel: str) -> ast.AST:
    if rel not in _tree_cache:
        _tree_cache[rel] = ast.parse((APPS_DIR / rel).read_text(encoding="utf-8"))
    return _tree_cache[rel]


def _iter_app_modules():
    for path in sorted(APPS_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        rel = str(path.relative_to(APPS_DIR))
        yield rel, _LazyModule(rel)


class _LazyModule:
    """源码照常扫描，只有解析事件常量时才加载所属运行面。"""

    def __init__(self, rel: str):
        self.rel = rel

    def __getattr__(self, name):
        return getattr(_module_of(self.rel), name)


def _scan() -> dict[str, set[str]]:
    """扫描 ⇒ `{模型: 写出的 kind 集合}`（只收**解得动**的；解不动的进 opaque 面）。"""
    kinds: dict[str, set[str]] = {JDP: set(), PEER: set()}
    opaque_found: set[tuple[str, str, str]] = set()
    for rel, module in _iter_app_modules():
        tree = _tree_of(rel)
        top_level_mouths = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in WRITE_MOUTHS
        }
        # 写口**定义**的 `kind=<默认值>`（`def _note_order(…, kind: str = KIND_STATUS_IN)`）：
        # 调用方不传 kind 时落的就是默认值 ⇒ 它也是「写口写出的 kind」。
        # ⚠️ 只认参数名恰为 `kind` 的默认（其余参数的默认是 status / 方向等，不是 kind）。
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in WRITE_MOUTHS:
                continue
            pos_args = node.args.args
            pos_defaults = node.args.defaults
            aligned = list(zip(pos_args[len(pos_args) - len(pos_defaults):], pos_defaults))
            aligned += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for arg, default in aligned:
                if arg.arg != "kind" or default is None:
                    continue
                value, resolved = _resolve_kind(default, module)
                if resolved:
                    kinds[WRITE_MOUTHS[node.name]].add(value)
        for scope, node in _scopes(tree):
            for child in ast.walk(node):
                if not (isinstance(child, ast.Call) and isinstance(child.func, (ast.Name, ast.Attribute))):
                    continue
                model = None
                shape = ""
                if isinstance(child.func, ast.Attribute) and child.func.attr in ORM_WRITE_CALLS:
                    root = _root_name(child.func)
                    if root in (JDP, PEER):
                        model, shape = root, "direct-create"
                elif isinstance(child.func, ast.Name) and child.func.id in (JDP, PEER):
                    # 裸构造器（`bulk_create([Model(kind=…)])` 的形状）
                    if any(kw.arg == "kind" for kw in child.keywords):
                        model, shape = child.func.id, "direct-create"
                if model is None:
                    mouth = child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", "")
                    if mouth in WRITE_MOUTHS:
                        model, shape = WRITE_MOUTHS[mouth], "mouth-call"
                if model is None:
                    continue
                expr = next((kw.value for kw in child.keywords if kw.arg == "kind"), None)
                if expr is None:
                    continue
                value, resolved = _resolve_kind(expr, module, scope_node=node)
                # 管道豁免：写口**定义体**里解不动的 kind（透传 `<形参>` / 复制 `event.kind`）
                # 不算落点——真 kind 由调用方贡献（判据：解不动才豁免；写死在定义体里的
                # kind 解得动、照常进全集）。
                if not resolved and scope in top_level_mouths:
                    continue
                if not resolved:
                    opaque_found.add((rel, scope, f"{shape}<{value}>"))
                    continue
                kinds[model].add(value)
    # 扫到的 opaque 落点挂到模块级，供判据 3 两向对拍（见 _LAST_OPAQUE_FOUND）。
    global _LAST_OPAQUE_FOUND
    _LAST_OPAQUE_FOUND = opaque_found
    return kinds


_LAST_OPAQUE_FOUND: set[tuple[str, str, str]] = set()


def _line_kinds_literals() -> set[str]:
    """全部 `LineKinds(...)` 构造关键字的解析值并集（引擎差异面那一族的 kind 域）。"""
    literals: set[str] = set()
    for rel, module in _iter_app_modules():
        for node in ast.walk(_tree_of(rel)):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name != LINE_KINDS_CTOR_NAME:
                continue
            for kw in node.keywords:
                value, resolved = _resolve_kind(kw.value, module)
                if resolved:
                    literals.add(value)
    return literals


# ═══════════════════════════════════════════════════════════════════════


class SyncEventKindRegistryTests(SimpleTestCase):
    """判据（逐条能被单独弄红；弄红证明见模块 docstring）。"""

    def test_uninstalled_app_is_scanned_without_eager_import(self):
        from unittest.mock import patch

        with patch(f'{__name__}._module_of', side_effect=AssertionError('不应提前导入')):
            module = next(module for rel, module in _iter_app_modules() if rel == 'settlement_control/models.py')
            self.assertIsInstance(module, _LazyModule)
            self.assertIsInstance(_tree_of('settlement_control/models.py'), ast.Module)

    def test_lazy_scan_still_detects_event_constant(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        tree = ast.parse('def new_event():\n    write_event(kind=NEW_KIND)\n')
        with patch(f'{__name__}._iter_app_modules', return_value=iter([('synthetic.py', _LazyModule('synthetic.py'))])), \
             patch(f'{__name__}._tree_of', return_value=tree), \
             patch(f'{__name__}._module_of', return_value=SimpleNamespace(NEW_KIND='synthetic.event')) as load:
            scanned = _scan()
        self.assertEqual(scanned[JDP], {'synthetic.event'})
        load.assert_called_once_with('synthetic.py')

    def test_the_registered_tables_are_exactly_the_written_kinds(self):
        """⭐ 恰好集合（⛔ 不许 toContain）：登记表 keys == 扫描全集 ∪ 豁免清单。"""
        scanned = _scan()
        line_family = _line_kinds_literals()
        for model, table in ((JDP, JIDANPU_KIND_LABELS), (PEER, PEER_KIND_LABELS)):
            exempt = set()
            for entry in OPAQUE_SITES:
                if entry.model == model:
                    exempt |= set(entry.kinds)
            total = scanned[model] | exempt | (line_family if model == JDP else set())
            missing = total - set(table)
            self.assertEqual(
                missing, set(),
                f"{model}：代码写出了这些 kind、人话表没有登记：{sorted(missing)}"
                "——**同一次**在 `apps/audit/timeline.py` 登记人话短名"
                "（新写口 / 新落点还要同一次登记 `WRITE_MOUTHS` 或 `OPAQUE_SITES`）。",
            )
            extra = set(table) - total
            self.assertEqual(
                extra, set(),
                f"{model}：登记表里这些 kind 已没有代码写口（或写口没被本网认出）："
                f"{sorted(extra)}——表烂了会给出假保证：要么删登记、要么按本文件头"
                "「扫描面」一节把新形状/新写口接进扫描（⛔ 不许匿名放宽）。",
            )

    def test_every_scanned_opaque_site_is_declared_and_every_entry_is_real(self):
        """判据 3 两向：AST 解不动的落点 ↔ `OPAQUE_SITES`（按 (文件, 作用域)）。"""
        _scan()  # 顺带把 _LAST_OPAQUE_FOUND 填上
        found_keys = {f"{key[0]}|{key[1]}" for key in _LAST_OPAQUE_FOUND}
        # 「引擎差异面」那条的五个作用域必须真的存在且真含 line.kinds 形状：
        for rel, scope in LINE_KINDS_SCOPES:
            node = dict(_scopes(_tree_of(rel))).get(scope)
            self.assertIsNotNone(node, f"{rel}::{scope} 不存在——登记表烂了")

            def _is_line_kinds_expr(expr):
                """`line.kinds.X`（三段链）或 `line.kinds`（两段）都算；根必须是形参 `line`。"""
                while isinstance(expr, ast.Attribute):
                    expr = expr.value
                return isinstance(expr, ast.Name) and expr.id == "line"

            has_line_kind = any(
                kw.value is not None
                and isinstance(kw.value, ast.Attribute)
                and _is_line_kinds_expr(kw.value)
                for stmt in ast.walk(node)
                if isinstance(stmt, ast.Call)
                for kw in stmt.keywords
                if kw.arg == "kind"
            )
            self.assertTrue(
                has_line_kind,
                f"{rel}::{scope} 里已没有 `line.kinds.*` 形状的 kind 实参——"
                "该落点要么已变成可解析形状（把它从 LINE_KINDS_SCOPES 摘掉），要么登记烂了",
            )
            found_keys.discard(f"{rel}|{scope}")
        found_keys.discard("business/jidanpu_openapi_consume.py|apply_authoritative_status")
        found_keys.discard("business/peer_worker.py|_rescue_hall_event")
        found_keys.discard("business/jidanpu_member_openapi_outbox.py|_ensure_remote_id")
        found_keys.discard("business/jidanpu_member.py|_queue_member_edit")
        self.assertEqual(
            found_keys, set(),
            f"这些解不动的落点没在 OPAQUE_SITES 登记：{sorted(found_keys)}"
            "——⛔ 新增解不动的形状必须显式登记（带 kinds 清单 + 机器判据），不许匿名放过",
        )
        declared_keys = {
            f"{entry.path}|{entry.scope}"
            for entry in OPAQUE_SITES
            if entry.scope != "<line.kinds 引擎差异面>"
        }
        real_found = {
            f"{key[0]}|{key[1]}" for key in _LAST_OPAQUE_FOUND
        } - {
            f"{rel}|{scope}"
            for rel, scope in LINE_KINDS_SCOPES
        }
        self.assertEqual(
            declared_keys, real_found,
            "OPAQUE_SITES 里有已经解得动 / 已不存在的条目——删掉它（表烂了会给出假保证）",
        )

    def test_member_edit_callers_match_the_declared_kinds(self):
        site = next(
            entry for entry in OPAQUE_SITES
            if entry.path == "business/jidanpu_member.py" and entry.scope == "_queue_member_edit"
        )
        actual = set()
        for rel, module in _iter_app_modules():
            for call in ast.walk(_tree_of(rel)):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if name != site.scope:
                    continue
                location = f"{rel}:{call.lineno}"
                expr = next((kw.value for kw in call.keywords if kw.arg == "kind"), None)
                self.assertIsNotNone(expr, f"{location} 未明确传入改单 kind")
                value, resolved = _resolve_kind(expr, module)
                self.assertTrue(resolved, f"{location} 无法确认改单 kind：{value}")
                actual.add(value)
        self.assertEqual(actual, set(site.kinds))

    def test_the_plan_kind_fallback_arm_is_still_the_declared_pair(self):
        """`plan.kind or KIND_STATUS_IN`：两臂的取值机器对拍（写法变了 ⇒ 本条红）。"""
        source = (APPS_DIR / "business/jidanpu_openapi_consume.py").read_text(encoding="utf-8")
        self.assertIn(
            "plan.kind or KIND_STATUS_IN", source,
            "那条回退写法已不在——把 OPAQUE_SITES 对应条目改准（或删除）",
        )
        consume = importlib.import_module("apps.business.jidanpu_openapi_consume")
        jidanpu_openapi = importlib.import_module("apps.business.jidanpu_openapi")
        self.assertEqual(
            getattr(jidanpu_openapi, "OPENAPI_UNMAPPED_STATUS_KIND"),
            "openapi.unmapped_status",
        )
        self.assertEqual(getattr(consume, "KIND_STATUS_IN"), "openapi.status_in")

    def test_the_line_kinds_family_matches_the_constructors(self):
        """`LineKinds(...)` 关键字全集 ⊆ 登记表（引擎差异面的 kind 域由构造面对拍）。"""
        family = _line_kinds_literals()
        self.assertTrue(
            family, "LineKinds 构造面一个关键字都没解到（自证失败）"
        )
        not_registered = family - set(JIDANPU_KIND_LABELS)
        self.assertEqual(
            not_registered, set(),
            f"LineKinds 构造面新增了这些 kind 而人话表没登记：{sorted(not_registered)}",
        )
        # 阳性对照：两张线的构造面都要在场（只扫到一张 ⇒ 另一张的落点没人管了）
        member_openapi = importlib.import_module("apps.business.jidanpu_member_openapi")
        self.assertEqual(
            getattr(member_openapi, "KIND_SIGN_REJECTED"), "member.openapi_unsigned"
        )
        self.assertTrue(
            any(k.startswith("member.openapi_") for k in family),
            "member 线的构造面没进并集（upstream 线独占了）",
        )
        self.assertTrue(
            any(k.startswith("openapi.callback_") for k in family),
            "upstream 线的构造面没进并集（member 线独占了）",
        )

    def test_the_copy_through_guards_are_still_real(self):
        """两条 copy-through 落点的**守卫**仍在（域声明不凭空成立）。"""
        worker = (APPS_DIR / "business/peer_worker.py").read_text(encoding="utf-8")
        self.assertIn('startswith("hall.")', worker, "救援的 hall.* 守卫没了")
        self.assertIn("kind=event.kind", worker, "救援的复制形状变了——把 OPAQUE_SITES 改准")
        outbox = (APPS_DIR / "business/jidanpu_member_openapi_outbox.py").read_text(encoding="utf-8")
        self.assertIn(
            "kind=event.kind", outbox, "outbox 自愈的复制形状变了——把 OPAQUE_SITES 改准"
        )
        # outbox 的 kind 域（IMPLEMENTED_KINDS）必须 ⊆ 已扫 kind ∪ 登记表：
        scanned = _scan()
        module = importlib.import_module("apps.business.jidanpu_member_openapi_outbox")
        implemented = set(getattr(module, "IMPLEMENTED_KINDS"))
        self.assertEqual(
            implemented - scanned[JDP], set(),
            f"outbox 域里出现了扫描不到的 kind：{sorted(implemented - scanned[JDP])}",
        )

    def test_every_write_mouth_is_a_real_top_level_function(self):
        """写口名表的真实性：每个名字都是某个 app 模块的顶层 def（防拼写错 / 防烂表）。

        ⚠️ `note_event` 例外（DISPATCH_MOUTHS）：它不是函数名，是 CallbackLine 引擎的
        **分派字段**——调用形状是 `line.note_event(...)`（落到各线自己的实现，
        `_note_event` / `_note_ref`，两者都在本表并有各自的真实性判据）。
        """
        dispatch = {"note_event"}
        found: dict[str, str] = {}
        for rel in sorted({str(p.relative_to(APPS_DIR)) for p in APPS_DIR.rglob("*.py") if "migrations" not in p.parts}):
            tree = _tree_of(rel)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WRITE_MOUTHS:
                    found.setdefault(node.name, rel)
        self.assertEqual(
            set(found), set(WRITE_MOUTHS) - dispatch,
            f"写口名表与真实定义不一致：缺 {sorted((set(WRITE_MOUTHS) - dispatch) - set(found))}、"
            f"多余 {sorted(set(found) - set(WRITE_MOUTHS))}",
        )

    def test_the_scan_itself_is_not_vacuous(self):
        """自证：两个模型的扫描集都非空（防「扫描器坏了 ⇒ 空集 ⇒ 恰好断言恒真」）。"""
        scanned = _scan()
        self.assertTrue(scanned[JDP], "jdp 一个 kind 都没扫到（自证失败）")
        self.assertTrue(scanned[PEER], "peer 一个 kind 都没扫到（自证失败）")
        # 已知的锚点 kind 必须在（扫描器形状改动时最先死的是它们）
        self.assertIn("member.launch", scanned[JDP])
        self.assertIn("hall.throw", scanned[PEER])
