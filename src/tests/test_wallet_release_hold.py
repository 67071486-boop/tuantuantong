"""押金解冻 7 天冷静期（业务方 2026-10-21 拍板，**选项 A**）。

## 钉住什么

1. **申请不动钱**：`request_release` 只记 `release_requested_at` + 申请金额 + 一条
   `release_request` 流水，`frozen` / `balance` 一分不变（旧实现会立刻把 frozen 转进 balance）；
2. **第 7 天到账**：阈值钉到**小时/秒**（`now - 7 天 + 1 小时` 不结、`now - 7 天 - 1 秒` 结）；
3. **有进行中的单不能申请**（沿用 `claim._ACTIVE_STATUSES`）；
4. **申请后接单 ⇒ 申请作废、押金照常补**（接口字段 `release_request_cancelled`）；
   客服指派（`/assign/`）同样作废（走 `set_taker`）；
5. **重复申请不刷新计时**（连点两次不能把到账日往后推）；
6. **连读两次只结算一次**（只写一条 `release` 流水）；
7. **懒结算埋的四个入口**都真的会结算：GET 钱包 / POST 申请 / 提现创建 / 接单补冻；
8. **到期先结算、再补冻**：否则会留下「有单、零冻结」的窟窿；
9. **真并发**（`TransactionTestCase` + 两线程）：申请方持钱包锁未提交时，接单方的作废
   **必须等锁**，不能因为「此刻还看不到申请」就漏掉（评审 2026-10-21 阻断 1）；
10. **金额取申请那一刻**：冷静期内客服 `frozen_add` 加冻的部分不退；`frozen_sub` 调小只退剩下的；
   冻结额为 0 ⇒ 清申请、不写 0 元流水（评审应修 3）。

## 时间怎么造

仓里没有 `freezegun` 依赖，先例是**直接改字段**（`test_operation_log_migration.py` 那种
打 `timezone.now` 的补丁只适合单次调用）。本文件把 `release_requested_at` 用
`QuerySet.update` 挪到 `now - N 天`：到期判据只读这一列，所以这就是「第 6 天 / 第 7 天」
本身，不需要等真实时间。

⚠️ 金额断言一律 `Decimal`（⛔ 不碰 float）；流水断言打**数据库行**。
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)

from apps.accounts.models import AppToken, StaffProfile, TakerWallet, WalletTransaction
from apps.audit.models import OperationLog
from apps.accounts.wallet import (
    ADJUST_DECREASE,
    ADJUST_INCREASE,
    ADJUST_TARGET_FROZEN,
    RELEASE_HOLD,
    RELEASE_HOLD_DAYS,
    WalletError,
    adjust_wallet,
    ensure_deposit_for_claim,
    get_wallet,
    request_release,
    request_withdraw,
    settle_release_if_due,
)
from apps.business.claim import claim_order
from apps.business.models import (
    EmpType,
    Game,
    GameDepositRule,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import make_logged_in_client

CLAIM_NODES = {"grab_hall.take_order"}


def _no(n):
    return f"20261021{n:012d}"


class _ReleaseHoldFixture:
    """公共夹具（不继承 `TenantTestCase`：领域层与接口层两个测试类各自继承它）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker = User.objects.create_user(username="hold-taker", password="pw-hold")
        profile = StaffProfile.objects.create(
            user=self.taker,
            mobile="13700002222",
            alipay_account="hold@example.com",
            alipay_name="冷静期打手",
        )
        profile.games.set([self.game])

    # ── 工具 ──────────────────────────────────────────────────────
    def _credit(self, amount):
        """直接改库造余额（充值端点不属本文件）。"""
        wallet = get_wallet(self.taker)
        wallet.balance += Decimal(amount)
        wallet.save(update_fields=["balance", "updated_at"])
        return wallet

    def _freeze(self, amount, game=None):
        """从余额冻出押金（走真实 `ensure_deposit_for_claim`，不手写 frozen）。"""
        self._credit(amount)
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, game or self.game)

    def _move_request_back(self, days):
        """把申请时刻挪到 `now - days` 天前（「第 N 天」的造法）。"""
        TakerWallet.objects.filter(user=self.taker).update(
            release_requested_at=timezone.now() - timedelta(days=days)
        )

    def _set_requested_at(self, moment):
        """把申请时刻设成 `moment`（阈值钉到小时/秒的造法；金额列保持不动）。"""
        TakerWallet.objects.filter(user=self.taker).update(release_requested_at=moment)

    def _claim(self, game, n):
        order = Order.objects.create(
            game=game,
            order_no=_no(n),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        with transaction.atomic():
            return claim_order(
                user_id=self.taker.id,
                order_id=order.id,
                nodes=CLAIM_NODES,
                # 本文件关心的是「解冻申请」的副作用，押金闸门在别的用例里显式开关。
                enforce_deposit=False,
            )


class ReleaseHoldTestCase(_ReleaseHoldFixture, TenantTestCase):
    """7 天冷静期的领域层判据（申请 / 结算 / 作废 / 幂等）。"""

    # ── ① 申请不动钱 ──────────────────────────────────────────────
    def test_request_keeps_the_money_frozen_and_writes_one_request_row(self):
        self._freeze("50.00")
        before = get_wallet(self.taker)
        self.assertEqual(before.frozen, Decimal("50.00"))

        wallet, created = request_release(self.taker)

        self.assertTrue(created)
        self.assertEqual(wallet.frozen, Decimal("50.00"))      # 钱仍冻着
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNotNone(wallet.release_requested_at)
        # ⭐ 金额与时刻一起落库（评审应修 3）：到期只退这一笔。
        self.assertEqual(wallet.release_requested_amount, Decimal("50.00"))
        tx = WalletTransaction.objects.get(kind=WalletTransaction.KIND_RELEASE_REQUEST)
        self.assertEqual(tx.amount, Decimal("50.00"))
        self.assertEqual(tx.frozen_after, Decimal("50.00"))
        self.assertEqual(tx.balance_after, Decimal("0.00"))
        self.assertIn("7 天", tx.note)
        # 没有发生「到账」那一步
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 0
        )

    # ── ② 阈值：第 7 天到账（钉到小时 / 秒，⛔ 不只钉「第 6 天 / 第 7 天」）──
    def test_the_threshold_is_exact_to_the_hour_and_second(self):
        """`now - RELEASE_HOLD + 1h` 不结；`now - RELEASE_HOLD - 1s` 结。

        ⚠️ 判别力：把判据从 `>=` 改成 `>`（或把边界挪到「第 6 天」）⇒ 第二条断言红。
        """
        self._freeze("50.00")
        request_release(self.taker)

        self._set_requested_at(timezone.now() - RELEASE_HOLD + timedelta(hours=1))
        wallet = settle_release_if_due(self.taker)
        self.assertEqual(wallet.frozen, Decimal("50.00"))      # 还差 1 小时：一分不动
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNotNone(wallet.release_requested_at)

        self._set_requested_at(timezone.now() - RELEASE_HOLD - timedelta(seconds=1))
        wallet = settle_release_if_due(self.taker)
        self.assertEqual(wallet.frozen, Decimal("0.00"))       # 已过 1 秒：到账
        self.assertEqual(wallet.balance, Decimal("50.00"))
        self.assertIsNone(wallet.release_requested_at)
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 1
        )

    def test_day_six_does_not_settle_and_day_seven_does(self):
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(6)

        wallet = settle_release_if_due(self.taker)
        self.assertEqual(wallet.frozen, Decimal("50.00"))      # 第 6 天：一分不动
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNotNone(wallet.release_requested_at)
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 0
        )

        self._move_request_back(7)
        wallet = settle_release_if_due(self.taker)
        self.assertEqual(wallet.frozen, Decimal("0.00"))       # 第 7 天：到账
        self.assertEqual(wallet.balance, Decimal("50.00"))
        self.assertIsNone(wallet.release_requested_at)
        tx = WalletTransaction.objects.get(kind=WalletTransaction.KIND_RELEASE)
        self.assertEqual(tx.amount, Decimal("50.00"))
        self.assertEqual(tx.frozen_after, Decimal("0.00"))

    def test_requesting_after_expiry_settles_instead_of_erroring(self):
        """到期后再点「申请解冻」：先把钱结回余额，本次不算一次新申请（⛔ 不报错）。"""
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(7)

        wallet, created = request_release(self.taker)

        self.assertFalse(created)
        self.assertEqual(wallet.frozen, Decimal("0.00"))
        self.assertEqual(wallet.balance, Decimal("50.00"))
        self.assertIsNone(wallet.release_requested_at)

    # ── ②′ 结算金额取申请那一刻（评审应修 3）──────────
    def test_an_admin_top_up_during_the_hold_is_not_released(self):
        admin = User.objects.create_user(username="hold-admin")
        self._freeze("50.00")
        request_release(self.taker)
        adjust_wallet(
            self.taker,
            target=ADJUST_TARGET_FROZEN,
            direction=ADJUST_INCREASE,
            amount=Decimal("100.00"),
            reason="冷静期内加冻",
            client_ref_no="hold-add-1",
            operator=admin,
        )
        self._move_request_back(7)

        wallet = settle_release_if_due(self.taker)

        self.assertEqual(wallet.balance, Decimal("50.00"))     # 只退申请那一刻的 50
        self.assertEqual(wallet.frozen, Decimal("100.00"))     # 多出来的继续冻着
        self.assertIsNone(wallet.release_requested_amount)
        tx = WalletTransaction.objects.get(kind=WalletTransaction.KIND_RELEASE)
        self.assertEqual(tx.amount, Decimal("50.00"))

    def test_a_zero_frozen_request_clears_without_a_zero_row(self):
        admin = User.objects.create_user(username="hold-admin-zero")
        self._freeze("50.00")
        request_release(self.taker)
        adjust_wallet(
            self.taker,
            target=ADJUST_TARGET_FROZEN,
            direction=ADJUST_DECREASE,
            amount=Decimal("50.00"),
            reason="冷静期内减冻",
            client_ref_no="hold-sub-1",
            operator=admin,
        )
        self._move_request_back(7)

        wallet = settle_release_if_due(self.taker)

        self.assertIsNone(wallet.release_requested_at)
        self.assertIsNone(wallet.release_requested_amount)
        self.assertEqual(wallet.frozen, Decimal("0.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 0
        )

    def test_settle_is_blocked_while_an_order_is_active(self):
        """到期复核：有单在跑 ⇒ 只清申请、不结算（评审 2026-10-21 阻断 1 的兜底）。"""
        self._freeze("50.00")
        request_release(self.taker)
        self._claim(self.game, 6)   # 真实路径会作废申请；随后把「坏状态」写回去
        TakerWallet.objects.filter(user=self.taker).update(
            release_requested_at=timezone.now() - timedelta(days=7),
            release_requested_amount=Decimal("50.00"),
        )

        wallet = settle_release_if_due(self.taker)

        self.assertEqual(wallet.frozen, Decimal("50.00"))      # ⛔ 有单在跑，钱不退
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNone(wallet.release_requested_at)         # 申请作废
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 0
        )

    def test_the_voided_request_leaves_an_audit_row(self):
        """Z3（，2026-10-25）：到期复核清申请**不再静默** ⇒ 留一条审计行。

        判据打在**数据库行**上（E-0009），并逐项钉字面值：动作码 / 分类 / 目标 / 申请额 /
        申请时刻 / 机器来源。⚠️ 光断「有一行日志」会让「写一句『已作废』」也通过——
        这里连 `to_value`（申请额）与 `reason`（申请时刻）都断，才答得出「为什么没了」。
        """
        self._freeze("50.00")
        request_release(self.taker)
        self._claim(self.game, 6)   # 真实路径会作废申请；随后把「坏状态」写回去
        requested_at = timezone.now() - timedelta(days=7)
        TakerWallet.objects.filter(user=self.taker).update(
            release_requested_at=requested_at,
            release_requested_amount=Decimal("50.00"),
        )

        wallet = settle_release_if_due(self.taker)

        # 钱没动、申请被清（与既有那条用例同一终态）。
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNone(wallet.release_requested_at)
        rows = OperationLog.objects.filter(
            action="wallet.release_request_voided_by_activity"
        )
        self.assertEqual(rows.count(), 1, list(rows.values()))
        row = rows.get()
        self.assertEqual(row.log_type, 70)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(self.taker.pk))
        self.assertEqual(row.target_name, "hold-taker")
        self.assertEqual(row.to_value, "50.00")
        self.assertEqual(
            row.reason, timezone.localtime(requested_at).strftime("%Y-%m-%d %H:%M")
        )
        self.assertEqual(row.actor, "system")
        self.assertIsNone(row.actor_id)
        self.assertEqual(
            row.summary,
            "【system】解冻申请因有进行中的订单作废：hold-taker，该笔 50.00 元押金继续冻结"
            f"（申请时间：{timezone.localtime(requested_at):%Y-%m-%d %H:%M}）",
        )

    # ── ③ 有进行中的单 ⇒ 不能申请 ───────────────────────────────
    def test_request_is_rejected_while_an_order_is_active(self):
        order = self._claim(self.game, 1)
        wallet = get_wallet(self.taker)
        wallet.balance += Decimal("50.00")                     # 够冻，确保不是「钱不够」挡的
        wallet.save(update_fields=["balance", "updated_at"])
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)

        with self.assertRaises(WalletError) as ctx:
            request_release(self.taker)
        self.assertIn("进行中的订单", str(ctx.exception))
        self.assertIsNone(get_wallet(self.taker).release_requested_at)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    # ── ④ 申请后接单 ⇒ 申请作废 + 押金照常补 ─────────────────────
    def test_claiming_again_cancels_the_request(self):
        """押金闸门关着时：接单成功 ⇒ 申请被清，钱继续冻着（⛔ 没有被退走）。"""
        expensive = Game.objects.create(name="三角洲行动")
        self._freeze("50.00")
        request_release(self.taker)
        self.assertIsNotNone(get_wallet(self.taker).release_requested_at)

        claimed = self._claim(expensive, 2)                    # 冷静期内又接单

        self.assertIs(claimed.release_request_cancelled, True)
        wallet = get_wallet(self.taker)
        self.assertIsNone(wallet.release_requested_at)
        self.assertIsNone(wallet.release_requested_amount)
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))

    def test_a_claim_with_the_deposit_gate_on_cancels_and_tops_up(self):
        """同一件事的完整版：押金闸门开着时，接单补冻与作废申请在**同一个事务**里。"""
        expensive = Game.objects.create(name="三角洲行动")
        GameDepositRule.objects.create(game=expensive, amount=Decimal("300"))
        self._freeze("50.00")
        request_release(self.taker)
        self._credit("250.00")
        order = Order.objects.create(
            game=expensive,
            order_no=_no(3),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )

        with transaction.atomic():
            claimed = claim_order(
                user_id=self.taker.id,
                order_id=order.id,
                nodes=CLAIM_NODES,
                enforce_deposit=True,
            )

        self.assertIs(claimed.release_request_cancelled, True)
        wallet = get_wallet(self.taker)
        self.assertIsNone(wallet.release_requested_at)
        self.assertEqual(wallet.frozen, Decimal("300.00"))     # 补到 300
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_FREEZE).count(), 2
        )                                                       # 50 + 250

    # ── ⑤ 重复申请不刷新计时 ────────────────────────────────────
    def test_repeated_request_does_not_restart_the_timer(self):
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(6)
        old = get_wallet(self.taker).release_requested_at

        wallet, created = request_release(self.taker)

        self.assertFalse(created)
        self.assertEqual(wallet.release_requested_at, old)      # 计时没被重置
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_RELEASE_REQUEST
            ).count(),
            1,
        )

    # ── ⑥ 连读两次只结算一次 ────────────────────────────────────
    def test_two_reads_settle_only_once(self):
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(7)

        first = settle_release_if_due(self.taker)
        second = settle_release_if_due(self.taker)

        self.assertEqual(first.balance, Decimal("50.00"))
        self.assertEqual(second.balance, Decimal("50.00"))      # 没有被加两次
        self.assertEqual(second.frozen, Decimal("0.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 1
        )

    # ── ⑦ 提现创建也是懒结算入口 ────────────────────────────────
    def test_withdraw_settles_an_expired_request_first(self):
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(7)

        order = request_withdraw(self.taker, Decimal("30.00"))

        self.assertEqual(order.state, "pending")
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, Decimal("20.00"))      # 50 到账后提 30
        self.assertEqual(wallet.frozen, Decimal("0.00"))
        self.assertIsNone(wallet.release_requested_at)

    # ── ⑧ 到期先结算、再补冻（防「有单、零冻结」）───────────────
    def test_an_expired_request_is_settled_before_the_deposit_is_topped_up(self):
        cheap = Game.objects.create(name="小游戏")              # 默认押金 50
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(7)
        self._credit("250.00")                                  # 结算后余额 = 250 + 50

        order = Order.objects.create(
            game=cheap,
            order_no=_no(4),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        with transaction.atomic():
            claimed = claim_order(
                user_id=self.taker.id,
                order_id=order.id,
                nodes=CLAIM_NODES,
                enforce_deposit=True,
            )

        # 已到期 ⇒ 不是「取消」而是「结算」；钱进余额后**重新冻**这单要求的 50。
        self.assertIs(claimed.release_request_cancelled, False)
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("50.00"))       # ⛔ 不是 0（有单、零冻结）
        self.assertEqual(wallet.balance, Decimal("250.00"))     # 300 − 50
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 1
        )


class ReleaseHoldAppApiTests(_ReleaseHoldFixture, TenantTestCase):
    """App 接口：`GET /api/app/wallet/` 的新字段 + 懒结算；解冻申请端点的幂等。"""

    def _issue_app_token(self, user=None):
        raw = new_token()
        AppToken.objects.create(
            user=user or self.taker,
            key_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(days=1),
            client="app-android",
        )
        return raw

    def _get_wallet(self, user=None):
        token = self._issue_app_token(user)
        body = Client(SERVER_NAME=self.tenant.get_primary_domain().domain).get(
            "/api/app/wallet/", HTTP_AUTHORIZATION="Bearer " + token
        ).json()
        self.assertEqual(body["code"], 0)
        return body["data"]

    def _post_release(self):
        token = self._issue_app_token()
        return Client(SERVER_NAME=self.tenant.get_primary_domain().domain).post(
            "/api/app/wallet/release/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + token,
        ).json()

    def test_wallet_api_exposes_the_hold_fields(self):
        self._freeze("50.00")
        wallet, _ = request_release(self.taker)
        data = self._get_wallet()

        self.assertTrue(data["release_pending"])
        self.assertEqual(data["release_hold_days"], RELEASE_HOLD_DAYS)
        self.assertEqual(data["release_hold_days"], 7)
        self.assertEqual(data["frozen"], "50.00")               # 钱仍冻着
        self.assertEqual(data["balance"], "0.00")
        expected = timezone.localtime(
            wallet.release_requested_at + timedelta(days=RELEASE_HOLD_DAYS)
        ).isoformat()
        self.assertEqual(data["release_available_at"], expected)
        self.assertTrue(data["release_available_at"].endswith("+08:00"))

    def test_wallet_api_settles_before_reading(self):
        self._freeze("50.00")
        request_release(self.taker)
        self._move_request_back(7)

        data = self._get_wallet()

        self.assertEqual(data["frozen"], "0.00")
        self.assertEqual(data["balance"], "50.00")
        self.assertFalse(data["release_pending"])
        self.assertIsNone(data["release_available_at"])
        self.assertEqual(
            WalletTransaction.objects.filter(kind=WalletTransaction.KIND_RELEASE).count(), 1
        )

    def test_wallet_api_reports_no_request_as_null_available_at(self):
        data = self._get_wallet()
        self.assertFalse(data["release_pending"])
        self.assertIsNone(data["release_available_at"])
        self.assertEqual(data["release_hold_days"], 7)

    def test_release_endpoint_is_idempotent_and_keeps_the_timer(self):
        self._freeze("50.00")
        first = self._post_release()
        self.assertEqual(first["code"], 0)
        self.assertTrue(first["data"]["release_pending"])
        old = get_wallet(self.taker).release_requested_at

        second = self._post_release()
        self.assertEqual(second["code"], 0)
        self.assertTrue(second["data"]["release_pending"])
        self.assertEqual(get_wallet(self.taker).release_requested_at, old)
        self.assertEqual(
            WalletTransaction.objects.filter(
                kind=WalletTransaction.KIND_RELEASE_REQUEST
            ).count(),
            1,
        )


class ReleaseHoldClaimApiTests(TenantTestCase):
    """接单接口能看出「申请已取消」（`release_request_cancelled`）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="hold-claim",
            password="pw-hold-claim",
            groups=("代练员",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )
        self.taker = User.objects.get(username="hold-claim")
        StaffProfile.objects.filter(user=self.taker).update(
            alipay_account="claim@example.com", alipay_name="接单打手"
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=_no(9),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )

    def test_claim_response_says_the_release_request_was_cancelled(self):
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("50.00")
        wallet.save(update_fields=["balance", "updated_at"])
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)
        request_release(self.taker)

        body = self.client.post(
            f"/api/business/hall/{self.order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertIs(body["data"]["release_request_cancelled"], True)
        self.assertIsNone(get_wallet(self.taker).release_requested_at)

    def test_claim_without_a_request_reports_false(self):
        """控制组：没申请过的人接单，字段必须是 `false`（⛔ 不是恒真）。"""
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("50.00")
        wallet.save(update_fields=["balance", "updated_at"])
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)

        body = self.client.post(
            f"/api/business/hall/{self.order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertIs(body["data"]["release_request_cancelled"], False)


class ReleaseHoldAssignTests(_ReleaseHoldFixture, TenantTestCase):
    """客服指派（`/assign/` → `set_taker`）也要作废冷静期内的申请（评审 2026-10-21 应修 2）。"""

    ASSIGN_NODES = ("order.assign_taker", "order.change_status", "order.list")

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="hold-cs",
            groups=("客服",),
            nodes=self.ASSIGN_NODES,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=_no(8),
            status=OrderStatus.UNASSIGNED,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self._freeze("50.00")
        request_release(self.taker)

    def test_assign_cancels_the_pending_request(self):
        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/assign/",
            {"user_id": self.taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        wallet = get_wallet(self.taker)
        self.assertIsNone(wallet.release_requested_at)          # 指派那一刻就作废
        self.assertIsNone(wallet.release_requested_amount)
        self.assertEqual(wallet.frozen, Decimal("50.00"))       # 钱继续冻着
        self.assertEqual(wallet.balance, Decimal("0.00"))


TENANT_SCHEMA = "test"


class ReleaseHoldConcurrencyTests(TransactionTestCase):
    """真并发：申请方的钱包行锁**未提交**时，接单方的作废必须等锁（评审 2026-10-21 阻断 1）。

    ## 构造（顺序是造出来的，不是碰运气）

    - 申请线程：`transaction.atomic()` 里跑 `request_release`（内层 `atomic` 是嵌套的），
      申请写下后**保持外层事务不提交** ⇒ 钱包行锁一直被它拿着；
    - 接单线程：等申请写下 → 走到 `claim_order` 的 `cancel_release_on_claim`。修好之后它
      `select_for_update()` **阻塞**在这里；旧实现（无锁 `exists()` 前置）会当场返回 False
      并完成接单 —— 于是主线程那条「接单还没完成」的断言会红。
    - 主线程：确认接单线程**没有**在申请提交前完成 → 放行提交 → 两个线程收尾。

    ## 判据

    ① 接单不得早于申请提交完成；② 接单成功；③ 申请被清、钱仍冻着；
    ④ **再补一刀**：把「作废被漏掉」的坏状态写回去（申请还在 + 单已接下 + 已到期）⇒
    懒结算只清申请、**不退钱**（`_settle_release_locked` 的到期复核）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="tenant.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_a_concurrent_claim_cannot_miss_the_pending_request(self):
        from apps.business import claim as claim_module

        game = Game.objects.create(name="原神")
        taker = User.objects.create_user(username="hold-race")
        TakerWallet.objects.create(
            user=taker, balance=Decimal("0.00"), frozen=Decimal("50.00")
        )
        order = Order.objects.create(
            game=game,
            order_no=_no(77),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        taker_id, order_id = taker.id, order.id

        request_written = threading.Event()
        limits_checked = threading.Event()
        allow_commit = threading.Event()
        claim_done = threading.Event()
        results = {}

        def _requester():
            connections.close_all()
            try:
                with schema_context(TENANT_SCHEMA):
                    with transaction.atomic():
                        request_release(User.objects.get(pk=taker_id))
                        request_written.set()
                        # 不提交：钱包行锁 + 未提交的申请一直留在这里。
                        allow_commit.wait(timeout=30)
                results["request"] = "ok"
            except Exception as exc:                   # noqa: BLE001 —— 分类落进结果
                results["request"] = getattr(exc, "code", repr(exc))
            finally:
                connections.close_all()

        def _claimer():
            connections.close_all()
            try:
                request_written.wait(timeout=30)
                with schema_context(TENANT_SCHEMA):
                    with transaction.atomic():
                        claim_order(
                            user_id=taker_id,
                            order_id=order_id,
                            nodes={"grab_hall.take_order"},
                            enforce_deposit=False,
                        )
                results["claim"] = "ok"
                claim_done.set()
            except Exception as exc:                   # noqa: BLE001
                results["claim"] = getattr(exc, "code", repr(exc))
                claim_done.set()
            finally:
                connections.close_all()

        real_check_grab_limits = claim_module.check_grab_limits

        def _check_then_signal(**kwargs):
            # 这个调用紧挨着 `cancel_release_on_claim`（申请作废就在它后面一行）
            # ⇒ 它一返回，接单线程下一步就是去抢钱包行锁。
            real_check_grab_limits(**kwargs)
            limits_checked.set()

        def _claimer_blocked_on_lock() -> bool:
            """库里有没有一条正在**等锁**的后端（= 接单线程卡在钱包行锁上）。

            只用 `pg_stat_activity`（⛔ 不看连接数）：本用例此刻只有申请线程（持锁、空闲）
            与接单线程（等锁）两条业务连接，等锁的那条只能是接单线程。
            """
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
                return cursor.fetchone()[0] > 0

        req = threading.Thread(target=_requester, name="hold-requester")
        clm = threading.Thread(target=_claimer, name="hold-claimer")
        with mock.patch(
            "apps.business.claim.check_grab_limits", side_effect=_check_then_signal
        ):
            req.start()
            clm.start()
            self.assertTrue(request_written.wait(timeout=30), "申请线程没有写下申请")
            self.assertTrue(limits_checked.wait(timeout=30), "接单线程没有走到作废那一步")
            # 判据 ①：修好之后接单线程必须**停在钱包行锁上**（等锁的后端出现）；
            # 旧的无锁前置会直接放行、当场完成接单 ⇒ 本循环等不到等锁的后端。
            deadline = time.time() + 10
            blocked = False
            while time.time() < deadline:
                if _claimer_blocked_on_lock():
                    blocked = True
                    break
                if claim_done.is_set():
                    break
                time.sleep(0.05)
            self.assertTrue(
                blocked,
                "接单线程没有停在钱包行锁上 —— 并发窗口（申请活 + 单接下）回来了",
            )
            self.assertFalse(claim_done.is_set(), "接单在申请提交前就完成了")
            allow_commit.set()
            req.join(timeout=30)
            clm.join(timeout=30)

        self.assertFalse([t.name for t in (req, clm) if t.is_alive()], "有线程没退出")
        self.assertEqual(results, {"request": "ok", "claim": "ok"}, results)

        # 判据 ②③：单接下了，申请也被作废（钱没动）。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, taker_id
        )
        wallet = TakerWallet.objects.get(user_id=taker_id)
        self.assertIsNone(wallet.release_requested_at)
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))

        # 判据 ④：把「作废被漏掉」的坏状态写回去 ⇒ 到期复核只清申请、不退钱。
        TakerWallet.objects.filter(user_id=taker_id).update(
            release_requested_at=timezone.now() - timedelta(days=7),
            release_requested_amount=Decimal("50.00"),
        )
        settle_release_if_due(User.objects.get(pk=taker_id))
        wallet.refresh_from_db()
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertIsNone(wallet.release_requested_at)
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet__user_id=taker_id,
                kind=WalletTransaction.KIND_RELEASE,
            ).count(),
            0,
        )
