"""同一个文件里，同一个顶层名字不许绑到两个不同的来源。

## 为什么要这条网（E-0073）

2026-09-23 RBAC 收口线合 `main` 时，两条线**各自**在 `tests/test_submit_api.py` 顶层加了一个
`Role` 导入（一个是状态机角色 `apps.business.order_status.Role`，一个是 Django 模型
`apps.rbac.models.Role`），两行落在**不同区域** ⇒ `git` 报**无冲突**、自动合并成功；
运行期**后一行静默覆盖前一行** ⇒ `main` 自己那条新用例在 `Role.TAKER` 上 `AttributeError`。
⚠️ 两侧**单独跑都是绿的**（`main` 上只有一个 `Role`；分支上那条用例还不存在）⇒
`git` 的「无冲突」不等于「语义无冲突」，这一类**只有合并树上的全套门禁**能发现。

`git` 按行合并、不看绑定语义；Python 的模块级重复绑定本身也不报错（它就是普通的重新赋值）。
本网把这一类显式钉住：扫出「同一文件内同名 → 多个不同来源」并直接红。

## 口径与边界（如实，别读成「同名一律不许」）

- 只扫 `src/apps` / `src/tests` / `src/config` 下的 `*.py`，且只扫**模块级、非缩进**的 import 行。
  **函数内 / `if TYPE_CHECKING:` / `try:` 块里的导入不在覆盖内**（那些是刻意的局部遮蔽）。
- `import a.b` 规范化成绑到 `a`（`import urllib.error` 与 `import urllib.parse` 都只是把
  `urllib` 绑到同一个模块对象 ⇒ **不是**冲突，实测本仓 `agiso.py` / `xgj.py` 就是这种写法）。
- 括号续行里的名字（`from x import (\n  A,\n)`）**不计**：少报不误报。
- 同一个名字在**不同文件**里各自导入是正常的——冲突只在**同一个文件**内定义。
"""

import ast
import re
from pathlib import Path
from unittest import TestCase

REPO_SRC = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("apps", "tests", "config")

_FROM_RE = re.compile(r"^from\s+(\.*[\w\.]*)\s+import\s+(.+?)\s*$")
_IMPORT_RE = re.compile(r"^import\s+(.+?)\s*$")


def module_level_bindings(source: str) -> dict[str, set[str]]:
    """模块级顶层 import 的 `名字 → {来源}`（括号续行与缩进块内的导入不计）。"""
    bindings: dict[str, set[str]] = {}
    for line in source.split("\n"):
        if not line or line[0] in " \t#":
            continue
        m = _FROM_RE.match(line)
        if m:
            module, names = m.group(1), m.group(2).strip()
            if names.startswith("(") or names.startswith("*"):
                continue
            for part in names.split(","):
                part = part.split("#")[0].strip().rstrip("\\").strip()
                if part.endswith("(") or not part:
                    continue
                if " as " in part:
                    orig, alias = (x.strip() for x in part.split(" as ", 1))
                    bindings.setdefault(alias, set()).add(f"{module}.{orig}")
                elif part.isidentifier():
                    bindings.setdefault(part, set()).add(f"{module}.{part}")
            continue
        m = _IMPORT_RE.match(line)
        if m:
            for part in m.group(1).split(","):
                part = part.split("#")[0].strip()
                if not part:
                    continue
                if " as " in part:
                    mod, alias = (x.strip() for x in part.split(" as ", 1))
                    bindings.setdefault(alias, set()).add(mod)
                elif part.isidentifier() or "." in part:
                    bindings.setdefault(part.split(".")[0], set()).add(part.split(".")[0])
    return bindings


def shadowed_names(source: str) -> dict[str, set[str]]:
    """同一文件内绑到多个来源的名字（值 = 来源集合）。"""
    return {n: srcs for n, srcs in module_level_bindings(source).items() if len(srcs) > 1}


class ParserTests(TestCase):
    """先证明这段解析**能分辨**（否则下面的全仓判据可能恒真）。"""

    def test_detects_the_real_incident_shape(self):
        """E-0073 的真实形状：两个同名、不同源的顶层导入。"""
        source = (
            "from apps.business.order_status import Role\n"
            "from apps.rbac.models import Role\n"
        )
        self.assertEqual(
            shadowed_names(source),
            {"Role": {"apps.business.order_status.Role", "apps.rbac.models.Role"}},
        )

    def test_aliasing_is_the_fix(self):
        source = (
            "from apps.business.order_status import Role\n"
            "from apps.rbac.models import Role as RbacRole\n"
        )
        self.assertEqual(shadowed_names(source), {})

    def test_submodule_imports_are_not_a_conflict(self):
        source = "import urllib.error\nimport urllib.parse\nimport urllib.request\n"
        self.assertEqual(shadowed_names(source), {})

    def test_indented_and_parenthesised_imports_are_out_of_scope(self):
        source = (
            "def f():\n"
            "    from a import X\n"
            "    from b import X\n"
            "from c import (\n"
            "    X,\n"
            ")\n"
        )
        self.assertEqual(shadowed_names(source), {})

    def test_alias_forms_are_compared_by_alias(self):
        source = "from a import X as Y\nfrom b import Z as Y\n"
        self.assertEqual(shadowed_names(source), {"Y": {"a.X", "b.Z"}})


class NoShadowedImportsTests(TestCase):
    def test_no_module_level_name_is_bound_to_two_sources(self):
        scanned = 0
        found: list[str] = []
        for base in SCAN_DIRS:
            for path in sorted((REPO_SRC / base).rglob("*.py")):
                if ".venv" in path.parts:
                    continue
                scanned += 1
                for name, sources in shadowed_names(path.read_text(encoding="utf-8")).items():
                    found.append(f"{path.relative_to(REPO_SRC)}: `{name}` ← {sorted(sources)}")
        # ⛔ 没有这条，glob 写错时本判据会**恒真**（E-0065：判据自己也要能失败）。
        # 实测读数：扫描面 = 300 个文件（下界取一半，留出删文件的余量）。
        self.assertGreater(scanned, 150, f"扫描面只有 {scanned} 个文件 ⇒ 判据可能是恒真的")
        self.assertEqual(
            found, [],
            "同一个文件里同一个顶层名字被绑到多个来源（`git` 对此**不报冲突**，"
            "运行期后一行会静默覆盖前一行，见 E-0073）⇒ 给其中一个改名导入：\n  "
            + "\n  ".join(found),
        )

    def test_the_scanner_reads_real_imports(self):
        """反向自证：扫描器在真文件上确实读到了导入（不是「一个都没解析出来」）。

        自证打在还在的绑定上。`order_status.Role` 已删，照抄那条断言必红，不要加回来。
        `RbacRole` 仍断言；再加一个无关绑定，证明解析器读的是真导入。
        """
        bindings = module_level_bindings((REPO_SRC / "tests" / "test_submit_api.py").read_text(
            encoding="utf-8"
        ))
        self.assertEqual(bindings["RbacRole"], {"apps.rbac.models.Role"})
        self.assertEqual(bindings["seed_rbac"], {"apps.rbac.seed.seed_rbac"})
        # 单轴化的不变式：那个已删的角色词表**不许**被任何导入复活。
        self.assertNotIn("Role", bindings)
        # 反向自证「解析器不是什么都没读出来」：实测该文件 13 个模块级绑定。
        self.assertGreater(len(bindings), 8, f"只解析出 {len(bindings)} 个绑定 ⇒ 解析器可能坏了")


class _AstSanityTests(TestCase):
    """确保 `tests/` 下这些被扫的文件本身语法合法（解析器读的是源码文本，不是 AST）。"""

    def test_files_are_parseable(self):
        for base in SCAN_DIRS:
            for path in sorted((REPO_SRC / base).rglob("*.py")):
                if ".venv" in path.parts:
                    continue
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
