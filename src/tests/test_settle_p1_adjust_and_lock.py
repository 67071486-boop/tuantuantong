"""P1-10（结账 / 改金额的行锁）+ P1-6 **复核的 witness**。计划 §7 与 §10。

## P1-6 复核：**评审结论是误报，本批不改代码**

评审说「`/adjust-amount/` 的 `bonus` 档直写 `perf.bonus_amount`、写完不刷客服行」。
实测**不成立**：那一档先走 `performance.apply_amount_to_effective_base` →
`recompute_order_amounts`，后者在**完成单**上把 `bonus_amount` 一起写成新金额、**save 之后**
才调 `refresh_customer_estimate` ⇒ 客服行读到的是**新利润**。
本文件的 `BonusBranchKeepsTheCustomerRowInSyncTests` 就是那条链的读数
（`bonus=80` ⇒ 打手 `80.00`、客服 `(130 − 80) × 40% = 20.00`）——它在**本批改任何代码之前
就是绿的**，所以 ⛔ **它不是 P1-6 的修复判据**，只是保留为那条「间接覆盖」链的回归网
（谁把主挂点里那次 `refresh_customer_estimate` 挪走，它会红）。

## P1-10

`/adjust-amount/` 先判 `is_settled` 再在事务里写，两段之间**不持锁**；`settle_performances`
的 `rows` 也不锁。⇒ 「判定与写入之间插一次结账」会让改金额**改到已结账的行上**。

本文件的判据是**确定性地**构造那个窗口（不依赖线程调度）：
- 改金额侧：桩打在 `PerformanceAdjustAmountSerializer.is_valid` 上（它排在
  `get_object()` **之后**、视图的 `transaction.atomic()` **之前**）⇒ 第一次校验时先把这一行
  结掉，再放行校验 ⇒ 加锁复查必须看见它已结账并回 `1014`，⛔ 不许改已结账行；
- 结账侧：① 抓 SQL 钉住「绩效行加锁 + `ORDER BY id`」；② 「状态未结账却留着结账痕迹」
  （`is_settled` 的三字段并集）的脏行必须让**整批** `1006`（⛔ 不是静默改写它的 `settled_amount`）。

⚠️ 期望值写字面十进制（E-0024）；夹具用合成值（E-0077）。
⚠️ 桩必须**自证接上**（E-0029）：`called` 为空即本用例无效，断言会红。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business import order_writes
from apps.business.api import performance_views
from apps.business.api.serializers import (
    AssignSerializer,
    BatchRecomputeSerializer,
    HallPriceChangeSerializer,
    PerformanceAdjustAmountSerializer,
    PerformanceAdjustSerializer,
    SalesAgentChangeSerializer,
)
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.settlement import SettleError, settle_performances
from tests.p5_auth_helpers import make_logged_in_client

#: 夹具数字（字面值，⛔ 不从被测模块 import）：
#: 净实收 130、打手 20% 订单价、客服 40% 利润百分比、发单价 88 ⇒ 打手预计 17.60、客服 44.96。
TAKER_RATE = Decimal("20.00")
SALES_RATE = Decimal("40.00")
ORDER_TOTAL = Decimal("130.00")
ORDER_HALL = Decimal("88.00")
TAKER_BEFORE = Decimal("17.60")
CUSTOMER_BEFORE = Decimal("44.96")
#: `bonus=80` 之后客服按新利润该拿的数：(130 − 80) × 40%。
CUSTOMER_AFTER_BONUS_80 = Decimal("20.00")

#: 本文件走到的六个钱面端点的节点门（与各端点既有判据网同一口径）：
#: `/adjust-amount/` 与 `/adjust/`、`/delete/` → `order.submit_payout_price`；
#: `/hall-price/` → `order.set_offer_price`；`/assign/` → `order.assign_taker`；
#: `/sales-agent/` → `order.allow_change_sales_agent`；
#: `batch/recompute-commission` → `finance.change_settlement_status`。
ADJUST_NODES = (
    "order.submit_payout_price",
    "order.list",
    "order.set_offer_price",
    "order.assign_taker",
    "order.allow_change_sales_agent",
    "finance.change_settlement_status",
)
ADJUST_URL = "/api/business/performances/{pk}/adjust-amount/"
#: （Z1）新增的五个既有端点（同一张夹具、同一套「锁内网」判据）。
ADJUST_FIELDS_URL = "/api/business/performances/{pk}/adjust/"
DELETE_URL = "/api/business/performances/{pk}/delete/"
HALL_PRICE_URL = "/api/business/orders/{order_no}/hall-price/"
ASSIGN_URL = "/api/business/orders/{order_no}/assign/"
SALES_AGENT_URL = "/api/business/orders/{order_no}/sales-agent/"
BATCH_RECOMPUTE_URL = "/api/business/orders/batch/recompute-commission/"


class _AdjustAndSettleFixture(TenantTestCase):
    """一条**已完成**的单 + 打手行（MAIN）+ 客服行（SALES）。"""

    def setUp(self):
        self.game = Game.objects.create(name="P1-6 调整与锁游戏")
        self.taker = User.objects.create_user(username="p1-adjust-taker")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=TAKER_RATE,
        )
        self.sales = User.objects.create_user(username="p1-adjust-sales")
        StaffProfile.objects.create(
            user=self.sales,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=SALES_RATE,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261022000000001001",
            status=OrderStatus.COMPLETED,
            total_price=ORDER_TOTAL,
            hall_price=ORDER_HALL,
            sales_user=self.sales,
        )
        self.main = Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=TAKER_BEFORE,
            bonus_amount=TAKER_BEFORE,
            review_status=ReviewStatus.APPROVED,
        )
        self.customer = Performance.objects.create(
            order=self.order,
            user=self.sales,
            emp_type=EmpType.SALES,
            temp_amount=CUSTOMER_BEFORE,
            bonus_amount=CUSTOMER_BEFORE,
            commission_rate=SALES_RATE,
        )
        self.client = make_logged_in_client(
            username="p1-adjust-clerk", groups=("客服丙",), nodes=ADJUST_NODES
        )
        #: `settle_performances` 的 `user=`（数据权限那一维）。
        self.clerk = User.objects.get(username="p1-adjust-clerk")
        self._seq = 0

    def _post(self, payload):
        response = self.client.post(
            ADJUST_URL.format(pk=self.main.pk),
            payload,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def _order_row(self):
        return Order.objects.get(pk=self.order.pk)

    def _main_row(self):
        return Performance.objects.get(pk=self.main.pk)


class BonusBranchKeepsTheCustomerRowInSyncTests(_AdjustAndSettleFixture):
    """P1-6 **复核 witness**（⛔ 不是本批的修复判据：**改代码前它就是绿的**）。

    完成单 `adjust-amount bonus=80` ⇒ 打手 `80.00`、客服按**新利润**重算成
    `(130 − 80) × 40% = 20.00`（不是按旧利润算的 `44.96`）。

    ⚠️ 它今天靠的是**间接覆盖**：`apply_amount_to_effective_base` → `recompute_order_amounts`
    在完成单上先 `save()` 新 `bonus_amount`、再调 `refresh_customer_estimate`
    （主挂点）⇒ 客服行读到的是新值。所以本用例不能用来证明"补刷了一句 refresh"
    （没有那句话也是绿的），它守的是上面这条链别被人拆掉。
    """

    def test_the_bonus_branch_recomputes_the_customer_row(self):
        body = self._post({"amount_kind": "bonus", "amount": "80.00"})

        self.assertEqual(body["code"], 0, body)
        main = self._main_row()
        customer = Performance.objects.get(pk=self.customer.pk)
        # 打手：生效绩效 = 用户填的 80.00（反推出的基价 400.00 也让预计跟着到 80.00）。
        self.assertEqual(main.bonus_amount, Decimal("80.00"))
        self.assertEqual(main.temp_amount, Decimal("80.00"))
        # 客服：按**新**利润算 ⇒ (130 − 80) × 40% = 20.00。
        self.assertEqual(customer.bonus_amount, CUSTOMER_AFTER_BONUS_80)
        self.assertEqual(customer.temp_amount, CUSTOMER_AFTER_BONUS_80)
        # 判别力自证：旧值 44.96 必须**没有**留下（它按旧利润 17.60 算的）。
        self.assertNotEqual(customer.bonus_amount, CUSTOMER_BEFORE)


class AdjustAmountRowLockTests(_AdjustAndSettleFixture):
    """P1-10：判定之后、写入之前插一次结账 ⇒ 改金额必须 `1014`，且一行不写。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_adjust_fail(self):
        called = []
        real_is_valid = PerformanceAdjustAmountSerializer.is_valid

        def interleaving_is_valid(serializer, *args, **kwargs):
            if not called:
                # 「判定之后、加锁之前」财务把这一行结掉（同一个连接、同一个测试事务里）。
                Performance.objects.filter(pk=self.main.pk).update(
                    settle_status=SettleStatus.SETTLED,
                    settled_at=timezone.now(),
                    settled_by="财务甲",
                    settled_amount=TAKER_BEFORE,
                )
            called.append(True)
            return real_is_valid(serializer, *args, **kwargs)

        with mock.patch.object(
            PerformanceAdjustAmountSerializer, "is_valid", interleaving_is_valid
        ):
            body = self._post({"amount_kind": "temp", "amount": "30.00"})

        # 桩自证接上（E-0029）：没接上时下面那条是不是 1014 都说明不了问题。
        self.assertTrue(called, "is_valid 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)

        main = self._main_row()
        order = self._order_row()
        # 已结账行 **一个字节都没改**：金额两列与基价都停在原值。
        self.assertEqual(main.temp_amount, TAKER_BEFORE)
        self.assertEqual(main.bonus_amount, TAKER_BEFORE)
        self.assertIsNone(main.taker_price)
        self.assertEqual(order.hall_price, ORDER_HALL)
        # 那笔「插进来的结账」仍然在（失败的是改金额，不是回滚别人已提交的钱）。
        self.assertEqual(main.settle_status, SettleStatus.SETTLED)


class SettleRowLockTests(_AdjustAndSettleFixture):
    """P1-10（结账侧）：绩效行加锁（`FOR UPDATE OF self` + `ORDER BY id`）+ 复查 `is_settled`。"""

    def _perf(self, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        # 每条绩效另起一单（`uniq_performance_order_user` 保证「订单 × 人」唯一）。
        self._seq += 1
        order = Order.objects.create(
            game=self.game,
            order_no=f"202610220000001{self._seq:05d}",
            status=OrderStatus.COMPLETED,
            total_price=ORDER_TOTAL,
            hall_price=ORDER_HALL,
        )
        return Performance.objects.create(order=order, user=user, **fields)

    def test_the_rows_are_locked_in_id_order(self):
        first = self._perf(self.taker)
        second = self._perf(self.sales)

        with CaptureQueriesContext(connection) as captured:
            settle_performances(
                [second.id, first.id], actor="财务甲", user=self.clerk
            )

        table = Performance._meta.db_table
        locking = [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
        ]
        self.assertTrue(locking, [q["sql"] for q in captured.captured_queries])
        # ① 锁的只是绩效行自己（`of=("self",)`）——不是整条 join 上的订单 / 账号表。
        self.assertTrue(
            any(f'FOR UPDATE OF "{table}"' in sql for sql in locking), locking
        )
        # ② 同批多行的取锁顺序确定（按 id 升序），否则两笔批量结账交叉会死锁。
        self.assertTrue(
            any(
                f'FOR UPDATE OF "{table}"' in sql
                and f'ORDER BY "{table}"."id" ASC' in sql
                for sql in locking
            ),
            locking,
        )

    def test_a_row_with_settlement_traces_blocks_the_whole_batch(self):
        good = self._perf(self.taker)
        # 脏行：状态列还是「未结账」，却已经留着结账痕迹（`is_settled` 的三字段并集判它已结账）。
        dirty = self._perf(
            self.sales,
            settled_at=timezone.now(),
            settled_amount=Decimal("5.00"),
        )
        good.refresh_from_db()
        dirty.refresh_from_db()
        good_at, dirty_at = good.updated_at, dirty.updated_at

        with self.assertRaises(SettleError) as raised:
            settle_performances([good.id, dirty.id], actor="财务甲", user=self.clerk)

        # 整批 `1006`（⛔ 不是「跳过那一条、把另一条结掉」，也⛔ 不是静默改写 `settled_amount`）。
        self.assertEqual(raised.exception.code, 1006)
        self.assertIn(str(dirty.id), raised.exception.message)
        good.refresh_from_db()
        dirty.refresh_from_db()
        self.assertEqual(good.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(good.settled_amount)
        self.assertEqual(dirty.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(dirty.settled_amount, Decimal("5.00"))
        # 一行都没动（`updated_at` 是 `auto_now`，任何一次 `save()` 都会推走它）。
        self.assertEqual(good.updated_at, good_at)
        self.assertEqual(dirty.updated_at, dirty_at)


# ── （Z1）：六处「锁 + 复查」的锁内网 ─────────────────────
#: 真 `atomic` / `set_rollback` 的引用：下面那个桩要转发给它们（桩只负责「在进事务之前
#: 把一行结掉」这一件事）。
_REAL_ATOMIC = transaction.atomic
_REAL_SET_ROLLBACK = transaction.set_rollback


class _SettleBeforeAtomic:
    """在目标模块**进入 `transaction.atomic()` 之前**把一条绩效行结掉（确定性的 TOCTOU 窗口）。

    为什么需要它（而不是复用 `/adjust-amount/` 的 serializer 桩）：本批六处里，`/hall-price/`、
    `/assign/`、`/delete/`、`batch/recompute-commission` 的「已结账」预判**都在写入函数内部**，
    而它们的序列化器排在预判**之前** ⇒ 在 serializer 上开窗口只会命中锁外那道快速预判，
    **锁内复查被删掉也照样绿**（无判别力）。把桩打在 `transaction.atomic()` 的入口上，
    结账动作就落在「预判之后、加锁读之前」——正是要构造的那个窗口。

    ⚠️ 结账那句跑在**外层事务**里（`atomic()` 的上下文管理器一被求值就执行）⇒ 本函数被拒时
    的 `set_rollback(True)` 只回滚它自己那个 savepoint，**不会把这次「并发结账」也回滚掉**
    （与被测函数的语义一致：结账是另一笔已经提交的钱）。
    ⚠️ 桩必须**自证接上**（E-0029）：`called` 为空即本用例无效。
    """

    def __init__(self, *, perf_id):
        self.perf_id = perf_id
        self.called = False

    def atomic(self, *args, **kwargs):
        if not self.called:
            self.called = True
            Performance.objects.filter(pk=self.perf_id).update(
                settle_status=SettleStatus.SETTLED,
                settled_at=timezone.now(),
                settled_by="财务甲",
                settled_amount=Decimal("1.00"),
            )
        return _REAL_ATOMIC(*args, **kwargs)

    def set_rollback(self, *args, **kwargs):
        return _REAL_SET_ROLLBACK(*args, **kwargs)


class _Z1LockBase(_AdjustAndSettleFixture):
    """六条用例共用的判据助手：**抓 SQL 钉「绩效行加锁」**（与 `/adjust-amount/` 同一条腿）。"""

    def assert_perf_row_is_locked(self, captured):
        table = Performance._meta.db_table
        locking = [
            query["sql"]
            for query in captured.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
        ]
        self.assertTrue(locking, [q["sql"] for q in captured.captured_queries])
        # 锁的是**绩效行自己**（`of=("self",)`）——不是整条 join 上的订单 / 账号表。
        self.assertTrue(
            any(f'FOR UPDATE OF "{table}"' in sql for sql in locking), locking
        )


class AdjustFieldsRowLockTests(_Z1LockBase):
    """Z1 ①：`/adjust/`（接单价覆盖 / 扣点）——判定之后、写入之前插一次结账 ⇒ `1014`。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_adjust_fail(self):
        called = []
        real_is_valid = PerformanceAdjustSerializer.is_valid

        def interleaving_is_valid(serializer, *args, **kwargs):
            if not called:
                # `get_object()` 已经读过这一行、`is_valid` 排在视图的预判与事务**之前** ⇒
                # 此刻结账，锁外预判看到的仍是内存里的旧状态，窗口正好落在加锁读之前。
                Performance.objects.filter(pk=self.main.pk).update(
                    settle_status=SettleStatus.SETTLED,
                    settled_at=timezone.now(),
                    settled_by="财务甲",
                    settled_amount=TAKER_BEFORE,
                )
            called.append(True)
            return real_is_valid(serializer, *args, **kwargs)

        with mock.patch.object(
            PerformanceAdjustSerializer, "is_valid", interleaving_is_valid
        ):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    ADJUST_FIELDS_URL.format(pk=self.main.pk),
                    {"taker_price": "30.00"},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(called, "is_valid 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)
        main, order = self._main_row(), self._order_row()
        # 已结账行一个字节都没改。
        self.assertIsNone(main.taker_price)
        self.assertEqual(main.temp_amount, TAKER_BEFORE)
        self.assertEqual(main.bonus_amount, TAKER_BEFORE)
        self.assertEqual(order.hall_price, ORDER_HALL)
        # 那句「插进来的结账」仍在（失败的是改金额，不是回滚别人已提交的钱）。
        self.assertEqual(main.settle_status, SettleStatus.SETTLED)
        self.assert_perf_row_is_locked(captured)


class DeleteRowLockTests(_Z1LockBase):
    """Z1 ④：`/delete/`（绩效垃圾桶软删）——锁内复查已结账 ⇒ `1014`、行不删、零日志。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_delete_fail(self):
        hook = _SettleBeforeAtomic(perf_id=self.main.pk)
        with mock.patch.object(performance_views, "transaction", hook):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    DELETE_URL.format(pk=self.main.pk), {},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(hook.called, "atomic 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)
        main = self._main_row()
        self.assertIsNone(main.deleted_at)
        self.assertEqual(main.deleted_by, "")
        self.assertEqual(main.settle_status, SettleStatus.SETTLED)
        self.assertFalse(
            OperationLog.objects.filter(action="performance.delete").exists()
        )
        self.assert_perf_row_is_locked(captured)


class HallPriceRowLockTests(_Z1LockBase):
    """Z1 ②：`/hall-price/`——锁内复查已结账 ⇒ `1014`、发单价不动、零日志。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_price_change_fail(self):
        hook = _SettleBeforeAtomic(perf_id=self.main.pk)
        with mock.patch.object(order_writes, "transaction", hook):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    HALL_PRICE_URL.format(order_no=self.order.order_no),
                    {"price": "99.00"},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(hook.called, "atomic 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)
        order = self._order_row()
        self.assertEqual(order.hall_price, ORDER_HALL)
        self.assertEqual(
            self._main_row().settle_status, SettleStatus.SETTLED
        )
        self.assertFalse(
            OperationLog.objects.filter(
                action="order.change_offer_price", order_no=self.order.order_no
            ).exists()
        )
        self.assert_perf_row_is_locked(captured)


class AssignRowLockTests(_Z1LockBase):
    """Z1 ③：`/assign/`——锁内复查已结账 ⇒ `1014`、归属与状态不动、零日志。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_assign_fail(self):
        # 目标打手要过 D-0252 的游戏范围闸（无档案 / 空集 ⇒ 1002，会走不到加锁那一步）。
        self.taker.staff_profile.games.add(self.game)
        order = Order.objects.create(
            game=self.game,
            order_no="20261025000000009001",
            status=OrderStatus.IN_PROGRESS,
            total_price=ORDER_TOTAL,
            hall_price=ORDER_HALL,
        )
        perf = Performance.objects.create(
            order=order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=TAKER_BEFORE,
            bonus_amount=TAKER_BEFORE,
            review_status=ReviewStatus.APPROVED,
        )
        hook = _SettleBeforeAtomic(perf_id=perf.pk)
        with mock.patch.object(order_writes, "transaction", hook):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    ASSIGN_URL.format(order_no=order.order_no),
                    {"user_id": self.taker.id},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(hook.called, "atomic 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)
        perf.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(perf.user_id, self.taker.id)
        self.assertEqual(perf.bonus_amount, TAKER_BEFORE)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(perf.settle_status, SettleStatus.SETTLED)
        self.assertFalse(
            OperationLog.objects.filter(
                action="performance.assign", order_no=order.order_no
            ).exists()
        )
        self.assert_perf_row_is_locked(captured)


class SalesAgentRowLockTests(_Z1LockBase):
    """Z1 ⑤：`/sales-agent/`——SALES 行锁内复查已结账 ⇒ `1014`、归属 / 行都不动。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_change_fail(self):
        called = []
        real_is_valid = SalesAgentChangeSerializer.is_valid

        def interleaving_is_valid(serializer, *args, **kwargs):
            if not called:
                # 本视图的「已结账」判据**就是**锁内那次取行 ⇒ 窗口只需要落在取行之前。
                Performance.objects.filter(pk=self.customer.pk).update(
                    settle_status=SettleStatus.SETTLED,
                    settled_at=timezone.now(),
                    settled_by="财务甲",
                    settled_amount=CUSTOMER_BEFORE,
                )
            called.append(True)
            return real_is_valid(serializer, *args, **kwargs)

        with mock.patch.object(
            SalesAgentChangeSerializer, "is_valid", interleaving_is_valid
        ):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    SALES_AGENT_URL.format(order_no=self.order.order_no),
                    {"user": self.taker.id},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(called, "is_valid 的桩没有被调用 —— 本用例无效")
        self.assertEqual(body["code"], 1014, body)
        order = self._order_row()
        customer = Performance.objects.get(pk=self.customer.pk)
        self.assertEqual(order.sales_user_id, self.sales.pk)
        self.assertEqual(customer.user_id, self.sales.pk)
        self.assertEqual(customer.bonus_amount, CUSTOMER_BEFORE)
        self.assertEqual(customer.settle_status, SettleStatus.SETTLED)
        self.assertFalse(
            OperationLog.objects.filter(
                action="order.change_sales_agent", order_no=self.order.order_no
            ).exists()
        )
        self.assert_perf_row_is_locked(captured)


class BatchRecomputeRowLockTests(_Z1LockBase):
    """Z1 ⑥：`batch/recompute-commission`——锁内复查已结账 ⇒ 整批 `1002`、金额不动。"""

    def test_a_settle_between_the_check_and_the_write_makes_the_recompute_fail(self):
        hook = _SettleBeforeAtomic(perf_id=self.customer.pk)
        with mock.patch.object(order_writes, "transaction", hook):
            with CaptureQueriesContext(connection) as captured:
                response = self.client.post(
                    BATCH_RECOMPUTE_URL,
                    {"order_nos": [self.order.order_no], "dry_run": False},
                    content_type="application/json",
                )

        body = response.json()
        self.assertTrue(hook.called, "atomic 的桩没有被调用 —— 本用例无效")
        # `run_batch` 把逐笔失败折成**整批 1002**（`msg` 里带逐笔那句）。
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("已结账", body["msg"])
        self.assertEqual(body["data"]["applied"], [])
        customer = Performance.objects.get(pk=self.customer.pk)
        self.assertEqual(customer.bonus_amount, CUSTOMER_BEFORE)
        self.assertEqual(customer.temp_amount, CUSTOMER_BEFORE)
        self.assert_perf_row_is_locked(captured)
