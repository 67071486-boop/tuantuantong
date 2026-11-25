"""T3：打手侧**内部**应用管理端点 + **身份限权**的判据（spec
`docs/specs/2026-10-08-taker-open-api-design.md` §3.1 / §6 / §9-10 / §9-11）。

## 本文件证什么（逐条都能失败，⛔ 不是"跑过就算"）

1. **认证与权限装配**：匿名 ⇒ **401**、登录但**没有**门里那两个节点 ⇒ **403**、
   持节点 ⇒ 200；⭐ 交叉判据：**合法的打手侧 Bearer 令牌**打本组端点 ⇒ **401**
   （spec §9-2 的"三组不混"，⛔ 不是靠"每条端点都记得过滤"）。
   ⚠️ 判据打在**行为**上（⛔ 不是读 `authentication_classes`）。
2. **`app_secret` 明文可随时获取**（与上家侧 `D-0201` 同口径）：列表 / 创建 / `PATCH` /
   轮换**都**回它，且**等于库里的值**；⭐ 行载荷**恰好**那 12 个键（字面量写在下面，
   ⛔ 不从常量 import —— 那样常量被改坏时本条不会红）。
3. **轮换 / 停用秒级生效**：轮换后**旧令牌立刻 401、新令牌可用**、停用后**秒级 401**。
   ⚠️ **令牌真从 `/api/taker/oauth/token` 换**（走真路由 + 真 `Client`，⛔ 不手搓）；
   令牌的**可用性**打在**真认证类**上（探针视图，理由见下面那一节）。
4. ⛔ **没有 `DELETE`**：那三条路由回 **405**（⛔ 不是"没测"、⛔ 也不是 404 ——
   404 只说明路径不存在，405 才说明"路径在、方法不允许"）。
   ⚠️ **准确表述：405 是「认证 + 权限都过了之后」的读数**——DRF 的 405 判定排在 `initial()`
   **之后** ⇒ **匿名 / 无节点**的 `DELETE` 会先被 **401 / 403** 挡下。三条边界都在本文件里钉住
   （⛔ 不是嘴上说说）。
5. ⭐ **身份限权**（spec §6 / §9-10）：打手身份建应用时 `taker` **锁死为他自己**（载荷里带
   别人的 id ⇒ **落成自己**这一支 + **反例**"结果里那条应用的 `taker` 绝不等于载荷里那个别人"）；
   管理员建时**必须选**一个打手（缺 / 非法 / 查不到 ⇒ `1000` + 点名）；⭐ **打手看不到、
   改不了别人的**（spec §6 那一行）——列表只回绑给自己的、`PATCH` / 轮换别人的 ⇒ `1000`。
6. **载荷白名单 fail-closed**：表外的键 ⇒ `1000` + **点名那几个键**、**一行都不落库**；
   `""` / `null` 值的**未知键照拒**；**合法可写字段 ⇒ 真变**（正对照，⛔ 不是"什么都拒"）。
7. ⛔ **`app_secret` 不进任何留痕 / 操作日志 / 错误消息**：判据打在**数据库行**与**响应体**上
   （⛔ 不靠 `assertLogs` 那种可被抬高 level 的中间层，`E-0009` / `E-0024`）。

## ⚠️ 令牌的"可用性"为什么用**探针视图**（⛔ 不是建第二个 urls 模块）

打手侧**对外**端点（`/api/taker/v1/…`）属 T5 / T6，今天**不存在**。为了测"旧令牌立刻 401"
就去建一个 `urls` 模块并挂进 `config/urls.py`，会**顺手**撞上一张承重网
（`tests/test_operation_log_coverage.py` 按 `get_resolver()` 枚举真实 urlconf）。
⇒ 探针视图定义在**本文件内**，装配方式（`authentication_classes` + 视图里碰
`request._api_app` 触发惰性认证）与真端点**逐字相同**，与 `tests/test_takerapi_auth.py` 同一条手法。
⚠️ **证明力边界（如实登记）**：它证的是**认证器**（凭据族）本身，⛔ **不证**"某条真端点有没有
挂对这个认证器"——那一半属 T5 / T6（T2b 评审第 6 条已把"打真路由"版列为 T5 的交付项）。

## ⚠️ 为什么夹具会**手工造** T4 那条权限点

`system.allow_taker_open_api` 由 **T4** 落进权限目录（`apps/rbac/catalog.py`）。在此之前
`make_logged_in_client(nodes=…)` **挂不上**它（那一步按 code 查 `PermissionNode` 表）。
⇒ 本夹具用 `get_or_create(code=…)` **手工造那一行**再挂给账号。
⚠️ 这样做的**正当性**：节点判据读的是**节点表**（`apps/rbac/permissions.py` 的
`user_node_codes` 查 `PermissionNode`），⛔ 不是目录常量 ⇒ 手工造行与 T4 落目录之后的行为
**逐字一致**；而 `get_or_create` 让 T4 落地之后本夹具仍然可用（那时它只是取到已有的那一行）。
⚠️ 而"**今天**除管理员外没人持有点 ⇒ 403"这条**如实**由
`TakerAppAdminAccessTests::test_a_logged_in_account_without_the_node_is_403` 钉住。

## 夹具纪律

- 凭据一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的 appKey / secret 无关。
- 来源 IP 取文档保留段（`203.0.113.0/24`），⛔ 不是任何真实客户端。
- 断言打在**数据库行** / **HTTP 响应**这些真实出口上；业务码与 key 集写**字面量**（`E-0024`）。
"""
import json
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.core.envelope import rendered_response
from apps.core.permissions import is_tenant_admin
from apps.openapi import outbound as openapi_outbound
from apps.openapi import serializers as openapi_serializers
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode
from apps.rbac.seed import seed_catalog
from apps.takerapi.api import app_views as taker_app_views
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.models import AppState, TakerApp
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 节点 code（**字面量**，⛔ 不从 `apps.takerapi.api.app_views` import：
#    那条常量被改坏时，从它派生的断言**不会红**，`E-0024`）──────────────────────────
SYSTEM_ADMIN_NODE = "system.pinduoduo_settings"
TAKER_IDENTITY_NODE = "system.allow_taker_open_api"

# ── 三条路径（与 `apps/takerapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）──────────────
LIST_PATH = "/api/taker/apps/"
DETAIL_TEMPLATE = "/api/taker/apps/{pk}/"
ROTATE_TEMPLATE = "/api/taker/apps/{pk}/rotate-secret/"

#: 取令牌端点（spec §2 的契约逐字：**不带尾斜杠**）。令牌一律从这里换。
TOKEN_PATH = "/api/taker/oauth/token"

#: 令牌交换的固定入参（与 `apps/openapi` 那条端点同款，spec §2）。
GRANT_TYPE = "client_credentials"

#: ⭐ 行载荷**恰好**这 14 个键（字面量写在这里是**有意**的：⛔ 不从 `app_row_payload`
#: 或任何常量派生的集合 —— 那样函数被改坏时本条不会红，`E-0024`）。
#: 与上家侧形状的两处差别：多了 `taker` / `taker_name`（绑定打手账号，spec §3.1）。
#: ⛔⭐ 台账键集合（回调配置两键在内）：
#: > 行载荷**恰好**这 12 个键 …… 与上家侧形状的两处差别：多了 `taker` / `taker_name`
#: > （绑定打手账号，spec §3.1），少了回调配置（第一版无推送，spec §3.1 明确⛔ 不放
#: > `callback_url`）。
#: 第二期（`docs/specs/2026-10-09-taker-callback-design.md` §7 第四行）把回调配置两键
#: **加上了**（⛔ 但**不加** `callback_state_text`，照上家侧那一格的口径）⇒ 今天**恰好 14 个**。
#: ⚠️ **这不是放宽断言**：仍然是与函数返回集合的**恰好相等**（多一个 / 少一个都红），
#: 只是"契约集合"本身按 spec 长了两个键。判据的第二个出处见
#: `tests/test_takerapi_callback_api.py::CallbackConfigPatchTests::test_the_row_payload_carries_exactly_the_fourteen_keys`。
ROW_KEYS = {
    "id",
    "name",
    "app_key",
    "app_secret",
    "taker",
    "taker_name",
    "state",
    "state_text",
    "ip_allowlist",
    "callback_url",
    "callback_state",
    "last_used_at",
    "secret_rotated_at",
    "created_at",
}

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字，让"码值被改掉"在用例里可见）──────
OK = 0
VALIDATION = 1000
UNAUTHENTICATED = 401
FORBIDDEN = 403
METHOD_NOT_ALLOWED = 405

# ── 合成夹具值（⛔ 不是任何真实数据）──────────────────────────────────────────
ADMIN_USERNAME = "takerapp-admin"
TAKER_USERNAME = "takerapp-taker"
#: 第二个打手：用来证「打手 A 改不动打手 B 那条应用」（两个同类调用方之间的越权）。
TAKER_B_USERNAME = "takerapp-taker-b"
OTHER_USERNAME = "takerapp-other"
TAKER_ADMIN_USERNAME = "takerapp-tenant-admin"
NOBODY_USERNAME = "takerapp-nobody"
PASSWORD = "pw-123456"

APP_NAME = "测试打手应用甲"
OTHER_APP_NAME = "测试打手应用乙"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.31"

# ── ⭐ T5b：回调配置那三格用的合成地址（⛔ 不是任何真实第三方）──────────────────
#: 公网那一档用**地址字面量**：地址校验只做 `getaddrinfo` / 字面量判断
#: ⇒ 本文件**一个真实 HTTP 都不发**（与 `tests/test_takerapi_callback_api.py` 同一条纪律）。
CALLBACK_PUBLIC_HOST = "93.184.216.34"
CALLBACK_URL = f"https://{CALLBACK_PUBLIC_HOST}/taker/hook"
#: 必须被拒的两类（各走 `outbound` 里一条不同的判据）：非 https / 私网。
CALLBACK_HTTP_URL = f"http://{CALLBACK_PUBLIC_HOST}/taker/hook"
CALLBACK_PRIVATE_URL = "https://10.11.12.13/taker/hook"


# ═════════════════════════════════════════════════════════════════════════════
# 探针视图：**打手侧对外认证类**的装配（与真端点逐字相同的手法，见模块 docstring）
# ═════════════════════════════════════════════════════════════════════════════
@api_view(["GET"])
@authentication_classes([TakerAppBearerAuthentication])
@permission_classes([AllowAny])
def taker_credential_probe(request):
    """回显"打手侧认证把哪一条凭据挂到了请求上"。

    ⚠️ 先碰 `request._api_app`：DRF 的认证是**惰性**的，视图不碰它就不会跑
    （那样本探针会恒 200、零判别力）。
    """
    app = getattr(request, "_api_app", None)
    return Response({"app_key": None if app is None else app.app_key})


TAKER_PROBE_HANDLER = taker_credential_probe.view_class.as_view()


class _TakerAppApiFixture(TenantTestCase):
    """内部应用管理端点的共用夹具：管理员 / 打手 / 无节点账号 / 另一个打手 + 造物工具。"""

    def setUp(self):
        super().setUp()
        seed_catalog()
        self.domain_name = self.domain.domain
        self.factory = APIRequestFactory()
        # 管理员：持门里那一个既有节点（`system.pinduoduo_settings`）。
        self.admin = make_logged_in_client(
            username=ADMIN_USERNAME, groups=("客服",), nodes=(SYSTEM_ADMIN_NODE,)
        )
        # 无节点账号（判 403 的那一支）。
        self.nobody = make_logged_in_client(username=NOBODY_USERNAME, groups=("代练员",))
        # 租户管理员（`nodes_of` 短路为全目录）——用来**如实钉住**那条既有行为。
        self.tenant_admin = make_logged_in_client(
            username=TAKER_ADMIN_USERNAME, is_superuser=True
        )
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.admin_user = User.objects.get(username=ADMIN_USERNAME)
        # 「另外一个人」：本用例要证的是"打手建应用时⛔ 不会落成他"。
        self.other_user = User.objects.create_user(
            username=OTHER_USERNAME, password=PASSWORD
        )
        # 一只游戏：`make_logged_in_client` 的默认语义是"给当前租户全部游戏"
        # （`AGENTS.md` §5-G 的夹具纪律），建一行让那条路真的走通。
        Game.objects.create(name="打手应用游戏甲")

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_taker_user(self, username, *, first_name=""):
        """建一个**普通账号**（当"被绑定的打手"用；⛔ 不带任何节点）。"""
        return User.objects.create_user(
            username=username, password=PASSWORD, first_name=first_name
        )

    def grant_taker_identity(self, username):
        """造 T4 那条节点 + 给某个账号挂上 ⇒ 返回**已登录**的 client（打手身份）。

        ⚠️ 见模块 docstring 的「为什么夹具会手工造 T4 那条权限点」：
        `get_or_create` 让 T4 落地之后本夹具仍然可用（那时只是取到已有的那一行）。
        ⚠️ 账号由 `make_logged_in_client` **自己建**（⛔ 调用方不要先建一个同名账号——
        那会撞 `username` 的唯一约束）。
        """
        PermissionNode.objects.get_or_create(
            code=TAKER_IDENTITY_NODE,
            defaults={
                "name": "允许打手使用开发平台应用",
                "note": "打手可进入「打手对接」页并为自己创建一套接入凭据",
                "url": "/open-platform/taker-apps",
                "kind": NodeKind.PAGE,
                "level": 1,
                "available": True,
            },
        )
        return make_logged_in_client(
            username=username, groups=("代练员",), nodes=(TAKER_IDENTITY_NODE,)
        )

    def make_app(self, *, name=APP_NAME, app_key="takerappkey000001",
                 app_secret="taker-app-secret-0000000000001",
                 taker=None, state=AppState.ENABLED, ip_allowlist=""):
        """直接经 ORM 造一行应用（端点的 `POST` 另有专门用例覆盖）。"""
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker or self.other_user,
            created_by=self.admin_user,
            state=state,
            ip_allowlist=ip_allowlist,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def get(self, url, *, client=None, data=None):
        return (client or self.admin).get(url, data or {})

    def post_json(self, url, payload, *, client=None):
        return (client or self.admin).post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def patch_json(self, url, payload, *, client=None):
        return (client or self.admin).patch(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def delete(self, url, *, client=None):
        return (client or self.admin).delete(url)

    # ── 令牌：**只从取令牌端点拿**（spec §9-2 的要求）───────────────────
    def token_request(self, *, app_key, app_secret, remote_addr=SOURCE_IP):
        return self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app_key,
                "client_secret": app_secret,
            },
            REMOTE_ADDR=remote_addr,
        )

    def issue_token(self, app, *, secret=None) -> str:
        """走**真实的**取令牌端点换一个令牌（⛔ 不是 `tokens.issue` 自己签一个）。

        ⚠️ 成功体是**裸三字段**（⛔ 没有本仓信封）：令牌从"调用方真正看到的载荷"里取，
        ⛔ 不从数据库对象拼。
        """
        response = self.token_request(
            app_key=app.app_key,
            app_secret=app.app_secret if secret is None else secret,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    def probe(self, token, *, remote_addr=SOURCE_IP):
        """拿一个令牌打**真认证类**装载的探针（见模块 docstring 的证明力边界）。"""
        request = self.factory.get("/probe/taker-credential/", REMOTE_ADDR=remote_addr)
        request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return rendered_response(TAKER_PROBE_HANDLER(request))

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def _row_snapshot(app):
        """一行应用的**全列快照**（`_meta.concrete_fields`，含 pk / 三个时间列）。

        ⚠️ 判"那一行一个字段都没变"必须用它，⛔ 不是挑几个字段比一比：挑字段的写法在
        "改了一列而我恰好没挑到"时**静默绿**（`E-0064` 同族：判据要钉在被保护的那个东西上）。
        ⚠️ 快照取 `attname`（`taker_id` 那一类外键取**主键值**），⛔ 不取对象本身
        —— 对象上的字段是刷新前的缓存，比"对象相等"会变成比内存地址。
        """
        return {
            field.name: getattr(app, field.attname)
            for field in TakerApp._meta.concrete_fields
        }

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
        """业务失败：**HTTP 200** + 指定的业务码（本仓口径：状态码只表达传输 / 认证层）。

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


class TakerAppAdminAccessTests(_TakerAppApiFixture):
    """判据 1 / 4：认证与权限装配、⛔ 没有 `DELETE`。"""

    def test_anonymous_requests_are_401(self):
        """匿名 ⇒ **401**（⛔ 不是 403）：DRF 对"没有任何认证类受理"的请求回 `NotAuthenticated`。"""
        app = self.make_app()
        for url in (
            LIST_PATH,
            DETAIL_TEMPLATE.format(pk=app.pk),
            ROTATE_TEMPLATE.format(pk=app.pk),
        ):
            with self.subTest(url=url):
                response = self.get(url, client=self.anonymous)
                self.assertEqual(
                    response.status_code, UNAUTHENTICATED, response.content[:300]
                )
                self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_logged_in_account_without_the_node_is_403(self):
        """登录但**两个门节点一个都没有** ⇒ **403**（节点门真的在拦，⛔ 不是 404 / 405）。

        ⚠️ 这一条**同时**是"今天目录里还没有 `system.allow_taker_open_api` ⇒ 除管理员外
        没人持有点"的现场：`self.nobody` 一个节点都没有，他对四条写 / 读方法的答复全是 **403**
        （⛔ 不许为了让打手"能过"去放宽这条门）。
        """
        app = self.make_app()
        calls = (
            ("get", LIST_PATH),
            ("post", LIST_PATH),
            ("patch", DETAIL_TEMPLATE.format(pk=app.pk)),
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
        """对照（能失败）：**有**门节点 ⇒ 列表 200。"""
        self.make_app()
        self.assert_ok(self.get(LIST_PATH))

    def test_the_taker_identity_node_alone_is_enough_to_enter(self):
        """⭐ 门是"**任一**命中即可"：只持 `system.allow_taker_open_api` 也能进（⛔ 不用管理员节点）。

        ⚠️ 这条**不是**"打手能当管理员"——进去之后能看见什么由**身份限权**回答
        （`TakerIdentityScopingTests` 与列表作用域那两条）。
        """
        taker = self.grant_taker_identity(TAKER_USERNAME)
        self.assert_ok(self.get(LIST_PATH, client=taker))

    def test_a_valid_taker_credential_is_not_a_session(self):
        """⭐ spec §9-2 的另一半：**打手侧那把对外凭据**打**内部**端点 ⇒ **401**。

        判据不打在 `authentication_classes` 上（那是"装了什么"），而打在**行为**上：
        一份**完全合法**、**当从真端点换到**的对外令牌，对内部端点**不是**身份 ⇒ **401**
        （内部端点走全局默认的会话认证，`Authorization` 头它根本不认）。

        ⚠️ **反向那一半**（拿会话打 `/api/taker/v1/…` ⇒ 401）属 **T5 / T6**：那几条对外
        路由今天还不存在，⛔ 本轮不造假端点去证它（spec §9-2 的另一半挂在 T5 的交付项里，
        T2b 评审第 6 条已登记）。
        """
        app = self.make_app(taker=self.other_user)
        token = self.issue_token(app)
        # 对照（能失败）：同一把令牌打**对外认证类**是**通过**的 ⇒ 上面的 401 不是"令牌本来就坏"。
        self.assertEqual(self.probe(token).status_code, 200, "令牌本身必须是有效的")

        for url in (LIST_PATH, DETAIL_TEMPLATE.format(pk=app.pk)):
            with self.subTest(url=url):
                response = self.anonymous.get(
                    url, HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP
                )
                self.assertEqual(
                    response.status_code, UNAUTHENTICATED, response.content[:300]
                )
                self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)
                self.assertIsNone(self.body(response)["data"])

    def test_no_delete_route_returns_405(self):
        """⛔ **没有 `DELETE`**：三条路径上、**有权限的管理员**发 `DELETE` 都回 **405**
        （⛔ 不是 404、⛔ 不是没测）。
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
                self.assertEqual(
                    response.status_code, METHOD_NOT_ALLOWED, response.content[:300]
                )
        self.assertTrue(TakerApp.objects.filter(pk=app.pk).exists(), "应用必须还在")

    def test_the_405_reading_belongs_to_the_authenticated_and_authorized_layer(self):
        """⚠️ **如实钉住 405 的那层前提**：DRF 的 405 判定排在 `initial()`（认证 + 权限）
        **之后** ⇒ 匿名 / 无节点的 `DELETE` 拿到的是 **401 / 403**，⛔ 不是 405。

        ⛔ 这条**不是**"把 405 那条放宽"：上一条仍要求**有权限的管理员**拿到字面 405。
        两条一起才说清「**认证 + 权限过了之后**才是 405」这句话（⛔ 也不许据此说"路径不存在"）。
        """
        app = self.make_app()
        url = DETAIL_TEMPLATE.format(pk=app.pk)
        self.assertEqual(
            self.delete(url, client=self.anonymous).status_code, UNAUTHENTICATED
        )
        self.assertEqual(self.delete(url, client=self.nobody).status_code, FORBIDDEN)

    def test_detail_get_and_put_are_405(self):
        """详情路径只定义 `PATCH` ⇒ `GET` / `PUT` 都是 405（spec §6 那一行只有 `PATCH`）。"""
        app = self.make_app()
        url = DETAIL_TEMPLATE.format(pk=app.pk)
        self.assertEqual(self.get(url).status_code, METHOD_NOT_ALLOWED)
        response = self.admin.put(
            url, data=json.dumps({"name": OTHER_APP_NAME}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, METHOD_NOT_ALLOWED)


class TakerAppNodeGateTests(_TakerAppApiFixture):
    """⭐ 门那条常量的**形状**判据（T4 的接线锚点靠它）。

    ⚠️ 判据是**两条独立的事实**：① 门确实是"那两个 code、任一命中"；
    ② 那条常量是**字面量元组** —— `tests/test_rbac_wiring.py` 的 T2 锚点解析只做
    **一层模块常量解引用**、且只收**字符串字面量**：拆成两个常量再拼起来 ⇒ 解析不到 code
    ⇒ T4 那条位点判据会**假红**。⇒ 这里把"字面量"这条形状钉住，让它在 T4 之前就能红。
    """

    def test_the_gate_constant_is_a_literal_tuple_of_the_two_codes(self):
        self.assertEqual(
            taker_app_views.APP_MANAGE_NODES,
            ("system.pinduoduo_settings", "system.allow_taker_open_api"),
            "门那条常量必须**逐字**是这两个 code 的字面量元组"
            "（拆成常量再拼会让 T4 的 AST 锚点解析不到）",
        )

    def test_the_gate_and_the_identity_branch_share_one_predicate(self):
        """身份分支用的是**门里那一个**管理员节点（⛔ 不另写一份"谁是管理员"的判据）。

        判据：把 `SYSTEM_ADMIN_NODE` 换成一个谁都没有的 code ⇒ 管理员那一支**当场失效**
        （他从"可建、必须选打手"退化成"落成自己"）。⚠️ 这是**桩打在常量上**（`E-0029`：
        桩打在被查找的那个模块上），⛔ 不是把行为与期望一起改。
        """
        app = self.make_app(taker=self.other_user)
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": "桩前的应用", "taker": self.other_user.pk}
            )
        )
        self.assertEqual(data["taker"], self.other_user.pk, "对照：管理员能选别人")
        original = taker_app_views.SYSTEM_ADMIN_NODE
        try:
            taker_app_views.SYSTEM_ADMIN_NODE = "system.no_such_node_in_this_tenant"
            data = self.assert_ok(
                self.post_json(
                    LIST_PATH, {"name": "桩后的应用", "taker": self.other_user.pk}
                )
            )
        finally:
            taker_app_views.SYSTEM_ADMIN_NODE = original
        self.assertEqual(
            data["taker"], self.admin_user.pk,
            "管理员那一条判据失效后，调用方应当落到「锁死为自己」那一支"
            "（不落成自己 ⇒ 说明身份分支用了另一份判据）",
        )
        self.assertTrue(TakerApp.objects.filter(pk=app.pk).exists())


class TakerAppListTests(_TakerAppApiFixture):
    """判据 1 / 2 / 5：列表的**行载荷**与**可见范围**。"""

    def test_a_row_carries_exactly_the_frozen_keys_and_a_live_secret(self):
        """⭐ 一行**恰好**这 12 个键（字面量见模块顶部），且 `app_secret` **等于库里的值**。

        ⛔ 本线**有意**含密钥（与上家侧 `D-0201` 同口径：明文可随时获取）——
        这条钉的是"**恰好**这些键"：多一个（例如 `created_by` / `callback_url`）也红。
        """
        app = self.make_app()
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(set(row), ROW_KEYS, f"行载荷的键集变了：{sorted(row)}")
        self.assertEqual(row["app_secret"], app.app_secret)
        self.assertEqual(row["app_key"], app.app_key)
        self.assertEqual(row["taker"], app.taker_id)
        self.assertEqual(row["state"], "enabled")
        self.assertEqual(row["state_text"], "启用")
        self.assertIsNone(row["secret_rotated_at"])
        self.assertIsNone(row["last_used_at"])

    def test_the_row_moments_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：应用行的三个时刻键是**项目时区**（+08:00）串。

        管理端打手应用页截串直显；旧写法裸 `.isoformat()` 出 UTC 串、差 8 小时。
        夹具把 `created_at` / `last_used_at` 钉在 UTC 04:30 ⇒ 北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        app = self.make_app()
        TakerApp.objects.filter(pk=app.pk).update(created_at=fixed, last_used_at=fixed)
        row = self.assert_ok(self.get(LIST_PATH))[0]
        self.assertEqual(row["created_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["last_used_at"], "2026-10-24T12:30:00+08:00")
        self.assertIsNone(row["secret_rotated_at"])

    def test_every_row_carries_its_own_secret_not_the_first_one(self):
        """每行给自己的密钥（⛔ 不是"把第一行的密钥发给所有人"那种装配错）。"""
        first = self.make_app(name=APP_NAME, app_key="takerappkey000001",
                              app_secret="taker-secret-00000000000000001")
        second = self.make_app(name=OTHER_APP_NAME, app_key="takerappkey000002",
                               app_secret="taker-secret-00000000000000002")
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(
            {row["name"]: row["app_secret"] for row in rows},
            {APP_NAME: first.app_secret, OTHER_APP_NAME: second.app_secret},
        )

    def test_the_list_is_ordered_by_id(self):
        """顺序确定（`Meta.ordering = ["id"]`）：两次调用不该给出不同顺序。"""
        self.make_app(name=APP_NAME, app_key="takerappkey000001")
        self.make_app(name=OTHER_APP_NAME, app_key="takerappkey000002")
        names = [row["name"] for row in self.assert_ok(self.get(LIST_PATH))]
        self.assertEqual(names, [APP_NAME, OTHER_APP_NAME])

    def test_the_query_count_does_not_grow_with_the_number_of_apps(self):
        """⭐ **列表不按行查库**：应用数变了、查询数**不跟着涨**。

        ⚠️ 判据取"应用数变了、查询数不跟着涨"，⛔ 不是写死一个数字（那个数字会随认证 /
        会话 / 中间件漂移，写死必过期）。本线的行载荷要读关联对象（`taker` 那一行的显示名）
        ⇒ `select_related("taker")` 是**承重**的：丢了它，查询数就随行数涨 ⇒ 本条红。
        """
        self.make_app(name=APP_NAME, app_key="takerappkey000001")
        one = self._list_query_count()
        for index in range(4):
            self.make_app(
                name=f"查询数应用{index}", app_key=f"takerappkey00000{index + 2}"
            )
        five = self._list_query_count()
        self.assertEqual(
            one, five, "列表的查询数随应用数增长 ⇒ `taker` 那一行在逐行查库"
        )

    def _list_query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assert_ok(self.get(LIST_PATH))
        return len(ctx.captured_queries)

    def test_each_taker_sees_exactly_their_own_apps_and_the_admin_sees_all(self):
        """⭐ spec §6：打手「⛔ **看不到**、改不了**别人的**」——判据是**恰好集合**（两侧各一条）。

        ⚠️ 这条**不只是整洁问题**：行载荷里是**明文密钥**（`D-0201`）⇒ 看得见别人的行
        就等于拿得到别人的凭据（拿它去取令牌端点换令牌即可用）。

        判据三件（每件都能单独失败）：
        ① **打手 A** 的列表**恰好**等于绑给 A 的那两条（自己建的 + 管理员替他建的都算"他的"）；
        ② **打手 B** 的列表**恰好**等于绑给 B 的那一条 —— ⛔ 不是只测"某一侧为空"：
           只测 A 的话，一个"谁都看得见只绑给 A 的行"的实现也能绿；
        ③ **对照**：管理员看得到**全部三条** ⇒ 上面两条不是"表格本来就空"。
        ⛔ 三处都是**恰好集合**（`==`），⛔ 不许放宽成 `assertNotIn` 那一种包含式写法。
        """
        taker_a = self.grant_taker_identity(TAKER_USERNAME)
        user_a = User.objects.get(username=TAKER_USERNAME)
        taker_b = self.grant_taker_identity(TAKER_B_USERNAME)
        user_b = User.objects.get(username=TAKER_B_USERNAME)
        mine = self.make_app(
            name="A 自己建的", app_key="takerappkey000011", taker=user_a
        )
        assigned = self.make_app(
            name="管理员替 A 建的", app_key="takerappkey000012", taker=user_a
        )
        theirs = self.make_app(
            name="B 的", app_key="takerappkey000013", taker=user_b
        )

        rows_a = self.assert_ok(self.get(LIST_PATH, client=taker_a))
        self.assertEqual({row["id"] for row in rows_a}, {mine.pk, assigned.pk})
        self.assertNotIn(
            theirs.pk, {row["id"] for row in rows_a}, "A 的列表里混进了 B 那一行"
        )
        rows_b = self.assert_ok(self.get(LIST_PATH, client=taker_b))
        self.assertEqual({row["id"] for row in rows_b}, {theirs.pk})
        # ③ 对照（能失败）：管理员看得到全部。
        self.assertEqual(
            {row["id"] for row in self.assert_ok(self.get(LIST_PATH))},
            {mine.pk, assigned.pk, theirs.pk},
        )


class TakerAppCreateTests(_TakerAppApiFixture):
    """判据 2：创建（响应含明文密钥，且它**一直**取得到）。"""

    def test_create_returns_the_secret_and_the_list_keeps_returning_it(self):
        """⭐ 创建响应的密钥与库里一致，且**之后列表里照样有它**（"随时可取"）。"""
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(data["app_secret"], app.app_secret)
        self.assertEqual(data["app_key"], app.app_key)
        rows = self.assert_ok(self.get(LIST_PATH))
        self.assertEqual(rows[0]["app_secret"], app.app_secret)

    def test_create_defaults_state_enabled_and_an_empty_allowlist(self):
        """默认值：`state=enabled`（唯一开关的默认）、`ip_allowlist=""`（空 = 不限制）。"""
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        self.assertEqual(data["state"], "enabled")
        self.assertEqual(data["state_text"], "启用")
        self.assertEqual(data["ip_allowlist"], "")
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(app.state, AppState.ENABLED)
        self.assertEqual(app.ip_allowlist, "")

    def test_create_records_who_created_it_and_who_it_is_bound_to(self):
        """`created_by` = **认证态那个人**；`taker` = 被选中的打手（两件事，⛔ 不混）。"""
        self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(app.created_by_id, self.admin_user.pk)
        self.assertEqual(app.taker_id, self.other_user.pk)

    def test_create_accepts_an_ip_allowlist(self):
        """可选 `ip_allowlist`：传了就真落库（正对照，⛔ 不是"入参全被忽略"）。"""
        data = self.assert_ok(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "taker": self.other_user.pk,
                 "ip_allowlist": f"{SOURCE_IP}\n198.51.100.7"},
            )
        )
        self.assertEqual(data["ip_allowlist"], f"{SOURCE_IP}\n198.51.100.7")
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(app.ip_allowlist, f"{SOURCE_IP}\n198.51.100.7")

    # ── ⭐ T5b：回调配置两键在**创建**这条路上的三条判据（红队 Blocking 的修复判据）──
    # ⚠️ 为什么必须有这三条：收口前 `POST /api/taker/apps/` 的白名单收了这两个键、
    #    序列化器也校验了它们，但视图的 `create_fields` 里**只有 `ip_allowlist` 那一支**
    #    ⇒ 传了这两个键**不会落库**、也没有任何报错（静默用模型默认值）。
    #    已有的网都没抓到的原因正是：**没有一条用例断言"创建时给的这两个值真进了数据库"**。

    def test_create_stores_the_callback_configuration_in_the_database(self):
        """⭐ **正对照**：创建时带合法 https 回调地址 + 开关 ⇒ **库里真读得到**。

        ⚠️ 判据打在**数据库行**上（⛔ 不看响应体）：静默丢弃那种实现的响应照样是
        `code=0` + 一行载荷 —— 只有断言**列值**才抓得到它。
        ⚠️ **弄红它**（实测过）：把 `post` 的 `create_fields` 里那两个回调键删掉
        ⇒ 本用例红在 `'' != 'https://93.184.216.34/taker/hook'`。
        """
        self.assert_ok(
            self.post_json(
                LIST_PATH,
                {
                    "name": APP_NAME,
                    "taker": self.other_user.pk,
                    "callback_url": CALLBACK_URL,
                    "callback_state": "disabled",
                },
            )
        )
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(
            app.callback_url, CALLBACK_URL, "回调地址没落库（创建路径把它静默丢了）"
        )
        self.assertEqual(app.callback_state, "disabled", "回调开关没落库")

    def test_create_rejects_an_illegal_callback_url_and_writes_nothing(self):
        """非法地址（`http://` / 私网）⇒ `1000` + **点名**，且**一行都不落库**。

        ⚠️ 两条腿都要：① 业务码 + 那句人话（文案**逐字来自** `apps/openapi/outbound.py`，
        ⛔ 不是本文件自己编一句）；② **库里没有那一行**（"被拒的请求不许落库"是承重面）。
        ⚠️ **弄红它**：把 `AppCreateSerializer.callback_url` 的 `validators=[…]` 去掉
        ⇒ 本用例红成 `code=0`（地址**静默入库**）。
        """
        for url, expected in (
            (CALLBACK_HTTP_URL, openapi_outbound.NOT_HTTPS_MSG),
            (CALLBACK_PRIVATE_URL, openapi_outbound.NON_PUBLIC_IP_MSG),
        ):
            with self.subTest(url=url):
                payload = self.assert_business_failure(
                    self.post_json(
                        LIST_PATH,
                        {
                            "name": APP_NAME,
                            "taker": self.other_user.pk,
                            "callback_url": url,
                        },
                    ),
                    VALIDATION,
                )
                self.assertIn(
                    expected, payload["msg"],
                    "拒收文案必须**逐字来自** apps/openapi/outbound.py"
                    "（⛔ 不许在打手侧自己编一句）："
                    f"{payload['msg']!r}",
                )
                self.assertEqual(
                    TakerApp.objects.filter(name=APP_NAME).count(), 0,
                    "被拒的地址不许落库（连一行都不许）",
                )

    def test_create_without_the_callback_keys_uses_the_model_defaults(self):
        """不带这两键 ⇒ **模型默认值**（地址空串、开关 `enabled`）。

        ⭐ 这一条是"**只有请求里真给了才写**"那条判据的反面对照：缺了它，
        `create_fields` 里无条件写死一个值（或把地址写死成空串）也能让上面两条绿
        ⇒ 那条判据退化成**恒真**（`E-0021` 同族）。
        ⚠️ 默认值本身也是契约（第二期 spec §3.1）：地址空 = 没配回调；开关默认 `enabled`
        在"地址为空 ⇒ 本来就不投递"之下**不改变安全面**。
        """
        self.assert_ok(
            self.post_json(LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk})
        )
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(app.callback_url, "")
        self.assertEqual(app.callback_state, "enabled")

    def test_create_rejects_a_missing_blank_or_too_long_name(self):
        for payload in ({"taker": self.other_user.pk}, {"name": ""},
                        {"name": "x" * 51, "taker": self.other_user.pk}):
            with self.subTest(payload=str(payload)[:40]):
                self.assert_business_failure(
                    self.post_json(LIST_PATH, payload), VALIDATION
                )
        self.assertEqual(TakerApp.objects.count(), 0, "被拒的创建不许落库")

    def test_create_rejects_a_duplicate_name(self):
        """重名 ⇒ `1000`（**可预期**失败：⛔ 不让它撞唯一索引变成 500）。"""
        self.make_app()
        self.assert_business_failure(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            ),
            VALIDATION,
            msg_fragment="应用名已存在",
        )
        self.assertEqual(TakerApp.objects.filter(name=APP_NAME).count(), 1)

    def test_create_writes_an_audit_row_with_the_key_but_never_the_secret(self):
        """留痕：一行、`to_value` 是 `app_key`（**非机密**）、⛔ 密钥一个字都不进。"""
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        rows = list(self.logs_for("system.taker_api_app_create"))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.log_type, 40)
        self.assertEqual(row.target_kind, "taker_app")
        self.assertEqual(row.target_name, APP_NAME)
        self.assertEqual(row.to_value, data["app_key"])
        self.assertIn(self.other_user.username, row.summary)
        self.assertNotIn(data["app_secret"], row.summary)


class TakerIdentityScopingTests(_TakerAppApiFixture):
    """⭐ 判据 3 / 5：**身份限权**（spec §6 / §9-10；本任务最承重的一段）。"""

    def test_a_taker_creating_an_app_gets_it_bound_to_themselves(self):
        """打手身份建应用 ⇒ `taker` **落成他自己**（正对照）。

        ⭐ **这也是"拒绑管理员"那条收窄的正对照②**（spec §6 红队裁定）：一个**恰好持新节点**的
        **普通账号**（`grant_taker_identity` 只给 `system.allow_taker_open_api`、⛔ 不给管理员职位）
        ⇒ **照样能建、落成自己**。⚠️ 缺了它，"无差别拒"的实现也能让那两条负判据绿。
        前置哨兵（⛔ 不是装饰）：他**不是**租户管理员——否则本条就变成了"管理员自绑"的反面例子。
        """
        taker = self.grant_taker_identity(TAKER_USERNAME)
        taker_user = User.objects.get(username=TAKER_USERNAME)
        self.assertFalse(
            is_tenant_admin(taker_user),
            "前提：他只持新节点、**不是**租户管理员（否则本正对照的语义变了）",
        )
        data = self.assert_ok(self.post_json(LIST_PATH, {"name": APP_NAME}, client=taker))
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertEqual(data["taker"], taker_user.pk)
        self.assertEqual(app.taker_id, taker_user.pk)
        self.assertEqual(app.created_by_id, taker_user.pk, "打手自己建的 ⇒ 创建人也是他")

    def test_a_taker_cannot_bind_someone_else_even_by_naming_them(self):
        """⭐ **选定的一支：落成自己**（⛔ 不是"拒"）。载荷里带**别人**的 id ⇒ **忽略**它。

        spec §6 那一栏的原话是「**锁死为他自己**（不给选）」，计划 T3 行同款
        （`docs/plans/2026-10-08-taker-open-api.md` 的判据列：打手身份建应用时 `taker`
        **落成他自己**）。理由与代价逐条写在 `TakerAppAdminView._resolve_taker` 的 docstring 里。

        ⭐ **反例（spec §9-10 点名要的那一条）**：结果里那条应用的 `taker`
        **绝不等于**载荷里那个别人 —— 它钉的是"**没有**被采纳"，与上面那条正对照一起
        排除了"两种都算过"那种自选期望的写法。
        """
        taker = self.grant_taker_identity(TAKER_USERNAME)
        taker_user = User.objects.get(username=TAKER_USERNAME)
        self.assertNotEqual(self.other_user.pk, taker_user.pk, "反例的前提：两个是不同的人")
        data = self.assert_ok(
            self.post_json(
                LIST_PATH,
                # ⚠️ 这个 id 是**真实存在**的另一个账号（⛔ 不是编的）——
                # 若实现"回落成载荷里的值"，本条必红。
                {"name": APP_NAME, "taker": self.other_user.pk},
                client=taker,
            )
        )
        app = TakerApp.objects.get(name=APP_NAME)
        self.assertNotEqual(
            data["taker"], self.other_user.pk,
            "打手建应用时载荷里的那个别人被采纳了（身份限权失效）",
        )
        self.assertNotEqual(app.taker_id, self.other_user.pk)
        self.assertEqual(data["taker"], taker_user.pk)
        self.assertEqual(app.taker_id, taker_user.pk)

    def test_taker_a_cannot_patch_or_rotate_taker_bs_app(self):
        """⭐ **反例判据（总指挥 2026-10-08 点名要的那一条）**：拿**打手 A 的会话**去改
        **打手 B 的那条应用** ⇒ `1000` + 「应用不存在」，且**库里 B 那一行一个字段都没变**。

        ⚠️ 与下面那条（改一个**普通账号**名下的行）**不是重复**：那条证的是"不是你的"，
        这条证的是**同类调用方之间**的越权——A 与 B 都持 `system.allow_taker_open_api`、
        都能进这组端点，差别只在**绑的是谁**。身份限权若只做在"谁持节点"这一层
        （而没做在"这一行是谁的"那一层），这条必红。

        ⚠️ **"一个字段都没变"是逐字段快照**（`TakerApp._meta.concrete_fields` 全列），
        ⛔ 不是挑四个字段比一比：挑字段的写法在"改了一列而我没挑到"时**静默绿**。
        ⚠️ 判据取"与 pk 不存在**同形**"（`1000` + 同一句）是**有意**的：403 会告诉 A
        "这个 pk 存在、只是不是你的" ⇒ 一条可枚举应用主键的信道（理由写在
        `TakerAppAdminView._get_app` 的 docstring 里）。
        """
        taker_a = self.grant_taker_identity(TAKER_USERNAME)
        # ⚠️ B 的客户端只建**一次**（夹具自己建号 ⇒ 再调一次会撞 `username` 唯一约束）。
        taker_b = self.grant_taker_identity(TAKER_B_USERNAME)
        user_b = User.objects.get(username=TAKER_B_USERNAME)
        b_app = self.make_app(
            name=OTHER_APP_NAME, app_key="takerappkey000025",
            app_secret="taker-secret-00000000000000025", taker=user_b,
            ip_allowlist="198.51.100.7",
        )
        before = self._row_snapshot(b_app)

        self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=b_app.pk),
                {"name": "A 改的名字", "state": "disabled", "ip_allowlist": ""},
                client=taker_a,
            ),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        self.assert_business_failure(
            self.post_json(ROTATE_TEMPLATE.format(pk=b_app.pk), {}, client=taker_a),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        b_app.refresh_from_db()
        self.assertEqual(self._row_snapshot(b_app), before, "B 的那一行被 A 改动了")

        # 正对照（能失败）：**B 自己**改得动同一条 ⇒ 上面那两条不是"这一行本来就不可写"。
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=b_app.pk), {"name": "B 自己改的"},
                client=taker_b,
            )
        )
        b_app.refresh_from_db()
        self.assertEqual(b_app.name, "B 自己改的")

    def test_a_taker_cannot_patch_or_rotate_someone_elses_app(self):
        """⭐ spec §6：打手「看不到、**改不了**别人的」（这里那个人**不是**打手）。

        ⚠️ 判据打在**终态**上：那一行**逐字段快照**（`_row_snapshot`）**一个都没变**
        （⛔ 不是只看状态码、也⛔ 不是挑几个字段比）。答复取"取不到"那一形（`1000` +
        「应用不存在」）：⛔ 不告诉调用方"这个 pk 存在、但不是你的"（可探测的信道，
        与全仓"查不到 / 不是你的同形"的口径一致）。
        """
        taker = self.grant_taker_identity(TAKER_USERNAME)
        other = self.make_app(
            name=OTHER_APP_NAME, app_key="takerappkey000021",
            app_secret="taker-secret-00000000000000021", taker=self.other_user,
            ip_allowlist="198.51.100.7",
        )
        before = self._row_snapshot(other)
        self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=other.pk),
                {"name": "被改掉的名字", "state": "disabled", "ip_allowlist": ""},
                client=taker,
            ),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        self.assert_business_failure(
            self.post_json(ROTATE_TEMPLATE.format(pk=other.pk), {}, client=taker),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        other.refresh_from_db()
        self.assertEqual(self._row_snapshot(other), before)

    def test_a_taker_can_patch_their_own_app(self):
        """对照（能失败）：**自己那条**要能改（否则"什么都拒"也能让上面那条绿）。"""
        taker = self.grant_taker_identity(TAKER_USERNAME)
        taker_user = User.objects.get(username=TAKER_USERNAME)
        mine = self.make_app(name=APP_NAME, taker=taker_user)
        data = self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=mine.pk),
                {"name": "改过的名字", "state": "disabled"},
                client=taker,
            )
        )
        mine.refresh_from_db()
        self.assertEqual(data["state"], "disabled")
        self.assertEqual(mine.name, "改过的名字")
        self.assertEqual(mine.state, AppState.DISABLED)

    def test_an_admin_must_pick_a_taker(self):
        """管理员建打手侧应用时**必须选**一个打手账号：缺 ⇒ `1000` + **点名** `taker`。"""
        payload = self.assert_business_failure(
            self.post_json(LIST_PATH, {"name": APP_NAME}), VALIDATION
        )
        self.assertIn("taker", payload["msg"])
        self.assertEqual(TakerApp.objects.count(), 0, "被拒的创建不许落库")

    def test_an_admin_must_pick_a_taker_that_shapes_like_a_number(self):
        """非法形状（字符串 / 对象 / 列表）⇒ 序列化器当场 `1000`（⛔ 不进 `filter(pk=…)`）。"""
        for value in ("abc", {"a": 1}, [1, 2]):
            with self.subTest(value=str(value)[:30]):
                self.assert_business_failure(
                    self.post_json(LIST_PATH, {"name": APP_NAME, "taker": value}),
                    VALIDATION,
                )
        self.assertEqual(TakerApp.objects.count(), 0)

    def test_an_admin_must_pick_a_taker_that_exists_in_this_tenant(self):
        """`taker` 查不到 ⇒ `1000` + **点名那个 id**。

        ⚠️ **"别的租户的账号"与"不存在"在本仓形态下是同一件事**：`auth_user` 是**每租户
        schema 各一份**（`django.contrib.auth` 同时在 `SHARED_APPS` 与 `TENANT_APPS`）⇒ 查询
        天然只在本租户里进行。⛔ 本线**不去加**一次跨 schema 的查找来"区分"这两者
        （那正是 `AGENTS.md` §4「不跨越租户边界」要挡的形状）。
        """
        payload = self.assert_business_failure(
            self.post_json(LIST_PATH, {"name": APP_NAME, "taker": 999999}),
            VALIDATION,
        )
        self.assertIn("taker", payload["msg"], "文案要点名那个字段")
        self.assertIn("999999", payload["msg"], "文案要点名那个被拒的 id")
        self.assertEqual(TakerApp.objects.count(), 0)

    def test_an_admin_can_pick_any_existing_account(self):
        """对照（能失败）：选一个**存在**的账号 ⇒ 真的落成他（⛔ 不是"管理员也被锁成自己"）。

        ⭐ **这也是"拒绑管理员"那条收窄的正对照③**：被绑的是**普通打手账号** ⇒ 建成功。
        ⚠️ 前置哨兵（⛔ 不是装饰）：`self.other_user` 真的**不是**租户管理员——
        否则本条断的就不是"普通账号能绑"了（而那条收窄的负判据也会跟着变成同义反复）。
        """
        self.assertFalse(
            is_tenant_admin(self.other_user),
            "前提：被绑的那个账号不是租户管理员（本条是「普通账号放行」的正对照）",
        )
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        self.assertEqual(data["taker"], self.other_user.pk)
        self.assertEqual(TakerApp.objects.get(name=APP_NAME).taker_id, self.other_user.pk)

    # ── ⭐ 拒绑租户管理员（红队 + 总指挥裁定，fail-closed）──────────
    def test_an_admin_cannot_bind_an_app_to_a_tenant_admin(self):
        """⭐ **负判据①（管理员分支）**：`taker=<一个租户管理员的 id>` ⇒ `1000` + **点名** + **0 行**。

        ## 为什么这是安全洞（⛔ 不是洁癖）

        `is_tenant_admin` 的账号在 `apps/core/scoping.py:12` 与
        `apps/business/game_scope.py:45` 上被**短路**（= 看得见**全部**游戏），
        `nodes_of` 也短路成**全目录** ⇒ 一旦把打手侧应用绑到管理员账号上，那条对外令牌
        **看得见整个大厅**、抢单那四道按人闸门**全部恒真** ⇒ **任何单都能抢**。

        ## 判据的三半（缺一不可）

        ① 业务码 `1000`（⛔ 不是 500、也⛔ 不是"静默成功"）；② 文案**点名**那个字段与那种身份；
        ③ 库里 **0 行**（被拒的创建一行都不落库）。
        ⚠️ 只断码一半不够：把拒因写成"账号不存在"也回 `1000`，但那对运营是**错的话**。

        ## ⚠️ 本条**不**覆盖的那一半（如实登记）

        "先把**普通**账号绑上去、**后来**把它提成租户管理员 ⇒ 范围静默变宽"——那是**提权**那一半，
        本守卫（建时判身份）**挡不住**，⚠️ ⛔ 不许读成"已彻底解决"；
        理由与两条兜底写在 `apps/takerapi/api/app_views.py` 的「⚠️⭐ 残余风险」那一节。
        """
        tenant_admin_user = User.objects.get(username=TAKER_ADMIN_USERNAME)
        self.assertTrue(
            is_tenant_admin(tenant_admin_user),
            "前提：那个账号**真的是**租户管理员（否则本条恒真、什么都没证）",
        )
        payload = self.assert_business_failure(
            self.post_json(LIST_PATH, {"name": APP_NAME, "taker": tenant_admin_user.pk}),
            VALIDATION,
        )
        self.assertIn("taker", payload["msg"], "文案要点名那个字段")
        self.assertIn("系统管理员", payload["msg"], "文案要点名那种身份（不是「不存在」）")
        self.assertEqual(TakerApp.objects.count(), 0, "被拒的创建不许落库（一行都不许）")
        self.assertEqual(self.logs_for("system.taker_api_app_create").count(), 0, "也不许写留痕")

    def test_the_taker_branch_also_refuses_a_tenant_admin_self(self):
        """⭐ **负判据②（打手分支）**：若"自己"是租户管理员 ⇒ **不许建**（spec §6 的裁定原文）。

        ## 为什么这条要**桩住** `_is_system_admin`

        今天这条**打 HTTP 打不到**：租户管理员在 `_is_system_admin`（= `has_node_now` 的管理员
        短路）上**恒为真** ⇒ 他一律走**管理员**分支（那条由上面那条用例钉住）。
        但 spec §6 的裁定明写"打手分支若'自己'是租户管理员 ⇒ 不许建"，而守卫若只写在管理员
        分支里，**门与身份一旦分叉**（门换了谓词、身份还认职位）就漏。
        ⇒ 本用例把 `_is_system_admin` 桩成 `False`，**强制走打手分支**，钉住"守卫挂在两条分支的
        汇合处"这条实现选择。
        ⚠️ **变异自证**：把守卫挪进管理员分支（在 `return taker` 之前）⇒ 本条**红**、
        而上面那条仍绿（这正是本条存在的理由）。

        ⚠️ 证明力边界（如实）：它证的是"守卫的位置覆盖打手分支"，⛔ **不**证"今天真有一条
        HTTP 请求会落到打手分支的租户管理员身上"（今天没有，见上）。
        """
        with mock.patch.object(
            taker_app_views.TakerAppAdminView,
            "_is_system_admin",
            staticmethod(lambda user: False),
        ):
            payload = self.assert_business_failure(
                self.post_json(LIST_PATH, {"name": APP_NAME}, client=self.tenant_admin),
                VALIDATION,
            )
        self.assertIn("系统管理员", payload["msg"])
        self.assertEqual(TakerApp.objects.count(), 0, "被拒的创建不许落库（一行都不许）")

    def test_a_tenant_admin_takes_the_admin_branch(self):
        """⚠️⭐ **如实登记**：本仓 `nodes_of` 对**租户管理员**（持「系统管理员」职位）短路为
        **全目录** ⇒ 他天然两条节点都过、走的是**管理员**那一支。

        ⇒ 由此的一条**代价**：租户管理员建打手侧应用时**必须显式选**一个打手账号，
        ⛔ 不会"顺手落成自己"（他缺 `taker` ⇒ `1000`）。
        ⛔ 本线**没有试图"修"** `nodes_of` 那个短路（它是全仓共用的管理员判据，不是本线的私产）；
        这里只把行为**钉住**，让它不再靠"没人测"。
        """
        data = self.assert_ok(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "taker": self.other_user.pk},
                client=self.tenant_admin,
            )
        )
        self.assertEqual(data["taker"], self.other_user.pk)
        self.assert_business_failure(
            self.post_json(
                LIST_PATH, {"name": OTHER_APP_NAME}, client=self.tenant_admin
            ),
            VALIDATION,
        )


class TakerAppPatchTests(_TakerAppApiFixture):
    """判据 5 / 6：`PATCH` 的可改字段、no-op、以及**不可改 `taker`**。"""

    def test_patch_renames(self):
        app = self.make_app()
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"name": OTHER_APP_NAME})
        )
        app.refresh_from_db()
        self.assertEqual(app.name, OTHER_APP_NAME)
        self.assertEqual(data["name"], OTHER_APP_NAME)

    def test_patch_disables_and_re_enables(self):
        """停用 / 启用都能改（spec §6：停用是唯一的关闭动作）。"""
        app = self.make_app()
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "disabled"})
        )
        self.assertEqual(data["state_text"], "停用")
        app.refresh_from_db()
        self.assertEqual(app.state, AppState.DISABLED)
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "enabled"})
        )
        app.refresh_from_db()
        self.assertEqual(app.state, AppState.ENABLED)
        self.assertEqual(data["state_text"], "启用")

    def test_patch_sets_and_clears_the_ip_allowlist(self):
        """⭐ 空串是**合法值**（清空白名单 = 不限制）⇒ 判据必须 `in validated_data`（⛔ 不用真值）。"""
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk), {"ip_allowlist": SOURCE_IP}
            )
        )
        app.refresh_from_db()
        self.assertEqual(app.ip_allowlist, SOURCE_IP)
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"ip_allowlist": ""})
        )
        app.refresh_from_db()
        self.assertEqual(app.ip_allowlist, "", "清空白名单必须真的生效（⛔ 不是被当成没传）")

    def test_patch_rejects_an_unknown_state(self):
        app = self.make_app()
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "BOGUS"}),
            VALIDATION,
        )
        app.refresh_from_db()
        self.assertEqual(app.state, AppState.ENABLED)

    def test_patch_rejects_a_duplicate_name(self):
        self.make_app()
        other = self.make_app(name=OTHER_APP_NAME, app_key="takerappkey000031")
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=other.pk), {"name": APP_NAME}),
            VALIDATION,
            msg_fragment="应用名已存在",
        )
        other.refresh_from_db()
        self.assertEqual(other.name, OTHER_APP_NAME)

    def test_patch_unknown_pk_is_a_business_error(self):
        self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=999999), {"name": APP_NAME}),
            VALIDATION,
            msg_fragment="应用不存在",
        )

    def test_huge_pk_is_a_business_error_not_a_500(self):
        """天文数字 pk ⇒ 仍是 `1000`（⛔ 不是 500）。

        ⚠️ 这条钉的是**输入形状**：`<int:pk>` 只要求 `[0-9]+`、**不挡上界**，而实测
        psycopg 会把超 `int8` 的整数按 `numeric` 送、PostgreSQL 比较后返回 0 行 ⇒ 走
        "查不到"那一支（与上家侧同一条实测结论）。将来驱动 / 版本变化让它变成 500 时本条会红。
        """
        for url in (
            DETAIL_TEMPLATE.format(pk=10 ** 30),
            ROTATE_TEMPLATE.format(pk=10 ** 30),
        ):
            with self.subTest(url=url):
                response = (
                    self.patch_json(url, {"name": APP_NAME})
                    if "/rotate-secret/" not in url
                    else self.post_json(url, {})
                )
                self.assert_business_failure(response, VALIDATION)

    def test_taker_is_not_a_writable_field(self):
        """⛔⭐ **`taker` 不可改**（本任务要求"你来定"的那一处，结论与理由见
        `apps/takerapi/serializers.py::AppUpdateSerializer` 的 docstring）。

        判据三件（缺一条都不算）：① 载荷里带 `taker` ⇒ `1000` **且点名 `taker`**；
        ② 库里那一行的 `taker_id` **一个字没变**；③ 正对照：同一次请求里**合法**字段真的生效
        —— ⛔ 不是"什么都拒"（`state` 那一条改的是另一个字段，所以这里再单独跑一次只带
        `name` 的请求）。
        """
        app = self.make_app(taker=self.other_user)
        before = app.taker_id
        somebody = self.make_taker_user("takerapp-somebody-else")
        payload = self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk), {"taker": somebody.pk}
            ),
            VALIDATION,
        )
        self.assertIn("taker", payload["msg"])
        app.refresh_from_db()
        self.assertEqual(app.taker_id, before, "`taker` 被改掉了（它是范围判据的唯一来源）")
        # ③ 正对照：合法的可写字段真的生效。
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"name": OTHER_APP_NAME})
        )
        app.refresh_from_db()
        self.assertEqual(app.name, OTHER_APP_NAME)
        self.assertEqual(app.taker_id, before)

    def test_an_empty_patch_writes_nothing(self):
        """纯 no-op（`PATCH {}`）**不写库、不写留痕**（全仓口径 R26）。"""
        app = self.make_app()
        before = OperationLog.objects.count()
        data = self.assert_ok(self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {}))
        self.assertEqual(data["name"], APP_NAME)
        self.assertEqual(OperationLog.objects.count(), before, "no-op 不该写留痕")

    def test_re_sending_the_same_values_writes_nothing(self):
        """重发同值同样是 no-op（⛔ 不在日志里留一条"改了什么"的假话）。"""
        app = self.make_app(ip_allowlist=SOURCE_IP)
        before = OperationLog.objects.count()
        data = self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": APP_NAME, "state": "enabled", "ip_allowlist": SOURCE_IP},
            )
        )
        self.assertEqual(data["name"], APP_NAME)
        self.assertEqual(OperationLog.objects.count(), before, "重发同值不该写留痕")


class TakerAppPatchAuditTests(_TakerAppApiFixture):
    """留痕：`PATCH` **每个真正变化的字段**各一行（与上家侧同形）。"""

    def test_one_row_per_changed_field(self):
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": OTHER_APP_NAME, "state": "disabled"},
            )
        )
        rows = list(self.logs_for("system.taker_api_app_update").order_by("id"))
        self.assertEqual(len(rows), 2, rows)
        by_field = {row.field_name: row for row in rows}
        self.assertEqual(set(by_field), {"name", "state"})
        self.assertEqual(by_field["name"].from_value, APP_NAME)
        self.assertEqual(by_field["name"].to_value, OTHER_APP_NAME)
        self.assertEqual(by_field["state"].from_value, "启用")
        self.assertEqual(by_field["state"].to_value, "停用")
        for row in rows:
            self.assertEqual(row.target_kind, "taker_app")
            self.assertEqual(row.target_name, OTHER_APP_NAME)
            self.assertEqual(row.log_type, 40)
        # 句子里是**中文标签**、⛔ 不是裸列名（标签表里那三条与上家侧**共用**）。
        self.assertIn("应用名", by_field["name"].summary)
        self.assertIn("状态", by_field["state"].summary)

    def test_clearing_the_allowlist_says_empty_not_a_half_written_sentence(self):
        """空值渲染成人话「空」（⛔ 不是「IP 白名单： → 1.2.3.4」那种漏了一半的样子）。"""
        app = self.make_app(ip_allowlist=SOURCE_IP)
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"ip_allowlist": ""})
        )
        row = self.logs_for("system.taker_api_app_update").get()
        self.assertEqual(row.field_name, "ip_allowlist")
        self.assertEqual(row.from_value, SOURCE_IP)
        self.assertEqual(row.to_value, "空")
        self.assertIn("空", row.summary)


class TakerAppWhitelistTests(_TakerAppApiFixture):
    """判据 6：载荷**白名单 fail-closed**（表外的键 ⇒ `1000` + 点名 + 一行都不落库）。"""

    def test_create_rejects_a_key_outside_the_table(self):
        """创建：表外的键 ⇒ `1000` + **点名那几个键**、一行都不落库。

        ⚠️ 这里刻意一次给**两个**越界键：只报第一个的实现在本条上红
        （错误信封只带一句 msg，所以那句话必须把键**列全**）。
        ⛔⭐ **改准（第二期 T5）—— 原句里的 `"callback_url": "https://evil.example/"`
        在此逐字保留**：
        > `{"name": …, "taker": …, "app_secret": "我自己挑的密钥",
        > "callback_url": "https://evil.example/"}`

        `callback_url` 从那一天起**是**可写字段（第二期 spec §7 第一行要 `PATCH` 能改它、
        并且允许创建时一次填完，见 `apps/takerapi/serializers.py::`
        `APP_CREATE_WRITABLE_FIELDS` 那段）⇒ 它不再是一个"表外的键"。**判据一个字没放宽**：
        仍是"越界键一律 `1000` + 点名 + 一行都不落库"，换成两个**真的**表外键
        （`app_secret` 与 `state`——后者只在 `PATCH` 的白名单里，⛔ 不是创建入参）。
        ⚠️ 新契约下"`callback_url` 不再被拒"这件事**另有**判据（不是靠本条静默少测一格）：
        见 `tests/test_takerapi_callback_api.py::CallbackUrlValidationTests`
        （非法地址仍 `1000` 点名；合法地址真入库）。
        """
        payload = self.assert_business_failure(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "taker": self.other_user.pk,
                 "app_secret": "我自己挑的密钥", "state": "disabled"},
            ),
            VALIDATION,
        )
        self.assertIn("app_secret", payload["msg"])
        self.assertIn("state", payload["msg"])
        self.assertEqual(TakerApp.objects.count(), 0, "带越界键的创建一行都不许落库")

    def test_create_rejects_server_generated_credential_keys(self):
        """⛔ `app_key` 也不是入参（"密钥可预测"不该变成一条静默的配置项）。"""
        payload = self.assert_business_failure(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "taker": self.other_user.pk, "app_key": "deadbeefdeadbeef"},
            ),
            VALIDATION,
        )
        self.assertIn("app_key", payload["msg"])
        self.assertEqual(TakerApp.objects.count(), 0)

    def test_patch_rejects_a_key_outside_the_table(self):
        """`PATCH`：表外的键同样 `1000` + 点名，且**库里一个字都没变**。"""
        app = self.make_app()
        before = (app.name, app.state, app.ip_allowlist)
        payload = self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": OTHER_APP_NAME, "taker": self.other_user.pk},
            ),
            VALIDATION,
        )
        self.assertIn("taker", payload["msg"])
        app.refresh_from_db()
        self.assertEqual((app.name, app.state, app.ip_allowlist), before)
        self.assertFalse(self.logs_for("system.taker_api_app_update").exists())

    def test_an_empty_or_null_valued_unknown_key_is_still_rejected(self):
        """⭐ `""` / `null` 的**未知键照拒**（⛔ 不是"空值就当没传"那种静默口子）。

        这一条是**白名单 fail-closed** 的判别力所在：拿真值 / 非空判断去筛越界键的实现，
        在空值这一格上会**静默放行**——而调用方以为自己配置生效了。
        """
        for payload in ({"name": APP_NAME, "ghost": ""}, {"name": APP_NAME, "ghost": None}):
            with self.subTest(payload=str(payload)[:40]):
                response = self.assert_business_failure(
                    self.post_json(LIST_PATH, payload), VALIDATION
                )
                self.assertIn("ghost", response["msg"])
        self.assertEqual(TakerApp.objects.count(), 0)

    def test_a_valid_write_really_changes_the_row(self):
        """⭐ **正对照**：合法可写字段 ⇒ **真变**（⛔ 不是"什么都拒"也能让上面几条绿）。"""
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"name": OTHER_APP_NAME, "state": "disabled",
                 "ip_allowlist": "198.51.100.7"},
            )
        )
        app.refresh_from_db()
        self.assertEqual(app.name, OTHER_APP_NAME)
        self.assertEqual(app.state, AppState.DISABLED)
        self.assertEqual(app.ip_allowlist, "198.51.100.7")

    def test_a_body_that_is_not_an_object_is_a_business_error(self):
        """畸形 body（列表 / 字符串 / 数字）⇒ `1000`（⛔ 不是 500）。

        ⚠️ 判据不是"防御性写法"：DRF 的 `JSONParser` 只 `json.load` 顶层、**不校验类型**，
        而白名单那一步要读 `.keys()` ⇒ 不挡就是 `AttributeError` ⇒ HTML 500（`E-0010`）。
        """
        for raw in ("[]", '"just a string"', "12345"):
            with self.subTest(raw=raw):
                response = self.admin.post(
                    LIST_PATH, data=raw, content_type="application/json"
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                payload = self.body(response)
                self.assertEqual(payload["code"], VALIDATION, payload)
        self.assertEqual(TakerApp.objects.count(), 0)


class TakerAppRotateAndDisableTests(_TakerAppApiFixture):
    """判据 2 / 3：轮换与停用的**秒级生效**（令牌真从取令牌端点换）。"""

    def test_rotate_returns_a_new_secret_and_rotates_the_timestamp(self):
        app = self.make_app()
        old_secret = app.app_secret
        self.assertIsNone(app.secret_rotated_at)
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        app.refresh_from_db()
        self.assertNotEqual(app.app_secret, old_secret)
        self.assertEqual(data["app_secret"], app.app_secret)
        self.assertIsNotNone(app.secret_rotated_at)

    def test_the_old_token_dies_immediately_and_the_new_secret_works(self):
        """⭐ 单密钥、**没有宽限期**：轮换后**旧令牌当场 401**、新密钥可换到可用的新令牌。

        ⚠️ 旧令牌是真的（轮换前从取令牌端点换来的），⛔ 不是手搓的；"立刻"打在**同一批
        请求**上——中间没有任何等待、也没有第二套状态。
        """
        app = self.make_app()
        old_token = self.issue_token(app)
        self.assertEqual(self.probe(old_token).status_code, 200, "对照：轮换前旧令牌可用")
        old_secret = app.app_secret

        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))

        # ① 轮换前发出的**旧令牌**当场 401（它签的是旧密钥）。
        response = self.probe(old_token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(json.loads(response.content)["code"], UNAUTHENTICATED)
        # ② 旧**密钥**也不再能换到令牌（同一个事实的另一面）。
        self.assertEqual(
            self.token_request(app_key=app.app_key, app_secret=old_secret).status_code,
            UNAUTHENTICATED,
        )
        # ③ 新密钥可用、且能换到**可用**的令牌。
        new_token = self.issue_token(app, secret=data["app_secret"])
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(self.probe(new_token).status_code, 200)

    def test_the_rotate_audit_row_never_carries_a_secret(self):
        """⛔ 密钥不进留痕：新旧密钥、以及库里当前那一把，都不许出现在任何一列。"""
        app = self.make_app()
        old_secret = app.app_secret
        data = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        rows = list(self.logs_for("system.taker_api_app_rotate_secret"))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.log_type, 40)
        self.assertNotIn(old_secret, row.summary)
        self.assertNotIn(data["app_secret"], row.summary)
        self.assertIn(APP_NAME, row.summary)

    def test_disabling_the_app_kills_the_token_within_the_same_request(self):
        """⭐ **停用秒级 401**（spec §6 末行：唯一开关、无宽限期）。

        判据三件：① 停用**之前**那条令牌可用；② `PATCH state=disabled` **之后**那条**已经
        发出去的**令牌当场 401（认证的第 5 步查的正是 `state`）＋ 同一份凭据也换不到新令牌；
        ③ 正对照：重新启用 ⇒ 那条令牌**又**可用（否则"停用"可能只是把那一行删了 / 弄坏了）。
        """
        app = self.make_app(taker=self.other_user)
        token = self.issue_token(app)
        self.assertEqual(self.probe(token).status_code, 200)

        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "disabled"})
        )
        response = self.probe(token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(
            self.token_request(
                app_key=app.app_key, app_secret=app.app_secret
            ).status_code,
            UNAUTHENTICATED,
        )

        # ③ 正对照：启用回来 ⇒ 同一条令牌又能用（证的是"状态被读"而不是"整行坏了"）。
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"state": "enabled"})
        )
        self.assertEqual(self.probe(token).status_code, 200)

    def test_rotating_an_unknown_app_is_a_business_error(self):
        self.assert_business_failure(
            self.post_json(ROTATE_TEMPLATE.format(pk=999999), {}),
            VALIDATION,
            msg_fragment="应用不存在",
        )

    def test_a_freshly_created_app_can_get_a_token_without_being_rotated(self):
        """⭐ **整链对照**（`E-0176` 的形状）：创建响应里的密钥**当场**就能换到令牌。

        ⚠️ 参数从**调用方真正看到的载荷**里取（`app_key` / `app_secret` 两条都取自创建响应），
        ⛔ 不是从数据库对象拼 —— 否则"响应里给错了密钥"这种缺陷不会红。
        """
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        response = self.token_request(
            app_key=data["app_key"], app_secret=data["app_secret"]
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        token = json.loads(response.content)["access_token"]
        self.assertEqual(self.probe(token).status_code, 200)


class TakerAppSecretNeverLoggedTests(_TakerAppApiFixture):
    """判据 7：⭐ **`app_secret` 绝不进留痕 / 操作日志 / 错误消息**（`D-0201` 不放松的那一格）。

    ## 为什么这一条在本线里最重要

    密钥**可以**出现在**响应体**里（那是"明文可随时获取"的口径，业务方定的），
    ⛔ 但**不许**落到任何**留痕 / 操作日志 / 错误消息**里 —— 那些地方是**长期**留存、
    会被别的页面读出来、还会进排障会话与备份。

    ## 判据为什么打在「数据库行」上（⛔ 不是 `assertLogs` / 不是读模板常量）

    `AGENTS.md` §5-G 的硬口径：**断言副作用要打在真实出口上**——`logger.info()` 是中间层，
    而 `assertLogs` 会自己把目标 logger 的级别抬到 INFO ⇒「生产一行都没写」的测试**全绿**
    （`E-0009` / `E-0024`）。本仓真正留存的是 `apps.audit` 的 `OperationLog`
    ⇒ 这里逐条翻**落库的那些行**，并把**每一个字符串列**拼起来找密钥。
    更严的一层：`_secret_columns_hit` 让**数据库**对**表里的每一列**做一次
    `CAST(col AS text) = 密钥` ⇒ ⛔ 不依赖"哪几列可能装文本"这个假设——将来给
    `OperationLog` 加一个新的文本列，这条判据**自动**把它盖进去（⛔ 不用改测试）。

    ## 覆盖面

    本组有**三条写路径**（创建 / 改 / 轮换），每条的留痕文案模板都不一样
    ⇒ 逐条打一遍。**纯 no-op** 的那两条也打（它们不写库，但"没写"本身是判据：
    若将来有人给 no-op 也补留痕，那条会红）。
    ⚠️ **错误消息**那一半打在**响应体**上：只查**失败**响应（`1000` / `401` / `403` / `405`）
    —— 成功响应里**按设计**就有密钥（那是业务方要的），拿它当判据是自相矛盾。
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
        """⭐ 第二层：让**数据库自己**逐列扫一遍（⛔ 不是 `LIKE '%…%'`）。

        `LIKE` 的通配位（`%` / `_`）必须手工转义，而 `OperationLog.action` 里的动作码
        **本身就带 `_`**（`system.taker_api_app_create`）⇒ 转义写错就得到一条**恒真的判据**
        （`E-0083` 同族）。这里改用 **`CAST(col AS text) = 值`** 的精确相等：没有通配位可转义，
        而且 `extra(where=…)` 走的仍是**数据库**那一侧（⛔ 不是把行拉进 Python 再比）。
        返回 `(字段名, 命中行数)` 的清单；空清单 = 一列都没命中。
        """
        hits = []
        table = model._meta.db_table
        for field in model._meta.concrete_fields:
            # ⚠️ 表名 / 列名**全部**来自 Django 自己的元数据（`_meta`），⛔ 没有请求来源的
            #    字符串参与拼 SQL；**只有密钥**走 `%s` 参数绑定。
            count = model.objects.extra(
                where=[f'CAST("{table}"."{field.column}" AS text) = %s'], params=[secret]
            ).count()
            if count:
                hits.append((field.name, count))
        return hits

    def _assert_no_secret_anywhere(self, secrets, actions):
        """三条一起断：① 那几条动作码都真写了行 ② 逐列精确扫 ③ 全表拼接的文本里也没有。"""
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
            self.assertNotIn(secret, self._rows_text(OperationLog), "留痕里出现了密钥")

    def test_no_secret_ever_reaches_the_audit_trail(self):
        """⭐ 三条写路径走一遍 ⇒ 新旧密钥都不在任何留痕列里（含"曾经的那把"）。"""
        created = self.assert_ok(
            self.post_json(
                LIST_PATH,
                {"name": APP_NAME, "taker": self.other_user.pk,
                 "ip_allowlist": SOURCE_IP},
            )
        )
        app = TakerApp.objects.get(name=APP_NAME)
        first_secret = created["app_secret"]
        self.assertEqual(first_secret, app.app_secret, "对照：创建响应里的就是库里那把")

        # 另外造一只**固定密钥**的应用，用来覆盖 `PATCH` 那条路的留痕。
        other = self.make_app(
            name=OTHER_APP_NAME, app_key="takerappkey000041",
            app_secret="taker-secret-00000000000000041", taker=self.other_user,
            # ⚠️ 白名单**非空**：下面那条 PATCH 把它清成 ""（`""` 是**合法值**）⇒
            #    那三个字段（name / state / ip_allowlist）各写一行留痕。
            ip_allowlist="198.51.100.7",
        )
        self.patch_json(
            DETAIL_TEMPLATE.format(pk=other.pk),
            {"name": "改了名的应用", "state": "disabled", "ip_allowlist": ""},
        )
        rotated = self.assert_ok(self.post_json(ROTATE_TEMPLATE.format(pk=app.pk), {}))
        app.refresh_from_db()
        self.assertEqual(self.logs_for("system.taker_api_app_update").count(), 3)

        self._assert_no_secret_anywhere(
            secrets=(first_secret, rotated["app_secret"], app.app_secret,
                     "taker-secret-00000000000000041"),
            actions=(
                "system.taker_api_app_create",
                "system.taker_api_app_update",
                "system.taker_api_app_rotate_secret",
            ),
        )

    def test_no_secret_ever_reaches_a_failure_response(self):
        """⭐ 密钥也不进**错误消息**：各类失败响应的**响应体**里都不许出现它。

        ⚠️ 只查**失败**响应：成功响应里按设计就有密钥（"明文可随时获取"），
        ⛔ 拿它当判据是自相矛盾（那种判据只能靠"哪条路径回了什么"来区分，很容易写成恒真）。
        """
        app = self.make_app(app_secret="taker-secret-00000000000000051")
        secret = app.app_secret
        failures = (
            # 匿名（401）
            self.get(LIST_PATH, client=self.anonymous),
            # 无节点（403）
            self.get(LIST_PATH, client=self.nobody),
            # 表外键（1000）
            self.post_json(LIST_PATH, {"name": APP_NAME, "ghost": secret}),
            # 取不到（1000）× 两条路径
            self.patch_json(DETAIL_TEMPLATE.format(pk=999999), {"name": APP_NAME}),
            self.post_json(ROTATE_TEMPLATE.format(pk=999999), {}),
            # 未定义方法（405）
            self.delete(DETAIL_TEMPLATE.format(pk=app.pk)),
        )
        for response in failures:
            with self.subTest(status=response.status_code):
                self.assertNotIn(
                    secret, response.content.decode("utf-8", "replace"),
                    "失败响应体里出现了密钥",
                )
        self.assertNotIn(secret, self._rows_text(OperationLog))
        self.assertEqual(self._secret_columns_hit(OperationLog, secret), [])

    def test_the_pure_no_ops_write_nothing_at_all(self):
        """对照：纯 no-op（`PATCH {}` / 重发同值）**一行都不写** ⇒ 也就无从泄密。"""
        app = self.make_app()
        before = OperationLog.objects.count()
        self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {})
        self.patch_json(
            DETAIL_TEMPLATE.format(pk=app.pk), {"ip_allowlist": app.ip_allowlist}
        )
        self.assertEqual(OperationLog.objects.count(), before, "no-op 不该写留痕")
        self.assertNotIn(app.app_secret, self._rows_text(OperationLog))


class CredentialGenerationTests(_TakerAppApiFixture):
    """⛔ **不抄第二份随机源**：打手侧的凭据生成口**就是**上家侧那两个函数对象。"""

    def test_the_view_uses_the_openapi_generators_themselves(self):
        """⭐ 判据取**函数对象的同一性**（`is`）：谁在打手侧另起一套随机源，本条当场红。

        ⚠️ 这不是风格判据：spec §5.3 的定值（16 位 hex 的 key、`new_token()` 出的 43 字符
        secret）只有一处出处；另起一套会让两条通道的强度与字符集**各自演进**，
        而症状是**没有任何症状**（两边的 key 看起来都像 key）。
        """
        self.assertIs(
            taker_app_views.generate_app_key, openapi_serializers.generate_app_key
        )
        self.assertIs(
            taker_app_views.generate_app_secret, openapi_serializers.generate_app_secret
        )

    def test_the_generated_credentials_have_the_frozen_shapes(self):
        """形状钉住：`app_key` = 16 位小写 hex；`app_secret` = 43 字符的 urlsafe。

        ⚠️ 形状取自**实测生成值**（跑一次读它给什么），⛔ 不是照文档抄一个数：
        两处（`APP_KEY_BYTES` 与 `new_token()`）都是既有定值，改了它们本条就红。
        """
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": APP_NAME, "taker": self.other_user.pk}
            )
        )
        self.assertRegex(data["app_key"], r"^[0-9a-f]{16}$")
        self.assertEqual(len(data["app_secret"]), 43)
        self.assertRegex(data["app_secret"], r"^[A-Za-z0-9_\-]+$")

    def test_a_wrong_credential_is_rejected_by_the_token_endpoint(self):
        """对照：**错的**凭据在取令牌端点上是 401（否则上面"密钥与库一致"那几条没有判别力）。"""
        app = self.make_app()
        response = self.token_request(
            app_key=app.app_key, app_secret="not-the-secret-0000000000000001"
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(
            set(json.loads(response.content)), {"error", "error_description"}
        )


class TakerAppMalformedInputTests(_TakerAppApiFixture):
    """补 `test_no_route_500` 的跳过面：带转换器的两条路由 × 畸形入参 ⇒ 不是 500。

    ⚠️ 那张网的显式路由腿按 Django 自己的事实**跳过带转换器的路由**，而本组四条里有一条
    （`apps/`）不带转换器 ⇒ 它落在网内；另外三条靠本文件补（与上家侧同一形状的已知盲区）。
    """

    def test_malformed_bodies_are_business_errors_not_500(self):
        """畸形入参 ⇒ **业务码 `1000`**（⛔ 不是 500、⛔ 也不是静默接受）。

        ⚠️ **`code` 键永远在**（`api_response()` 的信封恒有它）⇒ 只断 `"code" in body`
        是**恒真**判据（`E-0063` 同族）：实现被改成"畸形入参**静默接受**（`code=0`）"时，
        那种断言**照样绿**。所以每一格都断**那个码本身**（字面值，`E-0024`）。
        ⚠️ **一格例外、如实登记**：`rotate-secret` 那个端点**不读请求体**（spec §6 它没有入参）
        ⇒ 传什么它都成功（`code=0`）。那一格证的是「多余的 body **不 500**、
        也**不被误判成 `1000`**」⇒ 期望值写 `OK`（⛔ 不是把整条判据放宽成"非 0 即可"：
        另外几格仍然是字面 `1000`）。
        """
        app = self.make_app()
        cases = (
            ("patch", DETAIL_TEMPLATE, {"name": "x" * 500}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"state": "BOGUS"}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"ip_allowlist": ["not", "a", "string"]}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"ip_allowlist": "x" * 5000}, VALIDATION),
            ("patch", DETAIL_TEMPLATE, {"state": None}, VALIDATION),
            ("post", ROTATE_TEMPLATE, {"anything": "ignored"}, OK),
        )
        for method, template, payload, expected_code in cases:
            with self.subTest(method=method, payload=str(payload)[:40]):
                url = template.format(pk=app.pk)
                response = (
                    self.patch_json(url, payload)
                    if method == "patch"
                    else self.post_json(url, payload)
                )
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(
                    self.body(response)["code"], expected_code, response.content[:300]
                )

    def test_a_malformed_body_on_the_collection_is_not_500(self):
        """创建端点的畸形 body ⇒ 业务码 `1000`（⛔ 不是 500、⛔ 也不是静默建出一行）。

        ⚠️ 与上家侧同一条**实测**结论、同一条如实登记：DRF 的 `CharField.to_internal_value`
        对 `int` / `float` **不报错**，而是 `str(data)` ⇒ `{"name": 12345}` **不是**畸形入参，
        它会**真的建出一行**（业务码 `0`）。这一格如实钉住那条行为；
        ⛔ 不是把判据放宽成"非 0 即可"——另外两格仍是字面 `1000`。
        """
        for payload, expected_code in (
            ({"name": []}, VALIDATION),
            ({"name": {"a": 1}}, VALIDATION),
            # ⚠️ `taker` 必须一起给：管理员那一支**要求**它（缺 ⇒ `1000`），而本格要证的
            #    是"`12345` 真的建出了一行" ⇒ 少了它，这一格会以另一条判据的红出现。
            ({"name": 12345, "taker": self.other_user.pk}, OK),
        ):
            with self.subTest(payload=payload):
                response = self.post_json(LIST_PATH, payload)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(
                    self.body(response)["code"], expected_code, response.content[:300]
                )
