import re
from secrets import compare_digest, token_hex
from time import time
from urllib.parse import urlsplit

from django.db import connection
from rest_framework import serializers

from apps.business.api.dailiantong_push_views import _not_found
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.wecom import sdk
from apps.wecom.archive_order_session import ORDER_ARCHIVE_VIEWER_SESSION_KEY, order_archive_viewer_userid
from apps.wecom.archive_views import (
    ARCHIVE_VIEWER_SESSION_KEY,
    ArchiveOrderView,
    ArchiveView,
    _archive_unavailable,
    _config,
    archive_order,
    archive_viewer_enabled,
    archive_viewer_userid,
    archive_viewer_verified,
    order_archive_config,
)
from apps.wecom.client_lab import _sign, _ticket
from apps.wecom.group_sync import TimedSession

_LOGIN_STATE = 'wecom_archive_login_state'
_ORDER_LOGIN_STATE = 'wecom_order_archive_login_state'
_ORDER_PAGE_PATH = re.compile(r'/v2(?:/[\w-]+)*/?')


class SignatureQuery(serializers.Serializer):
    url = serializers.CharField(max_length=2048, trim_whitespace=False)
    reauthenticate = serializers.BooleanField(required=False, default=False)


class LoginInput(serializers.Serializer):
    code = serializers.CharField(max_length=512, trim_whitespace=False)
    state = serializers.RegexField(r'^[a-f0-9]{48}$')


def _valid_url(request, value):
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == 'https'
            and parsed.netloc.lower() == request.get_host().lower()
            and _ORDER_PAGE_PATH.fullmatch(parsed.path)
            and not parsed.fragment
            and '#' not in value
        )
    except ValueError:
        return False


def _remote_client(config):
    client = sdk.get_client(config, timeout=(5, 10))
    client._http.close()
    client._http = TimedSession()
    return client


def _error(msg):
    return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=msg)


def _login_identity(request, config):
    return [connection.schema_name, str(request.user.pk), config.corp_id, config.agent_id, config.identity_revision]


def _current_challenge(challenge, identity, now):
    return (
        isinstance(challenge, dict) and challenge.get('identity') == identity
        and type(challenge.get('expires_at')) is int and challenge['expires_at'] > now
        and isinstance(challenge.get('state'), str) and challenge['state'].isascii()
    )


def _official_employee_userid(result):
    if not isinstance(result, dict) or type(result.get('errcode')) is not int or result['errcode'] != 0:
        return ''
    userid = result.get('userid') or result.get('UserId')
    # WeCom qualifies linked-enterprise users as CorpId/userid.
    if not isinstance(userid, str) or not userid or not userid.isprintable() or '/' in userid:
        return ''
    return userid if len(userid) <= 256 and userid == userid.strip() else ''


class _ArchiveAuthView(ArchiveView):
    def _archive_config(self, request, order_no):
        if order_no is None:
            return _config() if archive_viewer_enabled(request.user) else None
        return order_archive_config() if archive_order(request, order_no) is not None else None


class ArchiveSignatureView(_ArchiveAuthView):
    def get(self, request, order_no=None):
        config = self._archive_config(request, order_no)
        if config is None:
            return _archive_unavailable() if order_no is None else _not_found()
        query = SignatureQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        url = query.validated_data['url']
        if not _valid_url(request, url):
            return api_response(code=ErrorCode.VALIDATION, msg='企微签名只支持当前工作室的页面')

        client = _remote_client(config)
        try:
            corp_ticket = _ticket(client, config, 'corp')
            agent_ticket = _ticket(client, config, 'agent')
        except Exception:
            return _error('企微网页授权初始化失败，请检查应用可信域名及权限')
        finally:
            client._http.close()

        timestamp = int(time())
        nonce = token_hex(16)
        is_order = order_no is not None
        verified = False
        if not query.validated_data['reauthenticate']:
            verified = bool(is_order and order_archive_viewer_userid(request, config)) or archive_viewer_verified(
                request, config, allow_legacy=is_order,
            )
        state = ''
        if not verified:
            state_key = _ORDER_LOGIN_STATE if is_order else _LOGIN_STATE
            current = request.session.get(state_key)
            identity = _login_identity(request, config)
            if _current_challenge(current, identity, timestamp):
                state = current['state']
            else:
                state = token_hex(24)
                request.session[state_key] = {
                    'state': state, 'identity': identity, 'expires_at': timestamp + 300,
                }
        return api_response({
            'corp_id': config.corp_id, 'agent_id': config.agent_id, 'timestamp': timestamp, 'nonce': nonce,
            'corp_signature': _sign(corp_ticket, nonce, timestamp, url),
            'agent_signature': _sign(agent_ticket, nonce, timestamp, url),
            'verified': verified, 'login_state': state,
        })


class ArchiveLoginView(_ArchiveAuthView):
    def post(self, request, order_no=None):
        config = self._archive_config(request, order_no)
        if config is None:
            return _archive_unavailable() if order_no is None else _not_found()
        data = LoginInput(data=request.data)
        data.is_valid(raise_exception=True)
        is_order = order_no is not None
        state_key = _ORDER_LOGIN_STATE if is_order else _LOGIN_STATE
        viewer_key = ORDER_ARCHIVE_VIEWER_SESSION_KEY if is_order else ARCHIVE_VIEWER_SESSION_KEY
        state = request.session.get(state_key)
        identity = _login_identity(request, config)
        if (
            not _current_challenge(state, identity, time())
            or not compare_digest(state['state'], data.validated_data['state'])
        ):
            return _error('企微验证已过期，请重新打开聊天记录')

        request.session.pop(state_key, None)
        request.session.pop(viewer_key, None)
        client = _remote_client(config)
        try:
            result = client.get('auth/getuserinfo', params={'code': data.validated_data['code']})
        except Exception:
            return _error('企微身份验证失败，请重新验证')
        finally:
            client._http.close()
        if is_order:
            userid = _official_employee_userid(result)
            if not userid:
                return _error('请使用当前工作室的企微员工身份')
        else:
            userid = archive_viewer_userid(request.user)
            if not isinstance(result, dict) or result.get('errcode') != 0 or not userid or result.get('userid') != userid:
                return _error('请使用与团团通账号绑定的企微员工身份')
        request.session[viewer_key] = {
            'user_id': str(request.user.pk), 'wecom_userid': userid, 'corp_id': config.corp_id,
            'tenant_schema': connection.schema_name, 'agent_id': config.agent_id,
            'identity_revision': config.identity_revision, 'expires_at': int(time()) + 3600,
        }
        return api_response({'verified': True})


class OrderArchiveSignatureView(ArchiveSignatureView):
    permission_classes = ArchiveOrderView.permission_classes


class OrderArchiveLoginView(ArchiveLoginView):
    permission_classes = ArchiveOrderView.permission_classes
