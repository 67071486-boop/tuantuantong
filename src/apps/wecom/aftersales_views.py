"""Tenant-scoped settings and selectors for after-sales notifications."""

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.envelope import api_response
from apps.wecom import aftersales
from apps.wecom.models import WecomAftersalesSettings, WecomAppConfig
from apps.wecom.views import WecomSettingsGateView, get_config


_OPTION_PAGE_SIZE = 30


class AftersalesSettingsSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    keywords = serializers.ListField(child=serializers.CharField(max_length=32), max_length=aftersales.MAX_KEYWORDS)
    group_ids = serializers.ListField(child=serializers.CharField(max_length=256), max_length=aftersales.MAX_GROUPS)
    order_recipient = serializers.ChoiceField(choices=WecomAftersalesSettings.OrderRecipient.values)
    fixed_recipient_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=2147483647),
        max_length=aftersales.MAX_FIXED_RECIPIENTS,
    )

    def validate(self, attrs):
        for key in ("keywords", "group_ids", "fixed_recipient_ids"):
            attrs[key] = list(dict.fromkeys(attrs[key]))
        if attrs["enabled"] and not attrs["keywords"]:
            raise serializers.ValidationError("启用时请至少填写一个关键词")
        return attrs


class AftersalesOptionsSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("group", "user"))
    q = serializers.CharField(allow_blank=True, default="", max_length=120)
    page = serializers.IntegerField(default=1, min_value=1, max_value=100000)


def _group_option(group):
    return {"id": group.chat_id, "name": group.name or group.chat_id, "detail": group.chat_id}


def _user_option(user):
    return {"id": user.pk, "name": user_display_name(user), "detail": user.get_username()}


def _payload(row, config):
    available = aftersales.available(config)
    legacy = aftersales.legacy_policy() if available and row is None else None
    if row is None:
        data = {
            "enabled": bool(legacy), "keywords": list(aftersales.DEFAULT_KEYWORDS), "group_ids": [],
            "order_recipient": WecomAftersalesSettings.OrderRecipient.SALES, "fixed_recipient_ids": [],
            "status": WecomAftersalesSettings.Status.SYNCED if legacy else WecomAftersalesSettings.Status.DISABLED,
        }
    else:
        data = {key: getattr(row, key) for key in (
            "enabled", "keywords", "group_ids", "order_recipient", "fixed_recipient_ids",
        )}
        data["status"] = aftersales.policy_status(row, config)
    groups = {
        group.chat_id: _group_option(group)
        for group in aftersales.eligible_groups(config).filter(chat_id__in=data["group_ids"]).only("chat_id", "name")
    } if available else {}
    users = {
        user.pk: _user_option(user)
        for user in aftersales.active_users().filter(pk__in=data["fixed_recipient_ids"])
    } if available else {}
    return {
        **data, "available": available, "legacy_pilot": bool(legacy), "default_keywords": list(aftersales.DEFAULT_KEYWORDS),
        "group_options": [groups.get(key, {"id": key, "name": key}) for key in data["group_ids"]],
        "fixed_recipient_options": [
            users.get(key, {"id": key, "name": str(key)}) for key in data["fixed_recipient_ids"]
        ],
    }


def _validate_selection(row, config, values):
    group_ids, user_ids = values["group_ids"], values["fixed_recipient_ids"]
    if not values["enabled"]:
        # Keep existing stale selections editable when switching the rule off.
        group_ids = set(group_ids) - set(row.group_ids)
        user_ids = set(user_ids) - set(row.fixed_recipient_ids)
    if aftersales.eligible_groups(config).filter(chat_id__in=group_ids).count() != len(group_ids):
        raise serializers.ValidationError({"group_ids": "部分群已失效，请重新选择"})
    if aftersales.active_users().filter(pk__in=user_ids).count() != len(user_ids):
        raise serializers.ValidationError({"fixed_recipient_ids": "部分通知账号已停用，请重新选择"})


class WecomAftersalesSettingsView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        config = get_config()
        row = WecomAftersalesSettings.objects.filter(config=config).first() if config else None
        return api_response(_payload(row, config))

    @transaction.atomic
    def put(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = AftersalesSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = WecomAppConfig.objects.select_for_update().first()
        if not aftersales.available(config):
            raise serializers.ValidationError("当前工作室尚未启用企微售后纠纷通知")
        row = WecomAftersalesSettings.objects.filter(config=config).first()
        if row is None:
            row = WecomAftersalesSettings(config=config)
        _validate_selection(row, config, serializer.validated_data)
        aftersales.set_settings(row, config, serializer.validated_data)
        record(
            log_type=ACTION_LOG_TYPES["system.wecom_config_update"], action="system.wecom_config_update",
            actor=request.user.get_username(), actor_id=request.user.pk, target_kind="wecom_app_config",
            target_id=str(config.pk), field_name="售后纠纷通知",
            reason="保存启用设置" if row.enabled else "保存关闭设置",
        )
        return api_response(_payload(row, config))


class WecomAftersalesOptionsView(WecomSettingsGateView):
    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        serializer = AftersalesOptionsSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        config = get_config()
        if not aftersales.available(config):
            return api_response({"items": [], "has_more": False})
        kind, query, page = (serializer.validated_data[key] for key in ("kind", "q", "page"))
        if kind == "group":
            queryset = aftersales.eligible_groups(config).order_by("name", "chat_id").only("chat_id", "name")
            if query:
                queryset = queryset.filter(Q(name__icontains=query) | Q(chat_id__icontains=query))
            option = _group_option
        else:
            queryset = aftersales.active_users().only("id", "username", "first_name").order_by("first_name", "username", "id")
            if query:
                queryset = queryset.filter(Q(first_name__icontains=query) | Q(username__icontains=query))
            option = _user_option
        start = (page - 1) * _OPTION_PAGE_SIZE
        rows = list(queryset[start:start + _OPTION_PAGE_SIZE + 1])
        return api_response({"items": [option(row) for row in rows[:_OPTION_PAGE_SIZE]], "has_more": len(rows) > _OPTION_PAGE_SIZE})
