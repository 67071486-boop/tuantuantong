"""付款支付宝授权：记录按当前租户隔离，控制服务结果决定状态。"""
from urllib.parse import urlsplit

from django.db import transaction
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.qr import qr_png_base64
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.alipay_auth_control import (
    ControlCaptchaFailed, ControlIdentityIncomplete, ControlIdentityUnverified, ControlOutcomeUnknown,
    ControlUnavailable, get_control_client,
)
from apps.business.alipay_auth_crypto import cipher, decrypt, encrypt
from apps.business.models import AlipaySettlementAuthorization, OnlinePayrollTransfer
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

NODE = "finance.alipay_authorization"
UNAVAILABLE = "授权服务暂不可用，请稍后重试"


class CreateSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()
    account = serializers.CharField(max_length=128, trim_whitespace=True)
    real_name = serializers.CharField(max_length=64, trim_whitespace=True)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {"request_id", "account", "real_name"}:
            raise ValidationError({"non_field_errors": ["请填写支付宝账号、实名和请求标识"]})
        return super().to_internal_value(data)


def _masked(account):
    return account[:3] + "****" + account[-4:] if len(account) > 7 else "****"


def _response(row, authorization_url=None):
    png = qr_png_base64(authorization_url) if authorization_url else None
    box = cipher()
    account = decrypt(row.account_ciphertext, box)
    name = decrypt(row.real_name_ciphertext, box)
    legacy_identity = row.next_action == "identity" and row.status != "signed"
    local_phase = None
    if getattr(settings, "ALIPAY_AUTH_LOCAL_ENABLED", False):
        from apps.settlement_control.models import Authorization
        local_phase = Authorization.objects.filter(client="local", request_id=row.request_id).values_list("phase", flat=True).first()
    review = local_phase in {"unknown", "creating", "authorizing", "refreshing", "identity_authorizing", "identity_refreshing", "revoking"}
    response = api_response(data={
        "id": row.pk, "request_id": str(row.request_id),
        "account_masked": _masked(account), "real_name": name[:1] + "*" * max(1, len(name) - 1),
        "status": "pending" if legacy_identity else row.status,
        "last_error": ("操作结果待平台核对，请勿重复操作" if review else
                       "付款身份信息不完整，请联系平台处理" if legacy_identity else row.last_error),
        "next_action": row.next_action if (row.status, row.next_action) in {("pending", "agreement"), ("signed", "identity")} else None,
        "requires_platform_review": row.requires_platform_review or legacy_identity or review,
        "retryable_login": row.status in {"pending", "signed", "revoked"} and row.last_error == "验证码识别失败，请稍后重试" and not (row.requires_platform_review or legacy_identity or review),
        "created_at": row.created_at.isoformat(),
        # 上游尚未提供语义已确认的签约时间。
        "authorized_at": None,
        "authorization_url": authorization_url, "qr_png_base64": png,
    })
    response["Cache-Control"] = "no-store"
    return response


def _record_alipay_authorization(request, row, operation):
    record(log_type=ACTION_LOG_TYPES["finance.alipay_authorization"],
           action="finance.alipay_authorization", actor=request.user.get_username(),
           actor_id=request.user.pk, target_kind="alipay_authorization",
           target_id=str(row.pk), reason=operation)


def _invoke_alipay_control(request, pk, operation, *, log_create_retry=False):
    box = cipher()
    with transaction.atomic():
        row = get_object_or_404(AlipaySettlementAuthorization.objects.select_for_update(), pk=pk)
        if operation == "revoke":
            terminal = [OnlinePayrollTransfer.Status.FAILED, OnlinePayrollTransfer.Status.CLOSED]
            if OnlinePayrollTransfer.objects.filter(
                gateway_batch__batch__authorization=row, finalized_at__isnull=True,
            ).exclude(status__in=terminal).exists():
                return api_response(msg="此支付宝仍有在线付款单待处理，暂时不能取消授权",
                                    code=ErrorCode.SETTLE_STATE_INVALID)
        account = decrypt(row.account_ciphertext, box)
        real_name = decrypt(row.real_name_ciphertext, box)
        in_progress = row.requires_platform_review and row.last_error == "操作处理中，请稍后重试"
        blocked = in_progress or ((row.requires_platform_review or (row.next_action == "identity" and row.status != "signed")) and operation != "refresh")
        if not blocked:
            row.revision += 1
            row.save(update_fields=["revision", "updated_at"])
            if operation != "create" or log_create_retry:
                _record_alipay_authorization(request, row, operation)
        revision = row.revision
        previous_status = row.status
        previous_error = row.last_error
        previous_review = row.requires_platform_review or (row.next_action == "identity" and row.status != "signed")
        previous_next_action = row.next_action
        if row.next_action == "identity" and row.status != "signed":
            previous_status, previous_error = "pending", "付款身份信息不完整，请联系平台处理"
        if not blocked and operation in {"revoke", "identity_refresh"}:
            row.requires_platform_review = True
            row.last_error = "操作处理中，请稍后重试"
            row.save(update_fields=["requires_platform_review", "last_error", "updated_at"])
    if blocked:
        return _response(row)
    url, next_action = None, previous_next_action
    requires_review = False
    clear_authorized_at = False
    try:
        client = get_control_client()
        if operation in {"create", "authorize"}:
            result = client.create(str(row.request_id), account, real_name)
            if operation == "authorize" and result.status in {"pending", "revoked"} and not result.authorization_url:
                result = client.authorize(str(row.request_id))
        else:
            result = getattr(client, operation)(str(row.request_id))
        if result.next_action == "identity" and result.status != "signed":
            raise ControlIdentityIncomplete()
        if result.status == "authorized" and result.next_action is not None:
            raise ControlUnavailable()
        status = result.status if result.status in {"pending", "failed", "signed", "revoked"} else "pending"
        if result.status == "authorized" and result.configured is True:
            status = "authorized"
        if result.status == "pending" and result.next_action == "agreement":
            next_action = result.next_action
            clear_authorized_at = True
        elif result.status == "signed" and result.next_action == "identity":
            next_action = "identity"
        else:
            next_action = ""
        if result.authorization_url and len(result.authorization_url.encode("utf-8")) <= 1024 and status in {"pending", "signed"}:
            try:
                parsed = urlsplit(result.authorization_url)
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                    url = result.authorization_url
            except ValueError:
                pass
        error = "授权未完成，请重试" if status == "failed" else ""
        if result.status not in {"pending", "authorized", "failed", "signed", "revoked"} or (result.status == "authorized" and not result.configured):
            error = "暂无法确认最新授权状态，请稍后重试"
        if previous_status in {"authorized", "signed"} and status not in {"authorized", "signed", "revoked"} and not next_action:
            status, error = previous_status, "暂无法确认最新授权状态，请稍后重试"
            url, next_action = None, ""
    except ControlOutcomeUnknown:
        status = previous_status if previous_status in {"authorized", "signed", "revoked"} else "pending"
        error = "操作结果待平台核对，请勿重复操作"
        requires_review = True
    except ControlIdentityIncomplete:
        status, error, requires_review = "pending", "付款身份信息不完整，请联系平台处理", True
        next_action = ""
        clear_authorized_at = True
    except ControlIdentityUnverified:
        status, error, requires_review = "pending", "无法确认付款身份是否匹配，请联系平台处理", True
        next_action = ""
        clear_authorized_at = True
    except ControlCaptchaFailed:
        status = previous_status if previous_status in {"authorized", "signed", "revoked"} else "pending"
        requires_review = previous_review
        error = previous_error if previous_review else "验证码识别失败，请稍后重试"
    except ControlUnavailable:
        status = previous_status if previous_status in {"authorized", "signed", "revoked"} else "pending"
        requires_review = previous_review
        error = previous_error if previous_review else (
            "暂无法确认最新授权状态，请稍后重试" if status in {"authorized", "signed"} else UNAVAILABLE
        )
    with transaction.atomic():
        row = AlipaySettlementAuthorization.objects.select_for_update().get(pk=pk)
        if row.revision != revision:
            return _response(row)
        row.status = status
        row.next_action = next_action if status in {"pending", "signed"} else ""
        row.requires_platform_review = requires_review
        row.last_error = error
        if clear_authorized_at or status == "authorized":
            row.authorized_at = None
        row.save(update_fields=["status", "next_action", "requires_platform_review", "last_error", "authorized_at", "updated_at"])
    return _response(row, url)


class AlipayAuthorizationCollectionView(APIView):
    permission_classes = [has_node(NODE)]

    def get(self, request):
        cipher()
        rows = AlipaySettlementAuthorization.objects.all()
        response = api_response(data=[_response(row).data["data"] for row in rows])
        response["Cache-Control"] = "no-store"
        return response

    def post(self, request):
        serializer = CreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        box = cipher()
        with transaction.atomic():
            row, created = AlipaySettlementAuthorization.objects.get_or_create(
                request_id=data["request_id"], defaults={
                    "account_ciphertext": encrypt(data["account"], box),
                    "real_name_ciphertext": encrypt(data["real_name"], box),
                }
            )
            if not created and (decrypt(row.account_ciphertext, box) != data["account"]
                                or decrypt(row.real_name_ciphertext, box) != data["real_name"]):
                raise ValidationError("请求标识已用于其他授权")
            if created:
                _record_alipay_authorization(request, row, "create")
        if row.status in {"authorized", "signed"} or row.requires_platform_review or row.next_action == "identity":
            return _response(row)
        return _invoke_alipay_control(request, row.pk, "create", log_create_retry=not created)


class AlipayAuthorizationActionView(APIView):
    permission_classes = [has_node(NODE)]

    def post(self, request, pk, operation):
        if operation not in {"authorize", "refresh", "identity", "identity-refresh", "revoke"}:
            raise ValidationError("不支持的操作")
        expected = {"confirm": True} if operation == "revoke" else {}
        if not isinstance(request.data, dict) or set(request.data) != set(expected) or (operation == "revoke" and request.data["confirm"] is not True):
            raise ValidationError("此操作不接受参数")
        return _invoke_alipay_control(request, pk, operation.replace('-', '_'))
