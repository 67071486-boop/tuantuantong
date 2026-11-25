"""`jidanpu_sync` 的五对互斥开关：①出向（`--outbox-only` / `--skip-outbox`）、
②式回调（`--callback-only` / `--skip-callback`）、①式快路（`--fastpath-only` /
`--skip-fastpath`）、①式状态回拉 + 对账 + 接单兜底网（`--reconcile-only` /
`--skip-reconcile`）、上家线（`--upstream-only` / `--skip-upstream`）。

依据：`docs/plans/2026-10-25-jidanpu-outbox-worker.md` §2 / §3（①与②那两对的权威 spec）
与 `docs/plans/2026-10-25-jidanpu-sync-decoupling.md` §1 / §2（快路与对账那两对，终审改准口径）。

## 这两对开关为什么要有

出向事件消费（推单 / 指派 / 抛单 / 退单 / 改价 / 项目 / 状态）与「日志快路 / 状态回拉 /
对账」挤在同一个 `jidanpu_sync --loop` 进程里串行跑 ⇒ 一条上游调用慢，整轮所有租户所有绑定
一起等。拆成两个进程之后：

- `--outbox-only`：**只**消费下家代练员绑定的出向台账，不跑任何轮询 / 对账
  （新单元 `tuantuantong-jidanpu-outbox` 用这个形态）；
- `--skip-outbox`：原有那台 `jidanpu_sync` 的一轮里**跳过① outbox**，其余三段照旧；
- 两个同时给 ⇒ `CommandError`（一个只跑出向、一个跳过出向，同时给是写错了）；
- 都不给 ⇒ 行为**逐字不变**（回归判据，见下面最后一条用例）。

## ②式（官方 API）回调消费再拆一个进程（续）

②式的「回调消费 + 兜底对账 + 出向发单」也在那一个轮次里，且与①式**完全不同的段**：

- `--callback-only`：**只**跑②式绑定的那一段（`consume_member_callbacks` →
  `reconcile_member_statuses` → `run_member_status_outbox`），不跑①式的 outbox / 快路 / 回拉 /
  对账、不跑上家线、不跑模拟单（新单元 `tuantuantong-jidanpu-callback` 用这个形态）；
- `--skip-callback`：原有一轮里**只跳过②式那一段**，其余照旧（主单元与 `--skip-outbox` 同给）；
- 两个同时给 ⇒ `CommandError`；都不给 ⇒ 行为**逐字不变**（回归判据）。
- ⚠️ **②式的出向发单仍留在同一条回调支里**：它依赖「先消费回调补远端单号」的**同轮顺序**
  （见 `_run_member_openapi_binding` 的表第 1 行）⇒ ⛔ 不许把它拆到 `--outbox-only` 那个单元。

## 判据打在「调用集合」上（`AGENTS.md` §5-G）

桩打在**被查找的那个模块**上（E-0029）：命令模块里查 `tenant_context` / `Tenant.objects` /
`timezone.now`，四条路在 `apps.business.jidanpu_member` 里查。
⚠️ 夹具一律**合成值**（E-0077）：域名 `…outbox-switch.test.invalid`、账号按用例名派生、密码 canary。

## ②式（官方 API）为什么不在**出向**那个进程里消费（⚠️ 计划没写，实现按 §6 的风险口径收窄）

计划的 §6 写着「只针对下家线：下家线的 outbox 不依赖同一轮的回调消费」。那句话对 **①式**
成立，对**②式下家线不成立**：`_run_member_openapi_binding` 的表里写明它的第三段（出向发单）
**必须在**同轮回调消费之后（回调消费会补上链路缺的远端单号，而发单六档每一档都要拿它去打
对方 ⇒ 顺序反过来就是「拿空号去办」）。⇒ `--outbox-only` 那个进程**只消费①式**的出向台账：
`--outbox-only` 跳过 `mode="openapi"` 的行、`--skip-outbox` 也**不**动②式那一段。
两条都各有用例钉住（免得后人以为那是漏写）。

⭐ **2026-10-25 续**：②式那一段**整体**（含它自己的出向发单）搬进另一个进程
（`--callback-only`），与「拆到 outbox 进程」是两件事——顺序依赖仍在**同一个进程**里满足。

## ①式快路（含身份自愈）再拆一台

- `--fastpath-only`：只跑下家①式绑定的日志快路（`poll_log`），身份核对在同一个会话内完成，⛔ 不跑状态回拉 / 对账 / 出向 / ②式 / 上家线 / 模拟单
  （新单元 `tuantuantong-jidanpu-fastpath` 用这个形态）；
- `--skip-fastpath`：主一轮里**跳过 ⓪ 自愈 + ② 快路**，其余照旧；
- 两个同时给 ⇒ `CommandError`；都不给 ⇒ 行为**逐字不变**（回归判据）。
- 专用进程在日志快路内核对登录账号，主进程给 `--skip-fastpath` 后跳过身份核对和快路。

## ①式状态回拉 + 对账 + 接单兜底网再拆一台

- `--reconcile-only`：**只**跑下家①式绑定的 **③ 状态回拉**（`pull_statuses`）+
  **④ 对账排事件**（`reconcile_and_queue`）+ **⑤ 接单兜底网**（`claim_net`），
  三段**同一个单元、各有各的闸门**（回拉 `status_pull_seconds` 默认 600 /
  对账 `reconcile_seconds` 默认 180 / 兜底网 `CLAIM_NET_SECONDS`=180 常量）；
  ⛔ 不跑 heal / 快路 / 出向 / ②式 / 上家线 / 模拟单
  （单元 `tuantuantong-jidanpu-reconcile` 用这个形态）；
- `--skip-reconcile`：主一轮里**跳过 ③ + ④ + ⑤**，其余照旧；
- 两个同时给 ⇒ `CommandError`；都不给 ⇒ 行为**逐字不变**（回归判据）。
- ⚠️⛔ **三段必须同进同出**：拆成两台会变成「同一段慢路两台各跑一遍」（重复回拉 +
  重复排事件 + 重复兜底）。下面用「`--reconcile-only` **三段都调**」+
  「`--skip-reconcile` **三段都不调**」钉死归属。
- ⚠️ ②式那条支的**兜底对账**（`reconcile_member_statuses`）**不在**这一对开关的范围里：
  它是②式自己那一段、跟回调同一轮（`--callback-only`），⛔ 别顺手把它也停了。

## 上家线（`JidanpuUpstreamBinding`）整行拆一台

- `--upstream-only`：**只**跑上家线的**整行** `_run_upstream_binding`（①式 = outbox + 大厅 +
  我的订单；②式 = 回调消费 + 兜底对账），⛔ 不跑下家任何一段、⛔ 不跑模拟单
  （新单元 `tuantuantong-jidanpu-upstream` 用这个形态）；
- `--skip-upstream`：主一轮里**跳过整行**上家绑定（主单元此后只剩租户级模拟单轮）；
- 两个同时给 ⇒ `CommandError`；都不给 ⇒ 行为**逐字不变**（回归判据）。

⚠️⛔ **上家线只能「整行」拆，⛔ 不许按「大厅 / 回拉 / 对账」子段再拆**（计划终审改准）：
它的**健康合成判据**（`last_ok_at` 只在「至少一条读路到点 + 三段都没未预期异常 + 大厅拉全 +
outbox 没记失败」时才盖）与**整行连续失败退避**（`_UPSTREAM_PENALTY_UNTIL` 跳的是整行、
连 outbox 一起）都是**行级**的——拆成子段会让每台各算一次「整行是否健康 / 是否退避」，
判据互相打架。所以本对开关的边界就是 `_run_upstream_binding` 这一个方法，
用例断的是「整行三段都跑 / 下家一段都不跑」。
"""
from __future__ import annotations

import io
import os
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
import unittest
from pathlib import Path
from threading import Event, get_ident
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business import (
    hall_simulated,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_member_openapi_outbox,
    jidanpu_proxy,
    jidanpu_upstream,
)
from apps.business.models import JidanpuMemberBinding, JidanpuUpstreamBinding

# ── 合成夹具值（E-0077）───────────────────────────────────────────────────────

TENANT_SCHEMA = "tenant-outbox-switch"
MEMBER_BASE_URL = "https://jidanpu-outbox-switch.test.invalid"
#: 上家线绑定用的合成地址（与下家那个分开，免得用例之间看串）。
UPSTREAM_BASE_URL = "https://jidanpu-upstream-switch.test.invalid"
#: 固定的「现在」（⛔ 不用墙钟：`now` 会原样传进 `run_outbox`，断言要钉值）。
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt_timezone.utc)


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


class _SwitchTestCase(TenantTestCase):
    """真租户库 + 一个假租户对象；每条用例两头清掉**进程内**锚（跨用例会串味）。"""

    PROCESS_LOCAL_NAMES = (
        "_MEMBER_FAST_AT", "_MEMBER_STATUS_PULL_AT", "_MEMBER_RECONCILE_AT",
        "_MEMBER_CLAIM_NET_AT",
        "_HALL_SIM_AT",
        #: 上家线的三个锚 + 连续失败退避两个字典（同族：进程内、跨用例会串味）。
        "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
        "_UPSTREAM_OPENAPI_RECONCILE_AT",
        "_UPSTREAM_FAILURE_COUNT", "_UPSTREAM_PENALTY_UNTIL",
    )

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        self._clear_process_local()
        self.addCleanup(self._clear_process_local)
        self.tenant = mock.Mock(schema_name=TENANT_SCHEMA)

    def _clear_process_local(self):
        for name in self.PROCESS_LOCAL_NAMES:
            getattr(self.module, name, {}).clear()

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _member(self, name, *, enabled=True, **kwargs):
        """一条**①式**（账号 + 密码）绑定（`enabled` 默认 False ⇒ 必须显式给）。"""
        return JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(
                username=f"kefu-outbox-switch-{name}", password="pw-canary-x"
            ),
            jidanpu_base_url=MEMBER_BASE_URL,
            account=f"acc-outbox-switch-{name}",
            account_name="传令兵甲",
            password="pw-member-canary",
            enabled=enabled,
            **kwargs,
        )

    def _member_openapi(self, name, *, enabled=True):
        """一条**②式**（官方 API）绑定：账号密码为空、凭据在 client 那两列。"""
        return JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(
                username=f"kefu-outbox-switch-api-{name}", password="pw-canary-x"
            ),
            jidanpu_base_url=MEMBER_BASE_URL,
            account="",
            password="",
            mode=JidanpuMemberBinding.Mode.OPENAPI,
            client_id=f"cid-outbox-switch-{name}",
            client_secret="csec-canary-outbox-switch",
            enabled=enabled,
        )

    def _upstream(self, name, *, enabled=True):
        """一条**上家线**（①式）绑定：地址 / 账号 / 密码齐全 ⇒ 过凭据闸。"""
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.create_user(
                username=f"kefu-upstream-switch-{name}", password="pw-canary-x"
            ),
            jidanpu_base_url=UPSTREAM_BASE_URL,
            account=f"acc-upstream-switch-{name}",
            password="pw-upstream-canary",
            enabled=enabled,
        )

    @contextmanager
    def _patched_round(self):
        """①式那一轮的出口全换桩（`heal_binding_identity` 是尽力而为的旁路，也换掉）。
        ⚠️ ⑤ 接单兜底网（`claim_net`）同样桩成 0 但**不 yield**：需要断言它的用例在外层
        再显式 patch 一次拿句柄（照 `heal_binding_identity` 的既有做法）。"""
        with mock.patch.object(
            jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats(),
        ) as box, mock.patch.object(
            jidanpu_member, "poll_log", return_value=0,
        ) as fast, mock.patch.object(
            jidanpu_member, "pull_statuses", return_value=0,
        ) as pull, mock.patch.object(
            jidanpu_member, "reconcile_and_queue",
            return_value=jidanpu_member.QueueStats(),
        ) as reconcile, mock.patch.object(
            jidanpu_member, "heal_binding_identity",
        ), mock.patch.object(
            jidanpu_member, "claim_net", return_value=0,
        ):
            yield box, fast, pull, reconcile

    @contextmanager
    def _patched_openapi_round(self):
        """②式那一段的三个出口全换桩（回调消费 / 兜底对账 / 出向发单）。"""
        consume = mock.Mock(alert="", applied=0, skipped=0, text="")
        reconcile = mock.Mock(alert="", applied=0, skipped=0, text="")
        with mock.patch.object(
            jidanpu_member_openapi, "consume_member_callbacks", return_value=consume,
        ) as consume_stub, mock.patch.object(
            jidanpu_member_openapi, "reconcile_member_statuses", return_value=reconcile,
        ) as reconcile_stub, mock.patch.object(
            jidanpu_member_openapi_outbox, "run_member_status_outbox",
            return_value=jidanpu_member.OutboxStats(),
        ) as outbox_stub:
            yield consume_stub, reconcile_stub, outbox_stub

    @contextmanager
    def _patched_upstream_row(self):
        """上家①式那一轮的三段全换桩（outbox / 大厅 / 我的订单；零真实网络）。"""
        with mock.patch.object(
            jidanpu_upstream, "run_outbox",
            return_value=jidanpu_upstream.OutboxStats(),
        ) as box, mock.patch.object(
            jidanpu_upstream, "sync_hall",
            return_value=jidanpu_upstream.HallOutcome(),
        ) as hall, mock.patch.object(
            jidanpu_upstream, "sync_my_orders",
            return_value=jidanpu_upstream.MyOrdersOutcome(),
        ) as orders:
            yield box, hall, orders

    # ── 跑一趟 ──────────────────────────────────────────────────────────────

    def _run(self, *args, expect_error=False, now=NOW):
        """跑一趟命令（租户遍历被替身接管），回 `(CommandError 或 None, out, err)`。"""
        out = io.StringIO()
        err = io.StringIO()
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


# ══ ① `--outbox-only`：只消费出向台账 ═════════════════════════════════════════


class OutboxOnlyTests(_SwitchTestCase):
    """`--outbox-only` 只调 `run_outbox`，⛔ 不碰轮询 / 回拉 / 对账 / 上家线。"""

    def test_outbox_only_calls_only_run_outbox(self):
        """两个①式绑定 ⇒ `run_outbox` 各一次；另外三段与上家线**一次都不调**。

        失败方向：把它实现成「还是跑整轮」⇒ 另外三段被调 ⇒ 本用例红；
        或者忘了按绑定遍历 ⇒ `run_outbox` 只被调一次 ⇒ 本用例红。
        """
        first = self._member("only-a")
        second = self._member("only-b")
        with self._patched_round() as (box, fast, pull, reconcile), \
             mock.patch.object(jidanpu_upstream, "run_outbox") as upstream_box, \
             mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            self._run("--once", "--outbox-only")
        self.assertEqual(box.call_count, 2, "每个启用中的①式绑定各消费一次")
        self.assertIn(mock.call(first, now=NOW), box.call_args_list)
        self.assertIn(mock.call(second, now=NOW), box.call_args_list)
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        upstream_box.assert_not_called()
        sim.assert_not_called()

    def test_outbox_only_keeps_going_when_one_binding_raises(self):
        """逐绑定异常边界：一个绑定抛，下一个照跑（同一轮里不中断）。

        失败方向：异常边界挪到整轮外面（或根本没有）⇒ 第二个绑定不再被调 ⇒ 本用例红。
        ⚠️ `--once` 下「有未预期失败」仍按既有退出码契约抛 `CommandError`——这条**不算**放宽边界：
        边界管的是「别的绑定还跑不跑」，退出码管的是「这趟有没有出事」。
        """
        broken = self._member("broken")
        healthy = self._member("healthy")
        seen: list[int] = []

        def _box(binding, **kwargs):
            seen.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("boom")
            return jidanpu_member.OutboxStats()

        with mock.patch.object(jidanpu_member, "run_outbox", side_effect=_box), \
             mock.patch.object(jidanpu_member, "poll_log") as fast, \
             mock.patch.object(jidanpu_member, "pull_statuses") as pull, \
             mock.patch.object(jidanpu_member, "reconcile_and_queue") as reconcile, \
             mock.patch.object(jidanpu_member, "heal_binding_identity"):
            _caught, _out, err = self._run(
                "--once", "--outbox-only", expect_error=True
            )
        self.assertEqual(seen, [broken.pk, healthy.pk], "坏的那个不许挡住后面的绑定")
        self.assertIn(f"代练员绑定 #{broken.pk}", err)
        self.assertIn("RuntimeError", err)
        self.assertIn("继续下一个", err)
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()

    def test_outbox_only_leaves_openapi_bindings_to_the_sync_round(self):
        """②式绑定**不**由本进程消费（它的出向有同轮回调顺序硬依赖，见模块 docstring）。

        失败方向：不筛 `mode` ⇒ ②式行也进 `run_outbox`（缺省取①式差异面）⇒ 一有在飞事件就
        每 2 秒在建连处 `ConfigError`，并且拿①式登录去打一张账号密码为空的绑定 ⇒ 本用例红。
        """
        self._member_openapi("official")
        with self._patched_round() as (box, _fast, _pull, _reconcile):
            self._run("--once", "--outbox-only")
        box.assert_not_called()


# ══ ② `--skip-outbox`：原有那一轮跳过① outbox ═════════════════════════════════


class SkipOutboxTests(_SwitchTestCase):
    """`--skip-outbox` 不调 `run_outbox`，其余三段照旧；②式那一段不受影响。"""

    def test_skip_outbox_calls_the_other_three_paths(self):
        """①式绑定：`run_outbox` 一次都不调；快路 / 回拉 / 对账各一次（顺序也不变）。

        失败方向：把整条 `_run_member_binding` 都跳掉 ⇒ 另外三段不被调 ⇒ 本用例红。
        """
        self._member("skip")
        with self._patched_round() as (box, fast, pull, reconcile):
            self._run("--once", "--skip-outbox")
        box.assert_not_called()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()

    def test_skip_outbox_leaves_the_openapi_line_untouched(self):
        """②式那一轮照旧跑（含它自己的出向发单）：计划只写「跳过 ① outbox」。

        失败方向：把 `skip_outbox` 顺手传进 `_run_member_openapi_binding`（或在它里面也判一次）
        ⇒ ②式的出向发单不被调 ⇒ 本用例红（那会让②式绑定在开关打开之后**永远不发单**）。
        """
        self._member_openapi("official")
        consume = mock.Mock(alert="", applied=0, skipped=0, token_rejected=False)
        reconcile = mock.Mock(alert="", applied=0, skipped=0, text="")
        with mock.patch.object(
            jidanpu_member_openapi, "consume_member_callbacks", return_value=consume,
        ) as consume_stub, mock.patch.object(
            jidanpu_member_openapi, "reconcile_member_statuses", return_value=reconcile,
        ) as reconcile_stub, mock.patch.object(
            jidanpu_member_openapi_outbox, "run_member_status_outbox",
            return_value=jidanpu_member.OutboxStats(),
        ) as api_outbox, mock.patch.object(
            jidanpu_member, "run_outbox",
        ) as plain_outbox:
            self._run("--once", "--skip-outbox")
        plain_outbox.assert_not_called()
        consume_stub.assert_called_once()
        reconcile_stub.assert_called_once()
        api_outbox.assert_called_once()


# ══ ③ 两个开关互斥 + 默认行为不变 ═════════════════════════════════════════════


class SwitchContractTests(_SwitchTestCase):
    """两个开关同给 ⇒ `CommandError`；都不给 ⇒ 四段全调（回归）。"""

    def test_both_switches_together_raise_a_command_error(self):
        """一个「只跑出向」、一个「跳过出向」同时给 ⇒ 响亮报错，⛔ 不静默挑一个执行。"""
        for extra in (("--once",), ("--loop",)):
            with self.subTest(extra=extra):
                with self.assertRaises(CommandError) as caught:
                    call_command(
                        "jidanpu_sync", *extra, "--outbox-only", "--skip-outbox",
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertIn("--outbox-only", str(caught.exception))
                self.assertIn("--skip-outbox", str(caught.exception))

    def test_the_default_round_still_calls_all_five_paths(self):
        """都不给 ⇒ heal + ① outbox → 快路 → 回拉 → 对账 → 兜底网，各一次（行为逐字不变）。"""
        self._member("default")
        with self._patched_round() as (box, fast, pull, reconcile), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net:
            self._run("--once")
        heal.assert_called_once()
        box.assert_called_once()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()
        net.assert_called_once()

    def test_the_default_round_still_calls_the_openapi_segment(self):
        """都不给 ⇒ ②式那一段照旧跑（回调消费 / 兜底对账 / 出向发单），行为逐字不变。"""
        api = self._member_openapi("default-api")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box):
            self._run("--once")
        consume.assert_called_once_with(api, now=NOW)
        self.assertEqual(consume.call_count, 1, "②式回调消费每轮每绑定一次")
        recon.assert_called_once()
        api_box.assert_called_once()
        box.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()

    def test_each_of_the_six_switch_pairs_is_mutually_exclusive(self):
        """六对互斥开关：**任一对的两个同时给**都必须响亮报错（⛔ 不静默挑一个执行）。

        六对 = `--once`/`--loop`、`--outbox-only`/`--skip-outbox`、
        `--callback-only`/`--skip-callback`、`--fastpath-only`/`--skip-fastpath`、
        `--reconcile-only`/`--skip-reconcile`、`--upstream-only`/`--skip-upstream`。
        失败方向：新加的那一对照抄 outbox 那对的**分支**却漏了校验 ⇒ 本用例在那一对上红；
        或者把校验写成「先到先得」（只判前者）⇒ 反顺序那一支红。
        """
        pairs = (
            ("--once", "--loop"),
            ("--outbox-only", "--skip-outbox"),
            ("--callback-only", "--skip-callback"),
            ("--fastpath-only", "--skip-fastpath"),
            ("--reconcile-only", "--skip-reconcile"),
            ("--upstream-only", "--skip-upstream"),
        )
        for first, second in pairs:
            with self.subTest(pair=(first, second)):
                with self.assertRaises(CommandError) as caught:
                    call_command(
                        "jidanpu_sync", first, second,
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                message = str(caught.exception)
                # ⚠️ 必须是我们自己那句「不能同时给」：命令行解析器对**不认识的**开关也抛
                #    `CommandError`（"unrecognized arguments"），那句话同样点名两个开关 ⇒
                #    只断「点名」的话，这一对在**开关还没实现时**就是假绿（本轮实测踩到）。
                self.assertIn("不能同时给", message, f"不是互斥校验那一句：{message}")
                self.assertIn(first, message, f"报错话里没点名 {first}")
                self.assertIn(second, message, f"报错话里没点名 {second}")

    def test_any_two_only_switches_cannot_both_be_on(self):
        """任意两个「**只**跑某一段」的开关同给 ⇒ 谁执行都会静默忽略另一个 ⇒ 响亮报错。

        失败方向：只判同一对内部互斥、不判两台「only」之间互斥 ⇒ 命令会进先判的那个分支
        并 `return`，另一段被**静默**跳掉（无输出、无报错、无台账）⇒ 本用例红。
        ⚠️ 五个「only」两两组合共**十对**都要判（出向×回调、出向×快路、出向×对账、
        回调×快路、回调×对账、快路×对账，再各加上家：出向×上家、回调×上家、快路×上家、
        对账×上家）：漏哪一对，那一对的两个进程就会「看起来都 active、实际只有一台在干活」。
        """
        pairs = (
            ("--outbox-only", "--callback-only"),
            ("--outbox-only", "--fastpath-only"),
            ("--outbox-only", "--reconcile-only"),
            ("--callback-only", "--fastpath-only"),
            ("--callback-only", "--reconcile-only"),
            ("--fastpath-only", "--reconcile-only"),
            ("--outbox-only", "--upstream-only"),
            ("--callback-only", "--upstream-only"),
            ("--fastpath-only", "--upstream-only"),
            ("--reconcile-only", "--upstream-only"),
        )
        for first, second in pairs:
            with self.subTest(pair=(first, second)):
                with self.assertRaises(CommandError) as caught:
                    call_command(
                        "jidanpu_sync", first, second,
                        stdout=io.StringIO(), stderr=io.StringIO(),
                    )
                self.assertIn("不能同时给", str(caught.exception))
                self.assertIn(first, str(caught.exception))
                self.assertIn(second, str(caught.exception))


# ══ ④ `--loop --outbox-only`：常驻形态的节奏 ═══════════════════════════════════


class OutboxLoopTests(_SwitchTestCase):
    """`--loop --outbox-only` 每轮之后睡 `OUTBOX_LOOP_SLEEP_SECONDS`（2 秒）。"""

    def test_outbox_loop_sleeps_two_seconds_between_rounds(self):
        """第一轮跑完 ⇒ `wait(2)`；等待时被打断 ⇒ 只跑了一轮。

        失败方向：误用 `LOOP_SLEEP_SECONDS`（3 秒）⇒ 断言的字面值红；
        或者不等待（紧循环打 DB）⇒ `wait` 没被调 ⇒ 本用例红。
        ⚠️ D2（M-12）：睡眠从 `time.sleep` 换成 `_STOP.wait`（可被
        SIGTERM/SIGINT 打断的优雅停机）⇒ 桩挪到 `module._STOP.wait`（同一处承重点），
        `signal` 桩掉（测试进程里装真 handler 会污染全局状态）。
        """
        self._member("loop")
        sleeps: list[float] = []

        def _sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self._patched_round() as (box, _fast, _pull, _reconcile), \
             mock.patch.object(self.module, "signal"), \
             mock.patch.object(self.module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(KeyboardInterrupt):
                self._run("--loop", "--outbox-only")
        self.assertEqual(sleeps, [2])
        self.assertEqual(box.call_count, 1, "被打断前只跑了一轮")
        self.assertEqual(self.module.OUTBOX_LOOP_SLEEP_SECONDS, 2)

    def test_direct_assignments_run_while_ordinary_outbox_is_busy(self):
        self._member("direct-assignment")
        ordinary_started = Event()
        direct_finished = Event()
        direct_thread = []

        def ordinary_outbox(*args, **kwargs):
            ordinary_started.set()
            finished_while_syncing = direct_finished.wait(2)
            self.module._STOP.set()
            self.assertTrue(finished_while_syncing, "普通同步未结束时，直接指派也应该能处理")
            return jidanpu_member.OutboxStats()

        def direct_round(*, session, stop):
            if ordinary_started.wait(2):
                direct_thread.append(get_ident())
                direct_finished.set()

        with (
            mock.patch.object(self.module, "signal"),
            mock.patch.object(self.module, "_STOP", Event()),
            mock.patch.object(jidanpu_member, "run_outbox", side_effect=ordinary_outbox),
            mock.patch.object(self.module.jidanpu_direct_queue, "run_pending", side_effect=direct_round),
        ):
            _, _, errors = self._run("--loop", "--outbox-only")

        self.assertEqual(errors, "")
        self.assertTrue(direct_thread)
        self.assertNotIn(get_ident(), direct_thread)

    def test_direct_worker_reuses_sessions_until_outlet_changes_and_reports_blocked_origin(self):
        command = self.module.Command()
        stop = Event()
        pool = mock.Mock()
        binding = mock.Mock()
        primary_outlet = "http://primary.test.invalid:8001"
        backup_outlet = "http://backup.test.invalid:8002"
        current_outlet = [primary_outlet]
        blocked = self.module.jidanpu.UpstreamBlockedError()
        origins = []

        @contextmanager
        def pooled_session(_binding):
            client = mock.Mock(_tt_egress_outlet=current_outlet[0])
            yield client, mock.Mock()

        def run_round(*_args, session, **_kwargs):
            with session(binding) as (client, _http):
                origins.append(client._tt_egress_outlet)
            with self.assertRaises(self.module.jidanpu.UpstreamBlockedError), session(binding) as (client, _http):
                current_outlet[0] = backup_outlet
                raise blocked
            with session(binding) as (client, _http):
                origins.append(client._tt_egress_outlet)
            stop.set()

        pool.session.side_effect = pooled_session
        with (
            mock.patch.object(self.module.jidanpu, "UpstreamClientPool", return_value=pool),
            mock.patch.object(self.module.jidanpu_proxy, "proxy_url", side_effect=lambda: current_outlet[0]),
            mock.patch.object(self.module, "close_old_connections"),
            mock.patch.object(self.module, "connections"),
            mock.patch.object(
                self.module.jidanpu_proxy, "note_blocked", return_value=jidanpu_proxy.BLOCK_COUNTED,
            ) as note_blocked,
            mock.patch.object(command, "_run_direct_assignment_once", side_effect=run_round),
        ):
            command._loop_direct_assignment(None, stop)

        self.assertEqual(origins, [primary_outlet, backup_outlet])
        note_blocked.assert_called_once_with(primary_outlet, force=True)
        pool.mark_all_stale.assert_called_once_with()
        pool.invalidate.assert_not_called()


# ══ ⑤ `--callback-only`：只跑②式那一段（回调消费 + 兜底对账 + 出向发单）══════════


class CallbackOnlyTests(_SwitchTestCase):
    """`--callback-only` 只调②式那三个出口，⛔ 不碰①式四条路 / 上家线 / 模拟单。"""

    def test_callback_only_calls_only_the_openapi_segment(self):
        """两个绑定（①式 + ②式）⇒ 只有②式的三个出口各一次，其余一次都不调。

        失败方向：
        ① 把 `--callback-only` 实现成「还是跑整轮」⇒ ①式四条路被调 ⇒ 本用例红；
        ② 忘了按 `mode` 筛绑定 ⇒ ①式行也进 `_run_member_openapi_binding`（它会拿官方 API
           凭据去打一个账号密码式的绑定）⇒ 本用例红；
        ③ 顺手把上家线 / 模拟单也跑了 ⇒ 对应断言红。
        """
        plain = self._member("cb-plain")
        api = self._member_openapi("cb-api")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(jidanpu_upstream, "run_outbox") as upstream_box, \
             mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            _caught, out, _err = self._run("--once", "--callback-only")
        consume.assert_called_once_with(api, now=NOW)
        recon.assert_called_once_with(api, now=NOW)
        api_box.assert_called_once_with(api, now=NOW)
        box.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        upstream_box.assert_not_called()
        sim.assert_not_called()
        self.assertIn(f"代练员绑定 #{api.pk}（官方API）", out)
        self.assertNotIn(f"代练员绑定 #{plain.pk}", out, "①式绑定不归这个进程")

    def test_callback_only_skips_bindings_that_are_not_official_api(self):
        """只有①式绑定 ⇒ ②式那三个出口一次都不调（⛔ 不拿官方 API 凭据去打①式行）。

        失败方向：漏了 `is_openapi_binding` 那道筛 ⇒ ①式行也被当②式跑 ⇒ 本用例红
        （现场形态：账号密码为空 ⇒ 在建连处每轮抛 `ConfigError`）。
        """
        plain = self._member("cb-plain-only")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(hall_simulated, "run_tenant_round"):
            _caught, out, _err = self._run("--once", "--callback-only")
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        box.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        self.assertNotIn(f"代练员绑定 #{plain.pk}", out)

    def test_callback_only_keeps_going_when_one_binding_raises(self):
        """逐绑定异常边界：一个②式绑定抛，下一个照跑（同一轮里不中断）。

        失败方向：异常边界挪到整个租户外面（或根本没有）⇒ 第二个绑定不再被调 ⇒ 本用例红。
        ⚠️ `--once` 下「有未预期失败」仍按既有退出码契约抛 `CommandError`（边界管的是
        「别的绑定还跑不跑」，退出码管的是「这趟有没有出事」）。
        """
        broken = self._member_openapi("cb-broken")
        healthy = self._member_openapi("cb-healthy")
        seen: list[int] = []

        def _consume(binding, **kwargs):
            seen.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("合成炸了-callback-only")
            return mock.Mock(alert="", applied=0, skipped=0, text="")

        with self._patched_openapi_round() as (_consume_stub, _recon, _api_box), \
             mock.patch.object(
                 jidanpu_member_openapi, "consume_member_callbacks", side_effect=_consume,
             ), mock.patch.object(hall_simulated, "run_tenant_round"):
            _caught, _out, err = self._run(
                "--once", "--callback-only", expect_error=True
            )
        self.assertEqual(seen, [broken.pk, healthy.pk], "坏的那个不许挡住后面的绑定")
        self.assertIn(f"代练员绑定 #{broken.pk} 的回调消费", err)
        self.assertIn("RuntimeError", err)
        self.assertIn("继续下一个", err)


# ══ ⑥ `--loop --callback-only`：常驻形态的节奏 ═════════════════════════════════


class CallbackLoopTests(_SwitchTestCase):
    """`--loop --callback-only` 每轮之后睡**既有**那个循环间隔（`LOOP_SLEEP_SECONDS`）。"""

    def test_callback_loop_uses_the_sync_round_cadence(self):
        """第一轮跑完 ⇒ `wait(LOOP_SLEEP_SECONDS)`；等待时被打断 ⇒ 只跑了一轮。

        失败方向：忘了等待（紧循环打 DB / 打上游）⇒ `wait` 没被调 ⇒ 本用例红；
        或者另造一个比 3 秒更小的常数（任务书：按现有 loop 的节奏即可）⇒ 断言的字面值红。
        （⚠️ D2 起：等待 = `_STOP.wait`，可被信号打断；`signal` 桩掉防全局污染。）
        """
        self._member_openapi("cb-loop")
        sleeps: list[float] = []

        def _sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self._patched_round(), self._patched_openapi_round() as (consume, _r, _b), \
             mock.patch.object(self.module, "signal"), \
             mock.patch.object(self.module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(KeyboardInterrupt):
                self._run("--loop", "--callback-only")
        self.assertEqual(sleeps, [self.module.LOOP_SLEEP_SECONDS])
        self.assertEqual(consume.call_count, 1, "被打断前只跑了一轮")


# ══ ⑦ `--skip-callback`：原有一轮只跳过②式那一段 ═══════════════════════════════


class SkipCallbackTests(_SwitchTestCase):
    """`--skip-callback` 只跳过②式那一段；①式四条路、上家线、模拟单都照旧。"""

    def test_skip_callback_leaves_the_openapi_segment_to_the_callback_worker(self):
        """②式绑定：三个出口一次都不调，而且**不静默**（stdout 点名去哪个进程了）。

        失败方向：不筛 `skip_callback` ⇒ 主单元和回调单元**同时**消费同一批回调
        ⇒ `consume` 被调 ⇒ 本用例红；或者跳过了但一个字都不打 ⇒ stdout 断言红
        （运维看不出「这一段为什么没跑」，照 `_member_outbox_text(skipped=True)` 的同一条口径）。
        """
        api = self._member_openapi("sc-api")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box):
            _caught, out, _err = self._run("--once", "--skip-callback")
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        box.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        self.assertIn(f"代练员绑定 #{api.pk}（官方API）", out)
        self.assertIn("--skip-callback", out)
        self.assertIn("tuantuantong-jidanpu-callback", out)

    def test_skip_callback_keeps_the_plain_binding_round(self):
        """①式绑定的四段照旧各一次（含 outbox：`--skip-callback` ⛔ 不碰① outbox）。"""
        self._member("sc-plain")
        with self._patched_round() as (box, fast, pull, reconcile):
            self._run("--once", "--skip-callback")
        box.assert_called_once()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()

    def test_the_main_unit_switches_skip_outbox_and_callback_together(self):
        """主单元那一串（`--skip-outbox --skip-callback`）⇒ 只留①式快路/回拉/对账 + 上家线。

        判据：
        ① ①式 outbox 与②式三个出口**都不调**；
        ② ①式快路 / 回拉 / 对账各一次；
        ③ **上家线与模拟单仍被调**（`--skip-callback` 只跳过②式那一段 ⇒ 把上家线一起停了
           就是「拆一个、顺手关掉另一个」，那会让上家线静默停摆而无任何提示）；
        ④ ②式那行的 stdout 说清它去了哪个进程。
        """
        self._member("main-plain")
        api = self._member_openapi("main-api")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(
                 self.module.Command, "_run_upstream_bindings", return_value=False,
             ) as upstream_line, mock.patch.object(
                 hall_simulated, "run_tenant_round",
             ) as sim:
            _caught, out, _err = self._run(
                "--once", "--skip-outbox", "--skip-callback"
            )
        box.assert_not_called()
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()
        upstream_line.assert_called_once()
        sim.assert_called_once()
        self.assertIn(f"代练员绑定 #{api.pk}（官方API）", out)


# ══ ⑧ `--fastpath-only`：只跑①式身份自愈 + 日志快路 ══════════════════════════════


class FastpathOnlyTests(_SwitchTestCase):
    def test_fastpath_only_checks_identity_inside_each_log_poll(self):
        first = self._member("fp-only-a")
        second = self._member("fp-only-b")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net, \
             mock.patch.object(jidanpu_upstream, "run_outbox") as upstream_box, \
             mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            _caught, out, _err = self._run("--once", "--fastpath-only")
        heal.assert_not_called()
        self.assertEqual(fast.call_count, 2, "每个启用中的①式绑定各读一次日志")
        self.assertIn(mock.call(first, now=NOW, verify_identity=True), fast.call_args_list)
        self.assertIn(mock.call(second, now=NOW, verify_identity=True), fast.call_args_list)
        box.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        net.assert_not_called()
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        upstream_box.assert_not_called()
        sim.assert_not_called()
        self.assertIn("身份自愈 + 快路", out)

    def test_fastpath_only_skips_openapi_bindings(self):
        """②式绑定**不**进本进程（它的自愈 / 快路都要①式登录凭据，见②式那张表）。

        失败方向：漏了 `is_openapi_binding` 那道筛 ⇒ ②式行也去 heal / poll_log
        ⇒ 拿空账号在建连处抛 `ConfigError`（把「这条路没有这一段」报成「同步坏了」）⇒ 本用例红。
        """
        api = self._member_openapi("fp-api")
        with mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(jidanpu_member, "poll_log") as fast:
            _caught, out, _err = self._run("--once", "--fastpath-only")
        heal.assert_not_called()
        fast.assert_not_called()
        self.assertNotIn(f"代练员绑定 #{api.pk}", out)

    def test_fastpath_only_keeps_going_when_one_binding_raises(self):
        """逐绑定异常边界：一个①式绑定的快路抛，下一个照跑（同一轮里不中断）。

        失败方向：异常边界挪到整个租户外面（或根本没有）⇒ 第二个绑定不再被调 ⇒ 本用例红。
        ⚠️ `--once` 下「有未预期失败」仍按既有退出码契约抛 `CommandError`。
        """
        broken = self._member("fp-broken")
        healthy = self._member("fp-healthy")
        seen: list[int] = []

        def _poll(binding, **kwargs):
            seen.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("合成炸了-fastpath-only")
            return 0

        with mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(jidanpu_member, "poll_log", side_effect=_poll):
            _caught, _out, err = self._run(
                "--once", "--fastpath-only", expect_error=True
            )
        self.assertEqual(seen, [broken.pk, healthy.pk], "坏的那个不许挡住后面的绑定")
        self.assertIn(f"代练员绑定 #{broken.pk} 的快路", err)
        self.assertIn("RuntimeError", err)
        self.assertIn("继续下一个", err)


class FastpathLoopTests(_SwitchTestCase):
    def test_fastpath_loop_schedules_from_each_start_without_catching_up(self):
        self._member("fp-loop")
        self.addCleanup(self.module._STOP.clear)
        clock = 10.1
        starts = []
        sleeps = []
        durations = iter([0.5, 0.25, 7.25, 0.25])

        def _poll(*args, **kwargs):
            nonlocal clock
            starts.append(clock)
            clock += next(durations)
            if len(starts) == 4:
                self.module._STOP.set()
            return 0

        def _wait(seconds):
            nonlocal clock
            sleeps.append(seconds)
            clock += seconds

        with self._patched_round() as (_box, fast, _pull, _reconcile), \
             mock.patch.object(self.module, "signal"), \
             mock.patch.object(self.module, "time", monotonic=lambda: clock), \
             mock.patch.object(self.module._STOP, "wait", side_effect=_wait):
            fast.side_effect = _poll
            self._run("--loop", "--fastpath-only")
        self.assertEqual(starts, [10.1, 13.1, 16.1, 23.35])
        self.assertEqual([round(seconds, 2) for seconds in sleeps], [2.5, 2.75])


# ══ ⑨ `--skip-fastpath`：主一轮跳过 ⓪ 自愈 + ② 快路 ══════════════════════════════


class SkipFastpathTests(_SwitchTestCase):
    """`--skip-fastpath` 既不调 heal 也不调 `poll_log`；出向 / 回拉 / 对账照旧。"""

    def test_skip_fastpath_skips_heal_and_poll_log(self):
        """①式绑定：heal / 快路**一次都不调**，出向 / 回拉 / 对账各一次，且不静默。

        失败方向：
        ① 只跳过 `poll_log`、**heal 还留在主进程** ⇒ heal 被调 ⇒ 本用例红
           （那正是终审说的「重复自愈」）；
        ② 把整条 `_run_member_binding` 都跳掉 ⇒ 另外三段不被调 ⇒ 本用例红；
        ③ 跳过了但一个字都不打 ⇒ stdout 断言红（运维看不出这一段为什么没跑）。
        """
        self._member("sf-plain")
        with self._patched_round() as (box, fast, pull, reconcile), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal:
            _caught, out, _err = self._run("--once", "--skip-fastpath")
        heal.assert_not_called()
        fast.assert_not_called()
        box.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()
        self.assertIn("--skip-fastpath", out)
        self.assertIn("tuantuantong-jidanpu-fastpath", out)

    def test_skip_fastpath_leaves_the_openapi_line_untouched(self):
        """②式那一轮照旧跑（它本来就没有 heal / 快路这两段）。

        失败方向：把 `skip_fastpath` 顺手传进 `_run_member_openapi_binding`（或在它里面也判一次）
        ⇒ ②式的回调 / 对账 / 发单不被调 ⇒ 本用例红。
        """
        api = self._member_openapi("sf-api")
        with self._patched_round(), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal:
            self._run("--once", "--skip-fastpath")
        consume.assert_called_once_with(api, now=NOW)
        recon.assert_called_once()
        api_box.assert_called_once()
        heal.assert_not_called()


# ══ ⑩ `--reconcile-only`：只跑①式 ③ 状态回拉 + ④ 对账 ════════════════════════════


class ReconcileOnlyTests(_SwitchTestCase):
    """`--reconcile-only` 只调 `pull_statuses` + `reconcile_and_queue` + `claim_net`，
    其余一概不碰。"""

    def test_reconcile_only_calls_only_pull_and_reconcile(self):
        """两个①式绑定 ⇒ 回拉 / 对账 / 兜底网各两次；heal / 快路 / 出向 / ②式 / 上家线 / 模拟单全零。

        失败方向：
        ① 把它实现成「还是跑整轮」⇒ heal / 快路 / 出向被调 ⇒ 本用例红；
        ② 只跑回拉、漏掉对账或兜底网（或反过来）⇒ 本用例红——三段**必须同一个单元**；
        ③ 忘了按绑定遍历 ⇒ 各只被调一次 ⇒ 本用例红；
        ④ 顺手把上家线 / 模拟单也跑了 ⇒ 对应断言红。
        """
        first = self._member("rc-only-a")
        second = self._member("rc-only-b")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net, \
             mock.patch.object(
                 self.module.Command, "_run_upstream_bindings", return_value=False,
             ) as upstream_line, \
             mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            _caught, out, _err = self._run("--once", "--reconcile-only")
        self.assertEqual(pull.call_count, 2, "每个启用中的①式绑定各回拉一次")
        self.assertIn(mock.call(first, now=NOW), pull.call_args_list)
        self.assertIn(mock.call(second, now=NOW), pull.call_args_list)
        self.assertEqual(reconcile.call_count, 2, "每个启用中的①式绑定各排一次事件")
        self.assertIn(mock.call(first, now=NOW), reconcile.call_args_list)
        self.assertIn(mock.call(second, now=NOW), reconcile.call_args_list)
        self.assertEqual(net.call_count, 2, "每个启用中的①式绑定各兜一次底")
        self.assertIn(mock.call(first, now=NOW), net.call_args_list)
        self.assertIn(mock.call(second, now=NOW), net.call_args_list)
        heal.assert_not_called()
        box.assert_not_called()
        fast.assert_not_called()
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        upstream_line.assert_not_called()
        sim.assert_not_called()
        self.assertIn("回拉 + 对账", out)
        self.assertIn("兜底网", out)

    def test_reconcile_only_claims_net_waits_for_its_own_anchor(self):
        """⭐ 兜底网有自己的锚：未到点 ⇒ 它**一次都不调**，同轮到点的回拉 / 对账照跑。

        失败方向：拿对账的锚判兜底网（共用一根锚）⇒ 兜底网被调 ⇒ 本用例红；
        或兜底网没有闸（每拍都跑）⇒ 同样红。
        """
        binding = self._member("rc-net-gate")
        self.module._MEMBER_CLAIM_NET_AT[
            (TENANT_SCHEMA, binding.pk)
        ] = NOW - timedelta(seconds=1)   # 180 秒的间隔只过去 1 秒 ⇒ 未到点
        with self._patched_round() as (_box, _fast, pull, reconcile), \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net:
            self._run("--once", "--reconcile-only")
        net.assert_not_called()
        pull.assert_called_once()
        reconcile.assert_called_once()

    def test_reconcile_only_skips_openapi_bindings(self):
        """②式绑定**不**进本进程（它的回拉 / 对账早就按 mode 明确「不跑」，见②式那张表）。

        失败方向：漏了 `is_openapi_binding` 那道筛 ⇒ ②式行也去 `pull_statuses` /
        `reconcile_and_queue`（拿①式登录去打一个账号密码为空的绑定；而且①式那一份对账排的
        `member.*` 事件在②式下只会积压）⇒ 本用例红。
        """
        api = self._member_openapi("rc-api")
        with self._patched_round() as (_box, _fast, pull, reconcile), \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net:
            _caught, out, _err = self._run("--once", "--reconcile-only")
        pull.assert_not_called()
        reconcile.assert_not_called()
        net.assert_not_called()
        self.assertNotIn(f"代练员绑定 #{api.pk}", out)

    def test_reconcile_only_keeps_going_when_one_binding_raises(self):
        """逐绑定异常边界：一个绑定的回拉抛，同一条绑定的对账**照跑**、下一个绑定也照跑。

        失败方向：
        ① 把两段合成一个 try ⇒ 回拉一抛、对账也被跳过 ⇒ 本用例红
           （那会把「排事件」一起停掉，与主一轮既有口径相反）；
        ② 异常边界挪到整个租户外面（或根本没有）⇒ 第二个绑定不再被调 ⇒ 本用例红。
        ⚠️ `--once` 下「有未预期失败」仍按既有退出码契约抛 `CommandError`。
        """
        broken = self._member("rc-broken")
        healthy = self._member("rc-healthy")
        seen: list[int] = []

        def _pull(binding, **kwargs):
            seen.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("合成炸了-reconcile-only")
            return 0

        with self._patched_round() as (_box, _fast, _pull_stub, reconcile), \
             mock.patch.object(jidanpu_member, "pull_statuses", side_effect=_pull):
            _caught, _out, err = self._run(
                "--once", "--reconcile-only", expect_error=True
            )
        self.assertEqual(seen, [broken.pk, healthy.pk], "坏的那个不许挡住后面的绑定")
        self.assertEqual(reconcile.call_count, 2, "回拉失败不许把同一条绑定的对账也跳掉")
        self.assertIn(f"代练员绑定 #{broken.pk} 的回拉出现未预期失败", err)
        self.assertIn("RuntimeError", err)
        self.assertIn("继续下一个", err)


class ReconcileLoopTests(_SwitchTestCase):
    """`--loop --reconcile-only` 每轮之后睡**既有**那个循环间隔（`LOOP_SLEEP_SECONDS`）。"""

    def test_reconcile_loop_uses_the_sync_round_cadence(self):
        """第一轮跑完 ⇒ `wait(LOOP_SLEEP_SECONDS)`；等待时被打断 ⇒ 只跑了一轮。

        失败方向：忘了等待（紧循环打 DB / 打上游）⇒ `wait` 没被调 ⇒ 本用例红；
        或者另造一个常数 ⇒ 断言的字面值红（真正的节奏仍由每条绑定自己的
        `reconcile_seconds` 闸门决定：拍子比 180 秒更快只是空转，不改变动作频率）。
        （⚠️ D2 起：等待 = `_STOP.wait`，可被信号打断；`signal` 桩掉防全局污染。）
        """
        self._member("rc-loop")
        sleeps: list[float] = []

        def _sleep(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self._patched_round() as (_box, _fast, pull, _reconcile), \
             mock.patch.object(self.module, "signal"), \
             mock.patch.object(self.module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(KeyboardInterrupt):
                self._run("--loop", "--reconcile-only")
        self.assertEqual(sleeps, [self.module.LOOP_SLEEP_SECONDS])
        self.assertEqual(pull.call_count, 1, "被打断前只跑了一轮")


# ══ ⑪ `--skip-reconcile`：主一轮跳过 ③ 回拉 + ④ 对账 ══════════════════════════════


class SkipReconcileTests(_SwitchTestCase):
    """`--skip-reconcile` 既不调 `pull_statuses` 也不调 `reconcile_and_queue`、不调 `claim_net`；其余照旧。"""

    def test_skip_reconcile_skips_pull_and_reconcile(self):
        """①式绑定：回拉 / 对账 / 兜底网**一次都不调**，heal / 快路 / 出向各一次，且不静默。

        失败方向：
        ① 只跳对账、回拉还留在主进程（或兜底网还留在主进程）⇒ 对应断言红——三段同进同出；
        ② 把整条 `_run_member_binding` 都跳掉 ⇒ 另外三段不被调 ⇒ 本用例红；
        ③ 跳过了但一个字都不打 ⇒ stdout 断言红（运维看不出这一段为什么没跑）。
        """
        self._member("sr-plain")
        with self._patched_round() as (box, fast, pull, reconcile), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(jidanpu_member, "claim_net", return_value=0) as net:
            _caught, out, _err = self._run("--once", "--skip-reconcile")
        pull.assert_not_called()
        reconcile.assert_not_called()
        net.assert_not_called()
        heal.assert_called_once()
        box.assert_called_once()
        fast.assert_called_once()
        self.assertIn("--skip-reconcile", out)
        self.assertIn("兜底网跳过", out)
        self.assertIn("tuantuantong-jidanpu-reconcile", out)

    def test_skip_reconcile_leaves_the_openapi_line_untouched(self):
        """②式那一轮照旧跑（它的兜底对账是②式自己的段，⛔ 不归这对开关管）。

        失败方向：把 `skip_reconcile` 顺手传进 `_run_member_openapi_binding`（或在它里面
        也判一次）⇒ ②式的回调 / 兜底对账 / 发单不被调 ⇒ 本用例红
        （那会让②式那条线的兜底对账在开关打开之后**永远不跑**）。
        """
        api = self._member_openapi("sr-api")
        with self._patched_round() as (_box, _fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box):
            self._run("--once", "--skip-reconcile")
        consume.assert_called_once_with(api, now=NOW)
        recon.assert_called_once()
        api_box.assert_called_once()
        pull.assert_not_called()
        reconcile.assert_not_called()


# ══ ⑬ `--upstream-only`：只跑上家线**整行** ══════════════════════════════════════


class UpstreamOnlyTests(_SwitchTestCase):
    """`--upstream-only` 只跑上家绑定整行，⛔ 下家任何一段与模拟单都不碰。"""

    def test_upstream_only_calls_only_the_whole_upstream_row(self):
        """两个上家绑定 ⇒ outbox / 大厅 / 我的订单各两次；下家四条路与模拟单全零。

        失败方向：
        ① 把它实现成「还是跑整轮」⇒ 下家四条路被调 ⇒ 本用例红；
        ② 只跑上家的 outbox、漏掉两条读路（= 按子段拆）⇒ 大厅 / 我的订单零调用 ⇒ 本用例红
           ——终审口径是**整行**，健康合成与退避都是行级的；
        ③ 忘了按绑定遍历 ⇒ 各只被调一次 ⇒ 本用例红；
        ④ 顺手把模拟单也跑了 ⇒ `sim` 断言红。
        """
        first = self._upstream("uo-a")
        second = self._upstream("uo-b")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_openapi_round() as (consume, recon, api_box), \
             self._patched_upstream_row() as (up_box, up_hall, up_orders), \
             mock.patch.object(jidanpu_member, "heal_binding_identity") as heal, \
             mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            _caught, out, _err = self._run("--once", "--upstream-only")
        self.assertEqual(up_box.call_count, 2, "每个启用中的上家绑定各消费一次 outbox")
        # ⚠️ 只钉「哪个绑定」：outbox 的调用里还有池里的 `client` / `http` 两个 kwargs
        #    （那是整行共用的那一份实现，⛔ 不是本开关要钉的形状）。
        self.assertEqual(
            {call.args[0].pk for call in up_box.call_args_list},
            {first.pk, second.pk},
        )
        self.assertEqual(up_hall.call_count, 2, "整行含大厅（⛔ 不许按子段拆）")
        self.assertEqual(up_orders.call_count, 2, "整行含我的订单（⛔ 不许按子段拆）")
        box.assert_not_called()
        fast.assert_not_called()
        pull.assert_not_called()
        reconcile.assert_not_called()
        consume.assert_not_called()
        recon.assert_not_called()
        api_box.assert_not_called()
        heal.assert_not_called()
        sim.assert_not_called()
        self.assertIn("上家", out)

    def test_upstream_only_does_not_touch_the_simulated_round(self):
        """⛔ 模拟单轮不属上家线：即使到点也不许在本进程跑。

        失败方向：把 `--upstream-only` 实现成「跳过下家、但还走 `_run_tenant`」⇒
        `hall_simulated.run_tenant_round` 被调 ⇒ 本用例红。
        """
        with mock.patch.object(hall_simulated, "run_tenant_round") as sim:
            self._run("--once", "--upstream-only")
        sim.assert_not_called()


# ══ ⑭ `--skip-upstream`：主一轮跳过整行上家绑定 ══════════════════════════════════


class SkipUpstreamTests(_SwitchTestCase):
    """`--skip-upstream` 整行上家绑定一次都不调；下家与模拟单照旧。"""

    def test_skip_upstream_skips_the_whole_upstream_row(self):
        """①式绑定 + 上家绑定：下家四条路各一次，上家三段**一次都不调**，且不静默。

        失败方向：
        ① 只跳过 outbox、两条读路还留在主单元 ⇒ 大厅 / 我的订单被调 ⇒ 本用例红
           （那就是按子段拆，与「整行退避 / 行级健康合成」的口径冲突）；
        ② 把整轮都跳掉 ⇒ 下家四条路不被调 ⇒ 本用例红；
        ③ 跳过了但一个字都不打 ⇒ stdout 断言红（运维看不出上家线为什么没跑）。
        """
        self._member("su-plain")
        self._upstream("su-plain")
        with self._patched_round() as (box, fast, pull, reconcile), \
             self._patched_upstream_row() as (up_box, up_hall, up_orders), \
             mock.patch.object(hall_simulated, "run_tenant_round"):
            _caught, out, _err = self._run("--once", "--skip-upstream")
        box.assert_called_once()
        fast.assert_called_once()
        pull.assert_called_once()
        reconcile.assert_called_once()
        up_box.assert_not_called()
        up_hall.assert_not_called()
        up_orders.assert_not_called()
        self.assertIn("--skip-upstream", out)
        self.assertIn("tuantuantong-jidanpu-upstream", out)


# ══ ⑫ 六个单元的 `ExecStart` 必须互补（⛔ 防「静默全停」）══════════════════════════


_HAS_SYSTEMD_UNITS = (Path(__file__).resolve().parents[2] / "deploy" / "systemd").is_dir()


@unittest.skipUnless(_HAS_SYSTEMD_UNITS, "本仓库只开源网页前后端源码，不含部署脚本 deploy/systemd")
class UnitExecStartComplementTests(SimpleTestCase):
    """六个常驻单元（轮询 / 出向 / ②式回调 / ①式快路 / 回拉+对账 / 上家线）的 `ExecStart` **合起来**才是一份完整同步。

    ⚠️ 这条网抓的是**静默全停**：把两个单元都写成同一个开关（或主单元把五个 skip 都写上、
    新单元却没装），`activate.sh` 的 `is-active` 仍会过、巡检也看不出——但某一整段从此
    **没人跑**：出向事件停在台账里、②式回调没人消费、①式快路没人读（接手 / 退单回声停摆）、
    状态回拉与对账没人做（远端状态永远不收敛）、上家大厅 / 回拉 / 对账没人跑。
    所以这里断的是「**六个单元互补**」，⛔ 不是单看某一行。
    """

    _DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
    _UNITS = {
        "sync": "tuantuantong-jidanpu-sync.service",
        "outbox": "tuantuantong-jidanpu-outbox.service",
        "callback": "tuantuantong-jidanpu-callback.service",
        "fastpath": "tuantuantong-jidanpu-fastpath.service",
        "reconcile": "tuantuantong-jidanpu-reconcile.service",
        "upstream": "tuantuantong-jidanpu-upstream.service",
    }
    _PYTHON = "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python"

    @staticmethod
    def _exec_start(text: str) -> str:
        for line in text.splitlines():
            if line.startswith("ExecStart="):
                return line.split("=", 1)[1]
        raise AssertionError("单元里没有 ExecStart=")

    def _argv(self, key: str) -> list[str]:
        path = self._DEPLOY / self._UNITS[key]
        self.assertTrue(path.is_file(), f"缺单元文件：deploy/systemd/{path.name}")
        return shlex.split(self._exec_start(path.read_text(encoding="utf-8")))

    def test_each_unit_starts_the_sync_command_with_its_own_switch(self):
        """逐段钉完整 argv（多一个开关少一个开关都红）。"""
        expected = {
            "sync": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop",
                "--skip-outbox", "--skip-callback", "--skip-fastpath",
                "--skip-reconcile", "--skip-upstream",
            ],
            "outbox": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop", "--outbox-only",
            ],
            "callback": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop", "--callback-only",
            ],
            "fastpath": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop", "--fastpath-only",
            ],
            "reconcile": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop", "--reconcile-only",
            ],
            "upstream": [
                self._PYTHON, "manage.py", "jidanpu_sync", "--loop", "--upstream-only",
            ],
        }
        for key, argv in expected.items():
            with self.subTest(unit=self._UNITS[key]):
                self.assertEqual(self._argv(key), argv)

    def test_the_six_units_cover_every_segment_exactly_once(self):
        """五个「only」各恰好一个单元、五个「skip」各恰好一个单元，且不落在同一个单元上。

        失败方向（每一种都实测能让某一段静默停掉或重复消费）：
        ① 两个单元都 `--skip-outbox`（或都 `--outbox-only`）⇒ 出向 / 轮询有一边没人跑；
        ② 主单元漏 `--skip-callback` ⇒ 与回调单元**同时**消费同一批回调；
        ③ 主单元漏 `--skip-fastpath` ⇒ 与快路单元**同时**跑 heal（重复自愈）+ 双读日志；
        ④ 主单元漏 `--skip-reconcile` ⇒ 与对账单元**同时**回拉 / 排事件（同一张单排两次动作）；
        ⑤ 主单元漏 `--skip-upstream` ⇒ 与上家单元**同时**跑整行上家（重复打上家、健康合成 /
           整行退避各算一次）；
        ⑥ 回调 / 快路 / 对账 / 上家单元漏自己的 `--*-only` ⇒ 它变成「整轮再跑一遍」（重复消费 +
           重复发单 + 重复跑下家）；
        ⑦ 「only」与「skip」出现在同一个单元上 ⇒ 命令自己会响亮报错，单元当场 crash-loop。
        """
        argv = {key: self._argv(key) for key in self._UNITS}
        for flag in (
            "--outbox-only", "--skip-outbox", "--callback-only", "--skip-callback",
            "--fastpath-only", "--skip-fastpath",
            "--reconcile-only", "--skip-reconcile",
            "--upstream-only", "--skip-upstream",
        ):
            with self.subTest(flag=flag):
                self.assertEqual(
                    sum(flag in tokens for tokens in argv.values()), 1,
                    f"{flag} 必须恰好出现在一个单元的 ExecStart 里（实际：{argv}）",
                )
        for key, tokens in argv.items():
            with self.subTest(unit=self._UNITS[key]):
                self.assertFalse(
                    "--outbox-only" in tokens and "--skip-outbox" in tokens,
                    "同一个单元里既有 --outbox-only 又有 --skip-outbox ⇒ 命令会响亮报错（crash-loop）",
                )
                self.assertFalse(
                    "--callback-only" in tokens and "--skip-callback" in tokens,
                    "同一个单元里既有 --callback-only 又有 --skip-callback ⇒ 命令会响亮报错",
                )
                self.assertFalse(
                    "--fastpath-only" in tokens and "--skip-fastpath" in tokens,
                    "同一个单元里既有 --fastpath-only 又有 --skip-fastpath ⇒ 命令会响亮报错",
                )
                self.assertFalse(
                    "--reconcile-only" in tokens and "--skip-reconcile" in tokens,
                    "同一个单元里既有 --reconcile-only 又有 --skip-reconcile ⇒ 命令会响亮报错",
                )
                self.assertFalse(
                    "--upstream-only" in tokens and "--skip-upstream" in tokens,
                    "同一个单元里既有 --upstream-only 又有 --skip-upstream ⇒ 命令会响亮报错",
                )
        # 每一段归谁：出向在 outbox、②式在 callback、①式快路在 fastpath、
        # ①式回拉+对账在 reconcile、上家整行在 upstream，主单元五段都跳过。
        self.assertIn("--outbox-only", argv["outbox"])
        self.assertNotIn("--callback-only", argv["outbox"])
        self.assertNotIn("--skip-callback", argv["outbox"])
        self.assertNotIn("--fastpath-only", argv["outbox"])
        self.assertNotIn("--reconcile-only", argv["outbox"])
        self.assertIn("--callback-only", argv["callback"])
        self.assertNotIn("--outbox-only", argv["callback"])
        self.assertNotIn("--skip-outbox", argv["callback"])
        self.assertNotIn("--fastpath-only", argv["callback"])
        self.assertNotIn("--reconcile-only", argv["callback"])
        self.assertIn("--fastpath-only", argv["fastpath"])
        self.assertNotIn("--skip-fastpath", argv["fastpath"])
        self.assertNotIn("--outbox-only", argv["fastpath"])
        self.assertNotIn("--callback-only", argv["fastpath"])
        self.assertNotIn("--reconcile-only", argv["fastpath"])
        self.assertIn("--reconcile-only", argv["reconcile"])
        self.assertNotIn("--skip-reconcile", argv["reconcile"])
        self.assertNotIn("--outbox-only", argv["reconcile"])
        self.assertNotIn("--callback-only", argv["reconcile"])
        self.assertNotIn("--fastpath-only", argv["reconcile"])
        self.assertNotIn("--upstream-only", argv["reconcile"])
        self.assertIn("--upstream-only", argv["upstream"])
        self.assertNotIn("--skip-upstream", argv["upstream"])
        self.assertNotIn("--outbox-only", argv["upstream"])
        self.assertNotIn("--callback-only", argv["upstream"])
        self.assertNotIn("--fastpath-only", argv["upstream"])
        self.assertNotIn("--reconcile-only", argv["upstream"])
        self.assertIn("--skip-outbox", argv["sync"])
        self.assertIn("--skip-callback", argv["sync"])
        self.assertIn("--skip-fastpath", argv["sync"])
        self.assertIn("--skip-reconcile", argv["sync"])
        self.assertIn("--skip-upstream", argv["sync"])

    def test_the_split_units_always_restart_without_rate_limit(self):
        """拆出来的单元：死了必须被拉起来 + 关掉启动限速 + cwd 是当前 release 的 src。"""
        for key in ("outbox", "callback", "fastpath", "reconcile", "upstream"):
            text = (self._DEPLOY / self._UNITS[key]).read_text(encoding="utf-8")
            with self.subTest(unit=self._UNITS[key]):
                self.assertIn("\nRestart=always\n", text)
                self.assertIn("\nStartLimitIntervalSec=0\n", text)
                self.assertIn(
                    "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src", text
                )
                self.assertNotIn("OnCalendar=", text)
                self.assertIn("WantedBy=multi-user.target", text)

    def test_the_callback_unit_keeps_the_outbound_dispatch_in_the_same_process(self):
        """②式的出向发单**必须**留在回调单元里（它依赖同轮先消费回调补远端单号）。

        ⚠️ 这条断的是**单元注释**（证明力有限）：保证「为什么不能把发单拆走」这句话还在，
        ⛔ 不证明有人读过它。失败方向：把这句约束删掉/改口 ⇒ 下一个人很可能顺手把发单挪到
        `--outbox-only` 那个单元（= 拿空号去办）。
        """
        text = (self._DEPLOY / self._UNITS["callback"]).read_text(encoding="utf-8")
        for phrase in ("出向发单", "顺序", "只在一台"):
            self.assertIn(phrase, text, f"回调单元注释里缺少承重约束：{phrase}")

    def test_the_reconcile_unit_keeps_both_segments_in_the_same_process(self):
        """状态回拉与对账**必须**同单元、共用同一个 `reconcile_seconds` 闸门。

        ⚠️ 同样断的是**单元注释**（证明力有限）：失败方向是把这两段拆成两台，或者把闸门
        各算一次——那会让「同一段慢路」被两个进程各跑一遍（重复回拉 + 重复排事件）。
        """
        text = (self._DEPLOY / self._UNITS["reconcile"]).read_text(encoding="utf-8")
        for phrase in ("状态回拉", "对账", "reconcile_seconds", "只在一台"):
            self.assertIn(phrase, text, f"对账单元注释里缺少承重约束：{phrase}")

    def test_the_upstream_unit_keeps_the_whole_row_in_the_same_process(self):
        """上家线**只能整行拆**，⛔ 不许按「大厅 / 回拉 / 对账」子段再拆（终审改准）。

        ⚠️ 这条断的是**单元注释**（证明力有限）：保证「为什么不能再拆」这句话还在，
        ⛔ 不证明有人读过它。失败方向：把这句话删掉/改口 ⇒ 下一个人很可能顺手把上家线的
        大厅 / 对账拆成两台（各自算一次**行级**的健康合成与整行退避 ⇒ 判据互相打架）。
        """
        text = (self._DEPLOY / self._UNITS["upstream"]).read_text(encoding="utf-8")
        for phrase in ("整行", "不可再拆", "健康", "退避", "只在一台"):
            self.assertIn(phrase, text, f"上家单元注释里缺少承重约束：{phrase}")


# ══ ⑥ 空闲 tick ⛔ 不许清共享出口计数════════════════════════


class IdleTickKeepsSharedEgressCounterTests(_SwitchTestCase):
    """独立单元的空闲轮**不许**复位共享状态里的「连续拦截计数」。

    ## 为什么（B1）

    三个 `--*-only` 入口原先在「这一段没抛异常」时就无条件 `note_ok()`，而它们手里
    明明有 `fast_due` / `status_pull_due` / `reconcile_due` 三个「到点了吗」的返回值
    （`_run_member_fastpath` / `_run_member_reconcile` 的返回点）却没用。后果：

    - 共享状态（`JIDANPU_EGRESS_STATE_FILE`）模式下，**空闲轮每 3 秒**把别的单元
      攒下的「连续拦截计数」清零 ⇒ 上家永远攒不到 3 次 ⇒ **自动换出口失效**；
    - `--outbox-only` 即使台账是空的，`run_outbox` 也会回一份 `done=0` 的计数，
      同样被当成「成功一次」。

    三支各一条用例：先攒 2 次拦截 → 跑一次**没到点 / 没事件**的 wrapper →
    第 3 次拦截必须**真的切换**（`BLOCK_SWITCHED`）。改前它会回落成 `BLOCK_COUNTED`
    （计数已被空闲轮清零），改后才切换。
    """

    PROXIES = (
        "http://egress-a.test.invalid:1,"
        "http://egress-b.test.invalid:2"
    )

    def _shared_egress_state(self) -> Path:
        """把出口池切到「共享状态文件」模式（B1 的失效只在共享模式下可观测）。"""
        directory = Path(tempfile.mkdtemp(prefix="egress-state-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        state = directory / "egress-state.json"
        patcher = mock.patch.dict(
            os.environ,
            {
                "JIDANPU_PROXIES": self.PROXIES,
                "JIDANPU_EGRESS_STATE_FILE": str(state),
                # 显式清空 ⇒ 用默认阈值 3 / 隔离 900 秒（⛔ 不受本机 .env 影响）。
                "JIDANPU_EGRESS_FAIL_THRESHOLD": "",
                "JIDANPU_EGRESS_QUARANTINE_SECONDS": "",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        jidanpu_proxy.reset_counter_for_tests()
        self.addCleanup(jidanpu_proxy.reset_counter_for_tests)
        return state

    def _block_twice(self):
        """在第一个出口上攒 2 次拦截（阈值 3 ⇒ 还差一次才切）。"""
        self.assertEqual(
            jidanpu_proxy.note_blocked(), jidanpu_proxy.BLOCK_COUNTED
        )
        self.assertEqual(
            jidanpu_proxy.note_blocked(), jidanpu_proxy.BLOCK_COUNTED
        )

    def _assert_third_block_switches(self, stage: str):
        """第 3 次拦截必须切换出口（改前：计数被空闲轮清零 ⇒ 只回 `counted`）。"""
        self.assertEqual(
            jidanpu_proxy.note_blocked(),
            jidanpu_proxy.BLOCK_SWITCHED,
            f"{stage} 的空闲轮把共享的连续拦截计数清零了 ⇒ 第 3 次拦截没有换出口"
            "（共享状态模式下自动换出口因此失效）",
        )

    def test_an_idle_outbox_tick_keeps_the_shared_counter(self):
        """`--outbox-only`：台账空（`done=0`）的一轮 ⛔ 不算「成功一次」。"""
        self._shared_egress_state()
        self._member("idle-outbox")
        self._block_twice()
        with mock.patch.object(
            jidanpu_member, "run_outbox",
            return_value=jidanpu_member.OutboxStats(),
        ) as box:
            self._run("--once", "--outbox-only")
        box.assert_called_once()
        self._assert_third_block_switches("outbox")

    def test_an_idle_fastpath_tick_keeps_the_shared_counter(self):
        """`--fastpath-only`：快路**没到点**（一次上游都没打）的一轮 ⛔ 不许复位。"""
        self._shared_egress_state()
        binding = self._member("idle-fastpath")
        self.module._MEMBER_FAST_AT[(TENANT_SCHEMA, binding.pk)] = NOW
        self._block_twice()
        with mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "poll_log", return_value=0,
             ) as fast:
            self._run("--once", "--fastpath-only")
        fast.assert_not_called()
        self._assert_third_block_switches("fastpath")

    def test_an_idle_reconcile_tick_keeps_the_shared_counter(self):
        """`--reconcile-only`：回拉与对账**都没到点**的一轮 ⛔ 不许复位。"""
        self._shared_egress_state()
        binding = self._member("idle-reconcile")
        key = (TENANT_SCHEMA, binding.pk)
        self.module._MEMBER_STATUS_PULL_AT[key] = NOW
        self.module._MEMBER_RECONCILE_AT[key] = NOW
        self._block_twice()
        with mock.patch.object(
            jidanpu_member, "pull_statuses", return_value=0,
        ) as pull, mock.patch.object(
            jidanpu_member, "reconcile_and_queue",
            return_value=jidanpu_member.QueueStats(),
        ) as reconcile:
            self._run("--once", "--reconcile-only")
        pull.assert_not_called()
        reconcile.assert_not_called()
        self._assert_third_block_switches("reconcile")


class MemberOutboxSuspendedTextTests(SimpleTestCase):
    """统计行「/熔断挂起 N」的渲染（D-0359 D1）：非零才拼进「outbox 完成/待重试/跳过」
    那一行——挂起期不打逐轮日志（⛔ 刷屏），这一格是稳态可见性（评审 S4 补）。"""

    def _text(self, **stats):
        from apps.business.management.commands.jidanpu_sync import Command
        from apps.business.jidanpu_member import OutboxStats
        base = dict(done=0, failed=0, deferred=0, skipped=0, capped=0,
                    suspended=0, abandoned=0)
        base.update(stats)
        return Command()._member_outbox_text(OutboxStats(**base))

    def test_suspended_count_is_appended_when_nonzero(self):
        text = self._text(done=5, failed=1, suspended=3)
        self.assertIn("/熔断挂起 3", text)
        self.assertIn("outbox 完成 5/待重试 1", text)

    def test_zero_suspended_adds_nothing(self):
        text = self._text(done=2)
        self.assertNotIn("熔断挂起", text)
