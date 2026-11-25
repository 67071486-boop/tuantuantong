"""交易平台对接设置两张表的**模型级**判据（计划 T1，2026-10-26）。

> 计划：`docs/plans/2026-10-26-platform-integration-settings.md` T1。
> 证据件：`src/frontend/public/replica/system-tripartiteplatformsetting.html`。
> 本模块只钉**表本身**：平台枚举 / 默认值 / 每平台至多一个默认 / 单行入口 / 凭据不进 `__str__`。
> ⛔ 不碰接口（T2 的序列化与 `write_only`）、不碰页面（T3/T4）。

判据口径（`AGENTS.md` §5-E / §5-G）：
- 断言打在**数据库行**上（`IntegrityError` + 字段值），⛔ 不打 logger、⛔ 不看接口文案；
- 默认值一律钉**字面量**（`0` / `True` / `False`），⛔ 不写 `== PlatformPushSettings.YesNo.NO`
  ——那是自指断言：常量被改坏时两边一起变、永远不会红（E-0024）；
- 夹具一律**合成值**（账号 `USR_TEST_…`、密钥 `secret-…`）：证据件里的真实 AppID / 支付密码
  不进测试（`AGENTS.md` §4 凭据红线 / §5-C 数据纪律）。

已知空档（本任务不覆盖，留给后续任务）：
- `app_secret` / `pay_password_md5` 的「只写不读」是**接口层**义务（序列化器 `write_only` +
  `*_set` 布尔）；模型层只负责「`__str__` 里不带它们」。
- 「同平台同账号能不能重复建」证据件没给（`:13` 只说允许同平台多个账号）⇒ 不加约束、
  也不写用例；⛔ 不要凭「应该唯一」补一条。
"""
import re
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django_tenants.test.cases import TenantTestCase

from apps.business.models import PlatformIntegration, PlatformPushSettings

#: 平台 id → 文案。**逐字从 `src/frontend/src/config/platforms.ts:16-30` 抄来**，
#: 不是从被测模型派生（否则枚举写错时两边一起错、用例照样绿）。
PLATFORM_LABELS = {
    8: "全能代",
    10: "代练通",
    30: "代练妈妈",
    50: "咕噜代练",
    60: "易代练",
    70: "代练宝",
    80: "代练丸子",
    90: "趣代练",
    91: "代练多",
    92: "代游通",
    93: "天玩代练",
    94: "通天代练",
    95: "享代练",
}

#: 证据件里固定出现的平台账号形状（`USR…`）；本测试只用合成串。
ACCOUNT_A = "USR_TEST_0001"
ACCOUNT_B = "USR_TEST_0002"
APP_SECRET = "secret-test-do-not-print"
PAY_PASSWORD_MD5 = "0123456789abcdef0123456789abcdef"


class PlatformIntegrationModelTests(TenantTestCase):
    """`PlatformIntegration`（多行 / 每平台至多一个默认）。"""

    def test_platform_choices_match_platforms_ts(self):
        """与**前端字典原文**对拍：解析 `platforms.ts` 的 `id` 集合，必须与模型 choices 恰好相等。

        ⚠️ 这条不是「再抄一遍」。平台字典在本仓有**三份**副本（前端 TS / 模型 choices /
        本文件的 `PLATFORM_LABELS` 字面量），任何一份漂了都要红。解析用正则、⛔ 不引依赖
        （`platforms.ts` 里那 13 行形如 `{ id: 8, label: '全能代' }`）。
        """
        ts_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "frontend"
            / "src"
            / "config"
            / "platforms.ts"
        )
        self.assertTrue(ts_path.is_file(), f"找不到前端平台字典：{ts_path}")
        text = ts_path.read_text(encoding="utf-8")

        ts_ids = {int(m.group(1)) for m in re.finditer(r"\{\s*id:\s*(\d+)\s*,", text)}
        model_ids = {value for value, _label in PlatformIntegration.Platform.choices}
        self.assertTrue(ts_ids, "正则没解析到任何 id（自证失败，不产出结论）")
        self.assertEqual(ts_ids, model_ids)

        ts_pairs = {
            (int(m.group(1)), m.group(2))
            for m in re.finditer(
                r"\{\s*id:\s*(\d+)\s*,\s*label:\s*'([^']*)'\s*\}", text
            )
        }
        self.assertEqual(ts_pairs, set(PLATFORM_LABELS.items()))
        self.assertEqual(PlatformIntegration.Platform.DAILIANTONG, 10)

    def test_column_set_is_exact(self):
        """列集合**恰好**是预期的那 16 列：新增/改名/漏删都会红。

        ⚠️ 凭据类列名**只能**是这两个：`app_secret` / `pay_password_md5`。
        将来若有人再加一个 `token` / `password` 列，这条会红——凭据面不许悄悄扩大。
        """
        expected = {
            "id",
            "platform",
            "account",
            "app_id",
            "app_secret",
            "pay_password_md5",
            "is_enabled",
            "is_default",
            "sync_accept",
            "sms_notify",
            "include_push",
            "contact_qq",
            "contact_phone",
            "remark",
            "created_at",
            "updated_at",
        }
        actual = {field.name for field in PlatformIntegration._meta.get_fields()}
        self.assertEqual(actual, expected)
        credential_like = {
            name
            for name in actual
            if any(token in name for token in ("secret", "password", "token"))
        }
        self.assertEqual(credential_like, {"app_secret", "pay_password_md5"})

    def test_platform_has_no_default_and_is_indexed(self):
        """`platform` 没有默认值（保存前必须选平台），且带索引（列表按它筛 / 排）。"""
        field = PlatformIntegration._meta.get_field("platform")
        self.assertFalse(field.has_default())
        self.assertIs(field.db_index, True)

    def test_account_is_required_and_rejects_blank(self):
        """`account` 必填：字段 `blank=False`，空串过 `full_clean()` 必须报 `account` 错。"""
        field = PlatformIntegration._meta.get_field("account")
        self.assertIs(field.blank, False)
        self.assertFalse(field.has_default())

        row = PlatformIntegration(platform=PlatformIntegration.Platform.DAILIANTONG)
        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()
        self.assertIn("account", ctx.exception.message_dict)

    def test_lengths_follow_evidence(self):
        """长度是两个有出处的值：`app_secret` = 证据件 `maxlength=1000`；
        `pay_password_md5` = MD5 十六进制 32 位。"""
        self.assertEqual(
            PlatformIntegration._meta.get_field("app_secret").max_length, 1000
        )
        self.assertEqual(
            PlatformIntegration._meta.get_field("pay_password_md5").max_length, 32
        )

    def test_defaults_are_enabled_reset(self):
        """新建行的默认值：只 `is_enabled=True`，其余开关全 False、凭据列空串。

        ⚠️ 布尔用 `assertIs` 而不是 `assertFalse`：后者对 `0` / `""` / `None` 也通过，
        「列是布尔且默认 False」这条会被「列其实是别的东西」骗过（`test_account_model.py`
        的同一条理由）。
        """
        row = PlatformIntegration.objects.create(
            platform=PlatformIntegration.Platform.DAILIANTONG, account=ACCOUNT_A
        )
        self.assertIs(row.is_enabled, True)
        self.assertIs(row.is_default, False)
        self.assertIs(row.sync_accept, False)
        self.assertIs(row.sms_notify, False)
        self.assertIs(row.include_push, False)
        self.assertEqual(row.app_id, "")
        self.assertEqual(row.app_secret, "")
        self.assertEqual(row.pay_password_md5, "")
        self.assertEqual(row.contact_qq, "")
        self.assertEqual(row.contact_phone, "")
        self.assertEqual(row.remark, "")

    def test_second_default_on_same_platform_is_rejected_by_db(self):
        """同平台第二个 `is_default=True` 撞**数据库**约束（不是模型 `save` 里的软校验）。

        判据：`IntegrityError` 由 `create()` 直接抛出；事后库里仍只有 1 行默认。
        """
        PlatformIntegration.objects.create(
            platform=PlatformIntegration.Platform.DAILIANTONG,
            account=ACCOUNT_A,
            is_default=True,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlatformIntegration.objects.create(
                platform=PlatformIntegration.Platform.DAILIANTONG,
                account=ACCOUNT_B,
                is_default=True,
            )
        self.assertEqual(
            PlatformIntegration.objects.filter(
                platform=PlatformIntegration.Platform.DAILIANTONG,
                is_default=True,
            ).count(),
            1,
        )

    def test_each_platform_can_have_its_own_default(self):
        """默认唯一是**按平台**的：不同平台各有一行默认，互不冲突。"""
        PlatformIntegration.objects.create(
            platform=PlatformIntegration.Platform.DAILIANTONG,
            account=ACCOUNT_A,
            is_default=True,
        )
        PlatformIntegration.objects.create(
            platform=PlatformIntegration.Platform.DAILIANMAMA,
            account=ACCOUNT_B,
            is_default=True,
        )
        self.assertEqual(
            PlatformIntegration.objects.filter(is_default=True).count(), 2
        )
        # 再钉一次「是哪两行」：只有计数相等时，「两行其实同平台」也会通过。
        self.assertEqual(
            set(
                PlatformIntegration.objects.filter(is_default=True).values_list(
                    "platform", flat=True
                )
            ),
            {10, 30},
        )

    def test_non_default_rows_may_share_a_platform(self):
        """反向用例：约束是**部分**唯一 ⇒ 同平台可以有任意多行非默认账号。

        没有这一条，把约束误写成「`platform` 单列唯一」也照样能通过上面那条正例
        （照 `tests/test_jidanpu_upstream_models.py` 模块头的同一理由）。
        """
        for idx in range(3):
            PlatformIntegration.objects.create(
                platform=PlatformIntegration.Platform.DAILIANTONG,
                account=f"{ACCOUNT_A}-{idx}",
            )
        self.assertEqual(
            PlatformIntegration.objects.filter(
                platform=PlatformIntegration.Platform.DAILIANTONG
            ).count(),
            3,
        )

    def test_str_does_not_expose_credentials(self):
        """`__str__` 含平台与账号（给人认），但 ⛔ 不含 AppSecret / 支付密码摘要。"""
        row = PlatformIntegration.objects.create(
            platform=PlatformIntegration.Platform.DAILIANTONG,
            account=ACCOUNT_A,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
        )
        text = str(row)
        self.assertIn("代练通", text)
        self.assertIn(ACCOUNT_A, text)
        self.assertNotIn(APP_SECRET, text)
        self.assertNotIn(PAY_PASSWORD_MD5, text)


class PlatformPushSettingsModelTests(TenantTestCase):
    """`PlatformPushSettings`（单行 / 9 组证据件默认值 / round-trip）。"""

    def test_defaults_match_evidence_checked_radios(self):
        """新行的 9 组默认值 = 证据件里带 `checked` 的那一支（html:149-247）。

        ⚠️ 证据件里 `0` 不总是「关」：`SK156` / `SK157` 的 `0` 是「否」，
        `SK159` 的 `0` 是「检测」，`SK155` / `SK158` 的 `0` 是「不限制 / 不发送」
        ——⛔ 不要按直觉统一取反。
        """
        settings = PlatformPushSettings.get_solo()
        self.assertIs(settings.quick_push_enabled, True)  # SK132 默认 Y 开
        self.assertEqual(settings.legacy_simple_mode, 0)  # SK156 0=否
        self.assertEqual(settings.check_game_region, 0)  # SK157 0=否
        self.assertIs(settings.forbid_push_when_double_gold_zero, False)  # SK107 默认 N
        self.assertEqual(settings.duplicate_push_check, 0)  # SK159 0=检测
        self.assertIs(settings.select_jidanpu_center_by_default, False)  # SK106 默认 N
        self.assertIs(settings.auto_zero_refund_on_conflict, True)  # SK145 默认 Y=0撤
        self.assertEqual(settings.block_push_completed_orders, 0)  # SK155 0=不限制
        self.assertEqual(settings.auto_send_group_qr_after_accept, 0)  # SK158 0=不发送
        # ⭐ 一键抛单 B4：门槛/模板三列的默认值。
        self.assertEqual(settings.default_power_level, 1)  # 完单数默认 ≥1（业务方选定）
        self.assertIsNone(settings.default_limit_accept)  # null = 不设置（⛔ 不是 0）
        self.assertEqual(settings.require_template, "")  # 空 = 用 Actor 占位文案

    def test_get_solo_creates_then_returns_the_same_row(self):
        """首次调用自动建一行（`pk=1`），再次调用是同一条，不会出第二行。"""
        first = PlatformPushSettings.get_solo()
        self.assertEqual(first.pk, 1)
        self.assertEqual(PlatformPushSettings.objects.count(), 1)

        again = PlatformPushSettings.get_solo()
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(PlatformPushSettings.objects.count(), 1)

    def test_round_trip_keeps_both_value_encodings(self):
        """9 组改到另一支后能原样读回——`Y/N` 组存布尔、`0/1` 组存原值 `1`。

        ⚠️ 这条同时钉「`0/1` 的两组没有被压成布尔」：`1` 读回来必须是整数 `1`
        （不是 `True`），否则 T4 按证据件文案回填时会错支。
        """
        settings = PlatformPushSettings.get_solo()
        settings.quick_push_enabled = False
        settings.legacy_simple_mode = 1
        settings.check_game_region = 1
        settings.forbid_push_when_double_gold_zero = True
        settings.duplicate_push_check = 1
        settings.select_jidanpu_center_by_default = True
        settings.auto_zero_refund_on_conflict = False
        settings.block_push_completed_orders = 1
        settings.auto_send_group_qr_after_accept = 1
        # ⭐ B4：三列也走一遍读写（`null` 与空串必须能原样回来）。
        settings.default_power_level = 7
        settings.default_limit_accept = 15
        settings.require_template = "合成模板：接单后联系客服"
        settings.save()

        reloaded = PlatformPushSettings.get_solo()
        self.assertIs(reloaded.quick_push_enabled, False)
        self.assertEqual(reloaded.legacy_simple_mode, 1)
        self.assertEqual(reloaded.check_game_region, 1)
        self.assertIs(reloaded.forbid_push_when_double_gold_zero, True)
        self.assertEqual(reloaded.duplicate_push_check, 1)
        self.assertIs(reloaded.select_jidanpu_center_by_default, True)
        self.assertIs(reloaded.auto_zero_refund_on_conflict, False)
        self.assertEqual(reloaded.block_push_completed_orders, 1)
        self.assertEqual(reloaded.auto_send_group_qr_after_accept, 1)
        self.assertEqual(reloaded.default_power_level, 7)
        self.assertEqual(reloaded.default_limit_accept, 15)
        self.assertEqual(reloaded.require_template, "合成模板：接单后联系客服")
        # ⚠️ 用 `type(...) is int` 而不是 `assertIsInstance(..., int)`：后者对 `True`
        # 也通过（`bool` 是 `int` 的子类），钉不住「这一组没有被压成布尔」。
        self.assertIs(type(reloaded.legacy_simple_mode), int)
        self.assertIs(type(reloaded.duplicate_push_check), int)

    def test_column_set_has_no_credential_columns(self):
        """列集合**恰好**是那 15 列，且**不含任何**凭据类列名（secret / password / token）。

        ⚠️ 这张表不该有凭据；将来若有人往里塞 `wechat_secret` / `api_token`，这条会红。
        （原来那条 `assertNotIn("secret", str(settings))` 是恒真的：本表今天没有凭据列、
        测试也没设值——只有把字面量 "secret" 硬写进 `__str__` 才会红，钉不住任何东西。）
        """
        expected = {
            "id",
            "quick_push_enabled",
            "legacy_simple_mode",
            "check_game_region",
            "forbid_push_when_double_gold_zero",
            "duplicate_push_check",
            "select_jidanpu_center_by_default",
            "auto_zero_refund_on_conflict",
            "block_push_completed_orders",
            "auto_send_group_qr_after_accept",
            # ⭐ B4：门槛默认值两份 + 代练要求模板。
            "default_power_level",
            "default_limit_accept",
            "require_template",
            "created_at",
            "updated_at",
        }
        actual = {field.name for field in PlatformPushSettings._meta.get_fields()}
        self.assertEqual(actual, expected)
        self.assertEqual(
            {
                name
                for name in actual
                if any(token in name for token in ("secret", "password", "token"))
            },
            set(),
        )
