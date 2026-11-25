"""`POST /api/business/orders/<no>/copy`：复制出一张初始状态的新单（spec §8）。

对应 spec §10 的测试清单第 8–13 条。逐条口径（**业务方裁定，不要"改进"**，spec §8.2）：

- 新单恒 `UNASSIGNED`；
- `hall_price`（发单价）**不复制**（业务方原话「不能带发单价」）；
- `refund_fee` **不复制**（新单没有退款记录，照抄就是假账）；
- `source_order_no` **复制**；
- `sales_user` **照抄原单**（提成归原单的销售客服）；原单为空 ⇒ 新单也为空，
  **不**回落到操作人（回落会静默改变提成归属）；
- `created_by` = 认证态操作人的用户名；
- 其余内容字段与金额字段逐一复制。

⚠️ 本文件里的 `COPIED_FIELDS` 是**字面量**，刻意**不** import
`OrderViewSet.COPY_FIELDS`：拿生产常量当期望值是自指断言（E-0024）——
常量被改坏时断言不会红。字面量多写一份，改坏任一侧都会红。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    Order,
    OrderSource,
    OrderStatus,
    OrderType,
    Performance,
    Project,
    ProjectCategory,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 复制端点与录单**同一个**节点（spec §8.1）。
COPY_NODES = ("order.add_edit",)

#: `GET`/`POST` 都用的订单号（20 位，与 `order_no` 列宽一致）。
SOURCE_NO = "20260922000000000021"
#: 原单没有 `sales_user` 时另建的那张单的号。
NO_SALES_NO = "20260922000000000022"
#: 仅存在于租户 B 的单号（跨租户判据用；两租户取值不同）。
ONLY_B_NO = "20260922000000000099"
ONLY_B_TEXT = "仅 B 可见"

#: 复制必须逐一相等的字段（**字面量**，见模块 docstring）。
#: 不含 `hall_price` / `refund_fee` / `order_no` / `status` / `sales_user` / 审计列——
#: 那些各有单独口径，由各自的用例钉。
COPIED_FIELDS = (
    "game_id", "server_id", "category_id", "project_id", "order_source_id",
    "project_text", "content", "demand", "time_requirement",
    "game_account", "game_sub_account", "game_role_name",
    "current_info_1", "current_info_2",
    "customer_phone", "customer_qq", "customer_ww",
    "order_type", "payment_method", "source", "source_order_no",
    "sales_agent", "tag", "remark", "is_urgent",
    "total_price", "received_price", "platform_price", "fee",
)

#: 跨租户用例：租户 A = `TenantTestCase` 的 test schema（域名沿用既有口径），
#: 租户 B 在 `setUpClass` 里供给。域名后缀 `.test.local` 在 `.env` 的
#: `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_A = "tenant.test.com"
DOMAIN_B = "olp1-copy-b.test.local"
SCHEMA_B = "olp1_copy_b"
CS_A = "cs-a-copy"


class OrderCopyApiTests(TenantTestCase):
    """单租户内的复制契约（spec §10 的 8–11、13 条）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.server = GameServer.objects.create(game=self.game, name="天空岛")
        self.category = ProjectCategory.objects.create(game=self.game, name="深境螺旋")
        self.project = Project.objects.create(category=self.category, name="满星")
        self.order_source = OrderSource.objects.create(name="淘宝", remark="来自淘宝")
        self.cs = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=COPY_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 复制端点的原单是经 `get_object()`（被 `game_scope` 收窄的 queryset）取的，
        # 新单写侧过 `assert_game_allowed` ⇒ 发起复制的账号必须显式持有 `self.game`。
        # 期望值一个字没改。
        grant_games(User.objects.get(username="cs1"), self.game)
        self.sales = User.objects.create_user(username="sales1")
        self.taker = User.objects.create_user(username="taker1")
        self.source = Order.objects.create(
            game=self.game,
            server=self.server,
            category=self.category,
            project=self.project,
            order_source=self.order_source,
            order_no=SOURCE_NO,
            status=OrderStatus.COMPLETED,
            project_text="原神-深渊",
            content="客户原始需求",
            demand="打手看的",
            time_requirement="24 小时",
            game_account="acc-1",
            game_sub_account="sub-1",
            game_role_name="角色一",
            current_info_1="信息1",
            current_info_2="信息2",
            customer_phone="13800000000",
            customer_qq="qq-1",
            customer_ww="ww-1",
            order_type=OrderType.BOOST,
            payment_method="支付宝",
            source="淘宝",
            source_order_no="TB-001",
            sales_agent="客服甲",
            tag="微",
            remark="备注",
            is_urgent=True,
            total_price=Decimal("100.00"),
            received_price=Decimal("100.00"),
            platform_price=Decimal("90.00"),
            hall_price=Decimal("60.00"),
            fee=Decimal("5.00"),
            refund_fee=Decimal("30.00"),
            sales_user=self.sales,
        )

    # ── 工具 ────────────────────────────────────────────────────────
    def _copy(self, order_no=None, client=None):
        return (client or self.cs).post(
            f"/api/business/orders/{order_no or self.source.order_no}/copy",
            data={},
            content_type="application/json",
        )

    def _new_order(self, *existing_order_nos):
        """取复制出来的那张单（**排除夹具里的既有单**，不靠主键）。"""
        return Order.objects.exclude(order_no__in=existing_order_nos or (SOURCE_NO,)).get()

    # ── spec §10-8：成功建单 ────────────────────────────────────────
    def test_copy_creates_a_new_order_in_initial_state(self):
        """新单号 ≠ 原单号、状态回初始、发单价不带、退款归零、提成归原单销售。"""
        response = self._copy()
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        # 前置哨兵：先证明端点成功，后面的取行断言才不会以 KeyError 的形式炸在别处。
        self.assertEqual(body["code"], 0, body)
        new = self._new_order()
        self.assertNotEqual(new.order_no, self.source.order_no)
        self.assertEqual(new.status, OrderStatus.UNASSIGNED)
        # ⛔ 业务方：「不能带发单价」
        self.assertIsNone(new.hall_price)
        # 新单没有退款记录；照抄旧值就是假账。
        self.assertEqual(new.refund_fee, Decimal("0.00"))
        # 提成归**原单的**销售客服。
        self.assertEqual(new.sales_user_id, self.sales.pk)
        # 录单人 = 认证态操作人（不是原单的 created_by，也不是请求体）。
        self.assertEqual(new.created_by, "cs1")
        self.assertEqual(body["data"]["order_no"], new.order_no)
        self.assertEqual(body["data"]["status"], OrderStatus.UNASSIGNED)

    def test_copy_copies_every_content_and_money_field(self):
        """内容字段与其余金额 / 来源单号**逐一**相等（spec §8.2 的「复制」行）。"""
        self._copy()
        new = self._new_order()
        for field in COPIED_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(
                    getattr(new, field), getattr(self.source, field),
                    f"{field} 没有被复制",
                )
        # 来源单号照抄（spec §8.2 单列一行；它不唯一，两行会显示同一个号）。
        self.assertEqual(new.source_order_no, "TB-001")

    def test_copy_preserves_project_and_order_category_from_same_game(self):
        category = ProjectCategory.objects.create(game=self.game, name="合成另一分类")
        self.source.category = category
        self.source.save(update_fields=["category"])

        body = self._copy().json()

        self.assertEqual(body["code"], 0, body)
        new = self._new_order()
        self.assertEqual(new.category_id, category.pk)
        self.assertEqual(new.project_id, self.project.pk)

    # ── spec §10-9：绩效行 ─────────────────────────────────────────
    def test_copy_creates_the_customer_performance_row_and_no_old_rows(self):
        """建**自己的**两行绩效（MAIN + SALES），不复制老单的行。

        ⚠️ 夹具先给原单造一条带接单人的 MAIN 行：否则「绩效行没有被复制」这条
        在「新单自己就有 2 行」面前**无法证伪**（0 行与 2 行也会红，但红的原因不同）。
        判据落在 `(emp_type, 用户名)` 的组合上——用主键会被两个 schema 的序列各自从 1
        开始这件事坑到（本类虽在单租户内，口径仍照 Global Constraints 走）。
        """
        Performance.objects.create(
            order=self.source,
            emp_type=EmpType.MAIN,
            user=self.taker,
            temp_amount=Decimal("60.00"),
        )
        self._copy()
        new = self._new_order()
        rows = {
            (row.emp_type, row.user.username if row.user_id else None)
            for row in Performance.objects.filter(order=new).select_related("user")
        }
        self.assertEqual(
            rows,
            {
                (EmpType.MAIN, None),          # 打手行：还没人接
                (EmpType.SALES, "sales1"),     # 客服行：按**新单的** sales_user 建
            },
        )
        self.assertFalse(
            Performance.objects.filter(order=new, user=self.taker).exists(),
            "把老单的绩效行复制过来了",
        )

    def test_copy_with_no_sales_user_leaves_it_null_and_creates_no_sales_row(self):
        """原单 `sales_user` 为空 ⇒ 新单也为空，且**不建**客服行（spec §10-9 / §8.2）。

        ⛔ **不回落成操作人**：那会静默改变提成归属（业务方裁定的方向）。
        """
        source = Order.objects.create(
            game=self.game,
            order_no=NO_SALES_NO,
            project_text="没有销售归属",
            sales_user=None,
        )
        body = self._copy(order_no=source.order_no).json()
        self.assertEqual(body["code"], 0, body)
        new = self._new_order(SOURCE_NO, NO_SALES_NO)
        self.assertIsNone(new.sales_user_id)
        rows = list(
            Performance.objects.filter(order=new).values_list("emp_type", flat=True)
        )
        self.assertEqual(rows, [EmpType.MAIN])

    # ── spec §10-10：留痕 ──────────────────────────────────────────
    def test_copy_writes_an_operation_log_row(self):
        """留痕断言打在**数据库行**上（不是 logger，E-0009）。"""
        new = None
        self._copy()
        new = self._new_order()
        row = OperationLog.objects.get(action="order.copy")
        self.assertEqual(row.order_no, new.order_no)
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, new.order_no)
        # ⭐ {reason} 放原单的**展示单号**（D-0301：来源号非空取它；
        #    本夹具的 `source.source_order_no = "TB-001"` 非空 ⇒ 逐字是来源号，
        #    ⛔ 不是 `order_no`）：模板是「复制订单 {reason} 生成新单：{order_no}」。
        self.assertEqual(row.reason, self.source.source_order_no)
        self.assertEqual(row.actor, "cs1")
        self.assertEqual(
            row.summary,
            f"【cs1】复制订单 {self.source.source_order_no} 生成新单：{new.order_no}",
        )

    # ── spec §10-11：权限 ──────────────────────────────────────────
    def test_copy_requires_the_add_edit_node(self):
        """无 `order.add_edit` ⇒ 403（节点门是**传输层**语义，不是 200 + 业务码）。

        ⚠️ **这个账号必须与 `setUp` 的 `cs1` 不同组**：节点挂在
        `Group → Role → permissions` 上（`apps/rbac/permissions.py::user_node_codes`），
        同组两个用户**共享**那份授权 ⇒ 用 `groups=("客服",)`（brief 片段原样）
        会给这个"无权限"账号**白送** `order.add_edit`，403 变成 200、用例**假绿**。
        """
        other = make_logged_in_client(username="noPerm", groups=("打手",), nodes=())
        response = self._copy(client=other)
        self.assertEqual(response.status_code, 403, response.content[:300])
        # 反向哨兵：拒绝之后一行都没多（403 不是"先建单再报错"）。
        self.assertEqual(Order.objects.count(), 1)

    def test_copy_rejects_anonymous(self):
        """匿名 ⇒ 401（fail-closed 的默认权限类）。"""
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        response = anonymous.post(
            f"/api/business/orders/{self.source.order_no}/copy",
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401, response.content[:300])
        self.assertEqual(response.json()["code"], 401)
        self.assertEqual(Order.objects.count(), 1)

    # ── spec §10-13：畸形入参不 500 ────────────────────────────────
    def test_copy_with_a_malformed_order_no_does_not_500(self):
        """订单号形状不对（`abc`）⇒ **200 + 业务码 1002**，不是 500。

        ⚠️ **口径按实测的真实代码，不按 spec §8.1 那句「原单不存在 → 404」**：
        本仓的分层规则是「业务结果（资源不存在 / 校验失败）= **HTTP 200** + `ErrorCode`」
        （`apps/core/exceptions.py` 的 docstring 与 `:68-71`：`NotFound` → `CODE=BUSINESS(1002)`、
        `status=200`）。本端点用 `self.get_object()`——与 `/transition/`、`/assign/` 等
        **全部兄弟动作同形**——所以「原单不存在」的既有形状是 200 + 1002。
        真返回 HTTP 404 反而会破坏那条全仓规则。

        ⚠️ 本端点带路径转换器 ⇒ `test_no_route_500` 的 explicit-routes 腿会**跳过**它
        （那是假红防护），所以畸形入参由本文件自己钉。判据是**不 500** + 落到既有业务分支。
        """
        response = self._copy(order_no="abc")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1002, response.content[:300])

    def test_copy_with_a_nul_byte_order_no_does_not_500(self):
        """NUL 字节走**既有共享转换器** ⇒ 路由不匹配 ⇒ 404（不是 `DataError` → 500）。

        形态与 `tests/test_p4_no_500.py::P4NulPathTests` 同源：`%00` 在
        `unquote_to_bytes` 之后是**真 NUL 字符**，裸 `<str:...>` 会把它送进 PostgreSQL。
        ⚠️ 这一条的 404 是**路由层**的（Django 根本不进视图），与上面那条业务层的 200+1002
        是两件事，别混。
        """
        response = self._copy(order_no="%00")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertEqual(response.status_code, 404, response.content[:300])


class OrderCopyIsolationTests(TenantTestCase):
    """同游戏跨分类订单可以在本租户复制，另一租户的订单不可复制。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单复制隔离租户 B", domain=DOMAIN_B
        )

    def _make_order(self, tenant_label, order_no, project_text):
        game = Game.objects.create(name=f"合成游戏 {tenant_label}")
        order_category = ProjectCategory.objects.create(game=game, name="订单分类")
        project_category = ProjectCategory.objects.create(game=game, name="项目归属分类")
        project = Project.objects.create(category=project_category, name="合成常用项目")
        return Order.objects.create(
            game=game, category=order_category, project=project,
            order_no=order_no, project_text=project_text,
        )

    def test_copy_is_tenant_scoped(self):
        connection.set_tenant(self.tenant)
        source = self._make_order("A", SOURCE_NO, "仅 A 可见")
        client = make_logged_in_client(
            domain=DOMAIN_A, username=CS_A, groups=("客服",), nodes=COPY_NODES
        )
        with tenant_context(self.tenant_b):
            source_b = self._make_order("B", ONLY_B_NO, ONLY_B_TEXT)

        response = client.post(
            f"/api/business/orders/{SOURCE_NO}/copy",
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        copied_no = body["data"]["order_no"]
        self.assertNotEqual(copied_no, SOURCE_NO)
        copied = Order.objects.get(order_no=copied_no)
        self.assertEqual(copied.category_id, source.category_id)
        self.assertEqual(copied.project_id, source.project_id)

        response = client.post(
            f"/api/business/orders/{ONLY_B_NO}/copy",
            data={},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1002, response.content[:300])
        self.assertEqual(
            set(Order.objects.values_list("order_no", flat=True)), {SOURCE_NO, copied_no}
        )

        with tenant_context(self.tenant_b):
            rows = list(
                Order.objects.values_list(
                    "order_no", "project_text", "category_id", "project_id"
                )
            )
            self.assertEqual(rows, [
                (ONLY_B_NO, ONLY_B_TEXT, source_b.category_id, source_b.project_id),
            ])
            self.assertFalse(OperationLog.objects.filter(action="order.copy").exists())


class CopyReasonSourceFallbackTests(OrderCopyApiTests):
    """⭐ D-0301（2026-10-24）的**回落变体**：来源号为空 ⇒ 复制留痕的 `reason`
    回退成内置单号（`source.source_order_no or source.order_no` 的 `or` 语义）。"""

    def test_blank_source_reason_falls_back_to_the_system_no(self):
        Order.objects.filter(pk=self.source.pk).update(source_order_no="")
        self.source.refresh_from_db()
        self._copy()
        row = OperationLog.objects.get(action="order.copy")
        self.assertEqual(row.reason, self.source.order_no)
        self.assertEqual(
            row.summary,
            f"【cs1】复制订单 {self.source.order_no} 生成新单：{self._new_order().order_no}",
        )
