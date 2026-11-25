"""Dedicated WSGI entry for external platforms and OpenAPI."""
from config.external_platform_routes import serves_external_platform
from config.wsgi import application as web_application


class ExternalPlatformApplication:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        if not serves_external_platform(environ.get("PATH_INFO", "")):
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [b"Not found"]
        return self.application(environ, start_response)


application = ExternalPlatformApplication(web_application)
