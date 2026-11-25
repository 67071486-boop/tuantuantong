import hashlib
import hmac
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model, login
from django.core import signing
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.accounts.models import WxPusherBinding, WxPusherLoginNonce
from apps.accounts.wallet import can_cover_deposit_for_claim
from apps.accounts.wxpusher_config import APP_ID_PATTERN, load_wxpusher_config
from apps.business.api.hall_claim_target import HallClaimTargetQuery, find_hall_claim_target, valid_hall_claim_target
from apps.business.models import Game
from apps.business.roles import WORKER
from apps.core.envelope import api_response, rendered_response
from apps.core.error_codes import ErrorCode
from apps.core.wxpusher import WxPusherClient, WxPusherError

_WINDOW_MS = 600_000
_BROWSER_MAX_AGE = 7 * 24 * 60 * 60
_BROWSER_COOKIE = "wxpusher_browser"
_BROWSER_SALT = "wxpusher-login-browser"
_HALL_PUSH_SALT = "wxpusher-hall-push"
_HALL_ENTRY_SALT = "wxpusher-hall-entry"
_HALL_AUTO_LOGIN_MAX_AGE = 15 * 60
_HALL_TARGET_MAX_AGE = 7 * 24 * 60 * 60
_CONFIRMATION = "wxpusher_login_confirmation"
_MOBILE_ENTRY = "/m/?entry=wxpusher"
_PASSWORD_ENTRY = "/m/login?entry=wxpusher"
_HALL_UNAVAILABLE = "/m/hall?claimUnavailable=1"
_FIELDS = ("nonce", "sv", "ts", "uid", "sign")
_PATTERNS = (
    r"[A-Za-z0-9_+/=-]{1,128}", r"1", r"[0-9]{13}",
    r"UID_[A-Za-z0-9_-]{1,124}", r"[0-9a-f]{64}",
)


def _validated_link(query, key, now_ms):
    values = [query.getlist(field) for field in _FIELDS]
    if any(len(items) != 1 or not re.fullmatch(pattern, items[0]) for items, pattern in zip(values, _PATTERNS, strict=True)):
        return None
    nonce, version, timestamp, uid, signature = (items[0] for items in values)
    if abs(now_ms - int(timestamp)) > _WINDOW_MS:
        return None
    canonical = f"nonce={nonce}&sv={version}&ts={timestamp}&uid={uid}"
    expected = hmac.new(key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if not constant_time_compare(expected, signature):
        return None
    return nonce, uid, int(timestamp) + _WINDOW_MS


def _consume_nonce(app_id, nonce, expires_ms, now_ms):
    expires_at = datetime.fromtimestamp(expires_ms / 1000, UTC)
    cutoff = datetime.fromtimestamp(now_ms / 1000, UTC)
    # 有效票据才触发清理，随机串记录在多个进程间也只允许一条。
    expired = WxPusherLoginNonce.objects.filter(expires_at__lt=cutoff).order_by("expires_at", "pk")[:100]
    WxPusherLoginNonce.objects.filter(pk__in=expired.values("pk")).delete()
    digest = hashlib.sha256(f"{app_id}:{nonce}".encode()).hexdigest()
    _, created = WxPusherLoginNonce.objects.get_or_create(pk=digest, defaults={"expires_at": expires_at})
    return digest if created else None


def _worker_binding(app_id, uid=None, *, user_id=None):
    if uid is None and user_id is None:
        return None
    recipient = Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False)
    if uid is not None:
        recipient = Q(uid=uid) | Q(scan_bound_uid=uid)
    bindings = WxPusherBinding.objects.select_related("user").filter(
        recipient, app_id=app_id, user__is_active=True, user__groups__name=WORKER,
    )
    if user_id is not None:
        bindings = bindings.filter(user_id=user_id)
    return bindings.first()


def _binding_generation(binding):
    started = binding.last_start_at.isoformat() if binding.last_start_at is not None else ""
    return f"{binding.pk}:{started}"


def make_hall_push_path(notice, binding, *, use_handoff=False):
    """Sign the recipient and claim target for the selected notification flow."""
    source = notice.details.get("source")
    ref = notice.details.get("ref")
    if not valid_hall_claim_target(source, ref):
        return _HALL_UNAVAILABLE
    expires_ms = int(timezone.now().timestamp() * 1000) + _HALL_AUTO_LOGIN_MAX_AGE * 1000
    ticket = signing.dumps([
        connection.schema_name, binding.app_id, binding.user_id, _binding_generation(binding),
        source, ref, expires_ms,
    ], salt=_HALL_ENTRY_SALT if use_handoff else _HALL_PUSH_SALT)
    if use_handoff:
        query = {"entry": "wxpusher"}
        if notice.details.get("open_in_app"):
            query.update(source=source, ref=ref)
        return f"/m/wxpusher/hall-entry?{urlencode(query)}#{urlencode({'hall': ticket})}"
    return f"{reverse('auth-wxpusher-login')}?{urlencode({'hall': ticket})}"


def make_shared_hall_push_path(source, ref):
    if not valid_hall_claim_target(source, ref):
        return _HALL_UNAVAILABLE
    query = urlencode({"entry": "wxpusher", "shared": "1", "source": source, "ref": ref})
    return f"/m/wxpusher/hall-entry?{query}"


def _matches_binding(binding, pending):
    return (
        binding is not None
        and _binding_generation(binding) == pending.get("binding_generation")
        and constant_time_compare(binding.user.get_session_auth_hash(), pending["auth_hash"])
    )


def _key_marker(key):
    return salted_hmac("wxpusher-login-confirmation", key, algorithm="sha256").hexdigest()


def _enabled(config):
    return (
        config.configured and config.login_enabled and bool(config.login_signing_key.strip())
        and bool(re.fullmatch(APP_ID_PATTERN, config.app_id))
    )


def _has_worker_session(user):
    return user.is_authenticated and user.is_active and user.groups.filter(name=WORKER).exists()


def _browser_proof(binding, key):
    scope = ":".join((
        connection.schema_name, binding.app_id, str(binding.user_id), binding.effective_uid,
        _binding_generation(binding), _key_marker(key), binding.user.get_session_auth_hash(),
    ))
    return salted_hmac(_BROWSER_SALT, scope, algorithm="sha256").hexdigest()


def _remember_browser(response, binding, key):
    response.set_signed_cookie(
        _BROWSER_COOKIE, _browser_proof(binding, key),
        salt=_BROWSER_SALT, max_age=_BROWSER_MAX_AGE, path=reverse("auth-wxpusher-login"),
        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Lax",
    )


def remember_bound_browser(request, response):
    if request.session.get(SESSION_KEY) != str(request.user.pk) or not _has_worker_session(request.user):
        return
    config = load_wxpusher_config()
    if not _enabled(config):
        return
    app_id, key = config.app_id, config.login_signing_key
    binding = WxPusherBinding.objects.select_related("user").filter(
        Q(uid__isnull=False) | Q(scan_bound_uid__isnull=False), user_id=request.user.pk, app_id=app_id,
    ).first()
    if binding is not None and constant_time_compare(
        request.session.get(HASH_SESSION_KEY, ""), binding.user.get_session_auth_hash(),
    ):
        _remember_browser(response, binding, key)


def forget_bound_browser(response):
    response.delete_cookie(_BROWSER_COOKIE, path=reverse("auth-wxpusher-login"), samesite="Lax")


def _pending_worker(request, binding, config, expires_ms, **extra):
    return {
        "uid": binding.effective_uid, "user_id": binding.user_id, "app_id": binding.app_id,
        "tenant": connection.schema_name, "initiator_id": request.user.pk,
        "key_marker": _key_marker(config.login_signing_key), "expires_ms": expires_ms,
        "auth_hash": binding.user.get_session_auth_hash(), "binding_generation": _binding_generation(binding),
        **extra,
    }


def _checked_worker_login(
    request, pending, config, *, verified_browser, signed_callback=False,
    destination=_MOBILE_ENTRY, fallback=_PASSWORD_ENTRY,
):
    user_id, app_id, uid = pending["user_id"], pending["app_id"], pending["uid"]
    try:
        with WxPusherClient(timeout=3.0) as provider:
            if not provider.is_subscribed(app_token=config.app_token, uid=uid, app_id=app_id):
                return HttpResponseRedirect(fallback)
    except WxPusherError:
        return HttpResponseRedirect(fallback)

    # WxPusher 查询在锁外；落登录会话前与解绑使用相同的账号行锁。
    with transaction.atomic():
        locked = get_user_model().objects.select_for_update().filter(pk=user_id, is_active=True).first()
        binding = _worker_binding(app_id, uid, user_id=user_id) if locked is not None else None
        current = load_wxpusher_config()
        if (
            not _enabled(current) or current.app_id != app_id or current.app_token != config.app_token
            or pending["key_marker"] != _key_marker(current.login_signing_key)
            or not _matches_binding(binding, pending)
            or (binding.requires_trusted_browser and not (verified_browser or signed_callback))
            or pending["expires_ms"] < int(timezone.now().timestamp() * 1000)
        ):
            return HttpResponseRedirect(fallback)
        login(request, binding.user, backend=settings.AUTHENTICATION_BACKENDS[0])
        request.session["identity_plane"] = connection.schema_name
    response = HttpResponseRedirect(destination)
    if not verified_browser:
        _remember_browser(response, binding, config.login_signing_key)
    return response


def _login_worker(request, pending, config, *, signed_callback=False):
    now = timezone.now()
    if not WxPusherLoginNonce.objects.filter(
        pk=pending["receipt"], accepted_at__isnull=True, expires_at__gte=now,
    ).update(accepted_at=now):
        return HttpResponseRedirect(_PASSWORD_ENTRY)
    return _checked_worker_login(request, pending, config, verified_browser=False, signed_callback=signed_callback)


def _hall_password_entry(destination):
    return _PASSWORD_ENTRY + "&" + urlencode({"redirect": destination})


def _read_hall_push_ticket(value, *, salt=_HALL_PUSH_SALT):
    try:
        tenant, app_id, user_id, generation, source, ref, expires_ms = signing.loads(
            value, salt=salt, max_age=_HALL_TARGET_MAX_AGE,
        )
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if (tenant != connection.schema_name or type(user_id) is not int or user_id <= 0
            or type(expires_ms) is not int or not valid_hall_claim_target(source, ref)):
        return None
    return app_id, user_id, generation, source, ref, expires_ms


def _hall_claim_destination(source, ref):
    return "/m/hall?" + urlencode({"claimSource": source, "claimRef": ref})


def _current_hall_binding(app_id, user_id, generation):
    binding = _worker_binding(app_id, user_id=user_id)
    if binding is None or not binding.effective_uid or _binding_generation(binding) != generation:
        return None
    return binding


def _redirect_hall_entry(request, worker_session):
    fallback = _HALL_UNAVAILABLE if request.user.is_authenticated else _PASSWORD_ENTRY
    values = request.GET.getlist("hallEntry")
    if (not worker_session or len(values) != 1 or len(values[0]) > 1024 or "hall" in request.GET
            or request.headers.get("Sec-Fetch-Dest", "document") != "document"):
        return HttpResponseRedirect(fallback)
    ticket = _read_hall_push_ticket(values[0], salt=_HALL_ENTRY_SALT)
    if ticket is None:
        return HttpResponseRedirect(fallback)
    app_id, user_id, generation, source, ref, _ = ticket
    if request.user.pk != user_id or app_id != load_wxpusher_config().app_id:
        return HttpResponseRedirect(fallback)
    if _current_hall_binding(app_id, user_id, generation) is None:
        return HttpResponseRedirect(fallback)
    return HttpResponseRedirect(_hall_claim_destination(source, ref))


def _redirect_hall_push(request, worker_session):
    fallback = _HALL_UNAVAILABLE if worker_session else _hall_password_entry(_HALL_UNAVAILABLE)
    values = request.GET.getlist("hall")
    if (len(values) != 1 or len(values[0]) > 1024
            or request.headers.get("Sec-Fetch-Dest", "document") != "document"):
        return HttpResponseRedirect(fallback)
    ticket = _read_hall_push_ticket(values[0])
    if ticket is None:
        return HttpResponseRedirect(fallback)
    app_id, user_id, generation, source, ref, expires_ms = ticket
    destination = _hall_claim_destination(source, ref)
    if worker_session:
        return HttpResponseRedirect(destination if request.user.pk == user_id else fallback)
    password = _hall_password_entry(destination)
    if request.user.is_authenticated or expires_ms <= int(timezone.now().timestamp() * 1000):
        return HttpResponseRedirect(password)

    config = load_wxpusher_config()
    if not _enabled(config) or config.app_id != app_id:
        return HttpResponseRedirect(password)
    proof = request.get_signed_cookie(_BROWSER_COOKIE, default="", salt=_BROWSER_SALT, max_age=_BROWSER_MAX_AGE)
    if not proof:
        return HttpResponseRedirect(password)
    binding = _current_hall_binding(app_id, user_id, generation)
    if binding is None or not constant_time_compare(proof, _browser_proof(binding, config.login_signing_key)):
        return HttpResponseRedirect(password)
    pending = _pending_worker(request, binding, config, expires_ms)
    return _checked_worker_login(
        request, pending, config, verified_browser=True, destination=destination, fallback=password,
    )


def _official_continue_url(app_id):
    return f"https://wxpusher.zjiecode.com/app/#/follow?type=1&id={app_id}&jump=auto"


def _official_continue_response(app_id):
    return api_response({"url": _official_continue_url(app_id)})


class HallContinuePayload(serializers.Serializer):
    hall = serializers.CharField(max_length=1024, trim_whitespace=False)


def _hall_continue_response(request, params):
    payload = HallContinuePayload(data=params)
    ticket = None
    if payload.is_valid():
        ticket = _read_hall_push_ticket(payload.validated_data["hall"], salt=_HALL_ENTRY_SALT)
    invalid = api_response(code=ErrorCode.VALIDATION, msg="抢单通知链接无效或已失效。", status=400)
    if ticket is None:
        return invalid
    app_id, user_id, generation, *_ = ticket
    if request.user.is_authenticated and request.user.pk != user_id:
        return invalid
    config = load_wxpusher_config()
    if not _enabled(config) or app_id != config.app_id:
        return invalid
    if _current_hall_binding(app_id, user_id, generation) is None:
        return invalid
    return _official_continue_response(app_id)


def _can_open_shared_hall_in_app(request, target):
    if not settings.DEPOSIT_ENFORCED_APP:
        return True
    # App 的实际身份由接单接口核验，这里只按当前网页会话提供跳转建议。
    if not request.user.is_authenticated or request.auth is not None:
        return False
    card = find_hall_claim_target(request.user, **target)
    if card is None:
        return False
    game = Game.objects.filter(pk=card["game"]).first()
    return game is not None and can_cover_deposit_for_claim(request.user, game)


@method_decorator([never_cache, csrf_protect], name="dispatch")
class WxPusherHallContinueView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params
        query = HallClaimTargetQuery(data=params)
        if (set(params) != {"source", "ref"} or any(len(values) != 1 for _, values in params.lists())
                or not query.is_valid()):
            return api_response(code=ErrorCode.VALIDATION, msg="抢单通知链接无效。", status=400)
        config = load_wxpusher_config()
        return api_response({
            "url": _official_continue_url(config.app_id) if _enabled(config) else None,
            "open_in_app": _can_open_shared_hall_in_app(request, query.validated_data),
        })

    def post(self, request):
        return _hall_continue_response(request, request.data)

@method_decorator([never_cache, csrf_protect], name="dispatch")
class WxPusherOrderContinueView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_no):
        if not re.fullmatch(r"[0-9]{20}", order_no):
            return api_response(code=ErrorCode.VALIDATION, msg="订单编号格式不正确。", status=400)
        return _continue_url()


def _continue_url():
    config = load_wxpusher_config()
    if not _enabled(config):
        return api_response(code=ErrorCode.BUSINESS, msg="暂时无法通过 WxPusher 登录，请使用账号密码。")
    return _official_continue_response(config.app_id)


@method_decorator(never_cache, name="dispatch")
class WxPusherChatContinueView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return _continue_url()


@method_decorator(csrf_protect, name="dispatch")
class WxPusherLoginView(View):
    http_method_names = ["get", "post", "options"]

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response["X-Frame-Options"] = "DENY"
        return response

    def get(self, request):
        if "hallStart" in request.GET:
            # Only return the official URL. This branch must leave the login session untouched.
            values = request.GET.getlist("hallStart")
            params = {"hall": values[0]} if len(values) == 1 and len(request.GET) == 1 else {}
            return rendered_response(_hall_continue_response(request, params))
        worker_session = _has_worker_session(request.user)
        fallback = _MOBILE_ENTRY if worker_session else _PASSWORD_ENTRY
        if not request.GET:
            return HttpResponseRedirect(fallback) if worker_session else self._confirmation_page(request)
        request.session.pop(_CONFIRMATION, None)
        if "hallEntry" in request.GET:
            return _redirect_hall_entry(request, worker_session)
        if "hall" in request.GET:
            return _redirect_hall_push(request, worker_session)
        if request.headers.get("Sec-Fetch-Dest", "document") != "document":
            return HttpResponseRedirect(fallback)

        config = load_wxpusher_config()
        app_id, key = config.app_id, config.login_signing_key
        if not _enabled(config):
            return HttpResponseRedirect(fallback)
        now_ms = int(timezone.now().timestamp() * 1000)
        link = _validated_link(request.GET, key, now_ms)
        if link is None:
            return HttpResponseRedirect(fallback)

        nonce, uid, expires_ms = link
        receipt = _consume_nonce(app_id, nonce, expires_ms, now_ms)
        if not receipt or expires_ms < int(timezone.now().timestamp() * 1000):
            return HttpResponseRedirect(fallback)
        binding = _worker_binding(app_id, uid)
        if binding is None or worker_session:
            return HttpResponseRedirect(fallback)

        pending = _pending_worker(request, binding, config, expires_ms, receipt=receipt)
        if not request.user.is_authenticated:
            return _login_worker(request, pending, config, signed_callback=True)
        if binding.requires_trusted_browser:
            return HttpResponseRedirect(fallback)
        request.session[_CONFIRMATION] = pending
        return HttpResponseRedirect(reverse("auth-wxpusher-login"))

    def _pending(self, request, pending, config):
        app_id, key = config.app_id, config.login_signing_key
        if (
            not _enabled(config) or pending["app_id"] != app_id or pending["tenant"] != connection.schema_name
            or pending.get("initiator_id") != request.user.pk
            or pending["key_marker"] != _key_marker(key)
            or pending["expires_ms"] < int(timezone.now().timestamp() * 1000)
        ):
            return None
        binding = _worker_binding(app_id, pending["uid"], user_id=pending["user_id"])
        if not _matches_binding(binding, pending) or binding.requires_trusted_browser:
            return None
        return binding.user

    def _confirmation_page(self, request):
        pending = request.session.get(_CONFIRMATION)
        if not pending:
            return HttpResponseRedirect(_PASSWORD_ENTRY)
        user = self._pending(request, pending, load_wxpusher_config())
        if user is None:
            return HttpResponseRedirect(_PASSWORD_ENTRY)
        username = user.get_username()
        label = username[:1] + "•••" + username[-1:] if len(username) >= 3 else "•••"
        response = render(request, "accounts/wxpusher_login.html", {
            "account_label": label, "confirmation": pending["receipt"],
            "switching_account": request.user.is_authenticated, "password_entry": _PASSWORD_ENTRY,
        })
        response["Referrer-Policy"] = "same-origin"
        response["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; form-action 'self'; style-src 'unsafe-inline'"
        )
        return response

    def post(self, request):
        if _has_worker_session(request.user):
            return HttpResponseRedirect(_MOBILE_ENTRY)
        pending = request.session.get(_CONFIRMATION)
        if not pending or not constant_time_compare(request.POST.get("confirmation", ""), pending["receipt"]):
            return HttpResponseRedirect(_PASSWORD_ENTRY)
        request.session.pop(_CONFIRMATION, None)
        config = load_wxpusher_config()
        if self._pending(request, pending, config) is None:
            return HttpResponseRedirect(_PASSWORD_ENTRY)
        return _login_worker(request, pending, config)
