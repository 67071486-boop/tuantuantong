"""WxPusher 外部浏览器的短时订单截图上传入口。"""

import hashlib
import re
import secrets
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.business.api.my_orders_views import get_own_order_detail_performance
from apps.business.api.screenshot_serializers import ScreenshotClaimSerializer
from apps.business.api.screenshot_views import (
    SCREENSHOT_UPLOAD_NODE,
    claim_screenshot,
    issue_screenshot_ticket,
)
from apps.business.models import OrderScreenshot, ScreenshotBrowserGrant
from apps.business.order_writes import lock_order_row
from apps.business.roles import WORKER
from apps.core.envelope import api_response
from apps.core.media_blob_views import MediaBlobView
from apps.core.plane import on_public_plane
from apps.rbac.permissions import has_node_now

CHALLENGE_TTL = timedelta(minutes=2)
BROWSER_TTL = timedelta(minutes=15)
COOKIE_PATH = "/api/business/screenshot-browser/"
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
WXPUSHER_ANDROID = "/WxPusher-Android"


class OrderParams(serializers.Serializer):
    order_no = serializers.RegexField(r"\A[0-9]{20}\Z")


class ExchangeParams(OrderParams):
    challenge = serializers.RegexField(TOKEN_SHAPE, min_length=43, max_length=43)


class TicketParams(OrderParams):
    ext = serializers.CharField(max_length=8)


class ClaimParams(OrderParams, ScreenshotClaimSerializer):
    pass


def _parse(serializer, data):
    params = serializer(data=data)
    params.is_valid(raise_exception=True)
    return params.validated_data


def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _in_wxpusher(request):
    return request.headers.get("User-Agent", "").endswith(WXPUSHER_ANDROID)


def _cookie_name(order_no):
    return f"ttt_shot_upload_{order_no}"


def _access_denied():
    return PermissionDenied("上传链接已失效，请回到 WxPusher 重新生成")


def _owned_order(user, order_no):
    if (
        not user.is_active
        or not user.groups.filter(name=WORKER).exists()
        or not has_node_now(user, "my_orders.send_to_my_system")
        or not has_node_now(user, SCREENSHOT_UPLOAD_NODE)
    ):
        raise PermissionDenied("你没有上传这笔订单截图的权限")
    return get_own_order_detail_performance(user, order_no).order


def _verify_grant(request, grant, order_no):
    if grant is None:
        raise _access_denied()
    user = grant.user
    if request.user.is_authenticated and request.user.pk != user.pk:
        raise PermissionDenied(
            "浏览器已登录另一个账号。请回到 WxPusher，从右上角复制上传链接，再粘贴到浏览器无痕窗口。"
            "如果链接已用过，请先重新生成。"
        )
    source = SessionStore(session_key=grant.source_session_key).load()
    if (
        source.get("identity_plane") != connection.schema_name
        or source.get(SESSION_KEY) != str(user.pk)
        or not constant_time_compare(source.get(HASH_SESSION_KEY, ""), user.get_session_auth_hash())
    ):
        raise _access_denied()
    try:
        order = _owned_order(user, order_no)
    except (Http404, PermissionDenied) as error:
        raise _access_denied() from error
    if order.pk != grant.order_id:
        raise _access_denied()


def _active_grants(request):
    return ScreenshotBrowserGrant.objects.filter(
        expires_at__gt=timezone.now(),
        host=request.get_host().lower(),
    )


def _active_browser_grants(request, order_no):
    token = request.COOKIES.get(_cookie_name(order_no), "")
    if on_public_plane() or _in_wxpusher(request) or not TOKEN_SHAPE.fullmatch(token):
        raise _access_denied()
    return _active_grants(request).filter(browser_digest=_digest(token))


def _browser_grant(request, order_no):
    grant = _active_browser_grants(request, order_no).select_related("user").first()
    _verify_grant(request, grant, order_no)
    return grant


@contextmanager
def _browser_write_grant(request, order_no):
    with transaction.atomic():
        candidate = (
            _active_browser_grants(request, order_no)
            .filter(order__deleted_at__isnull=True).select_related("order").only("order__order_no").first()
        )
        if candidate is None or candidate.order.order_no != order_no or lock_order_row(candidate.order) is None:
            raise _access_denied()
        # 等锁时可能发生登出、转派或授权过期，动订单前重新取权限。
        yield _browser_grant(request, order_no)


@method_decorator(csrf_protect, name="dispatch")
class ScreenshotBrowserViewSet(viewsets.ViewSet):
    def get_permissions(self):
        return [IsAuthenticated()] if self.action == "start" else [AllowAny()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def start(self, request):
        if on_public_plane() or not _in_wxpusher(request) or not request.session.session_key:
            raise PermissionDenied("请在 WxPusher 内打开订单截图页")
        order_no = _parse(OrderParams, request.data)["order_no"]
        order = _owned_order(request.user, order_no)
        now = timezone.now()
        expired = ScreenshotBrowserGrant.objects.filter(expires_at__lte=now).order_by("expires_at")[:100]
        ScreenshotBrowserGrant.objects.filter(pk__in=expired.values("pk")).delete()
        challenge = secrets.token_urlsafe(32)
        ScreenshotBrowserGrant.objects.create(
            order=order,
            user=request.user,
            host=request.get_host().lower(),
            source_session_key=request.session.session_key,
            challenge_digest=_digest(challenge),
            expires_at=now + CHALLENGE_TTL,
        )
        return api_response({"challenge": challenge})

    def exchange(self, request):
        if on_public_plane() or _in_wxpusher(request):
            raise _access_denied()
        params = _parse(ExchangeParams, request.data)
        order_no = params["order_no"]
        candidates = _active_grants(request).filter(
            challenge_digest=_digest(params["challenge"]), browser_digest__isnull=True,
        )
        grant = candidates.select_related("user").first()
        _verify_grant(request, grant, order_no)
        now = timezone.now()
        token = secrets.token_urlsafe(32)
        if not candidates.filter(pk=grant.pk, expires_at__gt=now).update(
            browser_digest=_digest(token), expires_at=now + BROWSER_TTL,
        ):
            raise _access_denied()
        response = api_response()
        response.set_cookie(
            _cookie_name(order_no), token, max_age=int(BROWSER_TTL.total_seconds()),
            path=COOKIE_PATH, secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Strict",
        )
        return response

    def status(self, request):
        order_no = _parse(OrderParams, request.query_params)["order_no"]
        grant = _browser_grant(request, order_no)
        challenge = request.headers.get("X-Screenshot-Challenge")
        if challenge is not None and (
            not TOKEN_SHAPE.fullmatch(challenge)
            or not constant_time_compare(grant.challenge_digest, _digest(challenge))
        ):
            raise _access_denied()
        return api_response()

    def ticket(self, request):
        params = _parse(TicketParams, request.data)
        order_no = params["order_no"]
        with _browser_write_grant(request, order_no) as grant:
            outcome = issue_screenshot_ticket(
                order_no=order_no, ext=params["ext"],
                img_type=OrderScreenshot.IMG_ORDER, uploaded_by=grant.user, presign=False,
            )
        if outcome.ok:
            screenshot_id = outcome.data["screenshot_id"]
            outcome.data["upload_url"] = f"{COOKIE_PATH}blob/{screenshot_id}?order_no={order_no}"
            outcome.data["expires_in"] = max(0, int((grant.expires_at - timezone.now()).total_seconds()))
        return api_response(outcome.data, code=outcome.code, msg=outcome.msg)

    def claim(self, request):
        params = _parse(ClaimParams, request.data)
        order_no = params["order_no"]
        with _browser_write_grant(request, order_no) as grant:
            outcome = claim_screenshot(
                order_no=order_no,
                screenshot_id=params["screenshot_id"],
                original_name=params["original_name"],
                uploaded_by=grant.user,
                img_type=OrderScreenshot.IMG_ORDER,
            )
        return api_response(outcome.data, code=outcome.code, msg=outcome.msg)


@method_decorator(csrf_protect, name="dispatch")
class ScreenshotBrowserBlobView(MediaBlobView):
    permission_classes = [AllowAny]
    http_method_names = ["put", "options"]

    def put(self, request, screenshot_id):
        order_no = _parse(OrderParams, request.query_params)["order_no"]
        with _browser_write_grant(request, order_no) as grant:
            row = OrderScreenshot.objects.select_for_update().filter(
                pk=screenshot_id, order_id=grant.order_id, uploaded_by_id=grant.user_id,
                img_type=OrderScreenshot.IMG_ORDER, status=OrderScreenshot.STATUS_PENDING,
            ).first()
            if row is None:
                raise _access_denied()
            return super().put(request, row.object_key)
