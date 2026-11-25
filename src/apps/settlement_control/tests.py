import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase

from .models import Authorization
from .upstream import UnknownResult


class FakeAdapter:
    creates = 0
    fail = False

    def create_app(self, name, secret):
        type(self).creates += 1
        if self.fail:
            raise UnknownResult('unknown')
        return 'synthetic-app'

    def authorize(self, app_id, account, real_name):
        return 'https://ur.alipay.com/synthetic'

    def identity_authorize(self, app_id):
        return 'https://ur.alipay.com/synthetic-identity'

    def refresh(self, app_id, account, real_name, next_action):
        return {'configured': True, 'next_action': 'agreement'}


class ControlServiceTests(TestCase):
    def setUp(self):
        FakeAdapter.creates = 0
        FakeAdapter.fail = False
        self.patcher = patch('apps.settlement_control.views._adapter', return_value=FakeAdapter())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.request_id = str(uuid.uuid4())

    def call(self, path, payload, client='client_a', nonce=None):
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        timestamp = str(int(time.time()))
        nonce = nonce or uuid.uuid4().hex
        message = '\n'.join(('POST', path, timestamp, nonce, hashlib.sha256(raw).hexdigest()))
        key = ('a' if client == 'client_a' else 'b') * 48
        sig = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
        return self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_CLIENT=client, HTTP_X_SETTLEMENT_TIMESTAMP=timestamp, HTTP_X_SETTLEMENT_NONCE=nonce, HTTP_X_SETTLEMENT_SIGNATURE=sig)

    def payload(self):
        return {'request_id': self.request_id, 'account': 'test@example.invalid', 'real_name': '测试用户'}

    def test_create_idempotent_and_secret_encrypted(self):
        one = self.call('/authorizations', self.payload())
        self.assertEqual(one.status_code, 200)
        self.assertIn('authorization_url', one.json())
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 200)
        self.assertEqual(FakeAdapter.creates, 1)
        row = Authorization.objects.get()
        self.assertNotIn(b'test@example.invalid', bytes(row.ciphertext))
        different = {**self.payload(), 'real_name': '另一用户'}
        self.assertEqual(self.call('/authorizations', different).status_code, 409)

    def test_other_tenant_cannot_read_and_refresh(self):
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        self.assertEqual(self.call(path, {}, 'client_b').status_code, 404)
        result = self.call(path, {}).json()
        self.assertEqual(result, {'status': 'authorized', 'configured': True})
        self.assertEqual(self.call(path, {}).json(), result)
        self.assertNotIn('authorization_url', result)

    def test_authorized_refresh_does_not_report_stale_success(self):
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        self.assertEqual(self.call(path, {}).json()['status'], 'authorized')
        with patch.object(FakeAdapter, 'refresh', return_value=False):
            result = self.call(path, {})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['code'], 'OUTCOME_UNKNOWN')
        self.assertNotIn('status', result.json())

    def test_nonce_replay_rejected(self):
        nonce = uuid.uuid4().hex
        self.assertEqual(self.call('/authorizations', self.payload(), nonce=nonce).status_code, 200)
        self.assertEqual(self.call('/authorizations', self.payload(), nonce=nonce).status_code, 401)

    def test_login_failure_can_retry_same_request(self):
        from .upstream import UpstreamError
        with patch('apps.settlement_control.views._adapter', side_effect=UpstreamError('failed')):
            self.assertEqual(self.call('/authorizations', self.payload()).status_code, 503)
        self.assertEqual(Authorization.objects.get().phase, 'new')
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 200)
        self.assertEqual(FakeAdapter.creates, 1)

    def test_ambiguous_create_is_not_retried(self):
        FakeAdapter.fail = True
        first = self.call('/authorizations', self.payload())
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json()['code'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 409)
        self.assertEqual(self.call(f'/authorizations/{self.request_id}/authorize', {}).json()['code'], 'OUTCOME_UNKNOWN')
        self.assertEqual(self.call(f'/authorizations/{self.request_id}/refresh', {}).json()['code'], 'OUTCOME_UNKNOWN')
        self.assertEqual(FakeAdapter.creates, 1)
        self.assertEqual(Authorization.objects.get().phase, 'unknown')


class UpstreamAdapterTests(TransactionTestCase):
    def test_failed_login_cools_down_for_three_seconds_after_completion(self):
        from datetime import timedelta
        from types import SimpleNamespace
        from django.utils import timezone
        from .models import UpstreamSession
        from .upstream import JeepayAdapter, UpstreamError

        started = timezone.now()
        finished = started + timedelta(seconds=10)
        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = SimpleNamespace(headers={})
        with patch('apps.settlement_control.upstream.timezone.now', side_effect=[started, finished]), \
             patch.object(adapter, '_login_once', side_effect=UpstreamError('synthetic failure')):
            with self.assertRaises(UpstreamError):
                adapter._session()
        self.assertEqual(UpstreamSession.objects.get(key='agent').attempted_at, finished)
        with patch('apps.settlement_control.upstream.timezone.now', return_value=finished + timedelta(seconds=2, microseconds=999999)), \
             patch.object(adapter, '_login_once') as login:
            with self.assertRaisesMessage(UpstreamError, '上游登录冷却中'):
                adapter._session()
            login.assert_not_called()
        with patch('apps.settlement_control.upstream.timezone.now', return_value=finished + timedelta(seconds=3)), \
             patch.object(adapter, '_login_once', return_value='synthetic-token') as login:
            adapter._session()
            login.assert_called_once()
        self.assertEqual(adapter.http.headers['iToken'], 'synthetic-token')

    def test_unexpected_login_error_is_hidden_and_still_starts_cooldown(self):
        from types import SimpleNamespace
        from .models import UpstreamSession
        from .upstream import JeepayAdapter, UpstreamError

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = SimpleNamespace(headers={})
        with patch.object(adapter, '_login_once', side_effect=RuntimeError('sensitive details')):
            with self.assertRaisesMessage(UpstreamError, '上游登录暂不可用') as error:
                adapter._session()
        self.assertNotIn('sensitive details', str(error.exception))
        self.assertIsNotNone(UpstreamSession.objects.get(key='agent').attempted_at)
        with patch.object(adapter, '_login_once') as login, self.assertRaisesMessage(UpstreamError, '上游登录结果待核对'):
            adapter._session()
        login.assert_not_called()

    def test_reconcile_login_requires_inflight_and_records_evidence(self):
        import tempfile
        from contextlib import nullcontext
        from pathlib import Path
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from .models import ManualReview, UpstreamSession

        attempt = uuid.uuid4()
        row = UpstreamSession.objects.create(key='agent', login_attempt=attempt)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / 'evidence.txt'
            evidence.write_text('synthetic evidence confirms no active login')
            kwargs = dict(operator='synthetic-operator', evidence_file=str(evidence),
                          confirm_no_inflight=True, confirm_workers_protected=True)
            with patch('apps.settlement_control.management.commands.reconcile_settlement_login.operation_lock', return_value=nullcontext()):
                call_command('reconcile_settlement_login', **kwargs)
                with self.assertRaises(CommandError):
                    call_command('reconcile_settlement_login', **kwargs)
        row.refresh_from_db()
        self.assertIsNone(row.login_attempt)
        self.assertIsNotNone(row.attempted_at)
        self.assertTrue(ManualReview.objects.filter(request_id=attempt, decision='login_reconciled').exists())

    def test_captcha_fetch_error_can_retry_after_three_seconds(self):
        from types import SimpleNamespace
        from .models import UpstreamSession
        from .upstream import JeepayAdapter, UnknownResult, UpstreamError

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = SimpleNamespace(headers={})
        with patch.object(adapter, '_call', side_effect=UnknownResult('synthetic')):
            with self.assertRaisesMessage(UpstreamError, '验证码获取失败'):
                adapter._session()
        self.assertIsNone(UpstreamSession.objects.get(key='agent').login_attempt)

    def test_unknown_login_outcome_stays_blocked(self):
        from types import SimpleNamespace
        from .models import UpstreamSession
        from .upstream import JeepayAdapter, UnknownResult, UpstreamError

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = SimpleNamespace(headers={})
        with patch.object(adapter, '_login_once', side_effect=UnknownResult('synthetic')):
            with self.assertRaises(UnknownResult):
                adapter._session()
        self.assertIsNotNone(UpstreamSession.objects.get(key='agent').login_attempt)
        with patch.object(adapter, '_login_once') as login, self.assertRaises(UpstreamError):
            adapter._session()
        login.assert_not_called()

    @patch('apps.settlement_control.upstream.subprocess.run')
    @patch('apps.settlement_control.upstream.httpx.Client')
    def test_login_and_create_use_bounded_mock_calls(self, http_client, ocr):
        import base64
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from django.test import override_settings
        from .upstream import JeepayAdapter

        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
        ocr.return_value = SimpleNamespace(stdout=b'ab12\n')
        http = http_client.return_value
        http.headers = {}
        responses = [
            {'code': 0, 'data': {'imageBase64Data': base64.b64encode(image.getvalue()).decode(), 'vercodeToken': 'synthetic'}},
            {'code': 0, 'data': {'iToken': 'synthetic-token'}},
            {'code': 0, 'data': {'appId': 'synthetic-app'}},
        ]
        http.request.side_effect = [SimpleNamespace(raise_for_status=lambda: None, json=lambda value=x: value) for x in responses]
        with override_settings(SETTLEMENT_UPSTREAM_URL='https://agent.jeepay.vip', SETTLEMENT_USERNAME='synthetic', SETTLEMENT_PASSWORD='synthetic', SETTLEMENT_MERCHANT='synthetic', SETTLEMENT_TESSERACT='/synthetic/tesseract'):
            adapter = JeepayAdapter()
            self.assertEqual(adapter.create_app('测试-123', 'synthetic-key'), 'synthetic-app')
        request = http.request.call_args_list[-1]
        self.assertEqual(request.args[:2], ('POST', '/api/mchApps'))
        self.assertEqual(request.kwargs['json']['defaultFlag'], 0)
        self.assertEqual(http.request.call_count, 3)

    @patch('apps.settlement_control.upstream.subprocess.run')
    @patch('apps.settlement_control.upstream.httpx.Client')
    def test_captcha_fallback_reuses_image_without_extra_login(self, http_client, ocr):
        import base64
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from django.test import override_settings
        from .upstream import JeepayAdapter

        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
        http = http_client.return_value
        http.headers = {}
        responses = [
            {'code': 0, 'data': {'imageBase64Data': base64.b64encode(image.getvalue()).decode(), 'vercodeToken': 'synthetic'}},
            {'code': 0, 'data': {'iToken': 'synthetic-token'}},
        ]
        http.request.side_effect = [SimpleNamespace(raise_for_status=lambda: None, json=lambda value=x: value) for x in responses]
        ocr.side_effect = [SimpleNamespace(stdout=b'no'), SimpleNamespace(stdout=b'ab12')]
        with override_settings(SETTLEMENT_UPSTREAM_URL='https://agent.jeepay.vip', SETTLEMENT_USERNAME='synthetic', SETTLEMENT_PASSWORD='synthetic', SETTLEMENT_MERCHANT='synthetic', SETTLEMENT_TESSERACT='/synthetic/tesseract'):
            JeepayAdapter()
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(http.request.call_count, 2)
        self.assertEqual(http.request.call_args_list[-1].args[:2], ('POST', '/api/anon/auth/validate'))

    @patch('apps.settlement_control.upstream.subprocess.run')
    @patch('apps.settlement_control.upstream.httpx.Client')
    def test_captcha_ocr_timeout_fails_before_login(self, http_client, ocr):
        import base64
        import subprocess
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from django.test import override_settings
        from .upstream import JeepayAdapter, UpstreamError

        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
        data = {'code': 0, 'data': {'imageBase64Data': base64.b64encode(image.getvalue()).decode(), 'vercodeToken': 'synthetic'}}
        http = http_client.return_value
        http.request.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)
        ocr.side_effect = subprocess.TimeoutExpired('synthetic', 2)
        with override_settings(SETTLEMENT_UPSTREAM_URL='https://agent.jeepay.vip', SETTLEMENT_USERNAME='synthetic', SETTLEMENT_PASSWORD='synthetic', SETTLEMENT_MERCHANT='synthetic', SETTLEMENT_TESSERACT='/synthetic/tesseract'):
            with self.assertRaisesMessage(UpstreamError, '验证码识别不可用'):
                JeepayAdapter()
        self.assertEqual(http.request.call_count, 1)
        self.assertEqual(ocr.call_args.kwargs['timeout'], 2)

    def test_only_observed_login_captcha_error_gets_specific_classification(self):
        from types import SimpleNamespace
        from .upstream import JeepayAdapter, CaptchaRecognitionFailed, UpstreamError

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        for path, message, expected in (
            ('/api/anon/auth/validate', '验证码有误！', CaptchaRecognitionFailed),
            ('/api/anon/auth/validate', '其他错误', UpstreamError),
            ('/api/mchApps', '验证码有误！', UpstreamError),
        ):
            response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'code': 9999, 'msg': message})
            adapter.http = SimpleNamespace(request=lambda *a, **k: response)
            with self.subTest(path=path, message=message):
                with self.assertRaises(expected) as error:
                    adapter._call('POST', path)
                self.assertIs(type(error.exception), expected)

    @patch('apps.settlement_control.upstream.subprocess.run')
    @patch('apps.settlement_control.upstream.httpx.Client')
    def test_captcha_attempts_are_bounded(self, http_client, ocr):
        import base64
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from django.test import override_settings
        from .upstream import JeepayAdapter, UpstreamError

        http = http_client.return_value
        image = BytesIO()
        Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
        data = {'code': 0, 'data': {'imageBase64Data': base64.b64encode(image.getvalue()).decode(), 'vercodeToken': 'synthetic'}}
        http.request.return_value = SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)
        ocr.return_value = SimpleNamespace(stdout=b'not valid')
        with override_settings(SETTLEMENT_UPSTREAM_URL='https://agent.jeepay.vip', SETTLEMENT_USERNAME='synthetic', SETTLEMENT_PASSWORD='synthetic', SETTLEMENT_MERCHANT='synthetic', SETTLEMENT_TESSERACT='/synthetic/tesseract'):
            with self.assertRaises(UpstreamError):
                JeepayAdapter()
        self.assertEqual(http.request.call_count, 1)


class ConfigurationSaveTests(TestCase):
    def test_signed_without_pid_is_read_only_and_does_not_claim_payment_ready(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        calls = []
        def request(method, path, **kwargs):
            calls.append(method)
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            return {'ifParams': json.dumps({'aliAccountNo': 'sample@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'synthetic'}), 'state': 1}
        adapter._call = request
        result = adapter.refresh('synthetic-app', 'sample@example.invalid', '测试用户', 'agreement')
        self.assertEqual(result, {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True})
        self.assertEqual(calls, ['GET', 'GET'])

    def test_absent_pid_never_hides_mismatched_account(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        adapter._call = lambda method, path, **kwargs: ({'status': 'NORMAL', 'agreementNo': 'synthetic'} if '/alipayTransAuthorQuery/' in path else {'ifParams': json.dumps({'aliAccountNo': 'other@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'synthetic'}), 'state': 1})
        self.assertEqual(adapter.refresh('synthetic-app', 'sample@example.invalid', '测试用户', 'agreement')['review_code'], 'IDENTITY_UNVERIFIED')

    def test_missing_pid_does_not_save_or_report_success(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        adapter._call = lambda method, path, **kwargs: {'status': 'NORMAL', 'agreementNo': 'synthetic'} if path.endswith('alitransympay') else {'ifParams': '{}', 'state': 1}
        self.assertEqual(adapter.refresh('synthetic-app', 'sample@example.invalid', '测试用户', 'agreement')['review_code'], 'IDENTITY_INCOMPLETE')

    def test_matching_saved_configuration_is_reported_as_signed(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        calls = []
        def request(method, path, **kwargs):
            calls.append(method)
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            return {'ifParams': json.dumps({'aliPid': '2088000000000000', 'aliAccountNo': 'sample@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'synthetic'}), 'state': 1}
        adapter._call = request
        result = adapter.refresh('synthetic-app', 'sample@example.invalid', '测试用户', 'agreement')
        self.assertTrue(result['configured'])
        self.assertNotIn('POST', calls)


class EndToEndProtocolTests(ControlServiceTests):
    def test_tenant_client_signature_and_control_response(self):
        from types import SimpleNamespace
        from apps.business.alipay_auth_control import AuthorizationControlClient
        def transport(url, content, headers, **kwargs):
            path = url.removeprefix('https://control.example.invalid')
            response = self.client.post(path, data=content, headers=headers, content_type='application/json')
            return SimpleNamespace(status_code=response.status_code, raise_for_status=lambda: None if response.status_code == 200 else (_ for _ in ()).throw(ValueError()), json=response.json)
        client = AuthorizationControlClient('a' * 48, base_url='https://control.example.invalid', client_id='client_a')
        with patch('apps.business.alipay_auth_control.httpx.post', side_effect=transport):
            result = client.create(self.request_id, 'test@example.invalid', '测试用户')
            self.assertEqual(result.status, 'pending')
            self.assertTrue(result.authorization_url)
            result = client.refresh(self.request_id)
            self.assertEqual(result.status, 'authorized')
            self.assertTrue(result.configured)
            self.assertIsNone(result.authorization_url)


class RateLimitTests(ControlServiceTests):
    def test_old_nonce_cleanup_is_limited_to_authenticated_client(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Nonce
        for client in ('client_a', 'client_b'):
            row = Nonce.objects.create(client=client, nonce='z' * 32)
            Nonce.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 200)
        self.assertFalse(Nonce.objects.filter(client='client_a', nonce='z' * 32).exists())
        self.assertTrue(Nonce.objects.filter(client='client_b', nonce='z' * 32).exists())

    def test_over_limit_does_not_create_nonces_and_is_per_client(self):
        from .models import Nonce, RateWindow, SecurityEvent
        RateWindow.objects.create(client='client_a', minute=int(time.time()) // 60, count=120)
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 429)
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 429)
        self.assertEqual(Nonce.objects.filter(client='client_a').count(), 0)
        self.assertEqual(SecurityEvent.objects.filter(client='client_a', action='rate_limit').count(), 1)
        self.assertEqual(self.call('/authorizations', self.payload(), 'client_b').status_code, 200)

    def test_security_log_has_no_account(self):
        from .models import SecurityEvent
        self.call('/authorizations', self.payload())
        event = SecurityEvent.objects.get(action='create')
        self.assertEqual(event.client, 'client_a')
        self.assertEqual(event.result, 200)
        self.assertNotIn('test@example.invalid', str(event.__dict__))


from django.db import connection, close_old_connections
from django.test import TransactionTestCase, Client
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless


@skipUnless(connection.vendor == 'postgresql', '并发锁测试需要 PostgreSQL')
class PostgresReplayTests(TransactionTestCase):
    def test_two_simultaneous_identical_nonces_only_one_passes(self):
        request_id, nonce = str(uuid.uuid4()), uuid.uuid4().hex
        body = json.dumps({'request_id': request_id, 'account': 'x@example.invalid', 'real_name': '测试'}, separators=(',', ':'), ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        message = '\n'.join(('POST', '/authorizations', timestamp, nonce, hashlib.sha256(body).hexdigest()))
        signature = hmac.new(('a' * 48).encode(), message.encode(), hashlib.sha256).hexdigest()
        barrier = Barrier(2)
        def call():
            close_old_connections()
            barrier.wait()
            try:
                return Client().post('/authorizations', data=body, content_type='application/json', HTTP_X_SETTLEMENT_CLIENT='client_a', HTTP_X_SETTLEMENT_TIMESTAMP=timestamp, HTTP_X_SETTLEMENT_NONCE=nonce, HTTP_X_SETTLEMENT_SIGNATURE=signature).status_code
            finally:
                close_old_connections()
        with patch('apps.settlement_control.views._adapter', return_value=FakeAdapter()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: call(), range(2)))
        self.assertEqual(sorted(results), [200, 401])
        self.assertEqual(Authorization.objects.count(), 1)


class IdentityAuthorizationTests(ControlServiceTests):
    def test_existing_incomplete_review_recovers_to_signed_without_payment_ready(self):
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        with patch.object(FakeAdapter, 'refresh', side_effect=[{'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}, {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True}]):
            self.assertEqual(self.call(path, {}).json()['code'], 'IDENTITY_INCOMPLETE')
            self.assertEqual(self.call(path, {}).json(), {'status': 'signed', 'configured': False, 'next_action': 'identity'})
        row = Authorization.objects.get()
        self.assertEqual(row.phase, 'signed')
        self.assertFalse(row.configured)
        self.assertEqual(self.call(f'/authorizations/{self.request_id}/authorize', {}).json(), {'status': 'signed', 'configured': False, 'next_action': 'identity'})

    def test_review_can_refresh_after_missing_pid_is_resolved(self):
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        with patch.object(FakeAdapter, 'refresh', side_effect=[{'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}, {'configured': True, 'next_action': 'agreement'}]):
            self.assertEqual(self.call(path, {}).json()['code'], 'IDENTITY_INCOMPLETE')
            self.assertEqual(self.call(path, {}).json(), {'status': 'authorized', 'configured': True})

    def test_identity_conflict_cannot_auto_clear_on_pid_absent(self):
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        with patch.object(FakeAdapter, 'refresh', side_effect=[{'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_UNVERIFIED'}, {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True}]):
            self.assertEqual(self.call(path, {}).json()['code'], 'IDENTITY_UNVERIFIED')
            self.assertEqual(self.call(path, {}).json()['code'], 'IDENTITY_UNVERIFIED')
        self.assertEqual(Authorization.objects.get().phase, 'identity_review')

    def test_unverified_identity_is_persisted_and_never_offers_a_new_scan(self):
        self.call('/authorizations', self.payload())
        refresh = f'/authorizations/{self.request_id}/refresh'
        authorize = f'/authorizations/{self.request_id}/authorize'
        outcome = {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_UNVERIFIED'}
        with patch.object(FakeAdapter, 'refresh', return_value=outcome), patch.object(FakeAdapter, 'authorize') as create_link:
            self.assertEqual(self.call(refresh, {}).json()['code'], 'IDENTITY_UNVERIFIED')
            self.assertEqual(self.call('/authorizations', self.payload()).json()['code'], 'IDENTITY_UNVERIFIED')
            self.assertEqual(self.call(authorize, {}).json()['code'], 'IDENTITY_UNVERIFIED')
            self.assertEqual(self.call(refresh, {}).json()['code'], 'IDENTITY_UNVERIFIED')
            create_link.assert_not_called()
        row = Authorization.objects.get()
        self.assertEqual(row.phase, 'identity_review')
        self.assertFalse(row.configured)

    def test_missing_identity_stops_more_scans_and_persists_review(self):
        create = self.call('/authorizations', self.payload()).json()
        self.assertEqual(create['next_action'], 'agreement')
        refresh = f'/authorizations/{self.request_id}/refresh'
        authorize = f'/authorizations/{self.request_id}/authorize'
        result = {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}
        with patch.object(FakeAdapter, 'refresh', return_value=result), patch.object(FakeAdapter, 'identity_authorize') as identity:
            pending = self.call(refresh, {})
            self.assertEqual(pending.status_code, 409)
            self.assertEqual(pending.json()['code'], 'IDENTITY_INCOMPLETE')
            self.assertEqual(self.call(authorize, {}).json()['code'], 'IDENTITY_INCOMPLETE')
            self.assertEqual(self.call(refresh, {}).json()['code'], 'IDENTITY_INCOMPLETE')
            identity.assert_not_called()
        self.assertEqual(Authorization.objects.get().phase, 'identity_review')

    def test_identity_query_failure_never_reports_success(self):
        from .upstream import UpstreamError
        self.call('/authorizations', self.payload())
        path = f'/authorizations/{self.request_id}/refresh'
        with patch.object(FakeAdapter, 'refresh', side_effect=UpstreamError('failure')):
            result = self.call(path, {})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['code'], 'OUTCOME_UNKNOWN')
        self.assertNotEqual(Authorization.objects.get().status, 'authorized')


class IdentityConfigurationTests(TestCase):
    def test_pid_for_different_account_is_never_accepted(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        calls = []
        def request(method, path, **kwargs):
            calls.append(method)
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            return {'ifParams': json.dumps({'aliPid': '2088000000000001', 'aliAccountNo': 'other@example.invalid'}), 'state': 1}
        adapter._call = request
        result = adapter.refresh('synthetic-app', 'first@example.invalid', '测试用户', 'agreement')
        self.assertEqual(result['review_code'], 'IDENTITY_UNVERIFIED')
        self.assertFalse(result['configured'])
        self.assertNotIn('POST', calls)

    def test_openid_without_pid_needs_manual_review(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        calls = []
        def request(method, path, **kwargs):
            calls.append(method)
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            return {'ifParams': json.dumps({'aliOpenId': 'synthetic-openid'}), 'state': 1}
        adapter._call = request
        result = adapter.refresh('synthetic-app', 'sample@example.invalid', '测试用户', 'agreement')
        self.assertEqual(result['review_code'], 'IDENTITY_INCOMPLETE')
        self.assertFalse(result['configured'])
        self.assertNotIn('POST', calls)


class ReconciliationReadOnlyTests(TestCase):
    def test_admin_list_is_scoped_and_never_returns_ciphertext(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import SecurityEvent
        for client in ('client_a', 'client_b'):
            Authorization.objects.create(client=client, request_id=uuid.uuid4(), payload_hash='x' * 64, ciphertext=b'private-synthetic', app_name='测试系统-001', phase='unknown', unknown_action='create_app')
        output = StringIO()
        call_command('list_settlement_unknown', client='client_a', stdout=output)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])['unknown_action'], 'create_app')
        self.assertNotIn('private-synthetic', output.getvalue())
        self.assertEqual(Authorization.objects.filter(phase='unknown').count(), 2)
        self.assertTrue(SecurityEvent.objects.filter(client='client_a', action='admin_list_unknown').exists())


class SigningBoundaryTests(TestCase):
    def test_pid_with_missing_fields_merges_and_rechecks_only_signing_state(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        state = {'aliPid': '2088000000000000', 'other': 'preserved'}
        calls = []
        def call(method, path, **kwargs):
            calls.append((method, path))
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            if method == 'POST':
                state.update(json.loads(kwargs['json']['ifParams']))
                return {}
            return {'state': 1, 'ifParams': json.dumps(state)}
        adapter._call = call
        result = adapter.refresh('synthetic', 'payer@example.invalid', '测试用户', 'agreement')
        self.assertTrue(result['configured'])
        self.assertEqual(state['other'], 'preserved')
        self.assertEqual(state['aliAccountNo'], 'payer@example.invalid')
        self.assertEqual(calls[-1][1], '/api/payConfig/interfaceSavedConfigs')

    def test_configuration_must_match_on_readback(self):
        from .upstream import JeepayAdapter
        adapter = object.__new__(JeepayAdapter)
        def call(method, path, **kwargs):
            if '/alipayTransAuthorQuery/' in path:
                return {'status': 'NORMAL', 'agreementNo': 'synthetic'}
            if method == 'POST':
                return {}
            return {'state': 1, 'ifParams': json.dumps({'aliPid': '2088000000000000'})}
        adapter._call = call
        self.assertFalse(adapter.refresh('synthetic', 'payer@example.invalid', '测试用户', 'agreement')['configured'])


class ManualRecoveryTests(ControlServiceTests):
    def test_confirm_absent_requires_exact_owner_and_does_not_call_upstream(self):
        from io import StringIO
        from tempfile import NamedTemporaryFile
        from django.core.management import call_command, CommandError
        from .models import ManualReview, SecurityEvent
        FakeAdapter.fail = True
        self.assertEqual(self.call('/authorizations', self.payload()).json()['code'], 'OUTCOME_UNKNOWN')
        with NamedTemporaryFile() as evidence:
            evidence.write(b'synthetic vendor response evidence only')
            evidence.flush()
            args = {'client': 'client_a', 'request_id': uuid.UUID(self.request_id), 'operator': 'operator-test', 'evidence_file': evidence.name, 'resolution': 'app_absent', 'confirm_upstream_conclusion': True, 'confirm_no_inflight': True, 'confirm_workers_protected': True}
            with self.assertRaises(CommandError):
                call_command('reconcile_settlement_attempt', **{**args, 'confirm_no_inflight': False}, stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command('reconcile_settlement_attempt', **{**args, 'client': 'client_b'}, stdout=StringIO())
            out = StringIO()
            call_command('reconcile_settlement_attempt', **args, stdout=out)
        row = Authorization.objects.get()
        self.assertEqual(row.phase, 'new')
        self.assertEqual(FakeAdapter.creates, 1)
        review = ManualReview.objects.get()
        self.assertNotIn(b'synthetic vendor', bytes(review.evidence_ciphertext))
        self.assertNotIn('synthetic vendor', out.getvalue())
        self.assertTrue(SecurityEvent.objects.filter(client='client_a', action='admin_reconcile_attempt').exists())
        FakeAdapter.fail = False
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 200)
        self.assertEqual(FakeAdapter.creates, 2)

    def test_unknown_authorization_cannot_be_reset_as_missing_application(self):
        from io import StringIO
        from tempfile import NamedTemporaryFile
        from django.core.management import call_command, CommandError
        self.call('/authorizations', self.payload())
        row = Authorization.objects.get()
        row.phase, row.unknown_action = 'unknown', 'agreement_authorize'
        row.save(update_fields=['phase', 'unknown_action'])
        with NamedTemporaryFile() as evidence:
            evidence.write(b'synthetic vendor response evidence only')
            evidence.flush()
            with self.assertRaises(CommandError):
                call_command('reconcile_settlement_attempt', client='client_a', request_id=uuid.UUID(self.request_id), operator='operator-test', evidence_file=evidence.name, resolution='app_absent', confirm_upstream_conclusion=True, confirm_no_inflight=True, confirm_workers_protected=True, stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.phase, 'unknown')


class InflightRecoveryTests(ControlServiceTests):
    def _reconcile(self, row, resolution, **overrides):
        from io import StringIO
        from tempfile import NamedTemporaryFile
        from django.core.management import call_command
        with NamedTemporaryFile() as evidence:
            evidence.write(b'synthetic vendor evidence of final outcome')
            evidence.flush()
            args = {'client': row.client, 'request_id': row.request_id, 'operator': 'test-operator', 'evidence_file': evidence.name, 'resolution': resolution, 'confirm_upstream_conclusion': True, 'confirm_no_inflight': True, 'confirm_workers_protected': True, **overrides}
            return call_command('reconcile_settlement_attempt', **args, stdout=StringIO())

    def test_crashed_creation_only_unlocked_if_app_absent(self):
        from .upstream import UnknownResult
        with patch.object(FakeAdapter, 'create_app', side_effect=UnknownResult('unknown')):
            self.call('/authorizations', self.payload())
        row = Authorization.objects.get()
        row.phase, row.unknown_action = 'creating', ''
        row.save(update_fields=['phase', 'unknown_action'])
        self._reconcile(row, 'app_absent')
        row.refresh_from_db()
        self.assertEqual(row.phase, 'new')
        self.assertEqual(row.app_id, '')

    def test_authorization_uncertainty_unlocked_only_with_existing_app(self):
        from django.core.management import CommandError
        self.call('/authorizations', self.payload())
        row = Authorization.objects.get()
        for phase, action in [('authorizing', ''), ('unknown', 'agreement_authorize')]:
            row.phase, row.unknown_action = phase, action
            row.save(update_fields=['phase', 'unknown_action'])
            with self.assertRaises(CommandError):
                self._reconcile(row, 'app_absent')
            self._reconcile(row, 'authorization_unavailable')
            row.refresh_from_db()
            self.assertEqual(row.phase, 'app_created')
            self.assertEqual(row.app_id, 'synthetic-app')

    def test_active_worker_blocks_admin_and_admin_lock_blocks_new_writes(self):
        from django.core.management import CommandError
        from .locking import operation_lock
        FakeAdapter.fail = True
        self.call('/authorizations', self.payload())
        row = Authorization.objects.get()
        with operation_lock():
            with self.assertRaises(CommandError):
                self._reconcile(row, 'app_absent')
        row.refresh_from_db()
        self.assertEqual(row.phase, 'unknown')
        self._reconcile(row, 'app_absent')
        FakeAdapter.fail = False
        with operation_lock(exclusive=True):
            self.assertEqual(self.call('/authorizations', self.payload()).status_code, 503)
        self.assertEqual(FakeAdapter.creates, 1)

    def test_admin_lock_blocks_refresh_that_could_save_upstream_configuration(self):
        from .locking import operation_lock
        self.call('/authorizations', self.payload())
        with operation_lock(exclusive=True), patch.object(FakeAdapter, 'refresh') as upstream:
            self.assertEqual(self.call(f'/authorizations/{self.request_id}/refresh', {}).status_code, 503)
        upstream.assert_not_called()

    def test_missing_lock_file_fails_closed(self):
        from django.test import override_settings
        with override_settings(SETTLEMENT_OPERATION_LOCK_FILE=''):
            self.assertEqual(self.call('/authorizations', self.payload()).status_code, 503)
        self.assertEqual(FakeAdapter.creates, 0)


class HealthTests(TestCase):
    def test_ready_and_database_failure_do_not_leak_details(self):
        from django.db import OperationalError
        result = self.client.get('/healthz')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['ok'])
        self.assertEqual(result.json()['service'], 'settlement-control')
        self.assertRegex(result.json()['instance'], r'^[0-9a-f]{32}$')
        with patch('apps.settlement_control.health.connection.cursor', side_effect=OperationalError('private database detail')):
            result = self.client.get('/healthz')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {'ok': False})
        self.assertNotIn('private', result.content.decode())

    def test_wrong_host_is_rejected(self):
        self.assertEqual(self.client.get('/healthz', HTTP_HOST='unexpected.example.invalid').status_code, 400)

    def test_instance_stable_and_challenge_is_fresh_and_exact(self):
        first = self.client.get('/healthz', {'challenge': 'a' * 32})
        second = self.client.get('/healthz', {'challenge': 'b' * 32})
        self.assertEqual(first.json()['instance'], second.json()['instance'])
        self.assertEqual(first.json()['challenge'], 'a' * 32)
        self.assertEqual(second.json()['challenge'], 'b' * 32)
        self.assertEqual(first['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/healthz?challenge=bad').status_code, 400)
        self.assertEqual(self.client.get('/healthz?challenge=' + 'a' * 32 + '&challenge=' + 'b' * 32).status_code, 400)

    def test_new_process_generates_distinct_instance(self):
        from multiprocessing import get_context
        from .health import _instance
        parent = _instance()
        ctx = get_context('fork')
        receiver, sender = ctx.Pipe(duplex=False)
        def worker():
            sender.send(_instance())
            sender.close()
        process = ctx.Process(target=worker)
        process.start()
        try:
            self.assertTrue(receiver.poll(3))
            self.assertNotEqual(receiver.recv(), parent)
        finally:
            process.join(5)
            receiver.close()
            sender.close()
        self.assertEqual(process.exitcode, 0)


class ProcessLockTests(InflightRecoveryTests):
    def test_live_other_process_prevents_manual_unlock(self):
        from multiprocessing import get_context
        from django.core.management import CommandError
        from .locking import operation_lock
        FakeAdapter.fail = True
        self.call('/authorizations', self.payload())
        row = Authorization.objects.get()
        ctx = get_context('fork')
        ready, release = ctx.Event(), ctx.Event()
        def worker():
            with operation_lock():
                ready.set()
                release.wait(5)
        process = ctx.Process(target=worker)
        process.start()
        try:
            self.assertTrue(ready.wait(3))
            with self.assertRaises(CommandError):
                self._reconcile(row, 'app_absent')
            row.refresh_from_db()
            self.assertEqual(row.phase, 'unknown')
        finally:
            release.set()
            process.join(5)
        self.assertEqual(process.exitcode, 0)
        self._reconcile(row, 'app_absent')
        row.refresh_from_db()
        self.assertEqual(row.phase, 'new')


class DuplicateClientKeyTests(ControlServiceTests):
    def test_duplicate_config_rejects_both_identities_even_with_valid_signature(self):
        from django.test import override_settings
        from .models import Nonce
        self.assertEqual(self.call('/authorizations', self.payload()).status_code, 200)
        path = f'/authorizations/{self.request_id}/refresh'
        key = 'a' * 48
        def signed(client):
            raw = b'{}'
            timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
            message = '\n'.join(('POST', path, timestamp, nonce, hashlib.sha256(raw).hexdigest()))
            signature = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
            return self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_CLIENT=client, HTTP_X_SETTLEMENT_TIMESTAMP=timestamp, HTTP_X_SETTLEMENT_NONCE=nonce, HTTP_X_SETTLEMENT_SIGNATURE=signature)
        count = Nonce.objects.count()
        with override_settings(SETTLEMENT_CLIENT_KEYS={'client_a': key, 'client_b': key}):
            for client in ('client_a', 'client_b'):
                result = signed(client)
                self.assertEqual(result.status_code, 401)
                self.assertNotIn(key, result.content.decode())
        self.assertEqual(Nonce.objects.count(), count)
        self.assertEqual(self.call(path, {}, 'client_b').status_code, 404)


class BusinessProbeTests(TestCase):
    def test_probe_identifies_same_instance_without_creating_records(self):
        from .models import Nonce, SecurityEvent
        challenge = 'a' * 32
        health = self.client.get('/healthz', {'challenge': challenge})
        result = self.client.get('/authorizations', {'challenge': challenge})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'ok': True, 'service': 'settlement-control', 'instance': health.json()['instance'], 'challenge': challenge, 'route': 'authorizations'})
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(Authorization.objects.count(), 0)
        self.assertEqual(Nonce.objects.count(), 0)
        self.assertEqual(SecurityEvent.objects.count(), 0)

    def test_probe_rejects_missing_invalid_duplicate_and_extra_parameters(self):
        valid = 'a' * 32
        for query in ('', '?challenge=bad', '?challenge=' + valid + '&challenge=' + valid, '?challenge=' + valid + '&other=x'):
            self.assertEqual(self.client.get('/authorizations' + query).status_code, 400)
        self.assertEqual(self.client.put('/authorizations', data=b'{}', content_type='application/json').status_code, 405)
        self.assertEqual(self.client.post('/authorizations', data=b'{}', content_type='application/json').status_code, 401)
        self.assertEqual(self.client.get('/authorizations?challenge=' + valid, HTTP_HOST='invalid.example.invalid').status_code, 400)

    def test_probe_database_failure_is_minimal_and_fail_closed(self):
        from django.db import OperationalError
        with patch('apps.settlement_control.health.connection.cursor', side_effect=OperationalError('sensitive test value')):
            response = self.client.get('/authorizations', {'challenge': 'a' * 32})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'ok': False})
        self.assertNotIn('sensitive', response.content.decode())


class PostBusinessProbeTests(ControlServiceTests):
    def test_each_post_route_probes_same_instance_without_writes(self):
        from .models import Nonce, SecurityEvent
        challenge = 'a' * 32
        raw = ('{"challenge":"' + challenge + '"}').encode()
        health = self.client.get('/healthz', {'challenge': challenge}).json()
        for path, route in [('/authorizations', 'create'), (f'/authorizations/{self.request_id}/authorize', 'authorize'), (f'/authorizations/{self.request_id}/refresh', 'refresh')]:
            result = self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {'ok': True, 'service': 'settlement-control', 'instance': health['instance'], 'challenge': challenge, 'route': route, 'method': 'POST'})
            self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(Authorization.objects.count(), 0)
        self.assertEqual(Nonce.objects.count(), 0)
        self.assertEqual(SecurityEvent.objects.count(), 0)
        self.assertEqual(FakeAdapter.creates, 0)

    def test_probe_with_business_data_or_credentials_does_not_bypass_auth(self):
        path = '/authorizations'
        for raw, extra in [(b'{"challenge":"' + b'a' * 32 + b'","extra":"x"}', {}), (b'{"challenge":"bad"}', {}), (b'{"challenge":"' + b'a' * 32 + b'"}', {'HTTP_AUTHORIZATION': 'Bearer synthetic'}), (b'{"challenge":"' + b'a' * 32 + b'"}', {'HTTP_X_SETTLEMENT_CLIENT': 'client_a'}), (b'{"challenge":"' + b'a' * 32 + b'"}', {'HTTP_COOKIE': 'session=synthetic'}), (b'{"challenge":"' + b'a' * 32 + b'"}', {'HTTP_X_SENSITIVE_DATA': 'synthetic'})]:
            result = self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1', **extra)
            self.assertEqual(result.status_code, 401)
        result = self.client.post(path + '?x=1', data=b'{"challenge":"' + b'a' * 32 + b'"}', content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1')
        self.assertEqual(result.status_code, 401)

    def test_post_probe_database_failure_does_not_leak_details(self):
        from django.db import OperationalError
        raw = b'{"challenge":"' + b'a' * 32 + b'"}'
        with patch('apps.settlement_control.health.connection.cursor', side_effect=OperationalError('sensitive test value')):
            response = self.client.post('/authorizations', data=raw, content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'ok': False})
        self.assertNotIn('sensitive', response.content.decode())

    def test_signed_business_post_with_probe_header_executes_normal_auth_and_audit(self):
        from .models import SecurityEvent
        path = '/authorizations'
        raw = json.dumps(self.payload(), ensure_ascii=False, separators=(',', ':')).encode()
        timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
        message = '\n'.join(('POST', path, timestamp, nonce, hashlib.sha256(raw).hexdigest()))
        signature = hmac.new(('a' * 48).encode(), message.encode(), hashlib.sha256).hexdigest()
        result = self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1', HTTP_X_SETTLEMENT_CLIENT='client_a', HTTP_X_SETTLEMENT_TIMESTAMP=timestamp, HTTP_X_SETTLEMENT_NONCE=nonce, HTTP_X_SETTLEMENT_SIGNATURE=signature)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Authorization.objects.count(), 1)
        self.assertTrue(SecurityEvent.objects.filter(client='client_a', action='create', result=200).exists())

    def test_signed_authorize_and_refresh_with_probe_header_use_normal_handlers(self):
        from .models import SecurityEvent
        self.call('/authorizations', self.payload())
        raw = b'{"challenge":"' + b'a' * 32 + b'"}'
        for action in ('authorize', 'refresh'):
            path = f'/authorizations/{self.request_id}/{action}'
            timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
            message = '\n'.join(('POST', path, timestamp, nonce, hashlib.sha256(raw).hexdigest()))
            signature = hmac.new(('a' * 48).encode(), message.encode(), hashlib.sha256).hexdigest()
            result = self.client.post(path, data=raw, content_type='application/json', HTTP_X_SETTLEMENT_PROBE='1', HTTP_X_SETTLEMENT_CLIENT='client_a', HTTP_X_SETTLEMENT_TIMESTAMP=timestamp, HTTP_X_SETTLEMENT_NONCE=nonce, HTTP_X_SETTLEMENT_SIGNATURE=signature)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('instance', result.json())
            self.assertTrue(SecurityEvent.objects.filter(client='client_a', action=action, result=200).exists())


class LoginConcurrencyTests(TransactionTestCase):
    def test_login_attempts_are_serialized_across_connections(self):
        from threading import Event, Thread
        from types import SimpleNamespace
        import time
        from django.db import close_old_connections, connection
        from .models import UpstreamSession
        from .upstream import JeepayAdapter, UpstreamError

        if connection.vendor != 'postgresql':
            self.skipTest('行锁并发测试需要 PostgreSQL')
        UpstreamSession.objects.create(key='agent')
        first_started, second_started, second_done, release = Event(), Event(), Event(), Event()
        attempts, errors, backend_pids = [], [], []

        def login(adapter):
            attempts.append('started')
            first_started.set()
            if not release.wait(5):
                raise RuntimeError('测试等待超时')
            raise UpstreamError('synthetic failure')

        def worker(started=None, done=None):
            close_old_connections()
            adapter = JeepayAdapter.__new__(JeepayAdapter)
            adapter.http = SimpleNamespace(headers={})
            try:
                if started:
                    connection.ensure_connection()
                    backend_pids.append(connection.connection.info.backend_pid)
                    started.set()
                adapter._session()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                close_old_connections()
                if done:
                    done.set()

        with patch.object(JeepayAdapter, '_login_once', login):
            first = Thread(target=worker)
            second = Thread(target=worker, args=(second_started, second_done))
            try:
                first.start()
                self.assertTrue(first_started.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                self.assertTrue(second_done.wait(5), '并发登录必须快速返回')
                self.assertEqual(attempts, ['started'])
            finally:
                release.set()
                first.join(8)
                if second.ident:
                    second.join(8)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(attempts, ['started'])
        self.assertCountEqual(errors, ['synthetic failure', '上游登录结果待核对'])


class IdentityAndRevokeFlowTests(TestCase):
    call = ControlServiceTests.call
    payload = ControlServiceTests.payload

    def setUp(self):
        self.patcher = patch('apps.settlement_control.views._adapter', return_value=FakeAdapter())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.request_id = str(uuid.uuid4())
        self.call('/authorizations', self.payload())
        Authorization.objects.filter(request_id=self.request_id).update(phase='signed', status='signed', next_action='identity')

    def test_identity_scan_refresh_and_revoke_keep_same_record(self):
        path = f'/authorizations/{self.request_id}'
        with patch.object(FakeAdapter, 'identity', return_value='https://ur.alipay.com/identity', create=True), \
             patch.object(FakeAdapter, 'identity_refresh', side_effect=[False, True], create=True), \
             patch.object(FakeAdapter, 'revoke', return_value=True, create=True) as cancel:
            self.assertEqual(self.call(path + '/identity', {}).json()['next_action'], 'identity')
            self.assertEqual(self.call(path + '/identity-refresh', {}).json()['status'], 'signed')
            self.assertEqual(self.call(path + '/identity-refresh', {}).json()['status'], 'authorized')
            self.assertEqual(self.call(path + '/revoke', {'request_id': self.request_id, 'confirm': 1}).status_code, 400)
            self.assertEqual(self.call(path + '/revoke', {'request_id': self.request_id, 'confirm': True}, 'client_b').status_code, 404)
            payload = {'request_id': self.request_id, 'confirm': True}
            self.assertEqual(self.call(path + '/revoke', payload).json()['status'], 'revoked')
            self.assertEqual(self.call(path + '/revoke', payload).json()['status'], 'revoked')
            cancel.assert_called_once()
            self.assertEqual(self.call(path + '/authorize', {}).json()['status'], 'pending')
        self.assertEqual(Authorization.objects.count(), 1)

    def test_uncertain_revoke_is_locked_and_not_retried(self):
        path = f'/authorizations/{self.request_id}/revoke'
        with patch.object(FakeAdapter, 'revoke', side_effect=UnknownResult('timeout'), create=True) as cancel:
            payload = {'request_id': self.request_id, 'confirm': True}
            self.assertEqual(self.call(path, payload).json()['code'], 'OUTCOME_UNKNOWN')
            self.assertEqual(self.call(path, payload).json()['code'], 'OUTCOME_UNKNOWN')
            cancel.assert_called_once()
        row = Authorization.objects.get(request_id=self.request_id)
        self.assertEqual((row.phase, row.unknown_action), ('unknown', 'revoke'))

    def test_other_client_cannot_access_identity(self):
        path = f'/authorizations/{self.request_id}'
        self.assertEqual(self.call(path + '/identity', {}, 'client_b').status_code, 404)
        self.assertEqual(self.call(path + '/identity-refresh', {}, 'client_b').status_code, 404)


class IdentityAdapterTests(TransactionTestCase):
    def test_identity_conflict_and_unrecognized_revoke_status_stop(self):
        from .upstream import JeepayAdapter, UpstreamError
        adapter = JeepayAdapter.__new__(JeepayAdapter)
        config = {'state': 1, 'ifParams': json.dumps({'aliAccountNo': 'test@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'agreement', 'aliPid': '2088000000000001'})}
        with patch.object(adapter, '_call', side_effect=[{'status': 'NORMAL', 'agreementNo': 'agreement'}, {'aliOpenId': 'open-id', 'aliPid': '2088000000000002'}, config]) as call:
            with self.assertRaises(UpstreamError):
                adapter.identity_refresh('app', 'test@example.invalid', '测试用户')
            self.assertEqual(call.call_count, 3)
        with patch.object(adapter, '_call', return_value={'status': 'FAILED'}) as call:
            with self.assertRaises(UnknownResult):
                adapter.revoke('app')
            call.assert_called_once()

    def test_identity_save_and_readback_and_revoke_cleanup(self):
        from .upstream import JeepayAdapter
        adapter = JeepayAdapter.__new__(JeepayAdapter)
        params = {'aliAccountNo': 'test@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'agreement', 'other': 'preserve'}
        config = {'state': 1, 'ifParams': json.dumps(params)}
        saved = {'state': 1, 'ifParams': json.dumps({**params, 'aliPid': '2088000000000000'})}
        with patch.object(adapter, '_call', side_effect=[{'status': 'NORMAL', 'agreementNo': 'agreement'}, {'aliPid': '2088000000000000'}, config, {}, saved]) as call:
            self.assertTrue(adapter.identity_refresh('app', 'test@example.invalid', '测试用户'))
            self.assertEqual(json.loads(call.call_args_list[3].kwargs['json']['ifParams'])['other'], 'preserve')
        cleared = {'state': 1, 'ifParams': json.dumps({**params, 'agreementNo': '', 'aliPid': '', 'aliOpenId': ''})}
        with patch.object(adapter, '_call', side_effect=[{}, saved, {}, cleared]) as call:
            self.assertTrue(adapter.revoke('app'))
            outgoing = json.loads(call.call_args_list[2].kwargs['json']['ifParams'])
            self.assertEqual(outgoing['aliPid'], '')
            self.assertEqual(outgoing['other'], 'preserve')

    def test_identity_and_revoke_do_not_succeed_when_readback_differs(self):
        from .upstream import JeepayAdapter
        adapter = JeepayAdapter.__new__(JeepayAdapter)
        config = {'state': 1, 'ifParams': json.dumps({'aliAccountNo': 'test@example.invalid', 'aliAccountName': '测试用户', 'agreementNo': 'agreement'})}
        with patch.object(adapter, '_call', side_effect=[{'status': 'NORMAL', 'agreementNo': 'agreement'}, {'aliPid': '2088000000000000'}, config, {}, config]):
            with self.assertRaises(UnknownResult):
                adapter.identity_refresh('app', 'test@example.invalid', '测试用户')
        with patch.object(adapter, '_call', side_effect=[{}, config, {}, config]):
            with self.assertRaises(UnknownResult):
                adapter.revoke('app')
