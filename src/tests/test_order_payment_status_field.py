"""`Order.payment_status` 的字段口径与伪造面（spec §4 / C1 / C12 / §8 第 5、14、17 条）。

三件事：字段形状（码 + 中文 label + 非空默认）；录单新建的单落默认档；
两个写序列化器都不接受请求体指定它（每条否定断言都带**阳性对照**，证明不是「整个请求挂了」）。

⚠️ 期望值一律写**字面值**（E-0024）：不从 `models.py` 导 `PaymentStatus` 来断言它自己。
"""
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE = "/api/business/orders"


class PaymentStatusFieldShapeTests(TenantTestCase):
    """列形状：三个码、三句中文 label、非空、默认档。"""

    def test_the_field_is_a_non_null_sixteen_char_code_column(self):
        field = Order._meta.get_field("payment_status")
        self.assertEqual(field.max_length, 16)
        self.assertFalse(field.null)
        self.assertEqual(field.default, "UNSETTLED")

    def test_the_choices_are_three_codes_with_the_ruled_chinese_labels(self):
        field = Order._meta.get_field("payment_status")
        self.assertEqual(
            [choice[0] for choice in field.choices],
            ["UNSETTLED", "SETTLING", "SETTLED"],
        )
        self.assertEqual(
            [choice[1] for choice in field.choices],
            ["未收款", "结算中", "已结款"],
        )


class PaymentStatusWritePathTests(TenantTestCase):
    """录单落默认档；两条通用写路径都不接受请求体指定它。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="paystat-field-cs",
            groups=("客服",),
            nodes=("order.add_edit", "order.submit_edit"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。本类三条用例都要在 `self.game` 下录单 / 改单
        # （写侧过 `assert_game_allowed`、改单取单过 `game_scope`），而
        # `grant_games` **没有**接进 `make_logged_in_client` 的默认路径（那会让
        # 「没有档案 ⇒ 什么都看不到」这条新品钉变成恒真）⇒ 这里显式授权。
        grant_games(User.objects.get(username="paystat-field-cs"), self.game)

    def _create(self, **overrides):
        payload = {"game": self.game.id, "project_text": "满星"}
        payload.update(overrides)
        body = self.client.post(
            BASE + "/", payload, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["order_no"]

    def test_intake_lands_the_default_tier(self):
        no = self._create()
        self.assertEqual(Order.objects.get(order_no=no).payment_status, "UNSETTLED")

    def test_intake_ignores_a_client_supplied_value(self):
        no = self._create(payment_status="SETTLED")
        order = Order.objects.get(order_no=no)
        # 阳性对照：同一次请求里 `project_text` 真的写进去了 ⇒ 上面那条不是「整个 create 没生效」。
        self.assertEqual(order.project_text, "满星")
        self.assertEqual(order.payment_status, "UNSETTLED")

    def test_patch_ignores_it_while_other_fields_still_change(self):
        no = self._create(remark="旧备注")
        body = self.client.patch(
            f"{BASE}/{no}/",
            {"payment_status": "SETTLED", "remark": "新备注"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=no)
        self.assertEqual(order.payment_status, "UNSETTLED")
        self.assertEqual(order.remark, "新备注")
