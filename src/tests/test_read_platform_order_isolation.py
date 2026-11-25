"""读单端点的跨租户隔离（`AGENTS.md` §4 硬规则：任何新增查询面都要有一条
「以租户 A 的凭据访问 B 的数据必须失败」的回归）。

⚠️ **夹具体例照 Task 3 已经跑通的那一套**（`src/tests/test_shop_binding_api.py:199-288`），
**不是**实施计划里的示意代码：计划写的是 `self.other_tenant` / `make_logged_in_client(tenant=...)`
——两者都不存在（`make_logged_in_client` 的真实签名是
`(domain="tenant.test.com", *, username=..., password=..., groups=(), is_superuser=False)`，
**没有** `tenant=` 关键字）。第二个租户在 `setUpClass` 里用 `provision_tenant` 建
（用例内部建 schema 会与外层事务相冲），请求发在 `tenant_context` 块**之外** + 显式
`SERVER_NAME=<自己或对方的域名>`。

⚠️ **判据是「租户内唯一且两租户取值不同」的业务值——授权码**（`TOKEN_A` / `TOKEN_B`），
**不是主键**：两个 schema 的主键序列**各自从 1 开始**，两租户的 `OrderSource` 主键**可能相同**
（P3b 实测过同名行都是 `1`）⇒ 拿主键做的否定断言恒真/恒假（`AGENTS.md` §5 的主键禁令）。
`OrderSource` 的 id 只是入参（API 本来就要它），真正被断言的「看得见谁的数据」是
**上游收到的授权码**；另加一条独立判据是**上游调用时连接的 schema 名**。

⚠️ **两个方向都要有非空哨兵**：先证明 A 用到了 A 自己的授权码、B 用到了 B 自己的授权码，
「B 没用 A 的」这条否定断言才不是被空结果凑出来的。

⚠️ **请求一律发在 `tenant_context(...)` 块外**：块内发请求时
`connection.schema_name` 已经是被测租户 ⇒ 用例的通过与否不取决于「中间件有没有按域名切 schema」。
块外 + 显式 `SERVER_NAME` 后，schema 只能由中间件按域名决定；中间件若没切，B 的请求会被
会话守卫中间件（`apps/core/middleware.py`）或 `TenantBoundAuthentication._assert_plane`
（挂在每个 DRF 视图上，不依赖中间件）拒成 401 ⇒ 本条仍然红。
⚠️ **别拿「改坏 `main.py:52`」当判别力证据**：`main.py:36` 的 `set_schema_to_public()` 在 `:52`
之前执行，变异下**每个**请求（含登录夹具）都在 public 跑、租户用户不在 public 的 `auth_user`
里 ⇒ 夹具先 401，修复前后都红，区分不了。
（两道守卫都在时，「请求跑在 A 上、于是回到 A 的行」这一层走不到——那是哨兵，不是装饰。）
"""
from unittest import mock

from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, ShopBinding
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/read-platform-order/"
#: 租户 A = `TenantTestCase` 的 `test` schema 的域名（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "read-order-iso-b.test.local"
SCHEMA_B = "read_order_iso_b"
#: 租户 A = `TenantTestCase` 建的 `test` schema（`get_test_schema_name()` 的默认值）。
SCHEMA_A = "test"

# ── 判据用的业务值（租户内唯一、两租户取值不同；**不是主键**）──────────────
TOKEN_A = "授权码-只属于-A"
TOKEN_B = "授权码-只属于-B"


# ⚠️ 与 `test_read_platform_order.py` 同因：本机没有 AGISO 凭证，而「凭证为空 ⇒ 1015」是正确行为
#    ⇒ 不给这一层，B 侧的成功哨兵会变成 1015，用例测不到隔离。
#    ⚠️ 类级 `@override_settings` 在 `TenantTestCase` 上**不生效**（它的 `setUpClass` 没转调
#    `SimpleTestCase.setUpClass`），所以在 `setUp` 里显式 `enable()` + `addCleanup(disable)`。
class ReadPlatformOrderIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="读单隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        creds = override_settings(AGISO_APP_ID="app", AGISO_APP_SECRET="sec")
        creds.enable()
        self.addCleanup(creds.disable)
        # ⚠️ 跨域名请求不会在响应结束时还原 schema（见 `test_p3b_isolation.py` 的模块说明），
        #    所以每条用例开头把连接显式钉回租户 A。
        connection.set_tenant(self.tenant)

    def _post_as_b(self, client, payload):
        """在 B 的域名上 POST，**结束后把连接钉回租户 A**。"""
        try:
            return client.post(URL, payload, content_type="application/json", SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def _recording_upstream(self, seen):
        """打桩并**记录上游调用时的「授权码 + 连接 schema」**。

        schema 是隔离边界的直接观测点：如果 B 的请求其实跑在 A 的 schema 上，
        这里记到的就是 `test` 而不是 `SCHEMA_B` —— 那是与「授权码是不是 B 的」相互独立的
        第二条判据（两道守卫都在时请求会在进视图前 401，所以这一层平时走不到；
        但守卫若被改成「放行且不切 schema」，这条仍然抓得住）。
        """
        def fake(_tid, token, **_kwargs):
            seen.append((token, connection.schema_name))
            return {"payment": 1}

        return mock.patch(
            "apps.business.api.platform_order_views.order_detail", side_effect=fake
        )

    def test_each_tenant_reads_only_its_own_binding(self):
        with tenant_context(self.tenant):
            source_a = OrderSource.objects.create(name="A源", shop_type=20)
            ShopBinding.objects.create(order_source=source_a, access_token=TOKEN_A)
            a_client = make_logged_in_client(domain=DOMAIN_A, username="a-read-iso")
        with tenant_context(self.tenant_b):
            source_b = OrderSource.objects.create(name="B源", shop_type=20)
            ShopBinding.objects.create(order_source=source_b, access_token=TOKEN_B)
            b_client = make_logged_in_client(domain=DOMAIN_B, username="b-read-iso")

        # ── A 侧：正向对照 + 非空哨兵（A 确实用到了 A 自己的授权码、跑在 A 的 schema 上）
        seen_a = []
        with self._recording_upstream(seen_a):
            a_body = a_client.post(
                URL, {"order_source": source_a.id, "tid": "1"},
                content_type="application/json", SERVER_NAME=DOMAIN_A,
            ).json()
        self.assertEqual(a_body["code"], 0, a_body)
        self.assertEqual(seen_a, [(TOKEN_A, SCHEMA_A)])

        # ── B 侧：只用自己的绑定（判据是授权码这个业务值，不是主键）──────────
        seen_b = []
        with self._recording_upstream(seen_b):
            b_body = self._post_as_b(b_client, {"order_source": source_b.id, "tid": "1"}).json()
        self.assertEqual(b_body["code"], 0, b_body)          # 非空哨兵：B 读得动自己的配置
        self.assertEqual(len(seen_b), 1)
        self.assertEqual(seen_b[0][0], TOKEN_B)
        self.assertNotEqual(seen_b[0][0], TOKEN_A)
        self.assertEqual(seen_b[0][1], SCHEMA_B)             # B 的请求确实跑在 B 的 schema 上
        # ⚠️ 为什么不用 `OrderSource.id` 当判据：两个 schema 的主键序列**各自从 1 开始**
        #    （P3b 实测两个租户的同名行都是 `1`），`id` 在 B 里命中的可能是 B 自己的行
        #    ⇒ 拿主键做的否定断言恒真/恒假。所以判据取授权码这个业务值。
