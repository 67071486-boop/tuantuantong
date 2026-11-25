"""Online payroll reservation, provider dispatch markers, and final accounting."""
import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from apps.accounts.display import user_display_name
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.accounts.models import StaffProfile
from apps.audit.actions import ACTION_LOG_TYPES, ONLINE_PAYROLL_SETTLE_FIELD
from apps.audit.log import record
from apps.business.alipay_auth_crypto import cipher, decrypt, encrypt
from apps.business.game_scope import game_scope
from apps.business.models import (
    AlipaySettlementAuthorization, DailiantongPush, EmpType, OnlinePayrollAdjustment, OnlinePayrollBatch,
    OnlinePayrollGatewayBatch, OnlinePayrollItem, OnlinePayrollRequest, OnlinePayrollTransfer, Order, Performance,
    PerformanceAdjustment, SettleStatus,
)
from apps.business.online_payroll_gateway import safe_payment_url as validate_payment_url
from apps.business.performance_amount import AmountError, deduct_amount
from apps.business.settlement import _resolve_batch
from apps.business.settlement_amount import payable_net
from apps.business.settlement_persistence import persist_online_settlement
from apps.core.scoping import scope_queryset
from apps.settlement_control.models import Authorization

CATEGORY_TYPE = {
    OnlinePayrollBatch.Category.TAKER: EmpType.MAIN,
    OnlinePayrollBatch.Category.CUSTOMER_SERVICE: EmpType.SALES,
}
Transfer = OnlinePayrollTransfer.Status
Gateway = OnlinePayrollGatewayBatch.Status
FAILED_STATES = {Transfer.FAILED, Transfer.CLOSED}
BATCH_SIZE = 100


class PayrollError(Exception):
    pass


def mask_account(account):
    return f"{account[:3]}****{account[-4:]}" if len(account) > 7 else "****"


def _hash(data):
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


def _record_payroll_batch(batch, user, reason):
    action = "finance.online_payroll"
    record(log_type=ACTION_LOG_TYPES[action], action=action,
           actor=user.get_username(), actor_id=user.pk, target_kind="online_payroll",
           target_id=str(batch.pk), target_name=str(batch.pk), reason=reason)


def _scope(user, queryset):
    return scope_queryset(user, queryset, scope_filter=game_scope("order__game_id"))


def can_view_batch(batch, user):
    ids = OnlinePayrollItem.objects.filter(transfer__gateway_batch__batch=batch).values("performance_id")
    return not Performance._base_manager.filter(pk__in=ids).exclude(
        pk__in=_scope(user, Performance._base_manager.all()).values("pk")
    ).exists()


def _ready_payers():
    if not getattr(settings, "ALIPAY_AUTH_LOCAL_ENABLED", False):
        return AlipaySettlementAuthorization.objects.none()
    control = Authorization.objects.filter(
        client="local", phase="authorized", status="authorized", configured=True,
    ).exclude(app_id="").exclude(ciphertext=b"")
    return AlipaySettlementAuthorization.objects.filter(
        request_id__in=control.values("request_id"), status="authorized", requires_platform_review=False,
        next_action="", last_error="",
    )


def _quote(ids, category, user, *, lock=False):
    batch = _resolve_batch(ids, user=user, lock=lock)
    rows = list(batch.rows)
    if set(ids) != {p.pk for p in rows} or batch.already_traced:
        raise PayrollError("仅可结算已完成订单中尚未结账的报酬，请刷新后重试")
    if category == OnlinePayrollBatch.Category.TAKER and DailiantongPush.objects.filter(
        order_id__in=[p.order_id for p in rows], state__in=DailiantongPush.ACTIVE_STATES,
    ).exists():
        raise PayrollError("所选订单有代练通付款记录或仍可能由代练通支付，请核对后再处理")
    if any(p.emp_type != CATEGORY_TYPE[category] for p in rows):
        raise PayrollError("打手和客服报酬请分开结算")
    if any(p.online_payroll_batch_id for p in rows):
        raise PayrollError("所选报酬已在其他付款单中，请前往在线结算明细查看")
    adjustments_by_row = defaultdict(list)
    adjustments = batch.adjustments.order_by("pk")
    if lock:
        adjustments = adjustments.select_for_update(of=("self",))
    for adjustment in adjustments:
        adjustments_by_row[adjustment.performance_id].append(adjustment)
    profiles = StaffProfile.objects.filter(user_id__in={p.user_id for p in rows})
    if lock:
        profiles = profiles.select_for_update().order_by("pk")
    profiles = {p.user_id: p for p in profiles}
    groups = {}
    fingerprint = []
    for row in sorted(rows, key=lambda p: p.pk):
        profile = profiles.get(row.user_id)
        account = profile.alipay_account.strip() if profile else ""
        name = profile.alipay_name.strip() if profile else ""
        if not account or not name:
            raise PayrollError(f"{user_display_name(row.user)}未填写完整的支付宝收款信息")
        if profile.settle_forbidden:
            raise PayrollError(f"{user_display_name(row.user)}暂不可结算")
        group = groups.setdefault(row.user_id, {
            "recipient_id": row.user_id, "recipient_name": user_display_name(row.user),
            "account": account, "name": name, "account_masked": mask_account(account),
            "amount": Decimal("0.00"), "items": [],
        })
        adjustments = adjustments_by_row[row.pk]
        try:
            net = payable_net(row, adjustments)
            deduction = deduct_amount(mode=row.deduct_mode, value=row.deduct_value, base=row.temp_amount)
        except AmountError:
            raise PayrollError(f"订单 {row.order.order_no} 的结算金额配置有误") from None
        delta = sum((a.amount for a in adjustments), Decimal("0.00"))
        if abs(delta) >= Decimal("10000000000") or abs(net) >= Decimal("10000000000"):
            raise PayrollError(f"订单 {row.order.order_no} 的结算金额超出范围")
        adjustment_ids = [a.pk for a in adjustments]
        group["items"].append({
            "performance_id": row.pk, "order_no": row.order.order_no, "bonus": row.bonus_amount,
            "deduction": deduction, "adjustment": delta, "amount": net, "adjustment_ids": adjustment_ids,
        })
        group["amount"] += net
        fingerprint.append([row.pk, row.user_id, account, name, row.bonus_amount, deduction, net,
                            [[a.pk, a.amount] for a in adjustments]])
    invalid = [g["recipient_name"] for g in groups.values() if g["amount"] <= 0]
    if invalid:
        raise PayrollError("以下人员实付金额不大于零：" + "、".join(invalid))
    token = salted_hmac("online-payroll-preview", json.dumps([category, fingerprint], default=str),
                        algorithm="sha256").hexdigest()
    return token, list(groups.values())


def _previous_batch(ids, user):
    held = list(_scope(user, Performance.objects.filter(pk__in=ids))
                .values_list("online_payroll_batch_id", flat=True))
    existing = set(held) - {None}
    if not existing:
        return None
    if len(existing) == 1 and len(held) == len(ids):
        batch = OnlinePayrollBatch.objects.get(pk=next(iter(existing)))
        original = set(OnlinePayrollItem.objects.filter(transfer__gateway_batch__batch=batch)
                       .values_list("performance_id", flat=True))
        if original == set(ids) and can_view_batch(batch, user):
            return batch
    raise PayrollError("所选报酬有一部分已在其他付款单中，请前往在线结算明细查看")


ITEM_FIELDS = ("performance_id", "order_no", "bonus", "deduction", "adjustment", "amount")


def public_item(item):
    data = {k: getattr(item, k) if isinstance(item, OnlinePayrollItem) else item[k] for k in ITEM_FIELDS}
    return {key: format(value, ".2f") if isinstance(value, Decimal) else value for key, value in data.items()}


def preview(performance_ids, category, *, user):
    ids = sorted(set(performance_ids))
    existing = _previous_batch(ids, user)
    if existing and existing.category == category and batch_is_active(existing):
        return {"existing_batch_id": str(existing.pk), "fingerprint": "", "total": "0.00", "transfers": [], "payers": []}
    fingerprint, groups = _quote(ids, category, user)
    payers = []
    rows = list(_ready_payers()) if getattr(settings, "ONLINE_PAYROLL_ENABLED", False) else []
    if rows:
        box = cipher()
        for row in rows:
            payers.append({"id": row.pk, "account_masked": mask_account(decrypt(row.account_ciphertext, box)),
                           "real_name": decrypt(row.real_name_ciphertext, box)})
    return {
        "fingerprint": fingerprint, "total": format(sum(g["amount"] for g in groups), ".2f"), "payers": payers,
        "transfers": [{"recipient_name": g["recipient_name"], "account_masked": g["account_masked"],
                       "amount": str(g["amount"]), "items": [public_item(i) for i in g["items"]]} for g in groups],
    }


@transaction.atomic
def create_batch(performance_ids, category, *, payer_id, fingerprint, request_id, user):
    ids = sorted(set(performance_ids))
    selection_hash = _hash([category, ids])
    # Serialize retries of the same request even when a conflicting payload selects unrelated rows.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(request_id.bytes[:8], "big", signed=True)])
    previous = OnlinePayrollRequest.objects.select_related("batch").filter(request_id=request_id).first()
    if previous:
        if (previous.batch.selection_hash != selection_hash or previous.payer_id != payer_id
                or previous.fingerprint != fingerprint or not can_view_batch(previous.batch, user)):
            raise PayrollError("请求标识已用于另一笔付款单，请刷新后重试")
        return previous.batch
    if not getattr(settings, "ONLINE_PAYROLL_ENABLED", False):
        raise PayrollError("在线结算暂未开启")
    rows = _scope(user, Performance.objects.filter(pk__in=ids))
    list(Order.objects.filter(pk__in=rows.values("order_id")).select_for_update().order_by("pk"))
    list(rows.select_for_update(of=("self",)).order_by("pk"))
    existing = _previous_batch(ids, user)
    if existing and existing.category == category and batch_is_active(existing):
        OnlinePayrollRequest.objects.create(request_id=request_id, batch=existing, fingerprint=fingerprint, payer_id=payer_id)
        return existing
    payer = _ready_payers().select_for_update().filter(pk=payer_id).first()
    if payer is None:
        raise PayrollError("所选付款支付宝目前不可用，请重新选择")
    actual, groups = _quote(ids, category, user, lock=True)
    if actual != fingerprint:
        raise PayrollError("报酬金额或收款信息已变更，请重新预览后确认")
    box = cipher()
    batch = OnlinePayrollBatch.objects.create(
        request_id=request_id, selection_hash=selection_hash, fingerprint=fingerprint, category=category,
        authorization=payer, created_by=user, payer_masked=mask_account(decrypt(payer.account_ciphertext, box)),
    )
    OnlinePayrollRequest.objects.create(request_id=request_id, batch=batch, fingerprint=fingerprint, payer_id=payer_id)
    for offset in range(0, len(groups), BATCH_SIZE):
        gateway = OnlinePayrollGatewayBatch.objects.create(
            batch=batch, mch_batch_no=f"tttb{batch.pk.hex}{offset // BATCH_SIZE}",
        )
        for index, group in enumerate(groups[offset:offset + BATCH_SIZE], start=offset):
            transfer = OnlinePayrollTransfer.objects.create(
                gateway_batch=gateway, recipient_id=group["recipient_id"], recipient_name=group["recipient_name"],
                account_masked=group["account_masked"], amount=group["amount"],
                recipient_ciphertext=encrypt(json.dumps({"account": group["account"], "name": group["name"]}), box),
                mch_order_no=f"ttto{batch.pk.hex}{index}",
            )
            for data in group["items"]:
                item = OnlinePayrollItem.objects.create(transfer=transfer, **{k: data[k] for k in ITEM_FIELDS})
                OnlinePayrollAdjustment.objects.bulk_create([
                    OnlinePayrollAdjustment(item=item, adjustment_id=pk) for pk in data["adjustment_ids"]
                ])
    Performance.objects.filter(pk__in=ids).update(online_payroll_batch=batch)
    _record_payroll_batch(batch, user, "已确认制单")
    # The worker reuses this module to advance the persisted batch.
    from apps.business.online_payroll_worker import schedule_initial_processing

    schedule_initial_processing(batch.pk)
    return batch


def batch_is_active(batch):
    return OnlinePayrollTransfer.objects.filter(gateway_batch__batch=batch).exclude(status__in=FAILED_STATES).filter(
        finalized_at__isnull=True
    ).exists()


def _locked_gateway(gateway_id):
    gateway = OnlinePayrollGatewayBatch.objects.get(pk=gateway_id)
    OnlinePayrollBatch.objects.select_for_update(no_key=True).get(pk=gateway.batch_id)
    return OnlinePayrollGatewayBatch.objects.select_for_update().get(pk=gateway_id)


def _locked_transfer(transfer_id):
    transfer = OnlinePayrollTransfer.objects.get(pk=transfer_id)
    _locked_gateway(transfer.gateway_batch_id)
    return OnlinePayrollTransfer.objects.select_for_update(of=("self",)).select_related("gateway_batch__batch__created_by").get(pk=transfer_id)


@transaction.atomic
def begin_submission(transfer_id):
    row = _locked_transfer(transfer_id)
    if row.status != Transfer.STAGED or row.gateway_batch.status != Gateway.STAGED:
        return False
    if not _ready_payers().select_for_update().filter(pk=row.gateway_batch.batch.authorization_id).first():
        return False
    row.status = Transfer.SUBMITTING
    row.save(update_fields=["status", "updated_at"])
    return True


@transaction.atomic
def record_submission(transfer_id, *, created, error=""):
    row = _locked_transfer(transfer_id)
    if row.status != Transfer.SUBMITTING:
        return
    row.status = Transfer.CREATED if created else Transfer.UNKNOWN
    row.last_error = error[:200]
    row.save(update_fields=["status", "last_error", "updated_at"])


@transaction.atomic
def begin_sealing(gateway_batch_id):
    gateway = _locked_gateway(gateway_batch_id)
    if (gateway.status != Gateway.STAGED or not gateway.transfers.exists()
            or gateway.transfers.exclude(status=Transfer.CREATED).exists()):
        return False
    if not _ready_payers().select_for_update().filter(pk=gateway.batch.authorization_id).first():
        return False
    gateway.status = Gateway.SEALING
    gateway.save(update_fields=["status", "updated_at"])
    return True


@transaction.atomic
def record_sealed(gateway_batch_id, *, payment_url=None, error=""):
    gateway = _locked_gateway(gateway_batch_id)
    if gateway.status not in {Gateway.SEALING, Gateway.UNKNOWN, Gateway.PAYABLE}:
        return
    if validate_payment_url(payment_url):
        gateway.payment_ciphertext = encrypt(payment_url, cipher())
        gateway.status = Gateway.PAYABLE
        gateway.last_error = ""
    elif gateway.status != Gateway.PAYABLE:
        gateway.status = Gateway.UNKNOWN
        gateway.last_error = error[:200] or "付款码暂不可用，请稍后查询"
    gateway.save(update_fields=["status", "last_error", "payment_ciphertext", "updated_at"])


def gateway_can_pay(gateway):
    return (gateway.status == Gateway.PAYABLE and bool(gateway.payment_ciphertext)
            and gateway.transfers.filter(status__in=(Transfer.CREATED, Transfer.PROCESSING)).exists())


def _release(items, batch_id):
    ids = list(items.filter(released_at__isnull=True).values_list("performance_id", flat=True))
    list(Performance.objects.filter(pk__in=ids).select_for_update().order_by("pk"))
    Performance.objects.filter(pk__in=ids, online_payroll_batch_id=batch_id).update(online_payroll_batch=None)
    items.filter(performance_id__in=ids, released_at__isnull=True).update(released_at=timezone.now())


@transaction.atomic
def cancel_batch(batch_id, *, user):
    batch = OnlinePayrollBatch.objects.select_for_update(no_key=True).get(pk=batch_id)
    if not can_view_batch(batch, user):
        raise PayrollError("付款单不存在或不可见")
    transfers = OnlinePayrollTransfer.objects.filter(gateway_batch__batch=batch)
    staged_ids = list(transfers.filter(status=Transfer.STAGED).values_list("pk", flat=True))
    if not staged_ids:
        if transfers.exclude(status=Transfer.CLOSED).exists():
            raise PayrollError("付款单已开始向计全付制单，请查清原单结果后再处理")
        return batch
    _release(OnlinePayrollItem.objects.filter(transfer_id__in=staged_ids), batch.pk)
    OnlinePayrollTransfer.objects.filter(pk__in=staged_ids).update(status=Transfer.CLOSED)
    gateways = OnlinePayrollGatewayBatch.objects.filter(batch=batch, transfers__pk__in=staged_ids)
    closed_ids = list(gateways.exclude(transfers__status__in=(Transfer.SUBMITTING, Transfer.CREATED,
                       Transfer.PROCESSING, Transfer.SUCCEEDED, Transfer.UNKNOWN)).values_list("pk", flat=True))
    OnlinePayrollGatewayBatch.objects.filter(pk__in=closed_ids).update(status=Gateway.CLOSED, payment_ciphertext="")
    gateways.exclude(pk__in=closed_ids).update(last_error="未发出的明细已取消，已发出部分仍需核对")
    _record_payroll_batch(batch, user, f"取消 {len(staged_ids)} 笔尚未制单的明细")
    return batch


def _finalize(transfer, success_time):
    batch = transfer.gateway_batch.batch
    items = list(transfer.items.filter(released_at__isnull=True).order_by("performance_id"))
    ids = [i.performance_id for i in items]
    rows = list(Performance.objects.filter(pk__in=ids).select_for_update().order_by("pk"))
    adjustments = list(PerformanceAdjustment.objects.filter(
        pk__in=OnlinePayrollAdjustment.objects.filter(item_id__in=[i.pk for i in items]).values("adjustment_id"),
    ).select_for_update().order_by("pk"))
    if (not items or len(rows) != len(items) or any(p.is_settled or p.online_payroll_batch_id != batch.pk for p in rows)
            or any(a.settle_status != SettleStatus.UNSETTLED for a in adjustments)):
        return False
    actor = batch.created_by.get_username()
    persist_online_settlement(rows, adjustments, items, actor=actor, settled_at=success_time)
    for item in items:
        record(log_type=ACTION_LOG_TYPES["performance.settle"], action="performance.settle",
               actor=actor, actor_id=batch.created_by_id, target_kind="user", target_id=transfer.recipient_id,
               target_name=transfer.recipient_name, order_no=item.order_no,
               field_name=ONLINE_PAYROLL_SETTLE_FIELD, reason="支付宝在线结算",
               from_value="未结账", to_value="已结账")
    return True


@transaction.atomic
def apply_transfer_result(transfer_id, *, state, success_time=None, transfer_no="", channel_order_no="", error=""):
    """Apply an authenticated result matched to the persisted order and amount."""
    status = {0: Transfer.CREATED, 1: Transfer.PROCESSING, 2: Transfer.SUCCEEDED,
              3: Transfer.FAILED, 4: Transfer.CLOSED}.get(state)
    if status is None:
        return
    row = _locked_transfer(transfer_id)
    if row.status in FAILED_STATES or row.status == Transfer.STAGED or row.finalized_at:
        return
    if row.status == Transfer.SUCCEEDED and status != Transfer.SUCCEEDED:
        return
    if row.status == Transfer.PROCESSING and status == Transfer.CREATED:
        return
    first_success = status == Transfer.SUCCEEDED and row.status != Transfer.SUCCEEDED
    row.status = status
    row.transfer_id = transfer_no[:128] or row.transfer_id
    row.channel_order_no = channel_order_no[:128] or row.channel_order_no
    row.last_error = error[:200] if status in FAILED_STATES else ""
    if status == Transfer.SUCCEEDED:
        if success_time is not None and timezone.is_aware(success_time):
            row.success_time = row.success_time or success_time
        if row.success_time is None:
            row.last_error = "打款成功，待补结账时间"
        elif _finalize(row, row.success_time):
            row.finalized_at = timezone.now()
        else:
            row.last_error = "打款成功，待核对结账记录"
    elif status in FAILED_STATES:
        _release(row.items.all(), row.gateway_batch.batch_id)
    row.save(update_fields=["status", "last_error", "transfer_id", "channel_order_no", "success_time", "finalized_at", "updated_at"])
    if first_success:
        order_nos = set(row.items.values_list("order_no", flat=True))
        details = {"amount": format(row.amount, ".2f"), "related_order_nos": sorted(order_nos - {""})}
        if row.success_time is not None:
            details["occurred_at"] = row.success_time.isoformat()
        enqueue_worker_notice(
            user_id=row.recipient_id, kind="transfer_succeeded", reference=f"transfer:{row.pk}", details=details,
        )


@transaction.atomic
def request_payment_refresh(gateway_id):
    gateway = _locked_gateway(gateway_id)
    if not gateway_can_pay(gateway):
        return gateway
    gateway.payment_ciphertext = ""
    gateway.status = Gateway.UNKNOWN
    gateway.save(update_fields=["payment_ciphertext", "status", "updated_at"])
    return gateway
