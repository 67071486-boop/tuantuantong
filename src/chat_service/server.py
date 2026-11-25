import ipaddress
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import psycopg

from .browser import (BROWSER_PREFIX, MAX_BROWSER_BODY, BrowserChat, browser_origin, browser_payload, browser_query,
                      browser_route)
from .config import ServiceError, internal_address
from .image_limits import MAX_CHAT_MESSAGE_BODY
from .protocol import ProtocolError, verify_request


_ORDER_BUSINESS_CODES = {400: 1000, 404: 1002}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine):
        host = address[0]
        if host == 'localhost':
            host = '127.0.0.1'
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('Bind host must be an internal IP address') from None
        if not (internal_address(ip)):
            raise ValueError('Chat service may only bind an internal address')
        self.engine = engine
        super().__init__((host, address[1]), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logging.getLogger(__name__).info('%s %s', self.command, self.path.split('?', 1)[0])

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _read_body(self, limit):
        if self.headers.get('Transfer-Encoding'):
            raise ServiceError('Transfer encoding is not accepted')
        lengths = self.headers.get_all('Content-Length', [])
        try:
            length = int(lengths[0] if lengths else '0')
        except ValueError:
            raise ServiceError('Invalid Content-Length') from None
        if len(lengths) > 1:
            raise ServiceError('Invalid Content-Length')
        if not 0 <= length <= limit:
            raise ServiceError('Request body too large', 413)
        return self.rfile.read(length)

    def _respond(self, status, data, *, origin=None, allowed_method=None, allowed_headers=()):
        raw = json.dumps(data, ensure_ascii=False).encode() if data is not None else b''
        self.send_response(status)
        if data is not None:
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        if origin is not None:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin' if allowed_method is None else
                             'Origin, Access-Control-Request-Method, Access-Control-Request-Headers')
        if allowed_method is not None:
            self.send_header('Access-Control-Allow-Methods', allowed_method)
            self.send_header('Access-Control-Allow-Headers', ', '.join(allowed_headers))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _browser_preflight(self, path, origin):
        methods = self.headers.get_all('Access-Control-Request-Method', [])
        fields = self.headers.get_all('Access-Control-Request-Headers', [])
        if len(methods) != 1 or len(fields) != 1 or not browser_route(methods[0], path):
            raise ServiceError('当前跨域请求未开放', 403)
        names = [name.strip().lower() for name in fields[0].split(',')]
        permitted = {'authorization': 'Authorization'}
        if methods[0] == 'POST':
            permitted['content-type'] = 'Content-Type'
        if 'authorization' not in names or len(set(names)) != len(names) or set(names) - permitted.keys():
            raise ServiceError('当前跨域请求未开放', 403)
        self._respond(204, None, origin=origin, allowed_method=methods[0],
                      allowed_headers=[permitted[name] for name in names])

    def _browser(self):
        origin = self.headers.get_all('Origin', [])
        host = self.headers.get_all('Host', [])
        origin_allowed = (len(origin) == len(host) == 1 and browser_origin(self.server.engine.tenants, origin[0], host[0]))
        cors_origin = origin[0] if origin_allowed else None
        order_business_response = False
        try:
            if not origin_allowed or self.headers.get_all('Cookie'):
                raise ServiceError('当前域名未开通聊天直连', 403)
            url = urlsplit(self.path)
            if self.command == 'OPTIONS':
                self._browser_preflight(url.path, cors_origin)
                return
            route = browser_route(self.command, url.path)
            if route is None:
                raise ServiceError('聊天接口不存在', 404)
            headers = self.headers.get_all('Authorization', [])
            if len(headers) != 1:
                raise ProtocolError('需要聊天凭证')
            chat = BrowserChat(self.server.engine, headers[0], cors_origin, host[0])
            body_limit = MAX_CHAT_MESSAGE_BODY if route.operation in {'send', 'order_send'} else MAX_BROWSER_BODY
            raw = self._read_body(body_limit)
            if raw and (self.command != 'POST' or self.headers.get_content_type() != 'application/json'):
                raise ServiceError('需要 JSON 聊天请求')
            order_business_response = route.order_no is not None
            query = browser_query(route, url.query)
            data = chat.dispatch(route, query, browser_payload(route.operation, raw))
            status, envelope = 200, {'code': 0, 'msg': '', 'data': data}
        except ProtocolError as error:
            status, envelope = 401, {'code': 401, 'msg': str(error), 'data': None}
        except ServiceError as error:
            status, code, message = error.status, error.status, str(error)
            if order_business_response and code in _ORDER_BUSINESS_CODES:
                if code == 404:
                    message = '记录不存在或已被删除'
                status, code = 200, _ORDER_BUSINESS_CODES[code]
            envelope = {'code': code, 'msg': message, 'data': None}
        except (psycopg.OperationalError, httpx.HTTPError):
            status, envelope = 503, {'code': 503, 'msg': '内部沟通暂时不可用', 'data': None}
        except Exception as error:
            logging.getLogger(__name__).error('Browser chat request failed: %s', type(error).__name__)
            status, envelope = 500, {'code': 500, 'msg': '内部沟通暂时不可用', 'data': None}
        self._respond(status, envelope, origin=cors_origin)

    def _dispatch(self):
        if self.path.startswith(BROWSER_PREFIX):
            self._browser()
            return
        if self.command == 'OPTIONS':
            self._respond(405, {'code': 405, 'msg': 'Method not allowed', 'data': None})
            return
        status, payload = 200, None
        try:
            if self.command == 'GET' and self.path == '/healthz':
                with self.server.engine.store.connect() as db:
                    db.execute('SELECT 1')
                payload = {'status': 'ok', 'service': 'chat-service'}
            else:
                raw = self._read_body(16 * 1024 * 1024)
                keys = {key: config['secret'] for key, config in self.server.engine.tenants.items()}
                tenant = verify_request(keys, dict(self.headers.items()), self.command, self.path, raw)
                if self.command == 'GET' and self.path == '/v1/status':
                    payload = self.server.engine.store.status(tenant)
                elif self.command == 'POST' and self.path in ('/v1/import', '/v1/read', '/v1/core'):
                    try:
                        body = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                    except (ValueError, UnicodeError):
                        raise ServiceError('Invalid JSON request') from None
                    operation = {'/v1/import': self.server.engine.import_page, '/v1/read': self.server.engine.read,
                                 '/v1/core': self.server.engine.core.handle}[self.path]
                    payload = operation(tenant, body)
                else:
                    raise ServiceError('Endpoint not found', 404)
            envelope = {'code': 0, 'msg': '', 'data': payload}
        except ProtocolError as error:
            status = 403
            envelope = {'code': status, 'msg': str(error), 'data': None}
        except ServiceError as error:
            status = error.status
            envelope = {'code': status, 'msg': str(error), 'data': None}
        except (psycopg.OperationalError, httpx.HTTPError):
            status = 503
            envelope = {'code': status, 'msg': 'Chat dependency unavailable', 'data': None}
        except Exception as error:
            logging.getLogger(__name__).error('Chat request failed: %s', type(error).__name__)
            status = 500
            envelope = {'code': status, 'msg': 'Chat service error', 'data': None}
        self._respond(status, envelope)
