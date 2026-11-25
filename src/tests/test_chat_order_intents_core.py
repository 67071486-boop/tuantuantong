import copy
import unittest
from unittest.mock import patch
import httpx

from tests import test_chat_order_lazy_core as fixture


class OrderIntentCoreTests(unittest.TestCase):
    setUpClass = classmethod(fixture.LazyOrderCoreTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.LazyOrderCoreTests.tearDownClass.__func__)
    tearDown = fixture.LazyOrderCoreTests.tearDown
    matrix_reply = fixture.LazyOrderCoreTests.matrix_reply
    request = fixture.LazyOrderCoreTests.request
    conversation = fixture.LazyOrderCoreTests.conversation
    message = fixture.LazyOrderCoreTests.message
    seed = fixture.LazyOrderCoreTests.seed
    core = fixture.LazyOrderCoreTests.core
    activate = fixture.LazyOrderCoreTests.activate
    authority_reply = fixture.LazyOrderCoreTests.authority_reply
    send_order = fixture.LazyOrderCoreTests.send_order

    def setUp(self):
        fixture.LazyOrderCoreTests.setUp(self)
        self.intents, self.acks, self.lose_ack, self.change_on_ack = [], [], False, False
        def source(tenant, operation, body, **kwargs):
            self.assertEqual(operation, 'order-intents')
            if body['operation'] == 'pull': return {'intents': copy.deepcopy(self.intents)}
            self.acks.append(body['intents'])
            if self.change_on_ack:
                self.change_on_ack = False; self.intents[0]['revision'] += 1
                self.fact['default_join_user_ids'] = [11, 14]
                self.fact['participants'][1] = {'local_user_id': 14, 'name': 'Replacement', 'order_role': 'worker'}
                self.fact['read_user_ids'] = [11, 14, 99]
                self.intents[0]['order'] = copy.deepcopy(self.fact)
            if self.lose_ack:
                self.lose_ack = False; raise httpx.ReadTimeout('intent ack response lost')
            acknowledged = {(i['id'], i['revision']) for i in body['intents']}
            removed = [i for i in self.intents if (i['id'], i['revision']) in acknowledged]
            self.intents = [i for i in self.intents if i not in removed]
            return {'acked': len(removed)}
        self.source = patch('chat_service.core_authority.CoreAuthority._source_request', side_effect=source)
        self.source.start(); self.addCleanup(self.source.stop)

    def enqueue(self, ensure=True):
        self.intents = [{'id': 1, 'revision': 1, 'ensure_group': ensure, 'order': copy.deepcopy(self.fact)}]

    def test_new_order_intent_recovers_latest_assignment_and_ack_retry_is_idempotent(self):
        self.enqueue(); self.change_on_ack = True
        self.assertEqual(self.engine.core._reconcile_order_intents('a'), 0)
        with self.store.connect() as db:
            cid = db.execute('SELECT conversation_id FROM core_order_rooms').fetchone()['conversation_id']
            old = db.execute('SELECT member_id FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=12', ('a', cid)).fetchone()['member_id']
        self.lose_ack = True
        with self.assertRaises(httpx.ReadTimeout): self.engine.core._reconcile_order_intents('a')
        self.assertEqual(self.engine.core._reconcile_order_intents('a'), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT member_id FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=12', ('a', cid)).fetchone()['member_id'], old)
        dto = self.core('order', viewer=14, query={'conversation_id': cid})
        self.assertEqual(dto[0], 200, dto); self.assertTrue(dto[1]['data']['is_member'])
        self.assertFalse(next(m for m in dto[1]['data']['members'] if m['id'] == old)['has_permission'])

    def test_old_order_without_group_is_not_created_by_nonensure_intent(self):
        self.fact['completed'] = True; self.enqueue(ensure=False)
        self.assertEqual(self.engine.core._reconcile_order_intents('a'), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM rooms WHERE tenant_id='a'").fetchone()['n'], 1)

    def test_delete_intent_archives_without_a_reader_but_grants_no_history_access(self):
        sent = self.send_order(); self.assertEqual(sent[0], 200, sent)
        cid = sent[1]['data']['conversation']['id']
        self.fact['deleted'] = True; self.fact['read_user_ids'] = []; self.fact['reopen_user_ids'] = []
        self.enqueue(ensure=False)
        self.assertEqual(self.engine.core._reconcile_order_intents('a'), 1)
        with self.store.connect() as db:
            self.assertTrue(db.execute('SELECT archived FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['archived'])
            self.assertEqual(db.execute('SELECT count(*) AS n FROM messages WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['n'], 1)
        self.assertEqual(self.core('history', viewer=99, query={'conversation_id': cid})[0], 403)
        self.assertEqual(self.core('reopen', viewer=99, query={'conversation_id': cid})[0], 403)

    def test_completed_new_order_is_archived_by_idle_worker_while_paused(self):
        self.fact['completed'] = True; self.enqueue()
        self.core('pause', viewer=None)
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)):
            self.engine.core.tick('a')
        with self.store.connect() as db:
            cid = db.execute('SELECT conversation_id FROM core_order_rooms').fetchone()['conversation_id']
            self.assertTrue(db.execute('SELECT archived FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['archived'])
        self.assertTrue(self.core('status')[1]['data']['writes_paused'])
        self.assertEqual(self.intents, [])

    def test_stale_delete_timestamp_cannot_rearchive_restored_reopened_history(self):
        sent = self.send_order(); cid = sent[1]['data']['conversation']['id']
        self.fact.update(deleted=True, deletion_revision='2026-11-20T08:00:00+00:00', read_user_ids=[], reopen_user_ids=[])
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        stale = copy.deepcopy(self.fact)
        self.fact.update(deleted=False, deletion_revision=None, read_user_ids=[11, 12, 99], reopen_user_ids=[11])
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        self.assertEqual(self.core('reopen', query={'conversation_id': cid})[0], 200)
        self.intents = [{'id': 1, 'revision': 2, 'ensure_group': False, 'order': stale}]
        self.engine.core._reconcile_order_intents('a')
        self.assertFalse(self.core('order', query={'conversation_id': cid})[1]['data']['archived'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT deletion_revision FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['deletion_revision'], stale['deletion_revision'])
        self.fact.update(deleted=True, deletion_revision='2026-11-20T09:00:00+00:00', read_user_ids=[], reopen_user_ids=[])
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        with self.store.connect() as db:
            self.assertTrue(db.execute('SELECT archived FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['archived'])

    def test_sticky_historical_delete_and_completion_archive_latest_live_order_once(self):
        sent = self.send_order(); cid = sent[1]['data']['conversation']['id']
        self.fact.update(completed=False, deleted=False, completion_revision=None, deletion_revision=None,
            last_deletion_revision='2026-11-20T08:00:00+00:00', last_completion_revision=None)
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        with self.store.connect() as db:
            self.assertTrue(db.execute('SELECT archived FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()['archived'])
        self.assertEqual(self.core('reopen', query={'conversation_id': cid})[0], 200)
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        self.assertFalse(self.core('order', query={'conversation_id': cid})[1]['data']['archived'])
        self.fact['last_completion_revision'] = '2026-11-20T09:00:00+00:00'
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        self.assertTrue(self.core('order', query={'conversation_id': cid})[1]['data']['archived'])
        self.assertEqual(self.core('reopen', query={'conversation_id': cid})[0], 200)
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        self.assertFalse(self.core('order', query={'conversation_id': cid})[1]['data']['archived'])
        self.fact['last_completion_revision'] = '2026-11-20T08:00:00+00:00'
        self.fact['last_deletion_revision'] = '2026-11-20T07:00:00+00:00'
        self.enqueue(False); self.engine.core._reconcile_order_intents('a')
        self.assertFalse(self.core('order', query={'conversation_id': cid})[1]['data']['archived'])
        with self.store.connect() as db:
            row = db.execute('SELECT completion_revision,deletion_revision FROM core_archives WHERE tenant_id=%s AND conversation_id=%s', ('a', cid)).fetchone()
            self.assertEqual(row, {'completion_revision':'2026-11-20T09:00:00+00:00','deletion_revision':'2026-11-20T08:00:00+00:00'})

    def test_intent_cannot_materialize_unimported_source_group(self):
        from chat_service.config import ServiceError
        self.fact['conversation_id'] = 222; self.fact['has_legacy_messages'] = False
        self.enqueue(ensure=False)
        with self.assertRaisesRegex(ServiceError, 'history is not ready'):
            self.engine.core._reconcile_order_intents('a')
        self.assertEqual(self.acks, [])
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM rooms WHERE tenant_id='a' AND conversation_id=222").fetchone())
            self.assertEqual(db.execute('SELECT count(*) AS n FROM core_order_rooms').fetchone()['n'], 0)
