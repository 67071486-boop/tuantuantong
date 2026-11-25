import base64
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from chat_service.config import ServiceError
from chat_service.core_authority import REVOKED, UNAVAILABLE
from chat_service.protocol import verify_request
from tests import test_chat_core_engine as core_tests


class OrderAuthorityCoreTests(unittest.TestCase):
    setUpClass = classmethod(core_tests.CoreHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(core_tests.CoreHTTPTests.tearDownClass.__func__)
    setUp = core_tests.CoreHTTPTests.setUp
    tearDown = core_tests.CoreHTTPTests.tearDown
    matrix_reply = core_tests.CoreHTTPTests.matrix_reply
    request = core_tests.CoreHTTPTests.request
    conversation = core_tests.CoreHTTPTests.conversation
    message = core_tests.CoreHTTPTests.message
    seed = core_tests.CoreHTTPTests.seed
    core = core_tests.CoreHTTPTests.core
    activate = core_tests.CoreHTTPTests.activate
    authority_reply = core_tests.CoreHTTPTests.authority_reply

    def join(self, viewer=12):
        result = self.core('join', viewer=viewer, query={'conversation_id': 1},
            payload={'grant': True, 'viewer': {'id': viewer, 'name': 'Worker'}})
        self.assertEqual(result[0], 200, result)
        return result[1]['data']

    def send(self, text, viewer=11):
        result = self.core('send', viewer=viewer, query={'conversation_id': 1}, payload={'text': text, 'client_id': text})
        self.assertEqual(result[0], 200, result)
        return result[1]['data']

    def test_revocation_hides_open_group_without_mutating_permanent_participation(self):
        self.activate(); self.join()
        cursor = self.core('status')[1]['data']['cursor']
        with self.store.connect() as db:
            before = db.execute("SELECT * FROM members WHERE tenant_id='a' ORDER BY member_id").fetchall()
        self.authority_denied['a'].add(12)
        for op, query in [('history', {'conversation_id': 1}), ('search', {'conversation_id': 1, 'q': 'hello'}), ('image', {'conversation_id': 1, 'message_id': 1})]:
            result = self.core(op, viewer=12, query=query)
            self.assertEqual((result[0], result[1]['msg']), (403, REVOKED))
        self.assertEqual(self.core('send', viewer=12, query={'conversation_id': 1}, payload={'text': 'denied', 'client_id': 'denied'})[0], 403)
        self.assertEqual(self.core('conversations', viewer=12)[1]['data']['conversations'], [])
        self.assertEqual(self.core('unread', viewer=12)[1]['data']['unread_count'], 0)
        self.assertEqual(self.core('sync', viewer=12, query={'after': cursor})[1]['data']['changes'], [{'kind': 'conversation.removed', 'conversation_id': 1, 'message': None}])
        visible = self.core('order', query={'conversation_id': 1})[1]['data']
        former = next(m for m in visible['members'] if m['name'] == 'Worker')
        self.assertFalse(former['has_permission']); self.assertIsNone(former['left_at'])
        with self.store.connect() as db:
            self.assertEqual(before, db.execute("SELECT * FROM members WHERE tenant_id='a' ORDER BY member_id").fetchall())
        self.assertEqual(self.core('status')[1]['data']['cursor'], cursor)

    def test_readonly_customer_sees_full_history_and_image_but_joins_before_send(self):
        self.activate()
        png = io.BytesIO(); Image.new('RGB', (3, 2)).save(png, 'PNG')
        sent = self.core('send', query={'conversation_id': 1}, payload={'text': 'prior image', 'client_id': 'prior-image', 'image': {'mime': 'image/png', 'data': base64.b64encode(png.getvalue()).decode()}})[1]['data']
        self.assertEqual(len(self.core('history', viewer=99, query={'conversation_id': 1}, readonly=True)[1]['data']['messages']), 2)
        result = self.core('image', viewer=99, query={'conversation_id': 1, 'message_id': sent['id']}, readonly=True)
        self.assertEqual(base64.b64decode(result[1]['data']['data']), png.getvalue())
        self.assertEqual(self.core('conversations', viewer=99)[1]['data']['conversations'], [])
        self.assertEqual(self.core('send', viewer=99, query={'conversation_id': 1}, payload={'text': 'not joined', 'client_id': 'not joined'})[0], 403)
        self.join(99); self.send('joined', 99)
        self.assertEqual(len(self.core('conversations', viewer=99)[1]['data']['conversations']), 1)

    def test_restored_viewer_reads_messages_from_suspension_window(self):
        self.activate(); self.join()
        self.authority_denied['a'].add(12)
        sent = self.send('during suspension')
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 403)
        self.authority_denied['a'].remove(12)
        self.assertEqual([m['id'] for m in self.core('history', viewer=12, query={'conversation_id': 1})[1]['data']['messages']], [1, sent['id']])

    def test_authority_failure_pauses_orders_and_preserves_direct_chat(self):
        self.activate()
        cid = self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[1]['data']['id']
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=ServiceError(UNAVAILABLE, 503)):
            self.assertEqual(self.core('history', query={'conversation_id': 1}, readonly=True)[0], 503)
            self.assertEqual(self.core('send', query={'conversation_id': cid}, payload={'text': 'direct works', 'client_id': 'direct works'})[0], 200)
            for op in ('conversations', 'unread', 'sync'):
                result = self.core(op, query={'after': 0} if op == 'sync' else {})
                self.assertEqual(result[0], 200, result); self.assertTrue(result[1]['data']['order_permission_unavailable'])
            self.assertEqual([c['id'] for c in self.core('conversations')[1]['data']['conversations']], [cid])

    def test_queued_delivery_rechecks_receivers_and_timeout_preserves_queue(self):
        self.activate(); self.join(); self.send('queued before revoke')
        self.authority_denied['a'].add(12)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=ServiceError(UNAVAILABLE, 503)), patch('chat_service.core_delivery.httpx.post') as gateway:
            self.engine.core.tick('a'); gateway.assert_not_called()
        self.assertGreater(self.core('status')[1]['data']['pending_notifications'], 0)
        with self.store.connect() as db:
            db.execute("UPDATE core_notifications SET available_at=now() WHERE tenant_id='a'")
            db.execute("UPDATE core_outbox SET available_at=now() WHERE tenant_id='a'")
        calls = []
        with patch('chat_service.core_delivery.httpx.post', side_effect=lambda url, **kw: calls.append(kw['json']) or httpx.Response(204)):
            self.engine.core.tick('a')
        self.assertTrue(calls); payloads = [item for batch in calls for item in batch['notifications']]
        self.assertTrue(all(12 not in p['users'] for p in payloads)); self.assertTrue(any(p.get('incoming') for p in payloads))
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)
        with self.store.connect() as db:
            row = db.execute("SELECT active,metadata FROM members WHERE tenant_id='a' AND local_user_id=12").fetchone()
            self.assertTrue(row['active']); self.assertIsNone(row['metadata']['left_at'])

    def test_completed_group_archives_and_only_customer_reopens_original_group(self):
        self.activate(); self.join(); self.authority_completed['a'] = True
        room = self.conversation(); room.update(can_send=True, can_pin_user_ids=[11]); self.core('room', payload=room)
        dto = self.core('order', query={'conversation_id': 1})[1]['data']
        self.assertTrue(dto['archived']); self.assertFalse(dto['can_send']); self.assertTrue(dto['can_reopen'])
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'closed', 'client_id': 'closed'})[0], 403)
        self.assertEqual(self.core('reopen', viewer=12, query={'conversation_id': 1})[0], 403)
        reopened = self.core('reopen', query={'conversation_id': 1})
        self.assertEqual(reopened[0], 200, reopened); self.assertFalse(reopened[1]['data']['archived'])
        self.send('worker sends after reopen', 12)
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        self.assertTrue(self.authority_completed['a'])

    def test_72_hour_boundary_and_retries_reads_do_not_renew_activity(self):
        self.activate(); first = self.send('original activity')
        with self.store.connect() as db:
            db.execute("UPDATE core_archives SET activity_at=now()-interval '71 hours 59 minutes' WHERE tenant_id='a'")
            before = db.execute("SELECT activity_at FROM core_archives WHERE tenant_id='a'").fetchone()['activity_at']
        self.assertEqual(self.send('original activity')['id'], first['id'])
        self.core('history', query={'conversation_id': 1}); self.core('read', query={'conversation_id': 1}, payload={'seq': first['seq']})
        self.core('preferences', query={'conversation_id': 1}, payload={'remark': 'no renewal'})
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT activity_at FROM core_archives WHERE tenant_id='a'").fetchone()['activity_at'], before)
            db.execute("UPDATE core_archives SET activity_at=now()-interval '72 hours' WHERE tenant_id='a'")
        self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'late', 'client_id': 'late'})[0], 403)

    def test_expiry_rechecks_latest_activity_after_competing_commit(self):
        self.activate(); self.send('old activity')
        with self.store.connect() as db:
            db.execute("UPDATE core_archives SET activity_at=now()-interval '72 hours' WHERE tenant_id='a'")
        entered, done, errors, replies = threading.Event(), threading.Event(), [], []
        def archive():
            try:
                entered.set()
                replies.append(self.core('order', query={'conversation_id': 1}))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        with self.store.connect() as db:
            db.execute("UPDATE core_archives SET activity_at=now() WHERE tenant_id='a'")
            thread = threading.Thread(target=archive); thread.start(); self.assertTrue(entered.wait(2)); self.assertFalse(done.wait(.1))
        thread.join(3); self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(replies[0][0], 200, replies)
        self.assertFalse(replies[0][1]['data']['archived'])
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])

    def test_matrix_commit_lost_confirmation_retries_same_transaction_without_duplicate(self):
        self.activate(); sent = self.send('lost ack'); original = self.engine.matrix.send
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise httpx.ReadTimeout('response lost after event commit')
        with patch.object(self.engine.matrix, 'send', side_effect=lost), patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)):
            self.engine.core.tick('a')
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 1)
        with self.store.connect() as db:
            db.execute("UPDATE core_outbox SET available_at=now() WHERE tenant_id='a'")
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)):
            self.engine.core.tick('a')
        urls = [url for method, url, body in self.matrix_requests if '/send/m.room.message/' in url and body.get('body') == 'lost ack']
        self.assertEqual(len(urls), 2); self.assertEqual(urls[0], urls[1]); self.assertEqual(len(self.events), 2)
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 0)
        self.assertEqual([m['id'] for m in self.core('history', query={'conversation_id': 1})[1]['data']['messages']], [1, sent['id']])

    def test_authority_transport_signs_exact_path_and_rejects_failure_envelope(self):
        self.authority_patch.stop()
        self.engine.tenants['a'].update(source_url='http://127.0.0.1:8081/base', source_host='studio-a.test.invalid')
        def reject(url, **kw):
            self.assertEqual(verify_request({'a': 'a-secret'}, kw['headers'], 'POST', '/base/api/internal-chat/service/order-authorizations/', kw['content']), 'a')
            self.assertEqual(kw['headers']['Host'], 'studio-a.test.invalid'); self.assertFalse(kw['trust_env'])
            return httpx.Response(200, json={'code': 403, 'data': {'groups': [], 'has_order_access': True}})
        with patch('chat_service.core_authority.httpx.post', side_effect=reject):
            with self.assertRaisesRegex(ServiceError, UNAVAILABLE):
                self.engine.core._authority('a', [{'conversation_id': 1, 'user_ids': [11]}], viewer=11)

    def test_1000_and_10000_groups_filter_before_page_with_bounded_batch_calls(self):
        self.activate(); metrics = []
        for count in (1000, 10000):
            with self.store.connect() as db:
                db.execute("DELETE FROM members WHERE tenant_id='a'"); db.execute("DELETE FROM rooms WHERE tenant_id='a'")
                db.execute("INSERT INTO rooms(tenant_id,conversation_id,room_id,kind,metadata) SELECT 'a',i,'pending:'||i,'order',jsonb_build_object('id',i,'kind','order','can_send',true) FROM generate_series(20,%s) i", (count + 19,))
                db.execute("INSERT INTO members(tenant_id,conversation_id,member_id,local_user_id,active,metadata) SELECT 'a',i,11,11,true,'{\"name\":\"Worker\",\"left_at\":null}' FROM generate_series(20,%s) i", (count + 19,))
            calls = []
            def authority(tenant, checks, **kw):
                calls.append(len(checks))
                return {'has_order_access': True, 'groups': [{'conversation_id': c['conversation_id'], 'read_user_ids': [11] if c['conversation_id'] % 3 else [], 'reopen_user_ids': [], 'completed': False, 'default_join_user_ids': [], 'room': {'id': c['conversation_id'], 'kind': 'order', 'can_send': True, 'members': [{'id': 11, 'local_user_id': 11, 'name': 'Worker'}]}} for c in checks]}
            start = time.monotonic()
            with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority), patch.object(self.engine.core, '_dto', wraps=self.engine.core._dto) as dto:
                result = self.core('conversations', query={'limit': 20, 'offset': 15})
                self.assertEqual(result[0], 200, result)
                visible = [i for i in reversed(range(20, count + 20)) if i % 3]
                self.assertEqual([c['id'] for c in result[1]['data']['conversations']], visible[15:35])
                self.assertEqual(dto.call_count, 20); self.assertEqual(len(calls), (count + 499) // 500); self.assertEqual(sum(calls), count)
            metrics.append({'groups': count, 'authority_calls': len(calls), 'seconds': round(time.monotonic() - start, 3)})
        print('AUTHORITY_PAGINATION_METRICS', json.dumps(metrics))

    def test_visibility_token_restores_unopened_group_without_message_or_cursor_change(self):
        self.activate(); self.join()
        before = self.core('sync', viewer=12, query={'after': 0})[1]['data']
        self.authority_denied['a'].add(12)
        denied = self.core('sync', viewer=12, query={'after': before['cursor']})[1]['data']
        self.assertNotEqual(denied['order_visibility_token'], before['order_visibility_token'])
        self.authority_denied['a'].remove(12)
        restored = self.core('sync', viewer=12, query={'after': denied['cursor']})[1]['data']
        self.assertEqual(restored['cursor'], denied['cursor'])
        self.assertEqual(restored['order_visibility_token'], before['order_visibility_token'])
        self.assertEqual(len(self.core('conversations', viewer=12)[1]['data']['conversations']), 1)
        self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 1})[1]['data']['messages'][0]['text'], 'hello')

    def test_new_group_direct_detail_and_default_discovery_preserve_customer_readonly(self):
        self.activate()
        original = self.authority_reply
        room = {'id': 2, 'kind': 'order', 'title': 'new order', 'can_send': True,
            'members': [{'id': 21, 'local_user_id': 11, 'name': 'Publisher'}, {'id': 22, 'local_user_id': 12, 'name': 'Current worker'}]}
        def reply(tenant, checks, *, viewer=None, discover=False, **kwargs):
            response = original(tenant, [c for c in checks if c['conversation_id'] != 2], viewer=viewer, discover=discover)
            check = next((c for c in checks if c['conversation_id'] == 2), None)
            if check is not None or discover and viewer in (11, 12):
                users = check['user_ids'] if check else [11, 12]
                response['groups'].append({'conversation_id': 2, 'read_user_ids': users, 'reopen_user_ids': [],
                    'completed': False, 'default_join_user_ids': [11, 12], 'room': room})
            return response
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=reply):
            unimported = self.core('order', viewer=99, query={'conversation_id': 2}, readonly=True)
            self.assertEqual(unimported[0], 503, unimported)
            core_tests.seed_imported_order(self, room)
            dto = self.core('order', viewer=99, query={'conversation_id': 2}, readonly=True)
            self.assertEqual(dto[0], 200, dto); self.assertFalse(dto[1]['data']['is_member'])
            self.assertEqual(self.core('conversations', viewer=99)[1]['data']['conversations'], [])
            self.assertEqual(self.core('send', viewer=99, query={'conversation_id': 2}, payload={'text': 'not joined', 'client_id': 'not joined'})[0], 403)
            self.assertIn(2, [c['id'] for c in self.core('conversations', viewer=12)[1]['data']['conversations']])
            self.assertEqual(self.core('history', viewer=12, query={'conversation_id': 2})[0], 200)

    def test_request_context_does_not_mix_two_users_and_tenants_during_concurrent_reads(self):
        self.activate('a'); self.activate('b')
        self.join()
        original = self.authority_reply
        barrier = threading.Barrier(2)
        def reply(tenant, checks, **kwargs):
            response = original(tenant, checks, **kwargs)
            for group in response['groups']:
                group['read_user_ids'] = [] if tenant == 'a' else kwargs['viewer'] and [kwargs['viewer']]
            barrier.wait(3)
            return response
        replies, errors = {}, []
        def read(tenant, viewer):
            try:
                replies[tenant] = self.core('history', tenant=tenant, viewer=viewer, query={'conversation_id': 1}, readonly=True)
            except Exception as error:
                errors.append(error)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=reply):
            threads = [threading.Thread(target=read, args=('a', 12)), threading.Thread(target=read, args=('b', 11))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(replies['a'][0], 403, replies)
        self.assertEqual(replies['b'][0], 200, replies)

    def test_group_batches_share_deadline_and_reject_late_authority_allow(self):
        self.activate()
        with patch('chat_service.core_authority.monotonic', side_effect=[10, 10, 16]), self.assertRaisesRegex(ServiceError, UNAVAILABLE):
            self.engine.core._authority('a', [{'conversation_id': 1, 'user_ids': [11]}], viewer=11)

    def test_schema_initialization_does_not_upgrade_protocol_but_live_operation_does(self):
        self.activate()
        with self.store.connect() as db:
            db.execute("UPDATE core_control SET required_protocol=1 WHERE tenant_id='a'")
        self.store.initialize()
        self.assertEqual(self.core('status')[1]['data']['required_protocol'], 1)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('status')[1]['data']['required_protocol'], 4)

    def test_idle_worker_archives_at_boundary_and_upgrades_live_protocol(self):
        self.activate(); self.send('expires without any reader')
        with self.store.connect() as db:
            db.execute("UPDATE core_control SET required_protocol=1 WHERE tenant_id='a'")
            db.execute("UPDATE core_archives SET activity_at=now()-interval '72 hours' WHERE tenant_id='a'")
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)):
            self.engine.core.tick('a')
        with self.store.connect() as db:
            self.assertTrue(db.execute("SELECT archived FROM core_archives WHERE tenant_id='a'").fetchone()['archived'])
        self.assertEqual(self.core('status')[1]['data']['required_protocol'], 4)

    def test_legacy_inactive_participation_migrates_once_and_rights_stay_dynamic(self):
        self.activate(); joined = self.join(); mid = joined['self_member_id']
        self.core('preferences', viewer=12, query={'conversation_id': 1}, payload={'remark': 'keep migrated preference'})
        with self.store.connect() as db:
            db.execute("UPDATE core_control SET required_protocol=1 WHERE tenant_id='a'")
            db.execute("UPDATE members SET active=false,metadata=metadata || '{\"left_at\":\"2026-11-19T00:00:00Z\"}'::jsonb WHERE tenant_id='a' AND local_user_id=12")
        self.authority_denied['a'].add(12)
        denied = self.core('sync', viewer=12, query={'after': 0})
        self.assertEqual(denied[0], 200, denied)
        self.assertIn('conversation.removed', [c['kind'] for c in denied[1]['data']['changes']])
        with self.store.connect() as db:
            member = db.execute("SELECT member_id,active,metadata FROM members WHERE tenant_id='a' AND local_user_id=12").fetchone()
        self.assertEqual(member['member_id'], mid); self.assertTrue(member['active']); self.assertIsNone(member['metadata']['left_at'])
        self.authority_denied['a'].remove(12)
        restored = self.core('conversations', viewer=12)[1]['data']['conversations']
        self.assertEqual(len(restored), 1); self.assertEqual(restored[0]['remark'], 'keep migrated preference')

    def test_repeated_completion_archives_same_group_after_a_real_noncompleted_interval(self):
        self.activate(); self.authority_completed['a'] = True
        self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        reopened = self.core('reopen', query={'conversation_id': 1})
        self.assertEqual(reopened[0], 200, reopened)
        self.core('room', payload=self.conversation())
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        self.authority_completed['a'] = False
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        self.authority_completed['a'] = True
        again = self.core('order', query={'conversation_id': 1})[1]['data']
        self.assertEqual(again['id'], 1); self.assertTrue(again['archived']); self.assertFalse(again['can_send'])
        self.assertEqual(self.core('history', query={'conversation_id': 1})[1]['data']['messages'][0]['text'], 'hello')
        self.assertFalse(self.core('reopen', query={'conversation_id': 1})[1]['data']['archived'])
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])

    def test_concurrent_completion_reads_archive_once_and_reopen_stays_open(self):
        self.activate(); self.authority_completed['a'] = True
        self.core('order', query={'conversation_id': 1}); self.core('reopen', query={'conversation_id': 1})
        self.authority_completed['a'] = False
        self.core('order', query={'conversation_id': 1})
        self.authority_completed['a'] = True
        original, barrier, replies, errors = self.authority_reply, threading.Barrier(2), [], []
        entered = threading.Event()
        def authority(*args, **kwargs):
            response = original(*args, **kwargs)
            entered.set()
            barrier.wait(3)
            return response
        def read():
            try:
                replies.append(self.core('order', query={'conversation_id': 1}))
            except Exception as error:
                errors.append(error)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            threads = [threading.Thread(target=read) for _ in range(2)]
            threads[0].start(); self.assertTrue(entered.wait(3)); threads[1].start()
            for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(errors, []); self.assertTrue(any(result[0] == 200 for result in replies), replies)
        self.assertTrue(all(result[0] in (200, 503) for result in replies), replies)
        for status, result in replies:
            if status == 200:
                self.assertTrue(result['data']['archived']); self.assertFalse(result['data']['can_send'])
        self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
        self.assertFalse(self.core('reopen', query={'conversation_id': 1})[1]['data']['archived'])
        self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])

    def test_each_gateway_batch_rechecks_users_after_previous_batch_revokes(self):
        self.activate(); self.join()
        for index in range(25): self.send('queued ' + str(index))
        received, original, checks = [], self.authority_reply, []
        def authority(tenant, batch, **kwargs):
            checks.append([row['conversation_id'] for row in batch]); return original(tenant, batch, **kwargs)
        def gateway(url, **kwargs):
            received.append(kwargs['json']); self.authority_denied['a'].add(12); return httpx.Response(204)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority), patch('chat_service.core_delivery.httpx.post', side_effect=gateway):
            self.engine.core.tick('a'); self.engine.core.tick('a')
        self.assertEqual(len(received), 2); self.assertEqual(checks, [[1], [1]])
        self.assertEqual(len(received[0]['notifications']), 20)
        incoming = [item for batch in received for item in batch['notifications'] if item.get('incoming')]
        self.assertEqual(len(incoming), 25)
        self.assertTrue(all(12 not in item['users'] for item in received[1]['notifications']))
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)

    def test_control_room_rejects_malformed_members_before_persistence(self):
        self.activate()
        import copy
        good = self.conversation()
        malformed = []
        duplicate = copy.deepcopy(good); duplicate['members'].append(copy.deepcopy(duplicate['members'][0])); malformed.append(duplicate)
        for field, value in [('name', {}), ('unknown_field', 'bad'), ('worker', 'yes')]:
            bad = copy.deepcopy(good); bad['members'][0][field] = value; malformed.append(bad)
        for payload in malformed:
            result = self.core('room', payload=payload)
            self.assertEqual(result[0], 400, result)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[1]['data']['messages'][0]['text'], 'hello')

    def test_new_completion_timestamp_rearchives_without_observed_false_interval(self):
        self.activate(); self.authority_completed['a'] = True
        original = self.authority_reply
        revision = ['2026-11-20T08:00:00+00:00']
        def authority(*args, **kwargs):
            data = original(*args, **kwargs)
            for group in data['groups']: group['completion_revision'] = revision[0]
            return data
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            self.core('reopen', query={'conversation_id': 1})
            self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            revision[0] = '2026-11-20T09:00:00+00:00'
            self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            self.core('reopen', query={'conversation_id': 1})
            self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            revision[0] = '2026-11-20T10:00:00+01:00'
            self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])

    def test_opposite_pinned_display_orders_use_consistent_database_lock_order(self):
        self.activate(); self.join()
        room = self.conversation(); room['id'] = 2; room['can_send'] = True
        core_tests.seed_imported_order(self, room)
        self.assertEqual(self.core('room', payload=room)[0], 200)
        self.assertEqual(self.core('join', viewer=12, query={'conversation_id': 2}, payload={'grant': True, 'viewer': {'id': 12, 'name': 'Worker'}})[0], 200)
        self.core('preferences', query={'conversation_id': 1}, payload={'pinned': True})
        self.core('preferences', viewer=12, query={'conversation_id': 2}, payload={'pinned': True})
        with self.store.connect() as db:
            db.execute('''CREATE FUNCTION pause_rooms() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN PERFORM pg_sleep(0.15); RETURN NEW; END $$''')
            db.execute('CREATE TRIGGER pause_room_update BEFORE UPDATE ON rooms FOR EACH ROW EXECUTE FUNCTION pause_rooms()')
        original, entered, barrier, replies, errors = self.authority_reply, threading.Event(), threading.Barrier(2), {}, []
        def authority(*args, **kwargs):
            response = original(*args, **kwargs); entered.set(); barrier.wait(3); return response
        def listing(viewer):
            try: replies[viewer] = self.core('conversations', viewer=viewer, query={'limit': 20})
            except Exception as error: errors.append(error)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            first = threading.Thread(target=listing, args=(11,)); second = threading.Thread(target=listing, args=(12,))
            first.start(); self.assertTrue(entered.wait(3)); second.start()
            for thread in (first, second): thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(errors, []); self.assertEqual([replies[u][0] for u in (11, 12)], [200, 200], replies)
        self.assertEqual([c['id'] for c in replies[11][1]['data']['conversations']], [1, 2])
        self.assertEqual([c['id'] for c in replies[12][1]['data']['conversations']], [2, 1])

    def test_permission_timeout_between_gateway_batches_preserves_remaining_notifications(self):
        self.activate(); self.join()
        for index in range(25): self.send('deferred ' + str(index))
        original, failed, calls = self.authority_reply, [False], []
        def authority(*args, **kwargs):
            if failed[0]: raise ServiceError(UNAVAILABLE, 503)
            return original(*args, **kwargs)
        def gateway(url, **kwargs):
            calls.append(kwargs['json']); failed[0] = True; self.authority_denied['a'].add(12)
            return httpx.Response(204)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority), patch('chat_service.core_delivery.httpx.post', side_effect=gateway):
            self.engine.core.tick('a'); self.engine.core.tick('a')
        self.assertEqual(len(calls), 1); self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 6)
        with self.store.connect() as db: db.execute("UPDATE core_notifications SET available_at=now() WHERE tenant_id='a'")
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)) as recovered: self.engine.core.tick('a')
        self.assertEqual(recovered.call_count, 1)
        self.assertTrue(all(12 not in item['users'] for item in recovered.call_args.kwargs['json']['notifications']))
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)

    def test_late_old_completion_and_false_responses_cannot_close_reopened_new_revision(self):
        for index, stale_completed in enumerate((True, False)):
            self.activate('a') if not self.engine.core.is_active('a') else None
            self.authority_completed['a'] = True
            original, revision = self.authority_reply, [f'2026-11-20T{8 + index * 2:02}:00:00+00:00']
            entered, release, replies = threading.Event(), threading.Event(), []
            def authority(*args, **kwargs):
                response = original(*args, **kwargs)
                for group in response['groups']: group['completion_revision'] = revision[0]
                return response
            with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
                self.core('order', query={'conversation_id': 1}); self.core('reopen', query={'conversation_id': 1})
                # HTTP server handlers have their own names; pause exactly the next authority response.
                first = [True]
                def delayed(*args, **kwargs):
                    response = original(*args, **kwargs)
                    for group in response['groups']:
                        group['completed'] = stale_completed
                        group['completion_revision'] = revision[0] if stale_completed else None
                    if first[0]: first[0] = False; entered.set(); self.assertTrue(release.wait(3))
                    return response
                with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=delayed):
                    thread = threading.Thread(target=lambda: replies.append(self.core('order', query={'conversation_id': 1})))
                    thread.start(); self.assertTrue(entered.wait(3))
                    with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
                        revision[0] = f'2026-11-20T{9 + index * 2:02}:00:00+00:00'
                        self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
                        self.core('reopen', query={'conversation_id': 1})
                        release.set(); thread.join(4)
                self.assertFalse(thread.is_alive()); self.assertEqual(replies[0][0], 200, replies)
                self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT completion_revision FROM core_archives WHERE tenant_id='a' AND conversation_id=1").fetchone()['completion_revision'], revision[0])

    def test_three_order_groups_use_one_authority_call_and_one_gateway_batch(self):
        self.activate(); self.join()
        for cid in (2, 3):
            room = self.conversation(); room.update(id=cid, can_send=True)
            core_tests.seed_imported_order(self, room)
            self.assertEqual(self.core('room', payload=room)[0], 200)
            self.assertEqual(self.core('send', query={'conversation_id': cid}, payload={'text': str(cid), 'client_id': str(cid)})[0], 200)
        self.send('one')
        checks, original = [], self.authority_reply
        def authority(tenant, batch, **kwargs):
            checks.append([r['conversation_id'] for r in batch]); return original(tenant, batch, **kwargs)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority), patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)) as gateway:
            self.engine.core.tick('a')
        self.assertEqual(checks, [[1, 2, 3]])
        self.assertEqual(gateway.call_count, 1)
        self.assertEqual(len(gateway.call_args.kwargs['json']['notifications']), 4)
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)

    def test_direct_batch_delivers_when_order_authority_is_unavailable(self):
        self.activate(); self.send('hold order')
        cid = self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[1]['data']['id']
        self.assertEqual(self.core('send', query={'conversation_id': cid}, payload={'text': 'deliver direct', 'client_id': 'deliver direct'})[0], 200)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=ServiceError(UNAVAILABLE, 503)), patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)) as gateway:
            self.engine.core.tick('a')
        self.assertEqual(gateway.call_count, 1)
        payloads = gateway.call_args.kwargs['json']['notifications']
        self.assertTrue(any(p.get('incoming', {}).get('conversation_id') == cid for p in payloads))
        self.assertFalse(any(p.get('incoming', {}).get('conversation_id') == 1 for p in payloads))
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 1)

    def test_missing_completion_timestamp_uses_observed_bool_without_losing_known_high_stamp(self):
        self.activate(); self.authority_completed['a'] = True
        original, revision = self.authority_reply, ['2026-11-20T08:00:00+00:00']
        def authority(*args, **kwargs):
            response = original(*args, **kwargs)
            for group in response['groups']: group['completion_revision'] = revision[0]
            return response
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            self.core('order', query={'conversation_id': 1}); self.core('reopen', query={'conversation_id': 1})
            revision[0] = None; self.authority_completed['a'] = False
            self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            self.authority_completed['a'] = True
            self.assertTrue(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])
            self.core('reopen', query={'conversation_id': 1})
            revision[0] = '2026-11-20T08:00:00+00:00'
            self.assertFalse(self.core('order', query={'conversation_id': 1})[1]['data']['archived'])

    def test_protocol_four_upgrade_does_not_rewrite_existing_protocol_two_members(self):
        self.activate()
        with self.store.connect() as db:
            db.execute("UPDATE core_control SET required_protocol=2 WHERE tenant_id='a'")
            db.execute("UPDATE members SET active=false WHERE tenant_id='a' AND local_user_id=11")
        self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})
        self.assertEqual(self.core('status')[1]['data']['required_protocol'], 4)
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT active FROM members WHERE tenant_id='a' AND conversation_id=1 AND local_user_id=11").fetchone()['active'])

    def test_direct_notification_bypasses_large_unavailable_order_backlog(self):
        self.activate()
        for index in range(25): self.send('order backlog ' + str(index))
        cid = self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[1]['data']['id']
        self.core('send', query={'conversation_id': cid}, payload={'text': 'private priority', 'client_id': 'private priority'})
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=ServiceError(UNAVAILABLE, 503)), patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)) as gateway:
            self.engine.core.tick('a')
        self.assertEqual(gateway.call_count, 1)
        self.assertTrue(any(item.get('incoming', {}).get('conversation_id') == cid for item in gateway.call_args.kwargs['json']['notifications']))
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 25)
