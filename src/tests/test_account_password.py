"""§7.2 密码：生成、`save()`、一次性回显、端到端登录。

本文件按 spec / 项目实况钉住四处与常见直觉不同的口径：
- **R5**：`TenantTestCase.setUpClass` 只建 schema 跑迁移，**不灌**基线数据，
  所以 `Group.objects.get(TENANT_ADMIN)` 前必须显式 `seed_tenant(cls.tenant)`；
- **§7.2-5**：真的用**同一段**密码文本（不能一侧端点生成、另一侧写死别的文本）；
- **§7.2-3**：brief 只钉了详情不含口令，spec 要求**详情与列表两个出口都钉**；
- **§7.2-7**：brief 的负断言 `assertNotIn(password, records)` 拿**列表**比整条消息，
  对 `logger.info("密码=%s", pw)` 这种**包含**关系恒真（已实测）——这里 join 后再比。
"""
import contextlib
import io
import json
import logging
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.views import RESET_PASSWORD
from apps.business.models import Game
from apps.business.roles import TENANT_ADMIN
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 登录夹具用的租户域名（`TenantTestCase.get_test_tenant_domain` 的默认值）。
DOMAIN = "tenant.test.com"

#: R13 / spec §3.2 / §4.2：新增载荷必须带齐的六个「必填，照抄」字段的合法取值。
CREATE_REQUIRED_PROFILE_FIELDS = {
    "display_name": "小王",
    "commission_value": "10.00",
    "grab_limit_dl": 0,
    "grab_limit_pw": 0,
    "hired_on": "2026-01-02",
    "deposit_amount": "0.00",
}


class AccountPasswordTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # R5：`TenantTestCase` 不跑 `seed_tenant`，默认 Group 与 `admin` 都不存在。
        # 本类的 `setUp` 要 `Group.objects.get(name=TENANT_ADMIN)`，不灌就 `DoesNotExist`。
        seed_tenant(cls.tenant)
        # 第二个租户在 `setUpClass` 里建：在用例内部建 schema 会与外层事务相冲
        # （与 `tests/test_account_api.py::AccountCrossTenantTests` 同手法）。
        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_hash_b", name="租户 B", domain="acct-hash-b.test.local"
        )

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="pw-admin")
        User.objects.get(username="pw-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))

    def _create(self, username="newbie"):
        return self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": username, "role": "客服", "games": [self.game.pk],
            },
            content_type="application/json",
        )

    def test_create_returns_six_digit_initial_password(self):
        body = self._create().json()
        self.assertEqual(body["code"], 0)
        self.assertRegex(body["data"]["initial_password"], r"^\d{6}$")
        # 正向对照：回显的口令属于**这次新建的那个账号**，不是随手编的字符串。
        self.assertEqual(body["data"]["account"]["username"], "newbie")
        self.assertTrue(User.objects.filter(username="newbie").exists())

    def test_generated_password_actually_logs_in(self):
        """**端到端**：只断言「字段非空」等于没测——必须真的登得进去。"""
        password = self._create().json()["data"]["initial_password"]
        user = authenticate(username="newbie", password=password)
        self.assertIsNotNone(user, "生成的密码登不进去——set_password 后可能漏了 save()")

    def test_generated_password_logs_in_through_the_login_endpoint(self):
        """上一条走 `authenticate()`；这条走**真实登录端点**（会话真的建起来）。

        两条都要：`authenticate()` 只证明哈希对得上，不证明账号可用
        （`is_active=False` 或身份平面校验不过时，前者仍会通过）。
        """
        password = self._create().json()["data"]["initial_password"]
        fresh = Client(SERVER_NAME=DOMAIN)
        response = fresh.post(
            "/api/auth/login",
            data={"username": "newbie", "password": password},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        # 负向对照：错口令不得登录（否则上面的 200 可能由「端点恒 200」凑出来）。
        wrong = Client(SERVER_NAME=DOMAIN)
        bad = wrong.post(
            "/api/auth/login",
            data={"username": "newbie", "password": "not-the-password"},
            content_type="application/json",
        )
        self.assertNotEqual(bad.json()["code"], 0)

    def test_detail_and_list_never_return_the_password(self):
        """§7.2-3：`initial_password` 只出现在新增响应里。详情**与列表**两个出口都要钉。"""
        self._create()
        target = User.objects.get(username="newbie")
        body = self.client.get(f"/api/accounts/{target.pk}/").json()
        self.assertEqual(body["code"], 0)
        self.assertNotIn("initial_password", body["data"])
        self.assertNotIn("password", body["data"])

        listing = self.client.get("/api/accounts/").json()["data"]["items"]
        # 正向对照：这条账号确实出现在列表里——否则「列表里没有口令」由空列表凑出来。
        self.assertIn(target.pk, [item["id"] for item in listing])
        self.assertTrue(
            all("initial_password" not in item and "password" not in item for item in listing),
            "列表载荷里出现了口令形态的键",
        )

    def test_password_is_hashed_not_plaintext(self):
        password = self._create().json()["data"]["initial_password"]
        user = User.objects.get(username="newbie")
        self.assertNotEqual(user.password, password)
        self.assertTrue(user.check_password(password))   # 哈希确实对应这个口令
        self.assertGreater(len(user.password), 30)        # 不是明文（不明文断言具体算法前缀）

    def test_hashes_differ_across_tenants_for_the_same_password(self):
        """§7.2-5 / D12：**同一段密码文本**在两个租户里的两行 `password` 逐字节不同。

        ⚠️ 归因：差异来自 `make_password()` 的**随机盐**，不是「按租户加盐」。
        所以同一租户里两个同密码用户同样不同——这条测的其实是「哈希不是密码的函数」。
        """
        # 同一段文本：A 的账号由**端点**建（口令是服务端生成的），B 的账号直接建，
        # 用的是从 A 的响应里取回的那串口令。两份文本逐字节相同。
        password = self._create("same-pw-a").json()["data"]["initial_password"]
        hash_a = User.objects.get(username="same-pw-a").password
        with tenant_context(self.tenant_b):
            User.objects.create_user(username="same-pw-b", password=password)
            hash_b = User.objects.get(username="same-pw-b").password
        self.assertNotEqual(hash_a, hash_b)
        # 反向断言：两边确实各有那个人（防「两边都是空的」假通过）
        self.assertTrue(User.objects.filter(username="same-pw-a").exists())
        with tenant_context(self.tenant_b):
            self.assertTrue(User.objects.filter(username="same-pw-b").exists())
            # 同时证明 A 的行没有漏进 B：B 里只有本用例建的那一个同名账号。
            self.assertEqual(User.objects.filter(username="same-pw-a").count(), 0)

    def test_duplicate_username_returns_1001_not_500(self):
        self.assertEqual(self._create("dup").json()["code"], 0)
        body = self._create("dup").json()
        self.assertEqual(body["code"], 1001)
        self.assertEqual(User.objects.filter(username="dup").count(), 1)

    def test_serializer_failure_path_also_skips_password_generation(self):
        """**另一条**失败分支（序列化器校验不过）同样不生成口令（spec §6.1 的一次性出口）。

        与 `test_integrity_error_path_*` 互补：那条走 `IntegrityError`，这条在
        `is_valid()` 就返回，连写路径都没进。两条都钉住，才不会是「只钉了其中一支」。
        """
        generate_calls = []
        with mock.patch(
            "apps.accounts.views._generate_initial_password",
            side_effect=lambda: generate_calls.append(1) or "246810",
        ):
            first = self._create("dup-again").json()
            second = self._create("dup-again").json()
        self.assertEqual(first["code"], 0)
        self.assertEqual(second["code"], 1001)
        # 第一次（成功）生成一次；第二次（被存在性检查挡住）不得再生成。
        self.assertEqual(generate_calls, [1])

    def test_create_requires_an_account_admin(self):
        """写路径的准入**不是零节点账号能过的**（spec D6 / §7.6 第 3 行）。

        ⚠️ 账号端点的准入是 `IsAccountManager`（系统管理员 **或** 持任一账号管理节点，
        `apps/accounts/permissions.py`）。本用例的 `outsider` 是**零节点**账号
        （`make_logged_in_client(username="pw-outsider")`，不传 `groups` / `nodes`）
        ⇒ 装配顺序 `[IsAuthenticated, gate, IsAccountManager]` 下先拒他的是**节点门**
        （`system_user.add_edit`）——403 的来源因此是节点门，不是准入类。
        ⚠️ **用例名保留历史值**：它钉的那件事没变——「不是管账号这块料的人发不出去」。
        正向对照在**同一用例内**：同一个载荷换成管理员发就成功——
        所以 403 是权限挡的，不是载荷本身有问题。
        """
        payload = {
            **CREATE_REQUIRED_PROFILE_FIELDS,
            "username": "nobody-made", "role": "客服", "games": [self.game.pk],
        }
        outsider = make_logged_in_client(username="pw-outsider")
        response = outsider.post(
            "/api/accounts/", data=payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertFalse(User.objects.filter(username="nobody-made").exists())
        self.assertEqual(
            self.client.post(
                "/api/accounts/", data=payload, content_type="application/json"
            ).json()["code"],
            0,
        )

    def test_failed_password_write_leaves_no_account_row(self):
        """B 项：账号行与口令 `save()` 必须在**同一个** `atomic()` 里。

        探针是「口令列的那次 UPDATE 失败」——它发生在账号行写成功**之后**。若两次写各在一个
        `atomic()` 里，先提交的账号行会留下来，于是库里多一个**没有可用口令**的账号：
        接口没回 `initial_password`（本次请求失败），这个号谁也登不进去，也永远没人知道
        它的口令是什么。

        ⚠️ 探针打在 `User.save(update_fields=["password"])` 上，而不是 patch 口令生成器：
        要复现的正是「账号行已落库、口令那一次 UPDATE 失败」这个顺序。
        `update_fields` 不是 `["password"]` 的 `save()`（`_persist_account` 自己那次）照常放行。

        ⚠️ 失败码是 `create` 那条 catch-all 的 1001（它的 msg 对「非 username 约束」不精确，
        已被 `create` 的注释登记）——本用例要钉的是**没有残留行**，不是文案。
        """
        real_save = User.save

        def failing_save(instance, *args, **kwargs):
            if kwargs.get("update_fields") == ["password"]:
                raise IntegrityError("模拟口令列 UPDATE 失败")
            return real_save(instance, *args, **kwargs)

        with mock.patch.object(User, "save", failing_save):
            response = self._create("half-written")

        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1001)
        self.assertFalse(
            User.objects.filter(username="half-written").exists(),
            "口令写失败后账号行还在库里——那是一个没有可用口令的账号",
        )

        # 正向对照：同一个 username 随后能正常建号，且口令真的对得上——
        # 所以上面的 False 不是「这个名字压根建不出来」凑出来的。
        body = self._create("half-written").json()
        self.assertEqual(body["code"], 0)
        self.assertTrue(
            User.objects.get(username="half-written").check_password(
                body["data"]["initial_password"]
            )
        )

    def test_integrity_error_path_returns_1001_envelope_and_no_password(self):
        """并发兜底分支：写路径抛 `IntegrityError` → 信封 1001，且**任何形态的口令都不出现**。

        ⚠️ 这条分支在非竞态下不可达（重名被序列化器的存在性检查挡住），只能靠 patch 命中：
        patch 的是 **view 里调用的那个模块级名字**（`_persist_account`），不是
        `django.contrib.auth.models.User`——后者打不中 `views` 里的引用。
        （补丁写错时 `mock.patch` 会直接 `AttributeError`，所以 patch 成功本身就是
        「这个名字今天存在」的证据。）

        ⚠️ 生成口令的判别力来自 `generate_calls`：本用例里 `_persist_account` **必然抛异常**，
        所以**没有任何成功路径**能凑出这个计数——非空就只可能来自「生成跑在写成功之前」。
        实测过：生成在 `try` 之前、在 `atomic()` 里写在写之前，这一条都变红；
        只有「写在生成之前」才绿。
        """
        payload = {
            **CREATE_REQUIRED_PROFILE_FIELDS,
            "username": "race-loser", "role": "客服", "games": [self.game.pk],
        }
        generate_calls = []
        with mock.patch(
            "apps.accounts.views._generate_initial_password",
            side_effect=lambda: generate_calls.append(1) or "246810",
        ), mock.patch(
            "apps.accounts.views._persist_account", side_effect=IntegrityError
        ) as persist:
            response = self.client.post(
                "/api/accounts/", data=payload, content_type="application/json"
            )

        # 信封形状：不是 HTML 500、不是裸 500。`status_code` 为 200 是业务码口径（信封 §1）。
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1001)
        self.assertTrue(body["msg"], "1001 分支必须带可读 msg")
        # 「没有口令」的**结构性**断言：信封就是这三个键、`data` 是空的。
        # 这比 `assertNotIn("initial_password", body)` 强——后者只看一个键名，
        # 换个键名（`password` / `pw_initial`）照旧通过。
        self.assertEqual(set(body), {"code", "msg", "data"})
        self.assertIsNone(body["data"])
        self.assertTrue(persist.called, "patch 没被调用——测的不是这条分支")
        # 失败路径**不生成**口令（spec §6.1：它只出现在成功响应体里那一次）。
        self.assertEqual(generate_calls, [], "失败路径仍然调用了口令生成器")
        # 抛异常时 `transaction.atomic()` 回滚 ⇒ 库里没有半成品账号。
        self.assertFalse(User.objects.filter(username="race-loser").exists())

        # 正向对照（E-0011：负断言必须配正向对照）：同一条路径成功时**有** `initial_password`，
        # 且此时生成器**确实**被调用过一次——否则上面那个空列表可能由「生成器压根不工作」凑出来。
        with mock.patch(
            "apps.accounts.views._generate_initial_password",
            side_effect=lambda: generate_calls.append(1) or "246810",
        ):
            ok = self._create("race-loser").json()
        self.assertEqual(ok["code"], 0)
        self.assertEqual(generate_calls, [1])
        self.assertEqual(ok["data"]["initial_password"], "246810")
        self.assertIn("initial_password", json.dumps(ok))

    def test_create_does_not_log_the_password(self):
        """§7.2-7：**根 logger + 自装 handler** 的真捕获（不能用 `assertLogs`——E-0009）。

        ⚠️ 必须带**捕获器自证有判别力**那一步：否则一个「什么都没捕获到」的坏捕获器
        会让负断言永远通过——这正是 E-0009 的假通过形状。

        ⚠️ brief 原文的负断言是 `assertNotIn(password, records)`，而 `records` 是**列表**：
        `in` 比的是「某条消息**整条等于**该口令」，而真实泄漏的形态是
        `logger.info("密码=%s", pw)` 这种**包含**关系。照抄 brief 会把这条负断言写成恒真
        ——实测：种一句探针日志，brief 版照样全绿。所以这里 join 成一个字符串再比，
        并把「join 后的写法看得见**子串**」也加进捕获器自证里。
        """
        records: list[str] = []

        class _Sink(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        root = logging.getLogger()
        sink = _Sink()
        old_level = root.level
        root.addHandler(sink)
        root.setLevel(logging.DEBUG)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            # ── 捕获器自证：记一条已知串，必须被看到 ──
            logging.getLogger("probe").info("capture-power-probe")
            self.assertIn("capture-power-probe", records, "捕获器没有判别力，下面的负断言无效")
            # ── 负断言的**表达式**自证：记录里的**子串**也必须能被看见 ——
            logging.getLogger("probe").info("密码=%s", "probe-embedded-value")
            self.assertIn(
                "probe-embedded-value",
                "\n".join(records),
                "join 后的写法看不见记录里的子串，下面的负断言无效",
            )

            records.clear()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                body = self._create().json()
            password = body["data"]["initial_password"]
            # 正断言：口令确实被生成过（否则「日志里没有」可能只是没生成）
            self.assertIn(password, json.dumps(body))
            # 负断言：不得出现在任何日志记录里
            self.assertNotIn(password, "\n".join(records))
            for stream in (stdout.getvalue(), stderr.getvalue()):
                self.assertNotIn(password, stream)
        finally:
            root.removeHandler(sink)
            root.setLevel(old_level)


class AccountStatusSessionLifecycleTests(TenantTestCase):
    """§7.2-6(b)(c)：停用即 401；但停用**不是凭据吊销**（启用后旧 cookie 复活）。

    ⚠️ (c) 断言的是**现状**（spec §8-3 的残余风险），不是我们喜欢它：停用只改
    `auth_user.is_active`、不碰 `password`，而会话里的 `_auth_user_hash` 是口令哈希的
    派生值——`django_session` 那一行留着，启用回来后 `auth.get_user()` 又能把它解析回该用户。
    将来若实现改成「停用时清理该用户会话」，(c) 会变红；那时的正确动作是**改这条测试
    并同步改 RUNBOOK**，而不是把它删掉（spec §7.2-6(c) 明文要求）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)     # R5：`TenantTestCase` 自己不灌基线（要 TENANT_ADMIN）。

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="sess-admin")
        User.objects.get(username="sess-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.victim_client = make_logged_in_client(username="sess-victim")
        self.victim = User.objects.get(username="sess-victim")
        self.bystander_client = make_logged_in_client(username="sess-bystander")
        self.bystander = User.objects.get(username="sess-bystander")

    def _status(self, user, is_active):
        return self.client.post(
            f"/api/accounts/{user.pk}/status/",
            {"is_active": is_active},
            content_type="application/json",
        ).json()

    def test_disabled_session_is_rejected_while_others_still_work(self):
        """(b)：停用者已有会话的下一个请求即 401；**另一个**账号的会话仍 200。"""
        # 正向对照先跑：两条会话此刻都可用（否则下面的 401 可能由「会话压根不可用」凑出）。
        self.assertEqual(self.victim_client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.bystander_client.get("/api/auth/me").status_code, 200)

        self.assertEqual(self._status(self.victim, False)["code"], 0)
        self.assertFalse(User.objects.get(pk=self.victim.pk).is_active)   # 断言落库

        # (b) 停用者的既有 cookie 下一个请求就进不去。
        self.assertEqual(self.victim_client.get("/api/auth/me").status_code, 401)
        # 正向对照：同租户**另一个**账号的会话不受影响——否则「把服务打死」也会让 (b) 通过。
        self.assertEqual(self.bystander_client.get("/api/auth/me").status_code, 200)
        self.assertTrue(User.objects.get(pk=self.bystander.pk).is_active)

    def test_pre_disable_cookie_works_again_after_reenable(self):
        """(c)：停用 → 启用后，**停用前那条 cookie** 又能通过认证（钉住「停用 != 吊销」）。"""
        self.assertEqual(self.victim_client.get("/api/auth/me").status_code, 200)  # 前提：可用
        self.assertEqual(self._status(self.victim, False)["code"], 0)
        self.assertEqual(self.victim_client.get("/api/auth/me").status_code, 401)  # 先证真被拒
        self.assertEqual(self._status(self.victim, True)["code"], 0)
        self.assertTrue(User.objects.get(pk=self.victim.pk).is_active)             # 断言落库

        # 复活：同一条（**没有重新登录**）cookie 必须重新可认证。
        revived = self.victim_client.get("/api/auth/me")
        self.assertEqual(revived.status_code, 200, revived.content[:200])
        self.assertEqual(revived.json()["data"]["user"]["username"], "sess-victim")


class AccountPasswordValidatorTests(TenantTestCase):
    """§6.1：本线的 API 写密码路径**刻意不调用** `validate_password()`。

    该纪律只有配上「校验器确实会拒绝这种口令」的正向对照才有判别力——
    `config/settings.py` 没配 `AUTH_PASSWORD_VALIDATORS`（Django 默认空表），
    所以这里显式打开一个校验器，再证明 6 位数字口令照样能建号。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="pw-admin-2")
        User.objects.get(username="pw-admin-2").groups.add(Group.objects.get(name=TENANT_ADMIN))

    def test_weak_six_digit_password_is_not_run_through_the_validators(self):
        validators = [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}]
        with override_settings(AUTH_PASSWORD_VALIDATORS=validators):
            # 正向对照：校验器在这个配置下**确实**会拒绝 6 位口令。
            with self.assertRaises(DjangoValidationError):
                validate_password("123456")
            response = self.client.post(
                "/api/accounts/",
                data={
                    **CREATE_REQUIRED_PROFILE_FIELDS,
                    "username": "weak-pw", "role": "客服", "games": [self.game.pk],
                },
                content_type="application/json",
            )
        self.assertEqual(response.json()["code"], 0)
        # 落到库上的哈希必须真的对应那个 6 位口令（否则「没调校验器」可能是压根没写密码）。
        user = User.objects.get(username="weak-pw")
        password = response.json()["data"]["initial_password"]
        self.assertTrue(user.check_password(password))


class AccountResetPasswordTests(TenantTestCase):
    """#7 重置密码：固定口令 `123456`，且**刻意不调用** `validate_password()`。

    ⚠️ `TenantTestCase` 不跑 `seed_tenant`，而本类要 `Group.objects.get(TENANT_ADMIN)`，
    所以 `setUpClass` 必须显式灌一次（否则 `DoesNotExist`）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="rst-admin")
        User.objects.get(username="rst-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.target = User.objects.create_user(username="rst-target", password="old-pw-9999")

    def _reset(self, pk):
        return self.client.post(
            f"/api/accounts/{pk}/reset-password/", content_type="application/json"
        )

    def test_reset_sets_the_fixed_reference_password(self):
        body = self._reset(self.target.pk).json()
        self.assertEqual(body["code"], 0)
        self.assertIsNotNone(authenticate(username="rst-target", password="123456"))
        # 再把断言打在**数据库行**上（E-0009）：`authenticate()` 走的是「按用户名重查一行」，
        # 它已经能看见落库结果，但「存的是明文」这种失败形态只有比对行里的 `password` 才看得见。
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("123456"))
        # 正向对照：同一条断言在重置**前**必须为假——否则它证明不了任何东西。
        self.assertFalse(self.target.check_password("old-pw-9999"))
        self.assertNotEqual(self.target.password, "123456")   # 存的是哈希不是明文

    def test_old_password_stops_working(self):
        # 正向对照：重置**之前**旧口令必须能登录（同一用例内，证明夹具的口令对得上）。
        self.assertIsNotNone(authenticate(username="rst-target", password="old-pw-9999"))
        self._reset(self.target.pk)
        self.assertIsNone(authenticate(username="rst-target", password="old-pw-9999"))

    def test_admin_may_reset_own_password(self):
        """§9-11：管理员重置**自己**的密码是允许的，且必须是**真的**允许。

        `reset_password` 不做 `check_admin_invariant`（重置不是停用/降级），所以自己不是
        自锁。判据取「随后真的能用固定口令登进去」——只看 `code 0` 的话，一个「看起来成功
        的拒绝」也能通过。
        """
        me = User.objects.get(username="rst-admin")
        self.assertTrue(me.groups.filter(name=TENANT_ADMIN).exists())   # 夹具前提
        body = self._reset(me.pk).json()
        self.assertEqual(body["code"], 0)
        me.refresh_from_db()
        self.assertTrue(me.check_password(RESET_PASSWORD))              # 断言落库

        # 端到端：走**真实登录端点**，而不是只调 `authenticate()`（后者不证明账号可用）。
        self.assertIsNotNone(authenticate(username="rst-admin", password=RESET_PASSWORD))
        fresh = Client(SERVER_NAME=DOMAIN)
        login = fresh.post(
            "/api/auth/login",
            data={"username": "rst-admin", "password": RESET_PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])
        self.assertEqual(login.json()["code"], 0)

    def test_reset_invalidates_the_targets_existing_session(self):
        """§7.2-6(a)：**断言登录态没了（401）**，不断言会话行被删（有回退分支）。

        ⚠️ 用**独立账号**：`make_logged_in_client` 会自己建用户，与 setUp 里那个撞唯一约束。
        """
        victim = make_logged_in_client(domain="tenant.test.com",
                                       username="rst-victim", password="old-pw-9999")
        victim_user = User.objects.get(username="rst-victim")
        self.assertEqual(victim.get("/api/auth/me").status_code, 200)   # 先证会话可用
        self.client.post(f"/api/accounts/{victim_user.pk}/reset-password/",
                         content_type="application/json")
        self.assertEqual(victim.get("/api/auth/me").status_code, 401)

    def test_reset_does_not_echo_the_password(self):
        """重置响应**不回显**口令（spec §6.1）。

        ⚠️ 原先这条是 `assertNotIn("password", json.dumps(body["data"] or {}))`，两处会恒真：
        ① `data` 为 `None` 时它比的是 `{}`（信封失败时照样通过）；② 它只找 `"password"`
        这个**键名**，换个键名（`initial_password` / `pw`）或把口令塞进 `msg` 都溜得过去。
        这里改成比**整个响应体**，并补上「搜索表达式看得见口令」的正向对照。
        """
        body = self._reset(self.target.pk).json()
        self.assertEqual(body["code"], 0)
        # 正向前提：这次确实是「重置成功」的响应——否则「响应里没有口令」可能由
        # 「请求压根没生效」凑出来。
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(RESET_PASSWORD))

        # 表达式自证（正向对照）：同一段「dump 后找子串」的搜索，在**真的带口令的响应**上
        # 必须命中。用新增端点做对照——它的 `initial_password` 是 spec §6.1 允许的唯一出口。
        created = self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "echo-control", "role": "客服", "games": [self.game.pk],
            },
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0)
        echoed = created["data"]["initial_password"]
        self.assertIn(
            echoed,
            json.dumps(created, ensure_ascii=False),
            "搜索表达式看不见响应里的口令，下面的负断言无效",
        )

        # 负断言：口令字面量、以及口令形态的键名，都不出现在**整个**响应体里（含 `msg`）。
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(RESET_PASSWORD, dumped)
        self.assertNotIn("password", dumped)

    def test_reset_returns_the_documented_id_shape(self):
        """响应形状按契约钉死：`data` **恰好**是 `{"id": <目标 pk>}`。

        没有这条时，`{"id": ...}` 只是 docstring 里的一句话——改回 `None`、多带几个键
        （例如顺手把账号载荷塞进去）或回错 id 都不会有测试变红。
        """
        body = self._reset(self.target.pk).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"], {"id": self.target.pk})
        # 正向对照：这个 id 确实是**被重置的那一行**（换个账号，回的就是它的 pk）。
        other = User.objects.create_user(username="rst-other", password="old-pw-0000")
        self.assertEqual(self._reset(other.pk).json()["data"], {"id": other.pk})
        other.refresh_from_db()
        self.assertTrue(other.check_password(RESET_PASSWORD))

    def test_malformed_path_id_returns_1000_not_1002(self):
        """重置入口也要走 `_lookup_id_error`：同一个畸形 pk 不得「GET 回 1000、POST 回 1002」。

        没有这条守卫时实测分叉：`GET /api/accounts/abc/` 回 1000，
        `POST /api/accounts/abc/reset-password/` 落到 `get_object()` 上回 1002。
        正对照在同一用例内：同一个 URL 换成合法 pk 必须回 0。
        """
        for pk in ("abc", "²", "①", "Ⅰ", "-1", "+1"):
            with self.subTest(pk=pk):
                detail = self.client.get(f"/api/accounts/{pk}/")
                reset = self._reset(pk)
                self.assertEqual(reset.status_code, 200)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(reset.json()["code"], 1000)
                self.assertEqual(
                    reset.json()["code"],
                    detail.json()["code"],
                    f"pk={pk!r}：重置入口与详情入口回的不是同一个码",
                )
        self.assertEqual(self._reset(self.target.pk).json()["code"], 0)

    def test_reset_does_not_touch_other_columns(self):
        """重置只写 `password` 列：`is_active` / `is_staff` / 用户名都不被顺手写回去。

        ⚠️ **判别力口径（如实，实测）**：把 `save(update_fields=["password"])` 换成不带
        `update_fields` 的 `save()` 时，**本用例仍然全绿**——`get_object()` 取到的是刚查出来
        的当前行，整行写回写的就是那些当前值。所以这条断言证明的是「重置不会丢掉别的列」，
        **不是**「`update_fields` 那次收窄必需」；后者只有「取行与写行之间被并发改过」才看得见，
        单线程测试构造不出来（同类的真实代价由 Task 6 在编辑路径上用陈旧实例钉住）。
        留着它是因为「重置顺手把停用账号启用回来」是真实会发生的回归形态。
        """
        self.target.is_active = False
        self.target.save(update_fields=["is_active"])
        staff_before = self.target.is_staff
        self._reset(self.target.pk)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)        # 没被写回成 True
        self.assertEqual(self.target.is_staff, staff_before)
        self.assertEqual(self.target.username, "rst-target")
        # 正向对照：同一个实现确实改了**该改的那一列**——否则上面的「没变」可能由
        # 「这个请求什么都没干」凑出来。
        self.assertTrue(self.target.check_password("123456"))

    def test_fixed_password_is_not_run_through_the_validators(self):
        """spec §6.1：本端点**刻意不调用** `validate_password()`。

        判别力来自**显式打开一个会拒绝 `123456` 的校验器**：`config/settings.py` 没配
        `AUTH_PASSWORD_VALIDATORS`（Django 默认空表），那种配置下 `validate_password("123456")`
        什么都不做——所以「补一行 `validate_password`」的改法在不加这条用例时**杀不掉**
        （实测：加了那行的变异体让本类其余 7 条全绿）。与
        `AccountPasswordValidatorTests.test_weak_six_digit_password_is_not_run_through_the_validators`
        同手法；那里钉的是新增路径，这里钉的是重置路径。
        """
        validators = [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"}]
        with override_settings(AUTH_PASSWORD_VALIDATORS=validators):
            # 正向对照：校验器在这个配置下**确实**会拒绝固定口令。
            with self.assertRaises(DjangoValidationError):
                validate_password("123456")
            response = self._reset(self.target.pk)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("123456"))

    def test_reset_requires_an_account_admin(self):
        """R6：重置**不是零节点账号能过的**（同一扇准入 + 它自己的节点门，⛔ 不是新权限名）。

        ⚠️ 准入是 `IsAccountManager`（持任一账号管理节点即可，**不要求是管理员**）。
        本用例的 `outsider` 零节点 ⇒ 先拒他的是**节点门**（`system_user.reset_password`）。
        ⚠️ 另外：**目标是管理员**时还有一条业务规则
        （`guards.check_admin_target_invariant` ⇒ 1002），本用例的目标是普通客服、不触发它。
        正向对照在同一用例内：同一 URL 换成管理员发就成功——所以 403 是权限挡的，
        不是路由/载荷的问题。
        """
        outsider = make_logged_in_client(username="rst-outsider")
        response = outsider.post(
            f"/api/accounts/{self.target.pk}/reset-password/", content_type="application/json"
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("old-pw-9999"))   # 口令没被改
        self.assertEqual(self._reset(self.target.pk).json()["code"], 0)

    def test_unknown_id_returns_1002_not_500(self):
        """不存在的 id：`get_object()` → `Http404` → 1002，不是 HTML 500。"""
        response = self._reset(99999999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)

    def test_delete_is_still_not_exposed_on_the_reset_route(self):
        """任务要求 2：重置 `@action` 只注册 `POST`，`DELETE` 必须仍是 **405**（D3）。

        正向对照：同一个 URL 的 `POST` 是通的（200），所以 405 来自「没有注册 DELETE」，
        不是「路由压根不存在」（那会是 404）。
        """
        self.assertEqual(
            self.client.delete(
                f"/api/accounts/{self.target.pk}/reset-password/"
            ).status_code,
            405,
        )
        self.assertEqual(self._reset(self.target.pk).status_code, 200)


class AccountResetPasswordCrossTenantTests(TenantTestCase):
    """重置密码的跨租户判别力（AGENTS.md §4 对**新增写路径**的硬要求）。

    ⚠️ 本类要证的不是「B 的 pk 在 A 里取不到」（**不成立**：两个 schema 的 `auth_user`
    序列各自从 1 开始，`seed_tenant` 又给每个租户灌一个 `admin`，id 天然重叠），
    而是「**同号的两行各取各的 schema**」——与
    `tests/test_account_api.py::AccountCrossTenantTests` 同一手法与同一理由。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        # 第二个租户在 `setUpClass` 里建（在用例内部建 schema 会与外层事务相冲）。
        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_reset_b", name="重置密码租户 B", domain="acct-reset-b.test.local"
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        from django_tenants.utils import tenant_context

        self.game = Game.objects.create(name="王者荣耀")
        # 客户端是**系统管理员**：本类只钉「租户上下文」这一个维度，所以先把权限维度让开
        # ——否则请求先被 `IsAccountAdmin` 挡成 403，`code` 永远不是 0，下面那条
        # 「B 的行没被动过」就由 403 凑出来，判别力为零（实测：普通账号拿到 403 而不是 0）。
        # 权限维度由 `test_reset_requires_an_account_admin` 单独钉。
        self.client = make_logged_in_client(username="xreset-admin")
        User.objects.get(username="xreset-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        # `auth_user` 的自增序列与 `seed_tenant` 无关地各自推进（事务回滚**不会**回退
        # Postgres 序列），所以「B 的新账号恰好是 pk=2」不可依赖。这里**显式指派**同一个 pk：
        # A 取一个空号，B 也把账号造成那个号——重叠由夹具制造，不由巧合凑出。
        # ⚠️ pk 必须在 `create_user` 时给：建成后再 `update(id=…)` 会把
        # `auth_user_groups.user_id` 的外键悬空（实测 IntegrityError）。
        self.overlap_pk = (
            User.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        ) + 1
        with tenant_context(self.tenant_b):
            User.objects.create_user(
                pk=self.overlap_pk, username="b-reset-marker", password="b-pw-8888"
            )
        User.objects.create_user(
            pk=self.overlap_pk, username="a-reset-marker", password="a-pw-7777"
        )

    def test_reset_of_an_overlapping_pk_only_touches_this_tenants_row(self):
        response = self.client.post(
            f"/api/accounts/{self.overlap_pk}/reset-password/", content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0)
        # A 侧：本租户那一行**确实被重置了**（否则下面的 B 侧断言可能由「什么都没发生」凑出来）。
        with tenant_context(self.tenant_b):
            b_row = User.objects.get(pk=self.overlap_pk)
            self.assertEqual(b_row.username, "b-reset-marker")
            self.assertTrue(
                b_row.check_password("b-pw-8888"),
                "A 的重置请求改掉了 B 的同号行——tenant 上下文没绑在连接上",
            )
            self.assertFalse(b_row.check_password("123456"))
        a_row = User.objects.get(pk=self.overlap_pk)
        self.assertEqual(a_row.username, "a-reset-marker")
        self.assertTrue(a_row.check_password("123456"))
        self.assertFalse(a_row.check_password("a-pw-7777"))

