"""页面二「经营报表」端点（`GET /api/business/business-report`）的判别力用例。

契约：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` §2 / §5 / §6.2。

核心口径（每条都有下面的用例钉住）：
- 利润 = 接单价 − 发单价 − 退款；**6 卡与 4 图排除已退单**；
- 接单时间 = 主绩效行 `claimed_at`；交单时间 = `submitted_at`；
- 6 个窗口：筛选窗 / 近 30 天 / 上月 / 近 90 天 / 近 365 天 / 筛选窗退款；
- 趋势 = 最近 12 个自然月（含本月），环比 `(本月−上月)/上月×100`；
- 数据权限 = 查看者游戏权限；两个端点共用 `finance.support_statistics`。

⚠️ 夹具一律合成值；`_today` 用 patch 钉成 滚动窗口依赖它）。
"""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from tests.p5_auth_helpers import make_logged_in_client

NODE = "finance.support_statistics"
URL = "/api/business/business-report"
DOMAIN = "tenant.test.com"
TODAY = date(2026, 10, 17)


def _at(day: int, hour: int = 12, month: int = 10, year: int = 2026) -> datetime:
    return timezone.make_aware(datetime(year, month, day, hour, 0))


class BusinessReportTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="原神")
        self.other_game = Game.objects.create(name="崩坏：星穹铁道")
        self.kefu_a = self._user("kefu-a", first_name="客服甲")
        self.kefu_b = self._user("kefu-b", first_name="客服乙")
        self.client = make_logged_in_client(
            domain=DOMAIN,
            username="report-viewer",
            groups=("客服",),
            nodes=(NODE,),
            games=[self.game, self.other_game],
        )
        self._seq = 0

    def _user(self, username: str, *, first_name: str = ""):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username=username)
        if first_name:
            user.first_name = first_name
            user.save(update_fields=["first_name"])
        return user

    def _order(
        self,
        *,
        total: str = "0.00",
        hall: str | None = None,
        refund: str = "0.00",
        source: str = "",
        sales_user=None,
        sales_agent: str = "",
        status: str = OrderStatus.COMPLETED,
        claimed_at: datetime | None = None,
        submitted_at: datetime | None = None,
        game=None,
        deleted: bool = False,
    ) -> Order:
        self._seq += 1
        order = Order.objects.create(
            order_no=f"20261018{self._seq:012d}",
            game=game or self.game,
            total_price=Decimal(total),
            hall_price=None if hall is None else Decimal(hall),
            refund_fee=Decimal(refund),
            source=source,
            sales_user=sales_user,
            sales_agent=sales_agent,
            status=status,
            submitted_at=submitted_at,
        )
        if claimed_at is not None:
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, claimed_at=claimed_at
            )
        if deleted:
            Order.objects.filter(pk=order.pk).update(deleted_at=_at(10))
        return order

    def _report(self, **params):
        with patch("apps.business.report_stats._today", return_value=TODAY):
            response = self.client.get(URL, params)
        return response.json()

    # ── 利润 / 退款口径 ─────────────────────────────────────────────────

    def test_profit_subtracts_hall_and_refund(self):
        self._order(
            total="100.00", hall="60.00", refund="10.00",
            claimed_at=_at(5),
        )
        body = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["cards"]["search_profit"], "30.00")
        self.assertEqual(body["data"]["cards"]["search_refund"], "10.00")

    def test_null_hall_price_counts_as_zero(self):
        self._order(total="80.00", hall=None, claimed_at=_at(5))
        body = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )
        self.assertEqual(body["data"]["cards"]["search_profit"], "80.00")

    def test_refunded_orders_are_excluded_from_cards_and_charts(self):
        """已退单不进利润 / 来源 / 客服金额；只进「退单量」与它自己的退款额（→ 也不进退款卡）。"""
        self._order(
            total="100.00", hall="60.00", refund="0.00",
            source="淘宝店", sales_user=self.kefu_a, claimed_at=_at(5),
        )
        self._order(
            total="50.00", hall="10.00", refund="5.00",
            source="淘宝店", sales_user=self.kefu_a,
            status=OrderStatus.REFUNDED, claimed_at=_at(6),
        )
        body = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]
        self.assertEqual(body["cards"]["search_profit"], "40.00")
        # 退款卡同样「不含已退单状态」⇒ 已退单单上的 5 元不计。
        self.assertEqual(body["cards"]["search_refund"], "0.00")
        self.assertEqual(body["source_distribution"], [
            {"name": "淘宝店", "amount": "100.00", "profit": "40.00"}
        ])
        self.assertEqual(body["support_stats"]["order_counts"], [1])
        self.assertEqual(body["support_stats"]["amounts"], ["100.00"])
        self.assertEqual(body["support_stats"]["refund_counts"], [1])

    # ── 时间口径 ───────────────────────────────────────────────────────

    def test_claim_and_submit_use_different_columns(self):
        """同一张单：接单时间在窗口内、交单时间在窗口外（反过来另一张）⇒ 两种口径各算各的。"""
        self._order(
            total="100.00", hall="60.00",
            claimed_at=_at(5, month=10), submitted_at=_at(20, month=9),
        )
        self._order(
            total="100.00", hall="70.00",
            claimed_at=_at(20, month=9), submitted_at=_at(5, month=10),
        )
        claim = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]
        submit = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="submit"
        )["data"]
        self.assertEqual(claim["cards"]["search_profit"], "40.00")
        self.assertEqual(submit["cards"]["search_profit"], "30.00")

    def test_rolling_windows(self):
        """6 个窗口各自取对了区间（today = 2026-10-17）。"""
        self._order(total="10.00", claimed_at=_at(5))                 # 筛选窗 + 近30 + 近90 + 近365
        self._order(total="20.00", claimed_at=_at(25, month=9))       # 近30（9/18 起）+ 近90 + 近365
        self._order(total="30.00", claimed_at=_at(5, month=9))        # 上月 + 近90 + 近365
        self._order(total="40.00", claimed_at=_at(1, month=8))        # 近90 + 近365
        self._order(total="50.00", claimed_at=_at(1, month=12, year=2025))  # 近365
        self._order(total="60.00", claimed_at=_at(1, month=1, year=2025))   # 都不在
        cards = self._report(
            date_from="2026-10-01", date_to="2026-10-10", stat_type="claim"
        )["data"]["cards"]
        self.assertEqual(cards["search_profit"], "10.00")
        self.assertEqual(cards["last_month_profit"], "30.00")
        self.assertEqual(cards["prev_month_profit"], "50.00")
        self.assertEqual(cards["three_month_profit"], "100.00")
        self.assertEqual(cards["year_profit"], "150.00")

    # ── 来源 / 客服 / 趋势 ─────────────────────────────────────────────

    def test_source_distribution_and_total(self):
        self._order(total="100.00", hall="40.00", source="淘宝店", claimed_at=_at(5))
        self._order(total="50.00", hall="30.00", source="淘宝店", claimed_at=_at(6))
        self._order(total="20.00", hall="5.00", source="", claimed_at=_at(7))
        data = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]
        self.assertEqual(data["source_distribution"], [
            {"name": "淘宝店", "amount": "150.00", "profit": "80.00"},
            {"name": "未指定", "amount": "20.00", "profit": "15.00"},
        ])
        self.assertEqual(data["source_total"], {"amount": "170.00", "profit": "95.00"})

    def test_support_stats_grouping_and_refund_count(self):
        self._order(total="100.00", hall="60.00", sales_user=self.kefu_a, claimed_at=_at(5))
        self._order(total="40.00", hall="10.00", sales_user=self.kefu_b, claimed_at=_at(6))
        self._order(
            total="30.00", hall="5.00", sales_user=self.kefu_b,
            status=OrderStatus.REFUNDED, claimed_at=_at(7),
        )
        stats = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]["support_stats"]
        index = {name: i for i, name in enumerate(stats["names"])}
        self.assertEqual(set(index), {"客服甲", "客服乙"})
        self.assertEqual(stats["order_counts"][index["客服甲"]], 1)
        self.assertEqual(stats["amounts"][index["客服甲"]], "100.00")
        self.assertEqual(stats["refund_counts"][index["客服甲"]], 0)
        self.assertEqual(stats["order_counts"][index["客服乙"]], 1)
        self.assertEqual(stats["amounts"][index["客服乙"]], "40.00")
        self.assertEqual(stats["refund_counts"][index["客服乙"]], 1)

    def test_trend_is_12_continuous_months_with_growth(self):
        self._order(total="100.00", claimed_at=_at(5, month=9))
        self._order(total="150.00", claimed_at=_at(5, month=10))
        trend = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]["trend"]
        self.assertEqual(len(trend["months"]), 12)
        self.assertEqual(trend["months"][0], "2025-11")
        self.assertEqual(trend["months"][-1], "2026-10")
        sep = trend["months"].index("2026-09")
        oct_ = trend["months"].index("2026-10")
        self.assertEqual(trend["order_counts"][sep], 1)
        self.assertEqual(trend["amounts"][sep], "100.00")
        self.assertEqual(trend["order_counts"][oct_], 1)
        self.assertEqual(trend["amounts"][oct_], "150.00")
        self.assertEqual(trend["count_growth"][oct_], "0.00")
        self.assertEqual(trend["amount_growth"][oct_], "50.00")
        self.assertEqual(trend["count_growth"][0], "0.00")

    # ── 筛选 / 参数 / 权限 ─────────────────────────────────────────────

    def test_kefu_filter_narrows_data_but_options_keep_everyone(self):
        self._order(total="100.00", sales_user=self.kefu_a, claimed_at=_at(5))
        self._order(total="40.00", sales_user=self.kefu_b, claimed_at=_at(6))
        data = self._report(
            date_from="2026-10-01", date_to="2026-10-31",
            stat_type="claim", kefu=str(self.kefu_a.id),
        )["data"]
        self.assertEqual(data["support_stats"]["names"], ["客服甲"])
        self.assertEqual(
            [option["name"] for option in data["kefu_options"]],
            ["客服乙", "客服甲"],
        )

    def test_game_filter_and_unknown_game(self):
        self._order(total="100.00", game=self.game, claimed_at=_at(5))
        self._order(total="40.00", game=self.other_game, claimed_at=_at(6))
        data = self._report(
            date_from="2026-10-01", date_to="2026-10-31",
            stat_type="claim", game=str(self.game.id),
        )["data"]
        self.assertEqual(data["cards"]["search_profit"], "100.00")
        bad = self._report(
            date_from="2026-10-01", date_to="2026-10-31",
            stat_type="claim", game="999999",
        )
        self.assertEqual(bad["code"], 1000)
        self.assertIn("游戏不存在", bad["msg"])

    def test_soft_deleted_orders_are_excluded(self):
        self._order(total="100.00", claimed_at=_at(5))
        self._order(total="999.00", claimed_at=_at(6), deleted=True)
        data = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]
        self.assertEqual(data["cards"]["search_profit"], "100.00")

    def test_hall_shadow_orders_are_excluded(self):
        """⭐ 二轮评审 Important 5：镜像卡派生的**隐藏单**不进经营报表基集。

        隐藏单的 `total_price` 是对端发单价、不是本租户的收入；算进来会虚增利润 /
        客服统计。⚠️ 它**带一条 MAIN 行且 `claimed_at` 落在窗口内**——这是判据的
        承重部分：没有 MAIN 行时它本来就落不进「接单时间」窗口，那条用例对
        `_base_orders` 的排除**没有判别力**（删掉排除也会绿）。
        """
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        self._order(total="100.00", hall=None, claimed_at=_at(5))   # 正对照：利润 100
        card = make_mirror(
            game=self.game, bound_by=self.kefu_a, no="SYN-SHADOW-REPORT"
        )
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261028000000000001",
            total="999.00", price="888.00", with_main=True,
        )
        # 让它的 MAIN 行也落进 `stat_type=claim` 的窗口（否则本条测不到 `_base_orders`）。
        Performance.objects.filter(
            order=hidden, emp_type=EmpType.MAIN
        ).update(claimed_at=_at(5))

        data = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="claim"
        )["data"]

        self.assertEqual(
            data["cards"]["search_profit"], "100.00",
            "隐藏单的利润（999−888=111）混进了经营报表",
        )

    def test_bad_params_return_1000(self):
        reversed_ = self._report(
            date_from="2026-10-31", date_to="2026-10-01", stat_type="claim"
        )
        self.assertEqual(reversed_["code"], 1000)
        bad_type = self._report(
            date_from="2026-10-01", date_to="2026-10-31", stat_type="bad"
        )
        self.assertEqual(bad_type["code"], 1000)
        bad_kefu = self._report(
            date_from="2026-10-01", date_to="2026-10-31",
            stat_type="claim", kefu="abc",
        )
        self.assertEqual(bad_kefu["code"], 1000)

    def test_viewer_without_node_gets_403(self):
        outsider = make_logged_in_client(
            domain=DOMAIN,
            username="report-outsider",
            games=[self.game, self.other_game],
        )
        response = outsider.get(
            URL,
            {"date_from": "2026-10-01", "date_to": "2026-10-31", "stat_type": "claim"},
        )
        self.assertEqual(response.status_code, 403)

    def test_game_scope_hides_orders_from_other_games(self):
        self._order(total="100.00", game=self.game, claimed_at=_at(5))
        self._order(total="999.00", game=self.other_game, claimed_at=_at(6))
        scoped = make_logged_in_client(
            domain=DOMAIN,
            username="report-one-game",
            groups=("客服",),
            nodes=(NODE,),
            games=[self.game],
        )
        with patch("apps.business.report_stats._today", return_value=TODAY):
            body = scoped.get(
                URL,
                {"date_from": "2026-10-01", "date_to": "2026-10-31", "stat_type": "claim"},
            ).json()
        self.assertEqual(body["data"]["cards"]["search_profit"], "100.00")

    def test_missing_dates_default_to_last_30_days(self):
        """不传日期 ⇒ 后端默认最近 30 天；`report_views` 与 `report_stats` 共用同一个 `_today`。"""
        self._order(total="100.00", claimed_at=_at(16))
        with patch("apps.business.report_stats._today", return_value=TODAY):
            body = self.client.get(URL, {"stat_type": "claim"}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["range"]["date_from"], "2026-09-18")
        self.assertEqual(body["data"]["range"]["date_to"], "2026-10-17")
        self.assertEqual(body["data"]["cards"]["search_profit"], "100.00")

    def test_anonymous_request_gets_401(self):
        """未登录 401（与「有登录但无节点」的 403 分开）。"""
        anonymous = Client(SERVER_NAME=DOMAIN)
        response = anonymous.get(
            URL,
            {"date_from": "2026-10-01", "date_to": "2026-10-31", "stat_type": "claim"},
        )
        self.assertEqual(response.status_code, 401)
