import re

from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.core.signing import BadSignature, Signer
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts import wxpusher_binding, wxpusher_notifications, wxpusher_staff
from apps.accounts.models import AppToken, WxPusherBinding
from apps.accounts.wxpusher_config import APP_ID_PATTERN, load_wxpusher_config, tenant_origin
from apps.accounts.wxpusher_notice_templates import ORDER_EVENTS, STAFF_KINDS
from apps.business.roles import WORKER
from apps.core.authentication import TenantBoundAuthentication
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.wxpusher_app_links import DAIDAITONG_ORIGIN
from apps.rbac.permissions import has_node


_PENDING_BINDING = "wxpusher_pending_binding"


class IsWxPusherWorker(BasePermission):
    def has_permission(self, request, view):
        return request.user.groups.filter(name=WORKER).exists()


class IsWxPusherStaff(BasePermission):
    def has_permission(self, request, view):
        return wxpusher_staff.is_wxpusher_staff(request.user)


STAFF_WXPUSHER_PERMISSIONS = [IsAuthenticated, IsWxPusherStaff]
STAFF_WXPUSHER_AUTHENTICATION = [TenantBoundAuthentication]
STAFF_WXPUSHER_VIEW_OPTIONS = {
    "staff": True,
    "permission_classes": STAFF_WXPUSHER_PERMISSIONS,
    "authentication_classes": STAFF_WXPUSHER_AUTHENTICATION,
}


def _browser_scope(request, config):
    return [request.tenant.schema_name, config.app_id, str(request.user.pk)]


def _app_binding_signer(request):
    # 同一账号的不同 App 登录也各自持有绑定凭据。
    return Signer(salt=f"wxpusher-app-binding:{request.tenant.schema_name}:{request.auth.key_hash}")


def _is_bound(request, config):
    return config.source != "none" and WxPusherBinding.objects.filter(
        Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False),
        user_id=request.user.pk, app_id=config.app_id,
    ).exists()


def _pending_binding(request, config):
    if config.source == "none" or request.session.get(SESSION_KEY) != str(request.user.pk):
        return None
    marker = request.session.get(_PENDING_BINDING)
    return marker if marker and marker["scope"] == _browser_scope(request, config) else None


def _pending_link(request, config):
    marker = _pending_binding(request, config)
    return marker if marker and marker.get("mode") == "link" else None


def _pending_guide(request, config, *, staff):
    marker = _pending_binding(request, config)
    if marker is None:
        return None
    if marker.get("mode") == "link":
        expires_at = None if staff else wxpusher_binding.link_attempt_expiry(request.user, config, marker["challenge"])
        if expires_at is None:
            return None
        return {
            "mode": "link", "attempt_id": marker["challenge"],
            "subscribe_url": marker["subscribe_url"], "expires_at": expires_at,
        }
    expires_at = WxPusherBinding.objects.filter(
        user_id=request.user.pk, app_id=config.app_id, uid__isnull=True, scan_bound_uid__isnull=True,
        challenge=marker["challenge"], expires_at__gt=timezone.now(),
    ).exclude(qr_code="").values_list("expires_at", flat=True).first()
    if expires_at is None:
        return None
    return {"qr_url": marker["qr_url"], "expires_at": expires_at}


class WxPusherView(APIView):
    permission_classes = [IsAuthenticated, has_node("my_profile"), IsWxPusherWorker]
    staff = False

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response


class WxPusherGuideView(WxPusherView):
    """按「我的」权限读取本工作室的公开订阅入口。"""

    def get(self, request):
        config = load_wxpusher_config()
        binding_enabled = bool(config.app_token)
        native = isinstance(request.auth, AppToken)
        payload = {
            "subscribe_url": config.subscribe_url or None,
            "qr_url": config.qr_url or None,
            "binding_enabled": binding_enabled,
            "bound": _is_bound(request, config),
            "pending_binding": _pending_guide(request, config, staff=self.staff) if binding_enabled and not native else None,
        }
        if self.staff:
            payload["workspace_enabled"] = config.configured and config.push_enabled
        else:
            payload["binding_prompt_enabled"] = binding_enabled and config.binding_prompt_enabled
            if binding_enabled and not native:
                payload["binding_link_supported"] = bool(re.fullmatch(APP_ID_PATTERN, config.app_id))
        if native:
            payload["native_binding_supported"] = True
        return api_response(payload)


class WxPusherPreferencesSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or data.keys() - {"order_enabled", "settlement_enabled", "order_events", "group_chat_enabled"}:
            raise serializers.ValidationError({"preferences": "请提供有效的推送设置。"})
        for key in ("order_enabled", "settlement_enabled", "group_chat_enabled"):
            if key in data and type(data[key]) is not bool:
                raise serializers.ValidationError({key: "开关必须为布尔值。"})
        events = data.get("order_events", {})
        if (not isinstance(events, dict) or events.keys() - set(ORDER_EVENTS)
                or any(type(value) is not bool for value in events.values())):
            raise serializers.ValidationError({"order_events": "请提供有效的订单状态开关。"})
        return data


class WxPusherPreferencesView(WxPusherView):
    parser_classes = [JSONParser]

    def get(self, request):
        if load_wxpusher_config().source == "none":
            return api_response(msg="本工作室尚未开通手机推送。", code=ErrorCode.BUSINESS)
        return api_response(wxpusher_notifications.get_preferences(request.user.pk))

    def patch(self, request):
        if load_wxpusher_config().source == "none":
            return api_response(msg="本工作室尚未开通手机推送。", code=ErrorCode.BUSINESS)
        serializer = WxPusherPreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return api_response(wxpusher_notifications.update_preferences(request.user.pk, serializer.validated_data))


class WxPusherStaffPreferencesSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or data.keys() - {"enabled", "events", "group_chat_enabled"}:
            raise serializers.ValidationError({"preferences": "请提供有效的员工通知设置。"})
        for key in ("enabled", "group_chat_enabled"):
            if key in data and type(data[key]) is not bool:
                raise serializers.ValidationError({key: "开关必须为布尔值。"})
        events = data.get("events", {})
        if (not isinstance(events, dict) or events.keys() - set(STAFF_KINDS)
                or any(type(value) is not bool for value in events.values())):
            raise serializers.ValidationError({"events": "请提供有效的员工通知开关。"})
        return data


class WxPusherStaffPreferencesView(WxPusherView):
    authentication_classes = STAFF_WXPUSHER_AUTHENTICATION
    permission_classes = STAFF_WXPUSHER_PERMISSIONS
    parser_classes = [JSONParser]

    def get(self, request):
        return api_response(wxpusher_staff.get_staff_preferences(request.user.pk))

    def patch(self, request):
        serializer = WxPusherStaffPreferencesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return api_response(wxpusher_staff.update_staff_preferences(request.user.pk, serializer.validated_data))


def _binding_config_error(config, *, require_token=True):
    if config.source == "none":
        return api_response(msg="本工作室尚未开通 WxPusher 绑定。", code=ErrorCode.BUSINESS)
    if require_token and not config.app_token:
        return api_response(msg="本工作室尚未配置 WxPusher 推送，请联系工作室。", code=ErrorCode.BUSINESS)
    return None


def _run_binding(request, action, config, *, require_token=True, **kwargs):
    config_error = _binding_config_error(config, require_token=require_token)
    if config_error is not None:
        return config_error
    try:
        return api_response(action(request.user, config, **kwargs))
    except wxpusher_binding.BindingError as exc:
        return api_response(msg=str(exc), code=ErrorCode.BUSINESS)


def _requested_link(request):
    return isinstance(request.data, dict) and request.data.get("mode") == "link"


def _matched_link(request, config):
    marker = _pending_link(request, config)
    return marker if marker and request.data.get("attempt_id") == marker["challenge"] else None


def _browser_binding_session(request):
    return not isinstance(request.auth, AppToken) and request.session.get(SESSION_KEY) == str(request.user.pk)


def _link_session_error():
    return api_response(msg="请在当前浏览器重新生成订阅链接。", code=ErrorCode.BUSINESS)


class WxPusherBindingStartView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        if _requested_link(request):
            if self.staff or not _browser_binding_session(request):
                return _link_session_error()
            response = _run_binding(
                request, wxpusher_binding.start_link_binding, config,
                session_hash=request.session.get(HASH_SESSION_KEY, ""),
            )
            if response.data["code"] == ErrorCode.OK:
                link = response.data["data"]
                request.session[_PENDING_BINDING] = {
                    "mode": "link", "scope": _browser_scope(request, config),
                    "challenge": link["attempt_id"], "subscribe_url": link["subscribe_url"],
                }
            return response
        response = _run_binding(request, wxpusher_binding.start_binding, config, staff=self.staff)
        if response.data["code"] == ErrorCode.OK:
            result = response.data["data"]
            challenge = result.pop("challenge")
            if isinstance(request.auth, AppToken):
                result["challenge"] = _app_binding_signer(request).sign(challenge)
            elif request.session.get(SESSION_KEY) == str(request.user.pk):
                request.session[_PENDING_BINDING] = {
                    "scope": _browser_scope(request, config), "challenge": challenge, "qr_url": result["qr_url"],
                }
        return response


def _finish_browser_binding(request, response, *, staff=False, consume_session_marker=True):
    from apps.core.wxpusher_login import remember_bound_browser

    if consume_session_marker:
        request.session.pop(_PENDING_BINDING, None)
    if not staff:
        remember_bound_browser(request, response)
    return response


class WxPusherAppChallengeSerializer(serializers.Serializer):
    challenge = serializers.RegexField(r"\A[a-f0-9]{32}:[A-Za-z0-9_-]{43}\Z", trim_whitespace=False)


class WxPusherBindingProgressView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        if _requested_link(request):
            return self._link_progress(request, config)
        if _is_bound(request, config):
            return api_response({"bound": True})
        if isinstance(request.auth, AppToken):
            return self._app_progress(request, config)
        marker = _pending_binding(request, config)
        if marker is None or marker.get("mode") == "link":
            return api_response(msg="请在当前浏览器重新生成绑定二维码。", code=ErrorCode.BUSINESS)
        response = _run_binding(
            request, wxpusher_binding.check_auto_binding, config,
            challenge=marker["challenge"], session_hash=request.session.get(HASH_SESSION_KEY, ""),
            staff=self.staff,
        )
        if response.data["code"] == ErrorCode.OK and response.data["data"]["bound"]:
            return _finish_browser_binding(request, response, staff=self.staff)
        if response.data["code"] != ErrorCode.OK and _is_bound(request, config):
            return api_response({"bound": True})
        return response

    def _link_progress(self, request, config):
        marker = _matched_link(request, config)
        if self.staff or not _browser_binding_session(request) or marker is None:
            return _link_session_error()
        config_error = _binding_config_error(config)
        if config_error is not None:
            return config_error
        try:
            result = wxpusher_binding.check_link_binding(
                request.user, config, challenge=marker["challenge"],
                session_hash=request.session.get(HASH_SESSION_KEY, ""),
            )
        except wxpusher_binding.BindingError as exc:
            return api_response(msg=str(exc), code=ErrorCode.BUSINESS)
        return _link_proof_response(request, result)

    def _app_progress(self, request, config):
        serializer = WxPusherAppChallengeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            challenge = _app_binding_signer(request).unsign(serializer.validated_data["challenge"])
        except BadSignature:
            return api_response(msg="绑定已失效，请重新生成二维码再次绑定。", code=ErrorCode.BUSINESS)
        response = _run_binding(
            request, wxpusher_binding.check_auto_binding, config, challenge=challenge,
            session_hash=request.user.get_session_auth_hash(), app_token_id=request.auth.pk,
        )
        if response.data["code"] != ErrorCode.OK and _is_bound(request, config):
            return api_response({"bound": True})
        return response


class WxPusherLinkCancelView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        if not _requested_link(request) or not _browser_binding_session(request):
            return _link_session_error()
        marker = _matched_link(request, config)
        if marker is None:
            return _link_session_error()
        return _run_binding(
            request, wxpusher_binding.cancel_link_binding, config,
            challenge=marker["challenge"], session_hash=request.session.get(HASH_SESSION_KEY, ""),
        )


class WxPusherLinkUidSerializer(serializers.Serializer):
    uid = serializers.RegexField(r"\AUID_[A-Za-z0-9_-]{1,124}\Z", trim_whitespace=False)


class WxPusherSubscriptionCallbackData(WxPusherLinkUidSerializer):
    appId = serializers.RegexField(APP_ID_PATTERN, max_length=19)
    extra = serializers.RegexField(r"\A[a-f0-9]{32}\.[a-f0-9]{24}\Z", trim_whitespace=False)


class WxPusherSubscriptionCallbackView(WxPusherView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]

    def post(self, request):
        event = request.data
        if not isinstance(event, dict) or event.get("action") != "app_subscribe":
            return api_response()
        payload = WxPusherSubscriptionCallbackData(data=event.get("data"))
        if payload.is_valid():
            config = load_wxpusher_config()
            values = payload.validated_data
            if config.configured and values["appId"] == config.app_id:
                wxpusher_binding.record_link_subscription(config, values["extra"], values["uid"])
        return api_response()


class WxPusherBindingScanView(WxPusherView):
    def post(self, request):
        return _run_binding(request, wxpusher_binding.scan_binding, load_wxpusher_config())


class WxPusherCodeSerializer(serializers.Serializer):
    code = serializers.JSONField()

    def validate_code(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
            raise serializers.ValidationError("请输入 6 位验证码。")
        return value


def _link_proof_response(request, result):
    response = api_response(result)
    if result["bound"]:
        # DB consumption must not overwrite a newer attempt's marker in another tab.
        return _finish_browser_binding(request, response, consume_session_marker=False)
    return response


class WxPusherLinkCodeView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        if not _requested_link(request) or not _browser_binding_session(request):
            return _link_session_error()
        marker = _matched_link(request, config)
        if marker is None:
            return _link_session_error()
        serializer = WxPusherLinkUidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return _run_binding(
            request, wxpusher_binding.send_link_code, config,
            challenge=marker["challenge"], session_hash=request.session.get(HASH_SESSION_KEY, ""),
            uid=serializer.validated_data["uid"],
        )


class WxPusherLinkConfirmView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        if not _requested_link(request) or not _browser_binding_session(request):
            return _link_session_error()
        marker = _matched_link(request, config)
        if marker is None:
            return _link_session_error()
        serializer = WxPusherCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        config_error = _binding_config_error(config)
        if config_error is not None:
            return config_error
        try:
            result = wxpusher_binding.confirm_link_code(
                request.user, config, challenge=marker["challenge"],
                session_hash=request.session.get(HASH_SESSION_KEY, ""), code=serializer.validated_data["code"],
            )
        except wxpusher_binding.BindingError as exc:
            return api_response(msg=str(exc), code=ErrorCode.BUSINESS)
        return _link_proof_response(request, result)


def _confirm(user, config, *, code):
    wxpusher_binding.confirm_binding(user, config, code)
    return {"bound": True}


def _unlink(user, config, *, staff):
    wxpusher_binding.unlink_binding(user, config, staff=staff)
    return {"bound": False}


class WxPusherBindingConfirmView(WxPusherView):
    def post(self, request):
        serializer = WxPusherCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        response = _run_binding(request, _confirm, load_wxpusher_config(), code=serializer.validated_data["code"])
        if response.data["code"] == ErrorCode.OK:
            return _finish_browser_binding(request, response)
        return response


class WxPusherBindingTestView(WxPusherView):
    def post(self, request):
        config = load_wxpusher_config()
        native = isinstance(request.auth, AppToken)
        return _run_binding(
            request, wxpusher_binding.send_test_notice, config, staff=self.staff,
            open_daidaitong=not self.staff and tenant_origin(request.tenant).lower() == DAIDAITONG_ORIGIN,
            session_hash=request.user.get_session_auth_hash() if native else None,
            app_token_id=request.auth.pk if native else None,
        )


class WxPusherBindingView(WxPusherView):
    def delete(self, request):
        response = _run_binding(request, _unlink, load_wxpusher_config(), require_token=False, staff=self.staff)
        if response.data["code"] == ErrorCode.OK:
            request.session.pop(_PENDING_BINDING, None)
        return response
