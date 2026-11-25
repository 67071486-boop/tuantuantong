"""结算 P0-4：人工调账**同步结账快照**（计划 §4；只对 MAIN + system 已结账那一支）。

## 为什么要有这条网

`payout_adjustment` 原来只动钱包 + 流水，**不改** `Performance.settled_amount`、
也不把那条调整记录标成已结账 ⇒ 两个错方向：

- 重开再完成（`COMPLETED → PENDING_REVIEW → COMPLETED`）时 `payout_completed_order` 的补差
  分支按 `payable_net − settled_amount` 算 ⇒ 同一笔正数调整**被算两遍**（钱包 40 → 50）；
- 全额退单的 `_claw_back_auto_paid_taker` 按 `settled_amount` 回冲 ⇒ 调账那 10 元
  **留在打手余额里**（应 0，实得 10）。

修法（计划 §4，同一事务）：那一支里追加 `settled_amount += amount`（**不夹 0**，允许负）
＋ 把这条调整记录置「已结账 / `settled_at=now()`」。锁序 = **先绩效行 `select_for_update`、
后钱包**（与完成入账同序，防死锁）。

⛔ 其余四支（SALES / 未结账 / 手工结账 / `amount=0`）**提前返回、一个字节都不动**——
它们各有自己的用例钉住（本文件末尾两条 + `tests/test_wallet_payout.py::AdjustmentOffsetTests`）。

## 判据钉在哪

一律钉**数据库行**（`AGENTS.md` §5-G / E-0009）：钱包余额、`Performance.settled_amount`、
那条调整记录的 `settle_status` / `settled_at`、流水条数。期望值写字面值（E-0024）。
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.payout import payout_adjustment, payout_completed_order
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import make_logged_in_client

#: 人工修正端点（`POST /performances/{id}/adjustments`，spec §7）。
ADJUSTMENTS_URL = "/api/business/performances/{performance_id}/adjustments"

COMPLETED_NODES = {"order.change_status_completed"}
#: 「重开」与「退单」共用的节点（与 `tests/test_wallet_payout.py` 逐字相同）。
STATUS_NODES = {"order.change_status"}


def _no(n):
    return f"20261021{n:012d}"


class _AdjustFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 自动结账只对**打手 App 注册**的接单人生效（D-0279）⇒ 这条线必须用 `app` 来源。
        self.taker = User.objects.create_user(username="p0adj-taker")
        StaffProfile.objects.create(
            user=self.taker,
            mobile="13700005555",
            alipay_account="p0adj@example.com",
            alipay_name="测试打手",
            account_source=AccountSource.APP,
        )
        # console 打手：完成不自动入账 ⇒ 用来钉「手工结账那一支一个字节都不动」。
        self.console_taker = User.objects.create_user(username="p0adj-console")
        StaffProfile.objects.create(
            user=self.console_taker,
            mobile="13700006666",
            alipay_account="p0adjc@example.com",
            alipay_name="测试打手乙",
            account_source=AccountSource.CONSOLE,
        )
        # ⚠️ 必须**接住**返回的 client（理由同 `test_settle_p0_revoke` 的夹具注释：
        #    不接住时它是非租户 host 的 `self.client`，会把连接留在 public schema）。
        self.client = make_logged_in_client(
            username="p0adj-fin", is_superuser=True, games=None
        )
        self.admin_user = User.objects.get(username="p0adj-fin")
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────
    def _order(self):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=_no(self._seq),
            status=OrderStatus.PENDING_REVIEW,
            total_price=Decimal("100.00"),
            hall_price=Decimal("30.00"),
        )

    def _main(self, order, *, user=None, amount="30.00"):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=user or self.taker,
            temp_amount=Decimal(amount),
            bonus_amount=Decimal(amount),
        )

    def _complete(self, order):
        """推进「已完成」，并**显式**跑一次工资入账（2026-10-23 起不再自动）。

        ⛔ `apps.business.order_status.apply_transition` 已不调
        `payout_completed_order`（spec `2026-10-23-taker-income-and-early-settlement.md`
        §3.1：钱包不再收订单绩效）⇒ 本文件的用例测的是**入账函数之后**那几段语义
        （结账 / 冲账 / 回退 / 上家跟退），所以在这一跳里显式补上。
        「完成单不再自动入账」那条新口径的判据在
        `tests/test_app_income_api.py::CompletionDoesNotCreditWalletTests`。
        """
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETED_NODES)
        return payout_completed_order(order)

    def _reopen(self, order):
        return apply_transition(order, OrderStatus.PENDING_REVIEW, nodes=STATUS_NODES)

    def _refund(self, order):
        return apply_transition(order, OrderStatus.REFUNDED, nodes=STATUS_NODES)

    def _post_adjustment(self, perf, amount, *, request_id=None):
        return self.client.post(
            ADJUSTMENTS_URL.format(performance_id=perf.pk),
            data=json.dumps(
                {
                    "amount": amount,
                    "reason": "对账修正",
                    "request_id": request_id or f"p0adj-{perf.pk}-{amount}",
                }
            ),
            content_type="application/json",
        )

    def _auto_settled(self, *, amount="30.00"):
        """完成一张 App 打手的单 ⇒ 自动入账（钱包 = 快照 = amount）。"""
        order = self._order()
        perf = self._main(order, amount=amount)
        self._complete(order)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_by, "system")
        return order, perf


class PositiveAdjustmentTests(_AdjustFixture):
    """判据 1 / 2：+10 之后快照跟着涨，重开再完成**不再补差**。"""

    def test_a_positive_adjustment_moves_the_wallet_and_the_snapshot_together(self):
        _order, perf = self._auto_settled()
        self.assertEqual(perf.settled_amount, Decimal("30.00"))

        response = self._post_adjustment(perf, "10.00")

        body = response.json()
        self.assertEqual(body["code"], 0, body)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("40.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("40.00"))
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(perf.settled_by, "system")

        # 那条调整记录自己也必须是「已结账」：否则它会以「未结账调整」的身份
        # 再进下一次结账（`payable_net` 再加一遍）。
        adjustment = PerformanceAdjustment.objects.get(
            performance=perf, kind=PerformanceAdjustment.Kind.MANUAL
        )
        self.assertEqual(adjustment.amount, Decimal("10.00"))
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

        tx = WalletTransaction.objects.get(wallet=wallet, kind="earning_adjust")
        self.assertEqual(tx.amount, Decimal("10.00"))
        self.assertEqual(tx.balance_after, Decimal("40.00"))

    def test_repeating_the_offset_does_not_add_to_the_snapshot_again(self):
        """同一条调整再冲一次 ⇒ 钱包与快照**都不动**（幂等判据那条流水先于快照写入）。"""
        _order, perf = self._auto_settled()
        self.assertEqual(self._post_adjustment(perf, "10.00").json()["code"], 0)
        adjustment = PerformanceAdjustment.objects.get(performance=perf)

        self.assertIsNone(payout_adjustment(adjustment))

        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("40.00"))
        self.assertEqual(
            TakerWallet.objects.get(user=self.taker).balance, Decimal("40.00")
        )
        self.assertEqual(
            WalletTransaction.objects.filter(kind="earning_adjust").count(), 1
        )

    def test_recompleting_after_the_adjustment_does_not_pay_it_again(self):
        order, perf = self._auto_settled()
        self.assertEqual(self._post_adjustment(perf, "10.00").json()["code"], 0)
        self.assertEqual(
            TakerWallet.objects.get(user=self.taker).balance, Decimal("40.00")
        )

        self._reopen(order)
        self._complete(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        # 修前这里是 50（调账那 10 元被补差分支又算了一遍）。
        self.assertEqual(wallet.balance, Decimal("40.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("40.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning").count(), 1
        )
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=wallet, kind="earning_adjust"
            ).count(),
            1,
        )


class NegativeAdjustmentTests(_AdjustFixture):
    """判据 4：负向 −10 ⇒ 钱包 20、快照 20（**不夹 0**，允许负）。"""

    def test_a_negative_adjustment_lowers_the_wallet_and_the_snapshot(self):
        _order, perf = self._auto_settled()

        body = self._post_adjustment(perf, "-10.00").json()

        self.assertEqual(body["code"], 0, body)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("20.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("20.00"))
        adjustment = PerformanceAdjustment.objects.get(
            performance=perf, kind=PerformanceAdjustment.Kind.MANUAL
        )
        self.assertEqual(adjustment.amount, Decimal("-10.00"))
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)


    def test_recompleting_after_a_negative_adjustment_does_not_give_it_back(self):
        """评审阻断 1：负向 −10 之后重开再完成，⛔ 不能把扣回的 10 元补回去。

        根因（修前）：补差分支的「应付」只算 `bonus + 未结账调整`（= 30），而
        `settled_amount` 已经是 20（P0-4 同步过）⇒ 差 +10 ⇒ 把刚扣的钱又发一次。
        修法：应付改成 `bonus + 全部调整`（含已结账）⇒ 应付 20、已发 20 ⇒ 差 0。
        **杀法**：把 `due_total` 换回 `amount`（只算未结账）⇒ 这条会看到 30.00。
        """
        order, perf = self._auto_settled()
        self.assertEqual(self._post_adjustment(perf, "-10.00").json()["code"], 0)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("20.00"))

        self._reopen(order)
        self._complete(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, Decimal("20.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, Decimal("20.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning").count(), 1
        )


class FullRefundAfterAdjustmentTests(_AdjustFixture):
    """判据 3：完成 → +10 → 全额退单 ⇒ 钱包 0（不是 10）。"""

    def test_a_full_refund_takes_the_adjusted_amount_back_too(self):
        order, perf = self._auto_settled()
        self.assertEqual(self._post_adjustment(perf, "10.00").json()["code"], 0)
        self.assertEqual(
            TakerWallet.objects.get(user=self.taker).balance, Decimal("40.00")
        )

        self._refund(order)

        wallet = TakerWallet.objects.get(user=self.taker)
        # 回冲读的是 `settled_amount`（40）⇒ 修前它读 30，这里会是 10。
        self.assertEqual(wallet.balance, Decimal("0.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(perf.settled_amount)


class UntouchedBranchesTests(_AdjustFixture):
    """⛔ 未结账 / 手工结账两支行早返回：钱包、快照、调整记录**一个字节都不动**。"""

    def test_an_unsettled_row_is_not_touched_by_the_adjustment(self):
        order = self._order()
        perf = self._main(order)

        body = self._post_adjustment(perf, "10.00").json()

        self.assertEqual(body["code"], 0, body)
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())
        perf.refresh_from_db()
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(perf.settle_status, "未结账")
        adjustment = PerformanceAdjustment.objects.get(performance=perf)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)

    def test_a_manually_settled_row_is_not_touched_by_the_adjustment(self):
        order = self._order()
        perf = self._main(order, user=self.console_taker)
        self._complete(order)
        settle_performances([perf.pk], actor="财务", user=self.admin_user)
        perf.refresh_from_db()
        self.assertEqual(perf.settled_by, "财务")
        settled_amount = perf.settled_amount
        settled_at = perf.settled_at

        body = self._post_adjustment(perf, "10.00").json()

        self.assertEqual(body["code"], 0, body)
        # 钱从来没进过钱包 ⇒ 一分不动、钱包行都不建。
        self.assertFalse(TakerWallet.objects.filter(user=self.console_taker).exists())
        perf.refresh_from_db()
        self.assertEqual(perf.settled_amount, settled_amount)
        self.assertEqual(perf.settled_at, settled_at)
        self.assertEqual(perf.settled_by, "财务")
        # 这条调整记录照旧留在「未结账」，进下一次人工结账。
        adjustment = PerformanceAdjustment.objects.get(performance=perf)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
