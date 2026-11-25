"""领域写入层（`order_writes.py`）的判据：`dry_run=True` 必须零副作用。

⛔ 断言打在**数据库行**上（E-0009）：不看 logger、不看内存里的字段。
夹具值一律合成值（E-0077）。基线见本计划「测试夹具基线」一节。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from apps.audit.models import OperationLog
from apps.business.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    EmpType,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderRefund,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
)
from apps.business.order_status import (
    _TARGET_HOOKS,
    clear_target_hooks,
    register_target_hook,
)
from apps.business.order_writes import (
    WriteContext,
    assign_order,
    change_hall_price,
    change_payment_status,
    change_price,
    delete_order_write,
    refund_total_write,
    transition_order,
    update_order_fields,
)
from apps.business.performance import apply_amount_to_effective_base
from apps.business.performance_amount import AmountError
from apps.business.refunds import (
    RefundError,
    assert_sales_commissions_configured,
    refund_total_denial,
    refund_set_total,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games
from tests.test_order_batch_base import CTX, CTX_FULL, OrderBatchTestBase


class ApprovedPenaltyAmountTests(OrderBatchTestBase):
    def setUp(self):
        super().setUp()
        self.approved_target = User.objects.create_user(username="approved-penalty-original-target")
        self.selected_taker = User.objects.create_user(username="approved-penalty-selected-taker")
        self.order = self._order(hall_price=Decimal("-30.00"))
        grant_games(self.selected_taker, self.order.game)
        self.order.penalty_approval = Approval.objects.create(
            kind=ApprovalKind.PENALTY,
            status=ApprovalStatus.APPROVED,
            target=self.approved_target,
            amount=Decimal("30.00"),
        )
        self.order.save(update_fields=["penalty_approval"])

    def test_assignment_can_change_taker_but_cannot_change_approved_amount(self):
        for dry_run in (True, False):
            result = assign_order(
                self.order, taker=self.selected_taker, price=Decimal("-25.00"),
                ctx=CTX_FULL, dry_run=dry_run,
            )
            self.assertFalse(result.ok)
        rejected_fee = assign_order(
            self.order, taker=self.selected_taker, fee=Decimal("-25.00"), ctx=CTX_FULL, dry_run=False,
        )
        self.assertFalse(rejected_fee.ok)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, self.order.hall_price), (OrderStatus.UNASSIGNED, Decimal("-30.00")))
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())

        assigned = assign_order(
            self.order, taker=self.selected_taker, price=Decimal("-30.00"), ctx=CTX_FULL, dry_run=False,
        )
        self.assertTrue(assigned.ok, assigned.msg)
        self.assertEqual(self._main_perf(self.order).user_id, self.selected_taker.pk)

    def test_performance_recalculation_cannot_change_approved_amount(self):
        assigned = assign_order(self.order, taker=self.selected_taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(assigned.ok, assigned.msg)
        perf = self._main_perf(self.order)
        with self.assertRaisesMessage(AmountError, "罚款金额已由审批确定"):
            apply_amount_to_effective_base(perf, Decimal("-25.00"))
        self.order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("-30.00"))
        self.assertEqual(perf.temp_amount, Decimal("-30.00"))

    def test_normal_price_change_cannot_override_approved_amount(self):
        for dry_run in (True, False):
            result = change_hall_price(
                self.order, price=Decimal("0"), ctx=CTX_FULL, dry_run=dry_run,
            )
            self.assertFalse(result.ok)
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("-30.00"))
        self.assertFalse(OperationLog.objects.filter(order_no=self.order.order_no).exists())


class ChangePriceCoreTests(OrderBatchTestBase):
    def test_dry_run_writes_nothing(self):
        order = self._order(total_price=Decimal("100.00"))
        before = (order.total_price, order.updated_by, OperationLog.objects.count())
        outcome = change_price(
            order, total_price=Decimal("88.00"), reason="", ctx=CTX, dry_run=True
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.total_price, before[0])
        self.assertEqual(order.updated_by, before[1])
        self.assertEqual(OperationLog.objects.count(), before[2])

    def test_real_run_writes_and_logs(self):
        order = self._order(total_price=Decimal("100.00"))
        outcome = change_price(
            order, total_price=Decimal("88.00"), reason="", ctx=CTX, dry_run=False
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("88.00"))
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "order.change_taker_price")
        self.assertEqual(log.to_value, "88.00")


class HallPriceCoreTests(OrderBatchTestBase):
    """`change_hall_price` 的两条领域判据（spec §D5 / §4.3）。

    ⚠️ 夹具基线的 `_order()` 用 `Order.objects.create`，**不建绩效行**，而 `_main_perf()`
    用 `.get()` ⇒ 那两条用例先各自补一行 MAIN（`Performance.objects.create(order=…,
    emp_type=EmpType.MAIN, …)`，与 `test_jidanpu_hall_launch.py` 同写法）。
    缺行时 `_main_perf()` 会响亮抛 `DoesNotExist`——那正是「补行这一步没做」的信号。

    ⚠️ 第二条的 MAIN 行**必须有 `user`**：`recompute_order_amounts` 只在
    「有 MAIN 行且 `user` 非空」时才跑 ⇒ 没有接单人的话，即使 `dry_run` 早返回放错位置
    （放到重算之后）`temp_amount` 也不会变，那条断言就成了**恒真**的（E-0024 的形状）。
    无 `StaffProfile` 时提成率按既有口径回落 100%（`commission_percent_of`），
    ⇒ 真跑会把 `temp_amount` 写成新发单价，判别力成立。
    """

    def _seed_main_row(self, order, *, user=None, temp_amount=Decimal("0.00")):
        """补一行 MAIN 绩效（合成值）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=user, temp_amount=temp_amount
        )

    def test_dry_run_reports_settled_order_as_ineligible_without_writing(self):
        order = self._order(total_price=Decimal("100.00"))
        self._seed_main_row(order)
        self._settle(self._main_perf(order))
        outcome = change_hall_price(
            order, price=Decimal("50.00"), reason="", ctx=CTX, dry_run=True
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.PERFORMANCE_STATE_INVALID))  # 1014
        order.refresh_from_db()
        self.assertIsNone(order.hall_price)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_does_not_touch_performance_amounts(self):
        order = self._order(total_price=Decimal("100.00"))
        taker = User.objects.create_user(username="obatch-hall-taker")
        self._seed_main_row(order, user=taker, temp_amount=Decimal("100.00"))
        before = self._main_perf(order).temp_amount
        outcome = change_hall_price(
            order, price=Decimal("50.00"), reason="", ctx=CTX, dry_run=True
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(self._main_perf(order).temp_amount, before)
        # ⭐ 上面那条只判 `temp_amount`，「dry_run 早返回
        #    真的挡住了写库」是**反推**出来的。补两条**直接**断言——目标列本身 + 日志行数。
        order.refresh_from_db()
        self.assertIsNone(order.hall_price)
        self.assertEqual(OperationLog.objects.count(), 0)


class PaymentStatusCoreDryRunTests(OrderBatchTestBase):
    """`change_payment_status` 的核心级 `dry_run`（Task 1 评审留的账，本任务关闭）。

    ⚠️ `dry_run` 是**新增**形参（`order_writes.py` 的搬迁加的）⇒ 这条腿在 Task 1 里
    只有端点级覆盖（`/payment-status/` 的 `dry_run=False`），核心级的零副作用没人判过。
    """

    def test_dry_run_writes_nothing(self):
        order = self._order(payment_status="UNSETTLED")
        outcome = change_payment_status(
            order, payment_status="SETTLED", ctx=CTX, dry_run=True
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "UNSETTLED")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_real_run_writes_and_logs(self):
        order = self._order(payment_status="UNSETTLED")
        outcome = change_payment_status(
            order, payment_status="SETTLED", ctx=CTX, dry_run=False
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "SETTLED")
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "order.change_payment_status")


class OrderFieldsCoreDryRunTests(OrderBatchTestBase):
    """`update_order_fields` 的核心级 `dry_run`（Task 2 评审留的账，本任务关闭）。

    ⚠️ `request_user` 必须传**真实登录账号**（`obatch-user`）。本函数会跑
    `assert_game_allowed(request_user, data.get("game", order.game))`，而 `validated` 只含
    `order_type` ⇒ `data.get("game", …)` 取的是**当前游戏**；传 `None` 会被
    `games_of(None)` 判成「没有该游戏的权限」（业务码 1000）——brief 里那句
    「若它需要真实 user，就用基线的登录账号」说的正是这一支。
    """

    def test_dry_run_writes_nothing(self):
        order = self._order(order_type="COMPANION")
        outcome = update_order_fields(
            order,
            validated={"order_type": "BOOST"},
            request_user=User.objects.get(username="obatch-user"),
            ctx=CTX,
            dry_run=True,
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.order_type, "COMPANION")
        self.assertEqual(OperationLog.objects.count(), 0)


class OrderFieldsConcurrentPatchTests(OrderBatchTestBase):
    """E-0251 回归钉：PATCH 编辑订单必须**窄写**（`update_fields`）。

    现场（闲时审查 H3，双连接探针实锤）：`order.save()` 不带 `update_fields`
    会把陈旧实例上**全部**已加载列写回库 —— 装载之后另一连接刚提交的
    状态 / 发单归属 / 发单时间被整批覆盖回旧快照。本钉照那个探针的形状：
    两个模型实例 + `Order.objects.filter(pk=…).update()` 模拟「另一连接的并发提交」。

    三段判据（⛔ 缺一不算落地）：
    ① 并发提交的 `status` / `hall_publisher` / `published_at` 在本次保存后**原样存活**
      （修前被覆盖回装载时的旧值 ⇒ 红）；
    ② 本次只改的 `remark` 真的落库（窄写不许把「改窄」变成「没写」）；
    ③ `updated_at` 前进、`updated_by` 落库、留痕照写 —— Django 5.2 语义（源码核实：
      `db/models/base.py::_save_table` 只对 `update_fields` 列出的字段调 `pre_save`，
      `auto_now` 不在清单里就**不前进**）⇒ 窄写清单必须**显式含 `updated_at`**。
    """

    def test_restoring_source_number_from_stale_order_still_queues_sync(self):
        user = User.objects.get(username="obatch-user")
        binding = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://jdp.test.invalid",
            account="jdp-source-owner", password="synthetic", enabled=True,
        )
        order = self._order(source_order_no="SYNTH-SOURCE-BEFORE")
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="SYNTH-JDP-ORDER",
        )
        Order.objects.filter(pk=order.pk).update(source_order_no="SYNTH-SOURCE-OTHER")

        outcome = update_order_fields(
            order, validated={"source_order_no": "SYNTH-SOURCE-BEFORE"},
            request_user=user, ctx=CTX, dry_run=False,
        )

        self.assertTrue(outcome.ok, outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.source_order_no, "SYNTH-SOURCE-BEFORE")
        self.assertTrue(JidanpuSyncEvent.objects.filter(
            target_ref=order.order_no, kind="member.source_order_no",
            payload__member_binding_id=binding.pk, status="pending",
        ).exists())

    def test_stale_instance_patch_keeps_concurrently_committed_columns(self):
        publisher = User.objects.create_user(username="obatch-publisher", password="测试口令-发")
        committed_at = timezone.now()
        order = self._order(
            status=OrderStatus.UNASSIGNED,
            hall_publisher=publisher,
            published_at=committed_at,
        )
        # 陈旧实例：另一连接早先装载的快照（此刻与库一致，随后库被并发改走）。
        stale = Order.objects.get(pk=order.pk)
        # 「另一连接」的并发提交：打手接单（状态进「代练中」）+ 清发单归属 + 发单时间重写。
        concurrently_committed_at = timezone.now()
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.IN_PROGRESS,
            hall_publisher=None,
            published_at=concurrently_committed_at,
        )

        outcome = update_order_fields(
            stale,
            validated={"remark": "并发下的备注"},
            request_user=User.objects.get(username="obatch-user"),
            ctx=CTX,
            dry_run=False,
        )
        self.assertTrue(outcome.ok, outcome.msg)

        # ① 并发提交的三个列原样存活（判据打在**数据库行**上，E-0009）。
        row = Order.objects.get(pk=order.pk)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(row.hall_publisher_id)
        self.assertEqual(row.published_at, concurrently_committed_at)
        # ② 本次请求改的那一列真的落库。
        self.assertEqual(row.remark, "并发下的备注")
        # ③ 窄写仍显式带审计两列：`updated_at` 前进（严格晚于并发提交那一刻）、
        #    `updated_by` 是本次的业务操作人；留痕一行不少。
        self.assertGreater(row.updated_at, concurrently_committed_at)
        self.assertEqual(row.updated_by, CTX.actor)
        log = OperationLog.objects.filter(
            order_no=order.order_no, action="order.update"
        ).latest("id")
        self.assertEqual(log.field_name, "remark")


class TransitionCoreTests(OrderBatchTestBase):
    """`transition_order` 的领域判据（spec §D5 / §4.3 第 4 条）。

    ⚠️ 用 `CTX_FULL`：基线里的 `CTX` 的 `nodes` 是**空集**，状态机会把每一条边都判成
    「节点不够」——那测不出「`dry_run` 不跑状态机」这件事（两条 dry_run 用例都会因为
    别的原因红）。
    """

    def test_dry_run_does_not_transition_or_run_hooks(self):
        order = self._order(status=OrderStatus.UNASSIGNED)
        outcome = transition_order(
            order, target=OrderStatus.IN_PROGRESS, ctx=CTX_FULL, dry_run=True
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_reports_illegal_edge(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        outcome = transition_order(
            order, target=OrderStatus.COMPLETED, ctx=CTX_FULL, dry_run=True
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.BUSINESS))       # 1002
        self.assertEqual(outcome.msg, "必须先把订单状态改成待验收才能改成已完成")

    def test_real_run_writes_status_and_upholds_the_chinese_labels(self):
        """真写路径：状态改了、留痕与 `order.updated_by` 都取**认证态**。

        ⚠️ 这是搬迁期抓出来的一个真回归的回归网（`ctx.actor` ≠ `ctx.log_actor`）：
        `/transition/` 的 `actor` 取**认证态用户名**（不读请求体的 `actor`），而它同时进
        `order.updated_by`；退款钩子（`on_order_refunded`）从这个列取操作人写
        `order.refund` 留痕。核心若错用 `ctx.actor`（在 `CTX_FULL` 里是「测试业务操作人」、
        且它为空时 `apply_transition` 的 `if actor:` 会让 `updated_by` **原地不动**）
        ⇒ 下面那条 `updated_by == log_actor` 会红（判别力就落在这一条上）。
        """
        order = self._order(status=OrderStatus.UNASSIGNED)
        outcome = transition_order(
            order, target=OrderStatus.WAITING_GRAB, ctx=CTX_FULL, dry_run=False
        )
        self.assertTrue(outcome.ok)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        # `CTX_FULL` 的 `actor` / `log_actor` **刻意不同源**：这里必须落在 `log_actor` 上。
        self.assertEqual(order.updated_by, CTX_FULL.log_actor)
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "order.transition")
        self.assertEqual(log.actor, CTX_FULL.log_actor)
        # 前后值是**中文标签**（不是状态码）。
        self.assertEqual(log.from_value, "未分配")
        self.assertEqual(log.to_value, "待抢单")

    # ── spec §4.2 的两道闸都要在 `dry_run` 上跑完 ──────────

    def _seed_settled_main_row(self, order):
        """补一行**已结账**的 MAIN 绩效（合成值；`is_settled` 是三字段并集，写一列即可）。"""
        perf = Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        self._settle(perf)
        return perf

    def test_settled_pool_entry_is_denied_on_both_branches(self):
        """已结账 + 回池目标 ⇒ `dry_run=True` 与 `dry_run=False` 必须给**同一个** 1002。

        spec §4.2 把「已结账回池(1002)」列为**逐笔合格性**，§D5 要求 `dry_run` 跑完
        **全部**判据 ⇒ 两道闸同源（`order_status` 里同一份判据、两处调用），
        ⛔ 不是各写一套。修前 `dry_run` 只跑第一道闸（`transition_denial`）⇒ 本用例红：
        同一个函数的两条分支对同一笔单给出**相反**答案。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        self._seed_settled_main_row(order)
        expected = "非法状态迁移：COMPLETED → WAITING_GRAB（该单绩效已结账，不能改回池内）"
        messages = []
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                outcome = transition_order(
                    order, target="WAITING_GRAB", ctx=CTX_FULL, dry_run=dry
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.code, int(ErrorCode.BUSINESS))  # 1002
                self.assertEqual(outcome.msg, expected)
                messages.append(outcome.msg)
        # 两条分支**逐字相同**（这正是 Important #1 的分歧所在）。
        self.assertEqual(messages[0], messages[1])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_treats_nodes_none_as_no_identity(self):
        """`ctx.nodes=None` ⇒ 空集（拒绝），与真写路径的 `apply_transition` 同口径。

        `transition_denial(..., nodes=None)` 的宽松语义是「这条边**存在吗**」——
        `dry_run` 预检若照单全收就是 fail-open（放行），而真写路径会拒绝。
        修前 `dry_run=True` 这条会红（返回 `ok`）；修后两条分支一致。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        ctx = WriteContext(
            actor="测试业务操作人", log_actor="测试登录账号", log_actor_id=1, nodes=None
        )
        for dry in (True, False):
            with self.subTest(dry_run=dry):
                outcome = transition_order(
                    order, target="WAITING_GRAB", ctx=ctx, dry_run=dry
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.code, int(ErrorCode.BUSINESS))
                self.assertIn("缺少所需权限节点", outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_never_runs_the_target_hooks(self):
        """`dry_run=True` 时目标态钩子**一次都不跑**（spec §4.3 第 4 条 / §7-T3）。

        ⚠️ 用**探针钩子**而不是既有生产钩子：既有那条边（`UNASSIGNED → IN_PROGRESS`）上
        `notifications._on_in_progress` 对 `from_status != WAITING_GRAB` **直接 return**
        ⇒ 没有可观测副作用，一个「在 `atomic()` 里真跑了 `apply_transition` 再回滚」的
        变异体能存活（DB 回滚了、内存里的 `order.status` 又被 `refresh_from_db` 抹平）。
        探针挂 `PAUSED`：生产钩子只在六个目标态上，`PAUSED` 不在其中（下面的守卫钉住）。
        """
        order = self._order(status=OrderStatus.UNASSIGNED)
        # 防回归：探针目标态上不能有生产钩子（`clear_target_hooks` 是整条 pop）。
        self.assertIsNone(_TARGET_HOOKS.get(str(OrderStatus.PAUSED)))
        calls = []
        register_target_hook(
            OrderStatus.PAUSED,
            lambda target_order, from_status: calls.append(
                (target_order.order_no, from_status)
            ),
        )
        self.addCleanup(clear_target_hooks, OrderStatus.PAUSED)
        outcome = transition_order(
            order, target=OrderStatus.PAUSED, ctx=CTX_FULL, dry_run=True
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(OperationLog.objects.count(), 0)


class AssignCoreTests(OrderBatchTestBase):
    """`assign_order` 的领域判据（spec §D5 / §4.2 的 assign 行 / §4.3 的五条特例）。

    ⚠️ 夹具基线的 `_order()` **不建绩效行**，而本动作有一条逐单判据要读 MAIN 行
    （`is_settled`）⇒ 凡要走到那一步的用例都先自己补一行（`_seed_main_row`）。
    ⛔ 目标账号必须**启用**（`User.objects.create_user` 默认 `is_active=True`；
    「不存在 / 已停用」是**请求级**校验，留在视图，本类不测那两条）。
    """

    def setUp(self):
        super().setUp()
        self.taker = User.objects.create_user(username="测试打手甲", password="测试口令-1")
        self.other = User.objects.create_user(username="测试打手乙", password="测试口令-2")
        # ⭐ D-0252：`assign_order` 入口多了「目标打手游戏范围」闸（无档案=拒）
        #    ⇒ 本类考的是 ⓪/①/②/③ 与五条特例，给两个打手都授**本基线的游戏**让那一闸放行；
        #    「无该游戏 ⇒ 拒」那一支由 tests/test_order_assign_game_gate.py 专钉。
        grant_games(self.taker, self.game)
        grant_games(self.other, self.game)

    def _seed_main_row(self, order, *, user=None, temp_amount=Decimal("20.00")):
        """补一行 MAIN 绩效（合成值）。"""
        return Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=user, temp_amount=temp_amount
        )

    # ── ① `taker == sales_user` ⇒ 1002 ────────────────────────────────────

    def test_dry_run_reports_taker_is_sales_user(self):
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.taker)
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.BUSINESS))  # 1002
        self.assertEqual(
            outcome.msg,
            "该账号是本单的销售提成归属人，不能同时指派为接单人"
            "（同一订单同一人只能有一条绩效）；若要支持兼岗需先放开约束",
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── ② MAIN 行 `is_settled` ⇒ 1014 ─────────────────────────────────────

    def test_dry_run_reports_settled_main_row(self):
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.other)
        self._settle(self._seed_main_row(order))
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.PERFORMANCE_STATE_INVALID))  # 1014
        self.assertEqual(
            outcome.msg, "该单的接单人绩效已结账，不能改派；请先撤销结账再指派"
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main_perf(order).user_id)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_settled_main_row_is_denied_on_the_real_write_too(self):
        """`is_settled` 闸在**真写路径**上同样在 `transaction.atomic()` 之外、任何写入之前。"""
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.other)
        self._settle(self._seed_main_row(order))
        outcome = assign_order(
            order, taker=self.taker, price=Decimal("50.00"), ctx=CTX_FULL, dry_run=False
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.PERFORMANCE_STATE_INVALID))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(order.hall_price)
        perf = self._main_perf(order)
        self.assertIsNone(perf.user_id)
        self.assertEqual(perf.temp_amount, Decimal("20.00"))
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── ③ 状态机边不可达 ⇒ 1002（`dry_run` 也要跑；`IN_PROGRESS` 时跳过）────

    def test_dry_run_reports_the_unreachable_state_machine_edge(self):
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.other)
        # `CTX` 的 `nodes` 是**空集** ⇒ 边存在、节点不够（两种拒绝可分辨，见 `order_status`）。
        outcome = assign_order(order, taker=self.taker, ctx=CTX, dry_run=True)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, int(ErrorCode.BUSINESS))  # 1002
        self.assertEqual(
            outcome.msg, "非法状态迁移：UNASSIGNED → IN_PROGRESS（缺少所需权限节点）"
        )

    # ── ④ `dry_run=True` 零副作用（spec §4.3 五条）────────────────────────

    def test_dry_run_writes_nothing(self):
        """不写状态 / 不写价 / 不动绩效列 / 不留痕。"""
        order = self._order(
            status=OrderStatus.UNASSIGNED, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        self._seed_main_row(order)
        outcome = assign_order(
            order, taker=self.taker, price=Decimal("50.00"), ctx=CTX_FULL, dry_run=True
        )
        self.assertTrue(outcome.ok, outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.hall_price, Decimal("88.00"))
        perf = self._main_perf(order)
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.claimed_at)
        self.assertEqual(perf.temp_amount, Decimal("20.00"))
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertFalse(
            OperationLog.objects.filter(order_no=order.order_no).exists()
        )

    def test_dry_run_does_not_create_the_missing_main_row(self):
        """缺行时 `dry_run` **不补行**（补行只在真写路径的 `atomic()` 内）。"""
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.other)
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=True)
        self.assertTrue(outcome.ok, outcome.msg)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )

    def test_dry_run_does_not_revive_the_soft_deleted_main_row(self):
        order = self._order(status=OrderStatus.UNASSIGNED, sales_user=self.other)
        perf = self._seed_main_row(order)
        perf.deleted_at = timezone.now()
        perf.deleted_by = "测试删除人"
        perf.save(update_fields=["deleted_at", "deleted_by"])
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=True)
        self.assertTrue(outcome.ok, outcome.msg)
        perf.refresh_from_db()
        self.assertIsNotNone(perf.deleted_at)               # 没被复活
        self.assertEqual(perf.deleted_by, "测试删除人")
        self.assertIsNone(perf.user_id)

    # ── ⑤ 真写路径的成功用例 ──────────────────────────────────────────────

    def test_real_run_writes_order_performance_and_the_reassign_log(self):
        """带价 ⇒ `performance.reassign`；`updated_by` 与留痕的 `actor` 都取**认证态**。"""
        order = self._order(
            status=OrderStatus.UNASSIGNED, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        self._seed_main_row(order)
        outcome = assign_order(
            order, taker=self.taker, price=Decimal("50.00"), ctx=CTX_FULL, dry_run=False
        )
        self.assertTrue(outcome.ok, outcome.msg)
        self.assertEqual(outcome.detail["order"].pk, order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.hall_price, Decimal("50.00"))
        # `apply_transition(actor=...)` 必须是**认证态用户名**（`CTX_FULL.actor` 刻意不同源）。
        self.assertEqual(order.updated_by, CTX_FULL.log_actor)
        perf = self._main_perf(order)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.temp_amount, Decimal("50.00"))   # 新价 × 100%（无档案回落）
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "performance.reassign")
        self.assertEqual(log.actor, CTX_FULL.log_actor)
        self.assertEqual(log.target_name, self.taker.username)
        self.assertEqual(log.from_value, "88.00")
        self.assertEqual(log.to_value, "50.00")

    def test_real_run_without_price_logs_assign(self):
        """不带价 ⇒ `performance.assign`（判据只看**发单价是否真的变了**）。"""
        order = self._order(
            status=OrderStatus.UNASSIGNED, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        self._seed_main_row(order)
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.hall_price, Decimal("88.00"))
        self.assertEqual(self._main_perf(order).user_id, self.taker.id)
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "performance.assign")
        self.assertEqual(log.from_value, "")
        self.assertEqual(log.to_value, "88.00")

    # ── ⑤′ D-0369：机器上下文（`user=None`）不写发单人 ─────────────────────

    def test_machine_context_does_not_touch_the_publisher(self):
        """D-0369 的负向钉子：ctx `user=None` ⇒ 发单人**不动**（机器 / 开放平台不算）。

        机器 ctx 照同步线占位接手（`dailiantong_push`）的形状造：`user=None`、
        `log_actor_id=None`（台账落系统行）。夹具先给发单人挂上哨兵，再机器指派；
        断言接手人**真的换了**（阳性对照，排除「整个动作没发生」的假绿）、
        发单人**仍是哨兵**。放本文件的理由：这是 `assign_order` 的**领域判据**
        （对全部 `user=None` 调用点生效：openapi 录单+指派、代练通占位、修复命令），
        不是某一条 HTTP 路径的性质；端点级的正向钉子在
        `tests/test_order_column_fields.py::HallPublisherWritePathTests`。
        ⚠️ 判别力：把 `assign_order` 直写点那句 `if ctx.user is not None:` 判据删掉
        ⇒ `hall_publisher` 被覆盖成 `None` ⇒ 本用例红。
        """
        publisher = User.objects.create_user(username="机器负向-抛单人")
        order = self._order(
            status=OrderStatus.UNASSIGNED, sales_user=self.other,
            hall_price=Decimal("88.00"),
            hall_publisher=publisher,
        )
        self._seed_main_row(order)
        machine_ctx = WriteContext(
            actor="机器占位接手",
            log_actor="机器占位接手",
            log_actor_id=None,   # `record()` 的 fail-closed 判据 ⇒ 台账落系统行
            nodes=frozenset({"order.change_status"}),
            user=None,
        )
        outcome = assign_order(order, taker=self.taker, ctx=machine_ctx, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)          # 阳性对照
        self.assertEqual(self._main_perf(order).user_id, self.taker.id)  # 接手人真的换了
        self.assertEqual(order.hall_publisher_id, publisher.id)          # 发单人仍是哨兵
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertIsNone(log.actor_id)   # 机器行口径没被本次改动碰（照实复核一眼）

    # ── ⑥ 真写路径的五条特例（spec §4.3 的 ⛔ 更正块；本分组覆盖前四条，第 5 条「钱钩子不在本入口跑」的判据在 `tests/test_in_progress_recalc.py`）────

    def test_already_in_progress_skips_the_state_machine(self):
        """R-3：已是 `IN_PROGRESS` ⇒ **不调** `apply_transition`（`TRANSITIONS` 没有自环）。"""
        order = self._order(
            status=OrderStatus.IN_PROGRESS, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        self._seed_main_row(order)
        with mock.patch(
            "apps.business.order_writes.apply_transition",
            side_effect=AssertionError("R-3：已是代练中，不许再走状态机"),
        ):
            outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        self.assertEqual(self._main_perf(order).user_id, self.taker.id)

    def test_same_taker_save_keeps_his_own_override_price(self):
        """R-2b：同一个人再保存 ⇒ **跳过**七列回退（保住覆盖价 / 工时 / 项目）。"""
        order = self._order(status=OrderStatus.IN_PROGRESS, sales_user=self.other)
        perf = self._seed_main_row(order, user=self.taker)
        perf.taker_price = Decimal("80.00")
        perf.hours = 5
        perf.project = "既有接手项目"
        perf.save(update_fields=["taker_price", "hours", "project"])
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        perf.refresh_from_db()
        self.assertEqual(perf.taker_price, Decimal("80.00"))   # 没被回退清掉
        self.assertEqual(perf.hours, 5)
        self.assertEqual(perf.project, "既有接手项目")
        self.assertEqual(perf.user_id, self.taker.id)

    def test_a_different_taker_rolls_the_seven_columns_back(self):
        """R-2b 的反面：**真正换人**时七列回退照跑。"""
        order = self._order(
            status=OrderStatus.IN_PROGRESS, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        perf = self._seed_main_row(order, user=self.other)
        perf.taker_price = Decimal("80.00")
        perf.hours = 5
        perf.project = "上一个接手的项目"
        perf.save(update_fields=["taker_price", "hours", "project"])
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertIsNone(perf.taker_price)
        self.assertIsNone(perf.hours)
        self.assertEqual(perf.project, "")

    def test_soft_deleted_main_row_is_revived_not_recreated(self):
        order = self._order(
            status=OrderStatus.UNASSIGNED, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        perf = self._seed_main_row(order)
        perf.deleted_at = timezone.now()
        perf.deleted_by = "测试删除人"
        perf.save(update_fields=["deleted_at", "deleted_by"])
        pk = perf.pk
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        rows = Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(rows.count(), 1)          # `uniq_order_main_taker`：仍是同一行
        revived = rows.get()
        self.assertEqual(revived.pk, pk)           # 复活，不是新建
        self.assertIsNone(revived.deleted_at)
        self.assertEqual(revived.deleted_by, "")
        self.assertEqual(revived.user_id, self.taker.id)

    def test_missing_main_row_is_created_inside_the_transaction(self):
        order = self._order(
            status=OrderStatus.PENDING_REVIEW, sales_user=self.other,
            hall_price=Decimal("88.00"),
        )
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        outcome = assign_order(order, taker=self.taker, ctx=CTX_FULL, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        perf = self._main_perf(order)
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertIsNotNone(perf.claimed_at)


#: 软删用例的操作人上下文。⚠️ `user` 是**认证态的 `User` 实例**（`soft_delete_order`
#: 要 `actor.get_username()`，⛔ 不是字符串）；它的用户名就是认证态用户名
#: （`request.user.get_username()`），与 `log_actor` 同源——这正是生产里 `_write_context`
#: 在 `validate_actor=False` 下造出来的形状。`actor`（请求体那一路）**刻意不同源**，
#: 好让「留痕取了请求体 `actor`」这种回归能被断言抓住。


def _delete_ctx(user):
    return WriteContext(
        actor="测试业务操作人",
        log_actor=user.get_username(),
        log_actor_id=7,
        user=user,
    )


class DeleteCoreTests(OrderBatchTestBase):
    """`delete_order_write` 的判据（spec §D5 / §4.2 的 delete 行 / §4.3）。

    ⚠️ `order` 由**调用方**用 `Order.all_objects` 查好（含已软删）——本类直接用
    `Order.all_objects.get(...)` 复现那个契约，⛔ 不走默认 manager。

    ⚠️ 断言打在**数据库行**上（E-0009）：`deleted_at` / `deleted_by` 用
    `Order.all_objects` 重读，绩效列用 `Performance.objects`（默认 manager 不看软删，
    所以要用 `all_objects` 才看得到被标记的行）。
    """

    def _reload(self, order):
        """按单号**含已软删**重读（默认 manager 看不到删过的行 ⇒ 必须 `all_objects`）。"""
        return Order.all_objects.get(order_no=order.order_no)

    def test_dry_run_does_not_soft_delete_or_log(self):
        """`dry_run=True` 零写入：订单两列不变、绩效行不被标记、一行日志也不写。"""
        order = self._order(status=OrderStatus.COMPLETED)
        perf = Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        ctx = _delete_ctx(self.sales)
        outcome = delete_order_write(order, is_del_emp="Y", ctx=ctx, dry_run=True)
        self.assertTrue(outcome.ok, outcome.msg)
        self.assertEqual(outcome.detail["order"].pk, order.pk)
        order.refresh_from_db()
        self.assertIsNone(order.deleted_at)
        self.assertEqual(order.deleted_by, "")
        # `isDelEmp="Y"` 时绩效行**没被标记**（这正是本用例要钉的那一条）。
        perf.refresh_from_db()
        self.assertIsNone(perf.deleted_at)
        self.assertEqual(perf.deleted_by, "")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_real_run_soft_deletes_and_marks_performances(self):
        """真写路径：订单两列 + 绩效行 + 一条 `order.delete` 留痕（操作人取 `log_actor`）。"""
        order = self._order(status=OrderStatus.COMPLETED)
        perf = Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        ctx = _delete_ctx(self.sales)
        outcome = delete_order_write(order, is_del_emp="Y", ctx=ctx, dry_run=False)
        self.assertTrue(outcome.ok, outcome.msg)
        row = self._reload(order)
        self.assertIsNotNone(row.deleted_at)
        # ⚠️ 业务列 `deleted_by` 取 **`ctx.user`**（`soft_delete_order` 调
        # `actor.get_username()`——它要的是 `User` 实例，⛔ 不是字符串 `log_actor`）；
        # 留痕的 `actor` 取认证态用户名、`actor_id` 取 `ctx.user.pk`（见下两段）。
        self.assertEqual(row.deleted_by, self.sales.get_username())
        perf.refresh_from_db()
        self.assertIsNotNone(perf.deleted_at)
        self.assertEqual(perf.deleted_by, self.sales.get_username())
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "order.delete")
        self.assertEqual(log.log_type, 10)          # 登记表 `ACTION_LOG_TYPES["order.delete"]`
        # 留痕取**认证态用户名**（= `ctx.user.get_username()` = `ctx.log_actor`），
        # ⛔ 不是请求体的 `ctx.actor`（"测试业务操作人"）。
        self.assertEqual(log.actor, self.sales.get_username())
        self.assertNotEqual(log.actor, ctx.actor)
        # ⚠️ `actor_id` 取 **`ctx.user.pk`**（`soft_delete_order` 里是
        # `getattr(actor, "pk", None)`）——生产里与 `ctx.log_actor_id` 同源，本用例的 ctx
        # 把它们**刻意分开**，好区分「读了 User 实例」与「读了 ctx.log_actor_id」。
        self.assertEqual(log.actor_id, self.sales.pk)
        self.assertNotEqual(log.actor_id, ctx.log_actor_id)

    def test_second_delete_is_idempotent_but_still_marks_performances(self):
        """已删单再删 ⇒ **幂等成功**（不是 1002），且 `isDelEmp="Y"` 仍标记绩效行、**不写**留痕。

        先以 `N` 删（订单已删、绩效行不动），再以 `Y` 删 ⇒ 绩效行被标掉，而订单两列与
        审计行保持第一次的值（`recycle.soft_delete_order` 的既有口径，spec §4.2）。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        perf = Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        ctx = _delete_ctx(self.sales)
        first = delete_order_write(order, is_del_emp="N", ctx=ctx, dry_run=False)
        self.assertTrue(first.ok, first.msg)
        first_deleted_at = self._reload(order).deleted_at
        self.assertIsNone(perf.deleted_at)           # 夹具前提：第一趟是 N
        second = delete_order_write(order, is_del_emp="Y", ctx=ctx, dry_run=False)
        self.assertTrue(second.ok, second.msg)       # 幂等成功
        row = self._reload(order)
        self.assertEqual(row.deleted_at, first_deleted_at)   # 订单的删除时间**逐字不变**
        perf.refresh_from_db()
        self.assertIsNotNone(perf.deleted_at)        # 但绩效行真的被标掉了
        self.assertEqual(
            OperationLog.objects.filter(action="order.delete").count(), 1
        )


class RefundCoreTests(OrderBatchTestBase):
    """`refund_total_write` 的五条逐笔判据（spec §4.2 的 refunds 行）。

    ⚠️ 退款链路**必须**先建客服 SALES 行（`_apply_sales_deductions` 要读它的
    `commission_rate`）：只建订单会让第 4 条判据（1009）变成另一件事的失败
    （见 brief 的 ⚠️ 那条），⛔ 不要把「只建订单」当成退款用例的前提。
    """

    def _seed_sales_row(self, order, *, commission_rate=Decimal("20.00")):
        """客服行（合成值；`test_refund_partial.py:106` 的同形写法）。"""
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=self.sales,
            temp_amount=Decimal("0.00"),
            bonus_amount=Decimal("8.00"),
            commission_rate=commission_rate,
        )

    # ── ①②③ 内存判据（1007 / 1008×2）────────────────────────────────────

    def test_dry_run_reports_non_completed_order(self):
        """① `status != COMPLETED` ⇒ 1007。"""
        order = self._order(status=OrderStatus.UNASSIGNED)
        self._seed_sales_row(order)
        outcome = refund_total_write(
            order, refund_total=Decimal("5.00"), reason="测试退款原因", request_id="测试键-1",
            ctx=CTX, dry_run=True,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 1007)          # `REFUND_STATE_INVALID`
        self.assertEqual(outcome.detail["exc"].code, 1007)
        self.assertEqual(OrderRefund.objects.count(), 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_dry_run_reports_a_negative_total(self):
        """② `refund_total < 0` ⇒ 1008（⭐ 新口径下 `0` 是合法的「清空」，不再是 1008）。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        self._seed_sales_row(order)
        outcome = refund_total_write(
            order, refund_total=Decimal("-1.00"), reason="测试退款原因", request_id="测试键-2",
            ctx=CTX, dry_run=True,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 1008)          # `REFUND_AMOUNT_INVALID`
        self.assertEqual(outcome.detail["exc"].code, 1008)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_dry_run_reports_order_price_overrun(self):
        """③ `refund_total > total_price` ⇒ 1008（上限 = 毛收款，不是「剩余可退额」）。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("10.00"))
        self._seed_sales_row(order)
        outcome = refund_total_write(
            order, refund_total=Decimal("11.00"), reason="测试退款原因", request_id="测试键-3",
            ctx=CTX, dry_run=True,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 1008)
        self.assertEqual(outcome.detail["exc"].code, 1008)
        self.assertEqual(OrderRefund.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))

    def test_dry_run_reports_missing_sales_commission_rate(self):
        """④ 客服行 `commission_rate` 为空 ⇒ 1009（**不是**「算不出提成」那一支）。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        perf = self._seed_sales_row(order, commission_rate=None)
        outcome = refund_total_write(
            order, refund_total=Decimal("5.00"), reason="测试退款原因", request_id="测试键-4",
            ctx=CTX, dry_run=True,
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, 1009)          # `COMMISSION_NOT_CONFIGURED`
        self.assertEqual(outcome.detail["exc"].code, 1009)
        self.assertIn(f"绩效 {perf.id}", outcome.msg)
        self.assertEqual(OrderRefund.objects.count(), 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)

    # ── `dry_run` 零副作用（spec §4.3）────────────────────────────────────

    def test_dry_run_writes_nothing_on_the_happy_path(self):
        """全部判据通过 ⇒ `dry_run` 仍**零写入**：不建 `OrderRefund`、不改 `refund_fee`、不建调整、不留痕。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        self._seed_sales_row(order)
        outcome = refund_total_write(
            order, refund_total=Decimal("5.00"), reason="测试退款原因", request_id="测试键-5",
            ctx=CTX, dry_run=True,
        )
        self.assertTrue(outcome.ok, outcome.msg)
        self.assertNotIn("record", outcome.detail)    # ⛔ `dry_run` 不返回新行
        self.assertEqual(OrderRefund.objects.count(), 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── 真写路径 ─────────────────────────────────────────────────────────

    def test_real_run_creates_the_refund_row_and_deducts_the_sales_row(self):
        """真写：建 `OrderRefund(kind=SET)`、`refund_fee` **覆盖**成新累计、客服行一条负数调整、
        一条 `order.refund` 留痕。"""
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        perf = self._seed_sales_row(order)
        outcome = refund_total_write(
            order, refund_total=Decimal("50.00"), reason="测试退款原因", request_id="测试键-6",
            ctx=CTX, dry_run=False,
        )
        self.assertTrue(outcome.ok, outcome.msg)
        record = outcome.detail["record"]
        self.assertEqual(record.request_id, "测试键-6")
        self.assertEqual(record.kind, "SET")
        self.assertEqual(record.amount, Decimal("50.00"))
        self.assertEqual(OrderRefund.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        adjustment = PerformanceAdjustment.objects.get(performance=perf)
        self.assertEqual(adjustment.amount, Decimal("-10.00"))   # 50.00 × 20%
        log = OperationLog.objects.get(order_no=order.order_no)
        self.assertEqual(log.action, "order.refund")
        self.assertEqual(log.from_value, "0.00")
        self.assertEqual(log.to_value, "50.00")
        self.assertEqual(log.actor, CTX.log_actor)              # 认证态，不是 `ctx.actor`
        self.assertEqual(log.actor_id, CTX.log_actor_id)

    def test_duplicate_request_id_on_the_real_write_yields_1010(self):
        """⑤ `request_id` 撞具名唯一约束 ⇒ 1010，且整笔回滚（第二个键不留半笔）。

        ⚠️ 第二次必须**改了累计值**（50 → 60）才会 INSERT；设成同值（差额 0）是
        全空操作、**不会**回 1010（那条契约在 `test_refund_partial` 里单独钉）。
        """
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        self._seed_sales_row(order)
        first = refund_total_write(
            order, refund_total=Decimal("50.00"), reason="测试退款原因", request_id="测试键-7",
            ctx=CTX, dry_run=False,
        )
        self.assertTrue(first.ok, first.msg)
        second = refund_total_write(
            order, refund_total=Decimal("60.00"), reason="测试退款原因", request_id="测试键-7",
            ctx=CTX, dry_run=False,
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.code, 1010)          # `REFUND_DUPLICATE_REQUEST`
        self.assertIsInstance(second.detail["exc"], RefundError)
        self.assertEqual(second.detail["exc"].code, 1010)
        self.assertEqual(OrderRefund.objects.count(), 1)
        self.assertEqual(PerformanceAdjustment.objects.count(), 1)   # 第二笔没留半条
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("50.00"))
        self.assertEqual(OperationLog.objects.filter(action="order.refund").count(), 1)


class RefundDelegationTests(OrderBatchTestBase):
    """`refund_set_total` **委托**给共用判据（编排方 Ruling 10）——本类**只**挡「把
    `refund_set_total` 改成不走共用判据（另写一套判据）」这种**显式**改法。

    ⚠️ 两侧**同源**（都调同一个函数）⇒ 本类是「委托即真」的结构：**一致的第二份实现**
    或把共用判据**自己**改坏，都**不会**让本类红（后两者要由直接钉核心的用例与既有退款
    端点测试来抓）。
    ⛔ 不测已删除的 `_remaining_refundable`（绝对值口径下它零调用，2026-10-26 已删）。
    """

    def test_refund_total_denial_is_the_single_source_for_the_three_memory_gates(self):
        order = self._order(status=OrderStatus.UNASSIGNED)
        with self.assertRaises(RefundError) as caught:
            refund_total_denial(order, Decimal("5.00"))
        self.assertEqual(caught.exception.code, 1007)
        # 单笔入口（`refund_set_total`）走的是**同一个**判据 ⇒ 码与 msg 逐字相同。
        with self.assertRaises(RefundError) as via_partial:
            refund_set_total(order, Decimal("5.00"), "测试退款原因", "测试键-源-1", "测试操作人")
        self.assertEqual(via_partial.exception.code, caught.exception.code)
        self.assertEqual(via_partial.exception.message, caught.exception.message)

    def test_sales_commission_gate_is_the_single_source(self):
        order = self._order(status=OrderStatus.COMPLETED, total_price=Decimal("100.00"))
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=self.sales, commission_rate=None
        )
        with self.assertRaises(RefundError) as caught:
            assert_sales_commissions_configured(order)
        self.assertEqual(caught.exception.code, 1009)
        self.assertIn(f"绩效 {perf.id}", caught.exception.message)
        with self.assertRaises(RefundError) as via_partial:
            refund_set_total(
                order, Decimal("5.00"), "测试退款原因", "测试键-源-2", "测试操作人"
            )
        self.assertEqual(via_partial.exception.code, 1009)
        self.assertEqual(via_partial.exception.message, caught.exception.message)
