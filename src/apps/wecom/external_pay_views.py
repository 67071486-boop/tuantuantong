"""企微对外收款同步状态和待处理账单。"""
from decimal import Decimal
from django.db.models import Exists, OuterRef, Q

from apps.business.game_scope import game_scope
from apps.business.models import Order
from apps.business.order_flags import scope_by_creator
from apps.core.envelope import api_response, paginated
from apps.core.permissions import is_tenant_admin
from apps.core.scoping import scope_queryset
from apps.core.permissions import is_tenant_admin
from apps.wecom.models import ExternalPayBill, ExternalPayInvalidBill, ExternalPayState
from apps.wecom.views import MessagesPageSerializer, WecomSettingsGateView, get_config


class ExternalPayStatusView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = get_config()
        ready = bool(config and config.is_ready())
        state = ExternalPayState.objects.filter(identity_revision=config.identity_revision, corp_id=config.corp_id).first() if config else None
        invalid_count = ExternalPayInvalidBill.objects.filter(state=state, resolved=False).count() if state else 0
        orders = Order.all_objects.all() if is_tenant_admin(request.user) else Order.objects.all()
        visible = scope_by_creator(request.user, scope_queryset(request.user, orders, scope_filter=game_scope('game_id')))
        pending_bills = ExternalPayBill.objects.filter(state=state, mismatch_notification_pending=True) if state else ExternalPayBill.objects.none()
        if not is_tenant_admin(request.user):
            hidden_numbers = Order.objects.exclude(pk__in=visible.values('pk')).exclude(source_order_no='').values('source_order_no')
            pending_bills = pending_bills.exclude(transaction_id__in=hidden_numbers)
        pending_notices = pending_bills.count()
        pending_orders = visible.filter(wecom_notice_pending=True).count()
        error = state.last_error if state else ""
        if invalid_count:
            error = f"{error}；" if error else ""
            error += f"有 {invalid_count} 条企微账单字段异常，无法自动处理"
        if pending_notices or pending_orders:
            error = f"{error}；" if error else ""
            error += f"有 {pending_notices + pending_orders} 项企微通知等待补偿"
        return api_response({
            "configured": ready, "enabled": bool(ready and config.external_pay_enabled),
            "last_run_at": state.last_run_at if state else None,
            "last_success_at": state.last_success_at if state else None,
            "last_error": error or None,
        })


class ExternalPayIssuesView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = MessagesPageSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        page = serializer.validated_data["page"]
        config = get_config()
        qs = ExternalPayBill.objects.none() if config is None else ExternalPayBill.objects.filter(
            state__identity_revision=config.identity_revision, state__corp_id=config.corp_id
        ).exclude(issue="").order_by("-pay_time", "-pk")
        all_orders = Order.objects.all()
        visible = scope_by_creator(request.user, scope_queryset(request.user, all_orders, scope_filter=game_scope("game_id")))
        hidden_numbers = all_orders.exclude(pk__in=visible.values("pk")).exclude(source_order_no="").values("source_order_no")
        hidden_payment = ExternalPayBill.objects.filter(state_id=OuterRef("state_id"), bill_type=0, mch_id=OuterRef("mch_id"), out_trade_no=OuterRef("out_trade_no"), transaction_id__in=hidden_numbers)
        qs = qs.annotate(_hidden_payment=Exists(hidden_payment)).exclude(
            Q(bill_type=0, transaction_id__in=hidden_numbers) | Q(bill_type=1, _hidden_payment=True)
        )
        total = qs.count()
        result = []
        for bill in qs[(page - 1) * 20:page * 20]:
            source_no = bill.transaction_id
            if bill.bill_type == 1:
                payments = list(ExternalPayBill.objects.filter(state=bill.state, bill_type=0, mch_id=bill.mch_id, out_trade_no=bill.out_trade_no).values_list("transaction_id", flat=True)[:2])
                source_no = payments[0] if len(payments) == 1 else ""
            candidates = visible.filter(source_order_no=source_no).order_by("id")
            if not source_no:
                candidates = candidates.none()
            count = candidates.count()
            result.append({
                "id": bill.pk, "bill_type": bill.bill_type, "transaction_id": bill.transaction_id,
                "source_order_no": source_no,
                "payment_amount": f"{Decimal(bill.total_fee) / 100:.2f}",
                "pay_time": bill.pay_time, "reason": bill.issue,
                "candidate_count": count,
                "candidates": [{"order_no": item.order_no, "order_amount": str(item.total_price)} for item in candidates[:10]],
            })
        return api_response(paginated(result, total=total, page=page, page_size=20))


class ExternalPayInvalidView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = MessagesPageSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        page = serializer.validated_data["page"]
        config = get_config()
        qs = ExternalPayInvalidBill.objects.none() if config is None else ExternalPayInvalidBill.objects.filter(state__identity_revision=config.identity_revision, state__corp_id=config.corp_id, resolved=False).order_by("id")
        total = qs.count()
        admin = is_tenant_admin(request.user)
        items = []
        for row in qs[(page - 1) * 20:page * 20]:
            payload = row.payload
            identity = bool(row.search_start and row.search_end) and all(isinstance(payload.get(k), str) and payload[k] for k in ("transaction_id", "mch_id", "out_trade_no")) and type(payload.get("bill_type")) is int and payload.get("bill_type") in (0, 1)
            items.append({"id": row.pk, "reason": row.issue, "window_start": row.search_start, "window_end": row.search_end, "last_retry_at": row.last_retry_at, "identifiable": bool(identity), "payload": payload if admin else None})
        return api_response(paginated(items, total=total, page=page, page_size=20))
