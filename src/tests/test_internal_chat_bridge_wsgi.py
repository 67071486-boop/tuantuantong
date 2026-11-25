import json
from unittest.mock import Mock, patch

from django.core.handlers.wsgi import WSGIHandler
from django.test import RequestFactory, SimpleTestCase, override_settings

from chat_service.protocol import sign_request


@override_settings(MIDDLEWARE=[])
class InternalChatBridgeEntryTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.enterContext(patch("config.internal_chat_bridge_wsgi.web_application", WSGIHandler()))

    def call(self, method, path, body=b"", headers=None):
        from config.internal_chat_bridge_wsgi import application

        request = self.factory.generic(method, path, body, content_type="application/json", headers=headers)
        start_response = Mock()
        environ = request.environ.copy()
        if environ.get("HTTP_COOKIE") == "":
            environ.pop("HTTP_COOKIE")
        result = application(environ, start_response)
        try:
            payload = b"".join(result)
        finally:
            if hasattr(result, "close"):
                result.close()
        status, response_headers = start_response.call_args.args
        return status, dict(response_headers), payload

    def test_health_uses_the_existing_django_view(self):
        status, _, body = self.call("GET", "/healthz")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_other_paths_are_rejected_before_django(self):
        paths = (
            "/api/internal-chat/service/",
            "/api/internal-chat/service/export",
            "/api/internal-chat/service/export.json",
            "/api/internal-chat/service/export/extra/",
            "/api/internal-chat/service/browser-authorize/extra/",
            "/api/internal-chat/service/peer-chat/extra/",
            "/api/internal-chat/direct-ticket/",
            "/api/internal-chat/service/unregistered/",
            "/api/internal-chat/conversations/",
            "/api/auth/me",
            "/api/business/orders/",
            "/healthz/",
        )
        with patch("config.internal_chat_bridge_wsgi.web_application") as django:
            for path in paths:
                with self.subTest(path=path):
                    status, headers, _ = self.call("GET", path)
                    self.assertEqual(status, "404 Not Found")
                    self.assertEqual(headers["Cache-Control"], "no-store")
            django.assert_not_called()

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    @patch("apps.internal_chat.peer_chat.core_requested", return_value=True)
    @patch("apps.internal_chat.matrix_bridge.tenant_config", return_value={
        "key_id": "bridge-test", "secret": "synthetic-chat-bridge-test-signing-value",
    })
    def test_callbacks_retain_existing_signature_and_input_validation(self, tenant_config, _core_requested):
        paths = (
            ("GET", "/api/internal-chat/service/export/?limit=0", b"", "400 Bad Request", 400),
            ("GET", "/api/internal-chat/service/snapshot/?kind=unrecognized", b"", "400 Bad Request", 400),
            ("POST", "/api/internal-chat/service/order-authorizations/", b"{}", "200 OK", 1000),
            ("POST", "/api/internal-chat/service/order-intents/", b"{}", "200 OK", 1000),
            ("POST", "/api/internal-chat/service/peer-chat/", b"{}", "200 OK", 1000),
            ("POST", "/api/internal-chat/service/browser-authorize/", b"{}", "200 OK", 1000),
            ("GET", "/api/internal-chat/service/order-intents/", b"", "405 Method Not Allowed", 405),
        )
        for method, path, body, rejected_status, rejected_code in paths:
            with self.subTest(method=method, path=path):
                status, _, response = self.call(method, path, body)
                self.assertEqual(status, "403 Forbidden")
                self.assertIsNone(json.loads(response)["data"])
                config = tenant_config.return_value
                signing = sign_request(config["secret"], config["key_id"], method, path, body)
                status, _, response = self.call(method, path, body, signing)
                self.assertEqual(status, rejected_status)
                self.assertEqual(json.loads(response)["code"], rejected_code)
                status, _, _ = self.call(method, path, body, {**signing, "Cookie": "sessionid=synthetic"})
                self.assertEqual(status, "403 Forbidden")
