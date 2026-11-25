"""Finance and recipient views of online payroll."""
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.qr import qr_png_base64
from apps.business import online_payroll as payroll
from apps.business.alipay_auth_crypto import cipher, decrypt
from apps.business.online_payroll_gateway import PayrollGatewayError
from apps.business.online_payroll_worker import desktop_payment_url
from apps.business.models import (
    OnlinePayrollBatch, OnlinePayrollGatewayBatch, OnlinePayrollTransfer, Performance,
)
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane
from apps.rbac.permissions import has_node, has_node_now

SETTLE_NODE = "finance.change_settlement_status"
DETAIL_NODE = "finance.online_settlement_details"


class PayrollPreviewInput(serializers.Serializer):
    performance_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1), min_length=1, max_length=500,
    )
    category = serializers.ChoiceField(choices=OnlinePayrollBatch.Category.values)


class PayrollCreateInput(PayrollPreviewInput):
    payer_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    fingerprint = serializers.RegexField(r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    request_id = serializers.UUIDField()


class PaymentInput(serializers.Serializer):
    gateway_batch_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    refresh_link = serializers.BooleanField(default=False)
    desktop = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if attrs['desktop'] and attrs['refresh_link']:
            raise serializers.ValidationError('请分别获取电脑付款链接和刷新扫码付款码')
        return attrs


class PageInput(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, max_value=1000000, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=25)


def _page(params, rows):
    form = PageInput(data=params)
    form.is_valid(raise_exception=True)
    page = form.validated_data["page"]
    size = form.validated_data["page_size"]
    return {"count": rows.count(), "page": page, "page_size": size}, rows[(page - 1) * size:page * size]


def _response(data):
    response = api_response(data=data)
    response["Cache-Control"] = "no-store"
    return response


def _error(exc):
    return api_response(data=None, msg=str(exc), code=ErrorCode.SETTLE_STATE_INVALID)


def _transfer(row, *, finance=False):
    batch = row.gateway_batch.batch
    data = {
        "id": row.pk, "batch_id": str(batch.pk), "category": batch.category, "created_at": local_iso(batch.created_at),
        "status": row.status, "recipient_name": row.recipient_name, "account_masked": row.account_masked,
        "amount": str(row.amount), "channel_order_no": row.channel_order_no, "transfer_id": row.transfer_id,
        "success_time": local_iso(row.success_time), "finalized_at": local_iso(row.finalized_at),
        "last_error": row.last_error, "items": [payroll.public_item(i) for i in row.items.all()],
    }
    if finance:
        data["mch_order_no"] = row.mch_order_no
    return data


def _terminal_status(transfers):
    if any(not row.finalized_at and row.status not in payroll.FAILED_STATES for row in transfers):
        return None
    paid = sum(bool(row.finalized_at) for row in transfers)
    if paid:
        return "succeeded" if paid == len(transfers) else "partial"
    if any(row.status == payroll.Transfer.FAILED for row in transfers):
        return "failed"
    return "closed"


def _batch_detail(batch, *, payment_gateway_id=None, can_operate=True):
    gateways = [(gateway, list(gateway.transfers.all())) for gateway in batch.gateway_batches.all()]
    transfers = [row for _, rows in gateways for row in rows]
    status = _terminal_status(transfers)
    active = status is None
    payability = {
        gateway.pk: (gateway.status == payroll.Gateway.PAYABLE and bool(gateway.payment_ciphertext)
                    and any(t.status in (payroll.Transfer.CREATED, payroll.Transfer.PROCESSING) for t in rows))
        for gateway, rows in gateways
    }
    if active:
        if any(row.status in (payroll.Transfer.UNKNOWN, payroll.Transfer.SUCCEEDED) and not row.finalized_at
               for row in transfers):
            status = "needs_review"
        elif any(payability.values()):
            status = "ready"
        elif any(gateway.status == payroll.Gateway.UNKNOWN or (gateway.last_error and gateway.status == payroll.Gateway.STAGED)
                 for gateway, _ in gateways):
            status = "needs_review"
        elif any(row.status == payroll.Transfer.PROCESSING for row in transfers):
            status = "processing"
        else:
            status = "preparing"
    gateway_data = []
    for gateway, rows in gateways:
        available = can_operate and payability[gateway.pk]
        payment_url = None
        if available and gateway.pk == payment_gateway_id:
            url = decrypt(gateway.payment_ciphertext, cipher())
            if payroll.validate_payment_url(url):
                payment_url = url
        gateway_data.append({
            "id": gateway.pk, "mch_batch_no": gateway.mch_batch_no, "status": _terminal_status(rows) or gateway.status,
            "can_prepare_payment": available, "payment_url": payment_url,
            "payment_qr_png_base64": qr_png_base64(payment_url) if payment_url else None,
            "last_error": gateway.last_error,
            "transfers": [_transfer(row, finance=True) for row in rows],
        })
    return {
        "id": str(batch.pk), "category": batch.category, "status": status, "active": active,
        "can_cancel": can_operate and any(row.status == payroll.Transfer.STAGED for row in transfers),
        "created_at": local_iso(batch.created_at), "payer_masked": batch.payer_masked,
        "total": str(sum(row.amount for row in transfers)), "gateway_batches": gateway_data,
    }


def _visible_batches(user):
    scoped = payroll._scope(user, Performance._base_manager.all())
    return OnlinePayrollBatch.objects.exclude(
        gateway_batches__transfers__items__performance_id__in=Performance._base_manager.exclude(
            pk__in=scoped.values("pk"),
        ).values("pk"),
    )


def _load_batch(batch_id, user):
    return get_object_or_404(_visible_batches(user), pk=batch_id)


def _with_details(queryset):
    transfers = OnlinePayrollTransfer.objects.select_related("gateway_batch__batch").prefetch_related("items").order_by("pk")
    gateways = OnlinePayrollGatewayBatch.objects.prefetch_related(Prefetch("transfers", queryset=transfers)).order_by("pk")
    return queryset.prefetch_related(Prefetch("gateway_batches", queryset=gateways))


class OnlinePayrollPreviewView(APIView):
    def get_permissions(self):
        return [(has_node(SETTLE_NODE) & has_node(DETAIL_NODE))()]

    def post(self, request):
        form = PayrollPreviewInput(data=request.data)
        form.is_valid(raise_exception=True)
        try:
            return _response(payroll.preview(**form.validated_data, user=request.user))
        except payroll.PayrollError as exc:
            return _error(exc)


class OnlinePayrollBatchCollectionView(APIView):
    def get_permissions(self):
        return [(has_node(SETTLE_NODE) & has_node(DETAIL_NODE))() if self.request.method == "POST" else has_node(DETAIL_NODE)()]

    def get(self, request):
        data, rows = _page(request.query_params, _visible_batches(request.user).order_by("-created_at", "pk"))
        data["items"] = []
        for row in _with_details(rows):
            item = _batch_detail(row, can_operate=has_node_now(request.user, SETTLE_NODE))
            item.pop("gateway_batches")
            data["items"].append(item)
        return _response(data)

    def post(self, request):
        form = PayrollCreateInput(data=request.data)
        form.is_valid(raise_exception=True)
        try:
            batch = payroll.create_batch(**form.validated_data, user=request.user)
        except payroll.PayrollError as exc:
            return _error(exc)
        return _response(_batch_detail(batch))


class OnlinePayrollBatchDetailView(APIView):
    def get_permissions(self):
        return [has_node(DETAIL_NODE)()]

    def get(self, request, batch_id):
        row = get_object_or_404(_with_details(_visible_batches(request.user)), pk=batch_id)
        return _response(_batch_detail(row, can_operate=has_node_now(request.user, SETTLE_NODE)))


class OnlinePayrollPaymentView(APIView):
    def get_permissions(self):
        return [(has_node(SETTLE_NODE) & has_node(DETAIL_NODE))()]

    def post(self, request, batch_id):
        form = PaymentInput(data=request.data)
        form.is_valid(raise_exception=True)
        batch = _load_batch(batch_id, request.user)
        gateway = get_object_or_404(batch.gateway_batches, pk=form.validated_data["gateway_batch_id"])
        if form.validated_data['desktop']:
            try:
                url = desktop_payment_url(gateway.pk)
            except PayrollGatewayError as exc:
                return _error(exc)
            data = _batch_detail(batch, payment_gateway_id=gateway.pk)
            for part in data['gateway_batches']:
                if part['id'] == gateway.pk and part['can_prepare_payment']:
                    part['payment_pc_url'] = url
            return _response(data)
        if form.validated_data["refresh_link"]:
            payroll.request_payment_refresh(gateway.pk)
        return _response(_batch_detail(batch, payment_gateway_id=gateway.pk))


class OnlinePayrollCancelView(APIView):
    def get_permissions(self):
        return [(has_node(SETTLE_NODE) & has_node(DETAIL_NODE))()]

    def post(self, request, batch_id):
        batch = _load_batch(batch_id, request.user)
        try:
            batch = payroll.cancel_batch(batch.pk, user=request.user)
        except payroll.PayrollError as exc:
            return _error(exc)
        return _response(_batch_detail(batch))


class OnlinePayrollMemberPermission(IsAuthenticated):
    def has_permission(self, request, view):
        return not on_public_plane() and super().has_permission(request, view)


class OnlinePayrollMineView(APIView):
    permission_classes = [OnlinePayrollMemberPermission]

    def get(self, request):
        transfers = OnlinePayrollTransfer.objects.filter(recipient=request.user).select_related(
            "gateway_batch__batch",
        ).prefetch_related("items").order_by("-gateway_batch__batch__created_at", "-pk")
        data, rows = _page(request.query_params, transfers)
        data["items"] = [_transfer(row) for row in rows]
        return _response(data)
