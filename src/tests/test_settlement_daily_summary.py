"""D-0319（2026-10-28）：`GET /api/business/settlements/daily-summary` 的测试网。

计划：`docs/plans/2026-10-28-settlement-daily-view.md` §3。本端点是「结算记录页」主视图
（按天聚合）的唯一后端新面；明细仍走既有 `/settlements`（那边的行为由
`tests/test_settlement_api.py` 守着，本文件**不重复**）。

网与判据（每条都有会红的用例）：

1. **聚合正确性**：同天多对象多行 ⇒ 一行；`order_count` 是 **distinct 订单数**
   （一单两绩效行计 1）；`total_amount` = Σ `settled_amount`（两位小数字符串）；
   未结账行不入聚合；跨天不混。
2. **时区钉子（UTC+8 归天）**：UTC 15:59 / 16:00 两条边界（= +8 的 23:59 / 次日 00:00）
   ——钉「按 +8 归天」，不是按 UTC 归天。
3. **越权与范围**：无 `finance.payroll` ⇒ 403（照 `SettlementReadPermissionTests` 形态，
   含「持别的节点仍 403」与「匿名 401」）；`scope_queryset` 接线（照 `ScopingWiringTests`
   形态：桩打在返回值上 + 不接桩的对照）与按游戏收窄（照 `PerformanceScopeTests` 形态）。
4. **跨租户否定断言**：以租户 A 的凭据访问 B 的聚合必须失败 ⇒ 判据用**租户内唯一、
   且两租户取值不同**的字段造数（本网用 `settled_at` 的**日期**：A 只有 10-10、B 只有 10-11）。
5. **分页与时间范围筛选**各一条；畸形参数 ⇒ 200 + 业务码（E-0010，不是 HTML 500）。
6. **软删排除**（D-0144 / R-6 口径，与 `/settlements` 同一条）：已软删单的行 / 被软删的
   绩效行都不进聚合——每条都先证「删之前它在」再断「删之后它不在」（E-0004 的假通过形态）。
7. **N3（W-01）半开区间边界对拍**（`DailySargableWindowBoundaryTests`）：`date_from`/`date_to`
   筛已从 `settled_at__date__gte/lte` 改写为裸列半开区间——亚秒级边界时刻（恰 00:00:00.000 /
   23:59:59.9 / 次日 00:00:00.1）新旧形态同库同值 + SQL 形状钉（裸列比较、无套函数比较、
   `TruncDate` 投影恰好一处）。

⚠️ 期望值一律写**字面值**（E-0024）：金额写 `"30.00"`、日期写 `"2026-10-28"`、
状态写 `"已结账"`——不引被测模块导出的常量（自指断言不会红）。
⚠️ 夹具值一律**合成**（E-0077）。
"""
import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client, RequestFactory
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.api.settlement_views import (
    SettlementDailySummaryView,
    _local_day_window,
)
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本端点（**没有**尾斜杠，与 `/settlements`、`/settlements/people` 同形）。
DAILY_URL = "/api/business/settlements/daily-summary"
#: 与 `/settlements` 同一颗读节点（`finance.payroll`）。
NODE_CODE = "finance.payroll"
#: Django 5 移除了 `django.utils.timezone.utc`，用标准库的 UTC。
UTC = dt_timezone.utc


def _at_utc(year, month, day, hour, minute):
    """固定的 UTC 时刻（不依赖 `now`——时区边界用例必须钉死钟点）。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


#: 业务时区（本仓 `TIME_ZONE`，恒定 UTC+8、无夏令时）。
SHANGHAI = dt_timezone(timedelta(hours=8))


def _at_shanghai(year, month, day, hour, minute, second=0, microsecond=0):
    """北京时间下的固定时刻（转成 UTC 存库）——能表达秒与亚秒的边界（23:59:59.9）。"""
    local = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=SHANGHAI)
    return local.astimezone(UTC)


class _DailyFixture(TenantTestCase):
    """造「已结账绩效行」的小夹具（各用例只表达自己的差异）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 业务逻辑用例用管理员：只为跳过建角色目录（闸门由 PermissionTests 用非管理员钉）。
        self.finance = make_logged_in_client(username="daily-fin", is_superuser=True)
        self.taker_a = User.objects.create_user(username="daily-taker-a")
        self.taker_b = User.objects.create_user(username="daily-taker-b")
        # ⚠️ 同一张单上造多行时还要避开 `uniq_performance_order_user`（一单一人一行）
        # ⇒ 需要第三个人的场合用他。
        self.taker_c = User.objects.create_user(username="daily-taker-c")
        self._order_seq = 0

    def _order(self, *, game=None):
        self._order_seq += 1
        return Order.objects.create(
            game=game or self.game,
            order_no=f"2026102800000{self._order_seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )

    def _perf(self, order, user=None, **overrides):
        """一条**已结账**的绩效行（默认结在 2026-10-28 本地晚间、快照 20.00）。"""
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.SETTLED,
            # 本地 2026-10-28 21:00（= UTC 13:00，离两条 UTC 边界都远，不会被误伤）。
            "settled_at": _at_utc(2026, 10, 28, 13, 0),
            "settled_amount": Decimal("20.00"),
            "settled_by": "daily-fin",
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _rows(self, params=None, client=None):
        body = (client or self.finance).get(DAILY_URL, params or {}).json()
        # 前置哨兵：端点本身成功（失败用例不走这个 helper）。
        self.assertEqual(body["code"], 0, body)
        return body["data"]


class DailyAggregationTests(_DailyFixture):
    """聚合口径：一天一行 / distinct 订单数 / Σ 快照 / 未结账不入 / 跨天不混。"""

    def test_same_day_rows_merge_into_one_line(self):
        """同天两个结算对象、三行（其中两行挂同一单）⇒ 一行；单数 = 2、金额 = 三行之和。"""
        order_shared = self._order()          # 这一张单挂两条绩效行
        order_solo = self._order()
        # ⚠️ `uniq_order_main_taker`：一单只允许一条「打单工资」行 ⇒ 同单的第二行用
        #    「销售提成」（真实形状也是这样：一单多绩效行 = 打单工资 + 销售提成）。
        self._perf(order_shared, self.taker_a, settled_amount=Decimal("12.34"))
        self._perf(
            order_shared, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("0.66"),
        )
        self._perf(order_solo, self.taker_a, settled_amount=Decimal("7.00"))

        data = self._rows()

        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        row = data["items"][0]
        # 字面值（E-0024）：一天一行、distinct 订单数、快照之和（12.34 + 0.66 + 7.00）。
        self.assertEqual(row["date"], "2026-10-28")
        self.assertEqual(row["order_count"], 2)
        self.assertEqual(row["total_amount"], "20.00")

    def test_order_count_is_distinct_orders_not_row_count(self):
        """⭐ 判别力用例：一张单两条已结账行 ⇒ `order_count` = 1（不是 2）。

        把 `Count("order_id", distinct=True)` 的 `distinct` 拿掉 ⇒ 本用例当场红。
        """
        order = self._order()
        self._perf(order, self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(
            order, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("5.00"),
        )

        row = self._rows()["items"][0]

        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["total_amount"], "15.00")

    def test_negative_and_zero_amounts_sum_as_is(self):
        """负数与 0 都是合法值（D-0015）：照实相加，不截断、不置 0。"""
        order = self._order()
        self._perf(order, self.taker_a, settled_amount=Decimal("-15.00"))
        self._perf(
            order, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("0.00"),
        )

        row = self._rows()["items"][0]

        self.assertEqual(row["total_amount"], "-15.00")

    def test_cross_day_rows_do_not_mix(self):
        """两天 ⇒ 两行，且最新的一天排在前（`-day` 排序）。"""
        self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, 27, 13, 0))
        self._perf(self._order(), self.taker_b, settled_at=_at_utc(2026, 10, 28, 13, 0))

        data = self._rows()

        self.assertEqual([row["date"] for row in data["items"]], ["2026-10-28", "2026-10-27"])

    def test_unsettled_rows_are_not_aggregated(self):
        """未结账行不入聚合（计划 §5：工作面仍在绩效管理页的清算弹层）。

        两半都打在**同一张单**上：已结账那行在、未结账那行不贡献金额也不贡献行数。
        顺带钉「`settle_status=已结账` 但 `settled_at` 为空」的存量形状同样不入
        （归不进任何一天）。
        """
        order = self._order()
        settled = self._perf(order, self.taker_a, settled_amount=Decimal("9.00"))
        # 未结账：状态列 + 时间列都没写。
        Performance.objects.create(
            order=order,
            user=self.taker_b,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )
        # 存量形状：状态已是「已结账」、但时间列为空（P4-FE-0 之前结的账）。
        # ⚠️ 同一张单不能有第二条「打单工资」行（`uniq_order_main_taker`）、也不能
        #    同一人两行（`uniq_performance_order_user`）⇒ 第三个人 + 销售提成。
        Performance.objects.create(
            order=order,
            user=self.taker_c,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("7.00"),
            bonus_amount=Decimal("7.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.SETTLED,
            settled_amount=Decimal("7.00"),
        )

        data = self._rows()

        # 前置哨兵：三行都在库里（不是「没造出来」）。
        self.assertEqual(Performance.objects.filter(order=order).count(), 3)
        self.assertEqual(data["total"], 1)
        row = data["items"][0]
        # 只有 `settled` 那一行进聚合：金额 9.00、订单数 1（本就一张单）。
        self.assertEqual(row["total_amount"], "9.00")
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["date"], "2026-10-28")


class DailyTimezoneTests(_DailyFixture):
    """⭐ 时区钉子：`settled_at` 按 **UTC+8** 的日期归天（D-0302 收口口径）。

    两条边界（UTC 15:59 = +8 的 23:59；UTC 16:00 = +8 的次日 00:00）。
    实现用的是 `TruncDate("settled_at")` + 项目 `TIME_ZONE = Asia/Shanghai`
    （先例 `report_stats.py` 的 `TruncMonth`，同一机制）——本组用例把「按 +8 归天」
    钉成行为：改成按 UTC 归天 ⇒ 两条全红。
    """

    def test_utc_1559_falls_on_the_same_local_day(self):
        """UTC 2026-10-28 15:59（= 北京时间 23:59）⇒ 归到 2026-10-28（不是 10-28 的 UTC 日期巧合：
        若按 UTC 归天它也是 10-28 ⇒ 本条单独不红，红的是下面那条边界）。
        """
        order = self._order()
        self._perf(order, self.taker_a, settled_at=_at_utc(2026, 10, 28, 15, 59))

        row = self._rows()["items"][0]

        self.assertEqual(row["date"], "2026-10-28")

    def test_utc_1600_falls_on_the_next_local_day(self):
        """⭐ 判别力边界：UTC 2026-10-28 16:00（= 北京时间 10-29 00:00）⇒ 归到 **2026-10-29**。

        按 UTC 归天会得到 10-28 ⇒ 本条当场红——这就是「+8 归天」的钉子。
        """
        order = self._order()
        self._perf(order, self.taker_a, settled_at=_at_utc(2026, 10, 28, 16, 0))

        row = self._rows()["items"][0]

        self.assertEqual(row["date"], "2026-10-29")


class DailyFilterAndPaginationTests(_DailyFixture):
    """结账时间范围筛选 + 分页 + 畸形参数的入口纪律。"""

    def test_date_range_filters_days_on_both_ends(self):
        """`date_from` / `date_to`（本地日期、两端含）只收窄「天」这一级。"""
        self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, 20, 13, 0))
        self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, 25, 13, 0))
        self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, 30, 13, 0))

        # 双端含：20 与 30 都收进来。
        self.assertEqual(
            [r["date"] for r in self._rows({"date_from": "2026-10-20", "date_to": "2026-10-30"})["items"]],
            ["2026-10-30", "2026-10-25", "2026-10-20"],
        )
        # 单端：只从 25 起。
        self.assertEqual(
            [r["date"] for r in self._rows({"date_from": "2026-10-25"})["items"]],
            ["2026-10-30", "2026-10-25"],
        )
        # 单端：只到 25 止。
        self.assertEqual(
            [r["date"] for r in self._rows({"date_to": "2026-10-25"})["items"]],
            ["2026-10-25", "2026-10-20"],
        )
        # 窗口外为空（非 404、非 500）。
        self.assertEqual(self._rows({"date_from": "2026-11-01"})["items"], [])

    def test_pagination_follows_the_envelope_shape(self):
        """分页照 `EnvelopePagination`：`page_size` 生效、`total` 是天数、翻页拿到余下那天。"""
        for day in (20, 25, 30):
            self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, day, 13, 0))

        first = self._rows({"page": 1, "page_size": 2})
        self.assertEqual(first["total"], 3)
        self.assertEqual([r["date"] for r in first["items"]], ["2026-10-30", "2026-10-25"])
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["page_size"], 2)

        second = self._rows({"page": 2, "page_size": 2})
        self.assertEqual([r["date"] for r in second["items"]], ["2026-10-20"])

    def test_malformed_params_are_business_codes_not_500(self):
        """非法日期 / 非法页码 ⇒ 200 + 业务码（E-0010 的形状），不是 HTML 500。"""
        bad_date = self.finance.get(DAILY_URL, {"date_from": "garbage"})
        self.assertEqual(bad_date.status_code, 200)
        self.assertEqual(bad_date.json()["code"], 1000)

        # `?page=abc` 是 DRF 分页器的 `1002`（与 `SettlementListTests` 的实测口径一致）。
        bad_page = self.finance.get(DAILY_URL, {"page": "abc"})
        self.assertEqual(bad_page.status_code, 200)
        self.assertEqual(bad_page.json()["code"], 1002)

        # 空串按「没传」：正常信封，不是 1000。
        blank = self.finance.get(DAILY_URL, {"date_from": ""})
        self.assertEqual(blank.json()["code"], 0)


class DailySoftDeleteTests(_DailyFixture):
    """软删排除（D-0144 / R-6，与 `/settlements` 同一条口径）——先证「删之前在」再断「不在」。"""

    def test_rows_of_a_soft_deleted_order_leave_the_aggregate(self):
        order_dead = self._order()
        order_alive = self._order()
        self._perf(order_dead, self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(order_alive, self.taker_b, settled_amount=Decimal("5.00"))

        # 对照：删之前那张单贡献着自己的金额与订单数（10.00 / 2 张单）。
        row = self._rows()["items"][0]
        self.assertEqual((row["order_count"], row["total_amount"]), (2, "15.00"))

        Order.objects.filter(pk=order_dead.pk).update(deleted_at=_at_utc(2026, 10, 28, 12, 0))

        row = self._rows()["items"][0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "5.00"))

    def test_a_soft_deleted_performance_row_leaves_the_aggregate(self):
        """订单活着、单条绩效行被软删（R-6 形状）：它不贡献金额；同单的活行照旧计单。"""
        order = self._order()
        alive = self._perf(order, self.taker_a, settled_amount=Decimal("5.00"))
        # 同单第二行用销售提成（`uniq_order_main_taker` 只约束打单工资行）。
        dead = self._perf(
            order, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("7.00"),
        )

        row = self._rows()["items"][0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "12.00"))

        Performance.objects.filter(pk=dead.pk).update(deleted_at=_at_utc(2026, 10, 28, 12, 0))

        row = self._rows()["items"][0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "5.00"))


class DailyScopingWiringTests(_DailyFixture):
    """⭐ `scope_queryset` 接线**真的被用上**（照 `ScopingWiringTests` 的形态：
    桩打在**返回值**上（清空），配不接桩的对照——只断言「被调用」会漏掉「调用了却丢掉返回值」）。
    """

    def test_the_daily_summary_uses_the_scoping_result(self):
        self._perf(self._order(), self.taker_a)

        self.assertEqual(self._rows()["total"], 1)  # 对照：本来点得见

        with mock.patch(
            "apps.business.api.settlement_views.scope_queryset",
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._rows()["items"], [])

        self.assertTrue(seam.called, "按天汇总没有经过 scope_queryset 卡点")

    def test_the_daily_summary_narrows_to_the_viewers_games(self):
        """按游戏权限收窄（照 `PerformanceScopeTests` 形态）：只勾 A 的人只看到 A 的天数。

        判别力：抽掉 `get_queryset` 的 `scope_filter` ⇒ B 那天一起出现，本用例红。
        """
        game_a = Game.objects.create(name="聚合甲")
        game_b = Game.objects.create(name="聚合乙")
        self._perf(
            self._order(game=game_a), self.taker_a,
            settled_at=_at_utc(2026, 10, 20, 13, 0), settled_amount=Decimal("10.00"),
        )
        self._perf(
            self._order(game=game_b), self.taker_b,
            settled_at=_at_utc(2026, 10, 25, 13, 0), settled_amount=Decimal("99.00"),
        )
        a_only = make_logged_in_client(
            username="daily-a-only", groups=("客服",), nodes=(NODE_CODE,), games=[]
        )
        grant_games(User.objects.get(username="daily-a-only"), game_a)

        data = self._rows(client=a_only)

        # 前置哨兵：B 那行真的在库里（不是「没造出来」）。
        self.assertEqual(Performance.objects.count(), 2)
        self.assertEqual([r["date"] for r in data["items"]], ["2026-10-20"])
        self.assertEqual(data["items"][0]["total_amount"], "10.00")


class DailyPermissionTests(TenantTestCase):
    """闸门 = `has_node("finance.payroll")`（与 `/settlements` 同一颗），非管理员双向钉住。

    ⚠️ 必须用非管理员：`has_node_now` 对 `is_superuser` 短路，用管理员构造会恒放行。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _grant(self, username, *codes):
        nodes = [
            PermissionNode.objects.get_or_create(
                code=code,
                defaults={
                    "name": code,
                    "note": f"测试节点 {code}",
                    "kind": NodeKind.API,
                    "level": 1,
                },
            )[0]
            for code in codes
        ]
        role = Role.objects.create(
            name=f"角{username}"[:5], group=Group.objects.create(name=f"g-{username}"[:150])
        )
        role.permissions.set(nodes)
        User.objects.get(username=username).groups.add(role.group)

    def test_a_holder_can_read(self):
        # ⚠️ `nodes=` 只往用户**已有组**对应的 Role 上挂（`_grant_nodes` 遍历 `user.groups`）
        #    ⇒ 必须同时给一个组，否则节点挂不上、恒 403。照 `test_scope_performance` 的形态。
        client = make_logged_in_client(
            username="daily-holder", groups=("客服",), nodes=(NODE_CODE,)
        )

        response = client.get(DAILY_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_a_user_without_the_node_is_denied_with_a_permission_message(self):
        client = make_logged_in_client(username="daily-no-node")

        response = client.get(DAILY_URL)

        self.assertEqual(response.status_code, 403)
        self.assertIn("权限", response.json()["msg"])

    def test_a_user_holding_a_different_node_is_still_denied(self):
        """持 `order.list`（另一个真节点）仍 403——否则「有任意权限就放行」也能绿。"""
        client = make_logged_in_client(username="daily-wrong-node")
        self._grant("daily-wrong-node", "order.list")

        response = client.get(DAILY_URL)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有查看结算汇总的权限")

    def test_anonymous_is_401_not_403(self):
        anonymous = Client(SERVER_NAME="tenant.test.com")

        self.assertEqual(anonymous.get(DAILY_URL).status_code, 401)


class DailyCrossTenantTests(TenantTestCase):
    """跨租户否定断言（`AGENTS.md` §4：新增查询必须补「以 A 的凭据访问 B 必须失败」）。

    判据字段 = **聚合的日期**（租户内唯一——一天只有一行；两租户取值不同：
    A 只有 2026-10-10、B 只有 2026-10-11）。金额也造成两租户不同（10.00 / 90.00），
    双保险。
    """

    DATE_A = "2026-10-10"
    DATE_B = "2026-10-11"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="daily_iso_b", name="按天汇总隔离租户 B", domain="daily-iso-b.test.local"
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求不会在响应结束时还原 schema）。
        connection.set_tenant(self.tenant)

        # ── B 侧先建（try/finally 还原 schema）──
        try:
            with tenant_context(self.tenant_b):
                self.admin_b = make_logged_in_client(
                    domain="daily-iso-b.test.local", username="daily-iso-admin-b", is_superuser=True
                )
                game_b = Game.objects.create(name="乙游")
                order_b = Order.objects.create(
                    game=game_b,
                    order_no="20261011000000000002",
                    status=OrderStatus.COMPLETED,
                    hall_price=Decimal("100.00"),
                )
                taker_b = User.objects.create_user(username="daily-iso-taker-b")
                Performance.objects.create(
                    order=order_b,
                    user=taker_b,
                    emp_type=EmpType.MAIN,
                    temp_amount=Decimal("90.00"),
                    bonus_amount=Decimal("90.00"),
                    review_status=ReviewStatus.APPROVED,
                    settle_status=SettleStatus.SETTLED,
                    settled_at=_at_utc(2026, 10, 11, 13, 0),
                    settled_amount=Decimal("90.00"),
                )
        finally:
            connection.set_tenant(self.tenant)

        # ── A 侧 ──
        self.admin_a = make_logged_in_client(
            domain="tenant.test.com", username="daily-iso-admin-a", is_superuser=True
        )
        game_a = Game.objects.create(name="甲游")
        order_a = Order.objects.create(
            game=game_a,
            order_no="20261010000000000001",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )
        taker_a = User.objects.create_user(username="daily-iso-taker-a")
        Performance.objects.create(
            order=order_a,
            user=taker_a,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.SETTLED,
            settled_at=_at_utc(2026, 10, 10, 13, 0),
            settled_amount=Decimal("10.00"),
        )

    def _get_b(self, params=None):
        """在 B 的域名上用 B 的凭据发请求，结束后把连接钉回租户 A。"""
        try:
            return self.admin_b.get(DAILY_URL, params or {}, SERVER_NAME="daily-iso-b.test.local")
        finally:
            connection.set_tenant(self.tenant)

    def test_the_daily_summary_does_not_leak_the_other_tenant(self):
        a_body = self.admin_a.get(DAILY_URL).json()
        self.assertEqual(a_body["code"], 0, a_body)
        a_dates = [row["date"] for row in a_body["data"]["items"]]
        # 非空哨兵 + 判据：A 只看到自己那天（日期与金额都是 A 的）。
        self.assertEqual(a_dates, [self.DATE_A])
        self.assertEqual(a_body["data"]["items"][0]["total_amount"], "10.00")

        b_body = self._get_b().json()
        self.assertEqual(b_body["code"], 0, b_body)
        b_rows = b_body["data"]["items"]
        self.assertTrue(b_rows, "B 的按天汇总为空——负断言被空列表凑出来了，测试自身失效")
        self.assertEqual([row["date"] for row in b_rows], [self.DATE_B])
        self.assertNotIn(self.DATE_A, [row["date"] for row in b_rows])

    def test_b_cannot_reach_a_by_date_filter_either(self):
        """B 的账号在自己的域名上用 A 那天做筛选 ⇒ 仍是空（不是把 A 的行筛出来）。"""
        b_body = self._get_b({"date_from": self.DATE_A, "date_to": self.DATE_A}).json()
        self.assertEqual(b_body["code"], 0, b_body)
        self.assertEqual(b_body["data"]["items"], [])


class DailySargableWindowBoundaryTests(_DailyFixture):
    """⭐ N3（W-01，2026-10-29）：`date_from`/`date_to` 筛从 `settled_at__date__gte/lte`
    改写为**裸列半开区间**（`>= 当日 00:00` / `< 次日 00:00`，窗口算式
    `settlement_views._local_day_window`）后的**边界等价对拍 + SQL 形状钉**。

    - **等价性**（结果对拍）：Asia/Shanghai 无夏令时 ⇒ 两种形态**逐行等价**；
      本组把等价性钉在亚秒级边界时刻上（恰好 00:00:00.000 / 23:59:59.9 / 次日
      00:00:00.1）。旧形态**写死在测试里**（Django 原生 `__date` lookup，
      `_old_form_pks`，⛔ 不 import 被测实现）同库同值对照——被测实现改回
      `__date` 时对照面不变，等价性断了才会红。
    - **SQL 形状钉**（变异判据）：筛必须出现在 SQL 的 WHERE 里为**裸列**比较
      （套了函数的列进不了 b-tree，那是 W-01 的病根）；`TruncDate` 归天投影
      必须仍在且**恰好一处**（分组一字不动）。

    ⚠️ 期望值一律写**字面值**（E-0024）；夹具值合成（E-0077）。
    """

    def _old_form_pks(self, date_from=None, date_to=None):
        """旧形态（`settled_at__date` 套函数筛）在同一批行上的命中集——**改口径之前**的行为。

        基集照端点逐字（已结账 + `settled_at` 非空 + 两条软删排除），只把「时间窗」
        留在旧 `__date` 形态上；两边各自独立算出同一批行，才叫「同库同值」。
        """
        qs = Performance.objects.filter(
            settle_status=SettleStatus.SETTLED,
            settled_at__isnull=False,
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        )
        if date_from is not None:
            qs = qs.filter(settled_at__date__gte=date_from)
        if date_to is not None:
            qs = qs.filter(settled_at__date__lte=date_to)
        return set(qs.values_list("pk", flat=True))

    def _new_form_pks(self, day):
        """新形态（被测实现的窗口算式）在同一批行上的命中集。"""
        start, end = _local_day_window(day)
        return set(
            Performance.objects.filter(settled_at__gte=start, settled_at__lt=end)
            .values_list("pk", flat=True)
        )

    def test_boundary_instants_land_in_the_same_buckets_as_the_old_date_lookup(self):
        """四个亚秒级边界行：新旧形态命中的都是「恰 00:00:00.000」与「23:59:59.9」两行。

        - 恰在**当日 00:00:00.000** ⇒ 归当日（半开区间的**闭端**）；
        - **23:59:59.9** ⇒ 归当日；
        - 恰在**次日 00:00:00.000** ⇒ 归次日（半开区间的**开端**——判别点）；
        - **次日 00:00:00.1** ⇒ 归次日。
        """
        rows = {
            "midnight_start": self._perf(
                self._order(), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 28, 0, 0),
                settled_amount=Decimal("1.00"),
            ),
            "last_of_day": self._perf(
                self._order(), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 28, 23, 59, 59, 900000),
                settled_amount=Decimal("2.00"),
            ),
            "exact_next_midnight": self._perf(
                self._order(), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 29, 0, 0),
                settled_amount=Decimal("4.00"),
            ),
            "past_next_midnight": self._perf(
                self._order(), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 29, 0, 0, 0, 100000),
                settled_amount=Decimal("8.00"),
            ),
        }
        # 前置哨兵：四行都真的在库里（不是「没造出来」）。
        self.assertEqual(Performance.objects.count(), 4)

        day = date(2026, 10, 28)
        # ⭐ 新旧形态同库同值：10-28 的窗口命中的都是前两行。
        self.assertEqual(self._new_form_pks(day), self._old_form_pks(day, day))
        self.assertEqual(
            self._new_form_pks(day),
            {rows["midnight_start"].pk, rows["last_of_day"].pk},
        )

        # 端点读数（字面值）：date_from/date_to 双端含 ⇒ 只有 10-28 一行（2 单、3.00）。
        data = self._rows({"date_from": "2026-10-28", "date_to": "2026-10-28"})
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"], [
            {"date": "2026-10-28", "order_count": 2, "total_amount": "3.00"},
        ])

        # 不带筛选：四个边界行各归各的天（恰 00:00:00.000 与 00:00:00.1 都归次日）。
        self.assertEqual(
            [(r["date"], r["order_count"]) for r in self._rows()["items"]],
            [("2026-10-29", 2), ("2026-10-28", 2)],
        )

    def test_single_sided_bounds_respect_microsecond_edges(self):
        """单端筛同样钉在亚秒边界上：闭端含恰 00:00:00.000，开端排恰 00:00:00.000。"""
        last_of_28 = self._perf(
            self._order(), self.taker_a,
            settled_at=_at_shanghai(2026, 10, 28, 23, 59, 59, 900000),
            settled_amount=Decimal("1.00"),
        )
        exact_29 = self._perf(
            self._order(), self.taker_a,
            settled_at=_at_shanghai(2026, 10, 29, 0, 0),
            settled_amount=Decimal("2.00"),
        )
        past_29 = self._perf(
            self._order(), self.taker_a,
            settled_at=_at_shanghai(2026, 10, 29, 0, 0, 0, 100000),
            settled_amount=Decimal("4.00"),
        )
        self.assertEqual(Performance.objects.count(), 3)  # 哨兵

        # date_from=10-29：恰在 10-29 00:00:00.000 的行**含**（gte 闭端），
        # 23:59:59.9 与它只差 0.1 秒 ⇒ 不含。
        self.assertEqual(
            self._old_form_pks(date_from=date(2026, 10, 29)),
            {exact_29.pk, past_29.pk},
        )
        data = self._rows({"date_from": "2026-10-29"})
        self.assertEqual([r["date"] for r in data["items"]], ["2026-10-29"])
        self.assertEqual(data["items"][0]["order_count"], 2)

        # date_to=10-28：23:59:59.9 含；恰在 10-29 00:00:00.000 的行**不含**（lt 开端，⭐ 判别点）。
        self.assertEqual(self._old_form_pks(date_to=date(2026, 10, 28)), {last_of_28.pk})
        data = self._rows({"date_to": "2026-10-28"})
        self.assertEqual([r["date"] for r in data["items"]], ["2026-10-28"])
        self.assertEqual(data["items"][0]["order_count"], 1)

    def test_the_window_filter_is_a_bare_column_half_open_interval(self):
        """⭐ SQL 形状钉（变异判据）：筛必须是**裸列**半开区间；`TruncDate` 投影恰好一处。

        把实现改回 `settled_at__date__gte/lte` ⇒ 三条断言全红：
        ① `>=`/`<` 的裸列界限从 WHERE 里消失；② `AT TIME ZONE` 出现第 2、3 处
        （筛也套上了函数）；③ `::date` 开始**参与比较**（W-01 的病根形状）。
        ③ 的正则只认「`::date` 后紧跟比较符」——归天投影（`::date AS "day"`）不误伤。
        """
        view = SettlementDailySummaryView()
        request = RequestFactory().get(
            DAILY_URL, {"date_from": "2026-10-28", "date_to": "2026-10-28"}
        )
        # RequestFactory 的 WSGIRequest 没有 DRF 的 `query_params`（那是
        # `initialize_request` 挂上的）⇒ 手工指过去；用户取夹具管理员（有本租户全部游戏，
        # D-0136）⇒ scope 非空、query 能编译。
        request.query_params = request.GET
        request.user = User.objects.get(username="daily-fin")
        view.request = request

        sql = str(view.get_queryset().query)

        # ① 裸列半开区间真的在 WHERE 里（sargable 的本体）。
        self.assertIn('."settled_at" >= ', sql)
        self.assertIn('."settled_at" < ', sql)
        # ② 归天投影（TruncDate）仍在且恰好一处——分组一字不动；筛没有再引入时区转换。
        self.assertEqual(sql.count("AT TIME ZONE"), 1)
        # ③ 没有任何「套了 ::date 的列」参与比较（旧 `__date` 筛的 SQL 形状）。
        self.assertIsNone(re.search(r"::date\s*(?:>=|<=|=|<|>)", sql))
