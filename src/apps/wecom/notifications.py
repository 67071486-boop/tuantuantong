"""企微通知出箱：先落库，独立调度发送。"""
import logging
from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from wechatpy.exceptions import WeChatClientException

from apps.wecom import sdk
from apps.wecom.models import ExternalPayBill, WecomAppConfig, WecomNotification

logger = logging.getLogger(__name__)


def lock_send_gate():
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [0x57454e55, 0])


def prepare_order_status(order, previous_status, *, event_at=None, sales_changed=False):
    """Attach the notice to the same order write that changes its status."""
    if order.status not in ('ISSUE', 'PAUSED') or order.status == previous_status:
        return False
    label = '问题单' if order.status == 'ISSUE' else '已暂停'
    event = {'event_key': uuid4().hex, 'kind': 'order_status', 'order_no': order.order_no,
             'lookup_name': (order.sales_agent or '').strip(),
             'detail': f'订单 {order.order_no} 状态变为{label}，请及时处理。',
             'observed_at': timezone.now().isoformat(), 'config_unknown': True}
    try:
        with transaction.atomic():
            config = WecomAppConfig.objects.first()
            if not config or not config.enabled or not config.order_status_enabled:
                return False
            if event_at is not None and config.order_status_enabled_at is not None and event_at < config.order_status_enabled_at:
                return False
            event.update(identity_revision=config.identity_revision, corp_id=config.corp_id, agent_id=config.agent_id,
                         enabled_at=config.order_status_enabled_at.isoformat() if config.order_status_enabled_at else None,
                         config_unknown=False)
    except Exception:
        logger.exception('读取企微配置失败，保留订单通知待人工处理')
    if order.pk:
        from apps.business.models import Order
        locked = Order.all_objects.select_for_update().only('wecom_notice_journal', 'sales_agent').get(pk=order.pk)
        journal = locked.wecom_notice_journal
        if not sales_changed:
            event['lookup_name'] = (locked.sales_agent or '').strip()
    else:
        journal = order.wecom_notice_journal
    order.wecom_notice_journal = [*journal, event]
    order.wecom_notice_pending = True
    return True


def queue_payment_mismatch(bill):
    try:
        with transaction.atomic():
            config = WecomAppConfig.objects.first()
            if not config:
                return not bill.mismatch_notification_pending
            if bill.mismatch_notification_pending:
                changed = (not config.enabled or not config.payment_mismatch_enabled or
                           bill.mismatch_notice_corp_id != config.corp_id or
                           bill.mismatch_notice_agent_id != config.agent_id or
                           bill.mismatch_notice_enabled_at != config.payment_mismatch_enabled_at)
                if changed:
                    WecomNotification.objects.create(
                        identity_revision=config.identity_revision, corp_id=config.corp_id, agent_id=config.agent_id,
                        enabled_at=config.payment_mismatch_enabled_at, kind='payment_mismatch',
                        order_no='', detail='企微配置发生变更，原收款异常通知未发送', status='failed',
                        reason='通知开关或企微应用已变更，请人工核对', resolved=True,
                    )
                    return True
            elif not config.enabled or not config.payment_mismatch_enabled:
                return True
            else:
                bill.mismatch_notice_corp_id = config.corp_id
                bill.mismatch_notice_agent_id = config.agent_id
                bill.mismatch_notice_enabled_at = config.payment_mismatch_enabled_at
            from apps.business.models import Order
            orders = list(Order.objects.filter(source_order_no=bill.transaction_id).values_list('order_no', 'total_price')[:5])
            order_text = '、'.join(f'{number}（{price}元）' for number, price in orders)
            WecomNotification.objects.create(
                identity_revision=config.identity_revision, corp_id=config.corp_id, agent_id=config.agent_id, enabled_at=config.payment_mismatch_enabled_at, kind='payment_mismatch',
                order_no=orders[0][0] if len(orders) == 1 else '',
                bill=bill,
                detail=f'企微收款 {bill.transaction_id} 金额 {Decimal(bill.total_fee) / 100:.2f} 元，与订单金额不一致。订单：{order_text}。',
                recipients={bill.payee_userid: 'pending'} if bill.payee_userid else {},
                resolved=bool(bill.payee_userid), status='pending',
                reason=('等待查询企微收款人' if not bill.payee_userid else ('' if config.is_ready() else '企微应用配置不完整，等待恢复')),
            )
        return True
    except Exception:
        logger.exception('企微收款异常通知入队失败')
        return False


def retry_expired_payment_mismatch(bill):
    from apps.business.models import Order
    config = WecomAppConfig.objects.first()
    if bill.mismatch_notification_pending and config and (
        not config.enabled or not config.payment_mismatch_enabled or
        bill.mismatch_notice_corp_id != config.corp_id or bill.mismatch_notice_agent_id != config.agent_id or
        bill.mismatch_notice_enabled_at != config.payment_mismatch_enabled_at
    ):
        return queue_payment_mismatch(bill)
    candidates = Order.objects.filter(source_order_no=bill.transaction_id)
    if not candidates.exists() or candidates.filter(total_price=Decimal(bill.total_fee) / 100).exists():
        return True
    return queue_payment_mismatch(bill)


def _fail(row, reason):
    row.status = 'failed'
    row.reason = reason
    row.save(update_fields=['status', 'reason'])


def _retry_lookup(row, reason='企微通讯录读取失败'):
    row.attempts += 1
    row.status = 'failed' if row.attempts >= 5 else 'pending'
    row.reason = reason + ('，请人工核对' if row.status == 'failed' else '，稍后重试')
    row.retry_at = timezone.now() + timedelta(minutes=5) if row.status == 'pending' else None
    row.save(update_fields=['attempts', 'status', 'reason', 'retry_at'])


def _rejected_recipient(row, userid, code):
    if code in (40001, 40014, 42001, 45009):
        attempts = row.recipient_attempts.get(userid, 0) + 1
        row.recipient_attempts[userid] = attempts
        row.recipients[userid] = 'pending' if attempts < 5 else 'failed'
        if row.recipients[userid] == 'pending':
            row.retry_at = timezone.now() + timedelta(minutes=5)
        row.save(update_fields=['recipient_attempts', 'recipients', 'retry_at'])
    else:
        row.recipients[userid] = 'failed'


def _recover_order_journal(limit=100):
    from apps.business.models import Order
    from django.utils.dateparse import parse_datetime
    config = WecomAppConfig.objects.first()
    if not config:
        return 0
    ids = list(Order.all_objects.filter(wecom_notice_pending=True).order_by('id').values_list('id', flat=True)[:limit])
    recovered = 0
    for pk in ids:
        try:
            with transaction.atomic():
                order = Order.all_objects.select_for_update(skip_locked=True).filter(pk=pk, wecom_notice_pending=True).first()
                if order is None:
                    continue
                remaining = []
                for event in order.wecom_notice_journal:
                    same_app = event.get('corp_id') == config.corp_id and event.get('agent_id') == config.agent_id
                    enabled_at = parse_datetime(event['enabled_at']) if event.get('enabled_at') else None
                    invalidated = not event.get('config_unknown') and (not config.enabled or not config.order_status_enabled or not same_app or enabled_at != config.order_status_enabled_at)
                    defaults = {'identity_revision': event.get('identity_revision', config.identity_revision),
                                'corp_id': event.get('corp_id', config.corp_id), 'agent_id': event.get('agent_id', config.agent_id),
                                'enabled_at': enabled_at, 'kind': 'order_status', 'order_no': event['order_no'],
                                'lookup_name': event['lookup_name'], 'detail': event['detail']}
                    if event.get('config_unknown'):
                        defaults.update(status='failed', reason='事件发生时企微配置不可读取，请人工核对')
                    elif invalidated:
                        defaults.update(status='failed', reason='通知开关或企微应用已变更，原通知未发送')
                        if not same_app:
                            defaults.update(identity_revision=config.identity_revision, corp_id=config.corp_id,
                                            agent_id=config.agent_id, lookup_name='', detail='企微应用变更，订单状态通知未发送')
                    elif not config.is_ready():
                        defaults.update(reason='企微应用配置不完整，等待恢复')
                    WecomNotification.objects.get_or_create(event_key=event['event_key'], defaults=defaults)
                    recovered += 1
                order.wecom_notice_journal = remaining
                order.wecom_notice_pending = bool(remaining)
                order.save(update_fields=['wecom_notice_journal', 'wecom_notice_pending'])
        except Exception:
            logger.exception('企微订单通知补偿暂未完成')
    return recovered


def _recover_invalidated_payment_notices(limit=100):
    config = WecomAppConfig.objects.first()
    if not config:
        return 0
    pending = ExternalPayBill.objects.filter(mismatch_notification_pending=True)
    if config.enabled and config.payment_mismatch_enabled:
        pending = pending.exclude(mismatch_notice_corp_id=config.corp_id, mismatch_notice_agent_id=config.agent_id,
                                  mismatch_notice_enabled_at=config.payment_mismatch_enabled_at)
    ids = list(pending.order_by('id').values_list('id', flat=True)[:limit])
    count = 0
    for pk in ids:
        try:
            with transaction.atomic():
                current = WecomAppConfig.objects.select_for_update().get(pk=config.pk)
                bill = ExternalPayBill.objects.select_for_update(skip_locked=True).filter(pk=pk, mismatch_notification_pending=True).first()
                if bill is None:
                    continue
                valid = (current.enabled and current.payment_mismatch_enabled and
                         bill.mismatch_notice_corp_id == current.corp_id and bill.mismatch_notice_agent_id == current.agent_id and
                         bill.mismatch_notice_enabled_at == current.payment_mismatch_enabled_at)
                if valid:
                    continue
                WecomNotification.objects.create(
                    identity_revision=current.identity_revision, corp_id=current.corp_id, agent_id=current.agent_id,
                    enabled_at=current.payment_mismatch_enabled_at, kind='payment_mismatch', order_no='',
                    detail='企微配置发生变更，原收款异常通知未发送', status='failed',
                    reason='通知开关或企微应用已变更，请人工核对', resolved=True,
                )
                bill.mismatch_notification_pending = False
                bill.save(update_fields=['mismatch_notification_pending'])
                count += 1
        except Exception:
            logger.exception('企微收款通知失效记录暂未完成')
    return count


def _lookup_payee(row, config, pages=50):
    from apps.wecom.external_pay import InvalidBill, _get_page
    bill = row.bill
    if not bill:
        _fail(row, '账单不存在，请人工核对')
        return False
    if bill.payee_userid:
        row.recipients = {bill.payee_userid: 'pending'}
        row.resolved = True
        row.reason = ''
        row.save(update_fields=['recipients', 'resolved', 'reason'])
        return True
    for _ in range(pages):
        latest = WecomAppConfig.objects.first()
        if not latest or not latest.is_ready() or latest.identity_revision != config.identity_revision or not latest.payment_mismatch_enabled:
            _fail(row, '企微应用配置已变更或通知已关闭，请人工核对')
            return False
        try:
            rows, following = _get_page(config, bill.pay_time - timedelta(minutes=1), bill.pay_time + timedelta(minutes=1), row.lookup_cursor)
        except Exception:
            _retry_lookup(row, '企微收款人查询失败')
            return False
        if following and (following == row.lookup_cursor or following in row.lookup_seen):
            _fail(row, '收款人查询分页异常，请人工核对')
            return False
        for item in rows:
            if isinstance(item, InvalidBill):
                payload = item.payload
                if not payload.get('transaction_id') or payload.get('transaction_id') == bill.transaction_id:
                    _retry_lookup(row, '企微账单无法核验收款人')
                    return False
                continue
            if item['bill_type'] == 0 and item['transaction_id'] == bill.transaction_id and item['mch_id'] == bill.mch_id and item['out_trade_no'] == bill.out_trade_no:
                if item['total_fee'] != bill.total_fee or item['pay_time'] != bill.pay_time:
                    _fail(row, '企微账单身份或金额冲突，请人工核对')
                    return False
                row.lookup_found.append(item['payee_userid'])
        row.lookup_cursor = following
        if following:
            row.lookup_seen.append(following)
        row.save(update_fields=['lookup_cursor', 'lookup_seen', 'lookup_found'])
        if not following:
            if len(row.lookup_found) != 1 or not row.lookup_found[0]:
                row.lookup_seen = []
                row.lookup_found = []
                row.save(update_fields=['lookup_seen', 'lookup_found'])
                _retry_lookup(row, '企微未返回唯一可用收款人')
                return False
            with transaction.atomic():
                lock_send_gate()
                latest = WecomAppConfig.objects.first()
                if not latest or not latest.is_ready() or latest.identity_revision != config.identity_revision or not latest.payment_mismatch_enabled:
                    _fail(row, '企微应用配置已变更，请人工核对')
                    return False
                bill.payee_userid = row.lookup_found[0]
                bill.save(update_fields=['payee_userid'])
                row.recipients = {bill.payee_userid: 'pending'}
                row.resolved = True
                row.reason = ''
                row.save(update_fields=['recipients', 'resolved', 'reason'])
            return True
    row.reason = '正在查询企微收款人'
    row.save(update_fields=['reason'])
    return False


def run_due(limit=100):
    _recover_order_journal(limit)
    _recover_invalidated_payment_notices(limit)
    config = WecomAppConfig.objects.first()
    if not config or not config.enabled:
        return 0
    if not config.is_ready():
        WecomNotification.objects.filter(status='pending', corp_id=config.corp_id, agent_id=config.agent_id).update(reason='企微应用配置不完整，等待恢复')
        return 0
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [0x57454e54, 0])
        if not cursor.fetchone()[0]:
            return 0
    try:
        return _run_locked(config, limit)
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [0x57454e54, 0])


def _run_locked(config, limit):
    from apps.wecom.external_pay import TimedSession
    rows = WecomNotification.objects.filter(status='pending').filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now())).order_by('id')[:limit]
    count = 0
    for row in rows:
        current = WecomAppConfig.objects.first()
        if not current or not current.is_ready():
            continue
        if not row.corp_id or not row.agent_id or (current.corp_id, current.agent_id) != (row.corp_id, row.agent_id):
            _fail(row, '企微应用配置已变更，请人工核对')
            continue
        if (row.kind == 'order_status' and not current.order_status_enabled) or (row.kind == 'payment_mismatch' and not current.payment_mismatch_enabled):
            _fail(row, '功能已关闭，通知未发送')
            continue
        boundary = current.order_status_enabled_at if row.kind == 'order_status' else current.payment_mismatch_enabled_at
        if row.enabled_at != boundary:
            _fail(row, '通知开关已重新开启，旧通知未发送')
            continue
        client = sdk.get_client(current, timeout=(5, 20), auto_retry=False)
        client._http.close()
        client._http = TimedSession()
        try:
            if not row.resolved:
                if row.kind == 'payment_mismatch':
                    if not _lookup_payee(row, current):
                        continue
                else:
                    if not row.lookup_name:
                        _fail(row, '订单未填写销售客服')
                        continue
                    try:
                        users = client.user.list(1, fetch_child=True, simple=True)
                    except Exception:
                        _retry_lookup(row)
                        continue
                    if not isinstance(users, list):
                        _fail(row, '企微通讯录返回异常，请人工处理')
                        continue
                    ids = sorted({u.get('userid') for u in users if isinstance(u, dict) and isinstance(u.get('userid'), str) and u.get('userid') and isinstance(u.get('name'), str) and row.lookup_name in u['name']})
                    if not ids:
                        _fail(row, '未找到匹配的企微成员')
                        continue
                    row.recipients = dict.fromkeys(ids, 'pending')
                    row.resolved = True
                    row.save(update_fields=['recipients', 'resolved'])
            for userid, status in row.recipients.items():
                if status == 'sending':
                    row.recipients[userid] = 'unknown'
                    row.save(update_fields=['recipients'])
                    continue
                if status != 'pending':
                    continue
                row.recipients[userid] = 'sending'
                row.save(update_fields=['recipients'])
                with transaction.atomic():
                    lock_send_gate()
                    latest = WecomAppConfig.objects.first()
                    enabled = latest and latest.is_ready() and latest.identity_revision == current.identity_revision and latest.corp_id == current.corp_id and latest.agent_id == current.agent_id and ((row.kind == 'order_status' and latest.order_status_enabled and latest.order_status_enabled_at == row.enabled_at) or (row.kind == 'payment_mismatch' and latest.payment_mismatch_enabled and latest.payment_mismatch_enabled_at == row.enabled_at))
                    if not enabled:
                        row.recipients[userid] = 'failed'
                    else:
                        try:
                            result = client.message.send_text(current.agent_id, [userid], row.detail)
                        except WeChatClientException as exc:
                            _rejected_recipient(row, userid, exc.errcode)
                            if exc.errcode in (40001, 40014, 42001) and row.recipients[userid] == 'pending':
                                try:
                                    client.fetch_access_token()
                                except Exception:
                                    pass
                        except Exception:
                            row.recipients[userid] = 'unknown'
                        else:
                            invalid = result.get('invaliduser', '') if isinstance(result, dict) else ''
                            ok = isinstance(result, dict) and result.get('errcode') == 0 and isinstance(invalid, str) and userid not in invalid.split('|')
                            if not ok and isinstance(result, dict) and result.get('errcode') in (40001, 40014, 42001, 45009):
                                _rejected_recipient(row, userid, result['errcode'])
                                if result['errcode'] in (40001, 40014, 42001) and row.recipients[userid] == 'pending':
                                    try:
                                        client.fetch_access_token()
                                    except Exception:
                                        pass
                            else:
                                row.recipients[userid] = 'sent' if ok else 'failed'
                    row.save(update_fields=['recipients'])
            values = row.recipients.values()
            row.status = 'pending' if 'pending' in values else ('sent' if row.recipients and all(value == 'sent' for value in values) else 'failed')
            row.reason = '' if row.status == 'sent' else ('企微明确拒绝请求，稍后重试' if row.status == 'pending' else '部分或全部通知未确认送达，请人工核对')
            row.save(update_fields=['status', 'reason'])
            count += 1
        finally:
            client._http.close()
    return count
