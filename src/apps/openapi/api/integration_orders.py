from copy import copy
from dataclasses import replace

from django.db.models import Q
from django.db.models.functions import Trim
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu_member, peer_dispatch
from apps.business.api.serializers import (
    OrderBalanceUpdateSerializer,
    OrderNoteSerializer,
)
from apps.business.models import (
    DailiantongPush, EmpType, JidanpuSyncEvent, Order, OrderStatus,
    PeerDownstreamBinding, PeerSyncEvent, Performance,
)
from apps.business.order_intake import resolve_assign_taker
from apps.business.order_time_fields import mark_published
from apps.business.order_writes import (
    assign_order, change_hall_price, change_price, delete_order_write, lock_order_row,
    transition_order, update_order_fields,
)
from apps.business.peer_sync import HALL_PULL_KIND, HALL_THROW_KIND
from apps.business.recycle import restore_order
from apps.business.validation import ValidationProblem
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.integration_auth import IntegrationAppAuthentication, IntegrationView, integration_actor
from apps.openapi.api.order_create_views import CREATE_FIELDS, _intake_actor
from apps.openapi.api.order_extra_views import OrderStatusView, _paginated
from apps.openapi.api.order_query_views import OrderSourceQueryView
from apps.openapi.api.order_write_views import OrderAssignPriceSerializer, OrderStatusChangeSerializer, _no_game_admission, _write_context
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.idempotency import idempotent
from apps.openapi.serializers import UpdatedSinceField, order_payload


class OrderListParams(serializers.Serializer):
    source_order_no = serializers.CharField(required=False, max_length=128)
    status = serializers.ChoiceField(choices=OrderStatus.choices, required=False)
    game_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807, required=False)
    taker_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807, required=False)
    source = serializers.CharField(max_length=128, required=False)
    created_from = UpdatedSinceField(required=False)
    created_to = UpdatedSinceField(required=False)
    updated_since = UpdatedSinceField(required=False)
    after = serializers.RegexField(r"^([0-9]{20})?$", required=False, allow_blank=True)


class TakerAssignment(serializers.Serializer):
    taker_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)


UPDATE_FIELDS = frozenset({
    "game", "server", "category", "project", "source", "source_order_no", "project_text",
    "content", "remark", "demand", "time_requirement", "game_account", "game_sub_account",
    "game_role_name", "current_info_1", "current_info_2", "customer_phone", "customer_qq",
    "customer_ww", "order_type", "is_urgent", "tag", "total_price",
})


def integration_order_payload(order):
    data = order_payload(order)
    data.pop("costs")
    for field in ("received_price", "platform_price", "refund_fee", "fee"):
        data["amounts"].pop(field)
    for field in ("payment_method", "payment_status"):
        data["order"].pop(field)
    data["submitted_at"] = order.submitted_at.isoformat() if order.submitted_at else None
    data["published_at"] = order.published_at.isoformat() if order.published_at else None
    data["game_id"] = order.game_id
    data["server_id"] = order.server_id
    data["project_id"] = order.project_id
    main = next((p for p in order.performances.all() if p.emp_type == EmpType.MAIN and p.deleted_at is None), None)
    data["taker_id"] = main.user_id if main else None
    data["deleted_at"] = order.deleted_at.isoformat() if order.deleted_at else None
    return data


def write_context(request):
    actor = integration_actor(request)
    return replace(
        _write_context(request.auth), actor=actor, log_actor=actor,
        recheck_external_refunds=False, allow_financial_writes=False,
        sync_personal_orders=False, sync_peer_orders=False, assert_game=_no_game_admission,
    )


def local_orders(queryset):
    pushed = DailiantongPush.objects.filter(is_active=True, state__in=DailiantongPush.ACTIVE_STATES)
    downstream_tasks = JidanpuSyncEvent.objects.filter(
        direction=JidanpuSyncEvent.Direction.OUT, target_type="order",
        kind__in=(jidanpu_member.KIND_LAUNCH, jidanpu_member.KIND_ASSIGN),
        status__in=(JidanpuSyncEvent.Status.PENDING, JidanpuSyncEvent.Status.DONE, JidanpuSyncEvent.Status.FAILED),
    )
    peer_refs = PeerSyncEvent.objects.filter(binding_role="downstream").filter(
        Q(direction="in", kind=HALL_PULL_KIND, status=PeerSyncEvent.Status.DONE, reason="")
        | Q(direction="out", kind__in=(HALL_THROW_KIND, "order.assign", "order.grab")) & (
            Q(status__in=(PeerSyncEvent.Status.PENDING, PeerSyncEvent.Status.FAILED))
            | Q(status=PeerSyncEvent.Status.DONE, reason="")
        ),
    ).values("target_ref")
    peer_owner_ids = PeerDownstreamBinding.objects.filter(state=PeerDownstreamBinding.State.ACTIVE).values("account_id")
    peer_assigned = Performance.objects.filter(
        emp_type=EmpType.MAIN, deleted_at__isnull=True, user_id__in=peer_owner_ids,
    ).values("order_id")
    if peer_dispatch._hall_targets():
        queryset = queryset.exclude(status=OrderStatus.WAITING_GRAB)
    return queryset.filter(hall_card__isnull=True, upstream_hall_card__isnull=True).alias(
        sdk_peer_source=Trim("source_order_no"),
    ).exclude(pk__in=peer_assigned).exclude(order_no__in=peer_refs).exclude(sdk_peer_source__in=peer_refs).exclude(
        pk__in=pushed.values("order_id"),
    ).exclude(order_no__in=downstream_tasks.values("target_ref")).exclude(
        peer_links__isnull=False,
    ).exclude(jidanpu_upstream_links__isnull=False).exclude(jidanpu_member_links__isnull=False)


def require_local_assignee(taker_id):
    has_peer = PeerDownstreamBinding.objects.filter(
        account_id=taker_id, state=PeerDownstreamBinding.State.ACTIVE,
    ).exists()
    if has_peer or jidanpu_member._assign_target_of(taker_id) is not None:
        raise ValidationError("打手接手会同步到外部平台，请到团团通原入口处理")


def require_local_write(order):
    if not local_orders(Order.all_objects.filter(pk=order.pk)).exists():
        raise ValidationError("订单有关联的外部平台业务，请到团团通原入口处理")


def _locked_order(order_no, *, include_deleted=False, lock_group=False):
    manager = Order.all_objects if include_deleted else Order.objects
    order = manager.filter(order_no=order_no).first()
    if order is None:
        raise NotFound()
    if lock_group and order.split_id:
        rows = list(manager.filter(split_id=order.split_id).order_by("id").select_for_update(of=("self",)))
        selected = next((row for row in rows if row.pk == order.pk), None)
        if selected is None:
            raise ValidationError("拆分组成员已变化，请刷新后重试")
        _check_split_local(rows)
        return selected
    locked = (
        manager.select_for_update(of=("self",)).filter(pk=order.pk).first()
        if include_deleted else lock_order_row(order)
    )
    if locked is None:
        raise NotFound()
    if lock_group and locked.split_id != order.split_id:
        raise ValidationError("拆分组成员已变化，请刷新后重试")
    require_local_write(locked)
    return locked


def _audit(request, order, action, **columns):
    record(
        log_type=ACTION_LOG_TYPES[action], action=action,
        actor=integration_actor(request), system=True,
        target_kind="order", target_id=order.order_no, order_no=order.order_no,
        source_order_no=order.source_order_no, **columns,
    )


def _outcome(outcome, order):
    if not outcome.ok:
        return api_response(msg=outcome.msg, code=outcome.code)
    order.refresh_from_db()
    return api_response(integration_order_payload(order))


class IntegrationOrderListView(OrderSourceQueryView):
    authentication_classes = [IntegrationAppAuthentication]
    allowed_create_fields = (CREATE_FIELDS - {"assignee", "throw_to_hall"}) | (UPDATE_FIELDS - {"tag"})
    create_payload_for_order = staticmethod(integration_order_payload)

    def intake_actor(self, request):
        ctx = write_context(request)
        return replace(_intake_actor(request.auth), log_actor=ctx.log_actor, write_ctx=ctx)

    def get(self, request):
        form = OrderListParams(data=request.query_params)
        form.is_valid(raise_exception=True)
        filters = dict(form.validated_data)
        taker_id = filters.pop("taker_id", None)
        after = filters.pop("after", None)
        for source, target in (
            ("created_from", "created_at__gte"), ("created_to", "created_at__lte"),
            ("updated_since", "updated_at__gte"),
        ):
            if source in filters:
                filters[target] = filters.pop(source)
        orders = self._orders_queryset().filter(**filters)
        if taker_id is not None:
            assigned_orders = Performance.objects.filter(
                user_id=taker_id, emp_type=EmpType.MAIN, deleted_at__isnull=True,
            ).values("order_id")
            orders = orders.filter(pk__in=assigned_orders)
        if after is not None:
            orders = orders.filter(order_no__gt=after)
            order_by = ("order_no",)
        else:
            order_by = ("updated_at", "id") if "updated_at__gte" in filters else ("-created_at", "-id")
        return api_response(_paginated(orders.order_by(*order_by), request, integration_order_payload))


class IntegrationOrderDetailView(IntegrationView):
    def get(self, request, order_no):
        order = OrderSourceQueryView._orders_queryset().filter(order_no=order_no).first()
        if order is None:
            raise NotFound()
        return api_response(integration_order_payload(order))

    @idempotent(spec=OPENAPI_SPEC)
    def patch(self, request, order_no):
        if not isinstance(request.data, dict) or request.data.keys() - UPDATE_FIELDS:
            raise ValidationError("请求体含有不支持修改的订单字段")
        order = _locked_order(order_no, lock_group="total_price" in request.data)
        ctx = write_context(request)
        form = OrderBalanceUpdateSerializer(order, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        values = dict(form.validated_data)
        price = values.pop("total_price", None)
        if values:
            try:
                outcome = update_order_fields(
                    order, validated=values, request_user=None, ctx=ctx,
                    assert_game=_no_game_admission, dry_run=False,
                )
            except ValidationProblem as exc:
                return api_response(msg=str(exc), code=exc.code)
            if not outcome.ok:
                return api_response(msg=outcome.msg, code=outcome.code)
            order = outcome.detail["order"]
        if price is not None:
            outcome = change_price(order, total_price=price, ctx=ctx, dry_run=False, assert_game=_no_game_admission)
            return _outcome(outcome, order)
        return api_response(integration_order_payload(order))


class IntegrationOrderStatusView(OrderStatusView):
    authentication_classes = [IntegrationAppAuthentication]

    def get(self, request, order_no):
        response = super().get(request, order_no)
        order = self._require_order(order_no)
        response.data["data"]["submitted_at"] = order.submitted_at.isoformat() if order.submitted_at else None
        return response

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        form = OrderStatusChangeSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        status = form.validated_data["status"]
        if status in {OrderStatus.WAITING_GRAB, OrderStatus.UNASSIGNED, OrderStatus.IN_PROGRESS}:
            raise ValidationError("请用发大厅、撤回、指派或清除接手人接口")
        order = _locked_order(order_no)
        return _outcome(transition_order(
            order, target=status, remark=form.validated_data["remark"],
            ctx=write_context(request), dry_run=False,
        ), order)


class IntegrationOrderPriceView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def put(self, request, order_no):
        form = OrderAssignPriceSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        order = _locked_order(order_no)
        return _outcome(change_hall_price(order, **form.validated_data, ctx=write_context(request), dry_run=False), order)


class IntegrationOrderAssignmentView(IntegrationView):
    replace_taker = False

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        from apps.openapi.api.integration_takers import integration_takers

        form = TakerAssignment(data=request.data)
        form.is_valid(raise_exception=True)
        order = _locked_order(order_no)
        has_taker = Performance.objects.filter(order=order, emp_type=EmpType.MAIN, user__isnull=False, deleted_at__isnull=True).exists()
        if has_taker != self.replace_taker:
            return api_response(msg="已有接手人，请使用改派" if has_taker else "该订单尚无人接手", code=ErrorCode.BUSINESS)
        taker_id = form.validated_data["taker_id"]
        if not integration_takers().filter(pk=taker_id).exists():
            raise NotFound("打手不存在")
        require_local_assignee(taker_id)
        taker, error = resolve_assign_taker(taker_id)
        if error is not None:
            return error
        return _outcome(assign_order(
            order, taker=taker, price=form.validated_data.get("price"),
            ctx=write_context(request), dry_run=False,
        ), order)


class IntegrationOrderActionView(IntegrationView):
    action = ""

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        recycle_action = self.action in {"delete", "restore"}
        order = _locked_order(order_no, include_deleted=recycle_action, lock_group=recycle_action)
        ctx = write_context(request)
        if self.action == "delete":
            if request.data not in ({}, {"isDelEmp": "N"}):
                raise ValidationError("SDK 删除订单时保留关联绩效")
            return _outcome(delete_order_write(
                order, is_del_emp="N", ctx=ctx, dry_run=False,
            ), order)
        if self.action == "restore":
            restore_order(order=order, actor_name=ctx.log_actor)
            order.refresh_from_db()
            return api_response(integration_order_payload(order))
        if self.action == "withdraw" and order.status != OrderStatus.WAITING_GRAB:
            return api_response(msg="只有待抢单可以撤回", code=ErrorCode.BUSINESS)
        target = OrderStatus.WAITING_GRAB if self.action == "publish" else OrderStatus.UNASSIGNED
        if self.action == "publish":
            published = copy(order)
            published.status = OrderStatus.WAITING_GRAB
            if peer_dispatch._hall_targets() or jidanpu_member.hall_launch_targets(published):
                raise ValidationError("发大厅会同步到外部平台，请到团团通原入口处理")
        outcome = transition_order(
            order, target=target, ctx=ctx, dry_run=False, allow_taker_release=self.action == "clear",
        )
        if not outcome.ok:
            return api_response(msg=outcome.msg, code=outcome.code)
        if self.action == "publish":
            mark_published(order)
            _audit(request, order, "order.throw_to_hall", to_value=order.hall_price)
        else:
            _audit(request, order, "order.withdraw" if self.action == "withdraw" else "order.clear_taker")
        return _outcome(outcome, order)


class IntegrationOrderRecycleView(IntegrationView):
    def get(self, request):
        orders = Order.all_objects.filter(
            deleted_at__isnull=False, hall_card__isnull=True, upstream_hall_card__isnull=True,
        ).select_related("game", "project", "server", "category").prefetch_related(
            "performances__user",
        ).order_by("-deleted_at", "-id")
        return api_response(_paginated(orders, request, integration_order_payload))


class IntegrationOrderAuditView(IntegrationView):
    def get(self, request, order_no):
        from apps.audit.models import OperationLog

        if not OrderSourceQueryView._orders_queryset().filter(order_no=order_no).exists():
            raise NotFound()
        rows = OperationLog.objects.filter(order_no=order_no, action__in={
            "order.create", "order.update", "order.transition", "order.assign", "order.claim",
            "order.throw_to_hall", "order.withdraw", "order.clear_taker", "order.note",
            "order.change_taker_price", "order.change_offer_price", "order.delete", "order.restore",
            "order.delete_screenshot", "performance.submit",
        }).order_by("-created_at", "-id")
        return api_response(_paginated(rows, request, lambda row: {
            "id": row.pk, "action": row.action, "actor": row.actor,
            "created_at": row.created_at.isoformat(), "reason": row.reason,
        }))


class IntegrationOrderNoteView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        order = _locked_order(order_no)
        form = OrderNoteSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        _audit(request, order, "order.note", reason=form.validated_data["content"])
        return api_response({"order_no": order_no})


class IntegrationOrderSplitView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        from apps.business.api.order_views import OrderViewSet, SplitOrderSerializer

        form = SplitOrderSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        order = OrderSourceQueryView._orders_queryset().filter(order_no=order_no).first()
        if order is None:
            raise NotFound()
        return OrderViewSet.split_local_order(
            order, projects=form.validated_data["projects"], operator=integration_actor(request),
            actor_id=None, assert_game=_no_game_admission, check_orders=_check_split_local,
        )


def _check_split_local(orders):
    for order in orders:
        require_local_write(order)


class IntegrationPricedOrderSplitView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        from apps.business.api.order_views import OrderViewSet, PeerSplitOrderSerializer

        form = PeerSplitOrderSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        order = OrderSourceQueryView._orders_queryset().filter(order_no=order_no).first()
        if order is None:
            raise NotFound()
        return OrderViewSet.split_priced_order(
            order, values=form.validated_data, price_context=write_context(request),
            assert_game=_no_game_admission, may_edit_price=lambda: True,
            check_orders=_check_split_local,
        )
