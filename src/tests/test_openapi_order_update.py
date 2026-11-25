"""T7：开放平台**改单**端点（`PATCH /api/open/v1/orders/<order_no>/`）的判据（v2 §6.1 / §6.2 / §6.3）。

## 本文件证什么（逐条对应任务书那一节）

1. **改一个允许字段 ⇒ 200 + 库里的值真的变了**（判据打在**库里的那一行**，⛔ 不看响应的自述）；
   同一条用例顺带钉住**留痕**：`update_order_fields` 内部那一处 `record(...)` 写下的
   `OperationLog` 行的动作码 / 操作人 / `field_name` / 前后值（⛔ 不另建一套留痕）。
2. ⭐ **只读字段（`status` / 金额类 / `sales_user` / `order_no` …）传了不生效**：
   判据打在**库里**——那些列**一字未变**；且同一次请求里带一个**可写**字段当**阳性对照**
   （否则"整条载荷都没生效"也会让这条绿）。
3. **幂等四态**（新键 / 同键同体**回放**且**副作用只发生一次** / 同键异体 `1000` / 缺键 `1000`）
   + ⭐ 一条**业务失败后同一个键可以重试**的用例（钉住幂等入口那条
   `transaction.set_rollback(True)`：可预期的失败⛔ 不许把占位行提交掉）。
4. **凭据与租户边界**：会话请求打 `PATCH` ⇒ **401**；跨租户（A 的令牌改 B 的单）⇒ 拒 +
   **库里一字未变**（A 侧那行、B 侧那行各查一次）。
5. **畸形入参**：超长 `order_no`（路径段）/ 超长文本 / 非法值 / 非对象请求体 / 畸形 JSON
   ⇒ **一条都不许 500**，每条写清期望码。
6. ⭐ **详情 `GET` 切 Bearer 之后**：合法令牌 ⇒ **200**；会话 ⇒ **401**；跨租户 ⇒ 按契约拒；
   ⛔ 旧签名头 ⇒ **401 + Bearer 那句**（"凭据换过了"这条事实的判据）。

## 夹具纪律

- 凭据一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的 appKey / secret 无关；
- 请求体一律 `json.dumps(body)`（**同一个字典两次调用得到同一串字节**）——幂等契约里的
  "同体"是 `request_hash` 的**字节**口径，"同字典"不等于"同字节"（键序 / 空白都可能不同）；
- 读面那条（第 6 组）用**另一个**应用，⛔ 不用写端点那个应用（两个应用今天授权面**相同**：
  都是 §13.10 的"整租户"；⛔⭐ **改准（T10）**：原句里的"勾了节点 + 游戏范围""一个节点都不勾、
  一款游戏都不勾"已随那三列删除，见 §13.1）。

## 本文件⛔ 不证明什么

- 不证明"第三方文档与实现一致"（那一层在 `tests/test_openapi_doc_examples.py`）；
- 不证明"每一个可写字段都真的写得进去"（那要 25 条用例；这里用**代表性字段** +
  一条"字段集来自序列化器"的结构判据覆盖 —— 见 `test_the_writable_field_set_is_the_serializers`）。
"""
import json
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.api.serializers import OrderUpdateSerializer
from apps.business.models import Game, Order, OrderStatus
from apps.openapi import tokens
from apps.openapi.api import order_write_views
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.idempotency import (
    DIFFERENT_REQUEST_MSG,
    KEY_LENGTH_MSG,
    MISSING_KEY_MSG,
)
from apps.openapi.models import ApiApp, OpenApiIdempotency
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 详情 / 改单**同一条**路径的模板（与 `apps/openapi/urls.py` 的 pattern 逐字一致；⚠️ 带尾斜杠）。
DETAIL_TEMPLATE = "/api/open/v1/orders/{order_no}/"

# ── 合成凭据（⛔ 不是任何真实租户的凭据）──────────────────────────────────────────
#: 写端点那个应用（§13.10：不装任何权限类 ⇒ 照样写得进去）。
#: ⛔ 应用不勾节点 / 游戏（那三列已随 v1 面删除，§13.1）。
WRITER_KEY = "probeupdatekey000001"
WRITER_SECRET = "probe-update-secret-00000000001"
WRITER_NAME = "改单探针应用甲"
#: 读面那个应用：用来打详情 `GET` 与做"同一个出口"的对照。
READER_KEY = "probeupdateread000001"
READER_SECRET = "probe-update-read-secret-0001"
READER_NAME = "改单探针只读应用"

#: 租户 B 的域名与 schema（与既有跨租户用例刻意不同 ⇒ 同一次运行里不撞 `Domain` 唯一约束）。
DOMAIN_B = "openapi-update-iso-b.test.local"
SCHEMA_B = "openapi_update_iso_b"

#: 来源 IP（文档保留段 203.0.113.0/24，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.97"

# ── 业务码 / 状态码的**字面值**（`AGENTS.md` §5-E：写数字，让"码值被改掉"在用例里可见）──
OK = 0
VALIDATION = 1000
BUSINESS = 1002
UNAUTHENTICATED = 401

#: 统一信封的键集（恰好三个）。
ENVELOPE_KEYS = {"code", "msg", "data"}

#: 留痕的动作码（与领域核心那一处**逐字相同**）。
UPDATE_ACTION = "order.update"

#: 订单号：本租户那张（A）与租户 B 那张（两侧取值不同 ⇒ 跨租户判据不靠主键，见 `AGENTS.md` §5-G）。
ORDER_NO_A = "20261008000000000701"
ORDER_NO_B = "20261008000000000702"

MIN_KEY = 8


class _UpdateFixture(TenantTestCase):
    """租户 A（`TenantTestCase` 的 schema）+ 租户 B（`SCHEMA_B`）两侧的最小数据。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase` 的事务里。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="对外改单租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A（上一条用例的跨域名请求会把连接留在那个租户的 schema 上）。
        connection.set_tenant(self.tenant)
        super().setUp()
        # ⚠️ 按来源 IP 的失败计数自 T3 起落 **DB 行**（随本用例的事务回滚）⇒ 这句只是"只加不删"。
        cache.clear()
        self.domain_a = self.domain.domain
        self.game_a = Game.objects.create(name="改单探针游戏甲")
        self.other_game = Game.objects.create(name="改单探针没勾给读应用的游戏")
        creator, _ = User.objects.get_or_create(
            username="改单探针建号人甲", defaults={"password": "unused-in-this-test"}
        )
        #: 写端点那个应用：**零节点、零游戏**（§13.10 ⇒ 本文件的头几个用例靠它证明
        #: 「改单不装节点门、也不做游戏范围过滤」）。
        self.writer_app = ApiApp.objects.create(
            name=WRITER_NAME, app_key=WRITER_KEY, app_secret=WRITER_SECRET, created_by=creator
        )
        #: 读面那个应用（详情 `GET` 用）。
        #: ⛔ 读面应用不挂节点 / 游戏范围（那三列已随 v1 面删除，§13.1 / §13.10）。
        self.reader_app = ApiApp.objects.create(
            name=READER_NAME, app_key=READER_KEY, app_secret=READER_SECRET, created_by=creator
        )

        self.order = Order.objects.create(
            game=self.game_a,
            order_no=ORDER_NO_A,
            project_text="改单探针项目甲",
            content="客户原文甲",
            total_price="88.00",
            hall_price="30.00",
            source_order_no="TT-UPDATE-0001",
        )
        #: 另一个内部账号：只读字段那一条要拿一个**真实的** `sales_user` 去试。
        self.sales_user = User.objects.create(
            username="改单探针客服甲", password="unused-in-this-test"
        )
        #: 会话客户端（打 `PATCH` 必须 401）。
        self.session_client = make_logged_in_client(
            domain=self.domain_a, username="改单探针会话账号甲", is_superuser=True
        )

        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(name="改单探针游戏乙租户")
                creator_b, _ = User.objects.get_or_create(
                    username="改单探针建号人乙", defaults={"password": "unused-in-this-test"}
                )
                self.app_b = ApiApp.objects.create(
                    name="改单探针应用乙",
                    app_key="probeupdatekey0000bbb",
                    app_secret="probe-update-secret-bbbbbbbbbbb",
                    created_by=creator_b,
                )
                self.order_b = Order.objects.create(
                    game=self.game_b,
                    order_no=ORDER_NO_B,
                    project_text="改单探针项目乙",
                    content="客户原文乙",
                )
        finally:
            connection.set_tenant(self.tenant)

    # ── 发请求 ─────────────────────────────────────────────────────────
    def _patch(self, body, *, order_no=ORDER_NO_A, key="probe-update-key-0001", app=None,
               domain=None, token=None, ip=SOURCE_IP, omit_key=False, raw=None):
        """带 Bearer 令牌发一条 `PATCH`；结束后把连接钉回租户 A。

        ⚠️ 请求体一律 `json.dumps(body)`（**同一个字典两次调用得到同一串字节**）
        ⇒ 「同键同体」那条判据里的"同体"是**真的**同字节（`request_hash` 的口径）。
        """
        app = self.writer_app if app is None else app
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if not omit_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        data = raw if raw is not None else json.dumps(body)
        try:
            return Client(SERVER_NAME=domain).generic(
                "PATCH",
                DETAIL_TEMPLATE.format(order_no=order_no),
                data=data,
                content_type="application/json",
                REMOTE_ADDR=ip,
                **headers,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _get_detail(self, order_no, *, app=None, token=None, domain=None):
        """带 Bearer 令牌查详情（T7 起这条路径只认令牌）；结束后把连接钉回租户 A。"""
        app = self.reader_app if app is None else app
        domain = self.domain_a if domain is None else domain
        token = tokens.issue(app.app_key, app.app_secret) if token is None else token
        try:
            return Client(SERVER_NAME=domain).get(
                DETAIL_TEMPLATE.format(order_no=order_no),
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)

    # ── 断言小工具 ─────────────────────────────────────────────────────
    def assert_ok(self, response):
        """成功路径：HTTP 200 + `code=0` + 信封恰好三个键，返回 `data`。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body["data"]

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + **指定的**业务码（⛔ 不只看 msg：1000 与 1002 的文案可能撞）。"""
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], code, body)
        self.assertEqual(set(body), ENVELOPE_KEYS, body)
        return body

    def reload(self, order_no=ORDER_NO_A):
        """从**库里**重读那一行（⛔ 不看内存对象、⛔ 不信响应体）。"""
        return Order.objects.get(order_no=order_no)

    def update_logs(self, order_no=ORDER_NO_A):
        return OperationLog.objects.filter(order_no=order_no, action=UPDATE_ACTION)


class AllowedFieldTests(_UpdateFixture):
    """① 允许字段 ⇒ 200 + 库里真的变了 + 留痕那一行。"""

    def test_an_allowed_field_really_changes_the_row_in_the_database(self):
        """⭐ 判据打在**库里的那一行**上；响应自述不算数。

        ⚠️ 夹具前提：`writer_app` **没有任何权限类**（`permission_classes = []`）⇒
        本条同时钉住「改单不装节点门、也不做游戏范围过滤」（§13.10）。
        若哪天有人给 `PATCH` 装上权限类，这条会红——那正是要抓的。
        ⛔⭐ **改准（T10）**：原判据在这里还有两句夹具前提
        （`writer_app` 的节点数与游戏数都是 0）—— 那两列已删除（§13.1）⇒ 同一次去掉；
        **下面那句 200 + 库里真变的判据一字未改**（它本身就是"不装节点门"的行为判据）。
        """
        payload = self.assert_ok(
            self._patch({"project_text": "改单探针项目乙", "content": "客户原文乙"})
        )
        # 响应回的是**改完之后**的载荷（`order_payload` 那个出口）。
        self.assertEqual(payload["order_no"], ORDER_NO_A)
        self.assertEqual(payload["order"]["project_text"], "改单探针项目乙")
        self.assertEqual(payload["content"]["content"], "客户原文乙")
        # ⭐ 库里那一行。
        order = self.reload()
        self.assertEqual(order.project_text, "改单探针项目乙")
        self.assertEqual(order.content, "客户原文乙")

    def test_the_response_is_the_same_payload_the_read_endpoint_returns(self):
        """⭐ 「响应 = 改后的载荷（同一个出口）」的判据：改完之后的 `PATCH` 载荷与随后
        `GET` 详情拿到的载荷**逐字相同**（两条路都走 `order_payload`）。

        ⚠️ 用**另一个**应用（`reader_app`）发那条 `GET`。
        ⛔⭐ **改准（T10）**：原文写「写应用零游戏 ⇒ 它的 `GET` 会撞"零游戏范围"
        那一档」—— 游戏范围过滤已随 v1 面删除（§13.1）⇒ 那句话今天不成立。
        保留"用另一个应用"这个形状是为了不改动**这条用例本身**（它钉的是"两条路同一个载荷出口"，
        ⛔ 与用哪个应用无关）；判据一字未改。
        """
        patched = self.assert_ok(self._patch({"project_text": "改单探针项目丙"}))
        fetched = self.assert_ok(self._get_detail(ORDER_NO_A))
        self.assertEqual(patched, fetched)

    def test_the_written_change_leaves_exactly_one_audit_row(self):
        """⭐ 留痕走 `update_order_fields` **内部既有**那一处 `record(...)`（⛔ 不另建一套）。

        判据取那一行的**列值**（⛔ 不看 logger、⛔ 不只断"有行"）：
        动作码 `order.update`、`field_name` 是**真实列名**、前后值、以及操作人
        = 「开放平台应用 <应用名>」且 `actor_id` 为 `None`（⇒ 落成**系统行**）。
        """
        self.assert_ok(self._patch({"project_text": "改单探针项目丁"}))
        rows = list(self.update_logs())
        self.assertEqual(len(rows), 1, [r.summary for r in rows])
        row = rows[0]
        self.assertEqual(row.field_name, "project_text")
        self.assertEqual(row.from_value, "改单探针项目甲")
        self.assertEqual(row.to_value, "改单探针项目丁")
        self.assertEqual(row.actor, f"开放平台应用 {WRITER_NAME}")
        self.assertIsNone(row.actor_id)

    def test_a_same_value_write_writes_no_audit_row(self):
        """⚠️ 既有口径（R26）：**值没变的字段不写行** —— 改单这条路上也一样（⛔ 不改它）。

        判据两半：同值 `PATCH` ⇒ `code=0`（请求合法）而 **0 行留痕**；
        对照一条真改 ⇒ 1 行（否则"永远不写行"也会让前一半绿）。
        """
        self.assert_ok(self._patch({"project_text": "改单探针项目甲"}))
        self.assertEqual(self.update_logs().count(), 0)
        self.assert_ok(self._patch({"project_text": "改单探针项目戊"}, key="probe-update-key-0002"))
        self.assertEqual(self.update_logs().count(), 1)


class ReadOnlyFieldTests(_UpdateFixture):
    """② 只读字段传了 ⇒ **拒**（`1000`）且库里**一字未变**；可写字段 ⇒ 真的变（阳性对照）。"""

    def test_read_only_fields_are_refused_and_change_nothing_in_the_database(self):
        """⭐ `status` / 金额类 / `sales_user` / `order_no` / 审计列 —— 传了 ⇒ **`1000`**。

        ⛔⭐ **这一条是 T7 与任务书原文的一处分叉，理由必须读懂**：任务书原话是
        「只读字段…传了**不生效**（判据打在库里）」。若照**序列化器**的默认行为办，
        "不生效"= DRF 静默忽略只读字段 ⇒ 回 **200 / `code=0`**、库里没变、**没有任何信号**
        ⇒ 第三方发 `{"status": "COMPLETED"}` 会**以为改成功了**（那是**静默丢字段**，
        比报错更贵）。这与本线刚裁过的「录单表外键 ⇒ `1000`」是同一类缺陷
        ⇒ 对外端点一律 **fail-closed**：**拒**。
        ⚠️ **判据仍然打在库里的那一行上**（任务书那句判据的内核），只是期望从"200 没变"
        改成"`1000` 且没变"；⛔ 内网 `PATCH /api/business/orders/<单号>/` 的行为**一个字未改**
        （它继续忽略只读字段）。⚠️ 已把这个分叉报告给总指挥，结论以他的裁定为准。
        ⚠️ **阳性对照另发一次请求**（⛔ 不能塞进同一个请求：那个请求会被整条拒掉
        ⇒ 塞进去就变成"什么都没测"）。
        """
        before = self.reload()
        before_status = before.status
        before_order_no = before.order_no
        before_money = {
            name: getattr(before, name)
            for name in ("total_price", "received_price", "platform_price", "hall_price", "fee")
        }
        self.assertIsNone(before.sales_user_id)

        body = self.assert_business_failure(
            self._patch(
                {
                    # 只读：状态 / 金额 / 归属人 / 单号 / 审计列
                    "status": OrderStatus.COMPLETED,
                    "total_price": "1.00",
                    "hall_price": "2.00",
                    "sales_user": self.sales_user.pk,
                    "order_no": "20261008000000009999",
                    "updated_by": "想冒充谁就冒充谁",
                }
            ),
            VALIDATION,
        )
        # msg **点名**是哪些键（⛔ 不只是一句"参数错误"）。
        for name in ("status", "total_price", "hall_price", "sales_user", "order_no", "updated_by"):
            self.assertIn(name, body["msg"], body)
        self.assertIsNone(body["data"])

        after = self.reload()
        self.assertEqual(after.status, before_status)
        self.assertEqual(after.order_no, before_order_no)
        self.assertIsNone(after.sales_user_id)
        self.assertEqual(after.updated_by, "")
        for name, value in before_money.items():
            self.assertEqual(getattr(after, name), value, name)
        # ⛔ 一行留痕都不该有（被拒的请求什么都没写）。
        self.assertEqual(self.update_logs().count(), 0)

        # ⭐ 阳性对照：**只带可写字段**的同一把钥匙 ⇒ 200 + 库里真的变了
        #    （否则"什么都拒"也能让上面那一半绿）。
        payload = self.assert_ok(self._patch({"content": "客户原文己"}, key="probe-update-key-0009"))
        self.assertEqual(payload["content"]["content"], "客户原文己")
        self.assertEqual(self.reload().content, "客户原文己")

    def test_the_two_order_time_columns_are_refused_and_change_nothing(self):
        """⭐⭐ **本批最要紧的那一面**：对外的改单端点⛔ 不给改「发单 / 交单」两个事件时刻。

        批次 5 · 线 E 这两列先落地时**没进**
        `SERVER_WRITTEN_ORDER_FIELDS` ⇒ 它们成了可写字段 ⇒ 这个端点**共用**
        `OrderUpdateSerializer` ⇒ **外部系统能伪造我方订单的事件时刻**。
        本条就是那张「最终对外字段表」（下面那条）当时当场报出来的那件事的可执行判据。

        ## 判据
        - 传这两列 ⇒ **`1000` 拒** + 库里那两列**仍是 NULL** + **一行留痕都没有**
          （对外端点是 fail-closed：表外的键**拒**，⛔ 不是静默忽略——见模块 docstring）；
        - **阳性对照另发一次请求**：只带可写字段 ⇒ 200 + 库里真的变了
          （⛔ 不能塞进同一个请求：那个请求会被整条拒掉）。
        """
        before = self.reload()
        self.assertIsNone(before.published_at)
        self.assertIsNone(before.submitted_at)

        body = self.assert_business_failure(
            self._patch({
                "published_at": "2020-01-01T00:00:00Z",
                "submitted_at": "2020-01-02T00:00:00Z",
            }),
            VALIDATION,
        )
        # msg **点名**是哪些键（与上面那条只读用例同款：⛔ 不只是一句"参数错误"）。
        for name in ("published_at", "submitted_at"):
            self.assertIn(name, body["msg"], body)
        self.assertIsNone(body["data"])

        after = self.reload()
        self.assertIsNone(after.published_at)
        self.assertIsNone(after.submitted_at)
        self.assertEqual(self.update_logs().count(), 0)     # 被拒的请求什么都没写

        # ⭐ 阳性对照：同一个应用、只带可写字段 ⇒ 200 + 库里真的变了
        payload = self.assert_ok(
            self._patch({"content": "客户原文庚"}, key="probe-update-key-0010")
        )
        self.assertEqual(payload["content"]["content"], "客户原文庚")
        self.assertEqual(self.reload().content, "客户原文庚")

    def test_the_receipt_notfound_counter_is_refused_and_changes_nothing(self):
        """⭐ D-0359（2026-11-01 合并树全量 run226 抓出）：「上家查无」放弃计数列
        `receipt_sync_notfound_count` ⛔ 对外不可写。

        该列只由 `upstream_receipt_sync._bump_notfound`（同步运行内的原子 UPDATE）写；
        先落地时没进 `SERVER_WRITTEN_ORDER_FIELDS` ⇒ 两个写序列化器都是 `exclude = []`
        ⇒ 它成了可写字段 ⇒ 这个端点（共用 `OrderUpdateSerializer`）能被第三方改小放弃
        计数。与上面「两个事件时刻」那条同形：表外的键**拒**（fail-closed），⛔ 不是静默忽略。

        ## 判据
        - 传这一列 ⇒ **`1000` 拒**（msg 点名该键）+ 库里计数仍是 0 + **一行留痕都没有**；
        - **阳性对照另发一次请求**：只带可写字段 ⇒ 200 + 库里真的变了；
        - **内部写入照常**：`tests/test_upstream_receipt_sync.py::GiveupCounterTests`
          钉住同步运行自己写得进去（「不是谁都不写」的那一半）。
        """
        before = self.reload()
        self.assertEqual(before.receipt_sync_notfound_count, 0)

        body = self.assert_business_failure(
            self._patch({"receipt_sync_notfound_count": 99}),
            VALIDATION,
        )
        self.assertIn("receipt_sync_notfound_count", body["msg"], body)
        self.assertIsNone(body["data"])

        after = self.reload()
        self.assertEqual(after.receipt_sync_notfound_count, 0)
        self.assertEqual(self.update_logs().count(), 0)     # 被拒的请求什么都没写

        # ⭐ 阳性对照：同一个应用、只带可写字段 ⇒ 200 + 库里真的变了
        payload = self.assert_ok(
            self._patch({"content": "客户原文辛"}, key="probe-update-key-0011")
        )
        self.assertEqual(payload["content"]["content"], "客户原文辛")
        self.assertEqual(self.reload().content, "客户原文辛")

    def test_the_accepted_field_set_is_the_final_external_table(self):
        """⭐ **契约表**：改单端点接受的键集 = 下面这 **25** 个（写死字面量，⛔ 不 import 常量）。

        ⚠️ 这张表就是"最终对外字段表"的可执行形式（与外发文档 §4.6 同一次同步）；
        序列化器加/删一个可写字段、或有人把只读字段放进接受集 ⇒ 本条红
        （那时必须同一次改这张表 + 文档 + 用例的其它断言，⛔ 不许把等号放宽）。
        """
        from apps.openapi.api.order_write_views import _accepted_update_fields

        self.assertEqual(
            set(_accepted_update_fields()),
            {
                "category", "content", "current_info_1", "current_info_2",
                "customer_phone", "customer_qq", "customer_ww", "demand",
                "game", "game_account", "game_role_name", "game_sub_account",
                "is_urgent", "order_source", "order_type", "payment_method",
                "project", "project_text", "remark", "sales_agent",
                "server", "source", "source_order_no", "tag", "time_requirement",
            },
        )

    def test_the_writable_field_set_is_the_serializers(self):
        """⭐ 结构判据：「哪些字段能改」**只有一个出处** = `OrderUpdateSerializer`。

        ⛔ 本仓⛔ 不许在 `apps/openapi/**` 里另判一遍（那就是第二份判据、会与内网分叉）
        ⇒ 判据取**类对象来源**（视图模块里那个名字**就是** `apps.business.api.serializers`
        里那个类，⛔ 不是"一个长得像它的新序列化器"），加上只读那一侧的**逐字段判据**。
        ⚠️ 判别力：谁在 openapi 侧另写一个只读名单、或换成一个新序列化器，这条会红。
        """
        self.assertIs(order_write_views.OrderUpdateSerializer, OrderUpdateSerializer)
        serializer = OrderUpdateSerializer()
        # ⭐ 反面（本任务最要紧的那一半）：这些列**必须**仍是只读 —— 一旦放开，
        #   通用 PATCH 就成了第二个写入点（状态绕过状态机、金额绕过改价留痕……）。
        for name in (
            "status",
            "order_no",
            "sales_user",
            "payment_status",
            "completed_at",
            "created_by",
            "updated_by",
            "total_price",
            "received_price",
            "platform_price",
            "hall_price",
            "refund_fee",
            "fee",
        ):
            self.assertTrue(
                serializer.fields[name].read_only,
                f"{name} 不再是只读 ⇒ 对外通用 PATCH 成了它的第二个写入点",
            )
        # ⭐ 正面：代表性**可写**字段仍在（⛔ 不是把整张表都锁死了也算"只读判据通过"）。
        for name in ("project_text", "content", "game", "server", "tag", "remark", "is_urgent"):
            self.assertFalse(
                serializer.fields[name].read_only, f"{name} 意外变成只读（改单会静默丢字段）"
            )


class IdempotencyTests(_UpdateFixture):
    """③ §6.3 的四态 + 一条「失败之后同一个键可以重试」。"""

    def test_a_new_key_executes_and_stores_the_snapshot(self):
        key = "probe-update-key-1001"
        payload = self.assert_ok(self._patch({"project_text": "改单探针项目庚"}, key=key))
        row = OpenApiIdempotency.objects.get(app=self.writer_app, key=key)
        self.assertEqual(row.method, "PATCH")
        self.assertEqual(row.path, DETAIL_TEMPLATE.format(order_no=ORDER_NO_A))
        self.assertEqual(len(row.request_hash), 64)
        self.assertEqual(row.status_code, 200)
        self.assertEqual(set(row.response_snapshot), ENVELOPE_KEYS)
        self.assertEqual(
            row.response_snapshot["data"]["order"]["project_text"], "改单探针项目庚"
        )
        self.assertEqual(self.reload().project_text, "改单探针项目庚")
        self.assertEqual(payload["order"]["project_text"], "改单探针项目庚")

    def test_the_same_key_and_body_replays_and_runs_the_side_effect_only_once(self):
        """⭐ 同键同体 ⇒ **回放**（载荷逐字相同），且**副作用只发生一次**。

        ⚠️ **「只发生一次」怎么证**（⛔ 不靠"两次响应一样"——请求体一样时重跑一遍也会得到
        一样的载荷，那样这条判据**恒真**）：在两次调用**之间**用 ORM 把那一列改成第三个值。
        - 若第二次真的**回放** ⇒ 库里的值仍是那个第三个值（handler 没跑），
          而响应的载荷是**上一次的快照**（第二，不是第三）；
        - 若第二次**重跑**了 handler ⇒ 库里会被写回"第二"、留痕行变成 2 行 ⇒ 本用例红。
        """
        key = "probe-update-key-1002"
        body = {"project_text": "改单探针项目辛"}
        first = self.assert_ok(self._patch(body, key=key))
        # ⚠️ 中间这一笔**故意绕过端点**（直接 ORM），它就是"副作用有没有再发生"的观测点。
        Order.objects.filter(order_no=ORDER_NO_A).update(project_text="改单探针项目壬")
        self.assertEqual(self.update_logs().count(), 1)

        second = self.assert_ok(self._patch(body, key=key))

        self.assertEqual(first, second)
        self.assertEqual(second["order"]["project_text"], "改单探针项目辛")
        # ⭐ 库里仍是中间那一笔写的值 ⇒ 第二次**没有**再执行副作用。
        self.assertEqual(self.reload().project_text, "改单探针项目壬")
        self.assertEqual(self.update_logs().count(), 1, "回放不该再写一行留痕")

    def test_the_same_key_with_a_different_body_is_refused(self):
        key = "probe-update-key-1003"
        self.assert_ok(self._patch({"project_text": "改单探针项目癸"}, key=key))
        body = self.assert_business_failure(
            self._patch({"project_text": "改单探针项目子"}, key=key), VALIDATION
        )
        self.assertEqual(body["msg"], DIFFERENT_REQUEST_MSG)
        # 异体那一笔**一次都没执行** ⇒ 库里仍是第一次写的值。
        self.assertEqual(self.reload().project_text, "改单探针项目癸")
        self.assertEqual(self.update_logs().count(), 1)

    def test_a_missing_key_is_refused_and_writes_nothing(self):
        body = self.assert_business_failure(
            self._patch({"project_text": "改单探针项目丑"}, omit_key=True), VALIDATION
        )
        self.assertEqual(body["msg"], MISSING_KEY_MSG)
        self.assertEqual(self.reload().project_text, "改单探针项目甲")
        self.assertEqual(self.update_logs().count(), 0)

    def test_a_too_short_key_is_refused(self):
        """键长契约是 **8–128**（§6.3）：7 个字符也算缺失那一档（`1000`）。"""
        body = self.assert_business_failure(
            self._patch({"project_text": "改单探针项目寅"}, key="x" * (MIN_KEY - 1)),
            VALIDATION,
        )
        self.assertEqual(body["msg"], KEY_LENGTH_MSG)
        self.assertEqual(self.reload().project_text, "改单探针项目甲")

    def test_a_business_failure_rolls_the_placeholder_back_so_the_key_can_retry(self):
        """⭐ 可预期的业务失败 ⇒ 占位行**跟着回滚**（同一个键**下次可以重试**）。

        ⚠️ 这条钉的是幂等入口那一句 `transaction.set_rollback(True)`
        （E-0022 的原话：⛔ 不许用裸 `return` 退出补偿路径——那会把占位行提交掉，
        让这个键**永久**卡在"正在处理中"）。判据打在**台账的行数**上。
        业务失败的取材：改单会把 `project_text` 归一化后跑 `validate_required`
        ⇒ 把它改成一个**空串**（`project_text` 是必填）⇒ `1000` 且**一行都不写**。
        """
        key = "probe-update-key-1005"
        failed = self.assert_business_failure(self._patch({"project_text": ""}, key=key), VALIDATION)
        self.assertNotEqual(failed["msg"], "")
        self.assertFalse(
            OpenApiIdempotency.objects.filter(app=self.writer_app, key=key).exists(),
            "业务失败之后占位行必须跟着回滚，否则这个键永久卡住",
        )
        self.assertEqual(self.reload().project_text, "改单探针项目甲")
        # ⭐ 同一个键**重试一个合法请求**必须能成（这正是"回滚"要保住的东西）。
        payload = self.assert_ok(self._patch({"project_text": "改单探针项目卯"}, key=key))
        self.assertEqual(payload["order"]["project_text"], "改单探针项目卯")
        row = OpenApiIdempotency.objects.get(app=self.writer_app, key=key)
        # ⚠️ 判"这一行是真结果还是占位"只看 **`response_snapshot` 空不空**：
        #    `status_code` 分不出来（占位值就是 200，与成功同值，见 `PLACEHOLDER_STATUS_CODE`
        #    的注释）——写成 `assertNotEqual(row.status_code, PLACEHOLDER_STATUS_CODE)`
        #    是一条**恒真**的判据（本用例第一版就是那么写的，当场自证了这件事）。
        self.assertEqual(row.response_snapshot["code"], OK)
        self.assertEqual(row.response_snapshot["data"]["order_no"], ORDER_NO_A)


class CredentialAndTenantTests(_UpdateFixture):
    """④ 会话 ⇒ 401；跨租户 ⇒ 按契约拒 + 库里一字未变。"""

    def test_a_session_request_gets_401(self):
        """⭐ 对外写端点⛔ **不收会话 cookie**（与集合路径 T6 的口径一致）。

        ⚠️ 用**真实登录**的客户端（`make_logged_in_client` 走真登录端点）+ 一个合法的幂等键：
        这样 401 只可能来自"这条路径不认会话"，⛔ 不是"缺幂等键"或"没登录"。
        对照：同一个客户端打 `/api/auth/me` ⇒ 200（证明会话本身是好的）。
        """
        response = self.session_client.generic(
            "PATCH",
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_A),
            data=json.dumps({"project_text": "改单探针项目辰"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="probe-update-key-2001",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        body = json.loads(response.content)
        self.assertEqual(body["code"], UNAUTHENTICATED)
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertEqual(self.session_client.get("/api/auth/me").status_code, 200)
        # 库里一字未变（会话请求连认证都没过）。
        self.assertEqual(self.reload().project_text, "改单探针项目甲")
        self.assertEqual(self.update_logs().count(), 0)

    def test_an_unknown_token_gets_401(self):
        response = self._patch({"project_text": "改单探针项目巳"}, token="not-a-token")
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertEqual(json.loads(response.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)
        self.assertEqual(self.reload().project_text, "改单探针项目甲")

    def test_a_token_cannot_reach_the_order_of_tenant_b(self):
        """⭐ 跨租户：A 的令牌 + **B 的单号**（打 A 的域名）⇒ 与「不存在」**同形**（`1002`）。

        ⚠️ 判据用**订单号**（租户内唯一、两侧取值不同），⛔ 不用主键
        （两个 schema 的主键序列各自从 1 开始 ⇒ 用主键指认"另一个租户那一行"会恒真或恒假）。
        ⚠️ **两边的库都要查**：A 侧没有那一单（回 `1002`），**B 侧那一行一字未变**。
        """
        body = self.assert_business_failure(
            self._patch({"project_text": "改单探针项目午"}, order_no=ORDER_NO_B), BUSINESS
        )
        self.assertIsNone(body["data"])
        self.assertFalse(Order.objects.filter(order_no=ORDER_NO_B).exists())
        try:
            with tenant_context(self.tenant_b):
                self.assertEqual(
                    Order.objects.get(order_no=ORDER_NO_B).project_text, "改单探针项目乙"
                )
        finally:
            connection.set_tenant(self.tenant)

    def test_the_token_of_tenant_a_on_the_domain_of_tenant_b_gets_401(self):
        """A 的令牌打 **B 的域名** ⇒ 401（`ApiApp` 是每租户一份的表；B 里没有这个 appKey）。"""
        response = self._patch(
            {"project_text": "改单探针项目未"}, order_no=ORDER_NO_B, domain=DOMAIN_B
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertEqual(json.loads(response.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_the_token_of_tenant_b_can_update_its_own_order_on_its_own_domain(self):
        """正向对照：B 的令牌 + B 的域名 + B 的单 ⇒ **真的改得动**（否则上面那条 401 可能是空转）。"""
        token = tokens.issue(self.app_b.app_key, self.app_b.app_secret)
        try:
            response = Client(SERVER_NAME=DOMAIN_B).generic(
                "PATCH",
                DETAIL_TEMPLATE.format(order_no=ORDER_NO_B),
                data=json.dumps({"project_text": "改单探针项目申"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY="probe-update-key-3001",
                REMOTE_ADDR=SOURCE_IP,
            )
            self.assertEqual(response.status_code, 200, response.content[:300])
            self.assertEqual(json.loads(response.content)["code"], OK)
        finally:
            connection.set_tenant(self.tenant)
        try:
            with tenant_context(self.tenant_b):
                self.assertEqual(
                    Order.objects.get(order_no=ORDER_NO_B).project_text, "改单探针项目申"
                )
        finally:
            connection.set_tenant(self.tenant)


class MalformedInputTests(_UpdateFixture):
    """⑤ 畸形入参 ⇒ **一条都不许 500**（逐条写清期望码）。"""

    def test_a_too_long_order_no_in_the_path_is_a_business_not_found(self):
        """路径段超长（`Order.order_no` 是 20 位）⇒ 路由照样匹配、取不到单 ⇒ **`1002`**。

        ⚠️ ⛔ 不是 500：`order_no` 走的是**共享转换器**（它只挡 NUL 字节，⛔ 不挡长度）
        ⇒ 超长串进 `filter(order_no=…)` 由 PostgreSQL 正常比较、命中 0 行。
        """
        body = self.assert_business_failure(
            self._patch({"project_text": "改单探针项目酉"}, order_no="9" * 64), BUSINESS
        )
        self.assertIsNone(body["data"])

    def test_a_too_long_project_text_is_a_validation_failure(self):
        """`project_text` 有列宽上限 ⇒ 超一格是 `1000`（⛔ 不是 500，也⛔ 不是静默截断）。"""
        limit = Order._meta.get_field("project_text").max_length
        body = self.assert_business_failure(
            self._patch({"project_text": "甲" * (limit + 1)}), VALIDATION
        )
        self.assertNotEqual(body["msg"], "")
        self.assertEqual(self.reload().project_text, "改单探针项目甲")

    def test_a_too_long_tag_is_a_validation_failure(self):
        """`tag` 是 `max_length=128` 的字符列 ⇒ 超长 `1000`（同上，取**另一个**列当样本）。"""
        limit = Order._meta.get_field("tag").max_length
        self.assert_business_failure(self._patch({"tag": "x" * (limit + 1)}), VALIDATION)
        self.assertEqual(self.reload().tag, "")

    def test_a_game_that_does_not_exist_in_this_tenant_is_a_validation_failure(self):
        """`game` 是 FK：本租户里不存在的 pk ⇒ `1000`（⛔ 不是 500、也⛔ 不是 1002）。

        ⚠️ 另一个租户的 game_id 在本 schema 里同样查不到 ⇒ 同一条判据覆盖它。
        """
        body = self.assert_business_failure(
            self._patch({"game": 999999}), VALIDATION
        )
        self.assertNotEqual(body["msg"], "")
        self.assertEqual(self.reload().game_id, self.game_a.pk)

    def test_an_illegal_choice_value_is_a_validation_failure(self):
        """`order_type` 是带 choices 的列 ⇒ 非法值 `1000`（⛔ 不是 500）。"""
        self.assert_business_failure(self._patch({"order_type": "NOT-A-REAL-TYPE"}), VALIDATION)
        # `order_type` 是 `blank=True` 且没有 `default` ⇒ 库里就是空串（⛔ 不是被写成了那个非法值）。
        self.assertEqual(self.reload().order_type, "")

    def test_a_non_object_body_is_a_validation_failure_not_a_500(self):
        """请求体是 JSON **数组** ⇒ `1000`（⛔ 不是 500）。

        ⚠️ 这正是 `AGENTS.md` §5-E 那条判据的形状（"**这类请求**还会不会 500"）：
        值来自请求，`request.data` 是 `list` 时任何"顺手 `.items()`"的实现都会 500。
        """
        body = self.assert_business_failure(
            self._patch(None, raw=json.dumps([1, 2, 3])), VALIDATION
        )
        self.assertNotEqual(body["msg"], "")
        self.assertEqual(self.reload().project_text, "改单探针项目甲")

    def test_malformed_json_is_a_transport_level_400_not_a_500(self):
        """畸形 JSON ⇒ **HTTP 400** + `code=400`（解析失败属**传输层**，本仓口径不伪装成 200）。"""
        response = self._patch(None, raw='{"project_text": ')
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertEqual(json.loads(response.content)["code"], 400)
        self.assertEqual(self.reload().project_text, "改单探针项目甲")

    def test_the_unknown_key_is_refused_and_writes_nothing(self):
        """表外键（本系统根本没有的键）⇒ **`1000` + msg 点名 + 一行不落库**。

        ⛔⭐ 与录单端点**同一条口径**（T6 修复轮 `265c7e9d`：表外键从"忽略"改判"拒"，
        理由是**静默丢字段**）⇒ 改单这条端点照同一条裁定办。
        ⚠️ 取材用对标文档里那两个**我们没有的**键（`hours` / `gamePassword`）：它们是
        "第三方照对标文档写"的真实形状，⛔ 不是随手编的名字。
        ⛔ **不许 500**。
        """
        response = self._patch({"hours": 3, "gamePassword": "合成密码-甲"})
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        body = self.assert_business_failure(response, VALIDATION)
        self.assertIn("hours", body["msg"])
        self.assertIn("gamePassword", body["msg"])
        self.assertEqual(self.reload().project_text, "改单探针项目甲")
        self.assertEqual(self.update_logs().count(), 0)

    def test_the_two_endpoints_use_the_same_sentence_for_unaccepted_fields(self):
        """⭐ 两个对外写端点对"表外键"必须用**同一句话**（对外只该有一种说法）。

        ⚠️ 为什么要有这条：那两句在代码里各留了一份**字面量**（`order_create_views`
        反过来 import 本模块的 `_write_context` ⇒ 直接共用常量会成环），
        ⇒ 靠这条判据把它们钉在一起（谁改一边就红）。
        ⚠️ 判据两侧都写成**字面量**（E-0024：钉值不钉常量）——直接比两个常量是自指断言。
        """
        from apps.openapi.api.order_create_views import UNKNOWN_FIELDS_MSG
        from apps.openapi.api.order_write_views import UNACCEPTED_FIELDS_MSG

        self.assertEqual(UNACCEPTED_FIELDS_MSG, "请求里有本 API 不接受的字段：{}")
        self.assertEqual(UNKNOWN_FIELDS_MSG, "请求里有本 API 不接受的字段：{}")
        self.assertEqual(UNACCEPTED_FIELDS_MSG, UNKNOWN_FIELDS_MSG)


class DetailEndpointBearerTests(_UpdateFixture):
    """⑥ 详情 `GET` 切 Bearer 之后的六条（含"旧签名头不再被接受"这条改准事实）。"""

    def test_a_valid_token_reads_the_detail(self):
        payload = self.assert_ok(self._get_detail(ORDER_NO_A))
        self.assertEqual(payload["order_no"], ORDER_NO_A)

    def test_a_session_request_gets_401(self):
        response = self.session_client.get(
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_A), REMOTE_ADDR=SOURCE_IP
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertEqual(json.loads(response.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_the_old_signature_headers_are_no_longer_accepted(self):
        """⭐ **改准事实的判据**：旧签名头打详情路径 ⇒ **401 + Bearer 那句**。

        ⚠️ 这条替换掉的是 T4/T6 期间"详情路径仍只认签名"那批用例的**取样点**
        （原来它们靠详情路径证明"签名协议仍在线"）。

        ⛔⭐ **改准（T10）**：本条 T7 时的形状是「**按签名协议当场算一份头**」
        （原文里那句"签名头当场算，⛔ 不把拼接逻辑抄一份进来"逐字保留在 git 历史里）；
        签名协议与它的纯函数模块已整体删除（设计文档 §13.1）⇒ 素材换成**直接把那三个头塞进去**
        （值随便给：这条路径**根本不读它们**）。
        ⚠️ **判据内核一字未改**：带上旧协议的头 ⇒ **401 + Bearer 那句**（⛔ 不是 200）。
        """
        order_no = ORDER_NO_A
        path = DETAIL_TEMPLATE.format(order_no=order_no)
        response = Client(SERVER_NAME=self.domain_a).get(
            path,
            HTTP_X_TT_APPKEY=self.reader_app.app_key,
            HTTP_X_TT_TIMESTAMP=str(int(time.time())),
            HTTP_X_TT_SIGN="0" * 64,
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])
        self.assertEqual(json.loads(response.content)["msg"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_a_token_of_another_tenant_gets_401_on_this_domain(self):
        response = self._get_detail(ORDER_NO_B, app=self.reader_app, domain=DOMAIN_B)
        # ⚠️ 这条用的是**本租户**的令牌打 **B 的域名**（B 里没有这个 appKey）⇒ 401。
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:300])

    def test_the_order_of_tenant_b_looks_exactly_like_a_missing_order(self):
        """跨租户 ⇒ `1002`（与「不存在」**同形**，⛔ 不回一个泄漏存在性的码）。"""
        body = self.assert_business_failure(self._get_detail(ORDER_NO_B), BUSINESS)
        self.assertIsNone(body["data"])

    def test_an_order_of_another_game_is_readable_and_writable(self):
        """⭐ **同一条路径两个方法今天**同一个口径**：另一款游戏的单**读得到、也改得动**。

        ⛔⭐ **改准（原判据逐字保留）**：本条原来叫
        `test_an_order_outside_the_game_range_is_not_visible_but_still_writable`，钉的是
        「⭐ **同一条路径两个方法的口径差**（有意的）：`GET`：那条单的游戏**不在**应用的
        游戏范围里 ⇒ `1002`；`PATCH`：**照样改得动**」—— 读侧那条游戏范围过滤已随 v1 面
        删除（§13.1 / §13.10）⇒ 判据**反过来钉新契约**：两个方法**一致**，游戏不再是
        可见性维度（`GET` 也查得到）。
        ⚠️ **它仍然有判别力**：把游戏范围过滤加回读侧 ⇒ 本条的 `GET` 那一腿**当场红**。
        """
        outside = Order.objects.create(
            game=self.other_game,
            order_no="20261008000000000777",
            project_text="另一款游戏的单",
        )
        fetched = self.assert_ok(self._get_detail(outside.order_no))
        self.assertEqual(fetched["order_no"], outside.order_no)
        payload = self.assert_ok(
            self._patch({"project_text": "另一款游戏也能改"}, order_no=outside.order_no)
        )
        self.assertEqual(payload["order"]["project_text"], "另一款游戏也能改")
        self.assertEqual(self.reload(outside.order_no).project_text, "另一款游戏也能改")

    def test_a_non_object_body_never_reaches_the_view(self):
        """⛔ 冗余但便宜的一条：`PATCH` 的畸形体在**这条路径**上也不许 500（与详情 `GET` 无关）。"""
        response = self._patch({"project_text": ["不是字符串"]})
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(json.loads(response.content)["code"], VALIDATION)
        self.assertEqual(self.reload().project_text, "改单探针项目甲")


class HiddenOrderPatchTests(_UpdateFixture):
    """⭐ 评审 Important 4：对外**改单**不得改到隐藏单（按单号 1002、库里的行不动）。"""

    def setUp(self):
        super().setUp()
        from tests.hall_shadow_helpers import make_mirror

        self.order.hall_card = make_mirror(
            game=self.game_a, bound_by=self.sales_user, no="SYN-SHADOW-UPD"
        )
        self.order.save(update_fields=["hall_card"])

    def test_patching_a_hidden_order_is_1002_and_changes_nothing(self):
        before = self.order.content
        self.assert_business_failure(
            self._patch({"content": "syn-对外改隐藏单"}, key="probe-update-key-shadow"),
            BUSINESS,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.content, before, "对外 PATCH 改到了隐藏单")
