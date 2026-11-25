"""客服行**预计**绩效（`Performance.temp_amount`）的重算：算式 + 主挂点 + 显式挂点。

业务方原话：「打手与客服报酬的销售客服提成账单页面的**预计绩效一直是 0** 生效绩效
能正常算 需要修复 预计和生效规则可以采用**打手绩效逻辑**来生成即可」。

根因（计划 §1.4）：`performance.create_customer_performance` 按 Ruling P 把 SALES 行的
`temp_amount` 写成**字面量 `Decimal("0.00")`**（录单不读提成率），而全仓原先**没有**任何
重算它的写入点（`recompute_customer_commission` 只写 `bonus_amount` / `commission_rate` /
`review_status`）⇒ 那一列永远是 0。本批**不动录单**（Ruling P 仍然成立），改成在订单事件里
按「与打手同一套逻辑」重算它。

本文件钉两件事：

1. **算式**：客服预计 = `(净实收 − 打手**预计**绩效) × 客服提成率`
   （`settlement_hooks.customer_commission_amount`——与「生效」**共用**同一份实现，
   差别只在减项那一侧传 `temp_amount` 还是 `bonus_amount`）；
2. **挂点**：计划 §2 T1.2 那张表里的主挂点与显式挂点，各至少一条
   「走那条业务路径 → 断言 SALES 行的 `temp_amount` 等于预期**字面值**」的用例。

⚠️ 期望值一律写**字面十进制值**（E-0024：不从被测模块 import 常量当期望值——
自指断言在被断言的常量被改坏时不会红）。
⚠️ 夹具一律用**合成值**（E-0077）：单号 / 用户名 / 游戏名都是编的，⛔ 不是任何真实数据。

## 为什么新建本文件、⛔ 不塞进 `tests/test_customer_commission.py`

那个文件的文件头明写它**刻意一个 `import settlement_hooks` 都不写**（Ruling O #1 的判别力
实验）：只要测试文件自己 import 它，「`apps.py::ready()` 里漏了那行 import」这个变异就会被
**测试自己的 import** 掩盖 ⇒ 那条结构性判据退化。本文件要 `mock.patch` 该模块里的
`customer_commission_amount`（E-0029：打在**被查找的那个模块**上）⇒ 放进那个文件会削弱它。
故新开本文件。

## 挂点 → 用例对照（计划 §2 T1.2）

| 计划表 # | 落点（文件:函数） | 本文件的用例 |
|---|---|---|
| 主挂点 | `performance.recompute_order_amounts`（8 个打手重算入口共用） | `MainHangPointTests` |
| 1 | `order_writes.change_hall_price` → `/hall-price/` | `OrderEventHangPointTests.test_*hall_price*` |
| 2 / 4 | `order_writes.assign_order` / `performance.set_taker` → `/assign/` | `…test_assign_*` |
| 3 | `claim.claim_order` → `/claim/` | `…test_claim_*` |
| 5 | `performance.apply_amount_to_effective_base` → `/adjust-amount/` | `…test_adjust_amount_*` |
| 6 | `performance_views.adjust` 的 `price_changed` 支 → `/adjust/` | `…test_adjust_with_a_taker_price_*` |
| 7 | `performance._on_in_progress` → 进「代练中」 | `…test_in_progress_transition_*` |
| 8 | ⛔ 已删（原 `jidanpu_hall._write_b1_taker_amount` 百分比档，随租户级鸡蛋铺线整条拆除） | — |
| 9 | `performance_views.adjust` 的显式 `temp_amount` 直写 | `ExplicitWriteHangPointTests.test_*explicit_temp_amount*` |
| 10 | `order_writes.assign_order` 的显式 `fee` 直写 | `…test_*explicit_fee*` |
| 11 | `order_views.clear_taker` 的 `isDelEmp=Y` 归零 | `…test_*is_del_emp*` |
| 12 | `refunds.on_order_refunded` 第 2 步 | `RefundAndPoolHangPointTests.test_*refund*` |
| 13 | `performance._on_pool_entry` | `…test_*pool*` |
| 14 | ⛔ 已删（原 `jidanpu_hall._write_b1_taker_amount` 固定金额档，随租户级鸡蛋铺线整条拆除） | — |
| 15 | `settlement_hooks.on_order_completed` | `OrderEventHangPointTests.test_completion_*` |

## 本文件**没有**覆盖到的（如实登记，⛔ 不是遗漏）

- ⛔⭐ 表 #8 与表 #14 整条作废：`_write_b1_taker_amount` 与其所属的租户级鸡蛋铺线已整体
  拆除 ⇒ 这两条挂点**都不存在**，#14 的专门用例（`B1FixedAmountHangPointTests`）同批删除；
  主挂点那几组（表 #1–#7、#9–#13、#15）**一个字没改**。
- **表 #11 的 `isDelEmp=Y` 在本文件里钉的是「端到端结果」**：`/clear-taker/` 先经
  `apply_transition → _on_pool_entry`（表 #13），而 `_on_pool_entry` 里的
  `reset_taker_performance` 已经把 MAIN 行的 `user` 清掉 ⇒ 按
  `refresh_customer_estimate` 的第 3 条判据减项**已经**是 `0.00`，之后那句归零
  只可能让**同一个值**再算一次（判据 `!=` ⇒ 不写库）。用例的 docstring 里写明了这一点。
- **`orders/batch/recompute-commission/`（`order_writes.recompute_customer_commission_write`）
  未接本挂点**：它写的是 SALES 行的 `bonus_amount` / `commission_rate` / `review_status`
  （`recompute_customer_commission` 的地盘），**不写 MAIN 行的金额** ⇒ 按计划表第 16 条的口径
  （「还有没有直写 MAIN 行金额的路径」）不在本任务范围内。它今天**不会**刷新客服预计。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business import settlement_hooks
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.performance import recompute_order_amounts
from apps.business.performance_amount import AmountError
from apps.business.refunds import refund_set_total
from apps.business.settlement_amount import payable_net
from apps.business.settlement_hooks import refresh_customer_estimate
from tests.p5_auth_helpers import make_logged_in_client

#: 夹具的数字（字面值，⛔ 不从被测模块 import）：
#: 净实收 130.00、客服率 40%（利润百分比）、打手率 20%（订单价百分比）、发单价 88.00。
#: ⚠️ 「净实收 130.00」今天只体现在 `_order` 的 `total` 默认值里（`refund_fee` 默认 `0.00` ⇒
#: 净实收 = 总价）：合并 `main` 的 **D-0181** 之后退单**不再写** `refund_fee`
#: ⇒ 那个数不再等于「订单总价 − 退款额」以外的任何东西，原先那条 `NET` 常量已无引用点、
#: 随本次重设一并删除（⛔ 别用一条没人读的常量冒充判据）。
SALES_RATE = Decimal("40.00")
TAKER_RATE = Decimal("20.00")
HALL = Decimal("88.00")
#: 打手重算后的预计绩效：`88.00 × 20%`。
TAKER_TEMP = Decimal("17.60")
#: 据此算出的客服预计：`(130.00 − 17.60) × 40%`。
CUSTOMER_ESTIMATE = Decimal("44.96")

#: 本文件走到的节点门。本文件**没有**任何「没有权限」的用例 ⇒ 一次授全，
#: 避免每条用例各写一份互相抄漏的门清单（那不是本文件的判据）。
def _set_customer_rate(customer):
    """给客服行补上提成率快照（部分退款的前置闸：提成率为空 ⇒ `1009`；E-0029 的桩自证同族）。"""
    Performance.objects.filter(pk=customer.pk).update(commission_rate=SALES_RATE)
    customer.refresh_from_db()
    return customer


NODES = (
    "order.list",
    "order.view_detail",
    "order.change_status",
    "order.change_status_completed",
    "order.assign_taker",
    "order.submit_payout_price",
    "order.set_offer_price",
    "order.clear_taker",
    "grab_hall.take_order",
)


def _no(n):
    """20 位订单号（`lookup_value_regex = r"\\d{20}"`）。"""
    return f"20261003{n:012d}"


def _updated_at(pk):
    return Performance.objects.get(pk=pk).updated_at


class _EstimateBase(TenantTestCase):
    """共享夹具：一个游戏、一个客服（40% 利润百分比）、一个打手（20% 订单价百分比）。"""

    _UNSET = object()

    def setUp(self):
        self.game = Game.objects.create(name="客服预计绩效测试游戏")
        self.sales = User.objects.create_user(username="测试客服戊")
        StaffProfile.objects.create(
            user=self.sales,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.taker = User.objects.create_user(username="测试打手戊")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        #    ⇒ 本基线的打手要持本类的游戏（走 /assign/ 的用例才过得去那一闸；
        #    专钉：tests/test_order_assign_game_gate.py）。
        from tests.p5_auth_helpers import grant_games

        grant_games(self.taker, self.game)

    def _order(self, no, *, status=OrderStatus.IN_PROGRESS, total="130.00",
               hall="88.00", refund="0.00", sales_user=_UNSET):
        if sales_user is self._UNSET:
            sales_user = self.sales
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            total_price=Decimal(total),
            hall_price=Decimal(hall),
            refund_fee=Decimal(refund),
            sales_user=sales_user,
        )

    def _main(self, order, *, user, temp, bonus="0.00"):
        return Performance.objects.create(
            order=order,
            user=user,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
        )

    def _customer(self, order, *, temp="0.00", user=_UNSET):
        if user is self._UNSET:
            user = order.sales_user
        return Performance.objects.create(
            order=order,
            user=user,
            emp_type=EmpType.SALES,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal("0.00"),
        )

    def _client(self, username="测试客服戊登录"):
        """一个**发请求的**客服账号（既不是归属人、也不是接单人）。

        ⚠️ 默认 `games=` 让夹具把「当前租户的全部游戏」勾给它 ⇒ `setUp` 里必须先建好
        `self.game`、再由**用例内**调用本方法（`Game.objects.all()` 是**调用那一刻**取的）。
        """
        return make_logged_in_client(username=username, groups=("客服",), nodes=NODES)

    def _taker_client(self, username):
        """一个**既登录又是接单人**的账号（`/claim/` 的抢单人是 `request.user`）。

        ⚠️ 必须把它那份自动建出来的档案改成「订单价百分比 20%」：夹具默认给的
        `commission_value=0` 会按 Q3 裁定回落 **100%**，打手预计就不是 `17.60` 了。
        """
        client = make_logged_in_client(username=username, groups=("客服",), nodes=NODES)
        user = User.objects.get(username=username)
        StaffProfile.objects.filter(user=user).update(
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        return client, user


def _update_statements(captured):
    return [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE")
    ]


# ── ① `refresh_customer_estimate` 的六条判据（计划 §2 T1.1 的表）──────────────
class RefreshCustomerEstimateBranchTests(_EstimateBase):
    """函数级：六条判据各一条用例（第 3 与第 6 条各多一条边界）。"""

    def test_no_1_without_a_sales_user_nothing_is_written(self):
        """① `order.sales_user_id is None` ⇒ 回 `None`、不写（与录单那条路同口径）。

        **杀法**：去掉最前那句早退 ⇒ 会按「无归属人回落 100%」算出 `130.00`
        （`commission_percent_of(None)` 查不到档案）⇒ 哨兵值 `3.00` 被覆盖 ⇒ 红。
        """
        order = self._order(_no(101), sales_user=None)
        self._main(order, user=self.taker, temp="100.00")
        # 这一行是「归属人事后被清空」的存量形状（`create_customer_performance` 不会这么建）。
        customer = self._customer(order, temp="3.00", user=self.sales)
        self.assertIsNone(refresh_customer_estimate(order))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("3.00"))

    def test_no_2_without_a_customer_row_nothing_is_built(self):
        """② 该单没有 SALES 行 ⇒ 回 `None`、**不补建**（补建是「录单即建绩效」那条路的活）。"""
        order = self._order(_no(102))
        self._main(order, user=self.taker, temp="100.00")
        self.assertIsNone(refresh_customer_estimate(order))
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )

    def test_no_3_without_a_main_row_the_basis_is_zero(self):
        """③ 没有 MAIN 行 ⇒ 减项 `0.00`（容错读，⛔ 不抛 `DoesNotExist`）⇒ `130.00 × 40%`。

        **杀法**：把容错读改成 `main_performance_of`（缺行抛 `DoesNotExist`）⇒ 本用例 ERROR。
        """
        order = self._order(_no(103))
        customer = self._customer(order)
        self.assertEqual(refresh_customer_estimate(order), Decimal("52.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))

    def test_no_3b_a_main_row_without_a_taker_also_uses_zero(self):
        """③ 的第二个形状：MAIN 行**在**、但 `user` 为空 ⇒ 减项同样是 `0.00`。

        「没有接手人」时那一行上的 `temp_amount` 是**录单即建的全额估值**
        （`create_performance_for_order` 写的 `order.hall_price`），不是「某个打手的预计绩效」。

        **杀法**：判据只写 `taker is None`（漏掉 `user_id is None`）⇒ 会得
        `(130.00 − 100.00) × 40% = 12.00` ⇒ 本用例红（这条是它的判别力所在）。
        """
        order = self._order(_no(104))
        self._main(order, user=None, temp="100.00")
        customer = self._customer(order)
        self.assertEqual(refresh_customer_estimate(order), Decimal("52.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))

    def test_no_4_a_changed_value_is_persisted(self):
        """④ 可算且值变了 ⇒ 写 `temp_amount` 并回该值。

        ⚠️ 顺带钉住「**只**动 `temp_amount`」：`bonus_amount` / `commission_rate` /
        `review_status` 是 `recompute_customer_commission` 的地盘，本批一个字没改。
        """
        order = self._order(_no(105))
        self._main(order, user=self.taker, temp="100.00")
        customer = self._customer(order, temp="0.00")
        self.assertEqual(refresh_customer_estimate(order), Decimal("12.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("12.00"))
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.commission_rate)
        self.assertIsNone(customer.review_status)

    def test_no_5_an_unchanged_value_is_not_written(self):
        """⑤ 可算但值与库里相同 ⇒ **不写**（判据 `!=`，⛔ 不是真值）——避免把 `updated_at` 抖一遍。

        **判据打在真实出口上**：抓这一次调用发出的全部 SQL，断言**一条 `UPDATE` 都没有**；
        再断言 `updated_at` 没变（`save(update_fields=["temp_amount","updated_at"])` 会推它）。

        **杀法**：把 `if customer.temp_amount != amount:` 改成无条件 `save()` ⇒ 本用例红。
        ⚠️ 也顺带钉住「判据不是真值」：把判据写成 `if customer.temp_amount:` 时，
        「算出来正好是 `0.00`」的那一档会被当成「没变」而漏写（另一条用例
        `test_a_zero_estimate_is_written_when_the_row_held_a_non_zero_value` 钉它）。
        """
        order = self._order(_no(106))
        self._main(order, user=self.taker, temp="100.00")
        customer = self._customer(order, temp="12.00")
        before = _updated_at(customer.pk)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(refresh_customer_estimate(order), Decimal("12.00"))
        self.assertEqual(_update_statements(captured), [])
        row = Performance.objects.get(pk=customer.pk)
        self.assertEqual(row.temp_amount, Decimal("12.00"))
        self.assertEqual(row.updated_at, before)

    def test_a_zero_estimate_is_written_when_the_row_held_a_non_zero_value(self):
        """⑤ 的边界（E-0021 的同族）：算出来是 `0.00` 而库里不是 `0.00` ⇒ **必须写**。

        判据是 `customer.temp_amount != amount`（**值比**）。误写成「**新值**是不是真值」
        （`if amount:`）时，`amount == Decimal("0.00")` 的真值为假 ⇒ 这里静默漏写，
        而正面用例全绿。反向的误写（拿**旧值**当判据：`if customer.temp_amount:`）
        由 `test_no_5_an_unchanged_value_is_not_written` 抓（旧值非 0 ⇒ 恒写）。

        **杀法**：把判据写成 `if amount:` ⇒ 本用例红（实测 KILLED）。
        """
        order = self._order(_no(107), total="130.00", refund="130.00")
        self._main(order, user=self.taker, temp="0.00")
        customer = self._customer(order, temp="44.96")
        self.assertEqual(refresh_customer_estimate(order), Decimal("0.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))

    def test_no_6_a_mismatched_rate_returns_none_and_writes_nothing(self):
        """⑥ 算不出（提成模式不匹配）⇒ **不写、回 `None`、⛔ 不抛**。

        客服档案是**默认档** `ORDER_PERCENT`(20)（`StaffProfile` 的默认值）而客服路径要的是
        `PROFIT_PERCENT`(30) ⇒ `commission_percent_of` 抛 `CommissionError`。

        **杀法**：去掉 `try/except` ⇒ 本用例 ERROR（而 `/assign/`、`/claim/` 会在默认档下
        普遍失败 —— 见 `OrderPercentDefaultTests`）。
        """
        sales = User.objects.create_user(username="测试客服默认档")
        StaffProfile.objects.create(
            user=sales,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        order = self._order(_no(108), sales_user=sales)
        self._main(order, user=self.taker, temp="100.00")
        customer = self._customer(order, temp="3.00")
        before = _updated_at(customer.pk)
        with CaptureQueriesContext(connection) as captured:
            self.assertIsNone(refresh_customer_estimate(order))
        self.assertEqual(_update_statements(captured), [])
        row = Performance.objects.get(pk=customer.pk)
        self.assertEqual(row.temp_amount, Decimal("3.00"))
        self.assertEqual(row.updated_at, before)

    def test_no_6b_an_amount_error_is_swallowed_too(self):
        """⑥ 的第二个异常类型：`AmountError` 同样被吞掉（回 `None`、不写）。

        ⚠️ **打桩用例，如实登记**：这一半今天在真实路径上**不可达**——`net_received` 自
        D-0142 起只读 `total_price`（`null=False default=0.00`）⇒ 恒有值，
        而 `profit_of` / `commission_amount` 都不校验金额。它的判别力只能由本用例承担
        （⛔ 不是「已有路径覆盖」，E-0065 同族）。
        """
        order = self._order(_no(109))
        self._main(order, user=self.taker, temp="100.00")
        customer = self._customer(order, temp="3.00")
        with mock.patch.object(
            settlement_hooks,
            "customer_commission_amount",
            side_effect=AmountError("金额不可计算", 1000),
        ):
            self.assertIsNone(refresh_customer_estimate(order))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("3.00"))

    def test_a_sales_user_without_a_profile_falls_back_to_full(self):
        """无档案 ⇒ 回落 **100%**（`commission.py` 的 Q3 裁定，本批**一个字没改**）⇒ `100.00`。"""
        sales = User.objects.create_user(username="测试客服无档案")
        order = self._order(_no(110), sales_user=sales)
        self._main(order, user=self.taker, temp="30.00")
        customer = self._customer(order)
        self.assertEqual(refresh_customer_estimate(order), Decimal("100.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("100.00"))


# ── ② 主挂点：`recompute_order_amounts` ──────────────────────────────────
class MainHangPointTests(_EstimateBase):
    """主挂点（计划 §2 T1.2 第一段）：`recompute_order_amounts` 的 `save()` 之后。"""

    def test_the_main_row_recompute_refreshes_the_customer_estimate(self):
        """MAIN 行的重算 ⇒ 客服预计跟着走：`88.00 × 20% = 17.60` ⇒ `(130.00 − 17.60) × 40%`。

        **杀法**：删掉 `recompute_order_amounts` 末尾那两行 ⇒ 客服预计停在 `0.00` ⇒ 红。
        """
        order = self._order(_no(201))
        main = self._main(order, user=self.taker, temp="1.00")
        customer = self._customer(order, temp="0.00")
        recompute_order_amounts(main)
        main.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(main.temp_amount, TAKER_TEMP)
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)

    def test_a_sales_row_does_not_trigger_the_customer_refresh(self):
        """`emp_type == MAIN` 那一道判据是**承重**的：SALES 行到达本函数时**不**刷客服预计。

        `/adjust/` 与 `/adjust-amount/` 的 `get_object()` 走 `_base_queryset()`
        （只排掉 `user IS NULL`，**不**按 `emp_type` 收窄）⇒ 一条 SALES 行**真的**能到这里。
        去掉判据的话，那次重算会「先按打手算式写一列、再被客服算式覆盖同一列」。

        ⚠️ 夹具刻意让**两种算式给出不同的数**：SALES 行自己的 `user` 是「订单价百分比 20%」
        （打手算式 `88.00 × 20% = 17.60`），而 `order.sales_user` 是「利润百分比 40%」
        （客服算式 `(130.00 − 17.60) × 40% = 44.96`）⇒ 两个数可分辨。

        **杀法**：删掉判据 ⇒ 客服算式把 `17.60` 覆盖成 `44.96` ⇒ 红。
        """
        other = User.objects.create_user(username="测试销售行归属人")
        StaffProfile.objects.create(
            user=other,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        order = self._order(_no(202))
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp="0.00", user=other)
        # 给 SALES 行一个覆盖价，让它走打手算式时得到一个**与客服算式不同**的值。
        customer.taker_price = HALL
        customer.save(update_fields=["taker_price"])
        recompute_order_amounts(customer)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, TAKER_TEMP)


# ── ③ 订单事件挂点（走真实端点）─────────────────────────────────────────
class OrderEventHangPointTests(_EstimateBase):
    """计划表 #1–#7 与 #15：改价 / 指派 / 抢单 / 进代练中 / 完成 / 改接单价 / 改金额。"""

    def test_hall_price_recomputes_the_customer_estimate(self):
        """#1 `/hall-price/` 改成 `200.00` ⇒ 打手 `40.00` ⇒ 客服 `(130.00 − 40.00) × 40%`。"""
        order = self._order(_no(301))
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("36.00"))

    def test_assign_recomputes_the_customer_estimate(self):
        """#2 / #4 `/assign/` ⇒ `set_taker` 按人重算 MAIN ⇒ 客服预计跟着走。"""
        order = self._order(_no(302), status=OrderStatus.UNASSIGNED)
        self._main(order, user=None, temp=HALL)          # 录单时的全额估值
        customer = self._customer(order, temp="0.00")
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": self.taker.id}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)

    def test_claim_recomputes_the_customer_estimate(self):
        """#3 `/claim/`：抢单人就是 `request.user` ⇒ 按他的 20% 重算 MAIN ⇒ 客服预计跟着走。"""
        order = self._order(_no(303), status=OrderStatus.WAITING_GRAB)
        self._main(order, user=None, temp=HALL)
        customer = self._customer(order, temp="0.00")
        client, claimer = self._taker_client("测试抢单打手甲")
        body = client.post(
            f"/api/business/hall/{order.order_no}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, claimer.id
        )
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)

    def test_in_progress_transition_recomputes_the_customer_estimate(self):
        """#7 进「代练中」（`_on_in_progress` 钩子）⇒ 客服预计跟着走。"""
        order = self._order(_no(304), status=OrderStatus.PENDING_REVIEW)
        self._main(order, user=self.taker, temp="1.00")
        customer = self._customer(order, temp="0.00")
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)

    def test_completion_sets_the_estimate_and_the_effective_commission_to_the_same_value(self):
        """#15 订单完成：打手「完成即生效」（`bonus := temp`）⇒ 客服的**预计**与**生效**
        用同一个基数、同一个算式 ⇒ 两列同值（业务方 Q1 那句「与打手同一套逻辑」）。

        **杀法**：删掉 `on_order_completed` 里那句 `refresh_customer_estimate` ⇒
        客服预计停在 `0.00` 而生效是 `44.96` ⇒ 红。

        ⚠️ 来源态必须是 `PENDING_REVIEW`：F3之后**入 `COMPLETED` 只剩
        `待验收 → 已完成` 一条边**（`order_status.TRANSITIONS`），从「代练中」直接改完成会拿
        `1002`（「必须先把订单状态改成待验收才能改成已完成」）——那是**既有业务规则**，
        与本批无关。
        """
        order = self._order(_no(305), status=OrderStatus.PENDING_REVIEW)
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp="0.00")
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(main.bonus_amount, TAKER_TEMP)              # 完成即生效
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)    # 预计
        self.assertEqual(customer.bonus_amount, CUSTOMER_ESTIMATE)   # 生效（同一个数）
        self.assertEqual(customer.commission_rate, SALES_RATE)

    def test_adjust_amount_recomputes_the_customer_estimate(self):
        """#5 `/adjust-amount/` 填 V=30.00 ⇒ 反推基价 150.00 ⇒ 打手 `30.00` ⇒ 客服 `(130−30)×40%`。"""
        order = self._order(_no(306))
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/performances/{main.id}/adjust-amount/",
            {"amount_kind": "temp", "amount": "30.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("40.00"))

    def test_adjust_with_a_taker_price_recomputes_the_customer_estimate(self):
        """#6 legacy `/adjust/` 改 `taker_price=50.00` ⇒ 打手 `10.00` ⇒ 客服 `(130−10)×40%`。"""
        order = self._order(_no(307))
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/performances/{main.id}/adjust/",
            {"taker_price": "50.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("48.00"))


# ── ④ 显式写入挂点（主挂点看不见最终值的那几条）───────────────────────────
class ExplicitWriteHangPointTests(_EstimateBase):
    """计划表 #9 / #10 / #11：显式金额直写排在共用重算**之后** / 归零路径。"""

    def test_an_explicit_fee_on_assign_wins_over_the_recomputed_estimate(self):
        """#10 `/assign/` 带 `fee=25.00`、**不带** `price` ⇒ `fee` 就是最终值 ⇒ 客服 `(130−25)×40%`。

        ⚠️ 顺序：`set_taker` 先算 `17.60`（客服此刻被刷成 `44.96`），紧接着那次显式 `save`
        把它改成 `25.00` ⇒ 主挂点读到的**不是**最终值。
        **杀法**：删掉 `assign_order` 里那句补刷 ⇒ 客服停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(308), status=OrderStatus.UNASSIGNED)
        self._main(order, user=None, temp=HALL)
        customer = self._customer(order, temp="0.00")
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": self.taker.id, "fee": "25.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.temp_amount, Decimal("25.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("42.00"))

    def test_an_explicit_temp_amount_on_adjust_refreshes_after_the_write(self):
        """#9 `/adjust/` 显式送 `temp_amount=25.00`（不带 `taker_price`）⇒ 客服 `(130−25)×40%`。

        ⚠️ 这一档 `price_changed` 为假 ⇒ **根本不走** `recompute_order_amounts`
        ⇒ 主挂点完全看不到它。
        **杀法**：删掉 `performance_views.adjust` 里那句补刷 ⇒ 客服停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(309))
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/performances/{main.id}/adjust/",
            {"temp_amount": "25.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.temp_amount, Decimal("25.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("42.00"))

    def test_clear_taker_with_the_amount_flag_zeroes_the_basis(self):
        """#11 `/clear-taker/?isDelEmp=Y`：MAIN 两个金额列归零 ⇒ 客服预计 `130.00 × 40%`。

        ⚠️ **如实登记**：这一条钉的是**端到端结果**，它**区分不出** #11 那句补刷 ——
        `apply_transition → _on_pool_entry`（#13）里的 `reset_taker_performance` 先把
        MAIN 行的 `user` 清掉，而 `refresh_customer_estimate` 的第 3 条判据在
        `user` 为空时**已经**把减项按 `0.00` 算 ⇒ 那两次刷出的是同一个值
        （判据 `!=` ⇒ 第二次不写库）。
        **杀法**：删掉 `_on_pool_entry` 里那句（#13）⇒ 客服停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(310))
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/clear-taker/",
            {"isDelEmp": "Y"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.temp_amount, Decimal("0.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))

    def test_returning_to_the_pool_recomputes_the_estimate_with_no_taker(self):
        """#13 `_on_pool_entry`：回池后 MAIN 的 `user` 被清 ⇒ 减项 `0.00` ⇒ `130.00 × 40%`。

        **杀法**：删掉 `_on_pool_entry` 里那句 `refresh_customer_estimate` ⇒
        客服停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(311))
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/clear-taker/",
            {"isDelEmp": "N"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertIsNone(main.user_id)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))


# ── ⑤ 退款与回池（判据 2.5 / 表 #12 / #13）──────────────────────────────
class RefundedStatusCriterionTests(_EstimateBase):
    """判据 **2.5**（业务方口径）：`order.status == REFUNDED` ⇒ 预计一律 `0.00`。

    业务方原话（逐字）：「不应该啊 只要发生已退单 打手绩效和客服绩效都得清0啊」。
    本批只做**客服预计**那一半（打手那一半由 `on_order_refunded` 第 2 步清零，本批未动）；
    **生效**绩效那一半登记为 `D-0182` 的待决项（⛔ 本批不动 `bonus_amount`）。

    ⚠️ 两条用例都**直接从内存里的 REFUNDED 单**调 `refresh_customer_estimate`（不走 HTTP）：
    这样钉住的是**判据本身**，而端到端那一条（`RefundAndPoolHangPointTests`）钉的是
    「退款这条路真的会走到它」。两条各自的判别力见 docstring。
    """

    def test_a_refunded_order_gets_zero_instead_of_the_formula_value(self):
        """已退单 + 算式本可算出 `44.96`（`(130.00 − 17.60) × 40%`）⇒ 仍必须写 `0.00`。

        ⚠️ 夹具的**退单前值刻意非 0**（`44.96`）：从 `0.00` 到 `0.00` 的断言没有判别力。
        **杀法**：删掉 `refresh_customer_estimate` 里那条 `REFUNDED` 判据 ⇒ 本用例回到
        `44.96` ⇒ 红（实测读数见 `docs/STATUS.md` 本线条目）。
        """
        order = self._order(_no(321), status=OrderStatus.REFUNDED)
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        self.assertEqual(refresh_customer_estimate(order), Decimal("0.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))

    def test_a_refunded_order_gets_zero_even_when_the_rate_is_uncomputable(self):
        """判据 2.5 **排在算式之前**：提成模式算不出（默认档 `ORDER_PERCENT`）时也照样写 `0.00`。

        ⚠️ 这是「判据位置」本身的判据：第 6 条「算不出 ⇒ 不写、保留上一个值」服务的是**活单**，
        而一张已退单的单留着非 0 的估值就是一句假话（那一行上的钱已被冲到 `0.00`）。
        **杀法**：把那条 `REFUNDED` 判据挪到 `try/except` **之后**（或挪进 `else`）⇒ 本条
        因为「算不出 ⇒ 不写」而停在 `44.96` ⇒ 红。
        """
        StaffProfile.objects.filter(user=self.sales).update(
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("10.00"),
        )
        order = self._order(_no(322), status=OrderStatus.REFUNDED)
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        self.assertEqual(refresh_customer_estimate(order), Decimal("0.00"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))


class RefundAndPoolHangPointTests(_EstimateBase):
    """`on_order_refunded` 里那次补刷（#12）与 `_on_pool_entry` 那次补刷（#13）。

    ⚠️ 口径：退单不写 `refund_fee`（D-0181）；「已退单 ⇒ 打手绩效和客服绩效都得清 0」
    ⇒ 退单那一刻的客服预计**必须是 `0.00`**（判据落在 `refresh_customer_estimate` 的
    **订单状态**上，见那里的第 2.5 条；⛔ 不是靠退款钩子显式清这一列）。
    而「先退单再回池」对 #13 **没有判别力**（退单那一步已把 MAIN 两个金额列清成 `0.00`，
    回池再清一次是幂等的）⇒ #13 走 `问题单 → 池内` 那条**不经过退款钩子**的边。
    """

    def test_a_full_refund_zeroes_the_estimate_on_the_untouched_net_received(self):
        """#12 全额退款：退单不写 `refund_fee`，客服预计落到 `0.00`。

        夹具先给非 0 的预计（`44.96`），否则 `0.00 → 0.00` 没有判别力。
        未结账客服行会先被清成 0，所以删掉 `refresh_customer_estimate` 或它的
        `REFUNDED` 分支，本条仍然绿。判据 2.5 自己的判别力在
        `RefundedStatusCriterionTests`（直调、不经退单钩子）。
        本条钉的是端到端终态。杀法：不再清未结账客服行 ⇒ 预计停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(312))
        self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.REFUNDED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))          # D-0181：退单不写这一列
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))

    def test_the_sales_row_payable_net_is_still_zero_and_never_negative(self):
        """保证 ②：退单后 `settlement_amount.payable_net(SALES 行, 未结账调整)` 仍是 `0.00`、
        **不许变负**（负值会让 `/settle` 触发 `1011`）。

        代数：**未结账的行两列都被清 0**、第 3 步读到 `net = 0 + 空链 = 0`
        ⇒ **一条 `FULL_REFUND` 都不写** ⇒ 应付 = `0.00 − 0.00 + 0.00 = 0.00`
        （若 `bonus` 留在行上、由一条冲减抵平，终态同为 0.00——差别只在两者还在不在）。
        ⛔ 本用例**不**放宽「不许变负」那条：它照旧断言 `assertGreaterEqual(net, 0)`。
        **杀法**：去掉 `_clear_unsettled_sales_amounts`（两列停在 `44.96`）⇒ 若第 3 步照旧
        写 `−44.96`，本条仍绿但上面那条「两列清 0」的断言会红；把清 0 挪到第 3 步**之后**
        ⇒ 第 3 步写 `−44.96` 而 `bonus` 已被清 0 ⇒ `net = −44.96` ⇒ **本条红**。
        """
        order = self._order(_no(314), status=OrderStatus.PENDING_REVIEW)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        customer.bonus_amount = CUSTOMER_ESTIMATE
        customer.commission_rate = SALES_RATE
        customer.save(update_fields=["bonus_amount", "commission_rate", "updated_at"])
        client = self._client()
        body = client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        body = client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.REFUNDED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))       # 判据 2.5
        # ⭐ 业务方 Q1 口径：**未结账**的行 `bonus_amount` 也清 0。
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        adjustments = PerformanceAdjustment.objects.filter(
            performance=customer,
            settle_status=SettleStatus.UNSETTLED,
            kind=PerformanceAdjustment.Kind.FULL_REFUND,
        )
        # 清 0 在先 ⇒ 第 3 步的冲减额是 0 ⇒ 一条都不写（旧口径这里是 `−44.96`）。
        self.assertEqual([adjustment.amount for adjustment in adjustments], [])
        net = payable_net(customer, adjustments)
        self.assertEqual(net, Decimal("0.00"))
        self.assertGreaterEqual(net, Decimal("0.00"))                 # ⛔ 不许变负（/settle 会 1011）

    def test_a_refunded_order_returning_to_the_pool_and_redone_is_not_stuck_at_zero(self):
        """保证 ③：`退单 → 回池 → 重做完成` 这条链上，回池之后客服预计要**重新变成活的值**。

        `REFUNDED → 池内` 那一刻订单状态已经不是 `REFUNDED` ⇒ 判据 2.5 不成立、走正常算式
        ⇒ 回池后 `(130.00 − 0.00) × 40% = 52.00`（MAIN 的 `user` 被 `reset_taker_performance`
        清掉、两个金额列在退单时已清零 ⇒ 减项 `0.00`）；重做完成（`/assign/` 带 `fee=25.00`
        ⇒ MAIN `25.00`）后 `(130.00 − 25.00) × 40% = 42.00`。

        ⛔ **本条钉的是「不是永久 0」**：若把判据 2.5 写成「一旦退过单就永远是 0」
        （例如把它绑在 `OrderRefund` 行数 / 订单历史而不是**此刻的状态**上），回池那一格
        会停在 `0.00`、本条当场红。
        """
        order = self._order(_no(315), status=OrderStatus.PENDING_REVIEW)
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp="0.00")
        client = self._client()
        # ① 完成 → 预计与生效同值
        body = client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)
        # ② 退单 → 预计 0.00
        body = client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.REFUNDED}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("0.00"))
        # ③ 回池（`REFUNDED → UNASSIGNED`；`/clear-taker/` 就是「显式清除接单人」那个动作，
        #    它自己打开 D2 的 `allow_taker_release`）⇒ 预计重新变活
        body = client.post(
            f"/api/business/orders/{order.order_no}/clear-taker/",
            {"isDelEmp": "N"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        main.refresh_from_db()
        self.assertIsNone(main.user_id)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))
        self.assertNotEqual(customer.temp_amount, Decimal("0.00"))
        # ④ 重做完成（指派 25.00 → 待验收 → 已完成）⇒ 预计跟着新基数走
        body = client.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": self.taker.id, "fee": "25.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        for target in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            body = client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": target}, content_type="application/json",
            ).json()
            self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("25.00"))
        self.assertEqual(customer.temp_amount, Decimal("42.00"))
        self.assertNotEqual(customer.temp_amount, Decimal("0.00"))

    def test_an_issue_order_returning_to_the_pool_gets_a_fresh_estimate(self):
        """#13 `_on_pool_entry`：走 `问题单 → 池内`（这条边**不经过**退款钩子）——
        MAIN 行的 `temp_amount` 还在（`17.60`），而 `reset_taker_performance` 把 `user` 清掉
        ⇒ 按 `refresh_customer_estimate` 的第 3 条判据减项从 `17.60` 变 `0.00`
        ⇒ 客服预计 `(130.00 − 0.00) × 40% = 52.00`（退款前是 `44.96`）。

        ⚠️ **为什么本条不走「先退单再回池」**（合并 `main` 的 D-0181 之后）：退单那一步
        （#12）已经把 MAIN 的 `temp_amount` 清成 `0.00`，于是回池时减项**本来**就是 `0.00`
        ⇒ 两次刷出同一个 `52.00`（判据 `!=` ⇒ 第二次不写库）⇒ **删掉 `_on_pool_entry` 那句
        补刷也不会红**（那条路今天已无判别力，如实登记而不是留着一条假保证）。
        `问题单 → 池内` 这条边不经过退款钩子 ⇒ 减项真的从 `17.60` 变 `0.00`。
        ⚠️ 走的是 `/clear-taker/`（`isDelEmp=N`）：迁入池内要求「显式清除当前接单人」
        那个产品动作（`allow_taker_release`），而 `/clear-taker/` 正是它的实现。
        **杀法**：删掉 `_on_pool_entry` 里那句补刷 ⇒ 客服停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(313), status=OrderStatus.ISSUE)
        main = self._main(order, user=self.taker, temp=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/clear-taker/",
            {"isDelEmp": "N"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertIsNone(main.user_id)                              # 减项归 0 的那一句
        self.assertEqual(main.temp_amount, TAKER_TEMP)               # `temp_amount` 刻意不动
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("52.00"))


# ── ⑥ ⛔ 原「鸡蛋铺同步线的固定金额档（表 #14）」整组退场 ──────────
# 原 `B1FixedAmountHangPointTests` 测的是 `jidanpu_hall._write_b1_taker_amount` 的
# **固定金额档**（直写 MAIN、不过共用重算）⇒ 该函数随租户级鸡蛋铺线整条拆除
# （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`），本组两条用例一并删除
# （它们的被测入口已不存在，留着只会得到「对不存在的东西打桩」的 ERROR）。
# ⚠️ **百分比档**那条路（走 `recompute_order_amounts`）不受影响，它的网在下面的
# 「主挂点」那几组里，一个字没改。


# ── ⑦ ⭐ 本批最关键的防回归：客服档案是**默认档**时订单动作照样成功 ──────────
class OrderPercentDefaultTests(_EstimateBase):
    """客服档案模式 = `ORDER_PERCENT`（`StaffProfile` 的**默认值**）时，`/assign/` 与 `/claim/`
    **照样成功**——预计算不出**不能**把订单动作带崩（计划 §2 T1.1 第 6 条的判别用例）。

    为什么这是本批最危险的回归：客服路径要 `PROFIT_PERCENT`，而**默认档是 `ORDER_PERCENT`**
    ⇒ 任何未配置过的客服都会让 `commission_percent_of` 抛 `CommissionError`。
    若 `refresh_customer_estimate` 跟着抛，**指派与抢单在这个项目里会普遍失败**。
    「预计这一侧」的正确选择是「算不出就不写、保留上一个值」（⛔ 不是写 0、也⛔ 不是猜一个数）。
    """

    def _default_mode_sales(self):
        sales = User.objects.create_user(username="测试客服默认档乙")
        StaffProfile.objects.create(
            user=sales,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        return sales

    def test_assign_still_succeeds_and_keeps_the_previous_estimate(self):
        """`/assign/` ⇒ `code == 0`、订单真的被指派、客服预计**保留旧值** `7.00`。"""
        order = self._order(_no(501), status=OrderStatus.UNASSIGNED,
                            sales_user=self._default_mode_sales())
        main = self._main(order, user=None, temp=HALL)
        customer = self._customer(order, temp="7.00")
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": self.taker.id}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(main.user_id, self.taker.id)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("7.00"))

    def test_claim_still_succeeds_and_keeps_the_previous_estimate(self):
        """`/claim/` 同形：`code == 0`、订单被抢到、客服预计保留旧值 `7.00`。"""
        order = self._order(_no(502), status=OrderStatus.WAITING_GRAB,
                            sales_user=self._default_mode_sales())
        main = self._main(order, user=None, temp=HALL)
        customer = self._customer(order, temp="7.00")
        client, claimer = self._taker_client("测试抢单打手乙")
        body = client.post(
            f"/api/business/hall/{order.order_no}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(main.user_id, claimer.id)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("7.00"))


# ── ⑧ ⭐ M-1：SALES 行 `temp_amount` 由 0 变正数之后，**扣点基数**跟着变（连带面钉住）──
class SalesDeductionBaseTests(_EstimateBase):
    """`settlement_amount.payable_net` 的扣点基数是 `performance.temp_amount`
    （`payable_net` 里那句 `base=Decimal(performance.temp_amount)`）。

    客服行那一列在 `E-0184` 坑 A 修好之前**恒为 `0.00`**（录单写死、没有任何重算写入点）
    ⇒ 同一条扣点配置在客服行上的后果与打手行**完全不同**。本类把两条连带面各钉一条用例，
    期望值一律**字面十进制数**（E-0024），⛔ 不从被测模块 import 常量：

    - **比例档**（`deduct_mode="0"`）：扣点额 = `temp_amount × 比例 / 100` ⇒ 以前恒为 0
      （`performance_amount.deduct_amount` 的 docstring 自己写着「漏掉扣点项 ⇒ 两种扣点模式
      被静默废掉」）、现在按设计生效；
    - **固定额档**（`deduct_mode="1"`）：判据是 `value > base` ⇒ 以前 `value > 0` **必抛**
      `AmountError`（结账整批 `1006` / 预览按 `0.00` 计入并进 `errors`），现在
      `value <= temp_amount` 就**静默可扣**——那是新出现的成功路径，⛔ 不是「修好」。
    """

    def _sales_with_deduction(self, order, *, mode, value):
        """客服行：`temp_amount` 照录单写 `0.00`（Ruling P），另配一条扣点。

        ⚠️ `bonus_amount` 取**冻结点**那个数（`44.96` = `(130.00 − 17.60) × 40%`）：
        它代表「这一行生效了多少钱」，正是 `payable_net` 的被减数。
        """
        customer = self._customer(order, temp="0.00")
        Performance.objects.filter(pk=customer.pk).update(
            deduct_mode=mode,
            deduct_value=Decimal(value),
            bonus_amount=Decimal("44.96"),
        )
        customer.refresh_from_db()
        return customer

    def test_percentage_deduction_on_a_sales_row_now_uses_the_refreshed_estimate(self):
        """比例档 10%：基数 `0.00` ⇒ 扣点 `0.00`（旧口径）；基数刷成 `44.96` ⇒ 扣点 `4.50`。

        **杀法**：把 `payable_net` 的 `base` 改回 `Decimal("0.00")`（或让
        `refresh_customer_estimate` 不写客服行）⇒ 第二条断言拿到 `44.96` ⇒ 红。
        """
        order = self._order(_no(601))
        self._main(order, user=self.taker, temp="17.60")
        customer = self._sales_with_deduction(order, mode="0", value="10.00")

        # 旧口径（前置对照）：基数 0.00 ⇒ 比例扣点算出来 0.00、应付净额 = 44.96 − 0.00。
        self.assertEqual(payable_net(customer, []), Decimal("44.96"))

        # 订单事件把客服预计刷成 (130.00 − 17.60) × 40% = 44.96（唯一刷新点）。
        self.assertEqual(refresh_customer_estimate(order), Decimal("44.96"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("44.96"))
        # 新口径：10% × 44.96 = 4.496 ⇒ 四舍五入到分 = 4.50；应付净额 = 44.96 − 4.50 = 40.46。
        self.assertEqual(payable_net(customer, []), Decimal("40.46"))

    def test_fixed_deduction_on_a_sales_row_stops_raising_once_the_base_is_positive(self):
        """固定额档 5.00：基数 `0.00` ⇒ **抛** `AmountError`；基数 `44.96` ⇒ 静默可扣。

        ⚠️ 这条登记的是**新引入的行为变化**（不是「旧口径坏了」）：改前只要客服行配了
        `value > 0` 的固定额扣点，`payable_net` 就必抛 ⇒ 那一行**结不了账**
        （整批 `1006`）；改后 `5.00 <= 44.96` 直接扣。判据 = 两个断言一抛一不抛。
        """
        order = self._order(_no(602))
        self._main(order, user=self.taker, temp="17.60")
        customer = self._sales_with_deduction(order, mode="1", value="5.00")

        with self.assertRaises(AmountError):
            payable_net(customer, [])

        self.assertEqual(refresh_customer_estimate(order), Decimal("44.96"))
        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("44.96"))
        # 5.00 ≤ 44.96 ⇒ 扣 5.00；应付净额 = 44.96 − 5.00 = 39.96（⛔ 不再抛）。
        self.assertEqual(payable_net(customer, []), Decimal("39.96"))


# ── ⑧ ⭐ 第二批：完成单上「预计」与「生效」**一起**重算 ──────────
class EffectiveCommissionOnCompletedRowTests(_EstimateBase):
    """业务方口径：「完成订单事后改价 预计和生效需要一起重新计算改掉」。

    落地形态（`refresh_customer_estimate` 第 4b 条，`D-0182`）：订单**当前**是 `COMPLETED`
    ⇒ `bonus_amount` 用「生效」那份数写；非完成态 ⇒ `bonus` 一个字节不动。**两列各按自己的
    减项**：「预计」减 MAIN 的 `temp_amount`、「生效」减 MAIN 的 `bonus_amount`
    （与 `recompute_order_amounts` 对 MAIN 行「两列一起动」同形；⛔ 不是「用同一个数一起写」）。
    ⚠️ 本类的夹具 MAIN 两列**同值**（`temp == bonus == TAKER_TEMP`）⇒ 下面各条读到的两列
    仍然同值 ⇒ 期望值**一个字没改**；只有「MAIN `temp != bonus`」那种形态才会分叉（那一档的
    判据在 `tests/test_p4fu_order_complete.py::CompletionDeterminesMoneyTests
    ::test_missing_commission_profile_falls_back_to_full_profit`）。

    ⚠️ 期望值一律写**字面十进制数**（E-0024），夹具编号接在本文件既有的 `_no()` 区间之后。
    """

    def test_hall_price_change_recomputes_both_temp_and_bonus(self):
        """改发单价 `88.00 → 200.00`（完成单，未结账）⇒ 打手 `40.00` ⇒ 客服两列都变 `36.00`。

        ⚠️ 判别力靠**新值 != 旧值**：用例把 `bonus_amount` 先摆成 `44.96`（旧口径的冻结点），
        改价后它**必须**变成 `36.00`——只写 `temp` 的实现会留 `44.96` ⇒ 红。
        **杀法**：删掉第 4b 条（`write_bonus` 那半）⇒ `bonus` 停在 `44.96` ⇒ 红。
        """
        order = self._order(_no(801), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        customer.bonus_amount = Decimal("44.96")
        customer.save(update_fields=["bonus_amount", "updated_at"])

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        # 88.00 → 200.00 ⇒ 打手 200 × 20% = 40.00 ⇒ 客服 (130.00 − 40.00) × 40% = 36.00
        self.assertEqual(customer.temp_amount, Decimal("36.00"))
        self.assertEqual(customer.bonus_amount, Decimal("36.00"))

    def test_a_partial_refund_recomputes_both_temp_and_bonus(self):
        """部分退款 `30.00`（完成单，未结账）⇒ 打手 `40.00` ⇒ 客服两列都变 `36.00`。

        代数：净实收 `130.00 − 30.00 = 100.00`；打手不变（`17.60` 的**生效**绩效按 200 的发单价
        重算是 `40.00`）⇒ 客服 `(100.00 − 40.00) × 40% = 24.00`。
        ⚠️ 本用例**刻意**断言 `bonus != 旧值 44.96`，且断言两条路（改价 / 部分退款）落在
        **同一个数**——它们走的是同一份算式。
        **杀法**：删掉 `_write_refund_total` 末尾那次补刷 ⇒ 两列都停在改价后的旧值 ⇒ 红。
        """
        order = self._order(_no(802), status=OrderStatus.COMPLETED)
        main = self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        # 部分退款要先过 `assert_sales_commissions_configured`（提成率为空 ⇒ 1009）
        _set_customer_rate(customer)

        # 前置：先把发单价改成 200 ⇒ 两列刷成 36.00（否则「退款后变成 24.00」没有判别力）。
        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer.refresh_from_db()
        self.assertEqual(customer.bonus_amount, Decimal("36.00"))    # 前置成立

        refund_set_total(order, Decimal("30.00"), "退差价", "taskb-partial-1", "cs-actor")

        main.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("40.00"))        # 打手未动（200 × 20%）
        self.assertEqual(customer.temp_amount, Decimal("24.00"))
        self.assertEqual(customer.bonus_amount, Decimal("24.00"))

    def test_an_order_price_change_recomputes_both_temp_and_bonus(self):
        """`/price/`（改订单**总价**，`order_writes.change_price`）⇒ 两列一起跟上。

        本批当场 grep 复核出来的落点（Task B2-2）：`total_price` 是 `net_received` 的输入
        （`settlement_amount.net_received = total_price − refund_fee`）⇒ 它变了，
        客服两列都该按新净实收走。`batch/price` 调的是**同一个** `change_price` ⇒ 自动覆盖。

        代数：总价 `130.00 → 200.00`、打手生效 `17.60` ⇒ 客服 `(200.00 − 17.60) × 40% = 72.96`。
        """
        order = self._order(_no(803), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        customer.bonus_amount = Decimal("44.96")
        customer.save(update_fields=["bonus_amount", "updated_at"])

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/price/",
            {"total_price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("72.96"))
        self.assertEqual(customer.bonus_amount, Decimal("72.96"))

    def test_an_unfinished_order_price_change_leaves_the_bonus_untouched(self):
        """**未完成**的单改发单价 ⇒ **只有 `temp` 动**、`bonus` 一个字节不动（Task B 第 10 条）。

        与 MAIN 侧 `recompute_order_amounts` 的判据同形（那里也是「订单当前状态 == `COMPLETED`
        才写 `bonus`」）。`bonus` 刻意先摆成**一个不可能由算式算出来的值**（`7.77`）——
        它必须原样留着（若实现无脑写 `bonus`，这一格会变成 `36.00` ⇒ 红）。
        """
        order = self._order(_no(804), status=OrderStatus.IN_PROGRESS)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        customer.bonus_amount = Decimal("7.77")
        customer.save(update_fields=["bonus_amount", "updated_at"])

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, Decimal("36.00"))     # 预计照刷
        self.assertEqual(customer.bonus_amount, Decimal("7.77"))     # 生效一个字节不动


class SettledRowGateTests(_EstimateBase):
    """⭐ 第 0 条（业务方口径）：**已结账**的客服行**一个字节都不动**。

    判据打在**目标 SALES 行自己**的 `is_settled`（三字段**并集**）上，⛔ 不是 MAIN 那道闸
    （`order_writes.change_hall_price` 只看 MAIN，而 SALES 行可以**独立**已结账）。
    三条用例各钉一种「已结账」形态 + 一条**脏行**（状态是未结账、却留着 `settled_at`）。

    ⚠️ `refresh_customer_estimate` 的第 6 条（算不出 ⇒ 不写）在第 0 条面前失效：
    已结账的行**连算都不算**，所以下面「脏行」那条不需要构造可算的提成率。
    """

    #: `bonus_amount` 刻意取一个**算式算不出来**的值：它必须原样留着。
    FROZEN = Decimal("8.88")

    def _settled_customer(self, order, **overrides):
        customer = self._customer(order, temp=CUSTOMER_ESTIMATE)
        fields = {"bonus_amount": self.FROZEN, "settle_status": SettleStatus.SETTLED}
        fields.update(overrides)
        Performance.objects.filter(pk=customer.pk).update(**fields)
        customer.refresh_from_db()
        assert customer.is_settled, "夹具自证：这一行必须是「已结账」"
        return customer

    def test_a_settled_row_is_not_written_by_a_price_change(self):
        """已结账 + 改发单价 `88.00 → 200.00` ⇒ `temp` / `bonus` **一个字节不动**。

        **杀法**：把闸门换成「恒真」（去掉 `if customer.is_settled: return None`）⇒
        两列被刷成 `36.00` ⇒ 红。
        """
        order = self._order(_no(811), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._settled_customer(order)

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)   # 不动
        self.assertEqual(customer.bonus_amount, self.FROZEN)        # 不动

    def test_a_settled_row_is_not_written_by_a_partial_refund(self):
        """已结账 + 部分退款 `30.00` ⇒ 两列不动（退款照样写它自己的 `PARTIAL_REFUND` 冲减链）。"""
        order = self._order(_no(812), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._settled_customer(order)
        _set_customer_rate(customer)

        refund_set_total(order, Decimal("30.00"), "退差价", "settled-gate-1", "cs-actor")

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)   # 不动
        self.assertEqual(customer.bonus_amount, self.FROZEN)        # 不动
        # 阳性对照：退款这条路**真的**走到了这一行（链上多了一条负的冲减）。
        self.assertEqual(
            [
                (row.kind, row.amount)
                for row in PerformanceAdjustment.objects.filter(performance=customer)
            ],
            [("PARTIAL_REFUND", Decimal("-12.00"))],                # 30.00 × 40%
        )

    def test_a_dirty_row_with_a_settled_timestamp_is_treated_as_settled(self):
        """⭐ **脏行**：`settle_status = 未结账` 但 `settled_at` 有值 ⇒ 仍算**已结账**、不被改写。

        这正是「并集」与「单看一列」的分界：单看 `settle_status` 的实现会把这一行当成
        未结账而**改写已经付出去的钱**。
        **杀法**：把闸门改成 `customer.settle_status == SettleStatus.SETTLED` ⇒
        这一行被刷成 `36.00` ⇒ 红。
        """
        order = self._order(_no(813), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._settled_customer(
            order,
            settle_status=SettleStatus.UNSETTLED,
            settled_at=timezone.now(),
        )
        # 夹具自证：**单看状态列**会说它未结账（否则这条用例没有判别力）。
        self.assertEqual(customer.settle_status, SettleStatus.UNSETTLED)

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)   # 不动
        self.assertEqual(customer.bonus_amount, self.FROZEN)        # 不动

    def test_a_dirty_row_with_only_a_settled_amount_is_treated_as_settled(self):
        """另一种脏行：只有 `settled_amount`（`settle_status` 是未结账、`settled_at` 为空）。"""
        order = self._order(_no(814), status=OrderStatus.COMPLETED)
        self._main(order, user=self.taker, temp=TAKER_TEMP, bonus=TAKER_TEMP)
        customer = self._settled_customer(
            order,
            settle_status=SettleStatus.UNSETTLED,
            settled_amount=Decimal("8.88"),
        )

        body = self._client().post(
            f"/api/business/orders/{order.order_no}/hall-price/",
            {"price": "200.00"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        customer.refresh_from_db()
        self.assertEqual(customer.temp_amount, CUSTOMER_ESTIMATE)
        self.assertEqual(customer.bonus_amount, self.FROZEN)
