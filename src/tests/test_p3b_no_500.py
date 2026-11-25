"""B1-9：P3b 新端点的**结构兜底**——畸形请求一律不许 500（E-0010 的结构网）。

⚠️ **为什么单独一个文件**：`tests/test_no_route_500.py` 的两条腿覆盖不到它们——
- collections 腿按 `router.registry` 遍历，只发 `GET <collection>/`（`/performances/` 在里面 ✓）；
- explicit routes 腿**跳过带路径转换器的路由**（`<int:pk>` 正是转换器）
  ⇒ `/performances/{id}/adjust/`、`/performances/{id}/review/` **整个逃出那张网**。
  这与 P3a 的 `/hall/{order_no}/claim/` 是同一个形状，那里也是补了一条专门用例。

判据（照 `test_claim.py::test_malformed_query_params_on_claim_do_not_500`）：
**不 500**、响应仍是**标准信封**；并且 detail 动作**不读**查询参数
（`_base_queryset` 刻意不套筛选——见 `performance_views.get_queryset` 的注释），
所以畸形参数在这里是**被忽略**而不是变成业务码。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, ReviewStatus
from tests.p5_auth_helpers import grant_games, make_logged_in_client

MALFORMED = "?emp_type=BOGUS&user=abc&date_from=garbage&page=abc&settle_status=?"

# 请求都打绩效端点，不经过订单节点门。`/adjust`、`/review` 与列表各自要一个绩效节点，
# 两个 client 必须显式声明；`groups` 只建组、不放行。


class P3bDetailRoutesSurviveMalformedParamsTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.PENDING_REVIEW, hall_price=Decimal("88.00"),
        )
        self.taker = User.objects.create_user(username="taker1")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("17.60"), review_status=ReviewStatus.PENDING,
        )
        self.cs = make_logged_in_client(
            username="cs1", groups=("客服",),
            nodes=("order.submit_payout_price", "my_performance.export"),
        )
        self.reviewer = make_logged_in_client(
            username="reviewer1", groups=("客服主管",),
            nodes=("order.change_status_completed",),
        )
        # ⭐ 按游戏权限收窄：`/adjust` 与 `/review` 都走
        # `_base_queryset` ⇒ 两个请求方都要显式持有本夹具的游戏，否则
        # `get_object()` 找不到那张绩效（200 + 1002），下面「参数被忽略、操作照常」
        # 的判据就测不到。期望值一个字没改（仍是 code 0 + 库里真的改了）。
        grant_games(User.objects.get(username="cs1"), self.game)
        grant_games(User.objects.get(username="reviewer1"), self.game)

    def _assert_envelope_not_500(self, response):
        self.assertNotEqual(response.status_code, 500)
        body = response.json()
        self.assertIn("code", body)                      # 仍是标准信封
        return body

    def test_adjust_survives_malformed_query_params(self):
        response = self.cs.post(
            f"/api/business/performances/{self.perf.id}/adjust/{MALFORMED}",
            {"deduct_mode": "1", "deduct_value": "10.00"},
            content_type="application/json",
        )
        body = self._assert_envelope_not_500(response)
        # detail 动作**不读**查询参数 ⇒ 参数被忽略，操作照常成功（不是 1000、更不是 500）
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))

    def test_review_survives_malformed_query_params(self):
        response = self.reviewer.post(
            f"/api/business/performances/{self.perf.id}/review/{MALFORMED}",
            {"approved": True}, content_type="application/json",
        )
        body = self._assert_envelope_not_500(response)
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)

    def test_performances_collection_survives_malformed_query_params(self):
        """集合腿已经由 `test_no_route_500` 覆盖（它是 registry 里的），这里再钉一次**本模块**的出口：
        非法筛选值必须是 **200 + code 1000**，不是 500。"""
        response = self.cs.get(f"/api/business/performances/{MALFORMED}")
        body = self._assert_envelope_not_500(response)
        self.assertEqual(body["code"], 1000)
