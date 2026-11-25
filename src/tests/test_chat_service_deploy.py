"""Exercise deployment CLIs without changing a server."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "chat-service"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.managed = self.root / "managed.json"
        self.sources = self.root / "sources.json"
        self.managed.write_text(json.dumps({
            "host": "db.test.invalid", "port": 5432, "database": "tt_chat_test",
            "sslmode": "require", "synapse": {"user": "chat_synapse", "password": "synth-password", "schema": "synapse"},
            "adapter": {"user": "chat_adapter", "password": "adapter-password", "schema": "chat_service"},
        }))
        self.sources.write_text(json.dumps({"service_url": "http://127.0.0.1:5291", "tenants": {
            "test-a": {"schema_name": "tenant_a", "secret": "a" * 48,
                       "source_url": "http://127.0.0.1:8000", "source_host": "a.test.invalid"},
        }}))
        self.managed.chmod(0o600)
        self.sources.chmod(0o600)

    def run_prepare(self, *extra):
        return subprocess.run([sys.executable, str(DEPLOY / "prepare.py"),
            "--root", str(self.root / "runtime"), "--managed-config", str(self.managed),
            "--sources", str(self.sources), "--server-name", "chat.test.invalid", *extra],
            capture_output=True, text=True)

    def test_dry_run_validates_without_writing_or_revealing_secrets(self):
        result = self.run_prepare("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "runtime").exists())
        self.assertNotIn("synth-password", result.stdout + result.stderr)
        self.assertNotIn("adapter-password", result.stdout + result.stderr)

    def test_rejects_world_readable_credentials(self):
        self.managed.chmod(0o644)
        result = self.run_prepare("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)

    def test_rejects_public_http_source_and_admin_fields(self):
        source = json.loads(self.sources.read_text())
        source["tenants"]["test-a"]["source_url"] = "http://public.test.invalid"
        self.sources.write_text(json.dumps(source))
        self.assertNotEqual(self.run_prepare("--dry-run").returncode, 0)
        managed = json.loads(self.managed.read_text())
        managed["admin_password"] = "DO-NOT-DEPLOY"
        self.managed.write_text(json.dumps(managed))
        result = self.run_prepare("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("DO-NOT-DEPLOY", result.stderr)

    def fake_database(self, locale="C", connectable=(("postgres", True), ("template1", True)),
                      tencent_setting=None, apply_tencent_option=True):
        driver = self.root / "driver/psycopg"
        driver.mkdir(parents=True)
        (driver / "__init__.py").write_text(f'''import json,os
from pathlib import Path
class Connection:
    def __init__(self,kw): self.kw=kw
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,params=None):
        if "current_database(), current_user" in sql:
            schema=self.kw["options"].split("search_path=")[1].split()[0]
            self.row=("tt_chat_test",self.kw["user"],"UTF8",{locale!r},"C",schema,160015)
        elif "tencentdb_log_unlogged_table" in sql:
            enabled="-c tencentdb_log_unlogged_table=off" in self.kw["options"]
            self.row=("off" if enabled and {apply_tencent_option!r} else {tencent_setting!r},)
        elif "rolsuper" in sql: self.row=(False,)
        elif "aclexplode" in sql: self.rows={connectable!r}
        else:
            self.row=(True,False,False)
            if "has_database_privilege" in sql: self.row += (bool({connectable!r}),)
        return self
    def fetchone(self): return self.row
    def fetchall(self): return self.rows
def connect(**kw):
    if os.environ.get("FAKE_CONNECTION_TRACE"):
        with open(os.environ["FAKE_CONNECTION_TRACE"],"a") as output: output.write(kw["sslmode"]+"\\n")
    if os.environ.get("FAKE_OPTIONS_TRACE"):
        with open(os.environ["FAKE_OPTIONS_TRACE"],"a") as output:
            output.write(json.dumps({{"user":kw["user"],"options":kw["options"]}})+"\\n")
    if os.environ.get("FAKE_SSL_UNAVAILABLE"): raise ConnectionError("SSL unavailable")
    assert kw["dbname"]=="tt_chat_test"
    assert kw["user"] in ("chat_synapse","chat_adapter")
    assert "default_transaction_read_only=on" in kw["options"]
    return Connection(kw)
''')
        (driver / "conninfo.py").write_text('def make_conninfo(**kwargs):\n    return " ".join(str(k)+"="+str(v) for k,v in kwargs.items())\n')
        return dict(os.environ, PYTHONPATH=str(driver.parent))

    def test_generated_synapse_uses_fixed_internal_bridge_message_rate_limit(self):
        result = self.normal_prepare(self.fake_database())
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        homeserver = json.loads((shared / "synapse/homeserver.yaml").read_text())
        self.assertEqual(homeserver.get("rc_message"), {"per_second": 20, "burst_count": 200})
        self.assertNotIn("rc_message", json.loads((shared / "service.json").read_text()))

    def test_rejects_arbitrary_message_rate_limit_configuration(self):
        managed = json.loads(self.managed.read_text())
        managed["rc_message"] = {"per_second": 999999, "burst_count": 999999}
        self.managed.write_text(json.dumps(managed))
        result = self.run_prepare("--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_tencent_native_unlogged_requires_strict_boolean(self):
        managed = json.loads(self.managed.read_text())
        for invalid in ("true", "false", 1, 0, None, {}):
            with self.subTest(value=invalid):
                managed["synapse_native_unlogged"] = invalid
                self.managed.write_text(json.dumps(managed))
                result = self.run_prepare("--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("boolean", result.stderr)

    def test_tencent_on_requires_explicit_opt_in_before_runtime_creation(self):
        environment = self.fake_database(tencent_setting="on")
        result = self.normal_prepare(environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("synapse_native_unlogged", result.stderr)
        self.assertFalse((self.root / "runtime").exists())

    def normal_prepare(self, environment, *extra):
        return subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid",
            *extra],
            env=environment, capture_output=True, text=True)

    def test_tencent_option_is_scoped_to_synapse_and_keeps_secrets_idempotent(self):
        managed = json.loads(self.managed.read_text())
        managed["synapse_native_unlogged"] = True
        self.managed.write_text(json.dumps(managed))
        environment = self.fake_database(tencent_setting="on")
        trace = self.root / "options-trace"
        environment["FAKE_OPTIONS_TRACE"] = str(trace)
        result = self.normal_prepare(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        homeserver = json.loads((shared / "synapse/homeserver.yaml").read_text())
        self.assertEqual(homeserver["database"]["args"]["options"],
            "-c search_path=synapse -c tencentdb_log_unlogged_table=off")
        self.assertNotIn("tencentdb", json.loads((shared / "service.json").read_text())["postgres_dsn"])
        connections = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(row["user"] == "chat_synapse" and "tencentdb_log_unlogged_table=off" in row["options"] for row in connections))
        self.assertTrue(all("tencentdb" not in row["options"] for row in connections if row["user"] == "chat_adapter"))
        files = [path for path in shared.rglob("*") if path.is_file()]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        self.assertEqual(self.normal_prepare(environment).returncode, 0)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})

    def test_tencent_opt_in_rejects_servers_without_the_known_parameter(self):
        managed = json.loads(self.managed.read_text())
        managed["synapse_native_unlogged"] = True
        self.managed.write_text(json.dumps(managed))
        result = self.normal_prepare(self.fake_database())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tencent", result.stderr)
        self.assertFalse((self.root / "runtime").exists())

    def test_tencent_opt_in_rejects_options_that_do_not_take_effect(self):
        managed = json.loads(self.managed.read_text())
        managed["synapse_native_unlogged"] = True
        self.managed.write_text(json.dumps(managed))
        result = self.normal_prepare(self.fake_database(tencent_setting="on", apply_tencent_option=False))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_explicit_disable_allowed_for_cloud_private_ip_and_loopback(self):
        managed = json.loads(self.managed.read_text())
        managed["sslmode"] = "disable"
        for host in ("172.17.111.3", "127.0.0.1", "localhost", "10.0.0.3", "192.168.1.3"):
            with self.subTest(host=host):
                managed["host"] = host
                self.managed.write_text(json.dumps(managed))
                result = self.run_prepare("--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_disable_rejected_for_public_ip_or_hostname(self):
        managed = json.loads(self.managed.read_text())
        managed["sslmode"] = "disable"
        for host in ("203.0.113.10", "db.test.invalid", "172.32.0.1", "169.254.1.1", "127.test.invalid"):
            with self.subTest(host=host):
                managed["host"] = host
                self.managed.write_text(json.dumps(managed))
                self.assertNotEqual(self.run_prepare("--dry-run").returncode, 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_explicit_disable_is_preserved_in_both_runtime_database_configs(self):
        managed = json.loads(self.managed.read_text())
        managed.update(host="172.17.111.3", sslmode="disable")
        self.managed.write_text(json.dumps(managed))
        environment = self.fake_database()
        result = subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"],
            env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        self.assertIn("sslmode=disable", json.loads((shared / "service.json").read_text())["postgres_dsn"])
        self.assertEqual(json.loads((shared / "synapse/homeserver.yaml").read_text())["database"]["args"]["sslmode"], "disable")

    def test_tls_connection_failure_does_not_retry_with_disabled_tls(self):
        environment = self.fake_database()
        trace = self.root / "connection-trace"
        environment.update(FAKE_CONNECTION_TRACE=str(trace), FAKE_SSL_UNAVAILABLE="1")
        result = subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"],
            env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(trace.read_text().splitlines(), ["require"])
        self.assertFalse((self.root / "runtime").exists())

    def test_prepares_private_configuration_once_and_refuses_silent_rotation(self):
        environment = self.fake_database()
        command = [sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"]
        first = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        shared = self.root / "runtime/shared"
        service = json.loads((shared / "service.json").read_text())
        homeserver = json.loads((shared / "synapse/homeserver.yaml").read_text())
        self.assertIn("search_path=chat_service", service["postgres_dsn"])
        self.assertEqual(homeserver["database"]["args"]["options"], "-c search_path=synapse")
        self.assertEqual(homeserver["registration_shared_secret"], service["registration_shared_secret"])
        self.assertEqual(homeserver["federation_domain_whitelist"], [])
        files = [p for p in shared.rglob("*") if p.is_file()]
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        repeated = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in files))
        changed = json.loads(self.sources.read_text())
        changed["tenants"]["test-a"]["secret"] = "b" * 48
        self.sources.write_text(json.dumps(changed))
        refused = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(original, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})

    def test_browser_opt_in_is_only_emitted_for_registered_tenant_and_internal_source(self):
        from chat_service.config import load_config

        sources = json.loads(self.sources.read_text())
        first = sources['tenants']['test-a']
        second = {**first, 'schema_name': 'tenant_b', 'secret': 'b' * 48, 'source_host': 'b.test.invalid'}
        sources['tenants']['test-b'] = second
        first['browser'] = {'audience': 'https://chat.test.invalid', 'origins': ['https://a.test.invalid']}
        self.sources.write_text(json.dumps(sources))
        result = self.prepare_with_database((('postgres', True), ('template1', True)))
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / 'runtime' / 'shared'
        self.assertEqual(load_config(shared / 'service.json')['tenants']['test-a']['browser'], first['browser'])
        a = json.loads((shared / 'bridge-tenant_a.json').read_text())
        b = json.loads((shared / 'bridge-tenant_b.json').read_text())
        self.assertEqual(a['tenants']['tenant_a']['browser'], first['browser'])
        self.assertNotIn('browser', b['tenants']['tenant_b'])
        second['browser'] = first['browser']
        self.sources.write_text(json.dumps(sources))
        self.assertNotEqual(self.run_prepare('--dry-run').returncode, 0)
        del second['browser']
        first['source_url'] = 'https://public.test.invalid'
        self.sources.write_text(json.dumps(sources))
        self.assertNotEqual(self.run_prepare('--dry-run').returncode, 0)

    def test_realtime_tenant_keys_are_private_scoped_and_do_not_replace_existing_configs(self):
        sources = json.loads(self.sources.read_text())
        first = sources["tenants"]["test-a"]
        first["realtime"] = {"url": "http://127.0.0.1:4194", "secret": "realtime-a_" * 4}
        sources["tenants"]["test-b"] = {
            **first, "schema_name": "tenant_b", "secret": "b" * 48, "source_host": "b.test.invalid",
            "realtime": {**first["realtime"], "secret": "realtime-b_" * 4},
        }
        self.sources.write_text(json.dumps(sources))
        environment = self.fake_database()
        flag = "--realtime-tenant-keys"
        result = self.normal_prepare(environment, flag)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        service = json.loads((shared / "service.json").read_text())["tenants"]
        snapshots = {}
        for key, tenant in sources["tenants"].items():
            schema = tenant["schema_name"]
            bridge_file = shared / f"bridge-{schema}.json"
            bridge = json.loads(bridge_file.read_text())["tenants"]
            secret = tenant["realtime"]["secret"]
            self.assertEqual(bridge, {schema: {"key_id": key, "secret": tenant["secret"], "realtime_secret": secret}})
            self.assertEqual(service[key]["realtime"]["secret"], secret)
            self.assertEqual(bridge_file.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(secret, result.stdout + result.stderr)
            snapshots[bridge_file] = (bridge_file.read_bytes(), bridge_file.stat().st_mtime_ns)
        self.assertEqual(self.normal_prepare(environment, flag).returncode, 0)
        result = self.normal_prepare(environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit secret/config rotation", result.stderr)
        for path, before in snapshots.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_realtime_tenant_keys_require_distinct_base64url_secrets_for_every_tenant(self):
        sources = json.loads(self.sources.read_text())
        first = sources["tenants"]["test-a"]
        first["realtime"] = {"url": "http://127.0.0.1:4194", "secret": "realtime-a_" * 4}
        second = {**first, "schema_name": "tenant_b", "secret": "b" * 48, "source_host": "b.test.invalid"}
        sources["tenants"]["test-b"] = second
        for secret in (None, "!" * 32, "x" * 129, first["realtime"]["secret"]):
            with self.subTest(secret=secret):
                if secret is None:
                    del second["realtime"]
                else:
                    second["realtime"] = {**first["realtime"], "secret": secret}
                self.sources.write_text(json.dumps(sources))
                result = self.run_prepare("--dry-run", "--realtime-tenant-keys")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Tenant realtime keys", result.stderr)
                self.assertNotIn(first["realtime"]["secret"], result.stdout + result.stderr)
                self.assertFalse((self.root / "runtime").exists())
        second["realtime"]["secret"] = "realtime-b_" * 4
        self.sources.write_text(json.dumps(sources))
        environment = self.fake_database()
        result = self.normal_prepare(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        shared = self.root / "runtime/shared"
        result = self.normal_prepare(environment, "--realtime-tenant-keys")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit secret/config rotation", result.stderr)
        for tenant in sources["tenants"].values():
            schema = tenant["schema_name"]
            bridge = json.loads((shared / f"bridge-{schema}.json").read_text())
            self.assertNotIn("realtime_secret", bridge["tenants"][schema])

    def prepare_with_database(self, connectable):
        environment = self.fake_database(connectable=connectable)
        return subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"],
            env=environment, capture_output=True, text=True)

    def test_prepare_preserves_explicit_chat_pool_size_without_implicit_rotation(self):
        managed = json.loads(self.managed.read_text())
        self.managed.write_text(json.dumps({**managed, "postgres_pool_size": 4}))
        environment = self.fake_database(connectable=(("postgres", True), ("template1", True)))
        command = [sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"]
        runtime = self.root / "runtime/shared/service.json"
        for _ in range(2):
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(runtime.read_text())["postgres_pool_size"], 4)
        self.managed.write_text(json.dumps({**managed, "postgres_pool_size": 0}))
        changed = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(json.loads(runtime.read_text())["postgres_pool_size"], 4)

    def test_preflight_allows_public_connect_to_two_system_maintenance_databases(self):
        result = self.prepare_with_database((("postgres", True), ("template1", True)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "runtime/shared/service.json").exists())

    def test_preflight_rejects_connect_to_any_business_database(self):
        result = self.prepare_with_database((("postgres", True), ("template1", True), ("tt_other_business", True)))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_preflight_rejects_non_public_connect_even_to_system_database(self):
        result = self.prepare_with_database((("postgres", False),))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_preflight_rejects_wrong_locale_before_creating_runtime(self):
        environment = self.fake_database(locale="en_US.UTF-8")
        result = subprocess.run([sys.executable, str(DEPLOY / "prepare.py"), "--root", str(self.root / "runtime"),
            "--managed-config", str(self.managed), "--sources", str(self.sources), "--server-name", "chat.test.invalid"],
            env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "runtime").exists())


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        import http.server
        import threading

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "chat"
        self.source = self.base / "source"
        (self.source / "src/chat_service").mkdir(parents=True)
        (self.source / "src/chat_service/__main__.py").write_text("print('test')\n")
        (self.source / "src/pyproject.toml").write_text('[project]\nname="test-chat"\nversion="0.0.0"\n')
        (self.source / "src/uv.lock").write_text("version = 1\n")
        import shutil
        shutil.copytree(DEPLOY, self.source / "deploy/chat-service")
        (self.root / "shared").mkdir(parents=True)
        for name in ("service.json", "compose.env"):
            (self.root / "shared" / name).write_text("{}")
            (self.root / "shared" / name).chmod(0o600)
        (self.root / "shared/service.json").write_text(json.dumps({
            "postgres_dsn": "deployment-fixture", "schema": "chat_service", "tenants": {"fixture-a": {}},
        }))
        self.units = self.base / "units"
        self.cgroup = self.base / "cgroup"
        self.proc = self.base / "proc"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "actions.log"
        fake = self.bin / "fake"
        fake.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
base=Path(os.environ["FAKE_BASE"])
name=Path(sys.argv[0]).name
with (base/"actions.log").open("a") as f: f.write(name+" "+" ".join(sys.argv[1:])+"\\n")
if name=="uv":
    if "--managed-python" in sys.argv and "UV_PYTHON_PREFERENCE" in os.environ: sys.exit(2)
    p=Path.cwd()/".venv/bin";p.mkdir(parents=True);(p/"python").symlink_to(base/"bin/fake")
if name=="python" and (base/"fail-init").exists(): sys.exit(1)
if name=="systemctl":
    if sys.argv[1]=="restart":
        if (base/"fail-once").exists():
            (base/"fail-once").unlink();sys.exit(1)
        unit=sys.argv[2]; group=base/"cgroup/system.slice"/unit;group.mkdir(parents=True,exist_ok=True)
        pid="11" if unit=="chat-service.service" else "12"
        (group/"cgroup.procs").write_text(pid)
        p=base/"proc"/pid;p.mkdir(parents=True,exist_ok=True)
        if (p/"cwd").is_symlink(): (p/"cwd").unlink()
        (p/"cwd").symlink_to((base/"chat/current/src").resolve())
        (p/"cmdline").write_bytes(b"python\\0-m\\0chat_service\\0")
    elif sys.argv[1]=="show": print("/system.slice/"+sys.argv[2])
    elif sys.argv[1]=="is-active": print("active")
''')
        fake.chmod(0o755)
        for name in ("uv", "systemctl", "docker"):
            (self.bin / name).symlink_to(fake)
        driver = self.base / "driver/psycopg"
        driver.mkdir(parents=True)
        (driver / "__init__.py").write_text('''
sql = object()
class Connection:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query, params=None):
        if query == "SELECT to_regclass(%s)": self.row = (None,)
        elif "pg_try_advisory_lock" in query: self.row = (True,)
        else: raise AssertionError("Unexpected deployment ownership query")
        return self
    def fetchone(self): return self.row
def connect(dsn, **kwargs):
    assert dsn == "deployment-fixture"
    return Connection()
''')
        self.env = dict(os.environ, FAKE_BASE=str(self.base), PYTHONPATH=str(driver.parent))

        fixture = self

        class Health(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                value = {"versions": ["v1.11"]} if "versions" in handler.path else {"code": 0, "msg": "ok", "data": {}}
                if "versions" not in handler.path and (fixture.base / "fail-envelope").exists() and (fixture.root / "current").resolve().name == "test-two":
                    value = {"code": 503, "msg": "not ready", "data": None}
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps(value).encode())
            def log_message(*args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.health = f"http://127.0.0.1:{server.server_port}"

    def release(self, action, version=None, *extra):
        command = [sys.executable, str(DEPLOY / "release.py"), action, "--root", str(self.root),
            "--systemd-dir", str(self.units), "--cgroup-root", str(self.cgroup), "--proc-root", str(self.proc),
            "--systemctl", str(self.bin / "systemctl"), "--docker", str(self.bin / "docker"),
            "--uv", str(self.bin / "uv"), "--service-url", self.health, "--matrix-url", self.health,
            "--health-timeout", "0", "--user", "ubuntu", "--group", "ubuntu"]
        if version:
            command += ["--version", version]
        if action == "publish":
            command += ["--source", str(self.source)]
        return subprocess.run([*command, *extra], env=self.env, text=True, capture_output=True)

    def test_publish_rollback_and_failure_restore_keep_shared_data(self):
        marker = self.root / "shared/media-sentinel"
        marker.write_text("keep-media-and-cursors")
        first = self.release("publish", "test-one")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.release("publish", "test-two")
        self.assertEqual(second.returncode, 0, second.stderr)
        rolled = self.release("rollback")
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual((self.root / "current").resolve().name, "test-one")
        (self.base / "fail-once").touch()
        failed = self.release("publish", "test-three")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "test-one")
        self.assertEqual(marker.read_text(), "keep-media-and-cursors")
        self.assertIn(str(self.root / "current/src"), (self.units / "chat-worker.service").read_text())
        self.assertNotIn("pgrep", self.log.read_text())

    def test_publish_installs_optional_pool_only_for_a_release_that_declares_it(self):
        self.assertEqual(self.release("publish", "base").returncode, 0)
        project = self.source / "src/pyproject.toml"
        with project.open("a") as handle:
            handle.write('[project.optional-dependencies]\nchat-pool = ["psycopg[pool]"]\n')
        pooled = self.release("publish", "pooled")
        self.assertEqual(pooled.returncode, 0, pooled.stderr)
        uv_runs = [line.split() for line in self.log.read_text().splitlines() if line.startswith("uv sync ")]
        self.assertEqual(len(uv_runs), 2)
        self.assertNotIn("--extra", uv_runs[0])
        self.assertEqual(uv_runs[1][-2:], ["--extra", "chat-pool"])
        self.assertEqual(self.release("rollback").returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "base")

    def test_dry_run_does_not_mutate_or_execute_tools(self):
        result = self.release("publish", "dry-run", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "current").exists())
        self.assertFalse(self.log.exists())

    def test_rejects_cgroup_process_running_old_code(self):
        self.assertEqual(self.release("publish", "test-one").returncode, 0)
        fake = self.bin / "fake"
        fake.write_text(fake.read_text().replace(
            '(p/"cwd").symlink_to((base/"chat/current/src").resolve())',
            '(p/"cwd").symlink_to(base/"chat/releases/test-one/src")'))
        failed = self.release("publish", "test-two")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "test-one")

    def test_initialization_failure_does_not_switch_current(self):
        self.assertEqual(self.release("publish", "test-one").returncode, 0)
        (self.base / "fail-init").touch()
        result = self.release("publish", "test-two")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "test-one")

    def test_first_activation_failure_stops_units_and_restores_empty_selection(self):
        (self.base / "fail-once").touch()
        result = self.release("publish", "first-broken")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "current").is_symlink())
        self.assertFalse((self.units / "chat-service.service").exists())
        self.assertIn("systemctl stop chat-worker.service", self.log.read_text())

    def test_failed_rollback_restores_current_release(self):
        self.assertEqual(self.release("publish", "test-one").returncode, 0)
        self.assertEqual(self.release("publish", "test-two").returncode, 0)
        (self.base / "fail-once").touch()
        result = self.release("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "test-two")
        self.assertEqual((self.root / "previous").resolve().name, "test-one")

    def test_http_200_failure_envelope_rejects_release(self):
        self.assertEqual(self.release("publish", "test-one").returncode, 0)
        (self.base / "fail-envelope").touch()
        result = self.release("publish", "test-two")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "current").resolve().name, "test-one")

    def test_publish_clears_inherited_python_preference_and_uses_managed_cli(self):
        self.env["UV_PYTHON_PREFERENCE"] = "only-system"
        result = self.release("publish", "managed-python")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "current").resolve().name, "managed-python")
        self.assertIn("--managed-python", self.log.read_text())
