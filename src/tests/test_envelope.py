from django.http import Http404
from django.test import SimpleTestCase, RequestFactory
from rest_framework import exceptions as drf_exc

from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.core.exceptions import envelope_exception_handler


# 用 SimpleTestCase 而非 TestCase：本文件是纯单元测试，不触碰数据库。
# TestCase 会声明 databases={"default"}，迫使 Django 建测试库并执行 migrate；
# 而本项目的 migrate 被 django-tenants 接管，需要 platform.Tenant（先建其行），
# 那行（`platform.Tenant` 的租户行）没建起来，测试库就根本建不起来。SimpleTestCase 的 databases 为空集，
# Django 会跳过测试库创建（输出 "Skipping setup of unused database(s): default."）。
class EnvelopeTests(SimpleTestCase):
    def test_success_envelope_uses_code_zero(self):
        response = api_response(data={"id": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"code": 0, "msg": "", "data": {"id": 1}})

    def test_paginated_shape_lives_inside_data(self):
        page = paginated([{"id": 1}], total=1, page=1, page_size=20)
        self.assertEqual(
            page, {"items": [{"id": 1}], "total": 1, "page": 1, "page_size": 20}
        )

    def test_validation_error_is_http_200_with_nonzero_code(self):
        exc = drf_exc.ValidationError({"name": ["该字段是必填项。"]})
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], ErrorCode.VALIDATION)
        self.assertIn("该字段是必填项。", response.data["msg"])

    def test_not_authenticated_keeps_http_401(self):
        exc = drf_exc.NotAuthenticated()
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], 401)

    def test_permission_denied_keeps_http_403(self):
        exc = drf_exc.PermissionDenied()
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], 403)

    def test_not_found_is_http_200_with_business_code(self):
        """业务资源不存在是业务结果，不是传输错误。"""
        exc = drf_exc.NotFound()
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], ErrorCode.BUSINESS)

    # 以下三条守住分层规则：传输/协议层错误**不得**被伪装成 HTTP 200，
    # 否则调用方无法区分「业务失败」与「请求本身有问题」。
    def test_malformed_request_keeps_http_400(self):
        exc = drf_exc.ParseError()
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], 400)

    def test_method_not_allowed_keeps_http_405(self):
        exc = drf_exc.MethodNotAllowed("GET")
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.data["code"], 405)

    def test_throttled_keeps_http_429(self):
        exc = drf_exc.Throttled()
        response = envelope_exception_handler(exc, {"request": RequestFactory().get("/")})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["code"], 429)

    def test_django_http404_is_business_not_found(self):
        """DRF 的 get_object_or_404 抛的是 Django 的 Http404，必须落到业务分支。

        否则真实的「资源不存在」会掉进兜底分支变成 404 + code=404，
        与 DRF 的 NotFound（200 + code=1002）形成两套客户端契约。
        """
        response = envelope_exception_handler(
            Http404("没找到"), {"request": RequestFactory().get("/")}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], ErrorCode.BUSINESS)

    def test_throttled_keeps_retry_after_header(self):
        """重建 Response 时不得丢掉 DRF 已算好的头。"""
        response = envelope_exception_handler(
            drf_exc.Throttled(wait=30), {"request": RequestFactory().get("/")}
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "30")
