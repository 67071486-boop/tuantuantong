import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from gunicorn.config import Config
from gunicorn.util import load_class

_ROOT = Path(__file__).resolve().parents[2]
_LOGIN = "/api/auth/wxpusher/login"


class WxPusherAccessLogTests(SimpleTestCase):
    def _deployed_logger(self):
        unit = (_ROOT / "deploy/systemd/tuantuantong.service").read_text(encoding="utf-8")
        self.assertIn("--logger-class ", unit)
        logger_name = unit.split("--logger-class ", 1)[1].split()[0]
        log_format = unit.split("--access-logformat '", 1)[1].split("'", 1)[0].replace("%%", "%")
        config = Config()
        config.set("accesslog", "-")
        config.set("access_log_format", log_format)
        with patch.object(sys, "path", [str(_ROOT / "deploy"), *sys.path]):
            return load_class(logger_name)(config)

    def _access_line(self, target, referer="-"):
        logger = self._deployed_logger()
        path, _, query = target.partition("?")
        environ = {
            "REQUEST_METHOD": "GET",
            "RAW_URI": target,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_REFERER": referer,
        }
        response = SimpleNamespace(status="302 Found", sent=0, headers=[])
        with self.assertLogs("gunicorn.access", level="INFO") as captured:
            logger.access(response, [], environ, timedelta(milliseconds=15))
        return captured.records[0].getMessage()

    def test_signed_request_and_referrer_queries_are_omitted(self):
        query = "?sign=OWN_REQUEST_SENTINEL&uid=TEST_UID"
        referer = "https://pilot.example.test" + _LOGIN + "?sign=REFERRER_SENTINEL"
        line = self._access_line(_LOGIN + query, referer)
        self.assertIn(f'"GET {_LOGIN} HTTP/1.1" 302', line)
        self.assertIn(f'"https://pilot.example.test{_LOGIN}"', line)
        self.assertTrue(line.endswith(" 0.015000"))
        self.assertNotIn("SENTINEL", line)
        self.assertNotIn("TEST_UID", line)

    def test_unrelated_request_keeps_its_query_and_hides_a_signed_referrer(self):
        referer = "https://pilot.example.test" + _LOGIN + "?sign=REFERRER_SENTINEL"
        line = self._access_line("/api/orders?page=5", referer)
        self.assertIn('"GET /api/orders?page=5 HTTP/1.1"', line)
        self.assertNotIn("REFERRER_SENTINEL", line)

    def test_worker_failure_keeps_the_path_and_hides_the_ticket(self):
        logger = self._deployed_logger()
        with self.assertLogs("gunicorn.error", level="WARNING") as captured:
            logger.exception("Error handling request %s", _LOGIN + "?sign=EXCEPTION_SENTINEL")
            logger.warning(f"Invalid Request Line 'GET {_LOGIN}?sign=INVALID_SENTINEL HTTP/1.1'")
            logger.warning("Error handling request /api/orders?page=5")
        self.assertEqual(
            [record.getMessage() for record in captured.records],
            [f"Error handling request {_LOGIN}", f"Invalid Request Line 'GET {_LOGIN} HTTP/1.1'",
             "Error handling request /api/orders?page=5"],
        )

    def test_nginx_protects_exact_port_443_and_port_80_routes(self):
        tls = (_ROOT / "deploy/nginx/tuantuantong.conf").read_text(encoding="utf-8")
        bootstrap = (_ROOT / "deploy/nginx/tuantuantong-http.conf").read_text(encoding="utf-8")
        protected_tls_blocks = tls.split(f"location = {_LOGIN} {{")[1:]
        self.assertEqual(len(protected_tls_blocks), 2)
        self.assertIn(f"location = {_LOGIN} {{", bootstrap)
        http_redirect, https_proxy = (part.split("}", 1)[0] for part in protected_tls_blocks)
        http_only = bootstrap.split(f"location = {_LOGIN} {{", 1)[1].split("}", 1)[0]
        for location in (http_redirect, https_proxy, http_only):
            self.assertIn("access_log off;", location)
            self.assertIn("error_log /dev/null;", location)
        self.assertIn(f"return 301 https://$host{_LOGIN};", http_redirect)
        self.assertIn("proxy_pass http://127.0.0.1:8001;", https_proxy)
        self.assertIn("return 404;", http_only)
