"""店铺对接（`ShopBinding`）测试。

本文件由 Task 2 建立，**只放模型约束用例**；Task 3 往同文件**追加** API 用例
（`ShopBindingApiTests` 与 `ShopBindingIsolationTests`），因此这里不做任何文件级
setup / 副作用。
"""
from django.db import connection
from django.test import Client, override_settings
from django.urls import resolve
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, ShopBinding
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 店铺对接 CRUD 的集合端点（spec §6.1，router 注册）。
URL = "/api/business/shop-bindings/"

#: 店铺对接全家端点的 RBAC 节点（E-0253：与前端门同一个点——页面/端点同宽）。
#: ⚠️ 写**字面量**（E-0024）：引前端常量或目录常量都是自指。
NODE = "system.pinduoduo_settings"

#: 平台级配置端点（`detail=False` 的 action）：与「行」解耦的 AppId 出口。
PLATFORM_CONFIG_URL = "/api/business/shop-bindings/platform-config/"

#: 租户 A = `TenantTestCase` 的 `test` schema 的域名（与 `test_p3b_isolation.py` 同口径）。
DOMAIN_A = "tenant.test.com"
#: 隔离用例的第二个租户。域名后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS`
#: 白名单里（与 `tests/test_p3b_isolation.py` 的 `p3b-iso-*` 同口径）。
DOMAIN_B = "shop-bind-iso-b.test.local"
SCHEMA_B = "shop_bind_iso_b"


class ShopBindingModelTests(TenantTestCase):
    def test_defaults_are_empty_and_platform_defaults_to_xianyu(self):
        row = ShopBinding.objects.create()
        # ⚠️ 期望值写**字面 20**（不是 `ShopType.XIANYU`）：后者的数值漂移时，
        #    自指断言（被测的符号 === 钉住的符号）不会红（`AGENTS.md` §5 / E-0024）。
        #    对标与前端两侧都硬编码 `20`，它才是真正的锚点。
        self.assertEqual(row.platform, 20)
        self.assertEqual(row.access_token, "")
        self.assertEqual(row.shop_id, "")
        self.assertIsNone(row.order_source)

    def test_deleting_order_source_nulls_the_binding_not_blocks_it(self):
        """D-0042 允许删来源；绑定行必须**置空**而不是被 PROTECT 挡住。"""
        source = OrderSource.objects.create(name="闲鱼A")
        row = ShopBinding.objects.create(order_source=source)
        source.delete()
        row.refresh_from_db()
        self.assertIsNone(row.order_source)


class ShopBindingApiTests(TenantTestCase):
    """`/api/business/shop-bindings/` 的 CRUD（spec §6.1）。

    ⚠️ E-0253 起端点有逐 action 节点门（`system.pinduoduo_settings`）⇒ 本类的既有用例
    全部改持**该节点**（夹具的组/节点口径见 `make_logged_in_client`：节点挂在
    `Group → Role` 上，不给组就一个节点都挂不上）；期望值一个字没改。
    无节点 ⇒ 403 的判据在 `ShopBindingNodeGateTests`。
    """

    def setUp(self):
        self.client = make_logged_in_client(
            username="shop-admin", groups=("客服",), nodes=(NODE,),
        )

    def test_create_minimal_row(self):
        res = self.client.post(
            URL,
            {"platform": 20, "shop_name": "我的闲鱼店", "access_token": "tok-1"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["shop_name"], "我的闲鱼店")
        self.assertNotIn("AGISO_APP_SECRET", res.content.decode())

    def test_non_xianyu_platform_is_rejected_with_1000(self):
        res = self.client.post(URL, {"platform": 10}, content_type="application/json")
        self.assertEqual(res.json()["code"], 1000)

    def test_create_with_empty_token_is_allowed(self):
        """「空 = 不改」**只作用于 PATCH**：`POST` 时空授权码合法（spec §5.1：
        允许「先建行、后拿授权码」）。若把那条 PATCH 语义错放进写路径，这条会红。
        """
        res = self.client.post(
            URL,
            {"platform": 20, "shop_name": "空串授权码", "access_token": ""},
            content_type="application/json",
        )
        self.assertEqual(res.json()["code"], 0)
        self.assertEqual(ShopBinding.objects.get(shop_name="空串授权码").access_token, "")

        # 连键都不带也合法（建行时还没去阿奇索授权）
        res = self.client.post(
            URL, {"platform": 20, "shop_name": "省略授权码"}, content_type="application/json"
        )
        self.assertEqual(res.json()["code"], 0)
        self.assertEqual(ShopBinding.objects.get(shop_name="省略授权码").access_token, "")

    def test_patch_without_token_does_not_wipe_it(self):
        """⚠️ 客服「只改备注」保存一次**不得**把授权码清空（spec §6.1，E-0027 的同族）。"""
        row = ShopBinding.objects.create(access_token="tok-keep", remark="旧")
        res = self.client.patch(
            f"{URL}{row.id}/", {"remark": "新"}, content_type="application/json"
        )
        self.assertEqual(res.json()["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.access_token, "tok-keep")
        self.assertEqual(row.remark, "新")

    def test_patch_with_empty_string_also_keeps_token(self):
        row = ShopBinding.objects.create(access_token="tok-keep")
        self.client.patch(
            f"{URL}{row.id}/", {"access_token": ""}, content_type="application/json"
        )
        row.refresh_from_db()
        self.assertEqual(row.access_token, "tok-keep")

    def test_patch_with_new_token_overwrites_it(self):
        row = ShopBinding.objects.create(access_token="old")
        self.client.patch(
            f"{URL}{row.id}/", {"access_token": "new"}, content_type="application/json"
        )
        row.refresh_from_db()
        self.assertEqual(row.access_token, "new")

    def test_list_hides_the_token_and_exposes_the_boolean_plus_platform_label(self):
        """E-0253：授权码**不再明文回显**（旧契约「照对标明文回显」的风险登记就此作废）——
        读侧只有 `access_token_set` 布尔；平台列照旧要能渲染文案。
        （无字面值 / 布尔取值的端到端判据在 `ShopBindingNodeGateTests`。）
        """
        ShopBinding.objects.create(access_token="tok-visible", platform=20)
        body = self.client.get(URL).json()
        item = body["data"]["items"][0]
        self.assertNotIn("access_token", item)
        self.assertIs(item["access_token_set"], True)
        self.assertEqual(item["platform_label"], "闲鱼")

    def test_delete(self):
        row = ShopBinding.objects.create()
        self.assertEqual(self.client.delete(f"{URL}{row.id}/").json()["code"], 0)
        self.assertFalse(ShopBinding.objects.filter(id=row.id).exists())

    def test_malformed_query_params_do_not_500(self):
        res = self.client.get(URL, {"page": "abc", "platform": "abc"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("code", res.json())

    def test_put_is_not_allowed(self):
        """spec §6.1 的端点表只有 `GET/POST/PATCH/DELETE`，没有 PUT。

        PUT 是 router/`ModelViewSet` 默认多给的能力面，本资源**没有 PUT 消费方**（本页用
        PATCH）⇒ 没有 spec 依据的能力面不暴露。视图用 `http_method_names` 关掉它，这条钉住
        「关掉了」。⚠️ 理由**不是**「PUT 会清空授权码」：`ShopBindingSerializer.update()` 对
        缺省的 `access_token` 保留旧值，DRF 非 partial 的 `ModelSerializer.update` 也只
        `setattr` 请求里出现的键——真理由只有「spec 没有 + 没有消费方」这一条。
        """
        row = ShopBinding.objects.create(access_token="tok-keep")
        res = self.client.put(
            f"{URL}{row.id}/", {"platform": 20}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 405, res.content[:200])
        self.assertEqual(res.json()["code"], 405)
        row.refresh_from_db()
        self.assertEqual(row.access_token, "tok-keep")

    def test_patch_with_null_token_is_a_validation_error(self):
        """`null` **不是**「不改」：`access_token` 是 `allow_null=False` 的字符串列
        ⇒ DRF 判类型错，回 `1000`（既不静默清空、也不 500）。

        现行行为钉在这里，免得后来者把 `null` 当成「不改」实现——那会让「清空授权码」
        变成一条可用的路径，而「空 = 不改」的**唯一**出口是重新授权替换。
        """
        row = ShopBinding.objects.create(access_token="tok-keep")
        res = self.client.patch(
            f"{URL}{row.id}/", {"access_token": None}, content_type="application/json"
        )
        self.assertEqual(res.json()["code"], 1000, res.content[:200])
        row.refresh_from_db()
        self.assertEqual(row.access_token, "tok-keep")

    def test_list_item_exposes_exactly_the_contract_fields(self):
        """列表 item 的键集**钉死**（E-0253 起 12 个字段逐一列出：明文 `access_token`
        被 `access_token_set` 布尔替掉）。

        ⚠️ 下面 `test_platform_app_id_...` 里那两条键名断言只抓得住「字段名里带
        `app_secret`」的漏法；将来新增一个叫 `platform_secret` 的字段照样能溜过去。
        把键集钉死后，**任何**字段增减都会让这条红——契约变更必须是有意识的
        （Task 6 的前端就按这个键集读）。
        """
        ShopBinding.objects.create(access_token="tok")
        item = self.client.get(URL).json()["data"]["items"][0]
        self.assertEqual(
            set(item.keys()),
            {
                "id", "platform", "platform_label", "platform_app_id", "shop_name",
                "shop_id", "order_source", "access_token_set", "remark",
                "created_at", "updated_at",
            },
        )

    @override_settings(AGISO_APP_ID="app-123", AGISO_APP_SECRET="secret-do-not-leak")
    def test_platform_app_id_is_exposed_but_the_secret_never_is(self):
        """弹层要回填平台 AppId（spec R9），而**平台密钥一次都不许出境**（spec §9-S3）。"""
        ShopBinding.objects.create(access_token="tok")
        raw = self.client.get(URL).content.decode()
        body = self.client.get(URL).json()
        self.assertEqual(body["data"]["items"][0]["platform_app_id"], "app-123")
        self.assertNotIn("AGISO_APP_SECRET", raw)
        self.assertNotIn("app_secret", raw)
        # ⚠️ 上面两条只钉**键名**；真正要钉的是**值**（`AGENTS.md` §5：断言要钉值，
        #    不要钉常量）。测试环境里 `AGISO_APP_SECRET` 默认是空串 ⇒ 只查键名是近乎
        #    空转的断言，所以这里显式给一个非空密钥、并断言它一个字都不出现在响应里。
        self.assertNotIn("secret-do-not-leak", raw)

    # ── 平台级配置端点（`platform-config`）：与「行」解耦的 AppId 出口 ──────────────

    @override_settings(AGISO_APP_ID="app-xyz")
    def test_platform_config_returns_the_app_id_with_zero_bindings(self):
        """**零绑定**租户也要能读到平台 AppId——这就是这个端点存在的全部理由。

        逐行的 `platform_app_id` 在列表为空时给不出值，新租户因此看不到自己要拿去
        阿奇索授权的 AppId（本用例钉住修复后的行为）。

        ⚠️ 断言写**字面量** `"app-xyz"`，不写 `settings.AGISO_APP_ID`：后者是被测代码
        取值用的同一个符号，它被改坏时断言不会红（自指断言，`AGENTS.md` §5 / E-0024）。
        """
        # 前置事实：真的一条对接都没有（否则判别力会来自「有行」，而不是「与行解耦」）。
        self.assertEqual(ShopBinding.objects.count(), 0)
        res = self.client.get(PLATFORM_CONFIG_URL)
        self.assertEqual(res.status_code, 200, res.content[:200])
        body = res.json()
        self.assertEqual(body["code"], 0)
        # 整个 `data` 钉死：今后往这个端点加字段必须是有意识的（前端按这个形状读）。
        self.assertEqual(body["data"], {"platform_app_id": "app-xyz"})

    def test_platform_config_route_is_not_swallowed_by_the_detail_route(self):
        """`platform-config` 没被 `{pk}` 详情路由抢走（`detail=False` 的 action 先注册）。

        判据不靠 404 间接推：直接看解析到的 view 的动作。若它落进详情路由，
        `lookup_value_regex` 会把这串字面路径当成主键 ⇒ 解析到 `retrieve`。
        """
        match = resolve(PLATFORM_CONFIG_URL)
        self.assertEqual(
            match.func.actions.get("get"), "platform_config", match.func.actions
        )

    def test_platform_config_requires_authentication(self):
        """匿名 ⇒ **401**（与列表同形：吃 fail-closed 的默认权限类，spec D11）。

        ⚠️ 用**裸 client**（`setUp` 里那个是已登录的）：不显式带域名会落到 public schema
        ⇒ 测的是别的东西；这里显式给租户 A 的域名。
        """
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        res = anonymous.get(PLATFORM_CONFIG_URL)
        self.assertEqual(res.status_code, 401, res.content[:200])
        self.assertEqual(res.json()["code"], 401)

    @override_settings(AGISO_APP_ID="app-xyz", AGISO_APP_SECRET="secret-platform-config-unique")
    def test_platform_config_never_leaks_the_secret(self):
        """新端点同样**不带出 AppSecret**（spec §9-S3：平台密钥一次都不进浏览器）。

        与逐行那条用例同一纪律：密钥给一个**非空字面量**再断言它不出现——
        空串会让 `assertNotIn` 恒真（空转断言）。
        """
        ShopBinding.objects.create(access_token="tok")
        raw = self.client.get(PLATFORM_CONFIG_URL).content.decode()
        # 正向哨兵：先证明下面查的确实是这个端点的响应（AppId 在、密钥不在）。
        self.assertIn("app-xyz", raw)
        self.assertNotIn("secret-platform-config-unique", raw)
        self.assertNotIn("AGISO_APP_SECRET", raw)


class ShopBindingNodeGateTests(TenantTestCase):
    """E-0253 回归钉：`/api/business/shop-bindings/` 的**逐 action 节点门**。

    修前形状（E-0253 的现场）：全套 CRUD 只吃默认权限类（`IsAuthenticated`）——前端把页面
    藏在 `system.pinduoduo_settings` 节点后，后端却没闸（「前端藏了、后端没闸」）。

    两条判据（计划书钉子原文）：
    ① 无节点账号 GET / POST / PATCH / DELETE 全 **403** 且**零写入**（断言打在数据库行上）；
    ② 有节点账号的列表响应里**不含 access_token 字面值**、含 `access_token_set` 布尔。
    """

    def test_no_node_account_gets_403_on_all_four_actions_and_zero_writes(self):
        """无节点账号：四动作全 403；POST 没建行、PATCH 没改行、DELETE 没删行。"""
        # 不传 `nodes` ⇒ 默认不挂任何节点（fail-closed，夹具 docstring 的口径）。
        client = make_logged_in_client(username="no-node-shop-user")

        # GET（列表）——修前是 200。
        self.assertEqual(client.get(URL).status_code, 403, "无节点账号的列表请求没有被 403")

        # POST（新增）——修前是 200 且建行。
        created = client.post(
            URL, {"platform": 20, "shop_name": "闸外店"}, content_type="application/json"
        )
        self.assertEqual(created.status_code, 403, "无节点账号的新增请求没有被 403")

        # PATCH（编辑）/ DELETE（删除）——打在**真实存在的一行**上（直接建行当夹具）。
        row = ShopBinding.objects.create(shop_name="闸内店", access_token="tok-inside", remark="旧")
        patched = client.patch(
            f"{URL}{row.id}/", {"remark": "改"}, content_type="application/json"
        )
        self.assertEqual(patched.status_code, 403, "无节点账号的编辑请求没有被 403")
        deleted = client.delete(f"{URL}{row.id}/")
        self.assertEqual(deleted.status_code, 403, "无节点账号的删除请求没有被 403")

        # 零写入（断言打在数据库行，`AGENTS.md` §5）：库里还是夹具那一行、值没动。
        self.assertEqual(ShopBinding.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.remark, "旧")
        self.assertEqual(row.access_token, "tok-inside")
        self.assertFalse(ShopBinding.objects.filter(shop_name="闸外店").exists())

    def test_node_account_list_has_no_token_literal_and_has_the_boolean(self):
        """有节点账号：列表照常 200，但授权码**字面值**不出现在响应里，取而代之的是布尔。

        canary 用非空合成值 ⇒ `assertNotIn` 不是空转；布尔用 `assertIs`（真 `bool`，不是真值）。
        ⚠️ 不对**整段响应文本**断言 `"access_token"` 键名不在——`access_token_set` 里含有
        `access_token` 这个子串，那种断言恒红；键的不在断言打在**单个 item** 上。
        """
        client = make_logged_in_client(
            username="node-shop-user", groups=("客服",), nodes=(NODE,),
        )
        ShopBinding.objects.create(shop_name="有码店", access_token="tok-secret-E252", platform=20)
        ShopBinding.objects.create(shop_name="无码店", access_token="", platform=20)

        response = client.get(URL)
        self.assertEqual(response.status_code, 200, response.content[:200])
        raw = response.content.decode()
        self.assertNotIn("tok-secret-E252", raw)

        items = {item["shop_name"]: item for item in response.json()["data"]["items"]}
        self.assertCountEqual(items.keys(), {"有码店", "无码店"})
        self.assertIs(items["有码店"]["access_token_set"], True)
        self.assertIs(items["无码店"]["access_token_set"], False)
        # 明文键本身不再下发（键集的恰好断言另有用例；这里钉「这一格」的不在）。
        self.assertNotIn("access_token", items["有码店"])


class ShopBindingIsolationTests(TenantTestCase):
    """⚠️ `AGENTS.md` §4 的**硬性要求**：Task 3 新建了租户内的查询面（CRUD 列表）⇒
    **必须**补一条「以租户 A 的凭据访问 B 的数据必须失败」。

    夹具照 `src/tests/test_p3b_isolation.py` 的**真实**写法：第二个租户在 `setUpClass`
    里用 `provision_tenant` 建（在一个用例内部建 schema 会与外层事务相冲），
    `make_logged_in_client` 的签名是
    `(domain="tenant.test.com", *, username=..., password=..., groups=(), is_superuser=False)`
    —— **没有 `tenant=` 关键字**。否定断言用**租户内唯一且两租户取值不同**的业务值
    （`shop_name`），**不用主键**（两个 schema 的主键序列各自从 1 开始，
    `filter(id=...)` 会命中对方自己的行 ⇒ 断言恒真/恒假，见 `AGENTS.md` §5）。

    ⚠️ **请求一律发在 `tenant_context(...)` 块外**：
    块内发请求时 `connection.schema_name` **已经**是被测租户 ⇒ 这条用例的通过与否并不
    取决于「中间件有没有按域名切 schema」——一个**不切 schema、但也不改动连接**的中间件
    能让它全绿（**该假绿已实测**：连接与被测租户同 schema 时会话查得到 ⇒ 认证通过、
    列表回的就是 B 自己的行，与中间件切不切无关。落点与判别细节见
    `docs/specs/2026-09-21-shop-binding-and-platform-order-read-design.md` §12-18；
    ⚠️ 过程台账 `.superpowers/**` **不进 git**，不作为引用出处）。
    请求在块外 + 显式带自己的 `SERVER_NAME` 后：A 侧请求发出时连接是 A，B 侧请求发出时
    连接同样是 A（`tenant_context(tenant_b)` 退出时已把连接还原到 A；`_get_as_b` 只在
    **请求之后**的 `finally` 里才把连接钉回 A）⇒ **schema 只能由中间件按域名决定**。
    中间件若不切 schema，B 的列表请求就过不去，但**红的不是哨兵**：两道各自独立的守卫是
    `apps/core/middleware.py:31`（会话守卫中间件）与
    `apps/core/authentication.py:26-27`（`TenantBoundAuthentication._assert_plane`，
    经 `settings.DEFAULT_AUTHENTICATION_CLASSES` 挂在每个 DRF 视图上，**不依赖中间件**）。
    ⚠️ **把这两道都拆掉，红灯也仍然落在下面 `assertEqual(b_body["code"], 0)` 那一行**
    （实测：`AssertionError: 401 != 0 : {'code': 401, 'msg': '身份认证信息未提供。',
    'data': None}`）——B 的会话存在 B 的 schema 里，而请求跑在 A 上 ⇒ 会话查不到 ⇒
    请求是**匿名**的，守卫里 `"_auth_user_id" in session` 那一句根本不成立。
    ⇒ 下面那两条（非空哨兵 / `assertNotIn`）**不是**这个变异的兜底：它们只在
    「列表真的回了本租户的行」时才有判别力，请求匿名时一行都走不到。
    对本条用例更准的说法是：**它对「中间件不切 schema」仍然是红的**（红在 401 那条断言上，
    证明的是「请求没被认证」，不是「隔离生效」）；哨兵那两条管的是另一类变异
    ——schema 切对了、但查询/序列化把别人的行带了出来。

    ⚠️ **别拿「改坏 `main.py:52`」当判别力证据**：`main.py:36` 的
    `set_schema_to_public()` 在 `:52` **之前**执行 ⇒ 变异下**每个**请求（登录也是请求）
    都在 public 跑，而租户用户不在 public 的 `auth_user` 里 ⇒ **登录夹具先 401**；
    修复**前**的版本同样会红。那个变异**区分不了修复前后**，它证明不了本条用例的判别力。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="店铺对接隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 跨域名请求不会在响应结束时还原 schema（见 `test_p3b_isolation.py` 的模块说明），
        #    所以每条用例开头把连接显式钉回租户 A（`TenantTestCase` 没有 `setUp` 做这件事）。
        connection.set_tenant(self.tenant)

    def _get_as_b(self, client):
        """在 B 的域名上请求，**结束后把连接钉回租户 A**（照 `test_p3b_isolation.py` 的
        `_get_b()`：跨域名请求会把连接留在 B）。
        """
        try:
            return client.get(URL, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def test_other_tenants_binding_is_not_listed(self):
        with tenant_context(self.tenant):
            ShopBinding.objects.create(shop_name="A-独有的店铺", access_token="A-token")
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="a-iso-user", groups=("客服",), nodes=(NODE,),
            )
        # ── 请求在块**外**（见类 docstring：块内会让隔离变成恒等式）────────────
        a_body = a_client.get(URL, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_body["code"], 0, a_body)
        # 非空哨兵：先证明 A **能看到自己的行**，下面的否定断言才有意义
        self.assertIn("A-独有的店铺", [r["shop_name"] for r in a_body["data"]["items"]])

        with tenant_context(self.tenant_b):
            ShopBinding.objects.create(shop_name="B-自己的店铺", access_token="B-token")
            b_client = make_logged_in_client(
                domain=DOMAIN_B, username="b-iso-user", groups=("客服",), nodes=(NODE,),
            )
        b_body = self._get_as_b(b_client).json()
        self.assertEqual(b_body["code"], 0, b_body)
        names = [r["shop_name"] for r in b_body["data"]["items"]]
        self.assertIn("B-自己的店铺", names)      # 非空哨兵
        self.assertNotIn("A-独有的店铺", names)    # 按**业务值**判，不按主键

    def test_a_session_on_b_host_is_rejected(self):
        """凭据面：**A 的会话 + B 的域名 ⇒ 401**（中间件/认证层短路，未进视图）。

        照 `src/tests/test_p3b_isolation.py:327-342` 的先例。正向对照：**同一个** client
        回到自己的域名必须 200——否则这条 401 可能由「这个会话压根不可用」凑出来，
        与域名绑定无关。
        """
        with tenant_context(self.tenant):
            ShopBinding.objects.create(shop_name="A-凭据面的店铺")
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="a-plane-user", groups=("客服",), nodes=(NODE,),
            )

        on_own_host = a_client.get(URL, SERVER_NAME=DOMAIN_A)
        self.assertEqual(on_own_host.status_code, 200, on_own_host.content[:200])
        self.assertEqual(on_own_host.json()["code"], 0)

        crossed = self._get_as_b(a_client)
        self.assertEqual(crossed.status_code, 401, crossed.content[:200])
