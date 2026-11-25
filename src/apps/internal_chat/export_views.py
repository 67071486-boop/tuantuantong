from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils.cache import add_never_cache_headers
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.views import APIView

from apps.core.envelope import api_response

from . import core_bridge, matrix_bridge, service
from .models import Change, Conversation, Message


class ExportQuery(serializers.Serializer):
    after = serializers.IntegerField(min_value=0, max_value=2**63 - 1, default=0)
    limit = serializers.IntegerField(min_value=1, max_value=200, default=100)


class SnapshotQuery(ExportQuery):
    kind = serializers.ChoiceField(choices=("conversations", "messages"))


class InvalidExportQuery(APIException):
    status_code = 400
    default_detail = "聊天导出参数有误"


def _source_write_fence():
    from apps.business.models import PeerDownstreamBinding, PeerSyncEvent, PeerUpstreamBinding
    from .order_groups import DOWNSTREAM, EVENT_KINDS, UPSTREAM
    from .peer_chat import legacy_bindings

    enabled = core_bridge.enabled()
    upstream = PeerUpstreamBinding.objects.filter(
        deleted_at__isnull=True, enabled=True, bound_by__is_active=True,
    )
    downstream = PeerDownstreamBinding.objects.filter(
        state=PeerDownstreamBinding.State.ACTIVE, account__is_active=True,
    )
    if enabled:
        upstream = legacy_bindings(upstream, UPSTREAM)
        downstream = legacy_bindings(downstream, DOWNSTREAM)
    writers = {
        "upstream_bindings": upstream.count(),
        "downstream_bindings": downstream.count(),
        # Future retries and leased deliveries still need draining; failed events may be requeued.
        "queued_events": PeerSyncEvent.objects.filter(
            direction="out", kind__in=EVENT_KINDS, target_type="chat",
            status__in=(PeerSyncEvent.Status.PENDING, PeerSyncEvent.Status.FAILED),
        ).count(),
    }
    active = any(writers.values())
    return {
        "core_write_fenced": enabled and not active,
        "legacy_chat_writers_active": active,
        "legacy_chat_writers": writers,
        "core_write_fence_blocked_reason": (
            "core_backend_legacy" if not enabled else "legacy_peer_chat_active" if active else ""
        ),
    }


class ServiceExportView(APIView):
    authentication_classes = []
    permission_classes = []
    query_serializer = ExportQuery

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        config = matrix_bridge.tenant_config()
        if config is None:
            raise NotFound()
        from chat_service.protocol import ProtocolError, verify_request

        try:
            verify_request({config["key_id"]: config["secret"]}, request.headers,
                           request.method, request.get_full_path(), request.body)
        except ProtocolError as exc:
            raise PermissionDenied("聊天服务签名无效") from exc
        schema = self.query_serializer(data=request.query_params)
        if (request.body or set(request.query_params) - set(schema.fields) or
                any(len(request.query_params.getlist(key)) != 1 for key in request.query_params) or
                not schema.is_valid()):
            raise InvalidExportQuery()
        self.params = schema.validated_data

    def handle_exception(self, exc):
        if isinstance(exc, ImproperlyConfigured):
            exc = matrix_bridge.ChatServiceUnavailable()
        return super().handle_exception(exc)

    @transaction.atomic
    def get(self, request):
        cursor = service._lock_cursor()
        fence = _source_write_fence()
        latest = cursor.latest
        params = self.params
        rows = list(Change.objects.filter(pk__gt=params["after"], pk__lte=latest).select_related(
            "conversation__first_user", "conversation__second_user", "conversation__order_group",
            "message__group_sender",
        ).order_by("pk")[:params["limit"] + 1])
        scanned = rows[:params["limit"]]
        # Legacy system notices stay in Django and must still advance the export cursor.
        page = [row for row in scanned if row.conversation.system_recipient_id is None]
        events = [{
            "id": row.pk, "kind": row.kind, "conversation": matrix_bridge.conversation_payload(row.conversation),
            "message": matrix_bridge.message_payload(row.message) if row.message_id else None,
            "viewer_id": row.viewer_id,
        } for row in page]
        response = api_response({"events": events, "cursor": scanned[-1].pk if scanned else params["after"],
                                 "has_more": len(rows) > params["limit"], "latest": latest,
                                 **fence, "chat_paused": settings.TT_CHAT_MAINTENANCE})
        add_never_cache_headers(response)
        return response


class ServiceSnapshotView(ServiceExportView):
    query_serializer = SnapshotQuery

    @transaction.atomic
    def get(self, request):
        cursor = service._lock_cursor()
        fence = _source_write_fence()
        latest = cursor.latest
        params = self.params
        conversations = params["kind"] == "conversations"
        queryset = Conversation.objects.filter(system_recipient__isnull=True).select_related(
            "first_user", "second_user", "order_group",
        ) if conversations else Message.objects.filter(conversation__system_recipient__isnull=True).select_related(
            "conversation__first_user", "conversation__second_user", "conversation__order_group", "group_sender",
        )
        rows = list(queryset.filter(pk__gt=params["after"]).order_by("pk")[:params["limit"] + 1])
        page = rows[:params["limit"]]
        payload = [matrix_bridge.conversation_payload(row) if conversations else {
            "conversation": matrix_bridge.conversation_payload(row.conversation),
            "message": matrix_bridge.message_payload(row),
        } for row in page]
        response = api_response({"rows": payload, "cursor": page[-1].pk if page else params["after"],
                                 "has_more": len(rows) > params["limit"], "latest": latest,
                                 **fence, "chat_paused": settings.TT_CHAT_MAINTENANCE})
        add_never_cache_headers(response)
        return response
