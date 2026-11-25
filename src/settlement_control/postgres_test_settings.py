import os
import uuid

import environ

from .test_settings import *  # noqa: F403

environ.Env.read_env(os.environ.get('SETTLEMENT_TEST_ENV_FILE', '.env'))
e = environ.Env()
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': e('POSTGRES_DB'), 'USER': e('POSTGRES_USER'), 'PASSWORD': e('POSTGRES_PASSWORD'), 'HOST': e('POSTGRES_HOST'), 'PORT': e('POSTGRES_PORT'), 'TEST': {'NAME': 'test_settlecontrol_' + uuid.uuid4().hex[:12]}}}
