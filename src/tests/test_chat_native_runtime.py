"""Exercise native Synapse preparation and release through deployment CLIs."""

import json
import subprocess
import sys
import unittest

from tests import test_chat_service_deploy as deploy_tests

DEPLOY = deploy_tests.DEPLOY


class NativePrepareTests(unittest.TestCase):
    setUp = deploy_tests.PrepareTests.setUp
    fake_database = deploy_tests.PrepareTests.fake_database
    normal_prepare = deploy_tests.PrepareTests.normal_prepare

    def native_prepare(self, environment, *extra):
        return subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name",
            "chat.test.invalid", "--synapse-runtime", "native", *extra],
            env=environment, capture_output=True, text=True)

    def test_native_paths_loopback_permissions_and_repeated_prepare_preserve_keys(self):
        environment = self.fake_database()
        ca = self.root / "source-ca.pem"
        ca.write_text("SYNTHETIC-CA")
        managed = json.loads(self.managed.read_text())
        managed.update(sslrootcert=str(ca), sslmode="verify-full")
        self.managed.write_text(json.dumps(managed))
        result = self.native_prepare(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        config = json.loads((shared / "synapse/homeserver.yaml").read_text())
        self.assertEqual(config["listeners"][0]["bind_addresses"], ["127.0.0.1"])
        self.assertEqual(config["listeners"][0]["port"], 5289)
        for key, name in (("media_store_path", "media_store"), ("signing_key_path", "server.signing.key"),
                          ("log_config", "log.config")):
            self.assertEqual(config[key], str(shared / "synapse" / name))
        self.assertEqual(config["database"]["args"]["sslrootcert"], str(shared / "synapse/managed-ca.pem"))
        runtime = json.loads((shared / "synapse-runtime.json").read_text())
        self.assertEqual(runtime, {"runtime": "native", "data_dir": str(shared / "synapse")})
        files = [p for p in shared.rglob("*") if p.is_file()]
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        self.assertEqual(self.native_prepare(environment).returncode, 0)
        self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))
        rejected = self.normal_prepare(environment)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("runtime", rejected.stderr.lower())
        self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})


    def test_native_prepare_refuses_unprotected_identity_without_touching_keys(self):
        environment = self.fake_database()
        self.assertEqual(self.native_prepare(environment).returncode, 0)
        shared = self.root / "runtime/shared"
        key = shared / "synapse/server.signing.key"
        original = key.read_bytes()
        identity = shared / "synapse-runtime.json"
        identity.chmod(0o644)
        result = self.native_prepare(environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertEqual(key.read_bytes(), original)

    def test_parallel_prepare_keeps_instance_ports_in_its_private_runtime(self):
        environment = self.fake_database()
        flags = ("--unit-prefix", "ttt-chat-prod", "--matrix-port", "5288", "--service-port", "5292")
        rejected = self.normal_prepare(environment, *flags)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())
        prepared = self.native_prepare(environment, *flags)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        shared = self.root / "runtime/shared"
        identity = shared / "synapse-runtime.json"
        instance = json.loads(identity.read_text())
        self.assertEqual(instance["unit_prefix"], "ttt-chat-prod")
        self.assertEqual(instance["matrix_port"], 5288)
        self.assertEqual(instance["service_port"], 5292)
        self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
        listener = json.loads((shared / "synapse/homeserver.yaml").read_text())["listeners"][0]
        self.assertEqual((listener["port"], listener["bind_addresses"]), (5288, ["127.0.0.1"]))
        self.assertEqual(json.loads((shared / "service.json").read_text())["matrix_url"], "http://127.0.0.1:5288")
        original = identity.read_bytes()
        self.assertEqual(self.native_prepare(environment, *flags).returncode, 0)
        self.assertNotEqual(self.native_prepare(environment, *flags, "--service-port", "5293").returncode, 0)
        self.assertEqual(identity.read_bytes(), original)



class NativeReleaseTests(unittest.TestCase):
    release = deploy_tests.ReleaseTests.release

    def setUp(self):
        deploy_tests.ReleaseTests.setUp(self)
        shared = self.root / "shared"
        (shared / "synapse").mkdir()
        for name, value in (("synapse-runtime.json", {"runtime": "native", "data_dir": str(shared / "synapse")}),
                            ("synapse/homeserver.yaml", {
                                "listeners": [{"port": 5289, "bind_addresses": ["127.0.0.1"]}],
                                "media_store_path": str(shared / "synapse/media_store"),
                                "signing_key_path": str(shared / "synapse/server.signing.key"),
                                "log_config": str(shared / "synapse/log.config"),
                            })):
            path = shared / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        fake = self.bin / "fake"
        script = fake.read_text().replace(
            'p=Path.cwd()/".venv/bin";p.mkdir(parents=True);(p/"python").symlink_to(base/"bin/fake")',
            'if sys.argv[1] == "venv": p=Path(sys.argv[-1])/"bin"\n'
            '    elif sys.argv[1] == "sync": p=Path.cwd()/".venv/bin"\n'
            '    else: sys.exit(0)\n'
            '    p.mkdir(parents=True);(p/"python").symlink_to(base/"bin/fake")')
        script = script.replace('pid="11" if unit=="chat-service.service" else "12"',
            'pid={"chat-service.service":"11", "chat-worker.service":"12", "chat-synapse.service":"13"}[unit]')
        script = script.replace('(base/"chat/current/src").resolve()',
            '(base/"chat/current"/("synapse" if unit=="chat-synapse.service" else "src")).resolve()')
        script = script.replace('(p/"cmdline").write_bytes(b"python\\0-m\\0chat_service\\0")',
            '(p/"cmdline").write_bytes((str((base/"chat/current/synapse/.venv/bin/python").absolute())+'
            '"\\0-m\\0synapse.app.homeserver\\0").encode() if unit=="chat-synapse.service" else b"python\\0-m\\0chat_service\\0")')
        fake.write_text(script)

    def publish(self, version, *extra):
        return self.release("publish", version, "--synapse-runtime", "native", *extra)

    def use_parallel_instance(self):
        shared = self.root / "shared"
        for name, updates in (
            ("synapse-runtime.json", {"unit_prefix": "ttt-chat-prod", "matrix_port": 5288, "service_port": 5292}),
            ("synapse/homeserver.yaml", {"listeners": [{"port": 5288, "bind_addresses": ["127.0.0.1"]}]}),
            ("service.json", {"matrix_url": "http://127.0.0.1:5288"}),
        ):
            path = shared / name
            path.write_text(json.dumps({**json.loads(path.read_text()), **updates}))
        script = self.bin / "fake"
        script.write_text(script.read_text().replace(
            'unit=sys.argv[2];', 'named=sys.argv[2]; unit=named.removeprefix("ttt-chat-prod-"); '
            'unit="chat-"+unit;').replace('/unit;group.mkdir', '/named;group.mkdir'))

    def test_parallel_publish_and_rollback_leave_default_units_alone(self):
        self.use_parallel_instance()
        self.units.mkdir()
        sentinel = self.units / "chat-service.service"
        sentinel.write_text("existing test chat service")
        first = self.publish("named-one")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("--port 5292", (self.units / "ttt-chat-prod-service.service").read_text())
        self.assertIn("After=network-online.target ttt-chat-prod-service.service",
                      (self.units / "ttt-chat-prod-worker.service").read_text())
        second = self.publish("named-two")
        self.assertEqual(second.returncode, 0, second.stderr)
        rolled = self.release("rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual((self.root / "current").resolve().name, "named-one")
        self.assertEqual(sentinel.read_text(), "existing test chat service")
        actions = self.log.read_text()
        self.assertNotIn("systemctl restart chat-", actions)
        for kind in ("synapse", "service", "worker"):
            self.assertIn(f"systemctl restart ttt-chat-prod-{kind}.service", actions)

    def test_parallel_name_collision_refuses_to_touch_other_root(self):
        self.use_parallel_instance()
        self.units.mkdir()
        sentinel = self.units / "ttt-chat-prod-service.service"
        sentinel.write_text("[Service]\nWorkingDirectory=/opt/another-chat/current/src\n")
        result = self.publish("collision")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another", result.stderr.lower())
        self.assertFalse(self.log.exists())
        self.assertEqual(sentinel.read_text(), "[Service]\nWorkingDirectory=/opt/another-chat/current/src\n")

    def test_native_publish_rollback_and_failure_restore_only_own_units(self):
        sentinel = self.root / "shared/synapse/media-sentinel"
        sentinel.write_text("preserve")
        first = self.publish("native-one")
        self.assertEqual(first.returncode, 0, first.stderr)
        manifest = json.loads((self.root / "current/manifest.json").read_text())
        self.assertEqual(manifest["synapse_runtime"], "native")
        self.assertEqual(len(manifest["synapse_lock_sha256"]), 64)
        self.assertTrue((self.root / "current/synapse/.venv/bin/python").exists())
        self.assertIn("synapse.app.homeserver", (self.units / "chat-synapse.service").read_text())
        second = self.publish("native-two")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.release("rollback").returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "native-one")
        (self.base / "fail-once").touch()
        self.assertNotEqual(self.publish("native-broken").returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "native-one")
        self.assertEqual(sentinel.read_text(), "preserve")
        log = self.log.read_text()
        self.assertNotIn("docker ", log)
        self.assertNotIn("tuantuantong.", log)
        self.assertIn("--require-hashes --only-binary", log)
        self.assertIn("systemctl restart chat-synapse.service", log)

    def test_first_native_activation_failure_stops_all_native_units_preserving_shared(self):
        marker = self.root / "shared/synapse/server.signing.key"
        marker.write_text("private synthetic seed")
        marker.chmod(0o600)
        (self.base / "fail-once").touch()
        result = self.publish("first-broken")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "current").is_symlink())
        self.assertFalse((self.units / "chat-synapse.service").exists())
        self.assertEqual(marker.read_text(), "private synthetic seed")
        log = self.log.read_text()
        for unit in ("chat-synapse.service", "chat-service.service", "chat-worker.service"):
            self.assertIn("systemctl stop " + unit, log)
            self.assertIn("systemctl disable " + unit, log)
        self.assertNotIn("docker ", log)

    def test_native_failed_rollback_restores_current_without_changing_previous(self):
        self.assertEqual(self.publish("native-one").returncode, 0)
        self.assertEqual(self.publish("native-two").returncode, 0)
        (self.base / "fail-once").touch()
        result = self.release("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "native-two")
        self.assertEqual((self.root / "previous").resolve().name, "native-one")
        self.assertNotIn("docker ", self.log.read_text())

    def test_native_publish_default_docker_and_crossmode_rollback_fail_before_tools(self):
        result = self.release("publish", "wrong-default")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.publish("native-one").returncode, 0)
        old = self.root / "releases/docker-old"
        old.mkdir()
        (old / "manifest.json").write_text('{"version":"docker-old"}')
        (self.root / "previous").symlink_to(old)
        before = self.log.read_text()
        result = self.release("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text(), before)
        self.assertEqual((self.root / "current").resolve().name, "native-one")

    def test_changed_native_lock_rejected_before_switch_or_unit_action(self):
        self.assertEqual(self.publish("native-one").returncode, 0)
        self.assertEqual(self.publish("native-two").returncode, 0)
        lock = self.root / "previous/deploy/chat-service/synapse-requirements.txt"
        lock.write_text(lock.read_text() + "# tampered\n")
        before = self.log.read_text()
        result = self.release("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log.read_text(), before)
        self.assertEqual((self.root / "current").resolve().name, "native-two")

    def test_old_native_synapse_process_in_own_cgroup_rejects_new_release(self):
        self.assertEqual(self.publish("native-one").returncode, 0)
        fake = self.bin / "fake"
        script = fake.read_text()
        needle = '(p/"cwd").symlink_to((base/"chat/current"/("synapse" if unit=="chat-synapse.service" else "src")).resolve())'
        self.assertIn(needle, script)
        fake.write_text(script.replace(needle,
            '(p/"cwd").symlink_to((base/"chat/releases/native-one/synapse") if '
            'unit=="chat-synapse.service" else (base/"chat/current/src").resolve())'))
        result = self.publish("native-two")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "native-one")

    def test_native_dryrun_and_source_package_include_isolated_runtime(self):
        result = self.publish("native-dry", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())
        result = self.publish("native-one")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("synapse_runtime.py", "synapse-requirements.in", "synapse-requirements.txt", "chat-synapse.service.in"):
            self.assertEqual((self.root / "current/deploy/chat-service" / name).read_bytes(), (DEPLOY / name).read_bytes())
        self.assertTrue((self.root / "current/src/.venv/bin/python").exists())

    def test_native_paused_protocol_four_rejects_lower_target_without_starting_units(self):
        import uuid
        import psycopg
        from psycopg import sql
        from tests import test_chat_core_runtime as core_tests

        self.env.pop("PYTHONPATH", None)
        self.schema = "runtime_guard_" + uuid.uuid4().hex[:12]
        self.config = {"postgres_dsn": "host=127.0.0.1 port=5432 dbname=postgres user=postgres connect_timeout=3",
                       "schema": self.schema, "tenants": {}}
        (self.root / "shared/service.json").write_text(json.dumps(self.config))
        with psycopg.connect(self.config["postgres_dsn"]) as connection:
            self.owner = connection.execute("SELECT current_user").fetchone()[0]
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(core_tests.CoreReleaseTests.drop_owned_schema.__get__(self))
        with psycopg.connect(self.config["postgres_dsn"]) as connection:
            connection.execute(sql.SQL("CREATE TABLE {}.core_control (tenant_id text PRIMARY KEY, "
                "active boolean NOT NULL, required_protocol integer NOT NULL, writes_paused boolean NOT NULL)")
                .format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL("INSERT INTO {}.core_control VALUES ('removed-paused', false, 4, true)")
                .format(sql.Identifier(self.schema)))
        declaration = self.source / "src/chat_service/core.py"
        declaration.write_text("CORE_PROTOCOL = 3\n")
        result = self.publish("native-too-old")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protocol", result.stderr.lower())
        self.assertFalse((self.root / "current").exists())
        self.assertNotIn("systemctl restart", self.log.read_text())
        declaration.write_text("CORE_PROTOCOL = 4\n")
        result = self.publish("native-capable")
        self.assertEqual(result.returncode, 0, result.stderr)
