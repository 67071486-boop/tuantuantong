from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

# 会话的**权威构造器**在认证 helper 模块里（`_auth_user_hash` 必须用
# `get_session_auth_hash()` 推导，不能把 `password` 字段原样塞进去——原样塞会被
# Django 的 HMAC 兜底拒掉，所有用它构造的断言都会**恒真**；细节见它的 docstring）。
# 这里直接复用而不是抄一份：抄写版会把那个坑复制一份，而两份拷贝一定会漂移。
from tests.test_p5_credential_plane import materialize_session

# 配置期的 DEBUG（settings 模块的全局），**不是** `django.conf.settings.DEBUG`：
# `manage.py test` 会用 `setup_test_environment(debug=False)` 把后者**强制改成 False**
# （`django/test/runner.py:1072` → `django/test/utils.py:144`），
# 拿它做判据，下面「本地开发形态」「生产 Secure」两支在测试里永远是死代码。
from config import settings as project_settings

from apps.core.envelope import error_response
from apps.platform.provisioning import provision_tenant

# 登录失败的**唯一**那句 msg。这里是**独立抄写**的期望值，不从实现里 import：
# 从实现 import 会让「测试用实现的值去断言实现」变成恒真式——实现把 msg 改成
# "密码错误" 也一样绿，而 D8 要求的正是"不泄露到底哪里错"。
FAIL_MESSAGE = "用户名或密码错误"

# 未认证 401 的 msg。DRF 5.x 在 zh-hans 下把 `NotAuthenticated.default_detail`
# 翻成这一句（`rest_framework/locale/zh_Hans/LC_MESSAGES/django.po`），
# `envelope_exception_handler` 直接转发它（`apps/core/exceptions.py:62-63`）。
# 同样**独立抄写**：从实现 import 等于用实现断言实现。
UNAUTHENTICATED_MESSAGE = "身份认证信息未提供。"

DOMAIN_A = "login-a.test.local"
DOMAIN_INACTIVE = "login-off.test.local"

# 信封/CSRF 端到端测试（第 10–14 条）的租户。**独立**于 `DOMAIN_A` 的 login_a：
# 那些测试要手工 materialize 会话，落在**别的**测试类建过的租户里会让构造物
# 依赖那个类的残留数据（E-0015 的教训是同一条线上踩出来的）。
DOMAIN_ENVELOPE = "envelope.test.local"


class EnvelopeHelperTests(SimpleTestCase):
    """信封只有一种形状：中间件与异常处理器必须共用它（spec D4 的代价）。"""

    def test_error_response_uses_the_unified_shape(self):
        response = error_response(401, "会话与当前租户不匹配", 401)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.data, {"code": 401, "msg": "会话与当前租户不匹配", "data": None}
        )

    def test_error_response_can_carry_headers(self):
        """401 必须能带上 WWW-Authenticate（spec §4.6）。"""
        response = error_response(401, "未认证", 401, headers={"WWW-Authenticate": "Session"})
        self.assertEqual(response.headers["WWW-Authenticate"], "Session")


class FailClosedConfigTests(SimpleTestCase):
    def test_default_permission_class_is_fail_closed(self):
        """D11：默认必须是 IsAuthenticated，不能是 DRF 的 AllowAny。

        这只是**配置级**证据（字面量写对了）。"漏写 `permission_classes` 的接口真的
        变成需登录"这件事由 `EnvelopeAndCsrfTests` 的 10b 用**真实业务端点**端到端钉住
        ——配置读对了仍可能被项目自己的中间件/异常处理器覆盖（E-0014）。
        """
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"],
            ["rest_framework.permissions.IsAuthenticated"],
        )

    def test_authentication_class_is_tenant_bound(self):
        # （打手 App 线）：默认认证类列表**变长**了——Session 仍排第一
        # （Web 端的 401 `WWW-Authenticate: Session` 契约由下面三条用例钉着），
        # Bearer 排第二、只在没有会话时接手。原断言写的是「只有一个元素」，
        # 这里按新配置改准，**不是**放宽：两条都必须在，顺序也在。
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"],
            [
                "apps.core.authentication.TenantBoundAuthentication",
                "apps.core.authentication.AppTokenAuthentication",
            ],
        )

    def test_session_cookie_is_host_only_and_hardened(self):
        """D5 + spec §4.4：host-only、HttpOnly、SameSite=Lax、生产 Secure。"""
        self.assertFalse(getattr(settings, "SESSION_COOKIE_DOMAIN", None))
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        # 「生产 Secure」不是靠 Django 的默认值：它的默认是 False（global_settings.py:490），
        # 必须由 settings 显式绑到 `not DEBUG`，否则生产会把会话 cookie 发到明文 HTTP 上。
        self.assertEqual(settings.SESSION_COOKIE_SECURE, not project_settings.DEBUG)

    def test_csrf_cookie_is_hardened_like_the_session_cookie(self):
        """CSRF cookie 与 session cookie **同口径**：生产必须 `Secure`（spec §4.4）。

        本分支把 CSRF 当成核心机制（登录也强制校验，D15），所以它不该在明文 HTTP 上传。
        Django 的默认值是 `False`（`global_settings.py:574`），必须显式绑到 `not DEBUG`。

        ⚠️ 本机 `.env` 是 `DJANGO_DEBUG=true`，两支都是 `False`，所以**在默认跑法下
        本断言证明不了"显式绑定"**。判别力由一次 `DJANGO_DEBUG=false` 的单测运行给出
        （见交付报告的变异证据：删掉 settings 里那一行 → 该运行下本方法红，
        `False != True`）。这里同时钉"与 session cookie 相等"，是为了让
        "两处口径漂移"（一个 Secure、一个不 Secure）在默认跑法下也立刻可见。
        """
        self.assertEqual(settings.CSRF_COOKIE_SECURE, settings.SESSION_COOKIE_SECURE)
        self.assertEqual(settings.CSRF_COOKIE_SECURE, not project_settings.DEBUG)

    def test_csrf_cookie_is_readable_by_the_spa(self):
        """前端要经 `document.cookie` 读 `csrftoken` —— 把「框架默认」钉成**项目契约**。

        **跨层契约的另一半**在前端：
        `src/frontend/src/api/csrf-cookie.ts:1` 的 `CSRF_COOKIE_NAME = 'csrftoken'` 与
        `src/frontend/src/api/http.ts:29` 的 `CSRF_HEADER_NAME = 'X-CSRFToken'`。

        下面四个值在 `config/settings.py` 里**都没有被覆盖**，全部来自 Django 默认
        （`django/conf/global_settings.py`）。正因为是"默认"而不是"显式约定"，
        它们才会被无声改掉：有人为了"更安全"打开 `CSRF_COOKIE_HTTPONLY`、
        或把 token 挪进 session，前端的注入会**静默失效**（所有写请求 403，
        且错在"读不到 cookie"而不是错在权限），所以在后端把它钉住。
        """
        # 1) cookie 必须能被 JS 读到（`global_settings.py:575`，默认 False）。
        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)
        # 2) token 必须来自 cookie 而不是 session（`:579`，默认 False）。
        #    走 session 时 Django **不发** CSRF cookie，前端一个值都读不到。
        self.assertFalse(settings.CSRF_USE_SESSIONS)
        # 3) cookie 名要与前端常量一致（`:570`，默认 "csrftoken"）。
        self.assertEqual(settings.CSRF_COOKIE_NAME, "csrftoken")
        # 4) 请求头名：Django 从 `request.META` 取 `HTTP_X_CSRFTOKEN`
        #    （`django/middleware/csrf.py:384`）↔ 前端发 HTTP 头 `X-CSRFToken`
        #    （`global_settings.py:577`，默认 "HTTP_X_CSRFTOKEN"）。
        self.assertEqual(settings.CSRF_HEADER_NAME, "HTTP_X_CSRFTOKEN")

    def test_csrf_trusted_origins_are_configured_for_subdomains(self):
        """spec §4.4：跨源写请求必须靠 `CSRF_TRUSTED_ORIGINS` 放行，生产按**根域通配**登记。

        为什么不断言**具体取值**（这是 brief 允许的替代断言，理由如下）：
        `CSRF_TRUSTED_ORIGINS` 是**部署值**——本机 `.env` 与 `.env.example` 用的是本地开发形态
        （显式 `http://localhost:*`），生产用根域通配（`https://*.example.com`）。
        把生产取值钉死在测试里，会让「照 `.env.example` 起步」的新克隆**必然红**，
        并且会引诱后来者把**真实域名**写进测试（计划明令禁止）。
        所以这里钉的是**形态**，不是取值：

        - 生产形态：至少一条 `https://*.` 通配项。Django 确实支持通配子域——
          见 `django/middleware/csrf.py:186-198` 的 `allowed_origin_subdomains`
          与 `:286-295` 里用 `is_same_domain` 做的子域比对；
        - 本地形态：**配置期** `DEBUG=True` 且来源非空——SPA 跑在另一个端口，
          本地也必须显式登记，空列表不是合法形态。

        非 DEBUG 却没有任何通配项 → 红：那说明生产会拒掉**所有租户子域**上的写请求。
        """
        origins = settings.CSRF_TRUSTED_ORIGINS
        is_production_shape = any(o.startswith("https://*.") for o in origins)
        is_local_dev_shape = bool(project_settings.DEBUG and origins)
        self.assertTrue(
            is_production_shape or is_local_dev_shape,
            f"CSRF_TRUSTED_ORIGINS 既非生产形态（无 https://*. 通配项）"
            f"也非本地开发形态（DEBUG + 非空）：{origins}",
        )


class GuardWiringTests(SimpleTestCase):
    def test_guard_middleware_is_after_authentication_middleware(self):
        middleware = list(settings.MIDDLEWARE)
        self.assertIn("apps.core.middleware.TenantSessionGuardMiddleware", middleware)
        self.assertLess(
            middleware.index("django.contrib.auth.middleware.AuthenticationMiddleware"),
            middleware.index("apps.core.middleware.TenantSessionGuardMiddleware"),
            "守卫必须排在 AuthenticationMiddleware 之后，否则它读不到 request.user",
        )


class LoginEndpointTests(TenantTestCase):
    """`/api/auth/*` 四个端点（spec §4.2/§4.3/§4.4）。

    用**真实** MIDDLEWARE 与真实 ROOT_URLCONF（`config.urls`）：认证端点的判别力
    来自"守卫装在栈上时它仍然能把人登进来"，`override_settings` 会把这个前提抽掉。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_A, DOMAIN_INACTIVE) if d not in settings.ALLOWED_HOSTS
        ]
        # ⚠️ **绝不能写 `cls.tenant = ...`**：`TenantTestCase.tearDownClass`
        # 用的是 `cls.tenant`（还有 `cls.domain`）来删掉它自己建的那个共享 `test` 租户。
        # 覆盖它 → 它删的是**我们的** login_a，共享 `test` 行留在库里，
        # 下一个 `TenantTestCase` 子类的 `setUpClass` 会撞
        # `duplicate key ... platform_tenant_schema_name_key`（实测踩到）。
        # provision_tenant 会灌默认职位与无密码的 admin。
        cls.login_tenant, _ = provision_tenant(
            schema_name="login_a", name="登录租户", domain=DOMAIN_A
        )
        cls.disabled_tenant, _ = provision_tenant(
            schema_name="login_off", name="停用租户", domain=DOMAIN_INACTIVE
        )
        user_model = get_user_model()
        with tenant_context(cls.login_tenant):
            cls.user = user_model.objects.create_user(username="kefu", password="pw-ok")
            cls.inactive_user = user_model.objects.create_user(
                username="stopped", password="pw-ok", is_active=False
            )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for domain in (DOMAIN_A, DOMAIN_INACTIVE):
            if domain in settings.ALLOWED_HOSTS:
                settings.ALLOWED_HOSTS.remove(domain)

    def _login(self, username, password, *, domain=DOMAIN_A):
        """D15 的真实流程：先取 csrftoken cookie，再把它放进 X-CSRFToken 头。

        用 `enforce_csrf_checks=True` 的 client：`Client` 默认把
        `request._dont_enforce_csrf_checks` 置 True（`django/test/client.py:183`），
        而视图上的 `csrf_protect` 就是 `CsrfViewMiddleware.process_view`
        （`decorator_from_middleware`），它会读同一个 flag
        （`django/middleware/csrf.py:427`）——默认 client 下装饰器等于不存在，
        "带着正确 token 也能登录"这类断言会**恒真**、零判别力。
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

    def test_login_success_and_me(self):
        client, response = self._login("kefu", "pw-ok")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        user_payload = response.json()["data"]["user"]
        self.assertEqual(user_payload["id"], self.user.pk)
        self.assertEqual(user_payload["username"], "kefu")
        self.assertEqual(user_payload["display_name"], "")
        self.assertEqual(user_payload["is_tenant_admin"], False)
        self.assertEqual(user_payload["dashboard_type"], "")
        # `is_account_admin` 由账号线加入（spec §5.4，**增量**契约变更）。
        # ⚠️ 判据统一后**两个字段在租户平面上同真同假**（判据 = 持「系统管理员」
        # 职位）：原文那句「`is_tenant_admin` 仍是 P5 的 `bool(is_superuser)`」随本线作废；
        # 分叉只剩平台平面（`tests/test_account_admin_predicate.py` 的 public 类钉住）。
        # 本用例的期望值一字未改——它测的是**普通客服**（两个字段都该是 False）。
        self.assertEqual(
            sorted(user_payload),
            [
                # `avatar_url` 由顶栏头像同步线加入（**增量**契约变更）：
                # 请求者**本人**头像的短时效读链接（没传过 / 平台平面 ⇒ `None`）。
                # ⚠️ 字母序：`avatar_url` 排在最前（`a` < `d`）。同样**只加字面条目**，
                # 没有把判据改成"包含"或删掉。
                "avatar_url",
                "dashboard_type",
                "display_name",
                # `game_ids` 由本线（订单域按游戏权限收窄）加入（**增量**契约变更）：
                # 该账号有权访问的游戏主键列表（升序；没有档案 ⇒ `[]`）。改动前这份字面键表
                # 没同步 ⇒ 本用例红（字面键表是**刻意**写死的，不许改成"包含"或删掉）。
                "game_ids",
                "id",
                "identity_plane",
                "is_account_admin",
                "is_tenant_admin",
                # `menu_permissions` 由 RBAC 基座加入（D-0028，**增量**契约变更）：
                # 节点 code 的排序后并集，是前端菜单/路由的可见性数据源。
                # `permissions`（Django codename）**保留原样**，冻结为 legacy——
                # 两套语义并存，不是替换关系。
                "menu_permissions",
                "permissions",
                # `role` / `tenant_name` 由「我的资料」线（spec §5.4）加入（**增量**契约变更）：
                # `role` = 单值职位（名称升序首个，无则 `""`）；`tenant_name` = 当前租户的
                # `name`（工作室名，取不到则 `""`）。同样**只加字面条目**，没有改成"包含"。
                "role",
                "roles",
                # ⚠️ 字母序：`tenant_name` 排在 `roles` 之后、`username` 之前。
                "tenant_name",
                "username",
            ],
        )
        # `identity_plane` 取**当前 schema 名**（spec §4.2 字段表）。这里不比硬编码
        # 字符串：TenantTestCase 的 schema 名由 django-tenants 决定。
        self.assertEqual(user_payload["identity_plane"], connection.schema_name)
        self.assertEqual(user_payload["roles"], [])
        self.assertEqual(user_payload["permissions"], [])
        # 空方向：`kefu` 不在任何角色里 ⇒ 节点并集为空（不是 None、不是 500）。
        self.assertEqual(user_payload["menu_permissions"], [])

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["user"], user_payload)

    def test_login_and_me_report_the_users_roles(self):
        """角色的**非空方向**：用户在某职位里 → `roles` 必须含该职位名（spec §4.2 / §6.1）。

        上面 `test_login_success_and_me` 只断言了空列表那一半（`kefu` 不在任何 Group 里），
        两个方向合起来才钉住 `roles` 这个字段：
        - 删掉 `_user_payload` 里的 `user.groups.values_list("name", flat=True)`（恒回 `[]`）
          → **本方法**变红，上面那条照样绿；
        - 反过来，把 `roles` 写成"当前 schema 里所有 Group 名"→ 上面那条变红（`kefu`
          会拿到五个默认职位），本方法靠"含该职位名"钉不住这一半——所以本方法
          **同时**断言该用户只持有这一个职位。
        前端的菜单过滤（`useMenu()`）吃的就是这个字段，空列表会让菜单永远为空。
        """
        from apps.business.roles import SERVICE

        with tenant_context(self.login_tenant):
            # 真实分配路径：`kefu` 加入「客服」Group（默认职位由 provision_tenant 种入）。
            self.user.groups.add(Group.objects.get(name=SERVICE))

        client, response = self._login("kefu", "pw-ok")
        self.assertEqual(response.status_code, 200, response.content[:200])
        user_payload = response.json()["data"]["user"]
        self.assertEqual(user_payload["roles"], [SERVICE])
        self.assertEqual(user_payload["dashboard_type"], "work")

        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.content[:200])
        self.assertEqual(me.json()["data"]["user"]["roles"], [SERVICE])
        self.assertEqual(me.json()["data"]["user"]["dashboard_type"], "work")

    def test_me_requires_authentication(self):
        """`/api/auth/me` 的匿名请求必须 **401**（端到端 401 行为）。

        ⚠️ 本条**不是**"`MeView` 必须显式声明 `IsAuthenticated`"的判据：默认权限类已是
        fail-closed（Task 11 开启，配置级证据见 `FailClosedConfigTests`），**删掉**
        `MeView` 上那一行它**仍然** 401。显式声明是**契约 / 纵深防御**——保证 `/me`
        的要求不随默认值的变迁而变——它的意义由源码注释与计划文档表述，不由本方法证明。
        """
        # 本类（认证/CSRF 端点）**一律**用 `enforce_csrf_checks=True` 的 client：
        # 默认 client 静默跳过 CSRF，留着它只会让后来复制粘贴的人
        # 继承一个"校验被关掉"的隐藏前提。这条是 GET、本就与 CSRF 无关，
        # 写在这里是为了让"本类不用默认 client"成为一条**机械规则**而非判断题。
        response = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True).get(
            "/api/auth/me"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Session")

    def test_unknown_user_is_rejected(self):
        self._assert_same_401(self._login("nobody", "pw-ok")[1])

    def test_wrong_password_is_rejected(self):
        self._assert_same_401(self._login("kefu", "wrong")[1])

    def test_inactive_user_is_rejected(self):
        self._assert_same_401(self._login("stopped", "pw-ok")[1])

    def test_inactive_tenant_is_rejected(self):
        """停用租户上的**任何**请求（含匿名）被守卫拒——spec §4.6 的**显式例外**。

        spec §4.6 的例外（Task 5 评审发现、控制器裁决后写入 spec）：
        停用租户由 `TenantSessionGuardMiddleware` 的 `is_active` 分支短路，
        用**守卫自己的 msg** `会话与当前租户不匹配`，且 **不带 `WWW-Authenticate`**
        ——它不是"认证挑战"（连账号都没查），而是平面/生命周期守卫的拒绝。
        所以本条的契约是**守卫的 401**，不是 `LoginView` 那句 `FAIL_MESSAGE`
        （`LoginView.post` 里的 `is_active` 判据只有在守卫被摘掉时才可达，
        留着是 D4"顺序耦合"代价下的兜底；Task 7 的红线覆盖那条路）。

        下面把这两点**显式**断言出来（此前只隐含：body 相等掩盖了"头必须缺席"
        这条契约，容易被后来的读者当成遗漏——评审 Finding 3）。
        """
        # 目录表（Tenant）属于 public：`Tenant.save()` 只在 public schema 下放行
        # （django_tenants/models.py:106-110），上一条测试跑完后连接还停在某个租户上。
        with schema_context("public"):
            self.disabled_tenant.is_active = False
            self.disabled_tenant.save(update_fields=["is_active"])
        try:
            client = Client(SERVER_NAME=DOMAIN_INACTIVE, enforce_csrf_checks=True)
            # 停用租户上的**任何**请求都被守卫短路（含匿名）：
            # CSRF 引导端点也拿不到 cookie——"停用租户还能访问是硬伤"的堵法。
            bootstrap_rejected = client.get("/api/auth/csrf")
            self.assertEqual(bootstrap_rejected.status_code, 401)
            # 匿名路径同样用守卫那句 msg，且同样**不带** `WWW-Authenticate`
            # （spec §4.6 例外对匿名请求一视同仁）。
            self.assertEqual(
                bootstrap_rejected.json(),
                {"code": 401, "msg": "会话与当前租户不匹配", "data": None},
            )
            self.assertIsNone(bootstrap_rejected.headers.get("WWW-Authenticate"))
            # CSRF token 从**活跃**租户那侧取：cookie 是 host-only 的（D5），但测试
            # client 会把 cookie 带给任意 Host；这里要的就是"CSRF 这一关能过"，
            # 好让失败原因唯一地落在"租户已停用"上。
            bootstrap = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True)
            bootstrap.get("/api/auth/csrf")
            token = bootstrap.cookies["csrftoken"].value
            response = client.post(
                "/api/auth/login",
                data={"username": "kefu", "password": "pw-ok"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            )
            self.assertEqual(response.status_code, 401, response.content[:200])
            self.assertEqual(
                response.json(),
                {"code": 401, "msg": "会话与当前租户不匹配", "data": None},
            )
            # **显式**断言缺席：401 必须带 `WWW-Authenticate` 是 §4.6 的通则，
            # 停用租户是它写死的例外；不加这条，"守卫忘了带头"与"裁决要求不带"
            # 在测试里长得一模一样。
            self.assertIsNone(response.headers.get("WWW-Authenticate"))
        finally:
            with schema_context("public"):
                self.disabled_tenant.is_active = True
                self.disabled_tenant.save(update_fields=["is_active"])

    def test_login_without_csrf_token_returns_json_403(self):
        """裸 POST 登录必须被 CSRF 挡住，且回统一信封——不是 Django 的 HTML 403。

        DRF 对每个 APIView 都套了 `csrf_exempt`（rest_framework/views.py:149），
        所以默认**零保护**：这条测试是 `csrf_protect` 装饰器真的在的唯一证据。
        `CSRF_FAILURE_VIEW` 默认渲染 HTML（django/views/csrf.py），
        配置里改成统一信封后这里才会是 application/json。

        `enforce_csrf_checks=True` 是**必须**的：默认 client 会把
        `request._dont_enforce_csrf_checks` 置 True（django/test/client.py:183），
        而 `CsrfViewMiddleware.process_view` 读同一个 flag
        （django/middleware/csrf.py:427）——用默认 client 时这条断言恒真。
        """
        response = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True).post(
            "/api/auth/login",
            data={"username": "kefu", "password": "pw-ok"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["data"], None)
        # **逐字节**对齐 DRF 的渲染：`CSRF_FAILURE_VIEW` 早先用
        # `JsonResponse` 手写信封，走 `json.dumps` 默认参数（分隔符带空格），
        # 而 DRF 的 `COMPACT_JSON` 用 `SHORT_SEPARATORS=(",",":")`——
        # 同一个 403 的字节形状因此不同。钉死原始 bytes，这条路径再也不能偷偷背离。
        self.assertEqual(
            response.content,
            '{"code":403,"msg":"CSRF 校验失败","data":null}'.encode(),
        )

    def test_logout_is_idempotent(self):
        """匿名裸 POST 也 200（`LogoutView` 刻意不加 `csrf_protect`，spec §4.2）。

        判据有**两条**，缺任一条这条断言就恒真、零判别力（评审 Finding 4）：

        1. `enforce_csrf_checks=True`——默认 client 把
           `request._dont_enforce_csrf_checks` 置 True（django/test/client.py:183），
           而 `csrf_protect` 就是 `CsrfViewMiddleware.process_view`
           （`decorator_from_middleware`），读同一个 flag
           （django/middleware/csrf.py:427）——默认 client 下"加了 `csrf_protect`
           也照样 200"；
        2. **不带** `HTTP_X_CSRFTOKEN`——带上一个合法 token 时，加不加
           `csrf_protect` 都是 200，同样证明不了任何事。

        两条一起才真正证明：外层 `csrf_exempt`（DRF 给每个 APIView 都套，
        `rest_framework/views.py:149`）+ 匿名请求不经过
        `SessionAuthentication.enforce_csrf`（它只在**已认证**时跑，
        `rest_framework/authentication.py:117-133`）。
        """
        anonymous = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True)
        first = anonymous.post("/api/auth/logout")
        second = anonymous.post("/api/auth/logout")
        for response in (first, second):
            self.assertEqual(response.status_code, 200, response.content[:200])
            self.assertEqual(response.json(), {"code": 0, "msg": "", "data": None})

        # 已认证则另一回事：DRF 的 `enforce_csrf` 自己把关，**不带 token 的登出被拒**
        # （这正是"跨站登出打不掉会话"的机制，spec §5.1-14）。
        # 与上面的匿名 200 合起来把"幂等"和"不设防"划开：不是没保护，
        # 是保护只覆盖已认证请求——那是 `LogoutView` 的显式代价（spec D15）。
        client, login_response = self._login("kefu", "pw-ok")
        self.assertEqual(login_response.status_code, 200)
        # `login()` 会 `rotate_token()`，必须重读 cookie（读旧值会 403，判据就失真）。
        token = client.cookies["csrftoken"].value
        rejected = client.post("/api/auth/logout")           # 故意不带 token
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertIn("CSRF", rejected.json()["msg"])
        # 被拒的登出**不得**产生副作用：会话必须仍然有效。
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        # 带上 token 才真的登出，且登出后不再是登录态。
        self.assertEqual(
            client.post("/api/auth/logout", HTTP_X_CSRFTOKEN=token).status_code, 200
        )
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_csrf_endpoint_hands_out_the_cookie(self):
        # 同样用 `enforce_csrf_checks=True`：本类是认证/CSRF 端点，
        # 不留任何"默认 client"——GET 走安全方法、行为不变，但这条规则机械可查。
        client = Client(SERVER_NAME=DOMAIN_A, enforce_csrf_checks=True)
        response = client.get("/api/auth/csrf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"code": 0, "msg": "", "data": None})
        self.assertIn("csrftoken", client.cookies)

    def _assert_same_401(self, response):
        """D8：四种失败一律 401 + **同一句** msg，不得泄露账号/租户是否存在或已停用。"""
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(
            response.json(),
            {"code": 401, "msg": FAIL_MESSAGE, "data": None},
        )
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Session")


class EnvelopeAndCsrfTests(TenantTestCase):
    """spec §5.1 第 10–14 条：未认证 401 的**信封与挑战头**、403 的可区分性、跨站登出。

    本类补的是「端到端」这一维：`LoginEndpointTests` 只覆盖了登录/登出这两条端点自己的
    契约，第 10 条要求的「真实受保护端点的匿名 401 带 `WWW-Authenticate`」在本类之前
    **没有任何测试**（`test_envelope.py` 只是孤立构造 `NotAuthenticated()` 调处理器，
    不经过 HTTP 栈，也不看响应头——spec §4.6 末尾点名的就是那条 P1 缺口）。

    三条必须照做的裁决（与 Task 5/7 的同类注释互为索引）：

    1. **每个 `Client` 都带 `enforce_csrf_checks=True`**：`Client` 默认把
       `request._dont_enforce_csrf_checks` 置 True（`django/test/client.py:183`），
       而 `CsrfViewMiddleware.process_view` 读同一个 flag
       （`django/middleware/csrf.py:427`）——默认 client 下每条 CSRF 断言都恒真。
    2. **`@override_settings` 一律放方法上**：`TenantTestCase.setUpClass`
       （`django_tenants/test/cases.py`）**不调** `super().setUpClass()`，类级 override
       在 `SimpleTestCase.setUpClass` 里生效，于是被**静默丢弃**。
    3. **额外租户存 `envelope_tenant`，绝不写 `cls.tenant` / `cls.domain`**：基类
       `tearDownClass` 正是用后两个属性删它自己建的共享 `test` 租户，覆盖它就是 E-0015
       （共享 schema 留在库里，下一个子类撞唯一约束）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        settings.ALLOWED_HOSTS += [
            d for d in (DOMAIN_ENVELOPE,) if d not in settings.ALLOWED_HOSTS
        ]
        # provision_tenant 会灌默认职位与无密码 admin，并建出真的租户 schema。
        # 用**独立** schema（不蹭基类的 `test`）：手工 materialize 的会话落在别处时，
        # 下面的 403 会因为「认证先失败」而以另一种原因通过（E-0003）。
        cls.envelope_tenant, _ = provision_tenant(
            schema_name="envelope", name="信封租户", domain=DOMAIN_ENVELOPE
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if DOMAIN_ENVELOPE in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN_ENVELOPE)

    def _authenticated_client(self):
        """本租户平面上的一个**真**已认证会话（手工 materialize，不带 CSRF cookie）。

        刻意不用 `client.login()` / `POST /api/auth/login`：登录会 `rotate_token()`
        并下发 `csrftoken` cookie，随后「不带 token 的写」就不成立了（client 会自动把
        cookie 里的 token 放进请求头）。手工造的会话**没有** CSRF cookie，
        于是下面第 11 条的「CSRF 失败」是**唯一**可能的原因。
        """
        with tenant_context(self.envelope_tenant):
            user = get_user_model().objects.create_user(username="envelope-user", password="pw")
            user_pk, pw_hash = user.pk, user.password
        session_id = materialize_session(
            self.envelope_tenant.schema_name,
            user_pk=user_pk,
            password_hash=pw_hash,
            plane=self.envelope_tenant.schema_name,
        )
        client = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True)
        client.cookies["sessionid"] = session_id
        return client

    # ── 10：未认证端到端 401 + 信封 + WWW-Authenticate ──────────────
    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_10_unauthenticated_request_has_envelope_and_www_authenticate(self):
        """spec §5.1-10 / §4.6：真实请求 → 真实受保护 view，401 必须带挑战头。

        这条头不是框架给的：`SessionAuthentication` **没有** `authenticate_header`，
        基类返回 None，DRF 于是把 `exc.status_code` 改成 403
        （`rest_framework/views.py:458-466`）。但在**本项目**里那个改写不会变成可观测的
        403：`envelope_exception_handler` 按异常类型重新钉死状态码
        （`apps/core/exceptions.py:62-63`，`NotAuthenticated` → `_envelope(401, ..., 401)`），
        它**忽略** DRF 改过的 `exc.status_code`。所以摘掉这条头时响应仍是 401 + `code:401`，
        消失的只是 `WWW-Authenticate`——M1 变异实测的红色落在下面那条头断言
        （`None != 'Session'`），**不是**状态码断言。因此这条测试的判别项是挑战头，
        不能按「DRF 默认会把 401 降级成 403」去描述本项目的行为（E-0014）。
        它是本项目为了合规额外加的一层（`TenantBoundAuthentication.authenticate_header`），
        所以必须**端到端**钉住：只在单元层断言 `error_response` 能带头
        （`EnvelopeHelperTests` 那条）证明不了真实的 DRF 异常链会把头带出来。

        只断状态码是不够的（§5.2-2）：401 可能来自守卫短路（那条**刻意不带**这个头，
        spec §4.6 的显式例外）或其它原因，所以这里把信封、`code`、头三件事一起断。

        `@override_settings(ROOT_URLCONF=...)` 必须是**方法级**：`TenantTestCase.setUpClass`
        不调 `super().setUpClass()`，类级 override 会被静默丢弃（本类 docstring 第 2 条）。
        """
        response = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True).get(
            "/__probe__/whoami"
        )
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(response.json()["code"], 401)
        self.assertEqual(response.json()["msg"], UNAUTHENTICATED_MESSAGE)
        self.assertEqual(response.json()["data"], None)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Session")
        # 401 的信封里只有信封本身：不得出现任何身份字段（探针在**放行**时会回显
        # `auth/pk/username/conn_schema`）。断**结构**而不是搜字符串：匿名 client 本来
        # 就没有任何身份可回显，`assertNotIn("envelope-user", ...)` 是恒真的。
        self.assertEqual(sorted(response.json()), ["code", "data", "msg"])

    # ── 10b/10c：D11 的端到端后果（真实 P2 业务端点，不是探针）──────
    def test_10b_real_business_endpoint_rejects_anonymous_requests(self):
        """spec D11 + §5.1-10：漏写 `permission_classes` 的**真实业务接口**对匿名请求必须 401。

        `FailClosedConfigTests.test_default_permission_class_is_fail_closed` 只证明 settings
        里的字面量写对了，那是**配置级**证据。D11 的真实后果是"忘记声明权限类的接口变成需登录"，
        所以这条打**真实请求 → 真实 P2 端点**（`GameViewSet`，`apps/business/api/config_views.py`
        里**没有** `permission_classes`），而不是测试专用探针——探针自己声明了
        `IsAuthenticated`，与默认权限类无关，摘掉默认权限类它照样 401（零判别力）。
        E-0014 的教训正是：框架层面读对了，仍可能被项目自己的配置/回调覆盖。

        三条一起断（§5.2-3/4 的"不用 count、断言身份/来源"精神）：401 + 信封 `code:401`
        + `WWW-Authenticate`（spec §4.2 写死的状态码边界）。反向对照见下一条 10c。
        """
        response = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True).get(
            "/api/business/games/"
        )
        self.assertEqual(response.status_code, 401, response.content[:200])
        self.assertEqual(response.json()["code"], 401)
        self.assertEqual(response.json()["data"], None)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Session")

    def test_10c_real_business_endpoint_serves_a_logged_in_user(self):
        """第 10b 条的**反向对照**：同一端点用已登录客户端 → 200 + 业务信封 `code:0`。

        没有它，10b 的 401 就有第二个解释："这个端点被整个打死了 / 路由坏了"——那样
        "匿名被拒"照样绿（§5.2-4：断言选中的身份，不只断言状态码）。这里走**真实登录**
        （D15 的 CSRF 引导 + 登录端点，用户建在**目标租户 schema 内**，E-0013），
        不是手工 materialize 会话：正常用户路径必须自己跑通。
        """
        with tenant_context(self.envelope_tenant):
            get_user_model().objects.create_user(username="e2e-user", password="pw-e2e")

        client = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True)
        client.get("/api/auth/csrf")
        token = client.cookies["csrftoken"].value
        login = client.post(
            "/api/auth/login",
            data={"username": "e2e-user", "password": "pw-e2e"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

        response = client.get("/api/business/games/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(response.json()["data"]["total"], 0)

    # ── 11：无权限 403 与 CSRF 失败 403 必须可区分 ────────────────
    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_11_csrf_failure_on_write_is_a_403_with_a_csrf_message(self):
        """spec §5.1-11 / §4.6：403 有三个来源，必须靠 `msg` 可区分。

        本方法钉的是其中**第二**个来源——DRF 的 CSRF 失败
        （`SessionAuthentication.enforce_csrf` → `PermissionDenied('CSRF Failed: ...')`，
        `rest_framework/authentication.py:148`）。它在之前**没有任何测试**：
        `test_p5_credential_plane.py` 的第 8 条只在 docstring 里提到「裸 POST 会被 CSRF
        挡成 403」，实际每次都带足 token，于是这条路径从未被走到。

        三条证据合起来才有判别力（只断 403 会让「因为别的原因被拒」照样绿）：

        1. `msg` 里含 `CSRF`——这是与「已认证但无权限」那一半的分界：403 的三个来源里
           **两个**都含 `CSRF`（本项目的 `csrf_failure`「CSRF 校验失败」、DRF 的
           `enforce_csrf`「CSRF Failed: ...」），**无权限**那条不含；
        2. `code == 403` 且**不带** `WWW-Authenticate`（那是 401 的挑战头，403 不该有）；
        3. 副作用的载体（`Group(name="probe-write")`）**不存在**——拒绝的语义是不进入视图。

        §5.1-11 的**另一半**（已认证但无权限的 `PermissionDenied`）需要「要求某职位」的
        端点，它属于 Task 9（`apps/core/permissions.py::HasRole`）；本任务不提前造那个产物，
        该半边顺延到 Task 9 的 `test_p5_rbac.py`（见 task-8-report）。
        """
        client = self._authenticated_client()
        response = client.post("/__probe__/write")           # 故意不带 X-CSRFToken
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()["code"], 403)
        self.assertIn(
            "CSRF",
            response.json()["msg"],
            "403 的 msg 必须能指出 CSRF 这个来源，否则与「已认证但无权限」的 403 同形",
        )
        self.assertEqual(response.json()["data"], None)
        self.assertIsNone(response.headers.get("WWW-Authenticate"))
        with tenant_context(self.envelope_tenant):
            # 按具体行断言（§5.2-3：不用 `count()` 做否定断言），且必须在**目标 schema
            # 内**查——写到别的 schema 去的话这个否定断言恒真。
            self.assertFalse(
                Group.objects.filter(name="probe-write").exists(),
                "视图被执行了——CSRF 拒绝必须发生在进入视图之前",
            )

    @override_settings(ROOT_URLCONF="tests.p5_probe_urls")
    def test_11_authenticated_write_with_a_valid_token_succeeds(self):
        """第 11 条 CSRF 半边的**对照**（§5.2-4）：同一个会话 + 合法 token → 200 且真写入。

        没有它，上面那条 403 就有了第二个解释：会话根本没被认出来、或者 token 机制
        整体坏掉（那样「不带 token 被拒」也是绿的，但拒的理由不是 CSRF）。
        对照要求副作用**真的**发生，所以它不仅证明 403 有判别力，也证明被拒的
        那是**同一个**接口（而不是一个永远拒绝的探针）。
        """
        client = self._authenticated_client()
        bootstrap = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True)
        bootstrap.get("/api/auth/csrf")
        token = bootstrap.cookies["csrftoken"].value
        # 把 token 以 cookie + 请求头**成对**给上（D15 的真实流程）。
        client.cookies["csrftoken"] = token
        response = client.post("/__probe__/write", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json(), {"ok": True})
        with tenant_context(self.envelope_tenant):
            self.assertTrue(
                Group.objects.filter(name="probe-write").exists(),
                "对照失败：合法 token 的写请求没有落库，上面那条 403 没有判别力",
            )

    # ── 13：裸 POST 登录必须返回 JSON，不是 Django 的 HTML 403 ────
    # spec §5.1-13 的两个半边都**已由既有用例覆盖**，本文件不重复实现（brief 的
    # 去重裁决），两个半边分别落在：
    #   - 失败半边（裸 POST → JSON 403 + code:403 + 逐字节信封）：
    #     `LoginEndpointTests.test_login_without_csrf_token_returns_json_403`；
    #   - 成功半边（带合法 token → 200）：
    #     `LoginEndpointTests.test_login_success_and_me`（`_login` helper 先取
    #     `csrftoken` 再放进 `X-CSRFToken`，且 client 带 `enforce_csrf_checks=True`，
    #     所以那个 200 是在 CSRF **真的**开着的情况下拿到的）。
    # 两边合起来正是 spec 要的「同一请求，有无 token 的差别」。再写一个把两半拼起来的
    # 方法只会在同一份契约上留第二份拷贝，故此处**刻意留空**。

    # ── 14：跨站形态的登出不得生效 ──────────────────────────────
    def test_14_cross_site_logout_without_token_keeps_the_session(self):
        """spec §5.1-14 / D15：跨站形态的登出必须被拒，**且会话仍然有效**。

        ⚠️ **与 Task 5 的 `test_logout_is_idempotent` 后半段同题**（已认证 → 不带 token
        的登出 → 403 → `/me` 仍 200）。保留一条的**唯一**理由是攻击形态更贴真，不是复制：
        **两条请求都没带任何 token**，判定机制也是同一条
        （DRF `SessionAuthentication.enforce_csrf` 调 Django 的
        `CsrfViewMiddleware.process_view`，失败即 `PermissionDenied('CSRF Failed: ...')`，
        `rest_framework/authentication.py:145-148`）——所以本方法**不**声称「请求头形态 vs
        form 形态」是两条代码路径。它与 Task 5 那半段的实际差别只有两点：

        1. **请求体形态**：这里是 `application/x-www-form-urlencoded`（跨站 HTML form 唯一
           发得出的形态，token 只可能落在 body 的 `csrfmiddlewaretoken` 字段），Task 5 那条
           不带 data（默认 multipart 形态）。**拒因**也随 client 构造而不同（实测）：
           本方法的 client 没有 `csrftoken` cookie → `CSRF Failed: CSRF cookie not set.`
           （`django/middleware/csrf.py:362`）；Task 5 的 client 带着登录下发的 cookie、
           只是没把 token 放进请求头 → `CSRF Failed: CSRF token missing.`（同文件 `:386`）。
           两条都只是同一条把关的两种拒因；
        2. **更强的前置/后置断言**：先证明这条会话**本来**有效（`/me` → 200），被拒后再断言
           `/me` 仍 200 **且 id 就是本人**（Task 5 那条只断状态码）。

        **判别力边界（实测，别把这条当成 Session 存活性红线）**：本方法对
        「已认证的裸 POST 被 DRF 的 `enforce_csrf` 拒」这件事与 Task 5 那半段**等价**，
        下面的变异实验证明了这一点：

        - 「摘掉 `TenantBoundAuthentication` 对 CSRF 的把关」→ 本方法与第 11 条**双双变红**
          （200 != 403），说明 403 这半边真实承重；
        - 「给 `LogoutView` 补 `csrf_protect`」→ 本方法**仍绿**。原因是 Django 的
          `CsrfViewMiddleware._reject`（`django/middleware/csrf.py:209-219`）只调
          `_get_failure_view()` 并记日志，**不会** flush 会话，所以两种拒绝的可观测结果
          完全一样（403 + 会话有效）。
          那个变异实际由 Task 5 的 `test_logout_is_idempotent` 抓住——它破坏的是
          「**未登录**也 200」的幂等语义（匿名裸 POST 变 403），不是跨站保护。
          所以本方法**不声称**能抓那个变异。

        会话用 `materialize_session` 手工造：真实登录会 `rotate_token()` 并下发
        `csrftoken` cookie，随后「不带 token」就不成立了。
        """
        with tenant_context(self.envelope_tenant):
            user = get_user_model().objects.create_user(username="keep", password="pw")
            user_pk, pw_hash = user.pk, user.password
        session_id = materialize_session(
            self.envelope_tenant.schema_name,
            user_pk=user_pk,
            password_hash=pw_hash,
            plane=self.envelope_tenant.schema_name,
        )
        client = Client(SERVER_NAME=DOMAIN_ENVELOPE, enforce_csrf_checks=True)
        client.cookies["sessionid"] = session_id
        # 先证明这条会话**本来**是有效的，否则下面的 200 可能是巧合、上面的 403 可能是
        # 「会话没被认出来」而不是「CSRF 被拒」（E-0003）。
        self.assertEqual(client.get("/api/auth/me").status_code, 200)

        rejected = client.post(
            "/api/auth/logout",
            data={},
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(rejected.status_code, 403, rejected.content[:200])
        self.assertEqual(rejected.json()["code"], 403)
        self.assertIn("CSRF", rejected.json()["msg"])
        # 副作用断言：会话必须**仍然**有效（这是"跨站登出没生效"的真正判据，
        # 不是那个 403——理由见 docstring 的判别力边界）。
        still_in = client.get("/api/auth/me")
        self.assertEqual(still_in.status_code, 200, still_in.content[:200])
        self.assertEqual(still_in.json()["data"]["user"]["id"], user_pk)


class SessionTablePlacementTests(TenantTestCase):
    """spec §5.1 第 15 条：`django_session` 只落在 `public`，租户 schema 里**没有**它。

    **为什么它是承重墙**（spec §3.1）：会话行只记用户主键，而"用户表"由 Host 决定——
    正因如此，一条会话才可能被搬到别的平面上去冒充（E-0012 实测）。把
    `django.contrib.sessions` 从 `SHARED_APPS` 挪进 `TENANT_APPS` 会同时改变
    "会话存哪"与"跨平面重放是否可能"，所以这条钉的是**落点本身**，不是某个函数的返回值。

    判别力按 §5.2 的三条要求写：

    - **§5.2-3 不用 `count()` 做否定断言**：查询直接按 `(table_schema, table_name)`
      取**具体行**，否定断言是"这个元组不存在"，不是"数量为 0"；
    - **§5.2-4 断言选中的 schema 名**：返回的每一行都带 `table_schema`，断言里比对它，
      而不是只回一个布尔；
    - **阳性对照**：同一次查询里 `business_game`（`TENANT_APPS` 专属）在租户 schema
      **有**、在 `public` **没有**——它证明"空列表"不是 schema 名/表名写错的恒真，
      也证明查询确实是按 schema 键控的。
    """

    def _tables_in(self, schema, table_name):
        """按具体行返回 `(table_schema, table_name)`（§5.2-3：否定断言不得用 count()）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_schema, table_name FROM information_schema.tables"
                " WHERE table_schema = %s AND table_name = %s",
                [schema, table_name],
            )
            return cursor.fetchall()

    def test_15_django_session_lives_only_in_the_public_schema(self):
        tenant_schema = self.tenant.schema_name
        # 断言**选中的 schema 名**（§5.2-4）：下面两条否定断言都挂在它上面。
        self.assertEqual(tenant_schema, connection.schema_name)
        self.assertNotEqual(tenant_schema, "public")

        self.assertEqual(
            self._tables_in("public", "django_session"),
            [("public", "django_session")],
            "会话表必须存在于 public——不在 public 就说明会话落点被改动了（spec §3.1）",
        )
        self.assertEqual(
            self._tables_in(tenant_schema, "django_session"),
            [],
            f"租户 schema {tenant_schema!r} 里出现了 django_session："
            "会话不再集中存储，跨平面重放的机制前提变了（spec §5.1-15）",
        )

        # 阳性对照：证明真的检到了 tenant_schema 这个 schema，且查询按 schema 键控。
        self.assertEqual(
            self._tables_in(tenant_schema, "business_game"),
            [(tenant_schema, "business_game")],
        )
        self.assertEqual(self._tables_in("public", "business_game"), [])
