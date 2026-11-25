"""`?payment_status=` 检索参数（spec §5.3 / C13 / §8 第 15 条）。

三档各一条数据、逐档断言结果集**恰好**是那一档；空值与非码值 ⇒ `code == 1000`
（不是静默全量、也不是 500）；不传该参数时三档都回（既有行为不回归）。
"""
from urllib.parse import quote

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderStatus
from tests.p5_auth_helpers import grant_games, make_logged_in_client

LIST_URL = "/api/business/orders/"


class PaymentStatusSearchTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="paystat-search", groups=("客服",), nodes=("order.list",)
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。本类四条用例都断言「三档各回自己那一档」，
        # 前提是这位读者看得到 `self.game` 下的三张单（读侧过 `game_scope`）；
        # 缺档案 ⇒ 空集 ⇒ 条数断言全红（那不是本批想测的过滤语义）。
        grant_games(User.objects.get(username="paystat-search"), self.game)
        for index, code in enumerate(("UNSETTLED", "SETTLING", "SETTLED")):
            Order.objects.create(
                game=self.game,
                order_no=f"2026092300000000030{index}",
                status=OrderStatus.UNASSIGNED,
                payment_status=code,
            )

    def _nos(self, query):
        body = self.client.get(LIST_URL + query).json()
        self.assertEqual(body["code"], 0, (query, body))
        return sorted(row["order_no"] for row in body["data"]["items"])

    def test_each_code_returns_exactly_that_tier(self):
        self.assertEqual(self._nos("?payment_status=UNSETTLED"), ["20260923000000000300"])
        self.assertEqual(self._nos("?payment_status=SETTLING"), ["20260923000000000301"])
        self.assertEqual(self._nos("?payment_status=SETTLED"), ["20260923000000000302"])

    def test_blank_and_non_code_values_are_validation_errors(self):
        for query in (
            "?payment_status=",
            "?payment_status=" + quote("未收款"),
            "?payment_status=BOGUS",
        ):
            body = self.client.get(LIST_URL + query).json()
            self.assertEqual(body["code"], 1000, (query, body))

    def test_omitting_the_parameter_returns_all_three(self):
        self.assertEqual(len(self._nos("")), 3)

    def test_the_filter_composes_with_the_existing_status_filter(self):
        """与既有 `status` 参数叠加时两个条件都要生效（不是「后来的盖掉先前的」）。"""
        body = self.client.get(
            LIST_URL + "?status=UNASSIGNED&payment_status=SETTLING"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]],
            ["20260923000000000301"],
        )
