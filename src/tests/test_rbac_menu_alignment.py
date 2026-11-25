"""菜单 / 路由 / 岛页按钮 声明的权限点 ↔ 权限树（`apps/rbac/catalog.py`）结构对拍。

为什么放后端：只有 Python 能直接 import 权限树，同时又能读前端那些 TS 做对拍。
前端既有的 `route-declarations.spec.ts` 只查**形态**与菜单↔路由**互相一致**；
`routePermissions.spec.ts` 覆盖的是**路由侧** code 在不在目录里。此前**完全没被覆盖**的是
**岛页按钮级声明源**（源 3：按钮的 code 改名 ⇒ 非管理员按钮**静默**失效、没有测试报错）
与「声明条数没有任何绊线」这两块；本文件补的就是这两块。

本文件**只读**：不改 `catalog.py`，也不改任何前端文件。

## 三个声明源（都纯正则，不 import TS）

1. `config/menu.ts`：菜单条目的 `permissions: ['code']` 数组（`_PERMISSIONS_ARRAY_RE`）；
2. `config/routePermissions.ts`：它**没有** `permissions: [...]` 数组——code 写在
   `ROUTE_NODE_CODES` 记录的**值**里（`'route-name': 'code',`），单独解析那个块
   （`_ROUTE_TABLE_MARKER` + `_ROUTE_NODE_ENTRY_RE`）；
3. `layui-island/pages/*.ts` 里的**按钮级门禁**（`viewerCanNode(me, 'code')` /
   `AR_ACTION_NODES` 这类表）：扫全部岛页里的**带点**字符串字面量
   （`_DOTTED_CODE_RE`；`glob("*.ts")` 不含 `__tests__/`）。
   为什么是「带点」而不是「可带点」——**实测选出来的**：
   - **不剥注释**、按「可带点」的形态扫（`^[a-z_]+(\\.[a-z_0-9]+)?$`）：去重后 **227** 个字面量，
     其中 **224** 个不在权限树里（`'form'` / `'table'` / `'order_no'` 这类 layui 模块名与字段名）；
   - **同一个「可带点」形态、但按源 3 实际使用的协议（`_strip_ts_comments` 先剥注释）**：去重后
     **205** 个，不在树里的 **202** 个 ⇒「224」与「202」是**两种协议下的两个数**，不是一个数的两种说法；
     CSS 选择器（标签名加点类名，例如 span.left）会撞上带点正则，按显式标签前缀排除。
     那不是新增按钮门，也不是放宽判据。
   - ⛔ **第二种形状误报（审计动作码）**：**审计动作码**（`apps/audit/actions.py`
     的 action code，如 `order.view_credentials`）**也**是「带点」字面量，但**不是**权限点
     ⇒ 它落在权限树之外、会把判据 1 打红。实测（本线 T2 在 `order-orderview.ts` 里加了
     `VIEW_CREDENTIALS_ACTION = 'order.view_credentials'`）：源 3 的集合由 **22 变 23**
     （判据 2 报 `AssertionError: 23 != 22`），判据 1 报
     `layui-island/pages/*.ts → order.view_credentials`。
     **处置 = 把这个常量挪出岛页目录**（落到 `src/frontend/src/api/orders.ts` 的
     `VIEW_CREDENTIALS_ACTION`，岛页改成 import 它），⛔ **不是**把它加进上面那张排除表。
     **理由**：排除表要按「哪些带点字面量不是权限点」逐条枚举，而动作码会随功能增长，
     排除表只会越写越长、最终把源 3 的判别力磨掉；**挪位置不削弱它**——岛页里仍然只允许
     权限点 code 以带点字面量的形态出现。现场见 `docs/ERRORS.md` 的 E-0187。
   这个源只覆盖带点的功能点。栏目级无点码拿来当门禁，本扫描看不见。
   哪些栏目码没有任何声明源，在判据 5 里具名冻结。裸小写词和 `'form'` / `'table'`
   在文本上分不开，不能靠扫裸词把洞补上。

`ROUTE_NODE_CODES` 的 key↔路由一一对应由前端 `config/__tests__/routePermissions.spec.ts`
钉住（本文件不重复钉）。

## 五条判据（期望值全部字面值，E-0024）

1. `test_declared_codes_exist_in_tree`：三个源声明的每个 code 都在 `MODULES ∪ NODES` 里；
   失败消息逐条给出「哪个源 → 哪个 code」，下一个人才知道去哪改。
   **边际价值（如实说）**：今天 `menu ⊆ route`，菜单 / 路由侧的存在性其实**已被**前端
   `routePermissions.spec.ts` 覆盖；本判据真正承重的是**岛页按钮级声明**（源 3），
   以及「置灰 / 无真路由的菜单条目」那类前端比对会跳过的条目
   （今天菜单侧没有这种带 `permissions` 的条目——同样实测）。
2. `test_declared_code_count_is_pinned`：**三个**数量绊线——菜单侧 48、菜单∪路由 49、岛页 3。
3. `test_frontend_action_nodes_match_backend`：岛页 `AR_ACTION_NODES` 的 code 集合必须等于后端
   `apps/rbac/views.py::ACTION_NODES` 的**值**集合（这两边都是手写的，此前没有任何对拍）。
   ⚠️ **它只钉「值集合」，不钉「动作 → code」的对应**：把 `copy` 从 `_edit` 改指向 `_view`
   （值集合不变）本判据照样绿。那条对应由前端
   `layui-island/pages/__tests__/system-roleauthority.spec.ts` 的 `AR_ACTION_NODES` 字面量
   `toEqual({…})` 与后端 `tests/test_rbac_api.py::RoleAuthorityPermissionGateTests` 钉住；
   本判据不重复钉，也别把本判据当那条用。
4. `test_unreferenced_page_codes_are_frozen`：权限树里**没有被菜单 / 路由引用**的 `page` 点，
   具名清单 + 数量冻结。⚠️ 这个清单**不**看岛页按钮门：所以
   `system.role_authority_edit` / `_view` 虽然出现在清单里，它们在岛页里**是活的**按钮判据
   （覆盖它们的是判据 1 / 3，不是本判据）。
5. `test_uncovered_module_codes_are_frozen`：13 个栏目码里**没有任何声明源引用**的那几个，
   具名清单 + 数量冻结——登记「裸栏目码当门禁就抓不到」这个洞（见该方法与
   `KNOWN_UNCOVERED_MODULE_CODES` 的注释）。

⚠️ 已知边界：判据 1 只证明 code **拼写**对得上，**不证明**该点语义上就是那个页面 / 按钮，
也不证明后端真按它拦请求（那是接线线的事）。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.rbac.catalog import MODULES, NODES
from apps.rbac.choices import NodeKind
from apps.rbac.views import ACTION_NODES as BACKEND_ACTION_NODES

#: `src/tests/…` → `src/` → 前端源码目录。从 `__file__` 推，不写死绝对路径。
_FRONTEND_SRC_DIR = Path(__file__).resolve().parents[1] / "frontend" / "src"
_FRONTEND_CONFIG_DIR = _FRONTEND_SRC_DIR / "config"
_MENU_TS = _FRONTEND_CONFIG_DIR / "menu.ts"
_ROUTE_PERMISSIONS_TS = _FRONTEND_CONFIG_DIR / "routePermissions.ts"
_ISLAND_PAGES_DIR = _FRONTEND_SRC_DIR / "layui-island" / "pages"
_AR_ACTION_NODES_TS = _ISLAND_PAGES_DIR / "system-roleauthority.ts"

#: 失败消息里用的源标签（相对 `src/frontend/src`）。
_MENU_LABEL = "config/menu.ts"
_ROUTE_LABEL = "config/routePermissions.ts"
_ISLAND_LABEL = "layui-island/pages/*.ts"

#: `permissions: ['a', 'b']`。`[^\]]*` 允许数组跨行；code 里不出现 `]`。
#: ⚠️ 这条正则（与 `_ROUTE_NODE_ENTRY_RE`）**不剥注释**：块注释里写 `permissions: [...]`
#: 会被算进来（今天没有这种注释）。岛页那条扫**剥过注释**的文本，因为实测有
#: `"router.register"` 落在块注释里（见模块 docstring）。
_PERMISSIONS_ARRAY_RE = re.compile(r"permissions:\s*\[([^\]]*)\]")
_SINGLE_QUOTED_RE = re.compile(r"'([^']*)'")

#: `ROUTE_NODE_CODES` 表的起点与条目形态 `'route-name': 'code',`。
#: ⚠️ `_route_declared_codes` 用 `split(marker, 1)[1]`：marker 被删 ⇒ IndexError
#: （**响亮**，可接受）；若表的收尾不再是顶格 `}`，则块会一路扫到文件末尾——那种情况由
#: 判据 1 的 `assertTrue(route_codes)` 与判据 2 的 54 兜住。
_ROUTE_TABLE_MARKER = "export const ROUTE_NODE_CODES"
_ROUTE_NODE_ENTRY_RE = re.compile(r"^\s*'([a-z][a-z0-9-]*)':\s*'([^']+)',\s*$", re.M)

#: 岛页扫描用：带点的 code 形态（栏目级 code 无点，本扫描不覆盖——见模块 docstring）与字符串字面量。
_DOTTED_CODE_RE = re.compile(r"^[a-z_]+\.[a-z_0-9]+$")
_STRING_LITERAL_RE = re.compile(r"(['\"])((?:[^'\"\\]|\\.)*)\1")

#: 岛页 `AR_ACTION_NODES` 表（`add: 'code',` 形态）与判据 1b 的对拍对象。
_AR_ACTION_NODES_RE = re.compile(r"AR_ACTION_NODES\s*=\s*\{(.*?)\}\s*as const", re.S)
_AR_ACTION_ENTRY_RE = re.compile(r":\s*'([^']+)'")

#: 源 3 的**形状误报**排除表（实测 + 修复）。
#:
#: `span.left` 这种「HTML 标签名 + `.` + class 名」的 **CSS 选择器**（来自
#: `querySelectorAll('span.left')`）也匹配 `_DOTTED_CODE_RE`，但它**不是权限点 code**。
#: **为什么这条排除是安全的**：权限点 code 的模块前缀来自 `apps/rbac/catalog.py` 的
#: `MODULES`（`order.` / `system.` / `user.` …），**从来不是 HTML 标签名**；所以排除
#: 「以这些标签名开头、且后接 `.class`」的字面量，不会削弱判据 1 的本意（专抓 code 写错 /
#: 不在树里）。⛔ 这也**不是**「先查权限树、命中才算」——那会让判据 1 恒真；判据 1 仍然
#: 要求所有**未被本表排除**的带点字面量都在树里。
#:
#: ⚠️ **显式枚举，不是通配**（E-0065：不要用「理由文本里含某个词」当后门）：今天实测全仓
#: 岛页只有 **1** 个这种字面量（`order-orderview.ts` 的 `span.left`）。将来出现别的
#: `tag.class` 时，判据 2 的条数绊线会先响（3 → 4）——届时**实测确认**后，再往这个集合里
#: **显式**加一个标签名，别改成正则通配。
#:
#: ⭐ **交易订单操作记录线**：上面那句预测当场兑现——岛页新增了
#: `order-tradingorderactionloglist.ts` 的 `target.closest('a.orderdetails')`，判据 2 的条数
#: 绊线先响了（22 → 23，`test_declared_code_count_is_pinned` 报 `23 != 22`）。按本表的既定协议
#: **显式**加一个标签名 `"a"`（⛔ 不是把绊线改成 23——那等于把误报洗成真值；⛔ 也不是正则通配）。
#: 加 `a` **不削弱判据 1**：`a.orderdetails` 与先例 `span.left` **同形**（HTML 标签名 + `.class`
#: 的 CSS 选择器），而权限点 code 的模块前缀是 13 个栏目的 snake_case，**从来不可能是 HTML 标签名**。
#: ⚠️ 同批新写的 `a.viewPlatmTid` 本来就不在扫描面内（`_DOTTED_CODE_RE` 只认小写 ⇒ 大写 `P`/`T`
#: 不匹配），所以本次只多出 1 个命中；⛔ 判据 1 的语义一个字没改。
_HTML_TAG_DOT_CLASS_PREFIXES: frozenset[str] = frozenset({"span", "a"})


def _tree_codes() -> set[str]:
    """权限树的全部 code：13 个栏目（`MODULES`）+ 151 个功能点（`NODES`）。

    ⭐ T4：150 → **151**（本仓第一条**自有**节点
    `system.allow_taker_open_api`——它同样进这棵树，见 `test_rbac_catalog.py`）。
    """
    return {m[0] for m in MODULES} | {n[0] for n in NODES}


def _menu_declared_codes() -> set[str]:
    """`menu.ts` 的 `permissions: [...]` 数组里声明的 code（去重）。"""
    text = _MENU_TS.read_text(encoding="utf-8")
    codes: set[str] = set()
    for array in _PERMISSIONS_ARRAY_RE.findall(text):
        codes.update(_SINGLE_QUOTED_RE.findall(array))
    return codes


def _route_declared_codes() -> set[str]:
    """`routePermissions.ts` 的 `ROUTE_NODE_CODES` 记录值里声明的 code（去重）。

    这个文件**没有** `permissions: [...]` 数组，所以不能只靠菜单那条正则。
    """
    text = _ROUTE_PERMISSIONS_TS.read_text(encoding="utf-8")
    block = text.split(_ROUTE_TABLE_MARKER, 1)[1].split("\n}", 1)[0]
    return {code for _name, code in _ROUTE_NODE_ENTRY_RE.findall(block)}


def _strip_ts_comments(text: str) -> str:
    """去掉 TS 的 `//` 行注释与 `/* */` 块注释（**字符串感知**：引号内的不删）。

    只做到"够用"这一级：不做 AST、**不认正则字面量**（`/…/` 里若含 `//` 会被当行注释
    截掉半行）。这个误差只会让扫描**少**取到字面量，而条数绊线（判据 2 的岛页 3）会响。
    """
    out: list[str] = []
    i, n = 0, len(text)
    state: str | None = None  # None / "'" / '"' / "`" / "line" / "block"
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if ch in "'\"`":
                state = ch
            out.append(ch)
            i += 1
            continue
        if state == "line":
            if ch == "\n":
                state = None
                out.append(ch)
            i += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                state = None
                i += 2
            else:
                if ch == "\n":
                    out.append(ch)
                i += 1
            continue
        # 字符串里：保留内容（只把注释去掉）
        if ch == "\\":
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        if ch == state:
            state = None
        out.append(ch)
        i += 1
    return "".join(out)


def _island_declared_codes() -> set[str]:
    """岛页（`layui-island/pages/*.ts`）里**带点**的 code 字面量（剥注释后去重）。

    扫的是按钮级门禁（`viewerCanNode(me, 'code')` 与 `AR_ACTION_NODES` 这类表）。
    `glob("*.ts")` 只取该目录顶层的文件，`__tests__/` 下的 spec 自然不在内。

    ⚠️ **形状误报的显式排除**（实测 + 修复）：`span.left` 这种「HTML 标签名 +
    `.` + class 名」的 **CSS 选择器**（`querySelectorAll('span.left')`）也匹配
    `_DOTTED_CODE_RE`，却是**误报**（T2 的 `order-orderview.ts` 触发，`4 != 3`）。
    **为什么排除它是安全的**：权限点 code 的模块前缀来自 `catalog.py` 的 `MODULES`
    （`order.` / `system.` / `user.` …），**从来不是 HTML 标签名**——所以排除「以
    `_HTML_TAG_DOT_CLASS_PREFIXES` 里的标签名开头、且后接 `.class`」的字面量，不会削弱
    本判据的本意（抓 code 拼错 / 不在树里）。⛔ 这**不是**「先查权限树、命中才算」——
    那会让判据 1（专抓"不在树里"的 code）**恒真**；未排除的带点字面量仍必须全部在树里。
    ⛔ 也**不是**「理由文本含某个词就跳过」那种后门（E-0065）：排除表是**显式枚举**的。
    """
    codes: set[str] = set()
    for page in sorted(_ISLAND_PAGES_DIR.glob("*.ts")):
        stripped = _strip_ts_comments(page.read_text(encoding="utf-8"))
        for _quote, literal in _STRING_LITERAL_RE.findall(stripped):
            if not _DOTTED_CODE_RE.match(literal):
                continue
            # `span.left` 这类「HTML 标签名 + `.` class」不是权限点 code。见函数 docstring
            # 与 `_HTML_TAG_DOT_CLASS_PREFIXES` 处的说明。
            if literal.split(".", 1)[0] in _HTML_TAG_DOT_CLASS_PREFIXES:
                continue
            codes.add(literal)
    return codes


def _menu_route_codes() -> set[str]:
    """菜单 ∪ 路由的 code 集合（判据 2 的「并集 49」与判据 4 的「被引用」用这个）。

    岛页按钮门**不算**菜单 / 路由引用——这不是遗漏，见判据 4 的说明。
    """
    return _menu_declared_codes() | _route_declared_codes()


def _declared_by_source() -> dict[str, set[str]]:
    """code → 声明它的源标签集合（三个源；同一 code 可能被多个源声明）。"""
    by_source: dict[str, set[str]] = {}
    for label, codes in (
        (_MENU_LABEL, _menu_declared_codes()),
        (_ROUTE_LABEL, _route_declared_codes()),
        (_ISLAND_LABEL, _island_declared_codes()),
    ):
        for code in codes:
            by_source.setdefault(code, set()).add(label)
    return by_source


class MenuPermissionAlignmentTests(SimpleTestCase):
    #: 权限树里 `page` 形态、但**没有被菜单 / 路由引用**的点（冻结）。
    #: ⚠️ 这份清单**不**看岛页按钮门：`system.role_authority_edit` / `_view` 在清单里，
    #: 但它们在岛页里是活的按钮判据（由判据 1 / 3 覆盖）。
    #: 往权限树加一个 `page` 点却没决定它挂哪，下面那条判据就会红——要么让它进菜单/路由，
    #: 要么把这个新 code 加进本清单，并在同一处写明「有意不挂」的理由。
    UNREFERENCED_PAGE_CODES = [
        # ⭐ 2026-11-11 业务方拍板客户维护组收敛：上面 6 个 customer page 点已随目录收敛
        #    **从权限树删除**（迁移 0015）⇒ 不再「未被引用」，而是「不存在」，从清单移出。
        "finance.add_edit_performance",
        "finance.entrusted_commission_details",
        "finance.export_performance",
        "finance.source_analysis",
        "finance.support_order_report",
        "game.add_edit",
        "home.blacklist_board",
        "home.blacklist_report",
        "home.change_password",
        "home.enter",
        # ⛔ `home.feedback` 已从本清单**移出**（问题反馈线）：本线把它挂进了菜单
        #    （`config/menu.ts` 的底部条目）与路由（`config/routePermissions.ts` 的
        #    `'feedback'`）⇒ 它**不再是**「没有被菜单 / 路由引用」的 page 点。
        #    留着它会让下面那条等号断言直接红（清单与差集必须逐条相同）。
        "home.index",
        "home.order_ranking",
        "home.system_circle",
        "home.system_circle_publish",
        "my_orders.export",
        "my_orders.import_to_my_system",
        "my_performance.export",
        "order.assignation",
        "order.download_screenshots_zip",
        "order.edit_payout_price",
        "order.export",
        "order.online_order_list",
        "order.online_payment_records",
        "order.online_refund",
        "order.quick_note",
        "order.screenshot_manage",
        "order.upload_screenshot",
        "order.view_related_split",
        "order.web_order_list",
        "system.add_edit_order_guide_template",
        "system.add_edit_source",
        # ⭐ 频道文章作品线：`system.article_manage` **不进**本清单——它同时被
        #    菜单（`config/menu.ts` 的【APP与小程序】→「发布文章作品」）与路由
        #    （`config/routePermissions.ts` 的 `'app-mp-articles'`）引用 ⇒ `page` 侧 +1 与
        #    「被引用」侧 +1 相抵，计数仍是 43。
        "system.edit_auto_discern_keywords",
        "system.edit_online_consultation_staff",
        "system.edit_position_group",
        "system.order_guide_templates",
        "system.role_authority_edit",
        "system.role_authority_view",
        "system_user.add_edit_login_restriction",
        "system_user.batch_set_game_permissions",
    ]

    #: 13 个栏目码里**没有任何声明源引用**的那几个（冻结，实测 5 个）。
    #:
    #: 这是**登记一个洞**，不是「这些码没问题」：若某个菜单 / 岛页拿一个**裸栏目码**
    #: （无点，例如 `'home'`）当门禁，本文件**看不见**——源 3 只扫带点字面量，而裸小写词
    #: 与 `'form'` / `'table'` / `'order_no'` 这类普通字面量在文本上不可区分，靠正则扫裸词
    #: 会一次卷进 224 个非 code（**不剥注释**协议下实测：227 去重 / 224 不在树；
    #: 同形态按源 3 的剥注释协议是 205 / 202——两个数别混，见模块 docstring）⇒ 不能那么补。
    #: 所以：**新加裸栏目码门禁时，要么让它进菜单 / 路由，要么把它从这里删掉并说明为什么
    #: 它仍然安全**；目录新增栏目、或某个栏目码第一次被声明，这条判据同样会红。
    KNOWN_UNCOVERED_MODULE_CODES = [
        "customer",
        "game",
        "home",
        # ⭐ 菜单两改线，D-0339：「异常消失订单」整条删除 ⇒ 裸栏目码 `order` 的
        # **最后两处声明源**（menu.ts 的那条菜单条目 + routePermissions.ts 的 orders-exception-log
        # 那一行）同一次消失 ⇒ 它**进**本清单。`order` 栏目本身仍在目录里（13 栏目不动），
        # 其后代点（`order.list` / `order.recycle` / …）照样被各自页面引用——离开的只是**裸码**这一种用法。
        "order",
        "system",
        "system_user",
    ]

    def test_declared_codes_exist_in_tree(self):
        """判据 1：三个源声明的每个 code 都必须在权限树里。"""
        tree = _tree_codes()
        # 自证下界：权限树导入/解析失效时先红，别让下面的存在性判据真空绿。
        # ⭐ 首页二维码横幅线（D-0262）：165 → **166**（第三条自有节点
        #    `home.qr_manage`，13 栏目 + 153 功能点）。
        # ⭐ QQ 机器人线：166 → **167**（第四条自有节点
        # `system.qqbot_settings`，13 栏目 + 154 功能点；真值由本网自己报出）。
        # ⭐ 打手钱包 / 提现审核线：167 → **168**（第五条自有节点
        # `finance.taker_withdraw_review`，13 栏目 + 155 功能点）。
        #    （第六条 `finance.taker_wallet_adjust` 的 168 → 169 只改了字面量、未补注记。）
        # ⭐ 闲管家收款明细线：169 → **170**（第七条自有节点
        # `finance.xgj_receipt_detail`，13 栏目 + 157 功能点）。
        # ⭐ 频道文章作品线：170 → **171**（第八条自有节点
        # `system.article_manage`，13 栏目 + 158 功能点）。
        # ⭐ 交易平台涉钱写动作线（审计 M5）：171 → **172**（第九条自有节点
        # `order.platform_write`，13 栏目 + 159 功能点）。
        # ⭐ 企微自建应用线（D-0379）：165 → **166**（第十一条自有节点
        # `system.wecom_settings`，13 栏目 + 153 功能点）。
        self.assertEqual(
            len(tree),
            170,
            "权限树应有 170 个 code（13 栏目 + 157 功能点）",
        )

        # 三个源各自的解析失效（TS 换写法、目录挪走）都要**响亮**，不能静默少查一批 code。
        self.assertTrue(
            _menu_declared_codes(),
            f"{_MENU_LABEL} 没解析到任何 permissions 数组——解析规则可能失效",
        )
        self.assertTrue(
            _route_declared_codes(),
            f"{_ROUTE_LABEL} 没解析到任何 ROUTE_NODE_CODES 条目——解析规则可能失效",
        )
        self.assertTrue(
            _island_declared_codes(),
            f"{_ISLAND_LABEL} 没解析到任何带点的 code 字面量——解析规则可能失效",
        )

        unknown = {
            code: sorted(labels)
            for code, labels in _declared_by_source().items()
            if code not in tree
        }
        if unknown:
            detail = "\n".join(
                f"  {', '.join(labels)} → {code}" for code, labels in sorted(unknown.items())
            )
            self.fail(
                "前端声明的权限点不在权限树里"
                "（权限树 = src/apps/rbac/catalog.py 的 MODULES ∪ NODES）：\n"
                f"{detail}\n"
                "修法：把上面那个源里的 code 改回权限树里的真名。"
            )

    def test_declared_code_count_is_pinned(self):
        """三条数量绊线：菜单、菜单并路由、岛页带点字面量。

        三个数各自抓一类改动。菜单声明是并集的子集，只钉并集时删一条菜单不会红。
        变红时先确认增删是有意的，再改字面量。不要加排除表把不是权限点的带点串
        「认下来」——审计动作码、台账 kind 这类应挪出岛页目录，选择器误报只按
        显式的「标签名.类名」排除。
        打手对接页的节点常量故意放在 api 模块、页面只 import：搬回岛页这条会红。
        """
        # 菜单侧：只删 / 改名一条菜单声明就会变。（54 → 52：2026-11-10 第二批删两条目；
        # 52 → 53：2026-11-11 第五批「客户管理」条目加第二码 member_list_self_only；
        # 53 → 54：同日企微自建应用线「企微自建应用」条目声明自有节点 wecom_settings。）
        self.assertEqual(len(_menu_declared_codes()), 54)
        # 菜单并路由：路由侧独有的 code 只在这里露头。（53 → 54：第五批同批 +1；
        # 54 → 55：企微线的 `v2-wecom-settings` 路由声明。）
        self.assertEqual(len(_menu_route_codes()), 55)
        # 岛页按钮门：带点 code 字面量的增删 / 改名。两条一起判，避免只改一处。
        self.assertEqual(len(_island_declared_codes()), 32)
        self.assertEqual(len(_island_declared_codes()), 32)

    def test_frontend_action_nodes_match_backend(self):
        """判据 3：岛页 `AR_ACTION_NODES` 的 code 集合 == 后端 `ACTION_NODES` 的值集合。

        这两边都是**手写**的、且都决定「哪个按钮打得开」——谁单方面改名，谁就先坏一边
        （前端按钮点了提示无权限、或后端直接 403），而此前没有任何对拍。

        ⚠️ 这里的期望值**刻意不是**字面 code 清单（与 E-0024 的一般要求不同）：
        本判据的**本体**就是"两个独立手写来源必须一致"，写成第三份字面清单反而会同时
        盖住两边的漂移。防真空靠下面那条后端条数的自证下界。

        ⚠️ **本判据只钉「值集合」，不钉「动作 → code」的映射**（两边**都**取 `.values()` /
        表里的值）：把 `copy` 从 `_edit` 改指向 `_view`（值集合仍是那 3 个）本判据照样绿。
        那条映射**已经**被别处钉住，别指望这一条：
        - 前端 `layui-island/pages/__tests__/system-roleauthority.spec.ts` 用 `AR_ACTION_NODES`
          的**字面量** `toEqual({ add / edit / copy / delete / view: … })` 钉住逐动作对应；
        - 后端 `tests/test_rbac_api.py::RoleAuthorityPermissionGateTests` 按
          「持有 ⇒ 放行、不持有 ⇒ 403」双向逐动作钉住 `_edit` / `_delete` 的区分。
        """
        text = _AR_ACTION_NODES_TS.read_text(encoding="utf-8")
        match = _AR_ACTION_NODES_RE.search(text)
        self.assertIsNotNone(match, "没解析到 AR_ACTION_NODES——解析规则可能失效")
        frontend_codes = sorted(set(_AR_ACTION_ENTRY_RE.findall(match.group(1))))
        backend_codes = sorted(set(BACKEND_ACTION_NODES.values()))

        self.assertEqual(
            len(backend_codes),
            3,
            "后端 ACTION_NODES 的值去重后应有 3 个 code；数字变了就要更新本判据",
        )
        self.assertEqual(frontend_codes, backend_codes)

    def test_unreferenced_page_codes_are_frozen(self):
        """判据 4：权限树里没有被菜单 / 路由引用的 `page` 点——具名清单 + 数量冻结。

        ⚠️ 判据里的「被引用」只算**菜单 + 路由**（`_menu_route_codes`）；岛页按钮门不算，
        所以 `system.role_authority_edit` / `_view` 会出现在清单里——它们的实际引用在岛页，
        由判据 1 / 3 覆盖。
        """
        page_codes = {n[0] for n in NODES if n[5] == NodeKind.PAGE}
        # 自证下界：形态取错（例如列号写错）时先红。
        self.assertEqual(len(page_codes), 83, "page 形态的权限点应有 83 个（企微线 D-0379 +1）")

        # 「被引用」= 菜单 ∪ 路由，不只是菜单那一份：`routePermissions.ts` 引用了
        # `order.view_detail` 这个 page 点，只算菜单会把它误报成「没人引用」。
        referenced = _menu_route_codes()
        unreferenced = sorted(code for code in page_codes if code not in referenced)

        # 差集变了：新 page 点要么挂进菜单和路由，要么写进下面这份具名清单。
        self.assertEqual(
            len(unreferenced),
            # 45 → 39：2026-11-11 客户维护组收敛，6 个 customer page 点从树里删除。
            39,
            "未被菜单/路由引用的 page 点数量变了：新 page 点要么挂进菜单/路由，"
            "要么加进本清单并写明理由",
        )
        self.assertEqual(unreferenced, self.UNREFERENCED_PAGE_CODES)

    def test_uncovered_module_codes_are_frozen(self):
        """判据 5：13 个栏目码里**没有任何声明源引用**的——具名清单 + 数量冻结。

        为什么需要它：源 3（岛页）只扫**带点**字面量，所以**裸栏目码**（无点）当门禁时
        本文件抓不到（评审实测：把某个活门禁换成 `'home'`，4 条判据全绿）。裸词不能靠正则
        扫出来（与 `'form'` / `'table'` 这类普通字面量不可区分），所以把这个洞**钉在测试里**：
        清单变了（目录加栏目、或某个栏目码第一次被声明 / 不再被声明）就必须有人有意识改一次。
        """
        module_codes = {m[0] for m in MODULES}
        # 自证下界：栏目数取错（例如列号写错）时先红。
        self.assertEqual(len(module_codes), 13, "栏目（level=0）应有 13 个")

        # 「被覆盖」= 出现在菜单或路由的声明里（源 1 / 2）；源 3 不扫裸词，故不看它。
        covered = _menu_route_codes()
        uncovered = sorted(code for code in module_codes if code not in covered)

        # 6 个栏目码没有任何声明源引用。裸栏目码当门禁这条网看不见，所以清单具名冻结。
        self.assertEqual(
            len(uncovered),
            6,
            "未被任何声明源引用的栏目码数量变了：要么把它挂进菜单/路由，"
            "要么更新本清单并说明为什么它仍然安全",
        )
        self.assertEqual(uncovered, self.KNOWN_UNCOVERED_MODULE_CODES)
