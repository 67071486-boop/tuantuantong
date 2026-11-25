import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from django.db import connection
from django.conf import settings
from django.test import TransactionTestCase, override_settings

from tests.p5_auth_helpers import make_logged_in_client

BASE = '/api/internal-chat/'


class ChatCoreBridgeTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant
        from django_tenants.utils import get_tenant_domain_model
        cls.tenant, _ = provision_tenant(schema_name='core_bridge_test', name='核心桥接甲', domain='core.test.local')
        cls.domain = get_tenant_domain_model().objects.get(tenant=cls.tenant)
        cls.added_hosts = [host for host in ('core.test.local', 'core-other.test.local') if host not in settings.ALLOWED_HOSTS]
        settings.ALLOWED_HOSTS += cls.added_hosts
        connection.set_tenant(cls.tenant)
        cls.other_tenant, _ = provision_tenant(schema_name='core_bridge_other', name='核心桥接乙', domain='core-other.test.local')

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.other_tenant.delete(force_drop=True)
        cls.tenant.delete(force_drop=True)
        for host in cls.added_hosts:
            settings.ALLOWED_HOSTS.remove(host)
        super().tearDownClass()

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.assertTrue(connection.get_autocommit())
        self.enterContext(override_settings(DEBUG=True, TT_CHAT_ORDER_GROUPS_ENABLED=True,
                                            TT_CHAT_CORE_BACKEND='legacy'))
        self.alice = make_logged_in_client(domain=self.domain.domain, username='core-alice', games=None)
        self.bob = make_logged_in_client(domain=self.domain.domain, username='core-bob', games=None)
        from django.contrib.auth import get_user_model
        self.alice_id = get_user_model().objects.get(username='core-alice').pk
        self.bob_id = get_user_model().objects.get(username='core-bob').pk
        self.folder = self.enterContext(TemporaryDirectory())
        self.config_file = Path(self.folder) / 'core.json'
        self.config_file.write_text(json.dumps({'service_url': 'http://127.0.0.1:9050', 'tenants': {
            connection.schema_name: {'key_id': 'core-a', 'secret': 'synthetic-core-a-key'},
        }}))
        self.config_file.chmod(0o600)
        self.enterContext(override_settings(TT_CHAT_SERVICE_CONFIG_FILE=str(self.config_file)))
        self.calls = []
        self.cid, self.mid = 1_000_000_000, 1_000_000_001
        self.message = {'id': self.mid, 'seq': 1, 'conversation_id': self.cid,
                        'sender_id': self.alice_id, 'text': '独立写入消息', 'created_at': '2026-11-20T00:00:00Z',
                        'revoked': False, 'reply_to_id': None}
        self.summary = {'id': self.cid, 'peer': {'id': self.bob_id, 'name': 'core-bob', 'avatar_url': None},
                        'last_message': self.message, 'unread_count': 0, 'read_seq': 1, 'peer_read_seq': 0,
                        'pinned': False, 'remark': '', 'marked_unread': False}

    def enable_browser_ticket(self):
        config = json.loads(self.config_file.read_text())
        tenant = config['tenants'][self.tenant.schema_name]
        tenant['browser'] = {'audience': 'https://chat.test.example', 'origins': [f'https://{self.domain.domain}']}
        self.config_file.write_text(json.dumps(config))
        return tenant

    def browser_claims(self, client, tenant):
        from chat_service.browser_ticket import verify_browser_ticket

        response = client.post(BASE + 'direct-ticket/')
        ticket = self.data(response)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertTrue(ticket['enabled'])
        claims = verify_browser_ticket(ticket['token'], {tenant['key_id']: tenant},
                                       origin=tenant['browser']['origins'][0], host='chat.test.example')
        self.assertEqual(claims['exp'], ticket['expires_at'])
        self.assertEqual(claims['exp'] - claims['iat'], ticket['ttl_seconds'])
        self.assertEqual(claims['viewer'], ticket['user_id'])
        return {key: claims[key] for key in ('viewer', 'origin', 'proof')}

    def browser_authority(self, claims, tenant, **metadata):
        from django.test import Client
        from chat_service.protocol import sign_request

        path = BASE + 'service/browser-authorize/'
        raw = json.dumps(claims | metadata).encode()
        signing = sign_request(tenant['secret'], tenant['key_id'], 'POST', path, raw)
        class CookielessClient(Client):
            def _base_environ(self, **request):
                environ = super()._base_environ(**request)
                environ.pop('HTTP_COOKIE', None)
                return environ

        client = CookielessClient(SERVER_NAME=tenant['browser']['origins'][0].removeprefix('https://'))
        return self.data(client.post(path, raw, content_type='application/json', headers=signing))

    def test_browser_ticket_is_opt_in_and_rechecks_actual_session_and_tenant(self):
        from django.test import Client
        from django_tenants.utils import tenant_context

        path = BASE + 'direct-ticket/'
        self.assertEqual(self.data(self.alice.post(path)), {'enabled': False})
        tenant = self.enable_browser_ticket()
        self.assertEqual(self.data(self.alice.post(path)), {'enabled': False})
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            self.assertEqual(Client(SERVER_NAME=self.domain.domain).post(path).status_code, 401)
            self.assertEqual(self.alice.post(path, HTTP_ORIGIN='https://foreign.test.example').status_code, 403)
            claims = self.browser_claims(self.alice, tenant)
            self.assertEqual(claims['viewer'], self.alice_id)
            self.assertNotIn(self.alice.cookies[settings.SESSION_COOKIE_NAME].value, claims['proof'])
            self.assertEqual(self.browser_authority(claims, tenant)['viewer']['id'], self.alice_id)
            self.assertEqual(self.browser_authority(claims, tenant, viewer=self.bob_id)['status'], 401)
            self.assertEqual(self.browser_authority(claims, tenant, origin='https://foreign.test.example')['status'], 403)
            self.data(self.alice.post('/api/auth/logout'))
            self.assertEqual(self.browser_authority(claims, tenant)['status'], 401)
            foreign = Client(SERVER_NAME='core-other.test.local')
            foreign.cookies.update(self.bob.cookies)
            with tenant_context(self.tenant):
                self.assertEqual(foreign.post(path).status_code, 401)

    @override_settings(DEBUG=False, TT_CHAT_CORE_BACKEND='service', TT_CHAT_ORDER_GROUPS_ENABLED=False)
    def test_disabled_chat_returns_disabled_ticket_after_normal_authentication(self):
        from django.test import Client

        self.enable_browser_ticket()
        path = BASE + 'direct-ticket/'
        response = self.alice.post(path)
        self.assertEqual(self.data(response), {'enabled': False})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(Client(SERVER_NAME=self.domain.domain).post(path).status_code, 401)
        with override_settings(TT_CHAT_MAINTENANCE=True):
            self.assertEqual(self.alice.post(path).status_code, 503)

    @override_settings(TT_CHAT_CORE_BACKEND='service')
    def test_browser_authority_rejects_other_enabled_tenant_proof_with_the_same_user_id(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django_tenants.utils import tenant_context

        from apps.internal_chat.browser_access import _backing_reference

        primary = self.enable_browser_ticket()
        secondary_host = 'core-other.test.local'
        secondary = {'key_id': 'core-b', 'secret': 'synthetic-core-b-key', 'browser': {
            'audience': 'https://chat.test.example', 'origins': [f'https://{secondary_host}'],
        }}
        config = json.loads(self.config_file.read_text())
        config['tenants'][self.other_tenant.schema_name] = secondary
        self.config_file.write_text(json.dumps(config))
        first = self.browser_claims(self.alice, primary)
        self.assertEqual(self.browser_authority(first, primary)['viewer']['name'], 'core-alice')

        with tenant_context(self.other_tenant):
            username, password = 'secondary-core-alice', 'synthetic-local-password'
            get_user_model().objects.create_user(pk=self.alice_id, username=username, password=password)
            client = Client(SERVER_NAME=secondary_host)
            self.data(client.post('/api/auth/login', {'username': username, 'password': password},
                                  content_type='application/json'))
            second = self.browser_claims(client, secondary)
            self.assertEqual((first['viewer'], second['viewer']), (self.alice_id, self.alice_id))
            self.assertEqual(self.browser_authority(second, secondary)['viewer']['name'], username)
            self.assertEqual(self.browser_authority(second | {'proof': first['proof']}, secondary)['status'], 401)

            def wrong_plane(request):
                return [self.tenant.schema_name, *_backing_reference(request)[1:]]

            with patch('apps.internal_chat.browser_access._backing_reference', side_effect=wrong_plane):
                foreign = self.browser_claims(client, secondary)
            self.assertEqual(self.browser_authority(foreign, secondary)['status'], 401)

    @override_settings(TT_CHAT_CORE_BACKEND='service', TT_CHAT_REALTIME_SECRET='synthetic',
                       TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify')
    def test_browser_authority_preserves_order_permission_directory_and_app_worker_scope(self):
        from django.test import Client
        from django.utils import timezone
        from apps.business.models import Game, Order
        from apps.internal_chat.models import ContactDirectory

        tenant = self.enable_browser_ticket()
        viewer = make_logged_in_client(domain=self.domain.domain, username='browser-order-viewer', is_superuser=True)
        order = Order.objects.create(order_no='20261120000000000101', game=Game.objects.create(name='直连鉴权测试'))
        claims = self.browser_claims(viewer, tenant)
        self.assertEqual(self.browser_authority(claims, tenant, order_no=order.order_no)['status'], 200)
        claims = self.browser_claims(self.alice, tenant)
        self.assertEqual(self.browser_authority(claims, tenant, order_no=order.order_no)['status'], 404)
        self.assertEqual(self.browser_authority(claims, tenant, notification='app')['status'], 404)
        self.assertEqual(self.browser_authority(claims, tenant, notification='web')['status'], 200)

        chat, app_token = self.native_chat_client()
        chat_claims = self.browser_claims(chat, tenant)
        self.assertTrue(self.browser_authority(chat_claims, tenant)['worker_directory'])
        self.assertEqual(self.browser_authority(chat_claims, tenant, notification='app')['status'], 404)
        self.assertEqual(self.browser_authority(chat_claims, tenant, peer=self.alice_id)['status'], 403)
        ContactDirectory.objects.update_or_create(pk=1, defaults={'primary_contact_id': self.alice_id})
        self.assertEqual(self.browser_authority(chat_claims, tenant, peer=self.alice_id)['status'], 200)
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION='Bearer synthetic-unread-app-token')
        app_claims = self.browser_claims(app, tenant)
        self.assertEqual(self.browser_authority(app_claims, tenant, notification='app')['status'], 200)
        self.assertEqual(self.browser_authority(app_claims, tenant)['status'], 404)
        app_token.revoked_at = timezone.now()
        app_token.save(update_fields=['revoked_at'])
        self.assertEqual(self.browser_authority(app_claims, tenant, notification='app')['status'], 401)
        self.assertEqual(self.browser_authority(chat_claims, tenant)['status'], 401)

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['code'], 0, response.content)
        return response.json()['data']

    def remote(self, url, *, content, headers, timeout, trust_env):
        from chat_service.protocol import verify_request
        self.assertFalse(trust_env)
        self.assertTrue(url.endswith('/v1/core'))
        verify_request({'core-a': 'synthetic-core-a-key'}, headers, 'POST', '/v1/core', content)
        body = json.loads(content)
        self.calls.append(body)
        operation = body['operation']
        responses = {
            'start': self.summary, 'send': self.message, 'history': {'messages': [self.message], 'has_more': False, 'has_newer': False},
            'conversations': {'conversations': [self.summary], 'cursor': 3}, 'unread': {'unread_count': 1},
            'read': {'read_seq': 1}, 'preferences': {'pinned': True, 'remark': '远端备注', 'marked_unread': True},
            'sync': {'changes': [{'kind': 'message.created', 'conversation_id': self.cid, 'message': self.message}], 'cursor': 3, 'has_more': False},
            'peer': {'peer_id': self.bob_id}, 'search': {'messages': [self.message], 'count': 1, 'has_more': False},
            'image': {'mime': 'image/png', 'data': 'synthetic-image'}, 'revoke': {**self.message, 'revoked': True, 'text': ''},
            'notification': {'messages': [self.message], 'has_more': False, 'has_newer': False, 'kind': 'direct',
                             'peer_name': 'core-alice', 'order_no': '', 'self_member_id': self.bob_id, 'can_reply': True,
                             'senders': [{'id': self.alice_id, 'name': 'core-alice', 'studio_name': ''}], 'remark': ''},
        }
        self.assertIn(operation, responses)
        return httpx.Response(200, json={'code': 0, 'msg': '', 'data': responses[operation]}, request=httpx.Request('POST', url))

    def reject_old_chat(self, execute, sql, params, many, context):
        if re.search(r'"internal_chat_(?:message|change|changecursor|conversation|conversationpreference)"', sql):
            raise AssertionError('old chat storage accessed')
        return execute(sql, params, many, context)

    def test_private_chat_works_with_old_message_conversation_and_change_storage_blocked(self):
        with connection.execute_wrapper(self.reject_old_chat), self.assertRaisesMessage(AssertionError, 'old chat storage'):
            self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json')
        with override_settings(TT_CHAT_CORE_BACKEND='service'), connection.execute_wrapper(self.reject_old_chat), patch(
            'httpx.post', side_effect=self.remote,
        ):
            self.assertEqual(self.data(self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json'))['id'], self.cid)
            root = BASE + f'conversations/{self.cid}/'
            self.assertEqual(self.data(self.alice.post(root + 'messages/', {'text': '独立写入消息', 'client_id': 'core-1'}, content_type='application/json')), self.message)
            self.assertEqual(self.data(self.bob.get(root + 'messages/'))['messages'], [self.message])
            self.assertEqual(self.data(self.alice.get(BASE + 'conversations/'))['conversations'][0]['id'], self.cid)
            self.assertEqual(self.data(self.bob.post(root + 'read/', {'seq': 1}, content_type='application/json')), {'read_seq': 1})
            self.assertEqual(self.data(self.bob.post(root + 'preferences/', {'pinned': True, 'remark': '远端备注', 'marked_unread': True}, content_type='application/json'))['remark'], '远端备注')
            self.assertEqual(self.data(self.bob.get(BASE + 'sync/', {'after': 0}))['cursor'], 3)
        self.assertEqual([call['operation'] for call in self.calls], ['start', 'send', 'history', 'conversations', 'read', 'preferences', 'sync'])
        self.assertEqual([call['viewer_id'] for call in self.calls], [self.alice_id, self.alice_id, self.bob_id, self.alice_id, self.bob_id, self.bob_id, self.bob_id])

    def test_authenticated_viewer_cannot_be_overridden_by_query_or_payload(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=self.remote):
            root = BASE + f'conversations/{self.cid}/'
            self.data(self.alice.get(root + 'messages/', {'viewer_id': self.bob_id, 'tenant': 'other', 'readonly': True}))
            self.data(self.alice.post(root + 'messages/', {'text': '独立写入消息', 'client_id': 'secure',
                           'viewer_id': self.bob_id, 'sender_id': self.bob_id, 'readonly': True}, content_type='application/json'))
        for body in self.calls:
            self.assertEqual(body['viewer_id'], self.alice_id)
            self.assertFalse(body['readonly'])
            self.assertNotIn('tenant', body['query'])
            self.assertNotIn('sender_id', body['payload'])
            self.assertNotIn('viewer_id', body['payload'])

    def test_core_failures_do_not_fall_back_to_legacy_or_report_success(self):
        request = httpx.Request('POST', 'http://127.0.0.1/v1/core')
        failures = [
            (httpx.Response(200, json={'code': 403, 'msg': 'denied', 'data': {'id': 99}}, request=request), 503, 503),
            (httpx.Response(403, json={'code': 403, 'msg': 'denied', 'data': None}, request=request), 403, 403),
            (httpx.Response(404, json={'code': 404, 'msg': 'missing', 'data': None}, request=request), 200, 1002),
            (httpx.ConnectError('unavailable'), 503, 503),
        ]
        for failure, status, code in failures:
            args = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with self.subTest(code=code), override_settings(TT_CHAT_CORE_BACKEND='service'), connection.execute_wrapper(
                self.reject_old_chat,
            ), patch('httpx.post', **args):
                response = self.alice.post(BASE + f'conversations/{self.cid}/messages/',
                                            {'text': '不能写回旧库', 'client_id': 'unavailable'}, content_type='application/json')
            self.assertEqual((response.status_code, response.json()['code'], response.json()['data']), (status, code, None))

    def test_image_search_revoke_peer_and_notification_endpoints_use_core_for_new_ids(self):
        import base64
        from io import BytesIO
        from PIL import Image

        image = BytesIO()
        Image.new('RGB', (2, 3), 'blue').save(image, format='PNG')
        encoded = base64.b64encode(image.getvalue()).decode()
        root = BASE + f'conversations/{self.cid}/'
        with override_settings(TT_CHAT_CORE_BACKEND='service', TT_CHAT_REALTIME_SECRET='synthetic',
                               TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify'), connection.execute_wrapper(
            self.reject_old_chat,
        ), patch('httpx.post', side_effect=self.remote):
            self.data(self.alice.post(root + 'messages/', {'text': '图片', 'client_id': 'image',
                       'image': {'mime': 'image/png', 'data': encoded}}, content_type='application/json'))
            self.assertEqual(self.calls[-1]['payload']['image'], {'mime': 'image/png', 'data': encoded})
            self.data(self.alice.get(root + 'messages/search/', {'q': '独立', 'before': 10, 'sender_id': self.alice_id}))
            self.assertEqual(self.calls[-1]['query'], {'conversation_id': self.cid, 'q': '独立', 'before': 10,
                                                      'sender_id': self.alice_id, 'image_only': False})
            self.assertEqual(self.data(self.alice.get(root + f'messages/{self.mid}/image/'))['mime'], 'image/png')
            self.assertTrue(self.data(self.alice.post(root + f'messages/{self.mid}/revoke/'))['revoked'])
            self.data(self.alice.post(root + 'presence-ticket/'))
            result = self.data(self.bob.get(BASE + f'notification-conversations/{self.cid}/messages/'))
            self.assertEqual((result['peer_name'], result['self_member_id']), ('core-alice', self.bob_id))
            self.data(self.bob.post(BASE + f'notification-conversations/{self.cid}/read/', {'seq': 1}, content_type='application/json'))
            denied = self.bob.get(BASE + f'order-groups/{self.cid}/messages/')
            self.assertEqual(denied.json()['code'], 1002)

    def test_legacy_global_and_tenant_override_keep_old_path_and_bad_config_fails_closed(self):
        self.config_file.write_text('bad json')
        with patch('httpx.post', side_effect=AssertionError('must not call')):
            local = self.data(self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json'))
        self.assertLess(local['id'], self.cid)
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            self.assertEqual(self.alice.get(BASE + 'conversations/').status_code, 503)
        self.config_file.write_text(json.dumps({'service_url': 'http://127.0.0.1:9050', 'tenants': {
            connection.schema_name: {'key_id': 'core-a', 'secret': 'synthetic-core-a-key', 'core_backend': 'legacy'},
        }}))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=AssertionError('must not call')):
            self.assertEqual(self.data(self.alice.get(BASE + 'conversations/'))['conversations'][0]['id'], local['id'])
        config = json.loads(self.config_file.read_text())
        for invalid in ('invalid', None, 1):
            config['tenants'][connection.schema_name]['core_backend'] = invalid
            self.config_file.write_text(json.dumps(config))
            with override_settings(TT_CHAT_CORE_BACKEND='service'):
                self.assertEqual(self.alice.get(BASE + 'conversations/').status_code, 503)

    def test_signed_source_export_reports_real_server_write_fence_and_rejects_request_override(self):
        from tests.test_chat_service_bridge import ServiceClient
        from chat_service.protocol import sign_request

        source = ServiceClient(SERVER_NAME=self.domain.domain)
        for suffix in ('service/export/', 'service/snapshot/?kind=conversations'):
            def fetch():
                path = BASE + suffix
                headers = sign_request('synthetic-core-a-key', 'core-a', 'GET', path, b'')
                return source.get(path, **{'HTTP_' + key.upper().replace('-', '_'): value for key, value in headers.items()})
            self.assertIs(self.data(fetch())['core_write_fenced'], False)
            with override_settings(TT_CHAT_CORE_BACKEND='service'):
                self.assertIs(self.data(fetch())['core_write_fenced'], True)
        path = BASE + 'service/export/?core_write_fenced=true'
        headers = sign_request('synthetic-core-a-key', 'core-a', 'GET', path, b'')
        self.assertEqual(source.get(path, **{'HTTP_' + key.upper().replace('-', '_'): value for key, value in headers.items()}).status_code, 400)

    def source_fence(self):
        from tests.test_chat_service_bridge import ServiceClient
        from chat_service.protocol import sign_request

        pages = []
        for suffix in ('service/export/', 'service/snapshot/?kind=conversations'):
            path = BASE + suffix
            headers = sign_request('synthetic-core-a-key', 'core-a', 'GET', path, b'')
            response = ServiceClient(SERVER_NAME=self.domain.domain).get(path, **{
                'HTTP_' + key.upper().replace('-', '_'): value for key, value in headers.items()})
            pages.append(self.data(response))
        for field in ('core_write_fenced', 'legacy_chat_writers_active', 'legacy_chat_writers',
                      'core_write_fence_blocked_reason'):
            self.assertEqual(pages[0][field], pages[1][field])
        return pages[0]

    def peer_binding(self, role):
        from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token

        token = 'synthetic-fence-' + role
        values = {'peer_base_url': 'https://peer.test.invalid', 'token_in_hash': hash_token(token),
                  'token_out': 'synthetic-outbound-fence'}
        if role == 'upstream':
            binding = PeerUpstreamBinding.objects.create(**values, peer_name='合成上游',
                peer_account='synthetic-account', bound_by_id=self.alice_id, hall_sync_enabled=False)
        else:
            binding = PeerDownstreamBinding.objects.create(**values, account_id=self.bob_id,
                                                           hall_push_enabled=False)
        return binding, token

    @override_settings(TT_CHAT_CORE_BACKEND='service', TT_CHAT_MAINTENANCE=True)
    def test_active_peer_credentials_block_fence_until_revoked_or_user_inactive(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django.utils import timezone

        path = '/api/business/peer-sync/inbound/events/'
        self.assertTrue(self.source_fence()['core_write_fenced'])
        for role in ('upstream', 'downstream'):
            with self.subTest(role=role):
                binding, token = self.peer_binding(role)
                peer = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION='Bearer ' + token)
                self.assertEqual(self.data(peer.post(path, {'events': []}, content_type='application/json'))['applied'], 0)
                page = self.source_fence()
                self.assertFalse(page['core_write_fenced'])
                self.assertTrue(page['legacy_chat_writers_active'])
                self.assertEqual(page['legacy_chat_writers'][role + '_bindings'], 1)
                self.assertEqual(page['core_write_fence_blocked_reason'], 'legacy_peer_chat_active')
                foreign = Client(SERVER_NAME='core-other.test.local', HTTP_AUTHORIZATION='Bearer ' + token)
                self.assertEqual(foreign.post(path, {'events': []}, content_type='application/json').status_code, 401)
                connection.set_tenant(self.tenant)
                uid = binding.bound_by_id if role == 'upstream' else binding.account_id
                get_user_model().objects.filter(pk=uid).update(is_active=False)
                self.assertEqual(peer.post(path, {'events': []}, content_type='application/json').status_code, 401)
                self.assertTrue(self.source_fence()['core_write_fenced'])
                get_user_model().objects.filter(pk=uid).update(is_active=True)
                if role == 'upstream':
                    binding.enabled = False
                    binding.save(update_fields=['enabled'])
                    self.assertTrue(self.source_fence()['core_write_fenced'])
                    binding.enabled, binding.deleted_at = True, timezone.now()
                    binding.save(update_fields=['enabled', 'deleted_at'])
                else:
                    binding.state = 'revoked'
                    binding.save(update_fields=['state'])
                self.assertEqual(peer.post(path, {'events': []}, content_type='application/json').status_code, 401)
                self.assertTrue(self.source_fence()['core_write_fenced'])

    @override_settings(TT_CHAT_CORE_BACKEND='service', TT_CHAT_MAINTENANCE=True)
    def test_new_core_handshake_and_queue_leave_legacy_write_fence_closed(self):
        from unittest.mock import patch
        from uuid import uuid4

        from django.test import Client
        from apps.business.models import Game, Order, PeerDownstreamBinding, PeerSyncEvent
        from apps.business.peer_sync import new_token
        from apps.internal_chat.models import Conversation, OrderGroup
        from tests.p5_auth_helpers import P2_TEST_PASSWORD

        with patch('apps.business.api.peer_views.normalize_base_url', return_value='https://peer.test.invalid'):
            response = Client(SERVER_NAME=self.domain.domain).post('/api/business/peer-sync/bind/', {
                'account': 'core-bob', 'password': P2_TEST_PASSWORD, 'peer_base_url': 'https://peer.test.invalid',
                'token_out': new_token(),
            }, content_type='application/json')
        self.assertTrue(self.data(response)['token_out'])
        binding = PeerDownstreamBinding.objects.get(account_id=self.bob_id)
        PeerSyncEvent.objects.create(
            binding_role='downstream', binding_id=str(binding.pk), direction='out', kind='chat.order_message',
            target_type='chat_core', target_ref=str(uuid4()), idempotency_key=uuid4().hex,
        )
        self.assertTrue(self.source_fence()['core_write_fenced'])
        order = Order.objects.create(order_no='20261121000000000054', game=Game.objects.create(name='旧群反证'))
        OrderGroup.objects.create(conversation=Conversation.objects.create(), order=order,
            order_no=order.order_no, wire_id=uuid4(), binding_role='downstream', binding_id=binding.pk)
        self.assertFalse(self.source_fence()['core_write_fenced'])

    @override_settings(TT_CHAT_MAINTENANCE=True)
    def test_legacy_peer_message_drains_before_service_mode_without_writing_after_it(self):
        from uuid import uuid4
        from django.test import Client
        from django.utils import timezone
        from apps.business.models import Game, Order
        from apps.internal_chat.models import Change, Conversation, GroupMember, Message, OrderGroup

        binding, token = self.peer_binding('downstream')
        order = Order.objects.create(order_no='20261120000000000987', game=Game.objects.create(name='切换合成游戏'))
        group = OrderGroup.objects.create(conversation=Conversation.objects.create(), order=order,
            order_no=order.order_no, wire_id=uuid4(), binding_role='downstream', binding_id=binding.pk)
        GroupMember.objects.create(group=group, wire_key='b:5', name='对端合成客服')
        before = Order.objects.filter(pk=order.pk).values().get()
        page = self.source_fence()
        self.assertFalse(page['core_write_fenced'])
        self.assertTrue(page['legacy_chat_writers_active'])
        message_id = str(uuid4())
        payload = {'message_id': message_id, 'sender': 'b:5', 'sender_name': '对端合成客服',
            'sender_left_at': None, 'text': '保留旧通道消息', 'created_at': timezone.now().isoformat(),
            'revoked': False, 'reply_to': None}
        peer = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION='Bearer ' + token)
        response = self.data(peer.post('/api/business/peer-sync/inbound/events/', {'events': [{
            'kind': 'chat.order_message', 'target_ref': str(group.wire_id), 'payload': payload,
            'idempotency_key': uuid4().hex}]}, content_type='application/json'))
        self.assertEqual((response['applied'], response['skipped']), (1, 0), response)
        message = Message.objects.get(wire_id=message_id)
        self.assertEqual(message.text, '保留旧通道消息')
        self.assertTrue(Change.objects.filter(message=message).exists())
        self.assertEqual(Order.objects.filter(pk=order.pk).values().get(), before)
        self.assertFalse(self.source_fence()['core_write_fenced'])
        service_message_id = str(uuid4())
        with override_settings(TT_CHAT_CORE_BACKEND='service'):
            response = self.data(peer.post('/api/business/peer-sync/inbound/events/', {'events': [{
                'kind': 'chat.order_message', 'target_ref': str(group.wire_id),
                'payload': {**payload, 'message_id': service_message_id},
                'idempotency_key': uuid4().hex}]}, content_type='application/json'))
            self.assertEqual((response['applied'], response['skipped']), (0, 1), response)
            self.assertFalse(self.source_fence()['core_write_fenced'])
        self.assertFalse(Message.objects.filter(wire_id=service_message_id).exists())

    @override_settings(TT_CHAT_CORE_BACKEND='service')
    def test_undrained_peer_chat_events_block_fence_even_with_no_active_binding(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.business.models import PeerSyncEvent
        from apps.internal_chat.order_groups import EVENT_KINDS

        values = {'binding_role': 'upstream', 'binding_id': '987654321', 'direction': 'out',
                  'target_type': 'chat', 'target_ref': 'synthetic-fence', 'payload': {},
                  'idempotency_key': 'synthetic-fence'}
        PeerSyncEvent.objects.create(**values, kind='order.update')
        self.assertTrue(self.source_fence()['core_write_fenced'])
        for kind in sorted(EVENT_KINDS):
            for status in ('pending', 'failed'):
                with self.subTest(kind=kind, status=status):
                    event = PeerSyncEvent.objects.create(**values, kind=kind, status=status,
                        next_attempt_at=timezone.now() + timedelta(days=1))
                    page = self.source_fence()
                    self.assertFalse(page['core_write_fenced'])
                    self.assertEqual(page['legacy_chat_writers']['queued_events'], 1)
                    event.status = 'done'
                    event.save(update_fields=['status'])
                    self.assertTrue(self.source_fence()['core_write_fenced'])
        PeerSyncEvent.objects.create(**{**values, 'direction': 'in'}, kind='chat.order_message')
        page = self.source_fence()
        self.assertTrue(page['core_write_fenced'])
        self.assertFalse(page['legacy_chat_writers_active'])

    def test_core_app_notification_and_unread_use_new_conversation_without_old_lookup(self):
        from datetime import timedelta
        from django.contrib.auth.models import Group
        from django.test import Client
        from django.utils import timezone
        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token
        from apps.business.roles import WORKER

        group, _ = Group.objects.get_or_create(name=WORKER)
        group.user_set.add(self.bob_id)
        raw = 'synthetic-core-app-token'
        AppToken.objects.create(user_id=self.bob_id, key_hash=hash_token(raw), expires_at=timezone.now() + timedelta(minutes=5))
        app = Client(SERVER_NAME=self.domain.domain, HTTP_AUTHORIZATION=f'Bearer {raw}')
        with override_settings(TT_CHAT_CORE_BACKEND='service', TT_CHAT_REALTIME_SECRET='synthetic',
                               TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify'), connection.execute_wrapper(
            self.reject_old_chat,
        ), patch('httpx.post', side_effect=self.remote):
            page = self.data(app.get(BASE + f'app/notification-conversations/{self.cid}/messages/'))
            self.assertEqual(page['peer_name'], 'core-alice')
            self.data(app.post(BASE + f'app/notification-conversations/{self.cid}/read/', {'seq': 1}, content_type='application/json'))
            self.assertEqual(self.data(app.get(BASE + 'app/unread/')), {'unread_count': 1})
        self.assertTrue(all(call['viewer_id'] == self.bob_id for call in self.calls))

    def test_worker_new_private_chat_keeps_directory_restriction_but_existing_chat_is_allowed(self):
        from django.contrib.auth.models import Group
        from apps.business.roles import WORKER

        group, _ = Group.objects.get_or_create(name=WORKER)
        group.user_set.add(self.alice_id)
        nobody = {'conversations': [], 'cursor': 0}
        replies = [nobody, {'conversations': [self.summary], 'cursor': 3}, self.summary]
        captured = []
        def remote(url, *, content, headers, timeout, trust_env):
            captured.append(json.loads(content))
            return httpx.Response(200, json={'code': 0, 'data': replies.pop(0)}, request=httpx.Request('POST', url))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=remote):
            denied = self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json')
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(self.data(self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json'))['id'], self.cid)
        self.assertEqual([call['operation'] for call in captured], ['conversations', 'conversations', 'start'])

    def test_success_envelope_with_missing_message_is_not_reported_as_send_success(self):
        response = httpx.Response(200, json={'code': 0, 'data': {'accepted': True}},
                                  request=httpx.Request('POST', 'http://127.0.0.1/v1/core'))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', return_value=response):
            reply = self.alice.post(BASE + f'conversations/{self.cid}/messages/', {'text': 'missing id', 'client_id': 'no-id'},
                                    content_type='application/json')
        self.assertEqual((reply.status_code, reply.json()['code'], reply.json()['data']), (503, 503, None))

    def test_same_numeric_viewer_and_room_are_signed_for_own_tenant_and_cross_host_session_fails(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from django_tenants.utils import tenant_context
        from chat_service.protocol import verify_request

        with tenant_context(self.other_tenant):
            get_user_model().objects.create_user(pk=self.alice_id, username='core-other-alice', password='synthetic-core-password')
            other = Client(SERVER_NAME='core-other.test.local')
            self.data(other.post('/api/auth/login', {'username': 'core-other-alice', 'password': 'synthetic-core-password'}, content_type='application/json'))
        config = json.loads(self.config_file.read_text())
        config['tenants']['core_bridge_other'] = {'key_id': 'core-b', 'secret': 'synthetic-core-b-key'}
        self.config_file.write_text(json.dumps(config))
        key_ids = []
        def remote(url, *, content, headers, timeout, trust_env):
            key = verify_request({'core-a': 'synthetic-core-a-key', 'core-b': 'synthetic-core-b-key'}, headers,
                                 'POST', '/v1/core', content)
            key_ids.append(key)
            self.assertEqual(json.loads(content)['viewer_id'], self.alice_id)
            result = {'messages': [{**self.message, 'text': '甲独有消息' if key == 'core-a' else '乙独有消息'}],
                      'has_more': False, 'has_newer': False}
            return httpx.Response(200, json={'code': 0, 'data': result}, request=httpx.Request('POST', url))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=remote), tenant_context(self.tenant):
            path = BASE + f'conversations/{self.cid}/messages/'
            self.assertEqual(self.data(self.alice.get(path))['messages'][0]['text'], '甲独有消息')
            self.assertEqual(self.data(other.get(path))['messages'][0]['text'], '乙独有消息')
            config['tenants'][self.tenant.schema_name]['core_backend'] = 'legacy'
            self.config_file.write_text(json.dumps(config))
            self.assertEqual(self.data(self.alice.get(BASE + 'conversations/'))['conversations'], [])
            self.assertEqual(key_ids, ['core-a', 'core-b'])
            self.assertEqual(self.data(other.get(path))['messages'][0]['text'], '乙独有消息')
            self.assertEqual(self.alice.get(path, HTTP_HOST='core-other.test.local').status_code, 401)
        self.assertEqual(key_ids, ['core-a', 'core-b', 'core-b'])

    def test_service_rejects_public_or_reserved_plain_http_config_before_transport(self):
        config = json.loads(self.config_file.read_text())
        for url in ('http://public.invalid', 'http://203.0.113.8', 'http://0.0.0.0'):
            config['service_url'] = url
            self.config_file.write_text(json.dumps(config))
            with self.subTest(url=url), override_settings(TT_CHAT_CORE_BACKEND='service'), patch(
                'httpx.post', side_effect=AssertionError('unsafe URL must not reach transport'),
            ):
                response = self.alice.get(BASE + f'conversations/{self.cid}/messages/')
            self.assertEqual(response.status_code, 503)

    def test_worker_first_send_rechecks_directory_for_previously_created_empty_chat(self):
        from django.contrib.auth.models import Group
        from apps.business.roles import WORKER
        from apps.internal_chat.models import ContactDirectory, Message

        group, _ = Group.objects.get_or_create(name=WORKER)
        group.user_set.add(self.bob_id)
        ContactDirectory.objects.update_or_create(pk=1, defaults={'primary_contact_id': self.alice_id})
        empty = self.data(self.bob.post(BASE + 'conversations/', {'peer_id': self.alice_id}, content_type='application/json'))
        ContactDirectory.objects.filter(pk=1).update(primary_contact_id=None)
        payload = {'text': '目录已变更', 'client_id': 'worker-first'}
        self.assertEqual(self.bob.post(BASE + f'conversations/{empty["id"]}/messages/', payload,
                                      content_type='application/json').status_code, 403)
        self.assertFalse(Message.objects.filter(conversation_id=empty['id']).exists())
        calls, populated = [], [False]
        def remote(url, *, content, headers, timeout, trust_env):
            body = json.loads(content)
            calls.append(body)
            if body['operation'] == 'order':
                result = {**self.summary, 'peer': {'id': self.alice_id, 'name': 'core-alice', 'avatar_url': None},
                          'last_message': self.message if populated[0] else None}
            elif body['operation'] == 'send':
                result = self.message
            else:
                raise AssertionError(body['operation'])
            return httpx.Response(200, json={'code': 0, 'data': result}, request=httpx.Request('POST', url))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=remote):
            denied = self.bob.post(BASE + f'conversations/{self.cid}/messages/', payload, content_type='application/json')
            self.assertEqual(denied.status_code, 403)
            self.assertEqual([call['operation'] for call in calls], ['order'])
            populated[0] = True
            self.data(self.bob.post(BASE + f'conversations/{self.cid}/messages/', payload, content_type='application/json'))
        self.assertEqual([call['operation'] for call in calls], ['order', 'order', 'send'])

    def test_selected_legacy_send_cannot_write_after_source_reports_service_fence(self):
        from apps.internal_chat import service
        from apps.internal_chat.models import Change, ChangeCursor, Message
        from tests.test_chat_service_bridge import ServiceClient
        from chat_service.protocol import sign_request

        conversation = self.data(self.alice.post(BASE + 'conversations/', {'peer_id': self.bob_id}, content_type='application/json'))
        path = BASE + f'conversations/{conversation["id"]}/messages/'
        self.data(self.alice.post(path, {'text': '切换前已落账', 'client_id': 'before-fence'}, content_type='application/json'))
        before = (Message.objects.count(), Change.objects.count(), ChangeCursor.objects.get(pk=1).latest)
        config = json.loads(self.config_file.read_text())
        config['tenants'][self.tenant.schema_name]['core_backend'] = 'legacy'
        self.config_file.write_text(json.dumps(config))
        original = service._legacy_send
        fence = []
        def delayed(*args, **kwargs):
            config['tenants'][self.tenant.schema_name]['core_backend'] = 'service'
            self.config_file.write_text(json.dumps(config))
            source_path = BASE + 'service/export/'
            headers = sign_request('synthetic-core-a-key', 'core-a', 'GET', source_path, b'')
            response = ServiceClient(SERVER_NAME=self.domain.domain).get(source_path, **{
                'HTTP_' + key.upper().replace('-', '_'): value for key, value in headers.items()})
            fence.append(self.data(response))
            return original(*args, **kwargs)
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch(
            'apps.internal_chat.service._legacy_send', side_effect=delayed,
        ), patch('httpx.post', side_effect=AssertionError('selected legacy request must fail, not fall back')):
            denied = self.alice.post(path, {'text': '不得晚写旧库', 'client_id': 'after-fence'}, content_type='application/json')
        self.assertTrue(fence[0]['core_write_fenced'])
        self.assertEqual(denied.status_code, 503)
        self.assertEqual((Message.objects.count(), Change.objects.count(), ChangeCursor.objects.get(pk=1).latest), before)

    def test_source_fence_and_latest_are_observed_after_old_writer_serialization_lock(self):
        from tests.test_chat_service_bridge import ServiceClient
        from chat_service.protocol import sign_request

        config = json.loads(self.config_file.read_text())
        for suffix in ('service/export/', 'service/snapshot/?kind=messages'):
            config['tenants'][self.tenant.schema_name]['core_backend'] = 'legacy'
            self.config_file.write_text(json.dumps(config))
            changed = []
            def switch_at_lock(execute, sql, params, many, context):
                if not changed and 'internal_chat_changecursor' in sql and 'FOR UPDATE' in sql:
                    config['tenants'][self.tenant.schema_name]['core_backend'] = 'service'
                    self.config_file.write_text(json.dumps(config))
                    changed.append(True)
                return execute(sql, params, many, context)
            source_path = BASE + suffix
            headers = sign_request('synthetic-core-a-key', 'core-a', 'GET', source_path, b'')
            with override_settings(TT_CHAT_CORE_BACKEND='service'), connection.execute_wrapper(switch_at_lock):
                response = ServiceClient(SERVER_NAME=self.domain.domain).get(source_path, **{
                    'HTTP_' + key.upper().replace('-', '_'): value for key, value in headers.items()})
            self.assertTrue(self.data(response)['core_write_fenced'])
            self.assertEqual(changed, [True])

    def test_worker_first_send_rejects_incomplete_core_conversation_response(self):
        from django.contrib.auth.models import Group
        from apps.business.roles import WORKER

        group, _ = Group.objects.get_or_create(name=WORKER)
        group.user_set.add(self.bob_id)
        reply = httpx.Response(200, json={'code': 0, 'data': {'id': self.cid}},
                               request=httpx.Request('POST', 'http://127.0.0.1/v1/core'))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', return_value=reply):
            response = self.bob.post(BASE + f'conversations/{self.cid}/messages/',
                                     {'text': '缺少会话状态', 'client_id': 'bad-conversation'}, content_type='application/json')
        self.assertEqual((response.status_code, response.json()['code'], response.json()['data']), (503, 503, None))

    def use_server(self, server):
        config = json.loads(self.config_file.read_text())
        config['service_url'] = server.url
        self.config_file.write_text(json.dumps(config))


    def test_busy_core_retries_same_signed_request_before_returning_list_to_browser(self):
        count = [0]
        def remote(body):
            self.assertEqual(body['operation'], 'conversations')
            count[0] += 1
            if count[0] <= 2:
                return 503, None, 'Tenant synchronization is busy'
            return 200, {'conversations': [self.summary], 'cursor': 3}
        with _SignedCoreServer(remote) as server, override_settings(TT_CHAT_CORE_BACKEND='service'):
            self.use_server(server)
            result = self.data(self.alice.get(BASE + 'conversations/'))
            self.assertEqual(result['conversations'][0]['id'], self.cid)
            self.assertFalse(server.errors, server.errors)
            self.assertEqual(len(server.requests), 3)
            self.assertEqual(server.requests[0], server.requests[1])
            self.assertEqual(server.requests[1], server.requests[2])

    def test_persistent_busy_core_has_one_shared_five_second_budget(self):
        from time import monotonic
        attempts = []
        def remote(url, *, content, headers, timeout, trust_env):
            attempts.append((content, headers, timeout))
            return httpx.Response(503, json={'code': 503, 'msg': 'Tenant synchronization is busy', 'data': None},
                                  request=httpx.Request('POST', url))
        began = monotonic()
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=remote):
            response = self.alice.get(BASE + 'conversations/')
        elapsed = monotonic() - began
        self.assertEqual(response.status_code, 503, response.content)
        self.assertGreater(elapsed, 4.9)
        self.assertLess(elapsed, 6.5)
        self.assertGreater(len(attempts), 2)
        self.assertTrue(all(0 < request[2] <= 5 for request in attempts))
        self.assertGreater(attempts[0][2], attempts[-1][2])
        self.assertTrue(all(request[:2] == attempts[0][:2] for request in attempts))

    def test_core_success_arriving_after_the_shared_budget_is_rejected(self):
        reply = httpx.Response(200, json={'code': 0, 'data': {'conversations': [], 'cursor': 0}},
                               request=httpx.Request('POST', 'http://127.0.0.1/v1/core'))
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch(
            'apps.internal_chat.core_bridge.monotonic', side_effect=[100, 100, 105.01],
        ), patch('httpx.post', return_value=reply) as post:
            response = self.alice.get(BASE + 'conversations/')
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['timeout'], 5)

    def test_core_dependency_and_business_errors_are_not_retried(self):
        failures = [(503, 503, 'Other dependency is unavailable', 503),
                    (503, 500, 'Tenant synchronization is busy', 503),
                    (503, 503, 'Tenant synchronization is busy ', 503),
                    (200, 503, 'Tenant synchronization is busy', 503),
                    (400, 400, 'Tenant synchronization is busy', 200),
                    (403, 403, 'Tenant synchronization is busy', 403),
                    (409, 409, 'Tenant synchronization is busy', 503)]
        for http_status, code, message, expected in failures:
            with self.subTest(http_status=http_status, code=code, message=message):
                reply = httpx.Response(http_status, json={'code': code, 'msg': message, 'data': None},
                                       request=httpx.Request('POST', 'http://127.0.0.1/v1/core'))
                with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', return_value=reply) as post:
                    response = self.alice.get(BASE + 'conversations/')
                self.assertEqual(response.status_code, expected, response.content)
                self.assertEqual(post.call_count, 1)
        with override_settings(TT_CHAT_CORE_BACKEND='service'), patch('httpx.post', side_effect=httpx.ConnectError('offline')) as post:
            self.assertEqual(self.alice.get(BASE + 'conversations/').status_code, 503)
            self.assertEqual(post.call_count, 1)


    def test_core_conversation_pagination_is_validated_and_forwarded_without_source_roster_rpc(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'), connection.execute_wrapper(self.reject_old_chat), patch('httpx.post',side_effect=self.remote):
            self.data(self.alice.get(BASE+'conversations/',{'limit':25,'offset':50}))
            self.assertEqual(self.calls[-1]['query'],{'limit':25,'offset':50})
            self.data(self.alice.get(BASE+'conversations/'))
            self.assertEqual(self.calls[-1]['query'],{})
        self.assertEqual([call['operation'] for call in self.calls],['conversations','conversations'])

    def test_core_conversation_pagination_rejects_invalid_duplicate_and_unknown_values(self):
        with override_settings(TT_CHAT_CORE_BACKEND='service'),patch('httpx.post',side_effect=AssertionError('invalid pagination must not reach core')):
            for query in ('limit=0','limit=101','limit=1.5','offset=-1','offset=9223372036854775808','limit=1&limit=2','offset=0&offset=1','viewer_id=99'):
                with self.subTest(query=query):
                    response=self.alice.get(BASE+'conversations/?'+query)
                    self.assertEqual(response.json()['code'],1000,response.content)

    def test_legacy_conversation_parameters_keep_existing_ignored_query_contract(self):
        self.data(self.alice.post(BASE+'conversations/',{'peer_id':self.bob_id},content_type='application/json'))
        with patch('httpx.post',side_effect=AssertionError('legacy must not call core')):
            result=self.data(self.alice.get(BASE+'conversations/?limit=0&offset=-1&unknown=yes'))
        self.assertEqual(len(result['conversations']),1)

    def test_normal_session_unread_uses_aggregate_without_loading_conversation_dtos(self):
        from django.test import Client
        with override_settings(TT_CHAT_CORE_BACKEND='service'),connection.execute_wrapper(self.reject_old_chat),patch('httpx.post',side_effect=self.remote):
            response=self.alice.get(BASE+'unread/')
        self.assertEqual(self.data(response),{'unread_count':1})
        self.assertEqual([call['operation'] for call in self.calls],['unread'])
        self.assertIn('no-store',response['Cache-Control'])
        self.assertEqual(Client(SERVER_NAME=self.domain.domain).get(BASE+'unread/').status_code,401)

    def test_normal_session_unread_preserves_legacy_unread_calculation(self):
        cid=self.data(self.alice.post(BASE+'conversations/',{'peer_id':self.bob_id},content_type='application/json'))['id']
        self.data(self.alice.post(BASE+f'conversations/{cid}/messages/',{'text':'合成未读','client_id':'unread-session'},content_type='application/json'))
        self.assertEqual(self.data(self.bob.get(BASE+'unread/')),{'unread_count':1})


    def test_browser_page_user_header_rejects_stale_cookie_account_before_changing_chat(self):
        from apps.internal_chat.models import Conversation

        path = BASE + 'conversations/'
        initial_count = Conversation.objects.count()
        for stale_user in (str(self.alice_id), '', f'00{self.bob_id}'):
            response = self.bob.post(path, {'peer_id': self.alice_id}, content_type='application/json',
                                     HTTP_X_CHAT_PAGE_USER=stale_user)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(Conversation.objects.count(), initial_count)

        response = self.bob.post(path, {'peer_id': self.alice_id}, content_type='application/json',
                                 HTTP_X_CHAT_PAGE_USER=str(self.bob_id))
        conversation = self.data(response)['id']
        self.assertEqual(Conversation.objects.count(), initial_count + 1)
        self.assertEqual(self.data(self.alice.get(path))['conversations'][0]['id'], conversation)

    def native_chat_client(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        from django.test import Client
        from django.utils import timezone
        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token
        from apps.business.roles import WORKER
        with patch('apps.internal_chat.realtime._notify'):
            get_user_model().objects.get(pk=self.bob_id).groups.add(Group.objects.get_or_create(name=WORKER)[0])
        raw='synthetic-unread-app-token'
        token=AppToken.objects.create(user_id=self.bob_id,key_hash=hash_token(raw),expires_at=timezone.now()+timedelta(minutes=5))
        app=Client(SERVER_NAME=self.domain.domain,HTTP_AUTHORIZATION=f'Bearer {raw}')
        grant=self.data(app.post(BASE+'app/access/'))['grant']
        return Client(SERVER_NAME=self.domain.domain,HTTP_AUTHORIZATION=f'Chat {grant}'),token

    @override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify')
    def test_unread_accepts_same_real_app_chat_grant_as_conversations(self):
        chat,_=self.native_chat_client()
        with connection.execute_wrapper(self.reject_old_chat),patch('httpx.post',side_effect=self.remote):
            self.data(chat.get(BASE+'conversations/'))
            result=chat.get(BASE+'unread/', HTTP_X_CHAT_PAGE_USER=str(self.alice_id))
        self.assertEqual(self.data(result),{'unread_count':1})
        self.assertIn('no-store',result['Cache-Control'])
        self.assertEqual([call['operation'] for call in self.calls],['conversations','unread'])
        self.assertTrue(all(call['viewer_id']==self.bob_id for call in self.calls))

    @override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify')
    def test_unread_rejects_expired_revoked_and_invalid_app_chat_grants_before_core(self):
        from django.utils import timezone
        chat,token=self.native_chat_client()
        with patch('httpx.post',side_effect=AssertionError('invalid App grant must not reach core')):
            with patch('apps.internal_chat.app_access._GRANT_LIFETIME',-1):
                self.assertEqual(chat.get(BASE+'unread/').status_code,401)
            self.assertEqual(self.alice.get(BASE+'unread/',HTTP_AUTHORIZATION='Chat synthetic-invalid').status_code,401)
            token.revoked_at=timezone.now()
            token.save(update_fields=['revoked_at'])
            self.assertEqual(chat.get(BASE+'unread/').status_code,401)

    @override_settings(TT_CHAT_CORE_BACKEND='service',TT_CHAT_REALTIME_SECRET='synthetic',TT_CHAT_REALTIME_NOTIFY_URL='http://127.0.0.1:4194/notify')
    def test_unread_app_chat_grant_cannot_cross_tenant_hosts(self):
        from django_tenants.utils import tenant_context
        chat,_=self.native_chat_client()
        with patch('httpx.post',side_effect=self.remote):
            self.assertEqual(self.data(chat.get(BASE+'unread/')),{'unread_count':1})
            with tenant_context(self.tenant):
                self.assertEqual(chat.get(BASE+'unread/',HTTP_HOST='core-other.test.local').status_code,401)
        self.assertEqual([call['operation'] for call in self.calls],['unread'])


    def test_exact_signed_core_join_required_conflict_reaches_the_browser_without_retry(self):
        def remote(body):
            self.assertEqual(body['operation'],'send')
            self.assertEqual(body['viewer_id'],self.alice_id)
            return 409,None,'请先加入会话'
        with _SignedCoreServer(remote) as server,override_settings(TT_CHAT_CORE_BACKEND='service'):
            self.use_server(server)
            result=self.alice.post(BASE+f'conversations/{self.cid}/messages/',
                {'text':'合成入群并发','client_id':'stable-join-race'},content_type='application/json')
            self.assertEqual((result.status_code,result.json()),(409,{'code':409,'msg':'请先加入会话','data':None}))
            self.assertEqual(len(server.requests),1)
            self.assertFalse(server.errors,server.errors)

    def test_other_conflict_envelopes_cannot_trigger_the_join_required_recovery(self):
        from chat_service.protocol import verify_request
        cases=[(200,409,'请先加入会话'),(409,409,'different conflict'),(409,0,'请先加入会话'),
               (503,409,'请先加入会话')]
        for status,code,message in cases:
            calls=[]
            def remote(url,**kwargs):
                verify_request({'core-a':'synthetic-core-a-key'},kwargs['headers'],'POST','/v1/core',kwargs['content'])
                calls.append(json.loads(kwargs['content']))
                return httpx.Response(status,json={'code':code,'msg':message,'data':None},request=httpx.Request('POST',url))
            with self.subTest(status=status,code=code,message=message),override_settings(TT_CHAT_CORE_BACKEND='service'),connection.execute_wrapper(self.reject_old_chat),patch('httpx.post',side_effect=remote):
                result=self.alice.post(BASE+f'conversations/{self.cid}/messages/',
                    {'text':'合成拒绝伪装','client_id':'same-request-key'},content_type='application/json')
            self.assertEqual((result.status_code,result.json()),(503,{'code':503,'msg':'内部沟通暂时不可用','data':None}))
            self.assertEqual(len(calls),1)


class _SignedCoreServer:
    def __init__(self, respond):
        self.respond, self.errors, self.requests = respond, [], []

    def __enter__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from chat_service.protocol import verify_request
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                try:
                    verify_request({'core-a': 'synthetic-core-a-key'}, dict(self.headers), 'POST', '/v1/core', raw)
                    owner.requests.append((raw, dict(self.headers)))
                    status, data, *message = owner.respond(json.loads(raw))
                    body = json.dumps({'code': 0 if status == 200 else status, 'msg': message[0] if message else '',
                                       'data': data}).encode()
                except Exception as exc:
                    owner.errors.append(repr(exc))
                    status, body = 500, b'{"code":500,"data":null}'
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                finally:
                    from django.db import connections
                    connections.close_all()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        from django.db import connections
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=4)
        connections.close_all()
