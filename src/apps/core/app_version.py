import re

from django.conf import settings
from django.utils.cache import add_never_cache_headers
from rest_framework.exceptions import APIException

from apps.core.envelope import api_response

_LEGACY_NATIVE_AGENT = re.compile(r"uni-app-x/[0-9]+(?:\.[0-9]+)+")
_VERSION_CODE = re.compile(r"[0-9]{1,10}")
_DOWNLOAD_URL = "https://suqing.suqing.chat/download/"


class AndroidUpdateRequired(APIException):
    status_code = 426
    default_detail = "当前版本已停止服务，请到设置的检查更新安装最新版后继续使用。"


def android_version_code(request) -> int | None:
    """0 代表可识别的原生旧版，None 代表其他入口。"""
    platform = request.headers.get("X-TT-App-Platform", "").lower()
    if platform == "ios":
        return None
    if platform != "android" and not _LEGACY_NATIVE_AGENT.fullmatch(request.headers.get("User-Agent", "")):
        return None
    value = request.headers.get("X-TT-App-Version-Code", "")
    if _VERSION_CODE.fullmatch(value):
        version = int(value)
        if version <= 2**31 - 1:
            return version
    return 0


def requires_android_update(version: int | None) -> bool:
    return version is not None and version < settings.APP_ANDROID_MIN_VERSION_CODE


def android_update_response():
    status = AndroidUpdateRequired.status_code
    response = api_response(
        {"minimum_version_code": settings.APP_ANDROID_MIN_VERSION_CODE, "download_url": _DOWNLOAD_URL},
        msg=AndroidUpdateRequired.default_detail, code=status, status=status,
    )
    add_never_cache_headers(response)
    return response
