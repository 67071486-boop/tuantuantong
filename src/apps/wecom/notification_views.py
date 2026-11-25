"""企微功能开关和通知记录。"""
from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from apps.business.game_scope import game_scope
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import Order
from apps.business.order_flags import scope_by_creator
from apps.core.envelope import api_response, paginated
from apps.core.permissions import is_tenant_admin
from apps.core.scoping import scope_queryset
from apps.wecom.models import ExternalPayBill, WecomAppConfig, WecomNotification
from apps.wecom.views import MessagesPageSerializer, WecomSettingsGateView, ensure_config, get_config

FIELDS = ('group_sync_enabled', 'group_qr_enabled', 'external_pay_enabled', 'payment_mismatch_enabled', 'order_status_enabled')
DEFAULTS = dict(zip(FIELDS, (True, True, True, False, False)))
LABELS = dict(zip(FIELDS, ('群同步', '群码', '收款同步', '金额异常通知', '订单状态通知')))


class FeatureSerializer(serializers.Serializer):
    group_sync_enabled = serializers.BooleanField(required=False)
    group_qr_enabled = serializers.BooleanField(required=False)
    external_pay_enabled = serializers.BooleanField(required=False)
    payment_mismatch_enabled = serializers.BooleanField(required=False)
    order_status_enabled = serializers.BooleanField(required=False)


class WecomFeatureSettingsView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = get_config()
        return api_response({key: getattr(config, key) if config else DEFAULTS[key] for key in FIELDS})

    def put(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = FeatureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = ensure_config()
        with transaction.atomic():
            from apps.wecom.notifications import lock_send_gate
            lock_send_gate()
            config = WecomAppConfig.objects.select_for_update().get(pk=config.pk)
            changed = [key for key, value in serializer.validated_data.items() if getattr(config, key) != value]
            fields = list(serializer.validated_data)
            if serializer.validated_data.get('order_status_enabled') is True and not config.order_status_enabled:
                config.order_status_enabled_at = timezone.now()
                fields.append('order_status_enabled_at')
            if serializer.validated_data.get('payment_mismatch_enabled') is True and not config.payment_mismatch_enabled:
                config.payment_mismatch_enabled_at = timezone.now()
                fields.append('payment_mismatch_enabled_at')
            for key, value in serializer.validated_data.items():
                setattr(config, key, value)
            if fields:
                config.save(update_fields=fields)
            if changed:
                reason = '，'.join(f"{LABELS[key]}{'开启' if getattr(config, key) else '关闭'}" for key in changed)
                record(log_type=ACTION_LOG_TYPES['system.wecom_config_update'], action='system.wecom_config_update', actor=request.user.get_username(), actor_id=request.user.pk, target_kind='wecom_app_config', target_id=str(config.pk), field_name='企微功能开关', reason=reason)
        return api_response({key: getattr(config, key) for key in FIELDS})


class WecomNotificationRecordsView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = MessagesPageSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        page = serializer.validated_data['page']
        config = get_config()
        qs = WecomNotification.objects.none() if config is None else WecomNotification.objects.filter(corp_id=config.corp_id, agent_id=config.agent_id)
        if not is_tenant_admin(request.user):
            visible = scope_by_creator(request.user, scope_queryset(request.user, Order.objects.all(), scope_filter=game_scope('game_id')))
            qs = qs.filter(order_no__in=visible.values('order_no'))
        qs = qs.order_by('-id')
        total = qs.count()
        items = [{
            'id': row.pk, 'kind': row.kind, 'order_no': row.order_no, 'detail': row.detail,
            'status': row.status, 'reason': row.reason, 'created_at': row.created_at,
            'recipients': [{'userid': k, 'status': v} for k, v in row.recipients.items()],
        } for row in qs[(page-1)*20:page*20]]
        return api_response(paginated(items, total=total, page=page, page_size=20))
