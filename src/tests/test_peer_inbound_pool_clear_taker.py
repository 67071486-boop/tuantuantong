"""上游「移除接手人 / 重新上架」把已建链订单打回未分配时，我方接手人必须自动清除
（D-0343，2026-10-30 涉事租户生产实锤：涉事单被上家 `order.assign → 未分配` 打回池内、
23:23:12 人工指派的接手人原样挂着 ⇒ 不能重新指派 / 抛厅 / 被抢，直到人工移除）。

根因（读码）：`peer_inbound.apply_events` 的已建链 `order.assign` / `order.grab` 支按
P4 口径（业务方「接手类事件 ⇒ 对方拿到的是未分配的单」）**直写**
`order.status = UNASSIGNED`——绕开了全仓唯一状态写入口 `apply_transition` ⇒
注册在其上的回池钩子 `performance._on_pool_entry`（**D-0032**：迁入池内两态 ⇒
`reset_taker_performance` 清接手人，「进池 ⇔ 无人」）从不触发。

修法口径（与人工 `/clear-taker` / 状态机回池逐字同族，见各用例）：
- 回池档（非 promoted、非 claiming）：同事务 `reset_taker_performance` + 客服预计刷新
  + `order.clear_taker` 审计行（生产投诉「订单日志无清除记录」）+ 下行 `order.withdraw
  (taker_removed)`（钩子同形）+ 打手侧回调（钩子同形）+ 鸡蛋铺下家副本退
  （`queue_withdraw`，人工路径同形——**先清人再调**，前接手人自己的店不再被排除）；
- **已结账挡住**（D4b / D-0156 同一口径：已结账不动钱）：不清，原因点名在
  `peer.inbound_assign` 审计行的 reason 里；
- `promoted_hall`（抢单已转正）/ `claiming`（转正确认在途）两档维持既有口径：
  落「代练中」、接手人不动（`_promote_claiming_mirror` 的交单语义）。

⚠️ 必须用 `TenantTestCase`（`apps.business` / `apps.audit` 在 `TENANT_APPS`）。
⚠️ 夹具一律合成值（`测试打手乙` / `peer-account-a` / `https://peer-a.test.invalid`）。

评审第 5 轮追加：
- **F-1（必修）**：清人排出的下行 `order.withdraw (taker_removed)` 事件必须带
  **新鲜**时刻（显式 `occurred_at=timezone.now()`）——本支 `save(update_fields)`
  不含 `updated_at` ⇒ 默认值 = 过期的上一次写入 ⇒ 多跳形态（接手人 = 下家绑定
  账号）下家按 stale「相等即丢」静默跳过 ⇒ 下家的接手人照样挂着（E-0307 传一跳）。
- **R-1**：`queue_withdraw` 排清人**之后**的承重顺序（B4：前接手人自己那家店的
  副本也退）用「绑定 user = 接手人本人」的夹具钉住（错序 ⇒ 红）。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone as dj_timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
    PerformanceSettleRequest,
)
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token

#: 本文件统一的"对端单号"常量（去重键是 `(绑定, 对端单号)`）。
PEER_NO = "A-1"
#: 本地订单号（形状照 `src/apps/business/order_no.py` 的产物，值本身无意义）。
LOCAL_NO = "20261030000000000301"
#: 下家鸡蛋铺地址（合成）。
MEMBER_URL = "https://member-a.test.invalid"


class PoolEntryClearsTakerTests(TenantTestCase):
    """`order.assign` / `order.grab` 打回未分配 ⇒ 接手人自动清除（D-0343）。"""

    #: 两个时刻：`T1` 是本地已应用到的时刻，`T2` 严格晚于它（事件才会被应用）。
    T1 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
    T2 = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="回池清人夹具游戏")
        self.taker = get_user_model().objects.create_user(
            username="peer-pool-taker", password="pw-taker"
        )
        self.kefu = get_user_model().objects.create_user(
            username="peer-pool-kefu", password="pw-kefu"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("10.00"),
            hall_price=Decimal("10.00"),
            sales_user=self.kefu,
            source_order_no=PEER_NO,
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no=PEER_NO,
            last_applied_at=self.T1,
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def _main_row(self, *, user=None, **over):
        """MAIN 绩效行（接手人挂单的载体——接手人**只**写在这里）。"""
        fields = dict(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=user or self.taker,
            claimed_at=self.T1,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("3.00"),
            taker_price=Decimal("9.00"),
            hours=Decimal("2.00"),
            project="合成接手项目",
        )
        fields.update(over)
        return Performance.objects.create(**fields)

    def _assign_event(self, *, occurred_at=None, kind="order.assign"):
        """一条已建链的接手类事件（P4：落地即「未分配」）。"""
        payload = {
            "status": OrderStatus.IN_PROGRESS,
            "total_price": "10.00",
            "hall_price": "10.00",
            "remark": "备",
            "project_text": "项",
            "customer_phone": "",
            "customer_qq": "",
            "content": "需",
            "game_name": self.game.name,
        }
        event = {"kind": kind, "target_ref": PEER_NO, "payload": payload}
        if occurred_at is not None:
            event["occurred_at"] = occurred_at.isoformat()
        return event

    def _apply(self):
        return apply_events(
            [self._assign_event(occurred_at=self.T2)], binding=self.binding
        )

    def _member_binding(self, *, username="peer-pool-member", url=MEMBER_URL):
        user = get_user_model().objects.create_user(username=username, password="pw-m")
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=url,
            account=f"acc-{username}",
            password="pw-m",
            enabled=True,
        )

    # ── ① 回池 ⇒ 清人（修复前红） ────────────────────────────────────────
    def test_assign_back_to_pool_clears_the_taker(self):
        """上游 assign 打回未分配 ⇒ MAIN 行的接手人三元组 + 归属列全部回退。"""
        self._main_row()
        result = self._apply()
        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)          # 接手人
        self.assertIsNone(perf.review_status)    # 审核态（撞约束的那一格）
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIsNone(perf.taker_price)      # 「这个人的」覆盖价
        self.assertIsNone(perf.claimed_at)       # 「这个人何时接的」
        self.assertIsNone(perf.hours)            # 「这个人的」时间要求
        self.assertEqual(perf.project, "")       # 「这个人的」接手项目
        # 资金面：预计绩效**不动**（与人工 /clear-taker 的 N 档逐字同形）。
        self.assertEqual(perf.temp_amount, Decimal("10.00"))

    def test_clearing_cancels_pending_settle_requests(self):
        """清人 ⇒ 该行 `pending` 的提前结算申请作废（reset_taker_performance 既有口径）。"""
        self._main_row()
        request = PerformanceSettleRequest.objects.create(
            performance=Performance.objects.get(
                order=self.order, emp_type=EmpType.MAIN
            ),
            user=self.taker,
        )
        self._apply()
        request.refresh_from_db()
        self.assertEqual(
            request.state, PerformanceSettleRequest.State.CANCELLED
        )

    # ── ② 订单日志要有清除记录（生产投诉原话「订单日志无清除记录」） ────────
    def test_the_clear_writes_the_audit_row(self):
        self._main_row()
        self._apply()
        row = OperationLog.objects.filter(
            order_no=LOCAL_NO, action="order.clear_taker"
        ).get()
        # 机器来源：fail-closed 要求显式 system（actor_id 空）；人名是**被清掉的人**。
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.target_name, self.taker.username)

    # ── ③ 下家鸡蛋铺副本由既有 member.withdraw 漏斗退（含前接手人自己的店） ─
    def test_downstream_member_copy_is_withdrawn_after_clear(self):
        member = self._member_binding()
        JidanpuMemberOrderLink.objects.create(
            binding=member,
            order=self.order,
            jidanpu_order_id="R-1",
            in_hall=True,
        )
        self._main_row()
        self._apply()
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind="member.withdraw", target_ref=LOCAL_NO,
                payload__member_binding_id=member.pk,
            ).exists(),
            "清人之后必须给推过的下家绑定排 member.withdraw",
        )

    # ── ④ 本来就没有接手人 ⇒ 幂等零动作（回归钉） ─────────────────────────
    def test_takerless_order_stays_untouched(self):
        result = self._apply()
        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        # ⛔ 不补建 MAIN 行（peer 建单支刻意不建绩效行，A2）；
        #   ⛔ 也不写 order.clear_taker（没有人被清）。
        self.assertFalse(
            Performance.objects.filter(
                order=self.order, emp_type=EmpType.MAIN
            ).exists()
        )
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=LOCAL_NO, action="order.clear_taker"
            ).exists()
        )

    # ── ⑤ 已结账挡住：状态照回池、钱面一个字不动（D4b 同一口径） ──────────
    def test_settled_row_is_not_cleared(self):
        self._main_row(
            settle_status="已结账",
            settled_by="财务甲",
            settled_amount=Decimal("3.00"),
            settled_at=self.T1,
        )
        result = self._apply()
        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk)      # 人没动
        self.assertEqual(perf.bonus_amount, Decimal("3.00"))  # 钱没动
        self.assertIsNotNone(perf.claimed_at)
        # 挡住的半条要**可见**：peer.inbound_assign 审计行点名请人工。
        assign_row = OperationLog.objects.filter(
            order_no=LOCAL_NO, action="peer.inbound_assign"
        ).get()
        self.assertIn("结账", assign_row.reason)
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=LOCAL_NO, action="order.clear_taker"
            ).exists()
        )

    # ── ⑥ 已转正的镜像单：接手类事件不回落未分配、不清人（既有口径回归钉） ─
    def test_promoted_hall_claim_keeps_the_taker(self):
        self._main_row()
        PeerHallMirror.objects.create(
            peer_base_url=self.binding.peer_base_url,
            peer_order_no=PEER_NO,
            status=PeerHallMirror.Status.PROMOTED,
            promoted_order=self.order,
            game=self.game,
            peer_upstream_binding=self.binding,
            first_seen_at=self.T1,
            last_seen_at=self.T1,
        )
        self._apply()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk)
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=LOCAL_NO, action="order.clear_taker"
            ).exists()
        )

    # ── ⑦ 转正确认在途（claiming）⇒ 落「代练中」、不清人（D-0303 建单支同款） ─
    def test_claiming_claim_completes_in_progress(self):
        claimer = get_user_model().objects.create_user(
            username="peer-pool-claimer", password="pw-c"
        )
        PeerHallMirror.objects.create(
            peer_base_url=self.binding.peer_base_url,
            peer_order_no=PEER_NO,
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by=claimer,
            claiming_at=self.T1,
            game=self.game,
            peer_upstream_binding=self.binding,
            remote_hall_price=Decimal("10.00"),
            first_seen_at=self.T1,
            last_seen_at=self.T1,
        )
        result = self._apply()
        self.assertEqual(result.applied, 1, result)
        self.order.refresh_from_db()
        # D-0303：接手事实已由本线落定（claiming 行恒带人）⇒ ⛔ 不落「未分配」。
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        # 转正收口跑了：镜像行 promoted、接手人 = 点单的人。
        mirror = PeerHallMirror.objects.get(
            peer_base_url=self.binding.peer_base_url, peer_order_no=PEER_NO
        )
        self.assertEqual(mirror.status, PeerHallMirror.Status.PROMOTED)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, claimer.pk)

    # ── ⑧ 评审 F-1：下行撤单事件的时刻必须是新鲜的（多跳 stale 丢弃钉） ────
    def test_downstream_withdraw_event_carries_a_fresh_occurred_at(self):
        """多跳形态（接手人 = 下家绑定账号）：清人排出的下行 `order.withdraw`
        事件的 `occurred_at` 必须**严格晚于**过期的 `order.updated_at`。

        本支的 `save(update_fields=_UPDATE_FIELDS)` 不含 `updated_at` ⇒
        `enqueue_order_event` 的默认时刻 = **上一次**订单写入（多跳场景恰是当初
        把单派给下家绑定账号的 `/assign` 时刻）⇒ 下家接收侧 stale 闸
        （`occurred_at <= last_applied_at`，相等即丢）把它当重复静默跳过 ⇒
        下家的接手人照样挂着。判据：夹具把 `updated_at` 倒填一小时，断言事件
        时刻严格晚于它（摘掉显式 `occurred_at=timezone.now()` ⇒ 本用例红）。
        """
        # 多跳形态：接手人本人被我方下家绑定（我曾把单派给他 ⇒ 他身上有生效绑定）。
        self._main_row()
        PeerDownstreamBinding.objects.create(
            account=self.taker,
            peer_base_url="https://peer-down-b.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        # 倒填 `updated_at`（绕 auto_now）=「上一次真实写入是很久以前的派单」；
        # 默认值若被走到，事件时刻就等于这个过期值。
        stale_at = dj_timezone.now() - timedelta(hours=1)
        Order.objects.filter(pk=self.order.pk).update(updated_at=stale_at)
        result = self._apply()
        self.assertEqual(result.applied, 1, result)
        event = PeerSyncEvent.objects.get(
            binding_role="downstream", kind="order.withdraw", target_ref=PEER_NO,
        )
        self.assertEqual(
            (event.meta or {}).get("withdraw_reason"), "taker_removed"
        )
        occurred_at = datetime.fromisoformat((event.meta or {})["occurred_at"])
        self.assertGreater(
            occurred_at,
            stale_at,
            "下行撤单事件取了过期的 order.updated_at ⇒ 下家会按 stale 静默丢弃",
        )

    # ── ⑨ 评审 R-1：queue_withdraw 排清人之后的承重顺序（B4 形态钉） ─────
    def test_queue_withdraw_after_clear_releases_the_takers_own_shop(self):
        """下家鸡蛋铺绑定属于**接手人本人**时，清人后那家店的副本也必须退。

        发运序（清人先、`queue_withdraw` 后）：人清掉 ⇒ `_main_taker_id` 回
        `None` ⇒ 「接手人自己的店不撤」的地址集为空 ⇒ 本绑定**不在排除集** ⇒
        排 `member.withdraw`（绿）。若错序（`queue_withdraw` 排到清人之前）：
        彼时接手人还在 ⇒ 他自己那家店被同店闸排除 ⇒ 一行都不排 ⇒ 本用例红
        （评审 M-A/M-B 变异证实错序确实这样红）。
        """
        member = JidanpuMemberBinding.objects.create(
            user=self.taker,
            jidanpu_base_url=MEMBER_URL,
            account="acc-taker-own-shop",
            password="pw-m",
            enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=member,
            order=self.order,
            jidanpu_order_id="R-B4",
            in_hall=True,
        )
        self._main_row()
        self._apply()
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind="member.withdraw", target_ref=LOCAL_NO,
                payload__member_binding_id=member.pk,
            ).exists(),
            "清人之后 queue_withdraw 必须把接手人自己那家店的副本也退掉"
            "（排到清人之前会被同店闸排除）",
        )
