from django.urls import path, register_converter

from apps.openapi.api.integration_changes import ChangeListView

from apps.openapi.api.integration_auth import IntegrationAppAuthentication
from apps.openapi.api.integration_binding import (
    IntegrationSubjectConverter, IntegrationTakerAuthentication, IntegrationTakerBindingView, IntegrationTakerTokenView,
)
from apps.openapi.api.integration_images import (
    IntegrationOrderImageDeleteView, IntegrationOrderImagesView, IntegrationOrderImageTicketView,
)
from apps.openapi.api.integration_hall import (
    IntegrationHallClaimView, IntegrationHallView, IntegrationTakerImageTicketView,
    IntegrationTakerImagesView, IntegrationTakerOrderStatusView,
)
from apps.takerapi.api.my_orders_views import MyOrderDetailView, MyOrdersListView
from apps.openapi.api.integration_catalog import IntegrationOrderSourceListView, IntegrationServerListView
from apps.openapi.api.integration_takers import OrderAssigneeListView, TakerDetailView, TakerLifecycleView, TakerListView
from apps.openapi.api.order_extra_views import GameListView, ProjectListView
from apps.openapi.api.integration_orders import (
    IntegrationOrderActionView,
    IntegrationOrderAssignmentView,
    IntegrationOrderAuditView,
    IntegrationOrderDetailView,
    IntegrationOrderListView,
    IntegrationOrderNoteView,
    IntegrationOrderPriceView,
    IntegrationOrderRecycleView,
    IntegrationOrderStatusView,
    IntegrationOrderSplitView,
    IntegrationPricedOrderSplitView,
)

register_converter(IntegrationSubjectConverter, "sdk_subject")

app_name = "business_integration"

urlpatterns = [
    path("changes/", ChangeListView.as_view(), name="integration-changes"),
    path("orders/", IntegrationOrderListView.as_view(), name="integration-orders"),
    path("orders/assignees/", OrderAssigneeListView.as_view(), name="integration-order-assignees"),
    path("orders/recycle-bin/", IntegrationOrderRecycleView.as_view(), name="integration-order-recycle-bin"),
    path("orders/<order_no:order_no>/", IntegrationOrderDetailView.as_view(), name="integration-order-detail"),
    path("orders/<order_no:order_no>/status/", IntegrationOrderStatusView.as_view(), name="integration-order-status"),
    path("orders/<order_no:order_no>/assign-price/", IntegrationOrderPriceView.as_view(), name="integration-order-price"),
    path("orders/<order_no:order_no>/assign/", IntegrationOrderAssignmentView.as_view(), name="integration-order-assign"),
    path("orders/<order_no:order_no>/reassign/", IntegrationOrderAssignmentView.as_view(replace_taker=True), name="integration-order-reassign"),
    path("orders/<order_no:order_no>/publish/", IntegrationOrderActionView.as_view(action="publish"), name="integration-order-publish"),
    path("orders/<order_no:order_no>/withdraw/", IntegrationOrderActionView.as_view(action="withdraw"), name="integration-order-withdraw"),
    path("orders/<order_no:order_no>/clear-taker/", IntegrationOrderActionView.as_view(action="clear"), name="integration-order-clear-taker"),
    path("orders/<order_no:order_no>/delete/", IntegrationOrderActionView.as_view(action="delete"), name="integration-order-delete"),
    path("orders/<order_no:order_no>/restore/", IntegrationOrderActionView.as_view(action="restore"), name="integration-order-restore"),
    path("orders/<order_no:order_no>/audit/", IntegrationOrderAuditView.as_view(), name="integration-order-audit"),
    path("orders/<order_no:order_no>/notes/", IntegrationOrderNoteView.as_view(), name="integration-order-notes"),
    path("orders/<order_no:order_no>/split/", IntegrationOrderSplitView.as_view(), name="integration-order-split"),
    path("orders/<order_no:order_no>/split-peer/", IntegrationPricedOrderSplitView.as_view(), name="integration-order-split-peer"),
    path("orders/<order_no:order_no>/images/", IntegrationOrderImagesView.as_view(), name="integration-order-images"),
    path("orders/<order_no:order_no>/images/ticket/", IntegrationOrderImageTicketView.as_view(), name="integration-order-image-ticket"),
    path("orders/<order_no:order_no>/images/<int:image_id>/", IntegrationOrderImageDeleteView.as_view(), name="integration-order-image-delete"),
    path("takers/", TakerListView.as_view(), name="integration-takers"),
    path("takers/<int:taker_id>/", TakerDetailView.as_view(), name="integration-taker-detail"),
    path("takers/<int:taker_id>/enable/", TakerLifecycleView.as_view(action="enable"), name="integration-taker-enable"),
    path("takers/<int:taker_id>/disable/", TakerLifecycleView.as_view(action="disable"), name="integration-taker-disable"),
    path("takers/<int:taker_id>/delete/", TakerLifecycleView.as_view(action="delete"), name="integration-taker-delete"),
    path("takers/<int:taker_id>/restore/", TakerLifecycleView.as_view(action="restore"), name="integration-taker-restore"),
    path("catalog/games/", GameListView.as_view(authentication_classes=[IntegrationAppAuthentication]), name="integration-catalog-games"),
    path("catalog/projects/", ProjectListView.as_view(authentication_classes=[IntegrationAppAuthentication]), name="integration-catalog-projects"),
    path("catalog/servers/", IntegrationServerListView.as_view(), name="integration-catalog-servers"),
    path("catalog/order-sources/", IntegrationOrderSourceListView.as_view(), name="integration-catalog-order-sources"),
    path("taker-bindings/<sdk_subject:external_user_id>/", IntegrationTakerBindingView.as_view(), name="integration-taker-binding"),
    path("taker-token/", IntegrationTakerTokenView.as_view(), name="integration-taker-token"),
    path("taker/hall/", IntegrationHallView.as_view(), name="integration-taker-hall"),
    path("taker/hall/<order_no:order_no>/claim/", IntegrationHallClaimView.as_view(), name="integration-taker-claim"),
    path("taker/my/orders/", MyOrdersListView.as_view(authentication_classes=[IntegrationTakerAuthentication]), name="integration-taker-orders"),
    path("taker/orders/<order_no:order_no>/", MyOrderDetailView.as_view(authentication_classes=[IntegrationTakerAuthentication]), name="integration-taker-order-detail"),
    path("taker/orders/<order_no:order_no>/status/", IntegrationTakerOrderStatusView.as_view(), name="integration-taker-order-status"),
    path("taker/orders/<order_no:order_no>/images/", IntegrationTakerImagesView.as_view(), name="integration-taker-order-images"),
    path("taker/orders/<order_no:order_no>/images/ticket/", IntegrationTakerImageTicketView.as_view(), name="integration-taker-order-image-ticket"),
]
