from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, Order
from apps.wecom.archive_views import ARCHIVE_VIEWER_SESSION_KEY
from apps.wecom.models import WecomAppConfig
from tests.p5_auth_helpers import make_logged_in_client


class ArchiveAuthTests(TenantTestCase):
    def setUp(self):
        cache.clear()
        self.config = WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id='synth-corp',
            agent_id=42, secret='synth-secret', identity_revision=2,
        )
        self.order = Order.objects.create(
            game=Game.objects.create(name='合成游戏'), order_no='20261114000000000001', source_order_no='SYN-001',
        )
        self.client = make_logged_in_client(username='archive-kf', groups=('客服',), nodes=('order.view_detail',))
        self.user_id = self.client.session['_auth_user_id']
        settings = override_settings(
            WECOM_ARCHIVE_TENANT_SCHEMA=connection.schema_name, WECOM_ARCHIVE_PROGRAM_ID='synth-program',
            WECOM_ARCHIVE_VIEWERS={self.user_id: 'synth-viewer'},
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.base = f'/api/wecom/orders/{self.order.order_no}/chat/'
        self.page = 'https://tenant.test.com/v2/order-manage'
        self.remote = mock.MagicMock()
        self.remote.jsapi.get_ticket.return_value = {'errcode': 0, 'ticket': 'corp-ticket', 'expires_in': 7200}
        self.remote.jsapi.get_agent_ticket.return_value = {'errcode': 0, 'ticket': 'agent-ticket', 'expires_in': 7200}
        self.remote.get.return_value = {'errcode': 0, 'userid': 'synth-viewer'}

    def sign(self, url=None, *, base=None, client=None, reauthenticate=False):
        current = client or self.client
        return current.get((base or self.base) + 'signature/', {
            'url': url or self.page, 'reauthenticate': str(reauthenticate).lower(),
        })

    def login(self, state, *, base=None, client=None):
        current = client or self.client
        return current.post((base or self.base) + 'login/', {
            'code': 'synth-code', 'state': state,
        }, content_type='application/json')

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_order_login_accepts_unbound_employee_and_reuses_only_order_identity(self, get_client):
        get_client.return_value = self.remote
        self.remote.get.return_value = {'errcode': 0, 'userid': 'other-staff'}
        from apps.wecom.archive_order_session import ORDER_ARCHIVE_VIEWER_SESSION_KEY

        with override_settings(WECOM_ARCHIVE_VIEWERS={}):
            response = self.sign()
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotIn('agent-ticket', response.content.decode())
            signed = response.json()['data']
            self.assertFalse(signed['verified'])
            self.assertEqual(self.login(signed['login_state']).json()['data'], {'verified': True})
            self.remote.get.assert_called_once_with('auth/getuserinfo', params={'code': 'synth-code'})
            self.assertTrue(self.sign().json()['data']['verified'])
            identity = self.client.session[ORDER_ARCHIVE_VIEWER_SESSION_KEY]
            self.assertEqual(identity['wecom_userid'], 'other-staff')
            self.assertNotIn(ARCHIVE_VIEWER_SESSION_KEY, self.client.session)
            unavailable = self.sign(base='/api/wecom/archive/').json()
            self.assertNotEqual(unavailable['code'], 0)

        # An order session cannot confer standalone access even if the account is mapped later.
        self.assertFalse(self.sign(base='/api/wecom/archive/').json()['data']['verified'])
        renewed = self.sign(reauthenticate=True).json()['data']
        self.assertFalse(renewed['verified'])
        self.assertNotEqual(renewed['login_state'], signed['login_state'])
        self.assertNotEqual(self.login(signed['login_state']).json()['code'], 0)
        self.remote.get.assert_called_once()

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_standalone_requires_mapped_employee_and_order_can_reuse_valid_session(self, get_client):
        get_client.return_value = self.remote
        standalone = '/api/wecom/archive/'
        order_state = self.sign().json()['data']['login_state']
        state = self.sign(base=standalone).json()['data']['login_state']
        self.assertNotEqual(self.login(order_state, base=standalone).json()['code'], 0)
        self.assertNotEqual(self.login(state).json()['code'], 0)
        self.assertNotEqual(self.login('a' * 48, base=standalone).json()['code'], 0)
        self.remote.get.assert_not_called()

        self.remote.get.return_value = {'errcode': 0, 'userid': 'other-staff'}
        self.assertNotEqual(self.login(state, base=standalone).json()['code'], 0)
        self.assertNotIn(ARCHIVE_VIEWER_SESSION_KEY, self.client.session)

        self.remote.get.return_value = {'errcode': 0, 'userid': 'synth-viewer'}
        state = self.sign(base=standalone).json()['data']['login_state']
        self.assertEqual(self.login(state, base=standalone).json()['data'], {'verified': True})
        self.assertTrue(self.sign(base=standalone).json()['data']['verified'])
        self.assertTrue(self.sign().json()['data']['verified'])

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_order_login_rejects_external_and_interconnected_enterprise_user(self, get_client):
        get_client.return_value = self.remote
        with override_settings(WECOM_ARCHIVE_VIEWERS={}):
            for result in ({'errcode': 0, 'openid': 'synthetic-external'},
                           {'errcode': 0, 'userid': 'other-corp/other-staff'},
                           {'errcode': 40029, 'userid': 'synth-viewer'}):
                self.remote.get.return_value = result
                state = self.sign().json()['data']['login_state']
                self.assertNotEqual(self.login(state).json()['code'], 0)
                self.assertFalse(self.sign().json()['data']['verified'])

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_order_login_and_signature_require_visible_order_and_configured_tenant(self, get_client):
        outside_game = make_logged_in_client(username='archive-outside-game', groups=('客服',),
                                             nodes=('order.view_detail',), games=[])
        with override_settings(WECOM_ARCHIVE_VIEWERS={}):
            self.assertNotEqual(self.sign(client=outside_game).json()['code'], 0)
            self.assertNotEqual(self.login('a' * 48, client=outside_game).json()['code'], 0)
        with override_settings(WECOM_ARCHIVE_TENANT_SCHEMA='another-studio'):
            self.assertNotEqual(self.sign().json()['code'], 0)
            self.assertNotEqual(self.login('a' * 48).json()['code'], 0)
        get_client.assert_not_called()

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_order_native_login_session_cannot_be_reused_after_identity_context_changes(self, get_client):
        get_client.return_value = self.remote
        from apps.wecom.archive_order_session import ORDER_ARCHIVE_VIEWER_SESSION_KEY

        with override_settings(WECOM_ARCHIVE_VIEWERS={}):
            state = self.sign().json()['data']['login_state']
            self.assertEqual(self.login(state).json()['data'], {'verified': True})
            original = self.client.session[ORDER_ARCHIVE_VIEWER_SESSION_KEY]
            for field, value in (
                ('tenant_schema', 'another-studio'), ('corp_id', 'other-corp'), ('user_id', '-1'),
                ('agent_id', 99), ('identity_revision', 999), ('expires_at', 1),
            ):
                session = self.client.session
                session[ORDER_ARCHIVE_VIEWER_SESSION_KEY] = {**original, field: value}
                session.save()
                self.assertFalse(self.sign().json()['data']['verified'], field)

    @mock.patch('apps.wecom.archive_signatures.sdk.get_client')
    def test_url_must_be_tenant_https_v2_page(self, get_client):
        for url in ('https://other.test.com/v2/order-manage', 'https://tenant.test.com@other.test.com/v2/',
                    'https://tenant.test.com/admin', self.page + '#partial', 'http://tenant.test.com/v2/order-manage'):
            self.assertNotEqual(self.sign(url).json()['code'], 0)
        get_client.assert_not_called()
