from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db.models import Prefetch, Q
from django.utils.cache import add_never_cache_headers
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.business.api.my_orders_views import MyOrdersViewSet
from apps.business.api.order_views import OrderViewSet
from apps.business.models import EmpType, Order, OrderStatus, PeerDownstreamBinding, Performance
from apps.core.envelope import api_response
from apps.rbac.permissions import nodes_of

from . import matrix_bridge
from .models import GroupMember, Message, OrderChatIntent, OrderGroup


class OrderAuthorityUnavailable(APIException):
    status_code = 503
    default_detail = '暂时无法确认订单权限，请稍后重试'


class StrictInput(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError({'non_field_errors': ['订单权限参数有误']})
        return super().to_internal_value(data)


class AuthorityCheck(StrictInput):
    conversation_id = serializers.IntegerField(min_value=1, max_value=2_000_000_000, required=False)
    order_no = serializers.RegexField(r'^[0-9]{20}$', required=False)
    user_ids = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=999_999_999), max_length=1000)

    def validate(self, data):
        if not ({'conversation_id', 'order_no'} & data.keys()):
            raise serializers.ValidationError('需要订单号或已有会话编号')
        return data


class AuthorityInput(StrictInput):
    checks = AuthorityCheck(many=True, max_length=10000)
    viewer_id = serializers.IntegerField(min_value=1, max_value=999_999_999, required=False)
    discover = serializers.BooleanField(default=False)

    def validate(self, data):
        if data['discover'] and 'viewer_id' not in data:
            raise serializers.ValidationError('发现会话需要当前用户')
        checks = data['checks']
        keys = [(row.get('conversation_id'), row.get('order_no')) for row in checks]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError('订单群重复')
        if sum(len(row['user_ids']) for row in checks) > 100000:
            raise serializers.ValidationError('订单权限检查数量过多')
        return data


def _operator_orders(user):
    view = OrderViewSet()
    view.action, view.request = 'retrieve', SimpleNamespace(user=user)
    return view.get_queryset({}).values('pk')


def _worker_orders(user):
    return Performance.objects.filter(user=user, emp_type=EmpType.MAIN).values('order_id')


def _groups(order_ids):
    return list(OrderGroup.objects.filter(order_id__in=order_ids).select_related('order').prefetch_related(
        Prefetch('members', queryset=GroupMember.objects.order_by('pk'))))


def order_facts(wanted, *, viewer_id=None):
    from . import peer_chat
    from .order_groups import UPSTREAM, can_join_order_group, can_pin_order_message
    from .order_intents import core_requested

    orders = list(Order.all_objects.filter(order_no__in=wanted))
    links = peer_chat.links_for_orders(orders) if core_requested() else {}
    sync_accounts = set(PeerDownstreamBinding.objects.filter(
        state=PeerDownstreamBinding.State.ACTIVE,
    ).values_list("account_id", flat=True)) if links else set()
    # Unbinding leaves the order's worker performance in place.
    historical_accounts = peer_chat.binding_accounts_for_orders(links)
    blocked_accounts = {
        order_id: sync_accounts.union(historical_accounts.get(order_id, ()))
        for order_id in links
    }
    groups = {row.order_id:row for row in _groups([order.pk for order in orders])}
    intents = {row.order_no:row for row in OrderChatIntent.objects.filter(order_no__in=wanted)}
    workers = dict(Performance.objects.filter(order_id__in=[order.pk for order in orders], emp_type=EmpType.MAIN).values_list('order_id', 'user_id'))
    history = set(Message.objects.filter(conversation_id__in=[row.conversation_id for row in groups.values()]).values_list('conversation_id', flat=True).distinct())
    participants, default_ids, peer_facts = {}, {}, {}
    for order in orders:
        group = groups.get(order.pk)
        members = list(group.members.all()) if group else []
        publisher = order.hall_publisher_id or next((member.local_user_id for member in members if member.publisher), None)
        worker = workers.get(order.pk)
        link = links.get(order.pk)
        acceptor = None
        if link is not None:
            peer_facts[order.pk] = peer_chat.describe(link, worker)
            if peer_facts[order.pk]["active"]:
                acceptor = link.acceptor_id
        blocked = blocked_accounts.get(order.pk, set())
        default_ids[order.pk] = list(dict.fromkeys(
            uid for uid in (publisher, worker, acceptor) if uid is not None and uid not in blocked
        ))
        participants[order.pk] = {member.local_user_id: {'local_user_id':member.local_user_id, 'name':member.name,
            'avatar_url':None, 'studio_name':member.studio_name, 'order_role':member.order_role}
            for member in members if member.local_user_id is not None}
        wanted[order.order_no].update(participants[order.pk])
        wanted[order.order_no].update(default_ids[order.pk])
    user_ids = set().union(*wanted.values()) if wanted else set()
    if viewer_id is not None:
        user_ids.add(viewer_id)
    users = {user.pk:user for user in get_user_model().objects.filter(pk__in=user_ids)}
    codes = {uid:nodes_of(user) if user.is_active else frozenset() for uid,user in users.items()}
    operator_codes = set(OrderViewSet.ORDER_PERMISSIONS['retrieve'])
    worker_codes = set(MyOrdersViewSet.MY_ORDERS_PERMISSIONS['order_detail'])
    alive_ids = [order.pk for order in orders if order.deleted_at is None]
    readable, reopenable = {}, {}
    for uid,user in users.items():
        code = codes[uid]
        backend_ids = set(_operator_orders(user).filter(pk__in=alive_ids).values_list('pk',flat=True)) if code & operator_codes else set()
        own_ids = set(_worker_orders(user).filter(order_id__in=alive_ids).values_list('order_id',flat=True)) if code & worker_codes else set()
        readable[uid] = backend_ids | own_ids
        reopenable[uid] = backend_ids if 'order.add_edit' in code else set()
    result = []
    for order in orders:
        group = groups.get(order.pk)
        members = list(group.members.all()) if group else []
        publisher = order.hall_publisher_id or next((member.local_user_id for member in members if member.publisher), None)
        worker = workers.get(order.pk)
        peer_link = peer_facts.get(order.pk)
        for uid in default_ids[order.pk]:
            if uid not in participants[order.pk] and uid in users:
                role = 'worker' if uid == worker else 'publisher' if uid == publisher else 'acceptor'
                participants[order.pk][uid] = {'local_user_id':uid,'name':user_display_name(users[uid]),'avatar_url':None,
                    'studio_name':getattr(connection_tenant(), 'name', ''),'order_role':role}
        completed = order.status == OrderStatus.COMPLETED
        rule_group = group if group is not None else OrderGroup(order=order,order_no=order.order_no)
        pin_members = members if group is not None else ([GroupMember(local_user_id=publisher,publisher=True)] if publisher is not None else [])
        can_send = can_join_order_group(group) if group is not None else True
        can_pin_user_ids = [member.local_user_id for member in pin_members
            if member.local_user_id is not None and can_pin_order_message(rule_group,member)]
        if peer_link and peer_link['side'] == UPSTREAM:
            can_send = peer_link['active']
            if can_send and peer_link['acceptor_user_id'] is not None:
                can_pin_user_ids.append(peer_link['acceptor_user_id'])
        blocked = blocked_accounts.get(order.pk, set())
        intent = intents.get(order.order_no)
        fact = {'order_no':order.order_no, 'conversation_id':group.conversation_id if group else None,
            'read_user_ids':sorted(uid for uid in wanted[order.order_no] - blocked if order.pk in readable.get(uid,set())),
            'reopen_user_ids':sorted(uid for uid in wanted[order.order_no] - blocked if order.pk in reopenable.get(uid,set())),
            'completed':completed, 'completion_revision':order.completed_at.isoformat() if completed and order.completed_at is not None else None,
            'deleted':order.deleted_at is not None, 'deletion_revision':order.deleted_at.isoformat() if order.deleted_at is not None else None,
            'last_deletion_revision':intent.last_deleted_at.isoformat() if intent and intent.last_deleted_at is not None else None,
            'last_completion_revision':intent.last_completed_at.isoformat() if intent and intent.last_completed_at is not None else None,
            'publisher_id':publisher,'worker_id':worker, 'can_send':can_send,
            'can_pin_user_ids':[uid for uid in can_pin_user_ids if uid not in blocked],
            'default_join_user_ids':default_ids[order.pk], 'participants':list(participants[order.pk].values()),
            'has_legacy_messages':bool(group and group.conversation_id in history)}
        if peer_link:
            fact['peer_link'] = peer_link
        if group:
            fact['room'] = {'id':group.conversation_id,'kind':'order','order_no':group.order_no,'local_order_no':order.order_no,
                'title':f'订单-{group.order_no}','can_send':can_send,
                'can_pin_user_ids':fact['can_pin_user_ids'],
                'members':[{'id':m.pk,'local_user_id':m.local_user_id,'name':m.name,'studio_name':m.studio_name,
                            'order_role':m.order_role,'left_at':None} for m in members]}
        result.append(fact)
    return result


def connection_tenant():
    from django.db import connection
    return getattr(connection, 'tenant', None)


def authorize(params):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return {'groups': [], 'orders': [], 'has_order_access': False}
    viewer_id = params.get('viewer_id')
    checks = params['checks']
    cids = [item['conversation_id'] for item in checks if 'order_no' not in item]
    source_groups = {row.conversation_id:row for row in OrderGroup.objects.filter(conversation_id__in=cids).select_related('order') if row.order_id}
    wanted = {}
    for item in checks:
        number = item.get('order_no') or (source_groups[item['conversation_id']].order.order_no if item['conversation_id'] in source_groups else None)
        if number is not None:
            wanted.setdefault(number,set()).update(item['user_ids'])
    discovered = set()
    if params['discover'] and viewer_id is not None:
        old = OrderGroup.objects.filter(members__local_user_id=viewer_id).values_list('order_id',flat=True)
        new = OrderChatIntent.objects.filter(ensure_group=True).values('order_no')
        for number in Order.all_objects.filter(pk__in=old).values_list('order_no',flat=True):
            discovered.add(number)
        for number in Order.all_objects.filter(order_no__in=new).filter(Q(hall_publisher_id=viewer_id) | Q(pk__in=Performance.objects.filter(user_id=viewer_id,emp_type=EmpType.MAIN).values('order_id'))).values_list('order_no',flat=True):
            discovered.add(number)
        for number in discovered:
            wanted.setdefault(number,set()).add(viewer_id)
    facts = order_facts(wanted,viewer_id=viewer_id)
    by_no = {fact['order_no']:fact for fact in facts}
    by_cid = {fact['conversation_id']:fact for fact in facts if fact['conversation_id'] is not None}
    groups, orders, included = [], [], set()
    for item in checks:
        fact = by_no.get(item['order_no']) if 'order_no' in item else by_cid.get(item['conversation_id'])
        if fact is None:
            continue
        if 'order_no' in item:
            orders.append(fact)
        if 'conversation_id' in item:
            group = {**fact,'conversation_id':item['conversation_id']}
            room = fact.get('room') or {'kind':'order','order_no':fact['order_no'],'local_order_no':fact['order_no'],
                'title':f'订单-{fact["order_no"]}','can_send':fact['can_send'],
                'can_pin_user_ids':fact['can_pin_user_ids'],'members':[]}
            group['room'] = {**room,'id':item['conversation_id']}
            groups.append(group)
            included.add(fact['order_no'])
    for fact in facts:
        if fact['order_no'] in discovered and viewer_id in fact['default_join_user_ids']:
            if fact['conversation_id'] is not None and fact['order_no'] not in included:
                groups.append(fact)
            elif fact['conversation_id'] is None:
                orders.append(fact)
    user = get_user_model().objects.filter(pk=viewer_id,is_active=True).first() if viewer_id is not None else None
    codes = nodes_of(user) if user is not None else frozenset()
    return {'groups':groups,'orders':orders,'has_order_access':bool(codes & (set(OrderViewSet.ORDER_PERMISSIONS['retrieve']) | set(MyOrdersViewSet.MY_ORDERS_PERMISSIONS['order_detail'])))}


class SignedServiceView(APIView):
    authentication_classes = []
    permission_classes = []
    http_method_names = ['post', 'options']

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        config = matrix_bridge.tenant_config()
        if config is None:
            raise NotFound()
        from chat_service.protocol import ProtocolError, verify_request
        try:
            if request.headers.get('Authorization'):
                raise ProtocolError('User tokens are not accepted')
            verify_request({config['key_id']:config['secret']},request.headers,request.method,request.get_full_path(),request.body)
        except ProtocolError as exc:
            raise PermissionDenied('聊天服务签名无效') from exc
        if request.query_params:
            raise serializers.ValidationError('订单权限接口不接受查询参数')

    def handle_exception(self, exc):
        if isinstance(exc,(ImproperlyConfigured,DatabaseError)):
            exc = OrderAuthorityUnavailable()
        return super().handle_exception(exc)


class ServiceOrderAuthorityView(SignedServiceView):
    def post(self, request):
        schema = AuthorityInput(data=request.data)
        schema.is_valid(raise_exception=True)
        response = api_response(authorize(schema.validated_data))
        add_never_cache_headers(response)
        return response
