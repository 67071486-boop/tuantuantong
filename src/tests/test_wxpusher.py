import json
import unittest
from io import StringIO
from unittest.mock import patch

import httpx
from django.core.management import CommandError, call_command

from apps.core.wxpusher import WxPusherClient, WxPusherError


class WxPusherClientTests(unittest.TestCase):
    def test_submits_a_text_notification_to_one_uid_over_https(self):
        def upstream(request):
            self.assertEqual(str(request.url), "https://wxpusher.zjiecode.com/api/send/message")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {
                "appToken": "AT_TEST_ONLY", "uids": ["UID_TEST_ONLY"],
                "content": "你有一条新消息。", "summary": "内部沟通提醒", "contentType": 1,
                "url": "https://studio.example.invalid/m/orders/00000000000000000001",
            })
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_TEST_ONLY", "code": 1000, "messageContentId": 42},
            ]})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            content_id = client.send(
                app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="你有一条新消息。",
                summary="内部沟通提醒", url="https://studio.example.invalid/m/orders/00000000000000000001",
            )

        self.assertEqual(content_id, 42)

    def test_marks_explicit_html_body_without_changing_the_top_level_link(self):
        html = '<p><a href="https://studio.example.invalid/m/mine">请前往查看</a></p>'

        def upstream(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["contentType"], 2)
            self.assertEqual(payload["content"], html)
            self.assertEqual(payload["url"], "https://studio.example.invalid/m/mine")
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_TEST_ONLY", "code": 1000, "messageContentId": 42},
            ]})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            self.assertEqual(client.send(
                app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content=html, html=True,
                url="https://studio.example.invalid/m/mine",
            ), 42)

    def test_sends_only_the_supported_daidaitong_links_as_original_url(self):
        urls = (
            "daidaitong://suqing.suqing.chat/m/wxpusher/open",
            "daidaitong://suqing.suqing.chat/m/orders/00000000000000000001",
        )

        def upstream(request):
            self.assertIn(json.loads(request.content)["url"], urls)
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_TEST_ONLY", "code": 1000, "messageContentId": 42},
            ]})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(client.send(
                        app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="通知", url=url,
                    ), 42)

    def test_rejects_failed_or_mismatched_recipient_even_after_outer_success(self):
        outcomes = [
            {"code": 1001, "data": None},
            {"code": 1000, "data": [{"uid": "UID_TEST_ONLY", "code": 1001, "messageContentId": 42}]},
            {"code": 1000, "data": [{"uid": "UID_WRONG", "code": 1000, "messageContentId": 42}]},
        ]
        for envelope in outcomes:
            with self.subTest(envelope=envelope):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))
                with WxPusherClient(transport=transport) as client, self.assertRaises(WxPusherError):
                    client.send(app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="通知")

    def test_batch_send_only_confirms_matching_unambiguous_uids(self):
        def upstream(request):
            self.assertEqual(str(request.url), "https://wxpusher.zjiecode.com/api/send/message")
            self.assertEqual(json.loads(request.content)["uids"], [
                "UID_MISSING", "UID_SENT", "UID_FAILED", "UID_DUPLICATED",
            ])
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_FAILED", "code": 1001, "messageContentId": 72},
                {"uid": "UID_SENT", "code": 1000, "messageContentId": 73},
                {"uid": "UID_DUPLICATED", "code": 1000, "messageContentId": 74},
                {"uid": "UID_DUPLICATED", "code": 1001, "messageContentId": 75},
                {"uid": "UID_UNREQUESTED", "code": 1000, "messageContentId": 76},
            ]})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            self.assertEqual(client.send_many(
                app_token="AT_TEST_ONLY",
                uids=["UID_MISSING", "UID_SENT", "UID_FAILED", "UID_DUPLICATED"],
                content="合成测试通知",
            ), {"UID_SENT": 73})

        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "code": 1001, "data": [{"uid": "UID_SENT", "code": 1000, "messageContentId": 73}],
        }))
        with WxPusherClient(transport=transport) as client, self.assertRaises(WxPusherError):
            client.send_many(app_token="AT_TEST_ONLY", uids=["UID_SENT"], content="合成测试通知")

    def test_does_not_repeat_or_redirect_a_send_request(self):
        request_count = 0

        def upstream(request):
            nonlocal request_count
            request_count += 1
            return httpx.Response(307, headers={"Location": "https://redirect.example.invalid"})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            with self.assertRaisesRegex(WxPusherError, "HTTP 307"):
                client.send(app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="通知")

        self.assertEqual(request_count, 1)

    def test_creates_a_short_lived_parameter_qrcode(self):
        def upstream(request):
            self.assertEqual(str(request.url), "https://wxpusher.zjiecode.com/api/fun/create/qrcode")
            self.assertEqual(request.method, "POST")
            self.assertEqual(json.loads(request.content), {
                "appToken": "AT_TEST_ONLY", "extra": "opaque-test-nonce", "validTime": 300,
            })
            return httpx.Response(200, json={"code": 1000, "data": {
                "code": "QR_TEST_ONLY", "url": "https://wxpusher.zjiecode.com/api/qrcode/QR_TEST_ONLY.jpg",
            }})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            result = client.create_qrcode(app_token="AT_TEST_ONLY", extra="opaque-test-nonce")

        self.assertEqual(result.code, "QR_TEST_ONLY")
        self.assertEqual(result.url, "https://wxpusher.zjiecode.com/api/qrcode/QR_TEST_ONLY.jpg")

    def test_retains_a_provider_hosted_subscription_link_without_exposing_extra(self):
        link = "https://wxpusher.zjiecode.com/api/s/ProviderLink"
        for returned, expected in (
            (link, link), (link + "?extra=visible", ""),
            ("https://wxpusher.zjiecode.com.evil.invalid/api/s/ProviderLink", ""),
        ):
            with self.subTest(returned=returned):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
                    "code": 1000, "data": {
                        "code": "QR_TEST_ONLY", "url": "https://wxpusher.zjiecode.com/api/qrcode/QR_TEST_ONLY.jpg",
                        "link": returned,
                    },
                }))
                with WxPusherClient(transport=transport) as client:
                    self.assertEqual(client.create_qrcode(app_token="AT_TEST_ONLY", extra="opaque-token").link, expected)

    def test_rejects_invalid_return_addresses_before_sending(self):
        transport = httpx.MockTransport(lambda request: self.fail("无效回跳地址不应发起请求"))
        with WxPusherClient(transport=transport) as client:
            for url in (
                "http://example.invalid", "https://example.invalid:65536/m",
                "https://example.invalid\\path/m", "https://%00/m",
                "intent://suqing.suqing.chat/m/wxpusher/open",
                "daidaitong://example.invalid/m/wxpusher/open",
                "daidaitong://suqing.suqing.chat/m/wxpusher/open?next=https://example.invalid",
                "daidaitong://suqing.suqing.chat/m/orders/00000000000000000001#fragment",
                "daidaitong://suqing.suqing.chat/m/orders/0000000000000000000",
                "daidaitong://suqing.suqing.chat/m/orders/%300000000000000000001",
            ):
                with self.subTest(url=url), self.assertRaisesRegex(WxPusherError, "HTTPS"):
                    client.send(app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="通知", url=url)

    def test_timeout_does_not_repeat_a_send(self):
        request_count = 0

        def upstream(request):
            nonlocal request_count
            request_count += 1
            raise httpx.ReadTimeout("测试超时", request=request)

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            with self.assertRaisesRegex(WxPusherError, "处理结果待确认"):
                client.send(app_token="AT_TEST_ONLY", uid="UID_TEST_ONLY", content="通知")
        self.assertEqual(request_count, 1)

    def test_create_qrcode_requires_success_before_returning_qrcode_fields(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "code": 1001, "data": {
                "code": "QR_TEST_ONLY", "url": "https://wxpusher.zjiecode.com/api/qrcode/QR_TEST_ONLY.jpg",
            },
        }))
        with WxPusherClient(transport=transport) as client, self.assertRaises(WxPusherError):
            client.create_qrcode(app_token="AT_TEST_ONLY", extra="opaque-test-nonce")

    def test_verifies_the_scanned_uid_against_the_app_subscription(self):
        def upstream(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, "wxpusher.zjiecode.com")
            if request.url.path == "/api/fun/scan-qrcode-uid":
                self.assertEqual(dict(request.url.params), {"code": "QR_TEST_ONLY"})
                return httpx.Response(200, json={"code": 1000, "data": "UID_TEST_ONLY"})
            self.assertEqual(request.url.path, "/api/fun/wxuser/v2")
            self.assertEqual(dict(request.url.params), {
                "appToken": "AT_TEST_ONLY", "uid": "UID_TEST_ONLY", "type": "0",
                "isBlock": "false", "page": "1", "pageSize": "100",
            })
            return httpx.Response(200, json={"code": 1000, "data": {"records": [
                {"uid": "UID_TEST_ONLY", "type": 0, "reject": False},
            ]}})

        with self.assertNoLogs("httpx", level="INFO"), WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            uid = client.verify_scanned_uid(app_token="AT_TEST_ONLY", code="QR_TEST_ONLY")

        self.assertEqual(uid, "UID_TEST_ONLY")

    def test_does_not_verify_blocked_topic_only_or_other_user_subscriptions(self):
        def upstream(request):
            if request.url.path == "/api/fun/scan-qrcode-uid":
                return httpx.Response(200, json={"code": 1000, "data": "UID_TEST_ONLY"})
            return httpx.Response(200, json={"code": 1000, "data": {"records": [
                {"uid": "UID_TEST_ONLY", "type": 0, "reject": True},
                {"uid": "UID_TEST_ONLY", "type": 1, "reject": False},
                {"uid": "UID_OTHER", "type": 0, "reject": False},
            ]}})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            self.assertIsNone(client.verify_scanned_uid(app_token="AT_TEST_ONLY", code="QR_TEST_ONLY"))

    def test_app_scope_rejects_subscription_to_a_different_app(self):
        def upstream(request):
            if request.url.path == "/api/fun/scan-qrcode-uid":
                return httpx.Response(200, json={"code": 1000, "data": "UID_TEST_ONLY"})
            return httpx.Response(200, json={"code": 1000, "data": {"records": [
                {"uid": "UID_TEST_ONLY", "type": 0, "reject": False, "appOrTopicId": 8123},
            ]}})

        with WxPusherClient(transport=httpx.MockTransport(upstream)) as client:
            self.assertIsNone(client.verify_scanned_uid(app_token="AT_TEST_ONLY", code="QR_TEST_ONLY", app_id="9999"))
            self.assertEqual(
                client.verify_scanned_uid(app_token="AT_TEST_ONLY", code="QR_TEST_ONLY", app_id="8123"),
                "UID_TEST_ONLY",
            )


class WxPusherCommandTests(unittest.TestCase):
    def test_command_submits_one_test_notice_given_an_explicit_recipient(self):
        def upstream(request):
            self.assertEqual(request.url.path, "/api/send/message")
            payload = json.loads(request.content)
            self.assertEqual(payload["appToken"], "AT_TEST_ONLY")
            self.assertEqual(payload["uids"], ["UID_TEST_ONLY"])
            self.assertEqual(payload["url"], "https://studio.example.invalid/m")
            return httpx.Response(200, json={"code": 1000, "data": [
                {"uid": "UID_TEST_ONLY", "code": 1000, "messageContentId": 42},
            ]})

        stdout = StringIO()
        with (
            patch.dict("os.environ", {"WXPUSHER_APP_TOKEN": "AT_TEST_ONLY"}),
            patch("apps.core.wxpusher.httpx.HTTPTransport", return_value=httpx.MockTransport(upstream)),
        ):
            call_command("wxpusher_test", uid="UID_TEST_ONLY", url="https://studio.example.invalid/m", stdout=stdout)

        self.assertIn("已受理", stdout.getvalue())
        self.assertIn("42", stdout.getvalue())
        self.assertNotIn("AT_TEST_ONLY", stdout.getvalue())

    def test_command_rejects_missing_credentials_without_network(self):
        with (
            patch.dict("os.environ", {"WXPUSHER_APP_TOKEN": ""}),
            patch("apps.core.wxpusher.httpx.HTTPTransport", side_effect=AssertionError("不应访问网络")),
            self.assertRaisesRegex(CommandError, "WXPUSHER_APP_TOKEN"),
        ):
            call_command("wxpusher_test", uid="UID_TEST_ONLY")

    def test_command_does_not_expose_the_upstream_error_body(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "code": 1001, "msg": "AT_TEST_ONLY 及上游正文", "data": None,
        }))
        with (
            patch.dict("os.environ", {"WXPUSHER_APP_TOKEN": "AT_TEST_ONLY"}),
            patch("apps.core.wxpusher.httpx.HTTPTransport", return_value=transport),
            self.assertRaises(CommandError) as raised,
        ):
            call_command("wxpusher_test", uid="UID_TEST_ONLY")
        self.assertNotIn("AT_TEST_ONLY", str(raised.exception))
        self.assertNotIn("上游正文", str(raised.exception))
