import unittest

from tests import test_chat_service_engine as mirror_tests


class CoreHTTPTests(unittest.TestCase):
    setUpClass = classmethod(mirror_tests.ServiceHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(mirror_tests.ServiceHTTPTests.tearDownClass.__func__)
    def setUp(self):
        mirror_tests.ServiceHTTPTests.setUp(self)
        with self.store.connect() as db:
            for table in ('core_control', 'core_identities', 'core_reads', 'core_preferences', 'core_events',
                          'core_commands', 'core_outbox', 'core_notifications', 'core_policy_clock', 'core_archives', 'core_order_rooms'):
                db.execute('TRUNCATE ' + table)
        from unittest.mock import patch
        self.authority_denied = {'a': set(), 'b': set()}
        self.authority_reopen = {'a': {11}, 'b': {11}}
        self.authority_completed = {'a': False, 'b': False}
        self.authority_patch = patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=self.authority_reply)
        self.authority_patch.start()
        self.addCleanup(self.authority_patch.stop)

    def authority_reply(self, tenant, checks, *, viewer=None, discover=False, timeout=5):
        groups = []
        with self.store.connect() as db:
            for check in checks:
                row = db.execute('SELECT metadata FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, check['conversation_id'])).fetchone()
                if row is None:
                    continue
                members = db.execute('SELECT member_id,local_user_id,metadata FROM members WHERE tenant_id=%s AND conversation_id=%s', (tenant, check['conversation_id'])).fetchall()
                room = {**{k: v for k, v in row['metadata'].items() if k != 'policy_fingerprint'}, 'members': [{**m['metadata'], 'id': m['member_id'], 'local_user_id': m['local_user_id']} for m in members]}
                groups.append({'conversation_id': check['conversation_id'], 'room': room,
                    'read_user_ids': [uid for uid in check['user_ids'] if uid not in self.authority_denied[tenant]],
                    'reopen_user_ids': list(set(check['user_ids']) & self.authority_reopen[tenant]),
                    'completed': self.authority_completed[tenant], 'default_join_user_ids': []})
        return {'groups': groups, 'has_order_access': True}

    tearDown = mirror_tests.ServiceHTTPTests.tearDown
    matrix_reply = mirror_tests.ServiceHTTPTests.matrix_reply
    request = mirror_tests.ServiceHTTPTests.request
    conversation = mirror_tests.ServiceHTTPTests.conversation
    message = mirror_tests.ServiceHTTPTests.message
    seed = mirror_tests.ServiceHTTPTests.seed

    def core(self, operation, *, tenant='a', viewer=11, query=None, payload=None, readonly=False):
        if operation == 'room' and payload is not None and 'policy_version' not in payload:
            status, policy = self.core('policy', tenant=tenant, viewer=None)
            self.assertEqual(status, 200, policy)
            payload = {**payload, **policy['data']}
        response = self.request('/v1/core', {'operation': operation, 'viewer_id': viewer,
            'query': query or {}, 'payload': payload or {}, 'readonly': readonly}, tenant)
        if operation == 'room' and response[0] == 200 and response[1]['data'].get('applied'):
            self.authority_denied[tenant] = set(payload.get('denied_user_ids', []))
        return response

    def activate(self, tenant='a'):
        self.seed(tenant)
        self.engine.tenants[tenant]['realtime'] = {'url': 'http://127.0.0.1:9998/notify', 'secret': 'gateway-secret', 'tenant': 'studio_' + tenant}
        status, result = self.core('activate', tenant=tenant, payload={'minimum_cursor': 10})
        self.assertEqual(status, 200, result)
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[11])
        self.assertEqual(self.core('room', tenant=tenant, payload=room)[0], 200)
        return result['data']

    def browser_fixture(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from chat_service.protocol import verify_request

        self.browser_audience = 'https://chat.test.example'
        self.browser_calls = []
        self.browser_source_status = 200
        self.browser_worker = False
        self.browser_peer_allowed = True
        fixture = self

        class SourceHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.assert_authority()
                status = 403 if 'peer' in body and not fixture.browser_peer_allowed else fixture.browser_source_status
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'code': 0, 'data': {
                    'status': status,
                    'viewer': {'id': 11, 'name': 'Alice', 'avatar_url': None},
                    'worker_directory': fixture.browser_worker,
                }}).encode())

            def assert_authority(self):
                fixture.assertEqual(self.path, '/api/internal-chat/service/browser-authorize/')
                raw = self.rfile.read(int(self.headers['Content-Length']))
                tenant = verify_request({key: config['secret'] for key, config in fixture.engine.tenants.items()},
                                        self.headers, 'POST', self.path, raw)
                fixture.assertEqual(self.headers['Host'], f'{tenant}.test.example')
                body = json.loads(raw)
                fixture.browser_calls.append((tenant, body))
                return body

            def log_message(self, format, *args):
                pass

        source = ThreadingHTTPServer(('127.0.0.1', 0), SourceHandler)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(source.server_close)
        self.addCleanup(source.shutdown)
        for key, config in self.engine.tenants.items():
            config.update(source_url=f'http://127.0.0.1:{source.server_port}', source_host=f'{key}.test.example',
                          browser={'audience': self.browser_audience, 'origins': [f'https://{key}.test.example']})

    def browser_headers(self, tenant='a', *, now=None):
        from chat_service.browser_ticket import issue_browser_ticket

        config = self.engine.tenants[tenant]
        origin = config['browser']['origins'][0]
        token, _ = issue_browser_ticket(key_id=tenant, secret=config['secret'], viewer=11, origin=origin,
                                        audience=self.browser_audience, proof='synthetic-source-proof', now=now)
        return {'Host': 'chat.test.example', 'Origin': origin, 'Authorization': 'Bearer ' + token}

    def test_direct_http_two_tenants_use_own_messages_and_images_with_the_same_ids(self):
        import base64
        from io import BytesIO
        from PIL import Image

        self.browser_fixture()
        first_message_ids, images = [], {}
        for tenant, color in [('a', 'red'), ('b', 'blue')]:
            self.activate(tenant)
            png = BytesIO()
            dimensions = (980, 900) if tenant == 'a' else (4, 2)
            Image.new('RGB', dimensions, color).save(png, format='PNG', compress_level=0)
            images[tenant] = base64.b64encode(png.getvalue()).decode()
            envelope = {'text': f'{tenant} only', 'client_id': 'same-client-id',
                        'image': {'mime': 'image/png', 'data': images[tenant]}}
            url = '/v1/browser/conversations/1/messages/'
            post = self.http.post(url, json=envelope, headers=self.browser_headers(tenant))
            self.assertEqual(post.status_code, 200, post.text)
            first_message_ids.append(post.json()['data']['id'])
            retry = self.http.post(url, json=envelope, headers=self.browser_headers(tenant))
            self.assertEqual(retry.json()['data']['id'], first_message_ids[-1])
        self.assertEqual(first_message_ids[0], first_message_ids[1])
        for tenant in ('a', 'b'):
            url = '/v1/browser/conversations/1/messages/'
            headers = self.browser_headers(tenant)
            history = self.http.get(url, headers=headers)
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual([m['text'] for m in history.json()['data']['messages']], ['hello', f'{tenant} only'])
            image = self.http.get(f'{url}{first_message_ids[0]}/image/', headers=headers)
            self.assertEqual(image.json()['data']['data'], images[tenant])
            self.assertEqual(image.headers['Access-Control-Allow-Origin'], headers['Origin'])
            self.assertNotIn('Access-Control-Allow-Credentials', image.headers)
            self.assertEqual(image.headers['Cache-Control'], 'no-store')
        self.assertNotEqual(images['a'], images['b'])
        self.assertTrue(self.browser_calls)
        self.assertTrue(all(set(body) == {'viewer', 'origin', 'proof'} and body['viewer'] == 11
                            for _, body in self.browser_calls))

    def test_direct_http_rejects_untrusted_or_stale_authentication_and_private_routes(self):
        import time

        self.browser_fixture()
        headers = self.browser_headers()
        url = '/v1/browser/conversations/1/messages/'
        preflight = {'Host': headers['Host'], 'Origin': headers['Origin'], 'Access-Control-Request-Method': 'POST',
                     'Access-Control-Request-Headers': 'authorization,content-type'}
        response = self.http.options(url, headers=preflight)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Methods'], 'POST')
        self.assertEqual(response.headers['Access-Control-Allow-Headers'], 'Authorization, Content-Type')
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], headers['Origin'])
        self.assertEqual(response.headers['Vary'], 'Origin, Access-Control-Request-Method, Access-Control-Request-Headers')
        get = preflight | {'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'Authorization'}
        response = self.http.options(url, headers=get)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers['Access-Control-Allow-Headers'], 'Authorization')
        for invalid in ('authorization,', 'authorization, AUTHORIZATION', 'authorization, content-type', 'content-type'):
            self.assertEqual(self.http.options(url, headers=get | {'Access-Control-Request-Headers': invalid}).status_code, 403)
        for name in ('Access-Control-Request-Headers', 'Access-Control-Request-Method'):
            duplicate = [*get.items(), (name, get[name])]
            self.assertEqual(self.http.options(url, headers=duplicate).status_code, 403)
        for changes, status in (
            ({'Origin': 'https://b.test.example'}, 401),
            ({'Origin': 'https://unrelated.test.example'}, 403),
            ({'Host': 'alternate.test.example'}, 403),
            ({'Cookie': 'sessionid=foreign'}, 403),
            ({'Authorization': headers['Authorization'][:-1] + ('0' if headers['Authorization'][-1] != '0' else '1')}, 401),
            (self.browser_headers(now=int(time.time()) - 120), 401),
        ):
            with self.subTest(changes=set(changes), status=status):
                response = self.http.get(url, headers=headers | changes)
                self.assertEqual(response.status_code, status, response.text)
                if response.status_code == 403 and 'Cookie' not in changes:
                    self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertEqual(self.http.options(url, headers=preflight | {'Access-Control-Request-Headers': 'cookie'}).status_code, 403)
        for path in ('core', 'import', 'notify', 'status', 'contacts/', 'order-authorizations/', '_matrix/client/versions'):
            self.assertEqual(self.http.post('/v1/browser/' + path, headers=headers, json={}).status_code, 404)
        private = self.http.post('/v1/core', headers=headers, json={'operation': 'status'})
        self.assertEqual(private.status_code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', private.headers)
        self.assertEqual(self.http.post(url, headers=headers, json={'tenant_id': 'b'}).status_code, 400)
        self.assertEqual(self.http.get(url + '?viewer_id=12', headers=headers).status_code, 400)

    def test_direct_http_source_denial_paused_writes_and_worker_directory(self):
        self.browser_fixture()
        self.activate()
        headers = self.browser_headers()
        url = '/v1/browser/conversations/1/messages/'
        self.browser_source_status = 401
        self.assertEqual(self.http.get(url, headers=headers).status_code, 401)
        self.browser_source_status = 503
        self.assertEqual(self.http.get(url, headers=headers).status_code, 503)
        self.browser_source_status = 200
        self.authority_denied['a'].add(11)
        self.assertEqual(self.http.get(url, headers=headers).status_code, 403)
        self.authority_denied['a'].clear()
        self.assertEqual(self.core('pause')[0], 200)
        self.assertEqual(self.http.post(url, headers=headers, json={'text': 'paused', 'client_id': 'paused'}).status_code, 503)
        self.assertEqual(self.core('resume')[0], 200)
        chat = self.core('start', payload={'viewer': {'id': 11, 'name': 'Alice'}, 'peer': {'id': 13, 'name': 'Chris'}})
        self.assertEqual(chat[0], 200, chat)
        direct_id = chat[1]['data']['id']
        self.browser_worker = True
        direct_url = f'/v1/browser/conversations/{direct_id}/messages/'
        body = {'text': 'directory start', 'client_id': 'first'}
        self.browser_peer_allowed = False
        self.assertEqual(self.http.post(direct_url, headers=headers, json=body).status_code, 403)
        self.assertEqual(self.browser_calls[-1][1]['peer'], 13)
        self.browser_peer_allowed = True
        result = self.http.post(direct_url, headers=headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.browser_calls[-1][1]['peer'], 13)
        self.browser_peer_allowed = False
        self.assertEqual(self.http.post(direct_url, headers=headers, json=body).json()['data']['id'], result.json()['data']['id'])
        result = self.http.get(f'/v1/browser/order-groups/{direct_id}/messages/', headers=headers)
        self.assertEqual(result.status_code, 404)

    def test_direct_order_paths_check_business_order_and_keep_first_send_idempotent(self):
        from unittest.mock import patch

        self.browser_fixture()
        self.activate()
        order_no = '20261120000000000321'
        path = f'/v1/browser/orders/{order_no}/'
        fact = {'order_no': order_no, 'read_user_ids': [11], 'reopen_user_ids': [11], 'default_join_user_ids': [],
                'completed': False, 'deleted': False, 'can_send': True, 'participants': []}
        headers = self.browser_headers()
        with patch.object(self.engine.core, '_order_fact', return_value=fact):
            self.assertIsNone(self.http.get(path, headers=headers).json()['data']['id'])
            self.assertEqual(self.http.get(path + 'messages/', headers=headers).json()['data']['messages'], [])
            for query in ('before=bad', 'around=0', 'before=1&around=2', 'before=9223372036854775808'):
                invalid = self.http.get(path + 'messages/?' + query, headers=headers)
                self.assertEqual((invalid.status_code, invalid.json()['code'], invalid.json()['data']),
                                 (200, 1000, None))
            missing = self.http.get(path + 'messages/1/image/', headers=headers)
            self.assertEqual((missing.status_code, missing.json()['code']), (200, 1002))
            unknown = self.http.get(path + 'unknown/', headers=headers)
            self.assertEqual((unknown.status_code, unknown.json()['code']), (404, 404))
            body = {'text': 'first order message', 'client_id': 'new-order-room'}
            response = self.http.post(path + 'messages/', headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            sent = response.json()['data']['messages'][0]
            retry = self.http.post(path + 'messages/', headers=headers, json=body)
            self.assertEqual(retry.json()['data']['messages'][0]['id'], sent['id'])
            self.assertEqual(self.http.get(path + 'messages/', headers=headers).json()['data']['messages'][0]['id'], sent['id'])
            self.browser_source_status = 404
            for response in (self.http.get(path, headers=headers),
                             self.http.post(path + 'messages/', headers=headers, json=body)):
                self.assertEqual((response.status_code, response.json()['code']), (200, 1002))
        self.assertTrue(all(body['order_no'] == order_no and 'message' not in body for _, body in self.browser_calls))

    def test_direct_and_same_origin_retries_use_the_same_integer_reply_fingerprint(self):
        self.browser_fixture()
        self.activate()
        path = '/v1/browser/conversations/1/messages/'
        reply = self.http.get(path, headers=self.browser_headers()).json()['data']['messages'][0]['id']
        body = {'text': 'same message through both transports', 'client_id': 'cross-transport-retry',
                'reply_to_id': reply}
        status, same_origin = self.core('send', query={'conversation_id': 1}, payload=body)
        self.assertEqual(status, 200, same_origin)
        retry = self.http.post(path, headers=self.browser_headers(), json=body | {'reply_to_id': str(reply)})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()['data']['id'], same_origin['data']['id'])

    def test_activation_fences_mirror_and_new_send_is_durable_without_synapse(self):
        self.assertTrue(self.activate()['active'])
        self.assertEqual(self.request('/v1/import', {'phase': 'changes', 'rows': [], 'cursor': 10, 'latest': 10, 'has_more': False})[0], 409)
        status, response = self.core('send', query={'conversation_id': 1}, payload={'text': 'queued in service', 'client_id': 'send-one'})
        self.assertEqual(status, 200, response)
        message = response['data']
        self.assertEqual(message['id'], 1_000_000_000)
        self.assertEqual(message['delivery_state'], 'queued')
        self.assertEqual(len(self.events), 1)
        from chat_service.engine import Engine
        self.server.engine = self.engine = Engine(self.store, self.engine.matrix, self.engine.tenants)
        status, retry = self.core('send', query={'conversation_id': 1}, payload={'text': 'queued in service', 'client_id': 'send-one'})
        self.assertEqual(retry['data']['id'], message['id'])
        history = self.core('history', query={'conversation_id': 1})[1]['data']
        self.assertEqual([m['text'] for m in history['messages']], ['hello', 'queued in service'])
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 1)

    def test_core_owns_direct_conversation_preferences_reads_and_cursor(self):
        self.activate()
        payload = {'viewer': {'id': 11, 'name': 'Alice', 'avatar_url': None},
                   'peer': {'id': 12, 'name': 'Bob', 'avatar_url': None}}
        status, response = self.core('start', payload=payload)
        self.assertEqual(status, 200, response)
        cid = response['data']['id']
        self.assertGreaterEqual(cid, 1_000_000_000)
        self.assertEqual(self.core('start', payload=payload)[1]['data']['id'], cid)
        before = self.core('conversations', viewer=12)[1]['data']['cursor']
        msg = self.core('send', query={'conversation_id': cid}, payload={'text': 'hello Bob', 'client_id': 'bob-one'})[1]['data']
        self.assertEqual(self.core('unread', viewer=12)[1]['data']['unread_count'], 1)
        pref = self.core('preferences', viewer=12, query={'conversation_id': cid}, payload={'remark': 'Customer', 'pinned': True, 'marked_unread': True})
        self.assertEqual(pref[0], 200, pref)
        self.assertEqual(self.core('read', viewer=12, query={'conversation_id': cid}, payload={'seq': msg['seq']})[1]['data'], {'read_seq': msg['seq']})
        self.assertEqual(self.core('unread', viewer=12)[1]['data']['unread_count'], 0)
        dto = next(c for c in self.core('conversations', viewer=12)[1]['data']['conversations'] if c['id'] == cid)
        self.assertEqual((dto['remark'], dto['pinned'], dto['marked_unread']), ('Customer', True, False))
        page = self.core('sync', viewer=12, query={'after': before})[1]['data']
        self.assertIn(msg['id'], [c['message']['id'] for c in page['changes'] if c['message']])
        self.assertEqual(self.core('peer', viewer=12, query={'conversation_id': cid})[1]['data'], {'peer_id': 11})
        self.assertEqual(self.core('history', viewer=99, query={'conversation_id': cid})[0], 403)

    def test_outbox_retries_after_external_success_before_local_commit_and_restart(self):
        import psycopg
        from unittest.mock import patch
        from chat_service.engine import Engine
        self.activate()
        msg = self.core('send', query={'conversation_id': 1}, payload={'text': 'durable retry', 'client_id': 'durable'})[1]['data']
        with patch('chat_service.core_delivery.CoreDelivery._complete', side_effect=psycopg.OperationalError('commit interrupted')):
            self.engine.core.tick('a')
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 1)
        self.assertEqual(len(self.events), 2)
        self.server.engine = self.engine = Engine(self.store, self.engine.matrix, self.engine.tenants)
        with self.store.connect() as db:
            db.execute("UPDATE core_outbox SET available_at=now() WHERE tenant_id='a'")
        self.engine.core.tick('a')
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 0)
        self.assertEqual(len(self.events), 2)
        history = self.core('history', query={'conversation_id': 1})[1]['data']['messages']
        self.assertEqual(history[-1]['id'], msg['id'])
        self.assertEqual(history[-1]['delivery_state'], 'delivered')

    def test_album_image_and_revoke_are_authoritative_before_and_after_delivery(self):
        import base64
        import io
        from PIL import Image
        self.activate()
        stream = io.BytesIO()
        Image.new('RGB', (3, 2), '#123456').save(stream, 'PNG')
        encoded = base64.b64encode(stream.getvalue()).decode()
        payload = {'text': 'two pictures', 'client_id': 'album', 'images': [{'mime': 'image/png', 'data': encoded}] * 2}
        status, response = self.core('send', query={'conversation_id': 1}, payload=payload)
        self.assertEqual(status, 200, response)
        first, second = response['data']['messages']
        self.assertEqual(first['album_id'], second['album_id'])
        self.assertEqual(self.core('image', query={'conversation_id': 1, 'message_id': first['id']})[1]['data']['data'], encoded)
        self.engine.core.tick('a')
        self.assertEqual(self.core('image', query={'conversation_id': 1, 'message_id': first['id']}, readonly=True, viewer=99)[1]['data']['data'], encoded)
        self.assertEqual(self.core('revoke', viewer=99, query={'conversation_id': 1, 'message_id': first['id']})[0], 403)
        self.assertEqual(self.core('revoke', query={'conversation_id': 1, 'message_id': first['id']})[1]['data']['text'], '')
        self.assertEqual(self.core('image', query={'conversation_id': 1, 'message_id': first['id']})[0], 404)
        retry = self.core('send', query={'conversation_id': 1}, payload=payload)[1]['data']['messages']
        self.assertTrue(retry[0]['revoked'])
        self.assertNotIn('image', retry[0])
        self.engine.core.tick('a')
        self.assertEqual(self.core('search', query={'conversation_id': 1, 'image_only': True})[1]['data']['count'], 1)
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 0)

    def test_group_business_roster_preserves_reads_preferences_and_core_manual_join(self):
        self.activate()
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[11], order_no='20261120000000000001')
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.core('preferences', query={'conversation_id': 1}, payload={'remark': 'my note', 'pinned': True})
        self.core('read', query={'conversation_id': 1}, payload={'seq': 1})
        msg = self.core('send', query={'conversation_id': 1}, payload={'text': 'pin me', 'client_id': 'pin-one'})[1]['data']
        self.assertEqual(self.core('pin', query={'conversation_id': 1}, payload={'message_id': msg['id']})[0], 200)
        initial_cursor = self.core('status')[1]['data']['cursor']
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('status')[1]['data']['cursor'], initial_cursor)
        dto = self.core('order', query={'conversation_id': 1})[1]['data']
        self.assertEqual((dto['remark'], dto['pinned'], dto['read_seq'], dto['pinned_message_id']), ('my note', True, 1, msg['id']))
        self.assertEqual(self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})[0], 200)
        self.core('room', payload=room)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        room['members'][0]['left_at'] = '2026-11-20T00:00:00Z'
        self.core('room', payload=room)
        self.authority_denied['a'].add(11)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 403)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'denied', 'client_id': 'old-worker'})[0], 403)
        self.assertEqual(self.core('history', viewer=99, query={'conversation_id': 1}, readonly=True)[0], 200)
        removed = self.core('sync', query={'after': initial_cursor})[1]['data']
        self.assertIn('conversation.removed', [x['kind'] for x in removed['changes']])
        self.assertFalse(any(x['message'] for x in removed['changes']))

    def test_two_studios_own_same_reserved_identifiers_and_reject_invalid_writes(self):
        self.activate('a'); self.activate('b')
        for tenant, word in [('a', 'Alpha private'), ('b', 'Bravo private')]:
            self.assertEqual(self.core('send', tenant=tenant, query={'conversation_id': 1}, payload={'text': word, 'client_id': 'same'})[1]['data']['id'], 1_000_000_000)
        for tenant, other in [('a', 'Bravo private'), ('b', 'Alpha private')]:
            self.assertNotIn(other, str(self.core('history', tenant=tenant, query={'conversation_id': 1})[1]['data']))
        invalid = [
            {'operation': {}, 'viewer_id': 11},
            {'operation': 'send', 'viewer_id': True, 'query': {'conversation_id': 1}, 'payload': {'text': 'x', 'client_id': 'bad'}},
            {'operation': 'send', 'viewer_id': 11, 'query': {'conversation_id': 1, 'tenant_id': 'b'}, 'payload': {'text': 'x', 'client_id': 'bad'}},
            {'operation': 'send', 'viewer_id': 11, 'query': {'conversation_id': 1}, 'payload': {'text': 'x', 'client_id': 'bad'}, 'readonly': True},
            {'operation': 'send', 'viewer_id': 11, 'query': {'conversation_id': 1}, 'payload': {'client_id': 'bad', 'image': {'mime': [], 'data': ''}}},
            {'operation': 'read', 'viewer_id': 11, 'query': {'conversation_id': 1}, 'payload': {'seq': 999}},
            {'operation': 'send', 'viewer_id': 11, 'query': {'conversation_id': 1}, 'payload': {'text': 'bad\x00text', 'client_id': 'bad'}},
            {'operation': 'send', 'viewer_id': 11, 'query': {'conversation_id': 1}, 'payload': {'text': '\ud800', 'client_id': 'bad'}},
        ]
        for body in invalid:
            with self.subTest(body=body):
                self.assertIn(self.request('/v1/core', body)[0], (400, 403))
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 1)

    def test_realtime_notification_is_durable_and_requires_successful_gateway_status(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        calls = []
        response_status = [200]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                calls.append((handler.path, handler.headers.get('Authorization'), json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))))
                handler.send_response(response_status[0]); handler.end_headers()
            def log_message(handler, *args):
                pass
        gateway = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True); thread.start()
        try:
            self.activate()
            self.engine.tenants['a']['realtime']['url'] = f'http://127.0.0.1:{gateway.server_port}'
            cid = self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[1]['data']['id']
            message = self.core('send', query={'conversation_id': cid}, payload={'text': 'notify B', 'client_id': 'notify'})[1]['data']
            self.engine.core.tick('a')
            self.assertGreater(self.core('status')[1]['data']['pending_notifications'], 0)
            response_status[0] = 204
            with self.store.connect() as db:
                db.execute("UPDATE core_notifications SET available_at=now() WHERE tenant_id='a'")
            self.engine.core.tick('a')
            self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)
            incoming = next({**item, 'tenant': body['tenant']} for _, _, body in calls for item in body['notifications'] if item.get('incoming', {}).get('id') == message['id'])
            self.assertEqual((incoming['tenant'], sorted(incoming['users']), incoming['incoming']['author']), ('studio_a', [11, 12], 11))
            self.assertTrue(all(path == '/notify' and token == 'Bearer gateway-secret' for path, token, _ in calls))
        finally:
            gateway.shutdown(); gateway.server_close(); thread.join()

    def test_125_messages_recover_with_cursor_pagination_and_no_duplicate_acceptance(self):
        self.activate()
        identifiers = []
        start = self.core('status')[1]['data']['cursor']
        for index in range(125):
            payload = {'text': f'offline-{index:03}', 'client_id': f'offline-{index:03}'}
            message = self.core('send', query={'conversation_id': 1}, payload=payload)[1]['data']
            identifiers.append(message['id'])
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 125)
        changes, cursor = [], start
        while True:
            page = self.core('sync', query={'after': cursor})[1]['data']
            changes += [c['message']['id'] for c in page['changes'] if c['message']]
            cursor = page['cursor']
            if not page['has_more']:
                break
        self.assertEqual(changes, identifiers)
        found, before = [], None
        while True:
            query = {'conversation_id': 1, 'q': 'offline-'}
            if before is not None:
                query['before'] = before
            page = self.core('search', query=query)[1]['data']
            self.assertEqual(page['count'], 125)
            found.extend(m['id'] for m in page['messages'])
            if not page['has_more']:
                break
            before = page['messages'][-1]['seq']
        self.assertEqual(found, list(reversed(identifiers)))
        self.assertEqual(self.core('history', query={'conversation_id': 1, 'around': identifiers[60]})[0], 200)
        while self.core('status')[1]['data']['pending_messages']:
            self.engine.core.tick('a')
        self.assertEqual(len(self.events), 126)
        self.assertEqual(len({m['id'] for m in self.core('history', query={'conversation_id': 1})[1]['data']['messages']}), 60)

    def test_concurrent_workers_cannot_send_the_same_pending_message_twice(self):
        import threading
        from unittest.mock import patch
        self.activate()
        self.core('send', query={'conversation_id': 1}, payload={'text': 'concurrent', 'client_id': 'concurrent'})
        entered, finish = threading.Event(), threading.Event()
        original = self.engine.matrix.send
        def paused(*args, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(3))
            return original(*args, **kwargs)
        result = []
        with patch.object(self.engine.matrix, 'send', side_effect=paused):
            one = threading.Thread(target=lambda: result.append(self.engine.core.tick('a')))
            one.start(); self.assertTrue(entered.wait(3))
            busy = self.engine.core.tick('a')
            self.assertEqual(busy['processed'], 0)
            self.assertTrue(busy['has_more'])
            finish.set(); one.join(5); self.assertFalse(one.is_alive())
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 0)
        sends = [r for r in self.matrix_requests if '/send/m.room.message/' in r[1] and r[2].get('body') == 'concurrent']
        self.assertEqual(len(sends), 1)

    def test_business_roster_keeps_service_manual_member_id_when_business_later_assigns(self):
        self.activate()
        joined = self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})[1]['data']
        mid = joined['self_member_id']
        sent = self.core('send', viewer=12, query={'conversation_id': 1}, payload={'text': 'manual author', 'client_id': 'manual'})[1]['data']
        room = self.conversation()
        room['members'].append({'id': 99, 'local_user_id': 12, 'name': 'Now worker', 'left_at': None, 'order_role': 'worker'})
        self.assertEqual(self.core('room', payload=room)[0], 200)
        dto = self.core('order', viewer=12, query={'conversation_id': 1})[1]['data']
        self.assertEqual(dto['self_member_id'], mid)
        self.assertEqual(sent['sender_id'], mid)
        self.assertEqual(len([m for m in dto['members'] if m['id'] == mid]), 1)
        self.assertEqual(self.core('notification', viewer=12, query={'conversation_id': 1})[1]['data']['self_member_id'], mid)

    def test_imported_order_group_requires_fresh_business_send_and_pin_grants(self):
        self.seed()
        self.engine.tenants['a']['realtime'] = {'url': 'http://127.0.0.1:9998', 'secret': 'gateway-secret', 'tenant': 'studio_a'}
        self.assertEqual(self.core('activate', payload={'minimum_cursor': 10})[0], 200)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'not permitted yet', 'client_id': 'no-grant'})[0], 403)
        self.assertEqual(self.core('pin', query={'conversation_id': 1}, payload={'message_id': 1})[0], 403)
        self.assertFalse(self.core('notification', query={'conversation_id': 1})[1]['data']['can_reply'])
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)
        room = self.conversation();room.update(can_send=True, can_pin_user_ids=[11])
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'permitted', 'client_id': 'granted'})[0], 200)

    def test_conversations_cursor_never_skips_message_committed_during_list_render(self):
        import threading
        from unittest.mock import patch
        self.activate()
        cid = self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[1]['data']['id']
        ready, release, wrote = threading.Event(), threading.Event(), threading.Event()
        original = self.engine.core._dto
        replies, sent, errors = [], [], []
        def paused(db, tenant, room, viewer):
            dto = original(db, tenant, room, viewer)
            if room['conversation_id'] == cid and viewer == 12:
                ready.set()
                if not release.wait(5):
                    raise RuntimeError('List reader was not released')
            return dto
        def listing():
            try:
                replies.append(self.core('conversations', viewer=12))
            except Exception as error:
                errors.append(error)
        def sending():
            try:
                sent.append(self.core('send', query={'conversation_id': cid}, payload={'text': 'racing message', 'client_id': 'racing'}))
            except Exception as error:
                errors.append(error)
            finally:
                wrote.set()
        with patch.object(self.engine.core, '_dto', side_effect=paused):
            reader = threading.Thread(target=listing); reader.start()
            writer = None
            try:
                self.assertTrue(ready.wait(3))
                writer = threading.Thread(target=sending); writer.start()
                self.assertTrue(wrote.wait(3), 'A list read must not hold the tenant write lock')
            finally:
                release.set(); reader.join(5)
                if writer is not None:
                    writer.join(5)
        self.assertEqual(errors, [])
        self.assertEqual((replies[0][0], sent[0][0]), (200, 200))
        listing_data, mid = replies[0][1]['data'], sent[0][1]['data']['id']
        visible = {r['last_message']['id'] for r in listing_data['conversations'] if r['last_message']}
        delta = self.core('sync', viewer=12, query={'after': listing_data['cursor']})[1]['data']
        visible.update(c['message']['id'] for c in delta['changes'] if c['message'])
        self.assertIn(mid, visible)

    def test_new_reply_to_revoked_message_is_denied_but_existing_reply_retry_is_stable(self):
        self.activate()
        target = self.core('send', query={'conversation_id': 1}, payload={'text': 'reply target', 'client_id': 'target'})[1]['data']
        valid = {'text': 'normal reply', 'client_id': 'valid-reply', 'reply_to_id': target['id']}
        status, reply = self.core('send', query={'conversation_id': 1}, payload=valid)
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['data']['reply_to_id'], target['id'])
        self.assertEqual(self.core('revoke', query={'conversation_id': 1, 'message_id': target['id']})[0], 200)
        before = self.core('status')[1]['data']['cursor']
        denied = self.core('send', query={'conversation_id': 1}, payload={'text': 'late reply', 'client_id': 'late-reply', 'reply_to_id': target['id']})
        self.assertEqual(denied[0], 404, denied)
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        retry = self.core('send', query={'conversation_id': 1}, payload=valid)
        self.assertEqual(retry[0], 200, retry)
        self.assertEqual(retry[1]['data']['id'], reply['data']['id'])

    def test_manual_follow_survives_worker_reassignment_but_explicit_business_denial_wins(self):
        self.activate()
        self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[11])
        room['members'].append({'id': 99, 'local_user_id': 12, 'name': 'Now worker', 'left_at': None, 'order_role': 'worker'})
        self.assertEqual(self.core('room', payload=room)[0], 200)
        room['members'][-1]['left_at'] = '2026-11-20T00:00:00Z'
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        dto = self.core('order', viewer=12, query={'conversation_id': 1})[1]['data']
        self.assertEqual(next(m for m in dto['members'] if m['id'] == dto['self_member_id'])['order_role'], 'worker')
        room['denied_user_ids'] = [12]
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 403)
        self.assertEqual(self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})[0], 403)
        room['denied_user_ids'] = []
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})[0], 200)

    def test_album_persists_each_change_but_only_first_image_requests_desktop_alert(self):
        import base64
        import io
        from PIL import Image
        self.activate()
        data = io.BytesIO(); Image.new('RGB', (3, 2), '#234567').save(data, 'PNG')
        image = {'mime': 'image/png', 'data': base64.b64encode(data.getvalue()).decode()}
        before = self.core('status')[1]['data']['cursor']
        status, response = self.core('send', query={'conversation_id': 1}, payload={'text': 'album alert', 'client_id': 'alert-album', 'images': [image, image]})
        self.assertEqual(status, 200, response)
        ids = [m['id'] for m in response['data']['messages']]
        page = self.core('sync', query={'after': before})[1]['data']
        self.assertEqual([c['message']['id'] for c in page['changes']], ids)
        with self.store.connect() as db:
            notifications = db.execute('SELECT payload FROM core_notifications WHERE tenant_id=%s AND event_id>%s ORDER BY event_id', ('a', before)).fetchall()
        self.assertEqual(len(notifications), 2)
        alerts = [n['payload']['incoming']['id'] for n in notifications if 'incoming' in n['payload']]
        self.assertEqual(alerts, [ids[0]])

    def test_binding_restriction_preserves_valid_manual_exemption_but_not_current_hard_denial(self):
        self.activate()
        self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[], denied_user_ids=[], restricted_user_ids=[11, 12])
        room['members'].append({'id': 99, 'local_user_id': 12, 'name': 'Manual', 'left_at': None, 'order_role': 'worker'})
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.authority_denied['a'].add(11)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 403)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        room['denied_user_ids'] = [12]
        self.core('room', payload=room)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 403)
        room['denied_user_ids'] = []
        self.core('room', payload=room)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})[0], 200)
        self.core('room', payload=room)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)

    def test_hard_denial_applies_to_manual_member_missing_from_business_snapshot(self):
        self.activate()
        self.core('join', viewer=12, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Manual'}})
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[11], denied_user_ids=[12], restricted_user_ids=[])
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 403)
        before = self.core('status')[1]['data']['cursor']
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        room['denied_user_ids'] = []
        self.core('room', payload=room)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)

    def test_join_is_noop_for_active_worker_and_does_not_create_permanent_follow(self):
        self.activate()
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[], denied_user_ids=[], restricted_user_ids=[])
        room['members'][0]['order_role'] = 'worker'
        self.core('room', payload=room)
        before = self.core('status')[1]['data']['cursor']
        self.assertEqual(self.core('join', viewer=11, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 11, 'name': 'Worker'}})[0], 200)
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        room['members'][0]['left_at'] = '2026-11-20T00:00:00Z'
        room['members'][0]['order_role'] = ''
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.authority_denied['a'].add(11)
        self.assertEqual(self.core('history', viewer=11, query={'conversation_id': 1})[0], 403)

    def test_policy_clock_is_tenant_local_and_does_not_create_chat_changes(self):
        self.activate('a'); self.activate('b')
        before = self.core('status')[1]['data']['cursor']
        first = self.core('policy', viewer=None)
        second = self.core('policy', viewer=None)
        other = self.core('policy', viewer=None, tenant='b')
        self.assertEqual(first[0], 200, first)
        self.assertEqual(second[1]['data']['policy_version'], first[1]['data']['policy_version'] + 1)
        self.assertEqual(other[1]['data']['policy_version'], first[1]['data']['policy_version'])
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        self.assertEqual(self.core('policy', viewer=None, query={'conversation_id': 1})[0], 400)
        self.assertEqual(self.core('policy', viewer=None, readonly=True)[0], 403)


    def test_delayed_old_business_policy_cannot_restore_revoked_member(self):
        import copy
        import threading
        self.activate()
        old = self.conversation()
        old.update(can_send=True, can_pin_user_ids=[11])
        old.update(self.core('policy', viewer=None)[1]['data'])
        waiting, release = threading.Event(), threading.Event()
        delayed = []
        def late_request():
            waiting.set()
            if release.wait(5):
                delayed.append(self.core('room', viewer=None, payload=old))
        thread = threading.Thread(target=late_request)
        thread.start()
        self.assertTrue(waiting.wait(3))
        try:
            current = copy.deepcopy(old)
            current.update(self.core('policy', viewer=None)[1]['data'])
            current['denied_user_ids'] = [11]
            current['members'][0]['left_at'] = '2026-11-20T00:00:00Z'
            self.assertEqual(self.core('room', viewer=None, payload=current)[0], 200)
            self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 403)
            cursor = self.core('status')[1]['data']['cursor']
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(delayed[0][0], 200, delayed)
        self.assertEqual(delayed[0][1]['data'], {'conversation_id': 1, 'applied': False})
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 403)
        self.assertEqual(self.core('status')[1]['data']['cursor'], cursor)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'no access', 'client_id': 'stale-policy'})[0], 403)

    def test_policy_replay_conflict_and_unissued_versions_do_not_modify_chat(self):
        self.activate('a'); self.activate('b')
        room = self.conversation()
        room.update(can_send=True, can_pin_user_ids=[11])
        room.update(self.core('policy', viewer=None)[1]['data'])
        before = self.core('status')[1]['data']['cursor']
        applied = self.core('room', viewer=None, payload=room)
        self.assertEqual(applied[0], 200, applied)
        self.assertEqual(applied[1]['data']['applied'], True)
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        replay = self.core('room', viewer=None, payload=room)
        self.assertEqual(replay[0], 200, replay)
        self.assertEqual(replay[1]['data']['applied'], False)
        self.assertEqual(self.core('room', viewer=None, payload={**room, 'denied_user_ids': [11]})[0], 409)
        self.assertEqual(self.core('room', viewer=None, payload={**room, 'policy_version': room['policy_version'] + 1})[0], 409)
        self.assertEqual(self.core('room', tenant='b', viewer=None, payload=room)[0], 409)
        for invalid in (None, 0, True):
            payload = {key: value for key, value in room.items() if key != 'policy_version'}
            if invalid is not None:
                payload['policy_version'] = invalid
            status, result = self.request('/v1/core', {'operation': 'room', 'viewer_id': None, 'query': {}, 'payload': payload})
            self.assertIn(status, (400, 409), result)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)
        room.update(self.core('policy', viewer=None)[1]['data'])
        self.assertTrue(self.core('room', viewer=None, payload=room)[1]['data']['applied'])
        self.assertEqual(self.core('status')[1]['data']['cursor'], before)

    def test_order_history_rejects_missing_authority_even_with_saved_membership(self):
        self.activate()
        self.authority_patch.stop()
        response = self.core('history', query={'conversation_id': 1})
        self.assertEqual(response[0], 503, response)
        self.assertEqual(response[1]['msg'], '暂时无法确认订单权限，请稍后重试')


def seed_imported_order(test, room, tenant='a'):
    from psycopg.types.json import Jsonb
    with test.store.tenant(tenant) as db:
        metadata = {key:value for key,value in room.items() if key != 'members'}
        db.execute('''INSERT INTO rooms(tenant_id,conversation_id,room_id,kind,metadata)
            VALUES(%s,%s,%s,'order',%s)''', (tenant, room['id'], 'pending:' + str(room['id']), Jsonb(metadata)))
        for member in room['members']:
            db.execute('''INSERT INTO members(tenant_id,conversation_id,member_id,local_user_id,active,metadata)
                VALUES(%s,%s,%s,%s,true,%s)''', (tenant, room['id'], member['id'], member.get('local_user_id'), Jsonb(member)))
