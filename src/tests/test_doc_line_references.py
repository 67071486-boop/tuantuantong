"""注释 / docstring 里的 `路径:行号` 引用 —— 结构检查。

## 这个检查为什么存在
注释里的「文件名加行号」引用在**写下的那一刻**是对的，之后任何一次改动都可能让它指向别处，
而普通门禁（`manage.py check` / 业务用例 / 覆盖率网）**读不到注释里的行号** ⇒ 腐化是静默的。
行号还落在文件长度里、那一行的内容已经换了人，第一层照样绿；内容对不对靠第二层锚点。

## 两层，与各自的证明力边界（**不要**把「绿」的结论放大）
### 第一层：自动、全量，只钉「没指飞」
扫 `src/apps/**/*.py` 与 `src/tests/**/*.py` 的**注释与 docstring** 文本，取形如
`文件名.py:行号` 的引用。实测存量里还有 `文件名.py:起-止` 与 `文件名.py:行号,行号` 两种写法，
所以三种都认——只认第一种会漏掉逗号后面那半截行号。
解析规则：把引用当**路径后缀**去匹配本仓（`src/` 下，排除 `.venv` / `__pycache__` /
`node_modules`）的 `.py` 文件（`apps/rbac/permissions.py` 命中 `src/apps/rbac/permissions.py`，
`src/tests/x.py` 命中 `src/tests/x.py`）；命中**不是恰好一个**（`views.py` / `models.py` /
`serializers.py` 这类仓内多处的）或**一个都没有**（`django/...` / `rest_framework/...` 这类前缀
在仓内不存在的——**带目录前缀的**引用不会命中仓内同名文件：`django/db/models/base.py` 不会
命中仓内那个 `base.py`）⇒ **跳过**，该条进不了第一层的断言。
⚠️ 上面那条只对**带目录前缀**的引用成立；**裸 basename 仍可能被静默解析**——实测探针：
只写 basename 的 `base.py` 被解析到仓内唯一的 `apps/business/api/base.py`（146 行），
于是框架那侧的行号会拿来量仓内的文件（假红 / 假绿都可能）。同类的仓内唯一 basename 还有
`authentication.py` / `exceptions.py`；两个有明确出处的同名短引用按出处映射原文件。
其他歧义仍按原规则跳过，新引用应写全路径。
对能解析的那部分：断言 `1 <= 行号 <= 该文件总行数`（起止两端都查）。
**第一层抓不到**「行还在、但那一行的内容已经不是注释说的那件事了」——它只回答「有没有指飞」。

### 第二层：声明式、小表，钉「内容还在」
`_DECLARED_ANCHORS` 手写 `(目标文件, 行号, 该行必须包含的片段)`，选的是**本仓自己代码里
承载语义、且被注释指名引用**的几处：`claim.py` 的来源态守卫与条件更新、`claim_order` 那次
`apply_transition` 调用、`order_status.py` 里 `updated_by` 被覆盖的那道 `if actor:` 守卫、
`performance.py` 写 `user` 的两个写入点、`refunds.py` 里退款承重链的三处（重跑判据 /
幂等冲减额判据 / 回池冲销的余额判据）。**行号与片段都是当场读码取的**，
不是抄注释里那个（抄了就等于用被检查的东西证明它自己）。
**第二层只覆盖声明过的这几处**——它不是「所有承载语义的引用」的枚举；表里加减条目要同时改
`len(_DECLARED_ANCHORS)` 那条断言（数目写死是**故意**的：表被悄悄缩小也是一种腐化）。
⚠️ 它还**不校验**「注释里写的数字 == 表里的锚点数字」——第二层只回答「我声明的那个目标行还在不在」，
两边**各写各的**。所以那 9 处锚定引用的注释数字若漂到**另一个仍在范围内的行**，这里照样绿
（例：把某条注释里的 `claim.py` 行号从 157 改成 158，锚点仍指 157 ⇒ 不红）；反过来，表里的锚点行
挪了位置、注释没跟着改，也是这里先红、由人去对注释。这类「数字联动」本检查**不接管**。

### 跳过清单是写死断言，不是静默跳过
`_EXPECTED_TOTAL_REFERENCES` 冻结语料大小，`_EXPECTED_SKIPPED_REFERENCES` 冻结
「跳过的引用原样路径 → 条数」；两者相减 = 被第一层真正检查的处数。
**新增或移除一条注释引用会让这条红**（避免扫了个寂寞）。按本网自己报出的差改常量，不要拿历史数字相加。

## 本检查**不**声称覆盖的（如实边界）
- 「注释说的是不是真话」仍然只能靠读码 + 反例（`E-0062`）——本检查不声称覆盖它，
  这里绿了**不**构成「注释都对」的依据。
- 第一层只钉范围；第二层只钉声明过的那几处片段；两者合起来**不**等于「引用都指向了
  注释想说的那一行」。
- 扫的是**注释与 docstring**（`tokenize` 的 `COMMENT` + `ast` 认出的 docstring）；
  普通字符串字面量与 `docs/**` 的 markdown 不在范围内。
- **裸行号简写整类不在扫描面内**：同一段里靠前面刚提到的文件名补全的那种写法
  （前面刚写过 `order_views.py`，后面就只写一个 `:557`），
  `_REFERENCE` 要求文件名与冒号**直接相接**才认 ⇒ 它**进不了语料**，第一层第二层都管不到。
  实测存量里的三个实例（都在 `notifications.py` / `performance.py` 的注释里）：
  - `notifications.py` 里那句「同族还有 order_views.py 的 transition docstring 与 `:557` 注释声称」
    ——这一处的 `:557` 是**裸行号**；
  - `performance.py` 里那句「修复前的栈」——这一处的 `:155`（指向 `order_status.py`）与
    `:96`（指向 `performance.py`）也都是**裸行号**。
  **为什么不管**：改写成「文件名加行号」会撑大冻结语料。这类腐化仍然靠读码，或手工加进第二层锚点。
  ⚠️ **本文件自己被扫，现在 0 条可匹配引用**——上面举这些实例时**故意**把文件名与冒号拆开写
  （写成两者直接相接就会进自己的语料、把冻结断言打红），不是漏写。
- 本文件自己的注释**不写**「路径加行号」形式：那会进自己的语料、扰动上面那几个冻结数字。
"""
import ast
import os
import re
import tokenize
from collections import Counter
import unittest
from pathlib import Path
from typing import NamedTuple

from django.test import SimpleTestCase

# ── 扫描面（本文件所在目录是 `src/tests/` ⇒ `parents[1]` 是 `src/`）────────────
_SRC = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SRC.parent
_SCAN_ROOTS = (_SRC / "apps", _SRC / "tests")
_EXCLUDED_DIRS = frozenset({".venv", "__pycache__", "node_modules"})

#: `文件名.py:行号` / `文件名.py:起-止` / `文件名.py:行号,行号`（都可带目录前缀）。
_REFERENCE = re.compile(
    r"(?P<path>[A-Za-z_][\w./\-]*\.py)"
    r":(?P<numbers>\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*)"
)

# 冻结语料大小。加或删一条「路径加行号」注释引用会让断言红；按本网自己报出的差改这个数，不要推算。
# 本文件自己的注释不许写成「路径 + 冒号 + 行号」（会进自己的语料）。
_EXPECTED_TOTAL_REFERENCES = 285
#: 跳过的引用（注释里写的**原样**路径 → 条数）。按路径而不只按 basename 写死：
#: basename 集合漏得掉「同名文件又添一条框架引用」这种变化。
_EXPECTED_SKIPPED_REFERENCES = (
    ("api/serializers.py", 1),
    ("apps/business/audit.py", 2),
    ("createsuperuser.py", 2),
    ("django/contrib/auth/__init__.py", 1),
    ("django/contrib/auth/base_user.py", 3),
    ("django/contrib/auth/management/__init__.py", 2),
    ("django/contrib/auth/middleware.py", 3),
    ("django/core/cache/backends/base.py", 2),
    ("django/core/handlers/base.py", 1),
    ("django/core/handlers/wsgi.py", 1),
    ("django/db/backends/postgresql/operations.py", 1),
    ("django/db/models/constraints.py", 1),
    ("django/db/models/fields/__init__.py", 1),
    ("django/db/models/fields/related_descriptors.py", 3),
    ("django/db/models/manager.py", 1),
    ("django/db/models/sql/compiler.py", 1),
    ("django/http/request.py", 2),
    ("django/middleware/common.py", 1),
    ("django/middleware/csrf.py", 13),
    ("django/test/client.py", 11),
    ("django/test/runner.py", 1),
    ("django/test/testcases.py", 1),
    ("django/test/utils.py", 1),
    ("django/utils/functional.py", 1),
    ("django/views/generic/base.py", 1),
    ("django_tenants/middleware/main.py", 1),
    ("django_tenants/models.py", 5),
    ("django_tenants/postgresql_backend/base.py", 1),
    ("django_tenants/test/cases.py", 7),
    ("django_tenants/utils.py", 3),
    ("fields.py", 1),
    ("global_settings.py", 4),
    ("main.py", 4),
    ("models.py", 1),
    ("related_descriptors.py", 1),
    ("request.py", 1),
    ("rest_framework/authentication.py", 5),
    ("rest_framework/exceptions.py", 1),
    ("rest_framework/fields.py", 8),
    ("rest_framework/generics.py", 3),
    ("rest_framework/mixins.py", 3),
    ("rest_framework/request.py", 1),
    ("rest_framework/routers.py", 1),
    ("rest_framework/serializers.py", 4),
    ("rest_framework/utils/encoders.py", 1),
    ("rest_framework/views.py", 15),
    ("rest_framework/viewsets.py", 1),
    ("routes/wechatmp.py", 1),
    ("selectors.py", 4),
    ("serializers.py", 1),
    ("urllib/request.py", 1),
    ("urls.py", 1),
    ("views.py", 5),
    ("xgj/client.py", 1),
)

#: 第二层：`(相对 `src/` 的目标文件, 行号, 该行必须包含的片段)`。行号当场读码取。
_DECLARED_ANCHORS = (
    # 来源态守卫：抢单到不了「内存状态不是待抢单」那条支路。
    ("apps/business/claim.py", 120, "if order.status != OrderStatus.WAITING_GRAB:"),
    # 条件更新：写非空 user 的是这一处（抢单原子性的来源）。
    ("apps/business/claim.py", 190, ".update(user_id=user_id, claimed_at=now, updated_at=now,"),
    # apply_transition 调用：抢单没传 actor。
    ("apps/business/claim.py", 214, "order, OrderStatus.IN_PROGRESS, nodes=nodes,"),
    # updated_by 只在 actor 非空时覆盖。
    ("apps/business/order_status.py", 372, "if actor:"),
    # set_taker 写非空 user。
    ("apps/business/performance.py", 78, "performance.user = user"),
    # reset_taker_performance 写 None。
    ("apps/business/performance.py", 195, "performance.user = None"),
    # 退款：重跑判据 / 幂等冲减额 / 回池冲销的余额判据。锚点是独立字面量，目标文件长了这里先红。
    (
        "apps/business/refunds.py",
        608,
        "is_rerun = str(from_status) == str(OrderStatus.REFUNDED)",
    ),
    (
        "apps/business/refunds.py",
        694,
        "if deduction == 0:",
    ),
    ("apps/business/refunds.py", 491, "if old_refund_fee != 0:"),
)


_HAS_INTERNAL_DOCS = (Path(__file__).resolve().parents[2] / "docs").is_dir()

class _Reference(NamedTuple):
    """一条注释引用：出处（相对 `src/`）、出处行号、原样文本、路径部分、行号部分。"""

    source: str
    lineno: int
    raw: str
    target: str
    numbers: tuple


def _python_files(root):
    """`root` 下的 `.py`（按排除目录过滤），顺序确定。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return sorted(found)


#: 解析用的仓内文件索引（模块导入时取一次快照；引用解析与语料计数共用同一份）。
_REPO_FILES = tuple(_python_files(_SRC))
_SCANNED_FILES = tuple(
    path for root in _SCAN_ROOTS for path in _python_files(root)
)


def _docstring_lines(source):
    """`ast` 认出的 docstring 覆盖的 1-based 行号（模块 / 类 / 函数 / 异步函数）。"""
    lines = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                start = first.value.lineno
                lines.update(range(start, (first.value.end_lineno or start) + 1))
    return lines


def _commented_lines(path, source):
    """该文件里属于**注释或 docstring** 的 1-based 行号。"""
    lines = _docstring_lines(source)
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.COMMENT:
                lines.add(token.start[0])
    return lines


def _parse_numbers(span):
    """`行号` / `起-止` / `行号,行号` ⇒ 逐个行号（起止两端都算进去）。"""
    numbers = []
    for part in span.split(","):
        part = part.strip()
        bounds = [piece for piece in re.split(r"\s*-\s*", part)]
        numbers.extend(int(piece) for piece in bounds)
    return tuple(numbers)


def _iter_references():
    """扫出所有注释/docstring 里的引用。"""
    for path in _SCANNED_FILES:
        source = path.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        for lineno in sorted(_commented_lines(path, source)):
            text = source_lines[lineno - 1]
            for match in _REFERENCE.finditer(text):
                yield _Reference(
                    source=path.relative_to(_SRC).as_posix(),
                    lineno=lineno,
                    raw=match.group(0),
                    target=match.group("path"),
                    numbers=_parse_numbers(match.group("numbers")),
                )


_CONTEXT_TARGETS = {
    ("tests/test_p5_credential_plane.py", "middleware.py"): "apps/core/middleware.py",
    ("apps/business/upstream_receipt_sync.py", "settings.py"): "config/settings.py",
}


def _resolve_reference(reference, source=None):
    """把引用当路径后缀解析成仓内文件；命中不是恰好一个就返回空/多个。"""
    normalized = reference.replace("\\", "/")
    contextual = _CONTEXT_TARGETS.get((source, normalized))
    if contextual:
        candidate = _SRC / contextual
        return (candidate,) if candidate in _REPO_FILES else ()
    hits = set()
    for candidate in _REPO_FILES:
        for base in (_SRC, _REPO_ROOT):
            relative = candidate.relative_to(base).as_posix()
            if relative == normalized or relative.endswith("/" + normalized):
                hits.add(candidate)
                break
    return tuple(sorted(hits))


def _line_count(path):
    return len(path.read_text(encoding="utf-8").splitlines())


@unittest.skipUnless(_HAS_INTERNAL_DOCS, "本仓库只开源网页前后端源码，不含内部文档 docs/")
class DocLineReferenceTests(SimpleTestCase):
    def test_layer1_reference_numbers_land_inside_the_target_file(self):
        """第一层：能解析的引用，行号都落在目标文件的行数范围内（起止两端都查）。"""
        problems = []
        for reference in _iter_references():
            targets = _resolve_reference(reference.target, reference.source)
            if len(targets) != 1:
                continue
            total = _line_count(targets[0])
            for number in reference.numbers:
                if not 1 <= number <= total:
                    problems.append(
                        f"{reference.source}:{reference.lineno} 的引用 "
                        f"{reference.raw!r} 指向 {targets[0].relative_to(_SRC)}"
                        f"（共 {total} 行），行号 {number} 越界"
                    )
        self.assertEqual(problems, [])

    def test_layer1_corpus_and_skip_list_are_frozen(self):
        """语料大小与「跳过清单」写死：新增 / 移除 / 改道一条引用都会红（**有意**）。

        两条断言各自能被**不同的**真实改动静默地弄红（不是从扫描结果反推的恒真判据）：
        ① 总数——注释里新添 / 删掉一条引用；
        ② 跳过清单——把一条**能解析**的引用改成仓内不存在的路径（总数不变，清单变化）。
        """
        references = list(_iter_references())
        skipped = Counter(
            reference.target
            for reference in references
            if len(_resolve_reference(reference.target, reference.source)) != 1
        )

        self.assertEqual(len(references), _EXPECTED_TOTAL_REFERENCES)
        self.assertEqual(dict(skipped), dict(_EXPECTED_SKIPPED_REFERENCES))

    def test_contextual_short_references_remain_checked(self):
        expected_targets = {
            ("tests/test_p5_credential_plane.py", "middleware.py"): "apps/core/middleware.py",
            ("apps/business/upstream_receipt_sync.py", "settings.py"): "config/settings.py",
        }
        self.assertEqual(_CONTEXT_TARGETS, expected_targets)
        references = [r for r in _iter_references() if (r.source, r.target) in _CONTEXT_TARGETS]
        self.assertEqual(Counter((r.source, r.target) for r in references), {
            ("tests/test_p5_credential_plane.py", "middleware.py"): 1,
            ("apps/business/upstream_receipt_sync.py", "settings.py"): 2,
        })
        for reference in references:
            target = _resolve_reference(reference.target, reference.source)
            self.assertEqual(target, (_SRC / expected_targets[(reference.source, reference.target)],))
            for number in reference.numbers:
                self.assertLessEqual(number, _line_count(target[0]))

    def test_layer2_declared_anchors_still_hold(self):
        """第二层：声明过的那几处，目标行现在仍含承诺的片段。"""
        self.assertEqual(len(_DECLARED_ANCHORS), 9)
        problems = []
        for target, lineno, fragment in _DECLARED_ANCHORS:
            path = _SRC / target
            lines = path.read_text(encoding="utf-8").splitlines()
            if not 1 <= lineno <= len(lines):
                problems.append(f"{target}:{lineno} 越界（该文件共 {len(lines)} 行）")
                continue
            if fragment not in lines[lineno - 1]:
                problems.append(
                    f"{target}:{lineno} 期望含 {fragment!r}，"
                    f"实际是 {lines[lineno - 1].strip()!r}"
                )
        self.assertEqual(problems, [])
