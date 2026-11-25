"""`prune_sync_events`（审计批 C10 / M-11，2026-10-25；**D-0349 扩展，2026-10-31**）：台账保留期清理的判据。

## 判据打在哪（`AGENTS.md` §5-G）

- **dry-run 零删除**：跑完逐表数行，一条都不许少（破坏性动作的第一道闸）；
- **`--commit` 按**同一把 WHERE**删**：哪几行该活、哪几行该死逐行点名（终态 + 超期 +
  非证据行），证据行（`hall.pull` 全部 / `hall.throw` done 空原因 / `peer.hall_hop_limit`）
  与在途行（`pending`）、窗口内行**全部存活**；
- **D-0349（业务方拍板）**：jidanpu 默认保留期 90→**30 天**（同龄行两表不同命——
  45 天的 jidanpu 终态行默认删、45 天的 peer 终态行默认留）；**恰在保留期边界上的行不删**
  （判据是严格 `<`）；**分批删除**（`BATCH_SIZE` 调小也必须删干净 = 循环真的在循环）；
  重跑 **0 行**（幂等）；
- **两租户 schema 都跑得动**：第二个租户在 `setUpClass` 建（schema DDL 不能进用例事务），
  命令的逐 schema 循环在两个库里各清各的（A 库删除不动 B 库的行）。
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import JidanpuSyncEvent, PeerSyncEvent
from apps.platform.provisioning import provision_tenant, seed_tenant

SCHEMA_B = "prune_tenant_b"
DOMAIN_B = "prune-b.test.com"

#: 「老」= 保留期窗口之外（默认 90 天）；「新」= 窗口之内。
OLD = timezone.now() - timedelta(days=120)
NEW = timezone.now() - timedelta(days=10)


def _peer(**over):
    data = {
        "binding_role": "downstream", "binding_id": "1", "direction": "out",
        "kind": "order.assign", "target_type": "order", "target_ref": "REF-1",
        "status": PeerSyncEvent.Status.DONE, "reason": "",
        "payload": {}, "idempotency_key": "k-default",
    }
    data.update(over)
    return PeerSyncEvent.objects.create(**data)


def _jidanpu(**over):
    data = {
        "direction": "out", "kind": "order.create", "target_type": "order",
        "target_ref": "20260101000000000001",
        "status": JidanpuSyncEvent.Status.DONE, "reason": "",
    }
    data.update(over)
    return JidanpuSyncEvent.objects.create(**data)


def _age(row, created_at):
    """`created_at` 是 `auto_now_add` ⇒ 落库后用一条 UPDATE 摆到想要的年代。"""
    type(row).objects.filter(pk=row.pk).update(created_at=created_at)


class PruneSyncEventsTests(TenantTestCase):
    def setUp(self):
        # 六行 peer：三行可删、三行必须活（证据 / 在途 / 窗口内）。
        _age(_peer(kind="order.assign", idempotency_key="k-old-done"), OLD)
        _age(_peer(kind="order.withdraw", idempotency_key="k-old-failed",
                   status=PeerSyncEvent.Status.FAILED, reason="对端没回话"), OLD)
        _age(_peer(kind="hall.throw", idempotency_key="k-old-voided",
                   status=PeerSyncEvent.Status.DONE, reason="已被 take 作废"), OLD)
        # ── 必须活的三行 ──
        _age(_peer(kind="hall.throw", idempotency_key="k-evidence"), OLD)  # done + 空原因 = 推送证据
        _age(_peer(kind="hall.pull", direction="in", binding_role="downstream",
                   idempotency_key="k-pull"), OLD)                      # 拉取证据
        _age(_peer(kind="peer.hall_hop_limit", direction="in",
                   idempotency_key="k-hop"), OLD)                       # 跳数台账
        _age(_peer(kind="order.assign", idempotency_key="k-pending",
                   status=PeerSyncEvent.Status.PENDING), OLD)           # 在途
        _age(_peer(kind="order.assign", idempotency_key="k-fresh"), NEW)  # 窗口内
        # jidanpu 侧：老 done / 老 skipped 可删；老 pending 与新 done 必须活。
        _age(_jidanpu(target_ref="JD-OLD-DONE"), OLD)
        _age(_jidanpu(target_ref="JD-OLD-SKIPPED",
                      status=JidanpuSyncEvent.Status.SKIPPED,
                      reason="游戏四级匹配不上"), OLD)
        _age(_jidanpu(target_ref="JD-OLD-PENDING",
                      status=JidanpuSyncEvent.Status.PENDING), OLD)
        _age(_jidanpu(target_ref="JD-NEW-DONE"), NEW)

    # ── dry-run（默认）──────────────────────────────────────────────────
    def test_dry_run_is_the_default_and_deletes_nothing(self):
        before_peer = PeerSyncEvent.objects.count()
        before_jdp = JidanpuSyncEvent.objects.count()
        out = StringIO()
        call_command("prune_sync_events", stdout=out)
        self.assertEqual(PeerSyncEvent.objects.count(), before_peer)
        self.assertEqual(JidanpuSyncEvent.objects.count(), before_jdp)
        self.assertIn("dry-run", out.getvalue())
        self.assertIn("命中 3 条", out.getvalue())       # peer：老 done / 老 failed / 老 voided
        self.assertIn("命中 2 条", out.getvalue())       # jidanpu：老 done / 老 skipped

    # ── --commit ───────────────────────────────────────────────────────
    def test_commit_deletes_exactly_the_terminal_expired_rows(self):
        out = StringIO()
        call_command("prune_sync_events", "--commit", stdout=out)
        # 可删的恰好没了（按幂等键点名，⛔ 不按 pk）。
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                idempotency_key__in=["k-old-done", "k-old-failed", "k-old-voided"]
            ).exists()
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                target_ref__in=["JD-OLD-DONE", "JD-OLD-SKIPPED"]
            ).exists()
        )
        # 必须活的逐行点名：证据三行 + 在途 + 窗口内。
        for key in ("k-evidence", "k-pull", "k-hop", "k-pending", "k-fresh"):
            self.assertTrue(
                PeerSyncEvent.objects.filter(idempotency_key=key).exists(), key
            )
        for ref in ("JD-OLD-PENDING", "JD-NEW-DONE"):
            self.assertTrue(
                JidanpuSyncEvent.objects.filter(target_ref=ref).exists(), ref
            )
        self.assertEqual(PeerSyncEvent.objects.count(), 5)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 2)
        self.assertIn("已删 peer 3", out.getvalue())
        self.assertIn("jidanpu 2", out.getvalue())

    # ── 保留期与参数 ────────────────────────────────────────────────────
    def test_days_parameter_moves_the_window(self):
        _age(_peer(kind="order.update", idempotency_key="k-100d"), OLD)
        call_command("prune_sync_events", "--commit", "--days", "180", stdout=StringIO())
        # 120 天的行在 180 天保留期里 ⇒ 活。
        self.assertTrue(
            PeerSyncEvent.objects.filter(idempotency_key="k-100d").exists()
        )

    def test_days_below_one_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command("prune_sync_events", "--days", "0")

    def test_an_unknown_tenant_schema_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("prune_sync_events", "--tenant", "no-such-schema")

    def test_retention_defaults_peer_ninety_jidanpu_thirty(self):
        """保留期默认：peer 90 天（M-11 原口径）· jidanpu **30 天**（D-0349 拍板）。

        两个常量各自有出处、各自被这条钉住：改任何一个（或把 jidanpu 偷偷改回 90）
        ⇒ 本用例红。
        """
        from apps.business.management.commands.prune_sync_events import (
            DEFAULT_RETENTION_DAYS,
            JIDANPU_DEFAULT_RETENTION_DAYS,
        )

        self.assertEqual(DEFAULT_RETENTION_DAYS, 90)
        self.assertEqual(JIDANPU_DEFAULT_RETENTION_DAYS, 30)

    def test_same_age_rows_meet_different_fates_across_the_two_tables(self):
        """同龄不同命（D-0349 的行为钉）：45 天的 jidanpu 终态行**默认删**、45 天的
        peer 终态行**默认留**（peer 窗口仍是 90 天）。

        失败方向：把 jidanpu 默认改回 90（45 天行活下来）或把 peer 默认改成 30
        （45 天 peer 行被删）⇒ 本用例红。
        """
        mid = timezone.now() - timedelta(days=45)
        _age(_peer(kind="order.update", idempotency_key="k-mid-peer"), mid)
        _age(_jidanpu(target_ref="JD-MID-DONE"), mid)
        call_command("prune_sync_events", "--commit", stdout=StringIO())
        self.assertTrue(
            PeerSyncEvent.objects.filter(idempotency_key="k-mid-peer").exists(),
            "45 天的 peer 终态行应在默认（90 天）窗口内存活",
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(target_ref="JD-MID-DONE").exists(),
            "45 天的 jidanpu 终态行应被默认（30 天）窗口删掉（D-0349）",
        )

    def test_explicit_days_overrides_both_tables(self):
        """`--days 90` 显式给出时**两张表同用 90**：45 天的 jidanpu 行这次活下来；
        随后不带 `--days` 的默认跑把它删掉（默认 ≠ 显式，两条都钉住）。
        """
        mid = timezone.now() - timedelta(days=45)
        _age(_jidanpu(target_ref="JD-MID-EXPLICIT"), mid)
        call_command(
            "prune_sync_events", "--commit", "--days", "90", stdout=StringIO()
        )
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(target_ref="JD-MID-EXPLICIT").exists(),
            "--days 90 显式窗口下 45 天的行应存活（覆盖对两张表生效）",
        )
        call_command("prune_sync_events", "--commit", stdout=StringIO())
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(target_ref="JD-MID-EXPLICIT").exists(),
            "回到默认（jidanpu 30 天）后 45 天的行应被删",
        )

    def test_a_jidanpu_row_exactly_at_the_cutoff_is_not_deleted(self):
        """**恰 30 天整的不删**：判据是严格小于（`created_at < cutoff`），不是 `<=`。

        把命令的钟钉在 `NOW`（patch 它**被查找的模块属性**，E-0029 同族），候选边界
        恰好落在 `NOW - 30d`：整点行活、早 1 秒的行死——同一把 WHERE，只差一秒。
        """
        now = timezone.now()
        edge = now - timedelta(days=30)
        _age(_jidanpu(target_ref="JD-EDGE-EXACT"), edge)
        _age(_jidanpu(target_ref="JD-EDGE-OUT"), edge - timedelta(seconds=1))
        with mock.patch("django.utils.timezone.now", return_value=now):
            call_command("prune_sync_events", "--commit", stdout=StringIO())
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(target_ref="JD-EDGE-EXACT").exists(),
            "恰好卡在保留期边界上的行不许删（created_at < cutoff 是严格小于）",
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(target_ref="JD-EDGE-OUT").exists(),
            "比边界早 1 秒的行应被删（窗口外）",
        )

    def test_explicit_dry_run_flag_wins_over_commit(self):
        """`--dry-run` 与 `--commit` 同给时**以 dry-run 为准**（零写入优先，矛盾时不猜）。"""
        before = JidanpuSyncEvent.objects.count()
        out = StringIO()
        call_command("prune_sync_events", "--commit", "--dry-run", stdout=out)
        self.assertEqual(JidanpuSyncEvent.objects.count(), before)
        self.assertIn("dry-run", out.getvalue())

    def test_dry_run_prints_the_oldest_and_newest_candidate_times(self):
        """dry-run（与实跑同款）要报**将删行数 + 最老 / 最新**时刻（D-0349 的口径）；
        没有候选时报「—」（不编时间）。"""
        out = StringIO()
        call_command("prune_sync_events", stdout=out)
        text = out.getvalue()
        self.assertIn("最老", text)
        self.assertIn("最新", text)
        out_empty = StringIO()
        call_command("prune_sync_events", "--days", "365", stdout=out_empty)
        self.assertIn("最老 —", out_empty.getvalue())

    def test_commit_is_idempotent_rerun_deletes_zero(self):
        """幂等（timer 每天跑 + `Persistent=true` 补跑的承重判据）：重跑 0 行。"""
        call_command("prune_sync_events", "--commit", stdout=StringIO())
        after_first = (
            PeerSyncEvent.objects.count(),
            JidanpuSyncEvent.objects.count(),
        )
        out = StringIO()
        call_command("prune_sync_events", "--commit", stdout=out)
        after_second = (
            PeerSyncEvent.objects.count(),
            JidanpuSyncEvent.objects.count(),
        )
        self.assertEqual(after_first, after_second)
        self.assertIn("peer 0 条", out.getvalue())
        self.assertIn("jidanpu 0 条", out.getvalue())


class PruneSyncEventsTwoSchemaTests(TenantTestCase):
    """逐 schema 循环：第二个租户库里的行**只按它自己的条件**清，A 不替 B 做主。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="台账清理租户 B", domain=DOMAIN_B
        )
        # ⚠️ 不变量「B 也有基线数据」不能依赖 schema 恰好是本次运行刚建的 ⇒ 显式再灌一次
        #    （照 `test_peer_binding_api.PeerCrossTenantTokenTests` 的同一条纪律）。
        seed_tenant(cls.tenant_b)

    def test_each_schema_is_pruned_by_its_own_rows(self):
        from django.db import connection
        from django_tenants.utils import tenant_context

        _age(_peer(kind="order.assign", idempotency_key="k-a-old"), OLD)
        with tenant_context(self.tenant_b):
            _age(_peer(kind="order.assign", idempotency_key="k-b-old"), OLD)
            _age(_peer(kind="order.assign", idempotency_key="k-b-fresh"), NEW)
        try:
            call_command("prune_sync_events", "--commit", stdout=StringIO())
            self.assertFalse(
                PeerSyncEvent.objects.filter(idempotency_key="k-a-old").exists()
            )
            with tenant_context(self.tenant_b):
                self.assertFalse(
                    PeerSyncEvent.objects.filter(idempotency_key="k-b-old").exists()
                )
                self.assertTrue(
                    PeerSyncEvent.objects.filter(idempotency_key="k-b-fresh").exists()
                )
        finally:
            connection.set_tenant(self.tenant)   # ⚠️ 把连接钉回本租户（跨 schema 纪律）


class PruneSyncEventsBatchingTests(TenantTestCase):
    """分批删除（D-0349）：把 `BATCH_SIZE` 调小到**小于命中数**，命中的行仍必须**全部**删掉。

    ## 这条抓的是什么

    「分批」承重的是拍板判据「⛔ 不锁全表（长事务 / 长锁）」——而它的**行为**判据是：
    循环真的在循环（每批删完接着删下一批，直到 0）。把 `BATCH_SIZE` patch 成 2、
    造 5 条候选：只删第一批的实现会留下 3 条 ⇒ 本用例红；一把删（不分批）的实现
    这里看不出来（都删干净）——那条由**变异**证明（把循环摘掉 ⇒ 本用例红），
    与 `AGENTS.md` §G「每条钉子都要有一次弄红的证明」同口径。
    """

    def test_batched_delete_drains_all_candidates_even_when_batch_is_smaller(self):
        from apps.business.management.commands import prune_sync_events as cmd

        for index in range(5):
            _age(
                _jidanpu(target_ref=f"JD-BATCH-{index}", status=JidanpuSyncEvent.Status.DONE),
                OLD,
            )
        out = StringIO()
        with mock.patch.object(cmd, "BATCH_SIZE", 2):
            call_command("prune_sync_events", "--commit", stdout=out)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref__startswith="JD-BATCH-").count(),
            0,
            "批大小(2) < 命中数(5) 时也必须循环删到 0（只删第一批 = 漏删 3 条）",
        )
        self.assertIn("jidanpu 5", out.getvalue())
        # 幂等：同一参数立刻重跑 ⇒ 0 行（timer 补跑的承重判据，批路径上同样成立）。
        out_rerun = StringIO()
        with mock.patch.object(cmd, "BATCH_SIZE", 2):
            call_command("prune_sync_events", "--commit", stdout=out_rerun)
        self.assertIn("jidanpu 0 条", out_rerun.getvalue())
