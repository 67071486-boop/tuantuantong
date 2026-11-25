import json
import hashlib
import hmac
import uuid
from unittest.mock import Mock, patch

import httpx
from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.alipay_auth_control import (
    AuthorizationControlClient, ControlIdentityIncomplete, ControlIdentityUnverified,
    ControlOutcomeUnknown, ControlResult, ControlUnavailable,
)
from apps.business.models import AlipaySettlementAuthorization
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/alipay-authorizations/"


class AuthorizationTests(TenantTestCase):
    def setUp(self):
        self.key_override = override_settings(ALIPAY_AUTH_DATA_KEY=Fernet.generate_key().decode())
        self.key_override.enable()
        self.addCleanup(self.key_override.disable)
        self.client = make_logged_in_client(is_superuser=True)
        self.request_id = str(uuid.uuid4())
        self.body = {"request_id": self.request_id, "account": "13800001234", "real_name": "测试姓名"}

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_unavailable_service_preserves_retryable_record_and_audit(self):
        response = self.post(URL, self.body)
        data = response.json()["data"]
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["last_error"], "授权服务暂不可用，请稍后重试")
        self.assertEqual(data["account_masked"], "138****1234")
        self.assertEqual(data["real_name"], "测***")
        self.assertIsNone(data["qr_png_base64"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(AlipaySettlementAuthorization.objects.filter(request_id=self.request_id).exists())
        row = AlipaySettlementAuthorization.objects.get(request_id=self.request_id)
        self.assertNotIn(self.body["account"], row.account_ciphertext)
        self.assertNotIn(self.body["real_name"], row.real_name_ciphertext)
        self.assertTrue(OperationLog.objects.filter(action="finance.alipay_authorization").exists())
        self.assertEqual(self.post(URL, self.body).json()["data"]["id"], data["id"])
        self.assertEqual(AlipaySettlementAuthorization.objects.count(), 1)

    def test_broker_results_are_verified_before_authorized(self):
        mock = Mock()
        mock.create.return_value = ControlResult("pending", "https://example.com/authorize")
        mock.refresh.side_effect = [ControlResult("authorized", configured=False), ControlResult("authorized", configured=True)]
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            response = self.post(URL, self.body)
            data = response.json()["data"]
            self.assertTrue(data["qr_png_base64"].startswith("iVBOR"))
            row = AlipaySettlementAuthorization.objects.get(pk=data["id"])
            self.assertFalse(hasattr(row, "authorization_url"))
            path = f"{URL}{data['id']}/refresh/"
            self.assertEqual(self.post(path, {}).json()["data"]["status"], "pending")
            self.assertEqual(self.post(path, {}).json()["data"]["status"], "authorized")
            mock.create.assert_called_once_with(self.request_id, self.body["account"], self.body["real_name"])

    def test_request_id_conflict_and_foreign_identifiers_rejected(self):
        self.post(URL, self.body)
        response = self.post(URL, {**self.body, "real_name": "另一个人"})
        self.assertNotEqual(response.json()["code"], 0)
        response = self.post(URL, {**self.body, "tenant": "other"})
        self.assertNotEqual(response.json()["code"], 0)
        self.assertEqual(AlipaySettlementAuthorization.objects.count(), 1)

    def test_credential_and_external_exception_are_not_exposed(self):
        mock = Mock()
        mock.create.side_effect = ControlUnavailable("sensitive-secret")
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            response = self.post(URL, self.body)
        self.assertNotIn("sensitive-secret", response.content.decode())
        self.assertNotIn("13800001234", response.content.decode())

    def test_unknown_result_tells_user_to_contact_platform(self):
        mock = Mock()
        mock.create.side_effect = ControlOutcomeUnknown("上游敏感原文")
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            response = self.post(URL, self.body)
        data = response.json()["data"]
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["last_error"], "操作结果待平台核对，请勿重复操作")
        self.assertNotIn("上游敏感原文", response.content.decode())
        self.assertTrue(data["requires_platform_review"])

    def test_incomplete_identity_blocks_rescan_until_refresh_recovers(self):
        mock = Mock()
        mock.create.side_effect = ControlIdentityIncomplete("敏感信息")
        mock.refresh.side_effect = [ControlUnavailable(), ControlResult("authorized", configured=True)]
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["last_error"], "付款身份信息不完整，请联系平台处理")
            self.assertTrue(first["requires_platform_review"])
            self.assertIsNone(first["next_action"])
            self.assertEqual(self.post(f"{URL}{first['id']}/authorize/", {}).json()["data"]["last_error"], first["last_error"])
            self.assertEqual(mock.create.call_count, 1)
            mock.authorize.assert_not_called()
            self.assertEqual(self.post(URL, self.body).json()["data"]["id"], first["id"])
            self.assertEqual(mock.create.call_count, 1)
            retry = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
            self.assertTrue(retry["requires_platform_review"])
            self.assertEqual(retry["last_error"], first["last_error"])
            recovered = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
        self.assertFalse(recovered["requires_platform_review"])
        self.assertEqual(recovered["status"], "authorized")
        self.assertIsNone(recovered["next_action"])

    def test_unverified_identity_removes_old_authorized_status(self):
        mock = Mock()
        mock.create.return_value = ControlResult("authorized", configured=True)
        mock.refresh.side_effect = ControlIdentityUnverified()
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            result = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["requires_platform_review"])
        self.assertIsNone(result["authorized_at"])
        self.assertEqual(result["last_error"], "无法确认付款身份是否匹配，请联系平台处理")

    def test_timeout_retry_uses_same_request_id(self):
        client = AuthorizationControlClient("test-key", base_url="https://service.example", client_id="tenant-a")
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=client), \
             patch("apps.business.alipay_auth_control.httpx.post", side_effect=httpx.ReadTimeout("test")) as post:
            first = self.post(URL, self.body).json()["data"]
            again = self.post(URL, self.body).json()["data"]
            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["last_error"], "授权服务暂不可用，请稍后重试")
            self.assertEqual(first["request_id"], self.request_id)
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(post.call_count, 2)
            self.assertEqual(json.loads(post.call_args_list[0].kwargs["content"])["request_id"], self.request_id)
            self.assertEqual(json.loads(post.call_args_list[1].kwargs["content"])["request_id"], self.request_id)
            self.post(f"{URL}{first['id']}/refresh/", {})
            self.assertEqual(post.call_args.args[0], f"https://service.example/authorizations/{self.request_id}/refresh")

    def test_retry_after_response_lost_uses_same_central_record(self):
        class Control:
            records = {}

            def create(self, request_id, account, real_name):
                if request_id not in self.records:
                    self.records[request_id] = (account, real_name)
                    raise ControlUnavailable()
                return ControlResult("pending", "https://example.com/resume")

        control = Control()
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=control):
            first = self.post(URL, self.body).json()["data"]
            second = self.post(URL, self.body).json()["data"]
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["qr_png_base64"].startswith("iVBOR"))
        self.assertEqual(len(control.records), 1)

    def test_retry_when_first_request_did_not_reach_control(self):
        mock = Mock()
        mock.create.side_effect = [ControlUnavailable(), ControlResult("pending", "https://example.com/retry")]
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            second = self.post(URL, self.body).json()["data"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(mock.create.call_count, 2)
        self.assertEqual(mock.create.call_args.args[0], self.request_id)
        self.assertTrue(second["qr_png_base64"].startswith("iVBOR"))

    def test_unavailable_refresh_preserves_last_confirmed_authorization(self):
        mock = Mock()
        mock.create.return_value = ControlResult("authorized", configured=True)
        mock.refresh.side_effect = ControlUnavailable()
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            updated = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
        self.assertEqual(updated["status"], "authorized")
        self.assertIsNone(first["authorized_at"])
        self.assertEqual(updated["authorized_at"], first["authorized_at"])
        self.assertEqual(updated["last_error"], "暂无法确认最新授权状态，请稍后重试")

    def test_authorized_timestamp_is_not_invented_from_local_clock(self):
        mock = Mock()
        mock.create.return_value = ControlResult("authorized", configured=True)
        mock.refresh.return_value = ControlResult("authorized", configured=True)
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            self.assertIsNone(first["authorized_at"])
            row = AlipaySettlementAuthorization.objects.get(pk=first["id"])
            row.authorized_at = timezone.now()
            row.save(update_fields=["authorized_at"])
            self.assertIsNone(self.client.get(URL).json()["data"][0]["authorized_at"])
            self.assertIsNone(self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]["authorized_at"])
            row.refresh_from_db()
            self.assertIsNone(row.authorized_at)

    def test_continue_recovers_when_create_never_reached_control(self):
        mock = Mock()
        mock.create.side_effect = [ControlUnavailable(), ControlResult("pending")]
        mock.authorize.return_value = ControlResult("pending", "https://example.com/authorize")
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            second = self.post(f"{URL}{first['id']}/authorize/", {}).json()["data"]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(mock.create.call_count, 2)
        mock.authorize.assert_called_once_with(self.request_id)
        self.assertTrue(second["qr_png_base64"].startswith("iVBOR"))

    def test_identity_step_is_never_presented_as_second_scan(self):
        mock = Mock()
        mock.create.return_value = ControlResult("pending", "https://example.com/agreement", next_action="agreement")
        mock.refresh.side_effect = [ControlResult("pending", "https://example.com/identity", next_action="identity"), ControlResult("authorized", configured=True)]
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            self.assertEqual((first["status"], first["next_action"]), ("pending", "agreement"))
            path = f"{URL}{first['id']}/"
            refreshed = self.post(path + "refresh/", {}).json()["data"]
            self.assertEqual((refreshed["status"], refreshed["next_action"]), ("pending", None))
            self.assertTrue(refreshed["requires_platform_review"])
            self.assertIsNone(refreshed["qr_png_base64"])
            self.assertIsNone(self.client.get(URL).json()["data"][0]["next_action"])
            second = self.post(path + "authorize/", {}).json()["data"]
            self.assertTrue(second["requires_platform_review"])
            mock.authorize.assert_not_called()
            final = self.post(path + "refresh/", {}).json()["data"]
        self.assertEqual(final["status"], "authorized")
        self.assertIsNone(final["next_action"])

    def test_identity_scan_and_confirmed_revoke(self):
        mock = Mock()
        mock.create.return_value = ControlResult('pending', next_action='agreement')
        mock.refresh.return_value = ControlResult('signed', next_action='identity')
        mock.identity.return_value = ControlResult('signed', 'https://ur.alipay.com/identity', next_action='identity')
        mock.identity_refresh.return_value = ControlResult('authorized', configured=True)
        mock.revoke.return_value = ControlResult('revoked')
        with patch('apps.business.api.alipay_authorization_views.get_control_client', return_value=mock):
            first = self.post(URL, self.body).json()['data']
            path = f"{URL}{first['id']}/"
            signed = self.post(path + 'refresh/', {}).json()['data']
            self.assertEqual((signed['status'], signed['next_action']), ('signed', 'identity'))
            scan = self.post(path + 'identity/', {}).json()['data']
            self.assertTrue(scan['qr_png_base64'].startswith('iVBOR'))
            self.assertEqual(self.post(path + 'identity-refresh/', {}).json()['data']['status'], 'authorized')
            self.assertNotEqual(self.post(path + 'revoke/', {'confirm': 1}).json()['code'], 0)
            mock.revoke.assert_not_called()
            cancelled = self.post(path + 'revoke/', {'confirm': True}).json()['data']
            self.assertEqual(cancelled['status'], 'revoked')
            self.assertEqual(AlipaySettlementAuthorization.objects.count(), 1)
            self.assertTrue(OperationLog.objects.filter(action='finance.alipay_authorization', reason='revoke').exists())

    def test_revoke_timeout_blocks_retry_and_refresh(self):
        mock = Mock()
        mock.create.return_value = ControlResult('authorized', configured=True)
        mock.revoke.side_effect = ControlOutcomeUnknown()
        with patch('apps.business.api.alipay_authorization_views.get_control_client', return_value=mock):
            first = self.post(URL, self.body).json()['data']
            path = f"{URL}{first['id']}/"
            result = self.post(path + 'revoke/', {'confirm': True}).json()['data']
            self.assertTrue(result['requires_platform_review'])
            self.assertEqual(result['last_error'], '操作结果待平台核对，请勿重复操作')
            self.post(path + 'revoke/', {'confirm': True})
            mock.revoke.assert_called_once()

    def test_inflight_write_blocks_parallel_refresh(self):
        mock = Mock()
        mock.create.return_value = ControlResult('authorized', configured=True)
        with patch('apps.business.api.alipay_authorization_views.get_control_client', return_value=mock):
            first = self.post(URL, self.body).json()['data']
            path = f"{URL}{first['id']}/"
            def cancel(_):
                during = self.post(path + 'refresh/', {}).json()['data']
                self.assertTrue(during['requires_platform_review'])
                mock.refresh.assert_not_called()
                return ControlResult('revoked')
            mock.revoke.side_effect = cancel
            self.assertEqual(self.post(path + 'revoke/', {'confirm': True}).json()['data']['status'], 'revoked')

    def test_known_identity_stage_supersedes_old_authorized_state(self):
        mock = Mock()
        mock.create.return_value = ControlResult("authorized", configured=True)
        mock.refresh.return_value = ControlResult("pending", next_action="identity")
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            result = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
        self.assertEqual((result["status"], result["next_action"]), ("pending", None))
        self.assertTrue(result["requires_platform_review"])
        self.assertIsNone(result["authorized_at"])

    def test_legacy_identity_stage_enters_review_without_rescan(self):
        mock = Mock()
        mock.create.return_value = ControlResult("pending", "https://example.com/agreement", next_action="agreement")
        mock.refresh.side_effect = ControlIdentityIncomplete()
        with patch("apps.business.api.alipay_authorization_views.get_control_client", return_value=mock):
            first = self.post(URL, self.body).json()["data"]
            row = AlipaySettlementAuthorization.objects.get(pk=first["id"])
            row.next_action = "identity"
            row.save(update_fields=["next_action"])
            listed = self.client.get(URL).json()["data"][0]
            self.assertTrue(listed["requires_platform_review"])
            self.assertEqual(listed["status"], "pending")
            self.assertIsNone(listed["next_action"])
            self.post(f"{URL}{first['id']}/authorize/", {})
            self.assertEqual(mock.create.call_count, 1)
            mock.authorize.assert_not_called()
            refreshed = self.post(f"{URL}{first['id']}/refresh/", {}).json()["data"]
        self.assertTrue(refreshed["requires_platform_review"])

    def test_missing_or_wrong_tenant_data_key_fails_closed(self):
        self.post(URL, self.body)
        with override_settings(ALIPAY_AUTH_DATA_KEY=""):
            self.assertEqual(self.client.get(URL).status_code, 503)
            self.assertEqual(self.post(URL, {**self.body, "request_id": str(uuid.uuid4())}).status_code, 503)
        with override_settings(ALIPAY_AUTH_DATA_KEY=Fernet.generate_key().decode()):
            self.assertEqual(self.client.get(URL).status_code, 503)
        self.assertEqual(AlipaySettlementAuthorization.objects.count(), 1)

    def test_without_permission_is_forbidden(self):
        client = make_logged_in_client(username="noauth", nodes=())
        self.assertEqual(client.get(URL).status_code, 403)

    def test_tenant_cannot_see_another_tenants_record(self):
        self.post(URL, self.body)
        tenant, _ = provision_tenant(schema_name="alipay_auth_b", name="租户 B", domain="alipay-b.test.local")
        with tenant_context(tenant):
            client_b = make_logged_in_client(domain="alipay-b.test.local", username="tenantb", is_superuser=True)
            body = {"request_id": str(uuid.uuid4()), "account": "13900001235", "real_name": "另一姓名"}
            response = client_b.post(URL, json.dumps(body), content_type="application/json")
            self.assertEqual(response.json()["code"], 0)
            items = client_b.get(URL).json()["data"]
            self.assertIn("139****1235", [x["account_masked"] for x in items])
            self.assertNotIn("138****1234", [x["account_masked"] for x in items])
        response = self.client.get(URL, HTTP_HOST="alipay-b.test.local")
        self.assertNotIn("139****1235", response.content.decode())
        self.assertNotEqual(response.status_code, 200)
        connection.set_tenant(self.tenant)


class ControlClientTests(SimpleTestCase):
    def test_write_transport_failure_is_outcome_unknown(self):
        client = AuthorizationControlClient('test-key', base_url='https://service.example', client_id='tenant-a')
        with patch('apps.business.alipay_auth_control.httpx.post', side_effect=httpx.ReadTimeout('test')):
            for operation in (client.revoke, client.identity_refresh):
                with self.assertRaises(ControlOutcomeUnknown):
                    operation('test-id')
        request = httpx.Request('POST', 'https://service.example/authorizations')
        for operation, body in ((client.revoke, {'status': 'authorized'}),
                                (client.identity_refresh, {'status': 'authorized', 'next_action': 'agreement'})):
            response = httpx.Response(200, json=body, request=request)
            with patch('apps.business.alipay_auth_control.httpx.post', return_value=response), self.assertRaises(ControlOutcomeUnknown):
                operation('test-id')
    def test_identity_review_codes_are_distinct_and_whitelisted(self):
        request = httpx.Request("POST", "https://service.example/authorizations")
        client = AuthorizationControlClient("test-key", base_url="https://service.example", client_id="tenant-a")
        for code, expected in (("IDENTITY_INCOMPLETE", ControlIdentityIncomplete), ("IDENTITY_UNVERIFIED", ControlIdentityUnverified)):
            response = httpx.Response(409, json={"code": code, "error": "不应显示"}, request=request)
            with self.subTest(code=code), patch("apps.business.alipay_auth_control.httpx.post", return_value=response):
                with self.assertRaises(expected):
                    client.refresh("test-id")

    def test_only_whitelisted_pending_stages_are_exposed(self):
        response = Mock()
        client = AuthorizationControlClient("test-key", base_url="https://service.example", client_id="tenant-a")
        with patch("apps.business.alipay_auth_control.httpx.post", return_value=response):
            response.json.return_value = {"status": "pending", "next_action": "identity"}
            with self.assertRaises(ControlIdentityIncomplete):
                client.refresh("test-id")
            response.json.return_value = {"status": "pending", "next_action": "unrecognized"}
            self.assertIsNone(client.refresh("test-id").next_action)
            response.json.return_value = {"status": "authorized", "configured": True, "next_action": "identity"}
            with self.assertRaises(ControlIdentityIncomplete):
                client.refresh("test-id")

    def test_unknown_code_is_distinct_from_other_conflicts(self):
        request = httpx.Request("POST", "https://service.example/authorizations")
        unknown = httpx.Response(409, json={"code": "OUTCOME_UNKNOWN", "error": "不应显示"}, request=request)
        other = httpx.Response(409, json={"code": "OTHER"}, request=request)
        client = AuthorizationControlClient("test-key", base_url="https://service.example", client_id="tenant-a")
        with patch("apps.business.alipay_auth_control.httpx.post", return_value=unknown):
            with self.assertRaises(ControlOutcomeUnknown):
                client.create("id", "13800001234", "测试姓名")
        with patch("apps.business.alipay_auth_control.httpx.post", return_value=other):
            with self.assertRaises(ControlUnavailable) as result:
                client.create("id", "13800001234", "测试姓名")
            self.assertNotIsInstance(result.exception, ControlOutcomeUnknown)

    def test_signs_exact_request_body_and_prohibits_redirects(self):
        response = Mock()
        response.json.return_value = {"status": "pending", "configured": False}
        with patch("apps.business.alipay_auth_control.httpx.post", return_value=response) as post, \
             patch("apps.business.alipay_auth_control.time.time", return_value=1234567890), \
             patch("apps.business.alipay_auth_control.secrets.token_hex", return_value="a" * 32):
            client = AuthorizationControlClient("test-key", base_url="https://service.example", client_id="tenant-a")
            self.assertEqual(client.create("test-id", "13800001234", "测试姓名").status, "pending")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://service.example/authorizations")
        self.assertFalse(kwargs["follow_redirects"])
        self.assertEqual(kwargs["timeout"], 10)
        body = kwargs["content"]
        signed = "\n".join(("POST", "/authorizations", "1234567890", "a" * 32, hashlib.sha256(body).hexdigest()))
        self.assertEqual(kwargs["headers"]["X-Settlement-Signature"], hmac.new(b"test-key", signed.encode(), hashlib.sha256).hexdigest())

    def test_plain_http_is_never_called(self):
        with patch("apps.business.alipay_auth_control.httpx.post") as post:
            with self.assertRaises(ControlUnavailable):
                AuthorizationControlClient("key", base_url="http://service.example", client_id="tenant-a").refresh("x")
        post.assert_not_called()
