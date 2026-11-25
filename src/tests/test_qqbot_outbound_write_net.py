"""⭐ 结构性网：`QQBotOutboundMessage` 的**写入点粒度**清单（照
`tests/test_order_time_fields.py::TimeFieldWritePathNetTests` 的形态）。

## 为什么要有这张网

发件箱表的状态机（`pending → sent/failed`、退避、降级）散在 `push.py`（建行）与
`worker.py`（改状态）两处——新增一个写入点而不回答「它写什么状态、为什么写不出
别的状态」，就会重演 E-0175 / 批次 4 的缺陷类：**单测全绿、线上状态对不上**。
本网按「一行一个写入点」逼每个落点登记它的表达与判据；多一处、少一处、换表达式都红。

## 扫描面的**如实声明**（⛔ 不是「全仓」，只是它声明的这一块）

- **文件**：只扫 `apps/qqbot/push.py` 与 `apps/qqbot/worker.py`
  （`QQBotOutboundMessage` 的写入只可能发生在这两个文件——models.py 只定义、
  events.py / 命令层不写它；这条边界有专门一条用例钉着，越界即红）；
- **形态**：只认
  ⓪ `QQBotOutboundMessage.objects.<create|get_or_create|update_or_create|bulk_create|bulk_update|update>(…)` 调用
  （取它的 `kind=` 实参当表达式）；与
  ① `X.<status|scheduled_at|msg_type_eff> = <表达式>` 的属性赋值；
- **不在网内**（边界，故意）：
  - `defaults={…}` / `create(…)` **内层字典**里的键（本仓今天的建行全部走显式 kwargs，实测）；
  - queryset **存进变量再** `.update(status=…)`（今天 `apps/qqbot` 下 0 处，grep 复核；
    真要加一处，本网看不见——这正是「作用域声明」的含义）；
  - `row.attempts += 1`（AugAssign，不是本网的三类属性赋值之一；`attempts` 不是状态）；
  - `binding.*` 的写入（`markdown_ok` 等——那是 `QQBotBinding`，另一张表）。
"""
import ast
import collections
import pathlib
import re

from django.test import TestCase

from apps.qqbot.models import QQBotOutboundMessage

_SRC = pathlib.Path(__file__).resolve().parents[1]
_APPS = _SRC / "apps"

#: 只扫这两个文件（边界声明见模块 docstring）。
_WRITER_FILES = ("qqbot/push.py", "qqbot/worker.py")

_BLOCK_TYPES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)

#: ⓪ 类落点的 ORM 方法白名单。
_QQBOT_WRITE_METHODS = (
    "create", "get_or_create", "update_or_create", "bulk_create", "bulk_update", "update",
)

#: ① 类落点盯的属性（`status` 是主题；`scheduled_at` / `msg_type_eff` 是它的两个伴随列
#: ——退避与降级都靠它们表达，漏盯就看不见「顺延」「降级」这两类写法）。
_TRACKED_ATTRS = ("status", "scheduled_at", "msg_type_eff")


def _walk_own(node):
    """子树内全部节点，⛔ 不下钻嵌套函数 / 类体（照 test_order_time_fields 同款）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_own(child)


def _scopes(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{scope}.{child.name}" if scope else child.name
                found[name] = child
                walk(child, name)
            else:
                walk(child, scope)

    walk(tree, "")
    return tree, found


def _innermost_block(tree, node):
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    cursor = parents.get(node)
    while cursor is not None and not isinstance(cursor, _BLOCK_TYPES):
        cursor = parents.get(cursor)
    return cursor


def _block_assigns_attr(block, attr):
    """这一支里（不含嵌套函数/类体）有没有对 `attr` 的属性赋值。"""
    if block is None:
        return False
    for sub in _walk_own(block):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Attribute) and target.attr == attr:
                    return True
    return False


def _outbound_write_sites():
    """实测的写入点清单：`{(文件, 作用域, 形态, 第几处): {"expr", "node", "block"}}`。"""
    sites = {}
    for rel in _WRITER_FILES:
        path = _APPS / rel
        tree, scopes = _scopes(path)
        for name, scope in scopes.items():
            counters = collections.Counter()
            for sub in _walk_own(scope):
                recorded = None
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr in _TRACKED_ATTRS
                        ):
                            recorded = ("assign", ast.unparse(sub.value))
                elif isinstance(sub, ast.Call):
                    text = ast.unparse(sub.func)
                    if text.startswith("QQBotOutboundMessage.objects."):
                        method = text.rsplit(".", 1)[-1]
                        if method in _QQBOT_WRITE_METHODS:
                            for kw in sub.keywords:
                                if kw.arg == "kind":
                                    recorded = (method, ast.unparse(kw.value))
                if recorded is not None:
                    kind, expr = recorded
                    counters[kind] += 1
                    key = (rel, name, kind, counters[kind])
                    sites[key] = {
                        "expr": expr,
                        "node": sub,
                        "block": _innermost_block(tree, sub),
                    }
    return sites


#: ⭐⭐ **写入点登记表**（键与 `_outbound_write_sites()` 同形；`verdict` 的可执行判据
#: 在下面各条测试里，⛔ 不是注释声称）。
OUTBOUND_WRITE_SITES = {
    # ── push.py：建行（只能产出默认 pending）────────────────────────────────
    ("qqbot/push.py", "queue_hall_push", "create", 1): {
        "expr": "QQBotOutboundMessage.Kind.HALL_PUSH",
        "verdict": "create_pending",
        "why": "大厅事件的建行入口：不传 status ⇒ 落模型默认 pending；"
               "同一 order+group 不去重不更新（一条事件一批新行）",
    },
    ("qqbot/push.py", "queue_test_push", "create", 1): {
        "expr": "QQBotOutboundMessage.Kind.TEST_PUSH",
        "verdict": "create_pending",
        "why": "页面「发送测试」的建行入口：同上，只建 pending",
    },
    ("qqbot/push.py", "queue_passive_reply", "create", 1): {
        "expr": "QQBotOutboundMessage.Kind.PASSIVE_REPLY",
        "verdict": "create_pending",
        "why": "被动回复建行：>5 条 / 窗口超时**先拒绝**（return False 不建行）"
               "⇒ 只有闸内那一支能到 create",
    },
    # ── worker.py：状态机的全部落点 ─────────────────────────────────────────
    ("qqbot/worker.py", "GroupSender._handle_row", "assign", 1): {
        "expr": "QQBotOutboundMessage.Status.FAILED",
        "verdict": "failed_terminal",
        "why": "①日限分支：写的是字面量 FAILED（error_code=DAILY_LIMIT）——"
               "它写不出 sent：分支里没有发送动作",
    },
    ("qqbot/worker.py", "GroupSender._handle_row", "assign", 2): {
        "expr": "now + timedelta(seconds=wait)",
        "verdict": "defer",
        "why": "②群窗口满：只顺延 `scheduled_at`（status 原样 pending），"
               "靠「下轮 drain 会重新抽到」留下这条行",
    },
    ("qqbot/worker.py", "GroupSender._send", "assign", 1): {
        "expr": "QQBotOutboundMessage.MsgType.TEXT",
        "verdict": "downgrade_flip",
        "why": "markdown 权限降级：改的是 `msg_type_eff`（伴随列），"
               "⛔ 不动 status——行的生死由随后的发送结果决定",
    },
    ("qqbot/worker.py", "GroupSender._send", "assign", 2): {
        "expr": "QQBotOutboundMessage.Status.SENT",
        "verdict": "sent_terminal",
        "why": "④发送成功支：字面量 SENT + `sent_at` + `qq_message_id`——"
               "只有 `_attempt_send` 回 ok 才进这支",
    },
    ("qqbot/worker.py", "GroupSender._send", "assign", 3): {
        "expr": "timezone.now() + timedelta(seconds=RETRY_BACKOFF_SECONDS)",
        "verdict": "retry_defer",
        "why": "可重试错误：`attempts+1`（AugAssign，不属本网三类）+ 只顺延 "
               "`scheduled_at`；status 原样 pending，靠下轮 drain 重抽",
    },
    ("qqbot/worker.py", "GroupSender._mark_failed", "assign", 1): {
        "expr": "QQBotOutboundMessage.Status.FAILED",
        "verdict": "failed_terminal",
        "why": "终态收口（永久错误 / 重试烧完）：字面量 FAILED——"
               "调用点只有「不可重试」与「attempts 达上限」两支",
    },
}


class OutboundWritePathNetTests(TestCase):
    """登记表 ↔ 实测扫描**恰好相等** + 每档 verdict 的可执行判据。"""

    def test_measured_sites_exactly_match_the_registered_table(self):
        """多一处、少一处、换表达式都红（新增写入点必须回来登记）。"""
        measured = {key: value["expr"] for key, value in _outbound_write_sites().items()}
        self.assertEqual(
            measured,
            {key: entry["expr"] for key, entry in OUTBOUND_WRITE_SITES.items()},
            "QQBotOutboundMessage 的写入点与登记表不一致——新增/改动写入点必须"
            "回来登记它写什么、判据是什么",
        )

    def test_every_verdict_is_one_of_the_known_kinds(self):
        for key, entry in OUTBOUND_WRITE_SITES.items():
            with self.subTest(site=key):
                self.assertIn(
                    entry["verdict"],
                    {"create_pending", "failed_terminal", "sent_terminal",
                     "defer", "retry_defer", "downgrade_flip"},
                )
                self.assertTrue(entry["why"], f"{key} 缺「为什么」")

    def test_model_default_status_is_pending(self):
        """`create_pending` 三行的判据地基：建行不传 status ⇒ 默认就是 pending。"""
        field = QQBotOutboundMessage._meta.get_field("status")
        self.assertEqual(field.default, QQBotOutboundMessage.Status.PENDING)

    def test_create_sites_never_pass_status_and_always_name_a_kind(self):
        """`create_pending`：create 调用里**不传 status**（否则能绕过默认值）。"""
        for key, entry in _outbound_write_sites().items():
            if key[2] not in _QQBOT_WRITE_METHODS:
                continue
            with self.subTest(site=key):
                self.assertNotIn(
                    "status", entry["expr"],
                    f"{key} 的 create 表达式里出现 status——建行点不该改状态",
                )
                call = entry["node"]
                self.assertFalse(
                    any(kw.arg == "status" for kw in call.keywords),
                    f"{key} 的建行调用传了 status 实参",
                )

    def test_terminal_assigns_are_literal_enum_members(self):
        """`sent_terminal` / `failed_terminal`：写的是**字面量枚举成员**，不是变量。"""
        measured = _outbound_write_sites()
        for key, entry in OUTBOUND_WRITE_SITES.items():
            if entry["verdict"] not in {"sent_terminal", "failed_terminal"}:
                continue
            with self.subTest(site=key):
                expr = measured[key]["expr"]
                expected = "Status.SENT" if entry["verdict"] == "sent_terminal" else "Status.FAILED"
                self.assertIn(expected, expr)
                self.assertNotIn("raw", expr)
                self.assertNotIn("status_var", expr)

    def test_defer_sites_touch_only_scheduled_at_in_their_own_branch(self):
        """`defer` / `retry_defer`：那一支**只**顺延 `scheduled_at`，不写 status。"""
        measured = _outbound_write_sites()
        for key, entry in OUTBOUND_WRITE_SITES.items():
            if entry["verdict"] not in {"defer", "retry_defer"}:
                continue
            with self.subTest(site=key):
                self.assertIn("timedelta", measured[key]["expr"])
                self.assertFalse(
                    _block_assigns_attr(measured[key]["block"], "status"),
                    f"{key} 所在的那一支偷偷写了 status——顺延不该改状态",
                )

    def test_downgrade_site_touches_only_msg_type_eff(self):
        """`downgrade_flip`：那一支只改 `msg_type_eff`，status 不归它管。"""
        measured = _outbound_write_sites()
        key = ("qqbot/worker.py", "GroupSender._send", "assign", 1)
        self.assertIn("MsgType.TEXT", measured[key]["expr"])
        self.assertFalse(_block_assigns_attr(measured[key]["block"], "status"))


class WriterBoundaryTests(TestCase):
    """扫描面边界的两条钉子（模块 docstring 那段「如实声明」的可执行版）。"""

    #: 写面形状（与 `_QQBOT_WRITE_METHODS` 同一份语义；文本级判定与这条网的既有风格一致
    #: ——本网本来就是「如实声明的扫描面」，不是全仓 AST）。
    _WRITE_SHAPE = re.compile(
        r"QQBotOutboundMessage\.objects\.(create|get_or_create|update_or_create|"
        r"bulk_create|bulk_update|update)\("
    )

    def test_no_other_qqbot_file_writes_the_outbound_model(self):
        """`push.py` / `worker.py` 之外的 qqbot 文件不得出现 `QQBotOutboundMessage`
        的**写面**。

        ⚠️ **批次B 改准**：本条原先按**子串**判——连
        `QQBotOutboundMessage.objects.select_related(…)` 这种**纯读**也会被抓
        （批次B 的设置页发件箱列表 `qqbot/api/views.py` 当场红：
        `offenders == ['qqbot/api/views.py']`）。网的意图是「写只许发生在
        `push.py`（建行）与 `worker.py`（改状态）」，**不是**「别的文件不许读」
        ——`events.py` / 命令层也从不读它，所以子串粒度当时没有误伤。
        ⇒ 判据精确化成**写面形状**（`objects.<六种写方法>(`）：
        - 读（`select_related` / `filter` / 计数）在 views.py 合法化——这是**有意识的
          扩权**：设置页要分页列发件箱（批次B 任务书），读不产状态、写入点粒度网
          （`OutboundWritePathNetTests`）照旧只认 push/worker 两文件的写点；
        - ⛔ 判据本身**没有放宽**：六个写方法在 `push.py` / `worker.py` 之外出现
          照样红（把 `.objects` 换成 `_default_manager` 之类**绕文本**的写法仍属
          「把门藏出网外」，登记表那条「多一处写入点必须回来登记」的义务不变）。
        """
        offenders = []
        for path in sorted((_APPS / "qqbot").rglob("*.py")):
            rel = str(path.relative_to(_APPS))
            if rel in _WRITER_FILES or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if self._WRITE_SHAPE.search(text):
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_no_app_outside_qqbot_writes_the_outbound_model(self):
        """今天全仓只有 qqbot 写这张表（business 只经本批的钩子调 `push.queue_hall_push`）。"""
        offenders = []
        for path in sorted(_APPS.rglob("*.py")):
            rel = str(path.relative_to(_APPS))
            if rel.startswith("qqbot/") or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "QQBotOutboundMessage.objects" in text:
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "别的 app 开始直接写 QQBotOutboundMessage 了——必须进"
            " `test_qqbot_outbound_write_net.py` 的登记表并回答「写什么状态」",
        )
