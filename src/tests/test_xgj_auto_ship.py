"""闲管家「无需物流发货」的后端网（spec `2026-10-03-xgj-auto-ship-design.md` §4-1 ~ §4-5）。

覆盖的验收判据（第 6 条是前端页，不在本文件）：

1. 订单迁到「已完成」⇒ **恰好一条**发货记录 + **恰好一条**订单日志；
2. 上游失败（桩）⇒ 订单**仍然是已完成**，记录 `result=0` 且 `remark` 是**上游原文**；
3. 无绑定 / 来源单号为空 ⇒ **不调上游**（桩断言零调用）、记失败、原因点名；
4. 「重试发货」对已成功的单再发一次 ⇒ 符合 §3.5 幂等与 D7（不新增记录行、只补日志）；
5. 列表 4 个筛选逐个能筛（日期边界 / result=-1/0/1 / key 命中系统单号与淘宝单号两种）。

## 桩打在哪（E-0029 / E-0087 的纪律）

`send_order_shipping` 是在 `apps/business/xgj_ship.py` 里**查模块全局名** `logistics_dummy_send`
的 ⇒ 桩打在 `apps.business.xgj_ship.logistics_dummy_send`（⛔ 不是定义它的 `apps.business.xgj`）。
「桩接上了」本身有判据：`test_the_module_global_is_the_looked_up_name` 断言
`xgj_ship.logistics_dummy_send is xgj.logistics_dummy_send`，其余用例再断言
`called_once_with(...)` / `assert_not_called()`。
「**失败信封必须被判失败**」按 E-0087 补一条：桩抛 `XgjError`（= 上游 `code != 0` 那条路）
必须记 `result=0`、⛔ 不能算成功。

## 夹具值一律合成（E-0077）

订单号 / 单号 / 店铺名 / 密钥都是**合成值**（只仿形状），⛔ 没有抄 spec 或对标里的示例值。
"""
from datetime import datetime
import json
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business import settlement_hooks, xgj, xgj_ship
from apps.business.commission import CommissionError
from apps.business.models import (
    Game,
    Order,
    OrderSource,
    OrderStatus,
    TaobaoOrderSendLog,
    XgjShop,
    XgjShopBinding,
)
from apps.business.order_status import apply_transition, _TARGET_HOOKS
from apps.business.xgj import XgjError
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 权限节点 = 对标 1227（淘宝发货记录页），两个动作共用（spec §3.7）。
NODE = "order.taobao_ship_records"

ORDER_NO = "20261003000000000001"
#: 合成单号：形状仿官方的 19 位纯数字（⛔ 不是任何真实订单号）。
TB_NO = "22000000000000000001"
TB_NO_OTHER = "22000000000000000002"

ACTION = "order.ship_result"
STUB_TARGET = "apps.business.xgj_ship.logistics_dummy_send"


class _ShipFixture(TenantTestCase):
    """一条「有来源 + 有店 + 有完整凭据」的夹具：解析链每一环都在（spec §3.3）。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.source = OrderSource.objects.create(name="测试来源甲")
        self.binding = XgjShopBinding.objects.create(
            shop_name="测试应用甲", app_key="AK-TEST-1", app_secret="SK-TEST-1"
        )
        self.shop = XgjShop.objects.create(
            binding=self.binding, user_identity="ID-TEST-1",
            shop_name="测试店铺甲", order_source=self.source,
        )
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no=TB_NO,
        )
        # 队列与去重集是 `xgj_ship` 的**模块级单例**、跨用例存活 ⇒ 每条用例两头清空
        # （同进程里前一条用例失败残留的元素会污染下一条的「恰好一条」判据）。
        xgj_ship._ship_queue.clear()
        xgj_ship._ship_pending.clear()

    def tearDown(self):
        xgj_ship._ship_queue.clear()
        xgj_ship._ship_pending.clear()

    # ── 夹具工具 ──────────────────────────────────────────────────────
    def inline_dispatch(self):
        """把「提交后起 daemon 线程」替成**线程体同步直调**的上下文。

        ⚠️ PROD-02）起 on_commit 回调只**起线程**（`xgj_ship.dispatch_auto_ship`），
        而线程里那条**新连接**看不见本用例未提交的订单 ⇒ 直接驱动真线程会让每条用例
        都走到「订单不存在」的失败留痕。所以留痕类用例在这里把「起线程」替成
        `run_auto_ship(..., schema_name=None)`（不动 schema——测试线程的已由
        `TenantTestCase` 设好）。⛔ 不替成 `_auto_ship_in_thread`：它 finally 里的
        `connections.close_all()` 会把**测试自己的事务连接**关掉。
        断言「只入队、请求不等发货」的用例在 `AsyncDispatchTests`（队列形状），
        队列自身的网在 `tests/test_xgj_ship_queue.py`。
        """
        return mock.patch.object(
            xgj_ship,
            "dispatch_auto_ship",
            side_effect=lambda order_no: xgj_ship.run_auto_ship(
                order_no=order_no, schema_name=None
            ),
        )

    def complete(self):
        """把订单迁到「已完成」并**执行**排下的 on_commit 回调（spec §3.4 的那个出口）。

        ⚠️ `captureOnCommitCallbacks(execute=True)`：`TenantTestCase` 把用例包在一层事务里
        ⇒ 不套它的话回调**根本不会跑**（先例见 `tests/test_jidanpu_immediate_sync.py`）。
        """
        with self.inline_dispatch(), self.captureOnCommitCallbacks(execute=True):
            apply_transition(
                self.order, OrderStatus.COMPLETED,
                nodes={"order.change_status_completed"},
            )
        self.order.refresh_from_db()
        return self.order

    def rows(self):
        return TaobaoOrderSendLog.objects.filter(order_no=ORDER_NO)

    def logs(self):
        return OperationLog.objects.filter(order_no=ORDER_NO, action=ACTION)

    def logged_in(self, *, username="ship-1", nodes=(NODE,), first_name=""):
        """建一个已登录的 client；`first_name` 非空时补成显示名（夹具不支持直接传它）。

        ⚠️ **必须同时给 `groups`**：`_grant_nodes` 把节点挂在「该用户**当前所在 Group**
        对应的 Role」上（`tests/p5_auth_helpers.py`）⇒ 只传 `nodes` 而一个组都没有时，
        节点**一个都挂不上**，症状是 403（看起来像判据写错）。
        ⚠️ 显示名必须**写在用户行上**再登录：`user_display_name` 读的是 `User.first_name`
        （`apps/accounts/display.py` 的唯一口径）。
        """
        client = make_logged_in_client(username=username, groups=("客服",), nodes=nodes)
        if first_name:
            User.objects.filter(username=username).update(first_name=first_name)
        return client


class AutoShipOnCompleteTests(_ShipFixture):
    """判据 1/2/3 + D7/D8：状态机钩子那条路。"""

    def test_the_module_global_is_the_looked_up_name(self):
        """**桩的靶子判据**（E-0029）：`xgj_ship` 里那个全局名与 `xgj.py` 的定义是同一个对象。

        没有这条，把桩打到 `apps.business.xgj.logistics_dummy_send` 上也会**看起来**通过
        （`send_order_shipping` 查的是 `xgj_ship` 的全局名 ⇒ 桩根本没接上、真函数照跑）。
        """
        self.assertIs(xgj_ship.logistics_dummy_send, xgj.logistics_dummy_send)

    def test_completing_an_order_writes_one_send_log_and_one_order_log(self):
        """判据 1：恰好一条发货记录 + 恰好一条订单日志，且值都对。"""
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()

        stub.assert_called_once()
        # 单号 = 录单时填的来源单号；`send_way` 固定 1（D6）；凭据取解析链上的那一条。
        self.assertEqual(stub.call_args.args[0], TB_NO)
        self.assertEqual(stub.call_args.kwargs["app_key"], "AK-TEST-1")
        self.assertEqual(stub.call_args.kwargs["app_secret"], "SK-TEST-1")
        self.assertEqual(stub.call_args.kwargs["send_way"], 1)

        self.assertEqual(self.rows().count(), 1)
        row = self.rows().get()
        self.assertEqual(row.order_no, ORDER_NO)
        self.assertEqual(row.tb_order_no, TB_NO)
        self.assertEqual(row.result, 1)
        self.assertEqual(row.operator, "系统")
        self.assertEqual(row.store_name, "测试店铺甲")
        self.assertEqual(row.remark, "")
        # 「发货原参数」是**实际发出去的**那两个业务参数（⛔ 不含 app_secret / sign）。
        self.assertEqual(json.loads(row.send_params)["send_way"], 1)
        self.assertEqual(json.loads(row.send_params)["order_no"], TB_NO)
        self.assertNotIn("SK-TEST-1", row.send_params)
        self.assertNotIn("sign", row.send_params)

        self.assertEqual(self.logs().count(), 1)
        log = self.logs().get()
        # 正文显示操作人、来源单号和发货结果，系统单号负责关联订单。
        self.assertEqual(log.summary, f"【系统】店铺订单 {TB_NO} 发货成功")
        self.assertEqual(log.order_no, ORDER_NO)
        self.assertEqual(log.source_order_no, TB_NO)
        self.assertEqual(log.actor, "系统")
        self.assertEqual(log.reason, "成功")

    def test_the_upstream_call_is_deferred_until_after_commit(self):
        """硬要求 2：真正的调用排在提交之后（回调节点之前一行都不写、也不打上游）。"""
        with mock.patch(STUB_TARGET, return_value={}) as stub, self.inline_dispatch():
            with self.captureOnCommitCallbacks() as callbacks:
                apply_transition(
                    self.order, OrderStatus.COMPLETED,
                    nodes={"order.change_status_completed"},
                )
            # 提交之前：上游零调用、零留痕。
            stub.assert_not_called()
            self.assertEqual(self.rows().count(), 0)
            self.assertTrue(callbacks, "一个回调都没排 ⇒ 提交之后不会发货")
            # 手动把排下的回调跑掉 ⇒ 它确实是我这一条（而不是别的线的回调凑数）。
            for callback in callbacks:
                callback()
        stub.assert_called_once()
        self.assertEqual(self.rows().count(), 1)

    def test_upstream_failure_keeps_the_order_completed_and_uses_the_upstream_text(self):
        """判据 2：上游失败 ⇒ 状态**不回滚**、`remark` 是上游原文。"""
        with mock.patch(
            STUB_TARGET, side_effect=XgjError(upstream_code=100004, upstream_msg="订单不存在")
        ):
            self.complete()

        # 状态没回滚（spec §3.4 硬要求 1）：库里仍是已完成。
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        # **上游原文**逐字（⛔ 不加我们自己的前缀）。
        self.assertEqual(row.remark, "订单不存在")
        self.assertEqual(self.logs().get().summary, f"【系统】店铺订单 {TB_NO} 发货失败（订单不存在）")

    def test_failure_envelope_is_judged_a_failure(self):
        """E-0087 同族：上游回「失败信封」（`code != 0` ⇒ `XgjError`）**必须**判失败。

        判别力：把 `xgj_ship` 里那句 `except XgjError` 删掉 ⇒ 本用例红
        （记录会变成成功行、或者订单直接 500）。
        """
        with mock.patch(
            STUB_TARGET,
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            self.complete()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "签名错误")
        self.assertEqual(TaobaoOrderSendLog.objects.filter(result=1).count(), 0)

    def test_already_shipped_rejection_is_judged_a_success(self):
        """E-0237：上游回「订单已发货不支持操作」⇒ **记发货成功**（业务方 2026-10-11 口径）。

        对方侧已发过货（人工在闲管家发过 / 别的渠道发过）时我方库没有成功行，D7 兜不到；
        这一回执 = 目标态已达成。判据：成功行 + `remark` 留上游原文可追溯 + 状态不回滚
        + **D7 随之生效**（下次重试认出成功行，不再打上游）。
        """
        with mock.patch(
            STUB_TARGET,
            side_effect=XgjError(
                upstream_code=100004, upstream_msg="订单已发货不支持操作"
            ),
        ) as stub:
            self.complete()
        stub.assert_called_once()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        row = self.rows().get()
        self.assertEqual(row.result, 1)
        self.assertIn("订单已发货不支持操作", row.remark)
        self.assertIn("发货成功", row.remark)
        self.assertEqual(self.logs().get().reason, "成功")
        # D7 联动：已有一张成功行 ⇒ 重试不再调上游、不新增记录行（spec §3.5）。
        with mock.patch(STUB_TARGET) as retry_stub:
            order = Order.objects.get(order_no=ORDER_NO)
            xgj_ship.send_order_shipping(order, operator=xgj_ship.SYSTEM_OPERATOR)
        retry_stub.assert_not_called()
        self.assertEqual(self.rows().count(), 1)

    def test_no_source_order_no_does_not_call_upstream(self):
        """判据 3（前半）：来源单号为空 ⇒ 零调用 + 记失败 + 原因点名。"""
        self.order.source_order_no = ""
        self.order.save(update_fields=["source_order_no"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
        stub.assert_not_called()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "订单没有来源单号，无法发货")
        self.assertEqual(row.tb_order_no, "")
        self.assertEqual(
            self.logs().get().summary,
            f"【系统】店铺订单 {ORDER_NO} 发货失败（订单没有来源单号，无法发货）",
        )

    def test_no_shop_or_no_credentials_does_not_call_upstream(self):
        """判据 3（后半）：没有店 / 没有应用 / 凭据不全 ⇒ 零调用 + `未绑定闲管家店铺/应用`。"""
        # ① 订单没选来源（连店都不去查）。
        self.order.order_source = None
        self.order.save(update_fields=["order_source"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
        stub.assert_not_called()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "未绑定闲管家店铺/应用")   # spec §3.3 的逐字文案
        self.assertEqual(row.store_name, "")

        # ② 有店、但凭据不全（`app_secret` 是空串）——那是「这一行还没配好」，同一条原因。
        TaobaoOrderSendLog.objects.all().delete()
        OperationLog.objects.filter(action=ACTION).delete()
        self.order.order_source = self.source
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["order_source", "status"])
        self.binding.app_secret = ""
        self.binding.save(update_fields=["app_secret"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
        stub.assert_not_called()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "未绑定闲管家店铺/应用")
        # 店解析到了 ⇒ 店铺名快照仍写下来（它对排障有用，spec §3.1 只说「解析不到才是空串」）。
        self.assertEqual(row.store_name, "测试店铺甲")

    def test_source_without_a_shop_row_or_with_a_null_binding_does_not_call_upstream(self):
        """判据 3 的另两档（spec §3.3 解析链的**中段**，与上一条互补）。

        上一条覆盖的是「订单**没选来源**」与「有店但**凭据不全**」；本条覆盖
        「有来源、但**没有任何 `XgjShop` 行**指向它」与「店解析到了、但它的 **`binding` 是 NULL**」
        （`SET_NULL` 的常态：应用被删掉 / 还没挂）。
        两档都必须**零上游调用** + 落一条 `result=0` + `remark` 点名原因
        （文案照实现现状 `未绑定闲管家店铺/应用`，⛔ 不新造文案）。
        """
        # ③ 有来源，但没有 XgjShop 行指向它（`XgjShop.objects.filter(order_source=…).first()` 回 None）。
        self.shop.delete()
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
        stub.assert_not_called()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "未绑定闲管家店铺/应用")
        self.assertEqual(row.store_name, "")   # 店都没解析到 ⇒ 快照只能是空串

        # ④ 店解析到了，但它的 `binding` 是 NULL（应用被删 / 还没挂）。
        TaobaoOrderSendLog.objects.all().delete()
        OperationLog.objects.filter(action=ACTION).delete()
        shop = XgjShop.objects.create(
            binding=None, user_identity="ID-TEST-2",
            shop_name="测试店铺乙", order_source=self.source,
        )
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
        stub.assert_not_called()
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "未绑定闲管家店铺/应用")
        self.assertEqual(row.store_name, shop.shop_name)   # 店解析到了 ⇒ 名字仍写下

    def test_unexpected_exception_is_swallowed_and_named_as_an_internal_error(self):
        """D8 的补救（spec §5-3）：内部错误被吞掉、记录里**点名**、订单照样完成。"""
        with mock.patch(STUB_TARGET, side_effect=RuntimeError("合成炸弹")):
            self.complete()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertTrue(
            row.remark.startswith("内部错误：RuntimeError"),
            f"内部错误必须点名（⛔ 不许写成上游拒绝那一类文案）：{row.remark!r}",
        )
        self.assertIn("合成炸弹", row.remark)
        log = self.logs().get()
        self.assertEqual(log.reason, "失败（内部错误：RuntimeError: 合成炸弹）")
        self.assertEqual(
            log.summary,
            f"【系统】店铺订单 {TB_NO} 发货失败（系统暂未能提交发货，请查看发货记录）",
        )

    def test_completing_twice_keeps_one_success_row_and_writes_two_logs(self):
        """D7（spec §5-2）：再触发一次 ⇒ 记录表仍只有一行 `result=1`，日志有两行。"""
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            self.complete()
            self.assertEqual(stub.call_count, 1)
            # 离开「已完成」再回来（状态机整体放开，D-0147）⇒ 第二次到达 COMPLETED。
            with self.captureOnCommitCallbacks(execute=True):
                apply_transition(self.order, OrderStatus.PENDING_REVIEW,
                                 nodes={"order.change_status"})
            self.complete()
            # ⛔ 第二次**不调上游**（已发货）。
            self.assertEqual(stub.call_count, 1)
        self.assertEqual(self.rows().filter(result=1).count(), 1)
        self.assertEqual(
            list(self.logs().order_by("id").values_list("summary", flat=True)),
            [
                f"【系统】店铺订单 {TB_NO} 发货成功",
                f"【系统】店铺订单 {TB_NO} 发货已完成，本次跳过重复发货",
            ],
        )


class HookRegistrationTests(_ShipFixture):
    """注册处与**顺序**的判据（spec §3.4：排在 `on_order_completed` 之后）。

    ⚠️ 本条钉的是 **spec 要求的注册顺序**（契约），⛔ 不是「顺序承重」：
    本钩子只排 `transaction.on_commit` 回调，而算钱失败时调用方整笔回滚、回调被丢弃
    ⇒ 今天两种顺序的可观测结果相同（`settlement_hooks.py` 注册处如实登记了这一点）。
    """

    def test_both_completed_hooks_are_registered_in_order(self):
        hooks = _TARGET_HOOKS[str(OrderStatus.COMPLETED)]
        self.assertIn(settlement_hooks.on_order_completed, hooks)
        self.assertIn(settlement_hooks.on_order_completed_ship, hooks)
        self.assertLess(
            hooks.index(settlement_hooks.on_order_completed),
            hooks.index(settlement_hooks.on_order_completed_ship),
            "spec §3.4 要求发货排在算钱之后（⛔ 别为了「并发快一点」把注册顺序换过来）",
        )

    def test_the_hook_only_schedules_the_call(self):
        """钩子体只**排**一次调用（⛔ 不在事务里同步打上游）。

        ⚠️ 桩打在 `settlement_hooks` 上（E-0029：被查找的那个模块是
        `apps/business/settlement_hooks.py`）。
        ⚠️ **D-0357 改准**：入口换成**发货闸** `schedule_auto_ship_gated`——非拆单单
        在闸里属性短路后仍走 `schedule_auto_ship`（行为对普通单不变），但钩子**自己**
        调的是带闸入口 ⇒ 本用例钉「钩子 → 闸」这一跳，闸内部两支的判据在
        `tests/test_split_ship_gate.py`。
        """
        order = Order.objects.create(
            game=Game.objects.create(name="测试游戏乙"),
            order_no="20261003000000000002", status=OrderStatus.COMPLETED,
        )
        with mock.patch(
            "apps.business.settlement_hooks.schedule_auto_ship_gated"
        ) as gated:
            settlement_hooks.on_order_completed_ship(order, OrderStatus.IN_PROGRESS)
        gated.assert_called_once_with(order)

    def test_a_rolled_back_completion_never_calls_upstream(self):
        """算钱失败 ⇒ 整笔回滚 ⇒ **一次上游都不打**、零留痕、状态也不是已完成。

        构造照 `tests/test_jidanpu_immediate_sync.py::OnCommitSemanticsTests` 的同一先例：
        在**显式的最外层 `atomic()`** 里让算钱那一步抛 `CommissionError`（它是**预期失败**，
        调用方会把整笔回滚）⇒ `transaction.on_commit` 排下的回调随回滚被 Django 丢弃。

        ⚠️ 这条同时是「发货排在算钱之后」那句注册注释的**实测依据**：被丢弃的是
        `on_commit` 回调本身（与两个钩子的注册先后无关）——所以那句话里如实写着
        「今天的可观测结果相同，保留顺序是为了将来改成同步调的那种写法」。
        """
        with mock.patch(
            "apps.business.settlement_hooks.recompute_customer_commission",
            side_effect=CommissionError("合成的提成算不出来", ErrorCode.BUSINESS),
        ), mock.patch(STUB_TARGET, return_value={}) as stub:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(CommissionError):
                    with transaction.atomic():
                        apply_transition(
                            self.order, OrderStatus.COMPLETED,
                            nodes={"order.change_status_completed"},
                        )
        stub.assert_not_called()
        self.assertEqual(list(callbacks), [])
        self.assertEqual(self.rows().count(), 0)
        self.assertEqual(self.logs().count(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)


class AsyncDispatchTests(_ShipFixture):
    """PROD-02 合同（**请求路径不出网、不等外呼**）在**队列形状**（D-0358）下的回归钉。

    改准：完单回调不再「每单起一个线程」，而是**入进程内队列**；
    旧类里「每单恰好一个发货线程 / daemon / 线程名」那组断言随旧形状一并废除，
    「不等待」职责由本类接管：判据钉「回调 / 响应返回时**只入队**、上游零调用」，
    再手动驱动一步（真 worker 的活）证明队列里的单最终发出、留痕落库。

    ⚠️ 凡走「入队」的用例把 `_ensure_ship_worker_locked` 替成空操作（E-0029：
    被查找的那个模块）——⛔ 不让懒启动在测试进程里拉起**真** worker：它拿另一条
    连接消费队列，与本用例未提交的订单竞态。驱动步的替身口径（`time.sleep` /
    `xgj_ship.connections`）见 `tests/test_xgj_ship_queue.py` 的模块 docstring。
    ⚠️ `apply_transition` 是单条 `/transition/` 端点与 `orders/batch` 批量端点**共用**
    的那道缝隙（完单钩子挂在它上面）——钉这里 = 两个请求路径一起钉住。
    """

    def test_completion_enqueues_and_returns_without_calling_upstream(self):
        """回调（= 请求路径）返回时：队列/pending 各恰一条（attempt 1 基、schema 已捕获）、
        上游零调用、零留痕；手动驱动一步后留痕落库。"""
        with mock.patch(STUB_TARGET, return_value={}) as stub, \
                mock.patch.object(xgj_ship, "_ensure_ship_worker_locked"):
            with self.captureOnCommitCallbacks(execute=True):
                apply_transition(
                    self.order, OrderStatus.COMPLETED,
                    nodes={"order.change_status_completed"},
                )
            # 回调已经返回（with 块退出）：只该有「队列里一条」，不该有任何上游动作。
            self.assertEqual(
                list(xgj_ship._ship_queue),
                [(ORDER_NO, connection.schema_name, 1)],
            )
            self.assertEqual(xgj_ship._ship_pending, {ORDER_NO})
            stub.assert_not_called()
            self.assertEqual(self.rows().count(), 0)
        # 手动驱动一步（worker 的活，同步直调不竞态）：消费成功，留痕落库。
        with mock.patch(STUB_TARGET, return_value={}) as stub2, \
                mock.patch.object(xgj_ship, "connections"), \
                mock.patch("time.sleep"):
            xgj_ship._ship_worker_step()
        stub2.assert_called_once()
        self.assertEqual(self.rows().count(), 1)

    def test_the_transition_endpoint_returns_while_the_ship_is_still_queued(self):
        """HTTP 端点形状的同一条判据：`/transition/` 完单请求**先返回**，发货还在队列里
        （等 worker 陆续发——D-0358「验收立即生效、发货陆续走」的业务方口径）。"""
        client = self.logged_in(username="ship-async", nodes=("order.change_status_completed",))
        with mock.patch(STUB_TARGET, return_value={}) as stub, \
                mock.patch.object(xgj_ship, "_ensure_ship_worker_locked"):
            with self.captureOnCommitCallbacks(execute=True):
                resp = client.post(
                    f"/api/business/orders/{ORDER_NO}/transition/",
                    {"target": OrderStatus.COMPLETED},
                    content_type="application/json",
                )
            self.assertEqual(resp.status_code, 200, resp.content[:200])
            self.assertEqual(resp.json()["code"], 0)
            # 响应回来了，发货**还没发**（在队列里）——这就是「请求不等外呼」。
            self.assertEqual(len(xgj_ship._ship_queue), 1)
            self.assertEqual(xgj_ship._ship_pending, {ORDER_NO})
            stub.assert_not_called()
        # 队列里的单稍后被 worker 消费 ⇒ 留痕最终落库（验收即时性 + 陆续发货同时成立）。
        with mock.patch(STUB_TARGET, return_value={}) as stub2, \
                mock.patch.object(xgj_ship, "connections"), \
                mock.patch("time.sleep"):
            xgj_ship._ship_worker_step()
        stub2.assert_called_once()
        self.assertEqual(self.rows().count(), 1)


class ThreadBodyGuardTests(_ShipFixture):
    """daemon 线程体自己的护栏：吞未预期异常、看门狗接线与告警、用完关连接。

    ⚠️ 直调 `_auto_ship_in_thread` 时它 finally 会 `connections.close_all()` —— 那会把
    **测试线程**的事务连接关掉 ⇒ 每条用例都把它查找的 `xgj_ship.connections` 替成
    替身再调（E-0029：桩打在被查找的那个模块）。
    """

    def test_unexpected_error_is_swallowed_and_logged_not_raised(self):
        """「连留痕都写不进去」级故障：线程体吞掉、记完整栈（改前它会让那次请求 500）。"""
        with mock.patch.object(xgj_ship, "auto_ship", side_effect=RuntimeError("合成炸弹三")), \
             mock.patch.object(xgj_ship.connections, "close_all") as close_all, \
             self.assertLogs("apps.business.xgj_ship", level="ERROR") as captured:
            xgj_ship._auto_ship_in_thread(order_no=ORDER_NO, schema_name=None)
        self.assertTrue(
            any("合成炸弹三" in line for line in captured.output),
            f"完整栈要进服务器日志：{captured.output}",
        )
        close_all.assert_called_once()

    def test_the_watchdog_is_wired_with_the_budget_and_cancelled_on_exit(self):
        """看门狗接线：预算常量 + 告警函数 + 单号；结束时 start 过、也 cancel 过。"""
        with mock.patch.object(xgj_ship, "auto_ship", return_value=None), \
             mock.patch.object(xgj_ship.connections, "close_all"), \
             mock.patch.object(xgj_ship.threading, "Timer") as timer:
            timer.return_value = mock.Mock()
            xgj_ship._auto_ship_in_thread(order_no=ORDER_NO, schema_name=None)
        timer.assert_called_once_with(
            xgj_ship.AUTO_SHIP_TIME_BUDGET, xgj_ship._warn_ship_over_budget, args=(ORDER_NO,)
        )
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()

    def test_over_budget_watchdog_emits_a_named_warning(self):
        """到点的看门狗只记 warning、带上单号与预算（杀不掉线程是如实登记的边界）。"""
        with self.assertLogs("apps.business.xgj_ship", level="WARNING") as captured:
            xgj_ship._warn_ship_over_budget(ORDER_NO)
        self.assertTrue(
            any(ORDER_NO in line and f"{xgj_ship.AUTO_SHIP_TIME_BUDGET:.0f} 秒" in line
                for line in captured.output),
            f"告警要能定位到单与预算：{captured.output}",
        )


class ShipBodySeamTests(_ShipFixture):
    """解耦后「发货本体」的直接用例（计划测试要求 2）：线程体抽出的可直调缝隙。

    ⚠️ 全部走**测试线程直调** `run_auto_ship(..., schema_name=None)`：
    成功写留痕、失败写失败留痕的判据与既有网相同，这里钉的是「新缝隙接的是同一份实现」。
    """

    def test_success_writes_the_ledger_through_the_new_seam(self):
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            xgj_ship.run_auto_ship(order_no=ORDER_NO, schema_name=None)
        stub.assert_called_once()
        row = self.rows().get()
        self.assertEqual(row.result, 1)
        self.assertEqual(row.operator, "系统")
        self.assertEqual(
            self.logs().get().summary, f"【系统】店铺订单 {TB_NO} 发货成功"
        )

    def test_failure_writes_the_failure_ledger_through_the_new_seam(self):
        """失败留痕不丢（D4）：线程体里失败也是**写库**，不是只写日志。"""
        with mock.patch(
            STUB_TARGET,
            side_effect=XgjError(upstream_code=100004, upstream_msg="店铺无此订单信息"),
        ):
            xgj_ship.run_auto_ship(order_no=ORDER_NO, schema_name=None)
        row = self.rows().get()
        self.assertEqual(row.result, 0)
        self.assertEqual(row.remark, "店铺无此订单信息")
        self.assertEqual(
            self.logs().get().summary,
            f"【系统】店铺订单 {TB_NO} 发货失败（店铺无此订单信息）",
        )

    def test_schema_is_only_set_when_given(self):
        """schema 是线程的**入参**（django-tenants 连接级状态的硬约束）：给了就落、没给不动。"""
        with mock.patch.object(connection, "set_schema") as set_schema, \
                mock.patch(STUB_TARGET, return_value={}):
            xgj_ship.run_auto_ship(order_no=ORDER_NO, schema_name="test")
        set_schema.assert_called_once_with("test")

        with mock.patch.object(connection, "set_schema") as set_schema_again, \
                mock.patch(STUB_TARGET, return_value={}):
            xgj_ship.run_auto_ship(order_no=ORDER_NO, schema_name=None)
        set_schema_again.assert_not_called()


class RetryEndpointTests(_ShipFixture):
    """判据 4 + 契约：`POST /api/business/tb-send-logs/retry/`（spec §3.7 第二行）。"""

    def setUp(self):
        super().setUp()
        self.url = "/api/business/tb-send-logs/retry/"
        # ⚠️ client **每条用例只建一次**：`make_logged_in_client` 走真实注册/登录，
        # 同一个用例里建第二次会撞 `auth_user_username_key`（用户名唯一）。
        self.client = self.logged_in()

    def _failed_row(self, *, tb_no=TB_NO):
        """造一条**失败**记录（走领域层那条真路，而不是直接 create 一个假行）。"""
        self.order.source_order_no = tb_no
        self.order.save(update_fields=["source_order_no"])
        with mock.patch(
            STUB_TARGET, side_effect=XgjError(upstream_code=100004, upstream_msg="店铺无此订单信息")
        ):
            self.complete()
        return self.rows().get()

    def _post(self, payload, *, client=None):
        client = client or self.client
        return client.post(self.url, payload, content_type="application/json")

    def test_retry_re_sends_and_reports_counts_and_detail(self):
        row = self._failed_row()
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [row.pk]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["success"], 1)
        self.assertEqual(body["data"]["fail"], 0)
        item = body["data"]["items"][0]
        self.assertEqual(item["id"], row.pk)
        self.assertEqual(item["result"], 1)
        self.assertEqual(item["remark"], "")
        self.assertIsNotNone(item["record_id"], "这次真的发了 ⇒ 必须有一条**新建**的记录行")
        stub.assert_called_once()
        # 前端传的是**行 id**，后端自己回查那一行取单号（spec §3.5 / 契约裁决）。
        self.assertEqual(stub.call_args.args[0], TB_NO)
        self.assertEqual(self.rows().count(), 2)

    def test_retry_uses_the_row_snapshot_not_the_current_order_value(self):
        """spec §3.5：重试拿**勾选行**的 `TbOrderNo` 重发（⛔ 不是订单的当前值）。"""
        row = self._failed_row()
        # 订单上的来源单号事后被改成另一个号：重试仍必须发**那一行**的快照。
        self.order.source_order_no = TB_NO_OTHER
        self.order.save(update_fields=["source_order_no"])
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [row.pk]})
        self.assertEqual(resp.json()["data"]["success"], 1)
        self.assertEqual(stub.call_args.args[0], TB_NO)

    def test_retry_on_an_already_successful_row_adds_no_row(self):
        """判据 4 + D7：对已成功的单再发一次 ⇒ 不调上游、不新增行，仍报成功。"""
        with mock.patch(STUB_TARGET, return_value={}):
            self.complete()
        row = self.rows().get()
        self.assertEqual(row.result, 1)
        logs_before = self.logs().count()
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [row.pk]})
        stub.assert_not_called()
        body = resp.json()
        self.assertEqual(body["data"]["success"], 1)
        self.assertEqual(body["data"]["fail"], 0)
        self.assertIsNone(body["data"]["items"][0]["record_id"])
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.logs().count(), logs_before + 1)

    def test_retry_failure_is_counted_and_keeps_the_named_reason(self):
        row = self._failed_row()
        with mock.patch(
            STUB_TARGET, side_effect=XgjError(upstream_code=100004, upstream_msg="还是不行")
        ):
            resp = self._post({"ids": [row.pk]})
        body = resp.json()
        self.assertEqual(body["data"]["success"], 0)
        self.assertEqual(body["data"]["fail"], 1)
        self.assertEqual(body["data"]["items"][0]["remark"], "还是不行")
        # 每次真发的尝试各留一条记录（失败档再发一次 = 第二行）。
        self.assertEqual(self.rows().count(), 2)

    def test_internal_error_is_counted_as_failure_not_500(self):
        row = self._failed_row()
        with mock.patch(STUB_TARGET, side_effect=RuntimeError("合成炸弹二")):
            resp = self._post({"ids": [row.pk]})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["data"]["fail"], 1)
        self.assertTrue(
            body["data"]["items"][0]["remark"].startswith("内部错误：RuntimeError")
        )

    def test_operator_is_the_logged_in_display_name(self):
        row = self._failed_row()
        with mock.patch(STUB_TARGET, return_value={}):
            resp = self._post({"ids": [row.pk]}, client=self.logged_in(
                username="ship-op", nodes=(NODE,), first_name="测试昵称甲",
            ))
        self.assertEqual(resp.json()["data"]["success"], 1)
        newest = self.rows().order_by("-id").first()
        self.assertEqual(newest.operator, "测试昵称甲")
        # 人工重试的正文显示这位客服的账号名称。
        self.assertEqual(
            self.logs().order_by("-id").first().summary,
            f"【测试昵称甲】店铺订单 {TB_NO} 发货成功",
        )

    def test_retry_for_a_vanished_order_records_a_named_failure(self):
        """系统订单已不在（软删 / 硬删）⇒ 没有可发的单，但仍留一条点名失败。"""
        row = TaobaoOrderSendLog.objects.create(
            order_no="20261003000000000099", tb_order_no=TB_NO_OTHER,
            store_name="测试店铺乙", result=0, sent_at=timezone.now(),
            operator="系统", remark="先前的失败",
        )
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [row.pk]})
        stub.assert_not_called()
        body = resp.json()
        self.assertEqual(body["data"]["fail"], 1)
        self.assertEqual(body["data"]["items"][0]["remark"], "系统订单不存在或已删除，无法发货")

    def test_missing_record_id_is_a_business_code_and_writes_nothing(self):
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [987654]})
        stub.assert_not_called()
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["code"], 1000)
        self.assertEqual(TaobaoOrderSendLog.objects.count(), 0)

    def test_malformed_payloads_are_validation_errors_not_500(self):
        for payload in ({}, {"ids": []}, {"ids": ["abc"]}, {"ids": [0]}, {"ids": [1] * 51}):
            with self.subTest(payload=payload):
                resp = self._post(payload)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["code"], 1000)

    def test_duplicate_ids_are_deduped(self):
        row = self._failed_row()
        with mock.patch(STUB_TARGET, return_value={}) as stub:
            resp = self._post({"ids": [row.pk, row.pk]})
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(len(resp.json()["data"]["items"]), 1)


class ListEndpointTests(_ShipFixture):
    """判据 5：列表的 4 个筛选 + 行形状 + 页大小。"""

    def setUp(self):
        super().setUp()
        self.url = "/api/business/tb-send-logs/"
        self.client = self.logged_in()
        # 三行，`sent_at` 跨一天的分界（按当前时区切天）。
        # ⚠️ 全部用 `timezone.make_aware`（naive 值会走框架兜底并写 RuntimeWarning）。
        self.early = self._row(
            order_no="20261003000000000011", tb_order_no=TB_NO, result=1,
            sent_at=timezone.make_aware(datetime(2026, 10, 1, 23, 30)),
        )
        self.late = self._row(
            order_no="20261003000000000012", tb_order_no=TB_NO_OTHER, result=0,
            sent_at=timezone.make_aware(datetime(2026, 10, 2, 0, 30)),
        )
        self.middle = self._row(
            order_no="20261003000000000013", tb_order_no="22000000000000000003", result=1,
            sent_at=timezone.make_aware(datetime(2026, 10, 2, 12, 0)),
        )

    def _row(self, *, order_no, tb_order_no, result, sent_at):
        return TaobaoOrderSendLog.objects.create(
            order_no=order_no, tb_order_no=tb_order_no, send_params='{"order_no":"x"}',
            store_name="测试店铺甲", result=result, sent_at=sent_at, operator="系统", remark="",
        )

    def _get(self, **params):
        resp = self.client.get(self.url, params)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["code"], 0)
        return body["data"]

    def _ids(self, data):
        return sorted(item["id"] for item in data["items"])

    def test_row_payload_keys_are_the_model_fields_verbatim(self):
        """契约：行载荷是**模型字段名逐字**（前端单元格按这些键取，改名会静默空白）。"""
        data = self._get()
        self.assertEqual(
            sorted(data["items"][0]),
            sorted([
                "id", "order_no", "tb_order_no", "send_params", "store_name",
                "result", "sent_at", "operator", "remark", "created_at",
            ]),
        )

    def test_result_filter_three_values(self):
        self.assertEqual(len(self._get(result=-1)["items"]), 3)
        self.assertEqual(self._ids(self._get(result=1)), sorted([self.early.pk, self.middle.pk]))
        self.assertEqual(self._ids(self._get(result=0)), [self.late.pk])

    def test_exclude_missing_filters_before_pagination(self):
        self.late.remark = "系统订单不存在或已删除，无法发货"
        self.late.save(update_fields=["remark"])
        other = self._row(order_no="20261003000000000014", tb_order_no="22000000000000000004", result=0, sent_at=timezone.now())
        other.remark = "上游连接失败"
        other.save(update_fields=["remark"])
        data = self._get(result=0, exclude_missing="true", page_size=1)
        self.assertEqual(data["total"], 1)
        self.assertEqual([row["order_no"] for row in data["items"]], [other.order_no])
        self.assertEqual(len(self._get(result=0)["items"]), 2)

    def test_date_filter_boundaries_are_inclusive(self):
        """日期边界：`sDate` / `eDate` 两端**都含**（`__date` 按当前时区切天）。"""
        self.assertEqual(self._ids(self._get(sDate="2026-10-02")), sorted([self.late.pk, self.middle.pk]))
        self.assertEqual(self._ids(self._get(eDate="2026-10-01")), [self.early.pk])
        self.assertEqual(
            self._ids(self._get(sDate="2026-10-02", eDate="2026-10-02")),
            sorted([self.late.pk, self.middle.pk]),
        )

    def test_key_hits_both_order_no_and_tb_order_no(self):
        # 系统单号命中一行。
        self.assertEqual(self._ids(self._get(key="20261003000000000012")), [self.late.pk])
        # 淘宝单号命中（`TB_NO` 只属于最早那一行）。
        self.assertEqual(self._ids(self._get(key=TB_NO)), [self.early.pk])
        # 两个字段是**或**：一个前缀同时命中两行的系统单号。
        self.assertEqual(len(self._get(key="202610030000000")["items"]), 3)

    def test_no_filters_returns_everything_newest_first(self):
        data = self._get()
        self.assertEqual(data["total"], 3)
        self.assertEqual(
            [item["id"] for item in data["items"]], [self.middle.pk, self.late.pk, self.early.pk]
        )

    def test_default_page_size_is_the_counterpart_limit(self):
        """对标的 `limit: 50`（只有一项）⇒ 默认页大小 50；layui 传的 `limit` 被忽略也不变。"""
        self.assertEqual(self._get()["page_size"], 50)
        self.assertEqual(self._get(limit=1)["page_size"], 50)

    def test_malformed_query_params_are_business_codes_not_500(self):
        for params in ({"result": "999"}, {"sDate": "garbage"}, {"eDate": "2026-13-99"}, {"exclude_missing": "garbage"}):
            with self.subTest(params=params):
                resp = self.client.get(self.url, params)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["code"], 1000)


class ShipFailureIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(schema_name="ship_iso_b", name="发货隔离乙", domain="ship-iso-b.test.local")

    def test_failure_query_is_scoped_to_authenticated_tenant(self):
        with tenant_context(self.tenant):
            TaobaoOrderSendLog.objects.create(order_no="SHIP-ISO-A", tb_order_no="TB-A", result=0, remark="上游失败", sent_at=timezone.now(), operator="系统")
            client_a = make_logged_in_client(domain="tenant.test.com", username="ship-iso-a", is_superuser=True)
        with tenant_context(self.tenant_b):
            TaobaoOrderSendLog.objects.create(order_no="SHIP-ISO-B", tb_order_no="TB-B", result=0, remark="上游失败", sent_at=timezone.now(), operator="系统")
            client_b = make_logged_in_client(domain="ship-iso-b.test.local", username="ship-iso-b", is_superuser=True)
        body = client_a.get("/api/business/tb-send-logs/", {"result": 0, "exclude_missing": "true"}, SERVER_NAME="tenant.test.com").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([row["order_no"] for row in body["data"]["items"]], ["SHIP-ISO-A"])
        crossed = client_a.get("/api/business/tb-send-logs/", {"result": 0, "exclude_missing": "true"}, SERVER_NAME="ship-iso-b.test.local")
        self.assertEqual(crossed.status_code, 401)
        body_b = client_b.get("/api/business/tb-send-logs/", {"result": 0, "exclude_missing": "true"}, SERVER_NAME="ship-iso-b.test.local").json()
        self.assertEqual(body_b["code"], 0)
        self.assertEqual([row["order_no"] for row in body_b["data"]["items"]], ["SHIP-ISO-B"])
        connection.set_tenant(self.tenant)


class PermissionTests(_ShipFixture):
    """spec §3.7 的 ⚠️：两个动作都要节点门（fail-closed）。"""

    def test_without_the_node_both_actions_are_forbidden(self):
        # 有组、**没有本节点** ⇒ 两个动作都 403（fail-closed，spec §3.7 的 ⚠️）。
        client = make_logged_in_client(username="ship-none", groups=("客服",), nodes=())
        self.assertEqual(client.get("/api/business/tb-send-logs/").status_code, 403)
        self.assertEqual(
            client.post(
                "/api/business/tb-send-logs/retry/", {"ids": [1]},
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_with_the_node_the_list_is_readable(self):
        client = self.logged_in(username="ship-ok")
        resp = client.get("/api/business/tb-send-logs/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0)


class SchemaPropagationTests(_ShipFixture):
    """M-1（线 D 评审必修）的**队列形状**改写（D-0358）：「线程碰库」的承重梁——
    schema 在**入队时**捕获、**每条处理时**才落。

    ⚠️ 三条既有网都钉不到这一段：AsyncDispatchTests 只钉「只入队、不等」、
    ThreadBodyGuard 直调传 `schema_name=None`、SeamTests 只钉缝隙自身契约
    ⇒ 回归成「捕获 None / 丢传」时它们全绿、生产每单发货只剩一条 ERROR 日志、零台账。
    本条**同步直调、不跟真线程竞态**：worker 懒启动替空、`connection` 替成
    带 `schema_name` 的替身（捕获值的来源）、`connections` 替掉（`close_all`
    不许关测试事务连接，口径照 `ThreadBodyGuardTests`）、`auto_ship` 换记录桩
    （每条处理体走真的 `run_auto_ship`，`set_schema` 断言才有靶）。
    判别力：把 `dispatch_auto_ship` 里的 schema 捕获删掉 ⇒ 队列元素不是
    `tenant-probe`、断言红；把 `run_auto_ship` 里的 `set_schema` 调用删掉 ⇒ 同样红。
    """

    def test_enqueue_captures_schema_and_processing_sets_it(self):
        schema_conn = mock.Mock()
        schema_conn.schema_name = "tenant-probe"
        shipped = []
        with mock.patch.object(xgj_ship, "connection", schema_conn), \
             mock.patch.object(xgj_ship, "_ensure_ship_worker_locked"), \
             mock.patch.object(xgj_ship, "connections"), \
             mock.patch.object(xgj_ship, "auto_ship",
                               side_effect=lambda order_no: shipped.append(order_no)), \
             mock.patch("time.sleep"):
            xgj_ship.dispatch_auto_ship("20261020000000000090")
            # 入队形状：schema 是**当刻连接**的值、attempt 1 基，与单号一起进队列。
            self.assertEqual(
                list(xgj_ship._ship_queue),
                [("20261020000000000090", "tenant-probe", 1)],
            )
            # 每条处理时才把 schema 落到（worker 那条）连接上——同步直调每条函数。
            xgj_ship._process_ship_item("20261020000000000090", "tenant-probe", 1)
        self.assertEqual(shipped, ["20261020000000000090"])
        schema_conn.set_schema.assert_called_once_with("tenant-probe")
