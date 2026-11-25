from hashlib import sha1
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.platform.provisioning import provision_tenant
from apps.wecom.models import WecomAppConfig, WecomGroupChat
from tests.p5_auth_helpers import make_logged_in_client


class ClientLabTests(TenantTestCase):
    def setUp(self):
        cache.clear()
        self.config = WecomAppConfig.objects.create(tenant_schema=connection.schema_name, enabled=True, corp_id='corp-test', agent_id=42, secret='secret-test')
        self.client = make_logged_in_client(username='lab-admin', is_superuser=True)
        self.url = 'https://tenant.test.com/wecom-client-lab'
        self.remote = MagicMock()
        self.remote.jsapi.get_ticket.return_value = {'errcode': 0, 'ticket': 'corp-ticket', 'expires_in': 7200}
        self.remote.jsapi.get_agent_ticket.return_value = {'errcode': 0, 'ticket': 'agent-ticket', 'expires_in': 7200}

    def test_signatures_and_cached_tickets_do_not_expose_secrets(self):
        with patch('apps.wecom.client_lab.sdk.get_client', return_value=self.remote):
            for _ in range(2):
                result = self.client.get('/api/wecom/client-lab/signature/', {'url': self.url}).json()
                self.assertEqual(result['code'], 0)
                data = result['data']
                for ticket, key in [('corp-ticket', 'corp_signature'), ('agent-ticket', 'agent_signature')]:
                    text = f"jsapi_ticket={ticket}&noncestr={data['nonce']}&timestamp={data['timestamp']}&url={self.url}"
                    self.assertEqual(data[key], sha1(text.encode()).hexdigest())
                self.assertNotIn('secret-test', str(result))
                self.assertNotIn('ticket', str(result))
        self.remote.jsapi.get_ticket.assert_called_once()
        self.remote.jsapi.get_agent_ticket.assert_called_once()

    def test_signature_rejects_other_page_and_failures_do_not_leak(self):
        with patch('apps.wecom.client_lab.sdk.get_client', return_value=self.remote) as factory:
            for url in ['https://evil.example/wecom-client-lab', 'https://tenant.test.com/other', self.url + '?x=1', self.url + '#x']:
                self.assertNotEqual(self.client.get('/api/wecom/client-lab/signature/', {'url': url}).json()['code'], 0)
            factory.assert_not_called()
            self.remote.jsapi.get_ticket.side_effect = RuntimeError('secret-test')
            result = self.client.get('/api/wecom/client-lab/signature/', {'url': self.url}).json()
            self.assertNotEqual(result['code'], 0)
            self.assertNotIn('secret-test', str(result))

    def test_requires_account_admin(self):
        denied = make_logged_in_client(username='lab-nonadmin', groups=('客服',), nodes=('system.wecom_settings',))
        for path, params in [('/api/wecom/client-lab/signature/', {'url': self.url}), ('/api/wecom/client-lab/groups/', {'name': 'group'})]:
            response = denied.get(path, params)
            self.assertTrue(response.status_code in (401, 403) or response.json()['code'] != 0)

    def test_exact_group_match_and_tenant_isolation(self):
        WecomGroupChat.objects.create(chat_id='own-id', name='group-a')
        WecomGroupChat.objects.create(chat_id='partial-id', name='group-a extra')
        WecomGroupChat.objects.create(chat_id='old-id', name='group-a', identity_revision=2)
        other, _ = provision_tenant(schema_name='lab_other', name='合成乙', domain='lab-other.test.com')
        with tenant_context(other):
            WecomAppConfig.objects.create(tenant_schema=other.schema_name, enabled=True, corp_id='corp-test', agent_id=42, secret='secret-test')
            WecomGroupChat.objects.create(chat_id='other-id', name='group-a')
        rows = self.client.get('/api/wecom/client-lab/groups/', {'name': 'group-a'}).json()['data']['groups']
        self.assertEqual([row['chat_id'] for row in rows], ['own-id'])
        try:
            response = self.client.get('/api/wecom/client-lab/groups/', {'name': 'group-a'}, HTTP_HOST='lab-other.test.com')
            if response.status_code == 200:
                self.assertNotEqual(response.json()['code'], 0)
            else:
                self.assertIn(response.status_code, (401, 403, 404))
        finally:
            connection.set_tenant(self.tenant)
