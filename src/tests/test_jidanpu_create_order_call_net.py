"""⭐ 结构性网：SDK 原语 `OpenApiClient.create_order` 的**生产调用点**只许在 outbox 模块
（ D5，L-14）。

## 为什么要有这张网

`create_order` 是**录单的最后一跳**（`apps/business/jidanpu_openapi.py` 的官方 API 客户端
方法）：`project` / `remark` 两格**原样过线**、自己**不裁字节**，而对端那一格有**字节级**
上限 ≈732（E-0242 实测）。防线（`jidanpu_common._clip_project` / 700 字节）只住在
**分派层**——审计 L-14 给的两个选项（把防线下沉进原语，或立 AST 网）里，本仓选的是
**后者**：原语保持「原样过线」的形状，谁来调它必须有出处、且那个出处在出向边界裁过。
没有这张网，第二个调用点可以在任何角落里拿未裁的值直打上游（E-0242 当天 6 张卡单同形，
**静默烂在退避表里**），而普通门禁一个都不会红。

## 扫描面的**如实声明**（⛔ 不是「全仓」，只是它声明的这一块）

- **文件**：`src/apps/**/*.py`（排除 `migrations/` 与 `__pycache__`）。
  ⛔ **不扫** `src/tests/**`（用例里桩掉 client 的调用不算生产调用点）；
  ⛔ **不扫** `src/jidanpu/**`（vendor 形状的 SDK 层，今天没有任何 `.create_order(` 调用，
  grep 复核过——它若开始调自己反而是更大的事，不该由这张网兜）；
- **形态**：只认 `ast.Call` 且 `func` 是 `ast.Attribute`、`attr == "create_order"`
  （即 `<expr>.create_order(...)` 的**属性调用**）。
  ⚠️ **重名豁免的事实**：`apps/business/order_intake.py::create_order` 是我方**建单**的
  内部函数，与本方法**没有任何关系**、只是重名；它的调用方（`api/order_views.py` /
  `openapi/api/order_create_views.py`）全部是**名字**调用（`from … import create_order`
  ⇒ `ast.Name`），落不进本网的形态——这条边界有专门一条用例钉着（见下）。
- ⚠️ 类上的**引用**（`OpenApiClient.create_order` 不带调用括号，比如传方法引用）
  不在网内：今天 0 处（grep 复核）；真出现时它是「准备调」的形状，登记义务同一条。
"""
import ast
import pathlib
import unittest

_SRC = pathlib.Path(__file__).resolve().parents[1]
_APPS = _SRC / "apps"

#: 现役**唯一**的生产调用点（与 `create_order` docstring 里的禁用牌同一句口径）。
OUTBOX_REL = "business/jidanpu_member_openapi_outbox.py"
#: 禁用牌必须钉住的关键句（docstring 被人删了 ⇒ 这条网也红——牌子不能只活在 git 历史里）。
_DOCSTRING_PHRASE = "必须已裁字节"

_MUTATION = (
    "\n\ndef _net_probe_violation(client):  # 变异探针（跑完立即反向补丁还原）\n"
    "    return client.create_order(project='未裁的合成探针值')\n"
)


def _create_order_call_sites() -> dict[str, list[int]]:
    """全 `src/apps`（排除迁移）里 `.create_order(` **属性调用**点：`{相对路径: [行号…]}`。"""
    sites: dict[str, list[int]] = {}
    for path in sorted(_APPS.rglob("*.py")):
        if "migrations" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_order"
        ]
        if lines:
            sites[str(path.relative_to(_APPS))] = lines
    return sites


class CreateOrderCallNetTests(unittest.TestCase):
    """生产调用点**恰好**只在 outbox 模块 + 禁用牌在位 + 网能红（变异自证）。"""

    def test_the_only_production_call_site_is_the_outbox_module(self):
        """多出任何一处 `.create_order(` 调用 ⇒ 本用例红（新增调用点必须回这张网登记）。"""
        sites = _create_order_call_sites()
        self.assertEqual(
            sites,
            {OUTBOX_REL: sites[OUTBOX_REL]},
            "create_order 的生产调用点超出了 outbox 模块——它是**不裁字节**的录单最后一跳，"
            "新调用点必须先过 `jidanpu_common._clip_project` 并回本网登记",
        )

    def test_the_outbox_call_site_feeds_a_clipped_body(self):
        """唯一那个调用点的实参来自 `_create_body(...)`（出向裁剪在它里面，E-0242 的网另有钉）。"""
        path = _APPS / OUTBOX_REL
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_order"
        ]
        self.assertEqual(len(calls), 1)
        (call,) = calls
        self.assertEqual(len(call.keywords), 1)
        self.assertEqual(call.keywords[0].arg, None, "实参必须是 ** 展开的一包")
        self.assertIsInstance(call.keywords[0].value, ast.Call)
        self.assertEqual(call.keywords[0].value.func.id, "_create_body")

    def test_the_do_not_use_sign_is_still_on_the_primitive(self):
        """禁用牌在位：`create_order` 的 docstring 必须点明「调用方必须已裁字节」。"""
        source = (_APPS / "business/jidanpu_openapi.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = [
            ast.get_docstring(node) or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "create_order"
        ]
        self.assertTrue(
            any(_DOCSTRING_PHRASE in text for text in docstrings),
            "create_order 的禁用牌被摘了——它是「原语不裁字节、调用方必须先裁」的当场说明",
        )

    def test_the_name_call_sites_of_order_intake_create_order_stay_out_of_scope(self):
        """边界钉子：`order_intake.create_order`（我方建单的重名函数）走**名字**调用，
        落不进本网形态——把它换成属性调用会红，重名永远不该成为漏网的借口。"""
        for rel in ("business/api/order_views.py", "openapi/api/order_create_views.py"):
            with self.subTest(file=rel):
                tree = ast.parse((_APPS / rel).read_text(encoding="utf-8"))
                attribute_calls = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_order"
                ]
                self.assertEqual(attribute_calls, [], f"{rel} 出现了属性形态的 create_order 调用")

    def test_the_net_can_actually_turn_red(self):
        """⭐ 变异自证（E-0064：每条钉子都要有一次「把它弄红」的证明）：
        往**生产模块**里临时加一个违规调用点 ⇒ 扫描面**真的**抓到它、断言**真的**会炸；
        跑完用**反向补丁**还原（⛔ 不用 `git checkout`），并自证字节级复原。

        ⚠️ 变异从**当前文件原文**出发（每条用例各自读原文，⛔ 不叠在上一条变异上）；
        `finally` 保证还原（中途断言炸了也不能把探针留在工作区）。
        """
        target = _APPS / "business/jidanpu_openapi.py"
        original = target.read_text(encoding="utf-8")
        self.assertNotIn("_net_probe_violation", original, "上一轮的探针没还原干净")
        mutated = original + _MUTATION
        self.assertNotEqual(mutated, original, "变异没有生效（自证失败）")
        try:
            target.write_text(mutated, encoding="utf-8")
            self.assertEqual(
                target.read_text(encoding="utf-8"), mutated, "变异没有落盘（自证失败）"
            )
            sites = _create_order_call_sites()
            self.assertIn("business/jidanpu_openapi.py", sites, "扫描面没看见违规调用点")
            with self.assertRaises(AssertionError):
                # 与第一条用例**同一个**断言形状：违规调用点在场 ⇒ 网红。
                self.assertEqual(
                    sites, {OUTBOX_REL: sites[OUTBOX_REL]},
                    "变异在场时这条断言居然绿了 ⇒ 本网是摆设",
                )
        finally:
            target.write_text(original, encoding="utf-8")
        # ⭐ 还原自证：字节级回到原文（⛔ 不是「差不多」）。
        self.assertEqual(target.read_text(encoding="utf-8"), original)
