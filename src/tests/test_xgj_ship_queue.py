"""闲管家发货**节流队列**的网（D-0358；计划 `docs/plans/2026-11-01-xgj-ship-queue-throttle.md` §4）。

钉的形状：`dispatch_auto_ship` 入队（去重、schema 捕获、attempt 1 基）→
worker 逐条消费（FIFO、每条后睡 `XGJ_SHIP_MIN_INTERVAL`）→ 限流回执自动重试
（总尝试恰 3 次、耗尽清 pending、其余失败不重试）→ 单条异常不杀 worker。
判据一律打在**库行 / 队列与去重集的真实状态**上，⛔ 不打中间层桩。

## 确定性纪律（⛔ 不跟真线程竞态）

真 worker 线程只是 `while True` 的循环壳；全部判据走两条**同步直调缝**：
`_process_ship_item`（单条处理体）与 `_ship_worker_step`（worker 的一步 =
取一条 → 处理 → 睡间隔）。凡走「入队」的用例把 `_ensure_ship_worker_locked`
替成空操作——⛔ 不让懒启动在测试进程里拉起**真** worker：它会拿另一条连接
消费队列，与本用例**未提交**的订单竞态（症状是随机出现「订单不存在」失败行）。

## 桩位（E-0029：打在「被查找的那个模块」上）

- 上游出口：`apps.business.xgj_ship.logistics_dummy_send`
  （`xgj_ship` 查的是自己模块的全局名，与 `test_xgj_auto_ship` 同判据）；
- `time.sleep`：`xgj_ship` 查的是全局 `time` 模块的 `sleep` ⇒ 桩打 `time.sleep`
  （替身后用例零真实等待；间隔**参数**的判据在 `ThrottleIntervalTests`）；
- `xgj_ship.connections`：直调处理体会走到 `_auto_ship_in_thread` finally 的
  `connections.close_all()`——不替掉它会把**测试线程自己的事务连接**关掉
  （口径照 `test_xgj_auto_ship.ThreadBodyGuardTests` 的 docstring；
  ORM 用的全局连接处理器不受这个桩影响，库行照常落）。

夹具值一律合成（E-0077）：单号 / 密钥只仿形状；「请求限流，请稍后重试」是
**上游回执文案模板**（与 E-0237 用例照抄「订单已发货不支持操作」同一先例）。
"""
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Game,
    Order,
    OrderSource,
    OrderStatus,
    TaobaoOrderSendLog,
    XgjShop,
    XgjShopBinding,
)
from apps.business.xgj import XgjError
from apps.business import xgj_ship

#: 上游出口的桩位（E-0029）。
STUB_TARGET = "apps.business.xgj_ship.logistics_dummy_send"

#: 上游限流回执的**逐字文案**（生产台账所见；marker 在头部 ⇒ 可判）。
LIMIT_MSG = "请求限流，请稍后重试"

ORDER_NO = "20261101000000000001"
ORDER_NO_B = "20261101000000000002"
#: 合成单号：形状仿 19 位纯数字（⛔ 不是任何真实订单号）。
TB_NO = "22001101000000000001"
TB_NO_B = "22001101000000000002"
#: 一张不存在的系统单号（「订单不存在」分支用）。
ORDER_NO_GONE = "20261101000000000099"


class _QueueFixture(TenantTestCase):
    """与 `test_xgj_auto_ship._ShipFixture` 同一条解析链的夹具（独立一份、值全合成）。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏丙")
        self.source = OrderSource.objects.create(name="测试来源丙")
        self.binding = XgjShopBinding.objects.create(
            shop_name="测试应用丙", app_key="AK-TEST-Q", app_secret="SK-TEST-Q"
        )
        self.shop = XgjShop.objects.create(
            binding=self.binding, user_identity="ID-TEST-Q",
            shop_name="测试店铺丙", order_source=self.source,
        )
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no=TB_NO,
        )
        self.schema = connection.schema_name
        # 队列与去重集是**模块级单例**、跨用例存活 ⇒ 每条用例两头都清空
        # （防上一条失败用例的残留把下一条的「恰好一条」判据污染成假红/假绿）。
        self._drain_queue()

    def tearDown(self):
        self._drain_queue()

    @staticmethod
    def _drain_queue():
        xgj_ship._ship_queue.clear()
        xgj_ship._ship_pending.clear()

    # ── 夹具工具 ──────────────────────────────────────────────────────
    def second_order(self):
        return Order.objects.create(
            game=self.game, order_no=ORDER_NO_B, status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no=TB_NO_B,
        )

    def dispatch(self, order_no):
        """入队（worker 懒启动替成空操作）⇒ 判据只看队列/去重集，不被真 worker 竞态。"""
        with mock.patch.object(xgj_ship, "_ensure_ship_worker_locked"):
            xgj_ship.dispatch_auto_ship(order_no)

    def step(self):
        """直驱 worker 的一步：零真实等待 + `close_all` 不许关测试事务的连接。"""
        return mock.patch("time.sleep"), mock.patch.object(xgj_ship, "connections")

    def rows(self, order_no=ORDER_NO):
        return TaobaoOrderSendLog.objects.filter(order_no=order_no)


class EnqueueTests(_QueueFixture):
    """入队形状：去重、attempt 1 基、schema 入队时捕获。"""

    def test_duplicate_enqueue_keeps_exactly_one_pending_item(self):
        """同单重复入队 ⇒ 队列/pending 各仍恰一条（attempt 停在首发值 1）。"""
        self.dispatch(ORDER_NO)
        self.dispatch(ORDER_NO)
        self.assertEqual(
            list(xgj_ship._ship_queue), [(ORDER_NO, self.schema, 1)],
            "同一单只该有一条队列元素，且 attempt 1 基初值 = 1",
        )
        self.assertEqual(xgj_ship._ship_pending, {ORDER_NO})

    def test_two_orders_enqueue_as_two_items_in_order(self):
        """两张单 ⇒ 两条元素、按入队顺序排；attempt 各自从 1 起。"""
        self.dispatch(ORDER_NO)
        self.dispatch(ORDER_NO_B)
        self.assertEqual(
            list(xgj_ship._ship_queue),
            [(ORDER_NO, self.schema, 1), (ORDER_NO_B, self.schema, 1)],
        )
        self.assertEqual(xgj_ship._ship_pending, {ORDER_NO, ORDER_NO_B})


class FifoConsumptionTests(_QueueFixture):
    """worker 按入队顺序逐条消费；判据 = **库行落地的先后**（真出口）。"""

    def test_steps_consume_in_enqueue_order_and_write_rows_in_order(self):
        order_b = self.second_order()
        self.dispatch(ORDER_NO)
        self.dispatch(ORDER_NO_B)
        sleep_stub, conn_stub = self.step()
        with mock.patch(STUB_TARGET, return_value={}), sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
            xgj_ship._ship_worker_step()
        # 库行按消费顺序落地（id 单调）＝ FIFO 的可观测证据。
        self.assertEqual(
            list(TaobaoOrderSendLog.objects.order_by("id")
                 .values_list("order_no", flat=True)),
            [ORDER_NO, ORDER_NO_B],
        )
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())


class ThrottleIntervalTests(_QueueFixture):
    """节流本体：每发完一条、按 settings 值睡一次（运行期读 ⇒ override_settings 生效）。"""

    def test_step_sleeps_the_configured_interval_after_each_ship(self):
        order_b = self.second_order()
        self.dispatch(ORDER_NO)
        self.dispatch(ORDER_NO_B)
        sleep_stub, conn_stub = self.step()
        with mock.patch(STUB_TARGET, return_value={}), sleep_stub as sleep, conn_stub, \
                override_settings(XGJ_SHIP_MIN_INTERVAL=2.5):
            xgj_ship._ship_worker_step()
            xgj_ship._ship_worker_step()
        # 两笔发货 ⇒ 恰两次间隔睡眠、参数都是运行期读到的 2.5（不是默认值 1.0 ——
        # 若实现改成 import 时读死，本条立刻红）。
        self.assertEqual(sleep.call_args_list, [mock.call(2.5), mock.call(2.5)])


class LimitRetryTests(_QueueFixture):
    """限流自动重试：attempt 递增回队、总尝试恰 3、耗尽清 pending（可再入队）。"""

    def test_limit_failure_requeues_with_bumped_attempt_and_stays_pending(self):
        """第 1 次限流 ⇒ attempt+1 回队尾、pending 保持；第 2 次成功 ⇒ 成功行 + 全清。"""
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(
            STUB_TARGET,
            side_effect=[XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG), {}],
        ), sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
            self.assertEqual(
                list(xgj_ship._ship_queue), [(ORDER_NO, self.schema, 2)],
                "限流回执必须 attempt+1 回队尾（变异钉 A：去掉 marker 判据本条红）",
            )
            self.assertEqual(xgj_ship._ship_pending, {ORDER_NO})
            xgj_ship._ship_worker_step()
        # 库内两行：一失败（限流原文）一成功；重试在页面上呈「失败→成功」。
        self.assertEqual(self.rows().count(), 2)
        self.assertEqual(self.rows().filter(result=0).get().remark, LIMIT_MSG)
        self.assertEqual(self.rows().filter(result=1).count(), 1)
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())

    def test_third_attempt_success_leaves_two_failures_and_one_success(self):
        """attempt 1 基：1、2 两次限流重排，第 3 次成功 ⇒ 2 失败 + 1 成功、队列集全空。"""
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(
            STUB_TARGET,
            side_effect=[
                XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG),
                XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG),
                {},
            ],
        ), sleep_stub, conn_stub:
            for _ in range(3):
                xgj_ship._ship_worker_step()
        self.assertEqual(self.rows().count(), 3)
        self.assertEqual(self.rows().filter(result=0).count(), 2)
        self.assertEqual(self.rows().filter(result=1).count(), 1)
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())

    def test_three_limits_stop_retry_and_release_pending_for_reenqueue(self):
        """连吃 3 次限流 ⇒ 停（恰 3 行失败），**pending 清空**（评审 S6-① 的洞）：
        该单之后再次完成必须还能重新入队。"""
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(
            STUB_TARGET,
            side_effect=[
                XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG),
                XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG),
                XgjError(upstream_code=100004, upstream_msg=LIMIT_MSG),
            ],
        ), sleep_stub, conn_stub:
            for _ in range(3):
                xgj_ship._ship_worker_step()
        self.assertEqual(self.rows().count(), 3)
        self.assertEqual(self.rows().filter(result=0).count(), 3)
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(
            xgj_ship._ship_pending, set(),
            "重试耗尽后必须出 pending 集——否则该单之后永远入不了队",
        )
        # 耗尽后再完成（再入队）必须能进：attempt 重新从 1 起。
        self.dispatch(ORDER_NO)
        self.assertEqual(list(xgj_ship._ship_queue), [(ORDER_NO, self.schema, 1)])
        self.assertEqual(xgj_ship._ship_pending, {ORDER_NO})


class NonLimitFailureTests(_QueueFixture):
    """非限流失败一律不重试（变异钉 B：把判据放宽成「凡失败皆重试」时这些条全红）。"""

    def test_non_limit_xgj_error_is_not_requeued(self):
        """非限流的 XgjError ⇒ 不回队、只 1 行失败、pending 清空。"""
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(
            STUB_TARGET,
            side_effect=XgjError(upstream_code=100004, upstream_msg="店铺无此订单信息"),
        ), sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.rows().get().result, 0)

    def test_no_binding_failure_is_not_requeued(self):
        """无绑定（解析不到店）⇒ 零上游调用 + 不回队 + 只 1 行点名失败。"""
        self.order.order_source = None
        self.order.save(update_fields=["order_source"])
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(STUB_TARGET, return_value={}) as stub, sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
        stub.assert_not_called()
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())
        self.assertEqual(self.rows().get().remark, "未绑定闲管家店铺/应用")

    def test_vanished_order_is_not_requeued(self):
        """订单不存在（`auto_ship` 的点名失败行）⇒ 同样不重试、只 1 行、pending 清空。"""
        self.dispatch(ORDER_NO_GONE)
        sleep_stub, conn_stub = self.step()
        with mock.patch(STUB_TARGET, return_value={}) as stub, sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
        stub.assert_not_called()
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())
        self.assertEqual(
            self.rows(ORDER_NO_GONE).get().remark,
            "系统订单不存在或已删除，无法发货",
        )

    def test_idempotent_skip_none_is_not_requeued(self):
        """D7 幂等跳过（`auto_ship` 返回 None）⇒ 不重试、不新增行、pending 清空。"""
        # 先造一条成功行 ⇒ 再触发时 send_order_shipping 走 D7 分支返回 None。
        with mock.patch(STUB_TARGET, return_value={}):
            xgj_ship.run_auto_ship(order_no=ORDER_NO, schema_name=None)
        self.assertEqual(self.rows().filter(result=1).count(), 1)
        self.dispatch(ORDER_NO)
        sleep_stub, conn_stub = self.step()
        with mock.patch(STUB_TARGET, return_value={}) as stub, sleep_stub, conn_stub:
            xgj_ship._ship_worker_step()
        stub.assert_not_called()
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())


class WorkerResilienceTests(_QueueFixture):
    """worker 健壮性：单条未预期异常 ⇒ 该条清出集、**下一条照发**（线程不许亡）。"""

    def test_unexpected_error_on_one_item_does_not_kill_the_next(self):
        order_b = self.second_order()
        self.dispatch(ORDER_NO)
        self.dispatch(ORDER_NO_B)
        real_body = xgj_ship._auto_ship_in_thread

        def _boom(*, order_no, schema_name):
            if order_no == ORDER_NO:
                raise RuntimeError("合成队列炸弹")
            return real_body(order_no=order_no, schema_name=schema_name)

        sleep_stub, conn_stub = self.step()
        with mock.patch.object(xgj_ship, "_auto_ship_in_thread", side_effect=_boom), \
                mock.patch(STUB_TARGET, return_value={}) as stub, \
                sleep_stub, conn_stub, \
                self.assertLogs("apps.business.xgj_ship", level="ERROR") as captured:
            xgj_ship._ship_worker_step()
            xgj_ship._ship_worker_step()
        # 第一条死在半路：零留痕（能逃到处理体之外的不是业务失败）+ 完整栈进日志。
        self.assertEqual(self.rows(ORDER_NO).count(), 0)
        self.assertTrue(any("合成队列炸弹" in line for line in captured.output))
        # 第二条照发：成功行落库（「worker 存活」的可观测证据）。
        self.assertEqual(self.rows(ORDER_NO_B).count(), 1)
        self.assertEqual(self.rows(ORDER_NO_B).get().result, 1)
        stub.assert_called_once()
        # 两条都以终态出了 pending 集（异常那条也清——不许钉死在集里）。
        self.assertEqual(list(xgj_ship._ship_queue), [])
        self.assertEqual(xgj_ship._ship_pending, set())


class ShipMinIntervalConfigTests(SimpleTestCase):
    """`XGJ_SHIP_MIN_INTERVAL` 的运行期兜底：≤0 / NaN ⇒ 回落 1.0 + warning（不静默失效）。"""

    def test_invalid_values_fall_back_to_default_with_warning(self):
        for bad in (0, -3, float("nan"), "abc"):
            with self.subTest(bad=bad):
                with override_settings(XGJ_SHIP_MIN_INTERVAL=bad), \
                        self.assertLogs("apps.business.xgj_ship", level="WARNING") as captured:
                    self.assertEqual(xgj_ship._ship_min_interval(), 1.0)
                self.assertTrue(
                    any("XGJ_SHIP_MIN_INTERVAL" in line for line in captured.output),
                    f"warning 必须点名配置键：{captured.output}",
                )

    def test_valid_value_passes_through(self):
        with override_settings(XGJ_SHIP_MIN_INTERVAL=0.5):
            self.assertEqual(xgj_ship._ship_min_interval(), 0.5)

    def test_settings_module_declares_the_default(self):
        """settings 里真有这一项、默认 1.0（钉 accidental 删除 / 改名）。"""
        from django.conf import settings as dj_settings

        self.assertEqual(dj_settings.XGJ_SHIP_MIN_INTERVAL, 1.0)
