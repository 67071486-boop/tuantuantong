from rest_framework.exceptions import NotFound

from apps.business.api.screenshot_views import delete_order_screenshot
from apps.business.models import OrderScreenshot
from apps.core.envelope import api_response
from apps.openapi.api.integration_auth import IntegrationAppAuthentication, IntegrationView, integration_actor
from apps.openapi.api.integration_orders import _locked_order
from apps.openapi.api.order_extra_views import OrderImagesView, _paginated
from apps.openapi.api.order_image_views import OrderImageTicketView
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.idempotency import idempotent
from apps.openapi.serializers import image_row


class IntegrationOrderImagesView(OrderImagesView):
    authentication_classes = [IntegrationAppAuthentication]

    def get(self, request, order_no):
        self._require_order(order_no)
        images = OrderScreenshot.objects.filter(
            order__order_no=order_no,
            order__deleted_at__isnull=True,
            status=OrderScreenshot.STATUS_READY,
            img_type=OrderScreenshot.IMG_ORDER,
        ).select_related("uploaded_by").order_by("pk")
        return api_response(_paginated(images, request, lambda row: dict(image_row(row), id=row.pk)))

    check_upload_order = staticmethod(_locked_order)


class IntegrationOrderImageTicketView(OrderImageTicketView):
    authentication_classes = [IntegrationAppAuthentication]

    check_upload_order = staticmethod(_locked_order)


class IntegrationOrderImageDeleteView(IntegrationView):
    @idempotent(spec=OPENAPI_SPEC)
    def delete(self, request, order_no, image_id):
        order = _locked_order(order_no)
        image = OrderScreenshot.objects.select_for_update().filter(
            pk=image_id, order=order, img_type=OrderScreenshot.IMG_ORDER,
        ).first()
        if image is None:
            raise NotFound("进度图不存在")
        delete_order_screenshot(image, actor=integration_actor(request), after_commit=True)
        return api_response(None)
