import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx
import psycopg
from psycopg import sql

from chat_service.config import ServiceError, load_config
from chat_service.core_utils import ID_START
from chat_service.engine import Engine
from chat_service.matrix import Matrix
from chat_service.protocol import sign_request
from chat_service.server import Server
from chat_service.store import Store


@unittest.skipUnless(importlib.util.find_spec('psycopg_pool'), 'Run with the chat-pool extra')
class ChatStorePoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get('CHAT_SERVICE_TEST_DSN', 'host=127.0.0.1 port=5432 user=postgres dbname=postgres')
        try:
            db = psycopg.connect(cls.dsn)
        except psycopg.OperationalError:
            raise unittest.SkipTest('Local PostgreSQL is unavailable') from None
        with db:
            cls.schema = 'chat_pool_test_' + uuid.uuid4().hex
            cls.owner = db.execute('SELECT current_user').fetchone()[0]
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        cls.addClassCleanup(cls.drop_schema)

    @classmethod
    def drop_schema(cls):
        with psycopg.connect(cls.dsn) as db:
            owner = db.execute('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s', (cls.schema,)).fetchone()
            if owner != (cls.owner,) or not cls.schema.startswith('chat_pool_test_'):
                raise AssertionError('Unexpected fixture schema owner')
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def test_reuses_backend_without_retaining_session_state_or_failed_changes(self):
        with Store(self.dsn, self.schema, pool_size=1) as store:
            with store.connect() as db:
                original = db.execute("SELECT pg_backend_pid() pid, current_setting('application_name') app").fetchone()
                db.execute("SET application_name TO 'pooled-fixture-only'")
                db.execute('CREATE TEMP TABLE pooled_fixture_only (id int)')
                db.execute('SELECT pg_advisory_lock(73015216)')
            with self.assertRaisesRegex(RuntimeError, 'rollback test'):
                with store.connect() as db:
                    state = db.execute('''SELECT pg_backend_pid() pid, current_schema() schema,
                        current_setting('application_name') app, to_regclass('pooled_fixture_only') temp''').fetchone()
                    self.assertEqual(state, {**original, 'schema': self.schema, 'temp': None})
                    db.execute('CREATE TABLE pooled_rollback_only (id int)')
                    raise RuntimeError('rollback test')
            with store.connect() as db:
                self.assertIsNone(db.execute("SELECT to_regclass('pooled_rollback_only') AS ddl").fetchone()['ddl'])
                with psycopg.connect(self.dsn) as other:
                    self.assertTrue(other.execute('SELECT pg_try_advisory_lock(73015216)').fetchone()[0])

    def test_http_admission_and_tenant_signatures_survive_reuse(self):
        with Store(self.dsn, self.schema, pool_size=1) as store:
            store.initialize()
            for tenant, cursor in (('alpha', 41), ('bravo', 78)):
                with store.tenant(tenant) as db:
                    db.execute('UPDATE sync_state SET cursor=%s WHERE tenant_id=%s', (cursor, tenant))
            engine = Engine(store, Matrix('http://127.0.0.1:9999', 'fixture'), {
                'alpha': {'secret': 'alpha-secret'}, 'bravo': {'secret': 'bravo-secret'},
            })
            with Server(('127.0.0.1', 0), engine) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
                        with store.connect():
                            busy = client.get('/healthz')
                            self.assertEqual((busy.status_code, busy.json()['code']), (503, 503))
                            with store.dedicated_connection() as lock_db:
                                self.assertEqual(lock_db.execute('SELECT 1 AS n').fetchone()['n'], 1)
                        self.assertEqual(client.get('/healthz').json()['code'], 0)
                        for tenant, cursor in (('alpha', 41), ('bravo', 78), ('alpha', 41)):
                            grant = sign_request(tenant + '-secret', tenant, 'GET', '/v1/status', b'')
                            self.assertEqual(client.get('/v1/status', headers=grant).json()['data']['cursor'], cursor)
                        forged = sign_request('alpha-secret', 'bravo', 'GET', '/v1/status', b'')
                        response = client.get('/v1/status', headers=forged)
                        self.assertEqual((response.status_code, response.json()['data']), (403, None))
                finally:
                    server.shutdown()
                    thread.join()

    def test_unread_returns_busy_when_local_authority_checkout_times_out(self):
        with Store(self.dsn, self.schema, pool_size=1) as store:
            store.initialize()
            tenant = 'orders-fixture'
            with store.tenant(tenant) as db:
                db.execute("UPDATE sync_state SET phase='changes' WHERE tenant_id=%s", (tenant,))
            config = {'secret': 'orders-secret', 'realtime': {
                'url': 'http://127.0.0.1:9999/notify', 'secret': 'fixture', 'tenant': 'orders_fixture',
            }}
            engine = Engine(store, Matrix('http://127.0.0.1:9999', 'fixture'), {tenant: config})
            engine.core.handle(tenant, {'operation': 'activate', 'payload': {'minimum_cursor': 0}})
            with store.tenant(tenant) as db:
                db.execute('''INSERT INTO rooms (tenant_id,conversation_id,room_id,kind,metadata)
                    VALUES (%s,%s,'!unread-fixture:local','order','{}')''', (tenant, ID_START))
                db.execute('''INSERT INTO members (tenant_id,conversation_id,member_id,local_user_id,active,metadata)
                    VALUES (%s,%s,1,7,true,'{}')''', (tenant, ID_START))

            authority = engine.core._authority

            def while_pool_busy(*args, **kwargs):
                with store.connect():
                    return authority(*args, **kwargs)

            with patch.object(engine.core, '_authority', side_effect=while_pool_busy):
                with self.assertRaisesRegex(ServiceError, '内部沟通数据库暂时繁忙') as error:
                    engine.core.handle(tenant, {'operation': 'unread', 'viewer_id': 7})
                self.assertEqual(error.exception.status, 503)

    def test_background_tick_can_borrow_with_one_connection_while_holding_session_lock(self):
        with Store(self.dsn, self.schema, pool_size=1) as store:
            store.initialize()
            with store.tenant('tick-fixture') as db:
                db.execute("UPDATE sync_state SET phase='changes' WHERE tenant_id=%s", ('tick-fixture',))
            tenant = {'secret': 'tick-secret', 'realtime': {
                'url': 'http://127.0.0.1:9999/notify', 'secret': 'fixture', 'tenant': 'tick_fixture',
            }}
            engine = Engine(store, Matrix('http://127.0.0.1:9999', 'fixture'), {'tick-fixture': tenant})
            self.assertTrue(engine.core.handle('tick-fixture', {
                'operation': 'activate', 'payload': {'minimum_cursor': 0},
            })['active'])
            self.assertFalse(engine.core.tick('tick-fixture')['has_more'])
            key = f'tt_chat_core_tick:{self.schema}:tick-fixture'
            with psycopg.connect(self.dsn) as db:
                self.assertTrue(db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (key,)).fetchone()[0])


class ChatStorePoolConfigTests(unittest.TestCase):
    def test_runtime_setting_is_optional_and_strictly_bounded(self):
        with TemporaryDirectory() as temp:
            private = Path(temp) / 'service.json'
            private.touch(mode=0o600)
            data = {'matrix_url': 'http://127.0.0.1:9999', 'registration_shared_secret': 'fixture',
                    'postgres_dsn': 'fixture', 'tenants': {'alpha': {'secret': 'alpha-secret'}}}
            private.write_text(json.dumps(data))
            self.assertEqual(load_config(private)['postgres_pool_size'], 0)
            private.write_text(json.dumps({**data, 'postgres_pool_size': 4}))
            self.assertEqual(load_config(private)['postgres_pool_size'], 4)
            for invalid in (True, '4', -1, 9, None):
                with self.subTest(invalid=invalid):
                    private.write_text(json.dumps({**data, 'postgres_pool_size': invalid}))
                    with self.assertRaisesRegex(ValueError, 'postgres_pool_size'):
                        load_config(private)
