import json
import re
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import parse_qsl, urlsplit

from .browser_ticket import verify_browser_ticket
from .config import ServiceError, integer
from .core_authority import UNAVAILABLE
from .core_utils import ID_END, fields
from .protocol import ProtocolError


BROWSER_PREFIX = '/v1/browser/'
MAX_BROWSER_BODY = 3 * 1024 * 1024
_NOTIFICATION_GROUPS = frozenset({'order-groups', 'app/order-groups'})
_CONVERSATION = re.compile(
    r'(conversations|(?:app/)?(?:order-groups|notification-conversations))/([1-9][0-9]{0,18})/(.*)'
)
_ORDER = re.compile(r'orders/([0-9]{20})/(.*)')
_IMAGE_OR_REVOKE = re.compile(r'messages/([1-9][0-9]{0,18})/(image|revoke)/')
_EMPTY_HISTORY = {'messages': [], 'has_more': False, 'has_newer': False}


@dataclass(frozen=True)
class BrowserRoute:
    operation: str
    conversation: int | None = None
    order_no: str | None = None
    message: int | None = None
    notification: str | None = None


_COMMON_ROUTES = {
    ('GET', 'conversations/'): 'conversations',
    ('GET', 'unread/'): 'unread',
    ('GET', 'sync/'): 'sync',
}
_CONVERSATION_ROUTES = {
    ('GET', 'messages/'): 'history',
    ('POST', 'messages/'): 'send',
    ('GET', 'messages/search/'): 'search',
    ('POST', 'read/'): 'read',
    ('POST', 'preferences/'): 'preferences',
    ('POST', 'pin/'): 'pin',
}
_ORDER_ROUTES = {
    ('GET', ''): 'order_preview',
    ('GET', 'messages/'): 'history',
    ('POST', 'messages/'): 'order_send',
    ('POST', 'reopen/'): 'reopen',
    ('POST', 'join/'): 'join',
}
_QUERY_FIELDS = {
    'conversations': {'limit', 'offset'},
    'history': {'before', 'around'},
    'notification': {'before', 'around'},
    'search': {'q', 'before', 'sender_id', 'date_from', 'date_to', 'image_only'},
    'sync': {'after'},
}
_MESSAGE_FIELDS = {'text', 'client_id', 'reply_to_id', 'image', 'images', 'mention_member_ids'}
_BODY_FIELDS = {
    'send': _MESSAGE_FIELDS,
    'order_send': _MESSAGE_FIELDS,
    'read': {'seq'},
    'preferences': {'pinned', 'remark', 'marked_unread'},
    'pin': {'message_id'},
}


def browser_origin(tenants, origin, host):
    return bool(origin and host and any(
        origin in config['browser']['origins'] and urlsplit(config['browser']['audience']).netloc == host
        for config in tenants.values() if 'browser' in config
    ))


def _image_route(method, suffix):
    match = _IMAGE_OR_REVOKE.fullmatch(suffix)
    if match and (method, match[2]) in (('GET', 'image'), ('POST', 'revoke')):
        return match[2], int(match[1])
    return None, None


def browser_route(method, path):
    if not path.startswith(BROWSER_PREFIX):
        return None
    suffix = path.removeprefix(BROWSER_PREFIX)
    common = _COMMON_ROUTES.get((method, suffix))
    if common:
        return BrowserRoute(common)
    matched = _CONVERSATION.fullmatch(suffix)
    if matched:
        root, conversation, action = matched.groups()
        operation, message = _image_route(method, action)
        operation = operation or _CONVERSATION_ROUTES.get((method, action))
        if root == 'conversations' and operation:
            return BrowserRoute(operation, conversation=int(conversation), message=message)
        if root != 'conversations' and operation in {'history', 'send', 'read', 'image'}:
            if operation == 'history':
                operation = 'notification'
            return BrowserRoute(operation, conversation=int(conversation), message=message, notification=root)
    matched = _ORDER.fullmatch(suffix)
    if matched:
        order_no, action = matched.groups()
        operation, message = _image_route(method, action)
        if operation == 'image':
            return BrowserRoute(operation, order_no=order_no, message=message)
        operation = _ORDER_ROUTES.get((method, action))
        if operation:
            return BrowserRoute(operation, order_no=order_no)
    return None


def browser_query(route, value):
    try:
        pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except ValueError as error:
        raise ServiceError('聊天查询参数有误') from error
    query = dict(pairs)
    if len(query) != len(pairs) or set(query) - _QUERY_FIELDS.get(route.operation, set()):
        raise ServiceError('聊天查询参数有误')
    if route.operation in {'history', 'notification'}:
        if {'before', 'around'} <= query.keys():
            raise ServiceError('只能指定一个历史消息定位方式')
        query = {key: integer(value, key) for key, value in query.items()}
    if 'image_only' in query:
        value = query['image_only'].lower()
        if value not in {'true', 'false', '0', '1'}:
            raise ServiceError('Invalid image filter')
        query['image_only'] = value in {'true', '1'}
    if 'q' in query:
        query['q'] = query['q'].strip()
    if route.operation == 'sync':
        query.setdefault('after', 0)
    return query


def browser_payload(operation, raw):
    try:
        payload = json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_json_fields) if raw else {}
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ServiceError('无效的聊天请求') from error
    if not isinstance(payload, dict):
        raise ServiceError('无效的聊天请求')
    fields(payload, _BODY_FIELDS.get(operation, set()))
    if operation in {'send', 'order_send'}:
        payload.setdefault('text', '')
        if isinstance(payload['text'], str):
            payload['text'] = payload['text'].strip()
        if payload.get('reply_to_id') is None:
            payload.pop('reply_to_id', None)
        else:
            payload['reply_to_id'] = integer(payload['reply_to_id'], 'reply_to_id')
        if 'mention_member_ids' in payload:
            mentions = payload['mention_member_ids']
            if not isinstance(mentions, list) or len(mentions) > 50:
                raise ServiceError('Invalid mentions')
            payload['mention_member_ids'] = [integer(value, 'mention') for value in mentions]
    if operation == 'preferences' and isinstance(payload.get('remark'), str):
        payload['remark'] = payload['remark'].strip()
    return payload


def _reject_constant(value):
    raise ValueError(value)


def _unique_json_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate JSON field')
    return result


class BrowserChat:
    def __init__(self, engine, authorization, origin, host):
        prefix, _, token = authorization.partition(' ')
        if prefix != 'Bearer':
            raise ProtocolError('需要聊天凭证')
        claims = verify_browser_ticket(token, engine.tenants, origin=origin, host=host)
        self.core = engine.core
        self.tenant = claims['key']
        self.viewer = claims['viewer']
        self.metadata = {key: claims[key] for key in ('viewer', 'origin', 'proof')}

    def authorize(self, **metadata):
        result = self.core._source_request(self.tenant, 'browser-authorize', self.metadata | metadata)
        status = result.get('status')
        if type(status) is not int:
            raise ServiceError(UNAVAILABLE, 503)
        if status == 401:
            raise ServiceError('聊天登录已过期', 401)
        if status == 403:
            raise ServiceError('没有聊天权限', 403)
        if status == 404:
            raise ServiceError('会话不存在或无权访问', 404)
        if (status != 200 or not isinstance(result.get('viewer'), dict)
                or result['viewer'].get('id') != self.viewer or type(result.get('worker_directory')) is not bool):
            raise ServiceError(UNAVAILABLE, 503)
        return result

    def call(self, operation, query=None, payload=None, *, readonly=False):
        body = {
            'operation': operation, 'viewer_id': self.viewer, 'query': query or {},
            'payload': payload or {}, 'readonly': readonly,
        }
        deadline = monotonic() + 5
        while True:
            try:
                return self.core.handle(self.tenant, body)
            except ServiceError as error:
                remaining = deadline - monotonic()
                if error.status != 503 or str(error) != 'Tenant synchronization is busy' or remaining <= 0:
                    raise
                sleep(min(0.05, remaining))

    def dispatch(self, route, query, payload):
        if route.order_no is not None:
            authority = self.authorize(order_no=route.order_no)
            return self.dispatch_order(route, query, payload, authority['viewer'])
        notification = ({'notification': 'app' if route.notification.startswith('app/') else 'web'}
                        if route.notification else {})
        authority = self.authorize(**notification)
        if route.conversation is not None:
            if not 0 < route.conversation <= ID_END or route.message is not None and not 0 < route.message <= ID_END:
                raise ServiceError('会话或消息不存在', 404)
            query['conversation_id'] = route.conversation
        if route.message is not None:
            query['message_id'] = route.message
        check_group = route.notification in _NOTIFICATION_GROUPS and route.operation != 'notification'
        if check_group or route.operation == 'send' and authority['worker_directory']:
            summary = self.call('order', {'conversation_id': route.conversation})
            if check_group and summary.get('kind') != 'order':
                raise ServiceError('会话不存在', 404)
            if (route.operation == 'send' and authority['worker_directory']
                    and summary.get('kind') != 'order' and summary['last_message'] is None):
                self.authorize(peer=summary['peer']['id'], **notification)
        result = self.call(route.operation, query, payload)
        if (route.operation == 'notification' and route.notification in _NOTIFICATION_GROUPS
                and result.get('kind') != 'order'):
            raise ServiceError('会话不存在', 404)
        return result

    def dispatch_order(self, route, query, payload, identity):
        requested_order = {'order_no': route.order_no}
        if route.operation == 'order_send':
            return self.call('order_send', requested_order, {'viewer': identity, 'message': payload})
        summary = self.call('order_preview', requested_order)
        if route.operation == 'order_preview':
            return summary
        cid = summary.get('id')
        if cid is None:
            if route.operation == 'history':
                return _EMPTY_HISTORY
            raise ServiceError('会话不存在', 404)
        query['conversation_id'] = cid
        if route.operation == 'join':
            return self.call('join', query, {'grant': True, 'viewer': identity})
        if route.operation == 'image':
            if not 0 < route.message <= ID_END:
                raise ServiceError('消息不存在', 404)
            query['message_id'] = route.message
        return self.call(route.operation, query, readonly=route.operation in {'history', 'image'})
