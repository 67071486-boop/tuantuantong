"""Task 13：`OperationLog` 的跨租户隔离回归（spec §10.5；`AGENTS.md` §4 硬性规则）。

`OperationLog` 建在每个租户自己的 schema 里（`apps.audit` 在 `TENANT_APPS`），
所以隔离在**构造上**成立——但「构造上成立」不等于「有回归网」：spec §10.5 点名要求
本文件，`AGENTS.md` §4 的硬规则是「任何新增查询都必须携带租户上下文，并补一条
『以租户 A 的凭据访问 B 的数据必须失败』的测试」。本文件就是那张网。

## 判据纪律（本任务的核心）

1. **不用主键指认「另一个租户的那一行」**：两个 schema 的主键序列**各自从 1 开始**
   ⇒ `filter(id=a_row_pk)` 在 B 里命中的是 **B 自己的行**，断言恒真/恒假
   （`AGENTS.md` §5 的硬规则；P3b 的 B1-1 第一版就是这么错的）。
   本文件的判据一律是**租户内唯一、且两租户取值不同**的业务值：
   `summary` 标记串、20 位 `order_no`、`actor` 用户名。
2. **每条否定断言之前先给阳性对照**：先证明「当前租户自己的那一行可见 / 写得进」，
   否则空表能让 `assertFalse(...exists())` 空洞通过
   （模板见 `src/tests/test_p2_isolation.py:89-99`）。
3. **不用 `count()` 当可见性判据**：数字取决于两个租户各有多少行，
   而不是「这一行可不可见」——隔离一旦破了而数字恰好没变，它就静默失去判别力。
4. 期望值写**字面值**（E-0024）；断言打在**数据库行 / 响应体**上（E-0009）。

## 本文件证什么 / 不证什么

- **数据层**（`OperationLogDataIsolationTests`）：两个方向（A→B 与 B→A）、
  同一 `order_no` 在两租户各自成立、以及**真实写入口** `apps/audit/log.py::record()`
  也只在当前租户落行。
- **HTTP 层**（`OperationLogHttpIsolationTests`）：`POST /api/audit/events/`（真实登录请求）
  写的行，经 `GET /api/audit/logs/` 各读一次——请求式 API 服务的行同样只在调用者租户里。
- **不证**：手工拼 SQL、或把 schema 固定在别处的查询路径（那些不走当前 schema 的 ORM，
  见 `tests/test_p3b_isolation.py` 文末的同款登记）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 租户与端点 ─────────────────────────────────────────────────────────
#: 租户 A = `TenantTestCase` 的 `test` schema，域名与既有隔离测试同口径。
DOMAIN_A = "tenant.test.com"
SCHEMA_B = "p13_iso_b"
DOMAIN_B = "p13-iso-b.test.local"

LOGS_ENDPOINT = "/api/audit/logs/"
EVENTS_ENDPOINT = "/api/audit/events/"

# ── 判据用的业务值（租户内唯一、且两租户取值不同；**不是主键**）──────────
#: `summary` 标记串：测试完全控制它，不受 `record()` 渲染影响。
MARKER_A = "仅 A 可见的操作日志"
MARKER_B = "仅 B 可见的操作日志"
#: 20 位订单号（与 `OperationLog.order_no` 列宽一致），A / B 取值不同。
NO_A = "20260921000000000131"
NO_B = "20260921000000000132"
#: 两个租户**共用**的订单号——订单号只有租户内唯一（与 P2 的 `Order.order_no` 同口径）。
SHARED_NO = "20260921000000000133"
#: 走 `record()` 的两个订单号。
REC_NO_A = "20260921000000000134"
REC_NO_B = "20260921000000000135"
#: 走 HTTP 上报的两个订单号。
HTTP_NO_A = "20260921000000000136"
HTTP_NO_B = "20260921000000000137"

ACTOR_A = "iso-p13-a"
ACTOR_B = "iso-p13-b"
HTTP_USER_A = "iso-p13-http-a"
HTTP_USER_B = "iso-p13-http-b"

#: `record()` 渲染出的 `order.open_edit` 文案**正文**（抄 `apps/audit/actions.py` 的模板原句，
#: 写字面值而不是引常量——E-0024）。
#: ⚠️ ⭐ 审计日志线（spec §3.3）之后，人为行的正文前面还有 `【显示名】` 前缀，而**显示名
#: 随行的操作人变**（本文件三处入口的操作人各不相同）⇒ 这里不能在模块级拼成一个常量；
#: 各调用点自己写全整句（含 `【】` 与前缀位置，⛔ 不退化成「只断言含昵称」）。
OPEN_EDIT_BODY = "进入编辑订单"


def visible_summaries():
    """当前租户上下文里**能查到**的全部 `summary`（本文件的可见性判据一律经此）。

    ⚠️ 判据是 `summary` 标记串，**不是主键**：两个 schema 的主键序列各自从 1 开始
    （`AGENTS.md` §5 的硬规则）。
    """
    return [row.summary for row in OperationLog.objects.all()]


def make_row(*, order_no, actor, summary):
    """在当前 schema 里造一条 `OperationLog`（模型层写入口）。

    ⚠️ 这是**模型层**的写入口；`record()` 那条真实写路径由
    `test_record_entry_point_is_tenant_scoped` 单独覆盖。
    """
    return OperationLog.objects.create(
        log_type=10,
        action="order.transition",
        actor=actor,
        order_no=order_no,
        summary=summary,
    )


class OperationLogDataIsolationTests(TenantTestCase):
    """数据层：一个租户写的日志行在另一个租户里查不到（两个方向都测）。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建：在用例内部建 schema 会与外层事务相冲
    （schema DDL 不能在 `TestCase` 的事务里回滚）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="操作日志隔离租户 B", domain=DOMAIN_B
        )

    # ── 方向一：A 写 → B 查不到 ─────────────────────────────────────
    def test_row_written_in_a_is_invisible_in_b_with_b_positive_control(self):
        with tenant_context(self.tenant_a):
            make_row(order_no=NO_A, actor=ACTOR_A, summary=MARKER_A)

        with tenant_context(self.tenant_b):
            # **阳性对照**：先证明 B 自己的表可达、写得进、查得到。
            # 否则下面的否定断言可能只是因为「这张表什么都查不到」而空洞通过。
            make_row(order_no=NO_B, actor=ACTOR_B, summary=MARKER_B)
            summaries = visible_summaries()
            self.assertIn(MARKER_B, summaries)
            self.assertTrue(OperationLog.objects.filter(order_no=NO_B).exists())
            # 再证明看不到 A 的——判据是 A 独有的标记串与订单号（**不是主键**）
            self.assertNotIn(MARKER_A, summaries)
            self.assertFalse(OperationLog.objects.filter(order_no=NO_A).exists())

        with tenant_context(self.tenant_a):
            # 回访：A 自己的行还在（证明上面的「查不到」不是「写丢了」凑出来的）
            self.assertIn(MARKER_A, visible_summaries())
            self.assertTrue(OperationLog.objects.filter(order_no=NO_A).exists())

    # ── 方向二：B 写 → A 查不到（反向）──────────────────────────────
    def test_row_written_in_b_is_invisible_in_a_with_a_positive_control(self):
        """只测一个方向证明不了隔离是**对称**的：读写落在错误的 schema 上只会在一边显形。"""
        with tenant_context(self.tenant_b):
            make_row(order_no=NO_B, actor=ACTOR_B, summary=MARKER_B)

        with tenant_context(self.tenant_a):
            # **阳性对照**：A 自己的表可达、写得进、查得到
            make_row(order_no=NO_A, actor=ACTOR_A, summary=MARKER_A)
            summaries = visible_summaries()
            self.assertIn(MARKER_A, summaries)
            self.assertTrue(OperationLog.objects.filter(order_no=NO_A).exists())
            # 再证明看不到 B 的
            self.assertNotIn(MARKER_B, summaries)
            self.assertFalse(OperationLog.objects.filter(order_no=NO_B).exists())

        with tenant_context(self.tenant_b):
            self.assertIn(MARKER_B, visible_summaries())
            self.assertTrue(OperationLog.objects.filter(order_no=NO_B).exists())

    # ── 同号碰撞：订单号只有租户内唯一 ──────────────────────────────
    def test_same_order_no_in_both_tenants_each_sees_only_its_own_row(self):
        """两个租户写**同一个** `order_no`，各自只看到自己的那一行。

        判据打在**具体行的 `summary` 值**上，不用 `count()`：数量断言在隔离破裂而
        数字恰好不变时**静默失去判别力**（`test_p2_isolation.py` 同款用例的自述）。
        """
        with tenant_context(self.tenant_a):
            make_row(order_no=SHARED_NO, actor=ACTOR_A, summary=MARKER_A)
        with tenant_context(self.tenant_b):
            make_row(order_no=SHARED_NO, actor=ACTOR_B, summary=MARKER_B)

        with tenant_context(self.tenant_b):
            # 阳性对照：B 里这个订单号**确实查得到**（一行，且是 B 自己写的那一行）
            rows = list(
                OperationLog.objects.filter(order_no=SHARED_NO).values_list(
                    "summary", "actor"
                )
            )
            self.assertEqual(rows, [(MARKER_B, ACTOR_B)])

        with tenant_context(self.tenant_a):
            rows = list(
                OperationLog.objects.filter(order_no=SHARED_NO).values_list(
                    "summary", "actor"
                )
            )
            self.assertEqual(rows, [(MARKER_A, ACTOR_A)])

    # ── 真实写入口：`apps/audit/log.py::record()` ────────────────────
    def test_record_entry_point_is_tenant_scoped(self):
        """至少一条走**生产写入口** `record()`，证明隔离不只属于 `objects.create`。

        `record()` 是 spec §7.1 的唯一写入出口（`summary` 由行内列渲染），
        业务埋点全部经它落行——它写错 schema 的话，模型层的隔离测试一条都抓不到。
        """
        with tenant_context(self.tenant_a):
            # 前置：本 schema 里没有 pk=11 的用户 ⇒ 显示名**确定性地**回落 `actor`
            # （spec §3.2 的「查不到 ⇒ 回落 `actor`」那一档；否则期望串会取决于夹具里
            # 恰好存在哪个人，判据会被别处新增的夹具悄悄改味）。
            self.assertFalse(get_user_model().objects.filter(pk=11).exists())
            record(
                log_type=10,
                action="order.open_edit",
                actor=ACTOR_A,
                actor_id=11,
                target_kind="order",
                target_id=REC_NO_A,
                order_no=REC_NO_A,
            )

        with tenant_context(self.tenant_b):
            # **阳性对照**：B 的表可达且 `record()` 写得进
            self.assertFalse(get_user_model().objects.filter(pk=22).exists())
            record(
                log_type=10,
                action="order.open_edit",
                actor=ACTOR_B,
                actor_id=22,
                target_kind="order",
                target_id=REC_NO_B,
                order_no=REC_NO_B,
            )
            self.assertTrue(OperationLog.objects.filter(order_no=REC_NO_B).exists())
            # 顺带钉住 `record()` 的渲染结果确实落在了这张表上
            row_b = OperationLog.objects.get(order_no=REC_NO_B)
            self.assertEqual(row_b.summary, f"【{ACTOR_B}】{OPEN_EDIT_BODY}：{REC_NO_B}")
            self.assertEqual(row_b.actor, ACTOR_B)
            self.assertEqual(row_b.actor_id, 22)
            # 再证明看不到 A 的
            self.assertFalse(OperationLog.objects.filter(order_no=REC_NO_A).exists())
            self.assertFalse(OperationLog.objects.filter(actor=ACTOR_A).exists())

        with tenant_context(self.tenant_a):
            row_a = OperationLog.objects.get(order_no=REC_NO_A)
            self.assertEqual(row_a.summary, f"【{ACTOR_A}】{OPEN_EDIT_BODY}：{REC_NO_A}")
            self.assertEqual(row_a.actor, ACTOR_A)
            self.assertEqual(row_a.actor_id, 11)


def _grant_logs_node_and_login(domain, username):
    """在当前 schema 里建 `system.logs` 节点 + 角色，返回**真实登录**的 client。

    与 `tests/test_operation_log_api.py::LogApiTestCase.setUp` 同口径：只关心日志列表的
    读取准入这一条节点。

    ⚠️ 用 `get_or_create` 而不是 `create`：租户 A（`TenantTestCase` 的 `test` schema）
    只跑迁移、没有目录种子，而 **B 是 `provision_tenant` 建的、`seed_tenant` 已经灌过
    完整 RBAC 目录**（`apps/platform/provisioning.py:114`）——`benchmark_id=108` 在 B 里
    已经存在，硬 `create` 会撞 `rbac_permissionnode_benchmark_id_key`（实测踩到）。
    """
    node, _ = PermissionNode.objects.get_or_create(
        benchmark_id=108,
        defaults={
            "code": "system.logs",
            "name": "系统日志",
            "note": "系统日志列表",
            "url": "/System/SystemLog",
            "kind": NodeKind.PAGE,
            "level": 1,
        },
    )
    # 自证拿到的是**这一条**节点：`benchmark_id` 撞上别的 code 时，上面的 get_or_create
    # 会静默返回那条错节点，随后请求以 403 失败——症状会指向「权限没给对」而不是「拿错了节点」。
    assert node.code == "system.logs", f"benchmark_id=108 不是 system.logs：{node.code!r}"
    client = make_logged_in_client(domain=domain, username=username)
    role = Role.objects.create(
        name="日志管理员", group=Group.objects.create(name="日志管理员")
    )
    role.permissions.set([node])
    User.objects.get(username=username).groups.add(role.group)
    return client


class OperationLogHttpIsolationTests(TenantTestCase):
    """HTTP 层：请求式 API 服务的行，同样只在**调用者**租户里。

    写入走 `POST /api/audit/events/`（spec §6.2 的读操作上报，准入只有「已登录」），
    读取走 `GET /api/audit/logs/`（准入 `system.logs`）——两条腿都在**当前连接**上跑，
    而连接由中间件按**认证态的宿主域名**钉住（`apps/audit/api/views.py` 的模块 docstring）。

    判据用 **`actor` 用户名**：它在租户内唯一、且两租户取值不同，并且**在响应体里**
    （`GET /api/audit/logs/` 的响应不含 `order_no`，见 `apps/audit/api/serializers.py`）。
    订单号则落在 ORM 断言上。

    ⚠️ **跨域名请求不会在响应结束时还原 schema**（实测，见 `tests/test_p3b_isolation.py`
    的模块说明）：所以 `setUp` 第一行显式把连接钉回租户 A，B 侧请求一律走
    `_on_b()`，它用 `try/finally` 把连接钉回 A。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B + "_http", name="操作日志 HTTP 隔离租户 B",
            domain="p13-iso-http-b.test.local",
        )

    def setUp(self):
        # 第一行：挡住上一条用例把连接留在 B 的 schema 上的残留（见类 docstring）。
        connection.set_tenant(self.tenant_a)
        self.client_a = _grant_logs_node_and_login(DOMAIN_A, HTTP_USER_A)
        try:
            with tenant_context(self.tenant_b):
                self.client_b = _grant_logs_node_and_login(
                    "p13-iso-http-b.test.local", HTTP_USER_B
                )
        finally:
            connection.set_tenant(self.tenant_a)

    # ── 请求工具 ────────────────────────────────────────────────────
    def _report(self, client, order_no):
        response = client.post(
            EVENTS_ENDPOINT,
            {"action": "order.open_edit", "order_no": order_no},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _actors(self, client):
        """`GET /api/audit/logs/` 的 `actor` 列；先断言端点本身成功（前置哨兵）。"""
        response = client.get(LOGS_ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return [row["actor"] for row in body["data"]["items"]]

    def _on_b(self, func, *args):
        """在 B 的域名上发请求，**结束后把连接钉回租户 A**（见类 docstring）。"""
        try:
            return func(*args)
        finally:
            connection.set_tenant(self.tenant_a)

    def test_reported_read_lands_in_and_is_visible_only_in_its_own_tenant(self):
        # ── A 与 B 各经真实请求上报一次 ──
        self._report(self.client_a, HTTP_NO_A)
        self._on_b(self._report, self.client_b, HTTP_NO_B)

        # ── 读路径：各租户的响应只含自己的操作人 ──
        actors_a = self._actors(self.client_a)
        self.assertTrue(actors_a, "A 的日志列表为空——下面的负断言会被空列表凑出来")
        self.assertIn(HTTP_USER_A, actors_a)      # 阳性对照
        self.assertNotIn(HTTP_USER_B, actors_a)   # 判据：B 的用户名（不是主键）

        actors_b = self._on_b(self._actors, self.client_b)
        self.assertTrue(actors_b, "B 的日志列表为空——下面的负断言会被空列表凑出来")
        self.assertIn(HTTP_USER_B, actors_b)      # 阳性对照
        self.assertNotIn(HTTP_USER_A, actors_b)

        # ── 落库位置：每张表里只有自己那次上报的订单号 ──
        with tenant_context(self.tenant_a):
            self.assertTrue(OperationLog.objects.filter(order_no=HTTP_NO_A).exists())
            self.assertFalse(OperationLog.objects.filter(order_no=HTTP_NO_B).exists())
            self.assertEqual(
                OperationLog.objects.get(order_no=HTTP_NO_A).summary,
                f"【{HTTP_USER_A}】{OPEN_EDIT_BODY}：{HTTP_NO_A}",
            )
        with tenant_context(self.tenant_b):
            self.assertTrue(OperationLog.objects.filter(order_no=HTTP_NO_B).exists())
            self.assertFalse(OperationLog.objects.filter(order_no=HTTP_NO_A).exists())
            self.assertEqual(
                OperationLog.objects.get(order_no=HTTP_NO_B).summary,
                f"【{HTTP_USER_B}】{OPEN_EDIT_BODY}：{HTTP_NO_B}",
            )
