"""⭐ H3-2（2026-10-28）：远端「已结算」同步**本地绩效结算状态**。

业务口径（业务方原话）：代练通打手申请交单后，**超过 48 小时未处理**平台会**自动验收为
完成**，钱直接到对方账户 ⇒ **钱已经付出去了，本地结算状态必须一致**。

## 覆盖什么

| 组 | 判据 |
|---|---|
| 远端 17 | 本地订单 `COMPLETED`（A 线既有映射）+ 本单**打单绩效** `settle_status=已结账` + `settled_at` 有值 |
| 客服提成 | **一个字都不动**（`SALES` 行仍「未结账」、`settled_at` 为空） |
| 幂等 | 已结账的不重复写（第二次调用零审计行、`settled_at` 不变） |
| 脏行 | 状态列「未结账」却留着结账痕迹（`settled_amount`）⇒ ⛔ 不覆盖 |
| 审计 | 机器行（`actor_id=None`），文案「代练通已结算 ⇒ 绩效置已结」 |
| 远端 13 | **不动**结算状态 |
| 失败 | 可读原因写进 `last_error`，**不假报成功**，且**保持轮询**（下一轮重试） |

## 不访问真实网络

上游一律 `httpx.MockTransport`（打桩点是 `apps.business.dailiantong_push._open_client`，
见 `AGENTS.md` §5-E）。一切凭据 / 单号都是**合成值**。
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business import dailiantong_push as push_module
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.order_status import IllegalTransition, apply_transition, machine_target_nodes
from tests.test_dailiantong_sync import (  # noqa: E402 —— 重夹具，见 `test_platform_adapters.py` 的先例
    _FakeUpstream,
    _SyncTestBase,
)

#: 合成上游单号（⛔ 不是真实单号）。
SERIAL_SETTLED = "19000000000000000301"
SERIAL_WAITING = "19000000000000000302"


class _SequenceFake(_FakeUpstream):
    """在 `_SyncTestBase` 的假上游上加一个「按顺序回详情」的队列（手动完单要写前 13 / 写后 17）。"""

    def __init__(self):
        super().__init__()
        self.next_details: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        action = form.get("Action", "")
        if action == "LevelOrderDetail" and self.next_details:
            self.calls.append(("LevelOrderDetail", form))
            return httpx.Response(200, json=self.next_details.pop(0))
        if action == "LevelOrderOver":
            # 确认完单：上游回成功码，状态由「写后回读」证明（本假上游的详情队列负责）。
            self.calls.append((action, form))
            return httpx.Response(200, json={"Result": 1, "Err": "完单成功."})
        if action == "LevelOrderValue":
            self.calls.append((action, form))
            return httpx.Response(200, json={"Result": 1, "Err": "评价成功."})
        return super().handler(request)


class _SettleFixtureMixin:
    """一个订单 + 打单绩效（MAIN）+ 客服提成（SALES）。"""

    def _make_order(self, *, order_no: str, status: str = OrderStatus.COMPLETED) -> Order:
        # 游戏 / 区服复用 `_SyncTestBase.setUp` 建好的那一对（⛔ 不另建，避免同名脏数据）。
        return Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=order_no,
            status=status,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )

    def _people(self):
        users = get_user_model().objects
        taker = users.create_user(username="dlt-settle-taker", password="synth-pw-0002")
        sales = users.create_user(username="dlt-settle-sales", password="synth-pw-0003")
        return taker, sales

    def _performances(self, order: Order, *, taker, sales):
        main = Performance.objects.create(
            order=order,
            user=taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("40.00"),
            bonus_amount=Decimal("40.00"),
            claimed_at=timezone.now(),
        )
        commission = Performance.objects.create(
            order=order,
            user=sales,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("6.00"),
            bonus_amount=Decimal("6.00"),
            commission_rate=Decimal("5.00"),
        )
        return main, commission


class SettleFunctionTests(_SettleFixtureMixin, _SyncTestBase):
    """函数级：`settle_performances_on_remote_settled`（远端 17 的那一半）。"""

    def setUp(self):
        super().setUp()
        self.taker, self.sales = self._people()
        self.order = self._make_order(order_no="20261028000000010001")
        self.main, self.commission = self._performances(
            self.order, taker=self.taker, sales=self.sales
        )

    def test_the_main_performance_is_settled_and_the_sales_commission_is_untouched(self):
        note = push_module.settle_performances_on_remote_settled(self.order)

        self.assertIn("1", note)
        self.main.refresh_from_db()
        self.commission.refresh_from_db()
        self.assertEqual(self.main.settle_status, SettleStatus.SETTLED)
        self.assertIsNotNone(self.main.settled_at)
        # ⛔ 只写两列：实结金额 / 结算人都**不许**凭空写（钱不是我们发的）
        self.assertIsNone(self.main.settled_amount)
        self.assertEqual(self.main.settled_by, "")

        # ⛔ 客服提成一个字都不动
        self.assertEqual(self.commission.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(self.commission.settled_at)
        self.assertIsNone(self.commission.settled_amount)

    def test_the_audit_row_is_machine_sourced_and_readable(self):
        push_module.settle_performances_on_remote_settled(self.order)

        row = OperationLog.objects.get(action="performance.settle")
        self.assertEqual(row.log_type, 20, "绩效相关")
        self.assertIsNone(row.actor_id, "机器行：actor_id=None")
        self.assertEqual(row.actor, push_module.SYNC_ACTOR)
        self.assertEqual(row.target_id, str(self.taker.pk))
        self.assertEqual(row.target_name, "dlt-settle-taker")
        self.assertEqual(row.order_no, self.order.order_no)
        self.assertEqual(row.from_value, "未结账")
        self.assertEqual(row.to_value, "已结账")
        self.assertIn("代练通已结算 ⇒ 绩效置已结", row.reason)
        self.assertIn("已结账", row.summary)

    def test_it_is_idempotent(self):
        push_module.settle_performances_on_remote_settled(self.order)
        self.main.refresh_from_db()
        settled_at = self.main.settled_at
        rows_before = OperationLog.objects.filter(action="performance.settle").count()

        note = push_module.settle_performances_on_remote_settled(self.order)

        self.assertEqual(note, "", "已经结过账 ⇒ 不再写、也不报成功")
        self.main.refresh_from_db()
        self.assertEqual(self.main.settled_at, settled_at, "不重复写 settled_at")
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(),
            rows_before,
            "不重复写审计行",
        )

    def test_a_dirty_row_with_settlement_traces_is_not_overwritten(self):
        """状态列还是「未结账」、却留着 `settled_amount` ⇒ ⛔ 不覆盖（那是已经发过的钱）。"""
        Performance.objects.filter(pk=self.main.pk).update(
            settled_amount=Decimal("40.00")
        )

        note = push_module.settle_performances_on_remote_settled(self.order)

        self.main.refresh_from_db()
        self.assertEqual(self.main.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(self.main.settled_at)
        self.assertEqual(self.main.settled_amount, Decimal("40.00"), "快照不许被覆盖")
        self.assertEqual(note, "")

    def test_an_order_without_a_main_performance_is_a_no_op(self):
        Performance.objects.all().delete()
        self.assertEqual(push_module.settle_performances_on_remote_settled(self.order), "")
        self.assertFalse(OperationLog.objects.exists())


class RemoteStatus17SyncTests(_SettleFixtureMixin, _SyncTestBase):
    """整条链路：`dailiantong_sync --once` 读到远端 17 ⇒ 订单已完成 + 绩效已结。"""

    def setUp(self):
        super().setUp()
        self.taker, self.sales = self._people()
        self.order = self._make_order(
            order_no="20261028000000020001", status=OrderStatus.PENDING_REVIEW
        )
        self.main, self.commission = self._performances(
            self.order, taker=self.taker, sales=self.sales
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=SERIAL_SETTLED,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=1,
            remote_status=13,
        )

    def test_remote_17_marks_the_order_completed_and_settles_the_main_performance(self):
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        out, err = self._run_once()

        self.assertEqual(err, "")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.main.refresh_from_db()
        self.commission.refresh_from_db()
        self.assertEqual(self.main.settle_status, SettleStatus.SETTLED)
        self.assertIsNotNone(self.main.settled_at)
        self.assertEqual(
            self.commission.settle_status,
            SettleStatus.UNSETTLED,
            "客服提成⛔ 不许被标成已结",
        )
        self.assertIsNone(self.commission.settled_at)
        self.push.refresh_from_db()
        self.assertIn("已结账", self.push.remote_note, "本次同步的业务结论要写进 remote_note")
        # 审计：机器行 + 指定文案
        row = OperationLog.objects.get(action="performance.settle")
        self.assertIsNone(row.actor_id)
        self.assertIn("代练通已结算 ⇒ 绩效置已结", row.reason)

    def test_remote_13_does_not_touch_the_settlement(self):
        self.fake.details[SERIAL_SETTLED] = {"Status": 13, "CancelStatus": 10}

        self._run_once()

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.main.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(self.main.settled_at)
        self.assertFalse(OperationLog.objects.filter(action="performance.settle").exists())

    def test_already_settled_manual_acceptance_can_now_complete_via_sync(self):
        platform_taker = push_module.placeholder_taker_user()
        settled_at = timezone.now()
        Performance.objects.filter(pk=self.main.pk).update(
            user=platform_taker,
            settle_status=SettleStatus.SETTLED,
            settled_at=settled_at,
            bonus_amount=Decimal("0.00"),
        )
        StaffProfile.objects.create(
            user=self.sales, commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("20.00"),
        )
        Order.objects.filter(pk=self.order.pk).update(sales_user=self.sales)
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        with self.assertRaisesRegex(IllegalTransition, "绩效已结账"), transaction.atomic():
            apply_transition(self.order, OrderStatus.COMPLETED,
                             nodes=machine_target_nodes(OrderStatus.COMPLETED))

        self._run_once()

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.commission.refresh_from_db()
        self.push.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED, self.push.last_error)
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.main.user_id, platform_taker.pk)
        self.assertEqual(self.main.settled_at, settled_at)
        self.assertIsNone(self.main.settled_amount)
        self.assertEqual(self.main.bonus_amount, Decimal("40.00"))
        self.assertEqual(self.main.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.commission.bonus_amount, Decimal("16.00"))
        self.assertEqual(self.commission.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(self.push.state, DailiantongPush.State.CLOSED)
        self.assertFalse(self.push.is_active)
        self.assertEqual(self.push.last_error, "")
        self.assertFalse(OperationLog.objects.filter(action="performance.settle").exists())

    def test_an_active_account_with_the_placeholder_name_does_not_bypass_settled_guard(self):
        same_name = get_user_model().objects.create_user(
            username=push_module.VIRTUAL_TAKER_USERNAME, password="synth-pw-0012",
        )
        settled_at = timezone.now()
        Performance.objects.filter(pk=self.main.pk).update(
            user=same_name, settle_status=SettleStatus.SETTLED, settled_at=settled_at,
        )
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        self._run_once()

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.push.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.main.settled_at, settled_at)
        self.assertIn("已有一个可登录的账号", self.push.last_error)
        self.assertTrue(self.push.is_active)

    def test_already_activated_or_snapshotted_cost_is_preserved(self):
        platform_taker = push_module.placeholder_taker_user()
        settled_at = timezone.now()
        Performance.objects.filter(pk=self.main.pk).update(
            user=platform_taker, settle_status=SettleStatus.SETTLED, settled_at=settled_at,
            bonus_amount=Decimal("36.00"), settled_amount=Decimal("35.00"),
            review_status=ReviewStatus.APPROVED,
        )
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        self._run_once()

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.main.bonus_amount, Decimal("36.00"))
        self.assertEqual(self.main.settled_amount, Decimal("35.00"))
        self.assertEqual(self.main.settled_at, settled_at)

    def test_remote_17_does_not_reopen_a_local_refund(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.REFUNDED)
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        self._run_once()

        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.push.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        self.assertEqual(self.main.user_id, self.taker.pk)
        self.assertEqual(self.main.settle_status, SettleStatus.UNSETTLED)
        self.assertIn("终态不来回改", self.push.remote_note)

    def test_a_settlement_failure_is_readable_and_keeps_the_row_polling(self):
        """绩效置结失败 ⇒ 可读原因 + **保持轮询**（下一轮重试），⛔ 不假报成功。"""
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}

        def fail_settlement(order):
            snapshot = DailiantongPush.objects.get(pk=self.push.pk)
            self.assertEqual(snapshot.remote_status, 17)
            self.assertTrue(snapshot.is_active)
            raise RuntimeError("simulated db failure")

        with mock.patch.object(
            push_module, "settle_performances_on_remote_settled", side_effect=fail_settlement,
        ), mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            self.push = push_module.refresh_push_status(self.push)

        self.push.refresh_from_db()
        self.order.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertIsNone(self.order.completed_at)
        self.assertEqual(self.main.user_id, self.taker.pk)
        self.assertFalse(OperationLog.objects.filter(action="order.transition").exists())
        self.assertIn("绩效置为已结账失败", self.push.last_error)
        self.assertIn("simulated db failure", self.push.last_error)
        self.assertTrue(
            self.push.is_active,
            "本地没写成 ⇒ 不能因为「远端读到了」就停掉轮询（下一轮重试）",
        )
        self.assertEqual(self.main.settle_status, SettleStatus.UNSETTLED)

    def test_the_next_round_retries_and_succeeds(self):
        """失败之后下一轮（真实的实现）必须把绩效补上 —— 幂等 + 可重试。"""
        self.fake.details[SERIAL_SETTLED] = {"Status": 17, "CancelStatus": 10}
        with mock.patch.object(
            push_module,
            "settle_performances_on_remote_settled",
            side_effect=RuntimeError("boom"),
        ), mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            self.push = push_module.refresh_push_status(self.push)

        # 第二轮：不再打桩 ⇒ 真的把那两列写上
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        ):
            self.push = push_module.refresh_push_status(self.push)

        self.main.refresh_from_db()
        self.assertEqual(self.main.settle_status, SettleStatus.SETTLED)
        self.assertIsNotNone(self.main.settled_at)
        self.push.refresh_from_db()
        self.assertEqual(self.push.last_error, "", "成功后清空 last_error")


class ManualAcceptSettlesTests(_SettleFixtureMixin, _SyncTestBase):
    """⭐ 手动「确认完单」（批量验收）同样把绩效置已结（否则远端 17 后不再轮询，永远结不了）。"""

    def setUp(self):
        super().setUp()
        self.fake = _SequenceFake()
        self.taker, self.sales = self._people()
        self.order = self._make_order(
            order_no="20261028000000030001", status=OrderStatus.PENDING_REVIEW
        )
        self.main, self.commission = self._performances(
            self.order, taker=self.taker, sales=self.sales
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=SERIAL_WAITING,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=1,
            remote_status=13,
        )

    def test_manual_finish_completes_the_order_and_settles_the_main_performance(self):
        from apps.business import dailiantong_actions as actions_module

        operator = get_user_model().objects.create_user(
            username="dlt-settle-operator", password="synth-pw-0004"
        )
        # 写前回读 = 13（等待验收）→ 写后回读 = 17（已结算）
        self.fake.next_details = [
            {"Status": 13, "CancelStatus": 10},
            {"Status": 17, "CancelStatus": 10},
        ]

        with mock.patch.object(
            actions_module, "_open_client", side_effect=self._client_factory
        ):
            actions_module.accept_push(self.order, operator=operator)

        self.main.refresh_from_db()
        self.commission.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.main.settle_status, SettleStatus.SETTLED)
        self.assertEqual(self.commission.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertIsNotNone(self.order.completed_at)
        self.assertEqual(self.fake.actions.count("LevelOrderOver"), 1)
        self.assertEqual(self.fake.actions.count("LevelOrderDetail"), 2)
        self.push.refresh_from_db()
        self.assertEqual(self.push.state, DailiantongPush.State.CLOSED)
        self.assertFalse(self.push.is_active)

    def test_the_settled_push_can_be_valued_after_manual_finish_closes_it(self):
        from apps.business import dailiantong_actions as actions_module
        from apps.business import dailiantong_extra_actions as extra_module

        operator = get_user_model().objects.create_user(username="dlt-value-operator")
        settled = {"Status": 17, "CancelStatus": 10, "AcceptUID": "USR-TAKER-SYNTH"}
        self.fake.next_details = [{"Status": 13, "CancelStatus": 10}, settled, settled]
        with mock.patch.object(actions_module, "_open_client", side_effect=self._client_factory):
            accepted = actions_module.accept_push(self.order, operator=operator)
        self.assertEqual(accepted.state, DailiantongPush.State.CLOSED)

        with mock.patch.object(extra_module, "_open_client", side_effect=self._client_factory):
            valued = extra_module.value_push_order(self.order, operator=operator, eval_type=10)

        self.assertEqual(valued.pk, accepted.pk)
        self.assertEqual(self.fake.actions.count("LevelOrderValue"), 1)
        valued.refresh_from_db()
        self.assertEqual(valued.state, DailiantongPush.State.CLOSED)
        self.assertFalse(valued.is_active)

    def test_manual_finish_keeps_polling_after_local_write_fails(self):
        from apps.business import dailiantong_actions as actions_module

        operator = get_user_model().objects.create_user(username="dlt-finish-operator")
        self.fake.next_details = [
            {"Status": 13, "CancelStatus": 10},
            {"Status": 17, "CancelStatus": 10},
        ]
        with (
            mock.patch.object(actions_module, "_open_client", side_effect=self._client_factory),
            mock.patch.object(push_module, "apply_remote_status", side_effect=RuntimeError("同步暂时失败")),
        ):
            actions_module.accept_push(self.order, operator=operator)

        self.push.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.push.remote_status, 17)
        self.assertEqual(self.push.state, DailiantongPush.State.SENT)
        self.assertTrue(self.push.is_active)
        self.assertIn("同步暂时失败", self.push.last_error)
        self.assertEqual(self.main.settle_status, SettleStatus.UNSETTLED)

        self.fake.details[SERIAL_WAITING] = {"Status": 17, "CancelStatus": 10}
        self._run_once()

        self.order.refresh_from_db()
        self.push.refresh_from_db()
        self.main.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.push.state, DailiantongPush.State.CLOSED)
        self.assertEqual(self.main.settle_status, SettleStatus.SETTLED)
        self.assertEqual(self.fake.actions.count("LevelOrderOver"), 1)
