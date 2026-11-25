"""T3：企微群码**锁定应用的单例管理端点**的判据。

设计文档：`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §7.3 / §5.2，
决策 **D7**（单例资源形状）/ **D2**（条件唯一约束）/ **D9**（复用现成节点）。

被测对象：`apps/openapi/api/wecom_qr_app_views.py` 的四条端点 + `apps/openapi/urls.py`
里那三条 `path()`（`GET`/`POST`/`PATCH` 共用一条 `wecom-qr-app/`）。

## 本文件证什么（逐条都能失败，⛔ 不是"跑过就算"）

1. **未创建时 `GET` ⇒ `code=0` + `data is None`**（⛔ 不是 404、⛔ 不是 `1002`）；
   创建之后同一条 `GET` ⇒ 回**那一行**的行载荷（含明文 `app_secret`，§7.3 的口径）。
   ⚠️ 判据用 `is None`（⛔ 不用真值）：`data` 是一个 dict 时真值判**也**为真 ⇒ 拿真值判
   分不开"没有"与"有"，那条用例会恒真（E-0063 同族）。
2. **创建 ⇒ 恰好一行 `kind=wecom_group_qr`**，`app_key` / `app_secret` 都在响应里、
   且**等于库里那一行**；`name` 缺席时用默认名（`DEFAULT_APP_NAME`）。
3. **再创建 ⇒ `1000` + 库里恰好一行**（先查那一层闸）。
4. ⭐ **并发两个创建 ⇒ 恰好一行 + 没有 500**（**库级**条件唯一约束那一层闸）。
   造并发的方式见 `ConcurrentWecomAppCreateTests` 的类 docstring
   （真 `threading` + `Barrier`，`TransactionTestCase` 直连库）。
5. **跨租户**：A 建的那把，在 B 的域名下 `GET` ⇒ `data is None`（判据用**业务值**
   ——按 `app_key` / 应用名对拍，⛔ 不用主键，`AGENTS.md` §5-G）。
6. **权限**：匿名 ⇒ `401`；登录但**没有** `system.pinduoduo_settings` 节点 ⇒ `403`
   （照既有用例的 opt-out 写法：`make_logged_in_client` 不传 `nodes`）。
7. **`PATCH`**：改 `callback_url` / `callback_state` / `state` / `ip_allowlist` 读到新值
   （写进库、留痕各一条）；**纯 no-op 不写库**（判据 = 那一段里**没有** `UPDATE` 语句，
   见 `test_a_noop_patch_issues_no_update`，⛔ 不是拿 `updated` 那种不存在的列判）。
8. **轮换密钥**：`app_secret` 真的变了、`secret_rotated_at` 非空、留痕一条。
9. **台账端点**：造一条 `OpenApiDelivery` ⇒ 在返回里看得到那一行（键集与既有台账逐字相同）。
10. ⛔ **没有 `DELETE`**（有权限的管理员发 ⇒ **405**）。

## 夹具纪律

- 应用名 / 凭据 / 单号一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户、
  任何真实第三方无关。
- 回调地址用**文档保留段的公网 IP 字面量**（`93.184.216.34`，与
  `tests/test_openapi_callback_config.py` 同一个值）：`validate_callback_url` 会真的解域名、
  真域名会让用例依赖 DNS ⇒ 字面量不解析、也不碰网。
- 跨租户那条的第二个租户在 `setUpClass` 里建（schema 的 DDL 不能回滚在 `TestCase` 的事务里，
  先例 = `tests/test_openapi_delivery_admin.py` 的同一段）。
"""
import json
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    tenant_context,
)

from apps.audit.models import OperationLog
from apps.openapi.api import wecom_qr_app_views
from apps.openapi.models import ApiApp, ApiAppKind, OpenApiDelivery
from apps.platform.provisioning import provision_tenant
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

User = get_user_model()

#: 应用管理的节点门（spec D9：**复用**现成的 `system.pinduoduo_settings`，⛔ 不新增节点）。
APP_MANAGE_NODE = "system.pinduoduo_settings"

#: 三条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
SINGLETON_PATH = "/api/open/wecom-qr-app/"
ROTATE_PATH = "/api/open/wecom-qr-app/rotate-secret/"
DELIVERIES_PATH = "/api/open/wecom-qr-app/deliveries/"

#: 业务码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
UNAUTHENTICATED = 401
FORBIDDEN = 403
METHOD_NOT_ALLOWED = 405

#: `kind` 的机器值**写字面量**（⛔ 不从 `ApiAppKind` 取：那样"枚举被改坏"时用例照样绿）。
KIND_WECOM = "wecom_group_qr"
KIND_GENERAL = "general"

#: 合成夹具值（⛔ 不是任何真实租户 / 真实第三方的数据）。
APP_NAME = "测试企微群码应用甲"
OTHER_APP_NAME = "测试企微群码应用乙"
APP_KEY = "wecomqrappkey0000001"
APP_SECRET = "wecom-qr-secret-000000000000000001"
#: 跨租户对拍用的业务值（租户内唯一、两侧取值不同 ⇒ ⛔ 不用主键指认对方那一行）。
APP_KEY_B = "wecomqrappkey0000002"
APP_NAME_B = "测试企微群码应用乙（另一租户）"

#: 文档保留段的公网 IP 字面量（不解析、不碰网；与 `tests/test_openapi_callback_config.py` 同值）。
PUBLIC_HOST = "93.184.216.34"
CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook"
OTHER_CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook2"

#: 控制组用的两个合成名字（⛔ 与任何真实应用名无关）。
CTRL_NAME = "测试控制组应用丙"
CTRL_OTHER_NAME = "测试控制组应用丁"
#: 「名字被占了」那一档用的合成名字。
APP_NAME_TAKEN = "测试已被占用的名字"

#: 台账行的合成单号。
ORDER_NO = "20261009000000000031"

SCHEMA_B = "wecom_gqr_t3_b"
DOMAIN_B = "wecom-gqr-t3-b.test.local"
#: 并发那条用例自己的租户域名（`TransactionTestCase` 自搭夹具，见那个类）。
DOMAIN_TEST = "tenant.test.com"


def _all_keys(node, into=None):
    """递归收集载荷里的全部 dict 键（判"密钥在不在"必须递归，⛔ 不是只看顶层）。"""
    into = set() if into is None else into
    if isinstance(node, dict):
        for key, value in node.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, into)
    return into


class _WecomQrAppFixture(TenantTestCase):
    """三条端点的共用夹具：一个持节点的管理员 + 造物 / 发请求 / 断言工具。"""

    def setUp(self):
        super().setUp()
        seed_catalog()
        self.domain_a = self.domain.domain
        self.admin = make_logged_in_client(
            username="wecom-qr-admin", groups=("客服",), nodes=(APP_MANAGE_NODE,)
        )
        # 没有该节点的那一位：**显式 opt-out**（⛔ 不传 `nodes`，见 `AGENTS.md` §5-G）。
        self.nobody = make_logged_in_client(username="wecom-qr-nobody")
        self.anonymous = Client(SERVER_NAME=self.domain_a)
        self.creator = User.objects.get(username="wecom-qr-admin")

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_app(self, *, name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET):
        """直接经 ORM 造那把锁定应用（端点的 `POST` 另有专门用例覆盖）。"""
        return ApiApp.objects.create(
            name=name,
            kind=ApiAppKind.WECOM_GROUP_QR,
            app_key=app_key,
            app_secret=app_secret,
            created_by=self.creator,
        )

    def make_general_app(self, *, name="通用应用甲", app_key="generalkey000000001"):
        """造一把**通用**应用（判据 8 的对照：`kind` 过滤真的按 kind 取）。"""
        return ApiApp.objects.create(
            name=name,
            kind=ApiAppKind.GENERAL,
            app_key=app_key,
            app_secret="general-secret-00000000000000001",
            created_by=self.creator,
        )

    def make_delivery(self, app, *, status=OpenApiDelivery.Status.FAILED):
        return OpenApiDelivery.objects.create(
            app=app,
            event=OpenApiDelivery.Event.ORDER_STATUS_CHANGED,
            order_no=ORDER_NO,
            source_order_no="TT-T3-0001",
            payload={},
            status=status,
            attempts=2,
            reason="对方返回 503，稍后重试",
            response_status=503,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def get(self, url, *, client=None, data=None):
        return (client or self.admin).get(url, data or {})

    def post_json(self, url, payload=None, *, client=None):
        return (client or self.admin).post(
            url,
            data=json.dumps({} if payload is None else payload),
            content_type="application/json",
        )

    def patch_json(self, url, payload, *, client=None):
        return (client or self.admin).patch(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def delete(self, url, *, client=None):
        return (client or self.admin).delete(url)

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_business_failure(self, response, code, *, msg_fragment=None):
        """业务失败：**HTTP 200** + 指定业务码（本仓口径：状态码只表达传输 / 认证层）。

        ⚠️ `code` 是承重入参（⛔ 不是装饰）：只断 msg 会让"把 1000 写成 1002"活下来。
        """
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        if msg_fragment is not None:
            self.assertIn(msg_fragment, payload["msg"], payload)
        return payload

    def wecom_rows(self):
        return ApiApp.objects.filter(kind=KIND_WECOM)

    def logs_for(self, action):
        return OperationLog.objects.filter(action=action)


# ═════════════════════════════════════════════════════════════════════════════
# 判据 1 / 2 / 3：未创建 ⇒ data=null；创建 ⇒ 一行；再创建 ⇒ 1000
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppSingletonReadTests(_WecomQrAppFixture):
    """判据 1：`GET` 的**两档答复**（不存在 ⇒ `null`；存在 ⇒ 那一行）。"""

    def test_get_before_creation_returns_null_not_an_error(self):
        """⭐ 未创建 ⇒ `code=0` + `data is None`（⛔ 不是 404、⛔ 不是 `1002`）。

        ⚠️ 判据用 `is None`（E-0063 / E-0021 同族）：`data` 是一个 dict 时真值判**也**为真
        ⇒ 拿真值判分不开"没有"与"有"，这条用例会退化成恒真。
        """
        self.assertEqual(self.wecom_rows().count(), 0)
        data = self.assert_ok(self.get(SINGLETON_PATH))
        self.assertIsNone(data)

    def test_get_returns_only_the_wecom_app(self):
        """⭐ 有这把 ⇒ 回它；同租户另有一把**通用**应用时**不许**串台（按 `kind` 取数）。"""
        self.make_general_app()
        app = self.make_app()
        data = self.assert_ok(self.get(SINGLETON_PATH))
        self.assertEqual(data["id"], app.pk)
        self.assertEqual(data["app_key"], APP_KEY)
        self.assertEqual(data["name"], APP_NAME)

    def test_get_never_returns_the_general_app_when_there_is_no_wecom_app(self):
        """对照（能失败）：只有通用应用 ⇒ 仍回 `None`（⛔ 不是"随便挑一把"）。"""
        self.make_general_app()
        self.assertIsNone(self.assert_ok(self.get(SINGLETON_PATH)))


class WecomQrAppCreateTests(_WecomQrAppFixture):
    """判据 2 / 3：创建（含默认名、明文密钥）、重复创建 ⇒ `1000` + 恰好一行。"""

    def test_create_writes_one_wecom_row_and_returns_the_plaintext_secret(self):
        """创建 ⇒ 一行 `kind=wecom_group_qr`；`app_key` / `app_secret` 都在响应里、**等于库里那一行**。"""
        data = self.assert_ok(self.post_json(SINGLETON_PATH, {}))
        rows = list(self.wecom_rows())
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.kind, KIND_WECOM)
        self.assertEqual(row.created_by_id, self.creator.pk)
        # 响应里的凭据 = 库里那一行（⛔ 不是服务端另算一份）
        self.assertEqual(data["app_key"], row.app_key)
        self.assertEqual(data["app_secret"], row.app_secret)
        self.assertEqual(row.kind, KIND_WECOM)
        # 默认名（`DEFAULT_APP_NAME`：入参缺席时的定值）
        self.assertEqual(row.name, wecom_qr_app_views.DEFAULT_APP_NAME)
        # 状态与回调的模型默认值没有被本端点改掉
        self.assertEqual(data["state"], "enabled")
        self.assertEqual(data["callback_state"], "enabled")
        self.assertEqual(data["callback_url"], "")

    def test_create_accepts_an_explicit_name(self):
        """可选入参 `name`（≤50）真的生效。"""
        data = self.assert_ok(self.post_json(SINGLETON_PATH, {"name": APP_NAME}))
        self.assertEqual(data["name"], APP_NAME)
        self.assertEqual(self.wecom_rows().get().name, APP_NAME)

    def test_create_rejects_a_too_long_name(self):
        """超长名 ⇒ `1000`（与既有创建端点同一道界），且**一行都没落**。"""
        self.assert_business_failure(
            self.post_json(SINGLETON_PATH, {"name": "x" * 51}), VALIDATION
        )
        self.assertEqual(self.wecom_rows().count(), 0)

    def test_second_create_is_a_business_error_with_exactly_one_row(self):
        """⭐ 再创建 ⇒ `1000` + 一句人话 + 库里**恰好一行**（先查那一层闸）。"""
        first = self.assert_ok(self.post_json(SINGLETON_PATH, {}))
        second = self.post_json(SINGLETON_PATH, {"name": OTHER_APP_NAME})
        self.assert_business_failure(
            second, VALIDATION,
            msg_fragment=wecom_qr_app_views.WECOM_APP_EXISTS_MSG,
        )
        self.assertEqual(self.wecom_rows().count(), 1)
        # 第二把**没有**落库（⛔ 不是"落了再删"）：那一行还是第一次那把
        row = self.wecom_rows().get()
        self.assertEqual(row.app_key, first["app_key"])
        self.assertEqual(row.name, wecom_qr_app_views.DEFAULT_APP_NAME)

    def test_create_writes_exactly_one_audit_row_with_the_key_but_not_the_secret(self):
        """留痕：复用 `system.open_api_app_create`（⛔ 不新增动作码），给 `app_key`、⛔ 无密钥。

        ⚠️ 判据打在**数据库行**上（E-0009：不打断言中间层），并逐列扫一遍**不许出现密钥**。
        """
        data = self.assert_ok(self.post_json(SINGLETON_PATH, {}))
        rows = list(self.logs_for("system.open_api_app_create"))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.target_kind, "api_app")
        self.assertEqual(str(row.target_id), str(self.wecom_rows().get().pk))
        self.assertEqual(row.to_value, data["app_key"])
        # 密钥一个字都不进留痕（凭据纪律）：把那一行的文本列拼起来扫
        text = " ".join(
            str(getattr(row, field)) for field in ("target_name", "from_value", "to_value", "summary")
        )
        self.assertNotIn(data["app_secret"], text)

    def test_the_second_create_writes_no_extra_audit_row(self):
        """重复创建被拒 ⇒ ⛔ 不留痕（⛔ 不能在日志里留一条"建了一把"的假话）。"""
        self.assert_ok(self.post_json(SINGLETON_PATH, {}))
        self.assert_business_failure(self.post_json(SINGLETON_PATH, {}), VALIDATION)
        self.assertEqual(self.logs_for("system.open_api_app_create").count(), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 判据 4：并发两个创建 ⇒ 恰好一行 + 没有 500（**库级**那一层闸）
# ═════════════════════════════════════════════════════════════════════════════

class ConcurrentWecomAppCreateTests(TransactionTestCase):
    """⭐ 判据 4：两个**同时**打到创建端点的请求 ⇒ 恰好一行 + 没有 500。

    ## 并发是怎么造的（照 `tests/test_account_concurrency.py` 的既有配方，⛔ 不另立一套）

    - 两个真 `threading.Thread`，各拿一个自带登录会话的 `Client`；
    - **每线程一条自己的连接**：线程入口 `close_old_connections()` +
      `connection.set_tenant(...)`，出口 `connection.close()`。Django 的连接与
      租户上下文都是**按线程**的（`django.db.connections` 是 `thread_critical`），
      不在线程里设租户就会落到 `public` 上找一个不存在的表（症状是 500，看起来像实现坏了）；
    - ⭐ **同步点打在「即将 INSERT」的前一刻**（`_BarrierBeforeTheInsert`，照
      `test_account_concurrency.py::_BarrierBeforeApplyStatus` 的既有手法）：
      两条线程先在栅栏上对齐，**再**各自插。理由（实测逼出来的）：
      光有"同时出发"的起跑线**不够** —— 先查那一层闸（`_fetch_wecom_app()`）是一条 SELECT，
      两条线程完全可能被调度成"甲查完、甲插完并提交、乙才查" ⇒ 乙走的是**第 1 层**闸，
      第 2 层的 `IntegrityError` 翻译**根本没被执行**，而用例照样绿（绿的并发测试等于没测并发）。
      在 INSERT 前一刻对齐 ⇒ 「两条 SELECT 都已完成、两条 INSERT 都还没开始」是**构造出来的**。
    - ⚠️ **补丁不改变任何判据**：它在原 `create` 之前加一个同步点，别的什么都不做
      （插的还是同一个函数、参数一字不改）⇒ 不会把"实现正确"变成"测试通过"。

    ## 为什么必须是 `TransactionTestCase`（⛔ 别挪回 `TenantTestCase`）

    `TenantTestCase` 继承 `django.test.TestCase` ⇒ 每条用例包在一个 atomic 块里。
    工作线程的连接**看不见**那个未提交的事务，而且两个请求的插入会各自**等**主事务
    释放那个条件唯一索引 ⇒ 用例变成"跑很久然后超时 / 恰好都成功"，**不是**在测约束。
    `TransactionTestCase` 直连库（不用外层事务），两个插入才真的会撞索引
    （先例 = `tests/test_account_concurrency.py` 的同一段理由；`setUp` 里有一条
    `get_autocommit()` 自证，基类被换掉时它当场红）。
    ⚠️ `django_tenants` **没有** `TenantTransactionTestCase`（读码：
    `django_tenants/test/cases.py` 只有 `TestCase` 版）⇒ 夹具自己搭一遍。

    ## 判据的读法

    - 两个响应都必须是 **HTTP 200**（业务失败也是 200，⛔ **没有 500**）；
    - 业务码**恰好一个 0、一个 1000**（⛔ 不是"两个都 0"、也⛔ 不是"两个都 1000"）；
    - 库里 `kind=wecom_group_qr` **恰好一行**。

    ⚠️ **本用例对"去掉先查那一层闸"这个变异仍然是绿的**（变异 A 的读数见报告）：
    第 2 层独立承担这条判据 —— 这正是它存在的理由。
    """

    BARRIER_TIMEOUT = 15.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase.setUpClass` / `tearDownClass` 的等价物（见类 docstring）。
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=DOMAIN_TEST)
        cls.domain.save()
        if DOMAIN_TEST not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [DOMAIN_TEST]
        connection.set_tenant(cls.tenant)
        seed_catalog()

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if DOMAIN_TEST in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN_TEST)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # 前提自证：本用例确实跑在**无外层事务**的形态上（见类 docstring）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        self.client_a = make_logged_in_client(
            domain=DOMAIN_TEST, username="wecom-qr-concurrent-a",
            groups=("客服",), nodes=(APP_MANAGE_NODE,),
        )
        self.client_b = make_logged_in_client(
            domain=DOMAIN_TEST, username="wecom-qr-concurrent-b",
            groups=("客服",), nodes=(APP_MANAGE_NODE,),
        )

    def _post_in_this_thread(self, client, results, key, *, barrier=None):
        """在**本线程自己的连接**上发一次创建请求；结果写进 `results[key]`。

        ⚠️ 线程里抛的异常也要带回主线程断言（否则它会静默消失、用例变成假绿）。
        """
        try:
            close_old_connections()
            connection.set_tenant(self.tenant)
            if barrier is not None:
                barrier.wait(timeout=self.BARRIER_TIMEOUT)
            response = client.post(
                SINGLETON_PATH, data=json.dumps({}), content_type="application/json"
            )
            results[key] = {"status_code": response.status_code, **json.loads(response.content)}
        except Exception as exc:  # noqa: BLE001 — 结果要回主线程断言
            results[key] = {"exception": f"{type(exc).__name__}: {exc}"}
        finally:
            connection.close()

    def test_two_concurrent_creates_yield_one_row_and_no_500(self):
        from unittest import mock

        barrier = threading.Barrier(2)
        results = {}
        original_create = ApiApp.objects.create

        def _create_aligned(*args, **kwargs):
            """把「即将 INSERT」这一刻与另一条线程对齐（见类 docstring）。"""
            barrier.wait(timeout=self.BARRIER_TIMEOUT)
            return original_create(*args, **kwargs)

        client_a, client_b = self.client_a, self.client_b
        with mock.patch.object(ApiApp.objects, "create", _create_aligned):
            threads = [
                threading.Thread(
                    target=self._post_in_this_thread, args=(client_a, results, "a")
                ),
                threading.Thread(
                    target=self._post_in_this_thread, args=(client_b, results, "b")
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        # 夹具自证：两条线程都回了结果（否则下面的码集合断言只是"少了一条"）
        self.assertEqual(sorted(results), ["a", "b"], results)
        for key in ("a", "b"):
            self.assertNotIn("exception", results[key], results[key])
            # ⛔ 判据一：没有 500（"约束翻译真的在干活"的直接读数）
            self.assertNotEqual(results[key]["status_code"], 500, results[key])
            self.assertEqual(results[key]["status_code"], 200, results[key])
        # ⛔ 判据二：恰好一个成功、一个业务失败
        #   （两个都成功 = 约束没拦住；两个都失败 = 路径不通）
        codes = sorted(results[key]["code"] for key in ("a", "b"))
        self.assertEqual(codes, [OK, VALIDATION], results)
        # ⛔ 判据三：库里恰好一行
        self.assertEqual(ApiApp.objects.filter(kind=KIND_WECOM).count(), 1)

    def test_the_sync_point_really_fires_twice(self):
        """控制组：同步点必须被**两条**线程各命中一次（⛔ 不是"只有一条真的插了"）。

        ⚠️ 没有它，"两条请求恰好一条成功"可能是"另一条压根没走到 INSERT"凑出来的
        （E-0004 同族：否定性结论要有能分辨竞争假设的对照）。
        判据 = 计数器 + 栅栏的**同时到达**性质：两条线程都进了包装器才会双双通过栅栏，
        有一条没进 ⇒ `BrokenBarrierError` ⇒ 那条线程以 `exception` 落进 `results` ⇒ 本用例红。
        """
        from unittest import mock

        barrier = threading.Barrier(2)
        hits = []
        lock = threading.Lock()
        results = {}
        original_create = ApiApp.objects.create

        def _create_counted(*args, **kwargs):
            with lock:
                hits.append(1)
            barrier.wait(timeout=self.BARRIER_TIMEOUT)
            return original_create(*args, **kwargs)

        client_a, client_b = self.client_a, self.client_b
        with mock.patch.object(ApiApp.objects, "create", _create_counted):
            threads = [
                threading.Thread(
                    target=self._post_in_this_thread, args=(client_a, results, "a")
                ),
                threading.Thread(
                    target=self._post_in_this_thread, args=(client_b, results, "b")
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)

        self.assertEqual(sorted(results), ["a", "b"], results)
        for key in ("a", "b"):
            self.assertNotIn("exception", results[key], results[key])
        # ⭐ 控制组的读数：两**条**线程都到了 INSERT 那一刻，而且只插了一次
        self.assertEqual(len(hits), 2, hits)
        self.assertEqual(sorted(results[key]["code"] for key in ("a", "b")), [OK, VALIDATION])
        self.assertEqual(ApiApp.objects.filter(kind=KIND_WECOM).count(), 1)


def _name_taken_integrity_error(creator):
    """造一个**撞 `ApiApp.name` 唯一索引**的 `IntegrityError`（通用应用重名）。

    控制组用：`_translate_create_conflict` 必须把它翻成 `APP_NAME_TAKEN_MSG`。
    ⚠️ 这条路的现场 = **两个并发创建都用默认名**：先到的那个把名字占住了，
    后到的那个先撞 `name`（两条约束都会命中，PostgreSQL 报哪一条不是契约）
    —— 业务方特意点名的那一档。
    """
    ApiApp.objects.create(
        name=APP_NAME_TAKEN, kind=ApiAppKind.GENERAL, app_key="ctrl-key-00000000001",
        app_secret="ctrl-secret-00000000000001", created_by=creator,
    )
    try:
        # ⚠️ **保存点**：失败那一笔只回滚自己，事务本身仍然可用
        #    （否则 PostgreSQL 报 `current transaction is aborted`，下面的回查会是 ERROR 不是断言失败）。
        with transaction.atomic():
            ApiApp.objects.create(
                name=APP_NAME_TAKEN, kind=ApiAppKind.WECOM_GROUP_QR,
                app_key="ctrl-key-00000000002",
                app_secret="ctrl-secret-00000000000002", created_by=creator,
            )
    except IntegrityError as exc:
        return exc
    raise AssertionError("夹具没有造出 IntegrityError（用例本身失效）")


def _key_taken_integrity_error(creator):
    """造一个**撞 `ApiApp.app_key` 唯一索引**的 `IntegrityError`（既不是 kind、也不是 name）。

    控制组用：这一类**必须原样抛**（未预期异常照样 500，E-0010）。
    """
    ApiApp.objects.create(
        name=CTRL_NAME, kind=ApiAppKind.GENERAL, app_key=APP_KEY,
        app_secret="ctrl-secret-00000000000001", created_by=creator,
    )
    try:
        with transaction.atomic():  # 保存点，理由同上
            ApiApp.objects.create(
                name=CTRL_OTHER_NAME, kind=ApiAppKind.GENERAL, app_key=APP_KEY,
                app_secret="ctrl-secret-00000000000002", created_by=creator,
            )
    except IntegrityError as exc:
        return exc
    raise AssertionError("夹具没有造出 IntegrityError（用例本身失效）")


def _conditional_unique_integrity_error(creator):
    """造一个**是**条件唯一约束的 `IntegrityError`（同租户第二把 `wecom_group_qr`）。

    ⚠️ 先落第一把（成功），第二把必须撞 `uniq_apiapp_wecom_qr` —— 造不出来就是夹具失效。
    """
    ApiApp.objects.create(
        name=APP_NAME, kind=ApiAppKind.WECOM_GROUP_QR, app_key=APP_KEY,
        app_secret=APP_SECRET, created_by=creator,
    )
    try:
        with transaction.atomic():  # 保存点，理由同上
            ApiApp.objects.create(
                name=OTHER_APP_NAME, kind=ApiAppKind.WECOM_GROUP_QR, app_key=APP_KEY_B,
                app_secret="wecom-qr-secret-000000000000000002", created_by=creator,
            )
    except IntegrityError as exc:
        return exc
    raise AssertionError("夹具没有造出 IntegrityError（用例本身失效）")


# ═════════════════════════════════════════════════════════════════════════════
# 判据 5：跨租户（判据用业务值，⛔ 不用主键）
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppCrossTenantTests(_WecomQrAppFixture):
    """判据 5：A 建的那把，在 B 的域名下 `GET` ⇒ `data is None`。

    ⚠️ 判据**不按主键**指认对方那一行（`AGENTS.md` §5-G：两个 schema 的主键序列各自
    从 1 开始 ⇒ `filter(pk=…)` 在 B 里命中的是 B 自己的行，那种断言恒真或恒假）。
    这里用**租户内唯一、两侧取值不同**的业务值（`app_key` / 应用名）对拍，
    并额外做一次**换库读数相反**的自证。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="企微群码隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：上一条用例的跨域名请求会把连接留在那个 schema 上。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.app_a = self.make_app()
        try:
            with tenant_context(self.tenant_b):
                seed_catalog()
                # ⚠️ 两条都踩过，写在这里免得下一个人再踩：
                #   ① `get_or_create` + `defaults={"password": …}` 建出来的是**不可用密码**
                #      的账号 ⇒ 登录 401，而症状看起来像"租户 B 的路由不通"；
                #   ② 密码必须是 `make_logged_in_client` 的**默认**那个
                #      （它不把密码当参数传，见 `P2_TEST_PASSWORD`）—— 自己编一个同样 401。
                creator_b = User.objects.create_user(
                    username="wecom-iso-b", password=P2_TEST_PASSWORD
                )
                self.app_b = ApiApp.objects.create(
                    name=APP_NAME_B, kind=ApiAppKind.WECOM_GROUP_QR, app_key=APP_KEY_B,
                    app_secret="wecom-qr-secret-b-000000000000001", created_by=creator_b,
                )
                # ⚠️ **必须在 `tenant_context` 里建会话**（先例 = `test_openapi_delivery_admin.py`
                #   的同一段）：`make_logged_in_client` 会在**当前 schema** 里建账号并真的登录一次
                #   ⇒ 放到 `with` 外面建出来的账号落在**租户 A** 的库里，而登录请求按 Host 路由到
                #   B ⇒ 401「用户名或密码错误」（症状看起来像"B 的域名不通"）。
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B, username="wecom-iso-b-admin", groups=("客服",),
                    nodes=(APP_MANAGE_NODE,),
                )
        finally:
            connection.set_tenant(self.tenant)
        # ── 构造物自证：两把**都在**、且**各在**自己的库里（否则下面的负断言是空转，E-0004）
        self.assertEqual(self._here_by_key(APP_KEY), 1)
        self.assertEqual(self._here_by_key(APP_KEY_B), 0)
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._here_by_key(APP_KEY_B), 1)
            self.assertEqual(self._here_by_key(APP_KEY), 0)
        finally:
            connection.set_tenant(self.tenant)

    def _here_by_key(self, key):
        return ApiApp.objects.filter(app_key=key).count()

    def test_each_domain_sees_only_its_own_row(self):
        """⭐ 判据 5 的**正向那一半**：A 的域名 + A 的会话 ⇒ 回 **A 那把**（⛔ 不是 B 的）。

        ⚠️ 两侧 `app_key` **取值不同**（`APP_KEY` vs `APP_KEY_B`）⇒ 这条断言同时钉住了
        "取到的是哪一库的那一行"；判据用的是**业务值**，⛔ 不是主键（`AGENTS.md` §5-G）。
        """
        data = self.assert_ok(self.admin.get(SINGLETON_PATH))
        self.assertEqual(data["app_key"], APP_KEY)
        self.assertEqual(data["name"], APP_NAME)
        # 自证两把的取值确实不同（否则上一条断言区分不出任何东西）
        self.assertNotEqual(APP_KEY, APP_KEY_B)

    def test_tenant_bs_own_domain_returns_tenant_bs_row(self):
        """⭐ 判据 5 的**另一半**：B 的域名 + B 的会话 ⇒ 回 **B 那把**。

        没有它，"A 那条回了 A 的行"可能是"这条路只认 A"凑出来的（E-0004 同族）。
        """
        data = self.assert_ok(self.client_b.get(SINGLETON_PATH))
        self.assertEqual(data["app_key"], APP_KEY_B)
        self.assertEqual(data["name"], APP_NAME_B)

    def test_tenant_as_session_on_tenant_bs_domain_is_rejected(self):
        """⭐ 跨租户那一半：**A 的会话打 B 的域名** ⇒ 拒（⛔ 不是"回 B 的那一行"）。

        ⚠️ 手法：同一份会话（`self.admin`）+ 把请求的 Host 改成 B 的域名
        （`self.admin.defaults["SERVER_NAME"]`，`django.test.Client` 的公开属性）。
        ⚠️ 拒的具体形状是 **401「会话与当前租户不匹配」**——
        `TenantBoundAuthentication._assert_plane` 的真实行为（会话里的 `identity_plane`
        ≠ 当前 schema ⇒ `AuthenticationFailed`）。⛔ 本用例**不**按"应该是 1000 / 403"
        那种猜测写，而是把**实测**到的码写死；症状若变成"回 B 的那一行"，这条当场红。

        ⚠️ 它与上面两条**合起来**才是一句完整的话：换域名**拿不到别人的行**，
        而且这件事是**认证层**保证的（不是这一层"取数时顺手过滤掉了"）。
        """
        original = self.admin.defaults["SERVER_NAME"]
        try:
            self.admin.defaults["SERVER_NAME"] = DOMAIN_B
            response = self.admin.get(SINGLETON_PATH)
        finally:
            self.admin.defaults["SERVER_NAME"] = original
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], UNAUTHENTICATED)
        self.assertIsNone(payload["data"])
        self.assertIn("会话与当前租户不匹配", payload["msg"])

    def test_the_business_value_criterion_flips_when_the_schema_changes(self):
        """判别力自证：同一句判据**换库**后读数相反（恒真的判据做不到这件事）。"""
        self.assertEqual(self._here_by_key(APP_KEY), 1)
        self.assertEqual(self._here_by_key(APP_KEY_B), 0)
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._here_by_key(APP_KEY_B), 1)
            self.assertEqual(self._here_by_key(APP_KEY), 0)
        finally:
            connection.set_tenant(self.tenant)


# ═════════════════════════════════════════════════════════════════════════════
# 判据 6：认证 / 权限 / 没有 DELETE
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppAccessTests(_WecomQrAppFixture):
    """判据 6 / 10：匿名 ⇒ 401、无节点 ⇒ 403、⛔ 没有 DELETE。"""

    def test_anonymous_requests_are_401(self):
        """匿名 ⇒ **401**（⛔ 不是 403）：DRF 对"没有任何认证类受理"的请求回 `NotAuthenticated`。"""
        calls = (
            ("get", SINGLETON_PATH),
            ("post", SINGLETON_PATH),
            ("patch", SINGLETON_PATH),
            ("post", ROTATE_PATH),
            ("get", DELIVERIES_PATH),
        )
        for method, url in calls:
            with self.subTest(method=method, url=url):
                if method == "get":
                    response = self.anonymous.get(url)
                else:
                    response = getattr(self.anonymous, method)(
                        url, data="{}", content_type="application/json"
                    )
                self.assertEqual(response.status_code, UNAUTHENTICATED,
                                 response.content[:300])
                self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_logged_in_account_without_the_node_is_403(self):
        """登录但**没有** `system.pinduoduo_settings` ⇒ **403**（D9 的节点门真的在拦）。"""
        self.make_app()
        calls = (
            ("get", SINGLETON_PATH),
            ("post", SINGLETON_PATH),
            ("patch", SINGLETON_PATH),
            ("post", ROTATE_PATH),
            ("get", DELIVERIES_PATH),
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
        """对照（能失败）：**有**这个节点的账号 ⇒ 单例读 200。"""
        self.make_app()
        self.assert_ok(self.get(SINGLETON_PATH))

    def test_a_valid_api_credential_is_not_a_session(self):
        """D13 的交叉判据：对外那把凭据打**内部**端点 ⇒ **401**（⛔ 两组不要混）。"""
        from apps.openapi import tokens

        app = self.make_app()
        token = tokens.issue(app.app_key, app.app_secret)
        response = self.anonymous.get(SINGLETON_PATH, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertIsNone(self.body(response)["data"])

    def test_no_delete_route_returns_405(self):
        """⛔ **没有 `DELETE`**：三条路径上，**有权限的管理员**发 `DELETE` 都回 **405**。

        ⚠️ 405 是「**认证 + 权限都过了之后**」的读数（DRF 的 405 判定排在 `initial()` 之后）
        ⇒ 匿名 / 无节点的那两条边界是 401 / 403，见上面两条用例。
        """
        self.make_app()
        for url in (SINGLETON_PATH, ROTATE_PATH, DELIVERIES_PATH):
            with self.subTest(url=url):
                response = self.delete(url)
                self.assertEqual(response.status_code, METHOD_NOT_ALLOWED,
                                 response.content[:300])
        # 405 不是 404：路径真的存在（上面那三条 GET/POST 都通）——判据在别处已有，
        # 这里补一条最直接的：单例路径的 GET 是 200。
        self.assertEqual(self.get(SINGLETON_PATH).status_code, 200)


# ═════════════════════════════════════════════════════════════════════════════
# 判据 7：PATCH（改值 / 纯 no-op 不写库）
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppPatchTests(_WecomQrAppFixture):
    """判据 7：四个字段能改、缺席保持原值、**纯 no-op 不写库**、留痕一条。"""

    def test_patch_updates_callback_url_and_callback_state(self):
        """⭐ 改 `callback_url` 与 `callback_state`，读到新值（且都真的落库）。"""
        app = self.make_app()
        data = self.assert_ok(
            self.patch_json(
                SINGLETON_PATH,
                {"callback_url": CALLBACK_URL, "callback_state": "disabled"},
            )
        )
        self.assertEqual(data["callback_url"], CALLBACK_URL)
        self.assertEqual(data["callback_state"], "disabled")
        app.refresh_from_db()
        self.assertEqual(app.callback_url, CALLBACK_URL)
        self.assertEqual(app.callback_state, "disabled")
        # 留痕：每个真正变化的字段各一条，动作码复用 `system.open_api_app_update`
        rows = list(self.logs_for("system.open_api_app_update"))
        self.assertEqual(sorted(row.field_name for row in rows),
                         ["callback_state", "callback_url"])

    def test_patch_leaves_absent_fields_untouched(self):
        """缺席 = **保持原值**（PATCH 语义）：只给 `state` ⇒ 回调那两项一个字不动。"""
        app = self.make_app()
        ApiApp.objects.filter(pk=app.pk).update(
            callback_url=OTHER_CALLBACK_URL, ip_allowlist="203.0.113.7"
        )
        self.assert_ok(self.patch_json(SINGLETON_PATH, {"state": "disabled"}))
        app.refresh_from_db()
        self.assertEqual(app.state, "disabled")
        self.assertEqual(app.callback_url, OTHER_CALLBACK_URL)
        self.assertEqual(app.ip_allowlist, "203.0.113.7")

    def test_patch_clears_the_callback_url_with_an_empty_string(self):
        """⭐ 空串是**合法值**（= 清掉回调）⇒ 判据必须是 `"字段" in data`（⛔ 不是真值）。"""
        app = ApiApp.objects.create(
            name=APP_NAME, kind=ApiAppKind.WECOM_GROUP_QR, app_key=APP_KEY,
            app_secret=APP_SECRET, created_by=self.creator, callback_url=CALLBACK_URL,
        )
        data = self.assert_ok(self.patch_json(SINGLETON_PATH, {"callback_url": ""}))
        self.assertEqual(data["callback_url"], "")
        app.refresh_from_db()
        self.assertEqual(app.callback_url, "")

    def test_a_noop_patch_issues_no_update(self):
        """⭐ **纯 no-op PATCH 不写库**：判据 = 那一段里**没有 `UPDATE` 语句**。

        ⚠️ 为什么不用"对比 `updated` 那类时间戳列"：`ApiApp` **没有** `updated_at`
        ⇒ 那种判据会当场报 `AttributeError`（看起来像实现坏了）。最直接的判据是
        **SQL 层**：拿 `CaptureQueriesContext` 抓这一段真正发出去的语句，
        断言其中没有对 `openapi_apiapp` 的 `UPDATE`（E-0009 的口径：打在真实出口上）。
        ⚠️ 同时断言重发**同值**也是 no-op（`PATCH {"callback_url": 同一个值}`）
        —— 两者都是"全仓口径 R26：不能在日志里留一条假话"的现场。
        """
        app = ApiApp.objects.create(
            name=APP_NAME, kind=ApiAppKind.WECOM_GROUP_QR, app_key=APP_KEY,
            app_secret=APP_SECRET, created_by=self.creator, callback_url=CALLBACK_URL,
        )
        before = self.logs_for("system.open_api_app_update").count()

        with CaptureQueriesContext(connection) as captured:
            data = self.assert_ok(self.patch_json(SINGLETON_PATH, {}))
            self.assert_ok(
                self.patch_json(SINGLETON_PATH, {"callback_url": CALLBACK_URL})
            )
        updates = [
            query["sql"] for query in captured.captured_queries
            if "openapi_apiapp" in query["sql"] and query["sql"].lstrip().upper().startswith("UPDATE")
        ]
        self.assertEqual(updates, [], updates)
        self.assertEqual(self.logs_for("system.open_api_app_update").count(), before)
        # 回的是当前行（no-op 也要有可用的答复，⛔ 不是空体）
        self.assertEqual(data["app_key"], APP_KEY)
        app.refresh_from_db()
        self.assertEqual(app.callback_url, CALLBACK_URL)

    def test_patch_on_a_tenant_without_the_app_is_a_business_error(self):
        """没创建那把 ⇒ `PATCH` 回 `1000`「应用不存在」（与 `GET` 的 `null` **刻意不同**：
        一条是读、一条是写；对不存在的资源写没有定义）。"""
        self.assert_business_failure(
            self.patch_json(SINGLETON_PATH, {"state": "disabled"}),
            VALIDATION,
            msg_fragment="应用不存在",
        )

    def test_a_too_long_ip_allowlist_is_a_business_error_not_a_500(self):
        """请求来源的值进列之前有一道界（E-0010）：超长 ⇒ `1000`，⛔ 不是 500。"""
        self.make_app()
        self.assert_business_failure(
            self.patch_json(SINGLETON_PATH, {"ip_allowlist": "x" * 2001}), VALIDATION
        )


# ═════════════════════════════════════════════════════════════════════════════
# 判据 8：轮换密钥
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppRotateSecretTests(_WecomQrAppFixture):
    """判据 8：`app_secret` 变了、`secret_rotated_at` 非空、留痕一条。"""

    def test_rotate_changes_the_secret_and_stamps_the_time(self):
        app = self.make_app()
        self.assertIsNone(app.secret_rotated_at)
        data = self.assert_ok(self.post_json(ROTATE_PATH, {}))
        app.refresh_from_db()
        self.assertNotEqual(data["app_secret"], APP_SECRET)
        self.assertEqual(data["app_secret"], app.app_secret)
        self.assertIsNotNone(app.secret_rotated_at)
        self.assertTrue(data["secret_rotated_at"])
        # `app_key` **不变**（换的是密钥，不是身份）
        self.assertEqual(data["app_key"], APP_KEY)
        rows = list(self.logs_for("system.open_api_app_rotate_secret"))
        self.assertEqual(len(rows), 1, rows)
        # 新旧密钥都不进留痕
        text = " ".join(str(getattr(rows[0], f)) for f in ("target_name", "from_value", "to_value"))
        self.assertNotIn(APP_SECRET, text)
        self.assertNotIn(data["app_secret"], text)

    def test_rotate_before_creation_is_a_business_error(self):
        self.assert_business_failure(
            self.post_json(ROTATE_PATH, {}), VALIDATION, msg_fragment="应用不存在"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 判据 9：推送台账
# ═════════════════════════════════════════════════════════════════════════════

class WecomQrAppDeliveriesTests(_WecomQrAppFixture):
    """判据 9：台账端点能看到那把应用的推送行，键集与既有台账**逐字相同**。"""

    #: 列表 item 的键集——**逐条字面量**（⛔ 不从 `delivery_row_payload` 派生：
    #: 派生是自指恒真，装配函数被改坏时它照样绿，E-0024 / E-0065）。
    ITEM_KEYS = (
        "attempts", "created_at", "event", "event_text", "id", "next_attempt_at",
        "order_no", "processed_at", "reason", "response_status", "source_order_no",
        "status", "status_text",
    )

    def test_the_delivery_row_is_visible_with_the_contract_keys(self):
        app = self.make_app()
        self.make_delivery(app)
        data = self.assert_ok(self.get(DELIVERIES_PATH))
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(sorted(item), sorted(self.ITEM_KEYS))
        self.assertEqual(item["order_no"], ORDER_NO)
        self.assertEqual(item["status"], "failed")
        # ⛔ 台账行里不许有凭据（与既有台账同一条纪律）
        self.assertFalse(
            _all_keys(data) & {"app_secret", "app_key", "sign", "signature", "token"}
        )

    def test_another_apps_delivery_is_not_visible(self):
        """对照：给**通用**应用造一条台账 ⇒ 本端点看不到它（按 kind 取数，⛔ 不是"全部台账"）。"""
        app = self.make_app()
        other = self.make_general_app()
        self.make_delivery(app)
        self.make_delivery(other, status=OpenApiDelivery.Status.DONE)
        data = self.assert_ok(self.get(DELIVERIES_PATH))
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["status"], "failed")

    def test_deliveries_before_creation_is_a_business_error(self):
        self.assert_business_failure(
            self.get(DELIVERIES_PATH), VALIDATION, msg_fragment="应用不存在"
        )

    def test_a_bad_status_filter_is_a_business_error(self):
        """`?status=` 非法 ⇒ `1000`（⛔ 不静默忽略：静默忽略会让页面以为"筛了"）。"""
        self.make_app()
        self.assert_business_failure(
            self.get(f"{DELIVERIES_PATH}?status=BOGUS"), VALIDATION
        )


# ═════════════════════════════════════════════════════════════════════════════
# 判据 11：`_translate_create_conflict` 的**另一端**（⛔ 不许吞掉别的库级错）
# ═════════════════════════════════════════════════════════════════════════════

class CreateConflictTranslationTests(_WecomQrAppFixture):
    """判据 11：`_translate_create_conflict` 的**三档**（两条都可翻、别的必须原样抛）。

    同一次 `INSERT` 上挂着**两条**唯一约束（`uniq_apiapp_wecom_qr` 与 `ApiApp.name`），
    而它们的正确文案**不同**（业务方补充）⇒ 这里把三档**逐条**对拍：

    | 回查到的实际冲突面 | 期望 |
    |---|---|
    | `kind=wecom_group_qr` 那一档有行 | `1000` +「本租户已创建过该应用，不能重复创建」 |
    | 没有 wecom 行、但 `name` 被占 | `1000` +「应用名已存在：…」（与既有创建端点**同一句**） |
    | 两条都不成立（如撞 `app_key`） | ⛔ **原样抛** `IntegrityError`（该 500 的照样 500） |

    ⚠️ 为什么这一批是本文件里最重要的控制组：把 `except IntegrityError: 1000` 写宽了
    在**功能上完全正常**（创建端点该拒的都拒了），唯一的差别是「真正该 500 的库级错被
    伪装成业务结果」⇒ 监控失明；而把两条约束合并成同一句文案，则会让排障的人查错方向。
    只有**直接对拍三档**才抓得到这两件事（`AGENTS.md` §5-E：别把未预期异常伪装成业务结果）。
    """

    def test_the_conditional_unique_constraint_is_translated(self):
        """档 1：撞 `uniq_apiapp_wecom_qr` ⇒ `1000` +「已经有一把了」。

        ⚠️ 传进去的名字是**第一把用的那个**（= 并发**默认名**的真实现场）：两条约束
        同时命中，正确的话是"已经有一把了"，⛔ 不是名字那一条（判据的顺序承重）。
        """
        from rest_framework.exceptions import ValidationError as DrfValidationError

        other = _conditional_unique_integrity_error(self.creator)
        self.assertIsInstance(other, IntegrityError)
        self.assertEqual(self.wecom_rows().count(), 1)
        with self.assertRaises(DrfValidationError) as caught:
            wecom_qr_app_views._translate_create_conflict(APP_NAME)
        self.assertIn(
            wecom_qr_app_views.WECOM_APP_EXISTS_MSG, str(caught.exception.detail)
        )

    def test_a_taken_name_is_translated_with_the_name_message(self):
        """档 2：没有 wecom 行、但名字被**别的**应用占了 ⇒ `1000` +「应用名已存在」。

        ⚠️ 这一档就是「两个并发创建都用默认名」里后到那一个可能撞上的形状
        —— 回成"已经有一把了"在**那一次**恰好也对，但换个场景就会误导排障方向。
        """
        from rest_framework.exceptions import ValidationError as DrfValidationError

        other = _name_taken_integrity_error(self.creator)
        self.assertIsInstance(other, IntegrityError)
        # 自证：这一档发生时**没有** wecom 行（否则正确答复是档 1）
        self.assertEqual(self.wecom_rows().count(), 0)
        with self.assertRaises(DrfValidationError) as caught:
            wecom_qr_app_views._translate_create_conflict(APP_NAME_TAKEN)
        self.assertIn(
            wecom_qr_app_views.APP_NAME_TAKEN_MSG.format(name=APP_NAME_TAKEN),
            str(caught.exception.detail),
        )

    def test_any_other_integrity_error_is_reraised(self):
        """⭐ 档 3（**最重要的一条**）：两条都不成立 ⇒ **原样抛**（⛔ 不许伪装成 `1000`）。

        素材 = 撞 `ApiApp.app_key` 的唯一索引（既不是 kind、也不是 name）⇒ 它必须
        响亮地 500（`AGENTS.md` §5-E：未预期异常不许伪装成业务结果，否则监控失明）。
        """
        other = _key_taken_integrity_error(self.creator)
        self.assertIsInstance(other, IntegrityError)
        # 夹具自证：这一档的两个"可翻"条件都**不成立**（否则下面的断言是空转）
        self.assertEqual(self.wecom_rows().count(), 0)
        self.assertFalse(ApiApp.objects.filter(name=CTRL_OTHER_NAME).exists())
        # ⚠️ `_translate_create_conflict` 的末句是**裸 `raise`**（重抛当前异常）⇒ 它只能
        #    在 `except` 块里调用；这里用 `pytest` 风格的 `assertRaises` 之外再包一层
        #    `except IntegrityError: …`，把"原样抛"这件事钉在同一句里。
        try:
            raise other
        except IntegrityError:
            with self.assertRaises(IntegrityError):
                wecom_qr_app_views._translate_create_conflict(CTRL_OTHER_NAME)
        # ⛔ 它**没有**被翻译成业务失败、也**没有**落任何行
        self.assertFalse(ApiApp.objects.filter(name=CTRL_OTHER_NAME).exists())
        self.assertEqual(self.wecom_rows().count(), 0)


# ═════════════════════════════════════════════════════════════════════════════
# 判据 12：**D8** —— 既有应用管理端按 `kind=general` 收窄（本任务同批做）
# ═════════════════════════════════════════════════════════════════════════════

class ExistingAppAdminIsNarrowedToGeneralTests(_WecomQrAppFixture):
    """D8：锁定应用**不出现在** `/api/open/apps/`，拿它的 pk 打那四条既有端点都 `1000`。

    ⚠️ 这是**两个页面各管各的**那条口径的判据（设计文档 §7.3 / D8）。它对既有数据
    **零行为变化**（今天库里没有任何 `kind != general` 的行）—— 所以下面每一条都
    **同时**造一把通用应用作为对照：证"挡掉的是锁定那一类"，⛔ 不是"把列表整体弄空了"。
    """

    LIST_PATH = "/api/open/apps/"
    DETAIL_TEMPLATE = "/api/open/apps/{pk}/"
    ROTATE_TEMPLATE = "/api/open/apps/{pk}/rotate-secret/"
    DELIVERIES_TEMPLATE = "/api/open/apps/{pk}/deliveries/"
    RETRY_TEMPLATE = "/api/open/apps/{pk}/deliveries/{delivery_pk}/retry/"

    def test_the_locked_app_is_not_in_the_general_list(self):
        """⭐ 列表里**只有**通用应用（锁定应用被 D8 的过滤挡在外面）。

        ⚠️ 判据用 `app_key` 集合**逐字相等**（⛔ 不是 `assertNotIn` 那种只测一半）：
        既证"锁定的不在"，也证"通用那把**在**"（否则"列表空了"也能让 `assertNotIn` 绿）。
        """
        locked = self.make_app()
        general = self.make_general_app()
        data = self.assert_ok(self.get(self.LIST_PATH))
        self.assertEqual(
            sorted(row["app_key"] for row in data), sorted([general.app_key])
        )
        self.assertNotIn(locked.app_key, [row["app_key"] for row in data])

    def test_the_four_existing_endpoints_refuse_the_locked_pk(self):
        """⭐ 拿**锁定应用**的 pk 打既有四条端点 ⇒ 都回 `1000`「应用不存在」。

        ⚠️ 四条就是 spec D8 点名的那些：`PATCH` / `rotate-secret` / `deliveries` / `retry`
        （列表那条是 `GET`，见上一条用例）。
        """
        locked = self.make_app()
        delivery = self.make_delivery(locked)
        calls = (
            ("patch", self.DETAIL_TEMPLATE.format(pk=locked.pk)),
            ("post", self.ROTATE_TEMPLATE.format(pk=locked.pk)),
            ("get", self.DELIVERIES_TEMPLATE.format(pk=locked.pk)),
            (
                "post",
                self.RETRY_TEMPLATE.format(pk=locked.pk, delivery_pk=delivery.pk),
            ),
        )
        for method, url in calls:
            with self.subTest(method=method, url=url):
                if method == "get":
                    response = self.get(url)
                elif method == "patch":
                    response = self.patch_json(url, {"state": "disabled"})
                else:
                    response = self.post_json(url, {})
                self.assert_business_failure(
                    response, VALIDATION, msg_fragment="应用不存在"
                )

    def test_the_same_four_endpoints_still_work_for_a_general_app(self):
        """正向对照（能失败）：同样四条端点打**通用**应用 ⇒ 全部正常。

        没有它，上面那条 `1000` 可能是"这四条路对谁都不通"凑出来的（E-0004 同族）。
        """
        general = self.make_general_app()
        delivery = self.make_delivery(general, status=OpenApiDelivery.Status.FAILED)

        patched = self.assert_ok(
            self.patch_json(
                self.DETAIL_TEMPLATE.format(pk=general.pk), {"state": "disabled"}
            )
        )
        self.assertEqual(patched["state"], "disabled")
        rotated = self.assert_ok(
            self.post_json(self.ROTATE_TEMPLATE.format(pk=general.pk), {})
        )
        self.assertNotEqual(rotated["app_secret"], general.app_secret)
        rows = self.assert_ok(self.get(self.DELIVERIES_TEMPLATE.format(pk=general.pk)))
        self.assertEqual(rows["total"], 1)
        retried = self.assert_ok(
            self.post_json(
                self.RETRY_TEMPLATE.format(pk=general.pk, delivery_pk=delivery.pk), {}
            )
        )
        self.assertEqual(retried["status"], "pending")

    def test_the_locked_row_is_untouched_by_those_refusals(self):
        """⭐ 「拒绝」要打在**库里的那一行**上（E-0009）：四次请求之后它逐列没被改过。

        ⚠️ 只断业务码不够：一个"先改了再回 `1000`"的实现同样能让上面那条用例绿。
        """
        locked = self.make_app()
        before = {
            field.name: getattr(locked, field.attname)
            for field in locked._meta.concrete_fields
        }
        delivery = self.make_delivery(locked, status=OpenApiDelivery.Status.FAILED)
        self.patch_json(self.DETAIL_TEMPLATE.format(pk=locked.pk), {"state": "disabled"})
        self.post_json(self.ROTATE_TEMPLATE.format(pk=locked.pk), {})
        self.get(self.DELIVERIES_TEMPLATE.format(pk=locked.pk))
        self.post_json(
            self.RETRY_TEMPLATE.format(pk=locked.pk, delivery_pk=delivery.pk), {}
        )
        locked.refresh_from_db()
        after = {
            field.name: getattr(locked, field.attname)
            for field in locked._meta.concrete_fields
        }
        self.assertEqual(after, before)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, OpenApiDelivery.Status.FAILED)
        self.assertEqual(delivery.attempts, 2)
