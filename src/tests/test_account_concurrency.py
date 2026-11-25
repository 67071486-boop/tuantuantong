"""双连接验证管理员保护、账号档案更新与账号和角色请求的互斥。
使用 TransactionTestCase，避开 TenantTestCase 的外层事务（`django_tenants/test/cases.py:8`）。
并发停用只保留两个活跃管理员，以识别两次停用同时生效。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from time import monotonic
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model

from apps.accounts.guards import active_admin_count
from apps.accounts.models import StaffProfile
from apps.accounts.views import AccountViewSet
from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.business.roles import TENANT_ADMIN
from apps.platform.provisioning import seed_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: `TenantTestCase.get_test_tenant_domain()` 的默认域名（与既有账号测试同口径）。
DOMAIN = "tenant.test.com"

#: 两个请求必须都在这个时间内到达交错点/结束。正常运行是毫秒级；给 15 秒是给
#: 「两条连接 + 一次登录」留余量，同时不让「锁写坏了」变成无限挂起。
BARRIER_TIMEOUT = 15.0

#: 业务错误码字面值（写数字是为了让「码值被改掉」在测试里可见）。
OK = 0
LAST_ADMIN = 1003


class _ActiveAdminConcurrencyFixture(TransactionTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase.setUpClass` / `tearDownClass` 的等价物
        # （django_tenants 只给了 `TestCase` 版）。
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=DOMAIN)
        cls.domain.save()
        # `TenantTestCase.add_allowed_test_domain()` 的等价物（Host 路由要放行它）。
        if DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [DOMAIN]
        connection.set_tenant(cls.tenant)
        seed_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN)
        super().tearDownClass()

    def setUp(self):
        # 前提自证：本用例确实跑在**无外层事务**的形态上。跑在 `TestCase` 里时
        # `get_autocommit()` 恒为 False，两个线程只会看到彼此的未提交状态——
        # 那条测试没有判别力（这正是 §7.7 点名的假通过）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        Game.objects.create(name="王者荣耀")
        # ⚠️ 停用基线超管：它是活跃管理员，留着会让「双双通过」也照样 ≥ 1 个活跃管理员，
        # 从而把本用例的判别力抹掉（见模块说明）。
        User.objects.filter(username="admin").update(is_active=False)
        # 两个可登录的管理员：互相是对方的「最后一个别的活跃管理员」。
        self.alpha = self._make_admin("conc-alpha")
        self.beta = self._make_admin("conc-beta")
        # 夹具前提：恰好两个活跃管理员，且都活跃（③ 自锁不会抢在①②前面）。
        self.assertTrue(self.alpha.is_active and self.beta.is_active)
        self.assertEqual(active_admin_count(), 2)
        # 反向自证：基线超管确实被停用了（否则下面「终态 = 1」可以由它凑出来）。
        self.assertFalse(User.objects.get(username="admin").is_active)

    def _make_admin(self, username):
        client = make_logged_in_client(username=username, domain=DOMAIN)
        user = User.objects.get(username=username)
        user.groups.add(Group.objects.get(name=TENANT_ADMIN))
        user.client = client          # 挂在实例上，供线程取用
        return user


class ConcurrentLastAdminDisableTests(_ActiveAdminConcurrencyFixture):
    """两条真实连接 + 屏障：并发停用必须留下至少一个活跃管理员。"""

    def _post_disable(self, actor, target_pk, results, key):
        """在**本线程自己的连接**上发一次「停用 target」，结果写进 `results[key]`。

        每条线程一条连接（Django 的连接是 thread-local）：不这么做，两条线程会共用
        主线程那条连接，于是它们**排在同一条连接上**、根本不可能并发。
        """
        try:
            close_old_connections()
            connection.set_tenant(self.tenant)
            response = actor.client.post(
                f"/api/accounts/{target_pk}/status/",
                {"is_active": False},
                content_type="application/json",
            )
            results[key] = {"status_code": response.status_code, **response.json()}
        except Exception as exc:                      # noqa: BLE001 — 结果要回主线程断言
            results[key] = {"exception": f"{type(exc).__name__}: {exc}"}
        finally:
            connection.close()

    def test_two_simultaneous_last_admin_disables_cannot_both_succeed(self):
        """核心用例：两个「停用对方」的请求并发 ⇒ 恰一个成功，终态仍有 1 个活跃管理员。"""
        both_read_targets = threading.Event()
        barrier = threading.Barrier(2, action=both_read_targets.set)
        results = {}
        original_get_object = AccountViewSet.get_object

        def synchronize_after_target_read(view):
            user = original_get_object(view)
            barrier.wait(timeout=BARRIER_TIMEOUT)
            return user

        threads = [
            threading.Thread(
                target=self._post_disable,
                args=(self.alpha, self.beta.pk, results, "alpha"),
            ),
            threading.Thread(
                target=self._post_disable,
                args=(self.beta, self.alpha.pk, results, "beta"),
            ),
        ]
        with mock.patch.object(AccountViewSet, "get_object", synchronize_after_target_read):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=BARRIER_TIMEOUT * 2)
        self.assertFalse(any(thread.is_alive() for thread in threads),
                         "线程没有结束——多半是 advisory 锁与屏障互相等了（锁序问题）")
        self.assertTrue(both_read_targets.is_set(), results)

        # 先排掉「不是业务失败」的归宿：异常会被包成 `{"exception": ...}`。
        for key, body in results.items():
            self.assertNotIn("exception", body,
                             f"{key} 线程抛了异常（不是业务失败）：{body}")
            self.assertNotEqual(body.get("status_code"), 500, body)

        codes = sorted(body["code"] for body in results.values())
        # ⚠️ 断言的核心：**不能两个都成功**。两个都成功说明第二个请求在
        # 「还剩几个活跃管理员」的算术里没看到第一个请求的那笔写 ⇒ 两笔写没有串行化
        # ⇒ 终态 0 个活跃管理员。这正是 spec §6.2.1 要挡的形态。
        self.assertEqual(codes, [OK, LAST_ADMIN],
                         f"并发停用的码值不是「一成功一拒绝」：{results}")

        # 终态断言打在**数据库行**上（E-0009），不看码值。
        self.alpha.refresh_from_db()
        self.beta.refresh_from_db()
        # 恰一个还活跃——「≥ 1」是不变式，「= 1」是这条用例能给出更强结论的地方。
        self.assertEqual(
            sum(1 for user in (self.alpha, self.beta) if user.is_active), 1,
            "α/β 的活跃状态不满足「恰一个还活跃」",
        )
        self.assertEqual(active_admin_count(), 1, "终态活跃管理员数不是 1")


class ConcurrentAccountAndRoleMutationTests(_ActiveAdminConcurrencyFixture):
    def test_removing_the_admin_role_waits_for_an_account_status_commit(self):
        role = Role.objects.get(name=TENANT_ADMIN)
        account_audit_written = threading.Event()
        role_finished = threading.Event()
        role_connection_ready = threading.Event()
        role_pid = None

        def delete_role():
            nonlocal role_pid
            try:
                connection.set_tenant(self.tenant)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_backend_pid(), set_config('statement_timeout', %s, false)",
                        [f"{BARRIER_TIMEOUT}s"],
                    )
                    role_pid, _ = cursor.fetchone()
                role_connection_ready.set()
                if not account_audit_written.wait(timeout=BARRIER_TIMEOUT):
                    raise AssertionError("账号请求没有到达审计写入")
                return self.beta.client.delete(f"/api/role-authorities/{role.pk}/")
            finally:
                role_finished.set()
                connection.close()

        def wait_for_role_block(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith('INSERT INTO "audit_operationlog"'):
                # 在账号请求的审计写入后、提交前，让第二条连接开始 DELETE。
                # 用持锁的连接观察 PostgreSQL，整个测试只有两条连接。
                self.assertTrue(connection.in_atomic_block)
                account_audit_written.set()
                deadline = monotonic() + BARRIER_TIMEOUT
                with connection.cursor() as cursor:
                    while monotonic() < deadline:
                        cursor.execute(
                            """
                            SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))
                            AND EXISTS (
                                SELECT 1 FROM pg_locks
                                WHERE pid = %s AND locktype = 'advisory' AND NOT granted
                            )
                            """,
                            [role_pid, role_pid],
                        )
                        if cursor.fetchone()[0]:
                            return result
                        if role_finished.wait(0.02):
                            break
                self.fail("PostgreSQL 未观测到角色请求等待账号请求持有的 advisory 锁")
            return result

        with ThreadPoolExecutor(max_workers=1) as executor:
            delete_future = executor.submit(delete_role)
            self.assertTrue(role_connection_ready.wait(timeout=BARRIER_TIMEOUT))
            with connection.execute_wrapper(wait_for_role_block):
                status = self.alpha.client.post(
                    f"/api/accounts/{self.beta.pk}/status/",
                    {"is_active": False},
                    content_type="application/json",
                )
            deleted = delete_future.result(timeout=BARRIER_TIMEOUT)

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json(), {"code": OK, "msg": "", "data": {"updated": [self.beta.pk]}})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["code"], 1000)
        self.assertIn("最后一个系统管理员", deleted.json()["msg"])
        self.beta.refresh_from_db()
        self.assertFalse(self.beta.is_active)
        self.assertEqual(active_admin_count(), 1)
        role.refresh_from_db()
        self.assertEqual(role.name, TENANT_ADMIN)
        self.assertEqual(role.group.name, TENANT_ADMIN)
        log = OperationLog.objects.get(action="user.status", target_id=str(self.beta.pk))
        self.assertEqual((log.actor, log.from_value, log.to_value), (self.alpha.username, "有效", "禁用"))
        self.assertFalse(OperationLog.objects.filter(action="system.role_destroy").exists())


class ConcurrentProfileUpdateTests(TransactionTestCase):
    """R15：两条真实连接并发改**同一账号的不同档案列**，两笔都必须存活。

    **判别力自证（做这条测试的意义所在）**：把裁决 R15 的两半同时还原成修复前的样子
    ——① `update()` 在 `atomic()` **之外** `get_object()`（读早于取锁）；
    ② `_persist_account` 对既存档案 `profile.save()` **不带** `update_fields=`——
    本用例**必须变红**（后写的那条整行写回，把先写的那列盖掉）。实测结果见
    `final-fix-report.md`；**不跑这条对照，就没有证据说本用例有判别力**。

    ⚠️ 如实口径：两半是**各自独立**的修复（只还原其中一半，本用例仍可能绿）——
    `update_fields=` 让每次只写本次改过的列，读在锁之后让快照不过期。本用例证的是
    「修复前的组合形态会丢更新」，不对「哪一半是必需的」下断言。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=DOMAIN)
        cls.domain.save()
        if DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [DOMAIN]
        connection.set_tenant(cls.tenant)
        seed_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN)
        super().tearDownClass()

    def setUp(self):
        # 前提自证：本用例确实跑在**无外层事务**的形态上（理由见模块说明）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        self.alpha = self._make_admin("conc-profile-alpha")
        self.beta = self._make_admin("conc-profile-beta")
        # 靶子：一个**非管理员**账号，档案里两列各有**非默认值**（否则「被盖回旧值」
        # 与「本来就是默认值」在断言里长得一样），再留一列当见证者。
        self.target = User.objects.create_user(
            username="conc-profile-target", password="pw-123456"
        )
        StaffProfile.objects.create(
            user=self.target,
            qq="qq-original",
            deposit_amount=Decimal("3.00"),
            mobile="13800000000",
        )

    def _make_admin(self, username):
        client = make_logged_in_client(username=username, domain=DOMAIN)
        user = User.objects.get(username=username)
        user.groups.add(Group.objects.get(name=TENANT_ADMIN))
        user.client = client
        return user

    def _patch(self, actor, field, value, results, key, before_lock):
        """在**本线程自己的连接**上发一次 PATCH，结果写进 `results[key]`。"""
        try:
            close_old_connections()
            connection.set_tenant(self.tenant)
            with connection.execute_wrapper(before_lock):
                response = actor.client.patch(
                    f"/api/accounts/{self.target.pk}/",
                    {field: value},
                    content_type="application/json",
                )
            results[key] = {"status_code": response.status_code, **response.json()}
        except Exception as exc:                      # noqa: BLE001 — 结果要回主线程断言
            results[key] = {"exception": f"{type(exc).__name__}: {exc}"}
        finally:
            connection.close()

    def test_concurrent_profile_field_updates_both_survive(self):
        both_reached_lock = threading.Event()
        barrier = threading.Barrier(2, action=both_reached_lock.set)
        results = {}

        def synchronize_before_lock(execute, sql, params, many, context):
            if sql.startswith("SELECT pg_advisory_xact_lock("):
                barrier.wait(timeout=BARRIER_TIMEOUT)
            return execute(sql, params, many, context)

        threads = [
            threading.Thread(
                target=self._patch,
                args=(self.alpha, "qq", "qq-from-alpha", results, "alpha", synchronize_before_lock),
            ),
            threading.Thread(
                target=self._patch,
                args=(self.beta, "deposit_amount", "999.00", results, "beta", synchronize_before_lock),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=BARRIER_TIMEOUT * 2)
        self.assertTrue(both_reached_lock.is_set(), results)
        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "线程没有结束——多半是租户 advisory 锁与屏障互相等了（锁序问题）",
        )

        for key, body in results.items():
            self.assertNotIn("exception", body, f"{key} 线程抛了异常：{body}")
            self.assertEqual(body.get("status_code"), 200, body)
            self.assertEqual(body.get("code"), OK, body)

        # 断言落在**数据库行**上（E-0009）：两笔改动都必须留下。
        profile = StaffProfile.objects.get(user=self.target)
        self.assertEqual(profile.qq, "qq-from-alpha", "另一条请求把 qq 盖回了旧值")
        self.assertEqual(
            profile.deposit_amount, Decimal("999.00"),
            "另一条请求把 deposit_amount 盖回了旧值",
        )
        # 见证列：两条请求都没碰它，整行写回时它必须仍是原值（不是被顺手重置成别的）。
        self.assertEqual(profile.mobile, "13800000000")
