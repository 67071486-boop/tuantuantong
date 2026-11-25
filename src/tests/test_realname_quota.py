"""T5：实名认证配额的**唯一出口**（`apps/realname/quota.py`）的判据。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` **§6-7**（本节是全部判据的来源）
与 **§5 的 ⑤**（选包 + 锁两行）；任务书 T5 的 10 条。

## 本文件钉什么

| # | 用例 | 它怎么才会红 |
|---|---|---|
| 1 | `lock()` 成功 + `charge()` 各 +1 | 扣减没落库 / 落错行 / 快照取错（取在扣减之后） |
| 2 | `granted=0` ⇒ 1026，且库里的 `used` 不变 | 把 `used >= granted` 写成真值判断（`granted=0` 会被放行） |
| 3 | `granted=1, used=1` ⇒ 1026 | 边界写成 `>` 而不是 `>=` |
| 4 | **没有额度行** ⇒ 1026（⛔ 不是 `DoesNotExist`） | auto-create 一行 `granted=0`，或让异常漏成 500 |
| 5 | 全部 `total_quota == used_quota` ⇒ 1027 | 选包条件写成 `<=` / 漏了余量过滤 |
| 6 | 过期包被跳过、选 `id` 大的可用包 | 过期条件写反（`expires_at__lt=now`）或没有它 |
| 7 | 全过期 ⇒ 1027 | 同上 |
| 8 | `is_active=False` 被跳过 ⇒ 1027 | 漏了 `is_active` 过滤 |
| 9 | ⭐ **并发**：`granted=1` 两条独立连接 ⇒ 恰一个成功、一个 1026，终态 `used==1` | 扣减改成读-改-写（丢更新 ⇒ 两个都成功） |
| 10 | ⭐ **并发**：包余量恰好 1 ⇒ 恰一个成功扣减，另一个被**锁内复查**拦下 | 去掉 `lock()` 里的 `select_for_update()`，或去掉锁内复查 |
| 11 | ⭐ **结构网**：`RealNameGrant.used` / `RealNamePackage.used_quota` 的赋值只准在 `quota.py` | 任何别处（含 `.update(used=F("used")+1)`）多一处写入点 |

⚠️ **判据一律打在终态上**（库里的行），⛔ 不断言「函数被调用了」这类中间层
（`AGENTS.md` §5-E / E-0009：断言副作用要打在真实出口上）。

## ⚠️ 夹具为什么一律要「先删自己那一行」

`TransactionTestCase.flush` **只清 `public`**，而平台表（`platform_realnamegrant` /
`platform_realnamepackage`）作为 `SHARED_APPS` 的模型在**每个租户 schema 里还有一份影子表**
（本仓既有形态，实测：`SHARED_APPS` 的模型会在每个 schema 建表）。⇒ 上一条用例留在租户 schema 里的
那一行**不会**被 flush 掉，下一条用例的 `create()` 会撞唯一约束 `uniq_realname_grant_tenant`。
所以本文件每个用例开头都**显式** `delete()` 自己那一行（T2 在
`src/tests/test_realname_package_command.py` 里踩过同一个坑，见那里的注释）。
"""
import ast
import datetime as dt
import json
import threading
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import QuerySet
from django.test import TransactionTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model

from apps.realname import quota
from apps.platform.models import RealNameGrant, RealNamePackage, Tenant
from apps.realname.quota import LockedQuota, QuotaError, charge, lock

# ── 合成夹具（`AGENTS.md` §5-C / E-0077：⛔ 不用任何真值，形状贴近真实即可）──────────
#: 合成的密钥 Id / Key（**一眼假**，⛔ 不是任何真实凭据；`key_secret` 本文件不参与任何断言）。
FAKE_KEY_ID = "AKIDFAKE0000T5SYNTH"
FAKE_KEY_SECRET = "SYNTH-T5-0000-FAKE-SECRET"

#: 并发用例的租户 schema（与既有并发用例 `tests/test_realname_package_command.py` 同口径：
#: `django-tenants` **没有** `TenantTransactionTestCase`，夹具照 `TenantTestCase.setUpClass` 自己搭）。
CONCURRENT_SCHEMA = "test"
CONCURRENT_DOMAIN = "tenant.test.com"

#: 同步点的「一轮」时长（屏障超时）。
#: ⚠️ 它**同时是判据的一部分**：正确实现下后到的那条会被行锁挡掉**至少一轮**才到达同步点
#: （读数见 `_BarrierAtGrantRowLock.serialized`）。3 秒是给「两条真连接 + 一次租户查询」留的余量，
#: 同时不让「锁写坏了」变成无限挂起；⛔ 不要再往下调（越低越容易在慢机器上假红）。
BARRIER_TIMEOUT = 3.0

#: 线程 join 的上限。⚠️ **必须比 `BARRIER_TIMEOUT` 宽松得多**（实测踩到）：正确实现下
#: 两条线程各自要等满**一轮**同步点超时（一条先到等一轮、另一条被行锁挡住之后再到、再等一轮），
#: 所以「join 超时 = 屏障超时」会把正常路径误判成挂起。这里给 4 轮 + 10 秒余量。
THREAD_JOIN_TIMEOUT = 4 * BARRIER_TIMEOUT + 10

#: 错误码的**字面值**（⛔ 不写 `ErrorCode.REALNAME_…`：那样「码值被改掉」在用例里看不见，
#: 同 `tests/test_account_admin_guard.py:51` 的口径）。
QUOTA_EXHAUSTED = 1026
NO_PACKAGE = 1027

#: 固定的「现在」（本模块自己不取时钟，`now` 由调用方给 ⇒ 用例钉一个确定值）。
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)


def make_package(**overrides) -> RealNamePackage:
    """建一个合成密钥包（默认 `total_quota=10`、`used_quota=0`、启用、不过期）。

    ⚠️ `key_id` 有**唯一约束**：同一个用例里建
    **多个**包时必须逐个显式给不同的 `key_id`（同形假号 `AKIDFAKE000N…`），
    ⛔ 靠默认值连建两个会直接撞约束。
    """
    fields = {
        "key_id": FAKE_KEY_ID,
        "key_secret": FAKE_KEY_SECRET,
        "total_quota": 10,
        "used_quota": 0,
        "is_active": True,
        "expires_at": None,
    }
    fields.update(overrides)
    return RealNamePackage.objects.create(**fields)


class RealNameQuotaTests(TransactionTestCase):
    """用例 1–8：`lock()` / `charge()` 的正面与全部边界（同一个租户上下文里跑）。

    ⚠️ 用 `TransactionTestCase`（⛔ 不是 `TenantTestCase`）：`TenantTestCase` 继承
    `django.test.TestCase`，把用例包在**外层事务**里 ⇒ 「闸门拒绝时库里没变」这条读数是
    「同一事务里回滚」的产物，判别力弱；而且本文件第 10、11 组要的是真并发，夹具形态统一更省事。
    代价（如实登记）：每条用例后 Django 会 flush 一次，比 `TestCase` 慢。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name=CONCURRENT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain=CONCURRENT_DOMAIN
        )
        cls.domain.save()
        if CONCURRENT_DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [CONCURRENT_DOMAIN]
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if CONCURRENT_DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(CONCURRENT_DOMAIN)
        super().tearDownClass()

    def setUp(self):
        # 前提自证：确实跑在**无外层事务**的形态上（否则并发态用例没有判别力）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)
        # ⚠️ flush 只清 `public`，租户 schema 里的影子表要自己清（见模块 docstring）。
        RealNameGrant.objects.filter(tenant=self.tenant).delete()
        RealNamePackage.objects.all().delete()

    def test_00_error_codes_are_the_ones_this_module_raises(self):
        """前提自证：本文件钉的两个码**就是** `error_codes.py` 里的实名那一段（⛔ 不用真值猜）。

        ⚠️ 这条**不替代**下面那两条（它们钉的是「什么情况下回哪个码」），只保证码值本身没被人
        顺手挪走——⛔ 也**不**断言错误码表的总条数（本仓没有网数它，如实登记见 T5 报告）。
        """
        from apps.core.error_codes import ErrorCode

        self.assertEqual(ErrorCode.REALNAME_QUOTA_EXHAUSTED, QUOTA_EXHAUSTED)
        self.assertEqual(ErrorCode.REALNAME_NO_PACKAGE, NO_PACKAGE)
        # ⚠️ 1025（功能未启用）**不在本模块**：它是「本租户开关」那一档，归核验视图。
        self.assertEqual(ErrorCode.REALNAME_DISABLED, 1025)
        self.assertEqual(quota.REALNAME_QUOTA_EXHAUSTED, QUOTA_EXHAUSTED)
        self.assertEqual(quota.REALNAME_NO_PACKAGE, NO_PACKAGE)

    # ── ① 正常：锁两层、扣两次 ────────────────────────────────────────
    def test_01_lock_and_charge_increment_both_layers(self):
        """`granted=5, used=0` + 包 `total=10, used=0` ⇒ 锁成功，扣减后两层各 1。

        顺带钉快照：`LockedQuota.used` / `used_quota` 必须是**扣减前**的 `0`
        （扣减会把属性就地变成 `F()` 表达式 ⇒ 取在之后的话这里会读到表达式对象）。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        pkg = make_package(total_quota=10, used_quota=0)

        with transaction.atomic():
            locked = lock(now=NOW)
            self.assertIsInstance(locked, LockedQuota)
            self.assertEqual(locked.package.pk, pkg.pk)
            self.assertEqual(locked.used, 0)
            self.assertEqual(locked.used_quota, 0)
            charge(locked)

        # ⭐ 终态一律重新查库（⛔ 不拿内存对象：那会掩盖「写没写进库」）。
        grant_row = RealNameGrant.objects.get(tenant=self.tenant)
        package_row = RealNamePackage.objects.get(pk=pkg.pk)
        self.assertEqual(grant_row.used, 1)
        self.assertEqual(package_row.used_quota, 1)
        # 快照**没有**被扣减改掉（它是独立的 int，不是那一行的引用）。
        self.assertEqual(locked.used, 0)
        self.assertEqual(locked.used_quota, 0)

    # ── ② 余量 0 不放行（承重：真值判断会放行它）─────────────────────
    def test_02_zero_grant_is_rejected_and_used_is_untouched(self):
        """`granted=0` ⇒ 1026，**且库里的 `used` 没变**。

        ⚠️ 这条是 `if grant.used >= grant.granted:` 那句**是不是整数比较**的答案：
        改成 `if grant.granted and grant.used >= grant.granted:`（真值判断）⇒ 本用例红
        （`granted=0` 被当成「没限制」放行）。`AGENTS.md` §5-E / E-0021。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=0, used=0)
        make_package(total_quota=10, used_quota=0)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)

        self.assertEqual(ctx.exception.code, QUOTA_EXHAUSTED)
        # 终态：额度行没被碰过、包也没被扣（⛔ 不许「先扣了再说」）。
        self.assertEqual(RealNameGrant.objects.get(tenant=self.tenant).used, 0)
        self.assertEqual(RealNamePackage.objects.get(key_id=FAKE_KEY_ID).used_quota, 0)

    # ── ③ granted 用完 ───────────────────────────────────────────────
    def test_03_exhausted_grant_is_rejected(self):
        """`granted=1, used=1` ⇒ 1026（边界是 `>=`，⛔ 不是 `>`）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=1, used=1)
        make_package(total_quota=10, used_quota=0)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)

        self.assertEqual(ctx.exception.code, QUOTA_EXHAUSTED)
        self.assertEqual(RealNameGrant.objects.get(tenant=self.tenant).used, 1)

    # ── ④ 额度行不存在 ⇒ 1026（⛔ 不是 DoesNotExist、⛔ 不 auto-create）────
    def test_04_missing_grant_row_is_quota_error_not_does_not_exist(self):
        """一行都没有 ⇒ 1026，且跑完之后**还是一行都没有**。

        ⚠️ 两件事一起钉：① 异常类型必须是 `QuotaError`（`DoesNotExist` 漏出去 = 500，那是「响亮
        但说错话」）；② ⛔ **不许 auto-create** 一个 `granted=0` 的行——那会让「没买过」与
        「买过用完」在数据上混淆（spec §6-7 ①）。
        """
        make_package(total_quota=10, used_quota=0)
        self.assertEqual(RealNameGrant.objects.filter(tenant=self.tenant).count(), 0)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)
        self.assertEqual(ctx.exception.code, QUOTA_EXHAUSTED)

        # ⛔ 一行都没被建出来（auto-create 会让这条红）。
        self.assertEqual(RealNameGrant.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(RealNamePackage.objects.get(key_id=FAKE_KEY_ID).used_quota, 0)

    # ── ⑤ 无可用包（全耗尽）⇒ 1027 ───────────────────────────────────
    def test_05_all_packages_exhausted_is_no_package_error(self):
        """`total_quota == used_quota` 的包一个都不选 ⇒ 1027（⛔ 不是 1026：两档必须分得开）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        make_package(total_quota=3, used_quota=3)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)

        self.assertEqual(ctx.exception.code, NO_PACKAGE)
        # 终态：那一包没有被再扣一次（读-改-写 / 漏过滤会让它变成 4）。
        self.assertEqual(RealNamePackage.objects.get(key_id=FAKE_KEY_ID).used_quota, 3)
        self.assertEqual(RealNameGrant.objects.get(tenant=self.tenant).used, 0)
        # 两档的文案必须能区分（否则管理员会去改一个**没错**的地方）。
        self.assertNotIn("本租户", ctx.exception.message)

    # ── ⑥ 过期包被跳过，选 id 大的可用包 ─────────────────────────────
    def test_06_expired_package_is_skipped_for_the_available_one(self):
        """两个包：`id` 小的已过期、`id` 大的可用 ⇒ 选中**大的**那个（⛔ 不是 1027）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        expired = make_package(
            key_id="AKIDFAKE0001T5SYNTH",
            total_quota=10, used_quota=0, expires_at=NOW - dt.timedelta(days=1)
        )
        usable = make_package(
            key_id="AKIDFAKE0002T5SYNTH",
            total_quota=10, used_quota=0, expires_at=NOW + dt.timedelta(days=1)
        )
        # 前提自证：`id` 顺序确实是「小的过期、大的可用」（⛔ 不靠拉取顺序假设）。
        self.assertLess(expired.pk, usable.pk)

        with transaction.atomic():
            locked = lock(now=NOW)
            self.assertEqual(locked.package.pk, usable.pk)
            charge(locked)

        self.assertEqual(RealNamePackage.objects.get(pk=usable.pk).used_quota, 1)
        # ⛔ 过期那一包一个数都不许动。
        self.assertEqual(RealNamePackage.objects.get(pk=expired.pk).used_quota, 0)

    def test_05b_exhausted_package_is_skipped_for_the_next_available_one(self):
        """判据 9（spec §2.4 的防回归钉子）：两包并存、包 1 用尽 ⇒ `lock()` 选中包 2。

        ⚠️ 这是「第一个密钥用完后自动切换」（业务方原话）的**后端判据**：
        钉 `quota.py::lock` 选包过滤里 `used_quota__lt=F("total_quota")` 这一条——
        把它删掉，本用例会选中已用尽的包 1（快照 `used_quota` 相等）或锁内复查炸 1027，必红。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        first = make_package(
            key_id="AKIDFAKE0001T5SYNTH", total_quota=3, used_quota=3
        )   # 已用尽（合法存态，不是停用）
        second = make_package(key_id="AKIDFAKE0002T5SYNTH", total_quota=10, used_quota=0)
        # 前提自证：`id` 顺序确实是「小的已用尽、大的有货」（⛔ 不靠返回顺序假设）。
        self.assertLess(first.pk, second.pk)

        with transaction.atomic():
            locked = lock(now=NOW)
            self.assertEqual(locked.package.pk, second.pk)
            charge(locked)

        # 终态：包 2 被扣了 1 次；已用尽的包 1 一个数都不许动（读-改-写会把它再扣一次）。
        self.assertEqual(RealNamePackage.objects.get(pk=second.pk).used_quota, 1)
        self.assertEqual(RealNamePackage.objects.get(pk=first.pk).used_quota, 3)

    def test_06b_never_expiring_package_is_usable(self):
        """`expires_at is None`（买了没有有效期的包）⇒ 可用（条件是「空 **或** 未到点」）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        pkg = make_package(total_quota=10, used_quota=0, expires_at=None)

        with transaction.atomic():
            locked = lock(now=NOW)
            self.assertEqual(locked.package.pk, pkg.pk)
            charge(locked)

        self.assertEqual(RealNamePackage.objects.get(pk=pkg.pk).used_quota, 1)

    # ── ⑦ 全过期 ⇒ 1027 ─────────────────────────────────────────────
    def test_07_all_packages_expired_is_no_package_error(self):
        """全部过期 ⇒ 1027（⛔ 不是拿过期包凑一次）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        make_package(
            key_id="AKIDFAKE0001T5SYNTH",
            total_quota=10, used_quota=0, expires_at=NOW - dt.timedelta(days=1),
        )
        make_package(
            key_id="AKIDFAKE0002T5SYNTH",
            total_quota=10, used_quota=0, expires_at=NOW - dt.timedelta(days=2),
        )

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)

        self.assertEqual(ctx.exception.code, NO_PACKAGE)
        self.assertEqual(
            RealNamePackage.objects.filter(used_quota=0).count(), 2, "过期包不该被扣"
        )

    def test_07b_package_expiring_exactly_now_is_not_used(self):
        """边界：`expires_at == now` 算**过期**（条件是 `expires_at > now`，⛔ 不是 `>=`）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0, expires_at=NOW)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)
        self.assertEqual(ctx.exception.code, NO_PACKAGE)

    # ── ⑧ 停用的包被跳过 ─────────────────────────────────────────────
    def test_08_inactive_package_is_skipped(self):
        """`is_active=False` 的包即使余量充足也不许被选中 ⇒ 1027。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0, is_active=False)

        with self.assertRaises(QuotaError) as ctx:
            with transaction.atomic():
                lock(now=NOW)

        self.assertEqual(ctx.exception.code, NO_PACKAGE)
        self.assertEqual(RealNamePackage.objects.get(key_id=FAKE_KEY_ID).used_quota, 0)


class RealNameQuotaConcurrencyTests(TransactionTestCase):
    """⭐ 用例 9 / 10：两条**独立连接**同时扣减（承重判据，守 `select_for_update()` 与「包不被用穿」）。

    ⚠️ 如实登记：**锁内那次复查**（`package.used_quota >= package.total_quota`）本类**判不到**
    （删掉它两条用例照样绿）——理由与代价写在 `test_10` 的 docstring 里，⛔ 不许读成「已覆盖」。

    ⚠️ **为什么必须 `TransactionTestCase`**：`TestCase`（含 `TenantTestCase`）把用例包在**外层事务**
    里 ⇒ 两条连接互相看不到未提交的行、也不会真的发生行级阻塞 ⇒ 并发用例**没有判别力**
    （同款先例与长解释：`src/tests/test_openapi_rate_limit.py:135` 那段 docstring、
    `src/tests/test_realname_package_command.py` 的 T2 并发用例）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name=CONCURRENT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain=CONCURRENT_DOMAIN
        )
        cls.domain.save()
        if CONCURRENT_DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [CONCURRENT_DOMAIN]
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if CONCURRENT_DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(CONCURRENT_DOMAIN)
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)
        # ⚠️ flush 只清 `public`，租户 schema 里的影子表要自己清（见模块 docstring）——
        # 否则上一条用例留下的额度行会让本条的 `create()` 撞 `uniq_realname_grant_tenant`。
        RealNameGrant.objects.filter(tenant=self.tenant).delete()
        RealNamePackage.objects.all().delete()

    def tearDown(self):
        connection.set_tenant(self.tenant)

    # ── ⑨ 两条并发扣减 ⇒ 恰一个成功 ──────────────────────────────────
    def test_09_two_concurrent_charges_admit_exactly_one(self):
        """`granted=1`：两条独立连接各跑一次 `lock()+charge()` ⇒ **恰一个成功、一个 1026**，
        终态 `used == 1`（⛔ 不是 2）。

        ⭐ **这条是「`lock()` 真的锁住了额度行」的答案**。判别力怎么来（⚠️ 不是靠"跑得快"）：
        用例把**额度行那次 `SELECT … FOR UPDATE` 取完数之后**那一刻包一层同步点
        （`_BarrierAtGrantRowLock`），
        让两条连接的**判定**真的叠在一起：

        - 实现正确 ⇒ 后到的连接**先被行锁挡掉一整轮**才到同步点，先到的那条在同步点上等满一轮
          **没等到第二条**（读数 `arrived == 2 且 timed_out == 2`）⇒ 它的判定与后到的那条**不重叠**
          ⇒ 后到的那条读到已提交的 `used=1` ⇒ 报 1026；
        - 去掉 `select_for_update()` ⇒ 两条**同时**到达同步点、屏障正常通过（`arrived == 2 且
          timed_out == 0`）⇒ 双双读到 `used=0` ⇒ 双双扣减 ⇒ 本用例红
          （实测原始输出：`AssertionError: Lists differ: ['ok', 'ok'] != ['ok', 'quota_error']`，
          且终态 `used == 2`）。

        ⚠️ 如实登记：**「恰一个成功」这句本身在多数跑法里是时序敏感的**——没有同步点时两条连接常常
        自动错开（先到的那条已经提交），那时去掉行锁也会绿（T2 在
        `src/tests/test_realname_package_command.py` 的
        `test_concurrent_grants_do_not_deadlock_or_hang` docstring 里点过同款）。同步点就是为把这条
        缝打开而加的，它在**每一次**跑法里都生效（`gate.serialized()` 断言就是它的见证）。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=1, used=0)
        pkg = make_package(total_quota=10, used_quota=0)

        with _BarrierAtGrantRowLock(timeout=BARRIER_TIMEOUT) as gate:
            results = run_two_workers(lambda: quota.lock(now=NOW), charge)

        # ⓪ 同步点自证（⭐ 这条让本用例的判别力**不依赖跑到多快**）：
        #    实现正确 ⇒ 后到的连接卡在行锁上、到不了同步点 ⇒ 只有 1 条到达、且它等到了超时；
        #    去掉行锁 ⇒ 两条**同时**对齐（`arrived == 2`）：那时它们双双读到 `used=0` ⇒ 下面 ① 红。
        self.assertTrue(
            gate.serialized(),
            f"行锁没把两条连接的判定串起来（到达 {gate.arrived} 条、超时 {gate.timed_out} 条）"
            "——去掉 `select_for_update()` 就是这个形状",
        )
        # ① 恰好一个成功、一个「余量不足」——⛔ 不许两个都成功（超扣），也⛔ 不许两个都失败。
        self.assertEqual(
            sorted(r[0] for r in results.values()), ["ok", "quota_error"], results
        )
        self.assertEqual(
            [r[1] for r in results.values() if r[0] == "quota_error"],
            [QUOTA_EXHAUSTED],
            results,
        )
        # ② 终态：恰好扣一次（读-改-写 ⇒ 2；一次都没扣 ⇒ 0）。
        self.assertEqual(RealNameGrant.objects.get(tenant=self.tenant).used, 1)
        self.assertEqual(RealNamePackage.objects.get(pk=pkg.pk).used_quota, 1)

    # ── ⑩ 包余量恰好 1 + 两条并发 ⇒ ⛔ 那一个包不被用穿 ────────────────
    def test_10_package_with_single_unit_is_not_overcharged_by_two_workers(self):
        """包余量**恰好 1**、额度充足、两条并发 ⇒ 那个包**恰好被扣 1 次**（⛔ 不是 2），
        另一条走备选包或报 1027——两条都不许把它用穿。

        ⭐ **这条守的是「`used_quota <= total_quota` 恒成立」这个不变量**（spec §4.1：
        包是我们真金白银买来的次数，用穿 = 白花一笔）。

        ⚠️⚠️ **如实登记（T5 实测，两处都要读）**：

        1. **`lock()` 里锁内那次复查**（`package.used_quota >= package.total_quota`）
           **没有判据守着它**：把它删掉，本用例**照样绿**。第一版把这条写成「去掉复查 ⇒ 后一条
           照样 `charge()` ⇒ 终态 2」——**那是错的**（实测读数：删掉复查后两条都成功，`single`
           仍然是 1，后一条选了备选包）。机理：`select_for_update()` 在 READ COMMITTED 下
           **锁住之后会重新求值 WHERE**（PostgreSQL 语义），而后到的连接在上一步**重新过滤**
           （`used_quota < total_quota`）时就已经跳过那个用尽的包了 ⇒ 「过了过滤、锁上时却已用尽」
           那条缝在**单库**形态里到不了。
        2. 因此本用例**只钉不变量**（那个包恰好被扣 1 次），⛔ **不**钉「另一条一定报 1027」
           （它选备选包也是对的，钉死它反而会变成一条只在某种时序下才绿的用例）。
           ⚠️ 代价：锁内复查是**结构化要求**（spec §6-7 ④ 明文要求）而非本用例覆盖的行为；
           要真的覆盖它，需要造出「过滤之后、锁之前包被别人用尽」的交错——那在单库 READ COMMITTED
           下不可达（见上）。⛔ 不许把它读成「复查已覆盖」。
        """
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=0)
        single = make_package(key_id="AKIDFAKE0001T5SYNTH", total_quota=1, used_quota=0)
        make_package(key_id="AKIDFAKE0002T5SYNTH", total_quota=5, used_quota=0)  # 备选包（它被用上是**允许**的）

        with _BarrierAtGrantRowLock(timeout=BARRIER_TIMEOUT) as gate:
            results = run_two_workers(lambda: quota.lock(now=NOW), charge)

        # ⓪ 同步点自证：额度行的行锁必须把两条连接串起来（同用例 9 的理由）。
        self.assertTrue(
            gate.serialized(),
            f"额度行没把两条连接串起来（到达 {gate.arrived} 条、超时 {gate.timed_out} 条）",
        )
        # ① 两条都必须跑完（⛔ 不许挂起 / 不许以 error 收场）。
        self.assertEqual(sorted(r[0] for r in results.values()), ["ok", "ok"], results)
        # ② ⭐ 不变量：那一个包**恰好**被扣 1 次（用穿 ⇒ 2）。
        self.assertEqual(RealNamePackage.objects.get(pk=single.pk).used_quota, 1)
        # ③ 不变量推广到全表：⛔ 任何一个包都不许 `used_quota > total_quota`。
        for pkg in RealNamePackage.objects.all():
            self.assertLessEqual(
                pkg.used_quota, pkg.total_quota, f"包 {pkg.pk} 被用穿了：{pkg.used_quota}/{pkg.total_quota}"
            )


class _BarrierAtGrantRowLock:
    """上下文管理器：在 `RealNameGrant` 的 `SELECT … FOR UPDATE` **之后**与另一条线程对齐。

    ⚠️ **为什么打在这个位置**（⛔ 不是随便挑一个同步点）：`select_for_update()` 的「读」与「锁」
    是**同一个动作**；只有在这里对齐，才能把「两条连接双双读到扣减前的 `used`」这件事**逼出来**。
    放在别处（例如 `lock()` 返回之后）的话，先到的那条早就提交了，后到的读到的是新值，
    用例就退化成「两条串行调用」，判别力归零。

    ⚠️ **为什么超时是「判据」而不是「容错」**：实现正确时，后到的连接**阻塞在行锁上**、
    根本到不了这个同步点 ⇒ 屏障只等到 1 个人、它等到超时（`arrived == 1 且 timed_out == 1`）；
    去掉行锁时两条会**双双对齐**（`arrived == 2 且 timed_out == 0`）。
    ⇒ 用例断言「只有一条到达、且它等到了超时」，于是一条本来**只在跑得巧时才会红**的并发用例
    变成确定判据。⚠️ 光看 `threading.Barrier.broken` **不够**：屏障一旦被打破，
    **后到**的那条再 `wait()` 会立刻抛同款异常（实测：`broken` 为真而两条其实都到过），
    所以读数取的是**计数**。

    ⚠️ **本包装器不改变任何判据**：它只加一个同步点，⛔ 不吞异常、⛔ 不改返回值、⛔ 不改行数。
    """

    def __init__(self, *, timeout: float):
        self.timeout = timeout
        self.barrier = threading.Barrier(2, timeout=timeout)
        self._original = QuerySet._fetch_all
        self.arrived = 0
        self.timed_out = 0
        self._counter_lock = threading.Lock()

    @classmethod
    def _is_gated(cls, queryset) -> bool:
        """这一次取数是不是「带着 `FOR UPDATE` 去取**额度行**」（按模型 + `_for_write` 认）。

        ⚠️ **为什么打 `_fetch_all`，⛔ 不是打 `select_for_update()`**（T5 实测踩到）：
        `select_for_update()` 只是**造**一个 queryset（不碰数据库），真正的 `SELECT … FOR UPDATE`
        发生在求值那一刻（`get()` → `_fetch_all`）。打在造 queryset 那一步 ⇒ 同步点在**锁之前**，
        两条连接会双双对齐（实测 `arrived == 2`），用例退化成「两条串行调用」、判别力归零。
        """
        return (
            getattr(queryset, "model", None) is RealNameGrant
            and bool(getattr(queryset, "_for_write", False))
        )

    def _wrapper(self, queryset, *args, **kwargs):
        locked = self._original(queryset, *args, **kwargs)
        if self._is_gated(queryset):
            with self._counter_lock:
                self.arrived += 1
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                # 等到超时（= 另一条没能同时到达）⇒ 这正是实现正确时的形状。⛔ 不报错。
                # ⚠️ 计数必须在这里做：屏障一旦被打破，**后到**的那条调用 `wait()` 会立刻抛同款异常，
                #    光看 `barrier.broken` 分不清「只来了一个」与「两个都来了但错开了」（实测踩到）。
                with self._counter_lock:
                    self.timed_out += 1
        return locked

    def __enter__(self):
        # ⚠️ **必须塞「普通函数」，⛔ 不能塞绑定方法**（T5 实测踩到）：
        # `mock.patch.object(QuerySet, "select_for_update", self._wrapper)` 里的 `self._wrapper`
        # 是**绑定方法**，它进类字典后**不会再被描述符协议重新绑定** ⇒ 实例调用时
        # `queryset` 少一个实参 ⇒ `TypeError: _wrapper() missing 1 required positional
        # argument: 'queryset'`（且它被 `run_two_workers` 记成 "error"，看起来像被测实现坏了）。
        # 写成 `functools.partial(self._wrapper, ...)` 之外最省事的形态：一个模块级闭包函数，
        # 它的第一个参数会被正常绑定成实例。
        def _patched(queryset, *args, **kwargs):
            return self._wrapper(queryset, *args, **kwargs)

        self._patch = mock.patch.object(QuerySet, "_fetch_all", _patched)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False

    def serialized(self) -> bool:
        """同步点的读数：**两条的判定到底有没有被行锁串起来**。

        | 读数 | 含义 |
        |---|---|
        | `arrived == 2 且 timed_out == 2`（⭐ 实现正确） | 后到的那条**先被行锁挡了一整轮**（≥ 超时）才到达同步点；先到的那条在同步点上等满一轮、**没能等到第二条**（= 它的判定与后到的那条**不重叠**）⇒ 两条的判定被串起来了 |
        | `arrived == 2 且 timed_out == 0`（去掉行锁） | 两条**同时**到达、屏障正常通过 ⇒ 它们双双读到扣减前的余量 ⇒ 会双双扣减 ⇒ 断言红 |

        ⚠️ **为什么不能只看「到达 1 条」**：先到的那条在同步点上**等满一整轮超时**就会往下走
        （`Barrier` 的超时是「打破屏障」，不是「永远等」）——那时后到的那条还没到（它正卡在行锁上），
        所以 `arrived` 最终仍是 2。实测踩到过这个读数（T5 报告里有原始输出）。
        """
        return self.arrived == 2 and self.timed_out == 2


def run_two_workers(call_lock, call_charge) -> dict:
    """两条**独立连接**（各自线程）各跑一次「`lock()` + `charge()`」，返回每条的结果。

    返回值：`{"alpha": ("ok"|"quota_error"|"error", <码或异常原文>), "beta": …}`。

    ⚠️ 本文件里的「同步点」= `RealNameGrant` 那次 `SELECT … FOR UPDATE` **取完数之后**那一刻
    （见 `_BarrierAtGrantRowLock._is_gated`）。

    ⚠️ 每条线程的 `lock()`+`charge()` 必须包在一个 `transaction.atomic()` 里（spec §6-7 的调用
    形态：锁与扣减同事务）；连接是 thread-local ⇒ 每条线程先 `close_old_connections()` 再
    `set_tenant(...)`。
    """
    results: dict[str, tuple[str, object]] = {}

    def worker(key: str) -> None:
        try:
            close_old_connections()
            # 租户上下文只从连接态来（`AGENTS.md` §4：⛔ 不接受任何请求参数传租户）。
            connection.set_tenant(Tenant.objects.get(schema_name=CONCURRENT_SCHEMA))
            with transaction.atomic():
                locked = call_lock()
                call_charge(locked)
            results[key] = ("ok", None)
        except QuotaError as exc:
            results[key] = ("quota_error", exc.code)
        except Exception as exc:  # noqa: BLE001 —— 结果要回主线程断言（⛔ 不许静默）
            results[key] = ("error", f"{type(exc).__name__}: {exc}")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(key,)) for key in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=THREAD_JOIN_TIMEOUT)

    # ⛔ 挂起 / 死锁一律红（不许被当成「读数不对」）。
    assert not any(thread.is_alive() for thread in threads), "并发用例挂住了（死锁或锁等待无上限）"
    assert set(results) == {"alpha", "beta"}, f"有线程没跑完：{results}"
    return results


# ── ⑪ 结构网：这两个字段的赋值只准出现在 quota.py ─────────────────────────
#: 受钉的两个字段的**字段名**（赋值侧按属性链的最右一段认，见 `_rightmost_attr`）。
WATCHED_FIELD_NAMES = frozenset({"used", "used_quota"})
#: 唯一合法写入点（相对 `src/` 的路径）。
QUOTA_MODULE = "apps/realname/quota.py"
#: 结构网的扫描根（本文件在 `src/tests/` 下 ⇒ 上两级是 `src/`）。
SRC_ROOT = Path(__file__).resolve().parents[1]
#: ⛔ 不扫的两类：测试自己、以及 Django 自动生成的迁移。
SKIPPED_DIRS = frozenset({"tests", "migrations"})


def _rightmost_attr(target) -> tuple[str, str] | None:
    """把 `a.b.c` 形态的属性链记成 `(对象名, 最右那个属性名)`（⛔ 不递归进下标 / 函数调用）。

    ⚠️ **为什么要认属性链**：本仓真实写法是 `locked.grant.used = F("used") + 1`
    （`quota.py`），只有一层时会被记成 `("locked", "used")` ⇒ 与「写的是哪个模型」对不上。
    实测（T5 第一版）：只认单层属性 ⇒ 扫描器在**真实写入点**上一个都没命中、网恒真。
    """
    if not isinstance(target, ast.Attribute):
        return None
    rightmost = target.attr  # 最外层那个属性名（`a.b.c` ⇒ `c`）
    current = target.value
    while isinstance(current, ast.Attribute):
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    return (current.id, rightmost)

def _assigned_field(node) -> tuple[str, str] | None:
    """`x.used = …` / `x.grant.used = …` 形态 ⇒ 返回 `(对象名, 字段名)`（只认受钉的两个字段）。

    ⚠️ 按 AST 里**写出来的名字**认（⛔ 不做类型推断）：本仓的写法就是
    `grant.used = …` / `locked.package.used_quota = …`。⚠️ 代价如实登记：换个别名
    （`g = grant; g.used = …`）能绕过这条网——但它拦住的正是**这个仓里实际会出现的写法**
    （以及 `.update(used=F("used") + 1)` 那类，见下）。
    """
    for target in node.targets:
        field = _rightmost_attr(target)
        if field and field[1] in WATCHED_FIELD_NAMES:
            return field
    return None


def _call_writes_watched_field(node: ast.Call) -> tuple[str, str] | None:
    """`…update(used=…)` / `…update(**{"used": …})` 形态 ⇒ 返回 `("update", 字段名)`。

    ⚠️ `F("used") + 1` 这类**读**不算写入点（它只是表达式的一部分）；
    真正落库的是 `save()` / `update()` / `create()`。
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "update"):
        return None
    for keyword in node.keywords:
        if keyword.arg in {"used", "used_quota"}:
            return ("update", keyword.arg)
        if keyword.arg is None and isinstance(keyword.value, ast.Dict):
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and key.value in {"used", "used_quota"}:
                    return ("update", key.value)
    return None


def collect_quota_write_sites(root: Path) -> dict[str, set[tuple[str, str]]]:
    """AST 扫 `root/<每一个包>/**/*.py`（跳过 `tests/` 与 `migrations/`），收「改这两个字段」的位置。

    返回 `{相对 root 的路径: {(对象名|'update', 字段名), …}}`——**只回命中项**，
    调用方自己比集合（⛔ 不在这里断言，那会让「哪一处违规」看不出来）。

    ⚠️ **只下潜到「本仓库自己的包」这一层**（`apps/` / `config/` / …），⛔ 不递归进
    `.venv` / `node_modules` 这类非仓库目录：实测（T5 第一版）`.venv` 是**软链**、`Path.rglob`
    会跟进去，Django 自己的 `django/contrib/messages/storage/base.py` 里就有一句
    `self.used = True` ⇒ 那条网会以「命中了 Django 源码」的形式假红（与本仓的实现无关）。
    """
    hits: dict[str, set[tuple[str, str]]] = {}
    candidates = []
    for base in sorted(root.iterdir()):
        if not base.is_dir() or base.name in SKIPPED_DIRS or base.name.startswith("."):
            continue
        candidates.extend(sorted(base.rglob("*.py")))
    for path in candidates:
        relative = path.relative_to(root)
        if SKIPPED_DIRS & set(relative.parts[:-1]):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[tuple[str, str]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                field = _assigned_field(node)
                if field:
                    found.add(field)
            elif isinstance(node, ast.Call):
                field = _call_writes_watched_field(node)
                if field:
                    found.add(field)
        if found:
            hits[str(relative)] = found
    return hits


class RealNameQuotaWriteSiteNetTests(TransactionTestCase):
    """⑪ 结构网：⛔ 别处不许直接改 `RealNameGrant.used` / `RealNamePackage.used_quota`。

    spec §4.2 的「代价」那一节把这条网的**理由**写死了：租户角色对 `public` 表**有写权限**
    （`django-tenants` 的默认形态）⇒ 能执行 SQL 的东西都能改平台表，所以「唯一出口」只能靠
    **纪律 + 结构网**，而**不能**靠数据库权限。

    ⚠️ **本网自己必须能红**：自指的结构断言（断言「它自己刚算出来的集合」）常常**恒真**
    （`AGENTS.md` §5-G / E-0065）⇒ 本类里有 `test_11b`：往一个**临时目录**里放一份
    「别处改了 `used`」的样例，断言收集器**报得出来**（那一半扫描逻辑与生产那一半是同一份代码）。
    """

    def test_11_only_quota_module_may_write_the_two_quota_fields(self):
        """全仓扫一遍：命中集合必须**恰好**是 `{apps/realname/quota.py}`。"""
        hits = collect_quota_write_sites(SRC_ROOT)

        self.assertEqual(
            set(hits),
            {QUOTA_MODULE},
            f"这两个字段只准在 {QUOTA_MODULE} 里改，实测命中：{json.dumps({k: sorted(map(str, v)) for k, v in hits.items()}, ensure_ascii=False)}",
        )
        # ⚠️ 前提自证：`quota.py` 里**确实**有命中——否则上面那条在「扫描器什么都没找到」时
        # 也会绿（那一版网是恒真的，等于没钉）。
        self.assertEqual(
            hits[QUOTA_MODULE],
            {("locked", "used"), ("locked", "used_quota")},
            "扫描器没认出 quota.py 里的两处写入 ⇒ 这条网自己坏了（不是实现坏了）",
        )

    def test_11b_the_scanner_reports_a_write_outside_quota_module(self):
        """⭐ 弄红自证：把「别处改 `RealNameGrant.used`」放进一个临时目录 ⇒ 收集器必须报出来。

        ⚠️ 这里扫的是**临时目录**（不是仓库），因为「在仓库里临时放一个违规文件再删掉」
        会在测试中途改仓库、并在失败时留下脏文件（`AGENTS.md` §5-G）。
        临时目录里同时放一份**合法**的 `quota.py`（当对照），判据是「只报违规那一份」。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legit = root / "apps" / "realname"
            legit.mkdir(parents=True)
            (legit / "quota.py").write_text(
                "def charge(grant):\n    grant.used = 1\n", encoding="utf-8"
            )
            offender = root / "apps" / "realname" / "views.py"
            offender.write_text(
                "def oops(grant):\n    grant.used = grant.used + 1\n    grant.save()\n",
                encoding="utf-8",
            )

            hits = collect_quota_write_sites(root)

        self.assertEqual(
            set(hits),
            {"apps/realname/quota.py", "apps/realname/views.py"},
            f"扫描器没报出别处的写入点 ⇒ 这条网是恒真的：{hits}",
        )
        self.assertEqual(hits["apps/realname/views.py"], {("grant", "used")})

    def test_11c_the_scanner_reports_an_update_call_outside_quota_module(self):
        """⭐ 同一条网的第二半：`.update(used=F("used") + 1)` 也算**写入点**（⛔ 不是只有赋值）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = root / "apps" / "business"
            pkg.mkdir(parents=True)
            (pkg / "refunds.py").write_text(
                'from django.db.models import F\n'
                'def oops(model):\n'
                '    model.objects.filter(pk=1).update(used_quota=F("used_quota") + 1)\n',
                encoding="utf-8",
            )

            hits = collect_quota_write_sites(root)

        self.assertEqual(
            hits,
            {"apps/business/refunds.py": {("update", "used_quota")}},
            f"`.update(...)` 形态没被认出来 ⇒ 网漏了一半：{hits}",
        )
