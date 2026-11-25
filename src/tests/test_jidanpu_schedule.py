"""调度与发版：`jidanpu_sync --loop` 的 systemd 单元 + 发版脚本接线。

## 这个文件守什么（为什么值得单独一张网）

业务方要「**10 秒级**」轮询（原话：「一分钟的粒度不够 10 秒以内比较合适」），而 cron 最细只有
1 分钟 ⇒ 生产调度是**常驻 `--loop`**，由 `deploy/systemd/` 下的 systemd 单元拉起。这一层
**没有任何业务用例能覆盖**（它不进 HTTP、不进管理命令的参数解析），而它坏了是**静默**的：
单元里开关写错 / 忘了 `Restart=always` / 发版脚本没装它 ⇒ 同步悄悄停摆，页面上一个字都不报。
所以这里按「**读真文件 + 钉字面值 + 拿真解析器验**」守三件事：

1. 单元里的 `ExecStart` 必须是**那条真命令**（真解析器认 `--loop`；`--once --loop` 仍被拒）；
2. `Restart=always`、关掉 systemd 的启动限速、⛔ 不叠 timer / cron；
3. 激活和回滚仍安装、重启常驻单元。发布传输由 `test_deploy_sync_inventory` 验证。

4. **调度侧与出口池的接线**（出口池线）——
`jidanpu_sync` 两处切池判据（下家 `:509` / 上家 `:895` 附近）从「只认 `AuthError`」扩成
「同时认 `UpstreamBlockedError`（对端 468 / WAF 拦截）」，拦截会累计到出口池、换出口时留一条
`logger.warning`、逐绑定那一行带 `outlet=<下标>:<host>`、每轮成功后复位连续计数。
⚠️ 判据打**真实出口**：池替身的 `invalidate` 调用集合、`jidanpu_proxy.current_label()` 的读数、
stdout 那一行 —— ⛔ 不读进程内私有字典。

⚠️ 每条断言都钉**字面值或真实出口**（E-0024）：⛔ 不与 shell 变量 / 模块常量自比。
⚠️ **如实边界**：跨机器的「只有一份」在本文件里只能钉到**单元注释与脚本**——真正的唯一性靠
   运维纪律（多台应用机时只在一台上 `systemctl enable`）。本文件**不声称**能测出「两台机器同时跑」。
"""
from __future__ import annotations

import io
import os
import shlex
import signal
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business import (
    hall_simulated,
    jidanpu,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_openapi_consume,
    jidanpu_proxy,
    jidanpu_upstream,
)
from apps.business.models import (
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
)
from jidanpu.errors import ApiError, AuthError

#: `src/tests/` 往上两级 = 仓库根；单元与部署脚本都在仓库根的 `deploy/` 下。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"
_UNIT = _DEPLOY / "systemd" / "tuantuantong-jidanpu-sync.service"
_UNIT_BASENAME = "tuantuantong-jidanpu-sync.service"


class _LoopStopped(Exception):
    """哨兵：从 `--loop` 的死循环里退出（⛔ 挂死算 `BROKEN`，见 E-0064 / E-0075）。"""


@contextmanager
def _noop_context(tenant):
    """把 `tenant_context` 换成「什么都不做」（只测接线逻辑，不切库）。"""
    yield


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exec_start(text: str) -> str:
    """单元里的 `ExecStart=` 值（支持 systemd 的 `\\` 续行）。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("ExecStart="):
            continue
        value = line.split("=", 1)[1]
        while value.endswith("\\"):
            index += 1
            value = value[:-1] + lines[index].strip()
        return value
    raise AssertionError("单元里没有 ExecStart=")


class UnitFileContractTests(SimpleTestCase):
    """systemd 单元本身：起的是哪条命令、崩了怎么办、有没有叠第二种调度。"""

    def test_the_unit_starts_the_loop_with_the_release_venv_python(self):
        """`ExecStart` 逐段钉住：venv python + `manage.py jidanpu_sync --loop --skip-outbox --skip-callback --skip-fastpath`。

        ⚠️ `ExecStart` 的各段 `--skip-*` 与拆出去的独立单元一一对应（仍是**逐段完整字面值**，
        多一个开关少一个开关都红）：出向事件消费 ⇒ `tuantuantong-jidanpu-outbox`（只做日志快路 /
        状态回拉 / 对账）；②式（官方 API）回调消费 ⇒ `tuantuantong-jidanpu-callback`；①式快路
        （含身份自愈，⛔ 留在本单元会与快路进程重复自愈）⇒ `tuantuantong-jidanpu-fastpath`；
        ①式状态回拉 + 对账（两段共用一个闸门，⛔ 只跳一段会让另一段每轮空转）⇒
        `tuantuantong-jidanpu-reconcile`；上家线（`JidanpuUpstreamBinding`）**整行**（⛔ 只能整行拆：
        行级健康合成与整行退避，子段拆开会让每台各算一次）⇒ `tuantuantong-jidanpu-upstream`
        ⇒ 主单元只剩**租户级模拟单轮**（`hall_simulated.run_tenant_round`）。

        失败方向：把开关改成 `--once`（就退回「一分钟粒度」，且与常驻互斥）、把 venv 路径写错、
        顺手加一个 `--tenant`（那会让它只服务一个租户）、**漏掉 `--skip-outbox`**
        （那会让它在轮询之外**又消费一遍出向台账**）、**漏掉 `--skip-callback`**
        （那会与回调单元抢同一批回调）、**漏掉 `--skip-fastpath`**
        （那会与快路单元重复跑，且**身份自愈跑两遍**）、**漏掉 `--skip-reconcile`**
        （那会与对账单元重复回拉 / 重复排事件）、或者**漏掉 `--skip-upstream`**
        （那会与上家单元**同时**跑整行上家：重复打上家、行级健康 / 退避各算一次）⇒ 本用例红。
        """
        tokens = shlex.split(_exec_start(_read(_UNIT)))
        self.assertEqual(
            tokens,
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--skip-outbox",
                "--skip-callback",
                "--skip-fastpath",
                "--skip-reconcile",
                "--skip-upstream",
            ],
        )

    def test_the_unit_working_directory_is_the_current_release_src(self):
        """cwd 必须是 `current/src`：单元靠它解析 `manage.py`，也靠它让新 release 生效。"""
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            _read(_UNIT),
        )

    def test_the_unit_always_restarts_and_is_not_rate_limited_to_death(self):
        """`Restart=always` + **关掉启动限速**：轮询进程死了必须被拉起来。

        ⚠️ systemd 默认 `StartLimitIntervalSec=10s` / `StartLimitBurst=5`：10 秒内崩 5 次就
        **永久放弃**该单元（应用单元上挂着同一条残差）⇒ 同步会**静默停摆**。
        失败方向：删掉 `StartLimitIntervalSec=0` ⇒ 本用例红。
        """
        text = _read(_UNIT)
        self.assertIn("\nRestart=always\n", text)
        self.assertIn("\nStartLimitIntervalSec=0\n", text)

    def test_the_unit_is_not_a_timer(self):
        """⛔ 不叠第二种调度：单元不是 `.timer`（那会与常驻并存 ⇒ 可能并发）。"""
        text = _read(_UNIT)
        self.assertNotIn("OnCalendar=", text)
        self.assertNotIn("WantedBy=timers.target", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_the_load_bearing_constraints_are_written_in_the_unit_comments(self):
        """任务书要求把承重约束**写进单元文件注释**（运维装单元时第一眼就看到它们）。

        ⚠️ 这条断的是**文字**、证明力有限：它只保证「这几句话还在」，不保证运维照做。
        失败方向：把某条约束的注释删掉 ⇒ 本用例红（正是要让它别被顺手删）。
        """
        text = _read(_UNIT)
        for phrase in (
            "只在一台",                 # 单实例（⛔ 两台 = 并发同步同一租户）
            "并发同步同一租户",          # 说清为什么单实例是承重的
            "不要再叠 cron",            # 与 --once 的 cron 会真并发
            "Restart=always",          # 死了靠 systemd 拉起来
            "last_ok_at",              # 告警判据：多久没成功同步过
        ):
            self.assertIn(phrase, text, f"单元注释里缺少承重约束：{phrase}")


class CommandSignatureTests(TenantTestCase):
    """单元里引用的那条命令，必须**真的**是管理命令认的签名（⛔ 不只 grep 自己写的文件）。"""

    def test_the_command_parses_the_loop_flag_the_unit_uses(self):
        """拿真解析器跑一遍：`--loop` 存在、且进的是常驻循环（哨兵退出）。

        失败方向：把 `--loop` 从 `add_arguments` 删掉（单元里那条命令当场 `CommandError`）
        ⇒ 本用例红。
        ⚠️ 循环睡眠从 `time.sleep` 换成 `_STOP.wait` ⇒ 桩跟着挪；
        `signal` 一并桩掉（测试进程里装真 handler 会污染全局状态）。
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
                call_command("jidanpu_sync", "--loop")
        self.assertEqual(rounds, [None])

    def test_the_once_and_loop_flags_stay_mutually_exclusive(self):
        """`--once` 与 `--loop` 同给仍**响亮报错**（契约不许改）⇒ 单元里出现 `--once` 就是坏的。

        ⚠️ 哨兵同时盯着：守卫若被删，命令会进常驻循环并撞哨兵（而不是挂死）⇒ 判 `FAIL` 而不是 `BROKEN`。
        """
        from apps.business.management.commands import jidanpu_sync as module

        def _sleep(seconds):
            raise _LoopStopped

        with mock.patch.object(module.Command, "_run_once", return_value=False), \
             mock.patch.object(module, "signal"), \
             mock.patch.object(module._STOP, "wait", side_effect=_sleep):
            with self.assertRaises(CommandError):
                call_command("jidanpu_sync", "--loop", "--once")


class GracefulShutdownTests(TenantTestCase):
    """⭐ `--loop` 的优雅停机。

    修的现场：发版时 systemd 发 `SIGTERM` ⇒ 旧代码裸 `while True + time.sleep` 不认它
    ⇒ `TimeoutStopSec=30` 一到就是 `SIGKILL`，在途上游写请求被拦腰打断（事件状态只在成功后
    落库 ⇒ 「上游已收到、我方没记账」⇒ 重发非幂等写）。
    ⚠️ 结构照 `tests/test_peer_worker.py` 的 C9 停机用例同型：`signal` 桩掉（测试进程里装真
    handler 会污染全局状态）、`_STOP` 整体替换隔离状态、判据钉**桩的调用集合**与 stdout。
    """

    TENANT_SCHEMA = "tenant-stop"

    #: 固定的「现在」：退避窗口与闸门判据都按它算（⛔ 不用墙钟让断言随运行时刻漂）。
    NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt_timezone.utc)

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        for name in ("_UPSTREAM_FAILURE_COUNT", "_UPSTREAM_PENALTY_UNTIL"):
            mapping = getattr(module, name, {})
            mapping.clear()
            self.addCleanup(mapping.clear)
        self.tenant = mock.Mock(schema_name=self.TENANT_SCHEMA)

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _member(self, name):
        return JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(username=f"stop-{name}", password="pw-canary-x"),
            jidanpu_base_url="https://jidanpu-stop.test.invalid",
            account=f"acc-stop-{name}",
            account_name="传令兵乙",
            password="pw-canary-stop",
            enabled=True,
        )

    @contextmanager
    def _patched_enumeration(self):
        """租户枚举与切库的替身 + 注入时钟（照 member/sync 两个测试文件的形状）。"""
        with mock.patch.object(self.module, "tenant_context", _noop_context), \
             mock.patch.object(self.module.timezone, "now", return_value=self.NOW):
            patcher = mock.patch.object(self.module.Tenant, "objects")
            objects = patcher.start()
            objects.exclude.return_value.order_by.return_value = [self.tenant]
            try:
                yield
            finally:
                patcher.stop()

    # ── 用例 ────────────────────────────────────────────────────────────────

    def test_the_loop_registers_term_and_int_handlers(self):
        """`--loop` 注册 SIGTERM / SIGINT → 停止位。判据钉**接线**（哪两个信号、接哪个函数）。"""
        from apps.business.management.commands import jidanpu_sync as module

        registered = []

        def _fake_signal(signum, handler):
            registered.append((signum, handler))

        with mock.patch.object(module, "signal") as fake_signal:
            # ⛔ 假 signal 模块也要给**真常量**：否则 handle 里读到的 SIGTERM 是个
            #    MagicMock，判据就在和影子比对（E-0024 同族）。
            fake_signal.SIGTERM = signal.SIGTERM
            fake_signal.SIGINT = signal.SIGINT
            fake_signal.signal.side_effect = _fake_signal
            with mock.patch.object(module, "_STOP", new=threading.Event()):
                with mock.patch.object(module.Command, "_run_once", return_value=False):
                    with mock.patch.object(
                        module._STOP, "wait", side_effect=_LoopStopped
                    ):
                        with self.assertRaises(_LoopStopped):
                            call_command("jidanpu_sync", "--loop")
        self.assertEqual(
            registered,
            [
                (signal.SIGTERM, module._request_stop),
                (signal.SIGINT, module._request_stop),
            ],
        )

    def test_request_stop_only_sets_the_flag(self):
        """信号处理器只置位、不抛不重活（信号上下文不可重入——它是被 ``signal`` 调的）。"""
        from apps.business.management.commands import jidanpu_sync as module

        stop = threading.Event()
        with mock.patch.object(module, "_STOP", new=stop):
            module._request_stop(signal.SIGTERM, None)
        self.assertTrue(stop.is_set())

    def test_a_signal_during_the_penalty_wait_exits_after_that_round(self):
        """⭐ 验收用例：「退避等待中 SIGTERM ⇒ 当轮退出」。

        场景：一行下家绑定正处在**行级退避**窗口里（`--loop` 每一拍对它只做「跳过」判定），
        信号在当轮到达 ⇒ 那一轮**跑完（= 跳过它）就退**，等待走 `Event.wait`——
        ⛔ 不陪退避睡满间隔、也⛔ 不进第二轮。
        判据三件事：① stdout 有「整行跳过」（证明当轮确实处在退避形态，⛔ 不是空跑的假阳性）；
        ② 退避中的那行**一次上游都不打**（`poll_log` 桩响亮失败兜底）；③ 睡眠走 `Event.wait`
        且带的是循环间隔（信号一到立刻醒，退出延迟毫秒级）。
        """
        from apps.business.management.commands import jidanpu_sync as module

        binding = self._member("penalty-stop")
        key = (self.TENANT_SCHEMA, binding.pk)
        self.module._UPSTREAM_FAILURE_COUNT[key] = 2
        self.module._UPSTREAM_PENALTY_UNTIL[key] = self.NOW + timedelta(seconds=120)

        stop = threading.Event()
        waits: list = []
        real_wait = stop.wait

        def recording_wait(timeout=None):
            waits.append(timeout)
            stop.set()                      # ⭐ 信号在「退避后的那一拍等待」里到达
            return real_wait(timeout)

        out = io.StringIO()
        with mock.patch.object(module, "signal"), \
             mock.patch.object(module, "_STOP", new=stop), \
             mock.patch.object(module._STOP, "wait", recording_wait), \
             mock.patch.object(
                 jidanpu_member, "poll_log",
                 side_effect=AssertionError("退避中的行不许打上游"),
             ), mock.patch.object(hall_simulated, "run_tenant_round"):
            with self._patched_enumeration():
                call_command("jidanpu_sync", "--loop", stdout=out)
        self.assertIn("整行跳过", out.getvalue())
        self.assertEqual(waits, [module.LOOP_SLEEP_SECONDS])


class DeployWiringTests(SimpleTestCase):
    """发版流程真的把调度装上（业务方：「**部署的时候一并挂上**」）。"""

    def test_activate_installs_enables_and_restarts_the_sync_unit(self):
        """激活一个 release 之后要**装 / 开机自启 / 重启**同步单元（切了软链不重启 = 跑旧代码）。

        失败方向：任一步删掉 ⇒ 本用例红；`--once` 的 cron 不在本脚本里（另有负断言）。
        ⚠️ `activate.sh` 是「单元清单 `SYNC_SERVICES=(…)` + 逐单元循环」（上下游同步也要
        随发版装）⇒ 本用例钉**清单里逐项仍会被装**：对清单里**每个**单元都断言同一个字面循环体
        （`install -m 644 "$SYNC_UNIT_SRC"` / `enable` / `restart`）。
        ⛔ 三个**字面值**仍然逐字在脚本里（下面第 3 条），⛔ 不是"文件里出现过某个词"那种判据。
        """
        text = _read(_DEPLOY / "activate.sh")
        for phrase in (
            'install -m 644 "$SYNC_UNIT_SRC"',
            "systemctl daemon-reload",
            'systemctl enable "$SYNC_SERVICE"',
            'systemctl restart "$SYNC_SERVICE"',
            # ⚠️ 缺单元就响亮失败的判据仍在（泛化后对清单里**每个**单元都成立，见下面那条）。
            # ⭐ E-0263-N4：脚本里是 `${SYNC_UNIT_SRC}（`（bash 3.2 多字节吞变量名同族清扫）
            # ——钉值随脚本现状取，判据本身一个字没放宽。
            "缺少 ${SYNC_UNIT_SRC}",
        ):
            self.assertIn(phrase, text, f"activate.sh 缺少：{phrase}")

    def test_activate_installs_every_unit_in_the_list(self):
        """清单里**每一个**单元都要真的被逐个安装（⛔ 不是只装清单里第一条）。

        失败方向：把循环换成"只处理第一个"、或忘了在循环里重算 `$R/shared/<单元名>.service`
        ⇒ 第二个及以后的同步线**从不安装**（`deploy.sh` 刷过去了、机器上却没装）。
        """
        text = _read(_DEPLOY / "activate.sh")
        self.assertIn('for SYNC_SERVICE in "${SYNC_SERVICES[@]}"; do', text)
        self.assertIn('SYNC_UNIT_SRC="$R/shared/${SYNC_SERVICE}.service"', text)

    def test_activate_skips_loudly_when_there_is_no_systemd(self):
        """没有 systemd 的机器（开发机 / 容器）**可跳过、但不许静默**。

        失败方向：删掉这道守卫（或把提示改成无声 `return`）⇒ 本用例红。
        ⚠️ 断言的**外部契约**是「响亮跳过 + 按**清单**点名本机不跑哪些同步
        （`${SYNC_SERVICES[*]}`）」，⛔ 不是某一条线专属的措辞。
        """
        text = _read(_DEPLOY / "activate.sh")
        self.assertIn("command -v systemctl", text)
        self.assertIn("本机没有 systemctl", text)
        self.assertIn("这些同步**都不会**在这台机器上跑", text)
        # ⛔ 点名的必须是**清单里的每一个**（`${SYNC_SERVICES[*]}`），⛔ 不是写死某一条线。
        self.assertIn("未安装 / 未重启 ${SYNC_SERVICES[*]}", text)

    def test_rollback_also_restarts_the_sync_unit(self):
        """回滚只切软链不重启常驻进程 ⇒ 应用回滚了、同步还在跑新代码（半新半旧）。

        失败方向：把 `rollback.sh` 里那次 restart 删掉 ⇒ 本用例红。
        清单覆盖由 `test_deploy_sync_inventory` 通过实际发布产物验证。
        """
        text = _read(_DEPLOY / "rollback.sh")
        self.assertIn('for SYNC_SERVICE in "${SYNC_SERVICES[@]}"; do', text)
        self.assertIn('systemctl restart "$SYNC_SERVICE"', text)

    def test_no_deploy_script_installs_a_cron_entry(self):
        """⛔ 不许给 `--once` 装 cron：与常驻会**真并发**（同一租户被同步两次、抢单与撤单互踩）。

        失败方向：在任何部署脚本里写入 `crontab` ⇒ 本用例红。
        """
        for name in ("deploy.sh", "activate.sh", "rollback.sh", "bootstrap.sh"):
            self.assertNotIn(
                "crontab", _read(_DEPLOY / name), f"{name} 里出现了 crontab（与常驻并发）"
            )


# ── ④ 出口池接线（出口池线 T3）────────────────────────────────────

#: 固定的「现在」：这几条判据不看闸门，只把它转手给各段。
_NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt_timezone.utc)


@contextmanager
def _egress_env(proxies):
    """临时设定出口相关的环境键：**空串 = 这台机器没配** ⇒ 被测代码回落到默认阈值 / 隔离时长。"""
    with mock.patch.dict(
        os.environ,
        {
            "JIDANPU_PROXIES": proxies,
            "JIDANPU_PROXY": "",
            "JIDANPU_EGRESS_FAIL_THRESHOLD": "",
            "JIDANPU_EGRESS_QUARANTINE_SECONDS": "",
        },
    ):
        yield


class _RecordingPool:
    """客户端池的替身：记下「谁被丢过 / 整池作废过几次」（判据钉在**调用集合**上，E-0009）。

    ⚠️ 两种动作**分开记**：`invalidate` = **当场关**一个键（段中间调用会
    把本轮还要用的客户端关掉 ⇒ 假故障）；`mark_all_stale` = 只打标记、下一次 `acquire` 才换。
    出口切换只许走后者。
    """

    def __init__(self):
        self.invalidated: list[int] = []
        self.marked_stale = 0

    def invalidate(self, binding) -> None:
        self.invalidated.append(binding.pk)

    def mark_all_stale(self) -> None:
        self.marked_stale += 1

    def acquire(self, binding):
        """真跑一轮时会走到这里（各段都打了桩 ⇒ 这一对只被传递）。"""
        return mock.Mock(), mock.Mock()

    @contextmanager
    def session(self, binding):
        """`jidanpu.member_session` 的形状（真跑 `poll_log` / `pull_statuses` 的用例会走到）。"""
        yield self.acquire(binding)


class _ClosablePair:
    """**会真的被关掉**的客户端替身（回归用例要它）。"""

    def __init__(self):
        self.client = mock.Mock()
        self.http = mock.Mock()
        self.open = True

    def close(self):
        self.open = False

    def orders(self, **_kwargs):
        """「我的订单」那一段：用的是**同一个**客户端 ⇒ 被关过就当场炸（真实形态）。"""
        if not self.open:
            raise RuntimeError("client has been closed")
        return jidanpu_upstream.MyOrdersOutcome()


def _blocked():
    """真阶段抛的那个异常：`UpstreamBlockedError`（对端 468 / WAF 拦截，T1 已收口）。"""
    return jidanpu.UpstreamBlockedError("对端 WAF/风控拦截")


class EgressOutletSwitchTests(TenantTestCase):
    """对端 468 / WAF 拦截也要换出口，并且换的时候留痕（出口池线）。

    ⚠️ **每条用例都从「真阶段抛 468」起跑**：⛔ **不 patch `_run_*_binding` 整个函数**
    ——那样测出来的是「逐绑定那层收到异常」，而真实路径上**各段边界会先把异常吞掉**
    （它们都不 re-raise）⇒ 那种用例绿着、生产上却一次都不换出口。
    本文件把 468 抛在**最里层的段**上（上家 `sync_hall` / 下家 `run_outbox` / ②式回调消费）。
    """

    H1 = "http://u1:pw-one@h1.egress.invalid:1"
    H2 = "http://u2:pw-two@h2.egress.invalid:2"
    LOGGER = "apps.business.management.commands.jidanpu_sync"

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        # ⚠️ 闸门锚 / 退避计数都是**进程内**状态 ⇒ 跨用例会串味（照 `test_jidanpu_upstream_schedule`）。
        for name in (
            "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
            "_UPSTREAM_OPENAPI_RECONCILE_AT", "_UPSTREAM_FAILURE_COUNT",
            "_UPSTREAM_PENALTY_UNTIL", "_MEMBER_FAST_AT", "_MEMBER_RECONCILE_AT",
        ):
            getattr(module, name, {}).clear()
        jidanpu_proxy.reset_counter_for_tests()
        # ⚠️ 出口状态是**进程内**的 ⇒ 用完必须清（隔离期漏到别的模块会让「当前出口」变成 `None`）。
        self.addCleanup(jidanpu_proxy.reset_counter_for_tests)
        self.tenant = mock.Mock(schema_name="tenant-egress-outlet")
        self.command = module.Command(stdout=io.StringIO(), stderr=io.StringIO())
        self.pool = _RecordingPool()

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def _upstream(self, name):
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.create_user(
                username=f"kefu-egress-up-{name}", password="pw-canary-x"
            ),
            jidanpu_base_url="https://jidanpu-egress-up.test.invalid",
            account=f"acc-egress-up-{name}",
            password="pw-canary-egress",
            enabled=True,
        )

    def _member(self, name, *, openapi=False):
        if openapi:
            # 界面真建出来的②式行（账号密码为空 + 地址 / 密钥齐全）⇒ 过得了入口闸。
            return JidanpuMemberBinding.objects.create(
                user=User.objects.create_user(
                    username=f"kefu-egress-mem-{name}", password="pw-canary-x"
                ),
                jidanpu_base_url="https://jidanpu-egress-mem.test.invalid",
                account="", password="", mode="openapi",
                client_id=f"synthetic-egress-{name}",
                client_secret="synthetic-egress-secret-x",
                enabled=True,
            )
        return JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(
                username=f"kefu-egress-mem-{name}", password="pw-canary-x"
            ),
            jidanpu_base_url="https://jidanpu-egress-mem.test.invalid",
            account=f"acc-egress-mem-{name}",
            password="pw-canary-mem-egress",
            enabled=True,
        )

    # ── 桩（⛔ 只桩**段**与**池**，⛔ 不桩 `_run_*_binding`）─────────────────

    @contextmanager
    def _upstream_pool(self):
        with mock.patch.object(self.module.Command, "_upstream_pool", lambda _cmd: self.pool):
            yield

    @contextmanager
    def _member_pool(self):
        with mock.patch.object(jidanpu, "member_pool", lambda: self.pool):
            yield

    @contextmanager
    def _upstream_segments(self, *, hall=None):
        """上家三段：outbox / 我的订单正常，**大厅按需**抛 468（`hall` = 异常实例）。"""
        hall_stub = (
            mock.patch.object(jidanpu_upstream, "sync_hall", side_effect=hall)
            if hall is not None
            else mock.patch.object(
                jidanpu_upstream, "sync_hall", return_value=jidanpu_upstream.HallOutcome()
            )
        )
        with mock.patch.object(
                 jidanpu_upstream, "run_outbox",
                 return_value=jidanpu_upstream.OutboxStats(),
             ), hall_stub, mock.patch.object(
                 jidanpu_upstream, "sync_my_orders",
                 return_value=jidanpu_upstream.MyOrdersOutcome(),
             ):
            yield

    def _seed_anchors(self, *names, binding):
        """把给定的闸门锚拨到「刚跑过」⇒ 那几段这一轮**不到点**。"""
        for name in names:
            getattr(self.module, name)[("tenant-egress-outlet", binding.pk)] = _NOW

    def _seed_member_idle(self, binding):
        """下家①式：快路与对账都拨到「刚跑过」⇒ 只剩 outbox 那一段会打上游。"""
        self._seed_anchors("_MEMBER_FAST_AT", "_MEMBER_RECONCILE_AT", binding=binding)

    # ── 跑 ──────────────────────────────────────────────────────────────────

    def _run_upstream_round(self):
        return self.command._run_upstream_bindings(self.tenant, now=_NOW)

    def _run_upstream_rounds(self, *, times=1, fresh_round=False):
        """跑 N 轮；`fresh_round` ⇒ 每轮之前清掉**退避窗口与间隔锚**（模拟「到点了再试」）。

        ⚠️ 两样都必须清：① 连续失败会进 §2.3 的整轮退避（后续几拍**一次上游都不打**）；
        ② 大厅 / 我的订单各有自己的间隔锚（300 / 60 秒）⇒ 不清锚的话第 2 轮起**不到点**、
        一次上游都不打。两样都不清就根本攒不到 3 次拦截——那是闸门与退避的功劳，
        ⛔ 不是本类要测的东西（会让用例假红）。
        """
        results = []
        for _ in range(times):
            if fresh_round:
                for name in (
                    "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
                    "_UPSTREAM_OPENAPI_RECONCILE_AT",
                    "_UPSTREAM_PENALTY_UNTIL",
                ):
                    getattr(self.module, name).clear()
            results.append(self._run_upstream_round())
        return results

    def _run_member_round(self):
        return self.command._run_member_bindings(self.tenant, now=_NOW)

    def _run_member_rounds(self, *, times=1, fresh_round=False):
        """跑 N 轮下家；`fresh_round` ⇒ 每轮之前清掉间隔锚**与行级退避**（模拟「到点了再试」）。

        ⚠️ `_UPSTREAM_PENALTY_UNTIL` 也要清：下家有**行级退避**
        （快路抛一次 ⇒ 整行 30 秒内不再跑）⇒ 不清它，第 2 轮起一次上游都不打，
        连续拦截永远攒不到阈值（用例会假红）。
        """
        results = []
        for _ in range(times):
            if fresh_round:
                self.module._MEMBER_FAST_AT.clear()
                self.module._MEMBER_RECONCILE_AT.clear()
                self.module._UPSTREAM_PENALTY_UNTIL.clear()
                self.module._UPSTREAM_FAILURE_COUNT.clear()
            results.append(self._run_member_round())
        return results

    @contextmanager
    def _member_segments(self, *, poll_log=None):
        """下家①式的五段（自愈 / outbox / 快路 / 回拉 / 对账），⛔ 零网络。

        `poll_log` 给一个异常 ⇒ 快路那一段抛它（其它段照常成功）。
        """
        poll = (
            mock.patch.object(jidanpu_member, "poll_log", side_effect=poll_log)
            if poll_log is not None
            else mock.patch.object(jidanpu_member, "poll_log", return_value=0)
        )
        with mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(),
             ), poll, mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            yield

    def _run_clean_round(self, binding):
        """真跑一轮上家绑定（三段 + 建连全打桩，⛔ 零网络）：一个失败都没有。"""
        with self._upstream_segments(), self._upstream_pool():
            return self.command._run_upstream_binding(self.tenant, binding, now=_NOW)

    # ── ① C1：468 在最里层段边界就要记账（⛔ 别指望逐绑定那层）──────────────

    def test_a_blocked_hall_counts_exactly_once_and_keeps_the_session(self):
        """一次 468 ⇒ 出口池只记**一次**、不重复计数，且**先不丢会话**（还没到阈值）。

        失败方向：① 不在段边界记账 ⇒ `note_blocked` 调用数 = 0（改前就是这样）⇒ 本用例红；
        ② 段边界与逐绑定层各记一次 ⇒ 调用数 = 2 ⇒ 本用例红。
        """
        self._upstream("blocked-once")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=_blocked()), \
             mock.patch.object(
                 jidanpu_proxy, "note_blocked", wraps=jidanpu_proxy.note_blocked
             ) as note_blocked:
            self._run_upstream_rounds()
            self.assertEqual(note_blocked.call_count, 1, "同一次 468 只许记一次")
            self.assertEqual(self.pool.invalidated, [], "没到阈值不该丢会话")
            self.assertEqual(jidanpu_proxy.current_label(), "0:h1.egress.invalid")

    def test_three_consecutive_blocked_rounds_switch_the_outlet(self):
        """连续 3 轮 468 ⇒ 出口标签变化；**只有换出口那一次**丢池里那份会话（否则换出口不落地）。"""
        binding = self._upstream("blocked-three")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=_blocked()):
            with self.assertLogs(self.LOGGER, level="WARNING") as caught:
                self._run_upstream_rounds(times=3, fresh_round=True)
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")
        # ⭐ 切换 ⇒ **整池作废**（只打标记），⛔ 不在段中间关客户端。
        self.assertEqual(self.pool.marked_stale, 1)
        self.assertEqual(self.pool.invalidated, [], "⛔ 段中间不许关客户端（评审 ②）")
        warnings = "\n".join(caught.output)
        self.assertIn("已切换到下一个出口", warnings)
        self.assertIn("outlet=1:h2.egress.invalid", warnings)
        self.assertEqual(len(caught.records), 1, f"切换只许留一条告警（实际：{caught.output}）")
        # ⛔ 日志 / 文案里不得出现代理的账号或密码。
        self.assertNotIn("pw-two", warnings)
        self.assertNotIn("u2:", warnings)

    def test_a_counted_block_logs_no_warning(self):
        """没到阈值的拦截**不刷告警**（只有「出口真的变了」才值得一条 WARNING）。"""
        self._upstream("counted")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=_blocked()):
            with self.assertNoLogs(self.LOGGER, level="WARNING"):
                self._run_upstream_rounds()
        self.assertEqual(self.pool.invalidated, [])

    def test_a_blocked_segment_never_closes_the_client_the_round_is_still_using(self):
        """切换**不许在段中间关客户端**（回归钉）。

        `_run_upstream_binding` 先 `acquire` 一次，大厅段 468、**我的订单段接着用同一个客户端**。
        改前：切换当场 `close_client_pair` ⇒ 我的订单撞 `RuntimeError: client has been closed`
        ⇒ 被记成「我的订单未完成」并触发整轮退避（**假故障**）。
        ⚠️ 这条必须用**真池**（`UpstreamClientPool`）：关闭动作只有它做，替身池测不出来。
        """
        self._upstream("midround")
        pair = _ClosablePair()
        pool = jidanpu.UpstreamClientPool()
        with _egress_env(f"{self.H1},{self.H2}"), \
             mock.patch.object(self.module.Command, "_upstream_pool", lambda _cmd: pool), \
             mock.patch.object(
                 jidanpu, "_open_client_pair", lambda _b: (pair.client, pair.http)
             ), mock.patch.object(jidanpu, "close_client_pair", lambda _c, _h: pair.close()):
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()  # 再被拦一次就到阈值 ⇒ 大厅那一段触发切换
            with mock.patch.object(
                     jidanpu_upstream, "run_outbox",
                     return_value=jidanpu_upstream.OutboxStats(),
                 ), mock.patch.object(
                     jidanpu_upstream, "sync_hall", side_effect=_blocked()
                 ), mock.patch.object(
                     jidanpu_upstream, "sync_my_orders", side_effect=pair.orders
                 ):
                self._run_upstream_round()
        self.assertTrue(pair.open, "切换当场关掉了本轮还要用的客户端（= 评审 ② 的假故障）")
        self.assertNotIn("我的订单未完成", self.command.stdout.getvalue())

    def test_the_exhausted_warning_says_it_fell_back_to_direct(self):
        """全隔离 ⇒ 告警说「已回落直连」（⛔ 不许说「已切换到下一个出口」——那是假话），
        并且**也要让整池作废**（否则连接还挂在被封的出口上，「回落直连」同样不落地）。

        ⭐ 只有「第一次切到直连」才告警；已经回落直连期间再被拦
        ⇒ `counted`（⛔ 不再 invalidate、⛔ 每拍刷一条「已回落直连」）。
        """
        self._upstream("exhausted")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=_blocked()):
            with self.assertLogs(self.LOGGER, level="WARNING") as caught:
                # 3 轮切到 h2、第 6 轮全隔离、第 7 轮落在直连上再被拦。
                self._run_upstream_rounds(times=7, fresh_round=True)
            self.assertEqual(jidanpu_proxy.current_label(), "-:direct")
        warnings = "\n".join(caught.output)
        self.assertIn("全部出口都在隔离期，已回落直连", warnings)
        self.assertIn("outlet=-:direct", warnings)
        self.assertEqual(
            len(caught.records), 2, f"切换 1 条 + 回落直连 1 条，⛔ 不是每拍一条（{caught.output}）"
        )
        # 切换那一次 + 回落直连那一次 ⇒ 整池作废两次；直连上再被拦不算。
        self.assertEqual(self.pool.marked_stale, 2)
        self.assertEqual(self.pool.invalidated, [])

    # ── ② 段边界先吞掉、逐绑定那层收不到（已知缺口，只登记不修）───────────────

    def test_an_auth_error_from_a_stage_never_reaches_the_per_binding_boundary(self):
        """**已知缺陷的见证**（`AuthError` 那条既有分支今天同样不可达，**只登记不修**）。

        段边界 `except Exception` 先接住登录被拒、且⛔ 不 re-raise ⇒ 逐绑定那层的
        `invalidate` 判据收不到。**修好之后本用例会红**，届时把它改成断言 `invalidate` 被调
        （⛔ 别顺手删掉：它是这条缺陷唯一的见证）。
        """
        self._upstream("auth")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=AuthError("登录失败")):
            self._run_upstream_rounds()
        self.assertEqual(self.pool.invalidated, [])

    def test_an_api_error_never_counts_and_never_drops_the_session(self):
        """回归：普通 4xx（`ApiError`）⛔ 不是封禁 —— 不计入出口池、不丢会话。"""
        self._upstream("api")
        with _egress_env(f"{self.H1},{self.H2}"), self._upstream_pool(), \
             self._upstream_segments(hall=ApiError("普通 4xx")), \
             mock.patch.object(
                 jidanpu_proxy, "note_blocked", wraps=jidanpu_proxy.note_blocked
             ) as note_blocked:
            self._run_upstream_rounds()
        self.assertEqual(note_blocked.call_count, 0)
        self.assertEqual(self.pool.invalidated, [])

    # ── ③ 下家那一处判据（`:509`）同样从真阶段起跑 ──────────────────────────

    def test_three_blocked_member_rounds_switch_the_outlet(self):
        """下家①式：**快路**连续 3 轮抛 468 ⇒ 出口标签变化 + 整池作废。

        ⚠️ 为什么不用 outbox 当触发点：`run_outbox` 是**逐事件吞掉**再抛
        聚合异常 ⇒ 段边界那个 `isinstance` 收不到（见下面 witness 那条）。快路（`poll_log`）
        没有内部吞，468 会原样逃到段边界 —— 这条才是真可达的下家路径。
        """
        binding = self._member("blocked")
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             self._member_segments(poll_log=_blocked()):
            self._seed_member_idle(binding)
            with self.assertLogs(self.LOGGER, level="WARNING") as caught:
                self._run_member_rounds(times=3, fresh_round=True)
            # ⭐ 精确断言（修 C1 后恢复）：每次 468 **只记一个数** ⇒ 第 3 轮恰好切到 H2。
            #    ⛔ 别退回 `assertIn` —— 那会把「同一发记两次（阈值 3 实际 2 次就切）」盖住。
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")
        self.assertGreaterEqual(self.pool.marked_stale, 1)
        self.assertEqual(self.pool.invalidated, [])
        self.assertIn("下家/快路", "\n".join(caught.output))

    # ── ⓑ 已知缺口见证（内部吞 468 的段不修）─────────

    def test_a_468_inside_the_member_outbox_never_reaches_the_stage_boundary(self):
        """**已知缺口见证**：outbox 的 468 到不了段边界（`run_outbox` 逐事件吞掉，只抛聚合异常）。

        ⚠️ **本期不修**（裁定：outbox 逐事件 / ②式逐单状态查询 / 下家自愈 / 拉图
        这几处内部吞 468 都留着）。RUNBOOK §3.0.2 的「已知覆盖缺口」登记了同一件事。
        **修好之后本用例会红**：届时改成断言「记账发生了」（并划掉 RUNBOOK 那条），⛔ 别删。
        """
        binding = self._member("outbox-witness")
        JidanpuSyncEvent.objects.create(
            direction="out",
            kind=jidanpu_member.KIND_STATUS_OUT,
            payload={"member_binding_id": binding.pk},
            status="pending",
        )

        def _real_outbox(_binding, *, now, **_kwargs):
            """⛔ 不桩 `run_outbox` 本身：让它按真实形状抛聚合异常。"""
            return jidanpu_member.run_outbox(_binding, now=now, client=mock.Mock())

        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(jidanpu_member, "_consume", side_effect=_blocked()), \
             mock.patch.object(jidanpu_member, "run_outbox", side_effect=_real_outbox), \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), \
             mock.patch.object(
                 jidanpu_proxy, "note_blocked", wraps=jidanpu_proxy.note_blocked
             ) as note_blocked:
            self._seed_member_idle(binding)
            self._run_member_round()
            # ⚠️ 这一句是**缺口本身**：outbox 里那次 468 一次都没记进出口池。
            self.assertEqual(
                note_blocked.call_count, 0,
                "outbox 的 468 竟然到达了段边界 ⇒ 缺口已修，请把本用例改成断言记账发生",
            )
        # ⛔ 但它**不是静默**：stderr 上有「outbox 出现未预期失败」，且聚合异常只带类型名。
        self.assertIn("outbox", self.command.stderr.getvalue())

    def test_a_468_on_the_openapi_status_query_never_reaches_the_stage_boundary(self):
        """**已知缺口见证**：②式**逐单状态查询**的 468 在引擎里就被吞了。

        `apply_authoritative_status` 的 `try/except` 把 `queries.status_of()` 的任何异常变成
        「本轮不写 + `_RETRY`」⇒ 调度层的段边界收不到。②式的**回调消费**与**兜底对账**
        都走这一段（见 `jidanpu_openapi_consume` 的 docstring：两个调用方）。
        ⚠️ 本期不修；修好后本用例会红（改成断言记账发生 + 划掉 RUNBOOK 那条），⛔ 别删。
        """
        binding = self._member("openapi-witness", openapi=True)
        queries = SimpleNamespace(status_of=mock.Mock(side_effect=_blocked()), cap=10)
        findings: list[str] = []
        with _egress_env(f"{self.H1},{self.H2}"), \
             mock.patch.object(jidanpu_openapi_consume, "_note_order") as note_order, \
             mock.patch.object(
                 jidanpu_proxy, "note_blocked", wraps=jidanpu_proxy.note_blocked
             ) as note_blocked:
            # ⚠️ 顺手把引擎那一条 warning 收进断言（⛔ 不许静默，也⛔ 不刷测试输出）。
            with self.assertLogs(
                "apps.business.jidanpu_openapi_consume", level="WARNING"
            ) as engine_logs:
                outcome = jidanpu_openapi_consume.apply_authoritative_status(
                    order=mock.Mock(), binding=binding, order_no="SYNTH-EGRESS-1",
                    queries=queries, now=_NOW, findings=findings,
                    trigger=jidanpu_openapi_consume.TRIGGER_CALLBACK,
                )
        self.assertEqual(outcome, jidanpu_openapi_consume.OUTCOME_RETRY, "⛔ 不外抛")
        self.assertTrue(note_order.called, "⛔ 也不许静默：要留一条台账")
        self.assertTrue(findings)
        self.assertIn("查权威状态失败", "\n".join(engine_logs.output))
        self.assertEqual(
            note_blocked.call_count, 0,
            "引擎里的 468 竟然到达了段边界 ⇒ 缺口已修，请把本用例改成断言记账发生",
        )

    # ── ④ 逐绑定那一行（两条线都带 outlet=）─────────────────────────

    def test_both_line_round_lines_carry_the_outlet_label(self):
        """下家与上家的逐绑定那一行**都**带 `outlet=`（两条线的可观测口径一致）。"""
        upstream = self._upstream("line-up")
        member = self._member("line-mem")
        with _egress_env(f"{self.H1},{self.H2}"):
            self._run_clean_round(upstream)
            self._seed_member_idle(member)
            with mock.patch.object(jidanpu_member, "heal_binding_identity"), \
                 mock.patch.object(
                     jidanpu_member, "run_outbox",
                     return_value=jidanpu_member.OutboxStats(),
                 ):
                self._run_member_round()
        out = self.command.stdout.getvalue()
        self.assertIn(f"上家绑定 #{upstream.pk}", out)
        self.assertIn(f"代练员绑定 #{member.pk}", out)
        self.assertEqual(out.count("outlet=0:h1.egress.invalid"), 2, out)
        self.assertNotIn("pw-one", out)

    # ── ⑤ 复位口径─────────────────────────────────────────

    def test_a_clean_round_that_actually_ran_resets_the_consecutive_block_counter(self):
        """拦截 1 次 → **真跑过**一轮且成功 → 再拦截 2 次 ⇒ **不切换**（成功一次即复位）。"""
        binding = self._upstream("clean")
        with _egress_env(f"{self.H1},{self.H2}"):
            jidanpu_proxy.note_blocked()
            self.assertFalse(self._run_clean_round(binding))  # 大厅 / 我的订单都到点且成功
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "0:h1.egress.invalid")

    def test_an_empty_round_never_resets_the_consecutive_block_counter(self):
        """拦截 1 次 → **空轮 2 次**（什么都没到点）→ 再拦截 2 次 ⇒ **必须切换**。

        ⚠️ 承重理由：连续拦截计数的意义是「同一出口**连着**被拦 3 次
        就换」。空轮只是「这一拍什么都没到点」（⛔ 不是「上游通了」）——拿它清零，等于
        「被拦一次之后隔几拍就永远攒不到阈值」，而那正是本线要防的形态。
        """
        binding = self._upstream("empty-rounds")
        with _egress_env(f"{self.H1},{self.H2}"):
            jidanpu_proxy.note_blocked()
            self._seed_anchors(
                "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
                "_UPSTREAM_OPENAPI_RECONCILE_AT",
                binding=binding,
            )
            for _ in range(2):
                self.assertFalse(self._run_clean_round(binding))
            self.assertEqual(
                jidanpu_proxy.current_label(), "0:h1.egress.invalid",
                "空轮本身不该换出口（阈值是 3 次**拦截**，不是 3 轮）",
            )
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")

    def test_a_reconcile_only_early_return_round_does_not_reset_the_counter(self):
        """①式下兜底对账**早退**（`ReconcileOutcome(enabled=False)`，一次库 / 上游都不打）
        也⛔ 不算「真跑过」⇒ 不复位连续拦截计数。

        失败方向：`ran_a_stage` 只判 `reconcile is not None` ⇒ ①式每 `reconcile_seconds`
        （默认 600 秒）就有一次这种「非 None 但没干活」的空轮 ⇒ 每 10 分钟白清一次计数 ⇒ 本用例红。
        """
        binding = self._upstream("reconcile-only")
        with _egress_env(f"{self.H1},{self.H2}"):
            jidanpu_proxy.note_blocked()
            # 三条读路 + 快路「刚跑过」⇒ 只剩兜底对账到点（①式 ⇒ 直接早退，非 None）。
            self._seed_anchors(
                "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
                binding=binding,
            )
            self.assertFalse(self._run_clean_round(binding))
            self.assertEqual(jidanpu_proxy.current_label(), "0:h1.egress.invalid")
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")

    # ── ⑥ 下家也要复位（只记账不复位 = 下家的计数永远攒着）──

    def test_a_member_round_that_actually_ran_resets_the_consecutive_block_counter(self):
        """下家**真跑过且干净**的一轮同样 `note_ok()`（与上家同一条判据口径）。

        失败方向：下家整条线不接 `note_ok()` ⇒ 一次拦截之后计数永远攒着，隔多久再拦都算「连续」
        ⇒ 本用例里那 2 次会把计数推到 3 ⇒ 当场换出口 ⇒ 红。
        """
        binding = self._member("member-clean")
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(),
             ), mock.patch.object(
                 jidanpu, "fetch_system_logs", return_value=[]
             ), mock.patch.object(
                 jidanpu_member, "pull_statuses", return_value=0
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ):
            # ⭐ **真跑**快路（只把上游取数换成回空列表）⇒ 「这一发真的调通了上游」。
            self._seed_anchors("_MEMBER_RECONCILE_AT", binding=binding)
            jidanpu_proxy.note_blocked()
            self.assertFalse(self._run_member_round())
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "0:h1.egress.invalid")

    def test_an_empty_member_round_never_resets_the_consecutive_block_counter(self):
        """下家的**空轮**（快路 / 对账都没到点、outbox 空转）也⛔ 不复位。

        ⚠️ 与上家那条**同一条口径**：⛔ 不许因为「下家没有 read_failed 那个判据」就把
        「整轮干净」当「真跑过」。
        """
        binding = self._member("member-empty")
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             self._member_segments():
            self._seed_member_idle(binding)
            jidanpu_proxy.note_blocked()
            ok = self._run_member_round()
            self.assertFalse(ok, self.command.stderr.getvalue())
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")

    # ── ⑦ 真跑过的判据 / 被吞的 468 / 468 归属 ─────────────────────

    def test_a_zero_upstream_early_return_round_does_not_reset_the_counter(self):
        """`_pull_statuses` 在「一条待处理链路都没有」时**零上游早退**（`return 0`）⇒ ⛔ 不算真跑过。

        ⚠️ `applied is not None` 把那个 `return 0` 也算成真跑过 ⇒ 空闲下家每拍复位共享计数
        ⇒ 一次被拦之后永远攒不到阈值（I1 同族）。失败方向：判据回到「非 None」⇒ 本用例红。
        """
        binding = self._member("zero-upstream")
        empty = mock.Mock()
        empty.select_related.return_value = []  # `pullable_links(...).select_related(...)`
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(),
             ), mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), mock.patch.object(
                 jidanpu_member, "pullable_links", return_value=empty
             ):
            # 只让**对账**那一段到点（快路拨到刚跑过）⇒ 这一轮的唯一动作就是那个零上游早退。
            self._seed_anchors("_MEMBER_FAST_AT", binding=binding)
            jidanpu_proxy.note_blocked()
            ok = self._run_member_round()
            self.assertFalse(ok, self.command.stderr.getvalue())
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")

    def test_a_swallowed_468_never_counts_as_a_clean_round(self):
        """同一轮里**快路撞了被吞的 468**、而**回拉真的调通了上游** ⇒ ⛔ 仍然不许复位。

        现场：`poll_log` 里那个 468 被 `except JidanpuError` 吞掉、回 0；同一轮的 `pull_statuses`
        确实打到了上游（`LAST_CALL.upstream` 为真）。改前这一轮会被判「真跑过且干净」⇒
        `note_ok()` 把共享计数清零 ⇒ 生产上「下家已有会话后被拦」永远攒不到阈值。

        ⚠️ **两段都必须发生**：只让快路 468 的话，「不复位」是被「没真打过上游」挡住的（① 那半边），
        `blocked` 这一格根本测不到（变异证明：去掉 `not swallowed_block` 时那种写法仍绿）。
        """
        binding = self._member("swallowed")
        links = mock.Mock()
        links.select_related.return_value = [mock.Mock()]
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(),
             ), mock.patch.object(
                 jidanpu, "fetch_system_logs", side_effect=_blocked()
             ), mock.patch.object(
                 jidanpu_member, "pullable_links", return_value=links
             ), mock.patch.object(
                 jidanpu_member, "_fetch_completed_rows",
                 return_value=([], True, "", 1),
             ), mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), mock.patch.object(
                 jidanpu_proxy, "note_blocked", wraps=jidanpu_proxy.note_blocked
             ) as note_blocked:
            jidanpu_proxy.note_blocked()  # 第 1 次（我手动记的）
            self.assertFalse(self._run_member_round())
            self.assertEqual(
                note_blocked.call_count, 1,
                "被吞的 468 不该计入出口池（那是本期的已知缺口）",
            )
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")

    def test_a_late_468_from_the_old_outlet_never_charges_the_new_outlet(self):
        """同一轮里 A 被切走之后，**仍用 A 客户端**的 468 ⛔ 不许把 B 的计数推高。

        现场：`_run_upstream_binding` 先 `acquire`（客户端烙着当时的出口 A），大厅段 468 触发切换
        A→B；**我的订单段还用同一个客户端**（仍是 A），它这一发 468 属于 A —— 记到 B 头上会让
        **健康出口** B 被误隔离。失败方向：按「记账那一刻的当前出口」记（改前就是这样）⇒ 本用例红。
        ⚠️ 用**真池 + 真 `_open_client_pair`**（只把 httpx / SDK 客户端换成替身）⇒ 出口烙印是真的。
        """
        self._upstream("attribution")
        pool = jidanpu.UpstreamClientPool()
        with _egress_env(f"{self.H1},{self.H2}"), \
             mock.patch.object(self.module.Command, "_upstream_pool", lambda _cmd: pool), \
             mock.patch.object(jidanpu.httpx, "Client"), \
             mock.patch.object(jidanpu, "_PooledJidanpuClient"), \
             mock.patch.object(
                 jidanpu_upstream, "run_outbox",
                 return_value=jidanpu_upstream.OutboxStats(),
             ), mock.patch.object(
                 jidanpu_upstream, "sync_hall", side_effect=_blocked()
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", side_effect=_blocked()
             ):
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()  # A 已 2 次 ⇒ 大厅那一发是第 3 次 ⇒ 切到 B
            self._run_upstream_round()
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")
            # ⭐ B 的计数必须是 **0**：再拦两次（共 2 次）⛔ 不该换出口。
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(
                jidanpu_proxy.current_label(), "1:h2.egress.invalid",
                "A 客户端迟到的 468 被记到 B 头上了（健康出口被误隔离）",
            )
            # 第 3 次才到阈值 ⇒ 那时才「无处可切」（池里只剩直连）。
            self.assertEqual(jidanpu_proxy.note_blocked(), "exhausted")

    def test_an_acquire_failure_keeps_its_own_exception_type(self):
        """池建连自己抛（`acquire` 失败）⇒ 记下的必须是**原异常类型**（回归钉）。

        现场：`client, http = acquire(...)` 在 `try` 里，而 `except` 又拿 `client` 去记账
        ⇒ acquire 抛的那一刻 `client` **还没绑定** ⇒ 那个分支自己抛 `UnboundLocalError`，
        把真正的异常类型与 outbox 的失败明细一起吞掉（页面/终端看不到真原因）。
        失败方向：不在 `try` 之前先给 `client = None` ⇒ stderr 里出现 `UnboundLocalError` ⇒ 本用例红。
        """
        self._upstream("acquire-boom")
        pool = mock.Mock()
        pool.acquire.side_effect = RuntimeError("池建连炸了")
        with _egress_env(f"{self.H1},{self.H2}"), \
             mock.patch.object(self.module.Command, "_upstream_pool", lambda _cmd: pool):
            self._run_upstream_rounds()
        err = self.command.stderr.getvalue()
        self.assertIn("RuntimeError", err, f"原异常类型被吞了：{err}")
        self.assertNotIn("UnboundLocalError", err, f"记账分支自己炸了：{err}")
        # ⚠️ 明细也要还在：那一段报的仍是 outbox（⛔ 不是「本轮」）。
        self.assertIn("outbox", err)

    def test_a_late_468_on_the_member_line_never_charges_the_new_outlet(self):
        """下家也要按「**建连出口**」归属：旧客户端的 468 ⛔ 不许记到新出口。

        现场：出口已在 H1 上被切到 H2，而下家那一段上一次真实建连用的仍是 **H1** 那个客户端
        （池里的会话跨轮复用）⇒ 它这一发 468 属于 H1。改前 `_note_member_blocked_stage` 不传
        客户端 ⇒ 按「当前出口」记 ⇒ **H2 被误隔离**（就是上家 C1 那一档重演）。
        """
        binding = self._member("member-attribution")
        stale = mock.Mock()
        stale._tt_egress_outlet = self.H1  # 这个客户端是 H1 建出来的
        links = mock.Mock()
        links.select_related.return_value = [mock.Mock()]
        with _egress_env(f"{self.H1},{self.H2}"), self._member_pool(), \
             mock.patch.object(jidanpu_member, "heal_binding_identity"), \
             mock.patch.object(
                 jidanpu_member, "run_outbox",
                 return_value=jidanpu_member.OutboxStats(),
             ), mock.patch.object(
                 jidanpu_member, "poll_log", return_value=0
             ), mock.patch.object(
                 jidanpu, "member_session",
                 lambda _binding: nullcontext((stale, mock.Mock())),
             ), mock.patch.object(
                 jidanpu_member, "pullable_links", return_value=links
             ), mock.patch.object(
                 jidanpu_member, "_fetch_completed_rows",
                 return_value=([], True, "", 1),
             ):
            for _ in range(3):
                jidanpu_proxy.note_blocked()  # 当前出口 H1 到阈值 ⇒ 切到 H2
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.egress.invalid")
            # 这一轮：回拉**真的建连**（用的是烙着 H1 的 stale 客户端），随后对账抛 468。
            with mock.patch.object(
                     jidanpu_member, "reconcile_and_queue", side_effect=_blocked()
                 ):
                self._run_member_round()
            # H2 的计数必须还是 0：再拦两次不换、第 3 次才 exhausted。
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(
                jidanpu_proxy.current_label(), "1:h2.egress.invalid",
                "旧客户端（H1）的 468 被记到 H2 头上了（清白的出口被误隔离）",
            )
            self.assertEqual(jidanpu_proxy.note_blocked(), "exhausted")
