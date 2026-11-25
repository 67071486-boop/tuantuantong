from uuid import uuid4

from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.db import connection
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, NotFound, ValidationError

from apps.core.envelope import api_response
from apps.openapi.api.integration_auth import IntegrationView
from apps.openapi.api.integration_takers import _taker, integration_takers, log_taker
from apps.openapi.authentication import _ip_allowed, client_ip
from apps.openapi.credentials import OPENAPI_SPEC, app_is_enabled
from apps.openapi.idempotency import idempotent
from apps.openapi.models import ApiApp, ApiAppKind, IntegrationTakerBinding
from apps.openapi.serializers import generate_app_key, generate_app_secret
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import SDK_BINDING_APP_KEY_PREFIX, SDK_BINDING_APP_STATE, TakerApp


TOKEN_SALT = "tuantuantong.integration.taker.v1"
TOKEN_TTL = 300
SUBJECT_CHARACTERS = r"[A-Za-z0-9_.:@-]{1,64}"
SUBJECT_PATTERN = rf"\A{SUBJECT_CHARACTERS}\Z"


class IntegrationSubjectConverter:
    regex = SUBJECT_CHARACTERS

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


class TakerBindingInput(serializers.Serializer):
    taker_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)


class TakerTokenInput(serializers.Serializer):
    external_user_id = serializers.RegexField(SUBJECT_PATTERN)


def _check_subject(external_user_id):
    form = TakerTokenInput(data={"external_user_id": external_user_id})
    form.is_valid(raise_exception=True)
    return form.validated_data["external_user_id"]


def _binding_rows():
    return IntegrationTakerBinding.objects.select_related("app", "taker_app__taker")


def _binding_fingerprint(binding):
    return salted_hmac(TOKEN_SALT, f"{connection.schema_name}:{binding.app.app_secret}:{binding.taker_app.app_secret}").hexdigest()


def _usable_binding(binding):
    if (
        binding is None or binding.app.kind != ApiAppKind.BUSINESS_INTEGRATION
        or not app_is_enabled(binding.app, OPENAPI_SPEC)
        or binding.taker_app.state != SDK_BINDING_APP_STATE
        or not binding.taker_app.taker.is_active
    ):
        return False
    return integration_takers().filter(pk=binding.taker_app.taker_id).exists()


class IntegrationTakerBindingView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def put(self, request, external_user_id):
        subject = _check_subject(external_user_id)
        form = TakerBindingInput(data=request.data)
        form.is_valid(raise_exception=True)
        app = ApiApp.objects.select_for_update().get(pk=request.auth.pk)
        taker = _taker(form.validated_data["taker_id"], lock=True)
        if not taker.is_active:
            raise ValidationError("该打手已停用")
        binding = _binding_rows().filter(app=app, external_user_id=subject).first()
        if binding is not None:
            if binding.taker_app.taker_id != taker.pk:
                raise ValidationError("App 账号已绑定，请先解绑")
            return api_response({"external_user_id": subject, "taker_id": taker.pk})
        if _binding_rows().filter(app=app, taker_app__taker=taker).exists():
            raise ValidationError("该打手已绑定此应用的另一个账号")
        # 旧打手令牌端点始终拒绝此内部凭据。每个绑定单独拥有既有打手幂等台账。
        taker_app = TakerApp.objects.create(
            name=f"SDK {uuid4().hex}", app_key=SDK_BINDING_APP_KEY_PREFIX + generate_app_key(),
            app_secret=generate_app_secret(), taker=taker, state=SDK_BINDING_APP_STATE, created_by=app.created_by,
        )
        IntegrationTakerBinding.objects.create(app=app, external_user_id=subject, taker_app=taker_app)
        log_taker(request, taker, "user.update", reason=f"绑定 App 账号 {subject}")
        return api_response({"external_user_id": subject, "taker_id": taker.pk})

    @idempotent(spec=OPENAPI_SPEC)
    def delete(self, request, external_user_id):
        subject = _check_subject(external_user_id)
        app = ApiApp.objects.select_for_update().get(pk=request.auth.pk)
        binding = _binding_rows().filter(app=app, external_user_id=subject).first()
        if binding is not None:
            log_taker(request, binding.taker_app.taker, "user.update", reason=f"解绑 App 账号 {subject}")
            binding.taker_app.delete()
        return api_response({"external_user_id": subject})


class IntegrationTakerTokenView(IntegrationView):
    def post(self, request):
        form = TakerTokenInput(data=request.data)
        form.is_valid(raise_exception=True)
        binding = _binding_rows().filter(app=request.auth, **form.validated_data).first()
        if not _usable_binding(binding):
            raise NotFound("当前 App 账号未绑定可用的打手")
        token = signing.dumps([binding.pk, _binding_fingerprint(binding)], salt=TOKEN_SALT)
        response = api_response({"access_token": token, "expires_in": TOKEN_TTL, "token_type": "bearer"})
        response["Cache-Control"] = "no-store"
        return response


class IntegrationTakerAuthentication(BaseAuthentication):
    spec = TAKER_SPEC

    def authenticate(self, request):
        try:
            scheme, token = get_authorization_header(request).decode("ascii").split()
            if scheme.lower() != "bearer" or len(token) > 512:
                raise ValueError
            binding_id, digest = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_TTL)
        except (signing.BadSignature, UnicodeError, ValueError):
            raise AuthenticationFailed("打手令牌无效或已过期") from None
        binding = _binding_rows().filter(pk=binding_id).first()
        if (
            not _usable_binding(binding)
            or not constant_time_compare(digest, _binding_fingerprint(binding))
            or not _ip_allowed(binding.app.ip_allowlist, client_ip(request))
        ):
            raise AuthenticationFailed("打手令牌无效或已过期")
        return AnonymousUser(), binding.taker_app

    def authenticate_header(self, request):
        return "Bearer"
