from unittest import TestCase

from chat_matrix_pilot.matrix import Matrix, PilotError
from chat_matrix_pilot.validation import integer, image_data


class MatrixPilotValidationTests(TestCase):
    def test_ids_reject_booleans_and_bad_strings(self):
        for value in (True, -1, 'abc', 1.5, None):
            with self.assertRaises(PilotError):
                integer(value)
        self.assertEqual(integer('12'), 12)

    def test_image_rejects_disguised_bytes(self):
        with self.assertRaises(PilotError):
            image_data({'mime': 'image/png', 'data': 'aGVsbG8='})

    def test_matrix_failure_response_is_not_success(self):
        import httpx
        def fail(request):
            return httpx.Response(403, json={'errcode': 'M_FORBIDDEN', 'error': 'denied'})
        matrix = Matrix('http://matrix.invalid', httpx.Client(transport=httpx.MockTransport(fail)))
        with self.assertRaises(PilotError) as failure:
            matrix.call('POST', '/_matrix/client/v3/createRoom', 'token', {})
        self.assertEqual(failure.exception.status, 502)


    def test_matrix_error_envelope_with_http_success_is_rejected(self):
        import httpx
        def fail(request):
            return httpx.Response(200, json={'errcode': 'M_FORBIDDEN', 'error': 'denied'})
        matrix = Matrix('http://matrix.invalid', httpx.Client(transport=httpx.MockTransport(fail)))
        with self.assertRaises(PilotError):
            matrix.call('PUT', '/_matrix/client/v3/rooms/room/send/m.room.message/txn', 'token', {})


    def test_registration_only_tolerates_existing_account_error(self):
        import httpx
        for status, code, expected_login in ((403, 'M_FORBIDDEN', False),
                                             (500, 'M_UNKNOWN', False),
                                             (400, 'M_USER_IN_USE', True),
                                             (200, 'M_FORBIDDEN', False)):
            with self.subTest(status=status, code=code):
                logins = []
                def reply(request):
                    if request.method == 'GET':
                        return httpx.Response(200, json={'nonce': 'example-nonce'})
                    if request.url.path.endswith('/login'):
                        logins.append(True)
                        return httpx.Response(200, json={'user_id': '@pilot:local', 'access_token': 'test-token'})
                    return httpx.Response(status, json={'errcode': code, 'error': 'denied'})
                matrix = Matrix('http://matrix.invalid', httpx.Client(transport=httpx.MockTransport(reply)))
                if expected_login:
                    self.assertEqual(matrix.provision({'username': 'pilot', 'password': 'internal-secret'}, 'test-registration-secret')[0], '@pilot:local')
                else:
                    with self.assertRaises(PilotError):
                        matrix.provision({'username': 'pilot', 'password': 'internal-secret'}, 'test-registration-secret')
                self.assertEqual(bool(logins), expected_login)


class MatrixPilotHttpTests(TestCase):
    def setUp(self):
        import threading
        import tempfile
        import httpx
        from chat_matrix_pilot.server import Server
        from chat_matrix_pilot.service import Pilot
        self.directory = tempfile.TemporaryDirectory()
        config = {'matrix_url': 'http://127.0.0.1:1', 'legacy_url': 'http://localhost:5173/',
                  'users': [{'id': 101, 'username': 'a', 'password': 'secret-a', 'studio': 'alpha', 'name': '甲客服'},
                            {'id': 102, 'username': 'a2', 'password': 'secret-a2', 'studio': 'alpha', 'name': '甲打手'},
                            {'id': 201, 'username': 'b', 'password': 'secret-b', 'studio': 'beta', 'name': '乙客服'}]}
        self.server = Server(('127.0.0.1', 0), Pilot(config), self.directory.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False)

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def login(self, username='a', password='secret-a'):
        return self.client.post('/pilot/login', json={'username': username, 'password': password})

    def test_login_binds_identity_and_directory_to_session(self):
        self.assertEqual(self.client.get('/api/internal-chat/contacts/').status_code, 401)
        result = self.login()
        self.assertEqual(result.status_code, 200)
        cookie = result.headers['set-cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertEqual(self.client.get('/pilot/session').json()['user']['id'], 101)
        contacts = self.client.get('/api/internal-chat/contacts/').json()['contacts']
        self.assertEqual([u['name'] for u in contacts], ['甲打手'])
        self.assertNotIn('access_token', result.text)
        self.assertEqual(self.client.get('/api/internal-chat/contacts/?studio=beta').status_code, 400)
        self.assertEqual(self.client.post('/pilot/logout', json={}).status_code, 200)
        self.assertEqual(self.client.get('/pilot/session').status_code, 401)
        self.assertEqual(self.login('b', 'secret-b').status_code, 200)
        self.assertEqual(self.client.get('/api/internal-chat/contacts/').json()['contacts'], [])

    def test_login_rejects_wrong_secret_identity_override_and_cross_origin(self):
        self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.client.post('/pilot/login', json={'username': 'a', 'password': 'secret-a', 'studio': 'beta'}).status_code, 400)
        self.assertEqual(self.client.post('/pilot/login', headers={'Origin': 'http://untrusted.invalid'}, json={'username': 'a', 'password': 'secret-a'}).status_code, 403)
        self.assertEqual(self.client.post('/pilot/login', content='[]', headers={'Content-Type': 'application/json'}).status_code, 400)

    def test_surrogate_strings_are_rejected_before_identity_or_message_handling(self):
        payloads = (
            ('/pilot/login', b'{"username":"a","password":"\\ud800"}'),
            ('/api/internal-chat/conversations/1/messages/', b'{"text":"\\ud800","client_id":"valid"}'),
            ('/api/internal-chat/conversations/1/messages/', b'{"text":"valid","client_id":"\\ud800"}'),
            ('/pilot/login', b'{"username":"a","password":{"nested":["\\ud800"]}}'),
        )
        for path, raw in payloads:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, content=raw, headers={'Content-Type': 'application/json'}).status_code, 400)

    def test_non_ascii_or_invalid_cookie_is_unauthorized(self):
        for raw in (b'tt_matrix_pilot=\xff', b'tt_matrix_pilot[=broken', b'[invalid]=token'):
            self.assertEqual(self.client.get('/pilot/session', headers=[(b'Cookie', raw)]).status_code, 401)

    def test_rollback_returns_original_entry_and_static_stays_in_root(self):
        result = self.client.get('/pilot/rollback')
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['location'], 'http://localhost:5173/')
        self.assertEqual(self.client.get('/internal-chat-webk/%2e%2e/%2e%2e/.env').status_code, 404)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)


class MatrixPilotRoomRecoveryTests(TestCase):
    def setUp(self):
        import httpx
        from chat_matrix_pilot.service import Pilot
        self.pilot = Pilot({'matrix_url': 'http://matrix.invalid', 'server_name': 'pilot.local',
                            'registration_shared_secret': 'test-registration-secret',
                            'users': [{'id': 1, 'studio': 'alpha', 'name': '甲一'},
                                      {'id': 2, 'studio': 'alpha', 'name': '甲二'}]})
        self.rooms = {}
        self.fail_join = True
        self.forge_marker = False
        self.pilot.matrix = Matrix('http://matrix.invalid', httpx.Client(transport=httpx.MockTransport(self.matrix_reply)))

    def state(self):
        return {'accounts': {'1': {'mxid': '@one:pilot.local', 'token': 'one'},
                             '2': {'mxid': '@two:pilot.local', 'token': 'two'}},
                'rooms': {}, 'next_room': 1, 'changes': [], 'cursor': 0}

    def matrix_reply(self, request):
        import httpx
        import json
        path = request.url.path
        if path.endswith('/createRoom'):
            body = json.loads(request.content)
            alias = body.get('room_alias_name', str(len(self.rooms)))
            if alias in self.rooms:
                return httpx.Response(400, json={'errcode': 'M_ROOM_IN_USE'})
            self.rooms[alias] = body
            return httpx.Response(200, json={'room_id': '!example:pilot.local'})
        if '/directory/room/' in path:
            return httpx.Response(200, json={'room_id': '!example:pilot.local'})
        if '/state/org.tuantuantong.pilot' in path:
            marker = next((e['content'] for e in next(iter(self.rooms.values()))['initial_state']
                           if e['type'] == 'org.tuantuantong.pilot'), {})
            return httpx.Response(200, json={} if self.forge_marker else marker)
        if '/state/m.room.join_rules' in path:
            return httpx.Response(200, json={'join_rule': 'invite'})
        if path.endswith('/joined_members'):
            return httpx.Response(200, json={'joined': {'@one:pilot.local': {}}})
        if '/join/' in path:
            if self.fail_join:
                self.fail_join = False
                return httpx.Response(500, json={'errcode': 'M_UNKNOWN'})
            return httpx.Response(200, json={'room_id': '!example:pilot.local'})
        raise AssertionError('unexpected Matrix request')

    def test_retry_after_join_failure_recovers_one_matrix_room(self):
        with self.assertRaises(PilotError):
            self.pilot.create_room(self.state(), '1', ['1', '2'])
        recovered = self.state()
        self.assertEqual(self.pilot.create_room(recovered, '2', ['2', '1']), '1')
        self.assertEqual(len(self.rooms), 1)
        self.assertEqual(recovered['rooms']['1']['matrix_id'], '!example:pilot.local')

    def test_alias_collision_with_wrong_binding_is_not_adopted(self):
        with self.assertRaises(PilotError):
            self.pilot.create_room(self.state(), '1', ['1', '2'])
        self.forge_marker = True
        state = self.state()
        with self.assertRaises(PilotError):
            self.pilot.create_room(state, '1', ['1', '2'])
        self.assertEqual(state['rooms'], {})
