import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


RENDER = Path(__file__).resolve().parents[2] / "deploy/platform-callback/render_worker_units.py"
JIDANPU = "ttt-worker-jidanpu-receiver@fixture.service"
DAILIANTONG = "ttt-worker-dailiantong-receiver@fixture.service"


class CallbackWorkerReleaseTests(TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()

    def release(self, version, *, compatible=True):
        release = self.root / "releases" / (version * 40)
        release.mkdir(parents=True)
        (release / "VERSION").write_text(release.name + "\n")
        for path in ("src/apps/callback/tenant_wsgi.py", "deploy/callback/dailiantong_receiver.py"):
            source = release / path
            source.parent.mkdir(parents=True, exist_ok=True)
            if compatible:
                source.touch()
        for program in ("gunicorn", "python"):
            executable = release / "src/.venv/bin" / program
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/bin/sh\npwd\n")
            executable.chmod(0o700)
        return release

    def render(self, release, output, *, check=True):
        return subprocess.run(
            [sys.executable, RENDER, "fixture", release, output],
            text=True, capture_output=True, check=check,
        )

    def assert_units_launch_release(self, output, release):
        self.assertCountEqual([path.name for path in output.iterdir()], [JIDANPU, DAILIANTONG])
        for unit in (JIDANPU, DAILIANTONG):
            text = (output / unit).read_text()
            fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            self.assertNotIn("/current/", text)
            self.assertEqual(fields["WorkingDirectory"], str(release / "src"))
            command = fields["ExecStart"].split()
            self.assertEqual(Path(command[0]).parent, release / "src/.venv/bin")
            if unit == DAILIANTONG:
                self.assertEqual(Path(command[1]), release / "deploy/callback/dailiantong_receiver.py")
            started = subprocess.run(
                command, cwd=fields["WorkingDirectory"], check=True, text=True, capture_output=True,
            )
            self.assertEqual(started.stdout.strip(), str(release / "src"))

    def test_receiver_unit_stays_on_its_version_after_a_background_rollback(self):
        candidate = self.release("a")
        incompatible = self.release("b", compatible=False)
        fallback = self.release("c")
        pointer = self.root / "current"
        pointer.symlink_to(candidate)

        active = self.root / "active"
        self.render(pointer, active)
        pointer.unlink()
        pointer.symlink_to(incompatible)
        self.assert_units_launch_release(active, candidate)

        invalid = self.root / "invalid"
        rejected = self.render(pointer, invalid, check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("缺少独立回调接收器", rejected.stderr)
        self.assertFalse(invalid.exists())

        rollback = self.root / "rollback"
        self.render(fallback, rollback)
        self.assert_units_launch_release(rollback, fallback)
