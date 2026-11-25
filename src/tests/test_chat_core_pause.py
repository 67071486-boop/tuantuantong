import unittest
from unittest.mock import patch
import httpx
from tests import test_chat_core_engine as fixture


class CorePauseTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CoreHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CoreHTTPTests.tearDownClass.__func__)
    setUp = fixture.CoreHTTPTests.setUp
    tearDown = fixture.CoreHTTPTests.tearDown
    matrix_reply = fixture.CoreHTTPTests.matrix_reply
    request = fixture.CoreHTTPTests.request
    conversation = fixture.CoreHTTPTests.conversation
    message = fixture.CoreHTTPTests.message
    seed = fixture.CoreHTTPTests.seed
    core = fixture.CoreHTTPTests.core
    activate = fixture.CoreHTTPTests.activate
    authority_reply = fixture.CoreHTTPTests.authority_reply

    def test_pause_before_import_activation_remains_paused_until_explicit_resume(self):
        paused = self.core('pause', viewer=None)
        self.assertEqual(paused[0], 200, paused); self.assertFalse(paused[1]['data']['active'])
        self.assertTrue(paused[1]['data']['writes_paused'])
        self.seed()
        self.engine.tenants['a']['realtime'] = {'url': 'http://127.0.0.1:9998', 'secret': 'gateway-secret', 'tenant': 'studio_a'}
        activated = self.core('activate', payload={'minimum_cursor': 10})
        self.assertEqual(activated[0], 200, activated); self.assertTrue(activated[1]['data']['writes_paused'])
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)
        self.assertEqual(self.core('start', payload={'viewer': {'id': 11, 'name': 'A'}, 'peer': {'id': 12, 'name': 'B'}})[0], 503)
        self.assertFalse(self.core('resume', viewer=None)[1]['data']['writes_paused'])

    def test_pause_blocks_new_and_retried_user_writes_but_worker_drains(self):
        self.activate()
        message = {'text': 'before pause', 'client_id': 'before pause'}
        accepted = self.core('send', query={'conversation_id': 1}, payload=message)
        self.assertEqual(accepted[0], 200, accepted)
        self.assertEqual(self.core('pause', viewer=None)[0], 200)
        for op, query, payload in [('send', {'conversation_id': 1}, message), ('read', {'conversation_id': 1}, {'seq': 1}),
                                  ('preferences', {'conversation_id': 1}, {'remark': 'paused'}), ('reopen', {'conversation_id': 1}, {})]:
            result = self.core(op, query=query, payload=payload)
            self.assertEqual((result[0], result[1]['msg']), (503, '内部沟通暂时不可用'))
        self.assertEqual(self.core('history', query={'conversation_id': 1})[0], 200)
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)):
            self.engine.core.tick('a')
        self.assertEqual(self.core('status')[1]['data']['pending_messages'], 0)
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)
        self.core('resume', viewer=None)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload=message)[1]['data']['id'], accepted[1]['data']['id'])

    def test_pause_waits_for_existing_writer_boundary_and_later_retries_are_blocked(self):
        import threading
        self.activate()
        entered, release, replies = threading.Event(), threading.Event(), []
        original = self.engine.core._send
        def sending(*args, **kwargs):
            entered.set(); self.assertTrue(release.wait(3)); return original(*args, **kwargs)
        body = {'text': 'in flight', 'client_id': 'in flight'}
        with patch.object(self.engine.core, '_send', side_effect=sending):
            thread = threading.Thread(target=lambda: replies.append(self.core('send', query={'conversation_id': 1}, payload=body)))
            thread.start(); self.assertTrue(entered.wait(3))
            busy = self.core('pause', viewer=None)
            self.assertEqual(busy[0], 503, busy)
            release.set(); thread.join(4); self.assertFalse(thread.is_alive())
        self.assertEqual(replies[0][0], 200, replies)
        self.assertTrue(self.core('pause', viewer=None)[1]['data']['writes_paused'])
        denied = self.core('send', query={'conversation_id': 1}, payload=body)
        self.assertEqual((denied[0], denied[1]['msg']), (503, '内部沟通暂时不可用'))

    def test_paused_order_send_rejects_before_contacting_business_authority(self):
        self.activate(); self.core('pause', viewer=None)
        with patch('chat_service.core_authority.CoreAuthority._authority_request') as authority:
            result = self.core('order_send', query={'order_no': '20261120000000000002'},
                payload={'viewer': {'id': 11, 'name': 'A'}, 'message': {'text': 'paused', 'client_id': 'paused'}})
        self.assertEqual((result[0], result[1]['msg']), (503, '内部沟通暂时不可用'))
        authority.assert_not_called()

    def test_resume_rechecks_pending_metadata_after_successful_external_precheck(self):
        self.activate(); self.core('pause', viewer=None)
        self.assertEqual(self.core('status')[1]['data']['pending_notifications'], 0)
        import threading
        entered, release, errors = threading.Event(), threading.Event(), []
        def metadata():
            try:
                with self.store.tenant('a') as db:
                    self.engine.core._event(db, 'a', 1, 'conversation.members_changed')
                    entered.set(); self.assertTrue(release.wait(3))
            except Exception as error: errors.append(error)
        thread = threading.Thread(target=metadata); thread.start(); self.assertTrue(entered.wait(3))
        self.assertEqual(self.core('resume', viewer=None)[0], 503)
        release.set(); thread.join(4); self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        resume = self.core('resume', viewer=None)
        self.assertEqual(resume[0], 409, resume)
        self.assertTrue(self.core('status')[1]['data']['writes_paused'])
        with patch('chat_service.core_delivery.httpx.post', return_value=httpx.Response(204)): self.engine.core.tick('a')
        self.assertFalse(self.core('resume', viewer=None)[1]['data']['writes_paused'])

    def test_source_success_codes_require_exact_integer_zero(self):
        from chat_service.config import ServiceError
        from chat_service.worker import Source
        self.engine.tenants['a']['source_url'] = 'http://127.0.0.1:8081'
        for code in (False, True, 0.0, '0'):
            with self.subTest(code=repr(code)):
                response = httpx.Response(200,json={'code':code,'data':{'groups':[],'has_order_access':True}})
                with patch('chat_service.core_authority.httpx.post', return_value=response), self.assertRaises(ServiceError):
                    self.engine.core._source_request('a','order-authorizations',{'checks':[]})
                client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'code':code,'data':{'rows':[]}})))
                with client, self.assertRaises(ServiceError):
                    Source('a', {'secret':'s','source_url':'http://127.0.0.1:8081'}, client).page('snapshot',{})

    def test_resume_requires_active_paused_and_no_pending_messages(self):
        self.assertEqual(self.core('resume', viewer=None)[0], 409)
        self.core('pause', viewer=None)
        self.assertEqual(self.core('resume', viewer=None)[0], 409)
        self.activate()
        message = self.core('send', query={'conversation_id': 1}, payload={'text': 'pending', 'client_id': 'pending'})
        self.assertEqual(message[0], 503, message)
        self.assertFalse(self.core('resume', viewer=None)[1]['data']['writes_paused'])
        self.assertEqual(self.core('resume', viewer=None)[0], 409)
        self.assertEqual(self.core('send', query={'conversation_id': 1}, payload={'text': 'pending', 'client_id': 'pending'})[0], 200)
        self.core('pause', viewer=None)
        self.assertEqual(self.core('resume', viewer=None)[0], 409)
        self.assertTrue(self.core('status')[1]['data']['writes_paused'])
