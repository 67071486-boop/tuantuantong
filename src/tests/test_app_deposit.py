"""充值（Jeepay 下单 + 回调入账，D-0269 P2 第二批，2026-10-19）。

## 钉住什么

1. **下单不提前加钱**：`RechargeOrder` 落库 + 返回支付参数，钱包余额不动；
2. **失败要能看见**：Jeepay `code != 0` / 未配置都回业务码，且那张待支付单被标成 failed；
3. **回调验签**：签名不对回 400 `fail`；
4. **回调幂等**：同一个 `mchOrderNo` 来两次只入账一次（Jeepay 会重试）；
5. **非成功状态不入账**（`state != 2`）；
6. ⭐ **四种支付方式**：`ALI_APP` / `ALI_QR` / `WX_NATIVE` / `WX_APP` 都发给
   上游正确的 `wayCode`，回参把 `payDataType` / `payData` 原样带出（客户端按前者分流）；
   非法值回业务码（⛔ 不 500）；`code=0` 但 `data.errCode=NO_AUTH` 必须判失败并带上游原文；
7. ⭐ **二维码出图**：`codeUrl` 的两种 way 回 `qr_png_base64`
   （**裸** base64、PNG 魔数）；`aliapp` 没有该字段；生成抛异常 ⇒ 端点**不 500**、
   `pay_data` 照旧可用、日志里**没有**支付链接。

⚠️ 全部 mock `httpx.post`：不真调 Jeepay（那也是别人的生产实例）。
"""
import base64
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.jeepay import sign_params
from apps.accounts.models import RechargeOrder, StaffProfile, WalletTransaction
from apps.accounts.wallet import get_wallet
from apps.audit.models import OperationLog

JEEPAY = {
    "JEEPAY_GATEWAY": "https://pay.example.test",
    "JEEPAY_MCH_NO": "M100",
    "JEEPAY_APP_ID": "app-1",
    "JEEPAY_KEY": "test-key",
    "JEEPAY_NOTIFY_URL": "https://suqing.example.test/api/app/pay/notify/",
}
LOGIN_URL = "/api/app/auth/login/"
DEPOSIT_URL = "/api/app/wallet/deposit/"
NOTIFY_URL = "/api/app/pay/notify/"
MOBILE = "13800009999"
PASSWORD = "pw-deposit-1"


def _response(payload, status=200):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = payload
    return response


class DepositTests(TenantTestCase):
    def setUp(self):
        # ⚠️ `override_settings` **不用类装饰器**：`TenantTestCase.setUpClass` 不走
        # `SimpleTestCase` 那套注入，实测类级装饰器不生效（`settings.JEEPAY_KEY` 仍是空）。
        # 用 `enable()` + `addCleanup` 是这里唯一可靠的写法。
        self._settings_override = override_settings(**JEEPAY)
        self._settings_override.enable()
        self.addCleanup(self._settings_override.disable)

        self.user = User.objects.create_user(username=MOBILE, password=PASSWORD)
        StaffProfile.objects.create(user=self.user, mobile=MOBILE)
        self.domain = self.tenant.get_primary_domain().domain
        self.client = Client(SERVER_NAME=self.domain)
        token = self.client.post(
            LOGIN_URL, {"username": MOBILE, "password": PASSWORD}, content_type="application/json"
        ).json()["data"]["token"]
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _deposit(self, amount="100.00", way="WX_APP"):
        return self.client.post(
            DEPOSIT_URL,
            {"amount": amount, "way_code": way},
            content_type="application/json",
            **self.auth,
        ).json()

    # ── 下单 ──────────────────────────────────────────────────────
    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_deposit_creates_an_order_without_crediting_the_wallet(self, post):
        post.return_value = _response(
            {"code": 0, "msg": "SUCCESS", "data": {"payDataType": "wxapp", "payData": "{}"}}
        )
        body = self._deposit()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["pay_data_type"], "wxapp")
        order = RechargeOrder.objects.get()
        self.assertEqual(order.amount, Decimal("100.00"))
        self.assertEqual(order.state, "pending")
        # ⚠️ 余额不动：钱要等回调。
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_jeepay_failure_marks_the_order_failed(self, post):
        post.return_value = _response({"code": 9999, "msg": "签名失败"})
        body = self._deposit()
        self.assertNotEqual(body["code"], 0)
        self.assertIn("签名失败", body["msg"])
        self.assertEqual(RechargeOrder.objects.get().state, "failed")

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_unsupported_way_is_rejected_before_calling_jeepay(self, post):
        body = self._deposit(way="QR_CASHIER")
        # `1000` = `ErrorCode.VALIDATION`；HTTP 仍是 200（业务码口径，⛔ 不 500）。
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("不支持的支付方式：QR_CASHIER", body["msg"])
        # 文案里带出合法集，客户端/排障的人不用再去翻代码。
        self.assertIn("ALI_QR", body["msg"])
        self.assertIn("WX_NATIVE", body["msg"])
        post.assert_not_called()
        # 这条分支也要把挂单作废，⛔ 不能留 pending。
        self.assertEqual(RechargeOrder.objects.get().state, "failed")

    def test_supported_ways_are_exactly_the_five_live_ones(self):
        """支持集**恰好**五种（2026-10-20 实测的四个 + 2026-10-21 的小程序 `WX_LITE`；
        `WX_H5` / `WX_JSAPI` 不接）。"""
        from apps.accounts.jeepay import SUPPORTED_WAYS

        self.assertEqual(
            SUPPORTED_WAYS,
            {"ALI_APP", "ALI_QR", "WX_NATIVE", "WX_APP", "WX_LITE"},
        )

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_four_supported_ways_reach_jeepay_with_the_right_way_code(self, post):
        """四种方式：发给上游的 `wayCode` 正确 + 回参把 `payDataType`/`payData` **原样**带出。

        ⚠️ 两条扫码方式的 `payDataType` 都是 `codeUrl`，客户端靠 `pay_data_type` 就能统一分流；
        `ALI_APP` 是 `aliapp`、`WX_APP` 是 `wxapp`（支付串由客户端交给各自的 SDK）。
        ⚠️ 其中**一条**刻意改用 `way` 字段（业务方口径叫 `way`）：证明两个字段名都收。
        """
        cases = (
            (
                "way_code", "ALI_APP", "aliapp",
                "alipay_sdk=alipay-sdk-java&app_id=2021000000000000&biz_content=%7B%7D&sign=x",
            ),
            (
                "way_code", "ALI_QR", "codeUrl",
                "https://qr.alipay.com/bax00123abc",
            ),
            (
                # ⭐ 别名 `way`：与 `way_code` 等价。
                "way", "WX_NATIVE", "codeUrl",
                "weixin://wxpay/bizpayurl?pr=abcdefg",
            ),
            (
                "way_code", "WX_APP", "wxapp",
                '{"appid":"wx123","partnerid":"m100","prepayid":"wx001",'
                '"package":"Sign=WXPay","noncestr":"n","timestamp":"1","sign":"S"}',
            ),
        )
        for field, way, pay_data_type, pay_data in cases:
            with self.subTest(way=way, field=field):
                post.reset_mock()
                post.return_value = _response(
                    {
                        "code": 0,
                        "msg": "SUCCESS",
                        "data": {"payDataType": pay_data_type, "payData": pay_data},
                    }
                )
                body = self.client.post(
                    DEPOSIT_URL,
                    {"amount": "100.00", field: way},
                    content_type="application/json",
                    **self.auth,
                ).json()
                self.assertEqual(body["code"], 0, body)
                # 发给上游的 `wayCode` 逐字对：不是「四种都能过」就行，要真的是选中的那种。
                self.assertEqual(post.call_args.kwargs["json"]["wayCode"], way)
                # 回参形状没变，且 `payData` **原样**带出（⛔ 不截断 / 不解析）。
                # ⭐ `codeUrl` 多一个**可选**字段 `qr_png_base64`，
                #    `aliapp` / `wxapp` 没有它 —— 键集按这一条分档断言（见下一条用例）。
                expected_keys = {"mch_order_no", "amount", "pay_data_type", "pay_data"}
                if pay_data_type == "codeUrl":
                    expected_keys.add("qr_png_base64")
                self.assertEqual(set(body["data"]), expected_keys)
                self.assertEqual(body["data"]["pay_data_type"], pay_data_type)
                self.assertEqual(body["data"]["pay_data"], pay_data)
                # 挂单上记的也是这种（对账 / 排障要用）。
                self.assertEqual(
                    RechargeOrder.objects.order_by("-id").first().way_code, way
                )

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_no_auth_from_upstream_is_a_business_error_with_the_upstream_text(self, post):
        """`code=0` 但 `data.errCode=NO_AUTH` ⇒ 业务错误 + **上游原文**（⛔ 不是假成功）。

        对应业务方真商户号实测：`WX_APP` / `WX_H5` 未开通时上游就是这么回的。
        旧实现只报「下单成功但没有返回支付参数」——那句话会让人去查代码，而真正要做的动作
        是去商户平台开通产品权限。
        """
        post.return_value = _response(
            {
                "code": 0,
                "msg": "SUCCESS",
                "data": {
                    "errCode": "NO_AUTH",
                    "errMsg": "商户号该产品权限未开通，请前往商户平台确认",
                },
            }
        )
        body = self._deposit(way="WX_APP")
        # `1002` = `ErrorCode.BUSINESS`（上游拒绝，不是参数错）。
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("NO_AUTH", body["msg"])
        self.assertIn("商户号该产品权限未开通", body["msg"])
        # 失败照既有纪律：挂单标 failed、钱包一分不加、不写充值审计。
        self.assertEqual(RechargeOrder.objects.get().state, "failed")
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))
        self.assertFalse(OperationLog.objects.filter(action="wallet.deposit").exists())

    # ── 二维码出图（业务方批准新增 qrcode + Pillow）──────────
    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_code_url_ways_return_a_qr_png_without_a_data_uri_prefix(self, post):
        """`codeUrl` 的两种 way 都出图：**裸 base64**、解码后是 PNG（魔数 `\\x89PNG`）。"""
        for way, pay_data in (
            ("ALI_QR", "https://qr.alipay.com/bax00123abc"),
            ("WX_NATIVE", "weixin://wxpay/bizpayurl?pr=abcdefg"),
        ):
            with self.subTest(way=way):
                post.return_value = _response(
                    {"code": 0, "data": {"payDataType": "codeUrl", "payData": pay_data}}
                )
                body = self._deposit(way=way)
                self.assertEqual(body["code"], 0, body)
                qr = body["data"]["qr_png_base64"]
                self.assertIsInstance(qr, str)
                self.assertTrue(qr)
                # 契约只有一种写法：**裸 base64**（客户端自己拼 `data:image/png;base64,`）。
                self.assertFalse(qr.startswith("data:"), qr[:30])
                raw = base64.b64decode(qr, validate=True)
                self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), raw[:8])

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_aliapp_has_no_qr_field(self, post):
        """`aliapp` 是给支付宝 SDK 的支付串，不是可扫链接 ⇒ ⛔ 不出图。"""
        post.return_value = _response(
            {"code": 0, "data": {"payDataType": "aliapp", "payData": "alipay_sdk=..."}}
        )
        body = self._deposit(way="ALI_APP")
        self.assertEqual(body["code"], 0, body)
        self.assertNotIn("qr_png_base64", body["data"])

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_qr_is_generated_from_the_pay_link(self, post):
        """出图的输入**就是** `pay_data`（钉住桩真的接上了，不是一张硬编码的图）。"""
        pay_data = "https://qr.alipay.com/bax00123abc"
        post.return_value = _response(
            {"code": 0, "data": {"payDataType": "codeUrl", "payData": pay_data}}
        )
        with mock.patch(
            "apps.accounts.app_wallet_views.qr_png_base64", return_value="QkFTRTY0"
        ) as generator:
            body = self._deposit(way="ALI_QR")
        generator.assert_called_once_with(pay_data)
        self.assertEqual(body["data"]["qr_png_base64"], "QkFTRTY0")

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_qr_failure_keeps_the_deposit_alive_and_never_logs_the_link(self, post):
        """生成抛异常 ⇒ **不 500**、没有 qr 字段、`pay_data` 照旧可用、日志里没有支付链接。"""
        pay_data = "https://qr.alipay.com/secret-signed-link"
        post.return_value = _response(
            {"code": 0, "data": {"payDataType": "codeUrl", "payData": pay_data}}
        )
        with mock.patch(
            "apps.accounts.app_wallet_views.qr_png_base64",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs(
                "apps.accounts.app_wallet_views", level="WARNING"
            ) as logs:
                body = self._deposit(way="ALI_QR")
        self.assertEqual(body["code"], 0, body)
        self.assertNotIn("qr_png_base64", body["data"])
        # 原有字段照旧可用（客户端兜底：自己画码 / 打开链接）。
        self.assertEqual(body["data"]["pay_data"], pay_data)
        self.assertEqual(body["data"]["pay_data_type"], "codeUrl")
        # 图失败不影响下单本身：挂单仍是 pending、钱没动。
        self.assertEqual(RechargeOrder.objects.get().state, "pending")
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))
        # ⛔ 支付链接是**可付款凭证**，不许进日志：只记异常**类名**。
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(pay_data, text)
        self.assertNotIn("secret-signed-link", text)

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_a_string_zero_code_is_accepted(self, post):
        """评审 Minor 8：上游把 `code` 序列化成 `"0"` 时不能误判失败。"""
        post.return_value = _response(
            {"code": "0", "data": {"payDataType": "wxapp", "payData": "{}"}}
        )
        body = self._deposit()
        self.assertEqual(body["code"], 0, body)

    def test_unconfigured_jeepay_is_a_business_error(self):
        with override_settings(JEEPAY_KEY=""):
            body = self._deposit()
        self.assertNotEqual(body["code"], 0)
        self.assertIn("Jeepay 未配置", body["msg"])

    # ── 回调 ──────────────────────────────────────────────────────
    def _notify(self, params):
        # ⚠️ 用默认（multipart）编码：Jeepay 真实回调是 `application/x-www-form-urlencoded`，
        # 但两者在 DRF 里都落成 QueryDict，签名口径一致。这里选默认是为了避开测试客户端
        # 对 udict 的隐式转换。
        return self.client.post(NOTIFY_URL, params)

    def _paid_params(self, order):
        params = {
            "mchNo": "M100",
            "appId": "app-1",
            "mchOrderNo": order.mch_order_no,
            "state": "2",
            "amount": str(int(order.amount * 100)),
        }
        params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
        return params

    def test_notify_with_a_bad_signature_is_rejected(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R1", way_code="WX_APP"
        )
        params = self._paid_params(order)
        params["sign"] = "BAD"
        response = self._notify(params)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))

    def test_notify_backfills_the_jeepay_order_id(self):
        """评审 Minor 7：回调里的 `payOrderId` 要落到 `jeepay_order_no`（对账要用）。"""
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R10", way_code="WX_APP"
        )
        params = {
            "mchNo": "M100",
            "appId": "app-1",
            "mchOrderNo": "R10",
            "state": "2",
            "amount": "5000",
            "payOrderId": "P202610190001",
        }
        params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
        self.assertEqual(self._notify(params).content, b"success")
        order.refresh_from_db()
        self.assertEqual(order.jeepay_order_no, "P202610190001")

    def test_notify_credits_the_wallet_once(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R2", way_code="WX_APP"
        )
        params = self._paid_params(order)
        self.assertEqual(self._notify(params).content, b"success")
        order.refresh_from_db()
        self.assertEqual(order.state, "paid")
        self.assertEqual(get_wallet(self.user).balance, Decimal("50"))
        # 第二次（Jeepay 重试）：仍然 success，但只入账一次。
        self.assertEqual(self._notify(params).content, b"success")
        self.assertEqual(get_wallet(self.user).balance, Decimal("50"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RECHARGE).count(), 1
        )

    def test_notify_with_a_non_success_state_does_not_credit(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R3", way_code="WX_APP"
        )
        params = self._paid_params(order)
        params["state"] = "3"
        params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
        self.assertEqual(self._notify(params).content, b"success")
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))
        order.refresh_from_db()
        self.assertEqual(order.state, "pending")

    def test_notify_for_an_unknown_order_returns_fail(self):
        params = {"mchOrderNo": "nope", "state": "2", "sign": sign_params({"mchOrderNo": "nope", "state": "2"}, JEEPAY["JEEPAY_KEY"])}
        self.assertEqual(self._notify(params).status_code, 404)

    # ── 补充用例──────────────────────────────
    def test_sign_matches_a_known_vector(self):
        """评审 Major 2：签名是**独立实现**算出来的已知值，⛔ 不是自己签自己验。

        这组参数与 key 的期望值由评审方用独立实现核对过；把 `sign_params` 的排序或拼接
        改错，这条会红（上一轮的变异 M2 正是这样存活的）。
        """
        params = {
            "mchNo": "M100",
            "appId": "app-1",
            "mchOrderNo": "R2",
            "state": "2",
            "amount": "5000",
        }
        self.assertEqual(sign_params(params, "test-key"), "A01452CC35548CEA967A1E4BFC9A30C3")

    def test_notify_accepts_a_payload_signed_outside_sign_params(self):
        """用**手写死的签名**构造回调：证明验签不是「调同一个函数所以恒成立」。"""
        RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R2", way_code="WX_APP"
        )
        response = self._notify(
            {
                "mchNo": "M100",
                "appId": "app-1",
                "mchOrderNo": "R2",
                "state": "2",
                "amount": "5000",
                "sign": "A01452CC35548CEA967A1E4BFC9A30C3",
            }
        )
        self.assertEqual(response.content, b"success")
        self.assertEqual(get_wallet(self.user).balance, Decimal("50"))

    def test_notify_with_a_mismatched_amount_is_rejected(self):
        """评审 Major 1：签名为真但金额被改小 ⇒ 不入账、回 fail。"""
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R4", way_code="WX_APP"
        )
        params = {
            "mchNo": "M100",
            "appId": "app-1",
            "mchOrderNo": "R4",
            "state": "2",
            "amount": "1",
        }
        params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
        self.assertEqual(self._notify(params).status_code, 400)
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))
        order.refresh_from_db()
        self.assertEqual(order.state, "pending")

    def test_notify_with_a_mismatched_merchant_is_rejected(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R5", way_code="WX_APP"
        )
        params = {
            "mchNo": "M-OTHER",
            "appId": "app-1",
            "mchOrderNo": "R5",
            "state": "2",
            "amount": "5000",
        }
        params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
        self.assertEqual(self._notify(params).status_code, 400)
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))

    def test_revoked_or_closed_notifications_finalize_the_order(self):
        """评审 Minor 2：撤销/关闭是终态，⛔ 不能留一条永远 pending 的单。"""
        for index, state in enumerate(("4", "6")):
            order = RechargeOrder.objects.create(
                user=self.user,
                amount=Decimal("50"),
                mch_order_no=f"R9{index}",
                way_code="WX_APP",
            )
            params = {
                "mchNo": "M100",
                "appId": "app-1",
                "mchOrderNo": order.mch_order_no,
                "state": state,
                "amount": "5000",
            }
            params["sign"] = sign_params(params, JEEPAY["JEEPAY_KEY"])
            self.assertEqual(self._notify(params).content, b"success")
            order.refresh_from_db()
            self.assertEqual(order.state, "closed")

    def test_notify_without_a_key_is_rejected(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R6", way_code="WX_APP"
        )
        params = self._paid_params(order)
        with override_settings(JEEPAY_KEY=""):
            self.assertEqual(self._notify(params).status_code, 400)
        self.assertEqual(get_wallet(self.user).balance, Decimal("0"))

    def test_notify_with_an_empty_signature_is_rejected(self):
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R7", way_code="WX_APP"
        )
        params = self._paid_params(order)
        params["sign"] = ""
        self.assertEqual(self._notify(params).status_code, 400)

    @mock.patch("apps.accounts.app_wallet_views.create_payment", side_effect=RuntimeError("boom"))
    def test_unexpected_failure_marks_the_order_failed(self, _payment):
        """评审 Minor 1：非预期异常也要把挂单作废，再让 500 冒上去。"""
        client = Client(
            SERVER_NAME=self.domain, raise_request_exception=False
        )
        response = client.post(
            DEPOSIT_URL,
            {"amount": "100.00", "way_code": "WX_APP"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(RechargeOrder.objects.get().state, "failed")

    def test_transactions_endpoint_lists_only_my_flow(self):
        """评审 Minor 6：流水读面有端点级用例（金额字符串、只读自己）。"""
        order = RechargeOrder.objects.create(
            user=self.user, amount=Decimal("50"), mch_order_no="R8", way_code="WX_APP"
        )
        params = self._paid_params(order)
        self._notify(params)
        body = self.client.get(
            "/api/app/wallet/transactions/", **self.auth
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 1)
        row = body["data"]["items"][0]
        self.assertEqual(row["kind"], "recharge")
        self.assertIsInstance(row["amount"], str)
        self.assertEqual(row["amount"], "50.00")
