from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.envelope import api_response
from apps.core.permissions import is_tenant_admin

from . import core_bridge
from .matrix_bridge import ChatServiceUnavailable
from .system_notice_settings import system_notice_settings, update_system_notice_settings
from .views import ChatView, _validated


class SystemNoticeSettingsUpdate(serializers.Serializer):
    approval_submitted = serializers.BooleanField(required=False)
    approval_approved = serializers.BooleanField(required=False)
    wecom_aftersales = serializers.BooleanField(required=False)
    order_grab_qr = serializers.BooleanField(required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict) and any(
            key not in self.fields or type(value) is not bool for key, value in data.items()
        ):
            raise serializers.ValidationError({"non_field_errors": ["系统通知规则或开关值不正确"]})
        return super().to_internal_value(data)


class SystemNoticeSettingsView(ChatView):
    def _available(self):
        try:
            return not core_bridge.enabled()
        except ChatServiceUnavailable:
            return False

    def get(self, request):
        if not is_tenant_admin(request.user):
            raise PermissionDenied()
        payload = system_notice_settings()
        if not self._available():
            payload["available"] = False
        return api_response(payload)

    @transaction.atomic
    def put(self, request):
        lock_tenant_admin_mutations()
        if not is_tenant_admin(request.user):
            raise PermissionDenied()
        if not self._available():
            raise ValidationError("当前聊天服务暂不支持发送系统通知")
        values = _validated(SystemNoticeSettingsUpdate, request.data)
        payload, changed = update_system_notice_settings(values)
        if changed:
            action = "system.internal_chat_notice_settings_update"
            record(
                log_type=ACTION_LOG_TYPES[action], action=action,
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="chat_notice_settings", target_id="1", field_name="、".join(changed),
            )
        return api_response(payload)
