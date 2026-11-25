"""T6：开放平台 v2 **对外录单**端点（`POST /api/open/v1/orders/`）的判据。

设计文档：`docs/specs/2026-10-08-open-platform-v2-parity-design.md` §6.1 / §6.2 / §6.3 / §13.14-3。

## 本文件证什么（每条都能独立地红）

1. **最小必填集就能建单**，且判据打在**库里**（⛔ 不是只看响应）：200 + 回单号 +
   `Order.objects` 里真的有那一行、`status` / `total_price` / `project_text` 都对。
2. **幂等四态**（§6.3）：新键执行 / 同键同体 ⇒ **回放且只建一单** / 同键异体 ⇒ `1000` /
   缺键 ⇒ `1000`；再加一条**事务口径**：建单之前被闸拒的那一次**不留台账行**
   （同一个键下次可以重试）。
3. ⭐ **「单已建好、后续动作没成」那一档不许把单回滚掉**（`1022` / `1023`）：
   订单**还在库里**、响应给得出单号、**同一个键重试回放同一份响应**、库里**只有一单**。
   ⇒ 这条是 `apps/openapi/idempotency.py::mark_side_effect` 的判据（那个钩子的存在理由见
   它自己的 docstring 与那个模块末尾的 T6 改准块）。
4. ⭐ **来源单号去重**（§6.3）：**不同** `Idempotency-Key` + **同一** `source_order_no`
   ⇒ 两次都 200、**订单号逐字相同**、库里**只有一张单**；并配两条对照（换号会建第二单 /
   去重按**应用**分桶，⛔ 不是全租户一把）。
5. **字段白名单**：表外的键 ⇒ **`1000` + 一行都不落库**（fail-closed；⛔ **不是**静默忽略
   ——见 `CreateShapeTests::test_fields_outside_the_whitelist_are_rejected_and_write_nothing`），
   并配**两条对照**：白名单**内**的可选字段缺失照旧可选 / 未知键配空值也照拒；
   另加一条结构判据（白名单里每个订单列名都真的是 `OrderSerializer` 的**可写**字段，防拼错漂移）。
6. ⭐ **`game_password` 不在任何查询载荷里**（查单 + 详情各一条）：本系统**没有**这个列，
   而"客户游戏密码"这类凭据不许出现在对外载荷里（`AGENTS.md` §4 的精神；文档 §17.9 的
   C / D 两组同一条纪律）。
7. **三道前置闸**：互斥（对外）、抛单发单价 `<= 0`（对外）、`"hall"` 配套闸（**内部端点**上钉
   —— 理由见 `HallPreferenceGateTests` 的 docstring）。
8. **只认令牌 / 租户边界**：会话请求打集合路径 ⇒ `401`（对照：同一会话打 `/api/auth/me` ⇒ 200）；
   A 的令牌打 B 的域名 ⇒ `401`；A 的令牌 + **B 的 game_id** ⇒ 业务码 + **A 库里没有新单**。
9. **畸形入参不 500**，逐条写清期望码；⭐ 其中 `hours` 是一条**「对标有、我们没有」**：
   它不在白名单里 ⇒ 被忽略（建单照样成功、绩效行的 `hours` 仍为空），
   ⛔ 不是"收下然后丢掉"（那是静默丢字段）。
10. ⭐ **内部 / 对外两条路建出来的单**在关键字段上**一致**（同一组参数、同一份领域核心），
    并显式钉住两处**有意的**不同（`created_by` / `sales_user` / `sales_agent`）。

## 夹具纪律

- 凭据 / 单号 / 游戏名一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的数据无关。
- 跨域名请求结束后连接**停在那个租户的 schema 上**（`TenantMainMiddleware` 进来时 set、
  响应后**不还原**）⇒ 每个发请求的辅助函数都用 `try/finally` 把连接**钉回租户 A**，
  `setUp` 第一行也显式 `set_tenant`。
- `tokens.issue()` **当场签发**（用它自己的密钥）⇒ 本文件不复制任何签名 / 编码逻辑。
- ⚠️ **每条用例内部的 401 次数别超过 20**：Bearer 认证那条"按来源 IP 的失败闸"
  （`authentication.IP_FAILURE_MAX`）会把第 21 次**从同一 IP** 的失败直接挡掉。
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.api.serializers import CreateOrderAssignSerializer, OrderSerializer
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_status import IllegalTransition
from apps.business.order_writes import _fail
from apps.core.error_codes import ErrorCode
from apps.openapi import tokens
from apps.openapi.api.order_create_views import (
    CREATE_FIELDS,
    EXTRA_REQUIRED_FIELDS,
)
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.models import ApiApp, OpenApiIdempotency
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 集合路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致；⚠️ 带尾斜杠）。
CREATE_PATH = "/api/open/v1/orders/"
#: 详情路径的**模板**（两条对外路径今天都只认 Bearer；旧签名认证已整体删除，设计文档 §13.1）。
DETAIL_TEMPLATE = "/api/open/v1/orders/{order_no}/"
#: 会话认证的对照端点（用它证明"会话本身是好的"）。
SESSION_CONTROL_PATH = "/api/auth/me"
#: 内部录单端点（对照用例用）。
INTERNAL_CREATE_PATH = "/api/business/orders/"

#: 业务码 / 状态码的**字面值**（`AGENTS.md` §5-E：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
CONFLICT = 1001
UNAUTHORIZED = 401
CREATED_ASSIGN_FAILED = 1022
CREATED_THROW_FAILED = 1023

#: 信封键集（**写死的字面量**，⛔ 不从实现 import）。
ENVELOPE_KEYS = {"code", "msg", "data"}

#: ⛔ **不许**出现在对外载荷里的键（D 组"我方员工写的" + C 组"员工身份" + 任何内部主键
#: + 客户游戏凭据）。⚠️ 这是**裁决**不是遗漏 —— 写进用例是为了让"后来者顺手补上"当场变红
#: （与 `tests/test_openapi_order_api.py` 的 `FORBIDDEN_KEYS` 同源，本文件只覆盖**录单响应**）。
FORBIDDEN_KEYS = {
    "id",
    "sales_user",
    "created_by",
    "updated_by",
    "hall_publisher",
    "remark",
    "deleted_at",
    "deleted_by",
    "game_password",
    "password",
}

#: 幂等键的契约长度（§6.3）：8–128。
MIN_KEY = 8

# ── 合成凭据（⛔ 不是任何真实租户的凭据）──────────────────────────────────────────
APP_NAME = "录单探针应用甲"
APP_KEY = "probecreatekey0000001"
APP_SECRET = "probe-create-secret-000000000001"
APP_NAME_B = "录单探针应用乙"
APP_KEY_B = "probecreatekey0000002"
APP_SECRET_B = "probe-create-secret-000000000002"
#: 只读那一侧的应用（节点 + 游戏范围都配好）：用来打**详情 / 查单**读端点。
READER_APP_KEY = "probecreateread0000001"
READER_APP_SECRET = "probe-create-read-secret-000001"

#: 租户 B 的域名与 schema（刻意与既有跨租户用例不同 ⇒ 同一次运行里不会撞 `Domain` 唯一约束）。
DOMAIN_B = "openapi-create-iso-b.test.local"
SCHEMA_B = "openapi_create_iso_b"

#: 来源 IP（文档保留段 203.0.113.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.91"

#: 留痕的动作码（与领域核心那两处**逐字相同**）。
CREATE_ACTION = "order.create"


def _all_keys(node, into=None):
    """递归收集载荷里出现的**全部 dict 键**（判"某个键不在载荷里"要用它，⛔ 不是只看顶层）。"""
    into = set() if into is None else into
    if isinstance(node, dict):
        for key, value in node.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, into)
    return into


class _CreateFixture(TenantTestCase):
    """租户 A（`TenantTestCase` 的 schema）+ 租户 B（`SCHEMA_B`）两侧的最小数据。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase` 的事务里。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="对外录单租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        # ⚠️ 按来源 IP 的失败计数自 T3 起落 **DB 行**（随本用例的事务回滚）⇒ 这句只是"只加不删"。
        cache.clear()
        self.domain_a = self.domain.domain
        self.game_a = Game.objects.create(name="录单探针游戏甲")
        self.game_a2 = Game.objects.create(name="录单探针游戏乙")
        creator, _ = User.objects.get_or_create(
            username="录单探针建号人甲", defaults={"password": "unused-in-this-test"}
        )
        #: 录单**不需要**任何权限节点（§13.10：一个应用凭据 = 本租户全部业务数据）
        #: ⇒ 这个应用**一个节点都不勾**，用它建单照样成功（那是判据的一部分）。
        self.app_a = ApiApp.objects.create(
            name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET, created_by=creator
        )
        #: 第二个应用（同一租户）：证明来源单号去重是**按应用**分桶的。
        self.app_a2 = ApiApp.objects.create(
            name=APP_NAME_B, app_key=APP_KEY_B, app_secret=APP_SECRET_B, created_by=creator
        )
        #: 只读那一侧的应用：打详情 / 查单读端点用。
        #: ⛔ 应用不挂节点 / 游戏（那三列已随 v1 面删除，§13.1；读面不再有节点门与游戏范围）。
        self.reader_app = ApiApp.objects.create(
            name="录单探针只读应用",
            app_key=READER_APP_KEY,
            app_secret=READER_APP_SECRET,
            created_by=creator,
        )
        #: 内部端点的对照客户端：只给建单那一类级门（`order.add_edit`）。
        self.internal = make_logged_in_client(
            domain=self.domain_a,
            username="录单探针对照客服甲",
            groups=("客服",),
            nodes=("order.add_edit",),
        )
        self.taker = User.objects.create(
            username="录单探针打手甲", password="unused-in-this-test"
        )

        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(name="录单探针游戏乙租户")
                creator_b, _ = User.objects.get_or_create(
                    username="录单探针建号人乙", defaults={"password": "unused-in-this-test"}
                )
                self.app_b = ApiApp.objects.create(
                    name="录单探针应用B", app_key="probecreatekey0000bbb",
                    app_secret="probe-create-secret-bbbbbbbbbbbb", created_by=creator_b,
                )
        finally:
            connection.set_tenant(self.tenant)

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _post(self, body, *, key="probe-create-key-0001", app=None, domain=None,
              token=None, ip=SOURCE_IP, omit_key=False, raw=None):
        """带 Bearer 令牌发一条录单请求；结束后把连接钉回租户 A。

        ⚠️ 请求体一律 `json.dumps(body)`（**同一个字典两次调用得到同一串字节**）
        ⇒ 「同键同体」那条判据里的"同体"是**真的**同字节（`request_hash` 的口径）。
        """
        app = self.app_a if app is None else app
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if not omit_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        data = raw if raw is not None else json.dumps(body)
        try:
            return Client(SERVER_NAME=domain).generic(
                "POST",
                CREATE_PATH,
                data=data,
                content_type="application/json",
                REMOTE_ADDR=ip,
                **headers,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _get_list(self, params, *, app=None, token=None):
        """带 Bearer 令牌查单（按来源单号）；结束后把连接钉回租户 A。"""
        app = self.reader_app if app is None else app
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        try:
            return Client(SERVER_NAME=self.domain_a).get(
                CREATE_PATH,
                params,
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _detail_get(self, order_no, *, app=None):
        """发一条**详情**请求（`GET v1/orders/<单号>/`），带 **Bearer 令牌**。

        ⛔⭐ **改准（T10）**：本助手原先**两套凭据都带**（签名头 + 令牌），
        理由是"当时详情路径的认证被切换过、不想钉它用哪一套"。签名面已整体删除
        （设计文档 §13.1）⇒ 今天只剩**一种**对外凭据 ⇒ 只带令牌。
        ⚠️ 判据内核一字未改：下面那句 `assert_ok` 仍然**响亮地**要求 `200`
        （万一这条路不认令牌，它会当场红）。
        """
        app = self.reader_app if app is None else app
        path = DETAIL_TEMPLATE.format(order_no=order_no)
        try:
            return Client(SERVER_NAME=self.domain_a).get(
                path,
                HTTP_AUTHORIZATION=f"Bearer {tokens.issue(app.app_key, app.app_secret)}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body["data"]

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + **指定的**业务码（⛔ 不只看 msg）。"""
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body

    def orders(self):
        return Order.objects.all()

    def minimal_body(self, **overrides):
        """最小必填集（`game` / `project_text` / `total_price` / `source_order_no`）。"""
        body = {
            "game": self.game_a.pk,
            "project_text": "满星 甲",
            "total_price": "88.00",
            "source_order_no": "TB-CREATE-0001",
        }
        body.update(overrides)
        return body


class CreateShapeTests(_CreateFixture):
    """① 最小必填集 ⇒ 建单（判据打在库里）+ 响应形状 + 字段白名单。"""

    def test_the_minimal_required_set_creates_the_order_in_the_database(self):
        """⭐ 判据打在**库里**：响应说建了不算数，`Order.objects` 里要有那一行。

        ⚠️ 夹具前提：这个应用**没有任何权限类**（`permission_classes = []`，§13.10）
        ⇒ 本条同时钉住「录单**不装**节点门」（否则这条会 403 而不是 200）。
        ⛔⭐ **改准（T10）**：原判据在这里还有一句
        `self.assertEqual(self.app_a.nodes.count(), 0, "夹具前提：录单应用没有节点")`
        —— 节点那一列已删除（§13.1）⇒ 那句同一次去掉；**下面那句 200 的判据一字未改**
        （它本身就是"不装节点门"的行为判据，比夹具前提更强）。
        """
        payload = self.assert_ok(self._post(self.minimal_body()))
        order_no = payload["order_no"]
        self.assertTrue(order_no)

        order = self.orders().get(order_no=order_no)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.total_price, Decimal("88.00"))
        self.assertEqual(order.project_text, "满星 甲")
        self.assertEqual(order.source_order_no, "TB-CREATE-0001")
        self.assertEqual(order.game_id, self.game_a.pk)
        # 录单人 / 归属人：那条路上没有"用户" ⇒ 都不写（见模块 docstring 与核心的 docstring）。
        self.assertEqual(order.created_by, "开放平台应用 录单探针应用甲")
        self.assertIsNone(order.sales_user_id)
        self.assertEqual(order.sales_agent, "")
        # 绩效行真的建了（订单与绩效同生共死，spec §7.4）。
        self.assertEqual(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).count(), 1
        )
        # 留痕一行（`order.create`），且是**系统行**（`actor_id` 为空、`actor` 写着哪个应用）。
        log = OperationLog.objects.get(action=CREATE_ACTION, order_no=order_no)
        self.assertIsNone(log.actor_id)
        self.assertEqual(log.actor, "开放平台应用 录单探针应用甲")

    def test_the_success_payload_is_the_read_contract_not_the_internal_one(self):
        """响应 `data` = 查单载荷（`order_payload`），⛔ 不是内部那个含员工身份的序列化器。

        判别力：把 `payload_for_order` 换成内部录单那句 `OrderSerializer(...).data`
        ⇒ 递归键集里会多出 `sales_user` / `created_by` / `remark` / `id` ⇒ 本条红
        （`FORBIDDEN_KEYS`）。
        """
        payload = self.assert_ok(self._post(self.minimal_body()))
        self.assertEqual(payload["order_no"][:2], "20")
        self.assertEqual(payload["status"], OrderStatus.UNASSIGNED)
        self.assertEqual(payload["amounts"]["total_price"], "88.00")
        self.assertEqual(payload["order"]["project_text"], "满星 甲")
        self.assertEqual(payload["game"], self.game_a.name)
        leaked = sorted(_all_keys(payload) & FORBIDDEN_KEYS)
        self.assertEqual(leaked, [], f"对外载荷里出现了内部键：{leaked}")

    def test_the_whitelist_only_names_writable_order_fields(self):
        """结构判据：白名单里的每个**订单列名**都真的是 `OrderSerializer` 的**可写**字段。

        ⚠️ 没有它，白名单里拼错一个名字（或内部改了字段名而这里没跟）**不会有任何用例变红**
        —— 那会让"这个键在我的契约里"与"它真的能落库"悄悄分叉
        （表外的名字会被序列化器静默忽略）。
        ⚠️ 另外两个键（`assignee` / `throw_to_hall`）**不是订单列**：它们由
        `CreateOrderAssignSerializer` 收，所以分开判。
        """
        order_fields = OrderSerializer().fields
        non_column = {"assignee", "throw_to_hall"}
        for name in sorted(CREATE_FIELDS - non_column):
            with self.subTest(field=name):
                self.assertIn(name, order_fields, f"白名单里的 {name} 不是 OrderSerializer 的字段")
                self.assertFalse(order_fields[name].read_only, f"{name} 在内部是只读的")
        assign_fields = CreateOrderAssignSerializer().fields
        for name in sorted(non_column):
            with self.subTest(field=name):
                self.assertIn(name, assign_fields)

    def test_the_extra_required_fields_are_the_two_the_contract_needs(self):
        """`EXTRA_REQUIRED_FIELDS` 恰好两项，且**不**含内部那两项（⛔ 不抄第二份必填判据）。"""
        self.assertEqual(set(EXTRA_REQUIRED_FIELDS), {"total_price", "source_order_no"})

    def test_fields_outside_the_whitelist_are_rejected_and_write_nothing(self):
        """⭐⭐ 表外的键 ⇒ **`1000` + 一行都不落库**（fail-closed；⛔ **不是**静默忽略）。

        走的都是**内部 `OrderSerializer` 本可写**的列（`source` / `order_type` / `tag` /
        `payment_method` / `game_sub_account` / `received_price`）加上本来就只读的
        （`status` / `sales_user`）—— 两类都**不许**被接受。

        ## 为什么是"拒"而不是"忽略"（这条判据的由来）

        首版按白名单**过滤**（表外的键不生效），总指挥裁定那是**静默丢字段**
        （与设计文档 §13.14-2 对 `remark` 的裁决同一类缺陷）：调用方以为值写进去了、
        库里其实没有，而**没有任何信号**。⇒ 现在表外出现任何键就拒，
        差异写在 `docs/OPEN-API.md` §4.6 的「对标有、我们没有」对照表里，
        让照对标文档写的调用方在**第一次调用**就看到。

        ⚠️ 判据是**两条一起**：① 码是 `1000` 且 msg **点名**了那些键；
        ② **库里一行都没写**（连幂等占位行也不留 ⇒ 同一个键改完可以重试——
        那条另有用例）。⛔ 只断"码是 1000"不够：一个"先落库再报错"的实现也能让它绿。
        """
        before = self.orders().count()
        fail = self.assert_business_failure(
            self._post(
                self.minimal_body(
                    status=OrderStatus.COMPLETED,
                    sales_user=self.taker.pk,
                    source="外部硬塞的来源",
                    order_type="BOOST",
                    tag="外部硬塞的标记",
                    payment_method="支付宝",
                    game_sub_account="外部硬塞的子账号",
                    received_price="999.00",
                )
            ),
            VALIDATION,
        )
        # ① 文案**点名**了这些键（按字典序拼出来 ⇒ 判据可以写死整句）。
        self.assertEqual(
            fail["msg"],
            "请求里有本 API 不接受的字段：game_sub_account、order_type、payment_method、"
            "received_price、sales_user、source、status、tag",
        )
        # ② **一行都没写**（判据打在库里）。
        self.assertEqual(self.orders().count(), before)

    def test_an_unknown_key_is_rejected_even_when_its_value_is_blank(self):
        """⚠️ **不特判空值**：未知键配 `""` / `null` **照拒**。

        理由：那会多出一条没有判据的分支（"空串算不算出现"）——而"这个字段我们不收"
        与它的值无关。⛔ 别为了"宽容"给它开例外。
        """
        before = self.orders().count()
        for value in ("", None):
            with self.subTest(value=repr(value)):
                fail = self.assert_business_failure(
                    self._post(self.minimal_body(hours=value), key="probe-create-key-0009"),
                    VALIDATION,
                )
                self.assertIn("hours", fail["msg"])
        self.assertEqual(self.orders().count(), before)

    def test_the_whitelisted_optional_fields_stay_optional(self):
        """⭐ 对照（⛔ 不是陪衬）：白名单**内**的可选字段缺失**照旧可选**。

        判别力：把这一改误伤成"所有字段都必填"（例如顺手给每个键加 `required`）⇒
        只发最小必填集的那一发会红。⚠️ 这就是"fail-closed 只管**表外**"的边界。
        """
        payload = self.assert_ok(
            self._post(self.minimal_body(), key="probe-create-key-0010")
        )
        order = self.orders().get(order_no=payload["order_no"])
        self.assertIsNone(order.server_id)
        self.assertEqual(order.game_account, "")
        self.assertEqual(order.remark, "")
        self.assertIsNone(order.hall_price)

    def test_a_missing_extra_required_field_is_a_parameter_error(self):
        """`total_price` 是**对外**必填（内部可选）⇒ 缺了回 `1000`，且一行都不写。"""
        before = self.orders().count()
        for body in (
            {k: v for k, v in self.minimal_body().items() if k != "total_price"},
            self.minimal_body(total_price="  "),
        ):
            with self.subTest(body=sorted(body)):
                fail = self.assert_business_failure(
                    self._post(body, key="probe-create-key-0002"), VALIDATION
                )
                self.assertIn("total_price", fail["msg"])
        self.assertEqual(self.orders().count(), before)

    def test_a_missing_or_blank_source_order_no_is_a_parameter_error(self):
        """⭐ `source_order_no` 必填**且非空**：它是去重键，空值会让所有空号的单互相去重。

        ⚠️ 判据是**两档**：缺键、以及"传了但全是空白"（后者在内部是合法的空串）。
        """
        before = self.orders().count()
        for body in (
            {k: v for k, v in self.minimal_body().items() if k != "source_order_no"},
            self.minimal_body(source_order_no="   "),
        ):
            with self.subTest(body=sorted(body)):
                fail = self.assert_business_failure(
                    self._post(body, key="probe-create-key-0003"), VALIDATION
                )
                self.assertIn("source_order_no", fail["msg"])
        self.assertEqual(self.orders().count(), before)

    def test_game_password_is_rejected_and_never_appears_in_any_payload(self):
        """⭐ `game_password`：**我们没有这个字段** ⇒ 传了直接 `1000`，而任何载荷里也读不到它。

        三条一起（缺一条都会让"客户游戏密码"从某个缝里露出来）：
        ① `Order` **没有**这个列（枚举 `_meta`，⛔ 不是"我记得没有"）——这也是"为什么它不在
           字段表里"的取证（设计文档 §1.2 第 6 条：形态对标、**字段名与字段集用我们自己的**）；
        ② 建单请求里带了它 ⇒ **`1000`**（表外键 fail-closed）+ **一行都不落库**
           （⛔ 不是"收下然后丢掉"——那正是静默丢字段）；
        ③ **查单**与**详情**两条读路径的载荷里都没有它（递归键集判据，用一条**正常**建的单来查）。
        """
        from apps.business.models import Order as OrderModel

        column_names = {field.name for field in OrderModel._meta.get_fields()}
        self.assertNotIn("game_password", column_names)

        # ② 传了它 ⇒ 拒 + 一行不写。
        before = self.orders().count()
        fail = self.assert_business_failure(
            self._post(self.minimal_body(game_password="合成密码-甲")), VALIDATION
        )
        self.assertIn("game_password", fail["msg"])
        self.assertEqual(self.orders().count(), before)

        # ③ 一条**正常**建单（不带那个键）之后：查单 + 详情两条读路径的载荷都没有它。
        payload = self.assert_ok(
            self._post(self.minimal_body(), key="probe-create-key-0008")
        )
        order_no = payload["order_no"]
        self.assertNotIn("game_password", _all_keys(payload))

        listed = self.assert_ok(self._get_list({"source_order_no": "TB-CREATE-0001"}))
        self.assertNotIn("game_password", _all_keys(listed))
        self.assertEqual([row["order_no"] for row in listed["items"]], [order_no])

        detail_response = self._detail_get(order_no)
        detail = self.assert_ok(detail_response)
        self.assertNotIn("game_password", _all_keys(detail))
        self.assertNotIn("password", _all_keys(detail))


class IdempotencyTests(_CreateFixture):
    """② §6.3 的**四态** + 作废键 + ⭐「单已建好、后续动作没成」不许回滚那一档。"""

    def test_a_new_key_creates_the_order_and_stores_the_snapshot(self):
        key = "probe-create-key-1001"
        payload = self.assert_ok(self._post(self.minimal_body(), key=key))
        row = OpenApiIdempotency.objects.get(app=self.app_a, key=key)
        self.assertEqual(row.method, "POST")
        self.assertEqual(row.path, CREATE_PATH)
        self.assertEqual(len(row.request_hash), 64)
        self.assertEqual(row.status_code, 200)
        self.assertEqual(set(row.response_snapshot), ENVELOPE_KEYS)
        self.assertEqual(row.response_snapshot["data"]["order_no"], payload["order_no"])

    def test_the_same_key_and_body_replays_and_creates_only_one_order(self):
        """⭐ 回放：载荷**逐字相同**，且副作用**只发生一次**（判据打在库里的行数上）。"""
        key = "probe-create-key-1002"
        body = self.minimal_body()
        first = self.assert_ok(self._post(body, key=key))
        second = self.assert_ok(self._post(body, key=key))
        self.assertEqual(first, second)
        self.assertEqual(
            self.orders().filter(source_order_no=body["source_order_no"]).count(), 1
        )
        self.assertEqual(OperationLog.objects.filter(action=CREATE_ACTION).count(), 1)

    def test_the_same_key_with_a_different_body_is_rejected(self):
        """同键异体 ⇒ `1000`（⛔ 不静默回放上一个响应），且**不建第二单**。"""
        key = "probe-create-key-1003"
        body = self.minimal_body()
        first = self.assert_ok(self._post(body, key=key))
        # ⚠️ 换掉 `source_order_no` ⇒ 去重那条路也走不到（否则本用例测的会是去重而不是幂等）。
        other = self.minimal_body(source_order_no="TB-CREATE-0002")
        self.assert_business_failure(self._post(other, key=key), VALIDATION)
        self.assertEqual(self.orders().count(), 1)
        self.assertEqual(self.orders().get().order_no, first["order_no"])

    def test_a_missing_idempotency_key_is_a_parameter_error(self):
        before = self.orders().count()
        fail = self.assert_business_failure(
            self._post(self.minimal_body(), omit_key=True), VALIDATION
        )
        self.assertEqual(fail["msg"], "缺少幂等键")
        self.assertEqual(self.orders().count(), before)
        # 键太短也算不合规（长度契约 8–128，逐字）。
        self.assert_business_failure(
            self._post(self.minimal_body(), key="short"), VALIDATION
        )
        self.assertEqual(self.orders().count(), before)

    def test_a_pre_create_gate_leaves_no_ledger_row_so_the_key_can_be_retried(self):
        """建单**之前**被拒的那一次：一行台账都不留（同一个键下次可以重试）。

        ⚠️ 判据打在**库里的台账行**上（⛔ 不是"响应码是 1000"）：
        占位行没被提交掉，才是"这个键还能再用"这件事的载体（`idempotency._run` 的事务口径）。
        """
        key = "probe-create-key-1004"
        body = self.minimal_body(throw_to_hall=True, assignee=self.taker.pk)
        self.assert_business_failure(self._post(body, key=key), VALIDATION)
        self.assertFalse(
            OpenApiIdempotency.objects.filter(app=self.app_a, key=key).exists()
        )
        self.assertEqual(self.orders().count(), 0)
        # 对照：同一个键换成一份**合法**的载荷 ⇒ 这一次真的建成（证明键没被占死）。
        ok = self.assert_ok(self._post(self.minimal_body(), key=key))
        self.assertEqual(self.orders().count(), 1)
        self.assertTrue(ok["order_no"])

    def test_an_assign_failure_keeps_the_order_and_replays_the_same_response(self):
        """⭐ `1022`：**单已建好**、指派没成 ⇒ 单**必须还在**，同一个键重试回放同一份响应。

        判别力（这条就是 `mark_side_effect` 的靶子）：把那个钩子去掉（或让
        `idempotency._run` 照 T5 那条"非 0 就回滚"办）⇒ 第一笔的**订单被回滚掉**、
        而响应仍写着「单已建好（单号 X）」⇒ 本条的"库里那单还在"当场红（实测读数见报告）。
        ⚠️ 桩打在**被查找的模块**上（E-0029）：领域核心 `apps.business.order_intake.assign_order`。
        """
        key = "probe-create-key-1005"
        body = self.minimal_body(assignee=self.taker.pk, hall_price="66.00")
        failure = _fail(ErrorCode.BUSINESS, "测试：这一步被拒了")
        with mock.patch(
            "apps.business.order_intake.assign_order", return_value=failure
        ):
            first = self._post(body, key=key)
        first_body = self.assert_business_failure(first, CREATED_ASSIGN_FAILED)
        order_no = first_body["data"]["order_no"]
        self.assertTrue(order_no)
        # ① 单**真的**在库里（不是"响应说有"）。
        order = self.orders().get(order_no=order_no)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.hall_price, Decimal("66.00"))
        self.assertEqual(self.orders().count(), 1)
        # ② 同一个键重试 ⇒ 回放**同一份响应**（桩已经撤掉，仍然回 1022 ⇒ 说明没重跑业务）。
        second = self._post(body, key=key)
        second_body = self.assert_business_failure(second, CREATED_ASSIGN_FAILED)
        self.assertEqual(second_body, first_body)
        self.assertEqual(self.orders().count(), 1)

    def test_a_throw_failure_keeps_the_order_and_replays_the_same_response(self):
        """⭐ `1023`：同上一档的形状（抛单没成、单已建好）。

        ⚠️ 桩打在 `apps.business.order_intake.apply_transition`（被查找的模块，E-0029）。
        """
        key = "probe-create-key-1006"
        body = self.minimal_body(throw_to_hall=True, hall_price="66.00")
        with mock.patch(
            "apps.business.order_intake.apply_transition",
            side_effect=IllegalTransition("测试：这张单现在不能进大厅"),
        ):
            first = self._post(body, key=key)
        first_body = self.assert_business_failure(first, CREATED_THROW_FAILED)
        order_no = first_body["data"]["order_no"]
        order = self.orders().get(order_no=order_no)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self.orders().count(), 1)

        second = self._post(body, key=key)
        second_body = self.assert_business_failure(second, CREATED_THROW_FAILED)
        self.assertEqual(second_body, first_body)
        self.assertEqual(self.orders().count(), 1)


class SourceDedupTests(_CreateFixture):
    """③ §6.3 的**来源单号去重**：同一应用 + 同号 ⇒ 回同一条订单号（⛔ 不建第二单）。"""

    def test_the_same_source_order_no_with_a_different_key_replays_the_same_order(self):
        """⭐ 判据三条一起：两次都 200、**订单号逐字相同**、库里**只有一张单**。"""
        body = self.minimal_body()
        first = self.assert_ok(self._post(body, key="probe-create-key-2001"))
        second = self.assert_ok(self._post(body, key="probe-create-key-2002"))
        self.assertEqual(second["order_no"], first["order_no"])
        self.assertEqual(
            self.orders().filter(source_order_no=body["source_order_no"]).count(), 1
        )
        self.assertEqual(self.orders().count(), 1)
        # 同一份载荷逐字相同（回放的就是那一次的快照）——三个键一起比。
        self.assertEqual(second, first)

    def test_a_different_source_order_no_creates_a_second_order(self):
        """对照：换个来源单号 ⇒ **真的**建第二单（否则"只建一单"可能由"这条路压根不建单"凑出来）。"""
        self.assert_ok(self._post(self.minimal_body(), key="probe-create-key-2003"))
        self.assert_ok(
            self._post(
                self.minimal_body(source_order_no="TB-CREATE-0003"),
                key="probe-create-key-2004",
            )
        )
        self.assertEqual(self.orders().count(), 2)

    def test_the_dedup_is_scoped_per_application(self):
        """⭐ §6.3 逐字是「**同一应用** + 同一来源单号」⇒ 换个应用**不算**重复。

        判别力：把去重改成"按来源单号查 `Order`"（⛔ 那是被否掉的实现）时，
        B 应用这一笔会被**回放成 A 的单**（订单号相同、库里只有 1 张）⇒ 本条红。
        """
        body = self.minimal_body()
        first = self.assert_ok(self._post(body, key="probe-create-key-2005"))
        second = self.assert_ok(
            self._post(body, key="probe-create-key-2006", app=self.app_a2)
        )
        self.assertNotEqual(second["order_no"], first["order_no"])
        self.assertEqual(self.orders().count(), 2)
        self.assertEqual(self.orders().filter(source_order_no=body["source_order_no"]).count(), 2)

    def test_the_dedup_wins_even_when_the_body_differs(self):
        """⭐ 去重按契约就是「**同号即同单**」：改了别的字段来重发 ⇒ 回的仍是原来那一单。

        ⚠️ 这是**有意**的（⛔ 不是 `1000`）：`source_order_no` 是调用方自己的单号，
        同号重发是"同一次录单的重试"，不是"另一张单"。
        """
        first = self.assert_ok(self._post(self.minimal_body(), key="probe-create-key-2007"))
        second = self.assert_ok(
            self._post(
                self.minimal_body(content="重发时改了客户原文"),
                key="probe-create-key-2008",
            )
        )
        self.assertEqual(second["order_no"], first["order_no"])
        self.assertEqual(second, first)
        self.assertEqual(self.orders().count(), 1)


class HallPreferenceGateTests(_CreateFixture):
    """④ 三道**前置闸**（建单之前判完，一行都不写）。

    ⚠️⭐ **第三道闸（`remember_assignee` 配套）在对外契约上不可达**，理由：
    `remember_assignee` 是**内部用户**的偏好（「我的默认接手人 / 默认抛单」），
    它**不在对外字段白名单**里 —— 那条路上根本没有"人"（`IntakeActor.user` 恒 `None`）。
    ⛔ 不把它暴露给第三方（那等于把内部操作人的偏好交给外部写）。
    ⇒ 它的判据打在**内部端点**上（同一份领域核心），本类那一条用例的 docstring 写了这件事。
    """

    def test_throwing_and_assigning_at_once_is_rejected(self):
        """闸①：`throw_to_hall` 与 `assignee` **互斥** ⇒ `1000` + 定稿文案 + 一行不写。"""
        before = self.orders().count()
        fail = self.assert_business_failure(
            self._post(
                self.minimal_body(
                    throw_to_hall=True, hall_price="66.00", assignee=self.taker.pk
                ),
                key="probe-create-key-3001",
            ),
            VALIDATION,
        )
        self.assertEqual(fail["msg"], "内部抢单与指派接手人只能二选一")
        self.assertEqual(self.orders().count(), before)

    def test_a_throw_with_a_non_positive_price_is_rejected(self):
        """闸②：抛单而 `hall_price` **解析后的数值** `<= 0` ⇒ `1000` + 一行不写。

        ⚠️ 判据是**数值**不是真值（E-0021）：`"0.00"` 是**真值**、却是非法价 ⇒
        实现若写成 `if not hall_price:` 就会静默建出一张 0 元抛单。
        两档一起测：显式 `"0.00"`、以及**根本不传**（`None`）。
        """
        before = self.orders().count()
        for body in (
            self.minimal_body(throw_to_hall=True, hall_price="0.00"),
            self.minimal_body(throw_to_hall=True),
        ):
            with self.subTest(body=sorted(body)):
                fail = self.assert_business_failure(
                    self._post(body, key="probe-create-key-3002"), VALIDATION
                )
                self.assertEqual(fail["msg"], "内部抢单的发单价必须大于 0")
        self.assertEqual(self.orders().count(), before)

    def test_the_hall_preference_gate_still_fires_on_the_internal_endpoint(self):
        """⭐ 闸③（`remember_assignee == "hall"` 必须本次真的抛单）——**在内部端点上**钉。

        为什么是内部端点：见类 docstring（`remember_assignee` 不在对外白名单里 ⇒
        对外那条路恒不触发这道闸）。本条要证的是**闸还在抽出的核心里**
        （把 `create_order` 里那一段删掉 ⇒ 本条会拿到 `code=0` 而红）。
        ⚠️ 内部请求走**会话**（`make_logged_in_client`，只给建单那一类级门）。
        """
        before = self.orders().count()
        response = self.internal.post(
            INTERNAL_CREATE_PATH,
            {
                "game": self.game_a.pk,
                "project_text": "满星 甲",
                "total_price": "88.00",
                "source_order_no": "TB-INTERNAL-0001",
                "remember_assignee": "hall",
            },
            content_type="application/json",
        )
        body = self.assert_business_failure(response, VALIDATION)
        self.assertEqual(body["msg"], "勾选「默认抛内部抢单」时必须选择内部抢单")
        self.assertEqual(self.orders().count(), before)

    def test_an_external_remember_assignee_is_rejected_outright(self):
        """⭐ 对照：对外传 `remember_assignee` ⇒ **`1000` + 一行都不写**。

        ⚠️ `remember_assignee` 是**内部用户**的偏好（「我的默认接手人 / 默认抛单」），
        它**不在**对外字段白名单里 ⇒ 表外键 fail-closed 会直接拒（⛔ 不是"收下但不生效"，
        也⛔ 不是"忽略"——见 `CreateShapeTests` 那条用例的 docstring）。
        判据两条一起：① 码是 `1000`、msg 点了名；② 库里**订单与偏好都没有新行**
        （⛔ 不许让第三方写别人的偏好，也⛔ 不许他误以为写进去了）。
        """
        from apps.accounts.models import StaffProfile

        before = self.orders().count()
        fail = self.assert_business_failure(
            self._post(
                self.minimal_body(
                    remember_assignee="hall", throw_to_hall=True, hall_price="66.00"
                ),
                key="probe-create-key-3003",
            ),
            VALIDATION,
        )
        self.assertIn("remember_assignee", fail["msg"])
        self.assertEqual(self.orders().count(), before)
        self.assertFalse(
            StaffProfile.objects.filter(default_throw_to_hall=True).exists()
        )


class AuthAndTenantBoundaryTests(_CreateFixture):
    """⑤ 只认令牌 / 租户边界（`AGENTS.md` §4）。"""

    def test_a_session_request_is_rejected_and_the_session_itself_is_fine(self):
        """会话请求打集合路径（`GET` 与 `POST` 各一条）⇒ `401`；对照同一个会话打 `me` ⇒ 200。"""
        client = make_logged_in_client(
            domain=self.domain_a,
            username="录单探针会话用户甲",
            groups=("客服",),
            nodes=("order.add_edit",),
        )
        me = client.get(SESSION_CONTROL_PATH)
        self.assertEqual(me.status_code, 200, me.content[:200])

        for method, body in (("get", None), ("post", self.minimal_body())):
            with self.subTest(method=method):
                response = client.generic(
                    method.upper(),
                    CREATE_PATH,
                    data=json.dumps(body) if body else None,
                    content_type="application/json",
                    HTTP_IDEMPOTENCY_KEY="probe-create-key-4001",
                )
                self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:200])
                payload = response.json()
                self.assertEqual(payload["code"], UNAUTHORIZED)
                self.assertEqual(payload["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertEqual(self.orders().count(), 0)

    def test_a_token_of_tenant_a_cannot_create_on_the_domain_of_tenant_b(self):
        """A 的令牌打 **B 的域名** ⇒ `401`（`ApiApp` 是**每租户一份**的表），两库都没有新单。"""
        token = tokens.issue(self.app_a.app_key, self.app_a.app_secret)
        response = self._post(
            self.minimal_body(), key="probe-create-key-4002", domain=DOMAIN_B, token=token
        )
        self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:200])
        self.assertEqual(response.json()["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertEqual(self.orders().count(), 0)
        with tenant_context(self.tenant_b):
            self.assertEqual(Order.objects.count(), 0)
        connection.set_tenant(self.tenant)

    def test_a_game_id_is_a_tenant_local_handle_and_can_never_reach_tenant_b(self):
        """⭐ 跨租户：A 的令牌 + **B 的 `game` 主键** ⇒ **只可能**碰到 A 自己的数据。

        ⚠️⭐ **这里有一条实测出来的判据边界（`AGENTS.md` §5-G 的同族教训：跨租户的否定断言
        ⛔ 不能用主键指认）**：两个 schema 的主键序列**各自从 1 开始** ⇒ 「B 的 game_id」
        在 A 里可能**恰好**是 A 自己某款游戏的主键（本夹具实测：`game_b.pk == game_a.pk`）
        ⇒ 那一发会**合法地**建出一张**属于 A 那款游戏**的单 —— 它**不是**越权，
        因为 B 的单根本不在射程里（查询跑在 A 的连接上）。
        ⇒ 本条钉的是**终态**：**B 库里订单数恒为 0**，且 A 这边若多了一单，
        它引用的游戏**是 A 的那一款**（用游戏名自证，⛔ 不是用主键）。
        真正"必须失败"的那一档（A 里**没有**这个 id）由
        `MalformedInputTests` 的「不存在的 `game`」逐条钉着。
        """
        with tenant_context(self.tenant_b):
            game_b = Game.objects.get(pk=self.game_b.pk)
            game_b_pk, game_b_name = game_b.pk, game_b.name
        connection.set_tenant(self.tenant)
        # 夹具自证：**主键空间各自从 1 开始**（这条是本用例判据口径的来源，⛔ 不是假设）。
        self.assertEqual(game_b_pk, self.game_a.pk)

        response = self._post(
            self.minimal_body(game=game_b_pk, source_order_no="TB-CROSS-0001"),
            key="probe-create-key-4003",
        )
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        body = json.loads(response.content)
        self.assertIn(body["code"], (OK, VALIDATION), body)
        # ① **终态**：B 库里一张单都没有（跨租户这件事**不可能**发生）。
        with tenant_context(self.tenant_b):
            self.assertEqual(Order.objects.count(), 0)
        connection.set_tenant(self.tenant)
        # ② A 这边若建成了，它引用的是 **A 的游戏**（名字自证），⛔ 不是 B 的那一款。
        if body["code"] == OK:
            order = self.orders().get(order_no=body["data"]["order_no"])
            self.assertNotEqual(order.game.name, game_b_name)
            self.assertEqual(order.game.name, self.game_a.name)
        else:
            self.assertEqual(self.orders().count(), 0)


class MalformedInputTests(_CreateFixture):
    """⑥ 畸形入参 ⇒ **不 500**，逐条写清期望码。"""

    def test_malformed_inputs_report_a_parameter_error_and_never_500(self):
        """逐档：超长来源单号 / 非整数 `game` / 不存在的 `game` / 非法金额 /
        JSON 数组体 —— 全部 `1000`（⛔ 不是 500、也不是别的码）。

        ⚠️ 「不 500」这条是**结构兜底**（`AGENTS.md` §5-E：判据是"**这类请求**还会不会 500"）：
        `request.data` 是数组时 `.items()` 会抛 `AttributeError`，那正是本模块要挡的形状。
        ⚠️ **负金额不在这里**（它是**合法值**，D-0015）：见下面
        `test_a_negative_amount_is_a_legal_value_not_a_malformed_one`。
        """
        cases = {
            "超长 source_order_no": self.minimal_body(source_order_no="T" * 129),
            "非整数 game": self.minimal_body(game="abc"),
            "不存在的 game": self.minimal_body(game=987654321),
            "非法金额": self.minimal_body(total_price="不是钱"),
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                response = self._post(body, key="probe-create-key-5001")
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assert_business_failure(response, VALIDATION)
        # JSON 数组体：走**原始字节**（`json.dumps` 一个 list）。
        raw = self._post(None, key="probe-create-key-5002", raw=json.dumps([1, 2, 3]))
        self.assertNotEqual(raw.status_code, 500, raw.content[:300])
        self.assert_business_failure(raw, VALIDATION)
        self.assertEqual(self.orders().count(), 0)

    def test_a_negative_amount_is_a_legal_value_not_a_malformed_one(self):
        """⭐ 负金额是**合法值**（D-0015：负值表示退款 / 冲抵 / 调整）⇒ 建单成功、**按原值落库**。

        ⚠️ 这条是"边界值不许用真值判断"的反面控制（E-0021）：实现若写成
        `if not total_price:` 之类，或者"顺手加一道 `min_value=0`"，本用例会红 ——
        而那是**放宽/收紧契约**，不是修 bug。
        """
        payload = self.assert_ok(
            self._post(self.minimal_body(total_price="-1.00"), key="probe-create-key-5004")
        )
        order = self.orders().get(order_no=payload["order_no"])
        self.assertEqual(order.total_price, Decimal("-1.00"))

    def test_a_field_we_do_not_have_is_rejected_outright(self):
        """⭐ `hours`：**对标有、我们没有** ⇒ 传了 **`1000` + 一行都不写**（⛔ 不是收下再丢掉）。

        取证（为什么它不在我们的字段表里）：本系统的 `hours` 是**绩效行**的列
        （`Performance.hours`「时间要求（小时）」，模型注释逐字写着「**每人一份**，
        不是订单级的」），而内部录单路径（`performance.create_performance_for_order`）
        **不收**它 ⇒ 要让它生效得改绩效建行逻辑（**动钱面**），那是另一条线（本批不做）。

        ⚠️ 判据是**拒 + 一行不写**（而不是首版的"忽略"）：`1000` 让照对标文档写的调用方在
        第一次调用就看到差异（对照表在 `docs/OPEN-API.md` §4.6）；
        "一行不写"则由 `Order` 与 `Performance` 两处一起判（⛔ 不是只看订单表）。
        ⚠️ 负值也一样拒 —— 本条用 `-3` 顺便钉住"不是金额/范围校验拒的它，是**未知字段**拒的"
        （msg 里点的是字段名，不是值域）。
        """
        before_orders = self.orders().count()
        before_perfs = Performance.objects.count()
        fail = self.assert_business_failure(
            self._post(self.minimal_body(hours=-3), key="probe-create-key-5003"), VALIDATION
        )
        self.assertIn("hours", fail["msg"])
        self.assertEqual(self.orders().count(), before_orders)
        self.assertEqual(Performance.objects.count(), before_perfs)


class CreateParityTests(_CreateFixture):
    """⑦ ⭐ **对照用例**：同一组参数下，内部端点建的单与对外端点建的单，关键字段一致。

    这是「领域核心只有一份」这条结构判据的**行为判据**（结构那一半是
    「生产代码里把 `OrderSerializer` 落库成订单的调用点只有一处」，读数见交付报告）。
    ⚠️ 两处**有意**的不同也要钉住（否则"一致"会被读成"两条路一模一样"）：
    `created_by`（用户名 vs 应用标签）、`sales_user`（有归属人 vs 没有）、
    `sales_agent`（显示名 vs 空串）—— 它们都是"谁在录"这件事在两条路上的**真实差异**。
    """

    def _create_via_internal(self, body):
        response = self.internal.post(
            INTERNAL_CREATE_PATH, body, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = response.json()
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def test_the_internal_and_external_endpoints_create_the_same_order(self):
        internal_body = {
            "game": self.game_a.pk,
            "project_text": "满星 甲",
            "total_price": "120.00",
            "source_order_no": "TB-PARITY-0001",
            "content": "客户原文",
        }
        external_body = dict(internal_body, source_order_no="TB-PARITY-0002")

        internal_payload = self._create_via_internal(internal_body)
        external_payload = self.assert_ok(
            self._post(external_body, key="probe-create-key-6001")
        )
        self.assertEqual(self.orders().count(), 2)
        internal = self.orders().get(order_no=internal_payload["order_no"])
        external = self.orders().get(order_no=external_payload["order_no"])

        # ① 关键字段**逐条一致**（判据打在库里）。
        for field in (
            "status",
            "total_price",
            "project_text",
            "content",
            "game_id",
            "refund_fee",
            "payment_status",
            "is_urgent",
        ):
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(internal, field), getattr(external, field), field
                )
        # ② 绩效行**都建了**，且形状一致（MAIN、没有接手人、预计金额同源）。
        for order in (internal, external):
            with self.subTest(order=order.order_no):
                main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
                self.assertIsNone(main.user_id)
                self.assertEqual(main.temp_amount, order.hall_price or Decimal("0.00"))
        # ③ 留痕都有一行 `order.create`。
        for order in (internal, external):
            self.assertTrue(
                OperationLog.objects.filter(
                    action=CREATE_ACTION, order_no=order.order_no
                ).exists()
            )
        # ④ 两处**有意**的不同：录单人那三格（对外没有"用户"这个人）。
        self.assertEqual(internal.created_by, "录单探针对照客服甲")
        self.assertEqual(external.created_by, "开放平台应用 录单探针应用甲")
        self.assertIsNotNone(internal.sales_user_id)
        self.assertIsNone(external.sales_user_id)
        # `sales_agent`（接单销售客服那一列）：内部按**录单人的显示名**兜底；对外**空串**
        # （那条路上没有"客服"这个人可兜底，⛔ 不编一个名字）。
        self.assertEqual(internal.sales_agent, "录单探针对照客服甲")
        self.assertEqual(external.sales_agent, "")
        # ⑤ 留痕的**操作人来源**也不同（对外是系统行）——两条纪律各自钉住。
        self.assertIsNotNone(
            OperationLog.objects.get(
                action=CREATE_ACTION, order_no=internal.order_no
            ).actor_id
        )
        self.assertIsNone(
            OperationLog.objects.get(
                action=CREATE_ACTION, order_no=external.order_no
            ).actor_id
        )
