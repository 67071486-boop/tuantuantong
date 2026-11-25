"""客服后台的打手钱包管理 / 调账 + 游戏押金金额（2026-10-20，`feat/taker-wallet-adjust`）。

## 六条硬判据（业务方任务书，逐条落成用例）

1. **无节点全 403**：列表 / 调账 / 押金列表 / 改押金四个端点都 fail-closed；
2. **跨租户拿不到别人的钱包**：判据用**租户内唯一且两租户取值不同**的字段（登录账号），
   ⛔ 不用主键指认「另一租户的那一行」（`AGENTS.md` §5-G）；
3. **扣成负数被拒**（余额与押金冻结各一条）；
4. **超上限被拒**（默认 1000；`is not None` 判据：配置 `0` = 一分钱也不许调、`None` = 不设上限）；
5. **缺原因被拒**（且一分钱都不动、一行流水都不写）；
6. **重复提交不重复记账**（同一个 `client_request_no` 只写一条流水；第二次回 `duplicate=true`，
   且**不写第二行审计**）。

## 另外几条（同一批实现里承重的）

- 金额精度：最多两位小数、`> 0`；0 / 负数 / 三位小数一律业务码 1000；
- 余额扣到**恰好 0** 放行（D-0015：0 是合法边界）；
- 幂等键换了内容（项目 / 方向 / 金额）⇒ `CONFLICT`，⛔ 不静默返回上一笔；
- 押金规则的读口径 = **规则行 > 三角洲名字兜底 300 > 默认 50**（与 `deposit_gate` 同源，
  ⛔ 本文件不另立一套期望值——`deposit_amount_for_game` 是唯一出处）；
- 改押金写操作日志（改的是「谁能接单」的门槛），且 `from_value` 是**改动前的生效值**；
- ⭐ 客服页「押金够不够」= **该打手所用游戏里要求的最大额度** vs `frozen`（P1-9，见
  `WalletListDepositCriterionTests`）；一款游戏都没勾 ⇒ `deposit_required` / `deposit_enough`
  都是 `null`（**未知**），⛔ 不回落 0（0 是「接单不需要冻结」，与「不知道」是两件事）。

## 钱包台账线（`feat/wallet-ledger`）

- **排序 / 分页**（`WalletListSortAndPaginationTests`）：`sort` 三档 × `order` 两向、
  非法值 `1000`、越界 `1002`、两页合起来不重不漏。⚠️ 夹具刻意**反序**（账号序 ≠ 金额序，
  且让两条**同额不同余额**）⇒ 「仍然按 username 排」不可能碰巧绿，副键也被断言到。
- **流水明细**（`WalletTransactionListTests` / `WalletTransactionListNPlusOneTests`）：
  行契约 / `kinds` 来自 `KIND_CHOICES` / `kind` / `q` / 上海日界 / 分页 / 含已软删打手 /
  查询数不随行数增长。

## 金额一律按**字符串**断言

后端契约：金额出参是两位小数字符串（`Decimal` 不交给 DRF 的 JSONEncoder 变 `float`）。
本文件不把响应转成 `float` 再比。
"""
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import MemberType
from apps.accounts.models import StaffProfile, TakerWallet, WalletTransaction
from apps.accounts.wallet import (
    DEFAULT_DEPOSIT_AMOUNT,
    DELTA_DEPOSIT_AMOUNT,
    deposit_amount_for_game,
    get_wallet,
    set_deposit_rule,
)
from apps.audit.models import OperationLog
from apps.business.models import Game, GameDepositRule
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 钱包管理页的权限节点（`apps/rbac/catalog.py` 的第六条自有节点）。
NODE = "finance.taker_wallet_adjust"

WALLETS_URL = "/api/business/wallets/"
RULES_URL = "/api/business/deposit-rules/"
TX_URL = "/api/business/wallet-transactions/"


class WalletAdminTestBase(TenantTestCase):
    """共用夹具：三个打手（其中一个没钱）、一款普通游戏、一款三角洲、一个持节点的客服。"""

    def setUp(self):
        # 配置断言用 override_settings（TenantTestCase 的类装饰器不生效，同既有用例的实测）。
        overrides = override_settings(WALLET_ADJUST_MAX_AMOUNT="1000")
        overrides.enable()
        self.addCleanup(overrides.disable)

        self.game = Game.objects.create(name="合成游戏甲")
        self.delta = Game.objects.create(name="合成三角洲行动")

        self.taker = self._make_taker("tw-wallet-a", "打手甲", "13700001001")
        self.taker_b = self._make_taker("tw-wallet-b", "打手乙", "13700001002")
        self.internal = User.objects.create_user(
            username="tw-wallet-internal", password="pw-internal"
        )
        StaffProfile.objects.create(
            user=self.internal,
            mobile="13700001003",
            member_type=MemberType.INTERNAL,
        )

        # 游戏权限：两位打手都**只勾**「合成游戏甲」（默认档 50）。押金的判据自 P1-9 起是
        # 「该打手所用游戏里的**最大**要求」，所以夹具必须真的给游戏，否则是「未知」那一支。
        self.taker.staff_profile.games.set([self.game])
        self.taker_b.staff_profile.games.set([self.game])

        # 甲有 100 余额 + 50 冻结（够接普通游戏）；乙空钱包。
        wallet = get_wallet(self.taker)
        wallet.balance = Decimal("100.00")
        wallet.frozen = Decimal("50.00")
        wallet.save(update_fields=["balance", "frozen", "updated_at"])

        self.admin_client = make_logged_in_client(
            username="tw-wallet-admin",
            password="pw-wallet-admin",
            groups=("客服",),
            nodes=(NODE,),
        )
        self.admin = User.objects.get(username="tw-wallet-admin")

    def _make_taker(self, username: str, display_name: str, mobile: str) -> User:
        user = User.objects.create_user(username=username, password="pw-taker")
        user.first_name = display_name
        user.save(update_fields=["first_name"])
        StaffProfile.objects.create(
            user=user, mobile=mobile, member_type=MemberType.PARTNER
        )
        return user

    def _adjust(self, user, data, *, client=None):
        return (client or self.admin_client).post(
            f"{WALLETS_URL}{user.pk}/adjust/",
            data,
            content_type="application/json",
        )

    def _body(self, user, data, *, client=None):
        return self._adjust(user, data, client=client).json()

    def _payload(self, **over):
        payload = {
            "target": "balance",
            "direction": "increase",
            "amount": "50.00",
            "reason": "合成测试：补发活动奖励",
            "client_request_no": "adj-test-1",
        }
        payload.update(over)
        return payload


class WalletListTests(WalletAdminTestBase):
    """列表：只列打手、金额字符串、押金够不够接单、搜索、上限随列表回。"""

    def test_list_returns_only_partner_takers_and_string_amounts(self):
        body = self.admin_client.get(WALLETS_URL).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["adjust_max_amount"], "1000.00")
        usernames = [row["username"] for row in body["data"]["items"]]
        # 正向对照 + 否定：内部成员不在「打手钱包」列表里。
        self.assertIn("tw-wallet-a", usernames)
        self.assertIn("tw-wallet-b", usernames)
        self.assertNotIn("tw-wallet-internal", usernames)

        row = next(r for r in body["data"]["items"] if r["username"] == "tw-wallet-a")
        # 逐键钉契约（前端按它渲染）。
        self.assertEqual(
            set(row),
            {
                "user_id", "username", "display_name", "mobile", "balance", "frozen",
                "deposit_required", "deposit_enough", "updated_at",
            },
        )
        self.assertEqual(row["balance"], "100.00")
        self.assertEqual(row["frozen"], "50.00")
        # 甲只勾了普通游戏（50）；三角洲 300 不在他的游戏里 ⇒ 与他无关
        # （P1-9：判据是「该打手所用游戏里的**最大**要求」，⛔ 不是全租户最低档）。
        self.assertEqual(row["deposit_required"], "50.00")
        self.assertIs(row["deposit_enough"], True)
        self.assertIsInstance(row["balance"], str)

        empty = next(r for r in body["data"]["items"] if r["username"] == "tw-wallet-b")
        self.assertEqual(empty["balance"], "0.00")
        self.assertEqual(empty["frozen"], "0.00")
        self.assertIs(empty["deposit_enough"], False)

    def test_search_matches_username_display_name_and_mobile(self):
        by_username = self.admin_client.get(f"{WALLETS_URL}?q=tw-wallet-a").json()
        self.assertEqual([r["username"] for r in by_username["data"]["items"]], ["tw-wallet-a"])

        by_display = self.admin_client.get(f"{WALLETS_URL}?q=打手乙").json()
        self.assertEqual([r["username"] for r in by_display["data"]["items"]], ["tw-wallet-b"])

        by_mobile = self.admin_client.get(f"{WALLETS_URL}?q=13700001002").json()
        self.assertEqual([r["username"] for r in by_mobile["data"]["items"]], ["tw-wallet-b"])

        none = self.admin_client.get(f"{WALLETS_URL}?q=不存在的人").json()
        self.assertEqual(none["data"]["items"], [])
        self.assertEqual(none["data"]["total"], 0)

    def test_over_long_search_is_a_business_error_not_a_500(self):
        body = self.admin_client.get(f"{WALLETS_URL}?q={'x' * 65}").json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)

    @override_settings(WALLET_ADJUST_MAX_AMOUNT="0")
    def test_zero_limit_is_reported_as_zero_not_as_unset(self):
        """`is not None` 判据：配置 0 是「上限 0 元」，⛔ 不是「没配」。"""
        body = self.admin_client.get(WALLETS_URL).json()
        self.assertEqual(body["data"]["adjust_max_amount"], "0.00")

        rejected = self._body(self.taker, self._payload(amount="0.01"))
        self.assertEqual(rejected["code"], ErrorCode.VALIDATION, rejected)
        self.assertIn("0", rejected["msg"])

    @override_settings(WALLET_ADJUST_MAX_AMOUNT=None)
    def test_unset_limit_means_no_limit(self):
        body = self.admin_client.get(WALLETS_URL).json()
        self.assertEqual(body["data"]["adjust_max_amount"], "")
        ok = self._body(
            self.taker,
            self._payload(amount="5000.00", client_request_no="adj-no-limit"),
        )
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(ok["data"]["balance_after"], "5100.00")


class WalletListDepositCriterionTests(WalletAdminTestBase):
    """「押金够不够」的口径（P1-9）：该打手**所用游戏里的最大要求** vs `frozen`。

    ⛔ 旧的「全租户最低一档」会放过下面第一条那种形状：租户里有 50 与 300 两款游戏、
    打手只勾了 300 那款、却只冻了 50 ⇒ 他其实一款都接不了，页面却说「够」。
    ⚠️ 与本判据同源的是 App 接单前的闸（`wallet.ensure_deposit_for_claim` 的
    `frozen >= deposit_amount_for_game(game)`），取值一律走 `wallet._effective_deposit`
    ——本文件不另立一套期望值。
    """

    def setUp(self):
        super().setUp()
        # 租户里两款游戏的押金：甲 = 50、三角洲 = 300（写成显式规则行，不靠名字兜底）。
        GameDepositRule.objects.create(game=self.game, amount=Decimal("50.00"))
        GameDepositRule.objects.create(game=self.delta, amount=Decimal("300.00"))

    def _row(self, username: str) -> dict:
        body = self.admin_client.get(WALLETS_URL).json()
        self.assertEqual(body["code"], 0, body)
        return next(r for r in body["data"]["items"] if r["username"] == username)

    def test_required_is_the_max_of_the_games_this_taker_actually_has(self):
        """⭐ 主判据：只勾 300 那款、frozen=50 ⇒ 「不够」（旧口径会误判「够」）。"""
        self.taker.staff_profile.games.set([self.delta])
        row = self._row("tw-wallet-a")
        self.assertEqual(row["frozen"], "50.00")
        self.assertEqual(row["deposit_required"], "300.00")
        self.assertIs(row["deposit_enough"], False)

    def test_required_ignores_the_games_the_taker_did_not_pick(self):
        """反向控制：只勾 50 那款时，300 那款与他无关 ⇒ 仍然「够」（⛔ 不是全租户最高档）。"""
        row = self._row("tw-wallet-a")
        self.assertEqual(row["deposit_required"], "50.00")
        self.assertIs(row["deposit_enough"], True)

    def test_zero_requirement_is_enough_not_unknown(self):
        """额度就是 0 ⇒ 「够」（0 元 = 接这单不需要冻结），⛔ 不许显示成「未知」/「不够」。"""
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("0.00"))
        row = self._row("tw-wallet-b")
        self.assertEqual(row["frozen"], "0.00")
        self.assertEqual(row["deposit_required"], "0.00")
        self.assertIs(row["deposit_enough"], True)

    def test_taker_without_any_game_is_unknown_not_zero(self):
        """没勾游戏 ⇒ 「未知」（`null`），⛔ 不是 0、也不是「不够」。"""
        self.taker.staff_profile.games.clear()
        row = self._row("tw-wallet-a")
        self.assertIsNone(row["deposit_required"])
        self.assertIsNone(row["deposit_enough"])

    def test_deleted_game_is_not_counted(self):
        """软删的游戏不算（与 App 的 `/api/app/wallet/` 同一支：`deleted_at__isnull=True`）。"""
        self.taker.staff_profile.games.set([self.game, self.delta])
        self.delta.deleted_at = timezone.now()
        self.delta.save(update_fields=["deleted_at"])
        row = self._row("tw-wallet-a")
        self.assertEqual(row["deposit_required"], "50.00")
        self.assertIs(row["deposit_enough"], True)

    def test_query_count_does_not_grow_with_the_number_of_takers(self):
        """N+1 守卫：每页的 SQL 条数不随行数增长（游戏走预取、押金规则整页只查一次）。"""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as baseline:
            body = self.admin_client.get(WALLETS_URL).json()
            self.assertEqual(body["code"], 0, body)
        baseline_count = len(baseline)

        for index in range(5):
            extra = self._make_taker(f"tw-wallet-bulk-{index}", f"批量打手{index}", f"1370000200{index}")
            extra.staff_profile.games.set([self.game, self.delta])

        with CaptureQueriesContext(connection) as grown:
            body = self.admin_client.get(WALLETS_URL).json()
            self.assertEqual(body["code"], 0, body)
        grown_count = len(grown)

        self.assertEqual(
            grown_count,
            baseline_count,
            f"列表查询数随打手数增长（{baseline_count} → {grown_count}），说明押金/游戏是逐行查的",
        )


class WalletAdjustTests(WalletAdminTestBase):
    """调账的四条硬闸 + 幂等 + 边界。"""

    def test_balance_increase_books_one_transaction_and_returns_before_after(self):
        body = self._body(self.taker, self._payload())

        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(data["balance_before"], "100.00")
        self.assertEqual(data["balance_after"], "150.00")
        self.assertEqual(data["frozen_before"], "50.00")
        self.assertEqual(data["frozen_after"], "50.00")
        self.assertEqual(data["amount"], "50.00")
        self.assertIs(data["duplicate"], False)

        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, Decimal("150.00"))
        self.assertEqual(wallet.frozen, Decimal("50.00"))

        tx = WalletTransaction.objects.get(pk=data["transaction_id"])
        self.assertEqual(tx.kind, WalletTransaction.KIND_ADJUST_ADD)
        self.assertEqual(tx.amount, Decimal("50.00"))
        self.assertEqual(tx.balance_after, Decimal("150.00"))
        # 原因进流水 + 操作人进流水（钱的证据要能自己讲清是谁、为什么）。
        self.assertEqual(tx.note, "合成测试：补发活动奖励")
        self.assertEqual(tx.operator_id, self.admin.pk)
        self.assertEqual(tx.client_ref, "adj-test-1")

    def test_balance_decrease_to_exactly_zero_is_allowed(self):
        body = self._body(
            self.taker,
            self._payload(direction="decrease", amount="100.00", client_request_no="adj-zero"),
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["balance_after"], "0.00")
        self.assertEqual(get_wallet(self.taker).balance, Decimal("0.00"))

    def test_balance_cannot_be_deducted_below_zero(self):
        body = self._body(
            self.taker,
            self._payload(direction="decrease", amount="100.01", client_request_no="adj-neg"),
        )
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIn("余额不足", body["msg"])
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))
        self.assertFalse(
            WalletTransaction.objects.filter(client_ref="adj-neg").exists(),
            "被拒的调账不该留下流水",
        )

    def test_frozen_cannot_be_deducted_below_zero(self):
        body = self._body(
            self.taker,
            self._payload(target="frozen", direction="decrease", amount="50.01"),
        )
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIn("押金冻结额不足", body["msg"])
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50.00"))

    def test_frozen_add_and_sub_use_their_own_kinds(self):
        added = self._body(
            self.taker,
            self._payload(target="frozen", amount="10.00", client_request_no="adj-f1"),
        )
        self.assertEqual(added["code"], 0, added)
        self.assertEqual(added["data"]["kind"], WalletTransaction.KIND_FROZEN_ADD)
        removed = self._body(
            self.taker,
            self._payload(
                target="frozen", direction="decrease", amount="10.00",
                client_request_no="adj-f2",
            ),
        )
        self.assertEqual(removed["code"], 0, removed)
        self.assertEqual(removed["data"]["kind"], WalletTransaction.KIND_FROZEN_SUB)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50.00"))

    def test_amount_over_the_single_limit_is_rejected(self):
        body = self._body(self.taker, self._payload(amount="1000.01"))
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIn("1000", body["msg"])
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))

    def test_the_single_limit_boundary_is_inclusive(self):
        body = self._body(
            self.taker,
            self._payload(amount="1000.00", client_request_no="adj-limit-ok"),
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["balance_after"], "1100.00")

    def test_reason_is_required_and_nothing_moves(self):
        for value in ("", "   "):
            with self.subTest(reason=value):
                body = self._body(self.taker, self._payload(reason=value))
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
                self.assertIn("原因", body["msg"])
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_amount_must_be_positive_and_at_most_two_decimals(self):
        for amount in ("0", "-1", "1.234", "abc", ""):
            with self.subTest(amount=amount):
                body = self._body(self.taker, self._payload(amount=amount))
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))
        self.assertEqual(WalletTransaction.objects.count(), 0)

    def test_client_request_no_is_required(self):
        body = self._body(self.taker, self._payload(client_request_no=""))
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIn("客户端单号", body["msg"])

    def test_bad_target_or_direction_is_a_business_error(self):
        bad_target = self._body(self.taker, self._payload(target="unknown"))
        self.assertEqual(bad_target["code"], ErrorCode.VALIDATION, bad_target)
        bad_direction = self._body(self.taker, self._payload(direction="sideways"))
        self.assertEqual(bad_direction["code"], ErrorCode.VALIDATION, bad_direction)
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))

    def test_the_same_client_request_no_books_only_once(self):
        """⭐ 幂等：同一请求重复提交 ⇒ 只记一次账、只写一行审计。"""
        first = self._body(self.taker, self._payload(client_request_no="adj-idem"))
        second = self._body(self.taker, self._payload(client_request_no="adj-idem"))

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        self.assertIs(first["data"]["duplicate"], False)
        self.assertIs(second["data"]["duplicate"], True)
        # 第二次回的是**第一次那一笔**（同一流水、同一份前后值）。
        self.assertEqual(second["data"]["transaction_id"], first["data"]["transaction_id"])
        self.assertEqual(second["data"]["balance_after"], first["data"]["balance_after"])

        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.balance, Decimal("150.00"), "重复提交把余额加了两次")
        self.assertEqual(
            WalletTransaction.objects.filter(client_ref="adj-idem").count(), 1
        )
        # 审计也只有一行（幂等命中不是第二次操作）。
        self.assertEqual(
            OperationLog.objects.filter(
                action="finance.taker_wallet_adjust", target_id=str(self.taker.pk)
            ).count(),
            1,
        )

    def test_reusing_a_client_request_no_for_a_different_adjustment_is_a_conflict(self):
        first = self._body(self.taker, self._payload(client_request_no="adj-reuse"))
        self.assertEqual(first["code"], 0, first)

        changed_amount = self._body(
            self.taker, self._payload(client_request_no="adj-reuse", amount="60.00")
        )
        self.assertEqual(changed_amount["code"], ErrorCode.CONFLICT, changed_amount)
        changed_target = self._body(
            self.taker,
            self._payload(client_request_no="adj-reuse", target="frozen"),
        )
        self.assertEqual(changed_target["code"], ErrorCode.CONFLICT, changed_target)
        self.assertEqual(get_wallet(self.taker).balance, Decimal("150.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(client_ref="adj-reuse").count(), 1
        )

    def test_adjusting_a_user_that_does_not_exist_in_this_tenant_is_a_business_error(self):
        response = self.admin_client.post(
            f"{WALLETS_URL}999999/adjust/",
            self._payload(),
            content_type="application/json",
        )
        body = response.json()
        # 本项目口径（`get_object_or_404` → 业务码 1002，HTTP 200）。
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertIsNone(body["data"])


class WalletAuditTests(WalletAdminTestBase):
    """每个写动作 `record(...)` 落一行；钱包调账的正文可读（人名 + 前后值 + 原因）。"""

    def test_adjust_writes_one_readable_audit_row(self):
        body = self._body(self.taker, self._payload())
        self.assertEqual(body["code"], 0, body)

        rows = OperationLog.objects.filter(action="finance.taker_wallet_adjust")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.actor_id, self.admin.pk)
        self.assertEqual(row.target_id, str(self.taker.pk))
        self.assertEqual(row.target_name, "打手甲")
        self.assertEqual(row.field_name, "balance")
        self.assertEqual(row.from_value, "100.00")
        self.assertEqual(row.to_value, "150.00")
        self.assertEqual(row.reason, "合成测试：补发活动奖励")
        # 模板里的 `{field_label}` 走静态标签表：正文是人话，⛔ 不是裸列名。
        self.assertIn("可用余额", row.summary)
        self.assertNotIn("{field_label}", row.summary)

    def test_frozen_adjust_audit_uses_the_frozen_label(self):
        self._body(
            self.taker,
            self._payload(target="frozen", amount="10.00", client_request_no="adj-audit-f"),
        )
        row = OperationLog.objects.get(action="finance.taker_wallet_adjust")
        self.assertEqual(row.field_name, "frozen")
        self.assertIn("押金冻结额", row.summary)


class DepositRuleTests(WalletAdminTestBase):
    """押金金额：读口径（规则 > 三角洲兜底 > 默认）与写动作。"""

    def test_list_reflects_the_effective_amount_and_its_source(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("80.00"))

        body = self.admin_client.get(RULES_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["default_amount"], "50.00")
        self.assertEqual(body["data"]["delta_amount"], "300.00")
        rows = {row["game_name"]: row for row in body["data"]["items"]}
        self.assertEqual(
            rows["合成游戏甲"],
            {
                "game_id": self.game.pk,
                "game_name": "合成游戏甲",
                "amount": "80.00",
                "source": "rule",
                "rule_id": GameDepositRule.objects.get(game=self.game).pk,
                "updated_at": rows["合成游戏甲"]["updated_at"],
            },
        )
        # 三角洲没有规则行 ⇒ 读层兜底 300（与 `deposit_gate` 同口径）。
        self.assertEqual(rows["合成三角洲行动"]["amount"], "300.00")
        self.assertEqual(rows["合成三角洲行动"]["source"], "default_delta")
        self.assertIsNone(rows["合成三角洲行动"]["rule_id"])

    def test_list_excludes_soft_deleted_games(self):
        self.game.deleted_at = timezone.now()
        self.game.save(update_fields=["deleted_at"])
        body = self.admin_client.get(RULES_URL).json()
        names = [row["game_name"] for row in body["data"]["items"]]
        self.assertNotIn("合成游戏甲", names)
        self.assertIn("合成三角洲行动", names)

    def test_update_creates_a_rule_row_and_changes_the_effective_amount(self):
        before = deposit_amount_for_game(self.game)
        self.assertEqual(before, DEFAULT_DEPOSIT_AMOUNT)

        response = self.admin_client.put(
            f"{RULES_URL}{self.game.pk}/",
            {"amount": "80.00"},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["amount"], "80.00")
        self.assertEqual(body["data"]["source"], "rule")
        self.assertIs(body["data"]["created"], True)

        self.assertEqual(deposit_amount_for_game(self.game), Decimal("80.00"))
        self.assertEqual(GameDepositRule.objects.get(game=self.game).amount, Decimal("80.00"))
        # 审计：from 是**改之前的生效值**（不是「规则行不存在」）。
        row = OperationLog.objects.get(action="finance.taker_deposit_rule_update")
        self.assertEqual(row.target_id, str(self.game.pk))
        self.assertEqual(row.target_name, "合成游戏甲")
        self.assertEqual(row.from_value, "50.00")
        self.assertEqual(row.to_value, "80.00")
        self.assertEqual(row.actor_id, self.admin.pk)

    def test_update_overrides_the_delta_fallback(self):
        self.assertEqual(deposit_amount_for_game(self.delta), DELTA_DEPOSIT_AMOUNT)
        body = self.admin_client.put(
            f"{RULES_URL}{self.delta.pk}/",
            {"amount": "120.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        # 规则行优先于三角洲名字兜底（读口径的唯一出处 `_effective_deposit`）。
        self.assertEqual(deposit_amount_for_game(self.delta), Decimal("120.00"))
        row = OperationLog.objects.get(action="finance.taker_deposit_rule_update")
        self.assertEqual(row.from_value, "300.00")

    def test_zero_is_a_legal_deposit_amount(self):
        body = self.admin_client.put(
            f"{RULES_URL}{self.game.pk}/", {"amount": "0"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["amount"], "0.00")
        self.assertEqual(deposit_amount_for_game(self.game), Decimal("0"))

    def test_negative_amount_is_rejected(self):
        body = self.admin_client.put(
            f"{RULES_URL}{self.game.pk}/", {"amount": "-1"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(GameDepositRule.objects.filter(game=self.game).exists())
        self.assertEqual(deposit_amount_for_game(self.game), DEFAULT_DEPOSIT_AMOUNT)

    def test_bad_amount_format_is_rejected(self):
        for amount in ("1.234", "abc", ""):
            with self.subTest(amount=amount):
                body = self.admin_client.put(
                    f"{RULES_URL}{self.game.pk}/",
                    {"amount": amount},
                    content_type="application/json",
                ).json()
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)

    def test_unknown_or_deleted_game_is_a_business_error(self):
        missing = self.admin_client.put(
            f"{RULES_URL}999999/", {"amount": "60.00"}, content_type="application/json"
        ).json()
        self.assertEqual(missing["code"], ErrorCode.BUSINESS, missing)

        self.game.deleted_at = timezone.now()
        self.game.save(update_fields=["deleted_at"])
        deleted = self.admin_client.put(
            f"{RULES_URL}{self.game.pk}/", {"amount": "60.00"}, content_type="application/json"
        ).json()
        self.assertEqual(deleted["code"], ErrorCode.BUSINESS, deleted)


class PermissionGateTests(WalletAdminTestBase):
    """判据 1：无节点 ⇒ 五个端点全 403（fail-closed，不是界面藏起来）。"""

    def test_all_five_endpoints_are_403_without_the_node(self):
        outsider = make_logged_in_client(
            username="tw-wallet-no-node", password="pw-wallet-no-node"
        )

        self.assertEqual(outsider.get(WALLETS_URL).status_code, 403)
        self.assertEqual(outsider.get(RULES_URL).status_code, 403)
        # 流水明细：复用同一颗节点，⛔ 不新增。
        self.assertEqual(outsider.get(TX_URL).status_code, 403)
        self.assertEqual(
            self._adjust(self.taker, self._payload(), client=outsider).status_code, 403
        )
        self.assertEqual(
            outsider.put(
                f"{RULES_URL}{self.game.pk}/",
                {"amount": "80.00"},
                content_type="application/json",
            ).status_code,
            403,
        )
        # 没有节点的人一分钱都没动到。
        self.assertEqual(get_wallet(self.taker).balance, Decimal("100.00"))
        self.assertFalse(GameDepositRule.objects.exists())
        self.assertEqual(WalletTransaction.objects.count(), 0)


class CrossTenantWalletTests(TenantTestCase):
    """判据 2：以 A 的会话拿不到 B 的钱包。

    ⚠️ **判据用「租户内唯一、且两租户取值不同」的字段**（登录账号），⛔ 不用主键指认
    「另一租户的那一行」（AGENTS §5-G：主键在两个 schema 里可以撞成同一串数字）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="tw_wallet_b", name="租户 B", domain="tw-wallet-b.test.local"
        )

    def setUp(self):
        overrides = override_settings(WALLET_ADJUST_MAX_AMOUNT="1000")
        overrides.enable()
        self.addCleanup(overrides.disable)

        # A：当前 schema。⭐ 与 B 里那个用户**同一个 pk=4242**，形成双向对照。
        self.user_a = User.objects.create_user(
            pk=4242, username="only-a-wallet", password="pw-a"
        )
        self.user_a.first_name = "租户 A 的打手"
        self.user_a.save(update_fields=["first_name"])
        StaffProfile.objects.create(
            user=self.user_a, mobile="13600000001", member_type=MemberType.PARTNER
        )
        wallet_a = get_wallet(self.user_a)
        wallet_a.balance = Decimal("100.00")
        wallet_a.save(update_fields=["balance", "updated_at"])
        self.client_a = make_logged_in_client(
            username="tw-wallet-a-admin",
            password="pw-a-admin",
            groups=("客服",),
            nodes=(NODE,),
        )

        # B：另一个租户 schema。同 pk 一张（对照用）+ 本租户不存在的 pk 一张（1002 用）。
        with tenant_context(self.tenant_b):
            self.user_b = User.objects.create_user(
                pk=4242, username="only-b-wallet", password="pw-b"
            )
            StaffProfile.objects.create(
                user=self.user_b, mobile="13600000002", member_type=MemberType.PARTNER
            )
            wallet_b = get_wallet(self.user_b)
            wallet_b.balance = Decimal("200.00")
            wallet_b.save(update_fields=["balance", "updated_at"])
            self.user_b_other = User.objects.create_user(
                pk=9999, username="only-b-wallet-2", password="pw-b2"
            )
            StaffProfile.objects.create(
                user=self.user_b_other, mobile="13600000003", member_type=MemberType.PARTNER
            )

    def test_the_list_never_contains_the_other_tenants_takers(self):
        body = self.client_a.get(WALLETS_URL).json()
        self.assertEqual(body["code"], 0, body)
        usernames = [row["username"] for row in body["data"]["items"]]
        # 正向对照：A 自己的行在（否则下面的否定断言可能是「列表本来就空」）。
        self.assertIn("only-a-wallet", usernames)
        self.assertNotIn("only-b-wallet", usernames)
        self.assertNotIn("only-b-wallet-2", usernames)

    def test_the_transactions_list_only_contains_the_current_tenants_rows(self):
        """流水同样是租户内的表：A 的会话只看得见 A 的流水。

        判据用**租户内唯一、且两租户取值不同**的字段（登录账号），⛔ 不用主键
        （两个 schema 里都能撞成 4242）。⚠️ A 的流水在**测试方法内**建，⛔ 不在 setUp：
        上面那条调账用例断言「B 一条流水都没有」。
        """
        WalletTransaction.objects.create(
            wallet=get_wallet(self.user_a),
            kind=WalletTransaction.KIND_RECHARGE,
            amount=Decimal("10.00"),
            balance_after=Decimal("100.00"),
            frozen_after=Decimal("0.00"),
            note="合成测试：租户 A 流水",
        )
        with tenant_context(self.tenant_b):
            WalletTransaction.objects.create(
                wallet=get_wallet(self.user_b),
                kind=WalletTransaction.KIND_RECHARGE,
                amount=Decimal("20.00"),
                balance_after=Decimal("200.00"),
                frozen_after=Decimal("0.00"),
                note="合成测试：租户 B 流水",
            )

        body = self.client_a.get(TX_URL).json()
        self.assertEqual(body["code"], 0, body)
        usernames = [row["username"] for row in body["data"]["items"]]
        # 正向对照：A 自己的流水在。
        self.assertIn("only-a-wallet", usernames)
        self.assertNotIn("only-b-wallet", usernames)

        # 按 B 的账号搜：A 的读面必须回空（不是 403、也不是把 B 的行带出来）。
        by_b = self.client_a.get(f"{TX_URL}?q=only-b-wallet").json()
        self.assertEqual(by_b["code"], 0, by_b)
        self.assertEqual(by_b["data"]["items"], [])
        self.assertEqual(by_b["data"]["total"], 0)

    def test_same_pk_adjust_only_touches_the_current_tenants_taker(self):
        """⭐ A 与 B 各有一个 pk=4242 的打手：A 的调账只能改 A 那个（判据落在用户名上）。"""
        body = self.client_a.post(
            f"{WALLETS_URL}4242/adjust/",
            self._payload(),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["username"], "only-a-wallet")

        self.assertEqual(get_wallet(self.user_a).balance, Decimal("150.00"))
        with tenant_context(self.tenant_b):
            b_wallet = TakerWallet.objects.get(user__username="only-b-wallet")
            self.assertEqual(b_wallet.balance, Decimal("200.00"), "跨租户把钱改了")
            b_tx = WalletTransaction.objects.filter(wallet=b_wallet)
            self.assertEqual(b_tx.count(), 0, "跨租户写了流水")

    def test_an_id_that_only_exists_in_b_is_not_found_in_a(self):
        body = self.client_a.post(
            f"{WALLETS_URL}{self.user_b_other.pk}/adjust/",
            self._payload(),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        with tenant_context(self.tenant_b):
            wallet = TakerWallet.objects.filter(user__username="only-b-wallet-2").first()
            self.assertIsNone(wallet, "对 B 的 pk 调账不该在 B 里建钱包")

    def _payload(self):
        return {
            "target": "balance",
            "direction": "increase",
            "amount": "50.00",
            "reason": "合成测试：跨租户",
            "client_request_no": "adj-cross-tenant",
        }


class WalletListSortAndPaginationTests(WalletAdminTestBase):
    """§1 排序 / 分页。

    ⚠️ 夹具刻意**反序**（评审⭐ 原来 a/b 两位的账号序与金额序同向 ⇒
    「仍然 order_by username」也能绿）。这里三条新打手的**账号字母序**与**金额序**不同向，
    并让两条同额（冻结 30）不同余额 ⇒ 副键真的被断言到，⛔ 不是碰巧。
    """

    def setUp(self):
        super().setUp()
        # 账号字母序 a < b < c；冻结额 c == b == 30（余额 c 300 > b 100）；a 余额最高、冻结最低。
        self._wallet("tw-sort-a", balance="500.00", frozen="10.00")
        self._wallet("tw-sort-b", balance="100.00", frozen="30.00")
        self._wallet("tw-sort-c", balance="300.00", frozen="30.00")

    def _wallet(self, username: str, *, balance: str, frozen: str):
        user = self._make_taker(username, f"排序打手{username[-1]}", f"1370000300{username[-1]}")
        wallet = get_wallet(user)
        wallet.balance = Decimal(balance)
        wallet.frozen = Decimal(frozen)
        wallet.save(update_fields=["balance", "frozen", "updated_at"])
        return user

    def _names(self, query: str = "") -> list:
        body = self.admin_client.get(f"{WALLETS_URL}{query}").json()
        self.assertEqual(body["code"], 0, body)
        return [row["username"] for row in body["data"]["items"]]

    def test_default_is_frozen_desc_with_balance_as_secondary(self):
        """缺省 `sort=frozen&order=desc`；同额（30）按余额降序 ⇒ c 在 b 前。"""
        self.assertEqual(
            self._names(),
            ["tw-wallet-a", "tw-sort-c", "tw-sort-b", "tw-sort-a", "tw-wallet-b"],
        )

    def test_frozen_asc(self):
        """升序时同额仍是**副键降序**（余额 c > b），只在主键上翻方向。"""
        self.assertEqual(
            self._names("?sort=frozen&order=asc"),
            ["tw-wallet-b", "tw-sort-a", "tw-sort-c", "tw-sort-b", "tw-wallet-a"],
        )

    def test_balance_desc(self):
        """余额 100 的同额（wallet-a 冻结 50 / sort-b 冻结 30）⇒ 副键把 wallet-a 排在前面。"""
        self.assertEqual(
            self._names("?sort=balance&order=desc"),
            ["tw-sort-a", "tw-sort-c", "tw-wallet-a", "tw-sort-b", "tw-wallet-b"],
        )

    def test_balance_asc(self):
        self.assertEqual(
            self._names("?sort=balance&order=asc"),
            ["tw-wallet-b", "tw-wallet-a", "tw-sort-b", "tw-sort-c", "tw-sort-a"],
        )

    def test_username_both_directions(self):
        self.assertEqual(
            self._names("?sort=username&order=asc"),
            ["tw-sort-a", "tw-sort-b", "tw-sort-c", "tw-wallet-a", "tw-wallet-b"],
        )
        self.assertEqual(
            self._names("?sort=username&order=desc"),
            ["tw-wallet-b", "tw-wallet-a", "tw-sort-c", "tw-sort-b", "tw-sort-a"],
        )

    def test_taker_without_a_wallet_row_sorts_as_zero(self):
        """没有钱包行的打手按 0 参与排序（⛔ 不是 NULL 序、也不是「看不见」）。"""
        body = self.admin_client.get(f"{WALLETS_URL}?sort=frozen&order=desc").json()
        row = body["data"]["items"][-1]
        self.assertEqual(row["username"], "tw-wallet-b")
        self.assertEqual(row["frozen"], "0.00")

    def test_pages_are_disjoint_and_complete(self):
        """两页（这里三页）合起来不重不漏，且 total 与真实行数一致。"""
        seen = []
        pages = []
        for number in (1, 2, 3):
            body = self.admin_client.get(
                f"{WALLETS_URL}?sort=balance&order=desc&page_size=2&page={number}"
            ).json()
            self.assertEqual(body["code"], 0, body)
            pages.append(body["data"])
            seen.extend(row["username"] for row in body["data"]["items"])
        self.assertEqual(pages[-1]["total"], 5)
        self.assertEqual([p["page"] for p in pages], [1, 2, 3])
        self.assertEqual(
            seen,
            ["tw-sort-a", "tw-sort-c", "tw-wallet-a", "tw-sort-b", "tw-wallet-b"],
        )

    def test_page_beyond_the_last_is_a_business_error_not_a_500(self):
        body = self.admin_client.get(f"{WALLETS_URL}?page=999").json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertIsNone(body["data"])

    def test_illegal_sort_or_order_is_a_business_error_not_a_500(self):
        # ⚠️ 空串参数在 `list()` 里等于「没传」（`raw` 过滤空值）⇒ 回缺省，不算非法值。
        for query in ("?sort=bogus", "?order=sideways"):
            with self.subTest(query=query):
                body = self.admin_client.get(f"{WALLETS_URL}{query}").json()
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)


class WalletTransactionTestBase(WalletAdminTestBase):
    """流水用例的共用夹具：造一条流水（`created_at` 可钉）。"""

    def _tx(
        self,
        user,
        *,
        kind: str = WalletTransaction.KIND_RECHARGE,
        amount: str = "10.00",
        balance_after: str = "0.00",
        frozen_after: str = "0.00",
        note: str = "",
        ref_no: str = "",
        created_at: datetime | None = None,
    ) -> WalletTransaction:
        wallet = get_wallet(user)
        tx = WalletTransaction.objects.create(
            wallet=wallet,
            kind=kind,
            amount=Decimal(amount),
            balance_after=Decimal(balance_after),
            frozen_after=Decimal(frozen_after),
            note=note,
            ref_no=ref_no,
        )
        if created_at is not None:
            # `created_at` 是 `auto_now_add` ⇒ 只能建完再 UPDATE（⛔ 传参会被静默重写）。
            WalletTransaction.objects.filter(pk=tx.pk).update(created_at=created_at)
            tx.refresh_from_db()
        return tx

    def _list(self, query: str = "") -> dict:
        body = self.admin_client.get(f"{TX_URL}{query}").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]


class WalletPayloadTimezoneTests(WalletTransactionTestBase):
    """D-0302（2026-10-24）：钱包管理面手拼时刻一律**项目时区**（+08:00）。

    管理端「切字符」格式化器直显 ⇒ 裸 datetime 经信封渲染成 UTC 串时整列差 8 小时。
    夹具时刻固定 UTC 2026-10-24 04:30 ⇒ 期望北京墙钟 12:30。
    """

    FIXED_UTC = datetime(2026, 10, 24, 4, 30, tzinfo=dt_timezone.utc)
    LOCAL_TEXT = "2026-10-24T12:30:00+08:00"

    def test_the_transaction_row_created_at_is_local(self):
        self._tx(self.taker, created_at=self.FIXED_UTC)
        row = self._list()["items"][0]
        self.assertEqual(row["created_at"], self.LOCAL_TEXT)

    def test_the_wallet_row_updated_at_is_local(self):
        get_wallet(self.taker)  # 确保有钱包行
        TakerWallet.objects.filter(user=self.taker).update(updated_at=self.FIXED_UTC)
        rows = self.admin_client.get(WALLETS_URL).json()["data"]["items"]
        mine = next(r for r in rows if r["user_id"] == self.taker.pk)
        self.assertEqual(mine["updated_at"], self.LOCAL_TEXT)

    def test_the_deposit_rule_rows_updated_at_is_local(self):
        set_deposit_rule(self.game, Decimal("66.00"))
        GameDepositRule.objects.filter(game=self.game).update(updated_at=self.FIXED_UTC)
        body = self.admin_client.get(RULES_URL).json()
        self.assertEqual(body["code"], 0, body)
        mine = next(r for r in body["data"]["items"] if r["game_id"] == self.game.pk)
        self.assertEqual(mine["updated_at"], self.LOCAL_TEXT)


class WalletTransactionListTests(WalletTransactionTestBase):
    """§3 流水明细：行契约 / kinds / 过滤 / 日界 / 分页 / 软删范围。"""

    def test_row_contract_string_money_and_kind_label_from_choices(self):
        tx = self._tx(
            self.taker,
            kind=WalletTransaction.KIND_ADJUST_ADD,
            amount="50.00",
            balance_after="150.00",
            frozen_after="50.00",
            note="合成测试：补发活动奖励",
            ref_no="R-1",
        )
        data = self._list()
        self.assertEqual(data["total"], 1)
        row = data["items"][0]
        self.assertEqual(
            set(row),
            {
                "id", "user_id", "username", "display_name", "mobile", "kind",
                "kind_label", "amount", "balance_after", "frozen_after", "note",
                "ref_no", "created_at",
            },
        )
        self.assertEqual(row["id"], tx.pk)
        self.assertEqual(row["user_id"], self.taker.pk)
        self.assertEqual(row["username"], "tw-wallet-a")
        self.assertEqual(row["display_name"], "打手甲")
        self.assertEqual(row["mobile"], "13700001001")
        self.assertEqual(row["kind"], WalletTransaction.KIND_ADJUST_ADD)
        # 中文只来自 `WalletTransaction.KIND_CHOICES`（`get_kind_display()`）。
        self.assertEqual(row["kind_label"], "后台加款")
        for key in ("amount", "balance_after", "frozen_after"):
            self.assertIsInstance(row[key], str)
        self.assertEqual(row["amount"], "50.00")
        self.assertEqual(row["balance_after"], "150.00")
        self.assertEqual(row["frozen_after"], "50.00")
        self.assertEqual(row["note"], "合成测试：补发活动奖励")
        self.assertEqual(row["ref_no"], "R-1")

    def test_kinds_are_all_the_model_choices(self):
        data = self._list()
        self.assertEqual(
            data["kinds"],
            [
                {"value": value, "label": label}
                for value, label in WalletTransaction.KIND_CHOICES
            ],
        )
        # ⭐ D-0336：新增第 14 个 kind `withdraw_offset`（提现抵扣退款欠款，
        #    带符号）⇒ 计数 13 → 14。下拉本身取自 `KIND_CHOICES`（上方逐项对拍不变）。
        self.assertEqual(len(data["kinds"]), 14)

    def test_kind_filter_and_illegal_kind(self):
        self._tx(self.taker, kind=WalletTransaction.KIND_RECHARGE)
        self._tx(self.taker_b, kind=WalletTransaction.KIND_ADJUST_ADD)

        data = self._list("?kind=recharge")
        self.assertEqual([row["kind"] for row in data["items"]], ["recharge"])
        self.assertEqual(data["total"], 1)

        bad = self.admin_client.get(f"{TX_URL}?kind=bogus").json()
        self.assertEqual(bad["code"], ErrorCode.VALIDATION, bad)

    def test_q_matches_username_display_name_and_mobile(self):
        self._tx(self.taker)
        self._tx(self.taker_b)

        def usernames(query: str) -> list:
            return [row["username"] for row in self._list(query)["items"]]

        self.assertEqual(usernames("?q=tw-wallet-b"), ["tw-wallet-b"])
        self.assertEqual(usernames("?q=打手甲"), ["tw-wallet-a"])
        self.assertEqual(usernames("?q=13700001002"), ["tw-wallet-b"])
        self.assertEqual(self._list("?q=不存在的人")["items"], [])

    def test_date_bounds_are_shanghai_days(self):
        """当天 00:00 含、次日 00:00 不含（⛔ 不按 UTC 切）。"""
        shanghai = ZoneInfo("Asia/Shanghai")
        at_start = self._tx(
            self.taker, created_at=datetime(2026, 10, 20, 0, 0, 0, tzinfo=shanghai)
        )
        at_end = self._tx(
            self.taker_b, created_at=datetime(2026, 10, 20, 23, 59, 59, tzinfo=shanghai)
        )
        next_day = self._tx(
            self.taker, created_at=datetime(2026, 10, 21, 0, 0, 0, tzinfo=shanghai)
        )
        previous_day = self._tx(
            self.taker_b, created_at=datetime(2026, 10, 19, 23, 59, 59, tzinfo=shanghai)
        )

        data = self._list("?date_from=2026-10-20&date_to=2026-10-20")
        self.assertEqual([row["id"] for row in data["items"]], [at_end.pk, at_start.pk])
        self.assertEqual(data["total"], 2)

        self.assertEqual(
            [row["id"] for row in self._list("?date_from=2026-10-21")["items"]],
            [next_day.pk],
        )
        self.assertEqual(
            [row["id"] for row in self._list("?date_to=2026-10-19")["items"]],
            [previous_day.pk],
        )

    def test_date_to_at_the_maximum_date_does_not_overflow(self):
        """`date_to=9999-12-31` 是合法日期（`DateField` 不设 `max_value`）。

        旧写法「次日 00:00 不含」会在 `date_to + timedelta(days=1)` 抛 `OverflowError`
        ⇒ 整页 500（评审 2026-10-23 抓到）；改成「当天 `time.max` 含」后必须回 200。
        """
        self._tx(
            self.taker,
            created_at=datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        data = self._list("?date_to=9999-12-31")
        self.assertEqual(data["total"], 1)

        # 只有下界也照样是 200（`time.min` 不参与加法），空结果是 0 行而不是 500。
        at_max = self._list("?date_from=9999-12-31&date_to=9999-12-31")
        self.assertEqual(at_max["items"], [])
        self.assertEqual(at_max["total"], 0)

    def test_illegal_or_reversed_dates_are_business_errors(self):
        for query in (
            "?date_from=garbage",
            "?date_to=2026-13-01",
            "?date_from=2026-10-21&date_to=2026-10-20",
        ):
            with self.subTest(query=query):
                body = self.admin_client.get(f"{TX_URL}{query}").json()
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)

    def test_ordering_is_created_at_desc_then_id_desc(self):
        older = self._tx(
            self.taker, created_at=datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        newer = self._tx(
            self.taker, created_at=datetime(2026, 10, 20, 11, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        self.assertEqual([row["id"] for row in self._list()["items"]], [newer.pk, older.pk])

    def test_pagination_total_and_last_page(self):
        out_of_order = []
        for hour in (9, 10, 11):
            out_of_order.append(
                self._tx(
                    self.taker,
                    created_at=datetime(
                        2026, 10, 20, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                    ),
                )
            )
        first = self._list("?page_size=2&page=1")
        second = self._list("?page_size=2&page=2")
        self.assertEqual(first["total"], 3)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(len(second["items"]), 1)
        ids = [row["id"] for row in first["items"] + second["items"]]
        self.assertEqual(ids, [out_of_order[2].pk, out_of_order[1].pk, out_of_order[0].pk])

    def test_soft_deleted_takers_transactions_are_still_listed(self):
        """流水是钱的历史证据 ⇒ 包含已软删打手；钱包列表**不**包含（有意的差异）。"""
        self._tx(self.taker)
        self.taker.staff_profile.deleted_at = timezone.now()
        self.taker.staff_profile.save(update_fields=["deleted_at"])

        self.assertIn(
            "tw-wallet-a", [row["username"] for row in self._list()["items"]]
        )
        wallets = self.admin_client.get(WALLETS_URL).json()
        self.assertNotIn(
            "tw-wallet-a", [row["username"] for row in wallets["data"]["items"]]
        )


class WalletTransactionListNPlusOneTests(WalletTransactionTestBase):
    """⭐ 流水的 `display_name` / `mobile` 走 `select_related`：SQL 条数不随行数增长。"""

    def test_query_count_does_not_grow_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._tx(self.taker)
        with CaptureQueriesContext(connection) as baseline:
            body = self.admin_client.get(TX_URL).json()
            self.assertEqual(body["code"], 0, body)
        baseline_count = len(baseline)

        for index in range(4):
            user = self._make_taker(
                f"tw-tx-bulk-{index}", f"流水批量{index}", f"1370000400{index}"
            )
            self._tx(user)

        with CaptureQueriesContext(connection) as grown:
            body = self.admin_client.get(TX_URL).json()
            self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            len(grown),
            baseline_count,
            f"流水查询数随行数增长（{baseline_count} → {len(grown)}），说明 user / staff_profile 是逐行查的",
        )


class WalletAdjustDefaultSettingsTests(SimpleTestCase):
    """默认配置：单笔上限 1000 元（与业务方口径一致；改它要是一次有意识的提交）。"""

    def test_wallet_adjust_max_amount_defaults_to_1000(self):
        from django.conf import settings

        self.assertEqual(Decimal(str(settings.WALLET_ADJUST_MAX_AMOUNT)), Decimal("1000"))
