"""账号 `flag` 读取点的单元用例（`apps/accounts/account_flags.py`）。

⚠️ 这些用例**不经 HTTP**：`flag` 的失败形态是字段脱敏，不是 403（spec §4.2）；
HTTP 层的脱敏矩阵在 `tests/test_rbac_batch2_system_user.py`（**本批 Task 2 新建该文件、
Task 3 追加脱敏矩阵**）。
"""
from django.contrib.auth.models import Group, User
from django_tenants.test.cases import TenantTestCase

from apps.accounts.account_flags import (
    ACCOUNT_FLAG_CODES,
    commission_params_visible,
    flag_enabled,
    id_card_visible,
)
from apps.business.roles import TENANT_ADMIN
from apps.rbac.models import Role
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client


class AccountFlagReaderTests(TenantTestCase):
    def setUp(self):
        make_logged_in_client(username="flag-holder", groups=("客服主管",))
        self.user = User.objects.get(username="flag-holder")

    def test_flags_default_to_absent_which_means_no_restriction(self):
        """⚠️ 本批最容易被误读的一条：**没有**这个 flag ⇒ **不施加**限制（D3 / D-0053）。"""
        for code in ACCOUNT_FLAG_CODES:
            self.assertFalse(flag_enabled(self.user, code), code)
        self.assertTrue(id_card_visible(self.user))
        self.assertTrue(commission_params_visible(self.user))

    def test_granting_one_restrict_flag_turns_only_that_capability_off(self):
        grant_nodes(self.user, "system_user.restrict_view_id_card")
        self.assertTrue(flag_enabled(self.user, "system_user.restrict_view_id_card"))
        self.assertFalse(id_card_visible(self.user))
        # 只挂一条不影响另一条（两条互不蕴含——防止"按模块批发"式的实现）
        self.assertTrue(commission_params_visible(self.user))

    def test_unregistered_code_raises_at_the_call_site(self):
        """拼错的 code 必须在**声明处**炸，而不是安静地恒 False。"""
        with self.assertRaises(ValueError):
            flag_enabled(self.user, "system_user.restrict_view_id_cards")

    def test_mobile_app_flag_is_not_registered_because_it_has_no_consumer(self):
        """⚠️ `restrict_mobile_app_access` **刻意不在**常量表里（§4 偏离 7）。"""
        self.assertNotIn("system_user.restrict_mobile_app_access", ACCOUNT_FLAG_CODES)
        with self.assertRaises(ValueError):
            flag_enabled(self.user, "system_user.restrict_mobile_app_access")

    def test_flag_codes_are_exactly_the_two_literal_codes(self):
        """⚠️ **值钉子**：期望值写字面量，不用被测常量自己（E-0024：自指断言在常量被改坏时不会红）。

        本模块只承认两条限制型 flag；少一条/多一条/拼错一个字母/顺序变了都要红。
        `restrict_mobile_app_access` **刻意不在**（没有消费方，见模块 docstring）。
        """
        self.assertEqual(
            ACCOUNT_FLAG_CODES,
            ("system_user.restrict_view_id_card", "system_user.restrict_view_commission_params"),
        )

    def test_the_admin_position_is_never_restricted_by_a_restrict_flag(self):
        """限制型 flag 的**管理员旁路**（D-0054 同口径；2026-09-23 判据已换成职位）。

        ⚠️ **判别力来自"节点确实挂着、却仍不施加限制"**：给管理员**所在的 Role** 挂上 flag，
        先断言**Role 上真的有这条节点**，再断言 `flag_enabled` 为 `False`。
        ⚠️ **前置条件不能用 `has_node_now(admin, code)`**：它对管理员**恒短路为真**
        （`apps/rbac/permissions.py`），拿它当前置等于**没有前置**——那条断言无论
        节点挂没挂上都会通过（E-0024 族的"断言没有判别力"）。所以前置读**库里 Role 的授权**。
        ⚠️ **2026-09-23 口径变更**：原文用「超管 + 客服主管组」构造旁路；现在旁路认的是
        「系统管理员」职位，夹具随之改成持该职位（否则限制会被正常施加 ⇒ 本条红）。
        """
        admin = User.objects.create_superuser(username="flag-super")
        group, _ = Group.objects.get_or_create(name=TENANT_ADMIN)
        admin.groups.add(group)
        grant_nodes(admin, "system_user.restrict_view_id_card")
        self.assertIn(
            "system_user.restrict_view_id_card",
            set(Role.objects.get(name=TENANT_ADMIN).permissions.values_list("code", flat=True)),
            "前置条件：节点确实挂上了（否则本条会因别的原因而绿）",
        )
        self.assertFalse(flag_enabled(admin, "system_user.restrict_view_id_card"))
        self.assertTrue(id_card_visible(admin))

    def test_unregistered_code_still_raises_for_an_admin(self):
        """⚠️ **守卫顺序**：先判 code 合法性、再判管理员（2026-09-23 起判据是职位）。

        判别力：把这两条 `if` 调换顺序后，**其余用例全都仍会绿**（两个条件不在同一条用例里交叉）
        ⇒ 没有本条就抓不到"管理员调拼错的 code 被静默当 fail-open"。期望值写字面量。
        """
        admin = User.objects.create_superuser(username="flag-super-bad-code")
        self.assertTrue(getattr(admin, "is_superuser", False), "前置条件：这确实是 superuser")
        with self.assertRaises(ValueError):
            flag_enabled(admin, "system_user.restrict_view_id_card_typo")
