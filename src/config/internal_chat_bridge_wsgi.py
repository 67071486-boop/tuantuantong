"""Keep internal chat callbacks on a dedicated listener."""
from config.wsgi import application as web_application

_ALLOWED_PATHS = frozenset({
    "/healthz",
    "/api/internal-chat/service/browser-authorize/",
    "/api/internal-chat/service/export/",
    "/api/internal-chat/service/snapshot/",
    "/api/internal-chat/service/order-authorizations/",
    "/api/internal-chat/service/order-intents/",
    "/api/internal-chat/service/peer-chat/",
})


def application(environ, start_response):
    if environ.get("PATH_INFO") not in _ALLOWED_PATHS:
        start_response("404 Not Found", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
        return [b"Not found"]
    return web_application(environ, start_response)
