from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError

from apps.accounts.display import user_display_name
from apps.accounts.serializers import AccountWriteSerializer
from apps.accounts.views import _persist_account, _validate_write_serializer
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.roles import WORKER
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.integration_auth import IntegrationView, integration_actor
from apps.openapi.api.order_extra_views import _paginated
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.idempotency import idempotent
from apps.openapi.serializers import money
from apps.accounts.models import WxPusherBinding

User = get_user_model()


TAKER_FIELDS = frozenset({
    "username", "display_name", "games", "business_type", "member_type", "is_studio",
    "grab_limit_dl", "grab_limit_pw", "grab_price_min", "grab_price_max",
    "grab_forbid_self_service", "live_url", "qq", "mobile", "hired_on", "tag", "remark", "order_by",
})


def integration_takers(*, include_deleted=False):
    others = Group.objects.exclude(name=WORKER)
    takers = User.objects.filter(groups__name=WORKER, is_staff=False, is_superuser=False).exclude(groups__in=others)
    if not include_deleted:
        takers = takers.filter(staff_profile__deleted_at__isnull=True)
    return takers


class TakerWriteInput(AccountWriteSerializer):
    creation_required_fields = ()

    def get_fields(self):
        return {name: field for name, field in super().get_fields().items() if name in TAKER_FIELDS}

    def to_internal_value(self, data):
        if not isinstance(data, dict) or data.keys() - TAKER_FIELDS:
            raise ValidationError({"non_field_errors": ["请求体含有不能写入的打手字段"]})
        return super().to_internal_value(data)


class TakerListParams(serializers.Serializer):
    q = serializers.CharField(required=False, max_length=64)
    is_active = serializers.BooleanField(required=False)
    include_deleted = serializers.BooleanField(required=False, default=False)
    after = serializers.IntegerField(min_value=0, max_value=9223372036854775807, required=False)


def _taker_row(user):
    profile = getattr(user, "staff_profile", None)
    data = {field: getattr(profile, field, None) for field in TAKER_FIELDS - {"username", "display_name", "games"}}
    data.update({
        "id": user.pk,
        "username": user.username,
        "display_name": user_display_name(user),
        "is_active": user.is_active,
        "games": sorted(game.pk for game in profile.games.all()) if profile else [],
        "grab_price_min": money(data["grab_price_min"]),
        "grab_price_max": money(data["grab_price_max"]),
        "deleted_at": profile.deleted_at.isoformat() if profile and profile.deleted_at else None,
    })
    return data


def _taker_queryset(*, include_deleted=False):
    return integration_takers(include_deleted=include_deleted).select_related("staff_profile").prefetch_related("staff_profile__games")


def _taker(taker_id, *, lock=False, include_deleted=False):
    takers = _taker_queryset(include_deleted=include_deleted)
    if lock:
        takers = takers.select_for_update(of=("self",))
    user = takers.filter(pk=taker_id).first()
    if user is None:
        raise NotFound("打手不存在")
    return user


def log_taker(request, user, action, **columns):
    record(
        log_type=ACTION_LOG_TYPES[action], action=action, actor=integration_actor(request),
        system=True, target_kind="user", target_id=user.pk, target_name=user.username, **columns,
    )


class TakerListView(IntegrationView):
    def get(self, request):
        form = TakerListParams(data=request.query_params.dict())
        form.is_valid(raise_exception=True)
        filters = form.validated_data
        takers = _taker_queryset(include_deleted=filters["include_deleted"]).order_by("id")
        if "is_active" in filters:
            takers = takers.filter(is_active=filters["is_active"])
        if "q" in filters:
            search = filters["q"]
            takers = takers.filter(Q(username__icontains=search) | Q(first_name__icontains=search))
        if "after" in filters:
            takers = takers.filter(pk__gt=filters["after"])
        return api_response(_paginated(takers, request, _taker_row))

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request):
        form = TakerWriteInput(data=request.data)
        error = _validate_write_serializer(form)
        if error is not None:
            return error
        fields = dict(form.validated_data, role=WORKER)
        if User.objects.filter(username=fields["username"]).exists():
            return api_response(msg="登录账号已存在，请使用恢复入口", code=ErrorCode.CONFLICT)
        try:
            with transaction.atomic():
                user = _persist_account(fields)
                user.set_unusable_password()
                user.save(update_fields=["password"])
                log_taker(request, user, "user.create", reason="业务应用新增打手")
        except IntegrityError:
            return api_response(msg="登录账号已存在", code=ErrorCode.CONFLICT)
        return api_response(_taker_row(user))


class TakerDetailView(IntegrationView):
    def get(self, request, taker_id):
        return api_response(_taker_row(_taker(taker_id)))

    @idempotent(spec=OPENAPI_SPEC)
    def patch(self, request, taker_id):
        user = _taker(taker_id, lock=True)
        form = TakerWriteInput(instance=user, data=request.data, partial=True)
        error = _validate_write_serializer(form)
        if error is not None:
            return error
        try:
            with transaction.atomic():
                user = _persist_account(form.validated_data, user=user)
                log_taker(request, user, "user.update", reason="、".join(sorted(form.validated_data)))
        except IntegrityError:
            return api_response(msg="登录账号已存在", code=ErrorCode.CONFLICT)
        return api_response(_taker_row(_taker(user.pk)))


class TakerLifecycleView(IntegrationView):
    action = ""

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, taker_id):
        user = _taker(taker_id, lock=True, include_deleted=self.action in {"delete", "restore"})
        profile = getattr(user, "staff_profile", None)
        if profile is None:
            raise NotFound("打手档案不存在")
        if self.action == "delete" and profile.deleted_at is None:
            profile.deleted_at = timezone.now()
            profile.save(update_fields=["deleted_at"])
            user.is_active = False
            user.save(update_fields=["is_active"])
            WxPusherBinding.objects.filter(user_id=user.pk).delete()
            log_taker(request, user, "user.delete", reason="业务应用删除打手")
        elif self.action == "restore" and profile.deleted_at is not None:
            profile.deleted_at = None
            profile.save(update_fields=["deleted_at"])
            log_taker(request, user, "user.restore", reason="业务应用恢复打手")
        elif self.action in {"enable", "disable"}:
            active = self.action == "enable"
            if user.is_active != active:
                user.is_active = active
                user.save(update_fields=["is_active"])
                log_taker(request, user, "user.status", to_value="启用" if active else "停用")
        return api_response(_taker_row(user))


class OrderAssigneeListView(IntegrationView):
    def get(self, request):
        from apps.openapi.api.order_extra_views import ProjectQuerySerializer

        form = ProjectQuerySerializer(data=request.query_params, partial=True)
        form.is_valid(raise_exception=True)
        takers = _taker_queryset().filter(is_active=True).order_by("id")
        if "game_id" in form.validated_data:
            takers = takers.filter(staff_profile__games=form.validated_data["game_id"])
        return api_response(_paginated(takers, request, _taker_row))
