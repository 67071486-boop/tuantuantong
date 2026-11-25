"""DRF 视图的**方法名撞 HTTP 动词** —— 结构检查（错题见 `docs/ERRORS.md` 的 **E-0072**）。

## 这条检查守的是什么（**框架行为**，T8 实测出来的）

DRF 的 `ViewSetMixin.as_view` 把路由声明里的 `actions` 映射**逐条 `setattr` 到实例上**
（`rest_framework/viewsets.py` 的 `self.action_map = actions` 与
`for method, action in actions.items(): setattr(self, method, handler)`，两处分别在 `:111` 与 `:117`）；
而 `APIView.dispatch` 找处理器的方式是**按 HTTP 动词做 `getattr`**：
`request.method.lower() in self.http_method_names` 之后
`getattr(self, request.method.lower(), self.http_method_not_allowed)`（`rest_framework/views.py`（:517-519））
——前置那道判据用的 `http_method_names` 在 Django 的 `View` 上**默认包含 `delete`**
（`django/views/generic/base.py`（:43））。

两条合起来 ⇒ **把一个 ViewSet 方法命名为 `delete`**：
`@action(methods=["post"])` 只决定**注册哪条 `actions` 映射**（于是实例上多一个 `post` 属性），
**不会**删掉类上那个叫 `delete` 的方法；于是**每一条**该 ViewSet 路由上的 `DELETE` 请求
都会被 `getattr(self, "delete")` 命中 ⇒ **绕过 router 的 method_map 过滤**。

T8 **实测**的后果（修好之前）：`DELETE /api/accounts/<pk>/delete/` 与
`DELETE /api/accounts/status/` **都回 200**（破了本类「不暴露硬删」的 405 边界），
且那条路径上 `self.action` 是 `None`（`action_map` 里没有 `delete` 键）
⇒ `get_permissions()` 取不到节点 code、**只剩默认权限类**（T8 当时是 `IsAccountAdmin`；
线 D起是 `IsAccountManager` —— 「绕过 `system_user.change_status` 节点门」
这条结论不变，只是默认类今天宽一些）。
T8 的修法不是改名，而是「**方法第一条语句**挡非 POST」（必须是第一条：放到 `_lookup_id_error`
之后时 `DELETE /api/accounts/status/` 会先被短路成 1000 信封、看不到 405 —— 见 `accounts/views.py`
同方法注释里的实测记录）。

## 判据的**边界**（别把这里的绿读大）

- **禁令对象只有 ViewSet 家族**（MRO 含 `ViewSetMixin`）：只有那里，动词名的方法会被
  `dispatch` 的 `getattr` 直接命中。**普通 `APIView` 上动词名就是处理器名**
  （`SettleView.post` 这种），那是 DRF 的契约、不是缺陷 ⇒ 不在禁令内。
  第 4 条用例反向钉住「扫描确实看得见这些 APIView 处理器」，免得这次「分类」退化成
  「什么都没扫到」而全绿。
- 本检查是**静态**的：它证「ViewSet 家族上没有**未登记**的动词名方法」+「登记在册的那条
  **第一条语句**确实在挡非本动词」。它**不**证那道具守卫在运行期足够（比如畸形 pk 的行为）；
  运行期证据在 `tests/test_account_soft_delete.py` 与 `tests/test_account_api.py` 里。
- 扫描面 = `src/apps/**/*views*.py` ∪ `src/apps/**/api/*.py`。
  ⚠️ brief 写的是 `views*.py`，而实测 `apps/core/auth_views.py` **不匹配** `views*.py`
  （它以 `auth_` 开头）⇒ 四个普通 `APIView` 会整个漏出扫描面。这里放宽成 `*views*.py`：
  那是**放宽扫描面**（多扫文件），不是放宽判据。仍有边界：`src/apps/` 之外若定义 ViewSet
  不在这张网里（今天没有——当场实测见报告）。
"""
import ast
import importlib
import pathlib

from django.test import SimpleTestCase
from rest_framework.views import APIView
from rest_framework.viewsets import ViewSetMixin

#: `src/`（`src/tests/xxx.py` 的上一级）——扫描面与模块名的共同基准。
SRC_DIR = pathlib.Path(__file__).resolve().parent.parent

#: 扫描面（见模块 docstring 的「边界」）。
VIEW_FILE_GLOBS = ("apps/**/*views*.py", "apps/**/api/*.py")

#: `APIView.dispatch` 会拿去 `getattr(self, …)` 的方法名：Django `View.http_method_names`
#: 的默认集（**含 `delete`** —— 这条正是 T8 那个洞能成立的前提）。
HTTP_METHOD_NAMES = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

#: 「框架」根包名：MRO 上这些包里的类不算本项目代码（判据只看本项目写的类）。
_FRAMEWORK_ROOTS = ("rest_framework", "django")

#: 前置断言用的「必须被扫到」的模块（写死字面量：glob 写坏时它会让本网红，
#: 而不是静默变成「什么都没扫」）。列的是**五种不同形状**的定位：
#: accounts 是 ViewSet 家族、core 是普通 APIView、其余三个是 `api/` 下的两种。
REQUIRED_MODULES = (
    "apps/accounts/views.py",
    "apps/core/auth_views.py",
    "apps/audit/api/views.py",
    "apps/business/api/order_stats_views.py",
    "apps/rbac/views.py",
)

#: ⛔ **显式登记**：ViewSet 家族里名字撞 HTTP 动词的方法
#: （`(文件, 类名, 方法名, 该方法真正接受的那个 HTTP 动词)`）。
#:
#: 第四条不能省：`AccountViewSet.delete` 的 `@action(methods=["post"])` 声明它**只**接受 POST
#: ⇒ 守卫挡的是「非 POST」，而**不是**「非 DELETE」。守卫要挡的动词来自路由声明、不来自方法名
#: ——这条差别正是这个洞能成立的原因（方法名与注册动词是**两个**东西）。
#:
#: 目前有两条成员（两条都是「软删」动作）：
#:
#: - `AccountViewSet.delete`：`@action(methods=["post"], url_path="delete")`
#:   决定了它对外的路径与动作名（`ACCOUNT_PERMISSIONS` 的键就是 `delete`），改名会牵动路由与权限表
#:   ⇒ 采用「方法第一条语句挡非 POST」。
#: - `OrderViewSet.delete`（订单软删线交付，D-0144）：同一形状——
#:   `spec §5.1` 要求 `ORDER_PERMISSIONS["delete"]` 与路由 `orders/<单号>/delete/` 都逐字叫 `delete`，
#:   而 D5 明说「三道闸一起上」，其中一道就是「方法第一条语句挡非 POST」。
#:   ⚠️ 本条的登记不是可选项：不登记第 2 条用例会红（它判的是**等号**）。
#:
#: ⚠️ **新增任何一条都必须有意为之**：把一个 ViewSet 方法命名成 HTTP 动词后，若不同时
#: 改这里，第 2 条用例就红；而登记了却没写那道具守卫，第 3 条用例红。
SANCTIONED_VERB_NAMED_VIEWSET_METHODS = frozenset({
    ("apps/accounts/views.py", "AccountViewSet", "delete", "post"),
    ("apps/business/api/order_views.py", "OrderViewSet", "delete", "post"),
})


def _module_name(path: pathlib.Path) -> str:
    """`src/apps/accounts/views.py` → `apps.accounts.views`。"""
    return ".".join(path.relative_to(SRC_DIR).with_suffix("").parts)


def _scan_paths() -> list[pathlib.Path]:
    """扫描面里的文件（两个 glob 会重叠，用集合去重后排序）。"""
    found: set[pathlib.Path] = set()
    for pattern in VIEW_FILE_GLOBS:
        found.update(SRC_DIR.glob(pattern))
    return sorted(found)


def _project_verb_named_methods(cls) -> dict[str, type]:
    """`cls`（含**本项目写的**父类）上名字是 HTTP 动词的方法 → 属主类。

    ⚠️ 走 MRO 而不是只看 `cls.__dict__`：本项目的 Mixin 里写一个 `get` 同样会被
    `dispatch` 命中，只看自己的 `__dict__` 会漏掉它。框架类（`rest_framework` / `django`）
    一律跳过——`django.views.generic.View` 自己就有 `options`，那是框架的实现，不是我们的方法。
    """
    hits: dict[str, type] = {}
    for klass in cls.__mro__:
        if klass.__module__.split(".")[0] in _FRAMEWORK_ROOTS:
            continue
        for name in vars(klass):
            if name in HTTP_METHOD_NAMES:
                hits.setdefault(name, klass)
    return hits


def _constant_verbs(node) -> set[str]:
    """把比较式的另一侧读成动词集合：`"POST"` 或 `("POST",)` 这种字面量。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value.lower()}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = set()
        for element in node.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                return set()
            values.add(element.value.lower())
        return values
    return set()


def _method_node(rel_path: str, class_name: str, method_name: str):
    """取 `(文件, 类, 方法)` 的 AST 节点（类体内定义的，不跟继承）。"""
    tree = ast.parse((SRC_DIR / rel_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                return item
    return None


def _first_executable_statement(node):
    """方法体里第一条**可执行**语句（跳过开头的 docstring）。

    ⚠️ 文档串在 AST 里也是一个 `Expr(Constant(str))` 语句，所以 `body[0]` 常常是它而不是守卫。
    「守卫必须是第一条语句」的语义是「**任何别的代码之前**」——docstring 不是代码，跳过它是对的。
    只跳过**开头**那一处：第二条位置上的字符串表达式仍按普通语句对待。
    """
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body[0] if body else None


def _guards_the_http_method_first(node, verb: str) -> tuple[bool, str]:
    """`node`（一个方法）的**第一条可执行语句**是不是「挡非 `verb` 的请求」的守卫。

    接受的形状（与 `AccountViewSet.delete` 现状一致）：

        if request.method != "POST":          # 或 `not in ("POST",)`
            return self.http_method_not_allowed(request)

    判据分四步：① 第一条可执行语句是 `if`；② 读了 `request.method`；
    ③ 条件里有 `!=` / `not in` 与**本届动词**的字面量比较（`==` / `in` 不算——
    那是「命中才拒绝」的反向写法）；④ 成立那一支 `return` 或 `raise`。
    """
    if node is None:
        return False, "找不到方法定义"
    first = _first_executable_statement(node)
    if first is None:
        return False, "方法体是空的"
    if not isinstance(first, ast.If):
        return False, "第一条可执行语句不是 `if`（守卫必须放在任何别的代码之前）"
    uses_request_method = False
    rejects_other_methods = False
    for sub in ast.walk(first.test):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "method"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "request"
        ):
            uses_request_method = True
        if isinstance(sub, ast.Compare):
            for op, comparator in zip(sub.ops, sub.comparators):
                if isinstance(op, (ast.NotEq, ast.NotIn)) and _constant_verbs(comparator) == {verb}:
                    rejects_other_methods = True
    if not uses_request_method:
        return False, "守卫没有读 `request.method`"
    if not rejects_other_methods:
        return False, "守卫不是在挡「非 %s」的请求" % verb.upper()
    if not any(isinstance(sub, (ast.Return, ast.Raise)) for sub in first.body):
        return False, "守卫成立时既不 return 也不 raise"
    return True, ""


class DrfViewMethodNameTests(SimpleTestCase):
    """ViewSet 家族的方法名不许撞 HTTP 动词（`docs/ERRORS.md` 的 E-0072）。"""

    maxDiff = None

    def test_control_views_are_in_scan(self):
        self.assertIn('apps/settlement_control/views.py', self.modules)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        #: 相对路径 → 模块对象（`import_module` 走 Django 已就绪的 app registry）。
        cls.modules = {
            str(path.relative_to(SRC_DIR)): importlib.import_module(_module_name(path))
            for path in _scan_paths()
        }

    def _view_classes(self):
        """产出 `(相对路径, 项目里定义的视图类)`（只看模块里定义的那些，不含 import 进来的）。"""
        for rel_path, module in sorted(self.modules.items()):
            for obj in vars(module).values():
                if not isinstance(obj, type):
                    continue
                if obj.__module__ != module.__name__:
                    continue
                if issubclass(obj, APIView):
                    yield rel_path, obj

    def _classes_of(self, predicate):
        # ⚠️ 排序键不能是类对象本身（类之间没有 `<`）⇒ 用 `(路径, 类名)`。
        return sorted(
            ((rel_path, obj) for rel_path, obj in self._view_classes() if predicate(obj)),
            key=lambda pair: (pair[0], pair[1].__name__),
        )

    # ── 前置断言：这张网不许静默覆盖不到东西 ─────────────────────────────
    def test_scan_is_not_vacuous(self):
        """扫描面真的看得见本项目那几类视图（glob 写坏 / 目录搬家时在这里红）。"""
        scanned = sorted(self.modules)
        for required in REQUIRED_MODULES:
            self.assertIn(
                required, scanned,
                "扫描面里少了 %s（%r 一个都没扫到？）" % (required, scanned),
            )
        self.assertGreater(len(scanned), 15, "扫到的视图文件太少：%r" % (scanned,))
        viewsets = self._classes_of(lambda cls: issubclass(cls, ViewSetMixin))
        apiviews = self._classes_of(
            lambda cls: not issubclass(cls, ViewSetMixin)
        )
        self.assertGreater(len(viewsets), 5, "扫到的 ViewSet 家族类太少：%r" % (viewsets,))
        self.assertGreater(len(apiviews), 3, "扫到的普通 APIView 太少：%r" % (apiviews,))

    # ── 核心断言 1：ViewSet 家族里不许有未登记的动词名方法 ────────────────
    def test_viewset_methods_are_never_named_after_http_methods(self):
        """ViewSet 家族的方法名不得撞 HTTP 动词（登记在册的那一条除外）。

        判据是**等号**（不是「包含」）：新出现一条动词名方法 ⇒ 红，逼作者要么改名、
        要么把它登记进 `SANCTIONED_VERB_NAMED_VIEWSET_METHODS` **并**满足下面那条
        「第一条可执行语句挡非本动词」的守卫用例。
        """
        found = set()
        for rel_path, cls in self._classes_of(lambda cls: issubclass(cls, ViewSetMixin)):
            for name in _project_verb_named_methods(cls):
                found.add((rel_path, cls.__name__, name))
        sanctioned_names = {(rel, cls_name, name) for rel, cls_name, name, _verb
                            in SANCTIONED_VERB_NAMED_VIEWSET_METHODS}
        self.assertEqual(
            sorted(found),
            sorted(sanctioned_names),
            "ViewSet 家族里出现了（或消失了）名字撞 HTTP 动词的方法。"
            "命名成动词会让**每一条**该路由上的该动词请求都命中它、绕过 router 的 method_map 过滤"
            "（E-0072）：要么改名成 DRF 的入口名（list/create/retrieve/update/partial_update/destroy）"
            "或路由动作名，要么登记并加「第一条语句挡非本动词」的守卫。实测差集：%r"
            % (sorted(found - sanctioned_names),),
        )
        # 反向：登记项不许指认一个不存在的方法（否则这份名单会烂成空话）。
        for rel_path, class_name, method_name, _verb in sorted(SANCTIONED_VERB_NAMED_VIEWSET_METHODS):
            self.assertIn(rel_path, self.modules, "登记项的文件不在扫描面里：%s" % rel_path)
            owner = getattr(self.modules[rel_path], class_name, None)
            self.assertIsNotNone(owner, "登记项 %s 的类不存在" % class_name)
            self.assertIn(
                method_name, _project_verb_named_methods(owner),
                "登记项 %s.%s 已经不是一个「名字是 HTTP 动词的」方法了——请从名单里删掉"
                % (class_name, method_name),
            )

    # ── 核心断言 2：登记在册的那条必须自己挡住其它动词 ────────────────────
    def test_sanctioned_methods_guard_the_http_method_in_their_first_statement(self):
        """登记在册的动词名方法：**第一条可执行语句**必须是「挡非本动词」的守卫。

        这条是「登记」的另一半：只登记不设防等于把这个洞写进白名单。
        ⚠️ **必须是第一条可执行语句**（docstring 不算代码，可以留在它前面）：
        T8 实测，守卫挪到 `_lookup_id_error` 之后时 `DELETE /api/accounts/status/`
        会先被短路成 1000 信封（HTTP 200）而看不到 405。
        """
        problems = []
        for rel_path, class_name, method_name, allowed_verb in sorted(
            SANCTIONED_VERB_NAMED_VIEWSET_METHODS
        ):
            node = _method_node(rel_path, class_name, method_name)
            ok, why = _guards_the_http_method_first(node, allowed_verb)
            if not ok:
                problems.append((rel_path, class_name, method_name, allowed_verb, why))
        self.assertEqual(
            problems, [],
            "这些动词名方法没有在**第一条可执行语句**挡住其它 HTTP 动词（E-0072）：%r"
            % (problems,),
        )

    # ── 反向钉子：APIView 上的动词名处理器是 DRF 契约，且扫描确实看得见 ────
    def test_apiview_verb_named_handlers_are_seen_and_are_not_viewset_methods(self):
        """普通 `APIView` 的 `get`/`post` **就是**处理器名（DRF 契约），不在禁令内。

        这条是**反向对照**：若扫描面坏掉、或「ViewSet 家族」的判据把所有类都吞进去，
        第 2 条用例会**因为看不见东西而恒绿**。所以这里要求：
        ① 确实扫到了一批 APIView 的动词名处理器；② 它们与登记项不重叠。
        """
        handlers = set()
        for rel_path, cls in self._classes_of(lambda cls: not issubclass(cls, ViewSetMixin)):
            for name in _project_verb_named_methods(cls):
                handlers.add((rel_path, cls.__name__, name))
        self.assertGreater(
            len(handlers), 0,
            "一个 APIView 的动词名处理器都没扫到 ⇒ 判据或扫描面可能已经失效",
        )
        self.assertEqual(
            handlers & {(rel, cls_name, name) for rel, cls_name, name, _verb
                        in SANCTIONED_VERB_NAMED_VIEWSET_METHODS}, set(),
            "登记项落在了普通 APIView 上——那不需要守卫，登记表该改",
        )
