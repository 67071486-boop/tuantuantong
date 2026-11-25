"""下游鸡蛋铺线的**调度接线**（Task 10；spec §10 + `docs/RUNBOOK.md` 的本线运行口径节）。

`jidanpu_sync` 从「每租户一轮（租户级那条线）」扩成「每租户：租户级那条线**照旧** + **逐个代练员绑定**
各跑一轮」。这个文件只守**接线契约**本身（闸门 / 顺序 / 异常边界 / 时钟 / 退出码），
⛔ 不重复测三条路各自的业务行为（那些在 `tests/test_jidanpu_member_fastpath.py` /
`test_jidanpu_member_status.py` / `test_jidanpu_member_consume.py` 里）。

被钉住的契约：

1. **顺序固定**：每个绑定的**一轮**是 `run_outbox` → `poll_log` → `pull_statuses` →
   `reconcile_and_queue` → `claim_net`（⑤接单兜底网排最后：先收敛对方事实再兜底）。
   ⭐ **2026-10-26 拆节奏**：`pull_statuses` 与 `reconcile_and_queue` 不再共用一道闸门
   （真机实测：共用 ⇒ 回拉被对账的间隔绑死，改成 10 分钟后又变成每 10 分钟排 450+ 条候选事件）。
2. **各条路各自按绑定自己的间隔判到点**：`fast_poll_seconds`（默认 3）管快路、
   `status_pull_seconds`（默认 600）管状态回拉、`reconcile_seconds`（默认 180）管补漏对账、
   `CLAIM_NET_SECONDS`（常量 180）管接单兜底网；
   未到点 ⇒ **那次调用一次都不发**，各条慢路有自己的锚、互不阻塞。
   ⚠️ 锚是**进程内**的（`last_run_at` 一列被各条路共用 ⇒ 库里的列分不开各自的节奏）——
   多进程部署会退化成「每进程一窗」，这条代价写进 RUNBOOK 的已知边界。
3. **逐绑定独立异常边界**：一个代练员的绑定抛未预期异常 ⇒ 同租户**其他绑定**照跑；
   该绑定内部一条路炸了 ⇒ 它自己另外两条路照跑。
   租户级鸡蛋铺线已拆除。判据是其他绑定照跑，恰好各被调 2 次。
4. **停用（`enabled=False`）与凭据不全 ⇒ 整行跳过**（spec §8.4：停用停止快路与对账 ⇒
   ⛔ 连 `reconcile_and_queue` 都不许调，它会给停用的行排事件）。
5. 走真命令跑一趟，各条路恰好各被调一次。租户级鸡蛋铺线已不存在。
   （`test_the_member_line_runs_via_the_real_command`，它是把 `_run_tenant` 的早退
   拆开之后必须钉住的一条）。
6. **退出码契约照旧**（spec §10）：代练员绑定出现未预期失败 ⇒ `--once` 非零退出；
   预期内、已记录的失败（三条路自己返回的失败计数）⇒ 退出 0。
7. **单实例口径**：一轮里的多个绑定由**同一个进程串行**跑（`threading.get_ident()` 全是调用方那个），
   ⛔ 没有线程池 / 无 in-process 扇出（生产是「一租户一套部署 + 这条常驻只在一台」）。
8. **失败也要打锚**（承重，保护下游容忍度）：一条路抛未预期异常 ⇒ 它那道锚**仍然推进**
   ⇒ 在它自己的间隔内**不会再打一次上游**（判据是**桩上的调用次数**，⛔ 不是「没报错」）。

⚠️ 夹具一律**合成值**（E-0077）；零真实网络（各条路全被桩掉）。
⚠️ 桩打在**被查找的那个模块**上（E-0029）：各条路都在 `apps.business.jidanpu_member` 里查，
`tenant_context` / `Tenant.objects` 在命令模块里查。
"""
from __future__ import annotations

import io
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_member, jidanpu_member_openapi_outbox
from apps.business.models import (
    JidanpuMemberBinding,
)

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

TENANT_SCHEMA = "tenant-a"
MEMBER_BASE_URL = "https://jidanpu-member.test.invalid"
MEMBER_ACCOUNT = "member-bot-account"

#: 固定的「现在」：闸门判据与注入的时钟都用它（⛔ 不用 `timezone.now()` 让断言随运行时刻漂）。
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt_timezone.utc)


@contextmanager
def _noop_context(tenant):
    """把 `tenant_context` 换成「什么都不做」（只测接线逻辑，不切库）。"""
    yield


def _patch_tenants(module, tenants):
    """把 `Tenant.objects` 换成回给定租户的替身（照 `test_jidanpu_sync_command` 的形状）。"""
    patcher = mock.patch.object(module.Tenant, "objects")
    objects = patcher.start()
    objects.exclude.return_value.order_by.return_value = list(tenants)
    return patcher, objects


class _ScheduleTestCase(TenantTestCase):
    """一租户一库的真测试库 + 一个假租户对象（`tenant_context` 被换掉）。"""

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        # ⚠️ 两条路的锚是**进程内**状态 ⇒ 跨用例会串味（上一条用例留下的锚会让这条用例
        #    「还没跑就未到点」）⇒ 每条用例开头清空。
        # ⚠️ `getattr` 的兜底只为让**实现之前**的红是**行为红**（遍历没接上），
        #    ⛔ 不是「锚可以不存在」——下面的用例会把它们当真用。
        getattr(module, "_MEMBER_FAST_AT", {}).clear()
        getattr(module, "_MEMBER_RECONCILE_AT", {}).clear()
        # ⭐ 接单兜底网有自己的锚（与两条慢路各管各的）。
        getattr(module, "_MEMBER_CLAIM_NET_AT", {}).clear()
        # ⭐ 审计批 D2：下家行的**行级退避**与上家线共用那两个字典（键同形）
        #    ⇒ 它们同样是进程内状态，不清会把上一条用例的退避窗口带进这一条。
        getattr(module, "_UPSTREAM_FAILURE_COUNT", {}).clear()
        getattr(module, "_UPSTREAM_PENALTY_UNTIL", {}).clear()
        #: ⭐ 拆节奏：状态回拉有自己的锚（与对账那道**互相独立**）。
        getattr(module, "_MEMBER_STATUS_PULL_AT", {}).clear()
        self.tenant = mock.Mock(schema_name=TENANT_SCHEMA)

    def _user(self, name):
        return User.objects.create_user(username=name, password="pw-canary-x")

    def _member(self, name, *, enabled=True, password="pw-member-canary", **kwargs):
        # ⚠️ 账号按用例内的名字派生：`(jidanpu_base_url, account)` 上有唯一约束
        #    （同一套鸡蛋铺里同一个账号不重复绑），⛔ 不能所有夹具共用一个账号。
        return JidanpuMemberBinding.objects.create(
            user=self._user(name),
            jidanpu_base_url=MEMBER_BASE_URL, account=f"{MEMBER_ACCOUNT}-{name}",
            account_name="传令兵甲", password=password, enabled=enabled,
            **kwargs,
        )

    def _run(self, stdout=None, stderr=None, *, expect_error=False, now=NOW):
        """跑一趟 `--once`（租户遍历被替身接管），回 `(CommandError 或 None, out, err)`。

        ⚠️ `now` 是**注入的墙钟**（默认本文件的 `NOW`）：跑第二趟时把它往后推，
        才验得了「未到点 ⇒ 不再打上游」这类**跨趟**的判据。
        """
        out = stdout if stdout is not None else io.StringIO()
        err = stderr if stderr is not None else io.StringIO()
        caught = None
        with mock.patch.object(self.module, "tenant_context", _noop_context), \
             mock.patch.object(
                 self.module.timezone, "now", return_value=now
             ):
            patcher, _ = _patch_tenants(self.module, [self.tenant])
            try:
                try:
                    call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
                except CommandError as exc:
                    caught = exc
            finally:
                patcher.stop()
        if expect_error:
            self.assertIsNotNone(caught, "本趟应当以非零退出码收尾，却没有抛 CommandError")
        else:
            self.assertIsNone(
                caught, f"本趟不该非零退出：{caught}；stderr={err.getvalue()!r}"
            )
        return caught, out.getvalue(), err.getvalue()


class MemberRoundWiringTests(_ScheduleTestCase):
    """一个绑定的一轮：四条路、固定顺序、同一个时钟。"""

    def test_the_member_round_calls_the_five_paths_in_the_fixed_order(self):
        """顺序：outbox → 快路 → 回拉 → 对账排事件 → 接单兜底网（spec §10 + §9.3 的同一段慢路；
        ⑤ 排最后：先收敛对方事实再兜底）。

        失败方向：把各条路换位（例如先对账再 outbox、或把兜底网插到对账之前）⇒ 本用例红。
        """
        self._member("member-order")
        order: list[str] = []
        seen_now: list[datetime] = []

        def _record(name, result):
            def _inner(*args, **kwargs):
                order.append(name)
                seen_now.append(kwargs["now"])
                return result
            return _inner

        with mock.patch.object(
                 jidanpu_member, "run_outbox", side_effect=_record("outbox", jidanpu_member.OutboxStats()),
             ), mock.patch.object(
                 jidanpu_member, "poll_log", side_effect=_record("fast", 2),
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", side_effect=_record("pull", 1),
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 side_effect=_record("reconcile", jidanpu_member.QueueStats()),
             ), mock.patch.object(
                 jidanpu_member, "claim_net", side_effect=_record("claim_net", 0),
             ):
            self._run()
        self.assertEqual(order, ["outbox", "fast", "pull", "reconcile", "claim_net"])
        # ⭐ 同一个 `now` 进各条路（不是各算一次时刻）
        self.assertEqual(
            seen_now, [NOW, NOW, NOW, NOW, NOW]
        )

    def test_the_round_summary_reports_the_skipped_count_and_the_reason(self):
        """⭐ 整体评审 I-1：调度每轮的汇总必须**同时**有跳过计数与**人话原因**。

        判别力：把 `_member_reconcile_text` 改回只报计数（`（跳过 {len(...)}）`）⇒
        人话那半条断言红——「跳过 1」本身看不出跳过了什么、更看不出为什么。
        """
        stats = jidanpu_member.QueueStats(
            launch=1,
            skipped=("同地址下负责推这张单的绑定（acc-a）占着归属，本次对账跳过这张单",),
        )
        text = self.module.Command._member_reconcile_text(
            stats, due=True, seconds=180
        )
        self.assertIn("排事件 1", text)
        self.assertIn("跳过 1", text)
        self.assertIn("同地址下负责推这张单的绑定", text)

    def test_the_round_summary_gives_each_slow_segment_its_own_interval(self):
        """⭐ 拆节奏之后人话汇总要**报清是哪一段没到点、它的间隔是多少**（⛔ 不许混成一句）。

        失败方向：把两段合并回一句「对账 …（间隔 180 秒）」⇒ 回拉那半条断言红
        （真机上就分不出「回拉没跑」是没到点还是没跑成）。
        """
        pull_text = self.module.Command._member_pull_text(3, due=True, seconds=600)
        self.assertIn("回拉 3 条", pull_text)
        pull_waiting = self.module.Command._member_pull_text(None, due=False, seconds=600)
        self.assertIn("回拉未到点", pull_waiting)
        self.assertIn("600", pull_waiting)
        reconcile_waiting = self.module.Command._member_reconcile_text(
            None, due=False, seconds=180
        )
        self.assertIn("对账未到点", reconcile_waiting)
        self.assertIn("180", reconcile_waiting)

    def test_the_round_summary_reports_the_claim_net_segment(self):
        """⭐ ⑤ 兜底网在逐轮人话汇总里有自己那一段，三档分得开（照 `_member_fast_text` 同一族）：
        跑了 ⇒ 「兜底网 N 行」；没到点 ⇒ 「未到点（间隔 180 秒）」；被跳过 ⇒ 点名开关与去向；
        到点但抛了 ⇒ 「未完成」（⛔ 不是「没跑」）。
        """
        text = self.module.Command._member_claim_net_text(3, due=True, seconds=180)
        self.assertIn("兜底网 3 行", text)
        waiting = self.module.Command._member_claim_net_text(None, due=False, seconds=180)
        self.assertIn("兜底网未到点", waiting)
        self.assertIn("180", waiting)
        skipped = self.module.Command._member_claim_net_text(
            None, due=True, seconds=180, skipped=True
        )
        self.assertIn("兜底网跳过", skipped)
        self.assertIn("--skip-reconcile", skipped)
        self.assertIn("tuantuantong-jidanpu-reconcile", skipped)
        blown = self.module.Command._member_claim_net_text(None, due=True, seconds=180)
        self.assertIn("兜底网未完成", blown)

    def test_the_member_line_runs_via_the_real_command(self):
        """⭐ 拆早退之后的钉子：**没有任何租户级绑定**时，个人绑定照样跑（走真命令）。

        ⛔ **2026-10-08 改准**：原用例名 `…_even_when_the_tenant_line_is_absent`，讲的是
        「租户级那条线没配也不影响本线」。租户级那一段已整条拆除 ⇒ 今天**不可能**配出
        租户级绑定，「有没有租户级那一段」不再是变量 ⇒ 名字与说法改准。
        ⚠️ **判据强度没降**：四条路仍然各自**恰好各被调一次**（下面四个 `assert_called_once`
        一个字没改），⛔ 没有放宽成「至少调过一次」。
        """
        self._member("member-without-any-tenant-binding")
        with mock.patch.object(jidanpu_member, "run_outbox") as outbox, \
             mock.patch.object(jidanpu_member, "poll_log") as fast, \
             mock.patch.object(jidanpu_member, "pull_statuses") as pull, \
             mock.patch.object(jidanpu_member, "reconcile_and_queue") as reconcile, \
             mock.patch.object(jidanpu_member, "claim_net") as claim_net:
            outbox.return_value = jidanpu_member.OutboxStats()
            fast.return_value = 0
            pull.return_value = 0
            reconcile.return_value = jidanpu_member.QueueStats()
            claim_net.return_value = 0
            self._run()
        outbox.assert_called_once()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()
        claim_net.assert_called_once()

class MemberIntervalGateTests(_ScheduleTestCase):
    """三条路**各自**按绑定自己的间隔判到点（锚在进程内；两条慢路互不阻塞）。"""

    def _key(self, binding):
        return (TENANT_SCHEMA, binding.pk)

    def _patch_paths(self):
        return (
            mock.patch.object(jidanpu_member, "run_outbox"),
            mock.patch.object(jidanpu_member, "poll_log"),
            mock.patch.object(jidanpu_member, "pull_statuses"),
            mock.patch.object(jidanpu_member, "reconcile_and_queue"),
            mock.patch.object(jidanpu_member, "claim_net"),
        )

    def test_the_fast_path_waits_for_its_own_interval(self):
        """`fast_poll_seconds=3`：锚只过去 1 秒 ⇒ 快路**一次都不发**；对账锚已过期 ⇒ 对账照跑。

        失败方向：把闸门删掉（或拿 `reconcile_seconds` 判快路）⇒ `poll_log` 被调 ⇒ 本用例红。
        """
        binding = self._member("member-fast-gate", fast_poll_seconds=3, reconcile_seconds=180)
        self.module._MEMBER_FAST_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._MEMBER_RECONCILE_AT[self._key(binding)] = NOW - timedelta(seconds=181)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            _pull.return_value = 0
            _rec.return_value = jidanpu_member.QueueStats()
            self._run()
        quick.assert_not_called()
        _pull.assert_called_once()
        _rec.assert_called_once()

    def test_the_status_pull_waits_for_its_own_interval(self):
        """⭐ 回拉到点、补漏未到点 ⇒ **只跑回拉**（2026-10-26 拆节奏，真机那条缺陷的正面判据）。

        失败方向：回拉仍拿 `reconcile_seconds` 判到点（拆之前的旧口径）⇒ 它会被对账的间隔
        绑死（真机把 `reconcile_seconds` 调到 3600 之后回拉也变成一小时一次）⇒ 本用例红。
        """
        binding = self._member(
            "member-pull-gate", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        self.module._MEMBER_STATUS_PULL_AT[self._key(binding)] = NOW - timedelta(seconds=601)
        self.module._MEMBER_RECONCILE_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as _quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            _pull.return_value = 0
            self._run()
        _pull.assert_called_once()
        _rec.assert_not_called()

    def test_the_reconcile_path_waits_for_its_own_interval(self):
        """⭐ 补漏到点、回拉未到点 ⇒ **只跑补漏**（互不阻塞的另一半）。

        `reconcile_seconds=180` 的锚已过去 181 秒 ⇒ 对账到点；`status_pull_seconds=600` 的锚
        只过去 1 秒 ⇒ 回拉**不发**。
        失败方向：两段仍共用一个闸门（拿对账的锚判回拉 / 共用一个锚）⇒ 两条断言里必有一条红。
        """
        binding = self._member(
            "member-reconcile-gate", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        self.module._MEMBER_STATUS_PULL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._MEMBER_RECONCILE_AT[self._key(binding)] = NOW - timedelta(seconds=181)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as _quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            _rec.return_value = jidanpu_member.QueueStats()
            self._run()
        _pull.assert_not_called()
        _rec.assert_called_once()

    def test_the_claim_net_waits_for_its_own_anchor(self):
        """⭐ ⑤ 兜底网有自己的锚：未到点 ⇒ 它一次都不调，同轮到点的对账照跑（互不阻塞）。

        失败方向：拿对账的锚判兜底网（共用一根锚）⇒ 兜底网被调 ⇒ 本用例红。
        """
        binding = self._member(
            "member-net-gate", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        self.module._MEMBER_RECONCILE_AT[self._key(binding)] = NOW - timedelta(seconds=181)
        self.module._MEMBER_CLAIM_NET_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as _quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            _rec.return_value = jidanpu_member.QueueStats()
            self._run()
        _rec.assert_called_once()
        _net.assert_not_called()

    def test_the_two_slow_segments_run_once_each_in_order_when_both_are_due(self):
        """⭐ 两者都到点 ⇒ 回拉一次、对账一次，**顺序仍是回拉 → 对账**（⛔ 只看次数会漏掉换位）。"""
        self._member(
            "member-both-due", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        seen: list[str] = []

        def _record(name, result):
            def _inner(*args, **kwargs):
                seen.append(name)
                return result
            return _inner

        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "pull_statuses", side_effect=_record("pull", 0),
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 side_effect=_record("reconcile", jidanpu_member.QueueStats()),
             ):
            self._run()
        self.assertEqual(seen, ["pull", "reconcile"])

    def test_the_default_intervals_are_600_and_180(self):
        """⭐ 默认配置：新列 `status_pull_seconds` = **600**、旧列 `reconcile_seconds` 仍是 **180**。

        ⚠️ 期望值写**字面量**（⛔ 不从 `MEMBER_STATUS_PULL_SECONDS` / `MEMBER_RECONCILE_SECONDS`
        派生）：派生出来的断言在常量被改坏时不会红（E-0024）。
        ⚠️ 两份读数：模型侧（`_meta`）+ 库侧（introspection）—— 光看模型挡不住「迁移没跑」。
        """
        self.assertEqual(
            JidanpuMemberBinding._meta.get_field("status_pull_seconds").default, 600
        )
        self.assertEqual(
            JidanpuMemberBinding._meta.get_field("reconcile_seconds").default, 180
        )
        binding = self._member("member-default-intervals")
        self.assertEqual(binding.status_pull_seconds, 600)
        #: 旧列**一个字都没动**（它已经上线、是既有旋钮）
        self.assertEqual(binding.reconcile_seconds, 180)
        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, JidanpuMemberBinding._meta.db_table
                )
            }
        self.assertIn("status_pull_seconds", columns, "迁移没跑：库里没有这一列")
        #: 哨兵：这条读的确实是那张表（照本文件既有列的读法）。
        self.assertIn("reconcile_seconds", columns)

        # 默认节奏下的行为：599 秒前的锚**还没**到点（600 的间隔），601 秒前才到点。
        key = self._key(binding)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as _quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            self.module._MEMBER_STATUS_PULL_AT[key] = NOW - timedelta(seconds=599)
            self.module._MEMBER_RECONCILE_AT[key] = NOW - timedelta(seconds=179)
            self._run()
            _pull.assert_not_called()
            _rec.assert_not_called()

            self.module._MEMBER_STATUS_PULL_AT[key] = NOW - timedelta(seconds=601)
            self.module._MEMBER_RECONCILE_AT[key] = NOW - timedelta(seconds=181)
            _pull.return_value = 0
            _rec.return_value = jidanpu_member.QueueStats()
            self._run(now=NOW + timedelta(seconds=30))
            _pull.assert_called_once()
            _rec.assert_called_once()

    def test_a_binding_that_never_ran_is_due_on_both_paths(self):
        """锚不存在（刚启用 / 进程刚起来）⇒ 两条路都到点（照 `interval_elapsed` 的形状）。"""
        self._member("member-never-ran", fast_poll_seconds=3, reconcile_seconds=180)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            quick.return_value = 0
            _pull.return_value = 0
            _rec.return_value = jidanpu_member.QueueStats()
            self._run()
        quick.assert_called_once()
        _pull.assert_called_once()
        _rec.assert_called_once()

    def test_a_stored_zero_falls_back_to_the_model_default(self):
        """间隔列存 `0`（配置写坏）⇒ 按**模型列自己的默认值**算（照 `interval_elapsed` 的 `or 60`）。

        ⚠️ 与租户级那条线同一条理由：0 是有意义的边界（E-0021 同族），它的意义是
        「配置写坏了、按默认走」，⛔ 不是「每轮都跑」。
        失败方向：`or` 兜底被删 ⇒ 0 秒间隔 ⇒ 锚只过去 1 秒也算到点 ⇒ 本用例红。
        """
        binding = self._member(
            "member-zero-interval", fast_poll_seconds=0,
            status_pull_seconds=0, reconcile_seconds=0,
        )
        self.module._MEMBER_FAST_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._MEMBER_STATUS_PULL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._MEMBER_RECONCILE_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        outbox, fast, pull, reconcile, claim_net = self._patch_paths()
        with outbox as box, fast as quick, pull as _pull, reconcile as _rec, claim_net as _net:
            box.return_value = jidanpu_member.OutboxStats()
            self._run()
        # 兜底值 = 3 / 600 / 180 ⇒ 都还没到点
        quick.assert_not_called()
        _pull.assert_not_called()
        _rec.assert_not_called()


class MemberSkipTests(_ScheduleTestCase):
    """整行跳过的两种形态：停用、凭据不全（spec §8.4 + 照租户级那一段的凭据闸门）。"""

    def test_a_disabled_member_binding_is_never_touched(self):
        """`enabled=False` ⇒ 四条路一条都不调（⚠️ 尤其 `reconcile_and_queue`：它会排事件）。

        失败方向：只按「绑定存在」遍历（不筛 `enabled`）⇒ 对账会给停用的行排事件 ⇒ 本用例红。
        """
        self._member("member-disabled", enabled=False)
        with mock.patch.object(jidanpu_member, "run_outbox") as outbox, \
             mock.patch.object(jidanpu_member, "poll_log") as fast, \
             mock.patch.object(jidanpu_member, "pull_statuses") as pull, \
             mock.patch.object(jidanpu_member, "reconcile_and_queue") as reconcile:
            self._run()
        outbox.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()

    def test_a_member_binding_with_incomplete_credentials_is_skipped(self):
        """凭据不全（密码为空）⇒ 整行跳过，⛔ 不发起任何一轮（否则每轮都在上游认证处失败）。

        失败方向：删掉 `credentials_missing` 那道闸 ⇒ `run_outbox` 被调 ⇒ 本用例红。
        """
        self._member("member-no-password", password="")
        with mock.patch.object(jidanpu_member, "run_outbox") as outbox, \
             mock.patch.object(jidanpu_member, "poll_log") as fast, \
             mock.patch.object(jidanpu_member, "pull_statuses") as pull, \
             mock.patch.object(jidanpu_member, "reconcile_and_queue") as reconcile:
            self._run()
        outbox.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()


class MemberExceptionBoundaryTests(_ScheduleTestCase):
    """逐绑定（且逐路）的异常边界。"""

    def test_one_member_binding_blowing_up_does_not_stop_the_others(self):
        """⭐ 一个代练员的绑定炸了 ⇒ 同租户另一个绑定照跑，退出码非零。

        失败方向：把个人绑定那一层包成一个大 `try` ⇒
        第二个绑定的三条路都不会被调到 ⇒ 本用例红。

        ⛔ **2026-10-08 改准（原名 …_or_the_tenant_line）**：原判据是「另一个绑定**与租户级
        那条线**照跑」，租户级那一段（`jidanpu_hall.run_round`）随租户级鸡蛋铺线整条拆除
        ⇒ 租户那一半退场。⚠️ **留下的判据强度没降**：仍然是「**另一个绑定的三条路各自
        恰好各被调 2 次**」（下面那三条 `assertEqual(..., 2)` 一个字没改），⛔ 没有放宽成
        「至少调过一次」。
        """
        broken = self._member("member-broken")
        healthy = self._member("member-healthy")

        def _boom(binding, **kwargs):
            if binding.pk == broken.pk:
                raise RuntimeError("boom")
            return jidanpu_member.OutboxStats()

        with mock.patch.object(jidanpu_member, "run_outbox", side_effect=_boom), \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0) as fast, \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0) as pull, \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ) as reconcile:
            caught, out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        # 坏的那个绑定：它自己的另外三条路照跑（逐路边界）
        self.assertEqual(fast.call_count, 2)
        self.assertEqual(pull.call_count, 2)
        self.assertEqual(reconcile.call_count, 2)
        # ⛔ 异常正文（可能带上游回包 / 凭据片段）一个字都不上行，只写异常类型名
        self.assertIn("RuntimeError", err)
        self.assertNotIn("boom", err)
        self.assertIn(f"#{broken.pk}", err)
        # 好的那个绑定仍然留下了它的人话汇总
        self.assertIn(f"代练员绑定 #{healthy.pk}", out)

    def test_a_failing_fast_path_does_not_skip_the_reconcile_path(self):
        """⭐ 逐**路**边界：快路抛未预期异常 ⇒ 同一绑定的回拉与对账照跑。

        失败方向：把四条路并成一个 `try`（或让快路的异常跳过后面两路）⇒
        `pull_statuses` / `reconcile_and_queue` 不被调 ⇒ 本用例红。
        """
        self._member("member-fast-boom")
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(
                 jidanpu_member, "poll_log", side_effect=RuntimeError("boom")
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ) as pull, mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ) as reconcile:
            caught, _out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        pull.assert_called_once()
        reconcile.assert_called_once()
        self.assertIn("RuntimeError", err)

    def test_a_member_failure_makes_once_exit_non_zero(self):
        """退出码契约：代练员绑定的**未预期**失败 ⇒ 非零（cron 才会发信）。"""
        self._member("member-exit-code")
        with mock.patch.object(
            jidanpu_member, "run_outbox", side_effect=RuntimeError("boom")
        ), mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            caught, _out, _err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)

    def test_expected_member_failures_keep_the_exit_code_zero(self):
        """退出码契约的另一半：三条路各自记账的**预期**失败 ⇒ 退出 0（那些是每分钟都有的噪音）。

        ⚠️ 判据是「命令这一层有没有抛 `CommandError`」，⛔ 不是「计数非零」。
        失败方向：把「有 `failed` / `abandoned` 计数」当未预期失败 ⇒ 本用例红。
        """
        self._member("member-expected-failure")
        with mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(failed=2, deferred=1),
             ), mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            _caught, out, _err = self._run()
        self.assertIn("代练员绑定", out)


class MemberSingleInstanceTests(_ScheduleTestCase):
    """单实例口径：一轮里的多个绑定由**同一个进程串行**跑（spec §10）。"""

    def test_the_member_rounds_run_serially_on_the_calling_thread(self):
        """⭐ 两个绑定：调用线程 id 全等于调用方的，且**先跑完 A 的四条路、再跑 B 的四条路**。

        ⛔ 不许改成线程池 / 并发扇出：生产形态是「一租户一套部署 + 这条常驻只在一台」
        （RUNBOOK 的本线运行口径节）——同一个下家账号被两个线程同时打就会真的并发。
        失败方向：把遍历换成线程池（或先跑所有人的 outbox 再跑所有人的快路）⇒ 本用例红。
        """
        first = self._member("member-serial-a")
        second = self._member("member-serial-b")
        caller = threading.get_ident()
        trace: list[tuple[str, int]] = []

        def _outbox(binding, **kwargs):
            trace.append((f"outbox:{binding.pk}", threading.get_ident()))
            return jidanpu_member.OutboxStats()

        def _fast(binding, **kwargs):
            trace.append((f"fast:{binding.pk}", threading.get_ident()))
            return 0

        def _pull(binding, **kwargs):
            trace.append((f"pull:{binding.pk}", threading.get_ident()))
            return 0

        def _reconcile(binding, **kwargs):
            trace.append((f"reconcile:{binding.pk}", threading.get_ident()))
            return jidanpu_member.QueueStats()

        with mock.patch.object(jidanpu_member, "run_outbox", side_effect=_outbox), \
             mock.patch.object(jidanpu_member, "poll_log", side_effect=_fast), \
             mock.patch.object(jidanpu_member, "pull_statuses", side_effect=_pull), \
             mock.patch.object(jidanpu_member, "reconcile_and_queue", side_effect=_reconcile):
            self._run()
        self.assertEqual(
            [tag for tag, _ident in trace],
            [
                f"outbox:{first.pk}", f"fast:{first.pk}", f"pull:{first.pk}", f"reconcile:{first.pk}",
                f"outbox:{second.pk}", f"fast:{second.pk}", f"pull:{second.pk}", f"reconcile:{second.pk}",
            ],
        )
        self.assertEqual({ident for _tag, ident in trace}, {caller})

    def test_the_bindings_are_visited_in_primary_key_order(self):
        """遍历顺序 = 主键升序（可复现：排障时人话汇总的顺序不会每次不一样）。"""
        first = self._member("member-order-a")
        second = self._member("member-order-b")
        self.assertLess(first.pk, second.pk)   # 自证前提：建行顺序与主键顺序一致
        visited: list[int] = []

        def _outbox(binding, **kwargs):
            visited.append(binding.pk)
            return jidanpu_member.OutboxStats()

        with mock.patch.object(jidanpu_member, "run_outbox", side_effect=_outbox), \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            self._run()
        self.assertEqual(visited, [first.pk, second.pk])


class MemberAnchorAfterFailureTests(_ScheduleTestCase):
    """⭐ 「**失败也要打锚**」的钉子（承重：持续失败的那条路**不许每拍重打上游**）。

    `jidanpu_sync._run_member_binding` 把 `_MEMBER_FAST_AT[key]` / `_MEMBER_STATUS_PULL_AT[key]` /
    `_MEMBER_RECONCILE_AT[key]` / `_MEMBER_CLAIM_NET_AT[key]` 写在 `finally` 里
    （照租户级那条线 `last_run_at` = 「上次**跑**」的口径；
    ⭐回拉 / 对账 / 兜底网**各一道**锚，失败互不牵连）。这条自述的含义是：
    **一条路失败之后，它在自己的间隔内不会再被打一次**——保护的是下家鸡蛋铺的容忍度
    （它到底能承受多密的轮询**我方未实测**，spec §13-R8）。
    ⛔ 判据必须打**桩上的调用次数**，⛔ 不是「没报错」：把锚从 `finally` 挪到成功路径，
    第二趟会因为锚为空而**再打一次** ⇒ 本类用例红（评审实测：原来的 15 条全是单趟调用，抓不到它）。
    """

    def _key(self, binding):
        return (TENANT_SCHEMA, binding.pk)

    def test_a_failed_fast_path_is_not_retried_within_its_interval(self):
        """快路抛未预期异常 ⇒ **锚仍然打上**：同一进程里隔 1 秒再跑一趟（间隔 180 秒）一次都不再打。

        失败方向：把 `_MEMBER_FAST_AT[key] = now` 从 `finally` 挪进 `try` 的成功路径 ⇒
        第二趟锚为空 ⇒ `poll_log` 又被调一次 ⇒ `call_count` 变 2 ⇒ 本用例红。
        """
        binding = self._member(
            "member-fast-fail-anchor", fast_poll_seconds=180, reconcile_seconds=180
        )
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(
                 jidanpu_member, "poll_log", side_effect=RuntimeError("boom")
             ) as fast, mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            # 第一趟：快路失败 ⇒ 响亮（非零退出 + stderr 只写异常类型名），但锚要留下
            _caught, _out, err = self._run(expect_error=True)
            self.assertIn("RuntimeError", err)
            self.assertEqual(fast.call_count, 1)
            self.assertEqual(self.module._MEMBER_FAST_AT.get(self._key(binding)), NOW)
            # 第二趟：钟只往后走 1 秒（远不到 180 秒的间隔）⇒ 一次都不许再打
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(fast.call_count, 1, "失败的那条快路在间隔内又被打了上游")

    def test_a_failed_reconcile_path_is_not_retried_within_its_interval(self):
        """对账（排事件）抛未预期异常 ⇒ **对账那道锚也仍然打上**（回拉有自己那道，互不影响）。

        ⭐ 2026-10-26 拆节奏之后：回拉与对账**各有各的锚**（`_MEMBER_STATUS_PULL_AT` /
        `_MEMBER_RECONCILE_AT`）——本用例同时钉住「对账失败照样打锚」与「回拉的锚照常推进」。

        失败方向：把 `_MEMBER_RECONCILE_AT[key] = now` 从 `finally` 挪进 `try`（跟在
        `reconcile_and_queue(...)` 之后）⇒ 抛异常那一趟不会打锚 ⇒ 第二趟 `reconcile_due` 又为真
        ⇒ `reconcile_and_queue` 被调第二次 ⇒ 本用例红。
        """
        binding = self._member(
            "member-reconcile-fail-anchor", fast_poll_seconds=180, reconcile_seconds=180
        )
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(
                 jidanpu_member, "poll_log", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ) as pull, mock.patch.object(
                 jidanpu_member, "reconcile_and_queue", side_effect=RuntimeError("boom")
             ) as reconcile:
            _caught, _out, err = self._run(expect_error=True)
            self.assertIn("RuntimeError", err)
            self.assertEqual(pull.call_count, 1)
            self.assertEqual(reconcile.call_count, 1)
            self.assertEqual(self.module._MEMBER_RECONCILE_AT.get(self._key(binding)), NOW)
            self.assertEqual(self.module._MEMBER_STATUS_PULL_AT.get(self._key(binding)), NOW)
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(pull.call_count, 1, "失败的那组慢路在间隔内又被打了上游（回拉）")
        self.assertEqual(reconcile.call_count, 1, "失败的那组慢路在间隔内又被打了上游（对账）")

    def test_a_failed_status_pull_is_not_retried_within_its_interval(self):
        """⭐ 回拉自己抛未预期异常 ⇒ **回拉那道锚也打上**（否则它会被每一拍重打上游）。

        判据是**桩上的调用次数**（⛔ 不是「没报错」）：第二趟只把钟拨 1 秒（间隔 600），
        回拉与对账都**不该**再被调。

        失败方向：把 `_MEMBER_STATUS_PULL_AT[key] = now` 从 `finally` 挪进 `try` 的成功路径
        ⇒ 第二趟回拉锚为空 ⇒ `pull_statuses` 又被调一次 ⇒ 本用例红。
        """
        binding = self._member(
            "member-pull-fail-anchor", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(
                 jidanpu_member, "poll_log", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", side_effect=RuntimeError("boom")
             ) as pull, mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ) as reconcile:
            _caught, _out, err = self._run(expect_error=True)
            self.assertIn("RuntimeError", err)
            self.assertEqual(pull.call_count, 1)
            self.assertEqual(
                self.module._MEMBER_STATUS_PULL_AT.get(self._key(binding)), NOW,
                "回拉失败也要打锚（否则下一拍又打一次上游）",
            )
            #: 回拉失败**不挡**对账（两条慢路各有异常边界，既有口径）
            self.assertEqual(reconcile.call_count, 1)
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(pull.call_count, 1, "失败的回拉在自己的间隔内又被打了上游")
        self.assertEqual(reconcile.call_count, 1, "对账在自己的间隔内又被打了上游")

    def test_a_failed_claim_net_is_not_retried_within_its_interval(self):
        """⭐ ⑤ 兜底网自己抛未预期异常 ⇒ **兜底网那道锚也打上**（否则它会被每一拍重打上游）。

        判据是**桩上的调用次数**（⛔ 不是「没报错」）：第二趟只把钟拨 1 秒（间隔 180），
        兜底网**不该**再被调。

        失败方向：把 `_MEMBER_CLAIM_NET_AT[key] = now` 从 `finally` 挪进 `try` 的成功路径
        ⇒ 第二趟兜底网锚为空 ⇒ `claim_net` 又被调一次 ⇒ 本用例红。
        """
        binding = self._member(
            "member-net-fail-anchor", fast_poll_seconds=180,
            status_pull_seconds=600, reconcile_seconds=180,
        )
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats()
             ), mock.patch.object(
                 jidanpu_member, "poll_log", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ) as reconcile, mock.patch.object(
                 jidanpu_member, "claim_net", side_effect=RuntimeError("boom")
             ) as net:
            _caught, _out, err = self._run(expect_error=True)
            self.assertIn("RuntimeError", err)
            self.assertEqual(net.call_count, 1)
            self.assertEqual(
                self.module._MEMBER_CLAIM_NET_AT.get(self._key(binding)), NOW,
                "兜底网失败也要打锚（否则下一拍又打一次上游）",
            )
            #: 兜底网失败**不挡**对账（各条慢路各有异常边界，既有口径）
            self.assertEqual(reconcile.call_count, 1)
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(net.call_count, 1, "失败的兜底网在自己的间隔内又被打了上游")
        self.assertEqual(reconcile.call_count, 1, "对账在自己的间隔内又被打了上游")


class MemberRowPenaltyTests(_ScheduleTestCase):
    """⭐ 2026-10-25 审计批 D2（M-10）：下家①式读路的**行级退避**。

    修的现场：下家快路此前读路无退避——被对方 WAF 拉黑时仍每 3 秒一拍白撞（同机单出口 IP，
    还会连累该机全部上家绑定；2026-10-20 上家线 9 小时 1 万次白撞同款）。
    判据（照上家线 `UpstreamPenaltyTests` 的同一条纪律，打**进程内那两个字典**与桩的调用集合）：

    1. 快路抛 ⇒ 记一次连续失败 + 写指数窗口（首窗 30 秒；⭐ 与上家线**同一张表、同一套阶梯**）；
    2. 窗口内那一行**整行跳过**（四条路一次都不调、连 outbox 一起），⛔ 不静默；
    3. 窗口过了再失败 ⇒ 阶梯上抬（30 → 60）；干净 ⇒ 清零；
    4. 独立快路单元（`--fastpath-only`）同样守这道闸。
    """

    def _key(self, binding):
        return (TENANT_SCHEMA, binding.pk)

    def _patch_paths(self):
        return (
            mock.patch.object(jidanpu_member, "run_outbox"),
            mock.patch.object(jidanpu_member, "poll_log"),
            mock.patch.object(jidanpu_member, "pull_statuses"),
            mock.patch.object(jidanpu_member, "reconcile_and_queue"),
            mock.patch.object(jidanpu_member, "claim_net"),
        )

    def test_a_fastpath_failure_starts_the_row_penalty_window(self):
        """快路抛 ⇒ 计数 1、窗口 = `now + 30` 秒（阶梯基数与上家线逐字同一档），且**不静默**。

        失败方向：下家失败不记窗口（M-10 的现状）⇒ 两个字典为空 ⇒ 本用例红。
        """
        binding = self._member("member-penalty")
        outbox, quick_p, pull_p, rec_p, net_p = self._patch_paths()
        with outbox as _box, quick_p as quick, pull_p as _pull, rec_p as _rec, net_p:
            quick.side_effect = RuntimeError("合成炸了-快路")
            _caught, _out, err = self._run(expect_error=True)
        self.assertEqual(self.module._UPSTREAM_FAILURE_COUNT.get(self._key(binding)), 1)
        self.assertEqual(
            self.module._UPSTREAM_PENALTY_UNTIL.get(self._key(binding)),
            NOW + timedelta(seconds=30),
        )
        self.assertIn("整行退避", err)
        self.assertIn("30 秒", err)

    def test_the_whole_row_is_skipped_inside_the_window(self):
        """窗口内那一行**整行跳过**：四条路一次都不调、`last_run_at` 照样前进、健康四列不动。

        失败方向：只挡快路不挡 outbox（同一 IP 还在打写接口）⇒ `run_outbox` 被调 ⇒ 本用例红；
        或跳过时不打 `last_run_at`（页面像「同步死了」）⇒ 刷新断言红。
        ⚠️ 夹具自证：先把 `last_run_at` 落在一个**更早**的合成时刻，才能断言「它前进了」。
        """
        binding = self._member("member-skip-row")
        key = self._key(binding)
        self.module._UPSTREAM_FAILURE_COUNT[key] = 1
        self.module._UPSTREAM_PENALTY_UNTIL[key] = NOW + timedelta(seconds=30)
        earlier = NOW - timedelta(minutes=10)
        JidanpuMemberBinding.objects.filter(pk=binding.pk).update(last_run_at=earlier)
        outbox, quick_p, pull_p, rec_p, net_p = self._patch_paths()
        with outbox as box, quick_p as quick, pull_p as pull, rec_p as rec, net_p:
            box.return_value = jidanpu_member.OutboxStats()
            quick.return_value = 0
            pull.return_value = 0
            rec.return_value = jidanpu_member.QueueStats()
            _caught, out, _err = self._run()
        box.assert_not_called()
        quick.assert_not_called()
        pull.assert_not_called()
        rec.assert_not_called()
        self.assertIn(f"代练员绑定 #{binding.pk}", out)
        self.assertIn("整行跳过", out)
        refreshed = JidanpuMemberBinding.objects.get(pk=binding.pk)
        self.assertEqual(refreshed.last_run_at, NOW, "退避的那一轮也要打「上次运行」")
        # ⛔ 四档语义不动：退避既不是成功、也不写新的失败原因。
        self.assertIsNone(refreshed.last_ok_at)
        self.assertEqual(refreshed.last_error, "")
        self.assertIsNone(refreshed.last_error_at)

    def test_the_window_escalates_then_resets_on_a_clean_round(self):
        """阶梯：窗口过了再失败 ⇒ 60 秒（30 → 60，指数）；干净的一拍 ⇒ 计数与窗口都清掉。

        失败方向：阶梯写死 30 秒（不 ×2）⇒ 第二个窗口断言红；跑完不清 ⇒ 两个字典仍留着旧值。
        """
        binding = self._member("member-escalate")
        key = self._key(binding)
        later = NOW + timedelta(seconds=31)   # 首窗（NOW+30）已过
        outbox, quick_p, pull_p, rec_p, net_p = self._patch_paths()
        with outbox as _box, quick_p as quick, pull_p as _pull, rec_p as _rec, net_p:
            quick.side_effect = RuntimeError("合成炸了-第一次")
            self._run(expect_error=True)
            quick.side_effect = RuntimeError("合成炸了-第二次")
            self._run(expect_error=True, now=later)
        self.assertEqual(self.module._UPSTREAM_FAILURE_COUNT.get(key), 2)
        self.assertEqual(
            self.module._UPSTREAM_PENALTY_UNTIL.get(key),
            later + timedelta(seconds=60),
        )
        # ⭐ 干净的一拍（窗口过了、快路成功）⇒ 清零，下次失败从 30 秒重新起算。
        final = later + timedelta(seconds=61)
        with outbox, quick_p as quick, pull_p, rec_p, net_p:
            quick.side_effect = None
            quick.return_value = 0
            self._run(now=final)
        self.assertNotIn(key, self.module._UPSTREAM_FAILURE_COUNT)
        self.assertNotIn(key, self.module._UPSTREAM_PENALTY_UNTIL)

    def test_the_independent_fastpath_unit_honors_the_same_row_gate(self):
        """独立快路单元（`--fastpath-only`）同样守这道闸：窗口内那行**连快路都不打**。

        失败方向：只在主一轮设闸、快路单元裸奔（M-10 现状的另一半）⇒ `poll_log` 被调
        ⇒ 本用例红（那正是「每 3 秒自证机器人」的那台）。
        ⚠️ 身份自愈**也**不跑（它与快路同进同出的既有口径）⇒ `heal_binding_identity` 桩断言。
        """
        binding = self._member("member-fp-gate")
        key = self._key(binding)
        self.module._UPSTREAM_FAILURE_COUNT[key] = 3
        self.module._UPSTREAM_PENALTY_UNTIL[key] = NOW + timedelta(seconds=120)
        out = io.StringIO()
        with mock.patch.object(jidanpu_member, "poll_log") as quick, \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal:
            self._run("--once", "--fastpath-only", stdout=out)
        quick.assert_not_called()
        heal.assert_not_called()
        self.assertIn(f"代练员绑定 #{binding.pk}", out.getvalue())
        self.assertIn("整行跳过", out.getvalue())

    def _run(self, *args, stdout=None, stderr=None, expect_error=False, now=NOW):
        """与 `_ScheduleTestCase._run` 同形，但参数原样透传（`--fastpath-only` 等开关用）。"""
        out = stdout if stdout is not None else io.StringIO()
        err = stderr if stderr is not None else io.StringIO()
        caught = None
        with mock.patch.object(self.module, "tenant_context", _noop_context), \
             mock.patch.object(self.module.timezone, "now", return_value=now):
            patcher, _ = _patch_tenants(self.module, [self.tenant])
            try:
                try:
                    call_command("jidanpu_sync", *args, stdout=out, stderr=err)
                except CommandError as exc:
                    caught = exc
            finally:
                patcher.stop()
        if expect_error:
            self.assertIsNotNone(caught, "本趟应当以非零退出码收尾，却没有抛 CommandError")
        else:
            self.assertIsNone(caught, f"本趟不该非零退出：{caught}")
        return caught, out.getvalue(), err.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# ②式那一轮的**出向发单**段（⭐ 二期**步骤 5**）
# ══════════════════════════════════════════════════════════════════════════════


class MemberOpenApiOutboxBoundaryTests(_ScheduleTestCase):
    """⭐ 步骤 5 的接线判据：②式那一轮多了一段，而它 ⛔ **不许拖垮①式那几个绑定**。

    ⚠️ 本类**只**钉「接线与边界」（这一段与别的绑定的关系）——那一段自己的行为（发得出去 /
    开关关着不发 / 六档）在 `tests/test_jidanpu_member_openapi_outbox.py` 里，⛔ 不重复测。
    """

    def setUp(self):
        super().setUp()
        #: 回调事件文件指到一个**不存在**的临时路径 ⇒ 回调消费那一段确定「零事件、零上游」
        #: （⛔ 不靠「默认路径恰好没有文件」——那是**环境**给的绿，换台机器就变）。
        env = mock.patch.dict(
            os.environ, {"CALLBACK_EVENTS_PATH": "/tmp/tt-jpw5-schedule-no-events.jsonl"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _openapi_member(self, name, *, enabled=True):
        """一行**界面真建出来的**②式绑定（`account=""` / `password=""` + 地址 / 密钥齐全）。

        ⛔ **不塞假密码**（E-0202 就是被假密码骗过去的）；⭐ 凭据按**②式那把判据**
        （地址 / `client_id` / `client_secret`）给全 ⇒ 它过得了入口闸、**真的进**
        `_run_member_openapi_binding`（否则本类测的是「整行被跳过」）。
        """
        return JidanpuMemberBinding.objects.create(
            user=self._user(name),
            jidanpu_base_url=MEMBER_BASE_URL, account="", password="",
            mode="openapi", client_id=f"synthetic-openapi-{name}",
            client_secret="synthetic-openapi-secret-x", enabled=enabled,
        )

    def _password_paths(self):
        """把①式那四条路换成替身（⭐ 判据钉**哪几个绑定、各调了几次**，⛔ 不是「没报错」）。

        ⚠️ **`run_outbox` 这一格必须分线**（步骤 5 接线后必须改）：②式那一轮的
        出向发单**按设计**经**同一个**共享引擎（`jidanpu_member.run_outbox(..., line=②式那一份)`
        —— 「过没过闸」的判据只有引擎那一处）⇒ 不分线的话，替身的计数会**把②式那一次也算进①式**
        （实测：`3 != 2`，看起来像「①式多跑了一次」）。⇒ 只有**没给 `line`**（①式那一份）
        才记进读数；给了 `line` 的转交真函数（②式那一份）。
        """
        self.password_outbox_calls: list[int] = []
        self.password_fast_calls: list[int] = []
        self.password_pull_calls: list[int] = []
        self.password_reconcile_calls: list[int] = []
        real_run_outbox = jidanpu_member.run_outbox

        def _record(store):
            def _inner(binding, **kwargs):
                store.append(binding.pk)
                return None
            return _inner

        def _password_outbox(binding, **kwargs):
            if kwargs.get("line") is not None:
                return real_run_outbox(binding, **kwargs)
            self.password_outbox_calls.append(binding.pk)
            return jidanpu_member.OutboxStats()

        return (
            mock.patch.object(jidanpu_member, "run_outbox", _password_outbox),
            mock.patch.object(
                jidanpu_member, "poll_log", side_effect=_record(self.password_fast_calls),
            ),
            mock.patch.object(
                jidanpu_member, "pull_statuses", side_effect=_record(self.password_pull_calls),
            ),
            mock.patch.object(
                jidanpu_member, "reconcile_and_queue",
                side_effect=_record(self.password_reconcile_calls),
            ),
        )

    def _assert_only_the_password_bindings_ran(self, expected: list[JidanpuMemberBinding]):
        """①式那四条路各自**恰好**跑了这几个绑定（各一次）。"""
        expected_pks = [binding.pk for binding in expected]
        self.assertEqual(self.password_outbox_calls, expected_pks)
        self.assertEqual(self.password_fast_calls, expected_pks)
        self.assertEqual(self.password_pull_calls, expected_pks)
        self.assertEqual(self.password_reconcile_calls, expected_pks)

    def test_the_openapi_outbox_section_runs_without_disturbing_the_password_bindings(self):
        """⭐⭐ 判据：②式绑定（账号密码为空）+ **开关开** ⇒ 跑一轮 ⇒ ①式那几个绑定照旧正常处理。

        ⭐ 三件事一起钉：
        ① **②式那一段真的被调到**（接线生效）——⛔ 弄红它：把 `jidanpu_sync` 里那一行改回
           「⛔ 不跑」⇒ 下面 `openapi_outbox.assert_called_once()` 红；
        ② **用的是同一个 `now`**（照①式四条路那条判据的同一口径）；
        ③ **①式那几个绑定照旧**：四条路各自**恰好**只跑了那两个①式绑定，退出码 0
           （这一轮没有未预期失败）。

        ⚠️ ②式那一行**没有待发台账** ⇒ 那一段真的跑了但一次上游都不打（`run_outbox` 的
        「空转不建连」那一支）⇒ 本用例**零真实网络**；⛔ 这里的「不发」不是靠开关关着
        （环境变量在本用例里是**开着**的）。
        """
        password_bindings = [self._member("member-pw-a"), self._member("member-pw-b")]
        openapi_binding = self._openapi_member("member-openapi-ok")
        seen: list[tuple[int, datetime]] = []
        real_outbox = jidanpu_member_openapi_outbox.run_member_status_outbox

        def _outbox(binding, **kwargs):
            seen.append((binding.pk, kwargs["now"]))
            return real_outbox(binding, **kwargs)

        outbox_patch, fast_patch, pull_patch, reconcile_patch = self._password_paths()
        with mock.patch.dict(os.environ, {"JIDANPU_MEMBER_OPENAPI_OUTBOX": "1"}), \
             mock.patch.object(
                 jidanpu_member_openapi_outbox, "run_member_status_outbox",
                 side_effect=_outbox,
             ) as openapi_outbox, outbox_patch, fast_patch, pull_patch, reconcile_patch:
            _caught, out, _err = self._run()

        openapi_outbox.assert_called_once()
        self.assertEqual(seen, [(openapi_binding.pk, NOW)])
        self._assert_only_the_password_bindings_ran(password_bindings)
        # ②式那一行的汇总里有出向发单那一段（⛔ 不是静默少打一段）
        self.assertIn(f"代练员绑定 #{openapi_binding.pk}（官方API）", out)
        self.assertIn("出向发单", out)

    def test_a_blowing_up_openapi_outbox_does_not_stop_the_password_bindings(self):
        """⭐⭐ 判据：②式那一段**炸了** ⇒ ①式那几个绑定照旧被处理（⛔ 一段出错不拖垮别人）。

        两半各自能红、由**不同的**错法弄红（都真跑过，读数见报告）：
        ① 「①式那几个绑定照旧各跑一次」——弄红它：把那一行调用挪到**逐绑定边界之外**
           （例如挪进 `_run_member_bindings` 的循环里、`try` 之前）⇒ 异常直接中断遍历
           ⇒ ①式绑定一条都跑不到；
        ② 「stderr 点名是**出向发单**这一段」——弄红它：把那一行自己的 `try/except` 删掉
           ⇒ 异常逃到逐绑定边界，措辞变成「本轮」（那正是①式那条路既有的写法）。

        ⚠️ 异常正文（可能带上游回包 / 凭据片段）一个字都不上行，只写异常类型名（spec §8.5-S1）。
        ⚠️ ②式那一行**建在最前**（绑定按主键序遍历）：它排在前面，后半条判据
        「**后面的**①式绑定照旧跑」才有判别力（建在后面的话，就算异常真把遍历打断了，
        ①式绑定也早就跑完了 —— 那是**假绿**）。
        """
        self._openapi_member("member-openapi-boom")
        password_bindings = [self._member("member-pw-c"), self._member("member-pw-d")]

        def _boom(*args, **kwargs):
            raise RuntimeError("合成炸了-openapi-outbox")

        outbox_patch, fast_patch, pull_patch, reconcile_patch = self._password_paths()
        with mock.patch.dict(os.environ, {"JIDANPU_MEMBER_OPENAPI_OUTBOX": "1"}), \
             mock.patch.object(
                 jidanpu_member_openapi_outbox, "run_member_status_outbox", side_effect=_boom,
             ), outbox_patch, fast_patch, pull_patch, reconcile_patch:
            caught, out, err = self._run(expect_error=True)

        self.assertEqual(caught.returncode, 1)
        # ① ①式那几个绑定照旧正常处理（四条路各自恰好只跑了那两个①式绑定）
        self._assert_only_the_password_bindings_ran(password_bindings)
        for binding in password_bindings:
            with self.subTest(binding=binding.pk):
                self.assertIn(f"代练员绑定 #{binding.pk} ·", out)
        # ② 这一段自己的异常边界接住了它，而且**点名是哪一段**
        self.assertIn("出向发单", err)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("合成炸了-openapi-outbox", err, "异常正文不该上行（可能含凭据 / 上游回包）")


# ══════════════════════════════════════════════════════════════════════════════
# `--loop` 常驻循环（自 `tests/test_jidanpu_sync_command.py` 原样搬来）
#
# ⚠️ **为什么搬**：那个文件测的是**租户级那一段**（`jidanpu_hall.run_round` / `consume_outbox`
# / `interval_elapsed`），它随租户级鸡蛋铺线整条拆除 ⇒ 整份删除。但里面 `LoopTests` 这一组
# 测的是**命令自己的常驻循环**（`handle` 的 `--loop` 分支：睡眠节奏、`--tenant` 闸门、
# 「失败轮不退出」），与租户级那段代码**无关**，今天仍然是**唯一**覆盖 `--loop` 的网
# ⇒ 整组**原样搬到这里**（判据一个字没改），⛔ 不是删掉。
# ══════════════════════════════════════════════════════════════════════════════


class _LoopStopped(Exception):
    """哨兵：`time.sleep` 被叫到第 N 次时抛出，用来从 `--loop` 的死循环里退出。

    ⚠️ 不挂死是硬要求（E-0064 / E-0075）：**挂死的用例算 `BROKEN`、不产出任何结论**。
    所以退出手段是「一睡就抛」，而不是 `timeout` 之类的壳。
    """


class LoopTests(TenantTestCase):
    """`--loop` 的睡眠节奏与「常驻不按 `--once` 的退出码契约收尾」（业务方 2026-09-26）。

    业务方原话：「一分钟的粒度不够 **10 秒以内**比较合适」⇒ 生产调度走常驻 `--loop`
    （cron 最细只有 1 分钟）。
    ⚠️ **睡眠间隔必须是 3 秒**（**裁定 10**）：本命令还要跑**个人绑定那条日志快路**，
    它的默认闸门 `MEMBER_FAST_POLL_SECONDS` = **3 秒**（业务方同日原话「**3秒都可以 别说5秒**」），
    而常驻**一拍最多跑一次** ⇒ 睡眠若大于 3，那条快路配的 3 秒**永远用不上**（实际节奏 = 睡眠值）。
    ⚠️ 判据钉**字面值 3**（⛔ 不拿 `module.LOOP_SLEEP_SECONDS` 自比，E-0024）：
    常量被改大（改回 10 / 60 = 快路退化成 10 秒 / 一分钟粒度）或被改小（无意义地加密空转，
    快路一点没变快）时本用例都必须红。
    ⚠️ 桩打在**被查找的那个模块**上：`_STOP.wait` 在命令模块里查（E-0029）。
    ⚠️ 这里 patch 的是 `Command._run_once`（命令自己的方法），与本文件别处的四条路桩互不干扰。
    ⚠️ D2（M-12）：循环睡眠从 `time.sleep` 换成 `_STOP.wait`（可被
    SIGTERM/SIGINT 打断的优雅停机）⇒ 桩跟着挪；`signal` 一并桩掉——测试进程里装真
    handler 会污染全局状态（照 `test_peer_worker.py` C9 用例的同一条纪律）。
    """

    def test_the_loop_runs_a_round_then_waits_three_seconds(self):
        """先跑一趟、再等 **3 秒**，然后接着跑（顺序与间隔都要对）。

        ⚠️ **为什么是 3**：常驻循环里最快的那条路是个人绑定的日志快路（默认 3 秒闸门）
        ⇒ 一拍 3 秒才让「配了 3 秒」在常驻形态下真的成立（裁定 10）；它同时是快路的**实际上限**
        （`fast_poll_seconds` 配得更小也不会更快）。
        失败方向：把 `_STOP.wait(LOOP_SLEEP_SECONDS)` 的值改掉（改大 = 快路退化；改小 = 无意义地
        加密空转），或删掉那次等待（⇒ 变成**紧循环空转**）⇒ 本用例在 `slept` 上红。
        """
        from apps.business.management.commands import jidanpu_sync as module

        rounds: list[str | None] = []
        slept: list[int] = []

        def _run_once(self, schema, **_kwargs):
            rounds.append(schema)
            return False

        def _sleep(seconds):
            slept.append(seconds)
            if len(slept) >= 2:
                raise _LoopStopped
            return None

        with mock.patch.object(module.Command, "_run_once", _run_once), \
             mock.patch.object(module, "signal"), \
             mock.patch.object(module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(_LoopStopped):
                call_command("jidanpu_sync", "--loop")
        # 序列是「跑 → 睡 → 跑 → 睡」，哨兵在第 2 次睡时退出 ⇒ 跑了 2 趟、睡了 2 次。
        self.assertEqual(rounds, [None, None])
        self.assertEqual(slept, [3, 3])

    def test_the_loop_keeps_the_tenant_gate_every_round(self):
        """`--loop` 下 `--tenant` 闸门**每一轮**都生效（⛔ 不是只第一轮认）。

        失败方向：把 handle 里传给 `_run_once` 的 `schema` 丢掉（传 None）⇒ 本用例红。
        """
        from apps.business.management.commands import jidanpu_sync as module

        rounds: list[str | None] = []

        def _run_once(self, schema, **_kwargs):
            rounds.append(schema)
            return False

        def _sleep(seconds):
            raise _LoopStopped

        with mock.patch.object(module.Command, "_run_once", _run_once), \
             mock.patch.object(module, "signal"), \
             mock.patch.object(module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(_LoopStopped):
                call_command("jidanpu_sync", "--loop", "--tenant", "tenant-a")
        self.assertEqual(rounds, ["tenant-a"])

    def test_the_loop_never_exits_on_a_failed_round(self):
        """`--once` 的退出码契约（未预期失败 ⇒ `CommandError`）**不进常驻循环**：
        常驻进程活着就一直轮询，失败由 systemd 的 `Restart=always` 与绑定上的 `last_ok_at`
        告警兜底（⛔ 它不靠「退出码让 cron 发信」）。

        失败方向：把 `if self._run_once(schema): raise CommandError(...)` 挪到循环里
        ⇒ 本用例拿到 `CommandError` 而不是哨兵 ⇒ 红。
        """
        from apps.business.management.commands import jidanpu_sync as module

        def _run_once(self, schema, **_kwargs):
            return True          # 未预期失败（`--once` 会据此抛 CommandError）

        def _sleep(seconds):
            raise _LoopStopped

        with mock.patch.object(module.Command, "_run_once", _run_once), \
             mock.patch.object(module, "signal"), \
             mock.patch.object(module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(_LoopStopped):
                call_command("jidanpu_sync", "--loop")
