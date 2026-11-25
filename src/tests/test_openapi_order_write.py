"""T5：开放平台 v2 **写端点**的判据（设计文档 `2026-10-08-open-platform-v2-parity-design.md`
§6.1 / §6.2 / §6.3；`AGENTS.md` §5-E / §5-G）。

## 本文件证什么（每条都能独立地红）

1. **幂等四态**（§6.3）：新键 ⇒ 执行副作用 + 落快照；同键同体 ⇒ **回放**且载荷逐字相同、
   ⭐ **副作用只发生一次**（判据打在**库里的行数与值**上，另加响应载荷这一条能分辨
   「回放」与「再执行一次」的腿）；同键异体 ⇒ `1000`；同键但快照为空 ⇒ `1001`。
   另加两条边界：缺键 / 键长不合规 ⇒ `1000`；**可预期的业务失败 ⇒ 占位行跟着回滚**
   （同一个键**下次可以重试**，⛔ 不是"永远卡在正在处理中"）。
2. **并发那一支真的用 savepoint**：把"查现有行"打桩成"没查到"（于是真的走到 `INSERT`
   撞唯一约束那条路）⇒ 回 `1001` 信封，⛔ **不是** 500、也不是 `TransactionManagementError`。
3. **改状态**：合法流转 ⇒ 200 + **库里**状态变了；非法流转 ⇒ 业务码 + **库里一字未变**
   （状态、`updated_by`、留痕行数一起判）。
4. **改发单价**：正常价 ⇒ 落库 + 响应里是**两位小数字符串**；已结账 ⇒ `1014`（且库里价未变）；
   ⭐ 边界 `0` 与**负数**各自按既有语义处理（⛔ 不用真值判断，`AGENTS.md` §5-E / E-0021）。
5. ⭐ **留痕**：每次**改动型**写入产生**恰好一行** `OperationLog`，该行是**系统行**
   （`actor_id is None`）、`actor` 里写着**哪个应用**；并且 `app_secret` **不在任何留痕列**里
   （照 `tests/test_openapi_app_api.py` 的 `AppSecretNeverLoggedTests` 的手法：打在**数据库行**上，
   让数据库自己逐列 `CAST(… AS text) = 密钥`）。
   ⚠️ **同值写入不写留痕行**（R27.2 的既有口径，内网同款）⇒ 本文件**单独一条**把它钉住
   （⛔ 不假装"每次写都有一条"；见 `test_a_same_value_price_write_records_nothing`）。
6. **只认令牌 / 租户边界**：会话请求 ⇒ `401`（对照：同一会话打 `/api/auth/me` ⇒ 200）；
   租户 A 的令牌打**租户 B 的域名** ⇒ `401`；B 的令牌拿 A 的**单号** ⇒ `1002`
   （⭐ 并配一条反向对照：B 自己的单号在 B 上**能改成功** ⇒ 证明那条路真的通）。
7. **畸形入参不 500**（超长 `order_no` / 中文状态标签 / 非法金额 / 非法键），逐个写清期望码。
8. ⭐ **结构网的两条腿**：那条 `status/` 路径**不再**在只读反向钉子里、而它的 `POST`
   **确实**被当成写端点管住（按覆盖率网自己的口径复核）；另一条带 `<order_no>` 的只读路径
   （`images/`）仍在只读表里（对照：⛔ 不是把那张表整张放宽）。

## 夹具纪律

- 凭据 / 单号一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的数据无关。
- 跨域名请求结束后连接**停在那个租户的 schema 上**（`TenantMainMiddleware` 进来时 set、
  响应后**不还原**）⇒ 每个发请求的辅助函数都用 `try/finally` 把连接**钉回租户 A**，
  `setUp` 第一行也显式 `set_tenant`。
- `tokens.issue()` **当场签发**（用它自己的密钥）⇒ 本文件不复制任何签名 / 编码逻辑。
- ⚠️ **每条用例内部的 401 次数别超过 20**：Bearer 认证那条"按来源 IP 的失败闸"
  （`authentication.IP_FAILURE_MAX`）会把第 21 次**从同一 IP** 的失败直接挡掉，
  于是断言会从"我期望的 401"变成"闸门给的 401"（读数还在，但覆盖的东西变了）。
  每条用例结束时那批计数随事务回滚 ⇒ 只要**单条**用例内不超 20 次就够。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client
from django.urls import get_resolver
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.openapi import idempotency, tokens
from apps.openapi.credentials import OPENAPI_SPEC, spec_of
from apps.openapi.idempotency import remember_source, seen_source_response
from apps.openapi.models import ApiApp, OpenApiIdempotency
from apps.platform.provisioning import provision_tenant
from apps.takerapi.models import TakerApp, TakerApiIdempotency
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致；⚠️ 都带尾斜杠）──────────────
STATUS_TEMPLATE = "/api/open/v1/orders/{order_no}/status/"
ASSIGN_PRICE_TEMPLATE = "/api/open/v1/orders/{order_no}/assign-price/"
#: 会话认证的对照端点（本文件用它证明"会话本身是好的"）。
SESSION_CONTROL_PATH = "/api/auth/me"

#: 业务码 / 状态码的**字面值**（`AGENTS.md` §5-E：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
CONFLICT = 1001
BUSINESS = 1002
SETTLED_STATE_INVALID = 1014
UNAUTHORIZED = 401

#: 载荷键集：**写死的字面量**（⛔ 不从实现 import —— import 常量的话
#: 「改实现 + 改常量」一起改就不会红）。
ENVELOPE_KEYS = {"code", "msg", "data"}
STATUS_PAYLOAD_KEYS = {"order_no", "status", "status_text"}
PRICE_PAYLOAD_KEYS = {"order_no", "hall_price"}

#: 幂等键的契约长度（§6.3）：8–128。
MIN_KEY = 8
MAX_KEY = 128
#: 幂等台账的保留期与单次清理上限（§6.3 / T5 任务书）：30 天 / 500 行。
RETENTION_DAYS = 30
PRUNE_CAP = 500

# ── 合成凭据（⛔ 不是任何真实租户的凭据）──────────────────────────────────────────
APP_NAME = "写端点探针应用甲"
APP_KEY = "probewritekey0000001"
APP_SECRET = "probe-write-secret-0000000000001"
APP_NAME_B = "写端点探针应用乙"
APP_KEY_B = "probewritekey0000002"
APP_SECRET_B = "probe-write-secret-0000000000002"

#: 租户 B 的域名与 schema（刻意与既有跨租户用例不同 ⇒ 同一次运行里不会撞 `Domain` 唯一约束）。
DOMAIN_B = "openapi-write-iso-b.test.local"
SCHEMA_B = "openapi_write_iso_b"

#: 两侧订单号取值**不同**（同名会让负断言失去判别力）。
ORDER_NO_A = "20261008000000000201"
ORDER_NO_B = "20261008000000000202"
A_ORDER_PK = 92000001
B_ORDER_PK = 92000002

#: 来源 IP（文档保留段 203.0.113.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.61"

#: 留痕的动作码（与 `order_writes` 内部那两处**逐字相同**）。
TRANSITION_ACTION = "order.transition"
OFFER_PRICE_ACTION = "order.change_offer_price"
#: `OperationLog.log_type` 的字面值：`LogType.ORDER` = 10（照既有用例的写法写数字）。
LOG_TYPE_ORDER = 10

#: 非法流转那一档的期望文案**片段**（业务方给的逐字文案，见 `order_status`）。
COMPLETE_REQUIRES_REVIEW_FRAGMENT = "必须先把订单状态改成待验收"


class _WriteFixture(TenantTestCase):
    """租户 A（`TenantTestCase` 的 schema）+ 租户 B（`SCHEMA_B`）两侧的最小数据。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase` 的事务里。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="开放平台写端点租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.domain_a = self.domain.domain
        self.game_a = Game.objects.create(name="测试游戏甲")
        #: ⭐ MAIN 绩效行的 `user` **故意留空**：D2 那道闸（「有接手人 ⇒ 不许回池」）会让
        #: 「UNASSIGNED → WAITING_GRAB」这条合法边被拒 ⇒ 夹具若有接手人，改状态用例测的
        #: 就不是它想测的东西了。已结账那一档由 `OrderAssignPriceTests` 自己另造一张单。
        self.moment = timezone.now()
        self.order_a = Order.objects.create(
            pk=A_ORDER_PK,
            game=self.game_a,
            order_no=ORDER_NO_A,
            project_text="测试占位项目甲",
            status=OrderStatus.UNASSIGNED,
            total_price=Decimal("100.00"),
            hall_price=Decimal("44.00"),
        )
        Performance.objects.create(
            order=self.order_a,
            emp_type=EmpType.MAIN,
            user=None,
            temp_amount=Decimal("20.00"),
            settle_status=SettleStatus.UNSETTLED,
        )
        creator, _ = User.objects.get_or_create(
            username="写端点探针建号人甲", defaults={"password": "unused-in-this-test"}
        )
        self.app_a = ApiApp.objects.create(
            name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET, created_by=creator
        )

        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(name="测试游戏乙")
                self.order_b = Order.objects.create(
                    pk=B_ORDER_PK,
                    game=self.game_b,
                    order_no=ORDER_NO_B,
                    project_text="测试占位项目乙",
                    status=OrderStatus.UNASSIGNED,
                    hall_price=Decimal("11.00"),
                )
                Performance.objects.create(
                    order=self.order_b,
                    emp_type=EmpType.MAIN,
                    user=None,
                    temp_amount=Decimal("1.00"),
                    settle_status=SettleStatus.UNSETTLED,
                )
                creator_b, _ = User.objects.get_or_create(
                    username="写端点探针建号人乙", defaults={"password": "unused-in-this-test"}
                )
                self.app_b = ApiApp.objects.create(
                    name=APP_NAME_B,
                    app_key=APP_KEY_B,
                    app_secret=APP_SECRET_B,
                    created_by=creator_b,
                )
        finally:
            connection.set_tenant(self.tenant)

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _request(
        self,
        method,
        path,
        body=None,
        *,
        key="probe-idem-key-0001",
        app=None,
        domain=None,
        token=None,
        ip=SOURCE_IP,
        omit_key=False,
    ):
        """按 `domain` 发一条**带 Bearer 令牌**的写请求；结束后把连接钉回租户 A。

        ⚠️ 请求体一律 `json.dumps(body)`（**同一个字典两次调用得到同一串字节**）
        ⇒ 「同键同体」那条判据里的"同体"是**真的**同字节（`request_hash` 的口径）。
        """
        app = self.app_a if app is None else app
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if not omit_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        try:
            return Client(SERVER_NAME=domain).generic(
                method,
                path,
                data=json.dumps({} if body is None else body),
                content_type="application/json",
                REMOTE_ADDR=ip,
                **headers,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _post_status(self, body, **kwargs):
        return self._request(
            "POST", STATUS_TEMPLATE.format(order_no=ORDER_NO_A), body, **kwargs
        )

    def _put_price(self, body, **kwargs):
        return self._request(
            "PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=ORDER_NO_A), body, **kwargs
        )

    def _session_request(self, method, path, *, client, body=None):
        """用一条**已登录的会话**发请求（同样把连接钉回租户 A）。"""
        try:
            return client.generic(
                method,
                path,
                data=json.dumps({} if body is None else body),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="probe-idem-key-0001",
            )
        finally:
            connection.set_tenant(self.tenant)

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response, keys):
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        self.assertEqual(set(body["data"]), keys, body)
        return body["data"]

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + **指定的**业务码（⛔ 不只看 msg）。"""
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body

    def _order(self, order_no=ORDER_NO_A):
        return Order.objects.get(order_no=order_no)

    def _logs(self, action, order_no=ORDER_NO_A):
        return OperationLog.objects.filter(action=action, order_no=order_no)


class IdempotencyStateTests(_WriteFixture):
    """① §6.3 的**四态** + 键的边界 + 事务口径（失败 ⇒ 占位行跟着消失）。"""

    def test_a_new_key_executes_the_side_effect_and_stores_the_snapshot(self):
        key = "probe-idem-key-0001"
        response = self._post_status(
            {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=key
        )
        data = self.assert_ok(response, STATUS_PAYLOAD_KEYS)
        self.assertEqual(data["order_no"], ORDER_NO_A)
        self.assertEqual(data["status"], "WAITING_GRAB")
        self.assertEqual(data["status_text"], "待抢单")
        # ① 副作用真的发生了（判据打在**库里的值**上）。
        self.assertEqual(self._order().status, OrderStatus.WAITING_GRAB)
        # ② 快照落了库：方法 / 路径 / 请求体摘要 / 状态码 / 非空快照。
        row = OpenApiIdempotency.objects.get(app=self.app_a, key=key)
        self.assertEqual(row.method, "POST")
        self.assertEqual(row.path, STATUS_TEMPLATE.format(order_no=ORDER_NO_A))
        self.assertEqual(len(row.request_hash), 64)
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.response_snapshot, json.loads(response.content))
        self.assertEqual(set(row.response_snapshot), ENVELOPE_KEYS)

    def test_the_same_key_and_body_replays_and_the_side_effect_happens_once(self):
        """⭐ 回放：载荷**逐字相同**，且副作用**只发生一次**。

        ## 判据与「哪一半能分辨回放 / 再执行一次」（如实写清，⛔ 不夸大）

        - **能分辨的那一半 = 响应**：本用例的这一笔是从 `UNASSIGNED` 改成 `WAITING_GRAB`，
          而状态机**拒绝自环**（`transition_denial`：目标 == 当前 ⇒ 一句人话的拒绝）
          ⇒ 「再执行一次」那一支会拿到业务码 `1002`、`data` 为 `None`，
          于是 `code == 0` 与"载荷逐字相同"两条断言当场红（这正是弄红证明 ① 的靶子）。
        - **"副作用只发生一次"打在库里**：留痕**只有一行**、订单状态**就是那一个值**。
          ⚠️ 这两条在"再执行一次"的世界里**也**成立（自重放的那一笔走不到写库）
          ⇒ 它们**不是**分辨力所在，而是"回放没有留下第二笔痕迹"的**正面陈述**；
          分辨力在那条 `code == 0` / 载荷相同上。⛔ 不把这两条读成"它们能证明回放"。
        """
        key = "probe-idem-key-0002"
        body = {"status": OrderStatus.WAITING_GRAB, "remark": ""}
        first = self._post_status(body, key=key)
        first_data = self.assert_ok(first, STATUS_PAYLOAD_KEYS)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 1)
        after_first = self._order()
        self.assertEqual(after_first.status, OrderStatus.WAITING_GRAB)

        second = self._post_status(body, key=key)
        second_data = self.assert_ok(second, STATUS_PAYLOAD_KEYS)
        # ⭐ 载荷**逐字相同**（整条信封都比：`code` / `msg` / `data` 三个键的值）。
        self.assertEqual(json.loads(second.content), json.loads(first.content))
        self.assertEqual(second_data, first_data)
        # ⭐ 副作用只发生一次（打在库里的行数 / 值上）。
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 1)
        after_second = self._order()
        self.assertEqual(after_second.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(after_second.updated_by, after_first.updated_by)
        self.assertEqual(after_second.updated_at, after_first.updated_at)
        # 台账也仍然只有一行（⛔ 回放不建第二行）。
        self.assertEqual(
            OpenApiIdempotency.objects.filter(app=self.app_a, key=key).count(), 1
        )

    def test_the_same_key_with_a_different_body_is_rejected(self):
        """同键**异体** ⇒ `1000`（⛔ 不静默返回上一个响应），且第二笔**不留副作用**。"""
        key = "probe-idem-key-0003"
        first = self._post_status(
            {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=key
        )
        self.assert_ok(first, STATUS_PAYLOAD_KEYS)
        second = self._post_status({"status": OrderStatus.IN_PROGRESS, "remark": ""}, key=key)
        body = self.assert_business_failure(second, VALIDATION)
        self.assertIn("幂等键", body["msg"])
        # 库里还是第一笔的结果（第二笔没有被执行）。
        self.assertEqual(self._order().status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 1)

    def test_a_missing_key_is_rejected(self):
        response = self._post_status(
            {"status": OrderStatus.WAITING_GRAB, "remark": ""}, omit_key=True
        )
        body = self.assert_business_failure(response, VALIDATION)
        # ⚠️ 这一句是设计文档 §6.3 的**逐字**要求。
        self.assertEqual(body["msg"], "缺少幂等键")
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 0)
        self.assertEqual(OpenApiIdempotency.objects.count(), 0)

    def test_a_key_outside_the_length_contract_is_rejected(self):
        """键长 8–128（含两端）：7 与 129 都不合规 ⇒ `1000`（⛔ 不放行）。"""
        for bad in ("x" * (MIN_KEY - 1), "y" * (MAX_KEY + 1), ""):
            with self.subTest(length=len(bad)):
                response = self._post_status(
                    {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=bad
                )
                self.assert_business_failure(response, VALIDATION)
        # 两端都合规（对照：不是把整段都判非法）。
        for good in ("a" * MIN_KEY, "b" * MAX_KEY):
            with self.subTest(length=len(good)):
                response = self._post_status(
                    {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=good
                )
                self.assert_ok(response, STATUS_PAYLOAD_KEYS)
                # 复位，免得第二次改状态撞自环。
                Order.objects.filter(pk=A_ORDER_PK).update(status=OrderStatus.UNASSIGNED)

    def test_a_running_attempt_returns_conflict(self):
        """第四态：同键、请求相同、但快照为空（上一笔还在执行 / 崩了）⇒ `1001` + 一句人话。"""
        key = "probe-idem-key-0004"
        OpenApiIdempotency.objects.create(
            app=self.app_a,
            key=key,
            method="POST",
            path=STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
            request_hash=hashlib.sha256(
                json.dumps({"status": OrderStatus.WAITING_GRAB, "remark": ""}).encode()
            ).hexdigest(),
            response_snapshot={},
            status_code=200,
        )
        response = self._post_status(
            {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=key
        )
        body = self.assert_business_failure(response, CONFLICT)
        self.assertEqual(body["msg"], "同一个幂等键的请求正在处理中")
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)

    def test_a_business_failure_leaves_no_placeholder_so_the_key_can_be_retried(self):
        """⭐ 可预期的业务失败 ⇒ **占位行跟着回滚**（同一个键**下次可以重试**）。

        判据：第二发用**同一个键、同一个请求体**（那一笔业务上仍然非法）
        ⇒ 它必须**再**回 `1002`，⛔ **不是** `1001`「正在处理中」。
        —— 若占位行被提交掉了（裸 `return` 那个形状），第二发就会撞上第四态
        （这正是弄红证明 ② 的靶子）。
        """
        key = "probe-idem-key-0005"
        body = {"status": OrderStatus.COMPLETED, "remark": ""}
        first = self._post_status(body, key=key)
        self.assert_business_failure(first, BUSINESS)
        self.assertEqual(OpenApiIdempotency.objects.filter(key=key).count(), 0)
        second = self._post_status(body, key=key)
        body_second = self.assert_business_failure(second, BUSINESS)
        self.assertEqual(body_second["msg"], json.loads(first.content)["msg"])
        # 订单一个字未变、也没有留痕。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 0)

    def test_a_lost_race_on_the_unique_constraint_is_a_conflict_not_a_500(self):
        """并发那一支：把「查现有行」打桩成**没查到** ⇒ 真的走到 `INSERT` 撞唯一约束。

        判据 = 回一个正常的 `1001` 信封（⛔ 不是 500、也不是 `TransactionManagementError`）。
        ⚠️ 这一条同时证明"内层 savepoint"这件事真的在做：在 `atomic()` 块里**裸捕**
        `IntegrityError` 之后，任何后续查询都会抛
        `TransactionManagementError: An error occurred in the current transaction…`
        ⇒ 那时这条用例会以 500 / ERROR 红掉（弄红证明用得上）。
        ⚠️ 打桩打在**被查找的模块**（`apps.openapi.idempotency`）上，`AGENTS.md` §5-E / E-0029。
        """
        key = "probe-idem-key-0006"
        OpenApiIdempotency.objects.create(
            app=self.app_a,
            key=key,
            method="POST",
            path=STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
            request_hash="0" * 64,
            response_snapshot={},
            status_code=200,
        )
        with mock.patch.object(idempotency, "_find_existing", return_value=None):
            response = self._post_status(
                {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key=key
            )
        self.assert_business_failure(response, CONFLICT)
        # 那一行还是原来那一行（占位行没被这次请求改坏）。
        row = OpenApiIdempotency.objects.get(app=self.app_a, key=key)
        self.assertEqual(row.request_hash, "0" * 64)
        self.assertEqual(row.response_snapshot, {})
        # 事务没有被弄坏：这次请求之后照样能查库（上面 `.get()` 就是一次真查询）。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)

    def test_the_stored_snapshot_contains_no_credential(self):
        """快照里**不许**有凭据类字段（D-0201 不放松的那一格）。"""
        self.assert_ok(
            self._put_price({"price": "88.00", "reason": ""}, key="probe-idem-key-0007"),
            PRICE_PAYLOAD_KEYS,
        )
        rows = OpenApiIdempotency.objects.filter(app=self.app_a)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        blob = json.dumps(row.response_snapshot)
        self.assertNotIn(APP_SECRET, blob)
        self.assertNotIn("app_secret", blob)
        # 对照：快照**不是**空的（否则上面两条恒真）。
        self.assertTrue(row.response_snapshot["data"])

    def test_a_snapshot_containing_a_credential_is_refused(self):
        """⭐ 那条纪律的**可执行**形式：命中凭据键名 ⇒ 响亮抛 `ValueError`（⛔ 不静默脱敏）。

        ⚠️ 判据打在**守卫自己**上（真实端点的响应体里本来就没有凭据 ⇒ 只测端点会让这条
        "永远绿"）。这也是"这条守卫不是防御性写法"的证明：去掉它，本用例红。
        """
        with self.assertRaises(ValueError):
            idempotency._assert_no_credentials({"code": 0, "data": {"app_secret": "x"}})
        # 嵌套（列表里）同样要被抓到。
        with self.assertRaises(ValueError):
            idempotency._assert_no_credentials({"data": {"rows": [{"password": "x"}]}})
        # 对照：干净的载荷不抛（⛔ 不是"见谁都抛"）。
        idempotency._assert_no_credentials({"code": 0, "msg": "", "data": {"order_no": "X"}})


class IdempotencyPruneTests(_WriteFixture):
    """幂等台账的机会式清理（30 天保留期 / 一次最多 500 行）。"""

    def _seed_old_rows(self, count):
        now = timezone.now()
        OpenApiIdempotency.objects.bulk_create(
            [
                OpenApiIdempotency(
                    app=self.app_a,
                    key=f"probe-old-key-{index:05d}",
                    method="POST",
                    path="/api/open/v1/orders/old/status/",
                    request_hash="f" * 64,
                    response_snapshot={},
                    status_code=200,
                )
                for index in range(count)
            ]
        )
        # ⚠️ `auto_now_add` 只在插入时写一次 ⇒ 用 `.update()` 把它们推到保留期之外
        #    （`update()` 不过 `save()`，⛔ 不会被 `auto_now_add` 覆盖回去）。
        OpenApiIdempotency.objects.filter(key__startswith="probe-old-key-").update(
            created_at=now - timedelta(days=RETENTION_DAYS + 1)
        )

    def test_rows_past_the_retention_window_are_pruned_on_a_new_key(self):
        self._seed_old_rows(2)
        self.assert_ok(
            self._post_status(
                {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key="probe-idem-key-0010"
            ),
            STATUS_PAYLOAD_KEYS,
        )
        cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
        self.assertEqual(
            OpenApiIdempotency.objects.filter(created_at__lt=cutoff).count(), 0
        )
        # 本次那一行还在（对照：⛔ 不是把整张表清空了）。
        self.assertTrue(
            OpenApiIdempotency.objects.filter(key="probe-idem-key-0010").exists()
        )

    def test_pruning_deletes_at_most_the_cap_in_one_pass(self):
        """⭐ 一次最多 500 行：501 行过期 ⇒ 这一趟**恰好**删 500（留 1 行给下一次）。"""
        self._seed_old_rows(PRUNE_CAP + 1)
        self.assert_ok(
            self._post_status(
                {"status": OrderStatus.WAITING_GRAB, "remark": ""}, key="probe-idem-key-0011"
            ),
            STATUS_PAYLOAD_KEYS,
        )
        cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
        self.assertEqual(
            OpenApiIdempotency.objects.filter(created_at__lt=cutoff).count(), 1
        )


class OrderTransitionTests(_WriteFixture):
    """③ 改状态：合法 / 非法 / 留痕 / `remark`。"""

    def test_a_legal_transition_changes_the_status_in_the_database(self):
        data = self.assert_ok(
            self._post_status({"status": OrderStatus.WAITING_GRAB, "remark": ""}),
            STATUS_PAYLOAD_KEYS,
        )
        self.assertEqual(data["status"], OrderStatus.WAITING_GRAB)
        self.assertEqual(data["status_text"], "待抢单")
        row = self._order()
        self.assertEqual(row.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(row.updated_by, f"开放平台应用 {APP_NAME}")

    def test_an_illegal_transition_is_rejected_and_the_database_is_untouched(self):
        """非法流转（`UNASSIGNED → COMPLETED`）⇒ 业务码 + **库里一字未变**。"""
        before = self._order()
        response = self._post_status({"status": OrderStatus.COMPLETED, "remark": ""})
        body = self.assert_business_failure(response, BUSINESS)
        self.assertIn(COMPLETE_REQUIRES_REVIEW_FRAGMENT, body["msg"])
        after = self._order()
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.updated_by, before.updated_by)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.hall_price, before.hall_price)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 0)

    def test_the_transition_writes_exactly_one_system_row_naming_the_app(self):
        """⭐ 留痕：**恰好一行**、是**系统行**、`actor` 里写着**哪个应用**。"""
        self.assert_ok(
            self._post_status({"status": OrderStatus.WAITING_GRAB, "remark": ""}),
            STATUS_PAYLOAD_KEYS,
        )
        logs = self._logs(TRANSITION_ACTION)
        self.assertEqual(logs.count(), 1)
        row = logs.get()
        self.assertEqual(row.log_type, LOG_TYPE_ORDER)
        # 系统行：没有账号主键（`record()` 的 fail-closed 判据要求显式声明 `system=True`）。
        self.assertIsNone(row.actor_id)
        # 「是哪个应用」写在 `actor` 与 `actor_name` 两列上（`_actor_name`：机器行沿用 `actor`）。
        self.assertEqual(row.actor, f"开放平台应用 {APP_NAME}")
        self.assertEqual(row.actor_name, f"开放平台应用 {APP_NAME}")
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, ORDER_NO_A)
        self.assertEqual(row.order_no, ORDER_NO_A)
        self.assertEqual(row.field_name, "status")
        # 前后值是**中文标签**（与内网同一条路径同源）。
        self.assertEqual(row.from_value, "未分配")
        self.assertEqual(row.to_value, "待抢单")

    def test_a_remark_is_stored_verbatim_in_the_log_reason_column(self):
        """⭐ `remark` 落进留痕行的 `reason` 列（**逐字等于**传入值）；不传 ⇒ 空串。"""
        remark = "第三方系统自动改状态：客户已确认代练完成"
        self.assert_ok(
            self._post_status({"status": OrderStatus.WAITING_GRAB, "remark": remark}),
            STATUS_PAYLOAD_KEYS,
        )
        self.assertEqual(self._logs(TRANSITION_ACTION).get().reason, remark)
        # 对照（"不传"这一档）：另一条合法边、不带 `remark` ⇒ 空串（⛔ 不是别的东西）。
        Order.objects.filter(pk=A_ORDER_PK).update(
            status=OrderStatus.WAITING_GRAB, updated_by=""
        )
        self.assert_ok(
            self._post_status(
                {"status": OrderStatus.IN_PROGRESS}, key="probe-idem-key-0020"
            ),
            STATUS_PAYLOAD_KEYS,
        )
        rows = self._logs(TRANSITION_ACTION).order_by("pk")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.last().reason, "")

    def test_a_chinese_label_is_rejected_and_the_database_is_untouched(self):
        """⭐ 只收**枚举值**：中文标签（含正确的那一个）与未知串都回 `1000`。"""
        for bad in ("待抢单", "WAITING_GRAB 待抢单", "waiting_grab", "NOPE"):
            with self.subTest(status=bad):
                response = self._post_status({"status": bad, "remark": ""})
                self.assert_business_failure(response, VALIDATION)
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 0)

    def test_a_missing_status_is_a_parameter_error(self):
        body = self.assert_business_failure(self._post_status({"remark": ""}), VALIDATION)
        self.assertEqual(body["msg"], "缺少 status")


class OrderAssignPriceTests(_WriteFixture):
    """④ 改发单价：正常 / 已结账 / 边界值 / 留痕。"""

    def test_a_normal_price_is_persisted_and_echoed_as_two_decimals(self):
        data = self.assert_ok(
            self._put_price({"price": "88.50", "reason": ""}), PRICE_PAYLOAD_KEYS
        )
        self.assertEqual(data["order_no"], ORDER_NO_A)
        self.assertEqual(data["hall_price"], "88.50")
        self.assertEqual(self._order().hall_price, Decimal("88.50"))
        self.assertEqual(self._order().updated_by, f"开放平台应用 {APP_NAME}")
        logs = self._logs(OFFER_PRICE_ACTION)
        self.assertEqual(logs.count(), 1)
        row = logs.get()
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor, f"开放平台应用 {APP_NAME}")
        self.assertEqual(row.field_name, "hall_price")
        self.assertEqual(row.from_value, "44.00")
        self.assertEqual(row.to_value, "88.50")

    def test_the_reason_is_stored_verbatim_in_the_log_reason_column(self):
        reason = "第三方按新报价改发单价"
        self.assert_ok(self._put_price({"price": "66.00", "reason": reason}), PRICE_PAYLOAD_KEYS)
        self.assertEqual(self._logs(OFFER_PRICE_ACTION).get().reason, reason)

    def test_a_same_value_price_write_records_nothing(self):
        """⚠️ **同值写入不写留痕行**（R27.2 的既有口径，内网 `/hall-price/` 同款）。

        ⛔ 本文件**不**把它写成"每次写都有一条留痕"：那条口径是领域核心的既有裁定
        （`change_hall_price` 内部：`price != old_price` 才 `record`），T5 ⛔ 不改它。
        ⇒ 这里把它**显式钉住**（而不是让它静默），免得后来者以为漏了埋点。
        ⚠️ 但**仍然写库**（`updated_by` / `updated_at` 会推）——那不是本用例的判据。
        """
        self.assert_ok(self._put_price({"price": "44.00", "reason": "同价"}), PRICE_PAYLOAD_KEYS)
        self.assertEqual(self._order().hall_price, Decimal("44.00"))
        self.assertEqual(self._logs(OFFER_PRICE_ACTION).count(), 0)

    def test_a_settled_order_is_rejected_with_1014(self):
        """已结账 ⇒ `1014`（⛔ 别改这个码），且**一行不写**。"""
        settled = Order.objects.create(
            game=self.game_a,
            order_no="20261008000000000203",
            project_text="测试占位项目（已结账）",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("44.00"),
        )
        perf = Performance.objects.create(
            order=settled,
            emp_type=EmpType.MAIN,
            user=None,
            temp_amount=Decimal("20.00"),
            settle_status=SettleStatus.SETTLED,
            settled_at=self.moment,
            settled_amount=Decimal("20.00"),
        )
        # 夹具自证：那一行**真的**被既有判据认成"已结账"（`Performance.is_settled` 的三字段并集）
        # ——⛔ 不是靠 `settle_status` 单列猜的。
        perf.refresh_from_db()
        self.assertTrue(perf.is_settled)
        response = self._request(
            "PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=settled.order_no),
            {"price": "99.00", "reason": ""},
        )
        self.assert_business_failure(response, SETTLED_STATE_INVALID)
        settled.refresh_from_db()
        self.assertEqual(settled.hall_price, Decimal("44.00"))
        self.assertEqual(settled.updated_by, "")
        self.assertEqual(
            OperationLog.objects.filter(order_no=settled.order_no).count(), 0
        )
        # 占位行也随着那一笔失败一起回滚（这个键**下次可以重试**）。
        self.assertEqual(OpenApiIdempotency.objects.count(), 0)

    def test_zero_and_negative_prices_follow_the_existing_semantics(self):
        """⭐ 边界：`0` 与**负数**都合法（D-0015）——判据用 `is not None` 语义，⛔ 不用真值。

        ⛔ 不写"必须 > 0"的断言：那会把本仓的既有口径（负值 = 冲抵 / 调整）改成错的。
        """
        data = self.assert_ok(
            self._put_price({"price": "0", "reason": ""}, key="probe-idem-key-0030"),
            PRICE_PAYLOAD_KEYS,
        )
        self.assertEqual(data["hall_price"], "0.00")
        self.assertIsNotNone(self._order().hall_price)
        self.assertEqual(self._order().hall_price, Decimal("0.00"))

        data = self.assert_ok(
            self._put_price({"price": "-5.5", "reason": ""}, key="probe-idem-key-0031"),
            PRICE_PAYLOAD_KEYS,
        )
        self.assertEqual(data["hall_price"], "-5.50")
        self.assertEqual(self._order().hall_price, Decimal("-5.50"))
        # 两次都是从"有值 → 不同值"⇒ 各一条留痕（⛔ 不是被真值判断吞掉）。
        self.assertEqual(self._logs(OFFER_PRICE_ACTION).count(), 2)

    def test_an_illegal_price_is_a_parameter_error(self):
        for bad in ("abc", "1.234", "", "50%", "1" * 13):
            with self.subTest(price=bad):
                response = self._put_price({"price": bad, "reason": ""})
                self.assert_business_failure(response, VALIDATION)
        self.assertEqual(self._order().hall_price, Decimal("44.00"))
        self.assertEqual(self._logs(OFFER_PRICE_ACTION).count(), 0)

    def test_a_missing_price_is_a_parameter_error(self):
        body = self.assert_business_failure(self._put_price({"reason": ""}), VALIDATION)
        self.assertEqual(body["msg"], "缺少 price")


class SecretNeverLoggedTests(_WriteFixture):
    """⭐ `app_secret` 不进任何**留痕列**（D-0201 不放松的那一格；手法照
    `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests`：打在**数据库行**上）。"""

    def _secret_columns_hit(self, secret):
        """让**数据库自己**逐列比一遍（`CAST(col AS text) = %s`，⛔ 不用 `LIKE` 的转义坑）。"""
        hits = []
        table = OperationLog._meta.db_table
        for field in OperationLog._meta.concrete_fields:
            count = OperationLog.objects.extra(
                where=[f'CAST("{table}"."{field.column}" AS text) = %s'], params=[secret]
            ).count()
            if count:
                hits.append((field.name, count))
        return hits

    def test_no_secret_ever_reaches_the_audit_trail(self):
        # 两条写路径都走一遍（改状态 + 改发单价）——⚠️ **两个不同的键**：同一个键配不同的
        # 请求会被幂等契约拒掉（`1000`），那是另一条用例的靶子。
        self.assert_ok(
            self._post_status(
                {"status": OrderStatus.WAITING_GRAB, "remark": "第三方自动改状态"},
                key="probe-idem-key-0060",
            ),
            STATUS_PAYLOAD_KEYS,
        )
        self.assert_ok(
            self._put_price(
                {"price": "77.00", "reason": "第三方改价"}, key="probe-idem-key-0061"
            ),
            PRICE_PAYLOAD_KEYS,
        )
        # 前置：两条留痕**真的**都在（否则本用例的覆盖面是假的）。
        self.assertTrue(self._logs(TRANSITION_ACTION).exists())
        self.assertTrue(self._logs(OFFER_PRICE_ACTION).exists())
        self.assertEqual(self._secret_columns_hit(APP_SECRET), [])
        text = "\n".join(
            str(getattr(row, field.attname, ""))
            for row in OperationLog.objects.all()
            for field in row._meta.concrete_fields
        )
        self.assertNotIn(APP_SECRET, text)


class IsolationAndBoundaryTests(_WriteFixture):
    """⑥ 只认令牌 / 租户边界；⑦ 畸形入参不 500。"""

    def test_a_session_request_is_unauthorized(self):
        client = make_logged_in_client(domain=self.domain_a)
        # 对照：会话本身是好的。
        self.assertEqual(self._session_request("GET", SESSION_CONTROL_PATH, client=client).status_code, 200)
        for method, path in (
            ("POST", STATUS_TEMPLATE.format(order_no=ORDER_NO_A)),
            ("PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=ORDER_NO_A)),
        ):
            with self.subTest(path=path):
                response = self._session_request(
                    method,
                    path,
                    client=client,
                    body={"status": OrderStatus.WAITING_GRAB, "remark": "", "price": "1.00"},
                )
                self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:300])
                self.assertEqual(json.loads(response.content)["code"], UNAUTHORIZED)
        # 两个端点都**没有**写库。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._order().hall_price, Decimal("44.00"))

    def test_a_token_from_another_tenant_is_unauthorized(self):
        """租户 A 的令牌打**租户 B 的域名** ⇒ `401`（那个 `app_key` 在 B 的库里不存在）。"""
        response = self._request(
            "POST",
            STATUS_TEMPLATE.format(order_no=ORDER_NO_B),
            {"status": OrderStatus.WAITING_GRAB, "remark": ""},
            app=self.app_a,
            domain=DOMAIN_B,
        )
        self.assertEqual(response.status_code, UNAUTHORIZED, response.content[:300])
        with tenant_context(self.tenant_b):
            self.assertEqual(
                Order.objects.get(order_no=ORDER_NO_B).status, OrderStatus.UNASSIGNED
            )
        connection.set_tenant(self.tenant)

    def test_another_tenants_order_cannot_be_written(self):
        """B 的令牌 + B 的域名 + **A 的单号** ⇒ `1002`；⭐ 反向对照：B 自己的单号能改成。"""
        with tenant_context(self.tenant_b):
            order_b_pk = Order.objects.get(order_no=ORDER_NO_B).pk
        connection.set_tenant(self.tenant)
        self.assertEqual(order_b_pk, B_ORDER_PK)  # 夹具自证：B 那张单真的在 B 里

        response = self._request(
            "POST",
            STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
            {"status": OrderStatus.WAITING_GRAB, "remark": ""},
            app=self.app_b,
            domain=DOMAIN_B,
        )
        self.assert_business_failure(response, BUSINESS)
        # A 的单在 A 里**没被改**。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)

        # ⭐ 对照腿（否则"B 这条路本来就是坏的"也能让上面那条绿）。
        control = self._request(
            "POST",
            STATUS_TEMPLATE.format(order_no=ORDER_NO_B),
            {"status": OrderStatus.WAITING_GRAB, "remark": ""},
            app=self.app_b,
            domain=DOMAIN_B,
            key="probe-idem-key-0040",
        )
        self.assert_ok(control, STATUS_PAYLOAD_KEYS)
        with tenant_context(self.tenant_b):
            self.assertEqual(
                Order.objects.get(order_no=ORDER_NO_B).status, OrderStatus.WAITING_GRAB
            )
            self.assertEqual(
                OperationLog.objects.filter(order_no=ORDER_NO_B).count(), 1
            )
        connection.set_tenant(self.tenant)

    def test_malformed_inputs_never_500(self):
        """逐条写清期望码（⛔ 不是"随便回什么都行"；判据里没有 500 这一档）。"""
        long_order_no = "9" * 100
        cases = (
            # (方式, 路径, 请求体, 幂等键, 期望业务码/状态码, 是 HTTP 码吗)
            ("POST", STATUS_TEMPLATE.format(order_no=long_order_no),
             {"status": OrderStatus.WAITING_GRAB, "remark": ""}, "probe-idem-key-0050",
             BUSINESS, False),
            ("PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=long_order_no),
             {"price": "1.00", "reason": ""}, "probe-idem-key-0051", BUSINESS, False),
            ("POST", STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
             {"status": "x" * 300, "remark": ""}, "probe-idem-key-0052", VALIDATION, False),
            ("POST", STATUS_TEMPLATE.format(order_no=ORDER_NO_A),
             {"status": OrderStatus.WAITING_GRAB, "remark": "r" * 300},
             "probe-idem-key-0053", VALIDATION, False),
            ("PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=ORDER_NO_A),
             {"price": "1" * 200, "reason": ""}, "probe-idem-key-0054", VALIDATION, False),
            ("PUT", ASSIGN_PRICE_TEMPLATE.format(order_no=ORDER_NO_A),
             {"price": "1.00", "reason": "r" * 300}, "probe-idem-key-0055",
             VALIDATION, False),
        )
        for method, path, body, key, expected, _is_http in cases:
            with self.subTest(path=path, body=str(body)[:40]):
                response = self._request(method, path, body, key=key)
                self.assertNotEqual(response.status_code, 500, response.content[:300])
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(json.loads(response.content)["code"], expected)
        # 超长 `order_no` 那两条走的是"查不到"那一支 ⇒ 一个字都不该写。
        self.assertEqual(self._order().status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._order().hall_price, Decimal("44.00"))
        self.assertEqual(self._logs(TRANSITION_ACTION).count(), 0)
        self.assertEqual(self._logs(OFFER_PRICE_ACTION).count(), 0)
        # ⭐ 六条全是**失败**（查不到 / 入参不合法）⇒ 它们建的占位行**全部**跟着回滚
        #    （这是"占位行与业务同事务"那条要求的另一条判据）。
        self.assertEqual(OpenApiIdempotency.objects.count(), 0)


class RegistryTests(_WriteFixture):
    """⑧ ⭐ 结构网的两条腿：那条 `status/` 路径**不再**是只读路径，而它的 `POST`
    **确实**被当成写端点管住（按覆盖率网自己的口径复核）。"""

    #: 只读反向钉子里的那条路径（T5 之前它在里面）。
    STATUS_PATH = "api/open/v1/orders/<order_no:order_no>/status/"
    #: ⚠️ 这条路径（`images/`）**不再是**只读路径 —— 认领 `POST .../images/` 落在同一条
#: 路径上（与 `GET` 同住一个视图类，Django 路由先匹配先赢）⇒ 它已从
#: `MUST_HAVE_NO_WRITE_METHOD` 移走；两条新 `POST` 的分类判据住在
#: `tests/test_openapi_order_images.py`——只删不加就是**净损失**。
    IMAGES_PATH = "api/open/v1/orders/<order_no:order_no>/images/"
    #: `images/ticket/`（T10a 的**取票**端点）——它也是新写端点，同样**不许**进只读表。
    TICKET_PATH = "api/open/v1/orders/<order_no:order_no>/images/ticket/"

    @staticmethod
    def _callback_for(method, path):
        """按覆盖率网自己的归一化口径，从 resolver 树里取那个端点的 callback。"""
        from tests.test_operation_log_coverage import _normalise, _walk

        for raw, callback in _walk(get_resolver()):
            if _normalise(raw) == path:
                return callback
        return None

    def test_the_status_path_is_no_longer_read_only_and_its_post_is_instrumented(self):
        """⛔ 两条腿都要有：只把它从只读表里删掉就是**净损失**（那正是静默空洞）。"""
        from tests.test_operation_log_coverage import (
            MUST_HAVE_NO_WRITE_METHOD,
            _certified_as_instrumented,
        )

        # ① 它**不再**在只读反向钉子里。
        self.assertNotIn(self.STATUS_PATH, MUST_HAVE_NO_WRITE_METHOD)
        # ② 原对照句（`assertIn(self.IMAGES_PATH, …)`）的前提——"`images/` 是只读路径"——已被推翻
        # （认领 `POST` 落在同一条路径上）⇒ 断言换成**两条仍然成立**的对照，
        # 判据强度不降：① 那张只读表**没有被整张放宽**（仍有既有的只读路径在表里，
        # 点名两条不带转换器的 v2 只读端点与一条内部只读端点）；② `images/` 与
        # `images/ticket/` 都**不在**表里（前者已移走、后者从来就是写端点）。
        # ⚠️ 两条新 `POST` 的**分类判据**（不许变成"既不在只读表、也不在写表"的静默空洞）
        # 在 `tests/test_openapi_order_images.py` 里，与 T5 给 `status/` 补的那条同形。
        self.assertIn("api/open/v1/ranks/", MUST_HAVE_NO_WRITE_METHOD)
        self.assertIn("api/open/v1/bills/", MUST_HAVE_NO_WRITE_METHOD)
        self.assertIn("api/business/my-orders/", MUST_HAVE_NO_WRITE_METHOD)
        self.assertNotIn(self.IMAGES_PATH, MUST_HAVE_NO_WRITE_METHOD)
        self.assertNotIn(self.TICKET_PATH, MUST_HAVE_NO_WRITE_METHOD)
        # ③ 它的 `POST` 真的被当成写端点管住 —— 按**那张网自己的口径**判（AST 上真的有埋点）。
        callback = self._callback_for("post", self.STATUS_PATH)
        self.assertIsNotNone(callback, "resolver 里找不到 POST .../status/ 的 callback")
        self.assertTrue(
            _certified_as_instrumented(callback, "post"),
            "POST .../status/ 没有被判成已埋点的写端点（要么端点没登记，要么那条链断了）",
        )
        # ④ 另一条新写端点（整改发单价）同样被管住。
        price_callback = self._callback_for(
            "put", "api/open/v1/orders/<order_no:order_no>/assign-price/"
        )
        self.assertIsNotNone(price_callback)
        self.assertTrue(_certified_as_instrumented(price_callback, "put"))

    def test_the_status_route_is_one_pattern_serving_both_handlers(self):
        """⭐ 框架事实：同一条路径在 resolver 树里**只**能有一个 pattern（先匹配先赢）。

        判据三条：① 这条路径恰好**一个** pattern；② 它的视图类**同时**有 `get` 与 `post`；
        ③ 两个处理器各住在**该住**的模块里（读的住 T4 那个模块、写的住 T5 这个模块）
        ⇒ ⛔ 不存在"又注册了一条同 pattern 的路由"那种**永远不会生效**的写法
        （第二条会被第一个 pattern 吃掉，`POST` 会得到 405）。
        """
        from tests.test_operation_log_coverage import _normalise, _owner_of, _walk

        patterns = [
            (raw, callback)
            for raw, callback in _walk(get_resolver())
            if _normalise(raw) == self.STATUS_PATH
        ]
        self.assertEqual(len(patterns), 1, patterns)
        cls = patterns[0][1].cls
        self.assertTrue(hasattr(cls, "get") and hasattr(cls, "post"), cls)
        self.assertEqual(
            _owner_of(cls, "get").__module__, "apps.openapi.api.order_extra_views"
        )
        self.assertEqual(
            _owner_of(cls, "post").__module__, "apps.openapi.api.order_write_views"
        )


class CredentialSpecDrivenLedgerTests(_WriteFixture):
    """⭐ T2（2026-10-08）：**幂等台账由 spec 指定**（⛔ 不是引擎里写死的那张表）。

    ## 这条判据为什么值钱（spec §4 第 1 条的后果）

    改造前 `_find_existing` / `_prune` / `seen_source_response` / `remember_source` 都硬编码
    `OpenApiIdempotency`。打手侧复用同一份引擎时，只要还剩**任何一处**硬编码，
    症状就是「**按 pk 命中另一族的同一号行 ⇒ 把别人的响应回放出去**」——
    认证层那两条 401 判据**抓不到**它（两族各自都认证通过），而既有网只打 openapi 这条路。

    ## 判据（四半，⛔ 不接受"只看一侧"）

    ① 同一颗来源单号在 **openapi 台账**里有一行 ⇒ 换成"台账模型 = 打手侧那张表"的 spec 去查，
       **必须查不到**（`None`）；
    ② 打手侧台账里另外放一行（**另一个**来源单号）⇒ 用打手侧那份 spec 查它**必须命中**
       （证明①不是"这个函数永远回 `None`"）；
    ③ **对照**：默认那一族的 spec 查 ① 那一行 ⇒ 命中并回放（本族的台账**真的在用**）；
    ④ 写那一半：`remember_source` 在打手侧那份 spec 下，行落在**打手侧台账**里
       （键还带着前缀）、而 openapi 台账**一行都没多**。

    ⚠️ 取数一律用**各自那一族的凭据实例**（`ApiApp` / `TakerApp`）：Django 的外键类型检查
    会拒绝"拿 `ApiApp` 去查 `TakerApiIdempotency`"（`ValueError: Must be "TakerApp" instance`）
    —— 那是**框架**的一道保护，⛔ 不构成本条判据（本条的判别力在"查的是哪张表"）。
    """

    #: 合成来源单号（⛔ 不是任何真实单据）。
    SOURCE_NO = "TT-T2-SPEC-0001"
    #: 打手侧那一行用的来源单号（与上面**不同** ⇒ 两条断言各自指向自己那一行）。
    TAKER_SOURCE_NO = "TT-T2-SPEC-0002"

    @staticmethod
    def _taker_spec():
        return replace(OPENAPI_SPEC, idem_model=TakerApiIdempotency)

    def _taker_app(self):
        creator = User.objects.get(username="写端点探针建号人甲")
        return TakerApp.objects.create(
            name="T2 规格探针打手应用甲",
            app_key="probe-taker-spec-key-0001",
            app_secret="probe-taker-spec-secret-00000001",
            taker=creator,
            created_by=creator,
        )

    def _seed_openapi_ledger_row(self):
        """在 **openapi 台账**里放一行（用本族的合成键拼出来的那一颗）。"""
        OpenApiIdempotency.objects.create(
            app=self.app_a,
            key=f"{OPENAPI_SPEC.source_key_prefix}{self.SOURCE_NO}",
            method="POST",
            path="/api/open/v1/orders/",
            request_hash="a" * 64,
            response_snapshot={"code": 0, "msg": "", "data": {"order_no": ORDER_NO_A}},
            status_code=200,
        )

    def test_the_ledger_lookup_follows_the_spec_model(self):
        self._seed_openapi_ledger_row()
        taker_app = self._taker_app()
        TakerApiIdempotency.objects.create(
            app=taker_app,
            key=f"{OPENAPI_SPEC.source_key_prefix}{self.TAKER_SOURCE_NO}",
            method="POST",
            path="/api/taker/v1/orders/",
            request_hash="b" * 64,
            response_snapshot={"code": 0, "msg": "", "data": {"order_no": ORDER_NO_B}},
            status_code=200,
        )
        # ① 打手侧那份 spec 去查 openapi 那一行：同一颗键在 openapi 台账里**不算命中**。
        self.assertIsNone(
            seen_source_response(
                app=taker_app, source_order_no=self.SOURCE_NO, spec=self._taker_spec()
            ),
            "按打手侧台账查却命中了 openapi 台账那一行 ⇒ 台账模型没经 spec 取",
        )
        # ② 打手侧那一行**查得到**（否则 ① 可能是"查什么都空"的恒真式）。
        taker_replay = seen_source_response(
            app=taker_app,
            source_order_no=self.TAKER_SOURCE_NO,
            spec=self._taker_spec(),
        )
        self.assertIsNotNone(taker_replay)
        self.assertEqual(taker_replay.data["data"]["order_no"], ORDER_NO_B)
        # ③ 对照：默认那一族查 ① 那一行 ⇒ 命中（本族台账真的在用）。
        replay = seen_source_response(
            app=self.app_a, source_order_no=self.SOURCE_NO, spec=OPENAPI_SPEC
        )
        self.assertIsNotNone(replay)
        self.assertEqual(replay.data["data"]["order_no"], ORDER_NO_A)

    def test_remember_source_writes_into_the_spec_ledger(self):
        taker_app = self._taker_app()
        request = APIRequestFactory().post("/api/taker/v1/orders/", data={})
        response = Response({"code": 0, "msg": "", "data": {"order_no": ORDER_NO_A}})
        remember_source(
            app=taker_app,
            source_order_no=self.SOURCE_NO,
            request=request,
            response=response,
            spec=self._taker_spec(),
        )
        # ④ 行落在**打手侧**台账里，键仍是那颗合成键（前缀 + 来源单号）。
        rows = list(
            TakerApiIdempotency.objects.values_list("app_id", "key", "status_code")
        )
        self.assertEqual(
            rows,
            [(taker_app.pk, f"{OPENAPI_SPEC.source_key_prefix}{self.SOURCE_NO}", 200)],
            rows,
        )
        # ⑤ 上家侧台账**一行都没多**（两族不共用台账）。
        self.assertEqual(OpenApiIdempotency.objects.count(), 0)

    def test_find_existing_follows_the_spec_model(self):
        """⭐ `_find_existing` 查**哪张台账**由 spec 决定（`_run` 的第一步就是它）。

        为什么单独打这个函数：它是幂等入口的**第一个库读**，也是 spec §4 第 1 条点名的
        那一处（改造前硬编码 `OpenApiIdempotency` ⇒ 打手侧会按 pk 命中上家侧的同一号行、
        把**别人的响应**回放出去）。判据三半（⛔ 不接受只有 ①）：

        ① 打手侧那份 spec 查一颗**只在 openapi 台账里**的键 ⇒ `None`；
        ② 往**打手侧台账**放同一颗键 ⇒ 打手侧 spec 查得到（否则 ① 可能是"查什么都空"）；
        ③ 默认那一族查 openapi 那一行 ⇒ 查得到（本族台账真的在用）。
        ⚠️ 取数用各自那一族的凭据实例（框架的外键类型检查会拒绝混用）。
        """
        key = f"{OPENAPI_SPEC.source_key_prefix}{self.SOURCE_NO}"
        self._seed_openapi_ledger_row()
        taker_app = self._taker_app()
        taker_spec = self._taker_spec()
        # ① 打手侧那份 spec：openapi 台账那一行**不算命中**。
        self.assertIsNone(
            idempotency._find_existing(app=taker_app, key=key, spec=taker_spec),
            "打手侧台账的查找命中了 openapi 台账那一行 ⇒ 台账模型没经 spec 取",
        )
        # ② 打手侧台账里同一颗键 ⇒ 查得到。
        TakerApiIdempotency.objects.create(
            app=taker_app,
            key=key,
            method="POST",
            path="/api/taker/v1/orders/",
            request_hash="c" * 64,
            response_snapshot={},
            status_code=200,
        )
        self.assertIsNotNone(
            idempotency._find_existing(app=taker_app, key=key, spec=taker_spec)
        )
        # ③ 对照：默认那一族查 openapi 那一行 ⇒ 查得到。
        self.assertIsNotNone(
            idempotency._find_existing(app=self.app_a, key=key, spec=OPENAPI_SPEC)
        )

    def test_the_spec_resolution_comes_from_the_authenticator(self):
        """⭐ `spec_of(request)`：这一次请求属于**哪一族**从**认证态**取（⛔ 不猜）。

        为什么它承重：`order_create_views` 那条"同一应用 + 同一来源单号"的去重
        （`seen_source_response` / `remember_source`）按请求取这一份；取错 = 去写**另一族**
        的台账 / 桶；取不到时**静默按上家侧处理**，则是最坏的形状。

        判据（三半，⛔ 不接受只有前一半）：
        ① 认证器带着**另一份** spec ⇒ 取到的就是**那一份**（⛔ 不是默认那一份）；
        ② 请求上**没有** `successful_authenticator` 这一格 / 它是 `None` ⇒ 回**默认**那一份
           （= 直接调用 / 单元用法 ⇒ 改造前那条硬编码路径，行为逐字不变）；
        ③ ⭐⭐ **T2 修复轮**：认证器**在**、但缺 `spec` 属性 / `spec` 不是 `CredentialSpec`
           ⇒ **响亮抛 `ImproperlyConfigured`**（⛔ 不再回默认 —— 那会让"认证器忘了声明 spec"
           在第一次请求静默按上家侧处理）。

        ⚠️ ③ 是**有意**的行为变化（本任务唯一允许的那一条），判据从"等于 `OPENAPI_SPEC`"
        改成"抛错"，⛔ 其余两条断言一字未动。
        """
        taker_spec = self._taker_spec()

        class _Authenticator:
            spec = taker_spec

        class _RequestWithSpec:
            successful_authenticator = _Authenticator()

        class _RequestWithoutAuthenticator:
            successful_authenticator = None

        class _RequestWithoutTheAttribute:
            """DRF 之外的形状：请求对象上**压根没有**这一格（直接调用 / 单元用法）。"""

        class _AuthenticatorWithJunk:
            spec = "not-a-credential-spec"

        class _RequestWithJunk:
            successful_authenticator = _AuthenticatorWithJunk()

        class _AuthenticatorWithoutSpec:
            """漏声明 `spec` 的认证器（本任务要让它**响亮**的那一档）。"""

        class _RequestWithoutSpec:
            successful_authenticator = _AuthenticatorWithoutSpec()

        self.assertIs(spec_of(_RequestWithSpec()), taker_spec)
        self.assertIs(spec_of(_RequestWithoutAuthenticator()), OPENAPI_SPEC)
        self.assertIs(spec_of(_RequestWithoutTheAttribute()), OPENAPI_SPEC)
        # ⭐⭐ 这两档是**响亮抛错**（⛔ 不是回默认）。
        with self.assertRaises(ImproperlyConfigured) as junk_ctx:
            spec_of(_RequestWithJunk())
        self.assertIn("_AuthenticatorWithJunk", str(junk_ctx.exception))
        with self.assertRaises(ImproperlyConfigured) as missing_ctx:
            spec_of(_RequestWithoutSpec())
        self.assertIn("_AuthenticatorWithoutSpec", str(missing_ctx.exception))
        # ⛔ 错误消息里**不带任何凭据值**（只说"哪一类认证器 / 缺什么"）。
        for ctx in (junk_ctx, missing_ctx):
            message = str(ctx.exception)
            for leak in (APP_SECRET, APP_KEY, APP_SECRET_B, APP_KEY_B):
                self.assertNotIn(leak, message)

    def test_the_idempotent_decorator_requires_an_explicit_spec(self):
        """⭐⭐ T2 修复轮：`@idempotent` 的 `spec` 是**必填** keyword-only（签名层判据）。

        改之前是裸 `@idempotent`（spec 由 `_run` 在运行期从认证态推）⇒ 同一种错
        （"忘了说自己是哪一族"）在这里**静默按上家侧处理**，而在 `_find_existing` 那几处
        是必填、当场 `TypeError`。现在漏传 = 装饰那一刻 `TypeError`
        （⛔ 不是等第一次请求按上家侧静默落库）。
        """
        with self.assertRaises(TypeError):
            idempotency.idempotent(lambda self, request: None)
        # 位置参数也不收（keyword-only）：`@idempotent(OPENAPI_SPEC)` 同样响亮。
        with self.assertRaises(TypeError):
            idempotency.idempotent(OPENAPI_SPEC)
        # ⚠️ 这一条必须留着：给 `spec` 补一个默认值（`= OPENAPI_SPEC`）时**只有它**会红
        # —— 裸 `@idempotent` / 位置参数两种写法在"keyword-only 工厂"这个形状下**照样**
        # 抛 `TypeError`（与有没有默认值无关）⇒ 不写这一条，"必填"这件事就没被钉住。
        with self.assertRaises(TypeError):
            idempotency.idempotent()
        # 对照：显式传了 ⇒ 正常拿到一个可用的装饰器（否则上面几条可能是"这个函数恒抛"）。
        decorated = idempotency.idempotent(spec=OPENAPI_SPEC)(
            lambda self, request: Response({"code": 0, "msg": "", "data": None})
        )
        self.assertTrue(callable(decorated))

    def test_the_run_writes_into_the_declared_spec_ledger(self):
        """⭐⭐ T2 修复轮：`_run` 落**哪一族**的台账由**调用方声明**的那一份决定。

        判据（两半）：① 声明打手侧那一份 ⇒ 占位行 / 快照落 `TakerApiIdempotency`、
        键就是这次请求的 `Idempotency-Key`；② ⛔ 上家侧台账**一行都没多**。
        ⚠️ 取数用各自那一族的凭据实例（框架的外键类型检查会拒绝混用）。
        """
        taker_app = self._taker_app()
        raw = APIRequestFactory().post(
            "/api/taker/v1/orders/",
            data=b"{}",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="t2fix-spec-declared-0001",
        )
        request = Request(raw)
        request.auth = taker_app
        response = idempotency._run(
            request,
            lambda: Response({"code": 0, "msg": "", "data": {"order_no": ORDER_NO_A}}),
            spec=self._taker_spec(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(TakerApiIdempotency.objects.values_list("app_id", "key", "status_code")),
            [(taker_app.pk, "t2fix-spec-declared-0001", 200)],
        )
        self.assertEqual(OpenApiIdempotency.objects.count(), 0)
