"""结算 P0-1：撤销结账**拒绝**「系统自动发薪」的行（计划 §1，选 A = 不动钱）。

## 为什么要有这条网

`revoke_settlements` 原来只清绩效行的结账四列（`settle_status` / `settled_at` /
`settled_by` / `settled_amount`），**钱包一分不退**；而完成入账的幂等键正是
`perf.is_settled`（`apps/accounts/payout.py::payout_completed_order`）
⇒ 「完成 → 自动入账 100 → 财务点撤销结账 → 重开再完成」会**再 +100**，同一笔钱付两次。

本批选 **A：拒绝**（计划 §1）。判据 = `settled_by == "system"`：
整批里只要有一条这样的行 ⇒ **整批拒绝**（`1014`）+ 一句能照着做的人话
（「系统已自动发薪，请走退单回冲或人工负向调账」），**一行都不改**。
财务**手工结账**的行（`settled_by` 是操作人名）钱从来没进过钱包 ⇒ 照旧只清四列。

⛔ 为什么不是「回冲」：回冲要新建轮次级幂等键、与调整记录的 `settled_at` 整批清空抢顺序、
还要和「调账可把钱包扣成负数」互相干扰（计划 §1 的选型段）。本批的代价是财务少一颗按钮。

## 判据钉在哪

一律钉**数据库行**（`AGENTS.md` §5-G / E-0009）：钱包余额、绩效行四列、流水条数、
`OperationLog` 条数。⛔ 不断言"函数抛了异常"——那不能回答「钱有没有动」。

⚠️ 期望值写字面值（E-0024）：业务码 `1014`、状态 `"已结账"` / `"未结账"`。
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.payout import payout_completed_order
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.business.order_status import apply_transition
from apps.business.settlement import settle_performances
from tests.p5_auth_helpers import make_logged_in_client

REVOKE_URL = "/api/business/settlements/revoke"

#: 进 `COMPLETED` 需要的节点（与 `tests/test_wallet_payout.py` 逐字相同）。
COMPLETED_NODES = {"order.change_status_completed"}

#: 拒绝文案必须点明的三件事（计划 §1：「文案说明『系统已自动发薪，请走退单回冲或人工负向调账』」）。
REJECT_MSG_PARTS = ("系统已自动发薪", "退单回冲", "人工负向调账")


def _no(n):
    return f"20261022{n:012d}"


class _RevokeFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # ① 自动结账的接单人：必须是**打手 App 注册**来源（D-0279），否则完成不自动入账。
        self.app_taker = User.objects.create_user(username="p0-taker-app")
        StaffProfile.objects.create(
            user=self.app_taker,
            mobile="13700003333",
            alipay_account="p0@example.com",
            alipay_name="测试打手",
            account_source=AccountSource.APP,
        )
        # ② 财务手工结账的接单人：`console` 来源 ⇒ 完成不自动入账，留给财务在后台结。
        self.console_taker = User.objects.create_user(username="p0-taker-console")
        StaffProfile.objects.create(
            user=self.console_taker,
            mobile="13700004444",
            alipay_account="p0c@example.com",
            alipay_name="测试打手乙",
            account_source=AccountSource.CONSOLE,
        )
        # ⚠️ 必须**接住**返回的 client（它的 `SERVER_NAME` 是租户域名）：
        #    `TenantTestCase.self.client` 打的是非租户 host ⇒ django-tenants 会把连接
        #    切到 public schema 且不还原，下一个用例的 setUp 会以
        #    `relation "business_game" does not exist` 假红。
        self.client = make_logged_in_client(
            username="p0-fin", is_superuser=True, games=None
        )
        self.admin_user = User.objects.get(username="p0-fin")
        self._seq = 0

    def _order(self):
        self._seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=_no(self._seq),
            status=OrderStatus.PENDING_REVIEW,
            total_price=Decimal("100.00"),
            hall_price=Decimal("30.00"),
        )

    def _main(self, order, user):
        return Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=user,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
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

    def _auto_settled_perf(self):
        """完成一张 App 打手的单 ⇒ 自动入账 30（`settled_by="system"`）。"""
        order = self._order()
        perf = self._main(order, self.app_taker)
        self._complete(order)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_by, "system")
        return order, perf

    def _manually_settled_perf(self):
        """完成一张 console 打手的单（不自动入账）后，由财务手工结账。"""
        order = self._order()
        perf = self._main(order, self.console_taker)
        self._complete(order)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.UNSETTLED)
        settle_performances([perf.pk], actor="财务", user=self.admin_user)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertEqual(perf.settled_by, "财务")
        return order, perf

    def _revoke(self, ids, *, reason="对账有误，撤销"):
        return self.client.post(
            REVOKE_URL,
            data=json.dumps({"performance_ids": list(ids), "reason": reason}),
            content_type="application/json",
        )


class AutoPaidRowsAreRefusedTests(_RevokeFixture):
    """① 自动结账的行 ⇒ 整批 `1014`，钱包与四列**原样**。"""

    def test_revoking_an_auto_paid_row_is_refused_and_touches_nothing(self):
        _order, perf = self._auto_settled_perf()
        wallet = TakerWallet.objects.get(user=self.app_taker)
        settled_at = perf.settled_at

        response = self._revoke([perf.pk])

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        # ⚠️ 业务码写**字面值**（E-0024：不引被测模块导出的常量）。
        self.assertEqual(body["code"], 1014, body)
        for part in REJECT_MSG_PARTS:
            self.assertIn(part, body["msg"], body)
        # 文案要**指名**是哪一行（否则财务拿着几十行无从下手）。
        self.assertIn(str(perf.pk), body["msg"], body)

        # 钱一分没动。
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind="earning").count(), 1
        )
        # 四列原样（含 `settled_at` 逐值相同：撤销会把它清成 NULL）。
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(perf.settled_at, settled_at)
        self.assertEqual(perf.settled_by, "system")
        self.assertEqual(perf.settled_amount, Decimal("30.00"))
        # 没有写半条撤销留痕（`from=已结账` / `to=未结账` 那一对）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle",
                from_value="已结账",
                to_value="未结账",
            ).count(),
            0,
        )


class ManuallySettledRowsStillRevokeTests(_RevokeFixture):
    """② 财务手工结账的行 ⇒ 照旧只清四列（回归网）。"""

    def test_a_manually_settled_row_still_revokes_normally(self):
        _order, perf = self._manually_settled_perf()
        self.assertIsNotNone(perf.settled_at)
        self.assertEqual(perf.settled_amount, Decimal("30.00"))

        body = self._revoke([perf.pk], reason="对账有误，撤销").json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["revoked_performances"], [perf.pk])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertEqual(perf.settled_by, "")
        self.assertIsNone(perf.settled_amount)
        # 手工结账的钱从来没进过钱包 ⇒ 撤销也一分不动（钱包行都不该被建出来）。
        self.assertFalse(TakerWallet.objects.filter(user=self.console_taker).exists())


class MixedBatchesAreRefusedAsAWholeTests(_RevokeFixture):
    """③ 一批里同时有 system 行与手工行 ⇒ 整批拒绝，**一行都不改**。"""

    def test_a_mixed_batch_is_refused_and_no_row_is_touched(self):
        _system_order, system_perf = self._auto_settled_perf()
        _manual_order, manual_perf = self._manually_settled_perf()
        system_settled_at = system_perf.settled_at
        manual_settled_at = manual_perf.settled_at

        body = self._revoke([system_perf.pk, manual_perf.pk]).json()

        self.assertEqual(body["code"], 1014, body)
        self.assertIn(str(system_perf.pk), body["msg"], body)

        # 自动结账那一行原样。
        system_perf.refresh_from_db()
        self.assertEqual(system_perf.settle_status, "已结账")
        self.assertEqual(system_perf.settled_at, system_settled_at)
        self.assertEqual(system_perf.settled_by, "system")
        self.assertEqual(system_perf.settled_amount, Decimal("30.00"))
        # ⭐ 手工结账那一行也**必须原样**：整批拒绝不是「跳过坏行、照改好行」。
        manual_perf.refresh_from_db()
        self.assertEqual(manual_perf.settle_status, "已结账")
        self.assertEqual(manual_perf.settled_at, manual_settled_at)
        self.assertEqual(manual_perf.settled_by, "财务")
        self.assertEqual(manual_perf.settled_amount, Decimal("30.00"))
        # 钱包与留痕同样不动。
        self.assertEqual(
            TakerWallet.objects.get(user=self.app_taker).balance, Decimal("30.00")
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="performance.settle",
                from_value="已结账",
                to_value="未结账",
            ).count(),
            0,
        )
