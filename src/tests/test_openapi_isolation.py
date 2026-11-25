"""T6：开放平台的**跨租户隔离**与**兜底闸**判据（spec §11.2 点名的那份 `test_openapi_isolation.py`）。

    ## 本文件证什么（四组，每一组都写明了"它怎么才会红"）

    A. **凭据层**：租户 A 的 appKey / secret 打**租户 B 的域名** ⇒ **401**
    （`ApiApp` 是**每个 schema 一份**的表，B 里根本没有这个 appKey ⇒ 与「签名错」同一句）。
    ⚠️ 配**两条正向对照**：A 的凭据打 A 的域名 ⇒ 200；**B 自己的凭据**打 B 的域名 ⇒ 200。
    没有它们，那个 401 可能由「凭据本来就坏 / 这条路对谁都不生效」凑出来（E-0004 同族）。
    B. **数据层**：A 的应用**查不到** B 的单（详情与来源单号两条路），且与「不存在」**同形**
    （§7.1：不给订单枚举面）。判据用**租户内唯一、且两租户取值不同**的订单号 / 来源单号。
    C. ⭐ **「这条判据真的能红」的证明**（`AGENTS.md` §5-G：写完做一次变异或换库验证）——
    两条，都在本文件里可重复跑：
    1. `test_the_order_no_criterion_flips_when_the_schema_changes`：**同一句判据换库（切 schema）
    后读数正好相反**；判据若是恒真的（比如查错了库、或两个租户的值撞了），那两句当场失败；
    2. `test_a_primary_key_cannot_identify_another_tenants_row`：**当场造出**「同一个 pk 在两个
    schema 里指两行不同的单」这个局面 ⇒ 说明为什么判据不能用主键（`AGENTS.md` §5-G 明文，
    且**写进规则后仍被踩过一次**）。
    D. ⭐ **兜底闸的租户段**（T6 新增生产代码的直接判据）：桶的键带 `connection.schema_name`
    ⇒ 租户 A 把桶打满后，**同 appKey** 的租户 B **一点不受影响**。
    ⚠️ 「超限怎么回（429 / `Retry-After`）」「401 先于 429、没带凭据的请求不进桶」那两条
    不是隔离判据，在 `tests/test_openapi_throttling.py` 里（⛔ 别把它们抄回来一份）。

    ## 凭据

    两条对外路径（集合 / 详情）都只认 **Bearer 令牌**（设计文档 §13.14-3：`PATCH` 与详情
    同路径 ⇒ 共用一套认证）；旧 HMAC 签名面已整体删除（§13.1）。401 那一档的文案是
    `BEARER_INVALID_CREDENTIALS_MSG`（与"凭据无效"不可分辨的同一句）。
    ⚠️ 本文件 A / B / C / D 四组的**判据一条未放宽**：它们全打在**跨租户**这一层
    （凭据 / 数据 / 桶），与本应用"被授权了什么"无关。

    ## 夹具纪律（与 `tests/test_openapi_order_api.py` 同款，⛔ 别删）

    - 凭据与订单号一律**合成值**（`AGENTS.md` §5-C），⛔ 与任何真实租户的数据无关。
    - 每条用例前 `cache.clear()`：**兜底闸的桶**与 §6.3 的**按来源 IP 失败计数**都落 `LocMemCache`，
    而它跨用例**不清空** ⇒ 上一组用例攒下的计数会让本组出现「合法请求也 401 / 429」这种
    **看起来像实现坏了**的假象。

    ⛔⭐ 改准（T3）：上面那句已作废—— **两样计数都换成 DB 行**
    （`OpenApiRateBucket`，见 `apps/openapi/rate_limit.py`）⇒ 它们由 `TenantTestCase` 的事务
    **每条用例回滚掉**，`cache.clear()` 不再承重（保留它是"只加不删"的纪律）。
    ⚠️ 本文件 D 组那条"桶按租户分"的判据**一字未改**：它现在由"行住在哪个 schema 里"承担
    （原由"键里带 schema"承担）—— 两条都是**跨租户隔离**的判据，⛔ 不是同一件事换了说法。
    - ⚠️ 跨域名请求结束后连接**停在那个租户的 schema 上**（`TenantMainMiddleware` 在请求进来时
    set、响应后**不还原**——实测，`tests/test_account_isolation.py` 里记着同一段）。
    ⇒ 本文件每个发请求的辅助函数都用 `try/finally` 把连接**钉回租户 A**，`setUp` 第一行也显式
    `set_tenant`（那一行挡的是**用例之间**的残留，`finally` 挡的是**同一条用例内部**的）。
    ⚠️ **如实标注**：今天这条 `finally` **不是承重逻辑**（每个用例里跨域名请求之后只剩
    `json.loads(response.content)` 这类不碰库的动作，去掉它本文件照样全绿）—— 它是**防御性**的：
    哪天有人在跨域名请求**之后**加一句 ORM 断言，没有它就**静默**去查别人的库
    （症状见 `tests/test_account_isolation.py` 的实测记录）。⛔ 别把它读成"去掉会红"。
"""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order
from apps.openapi import tokens
from apps.openapi.authentication import BEARER_INVALID_CREDENTIALS_MSG
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant

User = get_user_model()

#: 对外端点的两条路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
LIST_PATH = "/api/open/v1/orders/"
#: 详情路径的**模板**（`{order_no}` 由用例填）。
DETAIL_TEMPLATE = "/api/open/v1/orders/{order_no}/"

#: 租户 B 的域名与 schema。域名刻意与 A 不同（`Domain` 上有唯一约束，撞了 `provision_tenant` 会报错）。
DOMAIN_B = "openapi-iso-b.test.local"
SCHEMA_B = "openapi_iso_b"

#: 合成凭据（⛔ 不是任何真实租户的凭据）。
APP_KEY = "probeisokey00000001"
APP_SECRET = "probe-iso-secret-000000000000001"
#: B **自己**那把凭据（与 A 不同 ⇒ 才能证明"B 侧这条路是通的"）。
APP_KEY_B = "probeisokey00000002"
APP_SECRET_B = "probe-iso-secret-000000000000002"

#: 两侧的订单号 / 来源单号**取值不同**（同名会让断言失去判别力）。
ORDER_NO_A = "20261007000000000071"
ORDER_NO_B = "20261007000000000072"
SOURCE_NO_A = "TT-ISO-A-0001"
SOURCE_NO_B = "TT-ISO-B-0001"
#: C 组第 2 条用例在 A 里**故意**造的那张"同 pk"单。
TWIN_ORDER_NO = "20261007000000000073"

#: ⚠️ 两侧那张单**显式**给不同的 pk（⛔ 不靠自增）。
#: 为什么不靠自增：每个 schema 的主键序列**各自独立**，而本文件每个用例在两侧各只插一行
#: ⇒ 两侧会**恰好**拿到同一号；而 C 组第 2 条用例正是要**在 A 里造一张与 B 同 pk 的单**，
#: 那个号已经被 A 自己的单占了 ⇒ 插不进去（实测报的是 `business_order_pkey` 唯一约束冲突）。
#: 显式 pk 让"哪一行属于哪个租户"完全由本文件说了算，读数不依赖序列走到了哪儿。
#: ⚠️ 取远离序列的号段（序列从 1 开始），避免与别的行撞号。
A_ORDER_PK = 90000001
B_ORDER_PK = 90000002

#: 来源 IP（文档保留段，⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.21"

#: 业务码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）。
BUSINESS = 1002
#: 兜底闸超限的 HTTP 状态码与信封码（两者**同值**，本仓异常处理器对传输层失败的既定口径）。
THROTTLED_STATUS = 429


class OpenApiIsolationTests(TenantTestCase):
    """跨租户（A = `TenantTestCase` 的 `test` schema，B = `SCHEMA_B`）与兜底闸。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：schema 的 DDL **不能**回滚在 `TestCase`
        # 的事务里（与 `tests/test_account_isolation.py` 同一手法）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="开放平台隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行把连接钉回租户 A：上一条用例的跨域名请求会把连接留在那个租户的 schema 上。
        connection.set_tenant(self.tenant)
        super().setUp()
        # ⚠️ 必须清：兜底闸的桶与按 IP 失败计数都落 LocMemCache，跨用例不清空（见模块 docstring）。
        cache.clear()
        # ⚠️ `TenantTestCase.domain` 是 `Domain` **实例**（不是字符串）⇒ 取它的 `domain` 字段。
        self.domain_a = self.domain.domain

        self.game_a = Game.objects.create(name="隔离租户 A 的游戏")
        self.order_a = Order.objects.create(
            pk=A_ORDER_PK,
            game=self.game_a,
            order_no=ORDER_NO_A,
            source_order_no=SOURCE_NO_A,
            project_text="占位项目",
        )
        self.app_a = self._create_app(
            name="隔离租户 A 的应用",
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            username="openapi-iso-a",
        )

        try:
            with tenant_context(self.tenant_b):
                self.game_b = Game.objects.create(name="隔离租户 B 的游戏")
                self.order_b = Order.objects.create(
                    pk=B_ORDER_PK,
                    game=self.game_b,
                    order_no=ORDER_NO_B,
                    source_order_no=SOURCE_NO_B,
                    project_text="占位项目",
                )
                self.app_b = self._create_app(
                    name="隔离租户 B 的应用",
                    app_key=APP_KEY_B,
                    app_secret=APP_SECRET_B,
                    username="openapi-iso-b",
                )
        finally:
            connection.set_tenant(self.tenant)

        # ── 构造物自证：两侧的单**真的**各自落在自己的 schema 上（否则下面的负断言是空转）──
        self.assertEqual(self._count_here(ORDER_NO_A), 1)
        self.assertEqual(self._count_here(ORDER_NO_B), 0)
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._count_here(ORDER_NO_B), 1)
            self.assertEqual(self._count_here(ORDER_NO_A), 0)
        finally:
            connection.set_tenant(self.tenant)

    # ── C 组：判据"能红"的证明 ─────────────────────────────────────────
    def test_the_order_no_criterion_flips_when_the_schema_changes(self):
        """⭐ **换库验证**：同一句判据（按订单号数）**换一个 schema 读数正好相反**。

    这条是本文件 B 组那两条负断言的**判别力证明**：如果那条判据是恒真的
    （例如两个租户的订单号撞了、或查询压根没走当前 schema），下面 ② 里的两句
    **不可能同时成立** ⇒ 当场红。

    ⚠️ 与"凭记忆断言"的区别：这里的两组读数是在**同一次运行**里、用**同一个查询形状**、
    只换连接所属的 schema 取到的 —— 换库这一步就是"把租户上下文弄错"的那个对照。
"""
        # ① 租户 A 的 schema：A 的单在、B 的单不在
        self.assertEqual(self._count_here(ORDER_NO_A), 1)
        self.assertEqual(self._count_here(ORDER_NO_B), 0)
        # ② 换到租户 B 的 schema：**同一句判据**读数相反（这就是"能红"的证据）
        try:
            connection.set_tenant(self.tenant_b)
            self.assertEqual(self._count_here(ORDER_NO_B), 1)
            self.assertEqual(self._count_here(ORDER_NO_A), 0)
        finally:
            connection.set_tenant(self.tenant)

    def test_a_primary_key_cannot_identify_another_tenants_row(self):
        """⚠️ 为什么本文件的判据用**订单号**而⛔ 不是主键（`AGENTS.md` §5-G 明文）。

    两个 schema 的主键序列**各自独立**（`Order` 的 `id` 就是普通自增）⇒ **同一个 pk
    在两个租户里可以指两行完全不同的单**。这里不靠"序列恰好都从 1 开始"那种巧合，
    而是**当场造出**这个局面：在 A 里显式写一张 pk 与 B 那张单**相同**的单
    ⇒ `filter(pk=<B 那张单的 pk>)` 在 A 的 schema 里命中的是 **A 自己这一行**。

    ⇒ 若负断言写成"按 pk 取 B 那一行、断言在 A 里查不到"，它**恒假**（会命中 A 自己的行）；
    反过来断言"取到的是 A 的行"又**恒真**。两种都不产出结论 —— 这正是要避开的形状。
"""
        with tenant_context(self.tenant_b):
            b_pk = Order.objects.get(order_no=ORDER_NO_B).pk
            self.assertEqual(b_pk, B_ORDER_PK)
        # 在 A 里显式写一张**同 pk** 的单（⛔ 不是指望序列巧合，是写进去）
        twin = Order.objects.create(
            pk=b_pk,
            game=self.game_a,
            order_no=TWIN_ORDER_NO,
            project_text="占位项目",
        )
        self.assertEqual(twin.pk, b_pk)
        hit = Order.objects.get(pk=b_pk)
        # 命中是**A 自己**那一行，⛔ 不是 B 的单
        self.assertEqual(hit.order_no, TWIN_ORDER_NO)
        self.assertNotEqual(hit.order_no, ORDER_NO_B)
        # ⚠️ 自证"B 那一行确实在、只是不在这句查询的射程里"：它的 pk 与 twin **逐字相同**
        with tenant_context(self.tenant_b):
            self.assertEqual(Order.objects.get(pk=b_pk).order_no, ORDER_NO_B)
        connection.set_tenant(self.tenant)

    # ── A 组 + B 组：凭据层与数据层 ─────────────────────────────────────
    def test_a_app_can_read_its_own_order(self):
        """正向对照：A 的凭据 + A 的域名 + A 的单 ⇒ 200（没有它，下面的负断言可能是空转）。"""
        response = self._token_get_on(
            self.domain_a,
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_A),
            app_key=APP_KEY,
            app_secret=APP_SECRET,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            json.loads(response.content)["data"]["order_no"], ORDER_NO_A
        )

    def test_b_app_can_read_its_own_order_on_its_own_domain(self):
        """正向对照（B 侧）：B **自己**的凭据 + B 的域名 + B 的单 ⇒ 200。

    ⚠️ 这条是给下面那条 401 用的：它证明「B 的域名 + B 的表 + 对外这条路」**是通的**，
    所以 A 的凭据在同一个域名上拿 401 **只可能**是"B 里没有这个 appKey"。
"""
        response = self._token_get_on(
            DOMAIN_B,
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_B),
            app_key=APP_KEY_B,
            app_secret=APP_SECRET_B,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(json.loads(response.content)["data"]["order_no"], ORDER_NO_B)

    def test_a_credentials_on_the_domain_of_tenant_b_return_401(self):
        """A 的凭据打 **B 的域名** ⇒ 401（`ApiApp` 是每租户一份的表）。

    ⛔⭐ **改准（T7）**：本条原先拿 A 的 **appKey/secret 签名**打 B 的域名，
    断言的是签名那句（原文逐字保留：「B 里没有这个 appKey ⇒ 与"签名错"**同一句**」）。
    详情路径自 T7 起只认 **Bearer**（`PATCH` 与它同路径 ⇒ 共用一套认证）⇒ 凭据换成
    **A 的令牌**、断言换成 Bearer 那句。⚠️ 判据实质一个字未改：仍是
    「B 里没有这把凭据 ⇒ 401 + **不可分辨的同一句**」（⛔ 不给应用枚举面）。
"""
        response = self._token_get_on(
            DOMAIN_B,
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_B),
            app_key=APP_KEY,
            app_secret=APP_SECRET,
        )
        self.assertEqual(response.status_code, 401, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(body["code"], 401)
        # ⚠️ B 里没有这个 appKey ⇒ 与"令牌不对"**同一句**（§5.4：不给应用枚举面）。
        self.assertEqual(body["msg"], BEARER_INVALID_CREDENTIALS_MSG)

    def test_a_app_cannot_read_the_order_of_tenant_b(self):
        """A 的应用查 B 的订单号 / 来源单号 ⇒ 与「不存在」**同形**（§7.1）。"""
        # ① 详情：按 B 的**订单号**查 ⇒ code=1002（"查不到"），data 为 null
        detail = self._token_get_on(
            self.domain_a,
            DETAIL_TEMPLATE.format(order_no=ORDER_NO_B),
            app_key=APP_KEY,
            app_secret=APP_SECRET,
        )
        self.assertEqual(detail.status_code, 200, detail.content[:300])
        detail_body = json.loads(detail.content)
        self.assertEqual(detail_body["code"], BUSINESS)
        self.assertIsNone(detail_body["data"])

        # ② 列表：按 B 的**来源单号**查 ⇒ 空列表 + total=0（⛔ 不是"命中了一条别人的单"）
        # 集合路径只认 **Bearer 令牌**（§13.14-3）；判据一个字未改（200 + 空列表 + total=0）。
        listed = self._token_get_on(
            self.domain_a,
            LIST_PATH,
            data={"source_order_no": SOURCE_NO_B},
            app_key=APP_KEY,
            app_secret=APP_SECRET,
        )
        self.assertEqual(listed.status_code, 200, listed.content[:300])
        self.assertEqual(
            json.loads(listed.content)["data"], {"items": [], "total": 0}
        )

    # ⛔ 此处原有 `test_an_all_games_app_still_cannot_read_the_order_of_tenant_b`
# （钉「"全部游戏"不是"不过滤"」），那条按当前 schema 取游戏表的子查询已随游戏范围过滤
# 一起删除（§13.1）。跨租户隔离的内核由同组其余三条钉着、一条未放宽；
# 丢掉的判别力：它曾能抓「允许集合的查询不落在当前租户 schema 上」——今天查单端点
# 已没有任何按游戏取子查询的查询（§13.10），那种缺陷不可能发生。

    # ── D 组：兜底闸的**租户段**（跨租户影响这一面；429 本身在 test_openapi_throttling）──
    def test_the_bucket_is_per_tenant_even_for_the_same_app_key(self):
        """⭐ 桶里带 `connection.schema_name` ⇒ **租户 A 打满之后，租户 B 不受影响**。

    形状（额度压到 2）：先让 A 打满、再让 B 用**同一个 appKey**发一条 —— ⛔ B 那条**必须
    是 200**。B 里那个应用是**本用例当场建的**、`app_key` 与 A 的**逐字相同**
    （`app_key` 的唯一性只在**每个 schema 内部**成立）⇒ 这样两侧的桶键就**只差 schema 段**，
    把这一个变量单独拎出来。

    ⚠️ 去掉 `bucket_key` 里的 schema 段，B 那条就会撞在 A 那个**已满**的键上 ⇒ 429 ⇒ 本用例红
    （那次变异实测读数见 T6 的报告）。这是**跨租户影响**（`AGENTS.md` §4 明令不许）：
    一个租户的调用量把另一个租户拦掉，且**两边都看不出原因**。
"""
        try:
            with tenant_context(self.tenant_b):
                same_key_app_b = self._create_app(
                    name="隔离租户 B 的同名 appKey 应用",
                    app_key=APP_KEY,  # ← 与 A **逐字相同**（唯一性只在每个 schema 内部成立）
                    app_secret=APP_SECRET,
                    username="openapi-iso-b-samekey",
                )
                self.assertEqual(same_key_app_b.app_key, APP_KEY)
        finally:
            connection.set_tenant(self.tenant)

        with mock.patch(
            "apps.openapi.throttling.OPENAPI_FALLBACK_MAX_REQUESTS", 2
        ):
            # ① A 打满（两条都过）
            for _ in range(2):
                allowed = self._token_get_on(
                    self.domain_a, DETAIL_TEMPLATE.format(order_no=ORDER_NO_A)
                )
                self.assertEqual(allowed.status_code, 200, allowed.content[:300])
            # ② A 的第 3 条被挡（自证桶**真的**满了）
            over = self._token_get_on(
                self.domain_a, DETAIL_TEMPLATE.format(order_no=ORDER_NO_A)
            )
            self.assertEqual(over.status_code, THROTTLED_STATUS, over.content[:200])
            # ③ 同一个 appKey、**B 的域名** ⇒ 必须还是 200（并真的读到 B 自己的单）
            other_tenant = self._token_get_on(
                DOMAIN_B,
                DETAIL_TEMPLATE.format(order_no=ORDER_NO_B),
                app_key=APP_KEY,
                app_secret=APP_SECRET,
            )
            self.assertEqual(
                other_tenant.status_code, 200, other_tenant.content[:300]
            )
            self.assertEqual(
                json.loads(other_tenant.content)["data"]["order_no"], ORDER_NO_B
            )

    # ── 工具 ───────────────────────────────────────────────────────────
    @staticmethod
    def _count_here(order_no):
        """按订单号数**当前连接所属 schema** 里的行（⚠️ 用订单号，⛔ 不用主键）。"""
        return Order.objects.filter(order_no=order_no).count()

    @staticmethod
    def _create_app(*, name, app_key, app_secret, username):
        """在**当前 schema** 里建一个应用（只有 `name` / `app_key` / `app_secret` / 创建人）。

    ⛔⭐ **改准（T10）**：本助手原先还要 `seed_catalog()` 并把
    `order.view_detail` 节点与调用方给的游戏挂上去 —— 那三列（节点 M2M / 游戏 M2M /
    「全部游戏」开关）已随 v1 面删除（设计文档 §13.1）⇒ 参数与那几行**同一次**删掉。
    ⚠️ **判据不受影响**：本文件的断言全打在**跨租户**这一层（凭据 / 数据 / 桶），
    ⛔ 与"这个应用被授权了什么"无关（§13.10：一个应用凭据 = 本租户全部业务数据）。
"""
        creator, _ = User.objects.get_or_create(
            username=username, defaults={"password": "unused-in-this-test"}
        )
        return ApiApp.objects.create(
            name=name, app_key=app_key, app_secret=app_secret, created_by=creator
        )

    def _token_get_on(self, domain, path, data=None, *, app_key=None, app_secret=None):
        """按 `domain` 发一条**带 Bearer 令牌**的 GET；结束后把连接钉回租户 A。

    ⛔⭐ **为什么需要它（T6）**：`v1/orders/` 这条**集合**路径的认证自本批起
    从 HMAC 签名换成 **Bearer 令牌**（设计文档 §13.14-3）⇒ 打这条路径的用例必须带令牌；
    详情路径**不动**（仍走签名，用上面的 `_get_on`）。
    ⛔⭐ **改准（T7）**：上句「详情路径不动」已作废——详情路径
    **也**切成 Bearer 了 ⇒ 本文件**两条路径都用它**，`_get_on` 已无调用点。
    ⚠️ 令牌当场签发（`tokens.issue`，用它自己的密钥）——⛔ 不把签发 / 编码逻辑抄一份到用例里。
    ⚠️ 与 `_get_on` 同一个 `finally` 口径（防御性还原 schema）。
"""
        app_key = APP_KEY if app_key is None else app_key
        app_secret = APP_SECRET if app_secret is None else app_secret
        token = tokens.issue(app_key, app_secret)
        try:
            return Client(SERVER_NAME=domain).get(
                path,
                data or {},
                HTTP_AUTHORIZATION=f"Bearer {token}",
                REMOTE_ADDR=SOURCE_IP,
            )
        finally:
            connection.set_tenant(self.tenant)
