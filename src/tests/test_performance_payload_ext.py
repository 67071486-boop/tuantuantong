"""绩效载荷的三个新只读键，且 `taker_project` **不是** `dl_project`（spec §5.5 / §8 第 18 条）。"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/performances/"


class PerformancePayloadExtTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # ⚠️ 节点必须**同时**含 `order.list`：列表端点的门是 `my_performance.export`，
        #    但数据范围收口在 `performance_queryset` 末尾——不持 `order.list` 时只看得见
        #    **自己的**行（`user=request.user`），而本夹具的行归属 `payload-ext-taker`
        #    ⇒ 只给 `my_performance.export` 会拿到空列表（判据会以「0 行」的形式假红）。
        self.client = make_logged_in_client(
            username="payload-ext-cs",
            groups=("客服",),
            nodes=("my_performance.export", "order.list"),
        )
        self.taker = self._user("payload-ext-taker", "示例打手")
        # ⚠️ 两个「项目」刻意取**不同的值**：订单项目与接手项目分叉时才验得出键没串。
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260923000000002001",
            status=OrderStatus.IN_PROGRESS,
            project_text="订单项目-深渊满星",
        )
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
            hours=24,
            project="接手项目-三角洲",
        )

    def _user(self, username, first_name):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            username=username, first_name=first_name
        )

    def _row(self):
        body = self.client.get(
            URL, {"order_no": self.order.order_no, "emp_type": "打单工资"}
        ).json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)      # 非空哨兵
        return items[0]

    def test_the_three_new_keys_carry_their_values(self):
        row = self._row()
        self.assertEqual(row["display_name"], "示例打手")
        self.assertEqual(row["hours"], 24)
        self.assertEqual(row["taker_project"], "接手项目-三角洲")

    def test_the_two_project_keys_are_different_fields(self):
        row = self._row()
        self.assertEqual(row["dl_project"], "订单项目-深渊满星")
        self.assertEqual(row["taker_project"], "接手项目-三角洲")
        self.assertNotEqual(row["dl_project"], row["taker_project"])

    def test_a_missing_nickname_falls_back_and_null_hours_stay_null(self):
        self.taker.first_name = ""
        self.taker.save(update_fields=["first_name"])
        Performance.objects.filter(pk=self.perf.pk).update(hours=None, project="")
        row = self._row()
        self.assertEqual(row["display_name"], "payload-ext-taker")
        self.assertIsNone(row["hours"])
        self.assertEqual(row["taker_project"], "")
