"""Dedicated WSGI entry for the order and finance process."""
import re
from http import HTTPStatus

from django.conf import settings

from config.wsgi import application as web_application
from config.order_finance_routes import serves_order_finance
from config.shared_web import SharedWebApplication

from apps.core.envelope import error_response, rendered_response
from apps.core.order_finance_browser import (
    AUTH_FAILURE,
    AUTH_SCHEME,
    GRANT_ENVIRON_KEY,
    browser_config,
    verify_grant,
)

_BROWSER_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_BROWSER_HEADERS = {"authorization", "content-type"}
_SAME_ORIGIN_SCREENSHOT = "/api/business/screenshot-browser"
_REUSABLE_READ = re.compile(r"/api/business/(?:hall/|my-orders/|orders/(?:[0-9]{20}/)?)")


def _cors_headers(origin, preflight=False, *, retry_after=False):
    headers = [("Vary", "Origin"), ("Cache-Control", "no-store")]
    if origin:
        headers.append(("Access-Control-Allow-Origin", origin))
    if preflight:
        headers.append(("Vary", "Access-Control-Request-Method, Access-Control-Request-Headers"))
    elif origin:
        exposed = "Content-Disposition, Retry-After" if retry_after else "Content-Disposition"
        headers.append(("Access-Control-Expose-Headers", exposed))
    return headers


def _browser_error(start_response, status, message, origin=None, head=False):
    response = rendered_response(error_response(status.value, message, status.value))
    headers = _cors_headers(origin)
    headers.append(("Content-Type", response["Content-Type"]))
    headers.append(("Content-Length", str(len(response.content))))
    if status is HTTPStatus.UNAUTHORIZED:
        headers.append(("WWW-Authenticate", AUTH_SCHEME))
    start_response(f"{status.value} {status.phrase}", headers)
    return [] if head else [response.content]


def _preflight(environ, start_response, origin):
    method = environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "")
    raw_headers = environ.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS", "")
    requested = [header.strip().lower() for header in raw_headers.split(",")] if raw_headers else []
    if (
        method not in _BROWSER_METHODS
        or len(requested) != len(set(requested))
        or set(requested) - _BROWSER_HEADERS
    ):
        return _browser_error(start_response, HTTPStatus.FORBIDDEN, "订单财务直连预检未放行", origin)
    headers = _cors_headers(origin, preflight=True)
    headers.append(("Access-Control-Allow-Methods", method))
    if requested:
        headers.append(("Access-Control-Allow-Headers", ", ".join(requested)))
    start_response("204 No Content", headers)
    return []


class OrderFinanceApplication:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        config = browser_config()
        if config is not None and environ.get("HTTP_HOST") == config.service_host:
            return self._browser_request(config, environ, path, start_response)
        if not serves_order_finance(path):
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [b"Not found"]
        return self.application(environ, start_response)

    def _browser_request(self, config, environ, path, start_response):
        origin = environ.get("HTTP_ORIGIN")
        method = environ.get("REQUEST_METHOD", "GET")
        head = method == "HEAD"
        if origin not in config.origins:
            return _browser_error(start_response, HTTPStatus.FORBIDDEN, "当前域名未开通订单财务直连", head=head)
        if (not path.startswith("/api/") or not serves_order_finance(path)
                or path == _SAME_ORIGIN_SCREENSHOT or path.startswith(_SAME_ORIGIN_SCREENSHOT + "/")):
            return _browser_error(start_response, HTTPStatus.NOT_FOUND, "订单财务路径不存在", origin, head)
        if environ.get("HTTP_COOKIE"):
            return _browser_error(start_response, HTTPStatus.FORBIDDEN, "订单财务直连不接受会话 Cookie", origin, head)
        if method == "OPTIONS":
            return _preflight(environ, start_response, origin)
        if method not in _BROWSER_METHODS:
            return _browser_error(start_response, HTTPStatus.METHOD_NOT_ALLOWED, "请求方法不允许", origin)
        grant = verify_grant(config, origin, environ.get("HTTP_AUTHORIZATION", ""))
        if grant is None:
            return _browser_error(start_response, HTTPStatus.UNAUTHORIZED, AUTH_FAILURE, origin, head)

        # Bind the verified tenant before the usual tenant middleware or shared database router sees the request.
        environ = environ.copy()
        environ["HTTP_HOST"] = origin.removeprefix("https://")
        for name in ("HTTP_AUTHORIZATION", "HTTP_COOKIE", "HTTP_X_FORWARDED_HOST"):
            environ.pop(name, None)
        environ[GRANT_ENVIRON_KEY] = grant

        def browser_response(status, headers, exc_info=None):
            headers = [
                (name, value) for name, value in headers
                if name.lower() not in {"cache-control", "set-cookie"}
                and not name.lower().startswith("access-control-")
            ]
            cors_headers = _cors_headers(origin, retry_after=any(name.lower() == "retry-after" for name, _ in headers))
            return start_response(status, headers + cors_headers, exc_info)

        if (
            method == "GET"
            and grant.schema in settings.TT_ORDER_FINANCE_REUSE_SCHEMAS
            and _REUSABLE_READ.fullmatch(path)
            and isinstance(self.application, SharedWebApplication)
        ):
            return self.application(environ, browser_response, reuse_schema=grant.schema)
        return self.application(environ, browser_response)


application = OrderFinanceApplication(web_application)
