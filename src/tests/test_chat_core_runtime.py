"""Check independent chat worker and deployment behavior through their public entry points."""

import unittest
import os
import uuid


class CoreWorkerTests(unittest.TestCase):
    def test_active_core_never_reads_the_legacy_source_and_once_drains_pages(self):
        from types import SimpleNamespace
        from chat_service.worker import Worker

        class Core:
            def __init__(self):
                self.calls = []

            def is_active(self, tenant):
                return tenant == 'a'

            def tick(self, tenant):
                self.calls.append(tenant)
                return {'processed': 2, 'has_more': len(self.calls) < 2}

        class Source:
            def page(self, *args):
                raise AssertionError('active core must not read the legacy journal')

        core = Core()
        engine = SimpleNamespace(core=core, store=SimpleNamespace(status=lambda tenant: {'tenant': tenant, 'phase': 'changes', 'cursor': 0}))
        worker = Worker(engine, sources={'a': Source()})
        self.assertEqual(worker.once('a'), {'tenant': 'a', 'phase': 'changes', 'cursor': 0})
        self.assertEqual(core.calls, ['a', 'a'])

    def test_core_rate_limit_defers_only_that_tenant(self):
        from types import SimpleNamespace
        from chat_service.config import ServiceError
        from chat_service.worker import Worker

        class Core:
            def is_active(self, tenant):
                return True

            def tick(self, tenant):
                if tenant == 'a':
                    raise ServiceError('rate limited', 503, retry_after=7)
                return {'processed': 1, 'has_more': False}

        worker = Worker(SimpleNamespace(core=Core()), sources={'a': object(), 'b': object()}, clock=lambda: 10)
        with self.assertRaises(ServiceError):
            worker.step('a')
        self.assertEqual(worker.step('a')['retry_after'], 7)
        self.assertTrue(worker.step('b')['completed'])

    def test_once_reports_durable_core_backoff_without_spinning(self):
        from types import SimpleNamespace
        from chat_service.config import ServiceError
        from chat_service.worker import Worker

        calls = []
        core = SimpleNamespace(is_active=lambda tenant: True,
            tick=lambda tenant: calls.append(tenant) or {'processed': 0, 'has_more': True, 'retry_after': 3})
        worker = Worker(SimpleNamespace(core=core), sources={'a': object()})
        with self.assertRaises(ServiceError) as caught:
            worker.once('a')
        self.assertEqual(caught.exception.retry_after, 3)
        self.assertEqual(calls, ['a'])

    def test_core_tenant_without_legacy_source_remains_in_the_worker_schedule(self):
        from types import SimpleNamespace
        from chat_service.worker import Worker

        calls = []
        core = SimpleNamespace(is_active=lambda tenant: True,
            tick=lambda tenant: calls.append(tenant) or {'processed': 0, 'has_more': False})
        worker = Worker(SimpleNamespace(core=core, tenants={'a': {'secret': 's' * 48}}))
        for tenant in worker.sources:
            worker.step(tenant)
        self.assertEqual(calls, ['a'])


class CorePrepareTests(unittest.TestCase):
    def setUp(self):
        from tests.test_chat_service_deploy import PrepareTests
        PrepareTests.setUp(self)

    def test_realtime_config_is_scoped_to_source_schema_and_preserved_idempotently(self):
        import json
        from tests.test_chat_service_deploy import PrepareTests

        source = json.loads(self.sources.read_text())
        source['tenants']['test-a']['realtime'] = {'url': 'http://127.0.0.1:4194', 'secret': 'r' * 48}
        self.sources.write_text(json.dumps(source))
        result = PrepareTests.normal_prepare(self, PrepareTests.fake_database(self))
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = self.root / 'runtime/shared/service.json'
        value = json.loads(runtime.read_text())
        self.assertEqual(value['tenants']['test-a']['realtime'],
                         {'url': 'http://127.0.0.1:4194', 'secret': 'r' * 48, 'tenant': 'tenant_a'})
        self.assertNotIn('r' * 48, result.stdout + result.stderr)
        first = (runtime.read_bytes(), runtime.stat().st_mtime_ns)
        result = PrepareTests.normal_prepare(self, dict(os.environ,
            PYTHONPATH=str(self.root / 'driver')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(first, (runtime.read_bytes(), runtime.stat().st_mtime_ns))

    def test_realtime_rejects_public_http_and_client_chosen_tenant(self):
        import json
        from tests.test_chat_service_deploy import PrepareTests

        source = json.loads(self.sources.read_text())
        for realtime in ({'url': 'http://8.8.8.8', 'secret': 'r' * 48},
                         {'url': 'http://127.0.0.1:4194', 'secret': 'r' * 48, 'tenant': 'other'},
                         {'url': 'http://127.0.0.1:4194', 'secret': ''}):
            with self.subTest(realtime=realtime):
                source['tenants']['test-a']['realtime'] = realtime
                self.sources.write_text(json.dumps(source))
                self.assertNotEqual(PrepareTests.run_prepare(self, '--dry-run').returncode, 0)


class CoreConfigTests(unittest.TestCase):
    def test_runtime_realtime_rejects_invalid_address_secret_and_gateway_tenant(self):
        import json
        from pathlib import Path
        import tempfile
        from chat_service.config import load_config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'service.json'
            value = {'matrix_url': 'http://127.0.0.1:5289', 'registration_shared_secret': 'registration',
                     'postgres_dsn': 'dbname=test', 'tenants': {'a': {'secret': 's' * 48}}}
            for realtime in ({'url': 'http://8.8.8.8', 'secret': 'r' * 48, 'tenant': 'tenant_a'},
                             {'url': 'http://127.0.0.1:4194', 'secret': '', 'tenant': 'tenant_a'},
                             {'url': 'http://127.0.0.1:4194', 'secret': 'r' * 48, 'tenant': '../bad'},
                             {'url': 'http://127.0.0.1:4194', 'secret': 'r' * 48, 'tenant': 'tenant_a', 'x': True}):
                with self.subTest(realtime=realtime):
                    value['tenants']['a']['realtime'] = realtime
                    path.write_text(json.dumps(value))
                    path.chmod(0o600)
                    with self.assertRaises(ValueError):
                        load_config(path)


class CoreReleaseTests(unittest.TestCase):
    def setUp(self):
        import json
        import psycopg
        from psycopg import sql
        from tests.test_chat_service_deploy import ReleaseTests

        ReleaseTests.setUp(self)
        self.release = ReleaseTests.release.__get__(self)
        self.env.pop('PYTHONPATH', None)
        self.config = {'postgres_dsn': 'host=127.0.0.1 port=5432 dbname=postgres user=postgres connect_timeout=3',
                       'schema': 'runtime_guard_' + uuid.uuid4().hex[:12],
                       'tenants': {'test-a': {'secret': 'a' * 48}}}
        self.schema = self.config['schema']
        (self.root / 'shared/service.json').write_text(json.dumps(self.config))
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            self.owner = connection.execute('SELECT current_user').fetchone()[0]
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL('CREATE TABLE {}.core_control (tenant_id text PRIMARY KEY, active boolean NOT NULL)')
                               .format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL('INSERT INTO {}.core_control VALUES (%s,false)')
                               .format(sql.Identifier(self.schema)), ('test-a',))
        self.addCleanup(self.drop_owned_schema)

    def drop_owned_schema(self):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.config['postgres_dsn']) as connection:
            owner = connection.execute('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s',
                                       (self.schema,)).fetchone()
            self.assertEqual(owner, (self.owner,))
            self.assertTrue(self.schema.startswith('runtime_guard_'))
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def set_active(self, active, tenant='test-a'):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.config['postgres_dsn']) as connection:
            connection.execute(sql.SQL('INSERT INTO {}.core_control VALUES (%s,%s) '
                'ON CONFLICT (tenant_id) DO UPDATE SET active=EXCLUDED.active').format(sql.Identifier(self.schema)),
                (tenant, active))

    def require_protocol(self, version, tenant='test-a'):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.config['postgres_dsn']) as connection:
            connection.execute(sql.SQL('ALTER TABLE {}.core_control ADD COLUMN IF NOT EXISTS required_protocol integer NOT NULL DEFAULT 1').format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL('UPDATE {}.core_control SET required_protocol=%s WHERE tenant_id=%s').format(sql.Identifier(self.schema)), (version, tenant))

    def test_protocol_two_state_refuses_core_one_on_fresh_machine(self):
        self.set_active(True, 'removed-from-config')
        self.require_protocol(2, 'removed-from-config')
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 1\n')
        result = self.release('publish', 'old-core-fresh')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('protocol', result.stderr.lower())
        self.assertFalse((self.root / 'current').exists())

    def test_manifest_uses_literal_core_protocol_without_executing_source(self):
        import json

        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\nraise RuntimeError("must not execute for metadata")\n')
        result = self.release('publish', 'authority-two')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.root / 'current/manifest.json').read_text())['core_protocol'], 2)

    def test_required_protocol_blocks_rollback_to_core_one_without_restarting_units(self):
        self.publish_legacy_then_core()
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\n')
        self.assertEqual(self.release('publish', 'authority-two').returncode, 0)
        self.require_protocol(2)
        before = self.log.read_bytes()
        blocked = self.release('rollback')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('protocol', blocked.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'authority-two')
        self.assertEqual(self.log.read_bytes(), before)

    def test_compatible_protocol_two_rollback_preserves_persistent_state(self):
        import psycopg
        from psycopg import sql

        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\n')
        self.assertEqual(self.release('publish', 'authority-one').returncode, 0)
        self.set_active(True)
        self.require_protocol(2)
        self.assertEqual(self.release('publish', 'authority-two').returncode, 0)
        marker = self.root / 'shared/persistent-chat-state'
        marker.write_text('messages and media remain')
        restored = self.release('rollback')
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'authority-one')
        self.assertEqual(marker.read_text(), 'messages and media remain')
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            self.assertEqual(connection.execute(sql.SQL('SELECT active,required_protocol FROM {}.core_control WHERE tenant_id=%s').format(sql.Identifier(self.schema)), ('test-a',)).fetchone(), (True, 2))

    def test_core_one_guard_obeys_tenant_lock_before_possible_protocol_upgrade(self):
        import psycopg

        self.publish_legacy_then_core()
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            connection.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', ('tt_chat:' + self.schema + ':test-a',))
            blocked = self.release('publish', 'old-core-upgrade-race')
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn('busy', blocked.stderr)
            self.assertEqual((self.root / 'current').resolve().name, 'core-two')

    def test_invalid_source_protocol_is_rejected_without_execution(self):
        for source in ('CORE_PROTOCOL = True\n', 'CORE_PROTOCOL = 0\n', 'CORE_PROTOCOL = 1 + 1\n', 'CORE_PROTOCOL = 2\nCORE_PROTOCOL = 1\n'):
            with self.subTest(source=source):
                (self.source / 'src/chat_service/core.py').write_text(source)
                blocked = self.release('publish', 'invalid-' + uuid.uuid4().hex)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn('protocol', blocked.stderr)
                self.assertFalse((self.root / 'current').exists())

    def test_invalid_required_protocol_does_not_allow_a_core_release(self):
        self.set_active(True)
        self.require_protocol(0)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\n')
        blocked = self.release('publish', 'malformed-required-protocol')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('required protocol', blocked.stderr)
        self.assertFalse((self.root / 'current').exists())

    def test_failed_core_activation_reuses_held_lock_for_safe_recovery(self):
        self.publish_legacy_then_core()
        (self.base / 'fail-once').touch()
        failed = self.release('publish', 'failed-compatible-core')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.root / 'current').resolve().name, 'core-two')
        self.assertNotIn('needs attention', failed.stderr)

    def test_failed_activation_cannot_automatically_restore_incompatible_core(self):
        self.publish_legacy_then_core()
        self.require_protocol(2)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\n')
        (self.base / 'fail-once').touch()
        before = self.log.read_bytes()
        failed = self.release('publish', 'failed-authority-core')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('safe previous service recovery needs attention', failed.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'failed-authority-core')
        self.assertEqual(self.log.read_bytes()[len(before):].decode().count('systemctl restart'), 1)

    def test_inactive_paused_core_refuses_lower_protocol_without_current(self):
        import json
        import psycopg
        from psycopg import sql

        self.set_active(False, 'removed-paused-tenant')
        self.require_protocol(4, 'removed-paused-tenant')
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            connection.execute(sql.SQL('ALTER TABLE {}.core_control ADD COLUMN writes_paused boolean NOT NULL DEFAULT false').format(sql.Identifier(self.schema)))
            connection.execute(sql.SQL('UPDATE {}.core_control SET writes_paused=true WHERE tenant_id=%s').format(sql.Identifier(self.schema)), ('removed-paused-tenant',))
        (self.root / 'shared/service.json').write_text(json.dumps(dict(self.config, tenants={})))
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 3\n')
        blocked = self.release('publish', 'pre-activation-lower-protocol')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('protocol', blocked.stderr.lower())
        self.assertFalse((self.root / 'current').exists())
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 4\n')
        compatible = self.release('publish', 'pre-activation-compatible')
        self.assertEqual(compatible.returncode, 0, compatible.stderr)
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            self.assertEqual(connection.execute(sql.SQL('SELECT active,writes_paused,required_protocol FROM {}.core_control WHERE tenant_id=%s').format(sql.Identifier(self.schema)), ('removed-paused-tenant',)).fetchone(), (False, True, 4))

    def publish_legacy_then_core(self):
        self.assertEqual(self.release('publish', 'legacy-one').returncode, 0)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 1\n')
        result = self.release('publish', 'core-two')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.set_active(True)

    def test_active_core_refuses_legacy_rollback_before_current_or_units_change(self):
        self.publish_legacy_then_core()
        before = self.log.read_bytes()
        result = self.release('rollback')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('core', result.stderr.lower())
        self.assertEqual((self.root / 'current').resolve().name, 'core-two')
        self.assertNotIn('systemctl restart', self.log.read_bytes()[len(before):].decode())
        self.assertEqual((self.root / 'previous').resolve().name, 'legacy-one')

    def test_fresh_machine_without_current_still_refuses_active_database_legacy(self):
        self.set_active(True)
        before = self.log.read_bytes() if self.log.exists() else b''
        result = self.release('publish', 'legacy-on-fresh-machine')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('core', result.stderr.lower())
        self.assertFalse((self.root / 'current').exists())
        self.assertNotIn('systemctl restart', self.log.read_bytes()[len(before):].decode())

    def test_inactive_core_can_return_to_legacy_after_database_lock_check(self):
        self.publish_legacy_then_core()
        self.set_active(False)
        result = self.release('rollback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'legacy-one')

    def test_removed_active_tenant_still_blocks_legacy_without_current(self):
        import json

        self.set_active(False)
        self.set_active(True, 'removed-from-config')
        value = dict(self.config, tenants={})
        (self.root / 'shared/service.json').write_text(json.dumps(value))
        result = self.release('publish', 'legacy-with-removed-tenant')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'current').exists())

    def test_core_capable_versions_roll_back_without_removing_shared_state(self):
        self.publish_legacy_then_core()
        result = self.release('publish', 'core-three')
        self.assertEqual(result.returncode, 0, result.stderr)
        sentinel = self.root / 'shared/core-state-sentinel'
        sentinel.write_text('accepted messages remain')
        result = self.release('rollback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'core-two')
        self.assertEqual(sentinel.read_text(), 'accepted messages remain')

    def test_busy_persistent_tenant_lock_prevents_an_activation_race(self):
        import psycopg

        self.publish_legacy_then_core()
        self.set_active(False)
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            locked = connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',
                ('tt_chat:' + self.schema + ':test-a',)).fetchone()[0]
            self.assertTrue(locked)
            result = self.release('rollback')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('busy', result.stderr.lower())
            self.assertEqual((self.root / 'current').resolve().name, 'core-two')

    def test_legacy_recovery_is_allowed_when_the_core_table_does_not_exist(self):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.config['postgres_dsn']) as connection:
            count = connection.execute(sql.SQL('SELECT count(*) FROM {}.core_control')
                                       .format(sql.Identifier(self.schema))).fetchone()[0]
            self.assertEqual(count, 1)
            connection.execute(sql.SQL('DROP TABLE {}.core_control').format(sql.Identifier(self.schema)))
        result = self.release('publish', 'pre-core-database')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'pre-core-database')

    def test_database_connection_failure_does_not_change_current(self):
        import json

        self.publish_legacy_then_core()
        value = dict(self.config, postgres_dsn='host=127.0.0.1 port=1 dbname=postgres user=postgres connect_timeout=1')
        (self.root / 'shared/service.json').write_text(json.dumps(value))
        result = self.release('rollback')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'current').resolve().name, 'core-two')

    def test_invalid_persistent_flag_is_not_coerced_to_inactive(self):
        import psycopg
        from psycopg import sql

        with psycopg.connect(self.config['postgres_dsn']) as connection:
            connection.execute(sql.SQL('ALTER TABLE {}.core_control ALTER COLUMN active TYPE text USING active::text')
                               .format(sql.Identifier(self.schema)))
        result = self.release('publish', 'malformed-core-database')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ownership', result.stderr)
        self.assertFalse((self.root / 'current').exists())

    def test_restricted_service_role_can_guard_without_administrator_permissions(self):
        import json
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        role = 'runtime_guard_role_' + uuid.uuid4().hex[:12]
        password = uuid.uuid4().hex
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
            self.addCleanup(self.drop_fixture_role, role)
            connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(self.schema), sql.Identifier(role)))
            connection.execute(sql.SQL('GRANT SELECT ON {}.core_control TO {}')
                               .format(sql.Identifier(self.schema), sql.Identifier(role)))
        value = dict(self.config, postgres_dsn=make_conninfo(self.config['postgres_dsn'], user=role, password=password))
        (self.root / 'shared/service.json').write_text(json.dumps(value))
        self.set_active(True)
        blocked = self.release('publish', 'restricted-blocked')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('Active chat core', blocked.stderr)
        self.require_protocol(2)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 1\n')
        old_core = self.release('publish', 'restricted-old-core')
        self.assertNotEqual(old_core.returncode, 0)
        self.assertIn('protocol', old_core.stderr)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 2\n')
        current = self.release('publish', 'restricted-current-core')
        self.assertEqual(current.returncode, 0, current.stderr)
        self.set_active(False)
        self.require_protocol(4)
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            connection.execute(sql.SQL('ALTER TABLE {}.core_control ADD COLUMN writes_paused boolean NOT NULL DEFAULT true').format(sql.Identifier(self.schema)))
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 3\n')
        paused_old = self.release('publish', 'restricted-paused-old-core')
        self.assertNotEqual(paused_old.returncode, 0)
        self.assertIn('protocol', paused_old.stderr)
        (self.source / 'src/chat_service/core.py').write_text('CORE_PROTOCOL = 4\n')
        allowed = self.release('publish', 'restricted-allowed')
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual((self.root / 'current').resolve().name, 'restricted-allowed')
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            connection.execute(sql.SQL('REVOKE SELECT ON {}.core_control FROM {}')
                               .format(sql.Identifier(self.schema), sql.Identifier(role)))
        refused = self.release('publish', 'restricted-denied')
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((self.root / 'current').resolve().name, 'restricted-allowed')

    def drop_fixture_role(self, role):
        import psycopg
        from psycopg import sql

        self.assertTrue(role.startswith('runtime_guard_role_'))
        with psycopg.connect(self.config['postgres_dsn'], autocommit=True) as connection:
            self.assertEqual(connection.execute('SELECT rolname FROM pg_roles WHERE rolname=%s', (role,)).fetchone(), (role,))
            connection.execute(sql.SQL('REVOKE ALL ON {}.core_control FROM {}')
                               .format(sql.Identifier(self.schema), sql.Identifier(role)))
            connection.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(sql.Identifier(self.schema), sql.Identifier(role)))
            connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


class CoreControlTests(unittest.TestCase):
    def setUp(self):
        import http.server
        import json
        from pathlib import Path
        import tempfile
        import threading
        from chat_service.protocol import verify_request

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source_latest, self.mirror_cursor = 12, 12
        self.fenced, self.phase = True, 'changes'
        self.maintenance, self.paused, self.resume_ack = True, True, True
        self.pending, self.drain_after, self.status_calls = 0, None, 0
        self.status_mutation, self.health_failure, self.envelope_code = {}, '', 0
        self.commands, self.paths = [], []
        self.business_failure = False
        self.active = False
        self.activate_ack = True
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                if handler.path in ('/healthz', '/_matrix/client/versions'):
                    handler.send_response(503 if fixture.health_failure == handler.path else 200)
                    handler.end_headers()
                    handler.wfile.write(json.dumps({'versions': ['v1.11']} if 'versions' in handler.path else {'code': 0}).encode())
                    return
                verify_request({'a': 's' * 48}, dict(handler.headers), 'GET', handler.path, b'')
                fixture.paths.append(handler.path)
                if handler.path == '/v1/status':
                    data = {'phase': fixture.phase, 'cursor': fixture.mirror_cursor}
                else:
                    data = {'latest': fixture.source_latest, 'cursor': 0, 'has_more': False,
                            'events': [], 'core_write_fenced': fixture.fenced, 'chat_paused': fixture.maintenance}
                handler.respond(data)

            def do_POST(handler):
                raw = handler.rfile.read(int(handler.headers['Content-Length']))
                verify_request({'a': 's' * 48}, dict(handler.headers), 'POST', handler.path, raw)
                value = json.loads(raw)
                operation = value['operation']
                if operation == 'activate':
                    fixture.commands.append(value)
                    fixture.active = fixture.activate_ack
                elif operation == 'pause':
                    fixture.commands.append(value)
                    fixture.paused = True
                elif operation == 'resume':
                    fixture.commands.append(value)
                    if fixture.resume_ack: fixture.paused = False
                if operation == 'status':
                    fixture.status_calls += 1
                    if fixture.drain_after is not None and fixture.status_calls >= fixture.drain_after:
                        fixture.pending = 0
                handler.respond({'active': fixture.active, 'writes_paused': fixture.paused,
                                 'cursor': 0, 'pending_messages': fixture.pending,
                                 'pending_notifications': 0, 'next_id': 1_000_000_000, **fixture.status_mutation})

            def respond(handler, data):
                handler.send_response(200)
                handler.end_headers()
                value = {'code': 403, 'msg': 'denied', 'data': data} if fixture.business_failure else {
                    'code': fixture.envelope_code, 'msg': '', 'data': data}
                handler.wfile.write(json.dumps(value).encode())

            def log_message(*args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        (self.root / 'shared').mkdir()
        self.config = self.root / 'shared/service.json'
        self.config.write_text(json.dumps({'matrix_url': self.url,
            'registration_shared_secret': 'registration', 'postgres_dsn': 'dbname=unused',
            'tenants': {'a': {'secret': 's' * 48, 'source_url': self.url, 'source_host': 'source.test.invalid'}}}))
        self.config.chmod(0o600)
        release = self.root / 'releases/core-ready'
        release.mkdir(parents=True)
        (release / 'manifest.json').write_text(json.dumps({'core_protocol': 4}))
        (self.root / 'current').symlink_to(release)

    def control(self, action, *extra, use_default_url=False):
        from pathlib import Path
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[2] / 'deploy/chat-service/core_control.py'
        endpoint = [] if use_default_url else ['--service-url', self.url]
        return subprocess.run([sys.executable, str(script), action, '--root', str(self.root),
            '--tenant', 'a', *endpoint, *extra], capture_output=True, text=True)

    def test_parallel_instance_control_uses_registered_private_port(self):
        import json

        identity = self.root / 'shared/synapse-runtime.json'
        identity.write_text(json.dumps({'runtime': 'native', 'data_dir': str(self.root / 'shared/synapse'),
            'unit_prefix': 'ttt-chat-prod', 'matrix_port': 5288, 'service_port': self.server.server_port}))
        identity.chmod(0o600)
        wrong = self.control('pause', '--dry-run', '--service-url', 'http://127.0.0.1:5291', use_default_url=True)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertFalse(self.commands)
        correct = self.control('pause', use_default_url=True)
        self.assertEqual(correct.returncode, 0, correct.stderr)
        self.assertEqual([call['operation'] for call in self.commands], ['pause'])

    def test_pause_before_activation_is_explicit_and_preserves_inactive_state(self):
        import json

        result = self.control('pause')
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value['active'])
        self.assertTrue(value['writes_paused'])

    def test_activation_requires_both_source_maintenance_and_core_pause(self):
        self.maintenance = False
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)
        self.maintenance, self.paused = True, False
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)

    def test_drain_waits_for_both_queues_and_never_resumes(self):
        import json

        self.pending, self.drain_after = 3, 3
        drained = self.control('drain', '--timeout-seconds', '3', '--poll-interval', '0.01')
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertGreaterEqual(self.status_calls, 3)
        self.assertEqual(json.loads(drained.stdout)['pending_messages'], 0)
        self.assertTrue(self.paused)
        self.assertFalse(self.commands)

    def test_drain_timeout_or_unpaused_state_remains_closed(self):
        self.pending = 1
        timed_out = self.control('drain', '--timeout-seconds', '0.15', '--poll-interval', '0.01')
        self.assertNotEqual(timed_out.returncode, 0)
        self.assertTrue(self.paused)
        self.assertFalse(self.commands)
        self.pending, self.paused = 0, False
        self.assertNotEqual(self.control('drain').returncode, 0)
        self.assertFalse(self.commands)

    def test_resume_needs_explicit_business_verification_before_any_network(self):
        self.active = True
        blocked = self.control('resume')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('--confirm-verified', blocked.stderr)
        self.assertFalse(self.commands)
        self.assertFalse(self.paths)
        self.assertTrue(self.paused)

    def test_resume_rechecks_active_pause_empty_health_and_source_fence(self):
        cases = ({'active': False}, {'paused': False}, {'pending': 1},
                 {'health_failure': '/healthz'}, {'health_failure': '/_matrix/client/versions'},
                 {'maintenance': False}, {'fenced': False})
        for changes in cases:
            with self.subTest(changes=changes):
                self.active, self.paused, self.pending = True, True, 0
                self.health_failure, self.maintenance, self.fenced = '', True, True
                for key, value in changes.items(): setattr(self, key, value)
                self.assertNotEqual(self.control('resume', '--confirm-verified').returncode, 0)
                self.assertFalse(self.commands)

    def test_resume_only_opens_after_checks_and_explicit_success_ack(self):
        import json

        self.active = True
        self.resume_ack = False
        failed = self.control('resume', '--confirm-verified')
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(self.paused)
        self.resume_ack = True
        resumed = self.control('resume', '--confirm-verified')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(json.loads(resumed.stdout)['writes_paused'])
        self.assertFalse(self.paused)
        self.assertEqual(self.commands[-1], {'operation': 'resume', 'viewer_id': 0, 'query': {}, 'payload': {}})

    def test_control_rejects_coerced_flags_and_queue_counters(self):
        for mutation in ({'active': 1}, {'writes_paused': 'true'}, {'pending_messages': False},
                         {'pending_notifications': -1}, {'pending_messages': '0'}):
            with self.subTest(mutation=mutation):
                self.status_mutation = mutation
                self.assertNotEqual(self.control('status').returncode, 0)
                self.assertTrue(self.paused)
                self.assertFalse(self.commands)

    def test_dry_run_and_invalid_wait_options_cannot_open_chat(self):
        for action in ('pause', 'drain', 'resume'):
            self.assertEqual(self.control(action, '--dry-run').returncode, 0)
        for value in ('nan', 'inf', '0', '-1', '3601'):
            self.assertNotEqual(self.control('drain', '--timeout-seconds', value).returncode, 0)
        self.assertFalse(self.paths)
        self.assertFalse(self.commands)
        self.assertTrue(self.paused)

    def test_control_rejects_non_integer_success_code_and_missing_pause_ack(self):
        for invalid in (False, True, '0', 0.0):
            with self.subTest(code=invalid):
                self.envelope_code = invalid
                self.assertNotEqual(self.control('status').returncode, 0)
        self.envelope_code = 0
        self.status_mutation = {'writes_paused': False}
        failed = self.control('pause')
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(self.paused)

    def test_activation_uses_verified_source_cursor_and_preserves_secrets(self):
        result = self.control('activate', '--expected-cursor', '12')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commands, [{'operation': 'activate', 'viewer_id': 0, 'query': {}, 'payload': {'minimum_cursor': 12}}])
        self.assertNotIn('s' * 48, result.stdout + result.stderr)
        self.assertTrue(any(path.startswith('/api/internal-chat/service/export/') for path in self.paths))

    def test_activation_refuses_unfenced_behind_or_wrong_expected_source(self):
        self.fenced = False
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)
        self.fenced = True
        self.mirror_cursor = 11
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)
        self.mirror_cursor = 12
        self.assertNotEqual(self.control('activate', '--expected-cursor', '0').returncode, 0)
        self.assertFalse(self.commands)
        self.phase = 'message'
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)

    def test_business_error_envelope_is_not_treated_as_activation_success(self):
        self.business_failure = True
        self.assertNotEqual(self.control('activate').returncode, 0)
        self.assertFalse(self.commands)
        self.assertNotEqual(self.control('status').returncode, 0)

    def test_dry_run_does_not_check_network_or_mutate_ownership(self):
        result = self.control('activate', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not checked', result.stdout)
        self.assertFalse(self.paths)
        self.assertFalse(self.commands)
        self.assertFalse((self.root / '.release.lock').exists())

    def test_legacy_current_or_shared_release_lock_prevents_activation(self):
        import fcntl
        import json

        current = (self.root / 'current').resolve()
        for unsupported in (0, 1, 2, 3):
            (current / 'manifest.json').write_text(json.dumps({'core_protocol': unsupported}))
            self.assertNotEqual(self.control('activate').returncode, 0)
            self.assertFalse(self.commands)
        (current / 'manifest.json').write_text(json.dumps({'core_protocol': 4}))
        with (self.root / '.release.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.control('activate')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Another chat deployment', result.stderr)
            self.assertFalse(self.commands)

    def test_activation_requires_explicit_active_acknowledgement(self):
        self.activate_ack = False
        result = self.control('activate')
        self.assertNotEqual(result.returncode, 0)


    def test_repeated_activation_is_read_only_when_core_is_already_active(self):
        first = self.control('activate')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.commands), 1)
        self.mirror_cursor = 0
        repeated = self.control('activate')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(len(self.commands), 1)
