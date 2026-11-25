"""Workspace administration of WxPusher delivery and credentials."""

import re

from django.db import transaction
from django.db.models import Q
from django.views.decorators.debug import sensitive_variables
from rest_framework import serializers
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.accounts.models import WxPusherBinding, WxPusherNotice
from apps.accounts.permissions import IsAccountAdmin
from apps.accounts.wxpusher_binding import BindingError, send_test_notice
from apps.accounts.wxpusher_config import (
    APP_ID_PATTERN, WxPusherConfigError, load_wxpusher_config, tenant_origin, update_wxpusher_config,
)
from apps.accounts.wxpusher_notice_templates import (
    CHAT_KINDS, HALL_NEW_ORDER, NOTICE_TEMPLATES, ORDER_EVENTS, SETTLEMENT_KINDS, STAFF_KINDS,
    notice_display_order_no, render_notice,
)
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import Order
from apps.business.roles import WORKER
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode


class WxPusherConfigSerializer(serializers.Serializer):
    app_id = serializers.RegexField(r"\A" + APP_ID_PATTERN + r"\Z", max_length=19, required=False)
    expected_app_id = serializers.CharField(max_length=19, allow_blank=True, required=False)
    app_token = serializers.CharField(max_length=256, allow_blank=True, required=False, write_only=True)
    login_signing_key = serializers.CharField(
        max_length=256, allow_blank=True, trim_whitespace=False, required=False, write_only=True,
    )
    subscribe_url = serializers.CharField(max_length=200, allow_blank=True, required=False)
    push_enabled = serializers.BooleanField(required=False)
    login_enabled = serializers.BooleanField(required=False)
    binding_prompt_enabled = serializers.BooleanField(required=False)
    order_enabled = serializers.BooleanField(required=False)
    settlement_enabled = serializers.BooleanField(required=False)
    order_events = serializers.JSONField(required=False)

    @sensitive_variables()
    def to_internal_value(self, data):
        if not isinstance(data, dict) or data.keys() - self.fields.keys():
            raise serializers.ValidationError({"non_field_errors": "请提供有效的消息推送配置。"})
        for name, value in data.items():
            if isinstance(self.fields[name], serializers.BooleanField) and type(value) is not bool:
                raise serializers.ValidationError({name: "开关必须为布尔值。"})
            if isinstance(self.fields[name], serializers.CharField) and not isinstance(value, str):
                raise serializers.ValidationError({name: "请填入有效的文本。"})
        return super().to_internal_value(data)

    def validate_order_events(self, events):
        if (
            not isinstance(events, dict)
            or events.keys() - set(ORDER_EVENTS)
            or any(type(enabled) is not bool for enabled in events.values())
        ):
            raise serializers.ValidationError("请提供有效的订单状态开关。")
        return events

    @sensitive_variables()
    def validate_app_token(self, token):
        if token and not re.fullmatch(r"[!-~]+", token):
            raise serializers.ValidationError("请填写 WxPusher 的 appToken。")
        return token

    @sensitive_variables()
    def validate_login_signing_key(self, key):
        if key and (len(key) < 32 or not key.strip()):
            raise serializers.ValidationError("请使用至少 32 个字符的系统对接验证密钥。")
        return key


class WxPusherAdminView(APIView):
    permission_classes = [IsAuthenticated, IsAccountAdmin]
    parser_classes = [JSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        result = super().finalize_response(request, response, *args, **kwargs)
        result["Cache-Control"] = "private, no-store"
        return result


class WxPusherAdminConfigView(WxPusherAdminView):
    def get(self, request):
        return api_response(load_wxpusher_config().public_payload(request.tenant))

    @sensitive_variables()
    def patch(self, request):
        serializer = WxPusherConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        changes = dict(serializer.validated_data)
        expected_app_id = changes.pop("expected_app_id", None)
        for name in ("app_token", "login_signing_key"):
            if changes.get(name) == "":
                changes.pop(name)
        try:
            with transaction.atomic():
                config = update_wxpusher_config(changes, expected_app_id=expected_app_id)
                if changes:
                    record(
                        log_type=ACTION_LOG_TYPES["system.wxpusher_config_update"],
                        action="system.wxpusher_config_update",
                        actor=request.user.get_username(), actor_id=request.user.pk,
                        field_name=",".join(
                            name for name in ("app_id", "app_token", "login_signing_key", "binding_prompt_enabled")
                            if name in changes
                        ),
                    )
        except WxPusherConfigError as exc:
            return api_response(msg=str(exc), code=ErrorCode.BUSINESS)
        return api_response(config.public_payload(request.tenant))


def _recipients(app_id):
    return WxPusherBinding.objects.filter(
        Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False),
        app_id=app_id, user__is_active=True, user__groups__name=WORKER,
    ).exclude(Q(uid="") | Q(scan_bound_uid="")).select_related("user").only(
        "user_id", "user__first_name", "user__username",
    )


class WxPusherAdminRecipientsView(WxPusherAdminView):
    def get(self, request):
        config = load_wxpusher_config()
        bindings = _recipients(config.app_id).order_by("user__first_name", "user__username") if config.configured else ()
        return api_response({"items": [
            {"id": binding.user_id, "name": user_display_name(binding.user)} for binding in bindings
        ]})


class WxPusherTestRecipientSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1, max_value=2**31 - 1)
    expected_app_id = serializers.CharField(max_length=19, required=False)


class WxPusherAdminTestView(WxPusherAdminView):
    def post(self, request):
        serializer = WxPusherTestRecipientSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config = load_wxpusher_config()
        if not config.configured:
            return api_response(msg="请先保存 WxPusher 应用配置。", code=ErrorCode.BUSINESS)
        if serializer.validated_data.get("expected_app_id", config.app_id) != config.app_id:
            return api_response(msg="WxPusher 应用已更新，请刷新页面后再试。", code=ErrorCode.BUSINESS)
        binding = _recipients(config.app_id).filter(user_id=serializer.validated_data["user_id"]).first()
        if binding is None:
            return api_response(msg="请选择已绑定当前应用且正常启用的打手。", code=ErrorCode.BUSINESS)
        try:
            return api_response(send_test_notice(binding.user, config))
        except BindingError as exc:
            return api_response(msg=str(exc), code=ErrorCode.BUSINESS)


class WxPusherHistoryQuery(serializers.Serializer):
    order_no = serializers.CharField(max_length=128, required=False, allow_blank=True)
    recipient = serializers.CharField(max_length=150, required=False, allow_blank=True)
    status = serializers.ChoiceField(choices=WxPusherNotice.Status.values, required=False, allow_blank=True)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    page = serializers.IntegerField(min_value=1, max_value=2**31 - 1, default=1)

    def validate(self, attrs):
        start = attrs.get("date_from")
        end = attrs.get("date_to")
        if start and end and start > end:
            raise serializers.ValidationError({"date_to": "截至日期不能早于起始日期。"})
        return attrs


class WxPusherAdminHistoryView(WxPusherAdminView):
    def get(self, request):
        query = WxPusherHistoryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data
        order_no = filters.get("order_no", "")
        recipient = filters.get("recipient", "")
        notices = WxPusherNotice.objects.all()
        if order_no:
            matching_orders = Order.objects.filter(source_order_no=order_no).values("order_no")
            missing_snapshot = (
                Q(details__display_order_no__isnull=True)
                | Q(details__display_order_no=None)
                | Q(details__display_order_no="")
            )
            notices = notices.filter(
                Q(order_no=order_no) | Q(details__display_order_no=order_no)
                | Q(details__related_order_nos__contains=[order_no])
                | (missing_snapshot & Q(order_no__in=matching_orders)),
            )
        if recipient:
            notices = notices.filter(Q(user__first_name__icontains=recipient) | Q(user__username__icontains=recipient))
        if filters.get("status"):
            notices = notices.filter(status=filters["status"])
        if "date_from" in filters:
            notices = notices.filter(created_at__date__gte=filters["date_from"])
        if "date_to" in filters:
            notices = notices.filter(created_at__date__lte=filters["date_to"])

        page_size = 50
        total = notices.count()
        start = (filters["page"] - 1) * page_size
        rows = notices.select_related("user").only(
            "id", "kind", "status", "order_no", "details", "skip_reason", "created_at", "attempted_at",
            "user__first_name", "user__username",
        ).order_by("-id")[start:start + page_size] if start < total else ()
        items = [{
            "id": row.pk,
            "kind": row.kind,
            "title": NOTICE_TEMPLATES[row.kind].title if row.kind in NOTICE_TEMPLATES else "团团通测试通知",
            "recipient": user_display_name(row.user),
            "order_no": notice_display_order_no(row.order_no, row.details) or order_no,
            "status": row.status,
            "skip_reason": row.skip_reason,
            "created_at": row.created_at,
            "attempted_at": row.attempted_at,
        } for row in rows]
        return api_response({"items": items, "total": total})


class WxPusherAdminPreviewView(WxPusherAdminView):
    def get(self, request):
        origin = tenant_origin(request.tenant) or "https://example.invalid"
        details = {
            "display_order_no": "示例订单 0086",
            "project": "无畏契约排位 · 黄金二到铂金一",
            "amount": "120.00",
            "game_name": "无畏契约",
            "server_name": "国服",
            "sender": "客服小林",
            "occurred_at": "2026-11-16T14:30:00+08:00",
            "count": 2,
        }
        reasons = {
            "paused": "工作室需要确认新的代练要求。",
            "issue": "订单信息需要核对，请联系客服。",
            "review_rejected": "缺少结单截图。",
            "refunded": "客户取消了订单。",
        }
        items = []
        for kind in NOTICE_TEMPLATES:
            if kind in STAFF_KINDS:
                path = "/v2/approvals"
            elif kind in CHAT_KINDS:
                path = "/m/me"
            elif kind == HALL_NEW_ORDER:
                path = "/m/hall"
            elif kind in SETTLEMENT_KINDS:
                path = "/m/performance"
            else:
                path = "/m/mine"
            url = origin + path
            title, html = render_notice(kind, "", {**details, "reason": reasons.get(kind, "")}, url)
            items.append({"kind": kind, "title": title, "html": html, "url": url})
        return api_response({"items": items})
