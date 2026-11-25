"""D-0377 回流（E-0232 口径「先枚举字段全部写入点」）：**内容面同步的两张穷举网**。

## 网一：`update_order_fields` 的调用方（发送侧触发面）

编辑订单内容 ⇒ 下行同步（`peer_dispatch.notify_order_content_changed`）挂在
`update_order_fields` 的 `atomic()` 里 ⇒ 「这条编辑路会不会把内容变更带给下家」
完全由「走没走这个落库口 + changed 里有没有内容面字段」决定。将来多出第五个调用方
（或其中一个消失）本网当场红，逼着改动方把「这条路上的内容变更去哪」写清楚。
「内容面变更会被同步」的行为判据（真 HTTP、断台账行）在
`tests/test_peer_hall_dispatch.py::ContentEditTriggerTests` 与
`tests/test_jidanpu_member_triggers.py::ProjectEditTests`，本网只钉调用面清单。

## 网二：peer 接收侧内容列写入点

照 `test_peer_price_write_sites.py` 形状：扫
`apps/business/peer_inbound.py` **与** `apps/business/hall_shadow.py` 的 **AST**，
只认三种写入形态（属性赋值 / 调用关键字 / 字典字面量键——`defaults=` 与
`fields = {...}` 都在这一档）。**扫描面如实声明**（⛔ 不自称「全仓」）：本网只认
**六个内容字段名**（remark / project_text / customer_phone / customer_qq / content /
game）在这两个文件里的写入点；`order_writes.py` 的本地编辑面不经这里（它的触发判据
由调用方网 + `ProjectEditTests` 钉）；`save(update_fields=[...])` 里「值从哪来」、
`payload.get(...)` 这类**读**不在内。

## 每个点的判据 = 「写给哪个模型的哪一列 + 为什么」

订单行（Order）六列、镜像行（PeerHallMirror）的 game、截图行（OrderScreenshot）的
remark 各是各的事实，理由里点名模型；任何一行写不出理由就必须从表里消失。
"""
from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_APPS = Path(__file__).resolve().parent.parent / "apps"
_SRC = _APPS.parent

#: 网二要扫的生产文件（键带文件维度，同名函数不会被并桶——与价格网同一条纪律）。
SCANNED_FILES = {
    "business/peer_inbound.py": _APPS / "business" / "peer_inbound.py",
    "business/hall_shadow.py": _APPS / "business" / "hall_shadow.py",
}

#: 内容面字段（发送侧触发面见 `peer_dispatch.CONTENT_SYNC_FIELDS`；这里多一个
#: `game` 的写入面视角——FK 落库点也是内容落地的一部分）。
CONTENT_FIELDS = (
    "remark", "project_text", "customer_phone", "customer_qq", "content", "game",
)

#: D-0377 新开的更新支：这六列在这一支的分支链里**各有一条**（缺一即红）。
UPDATE_BRANCH_CHAIN = ("link is None [else]", "kind == 'order.update' [body]")


def _enclosing_function(parents: list[ast.AST]) -> str:
    parts: list[str] = []
    for node in parents:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(node.name)
        elif isinstance(node, ast.ClassDef):
            parts.append(node.name)
    return ".".join(parts[-2:]) if parts else "<module>"


def _branch_chain(node: ast.AST, parents: list[ast.AST]) -> tuple[str, ...]:
    """从最外层到最内层，记录「这条写入落在哪些 if 的哪一侧」（只取最后两层）。"""
    chain: list[str] = []
    for index, parent in enumerate(parents):
        if not isinstance(parent, ast.If):
            continue
        child = parents[index + 1] if index + 1 < len(parents) else None
        if child is not None and any(child is item for item in parent.body):
            side = "body"
        elif child is not None and any(child is item for item in parent.orelse):
            side = "else"
        else:  # pragma: no cover - 结构上到不了
            side = "?"
        chain.append(f"{ast.unparse(parent.test)} [{side}]")
    return tuple(chain[-2:])


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


class _ContentWriteCollector(ast.NodeVisitor):
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
            if isinstance(target, ast.Attribute) and target.attr in CONTENT_FIELDS:
                self.found.append((
                    self.relative_path,
                    _enclosing_function(self.stack),
                    target.attr,
                    ast.unparse(target),
                    _branch_chain(node, self.stack),
                    ast.unparse(node.value),
                ))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg in CONTENT_FIELDS:
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
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in CONTENT_FIELDS):
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


def _collect_content_writes() -> set[tuple[str, str, str, str, tuple[str, ...], str]]:
    writes: set[tuple[str, str, str, str, tuple[str, ...], str]] = set()
    for relative_path, path in SCANNED_FILES.items():
        collector = _ContentWriteCollector(relative_path)
        collector.visit(ast.parse(path.read_text(encoding="utf-8")))
        writes.update(collector.found)
    return writes


def _collect_callers() -> set[tuple[str, str]]:
    """扫 `src/apps` 全树的 `update_order_fields(...)` 调用点（发送侧触发面的入口清单）。"""
    callers: set[tuple[str, str]] = set()
    for path in sorted(_APPS.rglob("*.py")):
        collector = _CallerCollector(str(path.relative_to(_APPS)))
        collector.visit(ast.parse(path.read_text(encoding="utf-8")))
        callers.update(collector.found)
    return callers


class _CallerCollector(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.stack: list[ast.AST] = []
        self.found: list[tuple[str, str]] = []

    def visit(self, node: ast.AST) -> None:  # noqa: D401 - 维护祖先栈
        self.stack.append(node)
        super().visit(node)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "update_order_fields":
            self.found.append((self.relative_path, _enclosing_function(self.stack)))
        self.generic_visit(node)


#: 网一：`update_order_fields` 调用方 → 这条路上的**内容变更会不会被同步**（恰好集合）。
#: 键 = (相对 apps 的文件, 限定作用域名)。
UPDATE_ORDER_FIELDS_CALLERS: dict[tuple[str, str], str] = {
    ("openapi/api/integration_orders.py", "IntegrationOrderDetailView.patch"):
        "业务 SDK 写入本地订单，写上下文禁用对下游的内容同步。",
    ("business/api/order_views.py", "OrderViewSet.partial_update"):
        "单笔 PATCH /orders/<单号>/：客服编辑保存的落库口 ⇒ 内容面变更**会被同步**"
        "（notify_order_content_changed 挂在核心的 atomic 内；行为判据"
        " tests/test_peer_hall_dispatch.py::ContentEditTriggerTests）",
    ("business/api/order_views.py", "batch_types.per_order"):
        "批量改订单类型只给核心传 order_type 一个键 ⇒ changed 永不含内容面字段"
        " ⇒ 同步闸恒空转（**为何不触发**：这批动作本来就没有内容可带）",
    ("business/api/platform_push_action_views.py", "_update_order_field"):
        "第三方平台动作只写 order_label / tag / urgent 列（set_label / set_tag /"
        " clear_tag / urgent）⇒ 都不在 CONTENT_SYNC_FIELDS ⇒ 不触发"
        "（**为何不触发**：本地标签无对外出口，发出去对端也看不见）",
    ("openapi/api/order_write_views.py", "OrderUpdateMixin.patch"):
        "开放平台对外改单端点：改的就是同一批内容列 ⇒ 内容面变更**会被同步**"
        "（同一落库口、同一份 changed 清单，⛔ 不给任何调用方开第二份判据）",
}

#: 网二：内容列写入点 → **写给哪个模型的哪一列 + 为什么**（恰好集合，多一个/少一个都红）。
#: 键 = (相对文件, 函数, 字段, 写入形态, 分支链, 表达式)。
CONTENT_WRITE_SITES: dict[tuple[str, str, str, str, tuple[str, ...], str], str] = {
    # ── peer_inbound.apply_events：建单支（首条事件带全量白名单，P2 既有）──────
    (
        "business/peer_inbound.py", "apply_events", "remark", "kwarg",
        ("link is None [body]",), "_peer_text_or_empty(payload, 'remark')",
    ): "建单支：首单落地的白名单 remark（Order 行；非字符串按「没带」写空，A1/H-1）",
    (
        "business/peer_inbound.py", "apply_events", "project_text", "kwarg",
        ("link is None [body]",),
        "_clip_to_column(payload.get('project_text') or '', Order, 'project_text')",
    ): "建单支：首单落地的白名单 project_text（Order 行，R19 剪刀）",
    (
        "business/peer_inbound.py", "apply_events", "customer_phone", "kwarg",
        ("link is None [body]",), "payload.get('customer_phone') or ''",
    ): "建单支：首单落地的白名单 customer_phone（Order 行）",
    (
        "business/peer_inbound.py", "apply_events", "customer_qq", "kwarg",
        ("link is None [body]",), "payload.get('customer_qq') or ''",
    ): "建单支：首单落地的白名单 customer_qq（Order 行）",
    (
        "business/peer_inbound.py", "apply_events", "content", "kwarg",
        ("link is None [body]",), "_peer_text_or_empty(payload, 'content')",
    ): "建单支：首单落地的白名单 content（Order 行）",
    (
        "business/peer_inbound.py", "apply_events", "game", "kwarg",
        ("link is None [body]",), "game",
    ): "建单支：首单落地按**名字**解析的游戏（Order.game；R21，id 不对齐）",
    # ── peer_inbound.apply_events：已建单的 order.update 支（⭐ D-0377 新开）────
    (
        "business/peer_inbound.py", "apply_events", "remark", "order.remark",
        UPDATE_BRANCH_CHAIN, "_peer_text_or_empty(payload, 'remark')",
    ): "⭐ D-0377：order.update 支落 remark——已转正正式单跟着上家改内容（类型闸同建单支）",
    (
        "business/peer_inbound.py", "apply_events", "project_text", "order.project_text",
        UPDATE_BRANCH_CHAIN,
        "_clip_to_column(payload.get('project_text') or '', Order, 'project_text')",
    ): "⭐ D-0377：order.update 支落 project_text（R19 剪刀同建单支）",
    (
        "business/peer_inbound.py", "apply_events", "customer_phone", "order.customer_phone",
        UPDATE_BRANCH_CHAIN, "payload.get('customer_phone') or ''",
    ): "⭐ D-0377：order.update 支落 customer_phone",
    (
        "business/peer_inbound.py", "apply_events", "customer_qq", "order.customer_qq",
        UPDATE_BRANCH_CHAIN, "payload.get('customer_qq') or ''",
    ): "⭐ D-0377：order.update 支落 customer_qq",
    (
        "business/peer_inbound.py", "apply_events", "content", "order.content",
        UPDATE_BRANCH_CHAIN, "_peer_text_or_empty(payload, 'content')",
    ): "⭐ D-0377：order.update 支落 content",
    (
        "business/peer_inbound.py", "apply_events", "game", "order.game",
        UPDATE_BRANCH_CHAIN, "game",
    ): "⭐ D-0377：order.update 支的游戏照建单支按名字 resolve（server/category/project"
       " 只有 id、无对齐键 ⇒ 不消费，peer_sync.py 登记）",
    # ── peer_inbound.apply_events：接手类 / 撤单支（P2 既有，D-0377 未改）──────
    (
        "business/peer_inbound.py", "apply_events", "remark", "order.remark",
        ("link is None [else]", "kind == 'order.update' [else]"),
        "_peer_text_or_empty(payload, 'remark')",
    ): "接手类 / 撤单支：整单白名单照抄落库（Order 行，P2 既有）",
    (
        "business/peer_inbound.py", "apply_events", "project_text", "order.project_text",
        ("link is None [else]", "kind == 'order.update' [else]"),
        "_clip_to_column(payload.get('project_text') or '', Order, 'project_text')",
    ): "接手类 / 撤单支：整单白名单照抄落库（Order 行，P2 既有）",
    (
        "business/peer_inbound.py", "apply_events", "customer_phone", "order.customer_phone",
        ("link is None [else]", "kind == 'order.update' [else]"),
        "payload.get('customer_phone') or ''",
    ): "接手类 / 撤单支：整单白名单照抄落库（Order 行，P2 既有）",
    (
        "business/peer_inbound.py", "apply_events", "customer_qq", "order.customer_qq",
        ("link is None [else]", "kind == 'order.update' [else]"),
        "payload.get('customer_qq') or ''",
    ): "接手类 / 撤单支：整单白名单照抄落库（Order 行，P2 既有）",
    (
        "business/peer_inbound.py", "apply_events", "content", "order.content",
        ("link is None [else]", "kind == 'order.update' [else]"),
        "_peer_text_or_empty(payload, 'content')",
    ): "接手类 / 撤单支：整单白名单照抄落库（Order 行，P2 既有）",
    (
        "business/peer_inbound.py", "apply_events", "game", "order.game",
        ("link is None [else]", "kind == 'order.update' [else]"), "game",
    ): "接手类 / 撤单支：游戏按名字解析后照抄（Order 行，P2 既有）",
    # ── peer_inbound：大厅镜像行 / 截图行（非订单行，理由里点名模型）────────────
    (
        "business/peer_inbound.py", "_hall_mirror_upsert_throw", "game", "defaults[...]",
        ("pull_mode [body]",), "game",
    ): "镜像行**建行**默认值（PeerHallMirror.game，拉取路；非订单行）",
    (
        "business/peer_inbound.py", "_hall_mirror_upsert_throw", "game", "kwarg",
        ("pull_mode [else]", "existing is None [body]"), "game",
    ): "镜像行**建行**直写（PeerHallMirror.game，推送路；非订单行）",
    (
        "business/peer_inbound.py", "_hall_mirror_upsert_throw", "game", "kwarg",
        ("existing.status == PeerHallMirror.Status.GONE [body]", "pull_mode [body]"),
        "game",
    ): "gone 复活的条件更新（PeerHallMirror.game；「看见了」即直接证据）",
    (
        "business/peer_inbound.py", "_hall_mirror_upsert_throw", "game", "kwarg",
        ("existing.status == PeerHallMirror.Status.GONE [body]",), "game",
    ): "gone 复活 / listed 刷新的 .update(game=…)（PeerHallMirror.game）",
    (
        "business/peer_inbound.py", "_hall_mirror_upsert_throw", "game", "kwarg",
        (), "game",
    ): "listed 刷新投影的 .update(game=…)（PeerHallMirror.game；卡跟着上家改游戏）",
    (
        "business/peer_inbound.py", "_land_hall_throw", "game", "kwarg", (), "game",
    ): "同一份 game 转传给 upsert（PeerHallMirror.game；调用关键字本身也是一个登记面）",
    (
        "business/peer_inbound.py", "_apply_screenshot", "remark", "kwarg",
        ("kind == SCREENSHOT_ADD_KIND [body]",),
        "_clip_to_column(remark if isinstance(remark, str) else '', OrderScreenshot, 'remark')",
    ): "OrderScreenshot.remark（截图备注列，随截图事件落地；⛔ 非订单内容面）",
    (
        "business/peer_inbound.py", "_apply_screenshot", "remark", "kwarg",
        ("kind == SCREENSHOT_ADD_KIND [body]", "img_type == OrderScreenshot.IMG_ORDER [body]"),
        "remark if isinstance(remark, str) else ''",
    ): "OrderScreenshot.remark（图片行的备注档；同上非订单内容面）",
    # ── hall_shadow.ensure_for_card：隐藏单派生（残差口径：已存在不刷新，本线不修）──
    (
        "business/hall_shadow.py", "ensure_for_card", "remark", "defaults[...]", (),
        "_text(payload, 'remark')",
    ): "隐藏单建行默认值（Order 行；⚠️ 已存在的隐藏单**不刷新**内容——计划残差，如实登记）",
    (
        "business/hall_shadow.py", "ensure_for_card", "project_text", "defaults[...]", (),
        "_clipped(_text(payload, 'project_text'), Order, 'project_text')",
    ): "隐藏单建行默认值（Order 行；残差同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_phone", "defaults[...]", (),
        "_text(payload, 'customer_phone')",
    ): "隐藏单建行默认值（Order 行；残差同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_qq", "defaults[...]", (),
        "_text(payload, 'customer_qq')",
    ): "隐藏单建行默认值（Order 行；残差同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "content", "defaults[...]", (),
        "_text(payload, 'content')",
    ): "隐藏单建行默认值（Order 行；残差同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "game", "defaults[...]", (),
        "card.game",
    ): "隐藏单建行默认值（Order.game = 卡解析出的那款；残差同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "remark", "kwarg", (),
        "defaults['remark']",
    ): "复活条件更新（.update(**defaults)）：复活那一拍内容跟着新载荷刷（N4 同族）",
    (
        "business/hall_shadow.py", "ensure_for_card", "project_text", "kwarg", (),
        "defaults['project_text']",
    ): "复活条件更新：同上（R19 剪刀在建行侧已裁）",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_phone", "kwarg", (),
        "defaults['customer_phone']",
    ): "复活条件更新：同上",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_qq", "kwarg", (),
        "defaults['customer_qq']",
    ): "复活条件更新：同上",
    (
        "business/hall_shadow.py", "ensure_for_card", "content", "kwarg", (),
        "defaults['content']",
    ): "复活条件更新：同上",
    (
        "business/hall_shadow.py", "ensure_for_card", "game", "kwarg", (),
        "card.game",
    ): "复活条件更新：同上",
    (
        "business/hall_shadow.py", "ensure_for_card", "remark", "order.remark", (),
        "defaults['remark']",
    ): "复活后的实例回填（内存实例；落库判据是上面那条 .update()，⛔ 别当「已刷」的证明）",
    (
        "business/hall_shadow.py", "ensure_for_card", "project_text", "order.project_text", (),
        "defaults['project_text']",
    ): "复活后的实例回填（同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_phone", "order.customer_phone", (),
        "defaults['customer_phone']",
    ): "复活后的实例回填（同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "customer_qq", "order.customer_qq", (),
        "defaults['customer_qq']",
    ): "复活后的实例回填（同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "content", "order.content", (),
        "defaults['content']",
    ): "复活后的实例回填（同上）",
    (
        "business/hall_shadow.py", "ensure_for_card", "game", "order.game", (),
        "card.game",
    ): "复活后的实例回填（同上）",
}


class UpdateOrderFieldsCallerNetTests(SimpleTestCase):
    """网一：`update_order_fields` 的调用方 = 登记表（每一个都写明内容变更的去向）。"""

    def test_the_callers_are_exactly_the_registered_set(self):
        found = _collect_callers()
        expected = set(UPDATE_ORDER_FIELDS_CALLERS)
        self.assertEqual(
            found - expected,
            set(),
            "多出的 update_order_fields 调用方：先想清楚这条路上的内容变更会不会同步"
            "（不触发就写明为何），再登记进 UPDATE_ORDER_FIELDS_CALLERS",
        )
        self.assertEqual(
            expected - found,
            set(),
            "登记过的调用方不见了（删掉它就要同一次从表里删掉，别留说假话的登记）",
        )

    def test_every_registered_caller_carries_a_reason(self):
        for caller, why in UPDATE_ORDER_FIELDS_CALLERS.items():
            with self.subTest(caller=caller):
                self.assertTrue(why and why.strip(), "调用方必须写明内容变更的去向")

    def test_the_trigger_funnel_has_exactly_one_call_site(self):
        """发送侧漏斗只挂在核心一处（`update_order_fields` 内）——第二处判据必然漂。"""
        dispatch = ast.parse(
            (_APPS / "business" / "order_writes.py").read_text(encoding="utf-8")
        )
        sites = [
            node
            for node in ast.walk(dispatch)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "notify_order_content_changed"
        ]
        self.assertEqual(
            len(sites), 1,
            "notify_order_content_changed 的调用点必须是唯一一处（order_writes 核心）",
        )


class ContentWriteSitesNetTests(SimpleTestCase):
    """网二：两个文件的内容列写入点 = 登记表（每一个都带一句「为什么」）。"""

    def test_the_write_sites_are_exactly_the_registered_set(self):
        found = _collect_content_writes()
        expected = set(CONTENT_WRITE_SITES)
        self.assertEqual(
            found - expected,
            set(),
            "新增了没登记的内容写入点（想清楚它写给哪个模型的哪一列、为什么，"
            "再登记进 CONTENT_WRITE_SITES）",
        )
        self.assertEqual(
            expected - found,
            set(),
            "登记过的内容写入点不见了（删掉它就要同一次从表里删掉，别留下说假话的登记）",
        )

    def test_every_registered_site_carries_a_reason(self):
        for site, why in CONTENT_WRITE_SITES.items():
            with self.subTest(site=site):
                self.assertTrue(why and why.strip(), "内容写入点必须写明理由")

    def test_every_content_field_is_written_somewhere_in_the_scanned_files(self):
        """扫描面自证：六个字段名真的都有写点（否则整张网对某一列「恒绿」）。"""
        written = {site[2] for site in _collect_content_writes()}
        for field in CONTENT_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, written)

    def test_the_d0377_update_branch_writes_all_six_order_fields(self):
        """⭐ D-0377 的钉子：order.update 支的分支链里**六列各有一条**——
        摘掉任何一列（少一次赋值）本网当场红（与行为判据互为备份）。"""
        by_field: dict[str, int] = {}
        for site in _collect_content_writes():
            if site[1] == "apply_events" and site[4] == UPDATE_BRANCH_CHAIN:
                by_field[site[2]] = by_field.get(site[2], 0) + 1
        self.assertEqual(
            {field: by_field.get(field, 0) for field in CONTENT_FIELDS},
            {field: 1 for field in CONTENT_FIELDS},
            "order.update 支的内容面写入点少了一条（D-0377 的落地面被削了）",
        )
