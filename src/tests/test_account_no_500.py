"""§7.5 / E-0010：账号端点的**这类**请求都不得 500。

**判据是「这类请求还会不会 500」，不是「这个字段还会不会 500」**（AGENTS.md §5）。
所以本文件按**请求形状**分簇，而不是按字段逐个点名：

1. 畸形但语法合法的**查询参数**（`page=abc` / `is_active=maybe` / 未知 `role` / 超长 `key`）；
2. 畸形 **JSON 体**（截断的 JSON、JSON 之外的内容、空体）；
3. **超大值**（150/64/12 位以上的串、超出 int4 上界的整数、超长日期串）；
4. **缺字段**（`{}`、必填项为 `null`）；
5. `ids` 传**非列表** / 列表里塞非整数；
6. **畸形路径 id**（`abc`、`1.5`、`abc;DROP`、`1 2`）打在**每一个**带 `pk` 的动作上。

⚠️ **它为什么不能塞进 `tests/test_no_route_500.py`**：那个文件从
`apps.business.urls.router` **枚举**集合并拼 `/api/business/{prefix}/`
（`test_no_route_500.py:31-34`），而账号端点挂在
`config/urls.py → apps.core.urls → apps.accounts.urls` 上，**不在那个 router 里**。
照搬会得到一个「遍历 0 个账号路由」的**空洞测试**——正是该文件自己在 `:38-39` 防的
失败模式。所以本文件独立，并**自证路由来源非空**（`test_route_source_is_not_empty`
与 `test_every_declared_action_appears_in_the_table`）。

⚠️ **三处会假通过的地方，别把它们删掉**：
- 路由表为空时，下面的循环一个 subTest 都不跑，用例**照样绿**。所以
  `_route_table()` 里的 `assert` + 独立的非空自证用例、以及每簇末尾的
  `assertGreaterEqual(executed, …)` 是承重的；
- 每个 subTest 先断言**非 500**、再断言「响应是信封」——顺序反过来的话，HTML 500
  页面上没有 `code` 键，用例也会红，只是失败信息会指错方向；
- 「动作名 → HTTP 方法」的映射来自**解析器**，不是手抄的清单。手抄的清单会在
  「路由改了、方法改了」时静默走错分支（实测：本文件第一版把手抄清单里的
  `action` 当成 HTTP 方法用，于是 `getattr(client, "list")` 直接 AttributeError，
  同时「映射坏了」的那条自证断言把真正的失败信息淹在 63 个 error 下面）。

⚠️ **如实口径**：`status_code != 500` 是**结构兜底**，它证明不了「这些请求都返回了
正确的业务码」——那是各端点自己的契约测试（`tests/test_account_api.py`）的职责。
本文件只守「不许把未预期异常直接漏成 HTML 500」这一条下限（E-0010）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game
from apps.business.roles import TENANT_ADMIN
from apps.platform.provisioning import seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 租户域名（`TenantTestCase` 的默认测试域名，与其余账号测试同口径）。
DOMAIN = "tenant.test.com"

#: 必须被本文件覆盖的 9 个动作。少一个就说明路由表读漏了（或新动作没进表）。
REQUIRED_ACTIONS = frozenset(
    {
        "list",
        "create",
        "retrieve",
        "update",
        "partial_update",
        "roles",
        "status",
        "status_batch",
        "reset_password",
    }
)

#: 畸形路径 id。`٣` / `²` 不在这里：`isdecimal()` 对 `٣`（阿拉伯-印度数字）为真，
#: 它**会**被当成数字查库（已 ledger 的行为，见 `views._lookup_id_error` 的注释），
#: 归宿是「查不到行 → 1002」而不是「畸形 id」。本文件的判据是「不得 500」，
#: 两种归宿都合格，但把它们混进畸形清单会让失败信息误导读者，所以分开列在
#: `_DECIMAL_LOOKALIKE_IDS`。
#:
#: ⚠️ **带 `.` 的值不在这里**：DRF 生成的 `pk` 正则（`[^/.]+`）把 `.` 排除在外，
#: 所以 `/api/accounts/1.5/status/` **匹配不到路由**——它在 Django 的 URL 解析层
#: 就是 404，压根进不了视图（实测：`response.json()` 抛
#: `Content-Type is "text/html"`，那是 Django 的 404 页）。这条形状单列在
#: `_ROUTING_404_IDS` 里，判据仍是「不得 500」，只是归宿必然是 404。
_MALFORMED_IDS = ("abc", "-1", "1 2", "abc;DROP TABLE auth_user", "١٢٣abc", "0x10")

#: 匹配不到路由的路径值（含 `.` 或 `/` 会被 `[^/.]+` 排除）。归宿是 Django 的 404。
_ROUTING_404_IDS = ("1.5", "1.2.3", "a/b")

#: 会被 `isdecimal()` 放行、进而落到 ORM 上的「数字样」id。
_DECIMAL_LOOKALIKE_IDS = ("٣", "²")

#: 写路径的**超大值 / 畸形形状**载荷。这些形状正是 E-0010 的原型
#: （`?game=abc`、超长 `actor`、越界 int4）。
_OVERSIZED_WRITE_PAYLOADS = (
    {"username": "u" * 200, "role": "客服", "games": [1]},          # CharField max_length=150
    {"display_name": "d" * 200, "role": "客服", "games": [1]},       # 150
    {"id_card_no": "1" * 300, "role": "客服", "games": [1]},         # 18
    {"mobile": "1" * 300, "role": "客服", "games": [1]},             # 32
    {"remark": "r" * 100000, "role": "客服", "games": [1]},          # TextField（无上界）
    {"grab_limit_dl": 2147483648, "role": "客服", "games": [1]},     # int4 上界 + 1
    {"grab_limit_pw": 99999999999999999999, "role": "客服", "games": [1]},
    {"hired_on": "2026-13-45", "role": "客服", "games": [1]},        # 不存在的日期
    {"hired_on": "9" * 40, "role": "客服", "games": [1]},            # 超长日期串
    {"base_salary": "1" * 40, "role": "客服", "games": [1]},         # Decimal max_digits=12
    {"commission_value": "abc", "role": "客服", "games": [1]},
    {"games": "not-a-list"},                                          # ListField 收到字符串
    {"games": [{"nested": "dict"}]},                                  # 列表里塞字典
    {"games": [0]},                                                   # min_value=1
    {"role": "不存在的职位", "games": [1]},                            # ChoiceField
    {"role": None, "games": [1]},                                     # 必填项为 null
    {"username": None, "role": "客服", "games": [1]},                 # 必填项为 null
    {},                                                               # 缺全部字段
)

#: 动作类端点（启停 / 批量启停 / 重置密码）的**超大值 / 畸形形状**载荷。
_OVERSIZED_ACTION_PAYLOADS = (
    {"ids": "abc", "is_active": False},                               # ids 非列表
    {"ids": {"a": 1}, "is_active": False},                            # ids 是 dict
    {"ids": [1, "abc"], "is_active": False},                          # 列表里塞非整数
    {"ids": [2147483648], "is_active": False},                        # int4 上界 + 1
    {"ids": [99999999999999999999], "is_active": False},
    {"ids": [], "is_active": False},                                  # allow_empty=False
    {"ids": ["1" * 100], "is_active": False},                         # 超长数字串
    {"ids": [1, 1, 1], "is_active": "maybe"},                         # 布尔位收到字符串
    {"is_active": "maybe"},                                           # 单个启停的布尔位
    {"is_active": 1},                                                 # 布尔位收到整数
    {"is_active": None},                                              # 布尔位收到 null
    {},
)

#: 原始请求体（`data=` 直接给字符串，绕开 dict 编码）。截断的 JSON 是最真实的畸形体。
_RAW_BODIES = (
    "{not json",
    "[1, 2, 3]",                       # 合法 JSON、错误形状
    '"just-a-string"',                 # 合法 JSON、错误形状
    "",                                # 空体
    "\x00\x01\x02",                    # 二进制垃圾
)

#: 能收写请求的动作 → 该动作在 URL 上的 HTTP 方法（`roles` / `list` / `retrieve` 是只读）。
_WRITE_METHODS = {
    "create": "post",
    "update": "put",
    "partial_update": "patch",
    "status": "post",
    "status_batch": "post",
    "reset_password": "post",
}


def _route_table():
    """从解析器里读出**账号端点**的路由表：`路径模板 → {动作名: HTTP 方法}`。

    用 `get_resolver()` 递归展开，而不是照抄 `apps.accounts.urls.router.registry`
    ——`registry` 只有 `(prefix, viewset, basename)`，**读不出动作**，
    而本任务要按动作发出畸形路径 id（`/status/`、`/reset-password/`）。
    走解析器还有一个好处：它读的是**真正被挂载**的那份表（含 `apps/core/urls.py`
    的 `api/` 前缀与 router 生成的全部路由），而不是别人内存里的一条注册记录。

    ⚠️ 解析器给的是 **URL 正则**（`api/^accounts/(?P<pk>[^/.]+)/$`），**不是 URL**
    ——本文件第一版直接拿它当路径发请求，于是每条都是 404 的 HTML 页
    （症状：`response.json()` 抛 `Content-Type is "text/html"`，看着像 500）。
    所以这里做四件事，别删：
    1. 补**开头的 `/`**：没有它 Django 的测试客户端把 `api/accounts/` 当成相对路径，
       解析结果是 404（实测：带 `/` 就是 401 信封）——这是最隐蔽的一条；
    2. `^` / `$` 交给 Django 的解析器，不在路径里出现 ⇒ 去掉；
    3. 具名组 `(?P<pk>[^/.]+)` 换成占位符 **`<pk>`**，调用方再按用例换值；
    4. `.*\\.(?P<format>…)$` 那一族是 DRF 的**格式后缀**路由，与无后缀那条指向同一个
       视图、同一批动作。表里只留无后缀那条：留着只会让每组用例跑两遍（同样的动作、
       同样的载荷），却不多证明任何东西。
    """
    found = {}

    def to_template(pattern):
        """URL 正则 → 可请求的路径模板（见 docstring）。"""
        text = str(pattern).replace(r"(?P<pk>[^/.]+)", "<pk>")
        if "(?P<format>" in text:
            return None                     # 格式后缀路由，见 docstring
        text = text.replace("^", "").replace("$", "")
        return "/" + text.lstrip("/")

    def walk(patterns, prefix=""):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                raw = prefix + str(pattern.pattern)
                if not raw.startswith("api/^accounts"):
                    continue
                template = to_template(raw)
                if template is None:
                    continue
                actions = getattr(pattern.callback, "actions", None)
                if not actions:
                    continue        # 非 ViewSet 路由
                # DRF 的 `actions` 是 `{http_method: action_name}`，这里翻成
                # `{action_name: http_method}`——调用方要的是「这个动作用哪个动词发」。
                found[template] = {name: method for method, name in actions.items()}

    walk(get_resolver().url_patterns)
    return found


#: 模块级构建一次。**它是本文件唯一的覆盖来源**，所以每个用例都要先自证它非空。
ROUTE_TABLE = _route_table()
assert ROUTE_TABLE, "账号路由表是空的——本文件的全部用例都会空转通过"


def _action_routes():
    """产出 `(路径模板, 动作名, HTTP 方法)` 三元组。"""
    for path, actions in sorted(ROUTE_TABLE.items()):
        for action, method in sorted(actions.items()):
            yield path, action, method


class AccountNo500Tests(TenantTestCase):
    """账号端点全表面的 500 兜底。"""

    MALFORMED_QUERY = {
        "page": "abc",
        "page_size": "abc",
        "is_active": "maybe",
        "role": "不存在的职位",
        "key": "x" * 500,
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # R5：`TenantTestCase` 不跑 `seed_tenant`，而本类要 `Group.objects.get(TENANT_ADMIN)`。
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="n5-admin")
        User.objects.get(username="n5-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.target = User.objects.create_user(username="n5-target", password="pw-123456")

    def _url(self, path):
        return path.replace("<pk>", str(self.target.pk))

    # ── 自证：路由来源非空 + 动作齐全 ──────────────────────────────
    def test_route_source_is_not_empty(self):
        """先证路由表读到了东西，否则下面的循环会空转通过。"""
        self.assertTrue(ROUTE_TABLE, "解析器里没有账号路由——测试本身失效了")
        patterns = [str(p.pattern) for p in get_resolver().url_patterns]
        self.assertTrue(any("api/" in p for p in patterns), patterns)

    def test_every_declared_action_appears_in_the_table(self):
        """路由表必须覆盖声明的**每一个**动作；少一个就是这一族请求没被测到。

        ⚠️ 这条挡的是「表读漏了一个动作」：漏掉时下面的循环照样绿，
        因为没有任何断言知道那个动作本该在表里。
        """
        seen = {action for _path, action, _method in _action_routes()}
        self.assertEqual(REQUIRED_ACTIONS - seen, set(),
                         f"这些动作没进路由表：{sorted(REQUIRED_ACTIONS - seen)}")
        # 覆盖量自证：每个动作都带一个可用的 HTTP 方法（空方法会让 `getattr` 抛错）。
        for path, action, method in _action_routes():
            self.assertTrue(method, f"{path} 的 {action} 没有 HTTP 方法")

    # ── 1 查询参数 ────────────────────────────────────────────────
    def test_malformed_query_params_never_500(self):
        """畸形但语法合法的查询参数：`?is_active=maybe` 直接进 `filter()` 会 ValueError → 500。"""
        cases = (
            self.MALFORMED_QUERY,
            {"is_active": "maybe"},
            {"page": "-1"},
            {"page": "1e999"},
            {"page_size": "0"},
            {"page_size": "999999999999999999999"},
            {"key": "x" * 5000},
            {"unknown_param": "1"},
            {"role": ""},                      # 空串按「没传」处理（`views.list` 显式过滤）
            {"role": "客服", "is_active": "true"},
        )
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get("/api/accounts/", params)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertIn("code", response.json())

    # ── 2/3/4 写载荷：超大值、畸形形状、缺字段（逐路由）────────────
    def test_malformed_write_payloads_never_500(self):
        """把每一组写载荷打到每一条能收写请求的路由上。

        路由与动作从 `ROUTE_TABLE` 现读（不是手抄清单）：新增动作时它会自动进这组，
        漏测的条件就不再是「作者记得改清单」（spec §7.1 的「契约测试要自证覆盖面」）。
        """
        executed = 0
        for path, action, method in _action_routes():
            if action in ("update", "partial_update"):
                url, payloads = self._url(path), _OVERSIZED_WRITE_PAYLOADS
            elif action == "create":
                # 新增用与编辑**同一份**载荷：`role` 为 None 之类的形状在两条路径上同形。
                url, payloads = self._url(path), _OVERSIZED_WRITE_PAYLOADS
            elif action in ("status", "reset_password", "status_batch"):
                url, payloads = self._url(path), _OVERSIZED_ACTION_PAYLOADS
            else:
                continue                       # list / retrieve / roles 由别的簇覆盖
            for payload in payloads:
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
        # 自证真的跑到了：路由表为空 / 动作全落在 `continue` 上时，上面一圈都不跑。
        self.assertGreaterEqual(executed, len(REQUIRED_ACTIONS),
                                "这一簇几乎没发出请求——路由表或动作映射坏了")

    def test_raw_malformed_json_bodies_never_500(self):
        """原始体：截断的 JSON、错误形状的 JSON、空体、二进制垃圾。

        ⚠️ 与上面那簇的区别：这里 **`data=` 直接给字符串**。DRF 的 `JSONParser`
        在解析失败时抛 `ParseError` → 400 信封（`envelope_exception_handler` 的
        「其余全部」分支）；给 dict 是构造不出这一路的。
        """
        executed = 0
        for body in _RAW_BODIES:
            for path, action, method in _action_routes():
                if action not in _WRITE_METHODS:
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
        self.assertGreaterEqual(executed, len(_RAW_BODIES),
                                "这一簇几乎没发出请求——动作映射坏了")

    # ── 5 畸形路径 id（每一个带 pk 的动作）────────────────────────
    def test_malformed_path_ids_never_500(self):
        """畸形路径 id 打在**每一个**带 `pk` 的动作上（含每个动作的正确动词）。

        `views._lookup_id_error` 的守卫覆盖 `retrieve` / `update` /
        `partial_update` / `status` / `reset_password` 五条；没有它时
        `GET /api/accounts/abc/` 回 1000、而 `POST …/abc/status/` 落到
        `get_object()` 上回 1002——**分叉**本身就是缺陷。本用例只守「都不得 500」，
        码值口径由 `tests/test_account_api.py` 的各条钉住。
        """
        detail_routes = [
            (path, action, method) for path, action, method in _action_routes()
            if "<pk>" in path
        ]
        # 自证：确实有带 pk 的路由（没有时下面的双重循环一圈都不跑）。
        self.assertTrue(detail_routes, "路由表里没有带 pk 的账号路由")
        for path, action, method in detail_routes:
            for bad_id in _MALFORMED_IDS:
                url = path.replace("<pk>", bad_id)
                with self.subTest(action=action, pk=bad_id):
                    response = getattr(self.client, method)(
                        url, {"is_active": False}, content_type="application/json"
                    )
                    self.assertNotEqual(response.status_code, 500,
                                        f"{method.upper()} {url}: {response.content[:300]}")
                    self.assertIn("code", response.json())

    def test_decimal_lookalike_path_ids_never_500(self):
        """会被 `isdecimal()` 放行的「数字样」id（`٣` / `²`）：归宿可以是 1002/1000，不得 500。

        ⚠️ 如实口径：`_lookup_id_error` 用 `str.isdecimal()`，它**放行**
        阿拉伯-印度数字（`int("٣") == 3`）——这是**已 ledger 的行为**
        （`views._lookup_id_error` 的注释），本用例不主张它是缺陷，只要求它不落到
        HTML 500。`²` 走另一条：`isdecimal()` 为假 ⇒ 1000 守卫。
        """
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
                        url, {"is_active": False}, content_type="application/json"
                    )
                    self.assertNotEqual(response.status_code, 500,
                                        f"{method.upper()} {url}: {response.content[:300]}")
                    body = response.json()
                    self.assertIn("code", body)
                    # 业务成功（0，当该样值恰好落在一行上）或业务失败（1000/1002）都合格；
                    # 403/401 会掩盖真正的守卫缺陷，所以不许出现。
                    self.assertIn(body["code"], (0, 1000, 1002), body)

    def test_routing_level_404_ids_never_500(self):
        """带 `.`（或 `/`）的路径值**匹配不到注册路由**：归宿是 Django 的 404 页，不得 500。

        ⚠️ 这条与「畸形 id」是**两个层次**的失败，混在一起会让失败信息误导读者：
        - 这里：URL 解析层（router 的 `pk` 正则是 `[^/.]+`）⇒ Django 404，**没有信封**；
        - 上面：进了视图，由 `_lookup_id_error` 守卫 ⇒ 200 + `code 1000`。

        本用例只主张「不得 500」。⚠️ **不主张 404 页的形状**——那是 Django
        在 DEBUG=False 下的默认页，与账号线无关；把它的字节形状写进断言只会
        在 Django 升级时无谓地红。
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
                        url, {"is_active": False}, content_type="application/json"
                    )
                    self.assertNotEqual(
                        response.status_code, 500,
                        f"{method.upper()} {url}: {response.content[:300]}",
                    )

    # ── 6 正向对照：这些端点对**合法**请求必须真的工作 ──────────────
    def test_valid_requests_still_succeed(self):
        """正向对照：没有它，「一律不 500」可能由「这个端点对谁都拒绝」凑出来。

        ⚠️ 本用例刻意只断言**成功**（code 0）——它是上面全部负断言的对照组。
        """
        listing = self.client.get("/api/accounts/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["code"], 0)

        roles = self.client.get("/api/accounts/roles/")
        self.assertEqual(roles.json()["code"], 0)

        created = self.client.post(
            "/api/accounts/",
            # R13：六个「必填，照抄」字段必须带齐，否则这条正向对照会以 1000 收场。
            {
                "username": "n5-created",
                "display_name": "小王",
                "role": "客服",
                "games": [self.game.pk],
                "commission_value": "10.00",
                "deposit_amount": "0.00",
                "grab_limit_dl": 0,
                "grab_limit_pw": 0,
                "hired_on": "2026-01-02",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0, created)

        patched = self.client.patch(
            f"/api/accounts/{self.target.pk}/",
            {"display_name": "改名"},
            content_type="application/json",
        ).json()
        self.assertEqual(patched["code"], 0, patched)

        disabled = self.client.post(
            f"/api/accounts/{self.target.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(disabled["code"], 0, disabled)

        batch = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.target.pk], "is_active": True},
            content_type="application/json",
        ).json()
        self.assertEqual(batch["code"], 0, batch)

        reset = self.client.post(
            f"/api/accounts/{self.target.pk}/reset-password/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(reset["code"], 0, reset)

        detail = self.client.get(f"/api/accounts/{self.target.pk}/").json()
        self.assertEqual(detail["code"], 0, detail)

    # ── 7 未认证 / 非管理员也不得 500 ──────────────────────────────
    def test_unauthorized_shapes_never_500(self):
        """匿名与非管理员：`401` / `403` 信封，不是 500（fail-closed 的守卫本身也要稳）。

        ⚠️ 本用例的每个请求都发在**正确的动词**上：发错动词会先拿到 405
        （`{"code":405}` 信封，仍然不是 500），那样这条兜底就测不到鉴权那条路。
        """
        anonymous = Client(SERVER_NAME=DOMAIN)
        outsider = make_logged_in_client(username="n5-outsider")
        for who, client in (("anonymous", anonymous), ("non-admin", outsider)):
            for path, action, method in _action_routes():
                url = self._url(path)
                with self.subTest(action=action, who=who):
                    response = getattr(client, method)(
                        url, {}, content_type="application/json"
                    )
                    self.assertNotEqual(response.status_code, 500, response.content[:300])
                    body = response.json()
                    self.assertIn("code", body)
                    # 两种身份都**不是**管理员：一律留在 401/403 的拒绝面上。
                    self.assertIn(body["code"], (401, 403), body)


def _short(payload):
    """把载荷压成一行短文本放进 subTest 名里（超长载荷会把输出刷爆）。"""
    text = repr(payload)
    return text if len(text) <= 90 else text[:87] + "..."
