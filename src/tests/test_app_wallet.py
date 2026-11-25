"""打手钱包（押金冻结 / 解冻 / 提现 / 扣款，D-0269 P2，2026-10-19）。

## 钉住什么

1. **押金是账号级的**：同游戏多单只冻一次；
2. **不足就拒**：余额不够补差额时，抢单以 `GRAB_LIMIT_EXCEEDED` 失败（与其它抢单限制同码）；
3. **解冻条件**：有进行中订单时必须拒；
4. **每次金额变化都有流水**（断言数据库行，不是断言函数返回值）。

## ⚠️ `DEPOSIT_ENFORCED` 默认关

它是逐租户打开的闸（见 `settings.py` 与计划 §6）。本文件的抢单用例都显式
`override_settings(DEPOSIT_ENFORCED=True)`——这样「闸开着的形状」是被测过的，
而其它几百条抢单测试不受新规则影响。
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.db import transaction
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import (
    AppToken,
    StaffProfile,
    TakerWallet,
    WalletTransaction,
    WithdrawOrder,
)
from apps.accounts.wallet import (
    WalletError,
    deposit_amount_for_game,
    ensure_deposit_for_claim,
    get_wallet,
    reject_withdraw,
    request_release,
    request_withdraw,
)
from apps.business.claim import ClaimError, claim_order
from apps.business.deposit_gate import deposit_enforced_for
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import make_logged_in_client
from apps.business.models import (
    EmpType,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Game,
    GameDepositRule,
    Order,
    OrderStatus,
    Performance,
)
from apps.core.error_codes import ErrorCode

CLAIM_NODES = {"grab_hall.take_order"}


def _no(n):
    return f"20261019{n:012d}"


class WalletTestCase(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="wallet-taker", password="pw-wallet")
        profile = StaffProfile.objects.create(
            user=self.taker,
            mobile="13700001111",
            alipay_account="wallet@example.com",
            alipay_name="测试打手",
        )
        # 钱包端点按「我的游戏」列押金要求：不勾游戏时 `deposit_rules` 是空表（不是缺陷）。
        profile.games.set([self.game])

    # ── 工具 ──────────────────────────────────────────────────────
    def _order(self, no, status=OrderStatus.WAITING_GRAB):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _credit(self, amount):
        """直接改库造余额（充值端点在另一批，本文件只测钱包语义）。"""
        wallet = get_wallet(self.taker)
        wallet.balance += Decimal(amount)
        wallet.save(update_fields=["balance", "updated_at"])
        return wallet

    def _claim(self, order):
        with transaction.atomic():
            claim_order(user_id=self.taker.id, order_id=order.id, nodes=CLAIM_NODES)

    # ── 读 ────────────────────────────────────────────────────────
    def test_wallet_starts_empty(self):
        # ⚠️ 端点的鉴权由 `test_app_auth` 覆盖；本用例只验证钱包的初始值。
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, Decimal("0"))
        self.assertEqual(wallet.frozen, Decimal("0"))

    def test_deposit_rule_defaults_to_50(self):
        self.assertEqual(deposit_amount_for_game(self.game), Decimal("50"))

    def test_deposit_rule_can_be_overridden(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self.assertEqual(deposit_amount_for_game(self.game), Decimal("300"))

    # ── 接单冻结 ──────────────────────────────────────────────────
    @override_settings(DEPOSIT_ENFORCED=True)
    def test_claim_without_money_is_rejected(self):
        order = self._order(_no(1))
        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)  # 没接走

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_claim_freezes_exactly_the_required_amount(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self._credit("350.00")
        self._claim(self._order(_no(2)))
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("300"))
        self.assertEqual(wallet.balance, Decimal("50"))
        # 流水：一行 FROZEN，快照与钱包一致。
        tx = WalletTransaction.objects.get(kind=WalletTransaction.KIND_FREEZE)
        self.assertEqual(tx.amount, Decimal("300"))
        self.assertEqual(tx.frozen_after, Decimal("300"))
        self.assertEqual(tx.balance_after, Decimal("50"))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_second_claim_on_the_same_game_does_not_freeze_again(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self._credit("300.00")
        self._claim(self._order(_no(3)))
        self._claim(self._order(_no(4)))
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("300"))
        self.assertEqual(wallet.balance, Decimal("0"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_FREEZE).count(), 1
        )

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_a_cheaper_game_is_covered_by_the_existing_freeze(self):
        """已经冻了 300 的人接 50 的游戏：不再补冻（业务方口径「超过要求金额就可以接单」）。"""
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self._credit("300.00")
        self._claim(self._order(_no(5)))
        cheap = Game.objects.create(name="小游戏")
        order = Order.objects.create(
            game=cheap, order_no=_no(6), status=OrderStatus.WAITING_GRAB, hall_price=Decimal("10")
        )
        Performance.objects.create(order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("10"))
        self._claim(order)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("300"))

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_deposit_is_ignored_when_the_switch_is_off(self):
        """开关关着时，没有一分钱的打手也能接单（既有几百条抢单测试赖以不变的口径）。"""
        self._claim(self._order(_no(7)))
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("0"))

    # ── 解冻申请（起是「申请 + 7 天冷静期」，选项 A）──────────
    @override_settings(DEPOSIT_ENFORCED=True)
    def test_release_is_rejected_while_an_order_is_active(self):
        self._credit("50.00")
        # 走真实抢单：`request_release` 的判据是「MAIN 行的 user = 我」，直接改状态不写 user
        # 会被判成「没有进行中的单」——那正是本用例要防的形状。
        self._claim(self._order(_no(8)))
        with self.assertRaises(WalletError) as ctx:
            request_release(self.taker)
        self.assertIn("进行中的订单", str(ctx.exception))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_release_request_keeps_the_money_frozen_until_the_hold_expires(self):
        """选项 A：申请那一刻**一分不动**（钱还在 `frozen` 里），只留一条「申请」流水。

        ⚠️ 7 天口径/到账结算在 `tests/test_wallet_release_hold.py`；本用例只钉「申请不动钱」
        这个与本文件既有覆盖相邻的入口行为（旧实现会立刻把 50 元转进余额）。
        """
        self._credit("50.00")
        order = self._order(_no(9))
        self._claim(order)
        order.refresh_from_db()
        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status"])

        wallet, created = request_release(self.taker)
        self.assertTrue(created)
        self.assertEqual(wallet.frozen, Decimal("50"))
        self.assertEqual(wallet.balance, Decimal("0"))
        self.assertIsNotNone(wallet.release_requested_at)
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_RELEASE_REQUEST
            ).count(),
            1,
        )
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 0
        )

    def _claiming_mirror(self, status):
        owner = User.objects.create_user(username="release-owner", password="pw-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url="https://up.example.test",
            account="up-rel", password="up-pw", enabled=True,
        )
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://up.example.test",
            upstream_order_id="up-rel-1",
            owner_binding=binding,
            status=status,
            game=self.game,
            project_text="上家单",
            remote_rec_fee=Decimal("88.00"),
            upstream_payload={},
            first_seen_at=timezone.now(),
            claiming_by=self.taker if status == JidanpuUpstreamHallOrder.Status.CLAIMING else None,
            claiming_at=timezone.now() if status == JidanpuUpstreamHallOrder.Status.CLAIMING else None,
        )

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_release_is_rejected_while_an_upstream_claim_is_still_claiming(self):
        """评审 2026-10-20 Blocking：claiming 的镜像行也算「占着单」，不能解冻。"""
        self._credit("50.00")
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)
        mirror = self._claiming_mirror(JidanpuUpstreamHallOrder.Status.CLAIMING)
        with self.assertRaises(WalletError) as ctx:
            request_release(self.taker)
        self.assertIn("上家单", str(ctx.exception))
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50"))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_release_is_allowed_after_the_upstream_claim_is_gone(self):
        self._credit("50.00")
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)
        mirror = self._claiming_mirror(JidanpuUpstreamHallOrder.Status.CLAIMING)
        mirror.status = JidanpuUpstreamHallOrder.Status.GONE
        mirror.save(update_fields=["status"])
        wallet, created = request_release(self.taker)
        self.assertTrue(created)
        # 申请不改金额：钱仍在冻结额里，等满 7 天才回余额（选项 A）。
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))

    def test_release_without_a_freeze_is_rejected(self):
        with self.assertRaises(WalletError):
            request_release(self.taker)

    # ── 提现 ──────────────────────────────────────────────────────
    def test_withdraw_deducts_the_balance_and_creates_a_pending_order(self):
        self._credit("100.00")
        order = request_withdraw(self.taker, Decimal("30"))
        self.assertEqual(order.state, "pending")
        self.assertEqual(order.alipay_account, "wallet@example.com")
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, Decimal("70"))
        tx = WalletTransaction.objects.get(kind=WalletTransaction.KIND_WITHDRAW)
        self.assertEqual(tx.amount, Decimal("30"))

    def test_withdraw_more_than_the_balance_is_rejected(self):
        self._credit("10.00")
        with self.assertRaises(WalletError):
            request_withdraw(self.taker, Decimal("10.01"))
        self.assertEqual(get_wallet(self.taker).balance, Decimal("10"))

    def test_withdraw_requires_an_alipay_account(self):
        profile = StaffProfile.objects.get(user=self.taker)
        profile.alipay_account = ""
        profile.save(update_fields=["alipay_account"])
        self._credit("100.00")
        with self.assertRaises(WalletError) as ctx:
            request_withdraw(self.taker, Decimal("10"))
        self.assertIn("支付宝", str(ctx.exception))

    def test_a_small_amount_below_one_yuan_is_allowed(self):
        """⭐ 2026-10-23：不设提现门槛 ⇒ 0.5 元也能提（旧口径是「低于 1 元拒绝」）。

        小程序审核按「余额能不能提干净」判，⛔ 别把 1 元下限抬回来。
        """
        self._credit("100.00")
        order = request_withdraw(self.taker, Decimal("0.5"))
        self.assertEqual(order.amount, Decimal("0.50"))
        self.assertEqual(get_wallet(self.taker).balance, Decimal("99.50"))


    # ── 补充用例──────────────────────────────────────
    def test_wallet_endpoint_requires_auth(self):
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        self.assertEqual(client.get("/api/app/wallet/").status_code, 401)

    def test_wallet_endpoint_returns_strings(self):
        """评审 Major 2：金额必须是**字符串**（`api_response` 会把 Decimal 变 float）。"""
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        token = client.post(
            "/api/app/auth/login/",
            {"username": "wallet-taker", "password": "pw-wallet"},
            content_type="application/json",
        ).json()["data"]["token"]
        body = client.get("/api/app/wallet/", HTTP_AUTHORIZATION=f"Bearer {token}").json()
        self.assertEqual(body["code"], 0)
        self.assertIsInstance(body["data"]["balance"], str)
        self.assertIsInstance(body["data"]["frozen"], str)
        self.assertEqual(body["data"]["balance"], "0.00")
        self.assertEqual([r["amount"] for r in body["data"]["deposit_rules"]], ["50.00"])

    def _app_client_with_token(self):
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        token = client.post(
            "/api/app/auth/login/",
            {"username": "wallet-taker", "password": "pw-wallet"},
            content_type="application/json",
        ).json()["data"]["token"]
        return client, token

    def test_app_payload_moments_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：App 钱包/提现/流水三个端点的时刻键是**项目时区**（+08:00）。

        App **截串直显**（`app/utils/format.uts` 按 `MM-DD HH:mm` 切）⇒ 裸 datetime 经
        信封渲染成 UTC 串时整页差 8 小时。夹具把时刻钉在 UTC 04:30 ⇒ 北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        # 一条流水 + 一张提现单 + 余额变动先落库（`_credit` 的 save 会刷 auto_now 的
        # `updated_at` ⇒ 钱包列的钉值必须放在它之后）。
        WalletTransaction.objects.create(
            wallet=get_wallet(self.taker),
            kind=WalletTransaction.KIND_RECHARGE,
            amount=Decimal("30.00"),
            balance_after=Decimal("30.00"),
            frozen_after=Decimal("0.00"),
        )
        WalletTransaction.objects.filter(
            wallet__user=self.taker, kind=WalletTransaction.KIND_RECHARGE,
        ).update(created_at=fixed)
        self._credit("30.00")
        request_withdraw(self.taker, Decimal("10.00"))
        WithdrawOrder.objects.filter(user=self.taker).update(
            created_at=fixed, payout_after=fixed,
        )
        TakerWallet.objects.filter(user=self.taker).update(updated_at=fixed)

        client, token = self._app_client_with_token()
        wallet = client.get(
            "/api/app/wallet/", HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()["data"]
        self.assertEqual(wallet["updated_at"], "2026-10-24T12:30:00+08:00")

        txs = client.get(
            "/api/app/wallet/transactions/", HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()["data"]["items"]
        # 按 kind 认行（列表里还有 request_withdraw 落的那条「提现」流水，时刻是 now）。
        recharge = next(t for t in txs if t["kind"] == WalletTransaction.KIND_RECHARGE)
        self.assertEqual(recharge["created_at"], "2026-10-24T12:30:00+08:00")

        withdrawals = client.get(
            "/api/app/wallet/withdrawals/", HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()["data"]["items"]
        self.assertEqual(withdrawals[0]["created_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(withdrawals[0]["payout_after"], "2026-10-24T12:30:00+08:00")

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_partial_topup_from_fifty_to_three_hundred(self):
        """评审 Major 4 / M3 存活：已冻 50 的人接 300 的游戏必须再补 250。"""
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self._credit("350.00")
        cheap = Game.objects.create(name="小游戏")
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, cheap)      # 冻 50
            ensure_deposit_for_claim(self.taker, self.game)  # 补到 300
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("300"))
        self.assertEqual(wallet.balance, Decimal("50"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_FREEZE).count(), 2
        )

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_partial_topup_rejected_when_balance_is_short(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("300"))
        self._credit("100.00")
        cheap = Game.objects.create(name="小游戏")
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, cheap)
            with self.assertRaises(WalletError):
                ensure_deposit_for_claim(self.taker, self.game)
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("50"))   # 失败的补冻不留半截
        self.assertEqual(wallet.balance, Decimal("50"))

    def test_withdraw_endpoint_rejects_nan(self):
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        token = client.post(
            "/api/app/auth/login/",
            {"username": "wallet-taker", "password": "pw-wallet"},
            content_type="application/json",
        ).json()["data"]["token"]
        body = client.post(
            "/api/app/wallet/withdraw/",
            {"amount": "NaN"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()
        self.assertEqual(body["code"], 1000)

    def test_withdraw_endpoint_rejects_three_decimals(self):
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        token = client.post(
            "/api/app/auth/login/",
            {"username": "wallet-taker", "password": "pw-wallet"},
            content_type="application/json",
        ).json()["data"]["token"]
        self._credit("100.00")
        body = client.post(
            "/api/app/wallet/withdraw/",
            {"amount": "10.001"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()
        self.assertEqual(body["code"], 1000)

    def test_reject_withdraw_returns_the_money(self):
        """评审 Major 8：驳回必须把钱退回余额，并留一条退回流水。"""
        self._credit("100.00")
        order = request_withdraw(self.taker, Decimal("30"))
        admin = User.objects.create_user(username="wallet-admin", password="pw-admin")
        reject_withdraw(order, operator=admin, remark="支付宝信息不对")
        order.refresh_from_db()
        self.assertEqual(order.state, "rejected")
        self.assertEqual(order.remark, "支付宝信息不对")
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_WITHDRAW_REFUND).count(), 1
        )

    def test_reject_withdraw_requires_a_reason(self):
        self._credit("100.00")
        order = request_withdraw(self.taker, Decimal("30"))
        admin = User.objects.create_user(username="wallet-admin2", password="pw-admin")
        with self.assertRaises(WalletError):
            reject_withdraw(order, operator=admin, remark="   ")

    def test_delta_falls_back_to_300_without_a_rule_row(self):
        """评审 Major 7：`provisioning` 是「先迁移、后建游戏」⇒ 新租户的 0077 建不出规则，
        读层必须自己兜底（否则新租户三角洲只收 50）。"""
        delta = Game.objects.create(name="三角洲行动")
        self.assertEqual(deposit_amount_for_game(delta), Decimal("300"))

    def test_a_rule_row_overrides_the_delta_fallback(self):
        """建了规则行就以规则行为准（后台可调的前提）。"""
        delta = Game.objects.create(name="三角洲行动")
        GameDepositRule.objects.create(game=delta, amount=Decimal("200"))
        self.assertEqual(deposit_amount_for_game(delta), Decimal("200"))


class DepositGateScopeTests(WalletTestCase):
    """App 与网页分开控制：`DEPOSIT_ENFORCED_APP` 只对 App 令牌生效。

    业务方：网页端（团团通）不想被这条闸影响，只要求 **App** 接单先冻押金。
    """

    def _app_request(self, token=None):
        if token is None:
            token = AppToken(
                user=self.taker, key_hash="not-a-real-hash",
                expires_at=timezone.now() + timedelta(days=1),
            )
        return SimpleNamespace(auth=token)

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_request_is_gated_when_the_app_switch_is_on(self):
        self.assertTrue(deposit_enforced_for(self._app_request()))

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_session_request_is_not_gated_by_the_app_switch(self):
        self.assertFalse(deposit_enforced_for(SimpleNamespace(auth=None)))

    @override_settings(DEPOSIT_ENFORCED=True, DEPOSIT_ENFORCED_APP=False)
    def test_the_master_switch_still_gates_both(self):
        self.assertTrue(deposit_enforced_for(SimpleNamespace(auth=None)))
        self.assertTrue(deposit_enforced_for(self._app_request()))

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=False)
    def test_both_switches_off_is_not_gated(self):
        self.assertFalse(deposit_enforced_for(self._app_request()))

    def _issue_app_token(self):
        from apps.business.peer_sync import hash_token, new_token

        raw = new_token()
        AppToken.objects.create(
            user=self.taker,
            key_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(days=1),
            client="app-android",
        )
        return raw

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_wallet_endpoint_reports_the_app_gate_to_the_client(self):
        """前端据 `deposit_enforced` 决定要不要弹冻结确认——⛔ 不让 App 猜开关。"""
        raw = self._issue_app_token()
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        data = client.get(
            "/api/app/wallet/", HTTP_AUTHORIZATION="Bearer " + raw
        ).json()["data"]
        self.assertTrue(data["deposit_enforced"])

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=False)
    def test_wallet_endpoint_reports_false_when_the_app_gate_is_off(self):
        raw = self._issue_app_token()
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        data = client.get(
            "/api/app/wallet/", HTTP_AUTHORIZATION="Bearer " + raw
        ).json()["data"]
        self.assertFalse(data["deposit_enforced"])

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_claim_order_honours_an_explicit_override(self):
        """调用方显式给 `enforce_deposit=False` 时，即使总闸开着也不冻（App 分开控制的实现基础）。"""
        order = self._order(_no(31))
        with transaction.atomic():
            claim_order(
                user_id=self.taker.id, order_id=order.id, nodes=CLAIM_NODES,
                enforce_deposit=False,
            )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("0"))

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_claim_order_honours_an_explicit_override_to_enforce(self):
        """总闸关着、调用方显式 `True` 时照样要冻（App 专属闸就是这样接进来的）。"""
        order = self._order(_no(32))
        with self.assertRaises(ClaimError) as ctx:
            with transaction.atomic():
                claim_order(
                    user_id=self.taker.id, order_id=order.id, nodes=CLAIM_NODES,
                    enforce_deposit=True,
                )
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", str(ctx.exception))


class AppOnlyDepositGateEndpointTests(TenantTestCase):
    """端到端：带 App 令牌打真实接单端点（评审 2026-10-20 #5 要求补的那批）。"""

    def setUp(self):
        self.domain = self.tenant.get_primary_domain().domain
        self.game = Game.objects.create(name="App闸游戏")
        self.web = make_logged_in_client(
            domain=self.domain,
            username="gate-taker",
            password="pw-gate-123",
            groups=("代练员",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )
        self.taker = User.objects.get(username="gate-taker")
        profile, _ = StaffProfile.objects.get_or_create(
            user=self.taker, defaults={"mobile": "13700005555"}
        )
        if profile.mobile != "13700005555":
            profile.mobile = "13700005555"
            profile.save(update_fields=["mobile"])
        raw = new_token()
        AppToken.objects.create(
            user=self.taker,
            key_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(days=1),
            client="app-android",
        )
        self.token = raw

    def _order(self, no):
        order = Order.objects.create(
            game=self.game, order_no=no, status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _app_post(self, path):
        client = Client(SERVER_NAME=self.domain)
        return client.post(
            path, {}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.token,
        ).json()

    def _web_post(self, path):
        return self.web.post(path, {}, content_type="application/json").json()

    def _upstream_mirror(self):
        owner = User.objects.create_user(username="up-binding-owner", password="pw-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url="https://up.example.test",
            account="up-account", password="up-pw", enabled=True,
        )
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://up.example.test",
            upstream_order_id="up-order-1",
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            project_text="上家单",
            remote_rec_fee=Decimal("88.00"),
            upstream_payload={"orderId": "up-order-1"},
            first_seen_at=timezone.now(),
        )

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_claim_without_money_is_rejected(self):
        order = self._order(_no(41))
        body = self._app_post("/api/business/hall/" + order.order_no + "/claim/")
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", body["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_claim_freezes_when_the_balance_is_enough(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance", "updated_at"])
        order = self._order(_no(42))
        body = self._app_post("/api/business/hall/" + order.order_no + "/claim/")
        self.assertEqual(body["code"], 0)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50.00"))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_web_session_is_not_gated_by_the_app_switch(self):
        order = self._order(_no(43))
        body = self._web_post("/api/business/hall/" + order.order_no + "/claim/")
        self.assertEqual(body["code"], 0)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("0"))

    @override_settings(DEPOSIT_ENFORCED=True, DEPOSIT_ENFORCED_APP=False)
    def test_web_session_is_gated_by_the_master_switch(self):
        order = self._order(_no(44))
        body = self._web_post("/api/business/hall/" + order.order_no + "/claim/")
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED)

    @override_settings(DEPOSIT_ENFORCED=False, DEPOSIT_ENFORCED_APP=True)
    def test_app_upstream_claim_is_gated_like_a_native_claim(self):
        """上家单与自家单**一样**：App 闸开着、没冻够 ⇒ 1013「押金不足」（不是「去网页端」）。"""
        mirror = self._upstream_mirror()
        body = self._app_post("/api/business/hall/upstream/" + str(mirror.pk) + "/claim/")
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", body["msg"])

    @override_settings(DEPOSIT_ENFORCED=True, DEPOSIT_ENFORCED_APP=False)
    def test_master_switch_also_gates_web_upstream_claims(self):
        """总闸开着时上家单也要冻（2026-10-20 起上家单支持冻结）。"""
        mirror = self._upstream_mirror()
        body = self._web_post("/api/business/hall/upstream/" + str(mirror.pk) + "/claim/")
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", body["msg"])
