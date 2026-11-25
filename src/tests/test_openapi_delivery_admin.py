"""T9a：推送台账的两个**内部管理**端点（设计文档 §7.4 的「页面可见、可手工重投」+ §8）。

## 这个文件补的是什么缺口

T8 交付了主动推送（outbox + 常驻投递），但**没有任何读接口** ⇒ 租户在页面上看不到
"推送到底成没成"。T9a 补了两条端点：

| 方法 | 路径 | 是什么 |
|---|---|---|
| `GET` | `/api/open/apps/<pk>/deliveries/` | 台账列表（最近在前、分页、可按 `status` 筛） |
| `POST` | `/api/open/apps/<pk>/deliveries/<delivery_pk>/retry/` | 手工重投一条**投递失败**的行 |

两条都走**内部应用管理那一套**：会话认证 + 现成的节点门 `system.pinduoduo_settings`
（⛔ 不新增节点）+ `ApiAppAdminView._get_app` 的取数路径（⛔ 本任务不重写"取那个应用 +
404 / 1002"）。**⛔ 它们不是对外 Bearer 端点**。

## 本文件证什么（逐条都能失败）

1. **载荷形状**：列表每一行的键集是**字面量恰好相等**（不是 `assertIn`）；
   ⛔ **没有 `payload`**（那是待投递的报文原文，含订单业务数据）、⛔ **没有任何凭据**
   ——逐条递归扫键 **+** 全响应文本扫那两个合成凭据与报文标记（E-0065：只在顶层看一眼
   是假保证）。
2. **筛选 / 分页 / 排序**：`status` 真的筛得掉；`page_size` 真的生效、默认值就是内部页面端
   的口径（20）；顺序按 `created_at` 倒序、同 `created_at` 再按 `id` 倒序
   （用**显式写入**的时刻造这个局面，⛔ 不指望插入速度）。
3. ⭐ **重投的判据打在「库里的那一行」上**（E-0009：不打断言中间层）：
   `failed` ⇒ 那一行真的变 `pending` / `attempts=0` / `next_attempt_at=None`，
   而 `reason` / `response_status` / `processed_at` **刻意保留**（对照上一次失败）；
   `done` / `pending` ⇒ `1000` **且那一行逐列一字未变**（`done` 重投 = 对方收到两次）。
4. **越权面**：拿 A 应用的 pk 去重投**同租户里 B 应用**的那条行 ⇒ 拒（子资源不是装饰）；
   跨租户（A 的会话打 B 的应用 pk）⇒ 拒，且 B 那一行逐列一字未变 —— ⚠️ 判据**不按主键**
   指认对方那一行（`AGENTS.md` §5-G：两个 schema 的主键序列各自从 1 开始，按 pk 断言
   恒真或恒假），而是**换库对拍**（同一句判据在 A / B 两个 schema 上读数相反）。
5. **节点门**：匿名 ⇒ `401`、登录但**没有**该节点 ⇒ `403`（两条端点各测一遍）。
6. **畸形入参**：`pk` 超长 / 非数字、`delivery_pk` 超长 / 非数字、`status` 非法、
   `page` 非法、`page_size` 非法 —— **逐条写出期望码**，一条都不许 500
   （两条路由都带路径转换器 ⇒ 它们被 `test_no_route_500` 的显式路由腿跳过，
   所以这里用 **HTTP 级**断言补那张网的盲区；`pk` 那一档另外在
   `tests/test_openapi_app_api.py` 的 `CONVERTER_ROUTES` 里也挂了一份）。
7. ⭐ **留痕**：重投产生**恰好一条** `OperationLog`，逐列断言它的形状，
   并照 `AppSecretNeverLoggedTests` 的手法（数据库侧逐列 `CAST(col AS text) = 值`）
   证明那一行**不含任何凭据类字段**、也不含报文原文与诊断文本。

## 夹具纪律

- 应用名 / 凭据 / 单号 / 报文内容一律**合成值**（`AGENTS.md` §5-C），
  ⛔ 与任何真实租户、任何真实第三方无关。
- `created_at` 是 `auto_now_add`（`create()` 时**总是**被覆盖）⇒ 要造确定的顺序局面必须
  **建完再 `.update(created_at=…)`**（⛔ 不靠"插得快慢"这种不可复现的东西）。
"""
import json
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.openapi.models import ApiApp, OpenApiDelivery
from apps.platform.provisioning import provision_tenant
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 应用管理的节点门（复用现成的 `system.pinduoduo_settings`；⛔ 本任务不新增节点）。
APP_MANAGE_NODE = "system.pinduoduo_settings"

#: 两条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
DELIVERIES_TEMPLATE = "/api/open/apps/{pk}/deliveries/"
RETRY_TEMPLATE = "/api/open/apps/{pk}/deliveries/{delivery_pk}/retry/"

#: 业务码 / HTTP 码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
OK = 0
VALIDATION = 1000
UNAUTHENTICATED = 401
FORBIDDEN = 403
NOT_FOUND = 404

#: ⭐ 列表 item 的键集——**逐条字面量**（⛔ 不从 `delivery_row_payload` 派生：
#: 派生是自指恒真，装配函数被改坏时它照样绿，E-0024 / E-0065）。
#: ⚠️ 按 `sorted()` 的口径写，断言用等号（**恰好**，⛔ 不是 `assertIn`）。
ITEM_KEYS = (
    "attempts",
    "created_at",
    "event",
    "event_text",
    "id",
    "next_attempt_at",
    "order_no",
    "processed_at",
    "reason",
    "response_status",
    "source_order_no",
    "status",
    "status_text",
)

#: ⛔ **绝不许**出现在载荷里的键（递归扫；判据见模块 docstring 第 1 条）。
FORBIDDEN_ITEM_KEYS = frozenset(
    {"payload", "app_secret", "app_key", "secret", "sign", "signature", "token"}
)

#: 合成夹具值（⛔ 不是任何真实租户 / 真实第三方的数据）。
APP_NAME = "测试台账应用甲"
OTHER_APP_NAME = "测试台账应用乙"
APP_KEY = "t9adeliverykey0001"
APP_SECRET = "t9a-delivery-secret-000000000000001"
OTHER_APP_KEY = "t9adeliverykey0002"
OTHER_APP_SECRET = "t9a-delivery-secret-000000000000002"
ORDER_NO = "20261008000000000091"
OTHER_ORDER_NO = "20261008000000000092"

#: 报文原文里的合成标记：它只该待在 `payload` 里 —— 页面那一侧一个字都不该看到它。
PAYLOAD_MARKER = "台账夹具报文标记甲"
#: 诊断文本（合成）：列表里要看到它（截断后），而留痕里一个字都不许有。
REASON_TEXT = "对方返回 503，稍后重试"

#: 截断口径的**字面值**（生产常量在 `apps/openapi/serializers.py`，这里刻意写死：
#: 钉值不钉常量，E-0024 —— 常量被改坏时本文件必须红）。
REASON_DISPLAY_CHARS = 200
REASON_ELLIPSIS = "…"

#: 内部页面端的分页口径（`apps/business/api/base.py::EnvelopePagination` 的既有取值）。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 300

#: 不明参数 / 越界页的期望码（见 `DeliveryMalformedInputTests` 里逐条的说明）。
PAGE_OUT_OF_RANGE_CODE = 1002

#: 跨租户那一组用的 schema / 域名 / 显式主键。
SCHEMA_B = "openapi_delivery_admin_b"
DOMAIN_B = "openapi-delivery-admin-b.test.local"
#: ⚠️ **显式**给主键（⛔ 不靠自增）：两个 schema 的序列各自独立，靠巧合会让"哪一行属于
#: 哪个租户"说不清（先例：`tests/test_openapi_isolation.py` 的同一段注释）。
APP_PK_A = 93000001
APP_PK_B = 93000002


def _all_keys(node, into=None):
    """递归收集载荷里的**全部 dict 键**（判"报文 / 凭据不在载荷里"必须递归，⛔ 不是只看顶层）。"""
    into = set() if into is None else into
    if isinstance(node, dict):
        for key, value in node.items():
            into.add(key)
            _all_keys(value, into)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, into)
    return into


def _row_snapshot(row):
    """一条台账行的**逐列快照**（用来断言"一字未变"：⛔ 不挑列）。"""
    return {
        field.name: getattr(row, field.attname)
        for field in row._meta.concrete_fields
    }


class _DeliveryAdminFixture(TenantTestCase):
    """两条端点的共用夹具：一个持节点的管理员 + 造物 / 发请求工具。"""

    def setUp(self):
        super().setUp()
        seed_catalog()
        self.domain_a = self.domain.domain
        self.admin = make_logged_in_client(
            username="t9a-delivery-admin", groups=("客服",), nodes=(APP_MANAGE_NODE,)
        )
        self.nobody = make_logged_in_client(username="t9a-delivery-nobody")
        self.anonymous = Client(SERVER_NAME=self.domain_a)
        self.creator = User.objects.get(username="t9a-delivery-admin")

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_app(self, *, name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET):
        return ApiApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            created_by=self.creator,
        )

    def make_delivery(
        self,
        app,
        *,
        event=OpenApiDelivery.Event.ORDER_STATUS_CHANGED,
        order_no=ORDER_NO,
        source_order_no="TT-T9A-0001",
        payload=None,
        status=OpenApiDelivery.Status.FAILED,
        attempts=3,
        reason=REASON_TEXT,
        response_status=503,
        next_attempt_at=None,
        processed_at=None,
    ):
        """直接经 ORM 造一行台账（⛔ 不走 outbox：本文件测的是**读 / 重投**那一半）。

        ⚠️ `created_at` 是 `auto_now_add` ⇒ 想给它一个确定的时刻必须建完再 `.update(...)`
        （`make_delivery(..., created_at=…)` 那种写法会被静默忽略）。
        """
        return OpenApiDelivery.objects.create(
            app=app,
            event=event,
            order_no=order_no,
            source_order_no=source_order_no,
            payload={} if payload is None else payload,
            status=status,
            attempts=attempts,
            reason=reason,
            response_status=response_status,
            next_attempt_at=next_attempt_at,
            processed_at=processed_at,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def get(self, url, *, client=None, data=None):
        return (client or self.admin).get(url, data or {})

    def post_json(self, url, payload=None, *, client=None):
        return (client or self.admin).post(
            url, data=json.dumps({} if payload is None else payload),
            content_type="application/json",
        )

    def body(self, response):
        return json.loads(response.content)

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_business_failure(self, response, code, *, msg_fragment=None):
        """业务失败：**HTTP 200** + 指定业务码（状态码只表达传输 / 认证层）。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        if msg_fragment is not None:
            self.assertIn(msg_fragment, payload["msg"], payload)
        return payload

    def rows_for(self, action):
        return OperationLog.objects.filter(action=action)


class DeliveryListShapeTests(_DeliveryAdminFixture):
    """判据 1 / 2：列表的**载荷形状**、筛选、分页、排序。"""

    def test_the_item_keys_are_exactly_the_contract_ones(self):
        """⭐ 键集**字面量恰好相等**（⛔ 不是 `assertIn`：多回一个键就是契约变更）。"""
        app = self.make_app()
        self.make_delivery(app)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(sorted(data["items"][0]), list(ITEM_KEYS))
        # `data` 那一层是**内部页面端**的分页形状（items + total + page + page_size）。
        self.assertEqual(
            sorted(data), ["items", "page", "page_size", "total"]
        )
        self.assertEqual(data["total"], 1)

    def test_the_list_carries_neither_the_payload_nor_any_credential(self):
        """⭐ 逐条断言：**没有 `payload`**、**没有任何凭据**（递归扫键 + 全文本扫值）。

        两层一起才够：只看键会漏掉"某个键的**值**里塞了报文"（例如把一个 dict 原样塞进
        `reason`），只看值会漏掉"多回了一个 `payload` 键但内容是空的"。
        """
        app = self.make_app()
        self.make_delivery(app, payload={"note": PAYLOAD_MARKER, "status": "代练中"})
        self.make_delivery(
            app, order_no=OTHER_ORDER_NO, status=OpenApiDelivery.Status.PENDING
        )
        response = self.get(DELIVERIES_TEMPLATE.format(pk=app.pk))
        data = self.assert_ok(response)
        self.assertEqual(len(data["items"]), 2)
        for item in data["items"]:
            with self.subTest(item=item["id"]):
                self.assertEqual(_all_keys(item) & FORBIDDEN_ITEM_KEYS, set())
                self.assertNotIn("payload", item)
        text = response.content.decode()
        self.assertNotIn(PAYLOAD_MARKER, text, "报文原文漏进了列表响应")
        self.assertNotIn(APP_SECRET, text, "应用密钥漏进了列表响应")
        self.assertNotIn(APP_KEY, text, "appKey 漏进了列表响应")

    def test_the_reason_is_truncated_on_the_page_but_kept_in_the_database(self):
        """⭐ `reason` 是诊断文本、**页面只展示** ⇒ 超长时截断 + 加标记；库里一个字不动。

        边界（恰好等于上限）**不截、也不加标记** —— 那条边界是这个函数的契约
        （`apps/openapi/serializers.py::delivery_reason_text` 的 docstring 逐字写了）。
        """
        app = self.make_app()
        boundary = self.make_delivery(
            app, order_no=ORDER_NO, reason="甲" * REASON_DISPLAY_CHARS
        )
        over = self.make_delivery(
            app,
            order_no=OTHER_ORDER_NO,
            reason="乙" * (REASON_DISPLAY_CHARS + 1),
        )
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        by_id = {item["id"]: item for item in data["items"]}
        self.assertEqual(
            by_id[boundary.pk]["reason"], "甲" * REASON_DISPLAY_CHARS
        )
        self.assertEqual(
            by_id[over.pk]["reason"],
            "乙" * REASON_DISPLAY_CHARS + REASON_ELLIPSIS,
        )
        # 库里仍是**原文**（截断只发生在出口，⛔ 没有回写）
        boundary.refresh_from_db()
        over.refresh_from_db()
        self.assertEqual(len(boundary.reason), REASON_DISPLAY_CHARS)
        self.assertEqual(len(over.reason), REASON_DISPLAY_CHARS + 1)
        self.assertNotIn(REASON_ELLIPSIS, over.reason)

    def test_the_order_is_newest_first_and_the_id_breaks_ties(self):
        """`created_at` 倒序；**同一时刻**再按 `id` 倒序（⛔ 不指望插入速度）。"""
        app = self.make_app()
        base = datetime(2026, 10, 7, 10, 0, 0, tzinfo=dt_timezone.utc)
        older = self.make_delivery(app, order_no=ORDER_NO)
        tie_first = self.make_delivery(app, order_no=OTHER_ORDER_NO)
        tie_second = self.make_delivery(app, order_no="20261008000000000093")
        newest = self.make_delivery(app, order_no="20261008000000000094")
        # ⚠️ 显式写时刻：`auto_now_add` 让 `create()` 时的取值不可控。
        OpenApiDelivery.objects.filter(pk=older.pk).update(created_at=base)
        OpenApiDelivery.objects.filter(pk__in=[tie_first.pk, tie_second.pk]).update(
            created_at=base + timedelta(minutes=1)
        )
        OpenApiDelivery.objects.filter(pk=newest.pk).update(
            created_at=base + timedelta(minutes=2)
        )
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        # 同 `created_at` 的两条按 id **倒序**（大的在前）
        self.assertGreater(tie_second.pk, tie_first.pk)
        self.assertEqual(
            [item["id"] for item in data["items"]],
            [newest.pk, tie_second.pk, tie_first.pk, older.pk],
        )

    def test_the_time_fields_use_the_iso_shape_and_null_stays_null(self):
        """四个时间点：有值时是 ISO 8601 带时区、**没值时是 `None`**（⛔ 不是空串）。"""
        app = self.make_app()
        pinned = datetime(2026, 10, 7, 10, 0, 0, tzinfo=dt_timezone.utc)
        row = self.make_delivery(app)
        OpenApiDelivery.objects.filter(pk=row.pk).update(created_at=pinned)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        item = data["items"][0]
        self.assertEqual(item["created_at"], "2026-10-07T10:00:00+00:00")
        self.assertIsNone(item["next_attempt_at"])
        self.assertIsNone(item["processed_at"])

    def test_the_status_filter_selects_only_that_bucket(self):
        app = self.make_app()
        pending = self.make_delivery(
            app, order_no=ORDER_NO, status=OpenApiDelivery.Status.PENDING
        )
        failed = self.make_delivery(
            app, order_no=OTHER_ORDER_NO, status=OpenApiDelivery.Status.FAILED
        )
        for status, expected in (
            ("pending", [pending.pk]),
            ("failed", [failed.pk]),
            ("done", []),
        ):
            with self.subTest(status=status):
                data = self.assert_ok(
                    self.get(
                        DELIVERIES_TEMPLATE.format(pk=app.pk), data={"status": status}
                    )
                )
                self.assertEqual([item["id"] for item in data["items"]], expected)
                # ⚠️ `total` 是**筛选后**的命中数（⛔ 不是全表数）：页面要靠它算总页数。
                self.assertEqual(data["total"], len(expected))
        # 对照：**不传** `status` ⇒ 两档都在（证明上面的"筛得掉"不是"本来就只剩一条"）
        everything = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        self.assertEqual(everything["total"], 2)

    def test_the_list_only_shows_that_applications_rows(self):
        """台账按 `app` 收窄（同租户里另一个应用的行不许混进来）。"""
        app = self.make_app()
        other = self.make_app(name=OTHER_APP_NAME, app_key=OTHER_APP_KEY,
                              app_secret=OTHER_APP_SECRET)
        mine = self.make_delivery(app)
        self.make_delivery(other, order_no=OTHER_ORDER_NO)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        self.assertEqual([item["id"] for item in data["items"]], [mine.pk])
        self.assertEqual(data["total"], 1)

    def test_page_size_is_honoured_and_defaults_to_the_internal_page_size(self):
        """`page_size` 生效（显式给 / 默认 20 / 超上限夹到 300）——内部页面端的既有口径。"""
        app = self.make_app()
        for index in range(3):
            self.make_delivery(app, order_no=f"2026100800000000010{index}")
        default = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        self.assertEqual(default["page_size"], DEFAULT_PAGE_SIZE)
        self.assertEqual(len(default["items"]), 3)
        self.assertEqual(default["page"], 1)

        windowed = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=app.pk), data={"page_size": 2})
        )
        self.assertEqual(windowed["page_size"], 2)
        self.assertEqual(len(windowed["items"]), 2)
        # `total` 是**命中总数**（⛔ 不是本页条数）：退到第 2 页时它不变。
        self.assertEqual(windowed["total"], 3)
        second = self.assert_ok(
            self.get(
                DELIVERIES_TEMPLATE.format(pk=app.pk),
                data={"page_size": 2, "page": 2},
            )
        )
        self.assertEqual(second["page"], 2)
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["total"], 3)

        clamped = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=app.pk), data={"page_size": 1000})
        )
        self.assertEqual(clamped["page_size"], MAX_PAGE_SIZE)

    def test_an_unknown_app_is_a_business_error(self):
        """取不到应用 ⇒ `1000`（⛔ 不回 404 —— 那是取数路径 `_get_app` 的既有口径）。"""
        self.assert_business_failure(
            self.get(DELIVERIES_TEMPLATE.format(pk=999999)),
            VALIDATION,
            msg_fragment="应用不存在",
        )


class DeliveryRetryTests(_DeliveryAdminFixture):
    """判据 3 / 4：重投的语义、只允许 `failed`、子资源归属。"""

    def _retry(self, app, delivery, *, client=None):
        return self.post_json(
            RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=delivery.pk), client=client
        )

    def test_a_failed_row_is_requeued_and_the_response_is_that_row(self):
        """⭐ 判据打在**库里的那一行**上（E-0009）：三个字段真的被重排了。"""
        app = self.make_app()
        row = self.make_delivery(
            app,
            status=OpenApiDelivery.Status.FAILED,
            attempts=4,
            reason=REASON_TEXT,
            response_status=503,
            next_attempt_at=datetime(2026, 10, 8, 12, 0, tzinfo=dt_timezone.utc),
            processed_at=datetime(2026, 10, 8, 11, 0, tzinfo=dt_timezone.utc),
        )
        data = self.assert_ok(self._retry(app, row))
        row.refresh_from_db()
        self.assertEqual(row.status, OpenApiDelivery.Status.PENDING)
        self.assertEqual(row.attempts, 0)
        self.assertIsNone(row.next_attempt_at)
        # ⚠️ **刻意保留**的对照信息（见端点 docstring）：上一次为什么失败还读得到。
        self.assertEqual(row.reason, REASON_TEXT)
        self.assertEqual(row.response_status, 503)
        self.assertEqual(
            row.processed_at, datetime(2026, 10, 8, 11, 0, tzinfo=dt_timezone.utc)
        )
        # 响应 = **重排后的那一行**，键集与列表 item 逐字相同（页面可就地刷新）
        self.assertEqual(sorted(data), list(ITEM_KEYS))
        self.assertEqual(data["id"], row.pk)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["status_text"], "待投递")
        self.assertEqual(data["attempts"], 0)
        self.assertIsNone(data["next_attempt_at"])
        self.assertEqual(data["reason"], REASON_TEXT)

    def test_a_done_row_is_refused_and_the_row_is_untouched(self):
        """⛔ `done` 重投 = **对方会收到两次** ⇒ `1000`，且那一行逐列一字未变。"""
        app = self.make_app()
        row = self.make_delivery(
            app,
            status=OpenApiDelivery.Status.DONE,
            attempts=1,
            reason="",
            response_status=200,
            processed_at=datetime(2026, 10, 8, 11, 0, tzinfo=dt_timezone.utc),
        )
        before = _row_snapshot(row)
        self.assert_business_failure(
            self._retry(app, row), VALIDATION, msg_fragment="只有投递失败的推送记录可以重投"
        )
        row.refresh_from_db()
        self.assertEqual(_row_snapshot(row), before)

    def test_a_pending_row_is_refused_and_the_row_is_untouched(self):
        """`pending` 已经在队列里 ⇒ `1000`（重排只会把退避表清零），那一行一字未变。"""
        app = self.make_app()
        row = self.make_delivery(
            app,
            status=OpenApiDelivery.Status.PENDING,
            attempts=2,
            reason="上一次失败：连接超时",
            response_status=None,
        )
        before = _row_snapshot(row)
        self.assert_business_failure(
            self._retry(app, row), VALIDATION, msg_fragment="只有投递失败的推送记录可以重投"
        )
        row.refresh_from_db()
        self.assertEqual(_row_snapshot(row), before)

    def test_a_delivery_of_another_application_is_refused(self):
        """子资源不是装饰：拿 A 应用的 pk 去重投 B 应用的那条行 ⇒ 拒，且 B 那一行未变。"""
        app = self.make_app()
        other = self.make_app(name=OTHER_APP_NAME, app_key=OTHER_APP_KEY,
                              app_secret=OTHER_APP_SECRET)
        foreign = self.make_delivery(other, order_no=OTHER_ORDER_NO)
        before = _row_snapshot(foreign)
        self.assert_business_failure(
            self._retry(app, foreign), VALIDATION, msg_fragment="推送记录不存在"
        )
        foreign.refresh_from_db()
        self.assertEqual(_row_snapshot(foreign), before)

    def test_an_unknown_delivery_is_a_business_error(self):
        app = self.make_app()
        self.assert_business_failure(
            self.post_json(
                RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=999999)
            ),
            VALIDATION,
            msg_fragment="推送记录不存在",
        )

    def test_the_retried_row_comes_back_in_the_list_as_pending(self):
        """整链判据（E-0176 同族）：**从端点真正返回的载荷**里拿 `id`，再用它拼重投 URL。

        ⛔ 不直接拿数据库对象拼 URL —— 那样就证明不了"页面拿得到重投需要的那两个参数"。
        """
        app = self.make_app()
        self.make_delivery(app, status=OpenApiDelivery.Status.FAILED)
        listed = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        item = listed["items"][0]
        self.assertEqual(item["status"], "failed")
        retried = self.assert_ok(
            self.post_json(
                RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=item["id"])
            )
        )
        self.assertEqual(retried["id"], item["id"])
        self.assertEqual(retried["status"], "pending")
        again = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=app.pk)))
        self.assertEqual(again["items"][0]["status"], "pending")


class DeliveryAdminAccessTests(_DeliveryAdminFixture):
    """判据 5：两条端点都走**会话认证 + 那个节点门**（⛔ 不是对外 Bearer）。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.row = self.make_delivery(self.app)
        self.list_url = DELIVERIES_TEMPLATE.format(pk=self.app.pk)
        self.retry_url = RETRY_TEMPLATE.format(
            pk=self.app.pk, delivery_pk=self.row.pk
        )

    def test_anonymous_requests_are_401_on_both_endpoints(self):
        for url, method in ((self.list_url, "get"), (self.retry_url, "post")):
            with self.subTest(url=url):
                response = getattr(self.anonymous, method)(url)
                self.assertEqual(
                    response.status_code, UNAUTHENTICATED, response.content[:300]
                )

    def test_a_logged_in_account_without_the_node_is_403_on_both_endpoints(self):
        for url, method in ((self.list_url, "get"), (self.retry_url, "post")):
            with self.subTest(url=url):
                response = getattr(self.nobody, method)(url)
                self.assertEqual(
                    response.status_code, FORBIDDEN, response.content[:300]
                )

    def test_the_node_gate_holder_can_read_and_replay(self):
        """正向对照（没有它，上面两条 401 / 403 可能是"这条路对谁都不通"）。"""
        self.assert_ok(self.get(self.list_url))
        self.assert_ok(self.post_json(self.retry_url))
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, OpenApiDelivery.Status.PENDING)


class DeliveryMalformedInputTests(_DeliveryAdminFixture):
    """判据 6：两条带转换器的路由 × 畸形入参 ⇒ **逐条写出期望码，一条都不许 500**。

    ⚠️ 这两条路由被 `tests/test_no_route_500.py` 的显式路由腿**跳过**（它按 Django 自己的
    事实跳过带路径转换器的路由）⇒ 它们的畸形入参判据在这里用 **HTTP 级**断言补
    （⛔ 不是"不管"；`pk` 那一档另外在 `tests/test_openapi_app_api.py` 的
    `CONVERTER_ROUTES` 里也挂了一份）。
    """

    #: `<int:…>` 只要求 `[0-9]+` ⇒ 这个天文数字**匹配路由**、进 `filter(pk=…)`。
    #: 实测它不 500：psycopg 把超 `int8` 的整数按 `numeric` 送 ⇒ PostgreSQL 比较后 0 行。
    HUGE = 10**25

    def test_a_huge_pk_is_a_business_error_on_both_endpoints(self):
        for url in (
            DELIVERIES_TEMPLATE.format(pk=self.HUGE),
            RETRY_TEMPLATE.format(pk=self.HUGE, delivery_pk=self.HUGE),
        ):
            with self.subTest(url=url):
                response = (
                    self.get(url) if "retry" not in url else self.post_json(url)
                )
                self.assertNotEqual(response.status_code, 500)
                self.assert_business_failure(response, VALIDATION)

    def test_a_non_numeric_pk_does_not_match_the_route(self):
        """非数字 pk ⇒ **404**（⛔ 不是 500、也不进视图）——同一条判据两个参数位各测一次。"""
        for url in (
            DELIVERIES_TEMPLATE.format(pk="abc"),
            RETRY_TEMPLATE.format(pk="abc", delivery_pk=1),
            # 只有 `delivery_pk` 畸形（`pk` 合法）⇒ 同样不匹配路由
            RETRY_TEMPLATE.format(pk=1, delivery_pk="abc"),
        ):
            with self.subTest(url=url):
                response = (
                    self.get(url) if "retry" not in url else self.post_json(url)
                )
                self.assertEqual(response.status_code, NOT_FOUND, response.content[:300])

    def test_a_huge_delivery_pk_is_a_business_error(self):
        """`pk` 合法而 `delivery_pk` 是天文数字 ⇒ 进视图、查不到那条行 ⇒ `1000`。"""
        app = self.make_app()
        response = self.post_json(
            RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=self.HUGE)
        )
        self.assert_business_failure(
            response, VALIDATION, msg_fragment="推送记录不存在"
        )

    def test_a_bad_status_filter_is_a_business_error(self):
        """`status` 非法 ⇒ `1000` + 一句带字段名的人话（⛔ 不静默忽略）。"""
        app = self.make_app()
        self.make_delivery(app)
        for value in ("BOGUS", "FAILED", "pending,done"):
            with self.subTest(status=value):
                response = self.get(
                    DELIVERIES_TEMPLATE.format(pk=app.pk), data={"status": value}
                )
                self.assert_business_failure(
                    response, VALIDATION, msg_fragment="状态筛选值不合法"
                )

    def test_an_empty_status_is_treated_as_not_provided(self):
        """⚠️ **空串 ≠ 非法值**：`?status=` 被当成"没传"⇒ 不筛（`code=0` + 全部行）。

        ⚠️ 这条**不是我方选的**，是 DRF 的框架事实（读源码得到，E-0003）：`Field.get_value`
        对 HTML 形状的输入有一支 `ret == '' and not self.required ⇒ empty`
        （`rest_framework/fields.py` 的 `get_value`，那个 `empty` 是"缺席"哨兵）⇒
        非必填字段上的空串在**全仓**都是这个语义（`PATCH {}` 那一族同理）。
        ⛔ 本端点**不特判**它：特判会让"空串"这一种输入在本端点与其余每一个可选字段上分叉。
        """
        app = self.make_app()
        self.make_delivery(app, order_no=ORDER_NO)
        self.make_delivery(
            app, order_no=OTHER_ORDER_NO, status=OpenApiDelivery.Status.PENDING
        )
        data = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=app.pk), data={"status": ""})
        )
        self.assertEqual(data["total"], 2)

    def test_a_bad_page_is_a_business_error_not_a_500(self):
        """`page` 非法 / 越界 ⇒ **HTTP 200 + `1002`**。

        ⚠️ 这条期望码**不是本端点自选的**：`page` 的校验属于分页器
        （`apps/business/api/base.py::EnvelopePagination`），DRF 对"页码不是整数 / 超出末页"
        抛 `NotFound`，本仓的信封处理器把它翻成 **HTTP 200 + `1002`（「记录不存在或已被删除」）**
        —— `/api/business/` 下每一个分页列表都是这个读数，本端点**刻意不特判**
        （特判会让同一种输入在不同列表上给出不同的码）。
        """
        app = self.make_app()
        self.make_delivery(app)
        for value in ("abc", "0", "999"):
            with self.subTest(page=value):
                response = self.get(
                    DELIVERIES_TEMPLATE.format(pk=app.pk), data={"page": value}
                )
                self.assertNotEqual(response.status_code, 500)
                self.assert_business_failure(response, PAGE_OUT_OF_RANGE_CODE)

    def test_a_bad_page_size_is_ignored_rather_than_an_error(self):
        """`page_size` 非法 ⇒ **回落到默认值**（分页器的既有行为），⛔ 不是 `1000`。

        ⚠️ 与 `page` 那一档**刻意不同**，而这个不同不是我方选的：DRF 的
        `get_page_size` 对非整数**吞掉异常、用默认值**，而 `get_page_number` 交给
        `Paginator.page` 抛 `InvalidPage`。⛔ 本端点不"顺手修正"它 —— 那会让本端点与
        `/api/business/` 下每一个分页列表的行为分叉。
        """
        app = self.make_app()
        for index in range(2):
            self.make_delivery(app, order_no=f"2026100800000000020{index}")
        for value in ("abc", "-1", "0"):
            with self.subTest(page_size=value):
                data = self.assert_ok(
                    self.get(
                        DELIVERIES_TEMPLATE.format(pk=app.pk),
                        data={"page_size": value},
                    )
                )
                self.assertEqual(data["page_size"], DEFAULT_PAGE_SIZE)
                self.assertEqual(len(data["items"]), 2)


class DeliveryCrossTenantTests(_DeliveryAdminFixture):
    """判据 4（跨租户）：A 的会话打 **B 的应用 pk** ⇒ 拒，且 B 那一行一字未变。

    ⚠️ 判据**不按主键**指认对方那一行（`AGENTS.md` §5-G：两个 schema 的主键序列各自独立
    ⇒ `filter(pk=…)` 在 A 里命中的可能是 A 自己的行，那种断言恒真或恒假）。
    这里的做法是**换库对拍**：同一句读法在 A / B 两个 schema 上读数相反，
    并且对 B 的那一行用**租户内唯一、两侧取值不同**的单号来指认。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建（schema 的 DDL 不能回滚在 TestCase 的事务里；
        #    先例 = `tests/test_openapi_isolation.py` 的同一段）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="台账隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：上一条用例的跨域名请求会把连接留在那个 schema 上。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.app_a = ApiApp.objects.create(
            pk=APP_PK_A, name="台账隔离租户 A 的应用", app_key=APP_KEY,
            app_secret=APP_SECRET, created_by=self.creator,
        )
        self.row_a = self.make_delivery(self.app_a, order_no=ORDER_NO)
        try:
            with tenant_context(self.tenant_b):
                seed_catalog()
                creator_b, _ = User.objects.get_or_create(
                    username="t9a-iso-b", defaults={"password": "unused-in-this-test"}
                )
                self.app_b = ApiApp.objects.create(
                    pk=APP_PK_B, name="台账隔离租户 B 的应用", app_key=OTHER_APP_KEY,
                    app_secret=OTHER_APP_SECRET, created_by=creator_b,
                )
                self.row_b = OpenApiDelivery.objects.create(
                    app=self.app_b,
                    event=OpenApiDelivery.Event.ORDER_STATUS_CHANGED,
                    order_no=OTHER_ORDER_NO,
                    payload={},
                    status=OpenApiDelivery.Status.FAILED,
                    attempts=3,
                    reason=REASON_TEXT,
                    response_status=503,
                )
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="t9a-iso-b-admin",
                    groups=("客服",),
                    nodes=(APP_MANAGE_NODE,),
                )
        finally:
            connection.set_tenant(self.tenant)
        # ── 构造物自证：那一行真的在 B、不在 A（否则下面的负断言是空转，E-0004）──
        self.assertEqual(self._count_here(OTHER_ORDER_NO), 0)
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._count_here(OTHER_ORDER_NO), 1)
            self.assertEqual(self._count_here(ORDER_NO), 0)
        finally:
            connection.set_tenant(self.tenant)

    def _count_here(self, order_no):
        return OpenApiDelivery.objects.filter(order_no=order_no).count()

    def test_the_order_no_criterion_flips_when_the_schema_changes(self):
        """⭐ 判别力自证：同一句判据**换库**后读数相反（恒真的判据做不到这件事）。"""
        self.assertEqual(self._count_here(ORDER_NO), 1)
        self.assertEqual(self._count_here(OTHER_ORDER_NO), 0)
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._count_here(OTHER_ORDER_NO), 1)
            self.assertEqual(self._count_here(ORDER_NO), 0)
        finally:
            connection.set_tenant(self.tenant)

    def test_a_session_cannot_retry_another_tenants_delivery(self):
        """A 的会话打 **B 的应用 pk** ⇒ `1000`（A 里没有这个应用），B 那一行一字未变。"""
        url = RETRY_TEMPLATE.format(pk=APP_PK_B, delivery_pk=self.row_b.pk)
        self.assert_business_failure(
            self.post_json(url), VALIDATION, msg_fragment="应用不存在"
        )
        try:
            connection.set_tenant(self.tenant_b)
            row_b = OpenApiDelivery.objects.get(order_no=OTHER_ORDER_NO)
            self.assertEqual(row_b.status, OpenApiDelivery.Status.FAILED)
            self.assertEqual(row_b.attempts, 3)
        finally:
            connection.set_tenant(self.tenant)

    def test_the_same_request_on_tenant_b_succeeds(self):
        """正向对照：**同一条 URL 在 B 的域名上** ⇒ `200` 且 B 那一行真的被重排。

        没有它，上面那条 `1000` 可能是"这条路对谁都不通"凑出来的（E-0004 同族）。
        ⚠️ 两个租户各自造了**同一个号**的应用 pk —— 这正是"按 pk 判租户"不成立的现场。
        """
        self.assertEqual(self.app_a.pk, APP_PK_A)
        self.assertEqual(self.app_b.pk, APP_PK_B)
        url = RETRY_TEMPLATE.format(pk=APP_PK_B, delivery_pk=self.row_b.pk)
        response = self.client_b.post(
            url, data=json.dumps({}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(json.loads(response.content)["code"], OK)
        try:
            connection.set_tenant(self.tenant_b)
            row_b = OpenApiDelivery.objects.get(order_no=OTHER_ORDER_NO)
            self.assertEqual(row_b.status, OpenApiDelivery.Status.PENDING)
            self.assertEqual(row_b.attempts, 0)
        finally:
            connection.set_tenant(self.tenant)
        # A 自己那一行**没被动过**（同一句判据在 A 的 schema 上读数不同）
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.status, OpenApiDelivery.Status.FAILED)
        self.assertEqual(self.row_a.attempts, 3)


class DeliveryRetryAuditTests(_DeliveryAdminFixture):
    """判据 7：重投**恰好一条**留痕，且那一行不含任何凭据类字段。

    ## 判据为什么打在「数据库行」上（⛔ 不是 `assertLogs` / 不是读模板常量）

    `AGENTS.md` §5-G：断言副作用要打在真实出口上 —— `assertLogs` 会自己把目标 logger 的
    级别抬到 INFO ⇒「生产一行都没写」的测试**全绿**（E-0009 / E-0024）。这里翻的是
    `OperationLog` 的**行**，并且让**数据库自己**逐列扫一遍（照
    `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests` 的手法，⛔ 不用 `LIKE`：
    动作码本身带 `_`，通配位转义写错会得到恒真的判据，E-0083 同族）。
    """

    def _rows_text(self, model):
        """一张表的**每一行**的**每一个字符串列**拼成一段文本（⛔ 不挑列）。"""
        chunks = []
        for row in model.objects.all():
            for field in row._meta.concrete_fields:
                value = getattr(row, field.attname, None)
                if value is not None:
                    chunks.append(str(value))
        return "\n".join(chunks)

    def _value_columns_hit(self, model, value):
        """让**数据库**逐列比一次 `CAST(col AS text) = 值`（⚠️ 精确相等，没有通配位可转义）。"""
        hits = []
        table = model._meta.db_table
        for field in model._meta.concrete_fields:
            count = model.objects.extra(
                where=[f'CAST("{table}"."{field.column}" AS text) = %s'], params=[value]
            ).count()
            if count:
                hits.append((field.name, count))
        return hits

    def test_one_row_is_written_and_it_carries_no_credential(self):
        app = self.make_app()
        row = self.make_delivery(app, payload={"note": PAYLOAD_MARKER})
        self.assert_ok(
            self.post_json(RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=row.pk))
        )
        rows = list(self.rows_for("system.open_api_delivery_retry"))
        self.assertEqual(len(rows), 1, [r.summary for r in rows])
        logged = rows[0]
        # ── 形状：逐条**字面值**（E-0024：不从 `ACTION_TEMPLATES` / `ACTION_LOG_TYPES` 派生）──
        self.assertEqual(logged.log_type, 40)
        self.assertEqual(logged.action, "system.open_api_delivery_retry")
        self.assertEqual(logged.target_kind, "api_app")
        self.assertEqual(logged.target_id, str(app.pk))
        self.assertEqual(logged.target_name, APP_NAME)
        self.assertEqual(logged.order_no, ORDER_NO)
        # `{to_value}` 装的是**事件中文名**，「事件：」这个标签由模板正文提供（R10）
        self.assertEqual(logged.to_value, "订单状态变化")
        self.assertEqual(logged.field_name, "")
        self.assertEqual(logged.from_value, "")
        self.assertEqual(
            logged.summary,
            # ⚠️ 前缀 `【显示名】` 是渲染器加的（模板里没有 `{actor_name}` ⇒ 人为行加前缀，
            #    spec §3.3）；显示名走 `apps/accounts/display.py::user_display_name`
            #    （这个夹具账号没有昵称 ⇒ 就是用户名）。
            f"【t9a-delivery-admin】已将开放平台应用 {APP_NAME} 的回调重新排队"
            f"（事件：订单状态变化，订单 {ORDER_NO}）",
        )
        # ── ⛔ 凭据类字段 / 报文原文 / 诊断文本：一列都不许有 ──
        for forbidden in (APP_SECRET, APP_KEY, PAYLOAD_MARKER, REASON_TEXT):
            with self.subTest(value=forbidden):
                self.assertEqual(
                    self._value_columns_hit(OperationLog, forbidden), [],
                    "留痕里有某一列出现了不该出现的值（数据库侧逐列 CAST … = 值 扫出来的）",
                )
                self.assertNotIn(forbidden, self._rows_text(OperationLog))

    def test_a_refused_retry_writes_nothing(self):
        """对照：被拒的重投（`done`）**一行留痕都不写** —— "该不写的地方真的没写"。"""
        app = self.make_app()
        row = self.make_delivery(app, status=OpenApiDelivery.Status.DONE)
        self.assert_business_failure(
            self.post_json(RETRY_TEMPLATE.format(pk=app.pk, delivery_pk=row.pk)),
            VALIDATION,
        )
        self.assertEqual(self.rows_for("system.open_api_delivery_retry").count(), 0)
