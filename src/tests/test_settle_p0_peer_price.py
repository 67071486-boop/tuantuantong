"""D-0307（2026-10-26 业务方终裁）：上家改价**只动客户价**（`Order.total_price`）。

## 口径变更（本文件的前身是结算 P0-5「上家改价 ⇒ 按新价重算打手金额」）

- **旧口径**（P0-5）：`order.update` 支把上家的价写进 `hall_price`，并按新价
  重算打手金额 —— 前提是「上家发单价 = 下家给打手的价」。
- **新口径**（D-0307）：上家发单价落到下家账上 = **接单客户价**（`total_price`，进账侧）
  ⇒ 上家改价改 `total_price`；**下家给打手的价（`hall_price`）由下家自己决定改不改**，
  上家改价**不自动动它**、打手金额**也不因此重算**（`performance.order.hall_price` 未变）。
- 因此本文件钉四件事：
  1. 改价后**客户价 = 新价**、**打手价 = 旧价**（关键判据），打手金额一个字节不动；
  2. 客服估算行**跟着客户价刷新**（`refresh_customer_estimate` 补接线，评审抓的漏网）；
  3. **同价 ⇒ 一次都不写**（客户价按列精度比较；留痕 / 金额 / `updated_at` 都不动）；
  4. 「已结账」与「提成算不出」这两档**不再产生 skip**：这一支不重算金额，也就没有
     「已结账未重算」「重算失败回滚」可谈（旧常量保留但不再从这里触发）。

⚠️ 期望值一律写字面值（E-0024）；夹具一律**合成值**（E-0077）。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
    SettleStatus,
    UpstreamPriceChangeLog,
)
from apps.business.order_status import apply_transition
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token

#: 本文件统一的"对端单号"（去重键 = `(绑定, 对端单号)`）。
PEER_NO = "P0C5-1"
#: 本地订单号（形状照 `src/apps/business/order_no.py`，值本身无意义）。
LOCAL_NO = "20261022000000000051"

#: 三个时刻：`T1 < T2 < T3`，`T1` 是本地已应用到的时刻（`link.last_applied_at`）。
T1 = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)

#: 打手提成率（订单价百分比）：100 × 50% = 50、80 × 50% = 40。
TAKER_RATE = Decimal("50.00")
#: 客服提成率（**利润**百分比）。
SALES_RATE = Decimal("40.00")
#: 台账里那句人话（现在这一支记的是客户价）。
CUSTOMER_PRICE_NOTE = "上家改价（客户价）"


class _PeerPriceBase(TenantTestCase):
    """共享夹具：一个游戏、一个客服（绑定人）、一个打手（50% 订单价）+ 一张已接手的单。"""

    def setUp(self):
        self.game = Game.objects.create(name="结算P0改价夹具游戏")
        self.kefu = get_user_model().objects.create_user(username="结算P0客服甲")
        self.taker = get_user_model().objects.create_user(username="结算P0打手甲")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        StaffProfile.objects.create(
            user=self.kefu,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="结算P0上家工作室",
            peer_base_url="https://peer-p0c5.test.invalid",
            peer_account="peer-account-p0c5",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local-p0c5.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            # 客户价 130 / 打手价 100：两者**刻意不同**，改价后「只动客户价」才看得出来。
            total_price=Decimal("130.00"),
            hall_price=Decimal("100.00"),
            source="结算P0来源快照",
            source_order_no=PEER_NO,
            sales_user=self.kefu,
            created_by=self.kefu.get_username(),
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no=PEER_NO,
            last_applied_at=T1,
        )
        self.perf = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("50.00"),
            bonus_amount=Decimal("0.00"),
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def _event(self, *, hall_price, occurred_at=T2, status=OrderStatus.IN_PROGRESS):
        """造一条 `order.update` 事件（对端每条事件都**恒带** `hall_price`，spec §2.4）。"""
        return {
            "kind": "order.update",
            "target_ref": PEER_NO,
            "occurred_at": occurred_at.isoformat(),
            "payload": {
                "status": str(status),
                "hall_price": hall_price,
                "total_price": "999.00",   # 载荷里这个键**仍被忽略**（D-0307 之前就如此）
                "game_name": self.game.name,
                "remark": "改价备注",
                "project_text": "改价项目名",
                "customer_phone": "测试电话-P0C5",
                "customer_qq": "测试QQ-P0C5",
                "content": "改价需求",
            },
        }

    def _complete(self):
        """把单**真实地**走到已完成（待验收 → 已完成，两条边各用它的节点门）。"""
        apply_transition(
            self.order, OrderStatus.PENDING_REVIEW,
            nodes={"order.submit_work"}, actor=self.kefu.get_username(),
        )
        apply_transition(
            self.order, OrderStatus.COMPLETED,
            nodes={"order.change_status_completed"}, actor=self.kefu.get_username(),
        )


class PriceChangeMovesTheCustomerPriceOnlyTests(_PeerPriceBase):
    """判据 1：改价后**客户价 = 新价、打手价 = 旧价**，打手金额不重算。"""

    def test_a_price_change_moves_the_customer_price_and_leaves_the_taker_price_alone(self):
        result = apply_events([self._event(hall_price="80")], binding=self.binding)
        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)

        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        # ⭐ 关键判据：客户价改了（列精度两位小数）、打手价**一格不动**。
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        self.assertEqual(self.order.hall_price, Decimal("100.00"))
        # 打手金额：上家的价不是打手的价 ⇒ 不重算（仍是 100 × 50% = 50.00）。
        self.assertEqual(self.perf.temp_amount, Decimal("50.00"))
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        # 台账：**一行**、记客户价旧→新（⛔ 不是打手价）。
        rows = list(UpstreamPriceChangeLog.objects.order_by("id"))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].before_amount, Decimal("130.00"))
        self.assertEqual(rows[0].after_amount, Decimal("80.00"))
        self.assertEqual(rows[0].note, CUSTOMER_PRICE_NOTE)

        # 完单之后：`bonus := temp`（既有口径）⇒ 仍是旧的 50.00，证明全程没按新价重算。
        self._complete()
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("50.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("50.00"))

    def test_the_customer_estimate_follows_the_new_customer_price(self):
        """客户价的改价 ⇒ `refresh_customer_estimate` 必须跟上（D-0307 补接线）。

        客服预计 =（客户价 80 − 打手预计 50）× 40% = **12.00**。
        ⚠️ 旧口径下客户价不变（130）、打手重算成 40 ⇒（130 − 40）× 40% = 36.00 ——
        两个数不同，正好能分辨「这一支到底动的是哪一列」。
        """
        Performance.objects.create(
            order=self.order,
            user=self.kefu,
            emp_type=EmpType.SALES,
            temp_amount=Decimal("0.00"),
            bonus_amount=Decimal("0.00"),
        )
        result = apply_events([self._event(hall_price="80")], binding=self.binding)
        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        customer = Performance.objects.get(order=self.order, emp_type=EmpType.SALES)
        self.assertEqual(customer.temp_amount, Decimal("12.00"))


class SameCustomerPriceNoWriteTests(_PeerPriceBase):
    """判据 3：同价的 `order.update` ⇒ 金额列 / 台账 / `updated_at` 都不动。"""

    def test_the_same_price_in_three_representations_writes_nothing(self):
        """载荷是 str/数字、库里是 `Decimal` ⇒ 比较必须先折到列精度（⛔ 不是 `!=`）。

        三种表示都等于库里的客户价 `Decimal("100.00")`：`"100.00"`、`"100"`、`100`。
        """
        Order.all_objects.filter(pk=self.order.pk).update(total_price=Decimal("100.00"))
        perf_before = Performance.objects.get(pk=self.perf.pk)
        order_before = Order.objects.get(pk=self.order.pk)

        result = apply_events(
            [
                self._event(hall_price="100.00", occurred_at=T2),
                self._event(hall_price="100", occurred_at=T2 + timedelta(minutes=1)),
                self._event(hall_price=100, occurred_at=T2 + timedelta(minutes=2)),
            ],
            binding=self.binding,
        )
        self.assertEqual(result.applied, 3, result)

        perf_after = Performance.objects.get(pk=self.perf.pk)
        order_after = Order.objects.get(pk=self.order.pk)
        self.assertEqual(perf_after.temp_amount, Decimal("50.00"))
        self.assertEqual(perf_after.bonus_amount, Decimal("0.00"))
        self.assertEqual(perf_after.updated_at, perf_before.updated_at)
        self.assertEqual(order_after.total_price, Decimal("100.00"))
        self.assertEqual(order_after.hall_price, Decimal("100.00"))
        self.assertEqual(order_after.updated_at, order_before.updated_at)
        # 同价 ⇒ 不写改价留痕。
        self.assertEqual(UpstreamPriceChangeLog.objects.count(), 0)


class SettledMainRowTests(_PeerPriceBase):
    """已结账的 MAIN 行：客户价照写、金额一个字节不动，且**不再产生 skip**。"""

    def setUp(self):
        super().setUp()
        self.perf.settle_status = SettleStatus.SETTLED
        self.perf.settled_at = T1
        self.perf.bonus_amount = Decimal("50.00")  # 钱已按**旧打手价**（100 × 50%）发过
        self.perf.save()

    def test_a_settled_main_row_gets_the_customer_price_and_no_skip(self):
        perf_before = Performance.objects.get(pk=self.perf.pk)
        result = apply_events([self._event(hall_price="80")], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        self.assertEqual(self.order.hall_price, Decimal("100.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("50.00"))
        self.assertEqual(self.perf.bonus_amount, Decimal("50.00"))
        self.assertEqual(self.perf.updated_at, perf_before.updated_at)
        # ⭐ D-0307：这一支**不重算打手金额** ⇒ 没有「已结账未重算」这一档可报。
        self.assertEqual(result.skipped_details, [], "这一支不再重算金额，不该有 skip")


class NoTakerRowTests(_PeerPriceBase):
    """没有可重算的 MAIN 行（不存在 / user 为空）⇒ 客户价照写、金额列不动。"""

    def test_an_order_without_a_main_row_still_updates_the_customer_price(self):
        Performance.objects.filter(pk=self.perf.pk).delete()
        result = apply_events([self._event(hall_price="80")], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        self.assertEqual(self.order.hall_price, Decimal("100.00"))
        self.assertEqual(Performance.objects.filter(order=self.order).count(), 0)

    def test_an_unassigned_main_row_keeps_its_estimate(self):
        """MAIN 行在、`user` 为空（池内未指派）⇒ 那一列是**录单估值**，⛔ 不按人重算。"""
        self.perf.user = None
        self.perf.temp_amount = Decimal("100.00")
        self.perf.save()

        result = apply_events([self._event(hall_price="80")], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("100.00"))


class UncomputableCommissionDoesNotBlockThePriceTests(_PeerPriceBase):
    """提成配置坏（固定金额档）⇒ 改价**照常生效**：这一支已经不重算金额了。"""

    def _make_commission_uncomputable(self):
        StaffProfile.objects.filter(user=self.taker).update(
            commission_mode=CommissionMode.FIXED,
            commission_value=Decimal("10.00"),
        )

    def test_a_broken_commission_does_not_block_the_customer_price_change(self):
        self._make_commission_uncomputable()
        result = apply_events([self._event(hall_price="80")], binding=self.binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("80.00"))
        self.assertEqual(self.order.hall_price, Decimal("100.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("50.00"))
        # 台账与水位都正常落下（没有「重算失败 ⇒ 整笔回滚」这一档）。
        self.assertEqual(UpstreamPriceChangeLog.objects.count(), 1)
        self.assertEqual(self.link.last_applied_at, T2)
