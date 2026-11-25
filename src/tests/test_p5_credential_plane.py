"""凭据层红线（spec §5.1）：会话必须强绑身份平面。

本文件现有三块：

- **第 1–9 条**（`CredentialPlaneTests`，Task 7 补）：跨平面重放、最恶同哈希 twin、
  缺 `identity_plane`（本租户 / 平台平面）、跨平面双向、守卫承重性反证、
  拒绝=不进入视图、停用租户。**这是 P1 挂在 P5 的那条账的验收核心。**
- **第 7 条**「认证类独立可证」（`AuthenticationClassAloneTests`）：摘掉（Task 3 才存在的）
  守卫中间件后，`TenantBoundAuthentication` 必须**自己**拒绝跨平面会话。
- **第 16 条**「正常用户各自登录（防回归）」（`SharedCredentialNormalLoginTests`）：
  Task 5 评审发现实现侧漏写，已补在此处。它与第 7 条互补——只有"漏洞被堵住"，
  没有"正常登录还能用"，等于把服务打死也算通过。
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.platform.models import Tenant
from apps.platform.provisioning import provision_tenant

DOMAIN_A = "plane-a.test.local"
DOMAIN_B = "plane-b.test.local"
DOMAIN_DUP_A = "dup-a.test.local"
DOMAIN_DUP_B = "dup-b.test.local"

# 平台（public）平面域名。与 `apps/platform/bootstrap.py::PLATFORM_DOMAIN` 同一个值——
# 这里**独立抄写**而不是 import：本文件断言的正是"平台域名被路由到 public 平面且
# 跨平面会话在那里也被拒"，从实现 import 会让"实现改了默认域名"这件事静默通过。
PLATFORM_DOMAIN = "admin.test.local"

# spec §5.1-16 的构造物：两个租户里**同名 + 同密码文本**的用户。
SHARED_USERNAME = "kefu"
SHARED_PASSWORD = "pw-same-text"

# spec §5.1-9 的两个已认证构造物（各用一条独立测试：一个走守卫、一个走认证类）。
# 故意用**不同**用户名：两条测试各自 `create_user`，同一个 schema 里同名会撞唯一约束。
DISABLED_USER_ON_GUARD = "disabled-guard-user"
DISABLED_USER_ON_AUTH_CLASS = "disabled-authclass-user"


def materialize_session(schema, *, user_pk, password_hash, plane):
    """直接写一条会话（`schema` 只是签名的一部分，见下）。

    ⚠️ **`schema_context(schema)` 决定不了会话行落在哪个 schema**（Task 7 评审纠偏）：
    `django.contrib.sessions` 是 `SHARED_APPS` 成员（`config/settings.py:48`），
    所以 `django_session` **只存在于 public**，而 `TenantMainMiddleware` 走的
    `set_tenant(..., include_public=True)`（`django_tenants/postgresql_backend/base.py:96-104`）
    又把 public 留在 search_path 上——本函数无论传哪个 schema，会话行都写进 public。
    **别为了"把会话写进目标 schema"去改这里的 schema 处理，那是白费力。**

    真正决定判别力的是 `_auth_user_hash` 怎么算：`password_hash` 传的是**目标用户
    自己的 `password` 字段**（调用方写 `user.password`），会话里存的必须是它的 **HMAC**。
    这条构造对应 spec §5.1-2「最恶情形」：B 里站一个与 A **哈希逐字节相同**的 twin，
    于是 Django 的校验**会通过**、`get_user()` 真的把我们当成 twin 认出来——能拒的
    只剩平面不变式。若改用"A 登录后整体重放"，两边的 `password` 不同 → HMAC 对不上 →
    Django 自己就 `session.flush()` + 匿名
    （`django/contrib/auth/__init__.py:330-334`），测试将**零判别力**。
    反过来说，把 `password` 字段**原样**塞进 `_auth_user_hash` 也一样：
    Django 会**替**我们拒绝，所有用本函数构造的红线测试恒绿（spec §5.1-2 的警告正是
    这条；P5 计划给的本函数原样就有这个缺陷，实测见 task-2-report）。
    这里用 Django **自己的 API**（`get_session_auth_hash()`）推导该值，不复制它的
    `key_salt`（签名函数见 `django/contrib/auth/base_user.py:145-152`）。

    定义在模块层，供本文件里所有测试类复用（Task 7 直接调用它）。
    """
    from django.contrib.sessions.backends.db import SessionStore
    session_auth_hash = get_user_model()(
        pk=user_pk, password=password_hash
    ).get_session_auth_hash()
    with schema_context(schema):
        session = SessionStore()
        session["_auth_user_id"] = str(user_pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = session_auth_hash
        session["identity_plane"] = plane
        session.save()
    return session.session_key


class AuthenticationClassAloneTests(TenantTestCase):
    """摘掉守卫中间件后，认证类必须自己拒绝跨租户会话（spec D4/§5.1-7）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.conf import settings
        settings.ALLOWED_HOSTS += [DOMAIN_A, DOMAIN_B]
        cls.tenant_a, _ = provision_tenant(schema_name="plane_a", name="A", domain=DOMAIN_A)
        cls.tenant_b, _ = provision_tenant(schema_name="plane_b", name="B", domain=DOMAIN_B)

    @override_settings(
        ROOT_URLCONF="tests.p5_probe_urls",
        MIDDLEWARE=[
            "django_tenants.middleware.main.TenantMainMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ],
    )
    def test_auth_class_rejects_session_from_another_plane(self):
        # 攻击场景的构造物必须**写死**（spec §5.2-5）：目标 schema 里先站一个与 A
        # `password` 字段**逐字节相同**的同主键用户（twin）。少了它，Django 自己的
        # 会话哈希兜底就会替我们返回 401 —— 那条断言恒绿、零判别力（E-0012 的教训：
        # 触发条件是「哈希字符串相同」，不是「密码相同」）。
        with tenant_context(self.tenant_a):
            user_a = get_user_model().objects.create_user(username="twin", password="pw")
            hash_a = user_a.password
        with tenant_context(self.tenant_b):
            user_b = get_user_model().objects.create_user(username="twin", password="other")
            user_b.password = hash_a          # 逐字节覆盖：模拟种子/夹具复用同一份哈希
            user_b.save(update_fields=["password"])
            user_b.refresh_from_db()
            twin_pk, twin_hash = user_b.pk, user_b.password
        self.assertEqual(
            hash_a, twin_hash, "构造失败：两个租户的 password 字段必须逐字节相同"
        )

        # 会话在**目标 schema（B）内**materialize，但它声明自己是 A 平面的人。
        # 同一条会话先做**对照**（平面声明 = B）：必须 200。
        # 没有这个对照，万一构造物退化成"会话在 B 里根本认不出用户"，
        # 下面断言 401 会因为**另一种原因**通过（E-0003 的教训：状态码可能来自别处）。
        control_session = materialize_session(
            self.tenant_b.schema_name,
            user_pk=twin_pk,
            password_hash=twin_hash,
            plane=self.tenant_b.schema_name,
        )
        control_client = Client(SERVER_NAME=DOMAIN_B)
        control_client.cookies["sessionid"] = control_session
        control = control_client.get("/__probe__/whoami")
        self.assertEqual(
            control.status_code,
            200,
            f"对照失败：B 自己的会话都没被认出来，本条测试没有判别力"
            f"（{control.status_code}：{control.content[:200]!r}）",
        )
        self.assertEqual(control.json()["username"], "twin")

        # 攻击形态：同一条会话、同一个 twin，只把平面声明换成 A。
        session_id = materialize_session(
            self.tenant_b.schema_name,
            user_pk=twin_pk,
            password_hash=twin_hash,
            plane=self.tenant_a.schema_name,   # 平面声明是 A，而请求落在 B
        )

        client = Client(SERVER_NAME=DOMAIN_B)
        client.cookies["sessionid"] = session_id
        response = client.get("/__probe__/whoami")      # 受保护的探针端点（Task 5 之前唯一可用的）
        self.assertEqual(
            response.status_code,
            401,
            f"跨平面会话必须被拒；实际 {response.status_code}：{response.content[:200]!r}",
        )
        self.assertEqual(response.json()["code"], 401)
        # 401 必须带 WWW-Authenticate：DRF 在认证头为空时会把 401 **降级成 403**
        # （rest_framework/views.py:458-466），而 AuthenticationFailed 也走这条降级路径。
        # 这条断言钉住 `authenticate_header` 真的存在。
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Session")


class GuardSmokeTests(TenantTestCase):
    """守卫装上后，公开流量不能被打死（否则会为了安全把服务弄挂）。

    守卫的**行为**红线测试（缺 plane、跨平面、短路、停用租户）全部在 Task 7，
    因为那时 `/api/auth/*` 与平台域名才存在。

    本类走**真实** `MIDDLEWARE` 与真实 `ROOT_URLCONF`（`/healthz` 是既有路由），
    不做 `override_settings`：只有跑在守卫**实际接线**的栈上，这条才有判别力。
    它盯的是一类很具体的写坏方式——把平面判定写成 `plane != connection.schema_name`
    而漏掉 `"_auth_user_id" in session` 前置条件时，匿名请求的 plane 是 `None`，
    于是**每个**匿名请求都会被判 401，公开流量（存活探针、登录页）全灭。
    """

    def test_anonymous_health_check_still_passes(self):
        response = Client(SERVER_NAME="tenant.test.com").get("/healthz")
        self.assertEqual(response.status_code, 200)


class SharedCredentialNormalLoginTests(TenantTestCase):
    """spec §5.1-16（防回归）：跨租户**同名同密码**的两个用户各自从真实入口登录。

    为什么必须有这一条：§5.1-2 只证明"跨平面会话被拒"，一个把所有人都登不进去的
    实现同样能过它。两条合起来才把「修好的漏洞」与「被打死的功能」分开。

    与 §5.1-2 的差别是**全程走真实入口**：真 `POST /api/auth/login`（不是
    `materialize_session` 手工造会话）、真 `GET /api/auth/me`，且跑在真实
    `MIDDLEWARE` / `ROOT_URLCONF` 上（守卫中间件与 `TenantBoundAuthentication` 都在），
    不做 `override_settings`——`TenantTestCase.setUpClass` **不调** `super().setUpClass()`
    （`django_tenants/test/cases.py`），类级 `@override_settings` 会被静默丢弃。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_DUP_A, DOMAIN_DUP_B) if d not in settings.ALLOWED_HOSTS
        ]
        # ⚠️ 同样**不能**写 `cls.tenant = ...`：基类 tearDownClass 用它删自己建的
        # 共享 `test` 租户（详见 tests/test_p5_envelope_csrf.py 里的同类注释）。
        cls.tenant_a, _ = provision_tenant(
            schema_name="dup_a", name="同名A", domain=DOMAIN_DUP_A
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="dup_b", name="同名B", domain=DOMAIN_DUP_B
        )
        with tenant_context(cls.tenant_a):
            cls.user_a = get_user_model().objects.create_user(
                username=SHARED_USERNAME, password=SHARED_PASSWORD
            )
            cls.hash_a = cls.user_a.password
        with tenant_context(cls.tenant_b):
            # 占位用户：每个 schema 的用户表**各自独立编号**，且 `seed_tenant` 在每个
            # 租户都建了 pk=1 的 admin——不插占位，两边的用户 pk 会**相同**，
            # 下面"看不到对方的 id"就会变成 2 != 2 直接失败。让 pk 不同，
            # 这条断言才既成立又有判别力。
            get_user_model().objects.create_user(username="filler", password="pw-filler")
            cls.user_b = get_user_model().objects.create_user(
                username=SHARED_USERNAME, password=SHARED_PASSWORD
            )
            cls.hash_b = cls.user_b.password

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for domain in (DOMAIN_DUP_A, DOMAIN_DUP_B):
            if domain in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.remove(domain)

    def _real_login(self, domain):
        """D15 的真实流程：先取 `csrftoken` cookie，再带 `X-CSRFToken` 头登录。

        `enforce_csrf_checks=True` 不可省（Task 5 评审 Finding 5）：默认 client 会把
        `request._dont_enforce_csrf_checks` 置 True（`django/test/client.py:183`），
        而 `csrf_protect` 读同一个 flag（`django/middleware/csrf.py:427`）——
        用默认 client，"登录成功"可能是在**零 CSRF 校验**下发生的，断言恒真。
        """
        client = Client(SERVER_NAME=domain, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/auth/login",
            data={"username": SHARED_USERNAME, "password": SHARED_PASSWORD},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        return client, response

    def test_same_username_and_password_in_two_tenants_each_lands_on_its_own_plane(self):
        # 构造物先自证：同名同密码，但哈希必须不同（§5.1-16 明写"哈希不同"）。
        # 哪天有人把 hasher 换成不带 salt 的，本条就退化成 §5.1-2 的攻击场景。
        self.assertNotEqual(
            self.hash_a, self.hash_b, "构造失败：同密码文本的两个哈希必须不同"
        )
        self.assertNotEqual(
            self.user_a.pk,
            self.user_b.pk,
            "构造失败：两个用户的 pk 必须不同，否则'看不到对方的 id'没有判别力",
        )

        client_a, login_a = self._real_login(DOMAIN_DUP_A)
        self.assertEqual(login_a.status_code, 200, login_a.content[:200])
        payload_a = login_a.json()["data"]["user"]
        self.assertEqual(payload_a["username"], SHARED_USERNAME)
        self.assertEqual(payload_a["id"], self.user_a.pk)
        self.assertEqual(payload_a["identity_plane"], self.tenant_a.schema_name)
        self.assertNotEqual(payload_a["id"], self.user_b.pk)
        self.assertNotEqual(payload_a["identity_plane"], self.tenant_b.schema_name)
        # `/me` 必须原样复述同一个身份：同一个会话、同一个平面。
        me_a = client_a.get("/api/auth/me")
        self.assertEqual(me_a.status_code, 200, me_a.content[:200])
        self.assertEqual(me_a.json()["data"]["user"], payload_a)

        client_b, login_b = self._real_login(DOMAIN_DUP_B)
        self.assertEqual(login_b.status_code, 200, login_b.content[:200])
        payload_b = login_b.json()["data"]["user"]
        self.assertEqual(payload_b["username"], SHARED_USERNAME)
        self.assertEqual(payload_b["id"], self.user_b.pk)
        self.assertEqual(payload_b["identity_plane"], self.tenant_b.schema_name)
        self.assertNotEqual(payload_b["id"], self.user_a.pk)
        self.assertNotEqual(payload_b["identity_plane"], self.tenant_a.schema_name)
        me_b = client_b.get("/api/auth/me")
        self.assertEqual(me_b.status_code, 200, me_b.content[:200])
        self.assertEqual(me_b.json()["data"]["user"], payload_b)


class CredentialPlaneTests(TenantTestCase):
    """凭据层红线 1–9（spec §5.1）：**P1 挂在 P5 的那条账的验收核心**。

    「以租户 A 的凭据访问 B 的数据必须失败」（`AGENTS.md` §4）在 P1 只证明了数据层，
    凭据层一直推迟到这里。本类的每条都对应 `docs/specs/2026-09-20-p5-auth-and-permissions-design.md`
    §5.1 的一个编号，构造方式照 §5.2-5「把攻击场景的构造物写死」执行。

    三条必须照做的裁决：

    1. **不要在本类上加类级 `@override_settings`**：`TenantTestCase.setUpClass`
       （`django_tenants/test/cases.py:33-46`）**不调用** `super().setUpClass()`，
       而类级 override 是在 `SimpleTestCase.setUpClass` 里生效的（`django/test/testcases.py`）
       ——于是它被**静默丢弃**。需要探针路由/中间件栈的（第 6、8 条）逐个加**方法级**装饰器。
    2. **绝不要写 `cls.tenant = ...` / `cls.domain = ...`**：基类 `tearDownClass` 正是
       用这两个属性删它自己建的共享 `test` 租户与 schema（`django_tenants/test/cases.py:49-56`）。
       覆盖 = 共享 schema 再也不会被重建 + 下个子类撞唯一约束（`docs/ERRORS.md` E-0015，
       Task 5/6 各踩一次）。额外租户一律存不同名字的属性。
    3. **第 9 条按 §4.6 的显式例外断言**：停用租户由守卫拦住（含匿名），msg 是守卫那句，
       且**不带** `WWW-Authenticate`——它不是认证挑战。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_A, DOMAIN_B, PLATFORM_DOMAIN)
            if d not in settings.ALLOWED_HOSTS
        ]
        cls.tenant_a, _ = provision_tenant(
            schema_name="plane_a", name="A", domain=DOMAIN_A
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="plane_b", name="B", domain=DOMAIN_B
        )

    def setUp(self):
        super().setUp()
        # 平台平面（public）今天**没有 Host**（P1 的缺口，`bootstrap.py` 补上）：
        # 第 4/5 条要打平台域名，必须先让 `TenantMainMiddleware` 能把它解析到 public。
        #
        # ⚠️ 必须放在 **`setUp`（测试事务内）**，不能放进 `setUpClass`：`setUpClass` 的写入
        # 在事务之外、会被**提交**并活到整个测试进程结束，而
        # `tests/test_provisioning.py::ProvisioningTests.test_provision_rejects_public_schema`
        # 断言的正是"public tenant 行不存在"——放 `setUpClass` 会把它打红
        # （实测踩到：全量跑出 `AssertionError: True is not false`，而单跑本模块全绿）。
        # 放进 `setUp` 就随每个用例的事务一起回滚，对外零污染。
        from apps.platform.bootstrap import ensure_public_tenant

        ensure_public_tenant(PLATFORM_DOMAIN)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for domain in (DOMAIN_A, DOMAIN_B, PLATFORM_DOMAIN):
            if domain in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.remove(domain)

    def assert_guard_rejected(self, response):
        """断言这次 401 是**守卫短路**产出的，不是认证类或权限类兜住的。

        为什么不能只看状态码（E-0003 的教训：状态码可能由别的原因产生）：
        本项目有**两道**平面防线——`TenantSessionGuardMiddleware`（中间件，先跑）与
        `TenantBoundAuthentication`（DRF 认证类）——两者都产出 401、`msg` 还**逐字相同**
        （都是 `会话与当前租户不匹配`）。于是"守卫真的短路了"与"守卫失效、认证类兜住了"
        在只看状态码时**完全同形**，本文件里一大半测试会因此丧失判别力。

        spec §4.6 把两者的可观测差别写死了：认证类的 401 **带**
        `WWW-Authenticate: Session`（`TenantBoundAuthentication.authenticate_header`），
        守卫的 401 **不带**（它不是"认证挑战"，是平面/生命周期守卫的拒绝）。
        所以"无这个头 + 守卫那句 msg"就是"守卫承重"的唯一可观测证据。
        """
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(response.json()["code"], 401)
        self.assertEqual(response.json()["msg"], "会话与当前租户不匹配")
        self.assertIsNone(
            response.headers.get("WWW-Authenticate"),
            "401 带了 WWW-Authenticate：说明拒绝来自认证类（DRF 路径），"
            "而不是守卫——守卫应该已经短路，请求根本不该走到 DRF（spec §4.3）",
        )

    def _session_client(self, session_id, domain):
        """把手工 materialize 的会话装进 client。

        `enforce_csrf_checks=True` 不是可选项：默认 `Client` 会把
        `request._dont_enforce_csrf_checks` 置 True（`django/test/client.py:183`），
        而 `CsrfViewMiddleware.process_view` 读同一个 flag
        （`django/middleware/csrf.py:427`）——用默认 client 时"CSRF 这一关"是关掉的，
        拒绝理由就不再唯一（E-0003）。
        """
        client = Client(SERVER_NAME=domain, enforce_csrf_checks=True)
        client.cookies["sessionid"] = session_id
        return client

    # ── 1：A 的会话 → B 域名，打真实受保护 view ──────────────────────
    def test_1_session_from_a_is_rejected_on_b(self):
        """spec §5.1-1：跨租户重放 → 401，且响应体不得泄露 B 的任何标识数据。"""
        with tenant_context(self.tenant_a):
            user = get_user_model().objects.create_user(username="a-boss", password="pw")
            user_pk, pw_hash = user.pk, user.password
        # 会话在**目标 schema（B）内** materialize：这是 §5.1-2 的纪律，第 1 条也照做。
        # 若改成"A 登录后整体重放 cookie"，Django 自己的 HMAC 兜底就会替我们返回 401
        # （HMAC 的输入含 `user.password` 与本条会话的 `_auth_user_hash`），
        # 这条测试会**零判别力**地恒绿（见 `materialize_session` 的 docstring）。
        session_id = materialize_session(
            self.tenant_b.schema_name, user_pk=user_pk,
            password_hash=pw_hash, plane=self.tenant_a.schema_name,
        )
        response = self._session_client(session_id, DOMAIN_B).get("/api/auth/me")
        self.assert_guard_rejected(response)
        # 泄露断言是独立的第二半：只断 401 的话，"守卫把请求打死了但下游已经把
        # 对方数据塞进响应"这种形态照样绿。
        self.assertNotIn("a-boss", response.content.decode())
        self.assertNotIn("plane_a", response.content.decode())

    # ── 2：最恶情形（B 里的 twin 与 A 逐字节同哈希）────────────────
    def test_2_worst_case_twin_with_identical_password_hash_is_rejected(self):
        """spec §5.1-2 / §3.3 情形 2：两个租户的 `password` 字段**逐字节相同**时，
        Django 自己的会话哈希兜底**不会**替你拒绝——只有平面判定能。

        构造物（§5.2-5 要求写死）：B 里站一个与 A **哈希逐字节相同**的 twin，
        会话在 **B 的 schema 内** materialize，`_auth_user_id` = B 的 twin pk，
        而 `identity_plane` 声明是 A。这样 Django 的 `get_user()` 会**成功**认出
        twin（下面的对照请求把这一点钉死），唯一还能拒的就是平面不变式。
        """
        with tenant_context(self.tenant_a):
            user_a = get_user_model().objects.create_user(username="twin", password="pw")
            hash_a = user_a.password
        with tenant_context(self.tenant_b):
            user_b = get_user_model().objects.create_user(username="twin", password="other")
            user_b.password = hash_a                     # 逐字节相同
            user_b.save(update_fields=["password"])
            user_b.refresh_from_db()
            pk_b, hash_b = user_b.pk, user_b.password
        self.assertEqual(hash_a, hash_b, "构造失败：两边哈希必须逐字节相同")

        # 对照（构造物自证，spec §5.2-4/5）：同一条会话、只把平面声明换成 B
        # → 必须 200 且就是 B 的 twin。这一步证明"最恶情形"真的成立了：
        # Django 的会话哈希机制**接受**这条会话，唯一还会拒的就是平面不变式。
        # 没有这个对照，构造退化（例如 twin 没被认出来）会让下面的 401
        # 以**另一种原因**通过（E-0003：状态码可能来自别处）。
        #
        # ⚠️ 这里**不断言** A 的 twin 与 B 的 twin 同主键：Postgres 的序列不回滚，
        # 而测试方法里的插入逐个消耗序列值，两个 schema 的 `auth_user` 序列早已
        # 各走各的（实测 4 != 3）。"同 pk"在 spec 里说的是**主键空间相同**这件事
        # （`_auth_user_id` 只是一个裸整数，不带 schema），不是本用例能钉住的前提。
        control_session = materialize_session(
            self.tenant_b.schema_name, user_pk=pk_b, password_hash=hash_b,
            plane=self.tenant_b.schema_name,
        )
        control = self._session_client(control_session, DOMAIN_B).get("/api/auth/me")
        self.assertEqual(
            control.status_code,
            200,
            f"对照失败：B 自己的 twin 会话都没被认出来，本条没有判别力"
            f"（{control.status_code}：{control.content[:200]!r}）",
        )
        self.assertEqual(control.json()["data"]["user"]["username"], "twin")

        session_id = materialize_session(
            self.tenant_b.schema_name, user_pk=pk_b, password_hash=hash_b,
            plane=self.tenant_a.schema_name,
        )
        response = self._session_client(session_id, DOMAIN_B).get("/api/auth/me")
        self.assert_guard_rejected(response)

    # ── 3/4：缺 plane 的会话（本租户 / 平台平面）──────────────────
    def test_3_session_without_plane_is_rejected_on_its_own_tenant(self):
        """spec §5.1-3：`identity_plane` 缺失 → fail-closed 一律拒（D2）。

        这条会话里的用户在 A 里**确实存在**（会话就在 A 里 materialize），所以
        "缺 plane 就跳过判定"的弱写法会让它 200 通过——判别力来自构造，不是来自状态码。
        """
        with tenant_context(self.tenant_a):
            user = get_user_model().objects.create_user(username="n1", password="pw")
            user_pk, pw_hash = user.pk, user.password
        session_id = materialize_session(
            self.tenant_a.schema_name, user_pk=user_pk, password_hash=pw_hash, plane=None
        )
        response = self._session_client(session_id, DOMAIN_A).get("/api/auth/me")
        self.assert_guard_rejected(response)

    def test_4_session_without_plane_is_rejected_on_platform_plane(self):
        """spec §5.1-4 / §5.3 S7：弱守卫下这条会话会变成 **public 的平台账号**。
        这是本设计最恶的失效模式——越权者拿到的不是"另一个租户"，而是**平台**。
        """
        with tenant_context(self.tenant_a):
            user = get_user_model().objects.create_user(username="n2", password="pw")
            user_pk, pw_hash = user.pk, user.password

        # 攻击构造物必须写死（§5.2-5，与第 2 条同一纪律）：public 平面里放一个
        # **同主键、同 `password` 字段字节**的"平台账号"。少了它，这条缺 plane 的会话
        # 在 public 平面**根本认不出用户**，401 会由"Django 找不到用户"产生——
        # 弱守卫（`plane is not None and ...`）下照样绿，本条就零判别力（E-0003）。
        with schema_context("public"):
            twin = get_user_model()(pk=user_pk, username="platform")
            twin.password = pw_hash                      # 逐字节相同
            twin.is_staff = True
            twin.is_superuser = True
            twin.save()

        # 对照（构造物自证）：同一条会话、只把平面声明换成 public → 必须 200 且
        # 就是上面那个平台账号。没有这个对照，"public 里没有这个用户"这种构造退化
        # 会让下面的 401 以**错误的理由**通过。
        control_session = materialize_session(
            "public", user_pk=user_pk, password_hash=pw_hash, plane="public"
        )
        control = self._session_client(control_session, PLATFORM_DOMAIN).get("/api/auth/me")
        self.assertEqual(
            control.status_code,
            200,
            f"对照失败：public 平面里同主键的平台账号都没被认出来，"
            f"本条没有判别力（{control.status_code}：{control.content[:200]!r}）",
        )
        self.assertEqual(control.json()["data"]["user"]["username"], "platform")

        session_id = materialize_session(
            self.tenant_a.schema_name, user_pk=user_pk, password_hash=pw_hash, plane=None
        )
        response = self._session_client(session_id, PLATFORM_DOMAIN).get("/api/auth/me")
        self.assert_guard_rejected(response)

    # ── 5：platform ↔ tenant 双向（spec §5.1-5）──────────────────
    def test_5_cross_plane_replay_is_rejected_both_ways(self):
        with schema_context("public"):
            platform_user = get_user_model().objects.create_superuser(
                username="platform", password="pw"
            )
            p_pk, p_hash = platform_user.pk, platform_user.password
        with tenant_context(self.tenant_a):
            tenant_user = get_user_model().objects.create_user(username="a-user", password="pw")
            t_pk, t_hash = tenant_user.pk, tenant_user.password

        # (a) public 平面的会话 → 租户 B 的域名
        sid_public = materialize_session(
            "public", user_pk=p_pk, password_hash=p_hash, plane="public"
        )
        response_ab = self._session_client(sid_public, DOMAIN_B).get("/api/auth/me")
        self.assert_guard_rejected(response_ab)
        # 两个方向都要断言"没泄露对方平面"，否则"两个方向都被拒"里可能有一个是
        # 因为别的原因（例如域名解析失败）被拒的（E-0003）。
        self.assertNotIn("public", response_ab.content.decode())

        # (b) 租户 A 的会话 → 平台域名
        sid_tenant = materialize_session(
            self.tenant_a.schema_name, user_pk=t_pk, password_hash=t_hash,
            plane=self.tenant_a.schema_name,
        )
        response_ba = self._session_client(sid_tenant, PLATFORM_DOMAIN).get("/api/auth/me")
        self.assert_guard_rejected(response_ba)
        self.assertNotIn("a-user", response_ba.content.decode())

    # ── 6：negative control（守卫是承重件）───────────────────────
    @override_settings(
        ROOT_URLCONF="tests.p5_probe_urls",
        # 方法级（不是类级）：类级 override 会被 `TenantTestCase.setUpClass`
        # 静默丢弃（它不调 `super().setUpClass()`，见类 docstring 第 1 条）。
        # 这条 MIDDLEWARE 里**刻意不装** `apps.core.middleware.TenantSessionGuardMiddleware`。
        MIDDLEWARE=[
            "django_tenants.middleware.main.TenantMainMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ],
    )
    def test_6_without_the_guard_a_plain_view_sees_the_other_tenants_user(self):
        """spec §5.1-6：**守卫是承重件**的反证（negative control）。

        这条测试的命题是反的：它**要求**摘掉守卫后越权**成功**（裸 Django 视图看到 B 的用户），
        以此证明上面那些 401 不是"随便什么都会 401"的假绿——探针确实有能力看到 B 的身份，
        是守卫把它挡住的。

        判别力在哪：如果 `plain_whoami` 哪天被写成 DRF 视图，
        `TenantBoundAuthentication` 会替我们把这条跨平面会话挡掉（响应变成 401 信封），
        这条测试立刻变红——而那正是"摘掉守卫这个实验证明不了任何事"的形态
        （实测：把探针改成 `@api_view` + `IsAuthenticated` 后，本测试
        `KeyError: 'auth'` 报错）。
        """
        with tenant_context(self.tenant_b):
            user = get_user_model().objects.create_user(username="b-target", password="pw")
            user_pk, pw_hash = user.pk, user.password
        session_id = materialize_session(
            self.tenant_b.schema_name, user_pk=user_pk, password_hash=pw_hash,
            plane=self.tenant_a.schema_name,          # 平面声明是 A，而请求落在 B
        )
        body = self._session_client(session_id, DOMAIN_B).get("/__probe__/plain-whoami").json()
        # 没有守卫，裸 Django 视图看到的就是 B 的用户、B 的 schema。
        self.assertTrue(body["auth"])
        self.assertEqual(body["username"], "b-target")
        self.assertEqual(body["pk"], user_pk)
        # 断言**选中的 schema**（§5.2-4）：只看 `auth: True` 的话，"请求其实落在 A"
        # 也能绿，而本条的题目正是"Host 解析到 B、守卫缺位时 B 的身份被看到"。
        self.assertEqual(body["conn_schema"], self.tenant_b.schema_name)

    # ── 8：拒绝 = 视图没执行（状态码 + 副作用双断言）──────────────
    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_8_rejected_request_does_not_execute_the_view(self):
        """spec §5.1-8 / §4.3：拒绝的语义是"**不进入视图**"，不是"先进入再让下游拒绝"。

        只断状态码会漏掉"视图已经以对方身份跑完、只是响应被换成 401"——所以这里
        再查一次库：`probe-write` 那个 Group（副作用载体）**必须不存在**。

        两条**必须**的构造物，少了任何一条这条红线都会退化成摆设：

        1. **带合法 CSRF token**：DRF 对**已认证**的 POST 会跑
           `SessionAuthentication.enforce_csrf`（`rest_framework/authentication.py:117-133`），
           裸 POST 会被 CSRF 挡成 403——那样"视图没执行"就有了第二个解释，
           副作用断言不再是唯一红线（E-0003：症状可能来自别处）。给足 token 后，
           唯一还能拦住这次写入的就是平面不变式。
        2. **对照请求**：同一个接口 + 同一个 token，换成平面声明正确的 B 会话 →
           必须 200 且 Group 真的写进去了。没有它，副作用载体写坏（或 token 无效）时
           `assertFalse` 恒真。
        """
        with tenant_context(self.tenant_b):
            user = get_user_model().objects.create_user(username="w1", password="pw")
            user_pk, pw_hash = user.pk, user.password
        session_id = materialize_session(
            self.tenant_b.schema_name, user_pk=user_pk, password_hash=pw_hash,
            plane=self.tenant_a.schema_name,
        )
        # 攻击者拿得到 token：它就是同一个域上的一次 `GET /api/auth/csrf`（匿名也放行）。
        bootstrap = Client(SERVER_NAME=DOMAIN_B)
        bootstrap.get("/api/auth/csrf")
        token = bootstrap.cookies["csrftoken"].value

        # 对照：平面声明正确的会话 → 写成功（证明载体与 token 都真的有效）。
        legit_session = materialize_session(
            self.tenant_b.schema_name, user_pk=user_pk, password_hash=pw_hash,
            plane=self.tenant_b.schema_name,
        )
        legit = self._session_client(legit_session, DOMAIN_B)
        legit.cookies["csrftoken"] = token
        control = legit.post("/__probe__/write", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(
            control.status_code,
            200,
            f"对照失败：本平面的写请求都没成功，副作用断言没有判别力"
            f"（{control.status_code}：{control.content[:200]!r}）",
        )
        with tenant_context(self.tenant_b):
            self.assertTrue(Group.objects.filter(name="probe-write").exists())
            Group.objects.filter(name="probe-write").delete()   # 清掉对照留下的副作用

        # 攻击形态：同一条写接口、同一个 token，只有会话的平面声明是 A。
        client = self._session_client(session_id, DOMAIN_B)
        client.cookies["csrftoken"] = token
        response = client.post("/__probe__/write", HTTP_X_CSRFTOKEN=token)
        self.assert_guard_rejected(response)
        with tenant_context(self.tenant_b):
            # 用"按具体行"的断言（§5.2-3：不要用 count()），且必须**在 B 的 schema 内**
            # 查——写到别的 schema 去的话这里的否定断言会恒真。
            self.assertFalse(
                Group.objects.filter(name="probe-write").exists(),
                "视图被执行了——守卫必须短路（spec §4.3）",
            )

    # ── 9：停用租户 ─────────────────────────────────────────────
    def _disabled_guard_settings(self):
        """从真实 `settings.MIDDLEWARE` 里**摘掉**守卫的那份栈（只用于 9b 的短路自证）。"""
        return [m for m in settings.MIDDLEWARE if "TenantSessionGuard" not in m]

    def _real_login(self, domain, username, password):
        """真实 `POST /api/auth/login`（照 `SharedCredentialNormalLoginTests._real_login`）。

        必须在租户**还活着**的时候调用：`/api/auth/csrf`（取 token 用）本身也被守卫保护，
        租户一停用连 CSRF 引导都 401，token 根本拿不到（实测 `KeyError: 'csrftoken'`）。
        """
        client = Client(SERVER_NAME=domain, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        response = client.post(
            "/api/auth/login",
            data={"username": username, "password": password},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        return client, response

    def _assert_successful_login(self, login):
        """构造物自证：登录 200 + 平面 == 当前 schema。

        `identity_plane` 这一条同时证明"平面判定不会先短路"（会话平面与请求 schema 相同），
        于是下面那个 401 只可能是 `is_active` 分支产出的。
        """
        self.assertEqual(
            login.status_code,
            200,
            f"构造失败：租户还活着都登不进去，本条没有判别力"
            f"（{login.status_code}：{login.content[:200]!r}）",
        )
        self.assertEqual(
            login.json()["data"]["user"]["identity_plane"],
            self.tenant_a.schema_name,
            "构造失败：会话必须绑在 plane_a 平面上",
        )

    def _deactivate_tenant_a(self):
        """停用 `plane_a`：目录表（Tenant）属于 public，必须显式切进去。"""
        with schema_context("public"):
            tenant = Tenant.objects.get(schema_name=self.tenant_a.schema_name)
            tenant.is_active = False
            tenant.save(update_fields=["is_active"])

    def _reactivate_tenant_a(self):
        with schema_context("public"):
            tenant = Tenant.objects.get(schema_name=self.tenant_a.schema_name)
            tenant.is_active = True
            tenant.save(update_fields=["is_active"])

    def test_9_disabled_tenant_is_rejected_even_when_anonymous(self):
        """spec §5.1-9 的**匿名**半边 + §4.6 的**显式例外**：停用租户上的匿名请求被守卫拒，
        msg 是守卫那句，且**不带** `WWW-Authenticate`。

        ⚠️ brief 给的原样（裸 `Tenant.objects.get(...)` + `tenant.save()`）在本项目**必抛**：
        `TenantMixin.save` 对**已存在**的租户要求连接停在"它自己的 schema"或 public
        （`django_tenants/models.py:109-112`），而 `TenantTestCase.setUpClass` 把连接停在
        共享 `test` schema 上（`django_tenants/test/cases.py:46`）——两者都不是。
        见 `_deactivate_tenant_a`。
        """
        self._deactivate_tenant_a()
        try:
            # 完全匿名的请求（连 sessionid cookie 都没有）：`/healthz` 自己有 200 的能力，
            # 于是这个 401 只可能来自守卫的 `is_active` 分支——不是"因为没登录"。
            response = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True).get("/healthz")
            self.assert_guard_rejected(response)
        finally:
            self._reactivate_tenant_a()

    def test_9b_disabled_tenant_rejects_a_previously_valid_session(self):
        """spec §5.1-9 的**已认证**半边（守卫态）：租户活着时真实登录，停用后必须马上失效。

        为什么要单独一条：第 9 条的匿名请求**根本没有会话**，钉不住守卫的
        `"_auth_user_id" in session` 那一支；而 spec §5.1-9 的原文正是
        "已认证会话 + **取不到任何业务数据**"。

        会话必须**先于**停用取得（见 `_real_login`）。这条会话本身由守卫的 `is_active`
        分支拒掉，所以 `assert_guard_rejected`（无 `WWW-Authenticate`）成立；
        守卫缺位时该由认证类兜住——那半边在 `test_9c_...`。
        """
        with tenant_context(self.tenant_a):
            get_user_model().objects.create_user(
                username=DISABLED_USER_ON_GUARD, password="pw"
            )
        client, login = self._real_login(DOMAIN_A, DISABLED_USER_ON_GUARD, "pw")
        self._assert_successful_login(login)
        session_id = client.cookies["sessionid"].value

        self._deactivate_tenant_a()
        try:
            response = self._session_client(session_id, DOMAIN_A).get("/api/auth/me")
            self.assert_guard_rejected(response)
            # spec §5.1-9 的"取不到任何业务数据"：响应体里不得出现该租户用户的任何标识。
            body = response.content.decode()
            self.assertNotIn(DISABLED_USER_ON_GUARD, body)
            self.assertNotIn(self.tenant_a.schema_name, body)

            # 短路自证（§5.2-4）：守卫拒一次会把会话 `flush()` 掉（`middleware.py:39`），
            # 于是把守卫摘掉之后再打一次 `/api/auth/me`——此时请求**已认证不了**，
            # 401 来自 DRF 的 NotAuthenticated。反过来就证明上面那个 401 是守卫
            # **在会话还有效时**短路产出的（否则"守卫生效"这个故事就是空的）。
            # ⚠️ 它只钉守卫这一半，**不**覆盖认证类：实测在 `_assert_tenant_active`
            # 删空（MUT-I）时本条照样绿——认证类的半边由 `test_9c_...` 负责。
            with override_settings(MIDDLEWARE=self._disabled_guard_settings()):
                after = self._session_client(session_id, DOMAIN_A).get("/api/auth/me")
            self.assertEqual(
                after.status_code,
                401,
                f"守卫拒完之后这条会话居然还能用：说明上面的 401 不是守卫短路产出的"
                f"（{after.status_code}：{after.content[:200]!r}）",
            )
            self.assertEqual(
                after.headers.get("WWW-Authenticate"),
                "Session",
                "守卫拒完后重放必须落在认证类的 NotAuthenticated 上（带这个头）",
            )
        finally:
            self._reactivate_tenant_a()

    @override_settings(
        # 方法级（类级会被 `TenantTestCase.setUpClass` 静默丢弃，见类 docstring 第 1 条）。
        # 唯一差别：**摘掉** `apps.core.middleware.TenantSessionGuardMiddleware`，
        # 其余与真实栈一致，`TenantBoundAuthentication` 仍在 `REST_FRAMEWORK` 里配着。
        MIDDLEWARE=[
            "django_tenants.middleware.main.TenantMainMiddleware",
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
        ],
    )
    def test_9c_disabled_tenant_is_rejected_by_the_auth_class_without_the_guard(self):
        """spec §5.1-9 的已认证半边，由 `TenantBoundAuthentication` **独立**兜住。

        为什么必须有这一条（Task 7 评审 Finding 1）：守卫态只钉匿名 `/healthz`，
        于是 `TenantBoundAuthentication._assert_tenant_active`
        （`apps/core/authentication.py:29-38`）**零变异覆盖**——把它的函数体删空，
        整套测试仍然全绿（实测：删空后本模块其余 12 条全过，只有本条变红）。

        ⚠️ **必须用一条新会话**，不能复用 9b 那条：守卫 `_reject` 会 `session.flush()`
        （`apps/core/middleware.py:39`，实测守卫态请求后
        `Session.objects.filter(session_key=...).exists()` 为 `False`），复用只会得到
        "Django 认不出这条会话"的 401——那与被测分支无关，且删空
        `_assert_tenant_active` 后**照样绿**（零判别力）。所以这里重新真实登录一次再停用。

        两道防线的可观测差别（spec §4.6，`assert_guard_rejected` 的镜像用法）：
        认证类的 401 **带** `WWW-Authenticate: Session`（`authenticate_header`），
        守卫的 401 **不带**。本条的 401 必须带——这正是"拒绝来自认证类"的证据。
        """
        with tenant_context(self.tenant_a):
            get_user_model().objects.create_user(
                username=DISABLED_USER_ON_AUTH_CLASS, password="pw"
            )
        client, login = self._real_login(DOMAIN_A, DISABLED_USER_ON_AUTH_CLASS, "pw")
        self._assert_successful_login(login)
        session_id = client.cookies["sessionid"].value

        self._deactivate_tenant_a()
        try:
            response = self._session_client(session_id, DOMAIN_A).get("/api/auth/me")
            self.assertEqual(
                response.status_code,
                401,
                f"停用租户上的已认证会话必须被拒；实际 {response.status_code}："
                f"{response.content[:200]!r}",
            )
            self.assertEqual(response.json()["code"], 401)
            # 差异断言：带这个头 ⇒ 拒绝来自认证类（守卫在本栈里根本没装）。
            self.assertEqual(
                response.headers.get("WWW-Authenticate"),
                "Session",
                "401 没带 WWW-Authenticate：说明拒绝不是 TenantBoundAuthentication 抛的"
                "（本栈里没有守卫，只可能是别的原因）",
            )
            # spec §5.1-9 的"取不到任何业务数据"：响应体里不得出现该租户用户的任何标识。
            body = response.content.decode()
            self.assertNotIn(DISABLED_USER_ON_AUTH_CLASS, body)
            self.assertNotIn(self.tenant_a.schema_name, body)
        finally:
            self._reactivate_tenant_a()
