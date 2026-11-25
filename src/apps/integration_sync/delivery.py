from uuid import uuid4

from django.utils import timezone

from apps.integration_sync.feed import PUBLISH_BATCH_SIZE, RETENTION, event_payload, publish_changes
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import BUSINESS_INTEGRATION_ENABLED, ApiApp, CallbackState, OpenApiDelivery

INTEGRATION_APP_KIND = "business_integration"
CHANGE_EVENT = "integration.change"


def enqueue_deliveries(changes):
    app_ids = list(
        ApiApp.objects.filter(
            kind=INTEGRATION_APP_KIND, state=BUSINESS_INTEGRATION_ENABLED,
            callback_state=CallbackState.ENABLED,
        ).exclude(callback_url="").values_list("id", flat=True)
    )
    if not app_ids:
        return
    deliveries = []
    for change in changes:
        payload = event_payload(change)
        for app_id in app_ids:
            deliveries.append(OpenApiDelivery(
                app_id=app_id,
                event=CHANGE_EVENT,
                # The existing worker orders pending rows by this key.
                order_no=f"integration:{app_id}:{change.resource}:{change.resource_id}",
                payload={"event": CHANGE_EVENT, "delivery_id": uuid4().hex, **payload},
            ))
    OpenApiDelivery.objects.bulk_create(deliveries, batch_size=200)


def recover_interrupted_deliveries():
    # An older worker sees the SDK state as disabled and stops these deliveries.
    stopped = OpenApiDelivery.objects.filter(
        event=CHANGE_EVENT, status=OpenApiDelivery.Status.FAILED,
        reason=OPENAPI_SPEC.callback_disabled_reasons[0],
        app__kind=INTEGRATION_APP_KIND, app__state=BUSINESS_INTEGRATION_ENABLED,
        app__callback_state=CallbackState.ENABLED,
        created_at__gte=timezone.now() - RETENTION,
    ).exclude(app__callback_url="")
    batch = stopped.order_by("pk").values("pk")[:PUBLISH_BATCH_SIZE]
    stopped.filter(pk__in=batch).update(
        status=OpenApiDelivery.Status.PENDING, processed_at=None, next_attempt_at=None, reason="",
    )


def publish_and_enqueue():
    if not ApiApp.objects.filter(kind=INTEGRATION_APP_KIND).exists():
        return 0
    recover_interrupted_deliveries()
    return publish_changes(enqueue_deliveries)
