"""上家线 D-0366（2026-11-03，E-0314）：消失判定的**证据边界**——YF-01 删除后的 fail-closed 网。

背景（E-0314 生产实锤）：竹屿整厅口恒回 1 张、竹屿/初音/团团工作室三家 `key1` 复核对
**在厅活卡**也查无 ⇒ 旧「无游戏名全量路 + key1 逐单回查」两块基石在这类上家身上全部失真；
初音活卡 7 天被误标 gone 5 次（churn 台账实锤）。业务方拍板「以后别用空游戏名查」⇒
D-0366 改判：

- **候选** = `listed` 且不在本轮段行 `seen` 里，且**其游戏的段查询本轮成功**
  （⭐ **含 0 行成功**——YF-01 的 success-empty 排除**删除**：「成功回 0 行」= 有证据；
  「查询失败 / D-0321 跳过态」仍无证据，不进候选——fail-closed 对「失败」档保留）；
- **复核** = 该游戏段**重新查一次**（新鲜请求；同游戏多候选只复核一次）：复核仍不见
  ⇒ 才落 `gone`；复核失败 ⇒ 本轮跳过该候选（宁漏标勿误标不变）；
- ⛔ D-0258 的 `key1` 逐单回查删除（三家用它对在厅活卡盲查，它就是 YF-01 时代假保险的
  反面：盲查「查无」反把真活卡误标——初音 7 天 5 次）。

判据打**数据库行**与桩的调用集合（`AGENTS.md` §5-G），⛔ 不打 stdout。
⚠️ 夹具一律合成值（E-0077）：地址 `…test.invalid`、单号 `2026110300000000…` 全零段；
游戏名沿用本目录「测试游戏甲 / 乙」的形状口径。
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.models import (
    Game,
    HallExitSnapshot,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
)
from apps.business.order_no import generate_order_no
from tests.test_jidanpu_upstream_pull import BASE_URL, _StubClient

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_GONE = JidanpuUpstreamHallOrder.Status.GONE


class PerGameEvidenceFailClosedTests(TenantTestCase):
    """D-0366 的消失判定证据边界（候选门槛 + 段查复核 + 复核失败跳过）。"""

    def setUp(self):
        self.now = datetime(2026, 11, 3, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="up-evid", password="pw-evid")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-evid",
            password="pw-canary-evid", enabled=True,
        )
        #: victim 段的游戏（上游对它成功回 0 行）；another 段的游戏（上游对它有单）。
        self.game_victim = Game.objects.create(name="测试游戏甲")
        self.game_another = Game.objects.create(name="测试游戏乙")

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _hall_row(self, *, order_id, game_name):
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "gameName": game_name,
            "gameArea": "安卓", "gameServer": "微信区",
            "project": "合成项目", "orderRemark": "合成备注", "roleName": "合成角色",
            "recFee": "88.00", "createTime": "2026-11-03 11:46:54",
        }

    def _mirror(self, *, order_id, game, first_seen_at=None):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=order_id,
            status=_LISTED,
            game=game,
            owner_binding=self.binding,
            first_seen_at=first_seen_at or self.now,
        )

    def _shadow_order(self, mirror):
        """这张卡的一张派生隐藏单（`WAITING_GRAB`；gone 全链应把它翻 `REFUNDED`）。"""
        return Order.objects.create(
            order_no=generate_order_no(),
            game=mirror.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("88.00"),
            hall_price=Decimal("74.80"),
            upstream_hall_card=mirror,
        )

    # ── ① 0 行成功段 = 有证据：候选成立 → 复核仍无 → gone 全链 ─────────────

    def test_zero_row_segment_is_evidence_and_the_gone_chain_runs(self):
        """⭐ YF-01 删除的主证：甲段成功回 0 行 ⇒ 名下在挂镜像进候选；段查复核（新鲜
        第 2 问）仍不见 ⇒ 落 `gone`，且台账 / 灰卡快照 / 隐藏单退款**全链**各有一处实证。
        修前（YF-01）：这个段被 success-empty 排除 ⇒ 滞留单**永标不掉**（E-0314 现场形态）。
        """
        victim = self._mirror(order_id="20261103000000000101", game=self.game_victim)
        shadow = self._shadow_order(victim)
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [], "测试游戏乙": []},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 1)
        victim.refresh_from_db()
        self.assertEqual(victim.status, _GONE, "0 行成功段的滞留镜像没被标 gone（YF-01 还在）")
        self.assertEqual(victim.gone_at, self.now)
        # 台账：`upstream.hall_gone` 一条，target_ref 带它的单号。
        ledger = JidanpuSyncEvent.objects.get(kind="upstream.hall_gone")
        self.assertEqual(ledger.target_ref, f"{BASE_URL}/{victim.upstream_order_id}")
        self.assertEqual(ledger.status, "done")
        # 灰卡快照：离厅（WITHDRAWN）一张。
        self.assertTrue(
            HallExitSnapshot.objects.filter(
                mirror=victim, exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN
            ).exists(),
            "gone 全链没有写离厅快照",
        )
        # 隐藏单退款：WAITING_GRAB → REFUNDED（`upstream_hall_card` 不清——退掉的派生单
        # 仍属隐藏面）。
        shadow.refresh_from_db()
        self.assertEqual(shadow.status, OrderStatus.REFUNDED)
        self.assertIsNotNone(shadow.upstream_hall_card_id)
        # 复核 = 新鲜段查：甲段被问了第 2 次（第 1 问 = 本轮拉取，第 2 问 = 复核）。
        self.assertEqual(stub.game_asks.get("测试游戏甲"), 2, "段查复核没有作为新请求发生")

    # ── ② 段查询失败 ⇒ 本轮无证据，不进候选、零台账 ────────────────────────

    def test_a_failed_segment_round_never_marks_anything_gone(self):
        """乙段查询失败 ⇒ 整轮 `complete=False`、`_mark_gone` 不跑 ⇒ 名下在挂镜像原地不动。"""
        victim = self._mirror(order_id="20261103000000000201", game=self.game_another)
        stub = _StubClient(
            account=self.binding.account,
            rows=[self._hall_row(order_id="20261103000000000251", game_name="测试游戏甲")],
            fail_game_names={"测试游戏乙"},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete, "前提：这一轮因乙段失败判了不完整")
        self.assertEqual(outcome.gone, 0)
        victim.refresh_from_db()
        self.assertEqual(victim.status, _LISTED, "失败段的在挂镜像被动了")
        self.assertIsNone(victim.gone_at)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    # ── ③ 复核查询失败 ⇒ 跳过该候选（宁漏标勿误标，D-0366 保留）────────────

    def test_a_failing_recheck_skips_the_candidate_instead_of_marking_it_gone(self):
        """甲段本轮成功（第 1 问，含别的单）⇒ 候选成立；复核（第 2 问）失败 ⇒ 跳过。

        与①的分界：①是「复核答上来了、答的是仍不见」；③是「复核没答上来」——
        两档结论相反，缺一就是把「拿不到证据」错当「确认没有」。
        """
        victim = self._mirror(order_id="20261103000000000301", game=self.game_victim)
        stub = _StubClient(
            account=self.binding.account,
            rows=[self._hall_row(order_id="20261103000000000351", game_name="测试游戏甲")],
            fail_game_names_from_ask={"测试游戏甲": 2},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 0)
        victim.refresh_from_db()
        self.assertEqual(victim.status, _LISTED)
        self.assertIsNone(victim.gone_at)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    # ── ③b 复核**答了但判不了拉全** ⇒ 同样跳过（结构档：壳里没有可用 count）──

    def test_an_unusable_recheck_count_skips_too(self):
        """复核那一跳**回了行但壳里没有可用 count** ⇒ `_round_is_complete` 判 None
        ⇒ 复核结论不可信 ⇒ 跳过候选，⛔ 不许拿「本页行数」当 count 编一个数。

        与③的分界：③是复核**没答上来**（异常）；③b 是复核**答了、但「拉全」判不了**
        （结构档——docstring 点名的三个档之一；撞页上限/到数后探页有行两档殊途同归，
        都收口在同一个 `_round_is_complete` 判 None 上）。
        """
        victim = self._mirror(order_id="20261103000000000361", game=self.game_victim)
        stub = _StubClient(
            account=self.binding.account,
            rows=[self._hall_row(order_id="20261103000000000362", game_name="测试游戏甲")],
            count_missing_from_ask={"测试游戏甲": 2},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete, "前提：本轮段查（第 1 问）成功且带 count")
        self.assertEqual(outcome.gone, 0)
        victim.refresh_from_db()
        self.assertEqual(victim.status, _LISTED)
        self.assertIsNone(victim.gone_at)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    # ── ④ D-0321 跳过态游戏的镜像不进候选（两决策的交界面）──────────────────

    def test_skip_state_game_mirrors_are_not_candidates(self):
        """乙处于 D-0321 跳过态（本轮没查）⇒ 名下在挂镜像本轮**无证据**、不进候选；
        甲段正常 ⇒ 甲名下真消失的照常判 gone——门槛**没有**扩大化。
        """
        in_skip_state = self._mirror(
            order_id="20261103000000000401", game=self.game_another,
        )
        really_gone = self._mirror(order_id="20261103000000000402", game=self.game_victim)
        survivor_row = self._hall_row(
            order_id="20261103000000000451", game_name="测试游戏甲",
        )
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [survivor_row]},
        )
        skip_states = jidanpu_upstream._ZERO_ROW_GAME_SKIP.setdefault(
            jidanpu_upstream._hall_round_key(self.binding), {}
        )
        skip_states[self.game_another.pk] = jidanpu_upstream._ZeroRowGameSkip(
            consecutive_failures=3, skip=True, last_probe_at=self.now,
        )
        try:
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub, now=self.now
            )
        finally:
            jidanpu_upstream._ZERO_ROW_GAME_SKIP.clear()
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 1)
        in_skip_state.refresh_from_db()
        self.assertEqual(in_skip_state.status, _LISTED, "跳过态游戏的镜像进了消失候选")
        self.assertIsNone(in_skip_state.gone_at)
        really_gone.refresh_from_db()
        self.assertEqual(really_gone.status, _GONE, "门槛扩大化：正常段的真消失没被判")
        # 跳过态游戏本轮一次都没被查（无证据的来源就是这个）。
        self.assertEqual(
            [q for _m, _p, q in stub.calls if q.get("gameName") == "测试游戏乙"],
            [],
        )
        gone_refs = set(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone")
            .values_list("target_ref", flat=True)
        )
        self.assertEqual(gone_refs, {f"{BASE_URL}/{really_gone.upstream_order_id}"})
