from rest_framework.views import APIView

from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.models import ApiAppKind
from apps.openapi.permissions import EndpointOutOfScope
from apps.openapi.throttling import ApiAppFallbackThrottle


class IntegrationAppAuthentication(ApiAppBearerAuthentication):
    def authenticate(self, request):
        user, app = super().authenticate(request)
        if app.kind != ApiAppKind.BUSINESS_INTEGRATION:
            raise EndpointOutOfScope("此接口只接受业务应用凭据")
        return user, app


class IntegrationView(APIView):
    authentication_classes = [IntegrationAppAuthentication]
    permission_classes = []
    throttle_classes = [ApiAppFallbackThrottle]


def integration_actor(request):
    import re

    from rest_framework.exceptions import ValidationError

    from apps.openapi.api.order_write_views import OPEN_API_ACTOR_TEMPLATE

    label = OPEN_API_ACTOR_TEMPLATE.format(request.auth.name)
    subject = request.headers.get("X-TT-Actor")
    if subject is None:
        return label
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,64}", subject):
        raise ValidationError("X-TT-Actor 格式不正确")
    return f"{label} / {subject}"
