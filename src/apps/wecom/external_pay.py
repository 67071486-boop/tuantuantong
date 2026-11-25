"""对外收款账单的可恢复分页与订单对账。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from dataclasses import dataclass
from collections import deque
from decimal import Decimal
import hashlib
import json
from uuid import uuid4
import time

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.business import order_writes, refunds
from apps.business.models import Order, PaymentStatus
from apps.wecom import sdk
from apps.wecom.group_sync import TimedSession
from apps.wecom.models import ExternalPayBill, ExternalPayInvalidBill, ExternalPayRecheck, ExternalPayState, WecomAppConfig

WINDOW = timedelta(days=28)
RETRY = timedelta(hours=1)
EXPIRY = timedelta(days=3)
LOCK_NAMESPACE = 0x57455059
CTX = order_writes.WriteContext(log_actor="企微收款同步")


class SyncError(Exception):
    pass


class SyncPaused(Exception):
    pass


@dataclass(frozen=True)
class InvalidBill:
    payload: dict
    issue: str


def _safe_invalid(row, issue):
    keys = ("bill_type", "transaction_id", "mch_id", "out_trade_no", "out_refund_no", "total_fee", "total_refund_fee", "pay_time", "trade_state")
    row = row if isinstance(row, dict) else {}
    payload = {}
    for key in keys:
        value = row.get(key)
        payload[key] = value[:512] if isinstance(value, str) else value if value is None or isinstance(value, (int, bool)) else "<非法类型>"
    return InvalidBill(payload, str(issue)[:300])


def _quarantine(state, bad, start=None, end=None):
    digest = hashlib.sha256(json.dumps(bad.payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    row, created = ExternalPayInvalidBill.objects.get_or_create(state=state, fingerprint=digest, defaults={"payload": bad.payload, "issue": bad.issue, "search_start": start, "search_end": end})
    if not created and row.resolved:
        row.resolved = False
        row.last_retry_at = None
        row.search_start, row.search_end = start, end
        row.search_cursor, row.search_seen = "", []
        row.save(update_fields=["resolved", "last_retry_at", "search_start", "search_end", "search_cursor", "search_seen"])


def _resolve_invalid(state, data):
    ExternalPayInvalidBill.objects.filter(
        state=state, resolved=False, payload__bill_type=data["bill_type"],
        payload__transaction_id=data["transaction_id"], payload__mch_id=data["mch_id"],
        payload__out_trade_no=data["out_trade_no"],
    ).update(resolved=True)


def _int(value, field, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 999999999999:
        raise SyncError(f"企微账单字段 {field} 不合法")
    return value


def _text(value, field, *, optional=False):
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 128:
        raise SyncError(f"企微账单字段 {field} 不合法")
    return value


def parse_bill(row):
    if not isinstance(row, dict):
        raise SyncError("企微账单数据格式错误")
    kind = _int(row.get("bill_type"), "bill_type")
    if kind not in (0, 1):
        raise SyncError("企微账单类型未知")
    try:
        paid = datetime.fromtimestamp(_int(row.get("pay_time"), "pay_time"), tz=dt_timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise SyncError("企微账单付款时间不合法") from None
    if paid.year <= 1970 or paid > timezone.now() + timedelta(minutes=5):
        raise SyncError("企微账单付款时间不合法")
    result = dict(
        bill_type=kind,
        transaction_id=_text(row.get("transaction_id"), "transaction_id"),
        mch_id=_text(row.get("mch_id"), "mch_id"),
        out_trade_no=_text(row.get("out_trade_no"), "out_trade_no"),
        out_refund_no=_text(row.get("out_refund_no"), "out_refund_no", optional=kind == 0),
        payee_userid=(row.get("payee_userid") if isinstance(row.get("payee_userid"), str) and row.get("payee_userid") == row.get("payee_userid").strip() and len(row.get("payee_userid")) <= 128 else ""),
        total_fee=_int(row.get("total_fee"), "total_fee"),
        total_refund_fee=_int(row.get("total_refund_fee"), "total_refund_fee", nullable=kind == 1),
        pay_time=paid,
        trade_state=_int(row.get("trade_state"), "trade_state", nullable=kind == 1),
    )
    if kind == 0 and result["total_refund_fee"] > result["total_fee"]:
        raise SyncError("企微累计退款超出原付款金额")
    return result


def _assert_identity(config):
    current = WecomAppConfig.objects.select_for_update().filter(
        pk=config.pk, identity_revision=config.identity_revision, corp_id=config.corp_id,
        agent_id=config.agent_id, secret=config.secret, enabled=True,
    ).exists()
    if not current:
        raise SyncError("企微配置已改变，本次同步已停止")
    if not WecomAppConfig.objects.filter(pk=config.pk, external_pay_enabled=True).exists():
        raise SyncPaused()


def _get_page(config, start, end, cursor):
    body = {"begin_time": int(start.timestamp()), "end_time": int(end.timestamp()), "limit": 100}
    if cursor:
        body["cursor"] = cursor
    client = sdk.get_client(config, timeout=(5, 20), auto_retry=False)
    client._http.close()
    client._http = TimedSession()
    try:
        try:
            response = client.post("/externalpay/get_bill_list", data=body)
        except Exception as exc:
            if getattr(exc, "errcode", None) not in (40001, 40014, 42001):
                raise
            client.fetch_access_token()
            response = client.post("/externalpay/get_bill_list", data=body)
    finally:
        client._http.close()
    if not isinstance(response, dict) or response.get("errcode") != 0 or not isinstance(response.get("bill_list"), list):
        raise SyncError("企微账单返回格式错误")
    next_cursor = response.get("next_cursor") or ""
    if not isinstance(next_cursor, str) or len(next_cursor) > 4096:
        raise SyncError("企微账单分页游标无效")
    rows = []
    for row in response["bill_list"]:
        try:
            rows.append(parse_bill(row))
        except SyncError as exc:
            rows.append(_safe_invalid(row, exc))
    return rows, next_cursor


def _save_bill(state, data):
    key = {name: data[name] for name in ("bill_type", "transaction_id", "mch_id")}
    bill = ExternalPayBill.objects.select_for_update().filter(state=state, **key).first()
    if bill:
        if bill.conflicted:
            return bill
        if any(getattr(bill, name) != data[name] for name in ("out_trade_no", "total_fee", "pay_time", "out_refund_no")):
            raise SyncError("企微账单身份或金额发生冲突")
        if bill.bill_type == 0:
            incoming = data["total_refund_fee"]
            if bill.total_refund_fee is not None and incoming < bill.total_refund_fee:
                _resolve_invalid(state, data)
                return bill
            bill.total_refund_fee = incoming
            bill.trade_state = data["trade_state"]
            bill.payee_userid = data["payee_userid"] or bill.payee_userid
            bill.next_retry_at = timezone.now()
            bill.save(update_fields=["total_refund_fee", "trade_state", "payee_userid", "next_retry_at"])
        _resolve_invalid(state, data)
        return bill
    bill = ExternalPayBill.objects.create(state=state, **data, next_retry_at=timezone.now())
    _resolve_invalid(state, data)
    return bill


def _ingest(state, data, start, end):
    if isinstance(data, InvalidBill):
        _quarantine(state, data, start, end)
        return None
    try:
        with transaction.atomic():
            return _save_bill(state, data)
    except SyncError as exc:
        raw = {**data, "pay_time": int(data["pay_time"].timestamp())}
        _quarantine(state, _safe_invalid(raw, exc), start, end)
        ExternalPayBill.objects.filter(state=state, bill_type=data["bill_type"], transaction_id=data["transaction_id"], mch_id=data["mch_id"]).update(conflicted=True, issue="企微账单身份或金额发生冲突，请人工核对", next_retry_at=None)


def pull_one_page(config, state):
    start, end, cursor = state.window_start, state.window_end, state.cursor
    rows, following = _get_page(config, start, end, cursor)
    with transaction.atomic():
        _assert_identity(config)
        fresh = ExternalPayState.objects.select_for_update().get(pk=state.pk)
        if (fresh.window_start, fresh.window_end, fresh.cursor) != (start, end, cursor):
            raise SyncError("同步位置已变化，请稍后重试")
        if following and (following == cursor or following in fresh.cursor_seen):
            raise SyncError("企微账单分页游标循环")
        for data in rows:
            _ingest(fresh, data, start, end)
        if following:
            fresh.cursor_seen = [*fresh.cursor_seen, following]
            fresh.cursor = following
        else:
            fresh.watermark = end
            fresh.window_start = fresh.window_end = None
            fresh.cursor = ""
            fresh.cursor_seen = []
            fresh.last_success_at = timezone.now()
        fresh.last_error = ""
        fresh.save()


def prepare_window(config, state, now):
    if state.window_start is not None:
        return True
    local = timezone.localtime(now)
    today_cutoff = local.replace(hour=2, minute=0, second=0, microsecond=0)
    if local < today_cutoff:
        today_cutoff -= timedelta(days=1)
    if state.watermark and state.watermark >= today_cutoff:
        return False
    start = state.watermark or (today_cutoff - EXPIRY)
    with transaction.atomic():
        _assert_identity(config)
        state = ExternalPayState.objects.select_for_update().get(pk=state.pk)
        if state.window_start is None:
            state.window_start = start
            state.window_end = min(start + WINDOW, today_cutoff)
            state.save(update_fields=["window_start", "window_end"])
    return True


def _issue(bill, message, now):
    if message == "企微收款金额与订单总价不一致" and (not bill.mismatch_active or bill.mismatch_notification_pending) and bill.bill_type == 0:
        from apps.wecom.notifications import queue_payment_mismatch
        bill.mismatch_notification_pending = not queue_payment_mismatch(bill)
        bill.mismatch_active = True
    elif message in ("尚未匹配到订单", "存在多张金额相同的候选订单"):
        bill.mismatch_active = False
        bill.mismatch_notification_pending = False
    bill.issue = message
    bill.next_retry_at = now + RETRY
    bill.save(update_fields=["issue", "next_retry_at", "mismatch_active", "mismatch_notification_pending", "mismatch_notice_corp_id", "mismatch_notice_agent_id", "mismatch_notice_enabled_at"])


def match_payment(bill_id, config, now):
    with transaction.atomic():
        _assert_identity(config)
        bill = ExternalPayBill.objects.select_for_update().get(pk=bill_id, state__identity_revision=config.identity_revision)
        if bill.bill_type != 0:
            return
        if bill.conflicted:
            _issue(bill, "企微账单身份或金额发生冲突，请人工核对", now)
            return
        if bill.trade_state not in (1, 3):
            _issue(bill, "企微付款状态不支持自动处理", now)
            return
        if bill.matched_at is None and not bill.refund_origin and now >= bill.pay_time + EXPIRY:
            if bill.mismatch_notification_pending:
                from apps.wecom.notifications import retry_expired_payment_mismatch
                bill.mismatch_notification_pending = not retry_expired_payment_mismatch(bill)
            bill.issue = "超过三天仍未匹配订单"
            bill.next_retry_at = now + RETRY if bill.mismatch_notification_pending else None
            bill.save(update_fields=["issue", "next_retry_at", "mismatch_notification_pending"])
            return
        candidates = Order.objects.filter(source_order_no=bill.transaction_id)
        matching = list(candidates.filter(total_price=Decimal(bill.total_fee) / 100).order_by("id")[:2]) if not bill.order_id else list(candidates.filter(pk=bill.order_id)[:1])
        if not matching:
            if candidates.exists():
                _issue(bill, "企微收款金额与订单总价不一致", now)
            else:
                _issue(bill, "尚未匹配到订单", now)
            return
        if len(matching) > 1:
            _issue(bill, "存在多张金额相同的候选订单", now)
            return
        order = order_writes.lock_order_and_sales_nowait(matching[0])
        if not order or order.source_order_no != bill.transaction_id or (not bill.order_id and order.total_price != Decimal(bill.total_fee) / 100):
            _issue(bill, "订单已变化，等待重新匹配", now)
            return
        if ExternalPayBill.objects.filter(state=bill.state, bill_type=0, order=order).exclude(pk=bill.pk).exists():
            _issue(bill, "该订单已关联另一笔企微付款，需人工核对", now)
            return
        try:
            refunds.external_refund_history_denial(order)
        except refunds.RefundError as exc:
            raise SyncError(exc.message) from None
        desired = Decimal(bill.total_refund_fee) / 100
        if desired > 0 and order.refund_fee != desired:
            outcome = order_writes.refund_total_write(order, refund_total=desired, reason="企微对外收款退款同步", request_id=str(uuid4()), ctx=CTX, dry_run=False, allow_external_refund=True)
            if not outcome.ok:
                transaction.set_rollback(True)
                raise SyncError(f"订单退款同步失败：{outcome.msg or '请检查订单及客服结账状态'}")
        if order.payment_status != PaymentStatus.SETTLED and order.total_price == Decimal(bill.total_fee) / 100:
            outcome = order_writes.change_payment_status(order, payment_status=PaymentStatus.SETTLED, ctx=CTX, dry_run=False)
            if not outcome.ok:
                transaction.set_rollback(True)
                raise SyncError("订单收款状态同步失败")
        bill.order = order
        bill.matched_at = bill.matched_at or now
        if order.total_price != Decimal(bill.total_fee) / 100 and (not bill.mismatch_active or bill.mismatch_notification_pending):
            from apps.wecom.notifications import queue_payment_mismatch
            bill.mismatch_notification_pending = not queue_payment_mismatch(bill)
        bill.mismatch_active = order.total_price != Decimal(bill.total_fee) / 100
        if not bill.mismatch_active:
            bill.mismatch_notification_pending = False
        bill.issue = "" if order.total_price == Decimal(bill.total_fee) / 100 else "企微收款金额与订单总价不一致"
        bill.next_retry_at = now + RETRY if bill.mismatch_notification_pending else None
        bill.save(update_fields=["order", "matched_at", "issue", "next_retry_at", "mismatch_active", "mismatch_notification_pending", "mismatch_notice_corp_id", "mismatch_notice_agent_id", "mismatch_notice_enabled_at"])
        if desired > 0:
            ExternalPayBill.objects.filter(state=bill.state, bill_type=1, mch_id=bill.mch_id, out_trade_no=bill.out_trade_no, search_start__isnull=True, issue="已找到原付款，等待订单退款同步").update(issue="")


def process_refund(bill_id, config, now):
    """逐窗追溯原付款；每次只访问一页并持久保存进度。"""
    with transaction.atomic():
        _assert_identity(config)
        event = ExternalPayBill.objects.select_for_update().get(pk=bill_id, state__identity_revision=config.identity_revision)
        original = ExternalPayBill.objects.filter(
            state=event.state, bill_type=0, mch_id=event.mch_id, out_trade_no=event.out_trade_no,
        ).order_by("id")
        count = original.count()
        if count > 1:
            _issue(event, "同一商户单号对应多笔付款，需人工核对", now)
            return
        original = original.first()
        if event.search_end is None:
            if original:
                event.search_start = original.pay_time - timedelta(seconds=1)
                event.search_end = original.pay_time + timedelta(seconds=1)
            else:
                event.search_end = event.pay_time + timedelta(seconds=1)
                event.search_start = max(datetime(1970, 1, 1, tzinfo=dt_timezone.utc), event.search_end - WINDOW)
            event.search_cursor = ""
            event.search_seen = []
            event.search_candidate = None
            event.issue = "正在追溯原付款"
            event.save(update_fields=["search_start", "search_end", "search_cursor", "search_seen", "search_candidate", "issue"])
        start, end, cursor = event.search_start, event.search_end, event.search_cursor
    rows, following = _get_page(config, start, end, cursor)
    with transaction.atomic():
        _assert_identity(config)
        event = ExternalPayBill.objects.select_for_update().get(pk=bill_id)
        if (event.search_start, event.search_end, event.search_cursor) != (start, end, cursor):
            raise SyncError("退款追溯位置已变化")
        if following and (following == cursor or following in event.search_seen):
            raise SyncError("退款追溯分页游标循环")
        uncertain = False
        for row in rows:
            if isinstance(row, InvalidBill):
                _quarantine(event.state, row, start, end)
                merchant, number = row.payload.get("mch_id"), row.payload.get("out_trade_no")
                if not merchant or not number or (merchant == event.mch_id and number == event.out_trade_no):
                    uncertain = True
        if uncertain:
            _issue(event, "追溯窗口存在无法识别的账单，请检查同步告警", now)
            return
        found = [row for row in rows if not isinstance(row, InvalidBill) and row["bill_type"] == 0 and row["mch_id"] == event.mch_id and row["out_trade_no"] == event.out_trade_no]
        candidate = event.search_candidate
        for row in found:
            serial = {**row, "pay_time": row["pay_time"].isoformat()}
            if candidate is not None and candidate != serial:
                _issue(event, "同一商户单号对应多笔付款，需人工核对", now)
                return
            candidate = serial
        if original and candidate and original.transaction_id != candidate["transaction_id"]:
            _issue(event, "同一商户单号对应多笔付款，需人工核对", now)
            return
        event.search_candidate = candidate
        if following:
            event.search_seen = [*event.search_seen, following]
            event.search_cursor = following
            event.next_retry_at = now
        elif candidate:
            candidate["pay_time"] = datetime.fromisoformat(candidate["pay_time"])
            payment = _ingest(event.state, candidate, start, end)
            if payment is None or payment.conflicted:
                _issue(event, "企微账单身份或金额发生冲突，请人工核对", now)
                return
            payment.refund_origin = True
            payment.next_retry_at = now
            payment.save(update_fields=["refund_origin", "next_retry_at"])
            event.search_start = event.search_end = None
            event.search_cursor = ""
            event.search_seen = []
            event.search_candidate = None
            event.issue = "已找到原付款，等待订单退款同步"
            event.next_retry_at = now + timedelta(days=1)
        elif original:
            event.search_start = event.search_end = None
            event.search_cursor = ""
            event.search_seen = []
            event.search_candidate = None
            event.issue = "企微未返回原付款的最新退款信息"
            event.next_retry_at = now + RETRY
        elif start.year <= 1970:
            event.issue = "未找到原付款，请人工核对"
            event.next_retry_at = None
        else:
            event.search_end = start
            event.search_start = max(datetime(1970, 1, 1, tzinfo=dt_timezone.utc), start - WINDOW)
            event.search_cursor = ""
            event.search_seen = []
            event.search_candidate = None
            event.next_retry_at = now
        event.save()


def _error_text(exc):
    cause = getattr(exc, "__cause__", None)
    if getattr(cause, "sqlstate", None) == "55P03" or getattr(cause, "pgcode", None) == "55P03":
        return "订单正在处理，稍后自动重试"
    code = getattr(exc, "errcode", None)
    if isinstance(code, int):
        if code == 48002:
            return "企微接口返回 48002，当前应用无法查询对外收款，请检查应用权限及对外收款配置"
        return f"企微接口错误 {code}，请检查权限和配置"
    if isinstance(exc, SyncError):
        return str(exc)[:300]
    return "企微接口访问失败，请检查网络或配置"


def enqueue_order_refund_recheck(order):
    """退款修改事务内调用；不触碰账单行以保持订单锁顺序。"""
    config = WecomAppConfig.objects.first()
    if config and ExternalPayBill.objects.filter(
        state__identity_revision=config.identity_revision, state__corp_id=config.corp_id,
        bill_type=0, order=order,
    ).exists():
        ExternalPayRecheck.objects.create(order=order)


def _process_rechecks(state, now, config=None):
    config = config or WecomAppConfig.objects.first()
    for pk in ExternalPayRecheck.objects.filter(pending=True).order_by("pk").values_list("pk", flat=True)[:5000]:
        with transaction.atomic():
            _assert_identity(config)
            event = ExternalPayRecheck.objects.select_for_update().get(pk=pk)
            if not event.pending:
                continue
            ExternalPayBill.objects.filter(state=state, bill_type=0, order_id=event.order_id).update(next_retry_at=now)
            event.pending = False
            event.save(update_fields=["pending"])


def _retry_invalid(config, state, now, *, page_budget=500, time_budget=60):
    due = ExternalPayInvalidBill.objects.filter(state=state, resolved=False).filter(
        Q(last_retry_at__isnull=True) | Q(last_retry_at__lte=now - RETRY)
    ).order_by(F("last_retry_at").asc(nulls_first=True), "pk").values_list("pk", flat=True)[:50]
    queue = deque(due)
    deadline = time.monotonic() + time_budget
    while queue and page_budget > 0 and time.monotonic() < deadline:
        if not WecomAppConfig.objects.filter(pk=config.pk, external_pay_enabled=True, identity_revision=config.identity_revision).exists():
            raise SyncPaused()
        pk = queue.popleft()
        page_budget -= 1
        row = ExternalPayInvalidBill.objects.get(pk=pk)
        start, end, cursor = row.search_start, row.search_end, row.search_cursor
        key_names = ("bill_type", "transaction_id", "mch_id", "out_trade_no")
        identity = tuple(row.payload.get(k) for k in key_names)
        if start is None or end is None or type(identity[0]) is not int or identity[0] not in (0, 1) or not all(isinstance(v, str) and v for v in identity[1:]):
            with transaction.atomic():
                _assert_identity(config)
                ExternalPayInvalidBill.objects.filter(pk=pk).update(last_retry_at=now)
            continue
        try:
            rows, following = _get_page(config, start, end, cursor)
            time.sleep(0.2)
            recovered_payment_id = None
            with transaction.atomic():
                _assert_identity(config)
                row = ExternalPayInvalidBill.objects.select_for_update().get(pk=pk)
                if row.resolved or (row.search_start, row.search_end, row.search_cursor) != (start, end, cursor):
                    continue
                if following and (following == cursor or following in row.search_seen):
                    raise SyncError("异常账单重查游标循环")
                for item in rows:
                    if not isinstance(item, InvalidBill) and tuple(item.get(k) for k in key_names) == identity:
                        payment = _ingest(state, item, start, end)
                        if payment is not None and payment.bill_type == 0:
                            recovered_payment_id = payment.pk
                row.refresh_from_db()
                row.last_retry_at = now - RETRY if following and not row.resolved else now
                row.search_cursor = following
                row.search_seen = [*row.search_seen, following] if following else []
                row.save(update_fields=["last_retry_at", "search_cursor", "search_seen"])
                if following and not row.resolved:
                    queue.append(pk)
            if recovered_payment_id is not None:
                try:
                    match_payment(recovered_payment_id, config, timezone.now())
                except SyncPaused:
                    raise
                except Exception as exc:
                    with transaction.atomic():
                        _assert_identity(config)
                        ExternalPayBill.objects.filter(pk=recovered_payment_id, state=state).update(issue=_error_text(exc), next_retry_at=timezone.now() + RETRY)
        except SyncPaused:
            raise
        except Exception:
            with transaction.atomic():
                _assert_identity(config)
                ExternalPayInvalidBill.objects.filter(pk=pk).update(last_retry_at=now)


def run_due(now=None):
    now = now or timezone.now()
    config = WecomAppConfig.objects.first()
    if not config or not config.is_ready() or not config.external_pay_enabled:
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, 0])
        locked = cursor.fetchone()[0]
    if not locked:
        return
    try:
        state, _ = ExternalPayState.objects.get_or_create(identity_revision=config.identity_revision, defaults={"corp_id": config.corp_id})
        if state.corp_id != config.corp_id:
            raise SyncError("企微企业身份发生冲突")
        state.last_run_at = now
        state.save(update_fields=["last_run_at"])
        pull_error = None
        try:
            for _ in range(100):
                if not WecomAppConfig.objects.filter(pk=config.pk, external_pay_enabled=True, identity_revision=config.identity_revision).exists():
                    raise SyncPaused()
                state.refresh_from_db()
                if not prepare_window(config, state, now):
                    break
                state.refresh_from_db()
                pull_one_page(config, state)
                time.sleep(0.2)
        except SyncPaused:
            return
        except Exception as exc:
            pull_error = _error_text(exc)
            with transaction.atomic():
                try:
                    _assert_identity(config)
                except SyncPaused:
                    return
                ExternalPayState.objects.filter(pk=state.pk).update(last_error=pull_error)
        try:
            _process_rechecks(state, now, config)
            idle = 0
            for index in range(5000):
                now = timezone.now()
                due_qs = ExternalPayBill.objects.filter(state=state, next_retry_at__lte=now)
                if index % 3 == 0:
                    due_qs = due_qs.filter(bill_type=0)
                elif index % 3 == 1:
                    due_qs = due_qs.filter(bill_type=1).exclude(issue="")
                else:
                    due_qs = due_qs.filter(bill_type=1, issue="")
                due = due_qs.order_by("next_retry_at", "pk").values_list("pk", "bill_type").first()
                if due is None:
                    idle += 1
                    if idle == 3:
                        break
                    continue
                idle = 0
                bill_id, bill_type = due
                try:
                    if bill_type == 0:
                        match_payment(bill_id, config, now)
                    else:
                        process_refund(bill_id, config, now)
                        time.sleep(0.2)
                except SyncPaused:
                    raise
                except Exception as exc:
                    with transaction.atomic():
                        _assert_identity(config)
                        ExternalPayBill.objects.filter(pk=bill_id, state=state).update(issue=_error_text(exc), next_retry_at=now + RETRY)
            _retry_invalid(config, state, timezone.now())
        except SyncPaused:
            return
        except Exception as exc:
            ExternalPayState.objects.filter(pk=state.pk).update(last_error=_error_text(exc))
            raise SyncError(_error_text(exc)) from None
        if pull_error:
            raise SyncError(pull_error)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, 0])
