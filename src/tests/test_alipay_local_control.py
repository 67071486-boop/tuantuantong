import os
import json

import tempfile
import uuid
from io import StringIO
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.business.alipay_auth_control import ControlOutcomeUnknown, ControlUnavailable, get_control_client
from apps.platform.provisioning import provision_tenant
from apps.settlement_control.models import Authorization
from apps.settlement_control.upstream import UnknownResult
from tests.p5_auth_helpers import make_logged_in_client


class LocalControlTests(TenantTestCase):
    def setUp(self):
        self.lock = tempfile.NamedTemporaryFile()
        self.addCleanup(self.lock.close)
        self.credential = 'c' * 48
        self.state_key = Fernet.generate_key().decode()
        self.data_key = Fernet.generate_key().decode()
        self.values = dict(
            ALIPAY_AUTH_LOCAL_ENABLED=True, ALIPAY_AUTH_CONTROL_CREDENTIAL=self.credential,
            ALIPAY_AUTH_DATA_KEY=self.data_key, SETTLEMENT_FERNET_KEY=self.state_key,
            SETTLEMENT_CLIENT_KEYS={'local': self.credential}, SETTLEMENT_CLIENT_NAMES={'local': '测试工作室'},
            SETTLEMENT_USERNAME='synthetic', SETTLEMENT_PASSWORD='synthetic',
            SETTLEMENT_MERCHANT='synthetic', SETTLEMENT_TESSERACT='/usr/bin/true',
            SETTLEMENT_OPERATION_LOCK_FILE=self.lock.name,
        )
        self.override = override_settings(**self.values)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.adapter = Mock()
        self.adapter.create_app.return_value = 'synthetic-app'
        self.adapter.authorize.return_value = 'https://ur.alipay.com/synthetic'
        self.adapter.refresh.return_value = {'configured': True, 'next_action': 'agreement'}
        self.patcher = patch('apps.settlement_control.views._adapter', return_value=self.adapter)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        lock_patcher = patch('apps.settlement_control.locking._trusted_local_lock', return_value=True)
        lock_patcher.start()
        self.addCleanup(lock_patcher.stop)
        self.request_id = str(uuid.uuid4())

    def test_local_state_lifecycle_idempotent_and_encrypted(self):
        control = get_control_client()
        first = control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.assertEqual(first.authorization_url, 'https://ur.alipay.com/synthetic')
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.adapter.create_app.assert_called_once()
        row = Authorization.objects.get(request_id=self.request_id)
        self.assertNotIn(b'payer@example.invalid', bytes(row.ciphertext))
        self.assertNotIn(b'synthetic', bytes(row.ciphertext))
        self.assertEqual(control.refresh(self.request_id).status, 'authorized')
        self.assertTrue(Authorization.objects.get(pk=row.pk).configured)

    def test_manual_recovery_covers_crash_before_and_after_control_write(self):
        from apps.business.models import AlipaySettlementAuthorization
        client = make_logged_in_client(is_superuser=True)
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        item = client.post('/api/business/alipay-authorizations/', json.dumps(payload), content_type='application/json').json()['data']
        client.post(f"/api/business/alipay-authorizations/{item['id']}/refresh/", '{}', content_type='application/json')
        business = AlipaySettlementAuthorization.objects.get(pk=item['id'])
        control = Authorization.objects.get(request_id=self.request_id)
        with tempfile.NamedTemporaryFile(mode='w') as evidence:
            evidence.write('synthetic confirmation of no inflight requests and upstream evidence')
            evidence.flush()
            def recover(resolution):
                call_command('reconcile_settlement_attempt', client='local', request_id=uuid.UUID(self.request_id), operator='tester', evidence_file=evidence.name, resolution=resolution, confirm_upstream_conclusion=True, confirm_no_inflight=True, confirm_workers_protected=True)
            business.requires_platform_review, business.last_error = True, '操作处理中，请稍后重试'
            business.save(update_fields=['requires_platform_review', 'last_error'])
            output = StringIO()
            call_command('list_settlement_unknown', client='local', stdout=output)
            self.assertIn(self.request_id, output.getvalue())
            recover('write_not_sent')
            business.refresh_from_db()
            self.assertFalse(business.requires_platform_review)
            business.requires_platform_review, business.last_error = True, '操作处理中，请稍后重试'
            business.save(update_fields=['requires_platform_review', 'last_error'])
            control.phase = control.status = 'revoked'
            control.configured = False
            control.save(update_fields=['phase', 'status', 'configured'])
            recover('revocation_reconciled')
            business.refresh_from_db()
            self.assertEqual(business.status, 'revoked')
            self.assertFalse(business.requires_platform_review)

    def test_tenant_page_calls_local_state_machine(self):
        client = make_logged_in_client(is_superuser=True)
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        response = client.post('/api/business/alipay-authorizations/', json.dumps(payload), content_type='application/json')
        row = response.json()['data']
        self.assertEqual(row['status'], 'pending')
        self.assertTrue(row['qr_png_base64'].startswith('iVBOR'))
        response = client.post(f"/api/business/alipay-authorizations/{row['id']}/refresh/", '{}', content_type='application/json')
        self.assertEqual(response.json()['data']['status'], 'authorized')
        self.adapter.create_app.assert_called_once()

    def test_existing_identity_review_refreshes_to_signed_not_payment_ready(self):
        client = make_logged_in_client(is_superuser=True)
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        row = client.post('/api/business/alipay-authorizations/', json.dumps(payload), content_type='application/json').json()['data']
        path = f"/api/business/alipay-authorizations/{row['id']}/refresh/"
        self.adapter.refresh.return_value = {'configured': False, 'next_action': 'identity', 'review_code': 'IDENTITY_INCOMPLETE'}
        self.assertTrue(client.post(path, '{}', content_type='application/json').json()['data']['requires_platform_review'])
        self.adapter.refresh.return_value = {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True}
        response = client.post(path, '{}', content_type='application/json').json()['data']
        self.assertEqual(response['status'], 'signed')
        self.assertFalse(response['requires_platform_review'])
        self.assertFalse(response['last_error'])
        control = Authorization.objects.get(request_id=self.request_id)
        self.assertEqual(control.phase, 'signed')
        self.assertFalse(control.configured)

    def test_ambiguous_create_is_not_retried(self):
        self.adapter.create_app.side_effect = UnknownResult('private error')
        control = get_control_client()
        for _ in range(2):
            with self.assertRaises(ControlOutcomeUnknown):
                control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.adapter.create_app.assert_called_once()
        self.assertEqual(Authorization.objects.get(request_id=self.request_id).phase, 'unknown')

    def test_captcha_failure_is_visible_and_preserves_original_attempt(self):
        from types import SimpleNamespace
        from apps.settlement_control.upstream import JeepayAdapter
        client = make_logged_in_client(is_superuser=True)
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        def failed_login():
            adapter = JeepayAdapter.__new__(JeepayAdapter)
            upstream = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'code': 9999, 'msg': '验证码有误！'})
            adapter.http = SimpleNamespace(request=lambda *a, **k: upstream)
            return adapter._call('POST', '/api/anon/auth/validate')
        with patch('apps.settlement_control.views._adapter', side_effect=failed_login):
            response = client.post('/api/business/alipay-authorizations/', json.dumps(payload), content_type='application/json')
        result = response.json()['data']
        self.assertEqual(result['last_error'], '验证码识别失败，请稍后重试')
        self.assertTrue(result['retryable_login'])
        listed = client.get('/api/business/alipay-authorizations/').json()['data'][0]
        self.assertTrue(listed['retryable_login'])
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(result['requires_platform_review'])
        self.assertEqual(Authorization.objects.get(request_id=self.request_id).phase, 'new')
        self.adapter.create_app.assert_not_called()
        Authorization.objects.filter(request_id=self.request_id).update(phase='unknown', unknown_action='create_app')
        unsafe = client.get('/api/business/alipay-authorizations/').json()['data'][0]
        self.assertTrue(unsafe['requires_platform_review'])
        self.assertFalse(unsafe['retryable_login'])

    def test_missing_configuration_never_calls_adapter(self):
        missing = ('ALIPAY_AUTH_CONTROL_CREDENTIAL', 'SETTLEMENT_FERNET_KEY',
                   'SETTLEMENT_USERNAME', 'SETTLEMENT_PASSWORD', 'SETTLEMENT_MERCHANT',
                   'SETTLEMENT_TESSERACT', 'SETTLEMENT_OPERATION_LOCK_FILE')
        for key in missing:
            with self.subTest(key=key), override_settings(**{key: ''}):
                with self.assertRaises(ControlUnavailable):
                    get_control_client()
        with override_settings(SETTLEMENT_FERNET_KEY=self.data_key):
            with self.assertRaises(ControlUnavailable):
                get_control_client()
        self.adapter.create_app.assert_not_called()
        self.assertFalse(Authorization.objects.exists())

    def test_untrusted_lock_stops_before_upstream(self):
        with patch('apps.settlement_control.locking._trusted_local_lock', return_value=False):
            with self.assertRaises(ControlUnavailable):
                get_control_client().create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.adapter.create_app.assert_not_called()
        self.assertEqual(Authorization.objects.get(request_id=self.request_id).phase, 'new')

    def test_local_state_is_tenant_only_and_isolated(self):
        self.assertIn('settlement_control_authorization', connection.introspection.table_names())
        get_control_client().create(self.request_id, 'payer-a@example.invalid', '甲用户')
        tenant, _ = provision_tenant(schema_name='local_alipay_b', name='租户 B', domain='local-alipay-b.test.local')
        with tenant_context(tenant):
            self.assertFalse(Authorization.objects.filter(request_id=self.request_id).exists())
            other_id = str(uuid.uuid4())
            get_control_client().create(other_id, 'payer-b@example.invalid', '乙用户')
            self.assertEqual(list(Authorization.objects.values_list('request_id', flat=True)), [uuid.UUID(other_id)])
        self.assertEqual(list(Authorization.objects.values_list('request_id', flat=True)), [uuid.UUID(self.request_id)])
        with schema_context('public'):
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.settlement_control_authorization')")
                self.assertIsNone(cursor.fetchone()[0])

    def test_manual_reconcile_refuses_public_context(self):
        with schema_context('public'), self.assertRaisesMessage(CommandError, '租户上下文'):
            call_command('reconcile_settlement_attempt', client='local', request_id=uuid.uuid4(),
                         operator='tester', evidence_file='/missing', resolution='app_absent',
                         confirm_upstream_conclusion=True, confirm_no_inflight=True,
                         confirm_workers_protected=True)

    def test_refresh_uncertain_preserves_authorization_and_requires_review(self):
        control = get_control_client()
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.assertEqual(control.refresh(self.request_id).status, 'authorized')
        self.adapter.refresh.side_effect = UnknownResult('timeout')
        for method in ('refresh', 'authorize'):
            with self.assertRaises(ControlOutcomeUnknown):
                getattr(control, method)(self.request_id)
        self.assertEqual(self.adapter.refresh.call_count, 2)
        row = Authorization.objects.get(request_id=self.request_id)
        self.assertEqual((row.phase, row.unknown_action, row.status, row.configured),
                         ('unknown', 'refresh', 'authorized', True))
        with tempfile.NamedTemporaryFile(mode='w') as evidence:
            evidence.write('synthetic evidence that no upstream request remains')
            evidence.flush()
            call_command('reconcile_settlement_attempt', client='local', request_id=uuid.UUID(self.request_id),
                         operator='tester', evidence_file=evidence.name, resolution='refresh_reconciled',
                         confirm_upstream_conclusion=True, confirm_no_inflight=True,
                         confirm_workers_protected=True)
        row.refresh_from_db()
        self.assertEqual((row.phase, row.status, row.configured), ('authorized', 'authorized', True))

    def test_signed_refresh_uncertainty_and_manual_recovery_preserve_signed(self):
        control = get_control_client()
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        self.adapter.refresh.return_value = {'configured': False, 'next_action': 'agreement', 'signed_without_pid': True}
        self.assertEqual(control.refresh(self.request_id).status, 'signed')
        self.adapter.refresh.side_effect = UnknownResult('timeout')
        with self.assertRaises(ControlOutcomeUnknown):
            control.refresh(self.request_id)
        row = Authorization.objects.get(request_id=self.request_id)
        self.assertEqual((row.phase, row.status, row.configured), ('unknown', 'signed', False))
        with tempfile.NamedTemporaryFile(mode='w') as evidence:
            evidence.write('synthetic evidence that no upstream request remains')
            evidence.flush()
            call_command('reconcile_settlement_attempt', client='local', request_id=uuid.UUID(self.request_id),
                         operator='tester', evidence_file=evidence.name, resolution='refresh_reconciled',
                         confirm_upstream_conclusion=True, confirm_no_inflight=True,
                         confirm_workers_protected=True)
        row.refresh_from_db()
        self.assertEqual((row.phase, row.status, row.configured), ('signed', 'signed', False))

    def test_refresh_locks_until_result_persisted(self):
        from apps.settlement_control.locking import LockUnavailable, operation_lock
        from apps.settlement_control.views import _finish_refresh

        control = get_control_client()
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')

        def refresh(*args):
            self.assertEqual(Authorization.objects.get(request_id=self.request_id).phase, 'refreshing')
            with self.assertRaises(LockUnavailable), operation_lock(exclusive=True):
                pass
            return {'configured': True, 'next_action': 'agreement'}

        def finish(*args):
            with self.assertRaises(LockUnavailable), operation_lock(exclusive=True):
                pass
            return _finish_refresh(*args)

        self.adapter.refresh.side_effect = refresh
        with patch('apps.settlement_control.views._finish_refresh', side_effect=finish):
            self.assertEqual(control.refresh(self.request_id).status, 'authorized')
        self.assertEqual(Authorization.objects.get(request_id=self.request_id).phase, 'authorized')

    def test_second_refresh_during_first_does_not_call_upstream(self):
        control = get_control_client()
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')

        def refresh(*args):
            with self.assertRaises(ControlOutcomeUnknown):
                control.refresh(self.request_id)
            return {'configured': True, 'next_action': 'agreement'}

        self.adapter.refresh.side_effect = refresh
        self.assertEqual(control.refresh(self.request_id).status, 'authorized')
        self.adapter.refresh.assert_called_once()

    def test_process_death_during_refresh_never_retries(self):
        control = get_control_client()
        control.create(self.request_id, 'payer@example.invalid', '测试姓名')
        row = Authorization.objects.get(request_id=self.request_id)
        row.phase = 'refreshing'
        row.save(update_fields=['phase'])
        output = StringIO()
        call_command('list_settlement_unknown', client='local', stdout=output)
        listed = json.loads(output.getvalue())
        self.assertEqual((listed['request_id'], listed['phase']), (self.request_id, 'refreshing'))
        self.assertNotIn('payer@example.invalid', output.getvalue())
        for action in (lambda: control.create(self.request_id, 'payer@example.invalid', '测试姓名'),
                       lambda: control.authorize(self.request_id), lambda: control.refresh(self.request_id)):
            with self.assertRaises(ControlOutcomeUnknown):
                action()
        self.adapter.refresh.assert_not_called()

    def test_listing_marks_unknown_refresh_on_authorized_record(self):
        client = make_logged_in_client(is_superuser=True)
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        response = client.post('/api/business/alipay-authorizations/', json.dumps(payload), content_type='application/json')
        pk = response.json()['data']['id']
        client.post(f'/api/business/alipay-authorizations/{pk}/refresh/', '{}', content_type='application/json')
        for phase in ('refreshing', 'unknown'):
            Authorization.objects.filter(request_id=self.request_id).update(phase=phase, unknown_action='refresh' if phase == 'unknown' else '')
            result = client.get('/api/business/alipay-authorizations/').json()['data'][0]
            self.assertEqual(result['status'], 'authorized')
            self.assertTrue(result['requires_platform_review'])
            self.assertEqual(result['last_error'], '操作结果待平台核对，请勿重复操作')
            result = client.post(f'/api/business/alipay-authorizations/{pk}/refresh/', '{}', content_type='application/json').json()['data']
            self.assertTrue(result['requires_platform_review'])

    def test_manual_review_unblocks_tenant_for_each_unknown_operation(self):
        from apps.business.models import AlipaySettlementAuthorization

        client = make_logged_in_client(is_superuser=True)
        base = '/api/business/alipay-authorizations/'
        cases = ('create', 'authorize', 'refresh')
        for case in cases:
            with self.subTest(case=case), tempfile.NamedTemporaryFile(mode='w') as evidence:
                evidence.write('synthetic evidence that no upstream request remains')
                evidence.flush()
                request_id = str(uuid.uuid4())
                payload = {'request_id': request_id, 'account': f'{case}@example.invalid', 'real_name': '测试姓名'}
                self.adapter.create_app.side_effect = UnknownResult('failed') if case == 'create' else None
                self.adapter.authorize.side_effect = UnknownResult('failed') if case == 'authorize' else None
                self.adapter.refresh.side_effect = None
                result = client.post(base, json.dumps(payload), content_type='application/json').json()['data']
                if case == 'refresh':
                    self.adapter.refresh.side_effect = UnknownResult('failed')
                    result = client.post(f"{base}{result['id']}/refresh/", '{}', content_type='application/json').json()['data']
                self.assertTrue(result['requires_platform_review'])
                resolution = {'create': 'app_absent', 'authorize': 'authorization_unavailable', 'refresh': 'refresh_reconciled'}[case]
                call_command('reconcile_settlement_attempt', client='local', request_id=uuid.UUID(request_id),
                             operator='tester', evidence_file=evidence.name, resolution=resolution,
                             confirm_upstream_conclusion=True, confirm_no_inflight=True,
                             confirm_workers_protected=True)
                saved = AlipaySettlementAuthorization.objects.get(request_id=request_id)
                self.assertFalse(saved.requires_platform_review)
                self.adapter.create_app.side_effect = None
                self.adapter.authorize.side_effect = None
                self.adapter.refresh.side_effect = None
                endpoint = 'refresh' if case == 'refresh' else 'authorize'
                result = client.post(f'{base}{saved.pk}/{endpoint}/', '{}', content_type='application/json').json()['data']
                self.assertFalse(result['requires_platform_review'])

    def test_reconcile_invalidates_older_tenant_request(self):
        from apps.business.models import AlipaySettlementAuthorization

        client = make_logged_in_client(is_superuser=True)
        base = '/api/business/alipay-authorizations/'
        self.adapter.create_app.side_effect = UnknownResult('failed')
        payload = {'request_id': self.request_id, 'account': 'payer@example.invalid', 'real_name': '测试姓名'}
        first = client.post(base, json.dumps(payload), content_type='application/json').json()['data']
        self.assertTrue(first['requires_platform_review'])
        with tempfile.NamedTemporaryFile(mode='w') as evidence:
            evidence.write('synthetic evidence that no upstream request remains')
            evidence.flush()

            def reconcile(*args):
                call_command('reconcile_settlement_attempt', client='local', request_id=uuid.UUID(self.request_id),
                             operator='tester', evidence_file=evidence.name, resolution='app_absent',
                             confirm_upstream_conclusion=True, confirm_no_inflight=True,
                             confirm_workers_protected=True)
                raise ControlOutcomeUnknown()

            fake = Mock()
            fake.refresh.side_effect = reconcile
            with patch('apps.business.api.alipay_authorization_views.get_control_client', return_value=fake):
                result = client.post(f"{base}{first['id']}/refresh/", '{}', content_type='application/json').json()['data']
        self.assertFalse(result['requires_platform_review'])
        self.assertEqual(result['last_error'], '')
        self.assertFalse(AlipaySettlementAuthorization.objects.get(request_id=self.request_id).requires_platform_review)


from pathlib import Path
from types import SimpleNamespace
from django.test import SimpleTestCase
from apps.settlement_control.locking import _trusted_local_lock, trusted_executable


class LocalPathSecurityTests(SimpleTestCase):
    def test_executable_requires_root_owned_nonwritable_path_and_no_symlink(self):
        candidate = Path('/usr/bin/true')
        self.assertTrue(trusted_executable(str(candidate)))
        original = Path.lstat
        actual_file = original(candidate)
        actual_parent = original(candidate.parent)
        with patch.object(Path, 'lstat', autospec=True, side_effect=lambda p: SimpleNamespace(st_uid=12345, st_mode=actual_file.st_mode) if p == candidate else original(p)):
            self.assertFalse(trusted_executable(str(candidate)))
        with patch.object(Path, 'lstat', autospec=True, side_effect=lambda p: SimpleNamespace(st_uid=actual_parent.st_uid, st_mode=actual_parent.st_mode | 0o020) if p == candidate.parent else original(p)):
            self.assertFalse(trusted_executable(str(candidate)))
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / 'tool'
            link.symlink_to(candidate)
            self.assertFalse(trusted_executable(str(link)))

    def test_lock_requires_trusted_file_and_parent_and_rejects_replacement(self):
        candidate = Path('/usr/bin/true')
        original = Path.lstat
        actual_parent = original(candidate.parent)
        with candidate.open('rb') as file:
            self.assertTrue(_trusted_local_lock(str(candidate), file.fileno()))
            real_fstat = os.fstat(file.fileno())
            bad_owner = SimpleNamespace(st_mode=real_fstat.st_mode, st_uid=12345, st_nlink=1,
                                        st_dev=real_fstat.st_dev, st_ino=real_fstat.st_ino)
            with patch('apps.settlement_control.locking.os.fstat', return_value=bad_owner):
                self.assertFalse(_trusted_local_lock(str(candidate), file.fileno()))
            replaced = SimpleNamespace(st_mode=real_fstat.st_mode, st_uid=0, st_nlink=1,
                                       st_dev=real_fstat.st_dev, st_ino=real_fstat.st_ino + 1)
            with patch('apps.settlement_control.locking.os.fstat', return_value=replaced):
                self.assertFalse(_trusted_local_lock(str(candidate), file.fileno()))
            with patch.object(Path, 'lstat', autospec=True, side_effect=lambda p: SimpleNamespace(st_uid=actual_parent.st_uid, st_mode=actual_parent.st_mode | 0o002) if p == candidate.parent else original(p)):
                self.assertFalse(_trusted_local_lock(str(candidate), file.fileno()))
            with tempfile.TemporaryDirectory() as tmp:
                link = Path(tmp) / 'lock'
                link.symlink_to(candidate)
                self.assertFalse(_trusted_local_lock(str(link), file.fileno()))


class UpstreamLoggingTests(SimpleTestCase):
    def test_real_log_handlers_do_not_receive_private_call_details(self):
        import logging
        from apps.settlement_control.upstream import JeepayAdapter

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = Mock()
        response = Mock()
        response.json.return_value = {'code': 0, 'data': {}}

        def request(*args, **kwargs):
            logging.getLogger('httpx').info('GET https://agent.jeepay.vip/path?account=secret-account')
            logging.getLogger('httpcore.http11').debug('token=secret-token')
            return response

        adapter.http.request.side_effect = request
        with self.assertLogs(level='DEBUG') as logs:
            logging.getLogger('httpx').info('other request before')
            adapter._call('GET', '/synthetic')
            logging.getLogger('httpx').info('other request after')
        combined = '\n'.join(logs.output)
        self.assertIn('other request before', combined)
        self.assertIn('other request after', combined)
        self.assertNotIn('secret-account', combined)
        self.assertNotIn('secret-token', combined)

    def test_failed_call_restores_logging_and_hides_upstream_exception(self):
        import logging
        import httpx
        from apps.settlement_control.upstream import JeepayAdapter, UnknownResult

        adapter = JeepayAdapter.__new__(JeepayAdapter)
        adapter.http = Mock()
        adapter.http.request.side_effect = httpx.ReadTimeout('secret-account')
        with self.assertRaises(UnknownResult) as ctx:
            adapter._call('GET', '/synthetic')
        self.assertNotIn('secret-account', str(ctx.exception))
        with self.assertLogs('httpx', level='INFO') as logs:
            logging.getLogger('httpx').info('other request after failure')
        self.assertIn('other request after failure', logs.output[0])
