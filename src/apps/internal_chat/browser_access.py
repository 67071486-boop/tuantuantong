import base64
import json
from importlib import import_module
from types import SimpleNamespace

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.http import Http404
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import IsAuthenticated

from apps.accounts.models import AppToken
from apps.core.app_version import AndroidUpdateRequired
from apps.core.authentication import AppTokenAuthentication, TenantBoundAuthentication
from apps.core.envelope import api_response
from chat_service.browser_ticket import TICKET_LIFETIME, issue_browser_ticket, validate_browser

from . import core_bridge, matrix_bridge, service
from .app_access import AppChatAuthentication, AppChatSession
from .order_authority import SignedServiceView, StrictInput
from .views import AppChatView, OrderChatView


def _session_reference_cipher():
    key = salted_hmac('internal-chat.browser.source-session.v1', 'credential reference', algorithm='sha256').digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _backing_reference(request):
    if isinstance(request.auth, AppChatSession):
        kind, credential = 'chat', request.META.get('HTTP_AUTHORIZATION', '')
        headers = {'User-Agent': request.headers.get('User-Agent', '')[:256]}
    elif isinstance(request.auth, AppToken):
        kind, credential = 'app', request.META.get('HTTP_AUTHORIZATION', '')
        headers = {name: request.headers.get(name, '')[:32]
                   for name in ('User-Agent', 'X-TT-App-Platform', 'X-TT-App-Version-Code')}
    else:
        kind, credential, headers = 'web', request.session.session_key, {}
    return [connection.schema_name, request.user.pk, kind, credential, headers]


def _backing_user(reference, request, viewer_id):
    try:
        raw = _session_reference_cipher().decrypt(reference.encode('ascii'), ttl=TICKET_LIFETIME)
        schema, subject, kind, credential, headers = json.loads(raw)
        if (schema != connection.schema_name or subject != viewer_id
                or not isinstance(credential, str) or not credential):
            return None, None
        if kind in ('app', 'chat'):
            source_request = SimpleNamespace(META={'HTTP_AUTHORIZATION': credential}, tenant=request.tenant,
                                             headers=headers, method='GET')
            auth = AppTokenAuthentication() if kind == 'app' else AppChatAuthentication()
            authenticated = auth.authenticate(source_request)
            if authenticated and authenticated[0].pk == viewer_id:
                return authenticated[0], kind
        if kind == 'web':
            session_store = import_module(settings.SESSION_ENGINE).SessionStore
            session = session_store(session_key=credential)
            user = get_user_model().objects.filter(pk=viewer_id, is_active=True).first()
            if (user is not None and session.get('identity_plane') == connection.schema_name
                    and session.get(SESSION_KEY) == str(viewer_id)
                    and constant_time_compare(session.get(HASH_SESSION_KEY, ''), user.get_session_auth_hash())):
                return user, kind
    except (AndroidUpdateRequired, AuthenticationFailed, AttributeError, InvalidToken, TypeError, ValueError):
        pass
    return None, None


def _browser_config():
    tenant = matrix_bridge.tenant_config()
    if tenant is None or 'browser' not in tenant:
        return None
    browser = validate_browser(tenant['browser'])
    return tenant, browser


def _response(data):
    response = api_response(data)
    add_never_cache_headers(response)
    return response


@method_decorator(never_cache, name='dispatch')
class DirectChatTicketView(AppChatView):
    authentication_classes = [AppChatAuthentication, AppTokenAuthentication, TenantBoundAuthentication]
    permission_classes = [IsAuthenticated]
    http_method_names = ['post', 'options']
    requires_order_groups = False

    def post(self, request):
        if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
            return _response({'enabled': False})
        try:
            config = _browser_config()
        except (ImproperlyConfigured, ValueError) as error:
            raise matrix_bridge.ChatServiceUnavailable() from error
        if config is None or not core_bridge.enabled():
            return _response({'enabled': False})
        tenant, browser = config
        origin = 'https://' + request.get_host().lower()
        if origin not in browser['origins'] or request.headers.get('Origin', origin) != origin:
            raise PermissionDenied('当前域名未开通聊天直连')
        reference = _backing_reference(request)
        if not reference[3]:
            raise AuthenticationFailed('聊天登录已过期')
        proof = _session_reference_cipher().encrypt(json.dumps(reference, separators=(',', ':')).encode()).decode('ascii')
        token, expires = issue_browser_ticket(key_id=tenant['key_id'], secret=tenant['secret'],
                                              viewer=request.user.pk, origin=origin,
                                              audience=browser['audience'], proof=proof)
        return _response({'enabled': True, 'token': token, 'expires_at': expires,
                          'ttl_seconds': TICKET_LIFETIME, 'user_id': request.user.pk})


class BrowserAuthorityInput(StrictInput):
    viewer = serializers.IntegerField(min_value=1, max_value=999_999_999)
    origin = serializers.CharField(max_length=270)
    proof = serializers.CharField(max_length=2048, trim_whitespace=False)
    order_no = serializers.RegexField(r'^[0-9]{20}$', required=False)
    peer = serializers.IntegerField(min_value=1, max_value=999_999_999, required=False)
    notification = serializers.ChoiceField(choices=('app', 'web'), required=False)


class ServiceBrowserAuthorityView(SignedServiceView):
    def post(self, request):
        schema = BrowserAuthorityInput(data=request.data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        try:
            config = _browser_config()
        except ValueError as error:
            raise matrix_bridge.ChatServiceUnavailable() from error
        if (settings.TT_CHAT_MAINTENANCE or not settings.TT_CHAT_ORDER_GROUPS_ENABLED
                or config is None or not core_bridge.enabled()):
            return _response({'status': 503})
        _, browser = config
        if data['origin'] not in browser['origins'] or not getattr(getattr(request, 'tenant', None), 'is_active', False):
            return _response({'status': 403})
        user, kind = _backing_user(data['proof'], request, data['viewer'])
        if user is None:
            return _response({'status': 401})
        restricted = service._uses_worker_directory(user)
        notification = data.get('notification')
        if (notification == 'app' and not (kind == 'app' and restricted)
                or notification == 'web' and kind != 'web'
                or kind == 'app' and notification != 'app'):
            return _response({'status': 404})
        if 'peer' in data and restricted and not service._can_start_worker_dm(data['peer']):
            return _response({'status': 403})
        if 'order_no' in data:
            request.user = user
            try:
                OrderChatView().get_order(request, data['order_no'])
            except (Http404, PermissionDenied):
                return _response({'status': 404})
        return _response({'status': 200, 'viewer': service.person_payload(user), 'worker_directory': restricted})
