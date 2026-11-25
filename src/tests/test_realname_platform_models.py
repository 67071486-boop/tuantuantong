"""平台侧实名认证模型的判据：`RealNamePackage` / `RealNameGrant`（`apps/platform/models.py`）。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` §4.1；计划 T1。

## 这份文件钉什么

1. **字段能落能读**（两张表各建一行、读回一致）；
2. ⭐ **`RealNameGrant.tenant` 唯一**——扣减侧走 `select_for_update().get(tenant=…)`，
   不唯一就是 `MultipleObjectsReturned`（500），而不是「余量不足」业务码（spec §6-7 ①）；
3. ⭐ **`key_secret` 不进 `__str__`**——证明用的是**库里那一行的真实值**（⛔ 不是拿常量去比常量，
   那种自指断言在被断言的实现改坏时不会红，见 `AGENTS.md` §5-E / E-0024）。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：密钥用「一眼假」的形状，⛔ 不是任何真实密钥。

## ⚠️ 本文件原有 6 条 admin 用例已在 T2 删掉（不是漏写）

T1 曾建 `src/apps/platform/admin.py`（`RealNamePackageForm` + `register_all`），本文件随之有
`RealNamePackageAdminFormTests`（3 条）与 `RealNameAdminWiringTests`（3 条）钉它。
**T2 把那两份用例与 `admin.py` 一起删了**，理由（两条出路都不通，2026-10-10 实测）：

1. 本仓**没装 `django.contrib.admin`**（不在 `INSTALLED_APPS`），`config/settings.py` 明写不许装回来
   ⇒ 那份 admin 在本仓**永远长不出界面**；
2. 平台控制台已独立成 `../ttt-platform`（D-0196），而那个项目是**完全独立的 Django 工程**
   （app 是 `console`，自己的 `admin.py`），**不 import 本仓 `apps.*`**
   ⇒ `register_all(site)` 的第二个消费者也不存在。

⇒ 录入 / 拨额度的入口改为**管理命令** `manage.py realname_package`，
判据在 `src/tests/test_realname_package_command.py`（「`key_secret` 不回显」这条口径**没有丢**：
那里钉的是「`list` / `quota` 的输出里不含库里那个密钥原文」）。

## 为什么用 `TenantTestCase` 而不是光秃秃的 `TestCase`

这两张表在 public（平台）schema，而 `TenantTestCase.setUpClass` 会先 `sync_shared()`
把 public 面的迁移跑一遍，再把连接切到测试租户 schema。`RealNameGrant.tenant` 的外键指向
`platform.Tenant`——测试租户那一行就是现成的 FK 目标，不需要自己造租户。
⚠️ 代价如实登记：`setUpClass` 里的 `Tenant.save()` 会**真建一个测试 schema**并跑租户面迁移
⇒ 本文件比纯模型用例慢（本仓既有账号 / 实名相关的用例都是这个口径）。
"""
from __future__ import annotations

from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.platform.models import RealNameGrant, RealNamePackage

#: 合成的密钥 Key（形状贴近真实：两段带连字符的长串），⛔ 不是任何真实凭据。
FAKE_KEY_ID = "AKIDFAKE0000SYNTH"
FAKE_KEY_SECRET = "SYNTH-0000-FAKE-SECRET-1111-2222-3333"


class RealNamePlatformModelTests(TenantTestCase):
    """字段落库 / 读回，以及 `key_secret` 不外泄。"""

    def test_package_fields_round_trip(self):
        expires_at = timezone.now() + timedelta(days=365)
        pkg = RealNamePackage.objects.create(
            key_id=FAKE_KEY_ID,
            key_secret=FAKE_KEY_SECRET,
            remark="2026-10 购入 1000 次包（合成备注）",
            total_quota=1000,
            expires_at=expires_at,
        )

        # 读回走**另一条查询**（`refresh_from_db`）：只断言内存里的对象等于自己 = 自指。
        pkg.refresh_from_db()
        self.assertEqual(pkg.key_id, FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)
        self.assertEqual(pkg.remark, "2026-10 购入 1000 次包（合成备注）")
        self.assertEqual(pkg.total_quota, 1000)
        self.assertEqual(pkg.used_quota, 0)  # 默认值
        self.assertTrue(pkg.is_active)  # 默认值
        self.assertIsNotNone(pkg.created_at)
        self.assertIsNotNone(pkg.updated_at)
        # ⚠️ 不写 `assertEqual(pkg.expires_at, expires_at)`：`DateTimeField` 存的是微秒精度，
        # 拿内存对象与读回值直接比有精度陷阱 ⇒ 只钉「确实存在」与「不是 None」。
        self.assertIsNotNone(pkg.expires_at)

    def test_package_expires_at_may_be_null(self):
        # 空 = 不过期，是合法且有意义的边界（选包按「空 或 未到点」放行，spec §6-7 ③）。
        pkg = RealNamePackage.objects.create(
            key_id="AKIDFAKE1111SYNTH", key_secret=FAKE_KEY_SECRET, total_quota=0
        )
        pkg.refresh_from_db()
        self.assertIsNone(pkg.expires_at)
        self.assertEqual(pkg.remark, "")  # 默认值

    def test_grant_fields_round_trip(self):
        grant = RealNameGrant.objects.create(tenant=self.tenant, granted=50)

        grant.refresh_from_db()
        self.assertEqual(grant.tenant_id, self.tenant.pk)
        self.assertEqual(grant.granted, 50)
        self.assertEqual(grant.used, 0)  # 默认值
        self.assertIsNotNone(grant.created_at)
        # 反向关系按 `related_name="realname_grants"` 取得到（扣减侧只按 tenant 取，这里是接线判据）。
        self.assertEqual(list(self.tenant.realname_grants.all()), [grant])

    def test_str_does_not_leak_key_secret(self):
        """⭐ `__str__` 里不许出现密钥 Key 的**原始文本**。

        ⚠️ 断言钉的是**库里那一行的真实值**（`pkg.key_secret`，刚 `refresh_from_db` 过），
        ⛔ 不是模块里的常量——否则把 `key_secret` 悄悄换进 `__str__` 时这条仍然绿（自指）。
        """
        pkg = RealNamePackage.objects.create(
            key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=10
        )
        pkg.refresh_from_db()
        # 先证「这个值确实在库里」（否则下面那条 `assertNotIn` 会因为是空串而恒真）。
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)

        text = str(pkg)
        self.assertNotIn(pkg.key_secret, text)
        # 连它的一半也不许露：`__str__` 只回备注 + 密钥 Id 的前 4 位 + `***`。
        self.assertNotIn(pkg.key_secret[:16], text)
        self.assertNotIn(pkg.key_secret[16:], text)
        self.assertIn("（无备注）" if not pkg.remark else pkg.remark, text)
        self.assertIn(pkg.key_id[:4], text)

    def test_str_of_package_with_remark_shows_remark(self):
        pkg = RealNamePackage.objects.create(
            key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=10, remark="合成备注甲"
        )
        self.assertEqual(str(pkg), "合成备注甲#" + pkg.key_id[:4] + "***")


class RealNameGrantUniquenessTests(TenantTestCase):
    """⭐ 一个租户只能有一行额度（`uniq_realname_grant_tenant`）。"""

    def test_second_grant_for_same_tenant_raises_integrity_error(self):
        RealNameGrant.objects.create(tenant=self.tenant, granted=50)

        # `transaction.atomic()` 包住：IntegrityError 之后本用例的连接不会被标成
        # 「需要回滚」而让后续查询报 `TransactionManagementError`。
        with self.assertRaises(IntegrityError), transaction.atomic():
            RealNameGrant.objects.create(tenant=self.tenant, granted=10)

        # 终态：仍然只有那一行，且额度没被改（证明失败的那次插入没有留下半行）。
        self.assertEqual(RealNameGrant.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(
            RealNameGrant.objects.get(tenant=self.tenant).granted, 50
        )

    def test_select_for_update_get_returns_exactly_one_row(self):
        """扣减侧真正会调的那一句：唯一之后 `get()` 不会 `MultipleObjectsReturned`。

        ⚠️ 这是**承重判据**：`select_for_update()` 必须在事务里用（PostgreSQL 会报
        `select_for_update cannot be used outside of a transaction`）。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=7)
        with transaction.atomic():
            grant = RealNameGrant.objects.select_for_update().get(tenant=self.tenant)
        self.assertEqual(grant.granted, 7)


class RealNamePlatformModelMetaTests(TestCase):
    """纯元数据判据（不碰租户 schema ⇒ 用最轻的 `TestCase`）。"""

    def test_constraint_name_is_stable(self):
        """约束名是**定死的**（spec §4.1 / 计划 T1）：改名即等于扣减侧换了契约。

        ⚠️ 这条**不查库**、只读模型的 `_meta`：`TenantTestCase` 已经建了表，这里要验的是
        「模型声明里那个名字」，两者是不同的东西（库里的名字由迁移决定，T2 的活）。
        """
        names = [c.name for c in RealNameGrant._meta.constraints]
        self.assertIn("uniq_realname_grant_tenant", names)

    def test_ordering_is_by_id(self):
        for model in (RealNamePackage, RealNameGrant):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.ordering, ["id"])

    def test_verbose_names_are_chinese(self):
        self.assertEqual(RealNamePackage._meta.verbose_name, "实名密钥包")
        self.assertEqual(RealNameGrant._meta.verbose_name, "实名额度")

    def test_tables_live_in_public_schema(self):
        """两张表的默认 schema 是 public（平台面），⛔ 不是租户 schema。"""
        for model in (RealNamePackage, RealNameGrant):
            with self.subTest(model=model.__name__):
                self.assertEqual(model._meta.db_table.split("_")[0], "platform")

    def test_key_secret_field_is_not_nullable(self):
        """密钥必须落库（B2：密钥不存 `.env`，只存这张表）⇒ 它不能是可空的。"""
        field = RealNamePackage._meta.get_field("key_secret")
        self.assertFalse(field.null)
        self.assertEqual(field.max_length, 128)


class ConnectionSanityTests(TenantTestCase):
    """对照：证明上面那些用例跑的是**真库**（不是被 mock 掉的层）。

    ⚠️ 这条是给「全绿」上保险的**对照用例**：如果连接是假的 / 表是假的，
    上面的 `create()` 不会抛 `IntegrityError`，本条也会跟着红。
    """

    def test_tables_actually_exist_in_the_database(self):
        with connection.cursor() as cur:
            for table in ("platform_realnamepackage", "platform_realnamegrant"):
                cur.execute("SELECT to_regclass(%s)", [table])
                self.assertIsNotNone(cur.fetchone()[0], f"表 {table} 不存在")
