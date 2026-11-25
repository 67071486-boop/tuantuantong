"""E-0232 回流：**peer 事件里全部「会写订单价 / 大厅镜像价」的点**（恰好集合网）。

## 为什么要有这张网

D-0306 / D-0307改的是**价格语义**：订单流放行负价、上家改价只动客户价。
这类改动的错法不是「某个函数写错了」，而是「**还有一个写价点没被一起改**」——
的 M-2（total_price 与 hall_price 永久脱钩）就是只改了一半的现场。
⇒ 按 E-0232 的口径：**先穷举写入点，再把它们逐个登记进网**；将来新增 / 删掉一个写价点，
本网当场红，逼着改动方把「这个点写哪一列、为什么」想一遍。

## 扫描面（如实声明，⛔ 不自称「全仓」）

扫 `src/apps/business/peer_inbound.py` **与** `src/apps/business/hall_shadow.py` 的 **AST**
（N5 起同批扩面：隐藏单派生的写价面也必须进网；键首格带**文件维度**，同名函数不会被并桶），
只认**四种**写入形态：

1. `X.total_price = …` / `X.hall_price = …`（`ast.Attribute` 赋值，含 `row.remote_hall_price`）；
2. `X["remote_hall_price"] = …`（`ast.Subscript` 赋值，reprice 支的 `updates` 字典）；
3. **调用关键字** `total_price=…` / `hall_price=…` / `remote_hall_price=…`（三个 `PRICE_FIELDS`
   全认：`get_or_create` 的 `defaults=` 与 `create(...)` 的直写都在这一档）；
4. **字典字面量**里以 `PRICE_FIELDS` 为键的值（`defaults={...}`、`fields = {...}`）——
   它们的去向是 `get_or_create(defaults=…)` / `.update(**fields)`；**键本身**才是价格写点，
   所以登记的是那一段字典字面量（`fields` 被三处 `.update(**fields)` 共用，只登记一次）；
**不在内**（本网的如实边界）：`save(update_fields=[...])` 里「值从哪来」（那只列字段名、不写值）、
`payload.get("hall_price")` 这类**读**、以及在两个被扫文件之外构造 / 动态拼出来的
`**dict`（两个文件当前没有这种写价点；将来出现了要**同一次**把扫描面扩上去，
⛔ 别让它静默漏过去）。

## 每个点的判据 = 「写哪一列 + 为什么」

- 订单流：`total_price` = 客户价（进账侧），`hall_price` = 下家给打手的价；
- 大厅镜像行：`remote_hall_price` = 上家发单价（显示价 = 它 × 本层百分比）；
- 任何一行只要**没有**写价理由，就必须从这里消失（或者明确标成登记过的例外）。
"""
from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_APPS = Path(__file__).resolve().parent.parent / "apps"
#: 要扫的生产文件（N5 起**两个**：入向事件的写价面 + 隐藏单派生的写价面）。
#: ⚠️ 键里带**文件维度**：两文件的写价函数名今天不碰撞，但「恰好集合」网不吃这个保证
#: （将来同名函数会被静默并桶、少登记一条也不红）。
SCANNED_FILES = {
    "business/peer_inbound.py": _APPS / "business" / "peer_inbound.py",
    "business/hall_shadow.py": _APPS / "business" / "hall_shadow.py",
}

#: 会被当成「价格列」的字段名（镜像行那一列也在这张网里：大厅负价不传播是同一件事）。
PRICE_FIELDS = ("total_price", "hall_price", "remote_hall_price")


def _enclosing_function(parents: list[ast.AST]) -> str:
    for node in reversed(parents):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _branch_chain(node: ast.AST, parents: list[ast.AST]) -> tuple[str, ...]:
    """从最外层到最内层，记录「这条写入落在哪些 if 的哪一侧」（只取最后两层，够分辨）。"""
    chain: list[str] = []
    for index, parent in enumerate(parents):
        if not isinstance(parent, ast.If):
            continue
        child = parents[index + 1] if index + 1 < len(parents) else None
        if any(child is item for item in parent.body):
            side = "body"
        elif any(child is item for item in parent.orelse):
            side = "else"
        else:  # pragma: no cover - 结构上到不了
            side = "?"
        chain.append(f"{ast.unparse(parent.test)} [{side}]")
    return tuple(chain[-2:])


#: 一个价格键所在字典的「去向」：`defaults=` / 具名变量（`fields[...]`）/ 其它字面量。
def _dict_form(parents: list[ast.AST]) -> str:
    parent = parents[-2] if len(parents) >= 2 else None
    if isinstance(parent, ast.keyword) and parent.arg == "defaults":
        return "defaults[...]"
    if (
        isinstance(parent, ast.Assign)
        and len(parent.targets) == 1
        and isinstance(parent.targets[0], ast.Name)
    ):
        return f"{parent.targets[0].id}[...]"
    return "dict-literal[...]"


class _PriceWriteCollector(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.stack: list[ast.AST] = []
        self.found: list[tuple[str, str, str, str, tuple[str, ...], str]] = []

    def visit(self, node: ast.AST) -> None:  # noqa: D401 - 维护祖先栈
        self.stack.append(node)
        super().visit(node)
        self.stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            field: str | None = None
            form: str | None = None
            if isinstance(target, ast.Attribute) and target.attr in PRICE_FIELDS:
                field = target.attr
                form = ast.unparse(target)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in PRICE_FIELDS
            ):
                field = str(target.slice.value)
                form = ast.unparse(target)
            if field is not None and form is not None:
                self.found.append((
                    self.relative_path,
                    _enclosing_function(self.stack),
                    field,
                    form,
                    _branch_chain(node, self.stack),
                    ast.unparse(node.value),
                ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        #: 三个价格列都认（`remote_hall_price=` 是评审抓过的漏网：镜像行的
        #: 建行 / 回写走的是 queryset 的 `.update(remote_hall_price=…)` 与 `create(...)`）。
        for keyword in node.keywords:
            if keyword.arg in PRICE_FIELDS:
                self.found.append((
                    self.relative_path,
                    _enclosing_function(self.stack),
                    keyword.arg,
                    "kwarg",
                    _branch_chain(node, self.stack),
                    ast.unparse(keyword.value),
                ))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        #: 字典字面量里的价格键：`defaults={...}`（建行）与 `fields = {...}`（条件更新）
        #: 都是**落库的值**，⛔ 不是读（读走 `payload.get(...)`，形态不同、抓不到这里）。
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in PRICE_FIELDS):
                continue
            self.found.append((
                self.relative_path,
                _enclosing_function(self.stack),
                str(key.value),
                _dict_form(self.stack),
                _branch_chain(node, self.stack),
                ast.unparse(value),
            ))
        self.generic_visit(node)


def _collect() -> set[tuple[str, str, str, str, tuple[str, ...], str]]:
    writes: set[tuple[str, str, str, str, tuple[str, ...], str]] = set()
    for relative_path, path in SCANNED_FILES.items():
        collector = _PriceWriteCollector(relative_path)
        collector.visit(ast.parse(path.read_text(encoding="utf-8")))
        writes.update(collector.found)
    return writes


#: 写价点 → **为什么**（这张表与实现**恰好集合**：多一个 / 少一个都算红）。
#: 键 = (相对文件, 函数, 列, 写入形态, 分支链, 表达式)。
PRICE_WRITE_SITES: dict[tuple[str, str, str, str, tuple[str, ...], str], str] = {
    (
        "business/peer_inbound.py",
        "_promote_claiming_mirror",
        "hall_price",
        "order.hall_price",
        ("price is not None and order.hall_price != price [body]",),
        "price",
    ): "A6 转正覆盖：打手价按本层展示价落（D-0307 第 4 条明确保留）",
    (
        "business/peer_inbound.py",
        "_landing_hall_price",
        "remote_hall_price",
        "row.remote_hall_price",
        ("not _amounts_equal(row.remote_hall_price, incoming) [body]",),
        "incoming",
    ): "镜像行的价格事实列回写（内存对象；A6 转正后上家又来价；⛔ 不写 Order 的价）",
    # ⭐ 独立评审 B1：下面四处以前**号称覆盖、实际没抓到**（扫描面只认两列 /
    #    只认赋值与两列关键字）。现在三列关键字 + `defaults=` + `fields` 字典全在扫描面里。
    (
        "business/peer_inbound.py",
        "_landing_hall_price",
        "remote_hall_price",
        "kwarg",
        ("not _amounts_equal(row.remote_hall_price, incoming) [body]",),
        "incoming",
    ): "同一处回写的**落库那一笔**（`.update(remote_hall_price=…)`；与上一行同一件事，⛔ 只改内存 = 假成功）",
    (
        "business/peer_inbound.py",
        "_hall_mirror_upsert_throw",
        "remote_hall_price",
        "defaults[...]",
        ("pull_mode [body]",),
        "price",
    ): "拉取 upsert 的**建行**默认值：镜像行价格事实列（D-0309 拉取路负价闸在上游已挡，⛔ 不写 Order）",
    (
        "business/peer_inbound.py",
        "_hall_mirror_upsert_throw",
        "remote_hall_price",
        "kwarg",
        ("pull_mode [else]", "existing is None [body]"),
        "price",
    ): "推送 upsert 的**建行**直写（推送支不走 `get_or_create` ⇒ 两条建行路都要登记）",
    (
        "business/peer_inbound.py",
        "_hall_mirror_upsert_throw",
        "remote_hall_price",
        "fields[...]",
        (),
        "price",
    ): "`fields` 字典 → 复活两支 / `listed` 刷新支的 `.update(**fields)`（价格事实列随对端改价刷新；负价不传播）",
    (
        "business/peer_inbound.py",
        "_apply_hall_event",
        "remote_hall_price",
        "updates['remote_hall_price']",
        ("reprice_price is not None and (not negative_reprice) [body]",),
        "reprice_price",
    ): "hall.reprice 落地：负价按「没带价」处理（D-0306 大厅路）",
    (
        "business/peer_inbound.py",
        "apply_events",
        "total_price",
        "kwarg",
        ("link is None [body]",),
        "incoming_price if incoming_price is not None else 0",
    ): "建单支：客户价取上家原始发单价",
    (
        "business/peer_inbound.py",
        "apply_events",
        "hall_price",
        "kwarg",
        ("link is None [body]",),
        "peer_hall.price_at_percent(incoming_price, new_percent) if new_percent is not None else incoming_price",
    ): "建单支：打手价按入场比例折算，未配置时沿用上家原价",
    (
        "business/peer_inbound.py",
        "apply_events",
        "total_price",
        "order.total_price",
        ("kind == 'order.update' [body]", "incoming_total_price is not None [body]"),
        "incoming_total_price",
    ): "D-0307：order.update 改价只动客户价（显式 0 落 0、负价照落；`is None` 判据）",
    (
        "business/peer_inbound.py",
        "apply_events",
        "total_price",
        "order.total_price",
        ("kind == 'order.update' [else]", "incoming_total_price is not None [body]"),
        "incoming_total_price",
    ): "接手类 / 撤单支：客户价取发单价，`is None` 判据（L-4）",
    (
        "business/peer_inbound.py",
        "apply_events",
        "hall_price",
        "order.hall_price",
        ("kind == 'order.update' [else]", "hall_price is not None [body]"),
        "hall_price",
    ): "接手类支：照抄发单价（P3/R10；D-0307 未改这一支）",
    # ── hall_shadow.py（⭐ N5 起同批扫入：隐藏单派生的写价面）────────────────
    (
        "business/hall_shadow.py", "ensure_for_card", "total_price",
        "defaults[...]", (), "_total_price(payload)",
    ): "建行默认值：隐藏单的客户价 = 对端发单价（与 `peer_inbound.apply_events` 建单支同一口径；"
       "转正后这张单就是客户看到的那张单）",
    (
        "business/hall_shadow.py", "ensure_for_card", "hall_price",
        "defaults[...]", (), "price",
    ): "建行默认值：隐藏单展示价 = `display_price(card)`（上家价 × 本层百分比，⛔ 不另算）",
    (
        "business/hall_shadow.py", "ensure_for_card", "total_price",
        "kwarg", (), "defaults['total_price']",
    ): "复活条件更新的客户价：与新载荷同源（N4：复活不是只翻状态，内容一起刷）",
    (
        "business/hall_shadow.py", "ensure_for_card", "hall_price",
        "kwarg", (), "price",
    ): "复活条件更新的展示价：卡这次落厅的展示价（新载荷 / 新百分比）",
    (
        "business/hall_shadow.py", "ensure_for_card", "total_price",
        "order.total_price", (), "defaults['total_price']",
    ): "复活后的实例回填（同一事务内后续代码 / 返回值读它；⛔ 落库判据仍是上面那条 `.update()`）",
    (
        "business/hall_shadow.py", "ensure_for_card", "hall_price",
        "order.hall_price", (), "price",
    ): "复活后的实例回填（同上；⛔ 不把回填当成「内容已刷新」的证明）",
    (
        "business/hall_shadow.py", "sync_hidden_order_price", "hall_price",
        "kwarg", (), "price",
    ): "⭐ N5：`hall.reprice` / 改百分比把展示价同步到 `WAITING_GRAB` 隐藏单（唯一落价口；"
       "`update()` 不触发 `auto_now` ⇒ 同一条显式带 `updated_at`）",
    (
        "business/hall_shadow.py", "sync_hidden_order_price", "hall_price",
        "order.hall_price", (), "price",
    ): "⭐ N5：同一 helper 把新价回填内存实例（供 `queue_price_update` 的同一实例；⛔ DB 行才是判据）",
}


class PriceWriteSitesNetTests(SimpleTestCase):
    """恰好集合：**两个文件**的写价点 = 上表（每一个都带一句「为什么」）。"""

    def test_the_write_sites_are_exactly_the_registered_set(self):
        found = _collect()
        expected = set(PRICE_WRITE_SITES)
        self.assertEqual(
            found - expected,
            set(),
            "新增了没登记的写价点（想清楚它写哪一列、为什么，再登记进 PRICE_WRITE_SITES）",
        )
        self.assertEqual(
            expected - found,
            set(),
            "登记过的写价点不见了（删掉它就要同一次从表里删掉，别留下说假话的登记）",
        )

    def test_every_registered_site_carries_a_reason(self):
        for site, why in PRICE_WRITE_SITES.items():
            with self.subTest(site=site):
                self.assertTrue(why and why.strip(), "写价点必须写明理由")

    def test_the_order_update_branch_never_writes_the_taker_price(self):
        """⭐ D-0307 的关键不变量：`order.update` 支**一个 `hall_price` 写入点都没有**。"""
        offenders = [
            site for site in _collect()
            if site[1] == "apply_events"
            and site[2] == "hall_price"
            and "kind == 'order.update' [body]" in site[4]
        ]
        self.assertEqual(offenders, [], "order.update 支又去改打手价了（D-0307 不许）")

    def test_every_price_field_is_written_somewhere_in_the_scanned_files(self):
        """扫描面自证：三个字段名真的都有写点（否则整张网对某一列会「恒绿」）。

        ⚠️ 与旧版（只断言「字段名出现在文件文本里」）的区别：`remote_hall_price` 在文件里
        出现十几次（读也出现），**文本出现 ≠ 有写点**。这里要求它出现在 `_collect()` 的
        **写入点**里——正是本网「号称覆盖」要自证的那一半。
        """
        written = {site[2] for site in _collect()}
        for field in PRICE_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, written)

    def test_the_mirror_price_write_points_are_all_registered(self):
        """⭐ B1 的回归钉：`remote_hall_price` 的四个落库形态**各在表里**（⛔ 别再退回假覆盖）。

        三列关键字 / `defaults=` / `fields` 字典是这次扩上去的扫描面；它们抓得到的点必须
        同时出现在 `PRICE_WRITE_SITES` 里——只扩扫描面而不登记，本网会变成「一扩就红」，
        下一个人就会把扫描面改回去。
        """
        registered = set(PRICE_WRITE_SITES)
        forms = {site[3] for site in registered if site[2] == "remote_hall_price"}
        self.assertEqual(
            forms,
            {"row.remote_hall_price", "kwarg", "defaults[...]", "fields[...]", "updates['remote_hall_price']"},
            "镜像价的落库形态集合变了：新增形态要同一次登记进表（并写清为什么）",
        )
