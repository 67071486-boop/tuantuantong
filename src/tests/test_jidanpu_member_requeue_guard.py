"""终态停重排护栏（D-0370，2026-11-03）的判据网。

钉住的契约（`jidanpu_member.queue_member_event` 的自动路径闸）：

- **无变化不排**：同 `(kind, 单, 绑定)` 已有**终态**行（`skipped` / 烧完额度的
  `failed`）、且状态指纹与排队那一刻相同 ⇒ 不建新行、原样回终态行——两族生产
  死循环（launch 撞键 / withdraw 回读不确认）的「每 180 秒重排」由此断根；
- **有变化照排**：指纹由**驱动判定的字段**构成（状态 / 发单价有无 / 链路行归属与
  in_hall / withdrawn_at / 接手人）⇒ 任何一个变了都必须放行（⛔ 误拦合法重排）；
- **时间戳不进指纹**：只拨 `updated_at` 这类每轮必变的列 ⇒ 照旧拦（护栏不被
  时间戳击穿）；
- **人工路径永不被闸**：`rearm=True`（客服点重抛等显式动作）照旧复位烧完的行；
- **旧行 fail-open**：发版前落下的终态行 payload 里没有指纹 ⇒ 不拦（只多排一轮，
  新行带指纹、再下一轮起闸住）。

⚠️ 夹具一律合成值；终态行的「失败原因」用字面合成句，不依赖消费路径（消费侧的
两族端到端在 `test_jidanpu_member_consume` / 本文件 B 族组合用例）。
"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)

BASE_URL = "https://jidanpu-guard.test.invalid"
ACCOUNT = "guard-a"
SYNTHETIC_FAIL_REASON = "合成失败原因：回读不到（上游码 0）（已重试 5 次，不再自动重试，请人工核对）"


class _GuardBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="护栏合成游戏")
        self.user = User.objects.create_user(username=ACCOUNT, password="pw-guard")
        profile = StaffProfile.objects.create(user=self.user)
        profile.games.set([self.game])
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-guard",
            password="pw-guard",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261103000000000001",
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
        )

    def _rows(self, kind):
        return list(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind=kind, target_ref=self.order.order_no,
                payload__member_binding_id=self.binding.pk,
            ).order_by("id")
        )

    def _queue(self, *, kind, rearm=False):
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=kind, rearm=rearm
        )

    def _burn(self, row, *, status="failed", attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS):
        """把一条在飞行拨成终态（模拟消费烧完额度 / 终态跳过后的台账形态）。"""
        JidanpuSyncEvent.objects.filter(pk=row.pk).update(
            status=status, reason=SYNTHETIC_FAIL_REASON, attempts=attempts,
            processed_at=timezone.now(),
        )
        row.refresh_from_db()

    def _link(self, *, in_hall=False, withdrawn_at=None):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding,
            order=self.order,
            jidanpu_order_id="remote-guard-1",
            in_hall=in_hall,
            withdrawn_at=withdrawn_at,
        )


class BurnedFailedRowTests(_GuardBase):
    """A 族形态（launch 撞键）：终态 `failed` 烧完额度后的重排闸。"""

    def test_unchanged_state_does_not_queue_a_new_row(self):
        """无变化不排：烧完的行在 ⇒ 再排只回那条行，零新行（死循环的断根判据）。"""
        first = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertEqual(first.status, "pending")
        self.assertIn(jidanpu_member.REQUEUE_GUARD_FP_KEY, first.payload)
        self._burn(first)
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertEqual(second.pk, first.pk, "闸挡回时原样回终态行")
        self.assertEqual(len(self._rows(jidanpu_member.KIND_LAUNCH)), 1)

    def test_changed_order_status_requeues(self):
        """状态变了照排：订单状态是指纹的第一驱动，变了必须放行（⛔ 误拦合法重排）。"""
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        self.order.refresh_from_db()
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(second.pk, row.pk, "状态变了必须排新行")
        self.assertEqual(len(self._rows(jidanpu_member.KIND_LAUNCH)), 2)

    def test_only_touching_updated_at_still_blocks(self):
        """时间戳不进指纹：只拨「最后修改时刻」⇒ 照旧拦（护栏不被每轮必变的列击穿）。"""
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row)
        Order.objects.filter(pk=self.order.pk).update(
            updated_at=timezone.now() + timedelta(hours=3)
        )
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertEqual(second.pk, row.pk)

    def test_filling_the_hall_price_requeues(self):
        """补了发单价照排：无价被终态跳过的单，价补上后必须能重排（自愈路径）。"""
        Order.objects.filter(pk=self.order.pk).update(hall_price=None)
        self.order.refresh_from_db()
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row, status="skipped", attempts=0)
        Order.objects.filter(pk=self.order.pk).update(hall_price=Decimal("12.34"))
        self.order.refresh_from_db()
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(second.pk, row.pk, "价补上 = 状态变了 ⇒ 照排")

    def test_link_changes_requeue(self):
        """链路行变了照排：建链 / 翻在厅标记 / 标退成 / 改指远端单号，任一个都放行。

        ⚠️ 每一步之间先把上一条新行拨成终态：在飞行由**去重**收口（同键只留一行，
        spec §5.4 既有契约），闸只管「终态之后排不排新行」这一段。
        """
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row)
        link = self._link(in_hall=False)
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(second.pk, row.pk, "建了链 ⇒ 指纹变了 ⇒ 照排")
        self._burn(second)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(in_hall=True)
        third = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(third.pk, second.pk, "在厅标记翻了 ⇒ 照排")
        self._burn(third)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        fourth = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(fourth.pk, third.pk, "标了「已退成」⇒ 照排")
        self._burn(fourth)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            jidanpu_order_id="remote-guard-2"
        )
        fifth = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(fifth.pk, fourth.pk, "链行改指另一张远端单（数据修复）⇒ 照排")

    def test_taker_change_requeues(self):
        """接手人变了照排：期望形态与「同店不退」闸都按 MAIN 绩效行判。"""
        row = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self._burn(row)
        other = User.objects.create_user(username="guard-taker", password="pw-guard")
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=other,
            temp_amount=Decimal("88.00"),
        )
        second = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self.assertNotEqual(second.pk, row.pk)


class TerminalSkippedRowTests(_GuardBase):
    """B 族形态（withdraw 回读不确认）：终态 `skipped` 行同样要闸住重排。

    P0-3 起回读放弃收敛成 `skipped`（⛔ 不再烧额度），若闸只认 `failed` 烧完行，
    这族会变成「每 180 秒一条新 skipped 行」的另一种循环——本类钉死 skipped 也是终态。
    """

    def test_skipped_row_blocks_requeue_when_state_unchanged(self):
        row = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self._burn(row, status="skipped", attempts=0)
        second = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self.assertEqual(second.pk, row.pk)
        self.assertEqual(len(self._rows(jidanpu_member.KIND_WITHDRAW)), 1)

    def test_skipped_row_requeues_when_order_status_changes(self):
        row = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self._burn(row, status="skipped", attempts=0)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        self.order.refresh_from_db()
        second = self._queue(kind=jidanpu_member.KIND_WITHDRAW)
        self.assertNotEqual(second.pk, row.pk)


class FailOpenAndManualTests(_GuardBase):
    """旧行 / 人工路径的边界：闸只对「带指纹的终态行 + 自动路径」生效。"""

    def test_burned_row_without_fingerprint_fail_open(self):
        """发版前的旧行没有指纹 ⇒ 不拦（fail-open）；新排的行带上指纹。"""
        legacy = JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_member.KIND_LAUNCH,
            status="failed", target_type="order",
            target_ref=self.order.order_no,
            attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
            reason=SYNTHETIC_FAIL_REASON,
            payload={"member_binding_id": self.binding.pk},
        )
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(second.pk, legacy.pk, "旧行没指纹 ⇒ 照排（fail-open）")
        self.assertIn(
            jidanpu_member.REQUEUE_GUARD_FP_KEY, second.payload,
            "新行必须带上指纹（它才是下一轮的闸判据）",
        )
        self._burn(second)
        third = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertEqual(third.pk, second.pk, "新行烧完后下一轮被闸住（切换期自愈）")

    def test_manual_rearm_is_never_blocked_and_resets_budget(self):
        """人工路径不受闸：烧完的行被 `rearm=True` 复位（E-0153 契约原样）。"""
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row)
        rearmed = self._queue(kind=jidanpu_member.KIND_LAUNCH, rearm=True)
        self.assertEqual(rearmed.pk, row.pk)
        self.assertEqual(rearmed.status, "pending")
        self.assertEqual(rearmed.attempts, 0)
        self.assertIn("rearmed_at", rearmed.payload)

    def test_row_without_fingerprint_still_blocks_after_one_cycle(self):
        """闸的比对是「终态行的指纹 vs 现在的指纹」：终态行没指纹 ⇒ 放行（fail-open 半支）。"""
        row = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self._burn(row)
        JidanpuSyncEvent.objects.filter(pk=row.pk).update(
            payload={"member_binding_id": self.binding.pk}
        )
        second = self._queue(kind=jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(second.pk, row.pk, "终态行指纹缺失 ⇒ 不拦")


class ReconcileIntegrationTests(_GuardBase):
    """对账 → 排事件整链：终态行 + 状态无变化 ⇒ 对账每轮照判、但台账零新行。"""

    def test_reconcile_keeps_judging_but_stops_writing_rows(self):
        now = timezone.now()
        first = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(first.launch, 1, "待抢单 + 无链路 ⇒ 第一轮排出 launch")
        rows = self._rows(jidanpu_member.KIND_LAUNCH)
        self.assertEqual(len(rows), 1)
        self._burn(rows[0])
        for round_no in (2, 3):
            stats = jidanpu_member.reconcile_and_queue(
                self.binding, now=now, line=None
            )
            self.assertEqual(
                stats.launch, 1, "对账的差集判定不动（单还是该抛的形态）"
            )
            self.assertEqual(
                len(self._rows(jidanpu_member.KIND_LAUNCH)), 1,
                f"第 {round_no} 轮对账不许再写新行（死循环断根的整链判据）",
            )

    def test_state_change_unblocks_the_reconcile(self):
        """链路状态真的变了 ⇒ 对账排得出新行（护栏没有把合法重排一起闸死）。

        ⚠️ 用「建链（不在厅）」做放行面，⛔ 不用「状态绕一圈回到原值」：指纹的口径是
        「相对那次终态有没有变化」——回到与失败时**相同的**状态本来就该闸住（同状态
        重跑同一次注定失败的动作正是本闸要断的循环）。
        """
        now = timezone.now()
        jidanpu_member.reconcile_and_queue(self.binding, now=now)
        rows = self._rows(jidanpu_member.KIND_LAUNCH)
        self.assertEqual(len(rows), 1)
        self._burn(rows[0])
        # 状态绕一圈回原值：对账照判「该抛」，但台账零新行（同状态 ⇒ 同结局 ⇒ 闸住）。
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        self.order.refresh_from_db()
        self.assertEqual(
            jidanpu_member.reconcile_and_queue(self.binding, now=now).launch, 0
        )
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.WAITING_GRAB)
        self.order.refresh_from_db()
        jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(
            len(self._rows(jidanpu_member.KIND_LAUNCH)), 1,
            "状态回到与失败时相同的值 ⇒ 照旧闸住（护栏口径的负性钉）",
        )
        # 建一条「不在厅」的链路（= 真实状态变化，对账改判「补抛」）⇒ 排得出新行。
        self._link(in_hall=False)
        stats = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(stats.launch, 1)
        self.assertEqual(len(self._rows(jidanpu_member.KIND_LAUNCH)), 2)
