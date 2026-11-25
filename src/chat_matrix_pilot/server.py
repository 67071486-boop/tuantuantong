import hmac
import json
import mimetypes
import secrets
import time
import traceback
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .matrix import PilotError
from .validation import json_strings


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, pilot, webk_root):
        self.pilot = pilot
        self.webk_root = Path(webk_root).resolve()
        self.sessions = {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Paths and cookies can contain secrets; local pilot does not log requests.
        pass

    def respond(self, value, status=200, extra=None):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, content in (extra or {}).items():
            self.send_header(name, content)
        self.end_headers()
        self.wfile.write(data)

    def identity(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookie['tt_matrix_pilot'].value
        except (KeyError, ValueError, CookieError):
            raise PilotError('请先登录测试账号', 401) from None
        session = self.server.sessions.get(value)
        if not session or session[1] < time.time():
            raise PilotError('登录已过期', 401)
        return session[0]

    def user_result(self, uid):
        user = self.server.pilot.users[str(uid)]
        return {'user': {'id': int(uid), 'display_name': user['name']}, 'studio': user['studio']}

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def handle_request(self, method):
        try:
            parsed = urlsplit(self.path)
            if method == 'POST':
                origin = self.headers.get('Origin')
                host = self.headers.get('Host')
                actual_host, port = self.server.server_address
                allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
                if host not in allowed_hosts or (origin and origin != 'http://' + host):
                    raise PilotError('请求来源不受信任', 403)
                length = self.headers.get('Content-Length', '0')
                try:
                    length = int(length)
                except ValueError:
                    raise PilotError('请求长度错误') from None
                if length < 0 or length > 15_000_000:
                    raise PilotError('请求过大', 413)
                if self.headers.get_content_type() != 'application/json':
                    raise PilotError('需要JSON请求', 415)
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError, RecursionError):
                    raise PilotError('请求内容格式错误') from None
                if not isinstance(body, dict):
                    raise PilotError('请求内容需要是对象')
                json_strings(body)
            else:
                body = {}
            path = parsed.path
            if path == '/healthz' and method == 'GET':
                return self.respond({'ok': True, 'mode': 'matrix-pilot'})
            if path == '/pilot/config' and method == 'GET':
                return self.respond({'legacy_url': self.server.pilot.config['legacy_url']})
            if path == '/pilot/rollback' and method == 'GET':
                return self.respond({'message': '返回原应用，聊天样板数据保留'}, 303,
                                    {'Location': self.server.pilot.config['legacy_url']})
            if path == '/pilot/login' and method == 'POST':
                if set(body) - {'username', 'password'}:
                    raise PilotError('不接受工作室或其他身份参数')
                username, password = body.get('username'), body.get('password')
                if not isinstance(username, str) or not isinstance(password, str) or len(username) > 100 or len(password) > 256:
                    raise PilotError('账号或密码格式错误')
                user = next((u for u in self.server.pilot.users.values() if u['username'] == username), None)
                if user is None or not hmac.compare_digest(user['password'].encode(), password.encode()):
                    raise PilotError('账号或密码错误', 401)
                session = secrets.token_urlsafe(32)
                self.server.sessions[session] = (str(user['id']), time.time() + 8 * 3600)
                return self.respond(self.user_result(user['id']), extra={
                    'Set-Cookie': f'tt_matrix_pilot={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
            if path == '/pilot/logout' and method == 'POST':
                self.identity()
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                self.server.sessions.pop(cookie['tt_matrix_pilot'].value, None)
                return self.respond({'ok': True}, extra={'Set-Cookie': 'tt_matrix_pilot=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            if path == '/pilot/session' and method == 'GET':
                return self.respond(self.user_result(self.identity()))
            if path.startswith('/api/'):
                uid = self.identity()
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                if set(query) & {'studio', 'tenant', 'tenant_id', 'studio_id'} or set(body) & {'studio', 'tenant', 'tenant_id', 'studio_id'}:
                    raise PilotError('不接受工作室参数')
                return self.respond(self.server.pilot.dispatch(uid, method, path, query, body))
            if method == 'GET':
                return self.static(path)
            raise PilotError('接口不存在', 404)
        except PilotError as error:
            self.respond({'error': str(error)}, error.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.respond({'error': '测试样板内部错误'}, 500)

    def static(self, path):
        if path.startswith('/internal-chat-webk/'):
            root = self.server.webk_root
            relative = unquote(path[len('/internal-chat-webk/'):])
        else:
            root = Path(__file__).parent / 'static'
            relative = 'index.html' if path == '/' else unquote(path.lstrip('/'))
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            raise PilotError('文件不存在', 404)
        if target.is_dir():
            target = target / 'index.html'
        if not target.is_file():
            raise PilotError('文件不存在', 404)
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(raw)
