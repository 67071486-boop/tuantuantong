from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.display import user_display_name

from .models import VisitorChange, VisitorConversation, VisitorDeskPresence
from .service import _eligible_directory_staff

Seat = VisitorConversation.Seat
Status = VisitorConversation.ServiceStatus
SEAT_IDS = {Seat.HR: 2**41 + 1, Seat.ORDERS: 2**41 + 2}
SEATS_BY_ID = {peer_id: kind for kind, peer_id in SEAT_IDS.items()}
DESK_PEER_OFFSET = 2**42
HEARTBEAT_INTERVAL = timedelta(seconds=10)
ONLINE_WINDOW = timedelta(seconds=35)
DRAIN_BATCH_SIZE = 100


def configured_seats(config):
    return {seat["kind"]: seat for seat in config.seats or []}


def reception_member(config, staff_id):
    return any(staff_id in seat["contact_ids"] for seat in config.seats or [])


def public_peer(kind, presence):
    return {"id": SEAT_IDS[kind], "name": Seat(kind).label, "avatar_url": None, "seat_kind": kind, "presence": presence}


def _on_duty(seat, staff_id, local_now):
    if staff_id not in seat["contact_ids"]:
        return False
    if not seat["shifts"]:
        return True
    today = local_now.weekday()
    yesterday = (today - 1) % 7
    current = local_now.strftime("%H:%M")
    for shift in seat["shifts"]:
        if shift["contact_id"] != staff_id:
            continue
        day, start, end = shift["weekday"], shift["start"], shift["end"]
        if start < end and day == today and start <= current < end:
            return True
        if start > end and (day == today and start <= current or day == yesterday and current < end):
            return True
    return False


def _receiver_load(seat, now):
    local_now = timezone.localtime(now)
    scheduled = [staff_id for staff_id in seat["contact_ids"] if _on_duty(seat, staff_id, local_now)]
    if not scheduled:
        return {}
    eligible = _eligible_directory_staff().filter(pk__in=scheduled).values("pk")
    staff_ids = set(VisitorDeskPresence.objects.filter(
        staff_id__in=eligible, enabled=True, last_seen_at__gte=now - ONLINE_WINDOW,
    ).values_list("staff_id", flat=True))
    if not staff_ids:
        return {}
    active = VisitorConversation.objects.filter(
        seat_kind=seat["kind"], service_status=Status.ASSIGNED, assigned_staff_id__in=staff_ids,
    ).values("assigned_staff_id").annotate(load=Count("pk"))
    loads = dict.fromkeys(staff_ids, 0)
    loads.update((row["assigned_staff_id"], row["load"]) for row in active)
    return loads


def seat_presence(config):
    now = timezone.now()
    statuses = {}
    for seat in config.seats or []:
        loads = _receiver_load(seat, now)
        if not loads:
            statuses[seat["kind"]] = "offline"
        elif min(loads.values()) >= seat["capacity"]:
            statuses[seat["kind"]] = "busy"
        else:
            statuses[seat["kind"]] = "online"
    return statuses


def queue(conversation, now):
    if conversation.service_status == Status.WAITING:
        return
    conversation.service_status = Status.WAITING
    conversation.queued_at = now
    conversation.save(update_fields=["service_status", "queued_at"])
    # The former desk may still be showing the conversation in its list.
    VisitorChange.objects.create(conversation=conversation, former_staff_id=conversation.assigned_staff_id)


def queue_if_needed(conversation, seat, now):
    staff_id = conversation.assigned_staff_id
    if (
        conversation.service_status == Status.ASSIGNED and _on_duty(seat, staff_id, timezone.localtime(now))
        and VisitorDeskPresence.objects.filter(
            staff_id=staff_id, enabled=True, staff_id__in=_eligible_directory_staff().values("pk"),
        ).exists()
    ):
        return
    queue(conversation, now)


def _assign(conversation, target_id, now):
    previous_id = conversation.assigned_staff_id
    conversation.service_status = Status.ASSIGNED
    conversation.assigned_staff_id = target_id
    conversation.queued_at = None
    conversation.save(update_fields=["service_status", "assigned_staff", "queued_at"])
    change = {"former_staff_id": previous_id, "transferred_staff_id": target_id, "transferred_at": now}
    VisitorChange.objects.create(conversation=conversation, **(change if previous_id and previous_id != target_id else {}))


def drain(config, seat, now, *, limit=DRAIN_BATCH_SIZE, current_conversation=None):
    waiting = VisitorConversation.objects.filter(seat_kind=seat["kind"], service_status=Status.WAITING)
    if not waiting.exists():
        return set()
    loads = _receiver_load(seat, now)
    if not loads or min(loads.values()) >= seat["capacity"]:
        return set()
    kind = seat["kind"]
    rotation = seat["contact_ids"]
    last = config.seat_rotation.get(kind)
    start = rotation.index(last) + 1 if last in rotation else 0
    rows = waiting.order_by("queued_at", "pk")[:limit].iterator(chunk_size=DRAIN_BATCH_SIZE)
    affected = set()
    for conversation in rows:
        ranked = [pk for pk in rotation[start:] + rotation[:start] if pk in loads]
        target_id = min(ranked, key=loads.__getitem__)
        if loads[target_id] >= seat["capacity"]:
            break
        affected.add(target_id)
        # Keep the caller's already-locked room in sync when this drain assigns it.
        if current_conversation is not None and conversation.pk == current_conversation.pk:
            conversation = current_conversation
        _assign(conversation, target_id, now)
        loads[target_id] += 1
        start = rotation.index(target_id) + 1
        config.seat_rotation[kind] = target_id
    config.save(update_fields=["seat_rotation"])
    return affected


def drain_for_staff(config, staff_id, now):
    affected = set()
    for seat in config.seats or []:
        if staff_id in seat["contact_ids"]:
            affected.update(drain(config, seat, now))
    return affected


def release_for_staff(config, staff_id, now):
    affected = set()
    for seat in config.seats or []:
        if staff_id not in seat["contact_ids"]:
            continue
        assigned = VisitorConversation.objects.filter(
            seat_kind=seat["kind"], service_status=Status.ASSIGNED, assigned_staff_id=staff_id,
        )
        released = False
        for conversation in assigned.iterator(chunk_size=DRAIN_BATCH_SIZE):
            queue(conversation, now)
            released = True
        if released:
            affected.add(staff_id)
            affected.update(drain(config, seat, now, limit=None))
    return affected


def transfer_options(conversation, seat, now):
    if conversation.service_status != Status.ASSIGNED:
        return []
    loads = _receiver_load(seat, now)
    return [
        pk for pk in seat["contact_ids"]
        if pk != conversation.assigned_staff_id and pk in loads and loads[pk] < seat["capacity"]
    ]


def transfer(conversation, config, seat, target_id, now):
    if target_id not in transfer_options(conversation, seat, now):
        raise ValidationError({"target_id": "只能转接给同席位当前可接待的人员"})
    former_staff_id = conversation.assigned_staff_id
    _assign(conversation, target_id, now)
    return drain(config, seat, now) | {former_staff_id, target_id}


def transfer_history(conversation):
    rows = list(VisitorChange.objects.filter(
        conversation=conversation, transferred_at__isnull=False,
    ).order_by("pk").values_list("former_staff_id", "transferred_staff_id", "transferred_at"))
    staff_ids = {staff_id for former, target, _ in rows for staff_id in (former, target)}
    staff = get_user_model().objects.filter(pk__in=staff_ids).only("pk", "first_name", "username")
    names = {user.pk: user_display_name(user) for user in staff}
    return [
        {"from_id": former, "from_name": names.get(former), "to_id": target, "to_name": names.get(target),
         "transferred_at": when.isoformat()}
        for former, target, when in rows
    ]


def close(conversation, config, seat, now):
    if conversation.service_status == Status.CLOSED:
        return set()
    if conversation.service_status != Status.ASSIGNED:
        raise PermissionDenied("当前会话未由你接待")
    conversation.service_status = Status.CLOSED
    conversation.save(update_fields=["service_status"])
    VisitorChange.objects.create(conversation=conversation)
    return drain(config, seat, now) | {conversation.assigned_staff_id}
