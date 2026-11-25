"""T2：后台首页「数据概览」端点（`GET /api/business/home/overview/`）的判别力用例。

被测契约 = 计划 `docs/plans/2026-10-04-order-stats-console-and-price-sync.md` 的
§1 口径 3/4/5 与 §4-T2（端点、窗口、逐格算式、fail-closed 闸门）。

⚠️ **判别力纪律**（`AGENTS.md` §5 / E-0009 / E-0024）：
- 每条断言打**值**（字面量），不打常量、不打「有那个键就行」；
- 负向断言之前先证明**同一请求能看到正例**（否则空集能让 `assertNotIn` 假通过）；
- 窗口边界用**确定的时刻**（`created_at` 事后 `update`），不用 `timezone.now()`；
- 夹具一律**合成值**（E-0077），订单号是 20 位数字形状、游戏名是「游戏A」这种占位。
"""
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

URL = "/api/business/home/overview/"

#: 出参键的**恰好集合**（计划 §4-T2 的端点表逐字；⚠️ 没有 `cancel_rate` 之类的别名）。
EXPECTED_KEYS = {
    "start",
    "end",
    "add_count",
    "order_price",
    "hall_price_total",
    "profit_amount",
    "profit_rate",
    "rec_order_count",
    "rec_order_rate",
    "cancel_count",
    "cancel_order_rate",
    "refund_count",
    "refund_order_rate",
    "finish_count",
    "finish_order_rate",
}


def _at(day, hour, minute=0):
    """窗口用例的固定时刻（`Asia/Shanghai`；`USE_TZ=True`）。"""
    return timezone.make_aware(datetime(2026, 9, day, hour, minute))


class HomeOverviewFixtureMixin:
    """共用的夹具：一个游戏 + 两个身份的 client + 造单 helper。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="游戏A")
        self._seq = 0
        #: 每个用例内**只建一次**的两个身份 client（同一用例里第二次 `make_logged_in_client`
        #: 会用同一个用户名再建一次用户 ⇒ 撞 `auth_user_username_key`，那是夹具的坑不是被测代码的）。
        self._clients: dict[str, Client] = {}

    def _client(self, key, factory):
        if key not in self._clients:
            self._clients[key] = factory()
        return self._clients[key]

    def _admin(self):
        """管理员 = 持「系统管理员」职位（`is_account_admin` 为真）。

        ⚠️ 走既有夹具的 `is_superuser=True` 分支：它**顺手把账号加进「系统管理员」组**
        （见 `tests/p5_auth_helpers.py` 的 docstring），而租户平面上的 `is_account_admin`
        认的正是那个组。⛔ 不另写一份「造管理员」的代码。
        """
        return self._client(
            "admin",
            lambda: make_logged_in_client(username="home-admin", is_superuser=True),
        )

    def _non_admin(self):
        """非管理员：登录得进、但没有「系统管理员」职位 ⇒ 该点 403。"""
        return self._client(
            "kefu",
            lambda: make_logged_in_client(username="home-kefu", groups=("客服",)),
        )

    def _order(self, *, status, total, hall, created_at):
        """造一张**确定录单时刻**的单（`created_at` 是 `auto_now_add` ⇒ 事后 update）。"""
        self._seq += 1
        order = Order.objects.create(
            game=self.game,
            order_no=f"20261004{self._seq:012d}",
            status=status,
            total_price=Decimal(total),
            hall_price=None if hall is None else Decimal(hall),
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        return order

    def _main_row(self, order, user=None):
        """给单摆一条 MAIN 绩效行（`user=None` = **还没人接**——这是录单时的常态）。

        ⚠️ 这些「无主 MAIN 行」是**承重夹具**：少了它们，「接手数量」的判据
        （`emp_type=MAIN` **且** `user` 非空）与「`emp_type=MAIN` 就够」这两种实现
        在本用例里会给出**同一个**数字，判据就没有判别力了。
        """
        return Performance.objects.create(
            order=order, user=user, emp_type=EmpType.MAIN
        )

    def _sales_row(self, order, user):
        """给单摆一条 SALES 绩效行（**有归属人**）——用来证明 `emp_type=MAIN` 那条
        过滤是承重的：谁把它删掉，这张单就会被算进「接手数量」。
        """
        return Performance.objects.create(
            order=order, user=user, emp_type=EmpType.SALES
        )

    def _get(self, client, **params):
        response = client.get(URL, params)
        return response.status_code, response.json()


class PermissionTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ①：仅系统管理员（fail-closed）。"""

    def test_admin_gets_200_and_the_envelope(self):
        status, body = self._get(self._admin(), start="2026-09-10", end="2026-09-12")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.OK, body)
        self.assertEqual(set(body["data"]), EXPECTED_KEYS)

    def test_non_admin_gets_403_business_code(self):
        """非管理员 ⇒ **HTTP 403 + 业务码 403**。

        ⚠️ 不能只断 HTTP 状态码：本仓的 403 有多个来源（职位闸 / 节点闸 / CSRF），
        业务码 + HTTP 两条一起断才说明是**权限**拒的。
        ⛔ 也不接受「200 + 全 0」——那正是 fail-closed 要避免的形态
        （前端会以为算出来了，把 0 当成真实统计）。
        """
        status, body = self._get(self._non_admin(), start="2026-09-10", end="2026-09-12")
        self.assertEqual(status, 403, body)
        self.assertEqual(body["code"], 403, body)
        # ⚠️ 同时证明「同一个请求在管理员那里是 200」——否则本用例可能只是因为端点坏了而 403。
        admin_status, admin_body = self._get(
            self._admin(), start="2026-09-10", end="2026-09-12"
        )
        self.assertEqual(admin_status, 200, admin_body)

    def test_anonymous_gets_401_not_403(self):
        """匿名 ⇒ **401**（没认证成功），⛔ 不是 403：两者的处置不同（前端跳登录 vs 提示无权限）。"""
        status, body = self._get(
            Client(SERVER_NAME="tenant.test.com"), start="2026-09-10", end="2026-09-12"
        )
        self.assertEqual(status, 401, body)
        self.assertEqual(body["code"], 401, body)


class AggregateTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ②⑥：三张合成单上的**逐格**数值。"""

    #: 窗口（含首尾整天）。
    START = "2026-09-10"
    END = "2026-09-12"

    def setUp(self):
        super().setUp()
        self.taker = User.objects.create_user(username="home-taker")
        # ① 已退单：100.00 / 发单价 80.00 / **无接单人**（有一条无主 MAIN 行）
        refunded = self._order(
            status=OrderStatus.REFUNDED,
            total="100.00",
            hall="80.00",
            created_at=_at(11, 10, 0),
        )
        self._main_row(refunded)
        # ② 无接单人：50.00 / 发单价 NULL（NULL 按 0）/ 有一条第 ① 形状的无主 MAIN 行
        #    + 一条**有归属人**的 SALES 行（证明 `emp_type=MAIN` 那条过滤承重）
        refused = self._order(
            status=OrderStatus.UNASSIGNED,
            total="50.00",
            hall=None,
            created_at=_at(11, 11, 0),
        )
        self._main_row(refused)
        self._sales_row(refused, self.taker)
        # ③ 有 MAIN 接单人：200.00 / 发单价 150.00；状态取**待验收**（完单口径的两态之一）
        taken = self._order(
            status=OrderStatus.PENDING_REVIEW,
            total="200.00",
            hall="150.00",
            created_at=_at(12, 9, 0),
        )
        self._main_row(taken, self.taker)

    def _data(self):
        status, body = self._get(self._admin(), start=self.START, end=self.END)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.OK, body)
        return body["data"]

    def test_every_cell_has_the_expected_value(self):
        data = self._data()

        self.assertEqual(data["start"], self.START)
        self.assertEqual(data["end"], self.END)
        # 录单笔数 = 窗口内录的 3 张单
        self.assertEqual(data["add_count"], 3)
        # 订单总额 = 100 + 50 + 200（**含**已退单那张）
        self.assertEqual(data["order_price"], "350.00")
        # 发单总额 = 80 + 0(NULL) + 150
        self.assertEqual(data["hall_price_total"], "230.00")
        # 预计利润 = (50 - 0) + (200 - 150) = 100.00（**排除**已退单那张的 100-80）
        self.assertEqual(data["profit_amount"], "100.00")
        # 利润率 = 100 ÷ (50 + 200) = 40.0（分母是**同一子集**的订单总额，不是 350）
        self.assertEqual(data["profit_rate"], "40.0")
        # 接手数量 = 只有第 ③ 张有 MAIN 接单人
        self.assertEqual(data["rec_order_count"], 1)
        # 接手率 = 1 / 3 = 33.333… ⇒ 一位小数
        self.assertEqual(data["rec_order_rate"], "33.3")
        # 撤销两格**恒 0**（本仓没有撤销 / 仲裁状态，数据源不存在）
        self.assertEqual(data["cancel_count"], 0)
        self.assertEqual(data["cancel_order_rate"], "0.0")
        # 退单数量 = 状态为已退单的 1 张；退单率 = 1 / 3
        self.assertEqual(data["refund_count"], 1)
        self.assertEqual(data["refund_order_rate"], "33.3")
        # 完成数量 = 待验收 1 张；完成率 = 1 / 3
        self.assertEqual(data["finish_count"], 1)
        self.assertEqual(data["finish_order_rate"], "33.3")

    def test_counts_are_ints_and_rates_are_single_decimal_strings(self):
        """形状契约：计数是 int（不是字符串）、率是**一位小数**字符串、金额是**两位小数**字符串。"""
        data = self._data()
        for key in ("add_count", "rec_order_count", "cancel_count", "refund_count", "finish_count"):
            self.assertIsInstance(data[key], int, key)
        for key in ("profit_rate", "rec_order_rate", "cancel_order_rate", "refund_order_rate", "finish_order_rate"):
            self.assertIsInstance(data[key], str, key)
            self.assertRegex(data[key], r"^-?\d+\.\d$", key)
        for key in ("order_price", "hall_price_total", "profit_amount"):
            self.assertIsInstance(data[key], str, key)
            self.assertRegex(data[key], r"^-?\d+\.\d\d$", key)

    def test_a_hall_shadow_order_is_not_counted(self):
        """⭐ 二轮评审 Important 5：镜像卡派生的隐藏单不进首页概览的 12 格。

        它没有客户、`total_price` 是对端发单价 ⇒ 算进来会把录单笔数 / 订单总额 / 利润率
        一起撑大。⚠️ 逐格用例（`test_every_cell_has_the_expected_value`）与本条**同一组夹具**
        ⇒ 数值口径不变（3 张真单 / 350.00 / 230.00），只是多摆了一张窗口内的隐藏单：
        **删掉 `home_overview_data` 的排除，这里立刻红**。
        """
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(game=self.game, bound_by=self.taker, no="SYN-SHADOW-HOME")
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261026000000000010",
            total="999.00", price="888.00",
        )
        # `created_at` 是 `auto_now_add` ⇒ 事后推进窗口（否则本条只因「窗口外」而绿）。
        Order.objects.filter(pk=hidden.pk).update(created_at=_at(11, 12, 0))

        data = self._data()

        self.assertEqual(data["add_count"], 3, "隐藏单混进了录单笔数")
        self.assertEqual(data["order_price"], "350.00", "隐藏单的 999.00 混进了订单总额")
        self.assertEqual(data["hall_price_total"], "230.00")


class ZeroAndNegativeAmountTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ⑥（E-0021 同族）：金额为 `0` 与**负数**的单也要能算出来。"""

    def setUp(self):
        super().setUp()
        # 0 金额（合法值，⛔ 不能被当成「没填」而丢掉）
        self._order(
            status=OrderStatus.COMPLETED,
            total="0.00",
            hall="0.00",
            created_at=_at(11, 10, 0),
        )
        # 负金额（D-0015：负值 = 退款 / 冲抵，是合法且有意义的边界）
        self._order(
            status=OrderStatus.COMPLETED,
            total="-30.00",
            hall="-10.00",
            created_at=_at(11, 11, 0),
        )

    def test_zero_and_negative_amounts_are_summed_not_dropped(self):
        status, body = self._get(self._admin(), start="2026-09-11", end="2026-09-11")
        self.assertEqual(status, 200, body)
        data = body["data"]
        self.assertEqual(data["add_count"], 2)
        self.assertEqual(data["order_price"], "-30.00")
        self.assertEqual(data["hall_price_total"], "-10.00")
        self.assertEqual(data["profit_amount"], "-20.00")
        # 利润率 = -20 ÷ -30 × 100 = 66.666…（分子分母都是负 ⇒ 正；一位小数）
        self.assertEqual(data["profit_rate"], "66.7")
        # 两张都是「已完成」⇒ 完单率 100%（不是被 0 金额那一张拖成 50%）
        self.assertEqual(data["finish_count"], 2)
        self.assertEqual(data["finish_order_rate"], "100.0")


class WindowBoundaryTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ③：窗口 = `[start 00:00, end+1天 00:00)`。"""

    def setUp(self):
        super().setUp()
        # 窗口 = 09-10 ~ 09-12 ⇒ 含 09-10 00:00 与 09-12 23:59，不含 09-09 23:59 与 09-13 00:00
        self._order(
            status=OrderStatus.COMPLETED, total="1.00", hall="1.00",
            created_at=_at(9, 23, 59),
        )
        self._order(
            status=OrderStatus.COMPLETED, total="2.00", hall="2.00",
            created_at=_at(10, 0, 0),
        )
        self._order(
            status=OrderStatus.COMPLETED, total="4.00", hall="4.00",
            created_at=_at(12, 23, 59),
        )
        self._order(
            status=OrderStatus.COMPLETED, total="8.00", hall="8.00",
            created_at=_at(13, 0, 0),
        )

    def test_both_ends_are_inclusive_and_neighbours_are_excluded(self):
        status, body = self._get(self._admin(), start="2026-09-10", end="2026-09-12")
        self.assertEqual(status, 200, body)
        data = body["data"]
        # 2 张（09-10 00:00 与 09-12 23:59）；09-09 23:59 与 09-13 00:00 都在窗口外
        self.assertEqual(data["add_count"], 2)
        # 金额精确到「哪两张」：2.00 + 4.00（含 1.00 或 8.00 都会红）
        self.assertEqual(data["order_price"], "6.00")

    def test_a_single_day_window_is_legal_and_covers_the_whole_day(self):
        """`start == end` 必须合法，且覆盖那一整天（00:00 与 23:59 都算）。"""
        status, body = self._get(self._admin(), start="2026-09-12", end="2026-09-12")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.OK, body)
        self.assertEqual(body["data"]["add_count"], 1)
        self.assertEqual(body["data"]["order_price"], "4.00")


class ZeroDenominatorTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ④：分母为 0 ⇒ 率是 `"0.0"`（⛔ 不是 `NaN` / 空串 / `null`）。"""

    def test_empty_window_gives_zero_rates_not_nan(self):
        # 先证明**同一个端点**在有单的窗口里能回非 0（否则本用例可能只是端点坏了）。
        self._order(
            status=OrderStatus.COMPLETED, total="10.00", hall="5.00",
            created_at=_at(11, 10, 0),
        )
        status, body = self._get(self._admin(), start="2026-09-11", end="2026-09-11")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["add_count"], 1)
        self.assertEqual(body["data"]["finish_order_rate"], "100.0")

        status, body = self._get(self._admin(), start="2026-01-01", end="2026-01-02")
        self.assertEqual(status, 200, body)
        data = body["data"]
        self.assertEqual(data["add_count"], 0)
        self.assertEqual(data["order_price"], "0.00")
        self.assertEqual(data["hall_price_total"], "0.00")
        self.assertEqual(data["profit_amount"], "0.00")
        self.assertEqual(data["rec_order_count"], 0)
        self.assertEqual(data["refund_count"], 0)
        self.assertEqual(data["finish_count"], 0)
        for key in (
            "profit_rate",
            "rec_order_rate",
            "cancel_order_rate",
            "refund_order_rate",
            "finish_order_rate",
        ):
            self.assertEqual(data[key], "0.0", key)
            # ⛔ 不是 `NaN` / 空串 / `null`——逐条钉住（`== "0.0"` 已经排除了它们，
            #    这里再显式说明一次判据的意图）。
            self.assertNotIn(data[key], ("", "NaN", "nan", "None"))


class ValidationTests(HomeOverviewFixtureMixin, TenantTestCase):
    """判据 ⑤：缺参 / 形状错 / `start > end` ⇒ **业务码校验错**（⛔ 不是 500）。"""

    def setUp(self):
        super().setUp()
        self.client = self._admin()

    def _expect_validation(self, params):
        status, body = self._get(self.client, **params)
        # 业务失败 = HTTP 200 + 业务码（`apps/core/exceptions.py` 的 ValidationError 分支）。
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIsNone(body["data"], body)
        return body["msg"]

    def test_both_params_are_required_and_the_message_names_the_missing_ones(self):
        msg = self._expect_validation({})
        self.assertIn("start", msg)
        self.assertIn("end", msg)

    def test_missing_end_names_only_end(self):
        msg = self._expect_validation({"start": "2026-09-10"})
        self.assertIn("end", msg)
        # ⚠️ 说真话：缺的是 end，不是「两个都缺」（两处文案相同会让排障失去方向）。
        self.assertNotIn("start", msg)

    def test_missing_start_names_only_start(self):
        msg = self._expect_validation({"end": "2026-09-10"})
        self.assertIn("start", msg)
        self.assertNotIn("end", msg)

    def test_malformed_shape_is_rejected_with_the_parameter_name(self):
        msg = self._expect_validation({"start": "garbage", "end": "2026-09-10"})
        self.assertIn("start", msg)
        self.assertIn("yyyy-MM-dd", msg)

    def test_malformed_end_is_rejected_with_the_parameter_name(self):
        """⭐ 独立评审 2026-10-04 补的缺口：坏的是 **`end`** 那一侧（原用例只打了坏 `start`）。

        ⚠️ 两个字段各有一条 `error_messages["invalid"]`，只测一侧 ⇒ 另一侧的分支永远不红。
        """
        msg = self._expect_validation({"start": "2026-09-10", "end": "garbage"})
        self.assertIn("end", msg)
        self.assertIn("yyyy-MM-dd", msg)

    def test_datetime_with_time_part_is_rejected(self):
        """`yyyy-MM-dd` 是**唯一**形状：带时分秒、紧凑数字串都要被拒（⛔ 不是 500）。"""
        for bad in ("2026-09-10 00:00:00", "20260910", "2026/09/10"):
            msg = self._expect_validation({"start": bad, "end": "2026-09-12"})
            self.assertIn("start", msg)

    def test_start_after_end_is_rejected(self):
        msg = self._expect_validation({"start": "2026-09-12", "end": "2026-09-10"})
        self.assertIn("start", msg)
        self.assertIn("end", msg)

    def test_equal_dates_pass(self):
        """⚠️ 边界值：`start == end` 是**合法**的（只有严格晚于才拒）。"""
        status, body = self._get(self.client, start="2026-09-10", end="2026-09-10")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.OK, body)
        self.assertEqual(body["data"]["start"], "2026-09-10")
        self.assertEqual(body["data"]["end"], "2026-09-10")

    def test_unknown_and_malformed_extra_params_do_not_500(self):
        """多余的畸形参数被忽略（不改判据、也不许 500）——`test_no_route_500` 那条腿的语料形状。"""
        status, body = self._get(
            self.client,
            start="2026-09-10",
            end="2026-09-10",
            page="abc",
            game="abc",
            status="BOGUS",
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], ErrorCode.OK, body)

    def test_empty_values_are_treated_as_missing(self):
        """空串 = 「没选」（前端清空日期框会送 `?start=`）⇒ 走**缺参**那一支，不是 500。"""
        msg = self._expect_validation({"start": "", "end": "2026-09-10"})
        self.assertIn("start", msg)
