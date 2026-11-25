"""前三单快路的**下线判据**（2026-10-23，spec §8.2）。

本文件原先测的是「快路存在」：额度 = 前三单入账流水之和（封顶 300）、金额 ≤100 自动通过
并跳过冷静期、超额回到普通 `pending`、已退回的快路单不占额度。

业务方拍板**整段下线**（`docs/specs/2026-10-23-taker-income-and-early-settlement.md`
§8.2）⇒ 那些判据全部作废。这里改钉「下线之后的行为」，⛔ 一条都不留给旧行为作证：

1. 任何金额的提现申请都是 `pending`：不自动通过、不写 `reviewed_at` / `approved_at` /
   `payout_after`、不发 `finance.withdraw_approve` 审计；
2. 新单一律 `fast_lane=False`（列保留，只为历史行）；
3. `/api/app/wallet/` 的 `fast_lane_remaining` 仍在、恒 `"0.00"`（老版本 App 不崩），
   且 `fast_lane_cap` 已不在载荷里（那条算法的唯一消费者已删）；
4. `fast_lane_*` 额度函数已从 `apps.accounts.wallet` 删除（「整段下线」的结构判据）。

⚠️ 判据全部钉**数据库行 / 响应体**，⛔ 不断言「函数没被调用」那种中间层。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import TakerWallet, WithdrawOrder
from apps.accounts.wallet import request_withdraw
from apps.audit.models import OperationLog
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_wallet_payout import PayoutFixture

#: 老口径下「前三单快路」赖以成立的那个金额（≤100 才会自动通过）。
#: 今天它只是一个普通金额——用它当夹具，判据才有判别力（旧实现在这个金额上必然通过）。
OLD_FAST_LANE_AMOUNT = Decimal("100.00")


class NoFastLaneOnRequestTests(PayoutFixture):
    """申请提现：**一律 pending**，没有自动通过那一档。"""

    def _credit_three_orders(self, *, per_order="200.00"):
        """三单各人工入账 `per_order`（旧口径下这会把快路额度顶到封顶 300）。"""
        for _ in range(3):
            order = self._order()
            self._main(
                order,
                temp_amount=Decimal(per_order),
                bonus_amount=Decimal(per_order),
            )
            self._complete(order)  # 夹具里的显式人工入账（见 PayoutFixture._complete）

    def test_a_small_withdrawal_is_pending_not_auto_approved(self):
        self._credit_three_orders()

        order = request_withdraw(self.taker, OLD_FAST_LANE_AMOUNT)

        self.assertFalse(order.fast_lane)
        self.assertEqual(order.state, WithdrawOrder.STATE_PENDING)
        self.assertIsNone(order.reviewed_at)
        self.assertIsNone(order.approved_at)
        self.assertIsNone(order.payout_after)
        self.assertIsNone(order.reviewer_id)
        # 没有「自动通过」那条审计（旧实现会在这一档写一条 `finance.withdraw_approve`）。
        self.assertEqual(
            OperationLog.objects.filter(action="finance.withdraw_approve").count(), 0
        )

    def test_the_smallest_amount_does_not_skip_the_review_either(self):
        """10 元（旧口径下必然走快路）同样只是 `pending`——判据与金额无关。"""
        self._credit_three_orders()

        order = request_withdraw(self.taker, Decimal("10.00"))

        self.assertFalse(order.fast_lane)
        self.assertEqual(order.state, WithdrawOrder.STATE_PENDING)
        self.assertIsNone(order.payout_after)

    def test_the_wallet_balance_is_still_debited(self):
        """提现照旧**立即扣余额**（这条与快路无关，下线不许改坏它）。"""
        self._credit_three_orders(per_order="100.00")
        before = TakerWallet.objects.get(user=self.taker).balance

        request_withdraw(self.taker, OLD_FAST_LANE_AMOUNT)

        after = TakerWallet.objects.get(user=self.taker).balance
        self.assertEqual(after, before - OLD_FAST_LANE_AMOUNT)

    def test_historical_rows_keep_their_flag(self):
        """`fast_lane` **列保留**：历史行仍读得出 `True`，且这一列不会被改写。

        ⚠️ 这是「历史数据不动」那一半的判据（spec §8.2）——⛔ 不是「快路还在」：
        新申请恒 `False`（上面那条用例钉着），这里只证明读旧行不会丢信息。
        """
        legacy = WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal("100.00"),
            alipay_account="payout@example.com",
            alipay_name="测试打手",
            fast_lane=True,
            state=WithdrawOrder.STATE_PAID,
        )

        legacy.refresh_from_db()
        self.assertTrue(legacy.fast_lane)
        self.assertEqual(legacy.state, WithdrawOrder.STATE_PAID)

    def test_the_fast_lane_quota_helpers_are_gone(self):
        """「整段下线」的结构判据：那五个额度函数**不在** `wallet` 模块里了。

        ⚠️ 这不是「按名字猜实现」：`request_withdraw` 的额度分支删掉之后，这五个函数
        没有任何调用点；留着它们就是一段还能被下一个人「顺手接回去」的死代码。
        ⛔ 恢复快路 = 重新打开一条业务方已撤销的产品口径，必须重新拍板。
        """
        import apps.accounts.wallet as wallet

        for name in (
            "fast_lane_cap",
            "fast_lane_per_order",
            "fast_lane_capacity",
            "fast_lane_used",
            "fast_lane_remaining",
        ):
            self.assertFalse(hasattr(wallet, name), name)


class WalletPayloadTests(TenantTestCase):
    """`GET /api/app/wallet/` 的兼容字段（老版本 App 不崩）。"""

    def test_the_payload_keeps_a_zero_remaining_field(self):
        client = make_logged_in_client(username="fastlane-payload-user")

        body = client.get("/api/app/wallet/").json()

        self.assertEqual(body["code"], 0, body)
        # 字段**保留**、恒 "0.00"（照它的金额格式化口径：两位小数字符串）。
        self.assertEqual(body["data"]["fast_lane_remaining"], "0.00")
        # 只在「有额度」时才有意义的那一格已经不在载荷里（唯一消费者已删）。
        self.assertNotIn("fast_lane_cap", body["data"])
