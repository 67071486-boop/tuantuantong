"""打手提现审核后台 + 支付宝代付（2026-10-20，计划
`docs/plans/2026-10-20-taker-withdraw-review-and-alipay-payout.md` §7）。

## 十条判据逐条落成用例（计划 §7 的编号见每个类的 docstring）

1. `code=0` 但 `state=1` ⇒ `paying`，余额不变、不写退回流水；
2. `state=3` ⇒ 退余额，且只退一次（重复刷新不再退）；
3. 同一单连点两次代付 ⇒ 只发一笔（第二次被非 `approved` 挡）；
4. `mark-paid` 缺单号 / `reject` 缺原因 ⇒ 业务码 1000；
5. 非 `pending` 不能 approve；`paid` 不能退款 / 重新登记；
6. `confirm_amount` ≠ 单子金额 ⇒ 拒绝且**不发起上游请求**（用桩证明没调用）；
7. 上游签名失败 / 返回非 JSON ⇒ 不采信、不落 `paid`；
8. 无节点访问 6 个端点全 403（fail-closed）；
9. 跨租户：以 A 的会话查 B 的单子 ⇒ 404（判据用**租户内唯一的支付宝账号**，不用主键指认）；
10. 审计：5 个写动作各落一行；钱包三条写也各落一行。

## 桩一律打在「被查找的模块」上

- 代付两个上游函数由 `apps.accounts.wallet` 在模块级 import ⇒ 打在
  `apps.accounts.wallet.create_transfer` / `query_transfer`；
- 充值下单由 `apps.accounts.app_wallet_views` import ⇒ 打在
  `apps.accounts.app_wallet_views.create_payment`；
- 每条桩都用 `assert_called_once_with` / `assert_not_called` 自证「真的接上了」——
  ⛔ 只写 `patch(...)` 不检查调用等于没证明（AGENTS §5-E / E-0029）。

## 金额一律按**字符串**断言

响应契约（计划 §4.3.1）里金额是两位小数字符串；本文件不把响应转成 `float` 再比。
"""
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.jeepay import JeepayError, create_transfer, query_transfer
from apps.accounts.models import (
    StaffProfile,
    TakerWallet,
    WalletTransaction,
    WithdrawOrder,
)
from apps.accounts.wallet import (
    _refund_withdraw,
    get_wallet,
    request_withdraw,
)
from apps.audit.models import OperationLog
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 审核页的权限节点（`apps/rbac/catalog.py` 的第五条自有节点）。
NODE = "finance.taker_withdraw_review"

#: 代付相关配置：**不用类装饰器**（`TenantTestCase.setUpClass` 不走
#: `SimpleTestCase` 那套注入，类级 `override_settings` 不生效——同 `test_app_deposit` 的实测）。
JEEPAY = {
    "JEEPAY_GATEWAY": "https://pay.example.test",
    "JEEPAY_MCH_NO": "M100",
    "JEEPAY_APP_ID": "app-1",
    "JEEPAY_KEY": "test-key",
    "JEEPAY_NOTIFY_URL": "https://suqing.example.test/api/app/pay/notify/",
    "JEEPAY_TRANSFER_IF_CODE": "alipay",
    "JEEPAY_TRANSFER_ENTRY_TYPE": "ALIPAY_CASH",
    "WITHDRAW_AUTO_TRANSFER": True,
    "WITHDRAW_TRANSFER_MAX_AMOUNT": "1000",
}
BASE_URL = "/api/business/withdrawals/"


class WithdrawReviewTestBase(TenantTestCase):
    """共用夹具：一个打手、一个持节点的客服、一张可用的提现单。"""

    def setUp(self):
        overrides = override_settings(**JEEPAY)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.taker = User.objects.create_user(username="tw-taker", password="pw-tw-taker")
        self.profile = StaffProfile.objects.create(
            user=self.taker,
            mobile="13700002222",
            alipay_account="taker@example.com",
            alipay_name="打手甲",
        )
        self.admin_client = make_logged_in_client(
            username="tw-admin", password="pw-tw-admin", groups=("客服",), nodes=(NODE,)
        )
        self.admin = User.objects.get(username="tw-admin")

    # ── 建单 ──────────────────────────────────────────────────────
    def _fund(self, amount="88.00"):
        wallet = get_wallet(self.taker)
        wallet.balance += Decimal(amount)
        wallet.save(update_fields=["balance", "updated_at"])
        return wallet

    def _pending(self, amount="88.00"):
        """走**真实申请路径**建单（余额当场扣掉，退款才有意义）。"""
        self._fund(amount)
        return request_withdraw(self.taker, Decimal(amount))

    def _approved(self, amount="88.00"):
        order = self._pending(amount)
        WithdrawOrder.objects.filter(pk=order.pk).update(
            state=WithdrawOrder.STATE_APPROVED,
            reviewer=self.admin,
            reviewed_at=timezone.now(),
        )
        order.refresh_from_db()
        return order

    def _post(self, path, data=None, client=None):
        return (client or self.admin_client).post(
            f"{BASE_URL}{path}", data or {}, content_type="application/json"
        )

    def _body(self, path, data=None):
        return self._post(path, data).json()


class TransferSuccessCriteriaTests(WithdrawReviewTestBase):
    """计划 §7 第 1 / 2 / 3 / 6 / 7 条：代付成败的判据。"""

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_code_zero_with_state_one_is_paying_not_paid(self, create):
        """① `code=0` 只表示发起成功；`state=1` 必须落 `paying`、⛔ 不落 `paid`。"""
        order = self._approved("88.00")
        create.return_value = {"state": 1, "transferId": "T-1"}
        balance_before = get_wallet(self.taker).balance
        refunds_before = WalletTransaction.objects.filter(
            kind=WalletTransaction.KIND_WITHDRAW_REFUND
        ).count()

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["state"], "paying")
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)
        self.assertIsNone(order.paid_at)
        # 桩真的接上了：金额是 Decimal 元、账号是申请时的快照。
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["amount"], Decimal("88.00"))
        self.assertEqual(kwargs["account_no"], "taker@example.com")
        self.assertEqual(kwargs["account_name"], "打手甲")
        # 评审建议 5：商户单号带随机后缀（两台租户机共用商户号时防撞号）。
        self.assertRegex(
            kwargs["mch_order_no"], rf"^TW{order.pk}-[0-9a-f]{{6}}$",
            "商户单号必须带 6 位随机后缀（⛔ 不能只有 TW{pk}）",
        )
        # 余额不变、没有多出一条退回流水 ⇒ 「没落 paid」不只是状态字段上的话。
        self.assertEqual(get_wallet(self.taker).balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_WITHDRAW_REFUND
            ).count(),
            refunds_before,
        )

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_state_three_refunds_the_balance_exactly_once(self, create):
        """② `state=3` ⇒ `failed` + 退余额，且**重复刷新不再退**。"""
        order = self._approved("30.00")
        # ⚠️ 上游字段名是 `errMsg` / `errCode`（评审 A2：旧夹具的 `errorMsg` 是错的，
        #    会把正确实现反向判红）。
        create.return_value = {
            "state": 3, "errMsg": "账户名不匹配", "errCode": "ACCOUNT_NAME_NOT_MATCH",
        }
        balance_before = get_wallet(self.taker).balance

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "30.00"})
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["state"], "failed")
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_FAILED)
        self.assertIn("账户名不匹配", order.transfer_error)
        self.assertIn("ACCOUNT_NAME_NOT_MATCH", order.transfer_error)

        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, balance_before + Decimal("30.00"))
        refunds = WalletTransaction.objects.filter(
            kind=WalletTransaction.KIND_WITHDRAW_REFUND, ref_no=f"W{order.pk}"
        )
        self.assertEqual(refunds.count(), 1, "退回流水必须恰好一条")

        # 重复刷新：`failed` 是终态 ⇒ 不再查单、也不再退。
        with mock.patch("apps.accounts.wallet.query_transfer") as query:
            body = self._body(f"{order.pk}/refresh/")
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["state"], "failed")
        query.assert_not_called()
        self.assertEqual(get_wallet(self.taker).balance, balance_before + Decimal("30.00"))
        self.assertEqual(refunds.count(), 1, "重复刷新把同一笔钱退了第二次")

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_a_second_click_does_not_send_a_second_transfer(self, create):
        """③ 同一单连点两次 ⇒ 第二次被非 `approved` 挡，上游只被调一次。"""
        order = self._approved("88.00")
        create.return_value = {"state": 1, "transferId": "T-1"}

        first = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})
        second = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], ErrorCode.CONFLICT, second)
        create.assert_called_once()

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_confirm_amount_mismatch_is_rejected_before_the_upstream_call(self, create):
        """⑥ 金额回显不等于单子金额 ⇒ 拒绝，且**一次上游请求都不发**。"""
        order = self._approved("88.00")

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "87.99"})

        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)
        self.assertIsNone(order.mch_order_no, "被拒的请求不该占掉商户单号")

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_transfer_over_the_single_limit_is_rejected(self, create):
        """业务方 2026-10-20 拍板：超过单笔上限 ⇒ 业务码 VALIDATION + 指定文案。"""
        order = self._approved("1000.01")

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "1000.01"})

        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertEqual(body["msg"], "超过代付单笔上限，请人工打款")
        create.assert_not_called()

    def test_signature_failure_is_not_trusted_and_never_marks_paid(self):
        """⑦ 上游签名失败（`JeepayError`）⇒ 不采信、不落 `paid`。"""
        order = self._approved("88.00")
        with mock.patch(
            "apps.accounts.wallet.create_transfer",
            side_effect=JeepayError("Jeepay 代付失败：签名错误（code=9999）"),
        ) as create:
            body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("签名错误", body["msg"])
        create.assert_called_once()
        order.refresh_from_db()
        self.assertNotEqual(order.state, WithdrawOrder.STATE_PAID)
        self.assertIsNone(order.paid_at)
        self.assertIn("签名错误", order.transfer_error)
        # 评审 B1：请求已回来（哪怕失败）⇒ 在途标记必须清空，否则人工打款会白等 120 秒。
        self.assertIsNone(order.transfer_started_at)
        # 评审 B1 第 4 条：失败发起也要留一条审计（动作沿用审核那族，原因进 reason）。
        rows = OperationLog.objects.filter(action="finance.withdraw_transfer")
        self.assertEqual(rows.count(), 1, "发起失败没有留审计")
        self.assertIn("签名错误", rows.get().reason)
        self.assertEqual(rows.get().actor_id, self.admin.pk)

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_create_transfer_rejects_a_non_zero_code(self, post):
        """评审 A3：`code != 0` 必须在 **jeepay 这一层**被判失败（原来这条判据没人守）。"""
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"code": 9999, "msg": "验签失败"}
        post.return_value = response

        with self.assertRaises(JeepayError) as ctx:
            create_transfer(
                mch_order_no="TW1-abcdef",
                amount=Decimal("1.00"),
                account_no="taker@example.com",
                account_name="打手甲",
                desc="打手提现 1",
                client_ip="127.0.0.1",
            )
        self.assertIn("验签失败", str(ctx.exception))
        post.assert_called_once()

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_query_transfer_rejects_a_non_zero_code(self, post):
        """评审 A3：查单端点同样只认 `code == 0`。"""
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"code": 9999, "msg": "查单失败"}
        post.return_value = response

        with self.assertRaises(JeepayError) as ctx:
            query_transfer("TW1-abcdef")
        self.assertIn("查单失败", str(ctx.exception))
        post.assert_called_once()

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_confirm_amount_greater_than_the_order_is_rejected(self, create):
        """评审建议 8：金额比单子大（88.01 > 88.00）同样拒，⛔ 不是只拒更小的。"""
        order = self._approved("88.00")

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.01"})

        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        create.assert_not_called()

    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_the_single_limit_boundary_is_inclusive(self, create):
        """评审建议 8：恰好 1000.00 **放行**（`> limit` 才拒，不是 `>=`）。"""
        order = self._approved("1000.00")
        create.return_value = {"state": 1, "transferId": "T-1"}

        body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "1000.00"})

        self.assertEqual(body["code"], 0, body)
        create.assert_called_once()

    @mock.patch("apps.accounts.jeepay.httpx.post")
    def test_non_json_upstream_response_raises_instead_of_reporting_success(self, post):
        """⑦（下层）非 JSON 一律 `JeepayError`——`create_transfer` 不把它当成功。"""
        response = mock.Mock()
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        post.return_value = response

        with self.assertRaises(JeepayError):
            create_transfer(
                mch_order_no="TW1",
                amount=Decimal("1.00"),
                account_no="taker@example.com",
                account_name="打手甲",
                desc="打手提现 1",
                client_ip="127.0.0.1",
            )
        post.assert_called_once()

    @mock.patch("apps.accounts.wallet.create_transfer")
    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_refresh_marks_paid_only_when_the_query_says_state_two(self, query, create):
        """⭐ 判据 1 的另一半：`state=2` 才是成功——刷新后落 `paid`。

        ⚠️ 这里同时证明「`paying` 不是终态、必须查单」：发起时 `state=1`，刷新时才拿到 `2`。
        """
        order = self._approved("88.00")
        create.return_value = {"state": 1, "transferId": "T-1"}
        self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})

        query.return_value = {"state": 2, "transferId": "T-1"}
        body = self._body(f"{order.pk}/refresh/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["state"], "paid")
        order.refresh_from_db()
        query.assert_called_once_with(order.mch_order_no)
        self.assertEqual(order.state, WithdrawOrder.STATE_PAID)
        self.assertIsNotNone(order.paid_at)


class PayingSettlementTests(WithdrawReviewTestBase):
    """评审 B1：`paying` 的两层语义（在途窗口 / 上游已受理）与人工兜底的边界。"""

    def _paying(self, order, *, started_at):
        WithdrawOrder.objects.filter(pk=order.pk).update(
            state=WithdrawOrder.STATE_PAYING,
            mch_order_no=f"TW{order.pk}-abcdef",
            transfer_started_at=started_at,
        )
        order.refresh_from_db()
        return order

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_manual_mark_is_blocked_while_the_http_request_is_in_flight(self, query):
        """在途窗口（<120 秒）⇒ 直接拒，⛔ 连查单都不发。"""
        order = self._paying(self._approved("88.00"), started_at=timezone.now())

        body = self._body(f"{order.pk}/mark-paid/", {"transfer_no": "ALI-1"})

        self.assertEqual(body["code"], ErrorCode.CONFLICT, body)
        self.assertIn("正在发起代付", body["msg"])
        query.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_manual_mark_queries_upstream_when_the_window_expired(self, query):
        """窗口已过 ⇒ 先查单：上游 state=1 时拒（仍在途）。"""
        order = self._paying(
            self._approved("88.00"),
            started_at=timezone.now() - timedelta(seconds=121),
        )
        query.return_value = {"state": 1, "transferId": "T-1"}

        body = self._body(f"{order.pk}/mark-paid/", {"transfer_no": "ALI-1"})

        self.assertEqual(body["code"], ErrorCode.CONFLICT, body)
        self.assertIn("仍在进行中", body["msg"])
        query.assert_called_once_with(order.mch_order_no)
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAYING)

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_manual_mark_is_rejected_when_upstream_already_succeeded(self, query):
        """上游 `state=2` ⇒ 本地落 `paid` 再拒（⛔ 不许人工再打一次）。"""
        order = self._paying(self._approved("88.00"), started_at=None)
        query.return_value = {"state": 2, "transferId": "T-1"}

        body = self._body(f"{order.pk}/mark-paid/", {"transfer_no": "ALI-1"})

        self.assertEqual(body["code"], ErrorCode.CONFLICT, body)
        self.assertIn("已成功", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAID)
        self.assertIsNone(order.transfer_started_at)

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_manual_mark_refunds_and_rejects_when_upstream_failed(self, query):
        """上游 `state=3` ⇒ 先落 `failed` + 退款，再拒人工登记。"""
        order = self._paying(self._approved("30.00"), started_at=None)
        query.return_value = {"state": 3, "errMsg": "账户名不匹配", "errCode": "E1"}
        balance_before = get_wallet(self.taker).balance

        body = self._body(f"{order.pk}/mark-paid/", {"transfer_no": "ALI-1"})

        self.assertEqual(body["code"], ErrorCode.CONFLICT, body)
        self.assertIn("已失败", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_FAILED)
        self.assertEqual(get_wallet(self.taker).balance, balance_before + Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_WITHDRAW_REFUND, ref_no=f"W{order.pk}"
            ).count(),
            1,
        )

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_manual_mark_is_allowed_when_the_query_says_no_such_order(self, query):
        """查单报错 / 上游查不到 ⇒ 代付没发出去，允许人工兜底；渠道回到 manual。"""
        order = self._paying(self._approved("88.00"), started_at=None)
        query.side_effect = JeepayError("Jeepay 代付失败：订单不存在（code=1002）")

        body = self._body(f"{order.pk}/mark-paid/", {"transfer_no": "ALI-1"})

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["state"], "paid")
        order.refresh_from_db()
        self.assertEqual(order.channel, WithdrawOrder.CHANNEL_MANUAL)
        self.assertIn("ALI-1", order.remark)
        self.assertIsNotNone(order.transfer_id, "上游单号要留痕")
        self.assertIsNone(order.transfer_started_at)

    def test_the_list_flags_an_in_flight_transfer_and_stops_after_120_seconds(self):
        in_flight = self._paying(self._approved("11.00"), started_at=timezone.now())
        expired = self._paying(
            self._approved("12.00"),
            started_at=timezone.now() - timedelta(seconds=121),
        )
        idle = self._paying(self._approved("13.00"), started_at=None)

        body = self.admin_client.get(f"{BASE_URL}?page_size=20").json()

        flags = {row["id"]: row["transfer_inflight"] for row in body["data"]["items"]}
        self.assertIs(flags[in_flight.pk], True)
        self.assertIs(flags[expired.pk], False)
        self.assertIs(flags[idle.pk], False)


class RefundIdempotencyTests(WithdrawReviewTestBase):
    """评审 A5：`_refund_withdraw` 自己拿单据行锁，且**连调两次只退一次**。"""

    def test_calling_refund_twice_credits_the_balance_once(self):
        order = self._pending("30.00")
        balance_after_withdraw = get_wallet(self.taker).balance

        first = _refund_withdraw(order, operator=self.admin, note="直连测试")
        second = _refund_withdraw(order, operator=self.admin, note="直连测试（重入）")

        self.assertIs(first, True)
        self.assertIs(second, False)
        self.assertEqual(
            get_wallet(self.taker).balance, balance_after_withdraw + Decimal("30.00")
        )
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_WITHDRAW_REFUND, ref_no=f"W{order.pk}"
            ).count(),
            1,
        )


class ListRefreshBudgetTests(WithdrawReviewTestBase):
    """评审 A4：列表兜底查单每页最多 5 条；状态真的变了才补系统审计。"""

    def _paying_orders(self, count):
        return [
            WithdrawOrder.objects.create(
                user=self.taker,
                amount=Decimal("1.00"),
                alipay_account="taker@example.com",
                alipay_name="打手甲",
                state=WithdrawOrder.STATE_PAYING,
                mch_order_no=f"TW{1000 + i}-abcdef",
            )
            for i in range(count)
        ]

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_only_five_upstream_queries_are_made_per_page(self, query):
        self._paying_orders(20)
        query.return_value = {"state": 1, "transferId": "T-1"}

        body = self.admin_client.get(f"{BASE_URL}?page_size=20").json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(len(body["data"]["items"]), 20)
        self.assertLessEqual(query.call_count, 5, "兜底查单没有预算（每页最多 5 条）")
        self.assertEqual(query.call_count, 5)

    @mock.patch("apps.accounts.wallet.query_transfer")
    def test_a_state_change_during_the_fallback_writes_a_system_audit(self, query):
        order = self._paying_orders(1)[0]
        query.return_value = {"state": 2, "transferId": "T-1"}

        body = self.admin_client.get(f"{BASE_URL}?page_size=20").json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"][0]["state"], "paid")
        rows = OperationLog.objects.filter(
            action="finance.withdraw_refresh", target_id=str(order.pk)
        )
        self.assertEqual(rows.count(), 1, "自动查单改了状态却没有系统审计")
        row = rows.get()
        self.assertIsNone(row.actor_id)
        self.assertTrue(row.summary.startswith("【系统】"), row.summary)
        self.assertIn("自动查单", row.reason)
        self.assertIn("已打款", row.reason)


class StateMachineAndValidationTests(WithdrawReviewTestBase):
    """计划 §7 第 4 / 5 条：非法状态与缺参一律业务码，⛔ 不改数据。"""

    def test_mark_paid_without_a_transfer_no_is_a_business_error(self):
        """④ 缺打款单号 ⇒ 1000，且单子状态不变。"""
        order = self._approved("88.00")

        body = self._body(f"{order.pk}/mark-paid/", {})

        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)

    def test_reject_without_a_reason_is_a_business_error(self):
        """④ 缺驳回原因 ⇒ 1000，且一分钱都不退。"""
        order = self._pending("88.00")
        balance_before = get_wallet(self.taker).balance

        body = self._body(f"{order.pk}/reject/", {})

        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PENDING)
        self.assertEqual(get_wallet(self.taker).balance, balance_before)

    def test_non_pending_cannot_be_approved(self):
        """⑤ 非 `pending` 不能 approve。"""
        order = self._approved("88.00")

        body = self._body(f"{order.pk}/approve/")

        self.assertEqual(body["code"], ErrorCode.CONFLICT, body)
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)

    def test_paid_cannot_be_rejected_marked_paid_or_approved(self):
        """⑤ `paid` 不可逆：驳回 / 重新登记 / 再通过都拒，钱不动。"""
        order = self._approved("88.00")
        WithdrawOrder.objects.filter(pk=order.pk).update(
            state=WithdrawOrder.STATE_PAID, paid_at=timezone.now()
        )
        order.refresh_from_db()
        balance_before = get_wallet(self.taker).balance
        refunds_before = WalletTransaction.objects.filter(
            kind=WalletTransaction.KIND_WITHDRAW_REFUND
        ).count()

        for action, payload in (
            ("reject", {"remark": "想退回去"}),
            ("mark-paid", {"transfer_no": "ALI-1"}),
            ("approve", {}),
            # 评审建议 8：已打款的单没有可发起 / 可刷新的东西。
            ("transfer", {"confirm_amount": "88.00"}),
            ("refresh", {}),
        ):
            with self.subTest(action=action):
                body = self._body(f"{order.pk}/{action}/", payload)
                self.assertEqual(body["code"], ErrorCode.CONFLICT, body)

        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_PAID)
        self.assertEqual(get_wallet(self.taker).balance, balance_before)
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_WITHDRAW_REFUND
            ).count(),
            refunds_before,
        )

    def test_switched_off_auto_transfer_blocks_the_endpoint(self):
        """开关默认关 ⇒ `/transfer/` 直接拒（页面只出现「登记打款」）。"""
        order = self._approved("88.00")
        with override_settings(WITHDRAW_AUTO_TRANSFER=False):
            body = self._body(f"{order.pk}/transfer/", {"confirm_amount": "88.00"})
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertIn("自动代付未开启", body["msg"])

    def test_the_list_exposes_the_switch_and_the_limit_as_a_string(self):
        """列表多带两个开关位（计划 §4.3.1）：布尔 + 两位小数字符串。"""
        self._pending("12.00")

        body = self.admin_client.get(BASE_URL).json()

        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["auto_transfer_enabled"], True)
        self.assertEqual(body["data"]["transfer_max_amount"], "1000.00")
        self.assertEqual(body["data"]["total"], 1)
        row = body["data"]["items"][0]
        # 行对象金额一律字符串（同钱包端点口径）。
        self.assertEqual(row["amount"], "12.00")
        self.assertIsInstance(row["amount"], str)
        self.assertEqual(row["state_text"], "待审核")
        self.assertEqual(row["channel"], "manual")
        # 契约里的键一个都不能少（前端按它渲染）。
        self.assertEqual(
            set(row),
            {
                "id", "user_id", "username", "display_name", "amount", "alipay_account",
                "alipay_name", "state", "state_text", "channel", "transfer_id",
                "transfer_error", "transfer_inflight", "remark", "created_at",
                "reviewed_at", "paid_at",
                # ⭐ 超时自动审核 / 快路自动通过的单没有 reviewer
                #    ⇒ 行对象给前端两个可直说的键（「系统」/ 审核人名字）。
                "auto_approved", "reviewer_name",
                # ⭐ 前三单快路：列表要能标出快路单与预计打款时间。
                "fast_lane", "payout_after",
                # ⭐ 财务中断：只对 interrupted 有值。
                "interrupted_at", "interrupt_reason",
            },
        )

    def test_the_list_row_stamps_moments_in_the_project_timezone(self):
        """D-0302（2026-10-24）：提现审核行的六个时刻键是**项目时区**（+08:00）ISO 串。

        管理端提现页「切字符」直显 ⇒ 裸 datetime 经信封渲染成 UTC 串时整列差 8 小时。
        夹具把单子时刻钉在固定值（UTC 04:30 ⇒ 北京墙钟 12:30）；可空列回 null（形状不变）。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        order = self._pending("12.00")
        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        WithdrawOrder.objects.filter(pk=order.pk).update(
            created_at=fixed,
            payout_after=fixed,
            reviewed_at=fixed,
            paid_at=fixed,
            interrupted_at=None,
        )
        row = self.admin_client.get(BASE_URL).json()["data"]["items"][0]
        self.assertEqual(row["created_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["payout_after"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["reviewed_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["paid_at"], "2026-10-24T12:30:00+08:00")
        self.assertIsNone(row["interrupted_at"])

    def test_list_filters_by_state_and_rejects_an_unknown_state_with_1000(self):
        self._pending("12.00")
        self._approved("13.00")

        filtered = self.admin_client.get(f"{BASE_URL}?state=approved").json()
        self.assertEqual(filtered["code"], 0, filtered)
        self.assertEqual(filtered["data"]["total"], 1)
        self.assertEqual(filtered["data"]["items"][0]["amount"], "13.00")

        # 「转账失败」tab 的前端契约：`state=failed` 必须真的能筛（计划 §5）。
        failed = WithdrawOrder.objects.create(
            user=self.taker,
            amount=Decimal("14.00"),
            alipay_account="taker@example.com",
            alipay_name="打手甲",
            state=WithdrawOrder.STATE_FAILED,
        )
        failed_body = self.admin_client.get(f"{BASE_URL}?state=failed").json()
        self.assertEqual(failed_body["code"], 0, failed_body)
        self.assertEqual(failed_body["data"]["total"], 1)
        self.assertEqual(failed_body["data"]["items"][0]["id"], failed.pk)

        # 非法值照现有列表口径：HTTP 200 + 业务码 1000，⛔ 不是 500。
        bad = self.admin_client.get(f"{BASE_URL}?state=not-a-state").json()
        self.assertEqual(bad["code"], ErrorCode.VALIDATION, bad)


class PermissionGateTests(WithdrawReviewTestBase):
    """计划 §7 第 8 条：无节点 6 个端点全 403（fail-closed，不是界面藏起来）。"""

    def test_all_six_endpoints_are_403_without_the_node(self):
        order = self._approved("88.00")
        outsider = make_logged_in_client(
            username="tw-no-node", password="pw-tw-no-node"
        )

        self.assertEqual(outsider.get(BASE_URL).status_code, 403)
        for action, payload in (
            ("approve", {}),
            ("reject", {"remark": "不行"}),
            ("transfer", {"confirm_amount": "88.00"}),
            ("refresh", {}),
            ("mark-paid", {"transfer_no": "ALI-1"}),
        ):
            with self.subTest(action=action):
                response = self._post(f"{order.pk}/{action}/", payload, client=outsider)
                self.assertEqual(
                    response.status_code, 403,
                    f"{action} 没有节点也放行了（fail-closed 破了）",
                )
        # 没有节点的人连一行数据都没改到。
        order.refresh_from_db()
        self.assertEqual(order.state, WithdrawOrder.STATE_APPROVED)


class CrossTenantWithdrawTests(TenantTestCase):
    """计划 §7 第 9 条：以 A 的会话查 B 的单子 ⇒ 404。

    ⚠️ **判据用「租户内唯一、且两租户取值不同」的字段**（支付宝账号），⛔ 不用主键指认
    「另一租户的那一行」（AGENTS §5-G：主键在两个 schema 里可以撞成同一串数字）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="tw_tenant_b", name="租户 B", domain="tw-b.test.local"
        )

    def setUp(self):
        overrides = override_settings(**JEEPAY)
        overrides.enable()
        self.addCleanup(overrides.disable)

        # A：当前 schema（TenantTestCase 的默认租户）。
        self.taker_a = User.objects.create_user(username="tw-a-taker", password="pw-a")
        StaffProfile.objects.create(
            user=self.taker_a, alipay_account="only-a@example.com", alipay_name="甲"
        )
        # ⭐ 评审建议 6：A 里也建**同一个 pk=4242** 的单，与 B 的 pk=4242 形成**双向对照**。
        #    主键在两个 schema 里可以撞成同一串数字 ⇒ 判据必须落在「租户内唯一且两租户
        #    取值不同」的字段（支付宝账号）上，⛔ 不能用 pk 指认「那是 B 的行」。
        self.order_a = WithdrawOrder.objects.create(
            pk=4242,
            user=self.taker_a,
            amount=Decimal("10.00"),
            alipay_account="only-a@example.com",
            alipay_name="甲",
        )
        self.client_a = make_logged_in_client(
            username="tw-a-admin", password="pw-a-admin", groups=("客服",), nodes=(NODE,)
        )

        # B：另一个租户 schema。同 pk 一张（对照用）+ 本租户不存在的 pk 一张（404 用）。
        with tenant_context(self.tenant_b):
            user_b = User.objects.create_user(username="tw-b-taker", password="pw-b")
            self.order_b = WithdrawOrder.objects.create(
                pk=4242,
                user=user_b,
                amount=Decimal("20.00"),
                alipay_account="only-b@example.com",
                alipay_name="乙",
            )
            self.order_b_other = WithdrawOrder.objects.create(
                pk=9999,
                user=user_b,
                amount=Decimal("21.00"),
                alipay_account="only-b-2@example.com",
                alipay_name="乙二",
            )

    def test_same_pk_actions_touch_a_row_and_never_b(self):
        """⭐ 评审建议 6：A 与 B 各有一张 pk=4242，A 的 `/4242/approve/` 只能改 A 那张。"""
        body = self._post_with_a("4242/approve/")
        self.assertEqual(body["code"], 0, body)

        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.state, WithdrawOrder.STATE_APPROVED)
        with tenant_context(self.tenant_b):
            b_row = WithdrawOrder.objects.get(pk=4242)
            self.assertEqual(b_row.state, WithdrawOrder.STATE_PENDING)
            self.assertEqual(b_row.alipay_account, "only-b@example.com")

    def _post_with_a(self, path, payload=None):
        response = self.client_a.post(
            f"{BASE_URL}{path}", payload or {}, content_type="application/json"
        )
        return response.json()

    def test_actions_on_an_id_that_does_not_exist_in_a_are_not_found(self):
        # 只有 B 才有的 pk=9999 ⇒ 对 A 就是「不存在」。
        for action, payload in (
            ("approve", {}),
            ("reject", {"remark": "跨租户"}),
            ("transfer", {"confirm_amount": "21.00"}),
            ("refresh", {}),
            ("mark-paid", {"transfer_no": "ALI-B"}),
        ):
            with self.subTest(action=action):
                response = self.client_a.post(
                    f"{BASE_URL}{self.order_b_other.pk}/{action}/",
                    payload,
                    content_type="application/json",
                )
                # ⚠️ 本项目口径（D4 / `apps/core/exceptions.py`）：`get_object_or_404` 的
                # 「不存在」落 **HTTP 200 + 业务码 1002**，不是传输层 404。语义仍是
                # 「这张单对你不可见」——与「不存在」同一个回答。
                body = response.json()
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
                self.assertIsNone(body["data"])
                # ⛔ 不是 403：不给「这张单存在但不属于你」的差异。
                self.assertNotIn("only-b", response.content.decode())

    def test_the_list_never_contains_the_other_tenants_rows(self):
        body = self.client_a.get(BASE_URL).json()
        self.assertEqual(body["code"], 0, body)
        accounts = [row["alipay_account"] for row in body["data"]["items"]]
        # 正向对照：A 自己的行**在**（否则下面的否定断言可能是「列表本来就空」）。
        self.assertIn("only-a@example.com", accounts)
        self.assertNotIn("only-b@example.com", accounts)
        self.assertNotIn("only-b-2@example.com", accounts)


class AuditLedgerTests(WithdrawReviewTestBase):
    """计划 §7 第 10 条：5 个写动作各落一行操作日志。"""

    def _logs(self, action):
        return OperationLog.objects.filter(action=action)

    @mock.patch("apps.accounts.wallet.query_transfer")
    @mock.patch("apps.accounts.wallet.create_transfer")
    def test_each_write_action_writes_exactly_one_log_row(self, create, query):
        create.return_value = {"state": 1, "transferId": "T-1"}
        query.return_value = {"state": 2, "transferId": "T-1"}

        pending_for_approve = self._pending("11.00")
        self.assertEqual(self._body(f"{pending_for_approve.pk}/approve/")["code"], 0)

        pending_for_reject = self._pending("12.00")
        self.assertEqual(
            self._body(f"{pending_for_reject.pk}/reject/", {"remark": "账号不对"})["code"], 0
        )

        approved_for_transfer = self._approved("13.00")
        self.assertEqual(
            self._body(
                f"{approved_for_transfer.pk}/transfer/", {"confirm_amount": "13.00"}
            )["code"],
            0,
        )

        self.assertEqual(
            self._body(f"{approved_for_transfer.pk}/refresh/")["code"], 0
        )

        approved_for_mark = self._approved("14.00")
        self.assertEqual(
            self._body(
                f"{approved_for_mark.pk}/mark-paid/", {"transfer_no": "ALI-20261020"}
            )["code"],
            0,
        )

        for action in (
            "finance.withdraw_approve",
            "finance.withdraw_reject",
            "finance.withdraw_transfer",
            "finance.withdraw_refresh",
            "finance.withdraw_mark_paid",
        ):
            with self.subTest(action=action):
                rows = self._logs(action)
                self.assertEqual(rows.count(), 1, f"{action} 应当恰好一行")
                row = rows.get()
                self.assertEqual(row.actor_id, self.admin.pk)
                self.assertEqual(row.actor, "tw-admin")

        # 留痕内容可读（不是空 summary）：金额是字符串形态。
        self.assertIn("13.00", self._logs("finance.withdraw_transfer").get().summary)
        self.assertIn("ALI-20261020", self._logs("finance.withdraw_mark_paid").get().summary)
        self.assertIn("账号不对", self._logs("finance.withdraw_reject").get().reason)


class WalletAuditTests(TenantTestCase):
    """计划 §4.5：钱包三条写（解冻 / 提现申请 / 充值下单）补埋点后各落一行。"""

    MOBILE = "13800007777"
    PASSWORD = "pw-wallet-audit"

    def setUp(self):
        overrides = override_settings(**JEEPAY)
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.user = User.objects.create_user(username=self.MOBILE, password=self.PASSWORD)
        StaffProfile.objects.create(
            user=self.user,
            mobile=self.MOBILE,
            alipay_account="wallet-audit@example.com",
            alipay_name="钱包甲",
        )
        self.domain = self.tenant.get_primary_domain().domain
        self.client = Client(SERVER_NAME=self.domain)
        token = self.client.post(
            "/api/app/auth/login/",
            {"username": self.MOBILE, "password": self.PASSWORD},
            content_type="application/json",
        ).json()["data"]["token"]
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _wallet(self):
        return get_wallet(self.user)

    def _logs(self, action):
        return OperationLog.objects.filter(action=action)

    def test_release_writes_one_log_row(self):
        wallet = self._wallet()
        wallet.balance = Decimal("100.00")
        wallet.frozen = Decimal("50.00")
        wallet.save(update_fields=["balance", "frozen", "updated_at"])

        body = self.client.post(
            "/api/app/wallet/release/", content_type="application/json", **self.auth
        ).json()

        self.assertEqual(body["code"], 0, body)
        row = self._logs("wallet.release").get()
        self.assertEqual(row.actor_id, self.user.pk)
        self.assertIn("50.00", row.summary)

    def test_withdraw_writes_one_log_row(self):
        wallet = self._wallet()
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance", "updated_at"])

        body = self.client.post(
            "/api/app/wallet/withdraw/",
            {"amount": "30.00"},
            content_type="application/json",
            **self.auth,
        ).json()

        self.assertEqual(body["code"], 0, body)
        row = self._logs("wallet.withdraw").get()
        self.assertEqual(row.actor_id, self.user.pk)
        self.assertIn("30.00", row.summary)

    @mock.patch("apps.accounts.app_wallet_views.create_payment")
    def test_deposit_writes_one_log_row(self, create_payment):
        create_payment.return_value = {"payDataType": "wxapp", "payData": "{}"}

        body = self.client.post(
            "/api/app/wallet/deposit/",
            {"amount": "12.00", "way_code": "WX_APP"},
            content_type="application/json",
            **self.auth,
        ).json()

        self.assertEqual(body["code"], 0, body)
        create_payment.assert_called_once()          # 桩真的接上了
        row = self._logs("wallet.deposit").get()
        self.assertEqual(row.actor_id, self.user.pk)
        self.assertIn("12.00", row.summary)
        # 一次成功的充值下单只记一行（回调入账是机器对机器，不进操作日志）。
        self.assertEqual(self._logs("wallet.deposit").count(), 1)


class WithdrawDefaultSettingsTests(SimpleTestCase):
    """评审建议 8：两个开关的**默认值**（现在所有行为用例都 override 了，改默认值不会红）。

    ⚠️ 这条钉的是 `config/settings.py` 的 env 默认值，不是某个测试类的 override。
    """

    def test_auto_transfer_defaults_to_off(self):
        self.assertIs(
            settings.WITHDRAW_AUTO_TRANSFER, False,
            "WITHDRAW_AUTO_TRANSFER 的默认值必须是 False（上线前只能人工打款）",
        )

    def test_transfer_max_amount_defaults_to_1000(self):
        self.assertEqual(
            Decimal(str(settings.WITHDRAW_TRANSFER_MAX_AMOUNT)), Decimal("1000"),
            "WITHDRAW_TRANSFER_MAX_AMOUNT 的默认值必须是 1000 元",
        )
