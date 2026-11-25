"""结算 P0-6（2026-10-22）：上家退单的跟退闸对「系统自动发薪」开例外。

依据：[`docs/plans/2026-10-22-settlement-p1-and-peer.md`](../../docs/plans/2026-10-22-settlement-p1-and-peer.md)
的 §1 + `docs/DECISIONS.md` 的 `D-0287`。

## 一件被修的钱

钱包作为发薪唯一出口之后，**每一张完成单**都会被自动标成「已结账」（`settled_by="system"`，
`apps/accounts/payout.py::payout_completed_order`）。而跟退闸
（`apps/business/jidanpu_common.py::_b1_status_rollback_blocked`）原来只判
「`COMPLETED` **且** MAIN 绩效行 `is_settled`」⇒ **每一笔上家退单都被挡住**，回一句「请人工」
（`SETTLED_KEEP_REASON`）⇒ 上家跟退事实上停机，钱留在打手钱包里。

**改法**：闸的判据收窄成「已结账 **且** `settled_by != "system"` 才挡」（与
`order_status.py` 的「自动入账放行」同一口径）。系统自动发薪的行照常跟退——跟退内部走
`refunds.on_order_refunded` 的 1.6 步（回冲 + 结账痕迹复位）与第 2 步（金额清零）。
⛔ **财务手工结账的行仍挡住**：那些钱从来没进过钱包，扣钱包就是凭空多扣（规格 §3）。

## 判据钉在哪

一律钉**数据库行的值**（`AGENTS.md` §5-G）：`Order.status`、钱包余额、MAIN 行的
`temp_amount` / `bonus_amount` / 结账四列、回冲调整的 `(kind, amount)` 与条数。
⛔ 不断言「函数被调用了」。

⚠️ 期望值一律写**字面十进制值**（E-0024）；夹具一律**合成值**（E-0077）。

## 哪些用例在修前是红的（如实登记）

| 用例 | 修前 | 说明 |
|---|---|---|
| `…test_a_system_settled_completion_is_followed_and_the_wallet_goes_back_to_zero` | **红**（被「挡住」、钱包停在 `60.00`） | P0-6 本体 |
| `…test_a_manually_settled_completion_is_still_blocked` | 绿（回归网） | ⛔ 收窄不许把手工结账那一档也放开 |
| `…test_replaying_the_same_refund_is_only_clawed_back_once` | 绿（回归网） | 幂等出口（修前被挡住 ⇒ 这条今天靠第一条先红） |
| `…test_a_system_settled_row_without_settled_at_fails_loudly` | **红**（静默「挡住」） | 脏行必须**响亮报错**，⛔ 不许静默（D-0287 ③） |
"""
from datetime import datetime
from decimal import Decimal
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource, CommissionMode
from apps.accounts.models import StaffProfile, TakerWallet
from apps.accounts.payout import payout_completed_order
from apps.business import jidanpu_common, jidanpu_upstream
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.order_status import IllegalTransition, apply_transition
from tests.p5_auth_helpers import make_logged_in_client

#: 绑定客服（= 跟退时的节点集来源）：**必须有 `order.change_status`**——它是
#: 「已完成 → 已退单」这条边的节点门（`order_status._CHANGE_STATUS`）。
BINDING_USER = "p1-follow-kefu"
PASSWORD = "pw-canary-p1-follow"

#: 打手提成率（订单价百分比）与客服提成率（**利润**百分比）。
TAKER_RATE = Decimal("60.00")
SALES_RATE = Decimal("40.00")

#: 完成迁移要求的节点门（与本单的实际角色无关：本文件只调领域函数）。
COMPLETED_NODES = {"order.change_status_completed"}

#: 钱包里那笔自动入账的金额：`100.00 × 60% = 60.00`（字面值，⛔ 不从被测模块派生）。
PAID = Decimal("60.00")


class _FollowRefundFixture(TenantTestCase):
    """游戏 + 一个绑定客服（带 `order.change_status`）+ 一个 **App 注册**打手。

    ⚠️ 「App 注册」是承重的：`payout_completed_order` 只对 `account_source == "app"` 的
    接单人自动结账（D-0279）⇒ 用 `console` 的打手，这条线**根本走不到**「已结账」那一档。
    """

    def setUp(self):
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=dt_timezone.utc)
        self.game = Game.objects.create(name="结算P1跟退游戏")
        make_logged_in_client(
            username=BINDING_USER,
            password=PASSWORD,
            groups=("客服",),
            nodes=("order.change_status",),
        )
        self.kefu = User.objects.get(username=BINDING_USER)
        # ⚠️ `make_logged_in_client` **已经**给这个账号建了档案（它要给游戏范围）⇒ 这里只能
        #    `update`，⛔ 不能再 `create`（`StaffProfile.user` 是 1:1，会撞唯一约束）。
        StaffProfile.objects.filter(user=self.kefu).update(
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.taker = User.objects.create_user(username="p1-follow-taker", password=PASSWORD)
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
            account_source=AccountSource.APP,
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.kefu,
            jidanpu_base_url="https://up-p1-follow.test.invalid",
            account="acc-p1-follow",
            password=PASSWORD,
            enabled=True,
        )
        self._seq = 0

    # ── 夹具 ──────────────────────────────────────────────────────────────
    def _order(self, *, status=OrderStatus.PENDING_REVIEW, total="100.00"):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026102200001{self._seq:07d}",
            status=status,
            total_price=Decimal(total),
            hall_price=Decimal(total),
            sales_user=self.kefu,
        )

    def _taker_perf(self, order, *, temp="60.00", bonus="60.00"):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=self.taker,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
        )

    def _main(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    def _clawbacks(self):
        return list(
            PerformanceAdjustment.objects.filter(
                request_id__startswith="PAYOUT-CLAWBACK-"
            ).order_by("id")
        )

    # ── 状态迁移 ──────────────────────────────────────────────────────────
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
        payout_completed_order(order)

    def _follow(self, order):
        return jidanpu_upstream.follow_refund(order, now=self.now, binding=self.binding)


class SystemSettledFollowRefundTests(_FollowRefundFixture):
    """P0-6 ①③：系统自动发薪的完成单 ⇒ 上家退单要真的跟退（钱一起回来）。"""

    def test_a_system_settled_completion_is_followed_and_the_wallet_goes_back_to_zero(self):
        """完成（自动入账 60.00）→ 上家退单 ⇒ 跟退成功、钱包回 0、MAIN 行清零 + 结账痕迹复位。

        修前：闸只判 `is_settled` ⇒ `follow_refund` 回「挡住了」（`changed=False`），
        状态停在 `COMPLETED`、钱包停在 `60.00`（打手白拿）。
        """
        order = self._order()
        perf = self._taker_perf(order)
        self._complete(order)

        # 夹具自证：这一行**真的**是「系统自动发薪」，且钱**真的**进了钱包。
        perf.refresh_from_db()
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.balance, PAID)

        outcome = self._follow(order)

        self.assertTrue(outcome.changed, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("0.00"))
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        # 结账四列复位（钱已退回 ⇒ 这一轮不再算结过账，重做那一轮才能再入账 / 回池）。
        self.assertFalse(perf.is_settled)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        # 回冲只有一条、金额是 −60.00（⛔ 不是「有就行」）。
        clawbacks = self._clawbacks()
        self.assertEqual(len(clawbacks), 1)
        self.assertEqual(clawbacks[0].amount, Decimal("-60.00"))
        # 行与归属人都保留（第 2 步只清金额，⛔ 不删行、⛔ 不清 user）。
        self.assertEqual(perf.user_id, self.taker.pk)

    def test_replaying_the_same_refund_is_only_clawed_back_once(self):
        """同一笔退单重放 ⇒ 幂等出口（`changed=False`），钱包**不再**被扣第二次。"""
        order = self._order()
        self._taker_perf(order)
        self._complete(order)
        self.assertTrue(self._follow(order).changed)

        replay = self._follow(order)

        self.assertFalse(replay.changed, replay)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        TakerWallet.objects.get(user=self.taker).refresh_from_db()
        self.assertEqual(TakerWallet.objects.get(user=self.taker).balance, Decimal("0.00"))
        self.assertEqual(len(self._clawbacks()), 1)


class ManuallySettledFollowRefundTests(_FollowRefundFixture):
    """P0-6 ②：**财务手工结账**的完成单仍挡住（那些钱从来没进过钱包）。"""

    def test_a_manually_settled_completion_is_still_blocked(self):
        order = self._order(status=OrderStatus.COMPLETED)
        perf = self._taker_perf(order)
        Performance.objects.filter(pk=perf.pk).update(
            settle_status=SettleStatus.SETTLED,
            settled_at=timezone.now(),
            settled_by="p1-finance-manual",
            settled_amount=Decimal("60.00"),
        )
        perf.refresh_from_db()
        # 夹具自证：这一行是「已结账 + 手工来源」。
        self.assertTrue(perf.is_settled)
        self.assertNotEqual(perf.settled_by, "system")

        outcome = self._follow(order)

        self.assertFalse(outcome.changed, outcome)
        self.assertIn("挡住", outcome.reason)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("60.00"))
        self.assertEqual(perf.bonus_amount, Decimal("60.00"))
        self.assertEqual(perf.settled_by, "p1-finance-manual")
        self.assertEqual(self._clawbacks(), [])
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())


class DirtySettledRowTests(_FollowRefundFixture):
    """P0-6 ④：`settled_by="system"` 但 `settled_at` 为空的脏行 ⇒ **响亮报错**（⛔ 不静默）。"""

    def test_a_system_settled_row_without_settled_at_fails_loudly(self):
        """脏行不再被「挡住」吞掉，而是在回冲的幂等键格式化处**抛异常**（整笔回滚）。

        ⚠️ 这是**有意**的行为（D-0287 ③）：生产上把「已结账 + `settled_by="system"`」
        写得出来的只有 `payout_completed_order`，它四列同写 ⇒ 这种行只可能是数据缺陷。
        缺陷必须响亮（E-0022：不可预期的错误不许伪装成「没什么要回冲的」），
        ⛔ 不许回落成「请人工」把一笔已经付出去的钱留在钱包里。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        perf = self._taker_perf(order)
        Performance.objects.filter(pk=perf.pk).update(
            settle_status=SettleStatus.SETTLED,
            settled_at=None,
            settled_by="system",
            settled_amount=Decimal("60.00"),
        )
        perf.refresh_from_db()
        # 夹具自证：闸**放行**了这一行（收窄后的判据不再挡住 system 行）——否则下面测的是
        # 「被挡住」，根本走不到回冲那一句。
        self.assertFalse(jidanpu_common.status_rollback_blocked(order, perf))

        with self.assertRaises(TypeError):
            self._follow(order)

        # 整笔回滚：状态一个字没改、也没有留下半条回冲。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        perf.refresh_from_db()
        self.assertEqual(perf.bonus_amount, Decimal("60.00"))
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(self._clawbacks(), [])


class SettledDenialBranchLedgerTests(_FollowRefundFixture):
    """`_follow_refund_row` 的「结账挡住」异常支：台账写人话，**不再 NameError**。

    背景：该支原先
    `REMOVED_TAKER_SETTLED_REASON.format(count=count)` 引用了**不存在的 `count`**
    （那个 miss 计数属于「移除接手人」作用域）⇒ 真走到即 NameError、整轮回拉崩。
    可达路径实证：`apply_transition` 的 `settled_status_change_denial` 抛的
    `IllegalTransition` 消息含「结账」（`order_status.py`）——即 `follow_refund`
    的预检闸（`status_rollback_blocked`）没拦住、状态机里第二层闸拦住的形状。
    """

    def test_settled_illegal_transition_writes_failed_ledger_without_name_error(self):
        order = self._order(status=OrderStatus.COMPLETED)
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding,
            order=order,
            upstream_order_id="remote-settled-branch-1",
            in_hall=False,
            last_in_at=self.now,
        )
        message = "非法状态迁移：COMPLETED → REFUNDED（该单绩效已结账，请先撤销结账）"
        # ⛔ 桩打在**被查找的模块**（E-0029）：`_follow_refund_row` 以裸名查
        #    `jidanpu_upstream.follow_refund`。
        with mock.patch.object(
            jidanpu_upstream, "follow_refund",
            side_effect=IllegalTransition(message),
        ):
            outcome = jidanpu_upstream._follow_refund_row(
                link, binding=self.binding, now=self.now, terminated=False,
            )

        self.assertTrue(outcome.failed)
        self.assertFalse(outcome.applied)
        row = JidanpuSyncEvent.objects.filter(
            target_type="order", target_ref=order.order_no, status="failed",
        ).latest("id")
        self.assertEqual(row.reason, jidanpu_upstream.SETTLED_KEEP_REASON)
        # 状态一个字没动（「只标记、不改状态」口径）。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
