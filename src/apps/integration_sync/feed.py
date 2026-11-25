from datetime import timedelta

from django.core import signing
from django.db import connection, transaction
from django.utils import timezone

from apps.integration_sync.models import BusinessChange, ChangeCheckpoint

CHECKPOINT_ID = 1
RETENTION = timedelta(days=30)
PUBLISH_BATCH_SIZE = 200
PRUNE_BATCH_SIZE = 1000
CURSOR_SALT = "integration-sync-cursor-v1"


class InvalidCursor(ValueError):
    pass


class ExpiredCursor(ValueError):
    pass


def _encode_cursor(app_key, position):
    return signing.dumps(
        [connection.schema_name, app_key, position], salt=CURSOR_SALT,
    )


def _decode_cursor(app_key, token):
    try:
        schema, owner, position = signing.loads(token, salt=CURSOR_SALT)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise InvalidCursor from exc
    if schema != connection.schema_name or owner != app_key or type(position) is not int:
        raise InvalidCursor
    return position


def event_payload(change):
    return {
        "event_id": str(change.published_seq),
        "resource": change.resource,
        "resource_id": change.resource_id,
        "action": change.action,
        "occurred_at": change.occurred_at.isoformat(),
    }


def _page_bounds():
    # A shared row lock keeps retention from moving until this page has been read.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT published_through, expired_through "
            "FROM integration_sync_changecheckpoint WHERE id = %s FOR SHARE",
            [CHECKPOINT_ID],
        )
        return cursor.fetchone() or (0, 0)


@transaction.atomic
def read_page(*, app_key, cursor=None, page_size=50, start_now=False):
    latest, expired_through = _page_bounds()
    if start_now:
        position = latest
    elif cursor is not None:
        position = _decode_cursor(app_key, cursor)
        if position > latest or position < 0:
            raise InvalidCursor
        if position < expired_through:
            raise ExpiredCursor
    else:
        position = expired_through

    changes = []
    if position < latest:
        changes = list(
            BusinessChange.objects.filter(
                published_seq__gt=position, published_seq__lte=latest,
            ).order_by("published_seq")[:page_size + 1]
        )
    page = changes[:page_size]
    if page:
        position = page[-1].published_seq
    return {
        "results": [event_payload(change) for change in page],
        "next_cursor": _encode_cursor(app_key, position),
        "has_more": len(changes) > page_size,
    }


def _prune_changes(checkpoint, *, now):
    cutoff = now - RETENTION
    candidates = list(
        BusinessChange.objects.filter(published_seq__isnull=False)
        .order_by("published_seq")
        .values_list("pk", "published_seq", "published_at")[:PRUNE_BATCH_SIZE]
    )
    expired_ids = []
    for change_id, sequence, published_at in candidates:
        if published_at >= cutoff:
            break
        expired_ids.append(change_id)
        checkpoint.expired_through = sequence
    if expired_ids:
        BusinessChange.objects.filter(pk__in=expired_ids).delete()
    return bool(expired_ids)


@transaction.atomic
def publish_changes(enqueue, *, now=None):
    # Concurrent workers assign sequence numbers only to already committed changes.
    checkpoint, _ = ChangeCheckpoint.objects.select_for_update().get_or_create(
        pk=CHECKPOINT_ID,
    )
    now = now if now is not None else timezone.now()
    changes = list(
        BusinessChange.objects.filter(published_seq__isnull=True)
        .order_by("pk")[:PUBLISH_BATCH_SIZE]
    )
    for change in changes:
        checkpoint.published_through += 1
        change.published_seq = checkpoint.published_through
        change.published_at = now
    if changes:
        BusinessChange.objects.bulk_update(changes, ["published_seq", "published_at"])
        enqueue(changes)
    pruned = _prune_changes(checkpoint, now=now)
    if changes or pruned:
        checkpoint.save(update_fields=["published_through", "expired_through"])
    return len(changes)
