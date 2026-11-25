"""Advance existing payroll batches and reconcile individual transfers."""
from contextlib import contextmanager
import hashlib
import json
import logging
import threading
import time

import httpx
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.business import online_payroll as domain
from apps.business.alipay_auth_crypto import AuthorizationDataUnavailable, cipher, decrypt
from apps.business.models import OnlinePayrollGatewayBatch, OnlinePayrollTransfer
from apps.business.online_payroll_gateway import PayrollCredentials, PayrollGateway, PayrollGatewayError


TERMINAL = ('succeeded', 'failed', 'closed', 'cancelled')
GATEWAY = 'https://pay.jeepay.vip'
ROUND_SECONDS = 50
logger = logging.getLogger(__name__)
_initial_slots = threading.BoundedSemaphore(2)


def schedule_initial_processing(batch_id):
    schema = connection.schema_name
    transaction.on_commit(lambda: _start_initial_processing(schema, batch_id), robust=True)


def _start_initial_processing(schema, batch_id):
    if not _initial_slots.acquire(blocking=False):
        return  # The periodic worker picks up the persisted batch.
    try:
        threading.Thread(
            target=_process_initial_batch, args=(schema, batch_id),
            name='online-payroll-initial', daemon=True,
        ).start()
    except BaseException:
        _initial_slots.release()
        raise


def _process_initial_batch(schema, batch_id):
    try:
        with schema_context(schema), httpx.Client(base_url=GATEWAY, timeout=8, follow_redirects=False, trust_env=False) as http:
            deadline = time.monotonic() + ROUND_SECONDS
            gateway_ids = OnlinePayrollGatewayBatch.objects.filter(batch_id=batch_id, status='staged').order_by('pk')
            for gateway_id in list(gateway_ids.values_list('pk', flat=True)):
                if time.monotonic() >= deadline:
                    break
                advance_gateway_batch(gateway_id, http, deadline=deadline, initial_only=True)
    except PayrollGatewayError:
        logger.warning('在线结算首次制单暂不可用，等待后台重试')
    except Exception:
        logger.exception('在线结算首次制单异常，等待后台重试')
    finally:
        try:
            connection.close()
        finally:
            _initial_slots.release()


def desktop_payment_url(batch_id):
    """Read the PC credential from an already sealed, still payable batch."""
    with _batch_lock(batch_id) as acquired:
        if not acquired:
            raise PayrollGatewayError('正在核对这笔付款，请稍后再获取电脑付款链接')
        child = OnlinePayrollGatewayBatch.objects.select_related('batch__authorization').get(pk=batch_id)
        if not domain.gateway_can_pay(child):
            raise PayrollGatewayError('该批次目前无法付款，请刷新打款结果')
        mobile_url = decrypt(child.payment_ciphertext, cipher())
        credentials = PayrollCredentials.for_authorization(child.batch.authorization.request_id)
        transfers = list(child.transfers.all())
        with httpx.Client(base_url=GATEWAY, timeout=8, follow_redirects=False, trust_env=False) as http:
            url = PayrollGateway(credentials, http).seal(child, transfers, desktop=True)
        if url == mobile_url:
            raise PayrollGatewayError('计全付未返回独立的电脑付款链接，请使用支付宝扫码')
        return url


@contextmanager
def _batch_lock(batch_id):
    material = f'online-payroll:{connection.schema_name}:{batch_id}'.encode()
    key = int.from_bytes(hashlib.sha256(material).digest()[:8], 'big', signed=True)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [key])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [key])


def _recipient(transfer):
    try:
        data = json.loads(decrypt(transfer.recipient_ciphertext, cipher()))
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ('account', 'name')):
            raise ValueError
        return data
    except (ValueError, AuthorizationDataUnavailable):
        raise PayrollGatewayError('收款资料暂不可用') from None


def _query(gateway, transfers, deadline):
    for transfer in transfers:
        if time.monotonic() >= deadline:
            break
        if transfer.status == 'staged' or transfer.status in ('failed', 'closed', 'cancelled'):
            continue
        if transfer.status == 'succeeded' and transfer.finalized_at is not None:
            continue
        try:
            result = gateway.query(transfer, _recipient(transfer))
        except PayrollGatewayError as exc:
            OnlinePayrollTransfer.objects.filter(pk=transfer.pk, finalized_at__isnull=True).update(
                last_error=str(exc)[:200], updated_at=timezone.now(),
            )
            continue
        domain.apply_transfer_result(
            transfer.pk, state=result.state, success_time=result.success_time,
            transfer_no=result.transfer_no, channel_order_no=result.channel_order_no, error=result.error,
        )


def _submit(gateway, transfers, deadline):
    for transfer in transfers:
        if time.monotonic() >= deadline:
            break
        if transfer.status != 'staged':
            continue
        try:
            recipient = _recipient(transfer)
        except PayrollGatewayError:
            break
        if not domain.begin_submission(transfer.pk):
            break
        try:
            gateway.submit(transfer, recipient)
        except PayrollGatewayError as exc:
            domain.record_submission(transfer.pk, created=False, error=str(exc))
            break
        domain.record_submission(transfer.pk, created=True)


def advance_gateway_batch(batch_id, http, *, deadline=float('inf'), initial_only=False):
    with _batch_lock(batch_id) as acquired:
        if not acquired:
            return False
        child = OnlinePayrollGatewayBatch.objects.select_related('batch__authorization').get(pk=batch_id)
        if child.status == 'closed' or (initial_only and child.status != 'staged'):
            return False
        credentials = PayrollCredentials.for_authorization(child.batch.authorization.request_id)
        gateway = PayrollGateway(credentials, http)
        transfers = list(child.transfers.select_related('gateway_batch').order_by('pk'))
        checks = transfers
        if child.status == 'staged' and not any(t.status in ('closed', 'failed') for t in transfers):
            checks = [t for t in transfers if t.status in ('submitting', 'unknown')]
        _query(gateway, sorted(checks, key=lambda t: (t.updated_at, t.pk)), deadline)
        transfers = list(child.transfers.select_related('gateway_batch').order_by('pk'))
        if child.status == 'staged' and all(t.status in ('staged', 'created') for t in transfers):
            _submit(gateway, transfers, deadline)
            transfers = list(child.transfers.select_related('gateway_batch').order_by('pk'))
        if time.monotonic() >= deadline:
            return True
        if child.status == 'staged' and transfers and all(t.status == 'created' for t in transfers):
            if not domain.begin_sealing(child.pk):
                return False
        elif child.status not in ('sealing', 'unknown') or not any(t.status in ('created', 'processing') for t in transfers):
            return True
        try:
            payment_url = gateway.seal(child, transfers)
        except PayrollGatewayError as exc:
            domain.record_sealed(child.pk, error=str(exc))
        else:
            domain.record_sealed(child.pk, payment_url=payment_url)
        return True


def run_polling_round(report_failure, *, limit=20):
    deadline = time.monotonic() + ROUND_SECONDS
    active = OnlinePayrollTransfer.objects.filter(
        Q(status='succeeded', finalized_at__isnull=True) | ~Q(status__in=TERMINAL),
    ).values('gateway_batch_id')
    batch_ids = list(OnlinePayrollGatewayBatch.objects.filter(pk__in=active).exclude(status='closed')
                     .order_by('updated_at', 'pk').values_list('pk', flat=True)[:limit])
    with httpx.Client(base_url=GATEWAY, timeout=8, follow_redirects=False, trust_env=False) as http:
        for batch_id in batch_ids:
            if time.monotonic() >= deadline:
                break
            try:
                advance_gateway_batch(batch_id, http, deadline=deadline)
            except PayrollGatewayError:
                report_failure(batch_id, '付款应用资料暂不可用')
            finally:
                # Keep older unresolved batches from starving newer batches.
                OnlinePayrollGatewayBatch.objects.filter(pk=batch_id).update(updated_at=timezone.now())
