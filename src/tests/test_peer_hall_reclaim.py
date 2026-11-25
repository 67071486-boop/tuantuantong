"""D-0303 团团通大厅镜像：**P3 增量**（`feat/peer-hall-port`）——
接单卡超时回收 + 已转正的幂等重投。⭐ D-0335起阈值从 300 秒压到 **60 秒**。

v1 的口径是「不做 claiming 超时自动回退」（spec §4.4 / §6②）⇒ `claiming` 的行只有两条
出路（转正 / `hall.claim_refused` 收回），两条都要**对端有回音**。「事件丢了 / 上家一直没
指派」这一档两条都不成立 ⇒ 那张卡永久既接不了也看不见。本文件钉住补上的两支：

① `peer_hall.reclaim_stuck_claims`（**纯本地、零网络**）：
   `claiming` ∧ `claiming_at < now-60s`（**严格小于**，D-0335 起阈值 60）∧ 无 `promoted_order`
   ⇒ 回 `listed` + 清 `claiming_by` / `claiming_at` + `last_error` 一句人话 + 一条台账；
   `promoted` / `listed` **一个字不动**；回收之后能重新接一次。
② `claim_peer_mirror` 的幂等重投：行已 `promoted` 且正式单的 **MAIN 绩效行 `user`** 就是
   本人 ⇒ 回「正在处理中」（⛔ 不是「已被接走」）+ 重投一次 `order.grab`
   （⛔ 不建第二张单、⛔ 不改价）。
③ 常驻循环（`peer_sync` 命令）**每轮先跑一次**①（纯本地，挂在投递之前）。

⚠️ 判据一律钉**字面值**（`"该笔订单正在处理中"` / `"hall.claim_recycle"` / `60`），
⛔ 不拿实现常量当期望（E-0024）。
⚠️ 夹具值全是合成值（`*.test.invalid` / `syn-` 前缀 / 合成单号），见 E-0077。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.claim import ClaimError
from apps.business.models import (
    EmpType,
    Game,
    Order,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_hall import (
    RECLAIM_LEDGER_KIND,
    claim_peer_mirror,
    reclaim_stuck_claims,
)
from apps.business.peer_sync import effective_peer_ref, hash_token, new_token
from tests.test_peer_hall_claim import ClaimChainFixture

#: 回收时写进 `last_error` 的那句人话（**字面值**，⛔ 不 import 常量当期望）。
RECLAIMED_NOTE_TEXT = "接单超过 1 分钟仍未收到正式订单，已自动放回大厅，可以重新接单"
#: `jidanpu_common.IN_PROGRESS_MESSAGE` / `ALREADY_TAKEN_MESSAGE` 的字面值
#: （两句话必须**分得开**，本文件的判据就是「回的是哪一句」）。
IN_PROGRESS_TEXT = "该笔订单正在处理中"
ALREADY_TAKEN_TEXT = "该笔订单已被其他打手接取"


def _make_taker(username, game):
    """一个「有游戏范围」的合成打手（照 `ClaimChainFixture` 的同一条做法）。"""
    user = get_user_model().objects.create_user(username=username, password="pw-syn")
    StaffProfile.objects.create(user=user).games.set([game])
    return user


class ReclaimStuckClaimsTests(ClaimChainFixture):
    """① `reclaim_stuck_claims` 的判据与副作用（纯本地）。"""

    def _claim_and_age(self, *, age_seconds):
        """让 `self.mirror` 停在 `claiming`，并把占住时刻推到 `age_seconds` 秒之前。

        ⚠️ 时钟必须**钉住一份**：`claiming_at` 与判据用的 `now` 都取 `self.now_ref`
        （同一个值）。若判据那边重新取一次 `timezone.now()`，两次调用之间流逝的几毫秒会让
        「恰好 5 分钟」也满足 `<` ⇒ 那条用例变成恒假，判不出 `<` 与 `<=`。
        """
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.now_ref = self._now()
        self.claiming_at = self.now_ref - timedelta(seconds=age_seconds)
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            claiming_at=self.claiming_at
        )
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)

    def _reclaim(self):
        """按钉住的那一份时钟跑回收（见 `_claim_and_age` 的注）。"""
        return reclaim_stuck_claims(now=self.now_ref)

    def _ledger_rows(self):
        return PeerSyncEvent.objects.filter(kind=RECLAIM_LEDGER_KIND)

    def test_fifty_seconds_is_not_reclaimed(self):
        """50 秒不动 ⇒ **不动**（D-0335 起判据是 60 秒）。"""
        self._claim_and_age(age_seconds=50)
        self.assertEqual(self._reclaim(), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        self.assertEqual(self.mirror.claiming_at, self.claiming_at)
        self.assertEqual(self.mirror.last_error, "")
        self.assertFalse(self._ledger_rows().exists())

    def test_exactly_sixty_seconds_is_not_reclaimed(self):
        """**恰好** 60 秒 ⇒ 不回收：判据是 `claiming_at < now-60s`（严格小于）。

        判别力：把实现改成 `<=` ⇒ 本用例红（这是 `<` / `<=` 的唯一判据）。
        """
        self._claim_and_age(age_seconds=60)
        self.assertEqual(self._reclaim(), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        self.assertIsNotNone(self.mirror.claiming_at)
        self.assertFalse(self._ledger_rows().exists())

    def test_over_sixty_seconds_returns_the_card_and_clears_both_columns(self):
        """超 60 秒 ⇒ 回 `listed` + 清两列 + 人话 + 一条台账。"""
        self._claim_and_age(age_seconds=61)
        self.assertEqual(self._reclaim(), 1)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        # ⚠️ 两列都要清（E-0027 同族：留一句「谁在接 / 何时开始」就是假话；留着
        #    `claiming_at` 还会让下一轮拿一次**已作废**的时刻算年龄）。
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertIsNone(self.mirror.claiming_at)
        self.assertEqual(self.mirror.last_error, RECLAIMED_NOTE_TEXT)
        # 台账：本线既有形状（`PeerSyncEvent` 一张表兼 outbox 与台账）。
        ledger = self._ledger_rows().get()
        self.assertEqual(ledger.direction, "in")
        self.assertEqual(ledger.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(ledger.binding_role, "upstream")
        self.assertEqual(ledger.binding_id, str(self.up_binding.pk))
        self.assertEqual(ledger.target_type, "hall")
        self.assertEqual(ledger.target_ref, self.upstream_order.order_no)
        self.assertEqual(ledger.reason, RECLAIMED_NOTE_TEXT)
        self.assertEqual(ledger.payload["stuck_seconds"], 60)
        self.assertEqual(ledger.payload["claiming_at"], self.claiming_at.isoformat())
        self.assertEqual(ledger.payload["claiming_by_id"], self.taker.pk)

    def test_a_stuck_claim_with_take_evidence_closes_as_gone(self):
        """⭐ B3 / L-8（2026-10-25 审计批）：claiming 期间 take 来过（`gone_at` 有证据）
        ⇒ 回收**收口成 `gone`**，⛔ 不再放回大厅让打手再点一次注定被拒的单。

        判别力：删掉 `gone_at` 分叉 ⇒ 卡被放回 `listed`，本用例红在状态与文案两处。
        """
        self._claim_and_age(age_seconds=61)
        evidence_at = self.now_ref - timedelta(seconds=10)
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(gone_at=evidence_at)

        self.assertEqual(self._reclaim(), 1)

        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.GONE)
        # 文案 = refused-after-take 同族常量的**字面值**（peer_inbound 的那一句）。
        self.assertEqual(
            self.mirror.last_error,
            "上家拒绝：这张单在接单确认期间已从对方大厅撤下，无法再抢",
        )
        # `gone_at` 是证据本体，恒保留最早那一次（回收不覆盖）。
        self.assertEqual(self.mirror.gone_at, evidence_at)
        # 四个接单列照旧一起清（这一跳已经收口，留着就是假话）。
        self.assertIsNone(self.mirror.claiming_by_id)
        self.assertIsNone(self.mirror.claiming_at)
        ledger = self._ledger_rows().get()
        self.assertEqual(ledger.payload["closed_gone"], True)
        self.assertEqual(
            ledger.reason,
            "上家拒绝：这张单在接单确认期间已从对方大厅撤下，无法再抢",
        )

    def test_a_stuck_claim_without_evidence_still_returns_to_listed(self):
        """⭐ B3 的另一支（正对照）：没有 `gone_at` 证据 ⇒ 现行为逐字不变（回 `listed`）。"""
        self._claim_and_age(age_seconds=61)

        self.assertEqual(self._reclaim(), 1)

        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(self.mirror.last_error, RECLAIMED_NOTE_TEXT)
        self.assertIsNone(self.mirror.gone_at)
        self.assertEqual(self._ledger_rows().get().payload["closed_gone"], False)

    def test_a_promoted_card_is_never_reclaimed(self):
        """已转正（`promoted_order` 有值）⇒ 不动：回收只看「真单还没落进来」那一档。"""
        self.share_with_downstream()          # Z20：上家真投过这张单，claim 才会被放行
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        self.deliver_assign_to_downstream()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        promoted_pk = self.mirror.promoted_order_id
        self.assertIsNotNone(promoted_pk)
        # 占住时刻推到很久以前：年龄判据成立，唯一挡住回收的就是「已转正」。
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            claiming_at=self._now() - timedelta(minutes=10)
        )
        self.assertEqual(reclaim_stuck_claims(now=self._now()), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self.mirror.promoted_order_id, promoted_pk)
        self.assertEqual(self.mirror.claiming_by_id, self.taker.pk)
        self.assertEqual(self.mirror.last_error, "")
        self.assertFalse(self._ledger_rows().exists())

    def test_a_listed_card_is_not_touched(self):
        """`listed` 的行不在判据里（哪怕它 `claiming_at` 有值也不会被回收）。"""
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            claiming_at=self._now() - timedelta(minutes=10),
            claiming_by_id=self.taker.pk,
        )
        self.assertEqual(reclaim_stuck_claims(now=self._now()), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.LISTED)
        self.assertFalse(self._ledger_rows().exists())

    def test_a_card_without_claiming_at_is_not_reclaimed(self):
        """`claiming_at` 为空 ⇒ 判不出年龄 ⇒ **不回收**（⛔ 不拿别的时刻猜）。"""
        PeerHallMirror.objects.filter(pk=self.mirror.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by_id=self.taker.pk,
            claiming_at=None,
        )
        self.assertEqual(reclaim_stuck_claims(now=self._now()), 0)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.CLAIMING)

    def test_a_reclaimed_card_can_be_claimed_again(self):
        """回收之后**能重新接一次**（这就是回收的全部意义：卡回到可接区）。"""
        self._claim_and_age(age_seconds=61)
        self.assertEqual(self._reclaim(), 1)
        row = claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(row.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(row.claiming_by_id, self.taker.pk)
        # 上行 hall.claim 排了**两次**：第一次卡死、第二次是这次真接单。
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                binding_role="upstream", kind="hall.claim"
            ).count(),
            2,
        )
        # ⚠️ 上一轮的事故原因留在行上（与 `hall.claim_refused` 那一族同口径：
        #    `last_error` 记「最近一次失败」，接单本身不清它）。
        self.assertEqual(row.last_error, RECLAIMED_NOTE_TEXT)


class PromotedReplayTests(ClaimChainFixture):
    """② 已转正的卡再被要：回「正在处理中」+ 重投一次 `order.grab`。"""

    def _promote(self):
        """走到 `promoted`（全链：接单 → 上家指派 → assign 回推 → 转正）。"""
        self.share_with_downstream()          # Z20：上家真投过这张单，claim 才会被放行
        claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.deliver_claim_to_upstream()
        self.deliver_assign_to_downstream()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertIsNotNone(self.mirror.promoted_order_id)
        # 转正单的 MAIN 行归抢单人（H1 口径）——本文件的重投判据就建在这一列上。
        self.assertEqual(
            Performance.objects.get(
                order=self.mirror.promoted_order, emp_type=EmpType.MAIN
            ).user_id,
            self.taker.pk,
        )
        return self.mirror.promoted_order

    def _downstream_binding_for(self, account, base_url):
        """把某个打手设成下游绑定的账号 ⇒ `order.grab` 才有收件人。

        ⚠️ 这是**多跳**（我方是中间层）的形状；单跳（接手人是本地打手、没有下游绑定）时
        `enqueue_order_grabbed` 回 `None`、一行都不写——两种形状都有用例钉住。
        """
        return PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url=base_url,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def test_a_promoted_card_is_answered_as_in_progress_not_as_taken(self):
        """⭐ 单跳形状：回「正在处理中」（⛔ 不是「已被其他打手接取」），零新事件。

        判别力：把这一档删掉 ⇒ 会落到「已被接走」那句（`ALREADY_TAKEN_TEXT`）⇒ 本用例红。
        """
        self._promote()
        events_before = PeerSyncEvent.objects.count()
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(str(caught.exception), IN_PROGRESS_TEXT)
        self.assertNotEqual(str(caught.exception), ALREADY_TAKEN_TEXT)
        # ORDER_ALREADY_TAKEN = 1012（钉字面值；与 `claiming` + 本人那一档**同一个**业务码：
        # 本线的既有惯用法是「码管机器分支、msg 管给人的话」）。
        self.assertEqual(caught.exception.code, 1012)
        # 单跳没有下游绑定 ⇒ 「重投」是一次空操作（⛔ 不是漏做，形状如此）。
        self.assertEqual(PeerSyncEvent.objects.count(), events_before)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)

    def test_a_promoted_card_is_reinjected_once_without_a_second_order(self):
        """⭐ 多跳形状：`order.grab` 重投**恰好一次**，⛔ 不建第二张单、⛔ 不改价。"""
        promoted = self._promote()
        binding = self._downstream_binding_for(
            self.taker, "https://peer-hall-replay.test.invalid"
        )
        orders_before = Order.objects.count()
        price_before = promoted.hall_price
        self.assertEqual(price_before, Decimal("50.00"))

        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(str(caught.exception), IN_PROGRESS_TEXT)

        grabs = PeerSyncEvent.objects.filter(kind="order.grab")
        self.assertEqual(grabs.count(), 1)
        event = grabs.get()
        self.assertEqual(event.direction, "out")
        self.assertEqual(event.binding_role, "downstream")
        self.assertEqual(event.binding_id, str(binding.pk))
        # ⭐ peer 身份统一：`order.grab` 的同步身份是**来源单号**
        # （`effective_peer_ref`），⛔ 不再是内置 `order_no`（计划 `2026-10-25-peer-source-identity`
        # §2 第 1 条）。本用例的正式单来自上家镜像，来源号与内置号不同 ⇒ 原来那句
        # `promoted.order_no` 是**旧口径**，会被这条断言当场抓出来。
        self.assertEqual(event.target_ref, effective_peer_ref(promoted))
        # ⛔ 不建第二张单：订单张数一个字没变，镜像行仍指着同一张。
        self.assertEqual(Order.objects.count(), orders_before)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(self.mirror.promoted_order_id, promoted.pk)
        # ⛔ 不改价：大厅里看到的价 = 正式单的价（P1 的展示价口径原样）。
        self.mirror.promoted_order.refresh_from_db()
        self.assertEqual(self.mirror.promoted_order.hall_price, price_before)

    def test_a_reassigned_promoted_card_is_not_mine_any_more(self):
        """⭐ 判据是 **MAIN 绩效行**（不是 `claiming_by`）：改派之后原抢单人再要 ⇒ 「已被接走」。

        判别力：把判据改成 `row.claiming_by_id == user_id` ⇒ 本用例红（那会把一张
        **已经被改派给别人**的单当成「还在受理中」）。
        """
        self._promote()
        other = _make_taker("syn-replay-other", self.game)
        perf = Performance.objects.get(
            order=self.mirror.promoted_order, emp_type=EmpType.MAIN
        )
        perf.user = other
        perf.save(update_fields=["user", "updated_at"])
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=self.taker.pk, mirror_id=self.mirror.pk)
        self.assertEqual(str(caught.exception), ALREADY_TAKEN_TEXT)
        # ⛔ 不是本人的单 ⇒ 一个 `order.grab` 都不重投。
        self.assertFalse(PeerSyncEvent.objects.filter(kind="order.grab").exists())

    def test_the_delivered_taker_is_answered_as_in_progress(self):
        """反向对照：**改派后的接手人**来要 ⇒ 「正在处理中」+ 重投（判据跟着 MAIN 走）。"""
        self._promote()
        other = _make_taker("syn-replay-new-taker", self.game)
        perf = Performance.objects.get(
            order=self.mirror.promoted_order, emp_type=EmpType.MAIN
        )
        perf.user = other
        perf.save(update_fields=["user", "updated_at"])
        binding = self._downstream_binding_for(
            other, "https://peer-hall-replay2.test.invalid"
        )
        with self.assertRaises(ClaimError) as caught:
            claim_peer_mirror(user_id=other.pk, mirror_id=self.mirror.pk)
        self.assertEqual(str(caught.exception), IN_PROGRESS_TEXT)
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                kind="order.grab", binding_id=str(binding.pk)
            ).count(),
            1,
        )


class PeerSyncLoopReclaimHookTests(TenantTestCase):
    """③ 常驻循环每轮**先跑一次**回收（纯本地），再投递 / 对账。"""

    def _stuck_mirror(self):
        """摆一张超阈值、仍在 `claiming` 的镜像行（回收的命中形状）。"""
        game = Game.objects.create(name="回收夹具游戏")
        operator = get_user_model().objects.create_user(
            username="syn-reclaim-loop-op", password="pw-syn"
        )
        binding = PeerUpstreamBinding.objects.create(
            peer_name="回收上家",
            peer_base_url="https://peer-hall-reclaim.test.invalid",
            peer_account="syn-reclaim-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-reclaim-local.test.invalid",
            bound_by=operator,
            hall_sync_enabled=True,
        )
        row = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="20261025000000079901",
            status=PeerHallMirror.Status.CLAIMING,
            game=game,
            payload={"order_no": "20261025000000079901"},
            remote_hall_price=Decimal("20.00"),
            first_seen_at=timezone.now(),
            claiming_by_id=operator.pk,
            claiming_at=timezone.now() - timedelta(minutes=6),
        )
        return row

    @staticmethod
    def _summary():
        return {"delivered": 0, "retried": 0, "failed": 0, "voided": 0,
                "revoked": 0, "errors": []}

    def test_the_loop_runs_the_reclaim_before_delivery(self):
        """顺序是承重的：先回收（让卡回到大厅），再投递（才会把它重挂回去）。"""
        from apps.business.management.commands import peer_sync as module

        order: list[str] = []
        summary = self._summary()
        reconcile = {"requeued": 0, "errors": 0}
        out = io.StringIO()

        def _reclaim():
            order.append("reclaim")
            return 3

        with mock.patch.object(
            module.peer_hall, "reclaim_stuck_claims", side_effect=_reclaim
        ), mock.patch.object(
            module.peer_worker,
            "run_round",
            side_effect=lambda: (order.append("deliver"), summary)[1],
        ), mock.patch.object(
            module.peer_worker,
            "run_reconcile",
            side_effect=lambda: (order.append("reconcile"), reconcile)[1],
        ):
            module.Command(stdout=out)._run_tenant(
                mock.Mock(schema_name="tenant-a")
            )
        self.assertEqual(order, ["reclaim", "deliver", "reconcile"])
        self.assertIn("回收卡死 3", out.getvalue())

    def test_the_loop_really_reclaims_through_the_command(self):
        """不打桩回收：命令跑一趟之后，卡死的行**真的**回到了大厅。"""
        from apps.business.management.commands import peer_sync as module

        row = self._stuck_mirror()
        out = io.StringIO()
        with mock.patch.object(
            module.peer_worker, "run_round", return_value=self._summary()
        ), mock.patch.object(
            module.peer_worker,
            "run_reconcile",
            return_value={"requeued": 0, "errors": 0},
        ):
            module.Command(stdout=out)._run_tenant(
                mock.Mock(schema_name="tenant-a")
            )
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(row.claiming_by_id)
        self.assertIsNone(row.claiming_at)
        self.assertIn("回收卡死 1", out.getvalue())
