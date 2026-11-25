"""埋点覆盖率的**结构性兜底**（spec §10.3，评审 B5 重写）。**证明**：本项目**每一个写端点**都被分类过——要么代码里有一条**该 HTTP 方法真正会走到**的 `record(...)`（「做」），要么登记进「刻意不记」/「挂账」（`tests/test_operation_log_ledger.py`）；新增没分类的写端点 ⇒ **红**、删掉某处 `record(...)` ⇒ **红**、挂账功能的端点悄悄上线而仍挂着 ⇒ **红**。**不证明**「分类对了」：端点归到哪一类、埋了哪些字段，仍要人工评审（spec §10.3 末段）。
为什么**不照抄** `tests/test_no_route_500.py` 的 helper（spec §10.3 开头；三条腿在本线要抓的东西上有洞，评审逐条实测过 `test_no_route_500.py:43-46,74-75`）：① registry 腿**只枚举 collection 前缀** ⇒ 本线的动作端点（`orders/<no>/throw-to-hall/` 等）**整个逃出去**；② 显式路由腿**显式 `continue` 掉带路径转换器的路由** ⇒ `api/business/hall/<order_no:order_no>/claim/` **正是**这一形状、会被静默跳过（⚠️ 合并 `main` 后拼写从裸 `<str:order_no>` 换成共享 `order_no` 转换器，见 `test_operation_log_ledger.py` docstring 的「合并注」）；③ 它**只读 `apps/business` 一个 router**，全项目有**三个**（business / accounts / rbac）。故另起一套枚举并配前置断言，防「跳过规则」退化成「什么都没覆盖」。
枚举口径（逐条对应 spec §10.3）：① **递归遍历 `django.urls.get_resolver()`**，范围 = 三个 router + `apps/core/urls.py`（`ledger.COVERAGE_SCOPE` 声明），范围有两条**独立**复核（不靠 `_in_scope` 自证）：`test_enumerated_endpoints_come_from_declared_modules` 按**回调自己的 `__module__`** 查、`test_no_undeclared_urlconf_produces_endpoints` 按 **leaf 的父级 `include()` 模块**查——后者连「新挂一块 urlconf」都能抓到（实验 E 证明它能失败）；② router 生成的 callback 取 `callback.actions` **∩** `cls.http_method_names`——交点剔掉「mapping 里宣称了、但被 405 挡住」的方法（`orders/(?P<order_no>\\d{20})/$` 的 `delete`、订单详情的 `put` 都是这种，挂着会永久假红），同时归一化掉 DefaultRouter 的 `.format` 重复路由；③ **普通 APIView**（`/api/auth/*`、`/api/audit/*`）没有 `.actions`：判据是「**类上有没有** `post`/`put`/`patch`/`delete`」（`hasattr`，连继承算），**不是** `http_method_names`（后者在 `APIView` 上是 Django 默认全方法集，会把只读 `/api/auth/me` 误判成写端点）；④ **普通函数视图**（回调是函数、没有 `.cls`）今天覆盖范围内没有，但一旦出现就**一律当写端点**收进集合（`FUNCTION_VIEW_METHOD`）——否则整个逃出这张网（Task 12 变异实验 A 第一版实测漏掉了它）；⑤ 与登记表求差集并断言为空；「做」那一批**额外**要求**该 HTTP 方法真正会走到**的路径上有 `record(...)`（详见 `_method_writes_log` 的判据与边界）；⑥ 挂账集合与**端点全集**（含读方法）不相交（端点已出现而仍挂账 ⇒ 红）；⑦ **前置断言**：写端点集合非空、每个声明前缀都贡献端点、三个 router 的注册集合都在枚举范围内，且四个关键端点（含带转换器的 `claim` 与两个审计端点）都在覆盖范围内。"""
import ast
import inspect
import os
import re
import shutil
import sys

from django.test import SimpleTestCase
from django.urls import get_resolver

from tests.test_operation_log_ledger import (
    COVERAGE_SCOPE,
    DEFERRED_ACTIONS,
    DEFERRED_WITHOUT_ENDPOINT,
    DELIBERATELY_NOT_LOGGED,
    INSTRUMENTED,
    NOT_BUILT_ACTIONS,
    RESOLVED_ACTIONS,
)

#: 枚举只取这四个方法。`GET`/`HEAD`/`OPTIONS` 不是写方法（spec §5.7 尾注：一切 GET 不记）。
WRITE_METHODS = ("post", "put", "patch", "delete")

#: DefaultRouter 的 `.format` 后缀形状（`DefaultRouter` 默认带 format_suffix_patterns）。
#: 剥掉之后与同一条路由的常规形状归一 —— 否则「同一端点两种写法」会让语料翻倍。
_FORMAT_MARKER = "(?P<format>[a-z0-9]+)/?$"
_PARAM = re.compile(r"\(\?P<([a-z_]+)>[^)]*\)")

#: 必须出现在枚举集合里的关键端点（spec §10.3 第 5 点）。
#: 前两条是**带路径转换器**的写端点（先例的跳过规则会漏掉它们）；后两条是审计端点——
#: 它们在**读集合**里（写方法集合里当然没有），用来说明「只读端点没有混进写集合」。
MUST_BE_ENUMERATED = (
# ⚠️ 抢单这条的拼写是共享 `order_no` 转换器形状（`str(pattern)` = `<order_no:order_no>`），
# 仍是「带转换器」那一形状；这条前置断言的作用不变（见 `test_operation_log_ledger.py`
# docstring 的「路径写法」）。
    ("post", "api/business/hall/<order_no:order_no>/claim/"),
    ("post", "api/business/notifications/<int:notification_id>/read/"),
    ("post", "api/audit/events/"),
    ("get", "api/audit/logs/"),
    # ⭐ 交易订单操作记录线：本页的只读端点。它**只读**（不在写集合里），
    # 加进来是为了让「端点全集」这条前置断言真的覆盖到它——否则哪天它被改成写端点，
    # 下面 `MUST_HAVE_NO_WRITE_METHOD` 的反向钉子会先红，而不是静默掉出网。
    ("get", "api/audit/order-action-logs/"),
)

#: 只读端点：**这些路径上不许出现任何写方法**（反方向的钉子：防止「什么都算写端点」那种退化解，也防止普通 APIView 被 `http_method_names` 误判成写端点）。
#: ⚠️ 判据是「**有没有写方法键**」，不是「这个 GET 键在不在写集合里」——后者恒真（写集合按定义只含写方法），是无判别力的自指断言。
MUST_HAVE_NO_WRITE_METHOD = (
    "api/auth/me",
    "api/auth/csrf",
    "api/audit/actors/",
    "api/audit/logs/",
    # ⭐ 交易订单操作记录线：交易订单操作记录端点**只读 GET**
    # （spec D14「本线一个字节都不写业务表」）——这条反方向的钉子防它将来被改成写端点后
    # **静默掉出**这张覆盖面。
    "api/audit/order-action-logs/",
    "api/business/hall/",
    "api/business/my-orders/",
    # ⭐ booster-h5（spec §6.2）：打手受限订单详情——**只读 GET**（一行都不写：不写审计、不改 `updated_at`；载荷是 16 键受限字段集）⇒ 走这张只读表。
    # ⚠️ 登记它是**反向钉子**：这条路径上**不许**长出写方法键——将来谁把交单那种写动作挂到这条路径上，本表会先红，而不是让它静默掉出覆盖面。⚠️ 带路径转换器的路径按这张表自己的形状写（`_normalise` 保留转换器模板，与 `api/taker/apps/<int:pk>/deliveries/` 那条同款）。
    "api/business/my-orders/<order_no:order_no>/detail/",
    "api/business/performances/",
    "api/permission-nodes/",
    # ⭐ 开放平台 v2 的**只读扩展端点**（v2 设计文档 §6.1 / §13.10）：六条**全是 `GET`**、一个字节都不写业务表 ⇒ 进这张表而不是写端点那几张（写集合按定义只含写方法，把只读端点塞进去会让「哪些写端点没分类」失去意义）。`ranks/` 是**永远失败**的路径（恒回业务码 `1024`「尚未开放」），它同样不是写端点，按 §13.10-1 要求**显式**登记在这。
    # ⚠️ 带 `<order_no:order_no>` 的路径按这张表自己的形状写（`_normalise` 保留转换器模板）。
    # ⛔ `orders/<order_no:order_no>/status/` 与 `.../images/` **不在本表**：它们各自落了写方法（`POST` 改状态 / 认领上传；`images/` 的认领与只读列表**同住一个视图类**，路由先匹配先赢），不再只读——留着只会让本网红。写属性同次登记进了 `tests/test_operation_log_ledger.py`（`INSTRUMENTED` / `DELIBERATELY_NOT_LOGGED`），且本文件的`test_every_enumerated_write_endpoint_is_classified` 仍逼它们分类（不可能「既不在只读表、也不在写表」）；写面各自另有**能红**的判据（`tests/test_openapi_order_images.py` 等）。
    "api/open/v1/ranks/",
    "api/open/v1/bills/",
    "api/open/v1/games/",
    "api/open/v1/projects/",
    # ⭐ 打手侧对外端点的**拉大厅**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5端点表第 1 行）：它**只读 GET**（本模块一个字节都不写业务表：不写审计、不改 `updated_at`）⇒ 不进 `INSTRUMENTED`（写集合按定义只含写方法），走这张只读表。
    # ⚠️ 登记它是**反向钉子**：这条路径上**不许**长出写方法键——将来谁把抢单那种写动作挂到这条路径上，本表会先红，而不是让它静默掉出覆盖面。
    # ⚠️ 同批的**抢单**那条（`api/taker/v1/hall/<order_no:order_no>/claim/`）**不进本表**：它真的有写方法（`POST`），登记进这里会当场红；它进的是`tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`（动作码 `order.claim`）。
    "api/taker/v1/hall/",
    # ⭐ 打手侧对外端点的一条**只读**路径（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 3 行）：**我的单**（列表）——只读 GET（不写审计、不改 `updated_at`；唯一那行写库发生在认证层`TakerApp.last_used_at`，⛔ 不在这个视图里）⇒ 走这张只读表。
    # ⚠️ 登记它是**反向钉子**：这条路径上**不许**长出写方法键——将来谁把改状态那种写动作挂到这条路径上，本表会先红，而不是让它静默掉出覆盖面。
    # ⚠️ **同批另外几条各归各处**（⛔ 一处都不能漏、也⛔ 不能塞进本表）：· **进度图的 `GET …/images/`**（§5 端点表第 6 行的只读那一半）**进不了本表**：它与**认领**（`POST`）**同住一条路径**（Django 先匹配先赢 ⇒ 两个方法必须挂同一个视图类），而本表的判据是"这条路径上**一个**写方法键都没有" ⇒ 硬写进来会当场红。它那一半的写属性由 `DELIBERATELY_NOT_LOGGED` 里认领那一格接住（两处各自能红）。· **查单**（`…/orders/<order_no:order_no>/`）与**改状态**（`…/status/`）同理进不了本表（前者与 openapi 的 PATCH 那条路径**同名**、后者与 `POST` 同路径）。· **改状态** ⇒ `INSTRUMENTED`（动作码 `order.transition`，那一次 `record` 在领域口`order_writes.transition_order` 里）；**取票 / 认领** ⇒ `DELIBERATELY_NOT_LOGGED`（照上家侧那两条的先例）。
    "api/taker/v1/my/orders/",
    # ⭐ 打手侧**回调台账的读端点**（spec `docs/specs/2026-10-09-taker-callback-design.md` §7 的第二行）：**推送台账**（`GET /api/taker/apps/<int:pk>/deliveries/`）——只读 GET⇒ 走这张只读表（写集合按定义只含写方法，⛔ 不许塞进 `INSTRUMENTED`）。
    # ⚠️ 登记它是**反向钉子**：这条路径上**不许**长出写方法键——将来谁把"重投"那种写动作挂到这条路径上（而不是 `<delivery_pk>/retry/` 那条子路径），本表会先红，而不是让它静默掉出覆盖面。
    # ⚠️ **同批那条写端点不进本表**：`POST …/deliveries/<int:delivery_pk>/retry/`是一条**不同的路径**（多一层 `retry/`），它真的有写方法 ⇒ 登记进`tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`（动作码 `system.taker_api_delivery_retry`）。两处各自能红。
    "api/taker/apps/<int:pk>/deliveries/",
    # ⭐ 打手 App 开放注册：注册页的**匿名只读**游戏下拉
    # （`GET /api/app/auth/register/games/`）。它只读（不写审计、不改任何行），所以进这张表而不是 `INSTRUMENTED`。
    # ⚠️ 登记它是**反向钉子**：这条路径上**不许**长出写方法键——将来谁把「注册」那种写动作挂到 `games/` 这条子路径上（而不是 `register/` 本体），本表会先红，而不是让它静默掉出覆盖面。路径口径与本文件其余条目一致：`apps.accounts.urls`在 `api/` 下被 include，归一化后就是 `api/app/auth/register/games/`。
    "api/app/auth/register/games/",
)

#: 非写、非读的元方法：`OPTIONS` / `HEAD` 由框架兜底，不参与分类。
_META_METHODS = {"head", "options", "trace"}

#: 普通函数视图（`path("x/", some_view)`，回调是**函数**不是类）的写方法判据。这种视图没有 `.cls`、没有 `.actions`，DRF 的方法集也不适用，所以：**一律当作写端点收进集合**（最保守的一侧），方法名用一个醒目的哨兵。
#: 判据是「**新增写端点必须被分类**」——宁可让只读函数视图也登记一行，也不能漏掉写端点。今天全项目在 `COVERAGE_SCOPE` 内**没有**函数视图（全走 router / APIView），所以这条规则今天不产生任何登记负担；它守的是将来。
FUNCTION_VIEW_METHOD = "<plain-view>"

#: 三个 router 的模块（`ledger.COVERAGE_SCOPE` 里没有 `router` 属性的那两个不算）。
#: ⚠️ 开放平台对外 urlconf**没有 router**（两条显式 `path()`，D5 之下对外接口没有集合语义）⇒ 它进这一份；两处清单都是**恰好**断言，漏一处就红。
#: ⭐ 打手侧开放接口 urlconf 同属这一类（`urlpatterns` 全是显式 `path()`、一个 router 都没有 —— 对外接口没有集合语义、内部应用管理**刻意不用 router**，理由见 `apps/takerapi/urls.py` 的模块 docstring）。⚠️ 它与 `COVERAGE_SCOPE` 那一格**同一次**登记（先登记后建端点会让「声明了却没有 leaf」当场红，见那份登记表里的注释）。
NON_ROUTER_MODULES = (
    "apps.audit.urls",
    "apps.core.urls",
    "apps.openapi.urls",
    "apps.takerapi.urls",
    "apps.openapi.integration_urls",
    # ⭐ 实名认证线：用户自助那两个端点住在一份**独立的**`urlpatterns`（两条显式 `path()`，一个 router 都没有）。
    # ⚠️ 它与 `ledger.COVERAGE_SCOPE` 那一格**同一次**登记（两处都是**恰好**断言，漏一处就红）。
    "apps.realname.urls",
    # ⭐ 微信公众号集成线（D-0268）：API urlconf + **独立绑定确认页**
    # urlconf，两份都无 router（全是显式 `path()`）；与 `ledger.COVERAGE_SCOPE`
    # 同一次登记（两处都是恰好断言）。
    "apps.wechatmp.urls",
    "apps.wechatmp.page_urls",
    # ⭐ QQ 机器人线：设置页的 12 条显式 `path()`，无 router。
    # ⚠️ 与 `ledger.COVERAGE_SCOPE` 那一格**同一次**登记（两处都是**恰好**断言）。
    "apps.qqbot.urls",
    # ⭐ 企微自建应用线（D-0379）：设置页的 10 条显式 `path()`，无 router。
    # ⚠️ 与 `ledger.COVERAGE_SCOPE` 那一格**同一次**登记（两处都是**恰好**断言）。
    "apps.wecom.urls",
    # ⭐ 频道文章作品线：工作室频道的**公开读** urlconf（三条只读 GET、
    # 无 router）。⚠️ 与 `ledger.COVERAGE_SCOPE` 那一格**同一次**登记（两处都是**恰好**
    # 断言）；⛔ 它没有写端点，登记只是让「新挂一块 urlconf 却没人声明」的独立复核继续绿。
    "apps.business.channel_urls",
    "apps.internal_chat.urls",
)


def _normalise(pattern: str) -> str:
    """`str(URLPattern.pattern)` → 可读的归一化端点路径。
    做三件事：① 剥掉 DefaultRouter 的 `.format` 后缀（并补回尾斜杠，与常规形状归一）；
    ② 把 `(?P<名>…)` 写成 `<名>` 占位；③ 去掉正则锚点 `$`、regex 转义反斜杠与前缀里的 `^`。"""
    cut = pattern.find(_FORMAT_MARKER)
    if cut != -1:
        pattern = pattern[:cut].removesuffix(".").removesuffix("\\") + "/"
    path = _PARAM.sub(lambda m: "<%s>" % m.group(1), pattern)
    return path.replace("\\", "").replace("^", "").rstrip("$")


def _walk(resolver, prefix=""):
    """递归遍历 resolver，产出 `(原始 pattern 字符串, callback)`。"""
    for pattern in resolver.url_patterns:
        path = prefix + str(pattern.pattern)
        if hasattr(pattern, "url_patterns"):
            yield from _walk(pattern, path)
        else:
            yield path, pattern.callback


def _leaf_urlconf_modules(root_module="config.urls"):
    """产出**每条 leaf 端点**的 `(原始 pattern, callback, 产出它的 urlconf 模块名)`。
    「产出它的模块」= 这条 leaf 所在的那层 `include()` 的模块（`include("apps.business.urls")` ⇒ `apps.business.urls`）。这是 resolver 树的**结构事实**，不用路径前缀判据 ⇒ 可以独立复核 `COVERAGE_SCOPE` 是否写全（评审 Important 4：不能拿 `_in_scope` 反过来证明 `_in_scope`）。"""
    def walk(resolver, prefix, current):
        for pattern in resolver.url_patterns:
            path = prefix + str(pattern.pattern)
            if hasattr(pattern, "url_patterns"):
                inner = getattr(pattern, "urlconf_module", None)
                name = getattr(inner, "__name__", current)
                yield from walk(pattern, path, name)
            else:
                yield path, pattern.callback, current

    yield from walk(get_resolver(), "", root_module)


def _methods_available_to(callback) -> set[str]:
    """`callback` 上**真正可达**的 HTTP 方法（`actions` ∩ `http_method_names`，去掉元方法）。
    这条判据同时服务 `_declared_methods` 的 router 分支与订单 PUT 那条钉子，
    避免两处各写一遍后漂移。"""
    cls = getattr(callback, "cls", None)
    actions = getattr(callback, "actions", None)
    if cls is None or actions is None:
        return set()
    allowed = set(getattr(cls, "http_method_names", ())) or set(WRITE_METHODS + ("get",))
    return {m for m in actions if m in allowed and m not in _META_METHODS}


def _declared_methods(callback) -> set[str]:
    """这个 callback 上**真正可达**的 HTTP 方法（写 + 读，去掉元方法）。判据（spec §10.3 第 2、3 点）：
    - router 生成的 callback 带 `.actions`：见 `_methods_available_to`；- 普通 APIView 没有 `.actions` 但有 `.cls`：判据是「**类上有没有这个方法**」（`hasattr` 连继承来的处理器一起算），**不是** `http_method_names`。
    ⚠️ **不要**改用 `http_method_names`：它会把只读的 `/api/auth/me`、`/api/auth/csrf` 误判成写端点（spec §10.3 第 3 点）；- 普通函数视图（`getattr(callback, "cls", None) is None`）：见 `FUNCTION_VIEW_METHOD`。"""
    cls = getattr(callback, "cls", None)
    if cls is None:
        return {FUNCTION_VIEW_METHOD}
    if getattr(callback, "actions", None) is not None:
        return _methods_available_to(callback)
    return {
        m for m in WRITE_METHODS + ("get",)
        if hasattr(cls, m) and m not in _META_METHODS
    }


def _in_scope(path: str) -> bool:
    """声明式覆盖范围：只有 `ledger.COVERAGE_SCOPE` 列出的前缀算数。
    ⚠️ `test_enumerated_endpoints_come_from_declared_modules` 会**独立**复核
    （按回调自己的 `__module__`），因为「用这个谓词过滤 + 再用这个谓词断言」是自指。"""
    return any(path.startswith(prefix) for prefix, _ in COVERAGE_SCOPE)


def _endpoint_inventory() -> dict[tuple[str, str], object]:
    """归一化**写**端点 → callback（`(方法, 路径)` 为键）。
    ⚠️ 普通函数视图的键是 `(FUNCTION_VIEW_METHOD, 路径)`：它们没有方法集，但**必须**进集合（否则「新加一个函数视图写端点」会静默逃出这张网——Task 12 的变异实验 A 第一版正是这么漏的）。分类断言会逼它要么埋点、要么登记不记。"""
    inventory: dict[tuple[str, str], object] = {}
    for raw, callback in _walk(get_resolver()):
        path = _normalise(raw)
        if not _in_scope(path):
            continue
        for method in _declared_methods(callback):
            inventory[(method, path)] = callback
    return {
        key: cb for key, cb in inventory.items()
        if key[0] in WRITE_METHODS or key[0] == FUNCTION_VIEW_METHOD
    }


def _all_endpoints() -> set[tuple[str, str]]:
    """归一化端点**全集**（写 + 读）。挂账断言必须打在它上面：`user.rate_stat` 这条挂账项的端点落地时是**读**形状，只看写集合它**永远不会红**。
    ⚠️ `performance.export` / `order.export` 曾同属这一类「读形状挂账」，今天都已收口成**已埋点的读端点**（`GET` 导出端点上线并补了 `record(...)`，挂账表里已无它们）——这正是本条必须看**端点全集**的理由。"""
    endpoints = set()
    for raw, callback in _walk(get_resolver()):
        path = _normalise(raw)
        if not _in_scope(path):
            continue
        for method in _declared_methods(callback):
            endpoints.add((method, path))
    return endpoints


def _merged_inventory(extra_pattern, canonical_path: str) -> dict[tuple[str, str], object]:
    """把一条**合成** pattern 按给定归一化路径塞进当前写端点集合（只服务单元级断言）。"""
    merged = dict(_endpoint_inventory())
    for method in _declared_methods(extra_pattern.callback):
        merged[(method, canonical_path)] = extra_pattern.callback
    return merged


def _declared_actions() -> dict[tuple[str, str], str]:
    """登记表里「做」的全部 `(方法, 路径) -> action code`。"""
    declared: dict[tuple[str, str], str] = {}
    for module_endpoints in INSTRUMENTED.values():
        for key, action in module_endpoints.items():
            declared[key] = action
    return declared


#: **转发到 `record()` 的助手**（显式登记，不靠命名猜）。视图方法常常不直接调 `record`，而是调一条域内的统一埋点助手；这些助手的函数体里各自有一次 `record(` 调用。登记在这里 = 声明「这个名字背后有真埋点」，助手体里那行被删掉时断言照样红（`_node_writes_log` 会进助手再判一次）。
#: ⚠️ 新增一个埋点助手时把它加进来；**加进来但不真的调 `record()` 会被抓**。
#: ⚠️ 旧出口（`log_status_change` / `log_price_change` / `log_performance_adjust` /`log_performance_review`，写的是**旧** `AuditLog` 表）**故意不在**这里：有人把某个端点改回旧出口时，它就会从「有埋点」变成「没埋点」而红。旧出口（写**旧** `AuditLog` 表的那批）所在的 `apps/business/audit.py` 已整个删除；旧行形状留存在 `test_operation_log_migration.py`。旧出口名字**永远**不进`_RECORD_FORWARDERS` / `_CROSS_MODULE_FORWARDERS`：有人把某个端点改回旧出口时，它就会从「有埋点」变成「没埋点」而红。判据排除的是「**写旧表的出口**」这件事本身：将来若有人重新引入同类出口（不论叫什么名字），照样必须被排除、否则这张网会给它背书。
_RECORD_FORWARDERS = frozenset({
    "_link_progress",
    "_move_link_uid",
    "_submit_jidanpu_assignment", "_check_written_assignment", "_confirm_direct_assignment",
    "_resolve_pending", "_record_assignment_outcome",
    "_record_penalty_approval_logs",
    "record",              # `apps/audit/log.py` 的唯一写入出口
    # ⭐ 接单价写点共用专属留痕助手（领域服务与拆分端点调用它）；端点到核心的跨模块链仍由 `_CROSS_MODULE_FORWARDERS` 认证。
    "_record_taker_price_change",
    # ⭐⭐ peer 同步操作日志：接收端点的两跳。
    # `PeerInboundEventsView.post` → `receive_events`（同模块，peer_views）→`apply_events` / `apply_upstream_events`（跨模块，见 `_CROSS_MODULE_FORWARDERS`）→ `_record_peer_sync`（同模块，peer_inbound）→ `record(...)`。
    # ⚠️ 三跳都要在表里：`_node_writes_log` 只跟**登记过的名字**，少一跳这条链就断，端点会报「声称已埋点、代码里找不到埋点」。⛔ 不是放宽：`_record_peer_sync`里那次 `record(...)` 删掉，本网照样红。
    "receive_events",
    "_receive_assignment",
    "_finish_assignment",
    "_record_peer_sync",
    "_record_config",      # 配置域：`apps/business/api/config_views.py`
    "_record_saved",       # 配置域：同上
    "_record_account",     # 账号域：`apps/accounts/views.py`
    "_record_game_permissions",
    "_record_account_created",
    "_record_account_updated",
    # ⭐ 自助注册线：`apps/accounts/selfreg_views.py` 的注册域埋点助手。
    #   `SelfRegisterPublicView.post`（匿名端点）经它写 `user.create` +
    #   逐游戏 `user.game_permission_add`；actor 是 `invite.created_by`（理由见助手 docstring）。
    "_record_self_registered",
    "_record_role",        # 权限域：`apps/rbac/views.py`
    "_audit",
    "log_taker",
    # ⭐ 开放平台应用管理线：`apps/openapi/api/app_views.py` 的应用域埋点助手（五条写路径各自经它调 `record(...)`；形状照 `_record_role`）。⚠️ 名字取 `_record_app`、⛔ 不取裸 `_record`：后者太泛，会把别的模块里任何同名助手都算成"有埋点"（这张网按**名字**解析，名字越泛越容易假绿）。
    # ⭐ 企微群码锁定应用线（设计文档 §7.3 / D7）：`apps/openapi/api/wecom_qr_app_views.py` 里有一个**同名**的模块级直通函数（函数体只有一句 `return app_views_module.ApiAppAdminView._record_app(...)`，
    # ⛔ 不是第二份实现）。它**单独**不够：那张网跟到本模块这个函数之后就停了（它的函数体里既没有 `record(`、也没有表里认得的名字）⇒ 三条端点报「声称已埋点、代码里找不到埋点」。所以这个名字**同时**登记进下面的 `_CROSS_MODULE_FORWARDERS`（指向 `apps.openapi.api.app_views`），那条链才落到真正调 `record(...)` 的函数上。
    "_record_app",
    # ⭐ 客服后台打手钱包管理 / 押金金额：
    #   两个写端点各自的域内埋点助手（`apps/business/api/wallet_admin_views.py`）。
    #   ⚠️ 名字取具体动作、⛔ 不取裸 `_record`（与 `_record_app` 同一条理由：名字越泛越容易假绿）。
    "_record_adjust",
    "_record_rule_update",
    # ⭐ 交易平台抛单线：`apps/business/dailiantong_push.py` 的域内留痕助手
    #   （`push_order` 的成功 / 失败 / 未知三条出口都经它调 `record(...)`）。
    #   ⚠️ 名字取具体动作（⛔ 不取裸 `_record`，理由同上：名字越泛越容易假绿）。
    "_record_push",
    # ⭐ 交易平台抛单线：`push_order` 的三条出口（成功 / 失败 / 未知）
    #   都经 `_save_send_result` 写回链路行、再由它调 `_record_push` → `record(...)`。
    #   ⚠️ 这一跳也必须在表里：`_node_writes_log` 只跟**登记过的名字**，少一跳链就断。
    "_save_send_result",
    # ⭐ 代练通平台动作线：`apps/business/dailiantong_actions.py`
    #   的域内留痕助手（撤回 / 改价 / 补时 / 留言四个服务函数都直接调它）。
    #   ⚠️ 名字取具体动作（⛔ 不取裸 `_record`，理由同上：名字越泛越容易假绿）。
    "_record_action",
    # ⭐ 代练通「有、我们没用上」的动作线：`apps/business/dailiantong_extra_actions.py` 的**第二个**域内留痕助手（模板四条没有订单行、`target_kind` 与订单动作不同 ⇒ 单独一个助手）。
    # ⚠️ 它必须在这里登记：那张网按「裸名 + 当前模块」找助手，漏登记时三条模板端点会报「声称已埋点、代码里找不到埋点」。
    "_record_template_action",
    # ⭐ 大厅线（D-0296）：打手 API 镜像接单主体抽成模块函数，两条入口（`HallMirrorClaimView.post` 与老端点 `HallClaimView.post` 的镜像兜底）都经 `mirror_claim_response`（`apps/takerapi/api/hall_views.py`）走到`jidanpu_upstream.claim_upstream_mirror`（跨模块登记项在下面那张表，链不停）。
    # ⛔ 不是放宽：跟到领域层后仍走同一套判据——删掉 `promote_upstream_mirror`里那次 `record(...)`，本网照样红。
    "mirror_claim_response",
    # ⭐ `jidanpu_hall` 内部的转正链路（`claim_remote` → `_claim_upstream` → `_try_assign` → `promote_mirror` → `record`）。
    # ⚠️ `_try_assign` **必须在表里**：它是链上唯一直接调 `promote_mirror` 的那一跳，少了它这条链在 `_claim_upstream` 就断了（覆盖率网会报「声称已埋点却找不到`record(`」——那不是放宽登记的理由，而是这一跳漏登记的症状）。
    "_claim_upstream",
    "_try_assign",
    "promote_mirror",
    # ⭐ 上家鸡蛋铺线：**本线**接单链的最后一跳。
    # `jidanpu_upstream.claim_upstream_mirror` → `_claim_upstream` → `promote_upstream_mirror`→ `record(...)`（审计与「订单 + 绩效 + 链路 + 镜像行 promoted」在**同一个**`transaction.atomic()` 里，事务边界就在这个函数内 —— 把 `record` 搬到视图里会同时破坏这两条，所以与 P2 的 `promote_mirror` 同构地登记）。
    # ⚠️ `_claim_upstream` 这个名字在 `jidanpu_hall` 与 `jidanpu_upstream` 里**各有一份**实现：同模块登记项在**被走模块自己的** AST 里解析（见 `_node_writes_log`），所以这份登记不会把本线的链误解析到 P2 那份上。
    "promote_upstream_mirror",
    # ⭐ 自动发货线：`apps/business/xgj_ship.py` 的落库链。`retry_row`（跨模块登记见 `_CROSS_MODULE_FORWARDERS`）→ 两条支路
    # （`_record_failure` 或 `send_order_shipping`）→ `_write_attempt` → `_write_order_log`→ `record(...)`。**三跳都要在表里**：少一跳这条链就在半路断了（覆盖率网会报「声称已埋点却找不到 `record(`」——那不是放宽登记的理由，而是这一跳漏登记的症状）。
    "_record_failure",
    "_write_attempt",
    "_write_order_log",
    # 闲管家退款推送经 _route_refund 分流。改成已退单走 _refund_one；
    # 已结账保留原状态的分支走 _write_settled_log。
    "_route_refund",
    "_refund_one",
    "_write_transition_log",
    "_write_refund_log",
    "_write_settled_log",
    # ⭐ 微信公众号绑定确认页（D-0268）：`WechatMpBindConfirmView.post` →`self._post_bind` / `self._post_unbind`（**自方法委派不在 (a)(b)(c) 三类里** ——它们不是 DRF 入口名 ⇒ `_method_writes_log` 不跟）⇒ 四跳**都**要显式登记：
    # 两个委派名 + 两个落 `record(...)` 的模块级助手（埋点与绑定落库在**同一个**`atomic()` 里，事务边界在助手上）。少一跳，那条端点就报「声称已埋点、代码里找不到埋点」。
    "_post_bind",
    "_post_unbind",
    "_bind_openid",
    "_unbind_openid",
    # ⭐ 打手钱包 / 提现审核线：`WithdrawAdminViewSet` 的 5 个写动作（approve / reject / transfer / refresh / mark_paid）各自经类内助手`_record_withdraw_review` 写一行；`record(...)` 在那个助手的函数体里。
    # ⚠️ 名字带域前缀（⛔ 不取裸 `_log` / `_record`）：这张网按**名字**解析，名字越泛越容易把别处的同名方法算成「有埋点」而假绿。
    "_record_withdraw_review",
# ⭐⭐ D-0357：删单留痕的**最后一跳**——`recycle.soft_delete_order` 事务体内落`record(...)` 的是同模块 `_delete_single_order`（「删主单 = 整组同删」要求单笔直删与级联成员走**同一个单笔口径**）。⚠️ 本网只跟**登记过的名字**：两条删单端点
# （`POST api/business/orders/<order_no>/delete/` 与 `POST api/business/orders/batch/delete/`）的链少了这一跳就在 `recycle` 模块内断掉、报「声称已埋点、代码里找不到埋点」。
# ⛔ 不是放宽：它在**被走模块自己的** AST（`apps.business.recycle`）里解析——把 `_delete_single_order` 体内那次 `record(...)` 删掉，本网照样红。
    "_record_alipay_authorization",
    "_invoke_alipay_control",
    "_delete_single_order",
    "_run_order_batch_delete",
})

#: **跨模块**的埋点助手：名字 → 它**定义所在**的模块。
#: `_node_writes_log` 默认只在本模块里解析 `_RECORD_FORWARDERS` 里的名字。P4 的三个域（结账 / 退款 / 人工修正）把 `record(...)` 收在**领域服务**里（`settlement.py` /`refunds.py`），而端点住在**别的**模块（`api/settlement_views.py` /`api/refund_views.py`）——那是「六步逻辑收在一个函数、视图只做翻译」的既有结构，把 `record` 搬到视图里会同时破坏「与业务写入同事务」与模块边界。
#: ⚠️ **这不是把网放宽**：登记仍然是**显式且按名字**的，且解析到目标模块后仍走`_node_writes_log` 的同一套判据（直接 `record` / 已登记助手 / 深度上限）。「助手改名但没登记」照旧红——与同模块那条路的边界完全一致。⚠️ 名字一旦登记就**不再校验调用点所在模块**（同一名字在任何模块被调都认证）；这是刻意的：这些助手各只有一条定义、一个语义，多一处调用正说明它真的在写日志。
_CROSS_MODULE_FORWARDERS: dict[str, str] = {
    "check_link_binding": "apps.accounts.wxpusher_binding",
    "confirm_link_code": "apps.accounts.wxpusher_binding",
    "decide_approval": "apps.business.approvals",
    # ⭐⭐ peer 同步操作日志：接收端点的领域层两跳。`receive_events`（同模块 peer_views）→ 这两个函数（在 peer_inbound）→`_record_peer_sync`（同模块 peer_inbound）→ `record(...)`。
    # ⚠️ 为什么必须登记：本网的模块解析是「**裸名 + 当前模块**」，不登记就跟不进`apps/business/peer_inbound.py`（端点会报「声称已埋点、代码里找不到埋点」）。
    # ⛔ 不是放宽：解析到目标模块后仍走 `_node_writes_log` 的同一套判据 ——把 `_record_peer_sync` 里那次 `record(...)` 删掉，本网照样红。
    "log_taker": "apps.openapi.api.integration_takers",
    "delete_order_screenshot": "apps.business.api.screenshot_views",
    "split_local_order": "apps.business.api.order_views",
    "split_priced_order": "apps.business.api.order_views",
    "apply_events": "apps.business.peer_inbound",
    "assign_to_studio": "apps.business.peer_direct_taker",
    "assign_jidanpu_taker": "apps.business.jidanpu_direct_taker",
    "verify_jidanpu_assignment": "apps.business.jidanpu_direct_taker",
    "receive_assignment": "apps.business.peer_direct_taker",
    "apply_upstream_events": "apps.business.peer_inbound",
    # ⭐ 企微群码锁定应用线（设计文档 §7.3 / D7）：`wecom_qr_app_views` 的三条写路径 → 本模块的模块级 `_record_app`（直通）→`apps/openapi.api.app_views.ApiAppAdminView._record_app`（`record(...)` 在它的函数体里）。
    # ⚠️ **为什么必须登记**：那张网解析助手名时用的是「**裸名 + 本模块**」（`_module_functions(module_path)`），跟到本模块那个直通函数之后**不再跳到别的模块**⇒ 不登记时它报「登记表里声称已埋点、代码里找不到埋点」（实测：三条端点全红）。⛔ **不是放宽**：解析到目标模块后仍走 `_node_writes_log` 的**同一套判据** ——把 `app_views.ApiAppAdminView._record_app` 里那次 `record(...)` 删掉，本网照样红。
    # ⚠️ 代价（那张表自己的 docstring 写明的那一条）：名字一旦登记就**不再校验调用点所在模块**⇒ 本模块这一跳的"真的在写日志"由 `tests/test_openapi_wecom_qr_app.py` 的三条留痕断言承担（那一行的形状 + "⛔ 不含密钥"），那是**行为**判据、比结构判据强。⚠️ 三种写法的实测读数逐条记在 `WecomQrAppAdminView` 的类 docstring 里。
    "_record_app": "apps.openapi.api.app_views",
    # 结账六步：`SettleView.post` → `settle_performances`（`record` 在服务里）。
    "settle_performances": "apps.business.settlement",
    "create_batch": "apps.business.online_payroll",
    "cancel_batch": "apps.business.online_payroll",
    "_record_payroll_batch": "apps.business.online_payroll",
    # 部分退款：`refund_set_total`（`refunds.py` 的公开入口）→ `record` 在服务里。
    # ⚠️ 它**不再是 HTTP 端点的直接被调方**（`RefundView.refund` 改调领域核心 `order_writes.refund_total_write`），但仍是 `refunds.py` 的公开 API（`tests/test_refund_full.py` 等直接调它）⇒ 这条登记**照旧有效、不删**。
    "refund_set_total": "apps.business.refunds",
        # ⭐ `RefundView.refund` → `refund_total_write`（同一结构理由：写入与审计在同一个事务里，事务边界在 `refunds._write_refund_total` 内、由核心调它）。
        # ⛔ 不是放宽：把 `order_writes.refund_total_write` 调用链里的 `record(...)` 拿掉，本网照样红（解析到目标模块后仍走 `_node_writes_log` 的同一套判据）。
    "refund_total_write": "apps.business.order_writes",
        # ⭐ 第二跳：核心把写入委托给 `refunds._write_refund_total`，`record(...)`就住在它的函数体里（审计与四步写入同一个 `atomic()`）。
        # ⚠️ 登记成 `apps.business.refunds` 让解析器到**该模块里**找 `record` 调用——与 `refund_set_total` 那条同一个落点、同一份实现（它俩都走这个写入函数）。
    "_write_refund_total": "apps.business.refunds",
    # ⭐ 绩效页的撤销结账 / 删除记录。`SettlementRevokeView.post` → `revoke_settlements`（`record` 在服务里）；
    # `PerformanceDeleteView.post` → `delete_performances`（同）。与上面两条同一结构理由：审计必须与业务写入在**同一个事务**里，而事务边界在服务函数内。
    "revoke_settlements": "apps.business.performance_writes",
    "delete_performances": "apps.business.performance_writes",
    # ⭐ D-0144：订单软删 / 恢复。
    # `OrderViewSet.delete` → `soft_delete_order`（`record` 经服务体内抽出的`_delete_single_order` 落下，那一跳已登记进上面的 `_RECORD_FORWARDERS`，少那一跳两条删单端点会报「找不到埋点」）；
    # `OrderViewSet.restore` → `restore_order`（同）。与上面同一结构理由：审计行必须与「订单列 + 绩效列」的写入在**同一个**`transaction.atomic()` 里（spec §6 的纪律），事务边界在服务函数内。
    "soft_delete_order": "apps.business.recycle",
    "restore_order": "apps.business.recycle",
# ⭐ 镜像单接单（`jidanpu_hall` 侧）。`OrderViewSet.remote_claim` → `claim_remote`（`record` 在服务里，且与
# 「订单 + 绩效 + 链路 + 镜像行 promoted」**同一个事务**——事务边界在`promote_mirror` 内，把 `record` 搬到视图里会同时破坏这两条）。
    "claim_remote": "apps.business.jidanpu_hall",
    # ⭐ 上家鸡蛋铺线：**本线**的大厅镜像单接单。`JidanpuUpstreamHallClaimView.post` → `jidanpu_upstream.claim_upstream_mirror`（`record` 在领域层，与「订单 + 绩效 + 链路 + 镜像行 promoted」**同一个事务**，事务边界在 `promote_upstream_mirror` 内 —— 与 P2 那条 `claim_remote` 同一条理由）。
    # ⛔ 不是放宽：解析到 `apps.business.jidanpu_upstream` 后仍走 `_node_writes_log` 的同一套判据 —— 把 `promote_upstream_mirror` 里那次 `record(...)` 删掉，本网照样红。
    "claim_upstream_mirror": "apps.business.jidanpu_upstream",
    "record_successful_claim": "apps.business.claim_channel",
    "record_order_read": "apps.audit.log",
    # ⭐ 自动发货线（spec §3.7 的重试端点）：`TaobaoOrderSendLogViewSet.retry` → `xgj_ship.retry_row`（`record` 在领域模块里，与「发货记录行 + 订单日志行」**同一个** `atomic()`，事务边界在 `_write_attempt` 内—— 把 `record` 搬到视图里会同时破坏这两条，与 Task L / P4 同一条结构理由）。
    # ⛔ 不是放宽：解析到 `apps.business.xgj_ship` 后仍走 `_node_writes_log` 的同一套判据—— 把 `_write_order_log` 里那次 `record(...)` 删掉，本网照样红。
    "retry_row": "apps.business.xgj_ship",
    # ⭐ 订单批量线 A1（spec §D5）：改接单价 / 改收款状态。`OrderViewSet.price` → `change_price`（`record` 在领域核心 `order_writes` 里）；
    # `OrderViewSet.payment_status` → `change_payment_status`（同）。与上面几条同一结构理由：审计必须与业务写入在**同一个** `transaction.atomic()` 里，而 A1 把那两条动作体（连同事务边界）搬进了领域核心，好让单笔与批量共用一份判据。
    # ⛔ 不是放宽：解析到目标模块后仍走 `_node_writes_log` 的同一套判据——把`order_writes.change_price` 里的 `record(...)` 删掉，本网照样红。
    "change_price": "apps.business.order_writes",
    "change_payment_status": "apps.business.order_writes",
    # ⭐ 订单批量线 A2（spec §D5）：改发单价 / 订单字段更新。`OrderViewSet.hall_price` → `change_hall_price`（`record` 在领域核心里）；`OrderViewSet.partial_update` → `update_order_fields`（同）。与上面 A1 两条同一结构理由（审计与业务写入必须同一个 `transaction.atomic()`）。
    # ⛔ 不是放宽：解析到 `order_writes` 后仍走 `_node_writes_log` 的同一套判据。
    "change_hall_price": "apps.business.order_writes",
    "update_order_fields": "apps.business.order_writes",
    # ⭐ 开放平台 v2：**录单的领域核心**。
    # `OrderViewSet.create`（内部录单）与 `OrderCreateMixin.post`（对外录单）都调`order_intake.create_order`，而链上那两处 `record(...)`（`order.create` /`order.throw_to_hall`）**整段搬进了那个函数**（搬迁前它们长在视图方法体里）⇒ 这一跳必须登记，否则**两条录单端点**都会被判成「声称已埋点却找不到 `record(`」。
    # ⛔ 不是放宽：解析到 `apps.business.order_intake` 后仍走 `_node_writes_log` 的同一套判据 —— 把 `create_order` 里那两处 `record(...)` 删掉，本网照样红（两条端点各一条；读数见 T6 报告）。
    "create_order": "apps.business.order_intake",
    # ⭐ 订单批量线 A3（spec §D5）：改状态。`OrderViewSet.transition` → `transition_order`（`record` 在领域核心里）。与上面几条同一结构理由：审计必须与「状态改写 + 目标态钩子」在**同一个**`transaction.atomic()` 里，而 A3 把动作体（连同事务边界）搬进了领域核心。
    # ⛔ 不是放宽：解析到 `order_writes` 后仍走 `_node_writes_log` 的同一套判据——把 `order_writes.transition_order` 里的 `record(...)` 删掉，本网照样红。
    "transition_order": "apps.business.order_writes",
    # ⭐ 订单批量线 A4（spec §D5）：指派 / 改派接单人。`OrderViewSet.assign` → `assign_order`（`record` 在领域核心里）。与上面几条同一结构理由：审计必须与「状态改写 + 绩效列写入 + 同事务副作用」在**同一个** `transaction.atomic()` 里，而 A4 把动作体（连同事务边界）搬进了领域核心。
    # ⛔ 不是放宽：解析到 `order_writes` 后仍走 `_node_writes_log` 的同一套判据——把 `order_writes.assign_order` 里的 `record(...)` 删掉，本网照样红。
    "assign_order": "apps.business.order_writes",
    # ⭐ 订单批量线 A5（spec §D5）：软删订单。`OrderViewSet.delete` → `delete_order_write` → `recycle.soft_delete_order`（`record` 在 `recycle` 里，与「订单列 + 绩效列」同一个 `transaction.atomic()`）。
    # 本函数本体**不**直接调 `record`，但登记它是**显式且按名字**的跨模块指针（同 `refund_total_write` 那一类）：解析到 `order_writes` 后仍走`_node_writes_log` 的同一套判据（已登记的 `soft_delete_order` 继续负责那条链）。
    "delete_order_write": "apps.business.order_writes",
    # ⭐ 订单批量线 A10（spec §D15）：重算客服销售提成（**新能力**，无单笔对应物）。`OrderViewSet.batch_recompute_commission` → `recompute_customer_commission_write`（`record` 在领域核心里，与「补建 SALES 行 + 写金额」同一个 `transaction.atomic()`）。
    # ⛔ 不是放宽：解析到 `order_writes` 后仍走 `_node_writes_log` 的同一套判据——把 `order_writes.recompute_customer_commission_write` 里的 `record(...)` 删掉，本网照样红。
    "recompute_customer_commission_write": "apps.business.order_writes",
# ⭐ 换接单销售客服归属线：
# `OrderViewSet.sales_agent` → `order_writes.change_sales_agent`（`record(...)` 在领域核心里，与「`Order.sales_user` + `Order.sales_agent` + SALES 绩效行归属 + 金额重算」**同一个**`transaction.atomic()`——与 `assign_order` / `change_price` 那几条同一条结构理由：
# 留痕绝不能落在事务之外，否则会写出「日志说换了、钱没换」的行）。⛔ 不是放宽：解析到 `order_writes` 后仍走 `_node_writes_log` 的同一套判据——把 `order_writes.change_sales_agent` 里那次 `record(...)` 删掉，本网照样红。
    "change_sales_agent": "apps.business.order_writes",
    # ⭐ 下游鸡蛋铺线：「立即同步」端点的留痕。`JidanpuMemberSyncNowView.post` → `jidanpu_member.pull_statuses`→ `_pull_statuses` → `apply_remote_completion`（`record(...)` 在**最后那一跳**里，与「状态列 + 台账行」同一个 `transaction.atomic()`；端点只负责节流与信封）。
    # ⚠️ **链上每一跳都要登记**（少登记一跳，这条链就在那一跳断了 ⇒ 覆盖率网会报「声称已埋点却找不到 `record(`」）：`pull_statuses` 是端点直接调的那一跳、`_pull_statuses` 是慢路实做、`apply_remote_completion` 才是真正写日志的那一跳。落 `record(...)` 的最后一跳是 `apply_remote_completion`（②式回调 / 兜底对账 /慢路回拉**三条路共用**的那段「判 → 写」）。⚠️ 与 `test_order_read_path_net.py`的读点登记是**同一件事的两面**（那边按 AST 收读点、这边按登记表走可达性；两边都得改，否则一边绿一边红）。⛔ **不是放宽**：解析到 `apps.business.jidanpu_member`后仍走 `_node_writes_log` 的同一套判据——把 `apply_remote_completion` 里那次`record(...)` 删掉，本网当场红，且两条「立即同步」端点一起报出来。
    # ⚠️ ②式那一支够不到本网的名字解析面：它自己那条链由`tests/test_jidanpu_member_openapi_consume.py` 钉。⚠️ **本网的边界**：解析只按**名字**（`_called_names` 收集 `.attr`），⛔ 不校验调用点模块⇒ 本行登记**顺带**把上家线的 `jidanpu_upstream.pull_statuses` 也认证成已埋点（按名索引、装不下同名两模块）。那条链的调用点模块另有判据：`tests/test_jidanpu_upstream_api.py::test_the_endpoint_calls_the_slow_path_function_itself`（按 AST 钉）；它真正写日志那一下（`jidanpu_upstream._pull_row` 里的 `record(order.transition)`）**不是本网抓的**，抓它的是`tests/test_jidanpu_upstream_status.py` 的审计行用例（`OperationLog.DoesNotExist` ⇒ ERROR）。⇒ ⛔ 别把本网对那条链的绿读成「上家线的审计有保证」——它是**借**来的。
    "pull_statuses": "apps.business.jidanpu_member",
    "_pull_statuses": "apps.business.jidanpu_member",
    # ⭐ `record(...)` 随「**判 → 写**」整段住在 `apply_remote_completion`（②式回调 / ②式兜底对账 / 慢路回拉**三条路共用**那一段）⇒ 登记表这一跳指向新落点。
    # ⚠️ 少这一行的症状（实测）：两条「立即同步」端点同时报「该方法的代码路径上没有`record(...)` 调用」——`JidanpuMemberSyncNowView.post`（下家）与`JidanpuUpstreamSyncNowView.post`（上家，借下面那条 `pull_statuses` 登记**借**到认证，见上面对本网边界的说明）。
    # ⛔ **不是放宽**：解析到 `apps.business.jidanpu_member` 后仍走 `_node_writes_log`的同一套判据——把 `apply_remote_completion` 里那次 `record(...)` 删掉，本网照样红。
    "apply_remote_completion": "apps.business.jidanpu_member",
    # 公共入口持同步互斥锁，落账与审计在同模块的实际执行函数中。
    "_apply_remote_completion": "apps.business.jidanpu_member",
    # ⭐ 闲管家推送接收端（spec §7-9）：
    # `XgjOrderPushView.post` → `xgj_push.handle_order_push`（`record(...)` 在领域模块里，与「状态写入 + 两条订单日志」**同一个** `transaction.atomic()`——事务边界在`handle_order_push` 的逐张循环内，把 `record` 搬到视图里会同时破坏这两条，与 Task L / P4 / 自动发货线同一条结构理由）。
    # ⛔ 不是放宽：解析到 `apps.business.xgj_push` 后仍走 `_node_writes_log` 的同一套判据——把 `handle_order_push` 里那两次 `record(...)` 删掉，本网照样红。
    "handle_order_push": "apps.business.xgj_push",
    # ⭐ 上家绑定多行线（E-0255 / D-0256）：绑定改**多行**资源后，行级创建 / 编辑（`JidanpuUpstreamBindingCreateView.post` / `JidanpuUpstreamBindingDetailView.put`，住在 `upstream_binding_views.py`）与既有 member PUT（`jidanpu_upstream_views.py`）**共用**同一份「落库 + 留痕」实现 `_persist_binding_with_audit`（`record(...)` 就在它的 `transaction.atomic()` 里；⛔ 三处不抄第二份判据）。
    # ⚠️ 定义模块与两个调用模块**不同**（member 那条是同模块，解析目标相同、结果一致）⇒ 必须登记跨模块指针，否则三条端点全部报「声称已埋点、代码里找不到埋点」。
    # ⛔ 不是放宽：解析到 `apps.business.api.jidanpu_upstream_views` 后仍走`_node_writes_log` 的同一套判据——把 `_persist_binding_with_audit` 里那次`record(...)` 删掉，三条端点照样红。
    "_persist_binding_with_audit": "apps.business.api.jidanpu_upstream_views",
    # ⭐ 交易平台抛单线：`DailiantongPushView.post` → `push_order`（领域层 `apps.business.dailiantong_push`）→ `_record_push` → `record(...)`。
    # ⚠️ 前置闸 / 上游结果 / 落库 / 留痕都在服务层（视图只取订单 + 调服务），⇒ 不登记这一跳，端点会报「声称已埋点、代码里找不到埋点」。⛔ 不是放宽：解析到 `apps.business.dailiantong_push` 后仍走 `_node_writes_log`的同一套判据——把 `_record_push` 里那次 `record(...)` 删掉，本网照样红。
    "push_order": "apps.business.dailiantong_push",
    # ⭐ 代练通平台动作线：四个 `POST` 视图 → 各自的领域服务函数（`apps/business/dailiantong_actions`）→ `_record_action`（同模块，见`_RECORD_FORWARDERS`）→ `record(...)`。
    # ⚠️ 前置闸 / 上游调用 / 回读 / 留痕都在服务层（视图只取订单 + 调服务），⇒ 不登记这四跳，四个端点会报「声称已埋点、代码里找不到埋点」。
    # ⛔ 不是放宽：解析到 `apps.business.dailiantong_actions` 后仍走 `_node_writes_log`的同一套判据——把 `_record_action` 里那次 `record(...)` 删掉，本网照样红。
    "withdraw_push": "apps.business.dailiantong_actions",
    "modify_push_price": "apps.business.dailiantong_actions",
    "adjust_push_time": "apps.business.dailiantong_actions",
    "add_push_message": "apps.business.dailiantong_actions",
    # ⭐ 详情页补充动作：确认完单 / 重发 / 从所有平台撤回。`accept_push` 与 `repush_failed_push` 都经 `_record_action`（同模块）；
    # `withdraw_all_platforms` 逐平台转 `withdraw_push`（上一行），自己也会在「没有推送」那条闸上抛业务错误（不写审计）——三条都按名字登记进来。
    "accept_push": "apps.business.dailiantong_actions",
    "repush_failed_push": "apps.business.dailiantong_actions",
    "withdraw_all_platforms": "apps.business.dailiantong_actions",
    # ⭐ 第三方平台订单管理页：`PlatformPushActionView.post` →`_run_platform_push_action`（**同模块**的逐笔分派函数）→ 分支里直接`record(...)`（仲裁的本地标记），平台动作则转 `withdraw_push` / `accept_push` /`sync_push`（同在 `apps.business.dailiantong_actions`，见上面四跳）。
    # ⚠️ 为什么按**名字**登记跨模块：覆盖网只跟「登记过的名字」，且它不跟 `self._x()`这类 sibling 委派（只跟 DRF 入口名）⇒ 先把分派抽成模块级函数、再把名字登记进来。
    # ⛔ 不是放宽：解析到本模块后仍走 `_node_writes_log` 的同一套判据——把那次`record(...)` 删掉，本网照样红（该函数里的 `record` 是分支中的一支，判据按调用名）。
    "_run_platform_push_action": "apps.business.api.platform_push_action_views",
    # ⭐ 代练通「有、我们没用上」的动作线：十条 `POST`/`PATCH`/`DELETE`视图 → 各自的领域服务函数（`apps/business/dailiantong_extra_actions`）→`_record_action` / `_record_template_action`（同模块，见 `_RECORD_FORWARDERS`）→ `record(...)`。
    # ⚠️ 前置闸 / 上游调用 / 回读 / 留痕都在服务层（视图只做入参形状 + 翻信封）。
    # ⛔ 不是放宽：解析到 `apps.business.dailiantong_extra_actions` 后仍走`_node_writes_log` 的同一套判据——把两个助手里的 `record(...)` 删掉，本网照样红。
    "up_price_push": "apps.business.dailiantong_extra_actions",
    # ⭐ 详情页补充动作：补款 / 编辑平台单 / 追加进度截图。
    "add_push_amount": "apps.business.dailiantong_extra_actions",
    "edit_push_order": "apps.business.dailiantong_extra_actions",
    "add_push_screenshot": "apps.business.dailiantong_extra_actions",
    "request_push_arbitration": "apps.business.dailiantong_extra_actions",
    # ⭐ h13：同意撤销（`agree_cancel` → `_record_action` → `record(...)`）。
    "agree_cancel": "apps.business.dailiantong_extra_actions",
    "cancel_withdraw_request": "apps.business.dailiantong_extra_actions",
    "value_push_order": "apps.business.dailiantong_extra_actions",
    "off_sell_push_order": "apps.business.dailiantong_extra_actions",
    "lock_push_order": "apps.business.dailiantong_extra_actions",
    "blacklist_taker": "apps.business.dailiantong_extra_actions",
    "create_order_template": "apps.business.dailiantong_extra_actions",
    "update_order_template": "apps.business.dailiantong_extra_actions",
    "delete_order_template": "apps.business.dailiantong_extra_actions",
    "set_default_order_template": "apps.business.dailiantong_extra_actions",
}

#: HTTP 写方法 → DRF 的**入口方法名**（`DefaultRouter` 的 mapping；PATCH 落到
#: `partial_update`，`UpdateModelMixin.partial_update` 再 `return self.update(...)`）。
_DRF_ENTRY_POINT = {
    "post": "create",
    "put": "update",
    "patch": "partial_update",
    "delete": "destroy",
}

#: HTTP 写方法 → 框架 mixin 在**该入口内**调用的钩子名。`rest_framework/mixins.py`：`create`→`self.perform_create`（:19）、`update`→`self.perform_update`（:68）、`destroy`→`self.perform_destroy`（:91）。
#: ⚠️ 映射是**按方法**的：`perform_create` 里埋点**不能**证明 PUT/PATCH 写了行。
_DRF_METHOD_HOOK = {
    "post": "perform_create",
    "put": "perform_update",
    "patch": "perform_update",
    "delete": "perform_destroy",
}

_MODULE_FUNCTIONS_CACHE: dict[str, dict[str, ast.AST]] = {}


def _module_source(module_path: str) -> str:
    """取模块源码。
    ⚠️ 优先读 `__file__`，**不要**只靠 `inspect.getsource(module)`：后者对「临时文件里造出来的合成模块」会在文件删掉后失败（`PerMethodInstrumentationTests` 的合成模块就是这种形态）。实仓模块两条路都通，所以这个顺序对生产代码没有副作用。"""
    module = sys.modules[module_path]
    module_file = getattr(module, "__file__", None)
    if module_file and os.path.exists(module_file):
        with open(module_file, encoding="utf-8") as handle:
            return handle.read()
    return inspect.getsource(module)


def _module_functions(module_path: str) -> dict[str, ast.AST]:
    """模块里的 `FunctionDef` 节点，按**限定名**索引。
    键有两条：`"create"`（模块级函数 / 任意类的方法）与 `"ClassName.create"`（类内方法）。**不要**只按裸名索引 —— 同一个模块里两个类各有一个 `create`时，裸名会静默取到先出现的那个，把另一个类的端点判成「已埋点」。"""
    if module_path not in _MODULE_FUNCTIONS_CACHE:
        tree = ast.parse(_module_source(module_path))
        found: dict[str, ast.AST] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            found.setdefault(node.name, node)
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef) and node in parent.body:
                    found.setdefault("%s.%s" % (parent.name, node.name), node)
                    break
        _MODULE_FUNCTIONS_CACHE[module_path] = found
    return _MODULE_FUNCTIONS_CACHE[module_path]


def _lookup_node(module_path: str, cls, name: str):
    """取 `cls.<name>` 的 AST 节点；类为空时退回模块级函数。
    解析顺序：`"ClassName.name"` → `"name"`。前者是精确解析（同名方法不再串台），
    后者服务模块级助手（`_record_account_updated` 这类）与只有一个类时的简写。"""
    functions = _module_functions(module_path)
    if cls is not None:
        node = functions.get("%s.%s" % (cls.__name__, name))
        if node is not None:
            return node
    return functions.get(name)


def _called_names(node: ast.AST) -> set[str]:
    """这个函数体里**直接调用**的函数名（`record`、`self._record_role` → `_record_role`）。"""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        target = sub.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _node_writes_log(module_path: str, node: ast.AST, _seen=None) -> bool:
    """**AST 节点**最后会不会落到 `record(...)`（`record` 直接调用，或登记助手链）。
    边界（为什么它**不会**过度认证）：只跟登记过的名字——同模块的看`_RECORD_FORWARDERS`（在 `module_path` 自己的 AST 里解析），跨模块的看`_CROSS_MODULE_FORWARDERS`（在它声明的模块里解析，见那个常量的说明）。没登记的名字（包括旧 `AuditLog` 出口的 `log_*`）一律不跟 ——于是「助手改了名但没登记」会红，而不是静默通过。每条链上限 5 跳、带环检测。"""
    seen = set() if _seen is None else _seen
    if len(seen) > 5:
        return False
    names = _called_names(node)
    if "record" in names:
        return True
    functions = _module_functions(module_path)
    for name in sorted(names & (_RECORD_FORWARDERS | set(_CROSS_MODULE_FORWARDERS))):
        if name in seen:
            continue
        seen.add(name)
        # 跨模块登记项在**目标模块**里解析（同模块登记项仍在本模块解析）。
        target_module = _CROSS_MODULE_FORWARDERS.get(name, module_path)
        helper = _module_functions(target_module).get(name)
        if helper is not None and _node_writes_log(target_module, helper, _seen=seen):
            return True
    return False


def _method_delegations(node: ast.AST) -> set[tuple[str, str]]:
    """这个函数体里「把活交给别的方法」的 `(接收者, 名字)`。只认两种接收者，这是这条判据的**边界**：- `("self", 名)`：本类/父类的方法委派（DRF 的 `partial_update` 就是转 `update`）；
    - `("super", 名)`：显式父类委派（`EnvelopeModelViewSet.destroy` 转`DestroyModelMixin.destroy`；配置域的 `perform_create` 转 DRF mixin 的空实现）。
    其它对象上的同名方法（`serializer.save()`、`Foo.create()`）**不算** ——它们与「这个端点写不写日志」无关，认了就会把不写日志的端点认证成已埋点。"""
    names = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
            continue
        target = sub.func
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            names.add(("self", target.attr))
        elif isinstance(target.value, ast.Call):
            called = target.value.func
            if isinstance(called, ast.Name) and called.id == "super":
                names.add(("super", target.attr))
    return names


def _method_writes_log(cls, method: str, seen=None) -> bool:
    """**这个方法**（`method` 是 HTTP 动词）真正会走到的代码路径上有没有 `record(...)`。判据（按 HTTP 方法，不按「类里随便哪个方法」）：
    1. 取该 HTTP 方法的 DRF 入口方法（`post`→`create`、`patch`→`partial_update`…）；`APIView` 没有 `actions` 时入口就是 HTTP 方法名本身（`LoginView.post`）；2. 入口在**本项目代码里**（自写、或写在项目的 Mixin 里）⇒ 只看它自己的函数体：直接 `record(...)`、调登记助手、或把活交给别的方法（`self.update` /`self.perform_update` / `super().perform_update`）——委派目标跟着再判一次（带环检测，深度上限 `_MAX_DELEGATION_DEPTH`）；其中**按方法**的钩子（`_DRF_METHOD_HOOK[method]`，如 PATCH→`perform_update`）是 DRF 的契约：它被调就一定发生，所以它写日志即认证；3. 入口**继承自框架**（`rest_framework` / `django`，如 `CreateModelMixin.create`）⇒ 只有**映射到该方法的**那个钩子能认证它。`perform_create` **不能**证明 PUT/PATCH写了行 —— 这是评审点出的假阳性。
    ⚠️ 这条取代了早先「类里任意 `perform_*` 含 `record` 就认证该端点」的写法：后者会让「只覆写 `perform_create`、`perform_update` 留空」的 ViewSet 的PUT/PATCH 被认证成已埋点，而它们一行都不写（Task 12 评审 Important 2）。"""
    seen = set() if seen is None else seen
    if method in seen or len(seen) > _MAX_DELEGATION_DEPTH:
        return False
    seen.add(method)
    entry_name = _DRF_ENTRY_POINT.get(method, method)
    if _owner_of(cls, entry_name) is None:
        # 普通 `APIView`：没有 DRF 入口名，处理器就叫 HTTP 方法名本身（`LoginView.post`）。
        entry_name = method
    hook_name = _DRF_METHOD_HOOK.get(method)
    owner = _app_owner(cls, entry_name)
    if owner is None:
        # 入口**继承自框架**（如 `ModelViewSet.create` 来自 `CreateModelMixin`）
        # ⇒ 该端点只能由「映射到该方法的钩子」认证。
        return _hook_writes_log(cls, hook_name)

    node = _lookup_node(_module_of(owner), owner, entry_name)
    if node is None:
        return False
    if _node_writes_log(_module_of(owner), node, _seen=set()):
        return True
    # 委派：只认 `self.<名>` / `super().<名>`（见 `_method_delegations` 的边界）。两种合法委派，其余一律不跟（跟下去会把「不写日志的端点」认证成已埋点）：(a) 按方法的钩子（`perform_update`）—— DRF 契约下它必然被调用；
    # (b) 框架入口名（`super().destroy(...)` 落到 `DestroyModelMixin.destroy`，而后者会调用 `self.perform_destroy(...)`）—— 于是该方法的钩子生效；(c) 兄弟入口（`partial_update` → `self.update`）—— 换动词再判一次。
    for receiver, name in sorted(_method_delegations(node)):
        if (receiver, name) == ("self", entry_name):
            continue
        if name == hook_name:
            # (a) 直接调了「该方法的钩子」（`self.perform_update` / `super().perform_update`）
            if _hook_writes_log(cls, hook_name):
                return True
            continue
        if receiver == "super":
            # (b) 委派给**父类**：DRF 的 mixin 入口（`super().destroy(...)`）内部必然调用`self.perform_<方法>`；父类的同名入口（`self.update` 那种）则由 (c) 覆盖。
            # 判据是「父类这条路上有没有该方法的钩子」，**不去分辨**父类是本项目还是框架——`DestroyModelMixin.destroy` 就在框架里，`EnvelopeModelViewSet` 在本项目里，对「该方法的钩子会不会被调用」而言两者没有区别。
            if name in _DRF_ENTRY_POINT.values() and _hook_writes_log(cls, hook_name):
                return True
            continue
        if name not in _DRF_ENTRY_POINT.values() or _app_owner(cls, name) is None:
            continue
        # (c) 委派给**本项目**的兄弟入口（`partial_update` → `self.update`）：换动词再判一次
        target_method = _http_method_for_entry(cls, name) or name
        if _method_writes_log(cls, target_method, seen=set(seen)):
            return True
    return False


def _hook_writes_log(cls, hook_name) -> bool:
    """**该方法的**钩子在**本项目代码里**定义且真的写日志。
    ⚠️ 这是评审 Important 2 的核心边界：`perform_create` 里的 `record` **不能**证明 PUT/PATCH 写了行——调用方只会传「映射到当前 HTTP 方法」的那个钩子名。框架自带的空钩子（`CreateModelMixin.perform_create` 是 `pass`）不算本项目代码。"""
    if not hook_name:
        return False
    hook_owner = _app_owner(cls, hook_name)
    if hook_owner is None:
        return False
    hook_module = _module_of(hook_owner)
    hook_node = _lookup_node(hook_module, hook_owner, hook_name)
    return hook_node is not None and _node_writes_log(hook_module, hook_node, _seen=set())


def _module_of(klass) -> str:
    """类 → 它的模块路径（`None` 时给一个空串，调用方自己保证不为 None）。"""
    return "" if klass is None else klass.__module__


#: 委派链的最大跳数（`partial_update` → `update` → 别的助手）。超过就判「没认证」，
#: 宁可红也不要顺着无限链路猜。
_MAX_DELEGATION_DEPTH = 4

#: 「框架」的顶层包名：入口/钩子定义在这些包里的，不算本项目代码。
_FRAMEWORK_ROOTS = ("rest_framework", "django")


def _owner_of(cls, name: str):
    """在 MRO 上找**定义**了 `name` 的那个类（找不到返回 None）。"""
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return klass
    return None


def _app_owner(cls, name: str):
    """`name` 的属主类，**但只在本项目代码里**时返回它；来自框架则返回 None。
    判据是属主类自己的 `__module__`（不是方法对象的 `__module__`）：
    Mixin（`ConfigAuditMixin`）与本项目的 ViewSet 同在一个 app 模块里 ⇒ 算本项目代码。"""
    owner = _owner_of(cls, name)
    if owner is None:
        return None
    if owner.__module__.split(".")[0] in _FRAMEWORK_ROOTS:
        return None
    return owner


def _class_attr(cls, name):
    """在 MRO 上取方法对象。"""
    owner = _owner_of(cls, name)
    return None if owner is None else owner.__dict__[name]


def _http_method_for_entry(cls, entry_name: str):
    """反查：这个入口方法名对应哪个 HTTP 写方法（`partial_update` → `patch`）。
    `_DRF_ENTRY_POINT` 是「HTTP → 入口」；委派链上拿到的是入口名，需要反过来找动词，
    才能继续用「按方法」的判据。"""
    for method, name in _DRF_ENTRY_POINT.items():
        if name == entry_name:
            return method
    return None


def _certified_as_instrumented(callback, method: str) -> bool:
    """这个端点的**这个方法**被登记表认证前的代码判据（`True` = 真有埋点）。
    按 `callback.actions`（router 的 HTTP → action 映射）分流：
    - action 名就是 DRF 的入口名（`create`/`update`/`partial_update`/`destroy`）⇒ 走 `_method_writes_log`（按 HTTP 方法的路径判据，含框架钩子边界）；- 自定义 `@action`（`claim` / `throw_to_hall` / `parse` …）⇒ 没有框架入口/钩子那一套，直接看这个 action 的代码路径；- 普通 `APIView`（没有 `actions`）⇒ 入口就是 HTTP 方法名本身。"""
    cls = getattr(callback, "cls", None)
    if cls is None:
        # 普通函数视图：没有类可分析。⚠️ 一律判**未认证**——它会因此落进
        # `test_every_enumerated_write_endpoint_is_classified` 的差集，逼人显式处理
        # （要么给它埋点并登记，要么登记「刻意不记」）。
        return False
    actions = getattr(callback, "actions", None)
    if actions is not None and method in actions:
        entry = actions[method]
        if entry in _DRF_ENTRY_POINT.values():
            return _method_writes_log(cls, method)
        owner = _owner_of(cls, entry)
        if owner is None:
            return False
        return _function_writes_log(_class_attr(cls, entry), owner)
    return _method_writes_log(cls, method)


def _function_writes_log(func, owner) -> bool:
    """一个具体函数（自定义 action / 兄弟方法）真正会不会写日志。
    用 `(owner, func.__name__)` 定位 AST（同名方法不串台），然后：
    直接 `record(...)` / 登记助手 / 委派给同类的别的函数（跟着再判一次）。"""
    if func is None:
        return False
    node = _lookup_node(func.__module__, owner, func.__name__)
    if node is None:
        return False
    if _node_writes_log(func.__module__, node, _seen=set()):
        return True
    for _receiver, name in sorted(_method_delegations(node)):
        if name == func.__name__:
            continue
        target = _class_attr(owner, name)
        if target is not None and _function_writes_log(target, _owner_of(owner, name)):
            return True
    return False


class OperationLogCoverageTests(SimpleTestCase):
    """写端点的埋点覆盖率兜底（spec §10.3）。"""

    #: 失败时要把完整差集打出来（默认截断成 `[1960 chars]`，排障时看不到是哪些端点）。
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inventory = _endpoint_inventory()
        cls.all_endpoints = _all_endpoints()
        cls.instrumented = _declared_actions()

    # ── 前置断言：这张网不许静默覆盖不到东西 ─────────────────────────────
    def test_enumerated_write_endpoints_are_not_empty(self):
        # 没有前置断言时，「跳过规则写错」会退化成「什么都没覆盖」而全绿。
        self.assertGreater(
            len(self.inventory), 20,
            "枚举到的写端点只有 %d 条，覆盖范围可能被写窄了：%r"
            % (len(self.inventory), sorted(self.inventory)),
        )

    def test_required_endpoints_are_enumerated(self):
        """四个关键端点在覆盖范围内（spec §10.3 第 5 点的前置断言）。
        前两条是**写**端点（带路径转换器的 `claim` 是评审 B5 的死角）；后两条是审计端点，在**端点全集**里（`GET`，所以不在写集合里——`test_read_only_endpoints_have_no_write_method` 反向钉住这件事）。"""
        self.assertGreater(len(self.all_endpoints), 20, "端点全集是空的")
        missing = [key for key in MUST_BE_ENUMERATED if key not in self.all_endpoints]
        self.assertEqual(missing, [], "这些关键端点没被枚举到：%r" % (missing,))
        # 前两条必须落在**写**集合里（否则「写端点覆盖率」这条腿根本没覆盖到它们）
        for key in MUST_BE_ENUMERATED[:2]:
            self.assertIn(key, self.inventory, "关键写端点不在写集合里：%r" % (key,))

    def test_read_only_endpoints_have_no_write_method(self):
        """只读端点：**这些路径上不许出现任何写方法键**，而且它们必须真的存在。
        ⚠️ 判据是「这条路径有没有**写方法键**」（遍历写集合按路径过滤），**不是**「这个 GET 键在不在写集合里」——写集合按定义只含写方法，那样写是恒真的自指断言（评审 Important 1）。
        这条守的坑：用 `cls.http_method_names` 判普通 APIView 时，它在 `APIView` 上是Django 的**默认全方法集** ⇒ `POST/PUT/PATCH/DELETE /api/auth/me` 会进写集合，逼人把只读端点错误分类。"""
        write_methods_by_path = {}
        for method, path in self.inventory:
            write_methods_by_path.setdefault(path, set()).add(method)
        wrong = {
            path: sorted(write_methods_by_path[path] & set(WRITE_METHODS))
            for path in MUST_HAVE_NO_WRITE_METHOD
            if write_methods_by_path.get(path, set()) & set(WRITE_METHODS)
        }
        self.assertEqual(wrong, {}, "这些只读路径上出现了写方法键：%r" % (wrong,))
        # 反向：这些路径必须真的还存在（否则这份名单会烂成「指认已删除的路由」）
        for path in MUST_HAVE_NO_WRITE_METHOD:
            self.assertTrue(
                any(candidate == path for _method, candidate in self.all_endpoints),
                "只读端点 %r 已经从端点全集里消失了（改名/删除了？）" % path,
            )

    def test_enumerated_endpoints_come_from_declared_modules(self):
        """枚举集合里的每条端点，其**回调所在的模块**必须在 `COVERAGE_SCOPE` 里。
        ⚠️ 不能只写「用 `_in_scope` 过滤后再断言 `_in_scope`」——那是自指（评审 Important 4）。这里按回调自己的 `__module__` 独立复核，并且把声明的模块清单也钉死。"""
        declared_modules = sorted({module for _prefix, module in COVERAGE_SCOPE})
        self.assertEqual(
            declared_modules,
            [
                "apps.accounts.urls",
                "apps.audit.urls",
                # ⭐ 频道文章作品线：工作室频道的**公开读** urlconf（无 router，
                # 三条只读 GET）。⚠️ 又一次实测了上面那句"第二个必须同一次更新的地方"。
                # ⚠️ 本清单是 `sorted(...)` 的结果 ⇒ `channel_urls` 排在 `urls` **之后**。
                "apps.business.channel_urls",
                "apps.business.urls",
                "apps.core.urls",
                "apps.internal_chat.urls",
                "apps.openapi.integration_urls",
                # T4（开放平台对外查单端点）新增的第四个来源：无 router，两条只读显式 `path()`。
                # ⚠️ 这份清单是**第二个**必须与 `COVERAGE_SCOPE` 同一次更新的地方——
                # 只登记 `ledger.COVERAGE_SCOPE` 会让本用例红（实测：它报「声明了 6 个模块」）。
                "apps.openapi.urls",
                # ⭐ QQ 机器人线：设置页端点的 urlconf（无 router，
                # 12 条显式 `path()`）。同一句"第二个必须同一次更新的地方"的再一次实测。
                "apps.qqbot.urls",
                "apps.rbac.urls",
                # ⭐ 实名认证线：用户自助那两个端点的 urlconf（无 router）。
                # ⚠️ 又一次实测了下面那句"第二个必须同一次更新的地方"。
                "apps.realname.urls",
                # ⭐ 打手侧开放接口（无 router，一条取令牌显式 `path()`）。
                # ⚠️ 这就是上面那句"第二个必须同一次更新的地方"的**又一次实测**：只改 `ledger.COVERAGE_SCOPE` + 本文件的 `NON_ROUTER_MODULES` 仍然不够⇒ 本用例当场红（T2b 执行期的原始报文：`Lists differ: [… 'apps.openapi.urls','apps.rbac.urls', 'apps.takerapi.urls'] != [… 'apps.rbac.urls']`）。⛔ 这条断言一个字没放宽（清单仍是**恰好**集合），只是同一次补齐。
                "apps.takerapi.urls",
                # ⭐ 微信公众号集成线（D-0268）：API + 独立确认页两份 urlconf
                #    （无 router）。⚠️ 又一次实测了上面那句"第二个必须同一次更新的地方"。
                "apps.wechatmp.page_urls",
                "apps.wechatmp.urls",
                # ⭐ 企微自建应用线（D-0379）：设置页 urlconf（无 router，显式 `path()`）。
                "apps.wecom.urls",
            ],
            "声明的覆盖模块变了：声明覆盖 router + audit/core/openapi/takerapi/qqbot 的显式路由",
        )
        # 回调对象 → 它**实现**所在的模块（`callback.cls.__module__` / `callback.__module__`）
        walked = {}
        for raw, callback in _walk(get_resolver()):
            path = _normalise(raw)
            if not _in_scope(path):
                continue
            for method in _declared_methods(callback):
                walked[(method, path)] = callback
        actual_modules = set()
        for _method, callback in walked.items():
            cls = getattr(callback, "cls", None)
            actual_modules.add((cls or callback).__module__)
        allowed_roots = {module.rsplit(".", 1)[0] for module in declared_modules}
        undeclared = sorted(
            module for module in actual_modules
            if not any(module.startswith(root) for root in allowed_roots)
        )
        self.assertEqual(
            undeclared, [],
            "这些模块实现了枚举到的端点，但不在 COVERAGE_SCOPE 的声明范围里：%r" % (undeclared,),
        )

    def test_no_undeclared_urlconf_produces_endpoints(self):
        """**独立**复核覆盖范围：把 leaf 按「产出它的 urlconf 模块」归类。⚠️ 这条不是「用 `_in_scope` 过滤后再断言 `_in_scope`」（那是自指，评审 Important 4）。它从 resolver 树里取**结构事实**（每条 leaf 的父级 include 是哪个模块），然后要求产出这些 leaf 的模块集合**恰好**等于声明的模块 + 根 urlconf：
        - 每个声明模块都必须**真的**有 leaf（「声明了但没挂」会红）；- 任何**新挂载**的 urlconf 模块（哪怕当前没有任何写端点）都会出现在集合里而红——这正是「新增一块路由逃出这张网」的自动提醒。只读挂载（将来可能加的）也同样会被抓：本网声明覆盖的是**端点**，不是「已埋点的端点」。
        ⚠️ **`config.urls` 的容忍面被钉到字面 leaf `{"healthz"}` 上**（不是整个模块）：容忍整个 `config.urls` 会留下一个洞——一条**直接声明在 `config/urls.py` 里的写端点**既不进枚举（`_in_scope` 只认 `api/...` 前缀）、也不会被这条腿报警。今天根urlconf 只有一个 leaf（`healthz`），所以把容忍项枚举出来既精确又能失败。"""
        declared = {module for _prefix, module in COVERAGE_SCOPE}
        leaves = list(_leaf_urlconf_modules())
        produced = {module for _path, _callback, module in leaves}
        self.assertEqual(
            sorted(produced - declared),
            ["config.urls"],
            "这些 urlconf 模块产出了端点，但不在 COVERAGE_SCOPE 声明里（新挂载的路由？）：%r"
            % sorted(produced - declared),
        )
        # ⚠️ `config.urls` 被容忍，**只**因为它直接产出的 leaf 恰好是 `healthz`。
        # 把容忍面钉成字面集合：往 `config/urls.py` 里直接加一条 leaf（尤其是写端点）会红，
        # 逼作者登记进 `COVERAGE_SCOPE` 或在本网里显式裁定。
        tolerated = sorted(
            path for path, _callback, module in leaves if module == "config.urls"
        )
        self.assertEqual(
            tolerated, ["healthz"],
            "config.urls 直接产出的 leaf 变了：新增的端点若不登记进 COVERAGE_SCOPE，"
            "既不会进枚举、也不会被这条腿报警（这就是原先的 scope 洞）",
        )
        for module in sorted(declared):
            self.assertTrue(
                any(candidate == module for _path, _callback, candidate in leaves),
                "COVERAGE_SCOPE 声明了 %s，但它一个 leaf 端点都没有" % module,
            )

    def test_every_declared_scope_prefix_contributes_endpoints(self):
        """每个声明的前缀都至少贡献一个端点（声明的范围不能是空话）。
        ⚠️ 这条只保证「非空」：`api/` 同时挂 accounts 与 rbac 两个 router，任何 `api/` 端点都能满足它。**真正**钉住「三个 router 一个没漏」的是`test_three_routers_are_all_reachable` 的注册表反查。"""
        for prefix, module_path in COVERAGE_SCOPE:
            contributed = [key for key in self.all_endpoints if key[1].startswith(prefix)]
            self.assertTrue(
                contributed,
                "%s（前缀 %s）在端点全集里一个端点都没有" % (module_path, prefix),
            )

    def test_three_routers_are_all_reachable(self):
        """三个 router 各自的注册集合都能在枚举集合里找到（评审 B5 的第 3 个坑）。
        ⚠️ 判据是「**前缀出现过**」而不是「集合那一条在写集合里」：只读集合（`performances/`、`permission-nodes/`）合法地只有 `GET` ⇒ 不在写集合里，但它们必须仍然被枚举到（否则「跳过规则」把整个只读 router 吞掉也看不出来）。"""
        from importlib import import_module

        router_modules = [
            (prefix, module_path) for prefix, module_path in COVERAGE_SCOPE
            if module_path not in NON_ROUTER_MODULES
        ]
        self.assertEqual(
            sorted(module_path for _prefix, module_path in router_modules),
            ["apps.accounts.urls", "apps.business.urls", "apps.rbac.urls"],
            "router 模块清单变了：本线声明覆盖三个 router，新增/删除 router 时这里要一起改",
        )
        all_paths = {path for _method, path in self.all_endpoints}
        for prefix, module_path in router_modules:
            registered = getattr(import_module(module_path), "router").registry
            self.assertTrue(registered, "%s 的 router 注册表是空的" % module_path)
            for registered_prefix, _viewset, _basename in registered:
                collection = "%s%s/" % (prefix, registered_prefix)
                self.assertTrue(
                    any(path.startswith(collection) for path in all_paths),
                    "%s 注册的集合 %r 在端点全集里一条路由都没有" % (module_path, collection),
                )

    def test_orders_detail_put_is_blocked_by_http_method_names(self):
        """订单详情端点**没有** PUT（`http_method_names` 挡成 405）—— 枚举必须据此剔掉它。
        这条钉的是 spec §10.3 第 2(a) 点的同一个坑的另一面：`DefaultRouter` 的`actions` mapping 里写着 `put: update`，但 `OrderViewSet` 的`http_method_names` 不含 `put` ⇒ PUT 返回 **405**，不是写端点。若不剔掉，登记表就得多一条 `(put, orders/<no>/)` 的假条目。
        判据取「`actions` mapping ∩ `http_method_names`」（即枚举用的同一条判据），**不**发真实请求——这里钉的是**路由可达性**这个结构事实。实测旁证（Task 12 探针，跑完即删）：`PUT /api/business/orders/{20位}/` → 405，同路径 `PATCH` → 200，而 `PUT/DELETE /api/business/games/999/` → 200。"""
        key = ("put", "api/business/orders/<order_no>/")
        self.assertNotIn(key, self.inventory, "PUT 被枚举成了订单详情的写端点")
        callback = self.inventory[("patch", "api/business/orders/<order_no>/")]
        self.assertEqual(
            _methods_available_to(callback), {"get", "patch"},
            "OrderViewSet 详情路由的可达方法变了（PUT/DELETE 应当被 405 挡掉）",
        )
        # 反面对照：配置域的详情端点**允许** PUT（所以登记表里有它的 put 条目）。
        games = self.inventory[("put", "api/business/games/<pk>/")]
        self.assertIn("put", _methods_available_to(games))

    def test_plain_function_views_are_treated_as_write_endpoints(self):
        """普通函数视图没有方法集 ⇒ **一律当写端点**收进集合（防「函数视图写端点逃出网」）。
        Task 12 变异实验 A 的第一版就是往 `business/urls.py` 加了一个 `require_POST`的**函数**视图，而枚举只看 `.cls` ⇒ 那条新写端点静默溜过去了。这条用**合成** URLPattern 钉住新判据（不必真去改 `urls.py`）。"""
        from django.urls import re_path

        def temporary_view(request):  # pragma: no cover - 只为构造 pattern
            return None

        plain = re_path(r"^x/$", temporary_view)
        self.assertEqual(_declared_methods(plain), {FUNCTION_VIEW_METHOD})
        merged = _merged_inventory(plain, "api/business/orders/some-new-action/")
        self.assertIn(
            (FUNCTION_VIEW_METHOD, "api/business/orders/some-new-action/"), merged,
            "函数视图没有进写端点集合——新的函数视图写端点会逃出这张网",
        )
        # 分类断言同样要抓它：普通函数视图一律判「未认证」，因此必然落进差集
        self.assertFalse(_certified_as_instrumented(plain.callback, FUNCTION_VIEW_METHOD))
        self.assertTrue(
            [key for key in merged if key[0] == FUNCTION_VIEW_METHOD
             and key not in self.instrumented and key not in DELIBERATELY_NOT_LOGGED],
            "函数视图写端点没有被判成「未分类」",
        )

    # ── 核心断言：差集为空 + 「做」的代码路径必须真的 record ──────────────
    def test_every_enumerated_write_endpoint_is_classified(self):
        """每个写端点要么「做」（该方法的代码路径真的写日志），要么登记为刻意不记。"""
        unclassified = sorted(
            key for key in self.inventory
            if key not in self.instrumented
            and key not in DELIBERATELY_NOT_LOGGED
        )
        self.assertEqual(
            unclassified, [],
            "这些写端点没有分类：要么给它补 `record(...)` 并登记进 INSTRUMENTED，"
            "要么（确实不记时）登记进 DELIBERATELY_NOT_LOGGED：%r" % (unclassified,),
        )

    def test_wecom_group_qr_classification_is_load_bearing(self):
        from unittest.mock import patch

        key = ("post", "api/wecom/orders/<order_no:order_no>/group-qr/")
        self.assertIn(key, self.inventory)
        self.assertIn(key, DELIBERATELY_NOT_LOGGED)
        with patch.dict(DELIBERATELY_NOT_LOGGED):
            del DELIBERATELY_NOT_LOGGED[key]
            with self.assertRaisesRegex(AssertionError, "group-qr"):
                self.test_every_enumerated_write_endpoint_is_classified()

    def test_wecom_notification_settings_classification_is_load_bearing(self):
        from unittest.mock import patch

        key = ("put", "api/wecom/notification/settings/")
        self.assertIn(key, self.inventory)
        self.assertEqual(self.instrumented[key], "system.wecom_config_update")
        with patch.dict(self.instrumented):
            del self.instrumented[key]
            with self.assertRaisesRegex(AssertionError, "notification/settings"):
                self.test_every_enumerated_write_endpoint_is_classified()

    def test_instrumented_endpoints_really_call_record(self):
        """登记表只是指针：每个「做」端点必须**指向**该 HTTP 方法真正会走到的一次 `record(...)`。
        判据是 AST（真实调用），不是源码文本：方法体里写一句`# 这里应该 record(...)` 的注释不会让它假通过；而且是**按方法**的路径（`perform_create` 里的埋点不能认证 PUT/PATCH，见 `_method_writes_log`）。"""
        not_wired = []
        for key, action in sorted(self.instrumented.items()):
            callback = self.inventory.get(key)
            if callback is None:
                not_wired.append((key, action, "端点不在枚举集合里"))
                continue
            if not _certified_as_instrumented(callback, key[0]):
                not_wired.append((key, action, "该方法的代码路径上没有 record(...) 调用"))
        self.assertEqual(
            not_wired, [],
            "这些端点在登记表里声称已埋点，但代码里找不到埋点：%r" % (not_wired,),
        )

    def test_instrumented_action_codes_are_registered(self):
        """登记表里的 action code 必须都在 `ACTION_LOG_TYPES` 里（拼错就是漏埋点）。"""
        from apps.audit.actions import ACTION_LOG_TYPES, ACTION_TEMPLATES

        unknown = sorted(
            (key, action) for key, action in self.instrumented.items()
            if action not in ACTION_LOG_TYPES
        )
        self.assertEqual(unknown, [], "登记表里的 action code 没有登记：%r" % (unknown,))
        missing_template = sorted(
            (key, action) for key, action in self.instrumented.items()
            if action not in ACTION_TEMPLATES
        )
        self.assertEqual(
            missing_template, [],
            "登记表里的 action code 没有文案模板：%r" % (missing_template,),
        )

    def test_deliberately_not_logged_entries_exist_and_are_disjoint(self):
        """刻意不记的端点必须真实存在，且不得同时被登记为「做」。"""
        missing = sorted(
            key for key in DELIBERATELY_NOT_LOGGED if key not in self.inventory
        )
        self.assertEqual(
            missing, [],
            "这些端点在「刻意不记」表里，但枚举集合里找不到（端点改名/删除了？）：%r"
            % (missing,),
        )
        overlap = sorted(set(DELIBERATELY_NOT_LOGGED) & set(self.instrumented))
        self.assertEqual(
            overlap, [],
            "这些端点同时登记为「做」和「刻意不记」：%r" % (overlap,),
        )

    # ── 挂账：端点一旦出现（写**或读**）而仍挂账 ⇒ 红 ────────────────────
    def test_deferred_actions_have_no_endpoint_yet(self):
        """挂账动作的端点目前**不得存在**——判据打在**端点全集**（含读方法）上。⚠️ 只看写集合会漏掉读形状的挂账项：`user.rate_stat` 的端点落地时是**读**形状，写集合里永远看不到它。
        ⚠️ `performance.export` / `order.export` 也曾是这类「读形状挂账」，今天都已各自由`GET` 导出端点上线并埋点收口（挂账表里已无它们；本线的 `order.export` 收口见`test_operation_log_ledger.py` 里挂账表的订单导出线注释）（Task 12 评审 Important 3）。"""
        appeared = []
        for action, entry in sorted(DEFERRED_ACTIONS.items()):
            if entry.future_endpoint is None:
                continue
            hits = sorted(
                key for key in self.all_endpoints if key[1] == entry.future_endpoint
            )
            if hits:
                appeared.append((action, entry.why_pending, entry.future_endpoint, hits))
        self.assertEqual(
            appeared, [],
            "这些挂账动作的端点已经上线了，必须补埋点并从 DEFERRED_ACTIONS 移走：%r"
            % (appeared,),
        )

    def test_deferred_actions_are_explicitly_kinded(self):
        """挂账表每条必须显式声明「未来端点是什么形状」或「明确不会有端点」。
        ⚠️ 「不会有端点」是**枚举出来的豁免集合**（`DEFERRED_WITHOUT_ENDPOINT`），不是「把 `future_endpoint` 写成 `None` 就自动豁免」——后者是个文字后门：任何一条都能靠改写成 `None` 永久逃过提醒（评审 Important 3）。"""
        bad = []
        for action, entry in sorted(DEFERRED_ACTIONS.items()):
            if not entry.why_pending or not entry.why_pending.strip():
                bad.append((action, "理由为空"))
            if action in DEFERRED_WITHOUT_ENDPOINT:
                if entry.future_endpoint is not None:
                    bad.append((action, "在「不会有端点」集合里，却写了 future_endpoint"))
                if entry.future_endpoint_kind is not None:
                    bad.append((action, "在「不会有端点」集合里，却写了 kind"))
            else:
                if entry.future_endpoint is None:
                    bad.append((action, "没有 future_endpoint，又不在「不会有端点」集合里"))
                if entry.future_endpoint_kind not in ("read", "write"):
                    bad.append((action, "future_endpoint_kind 必须是 'read' 或 'write'"))
        self.assertEqual(bad, [], "挂账表登记不完整：%r" % (bad,))

    def test_deferred_without_endpoint_exemption_is_enumerated(self):
        """豁免集合本身要钉死：今天只有「5 号库」整体不存在这一条。"""
        self.assertEqual(
            sorted(DEFERRED_WITHOUT_ENDPOINT), ["library_5.all"],
            "「明确不会有端点」的豁免项变了；新增豁免必须是有意为之，并写清理由",
        )
        self.assertEqual(
            sorted(DEFERRED_WITHOUT_ENDPOINT - set(DEFERRED_ACTIONS)), [],
            "豁免集合里有不在挂账表里的动作",
        )

    # ── 挂账的**收口**：去向定死的动作不再叫「挂账」 ──
    def test_resolved_actions_are_explicitly_registered(self):
        """收口表（`RESOLVED_ACTIONS`）**显式枚举**，且与「挂账」「不做」两张动作表互斥。⚠️ 判据是**等号**（不是包含）：往收口表里加一条、或把某条**同时**留在`DEFERRED_ACTIONS` 里，都必须红——后者正是「收口只做了一半」的形状。这也是 `DEFERRED_WITHOUT_ENDPOINT` 那套「豁免必须显式枚举」的同一条规矩：**不许**用「去向说明里提到某个词」之类的文本后门来判收口。
        ⭐ `user.self_update`：自助改资料端点 `PATCH api/auth/profile`，去向 = 本表。"""
        self.assertEqual(
            sorted(RESOLVED_ACTIONS), ["user.self_update", "user.switches"],
            "收口表的成员变了；新增/移除一条必须有意为之，并同步更新这条字面量",
        )
        self.assertEqual(
            sorted(set(RESOLVED_ACTIONS) & set(DEFERRED_ACTIONS)), [],
            "这些动作**同时**躺在挂账表与收口表里：收口没做完（挂账表会继续让人以为「还没做」）",
        )
        self.assertEqual(
            sorted(set(RESOLVED_ACTIONS) & set(NOT_BUILT_ACTIONS)), [],
            "这些动作**同时**在收口表与「不做」表里：两者语义不同（收口 = 去向已定，"
            "其中有已上线的；不做 = 没有上线计划）",
        )
        empty = sorted(action for action, text in RESOLVED_ACTIONS.items() if not text.strip())
        self.assertEqual(empty, [], "这些收口条目没有写去向：%r" % (empty,))

    def test_resolved_actions_have_no_endpoint_of_their_own(self):
        """`user.switches` **这一档**收口动作没有自己的端点：它预测的那个不会出现，写入并进既有的动作。
        ⚠️ 「收口动作没有自己的端点」只对 `user.switches` **这一档**成立——`RESOLVED_ACTIONS["user.self_update"]` 有自己的端点（`PATCH api/auth/profile`，已埋点、登记在 `INSTRUMENTED`）。⛔ **方法名不改**（被 `.agents/notes/**` 与`docs/plans/**` 按名引用，改名会让引用悬空）；本函数体只断言 `user.switches` 那一档。两半判据都打在**实测枚举出来的集合**上（不是读文本）：
        ① 端点全集里**没有**任何 `switches/` 路径——`user.switches` 预测的`api/accounts/<pk>/switches/` 确实没出现（这是「收口」而不是「上线」的机械判据）；② 两个开关的写入走**既有的** `PATCH api/accounts/<pk>/`，且该端点的记账动作是`user.update`——这是 `RESOLVED_ACTIONS["user.switches"]` 那句「已落地、走 `user.update`」的机械判据；字段名在写入序列化器里也钉一次。"""
        # ⚠️ **前缀限定（系统开关线 D-0223）**：`api/business/system-switches/`也以 `switches/` 结尾，而它与本条判据的靶子（`user.switches` 预测的
        # `api/accounts/<pk>/switches/`）无关 ⇒ 判据限定在 `api/accounts/` 前缀下，对靶子的判别力一字不减（预测端点就在那个前缀下）。
        switches_paths = sorted(
            path
            for _method, path in self.all_endpoints
            if path.startswith("api/accounts/") and path.endswith("switches/")
        )
        self.assertEqual(
            switches_paths, [],
            "端点全集里出现了 `switches/` 路径：`user.switches` 预测的那个端点被造出来了，"
            "必须给它补埋点并重新分类（不能继续留在收口表里）",
        )
        self.assertIn(
            ("patch", "api/accounts/<pk>/"), self.inventory,
            "既有的账号编辑端点不在枚举集合里——下面那条判据会因此失去依据",
        )
        self.assertEqual(
            self.instrumented.get(("patch", "api/accounts/<pk>/")), "user.update",
            "账号编辑端点登记的记账动作不是 `user.update`：收口表里那句「走 user.update」需要重写",
        )
        # 两个开关的**字段名**必须真的可写（否则「已落地」那句话是假的）。
        from apps.accounts.serializers import AccountWriteSerializer

        fields = AccountWriteSerializer().fields
        missing = sorted(
            name for name in ("settle_forbidden", "grab_forbid_self_service")
            if name not in fields
        )
        self.assertEqual(
            missing, [],
            "这些开关字段不在账号写入序列化器上：收口表说它们「已落地」就不再成立",
        )

    # ── 证据：把枚举到的写端点打出来（scope 是真的）──────────────────────
    def test_print_enumerated_write_endpoints(self):
        """打印排序后的枚举集合（`-v 2` 下可见；这是「scope 是真的」的证据）。"""
        print("\nENUMERATED WRITE ENDPOINTS (%d):" % len(self.inventory))
        for method, path in sorted(self.inventory, key=lambda key: (key[1], key[0])):
            print("  %-6s %s" % (method.upper(), path))
        print("ENUMERATED ALL ENDPOINTS (read + write): %d" % len(self.all_endpoints))
        print("INSTRUMENTED: %d" % len(self.instrumented))
        print("DELIBERATELY_NOT_LOGGED: %d" % len(DELIBERATELY_NOT_LOGGED))
        print("DEFERRED_ACTIONS: %d" % len(DEFERRED_ACTIONS))
        print("ENUMERATED WRITE ENDPOINTS END")
        self.assertTrue(self.inventory)


class PerMethodInstrumentationTests(SimpleTestCase):
    """「按 HTTP 方法」的埋点判据（Task 12 评审 Important 2）。用一个**合成**的 ViewSet 模块钉住三件只有反例才能分辨的事：
    1. 只覆写 `perform_create` ⇒ **只有 POST** 被认证；PUT/PATCH 不能靠它蒙混（这正是评审点出的假阳性：旧判据「任意 `perform_*` 里有 `record`」会放行）；2. `partial_update` 转 `self.update`（DRF 的委派形状）要跟得下去；3. 完全没埋点的 ViewSet 一个方法都不能被认证。
    ⚠️ 合成源码必须落在**真文件**里：判据走 `inspect.getsource`，`exec` 出来的类没有源码。"""

    def _write_module(self, filename, source):
        """把一个合成模块写进**本用例自有的临时目录**并 import。⚠️ 三个必须：① 模块要有真文件（判据走 `inspect`/`__file__` 取源码）；
        ② 要登记进 `sys.modules`（类上的 `__module__` 是模块名，`_module_functions`按 `sys.modules[module_path]` 取源码）；③ 临时目录要活到**用例结束**（`addCleanup`），不能在构造完就 `TemporaryDirectory` 退出——那时源码文件已删。"""
        import importlib.util
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="task12_synthetic_views_")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        path = os.path.join(tmpdir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        name = "synthetic_views_for_%s" % os.path.splitext(filename)[0]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module

    def test_overrides_only_one_hook_leaves_other_methods_uncertified(self):
        """只覆写 `perform_create` ⇒ POST 认证、PUT/PATCH/DELETE **不**认证。"""
        source = '''
from rest_framework import viewsets, mixins
from apps.audit.log import record


class OnlyCreateInstrumented(viewsets.ModelViewSet):
    def perform_create(self, serializer):
        serializer.save()
        record(log_type=10, action="order.create", actor="tester")

    def perform_update(self, serializer):
        serializer.save()          # 故意**不**写日志

    def perform_destroy(self, instance):
        instance.delete()          # 故意**不**写日志


class NothingInstrumented(viewsets.ModelViewSet):
    pass


class DelegatesToUpdate(viewsets.ModelViewSet):
    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        record(log_type=10, action="order.update", actor="tester")
        return None
'''
        module = self._write_module("synthetic_views.py", source)

        only = module.OnlyCreateInstrumented
        self.assertTrue(_method_writes_log(only, "post"), "perform_create 有 record，POST 应认证")
        for method in ("put", "patch", "delete"):
            self.assertFalse(
                _method_writes_log(only, method),
                "%s 走了没埋点的钩子，不该被 perform_create 的 record 认证" % method,
            )
        for method in WRITE_METHODS:
            self.assertFalse(
                _method_writes_log(module.NothingInstrumented, method),
                "完全没埋点的 ViewSet 的 %s 不该被认证" % method,
            )
        for method in ("put", "patch"):
            self.assertTrue(
                _method_writes_log(module.DelegatesToUpdate, method),
                "%s 经 partial_update → self.update 的委派应当跟到 record" % method,
            )

    def test_public_apiview_post_directly_calling_record_is_certified(self):
        """普通 `APIView` 的 `post` 直接调 `record(...)` ⇒ 认证（入口名就是 HTTP 方法名）。
        ⚠️ 这条同时是**边界**的对照：`APIView` 没有 DRF 的 `perform_*` 钩子，
        所以它的认证只能来自「HTTP 方法方法体自己」的代码路径。"""
        source = '''
from rest_framework.views import APIView
from apps.audit.log import record


class PlainPostView(APIView):
    def post(self, request):
        record(log_type=10, action="order.create", actor="tester")
        return None

    def get(self, request):
        return None
'''
        module = self._write_module("synthetic_api_views.py", source)
        self.assertTrue(_method_writes_log(module.PlainPostView, "post"))
        # 只读方法不该被 `post` 里的 record 认证
        self.assertFalse(_method_writes_log(module.PlainPostView, "put"))
        self.assertFalse(_method_writes_log(module.PlainPostView, "patch"))
        self.assertFalse(_method_writes_log(module.PlainPostView, "delete"))

    def test_declared_helper_counts_but_unregistered_helper_does_not(self):
        """登记助手链能认证；**没登记**的助手不认证（否则「助手改名」会静默通过）。"""
        source = '''
from rest_framework import viewsets
from apps.audit.log import record


class RegisteredHelper(viewsets.ModelViewSet):
    def create(self, request, *args, **kwargs):
        return self._record_saved()

    def _record_saved(self):
        record(log_type=10, action="order.create", actor="tester")
        return None


class UnregisteredHelper(viewsets.ModelViewSet):
    def create(self, request, *args, **kwargs):
        return self._write_an_audit_row()

    def _write_an_audit_row(self):
        record(log_type=10, action="order.create", actor="tester")
        return None
'''
        module = self._write_module("synthetic_helpers.py", source)
        self.assertTrue(
            _method_writes_log(module.RegisteredHelper, "post"),
            "登记助手（`_record_saved`）应当被跟进",
        )
        self.assertFalse(
            _method_writes_log(module.UnregisteredHelper, "post"),
            "**没登记**的助手不能被当埋点——助手改名必须红，不能静默通过",
        )
