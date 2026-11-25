"""`POST /api/audit/events/`：前端上报的读操作（spec §6.2 / §5.1 / §7.3；R18 尾斜杠）。

## 这条腿为什么存在（spec §7.3）

三个读动作里，`order.view_credentials` 由订单详情视图**服务端**写（不可伪造），
而「进入编辑订单页面」（`order.open_edit`）**只有浏览器知道**，所以只能由前端上报。
代价是**公开登记**的：本端点写下的记录**可伪造、可被无限写**（spec §6.2 的边界块）。
**不要在这里加去重 / 限流 / 清理**——那是业务方尚未裁定的另一件事（spec §12-7）。

## 判据纪律

- 期望值一律写字面值（E-0024）：`log_type` 写 `10`，`summary` 写对标原句，
  白名单用**字面集合**钉（`FRONTEND_REPORTABLE_READ_ACTIONS` 被改宽 ⇒ 本文件必须红）。
- 否定断言（「不写行」）前先给**阳性对照**：同一条用例先证明白名单路径**写得进**，
  否则空的 `OperationLog` 表能让 `assertEqual(count, 0)` 空洞通过。
- 伪造防线断言的是**落库的列**，不是「请求被怎么处理了」——中间层不算证据（E-0009 同族）。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, tenant_context

from rest_framework.test import APIRequestFactory, force_authenticate

from apps.audit.api import serializers as audit_serializers
from apps.audit.api.views import OperationLogEventView
from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

ENDPOINT = "/api/audit/events/"

#: `TenantTestCase` 的租户域名（`make_logged_in_client` 的默认值同源）——
#: 匿名用例必须打在这个 host 上，否则请求先被租户中间件 404 掉，测不到认证层。
TENANT_DOMAIN = "tenant.test.com"

#: 20 位订单号（`Order.order_no` 与 `OperationLog.order_no` 的列宽都是 20）。
ORDER_NO = "20260921000000000001"


class EventWhitelistPinTests(TenantTestCase):
    """白名单是**命名常量**且当前**恰好**是 `order.open_edit`（brief 明确要求可被测试钉住）。

    为什么这条单独钉：白名单一旦被改宽（例如顺手加上 `order.view_credentials`），
    下面「非白名单动作不写行」的用例会红——但那条用例的**语义**是「这条动作不许上报」，
    而这条钉的是「清单本身没变」。两条一起看才知道「是清单错了还是动作被特殊放行」。
    """

    def test_frontend_reportable_read_actions_is_exactly_order_open_edit(self):
        self.assertEqual(
            set(audit_serializers.FRONTEND_REPORTABLE_READ_ACTIONS), {"order.open_edit"}
        )


class EventRequestMixin:
    """发请求 / 拆信封的公共口——**不碰夹具**，两个基类的登录用户不同，各自 `setUp` 准备。"""

    def _report(self, **payload):
        return self.client.post(ENDPOINT, payload, content_type="application/json")

    def _body(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()


class EventApiTestCase(EventRequestMixin, TenantTestCase):
    """公共夹具：一个已登录用户 + 一个读操作上报口。"""

    def setUp(self):
        self.client = make_logged_in_client(username="event-reporter")
        self.user = User.objects.get(username="event-reporter")


class EventReportTests(EventApiTestCase):
    """白名单内的一条读到的一行（spec §6.2 的请求 / 响应契约）。"""

    def test_whitelisted_read_action_writes_exactly_one_row(self):
        self.assertEqual(OperationLog.objects.count(), 0)  # 阳性前置：起点确实是空的

        body = self._body(self._report(action="order.open_edit", order_no=ORDER_NO))

        self.assertEqual(body["code"], 0, body)
        self.assertIsNone(body["data"], body)
        self.assertEqual(OperationLog.objects.count(), 1)
        row = OperationLog.objects.get()
        # 字面值，不引常量（E-0024）：`log_type` / `target_kind` / `summary` 全部照 spec 写死。
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.action, "order.open_edit")
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, ORDER_NO)
        self.assertEqual(row.order_no, ORDER_NO)
        self.assertEqual(row.actor, "event-reporter")
        self.assertEqual(row.actor_id, self.user.pk)
        # `summary` 由 `record()` 从行内列渲染（spec §7.1），调用方不给；
        # 期望值抄 `actions.py` 里 `order.open_edit` 的模板原句，**前面加**操作人前缀
        # （⭐ 审计日志线 spec §3.3：人为操作的正文带 `【昵称】`；该夹具用户没有
        # `first_name` ⇒ 显示名就是登录账号 `event-reporter`）。
        self.assertEqual(row.summary, f"【event-reporter】进入编辑订单：{ORDER_NO}")

    def test_repeated_edit_reads_have_independent_three_minute_windows(self):
        payload = {"action": "order.open_edit", "order_no": ORDER_NO}
        self.assertEqual(self._body(self._report(**payload))["code"], 0)
        start = OperationLog.objects.get(order_no=ORDER_NO).created_at
        with patch("django.utils.timezone.now", return_value=start + timedelta(seconds=179)):
            self.assertEqual(self._body(self._report(**payload))["code"], 0)
            other_no = "20260921000000000002"
            self.assertEqual(self._body(self._report(**{**payload, "order_no": other_no}))["code"], 0)
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 1)
        self.assertEqual(OperationLog.objects.filter(order_no=other_no).count(), 1)
        with patch("django.utils.timezone.now", return_value=start + timedelta(minutes=3)):
            self.assertEqual(self._body(self._report(**payload))["code"], 0)
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 2)

    def test_payload_without_order_no_still_records_the_read(self):
        """`order_no` 是可选键（spec §6.2 的请求体）——缺它不是错误，仍写一行。"""
        body = self._body(self._report(action="order.open_edit"))

        self.assertEqual(body["code"], 0, body)
        row = OperationLog.objects.get()
        self.assertEqual(row.order_no, "")
        self.assertEqual(row.summary, "【event-reporter】进入编辑订单")


class EventRejectionTests(EventApiTestCase):
    """白名单外的动作一律拒，且**零写入**（不是「写了再回滚」——这条腿本来就没有事务）。"""

    def test_action_outside_whitelist_is_rejected_and_writes_nothing_more(self):
        # 阳性对照：同一个端点、同一个 client、白名单内是写得进的。
        self.assertEqual(
            self._body(self._report(action="order.open_edit", order_no=ORDER_NO))["code"], 0
        )
        self.assertEqual(OperationLog.objects.count(), 1)

        body = self._body(self._report(action="order.transition", order_no=ORDER_NO))

        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(OperationLog.objects.count(), 1)  # 计数没涨
        self.assertFalse(OperationLog.objects.filter(action="order.transition").exists())

    def test_server_side_read_action_is_rejected(self):
        """`order.view_credentials` 必须**留在服务端**（spec §7.3）。

        它由订单详情视图写（不可伪造）；这里若放行，客户端就能伪造「看过账号密码」这条记录。
        """
        # 阳性对照（同上）：证明这个端点此刻是写得进的。
        self.assertEqual(
            self._body(self._report(action="order.open_edit", order_no=ORDER_NO))["code"], 0
        )
        self.assertEqual(OperationLog.objects.count(), 1)

        body = self._body(self._report(action="order.view_credentials", order_no=ORDER_NO))

        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(OperationLog.objects.count(), 1)
        self.assertFalse(
            OperationLog.objects.filter(action="order.view_credentials").exists()
        )


class EventForgeryTests(EventApiTestCase):
    """`actor` / `actor_id` / 租户键一律取认证态，请求体里的这些键**被忽略**（spec §6.2）。"""

    def test_forged_actor_keys_in_payload_are_ignored(self):
        body = self._body(
            self._report(
                action="order.open_edit",
                order_no=ORDER_NO,
                actor="伪造的操作人",
                actor_id=999999,
                # 租户 / schema 键：序列化器里根本没有这些入口，多传即被丢弃。
                tenant="audit_event_iso_b",
                schema_name="audit_event_iso_b",
                tenant_schema="audit_event_iso_b",
            )
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.count(), 1)
        row = OperationLog.objects.get()
        self.assertEqual(row.actor, "event-reporter")  # 认证态的用户名，不是伪造值
        self.assertEqual(row.actor_id, self.user.pk)  # 认证态的主键，不是 999999


class EventAdmissionTests(EventApiTestCase):
    def test_anonymous_request_is_rejected_with_401_and_writes_nothing(self):
        anonymous = Client(SERVER_NAME=TENANT_DOMAIN)
        response = anonymous.post(
            ENDPOINT,
            {"action": "order.open_edit", "order_no": ORDER_NO},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_authenticated_user_needs_no_rbac_node(self):
        """准入门槛就是「登录」：本端点记录的是**用户自己做的读操作**，不是管理员的查询。

        与 `GET /api/audit/logs/`（要 `system.logs` 节点）刻意不同——普通客服进入订单编辑页
        也必须能上报，否则这条腿在前端会恒空。夹具用户**没有**任何 RBAC 节点。
        """
        self.assertEqual(self.user.groups.count(), 0)
        self.assertEqual(
            self._body(self._report(action="order.open_edit", order_no=ORDER_NO))["code"], 0
        )
        self.assertEqual(OperationLog.objects.count(), 1)


class EventCrossTenantTests(EventRequestMixin, TenantTestCase):
    """A 的登录态上报 **B 的订单号** ⇒ 行写在 **A 的 schema**（AGENTS.md §4 的硬性要求）。

    判据用**订单号**（租户内唯一、且本例两租户取值不同），**不用主键**：两个 schema 的
    主键序列各自从 1 开始，`filter(id=...)` 会命中「另一个租户自己的那一行」（P3a 的教训）。
    """

    B_ORDER_NO = "20260921000000000077"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="audit_event_iso_b",
            name="读操作上报租户 B",
            domain="audit-event-iso-b.test.local",
        )

    def setUp(self):
        # 挡住上一条用例把连接留在别的 schema 上的残留（跨域名请求不会自动还原）。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(username="event-iso-a")

    def test_reported_order_of_another_tenant_lands_in_the_callers_schema(self):
        with tenant_context(self.other_tenant):
            game = Game.objects.create(name="B 租户的游戏")
            Order.objects.create(game=game, order_no=self.B_ORDER_NO)
            # 阳性对照：这个订单号**确实只存在于 B**，否则「A 里能查到」证明不了任何事。
            self.assertTrue(Order.objects.filter(order_no=self.B_ORDER_NO).exists())

        body = self._body(
            self._report(
                action="order.open_edit",
                order_no=self.B_ORDER_NO,
                tenant=self.other_tenant.schema_name,
                schema_name=self.other_tenant.schema_name,
            )
        )
        self.assertEqual(body["code"], 0, body)

        # A 的 schema：恰好一行，操作人是 A 的登录用户
        row = OperationLog.objects.get(order_no=self.B_ORDER_NO)
        self.assertEqual(row.actor, "event-iso-a")

        # B 的 schema：**一行都没有**——请求体里点名 B 也搬不动写入位置
        with tenant_context(self.other_tenant):
            # 阳性对照：B 的日志表**可查且写得进**，否则下面的 0 可能只是「查错了 schema」的假象。
            OperationLog.objects.create(
                log_type=10,
                action="order.open_edit",
                actor="B 的客服",
                order_no="20260921000000000099",
                summary="B 租户自己的查看记录",
            )
            self.assertTrue(
                OperationLog.objects.filter(summary="B 租户自己的查看记录").exists()
            )
            self.assertEqual(
                OperationLog.objects.filter(order_no=self.B_ORDER_NO).count(), 0
            )


class EventMalformedInputTests(EventApiTestCase):
    """畸形入参一律**业务码 1000**、不 500、不写行（AGENTS.md §5 的 E-0010 条）。"""

    def test_over_long_order_no_is_rejected_without_500_and_writes_nothing(self):
        response = self._report(action="order.open_edit", order_no="0" * 21)

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_missing_action_is_rejected_without_500(self):
        response = self._report(order_no=ORDER_NO)

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_unknown_action_is_rejected_without_500(self):
        response = self._report(action="totally.unknown", order_no=ORDER_NO)

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_non_string_shapes_are_rejected_without_500(self):
        """整型 / 列表形状的 `action` 同样走 1000——「这类请求」的判据不看字段类型（E-0010）。"""
        cases = (
            {"action": 123, "order_no": ORDER_NO},
            {"action": ["order.open_edit"], "order_no": ORDER_NO},
            {"action": "order.open_edit", "order_no": ["x"]},
            {"action": "order.open_edit", "order_no": {"a": 1}},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                response = self._report(**payload)
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(OperationLog.objects.count(), 0)


class ConcurrentEditEventTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("migrate_schemas", schema_name="public", interactive=False, verbosity=0)
        cls.tenant = get_tenant_model()(schema_name="audit_read_concurrent")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model().objects.create(
            tenant=cls.tenant, domain=TENANT_DOMAIN,
        )
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_live_connections_report_the_same_edit_read_once(self):
        user = User.objects.create_user(username="same-edit-reader")
        reporters = range(2)
        view = OperationLogEventView.as_view()
        payload = {"action": "order.open_edit", "order_no": ORDER_NO}
        ready = Barrier(len(reporters))

        def send(_reporter):
            try:
                with tenant_context(self.tenant):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        pid = cursor.fetchone()[0]
                    ready.wait(timeout=10)
                    request = APIRequestFactory().post(ENDPOINT, payload, format="json")
                    force_authenticate(request, user)
                    result = view(request)
                    return pid, result.status_code, result.data["code"]
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(reporters)) as pool:
            results = list(pool.map(send, reporters))
        self.assertEqual(len({pid for pid, _status, _code in results}), 2)
        self.assertEqual([(status, code) for _pid, status, code in results], [(200, 0), (200, 0)])
        self.assertEqual(OperationLog.objects.filter(action="order.open_edit", order_no=ORDER_NO).count(), 1)
