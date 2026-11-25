from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from rest_framework import serializers

from apps.business.models import EmpType, Order, OrderStatus, Performance
from apps.core.envelope import api_response
from apps.core.plane import on_public_plane

from . import matrix_bridge
from .models import OrderChatIntent, OrderChatPeerLink
from .order_authority import SignedServiceView, StrictInput, order_facts


def core_requested():
    backend = getattr(settings,'TT_CHAT_CORE_BACKEND','legacy')
    if on_public_plane() or backend == 'legacy':
        return False
    if backend != 'service':
        return True
    try:
        config = matrix_bridge.tenant_config()
    except ImproperlyConfigured:
        return True
    return config is None or config.get('core_backend') != 'legacy'


def enqueue_order(order_no, *, ensure_group=False, completed_at=None, deleted_at=None):
    if not core_requested():
        return
    row, created = OrderChatIntent.objects.get_or_create(order_no=order_no, defaults={'ensure_group':ensure_group,'last_completed_at':completed_at,'last_deleted_at':deleted_at})
    if not created:
        values = {'revision': F('revision') + 1, 'updated_at': timezone.now()}
        if ensure_group:
            values['ensure_group'] = True
        if completed_at is not None:
            values['last_completed_at'] = Greatest('last_completed_at',Value(completed_at))
        if deleted_at is not None:
            values['last_deleted_at'] = Greatest('last_deleted_at',Value(deleted_at))
        OrderChatIntent.objects.filter(pk=row.pk).update(**values)


ORDER_FIELDS = frozenset({'status','deleted_at','hall_publisher_id','hall_card_id','upstream_hall_card_id'})
MAIN_FIELDS = frozenset({'user_id','emp_type','deleted_at','order_id'})


def _tracked_names(fields):
    return {field if field.endswith('_id') else field + '_id' if field in {'user','order','hall_publisher','hall_card','upstream_hall_card'} else field for field in fields}


@receiver(pre_save, sender=Order, dispatch_uid='chat_order_intent_order_before')
@receiver(pre_save, sender=Performance, dispatch_uid='chat_order_intent_main_before')
def remember_chat_facts(sender, instance, update_fields=None, raw=False, **kwargs):
    if raw:
        return
    tracked = ORDER_FIELDS if sender is Order else MAIN_FIELDS
    fields = _tracked_names(update_fields) if update_fields is not None else None
    if fields is not None and not fields & tracked:
        return
    if not core_requested():
        return
    previous = None
    if instance.pk:
        manager = Order.all_objects if sender is Order else Performance.objects
        previous = manager.filter(pk=instance.pk).values(*tracked).first()
    instance._chat_intent_before = previous
    instance._chat_intent_relevant = True


@receiver(post_save, sender=Order, dispatch_uid='chat_order_intent_order_saved')
def order_saved(sender, instance, created, raw=False, **kwargs):
    relevant = instance.__dict__.pop('_chat_intent_relevant',False)
    previous = instance.__dict__.pop('_chat_intent_before',None)
    if raw or not relevant:
        return
    if created or previous is None or any(previous[field] != getattr(instance,field) for field in ORDER_FIELDS):
        enqueue_order(instance.order_no,ensure_group=created,deleted_at=instance.deleted_at,
                      completed_at=instance.completed_at if instance.status == OrderStatus.COMPLETED else None)


@receiver(post_save, sender=Performance, dispatch_uid='chat_order_intent_main_saved')
def main_saved(sender, instance, raw=False, **kwargs):
    relevant = instance.__dict__.pop('_chat_intent_relevant',False)
    previous = instance.__dict__.pop('_chat_intent_before',None)
    if raw or not relevant:
        return
    changed = previous is None or any(previous[field] != getattr(instance,field) for field in MAIN_FIELDS)
    if changed and (instance.emp_type == EmpType.MAIN or previous and previous['emp_type'] == EmpType.MAIN):
        enqueue_order(instance.order.order_no)


@receiver(post_delete, sender=Performance, dispatch_uid='chat_order_intent_main_deleted')
def main_deleted(sender, instance, **kwargs):
    if instance.emp_type == EmpType.MAIN and core_requested():
        enqueue_order(instance.order.order_no)


class IntentAck(StrictInput):
    id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    revision = serializers.IntegerField(min_value=1, max_value=2**63 - 1)


class IntentInput(StrictInput):
    operation = serializers.ChoiceField(choices=('pull', 'ack'))
    limit = serializers.IntegerField(min_value=1, max_value=100, default=100)
    intents = IntentAck(many=True, max_length=100, required=False)

    def validate(self, data):
        if data['operation'] == 'ack' and 'intents' not in data:
            raise serializers.ValidationError('确认需要待办编号和版本')
        if data['operation'] == 'pull' and 'intents' in data:
            raise serializers.ValidationError('拉取不能包含确认列表')
        return data


class ServiceOrderIntentsView(SignedServiceView):
    def post(self, request):
        schema = IntentInput(data=request.data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        if data['operation'] == 'ack':
            acknowledged = 0
            for item in data['intents']:
                acknowledged += OrderChatIntent.objects.filter(pk=item['id'], revision=item['revision']).update(
                    acked_revision=F('revision'))
            result = {'acked': acknowledged}
        else:
            rows = list(OrderChatIntent.objects.filter(revision__gt=F('acked_revision')).order_by('id')[:data['limit']])
            facts = {item['order_no']:item for item in order_facts({row.order_no:set() for row in rows})}
            result = {'intents': [{'id':row.pk, 'revision':row.revision,'ensure_group':row.ensure_group,
                                  'order':facts.get(row.order_no)} for row in rows]}
            result['peer_chat'] = core_requested() and OrderChatPeerLink.objects.exists()
        response = api_response(result)
        add_never_cache_headers(response)
        return response
