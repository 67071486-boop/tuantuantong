"""结算 P0-7（2026-10-22）：上家撤单落库后**走退单钩子**（钱与绩效一起回冲）。

依据：[`docs/plans/2026-10-22-settlement-p1-and-peer.md`](../../docs/plans/2026-10-22-settlement-p1-and-peer.md)
的 §2 + `docs/DECISIONS.md` 的 `D-0288`。

## 一件被修的钱

钱包作为发薪唯一出口之后，**每一张完成单**都会被自动标成「已结账」并把钱打进打手钱包
（`apps/accounts/payout.py::payout_completed_order`，D-0279：只对 App 注册打手）。
而接收侧的撤单支（`apps/business/peer_inbound.py` 的 `kind == "order.withdraw"`）只写了
`order.status = REFUNDED` + 一条订单日志，**不跑** `refunds.on_order_refunded`
⇒ 上家撤单后，已经打进打手钱包的钱**留在钱包里**、绩效行也一个字节没动。

**改法**：`save()` 之后（同一个 `atomic()` 内）对撤单支调
`on_order_refunded(order, previous_status)`，`previous_status` 必须是**赋值之前**的状态
（钩子用它判「这是不是一次真实的退单迁移」，传 `REFUNDED` 会被当成重跑、第 1.5 / 1.6 步全跳过）。
⛔ **仍然不走** `apply_transition`：那个入口要节点集、还会对外回声；这里调的只是**目标态钩子本身**。
失败 ⇒ `set_rollback(True)` + `note_skip`（`code=withdraw_refund_failed`）+ **不推进**
`link.last_applied_at`（配置 / 数据修好后可重投）。

## 判据钉在哪

一律钉**数据库行的值**（`AGENTS.md` §5-G）：`Order.status`、`PeerOrderLink.last_applied_at`、
钱包余额、MAIN / SALES 行的两个金额列与结账痕迹、回冲调整的 `(kind, amount)` 与条数、
订单日志行数。⛔ 不断言「函数被调用了」（那条桩自证放在第 ③ 条用例里，按 E-0029 的口径）。

⚠️ 期望值一律写**字面值**（E-0024）；夹具一律**合成值**（E-0077）。

## 哪些用例在修前是红的（如实登记）

| 用例 | 修前 | 说明 |
|---|---|---|
| `…test_a_withdrawn_completion_claws_the_money_back_and_clears_the_performances` | **红**（钱包停在 `60.00`、MAIN 行仍 `60.00`） | P0-7 本体 |
| `…test_an_unfinished_withdrawal_has_no_money_to_claw_back` | 绿（回归网） | 未完成 ⇒ 无钱可退、⛔ 不许因为多了钩子就报错 |
| `…test_a_failing_hook_rolls_the_whole_event_back` | **红**（钱 / 绩效被改了一半或整条被吞） | 钩子失败 ⇒ 整笔回滚 + skip + 水位不前进 |
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource, CommissionMode
from apps.accounts.models import StaffProfile, TakerWallet
from apps.accounts.payout import payout_completed_order
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from apps.business.performance_amount import AmountError

#: 对端单号（去重键 = `(绑定, 对端单号)`）与本地单号（形状照 `order_no.py` 的产物）。
PEER_NO = "A-P1-1"
LOCAL_NO = "20261022000000000901"

#: 打手提成率（订单价百分比）与客服提成率（**利润**百分比）。
TAKER_RATE = Decimal("60.00")
SALES_RATE = Decimal("40.00")

#: 自动入账那笔钱：`100.00 × 60% = 60.00`（字面值，⛔ 不从被测模块派生）。
PAID = Decimal("60.00")

#: 完成迁移要求的节点门（与本单的角色无关：本文件只调领域函数）。
COMPLETED_NODES = {"order.change_status_completed"}

#: 钩子失败的机器码。⚠️ 这里是**字面值**而不是 import 被测常量：它是给发送侧按码分流的
#: 对外契约，钉字面值才抓得住「有人把码改名」（E-0024 同族：断言钉值、不钉常量）。
WITHDRAW_REFUND_FAILED_CODE = "withdraw_refund_failed"


class _WithdrawFixture(TenantTestCase):
    """游戏 + 一个 App 注册打手 + 一条上下游绑定 + 一张本地单（含 `PeerOrderLink`）。"""

    #: `T1` = 本地已应用到的时刻（`link.last_applied_at`）；`T2` 是这条撤单事件的时刻。
    T1 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="结算P1撤单游戏")
        self.kefu = get_user_model().objects.create_user(
            username="p1-withdraw-kefu", password="pw-canary-p1-withdraw"
        )
        StaffProfile.objects.create(
            user=self.kefu,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        # ⚠️ 「App 注册」是承重的：自动入账只对 `account_source == "app"` 的接单人生效（D-0279）。
        self.taker = get_user_model().objects.create_user(
            username="p1-withdraw-taker", password="pw-canary-p1-withdraw"
        )
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
            account_source=AccountSource.APP,
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="结算P1上家",
            peer_base_url="https://peer-p1-wd.test.invalid",
            peer_account="peer-account-p1-wd",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-p1-wd-down.test.invalid",
            bound_by=self.kefu,
        )

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _order(self, *, status, total="100.00"):
        return Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=status,
            total_price=Decimal(total),
            hall_price=Decimal(total),
            sales_user=self.kefu,
        )

    def _link(self, order):
        return PeerOrderLink.objects.create(
            order=order,
            upstream_binding=self.binding,
            peer_order_no=PEER_NO,
            last_applied_at=self.T1,
        )

    def _taker_perf(self, order):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=self.taker,
            temp_amount=Decimal("60.00"),
            bonus_amount=Decimal("60.00"),
        )

    def _sales_perf(self, order):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.SALES,
            user=self.kefu,
            temp_amount=Decimal("12.00"),
            bonus_amount=Decimal("12.00"),
            commission_rate=SALES_RATE,
        )

    def _completed_paid_order(self):
        """一张「已完成 + 已自动入账」的单（钱包里**真的**有 60.00）。

        ⚠️ 客服行在**完成之后**才建：完成那一步会按订单金额重算客服提成，先建会让下面的
        `12.00 → 0.00` 断言在修前 / 修后都失去判别力（期望值会被完成那一步改写）。
        """
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        perf = self._taker_perf(order)
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        # ⛔ **`apply_transition` 不再自动调 `payout_completed_order`**
        #    （spec `2026-10-23-taker-income-and-early-settlement.md` §3.1：钱包不再收
        #    订单绩效）⇒ 本用例要测的是「系统自动发薪之后上家撤单要怎么回冲」，
        #    所以在这里**显式**补上那一跳。「完成单不再自动入账」的判据在
        #    `tests/test_app_income_api.py::CompletionDoesNotCreditWalletTests`。
        payout_completed_order(order)
        perf.refresh_from_db()
        # 夹具自证：这一行**真的**是「系统自动发薪」，且钱**真的**进了钱包。
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(TakerWallet.objects.get(user=self.taker).balance, PAID)
        sales = self._sales_perf(order)
        self._link(order)
        return order, perf, sales

    def _withdraw(self, *, occurred_at=None, reason="refunded", total="100.00"):
        """一条撤单事件：`withdraw_reason` 在**事件级**（⛔ 不在 `payload` 里）。"""
        event = {
            "kind": "order.withdraw",
            "target_ref": PEER_NO,
            "occurred_at": (occurred_at or self.T2).isoformat(),
            "withdraw_reason": reason,
            "actor_name": "结算P1上家客服",
            "payload": {
                "status": OrderStatus.WAITING_GRAB,
                "total_price": total,
                "game_name": self.game.name,
            },
        }
        return event

    def _clawbacks(self):
        return list(
            PerformanceAdjustment.objects.filter(
                request_id__startswith="PAYOUT-CLAWBACK-"
            ).order_by("id")
        )


class WithdrawnCompletionTests(_WithdrawFixture):
    """P0-7 ①：已完成 + 自动入账 ⇒ 上家撤单 ⇒ 钱包回 0、绩效清零、状态 `REFUNDED`。"""

    def test_a_withdrawn_completion_claws_the_money_back_and_clears_the_performances(self):
        """修前：只写状态列 ⇒ 钱包停在 `60.00`、MAIN 行仍是 `60.00 / 60.00`（打手白拿）。"""
        order, perf, sales = self._completed_paid_order()

        result = apply_events([self._withdraw()], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped_details, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # 钱：回冲 −60.00，钱包回 0。
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("0.00"))
        clawbacks = self._clawbacks()
        self.assertEqual(len(clawbacks), 1)
        self.assertEqual(clawbacks[0].amount, Decimal("-60.00"))
        # 绩效：MAIN 行两个金额列清零、结账四列复位；行与 `user` 保留。
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertFalse(perf.is_settled)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(perf.user_id, self.taker.pk)
        # 客服行（未结账的那一条）也清 0（第 2.7 步）。
        sales.refresh_from_db()
        self.assertEqual(sales.temp_amount, Decimal("0.00"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        # 水位前进 + 恰好一条撤单日志（⛔ 不是两条）。
        link = PeerOrderLink.objects.get(pk=PeerOrderLink.objects.get().pk)
        self.assertEqual(link.last_applied_at, self.T2)
        self.assertEqual(
            OperationLog.objects.filter(action="order.peer_withdraw").count(), 1
        )


class UnfinishedWithdrawalTests(_WithdrawFixture):
    """P0-7 ②：未完成的单被撤 ⇒ 无钱可退、⛔ 不报错（钩子照跑，钱那一半自然是 0）。"""

    def test_an_unfinished_withdrawal_has_no_money_to_claw_back(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        perf = self._taker_perf(order)
        self._link(order)

        result = apply_events([self._withdraw()], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped_details, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # 没有钱包行 ⇒ 没有可扣的钱；也没有任何回冲调整。
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        self.assertEqual(self._clawbacks(), [])
        # 绩效照样清零（那是退单钩子的第 2 步，与有没有入账无关）。
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))


class FailingHookTests(_WithdrawFixture):
    """P0-7 ③：钩子失败 ⇒ 整笔回滚、事件 skip、水位不前进（⛔ 不留半笔）。"""

    def test_a_failing_hook_rolls_the_whole_event_back(self):
        """桩打在**被查找的那个模块**上（`apps.business.refunds.on_order_refunded`）。

        ⚠️ 桩自证：下面那条 `skipped_details.reason` 必须带桩抛出的**那句原文**——
        没有它，这条用例就分不清「钩子被跳过了」与「桩根本没接上」。
        """
        order, perf, sales = self._completed_paid_order()

        def _boom(order, from_status):
            raise AmountError("合成桩：撤单退单钩子算不出金额", 1011)

        with mock.patch(
            "apps.business.refunds.on_order_refunded", side_effect=_boom
        ):
            result = apply_events([self._withdraw()], binding=self.binding)

        self.assertEqual(result.applied, 0, result)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(
            result.skipped_details[0]["code"], WITHDRAW_REFUND_FAILED_CODE
        )
        self.assertIn("合成桩：撤单退单钩子算不出金额", result.skipped_details[0]["reason"])
        # 整笔回滚：状态、水位、日志、钱、绩效**一个都没动**。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(PeerOrderLink.objects.get().last_applied_at, self.T1)
        self.assertFalse(
            OperationLog.objects.filter(action="order.peer_withdraw").exists()
        )
        self.assertEqual(TakerWallet.objects.get(user=self.taker).balance, PAID)
        perf.refresh_from_db()
        self.assertEqual(perf.bonus_amount, Decimal("60.00"))
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("12.00"))
        self.assertEqual(self._clawbacks(), [])
