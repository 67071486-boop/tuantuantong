"""spec §10-A9 / E-0010：RBAC 端点的**这类**请求都不得 500。

**判据是「这类请求还会不会 500」，不是「这个字段还会不会 500」**（AGENTS.md §5）。
所以本文件按**请求形状**分簇，而不是按字段逐个点名：

1. 畸形但语法合法的**查询参数**（`page=abc` / `available=maybe` / 超长 `key`）；
2. 畸形 **JSON 体**（截断的 JSON、JSON 之外的内容、空体、二进制垃圾）；
3. **超大值 / 畸形形状 / 缺字段**的写载荷（`name` 超长、`remark` 超长、
   `permission_codes` 非列表 / 元素非字符串 / 空列表 / 未知 code）；
4. **畸形路径 id**（`abc`、`-1`、`abc;DROP`、`1 2`）打在**每一个**带 `pk` 的动作上。

⚠️ **它为什么不能塞进 `tests/test_no_route_500.py`**：那个文件从
`apps.business.urls.router` **枚举**集合并拼 `/api/business/{prefix}/`
（`test_no_route_500.py:31-34`），而 RBAC 端点挂在
`config/urls.py → apps.core.urls → apps.rbac.urls` 上，**不在那个 router 里**。
照搬会得到一个「遍历 0 条 RBAC 路由」的**空洞测试**——正是该文件自己在 `:38-39` 防的
失败模式。所以本文件独立，并**自证路由来源非空**（`test_route_source_is_not_empty`
与 `test_every_declared_action_appears_in_the_table`）。

⚠️ **三处会假通过的地方，别把它们删掉**：
- 路由表为空时，下面的循环一个 subTest 都不跑，用例**照样绿**。所以
  `ROUTE_TABLE` 的模块级 `assert` + 独立的非空自证用例、以及每簇末尾的
  `assertGreaterEqual(executed, …)` 是承重的；
- 每个 subTest 先断言**非 500**、再断言「响应是信封」——顺序反过来的话，HTML 500
  页面上没有 `code` 键，用例也会红，只是失败信息会指错方向；
- 「动作名 → HTTP 方法」的映射来自**解析器**，不是手抄的清单（手抄清单会在
  「路由改了、方法改了」时静默走错分支）。

⚠️ **如实口径**：`status_code != 500` 是**结构兜底**，它证明不了「这些请求都返回了
正确的业务码」——那是 `tests/test_rbac_api.py` 的职责。本文件只守
「不许把未预期异常直接漏成 HTML 500」这一条下限（E-0010）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_builtin_roles, seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 租户域名（`TenantTestCase` 的默认测试域名，与其余 RBAC 测试同口径）。
DOMAIN = "tenant.test.com"

#: 必须被本文件覆盖的 8 个动作。少一个就说明路由表读漏了（或新动作没进表）。
REQUIRED_ACTIONS = frozenset(
    {
        "list",
        "create",
        "retrieve",
        "update",
        "partial_update",
        "destroy",
        "copy",
        "permissions",
    }
)

#: 本文件认的 URL 前缀（`get_resolver()` 展开后的正则前缀）。
#: `role-authorities` 是 8 个动作的全部来源，`permission-nodes` 只有 `list`。
_ROUTE_PREFIXES = ("api/^role-authorities", "api/^permission-nodes")

#: 畸形路径 id。`٣` / `²` 不在这里（`isdecimal()` 对 `٣` 为真，它会落到 ORM 上），
#: 单列在 `_DECIMAL_LOOKALIKE_IDS`；带 `.` 的匹配不到路由，单列在 `_ROUTING_404_IDS`。
_MALFORMED_IDS = ("abc", "-1", "1 2", "abc;DROP TABLE auth_user", "١٢٣abc", "0x10")

#: 匹配不到路由的路径值（含 `.` 或 `/` 会被 DRF 的 `pk` 正则 `[^/.]+` 排除）⇒ Django 404。
_ROUTING_404_IDS = ("1.5", "1.2.3", "a/b")

#: 会被 `isdecimal()` 放行、进而落到 ORM 上的「数字样」id。
_DECIMAL_LOOKALIKE_IDS = ("٣", "²")

#: 写路径的**超大值 / 畸形形状 / 缺字段**载荷。这些形状正是 E-0010 的原型。
_OVERSIZED_WRITE_PAYLOADS = (
    {"name": "超级客服主管", "remark": "r", "permission_codes": ["order.list"]},   # name > 5
    {"name": "客服", "remark": "r" * 51, "permission_codes": ["order.list"]},      # remark 上界 + 1
    {"name": "客服", "remark": "r" * 100000, "permission_codes": ["order.list"]},  # 远超上界
    {"name": "客服", "permission_codes": "not-a-list"},                            # ListField 收字符串
    {"name": "客服", "permission_codes": {"a": 1}},                                # ListField 收 dict
    {"name": "客服", "permission_codes": [{"nested": "dict"}]},                    # 元素非字符串
    {"name": "客服", "permission_codes": [123]},                                   # 元素非字符串
    {"name": "客服", "permission_codes": [None]},                                  # 元素为 null
    {"name": "客服", "permission_codes": ["x" * 1000]},                            # 元素超长
    {"name": "客服", "permission_codes": []},                                      # allow_empty=False
    {"name": "客服", "permission_codes": ["nope.nope"]},                           # 未知 code
    {"name": "客服", "permission_codes": ["order.list", "order.list"]},            # 重复 code
    {"name": None, "permission_codes": ["order.list"]},                            # 必填为 null
    {"name": "", "permission_codes": ["order.list"]},                              # 必填为空串
    {"name": 12345, "permission_codes": ["order.list"]},                           # name 非字符串
    {"name": "客服", "permission_codes": ["order.list"], "extra": "ignored"},      # 未知字段
    {},                                                                            # 缺全部字段
)

#: 原始请求体（`data=` 直接给字符串，绕开 dict 编码）。截断的 JSON 是最真实的畸形体。
_RAW_BODIES = (
    "{not json",
    "[1, 2, 3]",                       # 合法 JSON、错误形状
    '"just-a-string"',                 # 合法 JSON、错误形状
    "",                                # 空体
    "\x00\x01\x02",                    # 二进制垃圾
)

#: 能收写请求的动作 → 该动作在 URL 上的 HTTP 方法（`list` / `retrieve` / `permissions` 是只读）。
_WRITE_ACTIONS = frozenset({"create", "update", "partial_update", "destroy", "copy"})


def _route_table():
    """从解析器里读出**RBAC 端点**的路由表：`路径模板 → {动作名: HTTP 方法}`。

    用 `get_resolver()` 递归展开，而不是照抄 `apps.rbac.urls.router.registry`
    ——`registry` 只有 `(prefix, viewset, basename)`，**读不出动作**，而本任务要按动作
    发出畸形路径 id 与写载荷。走解析器读的是**真正被挂载**的那份表。

    ⚠️ 解析器给的是 **URL 正则**（`api/^role-authorities/(?P<pk>[^/.]+)/$`），不是 URL。
    所以这里做四件事（与 `test_account_no_500.py` 同形状，理由见那里的 docstring）：
    补开头的 `/`、去掉 `^`/`$`、把 `(?P<pk>…)` 换成占位符 `<pk>`、丢掉格式后缀路由。
    """
    found = {}

    def to_template(pattern):
        text = str(pattern).replace(r"(?P<pk>[^/.]+)", "<pk>")
        if "(?P<format>" in text:
            return None                     # 格式后缀路由，与无后缀那条同视图同动作
        text = text.replace("^", "").replace("$", "")
        return "/" + text.lstrip("/")

    def walk(patterns, prefix=""):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                raw = prefix + str(pattern.pattern)
                if not raw.startswith(_ROUTE_PREFIXES):
                    continue
                template = to_template(raw)
                if template is None:
                    continue
                actions = getattr(pattern.callback, "actions", None)
                if not actions:
                    continue        # 非 ViewSet 路由
                # DRF 的 `actions` 是 `{http_method: action_name}`，翻成 `{action_name: http_method}`。
                found[template] = {name: method for method, name in actions.items()}

    walk(get_resolver().url_patterns)
    return found


#: 模块级构建一次。**它是本文件唯一的覆盖来源**，所以每个用例都要先自证它非空。
ROUTE_TABLE = _route_table()
assert ROUTE_TABLE, "RBAC 路由表是空的——本文件的全部用例都会空转通过"


def _action_routes():
    """产出 `(路径模板, 动作名, HTTP 方法)` 三元组。"""
    for path, actions in sorted(ROUTE_TABLE.items()):
        for action, method in sorted(actions.items()):
            yield path, action, method


class RbacNo500Tests(TenantTestCase):
    """RBAC 端点全表面的 500 兜底。"""

    MALFORMED_QUERY = (
        {"page": "abc"},
        {"page": "-1"},
        {"page": "1e999"},
        {"page_size": "0"},
        {"page_size": "999999999999999999999"},
        {"available": "maybe"},
        {"available": ""},
        {"available": "1"},
        {"key": "x" * 5000},
        {"unknown_param": "1"},
    )

    def setUp(self):
        seed_catalog()
        seed_builtin_roles()
        # 判据的判别力由 `test_rbac_predicate.py` 的探针单独证；本文件测的是
        # 「畸形请求不得 500」，需要请求**进得了视图**，所以用管理员
        # （`has_node_now` 对租户管理员短路）。⚠️ 不能 `force_login`——会话没有 `identity_plane`
        # 会被 fail-closed 拒成 401（`tests/p5_auth_helpers.py` 的 docstring 有实测）。
        # ⚠️ 判据统一后不能"登录后手工置 `is_superuser`"：管理员判据是
        # 「持『系统管理员』职位」，裸超管过不了节点门；夹具的 `is_superuser=True` 会顺手给组。
        self.client = make_logged_in_client(
            username="n5-admin", password="pw-123456", is_superuser=True
        )
        # 一个**专用**的可改角色：避免把内置角色的名字/权限改掉而影响同文件的其它子用例。
        # `Role.name` 是 `varchar(5)`，名字必须短。
        self.role = Role.objects.create(
            name="n5角色", group=Group.objects.create(name="n5角色")
        )
        self.role.permissions.set(PermissionNode.objects.filter(code="order.list"))

    def _url(self, path):
        return path.replace("<pk>", str(self.role.pk))

    # ── 自证：路由来源非空 + 动作齐全 ──────────────────────────────
    def test_route_source_is_not_empty(self):
        """先证路由表读到了东西，否则下面的循环会空转通过。"""
        self.assertTrue(ROUTE_TABLE, "解析器里没有 RBAC 路由——测试本身失效了")
        patterns = [str(p.pattern) for p in get_resolver().url_patterns]
        self.assertTrue(any("api/" in p for p in patterns), patterns)

    def test_every_declared_action_appears_in_the_table(self):
        """路由表必须覆盖声明的**每一个**动作；少一个就是这一族请求没被测到。"""
        seen = {action for _path, action, _method in _action_routes()}
        self.assertEqual(
            REQUIRED_ACTIONS - seen, set(),
            f"这些动作没进路由表：{sorted(REQUIRED_ACTIONS - seen)}",
        )
        for path, action, method in _action_routes():
            self.assertTrue(method, f"{path} 的 {action} 没有 HTTP 方法")

    # ── 1 查询参数 ────────────────────────────────────────────────
    def test_malformed_query_params_never_500(self):
        """畸形但语法合法的查询参数：一律 200 + 业务码，不得 HTML 500。"""
        executed = 0
        for params in self.MALFORMED_QUERY:
            for path in ("/api/role-authorities/", "/api/permission-nodes/"):
                executed += 1
                with self.subTest(params=params, path=path):
                    response = self.client.get(path, params)
                    self.assertNotEqual(response.status_code, 500, response.content[:300])
                    self.assertIn("code", response.json())
        self.assertGreaterEqual(executed, len(self.MALFORMED_QUERY))

    # ── 2/3 写载荷：超大值、畸形形状、缺字段（逐路由）────────────
    def test_malformed_write_payloads_never_500(self):
        """把每一组写载荷打到每一条能收写请求的路由上。

        路由与动作从 `ROUTE_TABLE` 现读（不是手抄清单）：新增动作时它会自动进这组。
        """
        executed = 0
        for path, action, method in _action_routes():
            if action not in _WRITE_ACTIONS:
                continue
            url = self._url(path)
            for payload in _OVERSIZED_WRITE_PAYLOADS:
                executed += 1
                with self.subTest(action=action, payload=_short(payload)):
                    response = getattr(self.client, method)(
                        url, payload, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url} ← {_short(payload)}: {response.content[:300]}",
                    )
                    self.assertIn("code", response.json())
        # 自证真的跑到了：路由表为空 / 动作全被跳过时，上面一圈都不跑。
        self.assertGreaterEqual(
            executed, len(_OVERSIZED_WRITE_PAYLOADS),
            "这一簇几乎没发出请求——路由表或动作映射坏了",
        )

    def test_raw_malformed_json_bodies_never_500(self):
        """原始体：截断的 JSON、错误形状的 JSON、空体、二进制垃圾。

        ⚠️ 与上面那簇的区别：这里 **`data=` 直接给字符串**。DRF 的 `JSONParser`
        在解析失败时抛 `ParseError` → 400 信封；给 dict 是构造不出这一路的。
        """
        executed = 0
        for body in _RAW_BODIES:
            for path, action, method in _action_routes():
                if action not in _WRITE_ACTIONS:
                    continue
                url = self._url(path)
                executed += 1
                with self.subTest(action=action, body=body[:20]):
                    response = getattr(self.client, method)(
                        url, body, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url} ← {body[:40]!r}: {response.content[:300]}",
                    )
                    self.assertIn("code", response.json())
        self.assertGreaterEqual(executed, len(_RAW_BODIES), "这一簇几乎没发出请求——动作映射坏了")

    # ── 4 畸形路径 id（每一个带 pk 的动作）────────────────────────
    def test_malformed_path_ids_never_500(self):
        """畸形路径 id 打在**每一个**带 `pk` 的动作上（含每个动作的正确动词）。

        `views._get_object` 的 `isdecimal()` 守卫覆盖 8 个动作共用的出口；
        没有它时 `GET /api/role-authorities/abc/` 会抛 `ValueError` ⇒ HTML 500
        （`views._get_object` 的 docstring 有实测）。本用例只守「都不得 500」，
        码值口径由 `tests/test_rbac_api.py` 钉住。
        """
        detail_routes = [
            (path, action, method) for path, action, method in _action_routes()
            if "<pk>" in path
        ]
        self.assertTrue(detail_routes, "路由表里没有带 pk 的 RBAC 路由")
        for path, action, method in detail_routes:
            for bad_id in _MALFORMED_IDS:
                url = path.replace("<pk>", bad_id)
                with self.subTest(action=action, pk=bad_id):
                    response = getattr(self.client, method)(
                        url, {"name": "客服"}, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url}: {response.content[:300]}",
                    )
                    self.assertIn("code", response.json())

    def test_decimal_lookalike_path_ids_never_500(self):
        """会被 `isdecimal()` 放行的「数字样」id（`٣` / `²`）：归宿可以是 1000，不得 500。"""
        detail_routes = [
            (path, action, method) for path, action, method in _action_routes()
            if "<pk>" in path
        ]
        self.assertTrue(detail_routes)
        for path, action, method in detail_routes:
            for lookalike in _DECIMAL_LOOKALIKE_IDS:
                url = path.replace("<pk>", lookalike)
                with self.subTest(action=action, pk=lookalike):
                    response = getattr(self.client, method)(
                        url, {"name": "客服"}, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url}: {response.content[:300]}",
                    )
                    body = response.json()
                    self.assertIn("code", body)
                    # 业务成功（0，当该样值恰好落在一行上）或业务失败（1000）都合格。
                    self.assertIn(body["code"], (0, 1000), body)

    def test_routing_level_404_ids_never_500(self):
        """带 `.`（或 `/`）的路径值**匹配不到注册路由**：归宿是 Django 的 404 页，不得 500。

        ⚠️ 不主张 404 页的形状——那是 Django 的默认页，与 RBAC 无关；把它的字节形状
        写进断言只会在 Django 升级时无谓地红。
        """
        detail_routes = [
            (path, action, method) for path, action, method in _action_routes()
            if "<pk>" in path
        ]
        self.assertTrue(detail_routes)
        for path, action, method in detail_routes:
            for dotted in _ROUTING_404_IDS:
                url = path.replace("<pk>", dotted)
                with self.subTest(action=action, pk=dotted):
                    response = getattr(self.client, method)(
                        url, {"name": "客服"}, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url}: {response.content[:300]}",
                    )

    # ── 5 正向对照：这些端点对**合法**请求必须真的工作 ──────────────
    def test_valid_requests_still_succeed(self):
        """正向对照：没有它，「一律不 500」可能由「这个端点对谁都拒绝」凑出来。"""
        listing = self.client.get("/api/role-authorities/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["code"], 0)

        tree = self.client.get("/api/permission-nodes/")
        self.assertEqual(tree.json()["code"], 0)

        detail = self.client.get(f"/api/role-authorities/{self.role.pk}/").json()
        self.assertEqual(detail["code"], 0, detail)

        created = self.client.post(
            "/api/role-authorities/",
            {"name": "n5新", "remark": "r", "permission_codes": ["order.list"]},
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0, created)

        copied = self.client.post(
            f"/api/role-authorities/{self.role.pk}/copy/",
            {"name": "n5拷", "remark": ""},
            content_type="application/json",
        ).json()
        self.assertEqual(copied["code"], 0, copied)

        patched = self.client.patch(
            f"/api/role-authorities/{self.role.pk}/",
            {"remark": "改过"},
            content_type="application/json",
        ).json()
        self.assertEqual(patched["code"], 0, patched)

        permissions = self.client.get(
            f"/api/role-authorities/{self.role.pk}/permissions/"
        ).json()
        self.assertEqual(permissions["code"], 0, permissions)

        removed = self.client.delete(f"/api/role-authorities/{created['data']['id']}/")
        self.assertEqual(removed.json()["code"], 0, removed.json())

    # ── 6 未认证 / 无权限也不得 500 ───────────────────────────────
    def test_unauthorized_shapes_never_500(self):
        """匿名与无权限账号：`401` / `403` 信封，不是 500（fail-closed 的守卫本身也要稳）。"""
        anonymous = Client(SERVER_NAME=DOMAIN)
        outsider = make_logged_in_client(username="n5-outsider")
        for who, client in (("anonymous", anonymous), ("no-node", outsider)):
            for path, action, method in _action_routes():
                url = self._url(path)
                with self.subTest(action=action, who=who):
                    response = getattr(client, method)(
                        url, {}, content_type="application/json"
                    )
                    self.assertNotEqual(response.status_code, 500, response.content[:300])
                    body = response.json()
                    self.assertIn("code", body)
                    self.assertIn(body["code"], (401, 403), body)


def _short(payload):
    """把载荷压成一行短文本放进 subTest 名里（超长载荷会把输出刷爆）。"""
    text = repr(payload)
    return text if len(text) <= 90 else text[:87] + "..."
