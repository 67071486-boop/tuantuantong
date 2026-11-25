import uuid
from collections import defaultdict
from datetime import timedelta, timezone as utc_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Min, Q
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError

from apps.accounts.display import user_display_name
from apps.business.models import (
    EmpType,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    Performance,
)
from apps.business.peer_sync import CORE_CHAT_TARGET
from apps.business.peer_wakeup import notify_hall_take
from apps.core.envelope import api_response

from . import core_bridge, matrix_bridge, order_groups
from .images import encode_image
from .models import OrderChatPeerBinding, OrderChatPeerLink, OrderGroup
from .order_authority import SignedServiceView, StrictInput
from .order_intents import core_requested, enqueue_order


GROUP_MISSING = "这条绑定没有该订单群"
KEY_FIELD = r"\A[A-Za-z0-9_-]{1,64}\Z"


def _binding_field(role):
    return f"{role}_binding"


def _role(link):
    return order_groups.UPSTREAM if link.upstream_binding_id else order_groups.DOWNSTREAM


def _links():
    return OrderChatPeerLink.objects.select_related(
        "order", "upstream_binding__bound_by", "downstream_binding__account",
    )


def _active(link, worker_id):
    if link.detached_at or link.order.deleted_at or link.order.status == OrderStatus.REFUNDED:
        return False
    if not _same_credentials(link):
        return False
    if link.upstream_binding_id is not None:
        binding = link.upstream_binding
        return binding.enabled and binding.deleted_at is None and binding.bound_by.is_active
    binding = link.downstream_binding
    return (
        binding.state == PeerDownstreamBinding.State.ACTIVE
        and binding.account.is_active and binding.account_id == worker_id
    )


def _same_credentials(link):
    binding = link.upstream_binding if link.upstream_binding_id else link.downstream_binding
    return link.binding_token_hash == binding.token_in_hash


def _workers(links):
    return dict(Performance.objects.filter(
        order_id__in=[link.order_id for link in links], emp_type=EmpType.MAIN,
    ).values_list("order_id", "user_id"))


def _current(link):
    if link.upstream_binding_id is not None:
        return _active(link, None)
    return _active(link, _workers([link]).get(link.order_id))


def links_for_orders(orders):
    links = _links().filter(order_id__in=[order.pk for order in orders]).order_by("order_id", "-generation")
    return {link.order_id: link for link in links.distinct("order_id")}


def binding_accounts_for_orders(order_ids):
    accounts = defaultdict(set)
    rows = OrderChatPeerLink.objects.filter(
        order_id__in=order_ids, downstream_binding__isnull=False,
    ).values_list("order_id", "downstream_binding__account_id")
    for order_id, account_id in rows:
        accounts[order_id].add(account_id)
    return accounts


def describe(link, worker_id):
    return {
        **_invitation(link),
        "side": _role(link),
        "active": _active(link, worker_id),
        "acceptor_user_id": link.acceptor_id,
        "publisher_user_id": link.publisher_id,
        "generation": link.generation,
    }


def _invitation(link):
    return {
        "group_id": str(link.group_id),
        "order_no": link.order_no,
        "publisher_name": link.publisher_name,
        "publisher_studio_name": link.publisher_studio_name,
    }


def register_binding(role, binding):
    try:
        enabled = core_bridge.enabled()
    except matrix_bridge.ChatServiceUnavailable:
        return
    if enabled:
        OrderChatPeerBinding.objects.get_or_create(role=role, binding_id=binding.pk)


def legacy_bindings(bindings, role):
    groups = OrderGroup.objects.filter(binding_role=role, binding_id__isnull=False)
    registered = OrderChatPeerBinding.objects.filter(role=role).exclude(binding_id__in=groups.values("binding_id"))
    return bindings.exclude(pk__in=registered.values("binding_id"))


def _latest_link(order):
    return _links().select_for_update(of=("self",)).filter(order=order).order_by("-generation").first()


@transaction.atomic
def authorize_claim(order, binding):
    from apps.business.order_writes import lock_order_row

    order = lock_order_row(order)
    if order is None:
        return None
    publisher = order.hall_publisher
    if publisher is None or not order_groups._can_join_group(publisher.pk):
        return None
    if OrderGroup.objects.filter(order=order).exclude(binding_role="").exists():
        return None
    latest = _latest_link(order)
    if latest and latest.upstream_binding_id is not None:
        return None
    if latest and latest.detached_at is None:
        if latest.downstream_binding_id == binding.pk and latest.binding_token_hash == binding.token_in_hash:
            return _invitation(latest)
        _detach_downstream(latest)
    link = OrderChatPeerLink.objects.create(
        order=order, generation=latest.generation + 1 if latest else 1, group_id=uuid.uuid4(),
        downstream_binding=binding, binding_token_hash=binding.token_in_hash, order_no=order.order_no,
        publisher=publisher, publisher_name=user_display_name(publisher),
        publisher_studio_name=order_groups._tenant_studio_name(),
    )
    enqueue_order(order.order_no, ensure_group=True)
    return _invitation(link)


@transaction.atomic
def confirm_claim(order, binding, acceptor, invitation):
    from apps.business.order_writes import lock_order_row

    order = lock_order_row(order)
    if order is None:
        return
    if OrderGroup.objects.filter(order=order).exists():
        return
    if not PeerHallMirror.objects.filter(
        promoted_order=order, peer_upstream_binding=binding, claiming_by=acceptor,
        status=PeerHallMirror.Status.PROMOTED,
    ).exists():
        return
    source_number = PeerOrderLink.objects.filter(
        order=order, upstream_binding=binding,
    ).values_list("upstream_internal_order_no", flat=True).first()
    if source_number and source_number != invitation["order_no"]:
        return
    latest = _latest_link(order)
    if latest and (latest.group_id == invitation["group_id"] or latest.detached_at is None):
        return
    try:
        _, created = OrderChatPeerLink.objects.get_or_create(
            group_id=invitation["group_id"],
            defaults={
                "order": order,
                "generation": latest.generation + 1 if latest else 1,
                "upstream_binding": binding,
                "binding_token_hash": binding.token_in_hash,
                "order_no": invitation["order_no"],
                "publisher_name": invitation["publisher_name"],
                "publisher_studio_name": invitation.get("publisher_studio_name", ""),
                "acceptor": acceptor,
            },
        )
    except IntegrityError:
        if OrderChatPeerLink.objects.filter(group_id=invitation["group_id"]).exists():
            return
        raise
    if created:
        enqueue_order(order.order_no, ensure_group=True)


def _event_scope(link, direction):
    role = _role(link)
    return dict(
        binding_role=role, binding_id=str(getattr(link, f"{_binding_field(role)}_id")),
        direction=direction, target_type=CORE_CHAT_TARGET, target_ref=str(link.group_id),
    )


def _queue(link, direction, kind, payload, key):
    event, created = PeerSyncEvent.objects.get_or_create(
        **_event_scope(link, direction), idempotency_key=key,
        defaults={"kind": kind, "payload": payload},
    )
    if event.kind != kind or event.payload != payload:
        return False
    if direction == "out" and created:
        notify_hall_take()
    return True


def _time(value):
    return value.astimezone(utc_timezone.utc).isoformat() if value is not None else None


def _wire_roster(roster):
    return {**roster, "members": [
        {**member, "left_at": _time(member["left_at"])} for member in roster["members"]
    ]}


def _wire_message(data):
    event = {
        **data, "message_id": str(data["message_id"]), "sender_left_at": _time(data["sender_left_at"]),
        "created_at": _time(data["created_at"]), "reply_to": str(data["reply_to"]) if data["reply_to"] else None,
    }
    if "image" in data:
        event["image"] = encode_image(data["image"], data["image"]["data"])
    if "roster" in data:
        event["roster"] = _wire_roster(data["roster"])
    return event


def _wire_payload(kind, data):
    if kind == order_groups.ROSTER_EVENT:
        return _wire_roster(data)
    if kind == order_groups.MESSAGE_EVENT:
        return _wire_message(data)
    if kind == order_groups.ALBUM_EVENT:
        alb = {**data, "album_id": str(data["album_id"]), "messages": [
            _wire_message(message) for message in data["messages"]
        ]}
        if "roster" in data:
            alb["roster"] = _wire_roster(data["roster"])
        return alb
    if kind == order_groups.PIN_EVENT:
        return {**data, "operation_id": str(data["operation_id"]),
                "message_id": str(data["message_id"]) if data["message_id"] is not None else None}
    return {}


def _detach(link):
    if link.detached_at is None:
        link.detached_at = timezone.now()
        link.save(update_fields=["detached_at"])
        PeerSyncEvent.objects.filter(
            **_event_scope(link, "out"), status=PeerSyncEvent.Status.PENDING,
        ).update(
            status=PeerSyncEvent.Status.DONE, next_attempt_at=None, processed_at=link.detached_at,
            reason="该绑定已退出订单群",
        )


def _detach_downstream(link):
    _detach(link)
    if _same_credentials(link):
        _queue(link, "out", order_groups.DETACHED_EVENT, {}, uuid.uuid4().hex)


@transaction.atomic
def disconnect_from_upstream(order, binding):
    link = _links().select_for_update(of=("self",)).filter(
        order=order, upstream_binding=binding, binding_token_hash=binding.token_in_hash,
        detached_at__isnull=True,
    ).first()
    if link is not None:
        _detach(link)


@transaction.atomic
def disconnect_from_downstream(order, former_worker):
    if former_worker is None:
        return
    link = _links().select_for_update(of=("self",)).filter(
        order=order, downstream_binding__account=former_worker, detached_at__isnull=True,
    ).first()
    if link is not None:
        _detach_downstream(link)


@transaction.atomic
def receive(kind, payload, *, binding, binding_role, target_ref, event_key):
    identity = serializers.UUIDField()
    key_field = serializers.RegexField(KEY_FIELD)
    try:
        group_id = identity.run_validation(target_ref)
        event_key = key_field.run_validation(event_key)
    except ValidationError:
        return "订单群同步标识无效"
    link = _links().select_for_update(of=("self",)).filter(
        group_id=group_id, **{_binding_field(binding_role): binding},
    ).first()
    if link is None or not _same_credentials(link):
        return GROUP_MISSING
    data, error = order_groups.validate_peer_event(kind, payload, binding_role)
    if error is not None:
        return error
    payload = _wire_payload(kind, data)
    previous = PeerSyncEvent.objects.filter(**_event_scope(link, "in"), idempotency_key=event_key).first()
    if previous is not None:
        return None if previous.kind == kind and previous.payload == payload else "订单群同步编号重复"
    if kind == order_groups.DETACHED_EVENT:
        if binding_role != order_groups.UPSTREAM:
            return GROUP_MISSING
        _detach(link)
    elif not _current(link):
        return GROUP_MISSING
    if not _queue(link, "in", kind, payload, event_key):
        return "订单群同步编号重复"
    return None


def can_deliver(event, binding):
    if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not core_requested():
        return False
    link = _links().filter(group_id=event.target_ref, **{_binding_field(event.binding_role): binding}).first()
    if link is None or not _same_credentials(link):
        return False
    if event.kind == order_groups.DETACHED_EVENT:
        return event.binding_role == order_groups.DOWNSTREAM and link.detached_at is not None
    return _current(link)


class PeerChatInput(StrictInput):
    operation = serializers.ChoiceField(choices=("pull", "ack", "defer", "push"))
    limit = serializers.IntegerField(min_value=1, max_value=20, required=False)
    events = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1), max_length=20, required=False,
    )
    group_id = serializers.UUIDField(required=False)
    kind = serializers.ChoiceField(choices=sorted(order_groups.EVENT_KINDS), required=False)
    payload = serializers.DictField(required=False)
    event_key = serializers.RegexField(KEY_FIELD, required=False)

    def validate(self, data):
        needed = {
            "pull": {"limit"}, "ack": {"events"}, "defer": {"events"},
            "push": {"group_id", "kind", "payload", "event_key"},
        }
        if data.keys() - {"operation"} != needed[data["operation"]]:
            raise ValidationError("订单群同步参数有误")
        return data


def _pending_inbox():
    return PeerSyncEvent.objects.filter(
        direction="in", target_type=CORE_CHAT_TARGET, status=PeerSyncEvent.Status.PENDING,
        kind__in=order_groups.EVENT_KINDS,
    )


def _pull(limit):
    heads = _pending_inbox().order_by().values("target_ref").annotate(head=Min("pk")).values("head")
    events = list(_pending_inbox().filter(pk__in=heads).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()),
    ).order_by("pk")[:limit])
    numbers = {str(group_id): number for group_id, number in OrderChatPeerLink.objects.filter(
        group_id__in=[row.target_ref for row in events],
    ).values_list("group_id", "order__order_no")}
    retired = [row.pk for row in events if row.target_ref not in numbers]
    if retired:
        _pending_inbox().filter(pk__in=retired).update(
            status=PeerSyncEvent.Status.DONE, processed_at=timezone.now(), reason="订单已不存在",
        )
    return {"events": [
        {"id": row.pk, "order_no": numbers[row.target_ref], "group_id": row.target_ref,
         "kind": row.kind, "payload": row.payload}
        for row in events
        if row.target_ref in numbers
    ]}


@transaction.atomic
def _push(data):
    link = _links().select_for_update(of=("self",)).filter(group_id=data["group_id"]).first()
    if link is None or not _current(link):
        return {"queued": False}
    receiver = order_groups.DOWNSTREAM if _role(link) == order_groups.UPSTREAM else order_groups.UPSTREAM
    values, error = order_groups.validate_peer_event(data["kind"], data["payload"], receiver)
    if error is not None or data["kind"] == order_groups.DETACHED_EVENT:
        raise ValidationError(error or "订单群退出由业务端确认")
    if not _queue(link, "out", data["kind"], _wire_payload(data["kind"], values), data["event_key"]):
        raise ValidationError("订单群同步编号重复")
    return {"queued": True}


class ServicePeerChatView(SignedServiceView):
    def post(self, request):
        if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not core_requested():
            raise NotFound()
        schema = PeerChatInput(data=request.data)
        schema.is_valid(raise_exception=True)
        data = schema.validated_data
        operation = data["operation"]
        if operation == "pull":
            result = _pull(data["limit"])
        elif operation == "ack":
            acked = _pending_inbox().filter(pk__in=data["events"]).update(
                status=PeerSyncEvent.Status.DONE, processed_at=timezone.now(),
            )
            result = {"acked": acked}
        elif operation == "defer":
            deferred = _pending_inbox().filter(pk__in=data["events"]).update(
                next_attempt_at=timezone.now() + timedelta(seconds=5),
            )
            result = {"deferred": deferred}
        else:
            result = _push(data)
        response = api_response(result)
        add_never_cache_headers(response)
        return response
