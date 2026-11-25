"""绩效管理页 `/Emp/emplist` 后端契约（计划 §2.1 / §2.2）。

覆盖三件事：

- **A（§2.1）**：`PerformanceListSerializer` 新增的 12 个只读字段逐条钉值，含 `null` 边界
  （`received_price` 空 / `settled_at` 空 / `customer_ww` 空串）；
- **B（§2.1）**：`deduct_fee` 三种 `deduct_mode`（未设置 / 按比例 / 固定额）+ 负数（D-0015）
  + 越界（纯函数抛 `AmountError`）时的**读形状**：那一格 `null`、行留在列表里，
  而同一批数据 `POST /settlements/settle` **仍然整批拒绝**（读写两条口径的相邻对照）；
- **C（§2.2）**：`summary` 六项的算式（写死字面期望值）、**筛选后全集**（不是当前页）、
  空集为 `0.00`、以及「不传 `summary` 的既有调用者响应形状不变」。

三条纪律（与全仓同）：

- ⚠️ **期望值一律写字面值**（E-0024）：金额写 `"268.00"`、业务码写 `1000`、状态写 `"已结账"`，
  ⛔ 不引被测模块导出的常量（自指断言不会红）。
- ⚠️ **断言打在真实出口**：一律读 HTTP 响应体（列表 JSON / xlsx 字节 / 数据库行），
  ⛔ 不直接调 `performance_totals` / 序列化器内部方法当判据。
  xlsx 用 openpyxl 读单元格；对 zip 字节做文本解码会直接炸。
- ⚠️ **N+1 判据是「SQL 条数不随行数增长」**（实测 `CaptureQueriesContext`），
  ⛔ 不是某个具体数字——具体数字会被中间件 / 认证 / `SET search_path` 的条数变化带红。
"""
from datetime import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.core.envelope import paginated
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.xlsx_helpers import detail_rows, values_of, workbook_of

LIST_URL = "/api/business/performances/"
EXPORT_URL = "/api/business/performances/export"
ADJUST_URL = "/api/business/performances/{pk}/adjust/"
#: 结账端点（**无**尾斜杠）——「列表能看、结账仍拦」的对照要用它。
SETTLE_URL = "/api/business/settlements/settle"
#: 另一个用 `EnvelopePagination` 的端点（用来证明**没传** `summary` 的调用者形状不变）。
#: ⚠️ 对照用 `/api/business/games/`：`/orders/`（`D-0093`，spec §7.1 决策 C12）**有意**在
#: `data` 里多带一个 `summary` 统计块 ⇒ 不再是「没传 `summary` 的调用者」；
#: `/games/` 同样走 `EnvelopePagination` 且不设置统计块，是本条要的那个对照。
GAMES_URL = "/api/business/games/"

#: 列表角色闸门需要的节点（`my_performance.export`，字面值不引常量）。
LIST_NODE = "my_performance.export"
#: 数据范围节点：持它才算「管理侧」，列表才返回**全部**行
#: （不持 ⇒ 只看自己，见 `performance_views.PERFORMANCE_MANAGEMENT_NODE`）。
MANAGEMENT_NODE = "order.list"


class _EmpListFixture(TenantTestCase):
    """一份最小夹具：一个游戏、一个打手、一个超管 client（权限维度不混进字段用例）。

    ⚠️ 功能用例用**超级管理员**登录（与 `test_p4fu_performance_reads.py` 同处置）：
    本文件的关注点是字段值 / 算式 / 形状，权限维度由 `RedactionTests` 与既有
    `test_performance_api.py` 单独钉；超管让这些用例不随权限配置漂移。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="emp-taker")
        self.other = User.objects.create_user(username="emp-other")
        self._seq = 0
        self.client = make_logged_in_client(username="emp-list-admin", is_superuser=True)

    def _order(self, **overrides):
        self._seq += 1
        fields = {
            "game": self.game,
            # 20 位订单号（列宽 20）。
            "order_no": f"2026092300000{self._seq:07d}",
            "status": OrderStatus.IN_PROGRESS,
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _perf(self, order, *, user=None, **overrides):
        fields = {"emp_type": EmpType.MAIN, "temp_amount": Decimal("0.00")}
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _body(self, params=None):
        response = self.client.get(LIST_URL, params or {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _data(self, params=None):
        return self._body(params)["data"]

    def _items(self, params=None):
        return self._data(params)["items"]

    def _items_for(self, client, params=None):
        """用**指定 client**（非 `self.client` 那个超管）取列表 `items`。

        权限维度的用例必须用自己建的账号；`self.client` 是超管，走管理员短路 ⇒ 拿它测
        「非管理员那一支」等于什么都没测（本文件 `QueryCountTests` 的 M-2 用例就靠它）。
        """
        response = client.get(LIST_URL, params or {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]


# ── A：12 个新只读字段的值（含 null 边界）────────────────────────────


class NewFieldPayloadTests(_EmpListFixture):
    def test_every_new_field_carries_the_literal_value(self):
        """一行里把 9 个「有值」的字段一次钉完（金额是**字符串**形状，与 `temp_amount` 同源）。"""
        order = self._order(
            total_price=Decimal("268.00"),
            received_price=Decimal("200.00"),
            refund_fee=Decimal("8.00"),
            game_role_name="角色甲",
            project_text="原神代练",
            customer_ww="ww-abc",
            sales_agent="客服甲",
            created_by="录入员甲",
        )
        perf = self._perf(
            order,
            user=self.taker,
            temp_amount=Decimal("50.00"),
            deduct_mode="1",
            deduct_value=Decimal("10.00"),
            settled_at=timezone.make_aware(datetime(2026, 9, 23, 10, 30)),
            settled_by="财务甲",
            settled_amount=Decimal("40.00"),
        )

        row = self._items()[0]

        self.assertEqual(row["order_no"], order.order_no)
        self.assertEqual(row["order_price"], "268.00")       # order.total_price
        self.assertEqual(row["net_receipts"], "200.00")      # order.received_price
        self.assertEqual(row["refund_fee"], "8.00")          # order.refund_fee
        self.assertEqual(row["game_role"], "角色甲")           # order.game_role_name
        self.assertEqual(row["dl_project"], "原神代练")        # order.project_text
        self.assertEqual(row["wangwang"], "ww-abc")          # order.customer_ww
        self.assertEqual(row["kefu"], "客服甲")               # order.sales_agent
        self.assertEqual(row["log_user_name"], "录入员甲")     # order.created_by
        self.assertEqual(row["deduct_fee"], "10.00")         # 固定额
        self.assertEqual(row["settled_by"], "财务甲")
        self.assertEqual(row["settled_amount"], "40.00")
        self.assertEqual(row["settled_at"], "2026-09-23T10:30:00+08:00")
        # 结算留痕是**真实出口**：数据库行也确实是这样。
        perf.refresh_from_db()
        self.assertEqual(perf.settled_by, "财务甲")
        self.assertEqual(perf.settled_amount, Decimal("40.00"))

    def test_an_order_without_a_received_price_reports_null(self):
        """`received_price` 可空 ⇒ 没填是 **`null`**（不是 `0.00`）；空串列回**空串**。"""
        order = self._order()          # received_price 默认 None；几个文本列默认空串
        self._perf(order, user=self.taker)

        row = self._items()[0]

        self.assertIsNone(row["net_receipts"])
        # `total_price` / `refund_fee` 是 `null=False default=0.00` ⇒ 空单回 "0.00" 而不是 null。
        self.assertEqual(row["order_price"], "0.00")
        self.assertEqual(row["refund_fee"], "0.00")
        # 空串照原样出参（不翻译成 null）——与结算人空串同口径。
        self.assertEqual(row["game_role"], "")
        self.assertEqual(row["dl_project"], "")
        self.assertEqual(row["wangwang"], "")
        self.assertEqual(row["kefu"], "")
        self.assertEqual(row["log_user_name"], "")
        self.assertEqual(row["settled_by"], "")

    def test_unsettled_row_reports_null_time_and_null_amount(self):
        """未结账 ⇒ `settled_at` / `settled_amount` 都是 **`null`**；同一次响应里放一条
        结过账的做**正对照**（先证明有数据，再下否定断言——E-0004）。"""
        settled = self._perf(
            self._order(),
            user=self.taker,
            settled_at=timezone.make_aware(datetime(2026, 9, 23, 10, 30)),
            settled_by="财务甲",
            settled_amount=Decimal("40.00"),
        )
        unsettled = self._perf(self._order(), user=self.other)

        by_order = {row["order_no"]: row for row in self._items()}

        self.assertEqual(len(by_order), 2)
        self.assertIsNotNone(by_order[settled.order.order_no]["settled_at"])
        self.assertEqual(by_order[settled.order.order_no]["settled_amount"], "40.00")
        self.assertIsNone(by_order[unsettled.order.order_no]["settled_at"])
        self.assertIsNone(by_order[unsettled.order.order_no]["settled_amount"])
        self.assertEqual(by_order[unsettled.order.order_no]["settled_by"], "")


# ── B：deduct_fee 的三种模式 + 负数 + 越界 ───────────────────────────


class DeductFeeTests(_EmpListFixture):
    def _deduct(self, *, mode, value, temp="50.00"):
        order = self._order()
        self._perf(
            order,
            user=self.taker,
            temp_amount=Decimal(temp),
            deduct_mode=mode,
            deduct_value=Decimal(value),
        )
        return self._items()[0]["deduct_fee"]

    def test_mode_unset_is_zero_even_when_a_value_is_stored(self):
        """`mode=""` ⇒ **0.00**（不是把 `value` 当固定额扣掉）。

        ⚠️ 刻意让 `value` 非 0：这样「未设置」这条判据**真的在起作用**——
        去掉它就会走到「未知的扣点模式」而整表拒绝（用例变红），而不是被
        `value == 0` 那条短路遮住（那会是一条看不出差别的假用例）。
        """
        self.assertEqual(self._deduct(mode="", value="30.00"), "0.00")
        self.assertEqual(self._deduct(mode="", value="0.00"), "0.00")

    def test_percent_mode_computes_against_temp_amount(self):
        """`mode="0"`（按比例）：`temp_amount × value / 100` = `50.00 × 20 / 100`。"""
        self.assertEqual(self._deduct(mode="0", value="20.00"), "10.00")
        self.assertEqual(self._deduct(mode="0", value="100.00"), "50.00")   # 上界（含）
        self.assertEqual(self._deduct(mode="0", value="0.00"), "0.00")

    def test_fixed_mode_uses_the_value_itself(self):
        """`mode="1"`（固定额）：就是 `value`，与 `temp_amount` 无关（只要不越界）。"""
        self.assertEqual(self._deduct(mode="1", value="10.00"), "10.00")
        self.assertEqual(self._deduct(mode="1", value="50.00", temp="50.00"), "50.00")

    def test_negative_deductions_are_kept_verbatim(self):
        """⚠️ **D-0015**：扣点额可以是负数（冲抵 / 加钱）——谁加了 `≥ 0`，本用例变红。"""
        self.assertEqual(self._deduct(mode="1", value="-30.00"), "-30.00")
        self.assertEqual(self._deduct(mode="0", value="-20.00"), "-10.00")   # 50 × −20%

    def test_an_out_of_range_deduction_renders_as_null_not_a_broken_page(self):
        """固定额 > `temp_amount` ⇒ **整张列表照常 200 / code 0**，只有那一格是 `null`。

        ⭐ 裁定（计划 `docs/plans/2026-09-22-emp-emplist-completion.md` §2.1 +
        业务方「接不了就告诉我、别让页面坏掉」）：列表是财务**诊断坏行的唯一入口**，
        整页打不开比某格没数更糟 ⇒ 原先「整表回 `1000`」的口径**被推翻**。

        判据一次钉四条（缺一条都可能让「列表能用」变成假的）：
        ① 列表 200 + 业务码 `0`（⛔ 不是整表 `1000`）；
        ② **两行都在**（坏行没被藏掉）、正常行的 `deduct_fee` 仍是**字面值**；
        ③ 坏行的 `deduct_fee is None`（⛔ 不是 `"0.00"` 之类假数）；
        ④ `summary` 里没有扣点项（六项本来就不含扣点，所以越界行照样参与汇总）。

        这个状态**常见**且可达：退单会把 MAIN 行的 `temp_amount` 清 `0.00`（`refunds.py`
        第 2 步）而 `deduct_mode`/`deduct_value` 留着；`/adjust` 只改接单价也会让它变小。

        ⚠️ 期望值写**字面**；把 `DeductFeeField` 的 `except` 改回「抛出去」或改成
        `return Decimal("0.00")` ⇒ 本用例变红（变异证明见报告，E-0010 同族判据）。
        """
        bad = self._perf(
            self._order(),
            user=self.taker,
            temp_amount=Decimal("5.00"),
            deduct_mode="1",
            deduct_value=Decimal("40.00"),
        )
        good = self._perf(
            self._order(),
            user=self.other,
            temp_amount=Decimal("50.00"),
            deduct_mode="1",
            deduct_value=Decimal("10.00"),
        )

        body = self._body()

        self.assertEqual(body["code"], 0, body)
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertEqual(set(rows), {bad.order.order_no, good.order.order_no})  # ② 两行都在
        self.assertIsNone(rows[bad.order.order_no]["deduct_fee"])               # ③ 坏行 null
        self.assertEqual(rows[good.order.order_no]["deduct_fee"], "10.00")      # ② 正常行字面值
        # ④ 汇总六项里没有扣点项；越界行不是被排除，只是这一格没数（5 + 50）。
        self.assertEqual(
            set(body["data"]["summary"]),
            {
                "temp_amount", "bonus_amount",
                "order_price", "refund_fee", "profit_expected", "profit_actual",
            },
        )
        self.assertEqual(body["data"]["summary"]["temp_amount"], "55.00")

    def test_the_same_bad_row_still_makes_settle_refuse_the_whole_batch(self):
        """⚠️ **相邻对照**（别读成「能看就能结」）：同一批坏行下
        `POST /settlements/settle` **仍然整批拒绝**、一行都不落库。

        读、写两条路各有各的口径，且**刻意不同**：
        - 列表（上一条）走 `DeductFeeField` ⇒ 越界那一格 `null`，行照常渲染；
        - 结账走 `settlement_amount.payable_net`（`settlement.py::_row_nets` 把
          `AmountError` 翻成 `SettleError`）⇒ 业务码 `1000`，坏行与**同批的正常行都不动**。

        ⛔ 修「读要能看见」时**没有**顺手放宽写路径——本用例就是那条边界；
        删掉它，「列表改容忍」就会被后来者读成「越界也能结账」。
        """
        bad = self._perf(
            self._order(status=OrderStatus.COMPLETED),
            user=self.taker,
            temp_amount=Decimal("5.00"),
            bonus_amount=Decimal("5.00"),
            deduct_mode="1",
            deduct_value=Decimal("40.00"),
        )
        good = self._perf(
            self._order(status=OrderStatus.COMPLETED),
            user=self.other,
            temp_amount=Decimal("50.00"),
            bonus_amount=Decimal("50.00"),
            deduct_mode="1",
            deduct_value=Decimal("10.00"),
        )

        response = self.client.post(
            SETTLE_URL,
            {"performance_ids": [bad.id, good.id]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("预计绩效", body["msg"])
        self.assertIsNone(body["data"])
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.settle_status, "未结账")      # 整批回滚，不是半截成功
        self.assertEqual(good.settle_status, "未结账")

    def test_an_out_of_range_deduction_no_longer_breaks_the_export(self):
        """扣点越界的行**不让导出整批失败**（读路径容忍坏行）。

        原先这里回 `1000` 信封、一行数据都不给；现在导出照常出文件：两行**都在**、
        各自的预计额照旧。⛔ 不是半截文件、也不是 JSON 信封。

        明细表没有「扣点额」这一列，所以这里只钉两行都在、各自的预计额正确。
        `deduct_fee` 的读形状由本文件列表用例守。
        """
        bad = self._perf(
            self._order(),
            user=self.taker,
            temp_amount=Decimal("5.00"),
            deduct_mode="1",
            deduct_value=Decimal("40.00"),
        )
        good = self._perf(
            self._order(),
            user=self.other,
            temp_amount=Decimal("50.00"),
            deduct_mode="1",
            deduct_value=Decimal("10.00"),
        )

        response = self.client.get(EXPORT_URL)

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            response["Content-Type"],
        )
        rows = detail_rows(workbook_of(response))
        self.assertEqual(len(rows), 3)                        # 表头 + 两行
        by_order = {row[9]: row for row in rows[1:]}          # 第 10 列 = 系统内置单号
        self.assertEqual(set(by_order), {bad.order.order_no, good.order.order_no})
        self.assertEqual(Decimal(str(by_order[bad.order.order_no][1])), Decimal("5.00"))
        self.assertEqual(Decimal(str(by_order[good.order.order_no][1])), Decimal("50.00"))

    def test_lowering_the_price_below_a_fixed_deduction_is_rejected_before_any_write(self):
        """`/adjust` 只改接单价也会重算 `temp_amount` ⇒ 必须**写库之前**校验扣点仍可算。

        判据两条一起看（只看其一会漏）：
        ① 回业务码 `1000`（不是 500、也不是「改了却报错」）；
        ② **数据库一行都没变**——`temp_amount` 仍是 `50.00`、`taker_price` 仍是 `None`。
        去掉 `price_changed` 那个判据后：写会落库（① 可能仍绿，② 必红）。
        """
        perf = self._perf(
            self._order(hall_price=Decimal("250.00")),
            user=self.taker,
            temp_amount=Decimal("50.00"),
            deduct_mode="1",
            deduct_value=Decimal("40.00"),
        )

        body = self.client.post(
            ADJUST_URL.format(pk=perf.pk),
            {"taker_price": "0.00"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 1000, body)
        self.assertIn("预计绩效", body["msg"])
        perf.refresh_from_db()
        self.assertIsNone(perf.taker_price)
        self.assertEqual(perf.temp_amount, Decimal("50.00"))


# ── C：summary 六项（§2.2）──────────────────────────────────────────


class SummaryTests(_EmpListFixture):
    def _two_rows(self):
        """两行已知数：用来把六项算式**写死字面期望值**。"""
        order_a = self._order(total_price=Decimal("100.00"), refund_fee=Decimal("10.00"))
        order_b = self._order(total_price=Decimal("200.00"), refund_fee=Decimal("0.00"))
        self._perf(
            order_a, user=self.taker,
            temp_amount=Decimal("20.00"), bonus_amount=Decimal("15.00"),
        )
        self._perf(
            order_b, user=self.other,
            temp_amount=Decimal("50.00"), bonus_amount=Decimal("40.00"),
        )
        return order_a, order_b

    def test_the_six_amounts_match_the_literal_arithmetic(self):
        """六项一次钉完，**期望值全写字面值**（E-0024）。

        手算：`temp=20+50=70`；`bonus=15+40=55`；`order_price=100+200=300`；
        `refund=10+0=10`；`profit_expected=(100−20−10)+(200−50−0)=70+150=220`；
        `profit_actual=(100−15−10)+(200−40−0)=75+160=235`。
        ⚠️ `profit_expected` 与 `profit_actual` **刻意取不同的数**：把两者写成同一个算式
        （对标页面那处笔误）会让本用例变红。
        """
        self._two_rows()

        summary = self._data()["summary"]

        self.assertEqual(summary, {
            "temp_amount": "70.00",
            "bonus_amount": "55.00",
            "order_price": "300.00",
            "refund_fee": "10.00",
            "profit_expected": "220.00",
            "profit_actual": "235.00",
        })

    def test_a_two_row_order_counts_price_and_refund_once_per_order(self):
        """D-0255：利润改**按单**口径——同一张单的价与退款只计一次。

        一张「打手+客服」两行单（价 100、退款 20、两行提成 30/10；完成单
        `bonus == temp`）：
        - 新口径：`order_price=100`（价只计一次）、`refund_fee=20`（退款只计一次）、
          `temp_amount=bonus_amount=40`（提成仍按**行**求和）、
          两列利润 = (100−20) − (30+10) = **40.00**；
        - 旧口径（照对标 `#total` 逐行抄）：价/退款随两行各计一次（200/40）⇒
          逐行利润 (100−30−20)+(100−10−20) = **120.00**。
        判别力（变异）：去掉按单去重（`performance_totals` 里那个 `.distinct()`）⇒
        本条必红（六项回 40/40/200/20/120/120）。
        """
        order = self._order(total_price=Decimal("100.00"), refund_fee=Decimal("20.00"))
        self._perf(
            order, user=self.taker,
            temp_amount=Decimal("30.00"), bonus_amount=Decimal("30.00"),
        )
        self._perf(
            order, user=self.other,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00"),
        )

        summary = self._data()["summary"]

        self.assertEqual(summary, {
            "temp_amount": "40.00",
            "bonus_amount": "40.00",
            "order_price": "100.00",
            "refund_fee": "20.00",
            "profit_expected": "40.00",
            "profit_actual": "40.00",
        })

    def test_the_summary_is_the_whole_filtered_set_not_the_current_page(self):
        """⚠️ 核心判据：`page_size=1` 时 `items` 只有 1 行，而 `summary` 仍是**两行之和**。

        把 `summary` 打到当前页上会让这里变成 `temp_amount: "50.00"`（或 `"20.00"`），
        本用例变红——而只看「六项非空」的用例抓不到它。
        """
        self._two_rows()

        data = self._data({"page_size": 1})

        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["summary"]["temp_amount"], "70.00")
        self.assertEqual(data["summary"]["order_price"], "300.00")
        self.assertEqual(data["summary"]["profit_expected"], "220.00")

    def test_the_summary_follows_the_filters(self):
        """筛选后只剩一部分行 ⇒ `summary` 只算**这批**行（与 `items` 同一套筛选）。"""
        self._two_rows()

        data = self._data({"username": "emp-taker"})

        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["summary"]["temp_amount"], "20.00")
        self.assertEqual(data["summary"]["bonus_amount"], "15.00")
        self.assertEqual(data["summary"]["order_price"], "100.00")
        self.assertEqual(data["summary"]["refund_fee"], "10.00")
        self.assertEqual(data["summary"]["profit_expected"], "70.00")
        self.assertEqual(data["summary"]["profit_actual"], "75.00")

    def test_an_empty_filtered_set_reports_six_zeros_not_nulls(self):
        """空集 ⇒ 六项都是 `"0.00"`（⛔ **不是** `null`）：聚合对空集返回 `None`，必须收口。

        ⚠️ 先放一条**匹配不到**但真实存在的行，证明「空集」是筛选的结果、
        而不是端点坏了（E-0004 的对照）。
        """
        self._two_rows()

        data = self._data({"order_no": "20260923000000099999"})

        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["summary"], {
            "temp_amount": "0.00",
            "bonus_amount": "0.00",
            "order_price": "0.00",
            "refund_fee": "0.00",
            "profit_expected": "0.00",
            "profit_actual": "0.00",
        })

    def test_negative_amounts_are_summed_verbatim(self):
        """⚠️ **D-0015**：退款 / 亏损可以为负，汇总**照实**（⛔ 不取绝对值、不夹到 0）。"""
        order = self._order(total_price=Decimal("100.00"), refund_fee=Decimal("-5.00"))
        self._perf(
            order, user=self.taker,
            temp_amount=Decimal("30.00"), bonus_amount=Decimal("20.00"),
        )

        summary = self._data()["summary"]

        self.assertEqual(summary["refund_fee"], "-5.00")
        self.assertEqual(summary["profit_expected"], "75.00")    # 100 − 30 − (−5)
        self.assertEqual(summary["profit_actual"], "85.00")      # 100 − 20 − (−5)

    def test_export_carries_no_summary_row(self):
        """导出**明细表只有表头 + 数据行**：⛔ 不带页面 `#total` 那种汇总行。

        判据：`绩效明细` 的行数 = 表头 + 数据行。

        xlsx 是 zip，不能在字节里搜 `summary`。另一个工作表「绩效汇总」是按人聚合，
        不是页面那个 JSON。本判据只看明细表行数。
        """
        self._two_rows()

        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200, response.content[:200])
        workbook = workbook_of(response)
        # ⭐ 导出**恰好 4 张**（本批新增两张副表）——字面列表，⛔ 不放宽成 `toContain`。
        self.assertEqual(
            workbook.sheetnames,
            [
                "绩效明细",
                "绩效汇总",
                "页一支付宝批量付款文件上传模板",
                "打款跳过名单",
            ],
        )
        self.assertEqual(len(detail_rows(workbook)), 3)       # 表头 + 2 行


# ── C2：订单金额 / 退款 / 利润的**字段脱敏**（不持 `order.list` 的人看不到）────


class OrderMoneyMaskTests(_EmpListFixture):
    """⭐ 业务方报过的越权展示：打手在「我的绩效」看到了**订单金额**与**差额利润**。

    判据 = `PERFORMANCE_MANAGEMENT_NODE`（`order.list`，字面量 `MANAGEMENT_NODE`）：
    不持 ⇒ 汇总的四项与行上的订单价 / 实收 / 退款一并读成 `None`；
    持 ⇒ 照旧有值。**这是字段脱敏、不是 403**（不持的人本来就该能进列表、看自己的绩效）。

    ⚠️ 夹具刻意把提成（`temp_amount` / `bonus_amount`）与订单金额写成**不同的数**，
    这样「脱敏」与「整个 summary 没了」不会混淆；也刻意不断言 0 / 空串——
    `assertIsNone` 本身就把 `"0.00"` 与 `""` 排除掉了（0 会被当真值）。
    """

    def setUp(self):
        super().setUp()
        order = self._order(
            total_price=Decimal("268.00"),
            received_price=Decimal("200.00"),
            refund_fee=Decimal("8.00"),
        )
        # 两个账号都必须是「非管理员」：超管走 `nodes_of` 的全目录短路，
        # 拿它测「不持节点」那一支等于什么都没测（同 `_items_for` 的注释）。
        self.taker_client = make_logged_in_client(
            username="emp-mask-taker", groups=("代练员",), nodes=(LIST_NODE,)
        )
        self.manager_client = make_logged_in_client(
            username="emp-mask-cs", groups=("KFZ",), nodes=(LIST_NODE, MANAGEMENT_NODE)
        )
        # 绩效行必须挂在**那个打手账号**名下：不持 `order.list` 时数据范围收窄成
        # 「只看自己的行」，挂给别人本用例就只剩一个空列表（判不出脱敏）。
        self._perf(
            order,
            user=User.objects.get(username="emp-mask-taker"),
            temp_amount=Decimal("50.00"),
            bonus_amount=Decimal("40.00"),
        )
        for username in ("emp-mask-taker", "emp-mask-cs"):
            grant_games(User.objects.get(username=username), self.game)

    def _payload(self, client):
        response = client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_a_taker_gets_none_for_the_four_order_money_keys(self):
        """不持 `order.list`：四个金额键是 `None`（⛔ 不是 0、不是空串），提成照旧有值。"""
        data = self._payload(self.taker_client)

        self.assertEqual(len(data["items"]), 1)               # 正向控制：列表本身进得来
        summary = data["summary"]
        for key in ("order_price", "refund_fee", "profit_expected", "profit_actual"):
            with self.subTest(key=key):
                self.assertIsNone(summary[key])
        # 他自己挣的钱照旧给（业务口径：只收窄订单金额 / 差额利润这一类）
        self.assertEqual(summary["temp_amount"], "50.00")
        self.assertEqual(summary["bonus_amount"], "40.00")

        row = data["items"][0]
        for key in ("order_price", "net_receipts", "refund_fee"):
            with self.subTest(key=key):
                self.assertIsNone(row[key])
        self.assertEqual(row["temp_amount"], "50.00")
        self.assertEqual(row["bonus_amount"], "40.00")
        # 扣点额是**他自己**的钱（由 `temp_amount` 算出），不属于被脱敏的那一族。
        self.assertEqual(row["deduct_fee"], "0.00")

    def test_a_manager_still_gets_the_four_keys_with_values(self):
        """持 `order.list`（客服 / 管理员形态）⇒ 四项照旧有值（期望值写死字面量）。"""
        data = self._payload(self.manager_client)

        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["summary"], {
            "temp_amount": "50.00",
            "bonus_amount": "40.00",
            "order_price": "268.00",
            "refund_fee": "8.00",
            "profit_expected": "210.00",                       # 268 − 8 − 50
            "profit_actual": "220.00",                         # 268 − 8 − 40
        })

        row = data["items"][0]
        self.assertEqual(row["order_price"], "268.00")
        self.assertEqual(row["net_receipts"], "200.00")
        self.assertEqual(row["refund_fee"], "8.00")

    def test_a_finance_payroll_only_user_still_gets_the_four_keys(self):
        """⭐ 业务方拍板：判据是**两节点任一**——只持 `finance.payroll` 的财务
        也照旧看得到（财务看不到订单金额就没法对账，把真金额藏成 —/0.00 比不藏更糟）。

        ⚠️ 这条是**判据扩展**的判据：删掉 `can_view_order_money` 里的
        `PERFORMANCE_FINANCE_VIEW_NODE` ⇒ 本用例当场红（四个键变 `None`）。
        """
        finance_client = make_logged_in_client(
            username="emp-mask-finance", groups=("CW",), nodes=(LIST_NODE, "finance.payroll")
        )
        finance_user = User.objects.get(username="emp-mask-finance")
        grant_games(finance_user, self.game)
        # 财务**不持** `order.list` ⇒ 数据范围仍只看自己（`finance.payroll` 只解列、不放行）。
        # 所以给他自己那条绩效行，判据才能落在「四个键有值」上而不是空集。
        self._perf(
            self._order(
                total_price=Decimal("268.00"),
                received_price=Decimal("200.00"),
                refund_fee=Decimal("8.00"),
            ),
            user=finance_user,
            temp_amount=Decimal("50.00"),
            bonus_amount=Decimal("40.00"),
        )

        summary = self._payload(finance_client)["summary"]

        self.assertEqual(summary["order_price"], "268.00")
        self.assertEqual(summary["refund_fee"], "8.00")
        self.assertEqual(summary["profit_expected"], "210.00")
        self.assertEqual(summary["profit_actual"], "220.00")


# ── D：没有归属人的行**不进本页**（业务方现场提出，第四轮 A）─────


class NullUserRowsAreOutOfThisPageTests(_EmpListFixture):
    """`Performance.user IS NULL` 的行（订单录了但还没人接）**不出现在绩效管理页**。

    业务方原话：「只有订单关联的打手产生了绩效才可以生成到这里」。

    ⚠️ **这不是「录单即建绩效」被取消**（P3a D-0010）：行仍在库里，派单 / 抢单 / `/adjust`
    仍要用它（见 `test_the_row_is_still_in_the_database_and_still_reachable_by_adjust`）
    ——只是**不进本页**。⛔ 不要因为「库里查得到、列表查不到」就把它当脏数据清掉。

    判据打在三个真实出口上（列表 JSON / 导出 xlsx 单元格 / `summary`），并配反向对照
    （有归属人的行照常返回），排除「端点整体空了」这种假通过。
    """

    def _one_of_each(self):
        """造一行 `user=None` + 一行有归属人；两行 `emp_type` 相同 ⇒ 差异只在归属人。"""
        no_user = self._perf(
            self._order(total_price=Decimal("300.00")),
            user=None,
            temp_amount=Decimal("30.00"),
        )
        with_user = self._perf(
            self._order(total_price=Decimal("200.00")),
            user=self.taker,
            temp_amount=Decimal("20.00"),
        )
        return no_user, with_user

    def test_the_list_returns_only_the_row_with_a_user(self):
        no_user, with_user = self._one_of_each()

        data = self._data()

        self.assertEqual(data["total"], 1)
        self.assertEqual(
            [row["order_no"] for row in data["items"]], [with_user.order.order_no]
        )
        self.assertNotEqual(no_user.order.order_no, with_user.order.order_no)

    def test_the_export_xlsx_has_exactly_one_data_line(self):
        no_user, with_user = self._one_of_each()

        response = self.client.get(EXPORT_URL)

        self.assertEqual(response.status_code, 200, response.content[:300])
        workbook = workbook_of(response)
        rows = detail_rows(workbook)
        self.assertEqual(len(rows), 2)                         # 表头 + **1** 行
        self.assertEqual(rows[1][9], with_user.order.order_no)  # 第 10 列 = 系统内置单号
        self.assertNotIn(
            no_user.order.order_no, values_of(workbook, "绩效明细")
        )

    def test_the_summary_only_counts_the_row_with_a_user(self):
        self._one_of_each()

        summary = self._data()["summary"]

        self.assertEqual(summary["temp_amount"], "20.00")
        self.assertEqual(summary["order_price"], "200.00")

    def test_both_rows_match_the_same_emp_type_so_the_only_difference_is_the_user(self):
        """反向对照：按 `emp_type` 明筛时**有归属人的照常返回**。

        库里两行都命中同一个 `emp_type`（字面 2）⇒ 列表少的那一行只可能被「归属人」这条排掉，
        不是被别的条件顺带筛掉的（否则这条用例证明不了任何东西）。
        """
        no_user, with_user = self._one_of_each()

        self.assertEqual(Performance.objects.filter(emp_type=EmpType.MAIN).count(), 2)
        self.assertEqual(
            [row["order_no"] for row in self._items({"emp_type": EmpType.MAIN})],
            [with_user.order.order_no],
        )
        self.assertNotEqual(no_user.pk, with_user.pk)

    def test_the_row_is_still_in_the_database_and_still_reachable_by_adjust(self):
        """说明性断言：这不是跨租户过滤，也不是把行删了。

        两个方向：① 库里查得到（`user__isnull=True`）；② `/adjust`（detail 动作走
        `_base_queryset()`，**不过**本页的筛选函数）仍命中它 ⇒ 回的是它自己的
        「还没有接单人」1014，而**不是**「不存在或不可见」那条 404 派生形状。
        """
        no_user, _ = self._one_of_each()
        self.assertTrue(
            Performance.objects.filter(pk=no_user.pk, user__isnull=True).exists()
        )

        body = self.client.post(
            ADJUST_URL.format(pk=no_user.pk),
            {"deduct_mode": "1", "deduct_value": "0.00"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 1014, body)
        self.assertIn("还没有接单人", body["msg"])
        self.assertNotIn("不存在或不可见", body["msg"])


# ── C（信封）：不传 `summary` 的既有调用者形状不变 ────────────────────


class EnvelopeShapeTests(TestCase):
    """`paginated()` 的**可选**关键字：不传 ⇒ 键不进 payload（既有调用者逐字节不变）。"""

    def test_summary_is_absent_when_not_asked_for(self):
        self.assertEqual(
            paginated([], total=0, page=1, page_size=20),
            {
                "items": [],
                "total": 0,
                "page": 1,
                "page_size": 20,
            },
        )

    def test_summary_appears_verbatim_when_given(self):
        payload = paginated(
            [{"id": 1}], total=1, page=1, page_size=20,
            summary={"temp_amount": "0.00"},
        )
        self.assertEqual(
            payload,
            {
                "items": [{"id": 1}],
                "total": 1,
                "page": 1,
                "page_size": 20,
                "summary": {"temp_amount": "0.00"},
            },
        )


class OtherPaginatedEndpointTests(_EmpListFixture):
    def test_an_endpoint_that_does_not_ask_for_summary_keeps_four_keys(self):
        """真实出口的对照：不传 `summary` 的调用者 `data` 仍**恰好四个键**。

        ⚠️ 对照用 `/games/`：`/orders/`（`D-0093` / spec §7.1 决策 C12）**有意**多带一个
        统计块 ⇒ 不再是反例；订单列表的新形状由 `tests/test_order_list_payload.py` 钉住。
        """
        response = self.client.get(GAMES_URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            set(body["data"]), {"items", "total", "page", "page_size"}, body["data"]
        )


# ── 脱敏口径（读码结论 → 用例钉住）────────────────────────────────────


class RedactionTests(_EmpListFixture):
    """`order_price` / `net_receipts` / `refund_fee` **不脱敏**——这是读码结论，不是漏做。

    依据（逐处读过，行号会随文件变动，锚点写的是类/常量名）：

    - `apps/business/api/serializers.py::OrderSerializer` 用 `exclude = []`（模型全字段出参），
      而它的 `REDACTED_PRICE_FIELDS` **只列了 `hall_price`**（`can_view_offer_price`）；
      `total_price` / `received_price` / `refund_fee` 一个都不在里头。
    - 同文件 `OrderListSerializer.Meta.fields` 里有 `total_price`，而该类的
      `REDACTED_PRICE_FIELDS` **只列了 `hall_price` 那一项**（spec §5 / C6 起；此前它是
      继承来的空元组）⇒ 订单列表对 `total_price` 也不设限。
    - `apps/business/order_flags.py::ORDER_FLAG_CODES` 登记的四条 flag 是
      `restrict_to_self_created` / `restrict_view_taker_price` / `restrict_view_offer_price` /
      `restrict_edit_taker_price`——**没有**任何一条覆盖这三列。

    ⇒ 绩效列表照订单侧口径，**不给这三列加限制**（加了才是自造限制）。
    用例同时钉**两个方向**：受限者在这三个数上看到的与不受限者**逐字节相同**，
    而同一个受限者的 `taker_price` 确实是 `None`（证明这两条 flag 真的生效了——
    否则「看得到」这条断言是**空转**的）。
    """

    def _row_for(self, client):
        response = client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"][0]

    def test_the_order_endpoint_itself_does_not_restrict_these_three_amounts(self):
        """把读码结论打成**可跑的 oracle**：同一位受限者读**订单详情**时，
        这三列本来就能看到（订单侧唯一脱敏的是 `hall_price`）。

        这一条与下面那条互为证据：**同一批 flag** 在订单侧只挡住了 `hall_price`，
        在绩效侧也只挡住了 `taker_price`——所以「绩效列表不限制这三列」不是漏做，
        而是与订单侧同口径。
        """
        order = self._order(
            total_price=Decimal("268.00"),
            received_price=Decimal("200.00"),
            refund_fee=Decimal("8.00"),
            hall_price=Decimal("99.00"),
        )
        restricted = make_logged_in_client(
            username="emp-restricted-order-reader",
            groups=("客服",),
            nodes=(
                LIST_NODE,
                "order.view_detail",
                "order.restrict_view_offer_price",
            ),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据——本用例要走
        # `order.view_detail` 读 `self.game` 名下的订单详情，而 `make_logged_in_client`
        # 刻意不建 `StaffProfile`（没有档案 ⇒ 空集 ⇒ 详情回业务码 1002）。
        # 该用例的期望值一个字没改（三个金额仍是字面值、`hall_price` 仍是 None）。
        grant_games(
            User.objects.get(username="emp-restricted-order-reader"), self.game
        )

        response = restricted.get(f"/api/business/orders/{order.order_no}/")
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        row = body["data"]

        self.assertEqual(row["total_price"], "268.00")
        self.assertEqual(row["received_price"], "200.00")
        self.assertEqual(row["refund_fee"], "8.00")
        self.assertIsNone(row["hall_price"])          # 正向控制：抛单价确实被这条 flag 挡住了

    def test_the_two_view_flags_hide_taker_price_but_not_the_order_amounts(self):
        order = self._order(
            total_price=Decimal("268.00"),
            received_price=Decimal("200.00"),
            refund_fee=Decimal("8.00"),
        )
        self._perf(order, user=self.taker, taker_price=Decimal("66.00"))

        restricted = make_logged_in_client(
            username="emp-restricted-cs",
            groups=("客服",),
            nodes=(
                LIST_NODE,
                # 本用例要**看得到别人的行**才能比较脱敏前后 ⇒ 显式给管理侧节点
                # （不给的话数据范围把他的列表收窄成「只看自己」，行数为 0）。
                MANAGEMENT_NODE,
                "order.restrict_view_taker_price",
                "order.restrict_view_offer_price",
            ),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据——绩效列表按
        # 请求方的游戏权限收窄，不声明就恒空（同上面那条订单详情用例的处置）。
        grant_games(User.objects.get(username="emp-restricted-cs"), self.game)
        # ⚠️ 不受限的一侧用**超管**：两侧都挂 Group「客服」会**共用同一个 Role**
        #    （`_grant_nodes` 只做 `role.permissions.add`，不会移除），先建的 flag 会漏给后者
        #    ⇒ 「不受限」那一侧其实也被限制，正向控制变成假绿。
        #    `flag_enabled` 对 `is_superuser` 有短路（不施加任何限制型 flag），是**另一个**判据。
        unrestricted = make_logged_in_client(
            username="emp-unrestricted-admin", is_superuser=True
        )

        restricted_row = self._row_for(restricted)
        unrestricted_row = self._row_for(unrestricted)

        # 正向控制：两条 flag 确实生效（接单价被读成 None）。
        self.assertIsNone(restricted_row["taker_price"])
        self.assertEqual(unrestricted_row["taker_price"], "66.00")
        # 结论：订单金额三列**不受这两条 flag 影响**，两边逐字节相同。
        for key, expected in (
            ("order_price", "268.00"),
            ("net_receipts", "200.00"),
            ("refund_fee", "8.00"),
        ):
            with self.subTest(key=key):
                self.assertEqual(restricted_row[key], expected)
                self.assertEqual(restricted_row[key], unrestricted_row[key])


# ── N+1：SQL 条数不随行数增长 ────────────────────────────────────────


class QueryCountTests(_EmpListFixture):
    def test_the_list_query_count_does_not_grow_with_rows(self):
        """⭐ 12 个新字段里 8 个跨表读 `order.*`，必须走已有的 `select_related("order")`。

        判据：**1 行与 5 行的 SQL 条数完全相同**（实测 `CaptureQueriesContext`）。
        ⛔ 若哪天有人删掉 `performance_queryset` 里的 `select_related("order", "user")`，
        这条会红（每行各多一次订单查询 / 用户查询）。
        ⚠️ 判据是「不随行数增长」而不是某个具体数字：`summary` 的聚合查询、分页 `count()`、
        认证与 `SET search_path` 都是**常数条**，写死数字会被它们的变化带红（噪音）。
        ⚠️ 两侧探针**同不带 `order_no`**（1 行腿按 `user` 收窄）：2026-11-08 起 `order_no`
        会整体跳过挂负账 R3（少付快照聚合的常数查询），两侧不同基 ⇒ 等式恒假。
        """
        first = self._perf(
            self._order(total_price=Decimal("100.00"), refund_fee=Decimal("1.00")),
            user=self.taker,
            temp_amount=Decimal("20.00"),
        )

        with CaptureQueriesContext(connection) as one_row:
            items = self._items({"user": self.taker.pk})
            self.assertEqual(len(items), 1)
        one_count = len(one_row.captured_queries)

        for _ in range(4):
            self._perf(
                self._order(total_price=Decimal("200.00"), refund_fee=Decimal("2.00")),
                user=self.taker,
                temp_amount=Decimal("30.00"),
            )

        with CaptureQueriesContext(connection) as five_rows:
            items = self._items()
            self.assertEqual(len(items), 5)
        five_count = len(five_rows.captured_queries)

        self.assertEqual(
            five_count,
            one_count,
            "行数从 1 到 5 时 SQL 条数变了 ⇒ 新字段引入了 N+1：\n"
            + "\n".join(query["sql"] for query in five_rows.captured_queries),
        )

    def test_the_qr_permission_probe_is_cached_so_the_count_does_not_grow(self):
        """⭐ **非管理员**那一半：`get_wechat_qr_url` 的**逐实例判据缓存**必须真的生效。

        ⚠️ **为什么上面那条（超管）证明不了它**：`has_node_now` 对租户管理员**短路**、
        一条库都不查，所以把缓存那两行整段删掉，上面那条**照样绿**（独立评审实测）。
        缓存只在**非管理员**分支承重——那支每调一次都会经 `nodes_of` → `user_node_codes`
        查一次库（`apps/rbac/permissions.py`）。

        ⇒ 本用例换一个**非管理员**：持 `order.list`（`PERFORMANCE_MANAGEMENT_NODE`，数据范围
        放宽到全工作室 ⇒ 看得到多行）与 `my_performance.export`（列表端点的门），但**不持**
        `finance.payroll`（内置「客服 / 客服主管」正好是这个组合，见
        `apps/rbac/seed.py::BUILTIN_ROLE_MODULES`）。判据与上面那条同形：**1 行与 5 行的
        SQL 条数相同**；删掉缓存 ⇒ 5 行会比 1 行多出一批**随行数增长**的权限查询（每行一次
        `has_node_now` → `is_tenant_admin` + `user_node_codes`）⇒ 本用例变红
        （⚠️ 不写「多几条」这种数字：条数随权限判据的实现变动，写死会被一次重构证伪，E-0011）。
        ⚠️ 同时断言每行的字段值是 `None`（正向确认这条路径真的走到了判据，而不是被
        `assertEqual(len(items), 5)` 之外的东西短路）。
        """
        client = make_logged_in_client(
            username="emp-qr-count-cs",
            groups=("KFZ",),
            nodes=(LIST_NODE, MANAGEMENT_NODE),
        )
        first = self._perf(
            self._order(total_price=Decimal("100.00"), refund_fee=Decimal("1.00")),
            user=self.taker,
            temp_amount=Decimal("20.00"),
        )

        with CaptureQueriesContext(connection) as one_row:
            # 1 行腿按 `user` 收窄（不带 `order_no`）：order_no 请求 2026-11-08 起跳过
            # 挂负账 R3、与无筛选那一腿不同基（同上面那条的登记）。
            items = self._items_for(client, {"user": self.taker.pk})
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0]["wechat_qr_url"])
        one_count = len(one_row.captured_queries)

        for _ in range(4):
            self._perf(
                self._order(total_price=Decimal("200.00"), refund_fee=Decimal("2.00")),
                user=self.taker,
                temp_amount=Decimal("30.00"),
            )

        with CaptureQueriesContext(connection) as five_rows:
            items = self._items_for(client)
            self.assertEqual(len(items), 5)
            self.assertTrue(all(row["wechat_qr_url"] is None for row in items), items)
        five_count = len(five_rows.captured_queries)

        self.assertEqual(
            five_count,
            one_count,
            "行数从 1 到 5 时 SQL 条数变了 ⇒ `get_wechat_qr_url` 的逐实例判据缓存没生效"
            "（非管理员每行都会重查一次权限库）：\n"
            + "\n".join(query["sql"] for query in five_rows.captured_queries),
        )


class WechatQrUrlTests(_EmpListFixture):
    """图片存储线（2026-09-25）：绩效列表每行带一个**只读**的收款码短时效链接（spec §7 末行）。

    ⚠️ 判据钉**值**：没传过 ⇒ `None`；传过 ⇒ 是服务端签出来的 URL（含存储层给的路径）。
    ⛔ 不钉「键存在吗」——那只证明序列化器声明了字段。
    """

    def test_none_when_the_person_has_no_qr(self):
        self._perf(self._order(), user=self.taker)
        row = self._items()[0]
        self.assertIn("wechat_qr_url", row)
        self.assertIsNone(row["wechat_qr_url"])

    def test_signed_url_when_the_person_uploaded_one(self):
        from apps.accounts.models import StaffProfile
        from apps.core.storage import get_storage

        profile, _ = StaffProfile.objects.get_or_create(user=self.taker)
        key = f"test/users/{self.taker.pk}/wechat-qr.abc123.png"
        get_storage().put_bytes(key, b"\x89PNG\r\n\x1a\n", content_type="image/png")
        profile.wechat_qr_key = key
        profile.save(update_fields=["wechat_qr_key"])

        self._perf(self._order(), user=self.taker)
        row = self._items()[0]
        # 本机实现的签名 URL 指向我们自己的 blob 端点（生产是 COS 预签名，形状不同但同为短时效）
        self.assertIn("/api/media/blob/", row["wechat_qr_url"])
        self.assertIn(key, row["wechat_qr_url"])

    #: 本字段的准入节点：**A8 裁定「这个入口复用 `finance.payroll`」**（前端路由守卫
    #: `config/routePermissions.ts` 的 `agents-performance` 用的就是它），但路由守卫只挡页面、
    #: 挡不住接口 ⇒ 判据必须落在服务端。⚠️ 字面值不引常量（E-0024：自指断言不会红）。
    FINANCE_NODE = "finance.payroll"

    def _put_qr(self, user):
        """给这个账号的档案放一张收款码，返回对象名（`test/users/...`）。"""
        from apps.accounts.models import StaffProfile
        from apps.core.storage import get_storage

        profile, _ = StaffProfile.objects.get_or_create(user=user)
        key = f"test/users/{user.pk}/wechat-qr.{user.username}.png"
        get_storage().put_bytes(key, b"\x89PNG\r\n\x1a\n", content_type="image/png")
        profile.wechat_qr_key = key
        profile.save(update_fields=["wechat_qr_key"])
        return key

    def _viewer_with_own_qr(self, *, username, group, nodes):
        """建一个**看得到自己那一行**的账号，并在它**自己的档案**上放一张收款码。

        为什么行必须挂在自己名下：不持管理侧节点（`order.list`）的账号，列表数据范围
        收窄成 `user=自己`（`performance_views.performance_queryset`）⇒ 挂别人的行会得到
        0 行，`items[0]` 会 `IndexError`——那是**报错**，不是本节要的**判红**。

        ⚠️ 两个不同权限的账号一律给**不同组名**：节点挂在组共享的 `Role` 上
        （`tests/p5_auth_helpers.py::_grant_nodes`），同组会让先建的节点漏给后者。
        """
        client = make_logged_in_client(username=username, groups=(group,), nodes=nodes)
        user = User.objects.get(username=username)
        self._put_qr(user)
        self._perf(self._order(), user=user)
        return client

    def _only_row_for(self, client):
        response = client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)
        return items[0]

    def test_none_for_a_viewer_without_the_finance_payroll_node(self):
        """只持列表节点 `my_performance.export`（**代练员也有它**）⇒ 拿不到收款码链接。

        这是本字段的准入判据（A8：复用 `finance.payroll`）。⛔ 断言钉**值**：不是「键在不在」，
        而是**不许出现签名 URL**。
        """
        client = self._viewer_with_own_qr(
            username="emp-qr-taker", group="DLY", nodes=(LIST_NODE,)
        )
        row = self._only_row_for(client)
        self.assertIn("wechat_qr_url", row)
        self.assertIsNone(row["wechat_qr_url"])

    def test_none_for_someone_elses_qr_when_the_viewer_lacks_the_node(self):
        """持 `order.list`（数据范围放宽到**全工作室**）但不持 `finance.payroll` ⇒ 别人的收款码也读不到。

        这条**直接复现被报告的那个洞**：列表端点的门是 `my_performance.export`（代练员也有），
        行可见性再由 `order.list` 一放宽，载荷里就会出现**别人**的收款码签名 URL——
        前端路由守卫挡不住它（那是页面级的）。
        ⭐ D-0219后列表门是**任一即进**（1163 ∪ `order.list`）⇒ 本条的夹具
        仍持 1163、用例不受影响；而「只持 `order.list`」的账号如今**单凭它**就进得了列表，
        这个洞的暴露面反而更直接——本字段自己的判据（`finance.payroll`）承重不变。
        """
        client = make_logged_in_client(
            username="emp-qr-cs",
            groups=("KFZ",),
            nodes=(LIST_NODE, MANAGEMENT_NODE),
        )
        self._put_qr(self.taker)
        self._perf(self._order(), user=self.taker)
        row = self._only_row_for(client)
        # ⚠️ 「别人的」这件事必须落在断言里，不能只靠推理（夹具里只有 `self.taker` 有二维码）：
        #    只断言 `len(items)==1` + 值为 `None` 的话，夹具哪天变成「看的是自己的行」，
        #    这两条照样绿——而那已经不是本条要测的洞了。
        self.assertEqual(row["username"], "emp-taker")
        self.assertIsNone(row["wechat_qr_url"])

    def test_signed_url_for_a_viewer_with_the_finance_payroll_node(self):
        """正向控制：**非管理员**持 `finance.payroll` ⇒ 照旧拿到签名 URL。

        ⚠️ 与第一条负向用例**只差一个节点**（其余夹具逐字相同）⇒ 两条合起来才证明判据是这个
        节点本身，而不是「字段恒为 None」或管理员短路。
        """
        client = self._viewer_with_own_qr(
            username="emp-qr-finance", group="CWY", nodes=(LIST_NODE, self.FINANCE_NODE)
        )
        row = self._only_row_for(client)
        self.assertIn("/api/media/blob/", row["wechat_qr_url"])
