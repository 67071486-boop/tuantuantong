import base64
import json
from time import monotonic, sleep

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from . import matrix_bridge


class ChatJoinRequired(APIException):
    status_code = 409
    default_detail = '请先加入会话'


def enabled():
    backend = getattr(settings, 'TT_CHAT_CORE_BACKEND', 'legacy')
    if backend == 'legacy':
        return False
    if backend != 'service':
        raise matrix_bridge.ChatServiceUnavailable()
    try:
        config = matrix_bridge.tenant_config()
    except ImproperlyConfigured as exc:
        raise matrix_bridge.ChatServiceUnavailable() from exc
    if config is None:
        raise matrix_bridge.ChatServiceUnavailable()
    backend = config.get('core_backend', 'service')
    if backend not in ('legacy', 'service'):
        raise matrix_bridge.ChatServiceUnavailable()
    return backend == 'service'


def call(operation, viewer_id=None, *, query=None, payload=None, readonly=False):
    from chat_service.protocol import sign_request
    from chat_service.config import internal_url

    try:
        config = matrix_bridge.tenant_config()
        if config is None:
            raise matrix_bridge.ChatServiceUnavailable()
        url = internal_url(config['service_url']) + '/v1/core'
        body = json.dumps({'operation': operation, 'viewer_id': viewer_id, 'query': query or {},
                           'payload': payload or {}, 'readonly': readonly},
                          separators=(',', ':'), allow_nan=False).encode()
        headers = sign_request(config['secret'], config['key_id'], 'POST', '/v1/core', body)
        headers = {**headers, 'Content-Type': 'application/json'}
        deadline = monotonic() + 5
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise matrix_bridge.ChatServiceUnavailable()
            response = httpx.post(url, content=body, headers=headers, timeout=remaining, trust_env=False)
            envelope = response.json()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise matrix_bridge.ChatServiceUnavailable()
            if not isinstance(envelope, dict):
                raise ValueError('Invalid core envelope')
            if (response.status_code == 503 and envelope.get('code') == 503
                    and envelope.get('msg') == 'Tenant synchronization is busy'):
                sleep(min(0.05, remaining))
                continue
            break
        if response.status_code == envelope.get('code'):
            if response.status_code == 404:
                raise NotFound()
            if response.status_code == 403:
                raise PermissionDenied('订单权限已撤销' if envelope.get('msg') == '订单权限已撤销' else '当前账号无权执行这项聊天操作')
            if response.status_code == 400:
                raise ValidationError('聊天参数有误')
        if response.status_code == 409 and envelope.get('code') == 409 and envelope.get('msg') == '请先加入会话':
            raise ChatJoinRequired()
        if response.status_code == 503 and envelope.get('code') == 503 and envelope.get('msg') == '内部沟通暂时不可用':
            from .maintenance import ChatMaintenanceUnavailable
            raise ChatMaintenanceUnavailable()
        if response.status_code == 503 and envelope.get('code') == 503 and envelope.get('msg') == '暂时无法确认订单权限，请稍后重试':
            from .order_authority import OrderAuthorityUnavailable
            raise OrderAuthorityUnavailable()
        response.raise_for_status()
        if envelope.get('code') != 0 or not isinstance(envelope.get('data'), dict):
            raise ValueError('Core returned failure')
        data = envelope['data']
        required = {'start': ('id',), 'order': ('id', 'last_message'), 'join': ('id',),
                    'history': ('messages', 'has_more', 'has_newer'),
                    'notification': ('messages', 'has_more', 'has_newer', 'kind', 'self_member_id', 'can_reply'),
                    'search': ('messages', 'count', 'has_more'), 'image': ('mime', 'data'),
                    'read': ('read_seq',), 'preferences': ('pinned', 'remark', 'marked_unread'),
                    'revoke': ('id', 'revoked'), 'pin': ('pinned_message_id',),
                    'sync': ('changes', 'cursor', 'has_more'), 'peer': ('peer_id',),
                    'unread': ('unread_count',), 'conversations': ('conversations', 'cursor'),
                    'reopen': ('id', 'last_message'), 'order_preview': ('id','last_message','has_conversation'),
                    'order_send': ('conversation','messages'), 'policy': ('policy_version',)}
        if any(key not in data for key in required.get(operation, ())):
            raise ValueError('Core returned incomplete data')
        if operation == 'policy' and (type(data['policy_version']) is not int or data['policy_version'] < 1):
            raise ValueError('Core returned invalid policy version')
        if operation == 'order_send' and not (
                isinstance(data.get('conversation'),dict) and type(data['conversation'].get('id')) is int
                and isinstance(data.get('messages'),list) and data['messages']
                and all(isinstance(row,dict) and 'id' in row and 'seq' in row for row in data['messages'])):
            raise ValueError('Core did not confirm received order messages')
        if operation == 'send' and not (
                'id' in data and 'seq' in data or isinstance(data.get('messages'), list) and data['messages']
                and all(isinstance(row, dict) and 'id' in row and 'seq' in row for row in data['messages'])):
            raise ValueError('Core did not confirm received messages')
        return data
    except (ImproperlyConfigured, OSError, ValueError, TypeError, AttributeError, httpx.HTTPError) as exc:
        raise matrix_bridge.ChatServiceUnavailable() from exc


def operation(name, user_id, conversation_id=None, *, query=None, payload=None, readonly=False):
    params = {**(query or {})}
    if conversation_id is not None:
        params['conversation_id'] = conversation_id
    return call(name, user_id, query=params, payload=payload, readonly=readonly)


def start(user, peer_id):
    from . import service

    if user.pk == peer_id:
        raise ValidationError({'peer_id': '请选择其他成员'})
    peer = get_object_or_404(service._active_contacts(), pk=peer_id)
    if service._uses_worker_directory(user) and not service._can_start_worker_dm(peer_id):
        rows = operation('conversations', user.pk)['conversations']
        if not any(row.get('peer', {}).get('id') == peer_id for row in rows):
            raise PermissionDenied('请从打手通讯录发起新私聊')
    return call('start', user.pk, payload={'viewer': service.person_payload(user),
                                         'peer': service.person_payload(peer)})


def send(user, conversation_id, **values):
    from . import service

    if service._uses_worker_directory(user):
        conversation = operation('order', user.pk, conversation_id)
        if conversation.get('kind') != 'order':
            peer = conversation.get('peer')
            if not isinstance(peer, dict) or type(peer.get('id')) is not int:
                raise matrix_bridge.ChatServiceUnavailable()
            if conversation['last_message'] is None and not service._can_start_worker_dm(peer['id']):
                raise PermissionDenied('请从打手通讯录发起新私聊')
    payload = _message_payload(values)
    return operation('send', user.pk, conversation_id, payload=payload)


def _message_payload(values):
    payload = {key: value for key, value in values.items() if value is not None}
    for field in ('image', 'images'):
        if field not in payload:
            continue
        images = payload[field] if field == 'images' else [payload[field]]
        encoded = [{'mime': row['mime'], 'data': base64.b64encode(row['data']).decode()}
                   for row in images]
        payload[field] = encoded if field == 'images' else encoded[0]
    return payload


def order_preview(user_id, order_no):
    return call('order_preview',user_id,query={'order_no':order_no})


def order_send(user, order_no, **values):
    from . import service
    return call('order_send',user.pk,query={'order_no':order_no},
                payload={'viewer':service.person_payload(user),'message':_message_payload(values)})


def join(user, group):
    from . import service
    return call('join', user.pk, query={'conversation_id': group.conversation_id},
                payload={'grant': True, 'viewer': service.person_payload(user)})
