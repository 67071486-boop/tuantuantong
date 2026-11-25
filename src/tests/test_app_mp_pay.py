"""微信小程序支付（Jeepay `WX_LITE`，2026-10-21）。

## 钉住什么

1. **下单**：`way=WX_LITE` 时发给 Jeepay 的 `appId` 是**小程序那个应用**的
   （`JEEPAY_MINI_APP_ID`）、`channelExtra` 是 **JSON 字符串** `{"openid":"..."}`
   （⛔ dict 会签成 Python repr），签名用小程序密钥；
2. **openid 前置**：没有 openid / openid 属于别的小程序 ⇒ 业务错误「请在小程序内重新进入充值」，
   且**不落挂单、不调上游**；
3. **未配置**：`WX_MINI_*` 或 `JEEPAY_MINI_*` 缺项 ⇒ 「小程序支付未配置」（业务码，⛔ 不 500）；
4. **`/wx-openid/`**：code2session 成功存库、只回 `ok/bound`（不回显 openid）、
   `errcode != 0` 是业务错误（带 errcode，不带 errmsg）、按 IP 限流、未配置不 500；
5. **回归**：原有四种 way 仍用公众号应用（appId / key 不变、没有 `channelExtra`）；
   回调对公众号那条路径行为不变，并接受小程序应用的 `appId` + 密钥。

⚠️ 全部 mock `httpx`：不真调 Jeepay / 微信。
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.jeepay import sign_params
from apps.accounts.models import RechargeOrder, StaffProfile
from apps.accounts.wallet import get_wallet

JEEPAY = {
    "JEEPAY_GATEWAY": "https://pay.example.test",
    "JEEPAY_MCH_NO": "M100",
    "JEEPAY_APP_ID": "app-1",
    "JEEPAY_KEY": "test-key",
    "JEEPAY_NOTIFY_URL": "https://suqing.example.test/api/app/pay/notify/",
}
MINI = {
    "WX_MINI_APP_ID": "wx-mini-app-1",
    "WX_MINI_APP_SECRET": "mini-secret",
    "JEEPAY_MINI_APP_ID": "jeepay-mini-app-1",
    "JEEPAY_MINI_APP_KEY": "mini-key",
}
LOGIN_URL = "/api/app/auth/login/"
DEPOSIT_URL = "/api/app/wallet/deposit/"
WX_OPENID_URL = "/api/app/wallet/wx-openid/"
NOTIFY_URL = "/api/app/pay/notify/"
MOBILE = "13800008888"
PASSWORD = "pw-mp-pay-1"


def _response(payload, status=200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


def _wx_pay_response():
    return _response(
        {
            "code": 0,
            "msg": "SUCCESS",
            "data": {
                "payDataType": "wxapp",
                "payData": '{"appId":"wx-mini-app-1","package":"prepay_id=wx1"}',
            },
        }
    )


class MpPayTests(TenantTestCase):
    def setUp(self):
        # ⚠️ `override_settings` 用 `enable() + addCleanup`（`TenantTestCase.setUpClass`
        # 不走 `SimpleTestCase` 的类级注入，类装饰器实测不生效）。
        self._settings_override = override_settings(**JEEPAY, **MINI)
        self._settings_override.enable()
        self.addCleanup(self._settings_override.disable)
        # 限流桶在 LocMemCache 里，**不跟事务一起回滚** ⇒ 每条用例先清。
        cache.clear()

        self.user = User.objects.create_user(username=MOBILE, password=PASSWORD)
        self.profile = StaffProfile.objects.create(user=self.user, mobile=MOBILE)
        self.domain = self.tenant.get_primary_domain().domain
        self.client = Client(SERVER_NAME=self.domain)
        token = self.client.post(
            LOGIN_URL, {"username": MOBILE, "password": PASSWORD}, content_type="application/json"
        ).json()["data"]["token"]
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _deposit(self, way="WX_LITE", amount="100.00"):
        return self.client.post(
            DEPOSIT_URL,
            {"amount": amount, "way": way},
            content_type="application/json",
            **self.auth,
        ).json()

    def _bind_openid(self, openid="o-mini-1", appid=None):
        self.profile.wx_openid = openid
        self.profile.wx_openid_appid = appid if appid is not None else MINI["WX_MINI_APP_ID"]
        self.profile.save(update_fields=["wx_openid", "wx_openid_appid"])

    # ── 下单 ──────────────────────────────────────────────────────
    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_wx_lite_sends_json_channel_extra_with_mini_app_credentials(self, post):
        self._bind_openid()
        post.return_value = _wx_pay_response()

        body = self._deposit()
        self.assertEqual(body["code"], 0, body)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["wayCode"], "WX_LITE")
        # 用的是**小程序那个应用**的 appId（不是公众号那条 JEEPAY_APP_ID）。
        self.assertEqual(payload["appId"], MINI["JEEPAY_MINI_APP_ID"])
        # ⭐ 必须是 JSON **字符串**（⛔ 不是 dict）。
        extra = payload["channelExtra"]
        self.assertIsInstance(extra, str)
        self.assertEqual(extra, '{"openid":"o-mini-1"}')
        self.assertEqual(json.loads(extra), {"openid": "o-mini-1"})
        # 签名用的是小程序那把 key（换错 key 上行会验签失败）。
        unsigned = {k: v for k, v in payload.items() if k != "sign"}
        self.assertEqual(payload["sign"], sign_params(unsigned, MINI["JEEPAY_MINI_APP_KEY"]))

        # 出参照实透传上游的 payDataType / payData。
        self.assertEqual(body["data"]["pay_data_type"], "wxapp")
        self.assertEqual(body["data"]["pay_data"], _wx_pay_response().json()["data"]["payData"])
        self.assertEqual(RechargeOrder.objects.get().way_code, "WX_LITE")

    def test_wx_lite_without_openid_is_rejected_before_ordering(self):
        with mock.patch("apps.accounts.jeepay.httpx.post") as post:
            body = self._deposit()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "请在小程序内重新进入充值")
        self.assertFalse(RechargeOrder.objects.exists())
        post.assert_not_called()

    def test_wx_lite_openid_from_another_appid_is_rejected(self):
        self._bind_openid(openid="o-from-other-app", appid="wx-other-app")
        with mock.patch("apps.accounts.jeepay.httpx.post") as post:
            body = self._deposit()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "请在小程序内重新进入充值")
        self.assertFalse(RechargeOrder.objects.exists())
        post.assert_not_called()

    @override_settings(WX_MINI_APP_ID="")
    def test_wx_lite_without_mini_config_is_a_business_error(self):
        self._bind_openid()
        with mock.patch("apps.accounts.jeepay.httpx.post") as post:
            body = self._deposit()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("小程序支付未配置", body["msg"])
        self.assertFalse(RechargeOrder.objects.exists())
        post.assert_not_called()

    @override_settings(JEEPAY_MINI_APP_KEY="")
    def test_wx_lite_without_jeepay_mini_config_is_a_business_error(self):
        self._bind_openid()
        body = self._deposit()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("小程序支付未配置", body["msg"])
        # 失败照既有纪律：挂单作废（⛔ 不留 pending）。
        self.assertEqual(RechargeOrder.objects.get().state, "failed")

    # ── /wx-openid/ ──────────────────────────────────────────────
    def _openid(self, code="code-1"):
        return self.client.post(
            WX_OPENID_URL, {"code": code}, content_type="application/json", **self.auth
        ).json()

    @mock.patch("apps.accounts.wx_mini.httpx.get")
    def test_wx_openid_stores_the_openid_and_never_echoes_it(self, get):
        get.return_value = _response({"openid": "o-mini-9", "session_key": "SESSION_KEY"})
        body = self._openid("code-9")
        self.assertEqual(body["code"], 0, body)
        # 只回 ok / bound（⛔ 不回显 openid / session_key）。
        self.assertEqual(body["data"], {"ok": True, "bound": True})
        self.assertNotIn("o-mini-9", json.dumps(body, ensure_ascii=False))
        self.assertNotIn("SESSION_KEY", json.dumps(body, ensure_ascii=False))

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["appid"], MINI["WX_MINI_APP_ID"])
        self.assertEqual(params["secret"], MINI["WX_MINI_APP_SECRET"])
        self.assertEqual(params["js_code"], "code-9")

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.wx_openid, "o-mini-9")
        self.assertEqual(self.profile.wx_openid_appid, MINI["WX_MINI_APP_ID"])

    @mock.patch("apps.accounts.wx_mini.httpx.get")
    def test_wx_openid_errcode_is_a_business_error_with_the_code(self, get):
        get.return_value = _response({"errcode": 40029, "errmsg": "invalid code"})
        body = self._openid("bad-code")
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("40029", body["msg"])
        # 文案不搬微信 errmsg（⛔ 上游原文，且没有动作价值）。
        self.assertNotIn("invalid code", body["msg"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.wx_openid, "")

    @override_settings(APP_WX_OPENID_MAX_REQUESTS=2)
    @mock.patch("apps.accounts.wx_mini.httpx.get")
    def test_wx_openid_is_rate_limited_per_ip(self, get):
        get.return_value = _response({"openid": "o-mini-9"})
        self.assertEqual(self._openid("c1")["code"], 0)
        self.assertEqual(self._openid("c2")["code"], 0)
        body = self._openid("c3")
        self.assertEqual(body["code"], 1033, body)
        self.assertEqual(body["msg"], "尝试过于频繁，请稍后再试")
        # 被限流的那一次**不调微信**。
        self.assertEqual(get.call_count, 2)

    @override_settings(WX_MINI_APP_SECRET="")
    @mock.patch("apps.accounts.wx_mini.httpx.get")
    def test_wx_openid_without_config_is_a_business_error(self, get):
        body = self._openid()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("小程序支付未配置", body["msg"])
        get.assert_not_called()

    # ── 回归：原有四种 way 与回调 ─────────────────────────────────
    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_existing_way_still_uses_the_official_account_app(self, post):
        self._bind_openid()
        post.return_value = _response(
            {"code": 0, "data": {"payDataType": "wxapp", "payData": "{}"}}
        )
        body = self._deposit(way="WX_APP")
        self.assertEqual(body["code"], 0, body)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["appId"], JEEPAY["JEEPAY_APP_ID"])
        self.assertNotIn("channelExtra", payload)
        unsigned = {k: v for k, v in payload.items() if k != "sign"}
        self.assertEqual(payload["sign"], sign_params(unsigned, JEEPAY["JEEPAY_KEY"]))

    def _notify(self, order, app_id, key, amount_cents=None):
        params = {
            "mchNo": JEEPAY["JEEPAY_MCH_NO"],
            "appId": app_id,
            "mchOrderNo": order.mch_order_no,
            "state": "2",
            "amount": amount_cents or str(int(order.amount * 100)),
        }
        params["sign"] = sign_params(params, key)
        return self.client.post(NOTIFY_URL, params)

    def test_official_account_notify_still_credits(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="RMP1", way_code="WX_APP"
        )
        response = self._notify(order, JEEPAY["JEEPAY_APP_ID"], JEEPAY["JEEPAY_KEY"])
        self.assertEqual(response.content, b"success")
        self.assertEqual(get_wallet(self.user).balance, Decimal("50"))

    def test_wx_lite_notify_with_the_mini_app_credentials_credits(self):
        """小程序订单的回调带**小程序应用的** appId、用**小程序密钥**签（Jeepay 侧是
        按订单所属应用签的）⇒ 必须入账，否则钱永远到不了账。"""
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("30"), mch_order_no="RMP2", way_code="WX_LITE"
        )
        response = self._notify(
            order, MINI["JEEPAY_MINI_APP_ID"], MINI["JEEPAY_MINI_APP_KEY"]
        )
        self.assertEqual(response.content, b"success")
        self.assertEqual(get_wallet(self.user).balance, Decimal("30"))

    def test_wx_lite_notify_signed_with_the_wrong_key_is_rejected(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("30"), mch_order_no="RMP3", way_code="WX_LITE"
        )
        response = self._notify(order, MINI["JEEPAY_MINI_APP_ID"], JEEPAY["JEEPAY_KEY"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))
