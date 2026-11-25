"""使用本租户独立密钥保护付款账号资料。"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from rest_framework.exceptions import APIException


class AuthorizationDataUnavailable(APIException):
    status_code = 503
    default_detail = "付款账号资料暂不可用，请联系管理员"
    default_code = "authorization_data_unavailable"


def cipher():
    key = getattr(settings, "ALIPAY_AUTH_DATA_KEY", "")
    credential = getattr(settings, "ALIPAY_AUTH_CONTROL_CREDENTIAL", "")
    if not key or key == credential:
        raise AuthorizationDataUnavailable()
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError, UnicodeError):
        raise AuthorizationDataUnavailable() from None


def encrypt(value: str, box: Fernet) -> str:
    return box.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str, box: Fernet) -> str:
    try:
        return box.decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise AuthorizationDataUnavailable() from None
