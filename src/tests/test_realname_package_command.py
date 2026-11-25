"""`manage.py realname_package` 的判据（平台侧密钥包录入 / 拨额度 / 看余量）。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` §2-B2/B3、§4.1、§9.2；
本文件是 **T2** 的交付（spec §9.2 原写走 Django admin，本仓装不了 admin ⇒ 改管理命令，
理由见下）。

## ⚠️ 为什么是管理命令而不是 Django admin（T2 的改准）

spec §9.2 把「`RealNamePackage` 录入 / `RealNameGrant` 拨额度」的入口定成 Django admin。
T1 据此建了 `src/apps/platform/admin.py`，**T2 实测后删掉了它**，两条出路都不通：

1. 本仓 `django.contrib.admin` 不在 `INSTALLED_APPS`，`config/settings.py` 明写不许装回来
   ⇒ 那份 admin 在本仓永远长不出界面（`@admin.register(...)` 一 import 就抛
   `LookupError: No installed app with label 'admin'`）；
2. 平台控制台已独立成 `../ttt-platform`（D-0196），而那个项目是**完全独立的 Django 工程**
   （app 叫 `console`，有自己的 `admin.py`），**不 import 本仓 `apps.*`**
   ⇒ T1 那条「在平台项目里调 `register_all`」的第二出口也不存在。

⇒ 入口改为 `manage.py realname_package`，本文件钉它的行为。

## 本文件钉什么（对应任务书的 5 条 + 并发那一条）

1. `add` 之后 `list` 查得到、余量正确，且 **输出里不含 `key_secret` 原文**；
2. `grant` 两次 ⇒ `granted` **累加**（⛔ 不是覆盖）；
3. `grant` 给不存在的租户 ⇒ 报错 + 退出码非 0，且**不建行**
   （⚠️ 这条打在 `CommandError` 上；「真命令行退出码是 1」另有一次实测，读数在 T2 报告里，
   `CommandError` 正是 Django 把 `manage.py` 变成退出码 1 的那条路径）；
4. `quota` 输出余量 = `granted - used`；
5. `--total-quota 0` ⇒ 报错、不建行；
6. ⭐ **并发**：两条真实连接各 `grant` 一次 ⇒ `granted` 恰好 +2。**这一条才是**
   「`F("granted") + times` 有没有被判据守住」的答案——实测把 `F()` 换回读-改-写，
   它稳定红（读数 1 而不是 2，T2 变异验证 3/3 次一致）。
   ⚠️ 另一条 `test_concurrent_grants_do_not_deadlock_or_hang` 守的是「不死锁 / 不挂起」，
   ⛔ **不守** `F()`（删掉 `select_for_update()` 它也照样绿，理由写在那条的 docstring 里）。

## ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）

密钥用「一眼假」的形状，⛔ 不是任何真实凭据。判据钉的是**库里那一行的值**
（`RealNamePackage.objects.get(...).key_secret`），⛔ 不是拿模块常量比常量
（自指断言在被断言的实现改坏时不会红，见 E-0024）。

## ⚠️ 第 6 条（并发）为什么单独用 `TransactionTestCase`

`TestCase`（含 `TenantTestCase`）把每条用例包在**外层事务**里 ⇒ 两个线程**无法真的**
发生行级阻塞，用它写的并发用例**没有判别力**（测的是「同一事务里跑两次」）。
同款先例与长解释：`tests/test_account_concurrency.py` 的模块 docstring。
"""
import threading
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model

from apps.platform.models import RealNameGrant, RealNamePackage

#: 合成的密钥 Id / Key（形状贴近真实），⛔ 不是任何真实凭据。
FAKE_KEY_ID = "AKIDFAKE0000SYNTH"
FAKE_KEY_SECRET = "SYNTH-0000-FAKE-SECRET-1111-2222-3333"

#: 并发用例的租户 schema 名与域名（与既有并发用例同口径）。
CONCURRENT_SCHEMA = "test"
CONCURRENT_DOMAIN = "tenant.test.com"

#: 两条线程必须在这个时间内跑完。正常运行是毫秒级；15 秒是给「两条连接 + 一次行锁等待」
#: 留余量，同时不让「锁写坏了」变成无限挂起。
BARRIER_TIMEOUT = 15.0


def run_command(*args, **options):
    """跑一次命令，返回 `(stdout, stderr)` 原文。

    ⚠️ 命令输出是**判据的载体**（「⛔ 不打印 key_secret」这条只能打在输出上），
    所以这里必须把 stdout / stderr 都收回来，⛔ 不能让它们漏到测试日志里。
    """
    out, err = StringIO(), StringIO()
    call_command("realname_package", *args, stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


class RealNamePackageCommandTests(TenantTestCase):
    """`add` / `list` / `grant` / `quota` 的正面与边界判据。

    ⚠️ `TenantTestCase` 把连接切到测试租户 schema，而本命令**自己**显式切回 `public`
    （见命令模块 docstring：两张表以 public 那一份为唯一出处）。断言一律重新查库。
    """

    # ── ① add → list，且输出不含密钥原文 ─────────────────────────────────
    def test_add_then_list_shows_package_and_quota_without_key_secret(self):
        run_command(
            "add",
            key_id=FAKE_KEY_ID,
            key_secret=FAKE_KEY_SECRET,
            total_quota=1000,
            remark="2026-10 购入 1000 次包（合成备注）",
            expires_at="2027-10-10",
        )

        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        # 前提自证：这个密钥**确实在库里**——否则下面那条 `assertNotIn` 会因为
        # （比如）密钥根本没落库而恒真，等于没钉（`AGENTS.md` §5-G / E-0024）。
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)
        self.assertEqual(pkg.total_quota, 1000)
        self.assertEqual(pkg.used_quota, 0)
        self.assertIsNotNone(pkg.expires_at)

        out, err = run_command("list")
        # ① 查得到（掩码形状：前 4 位 + ***）。
        self.assertIn(f"{FAKE_KEY_ID[:4]}***", out)
        # ② 余量正确（`used_quota` 默认 0 ⇒ 余量 = 总额）。
        self.assertIn("余量 1000/1000", out)
        self.assertIn("已消耗 0", out)
        self.assertIn("2026-10 购入 1000 次包（合成备注）", out)
        # ③ ⭐ 输出里**不含密钥原文**——钉的是库里那一行的值，不是模块常量。
        self.assertNotIn(pkg.key_secret, out)
        self.assertNotIn(pkg.key_secret, err)
        # 连它的一半也不许露（掩码只打在 key_id 上，密钥整条不出现）。
        self.assertNotIn(pkg.key_secret[:16], out)
        self.assertNotIn(pkg.key_secret[16:], out)
        # ④ 而 `key_id` 的**完整**原文也不许出现（spec：密钥 Id 只回前 4 位 + ***）。
        self.assertNotIn(pkg.key_id, out)

    def test_add_output_does_not_contain_key_secret(self):
        """`add` 自己的输出同样不回显密钥（操作员的终端会进日志 / 截图）。"""
        out, err = run_command(
            "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=5
        )
        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)  # 前提自证
        self.assertNotIn(pkg.key_secret, out)
        self.assertNotIn(pkg.key_secret, err)
        self.assertNotIn(pkg.key_id, out)

    # ── ⑤ `--total-quota 0` ⇒ 报错、不建行 ─────────────────────────────
    def test_add_with_zero_total_quota_is_rejected_and_creates_nothing(self):
        with self.assertRaises(CommandError):
            run_command(
                "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=0
            )

        # 终态：一行都没落（`call_command` 把 `CommandError` 抛出来 ⇒ `manage.py`
        # 以退出码 1 结束、⛔ 不会静默成功）。
        self.assertEqual(RealNamePackage.objects.count(), 0)

    def test_add_with_negative_total_quota_is_rejected(self):
        with self.assertRaises(CommandError):
            run_command(
                "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=-1
            )
        self.assertEqual(RealNamePackage.objects.count(), 0)

    def test_add_with_blank_expires_at_is_rejected_not_silently_never_expires(self):
        """`--expires-at ""` ⇒ 报错。⛔ 不许静默当成「不过期」（那会让到期包被无限放行）。"""
        with self.assertRaises(CommandError):
            run_command(
                "add",
                key_id=FAKE_KEY_ID,
                key_secret=FAKE_KEY_SECRET,
                total_quota=5,
                expires_at="   ",
            )
        self.assertEqual(RealNamePackage.objects.count(), 0)

    # ── ② grant 两次 ⇒ 累加 ────────────────────────────────────────────
    def test_grant_twice_accumulates_instead_of_overwriting(self):
        run_command("grant", tenant=self.tenant.schema_name, times=100)
        run_command("grant", tenant=self.tenant.schema_name, times=50)

        # 终态一律重新查库：⛔ 不拿内存对象（那会掩盖「累加写没写进库」）。
        grant = RealNameGrant.objects.get(tenant=self.tenant)
        self.assertEqual(grant.granted, 150)
        self.assertEqual(
            RealNameGrant.objects.filter(tenant=self.tenant).count(), 1
        )  # 一个租户**一行**

    # ── ③ grant 给不存在的租户 ⇒ 报错、不建行 ───────────────────────────
    def test_grant_to_unknown_tenant_is_rejected_and_creates_no_row(self):
        with self.assertRaises(CommandError):
            run_command("grant", tenant="no-such-tenant", times=100)

        # 终态：额度表一行都没有；⛔ 租户表也不许被顺手建出来。
        self.assertEqual(RealNameGrant.objects.count(), 0)
        self.assertFalse(
            type(self.tenant).objects.filter(schema_name="no-such-tenant").exists()
        )

    # ── `--times` 的边界（0 / 负数不许静默成功）─────────────────────────
    def test_grant_with_zero_times_is_rejected_and_creates_no_row(self):
        with self.assertRaises(CommandError):
            run_command("grant", tenant=self.tenant.schema_name, times=0)
        self.assertEqual(RealNameGrant.objects.count(), 0)

    def test_grant_with_negative_times_is_rejected_and_creates_no_row(self):
        with self.assertRaises(CommandError):
            run_command("grant", tenant=self.tenant.schema_name, times=-5)
        self.assertEqual(RealNameGrant.objects.count(), 0)

    # ── ④ quota 输出余量 = granted - used ──────────────────────────────
    def test_quota_reports_granted_minus_used(self):
        run_command("grant", tenant=self.tenant.schema_name, times=100)
        # `used` 由扣减路径改（`apps/realname/quota.py`，本线另一任务）⇒ 这里直接改库
        # 造出「已经用过 30 次」的终态，判据只关心本命令读得对不对。
        RealNameGrant.objects.filter(tenant=self.tenant).update(used=30)

        out, _ = run_command("quota", tenant=self.tenant.schema_name)
        self.assertIn("剩余 70", out)
        self.assertIn("已拨 100", out)
        self.assertIn("已用 30", out)

    def test_quota_for_tenant_without_grant_row_reports_zero_and_creates_nothing(self):
        """⛔ 只读子命令读到「没拨过」时**不许建行**（读一下就改库是最难查的副作用）。"""
        out, _ = run_command("quota", tenant=self.tenant.schema_name)
        self.assertIn("剩余 0", out)
        self.assertEqual(RealNameGrant.objects.count(), 0)

    def test_quota_output_does_not_contain_key_secret(self):
        """`quota` 是操作员最常跑的一条 ⇒ 同一条「不回显密钥」的口径也要打在它上面。"""
        run_command(
            "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=1000
        )
        run_command("grant", tenant=self.tenant.schema_name, times=100)
        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)  # 前提自证

        out, err = run_command("quota", tenant=self.tenant.schema_name)
        self.assertNotIn(pkg.key_secret, out)
        self.assertNotIn(pkg.key_secret, err)

    # ── 顺带：add 不许静默覆盖已有的密钥 Id（密钥不可回读，覆盖等于丢）─────
    def test_add_with_existing_key_id_is_rejected_and_keeps_original(self):
        run_command(
            "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=1000
        )
        with self.assertRaises(CommandError):
            run_command(
                "add",
                key_id=FAKE_KEY_ID,
                key_secret="SYNTH-9999-REPLACEMENT-SECRET",
                total_quota=10,
            )
        self.assertEqual(RealNamePackage.objects.count(), 1)
        pkg = RealNamePackage.objects.get(key_id=FAKE_KEY_ID)
        self.assertEqual(pkg.key_secret, FAKE_KEY_SECRET)
        self.assertEqual(pkg.total_quota, 1000)

    def test_unique_constraint_race_translates_to_the_same_duplicate_text(self):
        """并发双录兜底（2026-10-16 修复轮）：预检通过后、INSERT 前另一笔已落库
        ⇒ `key_id` 唯一约束（platform 0004）落 `IntegrityError`，翻成**同一句**既有
        重复文案，仍只一行（⛔ 不是未预期 500、也不是第二行）。

        ⚠️ 竞态窗口的确定性形态：把 `filter(...)` 桩成空集（=「预检跑在对方提交之前」
        的真实时序），让 INSERT **真的撞库**——上一条用例（预检放行、同一载荷走文案
        相同的预检分支）就是这条的「桩接没接上」对照：没有唯一约束时本条会静默建出
        第二行 ⇒ `count` 断言红。
        ⚠️ patch 打在**被查找处**（`package_admin` 引用的同一个 manager 对象）。
        """
        run_command("add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=1000)
        with mock.patch.object(
            RealNamePackage.objects, "filter", return_value=RealNamePackage.objects.none()
        ):
            with self.assertRaises(CommandError) as ctx:
                run_command(
                    "add",
                    key_id=FAKE_KEY_ID,
                    key_secret="SYNTH-9999-RACE-SECRET",
                    total_quota=10,
                )
        self.assertIn("已经录过了", str(ctx.exception))
        # 终态：仍只一行，原 secret 没被覆盖。
        self.assertEqual(RealNamePackage.objects.count(), 1)
        self.assertEqual(
            RealNamePackage.objects.get(key_id=FAKE_KEY_ID).key_secret, FAKE_KEY_SECRET
        )

    # ── 只读子命令在空库上的样子（⛔ 不报错、也⛔ 不建行）──────────────
    def test_list_on_empty_table_is_not_an_error(self):
        out, _ = run_command("list")
        self.assertIn("还没有录入任何密钥包", out)
        self.assertEqual(RealNamePackage.objects.count(), 0)


class RealNamePackageGrantConcurrencyTests(TransactionTestCase):
    """⭐ 承重判据：两个并发 `grant` ⇒ `granted` **恰好 +2**（守 `F()` + 行锁）。

    见模块 docstring：`TransactionTestCase` 是**必须**的（`TestCase` 的外层事务会让两条
    线程看不到彼此、并发根本不会发生 ⇒ 用例失去判别力）。

    ⚠️ **这条用例是「`F()` + `select_for_update()` 有没有判据守着」的答案**：
    把命令里的 `F("granted") + times` 换回读-改-写（`grant.granted += times; save()`），
    这条会红（终态 2 而不是 3）。变异读数见 T2 报告。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase.setUpClass` / `tearDownClass` 的等价物（django_tenants 只给了
        # `TestCase` 版）。⚠️ 这里建租户只是为了拿一个真实的 `Tenant` 行当外键目标；
        # 平台表在 public，与租户 schema 无关。
        cls.tenant = get_tenant_model()(schema_name=CONCURRENT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=CONCURRENT_DOMAIN)
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
        # 前提自证：确实跑在**无外层事务**的形态上；否则两条线程只会看到彼此的未提交状态，
        # 用例没有判别力（这正是 `tests/test_account_concurrency.py` 点名的假通过）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)

    def test_two_concurrent_grants_add_up_to_exactly_two_times_the_amount(self):
        """两条真实连接**同时**各拨 1 次 ⇒ 终态恰好 `初值 + 2`。

        ⚠️ 初值刻意取 **0**（⛔ 不是 1）：`0` 是 `granted` 的合法边界，
        而且它让「+2」这条判据的读数与「丢一次更新」的读数（= 1）分得开。
        """
        # ⚠️ 先清掉本租户可能残留的那一行（同 `test_concurrent_grants_are_serialized_…`
        # 的理由：平台表在每个租户 schema 里也有一份影子表，而 flush 只清 public）
        # ⇒ 本用例要的是「恰好一行、初值 0」这个确定前提。
        RealNameGrant.objects.filter(tenant=self.tenant).delete()
        RealNameGrant.objects.create(tenant=self.tenant, granted=0)
        self.assertEqual(RealNameGrant.objects.get(tenant=self.tenant).granted, 0)

        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def worker(key: str) -> None:
            """在**本线程自己的连接**上拨一次额度（Django 的连接是 thread-local）。"""
            try:
                close_old_connections()
                connection.set_tenant(self.tenant)
                barrier.wait(timeout=BARRIER_TIMEOUT)
                run_command("grant", tenant=CONCURRENT_SCHEMA, times=1)
                results[key] = "ok"
            except Exception as exc:  # noqa: BLE001 —— 结果要回主线程断言
                results[key] = f"{type(exc).__name__}: {exc}"
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(key,)) for key in ("alpha", "beta")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT)

        # 两条线程都真的跑完了（⛔ 挂起 / 异常不许伪装成「读数不对」）。
        self.assertEqual(results, {"alpha": "ok", "beta": "ok"})

        # ⭐ 终态：0 + 1 + 1 = 2。丢一次更新（或 `update` 根本没落库）⇒ 1。
        connection.set_tenant(self.tenant)
        grant = RealNameGrant.objects.get(tenant=self.tenant)
        self.assertEqual(
            grant.granted,
            2,
            "两个并发 grant 没有都记上（初值 0 + 1 + 1 应为 2）",
        )
        # 一个租户**一行**（`uniq_realname_grant_tenant`，并发下也不许变成两行）。
        self.assertEqual(RealNameGrant.objects.filter(tenant=self.tenant).count(), 1)

    def test_concurrent_grants_do_not_deadlock_or_hang(self):
        """两条真实并发 `grant` 都必须**跑完**（⛔ 不许死锁 / 长期挂起）。

        ⚠️ **这条守什么、⛔ 不守什么**（照 `AGENTS.md` §5-E）：

        - **守**：「两个并发拨额度不会互相等死」——每次 grant 都开一个 `atomic()` 并拿行锁，
          锁序若被写反（例如先锁 `Tenant` 行再锁额度行、另一条路反过来）就会成环 ⇒
          PostgreSQL 报 `DeadlockDetected`（不是业务码）或干脆挂到超时。
          判据是**两条线程都返回、且都成功**，外加一个有限 `join` 超时（挂起即红）。
        - ⛔ **不守**「`grant` 用了 `select_for_update()`」：实测（T2 变异验证）把
          `select_for_update()` 整个删掉，本条**照样绿**——因为那条 `UPDATE` 自己也会拿排它行锁
          （PostgreSQL 的写锁与 `FOR UPDATE` 是同一把），锁行为在**这条路径上**没变。
          守「丢更新」的是上面那条 `test_two_concurrent_grants_add_up_to_exactly_two_times_the_amount`。
        """
        # ⚠️ 同前：flush 只清 public，先清掉本租户可能残留的行（唯一约束）。
        RealNameGrant.objects.filter(tenant=self.tenant).delete()
        RealNameGrant.objects.create(tenant=self.tenant, granted=0)

        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def worker(key: str) -> None:
            try:
                close_old_connections()
                connection.set_tenant(self.tenant)
                barrier.wait(timeout=BARRIER_TIMEOUT)
                run_command("grant", tenant=CONCURRENT_SCHEMA, times=1)
                results[key] = "ok"
            except Exception as exc:  # noqa: BLE001
                results[key] = f"{type(exc).__name__}: {exc}"
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(key,)) for key in ("alpha", "beta")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT)

        # ⛔ 挂起 / 死锁 / 异常一律红（不许被当成「读数不对」）。
        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "并发 grant 挂住了（死锁或锁等待无上限）",
        )
        self.assertEqual(results, {"alpha": "ok", "beta": "ok"})
        connection.set_tenant(self.tenant)
        self.assertEqual(
            RealNameGrant.objects.get(tenant=self.tenant).granted, 2
        )


class RealNamePackageCommandWiringTests(TestCase):
    """接线判据（不碰租户 schema ⇒ 用最轻的 `TestCase`）。"""

    def test_command_is_discoverable_with_the_four_subcommands(self):
        from django.core.management import get_commands

        self.assertEqual(get_commands().get("realname_package"), "apps.platform")

    def test_no_admin_module_in_the_platform_app(self):
        """⛔ 本仓没有 admin（T2 删掉了 T1 的 `apps/platform/admin.py`）。

        ⚠️ 这条钉的是**结构性事实**：`django.contrib.admin` 不在 `INSTALLED_APPS`
        ⇒ 任何 `apps/platform/admin.py` 都长不出界面。重新加回它之前先看
        `config/settings.py` 的禁令与 D-0196。
        """
        with self.assertRaises(ImportError):
            import apps.platform.admin  # noqa: F401

    def test_platform_tables_are_read_from_the_public_schema(self):
        """对照判据：命令从 **public** 那一份平台表读写（⛔ 不是当前连接的那一份）。

        ⚠️ 这条**只在 `TestCase`（public 平面）上**证明「命令用的是 public」，
        单看它还不够：`TenantTestCase` 那一组用例是在**租户 schema 上下文**里跑的，
        它们照样能读到同一行 ⇒ 两者合起来才说明命令**显式**切回了 public。
        """
        from django_tenants.utils import get_public_schema_name

        self.assertEqual(get_public_schema_name(), "public")
        self.assertEqual(connection.schema_name, get_public_schema_name())

        run_command(
            "add", key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET, total_quota=7
        )
        # 用**裸 SQL** 查 public 的那张表（绕过 ORM 的 search_path ⇒ 直接证明行落在 public）。
        with connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.platform_realnamepackage WHERE key_id = %s",
                [FAKE_KEY_ID],
            )
            self.assertEqual(cur.fetchone()[0], 1)
