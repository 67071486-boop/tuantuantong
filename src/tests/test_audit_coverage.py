"""B1-8：改金额 / 审核 / 交单的端点都要写**数据库**留痕（spec §8.3；E-0009）。

    📌 **本任务的范围已缩小**（P3b Part 2 的 B1-6 段记了原因）：`AuditLog.Action.REVIEW`
    的追加与迁移**已随 B1-6 落地**（`/review` 必须能写审核留痕）。所以这里做的是**收口**：
    把「留痕必须是**数据库行**、且**形状完整**」这条纪律在**跨端点**层面钉一次——
    单个端点的用例（B1-3/5/6）各有自己的断言，但「谁都没检查 actor/前后值是否为空」是共同的空档。

    ⚠️ **本文件在 已迁到新表 `OperationLog`**：三个端点（`/submit`、`/adjust`、`/review`）
    都是 转换过的写路径，旧表不再收到行。迁移是**按意图**做的——「形状完整」这条纪律
    原样保留，只是 `action` 换成新 code、`from/to` 换成各动作自己的语义。

    ⚠️ **为什么强调「数据库行」**：本项目的留痕一度用 `logger.info()` 实现，
    而**没有 `LOGGING` 配置**时它是空操作；`assertLogs` 还会把级别临时抬到 INFO ⇒
    那版测试**全绿、生产一行都没写**（E-0009）。断言打在 `OperationLog` 的行上，才是在**真实出口**上断言。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的节点门（三条）。前两条在 `OrderViewSet.ORDER_PERMISSIONS`，
#: 后两条在 `PerformanceViewSet.PERFORMANCE_PERMISSIONS`（`/adjust` 与
#: `/review` 各要一个 code；legacy 的 `business.review_performance` /
#: `business.override_taker_price` 已冻结，D-0028）。`groups` 只建 Group、**不放行**。
AUDIT_ORDER_NODES = (
    "order.submit_work",               # `/submit/`
    "order.view_detail",
    "order.submit_payout_price",       # `/adjust/`
    "order.change_status_completed",   # `/review/`
)


class AuditRowShapeTests(TenantTestCase):
    """三个「动钱 / 动审核」的端点，各跑一次真实请求，然后检查**那一行**的形状。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"),
        )
        self.taker_client = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=AUDIT_ORDER_NODES,
            # ⚠️ 显式 opt-out（默认值翻面）：下面自己建带提成率的档案 ⇒
            # 夹具必须**不建**，否则撞唯一约束；游戏由下面那句 `grant_games` 逐款补。
            games=None,
        )
        self.taker = User.objects.get(username="taker1")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("17.60"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # `/submit/` 先按订单号取订单（读侧过 `game_scope`），缺档案 ⇒ 命中不到该单
        # ⇒ 一行留痕都不写。⚠️ 必须在 `StaffProfile.objects.create` **之后**补
        # （`grant_games` 是 `get_or_create`，先补会撞 user 唯一约束）。
        grant_games(self.taker, self.game)

    def _cs_client(self):
        """客服（`/adjust` 的节点在 `AUDIT_ORDER_NODES` 里）。

    ⚠️ 原来的 `with_override` 开关是 legacy codename 的： 起
    `/adjust` 的 body 层判的是 `can_edit_taker_price`（flag 反面），挂不挂那条旧
    codename 都不影响结果，所以开关与它一起删掉（不让"判据到底看谁"含糊）。
"""
        client = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=AUDIT_ORDER_NODES,
            # ⚠️ 显式 opt-out（默认值翻面）：游戏由下面那句
            # `grant_games(..., self.game)` 逐款补；夹具不按新默认建全量档案。
            games=None,
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 客服要改这张单的绩效（`/adjust/`），显式声明它持有该单的游戏；
        # 该用例的期望值一个字没改。
        grant_games(User.objects.get(username="cs1"), self.game)
        return client

    def _reviewer_client(self):
        """客服主管（`/review` 的节点在 `AUDIT_ORDER_NODES` 里，同上不再挂 legacy codename）。"""
        client = make_logged_in_client(
            username="reviewer1", groups=("客服主管",), nodes=AUDIT_ORDER_NODES,
            # ⚠️ 显式 opt-out（默认值翻面）：同上。
            games=None,
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 客服主管要审核这张单的绩效（`/review/`），显式声明它持有该单的游戏；
        # 该用例的期望值一个字没改。
        grant_games(User.objects.get(username="reviewer1"), self.game)
        return client

    def _assert_row_shape(self, action, *, expected_from=None, expected_to=None,
                          from_may_be_empty=False):
        """**只取那一行**（不按 count），并断言形状完整。

    ⚠️ `from_value` **允许为空**，但必须**有解释**：`/adjust` 记的是「被改的那个字段」的值，
    而「改前没有覆盖值（NULL）」就是一个**真实的空**——把它写成 `0` 或订单默认价都是**编造**。
    `/submit` 的模板也不含 `{from_value}`（交单没有「改前值」这一说），同样按真实的空放过。
    所以：`actor` 与 `to_value` **必须**非空；`from_value` 只有调用方声明「可能为空」时才放过。
"""
        row = OperationLog.objects.get(order_no=self.order.order_no, action=action)
        self.assertTrue(row.actor, f"{action} 行的 actor 为空——留痕记不出「谁做的」")
        self.assertTrue(row.to_value, f"{action} 行的 to_value 为空——记不出「改成多少」")
        if not from_may_be_empty:
            self.assertTrue(row.from_value, f"{action} 行的 from_value 为空——记不出「改前是多少」")
        if expected_from is not None:
            self.assertEqual(row.from_value, expected_from)
        if expected_to is not None:
            self.assertEqual(row.to_value, expected_to)
        return row

    def test_submit_writes_a_performance_submit_row_with_a_full_shape(self):
        self.taker_client.post(
            f"/api/business/orders/{self.order.order_no}/submit/", {},
            content_type="application/json",
        )
        # ⭐ R2：交单**不写任何金额** ⇒ 留痕的 `to_value` 是显式的 `0.00`
        # （「本次未写金额」，spec §8-5）；`performance.submit` 没有「改前值」⇒ `from_value` 是真实的空
        row = self._assert_row_shape(
            "performance.submit", expected_to="0.00", from_may_be_empty=True
        )
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.target_name, "taker1")

    def test_adjust_writes_a_performance_adjust_row_with_a_full_shape(self):
        self._cs_client().post(
            f"/api/business/performances/{self.perf.id}/adjust/",
            {"taker_price": "66.00", "reason": "议价"}, content_type="application/json",
        )
        # 改前**没有**覆盖值（NULL）⇒ `from_value` 是空串，这是真实的空，不是漏记。
        row = self._assert_row_shape(
            "performance.adjust", expected_to="66.00", from_may_be_empty=True
        )
        self.assertEqual(row.from_value, "")
        self.assertEqual(row.field_name, "taker_price")
        self.assertEqual(row.reason, "议价")
        # 字段的中文名由静态标签表派生（R10：reason 只放纯值，不进 summary）
        self.assertIn("接单价覆盖值", row.summary)

    def test_review_writes_a_performance_review_row_with_a_full_shape(self):
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        self.perf.review_status = ReviewStatus.PENDING
        self.perf.save(update_fields=["review_status"])
        self._reviewer_client().post(
            f"/api/business/performances/{self.perf.id}/review/",
            {"approved": True, "comment": "OK"}, content_type="application/json",
        )
        row = self._assert_row_shape(
            "performance.review",
            expected_from="待审核",
            expected_to="已通过",
        )
        self.assertEqual(row.reason, "OK")
        self.assertEqual(row.log_type, 20)

    def test_approved_review_records_who_and_when_on_the_performance(self):
        """`reviewed_by` / `reviewed_at` 必须落库——P4 对账时要能追溯「谁审的、什么时候」。"""
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        self.perf.review_status = ReviewStatus.PENDING
        self.perf.save(update_fields=["review_status"])
        self._reviewer_client().post(
            f"/api/business/performances/{self.perf.id}/review/",
            {"approved": True}, content_type="application/json",
        )
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.reviewed_by, "reviewer1")
        self.assertIsNotNone(self.perf.reviewed_at)
