import json
import os
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.test import SimpleTestCase, override_settings
from django_tenants.utils import schema_context

from apps.business import dailiantong_push, jidanpu, jidanpu_proxy
from apps.business.dailiantong_client import (
    DailiantongClient,
    DailiantongConfig,
    DailiantongTransportError,
)
from apps.business.jidanpu_openapi import openapi_client

VENDOR_HOST = "orders-egress.invalid"
REMOTE_ORDER = "48202611210004298175"


class _VendorHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = self.rfile.read(int(self.headers["Content-Length"]))
        data = json.loads(payload) if self.headers.get_content_type() == "application/json" else parse_qs(payload.decode())
        self.server.received.append((self.path, self.headers["Host"], data))
        self.server.auth.append((self.headers.get("Cookie"), self.headers.get("Authorization")))
        response = json.dumps({
            "/Login/Login": {"code": 1, "data": "Y"},
            "/OrderAjax/AddOrderAjax": {"code": 1298175, "msg": "录单成功"},
            "/oauth/token": {"access_token": "fixture-token", "token_type": "bearer", "expires_in": 3600},
            "/api/OpenPlatform/CreateOrder": {"code": 1298175, "data": REMOTE_ORDER},
        }.get(self.path, {"Result": 1, "Err": REMOTE_ORDER})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.path == "/Login/Login":
            self.send_header("Set-Cookie", "sl-session=fixture-session; Path=/; Secure")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        pass


def _server_certificate(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, VENDOR_HOST)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(VENDOR_HOST)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    return cert_file, key_file


class PlatformEgressTests(SimpleTestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory(dir="/tmp")))
        socket_path = str(directory / "vendor.sock")
        cert_file, key_file = _server_certificate(directory)
        server = self.enterContext(UnixStreamServer(socket_path, _VendorHandler))
        server.received = []
        server.auth = []
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert_file, key_file)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        self.sni = []
        tls.set_servername_callback(lambda sock, hostname, context: self.sni.append(hostname))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.received = server.received
        self.auth = server.auth
        self.socket_path = Path(socket_path)
        self.routes_file = directory / "routes.json"
        self.routes_file.write_text(json.dumps({"mtaccept": {
            "hosts": {VENDOR_HOST: socket_path},
            "dailiantong_base_url": f"https://{VENDOR_HOST}/API/OpenService.ashx",
        }}))
        self.routes_file.chmod(0o600)
        self.enterContext(override_settings(TT_PLATFORM_EGRESS_ROUTES_FILE=str(self.routes_file)))
        self.enterContext(patch.dict(os.environ, {
            "SSL_CERT_FILE": str(cert_file),
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }))

    @staticmethod
    def publish(host=None):
        integration = SimpleNamespace(
            app_id="synthetic-app",
            app_secret="synthetic-secret",
            account="fixture",
            pay_password_md5="3" * 32,
        )
        if host is None:
            client = dailiantong_push._open_client(integration)
        else:
            client = DailiantongClient(DailiantongConfig(
                app_id=integration.app_id, app_secret=integration.app_secret,
                user_id=integration.account, pay_password_md5=integration.pay_password_md5,
                base_url=f"https://{host}/API/OpenService.ashx", timeout=1,
            ))
        with client:
            return client.add_order(
                zone_server_id="100100010000000",
                title="合成测试发单",
                time_limit=24,
                price=5,
                ensure1=0,
                ensure2=0,
                actors="fixture",
                ex_order_no="TT-TEST-EGRESS",
            )

    def test_dailiantong_publishes_through_tenant_socket_with_vendor_tls_and_host(self):
        with schema_context("mtaccept"):
            self.assertEqual(self.publish(), REMOTE_ORDER)
        self.assertEqual(self.sni, [VENDOR_HOST])
        ((path, host, form),) = self.received
        self.assertEqual(path, "/API/OpenService.ashx")
        self.assertEqual(host, VENDOR_HOST)
        self.assertEqual(form["Action"], ["LevelOrderAdd"])
        self.assertEqual(form["ExOrderNo"], ["TT-TEST-EGRESS"])

    def test_another_tenant_cannot_use_mtaccept_vendor_socket(self):
        with schema_context("otherworkshop"), self.assertRaises(DailiantongTransportError):
            self.publish(host=VENDOR_HOST)
        self.assertEqual(self.received, [])
        self.assertEqual(self.sni, [])

    def test_unregistered_vendor_and_non_https_port_never_use_tunnel(self):
        with schema_context("mtaccept"):
            for origin in ("unregistered-egress.invalid", f"{VENDOR_HOST}:444"):
                with self.subTest(origin=origin), self.assertRaises(DailiantongTransportError):
                    self.publish(host=origin)
        self.assertEqual(self.received, [])
        self.assertEqual(self.sni, [])

    def test_lost_tunnel_does_not_retry_on_old_egress(self):
        self.socket_path.rename(self.socket_path.with_suffix(".off"))
        with schema_context("mtaccept"), self.assertRaises(DailiantongTransportError) as failure:
            self.publish()
        self.assertEqual(type(failure.exception.__cause__).__name__, "ConnectError")
        self.assertEqual(self.received, [])

    def test_legacy_login_and_order_use_the_same_tunnel_and_session(self):
        binding = SimpleNamespace(
            base_url=f"https://{VENDOR_HOST}", jidanpu_base_url=f"https://{VENDOR_HOST}",
            account="fixture-login", password="fixture-password",
        )
        with schema_context("mtaccept"), jidanpu.open_client(binding) as client:
            result = jidanpu.add_order(
                client, form_payload={}, top_level={}, source_order_no="TT-LEGACY-EGRESS",
            )
        self.assertEqual(result.remote_oid, 1298175)
        self.assertEqual([path for path, _, _ in self.received], ["/Login/Login", "/OrderAjax/AddOrderAjax"])
        self.assertEqual(self.received[-1][-1]["souresOrderID"], ["TT-LEGACY-EGRESS"])
        self.assertEqual(self.auth[-1], ("sl-session=fixture-session", None))

    def test_openapi_token_and_order_use_the_same_tunnel(self):
        binding = SimpleNamespace(
            pk=9175, mode="openapi", jidanpu_base_url=f"https://{VENDOR_HOST}",
            client_id="fixture-id", client_secret="fixture-secret",
        )
        with schema_context("mtaccept"), openapi_client(binding) as client:
            remote_id = client.create_order(
                game_name="合成游戏", game_area="合成区", project="测试", hours=1,
                order_soures="测试", price=5, customer_id="1", rec_user="",
                assign_price=2, source_order_id="TT-OFFICIAL-EGRESS",
            )
        self.assertEqual(remote_id, REMOTE_ORDER)
        self.assertEqual([path for path, _, _ in self.received], ["/oauth/token", "/api/OpenPlatform/CreateOrder"])
        self.assertEqual(self.received[-1][-1]["souresOrderID"], "TT-OFFICIAL-EGRESS")
        self.assertEqual(self.auth[-1], (None, "Bearer fixture-token"))

    def test_dedicated_vendor_failure_cannot_change_other_tenants_proxy(self):
        previous = "http://127.0.0.1:48001"
        backup = "http://127.0.0.1:48002"
        self.addCleanup(jidanpu_proxy.reset_counter_for_tests)
        jidanpu_proxy.reset_counter_for_tests()
        with patch.dict(os.environ, {"JIDANPU_PROXIES": f"{previous},{backup}", "JIDANPU_EGRESS_STATE_FILE": ""}):
            with schema_context("mtaccept"):
                self.assertEqual(jidanpu_proxy.note_blocked(force=True), jidanpu_proxy.BLOCK_COUNTED)
            with schema_context("otherworkshop"):
                self.assertEqual(jidanpu_proxy.proxy_url(), previous)
                self.assertEqual(jidanpu_proxy.note_blocked(force=True), jidanpu_proxy.BLOCK_SWITCHED)
                self.assertEqual(jidanpu_proxy.proxy_url(), backup)
