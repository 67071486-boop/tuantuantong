import http.server
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from chat_service.protocol import verify_request
from tests import test_chat_core_engine as fixture


class CutoverRuntimeHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CoreHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CoreHTTPTests.tearDownClass.__func__)
    tearDown = fixture.CoreHTTPTests.tearDown
    matrix_reply = fixture.CoreHTTPTests.matrix_reply
    request = fixture.CoreHTTPTests.request
    conversation = fixture.CoreHTTPTests.conversation
    message = fixture.CoreHTTPTests.message
    seed = fixture.CoreHTTPTests.seed
    core = fixture.CoreHTTPTests.core
    activate = fixture.CoreHTTPTests.activate
    authority_reply = fixture.CoreHTTPTests.authority_reply

    def setUp(self):
        fixture.CoreHTTPTests.setUp(self)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'shared').mkdir()
        current = self.root / 'releases/protocol-four'
        current.mkdir(parents=True)
        (current / 'manifest.json').write_text(json.dumps({'core_protocol': 4}))
        (self.root / 'current').symlink_to(current)
        test = self

        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                data = {'versions': ['v1.11']}
                if handler.path != '/_matrix/client/versions':
                    verify_request({'a': 'a-secret'}, dict(handler.headers), 'GET', handler.path, b'')
                    data = {'code': 0, 'msg': '', 'data': {'latest': 10, 'cursor': 0, 'has_more': False,
                        'events': [], 'core_write_fenced': True, 'chat_paused': True}}
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps(data).encode())

            def do_POST(handler):
                if handler.path != '/notify' or handler.headers.get('Authorization') != 'Bearer gateway-secret':
                    handler.send_error(403)
                    return
                json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                test.notifications += 1
                handler.send_response(204)
                handler.end_headers()

            def log_message(*args):
                pass

        self.notifications = 0
        source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Source)
        self.addCleanup(source.server_close)
        self.addCleanup(source.shutdown)
        threading.Thread(target=source.serve_forever, daemon=True).start()
        self.source_url = 'http://127.0.0.1:' + str(source.server_port)
        self.engine.tenants['a']['realtime'] = {'url': self.source_url, 'secret': 'gateway-secret', 'tenant': 'studio_a'}
        config = self.root / 'shared/service.json'
        config.write_text(json.dumps({'matrix_url': self.source_url, 'registration_shared_secret': 'registration',
            'postgres_dsn': self.dsn, 'tenants': {'a': {'secret': 'a-secret',
                'source_url': self.source_url, 'source_host': 'source.test.invalid'}}}))
        config.chmod(0o600)

    def control(self, action, *extra):
        script = Path(__file__).resolve().parents[2] / 'deploy/chat-service/core_control.py'
        return subprocess.run([sys.executable, str(script), action, '--root', str(self.root), '--tenant', 'a',
            '--service-url', str(self.http.base_url).rstrip('/'), *extra], capture_output=True, text=True, timeout=10)

    def test_cli_pause_import_activate_drain_and_explicit_resume_use_real_core_http_and_pg(self):
        paused = self.control('pause')
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertFalse(json.loads(paused.stdout)['active'])
        self.seed()
        activated = self.control('activate', '--expected-cursor', '10')
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertTrue(json.loads(activated.stdout)['writes_paused'])
        self.assertEqual(self.control('drain', '--timeout-seconds', '2').returncode, 0)
        self.assertNotEqual(self.control('resume').returncode, 0)
        self.assertTrue(self.core('status')[1]['data']['writes_paused'])
        resumed = self.control('resume', '--confirm-verified')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(json.loads(resumed.stdout)['writes_paused'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT required_protocol,active,writes_paused FROM core_control WHERE tenant_id=%s', ('a',)).fetchone(),
                {'required_protocol': 4, 'active': True, 'writes_paused': False})

    def test_cli_drain_requires_actual_worker_queue_completion_and_keeps_writes_paused(self):
        self.activate()
        self.engine.tenants['a']['realtime']['url'] = self.source_url
        accepted = self.core('send', query={'conversation_id': 1}, payload={'text': 'accepted before pause', 'client_id': 'cutover-accepted'})
        self.assertEqual(accepted[0], 200, accepted)
        self.assertEqual(self.control('pause').returncode, 0)
        blocked = self.control('drain', '--timeout-seconds', '0.15', '--poll-interval', '0.01')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertTrue(self.core('status')[1]['data']['writes_paused'])
        self.engine.core.tick('a')
        drained = self.control('drain', '--timeout-seconds', '2')
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertTrue(json.loads(drained.stdout)['writes_paused'])
        self.assertEqual(json.loads(drained.stdout)['pending_messages'], 0)
        self.assertEqual(json.loads(drained.stdout)['pending_notifications'], 0)
        self.assertGreater(self.notifications, 0)
