"""launch 读回熔断 + 去重过滤精确化（N2 / E-0304，2026-10-29）的行为网。

## 背景（审计 S-01）

三家下家（xingqiong / qianyue / xingju）按店分化「录单后回读」0% 成功（对端 key1 搜索
不回）⇒ launch 烧完 `OUTBOX_MAX_ATTEMPTS` 后，那条 `failed` 行把 `queue_member_event`
的在飞去重槽**永久占死**（对账每 180 秒重排 `to_launch` 却全被去重挡回）⇒ 整店同步
静默失效。评审**证伪**了「只解除死槽」：无节流的重排 = 每单每天约 46 条新事件 × 5 发
≈ 9-10 发/秒白打对端 WAF ⇒ 两个改动必须同一次落：

1. **去重过滤精确化**：`rearm=False`（周期对账侧）的去重只认 `attempts < OUTBOX_MAX_ATTEMPTS`
   的在飞行——死行不再阻塞重排；退避周期内的行照旧去重；`rearm=True` 的人工复位
   （E-0153）原样保留（烧完的行仍由它复位，不另建新行）。
2. **按绑定的读回熔断**：回读失败族的 failed 行累计达阈值 ⇒ 挂起该绑定的 launch 投递
   （写一条人话台账 kind=`member.launch_breaker_open`，点名店名与恢复条件）；
   挂起满间隔放行**一发**探测，回读成功即恢复、失败续挂。
   ⭐ **D-0359 D1-D3（业务方三裁定）改准**：闸门从「排队侧挡新行」挪到
   **投递侧**（`_consume_all` → `_launch_delivery_gate`）——挂起期新事件**照常排队**
   （发单意图不丢，解除后队列自动放行），行保持 pending 只是不投；探测间隔退役
   「30 分钟 ×2 封顶 6 小时」改**固定 5 分钟**；首次判定挂起打一条 warning 日志
   （journald `grep 熔断` 可查），续挂不打（统计行「熔断挂起 N」承担可见性）。

⚠️ **弄红判据（本网的变异自证，实测读数见提交说明）**：
- 摘掉去重的 `attempts__lt` 过滤（还原原句）⇒ `DedupPrecisionTests` 第 1 条红；
- 摘掉 `_consume_all` 里的投递侧闸（launch 照投）⇒ 挂起/探测两组红；
- 摘掉兜底计数的 True 支（评审 F-2 ①）⇒ 兜底挂起条红；
- 把探测间隔改回「越挂越长」（或删掉固定 5 分钟常量）⇒ 探测节奏条红。
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_member
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
)

BASE_URL = "https://jidanpu-breaker.test.invalid"
ACCOUNT = "breaker-a"
ORDER_NO = "20261029000000003001"
ORDER_NO_2 = "20261029000000003002"
ORDER_NO_3 = "20261029000000003003"

#: 「回读失败族」三个原因的**字面值**（⛔ 不从被测常量派生：常量漂移时这里要红，E-0024）。
READBACK_LITERAL = "录单后回读不到对方订单号，本轮只保住「已录过」这个事实"
PRE_READ_LITERAL = "录单前回读「对方是否已有这张单」失败，本轮不录（下一轮重试）"
PREPARE_LITERAL = "抛单前回读对方那张单的状态失败（读不到或查无此单），本轮不抛单（下一轮重试）"
#: 非族失败的对照（录单被拒 = 写失败，不属于回读失败族）。
NON_FAMILY_LITERAL = "鸡蛋铺拒绝了这次录单"
#: `_retry` 到上限时追加的后缀（`ABANDONED_SUFFIX` 的字面副本；失败族行在生产里就长这样）。
ABANDONED_SUFFIX_LITERAL = "（已重试 5 次，不再自动重试，请人工核对）"

KIND_LAUNCH_LITERAL = "member.launch"
KIND_ASSIGN_LITERAL = "member.assign"
KIND_BREAKER_LITERAL = "member.launch_breaker_open"

#: 注入式假客户端：本网的消费路径要么被熔断挂起、要么走「无发单价 ⇒ 零上游」的
#: 立即跳过（`LAUNCH_NO_PRICE_REASON`）⇒ 假客户端**永远不该**被碰到真出口；
#: 碰到了 = 用例以错误的方式变红（响亮暴露，⛔ 不静默连真网）。
CLIENT = object()


def _family_reason(prefix: str) -> str:
    """生产里一条烧完额度的回读失败行的 reason 形状：前缀 + 上游原话 + 放弃后缀。"""
    return f"{prefix}：对方搜索接口无响应（上游码 0）{ABANDONED_SUFFIX_LITERAL}"


class _BreakerBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="合成游戏乙")
        self.user = User.objects.create_user(username=ACCOUNT, password="pw-b")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            password="pw-b",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
        )

    def _order(self, order_no: str) -> Order:
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
        )

    def _free_launch(self, order_no: str) -> Order:
        """造一张**没有发单价**的单：launch 消费者对 NULL 价第一跳就 `skipped`
        （`LAUNCH_NO_PRICE_REASON`，零上游调用）⇒ run_outbox 的「投了」断言
        不碰真出口、不建真连。"""
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            hall_price=None,
            total_price="50.00",
        )

    def _seed_failure(
        self, order, *, reason: str | None = None, attempts: int = 5, status: str = "failed",
    ) -> JidanpuSyncEvent:
        """手工造一条 launch 台账行（绕过排队闸，直接钉住消费者/历史留下的形状）。"""
        return JidanpuSyncEvent.objects.create(
            direction="out",
            kind=KIND_LAUNCH_LITERAL,
            status=status,
            target_type="order",
            target_ref=order.order_no,
            attempts=attempts,
            reason=reason if reason is not None else _family_reason(READBACK_LITERAL),
            payload={"member_binding_id": self.binding.pk},
        )

    def _seed_failures(self, n: int, *, prefix: str = READBACK_LITERAL):
        return [self._seed_failure(self.order, reason=_family_reason(prefix)) for _ in range(n)]

    def _launch_rows(self, order=None) -> "list[JidanpuSyncEvent]":
        qs = JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH_LITERAL,
            payload__member_binding_id=self.binding.pk,
        )
        if order is not None:
            qs = qs.filter(target_ref=order.order_no)
        return list(qs.order_by("id"))

    def _notes(self) -> "list[JidanpuSyncEvent]":
        return list(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_BREAKER_LITERAL,
                payload__member_binding_id=self.binding.pk,
            ).order_by("id")
        )

    def _backdate_notes(self, *, minutes: float) -> None:
        """把本绑定全部熔断台账行的 created_at 拨回去（模拟「挂起已经过了多久」）。"""
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_BREAKER_LITERAL,
            payload__member_binding_id=self.binding.pk,
        ).update(created_at=timezone.now() - timedelta(minutes=minutes))


# ══ ① 去重过滤精确化（修复前红的那条 + 两条回归钉）══════════════════════════


class DedupPrecisionTests(_BreakerBase):
    def test_a_burned_dead_row_no_longer_blocks_the_periodic_requeue(self):
        """烧完额度的死行不再阻塞对账重排：同单再排 → 新建一行，死行原样留作台账。

        S-01 现场：`(订单, 坏店绑定)` 全史只有 1 条 launch 行——死行把去重槽占死，
        对账每 180 秒白来一趟。判据钉**行数与两行各自的形状**（旧行 failed/5 不动、
        新行 pending/0 可被消费），⛔ 不是「返回了哪一行」。
        """
        burned = self._seed_failure(self.order)
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        rows = self._launch_rows(self.order)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].pk, burned.pk)
        self.assertEqual(rows[0].status, "failed")
        self.assertEqual(rows[0].attempts, jidanpu_member.OUTBOX_MAX_ATTEMPTS)
        self.assertEqual(rows[1].status, "pending")
        self.assertEqual(rows[1].attempts, 0)

    def test_a_row_still_inside_its_budget_still_dedups(self):
        """退避周期内的行（attempts < 上限）照旧去重：同单再排 → 原行返回、不新建。

        ⛔ 这是「精确化」的另一半：放宽只对**烧完**的行生效，不许连退避中的行一起放
        （否则连点几下 / 每轮对账都会绕过上游退避）。修复前后都绿（回归钉）。
        """
        event = self._seed_failure(self.order, attempts=1)
        got = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertEqual(got.pk, event.pk)
        self.assertEqual(len(self._launch_rows(self.order)), 1)

    def test_manual_rearm_still_resets_the_burned_row_in_place(self):
        """人工动作（rearm=True）对烧完的行仍走**复位**（E-0153 裁定原样保留）。

        ⛔ 不许因为去重放宽就改成「人工也另建新行」：一次点击 = 复用那一行、
        额度 / 状态 / 原因 / 退避锚四个值都复位。
        """
        burned = self._seed_failure(self.order)
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True
        )
        rows = self._launch_rows(self.order)
        self.assertEqual(len(rows), 1)
        fresh = JidanpuSyncEvent.objects.get(pk=burned.pk)
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.reason, "")
        self.assertIsNone(fresh.processed_at)


# ══ ② 读回熔断（按绑定）══════════════════════════════════════════════════


class BreakerContractTests(_BreakerBase):
    def test_the_threshold_is_the_plan_value(self):
        """阈值 20 是计划定稿值（依据=坏店 7 天 7,135 条 / 正常店≈0 的分化，见常量注释）。"""
        self.assertEqual(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD, 20)

    def test_the_probe_interval_is_fixed_five_minutes(self):
        """⭐ D2：探测间隔 = 固定 5 分钟（业务方「不要越搞越长」），常量钉死。"""
        self.assertEqual(
            jidanpu_member.LAUNCH_READBACK_PROBE_INTERVAL, timedelta(minutes=5)
        )

    def test_the_doubling_machinery_is_retired(self):
        """⭐ D2：「30 分钟 ×2、封顶 6 小时」整组退役——符号不许再存在（防复活）。

        `_breaker_interval` / 两个间隔常量 / `payload["window"]` 的键 / 排队侧闸
        `_launch_breaker_blocks` 全部拆除；谁把它们加回来（或按旧语义复用）这条就红。
        """
        for retired in (
            "_breaker_interval",
            "LAUNCH_READBACK_BREAKER_BASE_INTERVAL",
            "LAUNCH_READBACK_BREAKER_MAX_INTERVAL",
            "BREAKER_WINDOW_PAYLOAD_KEY",
            "_launch_breaker_blocks",
        ):
            self.assertFalse(
                hasattr(jidanpu_member, retired),
                msg=f"{retired} 已随 D2/D3 退役，⛔ 不许复活",
            )

    def test_the_ledger_kind_is_the_wire_value(self):
        self.assertEqual(jidanpu_member.KIND_LAUNCH_BREAKER_OPEN, KIND_BREAKER_LITERAL)

    def test_the_failure_family_is_pinned_to_the_three_read_hops(self):
        """回读失败族 = launch 消费路径的三跳**纯读**失败（字面钉死，⛔ 不许悄悄扩/缩）。

        ⚠️ 刻意不含 `THROW_CONFIRM_REASON`（写后判定，混着「读到了但不是 Y」的形态）
        与 ②式 自己的原因文案——边界登记在该常量的注释里。
        """
        self.assertEqual(
            jidanpu_member.LAUNCH_READBACK_FAILURE_REASONS,
            (PRE_READ_LITERAL, READBACK_LITERAL, PREPARE_LITERAL),
        )


class LaunchReadbackBreakerTests(_BreakerBase):
    """⭐ D-0359 D1-D3（2026-11-01）：投递侧熔断——意图不丢 + 固定 5 分钟探测 + 日志。

    旧网钉「排队侧挡新行」；D3 把闸挪到投递侧（`_consume_all` → `_launch_delivery_gate`）
    ⇒ 本类整体改准（判定三档 `_launch_readback_breaker_state` **原样**，另有兜底/精度
    用例钉着）：

    - 挂起期：新事件**照常排队**（pending），run_outbox 不投（`stats.suspended` 计数）、
      行保持 pending；首次判定挂起写**恰一条**台账 + 一条 warning（含「熔断」与店名）；
      续挂**不打** warning（⛔ 每 10 秒刷一条）也不加台账；
    - 探测：满**固定 5 分钟**放行**一发**（迭代序第一条待投 launch 行 = 行 id 最小者），
      放行写探测台账行（下一发的时刻锚）；探测 / 人工旁路行成功（done）⇒ 期满那一刀的
      状态查询直接开闸（恢复延迟 ≤ 5 分钟，D2 的承诺）；期满仍坏 ⇒ 续挂并重锚；
    - 人工重抛（payload 带 `rearmed_at`）**不进闸**、照旧直投。

    ⚠️ 本网消费路径的确定性：run_outbox 真投出去的行全部是 `_free_launch` 造的
    「无发单价」单（第一跳 `skipped`、零上游）⇒ 假客户端 `CLIENT` 永远不该被碰到
    真出口（碰到了会响亮炸）。

    ⚠️ **时刻锚的用法**：台账行的 `created_at` 是真实写入时刻；用例用 `_backdate_notes`
    拨锚（照旧网的形状），⛔ 不靠传未来的 `now` 凑 elapsed（两者混用会以错误的原因变绿）。
    """

    def test_crossing_threshold_queues_the_row_but_delivery_side_suspends_it(self):
        """达阈值 ⇒ 新 launch **照常排队**（D3：意图不丢），投递侧挂起 + 恰一条台账 + warning。

        - 行 pending 不动、`stats.suspended` 计数；
        - 台账**恰一条**（反复被挡不灌台账）、终态 skipped、点名店名与恢复条件；
        - D1：warning 含「熔断」与店名（journald `grep 熔断` 可查）；
        - 续挂一轮：**不打新 warning**（`assertNoLogs`）且台账不增加（⛔ 刷屏）。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        got = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertIsNotNone(got, "D3：新事件照常排队（⛔ 不许再返回 None）")
        with self.assertLogs("apps.business.jidanpu_member", level="WARNING") as captured:
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        joined = "\n".join(captured.output)
        self.assertIn("熔断", joined)
        self.assertIn(ACCOUNT, joined)
        self.assertEqual(stats.suspended, 1)
        row = JidanpuSyncEvent.objects.get(pk=got.pk)
        self.assertEqual(row.status, "pending", "挂起 = 行保持 pending（意图不丢）")
        notes = self._notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].status, "skipped")
        self.assertFalse(notes[0].payload["probe"])
        self.assertIn(ACCOUNT, notes[0].reason)
        self.assertIn("恢复", notes[0].reason)
        # 续挂：不打 warning（⛔ 刷屏）、不新增台账，行照旧挂着。
        with self.assertNoLogs("apps.business.jidanpu_member", level="WARNING"):
            stats2 = jidanpu_member.run_outbox(
                self.binding, now=timezone.now() + timedelta(minutes=1), client=CLIENT
            )
        self.assertEqual(stats2.suspended, 1)
        self.assertEqual(len(self._notes()), 1)
        self.assertEqual(JidanpuSyncEvent.objects.get(pk=got.pk).status, "pending")

    def test_probe_every_five_minutes_then_success_resumes(self):
        """固定 5 分钟节奏 + 恢复自动放行（D2/D3 主路径）。

        - 满 5 分钟前：仍挂起（零探测）；
        - 期满：放行**一发**——迭代序第一条（行 id 最小）真的被投（无价单 → skipped），
          第二条仍挂起；探测台账行落下（下一发的时刻锚）；
        - 探测成功（done）⇒ 下一轮期满刀的状态查询开闸：第二条**整批放行**
          （⛔ 不许让恢复的店一格一格放），台账不再增加。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        order3 = self._free_launch(ORDER_NO_3)
        row2 = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        row3 = jidanpu_member.queue_member_event(
            order3, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        # 满 5 分钟前：不放行。
        self._backdate_notes(minutes=4)
        self.assertEqual(
            jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            ).suspended,
            2,
        )
        self.assertEqual(len(self._notes()), 1)
        # 期满（拨锚 6 分钟）：放行一发探测 = 行 id 最小者（row2）被投；row3 仍挂起。
        self._backdate_notes(minutes=6)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row2.pk).status,
            "skipped",
            "探测代表（迭代序第一条）真的被投出去消费了",
        )
        self.assertEqual(JidanpuSyncEvent.objects.get(pk=row3.pk).status, "pending")
        notes = self._notes()
        self.assertEqual(len(notes), 2)
        self.assertTrue(notes[1].payload["probe"])
        # 探测成功（done）⇒ 期满刀的状态查询开闸：row3 整批放行。
        JidanpuSyncEvent.objects.filter(pk=row2.pk).update(
            status="done", reason="", processed_at=timezone.now(),
        )
        self._backdate_notes(minutes=6)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row3.pk).status,
            "skipped",
            "恢复后队列自动放行（整批）",
        )
        self.assertEqual(len(self._notes()), 2, "恢复不写台账")

    def test_probe_failure_keeps_suspension_on_the_fixed_five_minute_clock(self):
        """探测失败（仍坏）⇒ 续挂并**重锚**：下一次放行仍只等 5 分钟（D2：不再加倍）。

        ⭐ D2 的区分档：重锚后 **6 分钟**即放行第二发——旧机制 window 2 = 60 分钟，
        这里必不放（变异 = 把固定间隔改回倍增 ⇒ 本用例红）。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        row2 = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        # 首轮：挂起 + 恰一条台账（探测锚在台账行上，先有它才能谈「期满」）。
        self._backdate_notes(minutes=4)
        self.assertEqual(
            jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            ).suspended,
            1,
        )
        # 期满 ⇒ 放行第一发（row2 被投）。
        self._backdate_notes(minutes=6)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0, "期满放行的那一发不进「熔断挂起」计数")
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row2.pk).status, "skipped"
        )
        # 探测失败：照生产形状烧掉（族内 failed / 额度烧完）。
        JidanpuSyncEvent.objects.filter(pk=row2.pk).update(
            status="failed",
            attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
            reason=_family_reason(READBACK_LITERAL),
            processed_at=timezone.now(),
        )
        # 烧完的 row2 走 abandoned、不进闸 ⇒ 补一条新的待投行（后面的挂起/放行都有对象）。
        order3 = self._free_launch(ORDER_NO_3)
        row3 = jidanpu_member.queue_member_event(
            order3, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        # 重锚后 4 分钟：不放行。
        self._backdate_notes(minutes=4)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(JidanpuSyncEvent.objects.get(pk=row3.pk).status, "pending")
        # 重锚后 6 分钟：放行第二发（旧机制 window 2 = 60 分钟，必不放 ⇒ 变异红）。
        self._backdate_notes(minutes=6)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0)
        self.assertEqual(JidanpuSyncEvent.objects.get(pk=row3.pk).status, "skipped")
        notes = self._notes()
        self.assertEqual(len(notes), 3)
        self.assertTrue(notes[1].payload["probe"])
        self.assertTrue(notes[2].payload["probe"])

    def test_manual_rearm_bypasses_the_delivery_side_suspension(self):
        """人工旁路（D3 不变量）：`rearm=True` 复用的行带 `rearmed_at` ⇒ 照旧直投。

        挂起中的人工重抛**必须**立刻出去（业务方口径）；旁路行的凭据是 payload 上的
        `rearmed_at` 键——闸对它根本不判。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        row = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self._backdate_notes(minutes=1)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row.pk).status, "pending"
        )
        # 人工重抛（复用在飞行 ⇒ payload 落 rearmed_at）。
        jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True
        )
        row.refresh_from_db()
        self.assertIn("rearmed_at", row.payload)
        # 下一轮：旁路行直投（消费成 skipped），挂起计数为零。
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "skipped")

    def test_a_healthy_store_never_trips(self):
        """回归钉：健康店（launch 全 done）照常投递，零熔断台账、零挂起计数。"""
        for _ in range(25):
            self._seed_failure(self.order, status="done", reason="", attempts=0)
        order2 = self._free_launch(ORDER_NO_2)
        got = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertIsNotNone(got)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=got.pk).status, "skipped"
        )
        self.assertEqual(self._notes(), [])

    def test_non_family_failures_do_not_trip_the_breaker(self):
        """族精度：失败的 launch 若**不是**回读失败族（如录单被拒），攒再多也不挂起。"""
        self._seed_failures(
            jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD + 5,
            prefix=NON_FAMILY_LITERAL,
        )
        order2 = self._free_launch(ORDER_NO_2)
        got = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=got.pk).status, "skipped"
        )
        self.assertEqual(self._notes(), [])

    def test_other_kinds_still_deliver_while_launch_is_suspended(self):
        """熔断只闸 launch：挂起中的店，指派录单（assign）照常投递。"""
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        assigned = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_ASSIGN
        )
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=KIND_ASSIGN_LITERAL,
                payload__member_binding_id=self.binding.pk,
            ).exclude(pk=assigned.pk).exists(),
            False,
            "assign 恰一条（挂起期没多排也没少排）",
        )
        assigned.refresh_from_db()
        self.assertEqual(assigned.status, "skipped", "assign 照常投递（无价单零上游跳过）")

    def test_recovered_store_that_breaks_again_starts_a_fresh_episode(self):
        """恢复后再坏 ⇒ 全新一轮（新台账行 probe=False + warning，⛔ 不接续旧账）。

        ⭐ 走**人工旁路**触发恢复（D3 现实形状）：挂起期的人工重抛直投成功——它的
        `done` 行 id **晚于**台账行（挂起期排的队），`_launch_recovered_after` 的
        `id__gt` 锚看得见 ⇒ 恢复判定即时生效；随后重新攒满阈值 ⇒ 期满刀判「该挂」
        走**新一轮**支（写 probe=False 的台账行，而不是探测行）。
        ⚠️ 挂起**前**已排队、id 早于台账行的行成功时，id 锚看不见它们——那一档由
        期满刀的**状态查询**兜住（`_launch_readback_breaker_state` 第 1 档，
        `test_probe_every_five_minutes_then_success_resumes` 钉的就是那条腿）。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        row1 = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        run_outbox = jidanpu_member.run_outbox
        run_outbox(self.binding, now=timezone.now(), client=CLIENT)  # 首轮挂起
        # 挂起期排队的新行（id 晚于台账行）+ 人工重抛（旁路）⇒ 直投成功。
        order3 = self._free_launch(ORDER_NO_3)
        row2 = jidanpu_member.queue_member_event(
            order3, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        jidanpu_member.queue_member_event(
            order3, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True
        )
        run_outbox(self.binding, now=timezone.now(), client=CLIENT)
        row2.refresh_from_db()
        self.assertEqual(row2.status, "skipped", "旁路行直投（无价单零上游跳过）")
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row1.pk).status, "pending",
            "非旁路行仍挂起",
        )
        # 探测成功（row2 的 done，id 晚于台账行 ⇒ id 锚看得见）。
        JidanpuSyncEvent.objects.filter(pk=row2.pk).update(
            status="done", reason="", processed_at=timezone.now(),
        )
        # 又坏：重新攒满阈值（新的失败行比那条 done 新）。
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        # 恢复检查先于间隔检查：row2 的 done ⇒ 状态查询判「该挂」⇒ 全新一轮。
        stats = run_outbox(self.binding, now=timezone.now(), client=CLIENT)
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row1.pk).status, "pending"
        )
        notes = self._notes()
        self.assertEqual(len(notes), 2)
        self.assertFalse(notes[1].payload["probe"], "新一轮写的是首次挂起行、不是探测行")

    def test_the_gate_costs_nothing_when_the_dedup_hits(self):
        """成本钉：在飞行去重命中（健康店 steady state）时**只花一次去重查询**。

        排队侧不再有熔断闸（D3）⇒ 这一条现在钉的是「去重命中路径上没有任何多余的
        台账/熔断查询」。若有人把任何判定挪进去，这条当场红。
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        event = self._seed_failure(self.order, attempts=1)
        with CaptureQueriesContext(connection) as ctx:
            got = jidanpu_member.queue_member_event(
                self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
            )
        self.assertEqual(got.pk, event.pk)
        self.assertEqual(len(self._notes()), 0)
        sqls = [q["sql"] for q in ctx.captured_queries]
        # django-tenants 的 `SET search_path` 是夹具管道，不算本函数的查询；
        # 判据 = 触到台账表的查询**恰一条**、是去重 SELECT，且新过滤 `attempts <` 在里面。
        event_sqls = [s for s in sqls if "business_jidanpusyncevent" in s]
        self.assertEqual(len(event_sqls), 1, msg=f"意外的查询面：{sqls}")
        self.assertIn(
            f'"business_jidanpusyncevent"."attempts" < '
            f"{jidanpu_member.OUTBOX_MAX_ATTEMPTS}",
            event_sqls[0],
        )

    def test_backdating_beyond_the_note_window_starts_fresh(self):
        """台账查询界（12 小时）之外的旧熔断行对闸门不可见：老过界的行等于「上一段
        历史」，新一轮从阈值判定重新开始（写**新**的首次挂起行）。"""
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        order2 = self._free_launch(ORDER_NO_2)
        jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)
        self._backdate_notes(minutes=13 * 60)
        # 熔断行老过界 ⇒ 闸门按「没有台账」重新判定：阈值仍满 ⇒ 写**新**的首次挂起行。
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        notes = self._notes()
        self.assertEqual(len(notes), 2)
        self.assertFalse(notes[1].payload["probe"])

    def test_mixed_window_suspends_via_the_24h_family_count_fallback(self):
        """兜底挂起支真驱动为 True（评审 F-2 ①）：混合窗 + 24h 族计数 ≥ 20 ⇒ 照样挂起。

        造数：先撒 20 条**族内**失败（进 24h 计数），再压 15 条**族外**失败（录单被拒）
        在最新处 ⇒ 最新 20 条 = 15 族外 + 5 族内（混合、无 done）⇒ 精确档（「20 条全族内」）
        不中、退 24h 兜底计数 = 20 ≥ 20 ⇒ 挂起。这条分支是「恢复后再坏重攒期」与
        「人工 rearm 掺入 pending 行」场景的实际挂起路径，⛔ 不是死代码。
        判据：投递侧挂起（行保持 pending）+ 台账**恰一条**，且台账理由含「24 小时内」
        （钉住是**兜底档**挂的、不是精确档——精确档的人话是「最近 20 条 … 全部回读失败」）。
        """
        self._seed_failures(jidanpu_member.LAUNCH_READBACK_BREAKER_THRESHOLD)
        for _ in range(15):
            self._seed_failure(self.order, reason=NON_FAMILY_LITERAL)
        order2 = self._free_launch(ORDER_NO_2)
        row = jidanpu_member.queue_member_event(
            order2, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertIsNotNone(row)
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(pk=row.pk).status, "pending"
        )
        notes = self._notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].status, "skipped")
        self.assertFalse(notes[0].payload["probe"])
        self.assertIn("24 小时内", notes[0].reason)
        self.assertNotIn("全部回读失败", notes[0].reason)
        # 挂起期的后续排队照旧只挂不投，且不新增台账行。
        order3 = self._free_launch(ORDER_NO_3)
        jidanpu_member.queue_member_event(
            order3, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        stats = jidanpu_member.run_outbox(
            self.binding, now=timezone.now(), client=CLIENT
        )
        self.assertEqual(stats.suspended, 2)
        self.assertEqual(len(self._notes()), 1)
