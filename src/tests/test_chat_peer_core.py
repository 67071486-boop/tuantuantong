import base64
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from chat_service.config import ServiceError
from tests import test_chat_service_engine as fixture


class PeerCoreHTTPTests(TestCase):
    setUpClass = classmethod(fixture.ServiceHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ServiceHTTPTests.tearDownClass.__func__)
    tearDown = fixture.ServiceHTTPTests.tearDown
    request = fixture.ServiceHTTPTests.request
    seed = fixture.ServiceHTTPTests.seed
    conversation = fixture.ServiceHTTPTests.conversation
    message = fixture.ServiceHTTPTests.message
    matrix_reply = fixture.ServiceHTTPTests.matrix_reply

    numbers = {'a': '20261121000000000001', 'b': '20261121000000000002', 'c': '20261121000000000003'}
    group = 'f129115a-1845-4fd0-a520-61c352b0077a'
    next_group = 'ac531317-0236-4add-853c-1f6dc0c1b522'

    def setUp(self):
        fixture.ServiceHTTPTests.setUp(self)
        with self.store.connect() as db:
            db.execute('TRUNCATE core_control,core_order_rooms,core_identities,core_reads,core_preferences,core_events,'
                       'core_commands,core_outbox,core_notifications,core_archives,'
                       'core_peer_groups,core_peer_messages,core_peer_outbox,core_peer_imports')
        self.inbox = {'a': [], 'b': [], 'c': []}
        self.pushed = {}
        self.denied = {'a': set(), 'b': set()}
        self.active = {'a': True, 'b': True, 'c': True}
        self.rotated = False
        self.lower_worker = None
        self.fail_ack = False
        self.fail_push = False
        self.independent_order = False
        self.enterContext(patch('chat_service.core_authority.CoreAuthority._source_request', side_effect=self.source))
        for tenant in ('a', 'b'):
            self.activate(tenant)

    def activate(self, tenant):
        self.seed(tenant)
        self.engine.tenants[tenant]['realtime'] = {
            'url': 'http://127.0.0.1:9998/notify', 'secret': 'gateway-secret', 'tenant': 'studio_' + tenant,
        }
        self.core(tenant, 'activate', payload={'minimum_cursor': 10})

    def source(self, tenant, operation, body, **_kwargs):
        number = self.numbers[tenant]
        publisher = tenant == 'a'
        new_generation = publisher and self.rotated or tenant == 'c'
        peer = {'group_id': self.next_group if new_generation else self.group,
                'generation': 2 if publisher and self.rotated else 1,
                'side': 'downstream' if publisher else 'upstream',
                'order_no': self.numbers['a'], 'active': self.active[tenant],
                'publisher_name': '上家客服', 'publisher_studio_name': '验收甲',
                'acceptor_user_id': None if publisher else 11}
        profile = {'local_user_id': 11, 'name': peer['publisher_name'] if publisher else f'下家接单员-{tenant}',
                   'studio_name': f'验收-{tenant}', 'order_role': 'publisher' if publisher else 'acceptor'}
        members = [profile]
        if tenant == 'b' and self.lower_worker is not None:
            members.append({'local_user_id': self.lower_worker, 'name': f'下家打手-{self.lower_worker}',
                            'studio_name': '验收乙', 'order_role': 'worker'})
        granted = [m['local_user_id'] for m in members if m['local_user_id'] not in self.denied.get(tenant, ())]
        fact = {'order_no': number, 'read_user_ids': granted,
                'reopen_user_ids': [], 'default_join_user_ids': [m['local_user_id'] for m in members], 'can_pin_user_ids': [11],
                'completed': False, 'deleted': False, 'publisher_id': 11 if publisher else None,
                'participants': members, 'can_send': publisher or self.active[tenant], 'peer_link': peer}
        if tenant == 'b':
            fact['worker_id'] = self.lower_worker
        if operation == 'order-authorizations':
            facts = {number: fact}
            if tenant == 'b' and self.independent_order:
                independent = {**fact, 'order_no': self.numbers['a']}
                del independent['peer_link']
                facts[independent['order_no']] = independent
            groups, orders = [], []
            for check in body['checks']:
                local = facts.get(check.get('order_no'))
                if local is None:
                    continue
                if 'conversation_id' not in check:
                    orders.append(local)
                    continue
                cid = check['conversation_id']
                groups.append({**local, 'conversation_id': cid,
                               'room': {'id': cid, 'kind': 'order', 'members': [],
                                        'local_order_no': local['order_no'], 'can_send': local['can_send'],
                                        'can_pin_user_ids': [11]}})
            return {'orders': orders, 'groups': groups, 'has_order_access': True}
        if operation == 'order-intents':
            if body['operation'] == 'pull':
                return {'peer_chat': True, 'intents': [{'id': 1, 'revision': 1, 'ensure_group': True, 'order': fact}]}
            return {'acked': 1}
        self.assertEqual(operation, 'peer-chat')
        if body['operation'] == 'pull':
            return {'events': self.inbox[tenant][:body['limit']]}
        if body['operation'] == 'defer':
            return {'deferred': len(body['events'])}
        if body['operation'] == 'ack':
            if self.fail_ack:
                raise ServiceError('simulated ack failure', 503)
            self.inbox[tenant] = [event for event in self.inbox[tenant] if event['id'] not in body['events']]
            return {'acked': len(body['events'])}
        self.assertEqual(body['operation'], 'push')
        key = (tenant, body['event_key'])
        if key not in self.pushed:
            target = ('c' if body['group_id'] == self.next_group else 'b') if publisher else 'a'
            self.inbox[target].append({'id': len(self.pushed) + 1, 'order_no': self.numbers[target],
                                       **{k: body[k] for k in ('kind', 'group_id', 'payload')}})
            self.pushed[key] = body
        self.assertEqual(body, self.pushed[key])
        if self.fail_push:
            raise ServiceError('response lost after enqueue', 503, retry_after=.1)
        return {'queued': True}

    def core(self, tenant, operation, *, viewer=11, query=None, payload=None, expected=200):
        status, response = self.request('/v1/core', {'operation': operation, 'viewer_id': viewer,
            'query': query or {}, 'payload': payload or {}}, tenant)
        self.assertEqual(status, expected, response)
        return response.get('data')

    def tick(self, tenant):
        with patch('chat_service.core_delivery.httpx.post', return_value=type('Accepted', (), {'status_code': 204})()):
            return self.engine.core.tick(tenant)

    def room(self, tenant):
        return self.core(tenant, 'order_preview', query={'order_no': self.numbers[tenant]})

    def send(self, tenant, payload):
        return self.core(tenant, 'order_send', query={'order_no': self.numbers[tenant]}, payload={
            'viewer': {'id': 11, 'name': '合成打手', 'avatar_url': None}, 'message': payload,
        })['messages']

    def history(self, tenant, cid):
        return self.core(tenant, 'history', query={'conversation_id': cid})['messages']

    def test_two_orders_exchange_image_with_retry_and_require_local_order_permission(self):
        for tenant in ('a', 'b'):
            self.tick(tenant)
        photo = BytesIO()
        Image.new('RGB', (7, 3), 'blue').save(photo, format='PNG')
        data = base64.b64encode(photo.getvalue()).decode()
        payload = {'client_id': 'photo-1', 'text': '来自甲的测试图片', 'image': {'mime': 'image/png', 'data': data}}
        original, = self.send('a', payload)
        self.assertEqual(self.send('a', payload), [original])
        self.fail_push = True
        self.tick('a')
        self.assertGreater(self.core('a', 'status')['pending_peer_events'], 0)
        self.fail_push = False
        self.fail_ack = True
        self.tick('b')
        self.fail_ack = False
        self.tick('a')
        self.tick('b')
        room = self.room('b')
        self.assertEqual(room['local_order_no'], self.numbers['b'])
        self.assertEqual(room['order_no'], self.numbers['a'])
        copied, = self.history('b', room['id'])
        self.assertEqual(copied['text'], payload['text'])
        self.assertEqual(copied['image'], {'mime': 'image/png', 'width': 7, 'height': 3})
        self.assertEqual(self.core('b', 'image', query={'conversation_id': room['id'], 'message_id': copied['id']})['data'], data)
        self.assertEqual(len([event for event in self.pushed.values() if event['kind'] == 'chat.order_message']), 1)
        self.denied['b'].add(11)
        self.core('b', 'image', query={'conversation_id': room['id'], 'message_id': copied['id']}, expected=403)


    def test_two_directions_keep_album_reply_mention_pin_and_revocation(self):
        for tenant in ('a', 'b'):
            self.tick(tenant)
        note, = self.send('a', {'client_id': 'prompt', 'text': '上家发来的问题'})
        self.tick('a')
        self.tick('b')
        upstream, downstream = self.room('a'), self.room('b')
        mirror, = self.history('b', downstream['id'])
        remote_publisher = next(member['id'] for member in downstream['members'] if member['name'] == '上家客服')
        photo = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(photo, format='PNG')
        image = {'mime': 'image/png', 'data': base64.b64encode(photo.getvalue()).decode()}
        sent = self.send('b', {'client_id': 'two-photos', 'text': '下家回复的截图',
                               'reply_to_id': mirror['id'], 'mention_member_ids': [remote_publisher],
                               'images': [image, image]})
        self.tick('b')
        self.tick('a')
        self.assertEqual(len(sent), 2)
        original, copied, second = self.history('a', upstream['id'])
        self.assertEqual(original['id'], note['id'])
        self.assertEqual(copied['reply_to_id'], note['id'])
        self.assertEqual(copied['mention_member_ids'], [upstream['self_member_id']])
        self.assertEqual((copied['text'], second['text']), ('下家回复的截图', ''))
        self.assertEqual(copied['album_id'], second['album_id'])
        self.assertEqual(self.core('a', 'image', query={'conversation_id': upstream['id'], 'message_id': second['id']})['data'],
                         image['data'])
        self.core('a', 'pin', query={'conversation_id': upstream['id']}, payload={'message_id': second['id']})
        self.tick('a')
        self.tick('b')
        self.assertEqual(self.room('b')['pinned_message_id'], sent[1]['id'])
        self.core('b', 'revoke', query={'conversation_id': downstream['id'], 'message_id': sent[1]['id']})
        self.tick('b')
        self.tick('a')
        self.assertIsNone(self.room('a')['pinned_message_id'])
        self.assertIsNone(self.room('b')['pinned_message_id'])
        messages = self.history('a', upstream['id'])
        self.assertEqual(len(messages), 3)
        self.assertFalse(messages[1]['revoked'])
        self.assertTrue(messages[2]['revoked'])
        self.core('a', 'image', query={'conversation_id': upstream['id'], 'message_id': second['id']}, expected=404)

    def test_detach_keeps_accepted_history_and_disables_old_peer(self):
        for tenant in ('a', 'b'):
            self.tick(tenant)
        upstream, downstream = self.room('a'), self.room('b')
        self.send('a', {'client_id': 'pre-detach', 'text': '改派前已接收'})
        self.tick('a')
        self.active['a'] = False
        self.active['b'] = False
        self.inbox['b'].append({'id': 50, 'order_no': self.numbers['b'], 'group_id': self.group,
                                'kind': 'chat.order_detached', 'payload': {}})
        self.tick('b')
        self.assertFalse(self.room('b')['can_send'])
        self.assertEqual([m['text'] for m in self.history('b', downstream['id'])], ['改派前已接收'])
        self.core('b', 'send', query={'conversation_id': downstream['id']},
                  payload={'client_id': 'no-longer-bound', 'text': '失效后消息'}, expected=403)
        self.active['b'] = True
        self.assertFalse(self.room('b')['can_send'])
        self.send('a', {'client_id': 'new-assignment', 'text': '改派后仅上家'})
        self.tick('a')
        self.tick('b')
        self.assertEqual([m['text'] for m in self.history('a', upstream['id'])], ['改派前已接收', '改派后仅上家'])
        self.assertEqual([m['text'] for m in self.history('b', downstream['id'])], ['改派前已接收'])

    def test_valid_group_cannot_import_an_event_for_another_local_order(self):
        for tenant in ('a', 'b'):
            self.tick(tenant)
        self.send('a', {'client_id': 'valid-source', 'text': '错误本地单号不能入库'})
        self.tick('a')
        self.inbox['b'][-1]['order_no'] = self.numbers['a']
        room = self.room('b')
        self.tick('b')
        self.assertEqual(self.history('b', room['id']), [])
        self.assertEqual(len(self.inbox['b']), 1)

    def test_source_order_number_cannot_capture_an_independent_local_room(self):
        self.independent_order = True
        for tenant in ('a', 'b'):
            self.tick(tenant)
        peer = self.room('b')
        self.assertEqual(peer['order_no'], self.numbers['a'])
        independent = self.core('b', 'order_send', query={'order_no': self.numbers['a']}, payload={
            'viewer': {'id': 11, 'name': '本地同号订单客服', 'avatar_url': None},
            'message': {'client_id': 'same-display-number', 'text': '只准出现在下家的本地单'},
        })
        self.assertNotEqual(independent['conversation']['id'], peer['id'])
        self.assertEqual(independent['conversation']['local_order_no'], self.numbers['a'])
        self.tick('b')
        self.tick('a')
        self.assertEqual(self.history('b', peer['id']), [])
        self.assertEqual(self.history('a', self.room('a')['id']), [])

    def test_new_workshop_cannot_see_previous_counterparty_content_or_use_its_identity(self):
        for tenant in ('a', 'b'):
            self.tick(tenant)
        upstream = self.room('a')
        self.send('a', {'client_id': 'previous-local', 'text': '上家的历史消息'})
        self.tick('a')
        self.tick('b')
        self.send('b', {'client_id': 'previous-remote', 'text': '原下家只给上家'})
        self.tick('b')

        self.active['b'] = False
        self.rotated = True
        self.engine.tenants['c'] = {'secret': 'c-secret'}
        self.activate('c')
        self.tick('c')
        self.send('a', {'client_id': 'next-local', 'text': '新下家接单后的消息'})
        self.tick('a')
        self.tick('c')
        self.send('c', {'client_id': 'next-remote', 'text': '新下家的合成回信'})
        self.tick('c')
        self.tick('a')

        a = self.history('a', upstream['id'])
        b = self.history('b', self.room('b')['id'])
        c = self.history('c', self.room('c')['id'])
        self.assertEqual({m['text'] for m in a}, {
            '上家的历史消息', '原下家只给上家', '新下家接单后的消息', '新下家的合成回信',
        })
        self.assertEqual([m['text'] for m in b], ['上家的历史消息', '原下家只给上家'])
        self.assertEqual([m['text'] for m in c], ['上家的历史消息', '新下家接单后的消息', '新下家的合成回信'])
        old, new = (next(m['sender_id'] for m in a if m['text'] == marker)
                    for marker in ('原下家只给上家', '新下家的合成回信'))
        self.assertNotEqual(old, new)
        members = {m['id']: m for m in self.room('a')['members']}
        self.assertEqual(members[old]['permission_state'], 'denied')
        self.assertEqual(members[new]['permission_state'], 'allowed')

    def test_lower_workshop_reassignment_retires_previous_worker_in_both_rosters(self):
        self.lower_worker = 12
        for tenant in ('a', 'b', 'b', 'a'):
            self.tick(tenant)
        lower = self.room('b')
        old_peer = next(m for m in self.room('a')['members'] if m['name'] == '下家打手-12')
        self.assertIsNone(old_peer['left_at'])
        self.lower_worker = 13
        self.tick('b')
        self.tick('a')
        for room in (self.room('a'), self.room('b')):
            departed = next(m for m in room['members'] if m['name'] == '下家打手-12')
            joined = next(m for m in room['members'] if m['name'] == '下家打手-13')
            self.assertIsNotNone(departed['left_at'])
            self.assertEqual(departed['permission_state'], 'denied')
            self.assertIsNone(joined['left_at'])
            self.assertEqual(joined['permission_state'], 'allowed')
        self.core('b', 'send', viewer=12, query={'conversation_id': lower['id']},
                  payload={'client_id': 'old-worker', 'text': '旧打手不可发'}, expected=403)
        self.core('b', 'send', viewer=13, query={'conversation_id': lower['id']},
                  payload={'client_id': 'new-worker', 'text': '新打手可发'})
