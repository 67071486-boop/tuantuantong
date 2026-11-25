"""`taker_name` 改「昵称优先、空回落登录账号」，且**列表仍是一条 SQL**（C11 / spec §8 第 17 条）。

口径与 `apps/business/order_stats.py` / `apps/accounts/payloads.py` 一致：
**只把空串当没有**（⛔ 不在这里发明「空白串也算空」的第二套口径）。
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import make_logged_in_client

LIST_URL = "/api/business/orders/"
NODES = ("order.list",)


class TakerNameNicknameTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="nickname-cs", groups=("客服",), nodes=NODES
        )

    def _order_with_taker(self, no, *, first_name, username):
        from django.contrib.auth import get_user_model

        taker = get_user_model().objects.create_user(
            username=username, first_name=first_name
        )
        order = Order.objects.create(
            game=self.game, order_no=no, status=OrderStatus.IN_PROGRESS
        )
        Performance.objects.create(
            order=order,
            user=taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        return order

    def _rows(self):
        body = self.client.get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        return {row["order_no"]: row for row in body["data"]["items"]}

    def test_the_nickname_wins_when_it_is_not_empty(self):
        order = self._order_with_taker(
            "20260923000000001001", first_name="示例打手", username="zhang-san"
        )
        self.assertEqual(self._rows()[order.order_no]["taker_name"], "示例打手")

    def test_an_empty_nickname_falls_back_to_the_login_name(self):
        order = self._order_with_taker(
            "20260923000000001002", first_name="", username="li-si"
        )
        self.assertEqual(self._rows()[order.order_no]["taker_name"], "li-si")

    def test_a_whitespace_nickname_is_not_treated_as_empty(self):
        """只把**空串**当没有（与 `order_stats.py` 同口径）：`"  "` 照原样出参。"""
        order = self._order_with_taker(
            "20260923000000001003", first_name="  ", username="wang-wu"
        )
        self.assertEqual(self._rows()[order.order_no]["taker_name"], "  ")

    def test_the_list_query_count_does_not_grow_with_rows(self):
        """**接线判据 + 无 N+1**：1 行与 5 行的查询数必须相等。

        判别力：把注解换成逐行取 `perf.user`（例如在序列化器里查库），或把相关子查询拆成
        「先查 first_name 再查 username」两次，5 行那次会多出 SQL ⇒ 本用例红。
        """
        self._order_with_taker("20260923000000001011", first_name="甲", username="u11")
        with CaptureQueriesContext(connection) as one:
            rows = self._rows()
        self.assertEqual(len(rows), 1)          # 非空哨兵
        for index in range(12, 16):
            self._order_with_taker(
                f"20260923000000001{index:03d}", first_name=f"昵称{index}", username=f"u{index}"
            )
        with CaptureQueriesContext(connection) as five:
            rows = self._rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(one), len(five))
