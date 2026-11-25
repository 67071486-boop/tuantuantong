"""Personal business notices that can be read from the worker H5."""

from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound

from apps.accounts.models import WxPusherNotice
from apps.accounts.wxpusher_notice_templates import (
    CHAT_KINDS,
    HALL_NEW_ORDER,
    NOTICE_TEMPLATES,
    SETTLEMENT_KINDS,
    STAFF_KINDS,
    notice_display_order_no,
    notice_text_lines,
)
from apps.accounts.wxpusher_notifications import visible_notice_details, worker_notice_targets
from apps.core.envelope import api_response
from apps.core.wxpusher_views import WxPusherView

INBOX_PAGE_SIZE = 25
_WORKER_KINDS = set(NOTICE_TEMPLATES) - {*STAFF_KINDS, HALL_NEW_ORDER, *CHAT_KINDS}


class WxPusherInboxQuery(serializers.Serializer):
    category = serializers.ChoiceField(choices=["order", "settlement"], required=False)
    page = serializers.IntegerField(min_value=1, max_value=2**31 - 1, default=1)


def _worker_inbox(request):
    return WxPusherNotice.objects.filter(user_id=request.user.pk, kind__in=_WORKER_KINDS)


class WxPusherInboxView(WxPusherView):
    def get(self, request):
        query = WxPusherInboxQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = query.validated_data["page"]
        category = query.validated_data.get("category")
        inbox = _worker_inbox(request)
        unread = inbox.filter(read_at__isnull=True).count()
        notices = inbox
        if category == "order":
            notices = notices.exclude(kind__in=SETTLEMENT_KINDS)
        elif category == "settlement":
            notices = notices.filter(kind__in=SETTLEMENT_KINDS)
        total = notices.count()
        start = (page - 1) * INBOX_PAGE_SIZE
        rows = list(notices.order_by("-id")[start:start + INBOX_PAGE_SIZE]) if start < total else []
        targets = worker_notice_targets(request.user, rows) if rows else {}
        items = [{
            "id": row.pk,
            "kind": row.kind,
            "title": NOTICE_TEMPLATES[row.kind].title,
            "order_no": notice_display_order_no(row.order_no, row.details),
            "lines": notice_text_lines(row.kind, row.order_no, visible_notice_details(row, targets[row.pk])),
            "target": targets[row.pk],
            "created_at": row.created_at,
            "read_at": row.read_at,
        } for row in rows]
        return api_response({"items": items, "unread_count": unread, "total": total, "page_size": INBOX_PAGE_SIZE})


class WxPusherInboxReadView(WxPusherView):
    def post(self, request, pk):
        matched = _worker_inbox(request).filter(pk=pk)
        matched.filter(read_at__isnull=True).update(read_at=timezone.now())
        read_at = matched.values_list("read_at", flat=True).first()
        if read_at is None:
            raise NotFound()
        return api_response({"read_at": read_at})
