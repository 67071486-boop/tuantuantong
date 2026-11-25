"""P4-5：客服销售提成率读取——`PROFIT_PERCENT`(30) 口径（扩展 `apps/business/commission.py`）。

    **为什么单独一个文件**：`test_commission.py` 是 B1-1 打手路径（`ORDER_PERCENT`）的判据；
    本文件钉 P4 新增的客服路径。两处共用**同一个** `commission_percent_of`（E-0021「同一事实
    两套判据」的反面）——`mode=` 是**调用方声明的口径**，档案模式必须与它相等。

    ⚠️ **期望值一律写字面值，不引 `FULL_PERCENT` / `PERCENT_MAX` 等被测模块的常量**
    （E-0024：自指断言在常量被改坏时不会红）。

    ⚠️ **反转登记**：B1-1 曾有一条 `test_profit_percent_mode_is_rejected_with_its_own_reason`
    钉住「mode 30 被拒」；P4 按业务方裁定改为可算，那条测试已在 `test_commission.py` 里** + 反转**
    （本项目第二处旧口径反转，第一处见 D-0015）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.commission import CommissionError, commission_percent_of
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant

ORDER = CommissionMode.ORDER_PERCENT
FIXED = CommissionMode.FIXED
PROFIT = CommissionMode.PROFIT_PERCENT


class ProfitPercentTests(TenantTestCase):
    def _user(self, username="cs1", **profile):
        user = User.objects.create_user(username=username)
        if profile:
            StaffProfile.objects.create(user=user, **profile)
        return user

    # ── mode 30 的主路径 ────────────────────────────────────────────

    def test_profit_mode_value_is_read(self):
        """mode 30 + `value = 20` ⇒ `Decimal("20")`（字面值，不引常量——E-0024）。"""
        user = self._user(commission_mode=PROFIT, commission_value=Decimal("20.00"))
        self.assertEqual(
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT),
            Decimal("20"),
        )

    def test_profit_mode_zero_falls_back_to_full(self):
        """Ruling M：`value == 0` 的「未配置 ⇒ 全额」回落**对 mode 30 同样适用**。

    ⚠️ 这是**裁定**而不是推算：Q3 原文只说了打手路径（mode 20），P4-5 的编排方 Ruling M
    明确把它推广到 mode 30（无档案 与 `value == 0` 两种情形一致）。
"""
        user = self._user(commission_mode=PROFIT, commission_value=Decimal("0.00"))
        self.assertEqual(
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT),
            Decimal("100"),
        )

    def test_profit_mode_above_percent_max_is_rejected(self):
        """上界**对两个 mode 同一道**：mode 30 + `value > 100` ⇒ 拒绝（`VALIDATION`）。"""
        user = self._user(commission_mode=PROFIT, commission_value=Decimal("100.01"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        # 整句钉死（不再是 `assertIn("100", ...)`——消息里出现任意「1000」也能过）
        self.assertEqual(
            ctx.exception.message,
            "提成百分比必须在 0~100 之间，当前是 100.01",
        )

    def test_profit_mode_negative_value_is_rejected(self):
        """下界同理：负数百分比 ⇒ 拒绝。"""
        user = self._user(commission_mode=PROFIT, commission_value=Decimal("-1.00"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)

    def test_no_profile_falls_back_to_full_under_profit_mode(self):
        """无档案 + `mode=PROFIT_PERCENT` ⇒ `Decimal("100")`（与 mode 20 一致——Ruling M）。"""
        user = self._user()          # 不建档案
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())   # 前置哨兵
        self.assertEqual(
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT),
            Decimal("100"),
        )

    # ── 正向对照：mode 30 的支持不是「一律放行」 ────────────────────

    def test_fixed_mode_is_rejected_under_both_requested_modes(self):
        """`FIXED`(10) 的 `value` 是**元**不是百分数 ⇒ 两个请求模式下**都拒**（正向对照）。"""
        user = self._user(commission_mode=FIXED, commission_value=Decimal("10.00"))
        for requested in (CommissionMode.ORDER_PERCENT, CommissionMode.PROFIT_PERCENT):
            with self.subTest(requested=requested):
                with self.assertRaises(CommissionError) as ctx:
                    commission_percent_of(user, mode=requested)
                self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
                # 可辨：说清档案是「固定金额」，而不是「数值越界」
                self.assertIn("固定金额", ctx.exception.message)

    # ── 模式不匹配：两个方向都要拒，且 msg 能分辨两边 ──────────────

    def test_order_profile_rejects_profit_request(self):
        """档案 mode=20 但要 `PROFIT_PERCENT` ⇒ 拒绝；客服方向新文案（R4/D5）逐字。

    ⚠️ 整句钉死**方向**：两条 `assertIn` 顺序无关，把生产代码里两个标签对调也能全绿
    （评审 I-1 实测）。期望值是生产文案的逐字复制（`commission.py` 的客服方向模板常量）。
"""
        user = self._user(
            commission_mode=ORDER,
            commission_value=Decimal("20.00"),
            username="测试客服甲",
        )
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertEqual(
            ctx.exception.message,
            "该单的销售客服（用户名 测试客服甲）的提成模式是「订单价百分比」，"
            "本处需要「利润的百分比」；请到人员管理把 TA 的提成模式改成「利润的百分比」后再操作",
        )
        # ⛔ 防「整支替换」：客服方向**不许**说成「当前登录账号」（批量重算时财务可以
        # 一次对**许多别人录的单**重算 ⇒ 那句话会变成事实错误）
        self.assertNotIn("当前登录账号", ctx.exception.message)

    def test_profit_profile_rejects_default_order_request(self):
        """档案 mode=30 但**不传 `mode`**（默认打手口径）⇒ 打手方向新文案，逐字。

    ⚠️ 这条钉的是 B1-1 反转里**仍然成立**的部分：打手路径（默认 mode 20）对 mode 30 档案
    依旧拒绝，只是理由从「属 P4、本阶段不支持」变成「模式不匹配」。
"""
        user = self._user(
            commission_mode=PROFIT,
            commission_value=Decimal("30.00"),
            username="测试打手乙",
        )
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        # 反方向同样整句钉死（档案=利润、期望=订单价），与上一条互为镜像。
        self.assertEqual(
            ctx.exception.message,
            "该单的接手人（用户名 测试打手乙）的提成模式是「利润的百分比」，"
            "本处需要「订单价百分比」；请到人员管理把 TA 的提成模式改成「订单价百分比」后再操作",
        )

    def test_commission_error_str_is_the_message_itself(self):
        """`str(exc) == exc.message`（批量端点用 `str(exc)`、单笔端点用 `exc.message`，两者必须等价）。"""
        exc = CommissionError("甲", ErrorCode.VALIDATION)
        self.assertEqual(str(exc), "甲")
        self.assertEqual(exc.message, "甲")

    # ── 兜底：未知取值仍然响亮拒绝（原来的 `mode != PERCENT_MODE` 分支）──

    def test_unknown_profile_mode_is_rejected(self):
        """档案里存了 `choices` 之外的取值（DB 不校验）⇒ 拒绝，**不静默按百分比算**。"""
        user = self._user(commission_mode=99, commission_value=Decimal("20.00"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)

    def test_unknown_requested_mode_is_rejected(self):
        """调用方传了本层不认识的请求模式 ⇒ 拒绝（不因「档案恰好也是 99」而放行）。"""
        user = self._user(commission_mode=ORDER, commission_value=Decimal("20.00"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user, mode=99)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)


class ProfitCommissionIsolationTests(TenantTestCase):
    """`AGENTS.md` §4：新增读取路径要有跨租户回归；判据用**用户名**（不用主键）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="p4c_a", name="P4C 租户 A", domain="p4c-a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="p4c_b", name="P4C 租户 B", domain="p4c-b.test.local"
        )

    def test_profit_profile_does_not_cross_tenants(self):
        """⚠️ **判据不能用主键**：两个 schema 的 `auth_user` 主键序列各自从 1 开始。

    用主键指认「另一个租户的那一行」会命中**本租户自己的行**（`test_commission.py` 里
    记录在案的那次踩坑）。这里按**租户内唯一、且两租户取值不同**的用户名判。
"""
        with tenant_context(self.tenant_a):
            user = User.objects.create_user(username="p4c_a_cs")
            StaffProfile.objects.create(
                user=user, commission_mode=PROFIT, commission_value=Decimal("30.00")
            )
            self.assertEqual(
                commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT),
                Decimal("30"),
            )   # 本租户读得到

        with tenant_context(self.tenant_b):
            # 非空哨兵：B 里也有自己的 mode 30 档案，否定断言才有意义
            b_user = User.objects.create_user(username="p4c_b_cs")
            StaffProfile.objects.create(
                user=b_user, commission_mode=PROFIT, commission_value=Decimal("33.00")
            )
            self.assertTrue(StaffProfile.objects.filter(user=b_user).exists())
            # A 的档案不可见——按**用户名**判（不是主键）
            self.assertFalse(
                StaffProfile.objects.filter(user__username="p4c_a_cs").exists()
            )
            self.assertEqual(
                commission_percent_of(b_user, mode=CommissionMode.PROFIT_PERCENT),
                Decimal("33"),
            )   # 自己的仍可读
