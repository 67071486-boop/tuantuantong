"""企业微信工作台测试页的签名和客户群查询。"""
from hashlib import sha1, sha256
from secrets import token_hex
from urllib.parse import urlsplit
import time

from django.core.cache import cache
from django.db import connection
from rest_framework import serializers

from apps.accounts.permissions import is_account_admin
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.wecom import sdk
from apps.wecom.group_sync import TimedSession
from apps.wecom.models import WecomGroupChat
from apps.wecom.views import WecomSettingsGateView, get_ready_config


class LabSignatureQuery(serializers.Serializer):
    url = serializers.CharField(max_length=2048, trim_whitespace=False)


class LabGroupQuery(serializers.Serializer):
    name = serializers.CharField(max_length=512, trim_whitespace=False)


def _allowed_url(request, value):
    try:
        parsed = urlsplit(value)
        host = request.get_host().lower()
        local = parsed.hostname in ('localhost', '127.0.0.1', '::1')
        return (parsed.netloc.lower() == host and not parsed.username and not parsed.password
                and parsed.scheme in (('http', 'https') if local else ('https',))
                and parsed.path == '/wecom-client-lab' and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _ticket(client, config, kind):
    identity = f'{connection.settings_dict["NAME"]}|{connection.schema_name}|{config.corp_id}|{config.agent_id}|{config.identity_revision}|{config.secret}|{kind}'
    key = 'wecom-lab-ticket:' + sha256(identity.encode()).hexdigest()
    ticket = cache.get(key)
    if ticket:
        return ticket
    method = client.jsapi.get_ticket if kind == 'corp' else client.jsapi.get_agent_ticket
    try:
        data = method()
    except Exception as exc:
        if getattr(exc, 'errcode', None) not in (40001, 40014, 42001):
            raise
        client.fetch_access_token()
        data = method()
    if not isinstance(data, dict) or data.get('errcode') != 0 or not isinstance(data.get('ticket'), str) or not data['ticket']:
        raise ValueError('企微未返回有效票据')
    seconds = data.get('expires_in')
    if type(seconds) is not int or seconds < 1:
        raise ValueError('企微票据有效期无效')
    cache.set(key, data['ticket'], timeout=max(1, min(seconds - 60, 7000)))
    return data['ticket']


def _sign(ticket, nonce, timestamp, url):
    value = f'jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}'
    return sha1(value.encode('utf-8')).hexdigest()


class LabSignatureView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        if not is_account_admin(request.user):
            self.permission_denied(request)
        query = LabSignatureQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        url = query.validated_data['url']
        if not _allowed_url(request, url):
            return api_response(code=ErrorCode.VALIDATION, msg='页面地址不属于当前工作室测试页')
        config = get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg='请先启用并配置企微应用')
        client = sdk.get_client(config, timeout=(5, 10), auto_retry=False)
        client._http.close()
        client._http = TimedSession()
        try:
            corp_ticket = _ticket(client, config, 'corp')
            agent_ticket = _ticket(client, config, 'agent')
        except Exception as exc:
            code = getattr(exc, 'errcode', None)
            suffix = f'（企微错误 {code}）' if type(code) is int else ''
            return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg='企微授权初始化失败，请检查应用权限与网络' + suffix)
        finally:
            client._http.close()
        timestamp = int(time.time())
        nonce = token_hex(16)
        return api_response({'corp_id': config.corp_id, 'agent_id': config.agent_id,
                             'timestamp': timestamp, 'nonce': nonce,
                             'corp_signature': _sign(corp_ticket, nonce, timestamp, url),
                             'agent_signature': _sign(agent_ticket, nonce, timestamp, url)})


class LabGroupsView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        if not is_account_admin(request.user):
            self.permission_denied(request)
        query = LabGroupQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        config = get_ready_config()
        if config is None:
            return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg='请先启用并配置企微应用')
        rows = list(WecomGroupChat.objects.filter(identity_revision=config.identity_revision,
                    dismissed=False, pending_dismissal=False, name=query.validated_data['name'])
                    .order_by('id').values('chat_id', 'name')[:6])
        return api_response({'groups': rows[:5], 'too_many': len(rows) > 5})
