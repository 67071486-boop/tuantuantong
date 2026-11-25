"""打手**个人押金额度**（业务方 2026-10-23；计划
`docs/plans/2026-10-23-taker-deposit-quota-plan.md` §2 / §5 / §6 / §8.1）。

## 钉住什么（计划 §8.1 的 1–5 条）

1. **取值顺序**：开关关 ⇒ 全局；开 + 有行 ⇒ 行；开 + 无行 ⇒ 0；关 + 有行 ⇒ 全局（行被忽略）。
   批量函数 `deposit_amounts_for_user` 与逐个调用同值（读面靠它避免 N+1）。
2. **接单闸**（`override_settings(DEPOSIT_ENFORCED=True)`）：
   开关开 + 额度 0 ⇒ 余额 0 也能接、不冻钱；开关开 + 额度 100 + 余额够 ⇒ 冻到 100；
   余额不够 ⇒ `GRAB_LIMIT_EXCEEDED` + 「押金不足」；开关关 ⇒ 走全局（既有行为不变）。
3. **API**：GET 形状（含 `global_amount` / `amount=null`）、PUT 保存与校验（负数 / 三位小数 /
   超列宽 / 非本打手游戏 / 软删游戏 ⇒ 1000）、403、1002（不存在 / 无档案 / 软删账号）、
   保存后 GET 原样、未传的游戏行保留、审计（真变化才写）与幂等保存不刷屏。
4. **读面**：`GET /api/app/wallet/` 的 `deposit_rules` 用个人额度；客服钱包列表的
   `deposit_required` 用「个人额度 max」、开关关仍用全局。
5. **租户边界**：用租户 A 的 client 访问只存在于 B 的 user_id ⇒ 1002；同 pk 的 A 行只动 A。

## 关注点

- ⚠️ 押金**闸**与个人额度是**两件事**：额度只管「该冻多少」，闸关着时谁都不冻
  （`apps/business/deposit_gate.py`）。本文件用例都显式打开 `DEPOSIT_ENFORCED`，与
  `tests/test_app_wallet.py` 同一口径。
- ⚠️ 金额一律按**字符串**断言（后端契约：两位小数字符串，⛔ 不交给 DRF 变 float）。
- ⚠️ 审计断言打**数据库行**（`OperationLog`），不打函数返回值。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import MemberType
from apps.accounts.models import AppToken, StaffProfile, TakerDepositQuota
from apps.accounts.wallet import (
    PERSONAL_DEPOSIT_AMOUNT,
    deposit_amount_for_game,
    deposit_amount_for_user,
    deposit_amounts_for_user,
    ensure_deposit_for_claim,
    get_wallet,
)
from apps.audit.models import OperationLog
from apps.business.claim import ClaimError, claim_order
from apps.business.models import (
    EmpType,
    Game,
    GameDepositRule,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 钱包管理页的权限节点（复用 `finance.taker_wallet_adjust`，⛔ 不新增节点）。
NODE = "finance.taker_wallet_adjust"

#: 打手个人押金额度端点（`<user_id>` 就是打手的 user id）。
def quota_url(user_id) -> str:
    return f"/api/business/takers/{user_id}/deposit-quota/"


CLAIM_NODES = {"grab_hall.take_order"}
SWITCH_ACTION = "finance.taker_deposit_quota_switch"
AMOUNT_ACTION = "finance.taker_deposit_quota_amount"


def _order_no(n: int) -> str:
    return f"20261023{n:012d}"


class _QuotaFixtureMixin:
    """共用夹具：两款游戏 + 一个打手（两款都有权限）+ 一个持节点的客服。"""

    def setUp(self):
        self.game = Game.objects.create(name="合成游戏甲")
        self.game2 = Game.objects.create(name="合成游戏乙")
        self.taker = self._make_taker("depq-taker", "打手甲", "13800001001")
        self.taker_profile = self.taker.staff_profile
        self.taker_profile.games.set([self.game, self.game2])
        self.admin_client = make_logged_in_client(
            username="depq-admin",
            password="pw-depq-admin",
            groups=("客服",),
            nodes=(NODE,),
            games=None,
        )
        self.admin = User.objects.get(username="depq-admin")

    def _make_taker(self, username: str, display_name: str, mobile: str) -> User:
        user = User.objects.create_user(username=username, password="pw-taker")
        user.first_name = display_name
        user.save(update_fields=["first_name"])
        StaffProfile.objects.create(
            user=user, mobile=mobile, member_type=MemberType.PARTNER
        )
        return user

    def _set_switch(self, enabled: bool):
        profile = StaffProfile.objects.get(user=self.taker)
        profile.deposit_quota_enabled = enabled
        profile.save(update_fields=["deposit_quota_enabled", "updated_at"])
        return profile

    def _set_quota(self, game, amount: str):
        return TakerDepositQuota.objects.update_or_create(
            user=self.taker, game=game, defaults={"amount": Decimal(amount)}
        )[0]

    def _get(self, user_id=None, *, client=None):
        return (client or self.admin_client).get(
            quota_url(user_id if user_id is not None else self.taker.pk)
        )

    def _put(self, payload, user_id=None, *, client=None):
        return (client or self.admin_client).put(
            quota_url(user_id if user_id is not None else self.taker.pk),
            payload,
            content_type="application/json",
        )

    def _payload(self, enabled=True, amounts=None):
        return {
            "enabled": enabled,
            "amounts": amounts
            if amounts is not None
            else {str(self.game.pk): "50.00"},
        }


# ---------------------------------------------------------------------------
# 1. 取值顺序（计划 §8.1-1）
# ---------------------------------------------------------------------------
class DepositAmountResolutionTests(_QuotaFixtureMixin, TenantTestCase):
    """`deposit_amount_for_user` / `deposit_amounts_for_user` 的四档取值。"""

    def test_switch_off_uses_the_global_amount(self):
        self.assertFalse(self.taker_profile.deposit_quota_enabled)
        self.assertEqual(deposit_amount_for_user(self.taker, self.game), Decimal("50"))

    def test_switch_off_ignores_a_personal_row(self):
        """开关关着时，个人行存在也不读它（行被忽略）。"""
        self._set_quota(self.game, "100.00")
        self.assertEqual(deposit_amount_for_user(self.taker, self.game), Decimal("50"))

    def test_switch_on_with_a_row_uses_the_row(self):
        self._set_switch(True)
        self._set_quota(self.game, "100.00")
        self.assertEqual(
            deposit_amount_for_user(self.taker, self.game), Decimal("100.00")
        )

    def test_switch_on_without_a_row_is_zero_not_the_global_default(self):
        """业务方第 ③ 条：没填额度的游戏按 **0 元**（= 该游戏免押），⛔ 不是 50。"""
        self._set_switch(True)
        self.assertEqual(
            deposit_amount_for_user(self.taker, self.game),
            PERSONAL_DEPOSIT_AMOUNT,
        )
        self.assertEqual(deposit_amount_for_user(self.taker, self.game), Decimal("0"))

    def test_switch_off_keeps_the_delta_fallback(self):
        """开关关 ⇒ 全局口径一字不改（名字含「三角洲」且没有规则行 ⇒ 300）。"""
        delta = Game.objects.create(name="三角洲行动")
        self.assertEqual(deposit_amount_for_user(self.taker, delta), Decimal("300"))

    def test_switch_on_does_not_use_the_delta_fallback(self):
        delta = Game.objects.create(name="三角洲行动")
        self.taker_profile.games.add(delta)
        self._set_switch(True)
        self.assertEqual(deposit_amount_for_user(self.taker, delta), Decimal("0"))

    def test_bulk_function_matches_the_single_calls_when_switch_is_on(self):
        self._set_switch(True)
        self._set_quota(self.game, "10.00")
        games = [self.game, self.game2]
        self.assertEqual(
            deposit_amounts_for_user(self.taker, games),
            {
                self.game.pk: Decimal("10.00"),
                # 缺行 ⇒ 0（不是全局 50）。
                self.game2.pk: Decimal("0"),
            },
        )
        for game in games:
            self.assertEqual(
                deposit_amounts_for_user(self.taker, [game])[game.pk],
                deposit_amount_for_user(self.taker, game),
            )

    def test_bulk_function_matches_global_rules_when_switch_is_off(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("80.00"))
        games = [self.game, self.game2]
        self.assertEqual(
            deposit_amounts_for_user(self.taker, games),
            {
                self.game.pk: Decimal("80.00"),
                self.game2.pk: Decimal("50"),
            },
        )
        for game in games:
            self.assertEqual(
                deposit_amounts_for_user(self.taker, [game])[game.pk],
                deposit_amount_for_game(game),
            )

    def test_bulk_function_returns_empty_for_no_games(self):
        self.assertEqual(deposit_amounts_for_user(self.taker, []), {})


# ---------------------------------------------------------------------------
# 2. 接单闸（计划 §8.1-2，验收判据 1–3）
# ---------------------------------------------------------------------------
class ClaimDepositGateTests(_QuotaFixtureMixin, TenantTestCase):
    """接单补冻走个人额度：0 = 不冻也能接、100 = 冻 100、不够 = 押金不足。"""

    def _order(self, n: int):
        order = Order.objects.create(
            game=self.game,
            order_no=_order_no(n),
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _claim(self, order):
        with transaction.atomic():
            claim_order(
                user_id=self.taker.pk, order_id=order.pk, nodes=CLAIM_NODES
            )

    def _credit(self, amount: str):
        wallet = get_wallet(self.taker)
        wallet.balance += Decimal(amount)
        wallet.save(update_fields=["balance", "updated_at"])

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_a_zero_quota_lets_a_broke_taker_claim_and_freezes_nothing(self):
        """开关开 + 该游戏没填（0 元）⇒ 余额 0 也能接，一分不冻（验收判据 2）。"""
        self._set_switch(True)
        order = self._order(1)
        self._claim(order)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("0"))
        self.assertEqual(wallet.balance, Decimal("0"))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_a_hundred_quota_freezes_exactly_one_hundred(self):
        self._set_switch(True)
        self._set_quota(self.game, "100.00")
        self._credit("150.00")
        self._claim(self._order(2))
        wallet = get_wallet(self.taker)
        self.assertEqual(wallet.frozen, Decimal("100.00"))
        self.assertEqual(wallet.balance, Decimal("50.00"))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_a_hundred_quota_is_rejected_when_the_balance_is_short(self):
        self._set_switch(True)
        self._set_quota(self.game, "100.00")
        self._credit("50.00")
        order = self._order(3)
        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_switch_off_falls_back_to_the_global_gate(self):
        """开关关 ⇒ 走全局 50（验收判据 1：与改动前逐字一致）。"""
        self._credit("50.00")
        self._claim(self._order(4))
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("50"))

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_switch_off_still_rejects_a_broke_taker(self):
        order = self._order(5)
        with self.assertRaises(ClaimError) as ctx:
            self._claim(order)
        self.assertEqual(ctx.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn("押金不足", str(ctx.exception))

    def test_ensure_deposit_reads_the_personal_quota(self):
        """直接调服务层：开关开 + 行 30 ⇒ 冻 30（与 `deposit_amount_for_user` 同源）。"""
        self._set_switch(True)
        self._set_quota(self.game, "30.00")
        self._credit("30.00")
        with transaction.atomic():
            ensure_deposit_for_claim(self.taker, self.game)
        self.assertEqual(get_wallet(self.taker).frozen, Decimal("30.00"))


# ---------------------------------------------------------------------------
# 3. API（计划 §8.1-3）
# ---------------------------------------------------------------------------
class DepositQuotaApiShapeTests(_QuotaFixtureMixin, TenantTestCase):
    """GET / PUT 的形状与保存语义。"""

    def test_get_shape_lists_games_with_global_and_personal_amounts(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("80.00"))
        self._set_quota(self.game, "120.00")
        body = self._get().json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(
            set(data),
            {"user_id", "username", "display_name", "enabled", "games"},
        )
        self.assertEqual(data["user_id"], self.taker.pk)
        self.assertEqual(data["username"], "depq-taker")
        self.assertEqual(data["display_name"], "打手甲")
        self.assertIs(data["enabled"], False)
        rows = {row["game_id"]: row for row in data["games"]}
        # `games` 只列有权限的游戏（顺序按 id，与 App 钱包同一支取面）。
        self.assertEqual([row["game_id"] for row in data["games"]], [self.game.pk, self.game2.pk])
        self.assertEqual(
            set(rows[self.game.pk]),
            {"game_id", "game_name", "global_amount", "amount"},
        )
        self.assertEqual(rows[self.game.pk]["global_amount"], "80.00")
        # enabled=false 时也返回已存的个人金额（方便开关打开后接着用）。
        self.assertEqual(rows[self.game.pk]["amount"], "120.00")
        # 没有个人行 ⇒ null（不是 0、也不是全局值）。
        self.assertIsNone(rows[self.game2.pk]["amount"])
        self.assertEqual(rows[self.game2.pk]["global_amount"], "50.00")

    def test_get_skips_soft_deleted_and_unauthorized_games(self):
        hidden = Game.objects.create(name="没有授权的游戏")
        self.game2.deleted_at = timezone.now()
        self.game2.save(update_fields=["deleted_at"])
        body = self._get().json()
        ids = [row["game_id"] for row in body["data"]["games"]]
        self.assertEqual(ids, [self.game.pk])
        self.assertNotIn(hidden.pk, ids)

    def test_account_payload_exposes_the_switch_for_the_list_column(self):
        """人员管理页那一列的**数据来源**：账号读取载荷里必须有 `deposit_quota_enabled`。

        ⚠️ 这条不是同义反复：前端只用 `GET /api/accounts/` 的行渲染开关（不逐行再打一次
        deposit-quota 端点，否则一页 20 行 = 20 个请求）。字段没接进 `account_payload` 时，
        列会**恒为关**（用户点了开、刷新后又变回去）——单测里前端夹具随便给值，
        所以那种漏接在前端侧看不出来，只能在这里钉。
        """
        from apps.accounts.payloads import account_payload

        # 默认关（模型默认），键**在**且是真 bool。
        self.assertIs(account_payload(self.taker)["deposit_quota_enabled"], False)
        self._set_switch(True)
        fresh = User.objects.get(pk=self.taker.pk)
        self.assertIs(account_payload(fresh)["deposit_quota_enabled"], True)
        # 没有档案行的账号（seed 的 admin 那类）⇒ 整批档案键不出现（前端按「关」处理）。
        no_profile = User.objects.create_user(
            username="depq-list-no-profile", password="pw-list-no-profile"
        )
        self.assertNotIn("deposit_quota_enabled", account_payload(no_profile))

    def test_put_saves_the_switch_and_amounts_and_get_returns_the_same_shape(self):
        body = self._put(
            self._payload(True, {str(self.game.pk): "100.00", str(self.game2.pk): "0"})
        ).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertIs(data["enabled"], True)
        rows = {row["game_id"]: row for row in data["games"]}
        self.assertEqual(rows[self.game.pk]["amount"], "100.00")
        # 「没行 + 0」与「显式 0 行」生效值等价（都是 0）⇒ 不落行：GET 仍是 `null`
        # （前端显示「空白 + placeholder 0」，计划 §7.2）。
        self.assertIsNone(rows[self.game2.pk]["amount"])
        self.assertEqual(
            dict(
                TakerDepositQuota.objects.filter(user=self.taker).values_list(
                    "game_id", "amount"
                )
            ),
            {self.game.pk: Decimal("100.00")},
        )
        self.assertTrue(
            StaffProfile.objects.get(user=self.taker).deposit_quota_enabled
        )
        # 出参与 GET 同形、同值。
        self.assertEqual(self._get().json()["data"], data)

    def test_put_keeps_an_existing_row_when_it_is_changed_to_zero(self):
        """原本有行 50 → 改设 0：**保留这一行并记审计**（「没有行」不等于「原本有行、改 0」）。"""
        self._set_quota(self.game, "50.00")
        body = self._put(self._payload(True, {str(self.game.pk): "0"})).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            TakerDepositQuota.objects.get(user=self.taker, game=self.game).amount,
            Decimal("0.00"),
        )
        audit = OperationLog.objects.filter(action="finance.taker_deposit_quota_amount")
        self.assertEqual(audit.count(), 1)
        self.assertEqual(audit.get().from_value, "50.00")
        self.assertEqual(audit.get().to_value, "0.00")
        self.assertEqual(self._get().json()["data"]["games"][0]["amount"], "0.00")

    def test_put_keeps_rows_for_games_it_did_not_receive(self):
        self._put(
            self._payload(True, {str(self.game.pk): "10.00", str(self.game2.pk): "20.00"})
        )
        body = self._put(self._payload(True, {str(self.game.pk): "30.00"})).json()
        self.assertEqual(body["code"], 0, body)
        rows = {row["game_id"]: row["amount"] for row in body["data"]["games"]}
        self.assertEqual(rows[self.game.pk], "30.00")
        # ⛔ 不删除没传的游戏行（避免误清历史配置）。
        self.assertEqual(rows[self.game2.pk], "20.00")

    def test_put_can_turn_the_switch_off_without_touching_rows(self):
        self._put(self._payload(True, {str(self.game.pk): "10.00"}))
        body = self._put(self._payload(False, {str(self.game.pk): "10.00"})).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["enabled"], False)
        # 已存的个人金额保留（开关关只是不读它）。
        self.assertEqual(body["data"]["games"][0]["amount"], "10.00")
        self.assertEqual(
            TakerDepositQuota.objects.get(user=self.taker, game=self.game).amount,
            Decimal("10.00"),
        )

    def test_put_rejects_negative_amounts_and_writes_nothing(self):
        body = self._put(self._payload(True, {str(self.game.pk): "-1"})).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(TakerDepositQuota.objects.exists())
        self.assertFalse(StaffProfile.objects.get(user=self.taker).deposit_quota_enabled)

    def test_put_rejects_three_decimals(self):
        body = self._put(self._payload(True, {str(self.game.pk): "10.001"})).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertIn("两位小数", body["msg"])
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_rejects_an_amount_over_the_column_width(self):
        body = self._put(
            self._payload(True, {str(self.game.pk): "10000000000.00"})
        ).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_accepts_the_column_width_boundary(self):
        body = self._put(
            self._payload(True, {str(self.game.pk): "9999999999.99"})
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["games"][0]["amount"], "9999999999.99")

    def test_put_rejects_a_game_the_taker_has_no_permission_for(self):
        other = Game.objects.create(name="没有授权的游戏")
        body = self._put(self._payload(True, {str(other.pk): "10.00"})).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(TakerDepositQuota.objects.exists())
        self.assertFalse(StaffProfile.objects.get(user=self.taker).deposit_quota_enabled)

    def test_put_rejects_a_soft_deleted_game(self):
        self.game2.deleted_at = timezone.now()
        self.game2.save(update_fields=["deleted_at"])
        body = self._put(self._payload(True, {str(self.game2.pk): "10.00"})).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_rejects_a_non_integer_game_id(self):
        body = self._put(self._payload(True, {"abc": "10.00"})).json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_malformed_amount_values_are_business_errors_not_500(self):
        """请求来源的值进类型化的列之前一律过校验（E-0010：这类请求不许 500）。"""
        for bad in (None, {}, [], "not-a-number", "NaN", "Infinity", "1e999"):
            body = self._put(
                self._payload(True, {str(self.game.pk): bad})
            ).json()
            self.assertEqual(
                body["code"], ErrorCode.VALIDATION, f"amounts 值 = {bad!r}：{body}"
            )
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_requires_enabled_and_rejects_a_non_dict_amounts(self):
        missing_enabled = self._put({"amounts": {}}).json()
        self.assertEqual(missing_enabled["code"], ErrorCode.VALIDATION, missing_enabled)
        # ⭐ 改准：`amounts` 缺席**不再是错误**——那是「只改开关」那一档
        # （人员管理页的行内开关就发这个形状）。见下面
        # `test_put_switch_only_keeps_every_existing_per_game_amount`。
        not_a_dict = self._put({"enabled": True, "amounts": []}).json()
        self.assertEqual(not_a_dict["code"], ErrorCode.VALIDATION, not_a_dict)
        not_a_bool = self._put({"enabled": "maybe", "amounts": {}}).json()
        self.assertEqual(not_a_bool["code"], ErrorCode.VALIDATION, not_a_bool)
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_accepts_an_empty_amounts_dict(self):
        """只改开关、不动任何游戏 —— `amounts` 允许空对象。"""
        body = self._put(self._payload(True, {})).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["enabled"], True)
        self.assertFalse(TakerDepositQuota.objects.exists())

    def test_put_switch_only_keeps_every_existing_per_game_amount(self):
        """只发 `{"enabled": …}` ⇒ **既有每游戏额度逐值不变**（判据钉数据库行，不看不回显）。

        这是人员管理页行内开关走的形状：⛔ 若后端把「没传 amounts」当成「按 0 覆盖」，
        这个人的每款游戏额度会被静默清成 0（= 全部免押，钱相关）。
        两行的金额**刻意不同**（50 / 300），逐行比对才能分辨「保留」与「被写成同一个值」。
        """
        self._set_quota(self.game, "50.00")
        self._set_quota(self.game2, "300.00")
        before = dict(
            TakerDepositQuota.objects.filter(user=self.taker).values_list(
                "game_id", "amount"
            )
        )
        self.assertEqual(len(before), 2)

        body = self._put({"enabled": True}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["enabled"], True)

        # ① 钉库：逐值不变（不是「回显里没变」——回显是 `_deposit_quota_payload` 重算的）。
        after = dict(
            TakerDepositQuota.objects.filter(user=self.taker).values_list(
                "game_id", "amount"
            )
        )
        self.assertEqual(after, before)
        self.assertEqual(after[self.game.pk], Decimal("50.00"))
        self.assertEqual(after[self.game2.pk], Decimal("300.00"))
        # ② 开关真的开了（否则「只改开关」这件事没发生）。
        self.assertTrue(
            StaffProfile.objects.get(user=self.taker).deposit_quota_enabled
        )
        # ③ 一次「只改开关」也不许创建新行 / 删行。
        self.assertEqual(
            TakerDepositQuota.objects.filter(user=self.taker).count(), 2
        )

    def test_put_switch_only_off_also_keeps_every_row(self):
        """反方向（开 → 关）同样不动行；本用例专门防「关掉就顺手清额度」的写法。"""
        self._set_quota(self.game, "80.00")
        self._set_switch(True)
        body = self._put({"enabled": False}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["enabled"], False)
        self.assertEqual(
            TakerDepositQuota.objects.get(user=self.taker, game=self.game).amount,
            Decimal("80.00"),
        )
        self.assertFalse(
            StaffProfile.objects.get(user=self.taker).deposit_quota_enabled
        )


class DepositQuotaApiPermissionAndLookupTests(_QuotaFixtureMixin, TenantTestCase):
    """403（无节点）与 1002（不存在 / 无档案 / 软删账号）。"""

    def test_get_and_put_are_403_without_the_node(self):
        outsider = make_logged_in_client(
            username="depq-no-node", password="pw-depq-no-node", games=None
        )
        get_body = self._get(client=outsider).json()
        self.assertEqual(get_body["code"], 403, get_body)
        self.assertEqual(get_body["msg"], "你没有打手钱包调账的权限")
        put_body = self._put(self._payload(True, {}), client=outsider).json()
        self.assertEqual(put_body["code"], 403, put_body)
        self.assertEqual(put_body["msg"], "你没有打手钱包调账的权限")
        # ⭐ 行内开关走的「只改开关」形状也必须 403（权限门在类体上，与载荷形状无关）。
        switch_only = self._put({"enabled": True}, client=outsider).json()
        self.assertEqual(switch_only["code"], 403, switch_only)
        self.assertFalse(TakerDepositQuota.objects.exists())
        self.assertFalse(StaffProfile.objects.get(user=self.taker).deposit_quota_enabled)

    def test_unknown_user_id_is_a_business_error(self):
        body = self._get(user_id=999999).json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        put_body = self._put(self._payload(True, {}), user_id=999999).json()
        self.assertEqual(put_body["code"], ErrorCode.BUSINESS, put_body)

    def test_a_user_without_a_profile_is_a_business_error(self):
        no_profile = User.objects.create_user(
            username="depq-no-profile", password="pw-no-profile"
        )
        body = self._get(user_id=no_profile.pk).json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        put_body = self._put(self._payload(True, {}), user_id=no_profile.pk).json()
        self.assertEqual(put_body["code"], ErrorCode.BUSINESS, put_body)

    def test_a_soft_deleted_account_is_a_business_error(self):
        """⚠️ 回收站里的账号不能再配额度（恢复后拿到一份没人预期的配置）。"""
        profile = StaffProfile.objects.get(user=self.taker)
        profile.deleted_at = timezone.now()
        profile.save(update_fields=["deleted_at"])
        body = self._get().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertEqual(body["msg"], "该账号已删除，不能配置个人押金额度")
        put_body = self._put(self._payload(True, {})).json()
        self.assertEqual(put_body["code"], ErrorCode.BUSINESS, put_body)
        self.assertFalse(TakerDepositQuota.objects.exists())
        self.assertFalse(StaffProfile.objects.get(user=self.taker).deposit_quota_enabled)


# ---------------------------------------------------------------------------
# 3b. 审计（计划 §8.1-3 / §6）
# ---------------------------------------------------------------------------
class DepositQuotaAuditTests(_QuotaFixtureMixin, TenantTestCase):
    """只有真的变化才写：开关一条、每款真的变了的游戏一条；幂等保存不刷屏。"""

    def _logs(self, action):
        return OperationLog.objects.filter(action=action)

    def test_switch_and_amount_changes_write_one_row_each(self):
        body = self._put(
            self._payload(True, {str(self.game.pk): "100.00"})
        ).json()
        self.assertEqual(body["code"], 0, body)
        switch_logs = list(self._logs(SWITCH_ACTION))
        amount_logs = list(self._logs(AMOUNT_ACTION))
        self.assertEqual(len(switch_logs), 1)
        self.assertEqual(len(amount_logs), 1)
        switch = switch_logs[0]
        self.assertEqual(switch.log_type, 70)  # LogType.OTHER
        self.assertEqual(switch.actor_id, self.admin.pk)
        self.assertEqual(switch.target_kind, "taker_wallet")
        self.assertEqual(switch.target_id, str(self.taker.pk))
        self.assertEqual(switch.target_name, "打手甲")
        self.assertEqual(switch.from_value, "关")
        self.assertEqual(switch.to_value, "开")
        amount = amount_logs[0]
        self.assertEqual(amount.log_type, 70)
        self.assertEqual(amount.actor_id, self.admin.pk)
        self.assertEqual(amount.game_name, "合成游戏甲")
        # 旧行不存在 ⇒ 0 → 新值。
        self.assertEqual(amount.from_value, "0.00")
        self.assertEqual(amount.to_value, "100.00")
        self.assertIn("合成游戏甲", amount.summary)

    def test_a_no_op_save_writes_no_audit_rows(self):
        self._put(self._payload(True, {str(self.game.pk): "100.00"}))
        before = OperationLog.objects.count()
        body = self._put(self._payload(True, {str(self.game.pk): "100.00"})).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.count(), before)

    def test_switching_off_is_also_audited(self):
        self._put(self._payload(True, {}))
        body = self._put(self._payload(False, {})).json()
        self.assertEqual(body["code"], 0, body)
        latest = self._logs(SWITCH_ACTION).order_by("-id").first()
        self.assertEqual(latest.from_value, "开")
        self.assertEqual(latest.to_value, "关")

    def test_switch_only_writes_exactly_one_switch_log_and_no_amount_log(self):
        """只改开关（`amounts` 缺席）⇒ **恰好一条**开关审计、**零条**金额审计。

        判据打在 `OperationLog` 行上（§5-G：断言留痕请断言数据库行）。这条同时守住
        「只改开关」不会因为「什么都没传」而写出 0→0 的金额日志（那会让审计表被无变化刷屏）。
        """
        self._set_quota(self.game, "50.00")
        self._set_quota(self.game2, "300.00")
        self.assertEqual(self._logs(AMOUNT_ACTION).count(), 0)

        body = self._put({"enabled": True}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._logs(SWITCH_ACTION).count(), 1)
        self.assertEqual(self._logs(AMOUNT_ACTION).count(), 0)
        switch = self._logs(SWITCH_ACTION).get()
        self.assertEqual(switch.actor_id, self.admin.pk)
        self.assertEqual(switch.target_kind, "taker_wallet")
        self.assertEqual(switch.target_id, str(self.taker.pk))
        self.assertEqual(switch.target_name, "打手甲")
        self.assertEqual(switch.from_value, "关")
        self.assertEqual(switch.to_value, "开")

    def test_only_the_changed_game_gets_an_amount_row(self):
        self._put(
            self._payload(True, {str(self.game.pk): "10.00", str(self.game2.pk): "20.00"})
        )
        before = self._logs(AMOUNT_ACTION).count()
        body = self._put(
            self._payload(True, {str(self.game.pk): "10.00", str(self.game2.pk): "30.00"})
        ).json()
        self.assertEqual(body["code"], 0, body)
        new_logs = list(self._logs(AMOUNT_ACTION).order_by("id")[before:])
        self.assertEqual(len(new_logs), 1)
        self.assertEqual(new_logs[0].game_name, "合成游戏乙")
        self.assertEqual(new_logs[0].from_value, "20.00")
        self.assertEqual(new_logs[0].to_value, "30.00")


# ---------------------------------------------------------------------------
# 4. 读面（计划 §8.1-4，验收判据 4）
# ---------------------------------------------------------------------------
class DepositQuotaReadFacesTests(_QuotaFixtureMixin, TenantTestCase):
    """App 钱包的 `deposit_rules` 与客服钱包列表的 `deposit_required`。"""

    def _app_token(self) -> str:
        raw = new_token()
        AppToken.objects.create(
            user=self.taker,
            key_hash=hash_token(raw),
            expires_at=timezone.now() + timedelta(days=1),
            client="app-android",
        )
        return raw

    def _app_wallet(self) -> dict:
        raw = self._app_token()
        client = Client(SERVER_NAME=self.tenant.get_primary_domain().domain)
        body = client.get(
            "/api/app/wallet/", HTTP_AUTHORIZATION="Bearer " + raw
        ).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_app_wallet_deposit_rules_use_the_personal_amounts(self):
        self._set_switch(True)
        self._set_quota(self.game, "12.00")
        # game2 没填 ⇒ 0 元（该游戏免押）。
        rules = {
            row["game_id"]: row["amount"]
            for row in self._app_wallet()["deposit_rules"]
        }
        self.assertEqual(rules[self.game.pk], "12.00")
        self.assertEqual(rules[self.game2.pk], "0.00")

    def test_app_wallet_deposit_rules_use_the_global_amounts_when_switch_is_off(self):
        self._set_quota(self.game, "12.00")
        rules = {
            row["game_id"]: row["amount"]
            for row in self._app_wallet()["deposit_rules"]
        }
        # 开关关 ⇒ 个人行被忽略，走全局 50（两个都 50）。
        self.assertEqual(rules[self.game.pk], "50.00")
        self.assertEqual(rules[self.game2.pk], "50.00")

    def _wallet_row(self) -> dict:
        body = self.admin_client.get("/api/business/wallets/").json()
        self.assertEqual(body["code"], 0, body)
        return next(
            row for row in body["data"]["items"] if row["username"] == "depq-taker"
        )

    def test_support_list_uses_the_max_personal_amount(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("50.00"))
        GameDepositRule.objects.create(game=self.game2, amount=Decimal("300.00"))
        self._set_switch(True)
        self._set_quota(self.game, "10.00")
        self._set_quota(self.game2, "80.00")
        row = self._wallet_row()
        # 个人额度的 max = 80；⛔ 不是全局 300，也不是全局最低 50。
        self.assertEqual(row["deposit_required"], "80.00")
        # 这个打手还没有钱包行 ⇒ frozen=0 < 80 ⇒ 「不够」。
        self.assertIs(row["deposit_enough"], False)

    def test_support_list_uses_the_global_max_when_switch_is_off(self):
        GameDepositRule.objects.create(game=self.game, amount=Decimal("50.00"))
        GameDepositRule.objects.create(game=self.game2, amount=Decimal("300.00"))
        self._set_quota(self.game, "10.00")
        self._set_quota(self.game2, "80.00")
        row = self._wallet_row()
        self.assertEqual(row["deposit_required"], "300.00")
        self.assertIs(row["deposit_enough"], False)

    def test_support_list_treats_missing_personal_rows_as_zero(self):
        self._set_switch(True)
        self._set_quota(self.game2, "0.00")
        row = self._wallet_row()
        self.assertEqual(row["deposit_required"], "0.00")
        self.assertIs(row["deposit_enough"], True)


# ---------------------------------------------------------------------------
# 5. 跨租户（计划 §8.1-5）
# ---------------------------------------------------------------------------
class CrossTenantDepositQuotaTests(TenantTestCase):
    """以 A 的会话拿不到 B 的账号；判据用「租户内唯一、且两租户取值不同」的登录账号。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="depq_tenant_b",
            name="押金额度租户 B",
            domain="depq-tenant-b.test.local",
        )

    def setUp(self):
        Game.objects.create(name="租户A游戏")
        self.user_a = User.objects.create_user(
            pk=4242, username="only-a-quota", password="pw-a"
        )
        self.user_a.first_name = "租户 A 的打手"
        self.user_a.save(update_fields=["first_name"])
        StaffProfile.objects.create(
            user=self.user_a, mobile="13600000001", member_type=MemberType.PARTNER
        )
        self.client_a = make_logged_in_client(
            username="depq-a-admin",
            password="pw-a-admin",
            groups=("客服",),
            nodes=(NODE,),
            games=None,
        )

        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="租户B游戏")
            self.user_b = User.objects.create_user(
                pk=4242, username="only-b-quota", password="pw-b"
            )
            StaffProfile.objects.create(
                user=self.user_b, mobile="13600000002", member_type=MemberType.PARTNER
            )
            self.user_b_other = User.objects.create_user(
                pk=9999, username="only-b-quota-2", password="pw-b2"
            )
            StaffProfile.objects.create(
                user=self.user_b_other,
                mobile="13600000003",
                member_type=MemberType.PARTNER,
            )
            TakerDepositQuota.objects.create(
                user=self.user_b, game=game_b, amount=Decimal("66.00")
            )

    def test_an_id_that_only_exists_in_b_is_a_business_error_in_a(self):
        body = self.client_a.get(quota_url(self.user_b_other.pk)).json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        put_body = self.client_a.put(
            quota_url(self.user_b_other.pk),
            {"enabled": True, "amounts": {}},
            content_type="application/json",
        ).json()
        self.assertEqual(put_body["code"], ErrorCode.BUSINESS, put_body)
        with tenant_context(self.tenant_b):
            self.assertFalse(
                TakerDepositQuota.objects.filter(user=self.user_b_other).exists()
            )
            self.assertFalse(
                StaffProfile.objects.get(user=self.user_b_other).deposit_quota_enabled
            )

    def test_the_same_pk_only_touches_the_current_tenants_row(self):
        """A 与 B 各有一个 pk=4242 的打手：A 的保存只改 A 那个。"""
        body = self.client_a.get(quota_url(4242)).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["username"], "only-a-quota")

        saved = self.client_a.put(
            quota_url(4242),
            {"enabled": True, "amounts": {}},
            content_type="application/json",
        ).json()
        self.assertEqual(saved["code"], 0, saved)
        self.assertTrue(StaffProfile.objects.get(user=self.user_a).deposit_quota_enabled)

        with tenant_context(self.tenant_b):
            profile_b = StaffProfile.objects.get(user__username="only-b-quota")
            self.assertFalse(
                profile_b.deposit_quota_enabled, "跨租户把开关改了"
            )
            self.assertEqual(
                TakerDepositQuota.objects.get(user=self.user_b).amount,
                Decimal("66.00"),
                "跨租户把额度改了",
            )
