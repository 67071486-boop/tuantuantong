import base64
import binascii
import io

from PIL import Image

from .matrix import PilotError


def integer(value, minimum=1, maximum=2**53 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PilotError('标识格式错误')
    try:
        result = int(value)
    except ValueError:
        raise PilotError('标识格式错误') from None
    if not minimum <= result <= maximum:
        raise PilotError('标识超出范围')
    return result


def image_data(value):
    if not isinstance(value, dict) or value.get('mime') not in ('image/png', 'image/jpeg', 'image/webp'):
        raise PilotError('图片格式不支持')
    encoded = value.get('data')
    if not isinstance(encoded, str) or len(encoded) > 7_000_000:
        raise PilotError('图片过大或格式错误')
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format)
            if width * height > 20_000_000 or detected != value['mime']:
                raise ValueError
            image.verify()
    except (ValueError, OSError, SyntaxError, binascii.Error, Image.DecompressionBombError):
        raise PilotError('图片内容无效') from None
    return raw, value['mime'], width, height


def json_strings(value):
    pending, visited = [value], 0
    while pending:
        current = pending.pop()
        visited += 1
        if visited > 100000:
            raise PilotError('请求内容过于复杂')
        if isinstance(current, str):
            try:
                current.encode('utf-8')
            except UnicodeError:
                raise PilotError('请求含无效字符') from None
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
