"""批量端点的测试基线（本计划所有后端任务共用）。"""
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType, Game, Order, OrderRefund, OrderStatus, Performance, SettleStatus,
)
from apps.business.order_writes import WriteContext
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BATCH = "/api/business/orders/batch"

#: 领域层用例用的固定 ctx（写动作的业务列操作人 / 留痕操作人**刻意不同源**）。
CTX = WriteContext(actor="测试业务操作人", log_actor="测试登录账号", log_actor_id=1)

#: 领域层用例用的「节点已给足」的 ctx（状态机与 `has_node_now` 之外的判据才用得着）。
#: ⭐ 计划 ④：`order.send_to_grab_hall`(36) 也挂上——`/transition/` 的
#: `WAITING_GRAB` 目标与 `/throw-to-hall/` 都已改判独占 36，本 ctx 的用例里有打那个目标的
#: （`tests/test_order_writes_core.py` 的「真写路径」），缺了它是错门。
CTX_FULL = WriteContext(
    actor="测试业务操作人", log_actor="测试登录账号", log_actor_id=1,
    nodes=frozenset({"order.change_status", "order.change_status_completed",
                     "order.submit_payout_price", "order.set_offer_price",
                     "order.send_to_grab_hall"}),
)


class OrderBatchTestBase(TenantTestCase):
    """一个游戏 + 一个挂了本批全部节点的账号 + 建单/结账/打请求三个辅助。

    ⚠️ **节点必须显式声明**（`make_logged_in_client` 的 `nodes` 默认空集 ⇒ fail-closed）。
    本基线一次挂齐 9 个批量端点用到的节点；**要测「没有权限 ⇒ 403」的用例另建一个
    `nodes=()` 的账号**，⛔ 不许在本基线上放宽。
    ⚠️ `groups` 用**非内置组名**：内置同名 `Role` 是租户级共享的，会让节点集互相泄漏
    （`test_order_payment_status_api.py:29-37` 记了这条实测）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.client = make_logged_in_client(
            username="obatch-user",
            groups=("批量测试组",),
            nodes=(
                "order.list", "order.submit_payout_price", "order.edit_payout_price",
                "order.change_payment_status", "order.add_edit", "order.submit_edit",
                "order.set_offer_price", "order.change_status",
                # ⭐ 计划 ④：`batch/transition` 的「待抢单」目标已改判独占 36。
                "order.send_to_grab_hall",
                "order.change_status_completed", "order.assign_taker",
                "order.clear_taker", "order.delete", "order.recycle",
                "order.online_refund", "finance.change_settlement_status",
            ),
        )
        grant_games(User.objects.get(username="obatch-user"), self.game)
        #: 客服归属人（退款项与「重算客服提成」都用它）。
        self.sales = User.objects.create_user(username="obatch-sales", password="测试口令-客服")
        self._order_seq = 0

    def _order(self, **kw):
        """建一笔单。单号**必须 20 位数字**（`OrderViewSet.lookup_value_regex = r"\\d{20}"`）。

        ⚠️ `project_text` 默认给一个**合成值**：`update_order_fields` 会跑
        `validate_required`，空项目文本是 1000（`代练/陪玩项目是必填项`）——
        `batch/types` 那条核心腿必须有它才走得到「改类型」那一步。
        要考「项目文本缺失」的用例显式传 `project_text=""` 覆盖（⛔ 别删这一行默认值，
        否则 `batch_types` / `update_order_fields` 的用例全部变成考项目校验）。
        ⚠️ `game` 可覆盖（默认 = 本类 setUp 建的那只）：`assert_game_allowed` 的判别用例
        需要一笔挂在**当前账号没有权限**的游戏上的单（批量端点会因 `game_scope` 取不到它
        而报 `ineligible`）。⛔ 不要为了让那类用例过而放宽本基线的节点声明。
        """
        self._order_seq += 1
        kw.setdefault("status", OrderStatus.UNASSIGNED)
        kw.setdefault("game", self.game)
        kw.setdefault("project_text", "测试项目甲")
        return Order.objects.create(
            order_no=f"2026092600000000{self._order_seq:04d}", **kw
        )

    def _main_perf(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    def _customer_perf(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.SALES)

    def _settle(self, perf):
        """把这一行弄成「已结账」（`Performance.is_settled` 是三字段并集，写一列即可）。"""
        perf.settle_status = SettleStatus.SETTLED
        perf.save(update_fields=["settle_status"])

    def _post(self, path, payload):
        return self.client.post(path, payload, content_type="application/json").json()
