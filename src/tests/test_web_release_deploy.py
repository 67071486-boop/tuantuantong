import hashlib
import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch


CLI = Path(__file__).resolve().parents[2] / "deploy/web-release.py"
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class WebReleaseDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.root = self.path / "target"
        self.root.mkdir()
        self.env = self.path / "app.env"
        self.env.write_text("DJANGO_SECRET_KEY=synthetic-release-key\n", encoding="utf-8")
        self.env.chmod(0o600)
        self.config = self.path / "target.json"
        self.config.write_text(json.dumps({
            "root": str(self.root), "unit": "test-web.service", "app_env": str(self.env),
            "health_port": 18091, "hosts": ["first.test.local"],
        }), encoding="utf-8")

    def package(self, *, commit=COMMIT, contents=None):
        archive = self.path / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            entries = {
                "VERSION": f"{commit}\n".encode(), "src/manage.py": b"# fixture\n",
                "src/pyproject.toml": b"# fixture\n", "src/uv.lock": b"# fixture\n",
                "src/frontend/dist/index.html": b"<html>New bundle</html>",
                "src/frontend/dist/m/index.html": b"<html>Mobile bundle</html>",
                "deploy/deploy.sh": b"# fixture\n",
                **(contents or {}),
            }
            for name, body in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(body)
                bundle.addfile(info, io.BytesIO(body))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True, text=True, timeout=20)

    def test_stage_checks_digest_and_keeps_current_unchanged(self):
        archive, checksum = self.package()
        failed = self.cli("stage", self.config, archive, "0" * 64)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.root / "releases" / COMMIT).exists())

        staged = self.cli("stage", self.config, archive, checksum)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual((self.root / "releases" / COMMIT / "src/frontend/dist/index.html").read_bytes(),
                         b"<html>New bundle</html>")
        self.assertFalse((self.root / "current").exists())

    def staged_transition(self):
        spec = importlib.util.spec_from_file_location("web_release_cli", CLI)
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        for commit in (COMMIT, OLD_COMMIT):
            archive, checksum = self.package(commit=commit)
            result = self.cli("stage", self.config, archive, checksum)
            self.assertEqual(result.returncode, 0, result.stderr)
        current = self.root / "current"
        current.symlink_to(Path("releases") / OLD_COMMIT)
        return release, current

    def test_failed_preparation_keeps_serving_previous_release(self):
        release, current = self.staged_transition()
        failure = release.ReleaseError("fixture: second database unavailable")
        with patch.object(release, "verify_unit"), patch.object(release, "prepare", side_effect=failure), patch.object(release, "restart") as restart:
            with self.assertRaisesRegex(release.ReleaseError, "second database unavailable"):
                release.transition(release.read_config(self.config), COMMIT, migrate=True)
        restart.assert_not_called()
        self.assertEqual(current.resolve().name, OLD_COMMIT)

    def test_rollback_rejects_a_release_that_was_only_staged(self):
        _, current = self.staged_transition()
        rejected = self.cli("rollback", self.config, COMMIT)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("此前启动并通过探活", rejected.stderr)
        self.assertEqual(current.resolve().name, OLD_COMMIT)

    def test_activate_without_migrations_checks_databases_and_marks_new_release_ready(self):
        release, current = self.staged_transition()
        self.env.write_text(self.env.read_text(encoding="utf-8") + "PGOPTIONS='-c statement_timeout=5000'\n",
                            encoding="utf-8")
        with (patch.object(sys, "argv", [str(CLI), "activate", str(self.config), COMMIT, "--no-migrate"]),
              patch.object(release, "verify_unit"), patch.object(release, "run") as command,
              patch.object(release, "restart") as restart):
            release.main()

        target = current.resolve()
        self.assertEqual(target.name, COMMIT)
        self.assertEqual((target / "READY").read_text(), (target / "PACKAGE-SHA256").read_text())
        restart.assert_called_once_with(release.read_config(self.config), target)
        self.assertEqual(command.call_count, 2)
        preflight = command.call_args_list[-1]
        self.assertEqual(preflight.args[2:], ("_tenants", "verify", "first.test.local"))
        self.assertEqual(preflight.kwargs["env"]["PGOPTIONS"],
                         "-c statement_timeout=5000 -c default_transaction_read_only=on")

    def test_failed_live_probe_restarts_previous_release(self):
        release, current = self.staged_transition()
        restarted = []

        def restart(_config, destination):
            self.assertEqual(current.resolve(), destination)
            restarted.append(destination.name)
            if destination.name == COMMIT:
                raise release.ReleaseError("fixture: new app unhealthy")

        with patch.object(release, "verify_unit"), patch.object(release, "prepare"), patch.object(release, "restart", restart):
            with self.assertRaisesRegex(release.ReleaseError, "fixture: new app unhealthy"):
                release.transition(release.read_config(self.config), COMMIT, migrate=True)
        self.assertEqual(restarted, [COMMIT, OLD_COMMIT])
        self.assertEqual(current.resolve(), self.root.resolve() / "releases" / OLD_COMMIT)

    def test_second_package_for_same_commit_cannot_overwrite_assets(self):
        archive, checksum = self.package()
        self.assertEqual(self.cli("stage", self.config, archive, checksum).returncode, 0)
        archive, changed = self.package(contents={"src/frontend/dist/index.html": b"different"})
        rejected = self.cli("stage", self.config, archive, changed)
        self.assertNotEqual(rejected.returncode, 0)
        index = self.root / "releases" / COMMIT / "src/frontend/dist/index.html"
        self.assertEqual(index.read_bytes(), b"<html>New bundle</html>")

    def test_http_listener_must_belong_to_the_target_service(self):
        release, current = self.staged_transition()
        target = current.resolve()
        proc = self.path / "proc"
        cgroups = self.path / "cgroup"
        group = cgroups / "test-web.service"
        group.mkdir(parents=True)
        (group / "cgroup.procs").write_text("100\n", encoding="ascii")
        (proc / "100/fd").mkdir(parents=True)
        (proc / "100/cwd").symlink_to(target / "src")
        (proc / "net").mkdir()
        (proc / "net/tcp").write_text(
            "sl  local_address rem_address st tx_queue tr tm->when retrnsmt uid timeout inode\n"
            f"0: 0100007F:{18091:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000 501 0 312\n",
            encoding="ascii",
        )
        fd = proc / "100/fd/3"
        fd.symlink_to("socket:[999]")
        config = release.read_config(self.config)
        unit = {"MainPID": "100", "ControlGroup": "/test-web.service"}
        with (patch.object(release, "PROC_ROOT", proc, create=True),
              patch.object(release, "CGROUP_ROOT", cgroups, create=True),
              patch.object(release, "unit_show", return_value=unit),
              patch.object(release.http.client, "HTTPConnection") as connection):
            self.assertFalse(release.healthy(config, target))
            connection.assert_not_called()

            fd.unlink()
            fd.symlink_to("socket:[312]")
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"status":"ok"}'
            self.assertTrue(release.healthy(config, target))
            connection.assert_called_once_with("127.0.0.1", 18091, timeout=2)

    def test_cancelled_publisher_stops_descendants_before_releasing_lock(self):
        started = self.path / "started"
        delayed = self.path / "delayed"
        grandchild = f"from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(1.5); Path({str(delayed)!r}).touch()"
        worker = f"import subprocess,sys; subprocess.run([sys.executable, '-c', {grandchild!r}], check=True)"
        publisher = (
            "import runpy, signal, sys\nfrom pathlib import Path\n"
            "def interrupt(*args): raise KeyboardInterrupt\n"
            "signal.signal(signal.SIGTERM, interrupt)\n"
            f"release = runpy.run_path({str(CLI)!r})\n"
            f"with release['release_lock'](Path({str(self.root)!r})):\n"
            f"    release['run'](sys.executable, '-c', {worker!r})\n"
        )
        with subprocess.Popen([sys.executable, "-c", publisher], stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            process.terminate()
            self.assertNotEqual(process.wait(timeout=5), 0)
        time.sleep(1.6)
        self.assertFalse(delayed.exists(), "a cancelled release left a running descendant")

    def test_signal_after_fork_cancels_child_even_before_popen_returns(self):
        started = self.path / "fork-started"
        delayed = self.path / "fork-delayed"
        child = f"from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(1.5); Path({str(delayed)!r}).touch()"
        publisher = (
            "import os, runpy, signal, subprocess, sys, time\nfrom pathlib import Path\n"
            f"started = Path({str(started)!r})\n"
            "def interrupt(*args): raise KeyboardInterrupt\n"
            "signal.signal(signal.SIGTERM, interrupt)\n"
            "popen = subprocess.Popen\n"
            "def interrupt_after_fork(*args, **kwargs):\n"
            "    process = popen(*args, **kwargs)\n"
            "    deadline = time.monotonic() + 5\n"
            "    while not started.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
            "    assert started.exists()\n"
            "    subprocess.Popen = popen\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    return process\n"
            "subprocess.Popen = interrupt_after_fork\n"
            f"release = runpy.run_path({str(CLI)!r})\n"
            f"with release['release_lock'](Path({str(self.root)!r})):\n"
            f"    release['run'](sys.executable, '-c', {child!r})\n"
        )
        result = subprocess.run([sys.executable, "-c", publisher], capture_output=True, text=True, timeout=10)
        self.assertTrue(started.exists(), result.stderr)
        self.assertIn("KeyboardInterrupt", result.stderr)
        time.sleep(1.6)
        self.assertFalse(delayed.exists(), "child outlived the lock during subprocess creation")

    def test_staged_web_files_remain_readable_under_private_umask(self):
        archive, checksum = self.package()
        child = "import os,runpy,sys; os.umask(0o077); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
        result = subprocess.run([sys.executable, "-c", child, str(CLI), "stage", str(self.config), str(archive), checksum],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        releases = self.root / "releases"
        index = releases / COMMIT / "src/frontend/dist/index.html"
        for directory in (releases, releases / COMMIT, index.parent, index.parent.parent):
            self.assertEqual(directory.stat().st_mode & 0o005, 0o005)

    def test_manifest_change_during_preparation_keeps_previous_release(self):
        release, current = self.staged_transition()
        manifest = self.path / "shared-routes.json"
        manifest.write_text("{}", encoding="utf-8")
        manifest.chmod(0o600)
        self.env.write_text(f"TT_SHARED_WEB_ROUTES_FILE={manifest}\n", encoding="utf-8")

        def modify_inputs(*args, **kwargs):
            manifest.write_text('{"changed":true}', encoding="utf-8")

        with (patch.object(release, "verify_unit"), patch.object(release, "prepare", modify_inputs),
              patch.object(release, "restart") as restart):
            with self.assertRaisesRegex(release.ReleaseError, "发布期间部署配置发生变化"):
                release.transition(release.read_config(self.config), COMMIT, migrate=True)
        self.assertEqual(current.resolve().name, OLD_COMMIT)
        restart.assert_not_called()

    def test_environment_change_after_restart_restores_previous_release(self):
        release, current = self.staged_transition()
        restarted = []

        def restart(_config, destination):
            restarted.append(destination.name)
            if destination.name == COMMIT:
                self.env.write_text("DJANGO_SECRET_KEY=changed-synthetic-key\n", encoding="utf-8")

        with (patch.object(release, "verify_unit"), patch.object(release, "prepare"),
              patch.object(release, "restart", restart)):
            with self.assertRaisesRegex(release.ReleaseError, "发布期间部署配置发生变化"):
                release.transition(release.read_config(self.config), COMMIT, migrate=True)
        self.assertEqual(restarted, [COMMIT, OLD_COMMIT])
        self.assertFalse((self.root / "releases" / COMMIT / "READY").exists())
        self.assertEqual(current.resolve().name, OLD_COMMIT)

    def test_healthy_current_release_still_requires_database_preparation(self):
        release, current = self.staged_transition()
        with (patch.object(release, "verify_unit"), patch.object(release, "healthy", return_value=True),
              patch.object(release, "prepare", side_effect=release.ReleaseError("database unavailable"))):
            with self.assertRaisesRegex(release.ReleaseError, "database unavailable"):
                release.transition(release.read_config(self.config), OLD_COMMIT, migrate=True)
        self.assertFalse((current / "READY").exists())

    def test_ambiguous_environment_value_does_not_start_preparation(self):
        release, current = self.staged_transition()
        self.env.write_text('DJANGO_SECRET_KEY="first""second"\n', encoding="utf-8")
        with patch.object(release, "verify_unit"), patch.object(release, "prepare") as prepare:
            with self.assertRaisesRegex(release.ReleaseError, "不支持多段引号"):
                release.transition(release.read_config(self.config), COMMIT, migrate=True)
        prepare.assert_not_called()
        self.assertEqual(current.resolve().name, OLD_COMMIT)
