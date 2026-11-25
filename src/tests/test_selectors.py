"""`selectors.py`：**0 不是「没传」**（Ruling 39 的同形状清扫）。

`if game_id:` 这种真值判据会把 `0`（以及任何假值）当成「调用方没传这个条件」，
于是筛选被**静默**丢掉、查询退化成「不过滤」。Task 5 评审在金额两端
（`price_min` / `price_max`）抓到的是同一条形状——`bool(Decimal("0.00")) is False`，
`?price_max=0.00` 会连 300.00 的单一起返回（端到端对照见 `tests/test_hall_api.py`）。
本文件把同一形状在 `game_id` / `server_id` 上的语义钉死，免得下一个人照抄。

⚠️ **与 API 出口的分工（实测）**：`?game=0` / `?server=0` **到不了**这里——
`OrderSearchSerializer` 的 `IntegerField(min_value=1)` 在入口就判它非法、回 `code=1000`
（`tests/test_order_api.py::test_zero_game_param_is_rejected_not_silently_ignored`）。
所以本文件测的是**选择器层**的语义：非 `None` 的值 = 携带该条件，而不是「忽略它」。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.selectors import search_hall, search_orders


class FalsyZeroIsNotMissingTests(TenantTestCase):
    """非 `None` 的筛选值都必须**真的**收窄查询——包括 `0`。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.other_game = Game.objects.create(name="星穹铁道")
        self.server = GameServer.objects.create(game=self.game, name="国服")
        self.other_server = GameServer.objects.create(game=self.other_game, name="国服")
        self.order = self._order("20260920000000000001", self.game, self.server)
        self.other = self._order("20260920000000000002", self.other_game, self.other_server)

    def _order(self, no, game, server):
        order = Order.objects.create(
            game=game,
            server=server,
            order_no=no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        # 大厅的入选条件要求那条 MAIN 绩效行存在且 `user` 为空
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    @staticmethod
    def _nos(qs):
        return [o.order_no for o in qs]

    def test_search_hall_zero_game_id_is_a_filter(self):
        # 正向哨兵：真 id 命中 ⇒ 这条筛选本来就在工作
        # （否则下面的空集也可能是「过滤器整个坏了」而不是「0 被当成条件」）
        self.assertEqual(
            self._nos(search_hall(game_id=self.game.id)), [self.order.order_no]
        )
        # `0` 不是「没传」：必须收窄成空集，而不是退化成「全部游戏的大厅单」
        self.assertEqual(self._nos(search_hall(game_id=0)), [])

    def test_search_hall_zero_server_id_is_a_filter(self):
        self.assertEqual(
            self._nos(search_hall(server_id=self.server.id)), [self.order.order_no]
        )
        self.assertEqual(self._nos(search_hall(server_id=0)), [])

    def test_search_orders_zero_game_id_is_a_filter(self):
        self.assertEqual(
            self._nos(search_orders(game_id=self.game.id)), [self.order.order_no]
        )
        self.assertEqual(self._nos(search_orders(game_id=0)), [])

    def test_search_orders_zero_server_id_is_a_filter(self):
        self.assertEqual(
            self._nos(search_orders(server_id=self.server.id)), [self.order.order_no]
        )
        self.assertEqual(self._nos(search_orders(server_id=0)), [])
