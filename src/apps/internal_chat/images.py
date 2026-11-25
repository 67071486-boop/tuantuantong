import base64
from binascii import Error as Base64Error
from io import BytesIO

from PIL import Image
from rest_framework import serializers

from chat_service.image_limits import MAX_CHAT_IMAGE_BYTES as MAX_IMAGE_BYTES
from chat_service.image_limits import MAX_CHAT_IMAGE_PIXELS as MAX_IMAGE_PIXELS

MAX_ENCODED_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


class ImageInput(serializers.Serializer):
    mime = serializers.ChoiceField(choices=IMAGE_FORMATS)
    data = serializers.CharField(max_length=MAX_ENCODED_CHARS, trim_whitespace=False)

    def validate(self, attrs):
        mime, raw = attrs["mime"], attrs["data"]
        try:
            encoded = raw.encode("ascii")
            content = base64.b64decode(encoded, validate=True)
        except (Base64Error, UnicodeEncodeError) as exc:
            raise serializers.ValidationError("图片编码有误") from exc
        if len(content) > MAX_IMAGE_BYTES:
            raise serializers.ValidationError("图片不能超过 3 MiB")
        if base64.b64encode(content) != encoded:
            raise serializers.ValidationError("图片编码有误")
        try:
            with Image.open(BytesIO(content), formats=[IMAGE_FORMATS[mime]]) as image:
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise serializers.ValidationError("图片分辨率过大")
                if getattr(image, "is_animated", False):
                    raise serializers.ValidationError("只支持静态图片")
                image.load()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise serializers.ValidationError("仅支持可读取的 JPEG、PNG 和 WebP 图片") from exc
        return {"mime": mime, "data": content, "width": width, "height": height}


def image_fields(image):
    if image is None:
        return {}
    return {
        "image_metadata": {key: image[key] for key in ("mime", "width", "height")},
        "image_data": image["data"],
    }


def encode_image(metadata, data):
    return {"mime": metadata["mime"], "data": base64.b64encode(data).decode("ascii")}
