import hashlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "deploy/client-infrastructure-release.py"


def load_release():
    spec = importlib.util.spec_from_file_location("client_release_under_test", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClientInfrastructureReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "target"
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        self.backup_root = self.directory / "backup-target"
        self.backup_root.mkdir(mode=0o700)

    def cli(self, *arguments, ok=True):
        result = subprocess.run(
            [sys.executable, str(CLI), *map(str, arguments)],
            capture_output=True, text=True, timeout=25,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def build(self, path, ref="HEAD", *, pilot=False):
        self.cli("build", ref, path, *(["--test-download"] if pilot else []))
        with tarfile.open(path) as archive:
            version = archive.extractfile("VERSION").read().decode().strip()
        return version, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_download_and_backup_have_separate_install_paths(self):
        package = self.directory / "first.tar.gz"
        version, digest = self.build(package, pilot=True)
        self.cli("stage", self.root, package, digest)
        self.assertFalse((self.root / "current").exists())

        self.cli("activate", self.root, version)
        current = self.root / "current"
        self.assertEqual(current.resolve(), self.root / "releases" / version)

        page = (current / "public/index.html").read_text(encoding="utf-8")
        feed = (current / "public/releases.js").read_text(encoding="utf-8")
        apk = (current / "public/pilot-download.apk").read_bytes()
        self.assertIn("测试包不可安装", page)
        self.assertIn("window.APP_RELEASE_FEED", feed)
        self.assertIn(hashlib.sha256(apk).hexdigest(), feed)
        self.assertIn(b"NOT AN ANDROID INSTALLER", apk)
        self.assertTrue((current / "public/icon-192.png").is_file())
        self.assertFalse((current / "public/backup_daily.py").exists())
        backup = current / "backup"
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        original_backup = subprocess.check_output(["git", "show", f"{version}:deploy/backup_daily.py"], cwd=REPO)
        self.assertEqual((backup / "backup_daily.py").read_bytes(), original_backup)

    def test_same_commit_build_is_repeatable_and_keeps_regular_site_feed(self):
        first, second, pilot = (self.directory / name for name in ("first.tgz", "second.tgz", "pilot.tgz"))
        version, checksum = self.build(first)
        self.assertEqual(self.build(second), (version, checksum))
        with tarfile.open(first) as archive:
            published_page = archive.extractfile("public/index.html").read()
            self.assertNotIn("public/releases.js", archive.getnames())
            self.assertNotIn("public/pilot-download.apk", archive.getnames())
        original = subprocess.check_output(
            ["git", "show", f"{version}:src/frontend/public/download/index.html"], cwd=REPO,
        )
        self.assertEqual(published_page, original)
        self.assertIn(b"cos.ap-shanghai.myqcloud.com/releases.js", published_page)

        _, pilot_checksum = self.build(pilot, pilot=True)
        self.cli("stage", self.root, first, checksum)
        self.cli("stage", self.root, second, checksum)
        self.assertIn("不能混装", self.cli("stage", self.root, pilot, pilot_checksum, ok=False).stderr)

    def test_pilot_root_rejects_a_regular_package_even_at_a_different_commit(self):
        first, second = (self.directory / name for name in ("first.tgz", "second.tgz"))
        version, first_digest = self.build(first, "HEAD~1", pilot=True)
        second_version, second_digest = self.build(second)
        self.cli("stage", self.backup_root, first, first_digest)
        self.cli("activate", self.backup_root, version)
        failed = self.cli("stage", self.backup_root, second, second_digest, ok=False)
        self.assertIn("不能混装", failed.stderr)
        self.assertEqual((self.backup_root / "current").resolve().name, version)
        self.assertFalse((self.backup_root / "releases" / second_version).exists())

    def test_first_stage_rejects_an_existing_web_root_without_changing_it(self):
        package = self.directory / "standard.tgz"
        _, digest = self.build(package)
        active = self.root / "legacy"
        active.mkdir()
        current = self.root / "current"
        current.symlink_to(active)
        failed = self.cli("stage", self.root, package, digest, ok=False)
        self.assertIn("独立的空部署目录", failed.stderr)
        self.assertEqual(current.resolve(), active)
        self.assertFalse((self.root / ".web-release.lock").exists())
        self.assertFalse((self.root / ".release-profile").exists())

    def test_rollback_requires_previously_active_assets_and_recovers_from_damaged_current(self):
        first, second = (self.directory / name for name in ("first.tgz", "second.tgz"))
        previous, first_digest = self.build(first, "HEAD~1", pilot=True)
        candidate, second_digest = self.build(second, pilot=True)
        self.cli("stage", self.root, first, first_digest)
        self.cli("stage", self.root, second, second_digest)
        self.cli("activate", self.root, previous)
        current = self.root / "current"
        self.assertIn("尚未通过激活", self.cli("rollback", self.root, candidate, ok=False).stderr)
        self.assertEqual(current.resolve().name, previous)

        self.cli("activate", self.root, candidate)
        self.assertEqual(current.resolve().name, candidate)
        (current / "public/index.html").write_text("damaged page", encoding="utf-8")
        self.assertIn("发生变化", self.cli("activate", self.root, candidate, ok=False).stderr)
        self.cli("rollback", self.root, previous)
        self.assertEqual(current.resolve().name, previous)

    def test_download_and_backup_versions_switch_independently(self):
        first, second = (self.directory / name for name in ("first.tgz", "second.tgz"))
        previous, first_digest = self.build(first, "HEAD~1")
        candidate, second_digest = self.build(second)
        for target in (self.root, self.backup_root):
            self.cli("stage", target, first, first_digest)
            self.cli("stage", target, second, second_digest)
            self.cli("activate", target, previous)
        self.cli("activate", self.root, candidate)
        self.assertEqual((self.root / "current").resolve().name, candidate)
        self.assertEqual((self.backup_root / "current").resolve().name, previous)

        self.cli("rollback", self.root, previous)
        self.cli("activate", self.backup_root, candidate)
        self.assertEqual((self.root / "current").resolve().name, previous)
        self.assertEqual((self.backup_root / "current").resolve().name, candidate)

    def test_ready_marker_error_restores_previous_live_link(self):
        first, second = (self.directory / name for name in ("first.tgz", "second.tgz"))
        previous, first_digest = self.build(first, "HEAD~1")
        candidate, second_digest = self.build(second)
        self.cli("stage", self.root, first, first_digest)
        self.cli("stage", self.root, second, second_digest)
        self.cli("activate", self.root, previous)
        release = load_release()
        current = self.root / "current"

        def fail_while_marking(_path, _value):
            self.assertEqual(current.resolve().name, candidate)
            raise PermissionError("fixture marker failure")

        with patch.object(release, "write_marker", side_effect=fail_while_marking):
            with self.assertRaises(PermissionError):
                release.switch(self.root, candidate, rollback=False)
        self.assertEqual(current.resolve().name, previous)
        self.assertFalse((self.root / "releases" / candidate / "READY").exists())

    def test_rejects_bad_digest_and_unexpected_or_linked_files(self):
        standard = self.directory / "standard.tgz"
        version, _ = self.build(standard)
        self.assertIn("SHA-256 不匹配", self.cli("stage", self.root, standard, "0" * 64, ok=False).stderr)
        with tarfile.open(standard) as archive:
            entries = {name: archive.extractfile(name).read() for name in archive.getnames()}
        for index, name in enumerate(("../escape", "public/.env", "public/icon-192.png")):
            with self.subTest(name=name):
                malicious = self.directory / f"unexpected-{index}.tgz"
                with tarfile.open(malicious, "w:gz") as archive:
                    for existing, data in entries.items():
                        if name == existing:
                            continue
                        info = tarfile.TarInfo(existing)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                    info = tarfile.TarInfo(name)
                    if name == "public/icon-192.png":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "../backup/backup_daily.py"
                        archive.addfile(info)
                    else:
                        info.size = 7
                        archive.addfile(info, io.BytesIO(b"fixture"))
                digest = hashlib.sha256(malicious.read_bytes()).hexdigest()
                self.assertIn("文件清单不符", self.cli("stage", self.root, malicious, digest, ok=False).stderr)
                self.assertFalse((self.root / "releases" / version).exists())
                self.assertFalse((self.root / "current").exists())

    def test_backup_preflight_rejects_another_database_before_installing_dependencies(self):
        package = self.directory / "backup.tgz"
        version, digest = self.build(package)
        self.cli("stage", self.backup_root, package, digest)
        self.cli("activate", self.backup_root, version)
        environment = self.directory / "backup.env"
        environment.write_text(
            "POSTGRES_DB=tt_other\nPOSTGRES_USER=tt_other\nPOSTGRES_PASSWORD=synthetic\n",
            encoding="utf-8",
        )
        environment.chmod(0o600)
        failed = self.cli("backup-check", self.backup_root, environment, "pilot", ok=False)
        self.assertIn("该工作室自己的数据库和角色", failed.stderr)
        self.assertFalse((self.backup_root / "current/backup/.venv").exists())

    def test_pilot_backup_run_fails_before_opening_credentials_or_installing_dependencies(self):
        package = self.directory / "pilot.tgz"
        version, digest = self.build(package, pilot=True)
        self.cli("stage", self.backup_root, package, digest)
        self.cli("activate", self.backup_root, version)
        missing_env = self.directory / "missing.env"

        failed = self.cli("backup-run", self.backup_root, missing_env, "pilot", ok=False)
        self.assertIn("试点版本不能执行真实备份", failed.stderr)
        self.assertFalse(missing_env.exists())
        self.assertFalse((self.backup_root / "current/backup/.venv").exists())

    def test_backup_commands_require_a_private_root_before_reading_environment(self):
        package = self.directory / "standard.tgz"
        version, digest = self.build(package)
        self.cli("stage", self.root, package, digest)
        self.cli("activate", self.root, version)

        for command in ("backup-check", "backup-run"):
            failed = self.cli(command, self.root, self.directory / "missing.env", "pilot", ok=False)
            self.assertIn("备份部署目录必须为 0700", failed.stderr)
        self.assertFalse((self.root / "current/backup/.venv").exists())

    def test_backup_readonly_probe_rejects_cross_tenant_connect_grant(self):
        release = load_release()
        database = "tt_fixture_studio"
        options = {"dbname": database, "options": "-c default_transaction_read_only=on"}
        fixture = MagicMock()
        fixture.Error = RuntimeError
        cursor = fixture.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        role = (database, database, database, "on", database, False)
        with patch.dict(sys.modules, {"psycopg": fixture}):
            cursor.fetchone.side_effect = [role, (0,)]
            release.check_database({"options": options, "prefix": "tt_"})
            cursor.fetchone.side_effect = [role, (1,)]
            with self.assertRaisesRegex(release.ReleaseError, "另一工作室数据库"):
                release.check_database({"options": options, "prefix": "tt_"})


class BackupDumpTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            MEDIA_STORAGE_BACKEND="cos", COS_BUCKET="media-fixture-bucket", COS_REGION="media-region",
            COS_SECRET_ID="media-fixture-id", COS_SECRET_KEY="media-fixture-key",
        )
        connection = SimpleNamespace(settings_dict={
            "NAME": "tt_fixture", "HOST": "localhost", "PORT": "5432",
            "USER": "tt_fixture", "PASSWORD": "fixture",
        })
        self.client = MagicMock()
        self.client.list_objects.return_value = {}
        self.cos_config = MagicMock()
        self.modules = {
            "django": MagicMock(),
            "django.conf": SimpleNamespace(settings=self.settings),
            "django.db": SimpleNamespace(connection=connection),
            "qcloud_cos": SimpleNamespace(
                CosConfig=self.cos_config, CosS3Client=MagicMock(return_value=self.client),
            ),
        }
        self.created = []

    def dump(self, args, **kwargs):
        path = Path(args[args.index("-f") + 1])
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(any(key.startswith("BACKUP_COS_") for key in kwargs["env"]))
        path.write_bytes(b"synthetic database dump")
        self.created.append(path)
        return SimpleNamespace(returncode=0)

    def run_backup(self, environment):
        with patch.dict(os.environ, environment, clear=True), patch.dict(sys.modules, self.modules):
            with patch.object(subprocess, "run", side_effect=self.dump):
                runpy.run_path(str(REPO / "deploy/backup_daily.py"), run_name="__main__")

    def test_dedicated_backup_upload_works_with_local_media(self):
        self.settings.MEDIA_STORAGE_BACKEND = "local"
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.run_backup({
                "BACKUP_COS_BUCKET": "backup-fixture-bucket", "BACKUP_COS_REGION": "backup-region",
                "BACKUP_COS_SECRET_ID": "backup-fixture-id", "BACKUP_COS_SECRET_KEY": "backup-fixture-key",
            })

        self.cos_config.assert_called_once_with(
            Region="backup-region", SecretId="backup-fixture-id", SecretKey="backup-fixture-key",
        )
        self.client.put_object.assert_called_once_with(
            Bucket="backup-fixture-bucket", Key=f"backups/tt_fixture/{self.created[0].name}",
            Body=b"synthetic database dump", ContentType="application/octet-stream",
        )
        self.client.list_objects.assert_called_once_with(Bucket="backup-fixture-bucket", Prefix="backups/tt_fixture/")
        self.assertEqual(self.settings.MEDIA_STORAGE_BACKEND, "local")
        self.assertFalse(self.created[0].parent.exists())
        self.assertIn("BACKUP-OK", stdout.getvalue())
        self.assertNotIn("backup-fixture-id", stdout.getvalue())
        self.assertNotIn("backup-fixture-key", stdout.getvalue())

    def test_incomplete_dedicated_config_stops_before_dump_even_with_legacy_cos(self):
        for env in (
            {"BACKUP_COS_SECRET_ID": "private-id-marker"},
            {
                "BACKUP_COS_BUCKET": "backup-fixture-bucket", "BACKUP_COS_REGION": "backup-region",
                "BACKUP_COS_SECRET_ID": "private-id-marker", "BACKUP_COS_SECRET_KEY": " ",
            },
        ):
            with self.subTest(keys=tuple(env)):
                with self.assertRaisesRegex(SystemExit, "独立备份 COS 配置不完整") as error:
                    self.run_backup(env)
                self.assertNotIn("private-id-marker", str(error.exception))
                self.assertEqual(self.created, [])
                self.cos_config.assert_not_called()
                self.client.put_object.assert_not_called()

    def test_failed_upload_cleans_up_a_private_dump(self):
        def fail_upload(**kwargs):
            self.assertTrue(self.created[0].is_file())
            self.assertEqual(kwargs["Key"], f"backups/tt_fixture/{self.created[0].name}")
            self.assertEqual(kwargs["Body"], b"synthetic database dump")
            raise RuntimeError("fixture upload failed")

        self.client.put_object.side_effect = fail_upload
        with self.assertRaisesRegex(RuntimeError, "fixture upload failed"):
            self.run_backup({})
        self.cos_config.assert_called_once_with(
            Region="media-region", SecretId="media-fixture-id", SecretKey="media-fixture-key",
        )
        self.assertEqual(self.client.put_object.call_args.kwargs["Bucket"], "media-fixture-bucket")
        self.assertFalse(self.created[0].parent.exists())
        self.client.list_objects.assert_not_called()
