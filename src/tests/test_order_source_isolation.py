"""`order-sources` 的跨租户隔离（spec §8 #2 后半 / #7；`AGENTS.md` §4 的红线）。

`AGENTS.md` §4 要求「任何新增查询都必须携带租户上下文，并补一条
**以租户 A 的凭据访问 B 的数据必须失败**的测试」。本线的查询是新增的
`OrderSourceViewSet`（`OrderSource.objects.all()`），所以红线在**本文件**闭合。

**本文件证什么 / 不证什么**（分开读，别混）：

- **数据层**（`OrderSourceDataIsolationTests`）：同一个 `name` 在两个租户里各建一行，
  各自只看得见**自己那一行**；跨租户同名**允许**（约束是 per-schema 的，spec §8 #2）。
- **HTTP 层**（`OrderSourceHttpIsolationTests`）：每个租户一个**真实登录** client + 各自域名，
  用列表端点断言另一租户的来源名不出现，且**先证明自己有数据**（非空哨兵）。
- **凭据层**（同文件最后一条）：A 的会话拿到 **B 的域名**上 ⇒ 拒绝（**不是** 200 + A/B 的数据）。

⚠️ **判据纪律**（`AGENTS.md` §5，本任务的核心）：

1. **不用主键指认「另一个租户的那一行」**：两个 schema 的主键序列**各自从 1 开始**
   ⇒ `filter(id=a_pk)` 在 B 里命中的是 **B 自己的行**，断言恒真/恒假。
   本文件一律用**租户内唯一、且两租户取值不同**的字段：`name`（租户内唯一）与 `remark`。
2. **非空哨兵**：每条否定断言前先证明「自己这一侧看得到自己那一行」，
   否则空列表能让 `assertNotIn` 假通过。**不用 `count()`** 当可见性判据——
   它的通过与否取决于两个租户各有多少行，而不是「这一行可不可见」。
3. 期望值写**字面值**（错误码写 `401`，不写符号）；断言打**响应体 / 数据库行的值**。
4. **每条测试都能单独运行**（P1 的教训：红线测试曾靠测试顺序假通过）：
   HTTP 层那个类的 `setUp` 第一行显式把连接钉回租户 A。
   （数据层那个类没有 `setUp`——它用 `tenant_context(...)` 逐块自钉，见其用例。）
"""
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from apps.business.models import OrderSource, OrderSourceGroup
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

BASE = "/api/business/order-sources/"

#: 租户 A = `TenantTestCase` 的默认租户（schema `test`），域名与既有账号测试同口径。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名。刻意与 A 不同：`Domain` 上有唯一约束，撞了会在 `provision_tenant` 里报错。
#: 后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "order-src-api-b.test.local"
SCHEMA_B = "order_src_api_b"

#: 判据用的业务值（租户内唯一、两租户取值不同；**不是主键**）。
NAME_SHARED = "同名来源"
REMARK_A = "只在租户 A 的描述"
REMARK_B = "只在租户 B 的描述"

#: 最恶情形 twin 的用户名（两个租户里同名、密码哈希逐字节相同）。
TWIN_USERNAME = "src-cred-twin"


def _make_session(*, plane, user_pk, password_hash):
    """在 **public** 的 `django_session` 表里写一条会话，返回 `session_key`。

    ⚠️ 会话行的落点与 `schema_context(...)` 无关：`django.contrib.sessions` 是
    `SHARED_APPS` 成员 ⇒ `django_session` **只存在于 public**。所以「这条会话属于哪个
    租户」**不是**由它写在哪决定的，而是由 `identity_plane` 与 `_auth_user_hash` 决定的
    （后者才是让 Django 的 `get_user()` 认出 twin 的关键）。`schema_context` 在这里只是
    与 `tests/test_p5_credential_plane.py` 的同一手法保持一致。

    `_auth_user_hash` 用 Django **自己的** `get_session_auth_hash()` 推导
    （`password_hash` 传目标用户自己的 `password` 字段），**不要**把 `password` 原样塞进去：
    那样 Django 会替我们拒绝，红线测试将**零判别力**。
    """
    session_auth_hash = User(pk=user_pk, password=password_hash).get_session_auth_hash()
    with schema_context("public"):
        store = SessionStore()
        store["_auth_user_id"] = str(user_pk)
        store["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        store["_auth_user_hash"] = session_auth_hash
        store["identity_plane"] = plane
        store.save()
    return store.session_key


def _session_client(session_key, domain):
    """把手工 materialize 的会话装进 client（与凭据平面测试的 `_session_client` 同形）。"""
    client = Client(SERVER_NAME=domain)
    client.cookies["sessionid"] = session_key
    return client


class OrderSourceDataIsolationTests(TenantTestCase):
    """数据层：跨租户同名允许，且各自只看得见自己那一行。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建：用例内部的 schema DDL 与外层事务相冲。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单来源隔离租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；
        # 「B 也是完整可用的租户」是本类的前提，不能依赖 schema 恰好是本次新建的。
        seed_tenant(cls.tenant_b)

    def test_rows_do_not_cross_and_same_name_is_allowed(self):
        with tenant_context(self.tenant):
            OrderSource.objects.create(name=NAME_SHARED, remark=REMARK_A)
        # 跨租户同名**允许**：若约束不是 per-schema，这一行会抛 IntegrityError。
        with tenant_context(self.tenant_b):
            OrderSource.objects.create(name=NAME_SHARED, remark=REMARK_B)
            # 非空哨兵 + 判据：B 里按 name 只能查到 B 自己那一行。
            self.assertEqual(
                list(
                    OrderSource.objects.filter(name=NAME_SHARED).values_list(
                        "remark", flat=True
                    )
                ),
                [REMARK_B],
            )
        # 反向对照：A 的那一行仍在，且没有被 B 的写入覆盖。
        with tenant_context(self.tenant):
            self.assertEqual(
                list(
                    OrderSource.objects.filter(name=NAME_SHARED).values_list(
                        "remark", flat=True
                    )
                ),
                [REMARK_A],
            )


class OrderSourceHttpIsolationTests(TenantTestCase):
    """HTTP 层 + 凭据层：真实登录 client 在各自域名上打真实端点。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单来源隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（schema 上下文会跨用例残留，见模块说明纪律 4）。
        connection.set_tenant(self.tenant)

    def test_each_tenant_sees_only_its_own_rows(self):
        with tenant_context(self.tenant):
            OrderSource.objects.create(name="A 的来源", remark=REMARK_A)
            client_a = make_logged_in_client(domain=DOMAIN_A, username="src-iso-a")
        with tenant_context(self.tenant_b):
            # ⚠️ `POST /order-sources/` 现在还要
            # `system.add_edit_source`；`client_a` 只读、无需节点。
            # `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）。
            client_b = make_logged_in_client(
                domain=DOMAIN_B, username="src-iso-b",
                groups=("来源组",), nodes=("system.add_edit_source",),
            )
            created = client_b.post(
                BASE,
                {"name": "B 的来源", "remark": REMARK_B},
                content_type="application/json",
            )
            # 非空哨兵：先证明 B 自己看得见 B 的数据，否定断言才有判别力。
            self.assertEqual(created.json()["code"], 0)
            # `client_b` 的默认 Host 已经是 B：`make_logged_in_client` 返回的是
            # `Client(SERVER_NAME=domain)`（`tests/p5_auth_helpers.py:68`），
            # `domain` 参数不只在登录那一次生效。T2 评审的变异实测：另写一行
            # `client.defaults["SERVER_NAME"] = DOMAIN_B` 对 `client_b` 是 no-op
            # ⇒ 这里直接打，不再做「改 Host 再还原」那一套。
            # 请求会把连接的 tenant 切成 B，但本块是 `tenant_context(self.tenant_b)`，
            # 退出时由它还原（`django_tenants/utils.py:148-152`）。
            body = client_b.get(BASE).json()
            self.assertEqual(body["code"], 0)
            self.assertEqual(
                [i["remark"] for i in body["data"]["items"]],
                [REMARK_B],
            )

        with tenant_context(self.tenant):
            body = client_a.get(BASE).json()
            self.assertEqual(body["code"], 0)
            self.assertEqual(
                [i["remark"] for i in body["data"]["items"]],
                [REMARK_A],
            )
            self.assertNotIn(REMARK_B, [i["remark"] for i in body["data"]["items"]])

    def test_credential_of_a_cannot_read_b(self):
        """凭据层：**以租户 A 的凭据访问 B 的数据必须失败**（`AGENTS.md` §4 的红线）。

        ⚠️ **为什么不能拿「A 登录一次、把 Cookie 打到 B 的域名」当构造**（本文件实测踩到）：
        那样 Django 自己的会话哈希兜底就会先拒（`password` 两边不同 ⇒ HMAC 对不上 ⇒
        flush + 匿名），得到 401 的是 **Django**，不是本项目的平面判据——测试对
        「守卫还在不在」**零判别力**（实测：把守卫与认证类的跨平面分支同时摘掉仍是 401）。

        这里照 `tests/test_p5_credential_plane.py::test_2` 的**最恶情形 twin** 构造：
        B 里站一个与 A **密码哈希逐字节相同**的用户，会话在 **B 的 schema 内** materialize、
        `identity_plane` 声明是 A ⇒ Django 的 `get_user()` 会**成功**认出 B 的 twin，
        唯一还能拒的就是平面不变式。两条请求各司其职：

        1. **对照**（构造物自证）：同一条会话、只把平面声明换成 B ⇒ 必须 **200 + B 自己的数据**。
           没有这一步，构造退化（twin 没被认出来）也会让下面的 401 以**另一种原因**通过
           （E-0003：状态码可能来自别处）。
        2. **红线**：平面声明是 A ⇒ **401 + `code:401` + `data:null`**，
           且响应体里**既有 A 的标记也有 B 的标记都不出现**（既不许读到 B 的数据，
           也不许在 B 的域名上读到 A 的数据）。
        """
        with tenant_context(self.tenant):
            user_a = User.objects.create_user(username=TWIN_USERNAME, password="pw-red-line")
            hash_a = user_a.password
        with tenant_context(self.tenant_b):
            user_b = User.objects.create_user(username=TWIN_USERNAME, password="pw-other")
            user_b.password = hash_a  # 逐字节相同 ⇒ Django 的哈希兜底不再拒
            user_b.save(update_fields=["password"])
            user_b.refresh_from_db()
            pk_b, hash_b = user_b.pk, user_b.password
            OrderSource.objects.create(name="B 的来源", remark=REMARK_B)
        with tenant_context(self.tenant):
            OrderSource.objects.create(name="A 的来源", remark=REMARK_A)
        self.assertEqual(hash_a, hash_b, "构造失败：两边密码哈希必须逐字节相同")

        # ── 对照：平面声明 = B ⇒ 这条会话在 B 的域名上**必须被接受** ──────────
        control_id = _make_session(
            plane=self.tenant_b.schema_name, user_pk=pk_b, password_hash=hash_b
        )
        control = _session_client(control_id, DOMAIN_B).get(BASE)
        self.assertEqual(
            control.status_code,
            200,
            f"对照失败：B 自己的 twin 会话都没被认出来，红线那条没有判别力"
            f"（{control.status_code}：{control.content[:200]!r}）",
        )
        self.assertEqual(
            [i["remark"] for i in control.json()["data"]["items"]],
            [REMARK_B],
            "对照失败：B 的域名上看不到 B 自己的数据",
        )

        # ── 红线：平面声明 = A ⇒ 同一个 B 的 twin 也必须被拒 ─────────────────
        cross_id = _make_session(
            plane=self.tenant.schema_name, user_pk=pk_b, password_hash=hash_b
        )
        rejected = _session_client(cross_id, DOMAIN_B).get(BASE)
        self.assertEqual(rejected.status_code, 401)
        body = rejected.json()
        self.assertEqual(body["code"], 401)
        self.assertIsNone(body["data"])
        text = rejected.content.decode()
        self.assertNotIn(REMARK_B, text, "响应里带了 B 的数据")
        self.assertNotIn(REMARK_A, text, "响应里带了 A 的数据")


# ══════════════════════════════════════════════════════════════════════
# 公司店铺分组（计划 §6 批次 1a 「隔离同形」）
# ══════════════════════════════════════════════════════════════════════
GROUPS = "/api/business/order-source-groups/"
GROUP_SHARED = "同名分组"


class OrderSourceGroupIsolationTests(TenantTestCase):
    """分组字典的跨租户隔离（数据层 + HTTP 层；纪律同本模块前面那两条）。

    判据用 `name`（租户内唯一）而不是主键：两个 schema 的主键都从 1 开始。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单来源隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_same_group_name_in_two_tenants_is_allowed(self):
        with tenant_context(self.tenant):
            OrderSourceGroup.objects.create(name=GROUP_SHARED, order_by=1)
        with tenant_context(self.tenant_b):
            OrderSourceGroup.objects.create(name=GROUP_SHARED, order_by=9)
            self.assertEqual(
                list(OrderSourceGroup.objects.filter(name=GROUP_SHARED).values_list("order_by", flat=True)),
                [9],
            )
        with tenant_context(self.tenant):
            self.assertEqual(
                list(OrderSourceGroup.objects.filter(name=GROUP_SHARED).values_list("order_by", flat=True)),
                [1],
            )

    def test_cannot_attach_another_tenants_source_to_new_group(self):
        with tenant_context(self.tenant):
            foreign_source = OrderSource.objects.create(name="A 的来源", pk=90000)
        with tenant_context(self.tenant_b):
            local_source = OrderSource.objects.create(name="B 的来源")
            client = make_logged_in_client(
                domain=DOMAIN_B, username="grp-link-iso",
                groups=("分组租户",), nodes=("system.add_edit_source",),
            )
            rejected = client.post(
                GROUPS, {"name": "拒绝组", "source_ids": [foreign_source.pk, local_source.pk]},
                content_type="application/json",
            ).json()
            self.assertEqual(rejected["code"], 1000)
            self.assertFalse(OrderSourceGroup.objects.filter(name="拒绝组").exists())
            accepted = client.post(
                GROUPS, {"name": "本地组", "source_ids": [local_source.pk]},
                content_type="application/json",
            ).json()
            self.assertEqual(accepted["code"], 0)
            self.assertEqual(list(local_source.groups.values_list("name", flat=True)), ["本地组"])
        with tenant_context(self.tenant):
            self.assertFalse(foreign_source.groups.exists())

    def test_each_tenant_only_sees_its_own_groups_over_http(self):
        with tenant_context(self.tenant):
            OrderSourceGroup.objects.create(name="A 的分组")
            client_a = make_logged_in_client(domain=DOMAIN_A, username="grp-iso-a")
        with tenant_context(self.tenant_b):
            client_b = make_logged_in_client(
                domain=DOMAIN_B, username="grp-iso-b",
                groups=("分组租户",), nodes=("system.add_edit_source",),
            )
            created = client_b.post(
                GROUPS, {"name": "B 的分组"}, content_type="application/json"
            )
            self.assertEqual(created.json()["code"], 0)
            body = client_b.get(GROUPS).json()
            self.assertEqual(body["code"], 0)
            # 非空哨兵：B 看得见自己的。
            self.assertEqual([i["name"] for i in body["data"]["items"]], ["B 的分组"])
            self.assertNotIn("A 的分组", [i["name"] for i in body["data"]["items"]])

        with tenant_context(self.tenant):
            body = client_a.get(GROUPS).json()
            self.assertEqual(body["code"], 0)
            self.assertEqual([i["name"] for i in body["data"]["items"]], ["A 的分组"])
            self.assertNotIn("B 的分组", [i["name"] for i in body["data"]["items"]])
