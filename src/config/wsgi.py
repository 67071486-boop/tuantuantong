import os

from django.conf import settings
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

if settings.TT_SHARED_WEB_ROUTES_FILE:
    from config.shared_web import SharedWebApplication

    application = SharedWebApplication(application, settings.TT_SHARED_WEB_ROUTES_FILE)
