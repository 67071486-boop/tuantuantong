import hashlib
import logging
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name

from apps.accounts.models import WxPusherBinding, WxPusherHallPublication, WxPusherHallSubscription, WxPusherNotice
from apps.accounts.wxpusher_config import load_wxpusher_config
from apps.accounts.wxpusher_notice_templates import HALL_NEW_ORDER, safe_notice_order_text
from apps.accounts.wxpusher_wakeup import notify_hall_wakeup
from apps.business.models import JidanpuUpstreamHallOrder, PeerHallMirror
from apps.business.order_flags import users_with_hidden_offer_price
from apps.business.roles import WORKER

logger = logging.getLogger(__name__)


def _subscribers(game_id, listed_at):
    return WxPusherHallSubscription.objects.filter(
        game_id=game_id, game__deleted_at__isnull=True, game__is_active=True, selected_at__lte=listed_at,
        preference__hall_enabled_since__lte=listed_at, preference__user__is_active=True,
        preference__user__groups__name=WORKER,
    )


def queue_hall_publication(source, row, *, listed_at=None):
    if connection.schema_name == get_public_schema_name() or not row.game_id:
        return
    try:
        with transaction.atomic():
            _queue_hall_publication(source, row, listed_at or timezone.now())
    except DatabaseError as exc:
        logger.warning('抢单订阅登记失败 source=%s error=%s', source, type(exc).__name__)


def _queue_hall_publication(source, row, listed_at):
    if not _subscribers(row.game_id, listed_at).exists():
        return
    config = load_wxpusher_config()
    if not config.configured or not config.push_enabled:
        return
    if source == 'local':
        if row.hall_card_id or row.upstream_hall_card_id or row.hall_exit_snapshots.exists():
            return
        if (JidanpuUpstreamHallOrder.objects.filter(promoted_order=row).exists()
                or PeerHallMirror.objects.filter(promoted_order=row).exists()):
            return
        ref, identity_parts = row.order_no, (row.order_no,)
    elif source == 'upstream':
        ref = str(row.pk)
        identity_parts = (row.upstream_base_url, row.upstream_order_id)
    else:
        ref = str(row.pk)
        identity_parts = (row.peer_base_url, row.peer_order_no)
    digest = hashlib.sha256('\0'.join(identity_parts).encode()).hexdigest()
    _, created = WxPusherHallPublication.objects.get_or_create(
        identity=f'{source}:{digest}',
        defaults={
            'source': source, 'ref': ref, 'game_id': row.game_id, 'listed_at': listed_at, 'app_id': config.app_id,
        },
    )
    if created:
        transaction.on_commit(notify_hall_wakeup)


def _rules_match(rule, amount, project):
    if amount is None:
        if rule.price_min is not None or rule.price_max is not None:
            return False
    elif (rule.price_min is not None and amount < rule.price_min
            or rule.price_max is not None and amount > rule.price_max):
        return False
    return (not rule.include_keywords or any(word.casefold() in project for word in rule.include_keywords)) and (
        not any(word.casefold() in project for word in rule.exclude_keywords)
    )


def _notice_details(card, amount):
    raw_project = (card.get('project_text') or '').strip()
    project = safe_notice_order_text(
        raw_project, sensitive_fallback='', limit=len(raw_project), recover_public_tasks=True,
    )
    summary = project or ('请在接单大厅查看' if raw_project else '')
    if len(summary) > 300:
        summary = summary[:299] + '…'
    return {
        'display_order_no': (card.get('source_order_no') or card.get('remote_order_view')
                             or card.get('order_no') or card.get('remote_order_id') or '')[:128],
        'game_name': card.get('game_name') or '', 'server_name': card.get('server_name') or '',
        'amount': f'{amount:.2f}',
        'project': summary,
    }, project.casefold()


def _create_publication_notices(publication, config):
    from apps.business.api.hall_claim_target import hall_notice_card

    if publication.app_id != config.app_id:
        return
    rules = list(_subscribers(publication.game_id, publication.listed_at))
    if not rules:
        return
    card = hall_notice_card(publication.source, publication.ref, publication.game_id)
    if card is None:
        return
    amount = Decimal(card['hall_price'])
    public_details, project = _notice_details(card, amount)
    details = {
        'source': publication.source, 'ref': publication.ref, 'listed_at': publication.listed_at.isoformat(),
        'game_id': publication.game_id, **public_details,
    }
    details_without_price = {**details, 'amount': ''}
    user_ids = [rule.preference_id for rule in rules]
    price_hidden = users_with_hidden_offer_price(user_ids)
    bindings = {
        binding.user_id: binding for binding in WxPusherBinding.objects.filter(
            Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False), app_id=config.app_id, user_id__in=user_ids,
        ) if binding.effective_uid
    }
    notices = []
    for rule in rules:
        hide_price = rule.preference_id in price_hidden
        if not _rules_match(rule, None if hide_price else amount, project):
            continue
        binding = bindings.get(rule.preference_id)
        reason = WxPusherNotice.SkipReason
        skip_reason = ''
        if not config.push_enabled:
            skip_reason = reason.WORKSPACE_DISABLED
        elif binding is None:
            skip_reason = reason.NOT_BOUND
        elif binding.last_start_at is not None and binding.last_start_at > publication.listed_at:
            skip_reason = reason.BINDING_CHANGED
        notices.append(WxPusherNotice(
            user_id=rule.preference_id, kind=HALL_NEW_ORDER, reference=publication.identity,
            binding=binding, binding_started_at=binding.last_start_at if binding else None,
            order_no=publication.ref if publication.source == 'local' else '',
            details=details_without_price if hide_price else details,
            skip_reason=skip_reason,
            status=WxPusherNotice.Status.SKIPPED if skip_reason else WxPusherNotice.Status.PENDING,
        ))
    WxPusherNotice.objects.bulk_create(notices, ignore_conflicts=True, batch_size=100)


def process_hall_publications(config, *, limit, should_stop):
    for _ in range(min(limit, 10)):
        if should_stop is not None and should_stop():
            return
        with transaction.atomic(durable=True):
            publication = _due_publications().select_for_update(skip_locked=True).order_by('id').first()
            if publication is None:
                return
            try:
                with transaction.atomic():
                    _create_publication_notices(publication, config)
            except Exception as exc:
                publication.retry_at = timezone.now() + timedelta(minutes=1)
                publication.save(update_fields=['retry_at'])
                logger.warning('抢单通知展开失败 id=%s error=%s', publication.pk, type(exc).__name__)
            else:
                publication.processed_at = timezone.now()
                publication.save(update_fields=['processed_at'])


def _due_publications():
    return WxPusherHallPublication.objects.filter(
        processed_at__isnull=True,
    ).filter(Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()))


def has_hall_work():
    return _due_publications().exists() or WxPusherNotice.objects.filter(
        kind=HALL_NEW_ORDER, status=WxPusherNotice.Status.PENDING,
    ).exists()


def recheck_hall_notices(notices):
    from apps.business.api.hall_claim_target import hall_notice_card

    reason = WxPusherNotice.SkipReason
    batches = defaultdict(list)
    for notice in notices:
        details = notice.details
        key = details['source'], details['ref'], details['game_id'], details['listed_at']
        batches[key].append(notice)

    skipped = {}
    price_hidden = users_with_hidden_offer_price([
        notice.user_id for recipients in batches.values() for notice in recipients
    ])
    for (source, ref, game_id, listed_at), recipients in batches.items():
        rules = {
            rule.preference_id: rule
            for rule in _subscribers(game_id, parse_datetime(listed_at)).filter(
                preference_id__in=[notice.user_id for notice in recipients],
            )
        }
        card = hall_notice_card(source, ref, game_id)
        if card is not None:
            amount = Decimal(card["hall_price"])
            details, project = _notice_details(card, amount)
            details_without_price = {**details, 'amount': ''}
        for notice in recipients:
            hide_price = notice.user_id in price_hidden
            if hide_price:
                notice.details = {**notice.details, 'amount': ''}
            rule = rules.get(notice.user_id)
            if rule is None:
                skipped[notice.pk] = reason.PERSONAL_DISABLED
                continue
            if card is None:
                skipped[notice.pk] = reason.HALL_UNAVAILABLE
                continue
            if not _rules_match(rule, None if hide_price else amount, project):
                skipped[notice.pk] = reason.HALL_RULE_CHANGED
                continue
            notice.details = {**notice.details, **(details_without_price if hide_price else details)}
    return skipped
