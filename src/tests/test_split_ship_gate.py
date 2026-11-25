"""D-0357（拆单组协作重设计）的**发货闸网**（spec §7.2 / G5-G8）。

判据（全部逐字来自 spec §3.2）：

- **G5 完成流转零新增闸**：组内任何单按既有状态机自由完成——本网每一条「组未齐
  ⇒ 完成」的用例都同时钉住「完成**放行**」（谁在 transition 里加了组闸，这里红）；
- **G6 发货闸挂发货侧**：组未齐 ⇒ 发货跳过 + 一条人可见「暂不发货（X/N）」日志；
  末单完成 ⇒ 恰好真发一次；
- **G7 组幂等**：组内任一成员已有 SUCCESS 行 ⇒ 只补成功日志不重发
  （「重试发货」手工口**不在**本网——它不经闸，边界见 spec §6）；
- **同批批量**是一个大事务：按「当时刻快照」逐单过闸 ⇒ 只末跳发货（spec §6 勘误 3）；
- **并发末两单**：两事务交错 ⇒ 恰好一发（组互斥锁：锁组内 `id` 最小成员行——
  为什么不锁全组（与 `apply_transition` 已持有的本单行锁会 AB-BA 成环），
  见 `xgj_ship.schedule_auto_ship_gated` 的 docstring）。

## 七个完成入口的覆盖口径（spec §2.1 的表；⛔ 不留未定义）

全部入口共用唯一实现 `apply_transition` 的目标态钩子 ⇒ 闸结构性全覆盖。本网按
入口**各自**钉「完成放行 + 发货被跳过」：

| 入口 | 本网的用例 |
|---|---|
| 内网单笔 `/transition/` | `TransitionEndpointHoldTests` |
| 内网批量 `/orders/batch/transition` | `BatchCompletionTests` |
| `/review/` 审核通过 | `ReviewEndpointHoldTests` |
| 开放平台 `POST /api/open/v1/orders/<no>/status/` | `OpenApiStatusHoldTests` |
| 代练通回写（`_apply_local_status` 两步式） | `DomainFunnelHoldTests` |
| 平台订单页「改系统状态」（`platform_push_action_views`） | 与 `/transition/` **同一漏斗**（它的动作体就是 `transition_order`，即单笔端点的同一个函数）⇒ 由 `TransitionEndpointHoldTests` 结构性覆盖，不另造平台推送夹具 |
| 数据修复命令 `fix_missing_taker_performance` | 与域层直调**同一漏斗**（REWALK 两步就是两次 `apply_transition`）⇒ 由 `DomainFunnelHoldTests`（同为机器直调形态）覆盖其形状 |

夹具值一律合成（E-0077）；断言打在**数据库行**与**真实出口**（发货记录行 / 订单日志行）上。
"""
import json
import threading
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.db import connection, connections, transaction
from django.core.management import call_command
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.audit.models import OperationLog
from apps.business import xgj_ship
from apps.business.dailiantong_push import _apply_local_status
from apps.business.models import (
    EmpType,
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderSource,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
    ReviewStatus,
    SendResult,
    TaobaoOrderSendLog,
    XgjShop,
    XgjShopBinding,
)
from apps.business.order_status import apply_transition
from apps.openapi import tokens
from apps.openapi.models import ApiApp
from tests.p5_auth_helpers import make_logged_in_client

GROUP = "74000000000000000001"
ORDER_NO_MAIN = "20261101000000000401"
ORDER_NO_CHILD = "20261101000000000402"
#: 合成来源单号：组内成员**共享**上游单号（这正是 G7 组幂等的存在理由）。
TB_NO = "22000000000000000401"

COMPLETE_NODES = {"order.change_status", "order.change_status_completed"}

HOLD_ACTION = "order.split_ship_hold"


class _SplitShipFixture(TenantTestCase):
    """「有来源 + 有店 + 有完整凭据」的两成员拆单组（解析链每环都在）。"""

    def setUp(self):
        self.game = Game.objects.create(name="拆单发货闸夹具游戏")
        self.source = OrderSource.objects.create(name="拆单发货闸来源甲")
        self.binding = XgjShopBinding.objects.create(
            shop_name="拆单发货闸应用甲", app_key="AK-SG-1", app_secret="SK-SG-1"
        )
        self.shop = XgjShop.objects.create(
            binding=self.binding, user_identity="ID-SG-1",
            shop_name="拆单发货闸店铺甲", order_source=self.source,
        )
        self.main = Order.objects.create(
            game=self.game, order_no=ORDER_NO_MAIN, status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no=TB_NO, split_id=GROUP,
            total_price=Decimal("300.00"),
        )
        self.child = Order.objects.create(
            game=self.game, order_no=ORDER_NO_CHILD, status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no=TB_NO, split_id=GROUP,
        )
        # 队列与去重集是 `xgj_ship` 的模块级单例、跨用例存活 ⇒ 两头清空。
        xgj_ship._ship_queue.clear()
        xgj_ship._ship_pending.clear()

    def tearDown(self):
        xgj_ship._ship_queue.clear()
        xgj_ship._ship_pending.clear()

    # ── 工具 ──────────────────────────────────────────────────────────

    def inline_dispatch(self):
        """把「提交后入队」替成**线程体同步直调**（留痕类既有网的同款替身，
        见 `tests/test_xgj_auto_ship.py::_ShipFixture.inline_dispatch`）。
        """
        return mock.patch.object(
            xgj_ship,
            "dispatch_auto_ship",
            side_effect=lambda order_no: xgj_ship.run_auto_ship(
                order_no=order_no, schema_name=None
            ),
        )

    def stub_upstream(self):
        """上游 HTTP 桩：桩打在**被查找的模块**（E-0029）；「接上了」由调用方断言。"""
        return mock.patch.object(xgj_ship, "logistics_dummy_send")

    def complete_via_transition(self, order, client):
        """单笔 `/transition/` 完成一张单，并执行排下的 on_commit 回调。"""
        with self.inline_dispatch(), self.captureOnCommitCallbacks(execute=True):
            return client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": OrderStatus.COMPLETED},
                content_type="application/json",
            )

    def hold_logs(self, order_no):
        return OperationLog.objects.filter(action=HOLD_ACTION, order_no=order_no)


class TransitionEndpointHoldTests(_SplitShipFixture):
    """入口①：内网单笔 `/transition/`——组未齐 ⇒ 完成放行、发货跳过有日志。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="sg-cs", groups=("客服",),
            nodes=("order.change_status", "order.change_status_completed",
                   "order.list", "order.view_detail"),
        )

    def refund(self, order):
        with self.inline_dispatch(), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": OrderStatus.REFUNDED},
                content_type="application/json",
            )

    def test_an_incomplete_group_member_completes_but_the_ship_is_held(self):
        """**G5 + G6 的正面钉**：完成放行（DB 状态 COMPLETED）+ 无发货记录 + 暂不发日志。"""
        with self.stub_upstream() as upstream:
            response = self.complete_via_transition(self.child, self.client)
        body = response.json()
        self.assertEqual(body["code"], 0, body)  # 完成本身不被闸挡（G5）
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, OrderStatus.COMPLETED)
        upstream.assert_not_called()             # 发货侧被跳过（G6）
        self.assertFalse(
            TaobaoOrderSendLog.objects.filter(order_no=self.child.order_no).exists()
        )
        logs = self.hold_logs(self.child.order_no)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.get().reason, "1/2")     # X/N：组内已完成 1 / 共 2
        self.assertIn("暂不发货", logs.get().summary)  # 人话正文可见

    def test_the_last_completion_ships_exactly_once(self):
        """第一张完成被 hold 之后，末单完成 ⇒ **恰好一次真发**（上游桩各断言）。"""
        first = self.complete_via_transition(self.child, self.client)
        self.assertEqual(first.json()["code"], 0, first.json())
        with self.stub_upstream() as upstream:
            second = self.complete_via_transition(self.main, self.client)
        self.assertEqual(second.json()["code"], 0, second.json())
        upstream.assert_called_once()  # 上游被调了恰好一次（末跳）
        row = TaobaoOrderSendLog.objects.get(order_no=self.main.order_no)
        self.assertEqual(row.result, SendResult.SUCCESS)

    def test_refunded_order_is_excluded_from_the_completion_gate(self):
        with self.stub_upstream() as upstream:
            refund = self.refund(self.child)
            upstream.assert_not_called()
            complete = self.complete_via_transition(self.main, self.client)

        self.assertEqual(refund.json()["code"], 0, refund.json())
        self.assertEqual(complete.json()["code"], 0, complete.json())
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], TB_NO)

    def test_refunded_main_routes_the_shipment_but_normal_child_owns_the_attempt(self):
        self.child.source_order_no = f"{TB_NO}-1"
        self.child.save(update_fields=["source_order_no"])
        with self.stub_upstream() as upstream:
            refund = self.refund(self.main)
            upstream.assert_not_called()
            complete = self.complete_via_transition(self.child, self.client)

        self.assertEqual(refund.json()["code"], 0, refund.json())
        self.assertEqual(complete.json()["code"], 0, complete.json())
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], TB_NO)
        self.assertEqual(TaobaoOrderSendLog.objects.get().order_no, self.child.order_no)

    def test_refunding_last_blocker_queues_shipping(self):
        with self.stub_upstream() as upstream:
            complete = self.complete_via_transition(self.main, self.client)
            upstream.assert_not_called()
            refund = self.refund(self.child)

        self.assertEqual(complete.json()["code"], 0, complete.json())
        self.assertEqual(refund.json()["code"], 0, refund.json())
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], TB_NO)

    def test_a_fully_refunded_group_does_not_ship(self):
        with self.stub_upstream() as upstream:
            first = self.refund(self.main)
            last = self.refund(self.child)

        self.assertEqual(first.json()["code"], 0, first.json())
        self.assertEqual(last.json()["code"], 0, last.json())
        upstream.assert_not_called()
        self.assertFalse(TaobaoOrderSendLog.objects.exists())

    def test_child_completing_last_ships_with_the_main_source_number(self):
        self.main.split_ship_main_order_no = self.main.order_no
        self.main.save(update_fields=["split_ship_main_order_no"])
        self.child.source_order_no = f"{TB_NO}-1"
        self.child.split_ship_main_order_no = self.main.order_no
        self.child.save(update_fields=["source_order_no", "split_ship_main_order_no"])
        self.main.source_order_no = f"{TB_NO}-7"
        self.main.save(update_fields=["source_order_no"])

        with self.stub_upstream() as upstream:
            first = self.complete_via_transition(self.main, self.client)
            upstream.assert_not_called()
            second = self.complete_via_transition(self.child, self.client)

        self.assertEqual(first.json()["code"], 0, first.json())
        self.assertEqual(second.json()["code"], 0, second.json())
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], f"{TB_NO}-7")
        sent = TaobaoOrderSendLog.objects.get()
        self.assertEqual((sent.order_no, sent.tb_order_no, sent.result), (
            self.main.order_no, f"{TB_NO}-7", SendResult.SUCCESS,
        ))

    def test_survivor_ships_original_number_after_original_main_is_deleted(self):
        from apps.business.recycle import soft_delete_order

        self.main.split_kind = self.child.split_kind = "PEER"
        self.main.split_ship_main_order_no = self.child.split_ship_main_order_no = self.main.order_no
        self.child.source_order_no = f"{TB_NO}-1"
        self.main.save(update_fields=["split_kind", "split_ship_main_order_no"])
        self.child.save(update_fields=["split_kind", "split_ship_main_order_no", "source_order_no"])
        soft_delete_order(order=self.main, actor=User.objects.get(username="sg-cs"), is_del_emp="N")
        self.child.refresh_from_db()
        self.assertEqual(self.child.split_id, "")

        with self.stub_upstream() as upstream:
            result = self.complete_via_transition(self.child, self.client)

        self.assertEqual(result.json()["code"], 0, result.json())
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], TB_NO)
        sent = TaobaoOrderSendLog.objects.get()
        self.assertEqual((sent.order_no, sent.tb_order_no), (self.child.order_no, TB_NO))

    def test_manual_retry_reuses_original_route_after_main_is_deleted(self):
        from apps.business.recycle import soft_delete_order

        self.main.split_kind = self.child.split_kind = "PEER"
        self.main.split_ship_main_order_no = self.child.split_ship_main_order_no = self.main.order_no
        self.child.source_order_no = f"{TB_NO}-1"
        self.child.order_source = None
        self.main.save(update_fields=["split_kind", "split_ship_main_order_no"])
        self.child.save(update_fields=["split_kind", "split_ship_main_order_no", "source_order_no", "order_source"])
        soft_delete_order(order=self.main, actor=User.objects.get(username="sg-cs"), is_del_emp="N")

        with self.stub_upstream() as upstream:
            upstream.side_effect = RuntimeError("合成失败")
            response = self.complete_via_transition(self.child, self.client)
        self.assertEqual(response.json()["code"], 0, response.json())
        upstream.assert_called_once()
        failed = TaobaoOrderSendLog.objects.get()
        self.assertEqual((failed.order_no, failed.tb_order_no, failed.result), (
            self.child.order_no, TB_NO, SendResult.FAILED,
        ))

        with self.stub_upstream() as upstream:
            result = xgj_ship.retry_row(failed, operator="复核客服")
        upstream.assert_called_once()
        self.assertEqual(upstream.call_args.args[0], TB_NO)
        self.assertEqual(result["result"], SendResult.SUCCESS)

    def test_survivor_does_not_reship_after_shipped_original_main_is_deleted(self):
        from apps.business.recycle import soft_delete_order

        self.main.split_kind = self.child.split_kind = "PEER"
        self.main.split_ship_main_order_no = self.child.split_ship_main_order_no = self.main.order_no
        self.child.source_order_no = f"{TB_NO}-1"
        self.main.save(update_fields=["split_kind", "split_ship_main_order_no"])
        self.child.save(update_fields=["split_kind", "split_ship_main_order_no", "source_order_no"])
        sent = TaobaoOrderSendLog.objects.create(
            order_no=self.main.order_no, tb_order_no=TB_NO,
            result=SendResult.SUCCESS, sent_at=timezone.now(),
        )
        soft_delete_order(order=self.main, actor=User.objects.get(username="sg-cs"), is_del_emp="N")
        self.child.refresh_from_db()
        self.assertEqual(self.child.split_id, "")

        with self.stub_upstream() as upstream:
            result = self.complete_via_transition(self.child, self.client)

        self.assertEqual(result.json()["code"], 0, result.json())
        upstream.assert_not_called()
        self.assertEqual(TaobaoOrderSendLog.objects.get().pk, sent.pk)


class BatchCompletionTests(_SplitShipFixture):
    """入口②：内网批量 `/orders/batch/transition`——同批两单只末跳发货（spec §6 勘误 3）。

    ⚠️ 批量是一个**大事务**（`order_batch.run_batch` 的 apply 分支）：前一张单在同一
    事务里已写成 COMPLETED ⇒ 末单过闸时判得到「除本单外全完成」⇒ 天然只发一次。
    """

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="sg-batch-cs", groups=("客服",),
            nodes=("order.change_status", "order.change_status_completed",
                   "order.list", "order.view_detail"),
        )

    def test_completing_both_members_in_one_batch_ships_only_on_the_last_hop(self):
        with self.stub_upstream() as upstream, self.inline_dispatch(), \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                "/api/business/orders/batch/transition/",
                {
                    "order_nos": [ORDER_NO_CHILD, ORDER_NO_MAIN],
                    "target": OrderStatus.COMPLETED,
                    "dry_run": False,
                },
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        self.main.refresh_from_db()
        self.child.refresh_from_db()
        self.assertEqual(self.main.status, OrderStatus.COMPLETED)   # 两单都放行（G5）
        self.assertEqual(self.child.status, OrderStatus.COMPLETED)
        upstream.assert_called_once()                               # 只末跳真发
        self.assertEqual(
            TaobaoOrderSendLog.objects.filter(result=SendResult.SUCCESS).count(), 1
        )
        # 先过闸的那一张留了「暂不发货 1/2」日志（历史如实，不撤回）
        self.assertEqual(self.hold_logs(ORDER_NO_CHILD).count(), 1)


class DomainFunnelHoldTests(_SplitShipFixture):
    """入口⑤/⑦：机器直调形态（代练通两步式 `_apply_local_status` / 修复命令 REWALK
    同为「直接调 `apply_transition`」的形状）——组未齐 ⇒ 完成放行、发货跳过。
    """

    def test_dailiantong_style_two_step_completion_is_not_blocked(self):
        with self.stub_upstream() as upstream:
            _apply_local_status(self.child, OrderStatus.COMPLETED)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, OrderStatus.COMPLETED)  # G5：机器路径也放行
        upstream.assert_not_called()
        self.assertEqual(self.hold_logs(self.child.order_no).count(), 1)

    def test_domain_apply_transition_for_the_last_member_ships_once(self):
        """域层末跳：闸放行 ⇒ on_commit 后真发一次（发货闸挂在发货侧的直证）。"""
        self.complete_raw(self.child)
        with self.stub_upstream() as upstream, self.inline_dispatch(), \
                self.captureOnCommitCallbacks(execute=True):
            self.complete_raw(self.main)
        upstream.assert_called_once()
        self.assertEqual(
            TaobaoOrderSendLog.objects.filter(order_no=ORDER_NO_MAIN).count(), 1
        )

    def complete_raw(self, order):
        apply_transition(order, OrderStatus.COMPLETED, nodes=COMPLETE_NODES)


class ReviewEndpointHoldTests(_SplitShipFixture):
    """入口③：`/review/` 审核通过（= 业务方的「验收」动作之一）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="sg-review-cs", groups=("客服",),
            nodes=("order.change_status_completed", "order.list", "order.view_detail"),
        )
        # ⚠️ `performance_review_needs_user` 检查约束：带 `review_status` 的行必须有归属人
        reviewer = get_user_model().objects.get(username="sg-review-cs")
        Performance.objects.create(
            order=self.child, emp_type=EmpType.MAIN, user=reviewer,
            review_status=ReviewStatus.PENDING,
            temp_amount=Decimal("50.00"),
        )

    def test_review_approval_passes_but_the_ship_is_held(self):
        perf = Performance.objects.get(order=self.child)
        with self.stub_upstream() as upstream, self.inline_dispatch(), \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"/api/business/performances/{perf.pk}/review/",
                {"approved": True, "comment": "夹具通过"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, OrderStatus.COMPLETED)  # G5：验收自由
        upstream.assert_not_called()
        self.assertEqual(self.hold_logs(self.child.order_no).count(), 1)


class OpenApiStatusHoldTests(_SplitShipFixture):
    """入口④：开放平台 `POST /api/open/v1/orders/<no>/status/`。"""

    APP_KEY = "sg-openapi-key-0001"
    APP_SECRET = "sg-openapi-secret-0000001"

    def setUp(self):
        super().setUp()
        creator, _ = User.objects.get_or_create(
            username="sg-openapi-creator", defaults={"password": "unused"}
        )
        self.app = ApiApp.objects.create(
            name="拆单发货闸探针应用", app_key=self.APP_KEY,
            app_secret=self.APP_SECRET, created_by=creator,
        )

    def test_openapi_completion_passes_but_the_ship_is_held(self):
        token = tokens.issue(self.app.app_key, self.app.app_secret)
        client = Client(SERVER_NAME="tenant.test.com")
        with self.stub_upstream() as upstream, self.inline_dispatch(), \
                self.captureOnCommitCallbacks(execute=True):
            response = client.generic(
                "POST",
                f"/api/open/v1/orders/{ORDER_NO_CHILD}/status/",
                data=json.dumps({"status": "COMPLETED", "remark": "夹具"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY="sg-openapi-key-0001",
            )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.child.refresh_from_db()
        self.assertEqual(self.child.status, OrderStatus.COMPLETED)
        upstream.assert_not_called()
        self.assertEqual(self.hold_logs(self.child.order_no).count(), 1)


class GroupIdempotencyTests(_SplitShipFixture):
    """**G7 组幂等**：组内 A 已发过货 ⇒ B 重开重完成只补日志、不重发。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="sg-idem-cs", groups=("客服",),
            nodes=("order.change_status", "order.change_status_completed",
                   "order.list", "order.view_detail"),
        )

    def _ship_the_child(self):
        TaobaoOrderSendLog.objects.create(
            order_no=ORDER_NO_CHILD, tb_order_no=TB_NO,
            result=SendResult.SUCCESS, sent_at=timezone.now(),
        )

    def test_a_recompletion_after_reopen_does_not_reship_for_the_group(self):
        self._ship_the_child()
        # 主单重开回待验收再完成（D-0147 的合法路径）
        self.main.status = OrderStatus.PENDING_REVIEW
        self.main.save(update_fields=["status"])
        with self.stub_upstream() as upstream:
            response = self.complete_via_transition(self.main, self.client)
        self.assertEqual(response.json()["code"], 0, response.json())
        upstream.assert_not_called()  # 组幂等：闸拦下，不排队列
        # 没有第二行发货记录；只补了一条「成功」订单日志（与 D7 同形）
        self.assertEqual(
            TaobaoOrderSendLog.objects.count(), 1,
            "组内已有 SUCCESS 行时不得新增发货记录",
        )
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.ship_result", order_no=ORDER_NO_MAIN
            ).count(),
            1,
        )
        self.assertEqual(self.hold_logs(ORDER_NO_MAIN).count(), 0)  # 不是「暂不发货」


class ConcurrentLastTwoTests(TransactionTestCase):
    """**并发末两单**（评审 S3 的行锁判据）：两事务交错 ⇒ 恰好一发、无死锁。

    形状：两线程同时把组内两单迁到 `COMPLETED`——两边的闸各自去取**组互斥锁**
    （组内 `id` 最小成员行）：先取到的判「对方未完成」（对方事务未提交）⇒ 留 hold
    日志并提交；后取到的在锁上等前者**提交后**才判 ⇒ 看到「全组已齐」⇒ 恰好它发一次。
    ⛔ 不会「双双跳发」也不会「双双跳过」，也不会 AB-BA 死锁
    （每个事务至多新取这一把锁、持锁者不再等别人——见
    `xgj_ship.schedule_auto_ship_gated` docstring 的锁形论证）。

    ⚠️ **为什么必须 `TransactionTestCase` + 每线程 `schema_context`**：`TestCase`
    把每条用例包在事务里、其他线程看不到未提交夹具；线程各有独立连接、默认落
    `public` schema。`django_tenants` 没有 `TenantTransactionTestCase`
    （读码：`django_tenants/test/cases.py`）⇒ 租户夹具照
    `tests/test_order_screenshots_api.py::OrderScreenshotLimitConcurrencyTests`
    的先例在 `setUpClass` 自建。
    """

    TENANT_SCHEMA = "test"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=cls.TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="tenant.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_two_concurrent_final_completions_ship_exactly_once(self):
        ships = []
        ships_lock = threading.Lock()
        errors = []

        game = Game.objects.create(name="拆单并发夹具游戏")
        main = Order.objects.create(
            game=game, order_no=ORDER_NO_MAIN, status=OrderStatus.PENDING_REVIEW,
            split_id=GROUP,
        )
        child = Order.objects.create(
            game=game, order_no=ORDER_NO_CHILD, status=OrderStatus.PENDING_REVIEW,
            split_id=GROUP,
        )

        def work(order_pk):
            try:
                connections.close_all()
                with schema_context(self.TENANT_SCHEMA):
                    with transaction.atomic():
                        fresh = Order.objects.get(pk=order_pk)
                        apply_transition(
                            fresh, OrderStatus.COMPLETED, nodes=COMPLETE_NODES
                        )
            except Exception as exc:  # noqa: BLE001 —— 测试收集线程内异常
                errors.append(exc)
            finally:
                connections.close_all()

        def recording_dispatch(order_no):
            with ships_lock:
                ships.append(order_no)

        with mock.patch.object(
            xgj_ship, "dispatch_auto_ship", side_effect=recording_dispatch
        ):
            thread_a = threading.Thread(target=work, args=(child.pk,))
            thread_b = threading.Thread(target=work, args=(main.pk,))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=30)
            thread_b.join(timeout=30)

        self.assertEqual(errors, [])
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(main.status, OrderStatus.COMPLETED)
        self.assertEqual(child.status, OrderStatus.COMPLETED)
        # 恰好一发：两次完成只有一次排下发货（另一张留 hold 日志）
        self.assertEqual(ships, [ORDER_NO_MAIN])
        hold_nos = set(
            OperationLog.objects.filter(action=HOLD_ACTION).values_list(
                "order_no", flat=True
            )
        )
        self.assertEqual(len(hold_nos), 1)
        self.assertLessEqual(hold_nos, {ORDER_NO_MAIN, ORDER_NO_CHILD})


class UpstreamSourceSkipTests(_SplitShipFixture):
    """上家来源单不自动发货（业务方 2026-11-09 拍板）：闸顶零痕迹跳过、普通单照旧。

    两条上家链路（上家平台线 / 团团通上家线）各一条用例 + 一条对照（无上家链路
    ⇒ 照旧排队）。「不排队」断言打在 `dispatch_auto_ship` 替身上（替身只记录单号；
    真 worker 线程会在断言窗口外异步消费队列，替身让判据确定）。零痕迹 = 队列空 +
    无发货记录行 + 无订单日志行。
    """

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="sg-up-cs", groups=("客服",),
            nodes=("order.change_status", "order.change_status_completed",
                   "order.list", "order.view_detail"),
        )
        self.dispatched = []
        patcher = mock.patch.object(
            xgj_ship, "dispatch_auto_ship",
            side_effect=lambda order_no: self.dispatched.append(order_no),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 对照夹具：非拆单、无上家链路的普通单。
        self.plain = Order.objects.create(
            game=self.game, order_no="20261101000000000403",
            status=OrderStatus.PENDING_REVIEW,
            order_source=self.source, source_order_no="22000000000000000403",
            total_price=Decimal("120.00"),
        )

    def complete(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"/api/business/orders/{order.order_no}/transition/",
                {"target": OrderStatus.COMPLETED},
                content_type="application/json",
            )

    def _assert_silent_skip(self, order):
        self.assertEqual(self.complete(order).status_code, 200)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(
            TaobaoOrderSendLog.objects.filter(order_no=order.order_no).count(), 0
        )
        # 「零痕迹」只针对发货类留痕：完成动作本身的 order.transition 审计行照旧写。
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=order.order_no,
                action__in=(xgj_ship.SHIP_RESULT_ACTION, HOLD_ACTION),
            ).count(),
            0,
        )
        self.assertEqual(len(xgj_ship._ship_queue), 0)

    def test_jidanpu_upstream_link_order_is_silently_skipped(self):
        binding = JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username="sg-up-cs"),
            jidanpu_base_url="https://upstream.example.com", account="up-acc",
            password="up-pass",
        )
        JidanpuUpstreamOrderLink.objects.create(
            binding=binding, order=self.plain, upstream_order_id="UP-0001",
        )
        self._assert_silent_skip(self.plain)

    def test_peer_upstream_link_order_is_silently_skipped(self):
        upstream = PeerUpstreamBinding.objects.create(
            peer_name="对端甲", peer_base_url="https://peer.example.com",
            peer_account="peer-acc", token_in_hash="a" * 32, token_out="b" * 32,
            local_base_url="https://me.example.com",
            bound_by=User.objects.get(username="sg-up-cs"),
        )
        PeerOrderLink.objects.create(order=self.plain, upstream_binding=upstream)
        self._assert_silent_skip(self.plain)

    def test_plain_order_is_still_queued(self):
        """对照：无上家链路的普通单完成 ⇒ 照旧排队（现状不变）。"""
        self.assertEqual(self.complete(self.plain).status_code, 200)
        self.assertEqual(self.dispatched, [self.plain.order_no])
