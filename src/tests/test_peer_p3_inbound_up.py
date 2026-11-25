"""上下游 P3 Task 6：**上行接收侧**（上家收下家的状态回传）。

本文件钉四件事（任务书 `task-6-brief.md` + 预检裁定 R4）：

① **状态白名单**：上家**只接受** `UPSTREAM_STATUS_MAP` 的值域（`PENDING_REVIEW` /
   `ISSUE` / `PAUSED`）——下家在**发送前**就把自己的状态映射完了（`COMPLETED` 也映成
   `PENDING_REVIEW`）。`REFUNDED` 从不由下家上行 ⇒ 收到即硬跳过 `unknown_status`，
   ⛔ 不回落、⛔ 不给它另造一个 code。
② **安全判据（承重）**：一条上行事件只能改「**当前 MAIN 接手人 = 这条下游绑定的账号**」
   的那张单；对端拿 `target_ref` 指认别的单 ⇒ 硬跳过 `not_your_order`（本任务新增的 code）。
③ **冲突裁决 = 最后更改者胜**：上家侧没有 `PeerOrderLink` ⇒ 本地动作时刻用
   `order.updated_at` 作**代理**；`occurred_at <= order.updated_at` ⇒ 良性 `stale`。
   ⚠️ 本文件同时钉住「应用之后 `updated_at` **真的前进了**」——否则「最后更改者胜」会把
   自己判死（下一条回传永远被判 stale）。
④ **只改状态**：⛔ 不建单、⛔ 不碰价格 / 备注 / 游戏 / 归属、⛔ 不写订单日志、⛔ 不走
   `apply_transition`；端点层还要求 ⛔ **不写** `PeerDownstreamBinding` 的四个健康列
   （`last_ok_at` / `last_error` / `last_error_at` / `consecutive_failures`）——那是
   **发送方的账**（「我推给下家的东西成没成」），而 `peer_worker._record_failure` 正拿
   `consecutive_failures` 数到 10 去**自动停绑**。把「下家推来的东西我处理不了」也累加到
   同一个计数器上，会让一个推得挺正常的下家被停掉。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
⚠️ 夹具值**全部是合成值**（`SYN-UP-*` / `测试*` / `*.test.invalid`）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from queue import Queue
from time import monotonic, sleep
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_model, tenant_context

from apps.audit.models import OperationLog
from apps.business.api import peer_views
from apps.business.models import (
    AlipaySettlementAuthorization,
    EmpType,
    Game,
    OnlinePayrollBatch,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    Performance,
    SettleStatus,
)
from apps.business.peer_inbound import (
    SKIP_BAD_SHAPE,
    SKIP_NOT_YOUR_ORDER,
    SKIP_STALE,
    SKIP_UNKNOWN_ORDER,
    SKIP_UNKNOWN_STATUS,
    _upstream_accepted,
    apply_upstream_events,
)
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import _hard_skip_reason
from apps.business.roles import TENANT_ADMIN
from apps.business.settlement import SettleError, settle_performances

#: 接手人 = 这条下游绑定账号的那张单（安全判据**通过**）。
UP_ORDER_NO = "SYN-UP-P3-1"
#: 接手人是**别人**的那张单（安全判据必须拦住）。
OTHER_ORDER_NO = "SYN-UP-P3-2"
#: 端到端那一层要带的租户域名（`TenantTestCase` 自带租户的域名，与 `test_peer_order_push` 同源）。
PEER_DOMAIN = "tenant.test.com"
INBOUND_URL = "/api/business/peer-sync/inbound/events/"
GAME_NAME = "测试上行游戏"


def _now_plus(seconds):
    """相对「现在」往后 `seconds` 秒的 aware 时刻（事件级 `occurred_at` 夹具）。"""
    return timezone.now() + timedelta(seconds=seconds)


def _now_minus(seconds):
    """相对「现在」往前 `seconds` 秒的 aware 时刻（造「比本地动作旧」的回传）。"""
    return timezone.now() - timedelta(seconds=seconds)


class TestPeerBase(TenantTestCase):
    """共用夹具：一条生效的下游绑定 + 两张单（MAIN 接手人一个匹配、一个不匹配）。

    - `self.downstream_binding`：我方作为**上家**的那条绑定，`account` 是
      `self.downstream_account`（下家拿这个账号绑我）；
    - `self.upstream_order`：MAIN 接手人 **=** `downstream_binding.account`
      ⇒ 安全判据通过，状态回传能落地；
    - `self.other_order`：MAIN 接手人是 `self.other_taker`（别人）
      ⇒ 安全判据必须拦住（`not_your_order`）。

    ⚠️ `PeerDownstreamBinding` 的 `account` / `peer_base_url` / `token_in_hash` /
    `token_out` 全部非空 ⇒ 夹具按字段形状给全（少给一个就 `IntegrityError`）。
    """

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)
        self.downstream_account = get_user_model().objects.create_user(
            username="syn-up-p3-account", password="pw-syn-up-p3"
        )
        self.other_taker = get_user_model().objects.create_user(
            username="syn-up-p3-other", password="pw-syn-up-p3"
        )
        self.raw_downstream_token = new_token()
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.downstream_account,
            peer_base_url="https://peer-down.test.invalid",
            # 对方向我带的那套只存哈希；我发给对方的是明文——按模型字段口径给。
            token_in_hash=hash_token(self.raw_downstream_token),
            token_out=new_token(),
        )
        self.upstream_order = self._local_order(
            UP_ORDER_NO, taker=self.downstream_account
        )
        self.other_order = self._local_order(OTHER_ORDER_NO, taker=self.other_taker)

    def _local_order(self, number, *, taker, status=OrderStatus.IN_PROGRESS):
        """一张本地单 + 它的 MAIN 绩效行（`taker` 就是「当前接手人」）。"""
        order = Order.objects.create(
            order_no=number,
            game=self.game,
            status=status,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
            remark="本地备注",
        )
        Performance.objects.create(order=order, user=taker, emp_type=EmpType.MAIN)
        return order

    def _touch_local(self, order):
        """把 `order.updated_at` 推到「现在」，模拟**本地刚有人动过这张单**。

        ⚠️ `Order.updated_at` 是 `auto_now=True`：`pre_save` 只对 `update_fields` 里列出的
        字段跑 ⇒ **必须显式列它**，否则这次 `save()` 一个字都不改。
        """
        order.save(update_fields=["updated_at"])
        order.refresh_from_db()
        return order.updated_at


class UpstreamInboundTests(TestPeerBase):
    """`apply_upstream_events` 的状态白名单 / 安全判据 / 陈旧裁决 / 只改状态。"""

    def _push(self, status, occurred_at=None):
        return apply_upstream_events([{
            "kind": "order.update", "target_ref": self.upstream_order.order_no,
            "occurred_at": (occurred_at or _now_plus(5)).isoformat(),
            "payload": {"status": status},
        }], binding=self.downstream_binding)

    # ── ① 状态白名单 ──────────────────────────────────────────────────────
    def test_pending_review_lands_on_the_upstream_order(self):
        outcome = self._push("PENDING_REVIEW")
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "PENDING_REVIEW")
        self.assertEqual(outcome.applied, 1, outcome)

    def test_downstream_status_keeps_a_paid_order_completed(self):
        order = self.upstream_order
        order.status = OrderStatus.COMPLETED
        order.completed_at = timezone.now()
        order.save(update_fields=["status", "completed_at", "updated_at"])
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        perf.settle_status = SettleStatus.SETTLED
        perf.settled_at = timezone.now()
        perf.settled_amount = Decimal("25.00")
        perf.save(update_fields=["settle_status", "settled_at", "settled_amount"])
        order_fields = (order.updated_at, order.completed_at, order.submitted_at)
        logs = OperationLog.objects.filter(order_no=order.order_no).count()

        with (
            mock.patch("apps.openapi.dispatch.notify_order_status_changed") as notify_openapi,
            mock.patch("apps.takerapi.api.callback_recipients.notify_order_status_changed_for_taker") as notify_taker,
        ):
            for target in (OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE, OrderStatus.PAUSED):
                with self.subTest(target=target):
                    outcome = self._push(target)
                    self.assertEqual((outcome.applied, outcome.skipped), (0, 1))
                    self.assertIsNone(_hard_skip_reason({"skipped_details": outcome.skipped_details}, order.order_no))
                    self.assertIn("已结账", outcome.skipped_details[0]["reason"])
            notify_openapi.assert_not_called()
            notify_taker.assert_not_called()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual((order.updated_at, order.completed_at, order.submitted_at), order_fields)
        self.assertEqual(OperationLog.objects.filter(order_no=order.order_no).count(), logs)
        perf.refresh_from_db()
        self.assertEqual((perf.settle_status, perf.settled_amount), (SettleStatus.SETTLED, Decimal("25.00")))

    def test_settled_sales_amount_alone_is_a_settlement_trace(self):
        order = self.upstream_order
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status", "updated_at"])
        Performance.objects.create(
            order=order, user=self.other_taker, emp_type=EmpType.SALES, settled_amount=Decimal("0.00"),
        )

        outcome = self._push(OrderStatus.PENDING_REVIEW)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(outcome.skipped_details[0]["code"], SKIP_STALE)

    def test_in_progress_online_payroll_reservation_keeps_order_completed(self):
        order = self.upstream_order
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status", "updated_at"])
        payer = AlipaySettlementAuthorization.objects.create(request_id=uuid4())
        batch = OnlinePayrollBatch.objects.create(
            request_id=uuid4(), category="taker", authorization=payer, created_by=self.other_taker,
        )
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(online_payroll_batch=batch)

        outcome = self._push(OrderStatus.PENDING_REVIEW)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(outcome.applied, 0)
        self.assertIn("正在在线结算", outcome.skipped_details[0]["reason"])

    def test_completed_is_mapped_to_pending_review_by_the_sender(self):
        """映射在**发送侧**做；接收侧只收映射后的值 ⇒ 收到 `PENDING_REVIEW`。"""
        self._push("PENDING_REVIEW")
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "PENDING_REVIEW")
        self.assertNotEqual(self.upstream_order.status, OrderStatus.COMPLETED)

    def test_a_status_this_side_does_not_accept_is_hard_skipped(self):
        """⛔ 下家没有「退单上行」这条裁定 ⇒ `REFUNDED` 硬跳过，本单一个字不动。"""
        outcome = self._push("REFUNDED")
        self.upstream_order.refresh_from_db()
        self.assertNotEqual(self.upstream_order.status, "REFUNDED")
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_UNKNOWN_STATUS for d in outcome.skipped_details),
            outcome,
        )

    def test_the_accepted_set_is_exactly_the_sender_map_value_range(self):
        """上行接受集 = `UPSTREAM_STATUS_MAP` 的**值域**（独立契约钉子，⛔ 不是自指对拍）。

        判别力：`_upstream_accepted()` 若改成 `OrderStatus.values`（或另抄一份漂了），
        下面的字面量断言立刻红——那正是「多接受一个 `COMPLETED` / 漏掉 `PAUSED`」的形状。
        """
        self.assertEqual(
            set(_upstream_accepted()), {"PENDING_REVIEW", "ISSUE", "PAUSED"}
        )
        self.assertNotIn("REFUNDED", _upstream_accepted())

    # ── ② 安全判据（承重） ────────────────────────────────────────────────
    def test_an_order_whose_taker_is_not_the_binding_account_is_rejected(self):
        """对端用 `target_ref` 指认**别人的单** ⇒ `not_your_order`，一个字不改。"""
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": self.other_order.order_no,
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_NOT_YOUR_ORDER for d in outcome.skipped_details),
            outcome,
        )
        self.other_order.refresh_from_db()
        self.assertEqual(self.other_order.status, OrderStatus.IN_PROGRESS)

    def test_an_order_with_no_main_taker_is_not_yours(self):
        """MAIN 绩效行缺失（还没人接手）⇒ 同样 `not_your_order`（⛔ 不许默认放行）。"""
        orphan = Order.objects.create(
            order_no="SYN-UP-P3-3", game=self.game, status=OrderStatus.UNASSIGNED
        )
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": orphan.order_no,
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_NOT_YOUR_ORDER for d in outcome.skipped_details),
            outcome,
        )
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, OrderStatus.UNASSIGNED)

    # ── ③ 陈旧裁决 / updated_at 代理 ──────────────────────────────────────
    def test_an_older_echo_does_not_overwrite_a_newer_local_change(self):
        """最后更改者胜：下家事件比上家本地动作旧 ⇒ 不改（`stale`）。"""
        self._touch_local(self.upstream_order)
        outcome = self._push("PENDING_REVIEW", occurred_at=_now_minus(600))
        self.upstream_order.refresh_from_db()
        self.assertNotEqual(self.upstream_order.status, "PENDING_REVIEW")
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertTrue(
            any(d["code"] == SKIP_STALE for d in outcome.skipped_details), outcome
        )

    def test_an_equal_timestamp_is_a_benign_stale_skip(self):
        """相等 = 重投 ⇒ `stale`（⛔ 不许判成"应用"，否则重投会被当成新的本地动作）。"""
        self._touch_local(self.upstream_order)
        outcome = self._push(
            "PENDING_REVIEW", occurred_at=self.upstream_order.updated_at
        )
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)
        self.assertTrue(
            any(d["code"] == SKIP_STALE for d in outcome.skipped_details), outcome
        )

    def test_the_local_updated_at_advances_so_the_next_echo_is_not_stale(self):
        """⭐ 应用之后 `updated_at` 必须**前进**（`auto_now` 也要显式列进 `update_fields`）。

        判别力：把 `save(update_fields=["status"])`（漏掉 `updated_at`）⇒ 第一次断言
        `assertGreater` 立刻红——而那是「最后更改者胜」把自己判死的形状：库里那一列还停在
        夹具创建时刻，第二条回传本可落地却会被判 `stale`。
        """
        before = self.upstream_order.updated_at
        self._push("ISSUE")
        self.upstream_order.refresh_from_db()
        self.assertGreater(self.upstream_order.updated_at, before)

        # 紧跟的第二条回传仍能落地（证明上面那一跳真的推进了本地动作代理时刻）。
        self._push("PAUSED")
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "PAUSED")

    # ── ④ 只改状态 / 形状跳过 ─────────────────────────────────────────────
    def test_the_echo_changes_status_only(self):
        """⛔ 不建单、⛔ 不碰价格 / 备注 / 游戏 / 归属；⭐ 落一条 `peer.upstream_status` 日志。

        ⛔ **改准**：本用例原名 `...changes_status_only`、原断言是
        「`OperationLog` 一行都不写」。业务方明确要求补上「下家什么时候
        改了什么状态」⇒ 落地成功现在**恰好写一条** `peer.upstream_status`
        （见 `apps/business/peer_inbound.py::_record_peer_sync`）；「只改状态」这半句
        不变——日志是留痕，不是第二个写入面。
        """
        logs_before = OperationLog.objects.count()
        before = (
            self.upstream_order.total_price,
            self.upstream_order.hall_price,
            self.upstream_order.remark,
            self.upstream_order.game_id,
        )
        orders_before = Order.objects.count()

        self._push("ISSUE")

        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "ISSUE")
        self.assertEqual(
            (
                self.upstream_order.total_price,
                self.upstream_order.hall_price,
                self.upstream_order.remark,
                self.upstream_order.game_id,
            ),
            before,
        )
        self.assertEqual(Order.objects.count(), orders_before)
        # ⭐ 恰好 +1 行，且是那条状态回传日志（⛔ 不是「随便写了一行」）。
        self.assertEqual(OperationLog.objects.count(), logs_before + 1)
        row = OperationLog.objects.filter(order_no=UP_ORDER_NO).get()
        self.assertEqual(row.action, "peer.upstream_status")
        self.assertEqual(row.log_type, 40)                       # 系统相关（peer.* 一族）
        self.assertIsNone(row.actor_id)                          # 机器来源
        self.assertEqual(row.actor, "syn-up-p3-account")         # 绑定账号显示名（无昵称 ⇒ 登录账号）
        self.assertEqual(row.to_value, "问题单")                  # `OrderStatus.ISSUE.label`

    def test_an_unknown_order_is_a_hard_skip(self):
        """回传指向一张本地没有的单 ⇒ `unknown_order`（上家侧不建单）。"""
        orders_before = Order.objects.count()
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": "本地没有的单号",
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_UNKNOWN_ORDER for d in outcome.skipped_details),
            outcome,
        )
        self.assertEqual(Order.objects.count(), orders_before)

    def test_a_missing_time_is_a_bad_shape_skip(self):
        """没有可用的 `occurred_at` ⇒ `bad_shape`（⛔ 不许当成"很旧"静默丢弃）。"""
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": self.upstream_order.order_no,
            "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, OrderStatus.IN_PROGRESS)

    def test_a_non_object_event_is_a_bad_shape_skip_not_a_500(self):
        """`events` 是外部来的数组 ⇒ 非对象项跳过（⛔ 不是 500），`target_ref` 给空串。"""
        outcome = apply_upstream_events(["nope", 7], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(outcome.skipped, 2, outcome)
        for detail in outcome.skipped_details:
            self.assertEqual(detail["code"], SKIP_BAD_SHAPE)
            self.assertEqual(detail["target_ref"], "")

    # ── ⑤ 同步身份 = **来源单号**（计划 `2026-10-25-peer-source-identity` §2 第 1 条尾）──
    def test_a_source_order_no_ref_resolves_the_local_order(self):
        """新口径：下家回传的 `target_ref` 是**我方这张单的来源单号**（⛔ 不是内置号）。

        判别力：来源号与内置号**取值不同** ⇒ 仍按 `Order.order_no` 单键查的实现会
        `unknown_order`，本用例红。
        """
        self.upstream_order.source_order_no = "SYN-UP-P3-SRC-1"
        self.upstream_order.save(update_fields=["source_order_no"])
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": "SYN-UP-P3-SRC-1",
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "ISSUE")
        self.assertNotEqual("SYN-UP-P3-SRC-1", self.upstream_order.order_no)

    def test_the_legacy_builtin_ref_still_resolves_after_the_change(self):
        """兼容：老下家仍发**内置号** ⇒ 照旧命中（`order_no` 那一档优先，行为不变）。"""
        self.upstream_order.source_order_no = "SYN-UP-P3-SRC-2"
        self.upstream_order.save(update_fields=["source_order_no"])
        outcome = apply_upstream_events([{
            "kind": "order.update", "target_ref": self.upstream_order.order_no,
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "ISSUE")


class UpstreamSettlementConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        connection.set_schema_to_public()
        cls.tenant = get_tenant_model()(schema_name="peer_settle_lock")
        cls.tenant.save(verbosity=0)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        connection.set_tenant(self.tenant)
        TestPeerBase.setUp(self)

    _local_order = TestPeerBase._local_order
    _push = UpstreamInboundTests._push

    def _run_on_other_connection(self, call, pids):
        try:
            with tenant_context(self.tenant):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pids.put(cursor.fetchone()[0])
                return call()
        finally:
            connections.close_all()

    def _wait_for_blocked_request(self, pids):
        pid = pids.get(timeout=10)
        deadline = monotonic() + 10
        with connection.cursor() as cursor:
            while monotonic() < deadline:
                cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
                if cursor.fetchone()[0]:
                    return
                sleep(0.01)
        self.fail("另一条连接没有被当前操作锁住")

    def test_settlement_checks_status_again_after_the_inbound_update_commits(self):
        order = self.upstream_order
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status", "updated_at"])
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        cashier = get_user_model().objects.create_user(username="peer-settle-finance")
        cashier.groups.add(Group.objects.get_or_create(name=TENANT_ADMIN)[0])
        pids = Queue()

        def settle():
            settle_performances([perf.pk], actor=cashier.username, user=cashier)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                self.assertEqual(self._push(OrderStatus.PENDING_REVIEW).applied, 1)
                future = pool.submit(self._run_on_other_connection, settle, pids)
                self._wait_for_blocked_request(pids)
            with self.assertRaises(SettleError):
                future.result(timeout=10)

        order.refresh_from_db()
        perf.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)
        self.assertFalse(perf.is_settled)


class UpstreamInboundEndpointTests(TestPeerBase):
    """端点层：下家令牌走**上行支**落地，且 ⛔ 不写发送方的那本健康账。"""

    def _post(self, events):
        return Client(SERVER_NAME=PEER_DOMAIN).post(
            INBOUND_URL, {"events": events}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_downstream_token,
        )

    def test_the_downstream_token_applies_a_status_echo(self):
        resp = self._post([{
            "kind": "order.update", "target_ref": self.upstream_order.order_no,
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)
        self.upstream_order.refresh_from_db()
        self.assertEqual(self.upstream_order.status, "ISSUE")
        # ⛔ 成功也不写发送方的账：上行支只回 `skipped_details`。
        self.downstream_binding.refresh_from_db()
        self.assertIsNone(self.downstream_binding.last_ok_at)
        self.assertEqual(self.downstream_binding.consecutive_failures, 0)

    def test_an_oversized_batch_is_rejected_as_a_business_error(self):
        """⭐ C4（L-2，2026-10-25 审计批）：`events` 超过硬上限 ⇒ 业务信封拒收，⛔ 不进事务。

        上限 = `peer_sync.PEER_EVENTS_MAX_BATCH`（= 100：发送端一次 HTTP 只投一条，
        上限取 `max(2×发送批, 100)`）。拒在**批量检查**这一层 ⇒ 一条都不应用、一行台账都不落。
        ⚠️ 用「边界 + 1」探针：100 条照常、101 条被拒 —— 只测超大值（如 10 万）判不出
        「上限恰好在这里」。
        """
        from apps.business.peer_sync import PEER_EVENTS_MAX_BATCH

        junk = [{
            "kind": "order.update", "target_ref": f"SYN-C4-OVER-{i}",
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        } for i in range(PEER_EVENTS_MAX_BATCH + 1)]
        resp = self._post(junk)
        body = resp.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("分批", body["msg"])
        # ⛔ 一条都没应用（拒收发生在任何写入之前）。
        self.upstream_order.refresh_from_db()
        self.assertNotEqual(self.upstream_order.status, "ISSUE")

    def test_a_batch_at_the_cap_is_still_accepted(self):
        """⭐ C4 边界正向：**恰好** 100 条不许误杀（否则正常批量被一刀切）。

        ⚠️ 100 条都是「本地没有的单号」⇒ 每条走 `unknown_order` 硬跳（零业务写入），
        用它把**批量通道**走到头而不拖慢用例。
        """
        from apps.business.peer_sync import PEER_EVENTS_MAX_BATCH

        junk = [{
            "kind": "order.update", "target_ref": f"SYN-C4-CAP-{i}",
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        } for i in range(PEER_EVENTS_MAX_BATCH)]
        resp = self._post(junk)
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 0, body)
        self.assertEqual(body["data"]["skipped"], PEER_EVENTS_MAX_BATCH)

    def test_a_junk_echo_does_not_touch_the_senders_health_ledger(self):
        """⛔ 上行支**不写** `PeerDownstreamBinding` 的四个健康列（那是发送方的账）。

        判别力：把上行支改成也调 `_record_inbound_health(binding, outcome)`，
        `consecutive_failures` 会从 0 变 1、`last_error` 会被写上 ⇒ 本用例红。
        `peer_worker._close_round` 正拿 `consecutive_failures` 数到 10 去**自动停绑**
        ⇒ 这条判据守的是「别让一个推单正常的下家因为回传了几条废事件被停掉」。
        """
        resp = self._post([{
            "kind": "order.update", "target_ref": "本地没有的单号",
            "occurred_at": _now_plus(5).isoformat(), "payload": {"status": "ISSUE"},
        }])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(
            any(
                d["code"] == SKIP_UNKNOWN_ORDER
                for d in body["data"]["skipped_details"]
            ),
            body,
        )
        self.downstream_binding.refresh_from_db()
        self.assertEqual(self.downstream_binding.consecutive_failures, 0)
        self.assertEqual(self.downstream_binding.last_error, "")
        self.assertIsNone(self.downstream_binding.last_error_at)
        self.assertIsNone(self.downstream_binding.last_ok_at)
