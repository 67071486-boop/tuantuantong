"""B1-1：提成读取适配层（P3b 计划 §1 Q1 裁定「复用 `StaffProfile`」/ Q3 回落 / Q4 量纲）。

⚠️ **纯逻辑，不经 HTTP**：它是 `/submit`、`/adjust`、`temp_amount` 重算三处的**唯一**提成来源，
所以判据要在这里钉死，而不是在三个端点里各测一遍。

⚠️ **断言钉「字面值」，不钉常量**：第一版写成 `== FULL_PERCENT` / `== PERCENT_MAX`，
于是把常量改成 `0` 时测试**照样绿**（自指断言——本项目反复点名的形状）。
改回字面值后，「回落必须是 100%」这条裁定才真的被钉住（变异自证：常量改成 `0` ⇒ 红）。

⚠️ 判据来源（spec §3.3 已被账号线取代 + P3b 计划的裁定）：
- 提成落在 `apps/accounts/StaffProfile`（`commission_mode` / `commission_value`）；
- **量纲 0–100 的百分数**，算式一律 `× value / 100`（P4 的 B2）；
- **未配置（无档案 或 `value == 0`）⇒ 回落全额（100%）**（Q3 裁定原文——注意「0 也算未配置」这条
  是有代价的：业务上无法把某人设成「不付提成」，需要那样做要重新裁定）；
- **`ORDER_PERCENT`(20) 与 `PROFIT_PERCENT`(30) 都按同一量纲可算**（P4-5 扩展：客服提成
  = 按利润百分比，业务方裁定）——但**请求的 `mode` 必须与档案的 `commission_mode` 相等**，
  否则拒绝（`AGENTS.md` §4「不重复同一事实」）；
- `FIXED`(10)（固定金额，不是百分数）在**两个 `mode` 下都拒绝**，不猜、不静默按百分比算。
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

PV = CommissionMode.ORDER_PERCENT
FIXED = CommissionMode.FIXED
PROFIT = CommissionMode.PROFIT_PERCENT


class CommissionTests(TenantTestCase):
    def _user(self, username="taker1", **profile):
        user = User.objects.create_user(username=username)
        if profile:
            StaffProfile.objects.create(user=user, **profile)
        return user

    def test_percent_is_read_from_staff_profile(self):
        user = self._user(commission_mode=PV, commission_value=Decimal("20.00"))
        self.assertEqual(commission_percent_of(user), Decimal("20.00"))

    def test_missing_profile_falls_back_to_full(self):
        """没有档案 ⇒ 回落全额（Q3）。⚠️ 这条**必须**存在：`/assign` 允许指派给任何活跃账号。"""
        user = self._user()          # 不建档案
        self.assertFalse(StaffProfile.objects.filter(user=user).exists())   # 前置哨兵
        self.assertEqual(commission_percent_of(user), Decimal("100"))

    def test_zero_value_falls_back_to_full(self):
        """`value == 0` 也当「未配置」⇒ 全额。

        ⚠️ 这是**裁定原文**（Q3 的选项里明写了「或 `commission_value = 0`」），
        代价是「业务上无法把某人设成不付提成」。要改这条就是改**发钱结果**，必须重新裁定。
        """
        user = self._user(commission_mode=PV, commission_value=Decimal("0.00"))
        self.assertEqual(commission_percent_of(user), Decimal("100"))

    def test_value_100_is_allowed(self):
        """边界**内侧**：100% 合法（此时与回落全额同值，但**不是**回落）。"""
        user = self._user(commission_mode=PV, commission_value=Decimal("100.00"))
        self.assertEqual(commission_percent_of(user), Decimal("100"))

    def test_value_above_percent_max_is_rejected(self):
        """边界**外侧**：账号线允许写入 >100（写入侧无上界，见 spec §16.4 #19），计算侧必须拒。"""
        user = self._user(commission_mode=PV, commission_value=Decimal("100.01"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertIn("100", ctx.exception.message)      # 可辨来源：说清上界

    def test_negative_value_is_rejected(self):
        user = self._user(commission_mode=PV, commission_value=Decimal("-5.00"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)

    def test_fixed_amount_mode_is_rejected(self):
        """`FIXED`(10) 的 `value` 是**元**不是百分数——按百分比算会算错钱，必须拒。"""
        user = self._user(commission_mode=FIXED, commission_value=Decimal("50.00"))
        with self.assertRaises(CommissionError) as ctx:
            commission_percent_of(user)
        self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
        self.assertIn("固定金额", ctx.exception.message)   # 可辨：是"不支持这种模式"而不是"数值越界"

    def test_profit_percent_mode_is_readable_when_requested_explicitly(self):
        """`PROFIT_PERCENT`(30) **可算**——但必须显式按该模式请求（P4-5 反转）。

        ⚠️ **反转登记（本项目第二处「按旧口径假绿」的反转；第一处见 D-0015 反转 P2 金额测试）**：

        - **原用例名**：`test_profit_percent_mode_is_rejected_with_its_own_reason`
        - **原断言**：
          ```python
          user = self._user(commission_mode=PROFIT, commission_value=Decimal("30.00"))
          with self.assertRaises(CommissionError) as ctx:
              commission_percent_of(user)
          self.assertEqual(ctx.exception.code, ErrorCode.VALIDATION)
          self.assertIn("利润", ctx.exception.message)
          ```
          它钉的是 B1-1 时代的旧口径（原 docstring 原文）：「`PROFIT_PERCENT`(30) 属 P4
          （利润 = `price − tempAmount − refundFee`）——拒，但理由不同」。
        - **新口径**：P4 按**业务方裁定**做「客服销售提成 = 按利润百分比」，
          故 mode 30 现在**可算**；`commission_percent_of` 新增 keyword-only `mode=`
          （默认仍是 `ORDER_PERCENT` ⇒ 3 个既有调用点零改动）。
        - **依据**：P4 spec §4.4 + P4 计划 P4-5 + 编排方 Ruling L（接口裁定）。
        - ⚠️ 原口径里**仍然成立**的部分：**不传 `mode`** 时（打手路径）mode 30 档案**仍被拒**，
          只是理由从「属 P4、本阶段不支持」变成「档案 mode 与请求 mode 不匹配」。
          这条不变式由 `test_commission_profit.py` 的
          `test_profit_profile_rejects_default_order_request` 继续钉住。
        """
        user = self._user(commission_mode=PROFIT, commission_value=Decimal("30.00"))
        # 期望值写字面值，不引常量（E-0024 自指断言）
        self.assertEqual(
            commission_percent_of(user, mode=CommissionMode.PROFIT_PERCENT),
            Decimal("30"),
        )


class CommissionIsolationTests(TenantTestCase):
    """`AGENTS.md` §4：任何新增查询都要有跨租户回归。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="p3b_a", name="P3B 租户 A", domain="p3b-a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="p3b_b", name="P3B 租户 B", domain="p3b-b.test.local"
        )

    def test_profile_does_not_cross_tenants(self):
        """⚠️ **判据不能用主键**——本用例的第一版就栽在这里（记录在案）。

        第一版写 `StaffProfile.objects.filter(user_id=user_id).exists()`：两个租户的
        `auth_user` 主键序列**各自从 1 开始**，B 里那条断言命中的是 **B 自己的档案**，
        于是「A 的档案在 B 不可见」**恒假**（实测 `AssertionError: True is not false`）。
        这正是 P3a 独立整体评审在 Task 8 抓到的同一个形状——**换个模块照样会踩**，
        所以判据改成**租户内唯一、且本例两租户取值不同**的字段：用户名。
        """
        with tenant_context(self.tenant_a):
            user = User.objects.create_user(username="a_staff")
            StaffProfile.objects.create(
                user=user, commission_mode=PV, commission_value=Decimal("20.00")
            )
            self.assertEqual(commission_percent_of(user), Decimal("20.00"))   # 本租户读得到

        with tenant_context(self.tenant_b):
            # 非空哨兵：B 里也有自己的档案，否定断言才有意义
            b_user = User.objects.create_user(username="b_staff")
            StaffProfile.objects.create(
                user=b_user, commission_mode=PV, commission_value=Decimal("33.00")
            )
            self.assertTrue(StaffProfile.objects.filter(user=b_user).exists())
            # A 的档案不可见——按**用户名**判（不是主键）
            self.assertFalse(
                StaffProfile.objects.filter(user__username="a_staff").exists()
            )
            self.assertEqual(commission_percent_of(b_user), Decimal("33.00"))  # 自己的仍可读
