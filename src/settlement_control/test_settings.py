from .settings import *  # noqa: F403

SECRET_KEY = 'test-only'
ALLOWED_HOSTS = ['testserver']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
SETTLEMENT_CLIENT_KEYS = {'client_a': 'a' * 48, 'client_b': 'b' * 48}
SETTLEMENT_CLIENT_NAMES = {'client_a': '测试系统甲', 'client_b': '测试系统乙'}
SETTLEMENT_FERNET_KEY = 'MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA='

import os
import tempfile

_lock_fd, SETTLEMENT_OPERATION_LOCK_FILE = tempfile.mkstemp(prefix='settlement-control-lock-')
os.close(_lock_fd)
