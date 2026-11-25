"""DRF 异常 → 统一信封。分层规则（来自 docs/ARCHITECTURE.md §8 与 Global Constraints）：- 传输与认证层失败（401/403/5xx）：**保留 HTTP 状态码**，code 取同一数值。- 业务结果（资源不存在、校验失败）：
**HTTP 200**，code 取 ErrorCode。两处容易搞错、这里刻意处理的地方：1. DRF 的 `get_object_or_404` 抛的是 **Django 的 `Http404`**，不是 DRF 的 `NotFound`。
处理前必须先归一化，否则真正的「资源不存在」会掉进兜底分支变成 404 + code=404，与「业务结果一律 HTTP 200」冲突，同一情形出现两套客户端契约。2. DRF 在异常响应上可能已算好 `WWW-Authenticate` / `Retry-After` 等头，重建 Response 时必须转发，否则会被静默丢掉。"""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf_exc
from rest_framework.views import exception_handler as drf_exception_handler

from apps.core.app_version import AndroidUpdateRequired, android_update_response
from apps.core.envelope import api_response, error_response
from apps.core.error_codes import ErrorCode


class SplitGroupBusy(drf_exc.APIException):
    status_code = 409
    default_detail = '订单正在处理，请稍后重试'


def _first_message(detail):
    """把 DRF 的嵌套错误结构压成一句可读文本。"""
    if isinstance(detail, dict):
        for value in detail.values():
            message = _first_message(value)
            if message:
                return message
        return ""
    if isinstance(detail, (list, tuple)):
        for value in detail:
            message = _first_message(value)
            if message:
                return message
        return ""
    return str(detail)


def _envelope(code, msg, status, headers=None):
    # 委托共用构造函数：信封形状只允许有一处定义（spec D4）。
    return error_response(code, msg, status, headers)


def envelope_exception_handler(exc, context):
    if isinstance(exc, AndroidUpdateRequired):
        return android_update_response()

    # 归一化：DRF 自己的 exception_handler 内部也做同样的两步。
    # 不做这一步，get_object_or_404 / raise Http404 就走不到业务分支。
    if isinstance(exc, Http404):
        exc = drf_exc.NotFound(*exc.args)
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exc.PermissionDenied(*exc.args)

    response = drf_exception_handler(exc, context)
    if response is None:
        # 未处理的异常交给 Django（HTTP 500），不在这里吞掉。
        return None

    message = _first_message(getattr(exc, "detail", "")) or str(exc)
    # DRF 可能已算好 WWW-Authenticate / Retry-After 等头，重建时必须转发。
    headers = getattr(response, "headers", None)

    if isinstance(exc, SplitGroupBusy):
        return api_response(data={'retry_kind': 'split_group_busy'}, msg=message, code=409, status=409)

    if isinstance(exc, drf_exc.NotAuthenticated):
        return _envelope(401, message or "未认证", 401, headers)
    if isinstance(exc, drf_exc.AuthenticationFailed):
        return _envelope(401, message or "认证失败", 401, headers)
    if isinstance(exc, drf_exc.PermissionDenied):
        return _envelope(403, message or "无权限", 403, headers)
    if isinstance(exc, drf_exc.NotFound):
        # 不外传 DRF 的英文 detail（"No X matches the given query."）——
        # 本项目的 msg 是给中文界面直接显示的。
        return _envelope(int(ErrorCode.BUSINESS), "记录不存在或已被删除", 200, headers)
    if isinstance(exc, drf_exc.ValidationError):
        return _envelope(int(ErrorCode.VALIDATION), message, 200, headers)

    # 其余全部是传输/协议层语义：ParseError(400)、MethodNotAllowed(405)、UnsupportedMediaType(415)、Throttled(429)、未预期的 APIException(500)。
    # **保留 HTTP 状态码、code 取同一数值**——不得统一伪装成 200，否则调用方无法区分「业务失败」与「请求本身有问题」。
    status = response.status_code
    return _envelope(status, message, status, headers)
