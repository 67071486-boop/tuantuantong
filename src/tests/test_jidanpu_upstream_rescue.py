"""`rescue_gone_mirrors` 管理命令与 `sync_hall` 行循环**自动复活**（D-0300）的用例。

两个被测对象、同一套复活语义（条件更新 + 清灰卡 + `upstream.hall_rescued` 台账）：

- `RescueGoneMirrorsTests`：手工命令（YF-01 受害者恢复，2026-10-20；D-0300 起为存量批量
  兜底 / 审计工具）。判据四条：
  ① **默认 dry-run**：逐行打印候选，但**一个字不落库**（状态、`gone_at`、台账都不动）；
  ② `--execute` 只复活「`gone` 且有**晚于** `gone_at` 的终态再现台账」的行；
     **无证据**的 gone 行一动不动（它可能真没了——复活它才是制造假数据）；
  ③ **时间下界**是承重的：再现台账**早于** `gone_at`（同一张单上一次 gone 幕留下的旧证据）
     不能给这一次 gone 当证据 ⇒ 不复活；
  ④ `--binding` 只认领该绑定地址的镜像；pk 在任何租户都不存在 ⇒ 响亮 `CommandError`。

- `SyncHallRevivesGoneOnSightTests`：⭐ D-0300（业务方拍板）行循环**自动复活**
  ——`gone` 在任何一轮里重新见到即恢复 `listed`（⛔ 不要求 complete），`promoted` 仍不复活。

⚠️ 判据打**数据库行**（`AGENTS.md` §5-G），stdout 只当辅助；夹具合成值
（地址 `…test.invalid`、单号 `2026102000000000…` 起的全零段）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.jidanpu_common import (
    KIND_RESCUED,
    RESCUE_REASON,
    TERMINAL_REAPPEAR_REASON,
)
from apps.business.models import (
    Game,
    HallExitSnapshot,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
)
from tests.test_jidanpu_upstream_pull import BASE_URL, BASE_URL_B, _StubClient

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_GONE = JidanpuUpstreamHallOrder.Status.GONE
_PROMOTED = JidanpuUpstreamHallOrder.Status.PROMOTED


class RescueGoneMirrorsTests(TenantTestCase):
    def setUp(self):
        now = timezone.now()
        self.now = now
        user = _user("rescue-user")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account="acc-rescue",
            password="pw-canary-rescue", enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")
        self.schema = connection.schema_name

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _gone_mirror(self, *, order_id, base_url=BASE_URL, gone_at=None):
        """一条 `gone` 镜像行（`gone_at` 默认落在过去一小时，可传未来时刻造「第二次 gone」）。"""
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=base_url,
            upstream_order_id=order_id,
            status=_GONE,
            game=self.game,
            owner_binding=self.binding,
            first_seen_at=self.now - timedelta(hours=3),
            gone_at=gone_at or self.now - timedelta(hours=1),
        )

    def _reappear_event(self, mirror):
        """拉取轮见到终态行时会记的那条台账（`note_once` 的形状，逐字段照抄）。"""
        return JidanpuSyncEvent.objects.create(
            direction="in", kind="upstream.hall_skipped", status="skipped",
            target_type="hall",
            target_ref=(
                f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"[:191]
            ),
            reason=TERMINAL_REAPPEAR_REASON,
            payload={"upstream_binding_id": mirror.owner_binding_id},
        )

    def _run(self, *args):
        out = StringIO()
        call_command("rescue_gone_mirrors", "--schema", self.schema, *args, stdout=out)
        return out.getvalue()

    # ── ① 默认 dry-run：只打印、不落写 ─────────────────────────────────────

    def test_default_is_dry_run_and_writes_nothing(self):
        victim = self._gone_mirror(order_id="20261020000000000101")
        self._reappear_event(victim)
        out = self._run()
        self.assertIn(str(victim.upstream_order_id), out, "dry-run 也该逐行打印计划")
        victim.refresh_from_db()
        self.assertEqual(victim.status, _GONE)
        self.assertIsNotNone(victim.gone_at)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued").exists(),
            "dry-run 落了抢救台账",
        )

    # ── ② --execute：只复活有证据的 gone 行 ────────────────────────────────

    def test_execute_revives_only_rows_with_reappearance_evidence(self):
        victim = self._gone_mirror(order_id="20261020000000000101")
        self._reappear_event(victim)
        #: 无证据：真消失的行（⛔ 复活它才是制造假数据）。
        no_evidence = self._gone_mirror(order_id="20261020000000000201")
        #: 还在挂的行：命令根本不该碰它。
        listed = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261020000000000301",
            status=_LISTED, game=self.game, owner_binding=self.binding,
            first_seen_at=self.now,
        )
        self._run("--execute")
        victim.refresh_from_db()
        self.assertEqual(victim.status, _LISTED)
        self.assertIsNone(victim.gone_at)
        no_evidence.refresh_from_db()
        self.assertEqual(no_evidence.status, _GONE)
        self.assertIsNotNone(no_evidence.gone_at)
        listed.refresh_from_db()
        self.assertEqual(listed.status, _LISTED)
        #: 复活留痕恰好一条（断言打台账行，不打 stdout）。
        rescued = JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued")
        self.assertEqual(rescued.count(), 1)
        self.assertEqual(
            rescued.get().target_ref,
            f"{BASE_URL}/{victim.upstream_order_id}",
        )

    # ── ②b D-0302：证据里的时刻是项目时区墙钟 ──────────────────────────────

    def test_the_evidence_notes_stamp_moments_in_the_project_timezone(self):
        """D-0302（2026-10-24）：dry-run 人话与台账 payload 里的证据时刻都是**项目时区**墙钟。

        走「只有 last_seen_at 见证」那一档（通道②）：`last_seen_at` 固定 UTC 04:30 ⇒
        北京墙钟 12:30。旧写法 stdout 裸 `str()` 出 `…+00:00`、payload 裸 `isoformat()`
        出 UTC 串——同步事件页与救回人话上差 8 小时。夹具合成值（E-0077）。
        """
        from datetime import datetime as dt, timedelta as td, timezone as tz

        fixed_seen = dt(2026, 10, 24, 4, 30, tzinfo=tz.utc)
        victim = self._gone_mirror(
            order_id="20261020000000000501",
            gone_at=fixed_seen - td(hours=1),
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=victim.pk).update(last_seen_at=fixed_seen)
        # dry-run：人话里是北京墙钟（⛔ 不是 `…+00:00` 的 UTC 形状）。
        out = self._run()
        self.assertIn("last_seen_at=2026-10-24 12:30:00 见证", out)
        self.assertNotIn("+00:00", out)
        # execute：台账 payload 的证据时刻 = `local_iso`（+08:00）。
        self._run("--execute")
        rescued = JidanpuSyncEvent.objects.get(kind="upstream.hall_rescued")
        self.assertEqual(rescued.payload["evidence_last_seen_at"], "2026-10-24T12:30:00+08:00")

    # ── ③ 时间下界：旧 gone 幕的台账不当新证据 ─────────────────────────────

    def test_evidence_older_than_the_current_gone_at_is_not_accepted(self):
        mirror = self._gone_mirror(order_id="20261020000000000401")
        self._reappear_event(mirror)
        #: 第二次 gone 幕：`gone_at` 被推到台账**之后**（同一张单判 gone → 重现 → 又一次 gone）。
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            gone_at=timezone.now() + timedelta(hours=1)
        )
        self._run("--execute")
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _GONE, "旧幕的再现台账被当成了新证据")
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued").exists()
        )

    # ── ③' R1：救回过一次的行，第二幕必须还能救 ─────────────────

    def test_second_gone_episode_after_a_rescue_is_rescuable_again(self):
        """gone → 救回 → 再 gone → 再重现 ⇒ dry-run **看得到**它、--execute **再救得回**它。

        为什么单靠台账永远筛空（R1）：再现台账的唯一写口 `note_once` 按
        `(kind, target_type, target_ref, reason)` 去重**永只一行**——能被救回的行必然
        已有第 1 幕台账 ⇒ 第 2 幕的再现台账被去重**吞掉** ⇒ 只认台账时，第二幕起
        dry-run 静默显 0 条、行永久冻结。第二通道 = 镜像自己的 `last_seen_at`
        （终态再现分支每轮 `_touch` 刷新、不受台账去重限制）——两通道任一满足即可。
        """
        mirror = self._gone_mirror(order_id="20261020000000000601")
        self._reappear_event(mirror)  # 第 1 幕证据：台账 + 触发当时的 `_touch`
        self._run("--execute")
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED, "前提：第一幕被救回")

        #: 第二幕：再次判 gone（gone_at 推到第 1 幕台账之后）。
        second_gone_at = timezone.now() + timedelta(hours=2)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            status=_GONE, gone_at=second_gone_at,
        )
        #: 第二幕再现：台账被 note_once 去重吞掉 ⇒ **没有**新台账行；只有 `_touch`
        #: 留下的 `last_seen_at`（这正是 R1 点名的形状，照实模拟）。
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            last_seen_at=second_gone_at + timedelta(minutes=5),
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped", target_type="hall",
                reason=TERMINAL_REAPPEAR_REASON,
                target_ref=f"{BASE_URL}/{mirror.upstream_order_id}",
                created_at__gte=second_gone_at,
            ).exists(),
            "前提不成立：第二幕真的没有新台账（否则这条测的不是 R1）",
        )
        out = self._run()  # dry-run：修前静默显 0 条 ⇒ 红在这
        self.assertIn(
            str(mirror.upstream_order_id), out, "第二幕在 dry-run 里被静默筛空"
        )
        self._run("--execute")
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED)
        self.assertIsNone(mirror.gone_at)

    def test_last_seen_before_the_current_gone_at_is_not_accepted(self):
        """第二通道同样带时间下界：`last_seen_at` 早于本次 `gone_at`（判 gone 之后再没见过）
        ⇒ 不救（它可能真没了；复活才是制造假数据）。"""
        mirror = self._gone_mirror(order_id="20261020000000000701")
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            #: gone_at = 1 小时前；最后一次见到在 2 小时前 = 判 gone **之前**。
            last_seen_at=self.now - timedelta(hours=2),
        )
        out = self._run("--execute")
        self.assertNotIn(str(mirror.upstream_order_id), out)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _GONE)

    # ── ④ --binding 过滤 + 未知的 pk 响亮报错 ──────────────────────────────

    def test_binding_filter_only_claims_that_address(self):
        other_user = _user("rescue-user-b")
        other_binding = JidanpuUpstreamBinding.objects.create(
            user=other_user, jidanpu_base_url=BASE_URL_B, account="acc-rescue-b",
            password="pw-canary-rescue-b", enabled=True,
        )
        victim_a = self._gone_mirror(order_id="20261020000000000501")
        self._reappear_event(victim_a)
        victim_b = self._gone_mirror(
            order_id="20261020000000000502", base_url=BASE_URL_B,
        )
        self._reappear_event(victim_b)
        self._run("--execute", "--binding", str(other_binding.pk))
        victim_a.refresh_from_db()
        self.assertEqual(victim_a.status, _GONE, "--binding 没拦住别家地址的行")
        victim_b.refresh_from_db()
        self.assertEqual(victim_b.status, _LISTED)

    def test_unknown_binding_pk_is_a_loud_command_error(self):
        with self.assertRaises(CommandError):
            self._run("--execute", "--binding", "999999")


def _user(name: str):
    from django.contrib.auth.models import User

    return User.objects.create_user(username=name, password=f"pw-{name}")


class SyncHallRevivesGoneOnSightTests(TenantTestCase):
    """⭐ D-0300（2026-10-24 业务方拍板）：`sync_hall` 行循环的**自动复活**（D-0366 后的形状）。

    判据五条（复活**不要求本轮 complete** 仍是承重判据；⛔ D-0366 起行来源只有逐游戏段查）：
      ① 完整轮（该游戏段判全）段里重新见到 → 复活：状态 / `gone_at` / 投影 /
         `upstream.hall_rescued` 台账 / 灰卡快照清理，五处都钉在数据库行上；
      ② 不完整轮（**另一只游戏**的段查询失败 ⇒ `complete=False`）段里重新见到 → **仍复活**
         （「看见了」是直接证据，与 `_mark_gone` 只在拉全轮跑的不对称是有意的）；
      ③ 段**成功回 0 行**：gone 行不在本轮行里 ⇒ **不复活**；它已终态 ⇒ `_mark_gone`
         的条件更新抢不到它（rowcount=0）⇒ 静默保持 gone、零台账；
      ④ `promoted` 仍终态不复活：状态不动、只记「终态再现」台账
         （promoted 行已转成我方正式订单，复活会造成重复接单，这条口子不开）；
      ⑤ 复活不是免死金牌：复活后下一轮完整扫里真不在上游 → 照 D-0366 的 `_mark_gone`
         判据（段查复核仍不见）正常判 gone。

    ⚠️ 复用 `test_jidanpu_upstream_pull` 的 `_StubClient`（零网络；逐游戏分流见
    `per_game_rows` / `fail_game_names`）；夹具合成值（单号 `2026102400000000…` 全零段）。
    """

    def setUp(self):
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=dt_timezone.utc)
        user = _user("revive-user")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account="acc-revive",
            password="pw-canary-revive", enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")
        #: 第二只游戏：②需要「另一段失败 ⇒ 轮不完整」的来源（⛔ 本行轮里**每一只**
        #: 我方游戏都会被段查一次——它的段不在夹具里 ⇒ 成功回 0 行 = 有证据，不影响别的断言）。
        self.game_b = Game.objects.create(name="测试游戏乙")

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _gone_mirror(self, order_id: str) -> JidanpuUpstreamHallOrder:
        """一条 `gone` 镜像行（旧投影 + 一小时前判的 gone）。"""
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=order_id,
            status=_GONE,
            game=self.game,
            owner_binding=self.binding,
            project_text="判 gone 前的旧投影",
            first_seen_at=self.now - timedelta(hours=3),
            gone_at=self.now - timedelta(hours=1),
        )

    def _hall_row(self, order_id: str) -> dict:
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "gameName": "测试游戏甲",
            "gameArea": "安卓", "gameServer": "微信区",
            "project": "本轮刷新的投影", "orderRemark": "合成备注", "roleName": "合成角色",
            "recFee": "66.00", "createTime": "2026-10-24 11:00:00",
        }

    def _exit_snapshot(self, mirror) -> HallExitSnapshot:
        """一条灰卡快照（D-0296 的形状；判 gone 那一刻留下的）。"""
        return HallExitSnapshot.objects.create(
            mirror=mirror, source=HallExitSnapshot.Source.UPSTREAM,
            exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN, game=self.game,
            payload={"order_no": f"V-{mirror.upstream_order_id}"},
            exited_at=self.now - timedelta(hours=1),
        )

    # ── ① 完整轮：复活，五处判据 ───────────────────────────────────────────

    def test_a_complete_round_revives_the_gone_row_and_clears_the_gray_card(self):
        mirror = self._gone_mirror("20261024000000000101")
        self._exit_snapshot(mirror)
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [self._hall_row(mirror.upstream_order_id)]},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED)
        self.assertIsNone(mirror.gone_at)
        self.assertEqual(mirror.project_text, "本轮刷新的投影")   # 投影按本轮行内容刷新
        self.assertEqual(mirror.last_seen_at, self.now)
        self.assertEqual(mirror.owner_binding_id, self.binding.pk)
        self.assertEqual(outcome.listed, 1)
        rescued = JidanpuSyncEvent.objects.filter(kind=KIND_RESCUED)
        self.assertEqual(rescued.count(), 1)
        event = rescued.get()
        self.assertEqual(event.target_ref, f"{BASE_URL}/{mirror.upstream_order_id}")
        self.assertEqual(event.reason, RESCUE_REASON)
        self.assertEqual(event.status, "done")
        self.assertEqual(
            HallExitSnapshot.objects.count(), 0, "灰卡没被清（同一张单会一单双卡）"
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped",
                target_ref=f"{BASE_URL}/{mirror.upstream_order_id}",
                reason=TERMINAL_REAPPEAR_REASON,
            ).exists(),
            "复活分支不该再写「终态再现」台账（upstream.hall_rescued 就是记录）",
        )

    # ── ② 不完整轮（另一段失败）：仍复活 ───────────────────────────────────

    def test_an_incomplete_round_still_revives(self):
        """乙段查询失败 ⇒ `complete=False`，甲段里见到的 gone 行**仍复活**。"""
        mirror = self._gone_mirror("20261024000000000201")
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [self._hall_row(mirror.upstream_order_id)]},
            fail_game_names={"测试游戏乙"},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete, "前提：这一轮因乙段查询失败判了不完整")
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED, "不完整轮也该复活（「看见了」就是直接证据）")
        self.assertIsNone(mirror.gone_at)
        self.assertTrue(JidanpuSyncEvent.objects.filter(kind=KIND_RESCUED).exists())

    # ── ③ 段成功回 0 行：不复活、也不判任何东西 ───────────────────────────

    def test_a_zero_row_segment_neither_revives_nor_touches_the_gone_row(self):
        """甲段成功回 0 行（D-0366：= 有证据）⇒ 该 gone 行不在本轮行里 ⇒ 不复活；
        它已终态 ⇒ `_mark_gone` 的条件更新抢不到（rowcount=0）⇒ 静默保持、零台账。"""
        mirror = self._gone_mirror("20261024000000000301")
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [], "测试游戏乙": []},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _GONE, "段里没见到的 gone 行不许被复活")
        self.assertEqual(mirror.gone_at, self.now - timedelta(hours=1))  # 原时刻不动
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists(),
            "对已 gone 的行不该再落一条「已消失」台账（条件更新抢不到就不算）",
        )
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind=KIND_RESCUED).exists())

    # ── ④ promoted：仍不复活 ───────────────────────────────────────────────

    def test_a_promoted_row_stays_promoted_and_only_leaves_a_note(self):
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000000401",
            status=_PROMOTED, game=self.game, owner_binding=self.binding,
            first_seen_at=self.now - timedelta(hours=3),
        )
        stub = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [self._hall_row(mirror.upstream_order_id)]},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        self.assertEqual(
            mirror.status, _PROMOTED, "promoted 被复活了（重复接单的口子不能开）"
        )
        self.assertEqual(mirror.last_seen_at, self.now)   # 只碰「最近仍在大厅」
        self.assertEqual(outcome.listed, 0)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped",
                target_ref=f"{BASE_URL}/{mirror.upstream_order_id}",
                reason=TERMINAL_REAPPEAR_REASON,
            ).exists(),
            "promoted 再现仍只记「终态再现」台账",
        )
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind=KIND_RESCUED).exists())

    # ── ⑤ 复活不是免死金牌：下一轮真不在上游照旧判 gone ────────────────────

    def test_a_revived_row_can_go_gone_again_when_really_absent(self):
        """复活后下一轮完整扫里真不在上游 ⇒ 照 D-0366 的 `_mark_gone`（段查复核仍不见）判 gone。"""
        mirror = self._gone_mirror("20261024000000000501")
        stub1 = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [self._hall_row(mirror.upstream_order_id)]},
        )
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub1, now=self.now)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _LISTED, "前提：第一轮被复活")

        #: 第二轮：它不在了（该段有**别的**单在挂 ⇒ 不是 0 行段），
        #: 段查复核（D-0366）重查该段也确实不见它 ⇒ 判 gone。
        later = self.now + timedelta(hours=1)
        survivor_row = self._hall_row("20261024000000000502")
        stub2 = _StubClient(
            account=self.binding.account,
            per_game_rows={"测试游戏甲": [survivor_row], "测试游戏乙": []},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub2, now=later)
        self.assertTrue(outcome.complete)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, _GONE)
        self.assertEqual(mirror.gone_at, later)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )
