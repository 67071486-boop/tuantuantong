"""页面一「客服接发单统计」端点（`GET /api/business/support-stats`）的判别力用例。

契约：`docs/specs/2026-10-17-support-stats-and-business-report-design.md` §2 / §6.1。

核心口径（每条都有下面的用例钉住）：
- 接单价 = `Order.total_price`；发单价 = `Order.hall_price`（空按 0）；预计绩效 = 两者相减；
- 操作人员 = `Order.sales_user` 当前显示名；空回落 `sales_agent` 文本；再空归「未指定」；
- 店铺 = `Order.source` 文本快照；空归「未指定」；
- 窗口 = `Order.created_at` 本地日期含两端；**不排除已退单**；排除软删；
- 数据权限 = 查看者的游戏权限（管理员短路）。

⚠️ 夹具一律合成值（E-0077）；断言打**字面量**（E-0009 / E-0024）。
"""
from datetime import datetime
from decimal import Decimal

from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order, OrderStatus
from tests.p5_auth_helpers import make_logged_in_client

NODE = "finance.support_statistics"
URL = "/api/business/support-stats"
DOMAIN = "tenant.test.com"


def _at(day: int, hour: int = 12, month: int = 10) -> datetime:
    """2026 年 `month` 月 `day` 日（`Asia/Shanghai`，`USE_TZ=True`）。"""
    return timezone.make_aware(datetime(2026, month, day, hour, 0))


class SupportStatsTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="原神")
        self.other_game = Game.objects.create(name="崩坏：星穹铁道")
        self.kefu_a = self._user("kefu-a", first_name="客服甲")
        self.kefu_b = self._user("kefu-b", first_name="客服乙")
        self.client = make_logged_in_client(
            domain=DOMAIN,
            username="stats-viewer",
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
        source: str = "",
        sales_user=None,
        sales_agent: str = "",
        status: str = OrderStatus.UNASSIGNED,
        created_at: datetime | None = None,
        game=None,
        deleted: bool = False,
    ) -> Order:
        self._seq += 1
        order = Order.objects.create(
            order_no=f"20261017{self._seq:012d}",
            game=game or self.game,
            total_price=Decimal(total),
            hall_price=None if hall is None else Decimal(hall),
            source=source,
            sales_user=sales_user,
            sales_agent=sales_agent,
            status=status,
        )
        # ⚠️ **默认也钉成 2026-10-10**（不是 `auto_now_add` 的「今天」）：下面 8 条用例都把窗口
        # 写成 2026-10-01~10-31；不钉死的话它们只在 2026-10 当月绿、2026-11-01 起必红
        #。
        Order.objects.filter(pk=order.pk).update(
            created_at=created_at if created_at is not None else _at(10)
        )
        if deleted:
            Order.objects.filter(pk=order.pk).update(deleted_at=_at(10))
        return order

    def _get(self, client: Client | None = None, **params):
        return (client or self.client).get(URL, params)

    # ── 口径 ────────────────────────────────────────────────────────────

    def test_operator_row_uses_total_price_and_hall_price(self):
        """一行 = 接单价合计 / 发单价合计 / 预计绩效 = 前两者相减（含空 hall_price 按 0）。"""
        self._order(total="100.00", hall="60.00", sales_user=self.kefu_a)
        self._order(total="50.00", hall=None, sales_user=self.kefu_a)
        self._order(total="30.00", hall="10.00", sales_user=self.kefu_b)

        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        self.assertEqual(body["code"], 0)
        operators = {row["name"]: row for row in body["data"]["operators"]}
        self.assertEqual(
            operators["客服甲"],
            {
                "name": "客服甲",
                "order_count": 2,
                "total_price": "150.00",
                "total_hall_price": "60.00",
                "performance": "90.00",
            },
        )
        self.assertEqual(operators["客服乙"]["total_price"], "30.00")
        self.assertEqual(body["data"]["operators_total"], {
            "name": "合计",
            "order_count": 3,
            "total_price": "180.00",
            "total_hall_price": "70.00",
            "performance": "110.00",
        })

    def test_zero_and_negative_amounts_participate(self):
        """金额 0 与负数都是合法值（D-0015），⛔ 不当成「没填」。"""
        self._order(total="0.00", hall="0.00", sales_user=self.kefu_a)
        self._order(total="-20.00", hall="5.00", sales_user=self.kefu_a)
        row = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()["data"]["operators"][0]
        self.assertEqual(row["total_price"], "-20.00")
        self.assertEqual(row["total_hall_price"], "5.00")
        self.assertEqual(row["performance"], "-25.00")

    def test_sales_user_empty_falls_back_to_sales_agent_text(self):
        """`sales_user` 为空 ⇒ 用 `sales_agent` 文本；文本也空 ⇒「未指定」。"""
        self._order(total="30.00", hall="10.00", sales_agent="老客服")
        self._order(total="5.00", hall="2.00", sales_agent="")
        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        rows = {row["name"]: row for row in body["data"]["operators"]}
        self.assertEqual(set(rows), {"老客服", "未指定"})
        self.assertEqual(rows["老客服"]["order_count"], 1)
        self.assertEqual(rows["未指定"]["total_price"], "5.00")

    def test_same_display_name_is_not_merged_across_keys(self):
        """一个真实用户与一条同名文本快照**不合并**（主键与文本是两种 key）。"""
        self._order(total="10.00", sales_user=self.kefu_a)  # 显示名「客服甲」
        self._order(total="20.00", sales_agent="客服甲")  # 文本快照同名
        rows = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()["data"]["operators"]
        self.assertEqual([row["name"] for row in rows], ["客服甲", "客服甲"])
        self.assertEqual(sorted(row["total_price"] for row in rows), ["10.00", "20.00"])

    def test_source_groups_by_text_snapshot_and_empty_is_unspecified(self):
        """店铺按 `source` 文本快照分组；空串归「未指定」。"""
        self._order(total="100.00", hall="40.00", source="淘宝店")
        self._order(total="50.00", hall="30.00", source="淘宝店")
        self._order(total="20.00", hall="5.00", source="")
        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        sources = {row["name"]: row for row in body["data"]["sources"]}
        self.assertEqual(sources["淘宝店"], {
            "name": "淘宝店",
            "order_count": 2,
            "total_price": "150.00",
            "total_hall_price": "70.00",
        })
        self.assertEqual(sources["未指定"]["order_count"], 1)
        self.assertEqual(body["data"]["sources_total"]["total_price"], "170.00")

    def test_sales_agent_trailing_space_does_not_overwrite_another_group(self):
        """`"客服甲"` 与 `"客服甲 "` 是两个 key：⛔ 不许后一行覆盖前一行（I5）。"""
        self._order(total="10.00", sales_agent="客服甲")
        self._order(total="20.00", sales_agent="客服甲 ")
        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        rows = body["data"]["operators"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["total_price"] for row in rows), ["10.00", "20.00"])
        self.assertEqual(body["data"]["operators_total"]["total_price"], "30.00")

    # ── 窗口 / 排除 ─────────────────────────────────────────────────────

    def test_date_window_includes_both_ends_and_excludes_outside(self):
        self._order(total="1.00", created_at=_at(1))
        self._order(total="2.00", created_at=_at(31))
        self._order(total="4.00", created_at=_at(30, month=9))
        self._order(total="8.00", created_at=_at(1, month=11))
        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        self.assertEqual(body["data"]["operators_total"]["total_price"], "3.00")

    def test_refunded_orders_are_included_on_page_one(self):
        """页面一**不排除已退单**（业务方只要 5 个字段；预计绩效不是净利润）。"""
        self._order(total="100.00", hall="50.00", status=OrderStatus.REFUNDED)
        row = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()["data"]["operators"][0]
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["performance"], "50.00")

    def test_soft_deleted_orders_are_excluded(self):
        self._order(total="100.00")
        self._order(total="999.00", deleted=True)
        body = self._get(
            date_from="2026-10-01", date_to="2026-10-31"
        ).json()
        self.assertEqual(body["data"]["operators_total"]["total_price"], "100.00")

    # ── 参数 / 权限 / 数据权限 ──────────────────────────────────────────

    def test_reversed_range_returns_1000(self):
        body = self._get(date_from="2026-10-31", date_to="2026-10-01").json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("开始日期不能晚于结束日期", body["msg"])

    def test_malformed_date_returns_1000(self):
        body = self._get(date_from="2026/10/01", date_to="2026-10-31").json()
        self.assertEqual(body["code"], 1000)

    def test_missing_dates_default_to_last_30_days(self):
        """不传日期 ⇒ 后端默认最近 30 天（不是「全部时间」）。"""
        self._order(total="10.00", created_at=timezone.now())
        body = self._get().json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["operators_total"]["total_price"], "10.00")
        self.assertEqual(len(body["data"]["range"]["date_from"]), 10)

    def test_viewer_without_node_gets_403(self):
        outsider = make_logged_in_client(
            domain=DOMAIN,
            username="stats-outsider",
            games=[self.game, self.other_game],
        )
        response = outsider.get(URL, {"date_from": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(response.status_code, 403)

    def test_game_scope_hides_orders_from_other_games(self):
        """只有 A 游戏权限的查看者看不到 B 游戏的单；管理员不受影响。"""
        self._order(total="10.00", game=self.game)
        self._order(total="999.00", game=self.other_game)

        scoped = make_logged_in_client(
            domain=DOMAIN,
            username="stats-one-game",
            groups=("客服",),
            nodes=(NODE,),
            games=[self.game],
        )
        body = scoped.get(URL, {"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
        self.assertEqual(body["data"]["operators_total"]["total_price"], "10.00")

        admin = make_logged_in_client(
            domain=DOMAIN,
            username="stats-admin",
            groups=("客服",),
            nodes=(NODE,),
            is_superuser=True,
            games=[self.game, self.other_game],
        )
        body = admin.get(URL, {"date_from": "2026-10-01", "date_to": "2026-10-31"}).json()
        self.assertEqual(body["data"]["operators_total"]["total_price"], "1009.00")
