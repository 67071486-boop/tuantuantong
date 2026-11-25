"""`egress_probe` 命令 + `deploy/egress.sh`：出口自检与「加/换出口」的**默认 dry-run**。

## 这个文件守什么

`egress_probe` 是运维换完出口之后**唯一**能当场回答「这台机器现在还能不能登录 + 读一页大厅」
的东西（`deploy/egress.sh --apply` 的收尾就是它）。它坏了是**静默**的：脚本会以为换出口成功，
而同步其实仍在对端 WAF 上撞墙。⇒ 这里打**真实出口**：

1. **两条判据各自成对**：登录一次 + 读一页大厅（`HALL_PATH` + `gameName=原神&page=1&limit=10`）；
   任一失败 ⇒ 退出码 1（`CommandError`），两条都过 ⇒ 0。
2. **大厅的成功码自己判**（`code == 0`，复用 `jidanpu_upstream._require_read_success`）：
   SDK 的 `lenient` 会把 `code > 0` 判成成功 ⇒ 只看「没抛异常」会**误绿**（E-0087 的读侧同族）。
3. **不泄露代理凭据**：输出里只有出口 **host**；代理的账号 / 密码一个字都不许出现
   （硬约束：任何输出 / 日志 / 异常里都不得出现）。
4. **`open_client(..., proxy=…)` 这一格真的把给定出口交给 httpx**：探针换的就是它，
   ⛔ 不是让探针自己去拼一份建连逻辑。
5. **`deploy/egress.sh` 默认不动远端**：dry-run 只打印（打码后的）计划；
   `--apply` 才替换 `JIDANPU_PROXIES=` **那一行**（别的行原样保留），再重启单元 + 自检。

⚠️ 夹具一律**合成值**（E-0077）：域名 `*.test.invalid`、账号 / 密码带 canary 字样。
⚠️ 桩打在**被查找的那个模块**上（E-0029）：`open_client` 在 `apps.business.jidanpu` 里查。
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_upstream
from apps.business.models import JidanpuUpstreamBinding
from jidanpu.errors import UpstreamBlockedError

# ── 合成夹具值（E-0077）───────────────────────────────────────────────────────

#: 代理整条 URL 里的账号 / 密码**只许出现在传给 httpx 的那一格**，⛔ 不许出现在任何输出里。
PROXY = "http://probe-user:probe-pass@egress-probe.test.invalid:8080"
PROXY_USER = "probe-user"
PROXY_PASSWORD = "probe-pass"
PROXY_HOST = "egress-probe.test.invalid"

#: 上家绑定的凭据（合成值；命令只从这一行取，⛔ 不接受命令行给的账号密码）。
UPSTREAM_BASE_URL = "https://upstream-egress-probe.test.invalid"
UPSTREAM_ACCOUNT = "upstream-acc-canary"
UPSTREAM_PASSWORD = "upstream-pw-canary"

#: 大厅探针的**逐字**参数（brief 第 1 条）。
HALL_QUERY = {"gameName": "原神", "page": 1, "limit": 10}

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "deploy" / "egress.sh"

#: 拆进程后真正会走出口的六台同步单元（`deploy/systemd/` 的实际单元名）。
#: ⚠️ 只重启 `...-sync` 等于没重启：它的 ExecStart 全是 `--skip-*`。
_SYNC_UNITS = (
    "tuantuantong-jidanpu-sync",
    "tuantuantong-jidanpu-outbox",
    "tuantuantong-jidanpu-fastpath",
    "tuantuantong-jidanpu-reconcile",
    "tuantuantong-jidanpu-callback",
    "tuantuantong-jidanpu-upstream",
)


class _FakeClient:
    """`open_client` 产出的桩客户端：只记调用，不发网络。"""

    def __init__(self, *, login_error=None, call_error=None, code=0):
        self.login_error = login_error
        self.call_error = call_error
        self.last_envelope = {"code": code}
        self.logins = 0
        self.calls: list[tuple[str, str, dict]] = []

    def ensure_login(self):
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.call_error is not None:
            raise self.call_error
        return []


class ProbeCommandTests(TenantTestCase):
    """真租户库 + 一个真 `Tenant` 行（租户解析走真查询，⛔ 不用 mock 掉那一段）。"""

    def setUp(self):
        from apps.business.management.commands import egress_probe as module

        self.module = module
        user = User.objects.create_user(
            username="kefu-egress-probe", password="pw-canary-x"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=user,
            jidanpu_base_url=UPSTREAM_BASE_URL,
            account=UPSTREAM_ACCOUNT,
            password=UPSTREAM_PASSWORD,
            enabled=True,
        )
        self.out = io.StringIO()
        self.err = io.StringIO()

    # ── 夹具 ────────────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _patched_client(self, fake):
        """把 `jidanpu.open_client` 换成「回桩客户端」的上下文管理器，并记下实参。"""
        opened: list[tuple[object, dict]] = []

        def _open(binding, **kwargs):
            opened.append((binding, kwargs))
            return contextlib.nullcontext(fake)

        with mock.patch.object(jidanpu, "open_client", side_effect=_open):
            yield opened

    def _run(self, *args):
        return call_command(
            "egress_probe", *args, stdout=self.out, stderr=self.err
        )

    def _text(self):
        return self.out.getvalue() + self.err.getvalue()

    def _assert_no_proxy_secret(self, text):
        self.assertNotIn(PROXY_PASSWORD, text, "输出里出现了代理密码")
        self.assertNotIn(PROXY_USER, text, "输出里出现了代理账号")

    # ── 失败路径（brief 第 3 条）──────────────────────────────────────

    def test_blocked_login_reports_failure_and_exits_1(self):
        """登录被 WAF 拦（`UpstreamBlockedError`）⇒ `登录=失败`、退出码 1、⛔ 不泄露凭据。"""
        fake = _FakeClient(
            login_error=UpstreamBlockedError("blocked", status_code=468)
        )
        with self._patched_client(fake) as opened:
            with self.assertRaises(CommandError):
                self._run("--proxy", PROXY, "--tenant", self.tenant.schema_name)
        text = self._text()
        self.assertIn("登录=失败", text)
        self.assertIn("失败", text)
        self._assert_no_proxy_secret(text)
        self.assertEqual(fake.calls, [], "登录都没成，⛔ 不该再打大厅")
        self.assertEqual(
            opened[0][1].get("proxy"), PROXY, "探针必须把 --proxy 交给 open_client"
        )

    def test_blocked_hall_read_reports_failure_and_exits_1(self):
        fake = _FakeClient(
            call_error=UpstreamBlockedError("blocked", status_code=468)
        )
        with self._patched_client(fake):
            with self.assertRaises(CommandError):
                self._run("--proxy", PROXY, "--tenant", self.tenant.schema_name)
        text = self._text()
        self.assertIn("登录=OK", text)
        self.assertIn("大厅=失败", text)
        self._assert_no_proxy_secret(text)

    def test_a_nonzero_business_code_is_a_failed_hall(self):
        """`code != 0` 的失败信封必须判**失败**（⛔ 不能借 SDK 的宽松解包误绿，E-0087）。"""
        fake = _FakeClient(code=1)
        with self._patched_client(fake):
            with self.assertRaises(CommandError):
                self._run("--proxy", PROXY, "--tenant", self.tenant.schema_name)
        self.assertIn("大厅=失败", self._text())

    # ── 成功路径 ────────────────────────────────────────────────────────────

    def test_success_prints_the_four_readings_and_uses_the_brief_params(self):
        fake = _FakeClient()
        with self._patched_client(fake) as opened:
            self._run("--proxy", PROXY, "--tenant", self.tenant.schema_name)
        text = self._text()
        self.assertIn("登录=OK", text)
        self.assertIn("大厅=OK", text)
        self.assertRegex(text, r"耗时=\d+ms")
        self.assertIn(f"出口={PROXY_HOST}", text)
        self._assert_no_proxy_secret(text)
        # 大厅那一跳的**逐字**参数（`HALL_PATH` + gameName/page/limit）。
        self.assertEqual(len(fake.calls), 1, fake.calls)
        method, path, kwargs = fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, jidanpu_upstream.HALL_PATH)
        self.assertEqual(kwargs.get("query"), HALL_QUERY)
        # 凭据来自那一行绑定，⛔ 不来自命令行。
        self.assertEqual(opened[0][0].pk, self.binding.pk)

    def test_direct_token_means_no_proxy(self):
        """`--proxy direct` = 直连（出口池的同一个词）⇒ 交给 httpx 的是 `None`。"""
        fake = _FakeClient()
        with self._patched_client(fake) as opened:
            self._run("--proxy", "direct", "--tenant", self.tenant.schema_name)
        self.assertIsNone(opened[0][1].get("proxy"))
        self.assertIn("出口=direct", self._text())

    def test_proxy_dash_reads_the_outlet_from_stdin(self):
        """`--proxy -` ⇒ 从 **stdin** 读出出口（`deploy/egress.sh` 靠它避免密码进远端 ps）。"""
        fake = _FakeClient()
        with self._patched_client(fake) as opened, \
             mock.patch.object(sys, "stdin", io.StringIO(PROXY + "\n")):
            self._run("--proxy", "-", "--tenant", self.tenant.schema_name)
        self.assertEqual(
            opened[0][1].get("proxy"), PROXY,
            "`--proxy -` 必须把 stdin 那一行原样交给 open_client",
        )
        self.assertIn(f"出口={PROXY_HOST}", self._text())
        self._assert_no_proxy_secret(self._text())

    def test_without_tenant_it_finds_a_usable_binding(self):
        """`--tenant` 省略 ⇒ 按 schema 序找到第一份有凭据的启用绑定（brief 的方括号是可选的）。"""
        fake = _FakeClient()
        with self._patched_client(fake):
            self._run("--proxy", PROXY)
        self.assertIn("登录=OK", self._text())
        self.assertIn("大厅=OK", self._text())

    # ── 安静的失败（⛔ 不打上游）────────────────────────────────────────────

    def test_unknown_tenant_fails_without_touching_the_network(self):
        with mock.patch.object(jidanpu, "open_client") as open_client:
            with self.assertRaises(CommandError):
                self._run("--proxy", PROXY, "--tenant", "no-such-schema-zzz")
        open_client.assert_not_called()

    def test_tenant_without_a_password_binding_fails_without_the_network(self):
        """只有②式（官方 API）绑定 ⇒ 登录式探针无凭据可用，⛔ 不拿空账号去撞上游。"""
        self.binding.delete()
        with mock.patch.object(jidanpu, "open_client") as open_client:
            with self.assertRaises(CommandError):
                self._run("--proxy", PROXY, "--tenant", self.tenant.schema_name)
        open_client.assert_not_called()


class OpenClientProxyOverrideTests(SimpleTestCase):
    """`open_client(..., proxy=…)`：显式出口优先于出口池的当前出口（探针就靠这一格）。"""

    def test_explicit_proxy_wins_over_the_pool(self):
        binding = mock.Mock(
            base_url="https://upstream-egress-probe.test.invalid",
            account=UPSTREAM_ACCOUNT,
            password=UPSTREAM_PASSWORD,
        )
        with mock.patch.dict(
            os.environ, {"JIDANPU_PROXY": "http://pool-user:pool-pass@pool.invalid:1"}
        ), mock.patch.object(jidanpu.httpx, "Client") as client_cls, \
             mock.patch.object(jidanpu, "JidanpuClient"):
            with jidanpu.open_client(binding, proxy=PROXY):
                pass
        self.assertEqual(client_cls.call_args.kwargs.get("proxy"), PROXY)

    def test_default_still_uses_the_pool_outlet(self):
        binding = mock.Mock(
            base_url="https://upstream-egress-probe.test.invalid",
            account=UPSTREAM_ACCOUNT,
            password=UPSTREAM_PASSWORD,
        )
        pool = "http://pool-user:pool-pass@pool.invalid:1"
        with mock.patch.dict(os.environ, {"JIDANPU_PROXY": pool}), \
             mock.patch.object(jidanpu.httpx, "Client") as client_cls, \
             mock.patch.object(jidanpu, "JidanpuClient"):
            with jidanpu.open_client(binding):
                pass
        self.assertEqual(client_cls.call_args.kwargs.get("proxy"), pool)


#: 假的 `ssh`：把「远端命令」原样在**本机**跑（配合 `TT_EGRESS_REMOTE_ROOT` 指到临时目录）。
_FAKE_SSH = """#!/usr/bin/env bash
# 用法：ssh [-o key=value …] <host> '<command>'；本桩只取命令、忽略 host（测试不碰真机器）。
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
exec bash -c "$*"
"""

#: 假 `sudo`：直接执行参数（测试机没有 root 也可跑）。
_FAKE_SUDO = """#!/usr/bin/env bash
exec "$@"
"""

#: 假 `systemctl`：成功即返回（重启是**远端副作用**，测试里只验真的被调用）。
_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*"
exit 0
"""

#: 假 `python`（远端 `current/src/.venv/bin/python`）：把参数记一行后成功。
_FAKE_PROBE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> {log}
exit 0
"""


class EgressShellScriptTests(SimpleTestCase):
    """`deploy/egress.sh`：**默认 dry-run**，只有 `--apply` 才改 `.env` / 重启 / 自检。"""

    OLD_PROXY = "http://old-user:old-pass@old-egress.test.invalid:1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "remote"
        (self.root / "shared").mkdir(parents=True)
        self.env_file = self.root / "shared" / ".env"
        self.env_file.write_text(
            "DJANGO_ALLOWED_HOSTS=probe.test.invalid\n"
            f"JIDANPU_PROXIES={self.OLD_PROXY}\n"
            "OTHER_KEY=keep-me\n",
            encoding="utf-8",
        )
        self.fakebin = self.tmp / "bin"
        self.fakebin.mkdir()
        self._write_exe("ssh", _FAKE_SSH)
        self._write_exe("sudo", _FAKE_SUDO)
        self._write_exe("systemctl", _FAKE_SYSTEMCTL)
        self.probe_log = self.tmp / "probe.log"
        probe_python = self.root / "current" / "src" / ".venv" / "bin" / "python"
        probe_python.parent.mkdir(parents=True)
        probe_python.write_text(
            _FAKE_PROBE_PYTHON.format(log=shlex.quote(str(self.probe_log))),
            encoding="utf-8",
        )
        probe_python.chmod(0o755)

    def _write_exe(self, name, content):
        path = self.fakebin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, *args):
        env = dict(
            os.environ,
            PATH=f"{self.fakebin}:{os.environ['PATH']}",
            TT_EGRESS_REMOTE_ROOT=str(self.root),
        )
        return subprocess.run(
            ["bash", str(_SCRIPT), *args],
            capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT), timeout=60,
        )

    def test_dry_run_prints_the_plan_and_leaves_the_env_untouched(self):
        before = self.env_file.read_text(encoding="utf-8")
        result = self._run("set", "demo", PROXY)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout + result.stderr
        self.assertNotIn(PROXY_PASSWORD, text, "dry-run 输出里出现了代理密码")
        self.assertNotIn(PROXY_USER, text, "dry-run 输出里出现了代理账号")
        self.assertIn("***@", text, "打印出口时必须把 u:p@ 打码成 ***@")
        self.assertIn("egress_probe", text)
        # ⚠️ 承重：sync 的 ExecStart 全是 `--skip-*`，只重启它等于
        #    没重启——真正走出口的是另外五台 ⇒ 六台都要在计划里。
        for unit in _SYNC_UNITS:
            self.assertIn(f"systemctl restart {unit}", text)
        self.assertEqual(
            self.env_file.read_text(encoding="utf-8"), before,
            "默认 dry-run ⛔ 不许动远端 .env",
        )
        self.assertFalse(self.probe_log.exists(), "dry-run ⛔ 不许跑自检")

    def test_apply_replaces_only_the_proxies_line_and_restarts_and_probes(self):
        result = self._run("set", "demo", PROXY, "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout + result.stderr
        self.assertNotIn(PROXY_PASSWORD, text)
        self.assertNotIn(PROXY_USER, text)
        lines = self.env_file.read_text(encoding="utf-8").splitlines()
        proxies = [line for line in lines if line.startswith("JIDANPU_PROXIES=")]
        self.assertEqual(proxies, [f"JIDANPU_PROXIES={PROXY}"], lines)
        self.assertIn("DJANGO_ALLOWED_HOSTS=probe.test.invalid", lines)
        self.assertIn("OTHER_KEY=keep-me", lines)
        # ⚠️ 六台都要真的被重启（判据打在「真的调了 systemctl restart」上）。
        for unit in _SYNC_UNITS:
            self.assertIn(f"systemctl restart {unit}", text)
        # 自检真的被跑，且带的是**第一个**出口 + 租户 schema。
        self.assertTrue(self.probe_log.exists(), "apply 之后没跑 egress_probe 自检")
        probe = self.probe_log.read_text(encoding="utf-8")
        self.assertIn("egress_probe", probe)
        # ⭐ 出口经 stdin 传：远端命令行里只有 `--proxy -`，⛔ 不含账号 / 密码。
        self.assertIn("--proxy -", probe)
        self.assertNotIn(PROXY, probe, "出口 URL 进了远端命令行（远端 ps 能看到密码）")
        self.assertIn("--tenant demo", probe)

    def test_apply_keeps_an_export_prefix_on_that_line(self):
        """只换**值**：原行写成 `export JIDANPU_PROXIES=…` 时，前缀原样保留。"""
        self.env_file.write_text(
            "export JIDANPU_PROXIES=http://old-user:old-pass@old-egress.test.invalid:1\n",
            encoding="utf-8",
        )
        result = self._run("set", "demo", PROXY, "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.env_file.read_text(encoding="utf-8").splitlines()
        # ⭐ 脚本还会写一行共享状态文件（跨进程共享出口决策）。
        self.assertEqual(lines[0], f"export JIDANPU_PROXIES={PROXY}")
        state = [line for line in lines if line.startswith("JIDANPU_EGRESS_STATE_FILE=")]
        self.assertEqual(len(state), 1, lines)
        self.assertTrue(state[0].endswith("/shared/egress-state.json"), state)

    def test_apply_appends_the_line_when_the_file_has_none(self):
        self.env_file.write_text(
            "DJANGO_ALLOWED_HOSTS=probe.test.invalid\nOTHER_KEY=keep-me\n",
            encoding="utf-8",
        )
        result = self._run("set", "demo", PROXY, "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn(f"JIDANPU_PROXIES={PROXY}", text)
        self.assertIn("OTHER_KEY=keep-me", text)
        self.assertEqual(
            text.count("JIDANPU_PROXIES="), 1, "⛔ 追加时必须恰好一行"
        )
        self.assertEqual(
            text.count("JIDANPU_EGRESS_STATE_FILE="), 1,
            "⛔ 共享状态那行也必须恰好一行",
        )
