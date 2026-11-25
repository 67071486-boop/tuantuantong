import copy
import unittest
from unittest.mock import patch

from tests import test_chat_core_engine as fixture


class LazyOrderCoreTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CoreHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CoreHTTPTests.tearDownClass.__func__)
    tearDown = fixture.CoreHTTPTests.tearDown
    matrix_reply = fixture.CoreHTTPTests.matrix_reply
    request = fixture.CoreHTTPTests.request
    conversation = fixture.CoreHTTPTests.conversation
    message = fixture.CoreHTTPTests.message
    seed = fixture.CoreHTTPTests.seed
    core = fixture.CoreHTTPTests.core
    activate = fixture.CoreHTTPTests.activate
    authority_reply = fixture.CoreHTTPTests.authority_reply

    def setUp(self):
        fixture.CoreHTTPTests.setUp(self); self.activate()
        self.number = '20261120000000000002'
        self.fact = {'order_no': self.number, 'conversation_id': None, 'has_legacy_messages': False,
            'read_user_ids': [11, 12, 99], 'reopen_user_ids': [11], 'completed': False, 'deleted': False,
            'default_join_user_ids': [11, 12], 'participants': [{'local_user_id': 11, 'name': 'Publisher', 'order_role': 'publisher'},
                {'local_user_id': 12, 'name': 'Worker', 'order_role': 'worker'}]}
        self.checks = []
        def authority(tenant, checks, **kwargs):
            self.checks.extend(copy.deepcopy(checks))
            if checks and 'conversation_id' not in checks[0]:
                return {'groups': [], 'has_order_access': True, 'orders': [copy.deepcopy(self.fact)]}
            result = self.authority_reply(tenant, checks, **kwargs)
            for group in result['groups']:
                if group['conversation_id'] >= 1000000000:
                    users = set(next(c['user_ids'] for c in checks if c['conversation_id'] == group['conversation_id']))
                    users.update(m['local_user_id'] for m in group['room']['members'] if m.get('local_user_id') is not None)
                    users.update(self.fact['default_join_user_ids'])
                    group.update({key: self.fact[key] for key in ('completed', 'deleted', 'default_join_user_ids', 'participants')})
                    group.update({key: self.fact.get(key) for key in ('completion_revision', 'deletion_revision')})
                    group.update({key: [uid for uid in self.fact[key] if uid in users] for key in ('read_user_ids', 'reopen_user_ids')})
            return result
        self.fact_patch = patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority)
        self.fact_patch.start(); self.addCleanup(self.fact_patch.stop)

    def send_order(self, **message):
        return self.core('order_send', viewer=99, query={'order_no': self.number}, payload={
            'viewer': {'id': 99, 'name': 'Service'}, 'message': {'text': 'first message', 'client_id': 'first', **message}})

    def test_preview_and_discovery_do_not_create_missing_order_room(self):
        preview = self.core('order_preview', viewer=99, query={'order_no': self.number}, readonly=True)
        self.assertEqual(preview[0], 200, preview); self.assertIsNone(preview[1]['data']['id'])
        self.assertEqual(self.core('conversations', viewer=99)[1]['data']['conversations'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM rooms WHERE tenant_id='a'").fetchone()['n'], 1)

    def test_first_send_and_retry_commit_one_group_and_join_sender_once(self):
        first = self.send_order(); self.assertEqual(first[0], 200, first)
        retry = self.send_order(); self.assertEqual(retry[0], 200, retry)
        self.assertEqual(first[1]['data']['messages'][0]['id'], retry[1]['data']['messages'][0]['id'])
        cid = first[1]['data']['conversation']['id']
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 1)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM members WHERE tenant_id='a' AND conversation_id=%s", (cid,)).fetchone()['n'], 3)
        dto = self.core('order_preview', viewer=99, query={'order_no': self.number})[1]['data']
        self.assertEqual(dto['id'], cid); self.assertTrue(dto['is_member'])
        history = self.core('history', viewer=99, query={'conversation_id': cid})
        self.assertEqual(history[0], 200, history)
        self.assertTrue(any(c.get('order_no') == self.number and c.get('conversation_id') == cid for c in self.checks))

    def test_invalid_message_or_denied_viewer_leaves_no_empty_group(self):
        bad = self.send_order(reply_to_id=999999)
        self.assertEqual(bad[0], 404, bad)
        self.fact['read_user_ids'] = []
        denied = self.send_order(); self.assertEqual(denied[0], 403, denied)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM rooms WHERE tenant_id='a'").fetchone()['n'], 1)

    def test_existing_legacy_group_keeps_its_id_and_history(self):
        self.fact['conversation_id'] = 1; self.fact['has_legacy_messages'] = True
        self.assertEqual(self.core('join', viewer=99, query={'conversation_id': 1}, payload={'grant':True,'viewer':{'id':99,'name':'Service'}})[0], 200)
        sent = self.send_order(); self.assertEqual(sent[0], 200, sent)
        self.assertEqual(sent[1]['data']['conversation']['id'], 1)
        history = self.core('history', viewer=99, query={'conversation_id': 1})
        self.assertEqual([m['text'] for m in history[1]['data']['messages']], ['hello', 'first message'])

    def test_unknown_legacy_group_with_history_refuses_empty_replacement(self):
        self.fact['conversation_id'] = 222; self.fact['has_legacy_messages'] = True
        self.assertEqual(self.send_order()[0], 503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)

    def test_concurrent_first_send_retries_keep_one_room_and_one_message(self):
        import threading
        original = self.engine.core._authority_request
        entered, barrier, replies, errors = threading.Event(), threading.Barrier(2), [], []
        def authority(*args, **kwargs):
            result = original(*args, **kwargs); entered.set(); barrier.wait(3); return result
        def send():
            try: replies.append(self.send_order())
            except Exception as error: errors.append(error)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            first = threading.Thread(target=send); second = threading.Thread(target=send)
            first.start(); self.assertTrue(entered.wait(3)); second.start()
            for thread in (first, second): thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(errors, []); self.assertTrue(any(r[0] == 200 for r in replies), replies)
        self.assertTrue(all(r[0] in (200, 503) for r in replies), replies)
        retry = self.send_order(); self.assertEqual(retry[0], 200, retry)
        cid = retry[1]['data']['conversation']['id']
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 1)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM messages WHERE tenant_id='a' AND conversation_id=%s", (cid,)).fetchone()['n'], 1)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM members WHERE tenant_id='a' AND conversation_id=%s", (cid,)).fetchone()['n'], 3)

    def test_saved_attachment_url_requires_fresh_order_permission_after_revocation(self):
        import base64
        import io
        from PIL import Image
        raw = io.BytesIO(); Image.new('RGB', (3, 2)).save(raw, 'PNG')
        sent = self.send_order(image={'mime': 'image/png', 'data': base64.b64encode(raw.getvalue()).decode()})
        self.assertEqual(sent[0], 200, sent)
        message = sent[1]['data']['messages'][0]; query = {'conversation_id': message['conversation_id'], 'message_id': message['id']}
        image = self.core('image', viewer=99, query=query)
        self.assertEqual(image[0], 200, image); self.assertEqual(base64.b64decode(image[1]['data']['data']), raw.getvalue())
        self.fact['read_user_ids'].remove(99)
        self.assertEqual(self.core('image', viewer=99, query=query)[0], 403)
        self.fact['read_user_ids'].append(99)
        self.assertEqual(self.core('image', viewer=99, query=query)[0], 200)

    def test_existing_empty_legacy_group_still_requires_import_of_members_and_preferences(self):
        self.fact['conversation_id'] = 222; self.fact['has_legacy_messages'] = False
        self.fact['room'] = {'id': 222, 'kind': 'order', 'title': 'legacy empty group', 'members': [
            {'id': 77, 'local_user_id': 11, 'name': 'Legacy member'}],
            'preferences': [{'user_id': 11, 'pinned': True, 'remark': 'legacy preference'}]}
        preview = self.core('order_preview', viewer=99, query={'order_no': self.number})
        self.assertEqual(preview[0], 503, preview)
        sent = self.send_order(); self.assertEqual(sent[0], 503, sent)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)
            self.assertFalse(db.execute("SELECT 1 FROM rooms WHERE tenant_id='a' AND conversation_id=222").fetchone())

    def test_generic_endpoints_cannot_materialize_unimported_source_group(self):
        self.fact['conversation_id'] = 222; self.fact['has_legacy_messages'] = True
        legacy = {'id': 222, 'kind': 'order', 'can_send': True, 'order_no': self.number,
            'members': [{'id': 77, 'local_user_id': 11, 'name': 'Legacy'}]}
        self.fact['room'] = legacy
        original = self.engine.core._authority_request
        def authority(tenant, checks, **kwargs):
            if any(c.get('conversation_id') == 222 for c in checks):
                return {'groups': [{**copy.deepcopy(self.fact), 'room': copy.deepcopy(legacy)}], 'has_order_access': True}
            return original(tenant, checks, **kwargs)
        self.assertEqual(self.send_order()[0], 503)
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            for operation, query in [('history', {'conversation_id': 222}), ('search', {'conversation_id': 222, 'q': 'legacy'}),
                                     ('notification', {'conversation_id': 222}), ('order', {'conversation_id': 222})]:
                result = self.core(operation, viewer=99, query=query, readonly=operation in ('history', 'search', 'order'))
                self.assertEqual(result[0], 503, result)
            ordinary = self.core('send', viewer=99, query={'conversation_id': 222}, payload={'text': 'bypass', 'client_id': 'bypass'})
            self.assertEqual(ordinary[0], 503, ordinary)
        self.assertEqual(self.send_order()[0], 503)
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM rooms WHERE tenant_id='a' AND conversation_id=222").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM members WHERE tenant_id='a' AND conversation_id=222").fetchone())
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)

    def test_existing_order_requires_explicit_join_for_a_new_customer(self):
        self.fact['conversation_id'] = 1
        result = self.send_order()
        self.assertEqual((result[0], result[1]['msg']), (409, '请先加入会话'))
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM members WHERE tenant_id='a' AND conversation_id=1 AND local_user_id=99").fetchone())
        joined = self.core('join', viewer=99, query={'conversation_id': 1}, payload={'grant': True, 'viewer': {'id': 99, 'name': 'Service'}})
        self.assertEqual(joined[0], 200, joined)
        sent = self.send_order(); self.assertEqual(sent[0], 200, sent)
        self.assertEqual(self.send_order()[1]['data']['messages'][0]['id'], sent[1]['data']['messages'][0]['id'])

    def test_collection_discovery_cannot_materialize_unimported_source_group(self):
        original = self.engine.core._authority_request
        legacy = {**copy.deepcopy(self.fact), 'conversation_id': 222, 'read_user_ids': [11, 12],
            'room': {'id':222,'kind':'order','members':[{'id':77,'local_user_id':11,'name':'Legacy'}]}}
        def authority(*args, **kwargs):
            response = original(*args, **kwargs)
            if kwargs.get('discover'): response['groups'].append(copy.deepcopy(legacy))
            return response
        with patch('chat_service.core_authority.CoreAuthority._authority_request', side_effect=authority):
            self.assertEqual(self.core('conversations')[0], 503)
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM rooms WHERE tenant_id='a' AND conversation_id=222").fetchone())
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)

    def test_preview_preserves_existing_binding_send_and_pin_grants(self):
        self.fact['conversation_id'] = 1
        room = self.conversation(); room.update(can_send=False, can_pin_user_ids=[11])
        self.fact['room'] = room
        preview = self.core('order_preview', query={'order_no': self.number})
        self.assertEqual(preview[0], 200, preview); self.assertFalse(preview[1]['data']['can_send'])
        self.assertFalse(preview[1]['data']['can_pin_messages'])
        self.assertEqual(self.core('pin', query={'conversation_id':1}, payload={'message_id':1})[0], 403)
        with self.store.connect() as db:
            metadata = db.execute("SELECT metadata FROM rooms WHERE tenant_id='a' AND conversation_id=1").fetchone()['metadata']
        self.assertFalse(metadata['can_send']); self.assertEqual(metadata['can_pin_user_ids'], [11])
        self.fact['room']['can_send'] = True
        preview = self.core('order_preview', query={'order_no': self.number})
        self.assertTrue(preview[1]['data']['can_pin_messages'])
        self.fact['room']['can_send'] = False; self.fact['completed'] = True
        self.assertEqual(self.core('reopen', query={'conversation_id': 1})[0], 200)
        blocked = self.core('order_send', query={'order_no':self.number}, payload={'viewer':{'id':11,'name':'Publisher'},'message':{'text':'blocked','client_id':'blocked'}})
        self.assertEqual(blocked[0], 403, blocked)
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)

    def test_outer_order_permission_fields_are_validated(self):
        for key, bad in [('can_send','yes'),('can_pin_user_ids',[{}])]:
            self.fact[key] = bad
            self.assertEqual(self.send_order()[0], 503)
            self.fact.pop(key)
