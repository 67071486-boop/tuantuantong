import time
from dataclasses import dataclass

from django.core import signing
from django.db import connection
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from apps.accounts.models import AppToken
from apps.business.roles import WORKER
from apps.core.app_version import AndroidUpdateRequired, requires_android_update
from apps.core.authentication import AppTokenAuthentication

from .realtime import internal_chat_available

_GRANT_SALT = "internal-chat-app-access"
_GRANT_LIFETIME = 60 * 60
_GRANT_MAX_SESSION = 8 * 60 * 60


@dataclass(frozen=True)
class AppChatSession:
    token: AppToken
    deadline: int
    android_version: int | None


def issue_app_chat_grant(token, *, deadline=None, android_version=None):
    expires = deadline if deadline is not None else int(time.time()) + _GRANT_MAX_SESSION
    return signing.dumps(
        [connection.schema_name, token.pk, token.created_at.isoformat(), expires, android_version], salt=_GRANT_SALT,
    )


class AppChatAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = request.META.get("HTTP_AUTHORIZATION", "").split()
        if not parts or parts[0].lower() != "chat":
            return None
        invalid = AuthenticationFailed("聊天登录已过期，请重新打开聊天")
        if len(parts) != 2 or len(parts[1]) > 512 or not internal_chat_available():
            raise invalid
        try:
            tenant, token_id, issued, deadline, *version = signing.loads(
                parts[1], salt=_GRANT_SALT, max_age=_GRANT_LIFETIME,
            )
        except (signing.BadSignature, ValueError, TypeError) as exc:
            raise invalid from exc
        if tenant != connection.schema_name or not isinstance(deadline, int) or deadline <= time.time():
            raise invalid
        AppTokenAuthentication._assert_tenant_active(request)
        token = AppToken.objects.select_related("user").filter(
            pk=token_id, revoked_at__isnull=True, expires_at__gt=timezone.now(), user__is_active=True,
            user__groups__name=WORKER,
        ).first()
        if token is None or token.created_at.isoformat() != issued:
            raise invalid
        # 旧凭据没有版本，只按 Android 网页容器的标记处理；新凭据沿用原生签发值。
        android_version = version[0] if version else (
            0 if "android" in request.headers.get("User-Agent", "").lower() else None
        )
        if len(version) > 1 or (android_version is not None and type(android_version) is not int):
            raise invalid
        if request.method != "OPTIONS" and requires_android_update(android_version):
            raise AndroidUpdateRequired()
        return token.user, AppChatSession(token, deadline, android_version)

    def authenticate_header(self, request):
        return "Chat"
