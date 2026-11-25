"""Approval center API. Record visibility is limited to its participants."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Case, Q, Value, When
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.approval_evidence import (
    evidence_image_url,
    image_belongs_to_user,
    issue_image_ticket,
    lock_evidence,
    read_evidence,
    upload_evidence,
)
from apps.business.approvals import (
    APPROVAL_APPLY,
    APPROVAL_CONFIGURE,
    SYSTEM_ACTOR,
    active_accounts,
    can_decide,
    decide_approval,
    is_approval_account,
    notify_assigned_approval,
    penalty_targets,
    prepare_penalty_order_data,
    route_pending_refunds,
    settings_for_update,
    visible_original_order,
)
from apps.business.dailiantong_push import normal_taker_of
from apps.business.game_scope import games_of
from apps.business.models import (
    Approval, ApprovalKind, ApprovalSettings, ApprovalStatus, OrderType, PenaltyTargetType,
)
from apps.business.order_flags import can_view_taker
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.plane import on_public_plane
from apps.core.storage import ALLOWED_EXTS, MAX_APPROVAL_EVIDENCE_BYTES, get_storage, sniff_image_type
from apps.rbac.permissions import has_node_now


MAX_IMAGES = 20


def _ids(*, optional=True):
    return serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=not optional, max_length=500,
    )


class ApprovalOrderDataSerializer(serializers.Serializer):
    game = serializers.IntegerField(min_value=1, required=False)
    order_source = serializers.IntegerField(min_value=1, required=False)
    order_type = serializers.ChoiceField(choices=OrderType.choices, required=False)
    project_text = serializers.CharField(max_length=900)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)


class PenaltyApplicationSerializer(serializers.Serializer):
    original_order_no = serializers.RegexField(r"^[0-9]{20}\Z", allow_null=True, error_messages={
        "required": "请选择关联订单或选择「无」",
    })
    target_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    reason = serializers.CharField(max_length=3000)
    objection = serializers.CharField(max_length=3000, required=False, default="", allow_blank=True)
    remark = serializers.CharField(max_length=3000, required=False, default="", allow_blank=True)
    approver_id = serializers.IntegerField(min_value=1)
    cc_ids = _ids()
    image_keys = serializers.ListField(
        child=serializers.CharField(max_length=255), required=False, max_length=MAX_IMAGES,
    )
    order = ApprovalOrderDataSerializer()


class ApprovalSettingsSerializer(serializers.Serializer):
    approver_ids = _ids()
    available_cc_ids = _ids()
    default_cc_ids = _ids()
    refund_handler_id = serializers.IntegerField(required=False, min_value=1, allow_null=True)


class ApprovalListQuerySerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("mine", "pending", "copied", "handled"), default="mine")
    page = serializers.IntegerField(min_value=1, default=1, max_value=100_000)
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)


class ApprovalTargetsQuerySerializer(serializers.Serializer):
    game = serializers.IntegerField(min_value=1, required=False)
    key = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    original_order_no = serializers.RegexField(r"^[0-9]{20}\Z", required=False)


class ApprovalDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=(ApprovalStatus.APPROVED, ApprovalStatus.REJECTED))
    comment = serializers.CharField(max_length=3000, required=False, default="", allow_blank=True)

    def validate(self, attrs):
        if attrs["decision"] == ApprovalStatus.REJECTED and not attrs["comment"]:
            raise ValidationError("请填写驳回原因")
        return attrs


class ApprovalImageTicketSerializer(serializers.Serializer):
    ext = serializers.ChoiceField(choices=ALLOWED_EXTS)


def _as_name(user):
    return {"id": user.pk, "name": user_display_name(user)}


def _target_option(user):
    label = PenaltyTargetType(user.penalty_target_type).label
    return _as_name(user) | {"detail": f"{label} · #{user.pk}"}


def _account_choices(queryset):
    return [_as_name(user) for user in queryset]


def _configured_ids(config):
    if config is None:
        return {"approver_ids": [], "available_cc_ids": [], "default_cc_ids": [], "refund_handler_id": None}
    return {
        "approver_ids": list(config.approvers.order_by("pk").values_list("pk", flat=True)),
        "available_cc_ids": list(config.available_cc.order_by("pk").values_list("pk", flat=True)),
        "default_cc_ids": list(config.default_cc.order_by("pk").values_list("pk", flat=True)),
        "refund_handler_id": config.refund_handler_id,
    }


def _rows():
    return Approval.objects.select_related(
        "initiator", "approver", "target", "original_order", "generated_order",
    ).prefetch_related("cc")


VISIBLE_INTAKE_FIELDS = frozenset({
    "game", "server", "category", "project", "order_source", "order_type",
    "project_text", "total_price", "received_price", "platform_price", "fee",
})


def _payload(row, user):
    generated = getattr(row, "generated_order", None)
    return {
        "id": row.pk,
        "kind": row.kind,
        "status": row.status,
        "original_order_no": row.original_order.order_no if row.original_order_id else None,
        "refund_source_no": row.refund_source_no or None,
        "generated_order_no": generated.order_no if generated is not None else None,
        "initiator_name": user_display_name(row.initiator) if row.initiator_id else SYSTEM_ACTOR,
        "approver_name": user_display_name(row.approver) if row.approver_id else None,
        "target_name": user_display_name(row.target) if row.target_id else None,
        "target_type": row.target_type or None,
        "amount": format(row.amount, ".2f") if row.amount is not None else None,
        "reason": row.reason,
        "objection": row.objection,
        "remark": row.remark,
        "comment": row.comment,
        "created_at": local_iso(row.created_at),
        "decided_at": local_iso(row.decided_at),
        "images": [{"url": evidence_image_url(row.pk, index)} for index in range(len(row.image_keys))],
        "cc_names": [user_display_name(member) for member in row.cc.all()],
        "can_decide": can_decide(row, user),
        "requires_assignment": getattr(row, "requires_assignment", False),
        "order": {key: value for key, value in row.order_data.items() if key in VISIBLE_INTAKE_FIELDS},
    }


def _image_keys(keys, user):
    keys = list(dict.fromkeys(keys))
    if not keys:
        return keys
    storage = get_storage()
    for key in keys:
        if not image_belongs_to_user(key, user.pk):
            raise PermissionDenied("截图不属于当前账号")
        with lock_evidence(key):
            meta = storage.head(key)
            if meta is None or not 0 < meta[0] <= MAX_APPROVAL_EVIDENCE_BYTES:
                raise ValidationError("截图未上传成功、为空或超过 5 MB")
            if sniff_image_type(storage, key) is None:
                raise ValidationError("截图仅支持 JPG 或 PNG")
    return keys


class ApprovalTenantView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if on_public_plane():
            raise NotFound()


class ApprovalCollectionView(ApprovalTenantView):
    def get(self, request):
        query = ApprovalListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        mode = query.validated_data["mode"]
        user = request.user
        rows = _rows()
        if mode == "mine":
            rows = rows.filter(initiator=user)
        elif mode == "copied":
            rows = rows.filter(cc=user)
        elif mode == "handled":
            rows = rows.filter(approver=user).exclude(status=ApprovalStatus.PENDING)
        else:
            stale_refund = Q(kind=ApprovalKind.REFUND) & ~Q(approver_id__in=active_accounts().values("pk"))
            assigned = Q(approver=user) & ~stale_refund if is_approval_account(user) else Q(pk__in=[])
            if has_node_now(user, APPROVAL_CONFIGURE):
                assigned |= stale_refund
            rows = rows.filter(assigned, status=ApprovalStatus.PENDING).annotate(
                requires_assignment=Case(When(stale_refund, then=Value(True)), default=Value(False)),
            )

        size = query.validated_data["page_size"]
        start = (query.validated_data["page"] - 1) * size
        return api_response({
            "items": [_payload(row, user) for row in rows[start:start + size]],
            "total": rows.count(),
        })

    def post(self, request):
        if not has_node_now(request.user, "home.approval_apply"):
            raise PermissionDenied("没有发起罚款的权限")
        serializer = PenaltyApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        original_no = data["original_order_no"]
        original = visible_original_order(request.user, original_no) if original_no is not None else None
        order_data = prepare_penalty_order_data(serializer.data["order"], original, request.user)
        keys = _image_keys(data.get("image_keys", []), request.user)
        target = penalty_targets().filter(pk=data["target_id"]).first()
        if target is None:
            raise ValidationError("请选择可指派的罚款对象")
        if target.pk == request.user.pk:
            raise ValidationError("罚款对象不能同时作为这笔订单的销售客服")
        if not games_of(target).filter(pk=order_data["game"]).exists():
            raise ValidationError("罚款对象没有该游戏的接单权限")

        with transaction.atomic():
            config = settings_for_update()
            approver = active_accounts().filter(
                pk=data["approver_id"], pk__in=config.approvers.values("pk"),
            ).first()
            if approver is None:
                raise ValidationError("请选择审批配置中的在用账号")
            explicit = set(data.get("cc_ids", []))
            cc_ids = explicit | set(config.default_cc.values_list("pk", flat=True))
            available = active_accounts().filter(
                pk__in=cc_ids & set(config.available_cc.values_list("pk", flat=True)),
            )
            if not explicit.issubset(set(available.values_list("pk", flat=True))):
                raise ValidationError("请从配置的抄送名单中选择")
            approval = Approval.objects.create(
                kind=ApprovalKind.PENALTY, initiator=request.user, approver=approver,
                original_order=original, target=target, target_type=target.penalty_target_type,
                amount=data["amount"], reason=data["reason"], objection=data["objection"],
                remark=data["remark"], order_data=order_data, image_keys=keys,
            )
            approval.cc.set(available)
            notify_assigned_approval(approval)
        return api_response(_payload(approval, request.user))


class ApprovalOptionsView(ApprovalTenantView):
    def get(self, request):
        can_apply = has_node_now(request.user, APPROVAL_APPLY)
        can_manage = has_node_now(request.user, APPROVAL_CONFIGURE)
        config = ApprovalSettings.objects.filter(pk=1).first() if can_apply else None
        ids = _configured_ids(config)
        approvers = active_accounts().filter(pk__in=ids["approver_ids"])
        cc = list(active_accounts().filter(pk__in=ids["available_cc_ids"]))
        return api_response({
            "can_apply": can_apply,
            "can_manage": can_manage,
            "approvers": _account_choices(approvers),
            "available_cc": _account_choices(cc),
            "default_cc_ids": sorted(set(ids["default_cc_ids"]) & {user.pk for user in cc}),
        })


class ApprovalTargetsView(ApprovalTenantView):
    def get(self, request):
        if not has_node_now(request.user, "home.approval_apply"):
            raise PermissionDenied("没有发起罚款的权限")
        query = ApprovalTargetsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        original_no = data.get("original_order_no")
        original = visible_original_order(request.user, original_no) if original_no is not None else None
        game_id = original.game_id if original is not None else data.get("game")
        if (
            original is None
            and game_id is not None
            and not games_of(request.user).filter(pk=game_id).exists()
        ):
            raise PermissionDenied("没有该游戏的权限")
        queried = penalty_targets(key=data["key"], game_id=game_id).exclude(pk=request.user.pk)
        if game_id is None:
            queried = queried.filter(staff_profile__games__in=games_of(request.user))
        suggested = None
        if original is not None and can_view_taker(request.user):
            taker = normal_taker_of(original)
            if taker is not None and taker.pk != request.user.pk:
                suggested = penalty_targets(game_id=game_id).filter(pk=taker.pk).first()
        return api_response({
            "items": [_target_option(user) for user in queried[:100]],
            "suggested": _target_option(suggested) if suggested is not None else None,
        })


class ApprovalSettingsView(ApprovalTenantView):
    def _payload(self, config):
        # 已打开的旧版页面靠这两个字段显示候选人。
        users = [
            _as_name(account) | {"can_approve": True, "can_handle_refund": True}
            for account in active_accounts()
        ]
        return _configured_ids(config) | {"users": users}

    def get(self, request):
        if not has_node_now(request.user, "home.approval_configure"):
            raise PermissionDenied("没有配置审批的权限")
        return api_response(self._payload(ApprovalSettings.objects.filter(pk=1).first()))

    def patch(self, request):
        if not has_node_now(request.user, "home.approval_configure"):
            raise PermissionDenied("没有配置审批的权限")
        serializer = ApprovalSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            config = settings_for_update()
            old = _configured_ids(config)
            ids = old | serializer.validated_data
            cc_ids = set(ids["available_cc_ids"])
            if not set(ids["default_cc_ids"]).issubset(cc_ids):
                raise ValidationError("默认抄送人必须在可选抄送名单中")
            if cc_ids != set(active_accounts().filter(pk__in=cc_ids).values_list("pk", flat=True)):
                raise ValidationError("抄送名单包含已停用账号")
            approver_ids = set(ids["approver_ids"])
            if approver_ids != set(active_accounts().filter(pk__in=approver_ids).values_list("pk", flat=True)):
                raise ValidationError("罚款审批名单包含不可用的账号")
            handler_id = serializer.validated_data.get("refund_handler_id")
            if handler_id is not None and not active_accounts().filter(pk=handler_id).exists():
                raise ValidationError("请选择在用账号作为退款处理人")
            config.approvers.set(ids["approver_ids"])
            config.available_cc.set(ids["available_cc_ids"])
            config.default_cc.set(ids["default_cc_ids"])
            if "refund_handler_id" in serializer.validated_data:
                replace = config.refund_handler_id != handler_id
                if replace:
                    config.refund_handler_id = handler_id
                    config.save(update_fields=["refund_handler"])
                route_pending_refunds(handler_id, replace=replace)
            if old != _configured_ids(config):
                record(
                    log_type=ACTION_LOG_TYPES["system.approval_settings_update"],
                    action="system.approval_settings_update",
                    actor=request.user.get_username(), actor_id=request.user.pk,
                    target_kind="approval_settings", target_id=config.pk,
                )
        return api_response(self._payload(config))


class ApprovalDecisionView(ApprovalTenantView):
    def post(self, request, pk):
        serializer = ApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = decide_approval(pk, user=request.user, **serializer.validated_data)
        return api_response(_payload(row, request.user))


class ApprovalImageTicketView(ApprovalTenantView):
    def post(self, request):
        if not has_node_now(request.user, "home.approval_apply"):
            raise PermissionDenied("没有发起罚款的权限")
        serializer = ApprovalImageTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return api_response(issue_image_ticket(request.user, serializer.validated_data["ext"]))


class ApprovalImageUploadView(ApprovalTenantView):
    def put(self, request, ticket):
        if not has_node_now(request.user, "home.approval_apply"):
            raise PermissionDenied("没有发起罚款的权限")
        return upload_evidence(request, ticket)


class ApprovalImageReadView(ApprovalTenantView):
    def get(self, request, pk, index):
        return read_evidence(request.user, pk, index)
