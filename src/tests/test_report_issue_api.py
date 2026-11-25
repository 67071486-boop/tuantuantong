"""报问题单：`POST /api/business/orders/<order_no>/report-issue/`（业务方 2026-09-28 裁定）。

业务方原话：「更改状态只能从代练中更改为待验收或者问题单，不能随便改动其他状态」。
「待验收」那一步走的是**已有**的 `/submit/`（本文件不改它，判据在 `tests/test_submit_api.py`）；
本文件钉的是**另一半**——`IN_PROGRESS → ISSUE` 这条**受限**通道。

规则（逐条有测试）：
- 挂 `order.submit_work`（105，与 `/submit/` **同一个**权限点；业务方裁定复用并改名
  「提交完单和提交问题单」）。⛔ 它**不是** `order.change_status`（16）——16 会放开任意状态；
- **必须是这张单的接单人**（否则代练员 A 能替 B 报问题单）⇒ 403；
- 来源态**只接受 `IN_PROGRESS`**（`WAITING_GRAB → ISSUE` 这类边在状态机上存在，
  端点仍必须 200+1002 拒掉——来源态闸门在端点、不在状态机）；
- 真写走 `transition_order`（与 `/transition/` 同一条路径）⇒ 状态 → `ISSUE` **且**
  数据库里真的多一条 `order.transition` 留痕（**断言数据库行**，不断言 logger：E-0009）；
- 状态机那条边：持 `order.submit_work` 能到 `ISSUE` / `PENDING_REVIEW`，
  **不能**到 `COMPLETED` / `PAUSED` 等别的目标（「复用 105 不等于提权」的钉子）。

⚠️ 期望值一律写**字面值**（中文 label、code），不从生产模块导出的常量派生——自指断言在常量
被改坏时不红（E-0024）。
"""
from django.contrib.auth.models import User
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_status import can_transition
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `nodes` 默认空 ⇒ 每条用例必须**显式**声明它要过的门（fail-closed）。
REPORT_ISSUE_ORDER_NODES = (
    "order.submit_work",   # `/report-issue/`（复用 105）
    "order.view_detail",
)

#: DRF 对**没有自定义 `message` 的权限类**给的默认 403 文案（本项目 zh-hans 下的实测值；
#: 与 `test_submit_api.py` / `test_rbac_endpoint_enforcement` 同名常量一致）。
NODE_DENIED_MSG = "您没有执行该操作的权限"


class ReportIssueApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game, order_no="20260927000000000001",
            status=OrderStatus.IN_PROGRESS,
        )
        # ⚠️ 接单人用真实登录夹具建（`make_logged_in_client` 会当场建号+登录；
        #    它不接受已存在的用户名）。
        #    `games` **不传**（= 夹具默认给当前租户全部游戏）：本端点不读提成档案、
        #    也不改金额，唯一的读侧要求是 `get_object()` 的 game_scope 看得到这张单
        #    ——⛔ 传 `games=None`（= 不建档案 ⇒ 零游戏 ⇒ 看不到任何单）会让每条用例
        #    先撞上读侧的 1002，根本走不到被测的闸门。
        self.client = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=REPORT_ISSUE_ORDER_NODES,
        )
        self.taker = User.objects.get(username="taker1")
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
        )
        self.url = f"/api/business/orders/{self.order.order_no}/report-issue/"

    def _login(self, *groups, username, nodes=REPORT_ISSUE_ORDER_NODES):
        """换一个账号登录（每个 username 只能用一次：夹具会建号）。

        ⚠️ `games` **不传**（= 默认给全部游戏）：本类每条用例都要让 `get_object()`
        读得到这张单，否则会先撞读侧的 1002 而走不到被测的闸门（见 `setUp` 的说明）。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, nodes=nodes
        )
        return User.objects.get(username=username)

    def _report(self, payload=None):
        return self.client.post(self.url, payload or {}, content_type="application/json")

    # ── 正常路径 ────────────────────────────────────────────────
    def test_taker_reports_issue_and_the_status_becomes_issue(self):
        """本人 + 代练中 + 有原因 ⇒ 状态变成问题单。"""
        body = self._report({"reason": "账号掉线"}).json()
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)

    def test_issue_requires_a_nonblank_reason_without_writing_anything(self):
        for payload in ({}, {"reason": ""}, {"reason": " \t\n　"}):
            with self.subTest(payload=payload):
                response = self._report(payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)
                self.assertIn("请输入问题原因", response.json()["msg"])
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
                self.assertFalse(OperationLog.objects.filter(
                    order_no=self.order.order_no, action="order.transition",
                ).exists())

    def test_the_action_writes_a_real_order_transition_row(self):
        """留痕：数据库里真的多一条 `order.transition`，from/to 都是**中文 label**。

        ⚠️ **断言打在数据库行上、不打在 logger 上**（AGENTS §5-G / E-0009）：
        `assertLogs` 会把 logger 级别抬到 INFO ⇒「生产一行都没写」也会绿。
        ⚠️ 期望值写字面值（`代练中` / `问题单`），不引用 `OrderStatus.*.label`（E-0024）。
        """
        self._report({"reason": "账号掉线"})
        rows = OperationLog.objects.filter(
            order_no=self.order.order_no, action="order.transition"
        )
        # 本端点**复用** `transition_order` ⇒ 只有它写的那一条，⛔ 没有第二条自建留痕。
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.log_type, 10)          # LogType.ORDER
        self.assertEqual(row.field_name, "status")
        self.assertEqual(row.from_value, "代练中")
        self.assertEqual(row.to_value, "问题单")

    def test_a_reason_lands_on_the_transition_log(self):
        """原因走现成 order.transition 的 reason 列和摘要尾段，不另开动作码。"""
        self.assertEqual(self._report({"reason": "  账号掉线  "}).json()["code"], 0)
        row = OperationLog.objects.get(
            order_no=self.order.order_no, action="order.transition"
        )
        self.assertEqual(row.reason, "账号掉线")
        self.assertTrue(row.summary.endswith("（原因：账号掉线）"))

    def test_the_response_carries_the_transitioned_order(self):
        """响应体是流转后的订单（`OrderSerializer`）——前端靠它就地刷新。"""
        body = self._report({"reason": "账号掉线"}).json()
        self.assertEqual(body["data"]["status"], "ISSUE")
        self.assertEqual(body["data"]["order_no"], self.order.order_no)

    # ── 资源级授权：必须是这张单的接单人 ──────────────────────────
    def test_another_taker_cannot_report_someone_elses_order(self):
        """⚠️ 只判「持 `order.submit_work`」不够：接单人不是你 ⇒ 403，且**一个字节都没变**。

        判别力：去掉那句 `perf.user_id != request.user.id` ⇒ 代练员 A 能给 B 的单报问题单
        （本用例变红）。
        """
        self._login("代练员", username="taker_other")
        response = self._report()
        self.assertEqual(response.status_code, 403)
        self.assertIn("不是你接的", response.json()["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        # 被拒的请求不留下任何状态位移留痕
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="order.transition"
            ).exists()
        )

    def test_a_missing_main_performance_row_is_the_same_403(self):
        """缺 MAIN 行 = 「没有本地接单人」⇒ 与「那一行不是你的」**同一个**出口。

        ⚠️ 本端点**不补行**（理由见 `order_views.report_issue` 的注释：B-1 的幂等标记
        会让一次失败请求把业务方要清掉的无主行永久留回来）⇒ 请求后**仍然**没有 MAIN 行。
        """
        self.perf.delete()
        response = self._report()
        self.assertEqual(response.status_code, 403)
        self.assertIn("不是你接的", response.json()["msg"])
        self.assertFalse(
            Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).exists()
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    # ── 传输层授权：节点门 ──────────────────────────────────────
    def test_a_user_without_the_submit_work_node_is_rejected(self):
        """没有 `order.submit_work` ⇒ **HTTP 403**（DRF 默认文案），状态一行不动。

        ⚠️ 必须断 **msg**（403 有多个来源：CSRF / 权限类 / 节点门），并显式排除 CSRF。
        """
        self._login("客服", username="cs1", nodes=("order.view_detail",))
        response = self._report()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").post(
            self.url, {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    # ── 来源态：端点写死，不靠状态机 ────────────────────────────
    def test_report_issue_requires_in_progress(self):
        """来源态不是 `IN_PROGRESS`（待验收）⇒ 200 + 业务码 1002，状态不变。"""
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        body = self._report().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.assertIn("只有代练中", body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)

    def test_waiting_grab_to_issue_is_still_rejected_by_the_endpoint(self):
        """⚠️ **状态机有边不代表端点放行**：`WAITING_GRAB → ISSUE` 在 `TRANSITIONS` 里存在
        （`ISSUE` 的目标节点集按 target 定 ⇒ 每个来源态都有这条边），但本端点的来源态
        闸门写死 `IN_PROGRESS` ⇒ 仍 200 + 1002。这条是「来源态判据不能只靠状态机」的钉子。
        """
        # 前置哨兵：先证明这条边**确实**在状态机里存在——否则本用例测的是「边不存在」，
        # 而那是另一回事（E-0024 同族：没有哨兵的话，把边删掉本用例照样绿）。
        self.assertTrue(
            can_transition(
                OrderStatus.WAITING_GRAB, OrderStatus.ISSUE,
                nodes={"order.change_status"},
            )
        )
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        body = self._report().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.assertIn("只有代练中", body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    # ── 状态机：复用 105 **不等于**提权 ──────────────────────────
    def test_submit_work_reaches_issue_and_pending_review_only(self):
        """持 `order.submit_work`：到得了 `ISSUE`（本端点）与 `PENDING_REVIEW`（`/submit/`），
        到不了 `COMPLETED` / `PAUSED` / `WAITING_GRAB` / `UNASSIGNED` / `REFUNDED`。

        ⚠️ 这是「复用 105 不构成提权」的机械钉子：`_TARGET_NODES[ISSUE]` 加了 `_SUBMIT`
        之后，只有 `ISSUE` 与 `PENDING_REVIEW` 两个目标态认这个节点。
        ⛔ 期望值写字面值（`"order.submit_work"`），不从 `_SUBMIT` 派生。
        """
        nodes = {"order.submit_work"}
        # 能到的两个目标（两个端点的目标各自写死）
        self.assertTrue(can_transition(OrderStatus.IN_PROGRESS, OrderStatus.ISSUE, nodes=nodes))
        self.assertTrue(
            can_transition(OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW, nodes=nodes)
        )
        # 到不了的其它目标：全是假（逐条枚举，不是抽查）
        for target in (
            OrderStatus.COMPLETED,
            OrderStatus.PAUSED,
            OrderStatus.WAITING_GRAB,
            OrderStatus.UNASSIGNED,
            OrderStatus.REFUNDED,
        ):
            with self.subTest(target=target):
                self.assertFalse(
                    can_transition(OrderStatus.IN_PROGRESS, target, nodes=nodes)
                )
