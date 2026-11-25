"""参数入口同源判据（spec §2-2 / §8-1 B18；实施计划 Task 2 的模块级腿）。

`OrderSearchParamsTests` 是**模块级腿**（不碰数据库，判据打在列表与导出共同消费的那个
模块级函数上）；`OrderListBlankParamsHttpTests` 是**列表端点的 HTTP 腿**——它补的是
`?status=` / `?game=` 这类空串在**列表端点**上的判据（此前只有模块级腿，见评审 m2）。
B18 另外两半在别处：`payment_status` 空串/非码值 → 1000 在既有
`tests/test_order_search_payment_status.py`；导出 HTTP 腿（Task 3）在
`tests/test_order_export_api.py`。
"""
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.business.api.order_views import order_search_params
from apps.business.api.serializers import OrderSearchSerializer
from tests.p5_auth_helpers import make_logged_in_client


def _request(**params):
    return Request(APIRequestFactory().get("/api/business/orders/", params))


class OrderSearchParamsTests(SimpleTestCase):
    def test_blank_strings_are_treated_as_not_passed(self):
        data = order_search_params(_request(status="", game=""), OrderSearchSerializer)
        self.assertNotIn("status", data)
        self.assertNotIn("game", data)

    def test_blank_payment_status_still_reaches_the_serializer(self):
        with self.assertRaises(ValidationError):
            order_search_params(_request(payment_status=""), OrderSearchSerializer)

    def test_non_empty_values_still_pass_through(self):
        """⭐ 改准（2026-10-29 实装多选）：`status` 的出参是**已校验的码列表**
        （调用方 `search_orders` 直接喂 `status__in`；单值 = 只有一个元素的列表）。"""
        data = order_search_params(_request(status="UNASSIGNED"), OrderSearchSerializer)
        self.assertEqual(data["status"], ["UNASSIGNED"])

    def test_status_comma_string_passes_through_as_a_list(self):
        """⭐ 2026-10-29：逗号串枚举码逐 token 过校验、以列表出参（列表与导出两个入口共用）。"""
        data = order_search_params(
            _request(status="UNASSIGNED,COMPLETED"), OrderSearchSerializer
        )
        self.assertEqual(data["status"], ["UNASSIGNED", "COMPLETED"])

    def test_status_bad_token_is_rejected(self):
        """任一非法 token ⇒ `ValidationError`（HTTP 层是 200 + `code 1000`，点名那个 token）。"""
        with self.assertRaises(ValidationError) as ctx:
            order_search_params(
                _request(status="UNASSIGNED,BOGUS"), OrderSearchSerializer
            )
        self.assertIn("BOGUS", str(ctx.exception))

    def test_status_over_50_tokens_is_rejected(self):
        too_many = ",".join("IN_PROGRESS" for _ in range(51))
        with self.assertRaises(ValidationError):
            order_search_params(_request(status=too_many), OrderSearchSerializer)


class OrderListBlankParamsHttpTests(TenantTestCase):
    """列表端点上的空串判据（评审 m2）：`?status=&game=` 按「没传」处理，不是非法取值。"""

    def test_blank_status_and_game_are_treated_as_not_passed(self):
        client = make_logged_in_client(
            username="blank-params", groups=("客服",), nodes=("order.list",)
        )
        response = client.get("/api/business/orders/?status=&game=")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.json())
