"""Issue and verify short-lived browser grants for the order and finance service."""
import json
import os
import re
import stat
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.authentication import TenantBoundAuthentication
from apps.core.envelope import api_response, error_response, rendered_response
from apps.core.order_finance_runtime import serve_verified_finance_request
from apps.platform.identifiers import validate_schema_name

TICKET_TTL = 120
AUTH_SCHEME = "OrderFinance"
GRANT_ENVIRON_KEY = "tuantuantong.order_finance_grant"
AUTH_FAILURE = "订单财务登录已过期"
PURPOSE = "order-finance.browser.v2"
_DB_SESSION_ENGINE = "django.contrib.sessions.backends.db"
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HTTPS_ORIGIN = re.compile(rf"https://{_HOST_LABEL}(?:\.{_HOST_LABEL})+")
_KEY = re.compile(r"[A-Za-z0-9_-]{43}=")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,2048}={0,2}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OriginGrant:
    schema: str
    cipher: Fernet = field(repr=False)


@dataclass(frozen=True)
class BrowserConfig:
    audience: str
    origins: dict[str, OriginGrant]

    @property
    def service_host(self):
        return self.audience.removeprefix("https://")


@dataclass(frozen=True)
class BrowserGrant:
    schema: str
    user_id: int
    session_key: str = field(repr=False)
    session_digest: str = field(repr=False)
    password_digest: str = field(repr=False)


def _unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate browser configuration field")
    return result


def _valid_origin(value):
    return isinstance(value, str) and len(value) <= 261 and _HTTPS_ORIGIN.fullmatch(value)


@lru_cache(maxsize=8)
@sensitive_variables()
def _load_browser_config(filename):
    try:
        path = Path(filename)
        if not path.is_absolute():
            raise ValueError("browser configuration requires an absolute path")
        # A read-only or read-write owner mode is sufficient. No group or public access.
        with path.open(encoding="utf-8") as file:
            mode = os.fstat(file.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("browser configuration is not private")
            content = json.load(file, object_pairs_hook=_unique_keys)
        if not isinstance(content, dict) or content.keys() != {"audience", "origins"}:
            raise ValueError("invalid browser configuration structure")
        audience, origins = content["audience"], content["origins"]
        if not _valid_origin(audience) or not isinstance(origins, dict) or not origins or audience in origins:
            raise ValueError("invalid browser origins")
        result = {}
        keys = {}
        for origin, config in origins.items():
            if not _valid_origin(origin) or not isinstance(config, dict) or config.keys() != {"schema", "key"}:
                raise ValueError("invalid browser origin structure")
            key = config["key"]
            schema = validate_schema_name(config["schema"])
            if not isinstance(key, str) or not _KEY.fullmatch(key) or keys.setdefault(key, schema) != schema:
                raise ValueError("invalid browser key or key reused across tenants")
            result[origin] = OriginGrant(schema, Fernet(key.encode("ascii")))
        return BrowserConfig(audience, result)
    except (OSError, TypeError, ValueError):
        raise ImproperlyConfigured("Invalid order finance browser configuration or permissions") from None


def browser_config():
    filename = settings.TT_ORDER_FINANCE_BROWSER_FILE
    if not filename:
        return None
    if settings.SESSION_ENGINE != _DB_SESSION_ENGINE:
        raise ImproperlyConfigured("Order finance browser grants require the Django database session engine")
    return _load_browser_config(filename)


@sensitive_variables()
def verify_grant(config, origin, authorization):
    scheme, _, token = authorization.partition(" ")
    if scheme != AUTH_SCHEME or not _TOKEN.fullmatch(token):
        return None
    source = config.origins[origin]
    try:
        raw = source.cipher.decrypt(token.encode("ascii"), ttl=TICKET_TTL)
        purpose, audience, bound_origin, schema, user_id, session_key, session_digest, password_digest = json.loads(raw)
        if (
            (purpose, audience, bound_origin, schema) != (PURPOSE, config.audience, origin, source.schema)
            or type(user_id) is not int or user_id <= 0
            or not isinstance(session_key, str) or not session_key
            or not isinstance(session_digest, str) or not _DIGEST.fullmatch(session_digest)
            or not isinstance(password_digest, str) or not _DIGEST.fullmatch(password_digest)
        ):
            return None
        return BrowserGrant(schema, user_id, session_key, session_digest, password_digest)
    except (InvalidToken, TypeError, ValueError):
        return None


@sensitive_variables()
def _live_session_data(session_key):
    return (
        Session.objects.filter(session_key=session_key, expire_date__gt=timezone.now())
        .values_list("session_data", flat=True)
        .first()
    )


@sensitive_variables()
def _digest(value):
    return sha256(value.encode()).hexdigest()


@sensitive_variables()
def _session_user(grant):
    # The order service compares the live row without needing the Web process's Django secret.
    session_data = _live_session_data(grant.session_key)
    if session_data is None or not constant_time_compare(_digest(session_data), grant.session_digest):
        return None
    user = get_user_model().objects.filter(pk=grant.user_id, is_active=True).first()
    if user is not None and constant_time_compare(_digest(user.password), grant.password_digest):
        return user
    return None


class OrderFinanceBrowserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        grant = request.META.get(GRANT_ENVIRON_KEY)
        if not isinstance(grant, BrowserGrant):
            return self.get_response(request)
        tenant = getattr(request, "tenant", None)
        if tenant is not None and tenant.is_active and connection.schema_name == grant.schema == tenant.schema_name:
            user = _session_user(grant)
            if user is not None:
                request.user = request._cached_user = user
                request.order_finance_browser_authenticated = True
                return serve_verified_finance_request(request, self.get_response, grant.schema)
        response = error_response(401, AUTH_FAILURE, 401, headers={"WWW-Authenticate": AUTH_SCHEME})
        return rendered_response(response)


@method_decorator(never_cache, name="dispatch")
class OrderFinanceTicketView(APIView):
    authentication_classes = [TenantBoundAuthentication]
    permission_classes = [IsAuthenticated]
    http_method_names = ["post"]

    @sensitive_variables()
    def post(self, request):
        config = browser_config()
        origin = "https://" + request.get_host().lower()
        source = config.origins.get(origin) if config else None
        if source is None:
            return api_response({"enabled": False})
        tenant = getattr(request, "tenant", None)
        if (
            request.headers.get("Origin") != origin or tenant is None or not tenant.is_active
            or connection.schema_name != tenant.schema_name or source.schema != tenant.schema_name
        ):
            raise PermissionDenied("当前域名未开通订单财务直连")
        session_key = request.session.session_key
        session_data = _live_session_data(session_key) if session_key else None
        if session_data is None:
            raise AuthenticationFailed(AUTH_FAILURE)
        # Recheck the row that is about to be sealed in case the session changed during authentication.
        stored = SessionStore().decode(session_data)
        user = request.user
        if (
            stored.get("identity_plane") != source.schema
            or stored.get(SESSION_KEY) != str(user.pk)
            or stored.get(BACKEND_SESSION_KEY) not in settings.AUTHENTICATION_BACKENDS
            or not constant_time_compare(stored.get(HASH_SESSION_KEY, ""), user.get_session_auth_hash())
        ):
            raise AuthenticationFailed(AUTH_FAILURE)
        payload = [
            PURPOSE, config.audience, origin, source.schema, user.pk, session_key,
            _digest(session_data), _digest(user.password),
        ]
        token = source.cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")
        return api_response({"enabled": True, "token": token, "ttl_seconds": TICKET_TTL, "user_id": request.user.pk})
