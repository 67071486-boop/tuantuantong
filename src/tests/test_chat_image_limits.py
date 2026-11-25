from base64 import b64encode
from io import BytesIO

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from PIL import Image

from apps.core.middleware import NonDirectUploadBodyLimitMiddleware
from apps.internal_chat.serializers import MessageSend
from apps.internal_chat.visitor_views import VisitorMessageSend
from chat_service.config import ServiceError
from chat_service.core import Core


def png(width, height):
    output = BytesIO()
    Image.new("RGB", (width, height), "green").save(output, "PNG", compress_level=0)
    return {"mime": "image/png", "data": b64encode(output.getvalue()).decode("ascii")}


class ChatImageLimitsTests(SimpleTestCase):
    def test_all_chat_message_validators_accept_image_between_one_and_three_mib(self):
        image = png(980, 900)
        for schemas in (MessageSend, VisitorMessageSend):
            message = schemas(data={"client_id": "large-image", "image": image})
            self.assertTrue(message.is_valid(), message.errors)
        self.assertEqual(Core._image_input(image)["width"], 980)

    def test_message_validation_keeps_single_and_album_caps(self):
        over_three_mib = png(1100, 1100)
        for schemas in (MessageSend, VisitorMessageSend):
            message = schemas(data={"client_id": "too-large", "image": over_three_mib})
            self.assertFalse(message.is_valid())
            photos = schemas(data={"client_id": "album", "images": [png(980, 900)] * 2})
            self.assertTrue(photos.is_valid(), photos.errors)
            photos = schemas(data={"client_id": "album-heavy", "images": [png(1000, 1000)] * 2})
            self.assertFalse(photos.is_valid())
        with self.assertRaises(ServiceError):
            Core._image_input(over_three_mib)

    def test_larger_body_cap_is_scoped_to_json_chat_message_posts(self):
        upload = "/api/internal-chat/visitor/guest/conversations/123/messages/"
        wall = NonDirectUploadBodyLimitMiddleware(lambda _: HttpResponse(status=204))
        factory = RequestFactory()
        large = 6 * 1024 * 1024
        for path in (upload, upload.replace("/guest/", "/desk/"), "/api/internal-chat/conversations/123/messages/",
                     "/api/internal-chat/orders/00000000000000000123/messages/",
                     "/api/internal-chat/app/notification-conversations/123/messages/"):
            with self.subTest(path=path):
                request = factory.post(path, data="{}", content_type="application/json", CONTENT_LENGTH=large)
                self.assertEqual(wall(request).status_code, 204)
        overflow = factory.post(upload, data="{}", content_type="application/json", CONTENT_LENGTH=8 * 1024 * 1024)
        self.assertEqual(wall(overflow).status_code, 413)
        non_chat = factory.post("/api/auth/login", data="{}", content_type="application/json", CONTENT_LENGTH=large)
        self.assertEqual(wall(non_chat).status_code, 400)
        wrong_method = factory.generic("PUT", upload, data="{}", content_type="application/json", CONTENT_LENGTH=large)
        self.assertEqual(wall(wrong_method).status_code, 400)

    def test_chat_peer_delivery_has_body_budget_only_on_tokened_routes(self):
        factory = RequestFactory()
        wall = NonDirectUploadBodyLimitMiddleware(lambda _: HttpResponse(status=204))
        for path, credential in (
            ("/api/internal-chat/service/peer-chat/", {"HTTP_X_TTT_CHAT_SIGNATURE": "a" * 64}),
            ("/api/business/peer-sync/inbound/events/", {"HTTP_AUTHORIZATION": "Bearer binding-token"}),
        ):
            with self.subTest(path=path):
                params = {"content_type": "application/json", "CONTENT_LENGTH": 8 * 1024 * 1024}
                self.assertEqual(wall(factory.post(path, "{}", **params, **credential)).status_code, 204)
                self.assertEqual(wall(factory.post(path, "{}", **params)).status_code, 400)
