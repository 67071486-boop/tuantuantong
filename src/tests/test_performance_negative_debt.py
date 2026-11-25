"""挂负账判据的主网（2026-11-07 报酬页结算筛选返工 R3；判据唯一出处 =
`apps/business/performance_negative_debt.py`，语义唯一出处 =
`docs/plans/2026-11-07-settle-filter-rework.md` §2，含「已撤 2026-11-08」行）。

⚠️ 2026-11-08 业务方拍板**取消「挂负账强制展示」**（撤腿② `qs | forced`）——列表恢复
纯筛选语义，保留两筛选 + 红签 + order_no 跳过。原恒开展示/强制腿边界/身份收窄/
summary 计数的用例已按「功能已撤」改写或删除，⛔ 不留死代码。

| # | 判据 | 落点 |
|---|---|---|
| ⓪ | **纯筛选语义回归钉**（撤腿②反向钉：窗口外债行不出现）+ 组合矩阵（两哨兵 × settle_status 两态） | `PureFilterSemanticsTests` |
| ① | 挂负账判据（单负行 / AmountError / 已结账 / 软删行 / 软删单 / 被禁结算打手**算**） | `DebtCriterionTests` |
| ② | 净负判定 + **排除整人** + 去重口径 + 默认态负单可见 | `NetNegativeExclusionTests` |
| ③ | 参数契约（表外 1000 / 缺省 = 关） | `ParameterContractTests` |
| ④ | 缓存（勾选参数同签名零聚合 / TTL=0 直通） | `NegativeDebtCacheTests` |
| ⑤ | **范围=仅打手域**（emp_type=打单工资，业务方 2026-11-08 拍板）：客服负行不进快照/红签不亮/净负不排 | `PlayerDomainScopeTests` |
| ⑥ | 导出与列表同源（纯筛选下两处行集一致） | `ExportAgreesWithListTests` |
| ⑦ | **按单查询跳过挂负账段**（order_no 非空 ⇒ 净负排除跳过，2026-11-08 热修口径保留） | `OrderNoSkipTests` |
| ⑧ | 跨租户键不串 | `TenantIsolationDebtTests` |

夹具全合成值（PII 纪律）；测试进程 TTL=0（settings 测试块）⇒ 每请求快照直通——
这正是 `TotalsSingleQueryTests` M-5 修钉的原因（快照 = 每请求多一条聚合）。
「窗口」用 `date_from` 造：把债行 `created_at` 拨到 90 天前、窗口取最近 7 天
（date-only 整天含端，判据见 test_performance_list_totals::DateWindowBoundaryTests）。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.performance_negative_debt import (
    _compute_net_negative,
    negative_debt_snapshot,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import values_of, workbook_of

URL = "/api/business/performances/"
EXPORT_URL = "/api/business/performances/export"
PERF_MANAGEMENT_NODES = ("my_performance.export", "order.list")

#: 快照判据共用的负数金额（离量化带边界 ≥ 0.01，计划 §9-4）。
NEG = Decimal("-300.00")
#: 造「窗口」的两个日期（债行拨到 90 天前、窗口取最近 7 天）。
OLD_DAY = (timezone.now() - timedelta(days=90)).date().isoformat()
WINDOW_DAY = (timezone.now() - timedelta(days=7)).date().isoformat()
WINDOW = {"date_from": WINDOW_DAY}


def _days_ago(days):
    return timezone.now() - timedelta(days=days)


class _DebtFixture:
    """造数小夹具：游戏 + 两位打手 + 管理侧客户端（组名各用例类独立，防 Role 共组漏节点）。"""

    def _seed(self):
        cache.clear()
        self.game = Game.objects.create(name="挂负账主网游戏")
        self.taker_a = User.objects.create_user(username="debt-taker-a")
        self.taker_b = User.objects.create_user(username="debt-taker-b")
        self.finance = make_logged_in_client(
            username="debt-fin", groups=("债务客服",), nodes=PERF_MANAGEMENT_NODES
        )
        grant_games(User.objects.get(username="debt-fin"), self.game)

    def _order(self, *, game=None, no=None, price="100.00"):
        self._seq = getattr(self, "_seq", 0) + 1
        return Order.objects.create(
            game=game or self.game,
            order_no=no or f"2026110700000{self._seq:07d}",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal(price),
            total_price=Decimal(price),
        )

    def _perf(self, order, user, *, temp="20.00", bonus="20.00", **kw):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal(temp),
            "bonus_amount": Decimal(bonus),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(kw)
        return Performance.objects.create(order=order, user=user, **fields)

    def _debt_row(self, user, *, order=None, **kw):
        """一条未结账负行（`payable_net < 0` 的最简形态：bonus 负、无扣点、无调整）。"""
        kw.setdefault("temp", NEG)
        kw.setdefault("bonus", NEG)
        return self._perf(order or self._order(), user, **kw)

    def _body(self, params=None, *, client=None):
        body = (client or self.finance).get(URL, params or {}).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _nos(self, params=None, *, client=None):
        return [row["order_no"] for row in self._body(params, client=client)["items"]]


class PureFilterSemanticsTests(_DebtFixture, TenantTestCase):
    """⓪ 纯筛选语义回归钉（2026-11-08 撤强制展示）：列表不再有任何行被无视筛选塞入——
    业务方报障「窗口查询灌入 24,988 行」的反向钉；排除哨兵 = 纯排除（只减行、不加行）。"""

    def setUp(self):
        self._seed()

    def test_default_request_does_not_bring_back_the_out_of_window_debt_row(self):
        """⭐ 纯筛选主钉：不勾任何选项，窗口外挂负账打手的债行**不出现**
        （撤腿②的反向钉——旧行为它会被强制塞回）。"""
        debt = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        self.assertNotIn(debt.order.order_no, self._nos(WINDOW))

    def test_the_option_matrix_over_row_sets(self):
        """组合矩阵（纯筛选语义）：{不勾, 只勾排除禁止, 只勾排除净负, 两颗都勾} ×
        {缺省, 显式已结账}。造数：taker_a 窗口内正行 + 窗口外债行（⇒ 净负）；
        taker_b 窗口内正行且被禁结算。
        - 窗口外债行：任何格子都**不**出现（纯筛选，撤腿②反向钉）；
        - taker_a 正行：勾净负排除（含并勾）⇒ 整人消失；
        - taker_b 正行：勾排除禁止 ⇒ 消失（被禁整人排除，旧行为腿②会把它带回）；
        - 显式已结账 ⇒ 只剩已结账行（夹具没有）⇒ 全空。"""
        debt = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        a_pos = self._perf(self._order(), self.taker_a)
        StaffProfile.objects.create(user=self.taker_b, settle_forbidden=True)
        b_pos = self._perf(self._order(), self.taker_b)
        debt_no = debt.order.order_no
        a_no = a_pos.order.order_no
        b_no = b_pos.order.order_no

        for extra in (
            {},
            {"settle_forbidden_scope": "excluded"},
            {"exclude_net_negative": "1"},
            {"exclude_net_negative": "1", "settle_forbidden_scope": "excluded"},
        ):
            rows = self._nos(dict(WINDOW, **extra))
            self.assertNotIn(debt_no, rows, extra)
            if "exclude_net_negative" in extra:
                self.assertNotIn(a_no, rows, extra)
            else:
                self.assertIn(a_no, rows, extra)
            if "settle_forbidden_scope" in extra:
                self.assertNotIn(b_no, rows, extra)
            else:
                self.assertIn(b_no, rows, extra)
            history = self._nos({**WINDOW, **extra, "settle_status": "已结账"})
            self.assertEqual(history, [], extra)

    def test_a_debt_takers_visible_positive_row_also_lights_the_badge(self):
        """红签钉在**人**（打手挂负账是用户级事实）：窗口内可见的正数行同样亮签
        （改写自撤除前「收窄」线的红签回归钉，2026-11-08 撤腿②后行为不变）。"""
        debt = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        fresh_positive = self._perf(self._order(), self.taker_a)
        marks = {
            r["order_no"]: r["negative_debt"]
            for r in self._body(WINDOW)["items"]
        }
        self.assertTrue(marks[fresh_positive.order.order_no])
        self.assertNotIn(debt.order.order_no, marks)


class DebtCriterionTests(_DebtFixture, TenantTestCase):
    """① 挂负账判据（快照本体；不过 `_resolve_batch` 资格闸——被禁结算打手也算）。"""

    def setUp(self):
        self._seed()

    def test_a_single_negative_row_puts_its_player_in_the_snapshot(self):
        self._debt_row(self.taker_a)
        snapshot = negative_debt_snapshot()
        self.assertIn(self.taker_a.pk, snapshot)
        pid, net = snapshot[self.taker_a.pk][0]
        self.assertLess(net, Decimal("0.00"))

    def test_an_amount_error_row_is_not_counted(self):
        """`AmountError` 行（比例 >100）两腿都不计：SQL 守卫排除 + Python 精判跳过。"""
        self._debt_row(self.taker_a, deduct_mode="0", deduct_value=Decimal("150.00"))
        self.assertNotIn(self.taker_a.pk, negative_debt_snapshot())

    def test_settled_soft_deleted_row_and_soft_deleted_order_do_not_count(self):
        self._debt_row(self.taker_a, settle_status=SettleStatus.SETTLED)
        soft_row = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=soft_row.pk).update(deleted_at=_days_ago(1))
        soft_order_row = self._debt_row(self.taker_a)
        Order.objects.filter(pk=soft_order_row.order_id).update(deleted_at=_days_ago(1))
        snapshot = negative_debt_snapshot()
        self.assertNotIn(self.taker_a.pk, snapshot)

    def test_a_settle_forbidden_player_still_counts(self):
        """⭐ 与已撤线的差异钉住：被禁结算打手的负行**算**挂负账（账面事实优先；
        结算资格是另一道闸、留在结算侧）。"""
        StaffProfile.objects.create(user=self.taker_a, settle_forbidden=True)
        self._debt_row(self.taker_a)
        self.assertIn(self.taker_a.pk, negative_debt_snapshot())

    def test_a_sales_negative_row_does_not_enter_the_snapshot(self):
        """⭐ 范围改准（业务方 2026-11-08）：客服（销售提成档）负行不进快照；
        同一人补一条打手（打单工资档）负行后照常命中——判据按**行**的 emp_type 收窄。"""
        self._debt_row(self.taker_a, emp_type=EmpType.SALES)
        self.assertNotIn(self.taker_a.pk, negative_debt_snapshot())
        self._debt_row(self.taker_a)
        self.assertIn(self.taker_a.pk, negative_debt_snapshot())


class NetNegativeExclusionTests(_DebtFixture, TenantTestCase):
    """② 净负判定 + 排除整人 + 去重口径 + 默认态负单可见（净负排除语义 2026-11-08 不变）。"""

    def setUp(self):
        self._seed()

    def _compute(self):
        return _compute_net_negative(negative_debt_snapshot(), Performance.objects.all())

    def test_a_window_positive_player_with_a_small_debt_stays_positive(self):
        """窗口正行 +100、债行 -20 ⇒ S=+80 ⇒ 不排（「正收敛」对照）。"""
        self._perf(self._order(), self.taker_a, temp="100.00", bonus="100.00")
        self._debt_row(self.taker_a, temp=Decimal("-20.00"), bonus=Decimal("-20.00"))
        self.assertNotIn(self.taker_a.pk, self._compute())

    def test_an_out_of_window_negative_row_makes_the_player_net_negative(self):
        """窗口内 +10、窗口外 -30 ⇒ S=-20 ⇒ 净负 ⇒ 勾排除后**整人**不可见
        （排除就是看不到这个人——他的全部行一起消失）。"""
        keep = self._perf(self._order(), self.taker_a, temp="10.00", bonus="10.00")
        debt = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        self.assertIn(self.taker_a.pk, self._compute())

        rows = self._nos({"exclude_net_negative": "1"})
        self.assertNotIn(keep.order.order_no, rows)
        self.assertNotIn(debt.order.order_no, rows)
        # ⭐ 默认态（不勾排除）：负单**可见**（「负单永远可见」由默认态保证）。
        rows_default = self._nos()
        self.assertIn(keep.order.order_no, rows_default)
        self.assertIn(debt.order.order_no, rows_default)

    def test_a_window_negative_row_is_counted_exactly_once(self):
        """去重口径：负行在窗口内只计一次。K = 债行(-20) + 窗口正行 +15 与 +10
        ⇒ 真 S=+5（不排）；若把窗口侧的债行再计一次 ⇒ S=-15（误排）——成员集合翻转可辨。"""
        self._debt_row(self.taker_a, temp=Decimal("-20.00"), bonus=Decimal("-20.00"))
        self._perf(self._order(), self.taker_a, temp="15.00", bonus="15.00")
        self._perf(self._order(), self.taker_a, temp="10.00", bonus="10.00")
        self.assertNotIn(self.taker_a.pk, self._compute())


class OrderNoSkipTests(_DebtFixture, TenantTestCase):
    """⑦ 按单查询跳过挂负账段（2026-11-08 热修口径保留）：发 `order_no` ⇒ 快照/净负
    排除**都不取**——订单详情只看本单的行；净负排除会整人抽行，按单视图里一样是
    「这单的行消失」。撤腿②后本跳过覆盖的只剩净负排除与红签集，行为判据不变。"""

    def setUp(self):
        self._seed()

    def test_order_no_query_returns_only_that_orders_own_rows(self):
        """发 order_no 查某单 ⇒ 只回该单真实行；挂负账打手的债行不灌入。"""
        real = self._perf(self._order(), self.taker_a)
        debt = self._debt_row(self.taker_b)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        rows = self._nos(dict(WINDOW, order_no=real.order.order_no))
        self.assertEqual(rows, [real.order.order_no])

    def test_net_negative_takers_row_stays_visible_under_order_no(self):
        """该单打手是净负 ⇒ 勾排除后按单查询下其行**仍显示**（净负排除同样跳过）。"""
        real = self._perf(self._order(), self.taker_a)
        debt = self._debt_row(self.taker_a)
        Performance.objects.filter(pk=debt.pk).update(created_at=_days_ago(90))
        rows = self._nos(
            {"order_no": real.order.order_no, "exclude_net_negative": "1"}
        )
        self.assertEqual(rows, [real.order.order_no])
        # 对照（判别力）：不按单查、同一排除参数 ⇒ 整人排除照常生效（taker_a 两行全消失）。
        self.assertEqual(self._nos({"exclude_net_negative": "1"}), [])


class ParameterContractTests(_DebtFixture, TenantTestCase):
    """③ 参数契约（M-2）：哨兵必须在序列化器显式声明——表外值 1000、缺省 = 关。"""

    def setUp(self):
        self._seed()

    def test_out_of_table_values_are_1000(self):
        for params in (
            {"exclude_net_negative": "abc"},
            {"exclude_net_negative": "0"},
            {"settle_forbidden_scope": "only"},
            {"settle_forbidden_scope": "all"},
        ):
            body = self.finance.get(URL, params).json()
            self.assertEqual(body["code"], 1000, params)

    def test_absent_param_never_activates_the_exclusion(self):
        """S-1 锚句的用例形：缺省不发参 ⇒ validate 不跑 ⇒ 分支跳过（负行照常可见）。"""
        debt = self._debt_row(self.taker_a)
        self.assertIn(debt.order.order_no, self._nos())


class PlayerDomainScopeTests(_DebtFixture, TenantTestCase):
    """⑤ 范围=仅打手域（emp_type=打单工资，业务方 2026-11-08 拍板）：客服（销售提成档）
    负行不进快照、红签不亮、净负不排。"""

    def setUp(self):
        self._seed()
        self.sales = User.objects.create_user(username="debt-sales")

    def _out_of_window(self, *rows):
        for row in rows:
            Performance.objects.filter(pk=row.pk).update(created_at=_days_ago(90))

    def _compute(self):
        return _compute_net_negative(negative_debt_snapshot(), Performance.objects.all())

    def test_sales_rows_are_visible_but_never_tagged_and_player_tab_tags_its_debt_row(self):
        """SALES 负行在客服 tab 照常可见（纯筛选）但红签恒 false（不进快照）；
        打手 tab 的窗口内债行照常亮签（对照半边）。"""
        sales_neg = self._debt_row(self.sales, emp_type=EmpType.SALES)
        sales_pos = self._perf(
            self._order(), self.sales, emp_type=EmpType.SALES
        )
        debt = self._debt_row(self.taker_a)

        sales_marks = {
            r["order_no"]: r["negative_debt"]
            for r in self._body(dict(WINDOW, emp_type="销售提成"))["items"]
        }
        self.assertIn(sales_neg.order.order_no, sales_marks)
        self.assertIn(sales_pos.order.order_no, sales_marks)
        self.assertFalse(any(sales_marks.values()))
        main_marks = {
            r["order_no"]: r["negative_debt"]
            for r in self._body(dict(WINDOW, emp_type="打单工资"))["items"]
        }
        self.assertTrue(main_marks[debt.order.order_no])
        self.assertNotIn(sales_neg.order.order_no, main_marks)

    def test_a_debt_players_sales_rows_do_not_light_the_badge(self):
        """红签钉在**行**上：挂负账打手（MAIN 债行在快照）名下的销售提成行不亮红签。"""
        debt = self._debt_row(self.taker_a)
        sales_row = self._perf(self._order(), self.taker_a, emp_type=EmpType.SALES)
        marks = {r["order_no"]: r["negative_debt"] for r in self._body()["items"]}
        self.assertTrue(marks[debt.order.order_no])
        self.assertFalse(marks[sales_row.order.order_no])

    def test_net_negative_exclusion_ignores_sales_rows(self):
        """净负只按打手域算：打手窗口内 MAIN 正行 +10、窗口外 SALES 负行 -30
        ⇒ 旧行为 S=-20 会整人误排，改准后 S=+10 不排、排除勾选下照常可见。"""
        keep = self._perf(self._order(), self.taker_a, temp="10.00", bonus="10.00")
        sales_neg = self._debt_row(self.taker_a, emp_type=EmpType.SALES)
        Performance.objects.filter(pk=sales_neg.pk).update(created_at=_days_ago(90))
        self.assertNotIn(self.taker_a.pk, self._compute())
        rows = self._nos({"exclude_net_negative": "1"})
        self.assertIn(keep.order.order_no, rows)
        self.assertIn(sales_neg.order.order_no, rows)

    def test_sales_negative_rows_never_enter_the_net_negative_set(self):
        """纯客服净负（唯一行就是 SALES 负行）不进净负集：快照（A 项）与窗口（B 项）
        两处都不认销售提成行。"""
        self._debt_row(self.sales, emp_type=EmpType.SALES)
        self.assertNotIn(self.sales.pk, self._compute())


class ExportAgreesWithListTests(_DebtFixture, TenantTestCase):
    """⑥ 导出与列表同源（照 `test_export_and_list_agree_on_the_excluded_scope` 先例；
    纯筛选下两处吃同一筛选函数 ⇒ 行集一致）。"""

    def setUp(self):
        self._seed()

    def test_export_and_list_agree_under_pure_filtering(self):
        """窗口内债行两处**都在**；窗口外债行两处都**不在**（撤腿②后「导出带强制行」
        的旧行为随之消失，2026-11-08）。导出端点的门是 finance.export_performance（133）
        ⇒ 夹具换持该门的客户端。"""
        exporter = make_logged_in_client(
            username="debt-exporter",
            groups=("债务导出员",),
            nodes=PERF_MANAGEMENT_NODES + ("finance.export_performance",),
        )
        grant_games(User.objects.get(username="debt-exporter"), self.game)
        debt_in = self._debt_row(self.taker_a)
        debt_out = self._debt_row(self.taker_b)
        Performance.objects.filter(pk=debt_out.pk).update(created_at=_days_ago(90))
        cells = values_of(
            workbook_of(exporter.get(EXPORT_URL, WINDOW)), "绩效明细"
        )
        self.assertIn(debt_in.order.order_no, cells)
        self.assertNotIn(debt_out.order.order_no, cells)
        rows = self._nos(WINDOW)
        self.assertIn(debt_in.order.order_no, rows)
        self.assertNotIn(debt_out.order.order_no, rows)


class NegativeDebtCacheTests(_DebtFixture, TenantTestCase):
    """④ 缓存（S-6：勾选参数下的请求；TTL=0 直通；跨租户键不串）。"""

    def setUp(self):
        self._seed()

    @override_settings(PERFORMANCE_LIST_CACHE_TTL_S=45)
    def test_same_signature_second_request_runs_zero_aggregate_sql(self):
        self._debt_row(self.taker_a)
        params = {"exclude_net_negative": "1"}
        with CaptureQueriesContext(connection) as first:
            self.finance.get(URL, params).json()
        self.assertTrue(
            any("SUM(" in q["sql"] for q in first.captured_queries),
            "前置哨兵失败：第一次请求应当真的打聚合",
        )
        with CaptureQueriesContext(connection) as second:
            self.finance.get(URL, params).json()
        leftover = [
            q["sql"]
            for q in second.captured_queries
            if "SUM(" in q["sql"] or "COUNT(" in q["sql"]
        ]
        self.assertEqual(leftover, [])

    def test_ttl_zero_passes_through_with_no_cache_keys(self):
        self._debt_row(self.taker_a)
        self._nos()
        survivors = [
            k for k in cache._expire_info if "perfnegdebt" in str(k) or "perfr3" in str(k)
        ]
        self.assertEqual(survivors, [])


class TenantIsolationDebtTests(_DebtFixture, TenantTestCase):
    """④ 的跨租户半边：B 租户的债行不串进 A 的快照 / 列表（键带 schema 前缀；
    ⚠️ 判据不用主键指认另一租户的行——用订单号/用户名这类租户内唯一的形状）。"""

    def setUp(self):
        self._seed()

    def test_the_other_tenants_debt_rows_do_not_leak(self):
        from apps.platform.provisioning import provision_tenant

        tenant_b, _ = provision_tenant(
            schema_name="debt_iso_b", name="债务隔离租户B", domain="debt-iso-b.test.local"
        )
        self._debt_row(self.taker_a)
        try:
            with tenant_context(tenant_b):
                game_b = Game.objects.create(name="债务隔离游戏B")
                order_b = Order.objects.create(
                    game=game_b,
                    order_no="20261107000000099001",
                    status=OrderStatus.COMPLETED,
                    total_price=Decimal("88.00"),
                )
                user_b = User.objects.create_user(username="debt-iso-user-b")
                Performance.objects.create(
                    order=order_b,
                    user=user_b,
                    emp_type=EmpType.MAIN,
                    temp_amount=NEG,
                    bonus_amount=NEG,
                    review_status=ReviewStatus.APPROVED,
                    settle_status=SettleStatus.UNSETTLED,
                )
                self.assertIn(user_b.pk, negative_debt_snapshot())
        finally:
            connection.set_tenant(self.tenant)
        snapshot_a = negative_debt_snapshot()
        self.assertIn(self.taker_a.pk, snapshot_a)
        names_a = {
            username
            for username in User.objects.filter(
                pk__in=snapshot_a.keys()
            ).values_list("username", flat=True)
        }
        self.assertIn("debt-taker-a", names_a)
        self.assertNotIn("debt-iso-user-b", names_a)
        self.assertNotIn("20261107000000099001", self._nos())
