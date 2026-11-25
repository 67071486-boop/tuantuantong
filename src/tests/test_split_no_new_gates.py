"""D-0357 的**零新增闸对照网**（spec §7.4 / G4 禁止性判据的钉子）。

业务方第二轮拍板 12：「子单发单逻辑和正常单一样就行」——0 元发单闸
**不做**（首轮 G4 推定项整体作废，见 spec §8-R9）。本网把这句钉成**对照**：
对**子单**与**普通单**做同一个发单动作（不带价指派 / 带价指派 / 抛大厅），
两侧的结果**逐字段一致**——谁给子单加了任何发单侧的闸 / 豁免，这里红。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.order_writes import WriteContext, assign_order
from tests.p5_auth_helpers import grant_games, make_logged_in_client

GROUP = "76000000000000000001"
NO_MAIN = "20261101000000000601"
NO_CHILD = "20261101000000000602"
NO_PLAIN = "20261101000000000609"

WAIT_GRAB_NODES = frozenset({"order.send_to_grab_hall"})
#: 机器直调形态的 ctx 节点集 = 这条边自己的节点集（`fix_missing_taker_performance`
#: 的既有口径：数据修复 / 直调没有请求身份，取边表里这条边的节点集）。
ASSIGN_NODES = frozenset({"order.assign_taker", "order.change_status"})


class SplitDispatchParityTests(TenantTestCase):
    """子单发单与普通单**逐字段一致**（G4 的禁止性钉子，三档对照）。"""

    def setUp(self):
        self.game = Game.objects.create(name="拆单发单对照夹具游戏")
        self.client = make_logged_in_client(
            username="parity-cs", groups=("客服",), nodes=("order.list",)
        )
        users = get_user_model().objects
        self.taker = users.create_user(username="parity-taker", password="pw-taker")
        grant_games(self.taker, self.game)  # D-0252 游戏范围闸的夹具补齐
        profile, _ = StaffProfile.objects.get_or_create(user=self.taker)
        profile.save()

        self.child = Order.objects.create(
            game=self.game, order_no=NO_CHILD,
            status=OrderStatus.UNASSIGNED, split_id=GROUP,
            hall_price=Decimal("0.00"),
        )
        self.plain = Order.objects.create(
            game=self.game, order_no=NO_PLAIN,
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("0.00"),
        )

    def _assign(self, order, *, price=None):
        return assign_order(
            order, taker=self.taker, price=price,
            ctx=WriteContext(nodes=ASSIGN_NODES), dry_run=False,
        )

    @staticmethod
    def _fields(order):
        """对照读数：状态 / 发单价 / 主绩效行的（人，预计额）——发单写到的那些列。"""
        order.refresh_from_db()
        main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
        return (
            str(order.status),
            Decimal(order.hall_price),
            None if main is None else main.user_id,
            None if main is None else Decimal(main.temp_amount),
        )

    def test_assigning_without_a_price_behaves_like_a_plain_order(self):
        """**不带价指派**：两侧 outcome 与结果字段逐字段一致（⛔ 不给子单加 0 元闸）。"""
        first = self._assign(self.child)
        second = self._assign(self.plain)
        self.assertTrue(first.ok, first.msg)
        self.assertTrue(second.ok, second.msg)
        self.assertEqual(first.code, second.code)
        self.assertEqual(self._fields(self.child), self._fields(self.plain))
        # 两侧的「接单价」都还是 0 / 原值：没有闸把它拦下，也没有豁免把它改写
        self.assertEqual(Decimal(self.child.hall_price), Decimal("0.00"))
        self.assertEqual(Decimal(self.plain.hall_price), Decimal("0.00"))

    def test_assigning_with_a_price_behaves_like_a_plain_order(self):
        """**带价指派**（客服填发单价，口径 4）：子单侧照常落价、照常建绩效。"""
        first = self._assign(self.child, price=Decimal("66.00"))
        second = self._assign(self.plain, price=Decimal("66.00"))
        self.assertTrue(first.ok, first.msg)
        self.assertTrue(second.ok, second.msg)
        self.assertEqual(self._fields(self.child), self._fields(self.plain))
        self.assertEqual(Decimal(self.child.hall_price), Decimal("66.00"))

    def test_throwing_to_the_hall_behaves_like_a_plain_order(self):
        """**抛大厅**（不带价）：两侧同为 WAITING_GRAB、同一形状。"""
        from apps.business.order_status import apply_transition

        for order in (self.child, self.plain):
            apply_transition(order, OrderStatus.WAITING_GRAB, nodes=WAIT_GRAB_NODES)
        self.assertEqual(self._fields(self.child), self._fields(self.plain))
        self.assertEqual(str(self.child.status), str(OrderStatus.WAITING_GRAB))
