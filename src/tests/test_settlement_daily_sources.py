"""D-0323（2026-10-29）：`GET /api/business/settlements/daily-summary/sources` 的测试网。

业务方拍板：结算记录页「总结算金额」列可点击展开，展开后按**订单来源**
展示各自结算了多少笔、多少钱；来源行「查看明细」= **当天 + 该来源**（走 `/settlements`
的 `order_source` 两态筛选，那边的三态用例在 `tests/test_settlement_api.py`）。

本文件钉的网与判据（每条都有会红的用例）：

1. ⭐ **口径对拍钉**（`SourcesMatchDailySummaryTests`）：本端点某天的各行 `order_count`
   之和 == `daily-summary` 同天 `order_count`；`total_amount` 之和 == 同天
   `total_amount`——**混合 NULL 快照天 / 全 NULL 快照天 / 空天**三态都拍。两边口径
   漂移（基集 / 归天 / distinct / Sum NULL 忽略任一处不同）当场红。
2. **分组与排序**：多来源分组数值正确；NULL 来源归「未指定」行（`source_id=null`）；
   排序 = 自拟三条（金额降序 → 未指定垫底 → 名称升序，D-0323 登记）。
3. **越权与范围**：无 `finance.payroll` ⇒ 403（含「持别的节点仍 403」与「匿名 401」）；
   `scope_queryset` 接线（桩打在返回值上 + 不接桩的对照）与按游戏收窄。
4. **软删排除**（D-0144 / R-6 口径）：已软删单 / 被软删的绩效行都不进分解——
   每条都先证「删之前它在」再断「删之后它不在」（E-0004 的假通过形态）。
5. **入参纪律**：`date` 缺失 / 空串 / 非法 ⇒ 200 + 业务码 `1000`（E-0010，不是 HTML 500）；
   该天无已结账行 ⇒ `code 0` + 空 `items`（**不是 404**）。
6. **跨租户否定断言**（`AGENTS.md` §4）：以租户 A 的凭据访问 B 的分解必须失败 ⇒
   判据用**租户内唯一、且两租户取值不同**的字段造数（本网用来源名 + 金额）。
7. **N3（W-01）半开区间边界对拍**（`SourcesSargableWindowBoundaryTests`）：`date` 天窗已从
   `settled_at__date=<day>` 改写为裸列半开区间——亚秒级边界时刻新旧形态同库同值 +
   SQL 形状钉（本端点无归天投影 ⇒ SQL 里不该有任何 `AT TIME ZONE`）。

⚠️ 期望值一律写**字面值**（E-0024）：金额写 `"10.00"`、日期写 `"2026-10-28"`——
不引被测模块导出的常量（自指断言不会红）。
⚠️ 夹具值一律**合成**（E-0077）。
"""
import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.api.settlement_views import _local_day_window
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import workbook_of

#: 本端点（**没有**尾斜杠，与 `/settlements/daily-summary` 同形）。
SOURCES_URL = "/api/business/settlements/daily-summary/sources"
#: 对拍的另一端（口径的唯一伙伴；改它先看 D-0319 的拍板记录）。
DAILY_URL = "/api/business/settlements/daily-summary"
#: 与 `/settlements` 同一颗读节点（`finance.payroll`）。
NODE_CODE = "finance.payroll"
#: Django 5 移除了 `django.utils.timezone.utc`，用标准库的 UTC。
UTC = dt_timezone.utc


def _at_utc(year, month, day, hour, minute):
    """固定的 UTC 时刻（不依赖 `now`——时区边界必须钉死钟点）。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


#: 业务时区（本仓 `TIME_ZONE`，恒定 UTC+8、无夏令时）。
SHANGHAI = dt_timezone(timedelta(hours=8))


def _at_shanghai(year, month, day, hour, minute, second=0, microsecond=0):
    """北京时间下的固定时刻（转成 UTC 存库）——能表达秒与亚秒的边界（23:59:59.9）。"""
    local = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=SHANGHAI)
    return local.astimezone(UTC)


class _SourcesFixture(TenantTestCase):
    """造「已结账绩效行 + 订单来源字典」的小夹具（各用例只表达自己的差异）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 业务逻辑用例用管理员：只为跳过建角色目录（闸门由 PermissionTests 用非管理员钉）。
        self.finance = make_logged_in_client(username="src-fin", is_superuser=True)
        self.taker_a = User.objects.create_user(username="src-taker-a")
        self.taker_b = User.objects.create_user(username="src-taker-b")
        self._order_seq = 0

    def _source(self, name):
        return OrderSource.objects.create(name=name)

    def _order(self, *, game=None, order_source=None):
        self._order_seq += 1
        return Order.objects.create(
            game=game or self.game,
            order_no=f"2026102900000{self._order_seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
            order_source=order_source,
        )

    def _perf(self, order, user=None, **overrides):
        """一条**已结账**的绩效行（默认结在 2026-10-29 本地晚间、快照 20.00）。"""
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.SETTLED,
            # 本地 2026-10-29 21:00（= UTC 13:00，离 UTC 边界都远，不会被误伤）。
            "settled_at": _at_utc(2026, 10, 29, 13, 0),
            "settled_amount": Decimal("20.00"),
            "settled_by": "src-fin",
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _items(self, day, client=None):
        body = (client or self.finance).get(SOURCES_URL, {"date": day}).json()
        # 前置哨兵：端点本身成功（失败用例不走这个 helper）。
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _daily_row(self, day):
        """对拍伙伴：`daily-summary` 里 `date == day` 的那一行（必须存在，否则对拍失效）。"""
        body = self.finance.get(
            DAILY_URL, {"date_from": day, "date_to": day}
        ).json()
        self.assertEqual(body["code"], 0, body)
        rows = [r for r in body["data"]["items"] if r["date"] == day]
        self.assertTrue(rows, f"daily-summary 没有 {day} 那一行——对拍的另一边不存在")
        return rows[0]


class SettlementExportTests(_SourcesFixture):
    def test_export_matches_source_order_type_and_snapshot(self):
        source = self._source("测试来源")
        selected = self._order(order_source=source)
        self._perf(selected, self.taker_a, settled_amount=Decimal("9.25"), bonus_amount=Decimal("20.00"))
        self._perf(selected, self.taker_b, emp_type=EmpType.SALES, settled_amount=Decimal("44.00"))
        self._perf(self._order(order_source=source), self.taker_a, settled_amount=Decimal("88.00"))
        self._perf(self._order(), self.taker_b, settled_amount=Decimal("77.00"))
        params = {"date_from": "2026-10-29", "date_to": "2026-10-29", "settle_status": "已结账", "emp_type": "打单工资", "order_source": source.pk, "order_no": selected.order_no}
        response = self.finance.get("/api/business/settlements/export", params)
        audit = OperationLog.objects.filter(action="settlement.export").latest("id")
        self.assertEqual(audit.log_type, 20)
        self.assertEqual(audit.actor, "src-fin")
        self.assertIn("导出了结算明细", audit.summary)
        rows = list(workbook_of(response)["结算明细"].values)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][3], 9.25)
        self.assertEqual(rows[1][4], selected.order_no)
        self.assertEqual(workbook_of(response)["结算明细"]["D2"].number_format, "0.00")
        before = OperationLog.objects.filter(action="settlement.export").count()
        invalid = self.finance.get("/api/business/settlements/export", {"order_source": "invalid"}).json()
        self.assertEqual(invalid["code"], 1000)
        self.assertEqual(OperationLog.objects.filter(action="settlement.export").count(), before)
        unspecified = list(workbook_of(self.finance.get("/api/business/settlements/export", {"order_source": "none"}))["结算明细"].values)
        self.assertEqual(len(unspecified), 2)
        self.assertEqual(unspecified[1][3], 77)

    def test_export_requires_both_permissions_and_respects_game_scope(self):
        allowed = self._order()
        self._perf(allowed, self.taker_a)
        other = self._order(game=Game.objects.create(name="游戏乙"))
        self._perf(other, self.taker_b)
        reader = make_logged_in_client(username="export-reader", groups=("导读",), nodes=("finance.payroll",))
        exporter = make_logged_in_client(username="export-only", groups=("导出",), nodes=("finance.export_performance",))
        both = make_logged_in_client(username="export-both", groups=("导全",), nodes=("finance.payroll", "finance.export_performance"), games=[self.game])
        url = "/api/business/settlements/export"
        self.assertEqual(reader.get(url).status_code, 403)
        self.assertEqual(exporter.get(url).status_code, 403)
        rows = list(workbook_of(both.get(url))["结算明细"].values)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][4], allowed.order_no)

    def test_missing_snapshot_uses_same_amount_as_detail(self):
        order = self._order()
        self._perf(order, self.taker_a, settled_amount=None, bonus_amount=Decimal("17.50"))
        rows = list(workbook_of(self.finance.get("/api/business/settlements/export"))["结算明细"].values)
        self.assertEqual(rows[1][3], 17.5)


class SettlementTypeFilterTests(_SourcesFixture):
    def test_daily_and_sources_filter_before_aggregation(self):
        order = self._order(order_source=self._source("测试来源"))
        self._perf(order, self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(order, self.taker_b, emp_type=EmpType.SALES, settled_amount=Decimal("25.00"))
        for kind, amount in [("打单工资", "10.00"), ("销售提成", "25.00")]:
            with self.subTest(kind=kind):
                daily = self.finance.get(DAILY_URL, {"emp_type": kind, "page": 1, "page_size": 1}).json()
                sources = self.finance.get(SOURCES_URL, {"date": "2026-10-29", "emp_type": kind}).json()
                self.assertEqual(daily["code"], 0)
                self.assertEqual(daily["data"]["total"], 1)
                self.assertEqual(daily["data"]["items"][0]["total_amount"], amount)
                self.assertEqual(sources["code"], 0)
                self.assertEqual(sources["data"]["items"][0]["total_amount"], amount)
        for url, params in [(DAILY_URL, {}), (SOURCES_URL, {"date": "2026-10-29"})]:
            self.assertEqual(self.finance.get(url, {**params, "emp_type": "未知"}).json()["code"], 1000)


class SourcesMatchDailySummaryTests(_SourcesFixture):
    """⭐ 口径对拍钉：分解各行之和 == 按天汇总那一行（三态：混合 NULL / 全 NULL / 空天）。

    本组用例是「两边口径漂移」的哨兵——把本端点的基集 / 归天 / distinct Count /
    `Sum` NULL 忽略**任何一处**改得与 `daily-summary` 不同，对拍当场红。
    """

    DAY = "2026-10-29"

    def _assert_sums_match(self, day):
        items = self._items(day)
        daily = self._daily_row(day)
        self.assertEqual(
            sum(row["order_count"] for row in items),
            daily["order_count"],
            f"{day}：分解的 distinct 订单数之和与按天汇总的 order_count 对不上",
        )
        self.assertEqual(
            sum(Decimal(row["total_amount"]) for row in items),
            Decimal(daily["total_amount"]),
            f"{day}：分解的金额之和与按天汇总的 total_amount 对不上",
        )

    def test_mixed_null_snapshot_day_sums_match(self):
        """混合天：有快照行 + NULL 快照行（`Sum` 忽略 NULL，`Count` 照数单）。

        ⚠️ 同一张单不能有第二条「打单工资」行（`uniq_order_main_taker`）⇒
        同单的第二行用「销售提成」（真实形状：一单多行 = 打单工资 + 销售提成）。
        """
        src = self._source("甲店铺")
        # 甲店铺：一张单两条已结账行（12.34 快照 + NULL 快照）。
        shared = self._order(order_source=src)
        self._perf(shared, self.taker_a, settled_amount=Decimal("12.34"))
        self._perf(
            shared, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=None,
        )
        # 未指定来源：一张单一条已结账行（7.00 快照）。
        self._perf(self._order(), self.taker_a, settled_amount=Decimal("7.00"))

        items = self._items(self.DAY)

        # 字面值（E-0024）：甲店铺 1 单、12.34（NULL 那行贡献单数不贡献金额）；
        # 未指定 1 单、7.00。
        by_id = {row["source_id"]: row for row in items}
        self.assertEqual(by_id[src.pk]["order_count"], 1)
        self.assertEqual(by_id[src.pk]["total_amount"], "12.34")
        self.assertIsNone(by_id[None]["source_id"])
        self.assertEqual(by_id[None]["order_count"], 1)
        self.assertEqual(by_id[None]["total_amount"], "7.00")
        # 对拍：单数 2、金额 19.34。
        self.assertEqual(self._daily_row(self.DAY)["order_count"], 2)
        self.assertEqual(self._daily_row(self.DAY)["total_amount"], "19.34")
        self._assert_sums_match(self.DAY)

    def test_all_null_snapshot_day_sums_match(self):
        """全 NULL 天：`Sum` 出 `None` ⇒ 两边都按 0 落（`_or_zero` 的同一处定义）。"""
        src = self._source("乙店铺")
        self._perf(self._order(order_source=src), self.taker_a, settled_amount=None)

        items = self._items(self.DAY)

        # 字面值：金额 "0.00"（判空回落，不是「这天没钱」），单数照数。
        self.assertEqual(items[0]["total_amount"], "0.00")
        self.assertEqual(items[0]["order_count"], 1)
        self.assertEqual(self._daily_row(self.DAY)["total_amount"], "0.00")
        self._assert_sums_match(self.DAY)

    def test_empty_day_is_empty_items_not_404(self):
        """空天：`code 0` + 空 `items`（不 404——主表那行本来就来自别的天）。"""
        self._perf(self._order(), self.taker_a, settled_at=_at_utc(2026, 10, 28, 13, 0))

        body = self.finance.get(SOURCES_URL, {"date": "2026-10-29"}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])
        # 对照哨兵：库里有数据（是别的天的），不是「没造出来」。
        self.assertEqual(Performance.objects.count(), 1)

    def test_unsettled_rows_are_not_in_the_breakdown(self):
        """未结账行不入分解（与 daily-summary 同一条口径）：只贡献给已结账那一半。"""
        src = self._source("丙店铺")
        order = self._order(order_source=src)
        self._perf(order, self.taker_a, settled_amount=Decimal("9.00"))
        Performance.objects.create(
            order=order,
            user=self.taker_b,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("8.00"),
            bonus_amount=Decimal("8.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )

        items = self._items(self.DAY)

        # 前置哨兵：两行都在库里。
        self.assertEqual(Performance.objects.filter(order=order).count(), 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["order_count"], 1)
        self.assertEqual(items[0]["total_amount"], "9.00")


class SourcesAggregationTests(_SourcesFixture):
    """分组数值 + 自拟排序三条（金额降序 / 未指定垫底 / 同额名称升序）。"""

    DAY = "2026-10-29"

    def test_multiple_sources_group_separately(self):
        """两个来源 + 未指定：三行各自的单数与金额（跨天不混——都造在同一天）。"""
        src_a = self._source("甲店铺")
        src_b = self._source("乙店铺")
        self._perf(self._order(order_source=src_a), self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(
            self._order(order_source=src_a), self.taker_b,
            settled_amount=Decimal("5.00"),
        )
        self._perf(self._order(order_source=src_b), self.taker_a, settled_amount=Decimal("3.00"))
        self._perf(self._order(), self.taker_b, settled_amount=Decimal("2.00"))

        items = self._items(self.DAY)

        self.assertEqual(len(items), 3)
        by_id = {row["source_id"]: row for row in items}
        self.assertEqual((by_id[src_a.pk]["order_count"], by_id[src_a.pk]["total_amount"]), (2, "15.00"))
        self.assertEqual((by_id[src_b.pk]["order_count"], by_id[src_b.pk]["total_amount"]), (1, "3.00"))
        self.assertEqual((by_id[None]["order_count"], by_id[None]["total_amount"]), (1, "2.00"))

    def test_order_count_is_distinct_orders_within_a_source(self):
        """⭐ 判别力用例：同一来源、同一张单两条已结账行 ⇒ 该来源 `order_count` = 1。

        把 `Count("order_id", distinct=True)` 的 `distinct` 拿掉 ⇒ 本用例当场红。
        """
        src = self._source("甲店铺")
        shared = self._order(order_source=src)
        self._perf(shared, self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(
            shared, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("5.00"),
        )

        items = self._items(self.DAY)

        self.assertEqual(items[0]["order_count"], 1)
        self.assertEqual(items[0]["total_amount"], "15.00")

    def test_deleted_source_lands_in_the_unspecified_row(self):
        """来源被删（R8 允许、FK `SET_NULL`）⇒ 那些单落进「未指定」行（靠名字快照找不到主）。"""
        src = self._source("丁店铺")
        order = self._order(order_source=src)
        self._perf(order, self.taker_a, settled_amount=Decimal("6.00"))
        OrderSource.objects.filter(pk=src.pk).delete()

        items = self._items(self.DAY)

        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["source_id"])
        self.assertIsNone(items[0]["source_name"])
        self.assertEqual(items[0]["total_amount"], "6.00")

    def test_sort_amount_desc_then_unspecified_last_then_name_asc(self):
        """自拟排序三条（D-0323 登记）：

        金额降序（大额在前）→「未指定」垫底（**即使它金额最大**——它不是一家店）→
        同额按名称**码点升序**（Python 字符串序：`丙 U+4E19 < 乙 U+4E59 < 甲 U+7532`，
        稳定可复算；**不是拼音序**）。把任何一条换掉 ⇒ 本用例当场红。
        """
        src_b = self._source("乙店铺")   # 5.00（与丙同额 ⇒ 码点序：丙 < 乙 ⇒ 丙在前）
        src_c = self._source("丙店铺")   # 5.00
        src_a = self._source("甲店铺")   # 1.00（金额最小但指定来源 ⇒ 仍在未指定之前）
        self._perf(self._order(order_source=src_b), self.taker_a, settled_amount=Decimal("5.00"))
        self._perf(self._order(order_source=src_c), self.taker_b, settled_amount=Decimal("5.00"))
        self._perf(self._order(order_source=src_a), self.taker_a, settled_amount=Decimal("1.00"))
        # 未指定 9.00——金额最大，但必须垫底。
        self._perf(self._order(), self.taker_b, settled_amount=Decimal("9.00"))

        items = self._items(self.DAY)

        self.assertEqual(
            [(row["source_name"], row["total_amount"]) for row in items],
            [("丙店铺", "5.00"), ("乙店铺", "5.00"), ("甲店铺", "1.00"), (None, "9.00")],
        )


class SourcesParamTests(_SourcesFixture):
    """`date` 的入口纪律（E-0010：缺失 / 空串 / 非法 ⇒ 200 + `1000`，不是 HTML 500）。"""

    def test_missing_date_is_a_business_error(self):
        response = self.finance.get(SOURCES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_blank_date_counts_as_missing(self):
        """空串按「没传」（与 `SettlementViewSet._params` 同口径）⇒ 命中 `required`。"""
        response = self.finance.get(SOURCES_URL, {"date": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_garbage_date_is_a_business_error(self):
        for bad in ("garbage", "2026-13-45", "2026/10/29"):
            with self.subTest(bad=bad):
                response = self.finance.get(SOURCES_URL, {"date": bad})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)


class SourcesSoftDeleteTests(_SourcesFixture):
    """软删排除（D-0144 / R-6，与 daily-summary 同一条口径）——先证「删之前在」再断「不在」。"""

    DAY = "2026-10-29"

    def test_rows_of_a_soft_deleted_order_leave_the_breakdown(self):
        src = self._source("甲店铺")
        order_dead = self._order(order_source=src)
        order_alive = self._order(order_source=src)
        self._perf(order_dead, self.taker_a, settled_amount=Decimal("10.00"))
        self._perf(order_alive, self.taker_b, settled_amount=Decimal("5.00"))

        # 对照：删之前两张单都算进甲店铺（2 单 15.00）。
        row = self._items(self.DAY)[0]
        self.assertEqual((row["order_count"], row["total_amount"]), (2, "15.00"))

        Order.objects.filter(pk=order_dead.pk).update(deleted_at=_at_utc(2026, 10, 29, 12, 0))

        row = self._items(self.DAY)[0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "5.00"))

    def test_a_soft_deleted_performance_row_leaves_the_breakdown(self):
        """订单活着、单条绩效行被软删（R-6 形状）：它不贡献金额；同单的活行照旧计单。"""
        src = self._source("甲店铺")
        order = self._order(order_source=src)
        alive = self._perf(order, self.taker_a, settled_amount=Decimal("5.00"))
        dead = self._perf(
            order, self.taker_b,
            emp_type=EmpType.SALES, settled_amount=Decimal("7.00"),
        )

        row = self._items(self.DAY)[0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "12.00"))

        Performance.objects.filter(pk=dead.pk).update(deleted_at=_at_utc(2026, 10, 29, 12, 0))
        alive.refresh_from_db()  # 前置哨兵：活行还在。

        row = self._items(self.DAY)[0]
        self.assertEqual((row["order_count"], row["total_amount"]), (1, "5.00"))


class SourcesScopingWiringTests(_SourcesFixture):
    """⭐ `scope_queryset` 接线**真的被用上**（桩打在**返回值**上，配不接桩的对照）。"""

    DAY = "2026-10-29"

    def test_the_sources_breakdown_uses_the_scoping_result(self):
        src = self._source("甲店铺")
        self._perf(self._order(order_source=src), self.taker_a)

        self.assertEqual(len(self._items(self.DAY)), 1)  # 对照：本来点得见

        with mock.patch(
            "apps.business.api.settlement_views.scope_queryset",
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._items(self.DAY), [])

        self.assertTrue(seam.called, "来源分解没有经过 scope_queryset 卡点")

    def test_the_sources_breakdown_narrows_to_the_viewers_games(self):
        """按游戏权限收窄：只勾 A 游戏的人只看到 A 那天的分解（B 游戏的行不进任何来源）。"""
        game_a = Game.objects.create(name="分解甲")
        game_b = Game.objects.create(name="分解乙")
        src = self._source("甲店铺")
        # 甲游戏、甲店铺：10.00；乙游戏、同一家店铺：99.00（收窄不力就会混进 99.00）。
        self._perf(
            self._order(game=game_a, order_source=src), self.taker_a,
            settled_amount=Decimal("10.00"),
        )
        self._perf(
            self._order(game=game_b, order_source=src), self.taker_b,
            settled_amount=Decimal("99.00"),
        )
        a_only = make_logged_in_client(
            username="src-a-only", groups=("客服",), nodes=(NODE_CODE,), games=[]
        )
        grant_games(User.objects.get(username="src-a-only"), game_a)

        items = self._items(self.DAY, client=a_only)

        # 前置哨兵：B 那行真的在库里（不是「没造出来」）。
        self.assertEqual(Performance.objects.count(), 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_id"], src.pk)
        self.assertEqual(items[0]["total_amount"], "10.00")


class SourcesPermissionTests(TenantTestCase):
    """闸门 = `has_node("finance.payroll")`（与 `/settlements` / `daily-summary` 同一颗）。

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
        # ⚠️ `nodes=` 只往用户**已有组**对应的 Role 上挂 ⇒ 必须同时给一个组
        #    （照 `test_settlement_daily_summary.py` 的同款形态）。
        client = make_logged_in_client(
            username="src-holder", groups=("客服",), nodes=(NODE_CODE,)
        )

        response = client.get(SOURCES_URL, {"date": "2026-10-29"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_a_user_without_the_node_is_denied_with_a_permission_message(self):
        client = make_logged_in_client(username="src-no-node")

        response = client.get(SOURCES_URL, {"date": "2026-10-29"})

        self.assertEqual(response.status_code, 403)
        self.assertIn("权限", response.json()["msg"])

    def test_a_user_holding_a_different_node_is_still_denied(self):
        """持 `order.list`（另一个真节点）仍 403——否则「有任意权限就放行」也能绿。"""
        client = make_logged_in_client(username="src-wrong-node")
        self._grant("src-wrong-node", "order.list")

        response = client.get(SOURCES_URL, {"date": "2026-10-29"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有查看结算来源分解的权限")

    def test_anonymous_is_401_not_403(self):
        anonymous = Client(SERVER_NAME="tenant.test.com")

        self.assertEqual(
            anonymous.get(SOURCES_URL, {"date": "2026-10-29"}).status_code, 401
        )


class SourcesCrossTenantTests(TenantTestCase):
    """跨租户否定断言（`AGENTS.md` §4）：以 A 的凭据访问 B 的分解必须失败。

    判据字段 = **来源名 + 金额**（租户内唯一——`OrderSource.name` 租户内唯一；
    两租户取值不同：A 只有「甲店铺 10.00」、B 只有「乙店铺 90.00」）。
    """

    DAY = "2026-10-29"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="src_iso_b", name="来源分解隔离租户 B", domain="src-iso-b.test.local"
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求不会在响应结束时还原 schema）。
        connection.set_tenant(self.tenant)

        # ── B 侧先建（try/finally 还原 schema）──
        try:
            with tenant_context(self.tenant_b):
                self.admin_b = make_logged_in_client(
                    domain="src-iso-b.test.local", username="src-iso-admin-b", is_superuser=True
                )
                game_b = Game.objects.create(name="乙游")
                src_b = OrderSource.objects.create(name="乙店铺")
                order_b = Order.objects.create(
                    game=game_b,
                    order_no="20261029000000000002",
                    status=OrderStatus.COMPLETED,
                    hall_price=Decimal("100.00"),
                    order_source=src_b,
                )
                taker_b = User.objects.create_user(username="src-iso-taker-b")
                Performance.objects.create(
                    order=order_b,
                    user=taker_b,
                    emp_type=EmpType.MAIN,
                    temp_amount=Decimal("90.00"),
                    bonus_amount=Decimal("90.00"),
                    review_status=ReviewStatus.APPROVED,
                    settle_status=SettleStatus.SETTLED,
                    settled_at=_at_utc(2026, 10, 29, 13, 0),
                    settled_amount=Decimal("90.00"),
                )
        finally:
            connection.set_tenant(self.tenant)

        # ── A 侧 ──
        self.admin_a = make_logged_in_client(
            domain="tenant.test.com", username="src-iso-admin-a", is_superuser=True
        )
        game_a = Game.objects.create(name="甲游")
        src_a = OrderSource.objects.create(name="甲店铺")
        order_a = Order.objects.create(
            game=game_a,
            order_no="20261029000000000001",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
            order_source=src_a,
        )
        taker_a = User.objects.create_user(username="src-iso-taker-a")
        Performance.objects.create(
            order=order_a,
            user=taker_a,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.SETTLED,
            settled_at=_at_utc(2026, 10, 29, 13, 0),
            settled_amount=Decimal("10.00"),
        )

    def _get_b(self):
        """在 B 的域名上用 B 的凭据发请求，结束后把连接钉回租户 A。"""
        try:
            return self.admin_b.get(
                SOURCES_URL, {"date": self.DAY}, SERVER_NAME="src-iso-b.test.local"
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_the_breakdown_does_not_leak_the_other_tenant(self):
        a_body = self.admin_a.get(SOURCES_URL, {"date": self.DAY}).json()
        self.assertEqual(a_body["code"], 0, a_body)
        # 非空哨兵 + 判据：A 只看到自己的来源与金额。
        self.assertEqual(a_body["data"]["items"], [
            {"source_id": OrderSource.objects.get(name="甲店铺").pk,
             "source_name": "甲店铺", "order_count": 1, "total_amount": "10.00"},
        ])

        b_body = self._get_b().json()
        self.assertEqual(b_body["code"], 0, b_body)
        b_items = b_body["data"]["items"]
        self.assertTrue(b_items, "B 的来源分解为空——负断言被空列表凑出来了，测试自身失效")
        self.assertEqual(b_items[0]["source_name"], "乙店铺")
        self.assertEqual(b_items[0]["total_amount"], "90.00")
        # ⭐ 否定断言：A 的来源名不出现在 B 的分解里（以 A 的数据造的判据）。
        self.assertNotIn("甲店铺", [row["source_name"] for row in b_items])

    def test_settlement_export_isolated_between_tenants(self):
        url = "/api/business/settlements/export"
        a_rows = list(workbook_of(self.admin_a.get(url))["结算明细"].values)
        try:
            b_response = self.admin_b.get(url, SERVER_NAME="src-iso-b.test.local")
            b_rows = list(workbook_of(b_response)["结算明细"].values)
            wrong = self.admin_a.get(url, SERVER_NAME="src-iso-b.test.local")
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual([row[4] for row in a_rows[1:]], ["20261029000000000001"])
        self.assertEqual([row[4] for row in b_rows[1:]], ["20261029000000000002"])
        self.assertIn(wrong.status_code, (401, 403))
        # 用甲租户结果替换乙租户结果，确认乙侧唯一订单号的断言能失败。
        with self.assertRaises(AssertionError):
            self.assertEqual([row[4] for row in a_rows[1:]], ["20261029000000000002"])


class SourcesSargableWindowBoundaryTests(_SourcesFixture):
    """⭐ N3（W-01，2026-10-29）：`settled_at__date=day` 改写为**裸列半开区间**
    （`>= 当日 00:00` / `< 次日 00:00`，窗口算式 `settlement_views._local_day_window`）
    后的**边界等价对拍 + SQL 形状钉**。

    - **等价性**（结果对拍）：Asia/Shanghai 无夏令时 ⇒ 旧形态（写死在测试里的
      Django 原生 `__date` lookup，⛔ 不 import 被测实现）与新形态**逐行同库同值**；
      钉在亚秒级边界时刻（恰好 00:00:00.000 / 23:59:59.9 / 次日 00:00:00.1）上。
    - **SQL 形状钉**（变异判据）：本端点**没有**归天投影（分组键是来源），SQL 里
      连一处 `AT TIME ZONE` 都不该有；改回 `__date` ⇒ 当场红。
    """

    def test_boundary_instants_match_the_old_date_equality_lookup(self):
        """四个亚秒级边界行：新旧形态在 10-28 命中的都是甲店铺那两行（恰 00:00 与 23:59:59.9）。"""
        src_a = self._source("甲店铺")
        src_b = self._source("乙店铺")
        src_c = self._source("丙店铺")
        rows = {
            "a_midnight_start": self._perf(
                self._order(order_source=src_a), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 28, 0, 0),
                settled_amount=Decimal("8.00"),
            ),
            "a_last_of_day": self._perf(
                self._order(order_source=src_a), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 28, 23, 59, 59, 900000),
                settled_amount=Decimal("1.00"),
            ),
            "b_exact_next_midnight": self._perf(
                self._order(order_source=src_b), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 29, 0, 0),
                settled_amount=Decimal("2.00"),
            ),
            "c_past_next_midnight": self._perf(
                self._order(order_source=src_c), self.taker_a,
                settled_at=_at_shanghai(2026, 10, 29, 0, 0, 0, 100000),
                settled_amount=Decimal("4.00"),
            ),
        }
        # 前置哨兵：四行都真的在库里（不是「没造出来」）。
        self.assertEqual(Performance.objects.count(), 4)

        # ⭐ 新旧形态同库同值（`settled_at__date=day` 写死在测试里当对照）。
        day = date(2026, 10, 28)
        start, end = _local_day_window(day)
        old_form_pks = set(
            Performance.objects.filter(settled_at__date=day).values_list("pk", flat=True)
        )
        self.assertEqual(
            set(
                Performance.objects.filter(settled_at__gte=start, settled_at__lt=end)
                .values_list("pk", flat=True)
            ),
            old_form_pks,
        )
        self.assertEqual(old_form_pks, {rows["a_midnight_start"].pk, rows["a_last_of_day"].pk})

        # 端点读数（字面值）：10-28 只有甲店铺（2 单 9.00）；乙/丙是次日的行。
        items = self._items("2026-10-28")
        self.assertEqual(items, [
            {"source_id": src_a.pk, "source_name": "甲店铺",
             "order_count": 2, "total_amount": "9.00"},
        ])

        # 次日的窗口归 10-29（恰 00:00:00.000 与 00:00:00.1 都在）：丙 4.00 前、乙 2.00 后。
        self.assertEqual(
            [(r["source_name"], r["order_count"], r["total_amount"])
             for r in self._items("2026-10-29")],
            [("丙店铺", 1, "4.00"), ("乙店铺", 1, "2.00")],
        )

    def test_the_day_filter_is_a_bare_column_half_open_interval(self):
        """⭐ SQL 形状钉（变异判据）：筛是裸列 `>=`/`<`；SQL 里没有任何 `AT TIME ZONE`。

        本端点没有归天投影（分组键是来源）⇒ 旧 `settled_at__date=day` 的形状
        （`(settled_at AT TIME ZONE …)::date = …`）只要回来，两条断言全红：
        `AT TIME ZONE` 出现、`::date` 参与比较；同时裸列界限消失。
        """
        self._perf(self._order(), self.taker_a)  # 有一行可查，查询不至于被空集短路

        with CaptureQueriesContext(connection) as ctx:
            body = self.finance.get(SOURCES_URL, {"date": "2026-10-28"}).json()
        self.assertEqual(body["code"], 0, body)

        bounds_queries = [
            q["sql"] for q in ctx.captured_queries if '."settled_at" >= ' in q["sql"]
        ]
        # 前置哨兵：筛真的在跑（不是被空集短路成一条没有 WHERE 界限的查询）。
        self.assertTrue(bounds_queries, "没有捕获到带 settled_at 界限的查询")
        # 本端点不分页：界限只出现在这一条聚合查询里。
        sql = bounds_queries[0]

        # ① 裸列半开区间的两端都在。
        self.assertIn('."settled_at" < ', sql)
        # ② 没有任何时区转换 / 日期截断进 SQL（旧 `__date` 形状的整族判据）。
        self.assertNotIn("AT TIME ZONE", sql)
        self.assertIsNone(re.search(r"::date\s*(?:>=|<=|=|<|>)", sql))
