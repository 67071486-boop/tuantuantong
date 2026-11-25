"""发版常驻同步、定时任务与回调接收器接线检查。出向命令参数另由 test_deploy_sync_inventory 验证。"""
from __future__ import annotations

import shlex
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from .test_deploy_sync_inventory import rendered_sync_script

#: `src/tests/` 往上两级 = 仓库根；单元与部署脚本都在仓库根的 `deploy/` 下。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"

# 已有单元的命令契约。新增常驻单元的发布覆盖独立从 .service 文件发现。
_SYNC_COMMANDS = (
    ("jidanpu_sync", "tuantuantong-jidanpu-sync"),
    # ⭐ `docs/plans/2026-10-25-jidanpu-outbox-worker.md` §2：**出向事件消费**从
    #    `jidanpu_sync --loop` 拆出来的独立常驻进程（`--loop --outbox-only`）。
    #    命令名与上面那条**相同**、单元名不同：两者必须**同时**在跑（一个轮询、一个出向）
    #    ⇒ 各自都要走完四处接线。
    ("jidanpu_sync", "tuantuantong-jidanpu-outbox"),
    # ⭐ **②式（官方 API）回调消费**再拆一个独立常驻进程
    #    （`jidanpu_sync --loop --callback-only`）。⚠️ 它与上面两个同属一条线、**三台互补**：
    #    主单元跳过① outbox 与②式回调；出向单元只跑① outbox；本单元只跑②式回调那一段
    #    （含它自己的出向发单——那一段有同轮顺序依赖，⛔ 不能并进出向单元）。
    ("jidanpu_sync", "tuantuantong-jidanpu-callback"),
# ⭐ **①式日志快路（含身份自愈）**再拆一个独立常驻进程
# （`jidanpu_sync --loop --fastpath-only`）。⚠️ 它与上面三台**四台互补**：
# 主单元再跳过 ⓪ 自愈 + ② 快路；本单元只跑那两段（heal 与快路同进同出，
# ⛔ 不许把 heal 留在主单元——那会两个进程重复自愈）。
    ("jidanpu_sync", "tuantuantong-jidanpu-fastpath"),
    # ⭐ **①式状态回拉 + 对账**再拆一个独立常驻进程
    #    （`jidanpu_sync --loop --reconcile-only`）。⚠️ 它与上面四台、再加后面那台上家
    #    （共**六台互补**）：主单元再跳过 ③ 回拉 + ④ 对账；本单元只跑那两段（两段**同一个
    #    单元、共用同一个 `reconcile_seconds` 闸门**，⛔ 不许拆成两台）。
    ("jidanpu_sync", "tuantuantong-jidanpu-reconcile"),
    # ⭐ **上家线**（`JidanpuUpstreamBinding`）整行再拆一台
    #    （`jidanpu_sync --loop --upstream-only`）。⚠️ 它与上面五台**六台互补**：
    #    主单元再跳过整行上家（`--skip-upstream`）⇒ 此后主单元只剩租户级模拟单轮；
    #    本单元只跑整行上家（①式 outbox + 大厅 + 我的订单；②式回调 + 兜底对账）。
    #    ⛔ **只能整行拆**：健康合成判据（`last_ok_at`）与连续失败退避都是行级的，
    #    按「大厅 / 回拉 / 对账」子段拆会让每台各算一次。
    ("jidanpu_sync", "tuantuantong-jidanpu-upstream"),
    ("peer_sync", "tuantuantong-peer-sync"),
    ("shared_peer_sync", "tuantuantong-shared-peer-sync"),
    # ⭐ 开放平台主动回调推送的常驻投递进程。
    ("openapi_dispatch", "tuantuantong-openapi-dispatch"),
    # ⭐ 打手侧主动回调推送的常驻投递进程。
    # ⚠️ 名字与 `tuantuantong-callback`（**鸡蛋铺回调接收器**，另一件事、另一个开关）**不同**
    #    ⇒ ⛔ 别把两者当成同一个（那个不属 `SYNC_SERVICES`，见 `activate.sh` 里那一段注释）。
    ("takerapi_dispatch", "tuantuantong-takerapi-dispatch"),
    # ⭐ QQ 机器人线：QQ 机器人常驻收发（WebSocket 长连接 + 发件箱推送）。
    ("qqbot_ws", "tuantuantong-qqbot-sync"),
    # ⭐ T9c（代练通状态同步）：消费回调队列 + 轮询 sent/unknown 并回读远端状态
    #    （`dailiantong_sync --loop`）。⚠️ 回调接收器（`deploy/callback/dailiantong_receiver.py`，
    #    127.0.0.1:8003）**不是**常驻调度单元、不进本表（它没有 `--loop`）。
    ("dailiantong_sync", "tuantuantong-dailiantong-sync"),
)

#: `(管理命令名, 单元基名)`——**timer 型**（`oneshot` + `.timer`），与上面的 `_SYNC_COMMANDS`
#: （常驻 `--loop`）**分开**：两者的 `ExecStart` 契约相反（常驻必须有 `--loop`，本类**不能有**）。
#: 新增一条定时任务时**同一次**加一行 + 两份单元文件 + `deploy.sh` 两处 + 两个脚本的 `PAYOUT_UNITS`。
_TIMER_COMMANDS = (
    # ⭐ `feat/wallet-single-payout` §2.3：提现「超时自动审核 + 到期自动代付」，
    #    每 10 分钟一次（默认冷静期 T+3）。
    ("payout_due_withdrawals", "tuantuantong-payout"),
)


def _read(path: Path) -> str:
    if path in (_DEPLOY / "activate.sh", _DEPLOY / "rollback.sh", _DEPLOY / "inspect_remote.sh"):
        return rendered_sync_script(path)
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


def _logical_lines(text: str) -> list[str]:
    """把 shell 的 `\\` 续行拼成逻辑行（否则「这一行里有 rsync 吗」会看错行）。"""
    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        buffer += line
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        joined.append(buffer)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class SyncUnitsAreInstalledWithEveryReleaseTests(SimpleTestCase):
    """每条同步命令都必须**四处齐全**（单元文件 / `deploy.sh` 两处 / 两份脚本的清单）。"""

    def test_every_sync_command_has_a_loop_unit_that_runs_it(self):
        """① 单元文件存在，且 `ExecStart` 起的是这条命令的 `--loop`（不是 `--once`）。

        失败方向：单元文件没建、命令名写错、开关写成 `--once`（退回分钟粒度）⇒ 本用例红。
        ⚠️ 只钉「命令名 + `--loop`」这两段：完整的 `ExecStart` 逐段契约由各线自己的用例守
        （`test_jidanpu_schedule.py` 对本线；`peer_sync` 的那份见本文件下方
        `PeerSyncUnitTests`），⛔ 不在这里重复抄一遍路径。
        ⚠️ 缺文件用 `assertTrue(path.is_file(), …)` 而**不是** `_read()`：后者会抛
        `FileNotFoundError` ⇒ 记成 ERROR 而不是 FAIL（红是红的，但判据该落在 FAIL 上）。
        """
        for command, unit in _SYNC_COMMANDS:
            path = _DEPLOY / "systemd" / f"{unit}.service"
            with self.subTest(unit=unit):
                self.assertTrue(
                    path.is_file(),
                    f"缺单元文件：deploy/systemd/{unit}.service（发版时 activate.sh 会因它缺失响亮失败）",
                )
                tokens = shlex.split(_exec_start(_read(path)))
                self.assertIn(
                    ["manage.py", command, "--loop"],
                    [tokens[i:i + 3] for i in range(len(tokens) - 2)],
                    f"{unit} 的 ExecStart 里没有 `manage.py {command} --loop`（实际：{tokens}）",
                )

    def test_activate_and_rollback_both_list_every_sync_unit(self):
        """④ `activate.sh` **与** `rollback.sh` 的单元清单里都有它。

        `activate.sh` 那份决定**装不装**；`rollback.sh` 那份决定**回滚时重不重启**——
        漏了后者 ⇒ 常驻进程带着 `Restart=always` 继续跑被回滚掉的 release，且没有任何提示。
        判据打在实际生成的脚本的数组赋值上：
        光出现在注释里不算接进了清单。
        """
        for script in ("activate.sh", "rollback.sh"):
            assignments = [
                line for line in _logical_lines(_read(_DEPLOY / script))
                if line.lstrip().startswith("SYNC_SERVICES=(")
            ]
            with self.subTest(script=script):
                self.assertEqual(
                    len(assignments), 1,
                    f"{script} 里 `SYNC_SERVICES=(…)` 恰好应有一处，实际 {len(assignments)} 处",
                )
                for command, unit in _SYNC_COMMANDS:
                    self.assertIn(
                        unit, assignments[0],
                        f"{script} 的单元清单里缺 {unit}（命令 {command}）⇒ 它不会被安装 / 重启",
                    )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class PeerSyncUnitTests(SimpleTestCase):
    """上下游那条新单元自己的契约（与兄弟线同形：常驻 + 崩了必拉起来 + ⛔ 不叠第二种调度）。"""

    _UNIT = _DEPLOY / "systemd" / "tuantuantong-peer-sync.service"

    def test_the_unit_starts_the_peer_loop_with_the_release_venv_python(self):
        """`ExecStart` 逐段钉住：`current/src` 的 venv python + `manage.py peer_sync --loop`。

        失败方向：写成 `--once`（退回「每分钟粒度」且与常驻互斥）、顺手加 `--tenant`（只服务一个租户）
        ⇒ 本用例红。
        """
        self.assertEqual(
            shlex.split(_exec_start(_read(self._UNIT))),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "peer_sync",
                "--loop",
            ],
        )

    def test_the_unit_working_directory_is_the_current_release_src(self):
        """cwd 必须是 `current/src`：单元靠它解析 `manage.py`，也靠它让新 release 生效。"""
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            _read(self._UNIT),
        )

    def test_the_unit_always_restarts_and_is_not_rate_limited_to_death(self):
        """`Restart=always` + **关掉启动限速**：常驻投递进程死了必须被拉起来。

        ⚠️ systemd 默认 `StartLimitIntervalSec=10s` / `StartLimitBurst=5`：10 秒内崩 5 次就
        **永久放弃**该单元 ⇒ 投递会**静默停摆**（台账上还留着 pending 行，没人被告知）。
        失败方向：删掉 `StartLimitIntervalSec=0` ⇒ 本用例红。
        """
        text = _read(self._UNIT)
        self.assertIn("\nRestart=always\n", text)
        self.assertIn("\nStartLimitIntervalSec=0\n", text)
        self.assertIn("\nKillSignal=SIGTERM\n", text)
        self.assertIn("\nTimeoutStopSec=30\n", text)

    def test_the_unit_is_not_a_timer(self):
        """⛔ 不叠第二种调度：单元不是 `.timer`（那会与常驻并存 ⇒ 可能并发投递）。"""
        text = _read(self._UNIT)
        self.assertNotIn("OnCalendar=", text)
        self.assertNotIn("WantedBy=timers.target", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_the_load_bearing_constraints_are_written_in_the_unit_comments(self):
        """承重约束必须**写进单元注释**（运维装单元时第一眼就看到它们）。

        ⚠️ 这条断的是**文字**、证明力有限：只保证「这几句话还在」，不保证运维照做。
        失败方向：把某条约束的注释删掉 ⇒ 本用例红（正是要让它别被顺手删）。
        """
        text = _read(self._UNIT)
        for phrase in (
            "只在一台",                    # 单实例（⛔ 两台 = 并发投递同一批事件）
            "并发投递同一批事件",           # 说清为什么单实例是承重的
            "不要再叠 cron",              # 与 --once 的 cron 会真并发
            "Restart=always",            # 死了靠 systemd 拉起来
            "last_ok_at",                # 告警判据：多久没成功投递过
            "幂等",                       # at-least-once ⇒ 靠接收侧判重，⛔ 不是"不会重复"
        ):
            self.assertIn(phrase, text, f"单元注释里缺少承重约束：{phrase}")


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class ScheduledTimerUnitsTests(SimpleTestCase):
    """**timer 型**调度单元（oneshot + `.timer`）也要**四处齐全**。

    与上面 `SyncUnitsAreInstalledWithEveryReleaseTests` **刻意分开**（⛔ 别合并成一张表）：
    timer 型的服务是 `Type=oneshot`、**不能有 `--loop`**（那会变成常驻，与 timer 叠加成两种
    调度）——合并成一张表就得开「这条不需要 `--loop`」的例外，例外一开就挡不住
    「把常驻误接成 timer」/「把 timer 误接成常驻」。

    四处（与常驻同款，⛔ 漏一处就是半新半旧）：
    ① `.service` + `.timer` 两份文件都在，且 service 是 oneshot、timer 指向它并按 10 分钟触发；
    ② `deploy.sh` 的 rsync 上传面里有**两个文件**；
    ③ `deploy.sh` 的 `chmod 644 …; md5sum …` 那一面也有**两个文件**；
    ④ `activate.sh` 与 `rollback.sh` 的 `PAYOUT_UNITS=(…)` 清单里都有这个 timer。
    """

    def test_the_service_is_oneshot_and_the_timer_fires_it_every_ten_minutes(self):
        for command, unit in _TIMER_COMMANDS:
            service = _DEPLOY / "systemd" / f"{unit}.service"
            timer = _DEPLOY / "systemd" / f"{unit}.timer"
            with self.subTest(unit=unit):
                self.assertTrue(
                    service.is_file(),
                    f"缺单元文件：deploy/systemd/{unit}.service",
                )
                self.assertTrue(
                    timer.is_file(),
                    f"缺定时器文件：deploy/systemd/{unit}.timer",
                )
                service_text = _read(service)
                self.assertIn("\nType=oneshot\n", service_text)
                tokens = shlex.split(_exec_start(service_text))
                # `manage.py <命令>` 必须相邻出现（中间不许夹别的参数）。
                self.assertIn(
                    ["manage.py", command],
                    [tokens[i:i + 2] for i in range(len(tokens) - 1)],
                    f"{unit}.service 的 ExecStart 里没有 `manage.py {command}`（实际：{tokens}）",
                )
                # ⛔ oneshot 服务不许带 `--loop`（那会变成常驻，与 timer 叠加成两种调度）。
                self.assertNotIn(
                    "--loop", tokens,
                    f"{unit}.service 是 oneshot，ExecStart 里不应有 --loop（实际：{tokens}）",
                )
                timer_text = _read(timer)
                self.assertIn(f"Unit={unit}.service", timer_text)
                self.assertIn("OnCalendar=", timer_text)
                self.assertIn("WantedBy=timers.target", timer_text)

    def test_activate_and_rollback_both_list_the_timer(self):
        """两个脚本的 `PAYOUT_UNITS=(…)` 清单里都有它（判据打在赋值那一行，⛔ 不是出现即算）。"""
        for script in ("activate.sh", "rollback.sh"):
            assignments = [
                line for line in _logical_lines(_read(_DEPLOY / script))
                if line.lstrip().startswith("PAYOUT_UNITS=(")
            ]
            with self.subTest(script=script):
                self.assertEqual(
                    len(assignments), 1,
                    f"{script} 里 `PAYOUT_UNITS=(…)` 恰好应有一处，实际 {len(assignments)} 处",
                )
                for _command, unit in _TIMER_COMMANDS:
                    self.assertIn(
                        f"{unit}.timer", assignments[0],
                        f"{script} 的 PAYOUT_UNITS 清单里缺 {unit}.timer",
                    )

    def test_deploy_dot_sh_uploads_and_checksums_both_files(self):
        """`deploy.sh` 的**上传面**与 **chmod/md5sum 面**都各有 `.service` + `.timer`。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = "\n".join(rsync_lines)
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for _command, unit in _TIMER_COMMANDS:
            with self.subTest(unit=unit):
                for face_name, surface, prefix in (
                    ("rsync", upload_surface, "deploy/systemd/"),
                    # ⚠️ chmod 与 md5sum 是**两条并列的文件清单**：只断 md5sum 会漏掉
                    #    「文件传上去了、chmod 清单里没有」那种 0600 形态（E-0097）。
                    ("chmod", chmod_face, "$R/shared/"),
                    ("md5sum", md5sum_face, "$R/shared/"),
                ):
                    self.assertIn(
                        f"{prefix}{unit}.service", surface,
                        f"deploy.sh 的 {face_name} 面里缺 {prefix}{unit}.service",
                    )
                    self.assertIn(
                        f"{prefix}{unit}.timer", surface,
                        f"deploy.sh 的 {face_name} 面里缺 {prefix}{unit}.timer",
                    )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class BackupTimerUnitTests(SimpleTestCase):
    """**每日报份**（`tuantuantong-backup.service/.timer` + `deploy/backup_daily.py`）的四处接线。
    ⚠️ 它与上面两张表（`_SYNC_COMMANDS` / `_TIMER_COMMANDS`）**都不进**，这是**刻意**的：
    那两张表的 ExecStart 契约是 `manage.py <命令>`，而本单元是
    `bash -lc '… manage.py shell < shared/backup_daily.py'`（把脚本喂给 release 的 Django shell）；
    塞进表里就得开「这条不查 manage.py 相邻」的例外，例外一开就挡不住「把脚本型误写成命令型」。
    四处与常驻 / payout 同款（漏一处 = 半新半旧）：
    ① 两份单元文件 + 脚本都在，service 是 oneshot、ExecStart 喂的是 `$R/shared/backup_daily.py`；
    ② `deploy.sh` 的 rsync 上传面里有**三个文件**（两个单元 + 脚本——单元起了、脚本不在 = 每天空转失败）；
    ③ `deploy.sh` 的 chmod/md5sum 面**也有三个**；
    ④ `activate.sh` 与 `rollback.sh` 的 `BACKUP_UNITS=(…)` 清单里都有它，且**两份恰好相等**。
    ⚠️ 开关是**独立的** `BACKUP_UNIT_ENABLE`（默认 1 = 两台在役机的现役行为不变）：备份要在
    **每一台**租户机上跑（各自导出自己的库），⛔ 不挂 `SYNC_UNIT_ENABLE`（那是「单实例同步
    只在一台」的开关，挂上去会被 opt-out 的机器顺带停掉备份）。
    """

    _SERVICE = _DEPLOY / "systemd" / "tuantuantong-backup.service"
    _TIMER = _DEPLOY / "systemd" / "tuantuantong-backup.timer"
    _SCRIPT = _DEPLOY / "backup_daily.py"

    def test_the_service_is_oneshot_and_feeds_the_shared_script_to_manage_py_shell(self):
        """① service 是 oneshot、以 ubuntu 跑、把 `$R/shared/backup_daily.py` 喂给 `manage.py shell`。

        失败方向：脚本挪位（ExecStart 指向不存在的路径 ⇒ 每天准点失败一次）、被改成别的跑法
        ⇒ 本用例红。⚠️ 单元里**不许有 `Environment=` 行**（凭据红线：`Environment=` 会落进
        `systemctl show` / `/proc/<pid>/environ`；脚本运行时自己从 `.env` 读 `COS_*` / `POSTGRES_*`）。
        """
        self.assertTrue(self._SERVICE.is_file(), "缺单元文件：deploy/systemd/tuantuantong-backup.service")
        self.assertTrue(self._TIMER.is_file(), "缺定时器文件：deploy/systemd/tuantuantong-backup.timer")
        service_text = _read(self._SERVICE)
        self.assertIn("\nType=oneshot\n", service_text)
        self.assertIn("\nUser=ubuntu\n", service_text)
        exec_start = _exec_start(service_text)
        # 喂给**当前 release** 的 Django shell（current/src + manage.py shell + shared 脚本路径）。
        self.assertIn("current/src", exec_start)
        self.assertIn("manage.py shell", exec_start)
        self.assertIn("/shared/backup_daily.py", exec_start)
        # ⛔ 凭据红线（与 DailiantongUnitsTests::test_…never_carries_the_secret 同一条）。
        self.assertNotIn("\nEnvironment=", service_text)
        timer_text = _read(self._TIMER)
        self.assertIn("Unit=tuantuantong-backup.service", timer_text)
        self.assertIn("OnCalendar=", timer_text)
        # Persistent=true：机器重启 / 停电错过的那次，起来之后补跑（备份链路的承重项）。
        self.assertIn("Persistent=true", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)

    def test_activate_and_rollback_list_the_backup_timer_as_exactly_the_same_set(self):
        """④ 两个脚本的 `BACKUP_UNITS=(…)` 都有它，且**恰好相等**；⛔ 不许混进 `SYNC_SERVICES`。

        判据打在**数组赋值那一行**（`_shell_array` 解析），⛔ 不是「文件里出现过这个词」。
        恰好相等（两份清单互为镜像）与 `InspectRemoteSyncUnitListTests` 同一条哲学：只改一份
        ⇒ 红。另钉「不进 `SYNC_SERVICES`」：混进去会被 `SYNC_UNIT_ENABLE=0` 的机器把备份
        顺带 `disable --now` 掉（备份是每台都要跑的，不是单实例同步）。
        """
        activate_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "activate.sh", "BACKUP_UNITS"
        )
        rollback_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "rollback.sh", "BACKUP_UNITS"
        )
        for script, units in (("activate.sh", activate_units), ("rollback.sh", rollback_units)):
            with self.subTest(script=script):
                self.assertIn(
                    "tuantuantong-backup.timer", units,
                    f"{script} 的 BACKUP_UNITS 清单里缺 tuantuantong-backup.timer"
                    "（⇒ 每日报份不会被安装 / 重启，换机重装即丢）",
                )
        self.assertEqual(
            sorted(activate_units), sorted(rollback_units),
            "activate.sh 与 rollback.sh 的 BACKUP_UNITS 不是恰好集合："
            f"发版={sorted(activate_units)}，回滚={sorted(rollback_units)}"
            "（只改一份 = 发版装了、回滚不重启，半新半旧）",
        )
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(switch=script):
                # 独立开关 + 持久放法（默认 1 = 现役行为不变）必须在场。
                self.assertIn("BACKUP_UNIT_ENABLE", text, f"{script} 缺少 BACKUP_UNIT_ENABLE 开关")
                self.assertIn("$R/shared/backup-unit.env", text, f"{script} 缺少 backup-unit.env 持久放法")
                # ⛔ 不许混进常驻同步清单（会被 SYNC_UNIT_ENABLE=0 的机器顺带停掉）。
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(
                    "tuantuantong-backup", assignments[0],
                    f"{script} 把备份单元塞进了 SYNC_SERVICES（备份每台都要跑，"
                    "不该被 SYNC_UNIT_ENABLE=0 的机器停掉）",
                )

    def test_deploy_dot_sh_uploads_and_checksums_both_units_and_the_script(self):
        """②③ `deploy.sh` 的 rsync 面 + chmod/md5sum 面各有**三个文件**（两单元 + 脚本本体）。

        脚本与单元**同一次**传：单元 ExecStart 指着 `$R/shared/backup_daily.py`，漏传脚本
        ⇒ 单元准点起来、每天失败一次（比不装更吵且无意义）。
        """
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = "\n".join(rsync_lines)
        for required in (
            "deploy/systemd/tuantuantong-backup.service",
            "deploy/systemd/tuantuantong-backup.timer",
            "deploy/backup_daily.py",
        ):
            self.assertIn(
                required, upload_surface,
                f"deploy.sh 的 rsync 源文件清单里缺 {required}（机器上留旧版 / 根本没有）",
            )
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
            for required in (
                "$R/shared/tuantuantong-backup.service",
                "$R/shared/tuantuantong-backup.timer",
                "$R/shared/backup_daily.py",
            ):
                self.assertIn(
                    required, face,
                    f"deploy.sh 的 {face_name} 面里缺 {required}",
                )

    def test_inspect_remote_probes_the_backup_timer_and_the_last_ok(self):
        """巡检（`inspect_remote.sh`）必须探这个 timer 的判活 + 最近一次 BACKUP-OK（权威判据）。

        2026-10-29 之前巡检的备份新鲜度查的是 **Mac 手工备份目录**（`deploy/inspect.sh`）⇒
        手工没跑就误报「备份过期」，而服务器侧其实每天成功。判据改挂**服务器侧**后，这一条
        钉住「检查节还在、判的是这个 timer + BACKUP-OK」（⛔ 文本断言，证明力有限——
        保证「节没被顺手删」，不证明体检在真机上跑过；与 payout 那条同口径）。
        """
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "BACKUP_TIMER=tuantuantong-backup.timer", text,
            "inspect_remote.sh 里没有每日报份 timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$BACKUP_TIMER"', text,
            "inspect_remote.sh 没有对每日报份 timer 做 is-active 判活",
        )
        self.assertIn(
            "BACKUP-OK", text,
            "inspect_remote.sh 没有「最近一次 BACKUP-OK」的新鲜度探针（只有 is-active "
            "发现不了「timer 在转但每晚都失败」）",
        )
        # Mac 端入口必须已把权威判据让位给服务器侧（辅助渠道只报信息、不进 ISSUES 计数）。
        mac = _read(_DEPLOY / "inspect.sh")
        self.assertNotIn(
            "warn \"最新备份", mac,
            "inspect.sh 还在把 Mac 手工备份当新鲜度判据（会误报：手工没跑 ≠ 备份停了）",
        )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class LedgerPruneTimerUnitTests(SimpleTestCase):
    """**同步台账清理**（`tuantuantong-ledger-prune.service/.timer`，D-0349）的四处接线。

    ⚠️ 与 backup 同族但**单独一类、不进** `_TIMER_COMMANDS`：那张表的④钉的是
    `PAYOUT_UNITS` 清单，本单元用的是**自己的** `LEDGER_PRUNE_UNITS`（每台都跑的
    独立开关 `LEDGER_PRUNE_UNIT_ENABLE`，默认 1——台账清理与备份一样是「每台清本机」，
    ⛔ 不挂 `SYNC_UNIT_ENABLE`、不进 `SYNC_SERVICES`）。
    四处与常驻 / payout / backup 同款（漏一处 = 半新半旧）：
    ① 两份单元文件都在，service 是 oneshot、ExecStart = `manage.py prune_sync_events --commit`
      （⭐ **`--commit` 是承重参数**：命令缺省 dry-run ⇒ 漏了它 = timer 每天跑、一行不删）；
    ② `deploy.sh` 的 rsync 上传面里有**两个文件**；
    ③ `deploy.sh` 的 chmod/md5sum 面**也有两个**；
    ④ `activate.sh` 与 `rollback.sh` 的 `LEDGER_PRUNE_UNITS=(…)` 清单里都有它，且**两份恰好相等**。
    """

    _SERVICE = _DEPLOY / "systemd" / "tuantuantong-ledger-prune.service"
    _TIMER = _DEPLOY / "systemd" / "tuantuantong-ledger-prune.timer"

    @staticmethod
    def _argv(path: Path) -> list[str]:
        return shlex.split(_exec_start(_read(path)))

    def test_the_service_is_oneshot_and_runs_the_prune_command_with_commit(self):
        """① service 是 oneshot、以 ubuntu 跑、ExecStart = `manage.py prune_sync_events --commit`。

        失败方向：漏 `--commit`（每天 dry-run 空转）、被写成 `--loop`（常驻化，与 timer
        叠成两种调度）、命令名写错 ⇒ 本用例红。⚠️ 单元里**不许有含密钥的 `Environment=`
        行**（凭据红线，与 `DailiantongUnitsTests` 同一条；`PYTHONUNBUFFERED` 这类非密钥
        变量不在此限）。
        """
        self.assertTrue(
            self._SERVICE.is_file(),
            "缺单元文件：deploy/systemd/tuantuantong-ledger-prune.service",
        )
        self.assertTrue(
            self._TIMER.is_file(),
            "缺定时器文件：deploy/systemd/tuantuantong-ledger-prune.timer",
        )
        service_text = _read(self._SERVICE)
        self.assertIn("\nType=oneshot\n", service_text)
        self.assertIn("\nUser=ubuntu\n", service_text)
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            service_text,
        )
        self.assertEqual(
            self._argv(self._SERVICE),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "prune_sync_events",
                "--commit",
            ],
        )
        self.assertNotIn(
            "--loop", self._argv(self._SERVICE),
            "台账清理是 oneshot，ExecStart 里不应有 --loop",
        )
        for line in service_text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_timer_fires_daily_at_0330_and_is_persistent(self):
        """timer 每天 **03:30** 触发、`Persistent=true`（错过的补跑一次，命令幂等）、
        指名本 service；且**不与每日报份的 04:40 相撞**（D-0349 点名错峰）。"""
        timer_text = _read(self._TIMER)
        self.assertIn("Unit=tuantuantong-ledger-prune.service", timer_text)
        oncalendar = [
            line for line in timer_text.splitlines() if line.startswith("OnCalendar=")
        ]
        self.assertEqual(
            oncalendar, ["OnCalendar=*-*-* 03:30:00"],
            f"台账清理 timer 的触发时刻应为每天 03:30，实际：{oncalendar}",
        )
        self.assertIn("Persistent=true", timer_text)
        self.assertIn("RandomizedDelaySec=", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)
        # ⛔ 触发配置里不许出现 04:40（D-0349 点名 03:30 错峰）——判据打在**非注释行**上：
        #    注释里说明「为什么错开 04:40」是承重上下文，不算排程（E-0161 同族：打在标记上，
        #    ⛔ 不是「文件文本」）。
        active_lines = "\n".join(
            line for line in timer_text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "04:40", active_lines,
            "台账清理 timer 的有效配置里不应出现 04:40（D-0349 点名 03:30 错峰）",
        )

    def test_activate_and_rollback_list_the_ledger_prune_timer_as_exactly_the_same_set(self):
        """④ 两个脚本的 `LEDGER_PRUNE_UNITS=(…)` 都有它，且**恰好相等**；⛔ 不许混进
        `SYNC_SERVICES`；独立开关 + 持久放法必须在场。"""
        activate_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "activate.sh", "LEDGER_PRUNE_UNITS"
        )
        rollback_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "rollback.sh", "LEDGER_PRUNE_UNITS"
        )
        for script, units in (("activate.sh", activate_units), ("rollback.sh", rollback_units)):
            with self.subTest(script=script):
                self.assertIn(
                    "tuantuantong-ledger-prune.timer", units,
                    f"{script} 的 LEDGER_PRUNE_UNITS 清单里缺 tuantuantong-ledger-prune.timer"
                    "（⇒ 台账清理不会被安装 / 回滚不重启，半新半旧）",
                )
        self.assertEqual(
            sorted(activate_units), sorted(rollback_units),
            "activate.sh 与 rollback.sh 的 LEDGER_PRUNE_UNITS 不是恰好集合："
            f"发版={sorted(activate_units)}，回滚={sorted(rollback_units)}",
        )
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(switch=script):
                self.assertIn("LEDGER_PRUNE_UNIT_ENABLE", text, f"{script} 缺少开关")
                self.assertIn(
                    "$R/shared/ledger-prune-unit.env", text,
                    f"{script} 缺少 ledger-prune-unit.env 持久放法",
                )
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(
                    "tuantuantong-ledger-prune", assignments[0],
                    f"{script} 把台账清理单元塞进了 SYNC_SERVICES（它每台都要跑，"
                    "不该被 SYNC_UNIT_ENABLE=0 的机器停掉）",
                )

    def test_deploy_dot_sh_uploads_and_checksums_both_units(self):
        """②③ `deploy.sh` 的 rsync 面 + chmod/md5sum 面各有**两个文件**（⚠️ chmod 与
        md5sum 是两条并列清单，只断一面正是 E-0097 那种「漏一处」）。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = "\n".join(rsync_lines)
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for unit_file in (
            "tuantuantong-ledger-prune.service",
            "tuantuantong-ledger-prune.timer",
        ):
            with self.subTest(unit_file=unit_file):
                self.assertIn(
                    f"deploy/systemd/{unit_file}", upload_surface,
                    f"deploy.sh 的 rsync 源文件清单里缺 deploy/systemd/{unit_file}",
                )
                for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
                    self.assertIn(
                        f"$R/shared/{unit_file}", face,
                        f"deploy.sh 的 {face_name} 面里缺 $R/shared/{unit_file}",
                    )

    def test_inspect_remote_probes_the_ledger_prune_timer(self):
        """巡检必须探这个 timer 的判活（台账清理停了不吵不闹——表只是继续涨，等 DB CPU
        又告警才发现就晚了）。⚠️ 文本断言，证明力有限：保证「检查节还在、判的是这个
        timer」，与 payout / backup 那两条同口径。"""
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "LEDGER_PRUNE_TIMER=tuantuantong-ledger-prune.timer", text,
            "inspect_remote.sh 里没有台账清理 timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$LEDGER_PRUNE_TIMER"', text,
            "inspect_remote.sh 没有对台账清理 timer 做 is-active 判活",
        )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class OrderQrFetchTimerUnitTests(SimpleTestCase):
    """**马上来查码队列**（`tuantuantong-order-qr-fetch.service/.timer`，D-0356）的四处接线。

    ⚠️ 与 ledger-prune 同族但**单独一类**：本单元用**自己的** `ORDER_QR_FETCH_UNITS`
    （每台都跑的独立开关 `ORDER_QR_FETCH_UNIT_ENABLE`，默认 1——队列每台各扫**本机**
    租户，租户库物理分离、多台同跑互不相干，⛔ 不挂 `SYNC_UNIT_ENABLE`、不进
    `SYNC_SERVICES`）。
    四处与 ledger-prune / payout / backup 同款（漏一处 = 半新半旧）：
    ① 两份单元文件都在，service 是 oneshot、ExecStart = `manage.py fetch_order_qr_queue`
      （⭐ **不带任何行为闸**：命令缺省就是干活——它不删东西，最重动作是打对端，
      受批大小与租户配置闸双限；timer 触发在跑中会合并进在跑那次，不并发）；
    ② `deploy.sh` 的 rsync 上传面里有**两个文件**；
    ③ `deploy.sh` 的 chmod/md5sum 面**也有两个**；
    ④ `activate.sh` 与 `rollback.sh` 的 `ORDER_QR_FETCH_UNITS=(…)` 清单里都有它，
      且**两份恰好相等**。
    ⑤ 巡检（`inspect_remote.sh`）必须探这个 timer 的判活（查码队列停了不吵不闹——
      缺群码订单静默回到「只靠人带动」，等用户反馈才发现就晚了）。
    """

    _SERVICE = _DEPLOY / "systemd" / "tuantuantong-order-qr-fetch.service"
    _TIMER = _DEPLOY / "systemd" / "tuantuantong-order-qr-fetch.timer"

    @staticmethod
    def _argv(path: Path) -> list[str]:
        return shlex.split(_exec_start(_read(path)))

    def test_the_service_is_oneshot_and_runs_the_queue_command(self):
        """① service 是 oneshot、以 ubuntu 跑、ExecStart = `manage.py fetch_order_qr_queue`。

        失败方向：被写成 `--loop`（常驻化，与 timer 叠成两种调度）、命令名写错、
        被错加 `--commit` 之类的闸（本命令没有 dry-run 缺省）⇒ 本用例红。⚠️ 单元里
        **不许有含密钥的 `Environment=` 行**（凭据红线；`PYTHONUNBUFFERED` 不在此限）。
        """
        self.assertTrue(
            self._SERVICE.is_file(),
            "缺单元文件：deploy/systemd/tuantuantong-order-qr-fetch.service",
        )
        self.assertTrue(
            self._TIMER.is_file(),
            "缺定时器文件：deploy/systemd/tuantuantong-order-qr-fetch.timer",
        )
        service_text = _read(self._SERVICE)
        self.assertIn("\nType=oneshot\n", service_text)
        self.assertIn("\nUser=ubuntu\n", service_text)
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            service_text,
        )
        self.assertEqual(
            self._argv(self._SERVICE),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "fetch_order_qr_queue",
            ],
        )
        self.assertNotIn(
            "--loop", self._argv(self._SERVICE),
            "查码队列是 oneshot，ExecStart 里不应有 --loop",
        )
        # 低优先级（照 payout 同款）：非重活，别抢应用进程的 CPU。
        self.assertIn("Nice=10", service_text)
        for line in service_text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_timer_fires_every_minute_and_is_persistent(self):
        """timer **每分钟**触发（`*:0/1`）、`Persistent=true`（错过的补跑一次，命令幂等）、
        指名本 service。"""
        timer_text = _read(self._TIMER)
        self.assertIn("Unit=tuantuantong-order-qr-fetch.service", timer_text)
        oncalendar = [
            line for line in timer_text.splitlines() if line.startswith("OnCalendar=")
        ]
        self.assertEqual(
            oncalendar, ["OnCalendar=*:0/1"],
            f"查码队列 timer 应每分钟触发，实际：{oncalendar}",
        )
        self.assertIn("Persistent=true", timer_text)
        # 允许 systemd 把同一刻的触发合并进这个窗口（省一次唤醒）。
        self.assertIn("AccuracySec=1min", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)

    def test_activate_and_rollback_list_the_order_qr_fetch_timer_as_exactly_the_same_set(self):
        """④ 两个脚本的 `ORDER_QR_FETCH_UNITS=(…)` 都有它，且**恰好相等**；⛔ 不许混进
        `SYNC_SERVICES`；独立开关 + 持久放法必须在场。"""
        activate_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "activate.sh", "ORDER_QR_FETCH_UNITS"
        )
        rollback_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "rollback.sh", "ORDER_QR_FETCH_UNITS"
        )
        for script, units in (("activate.sh", activate_units), ("rollback.sh", rollback_units)):
            with self.subTest(script=script):
                self.assertIn(
                    "tuantuantong-order-qr-fetch.timer", units,
                    f"{script} 的 ORDER_QR_FETCH_UNITS 清单里缺 tuantuantong-order-qr-fetch.timer"
                    "（⇒ 查码队列不会被安装 / 回滚不重启，半新半旧）",
                )
        self.assertEqual(
            sorted(activate_units), sorted(rollback_units),
            "activate.sh 与 rollback.sh 的 ORDER_QR_FETCH_UNITS 不是恰好集合："
            f"发版={sorted(activate_units)}，回滚={sorted(rollback_units)}",
        )
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(switch=script):
                self.assertIn("ORDER_QR_FETCH_UNIT_ENABLE", text, f"{script} 缺少开关")
                self.assertIn(
                    "$R/shared/order-qr-fetch-unit.env", text,
                    f"{script} 缺少 order-qr-fetch-unit.env 持久放法",
                )
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(
                    "tuantuantong-order-qr-fetch", assignments[0],
                    f"{script} 把查码队列单元塞进了 SYNC_SERVICES（它每台都要跑、各扫本机"
                    "租户，不该被 SYNC_UNIT_ENABLE=0 的机器停掉）",
                )

    def test_deploy_dot_sh_uploads_and_checksums_both_units(self):
        """②③ `deploy.sh` 的 rsync 面 + chmod/md5sum 面各有**两个文件**（⚠️ chmod 与
        md5sum 是两条并列清单，只断一面正是「漏一处」的形态）。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = "\n".join(rsync_lines)
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for unit_file in (
            "tuantuantong-order-qr-fetch.service",
            "tuantuantong-order-qr-fetch.timer",
        ):
            with self.subTest(unit_file=unit_file):
                self.assertIn(
                    f"deploy/systemd/{unit_file}", upload_surface,
                    f"deploy.sh 的 rsync 源文件清单里缺 deploy/systemd/{unit_file}",
                )
                for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
                    self.assertIn(
                        f"$R/shared/{unit_file}", face,
                        f"deploy.sh 的 {face_name} 面里缺 $R/shared/{unit_file}",
                    )

    def test_inspect_remote_probes_the_order_qr_fetch_timer(self):
        """巡检必须探这个 timer 的判活。⚠️ 文本断言，证明力有限：保证「检查节还在、
        判的是这个 timer」，与 ledger-prune / payout / backup 那几条同口径。"""
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "ORDER_QR_FETCH_TIMER=tuantuantong-order-qr-fetch.timer", text,
            "inspect_remote.sh 里没有查码队列 timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$ORDER_QR_FETCH_TIMER"', text,
            "inspect_remote.sh 没有对查码队列 timer 做 is-active 判活",
        )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class ReceiptSyncTimerUnitTests(SimpleTestCase):
    """**上家收款同步**（`tuantuantong-receipt-sync.service/.timer`，D-0359）的四处接线。

    ⚠️ 与 ledger-prune / order-qr-fetch 同族但**单独一类、不进** `_TIMER_COMMANDS`：
    本单元用的是**自己的** `RECEIPT_SYNC_UNITS`（每台都跑的独立开关
    `RECEIPT_SYNC_TIMER_ENABLE`，默认 1——收款同步每台各扫**本机**租户，租户库物理分离、
    多台同跑互不相干，⛔ 不挂 `SYNC_UNIT_ENABLE`、不进 `SYNC_SERVICES`）。
    四处与 qr-fetch 同款（漏一处 = 半新半旧）：
    ① 两份单元文件都在，service 是 oneshot、ExecStart = `manage.py sync_upstream_receipts`
      （⭐ **不带任何行为闸**：命令缺省就是干活——最重动作是打上家，受 1.5s 限速、
      每通道 5000 单与 1 小时预算三限）；
    ② `deploy.sh` 的 rsync 上传面里有**两个文件**；
    ③ `deploy.sh` 的 chmod/md5sum 面**也有两个**；
    ④ `activate.sh` 与 `rollback.sh` 的 `RECEIPT_SYNC_UNITS=(…)` 清单里都有它，
      且**两份恰好相等**；
    ⑤ 巡检（`inspect_remote.sh`）必须探这个 timer 的判活（收款同步停了不吵不闹——
      收款回到「只靠人工点」，等业务方发现就晚了）。
    """

    _SERVICE = _DEPLOY / "systemd" / "tuantuantong-receipt-sync.service"
    _TIMER = _DEPLOY / "systemd" / "tuantuantong-receipt-sync.timer"

    @staticmethod
    def _argv(path: Path) -> list[str]:
        return shlex.split(_exec_start(_read(path)))

    def test_the_service_is_oneshot_and_runs_the_nightly_command(self):
        """① service 是 oneshot、以 ubuntu 跑、ExecStart = `manage.py sync_upstream_receipts`。

        失败方向：被写成 `--loop`（常驻化，与 timer 叠成两种调度）、命令名写错、
        被错加 `--commit` 之类的闸（本命令没有 dry-run 缺省）⇒ 本用例红。⚠️ 单元里
        **不许有含密钥的 `Environment=` 行**（凭据红线；`PYTHONUNBUFFERED` 不在此限）。
        """
        self.assertTrue(
            self._SERVICE.is_file(),
            "缺单元文件：deploy/systemd/tuantuantong-receipt-sync.service",
        )
        self.assertTrue(
            self._TIMER.is_file(),
            "缺定时器文件：deploy/systemd/tuantuantong-receipt-sync.timer",
        )
        service_text = _read(self._SERVICE)
        self.assertIn("\nType=oneshot\n", service_text)
        self.assertIn("\nUser=ubuntu\n", service_text)
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            service_text,
        )
        self.assertEqual(
            self._argv(self._SERVICE),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "sync_upstream_receipts",
            ],
        )
        self.assertNotIn(
            "--loop", self._argv(self._SERVICE),
            "上家收款同步是 oneshot，ExecStart 里不应有 --loop",
        )
        # 低优先级（照 payout / qr-fetch 同款）：非重活，别抢应用进程的 CPU。
        self.assertIn("Nice=10", service_text)
        for line in service_text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_timer_fires_daily_at_0200_and_is_persistent(self):
        """timer 每天 **02:00** 触发、`Persistent=true`（错过的补跑一次，命令幂等）、
        指名本 service；且**不与台账清理 03:30 / 每日报份 04:40 相撞**（D-0359 点名错峰）。"""
        timer_text = _read(self._TIMER)
        self.assertIn("Unit=tuantuantong-receipt-sync.service", timer_text)
        oncalendar = [
            line for line in timer_text.splitlines() if line.startswith("OnCalendar=")
        ]
        self.assertEqual(
            oncalendar, ["OnCalendar=*-*-* 02:00:00"],
            f"上家收款同步 timer 的触发时刻应为每天 02:00，实际：{oncalendar}",
        )
        self.assertIn("Persistent=true", timer_text)
        self.assertIn("RandomizedDelaySec=", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)
        # ⛔ 触发配置里不许出现 03:30 / 04:40（D-0359 点名 02:00 错峰）——判据打在
        #    **非注释行**上（E-0161 同族：打在标记上，⛔ 不是「文件文本」）。
        active_lines = "\n".join(
            line for line in timer_text.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in ("03:30", "04:40"):
            self.assertNotIn(
                forbidden, active_lines,
                f"上家收款同步 timer 的有效配置里不应出现 {forbidden}（D-0359 点名 02:00 错峰）",
            )

    def test_activate_and_rollback_list_the_receipt_sync_timer_as_exactly_the_same_set(self):
        """④ 两个脚本的 `RECEIPT_SYNC_UNITS=(…)` 都有它，且**恰好相等**；⛔ 不许混进
        `SYNC_SERVICES`；独立开关 + 持久放法必须在场。"""
        activate_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "activate.sh", "RECEIPT_SYNC_UNITS"
        )
        rollback_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "rollback.sh", "RECEIPT_SYNC_UNITS"
        )
        for script, units in (("activate.sh", activate_units), ("rollback.sh", rollback_units)):
            with self.subTest(script=script):
                self.assertIn(
                    "tuantuantong-receipt-sync.timer", units,
                    f"{script} 的 RECEIPT_SYNC_UNITS 清单里缺 tuantuantong-receipt-sync.timer"
                    "（⇒ 上家收款同步不会被安装 / 回滚不重启，半新半旧）",
                )
        self.assertEqual(
            sorted(activate_units), sorted(rollback_units),
            "activate.sh 与 rollback.sh 的 RECEIPT_SYNC_UNITS 不是恰好集合："
            f"发版={sorted(activate_units)}，回滚={sorted(rollback_units)}",
        )
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(script=script):
                self.assertIn("RECEIPT_SYNC_TIMER_ENABLE", text, f"{script} 缺少开关")
                self.assertIn(
                    "$R/shared/receipt-sync-unit.env", text,
                    f"{script} 缺少 receipt-sync-unit.env 持久放法",
                )
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(
                    "tuantuantong-receipt-sync", assignments[0],
                    f"{script} 把上家收款同步单元塞进了 SYNC_SERVICES（它每台都要跑、各扫本机"
                    "租户，不该被 SYNC_UNIT_ENABLE=0 的机器停掉）",
                )

    def test_deploy_dot_sh_uploads_and_checksums_both_units(self):
        """②③ `deploy.sh` 的 rsync 面 + chmod/md5sum 面各有**两个文件**（⚠️ chmod 与
        md5sum 是两条并列清单，只断一面正是「漏一处」的形态）。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = "\n".join(rsync_lines)
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for unit_file in (
            "tuantuantong-receipt-sync.service",
            "tuantuantong-receipt-sync.timer",
        ):
            with self.subTest(unit_file=unit_file):
                self.assertIn(
                    f"deploy/systemd/{unit_file}", upload_surface,
                    f"deploy.sh 的 rsync 源文件清单里缺 deploy/systemd/{unit_file}",
                )
                for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
                    self.assertIn(
                        f"$R/shared/{unit_file}", face,
                        f"deploy.sh 的 {face_name} 面里缺 $R/shared/{unit_file}",
                    )

    def test_inspect_remote_probes_the_receipt_sync_timer(self):
        """巡检必须探这个 timer 的判活。⚠️ 文本断言，证明力有限：保证「检查节还在、
        判的是这个 timer」，与 ledger-prune / qr-fetch 那几条同口径。"""
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "RECEIPT_SYNC_TIMER=tuantuantong-receipt-sync.timer", text,
            "inspect_remote.sh 里没有上家收款同步 timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$RECEIPT_SYNC_TIMER"', text,
            "inspect_remote.sh 没有对上家收款同步 timer 做 is-active 判活",
        )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class InspectRemoteSyncUnitListTests(SimpleTestCase):
    """发版和巡检使用完全相同的常驻同步清单。"""

    @staticmethod
    def _shell_array(path: Path, name: str) -> list[str]:
        """从 shell 脚本里解出 `NAME=(a b c)` 的元素（⛔ 不用 source / 执行脚本）。"""
        assignments = [
            line for line in _logical_lines(_read(path))
            if line.lstrip().startswith(f"{name}=(")
        ]
        if len(assignments) != 1:
            raise AssertionError(
                f"{path.name} 里 `{name}=(…)` 恰好应有一处，实际 {len(assignments)} 处"
            )
        line = assignments[0]
        prefix = f"{name}=("
        start = line.index(prefix) + len(prefix)
        end = line.index(")", start)
        return shlex.split(line[start:end])

    def test_inspect_remote_and_activate_list_exactly_the_same_sync_units(self):
        inspect_units = self._shell_array(_DEPLOY / "inspect_remote.sh", "SYNC_SERVICES")
        activate_units = self._shell_array(_DEPLOY / "activate.sh", "SYNC_SERVICES")
        self.assertEqual(inspect_units, activate_units)
        self.assertEqual(len(inspect_units), len(set(inspect_units)))

    def test_inspect_remote_checks_the_payout_timer_is_active(self):
        """`payout.timer` 的 `is-active` 检查节必须在（Z16 的另一半）。

        ⚠️ 这条断的是**脚本文本**（证明力有限）：保证「检查节还在、判的是这个 timer」，
        不证明体检在真机上跑过。失败方向：把这一节删掉 / 把 `is-active` 换成只打印
        ⇒ 提现自动代付停了巡检又回到不吭声的老样子。
        """
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "PAYOUT_TIMER=tuantuantong-payout.timer", text,
            "inspect_remote.sh 里没有 payout timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$PAYOUT_TIMER"', text,
            "inspect_remote.sh 没有对 payout timer 做 is-active 判活",
        )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class JidanpuOutboxUnitTests(SimpleTestCase):
    """鸡蛋铺六台常驻单元的契约（拆进程形态：轮询 / 出向 / ②式回调 / 快路 / 回拉+对账 / 上家线）。

    ⚠️ 这条网抓的是**静默全停**：六个单元如果都写成同一串开关（或主单元漏了某个 `--skip-*`），
    `activate.sh` 的 `is-active` 仍然会过、巡检也看不出——但出向事件 / ②式回调 / 快路 /
    状态回拉 + 对账 / 上家线从此**没人跑**，推单/指派/退单全部停在台账里、远端状态永远不收敛。
    所以这里断的是「**六个单元互补**」，不是单看某一行。
    """

    _OUTBOX = _DEPLOY / "systemd" / "tuantuantong-jidanpu-outbox.service"
    _SYNC = _DEPLOY / "systemd" / "tuantuantong-jidanpu-sync.service"
    _CALLBACK = _DEPLOY / "systemd" / "tuantuantong-jidanpu-callback.service"
    _FASTPATH = _DEPLOY / "systemd" / "tuantuantong-jidanpu-fastpath.service"
    _RECONCILE = _DEPLOY / "systemd" / "tuantuantong-jidanpu-reconcile.service"
    _UPSTREAM = _DEPLOY / "systemd" / "tuantuantong-jidanpu-upstream.service"

    @staticmethod
    def _argv(path: Path) -> list[str]:
        return shlex.split(_exec_start(_read(path)))

    def test_the_outbox_unit_runs_only_the_outbox_loop(self):
        """`--loop --outbox-only` 逐段钉住：少 `--outbox-only` ⇒ 两个进程都跑全套（重复消费）。"""
        self.assertEqual(
            self._argv(self._OUTBOX),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--outbox-only",
            ],
        )

    def test_the_sync_unit_skips_the_outbox_the_callback_the_fastpath_the_reconcile_and_the_upstream(self):
        """同步/轮询单元必须五个 skip 都写（否则与出向 / 回调 / 快路 / 对账 / 上家单元重复执行同一段）。"""
        self.assertEqual(
            self._argv(self._SYNC),
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

    def test_the_callback_unit_runs_only_the_openapi_segment(self):
        """②式回调单元 `--loop --callback-only` 逐段钉住（它含出向发单，见单元注释）。"""
        self.assertEqual(
            self._argv(self._CALLBACK),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--callback-only",
            ],
        )

    def test_the_fastpath_unit_runs_only_the_heal_and_log_fast_path(self):
        """①式快路单元 `--loop --fastpath-only` 逐段钉住。

        ⚠️ 它**必须含身份自愈**（`heal_binding_identity` 不在别处跑：主单元已 `--skip-fastpath`）
        —— 那一层由 `test_jidanpu_sync_outbox_switch.py` 的行为用例钉住（本条只管 argv）。
        """
        self.assertEqual(
            self._argv(self._FASTPATH),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--fastpath-only",
            ],
        )

    def test_the_reconcile_unit_runs_only_the_pull_and_reconcile_segments(self):
        """①式回拉 + 对账单元 `--loop --reconcile-only` 逐段钉住。

        ⚠️ 两段**必须同一个单元**（共用 `reconcile_seconds` 闸门；拆成两台 = `. 各算一次闸门、
        重复回拉 + 重复排事件）——那一条由 `test_jidanpu_sync_outbox_switch.py` 的行为用例钉住
        （本条只管 argv）。
        """
        self.assertEqual(
            self._argv(self._RECONCILE),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--reconcile-only",
            ],
        )

    def test_the_upstream_unit_runs_only_the_whole_upstream_row(self):
        """上家单元 `--loop --upstream-only` 逐段钉住。

        ⚠️ **整行不可再拆**：健康合成判据与整行连续失败退避都是行级的——
        按「大厅 / 回拉 / 对账」拆成多台会让每台各算一次。那条由
        `test_jidanpu_sync_outbox_switch.py` 的 `UpstreamOnlyTests`（整行三段都跑、下家零调用）
        与单元注释钉住（本条只管 argv）。
        """
        self.assertEqual(
            self._argv(self._UPSTREAM),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "jidanpu_sync",
                "--loop",
                "--upstream-only",
            ],
        )

    def test_the_six_units_are_complementary(self):
        """恰好一台跑出向、一台跑②式、一台跑快路、一台跑回拉+对账、一台跑上家整行，
        主单元五个 skip 都写；任一开关落两次都要红。

        失败方向：把两个单元都改成 `--skip-outbox`（或都 `--outbox-only`）⇒ 出向没人跑；
        主单元漏 `--skip-callback` ⇒ 与回调单元重复消费回调；
        主单元漏 `--skip-fastpath` ⇒ 与快路单元重复跑（heal 会**重复自愈**）；
        主单元漏 `--skip-reconcile` ⇒ 与对账单元重复回拉 / 重复排事件；
        主单元漏 `--skip-upstream` ⇒ 与上家单元同时跑整行（重复打上家、行级健康/退避各算一次）
        ⇒ 本用例红。
        """
        outbox_argv = self._argv(self._OUTBOX)
        sync_argv = self._argv(self._SYNC)
        callback_argv = self._argv(self._CALLBACK)
        fastpath_argv = self._argv(self._FASTPATH)
        reconcile_argv = self._argv(self._RECONCILE)
        upstream_argv = self._argv(self._UPSTREAM)
        all_argv = (
            outbox_argv, sync_argv, callback_argv, fastpath_argv, reconcile_argv,
            upstream_argv,
        )
        for flag in (
            "--outbox-only", "--skip-outbox", "--callback-only", "--skip-callback",
            "--fastpath-only", "--skip-fastpath",
            "--reconcile-only", "--skip-reconcile",
            "--upstream-only", "--skip-upstream",
        ):
            with self.subTest(flag=flag):
                self.assertEqual(
                    sum(flag in argv for argv in all_argv), 1,
                    f"{flag} 必须恰好出现在六个单元之一里",
                )
        self.assertIn("--outbox-only", outbox_argv)
        self.assertNotIn("--skip-outbox", outbox_argv)
        self.assertNotIn("--callback-only", outbox_argv)
        self.assertNotIn("--skip-callback", outbox_argv)
        self.assertNotIn("--fastpath-only", outbox_argv)
        self.assertNotIn("--skip-fastpath", outbox_argv)
        self.assertNotIn("--reconcile-only", outbox_argv)
        self.assertNotIn("--skip-reconcile", outbox_argv)
        self.assertIn("--skip-outbox", sync_argv)
        self.assertIn("--skip-callback", sync_argv)
        self.assertIn("--skip-fastpath", sync_argv)
        self.assertIn("--skip-reconcile", sync_argv)
        self.assertNotIn("--outbox-only", sync_argv)
        self.assertNotIn("--callback-only", sync_argv)
        self.assertNotIn("--fastpath-only", sync_argv)
        self.assertNotIn("--reconcile-only", sync_argv)
        self.assertIn("--callback-only", callback_argv)
        self.assertNotIn("--skip-callback", callback_argv)
        self.assertNotIn("--outbox-only", callback_argv)
        self.assertNotIn("--skip-outbox", callback_argv)
        self.assertNotIn("--fastpath-only", callback_argv)
        self.assertNotIn("--skip-fastpath", callback_argv)
        self.assertNotIn("--reconcile-only", callback_argv)
        self.assertNotIn("--skip-reconcile", callback_argv)
        self.assertIn("--fastpath-only", fastpath_argv)
        self.assertNotIn("--skip-fastpath", fastpath_argv)
        self.assertNotIn("--outbox-only", fastpath_argv)
        self.assertNotIn("--skip-outbox", fastpath_argv)
        self.assertNotIn("--callback-only", fastpath_argv)
        self.assertNotIn("--skip-callback", fastpath_argv)
        self.assertNotIn("--reconcile-only", fastpath_argv)
        self.assertNotIn("--skip-reconcile", fastpath_argv)
        self.assertIn("--reconcile-only", reconcile_argv)
        self.assertNotIn("--skip-reconcile", reconcile_argv)
        self.assertNotIn("--outbox-only", reconcile_argv)
        self.assertNotIn("--skip-outbox", reconcile_argv)
        self.assertNotIn("--callback-only", reconcile_argv)
        self.assertNotIn("--skip-callback", reconcile_argv)
        self.assertNotIn("--fastpath-only", reconcile_argv)
        self.assertNotIn("--skip-fastpath", reconcile_argv)
        self.assertIn("--upstream-only", upstream_argv)
        self.assertNotIn("--skip-upstream", upstream_argv)
        self.assertNotIn("--outbox-only", upstream_argv)
        self.assertNotIn("--skip-outbox", upstream_argv)
        self.assertNotIn("--callback-only", upstream_argv)
        self.assertNotIn("--skip-callback", upstream_argv)
        self.assertNotIn("--fastpath-only", upstream_argv)
        self.assertNotIn("--skip-fastpath", upstream_argv)
        self.assertNotIn("--reconcile-only", upstream_argv)
        self.assertNotIn("--skip-reconcile", upstream_argv)
        self.assertIn("--skip-upstream", sync_argv)
        self.assertNotIn("--upstream-only", sync_argv)

    def test_the_split_units_always_restart_without_rate_limit(self):
        """拆出来的单元 + 后来新增的同步单元，死了必须被拉起来，且不许被启动限速永久放弃。"""
        for path in (
            self._OUTBOX, self._CALLBACK, self._FASTPATH, self._RECONCILE,
            self._UPSTREAM,
            # T9c：代练通状态同步（不在鸡蛋铺那六台里，但同属「常驻 --loop」形态）
            _DEPLOY / "systemd" / "tuantuantong-dailiantong-sync.service",
        ):
            text = _read(path)
            with self.subTest(unit=path.name):
                self.assertIn("\nRestart=always\n", text)
                self.assertIn("\nStartLimitIntervalSec=0\n", text)
                self.assertIn(
                    "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src", text
                )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class DailiantongUnitsTests(SimpleTestCase):
    """T9c 两台新单元：状态同步（`dailiantong_sync --loop`）+ 回调接收器（8003）。

    ⚠️ 回调接收器**不在** `SYNC_SERVICES` 里（它是公网入口服务，不是租户数据同步），
    所以它不走 `SyncUnitsAreInstalledWithEveryReleaseTests` 那张表；它的四处接线由本类单独钉。
    """

    _SYNC = _DEPLOY / "systemd" / "tuantuantong-dailiantong-sync.service"
    _CALLBACK = _DEPLOY / "systemd" / "tuantuantong-dailiantong-callback.service"

    @staticmethod
    def _argv(path: Path) -> list[str]:
        return shlex.split(_exec_start(_read(path)))

    def test_the_sync_unit_starts_the_dailiantong_loop_with_the_release_venv_python(self):
        """状态同步单元：`current/src` 的 venv python + `manage.py dailiantong_sync --loop`。"""
        self.assertEqual(
            self._argv(self._SYNC),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "dailiantong_sync",
                "--loop",
            ],
        )

    def test_the_callback_unit_starts_the_shared_receiver_script(self):
        """回调接收器单元：跑 `$R/shared/callback/` 里那份标准库脚本（⛔ 不是 Django wsgi）。"""
        self.assertEqual(
            self._argv(self._CALLBACK),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "/home/ubuntu/projects/tuantuantong/shared/callback/dailiantong_receiver.py",
            ],
        )
        text = _read(self._CALLBACK)
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src", text
        )
        self.assertIn("\nRestart=always\n", text)
        self.assertIn("\nStartLimitIntervalSec=0\n", text)
        self.assertIn("\nUser=ubuntu\n", text)
        self.assertIn("\nTimeoutStopSec=15\n", text)
        self.assertNotIn("OnCalendar=", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_the_callback_unit_never_carries_the_secret_in_its_environment(self):
        """⛔ 单元里不许出现密钥变量（`Environment=` 会落进 `systemctl show` / `/proc/<pid>/environ`）。"""
        text = _read(self._CALLBACK)
        for line in text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_callback_switch_is_independent_and_defaults_to_off(self):
        """`DAILIANTONG_CALLBACK_UNIT_ENABLE` 与鸡蛋铺的 `CALLBACK_UNIT_ENABLE` 各自独立、默认 0。"""
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(script=script):
                self.assertIn("DAILIANTONG_CALLBACK_UNIT_ENABLE", text)
                self.assertIn("$R/shared/dailiantong-callback-unit.env", text)
                self.assertIn("tuantuantong-dailiantong-callback", text)
                # ⛔ 不许塞进常驻同步清单（那会被 SYNC_UNIT_ENABLE=0 的机器顺带停掉）。
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn("tuantuantong-dailiantong-callback", assignments[0])

    def test_deploy_sh_uploads_the_callback_unit_and_receiver_script_in_both_faces(self):
        """单元 + 接收器脚本都必须走 `deploy.sh` 的两处（rsync 面 / chmod+md5sum 面）。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_systemd = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_systemd, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        self.assertIn(
            "deploy/systemd/tuantuantong-dailiantong-callback.service",
            "\n".join(rsync_systemd),
        )
        rsync_callback = [
            line for line in lines if "rsync" in line and "deploy/callback/" in line
        ]
        self.assertTrue(
            rsync_callback,
            "deploy.sh 没有把 deploy/callback/ 下那份接收器脚本传上机",
        )

        chmod_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(chmod_lines), 1, "deploy.sh 里那条 chmod/md5sum 命令应当恰好一条")
        chmod_face, md5sum_face = chmod_lines[0].split("; md5sum ", 1)
        for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
            for required in (
                "$R/shared/tuantuantong-dailiantong-callback.service",
                "$R/shared/callback/dailiantong_receiver.py",
            ):
                self.assertIn(
                    required,
                    face,
                    f"deploy.sh 的 {face_name} 面里缺 {required}",
                )


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class AuditPruneTimerUnitTests(SimpleTestCase):
    """**审计表保留线**（`tuantuantong-audit-prune.service/.timer`，D-0370 批）
    的四处接线。⚠️ 与 ledger-prune 同族但**单独一类、不进** `_TIMER_COMMANDS`：本单元用的是
    **自己的** `AUDIT_PRUNE_UNITS`（每台都跑的独立开关 `AUDIT_PRUNE_UNIT_ENABLE`，默认 1
    ——审计表在租户 schema，与台账清理一样「每台清本机」，⛔ 不挂 `SYNC_UNIT_ENABLE`、
    不进 `SYNC_SERVICES`）。四处与 ledger-prune 同款（漏一处 = 半新半旧）：
    ① 两份单元文件都在，service 是 oneshot、ExecStart = `manage.py prune_audit_tables --apply`
      （⭐ **`--apply` 是承重参数**：命令缺省 dry-run ⇒ 漏了它 = timer 每天跑、一行不删）；
    ② `deploy.sh` 的 rsync 上传面里有**两个文件**；
    ③ `deploy.sh` 的 chmod/md5sum 面**也有两个**；
    ④ `activate.sh` 与 `rollback.sh` 的 `AUDIT_PRUNE_UNITS=(…)` 清单里都有它，且**两份恰好相等**。
    """

    _SERVICE = _DEPLOY / "systemd" / "tuantuantong-audit-prune.service"
    _TIMER = _DEPLOY / "systemd" / "tuantuantong-audit-prune.timer"

    @staticmethod
    def _argv(path: Path) -> list:
        return shlex.split(_exec_start(_read(path)))

    def test_the_service_is_oneshot_and_runs_the_prune_command_with_apply(self):
        """① service 是 oneshot、以 ubuntu 跑、ExecStart = `manage.py prune_audit_tables --apply`。

        失败方向：漏 `--apply`（每天 dry-run 空转）、被写成 `--loop`（常驻化）、
        命令名写错 ⇒ 本用例红。⚠️ 单元里**不许有含密钥的 `Environment=` 行**
        （凭据红线；`PYTHONUNBUFFERED` 这类非密钥变量不在此限）。
        """
        self.assertTrue(
            self._SERVICE.is_file(),
            "缺单元文件：deploy/systemd/tuantuantong-audit-prune.service",
        )
        self.assertTrue(
            self._TIMER.is_file(),
            "缺定时器文件：deploy/systemd/tuantuantong-audit-prune.timer",
        )
        service_text = _read(self._SERVICE)
        self.assertIn("\nType=oneshot\n", service_text)
        self.assertIn("\nUser=ubuntu\n", service_text)
        self.assertIn(
            "WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src",
            service_text,
        )
        self.assertEqual(
            self._argv(self._SERVICE),
            [
                "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/python",
                "manage.py",
                "prune_audit_tables",
                "--apply",
            ],
        )
        self.assertNotIn(
            "--loop", self._argv(self._SERVICE),
            "审计表清理是 oneshot，ExecStart 里不应有 --loop",
        )
        for line in service_text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_timer_fires_daily_at_0345_and_is_persistent(self):
        """timer 每天 **03:45** 触发、`Persistent=true`（命令幂等）、指名本 service；
        且**不与台账清理 03:30 / 每日报份 04:40 相撞**（错峰）。"""
        timer_text = _read(self._TIMER)
        self.assertIn("Unit=tuantuantong-audit-prune.service", timer_text)
        oncalendar = [
            line for line in timer_text.splitlines() if line.startswith("OnCalendar=")
        ]
        self.assertEqual(
            oncalendar, ["OnCalendar=*-*-* 03:45:00"],
            f"审计表清理 timer 的触发时刻应为每天 03:45，实际：{oncalendar}",
        )
        self.assertIn("Persistent=true", timer_text)
        self.assertIn("RandomizedDelaySec=", timer_text)
        self.assertIn("WantedBy=timers.target", timer_text)
        # ⛔ 触发配置里不许出现 03:30 / 04:40（错峰判据打在**非注释行**上，
        #    E-0161 同族：打在标记上，⛔ 不是「文件文本」）。
        active_lines = "\n".join(
            line for line in timer_text.splitlines() if not line.lstrip().startswith("#")
        )
        for busy in ("03:30", "04:40"):
            self.assertNotIn(
                busy, active_lines,
                f"审计表清理 timer 的有效配置里不应出现 {busy}（错峰）",
            )

    def test_inspect_remote_checks_the_audit_prune_timer_is_active(self):
        """巡检清单（`deploy/inspect_remote.sh`）必须有本 timer 的单元名常量与 `is-active`
        判活（审计表清理停了不吵不闹，巡检瞎一只眼 = D-0370 批的病换个房间回来）。"""
        text = _read(_DEPLOY / "inspect_remote.sh")
        self.assertIn(
            "AUDIT_PRUNE_TIMER=tuantuantong-audit-prune.timer", text,
            "inspect_remote.sh 里没有审计表清理 timer 的单元名常量",
        )
        self.assertIn(
            'systemctl is-active "$AUDIT_PRUNE_TIMER"', text,
            "inspect_remote.sh 没有对审计表清理 timer 做 is-active 判活",
        )

    def test_activate_and_rollback_list_the_audit_prune_timer_as_exactly_the_same_set(self):
        """④ 两个脚本的 `AUDIT_PRUNE_UNITS=(…)` 都有它，且**恰好相等**；⛔ 不许混进
        `SYNC_SERVICES`；独立开关 + 持久放法必须在场。"""
        activate_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "activate.sh", "AUDIT_PRUNE_UNITS"
        )
        rollback_units = InspectRemoteSyncUnitListTests._shell_array(
            _DEPLOY / "rollback.sh", "AUDIT_PRUNE_UNITS"
        )
        for script, units in (("activate.sh", activate_units), ("rollback.sh", rollback_units)):
            with self.subTest(script=script):
                self.assertIn(
                    "tuantuantong-audit-prune.timer", units,
                    f"{script} 的 AUDIT_PRUNE_UNITS 清单里缺 tuantuantong-audit-prune.timer"
                    "（⇒ 审计表清理不会被安装 / 回滚不重启，半新半旧）",
                )
        self.assertEqual(
            sorted(activate_units), sorted(rollback_units),
            "activate.sh 与 rollback.sh 的 AUDIT_PRUNE_UNITS 不是恰好集合："
            f"发版={sorted(activate_units)}，回滚={sorted(rollback_units)}",
        )
        for script in ("activate.sh", "rollback.sh"):
            text = _read(_DEPLOY / script)
            with self.subTest(switch=script):
                self.assertIn("AUDIT_PRUNE_UNIT_ENABLE", text, f"{script} 缺少开关")
                self.assertIn(
                    "$R/shared/audit-prune-unit.env", text,
                    f"{script} 缺少 audit-prune-unit.env 持久放法",
                )
                assignments = [
                    line for line in _logical_lines(text)
                    if line.lstrip().startswith("SYNC_SERVICES=(")
                ]
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(
                    "tuantuantong-audit-prune", assignments[0],
                    f"{script} 把审计表清理单元塞进了 SYNC_SERVICES（它每台都要跑，"
                    "不该被 SYNC_UNIT_ENABLE=0 的机器停掉）",
                )

    def test_deploy_dot_sh_uploads_and_checksums_both_units(self):
        """②③ `deploy.sh` 的 rsync 面 + chmod/md5sum 面各有**两个文件**（⚠️ chmod 与
        md5sum 是两条并列清单，只断一面正是「漏一处」的形态）。"""
        lines = _logical_lines(_read(_DEPLOY / "deploy.sh"))
        rsync_lines = [
            line for line in lines if "rsync" in line and "deploy/systemd/" in line
        ]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        upload_surface = chr(10).join(rsync_lines)
        checksum_lines = [
            line for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(checksum_lines), 1, "deploy.sh 里应恰好有一条 chmod/md5sum 命令")
        chmod_face, md5sum_face = checksum_lines[0].split("; md5sum ", 1)
        for unit_file in (
            "tuantuantong-audit-prune.service",
            "tuantuantong-audit-prune.timer",
        ):
            for face_name, face in (
                ("rsync", upload_surface),
                ("chmod", chmod_face),
                ("md5sum", md5sum_face),
            ):
                with self.subTest(unit_file=unit_file, face=face_name):
                    self.assertIn(
                        f"$R/shared/{unit_file}" if face_name != "rsync"
                        else f"deploy/systemd/{unit_file}",
                        face,
                        f"deploy.sh 的 {face_name} 面里缺 {unit_file}",
                    )
