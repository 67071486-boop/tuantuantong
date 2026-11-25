"""重算客服提成（spec §D15 / §7-T5）。基线见「测试夹具基线」一节。

⚠️ **这不是「单笔端点搬进领域核心」那一类任务**：`recompute-commission` 是一个**新能力**
（对标没有对应端点，spec §D15），所以本文件的判据全部是我方口径：
① 无客服归属人 ⇒ 1002；② 客服行已结账 ⇒ 1014；③ 缺 SALES 行 ⇒ 补建；
④ 已完成 ⇒ 按**与完成时同一套算式**写金额；⑤ 未完成 ⇒ 只补空行、金额不动；
⑥ 算不出提成率（`CommissionError`）⇒ 逐笔 1000、msg 用异常原文。
⚠️ 夹具一律合成值；断言一律打**数据库行**（E-0009：不打 logger / 内存对象）。
"""
from decimal import Decimal

from django.contrib.auth.models import User

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import EmpType, OrderStatus, Performance
from apps.business.performance import create_customer_performance
from tests.test_order_batch_base import BATCH, OrderBatchTestBase

BATCH_RECOMPUTE_URL = f"{BATCH}/recompute-commission/"


class RecomputeCommissionTests(OrderBatchTestBase):
    def _completed_order_with_commission(self, rate="30.00"):
        """一笔「已完成 + 客服提成已确定」的单（形态照既有 P4 客服提成用例）。

        ⚠️ 这里**不建** SALES 行：建单辅助不会自动建它，而「缺行 ⇒ 补建」正是本任务
        要覆盖的一档（spec §D15 第 3 条）。要「行已存在」的现场由调用方先跑一次
        `dry_run=False` 或显式 `create_customer_performance(order)`。
        """
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"),
                            sales_user=self.sales)
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN,
            user=User.objects.create_user(username=f"测试打手-{order.order_no[-4:]}",
                                          password="测试口令-7"),
        )
        return order

    def _recompute(self, order_nos, dry_run):
        return self._post(BATCH_RECOMPUTE_URL, {"order_nos": order_nos, "dry_run": dry_run})

    def _rate_profile(self, mode, value):
        """给客服归属人配一份档案（用来构造「提成率算不出」那一档）。"""
        StaffProfile.objects.create(
            user=self.sales, commission_mode=mode, commission_value=Decimal(value)
        )

    def test_recompute_restores_a_dirtied_amount(self):
        """重算是**覆盖写**：金额被改脏之后，再点一次会写回重算值（幂等回写）。

        ⚠️ **本条的 `expected` 是自指的**（Task 11 Step 0a-1 改准）：它由**同一个端点**的
        前一次调用得出 ⇒ 它钉的是「覆盖写 / 幂等」，**不是**「与完成时同一套算式」。
        跨路径等价由下一条 `test_batch_recompute_matches_the_value_written_by_single_transition`
        钉（那一条的 `expected` 来自**另一条路径**）。原用例名
        `test_completed_order_recomputes_amount_with_the_same_formula` 夸大了本条的证明力
        （AGENTS §5-G「断言要钉值」同族 / E-0024）。
        """
        order = self._completed_order_with_commission()
        # 先让端点跑一遍，拿到基准值（**同一个端点**⇒ 自指）
        self._recompute([order.order_no], dry_run=False)
        expected = self._customer_perf(order).bonus_amount
        self.assertIsNotNone(expected)
        # 再把金额打脏，重算应当把它写回基准值
        perf = self._customer_perf(order)
        perf.bonus_amount = Decimal("0.01")
        perf.save(update_fields=["bonus_amount"])
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["data"]["applied"], [order.order_no])
        self.assertEqual(self._customer_perf(order).bonus_amount, expected)

    def test_batch_recompute_matches_the_value_written_by_single_transition(self):
        """**跨路径**等价（Task 11 Step 0a-1）：批量重算写下的值 == 单笔 `/transition/` 到
        `COMPLETED` 时 `on_order_completed` 钩子写下的值。

        ⚠️ 与上一条的区别正是**基准值的来源**：上一条的 `expected` 由同一个批量端点得出
        （自指），本条的 `expected` 由**另一条实现入口**（单笔状态机 + 完成钩子）写下
        ⇒ 「两套算式漂移」这类改法只有本条能红（变异实测见 Task 11 报告）。

        ## 夹具为什么这样摆（否则比较无意义）

        - **两笔单的形状逐字相同**（同游戏 / 同价 `100.00` / 同客服 / 同 MAIN 行 / 同 SALES 行），
          只有**写金额的那条路径**不同 ⇒ 两个值可直接比。
        - MAIN 行的 `bonus_amount` **刻意给非零**（`40.00`）且 `temp_amount` 同值：
          完成钩子里的 `_activate_taker_performance` 因此**不动它**（`bonus_amount != 0.00`
          即早退），于是两条路径读到的减项**是同一个数**（`40.00`）。
          ⛔ 若给 0.00，完成钩子会把它补成 `temp_amount`，而批量端点**按 §D15 不碰打手那一侧**
          ⇒ 两边的利润基数会合法地不同，这条比较就变成假红。
        - 批量那一笔**直接建成 `COMPLETED`**：`recompute_customer_commission` 只在「已完成」
          才写金额（§D15 第 4/5 条），而它**不能**经 `/transition/` 到完成态——那会走回被比较的
          那条路径（钩子也写一遍），比较就退化成幂等回写。
        - 绝对值的钉子写在下面（`60.00`）：防「两边都写 0.00 ⇒ 相等 ⇒ 绿」的空比较。
          `100.00 − 40.00 = 60.00`；无 `StaffProfile` 的客服按既有口径回落 **100%** 提成率。
        """
        single = self._order(status=OrderStatus.PENDING_REVIEW,
                             total_price=Decimal("100.00"), sales_user=self.sales)
        batch = self._order(status=OrderStatus.COMPLETED,
                            total_price=Decimal("100.00"), sales_user=self.sales)
        for index, order in enumerate((single, batch)):
            taker = User.objects.create_user(
                username=f"测试打手-跨路径-{index}", password="测试口令-8"
            )
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=taker,
                temp_amount=Decimal("40.00"), bonus_amount=Decimal("40.00"),
            )
            create_customer_performance(order)

        # ── 路径 A：单笔 `/transition/` 进 COMPLETED（`on_order_completed` 写下基准值）──
        response = self.client.post(
            f"/api/business/orders/{single.order_no}/transition/",
            {"target": OrderStatus.COMPLETED}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0, response.content[:200])
        expected = self._customer_perf(single).bonus_amount
        self.assertEqual(expected, Decimal("60.00"))     # 绝对值钉子（防空比较）

        # ── 路径 B：批量重算在**另一笔形状相同**的单上写下金额 ──
        body = self._recompute([batch.order_no], dry_run=False)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], [batch.order_no])
        self.assertEqual(self._customer_perf(batch).bonus_amount, expected)

    def test_missing_customer_row_is_created(self):
        # ⚠️ 这个现场要自己造：建单辅助**不会**自动建 SALES 行。
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"),
                            sales_user=self.sales)
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        self.assertFalse(Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists())
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["data"]["applied"], [order.order_no])
        self.assertTrue(Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists())

    def test_non_completed_order_only_gets_an_empty_row(self):
        """§D15 第 5 条：不是「已完成」⇒ **只保证行存在、金额不动**。

        ⚠️ 判据打在**数据库行**上（`refresh_from_db`）：补建出来的行必须是
        `create_customer_performance` 的初始形态（`0.00` / 无提成率），
        ⛔ 不是「照完成单算一遍再被回滚」那种看起来一样的形状。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS, total_price=Decimal("100.00"),
                            sales_user=self.sales)
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], [order.order_no])
        perf = self._customer_perf(order)
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.sales.pk)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(perf.commission_rate)
        # 补建 + 留痕各一次 ⇒ 恰好一行日志（逐笔一行，spec §D7）。
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_no_sales_user_is_ineligible(self):
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        body = self._recompute([order.order_no], dry_run=True)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(body["data"]["ineligible"][0]["code"], 1002)
        self.assertEqual(
            body["data"]["ineligible"][0]["msg"],
            "该单没有客服归属人，无法生成客服绩效账单",
        )

    def test_settled_customer_row_is_ineligible(self):
        order = self._completed_order_with_commission()
        self._recompute([order.order_no], dry_run=False)
        self._settle(self._customer_perf(order))
        body = self._recompute([order.order_no], dry_run=True)
        self.assertEqual(body["data"]["ineligible"][0]["code"], 1014)
        self.assertEqual(
            body["data"]["ineligible"][0]["msg"],
            "该单的客服提成已结账，不能重算；请先撤销结账",
        )

    def test_settled_customer_row_blocks_apply_too(self):
        """1014 在 `apply` 上同样是逐笔不合格（不是只有 `dry_run` 判）。"""
        order = self._completed_order_with_commission()
        self._recompute([order.order_no], dry_run=False)
        perf = self._customer_perf(order)
        self._settle(perf)
        perf.refresh_from_db()
        before = (perf.bonus_amount, perf.commission_rate)
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["data"]["conflict"]["code"], 1014)
        self.assertEqual(body["data"]["applied"], [])
        perf.refresh_from_db()
        self.assertEqual((perf.bonus_amount, perf.commission_rate), before)

    def test_uncomputable_rate_is_ineligible_with_the_exception_message(self):
        """§D15 第 6 条：`CommissionError` ⇒ 逐笔 **1000**、msg 用异常原文。"""
        order = self._completed_order_with_commission()
        # 档案模式 = 订单价百分比(20)，而客服路径要的是利润百分比(30) ⇒ `CommissionError`。
        self._rate_profile(CommissionMode.ORDER_PERCENT, "10.00")
        body = self._recompute([order.order_no], dry_run=True)
        self.assertEqual(body["data"]["eligible"], [])
        issue = body["data"]["ineligible"][0]
        self.assertEqual(issue["code"], 1000)
        self.assertTrue(issue["msg"].strip(), issue)
        self.assertIn("利润", issue["msg"])

    def test_uncomputable_rate_rolls_the_whole_batch_back(self):
        """`apply` 撞上算不出的提成率 ⇒ 整批回滚（§D4 / E-0022），补建的行也不许留下。"""
        order = self._completed_order_with_commission()
        self._rate_profile(CommissionMode.FIXED, "5.00")
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["data"]["conflict"]["code"], 1000)
        self.assertEqual(body["data"]["applied"], [])
        # 补建那一行被同一个事务回滚掉了（E-0022：`set_rollback(True)` **再** return）。
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_main_row_is_never_touched(self):
        """⛔ 不碰打手那一侧：一个「已完成但 MAIN 行还没生效」的现场，重算不许把它激活。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"),
                            sales_user=self.sales)
        taker = User.objects.create_user(username="测试打手-不动", password="测试口令-7")
        main = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker,
            temp_amount=Decimal("50.00"),
        )
        body = self._recompute([order.order_no], dry_run=False)
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.bonus_amount, Decimal("0.00"))
        self.assertIsNone(main.review_status)
        # 客服提成的减项取的是 MAIN 行**当刻**的 `bonus_amount`（0.00）⇒ 提成 = 利润 100 × 100%。
        self.assertEqual(self._customer_perf(order).bonus_amount, Decimal("100.00"))

    def test_one_log_row_per_order_with_the_new_action_word(self):
        """逐笔一行、动作词是 Q2 新增的那个（spec §D7 / §D15 末段）。"""
        a = self._completed_order_with_commission()
        b = self._completed_order_with_commission()
        body = self._recompute([a.order_no, b.order_no], dry_run=False)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        rows = OperationLog.objects.filter(action="performance.recompute_customer_commission")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            sorted(rows.values_list("order_no", flat=True)), [a.order_no, b.order_no]
        )
        # 归**既有**分类 20（PERFORMANCE），⛔ 不新增 LogType。
        self.assertEqual({row.log_type for row in rows}, {20})

    def test_log_field_name_only_when_the_column_was_really_written(self):
        """`field_name` 只在**真的写了 `bonus_amount` 那一列**时才带（Task 11 Step 0a-2）。

        `bonus_amount` 在这条路径上有两个写入点：③ 补建（`create_customer_performance`
        写初始 `0.00`）与 ④ 完成态的覆盖写。而「**未完成** + SALES 行已存在」那一档是
        **纯 no-op**（既不建行也不写金额，§D15 第 5 条）⇒ 给它带 `field_name` 等于让结构化列
        说一句没发生的事。

        ⚠️ 判据打在**数据库行**上（E-0009），且三档**一起**判——只判 no-op 那一档的话，
        「干脆一律不带」这种改法也会绿（那一档本来就不该带）。
        ⛔ 日志行**本身照写**（spec §D15 明写不加「同值不写日志」的特判）⇒ 三档都必须有行。
        """
        def log_field_name(order_no):
            return OperationLog.objects.get(
                action="performance.recompute_customer_commission", order_no=order_no
            ).field_name

        # 档 1：未完成 + SALES 行**已存在** ⇒ no-op ⇒ 不带 field_name。
        noop = self._order(status=OrderStatus.IN_PROGRESS,
                           total_price=Decimal("100.00"), sales_user=self.sales)
        Performance.objects.create(order=noop, emp_type=EmpType.MAIN)
        create_customer_performance(noop)
        self.assertEqual(self._recompute([noop.order_no], dry_run=False)["code"], 0)
        self.assertEqual(log_field_name(noop.order_no), "")

        # 档 2：未完成 + SALES 行**缺失** ⇒ 补建（那一步写了这一列）⇒ 带 field_name。
        created = self._order(status=OrderStatus.IN_PROGRESS,
                              total_price=Decimal("100.00"), sales_user=self.sales)
        Performance.objects.create(order=created, emp_type=EmpType.MAIN)
        self.assertFalse(
            Performance.objects.filter(order=created, emp_type=EmpType.SALES).exists()
        )
        self.assertEqual(self._recompute([created.order_no], dry_run=False)["code"], 0)
        self.assertEqual(log_field_name(created.order_no), "bonus_amount")

        # 档 3：已完成 ⇒ 覆盖写金额 ⇒ 带 field_name。
        done = self._completed_order_with_commission()
        self.assertEqual(self._recompute([done.order_no], dry_run=False)["code"], 0)
        self.assertEqual(log_field_name(done.order_no), "bonus_amount")

    def test_dry_run_writes_nothing(self):
        order = self._completed_order_with_commission()
        before = (Performance.objects.count(), OperationLog.objects.count())
        # ⚠️ 这一档连 SALES 行都不存在 ⇒ 零副作用的判据同时覆盖「不建行」那条（§4.3 第 3 条）。
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )
        body = self._recompute([order.order_no], dry_run=True)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["eligible"], [order.order_no])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(
            (Performance.objects.count(), OperationLog.objects.count()), before
        )


class OnOrderCompletedRegressionTests(OrderBatchTestBase):
    def test_completion_still_activates_taker_and_customer(self):
        """抽函数之后 `on_order_completed` 的行为必须**逐字不变**。

        ⚠️ 「完成订单」这条链的既有用例在 `tests/test_p4fu_order_complete.py` 与
        `tests/test_customer_commission.py` 里 —— **那两套测试全绿**才是本条的判据；
        这里只补一条最直白的锚：完单后 MAIN 行与 SALES 行都被写。

        ⚠️ **SALES 行必须自己造**（建单辅助不会建它）：不造的话 `on_order_completed`
        走到「无客服行 ⇒ `return None`」那一支，`_customer_perf(order)` 会直接
        `DoesNotExist`——那**证明不了**「客服那一半仍然被调到了」。走
        `create_customer_performance(order)` 与录单那条真实路径同源（spec §4.2）。
        """
        order = self._order(status=OrderStatus.PENDING_REVIEW, total_price=Decimal("100.00"),
                            sales_user=self.sales)
        taker = User.objects.create_user(username="测试打手-回归", password="测试口令-7")
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=taker,
            temp_amount=Decimal("40.00"),
        )
        create_customer_performance(order)
        # 走**真实端点**触发完成（基线客户端的节点集里已含 order.change_status_completed）
        body = self._post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
        )
        self.assertEqual(body["code"], 0, body)
        # 打手那一半：从没交过单 ⇒ 完成即生效（`_activate_taker_performance`）。
        self.assertEqual(self._main_perf(order).bonus_amount, Decimal("40.00"))
        # 客服那一半：利润 = 100 − 40 = 60，无档案 ⇒ 100% ⇒ 60.00。
        self.assertEqual(self._customer_perf(order).bonus_amount, Decimal("60.00"))
