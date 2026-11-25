"""`jidanpu_sync` 命令层：上家绑定的**整轮退避**与**连接池生命周期**
（spec `docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2.1 / §2.3）。

这个文件是 2026-10-20 风控降险里**必须驱动整个命令**才看得见的那一半：

1. **会话复用落到了调度上**：同一轮的三段（outbox / 大厅 / 我的订单）共用**池里的那一个**
   客户端；`--loop` 连跑两轮也只建**一次**客户端（= 只登录一次）。
2. **连续失败退避**：读路段抛了 ⇒ 记一次连续失败、写下指数窗口；窗口内**整轮跳过**
   （连 outbox 一起），⛔ 不静默；整轮干净 ⇒ 清零。
3. **池的收尾**：`--once` 跑完 / `--loop` 被打断时 `release_all()` 真的把客户端关掉
   （⛔ 不泄漏连接池，也⛔ 不吞掉 `Ctrl-C`）。

⚠️ 纯函数那一半（30 / 60 / 120 / 300 的算法）在 `tests/test_jidanpu_waf_risk.py`，
本文件只测**调度接线**（⛔ 不重复验算退避秒数）。
⚠️ 桩打在**被查找的那个模块**上（E-0029）：建连在 `apps.business.jidanpu`（`_open_client_pair`），
三段在 `apps.business.jidanpu_upstream`，`tenant_context` / `Tenant.objects` / `timezone.now`
在命令模块里查。
⚠️ 判据打**桩的调用集合**与**进程内那两个字典**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ 夹具一律**合成值**（E-0077）：域名 `…waf-cmd.test.invalid`、账号 `kefu-waf-cmd-*`、密码 canary。
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_upstream
from apps.business.jidanpu_upstream import HallOutcome, MyOrdersOutcome, OutboxStats
from apps.business.models import JidanpuUpstreamBinding

# ── 合成夹具值（E-0077）───────────────────────────────────────────────────────

TENANT_SCHEMA = "tenant-waf-cmd"
BASE_URL = "https://jidanpu-waf-cmd.test.invalid"
PASSWORD = "pw-canary-waf-cmd"

#: 固定的「现在」：闸门与退避窗口的判据都按它算（⛔ 不用墙钟让断言随运行时刻漂）。
NOW = datetime(2026, 10, 20, 1, 15, 0, tzinfo=dt_timezone.utc)


@contextmanager
def _noop_context(tenant):
    """把 `tenant_context` 换成「什么都不做」（只测接线逻辑，不切库）。"""
    yield


def _patch_tenants(module, tenants):
    """把 `Tenant.objects` 换成回给定租户的替身（照 `test_jidanpu_upstream_schedule` 的形状）。"""
    patcher = mock.patch.object(module.Tenant, "objects")
    objects = patcher.start()
    objects.exclude.return_value.order_by.return_value = list(tenants)
    return patcher, objects


class _CommandTestCase(TenantTestCase):
    """真租户库 + 一个假租户对象；每条用例两头清掉**进程内**状态（它们跨用例会串味）。"""

    #: 本线全部「进程内锚 / 计数」（与命令模块里的定义同名；`getattr` 的兜底只为让
    #: **实现之前**的红是行为红，⛔ 不是「这些名字可以不存在」）。
    PROCESS_LOCAL_NAMES = (
        "_UPSTREAM_HALL_AT",
        "_UPSTREAM_MY_ORDERS_AT",
        "_UPSTREAM_OPENAPI_RECONCILE_AT",
        "_UPSTREAM_FAILURE_COUNT",
        "_UPSTREAM_PENALTY_UNTIL",
        "_MEMBER_FAST_AT",
        "_MEMBER_STATUS_PULL_AT",
        "_MEMBER_RECONCILE_AT",
    )

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        self._clear_process_local()
        # ⚠️ 用例结束再清一次：本文件**故意**制造读路失败，留下的退避窗口会串到别的模块去。
        self.addCleanup(self._clear_process_local)
        self.tenant = mock.Mock(schema_name=TENANT_SCHEMA)

    def _clear_process_local(self):
        for name in self.PROCESS_LOCAL_NAMES:
            getattr(self.module, name, {}).clear()

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _upstream(self, name="main", *, enabled=True, **kwargs):
        user = User.objects.create_user(
            username=f"kefu-waf-cmd-{name}", password="pw-canary-x"
        )
        # ⚠️ `(jidanpu_base_url, account)` 上有唯一约束 ⇒ 账号按用例内的名字派生。
        # ⚠️ `enabled` 的模型默认是 **False**（停用即停）⇒ 不显式给 True 的话这一行根本不会被遍历。
        return JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account=f"acc-waf-cmd-{name}",
            password=PASSWORD, enabled=enabled, **kwargs,
        )

    def _key(self, binding):
        return (TENANT_SCHEMA, binding.pk)

    @contextmanager
    def _patched_upstream(self, *, hall=None):
        """三段各自的桩（`hall` 可以给异常 ⇒ 制造一条读路失败）。"""
        with mock.patch.object(
            jidanpu_upstream, "run_outbox", return_value=OutboxStats(),
        ) as box, mock.patch.object(
            jidanpu_upstream, "sync_hall",
            return_value=HallOutcome() if hall is None else hall,
            **({"side_effect": hall} if isinstance(hall, BaseException) else {}),
        ) as hall_stub, mock.patch.object(
            jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome(),
        ) as orders_stub, mock.patch.object(
            jidanpu, "_open_client_pair", side_effect=self._pair_factory(),
        ) as pair_stub:
            yield box, hall_stub, orders_stub, pair_stub

    def _pair_factory(self):
        """建连替身：产出 `(client, http)`，两样都是可断言的 `Mock`（⛔ 零真实网络）。"""
        def _pair(binding):
            return mock.Mock(name=f"client-{binding.pk}"), mock.Mock(name=f"http-{binding.pk}")
        return _pair

    # ── 跑一趟 ──────────────────────────────────────────────────────────────

    def _run(self, *, now=NOW, expect_error=False):
        """跑一趟 `--once`（租户遍历被替身接管），回 `(CommandError, out, err)`。"""
        out = io.StringIO()
        err = io.StringIO()
        caught = None
        with mock.patch.object(self.module, "tenant_context", _noop_context), \
             mock.patch.object(self.module.timezone, "now", return_value=now):
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
            self.assertIsNone(caught, f"本趟不该非零退出：{caught}")
        return caught, out.getvalue(), err.getvalue()


# ══ ① 会话复用落到调度上 ═══════════════════════════════════════════════════════


class UpstreamPoolWiringTests(_CommandTestCase):
    """一轮的三段共用池里的那一个客户端；`--loop` 跨轮不重建。"""

    def test_one_client_serves_the_outbox_and_both_read_paths(self):
        """同一轮的 outbox / 大厅 / 我的订单拿到的是**同一个** client 对象。

        失败方向：三段各自 `open_client_with_http`（改回原样）⇒ 建连被调 2 次以上、
        走进来的 client 不是同一个 ⇒ 本用例红。
        """
        binding = self._upstream("shared")
        seen: dict[str, object] = {}

        def _box(bind, **kwargs):
            seen["box"] = kwargs.get("client")
            return OutboxStats()

        def _hall(**kwargs):
            seen["hall"] = kwargs.get("client")
            return HallOutcome()

        def _orders(**kwargs):
            seen["orders"] = kwargs.get("client")
            return MyOrdersOutcome()

        with mock.patch.object(jidanpu_upstream, "run_outbox", side_effect=_box), \
             mock.patch.object(jidanpu_upstream, "sync_hall", side_effect=_hall), \
             mock.patch.object(jidanpu_upstream, "sync_my_orders", side_effect=_orders), \
             mock.patch.object(
                 jidanpu, "_open_client_pair", side_effect=self._pair_factory(),
             ) as pair_stub:
            self._run()
        self.assertEqual(pair_stub.call_count, 1, "一轮只该建一次客户端")
        self.assertIsNotNone(seen.get("box"))
        self.assertIs(seen.get("box"), seen.get("hall"), "读路必须复用 outbox 那个客户端")
        self.assertIs(seen.get("hall"), seen.get("orders"))

    def test_the_loop_reuses_the_same_client_across_rounds(self):
        """`--loop` 连跑两轮 ⇒ 建连**只发生一次**（跨轮复用 = 只登录一次）。

        失败方向：把池建成「每轮新建一个」/每轮 `release_all` ⇒ 建连 2 次 ⇒ 本用例红。
        ⚠️ 两轮之间会被打断（停止位在第 2 次 `wait` 上引发 `KeyboardInterrupt`）⇒ 顺便钉住
        「池的收尾不吞中断」**且真的把池里的客户端关掉**（`close` 至少 1 次）。
        ⚠️ D2（M-12）：循环睡眠从 `time.sleep` 换成 `_STOP.wait`（可被
        信号打断的优雅停机）⇒ 本用例的桩从 `module.time` 挪到 `module._STOP.wait`（同一处
        承重点）；`signal` 桩掉——测试进程里装真 handler 会污染全局状态（C9 同一条）。
        """
        self._upstream("loop")
        waits: list[int] = []
        pairs: list[tuple] = []

        def _pair(_binding):
            pair = (mock.Mock(name="client"), mock.Mock(name="http"))
            pairs.append(pair)
            return pair

        def _wait(_seconds):
            waits.append(1)
            if len(waits) >= 2:
                raise KeyboardInterrupt

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()), \
             mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome(),
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", side_effect=_pair,
             ) as pair_stub, mock.patch.object(
                 self.module, "signal"
             ), mock.patch.object(
                 self.module._STOP, "wait", side_effect=_wait,
             ), mock.patch.object(
                 self.module, "tenant_context", _noop_context,
             ), mock.patch.object(
                 self.module.timezone, "now", return_value=NOW,
             ):
            patcher, _ = _patch_tenants(self.module, [self.tenant])
            try:
                with self.assertRaises(KeyboardInterrupt):
                    call_command("jidanpu_sync", "--loop", stdout=out, stderr=err)
            finally:
                patcher.stop()
        self.assertEqual(len(waits), 2, "应当真的跑了两轮才被打断")
        self.assertEqual(pair_stub.call_count, 1, "跨轮必须复用同一个客户端（即同一次登录）")
        # ⭐ 中断退出时池里的客户端必须被关掉（`handle` 的 `finally`）——⛔ 不泄漏连接池，
        #    也⛔ 不因为收尾把 `Ctrl-C` 吞掉（上面那条 `assertRaises` 已经钉住「不吞」）。
        self.assertEqual(len(pairs), 1)
        pairs[0][0].close.assert_called_once_with()
        pairs[0][1].close.assert_called_once_with()


# ══ ② 连续失败的整轮退避 ═══════════════════════════════════════════════════════


class UpstreamPenaltyTests(_CommandTestCase):
    """退避窗口内的那一轮**整轮跳过**（连 outbox 一起），干净的一轮清零。"""

    def test_the_skipped_round_still_advances_last_run_at(self):
        """窗口内那一轮**一次上游都不打**，但 `last_run_at` 照样前进（⛔ 页面别像「同步死了」）。

        ⚠️ 这条为什么必须有（评审 I-1）：退避最长 **300 秒**，而运维判「这条线在不在跑」
        第一眼看的就是 `last_run_at`（`docs/RUNBOOK.md` 那节第 3 条）⇒ 跳过时不打这一列，
        页面上会显示「上次运行 = 窗口起点」，人第一反应是去重启一个没坏的服务。

        ⛔ **只钉 `last_run_at` 前进**：`last_ok_at` / `last_error` / `last_error_at` **一个字都不许动**
        ——那是「退避 ≠ 成功、也 ≠ 新失败」的四档语义（⛔ 别为了「让页面好看」顺手盖成功）。

        失败方向：跳过分支直接 `return False`（不打这一列）⇒ 本用例红。
        """
        binding = self._upstream("skip-stamp")
        key = self._key(binding)
        self.module._UPSTREAM_FAILURE_COUNT[key] = 1
        self.module._UPSTREAM_PENALTY_UNTIL[key] = NOW + timedelta(seconds=30)
        earlier = NOW - timedelta(minutes=10)
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_run_at=earlier)
        with self._patched_upstream() as (box, hall, orders, pair_stub):
            _caught, out, _err = self._run()
        box.assert_not_called()
        hall.assert_not_called()
        orders.assert_not_called()
        pair_stub.assert_not_called()
        self.assertIn("整轮跳过", out)
        refreshed = JidanpuUpstreamBinding.objects.get(pk=binding.pk)
        self.assertEqual(refreshed.last_run_at, NOW, "退避的那一轮也要打「上次运行」")
        # ⛔ 四档语义不动：退避既不是成功、也不写新的失败原因。
        self.assertIsNone(refreshed.last_ok_at)
        self.assertEqual(refreshed.last_error, "")
        self.assertIsNone(refreshed.last_error_at)

    def test_a_round_inside_the_window_is_skipped_whole(self):
        """窗口未过 ⇒ 三段一条都不调、⛔ 连建连都不建，且**不静默**（stdout 有一行）。

        失败方向：只挡读路不挡 outbox（被 WAF 挡时同一个 IP 仍在发请求）⇒ `box` 被调 ⇒ 本用例红；
        或者跳过时不打那一行 ⇒ stdout 的断言红。
        """
        binding = self._upstream("skipped")
        self.module._UPSTREAM_FAILURE_COUNT[self._key(binding)] = 1
        self.module._UPSTREAM_PENALTY_UNTIL[self._key(binding)] = NOW + timedelta(seconds=30)
        with self._patched_upstream() as (box, hall, orders, pair_stub):
            _caught, out, _err = self._run()
        box.assert_not_called()
        hall.assert_not_called()
        orders.assert_not_called()
        pair_stub.assert_not_called()
        self.assertIn(f"上家绑定 #{binding.pk}", out)
        self.assertIn("整轮跳过", out)

    def test_a_read_failure_starts_the_window_and_the_next_round_is_skipped(self):
        """读路抛 ⇒ 窗口 = `now + 30` 秒；紧接着那一轮（1 秒后）**一次上游都不打**。

        失败方向：失败不记窗口 ⇒ 第二趟照打（`sync_hall` 被调 2 次）⇒ 本用例红；
        或者窗口算成固定 30 秒之外的别的值 ⇒ 字典断言红。
        """
        binding = self._upstream("penalty")
        key = self._key(binding)
        with self._patched_upstream(hall=RuntimeError("boom")):
            _caught, _out, err = self._run(expect_error=True)
        self.assertEqual(self.module._UPSTREAM_FAILURE_COUNT.get(key), 1)
        self.assertEqual(
            self.module._UPSTREAM_PENALTY_UNTIL.get(key), NOW + timedelta(seconds=30)
        )
        # ⛔ 不静默：本轮就要说「接下来多少秒退避」
        self.assertIn("整轮退避", err)
        self.assertIn("30 秒", err)

        calls: list[int] = []

        def _hall(**kwargs):
            calls.append(1)
            return HallOutcome()

        with mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()) as box, \
             mock.patch.object(jidanpu_upstream, "sync_hall", side_effect=_hall), \
             mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome(),
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", side_effect=self._pair_factory(),
             ) as pair_stub:
            _caught, out, _err = self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(calls, [], "窗口内的那一轮不许再打大厅")
        box.assert_not_called()
        pair_stub.assert_not_called()
        self.assertIn("整轮跳过", out)

    def test_a_clean_round_clears_the_window(self):
        """窗口已过 + 这一轮干净 ⇒ 计数与窗口都清掉（下一次失败从 30 秒重新起算）。

        失败方向：跑完不清 ⇒ 两个字典仍留着旧值（窗口过期了但计数还在）⇒ 本用例红。
        """
        binding = self._upstream("clean")
        key = self._key(binding)
        self.module._UPSTREAM_FAILURE_COUNT[key] = 2
        self.module._UPSTREAM_PENALTY_UNTIL[key] = NOW - timedelta(seconds=1)
        with self._patched_upstream():
            self._run()
        self.assertNotIn(key, self.module._UPSTREAM_FAILURE_COUNT)
        self.assertNotIn(key, self.module._UPSTREAM_PENALTY_UNTIL)

    def test_a_clean_round_keeps_the_count_at_zero_across_bindings(self):
        """两个绑定：一个失败、一个干净 ⇒ 干净的那个不留计数（⛔ 被别人的失败带累）。

        失败方向：键写成「整个租户一个」（丢 pk）⇒ 干净那个也会被记一次失败 ⇒ 本用例红。
        """
        broken = self._upstream("broken")
        healthy = self._upstream("healthy")

        def _hall(**kwargs):
            if kwargs["binding"].pk == broken.pk:
                raise RuntimeError("boom")
            return HallOutcome()

        with mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()), \
             mock.patch.object(jidanpu_upstream, "sync_hall", side_effect=_hall), \
             mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome(),
             ), mock.patch.object(jidanpu, "_open_client_pair", side_effect=self._pair_factory()):
            self._run(expect_error=True)
        self.assertEqual(self.module._UPSTREAM_FAILURE_COUNT.get(self._key(broken)), 1)
        self.assertNotIn(self._key(healthy), self.module._UPSTREAM_FAILURE_COUNT)


# ══ ③ 池的收尾 ═══════════════════════════════════════════════════════════════


class UpstreamPoolReleaseTests(_CommandTestCase):
    """`--once` 跑完 ⇒ 池里那些客户端被真的关掉（⛔ 泄漏连接池）。"""

    def test_the_pool_is_released_when_once_finishes(self):
        """跑完一趟：`client.close()` 与 `http.close()` 各被调一次。

        失败方向：把 `handle` 的 `finally` 去掉 ⇒ 两个 `close` 都没被调 ⇒ 本用例红。
        """
        self._upstream("release")
        pair = (mock.Mock(name="client"), mock.Mock(name="http"))
        with mock.patch.object(
                 jidanpu, "_open_client_pair", new=lambda _binding: pair,
             ), mock.patch.object(
                 jidanpu_upstream, "run_outbox", return_value=OutboxStats(),
             ), mock.patch.object(
                 jidanpu_upstream, "sync_hall", return_value=HallOutcome(),
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome(),
             ):
            self._run()
        pair[0].close.assert_called_once_with()
        pair[1].close.assert_called_once_with()
