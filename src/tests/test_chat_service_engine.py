import unittest

from chat_service.protocol import ProtocolError, sign_request, verify_request


class SignatureTests(unittest.TestCase):
    def test_signed_tenant_cannot_be_changed_or_replayed_with_other_body(self):
        headers = sign_request('secret-a', 'studio-a', 'POST', '/v1/read?a=1', b'{}', now=100)
        keys = {'studio-a': 'secret-a', 'studio-b': 'secret-b'}
        self.assertEqual(verify_request(keys, headers, 'POST', '/v1/read?a=1', b'{}', now=100), 'studio-a')
        for changed in ({**headers, 'X-TTT-Chat-Key': 'studio-b'}, {**headers, 'Cookie': 'session=x'}):
            with self.assertRaises(ProtocolError):
                verify_request(keys, changed, 'POST', '/v1/read?a=1', b'{}', now=100)
        with self.assertRaises(ProtocolError):
            verify_request(keys, headers, 'POST', '/v1/read?a=2', b'{}', now=100)
        with self.assertRaises(ProtocolError):
            verify_request(keys, headers, 'POST', '/v1/read?a=1', b'{}', now=161)

class MalformedSignatureTests(unittest.TestCase):
    def test_noncanonical_signature_headers_raise_protocol_error(self):
        headers = sign_request('secret', 'a', 'GET', '/v1/status', b'', now=100)
        for signature in ('', '0' * 63, '0' * 65, 'G' * 64, 'A' * 64, '\u00e9' * 64, b'0' * 64, None):
            with self.subTest(signature=signature), self.assertRaises(ProtocolError):
                verify_request({'a': 'secret'}, {**headers, 'X-TTT-Chat-Signature': signature},
                               'GET', '/v1/status', b'', now=100)


class SourceTests(unittest.TestCase):
    def test_source_rejects_http_200_business_failure(self):
        import httpx
        from chat_service.config import ServiceError
        from chat_service.worker import Source
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'code': 403, 'msg': 'denied', 'data': {'rows': []}})))
        source = Source('a', {'secret': 's', 'source_url': 'http://127.0.0.1:8000'}, client)
        with self.assertRaises(ServiceError):
            source.page('snapshot', {'kind': 'conversations', 'after': 0, 'limit': 100})

class RuntimeURLTests(unittest.TestCase):
    def test_public_and_reserved_http_addresses_are_rejected(self):
        from chat_service.config import internal_url
        for address in ('http://8.8.8.8', 'http://203.0.113.8', 'http://0.0.0.0', 'http://169.254.1.1'):
            with self.assertRaises(ValueError, msg=address):
                internal_url(address)
        self.assertEqual(internal_url('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000')


class ServiceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import uuid
        import psycopg
        from psycopg import sql
        cls.dsn = os.environ.get('CHAT_SERVICE_TEST_DSN', 'host=127.0.0.1 port=5432 user=postgres dbname=postgres')
        cls.schema = 'chat_engine_test_' + uuid.uuid4().hex
        try:
            with psycopg.connect(cls.dsn) as db:
                cls.owner = db.execute('SELECT current_user').fetchone()[0]
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        except psycopg.OperationalError:
            raise unittest.SkipTest('Local PostgreSQL is unavailable; set CHAT_SERVICE_TEST_DSN')

    @classmethod
    def tearDownClass(cls):
        import psycopg
        from psycopg import sql
        with psycopg.connect(cls.dsn) as db:
            row = db.execute('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s', (cls.schema,)).fetchone()
            if row != (cls.owner,) or not cls.schema.startswith('chat_engine_test_'):
                raise AssertionError('Refusing to clean schema not owned by fixture')
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        import httpx
        import threading
        from chat_service.engine import Engine
        from chat_service.matrix import Matrix
        from chat_service.server import Server
        from chat_service.store import Store
        self.store = Store(self.dsn, self.schema)
        self.store.initialize()
        with self.store.connect() as db:
            db.execute('TRUNCATE sync_state,accounts,rooms,members,messages,imported_changes')
        self.rooms, self.aliases, self.events, self.media = {}, {}, {}, {}
        self.fail_text = None
        self.rate_limit_text = None
        self.send_budget = None
        self.matrix_requests = []
        self.matrix_http = httpx.Client(transport=httpx.MockTransport(self.matrix_reply))
        self.engine = Engine(self.store, Matrix('http://127.0.0.1:9999', 'reg-secret', self.matrix_http), {'a': {'secret': 'a-secret'}, 'b': {'secret': 'b-secret'}})
        self.server = Server(('127.0.0.1', 0), self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = httpx.Client(base_url='http://127.0.0.1:' + str(self.server.server_port), trust_env=False)

    def tearDown(self):
        self.http.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.matrix_http.close()

    def matrix_reply(self, request):
        import json
        import httpx
        from urllib.parse import unquote
        path = unquote(request.url.path)
        body = json.loads(request.content) if request.content and request.headers.get('content-type', '').startswith('application/json') else {}
        self.matrix_requests.append((request.method, path, body))
        if path.endswith('/register'):
            return httpx.Response(200, json={'nonce': 'service-test-nonce'} if request.method == 'GET' else {'user_id': '@registered:matrix.test'})
        if path.endswith('/login'):
            name = body['identifier']['user']
            return httpx.Response(200, json={'user_id': '@' + name + ':matrix.test', 'access_token': 'token:' + name})
        if '/directory/room/' in path:
            alias = path.split('/directory/room/', 1)[1]
            return httpx.Response(200, json={'room_id': self.aliases[alias]}) if alias in self.aliases else httpx.Response(404, json={'errcode': 'M_NOT_FOUND'})
        if path.endswith('/createRoom'):
            room = '!' + str(len(self.rooms) + 1) + ':matrix.test'
            self.rooms[room] = body['initial_state'][0]['content']
            self.aliases['#' + body['room_alias_name'] + ':matrix.test'] = room
            return httpx.Response(200, json={'room_id': room})
        if '/state/chat.ttt.owner/' in path:
            room = path.split('/rooms/')[1].split('/state/')[0]
            return httpx.Response(200, json=self.rooms[room])
        if '/send/m.room.message/' in path:
            if self.send_budget is not None:
                if self.send_budget <= 0:
                    return httpx.Response(429, json={'errcode': 'M_LIMIT_EXCEEDED', 'retry_after_ms': 1000})
                self.send_budget -= 1
            if body.get('body') == self.rate_limit_text:
                return httpx.Response(429, json={'errcode': 'M_LIMIT_EXCEEDED', 'retry_after_ms': 5000})
            if body.get('body') == self.fail_text:
                return httpx.Response(503, json={'errcode': 'M_UNAVAILABLE'})
            event = '$' + path.rsplit('/', 1)[-1]
            self.events.setdefault(event, {'event_id': event, 'content': body})
            return httpx.Response(200, json={'event_id': event})
        if '/redact/' in path:
            event = path.split('/redact/')[1].split('/')[0]
            self.events[event]['content'] = {}
            return httpx.Response(200, json={'event_id': '$redacted'})
        if '/event/' in path:
            return httpx.Response(200, json=self.events[path.split('/event/')[1]])
        if path.endswith('/upload'):
            media = 'mxc://matrix.test/' + str(len(self.media) + 1)
            self.media[media] = request.content
            return httpx.Response(200, json={'content_uri': media})
        if '/media/download/' in path:
            media = 'mxc://' + path.split('/media/download/')[1]
            self.assertTrue(request.headers.get('authorization', '').startswith('Bearer '))
            return httpx.Response(200, content=self.media[media], headers={'Content-Type': 'image/png'})
        return httpx.Response(500, json={'errcode': 'UNEXPECTED', 'path': path})

    def request(self, path, body=None, tenant='a', headers=None):
        import json
        method = 'GET' if body is None else 'POST'
        raw = b'' if body is None else json.dumps(body).encode()
        signing = sign_request(tenant + '-secret', tenant, method, path, raw)
        response = self.http.request(method, path, content=raw, headers={**signing, **(headers or {})})
        return response.status_code, response.json()

    def conversation(self, title='Chat A', kind='order'):
        return {'id': 1, 'kind': kind, 'title': title, 'members': [
            {'id': 4, 'local_user_id': 11, 'name': 'local', 'left_at': None, 'read_seq': 0},
            {'id': 11, 'local_user_id': None, 'name': 'external', 'left_at': None, 'read_seq': 0}],
            'preferences': [{'user_id': 11, 'pinned': True, 'remark': 'Note', 'marked_unread': False}]}

    def message(self, mid=1, text='hello', seq=1):
        return {'id': mid, 'seq': seq, 'conversation_id': 1, 'sender_id': 4,
                'sender_local_user_id': 11, 'text': text, 'revoked': False,
                'created_at': '2026-11-19T10:00:00+00:00', 'image': None, 'album_id': None}

    def seed(self, tenant='a', text='hello', kind='order', messages=None):
        conversation = self.conversation(kind=kind)
        status, _ = self.request('/v1/import', {'phase': 'conversation', 'rows': [conversation], 'cursor': 1, 'latest': 10, 'has_more': False}, tenant)
        self.assertEqual(status, 200)
        status, result = self.request('/v1/import', {'phase': 'message', 'rows': [{'conversation': conversation, 'message': row} for row in (messages or [self.message(text=text)])], 'cursor': max(row['id'] for row in (messages or [self.message(text=text)])), 'latest': 10, 'has_more': False}, tenant)
        self.assertEqual(status, 200, result)

    def read(self, tenant='a', **overrides):
        return self.request('/v1/read', {'operation': 'history', 'conversation_id': 1,
            'viewer_id': 11, 'readonly': False, 'minimum_cursor': 10, 'query': {}, **overrides}, tenant)

    def test_same_numbers_in_two_tenants_stay_isolated_and_membership_is_local(self):
        self.seed('a', 'Alpha only')
        self.seed('b', 'Bravo only')
        self.assertEqual(self.read('a')[1]['data']['messages'][0]['text'], 'Alpha only')
        self.assertEqual(self.read('b')[1]['data']['messages'][0]['text'], 'Bravo only')
        self.assertEqual(self.read('a', viewer_id=4)[0], 403)
        self.assertEqual(self.read('a', viewer_id=99, readonly=True)[0], 200)
        self.assertEqual(self.read('a', minimum_cursor=11)[0], 503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM rooms').fetchone()['n'], 2)
            self.assertEqual(db.execute('SELECT metadata FROM rooms WHERE tenant_id=%s', ('a',)).fetchone()['metadata']['preferences'][0]['remark'], 'Note')

    def test_failure_keeps_cursor_then_retry_and_restart_restore_progress(self):
        from chat_service.engine import Engine
        from chat_service.store import Store
        self.seed()
        self.fail_text = 'fail next'
        page = {'phase': 'changes', 'rows': [{'id': 11, 'kind': 'message', 'conversation': self.conversation(), 'message': self.message(2, 'fail next', 2)}], 'cursor': 11, 'latest': 11, 'has_more': False}
        self.assertEqual(self.request('/v1/import', page)[0], 503)
        self.assertEqual(self.request('/v1/status')[1]['data']['cursor'], 10)
        self.fail_text = None
        self.assertEqual(self.request('/v1/import', page)[0], 200)
        self.assertEqual(self.request('/v1/import', page)[0], 200)
        self.assertEqual(len(self.events), 2)
        self.server.engine = Engine(Store(self.dsn, self.schema), self.engine.matrix, self.engine.tenants)
        self.assertEqual(self.request('/v1/status')[1]['data']['cursor'], 11)
        self.assertEqual(len(self.read(minimum_cursor=11)[1]['data']['messages']), 2)

    def test_history_before_is_seq_around_is_message_id_and_limit_is_bounded(self):
        self.seed(messages=[self.message(101, 'one', 1), self.message(102, 'two', 2), self.message(103, 'three', 3)])
        self.assertEqual([m['id'] for m in self.read(query={'before': 3})[1]['data']['messages']], [101, 102])
        around = self.read(query={'around': 102, 'limit': 2})[1]['data']
        self.assertEqual([m['id'] for m in around['messages']], [101, 102, 103])
        for query in ({'limit': 101}, {'limit': 0}, {'before': 'oops'}, {'before': 1, 'around': 2}, {'tenant_id': 'b'}):
            self.assertEqual(self.read(query=query)[0], 400, query)
        status, result = self.read(operation='search', query={'q': 'two'})
        self.assertEqual(status, 200)
        self.assertEqual([m['id'] for m in result['data']['messages']], [102])
        self.assertEqual(self.read(operation='search', query={'q': '', 'date_from': 2, 'date_to': 1})[0], 400)

    def test_image_is_in_matrix_and_revocation_removes_readable_bytes(self):
        import base64
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (980, 900), '#112233').save(image, format='PNG', compress_level=0)
        message = self.message()
        message['image'] = {'width': 980, 'height': 900}
        message['image_data'] = {'mime': 'image/png', 'data': base64.b64encode(image.getvalue()).decode()}
        self.seed(messages=[message])
        status, result = self.read(operation='image', query={'message_id': 1})
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(result['data']['data']), image.getvalue())
        with self.store.connect() as db:
            payload = db.execute('SELECT payload FROM messages WHERE tenant_id=%s', ('a',)).fetchone()['payload']
            self.assertNotIn('image_data', payload)
        revoked = {**message, 'revoked': True, 'text': ''}
        page = {'phase': 'changes', 'rows': [{'id': 11, 'conversation': self.conversation(), 'message': revoked}], 'cursor': 11, 'latest': 11, 'has_more': False}
        self.assertEqual(self.request('/v1/import', page)[0], 200)
        self.assertEqual(self.read(operation='image', query={'message_id': 1})[0], 404)
        self.assertTrue(self.read()[1]['data']['messages'][0]['revoked'])
        self.assertEqual(self.events[next(iter(self.events))]['content'], {})

    def test_http_rejects_cookies_and_body_tenant_switch(self):
        self.seed()
        self.assertEqual(self.request('/v1/status', headers={'Cookie': 'session=x'})[0], 403)
        self.assertEqual(self.read(tenant_id='b')[0], 400)
        self.assertEqual(self.http.get('/v1/status').status_code, 403)
        self.assertEqual(self.http.get('/healthz').json()['data'], {'status': 'ok', 'service': 'chat-service'})

    def test_alias_recovery_refuses_room_owned_by_other_tenant(self):
        self.seed()
        with self.store.connect() as db:
            db.execute('TRUNCATE sync_state,accounts,rooms,members,messages,imported_changes')
        self.rooms[next(iter(self.rooms))]['tenant_id'] = 'other-tenant'
        status, _ = self.request('/v1/import', {'phase': 'conversation', 'rows': [self.conversation()], 'cursor': 1, 'latest': 10, 'has_more': False})
        self.assertEqual(status, 503)
        self.assertEqual(self.request('/v1/status')[1]['data']['phase'], 'conversation')

    def test_partial_external_success_commits_prefix_and_retry_does_not_duplicate(self):
        self.seed()
        self.fail_text = 'fail last'
        page = {'phase': 'changes', 'rows': [
            {'id': 11, 'conversation': self.conversation(), 'message': self.message(2, 'accepted first', 2)},
            {'id': 12, 'conversation': self.conversation(), 'message': self.message(3, 'fail last', 3)}],
            'cursor': 12, 'latest': 12, 'has_more': False}
        self.assertEqual(self.request('/v1/import', page)[0], 503)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.request('/v1/status')[1]['data']['cursor'], 11)
        self.fail_text = None
        self.assertEqual(self.request('/v1/import', page)[0], 200)
        self.assertEqual(len(self.events), 3)
        self.assertEqual([m['text'] for m in self.read(minimum_cursor=12)[1]['data']['messages']], ['hello', 'accepted first', 'fail last'])

    def test_snapshot_worker_restarts_at_persisted_page_then_applies_changes(self):
        import httpx
        from chat_service.config import ServiceError
        from chat_service.worker import Source, Worker
        calls = []
        failures = [True]
        def source_reply(request):
            query = dict(request.url.params)
            calls.append((request.url.path, query))
            kind = query.get('kind')
            if kind == 'conversations':
                data = {'rows': [self.conversation()], 'cursor': 1, 'latest': 10, 'has_more': False}
            elif kind == 'messages':
                if failures[0]:
                    failures[0] = False
                    return httpx.Response(200, json={'code': 503, 'msg': 'busy', 'data': None})
                data = {'rows': [{'conversation': self.conversation(), 'message': self.message()}], 'cursor': 1, 'latest': 12, 'has_more': False}
            else:
                self.assertEqual(query['after'], '10')
                data = {'events': [{'id': 11, 'conversation': self.conversation(), 'message': self.message(2, 'after snapshot', 2)}], 'cursor': 12, 'latest': 12, 'has_more': False}
            return httpx.Response(200, json={'code': 0, 'msg': '', 'data': data})
        with httpx.Client(transport=httpx.MockTransport(source_reply)) as client:
            source = Source('a', {'secret': 'a-secret', 'source_url': 'http://127.0.0.1:8000'}, client)
            Worker(self.engine, {'a': source}).step('a')
            self.assertEqual(self.store.status('a')['phase'], 'message')
            with self.assertRaises(ServiceError):
                Worker(self.engine, {'a': source}).step('a')
            self.assertEqual(self.store.status('a')['snapshot_latest'], 10)
            Worker(self.engine, {'a': source}).once('a')
        self.assertEqual(self.store.status('a')['cursor'], 12)
        self.assertEqual(len([call for call in calls if call[1].get('kind') == 'conversations']), 1)
        self.assertEqual([m['text'] for m in self.read(minimum_cursor=12)[1]['data']['messages']], ['hello', 'after snapshot'])

    def test_left_members_and_readonly_direct_conversation_are_denied(self):
        self.seed(kind='direct')
        self.assertEqual(self.read(readonly=True, viewer_id=99)[0], 403)
        conversation = self.conversation(kind='direct')
        conversation['members'][0]['left_at'] = '2026-11-19T11:00:00+00:00'
        self.assertEqual(self.request('/v1/import', {'phase': 'changes', 'rows': [{'id': 11, 'conversation': conversation, 'message': None}], 'cursor': 11, 'latest': 11, 'has_more': False})[0], 200)
        self.assertEqual(self.read()[0], 403)


    def test_latin1_signature_header_is_a_403_instead_of_server_error(self):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            headers = sign_request('a-secret', 'a', 'GET', '/v1/status', b'')
            headers['X-TTT-Chat-Signature'] = '\u00e9' + '0' * 63
            connection.request('GET', '/v1/status', headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            self.assertIn(b'Invalid request signature', response.read())
        finally:
            connection.close()


    def test_snapshot_message_checkpoint_survives_later_rate_limit(self):
        conversation = self.conversation()
        self.assertEqual(self.request('/v1/import', {'phase': 'conversation', 'rows': [conversation],
            'cursor': 1, 'latest': 10, 'has_more': False})[0], 200)
        self.rate_limit_text = 'limited'
        page = {'phase': 'message', 'rows': [
            {'conversation': conversation, 'message': self.message()},
            {'conversation': conversation, 'message': self.message(2, 'limited', 2)}],
            'cursor': 2, 'latest': 10, 'has_more': False}
        self.assertEqual(self.request('/v1/import', page)[0], 503)
        state = self.store.status('a')
        self.assertEqual((state['phase'], state['snapshot_cursor'], state['cursor']), ('message', 1, 0))
        self.rate_limit_text = None
        self.assertEqual(self.request('/v1/import', page)[0], 200)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.store.status('a')['cursor'], 10)

    def test_rate_limited_tenant_keeps_progress_and_does_not_delay_other_tenant(self):
        import httpx
        from chat_service.config import ServiceError
        from chat_service.worker import Source, Worker
        self.seed('a')
        self.seed('b')
        self.rate_limit_text = 'limited'
        calls = []
        now = [0.0]
        def source_reply(request):
            tenant = request.url.path.split('/')[1]
            after = int(request.url.params['after'])
            calls.append((tenant, after))
            messages = [self.message(2, 'first accepted', 2), self.message(3, 'limited', 3)] if tenant == 'a' else [self.message(2, 'other tenant', 2)]
            events = [{'id': 11 + index, 'conversation': self.conversation(), 'message': message} for index, message in enumerate(messages) if 11 + index > after]
            latest = 12 if tenant == 'a' else 11
            return httpx.Response(200, json={'code': 0, 'msg': '', 'data': {'events': events,
                'cursor': latest, 'latest': latest, 'has_more': False}})
        with httpx.Client(transport=httpx.MockTransport(source_reply)) as client:
            sources = {tenant: Source(tenant, {'secret': tenant + '-secret',
                'source_url': 'http://127.0.0.1/' + tenant}, client) for tenant in ('a', 'b')}
            worker = Worker(self.engine, sources, clock=lambda: now[0])
            with self.assertRaises(ServiceError):
                worker.step('a')
            self.assertEqual(self.store.status('a')['cursor'], 11)
            self.assertEqual(worker.step('a')['retry_after'], 5.0)
            self.assertEqual(calls, [('a', 10)])
            self.assertTrue(worker.step('b')['completed'])
            self.assertEqual(self.store.status('b')['cursor'], 11)
            now[0] = 5.0
            self.rate_limit_text = None
            self.assertTrue(worker.step('a')['completed'])
            self.assertEqual(self.store.status('a')['cursor'], 12)
            self.assertEqual(calls, [('a', 10), ('b', 10), ('a', 11)])
            self.assertTrue(Worker(self.engine, sources).step('a')['completed'])
        self.assertEqual(len(self.events), 5)


    def test_125_changes_catch_up_with_burst_two_and_restart_after_each_limit(self):
        import httpx
        from chat_service.config import ServiceError
        from chat_service.worker import Source, Worker
        self.seed()
        events = [{'id': 11 + index, 'conversation': self.conversation(),
                   'message': self.message(2 + index, 'queued ' + str(index), 2 + index)}
                  for index in range(125)]
        afters = []
        now = [0.0]
        def source_reply(request):
            after = int(request.url.params['after'])
            afters.append(after)
            pending = [event for event in events if event['id'] > after]
            page = pending[:100]
            return httpx.Response(200, json={'code': 0, 'msg': '', 'data': {'events': page,
                'cursor': page[-1]['id'] if len(pending) > 100 else 135,
                'latest': 135, 'has_more': len(pending) > 100}})
        with httpx.Client(transport=httpx.MockTransport(source_reply)) as client:
            source = Source('a', {'secret': 'a-secret', 'source_url': 'http://127.0.0.1'}, client)
            previous = 10
            for _ in range(70):
                self.send_budget = 2
                worker = Worker(self.engine, {'a': source}, clock=lambda: now[0])
                try:
                    result = worker.step('a')
                except ServiceError as error:
                    self.assertEqual(error.matrix_code, 'M_LIMIT_EXCEEDED')
                    self.assertEqual(error.retry_after, 1.0)
                    self.assertEqual(worker.step('a')['retry_after'], 1.0)
                    result = {'completed': False}
                current = self.store.status('a')['cursor']
                self.assertGreater(current, previous)
                previous = current
                now[0] += 1.0
                if result['completed']:
                    break
            else:
                self.fail('Catch-up never completed under the small Matrix burst')
        self.assertEqual(self.store.status('a')['cursor'], 135)
        self.assertEqual(len(self.events), 126)
        self.assertEqual(afters[0], 10)
        self.assertTrue(all(right > left for left, right in zip(afters, afters[1:])))
        first_sends = [body for _, path, body in self.matrix_requests
                       if '/send/m.room.message/' in path and body.get('body') == 'queued 0']
        self.assertEqual(len(first_sends), 1)


class MatrixAccountTests(unittest.TestCase):
    def setUp(self):
        import httpx
        from chat_service.matrix import Matrix
        self.registered = set()
        self.logins = []
        self.registrations = []
        self.calls = []
        self.registration_error = None
        self.login_error = None
        self.http = httpx.Client(transport=httpx.MockTransport(self.reply))
        self.matrix = Matrix('http://127.0.0.1:9999', 'registration-secret', self.http)

    def tearDown(self):
        self.http.close()

    def reply(self, request):
        import httpx
        import json
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path))
        if request.url.path.endswith('/register'):
            if request.method == 'GET':
                return httpx.Response(200, json={'nonce': 'fixture-nonce'})
            self.registrations.append(body)
            if self.registration_error:
                status, code = self.registration_error
                return httpx.Response(status, json={'errcode': code, 'error': 'Sensitive server detail'})
            name = body['username']
            if name in self.registered:
                return httpx.Response(400, json={'errcode': 'M_USER_IN_USE'})
            self.registered.add(name)
            return httpx.Response(200, json={'user_id': '@' + name + ':matrix.test'})
        if request.url.path.endswith('/login'):
            self.logins.append(body)
            if self.login_error or len(self.logins) > 3:
                return httpx.Response(429, json={'errcode': 'M_LIMIT_EXCEEDED'})
            name = body['identifier']['user']
            if name not in self.registered:
                return httpx.Response(403, json={'errcode': 'M_FORBIDDEN'})
            return httpx.Response(200, json={'user_id': '@' + name + ':matrix.test', 'access_token': 'token'})
        return httpx.Response(500, json={'errcode': 'M_UNKNOWN'})

    def test_two_new_tenant_bots_use_two_logins_under_burst_three(self):
        first = self.matrix.account('studio-first')
        second = self.matrix.account('studio-second')
        self.assertNotEqual(first['matrix_id'], second['matrix_id'])
        self.assertEqual(len(self.logins), 2)
        self.assertEqual(len(self.registrations), 2)
        self.assertTrue(all(body.get('inhibit_login') is True for body in self.registrations))
        self.assertTrue(all(body['device_id'] == 'TT_CHAT_BRIDGE' for body in self.logins))

    def test_existing_registration_continues_to_fixed_device_login(self):
        first = self.matrix.account('studio-existing')
        self.calls.clear()
        self.logins.clear()
        second = self.matrix.account('studio-existing')
        self.assertEqual(second['matrix_id'], first['matrix_id'])
        self.assertEqual(self.calls, [('GET', '/_synapse/admin/v1/register'),
            ('POST', '/_synapse/admin/v1/register'), ('POST', '/_matrix/client/v3/login')])
        self.assertEqual(len(self.logins), 1)

    def test_registration_failures_propagate_without_login(self):
        from chat_service.config import ServiceError
        for status, code in ((403, 'M_FORBIDDEN'), (429, 'M_LIMIT_EXCEEDED'), (200, 'M_BAD_JSON')):
            self.logins.clear()
            self.registration_error = status, code
            with self.subTest(status=status), self.assertRaises(ServiceError) as caught:
                self.matrix.account('studio-denied')
            self.assertEqual(getattr(caught.exception, 'matrix_code', None), code)
            self.assertNotIn('Sensitive server detail', str(caught.exception))
            self.assertEqual(self.logins, [])

    def test_rate_limited_login_does_not_fall_back_to_registration(self):
        from chat_service.config import ServiceError
        self.login_error = True
        with self.assertRaises(ServiceError) as caught:
            self.matrix.account('studio-rate-limited')
        self.assertEqual(getattr(caught.exception, 'matrix_code', None), 'M_LIMIT_EXCEEDED')
        self.assertEqual(len(self.logins), 1)
        self.assertEqual(len(self.registrations), 1)
        self.assertEqual(self.calls[-1], ('POST', '/_matrix/client/v3/login'))
