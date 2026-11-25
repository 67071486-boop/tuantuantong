"""独立的结算授权控制服务设置；不载入租户或平台控制台。"""
import json
import os

SECRET_KEY = os.environ.get('SETTLEMENT_DJANGO_SECRET', '')
DEBUG = False
ALLOWED_HOSTS = [s for s in os.environ.get('SETTLEMENT_ALLOWED_HOSTS', '').split(',') if s]
ROOT_URLCONF = 'settlement_control.urls'
INSTALLED_APPS = ['apps.settlement_control']
MIDDLEWARE = ['apps.settlement_control.middleware.host_validation']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': os.environ.get('SETTLEMENT_DB_NAME', ''), 'USER': os.environ.get('SETTLEMENT_DB_USER', ''), 'PASSWORD': os.environ.get('SETTLEMENT_DB_PASSWORD', ''), 'HOST': os.environ.get('SETTLEMENT_DB_HOST', 'localhost'), 'PORT': os.environ.get('SETTLEMENT_DB_PORT', '5432'), 'CONN_MAX_AGE': 0}}
SETTLEMENT_CLIENT_KEYS = json.loads(os.environ.get('SETTLEMENT_CLIENT_KEYS', '{}'))
SETTLEMENT_CLIENT_NAMES = json.loads(os.environ.get('SETTLEMENT_CLIENT_NAMES', '{}'))
SETTLEMENT_FERNET_KEY = os.environ.get('SETTLEMENT_FERNET_KEY', '')
SETTLEMENT_OPERATION_LOCK_FILE = os.environ.get('SETTLEMENT_OPERATION_LOCK_FILE', '')
SETTLEMENT_UPSTREAM_URL = os.environ.get('SETTLEMENT_UPSTREAM_URL', '')
SETTLEMENT_USERNAME = os.environ.get('SETTLEMENT_USERNAME', '')
SETTLEMENT_PASSWORD = os.environ.get('SETTLEMENT_PASSWORD', '')
SETTLEMENT_MERCHANT = os.environ.get('SETTLEMENT_MERCHANT', '')
SETTLEMENT_TESSERACT = os.environ.get('SETTLEMENT_TESSERACT', '')
SETTLEMENT_ADAPTER = os.environ.get('SETTLEMENT_ADAPTER', 'apps.settlement_control.upstream.JeepayAdapter')
USE_TZ = True

# httpx 默认 INFO 日志可能包含上游 URL 中的账户及姓名。
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'loggers': {'httpx': {'handlers': [], 'level': 'CRITICAL', 'propagate': False}, 'httpcore': {'handlers': [], 'level': 'CRITICAL', 'propagate': False}}}
