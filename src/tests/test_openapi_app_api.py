"""T5：**应用管理端点**的判据（spec `docs/specs/2026-10-07-open-platform-api-design.md`
§7.3 / §5.3 / §17.5 / §17.7 / §21.3；决策 D9 / D10 / D13 / D15）。

## 本文件证什么（逐条都能失败，⛔ 不是"跑过就算"）

1. **认证与权限装配（D13 / D9）**：匿名 ⇒ **401**、登录但**没有** `system.pinduoduo_settings`
   ⇒ **403**、有这个节点 ⇒ 200。⚠️ 判据打在**行为**上（⛔ 不是读 `authentication_classes`）。
2. ⭐ **`app_secret` 是明文、随时可取**（业务方改过口径）：列表 / PATCH / 创建 / 轮换
   **都**回它，且**等于库里的值**
   （`test_the_list_shows_the_secret_from_the_database` 等）。
   ⛔ **仍然没有放松的那条**：它**不进任何留痕 / 操作日志 / 错误消息**
   ⇒ `AppSecretNeverLoggedTests` 五条写路径逐条断言（打在**数据库行**上，⛔ 不是打断言中间层）。
3. ⛔ **空集合冲突（§17.5 / §17.7 的成对判据）**：整条已随 v1 面删除
   （`AppPermissionsTests`，见下面类位置的登记块）——对拍的另一侧（应用端点）已不存在；
   角色侧自己的判据仍在 `tests/test_rbac_api.py`。
4. ⛔ **没有 `DELETE`**：判据是那几条路由回 **405**（⛔ 不是"没测"、⛔ 也不是 404——
   404 只说明路径不存在，405 才说明"路径在、方法不允许"）。`GET /apps/<pk>/`、`PUT /apps/<pk>/`
   同样是 405。⚠️ **这是「认证 + 权限过了之后」的读数**：DRF 的 405 判定排在 `initial()`
   （认证 + 权限）**之后** ⇒ **匿名 / 无节点**的 `DELETE` 回的是 **401 / 403**、⛔ 不是 405
   （本文件只测有权限的管理员那一路；匿名 / 无节点的边界另有用例，
   ⛔ 别据此把这里的 405 用例放宽）。
5. **列表要标出「未配置权限」**（§5.3 / §7.3）：空节点集的应用**调什么都 403**，
   列表上不标出来租户会以为"建好就能用"。
6. **停用 / 启用 / IP 白名单**都能改，且**空串是合法值**（清空白名单 ⇒ 不限制）。
7. **轮换**：新密钥入库、**旧密钥立即 401**、新密钥可用（§5.3 的单密钥立即失效）。
8. **纯 no-op 不留痕**（`PATCH {}` / 重发同值 / 重发同一份勾选）——全仓口径（R26），
   ⛔ 不能在日志里留一条"改了什么"的假话。
9. ⚠️ **补 `test_no_route_500` 的已知盲区**（spec §21.3）：`apps/<int:pk>/…` 四条带**路径
   转换器**，会被那张网的显式路由腿跳过 ⇒ 它们的畸形入参判据在这**用 HTTP 级断言补上**
   （含「天文数字 pk」那条——`<int:pk>` 只要求 `[0-9]+`、**不挡上界**，而实测它今天
   仍然只回 `1000`；⛔ 那条用例不是"没测"，见它自己的 docstring）。
   ⭐ 推送台账的两条（`GET apps/<int:pk>/deliveries/` 与
   `POST apps/<int:pk>/deliveries/<int:delivery_pk>/retry/`）**同样**带转换器、
   同样被跳过 ⇒ `CONVERTER_ROUTES` 现有**六条**（判据没放宽，只是覆盖面变大；
   重投那条的两个参数位在用例里都填同一个畸形值）。
   更细的一批（`delivery_pk` 非法 / `status` 非法 / `page` 非法）落在
   `tests/test_openapi_delivery_admin.py`（⛔ 不在这条腿里重复一遍）。
10. ⛔ **游戏范围两种模式**：整条已随 v1 面删除（`AppGamesTests`，见下面类位置的登记块）；
    配套「以后新增的游戏也能查到」在 `tests/test_openapi_order_api.py`，同一次删除。

## v1 面收口

- **删掉的**：两个配置 `PUT`（权限设置 / 游戏范围）与路径模板、`CONVERTER_ROUTES` 对应两项、
  三个整类与几条只谈"权限数 / 游戏范围概要"的列表用例（逐条理由在各自原位置的登记块）。
- **调整的**：`make_app` 去掉节点 / 游戏 / "全部游戏"入参；「对外凭据打内部端点 ⇒ 401」素材
  换成**真令牌**（今天唯一的对外凭据）；密钥纪律覆盖面从五条写路径收到三条；`DELETE` 405 五收三。
- ⛔ **没有放宽任何恰好集合断言**：`CONVERTER_ROUTES` 与 `urls.py` 的实际路由仍是**恰好**对拍
  （只是两边同一次少了那两项）。

## 夹具纪律

- 凭据一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的 appKey / secret 无关。
失败计数在 `OpenApiRateBucket` 上，由每条用例的事务回滚清掉。`cache.clear()` 不再承重。
"""
import json
import re

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.openapi import tokens
from apps.openapi.models import ApiApp, ApiAppState
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 应用管理的节点门（spec D9：复用现成的 `system.pinduoduo_settings`）。
APP_MANAGE_NODE = "system.pinduoduo_settings"

#: 三条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
#: ⛔ 「权限设置」「游戏范围」两条 `PUT`（`…/permissions/`、`…/games/`）随 v1 面删除（设计文档 §13.1）。
LIST_PATH = "/api/open/apps/"
DETAIL_TEMPLATE = "/api/open/apps/{pk}/"
ROTATE_TEMPLATE = "/api/open/apps/{pk}/rotate-secret/"
#: ⭐ 推送台账的两条（设计文档 §7.4 / §8）。⚠️ 重投那条有**两个**参数位
#: ⇒ 本文件拼 URL 一律 `{pk}` + `{delivery_pk}` 两个关键字一起给（`str.format` 忽略多余的关键字，
#: 所以上面四条模板用同一句也能拼出来）。
DELIVERIES_TEMPLATE = "/api/open/apps/{pk}/deliveries/"
RETRY_TEMPLATE = "/api/open/apps/{pk}/deliveries/{delivery_pk}/retry/"

#: 对外查单那条路径（用来证"改了权限/密钥**当场**生效"，⛔ 不是自说自话）。
OPEN_ORDER_TEMPLATE = "/api/open/v1/orders/{order_no}/"

#: 角色权限端点（成对判据的另一侧）。
ROLE_DETAIL_TEMPLATE = "/api/role-authorities/{pk}/"

#: 业务码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
BUSINESS = 1002

#: 认证 / 授权两档的 HTTP 码（⛔ 不合并成"没有权限"一句话）。
UNAUTHENTICATED = 401
FORBIDDEN = 403
METHOD_NOT_ALLOWED = 405
NOT_FOUND = 404

#: 合成应用名 / 合成订单号（⛔ 不是任何真实数据）。
APP_NAME = "测试应用甲"
OTHER_APP_NAME = "测试应用乙"
PROBE_ORDER_NO = "20261007000000000009"

#: 来源 IP（文档保留段，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.21"

#: 带路径转换器的**四条**（畸形入参判据由本文件补，理由见模块 docstring 第 9 条）。
#: ⚠️ 这条**恰好集合**与 `urls.py` 的实际路由是**恰好**对拍：两个配置 `PUT` 路由删除（§13.1）
#: 时它同一次少两项（判据没放宽，只是覆盖面变小）。
#: ⚠️ 台账那两条仍被 `test_no_route_500` 的显式路由腿跳过（它按 Django 自己的事实跳过带转换器的路由）。
CONVERTER_ROUTES = (
    ("patch", DETAIL_TEMPLATE),
    ("post", ROTATE_TEMPLATE),
    # ⭐ 推送台账列表（`GET`）与手工重投（`POST`）。
    ("get", DELIVERIES_TEMPLATE),
    ("post", RETRY_TEMPLATE),
)


def _all_keys(node, into=None):
    """递归收集载荷里的**全部 dict 键**（判"密钥不在载荷里"必须递归，⛔ 不是只看顶层）。"""
    into = set() if into is None else into
    if isinstance(node, dict):
        for key, value in node.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, into)
    return into


class _AppApiFixture(TenantTestCase):
    """应用管理端点的共用夹具：一个节点齐全的管理员 + 两只游戏 + 造物工具。"""

    def setUp(self):
        super().setUp()
        cache.clear()  # 见模块 docstring 的夹具纪律
        seed_catalog()
        self.domain_name = self.domain.domain
        self.game_a = Game.objects.create(name="应用管理游戏甲")
        self.game_b = Game.objects.create(name="应用管理游戏乙")
        self.admin = make_logged_in_client(
            username="openapp-admin", groups=("客服",), nodes=(APP_MANAGE_NODE,)
        )
        self.nobody = make_logged_in_client(username="openapp-nobody", groups=("代练员",))
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.creator = User.objects.get(username="openapp-admin")
        # 角色端点那一侧要一个超管（`has_node` 的既有短路；⛔ 不是为了绕开本线的判据）。
        self.superuser = make_logged_in_client(
            username="openapp-superuser", is_superuser=True
        )

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_app(self, *, name=APP_NAME, app_key="openappkey0000001",
                 app_secret="openapp-secret-000000000000000001",
                 state=ApiAppState.ENABLED, ip_allowlist=""):
        """直接经 ORM 造一个应用（端点的 POST 另有专门用例覆盖）。

        ⚠️ 应用上的 `nodes` / `games` / `all_games` 授权列已随 v1 面删除（设计文档 §13.1）；
        本文件的判据（认证 / 权限装配 / 密钥纪律 / 405 / 畸形入参）⛔ 与"应用被授权了什么"无关。
        """
        return ApiApp.objects.create(
            name=name, app_key=app_key, app_secret=app_secret,
            state=state, ip_allowlist=ip_allowlist, created_by=self.creator,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def get(self, url, *, client=None, data=None):
        return (client or self.admin).get(url, data or {})

    def post_json(self, url, payload, *, client=None):
        return (client or self.admin).post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def put_json(self, url, payload, *, client=None):
        return (client or self.admin).put(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def patch_json(self, url, payload, *, client=None):
        return (client or self.admin).patch(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def delete(self, url, *, client=None):
        return (client or self.admin).delete(url)

    def bearer_get(self, path, *, app, secret=None):
        """带 **Bearer 令牌**发一条对外查单请求（**两条**对外路径都只认令牌）。

        ⚠️ 令牌**当场签发**（`tokens.issue`）——⛔ 不抄签发 / 编码逻辑。
        `secret=` 用来构造"用**某一版**密钥签出来的令牌"（轮换那条判据的素材）。
        """
        token = tokens.issue(app.app_key, app.app_secret if secret is None else secret)
        return Client(SERVER_NAME=self.domain_name).get(
            path,
            {},
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
        )

# ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_ok(self, response):
        """成功路径：HTTP 200 + `code=0`，返回 `data`。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_business_failure(self, response, code, *, msg_fragment=None):
        """业务失败：**HTTP 200** + 指定的业务码（本仓口径：状态码只表达传输/认证层）。

        ⚠️ `code` 是承重入参（⛔ 不是装饰）：只断 msg 会让"把 1000 写成 1002"活下来。
        """
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        if msg_fragment is not None:
            self.assertIn(msg_fragment, payload["msg"], payload)
        return payload

    def logs_for(self, action):
        return OperationLog.objects.filter(action=action)


class AppAdminAccessTests(_AppApiFixture):
    """判据 1 / 4：认证与权限装配、⛔ 没有 DELETE。"""

    def test_anonymous_requests_are_401(self):
        """匿名 ⇒ **401**（⛔ 不是 403）：DRF 对"没有任何认证类受理"的请求回 `NotAuthenticated`。"""
        for url in (LIST_PATH, DETAIL_TEMPLATE.format(pk=1)):
            with self.subTest(url=url):
                response = self.get(url, client=self.anonymous)
                self.assertEqual(response.status_code, UNAUTHENTICATED,
                                 response.content[:300])
                self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_logged_in_account_without_the_node_is_403(self):
        """登录但没有 `system.pinduoduo_settings` ⇒ **403**（D9 的节点门真的在拦）。"""
        app = self.make_app()
        calls = (
            ("get", LIST_PATH),
            ("post", LIST_PATH),
            ("patch", DETAIL_TEMPLATE.format(pk=app.pk)),
            # ⛔ 两个配置 `PUT`（权限 / 游戏）路由已随 v1 面删除（§13.1）。
            ("post", ROTATE_TEMPLATE.format(pk=app.pk)),
        )
        for method, url in calls:
            with self.subTest(method=method, url=url):
                if method == "get":
                    response = self.nobody.get(url)
                else:
                    response = getattr(self.nobody, method)(
                        url, data="{}", content_type="application/json"
                    )
                self.assertEqual(response.status_code, FORBIDDEN, response.content[:300])

    def test_the_node_holder_is_let_through(self):
        """对照（能失败）：**有**这个节点的账号 ⇒ 列表 200。"""
        self.make_app()
        self.assert_ok(self.get(LIST_PATH))

    def test_a_valid_api_credential_is_not_a_session(self):
        """⭐ D13 的交叉判据：**对外那把凭据**打**内部**端点 ⇒ **401**（⛔ 两组不要混）。

        判据不打在 `authentication_classes` 上（那是"装了什么"），而打在**行为**上：
        一份**完全合法**的对外凭据，对应用管理端点**不是**身份 ⇒ 401。
        ⚠️ 反向那一半（会话打对外端点 ⇒ 401）在
        `tests/test_openapi_order_api.py::SessionRequestsAreRejectedTests`（T4）。

        ⚠️ 素材是**当场签发的真令牌**（签名面已随 v1 面整体删除，§13.1）；
        判据内核不变：**对外凭据 ≠ 内部会话**。
        """
        app = self.make_app()
        token = tokens.issue(app.app_key, app.app_secret)
        response = self.anonymous.get(
            LIST_PATH,
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertEqual(self.body(response)["data"], None)

    def test_no_delete_route_returns_405(self):
        """⛔ **没有 `DELETE`**：三条路径上，**有权限的管理员**发 `DELETE` 都回 **405**
        （两个配置 `PUT` 的路由已随 v1 面删除，见 §13.1；判据本身一字未改。
        ⛔ 不是 404、⛔ 不是没测）。

        ⚠️ 405 与 404 的区别是**判据本身**：404 只能说"没这条路由"，
        而 405 说明"路径在、这个方法不允许"——后者才是"只能停用不能删"的判据。
        ⚠️ **⛔ 别把这条读成"谁来都 405"**：DRF 的 405 判定在 `initial()`（认证 + 权限）
        **之后** ⇒ **匿名 / 无节点**的 `DELETE` 先被 **401 / 403** 挡下（那两条边界属
        匿名 / 无节点的边界另有用例；本用例只覆盖 `self.admin` 这一路）。
        """
        app = self.make_app()
        urls = (
            LIST_PATH,
            DETAIL_TEMPLATE.format(pk=app.pk),
            ROTATE_TEMPLATE.format(pk=app.pk),
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.delete(url)
                self.assertEqual(response.status_code, METHOD_NOT_ALLOWED,
                                 response.content[:300])
        self.assertTrue(ApiApp.objects.filter(pk=app.pk).exists(), "应用必须还在")

    def test_detail_get_and_put_are_405(self):
        """详情路径只定义 `PATCH` ⇒ `GET` / `PUT` 都是 405（§7.3 那一行只有 PATCH）。"""
        app = self.make_app()
        url = DETAIL_TEMPLATE.format(pk=app.pk)
        self.assertEqual(self.get(url).status_code, METHOD_NOT_ALLOWED)
        self.assertEqual(
            self.put_json(url, {"name": OTHER_APP_NAME}).status_code, METHOD_NOT_ALLOWED
        )


class AppListTests(_AppApiFixture):
    """判据 2：列表**含明文密钥**（"每行带权限数与游戏范围概要"那两组概要已随 v1 面删除，见 §13.1）。

    ⭐ 按业务方指令（「密钥改成明文展示 可以随时获取 不要加那个只能看一次的限制」），
    现在断言**列表里就有 `app_secret`、且与库里的值相等**。
    """

    def test_the_list_shows_the_secret_from_the_database(self):
        """⭐ 判据 2：列表每一行都回 `app_secret`，且**等于库里的值**（明文、随时可取）。

        ⚠️ 判据打在**值**上（`row["app_secret"] == app.app_secret`），⛔ 不是"键在不在"——
        只断键在的话，装配处回一个空串 / 别的应用的密钥都会活下来。
        """
        app = self.make_app()
        data = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(len(data), 1, data)
        self.assertIn("app_secret", _all_keys(data), data)
        self.assertEqual(data[0]["app_secret"], app.app_secret)
        # 对照：其余关键字段都在（否则"有 app_secret"可能是因为载荷整体错位）
        self.assertEqual(data[0]["app_key"], "openappkey0000001")
        self.assertEqual(data[0]["name"], APP_NAME)

    def test_every_row_carries_its_own_secret_not_the_first_one(self):
        """⭐ 两行**各回各的**密钥（装配里写死第一行 / 闭包串味会在这里露出来）。

        ⚠️ 只用一行断言的话，"所有行都回同一个密钥"这种错**不会红**——而它的后果是
        租户拿着 A 的密钥去调 B，得到 401 却看不出为什么（E-0021 同族：同一事实两套取值）。
        """
        first = self.make_app(
            name=APP_NAME, app_key="openappkey0000001",
            app_secret="openapp-secret-000000000000000001",
        )
        second = self.make_app(
            name=OTHER_APP_NAME, app_key="openappkey0000002",
            app_secret="openapp-secret-000000000000000002",
        )
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(
            {row["name"]: row["app_secret"] for row in rows},
            {APP_NAME: first.app_secret, OTHER_APP_NAME: second.app_secret},
        )
        self.assertNotEqual(first.app_secret, second.app_secret, "夹具本身要能分辨两行")
    def test_the_list_is_ordered_by_id(self):
        """顺序确定（`Meta.ordering = ["id"]`）：两次调用不该给出不同顺序。"""
        self.make_app(name=APP_NAME, app_key="openappkey0000001")
        self.make_app(name=OTHER_APP_NAME, app_key="openappkey0000002")
        names = [row["name"] for row in self.assert_ok(self.get(LIST_PATH))]
        self.assertEqual(names, [APP_NAME, OTHER_APP_NAME])

    def test_the_query_count_does_not_grow_with_the_number_of_apps(self):
        """⭐ **列表页不按行查库**：应用数变了、查询数**不跟着涨**。

        ⚠️ 判据取"**应用数变了、查询数不跟着涨**"，⛔ 不是写死一个数字（那个数字会随
        认证 / 会话 / 中间件漂移，写死必过期）。

        ⚠️ 今天行载荷**一个关联都不读**（原「两个概要各读一张 M2M 表」的形态已随 v1 面
        删除，§13.1）。⇒ 判据**留着、而且仍然有牙**：哪天有人给行载荷加回"逐行读一个关联"
        （或把预取丢了却没删读取），查询数就会随应用数增长 ⇒ 本条红。
        """
        self.make_app(name=APP_NAME, app_key="openappkey0000001")
        one = self._list_query_count()
        for index in range(4):
            self.make_app(
                name=f"测试应用{index}", app_key=f"openappkey00001{index:02d}"
            )
        five = self._list_query_count()
        self.assertEqual(one, five, "列表页的查询数随应用数增长 ⇒ 两个概要在逐行查库")

    def _list_query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assert_ok(self.get(LIST_PATH))
        return len(ctx.captured_queries)


class AppCreateTests(_AppApiFixture):
    """判据 2：创建（入参只要 `name`；响应含明文密钥，且它**一直**取得到）。"""

    def test_create_returns_the_secret_and_the_list_keeps_returning_it(self):
        """⭐ **改准（2026-10-07）**：创建响应的密钥与库里一致，且**之后列表里照样有它**。

        原用例名 `test_create_returns_the_secret_exactly_once`，末句断言「随后列表里就没有它了」
        ——那就是被业务方作废的「只能看一次」这条限制。现在**反过来**断：列表里**有**、且**等值**。
        """
        data = self.assert_ok(self.post_json(LIST_PATH, {"name": APP_NAME}))
        app = ApiApp.objects.get(name=APP_NAME)
        self.assertEqual(data["app_secret"], app.app_secret)
        self.assertEqual(data["app_key"], app.app_key)
        # ⚠️ 密钥形状也钉住：16 位小写 hex 的 AppKey、URL-safe 的 43 字符 AppSecret（§5.3）。
        self.assertRegex(app.app_key, re.compile(r"^[0-9a-f]{16}$"))
        self.assertRegex(app.app_secret, re.compile(r"^[A-Za-z0-9_-]{43}$"))
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(rows[0]["app_secret"], app.app_secret)

    def test_create_defaults_state_enabled(self):
        """新建 ⇒ 启用、创建人就是当前登录账号。

        ⚠️ 「零权限、零游戏」两句随节点 / 游戏 M2M 删除（§13.1）同一次去掉；剩下两条**一字未改**。
        """
        self.assert_ok(self.post_json(LIST_PATH, {"name": APP_NAME}))
        app = ApiApp.objects.get(name=APP_NAME)
        self.assertEqual(app.state, ApiAppState.ENABLED)
        self.assertEqual(app.created_by, self.creator)

    def test_create_only_needs_the_name(self):
        """入参**只要 `name`**：多给的键被忽略（⛔ 不能悄悄设置被忽略的字段）。"""
        data = self.assert_ok(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "state": "disabled", "app_secret": "attacker-value"},
            )
        )
        app = ApiApp.objects.get(name=APP_NAME)
        self.assertEqual(app.state, ApiAppState.ENABLED, "多余键不该生效")
        self.assertNotEqual(app.app_secret, "attacker-value", "⛔ 密钥不许由调用方指定")
        self.assertEqual(data["state"], ApiAppState.ENABLED)

    def test_create_rejects_a_missing_or_blank_name(self):
        for payload in ({}, {"name": ""}, {"name": "   "}):
            with self.subTest(payload=payload):
                self.assert_business_failure(
                    self.post_json(LIST_PATH, payload), VALIDATION
                )
        self.assertEqual(ApiApp.objects.count(), 0)

    def test_create_rejects_a_too_long_name(self):
        self.assert_business_failure(
            self.post_json(LIST_PATH, {"name": "x" * 51}), VALIDATION
        )
        self.assertEqual(ApiApp.objects.count(), 0)

    def test_create_rejects_a_duplicate_name(self):
        self.make_app()
        self.assert_business_failure(
            self.post_json(LIST_PATH, {"name": APP_NAME}), VALIDATION,
            msg_fragment="应用名已存在",
        )
        self.assertEqual(ApiApp.objects.count(), 1)

    def test_create_writes_an_audit_row_with_the_key_but_never_the_secret(self):
        """留痕：`system.open_api_app_create` + 应用名 + **app_key**（非机密），⛔ 无密钥。"""
        data = self.assert_ok(self.post_json(LIST_PATH, {"name": APP_NAME}))
        rows = list(self.logs_for("system.open_api_app_create"))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.actor, "openapp-admin")
        self.assertEqual(row.actor_id, self.creator.pk)
        self.assertEqual(row.target_kind, "api_app")
        self.assertEqual(row.target_name, APP_NAME)
        self.assertEqual(row.to_value, data["app_key"])
        self.assertIn(APP_NAME, row.summary)
        self.assertIn(data["app_key"], row.summary)
        self.assertNotIn(data["app_secret"], row.summary)


class AppPatchTests(_AppApiFixture):
    """§7.3 的 PATCH：改名 / 停用启用 / 改 IP 白名单（+ 纯 no-op 不留痕）。"""

    def test_patch_renames(self):
        app = self.make_app()
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"name": OTHER_APP_NAME})
        )
        app.refresh_from_db()
        self.assertEqual(app.name, OTHER_APP_NAME)
        self.assertEqual(data["name"], OTHER_APP_NAME)

    def test_patch_disables_and_re_enables(self):
        app = self.make_app()
        url = DETAIL_TEMPLATE.format(pk=app.pk)
        data = self.assert_ok(self.patch_json(url, {"state": "disabled"}))
        app.refresh_from_db()
        self.assertEqual(app.state, ApiAppState.DISABLED)
        self.assertEqual(data["state_text"], "停用")
        self.assert_ok(self.patch_json(url, {"state": "enabled"}))
        app.refresh_from_db()
        self.assertEqual(app.state, ApiAppState.ENABLED)

    def test_patch_sets_and_clears_the_ip_allowlist(self):
        """⚠️ **空串是合法值**（空 = 不限制）⇒ 清空必须生效（真值判断会静默漏掉这一支）。"""
        app = self.make_app(ip_allowlist="203.0.113.7")
        url = DETAIL_TEMPLATE.format(pk=app.pk)
        self.assert_ok(self.patch_json(url, {"ip_allowlist": "203.0.113.7, 203.0.113.8"}))
        app.refresh_from_db()
        self.assertEqual(app.ip_allowlist, "203.0.113.7, 203.0.113.8")
        self.assert_ok(self.patch_json(url, {"ip_allowlist": ""}))
        app.refresh_from_db()
        self.assertEqual(app.ip_allowlist, "")

    def test_patch_rejects_an_unknown_state(self):
        app = self.make_app()
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "BOGUS"}),
            VALIDATION,
        )
        app.refresh_from_db()
        self.assertEqual(app.state, ApiAppState.ENABLED)

    def test_patch_rejects_a_duplicate_name(self):
        self.make_app()
        other = self.make_app(name=OTHER_APP_NAME, app_key="openappkey0000002")
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=other.pk), {"name": APP_NAME}),
            VALIDATION,
            msg_fragment="应用名已存在",
        )
        other.refresh_from_db()
        self.assertEqual(other.name, OTHER_APP_NAME)

    def test_patch_unknown_pk_is_a_business_error(self):
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=999999), {"name": OTHER_APP_NAME}),
            VALIDATION,
            msg_fragment="应用不存在",
        )

    def _send_converter_route(self, method, url):
        """按**声明的 HTTP 方法**给这条带转换器的路由发一发（⛔ 不一律用 PATCH / PUT）。

        ⚠️ 这张表里现在是 `patch` / `post` / `get` 三种方法（`put` 那一支随两个配置
        `PUT` 的路由一起删除，§13.1）；各条的请求逐字不变，判据一个字没改：
        下面两条腿仍然只断言「非 500 + 业务码」与「畸形 pk ⇒ 404」。
        """
        if method == "get":
            return self.get(url)
        if method == "patch":
            return self.patch_json(url, {"name": OTHER_APP_NAME})
        return self.post_json(url, {})

    def test_huge_pk_is_a_business_error_not_a_500(self):
        """⭐ 判据取**输入形状**：`<int:pk>` **不挡上界**，而这个天文数字仍然只回 `1000`。

        `path("apps/<int:pk>/")` 的转换器只要求 `[0-9]+`（`django/urls/converters.py`
        的 `IntConverter.regex`）⇒ 这个数字会**匹配路由**并进 `filter(pk=…)`。
        ⚠️ **实测（单条变异）**：它**不会** 500——
        psycopg 把超 `int8` 的整数按 `numeric` 送、PostgreSQL 比较后返回 0 行 ⇒ 走
        "查不到"那一支。所以 `app_views._get_app` **刻意没有**"pk 上界"守卫
        （`AGENTS.md` §5-E：去掉它没有任何用例会红 ⇒ 它是防御性写法，⛔ 不该被写成承重逻辑）。
        ⛔ 本用例**不是**"没测"：它把"这类请求不许 500"这条**输入形状**钉住——
        驱动 / 版本变化让它变成 500 时它会红。

        ⭐ 台账两条也在这张表里（重投那条的两个参数位都填这个天文数字；
        ⚠️ 它**先**在 `_get_app` 上失败 ⇒ 期望码仍是 `1000`，与其余五条同一句判据）。
        """
        huge = 10**25
        for method, template in CONVERTER_ROUTES:
            with self.subTest(method=method, template=template):
                url = template.format(pk=huge, delivery_pk=huge)
                response = self._send_converter_route(method, url)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assert_business_failure(response, VALIDATION)

    def test_non_numeric_pk_is_404(self):
        """畸形 pk **不匹配路由** ⇒ 404（⛔ 不是 500、也不进视图）。"""
        for method, template in CONVERTER_ROUTES:
            with self.subTest(method=method, template=template):
                # ⚠️ 两个参数位都给 `"abc"`：重投那条只要**任一**位不是数字就不匹配路由。
                url = template.format(pk="abc", delivery_pk="abc")
                response = self._send_converter_route(method, url)
                self.assertEqual(response.status_code, NOT_FOUND, response.content[:300])


class AppPatchAuditTests(_AppApiFixture):
    """留痕：每个**真正变化**的字段一条，纯 no-op 一条都不写（判据 8）。"""

    def test_one_row_per_changed_field(self):
        app = self.make_app(ip_allowlist="203.0.113.7")
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": OTHER_APP_NAME, "state": "disabled", "ip_allowlist": ""},
            )
        )
        rows = list(self.logs_for("system.open_api_app_update"))
        self.assertEqual(len(rows), 3, [r.summary for r in rows])
        self.assertEqual(
            sorted(row.field_name for row in rows), ["ip_allowlist", "name", "state"]
        )
        by_field = {row.field_name: row for row in rows}
        self.assertEqual(by_field["name"].from_value, APP_NAME)
        self.assertEqual(by_field["name"].to_value, OTHER_APP_NAME)
        self.assertEqual(by_field["state"].from_value, "启用")
        self.assertEqual(by_field["state"].to_value, "停用")
        self.assertEqual(by_field["ip_allowlist"].from_value, "203.0.113.7")
        self.assertEqual(by_field["ip_allowlist"].to_value, "空")
        # 中文标签由 `FIELD_LABELS` 派生（R8/R9）⇒ 句子里不该出现裸列名
        self.assertIn("应用名：测试应用甲 → 测试应用乙", by_field["name"].summary)
        self.assertIn("状态：启用 → 停用", by_field["state"].summary)

    def test_an_empty_patch_writes_nothing(self):
        app = self.make_app()
        self.assert_ok(self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {}))
        self.assertEqual(self.logs_for("system.open_api_app_update").count(), 0)

    def test_re_sending_the_same_values_writes_nothing(self):
        """重发同值 = 没发生任何事 ⇒ ⛔ 不留一条"改了什么"的假话（R26 口径）。"""
        app = self.make_app(ip_allowlist="203.0.113.7")
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": APP_NAME, "state": "enabled", "ip_allowlist": "203.0.113.7"},
            )
        )
        self.assertEqual(self.logs_for("system.open_api_app_update").count(), 0)


# ⛔ 此处原有 `AppPermissionsTests`（钉 `PUT …/permissions/` 的节点集合整份替换、与角色侧的
# 成对判据），被测端点随 v1 面删除（设计文档 §13.1）——应用凭据 = 本租户全部业务数据，
# 「应用能持有权限节点」已不存在。如实登记丢掉的判别力：「两条路只差一个 `allow_empty`」
# 失去对拍对象；`resolve_node_codes(allow_empty=True)` 失去唯一消费者。旧语义见 git 历史与 §13.1。


# ⛔ 此处原有 `AppGamesTests`（钉 `PUT …/games/` 与"全部游戏"模式：入参二选一、落库、
# 清空 M2M、留痕模式），被测端点随 v1 面删除（设计文档 §13.1）——应用恒等于
# "本租户全部业务数据"，没有可收窄的授权面。丢掉的判别力：「改了授权面当场生效」的
# 端到端对照没了（凭据面由 `tests/test_openapi_token.py` / `test_openapi_order_api.py` 各自钉住）。


# ⛔ 此处原有 `AppRowPayloadPreloadTests`（钉行载荷里两个「当前勾选态回读」清单的逐字排序 /
# 空集 / 整份替换），被测端点随 v1 面删除（设计文档 §13.1）——它不是被"放宽"：读的键不回、
# 打的端点不存在。教训仍有效：「整份替换 + 读不回旧值 = 静默降权」（`AGENTS.md` §5-E /
# E-0021 家族）；将来再出现整份替换类端点，判据照这个类的形状重建。


class AppRotateSecretTests(_AppApiFixture):
    """§5.3 的轮换：新密钥入库、**旧密钥立即失效**、⛔ 密钥不进留痕。"""

    def test_rotate_returns_a_new_secret_and_rotates_the_timestamp(self):
        app = self.make_app()
        old_secret = app.app_secret
        self.assertIsNone(app.secret_rotated_at)
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        app.refresh_from_db()
        self.assertNotEqual(app.app_secret, old_secret)
        self.assertEqual(data["app_secret"], app.app_secret)
        self.assertIsNotNone(app.secret_rotated_at)

    def test_the_old_secret_dies_immediately_and_the_new_one_works(self):
        """⭐ 判据 7：旧密钥**当场** 401、新密钥可用（单密钥、没有宽限期）。"""
        app = self.make_app()
        path = OPEN_ORDER_TEMPLATE.format(order_no=PROBE_ORDER_NO)
        old_secret = app.app_secret
        self.assertEqual(self.bearer_get(path, app=app).status_code, 200, "轮换前应可用")
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        old_response = self.bearer_get(path, app=app, secret=old_secret)
        self.assertEqual(old_response.status_code, UNAUTHENTICATED,
                         old_response.content[:300])
        new_response = self.bearer_get(path, app=app, secret=data["app_secret"])
        self.assertEqual(new_response.status_code, 200, new_response.content[:300])

    def test_the_rotate_audit_row_never_carries_a_secret(self):
        """⛔ 密钥不进留痕：新旧密钥、以及库里当前那一把，都不许出现在任何一列。"""
        app = self.make_app()
        old_secret = app.app_secret
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        rows = list(self.logs_for("system.open_api_app_rotate_secret"))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertNotIn(old_secret, row.summary)
        self.assertNotIn(data["app_secret"], row.summary)
        self.assertIn(APP_NAME, row.summary)

    def test_the_list_shows_the_new_secret_after_a_rotation(self):
        """⭐ 轮换后列表里给的是**新**密钥（明文口径）。

        ⚠️ 判据必须**同时**钉两条：列表 = 新密钥 **且** ≠ 旧密钥——只断"等于库里的值"
        时，若轮换压根没换成功，这条也会绿（库和列表一起停在旧值上）。
        """
        app = self.make_app()
        old_secret = app.app_secret
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(rows[0]["app_secret"], data["app_secret"])
        self.assertNotEqual(rows[0]["app_secret"], old_secret)

    def test_rotating_an_unknown_app_is_a_business_error(self):
        self.assert_business_failure(
            self.post_json(ROTATE_TEMPLATE.format(pk=999999), {}),
            VALIDATION,
            msg_fragment="应用不存在",
        )


class AppSecretNeverLoggedTests(_AppApiFixture):
    """⭐ **改口径之后仍然没有放松的那一条**：`app_secret` 绝不进**留痕 / 操作日志 / 错误消息**。

    ## 为什么这一条在本线里最重要

    业务方已把「密钥只在创建 / 轮换那一次显示」改成「**明文展示、随时可以获取**」
    （spec 的 **§34**）⇒ 原来那条"接口不回显"的纪律**整条**没了。
    **唯一没跟着松的就是这一条**：密钥可以出现在**响应体**里（那是业务方要的），
    ⛔ 但**不许**落到任何留痕 / 操作日志 / 错误消息里 —— 那些地方是**长期**留存、
    会被别的页面读出来、还会进排障会话与备份。

    ## 判据为什么打在「数据库行」上（⛔ 不是 `assertLogs` / 不是读模板常量）

    `AGENTS.md` §5-G 的硬口径：**断言副作用要打在真实出口上**——`logger.info()` 是中间层，
    而 `assertLogs` 会自己把目标 logger 的级别抬到 INFO ⇒「生产一行都没写」的测试**全绿**
    （E-0009 / E-0024）。本仓真正留存的是 `apps.audit` 的两张表（`OperationLog`），
    ⇒ 这里逐条翻**落库的那些行**、把**每一个字符串列**拼起来找密钥。
    更严的一层：`_secret_columns_hit` 让**数据库**对**表里的每一列**做一次
    `CAST(col AS text) = 密钥` ⇒ ⛔ 不依赖「哪几列可能装文本」这个假设——将来给
    `OperationLog` 加一个新的文本列，这条判据**自动**把它盖进去（⛔ 不用改测试）。

    ## 覆盖面（为什么是这几条）

    应用管理有**三条写路径**、每条的留痕文案模板都不一样（`apps/audit/actions.py` 那三句）
    ⇒ 逐条打一遍。**纯 no-op** 的那两条也打（它们不写库，但"没写"本身是判据：
    若将来有人给 no-op 也补留痕，这条会红）。
    ⚠️ 覆盖面是**三条写路径**（权限设置与游戏范围那两条已随 v1 面删除，§13.1）。
    **判据一条未放宽**：仍然是"每个真正写了留痕的写路径都逐条走一遍、密钥不许出现在任何一列"。
    ⚠️ 创建那条**故意**让服务端自己生成密钥（⛔ 不用 `make_app` 的固定值）：
    固定值可能与别的文本撞形，生成值才是"真的从这一行走出去的那把"。
    """

    def _rows_text(self, model):
        """把一张表的**每一行**的**每一个字符串列**拼成一段文本（⛔ 不挑列）。"""
        chunks = []
        for row in model.objects.all():
            for field in model._meta.concrete_fields:
                value = getattr(row, field.attname, None)
                if value is not None:
                    chunks.append(str(value))
        return "\n".join(chunks)

    def _secret_columns_hit(self, model, secret):
        """⭐ 第二层：让**数据库自己**逐列扫一遍（连截断后的残留也算命中）。

        ⚠️ **为什么不是 `col::text LIKE '%…%'`**：`LIKE` 的通配位（`%` / `_`）必须手工转义，
        而 `OperationLog.action` 里的动作码**本身就带 `_`**（`system.open_api_app_create`）
        ⇒ 转义写错就得到一条**恒真的判据**（E-0083 同族）。这里改用 **`CAST(col AS text) = 值`
        的精确相等**：没有通配位可转义，而且 `extra(where=…)` 走的仍是**数据库**那一侧
        （⛔ 不是把行拉进 Python 再比）。
        返回 (`字段名`, 命中行数) 的清单；空清单 = 一列都没命中。
        """
        hits = []
        table = model._meta.db_table
        for field in model._meta.concrete_fields:
            # ⚠️ 表名 / 列名**全部**来自 Django 自己的元数据（`_meta`），⛔ 没有请求来源的字符串
            #    参与拼 SQL；**只有密钥**走 `%s` 参数绑定。
            count = model.objects.extra(
                where=[f'CAST("{table}"."{field.column}" AS text) = %s'], params=[secret]
            ).count()
            if count:
                hits.append((field.name, count))
        return hits

    def _assert_no_secret_anywhere(self, secrets, actions):
        """三条一起断：① 逐列的 `LIKE` ② `OperationLog` 全行拼接 ③ 那几条动作码都真写了行。"""
        for action in actions:
            self.assertTrue(
                self.logs_for(action).exists(),
                f"{action} 一条留痕都没有 ⇒ 本用例的覆盖面是假的（先修埋点）",
            )
        for secret in secrets:
            self.assertEqual(
                self._secret_columns_hit(OperationLog, secret), [],
                "留痕里有某一列出现了密钥（数据库侧逐列 CAST … = 密钥 扫出来的）",
            )
            text = self._rows_text(OperationLog)
            self.assertNotIn(secret, text, "留痕的某个字符串列里出现了密钥")

    def test_no_secret_ever_reaches_the_audit_trail(self):
        """⭐ 三条写路径走一遍 ⇒ 新旧密钥都不在任何留痕列里（含"曾经的那把"）。"""
        created = self.assert_ok(self.post_json(LIST_PATH, {"name": APP_NAME}))
        app = ApiApp.objects.get(name=APP_NAME)
        first_secret = created["app_secret"]
        self.assertEqual(first_secret, app.app_secret, "对照：创建响应里的就是库里那把")

        # 另外造一只**固定密钥**的应用，用来覆盖 PATCH 那条路的留痕。
        # ⛔ 两个配置 `PUT`（权限 / 游戏）各自的留痕路径已随 v1 面删除（§13.1）。
        other = self.make_app(name=OTHER_APP_NAME, app_secret="openapp-secret-000000000000000002")
        self.patch_json(
            DETAIL_TEMPLATE.format(pk=other.pk), {"name": "改了名的应用", "state": "disabled"}
        )
        rotated = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        app.refresh_from_db()

        self._assert_no_secret_anywhere(
            secrets=(first_secret, rotated["app_secret"], app.app_secret,
                     "openapp-secret-000000000000000002"),
            actions=(
                "system.open_api_app_create",
                "system.open_api_app_update",
                "system.open_api_app_rotate_secret",
            ),
        )

    def test_the_pure_no_ops_write_nothing_at_all(self):
        """纯 no-op（`PATCH {}` / 重发同值）**一行都不写** ⇒ 也就无从泄密。

        ⚠️ 这条是**对照**：上面那条证明"写了的内容里没有密钥"，这条证明"该不写的地方真的没写"
        （R26 的全仓口径）。两条一起才排除"no-op 也补了一条带密钥的留痕"。
        """
        app = self.make_app()
        before = OperationLog.objects.count()
        self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {})
        self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"ip_allowlist": app.ip_allowlist})
        self.assertEqual(OperationLog.objects.count(), before, "no-op 不该写留痕")
        self.assertNotIn(app.app_secret, self._rows_text(OperationLog))


class AppMalformedInputTests(_AppApiFixture):
    """补 `test_no_route_500` 的跳过面（spec §21.3）：带转换器的四条 × 畸形入参 ⇒ 不是 500。"""

    def test_malformed_bodies_are_business_errors_not_500(self):
        """畸形入参 ⇒ **业务码 1000**（⛔ 不是 500、⛔ 也不是静默接受）。

        ⚠️ **`code` 键永远在**（`api_response()` 的信封恒有它）⇒ 只断 `"code" in body`
        是**恒真**判据（E-0063 同族）：实现被改成"畸形入参**静默接受**（`code=0`）"时，
        那种断言**照样绿**。所以每一格都断**那个码本身**（字面值，E-0024）。
        ⚠️ **一格例外、如实登记**：`rotate-secret` 那个端点**不读请求体**（§7.3 它没有入参）
        ⇒ 传什么它都成功（`code=0`）。那一格在这里证的是「多余的 body **不 500**、
        也**不被误判成 1000**」⇒ 期望值写 `OK`（⛔ 不是把整条判据放宽成"非 0 即可"：
        另外四格仍然是字面 `1000`）。
        """
        app = self.make_app()
        cases = (
            ("patch", DETAIL_TEMPLATE, {"name": "x" * 500}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"state": "BOGUS"}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"ip_allowlist": ["not", "a", "string"]}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"ip_allowlist": "x" * 5000}, VALIDATION),
            # ⛔ 原表里打两个配置 `PUT` 的五格畸形入参随路由删除（§13.1）同一次去掉。
            ("post", ROTATE_TEMPLATE, {"anything": "ignored"}, OK),
        )
        for method, template, payload, expected_code in cases:
            with self.subTest(method=method, payload=str(payload)[:40]):
                url = template.format(pk=app.pk)
                response = (
                    self.patch_json(url, payload)
                    if method == "patch"
                    else self.put_json(url, payload)
                    if method == "put"
                    else self.post_json(url, payload)
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(
                    self.body(response)["code"], expected_code, response.content[:300]
                )

    def test_a_malformed_body_on_the_collection_is_not_500(self):
        """创建端点的畸形 body ⇒ 业务码 **1000**（⛔ 不是 500、⛔ 也不是静默建出一行）。

        ⚠️ 只断「HTTP 200」是**恒真**判据（E-0063：`api_response()` 的信封永远有 `code`）
        ⇒ 这里逐格断**那个码本身**（字面值，E-0024）。

        ⚠️ **`{"name": 12345}` 那一格是实测确认的**（不是推断）：DRF 的
        `CharField.to_internal_value` 对 `int` / `float` **不报错**，而是 `str(data)`
        ⇒ `12345` **不是**畸形入参，它会**真的建出一行** `name="12345"`（业务码 `0`，
        实测：`{"code":0,…,"data":{"id":1,"name":"12345",…}}`）。这一格如实钉住那条行为；
        ⛔ 不是把判据放宽成"非 0 即可"——另外两格仍是字面 `1000`。
        """
        for payload, expected_code in (
            ({"name": []}, VALIDATION),
            ({"name": {"a": 1}}, VALIDATION),
            # DRF 把 int 转成字符串 ⇒ 合法入参（实测建出 name="12345"）。
            # ⚠️ 这是 DRF 的**既有、有文档的强制转换**（`CharField.to_internal_value` 对
            #    `int` / `float` 走 `str(data)`），本线**有意接受**（E-0010：过了序列化器、
            #    不 500、写进 `CharField`；租户在列表里看得见这一行，可改名 / 停用）
            #    ⇒ ⛔ 别把它当缺陷来"修"（要拒得另上校验，属别的线）。
            ({"name": 12345}, OK),
        ):
            with self.subTest(payload=payload):
                response = self.post_json(LIST_PATH, payload)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(
                    self.body(response)["code"], expected_code, response.content[:300]
                )
