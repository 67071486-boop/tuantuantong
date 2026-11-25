"""Task 1（P3c 抢单语义收口）：按人抢单限制的读取层（`apps.business.grab_limits`）。

判据来源：P3c 实施计划 Task 1 brief（**C2**：缺档案 ⇒ 回落全局常量；**C4**：空 `order_type`
按代练走）＋ 编排方裁定「**本层只取值、不校验**」（价格越界与「该单没有接单价」两支的判定
留在 `claim.check_grab_limits`，是 Task 2 的范围）。

⚠️ **断言钉「字面值」，不钉被测常量**（E-0009 / E-0024）：期望值一律写成
`Decimal("0")` / `Decimal("99999")` / `10` / `None`，**不写** `GLOBAL_PRICE_MIN` 这类符号——
拿被测常量断自己时，把常量改坏测试**照样绿**（自指断言，本项目反复点名的形状）。

⚠️ **`0` 与 `None` 是两种语义**（E-0021）：`grab_limit_dl == 0` ⇒「**不限制**」⇒ `max_active is None`；
「缺档案」⇒「**未配**」⇒ 回落全局 `10`。两者在返回值上必须可区分，所以
`assertIsNone(...)` 与 `assertEqual(..., 10)` 各有独立用例，且有一条把两者放在同一个用例里对照。

⚠️ **判据不能用主键跨租户指认**：两个 schema 的 `auth_user` 主键序列各自从 1 开始，
所以跨租户那条用例按**租户内唯一且两租户取值不同**的用户名（`a_taker` / `b_taker`）判，
并用**两租户取值不同**的 `grab_limit_dl`（3 / 9）区分读到的到底是哪一边。
"""
import dataclasses
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.grab_limits import GrabLimits, limits_for_order, limits_of
from apps.business.models import Game, Order, OrderType
from apps.platform.provisioning import provision_tenant


def _no(n):
    """20 位订单号（P2 起订单的对外标识是 `order_no`，`lookup_value_regex = r"\\d{20}"`）。"""
    return f"20260922{n:012d}"


class GrabLimitsTests(TenantTestCase):
    """直接测纯逻辑——不经 HTTP（本层不碰 request）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")

    def _user(self, username="taker1", with_profile=False, **profile):
        user = User.objects.create_user(username=username)
        if with_profile:
            StaffProfile.objects.create(user=user, **profile)
        return user

    def _order(self, no, order_type=OrderType.BOOST):
        """只给 `order_type`——本层**不读**订单的价格等其它字段。

        `hall_price` 不传 ⇒ 它是 **NULL**（`models.py` 的 `hall_price` 是 `null=True`）；
        这正是「该单没有接单价」那一支的夹具形状（判定在 Task 2，不在本层）。
        """
        return Order.objects.create(game=self.game, order_no=no, order_type=order_type)

    # ── 缺档案 ⇒ 回落全局常量（C2 推荐分支）─────────────────────────────
    def test_missing_profile_falls_back_to_global(self):
        user = self._user()                                                  # 不建档案
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())    # 前置哨兵
        limits = limits_of(user.id)
        self.assertEqual(limits.price_min, Decimal("0"))
        self.assertEqual(limits.price_max, Decimal("99999"))
        self.assertEqual(limits.max_active, 10)

    def test_missing_profile_for_order_falls_back_to_global(self):
        """`limits_for_order` 也要处理缺档案，且**不能**把它当成「不限制」。

        用 `COMPANION` 下单：缺档案时走得通、且回落的是全局 10（不是 `None`）。
        """
        user = self._user()
        order = self._order(_no(1), OrderType.COMPANION)
        limits = limits_for_order(user.id, order)
        self.assertEqual(limits.price_min, Decimal("0"))
        self.assertEqual(limits.price_max, Decimal("99999"))
        self.assertEqual(limits.max_active, 10)

    # ── 档案在：价格两列各自回落（本层只取值，不判区间是否越界）──────────
    def test_profile_with_null_prices_falls_back_to_global(self):
        user = self._user(with_profile=True)
        self.assertTrue(StaffProfile.objects.filter(user=user).exists())     # 前置哨兵
        limits = limits_of(user.id)
        self.assertEqual(limits.price_min, Decimal("0"))
        self.assertEqual(limits.price_max, Decimal("99999"))

    def test_price_min_is_read(self):
        """「一端配、另一端未配」的生效区间 = 配的那端 + 全局的另一端。

        具体组合：`grab_price_min = Decimal("5")` 且 `max` 未配 ⇒ `(Decimal("5"), Decimal("99999"))`
        ——两列**互不牵连**（依据：模型的 CHECK 只在两端都有值时才要求有序）。
        """
        user = self._user(with_profile=True, grab_price_min=Decimal("5"))
        limits = limits_of(user.id)
        self.assertEqual(limits.price_min, Decimal("5"))
        self.assertEqual(limits.price_max, Decimal("99999"))

    def test_price_max_is_read(self):
        user = self._user(with_profile=True, grab_price_max=Decimal("200"))
        limits = limits_of(user.id)
        self.assertEqual(limits.price_min, Decimal("0"))
        self.assertEqual(limits.price_max, Decimal("200"))

    def test_both_prices_are_read(self):
        user = self._user(
            with_profile=True,
            grab_price_min=Decimal("5"),
            grab_price_max=Decimal("200"),
        )
        limits = limits_of(user.id)
        self.assertEqual(limits.price_min, Decimal("5"))
        self.assertEqual(limits.price_max, Decimal("200"))

    def test_price_fields_are_decimal_never_none(self):
        """价格两字段是 `Decimal`（未配时回落全局）——调用方不需要再判空。

        三条产生路径都走一遍（缺档案 / 档案在但两列 `null` / 档案在且两列有值）：
        「永不为 `None`」这句话要按分支覆盖，只测一条分支证不出其余两条。
        """
        cases = (
            ("missing", self._user(username="no_profile")),
            ("null_prices", self._user(username="null_prices", with_profile=True)),
            (
                "set_prices",
                self._user(
                    username="set_prices",
                    with_profile=True,
                    grab_price_min=Decimal("5"),
                    grab_price_max=Decimal("200"),
                ),
            ),
        )
        for label, user in cases:
            with self.subTest(label=label):
                limits = limits_of(user.id)
                self.assertIsInstance(limits.price_min, Decimal)
                self.assertIsInstance(limits.price_max, Decimal)

    # ── 上限：`0` = 不限制，`None` = 未配（E-0021 的边界）───────────────
    def test_zero_dl_limit_means_unlimited(self):
        user = self._user(with_profile=True, grab_limit_dl=0)
        self.assertIsNone(limits_of(user.id).max_active)

    # ── 禁自助接单：缺档案 = **不禁止**（与价格/上限的「回落」不是一回事）────
    def test_forbid_self_service_defaults_to_false_without_a_profile(self):
        """缺档案 ⇒ **不禁止**（`False`）。⚠️ 与价格/上限的「回落」不是一回事：
        那两项缺档案时回落到全局拟定值，这一项缺档案就是「没有施加这个限制」。"""
        user = self._user("taker-noprofile")
        self.assertIs(limits_of(user.id).forbid_self_service, False)

    def test_forbid_self_service_is_read_from_the_profile(self):
        """档案里开着 ⇒ 读取层带出来（`is True`，不是真值判断）。"""
        user = self._user("taker-forbid", with_profile=True, grab_forbid_self_service=True)
        self.assertIs(limits_for_order(user.id, self._order(_no(900))).forbid_self_service, True)

    def test_forbid_self_service_is_false_when_the_profile_says_so(self):
        user = self._user("taker-ok", with_profile=True, grab_forbid_self_service=False)
        self.assertIs(limits_of(user.id).forbid_self_service, False)

    def test_zero_pw_limit_means_unlimited(self):
        user = self._user(with_profile=True, grab_limit_pw=0)
        order = self._order(_no(2), OrderType.COMPANION)
        self.assertIsNone(limits_for_order(user.id, order).max_active)

    def test_positive_dl_limit_is_read(self):
        user = self._user(with_profile=True, grab_limit_dl=3)
        self.assertEqual(limits_of(user.id).max_active, 3)

    def test_zero_and_missing_profile_are_distinguishable(self):
        """本任务最重要的一条边界：`0`（不限制 ⇒ `None`）与「未配」（⇒ `10`）**不同**。"""
        unlimited = self._user(username="zero", with_profile=True, grab_limit_dl=0)
        missing = self._user(username="missing")
        self.assertIsNone(limits_of(unlimited.id).max_active)
        self.assertEqual(limits_of(missing.id).max_active, 10)
        # 反向哨兵：若实现把两种情形都写成同一个值，上面两条必有一条红
        self.assertNotEqual(limits_of(unlimited.id).max_active, 10)

    # ── `order_type` 选 dl / pw（C4：空串按代练走）──────────────────────
    def test_boost_order_uses_dl_limit(self):
        user = self._user(with_profile=True, grab_limit_dl=3, grab_limit_pw=7)
        order = self._order(_no(3), OrderType.BOOST)
        self.assertEqual(limits_for_order(user.id, order).max_active, 3)

    def test_companion_order_uses_pw_limit(self):
        user = self._user(with_profile=True, grab_limit_dl=3, grab_limit_pw=7)
        order = self._order(_no(4), OrderType.COMPANION)
        self.assertEqual(limits_for_order(user.id, order).max_active, 7)

    def test_blank_order_type_uses_dl_limit(self):
        """存量里 `order_type` 可能是空串（字段 `blank=True`）——按 C4 走**代练**（`dl`）。"""
        user = self._user(with_profile=True, grab_limit_dl=3, grab_limit_pw=7)
        order = self._order(_no(5), "")
        self.assertEqual(limits_for_order(user.id, order).max_active, 3)

    def test_unknown_order_type_uses_dl_limit(self):
        """`choices` 之外的第三取值也走 `dl`——判据是**穷尽式**的（只有 `COMPANION` 走 `pw`）。

        `Order.order_type` 是 `CharField + choices`，DB 层不拦第三值；本层把它与空串放在
        同一条 `else` 分支，这是**有意**的（模块 docstring 第 4 条），不是漏判。
        """
        user = self._user(with_profile=True, grab_limit_dl=3, grab_limit_pw=7)
        order = self._order(_no(6), "NOT_A_CHOICE")
        self.assertEqual(limits_for_order(user.id, order).max_active, 3)

    def test_limits_of_ignores_order_type(self):
        """`limits_of` 不含订单维度：它拿的是 `dl`（只关心上限的调用方）。"""
        user = self._user(with_profile=True, grab_limit_dl=3, grab_limit_pw=7)
        self.assertEqual(limits_of(user.id).max_active, 3)

    # ── 本层只取值，不判越界（越界判定留在 `check_grab_limits`）──────────
    def test_order_price_is_not_part_of_this_layer(self):
        """订单价格（含 `None` / `0` / 负数 / 超上界）都不影响本层的返回，也不触发拒绝。

        `None` = 「该单没有接单价」、越界 = 「不在区间内」——两者都要由 Task 2 的
        `check_grab_limits` 翻译成 `GRAB_LIMIT_EXCEEDED`；本层**提前拒绝**会让 Task 2 拿不到值。
        """
        user = self._user(
            with_profile=True,
            grab_price_min=Decimal("5"),
            grab_price_max=Decimal("100"),
        )
        for i, price in enumerate(
            (None, Decimal("0"), Decimal("-1"), Decimal("999999"))
        ):
            order = self._order(_no(100 + i), OrderType.BOOST)
            order.hall_price = price
            limits = limits_for_order(user.id, order)
            self.assertEqual(limits.price_min, Decimal("5"))
            self.assertEqual(limits.price_max, Decimal("100"))

    # ── `GrabLimits` 的形状（brief 的接口约定）─────────────────────────
    def test_limits_are_frozen(self):
        limits = limits_of(self._user().id)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            limits.price_min = Decimal("1")

    def test_limits_compare_by_value(self):
        user = self._user(with_profile=True, grab_limit_dl=0, grab_price_min=Decimal("5"))
        self.assertEqual(
            limits_of(user.id),
            # 第 4 个位置是 P3e 加的开关；这里写**字面量** `False`（该档案没配这一项）。
            GrabLimits(Decimal("5"), Decimal("99999"), None, False),
        )


class GrabLimitsIsolationTests(TenantTestCase):
    """`AGENTS.md` §4：任何新增查询都要有跨租户回归。

    ⚠️ **判据不用主键**（同 `test_commission.py` 里那条在案的形状）：两个 schema 的
    `auth_user` 主键序列各自从 1 开始，用 `user_id` 指认「另一个租户的那一行」会命中
    **自己租户的行** ⇒ 断言恒假或恒真。这里用**用户名**（租户内唯一、两租户取值不同）
    与**两租户取值不同的 `grab_limit_dl`**（3 / 9）判。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="p3c_a", name="P3C 租户 A", domain="p3c-a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="p3c_b", name="P3C 租户 B", domain="p3c-b.test.local"
        )

    def test_profile_does_not_cross_tenants(self):
        with tenant_context(self.tenant_a):
            a_user = User.objects.create_user(username="a_taker")
            StaffProfile.objects.create(user=a_user, grab_limit_dl=3)
            self.assertEqual(limits_of(a_user.id).max_active, 3)   # 本租户读得到

        with tenant_context(self.tenant_b):
            # 非空哨兵：B 里也有自己的档案，否定断言才有意义
            b_user = User.objects.create_user(username="b_taker")
            StaffProfile.objects.create(user=b_user, grab_limit_dl=9)
            self.assertTrue(StaffProfile.objects.filter(user=b_user).exists())
            # A 的档案不可见——按**用户名**判（不是主键）
            self.assertFalse(StaffProfile.objects.filter(user__username="a_taker").exists())
            # B 里读到的是 B 自己的 9，不是 A 的 3
            self.assertEqual(limits_of(b_user.id).max_active, 9)
