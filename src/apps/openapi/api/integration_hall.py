from dataclasses import replace

from apps.business.models import EmpType, Order, Performance
from apps.business.order_writes import lock_order_row
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.openapi.api.integration_binding import IntegrationTakerAuthentication
from apps.openapi.api.integration_orders import local_orders, require_local_assignee, require_local_write
from apps.business.game_scope import game_scope, games_of
from apps.rbac.permissions import nodes_of
from apps.takerapi.api.hall_views import HallListView, TAKER_HALL_NODE, native_hall_rows
from apps.takerapi.api.order_images_views import TakerOrderImagesView, TakerOrderImageTicketView
from apps.takerapi.api.order_scope import TAKER_ORDER_NOT_FOUND_MSG
from apps.takerapi.api.order_status_views import TakerOrderStatusChangeView
from apps.takerapi.api.order_write_views import HallClaimView


class IntegrationHallView(HallListView):
    authentication_classes = [IntegrationTakerAuthentication]
    cache_scope = "hall/integration/v1"

    def rows_for_taker(self, taker, **filters):
        if TAKER_HALL_NODE not in nodes_of(taker):
            return []
        pairs = native_hall_rows(taker, native_scope=local_orders, **filters)
        return [payload for _, payload in sorted(pairs, key=lambda row: row[0], reverse=True)]


class IntegrationHallClaimView(HallClaimView):
    authentication_classes = [IntegrationTakerAuthentication]
    allow_mirror_claim = False
    sync_personal_orders = False
    sync_peer_orders = False

    def get_native_order(self, taker, order_no):
        return scope_queryset(taker, Order.objects.filter(order_no=order_no), scope_filter=game_scope("game_id")).first()

    def prepare_native_order(self, order, *, taker):
        order = lock_order_row(order)
        if order is None or not games_of(taker).filter(pk=order.game_id).exists():
            return None
        require_local_write(order)
        require_local_assignee(taker.pk)
        return order


def _lock_claimed_order(taker, order):
    if order is None:
        return None
    order = lock_order_row(order)
    if order is None or not Performance.objects.filter(order=order, emp_type=EmpType.MAIN, user=taker, deleted_at__isnull=True).exists():
        return None
    require_local_write(order)
    return order


class IntegrationTakerOrderStatusView(TakerOrderStatusChangeView):
    authentication_classes = [IntegrationTakerAuthentication]

    def write_context(self, app):
        return replace(
            super().write_context(app),
            allow_financial_writes=False, recheck_external_refunds=False,
            sync_personal_orders=False, sync_peer_orders=False,
        )

    def get_claimed_order(self, taker, order_no):
        return _lock_claimed_order(taker, super().get_claimed_order(taker, order_no))


class IntegrationTakerImageGuard:
    authentication_classes = [IntegrationTakerAuthentication]

    def _claimed_or_failure(self, request, order_no):
        order, failure = super()._claimed_or_failure(request, order_no)
        if order is not None and request.method == "POST":
            order = _lock_claimed_order(request.auth.taker, order)
            if order is None:
                return None, api_response(msg=TAKER_ORDER_NOT_FOUND_MSG, code=ErrorCode.BUSINESS)
        return order, failure


class IntegrationTakerImagesView(IntegrationTakerImageGuard, TakerOrderImagesView):
    pass


class IntegrationTakerImageTicketView(IntegrationTakerImageGuard, TakerOrderImageTicketView):
    pass
