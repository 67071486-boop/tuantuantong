"""腾讯云短信后端（`apps/core/sms.py` 的 `tencent` 分支，评审 Major 6）。

## 为什么必须有这组用例

这是**外部写接口**：`htpx.post` 不抛异常 ≠ 发短信成功（E-0087 同族）。腾讯云的失败
有两种形状——顶层 `Response.Error` 与 `SendStatusSet[0].Code != "Ok"`——两种都必须被
判成失败。本文件把三种形状都钉住，并验证签名头真的算出来了。

⛔ 不真调腾讯云：全部 mock `httpx.post`。真实联调等凭据到位后单独做（不进 CI）。
"""
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.core import sms

CONF = {
    "secret_id": "AKIDtest",
    "secret_key": "secret-test",
    "sdk_app_id": "1400000000",
    "sign_name": "代代通",
    "template_id": "2597486",
    "region": "ap-guangzhou",
}


def _response(payload, status=200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


@override_settings(SMS_BACKEND="tencent", SMS_TENCENT=CONF, DEBUG=False)
class TencentSmsTests(SimpleTestCase):
    @mock.patch("apps.core.sms.httpx.post")
    def test_success_builds_a_signed_request(self, post):
        post.return_value = _response(
            {"Response": {"SendStatusSet": [{"Code": "Ok"}], "RequestId": "r-1"}}
        )
        sms.send_verification_code("13800001111", "123456")

        kwargs = post.call_args.kwargs
        headers = kwargs["headers"]
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDtest/"))
        self.assertEqual(headers["X-TC-Action"], "SendSms")
        self.assertEqual(headers["X-TC-Version"], "2021-01-11")
        body = json.loads(kwargs["content"])
        self.assertEqual(body["PhoneNumberSet"], ["+8613800001111"])
        self.assertEqual(body["SmsSdkAppId"], "1400000000")
        self.assertEqual(body["SignName"], "代代通")
        self.assertEqual(body["TemplateId"], "2597486")
        self.assertEqual(body["TemplateParamSet"], ["123456"])

    @mock.patch("apps.core.sms.httpx.post")
    def test_top_level_error_is_a_failure(self, post):
        post.return_value = _response(
            {"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad sign"}}}
        )
        with self.assertRaises(RuntimeError) as ctx:
            sms.send_verification_code("13800001111", "123456")
        self.assertIn("AuthFailure.SignatureFailure", str(ctx.exception))

    @mock.patch("apps.core.sms.httpx.post")
    def test_non_ok_send_status_is_a_failure(self, post):
        """HTTP 200 + `Code != Ok` 也是失败（E-0087）。"""
        post.return_value = _response(
            {
                "Response": {
                    "SendStatusSet": [
                        {"Code": "FailedOperation.TemplateParamSetNotMatchApprovedTemplate"}
                    ],
                    "RequestId": "r-2",
                }
            }
        )
        with self.assertRaises(RuntimeError) as ctx:
            sms.send_verification_code("13800001111", "123456")
        self.assertIn("TemplateParamSetNotMatchApprovedTemplate", str(ctx.exception))

    @mock.patch("apps.core.sms.httpx.post")
    def test_network_error_is_a_failure(self, post):
        import httpx

        post.side_effect = httpx.ConnectTimeout("timeout")
        with self.assertRaises(RuntimeError):
            sms.send_verification_code("13800001111", "123456")

    def test_missing_config_is_loud(self):
        from django.core.exceptions import ImproperlyConfigured

        with override_settings(SMS_TENCENT={**CONF, "secret_key": ""}):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                sms.send_verification_code("13800001111", "123456")
        self.assertIn("secret_key", str(ctx.exception))
